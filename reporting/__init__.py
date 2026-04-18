"""Trade reporting and P&L (Phase 9).

Provides:
  - `TradeLogger` — append-only trade CSV + structured JSON loguru sink.
  - `PnLTracker` — daily/weekly P&L summaries with per-strategy attribution.
  - `AlertDispatcher` — operator alerts with pluggable backends.
"""

from reporting.alerts import (
    Alert,
    AlertBackend,
    AlertDispatcher,
    AlertSeverity,
    AlertType,
    LogFileBackend,
)
from reporting.logger import TradeLogger, TradeRecord, install_json_sink
from reporting.pnl import DailySummary, PnLTracker, StrategyStats

__all__ = [
    "Alert",
    "AlertBackend",
    "AlertDispatcher",
    "AlertSeverity",
    "AlertType",
    "DailySummary",
    "LogFileBackend",
    "PnLTracker",
    "StrategyStats",
    "TradeLogger",
    "TradeRecord",
    "install_json_sink",
]
