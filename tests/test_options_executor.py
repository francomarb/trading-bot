"""Unit tests for execution/options_executor.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from alpaca.trading.enums import (
    OrderClass as AlpacaOrderClass,
    OrderSide,
    PositionIntent,
)
from execution.options_executor import (
    OptionsExecutionWorker,
    SpreadExecutionWorker,
    SpreadLeg,
    build_mleg_request,
)
from risk.manager import RiskDecision, Side
from strategies.base import OrderType


def _decision(entry: float = 10.0) -> RiskDecision:
    return RiskDecision(
        symbol="SPY260516C00520000",
        side=Side.BUY,
        qty=2,
        entry_reference_price=entry,
        stop_price=7.5,
        strategy_name="spy_options_reversion",
        reason="test",
        order_type=OrderType.LIMIT,
        limit_price=entry,
    )


def _submitted_order(order_id: str = "ord-1", *, status: str = "accepted"):
    return SimpleNamespace(
        id=order_id,
        status=SimpleNamespace(value=status),
        filled_qty="0",
        filled_avg_price=None,
        symbol="SPY260516C00520000",
        legs=[],
    )


def _filled_order(order_id: str = "ord-1", *, status: str = "filled"):
    return SimpleNamespace(
        id=order_id,
        status=SimpleNamespace(value=status),
        filled_qty="2",
        filled_avg_price="10.5",
        symbol="SPY260516C00520000",
        legs=[],
    )


class TestOptionsExecutionWorker:
    def test_halt_after_dispatch_blocks_sdk_submit(self):
        api = MagicMock()
        stream = MagicMock()
        stream.watch.return_value = MagicMock()
        on_fill = MagicMock()

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
            client_order_id="opt-test",
            entry_allowed=lambda: False,
        )
        worker.run()

        api.submit_order.assert_not_called()
        stream.unwatch.assert_called_once_with("opt-test")
        on_fill.assert_called_once_with("rejected", 0.0, None, "opt-test")

    def test_binds_real_order_id_after_submit(self):
        api = MagicMock()
        api.submit_order.return_value = _submitted_order("ord-1")
        api.get_order_by_id.return_value = _filled_order("ord-1")
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = True
        stream.watch.return_value = stream_event
        on_fill = MagicMock()

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
        )
        worker.run()

        watched_client_id = stream.watch.call_args.args[0]
        assert watched_client_id.startswith("opt-spy_options_reversion-")
        stream.bind_submitted_order.assert_called_once_with(
            client_order_id=watched_client_id,
            order_id="ord-1",
            stop_leg_ids=[],
        )
        stream.unwatch.assert_called_once_with("ord-1")

    def test_submit_failure_reports_rejected_and_cleans_watch(self):
        api = MagicMock()
        api.submit_order.side_effect = Exception("complex orders not supported for options trading")
        stream = MagicMock()
        stream_event = MagicMock()
        stream.watch.return_value = stream_event
        on_fill = MagicMock()

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
        )
        worker.run()

        watched_client_id = stream.watch.call_args.args[0]
        stream.unwatch.assert_called_once_with(watched_client_id)
        on_fill.assert_called_once_with("rejected", 0.0, None, watched_client_id)

    def test_timeout_reconciles_broker_state_before_canceling(self):
        api = MagicMock()
        api.submit_order.return_value = _submitted_order("ord-1")
        api.get_order_by_id.return_value = _filled_order("ord-1", status="filled")
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = False
        stream.watch.return_value = stream_event
        on_fill = MagicMock()

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
        )
        worker.run()

        api.cancel_order_by_id.assert_not_called()
        on_fill.assert_called_once_with("filled", 2.0, 10.5, "ord-1")

    def test_gap_unresolved_order_still_cancels_after_timeout(self):
        api = MagicMock()
        api.submit_order.return_value = _submitted_order("ord-1")
        api.get_order_by_id.return_value = _submitted_order("ord-1")
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = False
        stream.watch.return_value = stream_event
        on_fill = MagicMock()

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
        )
        worker.run()

        api.cancel_order_by_id.assert_called_once_with("ord-1")
        on_fill.assert_called_once_with("canceled", 0.0, None, "ord-1")


# ── Multi-leg (MLEG) construction (11.28) ───────────────────────────────────

_SHORT_OCC = "SPY260620P00580000"
_LONG_OCC = "SPY260620P00570000"


def _open_legs() -> list[SpreadLeg]:
    """A standard bull put credit spread: sell the higher strike, buy lower."""
    return [
        SpreadLeg(occ_symbol=_SHORT_OCC, side=Side.SELL, opening=True),
        SpreadLeg(occ_symbol=_LONG_OCC, side=Side.BUY, opening=True),
    ]


def _mleg_submitted(order_id: str = "combo-1", *, status: str = "accepted"):
    return SimpleNamespace(
        id=order_id,
        status=SimpleNamespace(value=status),
        filled_qty="0",
        filled_avg_price=None,
        symbol=None,  # MLEG parents can carry a null top-level symbol
        legs=[{"symbol": _SHORT_OCC}, {"symbol": _LONG_OCC}],
    )


def _mleg_filled(order_id: str = "combo-1"):
    return SimpleNamespace(
        id=order_id,
        status=SimpleNamespace(value="filled"),
        filled_qty="1",
        filled_avg_price="3.25",
        symbol=None,
        legs=[{"symbol": _SHORT_OCC}, {"symbol": _LONG_OCC}],
    )


class TestSpreadLeg:
    def test_open_short_leg_maps_to_sell_to_open(self):
        leg = SpreadLeg(occ_symbol=_SHORT_OCC, side=Side.SELL, opening=True)
        alpaca = leg.to_alpaca_leg()
        assert alpaca.symbol == _SHORT_OCC
        assert alpaca.side is OrderSide.SELL
        assert alpaca.position_intent is PositionIntent.SELL_TO_OPEN
        assert alpaca.ratio_qty == 1

    def test_open_long_leg_maps_to_buy_to_open(self):
        leg = SpreadLeg(occ_symbol=_LONG_OCC, side=Side.BUY, opening=True)
        alpaca = leg.to_alpaca_leg()
        assert alpaca.side is OrderSide.BUY
        assert alpaca.position_intent is PositionIntent.BUY_TO_OPEN

    def test_closing_legs_map_to_close_intents(self):
        short_close = SpreadLeg(_SHORT_OCC, Side.SELL, opening=False).to_alpaca_leg()
        long_close = SpreadLeg(_LONG_OCC, Side.BUY, opening=False).to_alpaca_leg()
        assert short_close.position_intent is PositionIntent.SELL_TO_CLOSE
        assert long_close.position_intent is PositionIntent.BUY_TO_CLOSE


class TestBuildMlegRequest:
    def test_builds_mleg_limit_request_with_both_legs(self):
        # Negative limit = net credit required (Alpaca MLEG sign convention).
        req = build_mleg_request(
            legs=_open_legs(),
            qty=2,
            limit_price=-3.256,
            client_order_id="spr-test-abc",
        )
        assert req.order_class is AlpacaOrderClass.MLEG
        assert req.qty == 2
        assert req.limit_price == -3.26  # rounded to cents, sign preserved
        assert req.client_order_id == "spr-test-abc"
        assert len(req.legs) == 2
        assert {leg.symbol for leg in req.legs} == {_SHORT_OCC, _LONG_OCC}

    def test_rejects_single_leg(self):
        with pytest.raises(ValueError, match="≥ 2 legs"):
            build_mleg_request(
                legs=[SpreadLeg(_SHORT_OCC, Side.SELL)],
                qty=1, limit_price=-1.0, client_order_id="x",
            )

    def test_rejects_non_positive_qty(self):
        with pytest.raises(ValueError, match="qty must be ≥ 1"):
            build_mleg_request(
                legs=_open_legs(), qty=0, limit_price=-1.0, client_order_id="x",
            )


class TestSpreadExecutionWorker:
    def test_halt_after_dispatch_blocks_sdk_submit(self):
        api = MagicMock()
        stream = MagicMock()
        stream.watch.return_value = MagicMock()
        on_fill = MagicMock()

        worker = SpreadExecutionWorker(
            legs=_open_legs(),
            qty=1,
            limit_price=-1.45,
            strategy_name="credit_spread",
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
            entry_allowed=lambda: False,
        )
        worker.run()

        api.submit_order.assert_not_called()
        watched_client_id = stream.watch.call_args.args[0]
        stream.unwatch.assert_called_once_with(watched_client_id)
        on_fill.assert_called_once_with(
            "rejected", 0.0, None, watched_client_id
        )

    def test_binds_real_order_id_after_submit(self):
        api = MagicMock()
        api.submit_order.return_value = _mleg_submitted("combo-1")
        api.get_order_by_id.return_value = _mleg_filled("combo-1")
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = True
        stream.watch.return_value = stream_event
        on_fill = MagicMock()

        worker = SpreadExecutionWorker(
            legs=_open_legs(),
            qty=1,
            limit_price=3.25,
            strategy_name="credit_spread",
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
        )
        worker.run()

        watched_client_id = stream.watch.call_args.args[0]
        assert watched_client_id.startswith("spr-credit_spread-")
        stream.bind_submitted_order.assert_called_once_with(
            client_order_id=watched_client_id,
            order_id="combo-1",
            stop_leg_ids=[],
        )
        stream.unwatch.assert_called_once_with("combo-1")
        on_fill.assert_called_once_with("filled", 1.0, 3.25, "combo-1")

    def test_submit_failure_reports_rejected_and_cleans_watch(self):
        api = MagicMock()
        api.submit_order.side_effect = Exception("MLEG rejected by Alpaca")
        stream = MagicMock()
        stream.watch.return_value = MagicMock()
        on_fill = MagicMock()

        worker = SpreadExecutionWorker(
            legs=_open_legs(),
            qty=1,
            limit_price=3.25,
            strategy_name="credit_spread",
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
        )
        worker.run()

        watched_client_id = stream.watch.call_args.args[0]
        stream.unwatch.assert_called_once_with(watched_client_id)
        on_fill.assert_called_once_with("rejected", 0.0, None, watched_client_id)

    def test_unfilled_combo_cancels_after_timeout(self):
        api = MagicMock()
        api.submit_order.return_value = _mleg_submitted("combo-1")
        api.get_order_by_id.return_value = _mleg_submitted("combo-1")  # still working
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = False
        stream.watch.return_value = stream_event
        on_fill = MagicMock()

        worker = SpreadExecutionWorker(
            legs=_open_legs(),
            qty=1,
            limit_price=3.25,
            strategy_name="credit_spread",
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
        )
        worker.run()

        api.cancel_order_by_id.assert_called_once_with("combo-1")
        on_fill.assert_called_once_with("canceled", 0.0, None, "combo-1")
