#!/usr/bin/env python3
"""Render the read-only PLAN 11.61 selected-versus-refused report."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from reporting.candidate_comparison import (  # noqa: E402
    SUPPORTED_STRATEGIES,
    build_candidate_comparison_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=settings.TRADE_LOG_DB)
    parser.add_argument(
        "--strategy",
        action="append",
        choices=SUPPORTED_STRATEGIES,
        help="repeat to select strategies; default is all supported strategies",
    )
    parser.add_argument("--output", help="write Markdown here instead of stdout")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        parser.error(f"database does not exist: {db_path}")
    conn = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        report = build_candidate_comparison_report(
            conn,
            strategies=args.strategy or SUPPORTED_STRATEGIES,
        )
    finally:
        conn.close()
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(output)
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
