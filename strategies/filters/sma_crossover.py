"""
SMA Crossover edge filter (Phase 10.F3a).

SMAEdgeFilter gates new entries with two conditions (from PLAN.md / docs/strategies.md):

  1. Market-trend confirmation — SPY close > SPY 200-day SMA.
     Blocks new SMA entries in broad market downtrends where trend-following
     strategies degrade: false crossovers are far more frequent when the
     macro environment is bearish.

  2. Earnings-blackout veto — blocks new entries in the N calendar days
     surrounding a symbol's earnings date. SMA crossover exits are slow
     (lagging indicator); an overnight gap on earnings can blow through the
     ATR stop before the exit fires.

Exits are NEVER blocked by this filter — that is enforced by BaseStrategy.

Usage (forward_test.py):
    from strategies.filters.sma_crossover import SMAEdgeFilter
    edge = SMAEdgeFilter()
    strategy = SMACrossover(fast=20, slow=50, edge_filter=edge)

The filter is a single callable that chains SPYTrendFilter → EarningsBlackout.
set_symbol() is called automatically by BaseStrategy.generate_signals() before
each __call__, routing the correct earnings data per symbol.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from strategies.filters.common import EarningsBlackout, SPYTrendFilter


class SMAEdgeFilter:
    """
    Entry gate for SMA Crossover: SPY > 200SMA AND not near earnings.

    Args:
        spy_sma_window:   SMA period for the SPY trend check (default 200).
        days_before:      Earnings blackout days before the event (default 5).
        days_after:       Earnings blackout days after the event (default 2).
        spy_lookback_days: Calendar days of SPY history to fetch (default 280).
        spy_cache_ttl:    Seconds to reuse cached SPY data (default 600).
    """

    def __init__(
        self,
        *,
        spy_sma_window: int = 200,
        days_before: int = 5,
        days_after: int = 2,
        spy_lookback_days: int = 280,
        spy_cache_ttl: float = 600.0,
    ) -> None:
        self._spy_filter = SPYTrendFilter(
            sma_windows=[spy_sma_window],
            lookback_days=spy_lookback_days,
            cache_ttl_seconds=spy_cache_ttl,
        )
        self._earnings = EarningsBlackout(
            days_before=days_before,
            days_after=days_after,
        )
        self._symbol: str = ""

    def set_symbol(self, symbol: str) -> None:
        """Injected by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol
        self._earnings.set_symbol(symbol)

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        spy_gate = self._spy_filter(df)
        earnings_gate = self._earnings(df)

        combined = spy_gate & earnings_gate

        # Log the combined outcome at the last bar only (avoid flooding).
        if not df.empty:
            allowed = bool(combined.iloc[-1])
            spy_ok = bool(spy_gate.iloc[-1])
            earn_ok = bool(earnings_gate.iloc[-1])
            if allowed:
                logger.debug(
                    f"SMAEdgeFilter: ALLOWED {self._symbol} — "
                    f"spy={spy_ok} earnings={earn_ok}"
                )
            else:
                reasons = []
                if not spy_ok:
                    reasons.append("SPY below 200SMA")
                if not earn_ok:
                    reasons.append("earnings blackout")
                logger.info(
                    f"SMAEdgeFilter: BLOCKED {self._symbol} — "
                    + ", ".join(reasons)
                )

        return combined
