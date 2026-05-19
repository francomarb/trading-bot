#!/usr/bin/env python3
"""
Strategy Health & Edge review CLI (PLAN 11.10e).

Operator-facing entry point for on-demand health reports. The same
function used by the forward_test.py Sunday EOD hook (added in
11.10g) is reachable here via `--window weekly`.

Per the v1 invariant (design §1.2): this script ONLY writes a
markdown report file and (optionally) dispatches alerts. It does
NOT modify trading state in any way.

Usage:
    /Users/franco/trading-bot/venv/bin/python scripts/strategy_health_review.py --window weekly
    /Users/franco/trading-bot/venv/bin/python scripts/strategy_health_review.py --window monthly
    /Users/franco/trading-bot/venv/bin/python scripts/strategy_health_review.py --window weekly --strategy donchian_breakout
    /Users/franco/trading-bot/venv/bin/python scripts/strategy_health_review.py --window weekly --dry-run

Dry-run prints the report to stdout and skips both file write AND
alert dispatch — safe for previewing what the operator would see.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from reporting.alerts import AlertDispatcher, LogFileBackend  # noqa: E402
from reporting.logger import TradeLogger  # noqa: E402
from strategies.health.reviewer import (  # noqa: E402
    render_markdown,
    run_review,
    window_from_args,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Strategy Health & Edge review (PLAN 11.10e)."
    )
    parser.add_argument(
        "--window",
        choices=["weekly", "monthly", "yearly"],
        required=True,
        help="Time window for the assessment.",
    )
    parser.add_argument(
        "--strategy",
        action="append",
        default=None,
        help=(
            "Limit assessment to a single strategy. Repeatable. "
            "Default: all strategies in STRATEGY_MIN_TRADES_FOR_VERDICT."
        ),
    )
    parser.add_argument(
        "--end-date",
        type=str,
        default=None,
        help=(
            "End date YYYY-MM-DD for the assessment window. "
            "Defaults to today."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Print the report to stdout; skip file write and alert "
            "dispatch. Safe for operator preview."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=None,
        help="Override report output directory (default data/health_reports/).",
    )
    args = parser.parse_args(argv)

    end_date = (
        date.fromisoformat(args.end_date)
        if args.end_date
        else date.today()
    )
    window = window_from_args(args.window, end_date=end_date)

    logger.info(
        f"strategy_health_review: window={args.window} "
        f"period={window.period_start}..{window.period_end} "
        f"strategies={args.strategy or 'all'} dry_run={args.dry_run}"
    )

    trade_logger = TradeLogger()
    conn = trade_logger._ensure_db()
    try:
        # Dispatcher uses LogFileBackend by default; Telegram backend
        # is added by forward_test.py when the bot runs in production.
        # On-demand CLI runs don't ship Telegram by default to avoid
        # sending duplicate alerts when the operator is debugging.
        dispatcher = AlertDispatcher() if not args.dry_run else None
        report_path, bundles = run_review(
            window,
            conn=conn,
            dispatcher=dispatcher,
            output_dir=args.output_dir,
            strategies=args.strategy,
            dry_run=args.dry_run,
        )

        if args.dry_run:
            # Re-render and print to stdout for inspection.
            print(render_markdown(bundles, window))
        else:
            print(f"report written: {report_path}")

        # Always print a one-line summary to stdout for shell scripting.
        from strategies.health.reports import EdgeVerdict
        n_neg = sum(
            1 for b in bundles
            if b.edge.verdict == EdgeVerdict.NEGATIVE
        )
        n_below = sum(
            1 for b in bundles
            if b.edge.verdict == EdgeVerdict.BELOW_BENCHMARK
        )
        sys.stderr.write(
            f"strategy_health_review: {len(bundles)} strategies, "
            f"{n_neg} silent-killer alarms, "
            f"{n_below} below-benchmark verdicts\n"
        )
        # Exit code: 1 if any silent-killer alarm fired so operator
        # cron / CI can detect it.
        return 1 if n_neg > 0 else 0
    finally:
        trade_logger.close()


if __name__ == "__main__":
    sys.exit(main())
