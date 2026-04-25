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
  - Fail open: if SPY data or earnings data is unavailable, allow the trade
    rather than block it silently. Log the failure so the operator knows.
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
        lookback_days: Calendar days of SPY history to fetch (default 280 covers
                       200 trading days with weekends/holidays).
        cache_ttl_seconds: How long to reuse a cached SPY fetch (default 600 s).
    """

    def __init__(
        self,
        *,
        sma_windows: list[int],
        lookback_days: int = 280,
        cache_ttl_seconds: float = 600.0,
    ) -> None:
        if not sma_windows:
            raise ValueError("sma_windows must not be empty")
        self._windows = sorted(sma_windows)
        self._lookback_days = lookback_days
        self._cache_ttl = cache_ttl_seconds
        self._spy_cache: pd.DataFrame | None = None
        self._cache_time: float = 0.0

    def _fetch_spy(self) -> pd.DataFrame | None:
        """Fetch SPY bars, reusing cache within TTL."""
        now = time.monotonic()
        if (
            self._spy_cache is not None
            and (now - self._cache_time) < self._cache_ttl
        ):
            return self._spy_cache
        try:
            from data.fetcher import fetch_symbol
            df = fetch_symbol("SPY", "1Day", lookback_days=self._lookback_days)
            self._spy_cache = df
            self._cache_time = now
            logger.debug(
                f"SPYTrendFilter: fetched {len(df)} SPY bars "
                f"(windows={self._windows})"
            )
            return df
        except Exception as e:
            logger.warning(
                f"SPYTrendFilter: failed to fetch SPY bars — {e}. "
                "Defaulting to ALLOW (fail open). Will retry after TTL."
            )
            # Advance cache_time so we don't hammer the API (and spam logs)
            # on every engine cycle during an outage — retry after full TTL.
            self._cache_time = now
            return self._spy_cache  # may be None (first failure) or stale

    def _check(self) -> tuple[bool, str]:
        """Return (allowed, reason) based on latest SPY data."""
        spy = self._fetch_spy()
        if spy is None or spy.empty:
            return True, "no SPY data — fail open"

        close = spy["close"]
        last_close = close.iloc[-1]

        for window in self._windows:
            sma = close.rolling(window).mean()
            sma_val = sma.iloc[-1]
            if pd.isna(sma_val):
                logger.debug(
                    f"SPYTrendFilter: SMA{window} is NaN (insufficient bars) "
                    "— skipping this window"
                )
                continue
            if last_close <= sma_val:
                reason = (
                    f"SPY {last_close:.2f} ≤ SMA{window} {sma_val:.2f}"
                )
                return False, reason

        return True, f"SPY {last_close:.2f} above all SMAs {self._windows}"

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        allowed, reason = self._check()
        if not allowed:
            logger.info(f"SPYTrendFilter: BLOCKED — {reason}")
        else:
            logger.debug(f"SPYTrendFilter: allowed — {reason}")
        return pd.Series(allowed, index=df.index, dtype=bool)


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
            ticker = yf.Ticker(symbol)
            dates: list[datetime.date] = []

            # Upcoming earnings from .calendar
            cal = ticker.calendar
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
                            try:
                                dates.append(pd.Timestamp(val).date())
                            except Exception:
                                pass

            # Historical earnings from .earnings_dates
            hist = ticker.earnings_dates
            if hist is not None and not hist.empty:
                for idx in hist.index:
                    try:
                        dates.append(pd.Timestamp(idx).date())
                    except Exception:
                        pass

            self._cache[symbol] = (today, dates)
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
