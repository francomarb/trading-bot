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
import warnings
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from execution.stream import StreamManager
    from engine.lifecycle import PositionLifecycleStore

from execution.options_executor import (
    OptionsExecutionWorker,
    SpreadExecutionWorker,
    SpreadLeg,
    build_mleg_request,
)
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"websockets\.legacy is deprecated.*",
        category=DeprecationWarning,
    )
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
        ReplaceOrderRequest,
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
    ORDER_CONFIRM_TIMEOUT_SECONDS,
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
    UNKNOWN = "unknown"          # submitted, but terminal state could not be confirmed


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
    requested_qty: float
    filled_qty: float
    avg_fill_price: float | None
    raw_status: str | None     # Alpaca's status string (for logging/debug)
    message: str = ""          # human-readable summary or error text
    submitted_at: datetime | None = None
    filled_at: datetime | None = None
    # Operator Controls Phase A — immutable lifecycle ID generated at
    # order-build time by `engine.lifecycle.new_position_uid()`. Carried
    # back to the engine so trade-log rows can be tagged with it. Always
    # populated for new entries; None for legacy callers that haven't
    # passed a lifecycle store through (e.g. close paths in Phase A,
    # tests that construct OrderResult directly).
    position_uid: str | None = None

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatus.FILLED,
            OrderStatus.PARTIAL,
            OrderStatus.REJECTED,
            OrderStatus.CANCELED,
            OrderStatus.TIMEOUT,
            OrderStatus.UNKNOWN,
        }


@dataclass(frozen=True)
class OpenOrder:
    """Lightweight projection of an Alpaca open order."""

    order_id: str
    symbol: str
    side: Side
    qty: float
    order_type: OrderType
    status: str          # raw alpaca status (open / accepted / pending_new / ...)
    submitted_at: datetime
    limit_price: float | None
    stop_price: float | None
    client_order_id: str | None = None
    time_in_force: str | None = None


@dataclass(frozen=True)
class BrokerSnapshot:
    """Snapshot returned by `sync_with_broker` — used by the Phase 8 engine."""

    account: AccountState
    open_orders: list[OpenOrder] = field(default_factory=list)
    fetched_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass(frozen=True)
class ClosedOrderInfo:
    """Typed projection of a broker historical order used for reconciliation."""

    order_id: str
    client_order_id: str | None
    symbol: str
    side: Side
    order_type: str | None
    status: OrderStatus
    raw_status: str
    qty: float
    filled_qty: float
    avg_fill_price: float | None
    stop_price: float | None
    submitted_at: datetime | None
    filled_at: datetime | None


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
        lifecycle_store: "PositionLifecycleStore | None" = None,
        entry_allowed: Callable[[], bool] | None = None,
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
        # Operator Controls Phase A — optional store. When set, the
        # broker writes lifecycle rows for each new entry (best-effort,
        # wrapped in try/except so persistence failure never aborts an
        # order). When None (legacy callers, tests), all lifecycle
        # writes are silently skipped and behavior is byte-for-byte
        # unchanged from before Phase A.
        self._lifecycle_store = lifecycle_store
        self._entry_allowed = entry_allowed
        # Dry-run: log orders but never submit. Defaults to settings.DRY_RUN.
        self._dry_run: bool = dry_run if dry_run is not None else DRY_RUN
        # Async option fills reported by OptionsExecutionWorker threads.
        # Drained by TradingEngine each cycle via drain_option_fills().
        self._pending_option_fills: list[tuple] = []
        self._pending_option_lock = threading.Lock()
        # Async MLEG combo fills reported by SpreadExecutionWorker threads.
        # Drained by TradingEngine each cycle via drain_spread_fills().
        self._pending_spread_fills: list[tuple] = []
        self._pending_spread_lock = threading.Lock()
        if self._stream_manager is not None:
            self._stream_manager.set_order_lookup_callbacks(
                by_id=self._stream_lookup_order_by_id,
                by_client_id=self._stream_lookup_order_by_client_id,
            )

    def _entries_allowed(self) -> bool:
        """Return whether a new opening order may be submitted right now."""
        callback = getattr(self, "_entry_allowed", None)
        return callback is None or callback()

    def bind_entry_guard(self, callback: Callable[[], bool]) -> bool:
        """
        Install an entry-submit guard when none was supplied at construction.

        Returns True when the callback was installed. An explicitly configured
        guard is preserved so an engine cannot weaken a stricter caller policy.
        """
        if getattr(self, "_entry_allowed", None) is not None:
            return False
        self._entry_allowed = callback
        return True

    @staticmethod
    def _entry_halt_result(symbol: str, requested_qty: float) -> OrderResult:
        return OrderResult(
            status=OrderStatus.REJECTED,
            order_id=None,
            symbol=symbol,
            requested_qty=requested_qty,
            filled_qty=0,
            avg_fill_price=None,
            raw_status="risk_halted",
            message="entry blocked: global risk halt active",
        )

    # ── Lifecycle helpers (Operator Controls Phase A) ───────────────────
    #
    # Every call site is wrapped in try/except → logger.warning so a DB
    # I/O failure can NEVER abort an order. This is the same discipline
    # used by `strategies.health.lifecycle` for the signal-lifecycle
    # counter table. Phase A is purely additive — when
    # `self._lifecycle_store is None`, these helpers are no-ops and the
    # broker's behavior is byte-for-byte identical to pre-Phase-A.

    def _lifecycle_begin(
        self,
        *,
        decision: "RiskDecision",
        client_order_id: str,
        position_type: str = "single_leg",
        owner_key: str | None = None,
    ) -> str | None:
        """Generate a position_uid and write a `pending` lifecycle row
        before the broker submission. Returns the uid (or None if no
        store is configured / persistence failed)."""
        if self._lifecycle_store is None:
            return None
        try:
            from engine.lifecycle import new_position_uid
            from engine.positions import owner_key_for
            uid = new_position_uid()
            self._lifecycle_store.create_pending(
                position_uid=uid,
                symbol=decision.symbol,
                owner_key=owner_key or owner_key_for(decision.symbol),
                strategy=decision.strategy_name,
                position_type=position_type,
                entry_qty=float(decision.qty),
                entry_client_order_id=client_order_id,
            )
            return uid
        except Exception as exc:
            logger.warning(
                f"lifecycle.create_pending failed for {decision.symbol} "
                f"({decision.strategy_name}): {exc}"
            )
            return None

    def _lifecycle_mark_filled(
        self,
        *,
        position_uid: str | None,
        result: OrderResult,
    ) -> None:
        """Best-effort lifecycle transition after the broker reaches a
        terminal outcome for an entry order.

        Handles every terminal status that ``_wait_for_fill`` /
        ``_unknown_after_submit`` can return:

          - FILLED with qty>0       → mark_open
          - PARTIAL with qty>0      → mark_partially_filled
          - CANCELED/REJECTED, qty=0→ mark_canceled (zero-fill cancel
                                       after broker acceptance)
          - CANCELED/REJECTED, qty>0→ leave as partially_filled at the
                                       filled qty (proposal §8.1 — a
                                       partial-then-cancel must NOT
                                       become 'canceled')
          - TIMEOUT/UNKNOWN         → leave the lifecycle row alone so
                                       the next startup reconciliation
                                       observes broker truth

        Without this discipline the pending row leaks indefinitely
        whenever Alpaca terminally rejects after accepting submission.
        """
        if position_uid is None or self._lifecycle_store is None:
            return
        filled_qty = float(result.filled_qty or 0.0)
        avg = result.avg_fill_price
        try:
            if result.status is OrderStatus.FILLED and avg is not None and filled_qty > 0:
                self._lifecycle_store.mark_open(
                    position_uid=position_uid,
                    avg_entry_price=float(avg),
                    current_qty=filled_qty,
                )
            elif result.status is OrderStatus.PARTIAL and avg is not None and filled_qty > 0:
                self._lifecycle_store.mark_partially_filled(
                    position_uid=position_uid,
                    avg_entry_price=float(avg),
                    current_qty=filled_qty,
                )
            elif result.status in {OrderStatus.CANCELED, OrderStatus.REJECTED}:
                if filled_qty > 0 and avg is not None:
                    # Partial fill then broker-side cancel — proposal
                    # §8.1: must stay open/partially_filled. Update the
                    # row to reflect the realized partial.
                    self._lifecycle_store.mark_partially_filled(
                        position_uid=position_uid,
                        avg_entry_price=float(avg),
                        current_qty=filled_qty,
                    )
                else:
                    # Zero fills — clean cancel of the pending row.
                    self._lifecycle_store.mark_canceled(
                        position_uid=position_uid,
                    )
            # TIMEOUT / UNKNOWN — leave the row pending so a future
            # reconcile pass observes broker truth and acts on it.
        except Exception as exc:
            logger.warning(
                f"lifecycle.mark_filled failed for {position_uid}: {exc}"
            )

    def _lifecycle_mark_canceled(self, position_uid: str | None) -> None:
        """Best-effort cancel transition for zero-fill rejections.
        Safe to call even if the row had partial fills — the store's
        own §8.1 invariant will refuse the transition in that case
        and we log + move on."""
        if position_uid is None or self._lifecycle_store is None:
            return
        try:
            self._lifecycle_store.mark_canceled(position_uid=position_uid)
        except Exception as exc:
            logger.warning(
                f"lifecycle.mark_canceled skipped for {position_uid}: {exc}"
            )

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

    def get_latest_quote_midpoint(self, symbol: str) -> float | None:
        """Return the NBBO midpoint (arrival price) for `symbol` at submission time.

        Thin convenience wrapper around `data.fetcher.fetch_latest_quote_midpoint`
        so the engine's entry-flow code reads as `broker.get_latest_quote_midpoint(...)`.
        Returns None on quote-fetch failure or one-sided book — never raises.

        Used by the trading loop to capture the canonical pre-trade
        benchmark for execution-quality slippage measurement
        (arrival-price methodology per TCA practice). Equity entries
        call this immediately before submission so the eventual fill's
        slippage attribution reflects broker execution quality at the
        moment of submission, not the bar close at decision time.
        """
        from data.fetcher import fetch_latest_quote_midpoint
        return fetch_latest_quote_midpoint(symbol)

    def get_account(self, *, session_start_equity: float | None = None) -> AccountState:
        """
        Return the current account as Phase 6's `AccountState`. The optional
        `session_start_equity` is passed through to populate the daily-loss
        baseline; if omitted, it defaults to current equity (sensible only on
        the very first call of a session).
        """
        acct = self._with_retry(self._api.get_account, op_desc="get_account")
        equity = float(acct.equity)
        last_equity = float(acct.last_equity) if getattr(acct, "last_equity", None) is not None else None
        return AccountState(
            equity=equity,
            cash=float(acct.cash),
            session_start_equity=session_start_equity if session_start_equity is not None else equity,
            previous_close_equity=last_equity,
            open_positions=self.get_positions(),
        )

    def get_positions(self) -> dict[str, Position]:
        """Return all open positions keyed by symbol."""
        raw = self._with_retry(self._api.get_all_positions, op_desc="get_all_positions")
        out: dict[str, Position] = {}
        for p in raw:
            out[p.symbol] = Position(
                symbol=p.symbol,
                qty=float(p.qty),
                avg_entry_price=float(p.avg_entry_price),
                market_value=float(p.market_value),
                current_price=(
                    float(p.current_price)
                    if getattr(p, "current_price", None) is not None
                    else None
                ),
                cost_basis=(
                    float(p.cost_basis)
                    if getattr(p, "cost_basis", None) is not None
                    else None
                ),
                unrealized_pl=(
                    float(p.unrealized_pl)
                    if getattr(p, "unrealized_pl", None) is not None
                    else None
                ),
                unrealized_plpc=(
                    float(p.unrealized_plpc)
                    if getattr(p, "unrealized_plpc", None) is not None
                    else None
                ),
            )
        return out

    def get_open_orders(self) -> list[OpenOrder]:
        """All currently-open orders, projected into `OpenOrder`."""
        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        raw = self._with_retry(
            lambda: self._api.get_orders(request),
            op_desc="get_orders(open)",
        )
        out: list[OpenOrder] = []
        for order in raw:
            projected = self._to_open_order(order)
            if projected is not None:
                out.append(projected)
        return out

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
            info = self._to_closed_order_info(o)
            if info is None:
                continue
            if symbols and info.symbol not in symbols:
                continue
            results.append(OrderResult(
                status=info.status,
                order_id=info.order_id,
                symbol=info.symbol,
                requested_qty=info.qty,
                filled_qty=info.filled_qty,
                avg_fill_price=info.avg_fill_price,
                raw_status=info.raw_status,
                message=(
                    f"historical: {info.side.value} {info.qty} "
                    f"{info.symbol} @ {info.avg_fill_price}"
                ),
            ))
        return results

    def find_recent_filled_stop_order(
        self,
        *,
        symbol: str,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> ClosedOrderInfo | None:
        """
        Return the latest filled SELL stop order for ``symbol`` if one exists.

        Used as a recovery path when the websocket missed a protective-stop
        execution and the engine needs broker truth to reconstruct the exit.
        """
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=limit,
            after=after,
            until=until,
            symbols=[symbol],
        )
        raw = self._with_retry(
            lambda: self._api.get_orders(request),
            op_desc=f"get_orders(closed_stop:{symbol})",
        )

        candidates: list[ClosedOrderInfo] = []
        for order in raw:
            info = self._to_closed_order_info(order)
            if info is None:
                continue
            if info.symbol != symbol:
                continue
            if info.side is not Side.SELL:
                continue
            if info.order_type != "stop":
                continue
            if info.status is not OrderStatus.FILLED:
                continue
            candidates.append(info)

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: item.filled_at or item.submitted_at or datetime.min.replace(tzinfo=timezone.utc)
        )
        return candidates[-1]

    def find_recent_filled_entry_order(
        self,
        *,
        symbol: str,
        after: datetime | None = None,
        until: datetime | None = None,
        limit: int = 500,
    ) -> ClosedOrderInfo | None:
        """
        Return the latest filled BUY entry order for ``symbol`` if one exists.

        Used by recovery paths that reconstruct a missing entry record from an
        already-open broker position and want the original broker ``filled_at``
        instead of the later recovery time.
        """
        request = GetOrdersRequest(
            status=QueryOrderStatus.CLOSED,
            limit=limit,
            after=after,
            until=until,
            symbols=[symbol],
        )
        raw = self._with_retry(
            lambda: self._api.get_orders(request),
            op_desc=f"get_orders(closed_entry:{symbol})",
        )

        candidates: list[ClosedOrderInfo] = []
        for order in raw:
            info = self._to_closed_order_info(order)
            if info is None:
                continue
            if info.symbol != symbol:
                continue
            if info.side is not Side.BUY:
                continue
            if info.status is not OrderStatus.FILLED:
                continue
            candidates.append(info)

        if not candidates:
            return None

        candidates.sort(
            key=lambda item: item.filled_at
            or item.submitted_at
            or datetime.min.replace(tzinfo=timezone.utc)
        )
        return candidates[-1]

    # ── Write-side: place / cancel / close ───────────────────────────────

    def place_order(
        self,
        decision: RiskDecision,
        *,
        poll_timeout: float = ORDER_CONFIRM_TIMEOUT_SECONDS,
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
        if not self._entries_allowed():
            logger.warning(
                f"entry blocked before broker dispatch: {decision.symbol} "
                "(global risk halt active)"
            )
            return self._entry_halt_result(decision.symbol, decision.qty)

        # Options path must come first — OCC symbols require the background
        # worker regardless of qty, and must never fall into the fractional path.
        import re
        is_option = bool(re.match(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$", decision.symbol))
        if is_option and decision.order_type is OrderType.LIMIT:
            if self._dry_run:
                logger.warning(
                    f"DRY RUN — option order NOT dispatched: "
                    f"{decision.qty} {decision.symbol}"
                )
                return OrderResult(
                    status=OrderStatus.FILLED,
                    order_id=f"dry-run-{uuid.uuid4().hex[:10]}",
                    symbol=decision.symbol,
                    requested_qty=decision.qty,
                    filled_qty=decision.qty,
                    avg_fill_price=decision.entry_reference_price,
                    raw_status="dry_run",
                    message="dry run — no option order dispatched",
                )

            opt_worker_id = f"opt-worker-{uuid.uuid4().hex[:10]}"
            client_order_id = f"opt-{decision.strategy_name}-{uuid.uuid4().hex[:8]}"
            position_uid = self._lifecycle_begin(
                decision=decision,
                client_order_id=client_order_id,
            )

            dec = decision  # capture for closure

            def _on_fill(status: str, filled_qty: float, avg_price: "float | None", order_id: str) -> None:
                result = OrderResult(
                    status={
                        "filled": OrderStatus.FILLED,
                        "partially_filled": OrderStatus.PARTIAL,
                    }.get(status, OrderStatus.CANCELED),
                    order_id=order_id,
                    symbol=dec.symbol,
                    requested_qty=dec.qty,
                    filled_qty=filled_qty,
                    avg_fill_price=avg_price,
                    raw_status=status,
                    message=f"options async fill: {status}",
                )
                self._lifecycle_mark_filled(
                    position_uid=position_uid,
                    result=result,
                )
                with self._pending_option_lock:
                    self._pending_option_fills.append(
                        (dec, status, filled_qty, avg_price, order_id, position_uid)
                    )

            worker = OptionsExecutionWorker(
                decision=decision,
                api=self._api,
                stream_manager=self._stream_manager,
                on_fill=_on_fill,
                client_order_id=client_order_id,
                entry_allowed=self._entry_allowed,
            )
            worker.start()

            return OrderResult(
                status=OrderStatus.ACCEPTED,
                order_id=opt_worker_id,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                filled_qty=0.0,
                avg_fill_price=0.0,
                raw_status="accepted",
                message="dispatched to OptionsExecutionWorker",
            )

        # PLAN 11.32: when an entry price cap is in effect, force the
        # whole-share path so the capped DAY LIMIT + OTO branch applies.
        # Alpaca's fractional path is market-only and cannot enforce a
        # limit, so a fractional capped entry would otherwise silently
        # bypass the guard — the exact class of bug that triggered this
        # work (QCOM 2026-05-11 was fractional). Flooring loses at most
        # 1 share of sizing precision (always conservative — never
        # exceeds the original risk budget) and is acceptable for the
        # equity-trend strategies that use caps.
        if (
            decision.entry_max_price is not None
            and math.floor(decision.qty) != decision.qty
        ):
            floored = math.floor(decision.qty)
            if floored < 1:
                logger.warning(
                    f"[entry-guard] {decision.symbol}: capped entry rounds "
                    f"down to 0 whole shares (qty={decision.qty}, "
                    f"cap=${decision.entry_max_price:.2f}); rejecting — a "
                    f"sub-share fractional fill cannot be price-capped on Alpaca"
                )
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    order_id=None,
                    symbol=decision.symbol,
                    requested_qty=decision.qty,
                    filled_qty=0,
                    avg_fill_price=None,
                    raw_status=None,
                    message=(
                        f"capped entry rounds to 0 whole shares "
                        f"(qty={decision.qty}, cap=${decision.entry_max_price:.2f})"
                    ),
                )
            logger.info(
                f"[entry-guard] {decision.symbol}: flooring fractional qty "
                f"{decision.qty} -> {floored} so the entry_max_price cap "
                f"can be enforced via DAY LIMIT + OTO"
            )
            decision = replace(decision, qty=float(floored))

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
        # Operator Controls Phase A — position_uid is created later,
        # AFTER the dry-run guard. Creating it before would persist a
        # pending lifecycle row that no broker order ever backs (a
        # preflight dry run would leak rows that look like real
        # positions to the operator CLI).
        position_uid: str | None = None
        stop_loss = StopLossRequest(stop_price=round(decision.stop_price, 2))

        tif = TimeInForce.DAY if self._time_in_force == "day" else TimeInForce.GTC

        # PLAN 11.32: an entry price cap on a MARKET decision is submitted as a
        # marketable DAY LIMIT + OTO at the cap. DAY (never GTC) so an unfilled
        # capped entry expires at session close instead of ghost-filling later.
        if (
            decision.order_type is OrderType.MARKET
            and decision.entry_max_price is not None
        ):
            cap = round(decision.entry_max_price, 2)
            logger.info(
                f"[entry-guard] {decision.symbol}: market entry capped at "
                f"${cap:.2f} (ref ${decision.entry_reference_price:.2f}); "
                f"submitting as DAY LIMIT + OTO"
            )
            order_request = LimitOrderRequest(
                symbol=decision.symbol,
                qty=decision.qty,
                side=AlpacaOrderSide.BUY if decision.side is Side.BUY else AlpacaOrderSide.SELL,
                type=AlpacaOrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                order_class=AlpacaOrderClass.OTO,
                stop_loss=stop_loss,
                client_order_id=client_order_id,
                limit_price=cap,
            )
        elif decision.order_type is OrderType.LIMIT:
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

        # Operator Controls Phase A — write the pending lifecycle row
        # only after the dry-run guard passes, so a preflight dry run
        # never leaks pending positions to the operator CLI.
        position_uid = self._lifecycle_begin(
            decision=decision,
            client_order_id=client_order_id,
        )

        # Register with the stream before submitting to avoid a fill-before-watch race.
        stream_event: threading.Event | None = None
        if self._stream_manager is not None:
            stream_event = self._stream_manager.watch(client_order_id)

        if not self._entries_allowed():
            logger.warning(
                f"entry canceled before Alpaca submit: {decision.symbol} "
                "(global risk halt active)"
            )
            if self._stream_manager is not None:
                self._stream_manager.unwatch(client_order_id)
            self._lifecycle_mark_canceled(position_uid)
            return self._entry_halt_result(decision.symbol, decision.qty)

        try:
            order = self._with_retry(
                lambda: self._api.submit_order(order_request),
                op_desc=f"submit_order({decision.symbol})",
            )
        except APIError as e:
            logger.error(f"broker rejected {decision.symbol}: {e}")
            # Lifecycle: zero-fill rejection — mark canceled so the row
            # doesn't sit indefinitely as 'pending'.
            self._lifecycle_mark_canceled(position_uid)
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=None,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                filled_qty=0,
                avg_fill_price=None,
                raw_status=None,
                message=str(e),
                position_uid=position_uid,
            )

        order_id = str(order.id)

        # Bind the real Alpaca order ID back to the pre-submit watch so either
        # identifier can resolve the same terminal update.
        if self._stream_manager is not None:
            self._stream_manager.bind_submitted_order(
                client_order_id=client_order_id,
                order_id=order_id,
                stop_leg_ids=[
                    str(leg.id)
                    for leg in (getattr(order, "legs", None) or [])
                    if getattr(leg, "id", None) is not None
                ],
            )

        try:
            result = self._wait_for_fill(
                order_id=order_id,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                timeout=poll_timeout,
                interval=poll_interval,
                stream_event=stream_event,
            )
        except Exception as e:
            result = self._unknown_after_submit(
                order_id=order_id,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                error=e,
            )
        # Lifecycle: best-effort fill transition. Returns the result
        # with position_uid attached so the engine can pass it to
        # TradeLogger.log().
        self._lifecycle_mark_filled(position_uid=position_uid, result=result)
        return replace(result, position_uid=position_uid)

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
        # Operator Controls Phase A — position_uid created after the
        # dry-run guard below; same reasoning as in place_order().
        position_uid: str | None = None

        # PLAN 11.32: defense in depth. place_order() floors fractional qty
        # to whole shares when entry_max_price is set so the capped DAY
        # LIMIT + OTO path applies. If we ever reach this branch with a cap
        # set, it means the floor logic was bypassed or removed — log an
        # ERROR so it's loud in alerts.
        if decision.entry_max_price is not None:
            logger.error(
                f"[entry-guard] {decision.symbol}: BUG — entry_max_price "
                f"${decision.entry_max_price:.2f} reached the fractional path "
                f"(qty={decision.qty}). The cap will NOT be enforced. "
                f"Investigate place_order() floor logic."
            )

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

        # Operator Controls Phase A — pending lifecycle row written
        # only after the dry-run guard passes.
        position_uid = self._lifecycle_begin(
            decision=decision,
            client_order_id=client_order_id,
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

        if not self._entries_allowed():
            logger.warning(
                f"fractional entry canceled before Alpaca submit: "
                f"{decision.symbol} (global risk halt active)"
            )
            if self._stream_manager is not None:
                self._stream_manager.unwatch(client_order_id)
            self._lifecycle_mark_canceled(position_uid)
            return self._entry_halt_result(decision.symbol, decision.qty)

        try:
            order = self._with_retry(
                lambda: self._api.submit_order(order_request),
                op_desc=f"submit_frac_order({decision.symbol})",
            )
        except APIError as e:
            logger.error(f"broker rejected fractional {decision.symbol}: {e}")
            self._lifecycle_mark_canceled(position_uid)
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=None,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                filled_qty=0,
                avg_fill_price=None,
                raw_status=None,
                message=str(e),
                position_uid=position_uid,
            )

        order_id = str(order.id)
        if self._stream_manager is not None:
            self._stream_manager.bind_submitted_order(
                client_order_id=client_order_id,
                order_id=order_id,
            )

        try:
            result = self._wait_for_fill(
                order_id=order_id,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                timeout=poll_timeout,
                interval=poll_interval,
                stream_event=stream_event,
            )
        except Exception as e:
            result = self._unknown_after_submit(
                order_id=order_id,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                error=e,
            )
            self._lifecycle_mark_filled(position_uid=position_uid, result=result)
            return replace(result, position_uid=position_uid)

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
                    stop_order = self._with_retry(
                        lambda: self._api.submit_order(stop_request),
                        op_desc=f"submit_frac_stop({decision.symbol})",
                    )
                    self._register_standalone_stop_leg(stop_order)
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

        # Operator Controls Phase A — lifecycle transition on the
        # success path. Best-effort.
        self._lifecycle_mark_filled(position_uid=position_uid, result=result)
        return replace(result, position_uid=position_uid)

    def reconcile_submitted_order(
        self,
        *,
        order_id: str,
        symbol: str,
        requested_qty: float,
    ) -> OrderResult:
        """
        Reconcile a bot-submitted order whose post-submit confirmation failed.

        This is intentionally narrow: callers must provide the exact order_id
        returned by Alpaca. Unknown broker positions are not adopted through
        this path.
        """
        order = self._with_retry(
            lambda: self._api.get_order_by_id(order_id),
            op_desc=f"reconcile_order({order_id})",
        )
        raw = order.status.value if isinstance(order.status, AlpacaOrderStatus) else str(order.status)
        mapped = _ALPACA_TERMINAL.get(raw)
        if mapped is not None and mapped is not OrderStatus.PARTIAL:
            return self._build_result(order, symbol, requested_qty, mapped)

        filled = float(order.filled_qty or 0)
        if filled > 0:
            return self._build_result(order, symbol, requested_qty, OrderStatus.PARTIAL)

        return self._build_result(order, symbol, requested_qty, OrderStatus.PENDING)

    def drain_option_fills(self) -> list[tuple]:
        """
        Return and clear all option fill events reported by background workers.
        Each entry is (RiskDecision, status_str, filled_qty, avg_fill_price, order_id).
        TradingEngine drains this each cycle to log fills and update position ownership.
        """
        with self._pending_option_lock:
            fills = list(self._pending_option_fills)
            self._pending_option_fills.clear()
        return fills

    def place_spread_order(
        self,
        *,
        legs: list[SpreadLeg],
        qty: int,
        limit_price: float,
        strategy_name: str,
    ) -> OrderResult:
        """
        Submit an atomic multi-leg (MLEG) limit order synchronously (11.28).

        Low-level primitive: builds the combo request, submits it, and
        returns the broker's initial ``OrderResult``. It does **not** run a
        watch/cancel loop — the async lifecycle (stream watch + timeout
        cancel) is ``SpreadExecutionWorker``'s job. The integration verify
        script calls this directly; PR 3's credit-spread strategy will go
        through the worker.

        ``limit_price`` is the net price of the combo. Alpaca's MLEG sign
        convention: **positive** is a net debit paid, **negative** is a net
        credit required. A bull put credit spread collecting ~$1.40/share is
        submitted with ``limit_price = -1.40`` — a positive value would mean
        "pay any debit up to that amount" and fill near-instantly.

        ``OrderResult.symbol`` is the short (SELL) leg's OCC string — the
        spread's defining contract.
        """
        short_leg = next((leg for leg in legs if leg.side is Side.SELL), legs[0])
        rep_symbol = short_leg.occ_symbol

        if self._dry_run:
            logger.warning(
                f"DRY RUN — MLEG spread order NOT submitted: {qty}× "
                f"[{', '.join(leg.occ_symbol for leg in legs)}] @ net {limit_price:.2f}"
            )
            return OrderResult(
                status=OrderStatus.FILLED,
                order_id=f"dry-run-{uuid.uuid4().hex[:10]}",
                symbol=rep_symbol,
                requested_qty=qty,
                filled_qty=qty,
                avg_fill_price=round(limit_price, 2),
                raw_status="dry_run",
                message="dry run — no MLEG order submitted",
            )

        client_order_id = f"spr-{strategy_name}-{uuid.uuid4().hex[:10]}"
        try:
            req = build_mleg_request(
                legs=legs,
                qty=qty,
                limit_price=limit_price,
                client_order_id=client_order_id,
            )
        except ValueError as e:
            logger.error(f"place_spread_order: invalid MLEG request: {e}")
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=None,
                symbol=rep_symbol,
                requested_qty=qty,
                filled_qty=0,
                avg_fill_price=None,
                raw_status=None,
                message=str(e),
            )

        try:
            order = self._with_retry(
                lambda: self._api.submit_order(req),
                op_desc=f"place_spread_order({rep_symbol})",
            )
        except APIError as e:
            logger.error(f"place_spread_order({rep_symbol}) rejected: {e}")
            return OrderResult(
                status=OrderStatus.REJECTED,
                order_id=None,
                symbol=rep_symbol,
                requested_qty=qty,
                filled_qty=0,
                avg_fill_price=None,
                raw_status=None,
                message=str(e),
            )

        raw_status = (
            order.status.value
            if hasattr(order.status, "value")
            else str(order.status)
        )
        status = _ALPACA_TERMINAL.get(raw_status, OrderStatus.ACCEPTED)
        logger.info(
            f"MLEG spread order {order.id} submitted ({rep_symbol}) — "
            f"status={raw_status}"
        )
        return OrderResult(
            status=status,
            order_id=str(order.id),
            symbol=rep_symbol,
            requested_qty=qty,
            filled_qty=float(getattr(order, "filled_qty", 0) or 0),
            avg_fill_price=(
                float(order.filled_avg_price)
                if getattr(order, "filled_avg_price", None) is not None
                else None
            ),
            raw_status=raw_status,
            message="MLEG combo order submitted",
        )

    def close_spread_order(
        self,
        *,
        legs: list[SpreadLeg],
        qty: int,
        limit_price: float,
        strategy_name: str,
    ) -> OrderResult:
        """
        Submit a closing MLEG combo order for an open spread (11.28).

        Accepts the same ``legs`` used to open the spread and rebuilds them
        as the *reversing* trade: each leg's side is flipped and the intent
        becomes ``*_TO_CLOSE``. For a bull put credit spread opened as
        short SELL_TO_OPEN + long BUY_TO_OPEN, this produces short
        BUY_TO_CLOSE + long SELL_TO_CLOSE — the trade that actually flattens
        the position.

        ``limit_price`` is the net debit paid to buy the spread back
        (positive, per the Alpaca MLEG sign convention).
        """
        closing_legs = [
            SpreadLeg(
                occ_symbol=leg.occ_symbol,
                side=Side.BUY if leg.side is Side.SELL else Side.SELL,
                opening=False,
                ratio_qty=leg.ratio_qty,
            )
            for leg in legs
        ]
        return self.place_spread_order(
            legs=closing_legs,
            qty=qty,
            limit_price=limit_price,
            strategy_name=strategy_name,
        )

    def dispatch_spread_order(
        self,
        *,
        legs: list[SpreadLeg],
        qty: int,
        limit_price: float,
        strategy_name: str,
        position_id: str,
        closing: bool = False,
    ) -> OrderResult:
        """
        Dispatch an asynchronous MLEG combo via ``SpreadExecutionWorker``
        (11.29 PR 3b). Mirrors how ``place_order`` dispatches the single-leg
        ``OptionsExecutionWorker``.

        ``closing=False`` opens a spread; ``closing=True`` closes one — the
        legs are reversed (each side flipped, ``opening=False``) into the
        ``*_TO_CLOSE`` trade that flattens the position. ``limit_price``
        follows the Alpaca MLEG sign convention: negative net credit to
        open, positive net debit to close.

        The worker submits the combo, watches for the atomic fill via the
        stream, and cancels if unfilled past its timeout. Its terminal
        outcome is pushed onto ``_pending_spread_fills`` tagged with
        ``position_id`` and the ``closing`` flag so ``TradingEngine`` can
        reconcile it against the tracked spread ``Position``.

        Returns immediately with ``OrderResult(ACCEPTED)``. In DRY_RUN a
        synthetic FILLED event is queued onto the drain instead — so the
        engine's dispatch → drain-confirm flow is identical in both modes.
        """
        if closing:
            legs = [
                SpreadLeg(
                    occ_symbol=leg.occ_symbol,
                    side=Side.BUY if leg.side is Side.SELL else Side.SELL,
                    opening=False,
                    ratio_qty=leg.ratio_qty,
                )
                for leg in legs
            ]
        short_leg = next((leg for leg in legs if leg.side is Side.SELL), legs[0])
        rep_symbol = short_leg.occ_symbol
        action = "close" if closing else "open"

        if not closing and not self._entries_allowed():
            logger.warning(
                f"MLEG spread entry blocked before dispatch: {rep_symbol} "
                "(global risk halt active)"
            )
            return self._entry_halt_result(rep_symbol, qty)

        if self._dry_run:
            logger.warning(
                f"DRY RUN — MLEG spread {action} NOT dispatched: {qty}× "
                f"[{', '.join(leg.occ_symbol for leg in legs)}] @ net {limit_price:.2f}"
            )
            with self._pending_spread_lock:
                self._pending_spread_fills.append((
                    position_id, strategy_name, closing, "filled", float(qty),
                    round(limit_price, 2), f"dry-run-{uuid.uuid4().hex[:10]}",
                    round(limit_price, 2),
                ))
            return OrderResult(
                status=OrderStatus.ACCEPTED,
                order_id=f"dry-run-{uuid.uuid4().hex[:10]}",
                symbol=rep_symbol,
                requested_qty=qty,
                filled_qty=0.0,
                avg_fill_price=0.0,
                raw_status="dry_run",
                message=f"dry run — synthetic spread {action} fill queued",
            )

        def _on_fill(
            status: str,
            filled_qty: float,
            avg_price: "float | None",
            order_id: str,
        ) -> None:
            with self._pending_spread_lock:
                self._pending_spread_fills.append((
                    position_id, strategy_name, closing, status,
                    filled_qty, avg_price, order_id, round(limit_price, 2),
                ))

        worker = SpreadExecutionWorker(
            legs=legs,
            qty=qty,
            limit_price=limit_price,
            strategy_name=strategy_name,
            api=self._api,
            stream_manager=self._stream_manager,
            on_fill=_on_fill,
            entry_allowed=None if closing else self._entry_allowed,
        )
        worker.start()
        return OrderResult(
            status=OrderStatus.ACCEPTED,
            order_id=f"spread-worker-{uuid.uuid4().hex[:10]}",
            symbol=rep_symbol,
            requested_qty=qty,
            filled_qty=0.0,
            avg_fill_price=0.0,
            raw_status="accepted",
            message=f"dispatched to SpreadExecutionWorker ({action})",
        )

    def drain_spread_fills(self) -> list[tuple]:
        """
        Return and clear all MLEG combo fill events reported by background
        ``SpreadExecutionWorker`` threads.

        Each entry is ``(position_id, strategy_name, closing, status_str,
        filled_qty, avg_fill_price, order_id, submitted_limit_price)`` —
        ``closing`` distinguishes a spread open from a spread close.
        ``submitted_limit_price`` is the original signed combo limit used for
        reusable MLEG slippage attribution.
        """
        with self._pending_spread_lock:
            fills = list(self._pending_spread_fills)
            self._pending_spread_fills.clear()
        return fills

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
        poll_timeout: float = ORDER_CONFIRM_TIMEOUT_SECONDS,
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
        self._register_standalone_stop_leg(order)
        return self._to_open_order(order)

    def submit_option_gtc_stop(
        self,
        *,
        symbol: str,
        qty: float,
        stop_price: float,
        client_order_id_prefix: str = "opt-trail-stop",
    ) -> OpenOrder:
        """Submit a durable GTC SELL stop for a single-leg option position."""
        if qty <= 0:
            raise BrokerError(f"option stop qty must be positive for {symbol}: {qty}")
        whole_qty = int(qty)
        if abs(float(qty) - whole_qty) > 1e-9:
            raise BrokerError(
                f"option stop qty must be a whole contract for {symbol}: {qty}"
            )
        if stop_price <= 0:
            raise BrokerError(
                f"option stop price must be positive for {symbol}: {stop_price}"
            )
        client_order_id = f"{client_order_id_prefix}-{uuid.uuid4().hex[:10]}"
        order_request = StopOrderRequest(
            symbol=symbol,
            qty=whole_qty,
            side=AlpacaOrderSide.SELL,
            time_in_force=TimeInForce.GTC,
            stop_price=round(stop_price, 2),
            client_order_id=client_order_id,
        )
        logger.warning(
            f"submitting option GTC trailing stop for {symbol}: "
            f"sell {qty:g} stop @ ${stop_price:.2f} "
            f"(client_id={client_order_id})"
        )
        order = self._with_retry(
            lambda: self._api.submit_order(order_request),
            op_desc=f"submit_option_gtc_stop({symbol})",
        )
        self._register_standalone_stop_leg(order)
        return self._to_open_order(order)

    def replace_option_stop(
        self,
        *,
        order_id: str,
        stop_price: float,
        client_order_id_prefix: str = "opt-trail-stop",
    ) -> OpenOrder:
        """Atomically ratchet an open option stop and enforce GTC duration."""
        if not order_id:
            raise BrokerError("option stop replacement requires an order id")
        if stop_price <= 0:
            raise BrokerError(
                f"option replacement stop price must be positive: {stop_price}"
            )
        client_order_id = f"{client_order_id_prefix}-{uuid.uuid4().hex[:10]}"
        request = ReplaceOrderRequest(
            time_in_force=TimeInForce.GTC,
            stop_price=round(stop_price, 2),
            client_order_id=client_order_id,
        )
        logger.warning(
            f"replacing option trailing stop {order_id}: "
            f"new GTC stop @ ${stop_price:.2f} (client_id={client_order_id})"
        )
        order = self._with_retry(
            lambda: self._api.replace_order_by_id(order_id, request),
            op_desc=f"replace_option_stop({order_id})",
        )
        self._register_standalone_stop_leg(order)
        return self._to_open_order(order)

    # ── Internals ────────────────────────────────────────────────────────

    def _wait_for_fill(
        self,
        *,
        order_id: str,
        symbol: str,
        requested_qty: float,
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
        try:
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
        finally:
            if self._stream_manager is not None:
                self._stream_manager.unwatch(order_id)

    @staticmethod
    def _build_result_from_stream(
        update,
        symbol: str,
        requested_qty: float,
    ) -> "OrderResult":
        """Build an OrderResult from a terminal TradeUpdate (WebSocket path)."""
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
        # update.qty is the per-execution chunk for this event only.
        # update.order.filled_qty is the cumulative total across all executions — correct for position sizing.
        # Same issue applies to price: update.price is the last-execution price;
        # update.order.filled_avg_price is the VWAP across all partial fills.
        filled_qty = float(update.order.filled_qty or 0)
        raw_avg = update.order.filled_avg_price
        avg_price = float(raw_avg) if raw_avg is not None else None
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
            submitted_at=AlpacaBroker._parse_datetime(getattr(update.order, "submitted_at", None)),
            filled_at=AlpacaBroker._parse_datetime(getattr(update.order, "filled_at", None)),
        )

    def _poll_until_terminal(
        self,
        *,
        order_id: str,
        symbol: str,
        requested_qty: float,
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
                filled = float(order.filled_qty or 0)
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
        order, symbol: str, requested_qty: float, status: OrderStatus
    ) -> OrderResult:
        filled = float(order.filled_qty or 0)
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
            submitted_at=AlpacaBroker._parse_datetime(getattr(order, "submitted_at", None)),
            filled_at=AlpacaBroker._parse_datetime(getattr(order, "filled_at", None)),
        )

    @staticmethod
    def _unknown_after_submit(
        *,
        order_id: str,
        symbol: str,
        requested_qty: float,
        error: Exception,
    ) -> OrderResult:
        msg = (
            f"submitted as {order_id}, but fill confirmation failed: {error}"
        )
        logger.warning(f"{symbol} order {order_id}: {msg}")
        return OrderResult(
            status=OrderStatus.UNKNOWN,
            order_id=order_id,
            symbol=symbol,
            requested_qty=requested_qty,
            filled_qty=0,
            avg_fill_price=None,
            raw_status=None,
            message=msg,
        )

    def _register_standalone_stop_leg(self, order) -> None:
        """Register a standalone broker stop order so later stop fills get logged."""
        if self._stream_manager is None:
            return
        order_id = getattr(order, "id", None)
        if order_id is None:
            return
        self._stream_manager.register_stop_leg(str(order_id))

    def _stream_lookup_order_by_id(self, order_id: str):
        """Read-only lookup hook used by StreamManager gap recovery."""
        return self._with_retry(
            lambda: self._api.get_order_by_id(order_id),
            op_desc=f"stream_get_order({order_id})",
        )

    def _stream_lookup_order_by_client_id(self, client_order_id: str):
        """Read-only lookup hook used by StreamManager gap recovery."""
        return self._with_retry(
            lambda: self._api.get_order_by_client_id(client_order_id),
            op_desc=f"stream_get_order_by_client_id({client_order_id})",
        )

    def close_connections(self) -> None:
        """Close idle broker HTTP connections between cycles."""
        self._api._session.close()

    @staticmethod
    def _parse_datetime(value) -> datetime | None:
        if isinstance(value, str):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value

    @staticmethod
    def _to_open_order(o) -> OpenOrder | None:
        # Handle both alpaca-py model objects and SimpleNamespace mocks.
        submitted = AlpacaBroker._parse_datetime(getattr(o, "submitted_at", None))
        if submitted is None:
            submitted = datetime.now(timezone.utc)

        order_class = getattr(o, "order_class", None)
        order_class_val = (
            order_class.value if hasattr(order_class, "value") else str(order_class)
            if order_class is not None else None
        )

        # Extract raw string values from enums or plain strings.
        side_raw = getattr(o, "side", None)
        symbol = getattr(o, "symbol", None)
        if side_raw is None or symbol is None:
            if order_class_val == "mleg":
                logger.debug(
                    "Skipping top-level MLEG parent order in open-order snapshot: "
                    f"order_id={getattr(o, 'id', None)} side={side_raw!r} symbol={symbol!r}"
                )
                return None
            raise ValueError(
                f"open order {getattr(o, 'id', None)} missing required side/symbol "
                f"(side={side_raw!r}, symbol={symbol!r})"
            )

        side_val = side_raw.value if hasattr(side_raw, 'value') else str(side_raw)
        type_val = o.type.value if hasattr(o.type, 'value') else str(o.type) if hasattr(o, 'type') and o.type else None
        status_val = o.status.value if hasattr(o.status, 'value') else str(o.status)
        order_id = str(o.id) if o.id is not None else None
        tif_raw = getattr(o, "time_in_force", None)
        tif_val = (
            tif_raw.value if hasattr(tif_raw, "value") else str(tif_raw)
            if tif_raw is not None else None
        )

        return OpenOrder(
            order_id=order_id,
            symbol=symbol,
            side=Side(side_val),
            qty=float(o.qty),
            order_type=OrderType(type_val) if type_val in {ot.value for ot in OrderType} else OrderType.MARKET,
            status=status_val,
            submitted_at=submitted,
            limit_price=float(o.limit_price) if getattr(o, "limit_price", None) else None,
            stop_price=float(o.stop_price) if getattr(o, "stop_price", None) else None,
            client_order_id=getattr(o, "client_order_id", None),
            time_in_force=tif_val,
        )

    @staticmethod
    def _to_closed_order_info(o) -> ClosedOrderInfo | None:
        order_class = getattr(o, "order_class", None)
        order_class_val = (
            order_class.value if hasattr(order_class, "value") else str(order_class)
            if order_class is not None else None
        )
        side_raw = getattr(o, "side", None)
        symbol = getattr(o, "symbol", None)
        if side_raw is None or symbol is None:
            if order_class_val == "mleg":
                logger.debug(
                    "Skipping top-level MLEG parent order in closed-order recovery: "
                    f"order_id={getattr(o, 'id', None)} side={side_raw!r} symbol={symbol!r}"
                )
                return None
            raise ValueError(
                f"closed order {getattr(o, 'id', None)} missing required side/symbol "
                f"(side={side_raw!r}, symbol={symbol!r})"
            )

        side_val = side_raw.value if hasattr(side_raw, "value") else str(side_raw)
        type_val = (
            o.type.value
            if hasattr(o.type, "value")
            else str(o.type)
            if hasattr(o, "type") and o.type
            else None
        )
        status_val = o.status.value if hasattr(o.status, "value") else str(o.status)
        return ClosedOrderInfo(
            order_id=str(o.id),
            client_order_id=getattr(o, "client_order_id", None),
            symbol=o.symbol,
            side=Side(side_val),
            order_type=type_val,
            status=_ALPACA_TERMINAL.get(status_val, OrderStatus.CANCELED),
            raw_status=status_val,
            qty=float(o.qty or 0.0),
            filled_qty=float(o.filled_qty or 0.0),
            avg_fill_price=(
                float(o.filled_avg_price)
                if getattr(o, "filled_avg_price", None) is not None
                else None
            ),
            stop_price=(
                float(o.stop_price)
                if getattr(o, "stop_price", None) is not None
                else None
            ),
            submitted_at=AlpacaBroker._parse_datetime(getattr(o, "submitted_at", None)),
            filled_at=AlpacaBroker._parse_datetime(getattr(o, "filled_at", None)),
        )
