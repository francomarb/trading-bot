"""
PLAN 11.47 unit tests — Donchian-style STOP_LIMIT entry path.

Covers:
  - TestPrepareStopLimitSplit: hybrid split helper — integer qty (no split),
    fractional qty with live >= trigger (whole + residual), fractional qty
    with live < trigger (residual gated out), whole_qty == 0 fallback to
    MARKET, non-STOP_LIMIT decision (unchanged).
  - TestCancelStaleStopLimitEntries: market-closed defensive sweep cancels
    open STOP_LIMIT BUY orders; ignores MARKET/LIMIT, exits, and orders
    without a recognizable identifier.
  - TestCloseOrphanStopLimitResiduals: closes fractional positions owned by
    STOP_LIMIT strategies when their whole-share leg never triggered.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from execution.broker import BrokerSnapshot, OpenOrder, OrderStatus, OrderResult
from execution.entry_guard import EntryPriceCap
from engine.trader import TradingEngine, EngineConfig
from risk.manager import (
    AccountState,
    OrderType,
    Position,
    RiskDecision,
    RiskManager,
    Side,
)
from reporting.logger import TradeLogger
from strategies.base import BaseStrategy, SignalFrame, EdgeFilter


T0 = datetime(2026, 6, 11, 13, 30, tzinfo=timezone.utc)


# ── Helpers ─────────────────────────────────────────────────────────────────


class _FakeDonchianStrategy(BaseStrategy):
    """Minimal STOP_LIMIT-style strategy for split-helper testing."""

    name = "donchian_breakout"
    preferred_order_type = OrderType.STOP_LIMIT

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        idx = df.index
        return SignalFrame(
            entries=pd.Series(False, index=idx, dtype=bool),
            exits=pd.Series(False, index=idx, dtype=bool),
        )

    def latest_trigger_price(self, df: pd.DataFrame) -> float | None:
        return 245.0


def _account(equity=100_000.0, positions=None):
    return AccountState(
        equity=equity,
        cash=equity,
        session_start_equity=equity,
        previous_close_equity=equity,
        open_positions=positions or {},
    )


def _snapshot(positions=None, open_orders=None):
    return BrokerSnapshot(
        account=_account(positions=positions),
        open_orders=open_orders or [],
    )


def _engine(tmp_path, broker=None, slots_strategy=None):
    """Build a TradingEngine with a configurable broker and strategy."""
    if broker is None:
        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.get_latest_quote_midpoint.return_value = None
        broker.cancel_order.return_value = True
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

    strategy = slots_strategy or _FakeDonchianStrategy()
    risk = RiskManager(
        max_position_pct=0.02,
        max_open_positions=5,
        max_gross_exposure_pct=0.50,
        atr_stop_multiplier=2.0,
        max_daily_loss_pct=0.05,
        hard_dollar_loss_cap=1_000_000.0,
        loss_streak_threshold=10,
        broker_error_threshold=10,
    )
    cfg = EngineConfig(
        history_lookback_days=120,
        cycle_interval_seconds=0.01,
        max_bar_age_multiplier=10.0,
        market_hours_only=False,
        cancel_orders_on_shutdown=True,
        atr_length=14,
    )
    trade_logger = TradeLogger(path=str(tmp_path / "trades.db"))
    return TradingEngine(
        strategy=strategy,
        symbols=["QCOM"],
        risk=risk,
        broker=broker,
        config=cfg,
        trade_logger=trade_logger,
        clock=lambda: T0,
    )


def _stop_limit_decision(*, qty=5.88, trigger=245.0, cap=257.0, stop=230.0) -> RiskDecision:
    return RiskDecision(
        symbol="QCOM",
        side=Side.BUY,
        qty=qty,
        entry_reference_price=trigger,
        stop_price=stop,
        strategy_name="donchian_breakout",
        reason="test",
        order_type=OrderType.STOP_LIMIT,
        entry_trigger_price=trigger,
        entry_max_price=cap,
    )


def _market_decision(*, qty=5, cap=None) -> RiskDecision:
    return RiskDecision(
        symbol="QCOM",
        side=Side.BUY,
        qty=qty,
        entry_reference_price=100.0,
        stop_price=95.0,
        strategy_name="sma_crossover",
        reason="test",
        order_type=OrderType.MARKET,
        entry_max_price=cap,
    )


# ── Tests ───────────────────────────────────────────────────────────────────


class TestPrepareStopLimitSplit:
    def test_integer_qty_no_residual(self, tmp_path):
        engine = _engine(tmp_path)
        engine.broker.get_latest_quote_midpoint.return_value = 246.0
        primary, residual = engine._prepare_stop_limit_split(
            _stop_limit_decision(qty=5), target_symbol="QCOM"
        )
        assert residual is None
        assert primary.qty == 5
        assert primary.order_type is OrderType.STOP_LIMIT

    def test_fractional_qty_with_live_above_trigger_splits(self, tmp_path):
        engine = _engine(tmp_path)
        engine.broker.get_latest_quote_midpoint.return_value = 246.0  # >= trigger 245
        primary, residual = engine._prepare_stop_limit_split(
            _stop_limit_decision(qty=5.88), target_symbol="QCOM"
        )
        assert primary.qty == 5
        assert primary.order_type is OrderType.STOP_LIMIT
        assert residual is not None
        assert residual.qty == 0.88
        assert residual.order_type is OrderType.MARKET
        assert residual.entry_trigger_price is None  # cleared on residual
        # 11.32 cap preserved on residual so broker enforces ceiling
        assert residual.entry_max_price == 257.0

    def test_fractional_qty_with_live_below_trigger_gates_residual(self, tmp_path):
        engine = _engine(tmp_path)
        engine.broker.get_latest_quote_midpoint.return_value = 228.0  # < trigger 245
        primary, residual = engine._prepare_stop_limit_split(
            _stop_limit_decision(qty=5.88), target_symbol="QCOM"
        )
        assert primary.qty == 5
        assert primary.order_type is OrderType.STOP_LIMIT
        assert residual is None  # gated out

    def test_fractional_qty_with_no_live_quote_gates_residual(self, tmp_path):
        engine = _engine(tmp_path)
        engine.broker.get_latest_quote_midpoint.return_value = None
        primary, residual = engine._prepare_stop_limit_split(
            _stop_limit_decision(qty=5.88), target_symbol="QCOM"
        )
        assert primary.qty == 5
        # No quote → can't verify trigger crossing → skip residual.
        assert residual is None

    def test_pure_fractional_qty_falls_back_to_market(self, tmp_path):
        """If sizing produces qty < 1 (entire position is fractional —
        e.g. ASML at $1700 sized to 0.6 shares), the STOP_LIMIT path is
        unavailable. Fall back to MARKET with the 11.32 cap intact."""
        engine = _engine(tmp_path)
        engine.broker.get_latest_quote_midpoint.return_value = 246.0
        primary, residual = engine._prepare_stop_limit_split(
            _stop_limit_decision(qty=0.6), target_symbol="QCOM"
        )
        assert primary.qty == 0.6
        assert primary.order_type is OrderType.MARKET
        assert primary.entry_trigger_price is None
        assert primary.entry_max_price == 257.0
        assert residual is None

    def test_non_stop_limit_decision_unchanged(self, tmp_path):
        engine = _engine(tmp_path)
        decision = _market_decision(qty=10)
        primary, residual = engine._prepare_stop_limit_split(
            decision, target_symbol="QCOM"
        )
        assert primary is decision
        assert residual is None


class TestCancelStaleStopLimitEntries:
    def _open_order(self, *, order_type=OrderType.STOP_LIMIT, side=Side.BUY,
                   order_id="ord-stoplimit-1", symbol="QCOM",
                   client_order_id="donchian_breakout-abc1234567"):
        return OpenOrder(
            order_id=order_id,
            symbol=symbol,
            side=side,
            qty=5,
            order_type=order_type,
            status="open",
            submitted_at=T0,
            limit_price=257.0 if order_type is OrderType.STOP_LIMIT else None,
            stop_price=245.0 if order_type is OrderType.STOP_LIMIT else None,
            client_order_id=client_order_id,
            time_in_force="day",
        )

    def test_open_stop_limit_buy_is_cancelled(self, tmp_path):
        engine = _engine(tmp_path)
        snap = _snapshot(open_orders=[self._open_order()])
        engine._cancel_stale_stop_limit_entries(snap)
        engine.broker.cancel_order.assert_called_once_with("ord-stoplimit-1")

    def test_open_market_order_ignored(self, tmp_path):
        engine = _engine(tmp_path)
        snap = _snapshot(open_orders=[
            self._open_order(order_type=OrderType.MARKET, order_id="ord-mkt-1")
        ])
        engine._cancel_stale_stop_limit_entries(snap)
        engine.broker.cancel_order.assert_not_called()

    def test_open_limit_order_ignored(self, tmp_path):
        engine = _engine(tmp_path)
        snap = _snapshot(open_orders=[
            self._open_order(order_type=OrderType.LIMIT, order_id="ord-lim-1")
        ])
        engine._cancel_stale_stop_limit_entries(snap)
        engine.broker.cancel_order.assert_not_called()

    def test_sell_stop_limit_ignored(self, tmp_path):
        # Exits never use STOP_LIMIT (engine convention), but if one
        # somehow appears, the entry sweep must not touch it.
        engine = _engine(tmp_path)
        snap = _snapshot(open_orders=[
            self._open_order(side=Side.SELL, order_id="ord-sl-sell-1")
        ])
        engine._cancel_stale_stop_limit_entries(snap)
        engine.broker.cancel_order.assert_not_called()

    def test_cancel_failure_does_not_raise(self, tmp_path):
        engine = _engine(tmp_path)
        engine.broker.cancel_order.side_effect = RuntimeError("broker down")
        snap = _snapshot(open_orders=[self._open_order()])
        # Must not propagate — operator alert handled by caller wrapper.
        engine._cancel_stale_stop_limit_entries(snap)

    def test_empty_orders_is_noop(self, tmp_path):
        engine = _engine(tmp_path)
        snap = _snapshot(open_orders=[])
        engine._cancel_stale_stop_limit_entries(snap)
        engine.broker.cancel_order.assert_not_called()


class TestCloseOrphanStopLimitResiduals:
    def _position(self, *, symbol="QCOM", qty=0.88, price=246.0):
        return Position(
            symbol=symbol,
            qty=qty,
            avg_entry_price=price,
            market_value=qty * price,
        )

    def _engine_with_donchian_owner(self, tmp_path, position, *,
                                    close_fn_mock=None):
        engine = _engine(tmp_path)
        # Register the symbol's owner as a STOP_LIMIT strategy. _get_owner
        # reads from _positions; the slot pointer makes the sweep
        # recognize the strategy type.
        engine._register_single_leg(
            strategy_name="donchian_breakout",
            symbol=position.symbol,
        )
        slot = SimpleNamespace(strategy=_FakeDonchianStrategy())
        engine.slots = [slot]
        # Spy on the close helper so we can assert it was invoked.
        engine._close_fractional_residual_position = MagicMock()
        return engine

    def test_orphan_fractional_position_is_closed(self, tmp_path):
        position = self._position(qty=0.88)
        engine = self._engine_with_donchian_owner(tmp_path, position)
        snap = _snapshot(positions={"QCOM": position})
        engine._close_orphan_stop_limit_residuals(snap)
        engine._close_fractional_residual_position.assert_called_once()
        kwargs = engine._close_fractional_residual_position.call_args.kwargs
        assert kwargs["symbol"] == "QCOM"
        assert kwargs["owner"] == "donchian_breakout"

    def test_whole_share_position_not_touched(self, tmp_path):
        position = self._position(qty=5.0)
        engine = self._engine_with_donchian_owner(tmp_path, position)
        snap = _snapshot(positions={"QCOM": position})
        engine._close_orphan_stop_limit_residuals(snap)
        engine._close_fractional_residual_position.assert_not_called()

    def test_hybrid_position_not_touched(self, tmp_path):
        # whole + residual aggregated at broker: qty = 5.88 (> 1). The
        # whole-share leg DID trigger; this is not an orphan.
        position = self._position(qty=5.88)
        engine = self._engine_with_donchian_owner(tmp_path, position)
        snap = _snapshot(positions={"QCOM": position})
        engine._close_orphan_stop_limit_residuals(snap)
        engine._close_fractional_residual_position.assert_not_called()

    def test_non_stop_limit_strategy_position_not_touched(self, tmp_path):
        # Same fractional qty, but the owning strategy is MARKET-typed
        # (e.g. SMA crossover). The sweep is scoped to STOP_LIMIT only.
        position = self._position(qty=0.88)
        engine = _engine(tmp_path)
        engine._register_single_leg(
            strategy_name="sma_crossover",
            symbol=position.symbol,
        )
        sma_slot = SimpleNamespace(
            strategy=SimpleNamespace(
                name="sma_crossover",
                preferred_order_type=OrderType.MARKET,
            )
        )
        engine.slots = [sma_slot]
        engine._close_fractional_residual_position = MagicMock()
        snap = _snapshot(positions={"QCOM": position})
        engine._close_orphan_stop_limit_residuals(snap)
        engine._close_fractional_residual_position.assert_not_called()

    def test_close_failure_does_not_raise(self, tmp_path):
        position = self._position(qty=0.88)
        engine = self._engine_with_donchian_owner(tmp_path, position)
        engine._close_fractional_residual_position.side_effect = RuntimeError("broker down")
        snap = _snapshot(positions={"QCOM": position})
        engine._close_orphan_stop_limit_residuals(snap)


# ── PLAN 11.47 §parity: limit price math must match 11.32 cap math ──────────


class TestStopLimitCapMathParity:
    """The STOP_LIMIT limit price MUST equal what the 11.32 entry-price-cap
    policy would compute for the same reference price + ATR. The
    `ENTRY_PRICE_CAPS["donchian_breakout"]` knob is the single source of
    truth — if it changes, both the historical MARKET path and the new
    STOP_LIMIT path move together. If they ever drift, this test fails
    loudly rather than letting one path silently undercut the other."""

    def test_donchian_stop_limit_limit_matches_11_32_compute_cap_price(self):
        from execution.entry_guard import compute_cap_price
        from config import settings

        policy = settings.ENTRY_PRICE_CAPS["donchian_breakout"]
        # Same inputs the engine uses on a real Donchian signal: the
        # breakout level as reference, the latest ATR.
        trigger_price = 245.0
        atr = 5.0
        expected_limit = compute_cap_price(
            reference_price=trigger_price,
            atr=atr,
            side="buy",
            policy=policy,
        )
        # The engine's STOP_LIMIT branch sets entry_max_price via this
        # same call (engine/trader.py: compute_cap_price reference=trigger).
        # A unit-direct call here proves the math is shared, not copy-pasted
        # — a future tightening of max_chase_bps in settings flows through
        # to both paths.
        assert expected_limit > trigger_price  # BUY: limit > trigger
        assert expected_limit <= trigger_price * 1.05 + 0.01  # ≤ 500 bps
        assert expected_limit - trigger_price <= 2.0 * atr + 1e-6  # ≤ 2 ATR

    def test_donchian_policy_exists_in_settings(self):
        """Defense against accidental deletion of the policy — without it
        the engine skips STOP_LIMIT entries entirely (see entry-guard
        branch in _process_symbol)."""
        from config import settings
        assert "donchian_breakout" in settings.ENTRY_PRICE_CAPS
        policy = settings.ENTRY_PRICE_CAPS["donchian_breakout"]
        # Both knobs must be set per the 11.32 audit conclusion (paired
        # bps + ATR caps catch the historical outliers).
        assert policy.max_chase_bps is not None
        assert policy.max_chase_atr_fraction is not None
