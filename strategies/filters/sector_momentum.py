"""
Sector Momentum edge filter — consumes ``SectorMomentumGauge`` to gate
or warn on entries based on sector health.

The gauge itself is a context provider (never blocks).  This filter
translates gauge output into an edge-filter boolean using a configurable
``sector_entry_policy``:

  "block"  → triggered sector score returns False (entry blocked)
  "warn"   → triggered sector score logs a warning but returns True (entry allowed)
  "pass"   → no action regardless of sector state

By default the trigger is the global COLD classification (score ≤ -2).
Pass ``score_threshold`` to override with a raw score boundary — useful
for mean-reversion strategies that should only block on genuine freefall
rather than a mild pullback (e.g. ``score_threshold=-3`` allows score=-2).

Exits are never affected — this filter is only used on the entry path
(enforced by ``BaseStrategy.generate_signals``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger

from strategies.base import EdgeFilterDecision

if TYPE_CHECKING:
    from sector.gauge import SectorMomentumGauge
    from sector.resolver import SectorResolver


class SectorMomentumFilter:
    """Edge filter that queries sector momentum for the current symbol.

    Parameters
    ----------
    gauge
        The ``SectorMomentumGauge`` instance (shared across strategies).
    resolver
        The ``SectorResolver`` for looking up a stock's sector.
    sector_entry_policy
        What to do when triggered:
        ``"block"`` (return False), ``"warn"`` (log + return True),
        ``"pass"`` (ignore).
    score_threshold
        Optional raw score boundary. When set, the filter triggers when
        ``detail.score <= score_threshold`` instead of using the COLD
        classification. Default ``None`` uses the global COLD threshold.
    """

    def __init__(
        self,
        gauge: SectorMomentumGauge,
        resolver: SectorResolver,
        sector_entry_policy: str = "block",
        score_threshold: int | None = None,
    ) -> None:
        if sector_entry_policy not in ("block", "warn", "pass"):
            raise ValueError(f"sector_entry_policy must be block/warn/pass, got {sector_entry_policy!r}")
        self._gauge = gauge
        self._resolver = resolver
        self._sector_entry_policy = sector_entry_policy
        self._score_threshold = score_threshold
        self._symbol: str = ""

    def set_symbol(self, symbol: str) -> None:
        self._symbol = symbol

    def __call__(self, df: pd.DataFrame) -> EdgeFilterDecision:
        if not self._symbol:
            return EdgeFilterDecision.allow_all(df.index)

        sector = self._resolver.resolve(self._symbol)
        if sector is None:
            logger.debug(
                f"SectorMomentumFilter: {self._symbol} has no sector mapping "
                "— allowing entry (fail-open)"
            )
            return EdgeFilterDecision.allow_all(df.index)

        detail = self._gauge.get_details(sector)

        from sector.gauge import SectorMomentum
        if self._score_threshold is not None:
            triggered = detail.score <= self._score_threshold
        else:
            triggered = detail.classification == SectorMomentum.COLD

        if triggered:
            reason = (
                f"cold sector {sector}/{detail.etf_ticker} "
                f"(score={detail.score:+.1f}, class={detail.classification.value})"
            )
            signal_str = (
                f">SMA200={detail.above_sma200}, >SMA50={detail.above_sma50}, "
                f"golden_cross={detail.golden_cross}, "
                f"dist_sma50={detail.dist_sma50_pct:+.1%}, "
                f"vol_confirm={detail.vol_confirm}"
            )
            if self._sector_entry_policy == "block":
                logger.info(
                    f"SECTOR GATE [block]: {self._symbol} "
                    f"({sector}/{detail.etf_ticker}) "
                    f"score={detail.score:+.1f}\n"
                    f"  signals: {signal_str}"
                )
                return EdgeFilterDecision(
                    allowed=pd.Series(False, index=df.index, dtype=bool),
                    reasons=pd.Series(
                        [[reason] for _ in range(len(df.index))],
                        index=df.index,
                        dtype=object,
                    ),
                )
            elif self._sector_entry_policy == "warn":
                logger.info(
                    f"SECTOR GATE [warn]: {self._symbol} "
                    f"({sector}/{detail.etf_ticker}) "
                    f"score={detail.score:+.1f}\n"
                    f"  signals: {signal_str}"
                )

        elif detail.classification == SectorMomentum.HOT:
            logger.debug(
                f"SectorMomentumFilter: {self._symbol} "
                f"({sector}/{detail.etf_ticker}) "
                f"score={detail.score:+.1f} [HOT]"
            )

        return EdgeFilterDecision.allow_all(df.index)
