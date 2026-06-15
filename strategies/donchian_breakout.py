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
STOP_LIMIT (PLAN 11.47) — the broker arms a stop at the prior-N-day high,
the same level that produced the signal. The stop only triggers if price
trades through that level the next session, which prevents the
failed-breakout case (gap-down opens that filled hundreds of dollars
below signal on QCOM/ARM/MRVL/ASML 6/01–6/05). The attached limit caps
chase past the trigger, which prevents the gap-up case (QCOM 5/11 filled
+1205 bps above signal close on a MARKET). The limit cap reuses
PLAN 11.32's `ENTRY_PRICE_CAPS` policy but is anchored to the trigger,
not to the close.

Original SMACrossover-style MARKET is preserved for other strategies;
STOP_LIMIT is opted into by overriding `preferred_order_type`.

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
    preferred_order_type = OrderType.STOP_LIMIT

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

    def compute_entry_trigger(self, df: pd.DataFrame) -> float:
        """
        Return the broker-resting stop trigger for the latest bar.

        For a Donchian breakout this is the prior-N-day high — the level
        the next session's price must trade through to confirm the
        breakout. Re-computes the same indicator used in `_raw_signals`
        so the trigger is exactly the level that produced the signal.
        """
        if "close" not in df.columns:
            raise ValueError("DonchianBreakout requires a 'close' column")
        with_high = add_donchian_high(df, self.entry_window)
        trigger = float(with_high[f"donchian_high_{self.entry_window}"].iloc[-1])
        if not (trigger > 0):
            raise ValueError(
                f"donchian_high_{self.entry_window} is non-positive ({trigger!r}); "
                f"insufficient history for trigger computation"
            )
        return trigger

    def __repr__(self) -> str:
        return (
            f"DonchianBreakout(entry={self.entry_window}, exit={self.exit_window})"
        )
