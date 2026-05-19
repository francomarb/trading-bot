"""
Capital allocator with dual pools, elastic equity sleeves, and drawdown gate.

The allocator sits upstream of RiskManager and answers one question:
"How much strategy capital is available for this new entry right now?"

RiskManager remains the sizing authority. It sizes from stop-risk first and
then trims to the allocator-supplied notional ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from execution.broker import OpenOrder
    from risk.manager import AccountState


_OCC_OPTION_SYMBOL = re.compile(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$")


def _contract_multiplier(symbol: str) -> int:
    """Return the contract multiplier for equities vs OCC option symbols."""
    return 100 if _OCC_OPTION_SYMBOL.match(symbol or "") else 1


class PoolType(str, Enum):
    EQUITY = "equity"
    ISOLATED = "isolated"


class SleeveRejectionCode(Enum):
    SLEEVE_FULL = "sleeve_full"
    SLEEVE_MAX_POSITIONS = "sleeve_max_positions"
    UNKNOWN_STRATEGY = "unknown_strategy"
    SLEEVE_DRAWDOWN = "sleeve_drawdown"


@dataclass(frozen=True)
class SleeveRejection:
    strategy_name: str
    code: SleeveRejectionCode
    message: str


@dataclass(frozen=True)
class SleeveCapacity:
    strategy_name: str
    pool_type: str
    target_budget: float
    effective_budget: float
    borrowed_budget: float
    used: float
    available: float
    positions_open: int
    hard_max_positions: int
    max_position_notional: float


@dataclass(frozen=True)
class PoolSnapshot:
    pool_type: str
    target_budget: float
    used: float
    available: float
    utilization: float
    pending_entry_notional: float


class SleeveAllocator:
    """
    Capital allocator with:
      - dual pools: shared equity + isolated options
      - elastic equity borrowing
      - strategy priority metadata
      - strategy-level HWM drawdown pause
      - hard position-count ceiling separate from sizing
    """

    def __init__(
        self,
        allocations: dict[str, dict],
        *,
        total_gross_pct: float,
        capital_pools: dict[str, float],
        stretch_utilization_threshold: float,
        default_stretch_pct: float,
        min_trade_notional: float = 100.0,
        dd_threshold: float = 0.0,
    ) -> None:
        if not allocations:
            raise ValueError("allocations must not be empty")
        if not (0.0 < total_gross_pct <= 1.0):
            raise ValueError(
                f"total_gross_pct must be in (0, 1], got {total_gross_pct}"
            )
        if min_trade_notional <= 0:
            raise ValueError("min_trade_notional must be > 0")
        if not (0.0 <= dd_threshold < 1.0):
            raise ValueError(f"dd_threshold must be in [0, 1), got {dd_threshold}")
        if not (0.0 < stretch_utilization_threshold <= 1.0):
            raise ValueError(
                "stretch_utilization_threshold must be in (0, 1]"
            )
        if default_stretch_pct < 0:
            raise ValueError("default_stretch_pct must be >= 0")

        required_pools = {PoolType.EQUITY.value, "isolated_options"}
        if set(capital_pools) != required_pools:
            raise ValueError(
                f"capital_pools must define exactly {sorted(required_pools)}, "
                f"got {sorted(capital_pools)}"
            )

        pool_total = sum(float(v) for v in capital_pools.values())
        if abs(pool_total - 1.0) > 1e-6:
            raise ValueError(
                f"capital_pools sum to {pool_total:.4f} — must equal 1.0"
            )

        self._capital_pools = {
            PoolType.EQUITY.value: float(capital_pools[PoolType.EQUITY.value]),
            "isolated_options": float(capital_pools["isolated_options"]),
        }
        self._entries: dict[str, dict] = {}
        priorities: set[int] = set()
        target_total = 0.0
        equity_total = 0.0
        isolated_total = 0.0
        for name, cfg in allocations.items():
            if not isinstance(cfg, dict):
                raise TypeError(
                    f"allocations['{name}'] must be a dict, got {type(cfg).__name__}"
                )

            missing = {
                "target_pct",
                "type",
                "priority",
                "can_stretch",
                "hard_max_positions",
                "max_position_pct_of_sleeve",
            } - set(cfg)
            if missing:
                raise ValueError(
                    f"allocations['{name}'] missing keys: {sorted(missing)}"
                )

            target_pct = float(cfg["target_pct"])
            pool_type = str(cfg["type"])
            priority = int(cfg["priority"])
            can_stretch = bool(cfg["can_stretch"])
            hard_max_positions = int(cfg["hard_max_positions"])
            max_position_pct = float(cfg["max_position_pct_of_sleeve"])
            stretch_pct = float(cfg.get("stretch_pct", default_stretch_pct))

            if pool_type not in {PoolType.EQUITY.value, PoolType.ISOLATED.value}:
                raise ValueError(
                    f"allocations['{name}']['type'] must be 'equity' or 'isolated', "
                    f"got {pool_type!r}"
                )
            if priority < 0:
                raise ValueError(
                    f"allocations['{name}']['priority'] must be >= 0, got {priority}"
                )
            if priority in priorities:
                raise ValueError(f"duplicate strategy priority {priority}")
            priorities.add(priority)
            if hard_max_positions < 1:
                raise ValueError(
                    f"allocations['{name}']['hard_max_positions'] must be >= 1, "
                    f"got {hard_max_positions}"
                )
            if not (0.0 < target_pct <= 1.0):
                raise ValueError(
                    f"allocations['{name}']['target_pct'] must be in (0, 1], "
                    f"got {target_pct}"
                )
            if not (0.0 < max_position_pct <= 1.0):
                raise ValueError(
                    f"allocations['{name}']['max_position_pct_of_sleeve'] must be in "
                    f"(0, 1], got {max_position_pct}"
                )
            if stretch_pct < 0:
                raise ValueError(
                    f"allocations['{name}']['stretch_pct'] must be >= 0, got {stretch_pct}"
                )
            if pool_type == PoolType.ISOLATED.value and can_stretch:
                raise ValueError(
                    f"allocations['{name}'] is isolated and cannot stretch"
                )

            self._entries[name] = {
                "target_pct": target_pct,
                "pool_type": pool_type,
                "priority": priority,
                "can_stretch": can_stretch,
                "hard_max_positions": hard_max_positions,
                "max_position_pct_of_sleeve": max_position_pct,
                "stretch_pct": stretch_pct,
            }
            target_total += target_pct
            if pool_type == PoolType.EQUITY.value:
                equity_total += target_pct
            else:
                isolated_total += target_pct

        if abs(target_total - 1.0) > 1e-6:
            raise ValueError(
                f"strategy target_pct values sum to {target_total:.4f} — must equal 1.0"
            )
        if abs(equity_total - self._capital_pools[PoolType.EQUITY.value]) > 1e-6:
            raise ValueError(
                "equity strategy target_pct total must match CAPITAL_POOLS['equity']"
            )
        if abs(isolated_total - self._capital_pools["isolated_options"]) > 1e-6:
            raise ValueError(
                "isolated strategy target_pct total must match "
                "CAPITAL_POOLS['isolated_options']"
            )

        self._total_gross_pct = total_gross_pct
        self._stretch_utilization_threshold = stretch_utilization_threshold
        self._default_stretch_pct = default_stretch_pct
        self._min_notional = min_trade_notional
        self._dd_threshold = dd_threshold
        self._strategy_realized_pnl = {name: 0.0 for name in self._entries}
        self._strategy_pnl_hwm = {name: 0.0 for name in self._entries}

    def record_realized_pnl(self, strategy_name: str, pnl: float) -> None:
        if strategy_name not in self._strategy_realized_pnl:
            logger.warning(
                f"record_realized_pnl: unknown strategy '{strategy_name}' — ignored"
            )
            return

        self._strategy_realized_pnl[strategy_name] += pnl
        running = self._strategy_realized_pnl[strategy_name]
        if running > self._strategy_pnl_hwm[strategy_name]:
            self._strategy_pnl_hwm[strategy_name] = running

        logger.debug(
            f"[{strategy_name}] realized_pnl update: trade={pnl:+.2f} "
            f"cumulative={running:+.2f} hwm={self._strategy_pnl_hwm[strategy_name]:+.2f}"
        )

    def pnl_summary(self) -> dict[str, dict[str, float]]:
        return {
            name: {
                "realized_pnl": self._strategy_realized_pnl[name],
                "hwm": self._strategy_pnl_hwm[name],
            }
            for name in self._entries
        }

    def restore_pnl_summary(self, summary: dict[str, dict[str, float]]) -> None:
        """Restore cumulative realized P&L / HWM state, typically from the trade log."""
        for name in self._entries:
            restored = summary.get(name, {})
            realized_pnl = float(restored.get("realized_pnl", 0.0))
            hwm = max(float(restored.get("hwm", 0.0)), realized_pnl)
            self._strategy_realized_pnl[name] = realized_pnl
            self._strategy_pnl_hwm[name] = hwm
            logger.debug(
                f"[{name}] restored allocator pnl state: "
                f"cumulative={realized_pnl:+.2f} hwm={hwm:+.2f}"
            )

    def strategies(self) -> list[str]:
        return list(self._entries.keys())

    def strategy_priority(self, strategy_name: str) -> int:
        entry = self._entries.get(strategy_name)
        if entry is None:
            return 1_000_000
        return int(entry["priority"])

    def strategy_pool_type(self, strategy_name: str) -> str | None:
        entry = self._entries.get(strategy_name)
        if entry is None:
            return None
        return str(entry["pool_type"])

    def target_budget(self, strategy_name: str, equity: float) -> float:
        entry = self._entries.get(strategy_name)
        if entry is None:
            return 0.0
        return equity * self._total_gross_pct * float(entry["target_pct"])

    def is_strategy_in_drawdown(self, strategy_name: str, equity: float) -> bool:
        if self._dd_threshold == 0.0 or strategy_name not in self._entries:
            return False
        target_budget = self.target_budget(strategy_name, equity)
        running = self._strategy_realized_pnl[strategy_name]
        hwm = self._strategy_pnl_hwm[strategy_name]
        return (hwm - running) > (self._dd_threshold * target_budget)

    def drawdown_snapshot(self, equity: float) -> dict[str, dict]:
        """Read-only per-strategy HWM-drawdown state.

        Surfaces the sleeve drawdown gate state for the engine state
        snapshot. Consumed by HealthAssessor L1 checks (PLAN 11.10d/f)
        to surface "strategy in sleeve drawdown" as a WATCH finding.
        Pure read; no mutation.

        Returns: `{strategy_name: {"in_drawdown": bool, "running_pnl":
        float, "hwm_pnl": float, "drawdown_dollars": float}}` for
        every registered strategy.
        """
        out: dict[str, dict] = {}
        for strategy_name in self._entries:
            running = self._strategy_realized_pnl.get(strategy_name, 0.0)
            hwm = self._strategy_pnl_hwm.get(strategy_name, 0.0)
            out[strategy_name] = {
                "in_drawdown": self.is_strategy_in_drawdown(
                    strategy_name, equity,
                ),
                "running_pnl": running,
                "hwm_pnl": hwm,
                "drawdown_dollars": max(hwm - running, 0.0),
            }
        return out

    def check(
        self,
        strategy_name: str,
        account: "AccountState",
        open_orders: list["OpenOrder"],
        position_owners: dict[str, str],
        order_strategy: dict[str, str],
        additional_used_notional: dict[str, float] | None = None,
    ) -> SleeveCapacity | SleeveRejection:
        if strategy_name not in self._entries:
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.UNKNOWN_STRATEGY,
                message=(
                    f"no sleeve defined for '{strategy_name}'; known: "
                    f"{sorted(self._entries)}"
                ),
            )

        snapshot = self._strategy_snapshot(
            strategy_name,
            account,
            open_orders,
            position_owners,
            order_strategy,
            additional_used_notional,
        )

        logger.debug(
            f"[{strategy_name}] sleeve check — "
            f"target=${snapshot.target_budget:,.0f} "
            f"effective=${snapshot.effective_budget:,.0f} "
            f"borrowed=${snapshot.borrowed_budget:,.0f} "
            f"used=${snapshot.used:,.0f} "
            f"available=${snapshot.available:,.0f} "
            f"positions={snapshot.positions_open}/{snapshot.hard_max_positions} "
            f"max_pos=${snapshot.max_position_notional:,.0f}"
        )

        if self.is_strategy_in_drawdown(strategy_name, account.equity):
            running = self._strategy_realized_pnl[strategy_name]
            hwm = self._strategy_pnl_hwm[strategy_name]
            trigger = self._dd_threshold * snapshot.target_budget
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.SLEEVE_DRAWDOWN,
                message=(
                    f"strategy in drawdown — realized_pnl={running:+.2f} "
                    f"is {hwm - running:.2f} below HWM={hwm:+.2f} "
                    f"(threshold={trigger:.2f})"
                ),
            )

        if snapshot.positions_open >= snapshot.hard_max_positions:
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.SLEEVE_MAX_POSITIONS,
                message=(
                    "hard max positions reached — "
                    f"{snapshot.positions_open}/{snapshot.hard_max_positions} open "
                    f"(available=${snapshot.available:,.0f})"
                ),
            )

        if snapshot.available < self._min_notional:
            return SleeveRejection(
                strategy_name=strategy_name,
                code=SleeveRejectionCode.SLEEVE_FULL,
                message=(
                    f"sleeve full — effective_budget=${snapshot.effective_budget:,.0f}, "
                    f"used=${snapshot.used:,.0f}, available=${snapshot.available:,.0f} "
                    f"(min_trade=${self._min_notional:,.0f})"
                ),
            )

        return snapshot

    def snapshot(
        self,
        account: "AccountState",
        open_orders: list["OpenOrder"],
        position_owners: dict[str, str],
        order_strategy: dict[str, str],
        additional_used_notional: dict[str, float] | None = None,
    ) -> dict[str, dict]:
        strategies: dict[str, dict] = {}
        pending_strategy_notional = self._pending_order_notional_by_strategy(
            open_orders,
            order_strategy,
        )
        for name in self._entries:
            cap = self._strategy_snapshot(
                name,
                account,
                open_orders,
                position_owners,
                order_strategy,
                additional_used_notional,
            )
            strategies[name] = {
                "pool_type": cap.pool_type,
                "priority": self._entries[name]["priority"],
                "target_budget": cap.target_budget,
                "effective_budget": cap.effective_budget,
                "borrowed_budget": cap.borrowed_budget,
                "used": cap.used,
                "available": cap.available,
                "positions_open": cap.positions_open,
                "hard_max_positions": cap.hard_max_positions,
                "max_position_notional": cap.max_position_notional,
                "pending_entry_notional": pending_strategy_notional.get(name, 0.0),
            }

        pools: dict[str, dict] = {}
        pool_pending = {
            PoolType.EQUITY.value: 0.0,
            "isolated_options": 0.0,
        }
        for strategy_name, notional in pending_strategy_notional.items():
            pool_name = self._pool_name_for_strategy(strategy_name)
            pool_pending[pool_name] += notional

        for pool_type, share in self._capital_pools.items():
            target_budget = account.equity * self._total_gross_pct * share
            used = self._pool_used_notional(
                pool_type,
                account,
                open_orders,
                position_owners,
                order_strategy,
                additional_used_notional,
            )
            available = max(0.0, target_budget - used)
            pools[pool_type] = {
                "target_budget": target_budget,
                "used": used,
                "available": available,
                "utilization": (used / target_budget) if target_budget > 0 else 0.0,
                "pending_entry_notional": pool_pending.get(pool_type, 0.0),
            }

        return {"strategies": strategies, "pools": pools}

    def _strategy_snapshot(
        self,
        strategy_name: str,
        account: "AccountState",
        open_orders: list["OpenOrder"],
        position_owners: dict[str, str],
        order_strategy: dict[str, str],
        additional_used_notional: dict[str, float] | None = None,
    ) -> SleeveCapacity:
        entry = self._entries[strategy_name]
        pool_type = entry["pool_type"]
        target_budget = self.target_budget(strategy_name, account.equity)
        used = self._used_notional(
            strategy_name,
            account,
            open_orders,
            position_owners,
            order_strategy,
            additional_used_notional,
        )
        positions_open = sum(
            1 for owner in position_owners.values() if owner == strategy_name
        )
        effective_budget = target_budget
        borrowed_budget = 0.0
        if pool_type == PoolType.EQUITY.value and entry["can_stretch"]:
            deployable_capital = account.equity * self._total_gross_pct
            pool_budget = deployable_capital * self._capital_pools[pool_type]
            pool_used = self._pool_used_notional(
                pool_type,
                account,
                open_orders,
                position_owners,
                order_strategy,
                additional_used_notional,
            )
            pool_slack = max(0.0, pool_budget - pool_used)
            utilization = (
                self._total_used_notional(
                    account,
                    open_orders,
                    position_owners,
                    order_strategy,
                    additional_used_notional,
                ) / deployable_capital
                if deployable_capital > 0 else 1.0
            )
            if utilization < self._stretch_utilization_threshold and pool_slack > 0:
                stretch_cap = target_budget * (1.0 + entry["stretch_pct"])
                effective_budget = min(stretch_cap, target_budget + pool_slack)
                borrowed_budget = max(0.0, effective_budget - target_budget)

        available = max(0.0, effective_budget - used)
        max_position_notional = min(
            available,
            effective_budget * entry["max_position_pct_of_sleeve"],
        )
        return SleeveCapacity(
            strategy_name=strategy_name,
            pool_type=pool_type,
            target_budget=target_budget,
            effective_budget=effective_budget,
            borrowed_budget=borrowed_budget,
            used=used,
            available=available,
            positions_open=positions_open,
            hard_max_positions=entry["hard_max_positions"],
            max_position_notional=max_position_notional,
        )

    def _pool_name_for_strategy(self, strategy_name: str) -> str:
        strategy_pool = self._entries[strategy_name]["pool_type"]
        return strategy_pool if strategy_pool == PoolType.EQUITY.value else "isolated_options"

    def _position_notional_by_strategy(
        self,
        account: "AccountState",
        position_owners: dict[str, str],
        additional_used_notional: dict[str, float] | None = None,
    ) -> dict[str, float]:
        totals = {name: 0.0 for name in self._entries}
        for symbol, pos in account.open_positions.items():
            owner = position_owners.get(symbol)
            if owner in totals:
                totals[owner] += abs(pos.market_value)
        for strategy_name, notional in (additional_used_notional or {}).items():
            if strategy_name in totals:
                totals[strategy_name] += max(0.0, float(notional))
        return totals

    def _pending_order_notional_by_strategy(
        self,
        open_orders: list["OpenOrder"],
        order_strategy: dict[str, str],
    ) -> dict[str, float]:
        totals = {name: 0.0 for name in self._entries}
        for order in open_orders:
            strategy_name = order_strategy.get(order.order_id)
            if strategy_name not in totals:
                continue
            from risk.manager import Side

            if order.side is not Side.BUY:
                continue
            price = float(order.limit_price or 0.0)
            totals[strategy_name] += (
                float(order.qty) * price * _contract_multiplier(order.symbol)
            )
        return totals

    def _used_notional(
        self,
        strategy_name: str,
        account: "AccountState",
        open_orders: list["OpenOrder"],
        position_owners: dict[str, str],
        order_strategy: dict[str, str],
        additional_used_notional: dict[str, float] | None = None,
    ) -> float:
        positions = self._position_notional_by_strategy(
            account, position_owners, additional_used_notional
        )
        pending = self._pending_order_notional_by_strategy(open_orders, order_strategy)
        return positions.get(strategy_name, 0.0) + pending.get(strategy_name, 0.0)

    def _pool_used_notional(
        self,
        pool_type: str,
        account: "AccountState",
        open_orders: list["OpenOrder"],
        position_owners: dict[str, str],
        order_strategy: dict[str, str],
        additional_used_notional: dict[str, float] | None = None,
    ) -> float:
        positions = self._position_notional_by_strategy(
            account, position_owners, additional_used_notional
        )
        pending = self._pending_order_notional_by_strategy(open_orders, order_strategy)
        total = 0.0
        for strategy_name, entry in self._entries.items():
            strategy_pool_name = (
                entry["pool_type"]
                if entry["pool_type"] == PoolType.EQUITY.value
                else "isolated_options"
            )
            if strategy_pool_name != pool_type:
                continue
            total += positions.get(strategy_name, 0.0) + pending.get(strategy_name, 0.0)
        return total

    def _total_used_notional(
        self,
        account: "AccountState",
        open_orders: list["OpenOrder"],
        position_owners: dict[str, str],
        order_strategy: dict[str, str],
        additional_used_notional: dict[str, float] | None = None,
    ) -> float:
        return self._pool_used_notional(
            PoolType.EQUITY.value,
            account,
            open_orders,
            position_owners,
            order_strategy,
            additional_used_notional,
        ) + self._pool_used_notional(
            "isolated_options",
            account,
            open_orders,
            position_owners,
            order_strategy,
            additional_used_notional,
        )
