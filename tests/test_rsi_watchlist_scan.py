"""
Unit tests for scripts/rsi_watchlist_scan.py.

These tests stay offline and focus on the pure scanner contract: technical
rejections short-circuit fundamentals, while technically valid symbols still
flow through market-cap/solvency checks before becoming candidates.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.rsi_watchlist_scan import ScanConfig, scan_candidates
from scripts.sma_watchlist_scan import AssetInfo


def _passing_metric() -> dict[str, float | int]:
    return {
        "close": 120.0,
        "sma50": 115.0,
        "sma200": 100.0,
        "avg_volume_20": 2_000_000.0,
        "avg_dollar_volume_50": 250_000_000.0,
        "high_52w": 140.0,
        "low_52w": 80.0,
        "rsi14": 42.0,
        "atr_pct": 0.03,
        "bb_width_pct": 0.12,
        "oversold_events": 4,
        "reversion_hit_rate": 0.75,
        "avg_reversion_return_10d": 0.04,
        "stop_failures": 1,
        "one_day_return": -0.01,
        "five_day_return": -0.03,
    }


class TestScanCandidates:
    def test_technical_rejection_skips_fundamentals(self, monkeypatch):
        metric = _passing_metric()
        metric["close"] = 5.0
        monkeypatch.setattr(
            "scripts.rsi_watchlist_scan._compute_metrics",
            lambda _df, _config: metric,
        )

        def fail_fetch(_symbol: str):
            raise AssertionError("fundamentals should not be fetched")

        monkeypatch.setattr("scripts.rsi_watchlist_scan.fetch_fundamentals", fail_fetch)

        candidates, rejections, _examples, explanations = scan_candidates(
            [AssetInfo("LOW", "Low Price Inc", "NYSE")],
            {"LOW": pd.DataFrame({"close": [5.0]})},
            config=ScanConfig(),
            include_fundamentals=True,
            top=10,
            explain_symbols={"LOW"},
        )

        assert candidates == []
        assert rejections["price"] == 1
        assert "Rejected: price" in explanations["LOW"]

    def test_passing_symbol_requires_market_cap_and_solvency(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.rsi_watchlist_scan._compute_metrics",
            lambda _df, _config: _passing_metric(),
        )
        monkeypatch.setattr(
            "scripts.rsi_watchlist_scan.fetch_fundamentals",
            lambda _symbol: SimpleNamespace(market_cap=25_000_000_000.0),
        )
        monkeypatch.setattr(
            "scripts.rsi_watchlist_scan.assess_fitness",
            lambda _fundamentals, _profile: SimpleNamespace(
                solvency_ok=True,
                error=None,
            ),
        )

        candidates, rejections, _examples, explanations = scan_candidates(
            [AssetInfo("GOOD", "Good Reverter", "NASDAQ")],
            {"GOOD": pd.DataFrame({"close": [120.0]})},
            config=ScanConfig(),
            include_fundamentals=True,
            top=10,
            explain_symbols={"GOOD"},
        )

        assert rejections == {}
        assert [candidate.symbol for candidate in candidates] == ["GOOD"]
        assert candidates[0].market_cap == pytest.approx(25_000_000_000.0)
        assert "Passed all enabled filters" in explanations["GOOD"]
