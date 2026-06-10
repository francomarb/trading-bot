"""Unit tests for the capital-driven allocator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from execution.broker import BrokerSnapshot, OpenOrder, OrderResult, OrderStatus
from risk.allocator import (
    SleeveAllocator,
    SleeveCapacity,
    SleeveRejection,
    SleeveRejectionCode,
)
from risk.manager import AccountState, Position, RiskManager, Side
from strategies.base import BaseStrategy, OrderType, SignalFrame, StrategySlot

T0 = datetime(2026, 1, 2, tzinfo=timezone.utc)

_CAPITAL_POOLS = {
    "equity": 0.95,
    "isolated_options": 0.05,
}

_DEFAULT_ALLOCS = {
    "sma_crossover": {
        "target_pct": 0.45,
        "type": "equity",
        "priority": 3,
        "can_stretch": True,
        "hard_max_positions": 8,
        "max_position_pct_of_sleeve": 0.40,
    },
    "rsi_reversion": {
        "target_pct": 0.50,
        "type": "equity",
        "priority": 1,
        "can_stretch": True,
        "hard_max_positions": 8,
        "max_position_pct_of_sleeve": 0.40,
    },
    "spy_options_reversion": {
        "target_pct": 0.05,
        "type": "isolated",
        "priority": 0,
        "can_stretch": False,
        "hard_max_positions": 1,
        "max_position_pct_of_sleeve": 1.00,
    },
}


def _account(
    equity: float = 100_000.0,
    positions: dict[str, Position] | None = None,
    cash: float | None = None,
) -> AccountState:
    return AccountState(
        equity=equity,
        cash=equity if cash is None else cash,
        session_start_equity=equity,
        open_positions=positions or {},
    )


def _position(symbol: str, qty: float, price: float) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry_price=price,
        market_value=qty * price,
    )


def _open_order(
    order_id: str,
    symbol: str,
    *,
    strategy: str,
    side: Side = Side.BUY,
    qty: float = 10,
    limit_price: float = 100.0,
) -> tuple[OpenOrder, dict[str, str]]:
    return (
        OpenOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=OrderType.LIMIT,
            status="open",
            submitted_at=T0,
            limit_price=limit_price,
            stop_price=None,
        ),
        {order_id: strategy},
    )


def _allocator(**kwargs) -> SleeveAllocator:
    kwargs.setdefault("allocations", _DEFAULT_ALLOCS)
    kwargs.setdefault("total_gross_pct", 0.80)
    kwargs.setdefault("capital_pools", _CAPITAL_POOLS)
    kwargs.setdefault("stretch_utilization_threshold", 0.80)
    kwargs.setdefault("default_stretch_pct", 0.15)
    kwargs.setdefault("min_trade_notional", 100.0)
    return SleeveAllocator(**kwargs)


class TestSleeveAllocatorConstruction:
    def test_valid_construction(self):
        allocator = _allocator()
        assert allocator.strategy_priority("spy_options_reversion") == 0
        assert allocator.strategy_priority("rsi_reversion") == 1

    def test_missing_required_key_raises(self):
        with pytest.raises(ValueError, match="missing keys"):
            _allocator(
                allocations={
                    "bad": {
                        "target_pct": 1.0,
                        "type": "equity",
                        "priority": 0,
                        "can_stretch": True,
                        "hard_max_positions": 8,
                    }
                }
            )

    def test_pool_totals_must_match(self):
        bad = dict(_DEFAULT_ALLOCS)
        bad["rsi_reversion"] = dict(bad["rsi_reversion"], target_pct=0.40)
        bad["spy_options_reversion"] = dict(
            bad["spy_options_reversion"], target_pct=0.15
        )
        with pytest.raises(ValueError, match="equity strategy target_pct total"):
            _allocator(allocations=bad)

    def test_isolated_strategy_cannot_stretch(self):
        bad = dict(_DEFAULT_ALLOCS)
        bad["spy_options_reversion"] = dict(
            bad["spy_options_reversion"], can_stretch=True
        )
        with pytest.raises(ValueError, match="cannot stretch"):
            _allocator(allocations=bad)

    def test_duplicate_priorities_rejected(self):
        bad = dict(_DEFAULT_ALLOCS)
        bad["sma_crossover"] = dict(bad["sma_crossover"], priority=1)
        with pytest.raises(ValueError, match="duplicate strategy priority"):
            _allocator(allocations=bad)


class TestSleeveCheck:
    def test_target_budget_uses_global_deployable_capital(self):
        result = _allocator().check(
            "sma_crossover",
            _account(),
            [],
            {},
            {},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.target_budget == pytest.approx(36_000.0)
        assert result.effective_budget == pytest.approx(41_400.0)
        assert result.borrowed_budget == pytest.approx(5_400.0)
        assert result.max_position_notional == pytest.approx(16_560.0)

    def test_equity_stretch_disabled_above_utilization_threshold(self):
        positions = {
            "SMA": _position("SMA", 1, 30_000.0),
            "RSI": _position("RSI", 1, 34_000.0),
            "OPT": _position("OPT", 1, 5_000.0),
        }
        owners = {
            "SMA": "sma_crossover",
            "RSI": "rsi_reversion",
            "OPT": "spy_options_reversion",
        }
        result = _allocator().check(
            "sma_crossover",
            _account(positions=positions),
            [],
            owners,
            {},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.effective_budget == pytest.approx(36_000.0)
        assert result.borrowed_budget == pytest.approx(0.0)

    def test_isolated_options_never_borrow(self):
        result = _allocator().check(
            "spy_options_reversion",
            _account(),
            [],
            {},
            {},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.pool_type == "isolated"
        assert result.target_budget == pytest.approx(4_000.0)
        assert result.effective_budget == pytest.approx(4_000.0)
        assert result.borrowed_budget == pytest.approx(0.0)

    def test_pending_buy_orders_count_against_available_capital(self):
        order, order_strategy = _open_order(
            "ord-1", "MSFT", strategy="sma_crossover", qty=50, limit_price=200.0
        )
        result = _allocator().check(
            "sma_crossover",
            _account(),
            [order],
            {},
            order_strategy,
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used == pytest.approx(10_000.0)
        assert result.available == pytest.approx(31_400.0)

    def test_pending_option_orders_use_contract_multiplier(self):
        order, order_strategy = _open_order(
            "ord-opt",
            "SPY260616C00520000",
            strategy="spy_options_reversion",
            qty=2,
            limit_price=10.0,
        )
        result = _allocator().check(
            "spy_options_reversion",
            _account(),
            [order],
            {},
            order_strategy,
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used == pytest.approx(2_000.0)
        assert result.available == pytest.approx(2_000.0)

    def test_additional_used_notional_counts_against_strategy_and_pool(self):
        result = _allocator().check(
            "spy_options_reversion",
            _account(),
            [],
            {},
            {},
            additional_used_notional={"spy_options_reversion": 850.0},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used == pytest.approx(850.0)
        assert result.available == pytest.approx(3_150.0)

    def test_count_rejection_comes_only_from_hard_max_positions(self):
        positions = {
            f"SYM{i}": _position(f"SYM{i}", 1, 1_000.0)
            for i in range(8)
        }
        owners = {symbol: "sma_crossover" for symbol in positions}
        result = _allocator().check(
            "sma_crossover",
            _account(positions=positions),
            [],
            owners,
            {},
        )
        assert isinstance(result, SleeveRejection)
        assert result.code is SleeveRejectionCode.SLEEVE_MAX_POSITIONS

    def test_available_capital_binds_independently_of_count(self):
        positions = {
            "AAPL": _position("AAPL", 1, 41_350.0),
        }
        owners = {"AAPL": "sma_crossover"}
        result = _allocator().check(
            "sma_crossover",
            _account(positions=positions),
            [],
            owners,
            {},
        )
        assert isinstance(result, SleeveRejection)
        assert result.code is SleeveRejectionCode.SLEEVE_FULL

    def test_snapshot_reports_allocator_and_pool_usage(self):
        positions = {
            "AAPL": _position("AAPL", 1, 10_000.0),
            "SPY": _position("SPY", 1, 2_000.0),
        }
        owners = {
            "AAPL": "sma_crossover",
            "SPY": "spy_options_reversion",
        }
        order, order_strategy = _open_order(
            "ord-2", "ALLY", strategy="rsi_reversion", qty=20, limit_price=100.0
        )
        snapshot = _allocator().snapshot(
            _account(positions=positions),
            [order],
            owners,
            order_strategy,
        )
        assert snapshot["strategies"]["sma_crossover"]["used"] == pytest.approx(10_000.0)
        assert snapshot["strategies"]["rsi_reversion"]["pending_entry_notional"] == pytest.approx(2_000.0)
        assert snapshot["pools"]["equity"]["pending_entry_notional"] == pytest.approx(2_000.0)
        assert snapshot["pools"]["isolated_options"]["used"] == pytest.approx(2_000.0)

    def test_snapshot_attributes_occ_option_position_to_underlying_owner(self):
        positions = {
            "SPY260618C00746000": _position("SPY260618C00746000", 3, 1_277.0),
        }
        owners = {
            "SPY": "spy_options_reversion",
        }
        snapshot = _allocator().snapshot(
            _account(positions=positions),
            [],
            owners,
            {},
        )
        assert snapshot["strategies"]["spy_options_reversion"]["positions_open"] == 1
        assert snapshot["strategies"]["spy_options_reversion"]["used"] == pytest.approx(3_831.0)
        assert snapshot["pools"]["isolated_options"]["used"] == pytest.approx(3_831.0)

    def test_snapshot_includes_additional_used_notional(self):
        snapshot = _allocator().snapshot(
            _account(),
            [],
            {"spread-1": "spy_options_reversion"},
            {},
            additional_used_notional={"spy_options_reversion": 850.0},
        )
        assert snapshot["strategies"]["spy_options_reversion"]["used"] == pytest.approx(850.0)
        assert snapshot["pools"]["isolated_options"]["used"] == pytest.approx(850.0)


class TestSleeveDrawdownGate:
    def _seed_above_floor(
        self, allocator: SleeveAllocator, strategy: str, count: int = 30,
    ) -> None:
        """Push enough no-op trades through to clear the min-trades guard.

        The new min-trades guard (see settings.STRATEGY_MIN_TRADES_FOR_DRAWDOWN_GATE)
        unconditionally returns False below the floor — so any test that wants
        to exercise the dollar-math part of the gate needs to seed enough
        trades for the guard to arm.
        """
        for _ in range(count):
            allocator.record_realized_pnl(strategy, 0.0)

    def test_drawdown_uses_target_budget_not_stretched_budget(self):
        allocator = _allocator(dd_threshold=0.15)
        self._seed_above_floor(allocator, "sma_crossover")
        allocator.record_realized_pnl("sma_crossover", -5_400.0)
        assert allocator.is_strategy_in_drawdown("sma_crossover", 100_000.0) is False
        allocator.record_realized_pnl("sma_crossover", -1.0)
        assert allocator.is_strategy_in_drawdown("sma_crossover", 100_000.0) is True

    def test_check_returns_drawdown_rejection(self):
        allocator = _allocator(dd_threshold=0.15)
        self._seed_above_floor(allocator, "sma_crossover")
        allocator.record_realized_pnl("sma_crossover", -5_401.0)
        result = allocator.check("sma_crossover", _account(), [], {}, {})
        assert isinstance(result, SleeveRejection)
        assert result.code is SleeveRejectionCode.SLEEVE_DRAWDOWN

    def test_restore_pnl_summary_rehydrates_cumulative_and_hwm(self):
        allocator = _allocator(dd_threshold=0.15)
        allocator.restore_pnl_summary(
            {
                "sma_crossover": {"realized_pnl": 75.0, "hwm": 100.0, "trade_count": 30},
                "rsi_reversion": {"realized_pnl": -25.0, "hwm": 0.0, "trade_count": 12},
            }
        )
        summary = allocator.pnl_summary()
        assert summary["sma_crossover"] == {
            "realized_pnl": pytest.approx(75.0),
            "hwm": pytest.approx(100.0),
            "trade_count": pytest.approx(30.0),
        }
        assert summary["rsi_reversion"] == {
            "realized_pnl": pytest.approx(-25.0),
            "hwm": pytest.approx(0.0),
            "trade_count": pytest.approx(12.0),
        }


class TestSleeveDrawdownMinTradesGuard:
    """The min-trades guard prevents the drawdown gate from firing
    when a strategy has too few trades to make HWM-vs-running meaningful.

    Motivating case (2026-06-10): spy_options_reversion had exactly ONE
    closed trade — a −$1,269 exit fired against still-buggy trailing-stop
    code that has since been fixed (PR #46 hardening). That single
    data point tripped the gate indefinitely. The guard removes that
    failure mode: below the floor, the gate fails open and the
    daily-loss / hard-dollar kill switches remain the active defense.
    """

    def test_gate_fails_open_below_floor(self):
        # spy_options_reversion floor = 15 (settings).
        allocator = _allocator(dd_threshold=0.05)
        allocator.record_realized_pnl("spy_options_reversion", -50_000.0)
        # Even with a catastrophic loss, N=1 must not trip the gate.
        assert allocator.is_strategy_in_drawdown(
            "spy_options_reversion", 100_000.0,
        ) is False

    def test_gate_arms_at_floor(self):
        allocator = _allocator(dd_threshold=0.05)
        # rsi_reversion floor = 8 (settings) — quickest to set up.
        for _ in range(7):
            allocator.record_realized_pnl("rsi_reversion", 0.0)
        # 7 no-op trades — below floor.
        allocator.record_realized_pnl("rsi_reversion", -10_000.0)
        # N=8 now (the loss is the 8th). Floor is 8 — guard armed.
        # Gate fires because the math is in drawdown.
        assert allocator.is_strategy_in_drawdown(
            "rsi_reversion", 100_000.0,
        ) is True

    def test_unknown_strategy_returns_false(self):
        # Pre-existing invariant — unaffected by the guard.
        allocator = _allocator(dd_threshold=0.05)
        assert allocator.is_strategy_in_drawdown("nonexistent", 100_000.0) is False

    def test_dd_threshold_zero_disables_gate_regardless(self):
        # Pre-existing invariant — disabling the gate is unconditional.
        allocator = _allocator(dd_threshold=0.0)
        for _ in range(100):
            allocator.record_realized_pnl("sma_crossover", -1_000.0)
        assert allocator.is_strategy_in_drawdown(
            "sma_crossover", 100_000.0,
        ) is False

    def test_floor_default_used_for_unmapped_strategy(self, monkeypatch):
        # When a strategy is in the allocator but NOT in the
        # min-trades-for-drawdown-gate map, the default floor applies.
        from config import settings as _s
        monkeypatch.setattr(_s, "STRATEGY_MIN_TRADES_FOR_DRAWDOWN_GATE", {})
        monkeypatch.setattr(_s, "STRATEGY_DEFAULT_MIN_TRADES_FOR_DRAWDOWN_GATE", 5)
        allocator = _allocator(dd_threshold=0.05)
        for _ in range(4):
            allocator.record_realized_pnl("sma_crossover", -100.0)
        # 4 trades — below default of 5.
        assert allocator.is_strategy_in_drawdown(
            "sma_crossover", 100_000.0,
        ) is False
        # One more — N=5, guard armed, math still in drawdown.
        allocator.record_realized_pnl("sma_crossover", -10_000.0)
        assert allocator.is_strategy_in_drawdown(
            "sma_crossover", 100_000.0,
        ) is True

    def test_drawdown_snapshot_exposes_guard_state(self):
        # The snapshot used for observability/health must surface the
        # trade count, the floor, and whether the guard is armed.
        allocator = _allocator(dd_threshold=0.05)
        allocator.record_realized_pnl("spy_options_reversion", -1_269.0)
        snap = allocator.drawdown_snapshot(100_000.0)
        entry = snap["spy_options_reversion"]
        assert entry["trade_count"] == 1
        assert entry["min_trades_for_gate"] == 15  # settings value
        assert entry["gate_armed"] is False
        assert entry["in_drawdown"] is False  # because gate not armed
        # The dollar math is preserved for diagnostic display.
        assert entry["drawdown_dollars"] == pytest.approx(1_269.0)
        assert entry["running_pnl"] == pytest.approx(-1_269.0)

    def test_record_realized_pnl_increments_trade_count(self):
        allocator = _allocator()
        for i in range(5):
            allocator.record_realized_pnl("sma_crossover", float(i))
        summary = allocator.pnl_summary()
        assert summary["sma_crossover"]["trade_count"] == pytest.approx(5.0)

    def test_restore_pnl_summary_handles_missing_trade_count_field(self):
        # Backward-compat: legacy summaries lacking trade_count restore N=0.
        # Combined with the guard, this is safe: legacy startups can't have
        # the gate fire on first cycle, but the guard reads the live count
        # going forward. Trade count is then re-counted from the trade log.
        allocator = _allocator(dd_threshold=0.05)
        allocator.restore_pnl_summary(
            {
                "sma_crossover": {"realized_pnl": -10_000.0, "hwm": 0.0},
                # no "trade_count" key — pre-fix summary shape
            }
        )
        # N=0 (defaulted) → guard not armed → gate returns False.
        assert allocator.is_strategy_in_drawdown(
            "sma_crossover", 100_000.0,
        ) is False
        snap = allocator.drawdown_snapshot(100_000.0)
        assert snap["sma_crossover"]["trade_count"] == 0
        assert snap["sma_crossover"]["gate_armed"] is False


class TestRiskManagerNotionalCap:
    def _risk(self) -> RiskManager:
        return RiskManager(
            max_position_pct=0.02,
            max_open_positions=10,
            max_gross_exposure_pct=0.80,
            atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.10,
            hard_dollar_loss_cap=10_000.0,
            loss_streak_threshold=10,
            broker_error_threshold=10,
        )

    def _signal(self, price: float = 100.0, atr: float = 2.0):
        from risk.manager import Signal

        return Signal(
            symbol="AAPL",
            side=Side.BUY,
            strategy_name="sma_crossover",
            reference_price=price,
            atr=atr,
            reason="test",
            order_type=OrderType.MARKET,
        )

    def test_allocator_cap_reduces_qty_without_replacing_risk_sizing(self):
        decision = self._risk().evaluate(
            self._signal(price=100.0, atr=2.0),
            _account(equity=100_000.0),
            notional_cap=8_000.0,
        )
        assert decision.qty == 80

    def test_first_position_does_not_automatically_take_concentration_maximum(self):
        decision = self._risk().evaluate(
            self._signal(price=100.0, atr=20.0),
            _account(equity=100_000.0),
            notional_cap=16_560.0,
        )
        assert decision.qty == 50
        assert decision.qty * decision.entry_reference_price == pytest.approx(5_000.0)


class TestEngineAllocatorIntegration:
    def _bars(self) -> pd.DataFrame:
        now = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        idx = pd.DatetimeIndex(
            [now - timedelta(days=59 - i) for i in range(60)], tz="UTC"
        )
        closes = [100.0 + i for i in range(60)]
        return pd.DataFrame(
            {
                "open": [c - 0.5 for c in closes],
                "high": [c + 2.0 for c in closes],
                "low": [c - 2.0 for c in closes],
                "close": closes,
                "volume": [1_000_000] * 60,
            },
            index=idx,
        )

    def test_engine_blocks_new_entry_when_available_capital_exhausted(self):
        from engine.trader import EngineConfig, TradingEngine
        from reporting.alerts import AlertDispatcher
        from reporting.logger import TradeLogger
        from reporting.pnl import PnLTracker

        class _Strategy(BaseStrategy):
            name = "sma_crossover"
            preferred_order_type = OrderType.MARKET

            def _raw_signals(self, df):
                entries = pd.Series([False] * len(df), index=df.index, dtype=bool)
                exits = pd.Series([False] * len(df), index=df.index, dtype=bool)
                entries.iloc[-1] = True
                return SignalFrame(entries=entries, exits=exits)

        slot = StrategySlot(strategy=_Strategy(), symbols=["AAPL"])
        positions = {"MSFT": _position("MSFT", 1, 41_350.0)}
        broker = MagicMock()
        broker.sync_with_broker.return_value = BrokerSnapshot(
            account=_account(positions=positions),
            open_orders=[],
        )
        broker.place_order.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="ord-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=100.0,
            raw_status="filled",
            message="ok",
        )
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

        engine = TradingEngine(
            slots=[slot],
            risk=RiskManager(
                max_position_pct=0.02,
                max_open_positions=10,
                max_gross_exposure_pct=0.80,
                atr_stop_multiplier=2.0,
                max_daily_loss_pct=0.10,
                hard_dollar_loss_cap=10_000.0,
                loss_streak_threshold=10,
                broker_error_threshold=10,
            ),
            broker=broker,
            config=EngineConfig(
                history_lookback_days=60,
                cycle_interval_seconds=300,
                max_bar_age_multiplier=10,
                market_hours_only=False,
            ),
            trade_logger=MagicMock(spec=TradeLogger),
            pnl_tracker=MagicMock(spec=PnLTracker),
            alerts=MagicMock(spec=AlertDispatcher),
            allocator=_allocator(),
        )
        engine._register_single_leg(strategy_name="sma_crossover", symbol="MSFT")

        with patch(
            "engine.trader.fetch_symbol",
            return_value=(self._bars(), SimpleNamespace(api_calls=0)),
        ):
            engine.start(max_cycles=1)

        broker.place_order.assert_not_called()
