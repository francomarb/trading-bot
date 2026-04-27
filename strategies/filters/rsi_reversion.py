"""
RSI Reversion edge filter (Phase 10.F3b).

RSIEdgeFilter enforces four entry gates:

  Rule 1 — Market trend (mandatory):
    SPY close > SPY 200-day SMA   (avoid bear markets)
    SPY close > SPY 50-day SMA    (avoid macro downtrends)

  Rule 2 — Earnings blackout:
    Block new entries within 3 calendar days before / 2 days after earnings.
    RSI reversion buys dips — a dip into a binary earnings event is gap risk,
    not a mean-reversion setup. Two days after lets post-earnings follow-through
    (options unwinding, analyst notes, institutional rebalancing) settle before
    re-engaging.

  Rule 3 — Minimum liquidity:
    20-day average volume ≥ 500K shares.
    RSI reversion uses limit orders. Thinly traded stocks fill partially, exit
    wide, and the edge on paper evaporates in practice. This is a hard floor
    on liquidity, not a direction signal. Fails open when insufficient bars or
    no volume column.

  Rule 4 — No active breakdown:
    Current close > minimum close of the prior 20 bars.
    A stock making new 20-day lows is in active breakdown, not consolidating
    for reversion. Each lower low looks like an RSI oversold setup; each one
    is a knife-catch. This gate blocks entries during sustained individual
    stock downtrends that the SPY macro gates cannot see.
    Fails open on insufficient history (<21 bars).

  Rule 5 — Long-only mode:
    Already enforced by RSIReversion strategy (only BUY signals emitted).

All conditions must be True for a new entry. Exits are NEVER blocked —
that is enforced by BaseStrategy.

Design notes:
  - Stock 50-day SMA gate intentionally excluded: RSI oversold stocks are
    typically below their 50 SMA — filtering there removes exactly the trades
    the strategy is designed to take. The new-low gate (Rule 4) addresses the
    same concern more precisely: it blocks active breakdown without penalising
    normal pullbacks.
  - SPY gate cliff-edge (hard cutoff when SPY crosses 50 SMA) noted for
    Phase 11: a smoother N-bar confirmation window would reduce false lockouts
    on brief SPY dips. Deferred — hard gates are operationally auditable.

Observability (required by docs/RSI-edge-filter.md):
  - Every allow/block decision is logged with the specific reason(s).

Usage (forward_test.py when RSI is activated in 10.F4):
    from strategies.filters.rsi_reversion import RSIEdgeFilter
    edge = RSIEdgeFilter()
    strategy = RSIReversion(period=14, edge_filter=edge)
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from strategies.filters.common import EarningsBlackout, SPYTrendFilter


_VOL_MIN_WINDOW = 20       # days for average volume calculation
_NOTIONAL_MIN_AVG = 10_000_000  # minimum average daily dollar volume ($)
_NEW_LOW_WINDOW = 20       # bars to look back for breakdown detection


class RSIEdgeFilter:
    """
    Entry gate for RSI Reversion.

    Gates (all must pass for a new entry):
      1. SPY > 200 SMA AND SPY > 50 SMA
      2. Not within earnings blackout window
      3. 20-day average dollar volume ≥ notional_min_avg (liquidity floor)
      4. Current close > min close of prior new_low_window bars (no active breakdown)

    Args:
        spy_lookback_days: Calendar days of SPY history to fetch (default 280).
        spy_cache_ttl:     Seconds to reuse cached SPY data (default 600).
        days_before:       Earnings blackout days before the event (default 3).
        days_after:        Earnings blackout days after the event (default 2).
        vol_min_window:    Rolling window for average volume check (default 20).
        notional_min_avg:  Minimum average daily dollar volume (default 10_000_000).
        new_low_window:    Bars to look back for new-low breakdown detection (default 20).
    """

    def __init__(
        self,
        *,
        spy_lookback_days: int = 280,
        spy_cache_ttl: float = 600.0,
        days_before: int = 3,
        days_after: int = 2,
        vol_min_window: int = _VOL_MIN_WINDOW,
        notional_min_avg: int = _NOTIONAL_MIN_AVG,
        new_low_window: int = _NEW_LOW_WINDOW,
    ) -> None:
        self._spy_filter = SPYTrendFilter(
            sma_windows=[200, 50],
            lookback_days=spy_lookback_days,
            cache_ttl_seconds=spy_cache_ttl,
        )
        self._earnings = EarningsBlackout(
            days_before=days_before,
            days_after=days_after,
        )
        self._vol_min_window = vol_min_window
        self._notional_min_avg = notional_min_avg
        self._new_low_window = new_low_window
        self._symbol: str = ""

    def set_symbol(self, symbol: str) -> None:
        """Injected by BaseStrategy.generate_signals before __call__."""
        self._symbol = symbol
        self._earnings.set_symbol(symbol)

    def _volume_liquid(self, df: pd.DataFrame) -> pd.Series:
        """
        True where 20-day average dollar volume ≥ notional_min_avg.
        Fails open (True) when no volume column or insufficient history.
        """
        if "volume" not in df.columns or "close" not in df.columns:
            return pd.Series(True, index=df.index, dtype=bool)
        dollar_vol = df["close"].astype(float) * df["volume"].astype(float)
        avg = dollar_vol.rolling(self._vol_min_window).mean()
        liquid = avg >= self._notional_min_avg
        # NaN (insufficient bars) → fail open
        liquid = liquid.where(avg.notna(), other=True)
        return liquid.astype(bool)

    def _no_new_low(self, df: pd.DataFrame) -> pd.Series:
        """
        True where the current close is above the minimum of the prior N bars.
        Blocks entries when the stock is making new N-day lows (active breakdown).
        Fails open (True) when insufficient history.
        """
        close = df["close"]
        # shift(1) excludes today so prior_min = min of the N bars before today
        prior_min = close.shift(1).rolling(self._new_low_window).min()
        not_new_low = close > prior_min
        # Fail open on NaN (insufficient history)
        not_new_low = not_new_low.where(prior_min.notna(), other=True)
        return not_new_low.astype(bool)

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        spy_gate      = self._spy_filter(df)
        earnings_gate = self._earnings(df)
        vol_gate      = self._volume_liquid(df)
        low_gate      = self._no_new_low(df)

        combined = spy_gate & earnings_gate & vol_gate & low_gate

        # Detailed observability log on the last bar.
        if not df.empty:
            allowed   = bool(combined.iloc[-1])
            spy_ok    = bool(spy_gate.iloc[-1])
            earn_ok   = bool(earnings_gate.iloc[-1])
            vol_ok    = bool(vol_gate.iloc[-1])
            low_ok    = bool(low_gate.iloc[-1])

            if allowed:
                logger.info(
                    f"RSI_FILTER_ALLOWED {self._symbol} — "
                    f"SPY={spy_ok} earnings={earn_ok} "
                    f"liquid={vol_ok} no_new_low={low_ok}"
                )
            else:
                reasons = []
                if not spy_ok:
                    reasons.append("SPY trend gate failed (below 200 or 50 SMA)")
                if not earn_ok:
                    reasons.append("earnings blackout")
                if not vol_ok:
                    if "volume" in df.columns and "close" in df.columns:
                        dollar_vol = df["close"].astype(float) * df["volume"].astype(float)
                        avg_vol = dollar_vol.rolling(self._vol_min_window).mean().iloc[-1]
                    else:
                        avg_vol = float("nan")
                    avg_str = f"${avg_vol:,.0f}" if pd.notna(avg_vol) else "NaN"
                    reasons.append(
                        f"liquidity too low (avg_dollar_vol{self._vol_min_window}={avg_str} "
                        f"< ${self._notional_min_avg:,})"
                    )
                if not low_ok:
                    reasons.append(
                        f"new {self._new_low_window}-day low (active breakdown)"
                    )
                logger.info(
                    f"RSI_FILTER_BLOCKED {self._symbol} — "
                    + ", ".join(reasons)
                )

        return combined
