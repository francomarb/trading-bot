"""
Unit tests for engine.positions — the Position abstraction introduced in PR 1
of the credit spread work (PLAN.md 11.27).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from engine.positions import (
    SINGLE_LEG,
    SPREAD,
    Position,
    PositionLeg,
    make_single_leg,
    new_spread_id,
    owner_key_for,
    view_owner_map,
)


class TestOwnerKeyFor:
    def test_equity_ticker_passthrough(self) -> None:
        assert owner_key_for("AAPL") == "AAPL"

    def test_occ_call_resolves_to_underlying(self) -> None:
        assert owner_key_for("SPY260516C00520000") == "SPY"

    def test_occ_put_resolves_to_underlying(self) -> None:
        assert owner_key_for("QQQ260620P00380000") == "QQQ"

    def test_lowercase_ticker_is_not_treated_as_occ(self) -> None:
        # OCC strings are uppercase; lowercase should pass through as-is.
        assert owner_key_for("aapl") == "aapl"


class TestPositionLeg:
    def test_minimal_construction(self) -> None:
        leg = PositionLeg(symbol="AAPL", qty=10.0)
        assert leg.symbol == "AAPL"
        assert leg.qty == 10.0
        assert leg.entry_price is None
        assert leg.side == "BUY"

    def test_lowercase_side_is_normalized_to_uppercase(self) -> None:
        assert PositionLeg(symbol="A", qty=1, side="sell").side == "SELL"
        assert PositionLeg(symbol="A", qty=1, side="Buy").side == "BUY"

    def test_invalid_side_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="side must be one of"):
            PositionLeg(symbol="A", qty=1, side="SHORT")

    def test_non_string_side_is_rejected(self) -> None:
        with pytest.raises(TypeError, match="side must be a string"):
            PositionLeg(symbol="A", qty=1, side=1)  # type: ignore[arg-type]

    def test_lowercase_sell_does_not_flip_spread_sign(self) -> None:
        """Regression: pre-normalization, side='sell' was treated as BUY,
        flipping net-credit sign. Confirm normalized math is correct."""
        from engine.positions import Position, SPREAD

        pos = Position(
            position_id="P",
            position_type=SPREAD,
            strategy_name="credit_spread",
            legs=[
                PositionLeg(symbol="A", qty=-1, entry_price=4.00, side="sell"),
                PositionLeg(symbol="B", qty=1, entry_price=1.50, side="buy"),
            ],
        )
        assert pos.legs[0].side == "SELL"
        assert pos.legs[1].side == "BUY"
        # Net credit = +4.00 - 1.50 = +2.50 (not -2.50).
        assert pos.entry_price == pytest.approx(2.50)


class TestPositionConstruction:
    def test_rejects_unknown_position_type(self) -> None:
        with pytest.raises(ValueError, match="position_type"):
            Position(
                position_id="AAPL",
                position_type="butterfly",
                strategy_name="rsi_reversion",
                legs=[PositionLeg(symbol="AAPL", qty=10)],
            )

    def test_rejects_empty_position_id(self) -> None:
        with pytest.raises(ValueError, match="position_id"):
            Position(
                position_id="",
                position_type=SINGLE_LEG,
                strategy_name="rsi_reversion",
                legs=[PositionLeg(symbol="AAPL", qty=10)],
            )

    def test_rejects_empty_strategy_name(self) -> None:
        with pytest.raises(ValueError, match="strategy_name"):
            Position(
                position_id="AAPL",
                position_type=SINGLE_LEG,
                strategy_name="",
                legs=[PositionLeg(symbol="AAPL", qty=10)],
            )

    def test_rejects_single_leg_with_multiple_legs(self) -> None:
        with pytest.raises(ValueError, match="single_leg"):
            Position(
                position_id="X",
                position_type=SINGLE_LEG,
                strategy_name="s",
                legs=[
                    PositionLeg(symbol="A", qty=1),
                    PositionLeg(symbol="B", qty=1),
                ],
            )

    def test_spread_with_two_legs_ok(self) -> None:
        pos = Position(
            position_id=new_spread_id(),
            position_type=SPREAD,
            strategy_name="credit_spread",
            legs=[
                PositionLeg(symbol="SPY260516P00500000", qty=-1, side="SELL"),
                PositionLeg(symbol="SPY260516P00490000", qty=1, side="BUY"),
            ],
        )
        assert pos.is_spread
        assert len(pos.legs) == 2


class TestMakeSingleLeg:
    def test_equity_position_id_equals_symbol(self) -> None:
        pos = make_single_leg(
            strategy_name="rsi_reversion",
            symbol="AAPL",
            qty=10,
            entry_price=150.0,
        )
        assert pos.position_id == "AAPL"
        assert pos.position_type == SINGLE_LEG
        assert pos.is_single_leg
        assert pos.primary_leg is not None
        assert pos.primary_leg.symbol == "AAPL"
        assert pos.entry_price == 150.0

    def test_option_position_id_is_underlying(self) -> None:
        pos = make_single_leg(
            strategy_name="spy_options_reversion",
            symbol="SPY260516C00520000",
            qty=1,
            entry_price=3.25,
            side="BUY",
        )
        assert pos.position_id == "SPY"
        assert pos.primary_leg.symbol == "SPY260516C00520000"
        assert pos.entry_price == 3.25

    def test_entry_time_propagates(self) -> None:
        ts = datetime(2026, 5, 13, 15, 30, tzinfo=timezone.utc)
        pos = make_single_leg(
            strategy_name="s",
            symbol="MSFT",
            entry_time=ts,
        )
        assert pos.primary_leg.entry_time == ts


class TestPositionEntryPriceForSpread:
    def test_spread_net_credit_when_short_premium_higher(self) -> None:
        pos = Position(
            position_id="P1",
            position_type=SPREAD,
            strategy_name="credit_spread",
            legs=[
                PositionLeg(symbol="A", qty=-1, entry_price=4.00, side="SELL"),
                PositionLeg(symbol="B", qty=1, entry_price=1.50, side="BUY"),
            ],
        )
        # Net credit = +4.00 (sold) - 1.50 (bought) = 2.50
        assert pos.entry_price == pytest.approx(2.50)

    def test_returns_none_when_any_leg_lacks_entry_price(self) -> None:
        pos = Position(
            position_id="P2",
            position_type=SPREAD,
            strategy_name="credit_spread",
            legs=[
                PositionLeg(symbol="A", qty=-1, entry_price=4.00, side="SELL"),
                PositionLeg(symbol="B", qty=1, entry_price=None, side="BUY"),
            ],
        )
        assert pos.entry_price is None


class TestNewSpreadId:
    def test_returns_unique_hex_string(self) -> None:
        a, b = new_spread_id(), new_spread_id()
        assert a != b
        assert len(a) == 32  # uuid4().hex


class TestViewOwnerMap:
    def test_collapses_to_legacy_dict(self) -> None:
        p1 = make_single_leg(strategy_name="sma", symbol="AAPL")
        p2 = make_single_leg(
            strategy_name="spy_options_reversion",
            symbol="SPY260516C00520000",
        )
        view = view_owner_map([p1, p2])
        assert view == {"AAPL": "sma", "SPY": "spy_options_reversion"}
