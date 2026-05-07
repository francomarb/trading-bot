"""
WebSocket order/fill streaming with reconnect + gap recovery.

StreamManager runs a repo-owned asyncio loop inside a background daemon thread.
It provides three integration seams:

  1. **Entry-order fill detection** (used by AlpacaBroker / options worker):
       event = stream.watch(client_order_id)
       stream.bind_submitted_order(client_order_id, order_id, stop_leg_ids=[...])
       event.wait(timeout=30)
       update = stream.get_update(order_id)

  2. **Stop-out detection** (used by TradingEngine each cycle):
       fills = stream.drain_stop_fills()

  3. **Connection observability** (used by TradingEngine each cycle):
       health = stream.health_snapshot()

Thread-safety: all mutable state is guarded by a single threading.Lock. The
async websocket handler acquires the lock only for O(1) bookkeeping so the
event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import json
import random
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable

import websockets
from alpaca.common.enums import BaseURL
from alpaca.trading.models import TradeUpdate
from loguru import logger
from websockets.legacy import client as websockets_legacy

from config import settings

if TYPE_CHECKING:
    from websockets.legacy.client import WebSocketClientProtocol


_TERMINAL_EVENTS: frozenset[str] = frozenset({
    "fill",
    "canceled",
    "expired",
    "replaced",
    "rejected",
})
_FILL_EVENTS: frozenset[str] = frozenset({"fill"})
_STATUS_TO_EVENT: dict[str, str] = {
    "filled": "fill",
    "stopped": "fill",
    "canceled": "canceled",
    "expired": "expired",
    "rejected": "rejected",
    "replaced": "replaced",
    "done_for_day": "canceled",
}


@dataclass(frozen=True)
class StreamHealth:
    """Thread-safe snapshot of websocket connection state."""

    connected: bool
    healthy: bool
    generation: int
    last_rx_at: datetime | None
    last_disconnect_at: datetime | None
    last_reconnect_at: datetime | None
    consecutive_failures: int


@dataclass
class _TrackedOrder:
    """Internal entry-order watch registration."""

    client_order_id: str
    event: threading.Event
    order_id: str | None = None
    update: Any | None = None
    aliases: set[str] = field(default_factory=set)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StreamManager:
    """
    Owns the Trading API websocket lifecycle, reconnection policy, and
    reconnect-time order/stop-leg re-sync.
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        *,
        paper: bool = True,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._endpoint = (
            BaseURL.TRADING_STREAM_PAPER if paper else BaseURL.TRADING_STREAM_LIVE
        )

        self._lock = threading.Lock()
        self._watched: dict[str, _TrackedOrder] = {}
        self._alias_to_canonical: dict[str, str] = {}
        self._stop_legs: set[str] = set()
        self._stop_fills: list[Any] = []
        self._health = StreamHealth(
            connected=False,
            healthy=False,
            generation=0,
            last_rx_at=None,
            last_disconnect_at=None,
            last_reconnect_at=None,
            consecutive_failures=0,
        )

        self._lookup_order_by_id: Callable[[str], Any] | None = None
        self._lookup_order_by_client_id: Callable[[str], Any] | None = None

        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._thread_stop = threading.Event()
        self._ws: WebSocketClientProtocol | None = None

        self._heartbeat_interval = settings.STREAM_HEARTBEAT_INTERVAL_SECONDS
        self._heartbeat_timeout = settings.STREAM_HEARTBEAT_TIMEOUT_SECONDS
        self._reconnect_base_delay = settings.STREAM_RECONNECT_BASE_DELAY_SECONDS
        self._reconnect_max_delay = settings.STREAM_RECONNECT_MAX_DELAY_SECONDS

    # ── Public API ───────────────────────────────────────────────────────

    def watch(self, client_order_id: str) -> threading.Event:
        """
        Register a pre-submit watch keyed by client_order_id.

        The returned event is shared across aliases after bind_submitted_order()
        links the eventual Alpaca order.id back to this pre-submit registration.
        """
        with self._lock:
            canonical = self._alias_to_canonical.get(client_order_id)
            if canonical is not None and canonical in self._watched:
                return self._watched[canonical].event

            event = threading.Event()
            tracked = _TrackedOrder(
                client_order_id=client_order_id,
                event=event,
                aliases={client_order_id},
            )
            self._watched[client_order_id] = tracked
            self._alias_to_canonical[client_order_id] = client_order_id
            return event

    def bind_submitted_order(
        self,
        client_order_id: str,
        order_id: str,
        stop_leg_ids: list[str] | tuple[str, ...] = (),
    ) -> None:
        """
        Link a pre-submit client_order_id watch to the real Alpaca order.id.

        This closes the fill-before-watch race: an immediate terminal update can
        now be resolved by either identifier.
        """
        with self._lock:
            canonical = self._alias_to_canonical.get(client_order_id, client_order_id)
            tracked = self._watched.get(canonical)
            if tracked is None:
                tracked = _TrackedOrder(
                    client_order_id=client_order_id,
                    event=threading.Event(),
                    aliases={client_order_id},
                )
                self._watched[canonical] = tracked
                self._alias_to_canonical[client_order_id] = canonical

            tracked.order_id = order_id
            tracked.aliases.add(order_id)
            self._alias_to_canonical[order_id] = canonical
            for stop_leg_id in stop_leg_ids:
                self._stop_legs.add(stop_leg_id)

    def set_order_lookup_callbacks(
        self,
        *,
        by_id: Callable[[str], Any],
        by_client_id: Callable[[str], Any] | None = None,
    ) -> None:
        """Inject broker-backed read-only lookup callbacks for gap recovery."""
        self._lookup_order_by_id = by_id
        self._lookup_order_by_client_id = by_client_id

    def get_update(self, order_id: str) -> Any | None:
        """Return the latest terminal update for a watched order, if any."""
        with self._lock:
            canonical = self._alias_to_canonical.get(order_id, order_id)
            tracked = self._watched.get(canonical)
            return None if tracked is None else tracked.update

    def register_stop_leg(self, order_id: str) -> None:
        """Legacy helper: register a standalone stop-leg order id."""
        with self._lock:
            self._stop_legs.add(order_id)

    def unwatch(self, order_id: str) -> None:
        """
        Remove watch/update bookkeeping for an entry order.

        Stop-leg registrations are intentionally preserved; they outlive the
        entry-order watch because the engine still needs later stop-fill events.
        """
        with self._lock:
            canonical = self._alias_to_canonical.get(order_id, order_id)
            tracked = self._watched.pop(canonical, None)
            if tracked is None:
                return
            for alias in tracked.aliases:
                self._alias_to_canonical.pop(alias, None)

    def drain_stop_fills(self) -> list[Any]:
        """Return and clear accumulated stop-leg fill events."""
        with self._lock:
            fills = list(self._stop_fills)
            self._stop_fills.clear()
            return fills

    def health_snapshot(self) -> StreamHealth:
        """Return an immutable snapshot of current websocket health."""
        with self._lock:
            return StreamHealth(**self._health.__dict__)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background websocket thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread_stop.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="alpaca-stream",
            daemon=True,
        )
        self._thread.start()
        logger.info("stream manager: background thread started")

    def stop(self) -> None:
        """Stop the websocket loop and wait for the background thread to exit."""
        self._thread_stop.set()
        if self._loop is not None and self._stop_event is not None:
            try:
                self._loop.call_soon_threadsafe(self._stop_event.set)
            except RuntimeError:
                pass
        if self._loop is not None and self._ws is not None:
            try:
                asyncio.run_coroutine_threadsafe(self._close_ws(), self._loop).result(timeout=5.0)
            except Exception as e:
                logger.warning(f"stream stop error (ignored): {e}")
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("stream manager: stopped")

    # ── Internal thread / event-loop management ──────────────────────────

    def _run_loop(self) -> None:
        try:
            asyncio.run(self._run_async())
        except Exception as e:
            logger.error(f"stream manager: unrecoverable error — {e}")

    async def _run_async(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        delay = self._reconnect_base_delay

        while not self._thread_stop.is_set():
            try:
                await self._run_session()
                delay = self._reconnect_base_delay
                if self._thread_stop.is_set():
                    break
                logger.warning("stream manager: session ended unexpectedly; reconnecting")
                await self._mark_disconnected(RuntimeError("stream session ended"))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if self._thread_stop.is_set():
                    break
                await self._mark_disconnected(exc)
            if self._thread_stop.is_set():
                break
            backoff = min(delay, self._reconnect_max_delay)
            jitter = min(backoff * 0.2, 1.0)
            sleep_for = backoff + random.uniform(0.0, jitter)
            logger.warning(f"stream manager: reconnecting in {sleep_for:.2f}s")
            await self._sleep_with_stop(sleep_for)
            delay = min(backoff * 2.0, self._reconnect_max_delay)

        await self._close_ws()
        self._loop = None
        self._stop_event = None

    async def _run_session(self) -> None:
        had_gap = self.health_snapshot().last_disconnect_at is not None
        await self._connect_and_subscribe()
        await self._mark_connected()
        if had_gap:
            await self._resync_tracked_state()

        receiver = asyncio.create_task(self._recv_loop())
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        stopper = asyncio.create_task(self._wait_for_stop())
        done, pending = await asyncio.wait(
            {receiver, heartbeat, stopper},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        result = next(iter(done))
        exc = result.exception()
        await self._close_ws()
        if exc is not None:
            raise exc

    async def _wait_for_stop(self) -> None:
        assert self._stop_event is not None
        await self._stop_event.wait()

    async def _sleep_with_stop(self, seconds: float) -> None:
        if seconds <= 0:
            return
        assert self._stop_event is not None
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return

    async def _connect_and_subscribe(self) -> None:
        self._ws = await websockets_legacy.connect(
            self._endpoint,
            ping_interval=None,
            ping_timeout=None,
            max_queue=1024,
        )
        await self._ws.send(
            json.dumps({
                "action": "authenticate",
                "data": {
                    "key_id": self._api_key,
                    "secret_key": self._secret_key,
                },
            })
        )
        auth_raw = await self._ws.recv()
        self._touch_rx()
        auth_msg = json.loads(auth_raw)
        if auth_msg.get("data", {}).get("status") != "authorized":
            raise ValueError("failed to authenticate trading stream")

        await self._ws.send(
            json.dumps({
                "action": "listen",
                "data": {"streams": ["trade_updates"]},
            })
        )
        logger.info("stream manager: trading websocket connected")

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        while not self._thread_stop.is_set():
            raw = await asyncio.wait_for(self._ws.recv(), timeout=1.0)
            self._touch_rx()
            msg = json.loads(raw)
            await self._dispatch_message(msg)

    async def _heartbeat_loop(self) -> None:
        while not self._thread_stop.is_set():
            await self._sleep_with_stop(self._heartbeat_interval)
            if self._thread_stop.is_set():
                return
            if self._ws is None:
                raise RuntimeError("heartbeat attempted without websocket")
            pong_waiter = await self._ws.ping()
            try:
                await asyncio.wait_for(pong_waiter, timeout=self._heartbeat_timeout)
            except asyncio.TimeoutError as exc:
                raise TimeoutError("trading websocket heartbeat timeout") from exc
            self._touch_rx()

    async def _dispatch_message(self, msg: dict[str, Any]) -> None:
        if msg.get("stream") != "trade_updates":
            return
        update = TradeUpdate(**msg.get("data"))
        await self._on_trade_update(update)

    async def _close_ws(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.close()
        except Exception:
            pass
        finally:
            self._ws = None

    async def _mark_connected(self) -> None:
        now = _utcnow()
        with self._lock:
            self._health = StreamHealth(
                connected=True,
                healthy=True,
                generation=self._health.generation + 1,
                last_rx_at=self._health.last_rx_at,
                last_disconnect_at=self._health.last_disconnect_at,
                last_reconnect_at=now,
                consecutive_failures=0,
            )
        logger.info(
            f"stream manager: healthy (generation={self._health.generation})"
        )

    async def _mark_disconnected(self, exc: Exception) -> None:
        await self._close_ws()
        now = _utcnow()
        with self._lock:
            self._health = StreamHealth(
                connected=False,
                healthy=False,
                generation=self._health.generation,
                last_rx_at=self._health.last_rx_at,
                last_disconnect_at=now,
                last_reconnect_at=self._health.last_reconnect_at,
                consecutive_failures=self._health.consecutive_failures + 1,
            )
            failures = self._health.consecutive_failures
        logger.warning(
            f"stream manager: disconnected ({type(exc).__name__}: {exc}) "
            f"[failures={failures}]"
        )

    def _touch_rx(self) -> None:
        now = _utcnow()
        with self._lock:
            self._health = StreamHealth(
                connected=self._health.connected,
                healthy=self._health.healthy,
                generation=self._health.generation,
                last_rx_at=now,
                last_disconnect_at=self._health.last_disconnect_at,
                last_reconnect_at=self._health.last_reconnect_at,
                consecutive_failures=self._health.consecutive_failures,
            )

    # ── Gap recovery ─────────────────────────────────────────────────────

    async def _resync_tracked_state(self) -> None:
        tracked_orders, stop_leg_ids = self._snapshot_pending_state()
        if not tracked_orders and not stop_leg_ids:
            return

        recovered_watches = 0
        recovered_stops = 0

        for tracked in tracked_orders:
            if tracked.update is not None or tracked.event.is_set():
                continue
            order = None
            if tracked.order_id is not None and self._lookup_order_by_id is not None:
                order = await self._lookup_safe(
                    lambda: self._lookup_order_by_id(tracked.order_id),
                    f"order_id={tracked.order_id}",
                )
            if order is None and self._lookup_order_by_client_id is not None:
                order = await self._lookup_safe(
                    lambda: self._lookup_order_by_client_id(tracked.client_order_id),
                    f"client_order_id={tracked.client_order_id}",
                )
            if order is None:
                continue
            event_val = _STATUS_TO_EVENT.get(self._raw_status(order))
            if event_val is None:
                continue
            await self._on_trade_update(self._make_synthetic_update(order, event_val))
            recovered_watches += 1

        for stop_leg_id in stop_leg_ids:
            if self._lookup_order_by_id is None:
                break
            order = await self._lookup_safe(
                lambda: self._lookup_order_by_id(stop_leg_id),
                f"stop_leg={stop_leg_id}",
            )
            if order is None:
                continue
            event_val = _STATUS_TO_EVENT.get(self._raw_status(order))
            if event_val is None:
                continue
            if event_val in _FILL_EVENTS:
                await self._on_trade_update(self._make_synthetic_update(order, event_val))
                recovered_stops += 1
            else:
                with self._lock:
                    self._stop_legs.discard(stop_leg_id)

        if recovered_watches or recovered_stops:
            logger.info(
                f"stream manager: gap re-sync recovered "
                f"{recovered_watches} watched order(s), {recovered_stops} stop fill(s)"
            )

    def _snapshot_pending_state(self) -> tuple[list[_TrackedOrder], list[str]]:
        with self._lock:
            tracked = [
                _TrackedOrder(
                    client_order_id=item.client_order_id,
                    event=item.event,
                    order_id=item.order_id,
                    update=item.update,
                    aliases=set(item.aliases),
                )
                for item in self._watched.values()
            ]
            stop_legs = list(self._stop_legs)
        return tracked, stop_legs

    async def _lookup_safe(
        self,
        fn: Callable[[], Any],
        label: str,
    ) -> Any | None:
        try:
            return await asyncio.to_thread(fn)
        except Exception as exc:
            logger.debug(f"stream manager: gap lookup failed for {label}: {exc}")
            return None

    @staticmethod
    def _raw_status(order: Any) -> str:
        status = getattr(order, "status", None)
        return status.value if hasattr(status, "value") else str(status)

    @staticmethod
    def _make_synthetic_update(order: Any, event_val: str) -> Any:
        filled_qty = float(getattr(order, "filled_qty", 0) or 0)
        filled_avg = getattr(order, "filled_avg_price", None)
        avg_price = float(filled_avg) if filled_avg is not None else None
        qty = filled_qty if filled_qty > 0 else float(getattr(order, "qty", 0) or 0)
        return SimpleNamespace(
            event=SimpleNamespace(value=event_val),
            qty=qty,
            price=avg_price,
            order=SimpleNamespace(
                id=str(getattr(order, "id")),
                client_order_id=getattr(order, "client_order_id", None),
                symbol=getattr(order, "symbol"),
                filled_qty=str(filled_qty),
                filled_avg_price=(
                    None if avg_price is None else str(avg_price)
                ),
            ),
        )

    # ── Trade update routing ─────────────────────────────────────────────

    async def _on_trade_update(self, update: Any) -> None:
        order_id = str(update.order.id)
        client_order_id = getattr(update.order, "client_order_id", None)
        client_order_id = None if client_order_id is None else str(client_order_id)
        event_val = (
            update.event.value
            if hasattr(update.event, "value")
            else str(update.event)
        )
        is_terminal = event_val in _TERMINAL_EVENTS

        logger.debug(
            f"trade update: order={order_id} symbol={update.order.symbol} "
            f"event={event_val} qty={update.qty} price={update.price}"
        )

        with self._lock:
            if order_id in self._stop_legs:
                if event_val in _FILL_EVENTS:
                    self._stop_fills.append(update)
                    logger.info(
                        f"stream: stop fill — {update.order.symbol} "
                        f"qty={update.qty} price={update.price}"
                    )
                if is_terminal:
                    self._stop_legs.discard(order_id)

            canonical = self._alias_to_canonical.get(order_id)
            if canonical is None and client_order_id is not None:
                canonical = self._alias_to_canonical.get(client_order_id)
                if canonical is not None:
                    tracked = self._watched.get(canonical)
                    if tracked is not None:
                        tracked.order_id = order_id
                        tracked.aliases.add(order_id)
                        self._alias_to_canonical[order_id] = canonical

            if canonical is not None and is_terminal:
                tracked = self._watched.get(canonical)
                if tracked is not None:
                    tracked.update = update
                    tracked.event.set()
