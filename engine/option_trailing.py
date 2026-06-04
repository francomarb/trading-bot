"""Durable trailing-stop state for single-leg option positions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone


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
    last_updated_at         TEXT NOT NULL
);
"""

_OPTION_TRAILING_STOPS_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_option_trailing_stops_strategy "
    "ON option_trailing_stops(strategy)",
    "CREATE INDEX IF NOT EXISTS idx_option_trailing_stops_owner_key "
    "ON option_trailing_stops(owner_key)",
    "CREATE INDEX IF NOT EXISTS idx_option_trailing_stops_occ "
    "ON option_trailing_stops(occ_symbol)",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class OptionTrailingStopRow:
    """One durable trailing-stop row for a single-leg option position."""

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
    ) -> None:
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
                last_observed_premium, last_updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                last_updated_at = excluded.last_updated_at
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
            ),
        )
        self._conn.commit()

    def get_by_occ(self, occ_symbol: str) -> OptionTrailingStopRow | None:
        row = self._conn.execute(
            """
            SELECT position_uid, occ_symbol, strategy, owner_key, qty,
                   entry_premium, hwm_premium, trail_activation_pct, trail_pct,
                   current_stop_price, alpaca_stop_order_id, stop_order_status,
                   last_observed_premium, last_updated_at
            FROM option_trailing_stops
            WHERE occ_symbol = ?
            """,
            (occ_symbol,),
        ).fetchone()
        if row is None:
            return None
        return OptionTrailingStopRow(*row)

    def delete_by_occ(self, occ_symbol: str) -> None:
        self._conn.execute(
            "DELETE FROM option_trailing_stops WHERE occ_symbol = ?",
            (occ_symbol,),
        )
        self._conn.commit()


__all__ = [
    "OptionTrailingStopRow",
    "OptionTrailingStopStore",
    "_CREATE_OPTION_TRAILING_STOPS_SQL",
    "_OPTION_TRAILING_STOPS_INDEXES_SQL",
]
