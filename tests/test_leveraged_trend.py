"""
Tests for the leveraged-ETF 200-day SMA phase monitor.

The state-machine tests run with ``sma_length=2``, where the SMA is the mean
of the current and previous close. That makes each bar's side of the line
exactly controllable: a close that rose sits strictly above its own SMA, one
that fell sits strictly below, and one that is unchanged lands exactly on it.
``_closes_from_sides`` builds a series from a side pattern on that basis.

The alternative — a flat base with excursions — cannot express the "exactly on
the line" case at all, and worse, its own base bars sit ON the SMA rather than
above it, which would silently seed the wrong phase. ``add_sma`` itself is
covered by ``tests/test_technicals.py``; what needs exercising here is the
replay, and the replay does not care how long the average is.
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


def _closes_from_sides(sides: str, *, start: float = BASE, step: float = 1.0) -> list[float]:
    """
    Build closes whose position against a 2-bar SMA follows ``sides``.

    One character per EVALUABLE bar; a leading anchor close is prepended for
    the SMA warmup and is dropped by the evaluator.

        'A' — close rose, so it sits strictly above the 2-bar mean
        'B' — close fell, so it sits strictly below
        'E' — close unchanged, so it lands exactly on the line

    Integer-valued steps keep every midpoint exactly representable, so 'E'
    means bit-exact equality rather than "close enough".
    """
    closes = [start]
    for ch in sides:
        last = closes[-1]
        closes.append({"A": last + step, "B": last - step, "E": last}[ch])
    return closes


def _evaluate(sides: str, **kwargs):
    """Evaluate a side pattern, e.g. 'AAABBB'."""
    params = {"sma_length": 2, "exit_days": 3, "entry_days": 5}
    params.update(kwargs)
    return evaluate_series(
        _frame(_closes_from_sides(sides)), underlying="TEST", **params
    )


class TestFixture:
    """The side-pattern helper must mean what the other tests assume."""

    @pytest.mark.parametrize(
        "side,expect_below,expect_above",
        [("A", False, True), ("B", True, False), ("E", False, False)],
    )
    def test_each_side_character_lands_where_claimed(
        self, side, expect_below, expect_above
    ):
        from indicators.technicals import add_sma

        frame = add_sma(_frame(_closes_from_sides(side)), 2)
        row = frame.iloc[-1]
        assert bool(row["close"] < row["sma_2"]) is expect_below
        assert bool(row["close"] > row["sma_2"]) is expect_above


class TestPhaseOut:
    """3 consecutive closes strictly below the SMA confirm a phase-out."""

    def test_third_consecutive_close_below_flips_to_out(self):
        state = _evaluate("AAAAABBB")
        assert state.phase is Phase.OUT
        assert state.below_streak == 3

    def test_two_closes_below_do_not_flip(self):
        state = _evaluate("AAAAABB")
        assert state.phase is Phase.IN
        assert state.below_streak == 2
        assert state.sessions_to_signal == 1

    def test_transition_is_recorded_on_the_confirming_session(self):
        state = _evaluate("AAAAABBB")
        assert len(state.transitions) == 1
        assert state.transitions[0].phase is Phase.OUT
        assert state.transitions[0].transition_date == state.last_bar_date
        assert state.phase_since == state.last_bar_date
        assert state.sessions_in_phase == 0


class TestFlappingIsFiltered:
    """A close back across the line resets the streak — the whole point."""

    def test_two_below_then_one_above_resets_the_streak(self):
        state = _evaluate("AAAAABBABB")
        assert state.phase is Phase.IN
        assert state.transitions == ()
        # The final two bars are a fresh streak, not a continuation of four.
        assert state.below_streak == 2

    def test_four_above_while_out_does_not_phase_in(self):
        state = _evaluate("AAAAABBBAAAA")
        assert state.phase is Phase.OUT
        assert state.above_streak == 4
        assert state.sessions_to_signal == 1

    def test_fifth_consecutive_close_above_phases_in(self):
        state = _evaluate("AAAAABBBAAAAA")
        assert state.phase is Phase.IN
        assert state.above_streak == 5
        assert [t.phase for t in state.transitions] == [Phase.OUT, Phase.IN]

    def test_a_break_in_the_re_entry_run_restarts_the_count(self):
        # Four above, one below, four above — never five consecutive.
        state = _evaluate("AAAAABBBAAAABAAAA")
        assert state.phase is Phase.OUT
        assert state.above_streak == 4
        assert [t.phase for t in state.transitions] == [Phase.OUT]


class TestOnTheLineIsNeitherSide:
    """
    The rule is strict in BOTH directions.

    A close landing exactly on the SMA is not a breach and not a confirmation.
    Folding it into "above" would let on-the-line closes complete a phase-IN
    run the rule never granted.
    """

    def test_on_the_line_resets_both_streaks(self):
        state = _evaluate("AAAAABBE")
        assert state.below_streak == 0
        assert state.above_streak == 0
        assert state.phase is Phase.IN

    def test_on_the_line_holds_the_phase_rather_than_flipping_it(self):
        state = _evaluate("AAAAABBBEE")
        # Already OUT; two on-the-line closes must not start an entry run.
        assert state.phase is Phase.OUT
        assert state.above_streak == 0

    def test_five_on_the_line_closes_cannot_phase_in(self):
        state = _evaluate("AAAAABBBEEEEE")
        assert state.phase is Phase.OUT
        assert [t.phase for t in state.transitions] == [Phase.OUT]

    def test_three_on_the_line_closes_cannot_phase_out(self):
        state = _evaluate("AAAAAEEE")
        assert state.phase is Phase.IN
        assert state.transitions == ()

    def test_on_the_line_breaks_a_re_entry_run(self):
        # Four above, one on the line, one above: the run restarts at 1.
        state = _evaluate("AAAAABBBAAAAEA")
        assert state.phase is Phase.OUT
        assert state.above_streak == 1

    def test_on_the_line_breaks_an_exit_run(self):
        state = _evaluate("AAAAABBEBB")
        assert state.phase is Phase.IN
        assert state.below_streak == 2
        assert state.transitions == ()

    def test_a_series_entirely_on_the_line_has_no_phase(self):
        state = _evaluate("EEEEE")
        assert state.phase is Phase.UNKNOWN
        assert state.error == "every close sits exactly on the SMA"
        assert state.sessions_to_signal is None


class TestSeedPhase:
    """
    The seed comes from the data, and is never dressed up as a signal.

    A phase already held when the window opens has no confirmed start date:
    it may have begun long before the data does.
    """

    def test_window_opening_below_the_sma_seeds_out(self):
        state = _evaluate("BBBBB")
        assert state.phase is Phase.OUT
        assert state.transitions == ()
        assert state.last_cross_date is None

    def test_window_opening_above_the_sma_seeds_in(self):
        state = _evaluate("AAAAA")
        assert state.phase is Phase.IN
        assert state.transitions == ()

    def test_seeded_phase_reports_no_confirmed_start_date(self):
        state = _evaluate("AAAAA")
        assert state.phase_is_seeded is True
        assert state.phase_since is None
        assert state.sessions_in_phase is None
        assert state.days_since_phase_change is None

    def test_seeded_phase_exposes_the_window_start_as_a_lower_bound(self):
        state = _evaluate("AAAAA")
        assert state.observed_from is not None
        assert state.observed_from < state.last_bar_date
        assert state.days_observed == (
            state.last_bar_date - state.observed_from
        ).days

    def test_an_observed_transition_is_not_seeded(self):
        state = _evaluate("AAAAABBB")
        assert state.phase_is_seeded is False
        assert state.phase_since is not None
        assert state.days_since_phase_change == 0

    def test_seed_ignores_leading_on_the_line_closes(self):
        # The first two bars sit on the line and pick no side; the seed comes
        # from the first bar that does.
        state = _evaluate("EEBBB")
        assert state.phase is Phase.OUT
        assert state.transitions == ()


class TestCrossTracking:
    """The raw SMA cross is tracked separately from the confirmed signal."""

    def test_last_cross_precedes_the_confirmed_transition(self):
        state = _evaluate("AAAAABBB")
        assert state.last_cross_date is not None
        assert state.phase_since is not None
        # The line was crossed on the first down bar; the phase confirmed two
        # sessions later.
        assert state.last_cross_date < state.phase_since
        assert state.sessions_since_cross == 2
        assert state.sessions_in_phase == 0

    def test_no_cross_in_window_reports_none(self):
        state = _evaluate("AAAAA")
        assert state.last_cross_date is None
        assert state.sessions_since_cross is None
        assert state.days_since_cross is None

    def test_touching_the_line_and_returning_is_not_a_cross(self):
        # Above, on the line, above again: the price reached the line but
        # never passed through it.
        state = _evaluate("AAAAAEAA")
        assert state.last_cross_date is None

    def test_dropping_to_the_line_and_back_below_is_not_a_new_cross(self):
        state = _evaluate("AAAAABEB")
        # One cross only, at the first bar below.
        assert state.sessions_since_cross == 2

    def test_passing_through_the_line_is_a_cross_on_the_far_side(self):
        # Below, on the line, above: the crossing lands on the bar that
        # actually reaches the other side, not on the on-the-line bar.
        state = _evaluate("AAAAABEA")
        assert state.sessions_since_cross == 0
        assert state.last_cross_date == state.last_bar_date


class TestFailsTowardStayingOut:
    """Insufficient or unusable data must never present as phase-IN."""

    def test_history_shorter_than_the_sma_is_unknown(self):
        state = _evaluate("AAAA", sma_length=10)
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
        state = _evaluate("AAAA", sma_length=10)
        assert state.sessions_to_signal is None

    def test_unknown_is_never_reported_as_seeded(self):
        # `phase_is_seeded` gates the '>=' lower-bound rendering; UNKNOWN has
        # no phase to bound.
        assert _evaluate("EEEEE").phase_is_seeded is False
        assert _evaluate("AAAA", sma_length=10).phase_is_seeded is False


class TestDropInProgressBar:
    """The live, unfinished daily bar must not advance a streak."""

    # 2026-08-24 is a Monday; August is EDT (UTC-4).
    TODAY_BAR = "2026-08-24"

    def _two_bars(self) -> pd.DataFrame:
        return _frame(_closes_from_sides("A"), start="2026-08-21")

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
        df = _frame(_closes_from_sides("A" * 22), start="2026-07-22")
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
            sma_length=2,
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
            return _frame(_closes_from_sides("A" * 24)), None

        monkeypatch.setattr(
            leveraged_trend.fetcher, "fetch_symbol", _capture
        )
        state = load_monitor_state("SPY", None, sma_length=2)
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
        assert {"QQQ", "XLK", "SPY", "SMH", "XLF", "XLE"} <= set(pairs)
        assert pairs["QQQ"] == "TQQQ"
        assert pairs["XLK"] == "TECL"
        assert pairs["SPY"] == "SPXL"
        assert pairs["SMH"] == "SOXL"
        assert pairs["XLF"] == "FAS"
        assert pairs["XLE"] == "ERX"

    def test_no_fund_is_reused_across_underlyings(self):
        # Two underlyings sharing a fund would render the same holding twice
        # under conflicting phase signals.
        funds = [f for f in settings.LEVERAGED_TREND_PAIRS.values() if f]
        assert len(funds) == len(set(funds))

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
