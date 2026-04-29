"""
Bollinger Squeeze edge filter.

BollingerSqueezeEdgeFilter enforces three entry gates on top of the squeeze
strategy's price/volatility signal:

  Rule 1 — Minimum liquidity (IEX-scaled):
    20-day average dollar volume ≥ notional_min_avg.
    The base threshold ($20M) is expressed in *consolidated tape* terms.
    When ALPACA_DATA_FEED == "iex" we see only ~5% of consolidated volume,
    so the threshold is multiplied by 0.05 (effective $1M on IEX). This is
    the SINGLE point of feed-conditionality in the squeeze stack — flipping
    to SIP later is a one-env-var change.
    Mirrors strategies/filters/rsi_reversion.py:113-117.
    Fails open when volume column is missing or there is insufficient history.

  Rule 2 — Earnings blackout:
    Block new entries within `days_before` calendar days before earnings
    and `days_after` days after. Defaults are 2/1 — shorter than RSI's 3/2
    because momentum-breakouts often follow earnings *positively* and we
    don't want to lock out the strongest setups. Fails open via
    EarningsBlackout.

  Rule 3 — No same-bar exhaustion:
    Block when close > BB upper + exhaustion_atr_mult × ATR. A close that
    far above the upper band is already extended; chasing those breakouts
    is statistically the worst time to enter. Fails open on insufficient
    history (NaN ATR or BB).

  Rule 4 — Long-only mode:
    Already enforced by BollingerSqueeze (only long entries emitted).

The SPY/macro gate is owned by RegimeDetector at the engine level (BEAR
regime blocks all new long entries). This filter does not duplicate that.

All conditions must be True for a new entry. Exits are NEVER blocked —
that is enforced by BaseStrategy.

Observability:
  - Every allow/block decision on the last bar is logged with the specific
    reason(s) and the displayed liquidity threshold (so the IEX scaling
    can be verified at runtime).

Usage:
    from strategies.filters.bollinger_squeeze import BollingerSqueezeEdgeFilter
    edge = BollingerSqueezeEdgeFilter()
    strategy = BollingerSqueeze(edge_filter=edge)
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from config.settings import ALPACA_DATA_FEED
from indicators.technicals import add_atr, add_bollinger_bands
from strategies.filters.common import EarningsBlackout


_VOL_MIN_WINDOW = 20
_NOTIONAL_MIN_AVG = 20_000_000  # $20M consolidated-tape average daily dollar volume
_IEX_VOLUME_FRACTION = 0.05     # IEX prints ~5% of consolidated volume

_EARNINGS_DAYS_BEFORE = 2
_EARNINGS_DAYS_AFTER = 1

_BB_LENGTH = 20
_BB_STD = 2.0
_ATR_LENGTH = 20
_EXHAUSTION_ATR_MULT = 1.5


class BollingerSqueezeEdgeFilter:
    """
    Entry gate for BollingerSqueeze.

    Args:
        vol_min_window:        Rolling window for average dollar volume (default 20).
        notional_min_avg:      Base $ threshold expressed in consolidated tape
                               terms (default $20M). When running on IEX,
                               this value is scaled by 0.05.
        days_before:           Earnings blackout days before announcement (default 2).
        days_after:            Earnings blackout days after announcement (default 1).
        bb_length:             Bollinger Bands length used for the exhaustion check
                               (default 20). Match the strategy's bb_length to
                               keep the gate consistent with the signal.
        bb_std:                Bollinger Bands std-dev (default 2.0).
        atr_length:            ATR length for the exhaustion gate (default 20).
        exhaustion_atr_mult:   Block entries when close > BB upper + this × ATR
                               (default 1.5). A close that far above the upper
                               band indicates the breakout is already extended.
    """

    def __init__(
        self,
        *,
        vol_min_window: int = _VOL_MIN_WINDOW,
        notional_min_avg: int = _NOTIONAL_MIN_AVG,
        days_before: int = _EARNINGS_DAYS_BEFORE,
        days_after: int = _EARNINGS_DAYS_AFTER,
        bb_length: int = _BB_LENGTH,
        bb_std: float = _BB_STD,
        atr_length: int = _ATR_LENGTH,
        exhaustion_atr_mult: float = _EXHAUSTION_ATR_MULT,
    ) -> None:
        self._vol_min_window = vol_min_window

        # IEX sees ~5% of consolidated market volume. Scale ONLY when on IEX.
        # Any other feed (sip, future paid feeds) leaves the threshold unscaled,
        # so a SIP transition is a single env-var flip.
        if ALPACA_DATA_FEED == "iex":
            self._notional_min_avg = int(notional_min_avg * _IEX_VOLUME_FRACTION)
        else:
            self._notional_min_avg = int(notional_min_avg)

        self._earnings = EarningsBlackout(
            days_before=days_before,
            days_after=days_after,
        )

        self._bb_length = bb_length
        self._bb_std = float(bb_std)
        self._atr_length = atr_length
        self._exhaustion_atr_mult = float(exhaustion_atr_mult)

        self._symbol: str = ""

    def set_symbol(self, symbol: str) -> None:
        """Injected by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol
        self._earnings.set_symbol(symbol)

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

    def _not_exhausted(self, df: pd.DataFrame) -> pd.Series:
        """
        True where close ≤ BB upper + exhaustion_atr_mult × ATR.
        Blocks entries on extended/parabolic bars where a breakout chase
        is statistically the worst time to enter.
        Fails open (True) when ATR or BB upper are NaN (insufficient history).
        """
        with_bb = add_bollinger_bands(df, self._bb_length, self._bb_std)
        with_atr = add_atr(with_bb, self._atr_length)

        bb_upper = with_atr[f"bb_upper_{self._bb_length}_{self._bb_std:g}"]
        atr      = with_atr[f"atr_{self._atr_length}"]
        close    = df["close"]

        threshold = bb_upper + self._exhaustion_atr_mult * atr
        not_exhausted = close <= threshold
        # Fail open on missing indicator values.
        not_exhausted = not_exhausted.where(threshold.notna(), other=True)
        return not_exhausted.astype(bool)

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        liquidity_gate  = self._liquidity_ok(df)
        earnings_gate   = self._earnings(df)
        exhaustion_gate = self._not_exhausted(df)

        combined = liquidity_gate & earnings_gate & exhaustion_gate

        if not df.empty:
            allowed     = bool(combined.iloc[-1])
            liq_ok      = bool(liquidity_gate.iloc[-1])
            earn_ok     = bool(earnings_gate.iloc[-1])
            exhaust_ok  = bool(exhaustion_gate.iloc[-1])

            feed_label = ALPACA_DATA_FEED
            threshold_str = f"${self._notional_min_avg:,}"

            if allowed:
                logger.info(
                    f"SQUEEZE_FILTER_ALLOWED {self._symbol} — "
                    f"liquidity={liq_ok} earnings={earn_ok} "
                    f"not_exhausted={exhaust_ok} "
                    f"(feed={feed_label}, liq_threshold={threshold_str})"
                )
            else:
                reasons = []
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
                if not earn_ok:
                    reasons.append("earnings blackout")
                if not exhaust_ok:
                    reasons.append(
                        f"exhausted (close > BB_upper + {self._exhaustion_atr_mult:g}×ATR)"
                    )
                logger.info(
                    f"SQUEEZE_FILTER_BLOCKED {self._symbol} — "
                    + ", ".join(reasons)
                )

        return combined
