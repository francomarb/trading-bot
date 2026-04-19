"""
Unit tests for the Phase 9 reporting module.

Covers:
  - TradeLogger: SQLite creation, record building, append, read-back
  - PnLTracker: daily summary, per-strategy attribution, intraday drawdown,
    slippage stats, weekly report generation
  - AlertDispatcher: fire, cooldown suppression, backend failure isolation
  - install_json_sink: creates the sink without crashing
  - Engine integration: _log_entry / _log_close wiring
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from execution.broker import OrderResult, OrderStatus
from reporting.alerts import (
    Alert,
    AlertBackend,
    AlertDispatcher,
    AlertSeverity,
    AlertType,
    LogFileBackend,
)
from reporting.logger import (
    TRADE_CSV_COLUMNS,
    TradeLogger,
    TradeRecord,
    install_json_sink,
)
from reporting.pnl import DailySummary, PnLTracker, StrategyStats
from risk.manager import RiskDecision, Side
from strategies.base import OrderType


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_csv(tmp_path: Path) -> str:
    return str(tmp_path / "trades.db")


@pytest.fixture
def tmp_daily_dir(tmp_path: Path) -> str:
    return str(tmp_path / "daily_pnl")


@pytest.fixture
def tmp_weekly_dir(tmp_path: Path) -> str:
    return str(tmp_path / "weekly_reports")


@pytest.fixture
def sample_decision() -> RiskDecision:
    return RiskDecision(
        symbol="AAPL",
        side=Side.BUY,
        qty=10,
        entry_reference_price=150.0,
        stop_price=145.0,
        strategy_name="sma_crossover",
        reason="test entry",
        order_type=OrderType.MARKET,
    )


@pytest.fixture
def sample_result() -> OrderResult:
    return OrderResult(
        status=OrderStatus.FILLED,
        order_id="abc123",
        symbol="AAPL",
        requested_qty=10,
        filled_qty=10,
        avg_fill_price=150.05,
        raw_status="filled",
        message="filled 10 @ 150.05",
    )


# ── TestTradeLogger ─────────────────────────────────────────────────────────


class TestTradeLogger:
    def test_db_created_with_trades_table(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)
        tl.log(record)

        conn = sqlite3.connect(tmp_csv)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_record_fields(self, sample_decision, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)

        assert record.symbol == "AAPL"
        assert record.side == "buy"
        assert record.qty == 10
        assert record.avg_fill_price == 150.05
        assert record.order_id == "abc123"
        assert record.strategy == "sma_crossover"
        assert record.stop_price == 145.0
        assert record.entry_reference_price == 150.0
        assert record.order_type == "market"
        assert record.status == "filled"

    def test_slippage_calculation(self, sample_decision, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)
        # |150.05 - 150.0| / 150.0 * 10_000 = 3.33 bps
        assert record.modeled_slippage_bps == 0.0
        assert abs(record.realized_slippage_bps - 3.33) < 0.1

    def test_append_multiple_records(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        for _ in range(3):
            record = tl.build_record(sample_decision, sample_result)
            tl.log(record)
        rows = tl.read_all()
        assert len(rows) == 3

    def test_read_all_empty(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        assert tl.read_all() == []

    def test_build_close_record(self, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_close_record(
            sample_result, strategy_name="sma_crossover", modeled_price=150.0
        )
        assert record.side == "sell"
        assert record.strategy == "sma_crossover"
        assert record.reason == "exit signal"
        assert record.stop_price == 0.0

    def test_as_dict_has_all_columns(self, sample_decision, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, sample_result)
        d = record.as_dict()
        assert set(d.keys()) == set(TRADE_CSV_COLUMNS)

    def test_no_fill_price_zero_slippage(self, sample_decision):
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="x",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=None,
            raw_status="filled",
        )
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, result, modeled_price=150.0)
        assert record.realized_slippage_bps == 0.0

    def test_read_trades_in_range(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        # Log 3 records — all will have today's timestamp
        for _ in range(3):
            record = tl.build_record(sample_decision, sample_result)
            tl.log(record)
        today = record.timestamp[:10]
        rows = tl.read_trades_in_range(today, today)
        assert len(rows) == 3
        # Out-of-range returns nothing
        assert tl.read_trades_in_range("1999-01-01", "1999-12-31") == []

    def test_read_recent(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        for _ in range(5):
            record = tl.build_record(sample_decision, sample_result)
            tl.log(record)
        recent = tl.read_recent(3)
        assert len(recent) == 3

    def test_close_and_reopen(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(sample_decision, sample_result)
        tl.log(record)
        tl.close()
        # After close, read_all should still work (reconnects lazily)
        tl2 = TradeLogger(path=tmp_csv)
        assert len(tl2.read_all()) == 1


# ── TestPnLTracker ──────────────────────────────────────────────────────────


class TestPnLTracker:
    def test_record_trade_pnl(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("sma_crossover", 100.0, today="2026-04-16")
        tracker.record_trade_pnl("sma_crossover", -50.0, today="2026-04-16")

        summary = tracker.generate_daily_summary(day="2026-04-16")
        assert summary.total_trades == 2
        assert summary.realized_pnl == 50.0
        assert summary.largest_win == 100.0
        assert summary.largest_loss == -50.0

    def test_per_strategy_attribution(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("strat_a", 200.0, today="2026-04-16")
        tracker.record_trade_pnl("strat_a", -80.0, today="2026-04-16")
        tracker.record_trade_pnl("strat_b", 50.0, today="2026-04-16")

        summary = tracker.generate_daily_summary(day="2026-04-16")
        assert "strat_a" in summary.strategies
        assert "strat_b" in summary.strategies

        a = summary.strategies["strat_a"]
        assert a.trade_count == 2
        assert a.total_pnl == 120.0
        assert a.wins == 1
        assert a.losses == 1
        assert a.win_rate == 0.5
        assert a.expectancy == 60.0

        b = summary.strategies["strat_b"]
        assert b.trade_count == 1
        assert b.profit_factor == float("inf")  # no losses

    def test_intraday_drawdown(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("s", 100.0, today="2026-04-16")
        tracker.record_trade_pnl("s", -150.0, today="2026-04-16")  # peak 100, now -50
        tracker.record_trade_pnl("s", 80.0, today="2026-04-16")   # now 30

        summary = tracker.generate_daily_summary(day="2026-04-16")
        # Max drawdown from peak=100 to trough=-50 = 150
        assert summary.max_intraday_drawdown == 150.0

    def test_day_reset(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("s", 100.0, today="2026-04-16")
        tracker.record_trade_pnl("s", -200.0, today="2026-04-17")  # new day

        summary = tracker.generate_daily_summary(day="2026-04-17")
        assert summary.total_trades == 1
        assert summary.realized_pnl == -200.0
        assert summary.max_intraday_drawdown == 200.0

    def test_write_daily_report(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("sma_crossover", 100.0, today="2026-04-16")

        summary = tracker.generate_daily_summary(
            day="2026-04-16",
            session_start_equity=100_000.0,
            session_end_equity=100_100.0,
        )
        path = tracker.write_daily_report(summary)

        assert os.path.exists(path)
        content = open(path).read()
        assert "Daily P&L" in content
        assert "sma_crossover" in content
        assert "$100.00" in content

    def test_no_trades_summary(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        summary = tracker.generate_daily_summary(day="2026-04-16")
        assert summary.total_trades == 0
        assert summary.realized_pnl == 0.0

    def test_slippage_report_empty(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        report = tracker.slippage_report()
        assert report["count"] == 0
        assert report["mean_bps"] == 0.0

    def test_slippage_report_from_db(self, tmp_csv, tmp_daily_dir, sample_decision, sample_result):
        # Write some trade records to the database first.
        tl = TradeLogger(path=tmp_csv)
        for _ in range(5):
            record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)
            tl.log(record)

        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        report = tracker.slippage_report(last_n=5)
        assert report["count"] == 5
        assert report["mean_bps"] > 0

    def test_weekly_report_no_dailies(self, tmp_csv, tmp_daily_dir, tmp_weekly_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv,
            daily_pnl_dir=tmp_daily_dir,
            weekly_report_dir=tmp_weekly_dir,
        )
        result = tracker.generate_weekly_report(week_end="2026-04-16")
        assert result is None

    def test_weekly_report_with_dailies(self, tmp_csv, tmp_daily_dir, tmp_weekly_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv,
            daily_pnl_dir=tmp_daily_dir,
            weekly_report_dir=tmp_weekly_dir,
        )
        # Create a daily report first.
        tracker.record_trade_pnl("s", 100.0, today="2026-04-16")
        summary = tracker.generate_daily_summary(day="2026-04-16")
        tracker.write_daily_report(summary)

        path = tracker.generate_weekly_report(week_end="2026-04-16")
        assert path is not None
        assert os.path.exists(path)
        content = open(path).read()
        assert "Weekly Report" in content
        assert "2026-04-16" in content


# ── TestStrategyStats ───────────────────────────────────────────────────────


class TestStrategyStats:
    def test_win_rate(self):
        s = StrategyStats(strategy_name="x", trade_count=4, wins=3, losses=1)
        assert s.win_rate == 0.75

    def test_expectancy(self):
        s = StrategyStats(strategy_name="x", trade_count=2, total_pnl=100.0)
        assert s.expectancy == 50.0

    def test_profit_factor_with_losses(self):
        s = StrategyStats(
            strategy_name="x",
            trade_count=3,
            trade_pnls=[100.0, 50.0, -30.0],
        )
        assert abs(s.profit_factor - 5.0) < 0.01  # 150/30

    def test_profit_factor_no_losses(self):
        s = StrategyStats(
            strategy_name="x",
            trade_count=2,
            trade_pnls=[100.0, 50.0],
        )
        assert s.profit_factor == float("inf")

    def test_profit_factor_no_trades(self):
        s = StrategyStats(strategy_name="x")
        assert s.profit_factor == 0.0


# ── TestAlertDispatcher ─────────────────────────────────────────────────────


class _CollectorBackend(AlertBackend):
    """Test backend that collects alerts in a list."""

    def __init__(self):
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class TestAlertDispatcher:
    def test_fire_sends_to_backend(self):
        backend = _CollectorBackend()
        dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=0)
        alert = Alert(
            alert_type=AlertType.ORDER_REJECTION,
            severity=AlertSeverity.WARNING,
            message="test",
            symbol="AAPL",
        )
        sent = dispatcher.fire(alert)
        assert sent is True
        assert len(backend.alerts) == 1
        assert backend.alerts[0].message == "test"

    def test_cooldown_suppression(self):
        backend = _CollectorBackend()
        dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=600)
        alert = Alert(
            alert_type=AlertType.ORDER_REJECTION,
            severity=AlertSeverity.WARNING,
            message="test",
            symbol="AAPL",
        )
        assert dispatcher.fire(alert) is True
        assert dispatcher.fire(alert) is False  # suppressed
        assert len(backend.alerts) == 1

    def test_different_symbols_not_suppressed(self):
        backend = _CollectorBackend()
        dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=600)
        a1 = Alert(
            alert_type=AlertType.STALE_DATA,
            severity=AlertSeverity.WARNING,
            message="stale",
            symbol="AAPL",
        )
        a2 = Alert(
            alert_type=AlertType.STALE_DATA,
            severity=AlertSeverity.WARNING,
            message="stale",
            symbol="MSFT",
        )
        assert dispatcher.fire(a1) is True
        assert dispatcher.fire(a2) is True
        assert len(backend.alerts) == 2

    def test_backend_failure_does_not_raise(self):
        class _FailingBackend(AlertBackend):
            def send(self, alert):
                raise RuntimeError("boom")

        dispatcher = AlertDispatcher(
            backends=[_FailingBackend()], cooldown_seconds=0
        )
        alert = Alert(
            alert_type=AlertType.BROKER_ERROR,
            severity=AlertSeverity.WARNING,
            message="test",
        )
        # Should not raise.
        sent = dispatcher.fire(alert)
        assert sent is True

    def test_convenience_methods(self):
        backend = _CollectorBackend()
        dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=0)

        dispatcher.order_rejection("AAPL", "sma", "halted", "halted")
        dispatcher.circuit_breaker("daily loss limit")
        dispatcher.stale_data("MSFT", "2 days old")
        dispatcher.broker_error("timeout")
        dispatcher.engine_halt("hard dollar cap")
        dispatcher.slippage_drift(15.0, 3.0)
        dispatcher.loss_streak_cooldown("sma", 3, 24.0)

        assert len(backend.alerts) == 7
        types = {a.alert_type for a in backend.alerts}
        assert AlertType.ORDER_REJECTION in types
        assert AlertType.CIRCUIT_BREAKER in types
        assert AlertType.STALE_DATA in types
        assert AlertType.BROKER_ERROR in types
        assert AlertType.ENGINE_HALT in types
        assert AlertType.SLIPPAGE_DRIFT in types
        assert AlertType.LOSS_STREAK_COOLDOWN in types

    def test_alert_format(self):
        alert = Alert(
            alert_type=AlertType.ORDER_REJECTION,
            severity=AlertSeverity.WARNING,
            message="order rejected",
            symbol="AAPL",
            strategy="sma",
            details={"code": "halted"},
        )
        fmt = alert.format()
        assert "[WARNING]" in fmt
        assert "[order_rejection]" in fmt
        assert "[AAPL]" in fmt
        assert "[sma]" in fmt
        assert "code=halted" in fmt


# ── TestLogFileBackend ──────────────────────────────────────────────────────


class TestLogFileBackend:
    def test_send_does_not_raise(self, tmp_path):
        path = str(tmp_path / "alerts.log")
        backend = LogFileBackend(path=path)
        alert = Alert(
            alert_type=AlertType.BROKER_ERROR,
            severity=AlertSeverity.WARNING,
            message="test",
        )
        backend.send(alert)  # Should not raise.


# ── TestInstallJsonSink ─────────────────────────────────────────────────────


class TestInstallJsonSink:
    def test_creates_sink(self, tmp_path):
        from loguru import logger as _logger

        path = str(tmp_path / "bot.jsonl")
        sink_id = install_json_sink(path=path)
        assert isinstance(sink_id, int)
        _logger.remove(sink_id)


# ── TestEngineReportingWiring ───────────────────────────────────────────────


class TestEngineReportingWiring:
    """Verify the engine calls reporting hooks correctly."""

    def _make_engine(self, tmp_csv, monkeypatch):
        """Build an engine with mocked broker and reporting wired to tmp."""
        from engine.trader import EngineConfig, TradingEngine
        from risk.manager import RiskManager
        from strategies.base import BaseStrategy, SignalFrame

        class _Strat(BaseStrategy):
            name = "test"
            preferred_order_type = OrderType.MARKET

            def _raw_signals(self, df):
                return SignalFrame(
                    entries=pd.Series(False, index=df.index, dtype=bool),
                    exits=pd.Series(False, index=df.index, dtype=bool),
                )

        broker = MagicMock()
        broker.sync_with_broker.return_value = MagicMock(
            account=MagicMock(
                equity=100_000.0,
                cash=50_000.0,
                session_start_equity=100_000.0,
                open_positions={},
            ),
            open_orders=[],
        )

        config = EngineConfig(
            cycle_interval_seconds=1,
            market_hours_only=False,
        )

        trade_logger = TradeLogger(path=tmp_csv)
        backend = _CollectorBackend()
        alerts = AlertDispatcher(backends=[backend], cooldown_seconds=0)

        engine = TradingEngine(
            strategy=_Strat(),
            symbols=["AAPL"],
            risk=RiskManager(),
            broker=broker,
            config=config,
            trade_logger=trade_logger,
            alerts=alerts,
        )
        return engine, trade_logger, backend

    def test_engine_has_reporting_attributes(self, tmp_csv, monkeypatch):
        engine, tl, backend = self._make_engine(tmp_csv, monkeypatch)
        assert engine.trade_logger is tl
        assert engine.alerts is not None
        assert engine.pnl_tracker is not None

    def test_entry_fill_logged_to_db(self, tmp_csv, monkeypatch, sample_decision, sample_result):
        engine, tl, backend = self._make_engine(tmp_csv, monkeypatch)
        engine._log_entry(sample_decision, sample_result, 150.0)

        rows = tl.read_all()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["side"] == "buy"

    def test_close_fill_logged_to_db(self, tmp_csv, monkeypatch, sample_result):
        engine, tl, backend = self._make_engine(tmp_csv, monkeypatch)
        engine._log_close(sample_result, 150.0)

        rows = tl.read_all()
        assert len(rows) == 1
        assert rows[0]["side"] == "sell"

    def test_non_filled_not_logged(self, tmp_csv, monkeypatch, sample_decision):
        engine, tl, backend = self._make_engine(tmp_csv, monkeypatch)
        rejected_result = OrderResult(
            status=OrderStatus.REJECTED,
            order_id="x",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=0,
            avg_fill_price=None,
            raw_status="rejected",
        )
        engine._log_entry(sample_decision, rejected_result, 150.0)
        assert tl.read_all() == []
