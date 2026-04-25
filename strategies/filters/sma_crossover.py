"""
SMA Crossover edge filter (Phase 10.F3a).

SMAEdgeFilter gates new entries on a single condition:

  Market-trend confirmation — SPY close > SPY 200-day SMA.
  Blocks new SMA entries in broad market downtrends where trend-following
  strategies degrade: false crossovers are far more frequent when the
  macro environment is bearish.

Earnings blackout deliberately excluded: SMA crossover is a trend-following
strategy. Earnings are often catalysts that accelerate trends, so blocking
entries around earnings can cause missed setups without meaningfully
reducing risk. Earnings blackout belongs on mean-reversion strategies
(e.g. RSIEdgeFilter) where binary events work against the edge.

Exits are NEVER blocked by this filter — that is enforced by BaseStrategy.

Usage (forward_test.py):
    from strategies.filters.sma_crossover import SMAEdgeFilter
    edge = SMAEdgeFilter()
    strategy = SMACrossover(fast=20, slow=50, edge_filter=edge)
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from strategies.filters.common import SPYTrendFilter


class SMAEdgeFilter:
    """
    Entry gate for SMA Crossover: SPY close > 200-day SMA.

    Args:
        spy_sma_window:    SMA period for the SPY trend check (default 200).
        spy_lookback_days: Calendar days of SPY history to fetch (default 280).
        spy_cache_ttl:     Seconds to reuse cached SPY data (default 600).
    """

    def __init__(
        self,
        *,
        spy_sma_window: int = 200,
        spy_lookback_days: int = 280,
        spy_cache_ttl: float = 600.0,
    ) -> None:
        self._spy_filter = SPYTrendFilter(
            sma_windows=[spy_sma_window],
            lookback_days=spy_lookback_days,
            cache_ttl_seconds=spy_cache_ttl,
        )

    def __call__(self, df: pd.DataFrame, *, symbol: str = "") -> pd.Series:
        gate = self._spy_filter(df)

        if not df.empty:
            allowed = bool(gate.iloc[-1])
            if allowed:
                logger.debug(f"SMAEdgeFilter: ALLOWED {symbol} — SPY above 200SMA")
            else:
                logger.info(f"SMAEdgeFilter: BLOCKED {symbol} — SPY below 200SMA")

        return gate
