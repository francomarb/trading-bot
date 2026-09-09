"""
Unit tests for scripts/rsi_watchlist_scan.py.

These tests stay offline and focus on the pure scanner contract: durable
operational failures short-circuit fundamentals, temporary technical state and
historical RSI outcomes cannot reject or rank a company, and fundamentals fail
closed before a company becomes a candidate.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from scripts.rsi_watchlist_scan import DEFAULT_POOL_SIZE, ScanConfig, scan_candidates
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
        "median_atr_pct_252": 0.035,
        "bb_width_pct": 0.12,
        "oversold_events": 4,
        "reversion_hit_rate": 0.75,
        "avg_reversion_return_10d": 0.04,
        "stop_failures": 1,
        "one_day_return": -0.01,
        "five_day_return": -0.03,
    }


class TestScanCandidates:
    def test_default_pool_size_is_fifty(self):
        assert DEFAULT_POOL_SIZE == 50

    def test_durable_rejection_skips_fundamentals(self, monkeypatch):
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

    def test_temporary_state_and_rsi_history_are_reference_only(self, monkeypatch):
        metric = _passing_metric()
        metric["avg_volume_20"] = 1.0
        metric["avg_dollar_volume_50"] = 60_000_000.0
        metric["sma200"] = 200.0
        metric["atr_pct"] = 0.20
        metric["bb_width_pct"] = 0.0
        metric["oversold_events"] = 0
        metric["reversion_hit_rate"] = 0.0
        metric["stop_failures"] = 99
        metric["one_day_return"] = -0.50
        metric["five_day_return"] = -0.75
        monkeypatch.setattr(
            "scripts.rsi_watchlist_scan._compute_metrics",
            lambda _df, _config: metric,
        )

        candidates, rejections, _examples, explanations = scan_candidates(
            [AssetInfo("BROAD", "Broad Candidate", "NYSE")],
            {"BROAD": pd.DataFrame({"close": [120.0]})},
            config=ScanConfig(),
            include_fundamentals=False,
            top=10,
            explain_symbols={"BROAD"},
        )

        assert rejections == {}
        assert [candidate.symbol for candidate in candidates] == ["BROAD"]
        assert "Passed all enabled filters" in explanations["BROAD"]

    def test_ranking_uses_dollar_liquidity_not_historical_outcome(self, monkeypatch):
        metrics = {
            "LIQ": {
                **_passing_metric(),
                "avg_dollar_volume_50": 500_000_000.0,
                "reversion_hit_rate": 0.0,
                "avg_reversion_return_10d": -0.50,
            },
            "HIST": {
                **_passing_metric(),
                "avg_dollar_volume_50": 100_000_000.0,
                "reversion_hit_rate": 1.0,
                "avg_reversion_return_10d": 0.50,
            },
        }
        monkeypatch.setattr(
            "scripts.rsi_watchlist_scan._compute_metrics",
            lambda df, _config: metrics[str(df.attrs["symbol"])],
        )
        bars = {}
        for symbol in metrics:
            frame = pd.DataFrame({"close": [120.0]})
            frame.attrs["symbol"] = symbol
            bars[symbol] = frame

        candidates, _rejections, _examples, _explanations = scan_candidates(
            [
                AssetInfo("LIQ", "Liquid", "NYSE"),
                AssetInfo("HIST", "Historical Winner", "NYSE"),
            ],
            bars,
            config=ScanConfig(),
            include_fundamentals=False,
            top=2,
        )

        assert [candidate.symbol for candidate in candidates] == ["LIQ", "HIST"]

    def test_unknown_solvency_fails_closed(self, monkeypatch):
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
                solvency_ok=None,
                error=None,
            ),
        )

        candidates, rejections, _examples, _explanations = scan_candidates(
            [AssetInfo("UNKNOWN", "Unknown Solvency", "NYSE")],
            {"UNKNOWN": pd.DataFrame({"close": [120.0]})},
            config=ScanConfig(),
            include_fundamentals=True,
            top=10,
        )

        assert candidates == []
        assert rejections["solvency"] == 1

    def test_alphabet_policy_preserves_goog_and_excludes_googl(self, monkeypatch):
        metrics = {
            "LIQ": {**_passing_metric(), "avg_dollar_volume_50": 900_000_000.0},
            "GOOG": {**_passing_metric(), "avg_dollar_volume_50": 100_000_000.0},
        }
        monkeypatch.setattr(
            "scripts.rsi_watchlist_scan._compute_metrics",
            lambda df, _config: metrics[str(df.attrs["symbol"])],
        )
        bars = {}
        for symbol in ("LIQ", "GOOG", "GOOGL"):
            frame = pd.DataFrame({"close": [120.0]})
            frame.attrs["symbol"] = symbol
            bars[symbol] = frame

        candidates, rejections, _examples, explanations = scan_candidates(
            [
                AssetInfo("LIQ", "Liquid", "NYSE"),
                AssetInfo("GOOG", "Alphabet C", "NASDAQ"),
                AssetInfo("GOOGL", "Alphabet A", "NASDAQ"),
            ],
            bars,
            config=ScanConfig(),
            include_fundamentals=False,
            top=1,
            explain_symbols={"GOOGL"},
        )

        assert [candidate.symbol for candidate in candidates] == ["GOOG"]
        assert rejections["nonpreferred_share_class"] == 1
        assert "use GOOG" in explanations["GOOGL"]

    def test_open_rsi_position_is_retained_when_it_fails_refresh(self, monkeypatch):
        metric = _passing_metric()
        metric["close"] = 5.0
        monkeypatch.setattr(
            "scripts.rsi_watchlist_scan._compute_metrics",
            lambda _df, _config: metric,
        )

        candidates, _rejections, _examples, _explanations = scan_candidates(
            [AssetInfo("HELD", "Held Position", "NYSE")],
            {"HELD": pd.DataFrame({"close": [5.0]})},
            config=ScanConfig(),
            include_fundamentals=False,
            top=50,
            protected_symbols={"HELD"},
        )

        assert [candidate.symbol for candidate in candidates] == ["HELD"]
        assert candidates[0].notes == [
            "PROTECTED: open RSI position; outside refreshed top pool"
        ]
