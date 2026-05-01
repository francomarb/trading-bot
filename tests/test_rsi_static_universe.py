"""
Unit tests for scripts/rsi_static_universe.py.

The static universe builder is intentionally different from the dynamic RSI
scanner: it should accept broad, liquid symbols with some recent RSI activity,
then rank them by long-run trade density and profitability.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

from scripts.rsi_static_universe import (
    PrefilterCandidate,
    StaticUniverseConfig,
    apply_fundamental_gate,
    assemble_final_basket,
    prefilter_assets,
    rank_static_universe,
)
from scripts.sma_watchlist_scan import AssetInfo


def _recent_df() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=80, freq="D", tz="UTC")
    close = [100.0] * 76 + [95.0, 90.0, 95.0, 101.0]
    return pd.DataFrame(
        {
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [800_000.0] * len(close),
        },
        index=idx,
    )


def _flat_recent_df() -> pd.DataFrame:
    idx = pd.date_range("2025-01-01", periods=80, freq="D", tz="UTC")
    close = [100.0] * len(idx)
    return pd.DataFrame(
        {
            "open": close,
            "high": [c * 1.01 for c in close],
            "low": [c * 0.99 for c in close],
            "close": close,
            "volume": [800_000.0] * len(close),
        },
        index=idx,
    )


def _validation(
    symbol: str,
    *,
    trade_count: float,
    total_return: float,
    sharpe: float,
    max_drawdown: float,
    profit_factor: float,
    win_rate: float = 0.70,
    events: int = 8,
    event_hit_rate: float = 0.40,
    stop_failures: int = 1,
):
    event_records = [SimpleNamespace(stop_failed=i < stop_failures) for i in range(events)]
    result = SimpleNamespace(
        symbol=symbol,
        group="candidate",
        bars=1000,
        start=datetime(2021, 1, 1, tzinfo=timezone.utc),
        end=datetime(2025, 1, 1, tzinfo=timezone.utc),
        events=event_records,
        backtest_stats={
            "trade_count": trade_count,
            "total_return": total_return,
            "sharpe": sharpe,
            "max_drawdown": max_drawdown,
            "profit_factor": profit_factor,
            "win_rate": win_rate,
        },
        event_hit_rate=event_hit_rate,
        stop_failures=stop_failures,
    )
    return result


class TestPrefilterAssets:
    def test_accepts_broad_liquid_symbol(self):
        assets = [AssetInfo(symbol="GOOD", name="Good", exchange="NYSE")]
        bars = {"GOOD": _recent_df()}

        candidates, rejections = prefilter_assets(
            assets,
            bars,
            config=StaticUniverseConfig(),
        )

        assert rejections == {}
        assert [candidate.symbol for candidate in candidates] == ["GOOD"]

    def test_blacklist_rejects_symbol(self):
        assets = [AssetInfo(symbol="DINO", name="Dino", exchange="NYSE")]
        bars = {"DINO": _recent_df()}

        candidates, rejections = prefilter_assets(
            assets,
            bars,
            config=StaticUniverseConfig(),
        )

        assert candidates == []
        assert rejections["blacklist"] == 1

    def test_rejects_illiquid_symbol(self):
        assets = [AssetInfo(symbol="THIN", name="Thin Co", exchange="NYSE")]
        thin = _recent_df().copy()
        thin["volume"] = 100_000.0
        bars = {"THIN": thin}

        candidates, rejections = prefilter_assets(
            assets,
            bars,
            config=StaticUniverseConfig(),
        )

        assert candidates == []
        assert rejections["share_volume"] == 1

    def test_rejects_non_stock_product(self):
        assets = [AssetInfo(symbol="XLV", name="Health Care Select Sector SPDR Fund", exchange="ARCA")]
        bars = {"XLV": _recent_df()}

        candidates, rejections = prefilter_assets(
            assets,
            bars,
            config=StaticUniverseConfig(),
        )

        assert candidates == []
        assert rejections["non_stock_product"] == 1

    def test_rejects_preferred_style_symbol(self):
        assets = [AssetInfo(symbol="BA.PRA", name="Boeing Preferred", exchange="NYSE")]
        bars = {"BA.PRA": _recent_df()}

        candidates, rejections = prefilter_assets(
            assets,
            bars,
            config=StaticUniverseConfig(),
        )

        assert candidates == []
        assert rejections["non_stock_product"] == 1

    def test_does_not_require_recent_oversold_activity(self):
        assets = [AssetInfo(symbol="STEADY", name="Steady", exchange="NYSE")]
        bars = {"STEADY": _flat_recent_df()}

        candidates, rejections = prefilter_assets(
            assets,
            bars,
            config=StaticUniverseConfig(),
        )

        assert rejections == {}
        assert [candidate.symbol for candidate in candidates] == ["STEADY"]


class TestRankStaticUniverse:
    def test_promotes_profitable_trade_dense_candidate(self):
        recent = [
            PrefilterCandidate(
                symbol="GOOD",
                price=100.0,
                avg_volume_20=800_000.0,
                avg_dollar_volume_50=80_000_000.0,
                recent_oversold_events=2,
                recent_rsi=42.0,
                market_cap=5_000_000_000.0,
                sector="TECHNOLOGY",
            )
        ]
        ranked = rank_static_universe(
            recent,
            [
                _validation(
                    "GOOD",
                    trade_count=6.0,
                    total_return=0.60,
                    sharpe=1.00,
                    max_drawdown=-0.30,
                    profit_factor=2.50,
                )
            ],
            config=StaticUniverseConfig(),
        )

        assert ranked[0].symbol == "GOOD"
        assert ranked[0].verdict == "PROMOTE"

    def test_prefers_more_profitable_trade_dense_symbol(self):
        recent = [
            PrefilterCandidate("OK", 100.0, 800_000.0, 80_000_000.0, 2, 42.0, 5_000_000_000.0, "TECHNOLOGY"),
            PrefilterCandidate("BEST", 100.0, 800_000.0, 80_000_000.0, 2, 42.0, 5_000_000_000.0, "TECHNOLOGY"),
        ]
        ranked = rank_static_universe(
            recent,
            [
                _validation(
                    "OK",
                    trade_count=4.0,
                    total_return=0.20,
                    sharpe=0.50,
                    max_drawdown=-0.25,
                    profit_factor=1.30,
                ),
                _validation(
                    "BEST",
                    trade_count=8.0,
                    total_return=0.90,
                    sharpe=1.20,
                    max_drawdown=-0.30,
                    profit_factor=3.00,
                ),
            ],
            config=StaticUniverseConfig(),
        )

        assert [item.symbol for item in ranked] == ["BEST", "OK"]

    def test_rejects_weak_candidate(self):
        recent = [
            PrefilterCandidate("WEAK", 100.0, 800_000.0, 80_000_000.0, 2, 42.0, 5_000_000_000.0, "TECHNOLOGY")
        ]
        ranked = rank_static_universe(
            recent,
            [
                _validation(
                    "WEAK",
                    trade_count=3.0,
                    total_return=0.02,
                    sharpe=0.10,
                    max_drawdown=-0.40,
                    profit_factor=0.90,
                )
            ],
            config=StaticUniverseConfig(),
        )

        assert ranked[0].verdict == "REJECT"
        assert "exact strategy return below threshold" in ranked[0].reasons


class TestApplyFundamentalGate:
    def test_rejects_sub_2b_market_cap_after_backtest_ranking(self, monkeypatch, tmp_path):
        recent = [
            PrefilterCandidate("SMALL", 100.0, 800_000.0, 80_000_000.0, 0, 50.0, None, "UNKNOWN")
        ]
        ranked = rank_static_universe(
            recent,
            [
                _validation(
                    "SMALL",
                    trade_count=6.0,
                    total_return=0.40,
                    sharpe=1.0,
                    max_drawdown=-0.20,
                    profit_factor=2.0,
                )
            ],
            config=StaticUniverseConfig(top=10),
        )
        monkeypatch.setattr(
            "scripts.rsi_static_universe.fetch_fundamentals",
            lambda _symbol: SimpleNamespace(market_cap=1_000_000_000.0, error=None),
        )
        monkeypatch.setattr(
            "scripts.rsi_static_universe.assess_fitness",
            lambda _fundamentals, _profile: SimpleNamespace(solvency_ok=True, error=None),
        )
        monkeypatch.setattr(
            "scripts.rsi_static_universe._fetch_sector",
            lambda _symbol: "TECHNOLOGY",
        )

        filtered, rejections = apply_fundamental_gate(
            ranked,
            config=StaticUniverseConfig(),
            cache_path=tmp_path / "fundamentals.json",
        )

        assert filtered == []
        assert rejections["market_cap"] == 1

    def test_enriches_sector_and_uses_cache(self, monkeypatch, tmp_path):
        recent = [
            PrefilterCandidate("GOOD", 100.0, 800_000.0, 80_000_000.0, 0, 50.0, None, "UNKNOWN")
        ]
        ranked = rank_static_universe(
            recent,
            [
                _validation(
                    "GOOD",
                    trade_count=6.0,
                    total_return=0.40,
                    sharpe=1.0,
                    max_drawdown=-0.20,
                    profit_factor=2.0,
                )
            ],
            config=StaticUniverseConfig(top=10),
        )
        fetch_calls = {"count": 0}

        def fake_fetch(_symbol):
            fetch_calls["count"] += 1
            return SimpleNamespace(market_cap=5_000_000_000.0, error=None)

        monkeypatch.setattr("scripts.rsi_static_universe.fetch_fundamentals", fake_fetch)
        monkeypatch.setattr(
            "scripts.rsi_static_universe.assess_fitness",
            lambda _fundamentals, _profile: SimpleNamespace(solvency_ok=True, error=None),
        )
        monkeypatch.setattr(
            "scripts.rsi_static_universe._fetch_sector",
            lambda _symbol: "TECHNOLOGY",
        )

        cache_path = tmp_path / "fundamentals.json"
        filtered1, rejections1 = apply_fundamental_gate(
            ranked,
            config=StaticUniverseConfig(),
            cache_path=cache_path,
        )
        filtered2, rejections2 = apply_fundamental_gate(
            ranked,
            config=StaticUniverseConfig(),
            cache_path=cache_path,
        )

        assert rejections1 == {}
        assert rejections2 == {}
        assert fetch_calls["count"] == 1
        assert filtered1[0].recent.market_cap == 5_000_000_000.0
        assert filtered1[0].recent.sector == "TECHNOLOGY"
        assert filtered2[0].recent.market_cap == 5_000_000_000.0


class TestAssembleFinalBasket:
    def test_respects_target_size_and_sector_cap(self):
        ranked = rank_static_universe(
            [
                PrefilterCandidate("A", 100.0, 800_000.0, 80_000_000.0, 2, 42.0, 5_000_000_000.0, "TECHNOLOGY"),
                PrefilterCandidate("B", 100.0, 800_000.0, 80_000_000.0, 2, 42.0, 5_000_000_000.0, "TECHNOLOGY"),
                PrefilterCandidate("C", 100.0, 800_000.0, 80_000_000.0, 2, 42.0, 5_000_000_000.0, "TECHNOLOGY"),
                PrefilterCandidate("D", 100.0, 800_000.0, 80_000_000.0, 2, 42.0, 5_000_000_000.0, "HEALTHCARE"),
            ],
            [
                _validation("A", trade_count=8.0, total_return=0.8, sharpe=1.0, max_drawdown=-0.3, profit_factor=2.0),
                _validation("B", trade_count=7.0, total_return=0.7, sharpe=0.9, max_drawdown=-0.3, profit_factor=2.0),
                _validation("C", trade_count=6.0, total_return=0.6, sharpe=0.8, max_drawdown=-0.3, profit_factor=2.0),
                _validation("D", trade_count=5.0, total_return=0.5, sharpe=0.7, max_drawdown=-0.3, profit_factor=2.0),
            ],
            config=StaticUniverseConfig(top=10),
        )
        selected, near_misses = assemble_final_basket(
            ranked,
            config=StaticUniverseConfig(target_count=3, max_per_sector=2),
        )

        assert [item.symbol for item in selected] == ["A", "B", "D"]
        assert [item.symbol for item in near_misses][:1] == ["C"]
