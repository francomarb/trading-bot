#!/usr/bin/env python3
"""
Go/No-Go Checker — decides whether paper trading results justify live capital.

Reads the trade database, pairs buy/sell fills into round-trip trades,
computes the five go/no-go metrics, checks operational criteria, and
prints a final verdict.

Usage:
    python scripts/gonogo.py                    # default: data/trades.db
    python scripts/gonogo.py --db path/to.db    # custom DB path
    python scripts/gonogo.py --min-trades 30    # override minimum trades
    python scripts/gonogo.py --json             # machine-readable output

Exit codes:
    0 — GO (all gates passed)
    1 — NO-GO (one or more gates failed)

Architecture reference (architecture.md §Go/No-Go Framework):
    1. Minimum 50 closed trades
    2. Paper trading spans at least 4 weeks
    3. All five metrics meet thresholds
    4. Bot ran 72+ hours continuously without crashes
    5. Risk manager daily halt never triggered unintentionally
    6. Paper ↔ Live toggle tested and working
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

import numpy as np

from config import settings
from reporting.logger import TradeLogger
from reporting.metrics import (
    MAX_DRAWDOWN_THRESHOLD,
    PROFIT_FACTOR_THRESHOLD,
    SHARPE_THRESHOLD,
    WIN_RATE_THRESHOLD,
    AVG_WIN_LOSS_THRESHOLD,
    MetricsSnapshot,
    compute_metrics,
)


# ── Round-trip pairing ──────────────────────────────────────────────────────


def pair_round_trips(trades: list[dict]) -> list[float]:
    """
    Pair buy/sell fills into round-trip P&Ls.

    Scans trades chronologically. When a 'buy' fill is seen for a
    (symbol, strategy) key, it's pushed onto an open-positions stack.
    When a 'sell' fill is seen, it's matched FIFO against the stack
    and the P&L is computed: (exit_price - entry_price) * qty.

    Returns a list of per-trade dollar P&L values.
    """
    # open_positions: (symbol, strategy) → list of (price, qty)
    open_positions: dict[tuple[str, str], list[tuple[float, int]]] = {}
    pnls: list[float] = []

    for t in trades:
        status = t.get("status", "")
        if status != "filled":
            continue

        symbol = t.get("symbol", "")
        strategy = t.get("strategy", "")
        side = t.get("side", "")
        key = (symbol, strategy)

        try:
            price = float(t["avg_fill_price"])
            qty = int(float(t["qty"]))
        except (ValueError, TypeError, KeyError):
            continue

        if side == "buy":
            open_positions.setdefault(key, []).append((price, qty))
        elif side == "sell":
            stack = open_positions.get(key, [])
            remaining_qty = qty
            while remaining_qty > 0 and stack:
                entry_price, entry_qty = stack[0]
                matched = min(remaining_qty, entry_qty)
                pnl = (price - entry_price) * matched
                pnls.append(pnl)
                remaining_qty -= matched
                if matched == entry_qty:
                    stack.pop(0)
                else:
                    stack[0] = (entry_price, entry_qty - matched)

    return pnls


# ── Operational checks ──────────────────────────────────────────────────────


@dataclass
class OperationalCheck:
    """Result of an operational (non-metric) gate."""

    name: str
    passed: bool
    detail: str


def check_trading_span(trades: list[dict], min_weeks: int = 4) -> OperationalCheck:
    """Check that paper trading spans at least `min_weeks` weeks."""
    timestamps = []
    for t in trades:
        ts = t.get("timestamp", "")
        if ts:
            try:
                timestamps.append(datetime.fromisoformat(ts))
            except ValueError:
                pass

    if len(timestamps) < 2:
        return OperationalCheck(
            name="trading_span",
            passed=False,
            detail="fewer than 2 timestamped trades",
        )

    earliest = min(timestamps)
    latest = max(timestamps)
    span = latest - earliest
    required = timedelta(weeks=min_weeks)

    return OperationalCheck(
        name="trading_span",
        passed=span >= required,
        detail=(
            f"span={span.days} days "
            f"({'PASS' if span >= required else 'FAIL'}: "
            f"need >= {required.days} days)"
        ),
    )


def check_trade_count(n_trades: int, min_trades: int = 50) -> OperationalCheck:
    """Check minimum closed trade count."""
    return OperationalCheck(
        name="trade_count",
        passed=n_trades >= min_trades,
        detail=f"{n_trades} trades ({'PASS' if n_trades >= min_trades else 'FAIL'}: need >= {min_trades})",
    )


def check_var(pnls: list[float]) -> OperationalCheck:
    """
    Check that the 99% trade-level VaR does not exceed the hard dollar loss cap.

    VaR here is the 1st-percentile worst single-trade P&L — i.e. the loss
    expected to be exceeded in only 1% of trades. Requires at least 100 closed
    trades for a meaningful estimate (the 1st percentile of fewer than 100
    observations is just the single worst trade, which is noise not statistics);
    returns PASS (with a note) if fewer.

    Gate: |99% trade VaR| <= HARD_DOLLAR_LOSS_CAP
    A single trade losing more than the configured cap in 1% of cases means the
    per-trade stop-loss sizing is not protecting the hard dollar limit reliably.
    """
    if len(pnls) < 100:
        return OperationalCheck(
            name="var_99",
            passed=True,
            detail=f"insufficient trades for VaR ({len(pnls)} < 100) — skipped",
        )

    var_99 = float(np.percentile(pnls, 1))  # 1st percentile (worst losses)
    cap = settings.HARD_DOLLAR_LOSS_CAP
    passed = abs(var_99) <= cap
    return OperationalCheck(
        name="var_99",
        passed=passed,
        detail=(
            f"99% trade VaR = ${var_99:.2f} | cap = ${cap:.2f} "
            f"({'PASS' if passed else 'FAIL'})"
        ),
    )


# ── Main ────────────────────────────────────────────────────────────────────


def run_gonogo(
    db_path: str,
    *,
    min_trades: int = 50,
    min_weeks: int = 4,
) -> tuple[bool, MetricsSnapshot, list[OperationalCheck]]:
    """
    Run the full go/no-go evaluation.

    Returns (go, metrics_snapshot, operational_checks).
    """
    tl = TradeLogger(path=db_path)
    all_trades = tl.read_all()

    # 1. Pair round-trips.
    pnls = pair_round_trips(all_trades)

    # 2. Compute metrics.
    metrics = compute_metrics(pnls)

    # 3. Check metric gates.
    metrics_go, metric_reasons = metrics.meets_go_thresholds(min_trades=min_trades)

    # 4. Operational checks.
    ops: list[OperationalCheck] = [
        check_trade_count(len(pnls), min_trades),
        check_trading_span(all_trades, min_weeks),
        check_var(pnls),
    ]

    ops_go = all(op.passed for op in ops)

    go = metrics_go and ops_go
    return go, metrics, ops


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Go/No-Go checker for live trading readiness.",
    )
    parser.add_argument(
        "--db",
        default="data/trades.db",
        help="Path to the trade database (default: data/trades.db)",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=50,
        help="Minimum closed trades required (default: 50)",
    )
    parser.add_argument(
        "--min-weeks",
        type=int,
        default=4,
        help="Minimum trading span in weeks (default: 4)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output machine-readable JSON instead of human-readable report",
    )
    args = parser.parse_args()

    go, metrics, ops = run_gonogo(
        args.db, min_trades=args.min_trades, min_weeks=args.min_weeks
    )

    if args.json:
        result = {
            "go": go,
            "metrics": {
                "trade_count": metrics.trade_count,
                "sharpe_ratio": metrics.sharpe_ratio,
                "max_drawdown_pct": metrics.max_drawdown_pct,
                "profit_factor": metrics.profit_factor,
                "win_rate": metrics.win_rate,
                "avg_win_loss_ratio": metrics.avg_win_loss_ratio,
                "total_pnl": metrics.total_pnl,
            },
            "operational_checks": [
                {"name": op.name, "passed": op.passed, "detail": op.detail}
                for op in ops
            ],
        }
        print(json.dumps(result, indent=2))
    else:
        print(metrics.format_report())
        print()
        print("## Operational Checks")
        print()
        for op in ops:
            status = "PASS" if op.passed else "FAIL"
            print(f"  [{status}] {op.name}: {op.detail}")
        print()
        verdict = "GO" if go else "NO-GO"
        print(f"{'=' * 40}")
        print(f"  FINAL VERDICT: {verdict}")
        print(f"{'=' * 40}")

    sys.exit(0 if go else 1)


if __name__ == "__main__":
    main()
