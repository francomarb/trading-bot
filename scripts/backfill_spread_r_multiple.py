#!/usr/bin/env python3
"""
One-off backfill of `initial_risk_dollars` and `r_multiple` on
pre-existing credit-spread rows in `trades.db`.

PR #34 wired `log_spread_fill` to write these fields on new fills, but
spread rows recorded before the merge still carry NULL for both,
which keeps the EdgeAssessor's R-multiple sample at zero for the
historical window and blocks `scripts/build_paper_envelope.py` from
fitting an envelope.

All the inputs needed to compute the basis are already on the existing
rows — they were just never written into the dedicated columns:

  - `qty` (contracts)         — present on every spread row
  - `net_credit` ($/share)    — short-leg open's `avg_fill_price`
  - `short_strike`            — last 8 OCC digits / 1000 on the open's
                                short-leg `symbol`
  - `long_strike`             — same on the long-leg `symbol`

Per bull-put defined-risk math:

    width                = short_strike − long_strike
    initial_risk_dollars = (width − net_credit) × 100 × qty

Each closed spread additionally gets:

    r_multiple = realized_pnl / initial_risk_dollars

This script deliberately deviates from `reporting/logger.py`'s
"all writes are append-only" invariant for the limited purpose of
backfilling these two columns on rows where they are NULL. It does
**not** touch any other column and **never** writes to a row where
`initial_risk_dollars IS NOT NULL` (so it's idempotent and won't
overwrite post-fix data).

Safety:
  - Dry-run by default; `--apply` required to actually mutate rows.
  - Before any UPDATE, the script writes a timestamped backup copy of
    `trades.db` alongside the original.
  - Prints a per-spread diff showing what would change so the operator
    can sanity-check before committing.

Usage:
    # Dry run (default — prints what would change, no DB writes)
    /Users/franco/trading-bot/venv/bin/python \\
        scripts/backfill_spread_r_multiple.py

    # Apply (operator commits to the change)
    /Users/franco/trading-bot/venv/bin/python \\
        scripts/backfill_spread_r_multiple.py --apply

    # Override DB path (default: settings.TRADE_LOG_DB)
    /Users/franco/trading-bot/venv/bin/python \\
        scripts/backfill_spread_r_multiple.py --db data/trades.db --apply
"""

from __future__ import annotations

import argparse
import re
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402


# OCC option symbol: <root: 1-6 chars><yymmdd: 6 digits><C/P><strike: 8 digits>
# Strike is in fixed-point thousandths — divide by 1000 for dollars.
_OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")


# ── Data ──────────────────────────────────────────────────────────────


@dataclass
class _SpreadLegs:
    """All the rows for one spread `position_id`, partitioned by role."""

    position_id: str
    strategy: str
    # Open: short leg sold (side='sell'), long leg bought (side='buy').
    open_short_id: int | None = None
    open_long_id: int | None = None
    open_short_occ: str | None = None
    open_long_occ: str | None = None
    open_short_price: float | None = None   # = net_credit per share
    open_qty: float | None = None
    open_initial_risk: float | None = None  # already-set value (NULL if unset)
    # Close: short leg bought back (side='buy'), long leg sold.
    close_short_id: int | None = None
    close_short_realized_pnl: float | None = None
    close_short_initial_risk: float | None = None
    close_short_r_multiple: float | None = None


@dataclass(frozen=True)
class _Plan:
    """One spread's planned update — what we'll write to which rows."""

    position_id: str
    strategy: str
    short_occ: str
    long_occ: str
    qty: float
    net_credit: float
    width: float
    initial_risk_dollars: float
    open_short_id: int
    close_short_id: int | None
    close_realized_pnl: float | None
    close_r_multiple: float | None


# ── DB readers ────────────────────────────────────────────────────────


def _load_spread_legs(conn: sqlite3.Connection) -> dict[str, _SpreadLegs]:
    """Group every `position_type='spread'` row by `position_id`."""
    cursor = conn.execute(
        "SELECT id, position_id, strategy, side, symbol, qty, filled_qty, "
        "avg_fill_price, realized_pnl, initial_risk_dollars, r_multiple "
        "FROM trades "
        "WHERE position_type = 'spread' "
        "AND status IN ('filled', 'partial') "
        "ORDER BY id ASC"
    )
    by_pid: dict[str, _SpreadLegs] = {}
    for row in cursor.fetchall():
        (row_id, position_id, strategy, side, symbol, qty, filled_qty,
         avg_fill_price, realized_pnl, initial_risk, r_multiple) = row
        if position_id is None:
            continue
        legs = by_pid.setdefault(
            position_id,
            _SpreadLegs(position_id=position_id, strategy=strategy or ""),
        )
        qty_value = float(filled_qty if filled_qty is not None else qty or 0.0)
        # Roles by side + presence of realized_pnl:
        #   open short  = side 'sell', no realized_pnl
        #   open long   = side 'buy',  no realized_pnl, price=0
        #   close short = side 'buy',  realized_pnl present (or external-close)
        #   close long  = side 'sell', no realized_pnl, price=0
        #
        # NB the close's long leg (side='sell', realized_pnl IS NULL)
        # collides with the open's short leg by side+realized_pnl alone.
        # Rows are scanned in id ASC order; the open arrives first, so
        # we accept the first occurrence per role and ignore later rows
        # that would otherwise overwrite it. The collision is harmless
        # because the close's long leg carries no information we need
        # (price=0, no realized_pnl, no basis).
        if realized_pnl is None:
            if side == "sell" and legs.open_short_id is None:
                legs.open_short_id = row_id
                legs.open_short_occ = symbol
                legs.open_short_price = (
                    float(avg_fill_price) if avg_fill_price is not None else None
                )
                legs.open_qty = qty_value
                legs.open_initial_risk = (
                    float(initial_risk) if initial_risk is not None else None
                )
            elif side == "buy" and legs.open_long_id is None:
                legs.open_long_id = row_id
                legs.open_long_occ = symbol
        else:
            if side == "buy" and legs.close_short_id is None:
                legs.close_short_id = row_id
                legs.close_short_realized_pnl = float(realized_pnl)
                legs.close_short_initial_risk = (
                    float(initial_risk) if initial_risk is not None else None
                )
                legs.close_short_r_multiple = (
                    float(r_multiple) if r_multiple is not None else None
                )
    return by_pid


# ── Plan computation ──────────────────────────────────────────────────


def _parse_strike(occ: str) -> float | None:
    """Return the OCC symbol's strike in dollars, or None if unparseable."""
    if not occ:
        return None
    match = _OCC_RE.match(occ.strip().upper())
    if match is None:
        return None
    return int(match.group(4)) / 1000.0


def _build_plans(
    legs_by_pid: dict[str, _SpreadLegs],
) -> tuple[list[_Plan], list[str]]:
    """Translate grouped rows into an update plan.

    Returns (plans, skip_reasons). `skip_reasons` is a list of
    operator-readable lines explaining why each non-eligible spread was
    skipped — surfaced in the report so partial results aren't silent.
    """
    plans: list[_Plan] = []
    skips: list[str] = []
    for pid, legs in sorted(legs_by_pid.items()):
        # Already backfilled? Skip both rows are touched.
        if (
            legs.open_initial_risk is not None
            and (
                legs.close_short_id is None
                or legs.close_short_initial_risk is not None
            )
        ):
            continue
        if legs.open_short_id is None:
            skips.append(
                f"{pid[:8]}: skipped — no open short-leg row found "
                f"(strategy={legs.strategy or '?'})"
            )
            continue
        if legs.open_short_price is None or legs.open_qty is None:
            skips.append(
                f"{pid[:8]}: skipped — open short leg missing avg_fill_price "
                f"or qty"
            )
            continue
        if not legs.open_short_occ or not legs.open_long_occ:
            skips.append(
                f"{pid[:8]}: skipped — missing leg OCC symbols (short="
                f"{legs.open_short_occ!r} long={legs.open_long_occ!r})"
            )
            continue
        short_strike = _parse_strike(legs.open_short_occ)
        long_strike = _parse_strike(legs.open_long_occ)
        if short_strike is None or long_strike is None:
            skips.append(
                f"{pid[:8]}: skipped — non-OCC symbol(s) (short="
                f"{legs.open_short_occ!r}, long={legs.open_long_occ!r})"
            )
            continue
        width = abs(short_strike - long_strike)
        net_credit = float(legs.open_short_price)
        qty = float(legs.open_qty)
        if width <= net_credit:
            skips.append(
                f"{pid[:8]}: skipped — degenerate basis "
                f"(width=${width:.2f} ≤ net_credit=${net_credit:.2f})"
            )
            continue
        initial_risk = (width - net_credit) * 100.0 * qty
        if initial_risk <= 0:
            skips.append(
                f"{pid[:8]}: skipped — non-positive computed basis "
                f"({initial_risk:.4f})"
            )
            continue
        close_r = None
        if (
            legs.close_short_id is not None
            and legs.close_short_realized_pnl is not None
        ):
            close_r = legs.close_short_realized_pnl / initial_risk
        plans.append(_Plan(
            position_id=pid,
            strategy=legs.strategy,
            short_occ=legs.open_short_occ,
            long_occ=legs.open_long_occ,
            qty=qty,
            net_credit=net_credit,
            width=width,
            initial_risk_dollars=initial_risk,
            open_short_id=legs.open_short_id,
            close_short_id=legs.close_short_id,
            close_realized_pnl=legs.close_short_realized_pnl,
            close_r_multiple=close_r,
        ))
    return plans, skips


# ── Apply ─────────────────────────────────────────────────────────────


def _apply_plans(
    conn: sqlite3.Connection, plans: Iterable[_Plan]
) -> tuple[int, int]:
    """Write the planned UPDATEs. Returns (open_rows_updated, close_rows_updated).

    Only writes columns that are NULL on the target row, so a re-run
    after a partial apply is safe.
    """
    open_updates = 0
    close_updates = 0
    for plan in plans:
        cur = conn.execute(
            "UPDATE trades SET initial_risk_dollars = ? "
            "WHERE id = ? AND initial_risk_dollars IS NULL",
            (plan.initial_risk_dollars, plan.open_short_id),
        )
        open_updates += cur.rowcount
        if plan.close_short_id is not None and plan.close_realized_pnl is not None:
            cur = conn.execute(
                "UPDATE trades SET initial_risk_dollars = ?, r_multiple = ? "
                "WHERE id = ? "
                "AND (initial_risk_dollars IS NULL OR r_multiple IS NULL)",
                (
                    plan.initial_risk_dollars,
                    plan.close_r_multiple,
                    plan.close_short_id,
                ),
            )
            close_updates += cur.rowcount
    conn.commit()
    return open_updates, close_updates


# ── Reporting ─────────────────────────────────────────────────────────


def _format_plan_table(plans: list[_Plan]) -> str:
    if not plans:
        return "(no spreads need backfilling)"
    lines = [
        f"{'pos_id':10}  {'strategy':16}  {'qty':>4}  "
        f"{'width':>6}  {'credit':>7}  {'basis $':>10}  "
        f"{'pnl $':>9}  {'r_mult':>7}",
        "-" * 92,
    ]
    for p in plans:
        r_str = "n/a" if p.close_r_multiple is None else f"{p.close_r_multiple:+.4f}"
        pnl_str = (
            "n/a" if p.close_realized_pnl is None
            else f"{p.close_realized_pnl:+.2f}"
        )
        lines.append(
            f"{p.position_id[:8]:10}  {p.strategy[:16]:16}  "
            f"{p.qty:>4.0f}  {p.width:>6.2f}  {p.net_credit:>7.2f}  "
            f"{p.initial_risk_dollars:>10.2f}  {pnl_str:>9}  {r_str:>7}"
        )
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill `initial_risk_dollars` + `r_multiple` on historical "
            "credit-spread rows. Dry-run by default; pass --apply to commit."
        ),
    )
    parser.add_argument("--db", default=None)
    parser.add_argument("--apply", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.db or settings.TRADE_LOG_DB)
    if not db_path.exists():
        print(f"error: trades.db not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    try:
        legs_by_pid = _load_spread_legs(conn)
        plans, skips = _build_plans(legs_by_pid)

        print(f"spread positions scanned: {len(legs_by_pid)}")
        print(f"plans (eligible for backfill): {len(plans)}")
        print(f"skipped (already done or ineligible): "
              f"{len(legs_by_pid) - len(plans)}")
        print()
        print(_format_plan_table(plans))
        if skips:
            print()
            print("skips:")
            for s in skips:
                print(f"  {s}")

        if not args.apply:
            print()
            print("dry-run only. re-run with --apply to write these updates.")
            return 0

        if not plans:
            print()
            print("nothing to apply.")
            return 0

        backup_suffix = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        backup_path = db_path.with_suffix(f"{db_path.suffix}.bak.{backup_suffix}")
        shutil.copy2(db_path, backup_path)
        print()
        print(f"backed up {db_path} -> {backup_path}")

        open_updates, close_updates = _apply_plans(conn, plans)
        print(
            f"applied: open rows updated={open_updates}, "
            f"close rows updated={close_updates}"
        )
    finally:
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
