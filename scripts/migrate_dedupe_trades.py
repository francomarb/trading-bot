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


def _cluster_fingerprint(rows: list[dict[str, Any]]) -> str:
    """Stable hash of the rows in a cluster, used by --apply to
    detect mutation between --review and --apply.

    PR #60 round 3 fix (P1 dedupe sub-point): the fingerprint must
    include every field the operator could have used to decide
    keep vs delete. Round 2's set covered status / qty / price /
    timestamp / position_uid; round 3 adds symbol / side / strategy /
    position_type and the accounting columns (net_realized_pnl,
    realized_pnl, realized_slippage_bps, slippage_signed_*,
    reason). A mutation in any of these between --review and --apply
    invalidates the operator's decision.

    Order independent (sorted by row identity) so cosmetic reordering
    doesn't trip the check."""
    import hashlib

    fingerprint_columns = (
        "status", "position_type", "qty", "avg_fill_price",
        "timestamp", "opened_at", "position_uid",
        "symbol", "side", "strategy",
        "net_realized_pnl",
        "realized_pnl", "realized_slippage_bps",
        "slippage_signed_bps", "slippage_adverse_bps",
        "reason",
    )
    keyed: list[tuple] = []
    for r in rows:
        identity = r.get("id") or r.get("position_uid") or ""
        keyed.append(
            (str(identity),) + tuple(r.get(c) for c in fingerprint_columns)
        )
    keyed.sort(key=lambda t: t[0])
    payload = json.dumps(keyed, default=str, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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

    # PR #60 round 3 fix (P1 dedupe): which optional accounting columns
    # exist on this particular DB. Older schemas may lack some.
    pl_optional_cols = [
        c for c in ("net_realized_pnl", "position_type")
        if c in pl_cols
    ]
    trades_cols = {
        col[1] for col in conn.execute("PRAGMA table_info(trades)").fetchall()
    }
    # PR #60 round 4 fix (P1 expanded coverage): every column the
    # conflict check might inspect must be in the report, otherwise
    # _delete_has_data_keeper_lacks gets None for both sides and
    # silently passes.
    trades_optional_cols = [
        c for c in (
            "order_type", "requested_qty", "filled_qty",
            "realized_pnl", "r_multiple",
            "realized_slippage_bps",
            "slippage_signed_bps", "slippage_adverse_bps",
            "modeled_slippage_bps",
            "slippage_benchmark_price", "slippage_benchmark_kind",
            "slippage_benchmark_timestamp",
            "slippage_measurement_quality",
            "initial_stop_loss", "initial_risk_per_share",
            "initial_risk_dollars",
            "entry_timestamp", "exit_timestamp",
            "stop_trigger_price", "reason",
        )
        if c in trades_cols
    ]

    owner_key_report = []
    for dup in owner_key_dupes:
        # PR #60 round 2 fix (P0): review MUST fetch only the rows
        # the detector flagged. Fetching all owner_key=X rows lets
        # historical closed rows show up next to the live duplicates
        # and the keep-earliest default would then propose deleting
        # the active positions. Scope to exactly dup.position_uids.
        if not dup.position_uids:
            continue
        placeholders = ", ".join("?" for _ in dup.position_uids)
        select_cols = [
            "position_uid", "status", "strategy", timestamp_col, "symbol",
        ] + pl_optional_cols
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} "
            "FROM position_lifecycle "
            f"WHERE position_uid IN ({placeholders}) "
            f"ORDER BY {timestamp_col}",
            dup.position_uids,
        ).fetchall()
        out_rows = []
        for r in rows:
            row_dict: dict[str, Any] = {
                "position_uid": r[0],
                "status": r[1],
                "strategy": r[2],
                "opened_at": r[3],
                "symbol": r[4],
            }
            for offset, col in enumerate(pl_optional_cols, start=5):
                row_dict[col] = r[offset]
            out_rows.append(row_dict)
        owner_key_report.append({
            "owner_key": dup.owner_key,
            "count": dup.count,
            "rows": out_rows,
        })

    trades_report = []
    for dup in trades_dupes:
        # Same defect on the trades side: a WHERE order_id = ? scope
        # includes legitimate spread legs (position_type='spread'
        # rows correctly share order_id). Filter to the exact trade
        # ids the detector returned.
        if not dup.trade_ids:
            continue
        placeholders = ", ".join("?" for _ in dup.trade_ids)
        select_cols = [
            "id", "timestamp", "symbol", "side", "qty", "avg_fill_price",
            "status", "position_type", "position_uid",
        ] + trades_optional_cols
        rows = conn.execute(
            f"SELECT {', '.join(select_cols)} "
            "FROM trades "
            f"WHERE id IN ({placeholders}) "
            "ORDER BY timestamp",
            dup.trade_ids,
        ).fetchall()
        out_rows = []
        for r in rows:
            row_dict = {
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
            for offset, col in enumerate(trades_optional_cols, start=9):
                row_dict[col] = r[offset]
            out_rows.append(row_dict)
        trades_report.append({
            "order_id": dup.order_id,
            "count": dup.count,
            "rows": out_rows,
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
    # PR #60 round 4 fix (P1 detect output): print every accounting /
    # provenance column we fetched so the operator can compare values
    # across rows before deciding the keeper. _scan now pulls
    # net_realized_pnl on the owner-key side and the full slippage /
    # risk-anchor / accounting set on the trades side; surface them
    # here.
    if owner_n:
        print("\nposition_lifecycle.owner_key duplicates:")
        for cluster in report["owner_key_duplicates"]:
            print(f"  owner_key={cluster['owner_key']!r} "
                  f"({cluster['count']} rows):")
            for r in cluster["rows"]:
                # Core identity fields first, then every additional
                # field that was fetched (skips None for readability).
                core = (
                    f"    - uid={r['position_uid']} "
                    f"status={r['status']} "
                    f"strategy={r['strategy']} "
                    f"opened_at={r['opened_at']} "
                    f"symbol={r['symbol']}"
                )
                extras = [
                    f"{k}={v!r}" for k, v in r.items()
                    if k not in {
                        "position_uid", "status", "strategy",
                        "opened_at", "symbol",
                    } and v is not None
                ]
                if extras:
                    print(core + " " + " ".join(extras))
                else:
                    print(core)
    if trades_n:
        print("\ntrades.order_id duplicates "
              "(NULL or single_leg position_type):")
        for cluster in report["trades_order_id_duplicates"]:
            print(f"  order_id={cluster['order_id']!r} "
                  f"({cluster['count']} rows):")
            for r in cluster["rows"]:
                core = (
                    f"    - id={r['id']} "
                    f"timestamp={r['timestamp']} "
                    f"symbol={r['symbol']} "
                    f"side={r['side']} "
                    f"qty={r['qty']} "
                    f"avg_fill_price={r['avg_fill_price']} "
                    f"position_type={r['position_type']} "
                    f"status={r['status']}"
                )
                extras = [
                    f"{k}={v!r}" for k, v in r.items()
                    if k not in {
                        "id", "timestamp", "symbol", "side", "qty",
                        "avg_fill_price", "position_type", "status",
                        "position_uid",
                    } and v is not None
                ]
                if extras:
                    print(core + " " + " ".join(extras))
                else:
                    print(core)
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
            # Snapshot fingerprint per cluster — apply mode uses these
            # to detect rows mutated between --review and --apply, so
            # an operator who reviewed a stale snapshot cannot
            # accidentally delete the wrong row.
            "review_fingerprint": _cluster_fingerprint(rows),
            "rows": rows,
        })
    for cluster_idx, dec_cluster in enumerate(decisions["owner_key_clusters"]):
        dec_cluster["review_fingerprint"] = _cluster_fingerprint(
            dec_cluster["rows"]
        )
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


class _ApplyAborted(Exception):
    """Internal signal that the apply transaction must roll back."""


# PR #60 round 4 fix (P1 dedupe — expanded coverage + asymmetric):
# Every operator-decision-relevant column. Round 3's set covered
# accounting headlines; round 4 reviewer noted it missed broker-
# cumulative state (filled_qty, avg_fill_price), entry/exit
# timestamps, modeled-slippage provenance, benchmark provenance,
# risk anchors, and stop-trigger price. A "partial earliest"
# keeper could therefore silently discard a more complete later
# delete row.
_OWNER_KEY_CONFLICT_COLUMNS = (
    "strategy",
    "symbol",
    "position_type",
    "net_realized_pnl",
)
_TRADES_CONFLICT_COLUMNS = (
    # Identity / intent
    "symbol", "side", "position_type", "position_uid",
    "order_type", "requested_qty",
    # Broker cumulative state — a later observation may legitimately
    # advance these, but a sparser keeper losing them is data loss
    # the operator must approve explicitly.
    "filled_qty", "avg_fill_price", "status",
    # Computed accounting
    "realized_pnl", "r_multiple",
    "realized_slippage_bps",
    "slippage_signed_bps", "slippage_adverse_bps",
    # Modeled slippage + benchmark provenance
    "modeled_slippage_bps",
    "slippage_benchmark_price", "slippage_benchmark_kind",
    "slippage_benchmark_timestamp",
    "slippage_measurement_quality",
    # Risk anchors set at entry
    "initial_stop_loss", "initial_risk_per_share",
    "initial_risk_dollars",
    # Lifecycle timestamps
    "entry_timestamp", "exit_timestamp",
    "stop_trigger_price",
    # Operator-readable rationale
    "reason",
)


def _delete_has_data_keeper_lacks(
    keep_val: Any, delete_val: Any,
) -> bool:
    """PR #60 round 4 fix (P1 asymmetric): the conflict check is
    one-directional. A delete row that carries a NON-NULL value the
    keeper LACKS (NULL or different non-null) represents data the
    keeper cannot replace — silently dropping the delete row would
    lose information. Symmetric NULLs and equal non-nulls are fine.

    Cases:
      delete_val is None                       → no conflict (delete
                                                  adds nothing).
      delete_val is non-null, keep_val is None → CONFLICT (data loss).
      delete_val == keep_val                    → no conflict.
      delete_val != keep_val (both non-null)    → CONFLICT (merge
                                                  required).
    """
    if delete_val is None:
        return False
    if keep_val is None:
        return True
    return keep_val != delete_val


def _reject_owner_cluster_on_accounting_conflict(
    cluster: dict[str, Any],
    current_rows: list[dict[str, Any]],
    keep_uid: str | None,
) -> None:
    by_uid = {r["position_uid"]: r for r in current_rows}
    keep_row = by_uid.get(keep_uid) if keep_uid is not None else None
    if keep_row is None:
        return
    for uid in cluster.get("delete_position_uids", []):
        delete_row = by_uid.get(uid)
        if delete_row is None:
            continue
        for col in _OWNER_KEY_CONFLICT_COLUMNS:
            if _delete_has_data_keeper_lacks(
                keep_row.get(col), delete_row.get(col),
            ):
                raise _ApplyAborted(
                    f"owner_key={cluster.get('owner_key')!r}: "
                    f"delete row position_uid={uid!r} has {col!r}="
                    f"{delete_row[col]!r} but keeper "
                    f"position_uid={keep_uid!r} has {col!r}="
                    f"{keep_row.get(col)!r}. Deleting would lose "
                    f"data. Resolve manually (update the keeper or "
                    f"choose a different keeper) and re-run --review."
                )


def _reject_trades_cluster_on_accounting_conflict(
    cluster: dict[str, Any],
    current_rows: list[dict[str, Any]],
    keep_id: int | None,
) -> None:
    by_id = {r["id"]: r for r in current_rows}
    keep_row = by_id.get(keep_id) if keep_id is not None else None
    if keep_row is None:
        return
    for tid in cluster.get("delete_trade_ids", []):
        delete_row = by_id.get(tid)
        if delete_row is None:
            continue
        for col in _TRADES_CONFLICT_COLUMNS:
            if _delete_has_data_keeper_lacks(
                keep_row.get(col), delete_row.get(col),
            ):
                raise _ApplyAborted(
                    f"order_id={cluster.get('order_id')!r}: delete "
                    f"trade id={tid} has {col!r}={delete_row[col]!r} "
                    f"but keeper trade id={keep_id} has {col!r}="
                    f"{keep_row.get(col)!r}. Deleting would lose "
                    f"data. Resolve manually (update the keeper or "
                    f"choose a different keeper) and re-run --review."
                )


def _refetch_owner_cluster(
    conn: sqlite3.Connection,
    cluster: dict[str, Any],
    timestamp_col: str,
) -> list[dict[str, Any]]:
    """Refetch a cluster's current state, mirroring _scan's column
    set so the fingerprint computed in --apply uses the same columns
    as --review's fingerprint."""
    uids = [r["position_uid"] for r in cluster["rows"]]
    if not uids:
        return []
    pl_cols = {
        col[1] for col in conn.execute(
            "PRAGMA table_info(position_lifecycle)"
        ).fetchall()
    }
    optional = [c for c in ("net_realized_pnl", "position_type") if c in pl_cols]
    select_cols = [
        "position_uid", "status", "strategy", timestamp_col, "symbol",
    ] + optional
    placeholders = ", ".join("?" for _ in uids)
    rows = conn.execute(
        f"SELECT {', '.join(select_cols)} "
        "FROM position_lifecycle "
        f"WHERE position_uid IN ({placeholders}) "
        f"ORDER BY {timestamp_col}",
        uids,
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        row_dict = {
            "position_uid": r[0],
            "status": r[1],
            "strategy": r[2],
            "opened_at": r[3],
            "symbol": r[4],
        }
        for offset, col in enumerate(optional, start=5):
            row_dict[col] = r[offset]
        out.append(row_dict)
    return out


def _refetch_trades_cluster(
    conn: sqlite3.Connection, cluster: dict[str, Any],
) -> list[dict[str, Any]]:
    ids = [r["id"] for r in cluster["rows"]]
    if not ids:
        return []
    trades_cols = {
        col[1] for col in conn.execute(
            "PRAGMA table_info(trades)"
        ).fetchall()
    }
    optional = [
        c for c in (
            "order_type", "requested_qty", "filled_qty",
            "realized_pnl", "r_multiple",
            "realized_slippage_bps",
            "slippage_signed_bps", "slippage_adverse_bps",
            "modeled_slippage_bps",
            "slippage_benchmark_price", "slippage_benchmark_kind",
            "slippage_benchmark_timestamp",
            "slippage_measurement_quality",
            "initial_stop_loss", "initial_risk_per_share",
            "initial_risk_dollars",
            "entry_timestamp", "exit_timestamp",
            "stop_trigger_price", "reason",
        )
        if c in trades_cols
    ]
    select_cols = [
        "id", "timestamp", "symbol", "side", "qty", "avg_fill_price",
        "status", "position_type", "position_uid",
    ] + optional
    placeholders = ", ".join("?" for _ in ids)
    rows = conn.execute(
        f"SELECT {', '.join(select_cols)} "
        "FROM trades "
        f"WHERE id IN ({placeholders}) "
        "ORDER BY timestamp",
        ids,
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        row_dict = {
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
        for offset, col in enumerate(optional, start=9):
            row_dict[col] = r[offset]
        out.append(row_dict)
    return out


def _apply(conn: sqlite3.Connection, in_path: Path) -> int:
    """--apply mode: execute deletes from a decisions file with the
    full safety bundle:

    1. PRAGMA foreign_keys = ON so deleting a parent row that the
       new position_lifecycle_orders schema FK-references can never
       leave orphaned children behind.
    2. Snapshot fingerprint per cluster, recomputed in-transaction
       just before the delete. If the cluster mutated between --review
       and --apply the operator gets a structured abort and the
       transaction rolls back (compare-and-set against stale review).
    3. rowcount verification: every DELETE BY id must affect exactly
       one row. A delete that affected zero rows means the operator
       was working from a snapshot where the row had already been
       removed — abort.
    4. In-transaction post-condition rescan: after the deletes apply,
       detect_owner_key_duplicates and detect_trades_order_id_duplicates
       must both return empty inside the SAME transaction (uncommitted
       state) before we commit. If they don't, the decisions file
       didn't cover every cluster — abort.

    Returns 0 on a clean apply, 2 on any abort. The transaction
    rolls back on any abort path, leaving the DB exactly as it was
    before --apply ran.

    Operator-facing precondition: the live bot must NOT be running
    against the same DB. SQLite's default locking will fight the
    bot's writes and either path will see WAL chaos. The script
    prints a banner reminding the operator before doing anything.
    """
    decisions = json.loads(in_path.read_text())
    if decisions.get("version") != 1:
        print(f"ERROR: unsupported decisions file version "
              f"{decisions.get('version')!r}")
        return 2

    print(
        "WARNING: --apply mutates the DB. The live bot MUST be "
        "stopped (stop_bot.sh) before running --apply. Mixed-access "
        "WAL state can produce silent data corruption."
    )

    # PR #60 round 2 fix (P1.7): enforce FK constraints during the
    # delete so any rows in dependent tables (position_lifecycle_orders
    # references position_lifecycle.position_uid) trigger an explicit
    # FK error instead of being silently orphaned.
    conn.execute("PRAGMA foreign_keys = ON")

    owner_clusters = decisions.get("owner_key_clusters", [])
    trades_clusters = decisions.get("trades_order_id_clusters", [])

    # Detect once before the deletes to compare schema column for
    # owner-cluster refetch — same logic as _scan.
    pl_cols = {
        col[1] for col in conn.execute(
            "PRAGMA table_info(position_lifecycle)"
        ).fetchall()
    }
    timestamp_col = "opened_at" if "opened_at" in pl_cols else "created_at"

    deleted_owner = 0
    deleted_trades = 0
    try:
        for cluster in owner_clusters:
            expected_fp = cluster.get("review_fingerprint")
            current_rows = _refetch_owner_cluster(
                conn, cluster, timestamp_col,
            )
            current_fp = _cluster_fingerprint(current_rows)
            if expected_fp is not None and expected_fp != current_fp:
                raise _ApplyAborted(
                    f"owner_key={cluster.get('owner_key')!r} cluster "
                    f"mutated between --review and --apply (snapshot "
                    f"fingerprint mismatch). Re-run --review."
                )

            # PR #60 round 4 fix (P0): keeper-required partition.
            # Round 3's symmetric "keep + delete == snapshot" check
            # passed when keeper=None and delete=all_snapshot_ids —
            # apply would then delete every row in the cluster and
            # the post-scan would report clean. The contract must be
            # stricter: exactly one keeper, drawn from the snapshot,
            # and deletes == snapshot - {keeper}.
            snapshot_uids = {r["position_uid"] for r in cluster["rows"]}
            keep_uid = cluster.get("keep_position_uid")
            delete_uids = list(cluster.get("delete_position_uids", []))
            if keep_uid is None:
                raise _ApplyAborted(
                    f"owner_key={cluster.get('owner_key')!r}: "
                    f"keep_position_uid is null. Every cluster must "
                    f"name exactly one surviving row; refusing to "
                    f"delete the entire cluster."
                )
            if keep_uid not in snapshot_uids:
                raise _ApplyAborted(
                    f"owner_key={cluster.get('owner_key')!r}: "
                    f"keep_position_uid={keep_uid!r} is not in the "
                    f"reviewed snapshot {sorted(snapshot_uids)}. "
                    f"Operator may have edited the keeper to an "
                    f"unrelated id. Aborting."
                )
            if len(delete_uids) != len(set(delete_uids)):
                raise _ApplyAborted(
                    f"owner_key={cluster.get('owner_key')!r}: "
                    f"delete_position_uids contains duplicates; "
                    f"aborting."
                )
            if keep_uid in delete_uids:
                raise _ApplyAborted(
                    f"owner_key={cluster.get('owner_key')!r}: keeper "
                    f"{keep_uid!r} also appears in delete list; "
                    f"aborting."
                )
            expected_deletes = snapshot_uids - {keep_uid}
            if set(delete_uids) != expected_deletes:
                raise _ApplyAborted(
                    f"owner_key={cluster.get('owner_key')!r}: "
                    f"delete_position_uids {sorted(delete_uids)} "
                    f"!= snapshot - {{keeper}} "
                    f"{sorted(expected_deletes)}. Operator may have "
                    f"injected or omitted ids. Aborting."
                )

            # PR #60 round 3 fix (P1 dedupe): accounting-conflict
            # rejection. If the kept row and any delete candidate
            # disagree on a populated accounting field, the operator
            # is asking for an implicit merge that the script does
            # NOT support. Reject the cluster.
            _reject_owner_cluster_on_accounting_conflict(
                cluster, current_rows, keep_uid,
            )

            for uid in delete_uids:
                cur = conn.execute(
                    "DELETE FROM position_lifecycle WHERE position_uid = ?",
                    (uid,),
                )
                if cur.rowcount != 1:
                    raise _ApplyAborted(
                        f"DELETE position_uid={uid!r} affected "
                        f"{cur.rowcount} rows (expected exactly 1). "
                        f"The row may have been removed since --review."
                    )
                deleted_owner += 1

        for cluster in trades_clusters:
            expected_fp = cluster.get("review_fingerprint")
            current_rows = _refetch_trades_cluster(conn, cluster)
            current_fp = _cluster_fingerprint(current_rows)
            if expected_fp is not None and expected_fp != current_fp:
                raise _ApplyAborted(
                    f"order_id={cluster.get('order_id')!r} cluster "
                    f"mutated between --review and --apply (snapshot "
                    f"fingerprint mismatch). Re-run --review."
                )

            # Partition validation (round 4 keeper-required form).
            snapshot_ids = {r["id"] for r in cluster["rows"]}
            keep_id = cluster.get("keep_trade_id")
            delete_ids = list(cluster.get("delete_trade_ids", []))
            if keep_id is None:
                raise _ApplyAborted(
                    f"order_id={cluster.get('order_id')!r}: "
                    f"keep_trade_id is null. Every cluster must name "
                    f"exactly one surviving row; refusing to delete "
                    f"the entire cluster."
                )
            if keep_id not in snapshot_ids:
                raise _ApplyAborted(
                    f"order_id={cluster.get('order_id')!r}: "
                    f"keep_trade_id={keep_id} is not in the reviewed "
                    f"snapshot {sorted(snapshot_ids)}. Operator may "
                    f"have edited the keeper to an unrelated id. "
                    f"Aborting."
                )
            if len(delete_ids) != len(set(delete_ids)):
                raise _ApplyAborted(
                    f"order_id={cluster.get('order_id')!r}: "
                    f"delete_trade_ids contains duplicates; aborting."
                )
            if keep_id in delete_ids:
                raise _ApplyAborted(
                    f"order_id={cluster.get('order_id')!r}: keeper "
                    f"{keep_id} also appears in delete list; "
                    f"aborting."
                )
            expected_deletes = snapshot_ids - {keep_id}
            if set(delete_ids) != expected_deletes:
                raise _ApplyAborted(
                    f"order_id={cluster.get('order_id')!r}: "
                    f"delete_trade_ids {sorted(delete_ids)} != "
                    f"snapshot - {{keeper}} "
                    f"{sorted(expected_deletes)}. Operator may have "
                    f"injected or omitted ids. Aborting."
                )

            # Accounting-conflict rejection (P1 dedupe).
            _reject_trades_cluster_on_accounting_conflict(
                cluster, current_rows, keep_id,
            )

            for tid in delete_ids:
                cur = conn.execute(
                    "DELETE FROM trades WHERE id = ?", (tid,),
                )
                if cur.rowcount != 1:
                    raise _ApplyAborted(
                        f"DELETE trades.id={tid} affected "
                        f"{cur.rowcount} rows (expected exactly 1). "
                        f"The row may have been removed since --review."
                    )
                deleted_trades += 1

        # In-transaction post-condition: every duplicate cluster must
        # be gone before we commit. If the decisions file didn't cover
        # every cluster (e.g., a new duplicate appeared between
        # --review and --apply, or the operator forgot a cluster), the
        # foundation will fail preflight on the next bot startup.
        # Catch it here while we can still roll back cleanly.
        owner_residual = detect_owner_key_duplicates(conn)
        trades_residual = detect_trades_order_id_duplicates(conn)
        if owner_residual or trades_residual:
            raise _ApplyAborted(
                f"Post-apply rescan still finds duplicates: "
                f"{len(owner_residual)} owner_key cluster(s), "
                f"{len(trades_residual)} trades cluster(s). The "
                f"decisions file did not cover everything. Roll back "
                f"and regenerate the file."
            )

        conn.commit()
    except _ApplyAborted as abort:
        conn.rollback()
        print(f"ABORT: {abort}")
        return 2
    except Exception as exc:
        conn.rollback()
        print(f"ERROR: apply failed, rolled back: {exc}")
        return 2

    print(f"Applied: deleted {deleted_owner} position_lifecycle row(s), "
          f"{deleted_trades} trades row(s).")
    print("Post-apply rescan: clean. Safe to restart the bot.")
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
