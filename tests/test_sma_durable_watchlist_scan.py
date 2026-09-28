"""Tests for the PLAN 11.75 report-only SMA durable selector."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from scripts.sma_durable_watchlist_scan import (
    Candidate,
    ScanConfig,
    _compute_metrics,
    _contention,
    _fetch_fundamentals_with_retry,
    _first_rejection,
    _rank,
    get_open_sma_positions,
)


def _bars(rows: int = 300) -> pd.DataFrame:
    index = pd.date_range("2025-01-01", periods=rows, freq="B", tz="UTC")
    close = pd.Series([100.0 + i * 0.1 for i in range(rows)], index=index)
    return pd.DataFrame(
        {
            "open": close - 0.1,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": 1_000_000.0,
        },
        index=index,
    )


def _candidate(symbol: str, liquidity: float, momentum: float, high52: float) -> Candidate:
    return Candidate(
        symbol=symbol,
        name=symbol,
        exchange="NASDAQ",
        close=100.0,
        avg_dollar_volume_50=liquidity,
        market_cap=10_000_000_000.0,
        sma50=99.0,
        sma150=98.0,
        sma200=97.0,
        atr_pct=0.04,
        high_52w_ratio=high52,
        momentum_12m_skip_1m=momentum,
        crossover_dates_252=("2026-01-02",),
        fcf_ok=True,
        revenue_ok=True,
    )


class TestDurableMetrics:
    def test_accepts_clean_bars_and_emits_diagnostics(self):
        metric = _compute_metrics(_bars(), ScanConfig())
        assert metric is not None
        assert metric["avg_dollar_volume_50"] > 50_000_000
        assert "crossover_dates_252" in metric

    def test_rejects_insufficient_bars(self):
        assert _compute_metrics(_bars(100), ScanConfig()) is None

    def test_membership_rejection_uses_only_price_and_dollar_liquidity(self):
        config = ScanConfig()
        metric = {
            "close": 9.0,
            "avg_dollar_volume_50": 1_000_000_000.0,
        }
        assert _first_rejection(metric, config) == "price"
        metric["close"] = 100.0
        metric["avg_dollar_volume_50"] = 40_000_000.0
        assert _first_rejection(metric, config) == "dollar_volume"


class TestDurableRanking:
    def test_liquidity_is_default_style_order(self):
        candidates = [
            _candidate("LOW", 100.0, 0.9, 0.9),
            _candidate("HIGH", 200.0, 0.1, 0.1),
        ]
        assert [c.symbol for c in _rank(candidates, "liquidity")] == ["HIGH", "LOW"]

    def test_research_rankings_do_not_use_crossover_outcomes(self):
        candidates = [
            _candidate("MOM", 100.0, 0.9, 0.2),
            _candidate("HIGH52", 200.0, 0.1, 0.95),
        ]
        assert _rank(candidates, "momentum")[0].symbol == "MOM"
        assert _rank(candidates, "high52")[0].symbol == "HIGH52"

    def test_contention_counts_days_and_capacity(self):
        first = _candidate("A", 1.0, 0.0, 0.0)
        second = _candidate("B", 2.0, 0.0, 0.0)
        second.crossover_dates_252 = ("2026-01-02", "2026-01-03")
        assert _contention([first, second], capacity=1) == (2, 2, 1)


class TestLifecycleSafety:
    def test_missing_trade_database_fails_closed(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError, match="trade database does not exist"):
            get_open_sma_positions(str(tmp_path / "missing.db"))


class TestFundamentalsRetry:
    def test_retries_one_whole_request_error(self, monkeypatch):
        class Result:
            def __init__(self, error):
                self.error = error

        results = iter([Result("timeout"), Result(None)])
        calls: list[str] = []
        monkeypatch.setattr(
            "scripts.sma_durable_watchlist_scan.fetch_fundamentals",
            lambda symbol: calls.append(symbol) or next(results),
        )
        monkeypatch.setattr(
            "scripts.sma_durable_watchlist_scan.time.sleep", lambda _delay: None
        )
        result = _fetch_fundamentals_with_retry("AAPL")
        assert result.error is None
        assert calls == ["AAPL", "AAPL"]

    def test_invalid_attempt_count_rejected(self):
        with pytest.raises(ValueError, match="attempts must be positive"):
            _fetch_fundamentals_with_retry("AAPL", attempts=0)
