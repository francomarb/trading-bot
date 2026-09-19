"""Tests for the report-only Donchian durable-universe selector."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from scripts.donchian_watchlist_scan import (
    Candidate,
    ScanConfig,
    _compute_metrics,
    get_open_donchian_positions,
    render_report,
    scan_candidates,
)
from scripts.sma_watchlist_scan import AssetInfo


def _bars(*, close: float = 120.0, volume: float = 1_000_000.0) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=300, freq="D", tz="UTC")
    closes = np.linspace(close * 0.75, close, len(index))
    return pd.DataFrame(
        {
            "open": closes - 0.5,
            "high": closes + 1.0,
            "low": closes - 1.0,
            "close": closes,
            "volume": np.full(len(index), volume),
        },
        index=index,
    )


def _metric(**overrides: object) -> dict[str, object]:
    metric: dict[str, object] = {
        "close": 120.0,
        "avg_dollar_volume_50": 120_000_000.0,
        "sma200": 100.0,
        "atr_pct": 0.04,
        "high_52w_ratio": 0.95,
        "momentum_12m_skip_1m": 0.20,
        "breakout_events_252": 8,
        "breakout_dates_252": ("2026-01-02", "2026-02-03"),
        "latest_breakout": False,
    }
    metric.update(overrides)
    return metric


class TestDonchianMetrics:
    def test_computes_reference_metrics_without_using_them_as_gates(self):
        metrics = _compute_metrics(_bars(), ScanConfig())

        assert metrics is not None
        assert metrics["close"] == pytest.approx(120.0)
        assert metrics["avg_dollar_volume_50"] > 100_000_000.0
        assert metrics["high_52w_ratio"] <= 1.0
        assert metrics["momentum_12m_skip_1m"] > 0
        assert int(metrics["breakout_events_252"]) > 0

    @pytest.mark.parametrize(
        "frame",
        [pd.DataFrame(), _bars().iloc[:259], _bars().drop(columns=["volume"])],
    )
    def test_rejects_unusable_history(self, frame: pd.DataFrame):
        assert _compute_metrics(frame, ScanConfig()) is None


class TestDonchianSelection:
    def test_open_position_read_fails_closed(self, tmp_path, monkeypatch):
        db_path = tmp_path / "trades.db"
        db_path.touch()

        class _BrokenTradeLogger:
            def __init__(self, *, path):
                raise OSError(f"unreadable: {path}")

        monkeypatch.setattr(
            "reporting.logger.TradeLogger",
            _BrokenTradeLogger,
        )

        with pytest.raises(RuntimeError, match="could not safely read"):
            get_open_donchian_positions(str(db_path))

    def test_default_ranking_uses_liquidity_not_historical_breakout_outcome(
        self, monkeypatch
    ):
        metrics = {
            "LIQ": _metric(
                avg_dollar_volume_50=500_000_000.0,
                breakout_events_252=0,
                momentum_12m_skip_1m=-0.50,
            ),
            "WIN": _metric(
                avg_dollar_volume_50=100_000_000.0,
                breakout_events_252=50,
                momentum_12m_skip_1m=0.80,
            ),
        }
        monkeypatch.setattr(
            "scripts.donchian_watchlist_scan._compute_metrics",
            lambda frame, _config: metrics[frame.attrs["symbol"]],
        )
        bars = {}
        for symbol in metrics:
            frame = pd.DataFrame({"close": [120.0]})
            frame.attrs["symbol"] = symbol
            bars[symbol] = frame

        candidates, rejections, _examples, _explanations = scan_candidates(
            [
                AssetInfo("LIQ", "Liquid", "NYSE"),
                AssetInfo("WIN", "Winner", "NASDAQ"),
            ],
            bars,
            config=ScanConfig(),
            include_fundamentals=False,
            top=2,
        )

        assert rejections == {}
        assert [candidate.symbol for candidate in candidates] == ["LIQ", "WIN"]

    def test_momentum_ranking_is_an_explicit_research_option(self, monkeypatch):
        metrics = {
            "LIQ": _metric(
                avg_dollar_volume_50=500_000_000.0,
                momentum_12m_skip_1m=-0.20,
            ),
            "MOM": _metric(
                avg_dollar_volume_50=100_000_000.0,
                momentum_12m_skip_1m=0.60,
            ),
        }
        monkeypatch.setattr(
            "scripts.donchian_watchlist_scan._compute_metrics",
            lambda frame, _config: metrics[frame.attrs["symbol"]],
        )
        bars = {}
        for symbol in metrics:
            frame = pd.DataFrame({"close": [120.0]})
            frame.attrs["symbol"] = symbol
            bars[symbol] = frame

        candidates, *_ = scan_candidates(
            [
                AssetInfo("LIQ", "Liquid", "NYSE"),
                AssetInfo("MOM", "Momentum", "NASDAQ"),
            ],
            bars,
            config=ScanConfig(),
            include_fundamentals=False,
            top=2,
            ranking="momentum",
        )

        assert [candidate.symbol for candidate in candidates] == ["MOM", "LIQ"]

    def test_temporary_trend_state_does_not_reject_company(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.donchian_watchlist_scan._compute_metrics",
            lambda _frame, _config: _metric(
                sma200=200.0,
                high_52w_ratio=0.20,
                momentum_12m_skip_1m=-0.90,
                breakout_events_252=0,
            ),
        )

        candidates, rejections, *_ = scan_candidates(
            [AssetInfo("BROAD", "Broad Candidate", "NYSE")],
            {"BROAD": pd.DataFrame({"close": [120.0]})},
            config=ScanConfig(),
            include_fundamentals=False,
            top=1,
        )

        assert rejections == {}
        assert [candidate.symbol for candidate in candidates] == ["BROAD"]

    def test_price_and_liquidity_are_hard_gates(self, monkeypatch):
        metrics = {
            "CHEAP": _metric(close=5.0),
            "THIN": _metric(avg_dollar_volume_50=10_000_000.0),
        }
        monkeypatch.setattr(
            "scripts.donchian_watchlist_scan._compute_metrics",
            lambda frame, _config: metrics[frame.attrs["symbol"]],
        )
        bars = {}
        for symbol in metrics:
            frame = pd.DataFrame({"close": [120.0]})
            frame.attrs["symbol"] = symbol
            bars[symbol] = frame

        candidates, rejections, *_ = scan_candidates(
            [
                AssetInfo("CHEAP", "Cheap", "NYSE"),
                AssetInfo("THIN", "Thin", "NYSE"),
            ],
            bars,
            config=ScanConfig(),
            include_fundamentals=False,
            top=2,
        )

        assert candidates == []
        assert rejections == {"price": 1, "dollar_volume": 1}

    def test_fundamentals_fail_closed(self, monkeypatch):
        monkeypatch.setattr(
            "scripts.donchian_watchlist_scan._compute_metrics",
            lambda _frame, _config: _metric(),
        )
        monkeypatch.setattr(
            "scripts.donchian_watchlist_scan.fetch_fundamentals",
            lambda _symbol: SimpleNamespace(market_cap=20_000_000_000.0),
        )
        monkeypatch.setattr(
            "scripts.donchian_watchlist_scan.assess_fitness",
            lambda _fundamentals, _profile: SimpleNamespace(
                solvency_ok=None, error=None
            ),
        )

        candidates, rejections, *_ = scan_candidates(
            [AssetInfo("UNKNOWN", "Unknown", "NYSE")],
            {"UNKNOWN": pd.DataFrame({"close": [120.0]})},
            config=ScanConfig(),
            include_fundamentals=True,
            top=1,
        )

        assert candidates == []
        assert rejections["solvency"] == 1

    def test_preferred_share_class_and_open_position_protection(self, monkeypatch):
        metrics = {
            "LIQ": _metric(avg_dollar_volume_50=500_000_000.0),
            "GOOG": _metric(avg_dollar_volume_50=100_000_000.0),
            "HELD": _metric(close=5.0),
        }
        monkeypatch.setattr(
            "scripts.donchian_watchlist_scan._compute_metrics",
            lambda frame, _config: metrics.get(frame.attrs.get("symbol", "")),
        )
        bars = {}
        for symbol in ("LIQ", "GOOG", "GOOGL", "HELD"):
            frame = pd.DataFrame({"close": [120.0]})
            frame.attrs["symbol"] = symbol
            bars[symbol] = frame

        candidates, rejections, *_ = scan_candidates(
            [AssetInfo(symbol, symbol, "NASDAQ") for symbol in bars],
            bars,
            config=ScanConfig(),
            include_fundamentals=False,
            top=1,
            protected_symbols={"HELD"},
        )

        assert [candidate.symbol for candidate in candidates] == ["LIQ", "HELD"]
        assert rejections["nonpreferred_share_class"] == 1
        assert candidates[1].notes == [
            "PROTECTED: open Donchian position; outside refreshed pool"
        ]

    def test_invalid_top_and_ranking_are_rejected(self):
        with pytest.raises(ValueError, match="top must be positive"):
            scan_candidates(
                [], {}, config=ScanConfig(), include_fundamentals=False, top=0
            )
        with pytest.raises(ValueError, match="unsupported ranking"):
            scan_candidates(
                [],
                {},
                config=ScanConfig(),
                include_fundamentals=False,
                top=1,
                ranking="profit",
            )


class TestDonchianReport:
    def test_reports_nested_pool_and_risk_coverage(self):
        candidates = [
            Candidate(
                symbol=f"S{i}",
                name=f"Symbol {i}",
                exchange="NYSE",
                sector="UNKNOWN",
                close=100.0,
                avg_dollar_volume_50=1_000_000_000.0 - i,
                market_cap=None,
                sma200=90.0,
                atr_pct=0.03,
                high_52w_ratio=0.95,
                momentum_12m_skip_1m=0.20,
                breakout_events_252=i,
                breakout_dates_252=("2025-06-01",),
                latest_breakout=i == 1,
            )
            for i in range(1, 4)
        ]

        report = render_report(
            candidates,
            Counter(),
            {},
            {},
            ranking="liquidity",
            pool_sizes=(1, 3),
            feed="sip",
            assets_seen=3,
            bars_seen=3,
            include_fundamentals=False,
            start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert "## Nested Pool Comparison" in report
        assert "| 1 |" in report
        assert "| 3 |" in report
        assert "Risk sizing binds at ATR14/close" in report
        assert "not a point-in-time backtest" in report
