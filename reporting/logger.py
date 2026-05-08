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
    "initial_stop_loss",
    "initial_risk_per_share",
    "initial_risk_dollars",
    "realized_pnl",
    "r_multiple",
    "entry_timestamp",
    "exit_timestamp",
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
    filled_qty            REAL,
    initial_stop_loss     REAL,
    initial_risk_per_share REAL,
    initial_risk_dollars  REAL,
    realized_pnl          REAL,
    r_multiple            REAL,
    entry_timestamp       TEXT,
    exit_timestamp        TEXT
);
"""

_MIGRATION_COLUMNS = {
    "initial_stop_loss": "REAL",
    "initial_risk_per_share": "REAL",
    "initial_risk_dollars": "REAL",
    "realized_pnl": "REAL",
    "r_multiple": "REAL",
    "entry_timestamp": "TEXT",
    "exit_timestamp": "TEXT",
}


# ── Trade record ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TradeRecord:
    """One row in the trades table — built from a RiskDecision + OrderResult."""

    timestamp: str
    symbol: str
    side: str
    qty: float
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
    requested_qty: float
    filled_qty: float
    initial_stop_loss: float | None = None
    initial_risk_per_share: float | None = None
    initial_risk_dollars: float | None = None
    realized_pnl: float | None = None
    r_multiple: float | None = None
    entry_timestamp: str | None = None
    exit_timestamp: str | None = None

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
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        for column, col_type in _MIGRATION_COLUMNS.items():
            if column not in existing:
                self._conn.execute(
                    f"ALTER TABLE trades ADD COLUMN {column} {col_type}"
                )
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
        initial_stop_loss = float(decision.stop_price)
        initial_risk_per_share = max(
            0.0,
            float(decision.entry_reference_price) - float(decision.stop_price),
        )
        initial_risk_dollars = (
            initial_risk_per_share * float(result.filled_qty or result.requested_qty or 0)
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        if (
            result.avg_fill_price is not None
            and ref_price > 0
        ):
            realized_bps = (
                abs(result.avg_fill_price - ref_price) / ref_price * 10_000
            )

        return TradeRecord(
            timestamp=now_iso,
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
            initial_stop_loss=initial_stop_loss,
            initial_risk_per_share=initial_risk_per_share,
            initial_risk_dollars=initial_risk_dollars,
            realized_pnl=None,
            r_multiple=None,
            entry_timestamp=now_iso,
            exit_timestamp=None,
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
        context = self._read_latest_open_entry_context(
            symbol=result.symbol,
            strategy=strategy_name,
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        if result.avg_fill_price is not None and modeled_price > 0:
            realized_bps = (
                abs(result.avg_fill_price - modeled_price)
                / modeled_price
                * 10_000
            )
        realized_pnl = None
        r_multiple = None
        initial_stop_loss = None
        initial_risk_per_share = None
        initial_risk_dollars = None
        entry_timestamp = None
        if context is not None:
            initial_stop_loss = context["initial_stop_loss"]
            initial_risk_per_share = context["initial_risk_per_share"]
            entry_timestamp = context["entry_timestamp"]
            if initial_risk_per_share is not None:
                initial_risk_dollars = (
                    float(initial_risk_per_share) * float(result.filled_qty or 0)
                )
            if (
                result.avg_fill_price is not None
                and context["entry_reference_price"] is not None
            ):
                realized_pnl = (
                    (float(result.avg_fill_price) - float(context["entry_reference_price"]))
                    * float(result.filled_qty or 0)
                )
                if initial_risk_dollars and initial_risk_dollars > 0:
                    r_multiple = realized_pnl / initial_risk_dollars

        return TradeRecord(
            timestamp=now_iso,
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
            initial_stop_loss=initial_stop_loss,
            initial_risk_per_share=initial_risk_per_share,
            initial_risk_dollars=initial_risk_dollars,
            realized_pnl=realized_pnl,
            r_multiple=r_multiple,
            entry_timestamp=entry_timestamp,
            exit_timestamp=now_iso,
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
            initial_stop_loss=None,
            initial_risk_per_share=None,
            initial_risk_dollars=None,
            realized_pnl=None,
            r_multiple=None,
            entry_timestamp=None,
            exit_timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.log(record)

    def log_stop_fill(
        self,
        *,
        symbol: str,
        strategy: str,
        qty: float,
        avg_fill_price: float,
        order_id: str | None = None,
    ) -> None:
        """
        Write a confirmed stop-fill record when the WebSocket stream delivers
        an exact bracket stop execution (price and qty known).

        Distinct from log_external_close, which is the fallback for positions
        that disappear without a confirmed fill event.
        """
        context = self._read_latest_open_entry_context(
            symbol=symbol,
            strategy=strategy,
        )
        now_iso = datetime.now(timezone.utc).isoformat()
        initial_stop_loss = None
        initial_risk_per_share = None
        initial_risk_dollars = None
        realized_pnl = None
        r_multiple = None
        entry_timestamp = None
        entry_reference_price = 0.0
        if context is not None:
            initial_stop_loss = context["initial_stop_loss"]
            initial_risk_per_share = context["initial_risk_per_share"]
            entry_timestamp = context["entry_timestamp"]
            entry_reference_price = float(context["entry_reference_price"] or 0.0)
            if initial_risk_per_share is not None:
                initial_risk_dollars = float(initial_risk_per_share) * qty
            if entry_reference_price > 0:
                realized_pnl = (avg_fill_price - entry_reference_price) * qty
                if initial_risk_dollars and initial_risk_dollars > 0:
                    r_multiple = realized_pnl / initial_risk_dollars

        record = TradeRecord(
            timestamp=now_iso,
            symbol=symbol,
            side="sell",
            qty=qty,
            avg_fill_price=avg_fill_price,
            order_id=order_id,
            strategy=strategy,
            reason="stop_triggered",
            stop_price=avg_fill_price,
            entry_reference_price=entry_reference_price,
            modeled_slippage_bps=0.0,
            realized_slippage_bps=0.0,
            order_type="stop",
            status="filled",
            requested_qty=qty,
            filled_qty=qty,
            initial_stop_loss=initial_stop_loss,
            initial_risk_per_share=initial_risk_per_share,
            initial_risk_dollars=initial_risk_dollars,
            realized_pnl=realized_pnl,
            r_multiple=r_multiple,
            entry_timestamp=entry_timestamp,
            exit_timestamp=now_iso,
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
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return None
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
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return {}
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
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return None
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

    def _read_latest_open_entry_context(
        self, *, symbol: str, strategy: str
    ) -> dict | None:
        """Return the latest still-open entry context for symbol/strategy."""
        if not os.path.exists(self._path):
            return None
        try:
            conn = self._ensure_db()
        except sqlite3.Error:
            return None
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT side, entry_reference_price, initial_stop_loss, "
            "initial_risk_per_share, entry_timestamp "
            "FROM trades "
            "WHERE symbol = ? AND strategy = ? "
            "AND status IN ('filled', 'partial') "
            "ORDER BY id DESC LIMIT 1",
            (symbol, strategy),
        )
        row = cursor.fetchone()
        if row is None or row["side"] != "buy":
            return None
        return {
            "entry_reference_price": (
                float(row["entry_reference_price"])
                if row["entry_reference_price"] is not None else None
            ),
            "initial_stop_loss": (
                float(row["initial_stop_loss"])
                if row["initial_stop_loss"] is not None else None
            ),
            "initial_risk_per_share": (
                float(row["initial_risk_per_share"])
                if row["initial_risk_per_share"] is not None else None
            ),
            "entry_timestamp": row["entry_timestamp"],
        }
