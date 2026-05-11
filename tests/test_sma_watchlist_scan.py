"""Unit tests for scripts/sma_watchlist_scan.

Coverage focuses on pure-function helpers introduced for sma_watchlist_v2:

  - _normalize_company_name (share-class normalization)
  - _is_biotech_industry (industry-string matcher with dash variants)
  - _first_technical_rejection (v2 gate order — sma200_rising removed)
  - get_open_sma_positions (trade-DB query, including missing file)
  - _hydrate_industry_cache (don't persist empty yfinance responses)

Pure logic only — no network, no real Alpaca/yfinance calls.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from scripts.sma_watchlist_scan import (
    REJECTION_LABELS,
    RULE_VERSION,
    ScanConfig,
    _first_technical_rejection,
    _hydrate_industry_cache,
    _is_biotech_industry,
    _normalize_company_name,
    get_open_sma_positions,
)


# ── Rule version ─────────────────────────────────────────────────────────────


class TestRuleVersion:
    def test_v2_constant(self):
        assert RULE_VERSION == "sma_watchlist_v2"

    def test_sma200_rising_label_removed(self):
        # The label was retired with the rule in v2.
        assert "sma200_rising" not in REJECTION_LABELS


# ── Company-name normalization (share-class dedup key) ───────────────────────


class TestNormalizeCompanyName:
    def test_alphabet_classes_collapse(self):
        a = _normalize_company_name("Alphabet Inc Class A Common Stock")
        c = _normalize_company_name("Alphabet Inc Class C Capital Stock")
        assert a == c == "ALPHABET INC"

    def test_berkshire_classes_collapse(self):
        a = _normalize_company_name("Berkshire Hathaway Inc Class A")
        b = _normalize_company_name("Berkshire Hathaway Inc Class B")
        assert a == b == "BERKSHIRE HATHAWAY INC"

    def test_news_corp_classes_collapse(self):
        a = _normalize_company_name("News Corp Class A Common Stock")
        b = _normalize_company_name("News Corp Class B Common Stock")
        assert a == b == "NEWS CORP"

    def test_dash_class_marker(self):
        # Some feeds render as "Inc. - Class C ..." with a leading separator.
        assert _normalize_company_name("Alphabet Inc. - Class C") == "ALPHABET INC"

    def test_series_marker(self):
        a = _normalize_company_name("Foo Trust Series A")
        b = _normalize_company_name("Foo Trust Series B")
        assert a == b == "FOO TRUST"

    def test_no_class_marker_unchanged(self):
        # If there's no Class/Series suffix, the full name is the key.
        result = _normalize_company_name("Apple Inc Common Stock")
        assert result == "APPLE INC COMMON STOCK"

    def test_empty_input(self):
        assert _normalize_company_name("") == ""
        assert _normalize_company_name(None) == ""  # type: ignore[arg-type]

    def test_case_insensitive(self):
        assert (
            _normalize_company_name("alphabet inc class a")
            == _normalize_company_name("ALPHABET INC CLASS C")
            == "ALPHABET INC"
        )


# ── Biotech industry detection ───────────────────────────────────────────────


class TestIsBiotechIndustry:
    @pytest.mark.parametrize(
        "industry",
        [
            "Biotechnology",
            "BIOTECHNOLOGY",
            "biotechnology",
            "Drug Manufacturers - Specialty & Generic",
            "Drug Manufacturers—Specialty & Generic",  # em-dash
            "Drug Manufacturers–Specialty & Generic",  # en-dash
            "Diagnostics & Research",
        ],
    )
    def test_matches_biotech_variants(self, industry):
        assert _is_biotech_industry(industry) is True

    @pytest.mark.parametrize(
        "industry",
        [
            "Drug Manufacturers - General",
            "Drug Manufacturers—General",
            "Semiconductors",
            "Internet Content & Information",
            "Banks - Diversified",
            "Medical Devices",
            "Medical Distribution",
            "Healthcare Plans",
            "",
        ],
    )
    def test_does_not_match_non_biotech(self, industry):
        assert _is_biotech_industry(industry) is False

    def test_none_input(self):
        assert _is_biotech_industry(None) is False  # type: ignore[arg-type]


# ── v2 technical-rejection gate order ────────────────────────────────────────


def _metric(**overrides) -> dict:
    """Build a metric dict that passes every gate by default."""
    base = {
        "close": 100.0,
        "sma20": 99.0,
        "sma50": 95.0,
        "sma150": 90.0,
        "sma200": 85.0,
        "sma200_20d_ago": 84.0,
        "avg_volume_20": 1_000_000.0,
        "avg_dollar_volume_50": 100_000_000.0,
        "high_52w": 110.0,
        "low_52w": 60.0,
        "adx14": 25.0,
        "adx14_5d_ago": 22.0,
        "plus_di14": 30.0,
        "minus_di14": 15.0,
        "atr_pct": 0.03,
        "momentum_12m_skip_1m": 1.0,
        "crossover_count_1y": 2,
    }
    base.update(overrides)
    return base


class TestFirstTechnicalRejectionV2:
    def setup_method(self):
        self.cfg = ScanConfig()

    def test_clean_metric_passes(self):
        assert _first_technical_rejection(_metric(), self.cfg) is None

    def test_sma200_rising_no_longer_rejects(self):
        # In v1 this would have rejected (sma200 == sma200_20d_ago).
        # In v2 the rule is retired; alignment + close>SMA200 carry the trend test.
        m = _metric(sma200=85.0, sma200_20d_ago=85.0)
        assert _first_technical_rejection(m, self.cfg) is None

    def test_sma200_falling_no_longer_rejects(self):
        # In v1 this would have rejected. In v2 it's accepted as long as
        # alignment and close>SMA200 still hold.
        m = _metric(sma200=85.0, sma200_20d_ago=90.0)
        assert _first_technical_rejection(m, self.cfg) is None

    def test_price_gate(self):
        assert _first_technical_rejection(_metric(close=5.0), self.cfg) == "price"

    def test_share_volume_gate(self):
        assert _first_technical_rejection(_metric(avg_volume_20=100), self.cfg) == "share_volume"

    def test_dollar_volume_gate(self):
        assert (
            _first_technical_rejection(_metric(avg_dollar_volume_50=1_000_000), self.cfg)
            == "dollar_volume"
        )

    def test_price_above_smas_gate(self):
        # Close drops below SMA50.
        m = _metric(close=94.0, sma50=95.0, sma150=90.0, sma200=85.0)
        assert _first_technical_rejection(m, self.cfg) == "price_above_smas"

    def test_sma_alignment_gate(self):
        # Alignment broken: SMA50 < SMA150.
        m = _metric(sma50=88.0, sma150=90.0)
        assert _first_technical_rejection(m, self.cfg) == "sma_alignment"

    def test_52w_low_gate(self):
        # close = 100, 1.30 * low = 1.30 * 80 = 104 > 100 → reject.
        m = _metric(close=100.0, low_52w=80.0)
        assert _first_technical_rejection(m, self.cfg) == "above_52w_low"

    def test_52w_high_gate(self):
        # close = 100, 0.75 * high = 0.75 * 200 = 150 > 100 → reject.
        m = _metric(close=100.0, high_52w=200.0)
        assert _first_technical_rejection(m, self.cfg) == "near_52w_high"

    def test_adx_gate(self):
        assert _first_technical_rejection(_metric(adx14=15.0), self.cfg) == "adx"

    def test_di_direction_gate(self):
        m = _metric(plus_di14=10.0, minus_di14=20.0)
        assert _first_technical_rejection(m, self.cfg) == "di_direction"

    def test_atr_too_low_gate(self):
        assert _first_technical_rejection(_metric(atr_pct=0.005), self.cfg) == "atr_too_low"

    def test_atr_too_high_gate(self):
        assert _first_technical_rejection(_metric(atr_pct=0.10), self.cfg) == "atr_too_high"


# ── Open-position protection (trade-DB query) ────────────────────────────────


def _build_trade_db(path: Path, rows: list[tuple]) -> None:
    """Create a minimal trades table matching production schema."""
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                filled_qty REAL,
                strategy TEXT NOT NULL,
                status TEXT NOT NULL
            )
            """
        )
        conn.executemany(
            "INSERT INTO trades (timestamp, symbol, side, qty, filled_qty, strategy, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


class TestGetOpenSmaPositions:
    def test_missing_db_returns_empty_set(self, tmp_path):
        assert get_open_sma_positions(str(tmp_path / "nope.db")) == set()

    def test_no_sma_rows_returns_empty(self, tmp_path):
        db = tmp_path / "trades.db"
        _build_trade_db(
            db,
            [
                ("2026-01-01", "AAPL", "buy", 10, 10, "rsi_reversion", "filled"),
                ("2026-01-02", "MSFT", "buy", 5, 5, "donchian_breakout", "filled"),
            ],
        )
        assert get_open_sma_positions(str(db)) == set()

    def test_net_positive_sma_position(self, tmp_path):
        db = tmp_path / "trades.db"
        _build_trade_db(
            db,
            [
                ("2026-01-01", "NVDA", "buy", 49, 49, "sma_crossover", "filled"),
            ],
        )
        assert get_open_sma_positions(str(db)) == {"NVDA"}

    def test_net_zero_excluded(self, tmp_path):
        db = tmp_path / "trades.db"
        _build_trade_db(
            db,
            [
                ("2026-01-01", "MU", "buy", 20, 20, "sma_crossover", "filled"),
                ("2026-02-01", "MU", "sell", 20, 20, "sma_crossover", "filled"),
            ],
        )
        assert get_open_sma_positions(str(db)) == set()

    def test_partial_close_still_protected(self, tmp_path):
        db = tmp_path / "trades.db"
        _build_trade_db(
            db,
            [
                ("2026-01-01", "AMD", "buy", 50, 50, "sma_crossover", "filled"),
                ("2026-02-01", "AMD", "sell", 20, 20, "sma_crossover", "filled"),
            ],
        )
        assert get_open_sma_positions(str(db)) == {"AMD"}

    def test_only_filled_status_counted(self, tmp_path):
        db = tmp_path / "trades.db"
        _build_trade_db(
            db,
            [
                ("2026-01-01", "PENDING_SYM", "buy", 10, 0, "sma_crossover", "pending"),
                ("2026-01-02", "CANCELLED_SYM", "buy", 10, 0, "sma_crossover", "cancelled"),
            ],
        )
        assert get_open_sma_positions(str(db)) == set()

    def test_multiple_strategies_isolated(self, tmp_path):
        db = tmp_path / "trades.db"
        _build_trade_db(
            db,
            [
                ("2026-01-01", "NVDA", "buy", 10, 10, "sma_crossover", "filled"),
                ("2026-01-02", "TSLA", "buy", 5, 5, "rsi_reversion", "filled"),
            ],
        )
        assert get_open_sma_positions(str(db)) == {"NVDA"}

    def test_filled_qty_null_falls_back_to_qty(self, tmp_path):
        # Older rows may have NULL filled_qty; the query coalesces to qty.
        db = tmp_path / "trades.db"
        _build_trade_db(
            db,
            [
                ("2026-01-01", "MU", "buy", 20, None, "sma_crossover", "filled"),
            ],
        )
        assert get_open_sma_positions(str(db)) == {"MU"}


# ── Industry cache: don't persist empty yfinance responses ───────────────────


class _FakeTicker:
    def __init__(self, info: dict):
        self.info = info


class TestHydrateIndustryCacheEmptyHandling:
    """Empty yfinance info must not be cached — otherwise next-run retries
    are blocked and biotech / ETF gates silently fail-open."""

    def test_non_empty_info_is_cached(self, tmp_path, monkeypatch):
        # Patch the persistence target so we don't touch the real cache file.
        cache_path = tmp_path / "industry.json"
        monkeypatch.setattr(
            "scripts.sma_watchlist_scan._INDUSTRY_CACHE_PATH", cache_path
        )

        fake_info = {
            "quoteType": "EQUITY",
            "industry": "Semiconductors",
            "sector": "Technology",
            "longName": "NVIDIA Corporation",
        }
        fake_yf = type("FakeYF", (), {"Ticker": lambda self, sym: _FakeTicker(fake_info)})()
        monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)

        cache = _hydrate_industry_cache(["NVDA"], {})
        assert "NVDA" in cache
        assert cache["NVDA"]["industry"] == "Semiconductors"
        assert cache_path.exists()
        on_disk = json.loads(cache_path.read_text())
        assert on_disk["NVDA"]["quoteType"] == "EQUITY"

    def test_empty_info_is_not_cached(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "industry.json"
        monkeypatch.setattr(
            "scripts.sma_watchlist_scan._INDUSTRY_CACHE_PATH", cache_path
        )

        fake_yf = type("FakeYF", (), {"Ticker": lambda self, sym: _FakeTicker({})})()
        monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)

        cache = _hydrate_industry_cache(["GHOST"], {})
        assert "GHOST" not in cache

    def test_yfinance_exception_does_not_cache(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "industry.json"
        monkeypatch.setattr(
            "scripts.sma_watchlist_scan._INDUSTRY_CACHE_PATH", cache_path
        )

        def _raise(self, sym):
            raise RuntimeError("yahoo rate limit")

        fake_yf = type("FakeYF", (), {"Ticker": _raise})()
        monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)

        cache = _hydrate_industry_cache(["RATE_LIMITED"], {})
        assert "RATE_LIMITED" not in cache

    def test_already_cached_symbols_skipped(self, tmp_path, monkeypatch):
        cache_path = tmp_path / "industry.json"
        monkeypatch.setattr(
            "scripts.sma_watchlist_scan._INDUSTRY_CACHE_PATH", cache_path
        )

        # If yfinance were called we'd crash — proves the cache hit path.
        def _crash(self, sym):
            raise AssertionError(f"yfinance should not be called for {sym}")

        fake_yf = type("FakeYF", (), {"Ticker": _crash})()
        monkeypatch.setitem(__import__("sys").modules, "yfinance", fake_yf)

        pre = {"NVDA": {"quoteType": "EQUITY", "industry": "Semiconductors",
                       "sector": "Technology", "longName": "NVIDIA"}}
        cache = _hydrate_industry_cache(["NVDA"], pre)
        assert cache["NVDA"]["industry"] == "Semiconductors"
