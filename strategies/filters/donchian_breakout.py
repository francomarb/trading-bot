"""
Donchian Breakout edge filter.

DonchianEdgeFilter enforces three entry gates on top of the strategy's
new-N-day-high signal:

  Rule 1 — Stock structural strength:
    Stock close > stock 200-day SMA.
    A breakout in a stock that's still below its own 200 SMA is typically
    a counter-trend rally in a structurally weak name, not a continuation
    of a real uptrend. Requiring stock > 200 SMA aligns the breakout with
    the long-term trend. Fails open when history is insufficient (<200 bars).

  Rule 2 — Earnings blackout (short window):
    Block new entries 1 calendar day before earnings (gap-down risk on
    the underlying). Allow immediate re-entry post-earnings — Donchian
    naturally captures positive post-earnings continuation when the
    gap-up pushes price to a new N-day high. Shorter window than RSI's
    3/2 because the strategy WANTS to participate in earnings-driven
    breakouts on the up-side.

  Rule 3 — Minimum liquidity:
    20-day average dollar volume ≥ notional_min_avg.
    Base threshold ($20M) is expressed in *consolidated tape* terms.
    The volume data is dynamically scaled to "Synthetic SIP" terms in the data fetcher.
    Fails open when volume column is missing or insufficient history.

  Rule 4 — Long-only mode:
    Already enforced by DonchianBreakout (only long entries emitted).

The SPY > 200 SMA macro gate is owned by RegimeDetector at the engine
level (BEAR regime blocks all new long entries). This filter does not
duplicate that.

All conditions must be True for a new entry. Exits are NEVER blocked —
that is enforced by BaseStrategy.

Observability:
  - Every allow/block decision on the last bar is logged with the specific
    reason(s) and the displayed liquidity threshold.

Usage:
    from strategies.filters.donchian_breakout import DonchianEdgeFilter
    edge = DonchianEdgeFilter()
    strategy = DonchianBreakout(edge_filter=edge)
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from config.settings import ALPACA_DATA_FEED
from strategies.filters.common import EarningsBlackout


_STOCK_SMA_WINDOW = 200
_VOL_MIN_WINDOW = 20
_NOTIONAL_MIN_AVG = 20_000_000  # $20M consolidated-tape average daily dollar volume

_EARNINGS_DAYS_BEFORE = 1   # block 1 day before (avoid pre-earnings entry)
_EARNINGS_DAYS_AFTER = 0    # allow immediately after (capture post-earnings continuation)


class DonchianEdgeFilter:
    """
    Entry gate for DonchianBreakout.

    Args:
        stock_sma_window:    SMA period for the stock's own trend filter (default 200).
        vol_min_window:      Rolling window for average volume check (default 20).
        notional_min_avg:    Base $ threshold expressed in consolidated tape
                             terms (default $20M).
        days_before:         Earnings blackout days before announcement (default 1).
        days_after:          Earnings blackout days after announcement (default 0).
    """

    def __init__(
        self,
        *,
        stock_sma_window: int = _STOCK_SMA_WINDOW,
        vol_min_window: int = _VOL_MIN_WINDOW,
        notional_min_avg: int = _NOTIONAL_MIN_AVG,
        days_before: int = _EARNINGS_DAYS_BEFORE,
        days_after: int = _EARNINGS_DAYS_AFTER,
    ) -> None:
        self._stock_sma_window = stock_sma_window
        self._vol_min_window = vol_min_window

        self._notional_min_avg = int(notional_min_avg)

        self._earnings = EarningsBlackout(
            days_before=days_before,
            days_after=days_after,
        )

        self._symbol: str = ""
        self._last_reasons: list[str] = []

    def set_symbol(self, symbol: str) -> None:
        """Injected by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol
        self._earnings.set_symbol(symbol)

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

    def _liquidity_ok(self, df: pd.DataFrame) -> pd.Series:
        """
        True where 20-day avg dollar volume ≥ self._notional_min_avg.
        Fails open (True) when volume column is missing or insufficient history.
        """
        if "volume" not in df.columns or "close" not in df.columns:
            return pd.Series(True, index=df.index, dtype=bool)
        dollar_vol = df["close"].astype(float) * df["volume"].astype(float)
        avg = dollar_vol.rolling(self._vol_min_window).mean()
        liquid = avg >= self._notional_min_avg
        liquid = liquid.where(avg.notna(), other=True)
        return liquid.astype(bool)

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        stock_gate     = self._stock_above_sma(df)
        earnings_gate  = self._earnings(df)
        liquidity_gate = self._liquidity_ok(df)

        combined = stock_gate & earnings_gate & liquidity_gate

        if not df.empty:
            allowed   = bool(combined.iloc[-1])
            stock_ok  = bool(stock_gate.iloc[-1])
            earn_ok   = bool(earnings_gate.iloc[-1])
            liq_ok    = bool(liquidity_gate.iloc[-1])

            feed_label = ALPACA_DATA_FEED
            threshold_str = f"${self._notional_min_avg:,}"

            if allowed:
                self._last_reasons = []
                logger.info(
                    f"DONCHIAN_FILTER_ALLOWED {self._symbol} — "
                    f"stock>200SMA={stock_ok} earnings={earn_ok} "
                    f"liquidity={liq_ok} "
                    f"(feed={feed_label}, liq_threshold={threshold_str})"
                )
            else:
                reasons = []
                if not stock_ok:
                    close_val = df["close"].iloc[-1]
                    sma_val = df["close"].rolling(self._stock_sma_window).mean().iloc[-1]
                    sma_str = f"{sma_val:.2f}" if pd.notna(sma_val) else "NaN"
                    reasons.append(
                        f"stock {close_val:.2f} ≤ SMA{self._stock_sma_window} {sma_str}"
                    )
                if not earn_ok:
                    reasons.append("earnings blackout (1 day before earnings)")
                if not liq_ok:
                    if "volume" in df.columns and "close" in df.columns:
                        dollar_vol = df["close"].astype(float) * df["volume"].astype(float)
                        avg_vol = dollar_vol.rolling(self._vol_min_window).mean().iloc[-1]
                    else:
                        avg_vol = float("nan")
                    avg_str = f"${avg_vol:,.0f}" if pd.notna(avg_vol) else "NaN"
                    reasons.append(
                        f"liquidity too low (avg_dollar_vol{self._vol_min_window}={avg_str} "
                        f"< {threshold_str}, feed={feed_label})"
                    )
                self._last_reasons = reasons
                logger.info(
                    f"DONCHIAN_FILTER_BLOCKED {self._symbol} — "
                    + ", ".join(reasons)
                )
        else:
            self._last_reasons = []

        return combined

    def get_last_block_reasons(self) -> list[str]:
        return list(self._last_reasons)
