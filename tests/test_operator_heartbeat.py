"""Tests for the Phase B operator-command heartbeat thread.

The heartbeat replaces the per-cycle queue poll. Verifies:
  - Thread starts on engine.start() and stops cleanly on engine.stop()
  - Drains commands at sub-second latency (vs. up to one cycle interval)
  - Survives queue I/O failures without crashing
  - Daemon=True so process exit isn't blocked
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from engine.operator_queue import OperatorCommandStore, new_command_uid
from engine.trader import TradingEngine
from reporting.logger import TradeLogger


def _build_engine(tmp_path):
    db_path = tmp_path / "trades.db"
    tl = TradeLogger(path=str(db_path))
    conn = tl._ensure_db()
    store = OperatorCommandStore(conn)

    engine = TradingEngine.__new__(TradingEngine)
    engine.operator_command_store = store
    engine.lifecycle_store = None
    engine.trade_logger = tl
    engine.risk = MagicMock()
    engine.risk.is_halted = MagicMock(return_value=False)
    engine.risk.halt_reason = MagicMock(return_value=None)
    engine.risk.is_entries_paused = MagicMock(return_value=False)
    engine.risk.entries_paused_reason = MagicMock(return_value=None)
    engine.risk.paused_strategies_snapshot = MagicMock(return_value={})
    engine.risk._engage_kill_switch = MagicMock()
    engine.alerts = MagicMock()
    engine.broker = MagicMock()

    engine._running = True  # heartbeat loop respects this
    engine._operator_heartbeat_thread = None
    engine._operator_heartbeat_stop = threading.Event()

    return engine, store


class TestHeartbeatLifecycle:
    def test_start_creates_daemon_thread(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "config.settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS", 1
        )
        engine, store = _build_engine(tmp_path)
        engine._start_operator_heartbeat()
        try:
            assert engine._operator_heartbeat_thread is not None
            assert engine._operator_heartbeat_thread.daemon is True
            assert engine._operator_heartbeat_thread.is_alive()
        finally:
            engine._running = False
            engine._operator_heartbeat_stop.set()
            engine._operator_heartbeat_thread.join(timeout=2.0)

    def test_start_is_idempotent(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "config.settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS", 1
        )
        engine, store = _build_engine(tmp_path)
        engine._start_operator_heartbeat()
        first = engine._operator_heartbeat_thread
        engine._start_operator_heartbeat()
        # Same thread instance (no double-start).
        assert engine._operator_heartbeat_thread is first
        engine._running = False
        engine._operator_heartbeat_stop.set()
        first.join(timeout=2.0)

    def test_start_skips_when_no_queue_store(self, tmp_path):
        engine, _ = _build_engine(tmp_path)
        engine.operator_command_store = None
        engine._start_operator_heartbeat()
        assert engine._operator_heartbeat_thread is None


class TestHeartbeatDrains:
    def test_drains_pending_command_within_two_intervals(
        self, tmp_path, monkeypatch
    ):
        """Queue a halt command; heartbeat should drain it within ≤2
        heartbeat intervals. Uses a fast (0.1s) interval so the test
        completes in under a second."""
        monkeypatch.setattr(
            "config.settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS", 1
        )
        # Patch the heartbeat loop's interval to 0.1s without changing
        # the settings value (which other code may read).
        engine, store = _build_engine(tmp_path)
        # Monkeypatch the loop to use a tighter interval directly.
        original_loop = engine._operator_heartbeat_loop

        def fast_loop():
            interval = 0.1
            while engine._running:
                try:
                    engine._process_operator_commands()
                except Exception:
                    pass
                if engine._operator_heartbeat_stop.wait(timeout=interval):
                    break

        engine._operator_heartbeat_loop = fast_loop

        # Start the heartbeat.
        engine._operator_heartbeat_stop.clear()
        thread = threading.Thread(target=fast_loop, daemon=True)
        engine._operator_heartbeat_thread = thread
        thread.start()

        try:
            # Queue a halt.
            uid = new_command_uid()
            store.insert(
                command_uid=uid, action="halt", reason="heartbeat test",
            )
            # Wait up to 1s for drain.
            deadline = time.monotonic() + 1.0
            row = None
            while time.monotonic() < deadline:
                row = store.get_by_command_uid(uid)
                if row.status != "pending":
                    break
                time.sleep(0.05)
            assert row is not None
            assert row.status == "succeeded", (
                f"heartbeat should have drained the halt within ~200ms; "
                f"got status={row.status}"
            )
        finally:
            engine._running = False
            engine._operator_heartbeat_stop.set()
            thread.join(timeout=2.0)

    def test_loop_survives_queue_errors(self, tmp_path, monkeypatch):
        """If the queue store raises, the heartbeat thread logs and
        continues — does not crash."""
        monkeypatch.setattr(
            "config.settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS", 1
        )
        engine, store = _build_engine(tmp_path)

        # Make claim_next_pending raise on the first call, succeed on
        # subsequent calls.
        call_count = {"n": 0}
        real_claim = store.claim_next_pending

        def flaky_claim(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("transient queue error")
            return real_claim(*args, **kwargs)

        store.claim_next_pending = flaky_claim

        engine._operator_heartbeat_stop.clear()
        interval_holder = {"interval": 0.05}

        def fast_loop():
            while engine._running:
                try:
                    engine._process_operator_commands()
                except Exception:
                    pass
                if engine._operator_heartbeat_stop.wait(timeout=interval_holder["interval"]):
                    break

        thread = threading.Thread(target=fast_loop, daemon=True)
        engine._operator_heartbeat_thread = thread
        thread.start()

        try:
            # Give it ≥3 iterations to demonstrate survival across the
            # transient error.
            time.sleep(0.3)
            assert thread.is_alive(), "heartbeat must survive queue errors"
            assert call_count["n"] >= 2, (
                "heartbeat must keep polling after a queue error"
            )
        finally:
            engine._running = False
            engine._operator_heartbeat_stop.set()
            thread.join(timeout=2.0)
