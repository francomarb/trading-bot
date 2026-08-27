"""Contract tests for the signal-only leveraged trend research strategy."""

from __future__ import annotations

import pandas as pd
import pytest

from backtest.leveraged_trend import (
    LeveragedPair,
    aggregate_parameter_study,
    build_pair_frame,
    buy_and_hold_study,
    run_pair_backtest,
)
from backtest.runner import BacktestConfig
from strategies.leveraged_trend import LeveragedTrend


def _frame(signal_closes: list[float], trading_closes: list[float] | None = None):
    """Build a composite daily frame with independently controlled prices."""
    trading_closes = trading_closes or [value * 3 for value in signal_closes]
    index = pd.bdate_range("2025-01-02", periods=len(signal_closes), tz="UTC")
    return pd.DataFrame(
        {
            "open": trading_closes,
            "high": [value * 1.01 for value in trading_closes],
            "low": [value * 0.99 for value in trading_closes],
            "close": trading_closes,
            "volume": [1_000_000] * len(signal_closes),
            "signal_close": signal_closes,
        },
        index=index,
    )


class TestLeveragedTrendValidation:
    """Configuration and input failures must be explicit."""

    @pytest.mark.parametrize("field", ["sma_length", "entry_days", "exit_days"])
    def test_rejects_non_positive_parameters(self, field):
        kwargs = {"sma_length": 2, "entry_days": 3, "exit_days": 2}
        kwargs[field] = 0
        with pytest.raises(ValueError, match="positive"):
            LeveragedTrend(**kwargs)

    @pytest.mark.parametrize("field", ["sma_length", "entry_days", "exit_days"])
    def test_rejects_non_integer_parameters(self, field):
        kwargs = {"sma_length": 2, "entry_days": 3, "exit_days": 2}
        kwargs[field] = 2.5
        with pytest.raises(TypeError, match="integer"):
            LeveragedTrend(**kwargs)

    def test_requires_explicit_underlying_signal_column(self):
        frame = _frame([100, 101, 102]).drop(columns="signal_close")
        with pytest.raises(ValueError, match="explicit 'signal_close'"):
            LeveragedTrend(sma_length=2).generate_signals(frame)

    def test_required_bars_includes_confirmation(self):
        strategy = LeveragedTrend(sma_length=200, entry_days=5, exit_days=2)
        assert strategy.required_bars() == 204


class TestLeveragedTrendSignals:
    """The state machine starts in cash and changes only on confirmed streaks."""

    def test_starts_out_and_enters_on_full_above_confirmation(self):
        frame = _frame([100, 101, 102, 103])
        signals = LeveragedTrend(
            sma_length=2, entry_days=3, exit_days=2
        ).generate_signals(frame)
        assert signals.entries.tolist() == [False, False, False, True]
        assert not signals.exits.any()

    def test_exit_requires_full_below_confirmation(self):
        frame = _frame([100, 101, 102, 103, 102, 101])
        signals = LeveragedTrend(
            sma_length=2, entry_days=3, exit_days=2
        ).generate_signals(frame)
        assert signals.entries[signals.entries].index.tolist() == [frame.index[3]]
        assert signals.exits[signals.exits].index.tolist() == [frame.index[5]]

    def test_on_sma_equality_resets_confirmation(self):
        # With SMA2, an unchanged close sits exactly on the moving average.
        frame = _frame([100, 101, 102, 102, 103, 104])
        signals = LeveragedTrend(
            sma_length=2, entry_days=3, exit_days=2
        ).generate_signals(frame)
        assert not signals.entries.any()

    def test_no_duplicate_entries_while_position_remains_open(self):
        frame = _frame([100, 101, 102, 103, 104, 105, 106])
        signals = LeveragedTrend(
            sma_length=2, entry_days=2, exit_days=2
        ).generate_signals(frame)
        assert int(signals.entries.sum()) == 1

    def test_fresh_confirmation_can_reenter_after_exit(self):
        frame = _frame([100, 101, 102, 101, 100, 101, 102])
        signals = LeveragedTrend(
            sma_length=2, entry_days=2, exit_days=2
        ).generate_signals(frame)
        assert signals.entries[signals.entries].index.tolist() == [
            frame.index[2],
            frame.index[6],
        ]
        assert signals.exits[signals.exits].index.tolist() == [frame.index[4]]

    def test_signal_is_independent_of_leveraged_fund_close(self):
        signal_closes = [100, 101, 102, 101, 100]
        first = _frame(signal_closes, [10, 11, 15, 12, 9])
        second = _frame(signal_closes, [50, 40, 30, 20, 10])
        strategy = LeveragedTrend(sma_length=2, entry_days=2, exit_days=2)
        assert strategy.generate_signals(first).entries.equals(
            strategy.generate_signals(second).entries
        )
        assert strategy.generate_signals(first).exits.equals(
            strategy.generate_signals(second).exits
        )


class TestLeveragedTrendPairFrame:
    """Pair construction must preserve execution prices and common sessions."""

    def test_aligns_underlying_close_to_leveraged_execution_bars(self):
        signal = _frame([100, 101, 102])[["open", "close"]].copy()
        signal["open"] = [300, 303, 306]
        signal["close"] = [100, 101, 102]
        trading = _frame([1, 2, 3], [30, 33, 36]).drop(columns="signal_close")
        signal = signal.iloc[1:]

        aligned = build_pair_frame(signal, trading)

        assert aligned.index.tolist() == trading.index[1:].tolist()
        assert aligned["signal_close"].tolist() == [101, 102]
        assert aligned["signal_open"].tolist() == [303, 306]
        assert aligned["open"].tolist() == [33, 36]

    def test_backtest_executes_confirming_close_on_next_open(self):
        frame = _frame([100, 101, 102, 103, 104], [30, 31, 32, 33, 34])
        result = run_pair_backtest(
            LeveragedPair("SPY", "SPXL"),
            frame,
            sma_length=2,
            entry_days=2,
            exit_days=2,
            config=BacktestConfig(slippage_bps=0),
        )
        # Confirmation is index 2; execution is shifted to index 3.
        assert result.entries_executed.tolist() == [False, False, False, True, False]
        assert result.portfolio.orders.records_readable.iloc[0]["Price"] == 33

    def test_buy_and_hold_benchmarks_both_assets_after_warmup(self):
        frame = _frame(
            [100 + index for index in range(8)],
            [30 + index for index in range(8)],
        )
        frame["signal_open"] = frame["signal_close"]
        benchmarks = buy_and_hold_study(
            {LeveragedPair("SPY", "SPXL"): frame},
            sma_length=2,
            config=BacktestConfig(slippage_bps=0),
        )
        assert benchmarks["symbol"].tolist() == ["SPY", "SPXL"]
        assert set(benchmarks["asset_kind"]) == {"unleveraged", "leveraged"}


class TestLeveragedTrendAggregation:
    """Cross-pair summaries retain downside-focused robustness columns."""

    def test_aggregates_each_configuration_across_pairs(self):
        study = pd.DataFrame(
            [
                {"entry_days": 3, "exit_days": 2, "cagr": 0.20, "sharpe": 1.0,
                 "max_drawdown": -0.30, "calmar": 0.67, "trade_count": 5,
                 "time_in_market": 0.60},
                {"entry_days": 3, "exit_days": 2, "cagr": 0.10, "sharpe": 0.8,
                 "max_drawdown": -0.40, "calmar": 0.25, "trade_count": 6,
                 "time_in_market": 0.50},
            ]
        )
        result = aggregate_parameter_study(study).iloc[0]
        assert result["pair_count"] == 2
        assert result["median_cagr"] == pytest.approx(0.15)
        assert result["worst_cagr"] == pytest.approx(0.10)
        assert result["worst_max_drawdown"] == pytest.approx(-0.40)
        assert result["total_trades"] == 11
