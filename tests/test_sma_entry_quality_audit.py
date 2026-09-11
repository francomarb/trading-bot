"""Unit tests for the production-mirror SMA entry audit (PLAN 11.70)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from config import settings
from scripts.sma_entry_quality_audit import (
    ATR_STOP_MULTIPLIER,
    ATR_LENGTH,
    AuditTrade,
    PeriodResult,
    PolicySkip,
    _exit_trade,
    _utc_index,
    _stop_fill,
    avoided_and_blocked,
    build_period_specs,
    evaluate_decisions,
    paired_policy_deltas,
    prepare_symbol_bars,
    selection_vs_chance,
    simulate_period,
    summarize,
    valid_signal_mask,
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


class TestInputIntegrity:
    def test_duplicate_cached_date_fails_with_clear_error(self):
        frame = _frame(np.asarray([100.0, 101.0, 102.0]))
        frame.index = pd.DatetimeIndex([frame.index[0], frame.index[1], frame.index[1]])
        with pytest.raises(ValueError, match="duplicate timestamp"):
            _utc_index(frame)


class TestValidSignalMask:
    def _inputs(self):
        index = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
        bars = pd.DataFrame({
            "golden_cross": [True] * 5,
            "stock_gate": [True] * 5,
            "volume_gate": [True] * 5,
        }, index=index)
        regimes = pd.Series("TRENDING", index=index)
        return bars, regimes

    def test_combines_signal_stock_volume_and_regime_gates(self):
        bars, regimes = self._inputs()
        bars.loc[bars.index[0], "golden_cross"] = False
        bars.loc[bars.index[1], "stock_gate"] = False
        bars.loc[bars.index[2], "volume_gate"] = False
        regimes.loc[bars.index[3]] = "BEAR"
        result = valid_signal_mask(bars, regimes, symbol="TEST", earnings={})
        assert result.tolist() == [False, False, False, False, True]

    def test_earnings_blocks_event_day_and_prior_two_calendar_days(self):
        bars, regimes = self._inputs()
        event = bars.index[2]
        result = valid_signal_mask(
            bars,
            regimes,
            symbol="TEST",
            earnings={"TEST": {event}},
        )
        assert result.tolist() == [False, False, False, True, True]

    def test_missing_regime_date_fails_closed(self):
        bars, regimes = self._inputs()
        regimes = regimes.drop(bars.index[-1])
        result = valid_signal_mask(bars, regimes, symbol="TEST", earnings={})
        assert not bool(result.iloc[-1])


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
        assert result.skips[0].reason == "pullback_unfilled"

    def test_pullback_rejects_nonfinite_atr_before_fill(self):
        bars, signals, signal_i = _prepared_trade_frame()
        limit_price = float(bars["close"].iloc[signal_i]) - 0.5 * float(
            bars[f"atr_{ATR_LENGTH}"].iloc[signal_i]
        )
        bars.iloc[signal_i + 1, bars.columns.get_loc("low")] = limit_price + 1.0
        bars.iloc[signal_i + 2, bars.columns.get_loc("low")] = limit_price - 1.0
        bars.iloc[signal_i + 1, bars.columns.get_loc(f"atr_{ATR_LENGTH}")] = np.nan
        with pytest.raises(ValueError, match="pullback fill ATR"):
            simulate_period(
                symbol="TEST",
                bars=bars,
                valid_signals=signals,
                start=bars.index[200],
                end=bars.index[-1],
                policy="pullback",
            )

    def test_extension_cap_explicitly_skips_extended_fill(self):
        bars, signals, signal_i = _prepared_trade_frame()
        bars.iloc[signal_i + 1, bars.columns.get_loc("open")] = 1_000.0
        result = simulate_period(
            symbol="TEST",
            bars=bars,
            valid_signals=signals,
            start=bars.index[200],
            end=bars.index[-1],
            policy="extension_cap",
        )
        assert result.trades == ()
        assert result.skips == (
            PolicySkip("TEST", bars.index[signal_i], "extension_cap"),
        )

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

    def test_stop_wins_when_stop_and_death_cross_share_a_bar(self):
        bars, _, signal_i = _prepared_trade_frame()
        entry_i = signal_i + 1
        entry = float(bars["open"].iloc[entry_i])
        stop = entry - 0.5
        bars.iloc[entry_i, bars.columns.get_loc("low")] = stop - 0.1
        bars.iloc[entry_i, bars.columns.get_loc("death_cross")] = True
        trade = _exit_trade(
            symbol="TEST",
            policy="control",
            bars=bars,
            signal_i=signal_i,
            entry_i=entry_i,
            entry_price=entry,
            stop_price=stop,
            period_end_i=len(bars) - 1,
        )
        assert trade.exit_reason == "atr_stop"

    def test_exit_guard_rejects_nonfinite_stop(self):
        bars, _, signal_i = _prepared_trade_frame()
        entry_i = signal_i + 1
        with pytest.raises(ValueError, match="stop must be finite"):
            _exit_trade(
                symbol="TEST",
                policy="pullback",
                bars=bars,
                signal_i=signal_i,
                entry_i=entry_i,
                entry_price=float(bars["open"].iloc[entry_i]),
                stop_price=float("nan"),
                period_end_i=len(bars) - 1,
            )

    def test_death_cross_on_period_end_stays_period_mark(self):
        bars, _, signal_i = _prepared_trade_frame()
        entry_i = signal_i + 1
        end_i = entry_i + 2
        bars.iloc[end_i, bars.columns.get_loc("death_cross")] = True
        entry = float(bars["open"].iloc[entry_i])
        trade = _exit_trade(
            symbol="TEST",
            policy="control",
            bars=bars,
            signal_i=signal_i,
            entry_i=entry_i,
            entry_price=entry,
            stop_price=entry - 10.0,
            period_end_i=end_i,
        )
        assert trade.exit_reason == "period_mark"
        assert trade.exit_date == bars.index[end_i]


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

    def test_overlap_skip_is_not_misattributed_to_entry_rule(self):
        loser = self._trade("2024-01-01", 90.0)
        skip = PolicySkip("X", loser.signal_date, "policy_overlap")
        assert avoided_and_blocked([loser], [], [skip]) == (0, 0)

    def test_selection_comparison_uses_random_same_skip_rate_baseline(self):
        control = [
            self._trade(f"2024-01-{day:02d}", 90.0 if day <= 6 else 110.0)
            for day in range(1, 11)
        ]
        variant = control[:5]
        chance = selection_vs_chance(control, variant)
        assert chance is not None
        assert chance.expected_losers_not_entered == pytest.approx(3.0)
        assert chance.expected_winners_not_entered == pytest.approx(2.0)

    def test_paired_deltas_assign_zero_r_to_declined_signal(self):
        control = [self._trade("2024-01-01", 110.0)]
        assert paired_policy_deltas(control, []) == [-1.0]


class TestDecisionRule:
    def _trade(self, day: int, r_multiple: float, *, policy: str) -> AuditTrade:
        date = pd.Timestamp("2024-01-01", tz="UTC") + pd.Timedelta(days=day)
        return AuditTrade(
            "X",
            policy,
            date,
            date,
            100.0,
            90.0,
            date,
            100.0 + 10.0 * r_multiple,
            "death_cross",
        )

    def _results(self, control_rs: list[float], variant_rs: list[float]):
        control = tuple(
            self._trade(i, value, policy="control")
            for i, value in enumerate(control_rs)
        )
        variant = tuple(
            self._trade(i, value, policy="structure_stop")
            for i, value in enumerate(variant_rs)
        )
        empty = PeriodResult((), (), ())
        return {
            "control": PeriodResult(control, (), ()),
            "structure_stop": PeriodResult(variant, (), ()),
            "pullback": empty,
            "extension_cap": empty,
        }

    def test_period_roles_are_tied_to_actual_dates(self):
        specs = {spec.role: spec for spec in build_period_specs(pd.Timestamp("2026-09-04", tz="UTC"))}
        assert specs["development"].start == pd.Timestamp("2017-01-01", tz="UTC")
        assert specs["development"].end == pd.Timestamp("2022-12-31", tz="UTC")
        assert specs["held_out"].start == pd.Timestamp("2023-01-01", tz="UTC")
        assert specs["held_out"].end == pd.Timestamp("2026-09-04", tz="UTC")

    def test_close_call_cannot_pass_without_positive_bootstrap_ci(self):
        control_rs = [-1.0] * 40
        # Better mean than control, but one severe outlier keeps uncertainty
        # wide enough that a positive edge is not established.
        variant_rs = [-0.5] * 39 + [-10.0]
        development = self._results(control_rs, variant_rs)
        held_out = self._results(control_rs, variant_rs)
        decision = evaluate_decisions(
            development=development,
            held_out=held_out,
        )[0]
        assert decision.expectancy_improved_both
        assert not decision.holdout_ci_positive
        assert not decision.passed


class TestConfigurationParity:
    def test_atr_settings_are_imported_from_production_config(self):
        assert ATR_LENGTH == settings.ATR_LENGTH
        assert ATR_STOP_MULTIPLIER == settings.ATR_STOP_MULTIPLIER
