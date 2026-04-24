"""
Trade logging and structured JSON sink (Phase 9 → migrated to SQLite in Step 5).

Two responsibilities:

1. **Structured JSON log sink** — adds a loguru sink that writes JSONL
   alongside the human-readable console output. Every log line becomes a
   machine-parseable record for downstream monitoring.

2. **TradeLogger** — inserts a row to the SQLite trades table after every
   fill. The database is the append-only audit trail of every order the bot
   placed, with slippage data baked in.

Design principles:
  - The schema is fixed at table-creation time; reads happen in
    `reporting/pnl.py`, `backtest/reconcile.py`, and in operator tools —
    never in the hot path.
  - All writes are append-only (INSERT). We never UPDATE or DELETE rows.
  - `_ensure_db` is called lazily on first write, not at import time, so
    tests that never write don't need the filesystem.
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from loguru import logger

from config import settings


# ── Structured JSON sink ────────────────────────────────────────────────────


def install_json_sink(path: str | None = None) -> int:
    """
    Add a JSONL loguru sink. Returns the sink ID so callers can remove it
    in tests if needed.
    """
    path = path or settings.JSON_LOG_FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    sink_id = logger.add(
        path,
        serialize=True,
        rotation="10 MB",
        retention="30 days",
        level="DEBUG",
    )
    return sink_id


# ── Schema ──────────────────────────────────────────────────────────────────

TRADE_COLUMNS = [
    "timestamp",
    "symbol",
    "side",
    "qty",
    "avg_fill_price",
    "order_id",
    "strategy",
    "reason",
    "stop_price",
    "entry_reference_price",
    "modeled_slippage_bps",
    "realized_slippage_bps",
    "order_type",
    "status",
    "requested_qty",
    "filled_qty",
]

# Keep the old name as an alias for backwards compatibility with tests
# that import TRADE_CSV_COLUMNS.
TRADE_CSV_COLUMNS = TRADE_COLUMNS

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS trades (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,
    symbol                TEXT    NOT NULL,
    side                  TEXT    NOT NULL,
    qty                   REAL    NOT NULL,
    avg_fill_price        REAL,
    order_id              TEXT,
    strategy              TEXT    NOT NULL,
    reason                TEXT    NOT NULL,
    stop_price            REAL,
    entry_reference_price REAL,
    modeled_slippage_bps  REAL,
    realized_slippage_bps REAL,
    order_type            TEXT,
    status                TEXT    NOT NULL,
    requested_qty         REAL,
    filled_qty            REAL
);
"""


# ── Trade record ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TradeRecord:
    """One row in the trades table — built from a RiskDecision + OrderResult."""

    timestamp: str
    symbol: str
    side: str
    qty: int
    avg_fill_price: float | None
    order_id: str | None
    strategy: str
    reason: str
    stop_price: float
    entry_reference_price: float
    modeled_slippage_bps: float
    realized_slippage_bps: float
    order_type: str
    status: str
    requested_qty: int
    filled_qty: int

    def as_dict(self) -> dict:
        """Column-ordered dict (same interface as before migration)."""
        return {col: getattr(self, col) for col in TRADE_COLUMNS}


# ── TradeLogger ─────────────────────────────────────────────────────────────


class TradeLogger:
    """
    Appends trade records to a SQLite database.

    Usage (from engine):
        trade_logger = TradeLogger()
        record = trade_logger.build_record(decision, result)
        trade_logger.log(record)
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or settings.TRADE_LOG_DB
        self._conn: sqlite3.Connection | None = None

    def _ensure_db(self) -> sqlite3.Connection:
        """Create the database and table if they don't exist yet."""
        if self._conn is not None:
            return self._conn
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute(_CREATE_TABLE_SQL)
        self._conn.commit()
        return self._conn

    def close(self) -> None:
        """Close the database connection."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def build_record(
        self,
        decision,  # RiskDecision
        result,    # OrderResult
        *,
        modeled_price: float | None = None,
    ) -> TradeRecord:
        """
        Build a TradeRecord from a RiskDecision + OrderResult.

        `modeled_price` is the bar close the engine acted on. If provided,
        slippage is computed; otherwise both slippage fields are 0.
        """
        modeled_bps = 0.0
        realized_bps = 0.0
        ref_price = modeled_price or decision.entry_reference_price
        if (
            result.avg_fill_price is not None
            and ref_price > 0
        ):
            realized_bps = (
                abs(result.avg_fill_price - ref_price) / ref_price * 10_000
            )

        return TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=decision.symbol,
            side=decision.side.value,
            qty=result.filled_qty,
            avg_fill_price=result.avg_fill_price,
            order_id=result.order_id,
            strategy=decision.strategy_name,
            reason=decision.reason,
            stop_price=decision.stop_price,
            entry_reference_price=decision.entry_reference_price,
            modeled_slippage_bps=modeled_bps,
            realized_slippage_bps=round(realized_bps, 2),
            order_type=decision.order_type.value,
            status=result.status.value,
            requested_qty=result.requested_qty,
            filled_qty=result.filled_qty,
        )

    def build_close_record(
        self,
        result,    # OrderResult from close_position
        *,
        strategy_name: str,
        modeled_price: float,
    ) -> TradeRecord:
        """
        Build a TradeRecord for a position close (exit). Closes don't have
        a RiskDecision — they come from the strategy exit signal directly.
        """
        realized_bps = 0.0
        if result.avg_fill_price is not None and modeled_price > 0:
            realized_bps = (
                abs(result.avg_fill_price - modeled_price)
                / modeled_price
                * 10_000
            )

        return TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=result.symbol,
            side="sell",
            qty=result.filled_qty,
            avg_fill_price=result.avg_fill_price,
            order_id=result.order_id,
            strategy=strategy_name,
            reason="exit signal",
            stop_price=0.0,
            entry_reference_price=modeled_price,
            modeled_slippage_bps=0.0,
            realized_slippage_bps=round(realized_bps, 2),
            order_type="market",
            status=result.status.value,
            requested_qty=result.requested_qty,
            filled_qty=result.filled_qty,
        )

    def log(self, record: TradeRecord) -> None:
        """Insert one trade record into the database."""
        conn = self._ensure_db()
        d = record.as_dict()
        columns = ", ".join(d.keys())
        placeholders = ", ".join(["?"] * len(d))
        conn.execute(
            f"INSERT INTO trades ({columns}) VALUES ({placeholders})",
            list(d.values()),
        )
        conn.commit()
        logger.info(
            f"trade logged: {record.side} {record.qty} {record.symbol} "
            f"@ ${record.avg_fill_price} [{record.strategy}] "
            f"slip={record.realized_slippage_bps}bps"
        )

    def read_all(self) -> list[dict]:
        """Read all trade records from the database. For reporting, not hot path."""
        if not os.path.exists(self._path):
            return []
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT {', '.join(TRADE_COLUMNS)} FROM trades ORDER BY id"
        )
        return [dict(row) for row in cursor.fetchall()]

    def read_trades_in_range(
        self, start_date: str, end_date: str
    ) -> list[dict]:
        """Read trades whose timestamp falls within [start_date, end_date] (date prefix match)."""
        if not os.path.exists(self._path):
            return []
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT {', '.join(TRADE_COLUMNS)} FROM trades "
            "WHERE substr(timestamp, 1, 10) >= ? AND substr(timestamp, 1, 10) <= ? "
            "ORDER BY id",
            (start_date, end_date),
        )
        return [dict(row) for row in cursor.fetchall()]

    def read_recent(self, last_n: int) -> list[dict]:
        """Read the last N trade records."""
        if not os.path.exists(self._path):
            return []
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            f"SELECT {', '.join(TRADE_COLUMNS)} FROM trades ORDER BY id DESC LIMIT ?",
            (last_n,),
        )
        rows = [dict(row) for row in cursor.fetchall()]
        rows.reverse()  # Return in chronological order
        return rows

    def log_external_close(self, *, symbol: str, strategy: str, reason: str) -> None:
        """
        Write a synthetic sell record when a position disappears externally
        (stop-out, manual liquidation, margin call, etc.) without the bot
        placing the closing order.

        Fill price is None because we don't know the exact execution price.
        The reason field carries the detection context.
        """
        record = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            side="sell",
            qty=0,
            avg_fill_price=None,
            order_id=None,
            strategy=strategy,
            reason=reason,
            stop_price=0.0,
            entry_reference_price=0.0,
            modeled_slippage_bps=0.0,
            realized_slippage_bps=0.0,
            order_type="unknown",
            status="filled",
            requested_qty=0,
            filled_qty=0,
        )
        self.log(record)

    def read_owner_for_symbol(self, symbol: str) -> str | None:
        """
        Return the strategy_name that owns the currently-open position for
        `symbol` per the trade log, or None if the position is closed (or
        never traded).

        The trade log is append-only and allows at most one open position per
        symbol at a time. The most recent filled/partial row determines state:
        - side='buy'  → position open, return strategy name
        - side='sell' → position closed, return None
        """
        if not os.path.exists(self._path):
            return None
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT side, strategy "
            "FROM trades "
            "WHERE symbol = ? "
            "AND status IN ('filled', 'partial') "
            "ORDER BY id DESC LIMIT 1",
            (symbol,),
        )
        row = cursor.fetchone()
        if row is None or row["side"] != "buy":
            return None
        return row["strategy"]

    def read_all_open_owners(self) -> dict[str, str]:
        """
        Return ``{symbol: strategy_name}`` for every symbol whose most recent
        filled/partial trade is a ``'buy'``.

        This is the trade log's view of currently-open positions and their
        owning strategies. Used by the engine on startup to restore durable
        ownership without guessing from slot order.
        """
        if not os.path.exists(self._path):
            return {}
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT symbol, strategy, side "
            "FROM trades "
            "WHERE id IN ("
            "  SELECT MAX(id) FROM trades "
            "  WHERE status IN ('filled', 'partial') "
            "  GROUP BY symbol"
            ") "
            "AND side = 'buy'"
        )
        return {row["symbol"]: row["strategy"] for row in cursor.fetchall()}

    def read_latest_open_stop_price(
        self, *, symbol: str, strategy: str
    ) -> float | None:
        """
        Return the original fixed stop price for the latest still-open trade
        on `symbol` owned by `strategy`.

        The trade log is append-only and this engine allows at most one open
        position per symbol. That means the latest filled/partial row for a
        symbol/strategy pair tells us whether the trade is still open:

        - latest row is `buy` with `stop_price > 0` -> position should still
          be open, so return that original stop
        - latest row is `sell` -> position was closed, return None
        """
        if not os.path.exists(self._path):
            return None
        conn = self._ensure_db()
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT side, stop_price "
            "FROM trades "
            "WHERE symbol = ? AND strategy = ? "
            "AND status IN ('filled', 'partial') "
            "ORDER BY id DESC LIMIT 1",
            (symbol, strategy),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        if row["side"] != "buy":
            return None
        stop_price = float(row["stop_price"] or 0.0)
        return stop_price if stop_price > 0 else None
