"""
RSI mean-reversion strategy.

Logic
-----
Given an RSI period `P`, an oversold threshold `oversold`, and an overbought
threshold `overbought`:

  entry[t] = True  iff  RSI(t) < oversold   AND  RSI(t-1) >= oversold
  exit[t]  = True  iff  RSI(t) > overbought AND  RSI(t-1) <= overbought

In plain English: "entry" fires on the bar where RSI crosses *below* the
oversold threshold (30 by default); "exit" fires when RSI crosses *above*
the overbought threshold (70 by default). Anywhere RSI is NaN (early bars)
the signal is False.

Look-ahead safety
-----------------
`rolling` / `shift` / `diff` use only past data, so the signal at bar t
depends only on closes up to and including t. The Phase 5 backtester shifts
execution to t+1's open; this strategy does *not* itself shift.

Order type
----------
Mean-reversion strategies prefer limit orders — we're fading an extreme move,
so we can afford to wait for a fill at a better price rather than chasing
with a market order.
"""

from __future__ import annotations

import pandas as pd

from indicators.technicals import add_rsi
from strategies.base import BaseStrategy, EdgeFilter, OrderType, SignalFrame


class RSIReversion(BaseStrategy):
    name = "rsi_reversion"
    preferred_order_type = OrderType.LIMIT

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        *,
        edge_filter: EdgeFilter | None = None,
    ) -> None:
        super().__init__(edge_filter=edge_filter)
        if not isinstance(period, int):
            raise TypeError("period must be an integer")
        if period < 1:
            raise ValueError("period must be positive")
        if not (0 < oversold < overbought < 100):
            raise ValueError(
                f"oversold ({oversold}) and overbought ({overbought}) must "
                f"satisfy 0 < oversold < overbought < 100"
            )
        self.period = period
        self.oversold = oversold
        self.overbought = overbought

    def required_bars(self) -> int:
        """Need period + 1 bars for RSI to produce its first value."""
        return self.period + 1

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        if "close" not in df.columns:
            raise ValueError("RSIReversion requires a 'close' column")

        with_rsi = add_rsi(df, self.period)
        rsi = with_rsi[f"rsi_{self.period}"]
        prev_rsi = rsi.shift(1)

        # Entry: RSI crosses below oversold threshold
        entries = (rsi < self.oversold) & (prev_rsi >= self.oversold)
        # Exit: RSI crosses above overbought threshold
        exits = (rsi > self.overbought) & (prev_rsi <= self.overbought)

        entries = entries.fillna(False).astype(bool)
        exits = exits.fillna(False).astype(bool)

        return SignalFrame(entries=entries, exits=exits)

    def __repr__(self) -> str:
        return (
            f"RSIReversion(period={self.period}, "
            f"oversold={self.oversold}, overbought={self.overbought})"
        )
