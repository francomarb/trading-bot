"""
Unit tests for indicators/technicals.py.

All expected values are hand-computed from the definitions so these tests
function as a specification, not just a regression suite.

References:
  SMA(n) = mean of last n closes.
  EMA(n): alpha = 2/(n+1). Seed = SMA of first n values at index n-1.
          Recurrence: ema_t = alpha * x_t + (1 - alpha) * ema_{t-1}.
  TR_t  = max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|);  TR_0 = H_0 - L_0.
  ATR(n): first ATR at index n-1 = mean(TR_0 ... TR_{n-1}).
          ATR_t = (ATR_{t-1} * (n - 1) + TR_t) / n.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from indicators.technicals import add_atr, add_ema, add_sma


# ── Helpers ──────────────────────────────────────────────────────────────────


def _close_series(values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"close": values})


def _ohlc(
    highs: list[float], lows: list[float], closes: list[float]
) -> pd.DataFrame:
    return pd.DataFrame({"high": highs, "low": lows, "close": closes})


def _approx_equal(actual: list, expected: list, tol: float = 1e-9) -> None:
    assert len(actual) == len(expected), f"length {len(actual)} vs {len(expected)}"
    for i, (a, e) in enumerate(zip(actual, expected)):
        if e is None or (isinstance(e, float) and math.isnan(e)):
            assert a is None or (isinstance(a, float) and math.isnan(a)), (
                f"idx {i}: expected NaN, got {a!r}"
            )
        else:
            assert isinstance(a, float) and not math.isnan(a), f"idx {i}: unexpected NaN"
            assert abs(a - e) < tol, f"idx {i}: expected {e}, got {a} (tol={tol})"


# ── SMA ──────────────────────────────────────────────────────────────────────


class TestSMA:
    def test_basic_values(self):
        df = _close_series([1, 2, 3, 4, 5])
        result = add_sma(df, 3)
        # sma[2] = (1+2+3)/3 = 2.0, sma[3] = (2+3+4)/3 = 3.0, sma[4] = 4.0
        _approx_equal(
            result["sma_3"].tolist(), [float("nan"), float("nan"), 2.0, 3.0, 4.0]
        )

    def test_input_not_mutated(self):
        df = _close_series([1, 2, 3, 4, 5])
        add_sma(df, 3)
        assert "sma_3" not in df.columns

    def test_column_name_includes_length(self):
        df = _close_series([1, 2, 3, 4, 5])
        assert "sma_20" in add_sma(df, 20).columns
        assert "sma_50" in add_sma(df, 50).columns

    def test_multiple_windows_coexist(self):
        df = _close_series(list(range(1, 11)))
        df = add_sma(df, 3)
        df = add_sma(df, 5)
        assert "sma_3" in df.columns and "sma_5" in df.columns

    def test_length_longer_than_data_gives_all_nan(self):
        df = _close_series([1, 2, 3])
        result = add_sma(df, 5)
        assert result["sma_5"].isna().all()

    def test_length_equal_to_data_gives_one_value(self):
        df = _close_series([2, 4, 6])
        result = add_sma(df, 3)
        _approx_equal(result["sma_3"].tolist(), [float("nan"), float("nan"), 4.0])

    def test_custom_source_column(self):
        df = pd.DataFrame({"close": [1, 2, 3], "open": [10, 20, 30]})
        result = add_sma(df, 2, source="open")
        # sma_2 on open = [nan, 15, 25]
        _approx_equal(result["sma_2"].tolist(), [float("nan"), 15.0, 25.0])

    def test_missing_source_column_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="missing required columns"):
            add_sma(df, 2, source="open")

    def test_invalid_length_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="positive int"):
            add_sma(df, 0)
        with pytest.raises(ValueError, match="positive int"):
            add_sma(df, -1)


# ── EMA ──────────────────────────────────────────────────────────────────────


class TestEMA:
    def test_basic_values(self):
        # length=3, alpha=0.5, data=[1,2,3,4,5]
        # seed at idx 2 = SMA(1,2,3) = 2.0
        # ema[3] = 0.5*4 + 0.5*2.0 = 3.0
        # ema[4] = 0.5*5 + 0.5*3.0 = 4.0
        df = _close_series([1, 2, 3, 4, 5])
        result = add_ema(df, 3)
        _approx_equal(
            result["ema_3"].tolist(), [float("nan"), float("nan"), 2.0, 3.0, 4.0]
        )

    def test_constant_series_yields_constant_ema(self):
        df = _close_series([5, 5, 5, 5, 5])
        result = add_ema(df, 3)
        # After seed all values = 5.
        _approx_equal(
            result["ema_3"].tolist(), [float("nan"), float("nan"), 5.0, 5.0, 5.0]
        )

    def test_alpha_for_length_2(self):
        # length=2, alpha = 2/3. data=[10, 20, 40]. seed at idx 1 = 15.
        # ema[2] = (2/3)*40 + (1/3)*15 = 26.6666...+5 = 31.6666...
        df = _close_series([10, 20, 40])
        result = add_ema(df, 2)
        _approx_equal(
            result["ema_2"].tolist(),
            [float("nan"), 15.0, (2 / 3) * 40 + (1 / 3) * 15],
        )

    def test_length_longer_than_data_gives_all_nan(self):
        df = _close_series([1, 2])
        result = add_ema(df, 5)
        assert result["ema_5"].isna().all()

    def test_input_not_mutated(self):
        df = _close_series([1, 2, 3, 4, 5])
        add_ema(df, 3)
        assert "ema_3" not in df.columns

    def test_missing_source_column_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="missing required columns"):
            add_ema(df, 2, source="nope")

    def test_invalid_length_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="positive int"):
            add_ema(df, 0)


# ── ATR ──────────────────────────────────────────────────────────────────────


class TestATR:
    def test_basic_values_n3(self):
        """
        Hand-worked example, length=3.

        bars:  H    L    C
        idx 0: 10,  9,   9.5
        idx 1: 12,  10,  11       prev_close=9.5
        idx 2: 13,  11,  12       prev_close=11
        idx 3: 12,  10,  10.5     prev_close=12
        idx 4: 14,  11,  13       prev_close=10.5

        TR: 1.0, 2.5, 2.0, 2.0, 3.5   (TR_0 = H-L = 1.0)

        ATR(3):
          idx 0,1 = NaN
          idx 2 = mean(1.0, 2.5, 2.0) = 5.5/3 = 1.8333...
          idx 3 = (1.8333... * 2 + 2.0) / 3 = (3.6666... + 2) / 3 = 1.8888...
          idx 4 = (1.8888... * 2 + 3.5) / 3 = (3.7777... + 3.5) / 3 = 2.4259...
        """
        df = _ohlc(
            highs=[10, 12, 13, 12, 14],
            lows=[9, 10, 11, 10, 11],
            closes=[9.5, 11, 12, 10.5, 13],
        )
        result = add_atr(df, 3)

        first_atr = 5.5 / 3
        second_atr = (first_atr * 2 + 2.0) / 3
        third_atr = (second_atr * 2 + 3.5) / 3

        _approx_equal(
            result["atr_3"].tolist(),
            [float("nan"), float("nan"), first_atr, second_atr, third_atr],
            tol=1e-9,
        )

    def test_constant_range_yields_constant_atr(self):
        # Every bar has H-L=2 and no gaps, so TR=2 everywhere; ATR converges to 2.
        df = _ohlc(
            highs=[12, 12, 12, 12, 12],
            lows=[10, 10, 10, 10, 10],
            closes=[11, 11, 11, 11, 11],
        )
        result = add_atr(df, 3)
        expected = [float("nan"), float("nan"), 2.0, 2.0, 2.0]
        _approx_equal(result["atr_3"].tolist(), expected)

    def test_atr_is_nonnegative(self):
        df = _ohlc(
            highs=[10, 11, 12, 11, 10, 13, 14, 12, 11, 10],
            lows=[9, 10, 11, 10, 9, 11, 13, 11, 10, 9],
            closes=[9.5, 10.5, 11.5, 10.5, 9.5, 12, 13.5, 11.5, 10.5, 9.5],
        )
        result = add_atr(df, 4)
        vals = result["atr_4"].dropna().tolist()
        assert all(v >= 0 for v in vals), "ATR must be non-negative"
        assert len(vals) > 0

    def test_length_longer_than_data_gives_all_nan(self):
        df = _ohlc(highs=[10, 11], lows=[9, 10], closes=[9.5, 10.5])
        result = add_atr(df, 5)
        assert result["atr_5"].isna().all()

    def test_input_not_mutated(self):
        df = _ohlc(highs=[10, 11, 12], lows=[9, 10, 11], closes=[9.5, 10.5, 11.5])
        add_atr(df, 2)
        assert "atr_2" not in df.columns

    def test_missing_ohlc_columns_raise(self):
        df = pd.DataFrame({"close": [1, 2, 3]})  # no high/low
        with pytest.raises(ValueError, match="missing required columns"):
            add_atr(df, 2)

    def test_default_length_is_14(self):
        df = _ohlc(
            highs=[10 + i for i in range(20)],
            lows=[9 + i for i in range(20)],
            closes=[9.5 + i for i in range(20)],
        )
        result = add_atr(df)  # default length
        assert "atr_14" in result.columns

    def test_invalid_length_raises(self):
        df = _ohlc(highs=[10], lows=[9], closes=[9.5])
        with pytest.raises(ValueError, match="positive int"):
            add_atr(df, 0)


# ── Cross-cutting: indicators stack on a single DataFrame ────────────────────


class TestCombined:
    def test_indicators_stack_predictably(self):
        df = pd.DataFrame(
            {
                "high": [10, 11, 12, 13, 14, 15, 16, 17, 18, 19],
                "low": [9, 10, 11, 12, 13, 14, 15, 16, 17, 18],
                "close": [9.5, 10.5, 11.5, 12.5, 13.5, 14.5, 15.5, 16.5, 17.5, 18.5],
            }
        )
        df = add_sma(df, 3)
        df = add_sma(df, 5)
        df = add_ema(df, 3)
        df = add_atr(df, 3)

        for col in ["sma_3", "sma_5", "ema_3", "atr_3"]:
            assert col in df.columns

        # Original columns preserved.
        for col in ["high", "low", "close"]:
            assert col in df.columns
