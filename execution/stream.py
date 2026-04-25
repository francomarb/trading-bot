"""
WebSocket order/fill streaming via Alpaca TradingStream (Phase 10.E1).

StreamManager runs alpaca-py's TradingStream in a background daemon thread.
It provides two integration seams:

  1. **Entry-order fill detection** (used by AlpacaBroker):
       event = stream.watch(order_id)   # register before submitting
       stream.register_stop_leg(leg_id) # register OTO stop child
       event.wait(timeout=30)           # block until terminal update arrives
       update = stream.get_update(order_id)

  2. **Stop-out detection** (used by TradingEngine each cycle):
       fills = stream.drain_stop_fills()  # [{symbol, qty, price, order_id}]

Thread-safety: all mutable state is protected by a single threading.Lock.
The TradingStream async handler acquires the lock only for O(1) dict ops —
holding it briefly enough that the event loop is never starved.

Fallback: if the stream is absent or times out, AlpacaBroker falls back to
REST polling (_poll_until_terminal). The engine's cycle-based external-close
detection (_detect_external_closes) remains the fallback for stop-outs that
arrive between sessions or during stream gaps.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from alpaca.trading.models import TradeUpdate


# TradeEvent values (string form) that mark an order as done.
_TERMINAL_EVENTS: frozenset[str] = frozenset({
    "fill",
    "canceled",
    "expired",
    "replaced",
    "rejected",
})

# Subset of terminal events that represent a genuine fill (position closed).
_FILL_EVENTS: frozenset[str] = frozenset({"fill"})


class StreamManager:
    """
    Wraps alpaca-py TradingStream in a background daemon thread.

    Lifecycle:
        stream = StreamManager(api_key, secret_key, paper=True)
        stream.start()   # idempotent; call before engine loop
        ...
        stream.stop()    # called by engine shutdown

    The instance is safe to create and wire up before start() — all public
    methods work without the stream running (watch/register return immediately,
    drain returns empty list, get_update returns None).
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

        self._lock = threading.Lock()
        # Entry orders waiting for a terminal event: order_id → Event.
        self._watched: dict[str, threading.Event] = {}
        # Latest terminal TradeUpdate per entry order.
        self._updates: dict[str, "TradeUpdate"] = {}
        # Stop-leg order IDs; their fills go to _stop_fills.
        self._stop_legs: set[str] = set()
        # Accumulated stop-leg fills, drained by the engine each cycle.
        self._stop_fills: list["TradeUpdate"] = []

        self._stream = None  # alpaca TradingStream instance
        self._thread: threading.Thread | None = None

    # ── Public API ───────────────────────────────────────────────────────

    def watch(self, order_id: str) -> threading.Event:
        """
        Register an entry order. Returns a threading.Event that fires when
        a terminal TradeUpdate arrives. Call this before submitting the order
        to avoid a race where the fill arrives before watch() is called.
        """
        event = threading.Event()
        with self._lock:
            self._watched[order_id] = event
        return event

    def get_update(self, order_id: str) -> "TradeUpdate | None":
        """Return the latest terminal TradeUpdate for a watched order, or None."""
        with self._lock:
            return self._updates.get(order_id)

    def register_stop_leg(self, order_id: str) -> None:
        """
        Register a stop-leg order ID for separate fill tracking.
        When this order fills, the update is routed to the stop-fills
        accumulator rather than (or in addition to) the watched-order path.
        """
        with self._lock:
            self._stop_legs.add(order_id)

    def drain_stop_fills(self) -> list["TradeUpdate"]:
        """
        Return and clear all accumulated stop-leg fill events.
        The engine calls this each cycle to detect WebSocket-notified stop-outs.
        """
        with self._lock:
            fills = list(self._stop_fills)
            self._stop_fills.clear()
        return fills

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the background streaming thread. Idempotent."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            name="alpaca-stream",
            daemon=True,
        )
        self._thread.start()
        logger.info("stream manager: background thread started")

    def stop(self) -> None:
        """Stop the stream and wait for the background thread to exit."""
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as e:
                logger.warning(f"stream stop error (ignored): {e}")
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        logger.info("stream manager: stopped")

    # ── Internal ─────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Entry point for the daemon thread. Blocks until stream stops."""
        from alpaca.trading.stream import TradingStream

        try:
            self._stream = TradingStream(
                api_key=self._api_key,
                secret_key=self._secret_key,
                paper=self._paper,
            )
            self._stream.subscribe_trade_updates(self._on_trade_update)
            logger.info("stream manager: TradingStream connected")
            self._stream.run()
        except Exception as e:
            logger.error(f"stream manager: unrecoverable error — {e}")

    async def _on_trade_update(self, update: "TradeUpdate") -> None:
        """
        Async handler invoked by TradingStream for every account event.

        Routes updates to two consumers:
          - Entry-order watchers (threading.Event + stored update)
          - Stop-leg fill accumulator (drained by engine each cycle)
        """
        order_id = str(update.order.id)
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
            # Stop-leg fill → accumulate for engine drain.
            if order_id in self._stop_legs and event_val in _FILL_EVENTS:
                self._stop_fills.append(update)
                logger.info(
                    f"stream: stop fill — {update.order.symbol} "
                    f"qty={update.qty} price={update.price}"
                )

            # Watched entry order → fire event on terminal state.
            if order_id in self._watched and is_terminal:
                self._updates[order_id] = update
                self._watched[order_id].set()
