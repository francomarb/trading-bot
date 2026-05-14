"""
Unit tests for utils.option_symbols — pure string helpers shared by the
engine and the trade logger (no engine deps).
"""

from __future__ import annotations

from utils.option_symbols import is_occ_option, owner_key_for


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
