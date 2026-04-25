"""
Unit tests for risk/allocator.py (Phase 10.F1).

Coverage map:
  - TestSleeveAllocatorConstruction : validation of weights, max_positions, gross_pct
  - TestSleeveBudget                : sleeve_budget() arithmetic
  - TestSleeveCheck                 : approved / max-positions / sleeve-full /
                                      unknown-strategy for various position +
                                      order combinations
  - TestPerPositionNotional         : per_position_notional derived correctly
  - TestRiskManagerNotionalCap      : notional_cap threads into _size_position
                                      and caps qty correctly
  - TestEngineSleeveGating          : full engine cycle — SMA sleeve full blocks
                                      new SMA entries; RSI sleeve unaffected
"""

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


# ── Helpers ──────────────────────────────────────────────────────────────────

T0 = datetime(2026, 1, 2, tzinfo=timezone.utc)

# Default allocation config used by most tests:
#   SMA: 50% weight, 5 max positions → per-pos = $40k / 5 = $8k at $100k equity
_DEFAULT_ALLOCS = {
    "sma_crossover": {"weight": 0.50, "max_positions": 5},
    "rsi_reversion":  {"weight": 0.50, "max_positions": 5},
}


def _account(
    equity: float = 100_000.0,
    positions: dict | None = None,
) -> AccountState:
    return AccountState(
        equity=equity,
        cash=equity,
        session_start_equity=equity,
        open_positions=positions or {},
    )


def _position(symbol: str, qty: int, price: float) -> Position:
    return Position(
        symbol=symbol,
        qty=qty,
        avg_entry_price=price,
        market_value=qty * price,
    )


def _open_order(
    order_id: str,
    symbol: str,
    side: Side = Side.BUY,
    qty: int = 10,
    limit_price: float | None = 150.0,
) -> OpenOrder:
    return OpenOrder(
        order_id=order_id,
        symbol=symbol,
        side=side,
        qty=qty,
        order_type=OrderType.LIMIT,
        status="open",
        submitted_at=T0,
        limit_price=limit_price,
        stop_price=None,
    )


def _allocator(**kwargs) -> SleeveAllocator:
    kwargs.setdefault("allocations", _DEFAULT_ALLOCS)
    kwargs.setdefault("total_gross_pct", 0.80)
    kwargs.setdefault("min_trade_notional", 100.0)
    return SleeveAllocator(**kwargs)


# ── Construction validation ───────────────────────────────────────────────────


class TestSleeveAllocatorConstruction:
    def test_valid_construction(self):
        a = _allocator()
        assert set(a.strategies()) == {"sma_crossover", "rsi_reversion"}

    def test_empty_allocations_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            SleeveAllocator(allocations={}, total_gross_pct=0.80)

    def test_non_dict_value_raises(self):
        with pytest.raises(TypeError, match="must be a dict"):
            SleeveAllocator(
                allocations={"a": 0.50},   # float, not dict
                total_gross_pct=0.80,
            )

    def test_missing_weight_key_raises(self):
        with pytest.raises(ValueError, match="missing 'weight'"):
            SleeveAllocator(
                allocations={"a": {"max_positions": 5}},
                total_gross_pct=0.80,
            )

    def test_missing_max_positions_key_raises(self):
        with pytest.raises(ValueError, match="missing 'max_positions'"):
            SleeveAllocator(
                allocations={"a": {"weight": 0.50}},
                total_gross_pct=0.80,
            )

    def test_max_positions_zero_raises(self):
        with pytest.raises(ValueError, match="max_positions.*≥ 1"):
            SleeveAllocator(
                allocations={"a": {"weight": 0.50, "max_positions": 0}},
                total_gross_pct=0.80,
            )

    def test_weights_sum_over_one_raises(self):
        with pytest.raises(ValueError, match="must be ≤ 1.0"):
            SleeveAllocator(
                allocations={
                    "a": {"weight": 0.60, "max_positions": 5},
                    "b": {"weight": 0.60, "max_positions": 5},
                },
                total_gross_pct=0.80,
            )

    def test_weights_summing_to_exactly_one_is_valid(self):
        SleeveAllocator(
            allocations={
                "a": {"weight": 0.50, "max_positions": 5},
                "b": {"weight": 0.50, "max_positions": 5},
            },
            total_gross_pct=0.80,
        )

    def test_weights_summing_under_one_is_valid(self):
        # Unallocated remainder sits idle — valid
        SleeveAllocator(
            allocations={
                "a": {"weight": 0.40, "max_positions": 5},
                "b": {"weight": 0.30, "max_positions": 5},
            },
            total_gross_pct=0.80,
        )

    def test_total_gross_pct_above_one_raises(self):
        with pytest.raises(ValueError, match="total_gross_pct"):
            SleeveAllocator(
                allocations={"a": {"weight": 1.0, "max_positions": 5}},
                total_gross_pct=1.5,
            )

    def test_total_gross_pct_zero_raises(self):
        with pytest.raises(ValueError, match="total_gross_pct"):
            SleeveAllocator(
                allocations={"a": {"weight": 1.0, "max_positions": 5}},
                total_gross_pct=0.0,
            )

    def test_min_notional_zero_raises(self):
        with pytest.raises(ValueError, match="min_trade_notional"):
            SleeveAllocator(
                allocations={"a": {"weight": 1.0, "max_positions": 5}},
                total_gross_pct=0.80,
                min_trade_notional=0.0,
            )


# ── sleeve_budget() ───────────────────────────────────────────────────────────


class TestSleeveBudget:
    def test_50_50_split_at_80pct(self):
        a = _allocator()
        # equity=100k, gross=80%, weight=50% → budget=40k
        assert a.sleeve_budget("sma_crossover", 100_000.0) == pytest.approx(40_000.0)
        assert a.sleeve_budget("rsi_reversion",  100_000.0) == pytest.approx(40_000.0)

    def test_unknown_strategy_returns_zero(self):
        a = _allocator()
        assert a.sleeve_budget("unknown", 100_000.0) == 0.0

    def test_budget_scales_with_equity(self):
        a = _allocator()
        assert a.sleeve_budget("sma_crossover", 200_000.0) == pytest.approx(80_000.0)


# ── per_position_notional ─────────────────────────────────────────────────────


class TestPerPositionNotional:
    """per_position_notional = budget / max_positions (fixed, not proportional to used)."""

    def test_per_position_notional_at_equity_100k(self):
        # budget=$40k, max_positions=5 → $8k per position
        a = _allocator()
        cap = a.check(
            strategy_name="sma_crossover",
            account=_account(equity=100_000.0),
            open_orders=[],
            position_owners={},
            order_strategy={},
        )
        assert isinstance(cap, SleeveCapacity)
        assert cap.per_position_notional == pytest.approx(8_000.0)

    def test_per_position_notional_unchanged_when_sleeve_partially_used(self):
        """The per-position cap is fixed at budget/max_positions, not based on available."""
        a = _allocator()
        # 1 position open, $15k used
        positions = {"AAPL": _position("AAPL", 100, 150.0)}
        cap = a.check(
            strategy_name="sma_crossover",
            account=_account(positions=positions),
            open_orders=[],
            position_owners={"AAPL": "sma_crossover"},
            order_strategy={},
        )
        assert isinstance(cap, SleeveCapacity)
        assert cap.per_position_notional == pytest.approx(8_000.0)
        assert cap.available == pytest.approx(25_000.0)  # available changes...
        assert cap.per_position_notional == pytest.approx(8_000.0)  # ...cap does not

    def test_per_position_notional_scales_with_equity(self):
        a = _allocator()
        cap = a.check(
            strategy_name="sma_crossover",
            account=_account(equity=200_000.0),
            open_orders=[],
            position_owners={},
            order_strategy={},
        )
        assert isinstance(cap, SleeveCapacity)
        # budget=$80k, max=5 → $16k per position
        assert cap.per_position_notional == pytest.approx(16_000.0)

    def test_custom_max_positions_divides_budget(self):
        a = SleeveAllocator(
            allocations={"sma_crossover": {"weight": 0.50, "max_positions": 10}},
            total_gross_pct=0.80,
        )
        cap = a.check(
            strategy_name="sma_crossover",
            account=_account(equity=100_000.0),
            open_orders=[],
            position_owners={},
            order_strategy={},
        )
        assert isinstance(cap, SleeveCapacity)
        # budget=$40k, max=10 → $4k per position
        assert cap.per_position_notional == pytest.approx(4_000.0)


# ── check() — approved paths ──────────────────────────────────────────────────


class TestSleeveCheck:
    def test_approved_when_no_positions_no_orders(self):
        a = _allocator()
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(),
            open_orders=[],
            position_owners={},
            order_strategy={},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.budget               == pytest.approx(40_000.0)
        assert result.used                 == pytest.approx(0.0)
        assert result.available            == pytest.approx(40_000.0)
        assert result.positions_open       == 0
        assert result.max_positions        == 5
        assert result.per_position_notional == pytest.approx(8_000.0)

    def test_approved_with_partial_sleeve_used(self):
        a = _allocator()
        # One $15k SMA position open
        positions = {"AAPL": _position("AAPL", 100, 150.0)}
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(positions=positions),
            open_orders=[],
            position_owners={"AAPL": "sma_crossover"},
            order_strategy={},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used                 == pytest.approx(15_000.0)
        assert result.available            == pytest.approx(25_000.0)
        assert result.positions_open       == 1
        assert result.per_position_notional == pytest.approx(8_000.0)

    def test_positions_from_other_strategy_not_counted(self):
        """RSI positions don't count against SMA sleeve (budget or position count)."""
        a = _allocator()
        positions = {"NVDA": _position("NVDA", 20, 900.0)}  # $18k RSI position
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(positions=positions),
            open_orders=[],
            position_owners={"NVDA": "rsi_reversion"},
            order_strategy={},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used           == pytest.approx(0.0)
        assert result.available      == pytest.approx(40_000.0)
        assert result.positions_open == 0

    def test_open_buy_limit_order_counts_against_sleeve(self):
        """Pending RSI limit order consumes RSI sleeve budget."""
        a = _allocator()
        orders = [_open_order("ord-1", "MSFT", qty=50, limit_price=300.0)]
        result = a.check(
            strategy_name="rsi_reversion",
            account=_account(),
            open_orders=orders,
            position_owners={},
            order_strategy={"ord-1": "rsi_reversion"},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used      == pytest.approx(50 * 300.0)   # $15k
        assert result.available == pytest.approx(40_000.0 - 15_000.0)

    def test_open_sell_order_not_counted(self):
        """Sell/close orders reduce exposure, not increase it — excluded."""
        a = _allocator()
        orders = [_open_order("ord-2", "AAPL", side=Side.SELL, qty=100, limit_price=150.0)]
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(),
            open_orders=orders,
            position_owners={},
            order_strategy={"ord-2": "sma_crossover"},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used == pytest.approx(0.0)

    def test_order_from_other_strategy_not_counted(self):
        a = _allocator()
        orders = [_open_order("ord-3", "MSFT", qty=50, limit_price=300.0)]
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(),
            open_orders=orders,
            position_owners={},
            order_strategy={"ord-3": "rsi_reversion"},  # RSI's order
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used == pytest.approx(0.0)

    def test_positions_plus_orders_combined(self):
        """Both open position and pending order count together against used."""
        a = _allocator()
        positions = {"AAPL": _position("AAPL", 100, 150.0)}  # $15k
        orders = [_open_order("ord-4", "MSFT", qty=50, limit_price=200.0)]  # $10k
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(positions=positions),
            open_orders=orders,
            position_owners={"AAPL": "sma_crossover"},
            order_strategy={"ord-4": "sma_crossover"},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.used      == pytest.approx(25_000.0)
        assert result.available == pytest.approx(15_000.0)

    # ── Rejection — max positions ─────────────────────────────────────────────

    def test_rejected_when_max_positions_reached(self):
        a = _allocator()   # max_positions=5
        # Build 5 open SMA positions
        positions = {
            f"SYM{i}": _position(f"SYM{i}", 10, 100.0) for i in range(5)
        }
        owners = {sym: "sma_crossover" for sym in positions}
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(positions=positions),
            open_orders=[],
            position_owners=owners,
            order_strategy={},
        )
        assert isinstance(result, SleeveRejection)
        assert result.code == SleeveRejectionCode.SLEEVE_MAX_POSITIONS
        assert "5/5" in result.message

    def test_rejected_at_exactly_max_positions(self):
        """5 positions = limit; 4 positions should still be approved."""
        a = _allocator()
        # 4 positions — still approved
        positions = {f"SYM{i}": _position(f"SYM{i}", 10, 100.0) for i in range(4)}
        owners = {sym: "sma_crossover" for sym in positions}
        result_4 = a.check(
            strategy_name="sma_crossover",
            account=_account(positions=positions),
            open_orders=[],
            position_owners=owners,
            order_strategy={},
        )
        assert isinstance(result_4, SleeveCapacity)
        assert result_4.positions_open == 4

        # 5 positions — rejected
        positions["SYM4"] = _position("SYM4", 10, 100.0)
        owners["SYM4"] = "sma_crossover"
        result_5 = a.check(
            strategy_name="sma_crossover",
            account=_account(positions=positions),
            open_orders=[],
            position_owners=owners,
            order_strategy={},
        )
        assert isinstance(result_5, SleeveRejection)
        assert result_5.code == SleeveRejectionCode.SLEEVE_MAX_POSITIONS

    def test_max_positions_only_counts_owned_positions(self):
        """RSI positions don't count against SMA's max_positions limit."""
        a = _allocator()
        # 5 RSI positions — should not block SMA
        positions = {f"RSI{i}": _position(f"RSI{i}", 10, 100.0) for i in range(5)}
        owners = {sym: "rsi_reversion" for sym in positions}
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(positions=positions),
            open_orders=[],
            position_owners=owners,
            order_strategy={},
        )
        assert isinstance(result, SleeveCapacity)
        assert result.positions_open == 0

    # ── Rejection — sleeve full ───────────────────────────────────────────────

    def test_rejected_when_sleeve_full(self):
        a = _allocator(min_trade_notional=100.0)
        # Position with market_value exactly at the $40k budget
        pos = Position(
            symbol="AAPL", qty=266,
            avg_entry_price=150.37,
            market_value=40_000.0,
        )
        result = a.check(
            strategy_name="sma_crossover",
            account=_account(positions={"AAPL": pos}),
            open_orders=[],
            position_owners={"AAPL": "sma_crossover"},
            order_strategy={},
        )
        assert isinstance(result, SleeveRejection)
        assert result.code == SleeveRejectionCode.SLEEVE_FULL

    def test_rejected_when_unknown_strategy(self):
        a = _allocator()
        result = a.check(
            strategy_name="unknown_algo",
            account=_account(),
            open_orders=[],
            position_owners={},
            order_strategy={},
        )
        assert isinstance(result, SleeveRejection)
        assert result.code == SleeveRejectionCode.UNKNOWN_STRATEGY
        assert "unknown_algo" in result.message

    def test_rsi_sleeve_unaffected_when_sma_full(self):
        """SMA sleeve full should not block RSI entries."""
        a = _allocator()
        pos = Position(
            symbol="AAPL", qty=266,
            avg_entry_price=150.37,
            market_value=40_000.0,
        )
        # SMA full (budget consumed)
        sma_result = a.check(
            strategy_name="sma_crossover",
            account=_account(positions={"AAPL": pos}),
            open_orders=[],
            position_owners={"AAPL": "sma_crossover"},
            order_strategy={},
        )
        # RSI still has room
        rsi_result = a.check(
            strategy_name="rsi_reversion",
            account=_account(positions={"AAPL": pos}),
            open_orders=[],
            position_owners={"AAPL": "sma_crossover"},
            order_strategy={},
        )
        assert isinstance(sma_result, SleeveRejection)
        assert isinstance(rsi_result, SleeveCapacity)
        assert rsi_result.available            == pytest.approx(40_000.0)
        assert rsi_result.per_position_notional == pytest.approx(8_000.0)


# ── RiskManager.evaluate() notional_cap ──────────────────────────────────────


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
        from risk.manager import Signal, OrderType
        return Signal(
            symbol="AAPL",
            side=Side.BUY,
            strategy_name="sma_crossover",
            reference_price=price,
            atr=atr,
            reason="test",
            order_type=OrderType.MARKET,
        )

    def test_no_cap_sizes_normally(self):
        risk = self._risk()
        account = _account(equity=100_000.0)
        sig = self._signal(price=100.0, atr=2.0)
        # stop_distance=4; risk_dollars=$2k; raw_qty=500
        # max_position_notional_pct=0.10 → $10k → qty=100
        decision = risk.evaluate(sig, account)
        from risk.manager import RiskDecision
        assert isinstance(decision, RiskDecision)
        assert decision.qty == 100

    def test_notional_cap_reduces_qty(self):
        risk = self._risk()
        account = _account(equity=100_000.0)
        sig = self._signal(price=100.0, atr=2.0)
        # Sleeve per-position cap = $8,000 → 80 shares < notional_pct_cap 100
        decision = risk.evaluate(sig, account, notional_cap=8_000.0)
        from risk.manager import RiskDecision
        assert isinstance(decision, RiskDecision)
        assert decision.qty == 80

    def test_notional_cap_larger_than_normal_has_no_effect(self):
        risk = self._risk()
        account = _account(equity=100_000.0)
        sig = self._signal(price=100.0, atr=2.0)
        # cap=$100k — larger than notional_pct_cap $10k → no effect
        decision = risk.evaluate(sig, account, notional_cap=100_000.0)
        from risk.manager import RiskDecision
        assert isinstance(decision, RiskDecision)
        assert decision.qty == 100   # still capped by notional_pct

    def test_notional_cap_so_tight_produces_position_too_small(self):
        risk = self._risk()
        account = _account(equity=100_000.0)
        sig = self._signal(price=500.0, atr=5.0)
        # cap=$100 → cap_qty=0 → POSITION_TOO_SMALL
        from risk.manager import RiskRejection
        decision = risk.evaluate(sig, account, notional_cap=100.0)
        assert isinstance(decision, RiskRejection)


# ── Engine sleeve gating (integration) ───────────────────────────────────────


class TestEngineSleeveGating:
    """
    Verify the engine blocks entries when a strategy's sleeve is exhausted
    (by budget), while leaving the other strategy's sleeve unaffected.

    Note: max-positions blocking is tested separately in TestSleeveCheck.
    The engine integration focuses on the sleeve-full path that arises when
    budget is consumed.
    """

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
                "open":   [c - 0.5 for c in closes],
                "high":   [c + 2.0 for c in closes],
                "low":    [c - 2.0 for c in closes],
                "close":  closes,
                "volume": [1_000_000] * 60,
            },
            index=idx,
        )

    def _make_engine(
        self,
        *,
        strategy_name: str = "sma_crossover",
        sleeve_used: float = 0.0,
        entry_signal: bool = True,
    ):
        from engine.trader import EngineConfig, TradingEngine
        from reporting.alerts import AlertDispatcher
        from reporting.logger import TradeLogger
        from reporting.pnl import PnLTracker

        class _FakeStrategy(BaseStrategy):
            name = strategy_name
            preferred_order_type = OrderType.MARKET

            def _raw_signals(self, df):
                entries = pd.Series([False] * len(df), index=df.index, dtype=bool)
                exits   = pd.Series([False] * len(df), index=df.index, dtype=bool)
                entries.iloc[-1] = entry_signal
                return SignalFrame(entries=entries, exits=exits)

        slot = StrategySlot(
            strategy=_FakeStrategy(),
            symbols=["AAPL"],
        )

        # If sleeve_used > 0, pre-fill the sleeve with a fake position.
        # Use 1 position so we stay under max_positions=5 (count check won't fire).
        existing_positions = {}
        position_owners = {}
        if sleeve_used > 0:
            fake_pos = Position(
                symbol="MSFT",
                qty=1,
                avg_entry_price=sleeve_used,
                market_value=sleeve_used,
            )
            existing_positions = {"MSFT": fake_pos}
            position_owners = {"MSFT": strategy_name}

        fake_broker = MagicMock()
        fake_broker.sync_with_broker.return_value = BrokerSnapshot(
            account=AccountState(
                equity=100_000.0,
                cash=100_000.0,
                session_start_equity=100_000.0,
                open_positions=existing_positions,
            ),
            open_orders=[],
        )
        fake_broker.place_order.return_value = OrderResult(
            symbol="AAPL",
            order_id="ord-001",
            status=OrderStatus.FILLED,
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=159.0,
            raw_status="filled",
            message="ok",
        )
        fake_broker._with_retry.side_effect = lambda fn, **_: fn()
        fake_broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

        allocator = SleeveAllocator(
            allocations={
                strategy_name: {"weight": 0.50, "max_positions": 5},
                "other":        {"weight": 0.50, "max_positions": 5},
            },
            total_gross_pct=0.80,
            min_trade_notional=100.0,
        )

        risk = RiskManager(
            max_position_pct=0.02,
            max_open_positions=10,
            max_gross_exposure_pct=0.80,
            atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.10,
            hard_dollar_loss_cap=10_000.0,
            loss_streak_threshold=10,
            broker_error_threshold=10,
        )

        config = EngineConfig(
            history_lookback_days=60,
            cycle_interval_seconds=300,
            max_bar_age_multiplier=10,
            market_hours_only=False,
        )

        engine = TradingEngine(
            slots=[slot],
            risk=risk,
            broker=fake_broker,
            config=config,
            trade_logger=MagicMock(spec=TradeLogger),
            pnl_tracker=MagicMock(spec=PnLTracker),
            alerts=MagicMock(spec=AlertDispatcher),
            allocator=allocator,
        )
        # Inject pre-existing ownership so allocator sees correct used exposure.
        engine._position_owners = position_owners

        return engine, fake_broker

    def test_entry_approved_when_sleeve_has_room(self):
        engine, broker = self._make_engine(sleeve_used=0.0, entry_signal=True)
        bars = self._bars()
        with patch(
            "engine.trader.fetch_symbol",
            return_value=(bars, SimpleNamespace(api_calls=0)),
        ):
            engine.start(max_cycles=1)
        broker.place_order.assert_called_once()

    def test_entry_blocked_when_sleeve_exhausted(self):
        # Sleeve budget=$40k; pre-fill with $39,990 → available=$10 < $100 min
        # positions_open=1 < max=5, so only the budget check fires
        engine, broker = self._make_engine(sleeve_used=39_990.0, entry_signal=True)
        bars = self._bars()
        with patch(
            "engine.trader.fetch_symbol",
            return_value=(bars, SimpleNamespace(api_calls=0)),
        ):
            engine.start(max_cycles=1)
        broker.place_order.assert_not_called()

    def test_no_entry_signal_sleeve_irrelevant(self):
        engine, broker = self._make_engine(sleeve_used=0.0, entry_signal=False)
        bars = self._bars()
        with patch(
            "engine.trader.fetch_symbol",
            return_value=(bars, SimpleNamespace(api_calls=0)),
        ):
            engine.start(max_cycles=1)
        broker.place_order.assert_not_called()
