"""Repair single-leg exit rows whose realized P&L was booked against a
corrupted entry basis.

Two historical trade-log defects (fixed in ``reporting/logger.py`` on
2026-07-12) wrote wrong ``realized_pnl`` on stop/exit rows:

  1. **Reference-price fallback** — when the entry row's
     ``avg_fill_price`` was still NULL at exit time, the open-state
     replay silently fell back to ``entry_reference_price`` and the
     exit writer booked realized P&L against the strategy's decision
     price instead of the broker fill (trades 285 PWR, 288 GOOG,
     290 TSLA, 301 ARM).

  2. **Position-blend replay** — the replay walked rows in insertion
     (id) order, but reconciliation backfills exit rows with higher
     ids and earlier execution timestamps, so adjacent positions of
     the same symbol were blended into one weighted basis (trade 320
     QCOM, which also inherited the earlier position's entry context).

This script re-walks the trade log with the FIXED chronological replay
(shared code: ``reporting.logger.replay_single_leg_rows`` — the repair
cannot drift from production logic) and, for every single-leg sell row,
recomputes what should have been booked from broker-fill basis only.
Rows whose recorded values differ are reported and, with ``--apply``,
corrected in place (realized_pnl, r_multiple, and the entry-context
columns that the blend corrupted).

Rows whose true basis is still unknowable (entry fill never backfilled,
basis source is reference/mixed) are reported as SKIPPED and never
touched.

Modes::

    # Dry run (default) — read-only report of every discrepancy
    venv/bin/python scripts/repair_stop_fill_pnl.py

    # Apply — backs up the DB file first, then updates in one
    # transaction. STOP THE BOT FIRST (./stop_bot.sh): the allocator's
    # running P&L / HWM state rehydrates from these rows on the next
    # engine start, so repair while stopped, then start again.
    venv/bin/python scripts/repair_stop_fill_pnl.py --apply

    # Non-default database
    venv/bin/python scripts/repair_stop_fill_pnl.py --db data/trades_live.db

The script is idempotent: a second run after ``--apply`` reports no
remaining discrepancies.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from reporting.logger import (  # noqa: E402
    ENTRY_BASIS_BROKER_FILL,
    _contract_multiplier,
    replay_single_leg_rows,
)

# A cent of tolerance — float round-trips through SQLite must not
# produce phantom findings on healthy rows.
PNL_TOLERANCE = 0.01
R_TOLERANCE = 1e-6

# Entry-context columns the position-blend defect corrupted on sell
# rows (trade 320 carried the PREVIOUS position's context). Repaired
# alongside realized_pnl from the same replay state.
#
# entry_reference_price is NOT in this tuple: its semantics differ per
# writer. log_stop_fill rows (order_type='stop') store the ENTRY's
# reference price and are repairable; build_close_record rows
# (order_type='market', reason 'exit signal' etc.) store the EXIT's
# modeled benchmark price in the same column, which the replay state
# must never overwrite.
_CONTEXT_COLUMNS = (
    "entry_timestamp",
    "initial_stop_loss",
    "initial_risk_per_share",
    "initial_risk_dollars",
)


@dataclass
class Finding:
    """One sell row whose recorded values differ from replayed truth."""

    row_id: int
    symbol: str
    strategy: str
    reason: str
    changes: dict[str, tuple[Any, Any]] = field(default_factory=dict)

    @property
    def pnl_delta(self) -> float:
        if "realized_pnl" not in self.changes:
            return 0.0
        old, new = self.changes["realized_pnl"]
        return float(new or 0.0) - float(old or 0.0)


@dataclass
class Skip:
    """One sell row that could not be verified against a broker-fill basis."""

    row_id: int
    symbol: str
    strategy: str
    reason: str
    why: str


def _differs(old: Any, new: Any, tolerance: float) -> bool:
    if old is None and new is None:
        return False
    if old is None or new is None:
        return True
    return abs(float(old) - float(new)) > tolerance


def scan(conn: sqlite3.Connection) -> tuple[list[Finding], list[Skip]]:
    """Re-walk the trade log and diff every sell row against replayed truth."""
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, timestamp, symbol, strategy, side, qty, filled_qty, "
        "stop_price, avg_fill_price, entry_reference_price, "
        "initial_stop_loss, initial_risk_per_share, initial_risk_dollars, "
        "entry_timestamp, realized_pnl, r_multiple, reason, status, "
        "order_type "
        "FROM trades "
        "WHERE status IN ('filled', 'partial') "
        "AND (position_type IS NULL OR position_type != 'spread')"
    ).fetchall()

    findings: list[Finding] = []
    skips: list[Skip] = []

    def on_sell(row: sqlite3.Row, state: dict[str, Any]) -> None:
        qty_raw = row["filled_qty"] if row["filled_qty"] is not None else row["qty"]
        qty = float(qty_raw or 0.0)
        if qty <= 0 or row["avg_fill_price"] is None:
            # log_external_close markers (qty=0, no fill) and unfilled
            # rows carry no P&L to verify.
            return
        basis = state.get("entry_fill_price")
        source = state.get("entry_fill_price_source")
        if basis is None or float(basis) <= 0 or float(state.get("open_qty") or 0.0) <= 0:
            if row["realized_pnl"] is not None:
                skips.append(Skip(
                    row_id=int(row["id"]),
                    symbol=row["symbol"],
                    strategy=row["strategy"],
                    reason=row["reason"],
                    why="no open entry basis at this point in the replay",
                ))
            return
        if source != ENTRY_BASIS_BROKER_FILL:
            skips.append(Skip(
                row_id=int(row["id"]),
                symbol=row["symbol"],
                strategy=row["strategy"],
                reason=row["reason"],
                why=f"entry basis source is '{source}', not a broker fill "
                    "— true P&L unknowable until the entry fill is backfilled",
            ))
            return

        multiplier = _contract_multiplier(row["symbol"])
        expected_pnl = (float(row["avg_fill_price"]) - float(basis)) * qty * multiplier
        risk_per_share = state.get("initial_risk_per_share")
        expected_risk_dollars = (
            float(risk_per_share) * qty * multiplier
            if risk_per_share is not None
            else None
        )
        expected_r = (
            expected_pnl / expected_risk_dollars
            if expected_risk_dollars and expected_risk_dollars > 0
            else None
        )
        expected_context = {
            "entry_timestamp": state.get("entry_timestamp"),
            "initial_stop_loss": state.get("initial_stop_loss"),
            "initial_risk_per_share": risk_per_share,
            "initial_risk_dollars": expected_risk_dollars,
        }

        changes: dict[str, tuple[Any, Any]] = {}
        if _differs(row["realized_pnl"], expected_pnl, PNL_TOLERANCE):
            changes["realized_pnl"] = (row["realized_pnl"], expected_pnl)
            # r_multiple must stay consistent with realized_pnl — when
            # the P&L is corrected, the recorded R is recomputed (or
            # honestly NULLed when the risk anchor is unknown).
            if _differs(row["r_multiple"], expected_r, R_TOLERANCE):
                changes["r_multiple"] = (row["r_multiple"], expected_r)
            for col in _CONTEXT_COLUMNS:
                expected_value = expected_context[col]
                if col == "entry_timestamp":
                    if (row[col] or None) != (expected_value or None):
                        changes[col] = (row[col], expected_value)
                elif _differs(row[col], expected_value, R_TOLERANCE):
                    # Never NULL a populated context column — only the
                    # P&L/R pair must stay internally consistent.
                    if expected_value is not None or row[col] is None:
                        changes[col] = (row[col], expected_value)
            # See _CONTEXT_COLUMNS: only stop rows store the entry's
            # reference price in entry_reference_price.
            if str(row["order_type"] or "") == "stop":
                expected_reference = state.get("entry_reference_price")
                if _differs(row["entry_reference_price"], expected_reference, R_TOLERANCE):
                    if expected_reference is not None or row["entry_reference_price"] is None:
                        changes["entry_reference_price"] = (
                            row["entry_reference_price"], expected_reference,
                        )
        if changes:
            findings.append(Finding(
                row_id=int(row["id"]),
                symbol=row["symbol"],
                strategy=row["strategy"],
                reason=row["reason"],
                changes=changes,
            ))

    replay_single_leg_rows(rows, on_sell=on_sell)
    return findings, skips


def apply_findings(conn: sqlite3.Connection, findings: list[Finding]) -> None:
    """Apply all findings in one transaction. Raises if the DB is locked
    by another writer (e.g. the live bot) — stop the bot first."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for finding in findings:
            assignments = ", ".join(f"{col} = ?" for col in finding.changes)
            values = [new for (_, new) in finding.changes.values()]
            conn.execute(
                f"UPDATE trades SET {assignments} WHERE id = ?",
                [*values, finding.row_id],
            )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def _format_value(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Repair single-leg exit rows booked against a corrupted entry basis.",
    )
    parser.add_argument(
        "--db",
        default=str(ROOT / "data" / "trades.db"),
        help="Path to the trade-log SQLite DB (default: data/trades.db)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the corrections (default is a read-only dry run). "
             "Stop the bot first: ./stop_bot.sh",
    )
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"ERROR: database not found: {db_path}")
        return 2

    conn = sqlite3.connect(str(db_path))
    try:
        findings, skips = scan(conn)

        if not findings and not skips:
            print("All single-leg sell rows match the replayed broker-fill basis. Nothing to do.")
            return 0

        for finding in findings:
            print(
                f"\nrow {finding.row_id}  {finding.symbol}  "
                f"[{finding.strategy}]  reason={finding.reason}"
            )
            for col, (old, new) in finding.changes.items():
                print(f"    {col}: {_format_value(old)} -> {_format_value(new)}")

        for skip in skips:
            print(
                f"\nSKIPPED row {skip.row_id}  {skip.symbol}  "
                f"[{skip.strategy}]  reason={skip.reason}\n    {skip.why}"
            )

        if findings:
            per_strategy: dict[str, float] = {}
            for finding in findings:
                per_strategy[finding.strategy] = (
                    per_strategy.get(finding.strategy, 0.0) + finding.pnl_delta
                )
            print("\nPer-strategy realized P&L delta (new - old):")
            for strategy, delta in sorted(per_strategy.items()):
                print(f"    {strategy}: {delta:+.2f}")

        if not args.apply:
            print(
                f"\nDRY RUN — no changes written. "
                f"{len(findings)} row(s) would be repaired, "
                f"{len(skips)} skipped. Re-run with --apply (bot stopped)."
            )
            return 1 if findings else 0

        if not findings:
            print("\nNo repairable rows. Nothing written.")
            return 0

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = db_path.with_name(f"{db_path.name}.pre_repair_{stamp}.bak")
        shutil.copy2(db_path, backup)
        print(f"\nBackup written: {backup}")

        apply_findings(conn, findings)
        print(f"Repaired {len(findings)} row(s).")
        print(
            "\nNext step: start (or recycle) the bot — the allocator's "
            "running P&L / HWM / trade-count state rehydrates from these "
            "rows on startup, so the sleeve drawdown gate picks up the "
            "corrected totals automatically. engine_state.json needs no "
            "manual edit."
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
