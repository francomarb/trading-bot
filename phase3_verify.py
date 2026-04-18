"""
Phase 3 verification — Technical Indicators.

Integration check: run SMA, EMA, ATR on real AAPL bars fetched via Phase 2
pipeline, print a summary, and assert shape/sanity properties.

Run: python phase3_verify.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from data.fetcher import fetch_symbol
from indicators.technicals import add_atr, add_ema, add_sma

logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
logger.add("logs/phase3.log", rotation="1 MB")


def main() -> None:
    Path("logs").mkdir(exist_ok=True)
    logger.info("═══ Phase 3 Verification — Technical Indicators ═══")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=200)
    df, _ = fetch_symbol("AAPL", start, end, "1Day")
    assert not df.empty, "no bars fetched"
    logger.success(f"Fetched {len(df)} AAPL daily bars")

    # Apply the full MVP indicator stack.
    df = add_sma(df, 20)
    df = add_sma(df, 50)
    df = add_ema(df, 20)
    df = add_atr(df, 14)

    for col in ["sma_20", "sma_50", "ema_20", "atr_14"]:
        assert col in df.columns, f"missing column {col}"

    # Shape: first (length-1) values are NaN, the rest are finite.
    assert df["sma_20"].iloc[:19].isna().all()
    assert df["sma_20"].iloc[19:].notna().all()
    assert df["sma_50"].iloc[:49].isna().all()
    assert df["sma_50"].iloc[49:].notna().all()
    assert df["ema_20"].iloc[:19].isna().all()
    assert df["ema_20"].iloc[19:].notna().all()
    assert df["atr_14"].iloc[:13].isna().all()
    assert df["atr_14"].iloc[13:].notna().all()

    # Sanity: ATR is non-negative.
    assert (df["atr_14"].dropna() >= 0).all()

    # Sanity: SMA and EMA should be within the min/max range of their window.
    # Use the final value as a spot check.
    last = df.iloc[-1]
    last20 = df["close"].iloc[-20:]
    assert last20.min() <= last["sma_20"] <= last20.max()
    assert last20.min() <= last["ema_20"] <= last20.max()

    # Print tail for human inspection.
    print()
    print("Last 5 bars with indicators:")
    print(
        df[["close", "sma_20", "sma_50", "ema_20", "atr_14"]]
        .tail(5)
        .round(3)
        .to_string()
    )
    print()
    logger.info(
        f"AAPL latest: close=${last['close']:.2f} "
        f"sma_20=${last['sma_20']:.2f} sma_50=${last['sma_50']:.2f} "
        f"ema_20=${last['ema_20']:.2f} atr_14=${last['atr_14']:.2f}"
    )
    trend = "BULL (sma_20 > sma_50)" if last["sma_20"] > last["sma_50"] else "BEAR (sma_20 < sma_50)"
    logger.info(f"Trend read: {trend}")

    logger.info("═══ Phase 3 Verification — all checks passed ✓ ═══")


if __name__ == "__main__":
    main()
