from __future__ import annotations

import pandas as pd

from backtest.donchian_portfolio_sim import simulate_portfolio
from scripts.donchian_parameter_rebaseline import select_variant


def _bars(*, breakout: bool = True) -> pd.DataFrame:
    idx = pd.bdate_range("2023-01-02", periods=260, tz="UTC")
    close = pd.Series(100.0, index=idx)
    if breakout:
        close.iloc[-3:] = [101.0, 102.0, 103.0]
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
