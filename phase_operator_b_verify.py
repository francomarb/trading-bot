"""Phase B verification — paper-equivalent end-to-end check.

Run after PR-#?? merges and the bot has migrated:

    venv/bin/python phase_operator_b_verify.py

Exits 0 on full pass. Verifies the Phase B additions WITHOUT hitting Alpaca:

  1. VALID_ACTIONS includes the 4 new soft-control actions.
  2. RiskManager exposes pause accessors and they round-trip cleanly.
  3. `_persist_control_state` / `_restore_control_state` round-trip
     halt + pause-entries + pause-strategy together.
  4. `scripts/operator.py` argparse wires the 4 new subcommands.
  5. Each CLI subcommand rejects without `--confirm` and enqueues a
     queue row when properly confirmed.
  6. `status` subcommand surfaces pause state from engine_state.json.
  7. Heartbeat thread can be constructed and drains commands.

Companion to `phase_operator_a_identity_verify.py` (PR-1) and
`phase_operator_a_controls_verify.py` (PR-2). All three must continue
to pass after Phase B lands.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import threading
import time
from contextlib import redirect_stderr, redirect_stdout

# Repo root onto sys.path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.operator_queue import (  # noqa: E402
    OperatorCommandStore,
    VALID_ACTIONS,
    new_command_uid,
)
from reporting.logger import TradeLogger  # noqa: E402
from risk.manager import RiskManager  # noqa: E402
from scripts import operator as operator_cli  # noqa: E402


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)


def _ok(msg: str) -> None:
    print(f"  OK    {msg}")


def _section(name: str) -> None:
    print()
    print("=" * 60)
    print(name)
    print("=" * 60)


def _run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            rc = operator_cli.main(argv)
        except SystemExit as e:
            rc = e.code if e.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


def run() -> int:
    failures = 0

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "trades.db")
        state_path = os.path.join(tmp, "engine_state.json")

        # ── 1. VALID_ACTIONS enum ─────────────────────────────────
        _section("1. VALID_ACTIONS includes Phase B")
        phase_b = {
            "pause-entries", "resume-entries",
            "pause-strategy", "resume-strategy",
        }
        if phase_b.issubset(VALID_ACTIONS):
            _ok("4 soft-control actions present")
        else:
            _fail(f"missing actions: {phase_b - VALID_ACTIONS}")
            failures += 1

        # ── 2. RiskManager accessors round-trip ──────────────────
        _section("2. RiskManager pause accessors")
        rm = RiskManager()
        rm.pause_entries(reason="r1", command_uid="c1")
        if rm.is_entries_paused() and rm.entries_paused_reason() == "r1":
            _ok("pause_entries → is_entries_paused True")
        else:
            _fail("pause_entries did not flip the flag")
            failures += 1
        rm.resume_entries()
        if not rm.is_entries_paused():
            _ok("resume_entries → is_entries_paused False")
        else:
            _fail("resume_entries did not clear the flag")
            failures += 1
        rm.pause_strategy(strategy_name="sma_crossover", reason="r2")
        if rm.is_strategy_paused("sma_crossover") and not rm.is_strategy_paused("rsi_reversion"):
            _ok("pause_strategy scoped to one strategy")
        else:
            _fail("pause_strategy scope broken")
            failures += 1
        rm.resume_strategy(strategy_name="sma_crossover")
        if not rm.is_strategy_paused("sma_crossover"):
            _ok("resume_strategy clears one strategy")
        else:
            _fail("resume_strategy did not clear")
            failures += 1

        # Idempotency.
        rm.pause_entries(reason="r")
        if rm.pause_entries(reason="again") is False:
            _ok("pause_entries idempotent (returns False on no-op)")
        else:
            _fail("pause_entries should return False when already paused")
            failures += 1
        rm.resume_entries()

        # ── 3. Persistence round-trip ─────────────────────────────
        _section("3. Control-state JSON round-trip")
        # Set up halt + pause + per-strategy together.
        rm = RiskManager()
        rm._engage_kill_switch("operator_halt: test halt")
        rm.pause_entries(reason="test pause", command_uid="cmd_pe")
        rm.pause_strategy(
            strategy_name="donchian_breakout", reason="test scope",
            command_uid="cmd_ps",
        )

        from unittest.mock import MagicMock
        from engine.trader import TradingEngine
        engine = TradingEngine.__new__(TradingEngine)
        engine.risk = rm
        engine.operator_command_store = None  # not exercised here

        ctrl_path = os.path.join(tmp, "operator_control_state.json")
        from config import settings as _settings
        _orig = _settings.OPERATOR_CONTROL_STATE_PATH
        _settings.OPERATOR_CONTROL_STATE_PATH = ctrl_path
        try:
            engine._persist_control_state(
                halt_command_uid="cmd_h",
                halt_reason_override="test halt",  # bare; prefix added on restore
            )
            if not os.path.exists(ctrl_path):
                _fail("persist did not write the JSON")
                failures += 1
            else:
                with open(ctrl_path) as fh:
                    persisted = json.load(fh)
                checks = (
                    persisted.get("halted") is True,
                    persisted.get("reason") == "test halt",
                    persisted.get("entries_paused") is True,
                    persisted.get("entries_paused_reason") == "test pause",
                    "donchian_breakout" in (persisted.get("paused_strategies") or {}),
                )
                if all(checks):
                    _ok("persist captured halt + pause-entries + pause-strategy")
                else:
                    _fail(f"persist payload incomplete: {persisted}")
                    failures += 1

            # Restore into a fresh RiskManager.
            rm2 = RiskManager()
            engine.risk = rm2
            engine._restore_control_state()
            if (
                rm2.is_halted()
                and rm2.halt_reason().startswith("operator_halt_sticky:")
                and rm2.is_entries_paused()
                and rm2.is_strategy_paused("donchian_breakout")
            ):
                _ok("restore re-applied all three flags")
            else:
                _fail("restore did not re-apply state correctly")
                failures += 1
        finally:
            _settings.OPERATOR_CONTROL_STATE_PATH = _orig

        # ── 4. CLI wiring ─────────────────────────────────────────
        _section("4. CLI argparse wires Phase B subcommands")
        rc, out, err = _run_cli(["--help"])
        text = out + err
        for sub in ("pause-entries", "resume-entries", "pause-strategy", "resume-strategy"):
            if sub not in text:
                _fail(f"--help missing subcommand: {sub}")
                failures += 1
        if all(s in text for s in (
            "pause-entries", "resume-entries", "pause-strategy", "resume-strategy",
        )):
            _ok("--help lists all 4 Phase B subcommands")

        # ── 5. CLI confirm + enqueue ──────────────────────────────
        _section("5. CLI confirm enforcement + enqueue")
        # Initialise the DB for the CLI.
        TradeLogger(path=db_path)._ensure_db()

        cases = [
            ("pause-entries", "pause", []),
            ("resume-entries", "resume", []),
            ("pause-strategy", "pause", ["--strategy", "sma_crossover"]),
            ("resume-strategy", "resume", ["--strategy", "sma_crossover"]),
        ]
        for action, token, extra in cases:
            # Without --confirm.
            rc, _, err = _run_cli([
                "--db", db_path, "--state-path", state_path,
                action, "--reason", "verify", *extra,
            ])
            if rc == 0:
                _fail(f"{action}: missing --confirm should have failed")
                failures += 1
            else:
                _ok(f"{action}: --confirm enforcement works (rc={rc})")
            # With --confirm.
            rc, out, _ = _run_cli([
                "--db", db_path, "--state-path", state_path,
                action, "--reason", "verify", "--confirm", token, *extra,
            ])
            if rc != 0 or f"queued {action}" not in out:
                _fail(f"{action}: enqueue failed when properly confirmed")
                failures += 1
            else:
                _ok(f"{action}: enqueue succeeded with --confirm {token}")

        # ── 6. status subcommand surfaces pauses ──────────────────
        _section("6. status surfaces pause state")
        with open(state_path, "w") as fh:
            json.dump({
                "risk_controls": {
                    "is_halted": False,
                    "entries_paused": True,
                    "entries_paused_reason": "verify",
                    "paused_strategies": {"sma_crossover": {"reason": "scoped"}},
                },
            }, fh)
        rc, out, _ = _run_cli([
            "--db", db_path, "--state-path", state_path, "status",
        ])
        if rc == 0 and "entries paused:     YES" in out and "sma_crossover" in out:
            _ok("status renders entries-paused + paused-strategies")
        else:
            _fail("status did not surface pause state")
            failures += 1

        # ── 7. Heartbeat drains commands ──────────────────────────
        _section("7. Heartbeat thread drains commands fast")
        # Make a fresh DB to avoid the test_operator_cli rows above.
        hb_db = os.path.join(tmp, "trades_hb.db")
        tl = TradeLogger(path=hb_db)
        conn = tl._ensure_db()
        store = OperatorCommandStore(conn)

        engine = TradingEngine.__new__(TradingEngine)
        engine.operator_command_store = store
        engine.lifecycle_store = None
        engine.trade_logger = tl
        engine.risk = RiskManager()
        engine.alerts = MagicMock()
        engine.broker = MagicMock()
        engine._running = True
        engine._operator_heartbeat_stop = threading.Event()
        engine._operator_heartbeat_thread = None

        def fast_loop():
            while engine._running:
                try:
                    engine._process_operator_commands()
                except Exception:
                    pass
                if engine._operator_heartbeat_stop.wait(timeout=0.05):
                    break

        thread = threading.Thread(target=fast_loop, daemon=True)
        engine._operator_heartbeat_thread = thread
        thread.start()
        try:
            uid = new_command_uid()
            store.insert(
                command_uid=uid, action="pause-entries", reason="hb",
            )
            deadline = time.monotonic() + 1.0
            row = None
            while time.monotonic() < deadline:
                row = store.get_by_command_uid(uid)
                if row.status != "pending":
                    break
                time.sleep(0.02)
            if row is not None and row.status == "succeeded":
                _ok("heartbeat drained pause-entries within ≤1s")
            else:
                _fail(f"heartbeat did not drain; status={row and row.status}")
                failures += 1
        finally:
            engine._running = False
            engine._operator_heartbeat_stop.set()
            thread.join(timeout=2.0)

    print()
    print("=" * 60)
    if failures == 0:
        print("PASS — operator controls Phase B invariants verified")
        return 0
    print(f"FAIL — {failures} check(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(run())
