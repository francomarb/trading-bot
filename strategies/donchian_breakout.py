"""
Donchian Channel Breakout — long-only trend continuation strategy.

Logic
-----
Classic Turtle Trading System 1 (Dennis & Eckhardt, 1983):

  ENTRY[t] = close[t] > rolling_max(close[t-entry_window:t])
              i.e. today's close exceeds the maximum close of the prior N days.

  EXIT[t]  = close[t] < rolling_min(close[t-exit_window:t])
              i.e. today's close drops below the minimum close of the prior M days.

The strategy emits signals only. Position sizing, ATR-based stop placement
(`ATR_STOP_MULTIPLIER`), regime gating (`StrategySlot.allowed_regimes`),
and exposure caps live elsewhere in the engine. Long-only by design (matches
the user's directional thesis on AI/Semi/Big-Tech).

Look-ahead safety
-----------------
The Donchian indicators (`add_donchian_high`, `add_donchian_low`) shift by 1
bar before the rolling window — today's close is NOT in its own comparison
window. A new high at bar t means today's close exceeded the prior N-bar
maximum, not the maximum that includes today.

Order type
----------
MARKET — breakouts are price-sensitive. Missing the fill is worse than
paying a few bps slippage. Same as SMACrossover.

Why this strategy fits trending mega-caps
-----------------------------------------
Stocks that go up "forever" (NVDA, AVGO, AMD, MSFT during AI mania) make
new highs constantly. Donchian buys *every* new high as a continuation
signal. Compare to BB Squeeze (parked) which waited for volatility
compression that never arrived on these names — Donchian has no such
prerequisite. As long as the stock keeps making higher highs, the strategy
stays in; the M-day low exit only fires when the trend genuinely fails.

Re-entry behaviour
------------------
Pyramiding is not supported in V1 (engine doesn't allow multiple open
positions per symbol per slot). However, after a stop-out or signal-exit,
the strategy will re-enter when the stock makes a new entry-window high
again — natural pyramiding-lite that captures most of the same benefit.
"""

from __future__ import annotations

import pandas as pd

from indicators.technicals import add_donchian_high, add_donchian_low
from strategies.base import BaseStrategy, EdgeFilter, OrderType, SignalFrame


class DonchianBreakout(BaseStrategy):
    name = "donchian_breakout"
    preferred_order_type = OrderType.MARKET

    def __init__(
        self,
        *,
        entry_window: int = 20,
        exit_window: int = 10,
        edge_filter: EdgeFilter | None = None,
    ) -> None:
        super().__init__(edge_filter=edge_filter)

        for name, value in (("entry_window", entry_window), ("exit_window", exit_window)):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive int, got {value!r}")

        if entry_window <= exit_window:
            raise ValueError(
                f"entry_window ({entry_window}) must be strictly greater than "
                f"exit_window ({exit_window}); a breakout entry on a tighter "
                f"window than the exit would whipsaw on every fill"
            )

        self.entry_window = entry_window
        self.exit_window = exit_window

    def required_bars(self) -> int:
        # Need entry_window + 1 bars to compute the first signal (the prior-window
        # max requires entry_window bars before today). Add a 5-bar safety buffer
        # to avoid edge cases on the first valid bar.
        return self.entry_window + 5

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        if "close" not in df.columns:
            raise ValueError("DonchianBreakout requires a 'close' column")

        with_high = add_donchian_high(df, self.entry_window)
        with_low  = add_donchian_low(with_high, self.exit_window)

        donchian_high = with_low[f"donchian_high_{self.entry_window}"]
        donchian_low  = with_low[f"donchian_low_{self.exit_window}"]
        close = df["close"]

        # Entry: close above the prior N-bar high (a "new" N-day high).
        entries = (close > donchian_high).fillna(False).astype(bool)

        # Exit: close below the prior M-bar low (trend genuinely failed).
        exits = (close < donchian_low).fillna(False).astype(bool)

        return SignalFrame(entries=entries, exits=exits)

    def __repr__(self) -> str:
        return (
            f"DonchianBreakout(entry={self.entry_window}, exit={self.exit_window})"
        )
