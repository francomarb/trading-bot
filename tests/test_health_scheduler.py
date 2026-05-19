"""
Unit tests for strategies/health/scheduler.py — Sunday/monthly hook
that forward_test.py wires as engine.start(post_cycle_hook=...).

Covers:
  - Sunday → weekly fires; Monday → no fire
  - First of month → monthly fires; non-first → no fire
  - Idempotent on same trigger day (fire once, then short-circuit)
  - Hook never raises into the trading loop (engine's try/except is
    backup; scheduler's own try/except is primary defense)
  - Both weekly + monthly can fire on the same date if Sunday is
    also the 1st of the month (independent state tracking)
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reporting.alerts import AlertDispatcher
from reporting.logger import TradeLogger
from strategies.health.scheduler import HealthReviewScheduler


# A Sunday (weekday=6).
_SUNDAY = datetime(2026, 5, 17, 18, 0, tzinfo=timezone.utc)
# A Monday.
_MONDAY = datetime(2026, 5, 18, 18, 0, tzinfo=timezone.utc)
# First of month (June 1, 2026 is a Monday — also exercises the
# "monthly fires on non-Sunday" case).
_FIRST_OF_MONTH = datetime(2026, 6, 1, 18, 0, tzinfo=timezone.utc)
# Sunday + first of month (Feb 1, 2026 is a Sunday).
_SUNDAY_AND_FIRST = datetime(2026, 2, 1, 18, 0, tzinfo=timezone.utc)


@pytest.fixture
def db_conn(tmp_path: Path):
    logger = TradeLogger(path=str(tmp_path / "trades.db"))
    conn = logger._ensure_db()
    yield conn
    logger.close()


@pytest.fixture
def mock_run_review(monkeypatch):
    """Replace run_review with a MagicMock so tests don't actually
    render reports / dispatch alerts."""
    mock = MagicMock(return_value=(None, []))
    monkeypatch.setattr(
        "strategies.health.scheduler.run_review", mock,
    )
    return mock


def _make_scheduler(db_conn, *, clock_value: datetime):
    """Build a scheduler with a frozen clock for deterministic tests."""
    return HealthReviewScheduler(
        conn_factory=lambda: db_conn,
        dispatcher=AlertDispatcher(),
        clock=lambda: clock_value,
    )


# ── Weekly trigger ────────────────────────────────────────────────────


class TestWeeklyTrigger:
    def test_sunday_fires_weekly(self, db_conn, mock_run_review):
        scheduler = _make_scheduler(db_conn, clock_value=_SUNDAY)
        scheduler()
        assert mock_run_review.call_count == 1
        # Verify the window passed had period_type='weekly'
        args, kwargs = mock_run_review.call_args
        window = args[0] if args else kwargs.get("window")
        # First positional arg might be the window.
        if window is None:
            window = args[0]
        assert window.period_type == "weekly"
        assert window.period_end == _SUNDAY.date()

    def test_monday_does_not_fire(self, db_conn, mock_run_review):
        scheduler = _make_scheduler(db_conn, clock_value=_MONDAY)
        scheduler()
        # Monday is not Sunday, not first of month → no fire
        assert mock_run_review.call_count == 0

    @pytest.mark.parametrize("weekday_offset", [0, 1, 2, 3, 4, 5])
    def test_no_fire_on_non_sunday_non_first(
        self, db_conn, mock_run_review, weekday_offset,
    ):
        # 2026-05-13 is a Wednesday. Walk through Mon-Sat.
        base = datetime(2026, 5, 11, 18, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        clock = base + timedelta(days=weekday_offset)
        # Skip dates that are first of month.
        if clock.day == 1:
            pytest.skip("first of month — covered by monthly tests")
        scheduler = _make_scheduler(db_conn, clock_value=clock)
        scheduler()
        assert mock_run_review.call_count == 0

    def test_sunday_idempotent_within_same_day(
        self, db_conn, mock_run_review,
    ):
        """Multiple cycle hooks on the same Sunday must fire the
        weekly review exactly once."""
        scheduler = _make_scheduler(db_conn, clock_value=_SUNDAY)
        for _ in range(10):
            scheduler()
        assert mock_run_review.call_count == 1

    def test_next_sunday_fires_again(self, db_conn, mock_run_review):
        """The next Sunday DOES fire again — the idempotency tracks
        date, not 'ever fired'."""
        from datetime import timedelta
        scheduler = HealthReviewScheduler(
            conn_factory=lambda: db_conn,
            dispatcher=AlertDispatcher(),
            clock=lambda: _SUNDAY,
        )
        scheduler()
        # Advance the clock to next Sunday by mutating the closure.
        next_sunday = _SUNDAY + timedelta(days=7)
        scheduler.clock = lambda: next_sunday
        scheduler()
        assert mock_run_review.call_count == 2


# ── Monthly trigger ───────────────────────────────────────────────────


class TestMonthlyTrigger:
    def test_first_of_month_fires_monthly(
        self, db_conn, mock_run_review,
    ):
        scheduler = _make_scheduler(db_conn, clock_value=_FIRST_OF_MONTH)
        scheduler()
        # Was June 1 (a Monday — not a Sunday), so only monthly fires.
        assert mock_run_review.call_count == 1
        args, kwargs = mock_run_review.call_args
        window = args[0] if args else kwargs.get("window")
        if window is None:
            window = args[0]
        assert window.period_type == "monthly"

    def test_second_of_month_does_not_fire_monthly(
        self, db_conn, mock_run_review,
    ):
        clock = datetime(2026, 6, 2, 18, 0, tzinfo=timezone.utc)
        scheduler = _make_scheduler(db_conn, clock_value=clock)
        scheduler()
        assert mock_run_review.call_count == 0

    def test_first_of_month_idempotent(
        self, db_conn, mock_run_review,
    ):
        scheduler = _make_scheduler(db_conn, clock_value=_FIRST_OF_MONTH)
        for _ in range(5):
            scheduler()
        assert mock_run_review.call_count == 1


# ── Both fire on Sunday + first ───────────────────────────────────────


class TestBothFireOnOverlap:
    """When the 1st of the month is also a Sunday, BOTH weekly and
    monthly reviews fire (independent state tracking)."""

    def test_sunday_first_fires_both(self, db_conn, mock_run_review):
        scheduler = _make_scheduler(
            db_conn, clock_value=_SUNDAY_AND_FIRST,
        )
        scheduler()
        assert mock_run_review.call_count == 2
        # Inspect both calls — one weekly, one monthly.
        types = []
        for call in mock_run_review.call_args_list:
            args, kwargs = call
            window = args[0] if args else kwargs.get("window")
            types.append(window.period_type)
        assert "weekly" in types
        assert "monthly" in types


# ── Failure tolerance ─────────────────────────────────────────────────


class TestFailureTolerance:
    """The scheduler must absorb reviewer failures — never raise into
    the engine's post_cycle_hook call site."""

    def test_reviewer_exception_does_not_raise(
        self, db_conn, monkeypatch,
    ):
        def _raises(*args, **kwargs):
            raise RuntimeError("simulated reviewer crash")
        monkeypatch.setattr(
            "strategies.health.scheduler.run_review", _raises,
        )
        scheduler = _make_scheduler(db_conn, clock_value=_SUNDAY)
        # Must NOT raise.
        scheduler()

    def test_db_conn_failure_does_not_raise(self, monkeypatch):
        def _bad_factory():
            raise sqlite3.OperationalError("db gone")
        scheduler = HealthReviewScheduler(
            conn_factory=_bad_factory,
            dispatcher=AlertDispatcher(),
            clock=lambda: _SUNDAY,
        )
        # Must NOT raise even though conn_factory fails.
        scheduler()
