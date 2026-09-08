"""
SMA crossover — the MVP trend-following strategy.

Logic
-----
Given a fast window `F` and slow window `S` with `F < S`:

  entry[t]  = True  iff  SMA_F(t) > SMA_S(t)  AND  SMA_F(t-1) <= SMA_S(t-1)
  exit[t]   = True  iff  SMA_F(t) < SMA_S(t)  AND  SMA_F(t-1) >= SMA_S(t-1)

In plain English: "entry" fires on the bar where the fast SMA crosses *above*
the slow SMA; "exit" fires on the bar where it crosses *below*. Anywhere
either SMA is NaN (early bars) the signal is False.

Look-ahead safety
-----------------
Both `rolling(...).mean()` and `shift(1)` use only past data, so the signal at
bar t depends only on closes up to and including t. The Phase 5 backtester
shifts execution to t+1's open; this strategy does *not* itself shift.

Order type
----------
Trend-followers prefer marketable orders — a crossover means the move is
already underway and missing the fill is worse than paying a few bps of
spread. The Phase 7 execution layer reads `preferred_order_type` and routes
accordingly.
"""

from __future__ import annotations

import pandas as pd

from indicators.technicals import add_sma
from strategies.base import BaseStrategy, EdgeFilter, OrderType, SignalFrame


class SMACrossover(BaseStrategy):
    name = "sma_crossover"
    preferred_order_type = OrderType.MARKET

    def __init__(
        self,
        fast: int = 20,
        slow: int = 50,
        *,
        edge_filter: EdgeFilter | None = None,
    ) -> None:
        super().__init__(edge_filter=edge_filter)
        if not isinstance(fast, int) or not isinstance(slow, int):
            raise TypeError("fast and slow must be integers")
        if fast < 1 or slow < 1:
            raise ValueError("fast and slow must be positive")
        if fast >= slow:
            raise ValueError(
                f"fast ({fast}) must be strictly less than slow ({slow})"
            )
        self.fast = fast
        self.slow = slow

    def required_bars(self) -> int:
        """Need at least `slow` bars for the slow SMA to produce a value."""
        return self.slow

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        if "close" not in df.columns:
            raise ValueError("SMACrossover requires a 'close' column")

        with_sma = add_sma(df, self.fast)
        with_sma = add_sma(with_sma, self.slow)

        fast_ma = with_sma[f"sma_{self.fast}"]
        slow_ma = with_sma[f"sma_{self.slow}"]

        diff = fast_ma - slow_ma
        prev = diff.shift(1)

        # Crossover: today above, yesterday at-or-below. Crossunder: mirror.
        entries = (diff > 0) & (prev <= 0)
        exits = (diff < 0) & (prev >= 0)

        # Anywhere either SMA is NaN → signal is False, not NaN.
        entries = entries.fillna(False).astype(bool)
        exits = exits.fillna(False).astype(bool)

        return SignalFrame(entries=entries, exits=exits)

    def candidate_features(self, df: pd.DataFrame) -> dict[str, object]:
        """Describe crossover shape without assigning it a quality score."""
        with_sma = add_sma(df, self.fast)
        with_sma = add_sma(with_sma, self.slow)
        close = float(df["close"].iloc[-1])
        fast = with_sma[f"sma_{self.fast}"]
        slow = with_sma[f"sma_{self.slow}"]
        fast_now = float(fast.iloc[-1])
        slow_now = float(slow.iloc[-1])
        previous_close = float(df["close"].iloc[-2]) if len(df) > 1 else None
        return {
            "fast_window": self.fast,
            "slow_window": self.slow,
            "fast_sma": fast_now,
            "slow_sma": slow_now,
            "crossover_gap_pct": (fast_now - slow_now) / close,
            "fast_slope_pct": (
                (fast_now - float(fast.iloc[-2])) / close if len(fast) > 1 else None
            ),
            "slow_slope_pct": (
                (slow_now - float(slow.iloc[-2])) / close if len(slow) > 1 else None
            ),
            "one_bar_return_pct": (
                close / previous_close - 1.0 if previous_close else None
            ),
        }

    def __repr__(self) -> str:
        return f"SMACrossover(fast={self.fast}, slow={self.slow})"
