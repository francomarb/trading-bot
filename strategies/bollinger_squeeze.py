"""
Bollinger Band Squeeze — TTM-style volatility-breakout strategy (long-only).

Logic
-----
The squeeze identifies *volatility compression* (Bollinger Bands narrowing
inside Keltner Channels) followed by *expansion* (BB exits the KC). The
expansion is the breakout signal; we then confirm direction with a price-based
filter (no volume reliance — we run on IEX which sees ~5% of consolidated tape).

Given default params bb_length=20, bb_std=2.0, kc_length=20, kc_atr_mult=1.5,
min_squeeze_bars=6, roc_lookback=5:

  squeeze_on[t]   = (bb_upper[t] < kc_upper[t]) AND (bb_lower[t] > kc_lower[t])
  squeeze_fire[t] = squeeze_on[t-1] AND NOT squeeze_on[t]
  duration_met[t] = sum of squeeze_on over (t-min_squeeze_bars .. t-1) == min_squeeze_bars
  direction_up[t] = (close[t] > close[t-roc_lookback]) AND (close[t] > high[t-1])

  entry[t]        = squeeze_fire[t] AND duration_met[t] AND direction_up[t]
  exit[t]         = close[t] < bb_mid[t]

Long-only. The strategy never emits a short entry — the user's directional
thesis is a sustained AI/Semi/Big-Tech bull move; symmetry would invert the
edge in this universe.

Look-ahead safety
-----------------
Every condition uses `shift` or rolling windows over past bars only. The
engine/backtester is responsible for shifting execution to bar t+1's open.

Order type
----------
MARKET — a squeeze fire means the breakout is underway and missing the fill is
worse than paying a few bps of spread (mirrors SMACrossover).

Stops & sizing
--------------
The strategy emits signals only. Position sizing, ATR-based stop placement
(`ATR_STOP_MULTIPLIER`), and exposure caps live in RiskManager. No state.
"""

from __future__ import annotations

import pandas as pd

from indicators.technicals import add_bollinger_bands, add_keltner_channels
from strategies.base import BaseStrategy, EdgeFilter, OrderType, SignalFrame


class BollingerSqueeze(BaseStrategy):
    name = "bollinger_squeeze"
    preferred_order_type = OrderType.MARKET

    def __init__(
        self,
        *,
        bb_length: int = 20,
        bb_std: float = 2.0,
        kc_length: int = 20,
        kc_atr_mult: float = 1.5,
        min_squeeze_bars: int = 6,
        roc_lookback: int = 5,
        edge_filter: EdgeFilter | None = None,
    ) -> None:
        super().__init__(edge_filter=edge_filter)

        # Validate ints
        for name, value in (
            ("bb_length", bb_length),
            ("kc_length", kc_length),
            ("min_squeeze_bars", min_squeeze_bars),
            ("roc_lookback", roc_lookback),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"{name} must be a positive int, got {value!r}")

        # Validate floats
        for name, value in (("bb_std", bb_std), ("kc_atr_mult", kc_atr_mult)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number, got {value!r}")

        self.bb_length = bb_length
        self.bb_std = float(bb_std)
        self.kc_length = kc_length
        self.kc_atr_mult = float(kc_atr_mult)
        self.min_squeeze_bars = min_squeeze_bars
        self.roc_lookback = roc_lookback

    def required_bars(self) -> int:
        # Need indicators warmed up + duration window + ROC window + safety buffer.
        return (
            max(self.bb_length, self.kc_length)
            + self.min_squeeze_bars
            + self.roc_lookback
            + 5
        )

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        for col in ("high", "low", "close"):
            if col not in df.columns:
                raise ValueError(
                    f"BollingerSqueeze requires '{col}' column on input DataFrame"
                )

        with_bb = add_bollinger_bands(df, self.bb_length, self.bb_std)
        with_kc = add_keltner_channels(with_bb, self.kc_length, self.kc_atr_mult)

        bb_upper = with_kc[f"bb_upper_{self.bb_length}_{self.bb_std:g}"]
        bb_lower = with_kc[f"bb_lower_{self.bb_length}_{self.bb_std:g}"]
        bb_mid   = with_kc[f"bb_mid_{self.bb_length}"]
        kc_upper = with_kc[f"kc_upper_{self.kc_length}_{self.kc_atr_mult:g}"]
        kc_lower = with_kc[f"kc_lower_{self.kc_length}_{self.kc_atr_mult:g}"]

        close = df["close"]
        high  = df["high"]

        # Squeeze ON when BB is fully inside KC.
        squeeze_on = (bb_upper < kc_upper) & (bb_lower > kc_lower)
        squeeze_on = squeeze_on.where(
            bb_upper.notna() & kc_upper.notna(), other=False
        ).astype(bool)

        # Squeeze FIRES on the bar that exits the squeeze (was on yesterday, off today).
        squeeze_fire = squeeze_on.shift(1, fill_value=False) & ~squeeze_on

        # Minimum-duration gate: the prior `min_squeeze_bars` bars must all be in squeeze.
        # Using shift(1) so we count the bars BEFORE the fire bar, exclusive of today.
        prior_window_sum = (
            squeeze_on.shift(1, fill_value=False)
            .astype(int)
            .rolling(self.min_squeeze_bars, min_periods=self.min_squeeze_bars)
            .sum()
        )
        duration_met = (prior_window_sum == self.min_squeeze_bars).fillna(False)

        # Direction confirmation — price-based, no volume reliance (IEX-safe).
        roc = close - close.shift(self.roc_lookback)
        direction_up = (roc > 0) & (close > high.shift(1))
        direction_up = direction_up.fillna(False).astype(bool)

        entries = (squeeze_fire & duration_met & direction_up).fillna(False).astype(bool)

        # Exit on a close back below the BB midline (loss of momentum).
        exits = (close < bb_mid).fillna(False).astype(bool)

        return SignalFrame(entries=entries, exits=exits)

    def __repr__(self) -> str:
        return (
            f"BollingerSqueeze(bb={self.bb_length}/{self.bb_std:g}, "
            f"kc={self.kc_length}/{self.kc_atr_mult:g}, "
            f"min_squeeze={self.min_squeeze_bars}, roc={self.roc_lookback})"
        )
