"""
Unit tests for strategies/donchian_breakout.py.

Coverage:
  - Param validation (positive ints, entry_window > exit_window)
  - No spurious signals on flat / monotonic-down paths
  - Entry fires on synthetic new-high breakout
  - Exit fires on synthetic new-low breakdown
  - Look-ahead guard: truncating input doesn't change past signals
  - Purity / no input mutation
  - required_bars() reflects the window correctly
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.donchian_breakout import DonchianBreakout
from strategies.base import OrderType


# ── Helpers ──────────────────────────────────────────────────────────────────


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame({"close": closes}, index=idx)


def _strategy(**overrides) -> DonchianBreakout:
    """Default test strategy with small windows for fast warmup."""
    params = dict(entry_window=5, exit_window=3)
    params.update(overrides)
    return DonchianBreakout(**params)


# ── Param validation ─────────────────────────────────────────────────────────


class TestDonchianBreakoutParams:
    def test_default_construction(self):
        s = DonchianBreakout()
        assert s.entry_window == 20
        assert s.exit_window == 10

    def test_default_order_type_is_market(self):
        assert DonchianBreakout().preferred_order_type == OrderType.MARKET

    def test_name_attribute(self):
        assert DonchianBreakout.name == "donchian_breakout"

    @pytest.mark.parametrize("kwarg", ["entry_window", "exit_window"])
    def test_windows_must_be_positive_int(self, kwarg):
        with pytest.raises(ValueError, match="positive int"):
            DonchianBreakout(**{kwarg: 0})
        with pytest.raises(ValueError, match="positive int"):
            DonchianBreakout(**{kwarg: -1})
        with pytest.raises(ValueError, match="positive int"):
            DonchianBreakout(**{kwarg: 5.5})

    def test_entry_must_exceed_exit(self):
        with pytest.raises(ValueError, match="strictly greater"):
            DonchianBreakout(entry_window=10, exit_window=10)
        with pytest.raises(ValueError, match="strictly greater"):
            DonchianBreakout(entry_window=10, exit_window=20)

    def test_required_bars_default(self):
        # Default 20 + 5 buffer
        assert DonchianBreakout().required_bars() == 25

    def test_required_bars_custom(self):
        assert DonchianBreakout(entry_window=55, exit_window=20).required_bars() == 60

    def test_repr_is_informative(self):
        r = repr(_strategy())
        assert "DonchianBreakout" in r
        assert "entry=5" in r
        assert "exit=3" in r

    def test_missing_close_column_raises(self):
        idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
        bad = pd.DataFrame({"open": [1, 2, 3]}, index=idx)
        with pytest.raises(ValueError, match="close"):
            _strategy().generate_signals(bad)


# ── Signal generation ───────────────────────────────────────────────────────


class TestDonchianBreakoutSignals:
    def test_no_entries_on_flat_series(self):
        # Constant prices → no new highs ever fire (close never > prior max).
        df = _df([10.0] * 20)
        sig = _strategy().generate_signals(df)
        assert sig.entries.sum() == 0

    def test_entry_on_new_high_after_consolidation(self):
        # Flat prices for 6 bars at 10, then a clear new high at 15.
        # entry_window=5, so prior 5-bar max at idx 6 is 10 → 15 > 10 = entry.
        closes = [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 15.0]
        df = _df(closes)
        sig = _strategy().generate_signals(df)
        assert sig.entries.iloc[6] == True  # noqa: E712
        # No earlier entries (flat).
        assert sig.entries.iloc[:6].sum() == 0

    def test_exit_on_new_low_breakdown(self):
        # Hold flat at 20, then a sharp drop to 5 — should trigger exit (new 3-bar low).
        closes = [20.0] * 6 + [5.0]
        df = _df(closes)
        sig = _strategy().generate_signals(df)
        # exit_window=3, prior 3-bar min at idx 6 is 20 → 5 < 20 = exit.
        assert sig.exits.iloc[6] == True  # noqa: E712

    def test_no_entries_on_monotonic_downtrend(self):
        # Strictly falling prices → no new highs.
        df = _df([20, 19, 18, 17, 16, 15, 14, 13, 12, 11])
        sig = _strategy().generate_signals(df)
        assert sig.entries.sum() == 0

    def test_entries_on_monotonic_uptrend(self):
        # Strictly rising prices → entry fires on every bar after warmup.
        # entry_window=5, so first valid signal is at idx 5.
        df = _df([10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20])
        sig = _strategy().generate_signals(df)
        # Bars 0..4 are warmup (NaN donchian_high). Bars 5+ should all be entries.
        assert sig.entries.iloc[:5].sum() == 0
        assert sig.entries.iloc[5:].all()

    def test_no_exits_during_warmup(self):
        # First exit_window bars cannot have a valid donchian_low.
        df = _df([20.0, 18.0, 16.0])  # only 3 bars
        sig = _strategy(entry_window=5, exit_window=3).generate_signals(df)
        # exit_window=3 with 3 bars → still NaN; no exits fire.
        assert sig.exits.sum() == 0

    def test_signals_are_bool_series(self):
        df = _df(list(range(1, 15)))
        sig = _strategy().generate_signals(df)
        assert sig.entries.dtype == bool
        assert sig.exits.dtype == bool

    def test_simultaneous_entry_exit_impossible_with_normal_data(self):
        # On real data, a single bar can't be both > prior max AND < prior min
        # (since prior max ≥ prior min). Verify our implementation respects this.
        df = _df([10, 12, 14, 16, 18, 11, 9])
        sig = _strategy().generate_signals(df)
        both = sig.entries & sig.exits
        assert not both.any()


# ── Look-ahead guard (the critical test for breakout strategies) ─────────────


class TestDonchianBreakoutLookAhead:
    """
    The truncation invariant: signals at bar t must depend only on data up to
    and including t. If we truncate input to [0:t+1] and recompute, the
    signals at 0..t must be byte-identical.

    For breakout strategies this is the most important test — a look-ahead
    leak in rolling().max() (e.g. forgetting shift(1)) would make the
    backtest look stunning and the live strategy worthless.
    """

    def test_truncating_input_preserves_past_signals(self):
        # Path with several breakouts and breakdowns.
        path = (
            list(range(10, 25))      # uptrend → many new highs
            + list(range(23, 10, -1)) # downtrend → many new lows
            + list(range(11, 22))     # uptrend again
            + list(range(20, 10, -1))  # downtrend again
        )
        df = _df(path)
        strat = _strategy(entry_window=5, exit_window=3)
        full = strat.generate_signals(df)

        for cut in range(strat.required_bars(), len(df)):
            truncated = strat.generate_signals(df.iloc[: cut + 1])
            pd.testing.assert_series_equal(
                full.entries.iloc[: cut + 1],
                truncated.entries,
                check_names=False,
            )
            pd.testing.assert_series_equal(
                full.exits.iloc[: cut + 1],
                truncated.exits,
                check_names=False,
            )


# ── Purity ───────────────────────────────────────────────────────────────────


class TestDonchianBreakoutPurity:
    def test_repeated_calls_are_deterministic(self):
        df = _df(list(range(1, 30)))
        strat = _strategy()
        a = strat.generate_signals(df)
        b = strat.generate_signals(df)
        pd.testing.assert_series_equal(a.entries, b.entries, check_names=False)
        pd.testing.assert_series_equal(a.exits, b.exits, check_names=False)

    def test_input_df_not_mutated(self):
        df = _df(list(range(1, 30)))
        original_cols = set(df.columns)
        _strategy().generate_signals(df)
        assert set(df.columns) == original_cols
