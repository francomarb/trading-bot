"""Trade reporting and P&L (Phase 9).

Provides:
  - `TradeLogger` — append-only SQLite trade log + structured JSON loguru sink.
  - `PnLTracker` — daily/weekly P&L summaries with per-strategy attribution.
  - `MetricsSnapshot` / `compute_metrics` — live go/no-go performance metrics.
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
from reporting.metrics import MetricsSnapshot, compute_metrics
from reporting.pnl import DailySummary, PnLTracker, StrategyStats

__all__ = [
    "Alert",
    "AlertBackend",
    "AlertDispatcher",
    "AlertSeverity",
    "AlertType",
    "DailySummary",
    "LogFileBackend",
    "MetricsSnapshot",
    "PnLTracker",
    "StrategyStats",
    "TradeLogger",
    "TradeRecord",
    "compute_metrics",
    "install_json_sink",
]
