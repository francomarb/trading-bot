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
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from reporting.logger import TradeLogger, TradeRecord
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


def _log_trade(
    tl: TradeLogger,
    *,
    symbol: str,
    side: str,
    qty: float,
    strategy: str,
    status: str = "filled",
    filled_qty: float | None = None,
    reason: str = "test",
) -> None:
    """Append a TradeRecord through the production logger.

    Using the real logger guarantees the test DB has the same schema and
    insert semantics as production, so this module's behavior is tested
    against the actual contract — not a hand-rolled mirror.
    """
    tl.log(
        TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            symbol=symbol,
            side=side,
            qty=qty,
            avg_fill_price=100.0,
            order_id=None,
            strategy=strategy,
            reason=reason,
            stop_price=0.0,
            entry_reference_price=0.0,
            modeled_slippage_bps=0.0,
            realized_slippage_bps=0.0,
            order_type="market",
            status=status,
            requested_qty=qty,
            filled_qty=qty if filled_qty is None else filled_qty,
            initial_stop_loss=None,
            initial_risk_per_share=None,
            initial_risk_dollars=None,
            realized_pnl=None,
            r_multiple=None,
            entry_timestamp=None,
            exit_timestamp=None,
        )
    )


class TestGetOpenSmaPositions:
    """``get_open_sma_positions`` must mirror the engine's ownership
    reconstruction semantics exactly (``TradeLogger.read_all_open_owners``)
    so the watchlist refresh never orphans a held position."""

    def test_missing_db_returns_empty_set(self, tmp_path):
        assert get_open_sma_positions(str(tmp_path / "nope.db")) == set()

    def test_empty_db_returns_empty_set(self, tmp_path):
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        tl._ensure_db()  # create the schema without any rows
        tl.close()
        assert get_open_sma_positions(str(db)) == set()

    def test_simple_open_buy_is_returned(self, tmp_path):
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        _log_trade(tl, symbol="NVDA", side="buy", qty=49, strategy="sma_crossover")
        tl.close()
        assert get_open_sma_positions(str(db)) == {"NVDA"}

    def test_buy_then_sell_is_closed(self, tmp_path):
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        _log_trade(tl, symbol="MU", side="buy", qty=20, strategy="sma_crossover")
        _log_trade(tl, symbol="MU", side="sell", qty=20, strategy="sma_crossover")
        tl.close()
        assert get_open_sma_positions(str(db)) == set()

    def test_only_sma_strategy_is_returned(self, tmp_path):
        # RSI / Donchian positions must not be returned as SMA protected.
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        _log_trade(tl, symbol="NVDA", side="buy", qty=10, strategy="sma_crossover")
        _log_trade(tl, symbol="TSLA", side="buy", qty=5, strategy="rsi_reversion")
        _log_trade(tl, symbol="META", side="buy", qty=3, strategy="donchian_breakout")
        tl.close()
        assert get_open_sma_positions(str(db)) == {"NVDA"}

    def test_partial_fill_is_protected(self, tmp_path):
        # Regression: previous implementation filtered status='filled' only
        # and would orphan partially-filled entries. The engine treats
        # status='partial' as a real position; protect them.
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        _log_trade(
            tl,
            symbol="AMD",
            side="buy",
            qty=30,
            filled_qty=20,
            strategy="sma_crossover",
            status="partial",
        )
        tl.close()
        assert get_open_sma_positions(str(db)) == {"AMD"}

    def test_external_close_marks_position_closed(self, tmp_path):
        # Regression: log_external_close() writes a zero-qty filled sell
        # marker when a position disappears externally (stop-out, manual
        # liquidation). Net-quantity SUM would still see net > 0 and treat
        # the symbol as open. Latest-row semantics correctly close it.
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        _log_trade(tl, symbol="WDC", side="buy", qty=49, strategy="sma_crossover")
        tl.log_external_close(
            symbol="WDC",
            strategy="sma_crossover",
            reason="manual_liquidation",
        )
        tl.close()
        assert get_open_sma_positions(str(db)) == set()

    def test_partial_close_via_smaller_sell_still_closed(self, tmp_path):
        # In production a sell row — even one with smaller qty than the buy —
        # marks the position as closed (the engine reconciles broker state
        # separately). The trade-log view follows latest-row semantics.
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        _log_trade(tl, symbol="AMD", side="buy", qty=50, strategy="sma_crossover")
        _log_trade(tl, symbol="AMD", side="sell", qty=20, strategy="sma_crossover")
        tl.close()
        assert get_open_sma_positions(str(db)) == set()

    def test_buy_after_sell_reopens(self, tmp_path):
        # Latest-row semantics: a new buy after a sell makes the symbol
        # owned again.
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        _log_trade(tl, symbol="MU", side="buy", qty=20, strategy="sma_crossover")
        _log_trade(tl, symbol="MU", side="sell", qty=20, strategy="sma_crossover")
        _log_trade(tl, symbol="MU", side="buy", qty=10, strategy="sma_crossover")
        tl.close()
        assert get_open_sma_positions(str(db)) == {"MU"}

    def test_non_terminal_statuses_ignored(self, tmp_path):
        # 'pending'/'cancelled'/'rejected'/'accepted' rows must not count —
        # only 'filled' and 'partial' represent real ownership.
        db = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db))
        _log_trade(tl, symbol="PEND", side="buy", qty=10, strategy="sma_crossover", status="pending")
        _log_trade(tl, symbol="CANC", side="buy", qty=10, strategy="sma_crossover", status="cancelled")
        _log_trade(tl, symbol="REJ", side="buy", qty=10, strategy="sma_crossover", status="rejected")
        tl.close()
        assert get_open_sma_positions(str(db)) == set()


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
