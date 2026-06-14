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


# ── Walk-and-market close path (PR: MLEG walk-and-market) ──────────────────


class TestSpreadExecutionWorkerWalkAndMarket:
    """Walk-and-market mode: scheduler-driven multi-step closes.

    The single-shot path is unchanged when no scheduler is supplied;
    these tests pin the new behaviour when both close_scheduler and
    quote_provider are set.
    """

    def _quote(self, mid: float = 4.60, bid: float = 4.12, ask: float = 5.08):
        from execution.mleg_close import MlegQuote
        return MlegQuote(mid=mid, bid=bid, ask=ask)

    def _scheduler(self, profile, *, reason="stop_loss", position_id="p1"):
        from execution.mleg_close import MlegCloseScheduler
        return MlegCloseScheduler(profile, reason=reason, position_id=position_id)

    def test_construction_requires_both_scheduler_and_provider(self):
        # Mismatched (one set, one None) is rejected at construction.
        sched = self._scheduler([("mid", 30), ("market", 0)])
        with pytest.raises(ValueError, match="both be set or both be None"):
            SpreadExecutionWorker(
                legs=_open_legs(), qty=1, limit_price=3.25,
                strategy_name="credit_spread", api=MagicMock(),
                close_scheduler=sched, quote_provider=None,
            )

    def test_walk_mode_property_off_when_no_scheduler(self):
        worker = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=3.25,
            strategy_name="credit_spread", api=MagicMock(),
        )
        assert worker.walk_and_market_mode is False

    def test_walk_mode_property_on_when_scheduler_supplied(self):
        sched = self._scheduler([("mid", 30), ("market", 0)])
        worker = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=3.25,
            strategy_name="credit_spread", api=MagicMock(),
            close_scheduler=sched,
            quote_provider=lambda: self._quote(),
        )
        assert worker.walk_and_market_mode is True

    def test_first_step_fills_terminates_walk(self):
        # Step 1 (mid) fills immediately — no further steps walked.
        api = MagicMock()
        api.submit_order.return_value = _mleg_submitted("combo-1")
        api.get_order_by_id.return_value = _mleg_filled("combo-1")
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = True  # filled via stream
        stream.watch.return_value = stream_event
        on_fill = MagicMock()
        on_walk_step = MagicMock()

        sched = self._scheduler([
            ("mid",                   30),
            ("mid + 0.25*(ask-mid)",  30),
            ("market",                 0),
        ])
        worker = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=3.25,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            on_fill=on_fill,
            close_scheduler=sched,
            quote_provider=lambda: self._quote(),
            on_walk_step=on_walk_step,
        )
        worker.run()

        # Only one submit happens — step 1 fills.
        assert api.submit_order.call_count == 1
        # Terminal fill reported via outer on_fill exactly once.
        assert on_fill.call_count == 1
        terminal_call = on_fill.call_args_list[0]
        assert terminal_call.args[0] == "filled"
        # on_walk_step gets one call for step 1.
        assert on_walk_step.call_count == 1
        kwargs = on_walk_step.call_args.kwargs
        assert kwargs["step_number"] == 1
        assert kwargs["terminal_status"] == "filled"
        assert kwargs["is_market"] is False

    def test_market_fallback_fires_after_walk_exhausted(self):
        # All limit steps unfilled → walk advances to market → submits market.
        api = MagicMock()
        api.submit_order.return_value = _mleg_submitted("combo-x")
        api.get_order_by_id.return_value = _mleg_submitted("combo-x")
        stream = MagicMock()
        # All steps time out (wait returns False) — except the market step
        # which we treat as filled.
        stream_event = MagicMock()
        stream_event.wait.return_value = False  # times out on every step
        stream.watch.return_value = stream_event
        on_fill = MagicMock()
        on_walk_step = MagicMock()

        # Two limit steps + market.
        sched = self._scheduler([
            ("mid",  30),
            ("ask",  30),
            ("market", 0),
        ])
        worker = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=3.25,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            on_fill=on_fill,
            close_scheduler=sched,
            quote_provider=lambda: self._quote(),
            on_walk_step=on_walk_step,
        )
        worker.run()

        # Three submits: 2 limits + 1 market.
        assert api.submit_order.call_count == 3
        # Three step callbacks recorded.
        assert on_walk_step.call_count == 3
        # Last step is the market fallback.
        last = on_walk_step.call_args_list[-1].kwargs
        assert last["step_number"] == 3
        assert last["is_market"] is True

    def test_walk_skips_step_when_quote_provider_returns_none(self):
        # Quote outages should not crash the walk — they should skip the
        # step and advance.
        api = MagicMock()
        api.submit_order.return_value = _mleg_submitted("combo-y")
        api.get_order_by_id.return_value = _mleg_submitted("combo-y")
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = False
        stream.watch.return_value = stream_event
        on_walk_step = MagicMock()

        # Two limit steps then market. Quote provider returns None first call,
        # quote second call. The market step doesn't need a quote.
        quotes = [None, self._quote(), self._quote()]
        provider_calls = {"n": 0}
        def _provider():
            i = provider_calls["n"]
            provider_calls["n"] += 1
            return quotes[i] if i < len(quotes) else self._quote()

        sched = self._scheduler([
            ("mid",  30),
            ("ask",  30),
            ("market", 0),
        ])
        worker = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=3.25,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            close_scheduler=sched,
            quote_provider=_provider,
            on_walk_step=on_walk_step,
        )
        worker.run()

        # 3 walk-step callbacks total (skipped step 1 + limit step 2 + market).
        statuses = [c.kwargs["terminal_status"] for c in on_walk_step.call_args_list]
        assert "skipped" in statuses

    def test_quote_outage_at_market_step_still_submits_market(self):
        """
        Regression test for the autonomous-fallback guarantee.

        Even if the quote provider returns None at the moment the walk
        has advanced to the market step, the worker MUST still submit
        the market order — that step doesn't need a quote, and skipping
        it would defeat the entire point of the design (the strongest
        exit signal becoming the most fragile to network glitches).
        """
        api = MagicMock()
        # Submit accepts both orders.
        api.submit_order.return_value = _mleg_submitted("combo-final")
        # First step (limit): REST check during stream gap shows still
        # working → worker cancels and advances. Second step (market):
        # stream fires filled.
        api.get_order_by_id.side_effect = [
            _mleg_submitted("combo-limit"),       # limit REST-gap check
            _mleg_filled("combo-market"),         # market fill confirmation
        ]
        stream = MagicMock()
        stream_event = MagicMock()
        # First step: times out (False). Market step: fills via stream (True).
        stream_event.wait.side_effect = [False, True]
        stream.watch.return_value = stream_event
        on_walk_step = MagicMock()

        # Profile: one limit + market. Quote provider returns valid for
        # step 1 (limit) then None for the market step.
        sched = self._scheduler([("mid", 30), ("market", 0)])
        quotes = [self._quote(), None]
        idx = {"i": 0}
        def _provider():
            i = idx["i"]
            idx["i"] += 1
            return quotes[i] if i < len(quotes) else None

        worker = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=3.25,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            close_scheduler=sched,
            quote_provider=_provider,
            on_walk_step=on_walk_step,
        )
        worker.run()

        # Two submits — the limit step plus the market step. The market
        # step submitted DESPITE quote_provider returning None.
        assert api.submit_order.call_count == 2
        # Last step recorded the market submission, not a skip.
        last_call = on_walk_step.call_args_list[-1].kwargs
        assert last_call["is_market"] is True
        assert last_call["terminal_status"] != "skipped"


# ── PR #60 commit 9 fix C: on_submitted callback ────────────────────────────


class TestOptionsExecutionWorkerOnSubmitted:
    """The worker fires on_submitted exactly once, synchronously, after
    a successful submit_order returns. Pre-submit rejections (entry
    halt, submit_order raising) must NOT fire it."""

    def test_on_submitted_fires_after_successful_submit(self):
        api = MagicMock()
        api.submit_order.return_value = _submitted_order("ord-42")
        api.get_order_by_id.return_value = _filled_order("ord-42")
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = True
        stream.watch.return_value = stream_event
        on_fill = MagicMock()
        on_submitted = MagicMock()

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
            on_submitted=on_submitted,
        )
        worker.run()

        on_submitted.assert_called_once()
        cli_id, broker_id = on_submitted.call_args.args
        assert cli_id.startswith("opt-spy_options_reversion-")
        assert broker_id == "ord-42"

    def test_on_submitted_NOT_fired_when_submit_raises(self):
        """Pre-submit failure must not fire on_submitted — the
        substrate row stays at order_id=NULL, which is the
        truthful state."""
        api = MagicMock()
        api.submit_order.side_effect = Exception("rejected at the door")
        stream = MagicMock()
        stream_event = MagicMock()
        stream.watch.return_value = stream_event
        on_fill = MagicMock()
        on_submitted = MagicMock()

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
            on_submitted=on_submitted,
        )
        worker.run()

        on_submitted.assert_not_called()
        # on_fill DID fire with rejected — note the bug-bait: order_id
        # arg is client_order_id, NOT a real broker id. Substrate must
        # NOT use it to attach.
        on_fill.assert_called_once()
        status, _, _, order_id_arg = on_fill.call_args.args
        assert status == "rejected"

    def test_on_submitted_NOT_fired_on_entry_halt(self):
        """Global risk halt before submit also skips on_submitted."""
        api = MagicMock()
        stream = MagicMock()
        stream.watch.return_value = MagicMock()
        on_fill = MagicMock()
        on_submitted = MagicMock()

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
            on_submitted=on_submitted,
            entry_allowed=lambda: False,
        )
        worker.run()

        api.submit_order.assert_not_called()
        on_submitted.assert_not_called()

    def test_on_submitted_callback_exception_does_not_crash_worker(self):
        """A misbehaving on_submitted must not abort the worker —
        it still runs _watch_to_terminal."""
        api = MagicMock()
        api.submit_order.return_value = _submitted_order("ord-77")
        api.get_order_by_id.return_value = _filled_order("ord-77")
        stream = MagicMock()
        stream_event = MagicMock()
        stream_event.wait.return_value = True
        stream.watch.return_value = stream_event
        on_fill = MagicMock()
        on_submitted = MagicMock(side_effect=RuntimeError("substrate kaboom"))

        worker = OptionsExecutionWorker(
            decision=_decision(),
            api=api,
            stream_manager=stream,
            on_fill=on_fill,
            on_submitted=on_submitted,
        )
        worker.run()

        # _watch_to_terminal still completed and fired on_fill.
        on_fill.assert_called_once()
        assert on_fill.call_args.args[0] == "filled"
