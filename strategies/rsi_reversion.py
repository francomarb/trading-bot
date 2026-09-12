"""
RSI mean-reversion strategy.

Logic
-----
Given an RSI period `P`, an oversold threshold `oversold`, and an overbought
threshold `overbought`:

  entry[t] = True  iff  RSI(t) < oversold
  exit[t]  = True  iff  close(t) > SMA(exit_sma_window)(t)
                    OR  RSI(t) > quick_exit_rsi

In plain English: RSI Reversion buys a sharp short-term dip and exits on the
first practical bounce. Anywhere RSI or the configured exit SMA is NaN (early
bars) the corresponding signal is False.

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

from indicators.technicals import add_rsi, add_sma
from strategies.base import BaseStrategy, EdgeFilter, OrderType, SignalFrame


class RSIReversion(BaseStrategy):
    name = "rsi_reversion"
    preferred_order_type = OrderType.LIMIT
    candidate_feature_schema_version = 2

    def __init__(
        self,
        period: int = 14,
        oversold: float = 30.0,
        overbought: float = 70.0,
        *,
        entry_mode: str = "cross_below",
        exit_sma_window: int | None = None,
        quick_exit_rsi: float | None = None,
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
        if entry_mode not in {"cross_below", "level_below"}:
            raise ValueError("entry_mode must be 'cross_below' or 'level_below'")
        if exit_sma_window is not None:
            if not isinstance(exit_sma_window, int):
                raise TypeError("exit_sma_window must be an integer or None")
            if exit_sma_window < 1:
                raise ValueError("exit_sma_window must be positive")
        if quick_exit_rsi is not None and not (oversold < quick_exit_rsi < 100):
            raise ValueError(
                f"quick_exit_rsi ({quick_exit_rsi}) must be between oversold "
                f"({oversold}) and 100"
            )
        self.period = period
        self.oversold = oversold
        self.overbought = overbought
        self.entry_mode = entry_mode
        self.exit_sma_window = exit_sma_window
        self.quick_exit_rsi = quick_exit_rsi

    def required_bars(self) -> int:
        """Need period + 1 bars for RSI to produce its first value."""
        required = self.period + 1
        if self.exit_sma_window is not None:
            required = max(required, self.exit_sma_window)
        return required

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        if "close" not in df.columns:
            raise ValueError("RSIReversion requires a 'close' column")

        with_rsi = add_rsi(df, self.period)
        rsi = with_rsi[f"rsi_{self.period}"]
        prev_rsi = rsi.shift(1)

        if self.entry_mode == "cross_below":
            entries = (rsi < self.oversold) & (prev_rsi >= self.oversold)
        else:
            entries = rsi < self.oversold

        if self.exit_sma_window is None and self.quick_exit_rsi is None:
            exits = (rsi > self.overbought) & (prev_rsi <= self.overbought)
        else:
            exits = pd.Series(False, index=df.index, dtype=bool)
            if self.exit_sma_window is not None:
                with_sma = add_sma(with_rsi, self.exit_sma_window)
                sma = with_sma[f"sma_{self.exit_sma_window}"]
                exits |= df["close"].astype(float) > sma.astype(float)
            if self.quick_exit_rsi is not None:
                exits |= rsi > self.quick_exit_rsi

        entries = entries.fillna(False).astype(bool)
        exits = exits.fillna(False).astype(bool)

        return SignalFrame(entries=entries, exits=exits)

    def latest_observation(self, df: pd.DataFrame) -> dict[str, float | str | None]:
        """Return compact latest-bar diagnostics for candidate logging."""
        with_rsi = add_rsi(df, self.period)
        rsi = with_rsi[f"rsi_{self.period}"]
        out: dict[str, float | str | None] = {
            "entry_mode": self.entry_mode,
            "rsi": float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,
            "oversold": float(self.oversold),
            "quick_exit_rsi": (
                float(self.quick_exit_rsi) if self.quick_exit_rsi is not None else None
            ),
            "exit_sma_window": self.exit_sma_window,
        }
        if self.exit_sma_window is not None:
            with_sma = add_sma(with_rsi, self.exit_sma_window)
            sma = with_sma[f"sma_{self.exit_sma_window}"].iloc[-1]
            out["exit_sma"] = float(sma) if pd.notna(sma) else None
        return out

    def candidate_features(self, df: pd.DataFrame) -> dict[str, object]:
        """Describe the depth and recent shape of the oversold setup."""
        with_rsi = add_rsi(df, self.period)
        rsi = with_rsi[f"rsi_{self.period}"]
        close = df["close"].astype(float)
        current_rsi = float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None
        previous_rsi = (
            float(rsi.iloc[-2]) if len(rsi) > 1 and pd.notna(rsi.iloc[-2]) else None
        )
        features: dict[str, object] = {
            **self.latest_observation(df),
            "period": self.period,
            "previous_rsi": previous_rsi,
            "oversold_depth": (
                float(self.oversold) - current_rsi
                if current_rsi is not None
                else None
            ),
            "one_bar_return_pct": (
                float(close.iloc[-1] / close.iloc[-2] - 1.0)
                if len(close) > 1 and close.iloc[-2] > 0
                else None
            ),
            "three_bar_return_pct": (
                float(close.iloc[-1] / close.iloc[-4] - 1.0)
                if len(close) > 3 and close.iloc[-4] > 0
                else None
            ),
        }
        exit_sma = features.get("exit_sma")
        features["distance_to_exit_sma_pct"] = (
            float(close.iloc[-1]) / float(exit_sma) - 1.0
            if exit_sma is not None and float(exit_sma) > 0
            else None
        )
        return features

    def candidate_replay_contract(self) -> dict[str, object]:
        """Freeze the production entry, protection, and exit semantics.

        A configuration hash proves two candidates differed, but it cannot be
        reversed into the values needed by an offline replay.  Store those
        values with the candidate so a later run never borrows today's RSI or
        risk settings for an older decision.
        """
        from config import settings

        return {
            "contract_version": 1,
            "strategy": self.name,
            "timeframe": "1Day",
            "period": self.period,
            "oversold": self.oversold,
            "overbought": self.overbought,
            "entry_mode": self.entry_mode,
            "exit_sma_window": self.exit_sma_window,
            "quick_exit_rsi": self.quick_exit_rsi,
            "entry_order_type": self.preferred_order_type.value,
            "atr_stop_multiplier": settings.ATR_STOP_MULTIPLIER,
            "exit_order_type": "market",
            "modeled_exit_slippage_bps": settings.SLIPPAGE_MODEL_MARKET_BPS,
        }

    def __repr__(self) -> str:
        return (
            f"RSIReversion(period={self.period}, "
            f"oversold={self.oversold}, overbought={self.overbought}, "
            f"entry_mode={self.entry_mode!r}, "
            f"exit_sma_window={self.exit_sma_window}, "
            f"quick_exit_rsi={self.quick_exit_rsi})"
        )
