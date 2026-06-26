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
from datetime import datetime, timedelta, timezone
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

# trades dedup key per discovery doc §6.5 (R5-C1 + R5-C2 fixes):
# scoped to single-leg rows only because log_spread_fill deliberately
# writes two leg rows with the same combo order_id. The ON CONFLICT
# clause inside apply_order_event references this exact predicate
# (R5-C2 SQLite partial-index alignment).
_UNIQ_TRADES_ORDER_ID_SINGLE_LEG_SQL = (
    "CREATE UNIQUE INDEX IF NOT EXISTS uniq_trades_order_id_single_leg "
    "ON trades(order_id) "
    "WHERE order_id IS NOT NULL AND position_type = 'single_leg'"
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


@dataclass(frozen=True)
class TradesOrderIdDuplicate:
    """One duplicate cluster on ``trades.order_id`` for the single-leg
    partial UNIQUE scope. Each entry collides with the new
    ``uniq_trades_order_id_single_leg`` index when it is created.

    Added per PR #60 review (commit 7): preflight must also detect
    trades-side duplicates because the same partial UNIQUE the discovery
    doc §6.5 requires is created during ``_ensure_db()``. Without this
    detection an existing duplicate raises a generic IntegrityError
    rather than the structured remediation flow §12.2 promises.
    """

    order_id: str
    count: int
    trade_ids: tuple[int, ...] = field(default_factory=tuple)


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

    The exception carries two dimensions of duplicates: position-level
    ``owner_key_duplicates`` (blocking
    ``uniq_one_active_position_per_owner_key``) and per-order
    ``trades_order_id_duplicates`` (blocking
    ``uniq_trades_order_id_single_leg``).
    """

    def __init__(
        self,
        *,
        owner_key_duplicates: tuple[OwnerKeyDuplicate, ...] = (),
        trades_order_id_duplicates: tuple[TradesOrderIdDuplicate, ...] = (),
        message: str | None = None,
    ) -> None:
        self.owner_key_duplicates = owner_key_duplicates
        self.trades_order_id_duplicates = trades_order_id_duplicates
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
            if trades_order_id_duplicates:
                parts.append(
                    f"{len(trades_order_id_duplicates)} trades.order_id "
                    f"duplicates (position_type='single_leg' scope)"
                )
                for dup in trades_order_id_duplicates[:5]:
                    parts.append(
                        f"  - order_id={dup.order_id!r} "
                        f"count={dup.count} "
                        f"trade_ids={dup.trade_ids}"
                    )
                if len(trades_order_id_duplicates) > 5:
                    parts.append(
                        f"  ... and {len(trades_order_id_duplicates) - 5} more"
                    )
            parts.append(
                "Pre-existing duplicates block the foundation PR's "
                "uniqueness indexes. Run scripts/migrate_dedupe_trades.py "
                "(detection / review / apply modes per discovery doc §12.2) "
                "and retry."
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

    Returns an empty tuple when ``position_lifecycle`` does not exist
    yet (first-startup DB or a partial-bootstrap test fixture). The
    table is created earlier in the same ``_ensure_db`` pass in
    production, so this only triggers in tests that call preflight
    against a hand-built connection.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='position_lifecycle'"
    ).fetchone()
    if has_table is None:
        return ()

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


def detect_trades_order_id_duplicates(
    conn: sqlite3.Connection,
) -> tuple[TradesOrderIdDuplicate, ...]:
    """Detect duplicate ``trades.order_id`` clusters that would block
    the new ``uniq_trades_order_id_single_leg`` partial UNIQUE index.

    Scope: any row with ``order_id IS NOT NULL`` and ``position_type``
    NULL **or** ``'single_leg'`` — i.e. anything that either already
    lives inside the partial index predicate or would be migrated
    into it by ``_BACKFILL_SQL``. Rows with ``position_type='spread'``
    are deliberately excluded because spread legs legitimately share
    ``order_id`` (combo order; one DB row per OCC leg) and are not
    in the unique scope.

    Detecting NULL+NULL and NULL+single_leg collisions matters
    because the BACKFILL step that runs immediately after preflight
    sets ``position_type='single_leg'`` on every NULL row — duplicates
    that look harmless pre-BACKFILL become hard collisions seconds
    later. The previous implementation relied on ``UPDATE OR IGNORE``
    in the BACKFILL to paper over these, which silently quarantined
    duplicate rows at NULL ``position_type`` and made them invisible
    to the rest of the schema. Preflight now surfaces them up front
    so the operator's dedupe script can act on the full picture.

    The ``trades`` table may not exist yet on a first-startup DB. In
    that case the partial UNIQUE can be created cleanly without any
    duplicates being possible, so we return an empty tuple rather
    than raising.

    Returns an empty tuple when the database is clean. Callers can
    treat a non-empty result as a fatal precondition and raise
    ``MigrationDuplicatesFound``.
    """
    has_trades = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='trades'"
    ).fetchone()
    if has_trades is None:
        return ()

    cols = conn.execute("PRAGMA table_info(trades)").fetchall()
    column_names = {col[1] for col in cols}
    if "order_id" not in column_names:
        return ()
    # Pre-PR-59 trades tables won't have position_type; the column
    # gets added by the migration step that runs BEFORE preflight in
    # reporting.logger._ensure_db, so by the time this function runs
    # the column is always present. The check below is defensive
    # against being called against an arbitrary connection (tests).
    if "position_type" not in column_names:
        type_filter = ""
        params: tuple = ()
    else:
        type_filter = (
            "AND (position_type IS NULL OR position_type = 'single_leg') "
        )
        params = ()

    rows = conn.execute(
        f"""
        SELECT order_id, COUNT(*) AS n,
               GROUP_CONCAT(id, ',') AS ids
        FROM trades
        WHERE order_id IS NOT NULL
          {type_filter}
        GROUP BY order_id
        HAVING COUNT(*) > 1
        ORDER BY order_id
        """,
        params,
    ).fetchall()
    duplicates: list[TradesOrderIdDuplicate] = []
    for order_id, count, ids_csv in rows:
        ids_tuple: tuple[int, ...] = (
            tuple(int(s) for s in ids_csv.split(",")) if ids_csv else ()
        )
        duplicates.append(
            TradesOrderIdDuplicate(
                order_id=str(order_id),
                count=int(count),
                trade_ids=ids_tuple,
            )
        )
    return tuple(duplicates)


def run_preflight_or_raise(conn: sqlite3.Connection) -> None:
    """Run all migration preflight checks before applying foundation
    PR's new UNIQUE INDEXes. Raises ``MigrationDuplicatesFound`` on
    any conflict (either dimension; the exception carries both). Safe
    to call on every startup; runs fast (indexed GROUP BY queries)
    when the database is clean.

    Two dimensions are checked:

    - ``position_lifecycle.owner_key`` duplicates (blocking
      ``uniq_one_active_position_per_owner_key``)
    - ``trades.order_id`` duplicates within
      ``position_type='single_leg'`` (blocking
      ``uniq_trades_order_id_single_leg``)

    Both unique indexes are created by the same ``_ensure_db()`` call
    on the same startup, so both dimensions must be clean before
    either index can be created. The exception consolidates them so
    the operator sees the full remediation surface in one diagnostic
    message rather than fixing one and tripping the other on the
    next startup.
    """
    owner_key_dupes = detect_owner_key_duplicates(conn)
    trades_dupes = detect_trades_order_id_duplicates(conn)
    if owner_key_dupes or trades_dupes:
        raise MigrationDuplicatesFound(
            owner_key_duplicates=owner_key_dupes,
            trades_order_id_duplicates=trades_dupes,
        )


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

    def attach_or_update_order_id_for_walk_step(
        self,
        *,
        client_order_id: str,
        order_id: str,
        submitted_at: str | None = None,
    ) -> bool:
        """Attach the broker ``order_id`` on a non-terminal close row,
        overwriting any previously-attached id.

        Differs from ``attach_broker_order_id`` in two ways:

          1. Accepts overwrites when the row is still non-terminal —
             supports walk-and-market progression where each step's
             broker order_id replaces the previous one (only one step
             is alive at the broker at any moment; the substrate row
             must track the *current* in-flight order so the cycle
             reconciler can poll the right id after a crash).

          2. Returns ``True`` when the row was updated, ``False`` if
             the row doesn't exist or is already terminal — callers
             can log and move on without raising. The single-leg
             ``attach_broker_order_id`` raises in these cases because
             single-leg attaches are exactly-once.

        Used by the spread close path (§10.7). The eager-attach
        (submit-time, via worker on_submitted callback queued to the
        engine drain) is what closes the restart-gap: a crash between
        broker submit and the next drain still leaves the substrate
        row carrying the live broker order_id, so the cycle / startup
        reconciler can resolve it via REST.
        """
        now = submitted_at or _utc_now_iso()
        existing = self._conn.execute(
            "SELECT status FROM position_lifecycle_orders "
            "WHERE client_order_id = ?",
            (client_order_id,),
        ).fetchone()
        if existing is None:
            return False
        if existing[0] in TERMINAL_ORDER_STATUSES:
            return False
        self._conn.execute(
            "UPDATE position_lifecycle_orders "
            "SET order_id = ?, "
            "    submitted_at = COALESCE(submitted_at, ?), "
            "    last_observed_at = ? "
            "WHERE client_order_id = ?",
            (order_id, now, now, client_order_id),
        )
        self._conn.commit()
        return True

    def mark_terminal_after_dispatch(
        self,
        *,
        client_order_id: str,
        broker_order_id: str | None,
        status: str,
        filled_qty: float,
        avg_fill_price: float | None,
        broker_updated_at: str | None = None,
    ) -> None:
        """Advance a per-order row from ``pending`` to a terminal status
        in one call, attaching the broker order_id along the way.

        Designed for the spread close path (§10.7): the substrate row is
        inserted ``pending`` before ``dispatch_spread_order(closing=True)``
        and the broker order_id is only observed via the drain. This
        method does the attach + status update without invoking the
        single-leg-scoped ``apply_order_event`` pipeline (no trades
        UPSERT, no position rollup, no position-status CTE — none of
        those apply to spreads).

        ``status`` must be one of ``filled / canceled / rejected /
        partially_filled / working / unknown``. Terminal states stamp
        ``terminal_at``; non-terminal advances leave it NULL.

        ``broker_order_id`` may be None — for spreads whose worker
        rolled back before any broker order was assigned (e.g. dry run
        canceled, build_mleg_request raised), we still want to mark
        the row terminal so the position is unlocked.

        Raises ``ValueError`` if the row does not exist.
        """
        _validate_status(status)
        now = _utc_now_iso()
        broker_updated_at = broker_updated_at or now
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
        # Idempotent: re-applying the same terminal status is a no-op
        # (the row already reflects it). Refuse to walk terminal → other.
        if current_status in TERMINAL_ORDER_STATUSES:
            return
        # If broker_order_id is provided and the row already has one,
        # keep the existing (immutable). Otherwise attach.
        order_id_to_set = current_order_id or broker_order_id
        is_terminal = status in TERMINAL_ORDER_STATUSES
        self._conn.execute(
            """
            UPDATE position_lifecycle_orders
            SET status                          = ?,
                filled_qty                      = ?,
                avg_fill_price                  = ?,
                order_id                        = ?,
                submitted_at                    = COALESCE(submitted_at, ?),
                last_observed_broker_updated_at = ?,
                last_observed_at                = ?,
                terminal_at                     = CASE
                    WHEN ? = 1 THEN ?
                    ELSE terminal_at
                END
            WHERE client_order_id = ?
            """,
            (
                status,
                float(filled_qty),
                avg_fill_price,
                order_id_to_set,
                now,
                broker_updated_at,
                now,
                1 if is_terminal else 0, now,
                client_order_id,
            ),
        )
        self._conn.commit()

    def mark_pending_unknown_to_broker(
        self,
        *,
        client_order_id: str,
        reason: str,
    ) -> bool:
        """Resolve a NULL-order_id orphan that the broker has never
        heard of by marking the substrate row 'rejected'.

        Outcome (c) of the NULL-order_id REST sweep (tracker row 89,
        'Known follow-ups'). When ``get_order_by_client_id`` raises
        NotFound, the broker never accepted the submission — there is
        no live order to lose track of, so the substrate row should
        be transitioned out of pending so the position rollup can
        progress.

        Behavior:
          - status='pending' AND order_id IS NULL → UPDATE to
            'rejected', set terminal_at, last_observed_at; run the
            position-status CTE so the parent position transitions
            (typically pending → canceled if no fill ever landed).
          - status already terminal OR order_id already attached →
            no-op; return False. The orphan was already resolved out
            of band (operator intervention, PR #71 trailing fallback,
            or a racing attach).
          - Row missing entirely → return False. Caller treats this
            as benign.

        Trades table is NOT touched: no fill happened, no trade row.
        Position ``current_qty`` is NOT touched: a row that never had
        order_id never had filled_qty either, so the rollup stays at
        zero. We only need the position-status CTE to advance the
        parent position out of 'pending'.

        Returns True if the row was rejected, False if no-op.
        """
        if not reason:
            raise ValueError("reason must not be empty")
        now = _utc_now_iso()
        # All-or-nothing transaction: row UPDATE + position-status
        # recompute land together or not at all.
        with self._conn:
            existing = self._conn.execute(
                "SELECT status, order_id, position_uid "
                "FROM position_lifecycle_orders "
                "WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            if existing is None:
                return False
            current_status, current_order_id, position_uid = existing
            if (
                current_status in TERMINAL_ORDER_STATUSES
                or current_order_id is not None
                or current_status != "pending"
            ):
                # Already resolved out of band; sweep treats as no-op.
                return False
            self._conn.execute(
                "UPDATE position_lifecycle_orders "
                "SET status = 'rejected', "
                "    terminal_at = ?, "
                "    last_observed_at = ? "
                "WHERE client_order_id = ?",
                (now, now, client_order_id),
            )
            # Position-status CTE recompute — typically transitions
            # the parent from pending → canceled when this was the
            # only pending entry. Imported here to avoid a
            # forward-ref against the module-level SQL constant.
            self._conn.execute(
                _POSITION_STATUS_SQL,
                {"position_uid": position_uid, "now": now},
            )
        return True

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

    def get_non_terminal_with_order_id(
        self, *, limit: int | None = None,
    ) -> list[PositionLifecycleOrderRow]:
        """All non-terminal rows that already have a broker order_id
        attached. Used by cycle-reconciliation (P-2) to walk rows
        whose state might have advanced at the broker without the
        WebSocket noticing.

        Rows still at status='pending' without an order_id are
        excluded — those belong to the lifecycle-attach queue path
        (foundation commit 12), not the broker-state reconciliation
        path.

        ``error`` status is also excluded: it's a sticky sentinel
        for invariant violations (§6.6.1 R9-P1b) and should not be
        revived by a passing broker fetch."""
        active_statuses = ("pending", "working", "partially_filled", "unknown")
        placeholders = ", ".join("?" for _ in active_statuses)
        sql = (
            _SELECT_LIFECYCLE_ORDER_COLUMNS
            + f" WHERE order_id IS NOT NULL AND status IN ({placeholders}) "
            "ORDER BY id ASC"
        )
        params: tuple = active_statuses
        if limit is not None:
            sql += " LIMIT ?"
            params = active_statuses + (int(limit),)
        rows = self._conn.execute(sql, params).fetchall()
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

    def get_non_terminal_spread_close_rows(
        self,
    ) -> list[PositionLifecycleOrderRow]:
        """All non-terminal close-side rows (role IN ('exit',
        'partial_close')) whose parent position is a spread.

        Used by the spread close reconciler (cycle + startup) to walk
        rows whose state may have advanced at the broker without the
        WebSocket noticing. ``apply_order_event`` is single-leg-scoped;
        spreads use ``mark_terminal_after_dispatch`` instead, so they
        need a dedicated walk that doesn't intermix with the
        single-leg reconciler.

        Returns rows in id-ascending order (insertion order).
        Includes rows with order_id IS NULL (e.g. the partial_close
        residual placeholder); callers must guard their broker fetch
        on order_id not being None.
        """
        roles = ("exit", "partial_close")
        status_placeholders = ", ".join("?" for _ in NON_TERMINAL_ORDER_STATUSES)
        rows = self._conn.execute(
            f"""
            SELECT plo.id, plo.position_uid, plo.role, plo.order_id,
                   plo.client_order_id, plo.order_type, plo.order_class,
                   plo.time_in_force, plo.side, plo.intended_qty,
                   plo.intended_stop_price, plo.intended_trigger_price,
                   plo.intended_limit_price, plo.intended_take_profit_price,
                   plo.parent_order_id, plo.replaces_order_id,
                   plo.origin_kind, plo.operator_command_uid,
                   plo.slippage_benchmark_price, plo.slippage_benchmark_kind,
                   plo.slippage_benchmark_timestamp,
                   plo.slippage_measurement_quality,
                   plo.status, plo.filled_qty, plo.avg_fill_price,
                   plo.created_at, plo.submitted_at, plo.terminal_at,
                   plo.last_observed_broker_updated_at, plo.last_observed_at
            FROM position_lifecycle_orders plo
            JOIN position_lifecycle pl ON pl.position_uid = plo.position_uid
            WHERE pl.position_type = 'spread'
              AND plo.role IN (?, ?)
              AND plo.status IN ({status_placeholders})
            ORDER BY plo.id ASC
            """,
            (roles[0], roles[1], *sorted(NON_TERMINAL_ORDER_STATUSES)),
        ).fetchall()
        return [_row_from_tuple(r) for r in rows]

    def get_orphaned_pending_single_leg_orders(
        self,
        *,
        min_age_seconds: int,
        limit: int | None = None,
    ) -> list[PositionLifecycleOrderRow]:
        """Single-leg pending rows where the broker order_id was never
        attached (orphan candidates for the NULL-order_id REST sweep).

        Returns rows matching ALL of:

          - ``position_lifecycle.position_type = 'single_leg'`` — spreads
            have their own crash-durable close path (PR #72 §10.7) and
            must not be touched by this sweep.
          - ``status = 'pending'`` — once a row reaches
            working/partially_filled/filled the broker order_id is
            already known by definition; this query targets rows the
            attach queue never got to.
          - ``order_id IS NULL`` — by construction the attach failed
            or the bot crashed between submit return and the next
            cycle's drain.
          - ``client_order_id IS NOT NULL`` — invariant of the
            schema (NOT NULL column), restated here so the SQL
            documents the sweep's recovery key.
          - The only intentional ``order_id IS NULL`` shape is the
            spread ``partial_close`` residual placeholder from PR #72
            §10.7. That row sits on a ``position_type='spread'``
            parent and is excluded by the JOIN above. Single-leg
            ``partial_close`` rows (operator ``reduce-position``,
            execution/broker.py:_lifecycle_orders_record_exit with
            ``partial_qty``) ARE valid sweep targets — they go
            through the same insert + attach pattern as ``exit``
            rows and orphan the same way if attach fails or the bot
            crashes mid-call.
          - ``created_at <= now - min_age_seconds`` — gives the
            synchronous attach queue time to drain naturally. The
            sweep is a safety net, not a replacement for the
            normal-path attach. 60s is the recommended floor; below
            ~30s the sweep starts competing with healthy attach
            flow and produces operator-visible INFO recovery noise
            for rows that would have self-healed.

        Returns oldest-first (id ASC ≈ created_at ASC since both
        increase monotonically per insert). ``limit`` is an optional
        safety cap on the result-set size (applied at SQL). Both
        engine callers — cycle AND startup — pass ``None`` (PR #73
        review R2): the REST budget is enforced in-loop by the
        engine and a SQL LIMIT here would starve newer orphans when
        the oldest rows are persistently failing. The ``limit``
        parameter is preserved for tests and future callers that
        legitimately want bounded reads.

        Operator note: the CRITICAL log in `_drain_lifecycle_attaches`
        is the real-time orphan signal and stays in place after this
        sweep ships. A successful sweep recovery produces a
        complementary INFO log so the failure→recovery pair is
        diagnostic evidence the substrate path is healthy.
        """
        if min_age_seconds < 0:
            raise ValueError(
                f"min_age_seconds must be >= 0; got {min_age_seconds}"
            )
        # Compute the cutoff in Python rather than SQL so the time
        # source is consistent with _utc_now_iso elsewhere in the
        # store and so the query stays trivially testable by
        # patching the cutoff.
        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=int(min_age_seconds))
        ).isoformat()
        sql = (
            "SELECT plo.id, plo.position_uid, plo.role, plo.order_id, "
            "plo.client_order_id, plo.order_type, plo.order_class, "
            "plo.time_in_force, plo.side, plo.intended_qty, "
            "plo.intended_stop_price, plo.intended_trigger_price, "
            "plo.intended_limit_price, plo.intended_take_profit_price, "
            "plo.parent_order_id, plo.replaces_order_id, "
            "plo.origin_kind, plo.operator_command_uid, "
            "plo.slippage_benchmark_price, plo.slippage_benchmark_kind, "
            "plo.slippage_benchmark_timestamp, "
            "plo.slippage_measurement_quality, "
            "plo.status, plo.filled_qty, plo.avg_fill_price, "
            "plo.created_at, plo.submitted_at, plo.terminal_at, "
            "plo.last_observed_broker_updated_at, plo.last_observed_at "
            "FROM position_lifecycle_orders plo "
            "JOIN position_lifecycle pl "
            "  ON pl.position_uid = plo.position_uid "
            "WHERE pl.position_type = 'single_leg' "
            "  AND plo.status = 'pending' "
            "  AND plo.order_id IS NULL "
            "  AND plo.client_order_id IS NOT NULL "
            "  AND plo.created_at <= ? "
            "ORDER BY plo.id ASC"
        )
        params: tuple = (cutoff,)
        if limit is not None:
            sql += " LIMIT ?"
            params = (cutoff, int(limit))
        rows = self._conn.execute(sql, params).fetchall()
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


def _validate_status(status: str) -> None:
    if status not in VALID_ORDER_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(VALID_ORDER_STATUSES)}; "
            f"got {status!r}"
        )


# ── apply_order_event (discovery doc §6.4 / §6.5 / §6.6 / §6.6.1) ──


@dataclass(frozen=True)
class OrderEvent:
    """One observed broker event for a specific order.

    All fields are required except ``execution_id`` and
    ``avg_fill_price`` (the latter is NULL on rejections and on
    canceled-zero-fill orders).

    ``broker_updated_at`` is the order's ``updated_at`` field as
    returned by Alpaca — the strict-newer rule in §6.4 uses this
    as the tiebreaker when state-machine rank and filled_qty are
    exactly equal (rare backend reissues).

    ``execution_id`` is audit-only on ``trades`` per discovery doc
    §6.5. REST recovery has no execution_id and passes None.
    """

    order_id: str
    status: str
    filled_qty: float
    avg_fill_price: float | None
    broker_updated_at: str
    execution_id: str | None = None


@dataclass(frozen=True)
class OrderEventOutcome:
    """Outcome of a single apply_order_event call.

    ``applied=True`` means the per-order row advanced AND the
    transaction (per-order UPDATE + trades UPSERT + position rollup
    + position status update) committed.

    ``applied=False`` means the event was ignored. ``reason``
    distinguishes the cases callers care about:
      - 'stale_or_duplicate': the event is older than the row's
        current observed state (per the strict-newer rule).
      - 'terminal_blocked': the row is already at filled / canceled /
        rejected; terminal states are immutable.
      - 'unknown_order': order_id has no matching per-order row.
        Foundation will not synthesize rows; the caller (typically
        a recovery path) should insert_pending first if appropriate.
    """

    applied: bool
    reason: str
    position_uid: str | None = None
    new_status: str | None = None


# State-machine rank case expression — matches §6.3 / §6.4 exactly.
# Pulled into one fragment so the compare-and-set SQL and the
# rollup SQL can reference identical semantics.
_STATUS_RANK_CASE = (
    "CASE status "
    "WHEN 'pending' THEN 0 "
    "WHEN 'working' THEN 1 "
    "WHEN 'unknown' THEN 1 "
    "WHEN 'partially_filled' THEN 2 "
    "ELSE 3 "
    "END"
)

_INCOMING_RANK_PARAM_CASE = (
    "CASE :status "
    "WHEN 'pending' THEN 0 "
    "WHEN 'working' THEN 1 "
    "WHEN 'unknown' THEN 1 "
    "WHEN 'partially_filled' THEN 2 "
    "ELSE 3 "
    "END"
)


# Compare-and-set SQL per discovery doc §6.4 (R3-P0 + R3-P1a +
# R10-P1 fixes incorporated). Order_id is the outer guard; terminal
# states are immutable; updated_at is the tiebreaker WITHIN
# state-machine-equal events only — never a bypass.
_COMPARE_AND_SET_SQL = f"""
UPDATE position_lifecycle_orders
SET
    status                          = :status,
    filled_qty                      = :filled_qty,
    avg_fill_price                  = :avg_fill_price,
    last_observed_broker_updated_at = :broker_updated_at,
    last_observed_at                = :now,
    terminal_at = CASE
        WHEN :status IN ('filled', 'canceled', 'rejected') THEN :now
        ELSE terminal_at
    END
WHERE order_id = :order_id
  AND status NOT IN ('filled', 'canceled', 'rejected')
  AND (
      ({_STATUS_RANK_CASE}, filled_qty)
        < ({_INCOMING_RANK_PARAM_CASE}, :filled_qty)
      OR (
          {_STATUS_RANK_CASE} = {_INCOMING_RANK_PARAM_CASE}
          AND filled_qty = :filled_qty
          AND (
              last_observed_broker_updated_at IS NULL
              OR last_observed_broker_updated_at < :broker_updated_at
          )
      )
  )
"""


# Trades UPSERT per discovery doc §6.5 (R5-C1 + R5-C2 fixes).
# Partial-unique-index conflict target matches the index WHERE clause
# exactly so SQLite ON CONFLICT can attach to it.
_TRADES_UPSERT_SQL = """
INSERT INTO trades (
    timestamp, symbol, side, qty, avg_fill_price, order_id,
    strategy, reason, stop_price, entry_reference_price,
    modeled_slippage_bps, realized_slippage_bps,
    order_type, status, requested_qty, filled_qty,
    initial_stop_loss, initial_risk_per_share, initial_risk_dollars,
    realized_pnl, r_multiple,
    entry_timestamp, exit_timestamp,
    position_id, position_type, position_uid,
    slippage_benchmark_price, slippage_benchmark_kind,
    slippage_benchmark_timestamp, slippage_measurement_quality,
    slippage_signed_bps, slippage_adverse_bps, stop_trigger_price,
    execution_id
) VALUES (
    :now, :symbol, :side, :filled_qty, :avg_fill_price, :order_id,
    :strategy, :reason, 0.0, COALESCE(:slippage_benchmark_price, 0.0),
    NULL, NULL,
    :order_type, :order_status, :intended_qty, :filled_qty,
    NULL, NULL, NULL,
    NULL, NULL,
    :now, NULL,
    :position_id, 'single_leg', :position_uid,
    :slippage_benchmark_price, :slippage_benchmark_kind,
    :slippage_benchmark_timestamp, :slippage_measurement_quality,
    NULL, NULL, NULL,
    :execution_id
)
ON CONFLICT(order_id) WHERE order_id IS NOT NULL AND position_type = 'single_leg'
DO UPDATE SET
    filled_qty                    = excluded.filled_qty,
    avg_fill_price                = excluded.avg_fill_price,
    status                        = excluded.status,
    position_uid                  = COALESCE(trades.position_uid, excluded.position_uid),
    slippage_benchmark_price      = COALESCE(trades.slippage_benchmark_price,      excluded.slippage_benchmark_price),
    slippage_benchmark_kind       = COALESCE(trades.slippage_benchmark_kind,       excluded.slippage_benchmark_kind),
    slippage_benchmark_timestamp  = COALESCE(trades.slippage_benchmark_timestamp,  excluded.slippage_benchmark_timestamp),
    slippage_measurement_quality  = COALESCE(trades.slippage_measurement_quality,  excluded.slippage_measurement_quality),
    execution_id                  = COALESCE(excluded.execution_id, trades.execution_id)
"""


# Position rollup SQL — discovery doc §6.6 (R3-P1b side-signed sum) +
# R13-G2 (net_realized_pnl from trades).
_POSITION_ROLLUP_SQL = """
WITH signed_qty AS (
    SELECT COALESCE(SUM(
        CASE side
            WHEN 'buy'  THEN  filled_qty
            WHEN 'sell' THEN -filled_qty
            ELSE              0
        END
    ), 0.0) AS net_qty
    FROM position_lifecycle_orders
    WHERE position_uid = :position_uid
)
UPDATE position_lifecycle
SET
    current_qty = CASE
        WHEN ABS((SELECT net_qty FROM signed_qty)) <= 1e-9 THEN 0.0
        ELSE (SELECT net_qty FROM signed_qty)
    END,
    avg_entry_price = (
        SELECT SUM(filled_qty * avg_fill_price) / NULLIF(SUM(filled_qty), 0)
        FROM position_lifecycle_orders
        WHERE position_uid = :position_uid
          AND role IN ('entry_primary', 'entry_residual')
          AND filled_qty > 0
    ),
    net_realized_pnl = COALESCE((
        SELECT SUM(realized_pnl)
        FROM trades
        WHERE position_uid = :position_uid
          AND realized_pnl IS NOT NULL
    ), 0.0)
WHERE position_uid = :position_uid
"""


# Position-status SQL — discovery doc §6.6.1 (R7-P0 + R8-1+R12-P1
# walk-back + R8-P2 + R9-P1a CTE + R9-P1b error + R10-P1 + R11-P1
# walk-back + R12-P1 sell-side gate). The CTE computes new_status
# once so the closed_at CASE can read it (a bare subquery in SET
# would see the pre-update value).
_POSITION_STATUS_SQL = """
WITH computed AS (
    SELECT CASE
        WHEN NOT EXISTS (
            SELECT 1 FROM position_lifecycle_orders
            WHERE position_uid = :position_uid
              AND status NOT IN ('pending')
        ) THEN 'pending'

        WHEN NOT EXISTS (
            SELECT 1 FROM position_lifecycle_orders
            WHERE position_uid = :position_uid
              AND filled_qty > 0
        ) THEN
            CASE
                WHEN NOT EXISTS (
                    SELECT 1 FROM position_lifecycle_orders
                    WHERE position_uid = :position_uid
                      AND role IN ('entry_primary', 'entry_residual')
                      AND status IN ('pending', 'working',
                                     'partially_filled', 'unknown')
                ) THEN 'canceled'
                ELSE 'pending'
            END

        WHEN COALESCE((SELECT current_qty FROM position_lifecycle
                       WHERE position_uid = :position_uid), 0) < 0
        THEN 'error'

        WHEN COALESCE((SELECT current_qty FROM position_lifecycle
                       WHERE position_uid = :position_uid), 0) = 0
             AND NOT EXISTS (
                 SELECT 1 FROM position_lifecycle_orders
                 WHERE position_uid = :position_uid
                   AND role IN ('exit', 'partial_close',
                                'protective_stop', 'replacement_stop')
                   AND status IN ('pending', 'working',
                                  'partially_filled', 'unknown')
             )
        THEN 'closed'

        WHEN COALESCE((SELECT current_qty FROM position_lifecycle
                       WHERE position_uid = :position_uid), 0) = 0
        THEN 'partially_filled'

        WHEN COALESCE((SELECT current_qty FROM position_lifecycle
                       WHERE position_uid = :position_uid), 0)
             < COALESCE((SELECT entry_qty FROM position_lifecycle
                         WHERE position_uid = :position_uid), 0)
        THEN 'partially_filled'

        ELSE 'open'
    END AS new_status
)
UPDATE position_lifecycle
SET status = (SELECT new_status FROM computed),
    closed_at = CASE
        WHEN (SELECT new_status FROM computed) IN ('closed', 'external_closed')
        THEN COALESCE(closed_at, :now)
        ELSE closed_at
    END
WHERE position_uid = :position_uid
"""


def apply_order_event(
    conn: sqlite3.Connection,
    event: OrderEvent,
    *,
    reason: str = "",
) -> OrderEventOutcome:
    """Apply one broker event atomically across four steps:

    1. Compare-and-set on the per-order row (§6.4).
    2. Trades UPSERT keyed on order_id (§6.5).
    3. Position-level rollup recompute (§6.6 + R13-G2 net_realized_pnl
       from trades).
    4. Position-level status update via CTE (§6.6.1).

    All four execute inside one ``with conn:`` transaction. Any
    exception rolls back the entire operation; partial application
    is impossible.

    Returns ``OrderEventOutcome`` describing whether the event was
    applied and (if not) why. The caller can use ``reason`` to
    decide whether to retry, alert, or move on.

    Pre-conditions:
      - The per-order row for ``event.order_id`` must already exist
        in ``position_lifecycle_orders``. ``insert_pending`` (the
        broker entry path) and ``attach_broker_order_id`` (post-
        submit) together establish this. Foundation will not
        synthesize rows here.
    """
    _validate_status(event.status)

    # Look up the per-order row + its parent position metadata in
    # one SELECT. apply_order_event needs symbol / strategy / side /
    # slippage benchmark fields for the trades UPSERT.
    pre_row = conn.execute(
        """
        SELECT plo.id, plo.position_uid, plo.role, plo.side,
               plo.order_type, plo.intended_qty,
               plo.slippage_benchmark_price, plo.slippage_benchmark_kind,
               plo.slippage_benchmark_timestamp, plo.slippage_measurement_quality,
               pl.symbol, pl.strategy, pl.owner_key, pl.position_type
        FROM position_lifecycle_orders plo
        JOIN position_lifecycle pl
          ON pl.position_uid = plo.position_uid
        WHERE plo.order_id = ?
        """,
        (event.order_id,),
    ).fetchone()
    if pre_row is None:
        return OrderEventOutcome(
            applied=False, reason="unknown_order"
        )

    (
        row_id, position_uid, role, side,
        order_type, intended_qty,
        slip_price, slip_kind, slip_ts, slip_quality,
        symbol, strategy, owner_key, position_type,
    ) = pre_row

    now = _utc_now_iso()

    try:
        with conn:
            # Step 1: compare-and-set on per-order.
            cur = conn.execute(
                _COMPARE_AND_SET_SQL,
                {
                    "status": event.status,
                    "filled_qty": float(event.filled_qty),
                    "avg_fill_price": event.avg_fill_price,
                    "broker_updated_at": event.broker_updated_at,
                    "now": now,
                    "order_id": event.order_id,
                },
            )
            if cur.rowcount == 0:
                # Either the row is terminal, the event is stale,
                # or the event is a duplicate. Tell the caller
                # apart by re-reading status.
                cur2 = conn.execute(
                    "SELECT status FROM position_lifecycle_orders "
                    "WHERE order_id = ?",
                    (event.order_id,),
                ).fetchone()
                if cur2 is not None and cur2[0] in TERMINAL_ORDER_STATUSES:
                    raise _AppliedZeroRows("terminal_blocked")
                raise _AppliedZeroRows("stale_or_duplicate")

            # Step 2: trades UPSERT keyed on order_id (single-leg scope).
            # Gated on event.filled_qty > 0 (PR #60 commit 8 fix B):
            # §6.5 defines `trades` as cumulative fill state. Status-only
            # transitions like pending→working with no fill, or a zero-
            # fill canceled, must NOT manufacture a trades row — that
            # would inflate downstream slippage / P&L / activity counts
            # with phantom orders that never actually traded.
            # §6.6's rollup of net_realized_pnl from trades depends on
            # this: only rows representing real fills count.
            # ON CONFLICT predicate matches the partial UNIQUE index
            # WHERE clause exactly (R5-C2). Re-applying the same
            # cumulative state is a no-op (values match); an advance
            # updates filled_qty / VWAP. Provenance preserved via
            # COALESCE.
            #
            # Important spread boundary: MLEG/spread lifecycle rows use
            # this same state machine for durable close tracking, but
            # their user-facing trade rows are written by
            # TradeLogger.log_spread_fill() as position_type='spread'
            # with one row per leg. Auto-UPserting them here would create
            # a fake single-leg row and poison read_all_open_owners().
            if float(event.filled_qty) > 0 and position_type == "single_leg":
                conn.execute(
                    _TRADES_UPSERT_SQL,
                    {
                        "now": now,
                        "symbol": symbol,
                        "side": side,
                        "filled_qty": float(event.filled_qty),
                        "avg_fill_price": event.avg_fill_price,
                        "order_id": event.order_id,
                        "strategy": strategy,
                        "reason": reason or f"{role}:{event.status}",
                        "order_type": order_type,
                        "order_status": event.status,
                        "intended_qty": float(intended_qty),
                        "position_id": owner_key,
                        "position_uid": position_uid,
                        "slippage_benchmark_price": slip_price,
                        "slippage_benchmark_kind": slip_kind,
                        "slippage_benchmark_timestamp": slip_ts,
                        "slippage_measurement_quality": slip_quality,
                        "execution_id": event.execution_id,
                    },
                )

            # Step 3: position rollup (current_qty + avg_entry_price
            # from orders, net_realized_pnl from trades).
            conn.execute(
                _POSITION_ROLLUP_SQL,
                {"position_uid": position_uid},
            )

            # Step 4: position-level status via CTE.
            conn.execute(
                _POSITION_STATUS_SQL,
                {"position_uid": position_uid, "now": now},
            )

            # Read back the new status for the outcome.
            new_status_row = conn.execute(
                "SELECT status FROM position_lifecycle "
                "WHERE position_uid = ?",
                (position_uid,),
            ).fetchone()
            new_status = new_status_row[0] if new_status_row else None

    except _AppliedZeroRows as drop:
        return OrderEventOutcome(
            applied=False,
            reason=drop.reason,
            position_uid=position_uid,
        )

    return OrderEventOutcome(
        applied=True,
        reason="applied",
        position_uid=position_uid,
        new_status=new_status,
    )


class _AppliedZeroRows(Exception):
    """Internal signal that the compare-and-set didn't advance the
    row. Caught inside apply_order_event so the transaction rolls
    back cleanly without surfacing as an error to the caller."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason
