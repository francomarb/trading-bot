"""
Phase 2 verification — Market Data Pipeline.

Checks (from PLAN.md Phase 2 exit criteria):
  1. Fetch 90 days of daily AAPL bars → validate DataFrame shape/dtypes/index
  2. Second call for the same range returns the same data with **zero API calls**
  3. Multi-symbol fetch works
  4. is_fresh() correctly flags stale data
  5. DataValidationError raised on a deliberately broken DataFrame

Run: python phase2_verify.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

from data.fetcher import (
    CACHE_DIR,
    DataValidationError,
    StaleDataError,
    fetch_symbol,
    fetch_symbols,
    is_fresh,
    require_fresh,
    _validate,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
logger.add("logs/phase2.log", rotation="1 MB")


def _clear_cache_for(symbol: str) -> None:
    """Remove cached parquet + meta for a symbol so we can test a cold fetch."""
    for pattern in (f"{symbol.upper()}_*.parquet", f"{symbol.upper()}_*.meta.json"):
        for p in CACHE_DIR.glob(pattern):
            p.unlink()
            logger.info(f"  cleared cache: {p.name}")


def test_single_symbol_cold_then_cached() -> None:
    logger.info("── Test 1: cold fetch → cached fetch (AAPL, 90d daily) ──")
    _clear_cache_for("AAPL")

    end = datetime.now(timezone.utc)
    start = end - timedelta(days=90)

    # Cold
    df1, stats1 = fetch_symbol("AAPL", start, end, "1Day")
    assert not df1.empty, "cold fetch returned empty"
    assert stats1.api_calls >= 1, f"expected ≥1 API call on cold fetch, got {stats1.api_calls}"
    assert stats1.rows_from_api > 0, "expected rows from API on cold fetch"
    logger.success(
        f"  cold: {len(df1)} rows, api_calls={stats1.api_calls}, "
        f"rows_api={stats1.rows_from_api}"
    )

    # Warm (same range)
    df2, stats2 = fetch_symbol("AAPL", start, end, "1Day")
    assert len(df2) == len(df1), f"cached length mismatch: {len(df2)} vs {len(df1)}"
    assert stats2.api_calls == 0, (
        f"expected 0 API calls on cached fetch, got {stats2.api_calls}"
    )
    assert stats2.rows_from_cache == len(df2), "all rows should come from cache"
    logger.success(
        f"  cached: {len(df2)} rows, api_calls={stats2.api_calls} ✓"
    )

    # Validate shape and contract
    assert isinstance(df2.index, pd.DatetimeIndex), "index must be DatetimeIndex"
    assert df2.index.tz is not None, "index must be tz-aware"
    assert df2.index.is_monotonic_increasing, "index must be sorted"
    assert not df2.index.has_duplicates, "index must not have duplicates"
    for col in ["open", "high", "low", "close", "volume"]:
        assert col in df2.columns, f"missing column {col}"
        assert df2[col].isna().sum() == 0, f"NaN in {col}"

    print()
    print("  Head:")
    print(df2.head(3).to_string())
    print()
    print("  Tail:")
    print(df2.tail(3).to_string())
    print()


def test_multi_symbol() -> None:
    logger.info("── Test 2: multi-symbol fetch (AAPL, MSFT, NVDA) ──")
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=30)

    result = fetch_symbols(["AAPL", "MSFT", "NVDA"], start, end, "1Day")
    assert set(result.keys()) == {"AAPL", "MSFT", "NVDA"}
    for sym, df in result.items():
        assert not df.empty, f"{sym}: empty"
        assert df.index.tz is not None, f"{sym}: index not tz-aware"
        logger.success(f"  {sym}: {len(df)} rows, last close ${df['close'].iloc[-1]:.2f}")


def test_freshness_helper() -> None:
    logger.info("── Test 3: is_fresh() / require_fresh() ──")
    idx = pd.DatetimeIndex(
        [datetime.now(timezone.utc) - timedelta(days=10)], tz="UTC"
    )
    stale = pd.DataFrame(
        {"open": [1], "high": [1], "low": [1], "close": [1], "volume": [1]},
        index=idx,
    )

    assert is_fresh(stale, timedelta(days=30)) is True, "10d-old should be fresh vs 30d threshold"
    assert is_fresh(stale, timedelta(hours=1)) is False, "10d-old should be stale vs 1h threshold"

    try:
        require_fresh(stale, timedelta(hours=1), "TEST")
    except StaleDataError as e:
        logger.success(f"  require_fresh raised StaleDataError as expected: {e}")
    else:
        raise AssertionError("require_fresh should have raised StaleDataError")


def test_validation_catches_bad_data() -> None:
    logger.info("── Test 4: _validate() rejects bad data ──")

    # Missing column
    bad = pd.DataFrame(
        {"open": [1.0], "high": [1.0], "low": [1.0], "close": [1.0]},  # no volume
        index=pd.DatetimeIndex([datetime.now(timezone.utc)], tz="UTC"),
    )
    try:
        _validate(bad, "TEST")
    except DataValidationError as e:
        logger.success(f"  missing column → DataValidationError: {e}")
    else:
        raise AssertionError("should have raised on missing column")

    # Naive index
    bad2 = pd.DataFrame(
        {c: [1.0] for c in ["open", "high", "low", "close", "volume"]},
        index=pd.DatetimeIndex([datetime.now()]),
    )
    try:
        _validate(bad2, "TEST")
    except DataValidationError as e:
        logger.success(f"  naive index → DataValidationError: {e}")
    else:
        raise AssertionError("should have raised on naive index")

    # NaN in OHLCV
    bad3 = pd.DataFrame(
        {
            "open": [1.0, float("nan")],
            "high": [1.0, 1.0],
            "low": [1.0, 1.0],
            "close": [1.0, 1.0],
            "volume": [1, 1],
        },
        index=pd.DatetimeIndex(
            [datetime.now(timezone.utc), datetime.now(timezone.utc) + timedelta(days=1)],
            tz="UTC",
        ),
    )
    try:
        _validate(bad3, "TEST")
    except DataValidationError as e:
        logger.success(f"  NaN → DataValidationError: {e}")
    else:
        raise AssertionError("should have raised on NaN")


def test_range_extension_uses_cache_for_overlap() -> None:
    """
    Second fetch with a wider range should only hit the API for the new part,
    not refetch the overlap.
    """
    logger.info("── Test 5: widening the range refetches only the missing part ──")
    _clear_cache_for("GOOGL")

    end = datetime.now(timezone.utc)

    # Fetch a narrow window
    start_narrow = end - timedelta(days=30)
    _, stats_a = fetch_symbol("GOOGL", start_narrow, end, "1Day")
    logger.info(f"  narrow fetch: api_calls={stats_a.api_calls}, rows_api={stats_a.rows_from_api}")

    # Fetch a wider window — earlier portion is new, recent portion should be cached
    start_wide = end - timedelta(days=90)
    _, stats_b = fetch_symbol("GOOGL", start_wide, end, "1Day")
    logger.info(
        f"  wide fetch: api_calls={stats_b.api_calls}, "
        f"rows_api={stats_b.rows_from_api}, rows_cache={stats_b.rows_from_cache}"
    )

    assert stats_b.rows_from_cache > 0, "cache overlap should contribute rows"
    assert stats_b.rows_from_api > 0, "new range should fetch some rows"
    logger.success("  partial cache hit on range extension ✓")


def main() -> None:
    logger.info("═══ Phase 2 Verification — Market Data Pipeline ═══")
    Path("logs").mkdir(exist_ok=True)

    test_single_symbol_cold_then_cached()
    test_multi_symbol()
    test_freshness_helper()
    test_validation_catches_bad_data()
    test_range_extension_uses_cache_for_overlap()

    logger.info("═══ Phase 2 Verification — all checks passed ✓ ═══")


if __name__ == "__main__":
    main()
