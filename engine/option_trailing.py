"""Durable trailing-stop state for single-leg option positions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


# Order lifecycle state-machine split (PR #59 §10.4).
#
# The legacy schema mixed two categories of state on one row:
#   - Strategy state (kept): entry_premium, hwm_premium, trail_*, etc.
#   - Broker-order state (migrated): alpaca_stop_order_id, stop_order_status.
#
# The broker-order columns are a denormalized mirror of state the order
# lifecycle foundation owns on ``position_lifecycle_orders`` as a
# ``role='protective_stop'`` / ``role='replacement_stop'`` row. The
# ``lifecycle_order_id`` FK below makes that relationship explicit and
# load-bearing so readers can join through to substrate-authoritative
# identity / status.
#
# Why ``REFERENCES position_lifecycle_orders(id)`` and not ``(order_id)``:
# ``position_lifecycle_orders.order_id`` has a *partial* unique index
# (``WHERE order_id IS NOT NULL``) and SQLite refuses to treat a
# partial-unique column as an FK parent key. The autoincrement PK is the
# only valid FK target.
#
# ``alpaca_stop_order_id`` / ``stop_order_status`` columns remain as
# denormalized mirrors during migration. Strict column removal is a
# follow-up cleanup PR once every reader has migrated to the FK join.
_CREATE_OPTION_TRAILING_STOPS_SQL = """
CREATE TABLE IF NOT EXISTS option_trailing_stops (
    position_uid            TEXT PRIMARY KEY,
    occ_symbol              TEXT NOT NULL UNIQUE,
    strategy                TEXT NOT NULL,
    owner_key               TEXT NOT NULL,
    qty                     REAL NOT NULL,
    entry_premium           REAL NOT NULL,
    hwm_premium             REAL NOT NULL,
    trail_activation_pct    REAL NOT NULL,
    trail_pct               REAL NOT NULL,
    current_stop_price      REAL,
    alpaca_stop_order_id    TEXT,
    stop_order_status       TEXT,
    last_observed_premium   REAL,
    last_updated_at         TEXT NOT NULL,
    lifecycle_order_id      INTEGER
        REFERENCES position_lifecycle_orders(id)
);
"""

# Idempotent migration for pre-existing databases — ALTER ADD COLUMN
# only when the column is missing. SQLite cannot add a column with a
# FOREIGN KEY clause via ALTER TABLE, so the FK constraint is only
# enforced on fresh tables created by ``_CREATE_OPTION_TRAILING_STOPS_SQL``
# above. On pre-existing tables the column exists as a plain INTEGER and
# is validated by application code + the join-based read instead. New
# installations (and any future rebuild) get the FK enforcement for
# free.
_MIGRATION_COLUMNS = {
    "lifecycle_order_id": "INTEGER",
}


def _ensure_lifecycle_order_id_column(conn: sqlite3.Connection) -> None:
    """Add ``lifecycle_order_id`` to ``option_trailing_stops`` when missing."""
    existing = {
        row[1]
        for row in conn.execute(
            "PRAGMA table_info(option_trailing_stops)"
        ).fetchall()
    }
    for column, col_type in _MIGRATION_COLUMNS.items():
        if column not in existing:
            conn.execute(
                f"ALTER TABLE option_trailing_stops ADD COLUMN {column} {col_type}"
            )


_OPTION_TRAILING_STOPS_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_option_trailing_stops_strategy "
    "ON option_trailing_stops(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_option_trailing_stops_owner_key "
    "ON option_trailing_stops(owner_key)",
    "CREATE INDEX IF NOT EXISTS idx_option_trailing_stops_occ "
    "ON option_trailing_stops(occ_symbol)",
    "CREATE INDEX IF NOT EXISTS idx_option_trailing_stops_lifecycle_order_id "
    "ON option_trailing_stops(lifecycle_order_id)",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class JoinedOptionTrailingRow:
    """Trailing row paired with substrate-authoritative identity / status.

    Returned by ``OptionTrailingStopStore.get_by_occ_joined``. The
    substrate fields are ``None`` when the FK is unpopulated (legacy
    row, or new row whose substrate insert hasn't caught up yet) — the
    caller must fall back to ``trailing.alpaca_stop_order_id`` /
    ``trailing.stop_order_status`` in that case.
    """

    trailing: "OptionTrailingStopRow"
    substrate_order_id: str | None
    substrate_status: str | None

    @property
    def authoritative_order_id(self) -> str | None:
        """Substrate order_id when present, else the mirror column."""
        return self.substrate_order_id or self.trailing.alpaca_stop_order_id

    @property
    def authoritative_status(self) -> str | None:
        """Substrate status when present, else the mirror column."""
        return self.substrate_status or self.trailing.stop_order_status


@dataclass(frozen=True)
class OptionTrailingStopRow:
    """One durable trailing-stop row for a single-leg option position.

    ``lifecycle_order_id`` is the FK into ``position_lifecycle_orders``
    (the autoincrement PK ``id``) and points at the substrate row that
    represents the currently-live broker stop — either the original
    ``protective_stop`` row or, after a ratchet, the latest
    ``replacement_stop`` row. ``None`` on legacy rows written before
    the split; populated for every new submit / replace after PR #59
    §10.4.
    """

    position_uid: str
    occ_symbol: str
    strategy: str
    owner_key: str
    qty: float
    entry_premium: float
    hwm_premium: float
    trail_activation_pct: float
    trail_pct: float
    current_stop_price: float | None
    alpaca_stop_order_id: str | None
    stop_order_status: str | None
    last_observed_premium: float | None
    last_updated_at: str
    lifecycle_order_id: int | None = None


class OptionTrailingStopStore:
    """SQLite-backed state for option trailing stops."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(
        self,
        *,
        position_uid: str,
        occ_symbol: str,
        strategy: str,
        owner_key: str,
        qty: float,
        entry_premium: float,
        hwm_premium: float,
        trail_activation_pct: float,
        trail_pct: float,
        current_stop_price: float | None = None,
        alpaca_stop_order_id: str | None = None,
        stop_order_status: str | None = None,
        last_observed_premium: float | None = None,
        lifecycle_order_id: int | None = None,
    ) -> None:
        """Insert or update the trailing-state row for a single-leg position.

        ``lifecycle_order_id`` should be the autoincrement ``id`` of
        the ``position_lifecycle_orders`` row representing the
        currently-live broker stop. When supplied alongside the legacy
        ``alpaca_stop_order_id`` / ``stop_order_status`` mirror, the
        substrate row is treated as authoritative for identity / status
        by the join-based read; the mirror columns remain populated
        during migration until the follow-up cleanup PR removes them.
        """
        if not position_uid:
            raise ValueError("position_uid must not be empty")
        if not occ_symbol:
            raise ValueError("occ_symbol must not be empty")
        if qty <= 0:
            raise ValueError("qty must be positive")
        if entry_premium <= 0:
            raise ValueError("entry_premium must be positive")
        if hwm_premium <= 0:
            raise ValueError("hwm_premium must be positive")
        now = _utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO option_trailing_stops (
                position_uid, occ_symbol, strategy, owner_key, qty,
                entry_premium, hwm_premium, trail_activation_pct, trail_pct,
                current_stop_price, alpaca_stop_order_id, stop_order_status,
                last_observed_premium, last_updated_at, lifecycle_order_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(position_uid) DO UPDATE SET
                occ_symbol = excluded.occ_symbol,
                strategy = excluded.strategy,
                owner_key = excluded.owner_key,
                qty = excluded.qty,
                entry_premium = excluded.entry_premium,
                hwm_premium = excluded.hwm_premium,
                trail_activation_pct = excluded.trail_activation_pct,
                trail_pct = excluded.trail_pct,
                current_stop_price = excluded.current_stop_price,
                alpaca_stop_order_id = excluded.alpaca_stop_order_id,
                stop_order_status = excluded.stop_order_status,
                last_observed_premium = excluded.last_observed_premium,
                last_updated_at = excluded.last_updated_at,
                lifecycle_order_id = excluded.lifecycle_order_id
            """,
            (
                position_uid,
                occ_symbol,
                strategy,
                owner_key,
                float(qty),
                float(entry_premium),
                float(hwm_premium),
                float(trail_activation_pct),
                float(trail_pct),
                current_stop_price,
                alpaca_stop_order_id,
                stop_order_status,
                last_observed_premium,
                now,
                lifecycle_order_id,
            ),
        )
        self._conn.commit()

    def get_by_occ(self, occ_symbol: str) -> OptionTrailingStopRow | None:
        row = self._conn.execute(
            """
            SELECT position_uid, occ_symbol, strategy, owner_key, qty,
                   entry_premium, hwm_premium, trail_activation_pct, trail_pct,
                   current_stop_price, alpaca_stop_order_id, stop_order_status,
                   last_observed_premium, last_updated_at, lifecycle_order_id
            FROM option_trailing_stops
            WHERE occ_symbol = ?
            """,
            (occ_symbol,),
        ).fetchone()
        if row is None:
            return None
        return OptionTrailingStopRow(*row)

    def get_by_occ_joined(
        self, occ_symbol: str
    ) -> "JoinedOptionTrailingRow | None":
        """Read the trailing row plus substrate-authoritative identity / status.

        Joins through ``option_trailing_stops.lifecycle_order_id`` →
        ``position_lifecycle_orders.id``. The substrate columns are the
        canonical source of broker identity / status post-§10.4; the
        denormalized mirror columns on ``option_trailing_stops`` are
        kept in sync by the writer but should not drive decisions when
        the FK is populated.

        Returns ``None`` when the trailing row is absent. When the FK
        is NULL (legacy rows pre-migration, or rows written before the
        substrate caught up) the substrate fields are ``None`` and the
        caller must fall back to the mirror columns.
        """
        row = self._conn.execute(
            """
            SELECT
                ots.position_uid, ots.occ_symbol, ots.strategy,
                ots.owner_key, ots.qty,
                ots.entry_premium, ots.hwm_premium,
                ots.trail_activation_pct, ots.trail_pct,
                ots.current_stop_price,
                ots.alpaca_stop_order_id, ots.stop_order_status,
                ots.last_observed_premium, ots.last_updated_at,
                ots.lifecycle_order_id,
                plo.order_id, plo.status
            FROM option_trailing_stops AS ots
            LEFT JOIN position_lifecycle_orders AS plo
                ON ots.lifecycle_order_id = plo.id
            WHERE ots.occ_symbol = ?
            """,
            (occ_symbol,),
        ).fetchone()
        if row is None:
            return None
        return JoinedOptionTrailingRow(
            trailing=OptionTrailingStopRow(*row[:15]),
            substrate_order_id=row[15],
            substrate_status=row[16],
        )

    def delete_by_occ(self, occ_symbol: str) -> None:
        self._conn.execute(
            "DELETE FROM option_trailing_stops WHERE occ_symbol = ?",
            (occ_symbol,),
        )
        self._conn.commit()


__all__ = [
    "JoinedOptionTrailingRow",
    "OptionTrailingStopRow",
    "OptionTrailingStopStore",
    "_CREATE_OPTION_TRAILING_STOPS_SQL",
    "_OPTION_TRAILING_STOPS_INDEXES_SQL",
    "_ensure_lifecycle_order_id_column",
]
