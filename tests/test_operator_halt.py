"""Integration tests for the operator halt / resume-after-halt flow.

Operator Controls Phase A PR-2. Covers the engine-side of the queue:

- `halt` flips the engine to halted via the existing RiskManager and
  persists the sticky-halt JSON.
- `resume-after-halt` clears the halt when reconciliation yields NORMAL,
  and refuses when it doesn't.
- Sticky halt re-engages on the next process start.
- Unsupported actions are rejected with `rejected_unsupported_phase_a`.

We exercise the methods directly on a hand-constructed engine — the
unit-test pattern used by `tests/test_lifecycle_review_fixes.py` —
rather than spinning up the full main-loop, which has too many
dependencies for a focused test of these handlers.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from engine.operator_queue import (
    OperatorCommandStore,
    new_command_uid,
)
from engine.trader import TradingEngine
from reporting.logger import TradeLogger


def _build_engine(tmp_path, *, halted=False, halt_reason=None):
    db_path = tmp_path / "trades.db"
    state_path = tmp_path / "operator_control_state.json"

    tl = TradeLogger(path=str(db_path))
    conn = tl._ensure_db()
    store = OperatorCommandStore(conn)

    engine = TradingEngine.__new__(TradingEngine)
    engine.operator_command_store = store
    engine.lifecycle_store = None
    engine._session_start_equity = 100_000.0
    engine.trade_logger = tl

    # Mock the risk manager with the minimum surface area used by the
    # operator-halt handlers.
    engine.risk = MagicMock()
    engine.risk.is_halted = MagicMock(return_value=halted)
    engine.risk.halt_reason = MagicMock(return_value=halt_reason)
    engine.risk._engage_kill_switch = MagicMock()
    engine.risk.reset_kill_switches = MagicMock()

    # Alerts dispatcher — record calls but don't actually send anywhere.
    engine.alerts = MagicMock()

    # The resume path calls sync_with_broker → _restore_ownership_from_db
    # → _reconcile_startup. Mock these explicitly per-test.
    engine.broker = MagicMock()

    # Patch the sticky-halt path to a tmp file. We import settings at
    # the method level so the engine reads the current value rather
    # than a cached one — monkeypatch via attribute on the module.
    return engine, store, str(state_path)


class TestHaltHandler:
    def test_halt_engages_kill_switch_and_persists(
        self, tmp_path, monkeypatch
    ):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )

        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="halt", reason="market event",
            requested_by="franco",
        )

        engine._process_operator_commands()

        # Risk manager kill switch was engaged.
        engine.risk._engage_kill_switch.assert_called_once()
        engage_arg = engine.risk._engage_kill_switch.call_args.args[0]
        assert "operator_halt" in engage_arg
        assert "market event" in engage_arg

        # Command transitioned to succeeded.
        row = store.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.completed_at is not None
        assert row.result.get("halted") is True

        # Sticky halt JSON written.
        import os
        assert os.path.exists(state_path)
        with open(state_path) as fh:
            persisted = json.load(fh)
        assert persisted["halted"] is True
        assert persisted["reason"] == "market event"
        assert persisted["command_uid"] == uid

    def test_kill_switch_failure_marks_failed(
        self, tmp_path, monkeypatch
    ):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        engine.risk._engage_kill_switch.side_effect = RuntimeError("boom")

        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="halt", reason="test",
        )
        engine._process_operator_commands()

        row = store.get_by_command_uid(uid)
        assert row.status == "failed"
        assert "boom" in (row.result.get("error") or "")


class TestResumeAfterHaltHandler:
    def _setup_resume(self, tmp_path, monkeypatch, *, mode):
        engine, store, state_path = _build_engine(
            tmp_path, halted=True, halt_reason="prior"
        )
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        # Pre-write a sticky-halt file so we can verify deletion.
        with open(state_path, "w") as fh:
            json.dump({"halted": True, "reason": "prior"}, fh)

        # Snapshot returned by sync_with_broker doesn't need real data —
        # _reconcile_startup is mocked out below.
        engine.broker.sync_with_broker = MagicMock(return_value=MagicMock())
        engine._restore_ownership_from_db = MagicMock(return_value=set())
        engine._reconcile_startup = MagicMock(return_value=mode)

        return engine, store, state_path

    def test_resume_clears_halt_on_normal(
        self, tmp_path, monkeypatch
    ):
        engine, store, state_path = self._setup_resume(
            tmp_path, monkeypatch, mode="NORMAL",
        )

        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="resume-after-halt",
            reason="event passed",
        )
        engine._process_operator_commands()

        engine.risk.reset_kill_switches.assert_called_once()
        row = store.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result.get("halted") is False
        assert row.result.get("reconcile_mode") == "NORMAL"

        import os
        assert not os.path.exists(state_path), (
            "sticky-halt file must be removed on successful resume"
        )

    def test_resume_refuses_when_restricted(
        self, tmp_path, monkeypatch
    ):
        engine, store, state_path = self._setup_resume(
            tmp_path, monkeypatch, mode="RESTRICTED",
        )

        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="resume-after-halt",
            reason="event passed",
        )
        engine._process_operator_commands()

        # Kill switch NOT cleared — RESTRICTED is unsafe.
        engine.risk.reset_kill_switches.assert_not_called()
        row = store.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert row.result.get("mode") == "RESTRICTED"

        import os
        # Sticky-halt file preserved so a subsequent restart re-engages.
        assert os.path.exists(state_path)

    def test_resume_when_not_halted_rejects(
        self, tmp_path, monkeypatch
    ):
        engine, store, state_path = _build_engine(
            tmp_path, halted=False,  # NOT halted
        )
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )

        uid = new_command_uid()
        store.insert(
            command_uid=uid, action="resume-after-halt",
            reason="confused",
        )
        engine._process_operator_commands()

        # No reconcile attempt was made — refused at the gate.
        engine.broker.sync_with_broker.assert_not_called()
        engine.risk.reset_kill_switches.assert_not_called()
        row = store.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "no active halt" in (row.result.get("note") or "")


class TestStickyHaltRestore:
    def test_restore_engages_kill_switch(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )

        with open(state_path, "w") as fh:
            json.dump(
                {
                    "halted": True,
                    "reason": "from prior session",
                    "command_uid": "cmd_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                },
                fh,
            )

        engine._restore_sticky_halt_state()

        engine.risk._engage_kill_switch.assert_called_once()
        engage_arg = engine.risk._engage_kill_switch.call_args.args[0]
        assert "operator_halt_sticky" in engage_arg
        assert "from prior session" in engage_arg

    def test_restore_skips_when_file_missing(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        # File does NOT exist.
        engine._restore_sticky_halt_state()
        engine.risk._engage_kill_switch.assert_not_called()

    def test_restore_skips_when_halted_false(self, tmp_path, monkeypatch):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        with open(state_path, "w") as fh:
            json.dump({"halted": False}, fh)
        engine._restore_sticky_halt_state()
        engine.risk._engage_kill_switch.assert_not_called()

    def test_restore_tolerates_malformed_json(
        self, tmp_path, monkeypatch
    ):
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )
        with open(state_path, "w") as fh:
            fh.write("{not valid json")
        # Must NOT raise — best-effort restore.
        engine._restore_sticky_halt_state()
        engine.risk._engage_kill_switch.assert_not_called()


class TestUnsupportedActions:
    def test_unknown_action_marks_unsupported(
        self, tmp_path, monkeypatch
    ):
        """If a future Phase B/C action is written to the queue but
        the running engine is Phase A, it must be rejected, not
        silently dropped."""
        engine, store, state_path = _build_engine(tmp_path)
        monkeypatch.setattr(
            "config.settings.OPERATOR_CONTROL_STATE_PATH", state_path
        )

        # Bypass insert validation to simulate a Phase B/C row.
        # created_at must be fresh so it doesn't get rejected_expired
        # by the claim path before the action handler sees it.
        from datetime import datetime, timezone
        uid = new_command_uid()
        store._conn.execute(
            "INSERT INTO operator_commands ("
            "schema_version, command_uid, created_at, action, reason, status, params_json"
            ") VALUES (1, ?, ?, ?, ?, 'pending', '{}')",
            (
                uid,
                datetime.now(timezone.utc).isoformat(),
                "reduce-position",  # Phase C
                "future action",
            ),
        )
        store._conn.commit()

        engine._process_operator_commands()

        row = store.get_by_command_uid(uid)
        assert row.status == "rejected_unsupported_phase_a"
        assert "not implemented in Phase A" in (
            row.result.get("note") or ""
        )
        engine.risk._engage_kill_switch.assert_not_called()
