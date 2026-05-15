"""
Unit tests for utils.option_symbols — pure string helpers shared by the
engine and the trade logger (no engine deps).
"""

from __future__ import annotations

from datetime import date

import pytest

from utils.option_symbols import is_occ_option, owner_key_for, parse_occ_symbol


class TestOwnerKeyFor:
    def test_equity_ticker_passthrough(self) -> None:
        assert owner_key_for("AAPL") == "AAPL"

    def test_occ_call(self) -> None:
        assert owner_key_for("SPY260516C00520000") == "SPY"

    def test_occ_put(self) -> None:
        assert owner_key_for("QQQ260620P00380000") == "QQQ"

    def test_short_ticker_option(self) -> None:
        assert owner_key_for("F260516C00012000") == "F"

    def test_lowercase_not_treated_as_occ(self) -> None:
        assert owner_key_for("spy260516c00520000") == "spy260516c00520000"


class TestIsOccOption:
    def test_equity_is_not_option(self) -> None:
        assert is_occ_option("AAPL") is False

    def test_occ_call_is_option(self) -> None:
        assert is_occ_option("SPY260516C00520000") is True

    def test_occ_put_is_option(self) -> None:
        assert is_occ_option("QQQ260620P00380000") is True

    def test_malformed_is_not_option(self) -> None:
        assert is_occ_option("SPY260516X00520000") is False


class TestParseOccSymbol:
    def test_parses_put(self) -> None:
        c = parse_occ_symbol("SPY260618P00689000")
        assert c.root == "SPY"
        assert c.expiration == date(2026, 6, 18)
        assert c.option_type == "P"
        assert c.strike == pytest.approx(689.0)

    def test_parses_call(self) -> None:
        c = parse_occ_symbol("QQQ260516C00520000")
        assert c.root == "QQQ"
        assert c.expiration == date(2026, 5, 16)
        assert c.option_type == "C"
        assert c.strike == pytest.approx(520.0)

    def test_parses_fractional_strike(self) -> None:
        # Strike 558.5 → 00558500.
        c = parse_occ_symbol("SPY260618P00558500")
        assert c.strike == pytest.approx(558.5)

    def test_short_root_ticker(self) -> None:
        c = parse_occ_symbol("F260516C00012000")
        assert c.root == "F"
        assert c.strike == pytest.approx(12.0)

    def test_equity_ticker_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid OCC option symbol"):
            parse_occ_symbol("AAPL")

    def test_malformed_raises(self) -> None:
        with pytest.raises(ValueError, match="not a valid OCC option symbol"):
            parse_occ_symbol("SPY260618X00689000")
