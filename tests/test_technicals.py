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

from indicators.technicals import (
    add_atr,
    add_bollinger_bands,
    add_donchian_high,
    add_donchian_low,
    add_ema,
    add_keltner_channels,
    add_rsi,
    add_sma,
)


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


# ── RSI ──────────────────────────────────────────────────────────────────────


class TestRSI:
    def test_basic_values_n3(self):
        """
        Hand-worked example, length=3.

        closes: 44, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10
        changes:    +0.34, -0.25, -0.48, +0.72, +0.50, +0.27

        First 3 changes (indices 1-3): +0.34, -0.25, -0.48
          avg_gain = (0.34 + 0 + 0) / 3 = 0.1133...
          avg_loss = (0 + 0.25 + 0.48) / 3 = 0.2433...
          RS = 0.1133 / 0.2433 = 0.46575...
          RSI[3] = 100 - 100 / (1 + 0.46575) = 31.81...

        idx 4: change = +0.72 → gain=0.72, loss=0
          avg_gain = (0.1133 * 2 + 0.72) / 3 = 0.3155...
          avg_loss = (0.2433 * 2 + 0.0) / 3 = 0.1622...
          RS = 0.3155 / 0.1622 = 1.9452...
          RSI[4] = 100 - 100 / (1 + 1.9452) = 66.05...
        """
        closes = [44, 44.34, 44.09, 43.61, 44.33, 44.83, 45.10]
        df = _close_series(closes)
        result = add_rsi(df, 3)

        rsi = result["rsi_3"].tolist()
        assert all(math.isnan(v) for v in rsi[:3]), "first 3 values must be NaN"

        avg_gain_0 = 0.34 / 3
        avg_loss_0 = (0.25 + 0.48) / 3
        rs_0 = avg_gain_0 / avg_loss_0
        expected_3 = 100 - 100 / (1 + rs_0)
        assert abs(rsi[3] - expected_3) < 1e-6

        avg_gain_1 = (avg_gain_0 * 2 + 0.72) / 3
        avg_loss_1 = (avg_loss_0 * 2 + 0.0) / 3
        rs_1 = avg_gain_1 / avg_loss_1
        expected_4 = 100 - 100 / (1 + rs_1)
        assert abs(rsi[4] - expected_4) < 1e-6

    def test_constant_prices_yield_nan_then_50(self):
        """When all prices are the same, gains=losses=0; RSI should be 100 (all-gains edge case)."""
        # Actually: avg_gain=0 and avg_loss=0 → division by zero.
        # Convention: if avg_loss=0 → RSI=100.
        df = _close_series([50.0] * 10)
        result = add_rsi(df, 3)
        rsi = result["rsi_3"].tolist()
        assert all(math.isnan(v) for v in rsi[:3])
        # avg_gain=0, avg_loss=0 → RSI=100
        assert rsi[3] == 100.0

    def test_monotonic_up_yields_100(self):
        df = _close_series(list(range(1, 20)))
        result = add_rsi(df, 5)
        rsi_vals = result["rsi_5"].dropna().tolist()
        assert all(v == 100.0 for v in rsi_vals), "pure uptrend → RSI=100"

    def test_monotonic_down_yields_0(self):
        df = _close_series(list(range(20, 0, -1)))
        result = add_rsi(df, 5)
        rsi_vals = result["rsi_5"].dropna().tolist()
        assert all(v == 0.0 for v in rsi_vals), "pure downtrend → RSI=0"

    def test_rsi_bounded_0_100(self):
        closes = [10, 12, 9, 14, 8, 15, 7, 16, 9, 13, 11, 10, 15, 8, 12]
        df = _close_series(closes)
        result = add_rsi(df, 5)
        rsi_vals = result["rsi_5"].dropna().tolist()
        assert all(0 <= v <= 100 for v in rsi_vals)

    def test_length_longer_than_data_gives_all_nan(self):
        df = _close_series([10, 11, 12])
        result = add_rsi(df, 5)
        assert result["rsi_5"].isna().all()

    def test_input_not_mutated(self):
        df = _close_series([10, 11, 12, 13, 14, 15])
        add_rsi(df, 3)
        assert "rsi_3" not in df.columns

    def test_default_length_is_14(self):
        df = _close_series(list(range(1, 25)))
        result = add_rsi(df)
        assert "rsi_14" in result.columns

    def test_missing_source_column_raises(self):
        df = pd.DataFrame({"open": [1, 2, 3]})
        with pytest.raises(ValueError, match="missing required columns"):
            add_rsi(df, 2)

    def test_invalid_length_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="positive int"):
            add_rsi(df, 0)


# ── Bollinger Bands ──────────────────────────────────────────────────────────


class TestBollingerBands:
    def test_basic_values_n3_std2(self):
        """
        length=3, std_dev=2, closes=[1, 2, 3, 4, 5]

        Population std (ddof=0) of any 3 consecutive integers k, k+1, k+2:
          variance = ((k - (k+1))^2 + 0 + ((k+2) - (k+1))^2) / 3 = 2/3
          std = sqrt(2/3) ≈ 0.81650

        At idx 2: mid = 2,   upper = 2 + 2*sqrt(2/3),   lower = 2 - 2*sqrt(2/3)
        At idx 3: mid = 3,   upper = 3 + 2*sqrt(2/3),   lower = 3 - 2*sqrt(2/3)
        At idx 4: mid = 4,   upper = 4 + 2*sqrt(2/3),   lower = 4 - 2*sqrt(2/3)
        """
        df = _close_series([1, 2, 3, 4, 5])
        result = add_bollinger_bands(df, length=3, std_dev=2.0)

        std = math.sqrt(2 / 3)

        _approx_equal(
            result["bb_mid_3"].tolist(),
            [float("nan"), float("nan"), 2.0, 3.0, 4.0],
        )
        _approx_equal(
            result["bb_upper_3_2"].tolist(),
            [float("nan"), float("nan"), 2 + 2 * std, 3 + 2 * std, 4 + 2 * std],
        )
        _approx_equal(
            result["bb_lower_3_2"].tolist(),
            [float("nan"), float("nan"), 2 - 2 * std, 3 - 2 * std, 4 - 2 * std],
        )

    def test_constant_series_zero_width(self):
        """Constant prices → std=0 → upper == mid == lower."""
        df = _close_series([5, 5, 5, 5, 5])
        result = add_bollinger_bands(df, length=3, std_dev=2.0)
        # First two NaN, then bands collapse to mid.
        for col in ("bb_mid_3", "bb_upper_3_2", "bb_lower_3_2"):
            vals = result[col].tolist()
            assert math.isnan(vals[0]) and math.isnan(vals[1])
            assert all(v == 5.0 for v in vals[2:])

    def test_column_names_with_fractional_std(self):
        df = _close_series([1, 2, 3, 4, 5])
        result = add_bollinger_bands(df, length=3, std_dev=1.5)
        assert "bb_mid_3" in result.columns
        assert "bb_upper_3_1.5" in result.columns
        assert "bb_lower_3_1.5" in result.columns

    def test_default_params(self):
        df = _close_series(list(range(1, 30)))
        result = add_bollinger_bands(df)  # length=20, std_dev=2.0
        assert "bb_mid_20" in result.columns
        assert "bb_upper_20_2" in result.columns
        assert "bb_lower_20_2" in result.columns

    def test_input_not_mutated(self):
        df = _close_series([1, 2, 3, 4, 5])
        add_bollinger_bands(df, length=3)
        for col in ("bb_mid_3", "bb_upper_3_2", "bb_lower_3_2"):
            assert col not in df.columns

    def test_length_longer_than_data_gives_all_nan(self):
        df = _close_series([1, 2, 3])
        result = add_bollinger_bands(df, length=5)
        assert result["bb_mid_5"].isna().all()
        assert result["bb_upper_5_2"].isna().all()
        assert result["bb_lower_5_2"].isna().all()

    def test_upper_above_mid_above_lower_when_volatile(self):
        df = _close_series([1, 5, 2, 8, 3, 9, 4, 7, 6, 10])
        result = add_bollinger_bands(df, length=3, std_dev=2.0)
        usable = result.dropna()
        assert (usable["bb_upper_3_2"] >= usable["bb_mid_3"]).all()
        assert (usable["bb_mid_3"] >= usable["bb_lower_3_2"]).all()

    def test_missing_source_column_raises(self):
        df = pd.DataFrame({"open": [1, 2, 3]})
        with pytest.raises(ValueError, match="missing required columns"):
            add_bollinger_bands(df, length=2)

    def test_invalid_length_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="positive int"):
            add_bollinger_bands(df, length=0)

    def test_invalid_std_dev_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="std_dev"):
            add_bollinger_bands(df, length=2, std_dev=0)
        with pytest.raises(ValueError, match="std_dev"):
            add_bollinger_bands(df, length=2, std_dev=-1.0)


# ── Keltner Channels ─────────────────────────────────────────────────────────


class TestKeltnerChannels:
    def test_basic_values_n3_mult1_5(self):
        """
        Hand-worked example, length=3, atr_mult=1.5

        OHLC (matches TestATR.test_basic_values_n3):
          highs=[10, 12, 13, 12, 14]
          lows =[9,  10, 11, 10, 11]
          closes=[9.5, 11, 12, 10.5, 13]

        ATR(3): nan, nan, 5.5/3, ...   (computed in TestATR)
        EMA(3) of closes (alpha=0.5):
          seed at idx 2 = (9.5 + 11 + 12) / 3 = 32.5/3
          ema[3] = 0.5*10.5 + 0.5*(32.5/3) = 5.25 + (32.5/6)
          ema[4] = 0.5*13 + 0.5*ema[3]

        KC at idx 2:
          mid   = ema[2]   = 32.5/3
          upper = mid + 1.5 * 5.5/3
          lower = mid - 1.5 * 5.5/3
        """
        df = _ohlc(
            highs=[10, 12, 13, 12, 14],
            lows=[9, 10, 11, 10, 11],
            closes=[9.5, 11, 12, 10.5, 13],
        )
        result = add_keltner_channels(df, length=3, atr_mult=1.5)

        # Reproduce the EMA seed/recurrence
        ema_seed = (9.5 + 11 + 12) / 3
        ema_3 = 0.5 * 10.5 + 0.5 * ema_seed
        ema_4 = 0.5 * 13 + 0.5 * ema_3

        atr_2 = 5.5 / 3
        atr_3 = (atr_2 * 2 + 2.0) / 3
        atr_4 = (atr_3 * 2 + 3.5) / 3

        _approx_equal(
            result["kc_mid_3"].tolist(),
            [float("nan"), float("nan"), ema_seed, ema_3, ema_4],
        )
        _approx_equal(
            result["kc_upper_3_1.5"].tolist(),
            [
                float("nan"), float("nan"),
                ema_seed + 1.5 * atr_2,
                ema_3 + 1.5 * atr_3,
                ema_4 + 1.5 * atr_4,
            ],
        )
        _approx_equal(
            result["kc_lower_3_1.5"].tolist(),
            [
                float("nan"), float("nan"),
                ema_seed - 1.5 * atr_2,
                ema_3 - 1.5 * atr_3,
                ema_4 - 1.5 * atr_4,
            ],
        )

    def test_constant_range_yields_constant_kc(self):
        df = _ohlc(
            highs=[12, 12, 12, 12, 12],
            lows=[10, 10, 10, 10, 10],
            closes=[11, 11, 11, 11, 11],
        )
        result = add_keltner_channels(df, length=3, atr_mult=2.0)
        # Mid converges to 11 (constant close), ATR=2, so upper=15, lower=7.
        for v in result["kc_mid_3"].dropna():
            assert v == 11.0
        for v in result["kc_upper_3_2"].dropna():
            assert v == 15.0
        for v in result["kc_lower_3_2"].dropna():
            assert v == 7.0

    def test_default_params(self):
        df = _ohlc(
            highs=[10 + i for i in range(30)],
            lows=[9 + i for i in range(30)],
            closes=[9.5 + i for i in range(30)],
        )
        result = add_keltner_channels(df)  # length=20, atr_mult=1.5
        assert "kc_mid_20" in result.columns
        assert "kc_upper_20_1.5" in result.columns
        assert "kc_lower_20_1.5" in result.columns

    def test_input_not_mutated(self):
        df = _ohlc(
            highs=[10, 11, 12, 13],
            lows=[9, 10, 11, 12],
            closes=[9.5, 10.5, 11.5, 12.5],
        )
        add_keltner_channels(df, length=2, atr_mult=1.5)
        for col in ("kc_mid_2", "kc_upper_2_1.5", "kc_lower_2_1.5"):
            assert col not in df.columns

    def test_length_longer_than_data_gives_all_nan(self):
        df = _ohlc(highs=[10, 11], lows=[9, 10], closes=[9.5, 10.5])
        result = add_keltner_channels(df, length=5)
        for col in ("kc_mid_5", "kc_upper_5_1.5", "kc_lower_5_1.5"):
            assert result[col].isna().all()

    def test_missing_ohlc_columns_raise(self):
        df = pd.DataFrame({"close": [1, 2, 3]})
        with pytest.raises(ValueError, match="missing required columns"):
            add_keltner_channels(df, length=2)

    def test_invalid_length_raises(self):
        df = _ohlc(highs=[10], lows=[9], closes=[9.5])
        with pytest.raises(ValueError, match="positive int"):
            add_keltner_channels(df, length=0)

    def test_invalid_atr_mult_raises(self):
        df = _ohlc(highs=[10, 11, 12], lows=[9, 10, 11], closes=[9.5, 10.5, 11.5])
        with pytest.raises(ValueError, match="atr_mult"):
            add_keltner_channels(df, length=2, atr_mult=0)
        with pytest.raises(ValueError, match="atr_mult"):
            add_keltner_channels(df, length=2, atr_mult=-1.0)


# ── Donchian high / low ──────────────────────────────────────────────────────


class TestDonchianHigh:
    def test_basic_values_n3(self):
        """
        length=3, closes=[10, 12, 11, 14, 13, 16].

        donchian_high_3[t] = max(close[t-3], close[t-2], close[t-1])
          t=0,1,2 → NaN (insufficient prior bars)
          t=3 → max(10, 12, 11) = 12
          t=4 → max(12, 11, 14) = 14
          t=5 → max(11, 14, 13) = 14
        """
        df = _close_series([10, 12, 11, 14, 13, 16])
        result = add_donchian_high(df, 3)
        _approx_equal(
            result["donchian_high_3"].tolist(),
            [float("nan"), float("nan"), float("nan"), 12.0, 14.0, 14.0],
        )

    def test_excludes_current_bar(self):
        """Critical look-ahead test: today's close must NOT be in the rolling max."""
        # If today were included, donchian_high_3[3] would be max(12, 11, 14) = 14 (wrong)
        # Correct: donchian_high_3[3] = max(close[0..2]) = max(10, 12, 11) = 12
        df = _close_series([10, 12, 11, 14])
        result = add_donchian_high(df, 3)
        assert result["donchian_high_3"].iloc[3] == 12.0

    def test_monotonic_uptrend(self):
        # Each new bar strictly higher → donchian_high always equals close[t-1].
        df = _close_series([1, 2, 3, 4, 5, 6, 7])
        result = add_donchian_high(df, 3)
        # NaN, NaN, NaN, max(1,2,3)=3, max(2,3,4)=4, max(3,4,5)=5, max(4,5,6)=6
        _approx_equal(
            result["donchian_high_3"].tolist(),
            [float("nan"), float("nan"), float("nan"), 3.0, 4.0, 5.0, 6.0],
        )

    def test_truncating_input_preserves_past_values(self):
        """Look-ahead guard: truncating must not change earlier values."""
        df = _close_series([5, 8, 6, 12, 10, 9, 15, 14, 18])
        full = add_donchian_high(df, 3)["donchian_high_3"].tolist()

        for cut in range(4, len(df)):
            partial = add_donchian_high(df.iloc[: cut + 1], 3)["donchian_high_3"].tolist()
            _approx_equal(partial, full[: cut + 1])

    def test_input_not_mutated(self):
        df = _close_series([1, 2, 3, 4, 5])
        add_donchian_high(df, 3)
        assert "donchian_high_3" not in df.columns

    def test_custom_source(self):
        df = pd.DataFrame({"close": [1, 2, 3, 4], "high": [10, 20, 30, 40]})
        result = add_donchian_high(df, 2, source="high")
        # NaN, NaN, max(10,20)=20, max(20,30)=30
        _approx_equal(
            result["donchian_high_2"].tolist(),
            [float("nan"), float("nan"), 20.0, 30.0],
        )

    def test_length_longer_than_data_gives_all_nan(self):
        df = _close_series([1, 2, 3])
        result = add_donchian_high(df, 5)
        assert result["donchian_high_5"].isna().all()

    def test_invalid_length_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="positive int"):
            add_donchian_high(df, 0)
        with pytest.raises(ValueError, match="positive int"):
            add_donchian_high(df, -1)

    def test_missing_source_column_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="missing required columns"):
            add_donchian_high(df, 2, source="open")


class TestDonchianLow:
    def test_basic_values_n3(self):
        """
        length=3, closes=[10, 12, 11, 8, 13, 6].
          t=0,1,2 → NaN
          t=3 → min(10, 12, 11) = 10
          t=4 → min(12, 11, 8)  = 8
          t=5 → min(11, 8, 13)  = 8
        """
        df = _close_series([10, 12, 11, 8, 13, 6])
        result = add_donchian_low(df, 3)
        _approx_equal(
            result["donchian_low_3"].tolist(),
            [float("nan"), float("nan"), float("nan"), 10.0, 8.0, 8.0],
        )

    def test_excludes_current_bar(self):
        """Today's close must NOT be in the rolling min."""
        df = _close_series([10, 12, 11, 5])  # today=5; if included, min would be 5
        result = add_donchian_low(df, 3)
        # Correct: min(close[0..2]) = min(10, 12, 11) = 10
        assert result["donchian_low_3"].iloc[3] == 10.0

    def test_monotonic_downtrend(self):
        df = _close_series([10, 9, 8, 7, 6, 5, 4])
        result = add_donchian_low(df, 3)
        # NaN×3, then min of prior 3 values each step.
        _approx_equal(
            result["donchian_low_3"].tolist(),
            [float("nan"), float("nan"), float("nan"), 8.0, 7.0, 6.0, 5.0],
        )

    def test_truncating_input_preserves_past_values(self):
        df = _close_series([15, 12, 14, 8, 10, 11, 5, 7, 9])
        full = add_donchian_low(df, 3)["donchian_low_3"].tolist()
        for cut in range(4, len(df)):
            partial = add_donchian_low(df.iloc[: cut + 1], 3)["donchian_low_3"].tolist()
            _approx_equal(partial, full[: cut + 1])

    def test_input_not_mutated(self):
        df = _close_series([1, 2, 3, 4, 5])
        add_donchian_low(df, 3)
        assert "donchian_low_3" not in df.columns

    def test_length_longer_than_data_gives_all_nan(self):
        df = _close_series([1, 2, 3])
        result = add_donchian_low(df, 5)
        assert result["donchian_low_5"].isna().all()

    def test_invalid_length_raises(self):
        df = _close_series([1, 2, 3])
        with pytest.raises(ValueError, match="positive int"):
            add_donchian_low(df, 0)


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
        df = add_rsi(df, 3)

        for col in ["sma_3", "sma_5", "ema_3", "atr_3", "rsi_3"]:
            assert col in df.columns

        # Original columns preserved.
        for col in ["high", "low", "close"]:
            assert col in df.columns
