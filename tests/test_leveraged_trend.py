"""
Tests for the leveraged-ETF 200-day SMA phase monitor.

The logic tests run with ``sma_length=10`` against a base of 100.0 with
excursions to 80/120: a 10-bar SMA anchored on a long flat base moves slowly
enough that a multi-session excursion stays cleanly on one side of the line.
A flat series would converge the SMA onto the close and dissolve the streak
being tested, which is why the excursions are large relative to the base.

Expected streaks and phases below are reasoned from the series, not read back
from the implementation.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd
import pytest

from config import settings
from monitors import leveraged_trend
from monitors.leveraged_trend import (
    Phase,
    drop_in_progress_bar,
    evaluate_series,
    load_monitor_state,
)

BASE = 100.0
DOWN = 80.0
UP = 120.0


def _frame(closes: list[float], *, start: str = "2026-01-05") -> pd.DataFrame:
    """Daily OHLCV frame on business days, timestamped 00:00 New York."""
    index = pd.bdate_range(start=start, periods=len(closes), tz="America/New_York")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c * 1.01 for c in closes],
            "low": [c * 0.99 for c in closes],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=index.tz_convert("UTC"),
    )


def _evaluate(closes: list[float], **kwargs):
    params = {"sma_length": 10, "exit_days": 3, "entry_days": 5}
    params.update(kwargs)
    return evaluate_series(_frame(closes), underlying="TEST", **params)


class TestPhaseOut:
    """3 consecutive closes below the SMA confirm a phase-out."""

    def test_third_consecutive_close_below_flips_to_out(self):
        state = _evaluate([BASE] * 20 + [DOWN] * 3)
        assert state.phase is Phase.OUT
        assert state.below_streak == 3

    def test_two_closes_below_do_not_flip(self):
        state = _evaluate([BASE] * 20 + [DOWN] * 2)
        assert state.phase is Phase.IN
        assert state.below_streak == 2
        assert state.sessions_to_signal == 1

    def test_transition_is_recorded_on_the_confirming_session(self):
        state = _evaluate([BASE] * 20 + [DOWN] * 3)
        assert len(state.transitions) == 1
        transition = state.transitions[0]
        assert transition.phase is Phase.OUT
        # 20 base bars + 3 down bars; the 23rd bar (index 22) confirms.
        assert transition.transition_date == state.last_bar_date


class TestFlappingIsFiltered:
    """A close back across the line resets the streak — the whole point."""

    def test_two_below_then_one_above_resets_the_streak(self):
        state = _evaluate([BASE] * 20 + [DOWN, DOWN, UP, DOWN, DOWN])
        assert state.phase is Phase.IN
        assert state.transitions == ()
        # The final two bars are a fresh streak, not a continuation of four.
        assert state.below_streak == 2

    def test_four_above_while_out_does_not_phase_in(self):
        closes = [BASE] * 20 + [DOWN] * 3 + [UP] * 4
        state = _evaluate(closes)
        assert state.phase is Phase.OUT
        assert state.above_streak == 4
        assert state.sessions_to_signal == 1

    def test_fifth_consecutive_close_above_phases_in(self):
        closes = [BASE] * 20 + [DOWN] * 3 + [UP] * 5
        state = _evaluate(closes)
        assert state.phase is Phase.IN
        assert state.above_streak == 5
        assert [t.phase for t in state.transitions] == [Phase.OUT, Phase.IN]

    def test_a_break_in_the_re_entry_run_restarts_the_count(self):
        # Four above, one below, four above — never five consecutive.
        closes = [BASE] * 20 + [DOWN] * 3 + [UP] * 4 + [DOWN] + [UP] * 4
        state = _evaluate(closes)
        assert state.phase is Phase.OUT
        assert state.above_streak == 4
        assert [t.phase for t in state.transitions] == [Phase.OUT]


class TestSmaBoundary:
    """A close exactly on the SMA is not a breach."""

    def test_flat_series_sits_on_its_sma_and_stays_in(self):
        state = _evaluate([BASE] * 25)
        assert state.close == state.sma
        assert state.phase is Phase.IN
        assert state.below_streak == 0
        assert state.above_streak > 0


class TestSeedPhase:
    """The seed comes from the data, not an assumption."""

    def test_window_opening_below_the_sma_seeds_out(self):
        # A monotonic decline: every evaluable bar closes below its own SMA.
        closes = [200.0 - i for i in range(25)]
        state = _evaluate(closes)
        assert state.phase is Phase.OUT
        # Seeded from the data, NOT transitioned into: the first evaluable bar
        # was already below, so there is no phase-OUT event to report and the
        # monitor does not manufacture one it never observed.
        assert state.transitions == ()
        assert state.last_cross_date is None
        # 25 bars, 10-bar warmup -> 16 evaluable bars, seeded at the first.
        assert state.sessions_in_phase == 15

    def test_window_opening_above_the_sma_seeds_in(self):
        closes = [100.0 + i for i in range(25)]
        state = _evaluate(closes)
        assert state.phase is Phase.IN
        assert state.transitions == ()


class TestCrossTracking:
    """The raw SMA cross is tracked separately from the confirmed signal."""

    def test_last_cross_precedes_the_confirmed_transition(self):
        state = _evaluate([BASE] * 20 + [DOWN] * 3)
        assert state.last_cross_date is not None
        assert state.phase_since is not None
        # The line was crossed on the first down bar; the phase confirmed two
        # sessions later.
        assert state.last_cross_date < state.phase_since
        assert state.sessions_since_cross == 2
        assert state.sessions_in_phase == 0

    def test_no_cross_in_window_reports_none(self):
        state = _evaluate([BASE] * 25)
        assert state.last_cross_date is None
        assert state.sessions_since_cross is None
        assert state.days_since_cross is None


class TestFailsTowardStayingOut:
    """Insufficient or unusable data must never present as phase-IN."""

    def test_history_shorter_than_the_sma_is_unknown(self):
        state = _evaluate([BASE] * 5, sma_length=10)
        assert state.phase is Phase.UNKNOWN
        assert state.error is not None
        assert "insufficient history" in state.error

    def test_empty_frame_is_unknown(self):
        state = evaluate_series(
            pd.DataFrame(), underlying="TEST", sma_length=10,
        )
        assert state.phase is Phase.UNKNOWN
        assert state.error == "no bars"

    def test_unknown_reports_no_pending_signal(self):
        state = _evaluate([BASE] * 5, sma_length=10)
        assert state.sessions_to_signal is None


class TestDropInProgressBar:
    """The live, unfinished daily bar must not advance a streak."""

    # 2026-08-24 is a Monday; August is EDT (UTC-4).
    TODAY_BAR = "2026-08-24"

    def _two_bars(self) -> pd.DataFrame:
        return _frame([BASE, DOWN], start="2026-08-21")

    def test_current_session_bar_is_dropped_before_the_cutoff(self):
        df = self._two_bars()
        assert pd.Timestamp(df.index[-1]).tz_convert(
            "America/New_York"
        ).date() == date(2026, 8, 24)
        # 18:00 UTC = 14:00 ET — session still open.
        out = drop_in_progress_bar(
            df, now=datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc)
        )
        assert len(out) == 1
        assert pd.Timestamp(out.index[-1]).tz_convert(
            "America/New_York"
        ).date() == date(2026, 8, 21)

    def test_current_session_bar_is_kept_after_the_cutoff(self):
        # 21:00 UTC = 17:00 ET — past 16:15, the bar is final.
        out = drop_in_progress_bar(
            self._two_bars(),
            now=datetime(2026, 8, 24, 21, 0, tzinfo=timezone.utc),
        )
        assert len(out) == 2

    def test_a_prior_session_bar_is_never_dropped(self):
        # Next morning: the trailing bar is yesterday's completed session.
        out = drop_in_progress_bar(
            self._two_bars(),
            now=datetime(2026, 8, 25, 13, 0, tzinfo=timezone.utc),
        )
        assert len(out) == 2

    def test_empty_frame_is_safe(self):
        assert drop_in_progress_bar(pd.DataFrame()).empty


class TestLoadMonitorState:
    """End-to-end behaviour through the public entry point."""

    def test_in_progress_bar_is_excluded_from_the_reported_state(
        self, monkeypatch
    ):
        # 22 completed sessions ending Friday 2026-08-21, plus a live Monday
        # bar. Only the completed sessions may reach the evaluator.
        closes = [BASE] * 21 + [BASE] + [DOWN]
        df = _frame(closes, start="2026-07-22")
        assert pd.Timestamp(df.index[-1]).tz_convert(
            "America/New_York"
        ).date() == date(2026, 8, 21)
        # Append a live bar for Monday 2026-08-24.
        live = df.iloc[[-1]].copy()
        live.index = pd.DatetimeIndex(
            [pd.Timestamp("2026-08-24 04:00", tz="UTC")]
        )
        df = pd.concat([df, live])

        monkeypatch.setattr(
            leveraged_trend.fetcher,
            "fetch_symbol",
            lambda *a, **k: (df, None),
        )
        state = load_monitor_state(
            "QQQ",
            "TQQQ",
            now=datetime(2026, 8, 24, 18, 0, tzinfo=timezone.utc),
            sma_length=10,
        )
        assert state.last_bar_date == date(2026, 8, 21)

    def test_fetch_failure_returns_unknown_instead_of_raising(
        self, monkeypatch
    ):
        def _boom(*args, **kwargs):
            raise RuntimeError("alpaca is down")

        monkeypatch.setattr(leveraged_trend.fetcher, "fetch_symbol", _boom)
        state = load_monitor_state("QQQ", "TQQQ")
        assert state.phase is Phase.UNKNOWN
        assert state.error is not None
        assert "alpaca is down" in state.error
        assert state.leveraged == "TQQQ"

    def test_configured_defaults_are_used_when_not_overridden(
        self, monkeypatch
    ):
        captured = {}

        def _capture(symbol, start, end, timeframe="1Day", **kwargs):
            captured["symbol"] = symbol
            captured["timeframe"] = timeframe
            captured["feed"] = kwargs.get("feed")
            return _frame([BASE] * 25), None

        monkeypatch.setattr(
            leveraged_trend.fetcher, "fetch_symbol", _capture
        )
        state = load_monitor_state("SPY", None, sma_length=10)
        assert captured["symbol"] == "SPY"
        assert captured["timeframe"] == "1Day"
        # SIP, not the engine's IEX — the rule keys off the official close.
        assert captured["feed"] == "sip"
        assert state.exit_days == settings.LEVERAGED_TREND_EXIT_DAYS
        assert state.entry_days == settings.LEVERAGED_TREND_ENTRY_DAYS


class TestSettings:
    """Configuration invariants the monitor depends on."""

    def test_pairs_are_configured_and_independent(self):
        pairs = settings.LEVERAGED_TREND_PAIRS
        assert {"QQQ", "XLK", "SPY", "SMH"} <= set(pairs)
        assert pairs["QQQ"] == "TQQQ"
        assert pairs["XLK"] == "TECL"
        assert pairs["SPY"] == "SPXL"
        assert pairs["SMH"] == "SOXL"

    def test_every_underlying_is_distinct_from_its_fund(self):
        # A pair mapping an underlying to itself would render a monitor that
        # claims to track an unleveraged index while naming a leveraged fund.
        for underlying, fund in settings.LEVERAGED_TREND_PAIRS.items():
            assert fund != underlying

    def test_reentry_demands_more_confirmation_than_exit(self):
        # The asymmetry is the noise filter: leave fast, return slowly.
        assert (
            settings.LEVERAGED_TREND_ENTRY_DAYS
            > settings.LEVERAGED_TREND_EXIT_DAYS
        )

    def test_monitor_feed_is_sip(self):
        assert settings.LEVERAGED_TREND_FEED == "sip"
