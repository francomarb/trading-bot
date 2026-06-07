"""
Unit tests for scripts/sma_giveback_audit.py.

Covers the failure modes flagged in the ChatGPT review:
  - Entry/stop ordering on the entry bar (no same-bar stop hit)
  - Gap-through-stop pricing (fill at stop, not at the gap-down open)
  - Alternative-policy comparisons fire on every entry, not just winners
  - Open positions at end of dataset are recorded with reason='eod'
  - Universe pinning makes results reproducible
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.sma_giveback_audit import (
    AUDIT_UNIVERSE,
    ATR_LEN,
    ATR_STOP_MULT,
    FAST,
    SLOW,
    Trade,
    _BarsView,
    _fill_through_stop,
    _iter_entries,
    _policy_baseline,
    _policy_chandelier,
    _policy_gated_trail,
    _policy_take_profit,
    _prepare_bars,
    simulate_symbol,
)


def _ohlcv_from_closes(closes, *, start="2018-01-01", atr_jitter=0.5) -> pd.DataFrame:
    """Build a minimal OHLCV frame from a close-price list.

    Adds a small high/low spread (`atr_jitter`) on every bar so ATR is
    non-zero — a perfectly flat warmup would produce ATR=0, which breaks
    any policy that scales a threshold by ATR (the trail-activation
    arithmetic becomes (close-entry) >= 0 and arms immediately). Bars are
    daily, business-day-indexed.
    """
    closes = np.asarray(closes, dtype=float)
    n = len(closes)
    dates = pd.date_range(start=start, periods=n, freq="B", tz="UTC")
    opens = closes.copy()
    return pd.DataFrame({
        "open":  opens,
        "high":  np.maximum(opens, closes) + atr_jitter,
        "low":   np.minimum(opens, closes) - atr_jitter,
        "close": closes,
        "volume": np.full(n, 1_000_000),
    }, index=dates)


def _golden_then_death_cross_frame() -> pd.DataFrame:
    """
    Build a 200-bar frame that:
      - has enough warmup for SLOW=50 SMA
      - prints a golden cross around bar 80
      - rides up to a peak around bar 140
      - prints a death cross around bar 180

    Used as a "well-behaved trend" fixture across multiple tests.
    """
    n = 200
    # Sideways for the first 60 bars to establish SMAs.
    base = np.full(60, 100.0)
    # Ramp up over bars 60-140 to print a golden cross then HWM.
    ramp = np.linspace(100.0, 160.0, 80)
    # Pull back over bars 140-200 to print a death cross.
    fade = np.linspace(160.0, 110.0, 60)
    closes = np.concatenate([base, ramp, fade])
    assert len(closes) == n
    return _ohlcv_from_closes(closes)


def _up_only_frame(n_warmup: int = 60, n_ramp: int = 80) -> pd.DataFrame:
    """
    Build a frame that warms up flat then ramps up monotonically with no
    death cross. Used to test stop-hit behaviors in isolation: any stop hit
    must come from an injected wick/gap, not from a natural exit.
    """
    closes = np.concatenate([
        np.full(n_warmup, 100.0),
        np.linspace(100.0, 200.0, n_ramp),
    ])
    return _ohlcv_from_closes(closes)


class TestFillThroughStop:
    def test_returns_stop_level_when_low_touches_stop(self):
        # Low exactly at stop — fills at stop.
        assert _fill_through_stop(low=95.0, stop=95.0) == 95.0

    def test_returns_stop_level_when_low_below_stop(self):
        # Documented limitation: gap-through fills AT the stop, not at the
        # gap-down low. This test pins that behavior so it's not changed
        # silently.
        assert _fill_through_stop(low=90.0, stop=95.0) == 95.0


class TestPrepareBars:
    def test_returns_none_for_insufficient_history(self):
        # Insufficient bars → all SMA/ATR rows are NaN → dropna leaves an
        # empty frame → returns None.
        df = _ohlcv_from_closes(np.linspace(100, 110, SLOW - 5))
        assert _prepare_bars(df) is None

    def test_drops_warmup_nans(self):
        df = _ohlcv_from_closes(np.linspace(100, 110, SLOW + 20))
        bars = _prepare_bars(df)
        assert bars is not None
        # Slow SMA needs SLOW bars; first SLOW-1 are NaN and dropped.
        # ATR needs ATR_LEN+1 bars; SMAs dominate the warmup since SLOW > ATR_LEN.
        assert len(bars.closes) <= len(df) - (SLOW - 1)

    def test_columns_are_numpy_arrays(self):
        df = _ohlcv_from_closes(np.linspace(100, 110, SLOW + 30))
        bars = _prepare_bars(df)
        assert isinstance(bars.opens, np.ndarray)
        assert isinstance(bars.fast, np.ndarray)
        assert isinstance(bars.slow, np.ndarray)
        assert isinstance(bars.atr, np.ndarray)


class TestIterEntries:
    def test_finds_golden_cross_entry(self):
        df = _golden_then_death_cross_frame()
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        # Should find at least one entry in this rising-then-falling regime.
        assert len(entries) >= 1
        # Each entry index must be valid (within array bounds).
        for ei in entries:
            assert 0 < ei < len(bars.closes)

    def test_no_entries_in_flat_data(self):
        # Pure flat closes never produce a crossover.
        df = _ohlcv_from_closes(np.full(SLOW + 50, 100.0))
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries == []


class TestPolicyEntryBarStopAndTakeProfit:
    """
    Entry-bar checks: in production the OTO stop attaches as soon as the
    parent fills at the open, so the stop (and a take-profit if any) can
    trigger on the same bar. The simulator must honor that.

    The DEATH-CROSS check still starts one bar after entry — see
    _policy_baseline docstring for why.
    """

    def _entry_idx_in_trimmed_frame(self, df):
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries, "fixture must produce at least one entry"
        return bars, entries[0]

    def _untrimmed_index(self, df, bars, trimmed_idx):
        """Map a trimmed-frame index back to the untrimmed df index."""
        return trimmed_idx + (len(df) - len(bars.closes))

    def test_entry_bar_stop_triggers_when_low_below_stop(self):
        # Inject a wick that pierces the disaster stop on the entry bar.
        df = _up_only_frame()
        bars, trimmed_entry = self._entry_idx_in_trimmed_frame(df)
        entry_price = bars.opens[trimmed_entry]
        entry_atr = bars.atr[trimmed_entry - 1]
        assert entry_atr > 0
        expected_stop = entry_price - ATR_STOP_MULT * entry_atr

        # Map back to untrimmed index and inject a low that touches the stop.
        u_idx = self._untrimmed_index(df, bars, trimmed_entry)
        df.iloc[u_idx, df.columns.get_loc("low")] = expected_stop - 0.01

        bars = _prepare_bars(df)
        exit_idx, exit_price, exit_reason, *_ = _policy_baseline(bars, trimmed_entry)
        assert exit_reason == "atr_stop"
        assert exit_idx == trimmed_entry  # SAME bar
        assert exit_price == pytest.approx(expected_stop)

    def test_entry_bar_take_profit_triggers_when_high_above_target(self):
        # Inject a high spike that touches the +10% target on the entry bar.
        df = _up_only_frame()
        bars, trimmed_entry = self._entry_idx_in_trimmed_frame(df)
        entry_price = bars.opens[trimmed_entry]
        target = entry_price * 1.10

        u_idx = self._untrimmed_index(df, bars, trimmed_entry)
        df.iloc[u_idx, df.columns.get_loc("high")] = target + 0.01

        bars = _prepare_bars(df)
        exit_idx, exit_price, exit_reason, *_ = _policy_take_profit(
            bars, trimmed_entry, target_pct=0.10
        )
        assert exit_reason == "take_profit"
        assert exit_idx == trimmed_entry  # SAME bar
        assert exit_price == pytest.approx(target)

    def test_entry_bar_no_trigger_when_range_inside_band(self):
        # Vanilla entry bar — low above stop, high below TP, close above
        # open. Nothing fires on the entry bar.
        df = _up_only_frame()
        bars, trimmed_entry = self._entry_idx_in_trimmed_frame(df)
        # Just run baseline — the up_only fixture shouldn't trip anything
        # on the entry bar by construction.
        exit_idx, _, exit_reason, *_ = _policy_baseline(bars, trimmed_entry)
        assert exit_idx > trimmed_entry  # survived the entry bar
        assert exit_reason in ("death_cross", "eod")

    def test_entry_bar_death_cross_does_not_exit_immediately(self):
        # Even if the entry bar's fast/slow could be interpreted as a
        # death cross relative to the prior bar (extreme corner), the
        # policy never exits on the death cross on the entry bar itself.
        df = _golden_then_death_cross_frame()
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries
        entry_idx = entries[0]
        # No injection — just verify the baseline does not exit on entry bar
        # via death cross even if the cross math happens to satisfy on that bar.
        exit_idx, _, exit_reason, *_ = _policy_baseline(bars, entry_idx)
        assert exit_idx > entry_idx  # never exits ON entry bar via death cross
        assert exit_reason in ("death_cross", "atr_stop", "eod")


class TestPolicyBaselineGapThroughStop:
    def test_gap_down_through_stop_fills_at_stop(self):
        """
        A bar that opens and closes well BELOW the stop — the audit fills
        at the stop level (documented limitation). This pins that behavior.

        Uses the up-only fixture so no natural exit (death cross / EOD)
        fires before the injected gap.
        """
        df = _up_only_frame()
        # Prepare once to find the trimmed-frame entry index, then map
        # back to the untrimmed index to inject the gap, then re-prepare.
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries
        trimmed_entry_idx = entries[0]
        entry_price = bars.opens[trimmed_entry_idx]
        entry_atr = bars.atr[trimmed_entry_idx - 1]
        assert entry_atr > 0, "fixture must produce non-zero ATR"
        expected_stop = entry_price - ATR_STOP_MULT * entry_atr

        # Untrimmed index = trimmed index + offset between frames.
        offset = len(df) - len(bars.closes)
        gap_untrimmed_idx = trimmed_entry_idx + offset + 5
        gap_low = expected_stop - 10.0
        for col, val in (("open", gap_low), ("high", gap_low),
                         ("low", gap_low), ("close", gap_low)):
            df.iloc[gap_untrimmed_idx, df.columns.get_loc(col)] = val

        bars = _prepare_bars(df)
        exit_idx, exit_price, exit_reason, *_ = _policy_baseline(
            bars, trimmed_entry_idx
        )
        assert exit_reason == "atr_stop"
        # Documented: fill at stop, not at the gap-down open.
        assert exit_price == pytest.approx(expected_stop)


class TestPolicyBaselineOpenAtEnd:
    def test_open_position_at_end_recorded_as_eod(self):
        # Build a fixture where the golden cross fires near the end of the
        # data and price keeps going up — no death cross, no stop hit.
        n = SLOW + 40
        closes = np.concatenate([
            np.full(60, 100.0),
            np.linspace(100.0, 150.0, n - 60),
        ])
        df = _ohlcv_from_closes(closes)
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries
        exit_idx, exit_price, exit_reason, *_ = _policy_baseline(bars, entries[0])
        # The position should still be open at end-of-data.
        assert exit_reason == "eod"
        assert exit_idx == len(bars.closes) - 1


class TestPolicyChandelierEntryBarNoLookahead:
    """
    The chandelier's HWM is initialized from entry_idx's close, which
    prints AFTER the bar's high/low. Evaluating a trail derived from that
    close against the same bar's low is intrabar look-ahead. The entry
    bar must enforce ONLY the static disaster stop; the trail engages
    from the next bar onward.
    """

    def test_entry_bar_low_below_trail_does_not_trigger_trail(self):
        # Arrange a setup where on the entry bar:
        #   - the disaster stop is FAR below entry (large ATR), so it does
        #     not trigger
        #   - a hypothetical chandelier trail at HWM - 0.5*ATR DOES sit
        #     above the bar's low, so a look-ahead check would fire
        # Under the corrected policy, neither fires on the entry bar.
        df = _up_only_frame()
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries
        trimmed_entry = entries[0]
        entry_close = bars.closes[trimmed_entry]
        entry_atr = bars.atr[trimmed_entry - 1]

        # Engineer the entry bar's low to sit between disaster_stop (very
        # low) and a hypothetical tight chandelier trail (HWM - 0.5*ATR).
        # A buggy implementation would treat this as a trail hit.
        target_low = entry_close - 0.5 * entry_atr  # tight; above disaster

        u_idx = trimmed_entry + (len(df) - len(bars.closes))
        df.iloc[u_idx, df.columns.get_loc("low")] = target_low

        bars = _prepare_bars(df)
        exit_idx, _, exit_reason, *_ = _policy_chandelier(
            bars, trimmed_entry, k=0.5
        )
        # Under the corrected policy: no trail check on entry bar →
        # position survives past the entry bar.
        assert exit_idx > trimmed_entry
        # And if it later exits via the chandelier, that's after entry.
        assert exit_reason in ("trail", "death_cross", "atr_stop", "eod")

    def test_entry_bar_disaster_stop_still_triggers(self):
        # The static disaster stop is computed from the entry open and is
        # known before the bar trades — it CAN trigger on the entry bar.
        # This pins that the entry-bar look-ahead fix did NOT also turn off
        # the legitimate disaster-stop check.
        df = _up_only_frame()
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries
        trimmed_entry = entries[0]
        entry_price = bars.opens[trimmed_entry]
        entry_atr = bars.atr[trimmed_entry - 1]
        disaster_stop = entry_price - ATR_STOP_MULT * entry_atr

        u_idx = trimmed_entry + (len(df) - len(bars.closes))
        df.iloc[u_idx, df.columns.get_loc("low")] = disaster_stop - 0.01

        bars = _prepare_bars(df)
        exit_idx, exit_price, exit_reason, *_ = _policy_chandelier(
            bars, trimmed_entry, k=3.0
        )
        assert exit_idx == trimmed_entry
        assert exit_reason == "atr_stop"
        assert exit_price == pytest.approx(disaster_stop)


class TestPolicyChandelierExitFiresBeforeDeathCross:
    def test_chandelier_can_exit_before_death_cross(self):
        df = _golden_then_death_cross_frame()
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries

        base_exit_idx, _, base_reason, *_ = _policy_baseline(bars, entries[0])
        # K=2.5 — should be tight enough to trip before the death cross on
        # the falling tail of the fixture.
        chand_exit_idx, _, chand_reason, *_ = _policy_chandelier(
            bars, entries[0], 2.5
        )
        # The chandelier either trips earlier OR fires on the same bar as the
        # death cross. It cannot fire LATER than the baseline because the
        # baseline's death-cross exit is also available to it.
        assert chand_exit_idx <= base_exit_idx
        # And when it trips first, it's labeled 'trail'.
        if chand_exit_idx < base_exit_idx:
            assert chand_reason == "trail"


class TestPolicyGatedTrailArming:
    def test_gated_trail_does_not_arm_below_activation_threshold(self):
        # Build a slow ramp where profit never reaches the activation level
        # before a death cross prints. The gated policy should behave
        # identically to the baseline.
        df = _golden_then_death_cross_frame()
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries
        base_result = _policy_baseline(bars, entries[0])
        # Set activation absurdly high — never arms.
        gated_result = _policy_gated_trail(bars, entries[0],
                                           activation_k=1000.0, trail_k=3.0)
        assert base_result == gated_result


class TestPolicyTakeProfitFiresOnEveryEntry:
    """The selection-bias fix: take-profit must be evaluated on losers too."""

    def test_take_profit_fires_on_eventual_loser_if_high_touched_first(self):
        # Build a fixture where price pops UP just above +10% then collapses
        # through the ATR stop. Baseline exits at atr_stop (loss); TP exits
        # at +10% (small win).
        n = SLOW + 30
        closes = np.concatenate([
            np.full(60, 100.0),                      # warmup
            np.linspace(100.0, 115.0, 8),            # golden cross + run-up
            np.linspace(115.0, 70.0, n - 68),        # collapse
        ])
        df = _ohlcv_from_closes(closes)
        bars = _prepare_bars(df)
        entries = _iter_entries(bars)
        assert entries
        entry_idx = entries[0]
        entry_price = bars.opens[entry_idx]

        base_idx, base_price, base_reason, *_ = _policy_baseline(bars, entry_idx)
        tp_idx, tp_price, tp_reason, *_ = _policy_take_profit(
            bars, entry_idx, target_pct=0.10
        )

        # Baseline exits at the stop, taking a loss.
        assert base_reason == "atr_stop"
        assert base_price < entry_price
        # Take-profit fires earlier with a small win.
        assert tp_reason == "take_profit"
        assert tp_idx < base_idx
        assert tp_price == pytest.approx(entry_price * 1.10)


class TestSimulateSymbolPolicyDispatch:
    def test_baseline_policy_runs(self):
        df = _golden_then_death_cross_frame()
        trades = simulate_symbol("TEST", df, "baseline", ())
        assert all(t.symbol == "TEST" for t in trades)
        assert all(t.policy.startswith("baseline") for t in trades)

    def test_chandelier_policy_passes_k(self):
        df = _golden_then_death_cross_frame()
        trades = simulate_symbol("TEST", df, "chandelier", (3.0,))
        assert all(t.policy.startswith("chandelier") for t in trades)

    def test_gated_policy_passes_both_args(self):
        df = _golden_then_death_cross_frame()
        trades = simulate_symbol("TEST", df, "gated", (3.0, 4.0))
        assert trades  # at minimum, the one entry should produce one trade
        assert all(t.policy.startswith("gated") for t in trades)

    def test_unknown_policy_raises_keyerror(self):
        df = _golden_then_death_cross_frame()
        with pytest.raises(KeyError):
            simulate_symbol("TEST", df, "no_such_policy", ())

    def test_no_overlapping_trades(self):
        # A trade cannot start before the previous one ends.
        n = 600
        rng = np.random.default_rng(42)
        # Random walk that should produce multiple crossovers.
        steps = rng.normal(0, 1, n).cumsum()
        closes = 100.0 + steps
        df = _ohlcv_from_closes(closes)
        trades = simulate_symbol("TEST", df, "baseline", ())
        for prev, curr in zip(trades, trades[1:]):
            assert curr.entry_date > prev.exit_date


class TestTradeProperties:
    def test_pnl_is_exit_minus_entry(self):
        t = Trade(
            symbol="X", policy="baseline",
            entry_date=pd.Timestamp("2024-01-01", tz="UTC"),
            entry_price=100.0,
            exit_date=pd.Timestamp("2024-01-10", tz="UTC"),
            exit_price=110.0,
            exit_reason="death_cross",
            hwm_close=115.0,
            hwm_date=pd.Timestamp("2024-01-08", tz="UTC"),
            atr_at_entry=2.0,
        )
        assert t.pnl == 10.0
        assert t.peak_open_profit == 15.0
        assert t.giveback_dollars == 5.0
        assert t.giveback_pct == pytest.approx(5.0 / 15.0)
        assert t.giveback_atr == pytest.approx(5.0 / 2.0)

    def test_giveback_pct_nan_when_peak_zero(self):
        t = Trade(
            symbol="X", policy="baseline",
            entry_date=pd.Timestamp("2024-01-01", tz="UTC"),
            entry_price=100.0,
            exit_date=pd.Timestamp("2024-01-10", tz="UTC"),
            exit_price=90.0,
            exit_reason="atr_stop",
            hwm_close=100.0,                    # peak never above entry
            hwm_date=pd.Timestamp("2024-01-01", tz="UTC"),
            atr_at_entry=2.0,
        )
        assert t.peak_open_profit == 0.0
        # NaN by NaN-equality check.
        assert t.giveback_pct != t.giveback_pct


class TestAuditUniverseFrozen:
    """The pinned audit universe must remain stable so docs reproduce."""

    def test_audit_universe_size(self):
        assert len(AUDIT_UNIVERSE) == 40

    def test_audit_universe_contains_known_top_contributors(self):
        # Top 5 by per-share P&L in the documented baseline.
        for sym in ("ASML", "CAT", "STRL", "STX", "MU"):
            assert sym in AUDIT_UNIVERSE

    def test_audit_universe_contains_culled_chronic_losers(self):
        # Chat GPT review (2026-06-06): the cull was deferred, so these
        # remain in the audit universe even though they were briefly
        # removed from production SMA_WATCHLIST.
        for sym in ("VIAV", "VSAT", "CIEN", "ALB", "INTC"):
            assert sym in AUDIT_UNIVERSE
