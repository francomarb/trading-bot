"""
Phase 4 verification — Strategy Framework.

Integration check: run SMACrossover on real AAPL bars fetched via Phase 2
pipeline, assert signal contract, and print entry/exit events.

Run: python phase4_verify.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

from data.fetcher import fetch_symbol
from strategies.base import OrderType, SignalFrame
from strategies.sma_crossover import SMACrossover

logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
logger.add("logs/phase4.log", rotation="1 MB")


def main() -> None:
    Path("logs").mkdir(exist_ok=True)
    logger.info("═══ Phase 4 Verification — Strategy Framework ═══")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=400)
    df, _ = fetch_symbol("AAPL", start, end, "1Day")
    assert not df.empty, "no bars fetched"
    logger.success(f"Fetched {len(df)} AAPL daily bars")

    strat = SMACrossover(fast=20, slow=50)
    logger.info(f"Strategy: {strat!r} (preferred_order_type={strat.preferred_order_type.value})")
    assert strat.preferred_order_type is OrderType.MARKET

    sig = strat.generate_signals(df)

    # Contract checks.
    assert isinstance(sig, SignalFrame)
    assert sig.entries.index.equals(df.index)
    assert sig.exits.index.equals(df.index)
    assert sig.entries.dtype == bool and sig.exits.dtype == bool
    assert not (sig.entries & sig.exits).any(), "entry and exit cannot co-occur on same bar"

    # Warmup region must be clean.
    assert not sig.entries.iloc[:49].any()
    assert not sig.exits.iloc[:49].any()

    # Look-ahead guard on real data.
    cut = len(df) - 10
    truncated = strat.generate_signals(df.iloc[: cut + 1])
    pd.testing.assert_series_equal(
        sig.entries.iloc[: cut + 1], truncated.entries, check_names=False
    )
    pd.testing.assert_series_equal(
        sig.exits.iloc[: cut + 1], truncated.exits, check_names=False
    )
    logger.success("Look-ahead guard verified on real bars")

    n_entries = int(sig.entries.sum())
    n_exits = int(sig.exits.sum())
    logger.info(f"Signals over window: entries={n_entries} exits={n_exits}")

    if n_entries:
        print()
        print("Entry bars:")
        print(df.loc[sig.entries, ["close"]].to_string())
    if n_exits:
        print()
        print("Exit bars:")
        print(df.loc[sig.exits, ["close"]].to_string())
    print()

    # Edge filter smoke test: gate on "price above 200 MA" proxy
    # (using a short proxy so the filter is actually active on this window).
    def trend_gate(bars: pd.DataFrame) -> pd.Series:
        sma100 = bars["close"].rolling(100).mean()
        return bars["close"] > sma100

    gated = SMACrossover(fast=20, slow=50, edge_filter=trend_gate).generate_signals(df)
    assert gated.entries.sum() <= n_entries, "edge filter can only reduce entries"
    assert int(gated.exits.sum()) == n_exits, "edge filter must never block exits"
    logger.info(
        f"With trend edge filter: entries={int(gated.entries.sum())} "
        f"(unfiltered={n_entries}), exits={int(gated.exits.sum())}"
    )

    logger.info("═══ Phase 4 Verification — all checks passed ✓ ═══")


if __name__ == "__main__":
    main()
