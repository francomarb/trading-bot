"""
SPY Options Reversion edge filter.

Single gate: SPY close must be above its 100-day SMA.

Rationale: this strategy buys SPY calls on RSI weakness. In a structural bear
market (SPY below 100 SMA), every oversold bounce is a dead-cat setup and the
call value decays against a declining underlying. The 100 SMA is the chosen
universally-recognised regime separator. No additional gates are applied —
SPY is an ETF with no earnings, no liquidity concern, and no breakdown risk
beyond what the regime gate already captures.

Fails CLOSED on API failure (no SPY data, no prior cache) — same as SPYTrendFilter.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from strategies.base import EdgeFilterDecision
from strategies.filters.common import SPYTrendFilter


class SPYOptionsEdgeFilter:
    """
    Entry gate for SPY Options Reversion.

    Gate: SPY close > 100-day SMA.

    Args:
        spy_lookback_days: Calendar days of SPY history to fetch (default 320).
        spy_cache_ttl:     Seconds to reuse cached SPY data (default 600).
    """

    def __init__(
        self,
        *,
        spy_lookback_days: int = 180,
        spy_cache_ttl: float = 600.0,
    ) -> None:
        self._spy_filter = SPYTrendFilter(
            sma_windows=[100],
            lookback_days=spy_lookback_days,
            cache_ttl_seconds=spy_cache_ttl,
        )

    def set_symbol(self, symbol: str) -> None:
        # SPY is both the symbol and the filter target — nothing to propagate.
        pass

    def __call__(self, df: pd.DataFrame) -> EdgeFilterDecision:
        gate: pd.Series = self._spy_filter(df)
        allowed = gate.astype(bool)
        reasons = pd.Series(
            [
                []
                if bool(ok)
                else ["SPY below 100 SMA (bear regime)"]
                for ok in allowed.tolist()
            ],
            index=allowed.index,
            dtype=object,
        )

        if not df.empty:
            if bool(allowed.iloc[-1]):
                logger.info("SPY_OPTIONS_FILTER_ALLOWED — SPY above 100 SMA")
            else:
                logger.info("SPY_OPTIONS_FILTER_BLOCKED — SPY below 100 SMA (bear regime)")

        return EdgeFilterDecision(
            allowed=allowed,
            reasons=reasons,
        )
