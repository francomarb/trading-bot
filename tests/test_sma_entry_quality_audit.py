"""Unit tests for the production-mirror SMA entry audit (PLAN 11.70)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.sma_entry_quality_audit import (
    ATR_LENGTH,
    AuditTrade,
    _stop_fill,
    avoided_and_blocked,
    classify_spy_regimes,
    prepare_symbol_bars,
    simulate_period,
    summarize,
)


def _frame(closes: np.ndarray, *, volume: np.ndarray | None = None) -> pd.DataFrame:
    index = pd.date_range("2020-01-01", periods=len(closes), freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": volume if volume is not None else np.full(len(closes), 1_000_000.0),
        },
        index=index,
    )


class TestStopFill:
    def test_gap_through_fills_at_open(self):
        row = pd.Series({"open": 90.0, "low": 89.0})
        assert _stop_fill(row, 95.0) == pytest.approx(90.0)

    def test_intraday_touch_fills_at_stop(self):
        row = pd.Series({"open": 100.0, "low": 94.0})
        assert _stop_fill(row, 95.0) == pytest.approx(95.0)


class TestPrepareSymbolBars:
    def test_volume_gate_matches_production_medians(self):
        closes = np.linspace(80.0, 120.0, 240)
        volume = np.concatenate((np.full(210, 2_000_000.0), np.full(30, 1_000_000.0)))
        bars = prepare_symbol_bars(_frame(closes, volume=volume))
        assert not bool(bars["volume_gate"].iloc[-1])

    def test_stock_gate_is_above_sma200(self):
        closes = np.concatenate((np.full(239, 100.0), [80.0]))
        bars = prepare_symbol_bars(_frame(closes))
        assert not bool(bars["stock_gate"].iloc[-1])


class TestRegimeMirror:
    def test_bear_has_priority_over_other_signals(self):
        closes = np.concatenate((np.linspace(100.0, 200.0, 220), [80.0]))
        regimes = classify_spy_regimes(_frame(closes))
        assert regimes.iloc[-1] == "BEAR"


def _prepared_trade_frame() -> tuple[pd.DataFrame, pd.Series, int]:
    closes = np.concatenate((np.full(210, 100.0), np.linspace(100.0, 140.0, 40)))
    bars = prepare_symbol_bars(_frame(closes))
    signal_i = 220
    bars.iloc[signal_i, bars.columns.get_loc("golden_cross")] = True
    bars.iloc[signal_i, bars.columns.get_loc("stock_gate")] = True
    bars.iloc[signal_i, bars.columns.get_loc("volume_gate")] = True
    signals = pd.Series(False, index=bars.index)
    signals.iloc[signal_i] = True
    assert pd.notna(bars[f"atr_{ATR_LENGTH}"].iloc[signal_i])
    return bars, signals, signal_i


class TestPolicySemantics:
    def test_structure_stop_is_never_tighter_than_control(self):
        bars, signals, _ = _prepared_trade_frame()
        kwargs = dict(
            symbol="TEST",
            bars=bars,
            valid_signals=signals,
            start=bars.index[200],
            end=bars.index[-1],
        )
        control = simulate_period(policy="control", **kwargs).trades[0]
        structure = simulate_period(policy="structure_stop", **kwargs).trades[0]
        assert structure.stop_price <= control.stop_price
        assert structure.r_multiple == pytest.approx(
            (structure.exit_price - structure.entry_price)
            / (structure.entry_price - structure.stop_price)
        )

    def test_pullback_expires_when_limit_is_never_touched(self):
        bars, signals, signal_i = _prepared_trade_frame()
        # Force every waiting session above the signal-close-minus-0.5ATR limit.
        for i in range(signal_i + 1, signal_i + 6):
            bars.iloc[i, bars.columns.get_loc("low")] = float(bars["close"].iloc[signal_i]) + 5.0
        result = simulate_period(
            symbol="TEST",
            bars=bars,
            valid_signals=signals,
            start=bars.index[200],
            end=bars.index[-1],
            policy="pullback",
        )
        assert result.trades == ()

    def test_same_symbol_signals_do_not_overlap(self):
        bars, signals, signal_i = _prepared_trade_frame()
        signals.iloc[signal_i + 2] = True
        result = simulate_period(
            symbol="TEST",
            bars=bars,
            valid_signals=signals,
            start=bars.index[200],
            end=bars.index[-1],
            policy="control",
        )
        assert len(result.trades) == 1

    def test_entry_day_death_cross_exits_at_following_open(self):
        bars, signals, signal_i = _prepared_trade_frame()
        entry_i = signal_i + 1
        bars.iloc[entry_i, bars.columns.get_loc("death_cross")] = True
        bars.iloc[entry_i, bars.columns.get_loc("low")] = float(bars["open"].iloc[entry_i])
        result = simulate_period(
            symbol="TEST",
            bars=bars,
            valid_signals=signals,
            start=bars.index[200],
            end=bars.index[-1],
            policy="control",
        )
        assert result.trades[0].exit_reason == "death_cross"
        assert result.trades[0].exit_date == bars.index[entry_i + 1]


class TestAuditMetrics:
    def _trade(self, signal: str, exit_price: float) -> AuditTrade:
        date = pd.Timestamp(signal, tz="UTC")
        return AuditTrade("X", "p", date, date, 100.0, 90.0, date, exit_price, "death_cross")

    def test_summarizes_fixed_risk_and_drawdown(self):
        result = summarize([self._trade("2024-01-01", 110.0), self._trade("2024-01-02", 80.0)])
        assert result["expectancy_r"] == pytest.approx(-0.5)
        assert result["total_r"] == pytest.approx(-1.0)
        assert result["max_drawdown_r"] == pytest.approx(2.0)

    def test_counts_unfilled_losers_and_winners(self):
        loser = self._trade("2024-01-01", 90.0)
        winner = self._trade("2024-01-02", 120.0)
        assert avoided_and_blocked([loser, winner], []) == (1, 1)
