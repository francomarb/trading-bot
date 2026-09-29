"""Tests for the PLAN 11.75 report-only SMA durable selector."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from config import settings
from scripts.donchian_watchlist_scan import ScanConfig as DonchianScanConfig
from scripts.rsi_watchlist_scan import ScanConfig as RSIScanConfig
from scripts.sma_durable_watchlist_scan import (
    DEFAULT_PROMOTION_SIZE,
    RULE_VERSION,
    Candidate,
    ScanConfig,
    _compute_metrics,
    _contention,
    _fetch_fundamentals_with_retry,
    _first_rejection,
    _rank,
    get_open_sma_positions,
    render_report,
    scan_candidates,
)
from scripts.sma_watchlist_scan import AssetInfo
from scripts.watchlist_review import SymbolFundamentals


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

    def test_shared_durable_thresholds_have_one_settings_source(self):
        expected = (
            settings.DURABLE_WATCHLIST_MIN_BARS,
            settings.DURABLE_WATCHLIST_MIN_MARKET_CAP,
            settings.DURABLE_WATCHLIST_MIN_PRICE,
            settings.DURABLE_WATCHLIST_MIN_AVG_DOLLAR_VOLUME_50,
        )
        for config in (ScanConfig(), DonchianScanConfig(), RSIScanConfig()):
            assert (
                config.min_bars,
                config.min_market_cap,
                config.min_price,
                config.min_avg_dollar_volume_50,
            ) == expected
        assert RULE_VERSION == settings.SMA_WATCHLIST_RULE_VERSION
        assert DEFAULT_PROMOTION_SIZE == settings.SMA_TARGET_POOL_SIZE


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

    def test_reads_only_open_sma_ownership(self, tmp_path: Path, monkeypatch):
        path = tmp_path / "trades.db"
        path.write_bytes(b"")
        monkeypatch.setattr(
            "reporting.logger.TradeLogger.read_all_open_owners",
            lambda _self: {
                "AAPL": "sma_crossover",
                "MSFT": "rsi_reversion",
            },
        )

        assert get_open_sma_positions(str(path)) == {"AAPL"}


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


class TestScanCandidates:
    @staticmethod
    def _metric(symbol: str) -> dict[str, object]:
        liquidity = {
            "GOOD": 900.0,
            "SECOND": 800.0,
            "THIRD": 700.0,
            "PROTECTED": 600.0,
        }.get(symbol, 500.0)
        return {
            "close": 100.0,
            "avg_dollar_volume_50": liquidity,
            "sma50": 99.0,
            "sma150": 98.0,
            "sma200": 97.0,
            "atr_pct": 0.04,
            "high_52w_ratio": 0.9,
            "momentum_12m_skip_1m": 0.2,
            "crossover_dates_252": ("2026-01-02",),
        }

    def test_enforces_durable_facts_and_marks_protection_boundary(
        self,
        monkeypatch,
    ):
        symbols = (
            "GOOD",
            "SECOND",
            "THIRD",
            "PROTECTED",
            "LOWCAP",
            "UNKNOWNCAP",
            "RUNWAY6",
            "SOLVUNK",
            "ERROR",
            "GOOGL",
        )
        frames: dict[str, pd.DataFrame] = {}
        for symbol in symbols:
            frame = pd.DataFrame({"symbol": [symbol]})
            frame.attrs["symbol"] = symbol
            frames[symbol] = frame
        monkeypatch.setattr(
            "scripts.sma_durable_watchlist_scan._compute_metrics",
            lambda frame, _config: self._metric(frame.attrs["symbol"]),
        )
        facts = {
            symbol: SymbolFundamentals(
                symbol=symbol,
                market_cap=5_000_000_000.0,
                fcf_annual=-1.0,
                revenue_growth_pct=-5.0,
                is_profitable=True,
            )
            for symbol in ("GOOD", "SECOND", "THIRD", "PROTECTED")
        }
        facts.update({
            "LOWCAP": SymbolFundamentals(
                symbol="LOWCAP", market_cap=1_000_000_000.0, is_profitable=True
            ),
            "UNKNOWNCAP": SymbolFundamentals(
                symbol="UNKNOWNCAP", market_cap=None, is_profitable=True
            ),
            "RUNWAY6": SymbolFundamentals(
                symbol="RUNWAY6",
                market_cap=5_000_000_000.0,
                is_profitable=False,
                cash_runway_months=6.0,
            ),
            "SOLVUNK": SymbolFundamentals(
                symbol="SOLVUNK",
                market_cap=5_000_000_000.0,
                is_profitable=None,
            ),
            "ERROR": SymbolFundamentals(symbol="ERROR", error="provider down"),
        })
        monkeypatch.setattr(
            "scripts.sma_durable_watchlist_scan._fetch_fundamentals_with_retry",
            lambda symbol: facts[symbol],
        )
        assets = [AssetInfo(symbol, symbol, "NASDAQ") for symbol in symbols]

        selected, rejections, examples = scan_candidates(
            assets,
            frames,
            config=ScanConfig(
                min_bars=1,
                min_market_cap=2_000_000_000.0,
                min_price=10.0,
                min_avg_dollar_volume_50=1.0,
            ),
            include_fundamentals=True,
            top=4,
            promotion_size=2,
            protected_symbols={"PROTECTED"},
        )

        assert [candidate.symbol for candidate in selected] == [
            "GOOD", "SECOND", "THIRD", "PROTECTED"
        ]
        assert selected[-1].protected_for_promotion is True
        assert selected[0].fcf_ok is False
        assert selected[0].revenue_ok is False
        assert rejections == Counter({
            "nonpreferred_share_class": 1,
            "market_cap": 1,
            "market_cap_unknown": 1,
            "solvency": 1,
            "solvency_unknown": 1,
            "fundamentals_error": 1,
        })
        assert examples["nonpreferred_share_class"] == ["GOOGL"]

    def test_appends_protected_member_when_metrics_are_unavailable(
        self,
        monkeypatch,
    ):
        frame = pd.DataFrame({"symbol": ["GOOD"]})
        frame.attrs["symbol"] = "GOOD"
        monkeypatch.setattr(
            "scripts.sma_durable_watchlist_scan._compute_metrics",
            lambda candidate_frame, _config: (
                self._metric(candidate_frame.attrs["symbol"])
                if not candidate_frame.empty
                else None
            ),
        )

        selected, _, _ = scan_candidates(
            [
                AssetInfo("GOOD", "Good", "NASDAQ"),
                AssetInfo("HELD", "Held", "NYSE"),
            ],
            {"GOOD": frame},
            config=ScanConfig(
                min_bars=1,
                min_price=1.0,
                min_avg_dollar_volume_50=1.0,
            ),
            include_fundamentals=False,
            top=1,
            promotion_size=1,
            protected_symbols={"HELD"},
        )

        assert [candidate.symbol for candidate in selected] == ["GOOD", "HELD"]
        assert selected[-1].in_ranked_pool is False
        assert selected[-1].protected_for_promotion is True
        assert selected[-1].notes == [
            "PROTECTED: open SMA position; metrics unavailable"
        ]


class TestRenderReport:
    def test_renders_contract_capacity_caveat_protection_and_rejections(self):
        first = _candidate("FIRST", 200.0, 0.2, 0.9)
        second = _candidate("SECOND", 100.0, 0.1, 0.8)
        protected = _candidate("HELD", 50.0, 0.0, 0.7)
        protected.in_ranked_pool = False
        protected.protected_for_promotion = True
        protected.notes.append("PROTECTED: open SMA position; outside promoted pool")

        report = render_report(
            [first, second, protected],
            Counter({"solvency": 1}),
            defaultdict(list, {"solvency": ["WEAK"]}),
            ranking="liquidity",
            pool_sizes=(1, 2),
            promotion_size=2,
            feed="sip",
            assets_seen=3,
            bars_seen=3,
            include_fundamentals=True,
            start=datetime(2025, 1, 1, tzinfo=timezone.utc),
            end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert f"Rule version: `{settings.SMA_WATCHLIST_RULE_VERSION}`" in report
        assert "Days above hard 8-position count" in report
        assert "`SLEEVE_FULL` can bind first" in report
        assert "## Protected Open SMA Positions" in report
        assert "| `solvency` | 1 |" in report
