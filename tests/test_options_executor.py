"""Unit tests for execution/options_executor.py."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from execution.options_executor import OptionsExecutionWorker
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
