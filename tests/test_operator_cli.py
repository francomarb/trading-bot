"""Tests for the operator CLI (`scripts/operator.py`).

Operator Controls Phase A PR-2. Covers:

- Argparse wiring: every subcommand is registered.
- `halt` and `resume-after-halt` reject without `--confirm` and write
  a queue row when properly confirmed.
- Read-only subcommands run successfully against a freshly migrated DB.
- Help text mentions every Phase A action.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout, redirect_stderr

import pytest

from engine.lifecycle import (
    PositionLifecycleStore,
    new_position_uid,
)
from engine.operator_queue import OperatorCommandStore
from reporting.logger import TradeLogger
from scripts import operator as operator_cli


@pytest.fixture
def db_paths(tmp_path):
    """Spin up a fully migrated trade DB + an empty state-snapshot path."""
    db_path = tmp_path / "trades.db"
    state_path = tmp_path / "engine_state.json"
    TradeLogger(path=str(db_path))._ensure_db()
    return str(db_path), str(state_path)


def _run(argv: list[str]):
    """Run the CLI and capture (rc, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = operator_cli.main(argv)
        except SystemExit as e:
            rc = e.code if e.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


class TestArgparse:
    def test_help_mentions_phase_a_actions(self):
        rc, out, err = _run(["--help"])
        assert rc == 0
        combined = out + err
        for action in (
            "status", "positions", "show-position",
            "commands", "halt", "resume-after-halt",
        ):
            assert action in combined

    def test_missing_action_fails(self):
        rc, _, err = _run([])
        # argparse exits non-zero when a required subcommand is absent.
        assert rc != 0
        assert "action" in err.lower() or "required" in err.lower()


class TestHaltCommand:
    def test_refuses_without_confirm(self, db_paths):
        db_path, state_path = db_paths
        rc, out, err = _run([
            "--db", db_path, "--state-path", state_path,
            "halt", "--reason", "test",
        ])
        assert rc != 0
        assert "--confirm halt" in err

    def test_refuses_without_reason(self, db_paths):
        db_path, state_path = db_paths
        rc, out, err = _run([
            "--db", db_path, "--state-path", state_path,
            "halt", "--reason", "", "--confirm", "halt",
        ])
        assert rc != 0
        assert "reason" in err.lower()

    def test_writes_queue_row_when_confirmed(self, db_paths):
        db_path, state_path = db_paths
        rc, out, err = _run([
            "--db", db_path, "--state-path", state_path,
            "halt", "--reason", "market event", "--confirm", "halt",
        ])
        assert rc == 0
        assert "queued halt" in out

        # Confirm the row landed and is pending.
        import sqlite3
        conn = sqlite3.connect(db_path)
        store = OperatorCommandStore(conn)
        recent = store.recent(limit=10)
        halts = [r for r in recent if r.action == "halt"]
        assert len(halts) == 1
        assert halts[0].status == "pending"
        assert halts[0].reason == "market event"

    def test_typo_in_confirm_token_is_rejected(self, db_paths):
        db_path, state_path = db_paths
        rc, _, err = _run([
            "--db", db_path, "--state-path", state_path,
            "halt", "--reason", "test", "--confirm", "halts",
        ])
        assert rc != 0
        assert "halt" in err


class TestResumeAfterHaltCommand:
    def test_refuses_without_confirm(self, db_paths):
        db_path, state_path = db_paths
        rc, _, err = _run([
            "--db", db_path, "--state-path", state_path,
            "resume-after-halt", "--reason", "event passed",
        ])
        assert rc != 0
        assert "--confirm resume" in err

    def test_writes_queue_row_when_confirmed(self, db_paths):
        db_path, state_path = db_paths
        rc, out, err = _run([
            "--db", db_path, "--state-path", state_path,
            "resume-after-halt",
            "--reason", "checked",
            "--confirm", "resume",
        ])
        assert rc == 0
        assert "queued resume-after-halt" in out


class TestReadOnlyCommands:
    def test_status_runs_without_state_file(self, db_paths):
        # state_path intentionally does NOT exist — status must still
        # render basic lifecycle/queue summary.
        db_path, state_path = db_paths
        rc, out, err = _run([
            "--db", db_path, "--state-path", state_path,
            "status",
        ])
        assert rc == 0
        assert "operator queue" in out or "pending" in out

    def test_positions_empty(self, db_paths):
        db_path, state_path = db_paths
        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "positions",
        ])
        assert rc == 0
        assert "(none)" in out

    def test_positions_with_one_row(self, db_paths):
        db_path, state_path = db_paths
        import sqlite3
        store = PositionLifecycleStore(sqlite3.connect(db_path))
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid, symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        store.mark_open(
            position_uid=uid, avg_entry_price=884.20, current_qty=10.0,
        )

        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "positions",
        ])
        assert rc == 0
        assert "NVDA" in out
        assert "sma_crossover" in out
        assert "open" in out

    def test_positions_joins_with_real_snapshot_shape(self, db_paths):
        """`positions_detail` in the live engine_state.json is a
        `dict[owner_key, dict]`, not a list of dicts. An earlier
        version of the CLI iterated it as a list and crashed on
        production state. This test locks in the real shape.
        """
        import json
        import sqlite3

        db_path, state_path = db_paths
        store = PositionLifecycleStore(sqlite3.connect(db_path))
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid, symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        store.mark_open(
            position_uid=uid, avg_entry_price=884.20, current_qty=10.0,
        )

        # Real engine_state.json structure (dict keyed by owner_key).
        with open(state_path, "w") as fh:
            json.dump({
                "positions_detail": {
                    "NVDA": {
                        "strategy": "sma_crossover",
                        "qty": 10.0,
                        "avg_entry_price": 884.20,
                        "market_value": 9010.0,
                        "unrealized_pnl": 125.50,
                    },
                },
            }, fh)

        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "positions",
        ])
        assert rc == 0, "positions must not crash on real engine_state.json shape"
        # The snapshot-supplied market_value / unrealized_pnl should
        # show up in the rendered table.
        assert "9,010" in out
        assert "+125.50" in out

    def test_positions_tolerates_unexpected_state_shapes(self, db_paths):
        """Defensive: an unexpected `positions_detail` shape (e.g. a
        list or a string) must not crash the CLI; it should fall back
        to lifecycle-only rendering."""
        import json
        import sqlite3

        db_path, state_path = db_paths
        store = PositionLifecycleStore(sqlite3.connect(db_path))
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid, symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        store.mark_open(
            position_uid=uid, avg_entry_price=884.20, current_qty=10.0,
        )

        # Wrong shape — list of strings.
        with open(state_path, "w") as fh:
            json.dump({"positions_detail": ["NVDA", "MU"]}, fh)

        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "positions",
        ])
        assert rc == 0
        assert "NVDA" in out

    def test_show_position_unknown_returns_error(self, db_paths):
        db_path, state_path = db_paths
        rc, _, err = _run([
            "--db", db_path, "--state-path", state_path,
            "show-position", "pos_doesnotexist00000000000000000000",
        ])
        assert rc == 1
        assert "no lifecycle row" in err

    def test_show_position_known_renders(self, db_paths):
        db_path, state_path = db_paths
        import sqlite3
        store = PositionLifecycleStore(sqlite3.connect(db_path))
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid, symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        store.mark_open(
            position_uid=uid, avg_entry_price=884.20, current_qty=10.0,
        )

        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "show-position", uid,
        ])
        assert rc == 0
        assert uid in out
        assert "NVDA" in out
        assert "sma_crossover" in out

    def test_commands_empty(self, db_paths):
        db_path, state_path = db_paths
        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "commands",
        ])
        assert rc == 0
        assert "(none)" in out

    def test_commands_after_halt(self, db_paths):
        db_path, state_path = db_paths
        _run([
            "--db", db_path, "--state-path", state_path,
            "halt", "--reason", "test 1", "--confirm", "halt",
        ])
        _run([
            "--db", db_path, "--state-path", state_path,
            "halt", "--reason", "test 2", "--confirm", "halt",
        ])
        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "commands",
        ])
        assert rc == 0
        # Newest first.
        assert out.index("test 2") < out.index("test 1")


class TestPhaseBSoftControls:
    """Phase B — the 4 soft-control subcommands. Same `--confirm` +
    `--reason` pattern as `halt`."""

    def test_pause_entries_refuses_without_confirm(self, db_paths):
        db_path, state_path = db_paths
        rc, _, err = _run([
            "--db", db_path, "--state-path", state_path,
            "pause-entries", "--reason", "test",
        ])
        assert rc != 0
        assert "--confirm pause" in err

    def test_pause_entries_writes_queue_row_when_confirmed(self, db_paths):
        db_path, state_path = db_paths
        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "pause-entries", "--reason", "market event", "--confirm", "pause",
        ])
        assert rc == 0
        assert "queued pause-entries" in out

        import sqlite3
        conn = sqlite3.connect(db_path)
        store = OperatorCommandStore(conn)
        rows = store.recent(limit=5)
        ent = [r for r in rows if r.action == "pause-entries"]
        assert len(ent) == 1
        assert ent[0].status == "pending"
        assert ent[0].reason == "market event"

    def test_resume_entries_refuses_without_confirm(self, db_paths):
        db_path, state_path = db_paths
        rc, _, err = _run([
            "--db", db_path, "--state-path", state_path,
            "resume-entries", "--reason", "ok",
        ])
        assert rc != 0
        assert "--confirm resume" in err

    def test_pause_strategy_requires_strategy(self, db_paths):
        db_path, state_path = db_paths
        # argparse should reject before our handler runs.
        rc, _, err = _run([
            "--db", db_path, "--state-path", state_path,
            "pause-strategy", "--reason", "test", "--confirm", "pause",
        ])
        assert rc != 0
        assert "strategy" in (err or "").lower()

    def test_pause_strategy_writes_target_strategy(self, db_paths):
        db_path, state_path = db_paths
        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "pause-strategy",
            "--strategy", "donchian_breakout",
            "--reason", "regime", "--confirm", "pause",
        ])
        assert rc == 0
        assert "queued pause-strategy" in out

        import sqlite3
        conn = sqlite3.connect(db_path)
        store = OperatorCommandStore(conn)
        rows = store.recent(limit=5)
        ps = [r for r in rows if r.action == "pause-strategy"]
        assert len(ps) == 1
        assert ps[0].target_strategy == "donchian_breakout"

    def test_resume_strategy_writes_target_strategy(self, db_paths):
        db_path, state_path = db_paths
        rc, _, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "resume-strategy",
            "--strategy", "sma_crossover",
            "--reason", "reviewed", "--confirm", "resume",
        ])
        assert rc == 0


class TestStatusSurfacesPauseState:
    """The `status` subcommand should display halt + pause state from
    the engine_state.json snapshot. Earlier versions only showed halt;
    Phase B adds pause-entries + paused_strategies."""

    def test_status_shows_no_pauses_when_clean(self, db_paths):
        import json as _json
        db_path, state_path = db_paths
        with open(state_path, "w") as fh:
            _json.dump({"risk_controls": {"is_halted": False}}, fh)
        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "status",
        ])
        assert rc == 0
        assert "entries paused:     no" in out
        assert "paused strategies:  (none)" in out

    def test_status_shows_pause_state(self, db_paths):
        import json as _json
        db_path, state_path = db_paths
        with open(state_path, "w") as fh:
            _json.dump({
                "risk_controls": {
                    "is_halted": False,
                    "entries_paused": True,
                    "entries_paused_reason": "market event",
                    "paused_strategies": {
                        "donchian_breakout": {"reason": "regime"},
                    },
                },
            }, fh)
        rc, out, _ = _run([
            "--db", db_path, "--state-path", state_path,
            "status",
        ])
        assert rc == 0
        assert "entries paused:     YES" in out
        assert "market event" in out
        assert "donchian_breakout" in out


class TestMissingDB:
    def test_missing_db_returns_2(self, tmp_path):
        # Point at a non-existent DB.
        rc, _, err = _run([
            "--db", str(tmp_path / "does_not_exist.db"),
            "--state-path", str(tmp_path / "engine_state.json"),
            "status",
        ])
        assert rc == 2
        assert "not found" in err
