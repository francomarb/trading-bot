"""
Unit tests for execution/stream.py (Phase 11.21).

These tests exercise StreamManager's public API plus its reconnect / gap
recovery logic without opening a real websocket connection.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from execution.stream import (
    StreamHealth,
    StreamManager,
    _FILL_EVENTS,
    _TERMINAL_EVENTS,
)


def _stream() -> StreamManager:
    return StreamManager(api_key="key", secret_key="secret", paper=True)


def _make_update(
    order_id: str,
    symbol: str,
    event: str,
    *,
    qty: float = 10.0,
    price: float = 100.0,
    client_order_id: str | None = None,
):
    return SimpleNamespace(
        order=SimpleNamespace(
            id=order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            filled_qty=str(qty),
            filled_avg_price=str(price),
        ),
        event=SimpleNamespace(value=event),
        qty=qty,
        price=price,
    )


def _make_order(
    *,
    order_id: str,
    client_order_id: str | None = None,
    symbol: str = "AAPL",
    status: str = "filled",
    filled_qty: float = 10.0,
    filled_avg_price: float = 100.5,
    qty: float = 10.0,
):
    return SimpleNamespace(
        id=order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        status=SimpleNamespace(value=status),
        filled_qty=str(filled_qty),
        filled_avg_price=str(filled_avg_price),
        qty=str(qty),
    )


async def _dispatch(sm: StreamManager, update) -> None:
    await sm._on_trade_update(update)


class _LoopTestStream(StreamManager):
    def __init__(self, actions: list[str]):
        super().__init__(api_key="key", secret_key="secret", paper=True)
        self.actions = list(actions)
        self.delays: list[float] = []

    async def _run_session(self) -> None:
        action = self.actions.pop(0)
        if action == "heartbeat_timeout":
            raise TimeoutError("trading websocket heartbeat timeout")
        if action == "disconnect":
            raise RuntimeError("socket dropped")
        if action == "success_stop":
            await self._mark_connected()
            self._thread_stop.set()
            assert self._stop_event is not None
            self._stop_event.set()
            return
        if action == "success_return":
            await self._mark_connected()
            return
        raise AssertionError(f"unknown test action: {action}")

    async def _sleep_with_stop(self, seconds: float) -> None:
        self.delays.append(seconds)


class TestStreamManagerPublicAPI:
    def test_watch_returns_event(self):
        sm = _stream()
        ev = sm.watch("client-1")
        assert isinstance(ev, threading.Event)
        assert not ev.is_set()

    def test_bind_submitted_order_aliases_order_id_to_client_order_id(self):
        sm = _stream()
        ev = sm.watch("client-1")
        sm.bind_submitted_order("client-1", "ord-1")

        update = _make_update(
            "ord-1",
            "AAPL",
            "fill",
            client_order_id="client-1",
        )
        asyncio.run(_dispatch(sm, update))

        assert ev.is_set()
        assert sm.get_update("client-1") is update
        assert sm.get_update("ord-1") is update

    def test_unwatch_clears_aliases_for_client_and_order_id(self):
        sm = _stream()
        sm.watch("client-1")
        sm.bind_submitted_order("client-1", "ord-1")

        sm.unwatch("ord-1")

        assert sm.get_update("client-1") is None
        assert sm.get_update("ord-1") is None
        with sm._lock:
            assert "client-1" not in sm._alias_to_canonical
            assert "ord-1" not in sm._alias_to_canonical

    def test_health_snapshot_starts_disconnected(self):
        sm = _stream()
        health = sm.health_snapshot()
        assert health == StreamHealth(
            connected=False,
            healthy=False,
            generation=0,
            last_rx_at=None,
            last_disconnect_at=None,
            last_reconnect_at=None,
            consecutive_failures=0,
        )


class TestStopLegRouting:
    def test_register_stop_leg_stores(self):
        sm = _stream()
        sm.register_stop_leg("stop-leg-1")
        with sm._lock:
            assert "stop-leg-1" in sm._stop_legs

    def test_stop_leg_fill_accumulates_and_cleans_up_terminal_registration(self):
        sm = _stream()
        sm.register_stop_leg("leg-1")
        update = _make_update("leg-1", "AAPL", "fill", qty=10.0, price=80.0)

        asyncio.run(_dispatch(sm, update))

        fills = sm.drain_stop_fills()
        assert len(fills) == 1
        assert fills[0] is update
        with sm._lock:
            assert "leg-1" not in sm._stop_legs

    def test_non_fill_stop_leg_terminal_event_cleans_up_without_accumulating(self):
        sm = _stream()
        sm.register_stop_leg("leg-1")
        update = _make_update("leg-1", "AAPL", "canceled")

        asyncio.run(_dispatch(sm, update))

        assert sm.drain_stop_fills() == []
        with sm._lock:
            assert "leg-1" not in sm._stop_legs


class TestGapResync:
    def test_resync_recovers_watched_order_that_filled_during_gap(self):
        sm = _stream()
        ev = sm.watch("client-1")
        sm.bind_submitted_order("client-1", "ord-1")
        sm.set_order_lookup_callbacks(
            by_id=lambda order_id: _make_order(
                order_id=order_id,
                client_order_id="client-1",
                status="filled",
            ),
            by_client_id=lambda client_id: None,
        )

        asyncio.run(sm._resync_tracked_state())

        assert ev.is_set()
        update = sm.get_update("ord-1")
        assert update is not None
        assert update.event.value == "fill"
        assert update.order.client_order_id == "client-1"

    def test_resync_recovers_stop_fill_during_gap(self):
        sm = _stream()
        sm.register_stop_leg("stop-1")
        sm.set_order_lookup_callbacks(
            by_id=lambda order_id: _make_order(
                order_id=order_id,
                symbol="AAPL",
                status="filled",
                filled_qty=10,
                filled_avg_price=95.0,
            ),
            by_client_id=lambda client_id: None,
        )

        asyncio.run(sm._resync_tracked_state())

        fills = sm.drain_stop_fills()
        assert len(fills) == 1
        assert fills[0].order.id == "stop-1"
        assert fills[0].price == 95.0


class TestReconnectLoop:
    def test_heartbeat_timeout_forces_reconnect(self, monkeypatch):
        monkeypatch.setattr("execution.stream.random.uniform", lambda *_: 0.0)
        sm = _LoopTestStream(["heartbeat_timeout", "success_stop"])

        asyncio.run(sm._run_async())

        assert sm.delays == [1.0]
        assert sm.health_snapshot().healthy is True
        assert sm.health_snapshot().generation == 1

    def test_websocket_error_uses_exponential_backoff(self, monkeypatch):
        monkeypatch.setattr("execution.stream.random.uniform", lambda *_: 0.0)
        sm = _LoopTestStream(["disconnect", "disconnect", "success_stop"])

        asyncio.run(sm._run_async())

        assert sm.delays == [1.0, 2.0]
        assert sm.health_snapshot().healthy is True

    def test_backoff_resets_after_successful_reconnect(self, monkeypatch):
        monkeypatch.setattr("execution.stream.random.uniform", lambda *_: 0.0)
        sm = _LoopTestStream(["disconnect", "success_return", "success_stop"])

        asyncio.run(sm._run_async())

        assert sm.delays[:2] == [1.0, 1.0]


class TestRecvLoopDefensiveBehavior:
    def test_recv_loop_ignores_idle_timeout(self):
        sm = _stream()

        class _FakeWS:
            def __init__(self):
                self.calls = 0

            async def recv(self):
                self.calls += 1
                if self.calls == 1:
                    raise asyncio.TimeoutError()
                sm._thread_stop.set()
                return "{\"stream\": \"status\"}"

        sm._ws = _FakeWS()

        asyncio.run(sm._recv_loop())

        assert sm._ws.calls == 2

    def test_dispatch_message_ignores_trade_update_without_data(self):
        sm = _stream()

        asyncio.run(sm._dispatch_message({"stream": "trade_updates"}))

        assert sm.drain_stop_fills() == []


class TestStreamManagerLifecycle:
    def test_stop_before_start_does_not_raise(self):
        sm = _stream()
        sm.stop()

    def test_double_start_is_idempotent(self):
        sm = _stream()
        ready = threading.Event()

        def _slow_run():
            ready.set()
            threading.Event().wait(timeout=2)

        with patch.object(sm, "_run_loop", side_effect=_slow_run):
            sm.start()
            ready.wait(timeout=1)
            thread1 = sm._thread
            sm.start()
            assert sm._thread is thread1
            sm.stop()

    def test_start_creates_daemon_thread(self):
        sm = _stream()
        ready = threading.Event()

        def _slow_run():
            ready.set()
            threading.Event().wait(timeout=1)

        with patch.object(sm, "_run_loop", side_effect=_slow_run):
            sm.start()
            ready.wait(timeout=1)
            assert sm._thread is not None
            assert sm._thread.daemon is True
            sm.stop()


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


# ── MLEG combo-payload handling (11.28) ─────────────────────────────────────


class TestMlegPayloadHandling:
    _SHORT = "SPY260620P00580000"
    _LONG = "SPY260620P00570000"

    def test_make_update_from_payload_extracts_leg_symbols(self):
        data = {
            "event": "fill",
            "qty": "1",
            "price": "3.25",
            "order": {
                "id": "combo-1",
                "client_order_id": "spr-credit_spread-abc",
                "symbol": None,  # MLEG parent — null top-level symbol
                "filled_qty": "1",
                "filled_avg_price": "3.25",
                "legs": [{"symbol": self._SHORT}, {"symbol": self._LONG}],
            },
        }
        update = StreamManager._make_update_from_payload(data)
        assert update.is_combo is True
        assert update.leg_symbols == [self._SHORT, self._LONG]
        # Null parent symbol falls back to the first leg.
        assert update.order.symbol == self._SHORT
        assert update.event.value == "fill"
        assert update.qty == 1.0
        assert update.price == 3.25

    def test_make_update_from_payload_single_leg_is_not_combo(self):
        data = {
            "event": "fill",
            "qty": "10",
            "price": "100.5",
            "order": {
                "id": "ord-1",
                "client_order_id": None,
                "symbol": "AAPL",
                "filled_qty": "10",
                "filled_avg_price": "100.5",
            },
        }
        update = StreamManager._make_update_from_payload(data)
        assert update.is_combo is False
        assert update.leg_symbols == []
        assert update.order.symbol == "AAPL"

    def test_make_synthetic_update_handles_mleg_order_object(self):
        order = SimpleNamespace(
            id="combo-2",
            client_order_id="spr-credit_spread-xyz",
            symbol=None,
            filled_qty="1",
            filled_avg_price="2.80",
            qty="1",
            legs=[
                SimpleNamespace(symbol=self._SHORT),
                SimpleNamespace(symbol=self._LONG),
            ],
        )
        update = StreamManager._make_synthetic_update(order, "fill")
        assert update.is_combo is True
        assert update.leg_symbols == [self._SHORT, self._LONG]
        assert update.order.symbol == self._SHORT
        assert update.order.id == "combo-2"

    def test_leg_symbols_empty_for_plain_order(self):
        assert StreamManager._leg_symbols({"id": "x"}) == []
        assert StreamManager._leg_symbols(SimpleNamespace(id="x")) == []

    def test_mleg_parent_terminal_event_sets_watched_event(self):
        """A bound MLEG parent order's fill event must release its watcher."""
        sm = _stream()
        client_id = "spr-credit_spread-abc"
        event = sm.watch(client_id)
        sm.bind_submitted_order(
            client_order_id=client_id, order_id="combo-1", stop_leg_ids=[]
        )
        update = StreamManager._make_update_from_payload({
            "event": "fill",
            "qty": "1",
            "price": "3.25",
            "order": {
                "id": "combo-1",
                "client_order_id": client_id,
                "symbol": None,
                "filled_qty": "1",
                "filled_avg_price": "3.25",
                "legs": [{"symbol": self._SHORT}, {"symbol": self._LONG}],
            },
        })
        asyncio.run(sm._on_trade_update(update))
        assert event.is_set()
        assert sm.get_update("combo-1") is not None
