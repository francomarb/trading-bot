"""
Unit tests for the engine's credit-spread wiring (PLAN.md 11.29 PR 3b):
the entry path (_enter_multi_leg), the async fill drain
(_drain_spread_fills), and the exit path (_process_credit_spread_exits).

The broker is a MagicMock; the strategy is a real CreditSpread with stubbed
quote/IV lookups, and find_best_put_spread is patched where needed.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from engine.trader import EngineConfig, TradingEngine
from execution.broker import BrokerSnapshot, OrderResult, OrderStatus
from regime.detector import MarketRegime
from reporting.logger import TradeLogger
from risk.manager import AccountState, RiskManager
from strategies.credit_spread import CreditSpread, CreditSpreadConfig, OpenSpread
from utils.iv_proxy import IVProxyResolver
from utils.options_lookup import SpreadPick
from utils.options_ranker import Quote


_RAW_SPY = {
    "short_leg_delta": 0.17, "spread_width": 10, "dte_min": 30, "dte_max": 45,
    "iv_proxy_source": "vix", "min_iv_proxy": 14, "min_credit_pct_of_width": 0.13,
    "max_concurrent_positions": 3, "max_per_expiration": 1,
    "min_dte_gap_between_opens": 7, "profit_target_pct": 0.50,
    "stop_loss_multiple": 2.0, "time_stop_dte": 21,
    "exit_on_short_strike_breach": True, "limit_timeout_seconds": 30,
    "earnings_blackout_days": 0,
}

_EXP = date.today() + timedelta(days=37)


def _config(**overrides) -> CreditSpreadConfig:
    return CreditSpreadConfig.from_dict("SPY", {**_RAW_SPY, **overrides})


def _pick(net_credit: float = 1.45) -> SpreadPick:
    return SpreadPick(
        short_occ="SPY260618P00568000",
        long_occ="SPY260618P00558000",
        short_strike=568.0,
        long_strike=558.0,
        expiration_date=_EXP,
        width=10.0,
        net_credit=net_credit,
        max_loss=(10.0 - net_credit) * 100,
        short_leg_delta=0.17,
        score=0.7,
        components={"short_delta": 1.0, "net_credit": 0.15,
                    "spread_quality": 0.8, "dte": 0.9},
        runners_up=[],
    )


def _open_spread(position_id: str = "p1", net_credit: float = 1.45) -> OpenSpread:
    return OpenSpread(
        position_id=position_id,
        short_occ="SPY260618P00568000",
        long_occ="SPY260618P00558000",
        short_strike=568.0,
        long_strike=558.0,
        expiration_date=_EXP,
        net_credit=net_credit,
        width=10.0,
        qty=1,
    )


def _strategy(config: CreditSpreadConfig | None = None, *, iv_points: float = 18.0,
              quote_lookup=None) -> CreditSpread:
    return CreditSpread(
        config or _config(),
        iv_resolver=IVProxyResolver(fetch_fn=lambda t: iv_points),
        quote_lookup=quote_lookup or (lambda occs: {o: None for o in occs}),
    )


def _engine(tmp_path, strategy: CreditSpread) -> tuple[TradingEngine, MagicMock]:
    broker = MagicMock()
    broker.sync_with_broker.return_value = SimpleNamespace(
        account=SimpleNamespace(open_positions={}, equity=100_000.0),
        open_orders=[],
    )
    risk = RiskManager(
        max_position_pct=0.02, max_open_positions=5, max_gross_exposure_pct=0.50,
        atr_stop_multiplier=2.0, max_daily_loss_pct=0.05,
        hard_dollar_loss_cap=1_000_000.0, loss_streak_threshold=10,
        broker_error_threshold=10,
    )
    tl = TradeLogger(path=str(tmp_path / "trades.db"))
    engine = TradingEngine(
        strategy=strategy,
        symbols=["SPY"],
        risk=risk,
        broker=broker,
        trade_logger=tl,
        config=EngineConfig(
            history_lookback_days=120, cycle_interval_seconds=0.01,
            max_bar_age_multiplier=10.0, market_hours_only=False,
        ),
    )
    return engine, broker


_SIGNAL_KEY = ("credit_spread", "SPY", "1Day")
_SIGNAL_BAR = "2026-05-14"


# ── Entry path ──────────────────────────────────────────────────────────────


class TestEnterCreditSpread:
    def test_existing_global_halt_blocks_mleg_dispatch(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        engine.risk.record_broker_error()
        engine.risk.record_broker_error()
        for _ in range(8):
            engine.risk.record_broker_error()
        assert engine.risk.is_halted()

        with patch("strategies.credit_spread.find_best_put_spread") as picker:
            engine._enter_multi_leg(
                strategy=strategy,
                symbol="SPY",
                underlying_close=745.0,
                notional_cap=2_000.0,
                signal_key=_SIGNAL_KEY,
                signal_bar=_SIGNAL_BAR,
                strategy_statuses={},
                strategy_reasons={},
            )

        picker.assert_not_called()
        broker.dispatch_spread_order.assert_not_called()

    def test_halt_during_plan_build_blocks_mleg_dispatch(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)

        def _pick_and_halt(*args, **kwargs):
            for _ in range(10):
                engine.risk.record_broker_error()
            return _pick()

        with patch(
            "strategies.credit_spread.find_best_put_spread",
            side_effect=_pick_and_halt,
        ):
            engine._enter_multi_leg(
                strategy=strategy,
                symbol="SPY",
                underlying_close=745.0,
                notional_cap=2_000.0,
                signal_key=_SIGNAL_KEY,
                signal_bar=_SIGNAL_BAR,
                strategy_statuses={},
                strategy_reasons={},
            )

        assert engine.risk.is_halted()
        broker.dispatch_spread_order.assert_not_called()

    def test_happy_path_dispatches_and_pre_registers(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="spread-worker-1", symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
            raw_status="accepted", message="",
        )
        with patch("strategies.credit_spread.find_best_put_spread", return_value=_pick()):
            engine._enter_multi_leg(
                strategy=strategy, symbol="SPY", underlying_close=745.0,
                notional_cap=2_000.0, signal_key=_SIGNAL_KEY, signal_bar=_SIGNAL_BAR,
                strategy_statuses={}, strategy_reasons={},
            )

        broker.dispatch_spread_order.assert_called_once()
        kw = broker.dispatch_spread_order.call_args.kwargs
        # The open path leaves `closing` at its default (False).
        assert kw.get("closing", False) is False
        assert kw["limit_price"] == pytest.approx(-1.45)  # negative = credit
        # A spread Position is pre-registered and the strategy's view updated.
        assert len(engine._positions) == 1
        pos = next(iter(engine._positions.values()))
        assert pos.is_spread and pos.strategy_name == "credit_spread"
        assert len(strategy.open_spreads) == 1
        assert kw["position_id"] in engine._spread_owner_strategy
        assert kw["position_id"] in engine._pending_spread_plans

    def test_rejected_entry_does_not_dispatch_or_register(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        with patch("strategies.credit_spread.find_best_put_spread", return_value=None):
            engine._enter_multi_leg(
                strategy=strategy, symbol="SPY", underlying_close=745.0,
                notional_cap=2_000.0, signal_key=_SIGNAL_KEY, signal_bar=_SIGNAL_BAR,
                strategy_statuses={}, strategy_reasons={},
            )
        broker.dispatch_spread_order.assert_not_called()
        assert engine._positions == {}
        assert strategy.open_spreads == []

    def test_no_notional_cap_skips_entry(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        engine._enter_multi_leg(
            strategy=strategy, symbol="SPY", underlying_close=745.0,
            notional_cap=None, signal_key=_SIGNAL_KEY, signal_bar=_SIGNAL_BAR,
            strategy_statuses={}, strategy_reasons={},
        )
        broker.dispatch_spread_order.assert_not_called()
        assert engine._positions == {}

    def test_non_accepted_dispatch_does_not_pre_register(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.REJECTED, order_id=None, symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=None,
            raw_status="rejected", message="rejected",
        )
        with patch("strategies.credit_spread.find_best_put_spread", return_value=_pick()):
            engine._enter_multi_leg(
                strategy=strategy, symbol="SPY", underlying_close=745.0,
                notional_cap=2_000.0, signal_key=_SIGNAL_KEY, signal_bar=_SIGNAL_BAR,
                strategy_statuses={}, strategy_reasons={},
            )
        assert engine._positions == {}
        assert strategy.open_spreads == []

    def test_global_cap_passed_through_to_strategy(self, tmp_path):
        # 8 open credit spreads already → build_spread_execution should reject
        # on the global cap before the picker runs.
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        # Pre-load 8 spread positions onto the engine.
        from engine.positions import make_spread, PositionLeg
        for i in range(8):
            pid = f"glob-{i}"
            engine._positions[pid] = make_spread(
                strategy_name="credit_spread", position_id=pid,
                legs=[PositionLeg("A", -1, side="SELL"), PositionLeg("B", 1, side="BUY")],
            )
        with patch("strategies.credit_spread.find_best_put_spread") as picker:
            engine._enter_multi_leg(
                strategy=strategy, symbol="SPY", underlying_close=745.0,
                notional_cap=2_000.0, signal_key=_SIGNAL_KEY, signal_bar=_SIGNAL_BAR,
                strategy_statuses={}, strategy_reasons={},
            )
            picker.assert_not_called()  # global cap rejected before the chain query
        broker.dispatch_spread_order.assert_not_called()


# ── Fill drain ──────────────────────────────────────────────────────────────


class TestDrainSpreadFills:
    def _pre_register(self, engine, strategy, position_id="p1"):
        from engine.positions import make_spread, PositionLeg
        engine._positions[position_id] = make_spread(
            strategy_name="credit_spread", position_id=position_id,
            legs=[PositionLeg("SPY260618P00568000", -1, side="SELL"),
                  PositionLeg("SPY260618P00558000", 1, side="BUY")],
        )
        engine._spread_owner_strategy[position_id] = strategy
        engine._pending_spread_plans[position_id] = _pick()
        strategy.register_spread(_open_spread(position_id))

    def test_open_filled_keeps_position_and_logs(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", False, "filled", 1.0, -1.50, "combo-1", -1.45),
        ]
        engine._drain_spread_fills()
        # Position stays; the plan is consumed.
        assert "p1" in engine._positions
        assert "p1" not in engine._pending_spread_plans
        assert len(strategy.open_spreads) == 1
        # Logged to the trade DB as a spread entry — one row per leg, both
        # keyed by the same position_id (needed for restart reconstruction).
        rows = engine.trade_logger.read_all()
        assert len(rows) == 2
        assert all(r["position_type"] == "spread" for r in rows)
        assert all(r["position_id"] == "p1" for r in rows)
        # Short leg sold to open, long leg bought to open.
        assert {r["side"] for r in rows} == {"sell", "buy"}

    def test_open_filled_logs_slippage_vs_submitted_credit(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", False, "filled", 1.0, -1.50, "combo-1", -1.45),
        ]
        engine._drain_spread_fills()

        short_row = [
            r for r in engine.trade_logger.read_all()
            if r["symbol"] == "SPY260618P00568000"
        ][0]
        assert short_row["entry_reference_price"] == pytest.approx(1.45)
        assert short_row["realized_slippage_bps"] == pytest.approx(-344.83)

    def test_open_canceled_rolls_back(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", False, "canceled", 0.0, None, "combo-1", -1.45),
        ]
        engine._drain_spread_fills()
        assert "p1" not in engine._positions
        assert "p1" not in engine._spread_owner_strategy
        assert strategy.open_spreads == []

    def test_close_filled_drops_position_and_logs(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        engine._spreads_pending_close.add("p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", True, "filled", 1.0, 0.60, "combo-close-1", 0.60),
        ]
        engine._drain_spread_fills()
        assert "p1" not in engine._positions
        assert "p1" not in engine._spread_owner_strategy
        assert "p1" not in engine._spreads_pending_close
        assert strategy.open_spreads == []
        # The close writes one row per leg, both position_type='spread'.
        rows = engine.trade_logger.read_all()
        close_rows = [r for r in rows if r["reason"] == "spread exit"]
        assert len(close_rows) == 2
        assert all(r["position_type"] == "spread" for r in close_rows)
        # Short leg bought back to close, long leg sold.
        assert {r["side"] for r in close_rows} == {"buy", "sell"}

    def test_close_filled_records_realized_pnl_to_allocator(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        # _open_spread default net_credit = 1.45.
        self._pre_register(engine, strategy, "p1")
        engine._allocator = MagicMock()
        engine._spreads_pending_close.add("p1")
        # Closed at a $0.60 debit → realized = (1.45 − 0.60) × 1 × 100 = 85.0.
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", True, "filled", 1.0, 0.60, "combo-close-1", 0.60),
        ]
        engine._drain_spread_fills()

        engine._allocator.record_realized_pnl.assert_called_once()
        name, pnl = engine._allocator.record_realized_pnl.call_args.args
        assert name == "credit_spread"
        assert pnl == pytest.approx(85.0)
        # Persisted on the close row so it survives a restart.
        close_rows = [
            r for r in engine.trade_logger.read_all()
            if r["reason"] == "spread exit" and r["realized_pnl"] is not None
        ]
        assert len(close_rows) == 1
        assert close_rows[0]["realized_pnl"] == pytest.approx(85.0)

    def test_close_filled_logs_slippage_vs_submitted_debit(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        engine._spreads_pending_close.add("p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", True, "filled", 1.0, 0.63, "combo-close-1", 0.60),
        ]
        engine._drain_spread_fills()

        close_rows = [
            r for r in engine.trade_logger.read_all()
            if r["reason"] == "spread exit" and r["avg_fill_price"] > 0
        ]
        assert len(close_rows) == 1
        assert close_rows[0]["entry_reference_price"] == pytest.approx(0.60)
        assert close_rows[0]["realized_slippage_bps"] == pytest.approx(500.0)

    def test_close_filled_with_no_allocator_still_logs_pnl(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        engine._allocator = None  # no allocator wired
        engine._spreads_pending_close.add("p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", True, "filled", 1.0, 0.60, "combo-close-1", 0.60),
        ]
        engine._drain_spread_fills()  # must not raise
        close_rows = [
            r for r in engine.trade_logger.read_all()
            if r["reason"] == "spread exit" and r["realized_pnl"] is not None
        ]
        assert close_rows[0]["realized_pnl"] == pytest.approx(85.0)

    def test_close_filled_with_no_fill_price_leaves_pnl_unset(self, tmp_path):
        # Stream said "filled" but the REST follow-up to fetch the combo fill
        # price failed → avg_fill_price=None reaches the drain. The position
        # must still close, but P&L must NOT be fabricated (0.0 debit would
        # record a bogus full-credit winner into the HWM gate).
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        engine._allocator = MagicMock()
        engine._spreads_pending_close.add("p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", True, "filled", 1.0, None, "combo-close-1", 0.60),
        ]
        engine._drain_spread_fills()

        # Position released — the spread genuinely closed.
        assert "p1" not in engine._positions
        assert strategy.open_spreads == []
        assert "p1" not in engine._spreads_pending_close
        # But NO bogus P&L recorded.
        engine._allocator.record_realized_pnl.assert_not_called()
        close_rows = [
            r for r in engine.trade_logger.read_all()
            if r["position_type"] == "spread" and r["exit_timestamp"] is not None
        ]
        assert close_rows  # the close was still logged
        assert all(r["realized_pnl"] is None for r in close_rows)

    def test_partial_close_preserves_position_and_fires_alert(self, tmp_path):
        """PR #56 R5: a partial-quantity spread close (close_qty <
        open_qty) must NOT release the spread. State stays intact for
        the residual fill event to handle; operator is alerted via
        broker_error so they can reconcile manually.
        """
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        # Pre-register a 2-contract spread.
        from engine.positions import make_spread, PositionLeg
        engine._positions["p1"] = make_spread(
            strategy_name="credit_spread", position_id="p1",
            legs=[PositionLeg("SPY260618P00568000", -2, side="SELL"),
                  PositionLeg("SPY260618P00558000", 2, side="BUY")],
        )
        engine._spread_owner_strategy["p1"] = strategy
        engine._pending_spread_plans["p1"] = _pick()
        # Register the open spread at qty=2.
        strategy.register_spread(OpenSpread(
            position_id="p1", short_occ="SPY260618P00568000",
            long_occ="SPY260618P00558000", short_strike=568.0,
            long_strike=558.0, expiration_date=_EXP,
            net_credit=1.45, width=10.0, qty=2,
        ))
        engine._spreads_pending_close.add("p1")
        engine.alerts = MagicMock()
        engine._allocator = MagicMock()
        # Drain a CLOSE event reporting only 1 of 2 contracts filled.
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", True, "filled", 1.0, 0.80, "combo-close-partial", 0.60),
        ]

        engine._drain_spread_fills()

        # Position MUST stay open in the engine.
        assert "p1" in engine._positions
        assert "p1" in engine._spread_owner_strategy
        # The strategy must still hold the spread (state preserved).
        assert len(strategy.open_spreads) == 1
        assert strategy.open_spreads[0].position_id == "p1"
        # PR #56 R6: position MUST stay in _spreads_pending_close so the
        # next cycle's _process_credit_spread_exits does NOT dispatch a
        # duplicate close order at the original full qty. The original
        # partial-fill order may still be working at the broker (the worker
        # exits on partially_filled per options_executor.py:275); a fresh
        # close dispatch would risk over-closing.
        assert "p1" in engine._spreads_pending_close, (
            "partial close must keep the position pending to prevent a "
            "duplicate close dispatch on the next cycle"
        )
        # Allocator was called with is_full_close=False — partial P&L
        # contributes to dollar math but does NOT increment trade_count.
        engine._allocator.record_realized_pnl.assert_called_once()
        kwargs = engine._allocator.record_realized_pnl.call_args.kwargs
        assert kwargs["position_uid"] == "p1"
        assert kwargs["is_full_close"] is False
        # Operator alerted via broker_error.
        engine.alerts.broker_error.assert_called_once()
        alert_msg = engine.alerts.broker_error.call_args.args[0]
        assert "partial close" in alert_msg
        assert "p1" in alert_msg
        # The partial fill row was logged with status='partial'.
        partial_rows = [
            r for r in engine.trade_logger.read_all()
            if r["position_type"] == "spread" and r["status"] == "partial"
        ]
        assert len(partial_rows) == 2  # one row per leg

    def test_partial_close_pending_state_blocks_next_cycle_dispatch(self, tmp_path):
        """PR #56 R6: the operational guarantee of the previous test.

        After a partial-fill drain leaves the position in
        _spreads_pending_close, _process_credit_spread_exits MUST skip
        the position on the next cycle — even though the strategy
        would otherwise want to close it. Without this, the engine
        would dispatch a duplicate close at the original qty while
        the first order may still be residual at the broker.
        """
        from datetime import date
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        # Pre-register a 2-contract spread and put it in pending-close
        # (simulating the state right after the R6 partial branch fires).
        from engine.positions import make_spread, PositionLeg
        engine._positions["p1"] = make_spread(
            strategy_name="credit_spread", position_id="p1",
            legs=[PositionLeg("SPY260618P00568000", -2, side="SELL"),
                  PositionLeg("SPY260618P00558000", 2, side="BUY")],
        )
        engine._spread_owner_strategy["p1"] = strategy
        strategy.register_spread(OpenSpread(
            position_id="p1", short_occ="SPY260618P00568000",
            long_occ="SPY260618P00558000", short_strike=568.0,
            long_strike=558.0, expiration_date=_EXP,
            net_credit=1.45, width=10.0, qty=2,
        ))
        # Critical: pending-close re-armed by R6.
        engine._spreads_pending_close.add("p1")
        # The broker dispatch must NOT be called this cycle.
        broker.dispatch_spread_order.reset_mock()

        # Run the close-exit evaluation directly (the path the cycle
        # would take). Even if the strategy says "yes close," the
        # pending guard must short-circuit BEFORE dispatch.
        engine._process_credit_spread_exits(
            strategy=strategy,
            underlying=strategy.symbol,
            underlying_close=1.0,  # well below short strike -> would force exit
            current_regime=None,
        )

        # No new dispatch — the position is still pending the residual.
        broker.dispatch_spread_order.assert_not_called()
        # And it's still pending.
        assert "p1" in engine._spreads_pending_close

    def test_close_canceled_keeps_position_for_retry(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        engine._spreads_pending_close.add("p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", True, "canceled", 0.0, None, "combo-close-1", 0.60),
        ]
        engine._drain_spread_fills()
        # Position stays open; pending-close flag cleared so the exit path retries.
        assert "p1" in engine._positions
        assert "p1" not in engine._spreads_pending_close
        assert len(strategy.open_spreads) == 1


# ── Exit path ───────────────────────────────────────────────────────────────


class TestProcessCreditSpreadExits:
    def _wire_open_spread(self, engine, strategy, position_id="p1"):
        from engine.positions import make_spread, PositionLeg
        engine._positions[position_id] = make_spread(
            strategy_name="credit_spread", position_id=position_id,
            legs=[PositionLeg("SPY260618P00568000", -1, side="SELL"),
                  PositionLeg("SPY260618P00558000", 1, side="BUY")],
        )
        engine._spread_owner_strategy[position_id] = strategy
        strategy.register_spread(_open_spread(position_id, net_credit=2.00))

    def test_exit_trigger_dispatches_closing_combo(self, tmp_path):
        # spread mid 1.00 = 50% of the 2.00 credit → profit target.
        quote_lookup = lambda occs: {
            "SPY260618P00568000": Quote(1.45, 1.55),
            "SPY260618P00558000": Quote(0.45, 0.55),
        }
        strategy = _strategy(_config(profit_target_pct=0.50), quote_lookup=quote_lookup)
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="spread-worker-c", symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
            raw_status="accepted", message="",
        )
        engine._process_credit_spread_exits(
            strategy=strategy, underlying="SPY", underlying_close=745.0,
        )
        broker.dispatch_spread_order.assert_called_once()
        kw = broker.dispatch_spread_order.call_args.kwargs
        assert kw["closing"] is True
        assert kw["position_id"] == "p1"
        assert kw["limit_price"] > 0  # positive net debit to close
        assert "p1" in engine._spreads_pending_close

    def test_no_trigger_does_not_dispatch(self, tmp_path):
        # spread mid 1.80 — no exit trigger fires.
        quote_lookup = lambda occs: {
            "SPY260618P00568000": Quote(2.45, 2.55),
            "SPY260618P00558000": Quote(0.65, 0.75),
        }
        strategy = _strategy(quote_lookup=quote_lookup)
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        engine._process_credit_spread_exits(
            strategy=strategy, underlying="SPY", underlying_close=745.0,
        )
        broker.dispatch_spread_order.assert_not_called()
        assert engine._spreads_pending_close == set()

    def test_pending_close_position_is_skipped(self, tmp_path):
        quote_lookup = lambda occs: {
            "SPY260618P00568000": Quote(1.45, 1.55),
            "SPY260618P00558000": Quote(0.45, 0.55),
        }
        strategy = _strategy(_config(profit_target_pct=0.50), quote_lookup=quote_lookup)
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        engine._spreads_pending_close.add("p1")  # close already in flight
        engine._process_credit_spread_exits(
            strategy=strategy, underlying="SPY", underlying_close=745.0,
        )
        broker.dispatch_spread_order.assert_not_called()  # not double-submitted

    def test_bear_regime_triggers_defensive_close(self, tmp_path):
        # Quotes that would NOT fire any normal trigger — only the BEAR
        # override should cause an exit.
        quote_lookup = lambda occs: {
            "SPY260618P00568000": Quote(2.45, 2.55),
            "SPY260618P00558000": Quote(0.65, 0.75),
        }
        strategy = _strategy(quote_lookup=quote_lookup)
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="spread-bear-close", symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
            raw_status="accepted", message="",
        )
        engine._process_credit_spread_exits(
            strategy=strategy, underlying="SPY", underlying_close=745.0,
            current_regime=MarketRegime.BEAR,
        )
        broker.dispatch_spread_order.assert_called_once()
        kw = broker.dispatch_spread_order.call_args.kwargs
        assert kw["closing"] is True
        assert kw["position_id"] == "p1"
        # No quote → falls back to spread width as a marketable debit.
        assert kw["limit_price"] == 10.0
        assert "p1" in engine._spreads_pending_close

    def test_bear_regime_skips_already_pending_close(self, tmp_path):
        # BEAR override must still respect _spreads_pending_close so the
        # defensive exit cannot double-submit alongside an in-flight close.
        strategy = _strategy(quote_lookup=lambda occs: {o: None for o in occs})
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        engine._spreads_pending_close.add("p1")
        engine._process_credit_spread_exits(
            strategy=strategy, underlying="SPY", underlying_close=745.0,
            current_regime=MarketRegime.BEAR,
        )
        broker.dispatch_spread_order.assert_not_called()

    def test_bear_regime_exit_survives_quote_outage(self, tmp_path):
        # quote_lookup raises — the normal exit path would swallow this and
        # hold the position. BEAR override must still close.
        def raising_lookup(occs):
            raise RuntimeError("OPRA outage")
        strategy = _strategy(quote_lookup=raising_lookup)
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="spread-bear-outage", symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
            raw_status="accepted", message="",
        )
        engine._process_credit_spread_exits(
            strategy=strategy, underlying="SPY", underlying_close=745.0,
            current_regime=MarketRegime.BEAR,
        )
        broker.dispatch_spread_order.assert_called_once()
        assert "p1" in engine._spreads_pending_close

    def test_non_bear_regime_falls_through_to_normal_exit_logic(self, tmp_path):
        # Spread mid 1.80 — no normal trigger fires. Confirms the regime
        # parameter is inert for non-BEAR values.
        quote_lookup = lambda occs: {
            "SPY260618P00568000": Quote(2.45, 2.55),
            "SPY260618P00558000": Quote(0.65, 0.75),
        }
        strategy = _strategy(quote_lookup=quote_lookup)
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        for regime in (MarketRegime.TRENDING, MarketRegime.RANGING,
                       MarketRegime.VOLATILE, None):
            broker.dispatch_spread_order.reset_mock()
            engine._spreads_pending_close.clear()
            engine._process_credit_spread_exits(
                strategy=strategy, underlying="SPY", underlying_close=745.0,
                current_regime=regime,
            )
            broker.dispatch_spread_order.assert_not_called()

    def test_same_daily_bar_still_retries_exit_after_canceled_close(
        self,
        tmp_path,
        monkeypatch,
    ):
        now = datetime(2026, 5, 26, 18, 30, tzinfo=timezone.utc)
        idx = pd.date_range(end=now - timedelta(minutes=5), periods=60, freq="1D")
        bars = pd.DataFrame(
            {
                "open": [745.0] * len(idx),
                "high": [750.0] * len(idx),
                "low": [740.0] * len(idx),
                "close": [745.0] * len(idx),
                "volume": [1_000_000] * len(idx),
            },
            index=idx,
        )
        monkeypatch.setattr(
            "engine.trader.fetch_symbol",
            lambda *args, **kwargs: (bars, SimpleNamespace(api_calls=0)),
        )

        quote_lookup = lambda occs: {
            "SPY260618P00568000": Quote(1.45, 1.55),
            "SPY260618P00558000": Quote(0.45, 0.55),
        }
        strategy = _strategy(_config(profit_target_pct=0.50), quote_lookup=quote_lookup)
        engine, broker = _engine(tmp_path, strategy)
        engine._clock = lambda: now
        self._wire_open_spread(engine, strategy, "p1")
        engine._processed_signal_bars[("credit_spread", "SPY", "1Day")] = pd.Timestamp(
            bars.index[-1]
        )
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED,
            order_id="spread-worker-retry",
            symbol="SPY",
            requested_qty=1,
            filled_qty=0.0,
            avg_fill_price=0.0,
            raw_status="accepted",
            message="",
        )
        snapshot = BrokerSnapshot(
            account=AccountState(
                equity=100_000.0,
                cash=100_000.0,
                session_start_equity=100_000.0,
                open_positions={},
            ),
            open_orders=[],
        )

        engine._process_symbol(
            "SPY",
            snapshot,
            snapshot.account,
            strategy,
            "1Day",
            market_open=True,
        )

        broker.dispatch_spread_order.assert_called_once()
        assert broker.dispatch_spread_order.call_args.kwargs["closing"] is True
        assert broker.dispatch_spread_order.call_args.kwargs["position_id"] == "p1"
        assert "p1" in engine._spreads_pending_close


# ── BEAR cycle-level sweep ──────────────────────────────────────────────────


class TestSweepBearSpreadExits:
    """The cycle-level BEAR sweep runs before _process_symbol, so the
    defensive override is not gated by per-symbol bar fetches, freshness
    checks, or empty decision frames."""

    def _wire_open_spread(self, engine, strategy, position_id="p1"):
        from engine.positions import make_spread, PositionLeg
        engine._positions[position_id] = make_spread(
            strategy_name="credit_spread", position_id=position_id,
            legs=[PositionLeg("SPY260618P00568000", -1, side="SELL"),
                  PositionLeg("SPY260618P00558000", 1, side="BUY")],
        )
        engine._spread_owner_strategy[position_id] = strategy
        strategy.register_spread(_open_spread(position_id, net_credit=2.00))

    def test_sweep_dispatches_close_for_open_spread(self, tmp_path):
        # Quote lookup raises — the sweep must still close because BEAR
        # override skips evaluate_spread_exit entirely.
        def raising_lookup(occs):
            raise RuntimeError("OPRA outage")
        strategy = _strategy(quote_lookup=raising_lookup)
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="bear-sweep-1", symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
            raw_status="accepted", message="",
        )

        engine._sweep_bear_spread_exits()

        broker.dispatch_spread_order.assert_called_once()
        kw = broker.dispatch_spread_order.call_args.kwargs
        assert kw["closing"] is True
        assert kw["position_id"] == "p1"
        # Width fallback — quote outage cannot suppress the defensive close.
        assert kw["limit_price"] == 10.0
        assert "p1" in engine._spreads_pending_close

    def test_sweep_skips_strategies_without_open_spreads(self, tmp_path):
        strategy = _strategy()  # no open spreads registered
        engine, broker = _engine(tmp_path, strategy)
        engine._sweep_bear_spread_exits()
        broker.dispatch_spread_order.assert_not_called()

    def test_sweep_skips_single_leg_strategies(self, tmp_path):
        # An engine slot whose strategy has no evaluate_spread_exit hook
        # must be ignored by the sweep.
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        from strategies.base import BaseStrategy, StrategySlot
        single_leg = MagicMock(spec=BaseStrategy)
        single_leg.name = "sma_crossover"
        del single_leg.evaluate_spread_exit  # force hasattr() to False
        engine.slots = [
            StrategySlot(strategy=single_leg, symbols=["AAPL"]),
        ]
        engine._sweep_bear_spread_exits()
        broker.dispatch_spread_order.assert_not_called()

    def test_sweep_respects_pending_close_set(self, tmp_path):
        # A position whose close is already in flight must not be
        # re-dispatched by the sweep.
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        engine._spreads_pending_close.add("p1")
        engine._sweep_bear_spread_exits()
        broker.dispatch_spread_order.assert_not_called()

    def test_sweep_continues_when_one_strategy_raises(self, tmp_path):
        # If one strategy's sweep raises, the next strategy's spreads must
        # still get the defensive close.
        strategy_a = _strategy()
        engine, broker = _engine(tmp_path, strategy_a)
        # Strategy A: has an open spread; its open_spreads attribute is
        # patched to raise when accessed by _process_credit_spread_exits.
        self._wire_open_spread(engine, strategy_a, "pA")
        # Strategy B: a second spread strategy with an open spread.
        strategy_b = _strategy(quote_lookup=lambda occs: {o: None for o in occs})
        from strategies.base import StrategySlot
        engine.slots = engine.slots + [
            StrategySlot(strategy=strategy_b, symbols=["SPY"]),
        ]
        from engine.positions import make_spread, PositionLeg
        engine._positions["pB"] = make_spread(
            strategy_name="credit_spread", position_id="pB",
            legs=[PositionLeg("SPY260618P00568000", -1, side="SELL"),
                  PositionLeg("SPY260618P00558000", 1, side="BUY")],
        )
        engine._spread_owner_strategy["pB"] = strategy_b
        strategy_b.register_spread(_open_spread("pB", net_credit=2.00))

        # Make strategy A's _process_credit_spread_exits raise; sweep must
        # still close strategy B's position.
        original = engine._process_credit_spread_exits
        def selective_raiser(*, strategy, **kw):
            if strategy is strategy_a:
                raise RuntimeError("simulated failure")
            return original(strategy=strategy, **kw)
        engine._process_credit_spread_exits = selective_raiser

        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="bear-sweep-b", symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
            raw_status="accepted", message="",
        )
        engine._sweep_bear_spread_exits()

        # B's close was dispatched even though A raised.
        broker.dispatch_spread_order.assert_called_once()
        assert broker.dispatch_spread_order.call_args.kwargs["position_id"] == "pB"

    def test_per_symbol_call_is_idempotent_after_sweep(self, tmp_path):
        # After the cycle-level sweep dispatches the close, the per-symbol
        # _process_credit_spread_exits call in the slot loop must not
        # re-dispatch (gated by _spreads_pending_close).
        strategy = _strategy(quote_lookup=lambda occs: {o: None for o in occs})
        engine, broker = _engine(tmp_path, strategy)
        self._wire_open_spread(engine, strategy, "p1")
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="bear-sweep-idem", symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
            raw_status="accepted", message="",
        )

        engine._sweep_bear_spread_exits()
        # Second invocation — simulating the per-symbol call in the slot
        # loop right after the sweep.
        engine._process_credit_spread_exits(
            strategy=strategy, underlying="SPY", underlying_close=745.0,
            current_regime=MarketRegime.BEAR,
        )

        # Only one dispatch total.
        broker.dispatch_spread_order.assert_called_once()


# ── Global counter ──────────────────────────────────────────────────────────


class TestCountOpenSpreads:
    def test_counts_every_spread_position_across_strategies(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        from engine.positions import make_spread, make_single_leg, PositionLeg
        # Two credit spreads + one single-leg equity + one other spread.
        # PLAN.md 11.31: all spread positions count toward the global MLEG
        # concurrent total, regardless of which spread strategy owns them.
        for pid in ("cs-1", "cs-2"):
            engine._positions[pid] = make_spread(
                strategy_name="credit_spread", position_id=pid,
                legs=[PositionLeg("A", -1, side="SELL"), PositionLeg("B", 1, side="BUY")],
            )
        engine._positions["AAPL"] = make_single_leg(
            strategy_name="sma_crossover", symbol="AAPL", qty=10,
        )
        engine._positions["other"] = make_spread(
            strategy_name="some_other_spread_strat", position_id="other",
            legs=[PositionLeg("C", -1, side="SELL"), PositionLeg("D", 1, side="BUY")],
        )
        assert engine._count_open_spreads() == 3


# ── Spread strategy lookup (multi-MLEG ready) ───────────────────────────────


class TestSpreadStrategyFor:
    """PLAN.md 11.31 — _spread_strategy_for must duck-type on
    build_spread_execution (not a hardcoded name) and disambiguate by
    strategy_name when two spread strategies share an underlying.
    """

    def _stub_spread_strategy(self, name: str, symbols: tuple[str, ...]):
        from strategies.base import BaseStrategy, StrategySlot
        strategy = MagicMock(spec=BaseStrategy)
        strategy.name = name
        # build_spread_execution presence is the duck-type signal.
        strategy.build_spread_execution = MagicMock()
        slot = MagicMock(spec=StrategySlot)
        slot.strategy = strategy
        slot.active_symbols.return_value = list(symbols)
        return strategy, slot

    def test_resolves_by_duck_type_not_by_hardcoded_name(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        # A spread strategy with a non-credit_spread name still resolves.
        future_strategy, future_slot = self._stub_spread_strategy(
            "iron_condor", ("SPY",)
        )
        engine.slots = [future_slot]
        assert engine._spread_strategy_for("SPY") is future_strategy

    def test_disambiguates_by_strategy_name_when_supplied(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        cs_strat, cs_slot = self._stub_spread_strategy("credit_spread", ("SPY",))
        ic_strat, ic_slot = self._stub_spread_strategy("iron_condor", ("SPY",))
        engine.slots = [cs_slot, ic_slot]
        # Both share SPY; the DB row's recorded strategy name picks the owner.
        assert engine._spread_strategy_for(
            "SPY", strategy_name="iron_condor"
        ) is ic_strat
        assert engine._spread_strategy_for(
            "SPY", strategy_name="credit_spread"
        ) is cs_strat

    def test_skips_strategies_without_build_spread_execution(self, tmp_path):
        from strategies.base import BaseStrategy, StrategySlot
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        single_leg = MagicMock(spec=BaseStrategy)
        single_leg.name = "sma_crossover"
        # Force hasattr(strategy, "build_spread_execution") to be False on
        # the MagicMock by deleting the auto-generated attribute.
        del single_leg.build_spread_execution
        slot = MagicMock(spec=StrategySlot)
        slot.strategy = single_leg
        slot.active_symbols.return_value = ["SPY"]
        engine.slots = [slot]
        assert engine._spread_strategy_for("SPY") is None


# ── State-snapshot field ────────────────────────────────────────────────────


class TestCreditSpreadsSnapshot:
    def test_snapshot_renders_open_spreads(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        engine._spread_owner_strategy["p1"] = strategy
        strategy.register_spread(_open_spread("p1", net_credit=1.45))

        snap = engine._credit_spreads_snapshot()
        assert len(snap) == 1
        row = snap[0]
        assert row["position_id"] == "p1"
        assert row["strategy"] == "credit_spread"
        assert row["underlying"] == "SPY"        # owner_key_for(short OCC)
        assert row["net_credit"] == pytest.approx(1.45)
        assert row["width"] == pytest.approx(10.0)
        assert row["pending_close"] is False

    def test_pending_close_flag_surfaces(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        engine._spread_owner_strategy["p1"] = strategy
        strategy.register_spread(_open_spread("p1"))
        engine._spreads_pending_close.add("p1")
        assert engine._credit_spreads_snapshot()[0]["pending_close"] is True

    def test_empty_when_no_spreads(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        assert engine._credit_spreads_snapshot() == []


class TestMultiLegRiskNotional:
    def test_credit_spread_uses_defined_max_loss_notional(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        engine._spread_owner_strategy["p1"] = strategy
        strategy.register_spread(_open_spread("p1", net_credit=1.45))

        usage = engine._multi_leg_risk_notional_by_strategy()

        assert usage == {"credit_spread": pytest.approx(855.0)}

    def test_non_credit_spread_strategy_name_is_supported(self, tmp_path):
        strategy = _strategy()
        strategy.name = "iron_condor"
        engine, _ = _engine(tmp_path, strategy)
        engine._spread_owner_strategy["p1"] = strategy
        strategy.register_spread(_open_spread("p1", net_credit=2.00))

        usage = engine._multi_leg_risk_notional_by_strategy()

        assert usage == {"iron_condor": pytest.approx(800.0)}


class TestMultiLegPositionsSnapshot:
    def test_credit_spread_snapshot_includes_live_mark_pnl_and_distance(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        engine._spread_owner_strategy["p1"] = strategy
        strategy.register_spread(_open_spread("p1", net_credit=1.45))
        engine._last_underlying_prices["SPY"] = 580.0
        engine._last_snapshot = _snapshot_with({
            "SPY260618P00568000": SimpleNamespace(qty=-1, market_value=-300.0),
            "SPY260618P00558000": SimpleNamespace(qty=1, market_value=200.0),
        })

        snap = engine._multi_leg_positions_snapshot()

        assert len(snap) == 1
        row = snap[0]
        assert row["structure"] == "put_credit_spread"
        assert row["position_id"] == "p1"
        assert row["strategy"] == "credit_spread"
        assert row["underlying"] == "SPY"
        assert row["entry_net_price"] == pytest.approx(1.45)
        assert row["current_exit_price"] == pytest.approx(1.0)
        assert row["unrealized_pnl"] == pytest.approx(45.0)
        assert row["max_loss"] == pytest.approx(855.0)
        assert row["distance_to_short_strike"] == pytest.approx(12.0)

    def test_missing_leg_marks_do_not_block_static_snapshot(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        engine._spread_owner_strategy["p1"] = strategy
        strategy.register_spread(_open_spread("p1", net_credit=1.45))

        row = engine._multi_leg_positions_snapshot()[0]

        assert row["current_exit_price"] is None
        assert row["unrealized_pnl"] is None
        assert row["max_profit"] == pytest.approx(145.0)
        assert row["risk_used"] == pytest.approx(855.0)


# ── External close detection ────────────────────────────────────────────────


class TestSpreadExternalCloseDetection:
    def _track_open_spread(self, engine, strategy, position_id="p1"):
        from engine.positions import PositionLeg, make_spread

        engine._positions[position_id] = make_spread(
            strategy_name="credit_spread",
            position_id=position_id,
            legs=[
                PositionLeg("SPY260618P00568000", -1, side="SELL"),
                PositionLeg("SPY260618P00558000", 1, side="BUY"),
            ],
        )
        engine._spread_owner_strategy[position_id] = strategy
        strategy.register_spread(_open_spread(position_id))

    def test_spread_with_both_broker_legs_is_not_externally_closed(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        object.__setattr__(engine.config, "external_close_confirm_cycles", 1)
        self._track_open_spread(engine, strategy, "uuid-1")
        engine.trade_logger.log_external_close = MagicMock()
        engine.trade_logger.log_spread_fill = MagicMock()

        engine._detect_external_closes(
            _snapshot_with({
                "SPY260618P00568000": object(),
                "SPY260618P00558000": object(),
            })
        )

        assert "uuid-1" in engine._positions
        assert strategy.get_open_spread("uuid-1") is not None
        engine.trade_logger.log_external_close.assert_not_called()
        engine.trade_logger.log_spread_fill.assert_not_called()

    def test_spread_with_all_legs_absent_logs_spread_close_not_single_leg_close(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        object.__setattr__(engine.config, "external_close_confirm_cycles", 1)
        self._track_open_spread(engine, strategy, "uuid-1")
        engine.trade_logger.log_external_close = MagicMock()
        engine.trade_logger.log_spread_fill = MagicMock()

        engine._detect_external_closes(_snapshot_with({}))

        assert "uuid-1" not in engine._positions
        assert strategy.get_open_spread("uuid-1") is None
        engine.trade_logger.log_external_close.assert_not_called()
        engine.trade_logger.log_spread_fill.assert_called_once()
        assert engine.trade_logger.log_spread_fill.call_args.kwargs["position_id"] == "uuid-1"
        assert engine.trade_logger.log_spread_fill.call_args.kwargs["opening"] is False
        assert engine.trade_logger.log_spread_fill.call_args.kwargs["reason"] == "external_close_detected"

    def test_spread_with_one_missing_leg_keeps_ownership_for_manual_reconciliation(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        object.__setattr__(engine.config, "external_close_confirm_cycles", 1)
        self._track_open_spread(engine, strategy, "uuid-1")
        engine.trade_logger.log_external_close = MagicMock()
        engine.trade_logger.log_spread_fill = MagicMock()

        engine._detect_external_closes(
            _snapshot_with({"SPY260618P00568000": object()})
        )

        assert "uuid-1" in engine._positions
        assert strategy.get_open_spread("uuid-1") is not None
        engine.trade_logger.log_external_close.assert_not_called()
        engine.trade_logger.log_spread_fill.assert_not_called()


# ── Startup reconciliation — spread reconstruction ──────────────────────────


_SHORT_OCC = "SPY260618P00689000"   # strike 689 — the short (sold) leg
_LONG_OCC = "SPY260618P00674000"    # strike 674 — the long (bought) leg


def _snapshot_with(open_positions: dict):
    return SimpleNamespace(
        account=SimpleNamespace(open_positions=open_positions, equity=100_000.0),
        open_orders=[],
    )


class TestRestoreSpreadPositions:
    def _log_open_spread(self, engine, position_id="uuid-1", qty=1, net=2.54):
        engine.trade_logger.log_spread_fill(
            position_id=position_id, strategy="credit_spread",
            short_occ=_SHORT_OCC, long_occ=_LONG_OCC,
            qty=qty, net_price=net, opening=True,
        )

    def test_reconstructs_full_spread_from_db_and_broker_legs(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        self._log_open_spread(engine, "uuid-1", qty=2, net=2.54)
        snapshot = _snapshot_with({_SHORT_OCC: object(), _LONG_OCC: object()})

        conflicts: set[str] = set()
        leg_occs = engine._restore_spread_positions(snapshot, conflicts)

        assert conflicts == set()
        assert leg_occs == {_SHORT_OCC, _LONG_OCC}
        # Two-leg Position rebuilt, keyed by the UUID.
        assert "uuid-1" in engine._positions
        pos = engine._positions["uuid-1"]
        assert pos.is_spread and pos.strategy_name == "credit_spread"
        assert {leg.symbol for leg in pos.legs} == {_SHORT_OCC, _LONG_OCC}
        # Owner-strategy map + the strategy's OpenSpread view rebuilt.
        assert engine._spread_owner_strategy["uuid-1"] is strategy
        spread = strategy.get_open_spread("uuid-1")
        assert spread is not None
        assert spread.short_strike == pytest.approx(689.0)
        assert spread.long_strike == pytest.approx(674.0)
        assert spread.width == pytest.approx(15.0)
        assert spread.net_credit == pytest.approx(2.54)
        assert spread.qty == 2

    def test_single_leg_loop_skips_reconstructed_spread_legs(self, tmp_path):
        # Full _restore_ownership_from_db: the spread legs must NOT be
        # mis-assigned as single-leg positions (the naked-short risk).
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        self._log_open_spread(engine, "uuid-1")
        snapshot = _snapshot_with({_SHORT_OCC: object(), _LONG_OCC: object()})

        conflicts = engine._restore_ownership_from_db(snapshot)

        assert conflicts == set()
        # Exactly one tracked position — the spread — keyed by UUID. Neither
        # leg was registered as a standalone single-leg position.
        assert set(engine._positions) == {"uuid-1"}
        assert engine._positions["uuid-1"].is_spread

    def test_reconcile_startup_treats_reconstructed_spread_legs_as_managed(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        self._log_open_spread(engine, "uuid-1")
        snapshot = _snapshot_with({_SHORT_OCC: object(), _LONG_OCC: object()})

        conflicts = engine._restore_ownership_from_db(snapshot)
        mode = engine._reconcile_startup(snapshot, conflicts)

        assert conflicts == set()
        assert mode == "NORMAL"

    def test_missing_broker_leg_declares_conflict(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        self._log_open_spread(engine, "uuid-1")
        # Only the short leg is present at the broker — the long leg vanished.
        snapshot = _snapshot_with({_SHORT_OCC: object()})

        conflicts: set[str] = set()
        engine._restore_spread_positions(snapshot, conflicts)

        assert "SPY" in conflicts          # → RESTRICTED startup mode
        assert "uuid-1" not in engine._positions
        assert strategy.open_spreads == []

    def test_no_configured_strategy_declares_conflict(self, tmp_path):
        # Engine built with an SMA-style strategy — no credit_spread slot.
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        # Remove the credit_spread slot so _spread_strategy_for finds none.
        engine.slots = []
        self._log_open_spread(engine, "uuid-1")
        snapshot = _snapshot_with({_SHORT_OCC: object(), _LONG_OCC: object()})

        conflicts: set[str] = set()
        engine._restore_spread_positions(snapshot, conflicts)

        assert "SPY" in conflicts
        assert "uuid-1" not in engine._positions

    def test_closed_spread_is_not_reconstructed(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        self._log_open_spread(engine, "uuid-1")
        engine.trade_logger.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=_SHORT_OCC, long_occ=_LONG_OCC,
            qty=1, net_price=1.10, opening=False,
        )
        snapshot = _snapshot_with({})  # broker has nothing open

        conflicts: set[str] = set()
        leg_occs = engine._restore_spread_positions(snapshot, conflicts)

        assert leg_occs == set()
        assert conflicts == set()
        assert engine._positions == {}


# ── Entry guard — spread strategies bypass the single-leg position block ────


class TestEntryBlockedByExistingPosition:
    def test_single_leg_strategy_blocked_when_position_exists(self):
        # A non-spread strategy is still blocked by an existing position —
        # the original "skip re-entry, the bar persists" behavior.
        from strategies.sma_crossover import SMACrossover
        sma = SMACrossover()
        assert TradingEngine._entry_blocked_by_existing_position(
            sma, object()
        ) is True

    def test_single_leg_strategy_not_blocked_when_flat(self):
        from strategies.sma_crossover import SMACrossover
        sma = SMACrossover()
        assert TradingEngine._entry_blocked_by_existing_position(
            sma, None
        ) is False

    def test_credit_spread_strategy_never_blocked_by_existing_position(self):
        # Regression: _get_position_for() regex-matches a spread *leg* OCC to
        # the underlying, so a held spread looked like an "existing position"
        # and silently disabled max_concurrent_positions / max_per_expiration
        # / DTE staggering. Spread strategies must bypass this guard — their
        # own per-instance caps are the concurrency control.
        strategy = _strategy()  # a real CreditSpread instance
        # Even with a (leg) position present, the guard must not block.
        assert TradingEngine._entry_blocked_by_existing_position(
            strategy, object()
        ) is False
        assert TradingEngine._entry_blocked_by_existing_position(
            strategy, None
        ) is False
