"""Tests for the historical production gate used by Donchian research."""

from __future__ import annotations

import pandas as pd

from scripts.backtest_donchian_breakout import ProductionMirrorDonchianFilter
from strategies.base import EdgeFilterDecision


class _StockFilterStub:
    def __init__(self, allowed: pd.Series) -> None:
        self._allowed = allowed
        self.symbol: str | None = None

    def set_symbol(self, symbol: str) -> None:
        self.symbol = symbol

    def __call__(self, df: pd.DataFrame) -> EdgeFilterDecision:
        return EdgeFilterDecision(
            allowed=self._allowed,
            reasons=pd.Series(
                [([] if ok else ["stock gate blocked"]) for ok in self._allowed],
                index=self._allowed.index,
                dtype=object,
            ),
        )


class TestProductionMirrorDonchianFilter:
    def test_requires_both_stock_filter_and_historical_trending_regime(self) -> None:
        index = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
        stock_allowed = pd.Series([True, False, True], index=index, dtype=bool)
        regime = pd.Series(["TRENDING", "TRENDING", "RANGING"], index=index)
        mirror = ProductionMirrorDonchianFilter(regime)
        stub = _StockFilterStub(stock_allowed)
        mirror._stock_filter = stub  # type: ignore[assignment]

        mirror.set_symbol("NVDA")
        decision = mirror(pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=index))

        assert stub.symbol == "NVDA"
        assert decision.allowed.tolist() == [True, False, False]
        assert decision.reasons.iloc[1] == ["stock gate blocked"]
        assert decision.reasons.iloc[2] == ["SPY regime is not TRENDING"]

    def test_missing_spy_date_fails_closed(self) -> None:
        index = pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC")
        mirror = ProductionMirrorDonchianFilter(
            pd.Series(["TRENDING"], index=index[:1])
        )
        mirror._stock_filter = _StockFilterStub(  # type: ignore[assignment]
            pd.Series(True, index=index, dtype=bool)
        )

        decision = mirror(pd.DataFrame({"close": [1.0, 2.0]}, index=index))

        assert decision.allowed.tolist() == [True, False]
        assert decision.reasons.iloc[1] == ["SPY regime is not TRENDING"]
