"""
Market Regime Detector (Phase 10.F2).

Classifies the current market environment into one of four discrete regimes
once per engine cycle. The engine gates new strategy entries on the result;
exits are never blocked regardless of regime.

Ownership model
---------------
This module owns macro-level SPY rules that apply universally across all
long-only strategies:
  - BEAR      : SPY < 200-day SMA — no new longs, period.
  - VOLATILE  : ATR% in the top 80th percentile of recent history — extreme
                volatility degrades every strategy's edge and inflates slippage.

Strategy-specific SPY rules stay in their edge filters:
  - RSIEdgeFilter keeps SPY > 50 SMA (mean-reversion degrades in corrections).
  - SMAEdgeFilter's SPY > 200 SMA check is DISABLED (this module owns it now).

Regimes
-------
  BEAR      SPY close < SPY 200-day SMA.
            No new long entries for any strategy. Hard stop.

  VOLATILE  Current ATR% (ATR14 / close) BOTH ranks above the
            `vol_percentile_threshold` (default 80th) of its trailing
            `vol_percentile_window` (default 126) bar history AND exceeds an
            absolute floor `vol_atr_pct_floor` (default 0.012 = 1.2%). The
            absolute floor is required so the gate cannot fire on objectively
            calm days that merely look "elevated" relative to an unusually quiet
            rolling window — without it, mean ATR% on flagged days in 2017 was
            0.68% (a textbook calm market). See scripts/regime_volatile_audit.py
            for the 12-year calibration; 0.012 was the out-of-sample winner on
            the crisis_catch − calm_false trade-off across 2014–2026.

  TRENDING  ADX(14) on SPY >= `adx_trend_threshold` (default 25).
            Trend-following (SMA crossover) has strong edge; mean-reversion
            less so (RSI edge filters provide the symbol-level guard).

  RANGING   ADX(14) on SPY <= `adx_range_threshold` (default 20).
            Mean-reversion (RSI) has stronger edge; SMA crossover more prone
            to whipsaws. Both strategies are still allowed — edge filters handle
            the rest.

  When ADX is between the two thresholds (20–25), the 50-day SMA slope
  disambiguates: positive slope → TRENDING, flat/negative → RANGING.

Fail-safe
---------
If SPY data is unavailable (fetch error), the detector returns the last cached
regime if one exists, or RANGING (the most conservative non-blocking default)
with a WARNING log. It never silently allows entries in BEAR or VOLATILE based
on stale data — if the cache is fresh enough it is used; otherwise RANGING.

VIX integration
---------------
Considered and parked. ATR% on SPY and VIX percentile are highly correlated
(daily-level ~0.85, percentile-level higher in stress); both rolling-percentile
classifiers share the same renormalisation defect, so swapping data sources
does not fix the underlying problem. The 2026-05 audit
(scripts/regime_volatile_audit.py) showed the real fix was an absolute ATR%
floor on top of the existing percentile rank. VIX remains a candidate for
*different* use cases — e.g. term-structure inversion as a binary stress
signal, or option-strategy entry confirmation — and should be reconsidered
when one of those needs arises, not as a regime-detector replacement.

Usage (forward_test.py)
-----------------------
    from regime.detector import MarketRegime, RegimeDetector

    regime = RegimeDetector()
    engine = TradingEngine(..., regime_detector=regime)

    # StrategySlot declares which regimes allow new entries:
    slot = StrategySlot(
        strategy=SMACrossover(...),
        watchlist_source=...,
        allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
    )
"""

from __future__ import annotations

import time
from enum import Enum

import pandas as pd
from loguru import logger

from indicators.technicals import add_adx, add_atr, add_sma


# ── Regime enum ───────────────────────────────────────────────────────────────


class MarketRegime(Enum):
    TRENDING = "trending"   # ADX ≥ 25, SPY above MAs — trend-following favoured
    RANGING  = "ranging"    # ADX ≤ 20 or ambiguous — mean-reversion favoured
    VOLATILE = "volatile"   # ATR% > 80th percentile — all new entries blocked
    BEAR     = "bear"       # SPY < 200 SMA — all new longs blocked


# ── Detector ──────────────────────────────────────────────────────────────────


class RegimeDetector:
    """
    Classifies market regime from SPY OHLCV bars fetched internally.

    Designed to be called once per engine cycle. Results are cached for
    `cache_ttl_seconds` so multiple callers within one cycle pay only one
    fetch.

    Args:
        lookback_days:           Calendar days of SPY history to fetch (default 300).
                                 300 cd ≈ 206 trading days, enough for SMA200.
        cache_ttl_seconds:       Seconds to reuse the cached regime (default 600).
        sma_long_window:         SMA window for the BEAR gate (default 200).
        sma_short_window:        SMA window used for slope disambiguation (default 50).
        sma_slope_bars:          Bars over which the short SMA slope is measured (default 5).
        atr_window:              ATR window for volatility calculation (default 14).
        vol_percentile_window:   Bars to use for ATR% percentile ranking (default 126 ≈ 6 mo).
        vol_percentile_threshold: ATR% percentile above which regime = VOLATILE (default 0.80).
        vol_atr_pct_floor:       Absolute ATR% floor required IN ADDITION to the percentile
                                 rank for VOLATILE to fire (default 0.012 = 1.2%). Anchors
                                 the gate to absolute volatility so it does not over-fire in
                                 unusually calm rolling-window samples.
        adx_window:              ADX window (default 14).
        adx_trend_threshold:     ADX >= this → TRENDING (default 25).
        adx_range_threshold:     ADX <= this → RANGING  (default 20).
    """

    def __init__(
        self,
        *,
        lookback_days: int = 300,
        cache_ttl_seconds: float = 600.0,
        sma_long_window: int = 200,
        sma_short_window: int = 50,
        sma_slope_bars: int = 5,
        atr_window: int = 14,
        vol_percentile_window: int = 126,
        vol_percentile_threshold: float = 0.80,
        vol_atr_pct_floor: float = 0.012,
        adx_window: int = 14,
        adx_trend_threshold: float = 25.0,
        adx_range_threshold: float = 20.0,
    ) -> None:
        self._lookback_days          = lookback_days
        self._cache_ttl              = cache_ttl_seconds
        self._sma_long               = sma_long_window
        self._sma_short              = sma_short_window
        self._sma_slope_bars         = sma_slope_bars
        self._atr_window             = atr_window
        self._vol_pct_window         = vol_percentile_window
        self._vol_pct_threshold      = vol_percentile_threshold
        self._vol_atr_pct_floor      = vol_atr_pct_floor
        self._adx_window             = adx_window
        self._adx_trend              = adx_trend_threshold
        self._adx_range              = adx_range_threshold

        self._spy_cache: pd.DataFrame | None = None
        self._spy_cache_time: float = 0.0
        self._last_regime: MarketRegime | None = None
        self._last_regime_time: float = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def detect(self) -> MarketRegime:
        """
        Return the current MarketRegime. Result is cached for `cache_ttl_seconds`.
        Logs regime + all contributing signals at INFO level on each fresh computation.
        """
        now = time.monotonic()
        if (
            self._last_regime is not None
            and (now - self._last_regime_time) < self._cache_ttl
        ):
            return self._last_regime

        spy = self._fetch_spy()
        if spy is None or spy.empty:
            fallback = self._last_regime or MarketRegime.RANGING
            logger.warning(
                f"RegimeDetector: no SPY data — returning {fallback.value!r} "
                "(last cached or conservative default)"
            )
            return fallback

        regime = self._classify(spy)
        self._last_regime      = regime
        self._last_regime_time = now
        return regime

    # ── SPY fetch ─────────────────────────────────────────────────────────────

    def _fetch_spy(self) -> pd.DataFrame | None:
        """Fetch SPY bars with TTL cache. Advances cache_time on failure to rate-limit retries."""
        now = time.monotonic()
        if (
            self._spy_cache is not None
            and (now - self._spy_cache_time) < self._cache_ttl
        ):
            return self._spy_cache

        try:
            from config.settings import ALPACA_DATA_FEED
            from data.fetcher import fetch_symbol
            import datetime
            end = datetime.datetime.now(datetime.timezone.utc)
            start = end - datetime.timedelta(days=self._lookback_days)
            # Live engine path — RegimeDetector runs every cycle, fetches
            # what the bot actually trades against.
            df, _stats = fetch_symbol(
                "SPY", start, end, timeframe="1Day", feed=ALPACA_DATA_FEED
            )
            self._spy_cache      = df
            self._spy_cache_time = now
            logger.debug(f"RegimeDetector: fetched {len(df)} SPY bars")
            return df
        except Exception as exc:
            logger.warning(
                f"RegimeDetector: SPY fetch failed — {exc}. "
                "Will retry after TTL."
            )
            self._spy_cache_time = now  # rate-limit retries
            return self._spy_cache      # stale or None

    # ── Classification ────────────────────────────────────────────────────────

    def _classify(self, spy: pd.DataFrame) -> MarketRegime:
        """
        Run all signals and return a MarketRegime. Priority order:
          1. BEAR     — SPY below long SMA
          2. VOLATILE — ATR% above percentile threshold
          3. TRENDING — ADX above trend threshold
          4. RANGING  — ADX below range threshold
          5. Ambiguous ADX zone — 50 SMA slope decides
        """
        close = spy["close"]

        # ── 1. BEAR gate ──────────────────────────────────────────────────────
        spy_with_sma = add_sma(spy, self._sma_long)
        sma_long_val = spy_with_sma[f"sma_{self._sma_long}"].iloc[-1]
        last_close   = float(close.iloc[-1])

        if pd.notna(sma_long_val) and last_close < float(sma_long_val):
            logger.info(
                f"REGIME=BEAR — SPY {last_close:.2f} < SMA{self._sma_long} {sma_long_val:.2f}"
            )
            return MarketRegime.BEAR

        # ── 2. VOLATILE gate ─────────────────────────────────────────────────
        spy_with_atr = add_atr(spy, self._atr_window)
        atr_col      = f"atr_{self._atr_window}"
        atr_pct      = spy_with_atr[atr_col] / close

        current_atr_pct = float(atr_pct.iloc[-1]) if pd.notna(atr_pct.iloc[-1]) else None
        pct_rank: float | None = None

        if current_atr_pct is not None:
            window = atr_pct.iloc[-self._vol_pct_window:]
            window_valid = window.dropna()
            if len(window_valid) >= 10:   # need enough history to rank
                pct_rank = float((window_valid < current_atr_pct).mean())
                rank_hit  = pct_rank >= self._vol_pct_threshold
                floor_hit = current_atr_pct >= self._vol_atr_pct_floor
                if rank_hit and floor_hit:
                    logger.info(
                        f"REGIME=VOLATILE — ATR% {current_atr_pct:.4f} "
                        f"at {pct_rank:.0%} of trailing {len(window_valid)}-bar history "
                        f"(rank_threshold={self._vol_pct_threshold:.0%}, "
                        f"atr_pct_floor={self._vol_atr_pct_floor:.4f})"
                    )
                    return MarketRegime.VOLATILE
                if rank_hit and not floor_hit:
                    logger.debug(
                        f"VOLATILE rank hit but absolute floor blocked it — "
                        f"ATR% {current_atr_pct:.4f} < floor {self._vol_atr_pct_floor:.4f} "
                        f"(pct_rank={pct_rank:.0%}); calm-market false-fire suppressed"
                    )

        # ── 3 & 4. TRENDING / RANGING via ADX ────────────────────────────────
        spy_with_adx = add_adx(spy, self._adx_window)
        adx_col      = f"adx_{self._adx_window}"
        adx_val      = spy_with_adx[adx_col].iloc[-1]

        # SMA50 slope for ADX ambiguous zone.
        spy_with_sma50 = add_sma(spy, self._sma_short)
        sma50          = spy_with_sma50[f"sma_{self._sma_short}"]
        sma50_slope: float | None = None
        if len(sma50.dropna()) > self._sma_slope_bars:
            sma50_slope = float(sma50.iloc[-1]) - float(sma50.iloc[-self._sma_slope_bars - 1])

        atr_pct_str  = f"{current_atr_pct:.4f}" if current_atr_pct is not None else "NaN"
        pct_rank_str = f"{pct_rank:.0%}"         if pct_rank is not None else "NaN"
        adx_str      = f"{adx_val:.1f}"          if pd.notna(adx_val) else "NaN"
        slope_str    = f"{sma50_slope:+.3f}"     if sma50_slope is not None else "NaN"

        if pd.notna(adx_val):
            adx_f = float(adx_val)
            if adx_f >= self._adx_trend:
                regime = MarketRegime.TRENDING
            elif adx_f <= self._adx_range:
                regime = MarketRegime.RANGING
            else:
                # Ambiguous zone (20–25): use 50 SMA slope
                regime = (
                    MarketRegime.TRENDING
                    if sma50_slope is not None and sma50_slope > 0
                    else MarketRegime.RANGING
                )
        else:
            # Insufficient ADX history → default to RANGING (conservative)
            regime = MarketRegime.RANGING

        logger.info(
            f"REGIME={regime.value.upper()} — "
            f"SPY {last_close:.2f} > SMA{self._sma_long} {sma_long_val:.2f} | "
            f"ATR% {atr_pct_str} rank={pct_rank_str} | "
            f"ADX={adx_str} | SMA{self._sma_short} slope={slope_str}"
        )
        return regime
