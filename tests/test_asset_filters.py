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
