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
