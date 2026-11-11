"""Session-final daily P&L scheduling and catch-up behavior."""

from __future__ import annotations

from datetime import date, datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from reporting.daily_scheduler import DailyPnLReportScheduler
from reporting.logger import TradeLogger, TradeRecord
from reporting.pnl import PnLTracker


def _tracker(tmp_path) -> PnLTracker:
    db = tmp_path / "trades.db"
    tl = TradeLogger(path=str(db))
    tl.log(TradeRecord(
        timestamp="2026-11-11T20:00:00+00:00",
        symbol="AAPL", side="sell", qty=1, avg_fill_price=100.0,
        order_id="close-1", strategy="sma_crossover", reason="exit",
        stop_price=0.0, entry_reference_price=100.0,
        modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
        order_type="market", status="filled", requested_qty=1,
        filled_qty=1, realized_pnl=125.0,
        entry_timestamp="2026-11-10T15:00:00+00:00",
        exit_timestamp="2026-11-11T20:00:00+00:00",
        position_type="single_leg", position_uid="pos_" + "b" * 32,
    ))
    return PnLTracker(
        trade_logger=tl,
        daily_pnl_dir=str(tmp_path / "daily"),
        weekly_report_dir=str(tmp_path / "weekly"),
    )


def _scheduler(tmp_path, *, now: datetime) -> DailyPnLReportScheduler:
    tracker = _tracker(tmp_path)
    broker = MagicMock()
    broker.get_intraday_equity_path.return_value = [100_000.0, 100_500.0, 99_900.0]
    engine = SimpleNamespace(
        _last_snapshot=SimpleNamespace(
            account=SimpleNamespace(open_positions={
                "AAPL": SimpleNamespace(symbol="AAPL", unrealized_pl=50.0),
            })
        )
    )
    return DailyPnLReportScheduler(
        tracker=tracker,
        broker=broker,
        engine=engine,
        dispatcher=MagicMock(),
        clock=lambda: now,
    )


def test_current_session_is_not_finalized_before_regular_close(
    tmp_path, monkeypatch,
):
    scheduler = _scheduler(
        tmp_path, now=datetime(2026, 11, 11, 20, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        "reporting.daily_scheduler.market_calendar.trading_sessions",
        lambda start, end: set(),
    )

    scheduler()

    assert not scheduler.tracker.is_daily_report_final("2026-11-11")
    scheduler.broker.get_intraday_equity_path.assert_not_called()
    scheduler.dispatcher.eod_summary.assert_not_called()


def test_after_close_finalizes_once_from_db_and_broker_history(
    tmp_path, monkeypatch,
):
    scheduler = _scheduler(
        tmp_path, now=datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        "reporting.daily_scheduler.market_calendar.trading_sessions",
        lambda start, end: {date(2026, 11, 11)},
    )

    scheduler()
    scheduler()

    path = tmp_path / "daily" / "2026-11-11.md"
    content = path.read_text()
    assert "> Finalized after market close" in content
    assert "| Realized P&L | $125.00 |" in content
    assert "| Total trades | 1 |" in content
    assert "| Unrealized P&L | $50.00 |" in content
    assert "| Max intraday drawdown | $600.00 |" in content
    assert scheduler.broker.get_intraday_equity_path.call_count == 1
    start, end = scheduler.broker.get_intraday_equity_path.call_args.args
    assert start == datetime(2026, 11, 11, 5, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 12, 5, 0, tzinfo=timezone.utc)
    scheduler.dispatcher.eod_summary.assert_called_once_with(
        daily_pnl=125.0, trade_count=1, win_rate=1.0,
    )


def test_current_session_keeps_larger_locally_observed_drawdown(
    tmp_path, monkeypatch,
):
    scheduler = _scheduler(
        tmp_path, now=datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc),
    )
    scheduler.engine.max_intraday_drawdown = 900.0
    monkeypatch.setattr(
        "reporting.daily_scheduler.market_calendar.trading_sessions",
        lambda start, end: {date(2026, 11, 11)},
    )

    scheduler()

    content = (tmp_path / "daily" / "2026-11-11.md").read_text()
    assert "| Max intraday drawdown | $900.00 |" in content


def test_next_morning_backfill_does_not_claim_current_unrealized_mark(
    tmp_path, monkeypatch,
):
    scheduler = _scheduler(
        tmp_path, now=datetime(2026, 11, 12, 14, 0, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        "reporting.daily_scheduler.market_calendar.trading_sessions",
        lambda start, end: {date(2026, 11, 11)},
    )

    scheduler()

    content = (tmp_path / "daily" / "2026-11-11.md").read_text()
    assert "| Unrealized P&L | — (unavailable) |" in content


def test_stale_shutdown_report_is_replaced_by_final_report(
    tmp_path, monkeypatch,
):
    scheduler = _scheduler(
        tmp_path, now=datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc),
    )
    stale = scheduler.tracker.generate_daily_summary(day="2026-11-11")
    scheduler.tracker.write_daily_report(stale)
    monkeypatch.setattr(
        "reporting.daily_scheduler.market_calendar.trading_sessions",
        lambda start, end: {date(2026, 11, 11)},
    )

    scheduler()

    assert scheduler.tracker.is_daily_report_final("2026-11-11")


def test_broker_history_failure_stays_pending_for_retry(tmp_path, monkeypatch):
    scheduler = _scheduler(
        tmp_path, now=datetime(2026, 11, 11, 22, 0, tzinfo=timezone.utc),
    )
    scheduler.broker.get_intraday_equity_path.return_value = []
    monkeypatch.setattr(
        "reporting.daily_scheduler.market_calendar.trading_sessions",
        lambda start, end: {date(2026, 11, 11)},
    )

    scheduler()

    assert not scheduler.tracker.is_daily_report_final("2026-11-11")
    scheduler.dispatcher.eod_summary.assert_not_called()
