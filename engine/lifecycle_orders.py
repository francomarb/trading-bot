"""
Per-order lifecycle substrate — DDL constants and shape.

This module owns the schema for the ``position_lifecycle_orders`` table
and the related position-level partial unique index that the foundation
PR adds to the existing ``position_lifecycle`` table. The DDL constants
live here so ``reporting.logger.TradeLogger._ensure_db`` can import and
execute them through the single migration path the rest of the schema
already flows through.

Design reference: ``docs/order_lifecycle_state_machine.md`` (the
discovery doc landed in PR #59) — particularly §6.2 for the schema,
§6.3 for the per-order state machine, §6.4 for ``apply_order_event``
atomic semantics, §6.6 / §6.6.1 for rollup queries, §10 for the
compensating-patch absorption matrix, §12.1 for the regression-test
matrix, and §12.2 for migration prerequisites.

Per the design's §11 ownership boundary:

- ``position_lifecycle_orders`` owns durable per-order intent and pre-fill
  benchmark provenance. It does NOT carry realized P&L or computed slippage.
- ``position_lifecycle`` keeps aggregate state and identity.
- ``trades`` remains the source of truth for realized P&L and computed
  ``slippage_signed_bps`` / ``slippage_adverse_bps``.

The store API (``PositionLifecycleOrdersStore``) lands in a follow-up
commit on this branch; this module ships only the DDL so the bootstrap
order in ``_ensure_db`` stays clean.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Sequence


# ── Schema version ──

# Bumped together with any backward-incompatible change to the
# ``position_lifecycle_orders`` schema (column type changes, index
# removals, etc.). Additive column adds via ALTER do NOT bump.
LIFECYCLE_ORDERS_SCHEMA_VERSION = 1


# ── DDL ──

_CREATE_POSITION_LIFECYCLE_ORDERS_SQL = """
CREATE TABLE IF NOT EXISTS position_lifecycle_orders (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_uid                  TEXT    NOT NULL,
    role                          TEXT    NOT NULL,

    -- Broker identity. order_id is NULL while the row is `pending`
    -- (created before submit returns); populated once the broker
    -- accepts the submission. client_order_id is always populated.
    order_id                      TEXT,
    client_order_id               TEXT    NOT NULL,

    -- Order intent — captured at row insert and immutable thereafter.
    order_type                    TEXT    NOT NULL,
    order_class                   TEXT    NOT NULL,
    time_in_force                 TEXT    NOT NULL,
    side                          TEXT    NOT NULL,
    intended_qty                  REAL    NOT NULL,
    intended_stop_price           REAL,
    intended_trigger_price        REAL,
    intended_limit_price          REAL,
    intended_take_profit_price    REAL,

    -- Order relationships.
    parent_order_id               TEXT,
    replaces_order_id             TEXT,

    -- Origin — schema-compatible hooks for Phase C operator-issued
    -- commands. Bot-originated rows leave origin_kind='bot' and
    -- operator_command_uid NULL. Foundation PR does NOT implement
    -- destructive operator commands; the columns exist so Phase C
    -- can populate them without a schema migration.
    origin_kind                   TEXT    NOT NULL DEFAULT 'bot',
    operator_command_uid          TEXT,

    -- Pre-fill slippage benchmark provenance. Canonical Phase 1
    -- naming. The per-order table carries INTENT only — computed
    -- slippage_signed_bps / slippage_adverse_bps stay on `trades`.
    slippage_benchmark_price      REAL,
    slippage_benchmark_kind       TEXT,
    slippage_benchmark_timestamp  TEXT,
    slippage_measurement_quality  TEXT,

    -- Lifecycle / observed state. The per-order state machine is
    -- defined in §6.3 of the discovery doc; allowed transitions
    -- enforced by apply_order_event (§6.4).
    status                        TEXT    NOT NULL,
    filled_qty                    REAL    NOT NULL DEFAULT 0.0,
    avg_fill_price                REAL,

    -- Timestamps with distinct semantics.
    -- created_at: row insert time (before broker submit).
    -- submitted_at: broker submit return time (NULL during pending).
    -- terminal_at: moved to a terminal status (filled/canceled/rejected).
    created_at                    TEXT    NOT NULL,
    submitted_at                  TEXT,
    terminal_at                   TEXT,

    -- Idempotency anchor — broker's last-observed updated_at echoed
    -- onto the row. Combined with the state-machine ordering in §6.4,
    -- this enforces exactly-once application without depending on a
    -- fabricated event_sequence (Alpaca doesn't expose one).
    last_observed_broker_updated_at TEXT,
    last_observed_at              TEXT    NOT NULL,

    FOREIGN KEY(position_uid) REFERENCES position_lifecycle(position_uid)
);
"""


# Unique constraints. Non-unique indexes don't enforce exactly-once
# (PR #59 review-2 P1.3 finding). order_id is NULL while pending;
# the partial unique index permits multiple NULLs while rejecting
# duplicate non-NULL ids — SQLite supports this natively.
_UNIQ_LIFECYCLE_ORDERS_ORDER_ID_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_lifecycle_orders_order_id "
    "ON position_lifecycle_orders(order_id) WHERE order_id IS NOT NULL"
)
_UNIQ_LIFECYCLE_ORDERS_CLIENT_ORDER_ID_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_lifecycle_orders_client_order_id "
    "ON position_lifecycle_orders(client_order_id)"
)

# Per-order entry_primary uniqueness within a position. Belt-and-
# suspenders alongside the position-level lock below (which is the
# actual cross-position duplicate-entry guard per PR #59 review-6 P1).
_UNIQ_ONE_ENTRY_PRIMARY_PER_POSITION_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_one_entry_primary_per_position "
    "ON position_lifecycle_orders(position_uid) "
    "WHERE role = 'entry_primary'"
)

# At most one non-terminal close-side order per position. Durable
# analog of `_spreads_pending_close` and `_has_pending_close_order()`
# (PR #59 review-7 P1). Stop-side roles (protective_stop,
# replacement_stop) are intentionally excluded — replacement_stop is
# an intentional second-stop pattern (PR #47 GTC promotion) and
# protective_stop is OTO-paired with the entry, not a competing close.
_UNIQ_ONE_ACTIVE_CLOSE_PER_POSITION_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_one_active_close_per_position "
    "ON position_lifecycle_orders(position_uid) "
    "WHERE role IN ('exit', 'partial_close') "
    "AND status IN ('pending', 'working', 'partially_filled', 'unknown')"
)

# Lookup indexes.
_IDX_LIFECYCLE_ORDERS_POSITION_UID_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_lifecycle_orders_position_uid "
    "ON position_lifecycle_orders(position_uid)"
)
_IDX_LIFECYCLE_ORDERS_STATUS_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_lifecycle_orders_status "
    "ON position_lifecycle_orders(status)"
)
_IDX_LIFECYCLE_ORDERS_PARENT_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_lifecycle_orders_parent "
    "ON position_lifecycle_orders(parent_order_id)"
)
_IDX_LIFECYCLE_ORDERS_REPLACES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_lifecycle_orders_replaces "
    "ON position_lifecycle_orders(replaces_order_id)"
)


# Position-level uniqueness added to the existing `position_lifecycle`
# table (no column change — index only). PR #59 review-6 P1 + R8-3 +
# R9-P1c: includes 'error' so an errored position retains the
# owner_key lock until the operator explicitly resolves it. Spreads
# have UUID owner_keys (always unique), so multiple spreads on the
# same underlying don't collide. Equity / single-leg options get
# one non-terminal position per owner_key.
_UNIQ_ONE_ACTIVE_POSITION_PER_OWNER_KEY_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_one_active_position_per_owner_key "
    "ON position_lifecycle(owner_key) "
    "WHERE status IN ('pending', 'open', 'partially_filled', 'error')"
)


# Tuple consumed by reporting.logger.TradeLogger._ensure_db. All
# statements are idempotent (CREATE TABLE IF NOT EXISTS, CREATE INDEX
# IF NOT EXISTS) so the bootstrap path can re-run safely without
# raising even when nothing needs to change.
_CREATE_POSITION_LIFECYCLE_ORDERS_INDEXES_SQL: tuple[str, ...] = (
    _UNIQ_LIFECYCLE_ORDERS_ORDER_ID_SQL,
    _UNIQ_LIFECYCLE_ORDERS_CLIENT_ORDER_ID_SQL,
    _UNIQ_ONE_ENTRY_PRIMARY_PER_POSITION_SQL,
    _UNIQ_ONE_ACTIVE_CLOSE_PER_POSITION_SQL,
    _IDX_LIFECYCLE_ORDERS_POSITION_UID_SQL,
    _IDX_LIFECYCLE_ORDERS_STATUS_SQL,
    _IDX_LIFECYCLE_ORDERS_PARENT_SQL,
    _IDX_LIFECYCLE_ORDERS_REPLACES_SQL,
)


# Per-role enum values, mirrored from the discovery doc §6.1 / §6.2
# for use by the store API and writer paths in later commits.
VALID_ORDER_ROLES = frozenset({
    "entry_primary",
    "entry_residual",
    "protective_stop",
    "replacement_stop",
    "exit",
    "partial_close",
})


# Per-order state machine values, mirrored from §6.3. Used by
# apply_order_event and the position-status rollup query.
VALID_ORDER_STATUSES = frozenset({
    "pending",
    "working",
    "partially_filled",
    "filled",
    "canceled",
    "rejected",
    "unknown",
})

TERMINAL_ORDER_STATUSES = frozenset({"filled", "canceled", "rejected"})
NON_TERMINAL_ORDER_STATUSES = (
    VALID_ORDER_STATUSES - TERMINAL_ORDER_STATUSES
)


# State-machine rank used by apply_order_event's strict-newer
# discriminator (§6.4). Lexicographic compare with filled_qty
# determines whether an incoming event advances the row.
STATE_MACHINE_RANK: dict[str, int] = {
    "pending": 0,
    "working": 1,
    "unknown": 1,
    "partially_filled": 2,
    "filled": 3,
    "canceled": 3,
    "rejected": 3,
}


# Close-side and stop-side role groupings used by the position-status
# CASE in §6.6.1. Close-side roles can change `current_qty`; stop-side
# roles are async broker-state cleanup that ALSO blocks `closed`
# (R12-P1 supersedes R8-1 — a working stop can fire after `closed`
# releases the lock and oversell a fresh entry).
CLOSE_SIDE_ROLES = frozenset({"exit", "partial_close"})
STOP_SIDE_ROLES = frozenset({"protective_stop", "replacement_stop"})
SELL_SIDE_ROLES = CLOSE_SIDE_ROLES | STOP_SIDE_ROLES
ENTRY_SIDE_ROLES = frozenset({"entry_primary", "entry_residual"})


# ── Migration preflight (discovery doc §12.2) ──


# Status set used by the position-level uniq_one_active_position_per_owner_key
# index. Preflight detection must mirror this set EXACTLY — if the
# preflight check uses a different status filter than the index's
# WHERE clause, duplicates that the index would reject can slip
# through preflight and break CREATE UNIQUE INDEX at runtime
# (PR #59 review-9 P1c).
_OWNER_KEY_LOCK_STATUSES: tuple[str, ...] = (
    "pending",
    "open",
    "partially_filled",
    "error",
)


@dataclass(frozen=True)
class OwnerKeyDuplicate:
    """One duplicate owner_key cluster detected by preflight."""

    owner_key: str
    count: int
    position_uids: tuple[str, ...] = field(default_factory=tuple)


class MigrationDuplicatesFound(RuntimeError):
    """Raised when preflight detects pre-existing duplicate rows that
    would block a new UNIQUE INDEX from being created.

    Per discovery doc §12.2 (PR #59 review-7 P1b): partial-migration
    mode is NOT safe — foundation writer code assumes the unique
    indexes exist. Continuing without them would either create more
    duplicates or produce silently wrong rollups. The correct
    behavior is to abort startup, force operator remediation
    (`scripts/migrate_dedupe_trades.py` per §12.2), and retry on
    the next startup once the operator has applied a decisions file.

    The exception carries structured detail so the alert backend
    and the operator dashboard can surface the affected rows.
    """

    def __init__(
        self,
        *,
        owner_key_duplicates: tuple[OwnerKeyDuplicate, ...] = (),
        message: str | None = None,
    ) -> None:
        self.owner_key_duplicates = owner_key_duplicates
        if message is None:
            parts: list[str] = []
            if owner_key_duplicates:
                parts.append(
                    f"{len(owner_key_duplicates)} position_lifecycle.owner_key "
                    f"duplicates"
                )
                for dup in owner_key_duplicates[:5]:
                    parts.append(
                        f"  - owner_key={dup.owner_key!r} "
                        f"count={dup.count} "
                        f"uids={dup.position_uids}"
                    )
                if len(owner_key_duplicates) > 5:
                    parts.append(
                        f"  ... and {len(owner_key_duplicates) - 5} more"
                    )
            parts.append(
                "Pre-existing duplicates block the foundation PR's "
                "uniq_one_active_position_per_owner_key partial unique "
                "index. Run scripts/migrate_dedupe_trades.py (detection / "
                "review / apply modes per discovery doc §12.2) and retry."
            )
            message = "\n".join(parts)
        super().__init__(message)


def detect_owner_key_duplicates(
    conn: sqlite3.Connection,
) -> tuple[OwnerKeyDuplicate, ...]:
    """Detect duplicate owner_key clusters that would block the new
    uniq_one_active_position_per_owner_key partial unique index.

    The status set MUST match the index's WHERE clause exactly
    (PR #59 review-9 P1c). Mismatched filters let duplicates slip
    through preflight and surface as the much-less-helpful generic
    SQLite UNIQUE constraint violation at index creation time.

    Returns an empty tuple when the database is clean. Callers can
    treat a non-empty result as a fatal precondition and raise
    ``MigrationDuplicatesFound``.
    """
    status_placeholders = ", ".join("?" for _ in _OWNER_KEY_LOCK_STATUSES)
    rows = conn.execute(
        f"""
        SELECT owner_key, COUNT(*) AS n,
               GROUP_CONCAT(position_uid, ',') AS uids
        FROM position_lifecycle
        WHERE status IN ({status_placeholders})
        GROUP BY owner_key
        HAVING COUNT(*) > 1
        ORDER BY owner_key
        """,
        _OWNER_KEY_LOCK_STATUSES,
    ).fetchall()
    duplicates: list[OwnerKeyDuplicate] = []
    for owner_key, count, uids_csv in rows:
        uids_tuple: tuple[str, ...] = (
            tuple(uids_csv.split(",")) if uids_csv else ()
        )
        duplicates.append(
            OwnerKeyDuplicate(
                owner_key=owner_key,
                count=int(count),
                position_uids=uids_tuple,
            )
        )
    return tuple(duplicates)


def run_preflight_or_raise(conn: sqlite3.Connection) -> None:
    """Run all migration preflight checks before applying foundation
    PR's new UNIQUE INDEXes. Raises ``MigrationDuplicatesFound`` on
    any conflict. Safe to call on every startup; runs fast (indexed
    GROUP BY queries) when the database is clean.

    Scope at commit 2: checks duplicates on the position_lifecycle
    owner_key dimension. Commit 5 (trades schema migration) will
    expand this to also check the trades.order_id /
    position_type='single_leg' dimension.
    """
    duplicates = detect_owner_key_duplicates(conn)
    if duplicates:
        raise MigrationDuplicatesFound(owner_key_duplicates=duplicates)


# ── Store API (discovery doc §6.2 / §6.3) ──


def _utc_now_iso() -> str:
    """UTC timestamp in ISO 8601, second precision. Same shape as
    engine.lifecycle._utc_now_iso so the two stores produce
    comparable timestamps."""
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class PositionLifecycleOrderRow:
    """One row of ``position_lifecycle_orders``.

    Frozen so callers can't accidentally mutate a snapshot. Re-query
    the store to observe updates (same discipline as
    ``PositionLifecycleRow`` in engine.lifecycle).
    """

    id: int
    position_uid: str
    role: str

    order_id: str | None
    client_order_id: str

    order_type: str
    order_class: str
    time_in_force: str
    side: str
    intended_qty: float
    intended_stop_price: float | None
    intended_trigger_price: float | None
    intended_limit_price: float | None
    intended_take_profit_price: float | None

    parent_order_id: str | None
    replaces_order_id: str | None

    origin_kind: str
    operator_command_uid: str | None

    slippage_benchmark_price: float | None
    slippage_benchmark_kind: str | None
    slippage_benchmark_timestamp: str | None
    slippage_measurement_quality: str | None

    status: str
    filled_qty: float
    avg_fill_price: float | None

    created_at: str
    submitted_at: str | None
    terminal_at: str | None

    last_observed_broker_updated_at: str | None
    last_observed_at: str


_SELECT_LIFECYCLE_ORDER_COLUMNS = (
    "SELECT id, position_uid, role, order_id, client_order_id, "
    "order_type, order_class, time_in_force, side, intended_qty, "
    "intended_stop_price, intended_trigger_price, intended_limit_price, "
    "intended_take_profit_price, parent_order_id, replaces_order_id, "
    "origin_kind, operator_command_uid, "
    "slippage_benchmark_price, slippage_benchmark_kind, "
    "slippage_benchmark_timestamp, slippage_measurement_quality, "
    "status, filled_qty, avg_fill_price, "
    "created_at, submitted_at, terminal_at, "
    "last_observed_broker_updated_at, last_observed_at "
    "FROM position_lifecycle_orders"
)


def _row_from_tuple(row: tuple) -> PositionLifecycleOrderRow:
    return PositionLifecycleOrderRow(
        id=row[0],
        position_uid=row[1],
        role=row[2],
        order_id=row[3],
        client_order_id=row[4],
        order_type=row[5],
        order_class=row[6],
        time_in_force=row[7],
        side=row[8],
        intended_qty=row[9],
        intended_stop_price=row[10],
        intended_trigger_price=row[11],
        intended_limit_price=row[12],
        intended_take_profit_price=row[13],
        parent_order_id=row[14],
        replaces_order_id=row[15],
        origin_kind=row[16],
        operator_command_uid=row[17],
        slippage_benchmark_price=row[18],
        slippage_benchmark_kind=row[19],
        slippage_benchmark_timestamp=row[20],
        slippage_measurement_quality=row[21],
        status=row[22],
        filled_qty=row[23],
        avg_fill_price=row[24],
        created_at=row[25],
        submitted_at=row[26],
        terminal_at=row[27],
        last_observed_broker_updated_at=row[28],
        last_observed_at=row[29],
    )


class PositionLifecycleOrdersStore:
    """Read/write API for ``position_lifecycle_orders``.

    Discovery doc §6.2 / §6.3 specify the schema and state machine.
    Two key properties this store enforces at the API boundary:

    - ``insert_pending(...)`` creates a row at ``status='pending'``
      with order_id NULL and submitted_at NULL. The broker submit
      hasn't returned yet; only client_order_id is known. ``apply_
      order_event`` (commit 4) advances the row from pending onward
      as broker reality arrives via stream / cycle / startup paths.

    - Reads always return ``PositionLifecycleOrderRow`` frozen
      snapshots, never mutable references. Callers cannot
      accidentally drift state by holding stale references.

    The store does NOT execute DDL. The schema is created by
    ``reporting.logger.TradeLogger._ensure_db`` through the same
    migration path as ``position_lifecycle`` (engine.lifecycle).

    All writes commit immediately so a crash between operations
    cannot lose a lifecycle event. The caller is responsible for
    wrapping calls in try/except so a DB I/O failure does not
    propagate into the trading loop — same discipline as
    ``PositionLifecycleStore`` and ``strategies.health.lifecycle``.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── Writes ────────────────────────────────────────────────────────

    def insert_pending(
        self,
        *,
        position_uid: str,
        role: str,
        client_order_id: str,
        order_type: str,
        order_class: str,
        time_in_force: str,
        side: str,
        intended_qty: float,
        intended_stop_price: float | None = None,
        intended_trigger_price: float | None = None,
        intended_limit_price: float | None = None,
        intended_take_profit_price: float | None = None,
        parent_order_id: str | None = None,
        replaces_order_id: str | None = None,
        origin_kind: str = "bot",
        operator_command_uid: str | None = None,
        slippage_benchmark_price: float | None = None,
        slippage_benchmark_kind: str | None = None,
        slippage_benchmark_timestamp: str | None = None,
        slippage_measurement_quality: str | None = None,
    ) -> int:
        """Insert a per-order row at ``status='pending'`` BEFORE the
        broker submit goes out. Returns the autoincrement id.

        Caller must pass a unique ``client_order_id`` — the broker
        will use it as the dedup key, and the foundation enforces
        the same locally via ``uniq_lifecycle_orders_client_order_id``.

        ``order_id`` is NULL on this row. The broker assigns it on
        submit return; ``apply_order_event`` populates it on the
        first observed event.

        Raises:
          - ``ValueError`` on invalid role / status / origin_kind
          - ``sqlite3.IntegrityError`` on duplicate client_order_id,
            duplicate non-terminal entry_primary per position_uid,
            duplicate non-terminal close per position_uid, or
            FK violation if position_uid doesn't exist in
            position_lifecycle.
        """
        _validate_role(role)
        _validate_origin_kind(origin_kind)
        if not client_order_id:
            raise ValueError("client_order_id must not be empty")
        if intended_qty <= 0:
            raise ValueError(
                f"intended_qty must be positive; got {intended_qty}"
            )

        now = _utc_now_iso()
        cursor = self._conn.execute(
            """
            INSERT INTO position_lifecycle_orders (
                position_uid, role,
                order_id, client_order_id,
                order_type, order_class, time_in_force, side,
                intended_qty,
                intended_stop_price, intended_trigger_price,
                intended_limit_price, intended_take_profit_price,
                parent_order_id, replaces_order_id,
                origin_kind, operator_command_uid,
                slippage_benchmark_price, slippage_benchmark_kind,
                slippage_benchmark_timestamp, slippage_measurement_quality,
                status, filled_qty, avg_fill_price,
                created_at, submitted_at, terminal_at,
                last_observed_broker_updated_at, last_observed_at
            ) VALUES (
                ?, ?,
                NULL, ?,
                ?, ?, ?, ?,
                ?,
                ?, ?, ?, ?,
                ?, ?,
                ?, ?,
                ?, ?, ?, ?,
                'pending', 0.0, NULL,
                ?, NULL, NULL,
                NULL, ?
            )
            """,
            (
                position_uid, role,
                client_order_id,
                order_type, order_class, time_in_force, side,
                float(intended_qty),
                intended_stop_price, intended_trigger_price,
                intended_limit_price, intended_take_profit_price,
                parent_order_id, replaces_order_id,
                origin_kind, operator_command_uid,
                slippage_benchmark_price, slippage_benchmark_kind,
                slippage_benchmark_timestamp, slippage_measurement_quality,
                now, now,
            ),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def attach_broker_order_id(
        self,
        *,
        client_order_id: str,
        order_id: str,
        submitted_at: str | None = None,
    ) -> None:
        """Populate the broker-assigned ``order_id`` on the row
        identified by ``client_order_id``, after the broker submit
        returns. Also stamps ``submitted_at`` (defaults to now UTC).

        This is the only path that should populate ``order_id``
        post-pending — once set, ``order_id`` is immutable. The
        partial unique index on ``order_id`` enforces no other row
        can claim the same id.

        Raises ``ValueError`` if the row is not at status='pending'
        or if order_id is already set.
        """
        now = submitted_at or _utc_now_iso()
        existing = self._conn.execute(
            "SELECT status, order_id FROM position_lifecycle_orders "
            "WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if existing is None:
            raise ValueError(
                f"unknown client_order_id: {client_order_id!r}"
            )
        current_status, current_order_id = existing
        if current_status != "pending":
            raise ValueError(
                f"row at client_order_id={client_order_id!r} is "
                f"status={current_status!r}; cannot attach order_id "
                f"after pending"
            )
        if current_order_id is not None:
            raise ValueError(
                f"row at client_order_id={client_order_id!r} already "
                f"has order_id={current_order_id!r}"
            )
        self._conn.execute(
            "UPDATE position_lifecycle_orders "
            "SET order_id = ?, submitted_at = ?, last_observed_at = ? "
            "WHERE client_order_id = ?",
            (order_id, now, now, client_order_id),
        )
        self._conn.commit()

    # ── Reads ─────────────────────────────────────────────────────────

    def get_by_id(self, row_id: int) -> PositionLifecycleOrderRow | None:
        row = self._conn.execute(
            _SELECT_LIFECYCLE_ORDER_COLUMNS + " WHERE id = ?",
            (row_id,),
        ).fetchone()
        return None if row is None else _row_from_tuple(row)

    def get_by_order_id(
        self, order_id: str
    ) -> PositionLifecycleOrderRow | None:
        """Lookup by broker-assigned order_id. Returns None if the row
        is still pending (order_id NULL) or doesn't exist."""
        row = self._conn.execute(
            _SELECT_LIFECYCLE_ORDER_COLUMNS + " WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        return None if row is None else _row_from_tuple(row)

    def get_by_client_order_id(
        self, client_order_id: str
    ) -> PositionLifecycleOrderRow | None:
        """Lookup by bot-generated client_order_id. Works whether or
        not the broker has yet returned an order_id."""
        row = self._conn.execute(
            _SELECT_LIFECYCLE_ORDER_COLUMNS + " WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        return None if row is None else _row_from_tuple(row)

    def get_all_for_position(
        self, position_uid: str
    ) -> list[PositionLifecycleOrderRow]:
        """All per-order rows for a position, terminal or not,
        ordered by row id (insertion order)."""
        rows = self._conn.execute(
            _SELECT_LIFECYCLE_ORDER_COLUMNS
            + " WHERE position_uid = ? ORDER BY id ASC",
            (position_uid,),
        ).fetchall()
        return [_row_from_tuple(r) for r in rows]

    def get_non_terminal_for_position(
        self, position_uid: str
    ) -> list[PositionLifecycleOrderRow]:
        """Per-order rows for a position whose status is in the
        non-terminal set (pending / working / partially_filled /
        unknown). Used by reconciliation paths and by the position-
        status rollup in §6.6.1."""
        placeholders = ", ".join("?" for _ in NON_TERMINAL_ORDER_STATUSES)
        rows = self._conn.execute(
            _SELECT_LIFECYCLE_ORDER_COLUMNS
            + f" WHERE position_uid = ? AND status IN ({placeholders}) "
            "ORDER BY id ASC",
            (position_uid, *sorted(NON_TERMINAL_ORDER_STATUSES)),
        ).fetchall()
        return [_row_from_tuple(r) for r in rows]

    def get_non_terminal_by_role(
        self, position_uid: str, roles: Iterable[str]
    ) -> list[PositionLifecycleOrderRow]:
        """Non-terminal per-order rows for a position whose role is
        in the given set. Used to query "any active sell-side order"
        for the position-status logic in §6.6.1."""
        roles_tuple = tuple(roles)
        if not roles_tuple:
            return []
        for role in roles_tuple:
            _validate_role(role)
        role_placeholders = ", ".join("?" for _ in roles_tuple)
        status_placeholders = ", ".join("?" for _ in NON_TERMINAL_ORDER_STATUSES)
        rows = self._conn.execute(
            _SELECT_LIFECYCLE_ORDER_COLUMNS
            + f" WHERE position_uid = ? "
            f"AND role IN ({role_placeholders}) "
            f"AND status IN ({status_placeholders}) "
            "ORDER BY id ASC",
            (
                position_uid,
                *roles_tuple,
                *sorted(NON_TERMINAL_ORDER_STATUSES),
            ),
        ).fetchall()
        return [_row_from_tuple(r) for r in rows]


# ── Validators ──


def _validate_role(role: str) -> None:
    if role not in VALID_ORDER_ROLES:
        raise ValueError(
            f"role must be one of {sorted(VALID_ORDER_ROLES)}; "
            f"got {role!r}"
        )


def _validate_origin_kind(origin_kind: str) -> None:
    if origin_kind not in ("bot", "operator"):
        raise ValueError(
            f"origin_kind must be 'bot' or 'operator'; got {origin_kind!r}"
        )
