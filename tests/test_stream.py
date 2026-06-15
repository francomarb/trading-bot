"""
Unit tests for execution/stream.py (Phase 11.21).

These tests exercise StreamManager's public API plus its reconnect / gap
recovery logic without opening a real websocket connection.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close

from execution.stream import (
    StreamHealth,
    StreamManager,
    _FILL_EVENTS,
    _TERMINAL_EVENTS,
    _utcnow,
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

    def test_option_stop_audit_associates_old_client_and_new_order_ids(self):
        sm = _stream()
        expires_at = _utcnow() + timedelta(minutes=5)
        sm.register_option_stop_audit(
            correlation_id="corr-1",
            strategy="spy_options_reversion",
            occ_symbol="SPY260702C00724000",
            aliases={"old-stop", "replacement-client"},
            expires_at=expires_at,
        )

        asyncio.run(_dispatch(
            sm,
            _make_update(
                "old-stop",
                "SPY260702C00724000",
                "replaced",
            ),
        ))
        asyncio.run(_dispatch(
            sm,
            _make_update(
                "new-stop",
                "SPY260702C00724000",
                "fill",
                client_order_id="replacement-client",
                qty=2,
                price=23.0,
            ),
        ))

        events = sm.drain_option_stop_audit_events()
        assert [event["record_type"] for event in events] == [
            "stream_replaced",
            "stream_fill",
        ]
        assert {event["order_id"] for event in events} == {
            "old-stop",
            "new-stop",
        }

        asyncio.run(_dispatch(
            sm,
            _make_update(
                "new-stop",
                "SPY260702C00724000",
                "canceled",
                client_order_id="replacement-client",
            ),
        ))
        assert sm.drain_option_stop_audit_events() == []

    def test_option_stop_audit_ignores_events_after_bounded_window(self):
        sm = _stream()
        sm.register_option_stop_audit(
            correlation_id="corr-expired",
            strategy="spy_options_reversion",
            occ_symbol="SPY260702C00724000",
            aliases={"old-stop"},
            expires_at=_utcnow() - timedelta(seconds=1),
        )

        asyncio.run(_dispatch(
            sm,
            _make_update(
                "old-stop",
                "SPY260702C00724000",
                "fill",
            ),
        ))

        assert sm.drain_option_stop_audit_events() == []

    @pytest.mark.parametrize("terminal_event", ["canceled", "rejected", "expired"])
    def test_option_stop_audit_releases_watch_on_terminal_failure(
        self, terminal_event
    ):
        sm = _stream()
        sm.register_option_stop_audit(
            correlation_id="corr-terminal",
            strategy="spy_options_reversion",
            occ_symbol="SPY260702C00724000",
            aliases={"replacement-client"},
            expires_at=_utcnow() + timedelta(minutes=5),
        )

        asyncio.run(_dispatch(
            sm,
            _make_update(
                "new-stop",
                "SPY260702C00724000",
                terminal_event,
                client_order_id="replacement-client",
            ),
        ))
        first = sm.drain_option_stop_audit_events()
        assert first[0]["record_type"] == f"stream_{terminal_event}"

        asyncio.run(_dispatch(
            sm,
            _make_update(
                "new-stop",
                "SPY260702C00724000",
                "fill",
                client_order_id="replacement-client",
            ),
        ))
        assert sm.drain_option_stop_audit_events() == []

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

    def test_unregister_stop_leg_discards_superseded_order(self):
        sm = _stream()
        sm.register_stop_leg("old-stop")

        sm.unregister_stop_leg("old-stop")

        with sm._lock:
            assert "old-stop" not in sm._stop_legs

    def test_sync_stop_legs_replaces_stale_registrations(self):
        sm = _stream()
        sm.register_stop_leg("old-stop")
        sm.sync_stop_legs({"new-stop-1", "new-stop-2"})

        with sm._lock:
            assert sm._stop_legs == {"new-stop-1", "new-stop-2"}

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


class TestDisconnectObservability:
    def test_disconnect_reason_buckets_known_cases(self):
        assert StreamManager._disconnect_reason(
            TimeoutError("trading websocket heartbeat timeout")
        ) == "heartbeat_timeout"
        assert StreamManager._disconnect_reason(
            RuntimeError("timed out during opening handshake")
        ) == "connect_timeout"
        assert StreamManager._disconnect_reason(
            RuntimeError("Connection reset by peer")
        ) == "connection_reset"

    def test_disconnect_reason_uses_cause_chain_for_network_errors(self):
        exc = RuntimeError("stream failed")
        exc.__cause__ = OSError(65, "No route to host")

        assert StreamManager._disconnect_reason(exc) == "network_unreachable"

    def test_mark_disconnected_logs_reason_and_timing_context(self, monkeypatch):
        sm = _stream()
        now = _utcnow()
        with sm._lock:
            sm._health = StreamHealth(
                connected=True,
                healthy=True,
                generation=3,
                last_rx_at=now,
                last_disconnect_at=None,
                last_reconnect_at=now,
                consecutive_failures=0,
            )
        warning = MagicMock()
        monkeypatch.setattr("execution.stream.logger.warning", warning)

        asyncio.run(sm._mark_disconnected(TimeoutError("trading websocket heartbeat timeout")))

        msg = warning.call_args.args[0]
        assert "reason=heartbeat_timeout" in msg
        assert "last_rx_age=" in msg
        assert "connected_for=" in msg
        assert "likely=" not in msg

    def test_mark_disconnected_logs_close_codes_and_causes(self, monkeypatch):
        sm = _stream()
        warning = MagicMock()
        monkeypatch.setattr("execution.stream.logger.warning", warning)
        exc = ConnectionClosedError(None, Close(1001, "going away"))
        exc.__cause__ = OSError(8, "nodename nor servname provided, or not known")

        asyncio.run(sm._mark_disconnected(exc))

        msg = warning.call_args.args[0]
        assert "reason=dns_failure" in msg
        assert "close_sent=1001:going away" in msg
        assert "errno=8" in msg
        assert (
            "caused_by=OSError: [Errno 8] nodename nor servname provided, or not known"
            in msg
        )

    def test_mark_connected_logs_downtime_after_disconnect(self, monkeypatch):
        sm = _stream()
        now = _utcnow()
        with sm._lock:
            sm._health = StreamHealth(
                connected=False,
                healthy=False,
                generation=2,
                last_rx_at=now,
                last_disconnect_at=now,
                last_reconnect_at=None,
                consecutive_failures=1,
            )
        info = MagicMock()
        monkeypatch.setattr("execution.stream.logger.info", info)

        asyncio.run(sm._mark_connected())

        msg = info.call_args.args[0]
        assert "healthy (generation=3" in msg
        assert "downtime=" in msg

    def test_dispatch_message_logs_server_error_payload(self, monkeypatch):
        sm = _stream()
        warning = MagicMock()
        monkeypatch.setattr("execution.stream.logger.warning", warning)

        asyncio.run(
            sm._dispatch_message(
                {"action": "error", "data": {"error_message": "internal server error"}}
            )
        )

        assert (
            warning.call_args.args[0]
            == "stream manager: server error before disconnect (internal server error)"
        )


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

    def test_make_update_from_payload_preserves_stop_price(self):
        """Slippage unification Phase 1 hotfix — the order
        SimpleNamespace must surface stop_price so log_stop_fill can
        benchmark against the broker's actual stop trigger. Without
        this, stop fills silently write 'unavailable' on the new
        slippage taxonomy columns and the load-bearing design fix
        dies on the wire. Real-world evidence on 2026-06-09: QCOM
        and SMCI WebSocket stop fills both wrote 'unavailable'."""
        data = {
            "event": "fill",
            "qty": "10",
            "price": "144.50",
            "order": {
                "id": "stop-1",
                "client_order_id": None,
                "symbol": "AAPL",
                "filled_qty": "10",
                "filled_avg_price": "144.50",
                "stop_price": "145.00",
            },
        }
        update = StreamManager._make_update_from_payload(data)
        assert update.order.stop_price == "145.00"

    def test_make_update_from_payload_missing_stop_price_yields_none(self):
        """A trade update for a non-stop order (e.g. plain market entry)
        carries no stop_price field. The reconstructor must surface
        None rather than raising AttributeError."""
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
        assert update.order.stop_price is None

    def test_make_synthetic_update_preserves_stop_price(self):
        """Gap-resync path also rebuilds the SimpleNamespace from a
        broker Order object. Must surface stop_price the same way."""
        order = SimpleNamespace(
            id="stop-2",
            client_order_id=None,
            symbol="AAPL",
            filled_qty="10",
            filled_avg_price="144.50",
            qty="10",
            stop_price="145.00",
        )
        update = StreamManager._make_synthetic_update(order, "fill")
        assert update.order.stop_price == "145.00"

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


# ── P-1: WebSocket → apply_order_event queue ───────────────────────────────


class TestLifecycleEventQueue:
    """P-1: every material Alpaca trade_update gets translated to an
    OrderEvent and enqueued for the engine drain. Non-material events
    (pending_*, suspended) are silently skipped."""

    def test_fill_event_enqueues_filled_order_event(self):
        from engine.lifecycle_orders import OrderEvent
        sm = StreamManager(api_key="k", secret_key="s", paper=True)
        update = _make_update(
            "ord-fill-1", "AAPL", "fill",
            qty=10.0, price=100.5,
        )
        asyncio.run(sm._on_trade_update(update))
        events = sm.drain_lifecycle_events()
        assert len(events) == 1
        ev = events[0]
        assert isinstance(ev, OrderEvent)
        assert ev.order_id == "ord-fill-1"
        assert ev.status == "filled"
        assert ev.filled_qty == 10.0
        assert ev.avg_fill_price == 100.5

    def test_partial_fill_enqueues_partially_filled_status(self):
        sm = StreamManager(api_key="k", secret_key="s", paper=True)
        update = _make_update(
            "ord-pf-1", "AAPL", "partial_fill",
            qty=5.0, price=100.0,
        )
        asyncio.run(sm._on_trade_update(update))
        events = sm.drain_lifecycle_events()
        assert len(events) == 1
        assert events[0].status == "partially_filled"
        assert events[0].filled_qty == 5.0

    def test_canceled_event_enqueues_canceled_status(self):
        sm = StreamManager(api_key="k", secret_key="s", paper=True)
        update = _make_update(
            "ord-x-1", "AAPL", "canceled", qty=0.0, price=0.0,
        )
        asyncio.run(sm._on_trade_update(update))
        events = sm.drain_lifecycle_events()
        assert len(events) == 1
        assert events[0].status == "canceled"

    def test_replaced_event_maps_to_canceled(self):
        """Alpaca's 'replaced' terminates the OLD order; the
        replacement has its own order_id and event stream."""
        sm = StreamManager(api_key="k", secret_key="s", paper=True)
        update = _make_update(
            "ord-old", "AAPL", "replaced", qty=0.0, price=0.0,
        )
        asyncio.run(sm._on_trade_update(update))
        events = sm.drain_lifecycle_events()
        assert len(events) == 1
        assert events[0].status == "canceled"

    def test_non_material_event_skipped(self):
        """pending_cancel / suspended / etc. don't advance the
        substrate state machine — drop them."""
        sm = StreamManager(api_key="k", secret_key="s", paper=True)
        update = _make_update(
            "ord-pc-1", "AAPL", "pending_cancel",
        )
        asyncio.run(sm._on_trade_update(update))
        assert sm.drain_lifecycle_events() == []

    def test_drain_clears_the_queue(self):
        sm = StreamManager(api_key="k", secret_key="s", paper=True)
        asyncio.run(sm._on_trade_update(
            _make_update("ord-1", "AAPL", "fill", qty=10.0, price=100.0)
        ))
        first = sm.drain_lifecycle_events()
        assert len(first) == 1
        # Second drain is empty.
        assert sm.drain_lifecycle_events() == []

    def test_multiple_events_preserve_order(self):
        sm = StreamManager(api_key="k", secret_key="s", paper=True)
        asyncio.run(sm._on_trade_update(
            _make_update("ord-a", "AAPL", "fill", qty=10.0, price=100.0)
        ))
        asyncio.run(sm._on_trade_update(
            _make_update("ord-b", "AAPL", "canceled", qty=0.0, price=0.0)
        ))
        events = sm.drain_lifecycle_events()
        assert [e.order_id for e in events] == ["ord-a", "ord-b"]
        assert [e.status for e in events] == ["filled", "canceled"]
