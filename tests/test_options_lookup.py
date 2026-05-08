from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from alpaca.trading.enums import AssetStatus, ContractType

from utils.options_lookup import find_best_call


def _contract(symbol: str, expiry: date, strike: float) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        expiration_date=expiry,
        strike_price=str(strike),
    )


class TestFindBestCall:
    def test_filters_to_target_strike_band_and_picks_nearest(self):
        expiry = date(2026, 5, 22)
        response = SimpleNamespace(
            option_contracts=[
                _contract("SPY260522C00720000", expiry, 720.0),
                _contract("SPY260522C00730000", expiry, 730.0),
                _contract("SPY260522C00735000", expiry, 735.0),
            ],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response

        with patch("utils.options_lookup._get_client", return_value=client):
            occ_symbol = find_best_call("SPY", 736.94, min_dte=14, max_dte=28)

        assert occ_symbol == "SPY260522C00735000"
        req = client.get_option_contracts.call_args.args[0]
        assert req.status == AssetStatus.ACTIVE
        assert req.type == ContractType.CALL
        assert req.strike_price_gte == "711.26"
        assert req.strike_price_lte == "755.25"

    def test_paginates_and_uses_later_page_when_needed(self):
        expiry = date(2026, 5, 22)
        first = SimpleNamespace(option_contracts=[], next_page_token="page-2")
        second = SimpleNamespace(
            option_contracts=[
                _contract("SPY260522C00725000", expiry, 725.0),
                _contract("SPY260522C00730000", expiry, 730.0),
            ],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.side_effect = [first, second]

        with patch("utils.options_lookup._get_client", return_value=client):
            occ_symbol = find_best_call("SPY", 733.71, min_dte=14, max_dte=28)

        assert occ_symbol == "SPY260522C00730000"
        assert client.get_option_contracts.call_count == 2

    def test_returns_none_when_only_far_off_contract_would_have_been_old_bug(self):
        expiry = date(2026, 5, 22)
        response = SimpleNamespace(
            option_contracts=[_contract("SPY260522C00659000", expiry, 659.0)],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response

        with patch("utils.options_lookup._get_client", return_value=client):
            occ_symbol = find_best_call("SPY", 736.94, min_dte=14, max_dte=28)

        assert occ_symbol is None
