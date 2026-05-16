"""
Throwaway analysis for PLAN 11.32 — entry price-cap sizing for Donchian.

For each Donchian (30/15) entry signal on the ai_bigtech universe over the
trailing 12 months, compute the realized "chase" the engine would have paid:

    open_chase_bps = (next_open / signal_close - 1) * 1e4
    high_chase_bps = (next_high / signal_close - 1) * 1e4   # intraday worst-case

`signal_close` is the bar whose close triggered the entry — that is the
reference price RiskManager would have used. `next_open` is the closest proxy
to where a MARKET order submitted at next session open would have filled.
`next_high` brackets the worst-case fill if the bot fires mid-session.

Output: distribution + percentiles + the worst N outliers. We are looking for
a cap that lets the bulk of normal breakouts through while killing the
QCOM-class (+1200 bps) outliers.

Run:
    /Users/franco/trading-bot/venv/bin/python \
        scripts/donchian_chase_distribution.py --years 1
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.fetcher import fetch_symbol  # noqa: E402
from indicators.technicals import add_atr  # noqa: E402
from scripts.backtest_bollinger_squeeze import UNIVERSES  # noqa: E402
from strategies.donchian_breakout import DonchianBreakout  # noqa: E402


def configure_logging(verbose: bool) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level, format="{time:HH:mm:ss} | {level: <7} | {message}")


def analyse_symbol(
    symbol: str, df: pd.DataFrame, entry_window: int, exit_window: int, atr_length: int
) -> pd.DataFrame:
    """Return one row per Donchian entry signal with chase metrics."""
    strategy = DonchianBreakout(entry_window=entry_window, exit_window=exit_window)
    signals = strategy.generate_signals(df, symbol=symbol)
    df = add_atr(df, atr_length)

    entries = signals.entries
    # Align next_open / next_high to the signal bar — entry would execute on bar t+1.
    next_open = df["open"].shift(-1)
    next_high = df["high"].shift(-1)

    rows = []
    for ts in entries.index[entries]:
        sig_close = float(df["close"].loc[ts])
        nopen = next_open.loc[ts]
        nhigh = next_high.loc[ts]
        atr = float(df[f"atr_{atr_length}"].loc[ts])
        if pd.isna(nopen) or pd.isna(nhigh) or sig_close <= 0 or atr <= 0:
            continue
        rows.append(
            {
                "symbol": symbol,
                "signal_ts": ts,
                "signal_close": sig_close,
                "next_open": float(nopen),
                "next_high": float(nhigh),
                "atr": atr,
                "open_chase_bps": (float(nopen) / sig_close - 1.0) * 1e4,
                "high_chase_bps": (float(nhigh) / sig_close - 1.0) * 1e4,
                "open_chase_atr": (float(nopen) - sig_close) / atr,
                "high_chase_atr": (float(nhigh) - sig_close) / atr,
            }
        )
    return pd.DataFrame(rows)


def summarise(df: pd.DataFrame, metric: str) -> str:
    s = df[metric].dropna()
    pcts = [50, 75, 90, 95, 97.5, 99]
    parts = [f"  n={len(s)}  mean={s.mean():7.1f}  max={s.max():8.1f}  min={s.min():8.1f}"]
    parts.append("  " + "  ".join(f"p{p}={s.quantile(p / 100):7.1f}" for p in pcts))
    return "\n".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", type=float, default=1.0)
    ap.add_argument("--entry-window", type=int, default=30)
    ap.add_argument("--exit-window", type=int, default=15)
    ap.add_argument("--atr-length", type=int, default=14)
    ap.add_argument("--top-n", type=int, default=15, help="show top N worst chases")
    ap.add_argument("--universe", type=str, default="ai_bigtech")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()
    configure_logging(args.verbose)

    symbols = UNIVERSES[args.universe]
    end = datetime.now(timezone.utc) - timedelta(minutes=60)
    start = end - timedelta(days=int(365 * args.years) + 60)

    all_rows = []
    for sym in symbols:
        try:
            df, _ = fetch_symbol(sym, start, end, "1Day")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{sym}: fetch failed — {exc}")
            continue
        if df is None or df.empty or len(df) < args.entry_window + args.atr_length + 5:
            logger.warning(f"{sym}: insufficient bars")
            continue
        df = df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
        # Trim to the trailing window for the analysis itself, but keep enough
        # warmup so the first signal is valid.
        warmup_cut = end - timedelta(days=int(365 * args.years))
        rows = analyse_symbol(sym, df, args.entry_window, args.exit_window, args.atr_length)
        if rows.empty:
            continue
        rows = rows[rows["signal_ts"] >= pd.Timestamp(warmup_cut)]
        if rows.empty:
            continue
        all_rows.append(rows)
        logger.info(f"{sym}: {len(rows)} entry signals in window")

    if not all_rows:
        print("No signals found.")
        return 1

    full = pd.concat(all_rows, ignore_index=True)
    print()
    print(f"=== Donchian {args.entry_window}/{args.exit_window} chase distribution ===")
    print(f"Universe: {args.universe}  symbols={len(symbols)}  "
          f"window={args.years}y  total_signals={len(full)}")
    print()
    print("Next-open chase (bps) — proxy for fill at next session open:")
    print(summarise(full, "open_chase_bps"))
    print()
    print("Next-bar high chase (bps) — worst-case if bot fires mid-session:")
    print(summarise(full, "high_chase_bps"))
    print()
    print("Next-open chase (ATR multiples):")
    print(summarise(full, "open_chase_atr"))
    print()
    print("Next-bar high chase (ATR multiples):")
    print(summarise(full, "high_chase_atr"))
    print()
    print(f"=== Top {args.top_n} worst next-open chases ===")
    worst = full.sort_values("open_chase_bps", ascending=False).head(args.top_n)
    for _, r in worst.iterrows():
        print(
            f"  {r['signal_ts'].date()}  {r['symbol']:6s}  "
            f"close=${r['signal_close']:8.2f}  next_open=${r['next_open']:8.2f}  "
            f"open_chase={r['open_chase_bps']:7.1f}bps ({r['open_chase_atr']:5.2f} ATR)  "
            f"next_high_chase={r['high_chase_bps']:7.1f}bps"
        )

    # Specific candidate-cap evaluation: how many signals each cap would block.
    print()
    print("=== Cap impact (would have BLOCKED the entry as priced) ===")
    print("  (open_chase_bps > cap ⇒ a marketable-limit at the cap would not fill at open)")
    for cap in (25, 50, 75, 100, 150, 200, 300, 500, 1000):
        blocked = (full["open_chase_bps"] > cap).sum()
        pct = blocked / len(full) * 100
        print(f"  cap={cap:5d}bps  blocked={blocked:4d}/{len(full)}  ({pct:5.1f}%)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
