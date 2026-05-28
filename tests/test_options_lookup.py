"""
Unit tests for ``utils.options_lookup`` (11.25 single-leg, 11.28 spreads).

The pickers take a quote-lookup callback and budget/target constraints,
then delegate ranking to ``utils.options_ranker``. These tests focus on
the glue: chain query construction, the K-nearest pre-filter, plumbing
between the chain response and the ranker, and graceful failure modes.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from alpaca.trading.enums import AssetStatus, ContractType

from utils.options_lookup import (
    ContractPick,
    SpreadPick,
    estimate_put_delta,
    find_best_call,
    find_best_put_spread,
)
from utils.options_ranker import Quote


def _contract(symbol: str, expiry: date, strike: float) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        expiration_date=expiry,
        strike_price=str(strike),
    )


def _quotes(d: dict[str, Quote]):
    """Build a quote_lookup callback that returns the canned dict, with
    every requested OCC symbol present (missing → None)."""

    def _lookup(occ_symbols: list[str]) -> dict[str, Quote | None]:
        return {occ: d.get(occ) for occ in occ_symbols}

    return _lookup


class TestFindBestCall:
    def test_chain_query_uses_band_and_dte_window(self):
        expiry = date(2026, 5, 22)
        response = SimpleNamespace(
            option_contracts=[
                _contract("SPY260522C00730000", expiry, 730.0),
                _contract("SPY260522C00735000", expiry, 735.0),
            ],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response

        quote_lookup = _quotes({
            "SPY260522C00730000": Quote(bid=8.00, ask=8.05),
            "SPY260522C00735000": Quote(bid=5.00, ask=5.05),
        })

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_call(
                "SPY", 736.94,
                min_dte=14, max_dte=28,
                max_premium_per_contract=2_000.0,
                quote_lookup=quote_lookup,
            )

        # Request shape — verifies the band and option-type constraints.
        req = client.get_option_contracts.call_args.args[0]
        assert req.status == AssetStatus.ACTIVE
        assert req.type == ContractType.CALL
        assert req.strike_price_gte == "711.26"
        assert req.strike_price_lte == "755.25"

        # A valid pick was returned (specific winner is the ranker's call).
        assert isinstance(pick, ContractPick)
        assert pick.occ_symbol in {"SPY260522C00730000", "SPY260522C00735000"}

    def test_paginates_chain_until_token_exhausted(self):
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

        quote_lookup = _quotes({
            "SPY260522C00725000": Quote(bid=9.00, ask=9.05),
            "SPY260522C00730000": Quote(bid=6.00, ask=6.05),
        })

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_call(
                "SPY", 733.71,
                min_dte=14, max_dte=28,
                max_premium_per_contract=2_000.0,
                quote_lookup=quote_lookup,
            )

        assert client.get_option_contracts.call_count == 2
        assert pick is not None
        assert pick.occ_symbol in {"SPY260522C00725000", "SPY260522C00730000"}

    def test_returns_none_when_chain_returns_only_far_off_contract(self):
        # Regression for the 2026-05-08 hotfix — far-OTM contract outside band.
        expiry = date(2026, 5, 22)
        response = SimpleNamespace(
            option_contracts=[_contract("SPY260522C00659000", expiry, 659.0)],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response

        quote_lookup = _quotes({})

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_call(
                "SPY", 736.94,
                min_dte=14, max_dte=28,
                max_premium_per_contract=2_000.0,
                quote_lookup=quote_lookup,
            )

        assert pick is None

    def test_returns_none_when_all_candidates_have_fatal_spreads(self):
        expiry = date(2026, 5, 22)
        response = SimpleNamespace(
            option_contracts=[
                _contract("SPY260522C00730000", expiry, 730.0),
                _contract("SPY260522C00735000", expiry, 735.0),
            ],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response

        # Both have spreads above the 10% FATAL_SPREAD_PCT.
        quote_lookup = _quotes({
            "SPY260522C00730000": Quote(bid=5.00, ask=6.00),  # 18% spread
            "SPY260522C00735000": Quote(bid=4.00, ask=5.00),  # 22% spread
        })

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_call(
                "SPY", 736.94,
                min_dte=14, max_dte=28,
                max_premium_per_contract=2_000.0,
                quote_lookup=quote_lookup,
            )

        assert pick is None

    def test_quote_lookup_failure_returns_none(self):
        expiry = date(2026, 5, 22)
        response = SimpleNamespace(
            option_contracts=[_contract("SPY260522C00730000", expiry, 730.0)],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response

        def _raising(_):
            raise RuntimeError("OPRA down")

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_call(
                "SPY", 736.94,
                min_dte=14, max_dte=28,
                max_premium_per_contract=2_000.0,
                quote_lookup=_raising,
            )

        assert pick is None

    def test_caps_quote_fetch_at_top_k_strike_nearest(self):
        # Six candidates in band — only top-5 strike-nearest should be quoted.
        expiry = date(2026, 5, 22)
        target_strike = 736.94 * 0.995  # ~733.36
        contracts = [
            _contract("SPY260522C00725000", expiry, 725.0),
            _contract("SPY260522C00730000", expiry, 730.0),
            _contract("SPY260522C00733000", expiry, 733.0),  # closest
            _contract("SPY260522C00735000", expiry, 735.0),
            _contract("SPY260522C00740000", expiry, 740.0),
            _contract("SPY260522C00745000", expiry, 745.0),  # farthest, should be cut
        ]
        response = SimpleNamespace(
            option_contracts=contracts,
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response

        seen: list[list[str]] = []

        def _capturing_lookup(occ_symbols: list[str]):
            seen.append(list(occ_symbols))
            return {occ: Quote(bid=5.00, ask=5.05) for occ in occ_symbols}

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_call(
                "SPY", 736.94,
                min_dte=14, max_dte=28,
                max_premium_per_contract=2_000.0,
                quote_lookup=_capturing_lookup,
            )

        assert pick is not None
        # Exactly one quote call, and at most 5 symbols requested.
        assert len(seen) == 1
        assert len(seen[0]) <= 5
        # The most-distant candidate must not be in the quoted list.
        assert "SPY260522C00745000" not in seen[0]

    def test_pick_carries_score_breakdown_and_runners_up(self):
        expiry = date(2026, 5, 22)
        response = SimpleNamespace(
            option_contracts=[
                _contract("SPY260522C00730000", expiry, 730.0),
                _contract("SPY260522C00735000", expiry, 735.0),
                _contract("SPY260522C00740000", expiry, 740.0),
            ],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response

        quote_lookup = _quotes({
            "SPY260522C00730000": Quote(bid=8.00, ask=8.05),
            "SPY260522C00735000": Quote(bid=5.00, ask=5.05),
            "SPY260522C00740000": Quote(bid=3.00, ask=3.05),
        })

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_call(
                "SPY", 736.94,
                min_dte=14, max_dte=28,
                max_premium_per_contract=2_000.0,
                quote_lookup=quote_lookup,
            )

        assert isinstance(pick, ContractPick)
        assert set(pick.components.keys()) == {
            "strike_proximity", "spread_quality", "premium_efficiency"
        }
        # Two surviving runners-up after the winner.
        assert len(pick.runners_up) == 2

    def test_picks_expiration_closest_to_dte_midpoint(self):
        """A4 — with two expirations in the DTE window, the picker selects
        the one whose DTE is closest to the (min_dte + max_dte) / 2 midpoint
        instead of always taking the nearest expiration. Mirrors
        find_best_put_spread's midpoint selection.
        """
        today = date.today()
        # min_dte=14, max_dte=28 → midpoint 21. Place expirations at +15 and
        # +21 days. The earlier expiry (15d) would win under the old
        # "nearest" rule; the new rule prefers +21d (exactly the midpoint).
        near_expiry = today + timedelta(days=15)
        mid_expiry = today + timedelta(days=21)
        response = SimpleNamespace(
            option_contracts=[
                _contract("SPY-NEAR-C00735000", near_expiry, 735.0),
                _contract("SPY-MID-C00735000", mid_expiry, 735.0),
            ],
            next_page_token=None,
        )
        client = MagicMock()
        client.get_option_contracts.return_value = response
        quote_lookup = _quotes({
            "SPY-NEAR-C00735000": Quote(bid=5.00, ask=5.05),
            "SPY-MID-C00735000": Quote(bid=5.00, ask=5.05),
        })

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_call(
                "SPY", 736.94,
                min_dte=14, max_dte=28,
                max_premium_per_contract=2_000.0,
                quote_lookup=quote_lookup,
            )

        assert pick is not None
        # The midpoint-DTE expiration is chosen over the nearer one.
        assert pick.occ_symbol == "SPY-MID-C00735000"


# ── estimate_put_delta (11.28) ──────────────────────────────────────────────


class TestEstimatePutDelta:
    def test_returns_absolute_value_in_unit_interval(self):
        d = estimate_put_delta(
            underlying_price=590.0, strike=568.0, dte_days=37, iv=0.15
        )
        assert 0.0 < d < 1.0
        # ~7% OTM put at 37 DTE / 15% IV sits near the 0.17-0.18 delta band.
        assert d == pytest.approx(0.177, abs=0.02)

    def test_deeper_otm_strike_has_smaller_delta(self):
        near = estimate_put_delta(
            underlying_price=590.0, strike=575.0, dte_days=37, iv=0.15
        )
        far = estimate_put_delta(
            underlying_price=590.0, strike=545.0, dte_days=37, iv=0.15
        )
        assert far < near

    def test_higher_iv_raises_otm_delta(self):
        low = estimate_put_delta(
            underlying_price=590.0, strike=560.0, dte_days=37, iv=0.12
        )
        high = estimate_put_delta(
            underlying_price=590.0, strike=560.0, dte_days=37, iv=0.25
        )
        assert high > low


# ── find_best_put_spread (11.28) ────────────────────────────────────────────

# Expirations must be future-dated relative to date.today() because the
# picker derives DTE from datetime.now().
_SPREAD_EXPIRY = date.today() + timedelta(days=37)
_SPREAD_STRIKES = [545, 550, 555, 558, 560, 562, 565, 568, 570, 572, 575]


def _put_chain(expiry: date = _SPREAD_EXPIRY) -> SimpleNamespace:
    return SimpleNamespace(
        option_contracts=[
            _contract(f"SPYP{k}", expiry, float(k)) for k in _SPREAD_STRIKES
        ],
        next_page_token=None,
    )


def _put_quote(strike: float) -> Quote:
    """Monotonic-in-strike premium so a higher-strike short minus a
    lower-strike long always yields a positive net credit. Tight bid/ask."""
    mid = (strike - 545) * 0.30 + 1.0
    return Quote(bid=round(mid - 0.025, 3), ask=round(mid + 0.025, 3))


def _put_quote_lookup(occ_symbols: list[str]) -> dict[str, Quote | None]:
    out: dict[str, Quote | None] = {}
    for occ in occ_symbols:
        strike = float(occ.removeprefix("SPYP"))
        out[occ] = _put_quote(strike)
    return out


class TestFindBestPutSpread:
    def test_chain_query_uses_put_type_and_strike_band(self):
        client = MagicMock()
        client.get_option_contracts.return_value = _put_chain()

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_put_spread(
                "SPY", 590.0,
                min_dte=30, max_dte=45,
                spread_width=10.0,
                target_short_delta=0.17,
                iv=0.15,
                max_loss_per_position=5_000.0,
                quote_lookup=_put_quote_lookup,
            )

        req = client.get_option_contracts.call_args.args[0]
        assert req.status == AssetStatus.ACTIVE
        assert req.type == ContractType.PUT
        # Band: 0.80 × 590 = 472.00 floor, 590.00 ceiling.
        assert req.strike_price_gte == "472.00"
        assert req.strike_price_lte == "590.00"
        assert isinstance(pick, SpreadPick)

    def test_picks_spread_with_short_delta_nearest_target(self):
        client = MagicMock()
        client.get_option_contracts.return_value = _put_chain()

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_put_spread(
                "SPY", 590.0,
                min_dte=30, max_dte=45,
                spread_width=10.0,
                target_short_delta=0.17,
                iv=0.15,
                max_loss_per_position=5_000.0,
                quote_lookup=_put_quote_lookup,
            )

        assert pick is not None
        # K=568 sits at ~0.177 delta — closest to the 0.17 target.
        assert pick.short_strike == pytest.approx(568.0)
        assert pick.long_strike == pytest.approx(558.0)
        assert pick.width == pytest.approx(10.0)
        # short mid 7.90 − long mid 4.90 = 3.00/sh credit; max loss (10−3)×100.
        assert pick.net_credit == pytest.approx(3.0, abs=0.05)
        assert pick.max_loss == pytest.approx(700.0, abs=5.0)
        assert set(pick.components.keys()) == {
            "short_delta", "net_credit", "spread_quality", "dte"
        }

    def test_long_leg_is_width_below_short(self):
        client = MagicMock()
        client.get_option_contracts.return_value = _put_chain()

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_put_spread(
                "SPY", 590.0,
                min_dte=30, max_dte=45,
                spread_width=5.0,
                target_short_delta=0.17,
                iv=0.15,
                max_loss_per_position=5_000.0,
                quote_lookup=_put_quote_lookup,
            )

        assert pick is not None
        assert pick.short_strike - pick.long_strike == pytest.approx(5.0, abs=1.0)
        assert pick.long_strike < pick.short_strike

    def test_returns_none_when_no_contracts(self):
        client = MagicMock()
        client.get_option_contracts.return_value = SimpleNamespace(
            option_contracts=[], next_page_token=None
        )
        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_put_spread(
                "SPY", 590.0,
                min_dte=30, max_dte=45,
                spread_width=10.0,
                target_short_delta=0.17,
                iv=0.15,
                max_loss_per_position=5_000.0,
                quote_lookup=_put_quote_lookup,
            )
        assert pick is None

    def test_returns_none_when_max_loss_cap_rejects_all(self):
        # All spreads have ~$700 max loss; a $300 cap rejects everything.
        client = MagicMock()
        client.get_option_contracts.return_value = _put_chain()
        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_put_spread(
                "SPY", 590.0,
                min_dte=30, max_dte=45,
                spread_width=10.0,
                target_short_delta=0.17,
                iv=0.15,
                max_loss_per_position=300.0,
                quote_lookup=_put_quote_lookup,
            )
        assert pick is None

    def test_quote_lookup_failure_returns_none(self):
        client = MagicMock()
        client.get_option_contracts.return_value = _put_chain()

        def _raising(_):
            raise RuntimeError("OPRA down")

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_put_spread(
                "SPY", 590.0,
                min_dte=30, max_dte=45,
                spread_width=10.0,
                target_short_delta=0.17,
                iv=0.15,
                max_loss_per_position=5_000.0,
                quote_lookup=_raising,
            )
        assert pick is None

    def test_paginates_chain_until_token_exhausted(self):
        first = SimpleNamespace(option_contracts=[], next_page_token="page-2")
        second = _put_chain()
        client = MagicMock()
        client.get_option_contracts.side_effect = [first, second]

        with patch("utils.options_lookup._get_client", return_value=client):
            pick = find_best_put_spread(
                "SPY", 590.0,
                min_dte=30, max_dte=45,
                spread_width=10.0,
                target_short_delta=0.17,
                iv=0.15,
                max_loss_per_position=5_000.0,
                quote_lookup=_put_quote_lookup,
            )

        assert client.get_option_contracts.call_count == 2
        assert pick is not None
