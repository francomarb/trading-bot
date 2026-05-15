"""
Unit tests for the engine's credit-spread wiring (PLAN.md 11.29 PR 3b):
the entry path (_enter_credit_spread), the async fill drain
(_drain_spread_fills), and the exit path (_process_credit_spread_exits).

The broker is a MagicMock; the strategy is a real CreditSpread with stubbed
quote/IV lookups, and find_best_put_spread is patched where needed.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from engine.trader import EngineConfig, TradingEngine
from execution.broker import OrderResult, OrderStatus
from reporting.logger import TradeLogger
from risk.manager import RiskManager
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
    def test_happy_path_dispatches_and_pre_registers(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        broker.dispatch_spread_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="spread-worker-1", symbol="SPY",
            requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
            raw_status="accepted", message="",
        )
        with patch("strategies.credit_spread.find_best_put_spread", return_value=_pick()):
            engine._enter_credit_spread(
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
            engine._enter_credit_spread(
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
        engine._enter_credit_spread(
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
            engine._enter_credit_spread(
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
            engine._enter_credit_spread(
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
            ("p1", "credit_spread", False, "filled", 1.0, -1.50, "combo-1"),
        ]
        engine._drain_spread_fills()
        # Position stays; the plan is consumed.
        assert "p1" in engine._positions
        assert "p1" not in engine._pending_spread_plans
        assert len(strategy.open_spreads) == 1
        # Logged to the trade DB as a spread entry.
        rows = engine.trade_logger.read_all()
        assert len(rows) == 1
        assert rows[0]["position_type"] == "spread"
        assert rows[0]["position_id"] == "p1"
        assert rows[0]["side"] == "sell"

    def test_open_canceled_rolls_back(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", False, "canceled", 0.0, None, "combo-1"),
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
            ("p1", "credit_spread", True, "filled", 1.0, 0.60, "combo-close-1"),
        ]
        engine._drain_spread_fills()
        assert "p1" not in engine._positions
        assert "p1" not in engine._spread_owner_strategy
        assert "p1" not in engine._spreads_pending_close
        assert strategy.open_spreads == []
        rows = engine.trade_logger.read_all()
        assert rows[-1]["side"] == "buy"  # closing the spread is a buy-back
        assert rows[-1]["position_type"] == "spread"

    def test_close_canceled_keeps_position_for_retry(self, tmp_path):
        strategy = _strategy()
        engine, broker = _engine(tmp_path, strategy)
        self._pre_register(engine, strategy, "p1")
        engine._spreads_pending_close.add("p1")
        broker.drain_spread_fills.return_value = [
            ("p1", "credit_spread", True, "canceled", 0.0, None, "combo-close-1"),
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


# ── Global counter ──────────────────────────────────────────────────────────


class TestCountOpenCreditSpreads:
    def test_counts_only_credit_spread_positions(self, tmp_path):
        strategy = _strategy()
        engine, _ = _engine(tmp_path, strategy)
        from engine.positions import make_spread, make_single_leg, PositionLeg
        # Two credit spreads + one single-leg equity + one other spread.
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
        assert engine._count_open_credit_spreads() == 2


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
