"""
Unit tests for strategies/health/scheduler.py — Monday-completed-week
+ first-of-month hook that forward_test.py wires as
engine.start(post_cycle_hook=...).

Covers:
  - Monday → weekly fires; Sunday / Tuesday-through-Saturday → no fire
  - First of month → monthly fires; non-first → no fire
  - Idempotent on same trigger day (fire once, then short-circuit)
  - Weekly window covers the *completed* previous Mon→Mon week (PR #22
    reviewer regression — Sunday-morning fires on in-progress week
    must be impossible)
  - Hook never raises into the trading loop (engine's try/except is
    backup; scheduler's own try/except is primary defense)
  - Both weekly + monthly can fire on the same date if Monday is
    also the 1st of the month (independent state tracking)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from reporting.alerts import AlertDispatcher
from reporting.logger import TradeLogger
from strategies.health.scheduler import HealthReviewScheduler


# A Monday (weekday=0). Weekly review fires here for the Mon→Mon
# completed week ending at this Monday.
_MONDAY = datetime(2026, 5, 18, 0, 30, tzinfo=timezone.utc)
# A Sunday — should NOT fire the weekly review (in-progress week).
_SUNDAY = datetime(2026, 5, 17, 18, 0, tzinfo=timezone.utc)
# A Tuesday — neither weekly nor monthly trigger.
_TUESDAY = datetime(2026, 5, 19, 18, 0, tzinfo=timezone.utc)
# First of month (June 1, 2026 is a Monday — exercises the overlap
# case where BOTH weekly and monthly fire on the same day).
_FIRST_OF_MONTH_AND_MONDAY = datetime(2026, 6, 1, 0, 30, tzinfo=timezone.utc)
# First of month that is NOT a Monday: July 1, 2026 is a Wednesday.
_FIRST_OF_MONTH_MIDWEEK = datetime(2026, 7, 1, 0, 30, tzinfo=timezone.utc)


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
    def test_monday_fires_weekly(self, db_conn, mock_run_review):
        scheduler = _make_scheduler(db_conn, clock_value=_MONDAY)
        scheduler()
        assert mock_run_review.call_count == 1
        # Verify the window passed had period_type='weekly'
        args, kwargs = mock_run_review.call_args
        window = args[0] if args else kwargs.get("window")
        # First positional arg might be the window.
        if window is None:
            window = args[0]
        assert window.period_type == "weekly"
        assert window.period_end == _MONDAY.date()

    def test_monday_window_covers_completed_previous_week(
        self, db_conn, mock_run_review,
    ):
        """PR #22 reviewer regression: the weekly window must cover
        the *completed* previous Mon→Mon week, NOT a Sunday-to-Sunday
        rolling window. Pin period_start = period_end - 7 days =
        previous Monday."""
        scheduler = _make_scheduler(db_conn, clock_value=_MONDAY)
        scheduler()
        args, kwargs = mock_run_review.call_args
        window = args[0] if args else kwargs.get("window")
        # this Monday is 2026-05-18; previous Monday is 2026-05-11
        from datetime import date as _date
        assert window.period_start == _date(2026, 5, 11)
        assert window.period_end == _date(2026, 5, 18)
        # Both bounds must be Mondays (weekday=0).
        assert window.period_start.weekday() == 0
        assert window.period_end.weekday() == 0

    def test_sunday_does_not_fire_weekly(self, db_conn, mock_run_review):
        """PR #22 reviewer regression: Sunday cycles must NOT fire
        the weekly review — that would report on an in-progress
        week and suppress the proper Monday fire."""
        scheduler = _make_scheduler(db_conn, clock_value=_SUNDAY)
        scheduler()
        assert mock_run_review.call_count == 0

    def test_tuesday_does_not_fire(self, db_conn, mock_run_review):
        scheduler = _make_scheduler(db_conn, clock_value=_TUESDAY)
        scheduler()
        assert mock_run_review.call_count == 0

    @pytest.mark.parametrize("weekday_offset", [1, 2, 3, 4, 5, 6])
    def test_no_fire_on_non_monday_non_first(
        self, db_conn, mock_run_review, weekday_offset,
    ):
        # 2026-05-11 is a Monday. Walk through Tue–Sun.
        base = datetime(2026, 5, 11, 18, 0, tzinfo=timezone.utc)
        from datetime import timedelta
        clock = base + timedelta(days=weekday_offset)
        # Skip dates that are first of month (handled by monthly tests).
        if clock.day == 1:
            pytest.skip("first of month — covered by monthly tests")
        scheduler = _make_scheduler(db_conn, clock_value=clock)
        scheduler()
        assert mock_run_review.call_count == 0

    def test_monday_idempotent_within_same_day(
        self, db_conn, mock_run_review,
    ):
        """Multiple cycle hooks on the same Monday must fire the
        weekly review exactly once."""
        scheduler = _make_scheduler(db_conn, clock_value=_MONDAY)
        for _ in range(10):
            scheduler()
        assert mock_run_review.call_count == 1

    def test_next_monday_fires_again(self, db_conn, mock_run_review):
        """The next Monday DOES fire again — idempotency tracks date,
        not 'ever fired'."""
        from datetime import timedelta
        scheduler = HealthReviewScheduler(
            conn_factory=lambda: db_conn,
            dispatcher=AlertDispatcher(),
            clock=lambda: _MONDAY,
        )
        scheduler()
        # Advance the clock to next Monday.
        next_monday = _MONDAY + timedelta(days=7)
        scheduler.clock = lambda: next_monday
        scheduler()
        assert mock_run_review.call_count == 2


# ── Monthly trigger ───────────────────────────────────────────────────


class TestMonthlyTrigger:
    def test_first_of_month_midweek_fires_monthly_only(
        self, db_conn, mock_run_review,
    ):
        """July 1, 2026 is a Wednesday — only monthly should fire."""
        scheduler = _make_scheduler(
            db_conn, clock_value=_FIRST_OF_MONTH_MIDWEEK,
        )
        scheduler()
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
        scheduler = _make_scheduler(
            db_conn, clock_value=_FIRST_OF_MONTH_MIDWEEK,
        )
        for _ in range(5):
            scheduler()
        assert mock_run_review.call_count == 1


# ── Both fire on Monday + first ───────────────────────────────────────


class TestBothFireOnOverlap:
    """When the 1st of the month is also a Monday, BOTH weekly and
    monthly reviews fire (independent state tracking). June 1, 2026
    is a Monday."""

    def test_monday_first_fires_both(self, db_conn, mock_run_review):
        scheduler = _make_scheduler(
            db_conn, clock_value=_FIRST_OF_MONTH_AND_MONDAY,
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
        scheduler = _make_scheduler(db_conn, clock_value=_MONDAY)
        # Must NOT raise.
        scheduler()

    def test_db_conn_failure_does_not_raise(self, monkeypatch):
        def _bad_factory():
            raise sqlite3.OperationalError("db gone")
        scheduler = HealthReviewScheduler(
            conn_factory=_bad_factory,
            dispatcher=AlertDispatcher(),
            clock=lambda: _MONDAY,
        )
        # Must NOT raise even though conn_factory fails.
        scheduler()


class TestWeeklyPnLReport:
    """The Monday trigger also writes the weekly P&L digest.

    `PnLTracker.generate_weekly_report` had existed since Phase 9 and was
    reachable only from tests and phase9_verify — so across ~4 months of
    live operation no weekly P&L report was ever written and
    `logs/weekly_reports/` did not exist. Found 2026-08-03 when the
    operator went looking for a report that had never been produced.
    """

    def _tracker(self):
        t = MagicMock()
        t.generate_weekly_report.return_value = "logs/weekly_reports/w.md"
        return t

    def test_monday_writes_the_weekly_pnl_report(self, db_conn, mock_run_review):
        tracker = self._tracker()
        sched = HealthReviewScheduler(
            conn_factory=lambda: db_conn, dispatcher=AlertDispatcher(),
            clock=lambda: _MONDAY, pnl_tracker=tracker,
        )
        sched()
        tracker.generate_weekly_report.assert_called_once()

    def test_week_ends_sunday_not_the_firing_monday(self, db_conn, mock_run_review):
        """The hook fires *during* Monday's session, so Monday's daily
        file is absent or a zero-trade stub. Ending on Sunday covers the
        fully completed Mon→Sun week and never averages in a partial day.
        """
        tracker = self._tracker()
        sched = HealthReviewScheduler(
            conn_factory=lambda: db_conn, dispatcher=AlertDispatcher(),
            clock=lambda: _MONDAY, pnl_tracker=tracker,
        )
        sched()
        sunday = (_MONDAY.date() - timedelta(days=1)).isoformat()
        tracker.generate_weekly_report.assert_called_once_with(week_end=sunday)

    def test_non_monday_writes_nothing(self, db_conn, mock_run_review):
        tracker = self._tracker()
        sched = HealthReviewScheduler(
            conn_factory=lambda: db_conn, dispatcher=AlertDispatcher(),
            clock=lambda: _MONDAY + timedelta(days=2), pnl_tracker=tracker,
        )
        sched()
        tracker.generate_weekly_report.assert_not_called()

    def test_fires_once_per_monday(self, db_conn, mock_run_review):
        tracker = self._tracker()
        sched = HealthReviewScheduler(
            conn_factory=lambda: db_conn, dispatcher=AlertDispatcher(),
            clock=lambda: _MONDAY, pnl_tracker=tracker,
        )
        sched(); sched(); sched()
        assert tracker.generate_weekly_report.call_count == 1

    def test_pnl_failure_does_not_block_the_health_review(
        self, db_conn, mock_run_review,
    ):
        """Isolated failure domains — a broken P&L digest must not cost
        you the health review, which is the higher-value artefact."""
        tracker = self._tracker()
        tracker.generate_weekly_report.side_effect = RuntimeError("disk full")
        sched = HealthReviewScheduler(
            conn_factory=lambda: db_conn, dispatcher=AlertDispatcher(),
            clock=lambda: _MONDAY, pnl_tracker=tracker,
        )
        sched()   # must not raise
        mock_run_review.assert_called_once()
        assert sched.last_weekly_fired_date == _MONDAY.date()

    def test_no_tracker_is_a_noop(self, db_conn, mock_run_review):
        """Existing constructions without a tracker keep working."""
        sched = _make_scheduler(db_conn, clock_value=_MONDAY)
        sched()   # must not raise
        mock_run_review.assert_called_once()

    def test_no_daily_files_is_reported_not_raised(self, db_conn, mock_run_review):
        """generate_weekly_report returns None when there is nothing to
        aggregate; that is a log line, not a failure."""
        tracker = self._tracker()
        tracker.generate_weekly_report.return_value = None
        sched = HealthReviewScheduler(
            conn_factory=lambda: db_conn, dispatcher=AlertDispatcher(),
            clock=lambda: _MONDAY, pnl_tracker=tracker,
        )
        sched()
        assert sched.last_weekly_fired_date == _MONDAY.date()
