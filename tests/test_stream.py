"""
Unit tests for execution/stream.py (Phase 10.E1).

StreamManager uses TradingStream internally, but all tests here exercise the
public API and the _on_trade_update handler directly — no real WebSocket.
The handler is async and is called via asyncio.run() in tests.
"""

from __future__ import annotations

import asyncio
import threading
import types
import uuid
from unittest.mock import MagicMock, patch

import pytest

from execution.stream import StreamManager, _TERMINAL_EVENTS, _FILL_EVENTS


# ── Helpers ──────────────────────────────────────────────────────────────────


def _make_update(
    order_id: str,
    symbol: str,
    event: str,
    qty: float = 10.0,
    price: float = 100.0,
) -> MagicMock:
    """Return a mock TradeUpdate with the given fields."""
    update = MagicMock()
    update.order.id = order_id
    update.order.symbol = symbol
    update.event.value = event
    # event also needs to respond to hasattr(event, 'value') check
    update.event = MagicMock()
    update.event.value = event
    update.qty = qty
    update.price = price
    return update


def _stream() -> StreamManager:
    return StreamManager(api_key="key", secret_key="secret", paper=True)


async def _dispatch(sm: StreamManager, update) -> None:
    """Call the handler directly (no real WebSocket needed)."""
    await sm._on_trade_update(update)


# ── TestStreamManagerPublicAPI ────────────────────────────────────────────────


class TestStreamManagerPublicAPI:
    def test_watch_returns_event(self):
        sm = _stream()
        ev = sm.watch("order-1")
        assert isinstance(ev, threading.Event)
        assert not ev.is_set()

    def test_get_update_none_before_fire(self):
        sm = _stream()
        sm.watch("order-1")
        assert sm.get_update("order-1") is None

    def test_get_update_returns_none_for_unknown(self):
        sm = _stream()
        assert sm.get_update("nonexistent") is None

    def test_register_stop_leg_stores(self):
        sm = _stream()
        sm.register_stop_leg("stop-leg-1")
        with sm._lock:
            assert "stop-leg-1" in sm._stop_legs

    def test_drain_stop_fills_empty(self):
        sm = _stream()
        assert sm.drain_stop_fills() == []

    def test_drain_stop_fills_clears_accumulator(self):
        sm = _stream()
        sm.register_stop_leg("leg-1")
        update = _make_update("leg-1", "AAPL", "fill")
        asyncio.run(_dispatch(sm, update))

        fills = sm.drain_stop_fills()
        assert len(fills) == 1
        # Second drain should be empty.
        assert sm.drain_stop_fills() == []

    def test_watch_multiple_orders(self):
        sm = _stream()
        ev1 = sm.watch("order-1")
        ev2 = sm.watch("order-2")
        assert ev1 is not ev2


# ── TestOnTradeUpdate ─────────────────────────────────────────────────────────


class TestOnTradeUpdate:
    def test_fill_fires_watched_event(self):
        sm = _stream()
        ev = sm.watch("order-1")
        update = _make_update("order-1", "AAPL", "fill")
        asyncio.run(_dispatch(sm, update))
        assert ev.is_set()

    def test_canceled_fires_watched_event(self):
        sm = _stream()
        ev = sm.watch("order-1")
        update = _make_update("order-1", "AAPL", "canceled")
        asyncio.run(_dispatch(sm, update))
        assert ev.is_set()

    def test_rejected_fires_watched_event(self):
        sm = _stream()
        ev = sm.watch("order-1")
        update = _make_update("order-1", "AAPL", "rejected")
        asyncio.run(_dispatch(sm, update))
        assert ev.is_set()

    def test_expired_fires_watched_event(self):
        sm = _stream()
        ev = sm.watch("order-1")
        update = _make_update("order-1", "AAPL", "expired")
        asyncio.run(_dispatch(sm, update))
        assert ev.is_set()

    def test_replaced_fires_watched_event(self):
        sm = _stream()
        ev = sm.watch("order-1")
        update = _make_update("order-1", "AAPL", "replaced")
        asyncio.run(_dispatch(sm, update))
        assert ev.is_set()

    def test_non_terminal_event_does_not_fire(self):
        sm = _stream()
        ev = sm.watch("order-1")
        for event in ("new", "pending_new", "partial_fill", "accepted"):
            update = _make_update("order-1", "AAPL", event)
            asyncio.run(_dispatch(sm, update))
        assert not ev.is_set()

    def test_fill_stores_update(self):
        sm = _stream()
        sm.watch("order-1")
        update = _make_update("order-1", "AAPL", "fill", qty=5.0, price=150.0)
        asyncio.run(_dispatch(sm, update))
        stored = sm.get_update("order-1")
        assert stored is update

    def test_unwatched_order_does_not_error(self):
        sm = _stream()
        update = _make_update("order-99", "AAPL", "fill")
        asyncio.run(_dispatch(sm, update))  # must not raise
        assert sm.get_update("order-99") is None

    def test_stop_leg_fill_accumulates(self):
        sm = _stream()
        sm.register_stop_leg("leg-1")
        update = _make_update("leg-1", "AAPL", "fill", qty=10.0, price=80.0)
        asyncio.run(_dispatch(sm, update))
        fills = sm.drain_stop_fills()
        assert len(fills) == 1
        assert fills[0] is update

    def test_stop_leg_non_fill_event_not_accumulated(self):
        sm = _stream()
        sm.register_stop_leg("leg-1")
        for event in ("canceled", "rejected", "new", "partial_fill"):
            update = _make_update("leg-1", "AAPL", event)
            asyncio.run(_dispatch(sm, update))
        assert sm.drain_stop_fills() == []

    def test_stop_leg_fill_also_fires_if_watched(self):
        """A stop leg registered as both a stop leg AND watched fires the event."""
        sm = _stream()
        ev = sm.watch("leg-1")
        sm.register_stop_leg("leg-1")
        update = _make_update("leg-1", "AAPL", "fill")
        asyncio.run(_dispatch(sm, update))
        assert ev.is_set()
        assert len(sm.drain_stop_fills()) == 1

    def test_multiple_stop_fills_accumulate(self):
        sm = _stream()
        sm.register_stop_leg("leg-1")
        sm.register_stop_leg("leg-2")
        for leg, sym in [("leg-1", "AAPL"), ("leg-2", "GOOG")]:
            asyncio.run(_dispatch(sm, _make_update(leg, sym, "fill")))
        fills = sm.drain_stop_fills()
        assert len(fills) == 2

    def test_update_for_order_not_watched_goes_to_stop_fills(self):
        """Stop leg fill doesn't require the order to be in _watched."""
        sm = _stream()
        sm.register_stop_leg("leg-only")
        update = _make_update("leg-only", "MU", "fill")
        asyncio.run(_dispatch(sm, update))
        fills = sm.drain_stop_fills()
        assert len(fills) == 1


# ── TestStreamManagerLifecycle ────────────────────────────────────────────────


class TestStreamManagerLifecycle:
    def test_stop_before_start_does_not_raise(self):
        sm = _stream()
        sm.stop()  # must not raise

    def test_double_start_is_idempotent(self):
        """start() while the thread is alive should not spawn a second thread."""
        sm = _stream()
        ready = threading.Event()

        def _slow_run():
            ready.set()
            threading.Event().wait(timeout=2)

        with patch.object(sm, "_run_loop", side_effect=_slow_run):
            sm.start()
            ready.wait(timeout=1)  # ensure thread is alive
            thread1 = sm._thread
            sm.start()
            assert sm._thread is thread1
            sm.stop()

    def test_start_creates_daemon_thread(self):
        sm = _stream()
        with patch("alpaca.trading.stream.TradingStream") as MockStream:
            MockStream.return_value.run.side_effect = lambda: None
            sm.start()
            assert sm._thread is not None
            assert sm._thread.daemon is True
            sm.stop()


# ── TestTerminalEventsConstants ───────────────────────────────────────────────


class TestTerminalEventsConstants:
    def test_terminal_events_include_expected(self):
        assert "fill" in _TERMINAL_EVENTS
        assert "canceled" in _TERMINAL_EVENTS
        assert "expired" in _TERMINAL_EVENTS
        assert "replaced" in _TERMINAL_EVENTS
        assert "rejected" in _TERMINAL_EVENTS

    def test_non_terminal_not_in_set(self):
        assert "new" not in _TERMINAL_EVENTS
        assert "partial_fill" not in _TERMINAL_EVENTS
        assert "pending_new" not in _TERMINAL_EVENTS

    def test_fill_events_subset_of_terminal(self):
        assert _FILL_EVENTS.issubset(_TERMINAL_EVENTS)
