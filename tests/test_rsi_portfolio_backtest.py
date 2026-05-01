from __future__ import annotations

import pandas as pd
import pytest

from backtest.runner import BacktestConfig
from scripts.rsi_portfolio_backtest import simulate_portfolio_from_signals


def _df(rows: dict[str, list[float]]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(next(iter(rows.values()))), freq="D", tz="UTC")
    return pd.DataFrame(rows, index=idx)


class TestSimulatePortfolioFromSignals:
    def test_single_symbol_trade_uses_next_open_and_shared_cash(self):
        open_prices = _df({"AAA": [10.0, 10.0, 12.0, 12.0]})
        close_prices = _df({"AAA": [10.0, 11.0, 12.0, 12.0]})
        entries = _df({"AAA": [False, True, False, False]}).astype(bool)
        exits = _df({"AAA": [False, False, True, False]}).astype(bool)

        result = simulate_portfolio_from_signals(
            open_prices,
            close_prices,
            entries,
            exits,
            basket_name="test",
            config=BacktestConfig(initial_cash=100.0, slippage_bps=0.0, commission_per_trade=0.0),
            max_positions=1,
        )

        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.entry_price == pytest.approx(10.0)
        assert trade.exit_price == pytest.approx(12.0)
        assert result.stats["final_equity"] == pytest.approx(120.0)
        assert result.stats["trade_count"] == pytest.approx(1.0)

    def test_same_day_entries_split_cash_equally(self):
        open_prices = _df(
            {
                "AAA": [10.0, 10.0, 10.0],
                "BBB": [20.0, 20.0, 20.0],
            }
        )
        close_prices = open_prices.copy()
        entries = _df(
            {
                "AAA": [False, True, False],
                "BBB": [False, True, False],
            }
        ).astype(bool)
        exits = _df({"AAA": [False, False, False], "BBB": [False, False, False]}).astype(bool)

        result = simulate_portfolio_from_signals(
            open_prices,
            close_prices,
            entries,
            exits,
            basket_name="test",
            config=BacktestConfig(initial_cash=120.0, slippage_bps=0.0, commission_per_trade=0.0),
            max_positions=2,
        )

        # $60 into each symbol: 6 AAA shares and 3 BBB shares.
        assert result.equity_curve.iloc[-1] == pytest.approx(120.0)
        assert result.stats["avg_open_positions"] == pytest.approx(4.0 / 3.0)

    def test_max_positions_caps_new_entries(self):
        open_prices = _df(
            {
                "AAA": [10.0, 10.0, 10.0],
                "BBB": [10.0, 10.0, 10.0],
                "CCC": [10.0, 10.0, 10.0],
            }
        )
        close_prices = open_prices.copy()
        entries = _df(
            {
                "AAA": [False, True, False],
                "BBB": [False, True, False],
                "CCC": [False, True, False],
            }
        ).astype(bool)
        exits = _df(
            {
                "AAA": [False, False, False],
                "BBB": [False, False, False],
                "CCC": [False, False, False],
            }
        ).astype(bool)

        result = simulate_portfolio_from_signals(
            open_prices,
            close_prices,
            entries,
            exits,
            basket_name="test",
            config=BacktestConfig(initial_cash=90.0, slippage_bps=0.0, commission_per_trade=0.0),
            max_positions=2,
        )

        assert result.stats["avg_open_positions"] <= 2.0
        assert result.equity_curve.iloc[-1] == pytest.approx(90.0)
