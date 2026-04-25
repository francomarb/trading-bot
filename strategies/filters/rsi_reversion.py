"""
RSI Reversion edge filter (Phase 10.F3b).

RSIEdgeFilter implements the Tier 1 gates from docs/RSI-edge-filter.md:

  Rule 1 — Market trend (mandatory):
    SPY close > SPY 200-day SMA   (avoid bear markets)
    SPY close > SPY 50-day SMA    (avoid macro downtrends)

  Rule 2 — Symbol trend:
    Stock close > stock 50-day SMA  (avoid structurally weak stocks;
                                     computed from the symbol's own df)

  Rule 3 — Long-only mode:
    Already enforced by RSIReversion strategy (only BUY signals emitted).

All three Tier 1 conditions must be True for a new entry. Exits are NEVER
blocked — that is enforced by BaseStrategy.

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

from strategies.filters.common import SPYTrendFilter


_STOCK_SMA_WINDOW = 50   # stock must be above its own 50-day SMA


class RSIEdgeFilter:
    """
    Entry gate for RSI Reversion: SPY > 200SMA AND SPY > 50SMA AND stock > 50SMA.

    Args:
        spy_lookback_days: Calendar days of SPY history to fetch (default 280).
        spy_cache_ttl:     Seconds to reuse cached SPY data (default 600).
        stock_sma_window:  SMA window for the symbol's own trend check (default 50).
    """

    def __init__(
        self,
        *,
        spy_lookback_days: int = 280,
        spy_cache_ttl: float = 600.0,
        stock_sma_window: int = _STOCK_SMA_WINDOW,
    ) -> None:
        self._spy_filter = SPYTrendFilter(
            sma_windows=[200, 50],
            lookback_days=spy_lookback_days,
            cache_ttl_seconds=spy_cache_ttl,
        )
        self._stock_sma_window = stock_sma_window
        self._symbol: str = ""

    def set_symbol(self, symbol: str) -> None:
        """Injected by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol

    def _stock_above_sma(self, df: pd.DataFrame) -> pd.Series:
        """
        Return True for each bar where the symbol's close > its N-day SMA.
        NaN SMA values (insufficient history) are treated as True (fail open).
        """
        close = df["close"]
        sma = close.rolling(self._stock_sma_window).mean()
        # NaN → True (not enough history, don't block)
        above = close > sma
        above = above.where(sma.notna(), other=True)
        return above.astype(bool)

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        spy_gate = self._spy_filter(df)
        stock_gate = self._stock_above_sma(df)

        combined = spy_gate & stock_gate

        # Detailed observability log on the last bar.
        if not df.empty:
            allowed = bool(combined.iloc[-1])
            spy_ok = bool(spy_gate.iloc[-1])
            stock_ok = bool(stock_gate.iloc[-1])

            close_last = df["close"].iloc[-1]
            sma_last = df["close"].rolling(self._stock_sma_window).mean().iloc[-1]

            sma_str = f"{sma_last:.2f}" if pd.notna(sma_last) else "NaN"
            if allowed:
                logger.info(
                    f"RSI_FILTER_ALLOWED {self._symbol} — "
                    f"SPY gates={spy_ok} "
                    f"stock {close_last:.2f} > SMA{self._stock_sma_window} {sma_str}"
                )
            else:
                reasons = []
                if not spy_ok:
                    reasons.append("SPY trend gate failed (below 200 or 50 SMA)")
                if not stock_ok:
                    reasons.append(
                        f"stock {close_last:.2f} ≤ SMA{self._stock_sma_window} {sma_str}"
                    )
                logger.info(
                    f"RSI_FILTER_BLOCKED {self._symbol} — "
                    + ", ".join(reasons)
                )

        return combined
