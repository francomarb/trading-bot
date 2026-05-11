"""Unit tests for utils.asset_filters.

The heuristic is best-effort by design; tests pin the documented behavior
so future tweaks don't silently change which asset types get excluded
from watchlist scans.
"""

from __future__ import annotations

import pytest

from utils.asset_filters import is_stock_like


class TestIsStockLikeAccepts:
    """Common stocks and similar tradable equities should pass."""

    @pytest.mark.parametrize(
        "symbol,name,exchange",
        [
            ("AAPL", "Apple Inc Common Stock", "NASDAQ"),
            ("MSFT", "Microsoft Corporation Common Stock", "NASDAQ"),
            ("NVDA", "NVIDIA Corporation Common Stock", "NASDAQ"),
            ("BRK.B", "Berkshire Hathaway Inc Class B", "NYSE"),
            ("GOOG", "Alphabet Inc Class C Capital Stock", "NASDAQ"),
            ("GOOGL", "Alphabet Inc Class A Common Stock", "NASDAQ"),
            ("MU", "Micron Technology Inc Common Stock", "NASDAQ"),
        ],
    )
    def test_accepts_common_stocks(self, symbol, name, exchange):
        assert is_stock_like(symbol, name, exchange) is True

    def test_accepts_minimal_metadata(self):
        # Even with sparse name, a clean ticker should pass.
        assert is_stock_like("XYZ", "", "") is True


class TestIsStockLikeRejectsETFs:
    """ETFs and ETNs should be excluded."""

    @pytest.mark.parametrize(
        "symbol,name",
        [
            ("SOXX", "iShares Semiconductor ETF"),
            ("SMH", "VanEck Semiconductor ETF"),
            ("SPY", "SPDR S&P 500 ETF Trust"),
            ("VOO", "Vanguard S&P 500 ETF"),
            ("QQQ", "Invesco QQQ Trust"),
            ("VTI", "Vanguard Total Stock Market Index Fund ETF"),
            ("EEM", "iShares MSCI Emerging Markets ETF"),
            ("AVLV", "Avantis US Large Cap Value ETF"),
            ("VGK", "Vanguard FTSE Europe ETF"),
            ("XLK", "SPDR Select Sector Technology"),
        ],
    )
    def test_rejects_etfs(self, symbol, name):
        assert is_stock_like(symbol, name, "NYSEARCA") is False

    def test_rejects_etn(self):
        assert is_stock_like("BARN", "Bear ETN Long Vol", "NYSEARCA") is False


class TestIsStockLikeRejectsLeveraged:
    """Leveraged / inverse / single-direction products."""

    @pytest.mark.parametrize(
        "symbol,name",
        [
            ("SQQQ", "ProShares UltraPro Short QQQ"),
            ("TQQQ", "ProShares UltraPro QQQ"),
            ("SOXL", "Direxion Daily Semiconductor Bull 3X Shares"),
            ("SOXS", "Direxion Daily Semiconductor Bear 3X Shares"),
            ("SPXU", "ProShares UltraPro Short S&P500"),
            ("UVXY", "ProShares Ultra VIX Short-Term Futures"),
        ],
    )
    def test_rejects_leveraged_products(self, symbol, name):
        assert is_stock_like(symbol, name, "NYSEARCA") is False


class TestIsStockLikeRejectsNonEquity:
    """Funds, trusts, preferreds, warrants, rights, units."""

    @pytest.mark.parametrize(
        "symbol,name,exchange",
        [
            ("MAIN", "Main Street Capital BDC Fund", "NASDAQ"),
            ("AMT", "American Tower Trust", "NYSE"),
            ("BIL", "SPDR Bloomberg 1-3 Month T-Bill ETF", "NYSEARCA"),
            ("TLT", "iShares 20+ Year Treasury Bond ETF", "NASDAQ"),
        ],
    )
    def test_rejects_funds_trusts_bonds(self, symbol, name, exchange):
        assert is_stock_like(symbol, name, exchange) is False

    @pytest.mark.parametrize(
        "symbol",
        ["AAPL.PR", "MSFT.WS", "NVDA.WT", "XYZ.RT"],
    )
    def test_rejects_preferred_warrant_right_suffixes(self, symbol):
        assert is_stock_like(symbol, "Some Name", "NYSE") is False

    def test_rejects_preferred_by_name(self):
        assert is_stock_like("ABC", "ACME Inc 7% Preferred Series A", "NYSE") is False

    def test_rejects_warrant_by_name(self):
        assert is_stock_like("XYZW", "XYZ Corp Warrant", "NASDAQ") is False

    def test_rejects_otc(self):
        # OTC string anywhere in symbol/name/exchange triggers exclusion.
        assert is_stock_like("XYZ", "XYZ Corp", "OTC Markets") is False


class TestIsStockLikeEdgeCases:
    def test_handles_none_inputs(self):
        # Defensive: empty/None strings should not crash. Empty everywhere
        # passes (nothing matches an exclusion term).
        assert is_stock_like("XYZ", None, None) is True
        assert is_stock_like(None, "Some Name", None) is True

    def test_case_insensitive(self):
        # The match uses uppercased text — lower-case ETF should still be caught.
        assert is_stock_like("soxx", "ishares semiconductor etf", "nyse") is False


class TestIsStockLikeWordBoundaries:
    """Regression: matching must be word-bounded so substrings inside
    real company names don't trigger false positives.

    Pinned by code review: a raw 'UNIT' substring rejected UnitedHealth,
    United Airlines, and Unity. Similar concern applies to other short
    tokens that could appear inside legitimate names.
    """

    @pytest.mark.parametrize(
        "symbol,name",
        [
            ("UNH", "UnitedHealth Group Incorporated Common Stock"),
            ("UAL", "United Airlines Holdings Inc Common Stock"),
            ("U", "Unity Software Inc Common Stock"),
            ("UNP", "Union Pacific Corporation Common Stock"),
        ],
    )
    def test_unit_does_not_match_united_or_unity(self, symbol, name):
        # "UNIT" was removed from the exclusion list (replaced by "UNITS"
        # plural) precisely so these pass. Even if it returns, the
        # word-bound regex must not match substring-only occurrences.
        assert is_stock_like(symbol, name, "NYSE") is True

    def test_units_plural_still_rejected(self):
        # SPAC pre-split unit naming convention. Must still be caught.
        assert (
            is_stock_like("ACME.U", "Acme Acquisition Corp Units", "NASDAQ")
            is False
        )

    def test_bull_does_not_match_bulldog(self):
        # Word-boundary: BULLDOG has 'BULL' as a prefix substring but is
        # not a standalone 'BULL' token.
        assert is_stock_like("BLDG", "Bulldog Industries Common Stock", "NASDAQ") is True

    def test_bear_does_not_match_bearings(self):
        # 'BEAR' inside 'BEARINGS' — substring match would be a false positive.
        assert is_stock_like("NN", "NN Inc Ball Bearings Manufacturer", "NYSE") is True

    def test_pref_does_not_match_prefer(self):
        # 'PREF' inside 'PREFERENCE' — substring match would falsely exclude.
        assert is_stock_like("XYZ", "Customer Preference Holdings Common Stock", "NYSE") is True

    def test_bond_does_not_match_diamondback(self):
        # 'BOND' as substring in 'DIAMONDBACK' — must not match.
        # (Word-boundary: ND/BA boundary blocks the match anyway.)
        assert is_stock_like("FANG", "Diamondback Energy Inc Common Stock", "NASDAQ") is True

    def test_ultra_does_not_match_ultragenyx(self):
        # 'ULTRA' inside 'ULTRAGENYX' — word boundary blocks the match.
        # (This was a known false positive of the prior substring filter.)
        assert is_stock_like("RARE", "Ultragenyx Pharmaceutical Inc Common Stock", "NASDAQ") is True

    def test_short_does_not_match_company_with_short_in_name(self):
        # Hypothetical: a company name containing 'short' as a regular word
        # is not present in current Alpaca data, but boundary matching is
        # the same. Standalone 'SHORT' is not in the exclusion list — only
        # explicit leveraged-product terms.
        assert is_stock_like("XYZ", "Shortwave Communications Inc", "NASDAQ") is True

    def test_etf_suffix_word_bounded(self):
        # 'ETF' must match as a token, not inside another word. Hypothetical
        # 'METF' wouldn't trigger.
        assert is_stock_like("METF", "Metformin Holdings Inc Common Stock", "NASDAQ") is True
        assert (
            is_stock_like("XLK", "Technology Select Sector SPDR ETF", "NYSEARCA")
            is False
        )

    def test_index_does_not_match_indexing_word(self):
        # Word-bound 'INDEX' shouldn't catch 'INDEXING' inside a real name.
        assert is_stock_like("XYZ", "Smart Indexing Technology Corp", "NASDAQ") is True
        # But standalone 'Index Fund' is still rejected.
        assert is_stock_like("VOO", "Vanguard 500 Index Fund", "NYSEARCA") is False

    def test_multi_word_phrase_global_x(self):
        # Multi-word terms still match as a phrase.
        assert (
            is_stock_like("LIT", "Global X Lithium & Battery Tech ETF", "NYSEARCA")
            is False
        )
        # But "Global Excellence" (different word after Global) passes.
        assert is_stock_like("XYZ", "Global Excellence Holdings Inc", "NYSE") is True

    def test_multi_word_phrase_daily_target(self):
        assert (
            is_stock_like("XYZ", "ProShares Daily Target Volatility Fund", "NYSEARCA")
            is False
        )
        assert (
            is_stock_like("XYZ", "Daily News Holdings Targeted Acquisitions Corp", "NYSE")
            is True
        )

    def test_leveraged_token(self):
        # 'LEVERAGED' standalone matches.
        assert (
            is_stock_like("XYZ", "ProShares Leveraged Long S&P 500", "NYSEARCA")
            is False
        )

    def test_2x_3x_tokens(self):
        # '3X' and '2X' as standalone tokens still caught.
        assert is_stock_like("SOXL", "Direxion Daily Semiconductor Bull 3X Shares", "NYSEARCA") is False
        assert is_stock_like("SSO", "ProShares Ultra 2X S&P 500", "NYSEARCA") is False
        # But a name with '3xx' (no boundary) wouldn't match — hypothetical safety.
        assert is_stock_like("XYZ", "Pi3xx Networks Inc", "NASDAQ") is True
