#!/usr/bin/env python3
"""Run the SIP leveraged-trend confirmation grid and write research artifacts."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from backtest.leveraged_trend import (  # noqa: E402
    RESEARCH_PAIRS,
    aggregate_parameter_study,
    buy_and_hold_study,
    candidate_period_study,
    fetch_pair_frame,
    parameter_study,
    write_study_report,
)
from backtest.runner import BacktestConfig  # noqa: E402


def _positive_int_list(value: str) -> list[int]:
    """Parse a comma-separated, unique list of positive integers."""
    try:
        values = [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected comma-separated integers") from exc
    if not values or any(item < 1 for item in values):
        raise argparse.ArgumentTypeError("values must be positive integers")
    return list(dict.fromkeys(values))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", default="2008-01-01")
    parser.add_argument(
        "--end",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="exclusive-ish API boundary; in-progress daily bars are removed",
    )
    parser.add_argument("--sma-length", type=int, default=200)
    parser.add_argument(
        "--entry-days",
        type=_positive_int_list,
        default=_positive_int_list("1,2,3,5,7,10"),
    )
    parser.add_argument(
        "--exit-days",
        type=_positive_int_list,
        default=_positive_int_list("1,2,3,5"),
    )
    parser.add_argument("--initial-cash", type=float, default=100_000.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    parser.add_argument(
        "--csv",
        type=Path,
        default=PROJECT_ROOT / "docs/reports/leveraged_trend_grid.csv",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "docs/reports/leveraged_trend_backtest.md",
    )
    return parser.parse_args()


def main() -> int:
    """Fetch SIP bars, execute the full grid, and save CSV/Markdown outputs."""
    args = _parse_args()
    if args.sma_length < 1:
        raise SystemExit("--sma-length must be positive")

    try:
        start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
        end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise SystemExit(f"invalid ISO date: {exc}") from exc
    if start >= end:
        raise SystemExit("--start must precede --end")

    now = datetime.now(timezone.utc)
    frames = {
        pair: fetch_pair_frame(pair, start=start, end=end, now=now)
        for pair in RESEARCH_PAIRS
    }
    config = BacktestConfig(
        initial_cash=args.initial_cash,
        slippage_bps=args.slippage_bps,
        commission_per_trade=0.0,
    )
    study = parameter_study(
        frames,
        entry_days_values=args.entry_days,
        exit_days_values=args.exit_days,
        sma_length=args.sma_length,
        config=config,
    )
    aggregate = aggregate_parameter_study(study)
    benchmarks = buy_and_hold_study(
        frames, sma_length=args.sma_length, config=config
    )
    candidate_periods = candidate_period_study(
        frames,
        periods={
            "2018 Q4 selloff": ("2018-09-01", "2019-01-31"),
            "2020 COVID crash/rebound": ("2020-02-01", "2020-06-30"),
            "2022 bear market": ("2022-01-01", "2022-12-31"),
        },
        sma_length=args.sma_length,
        entry_days=5,
        exit_days=2,
        config=config,
    )

    args.csv.parent.mkdir(parents=True, exist_ok=True)
    study.to_csv(args.csv, index=False)
    report = write_study_report(
        study,
        aggregate,
        output_path=args.report,
        config=config,
        benchmarks=benchmarks,
        candidate_periods=candidate_periods,
    )

    print(f"SIP study complete: {len(study)} pair/configuration rows")
    print(f"CSV: {args.csv}")
    print(f"Report: {report}")
    print("\nTop cross-pair configurations:")
    print(aggregate.head(10).to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
