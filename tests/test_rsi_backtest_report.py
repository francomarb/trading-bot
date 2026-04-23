"""
Unit tests for scripts/rsi_backtest_report.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from backtest.runner import BacktestConfig
from scripts.rsi_backtest_report import SymbolBacktest, render_report
from scripts.rsi_candidate_validate import ValidationConfig


def _row() -> SymbolBacktest:
    stats = {
        "trade_count": 3.0,
        "total_return": 0.25,
        "cagr": 0.05,
        "sharpe": 0.7,
        "sortino": 1.1,
        "max_drawdown": -0.2,
        "max_dd_days": 120.0,
        "win_rate": 0.67,
        "profit_factor": 2.5,
        "expectancy": 1234.56,
        "final_equity": 125000.0,
    }
    return SymbolBacktest(
        symbol="ALLY",
        group="promoted",
        bars=1000,
        start=pd.Timestamp("2021-01-01", tz="UTC"),
        end=pd.Timestamp("2025-01-01", tz="UTC"),
        result=SimpleNamespace(stats=stats),
        buy_hold_return=0.10,
        event_count=8,
        event_hit_rate=0.50,
        avg_event_return=0.03,
        stop_failures=1,
        chart_path=Path("logs/rsi_backtests/chart.png"),
    )


class TestRenderReport:
    def test_includes_summary_and_stop_caveat(self):
        report = render_report(
            [_row()],
            feed="sip",
            start=datetime(2021, 1, 1, tzinfo=timezone.utc),
            end=datetime(2025, 1, 1, tzinfo=timezone.utc),
            promoted=["ALLY"],
            comparisons=[],
            backtest_config=BacktestConfig(),
            validation_config=ValidationConfig(),
        )

        assert "RSI Backtest Report" in report
        assert "| promoted | ALLY |" in report
        assert "ATR stop counts are contextual event diagnostics" in report
        assert "Average per-symbol strategy return: 25.0%" in report
