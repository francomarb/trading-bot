#!/usr/bin/env python3
"""
One-shot audit: how far back does each ai_bigtech symbol have data, and which
regime years can it actually participate in?

Side-effect: backfills the project's parquet cache by requesting bars from
2018-11-01 -> now for every symbol plus SPY (regime context). The fetcher
only hits the API for missing ranges, so this is idempotent on a warm cache.
The deep start lets the API return whatever each symbol actually has — SPY
back to 2018-11-01, individual mega-caps to 2020-07-27 on IEX, with per-symbol
variation. (Switching the underlying feed to SIP unlocks bars back to
2016-01-04 for most names; see PR #50 and the feed-aware cache layout.)

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
# can warm up). Must stay in sync with scripts/donchian_trail_compare.py
# WINDOWS — when those change, update both.
REGIME_STARTS = [
    ("2021_melt_up", "2021-04-01"),  # matches compare script; chosen so
                                     # 200-SMA filter is populated by window
                                     # boundary given individual stocks' IEX
                                     # first-bar = 2020-07-27
    ("2022_bear",    "2022-01-01"),
    ("2023_rally",   "2023-01-01"),
    ("2024_rally",   "2024-01-01"),
]
WARMUP_TRADING_DAYS = 50  # matches compare script's slice_window default


def main() -> int:
    logger.remove()
    logger.add(sys.stderr, level="INFO", format="<level>{level: <8}</level> | {message}")

    symbols = list(UNIVERSES["ai_bigtech"])
    # Probe deep — let the API return whatever it has per symbol. The Alpaca
    # IEX paper feed depth varies per symbol: SPY back to 2018-11-01, most
    # individual ai_bigtech mega-caps to 2020-07-27, later-listed names at
    # their listing dates. A wide start range surfaces the true first bar
    # rather than gating us to whatever the previous job happened to fetch.
    # See feedback_audit_reachable_data_first.md in user memory.
    start = datetime(2018, 11, 1, tzinfo=timezone.utc)
    end = datetime.now(timezone.utc)

    # SPY is required by scripts/donchian_trail_compare.py for per-bar regime
    # classification. PR #49 follow-up flagged that the documented
    # reproduction path didn't backfill it. Fetch it here alongside the
    # universe so a clean-cache reviewer can reproduce in one command.
    try:
        spy_df, _ = fetch_symbol("SPY", start, end, "1Day", use_cache=True)
        logger.info(
            f"SPY backfilled: {len(spy_df)} bars from {spy_df.index[0].date()} "
            f"to {spy_df.index[-1].date()}"
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"SPY backfill failed — {exc}")

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
        f"- Universe: ai_bigtech ({len(symbols)} symbols) + SPY (regime context)",
        f"- Warmup requirement: {WARMUP_TRADING_DAYS} trading days before window start (~{int(WARMUP_TRADING_DAYS*1.4)} calendar days)",
        f"- Note: Alpaca IEX paper-feed depth varies per symbol. SPY back to "
        f"2018-11-01, most individual ai_bigtech mega-caps to 2020-07-27, "
        f"later-listed names at their listing dates. Pre-2020 stock-level "
        f"testing would need a different vendor (Polygon / yfinance / paid "
        f"Alpaca extended history) — not a SIP subscription, which is a "
        f"different axis.\n",
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
