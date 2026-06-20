"""
Background workers for async options limit-order execution.

  * ``OptionsExecutionWorker`` — single-leg limit entries (11.25).
  * ``SpreadExecutionWorker``  — multi-leg (MLEG) combo orders (11.28).

Both submit a DAY limit order, watch for a fill via the WebSocket stream
(falling back to REST polling), and cancel if the order is still unfilled
after a timeout. They report the terminal outcome through an ``on_fill``
callback so the broker/engine can drain results on its own cadence.

Combo fills are atomic — Alpaca fills or rejects an MLEG order as a unit —
so ``SpreadExecutionWorker`` is a sibling class rather than a mode flag on
the single-leg worker: the control flow genuinely differs. Shared
stream-watch / fill-report plumbing lives in ``_BaseExecutionWorker``.
"""

import threading
import time
import uuid
import warnings
from dataclasses import dataclass
from typing import Callable

from loguru import logger

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"websockets\.legacy is deprecated.*",
        category=DeprecationWarning,
    )
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import (
        LimitOrderRequest,
        MarketOrderRequest,
        OptionLegRequest,
    )
    from alpaca.trading.enums import (
        OrderClass as AlpacaOrderClass,
        OrderSide,
        OrderType as AlpacaOrderType,
        PositionIntent,
        TimeInForce,
    )

from risk.manager import RiskDecision, Side
from execution.stream import StreamManager
from execution.mleg_close import MlegCloseScheduler, MlegQuote
from config.settings import MLEG_ENTRY_WATCH_TIMEOUT_SECONDS

# Type alias: returns a fresh quote of the spread (net mid/bid/ask) each call.
# Required when a walk-and-market close is used so the worker can recompute
# each step's limit price against the latest market data.
QuoteProvider = Callable[[], "MlegQuote | None"]

# Callback signature: (status_str, filled_qty, avg_fill_price, order_id)
FillCallback = Callable[[str, float, "float | None", str], None]
SubmittedCallback = Callable[[str, str], None]
EntryAllowedCallback = Callable[[], bool]

# How long an unfilled limit order is allowed to work before we cancel it.
_ENTRY_WATCH_TIMEOUT_SECONDS = MLEG_ENTRY_WATCH_TIMEOUT_SECONDS


# ── Multi-leg order construction (11.28) ────────────────────────────────────


@dataclass(frozen=True)
class SpreadLeg:
    """
    One leg of a multi-leg (MLEG) options order.

    Attributes:
        occ_symbol:  The OCC contract string for this leg.
        side:        BUY or SELL for this leg.
        opening:     True for an entry leg (``*_TO_OPEN`` intent), False for
                     an exit leg (``*_TO_CLOSE`` intent).
        ratio_qty:   Proportional quantity vs. the overall order qty. 1 for
                     each leg of a standard vertical spread.
    """

    occ_symbol: str
    side: Side
    opening: bool = True
    ratio_qty: int = 1

    def to_alpaca_leg(self) -> "OptionLegRequest":
        """Translate to the Alpaca SDK leg request with the right intent."""
        if self.side is Side.BUY:
            order_side = OrderSide.BUY
            intent = (
                PositionIntent.BUY_TO_OPEN
                if self.opening
                else PositionIntent.BUY_TO_CLOSE
            )
        else:
            order_side = OrderSide.SELL
            intent = (
                PositionIntent.SELL_TO_OPEN
                if self.opening
                else PositionIntent.SELL_TO_CLOSE
            )
        return OptionLegRequest(
            symbol=self.occ_symbol,
            ratio_qty=self.ratio_qty,
            side=order_side,
            position_intent=intent,
        )


def build_mleg_request(
    *,
    legs: list[SpreadLeg],
    qty: int,
    limit_price: float,
    client_order_id: str,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> "LimitOrderRequest":
    """
    Build an Alpaca MLEG (multi-leg) limit order request.

    ``qty`` is the number of spreads; each leg's ``ratio_qty`` scales it.

    ``limit_price`` is the net price of the combo. Alpaca's MLEG sign
    convention (verified against the paper API by
    ``scripts/verify_spread_order.py``):

      * **positive** → a net **debit** the order will pay up to;
      * **negative** → a net **credit** the order requires at least.

    So a bull put credit spread that should collect ~$1.40/share is
    submitted with ``limit_price = -1.40``. Submitting a positive number
    means "pay any debit up to that amount" and will fill near-instantly.

    Multi-leg orders carry no top-level ``symbol``/``side`` — those live on
    the individual ``OptionLegRequest`` legs.
    """
    if len(legs) < 2:
        raise ValueError(f"MLEG order needs ≥ 2 legs, got {len(legs)}")
    if qty < 1:
        raise ValueError(f"MLEG order qty must be ≥ 1, got {qty}")
    return LimitOrderRequest(
        qty=qty,
        limit_price=round(limit_price, 2),
        type=AlpacaOrderType.LIMIT,
        time_in_force=time_in_force,
        order_class=AlpacaOrderClass.MLEG,
        client_order_id=client_order_id,
        legs=[leg.to_alpaca_leg() for leg in legs],
    )


def build_mleg_market_request(
    *,
    legs: list[SpreadLeg],
    qty: int,
    client_order_id: str,
    time_in_force: TimeInForce = TimeInForce.DAY,
) -> "MarketOrderRequest":
    """
    Build an Alpaca MLEG (multi-leg) MARKET order request.

    Used as the autonomous fallback in walk-and-market close sequences:
    when the walk-limit steps haven't filled within their windows, the
    final step submits this market request to guarantee an exit without
    operator intervention.

    No ``limit_price`` — Alpaca fills the combo at the prevailing market.
    The total cost is bounded by the spread width (we can't lose more
    than the spread's max loss), so this is structurally safe even
    though the exact fill is unknown.

    Same MLEG constraints as the limit variant: ``time_in_force`` is
    ``day`` only; no top-level symbol/side.
    """
    if len(legs) < 2:
        raise ValueError(f"MLEG order needs ≥ 2 legs, got {len(legs)}")
    if qty < 1:
        raise ValueError(f"MLEG order qty must be ≥ 1, got {qty}")
    return MarketOrderRequest(
        qty=qty,
        type=AlpacaOrderType.MARKET,
        time_in_force=time_in_force,
        order_class=AlpacaOrderClass.MLEG,
        client_order_id=client_order_id,
        legs=[leg.to_alpaca_leg() for leg in legs],
    )



# ── Shared worker plumbing ──────────────────────────────────────────────────


class _BaseExecutionWorker(threading.Thread):
    """
    Shared stream-watch / fill-report plumbing for async options workers.

    Subclasses implement ``run()``; they call ``_watch_to_terminal()`` to
    run the wait-then-cancel loop and ``_report_fill()`` to surface the
    outcome through the ``on_fill`` callback.
    """

    def __init__(
        self,
        *,
        name: str,
        api: TradingClient,
        stream_manager: StreamManager | None,
        on_fill: FillCallback | None,
        on_submitted: "Callable[[str, str], None] | None" = None,
    ) -> None:
        super().__init__(daemon=True, name=name)
        self.api = api
        self.stream_manager = stream_manager
        self._on_fill = on_fill
        # PR #60 commit 9 fix C: durable broker identity. Fires once
        # immediately after a successful ``submit_order`` returns with
        # the broker-assigned id. Used by the broker to attach the id
        # to the per-order substrate row at the earliest possible
        # moment, so a worker crash between submit and first fill
        # cannot leave the row with order_id=NULL and unrecoverable
        # by exact id.
        #
        # Pre-submit rejection paths MUST NOT invoke this callback —
        # the substrate row stays at status='pending' with order_id
        # NULL until the on_fill callback signals 'rejected', at which
        # point apply_order_event handles the canceled transition via
        # client_order_id lookup.
        self._on_submitted = on_submitted

    def _report_submitted(self, client_order_id: str, order_id: str) -> None:
        """Fire the on_submitted callback if wired. Wrapped in
        try/except so a misbehaving callback can't crash the worker."""
        if self._on_submitted is None:
            return
        try:
            self._on_submitted(client_order_id, order_id)
        except Exception as exc:
            logger.error(
                f"[{self.name}] on_submitted callback raised: {exc}"
            )

    def _report_fill(self, status: str, order_id: str, order=None) -> None:
        """Invoke the on_fill callback with normalized fill details.

        Also writes the latest step's status to ``_last_walk_step_status`` /
        ``_last_walk_step_order`` when those attributes exist — used by the
        walk-and-market loop in ``SpreadExecutionWorker`` to read each
        step's outcome while the outer ``on_fill`` is suppressed.
        """
        # Capture for the walk-and-market loop (if it's running).
        if hasattr(self, "_last_walk_step_status"):
            self._last_walk_step_status = status
            self._last_walk_step_order = order
        if self._on_fill is None:
            return
        filled_qty = 0.0
        avg_price = None
        if order is not None:
            filled_qty = float(getattr(order, "filled_qty", 0) or 0)
            avg = getattr(order, "filled_avg_price", None)
            avg_price = float(avg) if avg is not None else None
        try:
            self._on_fill(status, filled_qty, avg_price, order_id)
        except Exception as e:
            logger.error(f"[{self.name}] on_fill callback raised: {e}")

    def _watch_to_terminal(
        self,
        *,
        order_id: str,
        stream_event: "threading.Event | None",
        timeout: float = _ENTRY_WATCH_TIMEOUT_SECONDS,
    ) -> None:
        """
        Wait for the order to fill via the stream (REST-poll fallback), then
        cancel if still unfilled at ``timeout``. Reports the terminal outcome
        via ``_report_fill``.

        ``stream_event`` is the Event returned by ``StreamManager.watch()``
        before submission (None when no stream is wired).
        """
        if stream_event is not None:
            filled = stream_event.wait(timeout=timeout)
            self.stream_manager.unwatch(order_id)
            if filled:
                logger.info(f"[{self.name}] order filled via stream.")
                try:
                    final = self.api.get_order_by_id(order_id)
                    self._report_fill("filled", order_id, final)
                except Exception:
                    self._report_fill("filled", order_id)
                return
            # Stream gap — re-check REST before declaring it unfilled.
            try:
                latest = self.api.get_order_by_id(order_id)
                status = (
                    latest.status.value
                    if hasattr(latest.status, "value")
                    else str(latest.status)
                )
                if status in ("filled", "partially_filled", "canceled", "rejected"):
                    logger.info(
                        f"[{self.name}] order resolved during stream gap: {status}"
                    )
                    self._report_fill(status, order_id, latest)
                    return
            except Exception:
                latest = None
            logger.warning(
                f"[{self.name}] order unfilled after {timeout:.0f}s. Cancelling."
            )
            try:
                self.api.cancel_order_by_id(order_id)
            except Exception as e:
                logger.error(f"[{self.name}] cancel failed: {e}")
            self._report_fill("canceled", order_id, latest)
            return

        # No stream — REST polling fallback.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(5)
            try:
                latest = self.api.get_order_by_id(order_id)
                status = (
                    latest.status.value
                    if hasattr(latest.status, "value")
                    else str(latest.status)
                )
                if status in ("filled", "partially_filled", "canceled", "rejected"):
                    logger.info(
                        f"[{self.name}] order reached terminal state: {status}"
                    )
                    self._report_fill(status, order_id, latest)
                    return
            except Exception:
                pass
        logger.warning(
            f"[{self.name}] order unfilled after {timeout:.0f}s via REST. Cancelling."
        )
        try:
            self.api.cancel_order_by_id(order_id)
        except Exception:
            pass
        self._report_fill("canceled", order_id)


# ── Single-leg worker (11.25) ───────────────────────────────────────────────


class OptionsExecutionWorker(_BaseExecutionWorker):
    """Async single-leg options limit entry."""

    def __init__(
        self,
        decision: RiskDecision,
        api: TradingClient,
        stream_manager: StreamManager | None = None,
        on_fill: FillCallback | None = None,
        client_order_id: str | None = None,
        entry_allowed: EntryAllowedCallback | None = None,
        on_submitted: SubmittedCallback | None = None,
    ) -> None:
        super().__init__(
            name=f"OptionsExecutor-{decision.symbol}",
            api=api,
            stream_manager=stream_manager,
            on_fill=on_fill,
            on_submitted=on_submitted,
        )
        self.decision = decision
        self.client_order_id = client_order_id
        self._entry_allowed = entry_allowed

    def run(self) -> None:
        logger.info(
            f"[{self.name}] Started background execution for {self.decision.symbol}"
        )
        limit_price = self.decision.limit_price
        if limit_price is None:
            logger.error(f"[{self.name}] Options execution requires a limit price.")
            return

        client_order_id = (
            self.client_order_id
            or f"opt-{self.decision.strategy_name}-{uuid.uuid4().hex[:8]}"
        )
        req = LimitOrderRequest(
            symbol=self.decision.symbol,
            qty=self.decision.qty,
            side=OrderSide.BUY if self.decision.side is Side.BUY else OrderSide.SELL,
            type=AlpacaOrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            limit_price=round(limit_price, 2),
        )

        stream_event = None
        if self.stream_manager is not None:
            stream_event = self.stream_manager.watch(client_order_id)

        if self._entry_allowed is not None and not self._entry_allowed():
            logger.warning(
                f"[{self.name}] Entry canceled before submit: global risk halt active"
            )
            if self.stream_manager is not None:
                self.stream_manager.unwatch(client_order_id)
            self._report_fill("rejected", client_order_id)
            return

        try:
            order = self.api.submit_order(req)
            logger.info(f"[{self.name}] Submitted option limit order {order.id}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to submit option limit order: {e}")
            if self.stream_manager is not None:
                self.stream_manager.unwatch(client_order_id)
            self._report_fill("rejected", client_order_id)
            return

        if self.stream_manager is not None:
            self.stream_manager.bind_submitted_order(
                client_order_id=client_order_id,
                order_id=str(order.id),
                stop_leg_ids=[],
            )

        # PR #60 commit 9 fix C: attach the broker order_id to the
        # substrate row at the earliest possible moment so any later
        # crash / restart / reconciliation can resolve this order by
        # its broker id rather than only by client_order_id. If the
        # worker dies before _watch_to_terminal yields a fill, the
        # substrate row is still recoverable.
        self._report_submitted(client_order_id, str(order.id))

        self._watch_to_terminal(
            order_id=str(order.id),
            stream_event=stream_event,
        )


# ── Multi-leg combo worker (11.28) ──────────────────────────────────────────


class SpreadExecutionWorker(_BaseExecutionWorker):
    """
    Async multi-leg (MLEG) combo order execution.

    Submits an atomic two-leg (or more) limit order, watches for the combo
    fill via the stream, and cancels if unfilled after the timeout. The fill
    is atomic — Alpaca fills or rejects the whole combo — so there is no
    partial-leg orphan state to reconcile.

    No strategy wires this in yet (PR 2 is infrastructure only); PR 3's
    credit-spread strategy is the first consumer.
    """

    def __init__(
        self,
        *,
        legs: list[SpreadLeg],
        qty: int,
        limit_price: float,
        strategy_name: str,
        api: TradingClient,
        stream_manager: StreamManager | None = None,
        on_fill: FillCallback | None = None,
        entry_allowed: EntryAllowedCallback | None = None,
        # Walk-and-market close mode (optional, opt-in per-call). When both
        # are supplied, the worker ignores ``limit_price`` and runs the
        # scheduler-driven walk instead of the single-shot submit. The
        # scheduler is strategy-agnostic — any MLEG strategy can drive it.
        close_scheduler: MlegCloseScheduler | None = None,
        quote_provider: QuoteProvider | None = None,
        # Optional per-step telemetry sink. Called once for every step
        # the walk-and-market loop visits (including the market step).
        # Signature: ``(step_number, total_steps, price_expr, is_market,
        # limit_price, duration_seconds, terminal_status)`` where
        # ``terminal_status`` is "filled", "canceled", or "skipped".
        on_walk_step: Callable[..., None] | None = None,
        # §10.7 fix-up — per-submit attach callback. Fires synchronously
        # after every successful broker submit (single-shot AND each
        # walk step) with (engine_substrate_cloid, broker_order_id).
        # The engine uses this to attach the broker order_id to the
        # substrate row at the earliest possible moment so a worker
        # crash between submit and drain doesn't leave the substrate
        # at order_id=NULL (the original blocking defect found by
        # review on the spread close path).
        on_submitted: Callable[[str, str], None] | None = None,
        # §10.7 fix-up — caller-provided substrate cloid that the
        # worker emits via on_submitted. Distinct from the broker
        # cloids the worker generates per submit (Alpaca requires
        # unique cloids per order, walk has multiple submits). The
        # engine's substrate row carries this value as its
        # client_order_id; on_submitted echoes it so the engine can
        # update the substrate row's order_id on every step.
        substrate_cloid: str | None = None,
    ) -> None:
        # The short leg is the defining symbol for logging/identification.
        short_leg = next(
            (leg for leg in legs if leg.side is Side.SELL), legs[0]
        )
        super().__init__(
            name=f"SpreadExecutor-{short_leg.occ_symbol}",
            api=api,
            stream_manager=stream_manager,
            on_fill=on_fill,
        )
        self.legs = legs
        self.qty = qty
        self.limit_price = limit_price
        self.strategy_name = strategy_name
        self._entry_allowed = entry_allowed
        # Walk-and-market mode is opt-in: setting both turns it on.
        self._close_scheduler = close_scheduler
        self._quote_provider = quote_provider
        self._on_walk_step = on_walk_step
        self._on_submitted = on_submitted
        self._substrate_cloid = substrate_cloid
        if (close_scheduler is None) != (quote_provider is None):
            raise ValueError(
                "SpreadExecutionWorker: close_scheduler and quote_provider "
                "must both be set or both be None (walk-and-market needs "
                "fresh quotes for each step)"
            )

    def _fire_on_submitted(self, broker_order_id: str) -> None:
        """Emit the per-submit attach event if the engine wired it.

        Tagged with ``self._substrate_cloid`` (the engine's substrate
        row client_order_id), NOT the worker-generated broker cloid —
        the substrate row was inserted before dispatch with the
        engine's cloid as its key.
        """
        if self._on_submitted is None or self._substrate_cloid is None:
            return
        try:
            self._on_submitted(self._substrate_cloid, broker_order_id)
        except Exception as exc:
            logger.error(
                f"[{self.name}] on_submitted callback raised: {exc}"
            )

    @property
    def walk_and_market_mode(self) -> bool:
        """True when the worker will drive the walk-and-market scheduler."""
        return self._close_scheduler is not None

    def run(self) -> None:
        if self.walk_and_market_mode:
            self._run_walk_and_market()
            return
        logger.info(
            f"[{self.name}] Started combo execution: {self.qty}× "
            f"[{', '.join(leg.occ_symbol for leg in self.legs)}] "
            f"@ net {self.limit_price:.2f}"
        )
        client_order_id = f"spr-{self.strategy_name}-{uuid.uuid4().hex[:8]}"
        try:
            req = build_mleg_request(
                legs=self.legs,
                qty=self.qty,
                limit_price=self.limit_price,
                client_order_id=client_order_id,
            )
        except ValueError as e:
            logger.error(f"[{self.name}] Invalid MLEG request: {e}")
            self._report_fill("rejected", client_order_id)
            return

        stream_event = None
        if self.stream_manager is not None:
            stream_event = self.stream_manager.watch(client_order_id)

        if self._entry_allowed is not None and not self._entry_allowed():
            logger.warning(
                f"[{self.name}] Entry canceled before submit: global risk halt active"
            )
            if self.stream_manager is not None:
                self.stream_manager.unwatch(client_order_id)
            self._report_fill("rejected", client_order_id)
            return

        try:
            order = self.api.submit_order(req)
            logger.info(f"[{self.name}] Submitted MLEG combo order {order.id}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to submit MLEG combo order: {e}")
            if self.stream_manager is not None:
                self.stream_manager.unwatch(client_order_id)
            self._report_fill("rejected", client_order_id)
            return

        # §10.7 fix-up — eager attach the broker order_id to the
        # substrate row (single-shot path).
        self._fire_on_submitted(str(order.id))

        if self.stream_manager is not None:
            self.stream_manager.bind_submitted_order(
                client_order_id=client_order_id,
                order_id=str(order.id),
                stop_leg_ids=[],
            )

        self._watch_to_terminal(
            order_id=str(order.id),
            stream_event=stream_event,
        )

    # ── Walk-and-market close path ─────────────────────────────────────────
    #
    # Walk the limit from a patient starting price through several escalating
    # steps, then submit market as the autonomous fallback if nothing fills.
    # Strategy-agnostic: any MLEG strategy that constructs a scheduler +
    # quote_provider gets this behaviour for free.

    def _submit_walk_step(self, *, step) -> tuple[str, "object | None"]:
        """Submit one walk step (limit or market). Returns (status, latest_order).

        ``status`` is one of "filled", "canceled", "rejected", "skipped".
        ``latest_order`` is the most recent Alpaca order object for
        telemetry, or None if the submit itself failed.
        """
        client_order_id = (
            f"spr-{self.strategy_name}-walk{step.step_number:02d}-"
            f"{uuid.uuid4().hex[:6]}"
        )
        try:
            if step.is_market:
                req = build_mleg_market_request(
                    legs=self.legs,
                    qty=self.qty,
                    client_order_id=client_order_id,
                )
            else:
                req = build_mleg_request(
                    legs=self.legs,
                    qty=self.qty,
                    limit_price=step.limit_price,
                    client_order_id=client_order_id,
                )
        except ValueError as exc:
            logger.error(
                f"[{self.name}] walk step {step.step_number}/{step.total_steps} "
                f"invalid MLEG request: {exc}"
            )
            return "rejected", None

        stream_event = None
        if self.stream_manager is not None:
            stream_event = self.stream_manager.watch(client_order_id)

        try:
            order = self.api.submit_order(req)
        except Exception as exc:
            logger.error(
                f"[{self.name}] walk step {step.step_number}/{step.total_steps} "
                f"submit failed: {exc}"
            )
            if self.stream_manager is not None:
                self.stream_manager.unwatch(client_order_id)
            return "rejected", None

        if step.is_market:
            logger.info(
                f"[{self.name}] walk step {step.step_number}/{step.total_steps} "
                f"submitted MARKET order {order.id} — autonomous fallback"
            )
        else:
            logger.info(
                f"[{self.name}] walk step {step.step_number}/{step.total_steps} "
                f"submitted LIMIT @ ${step.limit_price:.2f} "
                f"(expr={step.price_expr!r}, hold={step.duration_seconds}s) "
                f"order={order.id}"
            )

        # §10.7 fix-up — eager attach the broker order_id to the
        # substrate row. At any moment during walk-and-market, exactly
        # one broker order is in flight; this keeps substrate.order_id
        # current so the cycle / startup reconciler can resolve it
        # via REST after a crash.
        self._fire_on_submitted(str(order.id))

        if self.stream_manager is not None:
            self.stream_manager.bind_submitted_order(
                client_order_id=client_order_id,
                order_id=str(order.id),
                stop_leg_ids=[],
            )

        # Market orders fill (or reject) at the venue — give them a generous
        # 60s window but don't cancel. Limit orders use the step's own
        # duration as the cancel-after window.
        timeout = 60.0 if step.is_market else float(step.duration_seconds)
        self._last_walk_step_status = "canceled"
        self._last_walk_step_order = None
        self._watch_to_terminal(
            order_id=str(order.id),
            stream_event=stream_event,
            timeout=timeout,
        )
        return self._last_walk_step_status, self._last_walk_step_order

    def _run_walk_and_market(self) -> None:
        """Drive the scheduler through its steps until filled or exhausted."""
        scheduler = self._close_scheduler
        assert scheduler is not None  # type narrowing; checked in run()
        quote_provider = self._quote_provider
        assert quote_provider is not None

        logger.info(
            f"[{self.name}] walk-and-market close started: "
            f"reason={scheduler.reason}, position={scheduler.position_id}, "
            f"qty={self.qty}× [{', '.join(leg.occ_symbol for leg in self.legs)}], "
            f"steps={scheduler.total_steps}"
        )

        # Defer on_fill until the walk terminates — intermediate cancels
        # are normal and shouldn't be reported as terminal outcomes. The
        # _walk_step_*  fields communicate inner-step outcomes back to us.
        outer_on_fill = self._on_fill
        self._on_fill = None

        terminal_status = "canceled"
        terminal_order = None
        try:
            while not scheduler.exhausted:
                # Only fetch a quote for limit steps. The market sentinel
                # doesn't need market data to build its request, and we
                # must NEVER skip the market step on a quote outage —
                # that would defeat the autonomous-fallback guarantee
                # (the strongest exit signal becoming the most fragile to
                # network conditions, which is exactly backwards).
                if scheduler.current_step_is_market:
                    quote = None
                else:
                    quote = quote_provider()
                    if quote is None:
                        logger.warning(
                            f"[{self.name}] walk step {scheduler.current_step_number}: "
                            f"quote_provider returned None — skipping this limit step"
                        )
                        if self._on_walk_step is not None:
                            try:
                                self._on_walk_step(
                                    step_number=scheduler.current_step_number,
                                    total_steps=scheduler.total_steps,
                                    price_expr="(no quote)",
                                    is_market=False,
                                    limit_price=float("nan"),
                                    duration_seconds=0,
                                    terminal_status="skipped",
                                )
                            except Exception as exc:
                                logger.error(
                                    f"[{self.name}] on_walk_step raised: {exc}"
                                )
                        scheduler.advance()
                        continue

                step = scheduler.next_step(quote)
                if step is None:
                    break  # exhaustion safety

                status, latest_order = self._submit_walk_step(step=step)

                if self._on_walk_step is not None:
                    try:
                        self._on_walk_step(
                            step_number=step.step_number,
                            total_steps=step.total_steps,
                            price_expr=step.price_expr,
                            is_market=step.is_market,
                            limit_price=step.limit_price,
                            duration_seconds=step.duration_seconds,
                            terminal_status=status,
                        )
                    except Exception as exc:
                        logger.error(
                            f"[{self.name}] on_walk_step raised: {exc}"
                        )

                if status == "filled":
                    terminal_status = "filled"
                    terminal_order = latest_order
                    break
                if status == "rejected":
                    terminal_status = "rejected"
                    break
                # canceled or skipped → advance and continue
                scheduler.advance()

            logger.info(
                f"[{self.name}] walk-and-market close finished: "
                f"reason={scheduler.reason}, terminal={terminal_status}, "
                f"steps_used={min(scheduler.current_step_number - 1, scheduler.total_steps)}/"
                f"{scheduler.total_steps}"
            )
        finally:
            # Restore the outer on_fill and report the terminal outcome.
            self._on_fill = outer_on_fill
            client_order_id = f"spr-{self.strategy_name}-walk-terminal"
            self._report_fill(terminal_status, client_order_id, terminal_order)
