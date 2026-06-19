"""
Shared edge-filter building blocks (Phase 10.F3a / 10.F3b).

Two reusable callables used by both SMA and RSI filters:

  SPYTrendFilter  — gates entries based on SPY close vs. one or more SMAs.
                    Fetches SPY bars internally with a short-lived cache so a
                    16-symbol cycle does not hit the data API 16 times.

  EarningsBlackout — vetoes SMA entries N calendar days surrounding a known
                     earnings date. Uses yfinance; degrades gracefully when
                     data is unavailable. Symbol-aware via set_symbol().

Design principles (from docs/RSI-edge-filter.md and docs/strategies.md):
  - Simple, deterministic, auditable — no ML, no lookahead.
  - SPYTrendFilter fails CLOSED on cold-start API failure (no prior cache).
    If a prior cache exists, stale data is reused and a WARNING is logged —
    last known SPY state is a safe proxy for brief outages.
  - Every configured SPY SMA is mandatory. Insufficient history fails closed
    rather than silently weakening a macro entry gate.
  - EarningsBlackout fails open — missing earnings data affects one symbol
    at a time; yfinance outages are common; silent blocking would be worse.
  - Exits are NEVER blocked — that responsibility lives in BaseStrategy.
  - Every filter action is logged at INFO (allowed) or INFO (blocked) so
    the operator can reconstruct why any signal was suppressed.
"""

from __future__ import annotations

import datetime
import time
from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger

from strategies.base import EdgeFilterDecision, normalize_edge_filter_result

if TYPE_CHECKING:
    pass


# ── SPYTrendFilter ───────────────────────────────────────────────────────────


class SPYTrendFilter:
    """
    Returns True for each bar in `df` when SPY's latest close is above all
    specified SMA windows.

    Fetches SPY daily bars internally with a configurable TTL cache so the
    same SPY data is reused across all symbols in one engine cycle.

    Args:
        sma_windows: SMA periods to check (e.g. [200] for SMA, [200, 50] for RSI).
        lookback_days: Calendar days of SPY history to fetch (default 320).
        cache_ttl_seconds: How long to reuse a cached SPY fetch (default 600 s).
        sma_tolerance_pct: Allow SPY up to this fraction below each SMA.
    """

    def __init__(
        self,
        *,
        sma_windows: list[int],
        lookback_days: int = 320,
        cache_ttl_seconds: float = 600.0,
        sma_tolerance_pct: float = 0.0,
    ) -> None:
        if not sma_windows:
            raise ValueError("sma_windows must not be empty")
        if sma_tolerance_pct < 0:
            raise ValueError("sma_tolerance_pct must be >= 0")
        self._windows = sorted(sma_windows)
        self._lookback_days = lookback_days
        self._cache_ttl = cache_ttl_seconds
        self._sma_tolerance_pct = float(sma_tolerance_pct)
        self._spy_cache: pd.DataFrame | None = None
        self._cache_time: float = 0.0
        self._last_reason: str = ""

    @property
    def last_reason(self) -> str:
        """Return the reason produced by the most recent gate evaluation."""
        return self._last_reason

    def _fetch_spy(self) -> pd.DataFrame | None:
        """Fetch SPY bars, reusing cache within TTL."""
        now = time.monotonic()
        if (
            self._spy_cache is not None
            and (now - self._cache_time) < self._cache_ttl
        ):
            return self._spy_cache
        try:
            from config.settings import ALPACA_DATA_FEED
            from data.fetcher import fetch_symbol
            end = datetime.datetime.now(datetime.timezone.utc)
            start = end - datetime.timedelta(days=self._lookback_days)
            # Live engine path — SPYTrendFilter is evaluated each cycle.
            df, _stats = fetch_symbol(
                "SPY", start, end, timeframe="1Day", feed=ALPACA_DATA_FEED
            )
            self._spy_cache = df
            self._cache_time = now
            logger.debug(
                f"SPYTrendFilter: fetched {len(df)} SPY bars "
                f"(windows={self._windows})"
            )
            return df
        except Exception as e:
            if self._spy_cache is not None:
                logger.warning(
                    f"SPYTrendFilter: failed to fetch SPY bars — {e}. "
                    "Using stale cache. Will retry after TTL."
                )
            else:
                logger.error(
                    f"SPYTrendFilter: failed to fetch SPY bars and no prior "
                    f"cache exists — {e}. Failing CLOSED (blocking all entries)."
                )
            # Advance cache_time so we don't hammer the API (and spam logs)
            # on every engine cycle during an outage — retry after full TTL.
            self._cache_time = now
            return self._spy_cache  # None (first failure) → fail closed; stale → reused

    def _check(self) -> tuple[bool, str]:
        """Return (allowed, reason) based on latest SPY data."""
        spy = self._fetch_spy()
        if spy is None or spy.empty:
            # No prior cache exists — fail closed to prevent entries during
            # a cold-start API outage (e.g. market crash + data API down).
            return False, "no SPY data and no prior cache — fail closed"

        close = spy["close"]
        last_close = close.iloc[-1]

        for window in self._windows:
            sma = close.rolling(window).mean()
            sma_val = sma.iloc[-1]
            if pd.isna(sma_val):
                reason = (
                    f"insufficient SPY history for SMA{window}: "
                    f"{len(close)} bars available, {window} required"
                )
                return False, reason
            threshold = sma_val * (1.0 - self._sma_tolerance_pct)
            if last_close <= threshold:
                if self._sma_tolerance_pct > 0:
                    reason = (
                        f"SPY {last_close:.2f} ≤ SMA{window} tolerance floor {threshold:.2f} "
                        f"(SMA {sma_val:.2f}, tolerance {self._sma_tolerance_pct:.1%})"
                    )
                else:
                    reason = f"SPY {last_close:.2f} ≤ SMA{window} {sma_val:.2f}"
                return False, reason

        if self._sma_tolerance_pct > 0:
            return (
                True,
                f"SPY {last_close:.2f} within {self._sma_tolerance_pct:.1%} "
                f"of all SMAs {self._windows}",
            )
        return True, f"SPY {last_close:.2f} above all SMAs {self._windows}"

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        allowed, reason = self._check()
        self._last_reason = reason
        if not allowed:
            logger.info(f"SPYTrendFilter: BLOCKED — {reason}")
        else:
            logger.debug(f"SPYTrendFilter: allowed — {reason}")
        return pd.Series(allowed, index=df.index, dtype=bool)


# ── CompositeEdgeFilter ──────────────────────────────────────────────────────


class CompositeEdgeFilter:
    """AND-chains multiple ``EdgeFilter`` callables into one.

    Each child filter's ``set_symbol()`` is called if it exists, and
    ``__call__`` results are normalized and AND-ed together. New filters
    should return ``EdgeFilterDecision`` when they have meaningful block
    reasons; plain boolean ``pd.Series`` remains a compatibility fallback.

    Deprecation note:
        The boolean-only filter style is being phased out for first-party
        filters. Mixed-mode composition is still supported here so active
        legacy filters can coexist with migrated structured filters during
        rollout.
    """

    def __init__(self, filters: list) -> None:
        if not filters:
            raise ValueError("CompositeEdgeFilter requires at least one filter")
        self._filters = list(filters)
        self._last_reasons: list[str] = []

    def set_symbol(self, symbol: str) -> None:
        for f in self._filters:
            setter = getattr(f, "set_symbol", None)
            if callable(setter):
                setter(symbol)

    def __call__(self, df: pd.DataFrame) -> pd.Series | EdgeFilterDecision:
        """
        Compose child filters over ``df``.

        Returns ``EdgeFilterDecision`` if any child already uses the structured
        contract; otherwise returns the legacy boolean gate for compatibility
        with older direct callers.
        """
        result: EdgeFilterDecision | None = None
        saw_structured = False
        for f in self._filters:
            raw_result = f(df)
            if isinstance(raw_result, EdgeFilterDecision):
                saw_structured = True
            getter = getattr(f, "get_last_block_reasons", None)
            legacy_blocked_reasons = list(getter() or []) if callable(getter) else None
            decision = normalize_edge_filter_result(
                raw_result,
                df.index,
                legacy_blocked_reasons=legacy_blocked_reasons,
            )
            result = decision if result is None else result.and_with(decision)
        assert result is not None
        self._last_reasons = result.latest_reasons
        return result if saw_structured else result.allowed

    def get_last_block_reasons(self) -> list[str]:
        return list(self._last_reasons)


# ── EarningsBlackout ─────────────────────────────────────────────────────────


class EarningsBlackout:
    """
    Vetoes entries within a configurable window around known earnings dates.

    Uses yfinance to look up upcoming (and recent past) earnings dates.
    Degrades gracefully: if yfinance is unavailable or returns no data, the
    filter allows the trade and logs a warning.

    Requires set_symbol(symbol) to be called before __call__ — BaseStrategy
    does this automatically when the filter has a set_symbol method.

    Args:
        days_before: Block entries this many calendar days before earnings.
        days_after:  Block entries this many calendar days after earnings.
    """

    def __init__(
        self,
        *,
        days_before: int = 5,
        days_after: int = 2,
    ) -> None:
        self._days_before = days_before
        self._days_after = days_after
        self._symbol: str = ""
        # Cache: symbol → (cache_date, list[date])
        self._cache: dict[str, tuple[datetime.date, list[datetime.date]]] = {}
        # Permanent cache: symbol → quoteType (e.g. "EQUITY", "ETF")
        self._quote_types: dict[str, str] = {}

    def set_symbol(self, symbol: str) -> None:
        """Called by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol

    def _get_earnings_dates(self, symbol: str) -> list[datetime.date]:
        """Return known earnings dates for symbol, using a daily cache."""
        today = datetime.date.today()
        if symbol in self._cache:
            cached_date, cached_dates = self._cache[symbol]
            if cached_date == today:
                return cached_dates

        try:
            import yfinance as yf
            import os
            import contextlib
            
            ticker = yf.Ticker(symbol)
            dates: list[datetime.date] = []

            # Determine quote type using permanent cache to avoid rate limits.
            # NOTE: We use yfinance for this because Alpaca groups all stocks and ETFs
            # under the generic 'us_equity' asset class and does not expose a native
            # quote_type or is_etf flag.
            qtype = self._quote_types.get(symbol)
            if not qtype:
                with open(os.devnull, "w") as devnull:
                    with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                        # Use fast_info if possible, fallback to info
                        try:
                            qtype = str(ticker.info.get("quoteType", "EQUITY")).upper()
                        except Exception:
                            qtype = "EQUITY"
                self._quote_types[symbol] = qtype

            if qtype == "ETF":
                # ETFs do not have quarterly earnings; skip the calendar fetch completely
                self._cache[symbol] = (today, dates)
                return dates

            # Suppress yfinance internal prints for missing data
            with open(os.devnull, "w") as devnull:
                with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                    cal = ticker.calendar
                    hist = ticker.earnings_dates

            if cal is not None and not (
                isinstance(cal, pd.DataFrame) and cal.empty
            ):
                if isinstance(cal, pd.DataFrame):
                    for col in cal.columns:
                        if "earnings" in col.lower():
                            val = cal[col].iloc[0]
                            if val is not None:
                                try:
                                    dates.append(pd.Timestamp(val).date())
                                except Exception:
                                    pass
                elif isinstance(cal, dict):
                    for key, val in cal.items():
                        if "earnings" in key.lower() and val is not None:
                            # yfinance sometimes returns lists for 'Earnings Date' (e.g. [datetime.date(...)])
                            # and floats for estimates like 'Earnings High'
                            if isinstance(val, list) and len(val) > 0:
                                val = val[0]
                            if isinstance(val, (float, int)) and val < 10000:
                                continue
                            try:
                                dates.append(pd.Timestamp(val).date())
                            except Exception:
                                pass

            if hist is not None and not hist.empty:
                for idx in hist.index:
                    try:
                        dates.append(pd.Timestamp(idx).date())
                    except Exception:
                        pass

            self._cache[symbol] = (today, dates)
            if not dates:
                logger.warning(
                    f"EarningsBlackout: {symbol} — found 0 earnings dates. "
                    f"This {qtype} is missing fundamental calendar data in Yahoo Finance!"
                )
            else:
                logger.debug(
                    f"EarningsBlackout: {symbol} — found {len(dates)} earnings dates"
                )
            return dates

        except Exception as e:
            logger.warning(
                f"EarningsBlackout: failed to fetch earnings for {symbol} — {e}. "
                "Defaulting to ALLOW (fail open)."
            )
            # Return stale cache if available, else empty.
            if symbol in self._cache:
                return self._cache[symbol][1]
            return []

    def _is_blacked_out(self, symbol: str, check_date: datetime.date) -> bool:
        """Return True if check_date falls within any earnings blackout window."""
        for ed in self._get_earnings_dates(symbol):
            delta = (check_date - ed).days
            if -self._days_before <= delta <= self._days_after:
                return True
        return False

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        symbol = self._symbol
        if not symbol or df.empty:
            return pd.Series(True, index=df.index, dtype=bool)

        # Check the date of the most recent bar.
        last_idx = df.index[-1]
        check_date = (
            last_idx.date() if hasattr(last_idx, "date") else last_idx
        )

        blocked = self._is_blacked_out(symbol, check_date)
        if blocked:
            logger.info(
                f"EarningsBlackout: BLOCKED {symbol} — "
                f"{check_date} within {self._days_before}d before / "
                f"{self._days_after}d after earnings"
            )
        else:
            logger.debug(
                f"EarningsBlackout: allowed {symbol} — "
                f"no earnings within blackout window of {check_date}"
            )
        return pd.Series(not blocked, index=df.index, dtype=bool)
