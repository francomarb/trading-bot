"""
Position abstraction (Phase 11.27).

Generalizes the engine from `dict[symbol, strategy_name]` ownership to a
`Position`-based model keyed by `position_id`. A single logical position can
carry one or many legs:

  - single-leg equity   → one PositionLeg, symbol = ticker
  - single-leg option   → one PositionLeg, symbol = OCC string
  - spread (future)     → two or more PositionLegs sharing a position_id

For single-leg positions, `position_id` equals the legacy `owner_key`:
  - equity:  position_id = ticker (e.g. "AAPL")
  - option:  position_id = underlying ticker (e.g. "SPY"), leg symbol = OCC
This keeps the DB backfill trivial — existing rows get `position_id = symbol`
and the engine treats that as the owner key it always used.

Spread support arrives in PR 2/3. PR 1 only ships the abstraction so that
the engine no longer cares whether a position has one leg or many.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterable


_OCC_PAT = re.compile(r"^([A-Z]{1,6})[0-9]{6}[CP][0-9]{8}$")


# ── Constants ───────────────────────────────────────────────────────────────


SINGLE_LEG = "single_leg"
SPREAD = "spread"


VALID_POSITION_TYPES = frozenset({SINGLE_LEG, SPREAD})


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class PositionLeg:
    """
    One leg of a position. Equity positions and single-leg option positions
    have exactly one leg; multi-leg option strategies (e.g. credit spreads)
    have two or more.

    Attributes:
        symbol:       OCC string for options, ticker for equities.
        qty:          Signed share/contract quantity (positive = long).
        entry_price:  Per-share fill price at open (None if unknown).
        entry_time:   Fill timestamp (None if unknown).
        side:         "BUY" or "SELL" at open (the side that opened the leg).
    """

    symbol: str
    qty: float
    entry_price: float | None = None
    entry_time: datetime | None = None
    side: str = "BUY"


@dataclass
class Position:
    """
    A logical position composed of one or more legs, owned by one strategy.

    The ``position_id`` is the engine's primary key:
      - For single-leg positions, it equals the equity ticker or option
        underlying — matching the legacy ``_position_owners`` key.
      - For spreads (PR 2/3), it is a UUID assigned at submission time.

    Attributes:
        position_id:    Stable identifier across legs.
        position_type:  "single_leg" or "spread".
        strategy_name:  Owning strategy (e.g. "rsi_reversion").
        legs:           One or more PositionLeg entries.
    """

    position_id: str
    position_type: str
    strategy_name: str
    legs: list[PositionLeg] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.position_type not in VALID_POSITION_TYPES:
            raise ValueError(
                f"position_type must be one of {sorted(VALID_POSITION_TYPES)}, "
                f"got {self.position_type!r}"
            )
        if self.position_type == SINGLE_LEG and len(self.legs) > 1:
            raise ValueError(
                "single_leg position cannot have more than one leg"
            )
        if not self.position_id:
            raise ValueError("position_id must be a non-empty string")
        if not self.strategy_name:
            raise ValueError("strategy_name must be a non-empty string")

    # ── Convenience accessors ───────────────────────────────────────────

    @property
    def is_single_leg(self) -> bool:
        return self.position_type == SINGLE_LEG

    @property
    def is_spread(self) -> bool:
        return self.position_type == SPREAD

    @property
    def primary_leg(self) -> PositionLeg | None:
        """The first leg, or None if no legs have been recorded yet."""
        return self.legs[0] if self.legs else None

    @property
    def entry_price(self) -> float | None:
        """
        Entry price summary used by realized-P&L bookkeeping.

        For single-leg: the leg's entry price.
        For spread:     the net credit per contract (positive number),
                        computed as the sum of leg-signed entry prices.
                        Returns None if any leg lacks an entry price.
        """
        if not self.legs:
            return None
        if self.is_single_leg:
            return self.legs[0].entry_price
        # Spread: net credit = short premium - long premium.
        # Convention: a leg opened by SELL contributes +entry_price,
        # a leg opened by BUY contributes -entry_price.
        prices = []
        for leg in self.legs:
            if leg.entry_price is None:
                return None
            sign = 1.0 if leg.side == "SELL" else -1.0
            prices.append(sign * leg.entry_price)
        return float(sum(prices))

    def symbols(self) -> list[str]:
        """All leg symbols (OCC strings or tickers)."""
        return [leg.symbol for leg in self.legs]


# ── Helpers ─────────────────────────────────────────────────────────────────


def owner_key_for(symbol: str) -> str:
    """
    Compute the engine's owner_key for a raw broker symbol.

      - equity ticker  → unchanged.
      - OCC option     → underlying ticker (e.g. SPY260516C00520000 → SPY).

    This is the same normalization the legacy ``_position_owners`` map used.
    For single-leg positions, owner_key == position_id by convention.
    """
    match = _OCC_PAT.match(symbol)
    return match.group(1) if match else symbol


def make_single_leg(
    *,
    strategy_name: str,
    symbol: str,
    qty: float = 0.0,
    entry_price: float | None = None,
    entry_time: datetime | None = None,
    side: str = "BUY",
) -> Position:
    """
    Construct a single-leg Position. ``position_id`` is derived from the
    symbol (underlying for OCC option strings, ticker for equities) to
    preserve the existing engine keying.
    """
    position_id = owner_key_for(symbol)
    leg = PositionLeg(
        symbol=symbol,
        qty=qty,
        entry_price=entry_price,
        entry_time=entry_time,
        side=side,
    )
    return Position(
        position_id=position_id,
        position_type=SINGLE_LEG,
        strategy_name=strategy_name,
        legs=[leg],
    )


def new_spread_id() -> str:
    """Allocate a fresh UUID for a multi-leg position (used by PR 2/3)."""
    return uuid.uuid4().hex


def view_owner_map(positions: Iterable[Position]) -> dict[str, str]:
    """
    Build the legacy ``dict[owner_key, strategy_name]`` view from Position
    records. Used to bridge the old SleeveAllocator interface in PR 1 without
    forcing an allocator refactor.

    Spreads contribute one entry per position_id (since the allocator counts
    *positions* per strategy, not legs).
    """
    return {pos.position_id: pos.strategy_name for pos in positions}
