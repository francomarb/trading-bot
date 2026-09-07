#!/usr/bin/env python3
"""Generate the advisory per-strategy graduation evidence package."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from reporting.graduation import write_graduation_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=settings.TRADE_LOG_DB)
    parser.add_argument("--output-dir", default="logs/strategy_graduation")
    parser.add_argument(
        "--start-date",
        help="inclusive UTC close/mark/event date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--end-date",
        help="inclusive UTC close/mark/event date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--strategy",
        dest="strategies",
        action="append",
        help="include one strategy (repeat for more than one)",
    )
    parser.add_argument(
        "--reviewed-epochs",
        help="optional operator-reviewed JSON file classifying legacy intervals",
    )
    args = parser.parse_args()
    try:
        json_path, md_path = write_graduation_report(
            args.db,
            args.output_dir,
            reviewed_epochs_path=args.reviewed_epochs,
            start_date=args.start_date,
            end_date=args.end_date,
            strategies=args.strategies,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
