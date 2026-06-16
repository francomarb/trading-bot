"""Integration tests for the 4 Phase B soft-control handlers.

Mirrors the structure of `tests/test_operator_halt.py`: builds a
hand-constructed TradingEngine with mocked broker/risk/alerts and
exercises the per-handler engine paths directly.

Covers:
  - pause-entries flips RiskManager flag, persists JSON, marks succeeded
  - resume-entries clears the flag without reconciliation
  - pause-strategy carries target_strategy through; rejects when missing
  - resume-strategy mirrors pause-strategy
  - idempotency: re-pausing returns already_paused=True, status succeeded
  - sticky pauses restore across restart via `_restore_control_state`
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from engine.operator_queue import OperatorCommandStore, new_command_uid
from engine.trader import TradingEngine
from reporting.logger import TradeLogger
from risk.manager import RiskManager


def _build_engine(tmp_path):
    """Build an engine wired with a REAL RiskManager (so pause flags
    behave correctly) + mocked broker/alerts. Tests for halt use a
    MagicMock risk because halt has more side-effect paths; pause
    handlers are simpler and benefit from real state."""
    db_path = tmp_path / "trades.db"
    state_path = tmp_path / "operator_control_state.json"

    tl = TradeLogger(path=str(db_path))
    conn = tl._ensure_db()
    store = OperatorCommandStore(conn)

    engine = TradingEngine.__new__(TradingEngine)
    engine.operator_command_store = store
    engine.lifecycle_store = None
    engine.trade_logger = tl
    engine.risk = RiskManager()
    engine.alerts = MagicMock()
    engine.broker = MagicMock()

    return engine, store, str(state_path)


class TestPauseEntries:
    def test_pause_entries_sets_flag_and_persists(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="pause-entries", reason="market event",
            requested_by="franco",
        )
        engine._process_operator_commands()

        # RiskManager flag set.
        assert engine.risk.is_entries_paused() is True
        assert engine.risk.entries_paused_reason() == "market event"

        # Queue row terminal-state.
        row = store.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["entries_paused"] is True
        assert row.result["already_paused"] is False

        # Persisted JSON contains the new flag.
        import os
        assert os.path.exists(state_path)
        with open(state_path) as fh:
            persisted = json.load(fh)
        assert persisted["entries_paused"] is True
        assert persisted["entries_paused_reason"] == "market event"
        assert persisted["entries_paused_command_uid"] == uid

    def test_resume_entries_clears_flag(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        # Pre-pause so we have something to resume.
        engine.risk.pause_entries(reason="prior", command_uid="cmd_prior")
        engine._persist_control_state()
        import os
        assert os.path.exists(state_path)

        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="resume-entries", reason="event passed",
        )
        engine._process_operator_commands()

        assert engine.risk.is_entries_paused() is False
        row = store.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["entries_paused"] is False
        # File is removed when no flag remains.
        assert not os.path.exists(state_path)

    def test_re_pausing_is_idempotent(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        engine.risk.pause_entries(reason="already", command_uid="cmd_x")

        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="pause-entries", reason="again",
        )
        engine._process_operator_commands()

        row = store.get_by_command_uid(uid)
        # End-state met; command succeeded but already_paused=True.
        assert row.status == "succeeded"
        assert row.result["already_paused"] is True


class TestPauseStrategy:
    def test_pause_strategy_carries_target_through(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="pause-strategy",
            reason="regime concern",
            target_strategy="donchian_breakout",
            requested_by="franco",
        )
        engine._process_operator_commands()

        assert engine.risk.is_strategy_paused("donchian_breakout") is True
        # Other strategies untouched.
        assert engine.risk.is_strategy_paused("sma_crossover") is False

        row = store.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["strategy"] == "donchian_breakout"
        assert row.result["paused"] is True

        # Persisted JSON.
        with open(state_path) as fh:
            persisted = json.load(fh)
        assert "donchian_breakout" in persisted["paused_strategies"]

    def test_pause_strategy_without_target_rejects(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="pause-strategy", reason="test",
        )  # no target_strategy
        engine._process_operator_commands()

        row = store.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "target_strategy" in (row.result.get("note") or "")
        assert engine.risk.paused_strategies_snapshot() == {}

    def test_resume_strategy_clears_per_strategy_flag(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        engine.risk.pause_strategy(
            strategy_name="sma_crossover",
            reason="prior",
            command_uid="cmd_prior",
        )

        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="resume-strategy", reason="reviewed",
            target_strategy="sma_crossover",
        )
        engine._process_operator_commands()

        assert engine.risk.is_strategy_paused("sma_crossover") is False
        row = store.get_by_command_uid(uid)
        assert row.status == "succeeded"

    def test_resume_strategy_without_target_rejects(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="resume-strategy", reason="test",
        )
        engine._process_operator_commands()
        row = store.get_by_command_uid(uid)
        assert row.status == "rejected_validation"


class TestStickyPauseRestore:
    def test_restore_pause_entries_from_disk(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        with open(state_path, "w") as fh:
            json.dump(
                {
                    "entries_paused": True,
                    "entries_paused_reason": "from prior session",
                    "entries_paused_command_uid": "cmd_aaa",
                },
                fh,
            )
        engine._restore_control_state()
        assert engine.risk.is_entries_paused() is True
        assert engine.risk.entries_paused_reason() == "from prior session"

    def test_restore_paused_strategies_from_disk(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        with open(state_path, "w") as fh:
            json.dump(
                {
                    "paused_strategies": {
                        "sma_crossover": {
                            "reason": "from prior",
                            "command_uid": "cmd_xxx",
                            "paused_at": "2026-06-15T00:00:00+00:00",
                        },
                    },
                },
                fh,
            )
        engine._restore_control_state()
        assert engine.risk.is_strategy_paused("sma_crossover") is True
        snap = engine.risk.paused_strategies_snapshot()
        assert snap["sma_crossover"]["reason"] == "from prior"

    def test_restore_handles_mixed_halt_plus_pauses(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        with open(state_path, "w") as fh:
            json.dump(
                {
                    "halted": True,
                    "reason": "halt reason",
                    "command_uid": "cmd_halt",
                    "entries_paused": True,
                    "entries_paused_reason": "pause reason",
                    "paused_strategies": {
                        "rsi_reversion": {"reason": "scoped", "command_uid": "cmd_s"},
                    },
                },
                fh,
            )
        engine._restore_control_state()
        # Halt: prefix added by restore code.
        assert engine.risk.is_halted() is True
        assert engine.risk.halt_reason().startswith("operator_halt_sticky:")
        # Pause flags also set.
        assert engine.risk.is_entries_paused() is True
        assert engine.risk.is_strategy_paused("rsi_reversion") is True


class TestProcessSymbolGate:
    """The engine's `_process_symbol` consults RiskManager pause flags
    before entry dispatch. These tests exercise the gate via the
    RiskManager directly — confirming the flag is the source of truth
    is enough for unit scope; the engine wiring is exercised by the
    paper end-to-end verify script."""

    def test_gate_blocks_when_entries_paused(self):
        risk = RiskManager()
        assert risk.is_entries_paused() is False
        risk.pause_entries(reason="t", command_uid="c")
        assert risk.is_entries_paused() is True

    def test_gate_per_strategy_is_scoped(self):
        risk = RiskManager()
        risk.pause_strategy(strategy_name="sma_crossover", reason="t")
        assert risk.is_strategy_paused("sma_crossover") is True
        assert risk.is_strategy_paused("rsi_reversion") is False

    def test_halt_and_pause_are_independent(self):
        """The halt check is the outer gate; soft pause layers under it.
        Both can be set independently; clearing one does NOT clear the
        other."""
        risk = RiskManager()
        risk._engage_kill_switch("operator_halt: x")
        risk.pause_entries(reason="x", command_uid="c")
        assert risk.is_halted() is True
        assert risk.is_entries_paused() is True
        # Resume entries — halt still active.
        risk.resume_entries()
        assert risk.is_halted() is True
        assert risk.is_entries_paused() is False
        # Reset kill switch — pause stays false.
        risk.reset_kill_switches()
        assert risk.is_halted() is False
        assert risk.is_entries_paused() is False
