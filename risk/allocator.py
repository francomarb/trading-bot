"""
Capital sleeve allocator (Phase 10.F1 + HWM drawdown gate).

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
     SleeveRejection (max positions hit / sleeve full / unknown strategy /
     strategy in HWM drawdown).
  3. On approval, engine passes SleeveCapacity.per_position_notional as
     `notional_cap` to risk.evaluate(), which caps position sizing to the
     per-position budget without changing the risk interface.
  4. Global caps in RiskManager (gross exposure, daily loss, kill
     switches) remain fully authoritative — the allocator only narrows
     the available notional, it never widens it.

HWM drawdown gate (2026-05-01)
-------------------------------
When `dd_threshold > 0`, the allocator tracks cumulative realized P&L
per strategy. If a strategy's running P&L drops more than
`dd_threshold × sleeve_budget` below its historical peak (high-water
mark), new entries are paused until P&L recovers. Exits are never
blocked. This stops a losing strategy from digging deeper across
multiple sessions.

  record_realized_pnl(strategy_name, pnl) must be called by the engine
  whenever a position closes (signal-based, stop-out, or external close).

Configuration (current format)
-------------------------------
  STRATEGY_ALLOCATIONS = {
      "sma_crossover":    {"weight": 0.50, "max_positions": 5},
      "rsi_reversion":    {"weight": 0.25, "max_positions": 5},
      "donchian_breakout": {"weight": 0.25, "max_positions": 5},
  }
  MAX_GROSS_EXPOSURE_PCT   = 0.80
  STRATEGY_SLEEVE_DD_THRESHOLD = 0.15   # pause entries if down 15% of budget from HWM

  At $100k equity:
    SMA sleeve          = $100k × 0.80 × 0.50 = $40,000
    SMA per-position    = $40,000 ÷ 5          =  $8,000
    RSI sleeve          = $100k × 0.80 × 0.25 = $20,000
    RSI per-position    = $20,000 ÷ 5          =  $4,000
    Donchian sleeve     = $100k × 0.80 × 0.25 = $20,000
    Donchian per-pos    = $20,000 ÷ 5          =  $4,000
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
    SLEEVE_DRAWDOWN      = "sleeve_drawdown"


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
        dd_threshold:       High-water-mark drawdown gate. If a strategy's
                            cumulative realized P&L drops more than
                            `dd_threshold × sleeve_budget` below its peak,
                            new entries are paused. 0.0 disables the gate
                            (default). Must be in [0, 1).
    """

    def __init__(
        self,
        allocations: dict[str, dict],
        *,
        total_gross_pct: float,
        min_trade_notional: float = 100.0,
        dd_threshold: float = 0.0,
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
            self._entries[name] = {
                "weight": float(cfg["weight"]),
                "max_positions": int(cfg["max_positions"]),
            }

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
        if not (0.0 <= dd_threshold < 1.0):
            raise ValueError(
                f"dd_threshold must be in [0, 1), got {dd_threshold}"
            )

        self._total_gross_pct = total_gross_pct
        self._min_notional    = min_trade_notional
        self._dd_threshold    = dd_threshold

        # HWM drawdown gate state — updated by record_realized_pnl().
        # Keyed by strategy_name; initialized to 0.0 (break-even) for all
        # configured strategies so the HWM starts at par, not negative.
        self._strategy_realized_pnl: dict[str, float] = {
            name: 0.0 for name in self._entries
        }
        self._strategy_pnl_hwm: dict[str, float] = {
            name: 0.0 for name in self._entries
        }

    # ── Public API ────────────────────────────────────────────────────────────

    def record_realized_pnl(self, strategy_name: str, pnl: float) -> None:
        """
        Record a realized P&L event for a strategy (called by the engine on
        every position close — signal-based exit, ATR stop, or external close).

        Updates the running cumulative P&L and advances the HWM when the
        strategy is at a new equity peak. If the gate subsequently trips,
        check() returns SLEEVE_DRAWDOWN until P&L recovers.

        Unknown strategy names are silently ignored (defensive — should not
        happen in normal operation).
        """
        if strategy_name not in self._strategy_realized_pnl:
            logger.warning(
                f"record_realized_pnl: unknown strategy '{strategy_name}' — ignored"
            )
            return

        prev = self._strategy_realized_pnl[strategy_name]
        self._strategy_realized_pnl[strategy_name] = prev + pnl

        # Advance HWM only on improvement.
        running = self._strategy_realized_pnl[strategy_name]
        if running > self._strategy_pnl_hwm[strategy_name]:
            self._strategy_pnl_hwm[strategy_name] = running

        logger.debug(
            f"[{strategy_name}] realized_pnl update: "
            f"trade={pnl:+.2f} cumulative={running:+.2f} "
            f"hwm={self._strategy_pnl_hwm[strategy_name]:+.2f}"
        )

    def is_strategy_in_drawdown(self, strategy_name: str, equity: float) -> bool:
        """
        Return True if the strategy's cumulative realized P&L is more than
        `dd_threshold × sleeve_budget` below its HWM.

        Returns False when the gate is disabled (dd_threshold == 0) or the
        strategy is unknown.
        """
        if self._dd_threshold == 0.0:
            return False
        if strategy_name not in self._entries:
            return False

        budget  = self._sleeve_budget_for(strategy_name, equity)
        running = self._strategy_realized_pnl[strategy_name]
        hwm     = self._strategy_pnl_hwm[strategy_name]
        gap     = hwm - running          # positive when below HWM
        trigger = self._dd_threshold * budget
        return gap > trigger

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
          1. Unknown strategy       → UNKNOWN_STRATEGY rejection.
          2. HWM drawdown gate      → SLEEVE_DRAWDOWN rejection (if enabled
                                      and strategy P&L is below threshold).
          3. Position count at limit → SLEEVE_MAX_POSITIONS rejection.
          4. Remaining budget low   → SLEEVE_FULL rejection.
          5. All clear              → SleeveCapacity.

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

        # HWM drawdown gate diagnostic — always log at DEBUG regardless of gate state.
        if self._dd_threshold > 0.0:
            running = self._strategy_realized_pnl.get(strategy_name, 0.0)
            hwm     = self._strategy_pnl_hwm.get(strategy_name, 0.0)
            trigger = self._dd_threshold * budget
            logger.debug(
                f"[{strategy_name}] DD gate — "
                f"realized={running:+.2f} hwm={hwm:+.2f} "
                f"gap={hwm - running:.2f} trigger={trigger:.2f}"
            )

        logger.debug(
            f"[{strategy_name}] sleeve check — "
            f"budget=${budget:,.0f} used=${used:,.0f} available=${available:,.0f} "
            f"positions={positions_open}/{max_positions} "
            f"per_pos=${per_position_notional:,.0f}"
        )

        # Check 1: HWM drawdown gate.
        if self.is_strategy_in_drawdown(strategy_name, account.equity):
            running = self._strategy_realized_pnl[strategy_name]
            hwm     = self._strategy_pnl_hwm[strategy_name]
            trigger = self._dd_threshold * budget
            logger.warning(
                f"[{strategy_name}] SLEEVE_DRAWDOWN — new entries paused: "
                f"realized_pnl={running:+.2f} hwm={hwm:+.2f} "
                f"gap={hwm - running:.2f} > trigger={trigger:.2f} "
                f"({self._dd_threshold*100:.0f}% of ${budget:,.0f} budget). "
                f"Entries resume when P&L recovers."
            )
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.SLEEVE_DRAWDOWN,
                message=(
                    f"strategy in drawdown — realized_pnl={running:+.2f} "
                    f"is {hwm - running:.2f} below HWM={hwm:+.2f} "
                    f"(threshold={trigger:.2f})"
                ),
            )

        # Check 2: position count limit.
        if positions_open >= max_positions:
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.SLEEVE_MAX_POSITIONS,
                message=(
                    f"max positions reached — {positions_open}/{max_positions} open "
                    f"(budget=${budget:,.0f})"
                ),
            )

        # Check 3: remaining budget.
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
        return self._sleeve_budget_for(strategy_name, equity)

    def strategies(self) -> list[str]:
        """Names of all configured strategies."""
        return list(self._entries.keys())

    def pnl_summary(self) -> dict[str, dict[str, float]]:
        """
        Return per-strategy P&L tracking state. Useful for logging and
        diagnostics. Keys: strategy_name → {"realized_pnl", "hwm"}.
        """
        return {
            name: {
                "realized_pnl": self._strategy_realized_pnl[name],
                "hwm": self._strategy_pnl_hwm[name],
            }
            for name in self._entries
        }

    # ── Internal ──────────────────────────────────────────────────────────────

    def _sleeve_budget_for(self, strategy_name: str, equity: float) -> float:
        entry = self._entries.get(strategy_name)
        if entry is None:
            return 0.0
        return equity * self._total_gross_pct * entry["weight"]

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
