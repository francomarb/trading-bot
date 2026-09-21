from __future__ import annotations

import pandas as pd

from backtest.donchian_portfolio_sim import (
    entry_quantity,
    simulate_portfolio,
    stop_limit_fill,
)
from scripts.donchian_parameter_rebaseline import select_variant


def _bars(*, breakout: bool = True, base_price: float = 100.0) -> pd.DataFrame:
    idx = pd.bdate_range("2023-01-02", periods=260, tz="UTC")
    close = pd.Series(base_price, index=idx)
    if breakout:
        close.iloc[-3:] = [base_price + 1.0, base_price + 2.0, base_price + 3.0]
    return pd.DataFrame({
        "open": close, "high": close + 1.0, "low": close - 1.0,
        "close": close, "volume": 1_000_000.0,
    }, index=idx)


class TestSharedDonchianPortfolio:
    def test_shared_position_ceiling_counts_capacity_skips(self) -> None:
        bars = {f"S{i}": _bars() for i in range(3)}
        regime = pd.Series("TRENDING", index=next(iter(bars.values())).index)
        result = simulate_portfolio(
            bars, regime, list(bars), entry_window=20, exit_window=10,
            trade_start=pd.Timestamp("2023-11-01", tz="UTC"),
            trade_end=pd.Timestamp("2023-12-29", tz="UTC"), max_positions=1,
            sleeve_notional_pct=1.0, position_notional_pct=1.0,
        )
        assert result.skipped_capacity >= 1
        assert result.stats["trades"] >= 1

    def test_bear_regime_blocks_entries(self) -> None:
        bars = {"TEST": _bars()}
        regime = pd.Series("BEAR", index=bars["TEST"].index)
        result = simulate_portfolio(
            bars, regime, ["TEST"], entry_window=20, exit_window=10,
            trade_start=pd.Timestamp("2023-11-01", tz="UTC"),
            trade_end=pd.Timestamp("2023-12-29", tz="UTC"),
        )
        assert not result.trades

    def test_symbols_argument_is_the_simulated_universe_boundary(self) -> None:
        bars = {"INCLUDED": _bars(), "EXCLUDED": _bars()}
        regime = pd.Series("TRENDING", index=bars["INCLUDED"].index)
        result = simulate_portfolio(
            bars, regime, ["INCLUDED"], entry_window=20, exit_window=10,
            trade_start=pd.Timestamp("2023-11-01", tz="UTC"),
            trade_end=pd.Timestamp("2023-12-29", tz="UTC"),
        )
        assert {trade.symbol for trade in result.trades} == {"INCLUDED"}

    def test_gap_through_protective_stop_fills_at_open(self) -> None:
        frame = _bars()
        frame.iloc[-1, frame.columns.get_loc("open")] = 80.0
        frame.iloc[-1, frame.columns.get_loc("high")] = 81.0
        frame.iloc[-1, frame.columns.get_loc("low")] = 79.0
        frame.iloc[-1, frame.columns.get_loc("close")] = 80.0
        regime = pd.Series("TRENDING", index=frame.index)
        result = simulate_portfolio(
            {"TEST": frame}, regime, ["TEST"], entry_window=20, exit_window=10,
            trade_start=pd.Timestamp("2023-11-01", tz="UTC"),
            trade_end=pd.Timestamp("2023-12-29", tz="UTC"),
            exit_slippage_bps=0.0,
        )
        stopped = [trade for trade in result.trades if trade.exit_reason == "stop_gap"]
        assert len(stopped) == 1
        assert stopped[0].exit_price == 80.0

    def test_signal_exit_releases_slot_before_same_session_entry(self) -> None:
        first = _bars(breakout=False)
        # A wide breakout bar creates a deep static stop. Two sessions later,
        # close=99 breaks the prior 10-day close-low without touching that stop.
        first.iloc[-6] = [100.0, 121.0, 81.0, 101.0, 1_000_000.0]
        first.iloc[-5] = [101.0, 102.0, 100.0, 101.0, 1_000_000.0]
        first.iloc[-4] = [100.0, 100.5, 98.5, 99.0, 1_000_000.0]
        second = _bars(breakout=False, base_price=20.0)
        second.iloc[-4] = [20.0, 22.0, 19.5, 21.0, 2_000_000.0]
        second.iloc[-3] = [21.0, 22.0, 20.0, 21.0, 2_000_000.0]
        regime = pd.Series("TRENDING", index=first.index)
        result = simulate_portfolio(
            {"A": first, "B": second}, regime, ["A", "B"],
            entry_window=20, exit_window=10,
            trade_start=pd.Timestamp("2023-11-01", tz="UTC"),
            trade_end=pd.Timestamp("2023-12-29", tz="UTC"),
            max_positions=1, sleeve_notional_pct=1.0,
            position_notional_pct=1.0, exit_slippage_bps=0.0,
        )
        assert any(trade.symbol == "A" and trade.exit_reason == "signal" for trade in result.trades)
        assert any(trade.symbol == "B" for trade in result.trades)

    def test_selector_uses_drawdown_inside_sharpe_tie_band(self) -> None:
        bars = {"TEST": _bars()}
        regime = pd.Series("TRENDING", index=bars["TEST"].index)
        a = simulate_portfolio(
            bars, regime, ["TEST"], entry_window=20, exit_window=10,
            trade_start=pd.Timestamp("2023-11-01", tz="UTC"),
            trade_end=pd.Timestamp("2023-12-29", tz="UTC"),
        )
        # Same result objects force a true tie; the slower pair is the final tie-break.
        assert select_variant({(20, 10): a, (55, 20): a}) == (55, 20)


class TestStopLimitFill:
    def test_expires_when_trigger_does_not_trade(self) -> None:
        assert stop_limit_fill(
            open_price=99.0, high=99.9, low=98.0, trigger=100.0, limit=104.0,
        ) is None

    def test_expires_when_triggered_but_limit_never_reachable(self) -> None:
        assert stop_limit_fill(
            open_price=106.0, high=108.0, low=105.0, trigger=100.0, limit=104.0,
        ) is None

    def test_gap_above_limit_then_retrace_fills_at_limit(self) -> None:
        assert stop_limit_fill(
            open_price=106.0, high=108.0, low=103.0, trigger=100.0, limit=104.0,
        ) == 104.0

    def test_open_inside_band_fills_at_open(self) -> None:
        assert stop_limit_fill(
            open_price=102.0, high=103.0, low=101.0, trigger=100.0, limit=104.0,
        ) == 102.0


class TestEntryQuantity:
    def test_rejects_when_sleeve_availability_is_below_production_floor(self) -> None:
        assert entry_quantity(
            equity=100_000.0, cash=90_000.0, available=99.99,
            fill=20.0, sizing_limit=21.0, atr=1.0,
            risk_per_trade_pct=0.004, position_notional_pct=0.048,
            stop_atr=2.0,
        ) == 0

    def test_does_not_apply_stricter_floor_to_resulting_whole_share_notional(self) -> None:
        # Production checks sleeve availability before RiskManager sizing. It
        # can therefore approve one $70 share when availability is $120.
        assert entry_quantity(
            equity=100_000.0, cash=90_000.0, available=120.0,
            fill=70.0, sizing_limit=70.0, atr=200.0,
            risk_per_trade_pct=0.004, position_notional_pct=0.048,
            stop_atr=2.0,
        ) == 1

    def test_position_notional_cap_can_bind_before_risk_budget(self) -> None:
        assert entry_quantity(
            equity=100_000.0, cash=100_000.0, available=12_000.0,
            fill=100.0, sizing_limit=100.0, atr=1.0,
            risk_per_trade_pct=0.004, position_notional_pct=0.01,
            stop_atr=2.0,
        ) == 10
