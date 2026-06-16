"""Phase C verification — paper-equivalent end-to-end check.

Run after PR merge and migration:

    venv/bin/python phase_operator_c_verify.py

Exits 0 on full pass. Verifies the Phase C additions WITHOUT hitting Alpaca:

  1. VALID_ACTIONS includes the 3 destructive actions.
  2. SymbolLockRegistry round-trip — acquire/release/conflict semantics.
  3. scripts/operator.py argparse wires the 3 new subcommands.
  4. close-position / reduce-position / cancel-position-orders all
     enforce --confirm <position_uid_short> (first 10 hex of uid).
  5. Each subcommand enqueues a queue row carrying target_position_uid
     (and params.pct for reduce-position).

Phase A + B verify scripts must continue to pass after Phase C lands.
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.operator_queue import (  # noqa: E402
    OperatorCommandStore,
    VALID_ACTIONS,
    new_command_uid,
)
from engine.symbol_locks import SymbolLockRegistry  # noqa: E402
from reporting.logger import TradeLogger  # noqa: E402
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
        _section("1. VALID_ACTIONS includes Phase C")
        phase_c = {"close-position", "reduce-position", "cancel-position-orders"}
        if phase_c.issubset(VALID_ACTIONS):
            _ok("3 destructive actions present")
        else:
            _fail(f"missing actions: {phase_c - VALID_ACTIONS}")
            failures += 1

        # ── 2. SymbolLockRegistry contract ────────────────────────
        _section("2. SymbolLockRegistry semantics")
        reg = SymbolLockRegistry()
        h = reg.acquire(
            owner_key="AAPL", kind="operator_command", identifier="cmd_a",
        )
        if h is not None:
            _ok("first acquire returns holder")
        else:
            _fail("first acquire failed")
            failures += 1
        second = reg.acquire(
            owner_key="AAPL", kind="operator_command", identifier="cmd_b",
        )
        if second is None:
            _ok("conflicting acquire returns None")
        else:
            _fail("conflicting acquire incorrectly succeeded")
            failures += 1
        reg.release(owner_key="AAPL", holder=h)
        if reg.is_locked("AAPL") is None:
            _ok("release clears lock")
        else:
            _fail("release did not clear lock")
            failures += 1

        # ── 3. CLI wiring ─────────────────────────────────────────
        _section("3. CLI argparse wires Phase C subcommands")
        rc, out, err = _run_cli(["--help"])
        text = out + err
        for sub in ("close-position", "reduce-position", "cancel-position-orders"):
            if sub not in text:
                _fail(f"--help missing subcommand: {sub}")
                failures += 1
        if all(s in text for s in
               ("close-position", "reduce-position", "cancel-position-orders")):
            _ok("--help lists all 3 destructive subcommands")

        # ── 4. CLI confirm enforcement ────────────────────────────
        _section("4. CLI confirm enforcement (--confirm <uid_short>)")
        TradeLogger(path=db_path)._ensure_db()
        full_uid = "pos_abcdef0123456789abcdef0123456789"
        good_token = "abcdef0123"
        for action, extra in (
            ("close-position", []),
            ("reduce-position", ["--pct", "50"]),
            ("cancel-position-orders", []),
        ):
            # Without --confirm.
            rc, _, err = _run_cli([
                "--db", db_path, "--state-path", state_path,
                action, full_uid, "--reason", "verify", *extra,
            ])
            if rc == 0:
                _fail(f"{action}: missing --confirm should have failed")
                failures += 1
            elif good_token not in err:
                _fail(f"{action}: error message should mention required token")
                failures += 1
            else:
                _ok(f"{action}: --confirm enforcement works")

            # Wrong --confirm.
            rc, _, err = _run_cli([
                "--db", db_path, "--state-path", state_path,
                action, full_uid, "--reason", "verify", *extra,
                "--confirm", "wrong",
            ])
            if rc == 0:
                _fail(f"{action}: wrong --confirm should have failed")
                failures += 1
            else:
                _ok(f"{action}: wrong --confirm rejected")

            # Correct --confirm enqueues.
            rc, out, _ = _run_cli([
                "--db", db_path, "--state-path", state_path,
                action, full_uid, "--reason", "verify", *extra,
                "--confirm", good_token,
            ])
            if rc != 0 or f"queued {action}" not in out:
                _fail(f"{action}: enqueue failed when properly confirmed")
                failures += 1
            else:
                _ok(f"{action}: enqueued row with correct --confirm")

        # ── 5. Queue row shape ────────────────────────────────────
        _section("5. Queue rows carry target_position_uid + params")
        import sqlite3 as _sql
        conn = _sql.connect(db_path)
        store = OperatorCommandStore(conn)
        rows = store.recent(limit=10)
        for action in ("close-position", "reduce-position", "cancel-position-orders"):
            matches = [r for r in rows if r.action == action]
            if not matches:
                _fail(f"no queue row for {action}")
                failures += 1
                continue
            row = matches[0]
            if row.target_position_uid != full_uid:
                _fail(f"{action} row missing target_position_uid")
                failures += 1
            else:
                _ok(f"{action} row carries target_position_uid")
            if action == "reduce-position":
                if row.params.get("pct") != 50.0:
                    _fail(f"reduce-position row missing pct in params: {row.params}")
                    failures += 1
                else:
                    _ok("reduce-position row carries pct=50 in params")

    print()
    print("=" * 60)
    if failures == 0:
        print("PASS — operator controls Phase C invariants verified")
        return 0
    print(f"FAIL — {failures} check(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(run())
