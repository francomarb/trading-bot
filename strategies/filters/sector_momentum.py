"""
Sector Momentum edge filter — consumes ``SectorMomentumGauge`` to gate
or warn on entries based on sector health.

The gauge itself is a context provider (never blocks).  This filter
translates gauge output into an edge-filter boolean using a configurable
``cold_policy``:

  "block"  → COLD sector returns False (entry blocked)
  "warn"   → COLD sector logs a warning but returns True (entry allowed)
  "pass"   → no action regardless of sector state

Exits are never affected — this filter is only used on the entry path
(enforced by ``BaseStrategy.generate_signals``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pandas as pd
from loguru import logger

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
    cold_policy
        What to do when the sector is COLD:
        ``"block"`` (return False), ``"warn"`` (log + return True),
        ``"pass"`` (ignore).
    """

    def __init__(
        self,
        gauge: SectorMomentumGauge,
        resolver: SectorResolver,
        cold_policy: str = "block",
    ) -> None:
        if cold_policy not in ("block", "warn", "pass"):
            raise ValueError(f"cold_policy must be block/warn/pass, got {cold_policy!r}")
        self._gauge = gauge
        self._resolver = resolver
        self._cold_policy = cold_policy
        self._symbol: str = ""

    def set_symbol(self, symbol: str) -> None:
        self._symbol = symbol

    def __call__(self, df: pd.DataFrame) -> pd.Series:
        if not self._symbol:
            return pd.Series(True, index=df.index, dtype=bool)

        sector = self._resolver.resolve(self._symbol)
        if sector is None:
            logger.debug(
                f"SectorMomentumFilter: {self._symbol} has no sector mapping "
                "— allowing entry (fail-open)"
            )
            return pd.Series(True, index=df.index, dtype=bool)

        detail = self._gauge.get_details(sector)

        from sector.gauge import SectorMomentum
        if detail.classification == SectorMomentum.COLD:
            signal_str = (
                f">SMA200={detail.above_sma200}, >SMA50={detail.above_sma50}, "
                f"golden_cross={detail.golden_cross}, "
                f"dist_sma50={detail.dist_sma50_pct:+.1%}, "
                f"vol_confirm={detail.vol_confirm}"
            )
            if self._cold_policy == "block":
                logger.info(
                    f"SECTOR GATE [block]: {self._symbol} "
                    f"({sector}/{detail.etf_ticker}) "
                    f"score={detail.score} [COLD]\n"
                    f"  signals: {signal_str}"
                )
                return pd.Series(False, index=df.index, dtype=bool)
            elif self._cold_policy == "warn":
                logger.info(
                    f"SECTOR GATE [warn]: {self._symbol} "
                    f"({sector}/{detail.etf_ticker}) "
                    f"score={detail.score} [COLD]\n"
                    f"  signals: {signal_str}"
                )

        elif detail.classification == SectorMomentum.HOT:
            logger.debug(
                f"SectorMomentumFilter: {self._symbol} "
                f"({sector}/{detail.etf_ticker}) "
                f"score={detail.score} [HOT]"
            )

        return pd.Series(True, index=df.index, dtype=bool)
