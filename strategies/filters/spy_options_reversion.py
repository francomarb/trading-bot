"""
SPY Options Reversion edge filter.

Two gates:

1. SPY close must be above its 100-day SMA (structural bull regime separator).
2. VIX-percentile gate — enforced **only in TRENDING regime**: today's VIX must
   sit at or above ``SPY_OPTIONS_MIN_VIX_PERCENTILE`` of its trailing ~1-year
   range (the ≤-percentile from ``IVProxyResolver.resolve_rank``).

Rationale
---------
This strategy buys SPY calls on an oversold-RSI bounce. In a structural bear
market (SPY below 100 SMA) every bounce is a dead-cat setup, so gate 1 stays.

Gate 2 comes from the 2018–2025 production-mirrored backtest + the live paper
sample: the reversion edge splits sharply by regime × implied vol. In a
**TRENDING** market a dip only pays when vol is already elevated (fear fuels a
sharp snap-back); a dip in a *calm* uptrend is noise/continuation that theta
grinds the long call out on — the single worst quadrant (TRENDING + low VIX was
−104% over 8 backtest trades, and 3/3 of the live losers). In a **RANGING**
market the mean-reversion edge holds regardless of vol, so gate 2 is not
enforced there — RANGING trades on gate 1 alone.

Regime ownership
----------------
Regime is **detected only at the engine** (``RegimeDetector``). This filter does
not detect or own regime logic; it merely *receives* the current regime label
via :meth:`set_regime` (injected each cycle by ``BaseStrategy.inspect_signals``,
mirroring the existing ``set_symbol`` injection) and uses it to decide whether
to enforce gate 2. When no regime is injected (offline/back-compat callers) the
VIX gate is not enforced and the filter behaves as gate 1 only.

Fail-safe posture
-----------------
- SPY gate: fails CLOSED on API failure (no SPY data / no cache) — unchanged.
- VIX gate (TRENDING only): fails CLOSED — if the VIX percentile is unavailable
  or the trailing series is insufficient, a TRENDING entry is blocked. We only
  take the trend-regime trade when we can positively confirm elevated IV.
"""

from __future__ import annotations

import pandas as pd
from loguru import logger

from config import settings
from strategies.base import EdgeFilterDecision
from strategies.filters.common import SPYTrendFilter
from utils.iv_proxy import IVProxyResolver


class SPYOptionsEdgeFilter:
    """
    Entry gate for SPY Options Reversion.

    Gate 1: SPY close > 100-day SMA (always).
    Gate 2: VIX percentile ≥ ``min_vix_percentile`` (TRENDING regime only).

    Args:
        spy_lookback_days:  Calendar days of SPY history to fetch (default 180).
        spy_cache_ttl:      Seconds to reuse cached SPY data (default 600).
        iv_resolver:        Shared IV-proxy data layer. Production wiring injects
                            the one resolver shared with the strategy and the
                            credit-spread filter (single daily VIX fetch). Tests
                            inject a stub. Defaults to a private resolver.
        min_vix_percentile: TRENDING-regime VIX-percentile floor. Defaults to
                            ``settings.SPY_OPTIONS_MIN_VIX_PERCENTILE`` (0.60).
        vix_source:         IV-proxy source key (default ``"vix"``).
    """

    def __init__(
        self,
        *,
        spy_lookback_days: int = 180,
        spy_cache_ttl: float = 600.0,
        iv_resolver: IVProxyResolver | None = None,
        min_vix_percentile: float = settings.SPY_OPTIONS_MIN_VIX_PERCENTILE,
        vix_source: str = "vix",
    ) -> None:
        self._spy_filter = SPYTrendFilter(
            sma_windows=[100],
            lookback_days=spy_lookback_days,
            cache_ttl_seconds=spy_cache_ttl,
        )
        self._iv_resolver = iv_resolver or IVProxyResolver()
        self._min_vix_percentile = float(min_vix_percentile)
        self._vix_source = vix_source
        # Current market-regime label, injected by the engine each cycle via
        # set_regime(). None → VIX gate not enforced (offline/back-compat).
        self._regime: str | None = None

    def set_symbol(self, symbol: str) -> None:
        # SPY is both the symbol and the filter target — nothing to propagate.
        pass

    def set_regime(self, regime) -> None:
        """Receive the current market regime from the engine.

        Regime detection stays entirely at the engine (``RegimeDetector``); the
        filter only consumes the label to decide whether to enforce the VIX gate.
        Accepts a ``MarketRegime`` enum (uses ``.value``) or a plain string.
        """
        value = getattr(regime, "value", regime)
        self._regime = value.lower() if isinstance(value, str) else None

    def _vix_gate(self) -> tuple[bool, str | None]:
        """Evaluate the VIX-percentile gate. Returns ``(allowed, block_reason)``.

        Only meaningful in TRENDING regime. Fails CLOSED when the percentile is
        unavailable or the trailing series is insufficient.
        """
        snap = self._iv_resolver.resolve_rank(self._vix_source)
        pct = snap.percentile
        if pct is None or not snap.sufficient:
            return False, (
                "VIX percentile unavailable "
                f"(sufficient={snap.sufficient}, lookback_days={snap.lookback_days_used}) "
                "— fail-closed in TRENDING"
            )
        if pct < self._min_vix_percentile:
            return False, (
                f"VIX percentile {pct:.2f} < {self._min_vix_percentile:.2f} "
                f"(VIX={snap.current:.1f}) — TRENDING dip requires elevated IV"
            )
        return True, None

    def __call__(self, df: pd.DataFrame) -> EdgeFilterDecision:
        # ── Gate 1: SPY > 100 SMA (per-bar series) ──────────────────────────
        gate: pd.Series = self._spy_filter(df)
        spy_reason = self._spy_filter.last_reason
        spy_ok = gate.astype(bool)

        # ── Gate 2: VIX percentile — TRENDING only ──────────────────────────
        # The gate is a "today" scalar (one VIX percentile), so its decision is
        # broadcast across every bar and AND-ed with the per-bar SPY gate. Only
        # the latest bar drives a live entry.
        enforce_vix = self._regime == "trending"
        vix_ok, vix_reason = True, None
        if enforce_vix:
            vix_ok, vix_reason = self._vix_gate()

        allowed = spy_ok & vix_ok  # scalar bool broadcasts over the Series

        reasons = pd.Series(
            [
                (
                    ([] if ok else [f"SPY trend gate failed: {spy_reason}"])
                    + ([vix_reason] if (enforce_vix and not vix_ok) else [])
                )
                for ok in spy_ok.tolist()
            ],
            index=spy_ok.index,
            dtype=object,
        )

        if not df.empty:
            if bool(allowed.iloc[-1]):
                suffix = (
                    f", VIX%ile≥{self._min_vix_percentile:.2f} (TRENDING)"
                    if enforce_vix
                    else ""
                )
                logger.info(f"SPY_OPTIONS_FILTER_ALLOWED — SPY above 100 SMA{suffix}")
            else:
                latest = reasons.iloc[-1]
                logger.info(f"SPY_OPTIONS_FILTER_BLOCKED — {'; '.join(latest)}")

        return EdgeFilterDecision(
            allowed=allowed,
            reasons=reasons,
        )
