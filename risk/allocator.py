"""
Capital sleeve allocator (Phase 10.F1).

Enforces per-strategy gross-notional budgets ("sleeves") derived from
account equity and configurable allocation weights. Sits upstream of
RiskManager.evaluate() — it narrows the capital available to a strategy
without bypassing any global risk control.

Design decisions (locked in 2026-04-25):
  - Idle sleeve capital stays locked to its strategy (no cross-borrowing).
    Dynamic reallocation is a Phase 11 item.
  - Two strategies may hold the same symbol simultaneously (the user's
    call: if both strategies are convinced it is the right trade, double
    exposure is permitted). Sleeve accounting treats them independently.
  - Open limit orders count against the sleeve at full notional (qty ×
    limit_price). This is conservative but accurate: the capital is
    genuinely committed until the order fills or is cancelled.

Ownership model
---------------
Positions  — attributed via `position_owners` (symbol → strategy_name),
             maintained by the engine from the trade DB (10.C1).
Orders     — attributed via `order_strategy` (order_id → strategy_name),
             computed by the engine each cycle from watchlist membership
             for pending buy entries (see engine._attribute_orders).

Flow
----
  1. Engine calls allocator.check() before risk.evaluate().
  2. allocator.check() returns SleeveCapacity (approved) or
     SleeveRejection (max positions hit / sleeve full / unknown strategy).
  3. On approval, engine passes SleeveCapacity.per_position_notional as
     `notional_cap` to risk.evaluate(), which caps position sizing to the
     per-position budget without changing the risk interface.
  4. Global caps in RiskManager (gross exposure, daily loss, kill
     switches) remain fully authoritative — the allocator only narrows
     the available notional, it never widens it.

Configuration (2026-04-25 format)
----------------------------------
  STRATEGY_ALLOCATIONS = {
      "sma_crossover": {"weight": 0.50, "max_positions": 5},
      "rsi_reversion":  {"weight": 0.50, "max_positions": 5},
  }
  MAX_GROSS_EXPOSURE_PCT = 0.80    # already the global RiskManager cap

  At $100k equity:
    SMA sleeve          = $100k × 0.80 × 0.50 = $40,000
    SMA per-position    = $40,000 ÷ 5          =  $8,000
    RSI sleeve          = $100k × 0.80 × 0.50 = $40,000
    RSI per-position    = $40,000 ÷ 5          =  $8,000
    Unallocated         =  0 % (fully allocated)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from execution.broker import OpenOrder
    from risk.manager import AccountState


# ── Return types ──────────────────────────────────────────────────────────────


class SleeveRejectionCode(Enum):
    SLEEVE_FULL          = "sleeve_full"
    SLEEVE_MAX_POSITIONS = "sleeve_max_positions"
    UNKNOWN_STRATEGY     = "unknown_strategy"


@dataclass(frozen=True)
class SleeveRejection:
    """Returned when a strategy's sleeve cannot accommodate a new entry."""
    strategy_name: str
    code: SleeveRejectionCode
    message: str


@dataclass(frozen=True)
class SleeveCapacity:
    """
    Returned when the sleeve has room for a new entry.

    `per_position_notional` is passed to RiskManager.evaluate() as
    `notional_cap` so every new position is capped to its fair share of
    the sleeve budget, regardless of ATR-derived sizing.

    Fields:
        budget               — equity × total_gross_pct × weight
        used                 — current gross exposure for this strategy
                               (positions + pending buy orders)
        available            — budget - used
        positions_open       — positions currently owned by this strategy
        max_positions        — configured cap on simultaneous positions
        per_position_notional — budget / max_positions (the notional cap
                               passed to risk.evaluate())
    """
    strategy_name: str
    budget:               float
    used:                 float
    available:            float
    positions_open:       int
    max_positions:        int
    per_position_notional: float


# ── Allocator ────────────────────────────────────────────────────────────────


class SleeveAllocator:
    """
    Computes and enforces per-strategy capital sleeves.

    Args:
        allocations:        strategy_name → {"weight": float, "max_positions": int}.
                            Weights must sum to ≤ 1.0. Unallocated weight
                            (if sum < 1.0) sits idle.
        total_gross_pct:    Fraction of equity available for all strategies
                            combined. Should match RiskManager.max_gross_exposure_pct
                            so the two controls are consistent.
        min_trade_notional: Minimum remaining sleeve budget to permit a new
                            entry (prevents tiny residual positions). Default $100.
    """

    def __init__(
        self,
        allocations: dict[str, dict],
        *,
        total_gross_pct: float,
        min_trade_notional: float = 100.0,
    ) -> None:
        if not allocations:
            raise ValueError("allocations must not be empty")

        self._entries: dict[str, dict] = {}
        for name, cfg in allocations.items():
            if not isinstance(cfg, dict):
                raise TypeError(
                    f"allocations['{name}'] must be a dict with 'weight' and "
                    f"'max_positions' keys, got {type(cfg).__name__}"
                )
            if "weight" not in cfg:
                raise ValueError(f"allocations['{name}'] missing 'weight' key")
            if "max_positions" not in cfg:
                raise ValueError(f"allocations['{name}'] missing 'max_positions' key")
            if cfg["max_positions"] < 1:
                raise ValueError(
                    f"allocations['{name}']['max_positions'] must be ≥ 1, "
                    f"got {cfg['max_positions']}"
                )
            self._entries[name] = {"weight": float(cfg["weight"]), "max_positions": int(cfg["max_positions"])}

        total_weight = sum(e["weight"] for e in self._entries.values())
        if total_weight > 1.0 + 1e-6:
            raise ValueError(
                f"allocation weights sum to {total_weight:.4f} — must be ≤ 1.0"
            )
        if not (0.0 < total_gross_pct <= 1.0):
            raise ValueError(
                f"total_gross_pct must be in (0, 1], got {total_gross_pct}"
            )
        if min_trade_notional <= 0:
            raise ValueError("min_trade_notional must be > 0")

        self._total_gross_pct = total_gross_pct
        self._min_notional    = min_trade_notional

    # ── Public API ────────────────────────────────────────────────────────────

    def check(
        self,
        strategy_name: str,
        account: "AccountState",
        open_orders: list["OpenOrder"],
        position_owners: dict[str, str],
        order_strategy: dict[str, str],
    ) -> SleeveCapacity | SleeveRejection:
        """
        Check whether strategy_name has sleeve room for a new entry.

        Checks (in order):
          1. Unknown strategy → UNKNOWN_STRATEGY rejection.
          2. Position count at limit → SLEEVE_MAX_POSITIONS rejection.
          3. Remaining sleeve budget < min_trade_notional → SLEEVE_FULL rejection.
          4. All clear → SleeveCapacity with per_position_notional as the cap.

        Args:
            strategy_name:   The strategy requesting a new entry.
            account:         Current account state (equity + open positions).
            open_orders:     All open broker orders this cycle.
            position_owners: symbol → strategy_name (engine's ownership map).
            order_strategy:  order_id → strategy_name for pending buy entries,
                             computed by the engine from watchlist membership.

        Returns:
            SleeveCapacity  — approved; pass .per_position_notional to
                              risk.evaluate() as notional_cap.
            SleeveRejection — blocked; log and skip this symbol.
        """
        if strategy_name not in self._entries:
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.UNKNOWN_STRATEGY,
                message=(
                    f"no sleeve defined for '{strategy_name}'; "
                    f"known: {sorted(self._entries)}"
                ),
            )

        entry         = self._entries[strategy_name]
        weight        = entry["weight"]
        max_positions = entry["max_positions"]
        budget        = account.equity * self._total_gross_pct * weight
        per_position_notional = budget / max_positions

        # Count positions currently owned by this strategy.
        positions_open = sum(
            1 for strat in position_owners.values() if strat == strategy_name
        )

        used      = self._used_notional(
            strategy_name, account, open_orders, position_owners, order_strategy
        )
        available = budget - used

        logger.debug(
            f"[{strategy_name}] sleeve check — "
            f"budget=${budget:,.0f} used=${used:,.0f} available=${available:,.0f} "
            f"positions={positions_open}/{max_positions} "
            f"per_pos=${per_position_notional:,.0f}"
        )

        # Check 1: position count limit.
        if positions_open >= max_positions:
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.SLEEVE_MAX_POSITIONS,
                message=(
                    f"max positions reached — {positions_open}/{max_positions} open "
                    f"(budget=${budget:,.0f})"
                ),
            )

        # Check 2: remaining budget.
        if available < self._min_notional:
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.SLEEVE_FULL,
                message=(
                    f"sleeve full — budget=${budget:,.0f}, "
                    f"used=${used:,.0f}, available=${available:,.0f} "
                    f"(min_trade=${self._min_notional:,.0f})"
                ),
            )

        return SleeveCapacity(
            strategy_name=strategy_name,
            budget=budget,
            used=used,
            available=available,
            positions_open=positions_open,
            max_positions=max_positions,
            per_position_notional=per_position_notional,
        )

    def sleeve_budget(self, strategy_name: str, equity: float) -> float:
        """Gross notional ceiling for a strategy at the given equity level."""
        entry = self._entries.get(strategy_name)
        if entry is None:
            return 0.0
        return equity * self._total_gross_pct * entry["weight"]

    def strategies(self) -> list[str]:
        """Names of all configured strategies."""
        return list(self._entries.keys())

    # ── Internal ──────────────────────────────────────────────────────────────

    def _used_notional(
        self,
        strategy_name: str,
        account: "AccountState",
        open_orders: list["OpenOrder"],
        position_owners: dict[str, str],
        order_strategy: dict[str, str],
    ) -> float:
        """
        Gross notional currently consumed by strategy_name:
          positions: sum of |market_value| for owned positions
          orders:    sum of qty × limit_price for pending buy orders
                     attributed to this strategy
        """
        # Open positions owned by this strategy.
        position_exposure = sum(
            abs(pos.market_value)
            for sym, pos in account.open_positions.items()
            if position_owners.get(sym) == strategy_name
        )

        # Pending buy orders attributed to this strategy.
        # Sell / close orders are excluded — they reduce exposure, not add.
        order_exposure = 0.0
        for order in open_orders:
            if order_strategy.get(order.order_id) != strategy_name:
                continue
            # Only count buy-side orders (entries consume sleeve budget).
            from risk.manager import Side
            if order.side is not Side.BUY:
                continue
            # Limit orders: use limit_price. Market orders: 0 (filled instantly,
            # position will already be in open_positions by next cycle).
            price = order.limit_price if order.limit_price is not None else 0.0
            order_exposure += float(order.qty) * price

        return position_exposure + order_exposure
