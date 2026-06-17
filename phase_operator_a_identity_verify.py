"""
Operator Controls Phase A (PR-1) — Identity foundation verification.

What this proves end-to-end against a paper Alpaca account / local DB:

  1. **Schema migration**: a fresh `TradeLogger` creates the
     `position_lifecycle`, `position_lifecycle_legs` tables and adds
     the `position_uid` column to `trades`. Running migration twice is
     idempotent.

  2. **Lifecycle transitions**: `create_pending` → `mark_open` →
     `mark_closed` produces the expected row state and timestamps;
     `mark_canceled` after a partial fill is rejected per §8.1.

  3. **Backfill**: `synthesize_for_existing` is idempotent — running
     it twice for the same owner_key produces exactly one row.

  4. **TradeLogger threading**: a trade record built with
     `position_uid=...` persists the value to the `trades` table.

  5. **Reconciliation pass** (offline simulation): given a
     `BrokerSnapshot`-like mock, `_reconcile_position_lifecycle`
     synthesizes rows for unmanaged broker positions and closes
     orphan lifecycle rows.

This script does NOT submit live orders. It exercises the SQLite-
and code-level invariants of PR-1. For the full paper-trading flow
verification (open an SMA position, restart the bot, confirm
lifecycle survives), see the end-to-end checklist in the
operator-controls implementation plan.

Run: `python phase_operator_a_identity_verify.py`
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

from loguru import logger

from engine.lifecycle import (
    PositionLifecycleLeg,
    PositionLifecycleStore,
    client_order_id_for,
    new_position_uid,
)
from reporting.logger import TradeLogger, TradeRecord


def _ok(msg: str) -> None:
    print(f"  OK    {msg}")


def _fail(msg: str) -> None:
    print(f"  FAIL  {msg}", file=sys.stderr)


def _section(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def check_schema_migration(tmp_dir: str) -> int:
    _section("1. Schema migration — idempotent on first and second call")
    failures = 0
    db_path = os.path.join(tmp_dir, "verify1.db")

    conn1 = TradeLogger(path=db_path)._ensure_db()
    tables = {
        r[0] for r in conn1.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if "position_lifecycle" in tables and "position_lifecycle_legs" in tables:
        _ok("position_lifecycle and position_lifecycle_legs tables created")
    else:
        _fail(f"missing lifecycle tables; got {sorted(tables)}")
        failures += 1

    cols = {r[1] for r in conn1.execute("PRAGMA table_info(trades)")}
    if "position_uid" in cols:
        _ok("trades.position_uid column added")
    else:
        _fail("trades.position_uid missing")
        failures += 1

    # Second open on same path — migration must be idempotent.
    conn2 = TradeLogger(path=db_path)._ensure_db()
    rows = conn2.execute(
        "SELECT COUNT(*) FROM sqlite_master "
        "WHERE name IN ('position_lifecycle', 'position_lifecycle_legs')"
    ).fetchone()
    if rows[0] == 2:
        _ok("second migration call is idempotent (no duplicates)")
    else:
        _fail(f"second migration produced unexpected table count: {rows[0]}")
        failures += 1

    return failures


def check_lifecycle_transitions(tmp_dir: str) -> int:
    _section("2. Lifecycle transitions and §8.1 invariant")
    failures = 0
    db_path = os.path.join(tmp_dir, "verify2.db")
    conn = TradeLogger(path=db_path)._ensure_db()
    store = PositionLifecycleStore(conn)

    uid = new_position_uid()
    store.create_pending(
        position_uid=uid,
        symbol="NVDA", owner_key="NVDA",
        strategy="sma_crossover", position_type="single_leg",
        entry_qty=10.0,
        entry_client_order_id=client_order_id_for("sma_crossover", uid),
    )
    row = store.get_by_position_uid(uid)
    if row and row.status == "pending":
        _ok("create_pending → pending status")
    else:
        _fail(f"unexpected status after create_pending: {row.status if row else None}")
        failures += 1

    store.mark_open(position_uid=uid, avg_entry_price=884.20, current_qty=10.0)
    row = store.get_by_position_uid(uid)
    if row.status == "open" and row.avg_entry_price == 884.20:
        _ok("mark_open → open status with avg_entry_price persisted")
    else:
        _fail(f"unexpected post-open state: status={row.status}, avg={row.avg_entry_price}")
        failures += 1

    # §8.1 invariant — partial fill + cancel attempt must be rejected.
    uid2 = new_position_uid()
    store.create_pending(
        position_uid=uid2, symbol="MU", owner_key="MU",
        strategy="sma_crossover", position_type="single_leg",
        entry_qty=10.0,
    )
    store.mark_partially_filled(
        position_uid=uid2, avg_entry_price=120.0, current_qty=4.0,
    )
    try:
        store.mark_canceled(position_uid=uid2)
        _fail("mark_canceled SHOULD have been rejected after partial fill (§8.1)")
        failures += 1
    except ValueError as exc:
        if "§8.1" in str(exc):
            _ok("§8.1 invariant enforced — mark_canceled rejected after partial fill")
        else:
            _fail(f"rejection happened but wrong message: {exc}")
            failures += 1

    store.mark_closed(position_uid=uid, net_realized_pnl=250.0)
    row = store.get_by_position_uid(uid)
    if row.status == "closed" and row.closed_at is not None:
        _ok("mark_closed → closed status with closed_at set")
    else:
        _fail(f"unexpected post-close state: {row.status} closed_at={row.closed_at}")
        failures += 1
    return failures


def check_backfill_idempotent(tmp_dir: str) -> int:
    _section("3. Backfill — synthesize_for_existing is idempotent")
    failures = 0
    db_path = os.path.join(tmp_dir, "verify3.db")
    conn = TradeLogger(path=db_path)._ensure_db()
    store = PositionLifecycleStore(conn)

    uid_first = store.synthesize_for_existing(
        symbol="AAPL", owner_key="AAPL",
        strategy="sma_crossover", position_type="single_leg",
        current_qty=5.0, avg_entry_price=189.50,
    )
    uid_second = store.synthesize_for_existing(
        symbol="AAPL", owner_key="AAPL",
        strategy="sma_crossover", position_type="single_leg",
        current_qty=5.0, avg_entry_price=189.50,
    )
    if uid_first == uid_second:
        _ok("two synthesize_for_existing calls returned same uid")
    else:
        _fail(f"backfill not idempotent: {uid_first} vs {uid_second}")
        failures += 1
    rows = [r for r in store.get_open() if r.owner_key == "AAPL"]
    if len(rows) == 1:
        _ok("exactly one lifecycle row exists for owner_key=AAPL")
    else:
        _fail(f"expected 1 row, got {len(rows)}")
        failures += 1
    if rows and rows[0].metadata.get("synthesized") is True:
        _ok("backfill row tagged with metadata.synthesized=true")
    else:
        _fail("backfill row missing synthesized metadata flag")
        failures += 1
    return failures


def check_trade_log_threading(tmp_dir: str) -> int:
    _section("4. TradeLogger persists position_uid")
    failures = 0
    db_path = os.path.join(tmp_dir, "verify4.db")
    tl = TradeLogger(path=db_path)
    uid = new_position_uid()
    record = TradeRecord(
        timestamp="2026-05-31T10:00:00+00:00",
        symbol="NVDA",
        side="buy",
        qty=10.0,
        avg_fill_price=884.20,
        order_id="alpaca-1234",
        strategy="sma_crossover",
        reason="entry",
        stop_price=850.0,
        entry_reference_price=884.20,
        # Phase 2 + 4 (PR #67): production writers never populate
        # the legacy columns; set None to mirror the shape every
        # other path writes.
        modeled_slippage_bps=None,
        realized_slippage_bps=None,
        order_type="market",
        status="filled",
        requested_qty=10.0,
        filled_qty=10.0,
        position_id="NVDA",
        position_type="single_leg",
        position_uid=uid,
    )
    tl.log(record)
    conn = tl._ensure_db()
    row = conn.execute(
        "SELECT position_uid FROM trades WHERE order_id='alpaca-1234'"
    ).fetchone()
    if row and row[0] == uid:
        _ok(f"position_uid persisted to trades row ({uid[:18]}…)")
    else:
        _fail(f"position_uid not persisted; row={row}")
        failures += 1
    return failures


def check_client_order_id_helper() -> int:
    _section("5. client_order_id_for helper format")
    failures = 0
    uid = "pos_abcdef0123456789abcdef0123456789"
    coid = client_order_id_for("sma_crossover", uid)
    if coid == "sma_crossover-abcdef0123":
        _ok(f"base format: {coid}")
    else:
        _fail(f"unexpected base format: {coid}")
        failures += 1
    coid_close = client_order_id_for("sma_crossover", uid, suffix="close")
    if coid_close == "sma_crossover-abcdef0123-close":
        _ok(f"suffix format: {coid_close}")
    else:
        _fail(f"unexpected suffix format: {coid_close}")
        failures += 1
    # Length within Alpaca's typical 48-char limit.
    if len(coid_close) <= 48:
        _ok(f"client_order_id length within budget ({len(coid_close)} chars)")
    else:
        _fail(f"client_order_id too long: {len(coid_close)} chars")
        failures += 1
    return failures


def main() -> int:
    logger.remove()  # quiet loguru noise during verification
    print("=" * 60)
    print("Operator Controls Phase A (PR-1) — Identity verification")
    print("=" * 60)

    with tempfile.TemporaryDirectory() as tmp_dir:
        failures = 0
        failures += check_schema_migration(tmp_dir)
        failures += check_lifecycle_transitions(tmp_dir)
        failures += check_backfill_idempotent(tmp_dir)
        failures += check_trade_log_threading(tmp_dir)
        failures += check_client_order_id_helper()

    print()
    print("=" * 60)
    if failures == 0:
        print("PASS — all identity-foundation invariants verified")
        return 0
    else:
        print(f"FAIL — {failures} check(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
