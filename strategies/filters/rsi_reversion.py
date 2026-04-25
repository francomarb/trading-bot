"""
RSI Reversion edge filter (Phase 10.F3b).

RSIEdgeFilter implements the Tier 1 gates from docs/RSI-edge-filter.md:

  Rule 1 — Market trend (mandatory):
    SPY close > SPY 200-day SMA   (avoid bear markets)
    SPY close > SPY 50-day SMA    (avoid macro downtrends)

  Rule 2 — Earnings blackout:
    Block new entries within 3 calendar days before / 1 day after earnings.
    RSI reversion buys dips — a dip into a binary earnings event is not a
    mean-reversion setup, it is gap risk. The post-earnings window (1 day)
    lets overnight volatility settle before re-engaging.

  Rule 3 — Long-only mode:
    Already enforced by RSIReversion strategy (only BUY signals emitted).

All conditions must be True for a new entry. Exits are NEVER blocked —
that is enforced by BaseStrategy.

Note: the stock-level 50 SMA gate was intentionally excluded. RSI mean-
reversion targets stocks that have pulled back; a pullback large enough to
trigger RSI oversold will often push the stock below its 50 SMA — filtering
on that condition removes exactly the trades the strategy is designed to take.
The SPY 50/200 SMA gates handle macro regime; stock-level structural weakness
is addressed by the regime detector (Phase 10.F2).

Observability (required by docs/RSI-edge-filter.md):
  - Every raw RSI signal that reaches the filter is logged.
  - Every allow/block decision is logged with the specific reason.

Usage (forward_test.py when RSI is activated in 10.F4):
    from strategies.filters.rsi_reversion import RSIEdgeFilter
    edge = RSIEdgeFilter()
    strategy = RSIReversion(period=14, edge_filter=edge)
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from strategies.filters.common import EarningsBlackout, SPYTrendFilter


class RSIEdgeFilter:
    """
    Entry gate for RSI Reversion: SPY > 200SMA AND SPY > 50SMA AND not near earnings.

    Args:
        spy_lookback_days: Calendar days of SPY history to fetch (default 280).
        spy_cache_ttl:     Seconds to reuse cached SPY data (default 600).
        days_before:       Earnings blackout days before the event (default 3).
        days_after:        Earnings blackout days after the event (default 1).
    """

    def __init__(
        self,
        *,
        spy_lookback_days: int = 280,
        spy_cache_ttl: float = 600.0,
        days_before: int = 3,
        days_after: int = 1,
    ) -> None:
        self._spy_filter = SPYTrendFilter(
            sma_windows=[200, 50],
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

        # Detailed observability log on the last bar.
        if not df.empty:
            allowed = bool(combined.iloc[-1])
            spy_ok = bool(spy_gate.iloc[-1])
            earn_ok = bool(earnings_gate.iloc[-1])

            if allowed:
                logger.info(
                    f"RSI_FILTER_ALLOWED {self._symbol} — "
                    f"SPY gates={spy_ok} earnings={earn_ok}"
                )
            else:
                reasons = []
                if not spy_ok:
                    reasons.append("SPY trend gate failed (below 200 or 50 SMA)")
                if not earn_ok:
                    reasons.append("earnings blackout")
                logger.info(
                    f"RSI_FILTER_BLOCKED {self._symbol} — "
                    + ", ".join(reasons)
                )

        return combined
