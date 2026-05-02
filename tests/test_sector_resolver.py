"""
Unit tests for sector/resolver.py — SectorResolver.

All tests are offline; yfinance and Alpaca API calls are mocked.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from sector.resolver import SectorResolver


VALID_SECTORS = {
    "technology", "semiconductors", "financials", "energy",
    "utilities", "healthcare", "industrials", "staples",
    "discretionary", "materials", "real_estate", "communications",
}


class TestSectorResolverCache:
    def test_loads_empty_cache_when_file_missing(self, tmp_path):
        r = SectorResolver(cache_path=tmp_path / "missing.json")
        assert r._cache == {}

    def test_loads_existing_cache(self, tmp_path):
        cache_file = tmp_path / "sector_map.json"
        data = {
            "NVDA": {"sector": "Technology", "industry": "Semiconductors", "normalized": "semiconductors"},
        }
        cache_file.write_text(json.dumps(data))
        r = SectorResolver(cache_path=cache_file)
        assert r._cache == data

    def test_resolve_returns_normalized_from_cache(self, tmp_path):
        cache_file = tmp_path / "sector_map.json"
        cache_file.write_text(json.dumps({
            "NVDA": {"sector": "Technology", "industry": "Semiconductors", "normalized": "semiconductors"},
        }))
        r = SectorResolver(cache_path=cache_file)
        assert r.resolve("NVDA") == "semiconductors"

    def test_resolve_returns_none_for_missing_symbol(self, tmp_path):
        r = SectorResolver(cache_path=tmp_path / "empty.json")
        assert r.resolve("AAPL") is None

    def test_save_and_reload_cache(self, tmp_path):
        cache_file = tmp_path / "sector_map.json"
        r = SectorResolver(cache_path=cache_file)
        r._cache["NVDA"] = {"sector": "Technology", "industry": "Semiconductors", "normalized": "semiconductors"}
        r._save_cache()

        r2 = SectorResolver(cache_path=cache_file)
        assert r2.resolve("NVDA") == "semiconductors"

    def test_corrupted_cache_file_returns_empty(self, tmp_path):
        cache_file = tmp_path / "bad.json"
        cache_file.write_text("{ not valid json")
        r = SectorResolver(cache_path=cache_file)
        assert r._cache == {}


class TestSectorResolverNormalization:
    def _resolver(self, tmp_path):
        return SectorResolver(
            cache_path=tmp_path / "cache.json",
            valid_sectors=VALID_SECTORS,
        )

    def test_semiconductor_industry_beats_tech_sector(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("Semiconductors", "Technology")
        assert result == "semiconductors"

    def test_semiconductor_equipment_maps_to_semiconductors(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("Semiconductor Equipment & Materials", "Technology")
        assert result == "semiconductors"

    def test_technology_sector_maps_when_no_industry_match(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("Consumer Electronics", "Technology")
        assert result == "technology"

    def test_financials_sector(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("Capital Markets", "Financial Services")
        assert result == "financials"

    def test_consumer_cyclical_maps_to_discretionary(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("Specialty Retail", "Consumer Cyclical")
        assert result == "discretionary"

    def test_consumer_defensive_maps_to_staples(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("Packaged Foods", "Consumer Defensive")
        assert result == "staples"

    def test_unknown_industry_and_sector_returns_none(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("Something Unknown", "Something Else")
        assert result is None

    def test_empty_strings_return_none(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("", "")
        assert result is None

    def test_valid_sectors_filter_rejects_unmapped_key(self, tmp_path):
        # Create resolver with only one sector — all others filtered out
        r = SectorResolver(
            cache_path=tmp_path / "cache.json",
            valid_sectors={"semiconductors"},
        )
        result = r._normalize("Consumer Electronics", "Technology")
        assert result is None  # "technology" not in valid_sectors

    def test_case_insensitive_normalization(self, tmp_path):
        r = self._resolver(tmp_path)
        result = r._normalize("SEMICONDUCTORS", "TECHNOLOGY")
        assert result == "semiconductors"


class TestSectorResolverYFinanceLookup:
    def _make_info(self, industry="", sector="", quote_type="EQUITY"):
        return {"industry": industry, "sector": sector, "quoteType": quote_type}

    def test_etf_returns_none(self, tmp_path):
        r = SectorResolver(cache_path=tmp_path / "cache.json")
        info = self._make_info(quote_type="ETF")
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.info = info
            result = r._lookup_yfinance("XLK")
        assert result is None

    def test_semiconductor_stock_maps_correctly(self, tmp_path):
        r = SectorResolver(
            cache_path=tmp_path / "cache.json",
            valid_sectors=VALID_SECTORS,
        )
        info = self._make_info(industry="Semiconductors", sector="Technology")
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.info = info
            result = r._lookup_yfinance("NVDA")
        assert result is not None
        assert result["normalized"] == "semiconductors"
        assert result["industry"] == "Semiconductors"
        assert result["sector"] == "Technology"

    def test_tech_stock_with_no_industry_match(self, tmp_path):
        r = SectorResolver(
            cache_path=tmp_path / "cache.json",
            valid_sectors=VALID_SECTORS,
        )
        info = self._make_info(industry="Consumer Electronics", sector="Technology")
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.info = info
            result = r._lookup_yfinance("AAPL")
        assert result is not None
        assert result["normalized"] == "technology"

    def test_empty_info_returns_none(self, tmp_path):
        r = SectorResolver(cache_path=tmp_path / "cache.json")
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.info = {}
            result = r._lookup_yfinance("AAPL")
        assert result is None

    def test_unmappable_stock_returns_none(self, tmp_path):
        r = SectorResolver(
            cache_path=tmp_path / "cache.json",
            valid_sectors=VALID_SECTORS,
        )
        info = self._make_info(industry="Unknown Industry", sector="Unknown Sector")
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.info = info
            result = r._lookup_yfinance("WEIRD")
        assert result is None


class TestSectorResolverHydrate:
    def test_hydrate_skips_already_cached(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        cache_file.write_text(json.dumps({
            "NVDA": {"sector": "Technology", "industry": "Semiconductors", "normalized": "semiconductors"},
        }))
        r = SectorResolver(cache_path=cache_file, valid_sectors=VALID_SECTORS)

        # hydrate should not call yfinance for already-cached NVDA
        with patch.object(r, "_lookup_yfinance") as mock_lookup:
            r.hydrate(["NVDA"])
            mock_lookup.assert_not_called()

    def test_hydrate_resolves_missing_symbols(self, tmp_path):
        r = SectorResolver(
            cache_path=tmp_path / "cache.json",
            valid_sectors=VALID_SECTORS,
        )
        entry = {"sector": "Financial Services", "industry": "Capital Markets", "normalized": "financials"}

        with patch.object(r, "_lookup_with_retry", return_value=entry) as mock_lookup:
            r.hydrate(["MS", "GS"])
            assert mock_lookup.call_count == 2

        assert r.resolve("MS") == "financials"
        assert r.resolve("GS") == "financials"

    def test_hydrate_fail_open_when_lookup_fails(self, tmp_path):
        r = SectorResolver(cache_path=tmp_path / "cache.json")
        with patch.object(r, "_lookup_with_retry", return_value=None):
            r.hydrate(["WEIRD"])
        assert r.resolve("WEIRD") is None  # not cached, but no crash

    def test_hydrate_persists_cache_to_disk(self, tmp_path):
        cache_file = tmp_path / "cache.json"
        r = SectorResolver(cache_path=cache_file, valid_sectors=VALID_SECTORS)
        entry = {"sector": "Technology", "industry": "Semiconductors", "normalized": "semiconductors"}

        with patch.object(r, "_lookup_with_retry", return_value=entry):
            r.hydrate(["NVDA"])

        assert cache_file.exists()
        saved = json.loads(cache_file.read_text())
        assert "NVDA" in saved

    def test_hydrate_retries_on_failure(self, tmp_path):
        r = SectorResolver(
            cache_path=tmp_path / "cache.json",
            valid_sectors=VALID_SECTORS,
            max_retries=2,
        )
        entry = {"sector": "Technology", "industry": "Semiconductors", "normalized": "semiconductors"}
        # First call fails, second succeeds
        with patch.object(r, "_lookup_yfinance", side_effect=[RuntimeError("timeout"), entry]):
            with patch("time.sleep"):  # don't actually sleep
                result = r._lookup_with_retry("NVDA")
        assert result == entry
