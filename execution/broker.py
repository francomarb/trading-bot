"""
Broker integration & order execution (Phase 7).

`AlpacaBroker` is the sole component in the system that talks to Alpaca for
order placement. Its `place_order` signature accepts only a `RiskDecision`,
so the Phase 6 risk gate cannot be bypassed — unsafe orders are structurally
impossible.

Contract guarantees
-------------------
1. **Risk gate enforcement.** `place_order(decision)` requires a `RiskDecision`
   instance; any other type raises `TypeError` immediately. There is no
   `place_raw_order` escape hatch on this class.

2. **Stop attached at submission.** Entries are submitted as Alpaca
   `order_class="oto"` (one-triggers-other) with a `stop_loss` leg. The stop
   is live as soon as the entry fills — there is no window where a position
   exists without a stop.

3. **Typed, terminal results.** `place_order` polls the order until it
   reaches a terminal state (filled / rejected / canceled) or the timeout
   elapses. The return is always an `OrderResult` with a defined
   `OrderStatus` — never a raw Alpaca object.

4. **Source of truth = the broker.** `sync_with_broker()` returns a snapshot
   of account / positions / open orders straight from Alpaca. Phase 8's
   engine calls this on every cycle and on startup before any decision.

5. **Rate-limit aware.** All Alpaca calls are wrapped in `_with_retry`
   (exponential backoff on 429 / 5xx / network), with a configurable max.

Hard-risk exits (engine-initiated stop-outs, kill-switch liquidations) go
through `close_position`, which always uses MARKET regardless of the
strategy's preferred order type.

SDK: alpaca-py (official, replaces deprecated alpaca-trade-api).
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from execution.stream import StreamManager

from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import (
    OrderClass as AlpacaOrderClass,
    OrderSide as AlpacaOrderSide,
    OrderStatus as AlpacaOrderStatus,
    OrderType as AlpacaOrderType,
    QueryOrderStatus,
    TimeInForce,
)
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
    StopLossRequest,
)
from loguru import logger

from data.fetcher import _install_timeout

import math

from config.settings import (
    ALPACA_API_KEY,
    ALPACA_PAPER,
    ALPACA_SECRET_KEY,
    DRY_RUN,
    FRACTIONAL_ENABLED,
)
from risk.manager import AccountState, Position, RiskDecision, Side
from strategies.base import OrderType


# ── Public types ─────────────────────────────────────────────────────────────


class BrokerError(Exception):
    """Raised when the broker itself cannot service a request (vs. the broker
    *rejecting* an order, which yields an `OrderResult(REJECTED, ...)`)."""


class OrderStatus(str, Enum):
    """Terminal + intermediate states for an `OrderResult`.

    Terminal: FILLED, PARTIAL, REJECTED, CANCELED, TIMEOUT.
    Non-terminal (only seen if the caller skips polling): ACCEPTED, PENDING.
    """

    ACCEPTED = "accepted"        # broker has the order, not yet working
    PENDING = "pending"          # working but not filled
    FILLED = "filled"            # fully filled — terminal
    PARTIAL = "partial"          # partially filled and no further activity — terminal
    REJECTED = "rejected"        # broker rejected — terminal
    CANCELED = "canceled"        # canceled before/during fill — terminal
    TIMEOUT = "timeout"          # poll deadline exceeded — terminal (caller decides next step)


# Alpaca status → our enum. Anything not listed maps to PENDING and we keep polling.
_ALPACA_TERMINAL: dict[str, OrderStatus] = {
    "filled": OrderStatus.FILLED,
    "partially_filled": OrderStatus.PARTIAL,  # treated terminal at timeout only
    "canceled": OrderStatus.CANCELED,
    "expired": OrderStatus.CANCELED,
    "rejected": OrderStatus.REJECTED,
    "done_for_day": OrderStatus.CANCELED,
    "replaced": OrderStatus.CANCELED,
    "stopped": OrderStatus.FILLED,
}


@dataclass(frozen=True)
class OrderResult:
    """Outcome of `place_order`. Status is always defined."""

    status: OrderStatus
    order_id: str | None
    symbol: str
    requested_qty: int
    filled_qty: int
    avg_fill_price: float | None
    raw_status: str | None     # Alpaca's status string (for logging/debug)
    message: str = ""          # human-readable summary or error text

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatus.FILLED,
            OrderStatus.PARTIAL,
            OrderStatus.REJECTED,
            OrderStatus.CANCELED,
            OrderStatus.TIMEOUT,
        }


@dataclass(frozen=True)
class OpenOrder:
    """Lightweight projection of an Alpaca open order."""

    order_id: str
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    status: str          # raw alpaca status (open / accepted / pending_new / ...)
    submitted_at: datetime
    limit_price: float | None
    stop_price: float | None


@dataclass(frozen=True)
class BrokerSnapshot:
    """Snapshot returned by `sync_with_broker` — used by the Phase 8 engine."""

    account: AccountState
    open_orders: list[OpenOrder] = field(default_factory=list)
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


# ── Broker ───────────────────────────────────────────────────────────────────


class AlpacaBroker:
    """
    Thin wrapper around `alpaca-py` TradingClient that enforces the risk gate
    and normalises responses into typed dataclasses.

    The instance is cheap; create one per session. Tests inject a mock client
    via the `client` constructor argument.
    """

    def __init__(
        self,
        *,
        client: TradingClient | None = None,
        max_attempts: int = 5,
        base_delay: float = 1.0,
        time_in_force: str = "gtc",
        stream_manager: "StreamManager | None" = None,
        dry_run: bool | None = None,
    ) -> None:
        self._api = client or TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            paper=ALPACA_PAPER,
        )
        if client is None:
            _install_timeout(self._api._session)
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._time_in_force = time_in_force
        self._stream_manager = stream_manager
        # Dry-run: log orders but never submit. Defaults to settings.DRY_RUN.
        self._dry_run: bool = dry_run if dry_run is not None else DRY_RUN

    # ── Retry wrapper ────────────────────────────────────────────────────

    def _with_retry(self, fn, *, op_desc: str = "broker call"):
        """
        Call `fn()` with exponential backoff on rate-limit (HTTP 429),
        transient 5xx, and network errors. 4xx errors other than 429 raise
        immediately — they're our bug, not a transient blip.
        """
        delay = self._base_delay
        last_exc: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return fn()
            except APIError as e:
                status = e.status_code
                last_exc = e
                if status == 429 or (status is not None and 500 <= status < 600):
                    logger.warning(
                        f"{op_desc} attempt {attempt}/{self._max_attempts} "
                        f"failed (status={status}): {e}. Sleeping {delay:.1f}s."
                    )
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise
            except (ConnectionError, TimeoutError) as e:
                last_exc = e
                logger.warning(
                    f"{op_desc} attempt {attempt}/{self._max_attempts} network "
                    f"error: {e}. Sleeping {delay:.1f}s."
                )
                time.sleep(delay)
                delay *= 2
        assert last_exc is not None
        raise last_exc

    # ── Read-side: account, positions, orders ────────────────────────────

    def get_account(self, *, session_start_equity: float | None = None) -> AccountState:
        """
        Return the current account as Phase 6's `AccountState`. The optional
        `session_start_equity` is passed through to populate the daily-loss
        baseline; if omitted, it defaults to current equity (sensible only on
        the very first call of a session).
        """
        acct = self._with_retry(self._api.get_account, op_desc="get_account")
        equity = float(acct.equity)
        return AccountState(
            equity=equity,
            cash=float(acct.cash),
            session_start_equity=session_start_equity if session_start_equity is not None else equity,
            open_positions=self.get_positions(),
        )

    def get_positions(self) -> dict[str, Position]:
        """Return all open positions keyed by symbol."""
        raw = self._with_retry(self._api.get_all_positions, op_desc="get_all_positions")
        out: dict[str, Position] = {}
        for p in raw:
            out[p.symbol] = Position(
                symbol=p.symbol,
                qty=int(float(p.qty)),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
            )
        return out

    def get_open_orders(self) -> list[OpenOrder]:
        """All currently-open orders, projected into `OpenOrder`."""
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        raw = self._with_retry(
            lambda: self._api.get_orders(request),
            op_desc="get_orders(open)",
        )
        return [self._to_open_order(o) for o in raw]

    def sync_with_broker(
        self, *, session_start_equity: float | None = None
    ) -> BrokerSnapshot:
        """
        Authoritative snapshot of broker state. Phase 8's engine calls this
        at the top of every cycle and on startup before any decision.
        """
        return BrokerSnapshot(
            account=self.get_account(session_start_equity=session_start_equity),
            open_orders=self.get_open_orders(),
        )

    def get_closed_orders(
        self,
        *,
        after: datetime | None = None,
        until: datetime | None = None,
        symbols: list[str] | None = None,
        limit: int = 500,
    ) -> list[OrderResult]:
        """
        Retrieve filled / closed orders from Alpaca for reconciliation.

        Returns `OrderResult` objects so the caller gets the same typed
        interface as `place_order`. Filters to status=closed (terminal),
        optionally scoped by date range and symbols.
        """
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=limit,
            after=after,
            until=until,
        )

        raw = self._with_retry(
            lambda: self._api.get_orders(request),
            op_desc="get_orders(closed)",
        )

        results: list[OrderResult] = []
        for o in raw:
            sym = o.symbol
            if symbols and sym not in symbols:
                continue
            status_str = o.status.value if isinstance(o.status, AlpacaOrderStatus) else str(o.status)
            mapped = _ALPACA_TERMINAL.get(status_str, OrderStatus.CANCELED)
            filled = int(float(o.filled_qty or 0))
            avg = o.filled_avg_price
            avg_price = float(avg) if avg is not None else None
            side_str = o.side.value if isinstance(o.side, AlpacaOrderSide) else str(o.side)
            results.append(OrderResult(
                status=mapped,
                order_id=str(o.id),
                symbol=sym,
                requested_qty=int(float(o.qty)),
                filled_qty=filled,
                avg_fill_price=avg_price,
                raw_status=status_str,
                message=f"historical: {side_str} {o.qty} {sym} @ {avg_price}",
            ))
        return results

    # ── Write-side: place / cancel / close ───────────────────────────────

    def place_order(
        self,
        decision: RiskDecision,
        *,
        poll_timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> OrderResult:
        """
        Submit `decision` to Alpaca.

        Whole-share path (FRACTIONAL_ENABLED=False, or qty is a whole number):
          OTO GTC — entry + stop submitted atomically. Exact original behaviour.

        Fractional path (FRACTIONAL_ENABLED=True and qty has a decimal part):
          DAY market entry (Alpaca fractional requires DAY TIF, no OTO).
          After fill: standalone GTC stop for floor(qty) whole shares.
          The fractional remainder exits via engine exit signals.

        Refuses any non-`RiskDecision` input. There is no other way to call
        this — that is the Phase 6 / 7 contract.
        """
        if not isinstance(decision, RiskDecision):
            raise TypeError(
                f"place_order requires a RiskDecision (got {type(decision).__name__}). "
                "Strategy signals must go through RiskManager.evaluate first."
            )

        # Route fractional quantities to the DAY-entry + GTC-stop path.
        # When FRACTIONAL_ENABLED=False, _size_position always returns a whole
        # number so math.floor(qty) == qty is always True — this branch is
        # never entered and the OTO path below is byte-for-byte unchanged.
        if math.floor(decision.qty) != decision.qty:
            return self._place_fractional_order(
                decision, poll_timeout=poll_timeout, poll_interval=poll_interval
            )

        # Build request object.
        client_order_id = f"{decision.strategy_name}-{uuid.uuid4().hex[:10]}"
        stop_loss = StopLossRequest(stop_price=round(decision.stop_price, 2))

        tif = TimeInForce.DAY if self._time_in_force == "day" else TimeInForce.GTC

        if decision.order_type is OrderType.LIMIT:
            if decision.limit_price is None:
                raise ValueError("LIMIT decision missing limit_price")
            order_request = LimitOrderRequest(
                symbol=decision.symbol,
                qty=decision.qty,
                side=AlpacaOrderSide.BUY if decision.side is Side.BUY else AlpacaOrderSide.SELL,
                type=AlpacaOrderType.LIMIT,
                time_in_force=tif,
                order_class=AlpacaOrderClass.OTO,
                stop_loss=stop_loss,
                client_order_id=client_order_id,
                limit_price=round(decision.limit_price, 2),
            )
        else:
            order_request = MarketOrderRequest(
                symbol=decision.symbol,
                qty=decision.qty,
                side=AlpacaOrderSide.BUY if decision.side is Side.BUY else AlpacaOrderSide.SELL,
                type=AlpacaOrderType.MARKET,
                time_in_force=tif,
                order_class=AlpacaOrderClass.OTO,
                stop_loss=stop_loss,
                client_order_id=client_order_id,
            )

        logger.info(
            f"placing {decision.order_type.value} {decision.side.value} "
            f"{decision.qty} {decision.symbol} (stop ${decision.stop_price:.2f}, "
            f"client_id={client_order_id})"
        )

        if self._dry_run:
            logger.warning(
                f"DRY RUN — order NOT submitted: {decision.order_type.value} "
                f"{decision.side.value} {decision.qty} {decision.symbol}"
            )
            return OrderResult(
                status=OrderStatus.FILLED,
                order_id=f"dry-run-{uuid.uuid4().hex[:10]}",
                symbol=decision.symbol,
                requested_qty=decision.qty,
                filled_qty=decision.qty,
                avg_fill_price=decision.entry_reference_price,
                raw_status="dry_run",
                message="dry run — no order submitted",
            )

        # Register with the stream before submitting to avoid a fill-before-watch race.
        stream_event: threading.Event | None = None
        if self._stream_manager is not None:
            stream_event = self._stream_manager.watch(client_order_id)

        try:
            order = self._with_retry(
                lambda: self._api.submit_order(order_request),
                op_desc=f"submit_order({decision.symbol})",
            )
        except APIError as e:
            logger.error(f"broker rejected {decision.symbol}: {e}")
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=None,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                filled_qty=0,
                avg_fill_price=None,
                raw_status=None,
                message=str(e),
            )

        order_id = str(order.id)

        # Re-register using the real Alpaca order ID (stream matches on order.id).
        if self._stream_manager is not None:
            stream_event = self._stream_manager.watch(order_id)
            # Register stop-leg ID so stop-outs are captured by drain_stop_fills().
            for leg in getattr(order, "legs", None) or []:
                self._stream_manager.register_stop_leg(str(leg.id))

        return self._wait_for_fill(
            order_id=order_id,
            symbol=decision.symbol,
            requested_qty=decision.qty,
            timeout=poll_timeout,
            interval=poll_interval,
            stream_event=stream_event,
        )

    def _place_fractional_order(
        self,
        decision: RiskDecision,
        *,
        poll_timeout: float,
        poll_interval: float,
    ) -> OrderResult:
        """
        Fractional market entry path — only reached when FRACTIONAL_ENABLED=True
        and decision.qty has a decimal part.

        Alpaca fractional shares require DAY TIF and cannot use OTO order class,
        so the entry and stop are submitted as two separate orders:
          1. DAY MarketOrderRequest (fractional qty, no stop leg).
          2. After confirmed fill: GTC StopOrderRequest for floor(qty) whole shares.

        If floor(qty) == 0 (qty < 1 share), no stop can be submitted — the
        position exits via engine exit signals only (logged as WARNING).

        If stop submission fails after a successful fill, an ERROR is logged.
        The position is unprotected until the engine's next exit signal; close
        manually if that occurs.

        When FRACTIONAL_ENABLED=False this method is never called — place_order()
        routes directly to the OTO GTC path, which is byte-for-byte unchanged.
        """
        client_order_id = f"{decision.strategy_name}-frac-{uuid.uuid4().hex[:10]}"

        logger.info(
            f"placing fractional market buy {decision.qty} {decision.symbol} "
            f"[DAY, stop ${decision.stop_price:.2f}, "
            f"client_id={client_order_id}]"
        )

        if self._dry_run:
            logger.warning(
                f"DRY RUN — fractional order NOT submitted: "
                f"market buy {decision.qty} {decision.symbol}"
            )
            return OrderResult(
                status=OrderStatus.FILLED,
                order_id=f"dry-run-{uuid.uuid4().hex[:10]}",
                symbol=decision.symbol,
                requested_qty=decision.qty,
                filled_qty=decision.qty,
                avg_fill_price=decision.entry_reference_price,
                raw_status="dry_run",
                message="dry run — no order submitted",
            )

        order_request = MarketOrderRequest(
            symbol=decision.symbol,
            qty=decision.qty,
            side=AlpacaOrderSide.BUY if decision.side is Side.BUY else AlpacaOrderSide.SELL,
            type=AlpacaOrderType.MARKET,
            time_in_force=TimeInForce.DAY,   # required for fractional
            client_order_id=client_order_id,
            # No order_class / stop_loss — stop submitted separately after fill.
        )

        # Register with stream before submitting to avoid fill-before-watch race.
        stream_event: threading.Event | None = None
        if self._stream_manager is not None:
            stream_event = self._stream_manager.watch(client_order_id)

        try:
            order = self._with_retry(
                lambda: self._api.submit_order(order_request),
                op_desc=f"submit_frac_order({decision.symbol})",
            )
        except APIError as e:
            logger.error(f"broker rejected fractional {decision.symbol}: {e}")
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=None,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                filled_qty=0,
                avg_fill_price=None,
                raw_status=None,
                message=str(e),
            )

        order_id = str(order.id)
        if self._stream_manager is not None:
            stream_event = self._stream_manager.watch(order_id)

        result = self._wait_for_fill(
            order_id=order_id,
            symbol=decision.symbol,
            requested_qty=decision.qty,
            timeout=poll_timeout,
            interval=poll_interval,
            stream_event=stream_event,
        )

        # Submit standalone GTC stop for the whole-share portion after fill.
        # Alpaca stop orders require whole shares; fractional remainder exits
        # via engine exit signals when the strategy fires.
        if result.status is OrderStatus.FILLED:
            stop_qty = math.floor(decision.qty)
            if stop_qty >= 1:
                stop_client_id = f"frac-stop-{uuid.uuid4().hex[:10]}"
                stop_request = StopOrderRequest(
                    symbol=decision.symbol,
                    qty=stop_qty,
                    side=AlpacaOrderSide.SELL,
                    time_in_force=TimeInForce.GTC,
                    stop_price=round(decision.stop_price, 2),
                    client_order_id=stop_client_id,
                )
                try:
                    self._with_retry(
                        lambda: self._api.submit_order(stop_request),
                        op_desc=f"submit_frac_stop({decision.symbol})",
                    )
                    logger.info(
                        f"[fractional] GTC stop: sell {stop_qty} "
                        f"{decision.symbol} @ ${decision.stop_price:.2f}"
                    )
                except APIError as e:
                    logger.error(
                        f"[fractional] stop submission failed for "
                        f"{decision.symbol}: {e} — position has no stop "
                        f"protection; close manually or wait for exit signal"
                    )
            else:
                logger.warning(
                    f"[fractional] {decision.symbol}: qty={decision.qty:.4f} — "
                    f"no whole-share stop possible (floor=0); "
                    f"position exits via engine signals only"
                )

        return result

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an order by id. Returns True on success, False on failure."""
        try:
            self._with_retry(
                lambda: self._api.cancel_order_by_id(order_id),
                op_desc=f"cancel_order({order_id})",
            )
            logger.info(f"canceled order {order_id}")
            return True
        except APIError as e:
            logger.warning(f"cancel_order({order_id}) failed: {e}")
            return False

    def close_position(
        self,
        symbol: str,
        *,
        poll_timeout: float = 30.0,
        poll_interval: float = 1.0,
    ) -> OrderResult:
        """
        Liquidate an open position with a market order. Used for hard-risk
        exits (engine stop-out, kill-switch liquidation) — always MARKET,
        ignoring any strategy preferred order type.

        Any open orders on the same symbol (typically the OTO stop_loss leg
        attached at entry) are canceled first — Alpaca otherwise reserves the
        shares against those siblings and rejects the close as
        "insufficient qty available". A hard exit must not fail because of an
        already-attached stop.
        """
        positions = self.get_positions()
        if symbol not in positions:
            raise BrokerError(f"no open position for {symbol}")
        qty = abs(positions[symbol].qty)

        # Cancel sibling orders so their shares are freed for the close.
        for o in self.get_open_orders():
            if o.symbol == symbol:
                self.cancel_order(o.order_id)

        try:
            order = self._with_retry(
                lambda: self._api.close_position(symbol),
                op_desc=f"close_position({symbol})",
            )
        except APIError as e:
            logger.error(f"close_position({symbol}) failed: {e}")
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=None,
                symbol=symbol,
                requested_qty=qty,
                filled_qty=0,
                avg_fill_price=None,
                raw_status=None,
                message=str(e),
            )
        return self._wait_for_fill(
            order_id=str(order.id),
            symbol=symbol,
            requested_qty=qty,
            timeout=poll_timeout,
            interval=poll_interval,
            stream_event=None,  # close_position doesn't pre-register with stream
        )

    def place_protective_stop(
        self,
        *,
        symbol: str,
        qty: int,
        stop_price: float,
        client_order_id_prefix: str = "repair-stop",
    ) -> OpenOrder:
        """
        Submit a standalone protective SELL stop as a simple GTC order.

        Used by engine reconciliation when a managed long position exists
        without any broker-side protective stop.
        """
        client_order_id = f"{client_order_id_prefix}-{uuid.uuid4().hex[:10]}"
        order_request = StopOrderRequest(
            symbol=symbol,
            qty=qty,
            side=AlpacaOrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=round(stop_price, 2),
            client_order_id=client_order_id,
        )
        logger.warning(
            f"repairing protective stop for {symbol}: "
            f"sell {qty} stop @ ${stop_price:.2f} (client_id={client_order_id})"
        )
        order = self._with_retry(
            lambda: self._api.submit_order(order_request),
            op_desc=f"submit_repair_stop({symbol})",
        )
        return self._to_open_order(order)

    # ── Internals ────────────────────────────────────────────────────────

    def _wait_for_fill(
        self,
        *,
        order_id: str,
        symbol: str,
        requested_qty: int,
        timeout: float,
        interval: float,
        stream_event: threading.Event | None,
    ) -> OrderResult:
        """
        Stream-first fill detection with REST polling fallback.

        If a stream_event is provided, waits up to `timeout` seconds for the
        WebSocket to deliver a terminal update. On success, builds the result
        from the stream data (no REST call). On timeout or missing update,
        falls back to a single-pass REST poll.

        If no stream is wired, delegates directly to _poll_until_terminal.
        """
        if stream_event is not None:
            fired = stream_event.wait(timeout=timeout)
            if fired:
                update = (
                    self._stream_manager.get_update(order_id)
                    if self._stream_manager is not None
                    else None
                )
                if update is not None:
                    return self._build_result_from_stream(update, symbol, requested_qty)
            # Stream timed out or update was None — fall back to a short REST check.
            logger.debug(
                f"{symbol} order {order_id}: stream timeout, falling back to REST"
            )
            return self._poll_until_terminal(
                order_id=order_id,
                symbol=symbol,
                requested_qty=requested_qty,
                timeout=interval * 3,
                interval=interval,
            )

        return self._poll_until_terminal(
            order_id=order_id,
            symbol=symbol,
            requested_qty=requested_qty,
            timeout=timeout,
            interval=interval,
        )

    @staticmethod
    def _build_result_from_stream(
        update: "TradeUpdate",
        symbol: str,
        requested_qty: int,
    ) -> "OrderResult":
        """Build an OrderResult from a terminal TradeUpdate (WebSocket path)."""
        from alpaca.trading.models import TradeUpdate  # local import avoids circular

        event_val = (
            update.event.value
            if hasattr(update.event, "value")
            else str(update.event)
        )
        _STATUS_MAP = {
            "fill": OrderStatus.FILLED,
            "canceled": OrderStatus.CANCELED,
            "expired": OrderStatus.CANCELED,
            "replaced": OrderStatus.CANCELED,
            "rejected": OrderStatus.REJECTED,
        }
        status = _STATUS_MAP.get(event_val, OrderStatus.FILLED)
        filled_qty = int(float(update.qty or 0))
        avg_price = float(update.price) if update.price is not None else None
        order_id = str(update.order.id)
        msg = (
            f"{status.value} (stream): filled {filled_qty}/{requested_qty} "
            f"@ avg {avg_price if avg_price is not None else '—'}"
        )
        logger.info(f"{symbol} order {order_id}: {msg}")
        return OrderResult(
            status=status,
            order_id=order_id,
            symbol=symbol,
            requested_qty=requested_qty,
            filled_qty=filled_qty,
            avg_fill_price=avg_price,
            raw_status=event_val,
            message=msg,
        )

    def _poll_until_terminal(
        self,
        *,
        order_id: str,
        symbol: str,
        requested_qty: int,
        timeout: float,
        interval: float,
    ) -> OrderResult:
        """
        Poll Alpaca's order-status endpoint until the order reaches a terminal
        state or the deadline expires. On timeout, returns whatever is current
        (often `partially_filled` → PARTIAL, otherwise TIMEOUT).
        """
        deadline = time.monotonic() + timeout
        while True:
            order = self._with_retry(
                lambda: self._api.get_order_by_id(order_id),
                op_desc=f"get_order({order_id})",
            )
            raw = order.status.value if isinstance(order.status, AlpacaOrderStatus) else str(order.status)
            mapped = _ALPACA_TERMINAL.get(raw)
            if mapped is not None and mapped is not OrderStatus.PARTIAL:
                # Truly terminal (filled / rejected / canceled / etc).
                return self._build_result(order, symbol, requested_qty, mapped)
            if time.monotonic() >= deadline:
                # Out of time. If we've got partial fills, surface them.
                filled = int(float(order.filled_qty or 0))
                if filled > 0:
                    return self._build_result(
                        order, symbol, requested_qty, OrderStatus.PARTIAL
                    )
                return self._build_result(
                    order, symbol, requested_qty, OrderStatus.TIMEOUT
                )
            time.sleep(interval)

    @staticmethod
    def _build_result(
        order, symbol: str, requested_qty: int, status: OrderStatus
    ) -> OrderResult:
        filled = int(float(order.filled_qty or 0))
        avg = order.filled_avg_price
        avg_price = float(avg) if avg is not None else None
        order_id = str(order.id) if order.id is not None else None
        msg = (
            f"{status.value}: filled {filled}/{requested_qty} "
            f"@ avg {avg_price if avg_price is not None else '—'}"
        )
        logger.info(f"{symbol} order {order_id}: {msg}")
        return OrderResult(
            status=status,
            order_id=order_id,
            symbol=symbol,
            requested_qty=requested_qty,
            filled_qty=filled,
            avg_fill_price=avg_price,
            raw_status=order.status.value if hasattr(order.status, 'value') else str(order.status),
            message=msg,
        )

    @staticmethod
    def _to_open_order(o) -> OpenOrder:
        # Handle both alpaca-py model objects and SimpleNamespace mocks.
        submitted = getattr(o, "submitted_at", None)
        if isinstance(submitted, str):
            submitted = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
        elif submitted is None:
            submitted = datetime.now(timezone.utc)

        # Extract raw string values from enums or plain strings.
        side_val = o.side.value if hasattr(o.side, 'value') else str(o.side)
        type_val = o.type.value if hasattr(o.type, 'value') else str(o.type) if hasattr(o, 'type') and o.type else None
        status_val = o.status.value if hasattr(o.status, 'value') else str(o.status)
        order_id = str(o.id) if o.id is not None else None

        return OpenOrder(
            order_id=order_id,
            symbol=o.symbol,
            side=Side(side_val),
            qty=int(float(o.qty)),
            order_type=OrderType(type_val) if type_val in {ot.value for ot in OrderType} else OrderType.MARKET,
            status=status_val,
            submitted_at=submitted,
            limit_price=float(o.limit_price) if getattr(o, "limit_price", None) else None,
            stop_price=float(o.stop_price) if getattr(o, "stop_price", None) else None,
        )
