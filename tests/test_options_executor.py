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


class TestSpreadExecutionWorkerEntryWalk:
    """Bounded entry walk (opening side).

    The close walk exists to guarantee an exit; this one exists to get a
    better fill *without* buying it. Everything here asserts on what the
    broker actually received — `api.submit_order` call args — because a
    walk that computes the right price and submits the wrong one is the
    failure that matters.
    """

    def _quote(self, mid=2.01, bid=1.80, ask=2.22):
        from execution.mleg_close import MlegQuote
        return MlegQuote(mid=mid, bid=bid, ask=ask)

    def _walk(self, profile=None, *, width=15.0, qty=1, floor_pct=0.13, budget=3322.0):
        from execution.mleg_entry import EntryWalkBounds, MlegEntryWalk
        return MlegEntryWalk(
            profile or [("mid", 30), ("mid - 0.5*(mid-bid)", 30), ("bid", 30)],
            bounds=EntryWalkBounds(
                width=width, qty=qty, min_credit_pct_of_width=floor_pct,
                max_loss_budget=budget,
            ),
            position_id="p1",
        )

    def _api(self, *, resolves_filled: bool = False):
        """Fake broker.

        ``resolves_filled`` controls what the REST re-check returns. It
        must be False for the "nothing fills" tests: ``_watch_to_terminal``
        re-polls REST after a stream miss, so a fake that always answers
        "filled" makes every walk terminate on rung 1 and silently voids
        every concession assertion below.
        """
        api = MagicMock()
        api.submit_order.side_effect = [
            _mleg_submitted(f"combo-{i}") for i in range(1, 12)
        ]
        api.get_order_by_id.return_value = (
            _mleg_filled("combo-1") if resolves_filled
            else _mleg_submitted("combo-1", status="accepted")
        )
        return api

    def _stream(self, fill_sequence):
        """fill_sequence: list of bools, one per step — did it fill?"""
        stream = MagicMock()
        events = []
        for filled in fill_sequence:
            ev = MagicMock()
            ev.wait.return_value = filled
            events.append(ev)
        stream.watch.side_effect = events
        return stream

    def _submitted_limits(self, api):
        return [c.kwargs.get("order_data", c.args[0] if c.args else None)
                for c in api.submit_order.call_args_list]

    def test_mode_off_by_default(self):
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=MagicMock(),
        )
        assert w.entry_walk_mode is False

    def test_entry_walk_and_close_scheduler_are_mutually_exclusive(self):
        from execution.mleg_close import MlegCloseScheduler
        sched = MlegCloseScheduler([("mid", 30)], reason="stop_loss", position_id="p")
        with pytest.raises(ValueError, match="mutually exclusive"):
            SpreadExecutionWorker(
                legs=_open_legs(), qty=1, limit_price=-2.01,
                strategy_name="credit_spread", api=MagicMock(),
                close_scheduler=sched, quote_provider=lambda: self._quote(),
                entry_walk=self._walk(),
            )

    def test_entry_walk_requires_a_quote_provider(self):
        with pytest.raises(ValueError, match="needs a quote_provider"):
            SpreadExecutionWorker(
                legs=_open_legs(), qty=1, limit_price=-2.01,
                strategy_name="credit_spread", api=MagicMock(),
                entry_walk=self._walk(), quote_provider=None,
            )

    def test_walks_down_through_the_rungs_when_nothing_fills(self):
        api = self._api()
        stream = self._stream([False, False, False])
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            entry_walk=self._walk(), quote_provider=lambda: self._quote(),
        )
        w.run()
        limits = [r.limit_price for r in self._submitted_limits(api)]
        assert len(limits) >= 2, f"walk never conceded: {limits}"
        # Credits descend (limits are negative, so they ascend toward zero).
        credits = [-p for p in limits]
        assert credits == sorted(credits, reverse=True)
        assert credits[0] == pytest.approx(2.01)

    def test_never_submits_below_the_credit_floor(self):
        """The bid is 1.80, the floor is 1.95, and the last rung is
        literally ('bid', 30) — so an unbounded walk WOULD breach."""
        api = self._api()
        stream = self._stream([False, False, False])
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            entry_walk=self._walk(), quote_provider=lambda: self._quote(),
        )
        w.run()
        for req in self._submitted_limits(api):
            assert -req.limit_price >= 1.95 - 1e-9, (
                f"submitted credit {-req.limit_price} below the 1.95 floor"
            )

    def test_never_submits_a_market_order(self):
        api = self._api()
        stream = self._stream([False, False, False])
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            entry_walk=self._walk(), quote_provider=lambda: self._quote(),
        )
        w.run()
        for req in self._submitted_limits(api):
            assert hasattr(req, "limit_price") and req.limit_price is not None, (
                "entry walk submitted a request with no limit price — that is "
                "a market order, which the entry path must never place"
            )

    def test_stops_when_the_book_will_not_pay_the_floor(self):
        """ask 1.50 < floor 1.95: nothing can fill, so submit nothing."""
        api = self._api()
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=self._stream([]),
            entry_walk=self._walk(),
            quote_provider=lambda: self._quote(mid=1.30, bid=1.10, ask=1.50),
        )
        w.run()
        assert api.submit_order.call_count == 0

    def test_a_quote_outage_submits_nothing(self):
        """No quote means no bounded price, so nothing may be sent.

        Note this holds under *any* outage handling — the walk only ever
        prices a rung from a real quote — so it is a floor, not proof
        that the abort below works. That is the next test's job.
        """
        api = self._api()
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=self._stream([]),
            entry_walk=self._walk(), quote_provider=lambda: None,
        )
        w.run()
        assert api.submit_order.call_count == 0

    def test_an_outage_mid_walk_abandons_instead_of_marching_on(self):
        """The close walk must push through an outage to guarantee an exit.
        The entry walk stops instead: there is nothing to guarantee, and
        re-polling a dead feed once per remaining rung just burns the
        attempt window.

        Asserted on the provider call count, because submit count cannot
        tell the two behaviours apart — neither one can submit without a
        quote.
        """
        api = self._api()
        calls = {"n": 0}

        def provider():
            calls["n"] += 1
            return self._quote() if calls["n"] == 1 else None

        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api,
            stream_manager=self._stream([False]),
            entry_walk=self._walk(), quote_provider=provider,
        )
        w.run()
        assert api.submit_order.call_count == 1
        # rung 1 priced, rung 2 hits the outage and stops. Marching on
        # would poll again for rung 3.
        assert calls["n"] == 2, (
            f"expected the walk to stop at the first outage, but the quote "
            f"provider was polled {calls['n']} times"
        )

    def test_fill_on_the_first_rung_stops_the_walk(self):
        api = self._api(resolves_filled=True)
        stream = self._stream([True])
        on_fill = MagicMock()
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            on_fill=on_fill, entry_walk=self._walk(),
            quote_provider=lambda: self._quote(),
        )
        w.run()
        assert api.submit_order.call_count == 1
        assert on_fill.call_count == 1
        assert on_fill.call_args.args[0] == "filled"

    def test_identical_bounded_price_is_not_resubmitted(self):
        """When the floor collapses two rungs onto one price, resubmitting
        only loses queue position."""
        api = self._api()
        stream = self._stream([False, False, False])
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            entry_walk=self._walk(
                [("mid - 0.9*(mid-bid)", 30), ("bid", 30), ("bid", 30)]
            ),
            quote_provider=lambda: self._quote(),
        )
        w.run()
        limits = [r.limit_price for r in self._submitted_limits(api)]
        assert len(limits) == len(set(limits)), f"duplicate prices submitted: {limits}"

    def test_reports_canceled_once_when_the_walk_runs_out(self):
        api = self._api()
        stream = self._stream([False, False, False])
        on_fill = MagicMock()
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            on_fill=on_fill, entry_walk=self._walk(),
            quote_provider=lambda: self._quote(),
        )
        w.run()
        assert on_fill.call_count == 1
        assert on_fill.call_args.args[0] == "canceled"

    def test_max_loss_of_every_submitted_price_stays_inside_budget(self):
        """The bound that makes 'do not overexpose to get a fill' real:
        conceding credit raises max loss."""
        api = self._api()
        stream = self._stream([False, False, False])
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            entry_walk=self._walk(budget=1300.0),
            quote_provider=lambda: self._quote(),
        )
        w.run()
        for req in self._submitted_limits(api):
            credit = -req.limit_price
            assert (15.0 - credit) * 100 * 1 <= 1300.0 + 1e-6, (
                f"credit {credit} implies max loss "
                f"${(15.0 - credit) * 100:,.2f} over the $1,300 budget"
            )

    def test_a_halt_before_the_first_rung_submits_nothing(self):
        api = self._api()
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=self._stream([]),
            entry_walk=self._walk(), quote_provider=lambda: self._quote(),
            entry_allowed=lambda: False,
        )
        w.run()
        assert api.submit_order.call_count == 0

    def test_a_halt_raised_mid_walk_stops_the_remaining_rungs(self):
        """The single-shot path checks the halt once because it makes one
        decision. A walk spans the whole attempt window, so a halt at
        second 20 must stop rungs 2 and 3 — checking only before rung 1
        would keep submitting into a halted account."""
        api = self._api()
        allowed = {"n": 0}

        def entry_allowed():
            allowed["n"] += 1
            return allowed["n"] == 1  # healthy for rung 1, halted after

        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api,
            stream_manager=self._stream([False, False, False]),
            entry_walk=self._walk(), quote_provider=lambda: self._quote(),
            entry_allowed=entry_allowed,
        )
        w.run()
        assert api.submit_order.call_count == 1, (
            "the walk kept submitting after the risk halt fired"
        )

    def test_a_mid_walk_halt_is_reported_as_rejected_not_canceled(self):
        api = self._api()
        allowed = {"n": 0}

        def entry_allowed():
            allowed["n"] += 1
            return allowed["n"] == 1

        on_fill = MagicMock()
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api,
            stream_manager=self._stream([False, False, False]),
            on_fill=on_fill, entry_walk=self._walk(),
            quote_provider=lambda: self._quote(), entry_allowed=entry_allowed,
        )
        w.run()
        assert on_fill.call_args.args[0] == "rejected"

    def test_live_order_time_is_not_shortened_when_rungs_collapse(self):
        """Review finding (PR #103), at the level that matters: the seconds
        an order is actually resting on the book.

        `_submit_walk_step` waits then cancels, so a rung that is skipped
        rather than merged contributes ZERO live time. Asserted on the
        timeout passed to the stream wait, i.e. what the worker really
        held for — not on the planner's arithmetic.
        """
        api = self._api()
        stream = MagicMock()
        waits = []

        def make_event():
            ev = MagicMock()
            ev.wait.side_effect = lambda timeout=None: waits.append(timeout) or False
            return ev

        stream.watch.side_effect = [make_event() for _ in range(8)]
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            # bid 1.80 sits under the 1.95 floor, so rungs 2-3 both clamp
            entry_walk=self._walk(
                [("mid", 60), ("mid - 0.34*(mid-bid)", 60), ("mid - 0.67*(mid-bid)", 60)]
            ),
            quote_provider=lambda: self._quote(),
        )
        w.run()
        limits = [-r.limit_price for r in self._submitted_limits(api)]
        assert len(set(limits)) < 3, "fixture must collapse rungs or it proves nothing"
        assert sum(waits) == 180, (
            f"walk rested a live order for {sum(waits)}s, not the 180s the "
            f"profile promises — collapsed rungs are being dropped, not merged"
        )

    def test_live_order_time_matches_the_profile_when_nothing_collapses(self):
        api = self._api()
        stream = MagicMock()
        waits = []

        def make_event():
            ev = MagicMock()
            ev.wait.side_effect = lambda timeout=None: waits.append(timeout) or False
            return ev

        stream.watch.side_effect = [make_event() for _ in range(8)]
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-3.42,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            entry_walk=self._walk(
                [("mid", 60), ("mid - 0.34*(mid-bid)", 60), ("mid - 0.67*(mid-bid)", 60)]
            ),
            quote_provider=lambda: self._quote(mid=3.42, bid=3.05, ask=3.79),
        )
        w.run()
        assert len(waits) == 3
        assert sum(waits) == 180

    def test_the_close_walk_gets_the_same_benchmark_fix(self):
        """`_submit_walk_step` is shared, so closes had the identical stale
        benchmark: an escalating walk that fills on step 3 was recorded
        against step 1's limit. Locking that in here so a future change to
        the entry path cannot quietly regress the close path."""
        from execution.mleg_close import MlegCloseScheduler, MlegQuote
        api = self._api()
        api.get_order_by_id.side_effect = [
            _mleg_submitted("combo-1", status="accepted"),
            _mleg_filled("combo-2"),
            _mleg_filled("combo-2"),
        ]
        sched = MlegCloseScheduler(
            [("mid", 30), ("mid + 0.5*(ask-mid)", 30)],
            reason="stop_loss", position_id="p1",
        )
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=4.60,
            strategy_name="credit_spread", api=api,
            stream_manager=self._stream([False, True]),
            close_scheduler=sched,
            quote_provider=lambda: MlegQuote(mid=4.60, bid=4.12, ask=5.08),
        )
        w.run()
        submitted = [r.limit_price for r in self._submitted_limits(api)]
        assert len(submitted) == 2
        assert w.effective_limit_price == pytest.approx(submitted[1])

    def test_effective_limit_tracks_the_resting_rung(self):
        """The worker-side half of the benchmark fix: after each submit,
        `effective_limit_price` must be the price now on the book."""
        api = self._api()
        stream = self._stream([False, False, False])
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-2.01,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            entry_walk=self._walk(), quote_provider=lambda: self._quote(),
        )
        assert w.effective_limit_price == -2.01, "starts at the plan limit"
        w.run()
        submitted = [r.limit_price for r in self._submitted_limits(api)]
        assert w.effective_limit_price == pytest.approx(submitted[-1]), (
            f"effective limit {w.effective_limit_price} is not the last "
            f"rung submitted {submitted[-1]}"
        )
        assert w.effective_limit_price != -2.01, "fixture must concede"

    def test_a_fill_on_a_later_rung_reports_that_rungs_limit(self):
        api = self._api()
        # Rung 1: stream miss AND a non-terminal REST re-check, so it is
        # genuinely cancelled. Rung 2: stream fill, REST returns filled.
        # A single `resolves_filled=True` would terminate rung 1 on the
        # REST re-check and the walk would never reach rung 2 — the test
        # would then assert nothing about later rungs.
        api.get_order_by_id.side_effect = [
            _mleg_submitted("combo-1", status="accepted"),
            _mleg_filled("combo-2"),
            _mleg_filled("combo-2"),
        ]
        stream = self._stream([False, True])   # rung 1 misses, rung 2 fills
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=-3.42,
            strategy_name="credit_spread", api=api, stream_manager=stream,
            entry_walk=self._walk(
                [("mid", 60), ("mid - 0.34*(mid-bid)", 60), ("mid - 0.67*(mid-bid)", 60)]
            ),
            quote_provider=lambda: self._quote(mid=3.42, bid=3.05, ask=3.79),
        )
        w.run()
        submitted = [r.limit_price for r in self._submitted_limits(api)]
        assert len(submitted) == 2
        assert w.effective_limit_price == pytest.approx(submitted[1])
        assert w.effective_limit_price != pytest.approx(submitted[0])

    def test_a_market_fallback_step_clears_the_benchmark(self):
        """Review follow-up (PR #103): market steps have no limit, so the
        worker must not leave the previous rung's limit standing as the
        benchmark a market fill gets measured against."""
        from execution.mleg_close import MlegCloseScheduler, MlegQuote
        api = self._api()
        api.get_order_by_id.side_effect = [
            _mleg_submitted("combo-1", status="accepted"),
            _mleg_filled("combo-2"),
            _mleg_filled("combo-2"),
        ]
        sched = MlegCloseScheduler(
            [("mid", 30), ("market", 0)], reason="stop_loss", position_id="p1",
        )
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=4.60,
            strategy_name="credit_spread", api=api,
            stream_manager=self._stream([False, True]),
            close_scheduler=sched,
            quote_provider=lambda: MlegQuote(mid=4.60, bid=4.12, ask=5.08),
        )
        w.run()
        assert w.effective_limit_price is None, (
            f"market fill would be benchmarked against "
            f"{w.effective_limit_price}, a limit nobody submitted"
        )

    def test_the_limit_rung_before_a_market_step_did_set_a_benchmark(self):
        """Guards the test above from passing vacuously — the walk really
        did submit a limit first, and it really was recorded."""
        from execution.mleg_close import MlegCloseScheduler, MlegQuote
        api = self._api()
        sched = MlegCloseScheduler(
            [("mid", 30)], reason="stop_loss", position_id="p1",
        )
        w = SpreadExecutionWorker(
            legs=_open_legs(), qty=1, limit_price=4.60,
            strategy_name="credit_spread", api=api,
            stream_manager=self._stream([False]),
            close_scheduler=sched,
            quote_provider=lambda: MlegQuote(mid=4.60, bid=4.12, ask=5.08),
        )
        w.run()
        assert w.effective_limit_price == pytest.approx(4.60)
