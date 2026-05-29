"""
IV proxy data layer (PLAN.md 11.29, refactored under 11.46).

Resolves per-instrument implied-volatility proxies for options strategies.
Alpaca paper does not stream live Greeks, so each instrument points at a
published volatility index instead:

  * SPY, QQQ  → VIX  (``^VIX``)  — SPX-family, tracks both closely
  * IWM       → RVX  (``^RVX``)  — the Russell 2000 volatility index

Two public methods:

* ``resolve(source) -> float`` returns today's IV proxy in **index points**
  (VIX convention: ``14.5`` means 14.5% annualized vol), matching how
  ``min_iv_proxy`` is expressed in ``CREDIT_SPREAD_INSTRUMENTS``. Callers
  that need a decimal sigma for Black-Scholes divide by 100.
* ``resolve_rank(source) -> IVRankSnapshot`` returns where today's value
  sits in its trailing ~1-year range — both min/max rank and ≤-percentile.

Internally the resolver caches the trailing ~1-year daily-close series
per source, keyed by calendar date. The scalar path is pinned as
``float(series.iloc[-1])`` so it stays bit-for-bit compatible with the
pre-11.46 contract that the live credit-spread filter depends on.

Network I/O (yfinance) is isolated behind an injectable ``fetch_fn`` so
the resolver is trivial to unit-test offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Callable

import pandas as pd
from loguru import logger


# Proxy source name (as written in CREDIT_SPREAD_INSTRUMENTS) → index ticker.
_SOURCE_TICKERS: dict[str, str] = {
    "vix": "^VIX",
    "rvx": "^RVX",
}

# Used when a live fetch fails and there is no cached value yet. ~VIX 15 is a
# neutral long-run level; a stale proxy should neither force nor block trades
# on its own — the gate is one of several.
_DEFAULT_FALLBACK_POINTS = 15.0

# Trading-day floor for IVR sufficiency. yfinance ``period="1y"`` returns
# ~250-252 closes depending on the year's holiday schedule; 240 (~48 weeks)
# is the robust floor that still well over-covers the 52-week min/max while
# remaining immune to holiday / early-close variation.
_DEFAULT_LOOKBACK_FLOOR = 240


# fetch_fn signature: (yfinance_ticker: str) -> pd.Series | None.
# Returns a daily-close series indexed by date (timezone-naive or aware OK).
FetchFn = Callable[[str], "pd.Series | None"]


@dataclass(frozen=True)
class IVRankSnapshot:
    """Where today's IV proxy sits in its trailing ~1-year range.

    ``rank`` and ``percentile`` are ``None`` when the trailing series is
    degenerate (max == min — no spread to rank against) or empty. ``sufficient``
    is True only when ``lookback_days_used >= _DEFAULT_LOOKBACK_FLOOR`` AND a
    well-defined rank can be computed — callers pick their own fail-open or
    fail-closed posture based on this field.
    """

    source: str           # "vix" | "rvx"
    current: float        # today's IV proxy in index points
    rank: float | None    # (current - min) / (max - min) over trailing window, in [0, 1]
    percentile: float | None  # fraction of trailing closes <= current, in [0, 1]
    lookback_days_used: int   # count of non-NaN closes in the trailing series
    as_of: date
    sufficient: bool


def _yfinance_fetch(ticker: str) -> pd.Series | None:
    """Default fetcher: trailing ~1-year daily closes of a vol index via yfinance.

    Returns a ``pd.Series`` of ``Close`` values indexed by date, or ``None``
    on any failure. The IV-proxy data layer needs the full series — today's
    scalar is derived from ``series.iloc[-1]``.
    """
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period="1y")
        if hist.empty:
            return None
        return hist["Close"].astype(float)
    except Exception as e:
        logger.debug(f"iv_proxy: yfinance fetch failed for {ticker}: {e}")
        return None


class IVProxyResolver:
    """
    Resolves a per-instrument IV proxy source to a current value and rank.

    Caches the trailing ~1-year close series per source for the calendar day —
    the proxy moves slowly enough that intraday refreshes add network risk
    without adding signal. The scalar ``resolve()`` path is pinned as
    ``float(series.iloc[-1])`` so it stays bit-for-bit compatible with the
    pre-11.46 contract.
    """

    def __init__(
        self,
        *,
        fetch_fn: FetchFn | None = None,
        fallback_points: float = _DEFAULT_FALLBACK_POINTS,
        lookback_floor: int = _DEFAULT_LOOKBACK_FLOOR,
    ) -> None:
        self._fetch_fn: FetchFn = fetch_fn or _yfinance_fetch
        self._fallback = fallback_points
        self._lookback_floor = int(lookback_floor)
        # source → (date, trailing-1y close series)
        self._cache: dict[str, tuple[date, pd.Series]] = {}

    # ── Internal series resolution ───────────────────────────────────────────

    def _resolve_series(self, source: str) -> tuple[str, pd.Series | None]:
        """Return ``(canonical_source_key, series)`` for ``source``.

        On a fresh successful fetch the cache is refreshed; on a fetch failure
        the last cached series is returned if present, else ``None``.

        Raises ``ValueError`` for an unknown source — a config typo should
        fail loudly, not silently trade on a fallback.
        """
        key = source.lower()
        ticker = _SOURCE_TICKERS.get(key)
        if ticker is None:
            raise ValueError(
                f"unknown IV proxy source {source!r} — "
                f"valid sources: {sorted(_SOURCE_TICKERS)}"
            )

        today = date.today()
        cached = self._cache.get(key)
        if cached is not None and cached[0] == today:
            return key, cached[1]

        series = self._fetch_fn(ticker)
        if series is not None:
            series = pd.Series(series).dropna().astype(float)
            # Drop non-positive prints — a vol index ≤ 0 is bad data.
            series = series[series > 0]
            if len(series) > 0:
                self._cache[key] = (today, series)
                logger.debug(
                    f"iv_proxy: {key} ({ticker}) refreshed — "
                    f"{len(series)} days, latest={float(series.iloc[-1]):.2f}"
                )
                return key, series

        # Fetch failed or returned an unusable series — reuse stale cache if any.
        if cached is not None:
            logger.warning(
                f"iv_proxy: {key} fetch failed; reusing stale series from {cached[0]} "
                f"({len(cached[1])} days, latest={float(cached[1].iloc[-1]):.2f})"
            )
            return key, cached[1]

        return key, None

    # ── Public scalar API (bit-for-bit compatible with pre-11.46) ────────────

    def resolve(self, source: str) -> float:
        """
        Return the current IV proxy for ``source`` in index points.

        Pinned implementation: ``float(series.iloc[-1])`` from the cached
        trailing-1y series. Stays bit-for-bit compatible with the pre-11.46
        scalar contract that the live credit-spread filter depends on.

        On a fetch failure with no prior cache, returns the fallback
        (``_DEFAULT_FALLBACK_POINTS=15.0``) and logs a warning.
        """
        key, series = self._resolve_series(source)
        if series is None or len(series) == 0:
            logger.warning(
                f"iv_proxy: {key} fetch failed and no cache — "
                f"using fallback {self._fallback:.2f}"
            )
            return self._fallback
        return float(series.iloc[-1])

    # ── Public IV Rank API (new in 11.46) ────────────────────────────────────

    def resolve_rank(self, source: str) -> IVRankSnapshot:
        """
        Return today's IV proxy rank + percentile against the trailing-1y series.

        * ``rank`` = ``(current - min) / (max - min)`` over the trailing window.
        * ``percentile`` = fraction of trailing closes ≤ ``current``.

        Returns a snapshot with ``sufficient=False`` and ``rank=None`` /
        ``percentile=None`` when:

        - the trailing series is empty (no fetch ever succeeded), or
        - the series has ``max == min`` (degenerate — no spread to rank against), or
        - ``lookback_days_used < lookback_floor`` (default 240 trading days).

        Insufficient-history snapshots still report ``current`` (from the
        fallback if no series ever cached) and ``lookback_days_used`` so the
        operator can see how the cold-start is progressing.
        """
        key, series = self._resolve_series(source)
        today = date.today()

        if series is None or len(series) == 0:
            return IVRankSnapshot(
                source=key,
                current=self._fallback,
                rank=None,
                percentile=None,
                lookback_days_used=0,
                as_of=today,
                sufficient=False,
            )

        current = float(series.iloc[-1])
        lookback_days_used = len(series)
        s_max = float(series.max())
        s_min = float(series.min())

        # Degenerate series — no spread to rank against. Do not fabricate a
        # 1.0 percentile (Gemini-review point D).
        if s_max == s_min:
            return IVRankSnapshot(
                source=key,
                current=current,
                rank=None,
                percentile=None,
                lookback_days_used=lookback_days_used,
                as_of=today,
                sufficient=False,
            )

        rank = (current - s_min) / (s_max - s_min)
        percentile = float((series <= current).sum()) / lookback_days_used
        sufficient = lookback_days_used >= self._lookback_floor

        return IVRankSnapshot(
            source=key,
            current=current,
            rank=float(rank),
            percentile=float(percentile),
            lookback_days_used=lookback_days_used,
            as_of=today,
            sufficient=sufficient,
        )


def is_valid_source(source: str) -> bool:
    """True if ``source`` is a recognized IV proxy source name."""
    return source.lower() in _SOURCE_TICKERS
