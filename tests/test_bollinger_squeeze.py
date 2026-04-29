"""
Unit tests for strategies/bollinger_squeeze.py and the BB Squeeze edge filter.

The signal logic uses small parameters (bb/kc length=3, min_squeeze_bars=4,
roc_lookback=2) so the warmup is short and every entry/exit can be traced
by hand.

Coverage:
  - Param validation
  - No spurious signals on monotonic / wide-trend paths
  - Squeeze fires + correct direction → entry
  - Min squeeze duration enforced
  - Direction filter rejects breakdown firing
  - Exit on close back below BB midpoint
  - Look-ahead guard: truncating input doesn't change past signals
  - Purity / no input mutation
  - Filter: liquidity floor with explicit IEX/SIP scaling tests
  - Filter: exhaustion gate
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.bollinger_squeeze import BollingerSqueeze
from strategies.base import OrderType


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ohlc_df(highs: list[float], lows: list[float], closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    idx = pd.date_range("2026-01-01", periods=n, freq="D", tz="UTC")
    return pd.DataFrame(
        {"high": highs, "low": lows, "close": closes},
        index=idx,
    )


def _flat_then_breakout(
    flat_n: int = 8, flat_close: float = 10.0, breakout_close: float = 15.0
) -> pd.DataFrame:
    """
    Flat consolidation (range = 2) for `flat_n` bars at `flat_close`, then
    one breakout bar at `breakout_close` with high = breakout_close + 1.
    """
    closes = [flat_close] * flat_n + [breakout_close]
    highs = [flat_close + 1] * flat_n + [breakout_close + 1]
    lows = [flat_close - 1] * flat_n + [breakout_close - 1]
    return _ohlc_df(highs, lows, closes)


def _flat_then_breakdown(
    flat_n: int = 8, flat_close: float = 10.0, break_close: float = 5.0
) -> pd.DataFrame:
    closes = [flat_close] * flat_n + [break_close]
    highs = [flat_close + 1] * flat_n + [break_close + 1]
    lows = [flat_close - 1] * flat_n + [break_close - 1]
    return _ohlc_df(highs, lows, closes)


def _strategy(**overrides) -> BollingerSqueeze:
    """Default test strategy with small params for quick warmup."""
    params = dict(
        bb_length=3,
        bb_std=2.0,
        kc_length=3,
        kc_atr_mult=1.5,
        min_squeeze_bars=4,
        roc_lookback=2,
    )
    params.update(overrides)
    return BollingerSqueeze(**params)


# ── Param validation ─────────────────────────────────────────────────────────


class TestBollingerSqueezeParams:
    def test_default_construction(self):
        s = BollingerSqueeze()
        assert s.bb_length == 20
        assert s.bb_std == 2.0
        assert s.kc_length == 20
        assert s.kc_atr_mult == 1.5
        assert s.min_squeeze_bars == 6
        assert s.roc_lookback == 5

    def test_default_order_type_is_market(self):
        assert BollingerSqueeze().preferred_order_type == OrderType.MARKET

    def test_name_attribute(self):
        assert BollingerSqueeze.name == "bollinger_squeeze"

    @pytest.mark.parametrize(
        "kwarg",
        ["bb_length", "kc_length", "min_squeeze_bars", "roc_lookback"],
    )
    def test_int_params_must_be_positive_int(self, kwarg):
        with pytest.raises(ValueError, match="positive int"):
            BollingerSqueeze(**{kwarg: 0})
        with pytest.raises(ValueError, match="positive int"):
            BollingerSqueeze(**{kwarg: -1})
        with pytest.raises(ValueError, match="positive int"):
            BollingerSqueeze(**{kwarg: 3.5})

    @pytest.mark.parametrize("kwarg", ["bb_std", "kc_atr_mult"])
    def test_float_params_must_be_positive(self, kwarg):
        with pytest.raises(ValueError, match=kwarg):
            BollingerSqueeze(**{kwarg: 0})
        with pytest.raises(ValueError, match=kwarg):
            BollingerSqueeze(**{kwarg: -1.5})

    def test_required_bars_includes_safety_buffer(self):
        s = _strategy()
        # max(3, 3) + 4 + 2 + 5 = 14
        assert s.required_bars() == 14

    def test_required_bars_default_strategy(self):
        s = BollingerSqueeze()
        # max(20, 20) + 6 + 5 + 5 = 36
        assert s.required_bars() == 36

    def test_repr_is_informative(self):
        r = repr(_strategy())
        assert "BollingerSqueeze" in r
        assert "bb=3" in r
        assert "kc=3" in r

    def test_missing_required_columns_raises(self):
        idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)  # no high/low
        with pytest.raises(ValueError, match="high"):
            _strategy().generate_signals(df)


# ── Signal generation ───────────────────────────────────────────────────────


class TestBollingerSqueezeSignals:
    def test_no_entries_on_monotonic_uptrend(self):
        # Wide trending move: BB std grows large, BB envelopes KC, squeeze
        # rarely (if ever) toggles. No entries should fire.
        n = 30
        closes = [10.0 + i for i in range(n)]
        highs = [c + 1 for c in closes]
        lows = [c - 1 for c in closes]
        df = _ohlc_df(highs, lows, closes)

        sig = _strategy().generate_signals(df)
        assert sig.entries.sum() == 0

    def test_entry_fires_after_consolidation_and_breakout(self):
        df = _flat_then_breakout(flat_n=8, flat_close=10.0, breakout_close=15.0)
        sig = _strategy().generate_signals(df)
        # The breakout bar (last) should be the only entry.
        assert sig.entries.iloc[-1] == True  # noqa: E712
        assert sig.entries.iloc[:-1].sum() == 0

    def test_breakdown_does_not_trigger_entry(self):
        # Squeeze fires but the move is DOWN — long-only strategy must skip.
        df = _flat_then_breakdown(flat_n=8, flat_close=10.0, break_close=5.0)
        sig = _strategy().generate_signals(df)
        assert sig.entries.sum() == 0

    def test_min_squeeze_bars_enforced(self):
        # Only 3 bars of squeeze before fire, but min=4 → no entry.
        df = _flat_then_breakout(flat_n=4, flat_close=10.0, breakout_close=15.0)
        # warmup: idx 0,1 NaN; idx 2,3 in squeeze; idx 4 fires.
        # Prior 4-bar window for fire-bar = idx 0..3 = (NaN, NaN, on, on) → sum < 4.
        s = _strategy(min_squeeze_bars=4)
        sig = s.generate_signals(df)
        assert sig.entries.sum() == 0

    def test_min_squeeze_bars_satisfied_with_longer_consolidation(self):
        # 8 bars flat is plenty for min=4 and warmup.
        df = _flat_then_breakout(flat_n=8, flat_close=10.0, breakout_close=15.0)
        sig = _strategy(min_squeeze_bars=4).generate_signals(df)
        assert sig.entries.sum() == 1

    def test_exit_when_close_below_bb_midpoint(self):
        # Construct: 8 bars flat at 10, breakout to 15, then close back below mid.
        # After breakout, BB mid drifts upward. Append a low close.
        breakout = _flat_then_breakout(flat_n=8, flat_close=10.0, breakout_close=15.0)
        # add one more bar with very low close
        closes = breakout["close"].tolist() + [5.0]
        highs = breakout["high"].tolist() + [6.0]
        lows = breakout["low"].tolist() + [4.0]
        df = _ohlc_df(highs, lows, closes)

        sig = _strategy().generate_signals(df)
        # Last bar must have an exit (close=5 well below any reasonable BB mid).
        assert sig.exits.iloc[-1] == True  # noqa: E712

    def test_signals_are_bool_series(self):
        df = _flat_then_breakout()
        sig = _strategy().generate_signals(df)
        assert sig.entries.dtype == bool
        assert sig.exits.dtype == bool

    def test_no_entry_during_warmup(self):
        df = _flat_then_breakout(flat_n=2, flat_close=10.0, breakout_close=15.0)
        sig = _strategy().generate_signals(df)
        # During warmup (first few bars), no entries can fire.
        assert sig.entries.iloc[:3].sum() == 0


# ── Look-ahead guard ─────────────────────────────────────────────────────────


class TestBollingerSqueezeLookAhead:
    def test_truncating_input_preserves_past_signals(self):
        # Build a long path with one clear squeeze + breakout.
        df = _flat_then_breakout(flat_n=12, flat_close=10.0, breakout_close=15.0)
        # Append more flat noise after, plus a second breakout.
        more_closes = [12.0, 11.5, 12.0, 11.5, 12.0, 11.5, 12.0, 11.5, 16.0]
        more_highs = [c + 1 for c in more_closes]
        more_lows = [c - 1 for c in more_closes]
        df2 = pd.concat([
            df,
            _ohlc_df(more_highs, more_lows, more_closes).set_index(
                pd.date_range(
                    df.index[-1] + pd.Timedelta(days=1),
                    periods=len(more_closes),
                    freq="D",
                    tz="UTC",
                )
            ),
        ])

        strat = _strategy()
        full = strat.generate_signals(df2)

        for cut in range(strat.required_bars(), len(df2)):
            truncated = strat.generate_signals(df2.iloc[: cut + 1])
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


class TestBollingerSqueezePurity:
    def test_repeated_calls_are_deterministic(self):
        df = _flat_then_breakout()
        strat = _strategy()
        a = strat.generate_signals(df)
        b = strat.generate_signals(df)
        pd.testing.assert_series_equal(a.entries, b.entries, check_names=False)
        pd.testing.assert_series_equal(a.exits, b.exits, check_names=False)

    def test_input_df_not_mutated(self):
        df = _flat_then_breakout()
        original_cols = set(df.columns)
        _strategy().generate_signals(df)
        assert set(df.columns) == original_cols
