"""
Trade logging and structured JSON sink (Phase 9).

Two responsibilities:

1. **Structured JSON log sink** — adds a loguru sink that writes JSONL
   alongside the human-readable console output. Every log line becomes a
   machine-parseable record for downstream monitoring.

2. **TradeLogger** — appends a row to the trade CSV after every fill. The
   CSV is the append-only audit trail of every order the bot placed, with
   slippage data baked in.

Design principles:
  - The CSV schema is fixed at write time; reads happen in `reporting/pnl.py`
    and in operator tools — never in the hot path.
  - All writes are append-only. We never rewrite the file.
  - `ensure_dirs` is called lazily on first write, not at import time, so
    tests that never write don't need the filesystem.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

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


# ── Trade CSV ───────────────────────────────────────────────────────────────

TRADE_CSV_COLUMNS = [
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


@dataclass(frozen=True)
class TradeRecord:
    """One row in the trade CSV — built from a RiskDecision + OrderResult."""

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
        """Column-ordered dict for csv.DictWriter."""
        return {col: getattr(self, col) for col in TRADE_CSV_COLUMNS}


class TradeLogger:
    """
    Appends trade records to a CSV file.

    Usage (from engine):
        trade_logger = TradeLogger()
        record = trade_logger.build_record(decision, result)
        trade_logger.log(record)
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or settings.TRADE_LOG_CSV
        self._initialized = False

    def _ensure_file(self) -> None:
        """Create the CSV with a header row if it doesn't exist yet."""
        if self._initialized:
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        if not os.path.exists(self._path):
            with open(self._path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS)
                writer.writeheader()
        self._initialized = True

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
        """Append one trade record to the CSV."""
        self._ensure_file()
        with open(self._path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS)
            writer.writerow(record.as_dict())
        logger.info(
            f"trade logged: {record.side} {record.qty} {record.symbol} "
            f"@ ${record.avg_fill_price} [{record.strategy}] "
            f"slip={record.realized_slippage_bps}bps"
        )

    def read_all(self) -> list[dict]:
        """Read all trade records from the CSV. For reporting, not hot path."""
        if not os.path.exists(self._path):
            return []
        with open(self._path, newline="") as f:
            return list(csv.DictReader(f))
