"""
Strategy framework (Phase 4).

Defines the abstract `BaseStrategy` contract that every concrete strategy must
implement, plus the shared `SignalFrame` return type and `OrderType` enum.

Design principles (from CLAUDE.md + PLAN.md):

  1. **Pure functions of data.** A strategy is a deterministic transform from
     a bars DataFrame to a pair of boolean Series (entries / exits). No
     network calls, no broker state, no hidden randomness, no reading the
     wall clock.

  2. **vectorbt-native convention.** Signals are *separate* `entries` and
     `exits` boolean Series, not a conflated {-1, 0, 1} column. This scales
     to strategies with variable conviction, multiple positions, and
     directly consumes by the Phase 5 vectorbt backtester.

  3. **No look-ahead.** A signal at bar t is computed from data available at
     t's close only (pandas `rolling` / `shift` constructs naturally respect
     this). The Phase 5 backtester is responsible for shifting execution to
     the *next* bar's open — strategies emit signals aligned to the bar
     whose close triggered them.

  4. **Composable edge filters.** A strategy can be constructed with an
     optional callable `edge_filter(df) -> pd.Series[bool]` that gates
     entries. This is the minimal "regime awareness" hook: e.g. only emit
     long entries when SPY > 200-day MA. Full regime detection is Phase 11.

  5. **Strategy declares its preferred order type.** Trend/breakout
     strategies prefer marketable orders; mean-reversion prefers limit. The
     Phase 7 execution layer reads this attribute and routes accordingly.
     Hard-risk exits (stop-outs) always override with market orders.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Callable

import pandas as pd


# ── Shared types ─────────────────────────────────────────────────────────────


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"


@dataclass(frozen=True)
class SignalFrame:
    """
    The output of `BaseStrategy.generate_signals`.

    - `entries[t] == True` means: at bar t's close, open a new long position.
      Execution happens on bar t+1's open (responsibility of the engine /
      backtester, not the strategy).
    - `exits[t] == True` means: at bar t's close, close any open position.

    Both Series share the same DatetimeIndex as the input bars.
    """

    entries: pd.Series
    exits: pd.Series

    def __post_init__(self) -> None:
        if not self.entries.index.equals(self.exits.index):
            raise ValueError("entries and exits must share the same index")
        if self.entries.dtype != bool or self.exits.dtype != bool:
            raise ValueError("entries and exits must be boolean Series")


EdgeFilter = Callable[[pd.DataFrame], pd.Series]


# ── Base class ───────────────────────────────────────────────────────────────


class BaseStrategy(ABC):
    """
    Abstract strategy.

    Subclasses must set `name` and `preferred_order_type` as class attributes
    and implement `_raw_signals(df)`. The public `generate_signals(df)` applies
    any configured edge filter on top.
    """

    name: str  # concrete subclasses must override
    preferred_order_type: OrderType = OrderType.MARKET

    def __init__(self, *, edge_filter: EdgeFilter | None = None) -> None:
        self._edge_filter = edge_filter

    # Concrete strategies implement this.
    @abstractmethod
    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        """Compute entries/exits before edge-filter gating."""

    def generate_signals(self, df: pd.DataFrame) -> SignalFrame:
        """
        Public entry point. Computes raw signals, then AND-gates entries
        (but not exits — we always want to be able to exit) with the
        edge filter if one is configured.
        """
        raw = self._raw_signals(df)
        if self._edge_filter is None:
            return raw

        gate = self._edge_filter(df)
        if not isinstance(gate, pd.Series):
            raise TypeError(
                f"edge_filter must return a pd.Series, got {type(gate).__name__}"
            )
        # Reindex defensively; anywhere the filter is missing/NaN → treat as
        # "regime not confirmed" → block the entry. Build the boolean Series
        # from raw values to sidestep the pandas fillna-downcast FutureWarning.
        reindexed = gate.reindex(df.index)
        values = [bool(v) if pd.notna(v) else False for v in reindexed]
        gate_aligned = pd.Series(values, index=df.index, dtype=bool)

        return SignalFrame(
            entries=(raw.entries & gate_aligned),
            exits=raw.exits,
        )
