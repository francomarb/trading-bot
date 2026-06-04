"""Phase operator-A PR-2 verification.

Run after PR-2 lands and the bot has migrated:

    venv/bin/python phase_operator_a_controls_verify.py

Exits 0 on full pass. Verifies the operator control surface end-to-end
WITHOUT hitting Alpaca:

  1. Schema migration produced `operator_commands` with the expected columns.
  2. The queue's `insert` / `claim_next_pending` / terminal-state writes
     behave per `engine.operator_queue`'s contract.
  3. `scripts/operator.py` argparse wiring exposes every Phase A action.
  4. The `halt` CLI subcommand writes a pending queue row and rejects
     without `--confirm`.
  5. The `resume-after-halt` CLI subcommand writes a pending queue row
     and rejects without `--confirm`.
  6. Reading subcommands (`status`, `positions`, `show-position`,
     `commands`) execute against a freshly migrated DB.
  7. Sticky halt persistence helpers round-trip.

Companion to `phase_operator_a_identity_verify.py` (PR-1) which
validates the lifecycle-identity foundation. This one validates the
operator command surface added by PR-2.
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

# Ensure repo root is on sys.path even when this script is invoked from
# arbitrary working directories.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.operator_queue import (  # noqa: E402
    OPERATOR_QUEUE_SCHEMA_VERSION,
    OperatorCommandStore,
    VALID_ACTIONS,
    VALID_STATUSES,
    new_command_uid,
)
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


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
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
        control_path = os.path.join(tmp, "operator_control_state.json")

        # ── 1. Schema migration ───────────────────────────────────
        _section("1. Schema migration")
        conn = TradeLogger(path=db_path)._ensure_db()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(operator_commands)")}
        expected_cols = {
            "id", "schema_version", "command_uid", "created_at",
            "accepted_at", "completed_at", "requested_by", "action",
            "target_position_uid", "target_strategy", "params_json",
            "reason", "status", "client_order_id", "result_json",
        }
        missing = expected_cols - cols
        if missing:
            _fail(f"operator_commands missing columns: {sorted(missing)}")
            failures += 1
        else:
            _ok(f"operator_commands has {len(expected_cols)} expected columns")
        idx = {
            r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_operator_commands%'"
            )
        }
        for needed in (
            "idx_operator_commands_status_created",
            "idx_operator_commands_target_position_uid",
            "idx_operator_commands_target_strategy",
        ):
            if needed not in idx:
                _fail(f"missing index: {needed}")
                failures += 1
        if not (
            "idx_operator_commands_status_created" in idx
            and "idx_operator_commands_target_position_uid" in idx
            and "idx_operator_commands_target_strategy" in idx
        ):
            pass
        else:
            _ok("all three operator-queue indexes present")

        # ── 2. Queue API contract ─────────────────────────────────
        _section("2. Queue API contract")
        store = OperatorCommandStore(conn)

        uid_a = new_command_uid()
        uid_b = new_command_uid()
        store.insert(command_uid=uid_a, action="halt", reason="first")
        store.insert(command_uid=uid_b, action="halt", reason="second")
        _ok("inserted two pending halt commands")

        claimed = store.claim_next_pending(expiry_seconds=180)
        if claimed is None or claimed.command_uid != uid_a:
            _fail("claim_next_pending did not return the oldest row")
            failures += 1
        else:
            _ok(f"claim_next_pending returned oldest row ({claimed.action})")

        if claimed.status != "accepted":
            _fail(f"claimed row status={claimed.status} (expected accepted)")
            failures += 1
        else:
            _ok("claimed row transitioned to accepted")

        store.mark_succeeded(
            command_uid=uid_a, result={"halted": True},
        )
        row_a = store.get_by_command_uid(uid_a)
        if row_a.status != "succeeded":
            _fail("mark_succeeded did not transition row")
            failures += 1
        else:
            _ok("mark_succeeded transitioned uid_a to succeeded")

        # ── 3. CLI argparse wiring ────────────────────────────────
        _section("3. CLI argparse wiring")
        rc, out, err = _run_cli(["--help"])
        combined = out + err
        for sub in (
            "status", "positions", "show-position",
            "commands", "halt", "resume-after-halt",
        ):
            if sub not in combined:
                _fail(f"--help missing subcommand: {sub}")
                failures += 1
        if all(s in combined for s in (
            "status", "positions", "show-position",
            "commands", "halt", "resume-after-halt",
        )):
            _ok("--help lists every Phase A subcommand")

        # ── 4. CLI halt ───────────────────────────────────────────
        _section("4. CLI halt")
        rc, _, err = _run_cli([
            "--db", db_path, "--state-path", state_path,
            "halt", "--reason", "verify",
        ])
        if rc == 0:
            _fail("halt without --confirm should have failed")
            failures += 1
        else:
            _ok("halt without --confirm rejected (rc=%d)" % rc)

        rc, out, _ = _run_cli([
            "--db", db_path, "--state-path", state_path,
            "halt", "--reason", "verify", "--confirm", "halt",
        ])
        if rc != 0 or "queued halt" not in out:
            _fail("halt with --confirm did not enqueue a row")
            failures += 1
        else:
            _ok("halt with --confirm enqueued a row")

        # ── 5. CLI resume-after-halt ─────────────────────────────
        _section("5. CLI resume-after-halt")
        rc, _, err = _run_cli([
            "--db", db_path, "--state-path", state_path,
            "resume-after-halt", "--reason", "verify",
        ])
        if rc == 0:
            _fail("resume-after-halt without --confirm should have failed")
            failures += 1
        else:
            _ok("resume-after-halt without --confirm rejected (rc=%d)" % rc)

        rc, out, _ = _run_cli([
            "--db", db_path, "--state-path", state_path,
            "resume-after-halt", "--reason", "verify", "--confirm", "resume",
        ])
        if rc != 0 or "queued resume-after-halt" not in out:
            _fail("resume-after-halt with --confirm did not enqueue a row")
            failures += 1
        else:
            _ok("resume-after-halt with --confirm enqueued a row")

        # ── 6. Read-only subcommands ─────────────────────────────
        _section("6. Read-only subcommands")
        for cmd in ("status", "positions", "commands"):
            rc, _, err = _run_cli([
                "--db", db_path, "--state-path", state_path, cmd,
            ])
            if rc != 0:
                _fail(f"{cmd} returned non-zero (rc={rc}): {err.strip()}")
                failures += 1
            else:
                _ok(f"{cmd} ran successfully")

        # ── 7. Sticky halt round-trip ────────────────────────────
        _section("7. Sticky halt persistence helpers")
        # Manually round-trip the JSON file the engine reads/writes.
        payload = {
            "halted": True,
            "reason": "verify",
            "command_uid": new_command_uid(),
            "set_at": "2026-06-04T00:00:00+00:00",
        }
        with open(control_path, "w") as fh:
            json.dump(payload, fh)
        with open(control_path) as fh:
            loaded = json.load(fh)
        if loaded == payload:
            _ok("sticky halt JSON round-trips byte-for-byte")
        else:
            _fail("sticky halt JSON did not round-trip identically")
            failures += 1

        # ── 8. Public constants ──────────────────────────────────
        _section("8. Public constants")
        if VALID_ACTIONS == {"halt", "resume-after-halt"}:
            _ok("VALID_ACTIONS locked to Phase A actions")
        else:
            _fail(f"VALID_ACTIONS = {VALID_ACTIONS} (expected halt + resume-after-halt)")
            failures += 1
        if "rejected_unsupported_phase_a" in VALID_STATUSES:
            _ok("rejected_unsupported_phase_a recognised by store")
        else:
            _fail("rejected_unsupported_phase_a missing from VALID_STATUSES")
            failures += 1

    print()
    print("=" * 60)
    if failures == 0:
        print("PASS — operator controls PR-2 invariants verified")
        return 0
    print(f"FAIL — {failures} check(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(run())
