"""
Market data fetcher for the trading bot.

Responsibilities (Phase 2):
  - Fetch historical OHLCV bars from Alpaca for one or many symbols
  - Validate: tz-aware index, no NaNs in OHLCV, correct dtypes, monotonic,
    no duplicate timestamps
  - Cache to local Parquet files in data/historical/ keyed by
    (symbol, timeframe, adjustment). Second fetch of overlapping data
    serves from cache and only requests the missing range from Alpaca.
  - Freshness guard: `is_fresh(df, max_age)` — live cycles must refuse to
    trade on stale data.
  - Rate-limit-aware retry with exponential backoff.

Non-goals (deferred to later phases):
  - Streaming / websocket data
  - Indicators (Phase 3)
  - Corporate actions beyond what Alpaca's `adjustment` flag provides
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
import alpaca_trade_api as tradeapi
from alpaca_trade_api.rest import APIError
from loguru import logger
from requests.adapters import HTTPAdapter

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL


# ── Paths & constants ────────────────────────────────────────────────────────

CACHE_DIR = Path(__file__).resolve().parent / "historical"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

OHLCV_COLS = ["open", "high", "low", "close", "volume"]

# Timeframe string → Alpaca TimeFrame object + a pandas offset for gap math.
_TIMEFRAME_MAP: dict[str, tuple[tradeapi.TimeFrame, pd.Timedelta]] = {
    "1Day": (tradeapi.TimeFrame.Day, pd.Timedelta(days=1)),
    "1Hour": (tradeapi.TimeFrame.Hour, pd.Timedelta(hours=1)),
    "1Min": (tradeapi.TimeFrame.Minute, pd.Timedelta(minutes=1)),
}


# ── Exceptions ───────────────────────────────────────────────────────────────


class DataValidationError(Exception):
    """Raised when fetched/cached bars fail integrity checks."""


class StaleDataError(Exception):
    """Raised when the latest bar is older than the freshness threshold."""


# ── HTTP timeout adapter ─────────────────────────────────────────────────────

HTTP_TIMEOUT_SECONDS = 30


class _TimeoutAdapter(HTTPAdapter):
    """HTTPAdapter that enforces a default timeout on every request."""

    def send(self, request, **kwargs):
        kwargs.setdefault("timeout", HTTP_TIMEOUT_SECONDS)
        return super().send(request, **kwargs)


def _install_timeout(session) -> None:
    """Mount the timeout adapter on a requests.Session."""
    adapter = _TimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)


# ── Client (lazy singleton) ──────────────────────────────────────────────────

_client: tradeapi.REST | None = None


def close_connections() -> None:
    """Close idle HTTP connections to avoid stale-connection errors between cycles."""
    if _client is not None:
        _client._session.close()


def _get_client() -> tradeapi.REST:
    global _client
    if _client is None:
        _client = tradeapi.REST(
            key_id=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            base_url=ALPACA_BASE_URL,
        )
        _install_timeout(_client._session)
    return _client


# ── Validation ───────────────────────────────────────────────────────────────


def _validate(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    """
    Enforce Phase 2 data-integrity contract on a bars DataFrame.
    Returns the validated df (may drop duplicates / sort).
    """
    if df.empty:
        # Empty is allowed (e.g. cache miss range yielded no bars); caller decides.
        return df

    missing = [c for c in OHLCV_COLS if c not in df.columns]
    if missing:
        raise DataValidationError(f"{symbol}: missing OHLCV columns {missing}")

    if not isinstance(df.index, pd.DatetimeIndex):
        raise DataValidationError(f"{symbol}: index is not a DatetimeIndex")

    if df.index.tz is None:
        raise DataValidationError(f"{symbol}: index is not timezone-aware")

    # Drop exact duplicates on the timestamp index (can happen at cache-merge
    # boundaries). Keep first.
    if df.index.has_duplicates:
        before = len(df)
        df = df[~df.index.duplicated(keep="first")]
        logger.warning(f"{symbol}: dropped {before - len(df)} duplicate-timestamp rows")

    if not df.index.is_monotonic_increasing:
        df = df.sort_index()

    nan_counts = df[OHLCV_COLS].isna().sum()
    if nan_counts.any():
        raise DataValidationError(
            f"{symbol}: NaNs in OHLCV columns: {nan_counts[nan_counts > 0].to_dict()}"
        )

    # Dtype sanity: numeric OHLCV.
    for col in OHLCV_COLS:
        if not pd.api.types.is_numeric_dtype(df[col]):
            raise DataValidationError(f"{symbol}: column {col} is not numeric")

    return df


# ── Freshness ────────────────────────────────────────────────────────────────


def is_fresh(df: pd.DataFrame, max_age: timedelta) -> bool:
    """
    True if the most recent bar is within `max_age` of now (UTC).
    Weekend/holiday-aware callers should pass a generous `max_age`.
    """
    if df.empty:
        return False
    last_ts = df.index[-1]
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    now = datetime.now(timezone.utc)
    age = now - last_ts.to_pydatetime()
    return age <= max_age


def require_fresh(df: pd.DataFrame, max_age: timedelta, symbol: str) -> None:
    """Raise StaleDataError if bars are not fresh. Live-cycle gate."""
    if not is_fresh(df, max_age):
        last = df.index[-1] if not df.empty else "EMPTY"
        raise StaleDataError(
            f"{symbol}: latest bar {last} is older than {max_age}"
        )


# ── Cache ────────────────────────────────────────────────────────────────────


def _cache_path(symbol: str, timeframe: str, adjustment: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_{timeframe}_{adjustment}.parquet"


def _meta_path(symbol: str, timeframe: str, adjustment: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_{timeframe}_{adjustment}.meta.json"


def _read_cache(symbol: str, timeframe: str, adjustment: str) -> pd.DataFrame:
    path = _cache_path(symbol, timeframe, adjustment)
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path)
    # Parquet round-trip should preserve tz, but belt-and-suspenders.
    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def _read_meta(
    symbol: str, timeframe: str, adjustment: str
) -> tuple[datetime | None, datetime | None]:
    """Return (covered_start, covered_end) from sidecar, or (None, None)."""
    path = _meta_path(symbol, timeframe, adjustment)
    if not path.exists():
        return None, None
    try:
        data = json.loads(path.read_text())
        start = datetime.fromisoformat(data["covered_start"])
        end = datetime.fromisoformat(data["covered_end"])
        return start, end
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"{symbol}: bad cache meta, ignoring ({e})")
        return None, None


def _write_cache(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    adjustment: str,
    covered_start: datetime,
    covered_end: datetime,
) -> None:
    if df.empty:
        return
    df.to_parquet(_cache_path(symbol, timeframe, adjustment))
    _meta_path(symbol, timeframe, adjustment).write_text(
        json.dumps(
            {
                "covered_start": covered_start.isoformat(),
                "covered_end": covered_end.isoformat(),
            }
        )
    )


# ── Retry wrapper ────────────────────────────────────────────────────────────


def _with_retry(
    fn, *, max_attempts: int = 5, base_delay: float = 1.0, op_desc: str = "API call"
):
    """
    Call `fn()` with exponential backoff on rate-limit (HTTP 429) or transient
    network errors. Raises the final exception if all attempts fail.
    """
    delay = base_delay
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except APIError as e:
            status = getattr(e, "status_code", None) or getattr(
                getattr(e, "response", None), "status_code", None
            )
            last_exc = e
            # 429 = rate limit. 5xx = transient server. Retry both.
            if status == 429 or (status is not None and 500 <= status < 600):
                logger.warning(
                    f"{op_desc} attempt {attempt}/{max_attempts} failed "
                    f"(status={status}): {e}. Sleeping {delay:.1f}s."
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (ConnectionError, TimeoutError) as e:
            last_exc = e
            logger.warning(
                f"{op_desc} attempt {attempt}/{max_attempts} network error: {e}. "
                f"Sleeping {delay:.1f}s."
            )
            time.sleep(delay)
            delay *= 2
    assert last_exc is not None
    raise last_exc


# ── Core fetch ───────────────────────────────────────────────────────────────


def _fetch_bars_api(
    symbol: str,
    timeframe: str,
    start: datetime,
    end: datetime,
    adjustment: str,
    feed: str,
) -> pd.DataFrame:
    """Single uncached API fetch for one symbol + range."""
    tf_obj, _ = _TIMEFRAME_MAP[timeframe]
    api = _get_client()

    def _call():
        return api.get_bars(
            symbol,
            tf_obj,
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            adjustment=adjustment,
            feed=feed,
        ).df

    bars = _with_retry(_call, op_desc=f"get_bars({symbol}, {timeframe})")

    if bars is None or bars.empty:
        return pd.DataFrame()

    # Alpaca returns a tz-aware DatetimeIndex named 'timestamp'.
    # For multi-symbol responses it may include a 'symbol' column; we fetch
    # per-symbol here so that won't happen, but be defensive.
    if "symbol" in bars.columns:
        bars = bars[bars["symbol"] == symbol].drop(columns=["symbol"])

    keep = [c for c in OHLCV_COLS if c in bars.columns]
    bars = bars[keep]
    return bars


@dataclass
class FetchStats:
    symbol: str
    rows_from_cache: int
    rows_from_api: int
    api_calls: int


def fetch_symbol(
    symbol: str,
    start: datetime,
    end: datetime,
    timeframe: str = "1Day",
    *,
    adjustment: str = "all",
    feed: str = "iex",
    use_cache: bool = True,
) -> tuple[pd.DataFrame, FetchStats]:
    """
    Fetch OHLCV bars for one symbol over [start, end], using the local Parquet
    cache when possible. Only the missing time range(s) hit the Alpaca API.

    Returns (df, stats). `df` has a tz-aware DatetimeIndex and OHLCV columns.
    """
    if timeframe not in _TIMEFRAME_MAP:
        raise ValueError(
            f"Unsupported timeframe '{timeframe}'. Supported: {list(_TIMEFRAME_MAP)}"
        )

    start = _to_utc(start)
    end = _to_utc(end)
    if start >= end:
        raise ValueError(f"start ({start}) must be < end ({end})")

    cached = _read_cache(symbol, timeframe, adjustment) if use_cache else pd.DataFrame()
    cov_start, cov_end = (
        _read_meta(symbol, timeframe, adjustment) if use_cache else (None, None)
    )

    api_calls = 0
    fetched_frames: list[pd.DataFrame] = []

    # Determine which sub-ranges of [start, end] are NOT covered by cache.
    # Coverage is tracked by the sidecar meta (what we *requested*), not by
    # actual bar timestamps — weekends/holidays mean the first/last bar are
    # often strictly inside the requested window.
    missing_ranges = _missing_ranges(cov_start, cov_end, start, end)

    for rng_start, rng_end in missing_ranges:
        logger.info(
            f"{symbol} [{timeframe}]: fetching {rng_start.date()} → {rng_end.date()} from API"
        )
        frame = _fetch_bars_api(
            symbol, timeframe, rng_start, rng_end, adjustment, feed
        )
        api_calls += 1
        if not frame.empty:
            fetched_frames.append(frame)

    # New covered window = union of old coverage and this request.
    new_cov_start = min(cov_start, start) if cov_start else start
    new_cov_end = max(cov_end, end) if cov_end else end

    if fetched_frames:
        new_data = pd.concat(fetched_frames)
        merged = pd.concat([cached, new_data]) if not cached.empty else new_data
        # Deduplicate at cache-merge seam before validation — the overlap
        # is an expected artifact of appending fresh API bars to the cache.
        if merged.index.has_duplicates:
            merged = merged[~merged.index.duplicated(keep="last")]
        merged = _validate(merged, symbol)
        _write_cache(
            merged, symbol, timeframe, adjustment, new_cov_start, new_cov_end
        )
    else:
        merged = cached
        # Even with no new data, if the user requested a widened window
        # that returned zero rows, persist the expanded coverage so we
        # don't refetch next time.
        if cached is not None and not cached.empty and (
            cov_start != new_cov_start or cov_end != new_cov_end
        ):
            _write_cache(
                merged, symbol, timeframe, adjustment, new_cov_start, new_cov_end
            )

    rows_from_api = sum(len(f) for f in fetched_frames)

    if merged.empty:
        return merged, FetchStats(symbol, 0, rows_from_api, api_calls)

    # Slice to requested window.
    window = merged.loc[(merged.index >= start) & (merged.index <= end)]
    rows_from_cache = len(window) - rows_from_api
    # Clamp (overlap at merge seams can make this off by a few).
    rows_from_cache = max(rows_from_cache, 0)

    return window, FetchStats(symbol, rows_from_cache, rows_from_api, api_calls)


def fetch_symbols(
    symbols: Iterable[str],
    start: datetime,
    end: datetime,
    timeframe: str = "1Day",
    *,
    adjustment: str = "all",
    feed: str = "iex",
    use_cache: bool = True,
) -> dict[str, pd.DataFrame]:
    """
    Fetch bars for multiple symbols. Returns a dict {symbol: df}.
    Each symbol is cached independently (Phase 2 cache layout is per-symbol).
    """
    out: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df, stats = fetch_symbol(
            sym,
            start,
            end,
            timeframe,
            adjustment=adjustment,
            feed=feed,
            use_cache=use_cache,
        )
        logger.info(
            f"{sym}: rows_cache={stats.rows_from_cache} "
            f"rows_api={stats.rows_from_api} api_calls={stats.api_calls}"
        )
        out[sym] = df
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────


def _to_utc(dt: datetime) -> datetime:
    """Normalize any datetime to UTC tz-aware."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _missing_ranges(
    cov_start: datetime | None,
    cov_end: datetime | None,
    start: datetime,
    end: datetime,
) -> list[tuple[datetime, datetime]]:
    """
    Given the previously-covered window [cov_start, cov_end] (from sidecar
    metadata — what we *asked the API for* last time, not what bars came back)
    and a new requested [start, end] window, return the sub-ranges we still
    need to fetch.

    We deliberately don't fill interior gaps: for Alpaca daily/hourly bars,
    interior gaps mean non-trading sessions, not missing data.
    """
    if cov_start is None or cov_end is None:
        return [(start, end)]

    ranges: list[tuple[datetime, datetime]] = []
    if start < cov_start:
        ranges.append((start, min(cov_start, end)))
    if end > cov_end:
        ranges.append((max(cov_end, start), end))
    return ranges
