"""Detect and remediate duplicate rows that block the foundation PR's
new partial UNIQUE indexes.

Discovery doc §12.2: when the foundation startup migration fires
``MigrationDuplicatesFound``, the operator runs this script offline
against the affected database (``data/trades.db`` for paper,
``data/trades_live.db`` for live). Two dimensions are covered:

  - ``position_lifecycle.owner_key`` duplicates that would block
    ``uniq_one_active_position_per_owner_key``
  - ``trades.order_id`` duplicates within the
    ``position_type='single_leg'`` (or NULL — pending BACKFILL) scope
    that would block ``uniq_trades_order_id_single_leg``

Three modes (PR #59 §12.2 / R8-2):

  ``--detect`` (default)
      Read-only. Lists every duplicate cluster with the rows it would
      keep / delete. Writes nothing. Exit 0 if clean, 1 if duplicates
      found.

  ``--review FILE``
      Same scan as ``--detect`` but emits a JSON decisions file the
      operator can edit. Each cluster gets an explicit "keep" row id
      and a "delete" list. Exit always 0 once the file is written.

  ``--apply FILE``
      Apply a previously-emitted decisions file. Each delete is a
      single-row ``DELETE FROM ... WHERE id = ?``. The script runs
      every delete inside one transaction; partial failure rolls
      back. Exit 0 on success.

The script never writes outside of explicit ``--apply``. Detection
and review are safe to run while the live bot is up.

Usage::

    python scripts/migrate_dedupe_trades.py --db data/trades.db
    python scripts/migrate_dedupe_trades.py --db data/trades.db \\
        --review decisions.json
    # operator edits decisions.json, then:
    python scripts/migrate_dedupe_trades.py --db data/trades.db \\
        --apply decisions.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

# Local import — the script intentionally pulls from engine.lifecycle_orders
# so detection stays in sync with the runtime preflight. If the migration
# logic changes, this script picks up the change automatically.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine.lifecycle_orders import (  # noqa: E402
    detect_owner_key_duplicates,
    detect_trades_order_id_duplicates,
)


def _scan(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return a structured report of both duplicate dimensions."""
    owner_key_dupes = detect_owner_key_duplicates(conn)
    trades_dupes = detect_trades_order_id_duplicates(conn)

    # position_lifecycle's timestamp column is `opened_at` in newer
    # schema and `created_at` in older. Detect which one exists so the
    # script keeps working against both.
    pl_cols = {
        col[1] for col in conn.execute(
            "PRAGMA table_info(position_lifecycle)"
        ).fetchall()
    }
    timestamp_col = "opened_at" if "opened_at" in pl_cols else "created_at"

    owner_key_report = []
    for dup in owner_key_dupes:
        # For each cluster, fetch the rows so the operator can choose
        # which to keep based on timestamps / strategy / status.
        rows = conn.execute(
            f"SELECT position_uid, status, strategy, {timestamp_col}, "
            "symbol "
            "FROM position_lifecycle "
            "WHERE owner_key = ? "
            f"ORDER BY {timestamp_col}",
            (dup.owner_key,),
        ).fetchall()
        owner_key_report.append({
            "owner_key": dup.owner_key,
            "count": dup.count,
            "rows": [
                {
                    "position_uid": r[0],
                    "status": r[1],
                    "strategy": r[2],
                    "opened_at": r[3],
                    "symbol": r[4],
                }
                for r in rows
            ],
        })

    trades_report = []
    for dup in trades_dupes:
        rows = conn.execute(
            "SELECT id, timestamp, symbol, side, qty, avg_fill_price, "
            "status, position_type, position_uid "
            "FROM trades "
            "WHERE order_id = ? "
            "ORDER BY timestamp",
            (dup.order_id,),
        ).fetchall()
        trades_report.append({
            "order_id": dup.order_id,
            "count": dup.count,
            "rows": [
                {
                    "id": r[0],
                    "timestamp": r[1],
                    "symbol": r[2],
                    "side": r[3],
                    "qty": r[4],
                    "avg_fill_price": r[5],
                    "status": r[6],
                    "position_type": r[7],
                    "position_uid": r[8],
                }
                for r in rows
            ],
        })

    return {
        "owner_key_duplicates": owner_key_report,
        "trades_order_id_duplicates": trades_report,
    }


def _detect(conn: sqlite3.Connection) -> int:
    """--detect mode: print summary, exit non-zero if dirty."""
    report = _scan(conn)
    owner_n = len(report["owner_key_duplicates"])
    trades_n = len(report["trades_order_id_duplicates"])
    if owner_n == 0 and trades_n == 0:
        print("CLEAN: no duplicates detected. Foundation migration "
              "will run without remediation.")
        return 0
    print(f"DIRTY: {owner_n} owner_key cluster(s), "
          f"{trades_n} trades.order_id cluster(s)")
    if owner_n:
        print("\nposition_lifecycle.owner_key duplicates:")
        for cluster in report["owner_key_duplicates"]:
            print(f"  owner_key={cluster['owner_key']!r} "
                  f"({cluster['count']} rows):")
            for r in cluster["rows"]:
                print(f"    - uid={r['position_uid']} "
                      f"status={r['status']} "
                      f"strategy={r['strategy']} "
                      f"opened_at={r['opened_at']} "
                      f"symbol={r['symbol']}")
    if trades_n:
        print("\ntrades.order_id duplicates "
              "(NULL or single_leg position_type):")
        for cluster in report["trades_order_id_duplicates"]:
            print(f"  order_id={cluster['order_id']!r} "
                  f"({cluster['count']} rows):")
            for r in cluster["rows"]:
                print(f"    - id={r['id']} "
                      f"timestamp={r['timestamp']} "
                      f"symbol={r['symbol']} "
                      f"side={r['side']} "
                      f"qty={r['qty']} "
                      f"avg_fill_price={r['avg_fill_price']} "
                      f"position_type={r['position_type']} "
                      f"status={r['status']}")
    print("\nNext step: re-run with --review FILE to emit a decisions "
          "file, edit it, then --apply FILE.")
    return 1


def _review(conn: sqlite3.Connection, out_path: Path) -> int:
    """--review mode: emit JSON decisions file. Default behavior is to
    keep the EARLIEST row in each cluster and delete the rest; operator
    is expected to read and adjust before --apply."""
    report = _scan(conn)
    decisions: dict[str, Any] = {
        "version": 1,
        "note": (
            "Edit the keep / delete lists. Defaults keep the earliest "
            "row in each cluster. Run --apply on this file after "
            "review."
        ),
        "owner_key_clusters": [],
        "trades_order_id_clusters": [],
    }
    for cluster in report["owner_key_duplicates"]:
        # Default: keep the earliest opened_at, delete the rest.
        rows = cluster["rows"]
        # Already sorted by opened_at ASC; the first is the proposed
        # keeper.
        decisions["owner_key_clusters"].append({
            "owner_key": cluster["owner_key"],
            "keep_position_uid": rows[0]["position_uid"],
            "delete_position_uids": [r["position_uid"] for r in rows[1:]],
            "rows": rows,  # for operator reference
        })
    for cluster in report["trades_order_id_duplicates"]:
        rows = cluster["rows"]
        decisions["trades_order_id_clusters"].append({
            "order_id": cluster["order_id"],
            "keep_trade_id": rows[0]["id"],
            "delete_trade_ids": [r["id"] for r in rows[1:]],
            "rows": rows,
        })
    out_path.write_text(json.dumps(decisions, indent=2, default=str))
    n = (
        len(decisions["owner_key_clusters"])
        + len(decisions["trades_order_id_clusters"])
    )
    if n == 0:
        print(f"CLEAN: no clusters found. {out_path} written (no-op).")
    else:
        print(f"Wrote decisions file with {n} cluster(s) to {out_path}")
        print("Edit the keep / delete fields, then run --apply.")
    return 0


def _apply(conn: sqlite3.Connection, in_path: Path) -> int:
    """--apply mode: execute deletes from a decisions file. Single
    transaction; rolls back on any failure."""
    decisions = json.loads(in_path.read_text())
    if decisions.get("version") != 1:
        print(f"ERROR: unsupported decisions file version "
              f"{decisions.get('version')!r}")
        return 2

    owner_clusters = decisions.get("owner_key_clusters", [])
    trades_clusters = decisions.get("trades_order_id_clusters", [])

    deleted_owner = 0
    deleted_trades = 0
    try:
        for cluster in owner_clusters:
            for uid in cluster.get("delete_position_uids", []):
                conn.execute(
                    "DELETE FROM position_lifecycle WHERE position_uid = ?",
                    (uid,),
                )
                deleted_owner += 1
        for cluster in trades_clusters:
            for tid in cluster.get("delete_trade_ids", []):
                conn.execute("DELETE FROM trades WHERE id = ?", (tid,))
                deleted_trades += 1
        conn.commit()
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: apply failed, rolled back: {exc}")
        return 2

    print(f"Applied: deleted {deleted_owner} position_lifecycle row(s), "
          f"{deleted_trades} trades row(s).")
    print("Re-run --detect to confirm the DB is clean before restarting "
          "the bot.")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=(
            "Detect and remediate duplicate rows that block the "
            "foundation PR's new partial UNIQUE indexes."
        )
    )
    p.add_argument("--db", required=True, help="Path to SQLite DB file")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--detect", action="store_true",
        help="Default. Read-only scan; exit 1 if duplicates found.",
    )
    mode.add_argument(
        "--review", metavar="FILE",
        help="Read-only scan; emit JSON decisions file to FILE.",
    )
    mode.add_argument(
        "--apply", metavar="FILE",
        help="Apply deletes from FILE inside a single transaction.",
    )
    args = p.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: db not found at {db_path}")
        return 2

    conn = sqlite3.connect(db_path)
    try:
        if args.review:
            return _review(conn, Path(args.review))
        if args.apply:
            return _apply(conn, Path(args.apply))
        return _detect(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
