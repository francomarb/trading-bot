"""
SMA Crossover edge filter (Phase 10.F3a).

SMAEdgeFilter gates new entries on two active conditions:

  1. Stock structural strength — stock close > stock 200-day SMA.
     A crossover of the 20/50 SMA while the stock is still below its own
     200 SMA is typically a short-term recovery in a structurally weak
     name, not a genuine trend change. Requiring the stock to be above its
     200 SMA ensures the crossover aligns with the long-term trend.
     Fails open (allows) when history is insufficient (<200 bars).

  2. Volume expansion — 10-day average volume > 30-day average volume.
     A crossover on contracting volume is a weak signal; institutions are
     not participating. Expanding volume confirms that the move has
     underlying demand behind it.
     Fails open when insufficient bars or no volume column.

SPY > 200 SMA gate — INTENTIONALLY DISABLED (see note below):
  This filter previously checked SPY close > SPY 200-day SMA directly.
  That rule is now owned by the RegimeDetector (regime/detector.py) as
  the universal BEAR regime gate — it applies to ALL long-only strategies
  and is enforced at the engine level before any filter is called.
  Duplicating it here would be redundant as long as the regime detector
  is active and owns that rule.

  ⚠  RE-ENABLE if either of the following becomes true:
      - The RegimeDetector is disabled, removed, or bypassed.
      - The BEAR regime gate no longer uses SPY > 200 SMA as its signal.
  To re-enable: uncomment the _spy_filter instantiation in __init__, the
  spy_gate line in __call__, and restore `spy_gate &` in `combined`.

Earnings blackout deliberately excluded: SMA crossover is a trend-following
strategy. Earnings are often catalysts that accelerate trends, so blocking
entries around earnings can cause missed setups without meaningfully
reducing risk. Earnings blackout belongs on mean-reversion strategies
(e.g. RSIEdgeFilter) where binary events work against the edge.

Exits are NEVER blocked by this filter — that is enforced by BaseStrategy.

Phase 11 notes (deferred):
  - RSI-at-entry gate: avoid crossovers where RSI is already overbought (>70).
  - Same-day concentration cap: limit how many new entries fire simultaneously
    on correlated signals (broad market rip). Partly addressed by 10.F5
    concentration guardrails; full treatment in Phase 11 portfolio layer.

Usage (forward_test.py):
    from strategies.filters.sma_crossover import SMAEdgeFilter
    edge = SMAEdgeFilter()
    strategy = SMACrossover(fast=20, slow=50, edge_filter=edge)
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

# SPYTrendFilter import retained — needed if the SPY gate is re-enabled.
# See the re-enable note in the module docstring above.
from strategies.filters.common import SPYTrendFilter  # noqa: F401


_STOCK_SMA_WINDOW = 200
_VOL_SHORT_WINDOW = 10
_VOL_LONG_WINDOW  = 30


class SMAEdgeFilter:
    """
    Entry gate for SMA Crossover: stock > 200SMA AND volume expanding.

    The SPY > 200 SMA gate is intentionally disabled — it is owned by
    RegimeDetector as the universal BEAR gate and enforced at the engine
    level. Re-enable if RegimeDetector is disabled or stops owning that rule.

    Args:
        stock_sma_window:  SMA period for the stock's own trend check (default 200).
        vol_short_window:  Short window for volume expansion check (default 10).
        vol_long_window:   Long window for volume expansion check (default 30).

        # SPY gate args — kept for easy re-enable, currently unused:
        # spy_sma_window:    SMA period for the SPY trend check (default 200).
        # spy_lookback_days: Calendar days of SPY history to fetch (default 280).
        # spy_cache_ttl:     Seconds to reuse cached SPY data (default 600).
    """

    def __init__(
        self,
        *,
        stock_sma_window: int = _STOCK_SMA_WINDOW,
        vol_short_window: int = _VOL_SHORT_WINDOW,
        vol_long_window: int = _VOL_LONG_WINDOW,
        # SPY gate params — disabled while RegimeDetector owns SPY > 200 SMA.
        # Uncomment to re-enable:
        # spy_sma_window: int = 200,
        # spy_lookback_days: int = 280,
        # spy_cache_ttl: float = 600.0,
    ) -> None:
        self._stock_sma_window = stock_sma_window
        self._vol_short = vol_short_window
        self._vol_long = vol_long_window
        self._symbol: str = ""

        # SPY gate — disabled while RegimeDetector owns SPY > 200 SMA.
        # Re-enable by uncommenting and restoring spy_gate in __call__:
        # self._spy_filter = SPYTrendFilter(
        #     sma_windows=[spy_sma_window],
        #     lookback_days=spy_lookback_days,
        #     cache_ttl_seconds=spy_cache_ttl,
        # )

    def set_symbol(self, symbol: str) -> None:
        """Injected by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol

    def _stock_above_sma(self, df: pd.DataFrame) -> pd.Series:
        """
        True where stock close > its N-day SMA.
        NaN SMA (insufficient history) → True (fail open).
        """
        close = df["close"]
        sma = close.rolling(self._stock_sma_window).mean()
        above = close > sma
        above = above.where(sma.notna(), other=True)
        return above.astype(bool)

    def _volume_expanding(self, df: pd.DataFrame) -> pd.Series:
        """
        True where the short-window average volume > long-window average volume.
        Expanding volume confirms institutional participation in the crossover.
        Fails open when volume column is absent or there are insufficient bars.
        """
        if "volume" not in df.columns:
            return pd.Series(True, index=df.index, dtype=bool)

        vol = df["volume"].astype(float)
        short_avg = vol.rolling(self._vol_short).mean()
        long_avg  = vol.rolling(self._vol_long).mean()
        expanding = short_avg > long_avg

        # Fail open when either average is NaN (insufficient history).
        has_data = short_avg.notna() & long_avg.notna()
        expanding = expanding.where(has_data, other=True)
        return expanding.astype(bool)

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        # SPY gate — disabled while RegimeDetector owns SPY > 200 SMA.
        # Re-enable by uncommenting and restoring `spy_gate &` in combined:
        # spy_gate = self._spy_filter(df)

        stock_gate = self._stock_above_sma(df)
        vol_gate   = self._volume_expanding(df)

        # When re-enabling SPY gate: combined = spy_gate & stock_gate & vol_gate
        combined = stock_gate & vol_gate

        if not df.empty:
            allowed  = bool(combined.iloc[-1])
            stock_ok = bool(stock_gate.iloc[-1])
            vol_ok   = bool(vol_gate.iloc[-1])

            if allowed:
                logger.debug(
                    f"SMAEdgeFilter: ALLOWED {self._symbol} — "
                    f"stock>200SMA vol_expanding"
                )
            else:
                reasons = []
                if not stock_ok:
                    close = df["close"].iloc[-1]
                    sma_val = df["close"].rolling(self._stock_sma_window).mean().iloc[-1]
                    sma_str = f"{sma_val:.2f}" if pd.notna(sma_val) else "NaN"
                    reasons.append(
                        f"stock {close:.2f} ≤ SMA{self._stock_sma_window} {sma_str}"
                    )
                if not vol_ok:
                    reasons.append(
                        f"volume contracting "
                        f"(avg{self._vol_short} ≤ avg{self._vol_long})"
                    )
                logger.info(
                    f"SMAEdgeFilter: BLOCKED {self._symbol} — "
                    + ", ".join(reasons)
                )

        return combined
