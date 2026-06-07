#!/usr/bin/env python3
"""
One-shot audit: how far back does each ai_bigtech symbol have data, and which
regime years can it actually participate in?

Side-effect: backfills the project's parquet cache by requesting
2021-01-01 -> now for every symbol. The fetcher only hits the API for
missing ranges, so this is idempotent on a warm cache.

Output: a markdown table to stdout AND to
logs/backtests/<timestamp>_donchian_history_audit.md.

Run:
    venv/bin/python scripts/audit_donchian_history.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.fetcher import fetch_symbol  # noqa: E402
from scripts.backtest_bollinger_squeeze import UNIVERSES  # noqa: E402


# Regime-window starts. A symbol "participates" in a window if its earliest
# bar is at least `warmup_trading_days` before the window start (so indicators
# can warm up).
REGIME_STARTS = [
    ("2021_melt_up", "2021-01-01"),
    ("2022_bear",    "2022-01-01"),
    ("2023_rally",   "2023-01-01"),
    ("2024_rally",   "2024-01-01"),
]
WARMUP_TRADING_DAYS = 60


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{level: <8}</level> | {message}")

    symbols = list(UNIVERSES["ai_bigtech"])
    start = datetime(2021, 1, 1, tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)

    rows: list[dict] = []
    for sym in symbols:
        try:
            df, stats = fetch_symbol(sym, start, end, "1Day", use_cache=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{sym}: fetch failed — {exc}")
            rows.append({"symbol": sym, "first_bar": None, "n_bars": 0})
            continue
        if df is None or df.empty:
            rows.append({"symbol": sym, "first_bar": None, "n_bars": 0})
            continue
        rows.append({
            "symbol": sym,
            "first_bar": df.index[0].date(),
            "n_bars": len(df),
        })

    # For each symbol, work out which regime windows it can participate in.
    enriched: list[dict] = []
    for row in rows:
        first = row["first_bar"]
        flags: dict[str, str] = {}
        if first is None:
            for name, _ in REGIME_STARTS:
                flags[name] = "—"
        else:
            for name, ws in REGIME_STARTS:
                start_ts = pd.Timestamp(ws).date()
                # Need first_bar at least WARMUP_TRADING_DAYS calendar days
                # before window start (approximate: 60 trading days ≈ 85 cal days).
                cutoff = pd.Timestamp(ws) - pd.Timedelta(days=int(WARMUP_TRADING_DAYS * 1.4))
                flags[name] = "✓" if pd.Timestamp(first) <= cutoff else "✗"
        enriched.append({**row, **flags})

    # Sort by first_bar (None last)
    enriched.sort(key=lambda r: (r["first_bar"] is None, r["first_bar"] or pd.NaT))

    lines = [
        f"# ai_bigtech historical coverage audit\n",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}\n",
        f"- Universe: ai_bigtech ({len(symbols)} symbols)",
        f"- Warmup requirement: {WARMUP_TRADING_DAYS} trading days before window start (~{int(WARMUP_TRADING_DAYS*1.4)} calendar days)",
        f"- Note: Alpaca IEX paper feed serves data back to ~2021-01-04 only; "
        f"pre-2021 windows are not accessible without a SIP subscription.\n",
        "| Symbol | First bar | Bars | 2021 melt-up | 2022 bear | 2023 rally | 2024 rally |",
        "|--------|-----------|-----:|:---:|:---:|:---:|:---:|",
    ]
    for r in enriched:
        first_str = str(r["first_bar"]) if r["first_bar"] else "—"
        lines.append(
            f"| {r['symbol']:<6} | {first_str:<10} | {r['n_bars']:>4} | "
            f"{r['2021_melt_up']} | {r['2022_bear']} | "
            f"{r['2023_rally']} | {r['2024_rally']} |"
        )

    # Counts row
    counts = {name: sum(1 for r in enriched if r[name] == "✓") for name, _ in REGIME_STARTS}
    lines.append(
        f"| **TOTAL** | | | "
        f"**{counts['2021_melt_up']}** | **{counts['2022_bear']}** | "
        f"**{counts['2023_rally']}** | **{counts['2024_rally']}** |"
    )

    report = "\n".join(lines)
    print(report)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = ROOT / "logs" / "backtests" / f"{ts}_donchian_history_audit.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    logger.info(f"wrote report to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
