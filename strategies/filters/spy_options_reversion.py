"""
SPY Options Reversion edge filter.

Single gate: SPY close must be above its 200-day SMA.

Rationale: this strategy buys SPY calls on RSI weakness. In a structural bear
market (SPY below 200 SMA), every oversold bounce is a dead-cat setup and the
call value decays against a declining underlying. The 200 SMA is the minimal,
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

    Gate: SPY close > 200-day SMA.

    Args:
        spy_lookback_days: Calendar days of SPY history to fetch (default 320).
        spy_cache_ttl:     Seconds to reuse cached SPY data (default 600).
    """

    def __init__(
        self,
        *,
        spy_lookback_days: int = 320,
        spy_cache_ttl: float = 600.0,
    ) -> None:
        self._spy_filter = SPYTrendFilter(
            sma_windows=[200],
            lookback_days=spy_lookback_days,
            cache_ttl_seconds=spy_cache_ttl,
        )
        self._last_reasons: list[str] = []

    def set_symbol(self, symbol: str) -> None:
        # SPY is both the symbol and the filter target — nothing to propagate.
        pass

    def __call__(self, df: pd.DataFrame) -> EdgeFilterDecision:
        gate: pd.Series = self._spy_filter(df)
        allowed = gate.astype(bool)

        if not df.empty:
            if bool(allowed.iloc[-1]):
                self._last_reasons = []
                logger.info("SPY_OPTIONS_FILTER_ALLOWED — SPY above 200 SMA")
            else:
                self._last_reasons = ["SPY below 200 SMA (bear regime)"]
                logger.info("SPY_OPTIONS_FILTER_BLOCKED — SPY below 200 SMA (bear regime)")

        return EdgeFilterDecision.from_bool_series(
            allowed,
            blocked_reasons=self._last_reasons or None,
        )

    def get_last_block_reasons(self) -> list[str]:
        return list(self._last_reasons)
