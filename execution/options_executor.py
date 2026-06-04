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
from config.settings import MLEG_ENTRY_WATCH_TIMEOUT_SECONDS

# Callback signature: (status_str, filled_qty, avg_fill_price, order_id)
FillCallback = Callable[[str, float, "float | None", str], None]

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
    ) -> None:
        super().__init__(daemon=True, name=name)
        self.api = api
        self.stream_manager = stream_manager
        self._on_fill = on_fill

    def _report_fill(self, status: str, order_id: str, order=None) -> None:
        """Invoke the on_fill callback with normalized fill details."""
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
    ) -> None:
        super().__init__(
            name=f"OptionsExecutor-{decision.symbol}",
            api=api,
            stream_manager=stream_manager,
            on_fill=on_fill,
        )
        self.decision = decision
        self.client_order_id = client_order_id

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

    def run(self) -> None:
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

        try:
            order = self.api.submit_order(req)
            logger.info(f"[{self.name}] Submitted MLEG combo order {order.id}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to submit MLEG combo order: {e}")
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

        self._watch_to_terminal(
            order_id=str(order.id),
            stream_event=stream_event,
        )
