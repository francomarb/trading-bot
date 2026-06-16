"""Operator command queue: durable record of operator-issued commands.

Why this module exists
----------------------
Operator Controls Phase A PR-2. The proposal (`docs/operator_controls_proposal.md`
§7) defines a SQLite-backed command queue as the single channel through which
operator intent reaches the running bot. The CLI (`scripts/operator.py`) writes
rows; the engine's per-cycle poll claims and processes them.

For Phase A only two state-changing actions land:
- ``halt``                 — sticky kill switch via existing `RiskManager`
- ``resume-after-halt``    — clears the sticky halt after re-reconciliation

Every other command listed in proposal §5 (`pause-entries`, `reduce-position`,
etc.) is rejected with ``status='rejected_unsupported_phase_a'`` so the
operator gets an immediate, audited "not yet wired" response rather than a
silent no-op.

Design contract (per implementation plan)
-----------------------------------------
- Self-contained module: imports only from stdlib + ``engine.lifecycle`` for
  uid generation. Does not import from ``engine.trader``, ``risk/``, or
  ``strategies/``. Any subsystem can ``from engine.operator_queue import
  OperatorCommandStore`` without circular-import risk.
- ``OperatorCommandRow`` is a typed frozen dataclass with no engine deps.
- Store accepts a ``sqlite3.Connection``; it does not own the DB connection.
- DDL lives here but is executed by ``reporting.logger.TradeLogger._ensure_db()``
  so all schema initialisation happens through the existing single migration
  path (same discipline as ``engine.lifecycle``).
- Every write commits explicitly so a crash between operations cannot lose
  a command-state transition.
- The caller wraps store calls in ``try/except logger.warning`` so a DB I/O
  failure on the engine side cannot raise into the trading loop. The CLI
  side can raise — it is interactive and should fail loudly.

Crash-recovery semantics (proposal §7.1)
----------------------------------------
- A command is created in ``status='pending'``.
- ``claim_next_pending`` atomically transitions the oldest still-fresh
  pending command to ``status='accepted'`` and returns it. Any pending row
  older than ``OPERATOR_COMMAND_EXPIRY_SECONDS`` is transitioned to
  ``status='rejected_expired'`` and skipped — stale intent must not auto-fire.
- The engine processes the accepted command and transitions it to
  ``succeeded`` / ``failed`` / ``rejected_<...>``.
- ``client_order_id`` plumbing exists in the schema for Phase C destructive
  commands but is not populated in Phase A (no broker orders here).
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


OPERATOR_QUEUE_SCHEMA_VERSION = 1


# ── DDL ───────────────────────────────────────────────────────────────


_CREATE_OPERATOR_COMMANDS_SQL = """
CREATE TABLE IF NOT EXISTS operator_commands (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version          INTEGER NOT NULL DEFAULT 1,
    command_uid             TEXT    NOT NULL UNIQUE,
    created_at              TEXT    NOT NULL,
    accepted_at             TEXT,
    completed_at            TEXT,
    requested_by            TEXT,
    action                  TEXT    NOT NULL,
    target_position_uid     TEXT,
    target_strategy         TEXT,
    params_json             TEXT    NOT NULL DEFAULT '{}',
    reason                  TEXT    NOT NULL,
    status                  TEXT    NOT NULL DEFAULT 'pending',
    client_order_id         TEXT,
    result_json             TEXT
);
"""


_CREATE_OPERATOR_COMMANDS_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_operator_commands_status_created "
    "ON operator_commands(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_operator_commands_target_position_uid "
    "ON operator_commands(target_position_uid)",
    "CREATE INDEX IF NOT EXISTS idx_operator_commands_target_strategy "
    "ON operator_commands(target_strategy)",
)


# Status values that flow through the queue. Validated at the API boundary.
#
# Transitions:
#   pending → accepted → succeeded | failed
#   pending → rejected_expired | rejected_unsupported_phase_a |
#             rejected_<bot-specific>
#
# `rejected_*` statuses are terminal and mean the engine refused to act —
# the operator should re-issue (or not) after checking state. `failed`
# means the engine TRIED to act and something went wrong; the result_json
# carries the broker/exception details.
VALID_STATUSES = frozenset({
    "pending",
    "accepted",
    "succeeded",
    "failed",
    "rejected_expired",
    "rejected_unsupported_phase_a",
    "rejected_validation",
})

# Actions the CLI may submit. Validated at insert time — the CLI cannot
# write an unknown action.
#
# Phase B adds four soft-control actions: pause-entries / resume-entries
# block all new entries while existing position management continues;
# pause-strategy / resume-strategy scope the block to a single strategy
# (the strategy name is passed in target_strategy, validated at the
# engine handler).
#
# Phase C adds three destructive position-control actions:
# - close-position: full close of one lifecycle, identified by
#   target_position_uid. Cancels any protective stop on that position,
#   submits a closing order via the engine (origin_kind='operator' on
#   the substrate row), waits for fill confirmation, then releases the
#   symbol lock.
# - reduce-position: partial close. Same flow but the engine computes
#   the reduce qty from the operator's --pct (carried in params_json)
#   and updates the lifecycle's current_qty to the residual via the
#   existing _reduce_lifecycle_for_owner_key helper.
# - cancel-position-orders: cancel every non-terminal protective stop
#   / exit / partial_close row for the position. Does NOT submit any
#   new orders. Useful before a manual close or when a stale broker
#   stop needs operator intervention.
VALID_ACTIONS = frozenset({
    "halt",
    "resume-after-halt",
    "pause-entries",
    "resume-entries",
    "pause-strategy",
    "resume-strategy",
    "close-position",
    "reduce-position",
    "cancel-position-orders",
})


# ── ID generator ──────────────────────────────────────────────────────


def new_command_uid() -> str:
    """The single producer of operator command UIDs.

    Returns ``cmd_<32-hex>`` (36 chars total). Mirrors the
    ``pos_<hex>`` convention used by `engine.lifecycle.new_position_uid`
    so logs and CLI output are visually consistent.
    """
    return f"cmd_{uuid.uuid4().hex}"


# ── Dataclass ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OperatorCommandRow:
    """One row of ``operator_commands``.

    Frozen so callers can't accidentally mutate a snapshot. Re-query
    the store to observe updates.
    """

    command_uid: str
    created_at: str
    accepted_at: str | None
    completed_at: str | None
    requested_by: str | None
    action: str
    target_position_uid: str | None
    target_strategy: str | None
    params: dict
    reason: str
    status: str
    client_order_id: str | None
    result: dict


# ── Store ─────────────────────────────────────────────────────────────


class OperatorCommandStore:
    """Read/write API for the ``operator_commands`` table.

    The store does NOT own the DB connection — it expects a connection
    where the table already exists (created by
    `reporting.logger.TradeLogger._ensure_db()`). This mirrors the
    pattern used by `engine.lifecycle.PositionLifecycleStore`.

    All writes call ``conn.commit()`` directly. The engine wraps store
    calls in ``try/except logger.warning`` so I/O failure cannot raise
    into the trading loop. The CLI can raise — it is interactive.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        # PR-65 review F3: serialise all DB ops on this connection so
        # the heartbeat thread (calling claim_next_pending / mark_*)
        # and the Telegram listener thread (calling insert) cannot
        # interleave on the same shared connection. Cycle-thread
        # writes go through a SEPARATE connection (TradeLogger's) so
        # the lock here is only for the queue's own threads.
        self._lock = threading.RLock()

    # ── Writes (CLI side) ────────────────────────────────────────────

    def insert(
        self,
        *,
        command_uid: str,
        action: str,
        reason: str,
        requested_by: str | None = None,
        target_position_uid: str | None = None,
        target_strategy: str | None = None,
        params: dict | None = None,
    ) -> None:
        """Append a new command row in ``status='pending'``.

        Validates ``action`` against ``VALID_ACTIONS`` and ``reason``
        as non-empty (proposal §6 requires a reason for every state-
        changing command). The CLI is the only expected caller.

        Raises ValueError on validation failure;
        sqlite3.IntegrityError on duplicate command_uid.
        """
        if action not in VALID_ACTIONS:
            raise ValueError(
                f"action must be one of {sorted(VALID_ACTIONS)}; got {action!r}"
            )
        if not command_uid or not command_uid.startswith("cmd_"):
            raise ValueError(
                f"command_uid must be a 'cmd_<hex>' string; got {command_uid!r}"
            )
        if not reason or not reason.strip():
            raise ValueError("reason must be a non-empty string")

        now = _utc_now_iso()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO operator_commands (
                    schema_version, command_uid, created_at, accepted_at,
                    completed_at, requested_by, action, target_position_uid,
                    target_strategy, params_json, reason, status,
                    client_order_id, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    OPERATOR_QUEUE_SCHEMA_VERSION,
                    command_uid,
                    now,
                    None,
                    None,
                    requested_by,
                    action,
                    target_position_uid,
                    target_strategy,
                    json.dumps(params or {}),
                    reason.strip(),
                    "pending",
                    None,
                    None,
                ),
            )
            self._conn.commit()

    # ── Writes (engine side) ─────────────────────────────────────────

    def claim_next_pending(
        self,
        *,
        now_iso: str | None = None,
        expiry_seconds: int,
    ) -> OperatorCommandRow | None:
        """Atomically claim the oldest still-fresh pending command.

        Steps in one transaction:
          1. Expire stale rows: any ``pending`` row older than
             ``expiry_seconds`` is set to ``rejected_expired`` so it
             never auto-fires.
          2. Pick the oldest remaining ``pending`` row.
          3. Transition it to ``accepted`` and stamp ``accepted_at``.

        Returns the claimed row, or None when no fresh pending row
        exists. Concurrent callers cannot race — the transaction
        guarantees only one returns the row.

        `now_iso` is injectable for tests; production uses UTC now.
        """
        now = now_iso or _utc_now_iso()
        cutoff = _iso_minus_seconds(now, expiry_seconds)

        with self._lock:
            # Expire stale rows first. Idempotent — operates only on `pending`.
            self._conn.execute(
                "UPDATE operator_commands "
                "SET status = 'rejected_expired', completed_at = ? "
                "WHERE status = 'pending' AND created_at < ?",
                (now, cutoff),
            )

            # Pick the oldest remaining pending row.
            row = self._conn.execute(
                "SELECT id, " + _SELECT_COLUMNS_CSV + " "
                "FROM operator_commands "
                "WHERE status = 'pending' "
                "ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                self._conn.commit()
                return None

            row_id = row[0]
            # Transition to accepted atomically. The id is unique so the
            # WHERE clause guarantees a single update; combined with the
            # same transaction the next call will not see this row in the
            # SELECT above.
            self._conn.execute(
                "UPDATE operator_commands "
                "SET status = 'accepted', accepted_at = ? WHERE id = ?",
                (now, row_id),
            )
            self._conn.commit()

            # Re-read the row to return the post-update state.
            return self._row_to_obj(self._conn.execute(
                "SELECT " + _SELECT_COLUMNS_CSV + " "
                "FROM operator_commands WHERE id = ?",
                (row_id,),
            ).fetchone())

    def mark_succeeded(
        self,
        *,
        command_uid: str,
        result: dict | None = None,
    ) -> None:
        """Transition ``accepted`` → ``succeeded`` after the engine
        completes the action. ``result`` is stored as JSON for the
        audit trail."""
        self._set_terminal(
            command_uid=command_uid,
            status="succeeded",
            result=result,
        )

    def mark_failed(
        self,
        *,
        command_uid: str,
        result: dict | None = None,
    ) -> None:
        """Transition ``accepted`` → ``failed`` when the engine tried
        the action and it raised or otherwise didn't complete. The
        ``result`` dict should carry the error context."""
        self._set_terminal(
            command_uid=command_uid,
            status="failed",
            result=result,
        )

    def mark_rejected(
        self,
        *,
        command_uid: str,
        status: str,
        result: dict | None = None,
    ) -> None:
        """Transition to a rejection terminal status. Engine calls this
        when it sees a Phase B/C action in Phase A
        (``rejected_unsupported_phase_a``) or fails validation
        (``rejected_validation``).

        The `pending` → `rejected_expired` path is handled inside
        `claim_next_pending` and does not go through this method.
        """
        if status not in {
            "rejected_unsupported_phase_a",
            "rejected_validation",
        }:
            raise ValueError(
                f"mark_rejected: status must be a recognised rejection; "
                f"got {status!r}"
            )
        self._set_terminal(
            command_uid=command_uid,
            status=status,
            result=result,
        )

    def _set_terminal(
        self,
        *,
        command_uid: str,
        status: str,
        result: dict | None,
    ) -> None:
        if status not in VALID_STATUSES:
            raise ValueError(
                f"status must be one of {sorted(VALID_STATUSES)}; "
                f"got {status!r}"
            )
        with self._lock:
            self._conn.execute(
                "UPDATE operator_commands "
                "SET status = ?, completed_at = ?, result_json = ? "
                "WHERE command_uid = ?",
                (
                    status,
                    _utc_now_iso(),
                    json.dumps(result) if result else None,
                    command_uid,
                ),
            )
            self._conn.commit()

    # ── Reads ─────────────────────────────────────────────────────────

    def get_by_command_uid(self, command_uid: str) -> OperatorCommandRow | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT " + _SELECT_COLUMNS_CSV + " "
                "FROM operator_commands WHERE command_uid = ?",
                (command_uid,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_obj(row)

    def recent(self, *, limit: int = 20) -> list[OperatorCommandRow]:
        """Most recent commands, newest first. CLI's ``commands`` uses
        this for the audit view."""
        if limit <= 0:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT " + _SELECT_COLUMNS_CSV + " "
                "FROM operator_commands "
                "ORDER BY created_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        return [self._row_to_obj(r) for r in rows]

    def count_pending(self) -> int:
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM operator_commands WHERE status = 'pending'"
            ).fetchone()[0]

    # ── Internal ──────────────────────────────────────────────────────

    @staticmethod
    def _row_to_obj(row) -> OperatorCommandRow:
        params = _safe_json(row[8])
        result = _safe_json(row[12])
        return OperatorCommandRow(
            command_uid=row[0],
            created_at=row[1],
            accepted_at=row[2],
            completed_at=row[3],
            requested_by=row[4],
            action=row[5],
            target_position_uid=row[6],
            target_strategy=row[7],
            params=params,
            reason=row[9],
            status=row[10],
            client_order_id=row[11],
            result=result,
        )


_SELECT_COLUMNS_CSV = (
    "command_uid, created_at, accepted_at, completed_at, requested_by, "
    "action, target_position_uid, target_strategy, params_json, reason, "
    "status, client_order_id, result_json"
)


# ── Helpers ───────────────────────────────────────────────────────────


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso_minus_seconds(iso: str, seconds: int) -> str:
    """Return an ISO timestamp `seconds` seconds before `iso`.

    Used by `claim_next_pending` to compute the expiry cutoff. Accepts
    both timezone-aware and naive ISO strings (tests sometimes pass
    naive ones); always returns the same format the input used.
    """
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        # Fallback: assume the operator passed something parseable
        # without offset. Best-effort; never raises here.
        dt = datetime.now(timezone.utc)
    from datetime import timedelta
    return (dt - timedelta(seconds=seconds)).isoformat()


def _safe_json(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        loaded = json.loads(raw)
        if isinstance(loaded, dict):
            return loaded
        return {"_raw": loaded}
    except (TypeError, ValueError):
        return {"_raw": raw}
