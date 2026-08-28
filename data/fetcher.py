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

SDK: alpaca-py (official, replaces deprecated alpaca-trade-api).
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from loguru import logger
from requests.adapters import HTTPAdapter

from config.settings import (
    ALPACA_API_KEY,
    ALPACA_SECRET_KEY,
    ALPACA_DATA_FEED,
    ARRIVAL_QUOTE_MAX_AGE_SECONDS,
)


# ── Paths & constants ────────────────────────────────────────────────────────

CACHE_DIR = Path(__file__).resolve().parent / "historical"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

OHLCV_COLS = ["open", "high", "low", "close", "volume"]

# Alpaca publishes a session's daily bar only after the regular market opens:
# 13:30 UTC during daylight time and 14:30 UTC during standard time. Cache
# repair must not classify today's bar as missing before then. Keep 30 minutes
# of slack rather than coupling this US-equities boundary to DST conversion.
_DAILY_BAR_READY_UTC_OFFSET = timedelta(hours=15)

# Timeframe string → alpaca-py TimeFrame + pandas offset for gap math.
_TIMEFRAME_MAP: dict[str, tuple[TimeFrame, pd.Timedelta]] = {
    "1Day": (TimeFrame.Day, pd.Timedelta(days=1)),
    "1Hour": (TimeFrame.Hour, pd.Timedelta(hours=1)),
    "5Min": (TimeFrame(5, TimeFrameUnit.Minute), pd.Timedelta(minutes=5)),
    "1Min": (TimeFrame.Minute, pd.Timedelta(minutes=1)),
}

# Adjustment string → alpaca-py enum.
_ADJUSTMENT_MAP: dict[str, Adjustment] = {
    "raw": Adjustment.RAW,
    "split": Adjustment.SPLIT,
    "dividend": Adjustment.DIVIDEND,
    "all": Adjustment.ALL,
}

# Feed string → alpaca-py enum.
_FEED_MAP: dict[str, DataFeed] = {
    "iex": DataFeed.IEX,
    "sip": DataFeed.SIP,
}
_VALID_FEEDS: frozenset[str] = frozenset(_FEED_MAP.keys())


def _validate_feed(feed: str) -> str:
    """
    Strict feed validation. Returns the lowercased feed string if valid;
    raises ValueError otherwise.

    Pre-PR #50 the API path used .get(feed, DataFeed.IEX) which silently
    fell through to IEX for unknown feeds while caching under the raw
    string. Net effect: a typo (e.g. ``feed="six"``) created a cache
    directory ``data/historical/six/`` containing IEX bars, then on the
    next call the IEX synthetic-SIP volume scaling was NOT applied
    (only IEX-cased input triggers it), silently producing wrong volume
    numbers. Strict validation prevents this.
    """
    if not isinstance(feed, str):
        raise ValueError(f"feed must be a str, got {type(feed).__name__}")
    normalized = feed.lower()
    if normalized not in _VALID_FEEDS:
        raise ValueError(
            f"feed must be one of {sorted(_VALID_FEEDS)}; got {feed!r}"
        )
    return normalized


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
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = HTTP_TIMEOUT_SECONDS
        return super().send(request, **kwargs)


def _install_timeout(session) -> None:
    """Mount the timeout adapter on a requests.Session."""
    adapter = _TimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)


# ── Client (lazy singleton) ──────────────────────────────────────────────────

_client: StockHistoricalDataClient | None = None


def close_connections() -> None:
    """Close idle HTTP connections to avoid stale-connection errors between cycles."""
    if _client is not None:
        _client._session.close()


def _get_client() -> StockHistoricalDataClient:
    global _client
    if _client is None:
        _client = StockHistoricalDataClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
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


def is_fresh(df: pd.DataFrame, max_age: timedelta, now: datetime | None = None) -> bool:
    """
    True if the most recent bar is within `max_age` of now (UTC).
    Weekend/holiday-aware callers should pass a generous `max_age`.
    """
    if df.empty:
        return False
    last_ts = df.index[-1]
    if last_ts.tzinfo is None:
        last_ts = last_ts.tz_localize("UTC")
    now = now or datetime.now(timezone.utc)
    age = now - last_ts.to_pydatetime()
    return age <= max_age


def require_fresh(df: pd.DataFrame, max_age: timedelta, symbol: str, now: datetime | None = None) -> None:
    """Raise StaleDataError if bars are not fresh. Live-cycle gate."""
    if not is_fresh(df, max_age, now):
        last = df.index[-1] if not df.empty else "EMPTY"
        raise StaleDataError(
            f"{symbol}: latest bar {last} is older than {max_age}"
        )


# ── Arrival-price quote ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArrivalQuote:
    """A quote captured as the arrival-price benchmark, with its provenance.

    `quote_timestamp` is the venue's own timestamp for the quote — NOT when we
    fetched it. That distinction is the whole point: before this existed, only
    the capture time was recorded, so a quote could be arbitrarily old and
    nothing downstream could tell.
    """

    symbol: str
    midpoint: float
    bid: float
    ask: float
    spread_bps: float
    quote_timestamp: datetime
    captured_at: datetime
    age_seconds: float


def fetch_latest_quote(
    symbol: str, *, max_age_seconds: float | None = None,
) -> ArrivalQuote | None:
    """Fetch the latest IEX quote for `symbol` as an arrival-price benchmark.

    Returns ``None`` — meaning "no usable benchmark" — when the quote is
    unavailable, malformed, non-finite, one-sided (a zero bid or ask —
    pre-market quoting gap, halt, illiquid symbol), **crossed** (ask < bid), or
    **older than `max_age_seconds`**. A locked book (bid == ask) IS accepted;
    the midpoint is unambiguous and the zero spread is recorded. Callers treat ``None``
    as "fall back to a non-execution-quality benchmark", which is what keeps a
    bad reading out of the calibration pool rather than silently polluting it.

    Emits an ``arrival_quote`` event on every capture, accepted or rejected,
    carrying `age_seconds` and `spread_bps`. Those two fields were never
    recorded before and are what allow the staleness threshold to be
    calibrated — and, more importantly, what distinguish a stale quote from a
    fresh-but-wide one. See `ARRIVAL_QUOTE_MAX_AGE_SECONDS` in settings for the
    audit that motivated this and for the competing hypothesis.

    Paper-trading note: with the IEX feed (the only one available on a paper
    Alpaca subscription) this is IEX BBO, not full SIP NBBO. IEX is a single
    venue at roughly 2-3% of consolidated volume, so its book can be
    unrepresentative even when perfectly fresh.
    """
    if not symbol:
        return None
    if max_age_seconds is None:
        max_age_seconds = ARRIVAL_QUOTE_MAX_AGE_SECONDS
    try:
        client = _get_client()
        result = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=DataFeed.IEX)
        )
    except (APIError, Exception) as exc:  # noqa: BLE001 — never raise into trading loop
        logger.warning(f"{symbol}: latest-quote fetch failed: {exc}")
        return None
    quote = result.get(symbol) if isinstance(result, dict) else None
    if quote is None:
        return None
    try:
        bid = float(getattr(quote, "bid_price", 0.0) or 0.0)
        ask = float(getattr(quote, "ask_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(bid) and math.isfinite(ask)):
        # NaN/inf reach here as floats and survive every `<= 0` comparison.
        # The engine's `_finite_or_none` happens to catch them downstream, but
        # relying on a caller's guard for a contract this function claims to
        # enforce is how the crossed-book case below slipped through.
        logger.warning(
            f"{symbol}: arrival quote has non-finite prices (bid={bid}, ask={ask}) "
            "— rejected"
        )
        return None
    if bid <= 0 or ask <= 0:
        return None
    if ask < bid:
        # Crossed book. This function's docstring has claimed to reject crossed
        # books since it was written, but only ever tested for a zero side, so
        # a crossed quote was accepted with a plausible-looking finite midpoint
        # and a NEGATIVE spread. `_finite_or_none` does NOT catch it — the
        # midpoint is finite and positive — so it would be certified
        # `arrival_midpoint` / `primary` and enter the calibration pool.
        # Found in review of PR #127; reproduced with bid=101, ask=100 giving
        # midpoint 100.50 and spread -99.50 bps.
        logger.warning(
            f"{symbol}: arrival quote is crossed (bid={bid} > ask={ask}) — rejected"
        )
        return None

    # A LOCKED book (bid == ask) is deliberately allowed: it is a real, if
    # transient, market state and the midpoint is unambiguous. It records
    # spread_bps=0.0, which is visible in the arrival_quote event if it later
    # proves to correlate with bad benchmarks.
    midpoint = (bid + ask) / 2.0
    spread_bps = (ask - bid) / midpoint * 10_000 if midpoint > 0 else float("nan")

    captured_at = datetime.now(timezone.utc)
    qts = getattr(quote, "timestamp", None)
    if qts is None:
        # No venue timestamp — age is unknowable, so this cannot be certified
        # as an execution-quality benchmark. Reject rather than assume fresh.
        _emit_arrival_quote_event(
            symbol, midpoint, bid, ask, spread_bps,
            quote_timestamp=None, captured_at=captured_at,
            age_seconds=None, accepted=False, reason="no_quote_timestamp",
        )
        return None
    try:
        if qts.tzinfo is None:
            qts = qts.replace(tzinfo=timezone.utc)
        age_seconds = (captured_at - qts).total_seconds()
    except (AttributeError, TypeError, ValueError) as exc:
        # A timestamp we cannot do arithmetic on is the same situation as no
        # timestamp: the age is unknowable, so the quote cannot be certified.
        # Reject rather than raise — this function's contract is that it never
        # raises into the trading loop.
        logger.warning(
            f"{symbol}: arrival quote has an unusable timestamp ({qts!r}): {exc}"
        )
        _emit_arrival_quote_event(
            symbol, midpoint, bid, ask, spread_bps,
            quote_timestamp=None, captured_at=captured_at,
            age_seconds=None, accepted=False, reason="bad_quote_timestamp",
        )
        return None

    accepted = age_seconds <= max_age_seconds
    _emit_arrival_quote_event(
        symbol, midpoint, bid, ask, spread_bps,
        quote_timestamp=qts, captured_at=captured_at,
        age_seconds=age_seconds, accepted=accepted,
        reason=None if accepted else "stale",
    )
    if not accepted:
        logger.warning(
            f"{symbol}: arrival quote rejected as stale — age={age_seconds:.1f}s "
            f"> {max_age_seconds:.0f}s (mid={midpoint:.4f}, spread={spread_bps:.1f}bps). "
            "Falling back to a non-execution-quality benchmark."
        )
        return None

    return ArrivalQuote(
        symbol=symbol, midpoint=midpoint, bid=bid, ask=ask,
        spread_bps=spread_bps, quote_timestamp=qts,
        captured_at=captured_at, age_seconds=age_seconds,
    )


def _emit_arrival_quote_event(
    symbol: str, midpoint: float, bid: float, ask: float, spread_bps: float,
    *, quote_timestamp, captured_at, age_seconds, accepted: bool,
    reason: str | None,
) -> None:
    """Observational only — never blocks a trade, never raises."""
    try:
        logger.bind(
            event="arrival_quote",
            symbol=symbol,
            midpoint=midpoint,
            bid=bid,
            ask=ask,
            spread_bps=round(spread_bps, 2) if spread_bps == spread_bps else None,
            quote_timestamp=quote_timestamp.isoformat() if quote_timestamp else None,
            captured_at=captured_at.isoformat(),
            age_seconds=round(age_seconds, 3) if age_seconds is not None else None,
            accepted=accepted,
            reject_reason=reason,
            feed="iex",
        ).debug(f"arrival_quote {symbol} accepted={accepted}")
    except Exception:  # noqa: BLE001 — instrumentation must never break trading
        pass


def fetch_latest_quote_midpoint(symbol: str) -> float | None:
    """Latest usable arrival midpoint, or ``None``.

    Thin wrapper over `fetch_latest_quote` preserved so existing callers keep
    their signature. ``None`` now additionally means "the quote was too old to
    certify", which routes the caller to a fallback benchmark.
    """
    quote = fetch_latest_quote(symbol)
    return quote.midpoint if quote is not None else None


# ── Cache ────────────────────────────────────────────────────────────────────


def _legacy_cache_path(symbol: str, timeframe: str, adjustment: str) -> Path:
    """Top-level (pre-feed-aware) cache path. Read fallback only — never written.

    All bars cached before the feed-aware layout landed live here. The fallback
    keeps the live bot from seeing an empty cache after the new fetcher deploys
    but before scripts/migrate_cache_to_feed_aware.py has been run. Once
    migration is complete, this path is empty and the fallback becomes dead
    code (safe to delete in a follow-up).
    """
    return CACHE_DIR / f"{symbol.upper()}_{timeframe}_{adjustment}.parquet"


def _legacy_meta_path(symbol: str, timeframe: str, adjustment: str) -> Path:
    return CACHE_DIR / f"{symbol.upper()}_{timeframe}_{adjustment}.meta.json"


def _cache_path(symbol: str, timeframe: str, adjustment: str, feed: str) -> Path:
    """Feed-aware cache path. Writes go here; reads check here first."""
    return CACHE_DIR / feed.lower() / f"{symbol.upper()}_{timeframe}_{adjustment}.parquet"


def _meta_path(symbol: str, timeframe: str, adjustment: str, feed: str) -> Path:
    return CACHE_DIR / feed.lower() / f"{symbol.upper()}_{timeframe}_{adjustment}.meta.json"


def _read_cache(symbol: str, timeframe: str, adjustment: str, feed: str) -> pd.DataFrame:
    """Read cached bars. Tries feed-aware path first, falls back to legacy.

    The fallback exists for graceful deploy before migration. New writes always
    go to the feed-aware path, so once a symbol is touched after the fetcher
    upgrade, only the feed-aware path is consulted for it from then on.
    """
    path = _cache_path(symbol, timeframe, adjustment, feed)
    if not path.exists():
        # Legacy fallback — pre-feed-aware layout. Only applies for the IEX
        # feed because that's what every cache file was tagged as before
        # the migration.
        if feed.lower() == "iex":
            path = _legacy_cache_path(symbol, timeframe, adjustment)
            if not path.exists():
                return pd.DataFrame()
        else:
            return pd.DataFrame()
    df = pd.read_parquet(path)
    # Parquet round-trip should preserve tz, but belt-and-suspenders.
    if isinstance(df.index, pd.DatetimeIndex) and df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def _read_meta(
    symbol: str, timeframe: str, adjustment: str, feed: str
) -> tuple[datetime | None, datetime | None]:
    """Return (covered_start, covered_end) from sidecar, or (None, None).

    Tries feed-aware sidecar first, falls back to legacy top-level sidecar
    (IEX feed only).
    """
    path = _meta_path(symbol, timeframe, adjustment, feed)
    if not path.exists():
        if feed.lower() == "iex":
            path = _legacy_meta_path(symbol, timeframe, adjustment)
            if not path.exists():
                return None, None
        else:
            return None, None
    try:
        data = json.loads(path.read_text())
        start = datetime.fromisoformat(data["covered_start"])
        end = datetime.fromisoformat(data["covered_end"])
        return start, end
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"{symbol}: bad cache meta, ignoring ({e})")
        return None, None


def _read_gap_state(
    symbol: str, timeframe: str, adjustment: str, feed: str
) -> tuple[set[date], set[date]]:
    """Return (absent_sessions, seen_once) from the sidecar.

    ``absent`` are sessions requested twice and delivered neither time —
    treated as genuinely unavailable upstream (a feed-depth boundary, a
    delisted stretch) and excluded from gap detection so the fetcher does
    not chase them forever. ``seen_once`` is the first strike.

    Both default to empty for pre-11.52 sidecars, so old meta files keep
    working untouched.
    """
    path = _meta_path(symbol, timeframe, adjustment, feed)
    if not path.exists():
        return set(), set()
    try:
        data = json.loads(path.read_text())
        absent = {date.fromisoformat(d) for d in data.get("absent_sessions", [])}
        seen = {date.fromisoformat(d) for d in data.get("gap_seen_once", [])}
        return absent, seen
    except (json.JSONDecodeError, ValueError):
        return set(), set()


def _write_cache(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    adjustment: str,
    covered_start: datetime,
    covered_end: datetime,
    feed: str,
    absent_sessions: set[date] | None = None,
    gap_seen_once: set[date] | None = None,
) -> None:
    if df.empty:
        return
    cache_path = _cache_path(symbol, timeframe, adjustment, feed)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache_path)
    meta: dict = {
        "covered_start": covered_start.isoformat(),
        "covered_end": covered_end.isoformat(),
    }
    if absent_sessions:
        meta["absent_sessions"] = sorted(d.isoformat() for d in absent_sessions)
    if gap_seen_once:
        meta["gap_seen_once"] = sorted(d.isoformat() for d in gap_seen_once)
    _meta_path(symbol, timeframe, adjustment, feed).write_text(json.dumps(meta))


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
            status = e.status_code
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
    client = _get_client()

    adj_enum = _ADJUSTMENT_MAP.get(adjustment, Adjustment.ALL)
    # Strict feed lookup — _validate_feed at the public entry has already
    # normalized this, so a KeyError here would indicate a bypass bug.
    feed_enum = _FEED_MAP[feed]

    request = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=tf_obj,
        start=start,
        end=end,
        adjustment=adj_enum,
        feed=feed_enum,
    )

    def _call():
        return client.get_stock_bars(request)

    barset = _with_retry(_call, op_desc=f"get_stock_bars({symbol}, {timeframe})")

    if barset is None:
        return pd.DataFrame()

    # Convert BarSet to DataFrame.
    bars = barset.df

    if bars is None or bars.empty:
        return pd.DataFrame()

    # alpaca-py returns a MultiIndex (symbol, timestamp) for single-symbol
    # requests. Drop the symbol level to get a plain DatetimeIndex.
    if isinstance(bars.index, pd.MultiIndex):
        bars = bars.droplevel("symbol")

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
    feed: str = ALPACA_DATA_FEED,
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

    # Strict feed validation — rejects typos / unknown feeds before they
    # reach the cache layer (which used to silently create mis-tagged subdirs).
    feed = _validate_feed(feed)

    start = _to_utc(start)
    end = _to_utc(end)
    if start >= end:
        raise ValueError(f"start ({start}) must be < end ({end})")

    # Basic Alpaca tier rule: SIP historical queries are only allowed for
    # bars whose timestamp is at least 15 minutes old. Real-time SIP needs
    # the Algo Trader Plus subscription. For backtests / offline analysis
    # the 15-min delay is irrelevant — we're typically asking for bars far
    # older than that — but the moment a forward-test or recent-data audit
    # passes `end=now`, the API returns 422. Clamp here so callers don't
    # have to think about it. Watchlist scanners enforce the same rule via
    # --end-delay-minutes (default 60).
    if feed == "sip":
        sip_end_cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
        if end > sip_end_cutoff:
            logger.warning(
                f"{symbol}: SIP requires end <= now-15min; clamping end "
                f"{end.isoformat()} → {sip_end_cutoff.isoformat()}"
            )
            end = sip_end_cutoff
            # Re-validate the interval — if the caller asked for a window
            # entirely within the 15-min SIP delay (e.g. start=now-10min,
            # end=now), clamping collapses end below start. That's a real
            # caller error, not something to swallow.
            if start >= end:
                raise ValueError(
                    f"{symbol}: requested SIP window collapsed after 15-min "
                    f"end-clamp (start={start.isoformat()}, "
                    f"clamped end={end.isoformat()}). The interval was "
                    f"entirely within the SIP delay window. Pass an earlier "
                    f"`end` or wait 15 minutes for the bars to become "
                    f"available."
                )

    cached = _read_cache(symbol, timeframe, adjustment, feed) if use_cache else pd.DataFrame()
    cov_start, cov_end = (
        _read_meta(symbol, timeframe, adjustment, feed) if use_cache else (None, None)
    )

    # Clamp a future-dated covered_end to now so a backtest or verify script
    # that fetched with end=<far future> can't lock out the live fetcher.
    now_utc = datetime.now(timezone.utc)
    if cov_end is not None and cov_end > now_utc:
        logger.warning(
            f"{symbol}: cache covered_end {cov_end.date()} is in the future — "
            "clamping to now to force re-fetch of recent bars"
        )
        cov_end = now_utc

    _, bar_interval = _TIMEFRAME_MAP[timeframe]

    api_calls = 0
    fetched_frames: list[pd.DataFrame] = []

    # Determine which sub-ranges of [start, end] are NOT covered by cache.
    # Coverage is tracked by the sidecar meta (what we *requested*), not by
    # actual bar timestamps — weekends/holidays mean the first/last bar are
    # often strictly inside the requested window.
    #
    # We subtract one bar interval from cov_end before the range check so the
    # most-recent bar is always re-fetched. This catches feed-lag gaps where
    # IEX delivers a bar late: without this, the fetcher would mark the range
    # as covered even though the bar never arrived, locking it out permanently.
    effective_cov_end = cov_end - bar_interval if cov_end is not None else None
    missing_ranges = _missing_ranges(cov_start, effective_cov_end, start, end)

    # Interval coverage is blind to gaps in the MIDDLE of a covered range
    # (PLAN 11.52). Diff what we hold against a real session calendar and
    # add whatever is genuinely missing. Fail-open: no calendar, no extra
    # ranges, pre-11.52 behaviour.
    absent_sessions, gap_seen_once = (
        _read_gap_state(symbol, timeframe, adjustment, feed)
        if use_cache else (set(), set())
    )
    gap_ranges: list[tuple[datetime, datetime]] = []
    if use_cache and timeframe == "1Day":
        gap_ranges = _session_gap_ranges(
            cached, start, end, absent_sessions, symbol
        )
        missing_ranges = missing_ranges + gap_ranges

    # Every range producer must honor the public request end.  The session
    # calendar deals in whole days, while delayed SIP requests may end partway
    # through a day; this final boundary is the invariant before any API call.
    missing_ranges = _bound_ranges_to_end(missing_ranges, end)
    gap_ranges = _bound_ranges_to_end(gap_ranges, end)

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

    # New covered window = union of old coverage and this request, capped at now
    # so a future-dated end argument can never poison the coverage metadata.
    new_cov_start = min(cov_start, start) if cov_start else start
    new_cov_end = min(max(cov_end, end) if cov_end else end, now_utc)

    if fetched_frames:
        new_data = pd.concat(fetched_frames)
        merged = pd.concat([cached, new_data]) if not cached.empty else new_data
        # Deduplicate at cache-merge seam before validation — the overlap
        # is an expected artifact of appending fresh API bars to the cache.
        if merged.index.has_duplicates:
            merged = merged[~merged.index.duplicated(keep="last")]
        merged = _validate(merged, symbol)
        # Record what came back, not what we asked for (PLAN 11.52).
        new_cov_end = _coverage_end_from_bars(merged, new_cov_end)
        if use_cache:
            absent_sessions, gap_seen_once = _update_gap_strikes(
                merged, gap_ranges, absent_sessions, gap_seen_once, symbol
            )
            _write_cache(
                merged, symbol, timeframe, adjustment, new_cov_start, new_cov_end,
                feed, absent_sessions, gap_seen_once,
            )
    else:
        merged = cached
        # Even with no new data, if the user requested a widened window
        # that returned zero rows, persist the expanded coverage so we
        # don't refetch next time.
        # Not clamped here: a request that returned ZERO rows cannot tell a
        # truncated response apart from genuinely-absent data (a weekend, or
        # a window past this symbol's feed depth). Persisting the widened
        # coverage is the right call for the latter and is the pre-existing
        # behaviour; clamping would re-request an empty range forever. The
        # 11.52 failure mode is a PARTIAL response, which lands above.
        if use_cache and cached is not None and not cached.empty and (
            cov_start != new_cov_start or cov_end != new_cov_end
        ):
            absent_sessions, gap_seen_once = _update_gap_strikes(
                merged, gap_ranges, absent_sessions, gap_seen_once, symbol
            )
            _write_cache(
                merged, symbol, timeframe, adjustment, new_cov_start, new_cov_end,
                feed, absent_sessions, gap_seen_once,
            )

    rows_from_api = sum(len(f) for f in fetched_frames)

    if merged.empty:
        return merged, FetchStats(symbol, 0, rows_from_api, api_calls)

    # Slice to requested window.
    window = merged.loc[(merged.index >= start) & (merged.index <= end)]
    
    if feed.lower() == "iex":
        from utils.market import apply_synthetic_sip_volume
        is_daily = (timeframe == "1Day")
        window = apply_synthetic_sip_volume(window, is_daily=is_daily)

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
    feed: str = ALPACA_DATA_FEED,
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


def _coverage_end_from_bars(
    bars: pd.DataFrame, requested_end: datetime
) -> datetime:
    """
    Clamp the coverage end to the last bar that actually arrived.

    The sidecar meta used to record what we *asked the API for*. When a
    response came back short, the missing tail was still stamped as
    covered — and `_missing_ranges` only ever asks for bars before
    `covered_start` or after `covered_end`, so those sessions were never
    requested again no matter how long the bot ran (PLAN 11.52).

    The confirmed case: after the laptop was off 2026-07-13 → 07-20, the
    resume issued one large catch-up fetch, stamped `covered_end = 07-20`
    from the request, and silently accepted whatever subset came back.
    92 of 106 SIP symbols lost that entire trading week, undetectable
    from the metadata.

    Only the END is clamped. The start side is where per-symbol feed-depth
    boundaries live — SIP begins 2016-01-04, so a request reaching further
    back legitimately returns nothing before it, and clamping there would
    re-request the missing years on every call forever. Recording the
    requested start is correct for that; only the end was lying.
    """
    if bars is None or bars.empty:
        return requested_end
    last = pd.Timestamp(bars.index.max()).to_pydatetime()
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return min(requested_end, last)




def _update_gap_strikes(
    merged: pd.DataFrame,
    requested: list[tuple[datetime, datetime]],
    absent: set[date],
    seen_once: set[date],
    symbol: str,
) -> tuple[set[date], set[date]]:
    """
    Two-strike rule for sessions we asked for and did not get.

    A single miss is ambiguous — it could be a truncated response (retry
    and it heals) or data that genuinely does not exist upstream (retry
    forever and it never will). Marking absent on the first miss would have
    hidden the July incident; never marking absent would chase `iex/SPY`'s
    634-day feed-depth gap on every call for the life of the cache.

    So: first miss records a strike and leaves the session eligible for
    re-request. Second miss retires it to ``absent``.
    """
    if not requested:
        return absent, seen_once
    from data import market_calendar

    have = (
        {pd.Timestamp(ts).tz_convert("UTC").date() for ts in merged.index}
        if merged is not None and not merged.empty else set()
    )
    lo = min(r[0] for r in requested).date()
    hi = max(r[1] for r in requested).date()
    still_missing = market_calendar.missing_sessions(lo, hi, have, ignore=absent)
    if still_missing is None:
        return absent, seen_once

    newly_absent = still_missing & seen_once
    if newly_absent:
        logger.info(
            f"{symbol}: {len(newly_absent)} session(s) unavailable after a "
            "second request — recording as genuinely absent upstream"
        )
    return absent | newly_absent, (seen_once | still_missing) - newly_absent


def _session_gap_ranges(
    cached: pd.DataFrame,
    start: datetime,
    end: datetime,
    absent: set[date],
    symbol: str,
) -> list[tuple[datetime, datetime]]:
    """
    Fetch ranges for sessions that SHOULD exist in [start, end] but are
    absent from the cache (PLAN 11.52).

    `_missing_ranges` works on the covered interval, so it can only ever
    ask for bars before `covered_start` or after `covered_end`. That is
    blind to a gap in the middle — which is exactly how a short catch-up
    response after the 2026-07-13→07-20 shutdown cost 92 of 106 symbols an
    entire trading week, permanently.

    This is the complement: diff what we hold against a real session
    calendar and re-request whatever is genuinely missing, wherever it
    sits. Layered on top of the interval logic rather than replacing it —
    the interval path stays the cheap common case.

    Returns [] when the calendar is unavailable (fail-open), when nothing
    is missing, or when the cache is empty (the interval path already asks
    for everything).
    """
    if cached is None or cached.empty:
        return []
    from data import market_calendar

    # Alpaca publishes a daily bar after the US-equities market opens
    # (13:30/14:30 UTC). A request ending before the conservative readiness
    # cutoff cannot contain that day's bar, so it is not a repair candidate or
    # gap strike.
    last_session = end.date()
    session_start = datetime.combine(
        last_session, datetime.min.time(), tzinfo=timezone.utc
    )
    if end < session_start + _DAILY_BAR_READY_UTC_OFFSET:
        last_session -= timedelta(days=1)
    if last_session < start.date():
        return []

    have = {pd.Timestamp(ts).tz_convert("UTC").date() for ts in cached.index}
    missing = market_calendar.missing_sessions(
        start.date(), last_session, have, ignore=absent
    )
    if not missing:
        return []

    ranges: list[tuple[datetime, datetime]] = []
    run_start = run_end = None
    for d in sorted(missing):
        if run_start is None:
            run_start = run_end = d
        elif (d - run_end).days <= 4:      # bridge weekends/holidays
            run_end = d
        else:
            ranges.append((run_start, run_end))
            run_start = run_end = d
    if run_start is not None:
        ranges.append((run_start, run_end))

    # Preserve the full start day: a daily bar can be timestamped before a
    # rolling request's wall-clock start.  Only the end is a hard API limit.
    bounded: list[tuple[datetime, datetime]] = []
    for first_day, last_day in ranges:
        range_start = datetime.combine(
            first_day, datetime.min.time(), tzinfo=timezone.utc
        )
        range_end = min(
            datetime.combine(last_day, datetime.max.time(), tzinfo=timezone.utc),
            end,
        )
        if range_start < range_end:
            bounded.append((range_start, range_end))

    if bounded:
        logger.warning(
            f"{symbol}: {len(missing)} session(s) missing from cache inside a "
            f"covered range — re-requesting "
            f"({', '.join(str(d) for d in sorted(missing)[:5])}"
            f"{'…' if len(missing) > 5 else ''})"
        )
    return bounded


def _bound_ranges_to_end(
    ranges: list[tuple[datetime, datetime]], end: datetime
) -> list[tuple[datetime, datetime]]:
    """Keep generated fetch ranges within the caller's end boundary."""
    bounded: list[tuple[datetime, datetime]] = []
    for range_start, range_end in ranges:
        bounded_end = min(range_end, end)
        if range_start < bounded_end:
            bounded.append((range_start, bounded_end))
    return bounded


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
