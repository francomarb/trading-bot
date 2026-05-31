"""
Position lifecycle store: immutable per-lifecycle identity for every
position opened by the bot.

Why this module exists
----------------------
Today the bot tracks positions by `position_id` — which for equities
equals the symbol and for spreads is a per-instance UUID generated in
`engine.positions`. That works for live broker aggregation but it is
**not** an immutable lifecycle identifier:

- A symbol closed today and reopened next week produces ambiguous
  history when the trade log is queried by symbol+time-window.
- The operator CLI (see `docs/operator_controls_proposal.md`) needs to
  target one exact opened position for read-only display today and for
  destructive commands (reduce/close) later.
- Multiple downstream subsystems will benefit from per-lifecycle
  identity once it exists (see proposal §17): health monitor partial-
  exit accounting, sleeve allocator reserve/release, backtest
  reconciliation, alerts, dashboard.

This module is the foundation. It introduces:

- `position_uid` — a globally unique, immutable identifier
  (`pos_<uuid4_hex>`) generated *before* the order leaves the bot.
- `position_lifecycle` — one row per opened position lifecycle, with
  status tracking (`pending` → `open`/`partially_filled` →
  `closed`/`canceled`).
- `position_lifecycle_legs` — child rows for spread legs.

Phase A scope (operator controls v1)
------------------------------------
The operator CLI is the first and only customer in Phase A. The store
is designed so other subsystems can adopt `position_uid` independently,
each in its own future PR, per proposal §17. **No other subsystem reads
this table in Phase A.**

Phase A is purely additive: no existing call signature, schema column,
or behavior path changes. Lifecycle writes are best-effort and wrapped
by the caller in try/except so persistence failure can never abort an
order or a cycle (matches the same discipline used by
`strategies.health.lifecycle`).

Reusability contract (per implementation plan)
----------------------------------------------
- This module imports only from the standard library. It does not
  import from `engine/trader.py`, `risk/`, `strategies/`, or
  `execution/`. Any subsystem can `from engine.lifecycle import ...`
  without circular-import risk.
- `new_position_uid()` is the single producer of position UIDs. Every
  call site uses it; no inline `uuid.uuid4()` for this purpose
  elsewhere.
- `client_order_id_for()` is the single producer of the operator-aware
  `client_order_id` format used by Phase C destructive commands. Phase
  A entry paths keep their existing `client_order_id` format
  unchanged — this helper exists for Phase C to reuse.
- The store exposes typed query helpers (`get_open`,
  `get_by_position_uid`, etc.) rather than expecting callers to write
  raw SQL.

DDL ownership
-------------
The two CREATE TABLE statements live in this module but are executed
by `reporting.logger.TradeLogger._ensure_db()` so all schema
initialisation happens through the existing single migration path. The
store itself never executes DDL — it expects the tables to already
exist when `PositionLifecycleStore(conn)` is constructed.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable


LIFECYCLE_SCHEMA_VERSION = 1


# ── DDL ───────────────────────────────────────────────────────────────


_CREATE_POSITION_LIFECYCLE_SQL = """
CREATE TABLE IF NOT EXISTS position_lifecycle (
    position_uid            TEXT PRIMARY KEY,
    schema_version          INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT    NOT NULL,
    closed_at               TEXT,
    symbol                  TEXT    NOT NULL,
    owner_key               TEXT    NOT NULL,
    strategy                TEXT    NOT NULL,
    position_type           TEXT    NOT NULL,
    status                  TEXT    NOT NULL,
    entry_qty               REAL,
    current_qty             REAL,
    avg_entry_price         REAL,
    net_realized_pnl        REAL    NOT NULL DEFAULT 0.0,
    entry_order_id          TEXT,
    entry_client_order_id   TEXT,
    first_fill_at           TEXT,
    last_fill_at            TEXT,
    metadata_json           TEXT
);
"""


_CREATE_POSITION_LIFECYCLE_LEGS_SQL = """
CREATE TABLE IF NOT EXISTS position_lifecycle_legs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_uid        TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    qty                 REAL    NOT NULL,
    avg_entry_price     REAL,
    FOREIGN KEY(position_uid) REFERENCES position_lifecycle(position_uid)
);
"""


_CREATE_POSITION_LIFECYCLE_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_position_lifecycle_owner_key "
    "ON position_lifecycle(owner_key)",
    "CREATE INDEX IF NOT EXISTS idx_position_lifecycle_status "
    "ON position_lifecycle(status)",
    "CREATE INDEX IF NOT EXISTS idx_position_lifecycle_strategy "
    "ON position_lifecycle(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_position_lifecycle_legs_uid "
    "ON position_lifecycle_legs(position_uid)",
)


# Valid status values. Enforced at the API boundary so invalid statuses
# never reach the DB.
#
# Lifecycle transitions (proposal §8.1):
#   pending           → open | partially_filled | canceled
#   partially_filled  → open | closed   (partial cancel STAYS open)
#   open              → closed
#   closed            → (terminal)
#   canceled          → (terminal — zero-fill cancellation only)
#   external_closed   → (terminal — broker closed outside bot control)
#   error             → (terminal — needs operator review)
VALID_STATUSES = frozenset({
    "pending",
    "open",
    "partially_filled",
    "closed",
    "canceled",
    "external_closed",
    "error",
})

VALID_POSITION_TYPES = frozenset({"single_leg", "spread"})


# ── ID generators ─────────────────────────────────────────────────────


def new_position_uid() -> str:
    """The single producer of position UIDs.

    Returns ``pos_<32-hex>`` (38 chars total). uuid4 is used so the ID
    is safe offline, requires no broker round-trip, and does not derive
    from mutable position metadata. The `pos_` prefix makes the
    namespace obvious in logs, alerts, and operator-CLI output.

    Reused by every entry path in `execution.broker` and
    `execution.options_executor`. Do NOT call `uuid.uuid4()` inline
    elsewhere for this purpose — keep generation centralised so the
    format can evolve.
    """
    return f"pos_{uuid.uuid4().hex}"


def client_order_id_for(
    strategy_name: str,
    position_uid: str,
    *,
    suffix: str | None = None,
) -> str:
    """The single producer of the operator-aware ``client_order_id``
    format.

    Format: ``<strategy>-<uid_short>[-<suffix>]`` where ``uid_short`` is
    the first 10 hex chars of the ``position_uid``. Kept short to
    stay well within Alpaca's documented client_order_id length budget
    (the full mapping is recoverable from the
    ``position_lifecycle.entry_client_order_id`` column).

    **Phase A note:** entry paths in `execution.broker` and
    `execution.options_executor` keep their EXISTING client_order_id
    format unchanged in Phase A. This helper exists for Phase C
    operator-issued orders (reduce/close) to reuse.

    Suffix conventions for Phase C:
      - ``"reduce"`` — operator-issued partial close
      - ``"close"``  — operator-issued full close
      - ``"cancel"`` — operator-issued order cancellation
    """
    if not strategy_name:
        raise ValueError("strategy_name must not be empty")
    if not position_uid or not position_uid.startswith("pos_"):
        raise ValueError(
            f"position_uid must be a 'pos_<hex>' string; got {position_uid!r}"
        )
    uid_short = position_uid.removeprefix("pos_")[:10]
    base = f"{strategy_name}-{uid_short}"
    if suffix:
        return f"{base}-{suffix}"
    return base


# ── Dataclasses (consumer-facing, no engine dependencies) ─────────────


@dataclass(frozen=True)
class PositionLifecycleLeg:
    """One leg of a lifecycle position. For single-leg equities/options,
    exactly one leg exists. For spreads, two or more."""

    position_uid: str
    symbol: str
    side: str
    qty: float
    avg_entry_price: float | None = None


@dataclass(frozen=True)
class PositionLifecycleRow:
    """One row of ``position_lifecycle`` plus its legs.

    Frozen so callers can't accidentally mutate a snapshot. Re-query
    the store to observe updates.
    """

    position_uid: str
    created_at: str
    closed_at: str | None
    symbol: str
    owner_key: str
    strategy: str
    position_type: str
    status: str
    entry_qty: float | None
    current_qty: float | None
    avg_entry_price: float | None
    net_realized_pnl: float
    entry_order_id: str | None
    entry_client_order_id: str | None
    first_fill_at: str | None
    last_fill_at: str | None
    metadata: dict = field(default_factory=dict)
    legs: tuple[PositionLifecycleLeg, ...] = ()


# ── Store ─────────────────────────────────────────────────────────────


class PositionLifecycleStore:
    """Read/write API for the ``position_lifecycle`` and
    ``position_lifecycle_legs`` tables.

    The store does NOT own the DB connection — it expects a connection
    where the two tables already exist (created by
    `reporting.logger.TradeLogger._ensure_db()`). This mirrors the
    pattern used by `strategies.health.lifecycle` and keeps schema
    initialisation in one place.

    All writes call ``conn.commit()`` directly so a crash between
    operations cannot lose a lifecycle event. The caller is
    responsible for wrapping calls in ``try/except logger.warning``
    so a DB I/O failure does not propagate into the trading loop
    (same discipline as health/lifecycle).
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ── Writes ────────────────────────────────────────────────────────

    def create_pending(
        self,
        *,
        position_uid: str,
        symbol: str,
        owner_key: str,
        strategy: str,
        position_type: str,
        entry_qty: float | None,
        entry_client_order_id: str | None = None,
        entry_order_id: str | None = None,
        legs: Iterable[PositionLifecycleLeg] = (),
        metadata: dict | None = None,
    ) -> None:
        """Insert a new lifecycle row in ``pending`` status before the
        broker submission goes out.

        Generates the row with ``created_at`` = now (UTC).

        Raises:
          - ValueError on invalid position_type
          - sqlite3.IntegrityError if position_uid already exists
            (UIDs must be unique — UNIQUE/PRIMARY KEY enforced).
        """
        _validate_position_uid(position_uid)
        _validate_position_type(position_type)
        if not symbol:
            raise ValueError("symbol must not be empty")
        if not owner_key:
            raise ValueError("owner_key must not be empty")
        if not strategy:
            raise ValueError("strategy must not be empty")

        now = _utc_now_iso()
        meta_json = json.dumps(metadata) if metadata else None

        self._conn.execute(
            """
            INSERT INTO position_lifecycle (
                schema_version, position_uid, created_at, closed_at,
                symbol, owner_key, strategy, position_type, status,
                entry_qty, current_qty, avg_entry_price,
                net_realized_pnl,
                entry_order_id, entry_client_order_id,
                first_fill_at, last_fill_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                LIFECYCLE_SCHEMA_VERSION,
                position_uid,
                now,
                None,
                symbol,
                owner_key,
                strategy,
                position_type,
                "pending",
                entry_qty,
                0.0 if entry_qty is not None else None,
                None,
                0.0,
                entry_order_id,
                entry_client_order_id,
                None,
                None,
                meta_json,
            ),
        )
        for leg in legs:
            if leg.position_uid != position_uid:
                raise ValueError(
                    f"leg.position_uid ({leg.position_uid!r}) does not "
                    f"match lifecycle position_uid ({position_uid!r})"
                )
            self._insert_leg(leg)
        self._conn.commit()

    def mark_open(
        self,
        *,
        position_uid: str,
        avg_entry_price: float,
        current_qty: float,
        first_fill_at: str | None = None,
        last_fill_at: str | None = None,
    ) -> None:
        """Transition ``pending``/``partially_filled`` → ``open`` after
        a fill confirms the full intended quantity.

        ``first_fill_at`` is set only if the row does not already have
        one (idempotent for subsequent fills). ``last_fill_at`` is
        always updated.
        """
        _validate_position_uid(position_uid)
        now = _utc_now_iso()
        first = first_fill_at or now
        last = last_fill_at or now
        self._conn.execute(
            """
            UPDATE position_lifecycle
            SET status           = 'open',
                avg_entry_price  = ?,
                current_qty      = ?,
                first_fill_at    = COALESCE(first_fill_at, ?),
                last_fill_at     = ?
            WHERE position_uid = ?
            """,
            (avg_entry_price, current_qty, first, last, position_uid),
        )
        self._conn.commit()

    def mark_partially_filled(
        self,
        *,
        position_uid: str,
        avg_entry_price: float,
        current_qty: float,
        first_fill_at: str | None = None,
        last_fill_at: str | None = None,
    ) -> None:
        """Transition ``pending`` → ``partially_filled`` while waiting
        for the rest of the order to fill.

        Note: per proposal §8.1, an entry that fills partially and is
        then *cancelled* must remain at ``open`` (or stay
        ``partially_filled`` if the engine treats it that way) at the
        filled quantity. It must NEVER transition to ``canceled``. The
        ``mark_canceled`` method enforces this — call it only when
        zero fills occurred.
        """
        _validate_position_uid(position_uid)
        now = _utc_now_iso()
        first = first_fill_at or now
        last = last_fill_at or now
        self._conn.execute(
            """
            UPDATE position_lifecycle
            SET status           = 'partially_filled',
                avg_entry_price  = ?,
                current_qty      = ?,
                first_fill_at    = COALESCE(first_fill_at, ?),
                last_fill_at     = ?
            WHERE position_uid = ?
            """,
            (avg_entry_price, current_qty, first, last, position_uid),
        )
        self._conn.commit()

    def mark_residual(
        self,
        *,
        position_uid: str,
        current_qty: float,
        last_fill_at: str | None = None,
    ) -> None:
        """Update ``current_qty`` after a partial close.

        Does NOT change ``status`` — a partially-closed row stays in
        whatever non-terminal status it was already in (``open`` or
        ``partially_filled``). For Phase A the operator CLI keys off
        ``status`` for the open/closed split and off ``current_qty``
        for the displayed size; that's enough to surface "this
        position is still open but smaller than entry."

        Full partial-close lifecycle accounting (per-event realized R,
        ``net_realized_pnl`` accumulation, a dedicated
        ``partially_closed`` state if needed) is bundled into Phase C
        per the operator-controls implementation plan. This helper is
        the minimum needed so the operator CLI does not show stale
        entry quantity after a partial-fill exit.

        Raises ValueError when ``current_qty <= 0`` — those cases must
        use ``mark_closed`` instead so the row reaches a terminal
        status. The engine's reduce helper handles that branch before
        calling here.
        """
        _validate_position_uid(position_uid)
        if current_qty <= 0:
            raise ValueError(
                f"current_qty must be > 0 for residual update; got "
                f"{current_qty}. Use mark_closed for a fully exited "
                f"position."
            )
        last = last_fill_at or _utc_now_iso()
        self._conn.execute(
            "UPDATE position_lifecycle "
            "SET current_qty = ?, last_fill_at = ? "
            "WHERE position_uid = ?",
            (float(current_qty), last, position_uid),
        )
        self._conn.commit()

    def mark_canceled(self, *, position_uid: str) -> None:
        """Transition ``pending`` → ``canceled`` for an entry that
        cancelled with **zero fills**.

        Raises ValueError if the row has any fills recorded
        (``first_fill_at IS NOT NULL`` or ``current_qty > 0``). Those
        rows must remain ``open`` / ``partially_filled`` at the filled
        quantity per proposal §8.1.
        """
        _validate_position_uid(position_uid)
        row = self._conn.execute(
            "SELECT status, first_fill_at, current_qty "
            "FROM position_lifecycle WHERE position_uid = ?",
            (position_uid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown position_uid: {position_uid!r}")
        _, first_fill_at, current_qty = row
        if first_fill_at is not None or (current_qty or 0.0) > 0.0:
            raise ValueError(
                f"refusing to mark {position_uid!r} canceled — it has "
                f"fills (first_fill_at={first_fill_at!r}, "
                f"current_qty={current_qty!r}). Per proposal §8.1 a "
                f"partially-filled-then-cancelled entry stays open at "
                f"the filled quantity."
            )
        self._conn.execute(
            "UPDATE position_lifecycle "
            "SET status = 'canceled', closed_at = ? "
            "WHERE position_uid = ?",
            (_utc_now_iso(), position_uid),
        )
        self._conn.commit()

    def mark_closed(
        self,
        *,
        position_uid: str,
        net_realized_pnl: float | None = None,
        external: bool = False,
    ) -> None:
        """Transition open/partially_filled → ``closed`` (or
        ``external_closed`` if the broker closed the position outside
        bot control, e.g. a stop fill the engine didn't initiate).

        ``net_realized_pnl`` is optional — callers may update it
        independently via the trade-log path. If provided, overwrites
        the existing value (not accumulative; the caller is the source
        of truth for the final realized number).
        """
        _validate_position_uid(position_uid)
        status = "external_closed" if external else "closed"
        params: list = [status, _utc_now_iso()]
        sql = (
            "UPDATE position_lifecycle "
            "SET status = ?, closed_at = ?, current_qty = 0.0"
        )
        if net_realized_pnl is not None:
            sql += ", net_realized_pnl = ?"
            params.append(net_realized_pnl)
        sql += " WHERE position_uid = ?"
        params.append(position_uid)
        self._conn.execute(sql, params)
        self._conn.commit()

    def synthesize_for_existing(
        self,
        *,
        symbol: str,
        owner_key: str,
        strategy: str,
        position_type: str,
        current_qty: float,
        avg_entry_price: float | None,
        first_fill_at: str | None = None,
        legs: Iterable[PositionLifecycleLeg] = (),
        backfill_note: str = "synthesized at startup from broker state",
    ) -> str:
        """Idempotent backfill helper: ensure an open lifecycle row
        exists for a broker-open position the bot didn't originate.

        If an open row already exists for ``owner_key``, returns that
        row's ``position_uid`` and does nothing else (idempotent). Else
        creates a new lifecycle row in ``open`` status with a generated
        ``position_uid``, marks ``metadata.synthesized = true``, and
        returns the new uid.

        Used by `engine.trader._backfill_position_lifecycle()` on
        startup so the operator CLI can see and act on positions that
        existed before this code shipped.
        """
        existing = self.get_open_for_owner_key(owner_key)
        if existing is not None:
            return existing.position_uid

        position_uid = new_position_uid()
        now = _utc_now_iso()
        first = first_fill_at or now
        meta = {
            "synthesized": True,
            "note": backfill_note,
            "synthesized_at": now,
        }
        self._conn.execute(
            """
            INSERT INTO position_lifecycle (
                schema_version, position_uid, created_at, closed_at,
                symbol, owner_key, strategy, position_type, status,
                entry_qty, current_qty, avg_entry_price,
                net_realized_pnl,
                entry_order_id, entry_client_order_id,
                first_fill_at, last_fill_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                LIFECYCLE_SCHEMA_VERSION,
                position_uid,
                now,
                None,
                symbol,
                owner_key,
                strategy,
                position_type,
                "open",
                current_qty,
                current_qty,
                avg_entry_price,
                0.0,
                None,
                None,
                first,
                first,
                json.dumps(meta),
            ),
        )
        for leg in legs:
            # Re-key legs to the freshly-generated uid (caller may have
            # constructed them with a placeholder uid).
            self._insert_leg(
                PositionLifecycleLeg(
                    position_uid=position_uid,
                    symbol=leg.symbol,
                    side=leg.side,
                    qty=leg.qty,
                    avg_entry_price=leg.avg_entry_price,
                )
            )
        self._conn.commit()
        return position_uid

    # ── Reads ─────────────────────────────────────────────────────────

    def get_by_position_uid(self, position_uid: str) -> PositionLifecycleRow | None:
        """Return the full row for one position_uid, with legs, or
        None if it doesn't exist."""
        row = self._conn.execute(
            _SELECT_LIFECYCLE_COLUMNS + " WHERE position_uid = ?",
            (position_uid,),
        ).fetchone()
        if row is None:
            return None
        return self._row_with_legs(row)

    def get_open(self) -> list[PositionLifecycleRow]:
        """All currently-open lifecycle rows (status in
        {pending, open, partially_filled}), with legs.

        Used by the operator CLI's `positions` command. Ordered by
        ``created_at`` ascending (oldest first) for deterministic
        display.
        """
        rows = self._conn.execute(
            _SELECT_LIFECYCLE_COLUMNS
            + " WHERE status IN ('pending', 'open', 'partially_filled') "
            "ORDER BY created_at ASC"
        ).fetchall()
        return [self._row_with_legs(r) for r in rows]

    def get_open_for_owner_key(self, owner_key: str) -> PositionLifecycleRow | None:
        """The single open lifecycle row for an owner_key, or None.

        Per `engine.positions`, owner_key is the broker-aggregation key
        (equity: symbol; options: underlying ticker; spread: per-
        instance UUID). At any moment a single owner_key should have
        at most one non-terminal lifecycle row — if more than one is
        found, this returns the *most recently created* and logs
        nothing (the caller should detect and reconcile).

        Returns None if no open row exists for that owner_key.
        """
        rows = self._conn.execute(
            _SELECT_LIFECYCLE_COLUMNS
            + " WHERE owner_key = ? AND status IN "
            "('pending', 'open', 'partially_filled') "
            "ORDER BY created_at DESC LIMIT 1",
            (owner_key,),
        ).fetchall()
        if not rows:
            return None
        return self._row_with_legs(rows[0])

    def get_legs_for(self, position_uid: str) -> list[PositionLifecycleLeg]:
        """Just the legs for a position_uid (no parent row)."""
        rows = self._conn.execute(
            "SELECT position_uid, symbol, side, qty, avg_entry_price "
            "FROM position_lifecycle_legs WHERE position_uid = ? "
            "ORDER BY id ASC",
            (position_uid,),
        ).fetchall()
        return [
            PositionLifecycleLeg(
                position_uid=r[0],
                symbol=r[1],
                side=r[2],
                qty=r[3],
                avg_entry_price=r[4],
            )
            for r in rows
        ]

    # ── Internal ──────────────────────────────────────────────────────

    def _insert_leg(self, leg: PositionLifecycleLeg) -> None:
        self._conn.execute(
            "INSERT INTO position_lifecycle_legs "
            "(position_uid, symbol, side, qty, avg_entry_price) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                leg.position_uid,
                leg.symbol,
                leg.side,
                leg.qty,
                leg.avg_entry_price,
            ),
        )

    def _row_with_legs(self, row: tuple) -> PositionLifecycleRow:
        position_uid = row[0]
        meta = {}
        if row[16]:
            try:
                meta = json.loads(row[16])
            except (TypeError, ValueError):
                meta = {"_raw": row[16]}
        legs = tuple(self.get_legs_for(position_uid))
        return PositionLifecycleRow(
            position_uid=position_uid,
            created_at=row[1],
            closed_at=row[2],
            symbol=row[3],
            owner_key=row[4],
            strategy=row[5],
            position_type=row[6],
            status=row[7],
            entry_qty=row[8],
            current_qty=row[9],
            avg_entry_price=row[10],
            net_realized_pnl=row[11],
            entry_order_id=row[12],
            entry_client_order_id=row[13],
            first_fill_at=row[14],
            last_fill_at=row[15],
            metadata=meta,
            legs=legs,
        )


_SELECT_LIFECYCLE_COLUMNS = (
    "SELECT position_uid, created_at, closed_at, symbol, owner_key, "
    "strategy, position_type, status, entry_qty, current_qty, "
    "avg_entry_price, net_realized_pnl, entry_order_id, "
    "entry_client_order_id, first_fill_at, last_fill_at, metadata_json "
    "FROM position_lifecycle"
)


# ── Validators / helpers ──────────────────────────────────────────────


def _validate_position_uid(position_uid: str) -> None:
    if not position_uid or not isinstance(position_uid, str):
        raise ValueError(f"position_uid must be non-empty string; got {position_uid!r}")
    if not position_uid.startswith("pos_"):
        raise ValueError(
            f"position_uid must start with 'pos_'; got {position_uid!r}"
        )


def _validate_position_type(position_type: str) -> None:
    if position_type not in VALID_POSITION_TYPES:
        raise ValueError(
            f"position_type must be one of {sorted(VALID_POSITION_TYPES)}; "
            f"got {position_type!r}"
        )


def _utc_now_iso() -> str:
    """UTC timestamp in ISO 8601, second precision."""
    return datetime.now(timezone.utc).isoformat()
