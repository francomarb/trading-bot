#!/usr/bin/env python3
"""Render temporary option-stop diagnostics chronologically."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config import settings
from engine.option_stop_audit import OptionStopReplaceAuditStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Dump SPY options-reversion stop diagnostic evidence."
    )
    parser.add_argument(
        "--db",
        default=settings.OPTION_STOP_REPLACE_AUDIT_DB_PATH,
        help="Diagnostic SQLite path.",
    )
    parser.add_argument("--occ", help="Filter by exact OCC symbol.")
    parser.add_argument(
        "--correlation-id",
        help="Filter by one replacement correlation id.",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Include complete broker and stream JSON payloads.",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    path = Path(args.db)
    if not path.exists():
        raise SystemExit(f"diagnostic DB does not exist: {path}")

    store = OptionStopReplaceAuditStore(path)
    try:
        records = store.read_records(
            occ_symbol=args.occ,
            correlation_id=args.correlation_id,
        )
    finally:
        store.close()

    for record in records:
        payload = record["payload"]
        print(
            f"{record['recorded_at']}  {record['occ_symbol']}  "
            f"{record['record_type']}  order={record['order_id']} "
            f"correlation={record['correlation_id']}"
        )
        if args.raw:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            summary = {
                key: payload.get(key)
                for key in (
                    "reason",
                    "desired_stop_price",
                    "requested_stop_price",
                    "client_order_id",
                    "position_current_price",
                    "hwm_premium",
                    "qty",
                    "filled_qty",
                    "price",
                    "avg_fill_price",
                    "stop_price",
                    "adverse_slippage_bps",
                    "status",
                    "replace_call_latency_ms",
                    "submit_call_latency_ms",
                    "error",
                )
                if payload.get(key) is not None
            }
            print(f"  {json.dumps(summary, sort_keys=True, default=str)}")

    if not records:
        print("No matching option-stop diagnostics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
