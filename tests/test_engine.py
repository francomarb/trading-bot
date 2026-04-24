"""
Unit tests for engine/trader.py.

The engine wires together the broker, risk, strategy, fetcher, and
indicators. We test it offline with:
  - a fake broker (records calls + lets us script fills / open positions)
  - a fake strategy (lets us declare entry/exit on the latest bar)
  - monkeypatched fetch_symbol (returns synthetic bars, freshness controllable)

Coverage map (one class per concern):
  - TestEngineConfig: validation (empty symbols, bad timeframe, etc.)
  - TestProcessSymbol: every branch of the per-symbol pipeline
      * entry signal, no position → place_order called via RiskDecision
      * entry signal, already in position → risk DUPLICATE_POSITION rejection,
        no order
      * exit signal, position open → close_position called
      * exit signal, no position → no action
      * no signal → no action
      * stale data → no action, no broker call
      * fetch raises → caught, no crash, no broker call
      * pending close order → exit signal does not double-close
  - TestRunOneCycle: market-closed skip, broker sync failure containment,
    one bad symbol does not abort the cycle
  - TestStartStop: max_cycles termination, stop() mid-loop
  - TestShutdown: cancel_orders_on_shutdown true / false
  - TestSlippageRecording: realized vs modeled is fed to the risk manager
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from engine.trader import EngineConfig, TradingEngine, _lookback_days
from execution.broker import (
    BrokerSnapshot,
    OpenOrder,
    OrderResult,
    OrderStatus,
)
from reporting.logger import TradeLogger
from risk.manager import (
    AccountState,
    Position,
    RiskManager,
    Side,
)
from strategies.base import BaseStrategy, OrderType, SignalFrame


# ── Fakes ────────────────────────────────────────────────────────────────────


T0 = datetime(2026, 4, 16, 14, 30, tzinfo=timezone.utc)


class FakeStrategy(BaseStrategy):
    """Returns whatever entry/exit pattern the test pins on construction."""

    name = "fake_strategy"
    preferred_order_type = OrderType.MARKET

    def __init__(self, *, entries: list[bool], exits: list[bool]):
        super().__init__()
        self._entries = entries
        self._exits = exits

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        # Pad/trim to df length.
        n = len(df)
        e = (self._entries + [False] * n)[:n]
        x = (self._exits + [False] * n)[:n]
        return SignalFrame(
            entries=pd.Series(e, index=df.index, dtype=bool),
            exits=pd.Series(x, index=df.index, dtype=bool),
        )


def _bars(n: int = 60, end: datetime = T0, base: float = 100.0) -> pd.DataFrame:
    """Synthetic daily bars ending at `end`."""
    idx = pd.DatetimeIndex(
        [end - timedelta(days=n - 1 - i) for i in range(n)], tz="UTC"
    )
    closes = [base + (i % 7) * 0.5 for i in range(n)]
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1_000 + i for i in range(n)],
        },
        index=idx,
    )


def _snapshot(
    *,
    equity: float = 100_000.0,
    positions: dict[str, Position] | None = None,
    open_orders: list[OpenOrder] | None = None,
) -> BrokerSnapshot:
    return BrokerSnapshot(
        account=AccountState(
            equity=equity,
            cash=equity,
            session_start_equity=equity,
            open_positions=positions or {},
        ),
        open_orders=open_orders or [],
    )


def _filled_result(symbol: str, qty: int, avg: float) -> OrderResult:
    return OrderResult(
        status=OrderStatus.FILLED,
        order_id="ord-1",
        symbol=symbol,
        requested_qty=qty,
        filled_qty=qty,
        avg_fill_price=avg,
        raw_status="filled",
        message="ok",
    )


def _open_sell_order(symbol: str = "AAPL") -> OpenOrder:
    return OpenOrder(
        order_id="o-sell",
        symbol=symbol,
        side=Side.SELL,
        qty=1,
        order_type=OrderType.MARKET,
        status="open",
        submitted_at=T0,
        limit_price=None,
        stop_price=None,
    )


def _open_stop_order(symbol: str = "AAPL", stop_price: float = 95.0) -> OpenOrder:
    return OpenOrder(
        order_id="o-stop",
        symbol=symbol,
        side=Side.SELL,
        qty=1,
        order_type=OrderType.MARKET,
        status="open",
        submitted_at=T0,
        limit_price=None,
        stop_price=stop_price,
    )


@pytest.fixture
def patch_fetch(monkeypatch):
    """Provide a controllable fetch_symbol stub. Tests mutate the returned
    holder dict to set the next df / next exception."""
    holder: dict = {"df": _bars(), "raises": None}

    def _fetch(symbol, start, end, timeframe="1Day"):
        if holder["raises"] is not None:
            raise holder["raises"]
        # Return whatever the test pinned. Stats is not used by the engine,
        # so a simple namespace is fine.
        return holder["df"], SimpleNamespace(api_calls=0)

    monkeypatch.setattr("engine.trader.fetch_symbol", _fetch)
    return holder


@pytest.fixture
def engine_factory(patch_fetch):
    """Build an engine with one symbol, default risk, fake broker, fake strategy."""

    def _factory(
        *,
        entries: list[bool] = [False],
        exits: list[bool] = [False],
        snapshot: BrokerSnapshot | None = None,
        place_result: OrderResult | None = None,
        close_result: OrderResult | None = None,
        market_open: bool = True,
        config_overrides: dict | None = None,
    ) -> tuple[TradingEngine, MagicMock]:
        broker = MagicMock()
        broker.sync_with_broker.return_value = snapshot or _snapshot()
        broker.place_order.return_value = place_result or _filled_result("AAPL", 1, 100.5)
        broker.close_position.return_value = close_result or _filled_result("AAPL", 1, 100.0)
        broker.get_open_orders.return_value = []
        # Market-clock injection: engine calls broker._with_retry(broker._api.get_clock).
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=market_open)

        strategy = FakeStrategy(entries=entries, exits=exits)
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
            max_bar_age_multiplier=10.0,  # synthetic bars are days "old" wrt T0
            market_hours_only=False,
            cancel_orders_on_shutdown=True,
            atr_length=14,
        )
        if config_overrides:
            cfg = EngineConfig(**{**cfg.__dict__, **config_overrides})

        engine = TradingEngine(
            strategy=strategy,
            symbols=["AAPL"],
            risk=risk,
            broker=broker,
            config=cfg,
            clock=lambda: T0,
        )
        return engine, broker

    return _factory


# ── EngineConfig ─────────────────────────────────────────────────────────────


class TestEngineConfig:
    def test_negative_cycle_interval_rejected(self):
        with pytest.raises(ValueError):
            EngineConfig(cycle_interval_seconds=0)

    def test_max_bar_age_multiplier_must_be_above_one(self):
        with pytest.raises(ValueError):
            EngineConfig(max_bar_age_multiplier=1.0)


# ── _lookback_days helper ──────────────────────────────────────────────────


class TestLookbackDays:
    def test_daily_bars_accounts_for_weekends(self):
        # 200 daily bars × 1.5 cal days/bar + 5 buffer = 305
        assert _lookback_days(200, "1Day", config_lookback=60) == 305

    def test_hourly_bars(self):
        # 50 hourly bars × (1/6.5) + 5 ≈ 12
        result = _lookback_days(50, "1Hour", config_lookback=5)
        assert result == int(50 * (1.0 / 6.5)) + 5

    def test_config_lookback_wins_when_larger(self):
        # 20 daily bars × 1.5 + 5 = 35, but config says 60
        assert _lookback_days(20, "1Day", config_lookback=60) == 60

    def test_unknown_timeframe_uses_conservative_default(self):
        # Unknown → 1.5 days/bar (same as daily)
        assert _lookback_days(100, "5Min", config_lookback=10) == int(100 * 1.5) + 5


# ── _process_symbol: every branch ────────────────────────────────────────────


class TestProcessSymbol:
    def _process(self, engine, symbol, snap):
        """Helper: call _process_symbol with the engine's first slot."""
        slot = engine.slots[0]
        return engine._process_symbol(
            symbol, snap, snap.account, slot.strategy, slot.timeframe
        )

    def test_entry_signal_no_position_places_order(self, engine_factory):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        filled = self._process(engine, "AAPL", snap)
        assert broker.place_order.call_count == 1
        decision = broker.place_order.call_args.args[0]
        assert decision.symbol == "AAPL"
        assert decision.side is Side.BUY
        assert filled == Position("AAPL", 1, 100.5, 100.5)
        broker.close_position.assert_not_called()

    def test_entry_signal_with_existing_position_no_order(self, engine_factory):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        positions = {
            "AAPL": Position("AAPL", 10, 100.0, 1010.0),
        }
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity
        self._process(engine, "AAPL", snap)
        # Risk would reject DUPLICATE_POSITION → no place_order.
        broker.place_order.assert_not_called()
        broker.close_position.assert_not_called()

    def test_exit_signal_with_position_calls_close(self, engine_factory):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity
        self._process(engine, "AAPL", snap)
        broker.close_position.assert_called_once_with("AAPL")
        broker.place_order.assert_not_called()

    def test_exit_signal_with_no_position_does_nothing(self, engine_factory):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        self._process(engine, "AAPL", snap)
        broker.close_position.assert_not_called()
        broker.place_order.assert_not_called()

    def test_no_signal_no_action(self, engine_factory):
        engine, broker = engine_factory()
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        self._process(engine, "AAPL", snap)
        broker.place_order.assert_not_called()
        broker.close_position.assert_not_called()

    def test_stale_data_skips_silently(self, engine_factory, patch_fetch):
        # Bars from 30 days ago — easily past max_bar_age (10×1day).
        old_end = T0 - timedelta(days=30)
        patch_fetch["df"] = _bars(end=old_end)
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        self._process(engine, "AAPL", snap)
        broker.place_order.assert_not_called()

    def test_fetch_failure_caught_no_crash(self, engine_factory, patch_fetch):
        patch_fetch["raises"] = RuntimeError("boom")
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        # Should not raise.
        self._process(engine, "AAPL", snap)
        broker.place_order.assert_not_called()

    def test_pending_close_order_blocks_redundant_close(self, engine_factory):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions, open_orders=[_open_sell_order("AAPL")])
        engine._session_start_equity = snap.account.equity
        self._process(engine, "AAPL", snap)
        broker.close_position.assert_not_called()


# ── _run_one_cycle ───────────────────────────────────────────────────────────


class TestRunOneCycle:
    def test_market_closed_skips_cycle(self, engine_factory):
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            market_open=False,
            config_overrides={"market_hours_only": True},
        )
        engine._session_start_equity = 100_000.0
        engine._cycle_count = 1
        engine._run_one_cycle()
        broker.sync_with_broker.assert_not_called()
        broker.place_order.assert_not_called()

    def test_market_closed_cycle_updates_sleep_gap_baseline(self, engine_factory):
        engine, broker = engine_factory(
            market_open=False,
            config_overrides={"market_hours_only": True},
        )
        engine._session_start_equity = 100_000.0
        engine._cycle_count = 1
        before = time.monotonic()

        engine._run_one_cycle()

        assert engine._last_cycle_end >= before
        broker.sync_with_broker.assert_not_called()

    def test_sync_failure_skips_cycle_and_records_broker_error(
        self, engine_factory
    ):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        broker.sync_with_broker.side_effect = RuntimeError("network down")
        engine._cycle_count = 1
        engine._run_one_cycle()
        broker.place_order.assert_not_called()
        # broker_error recorder bumped:
        assert len(engine.risk._broker_errors) == 1

    def test_one_bad_symbol_does_not_abort_cycle(
        self, engine_factory, patch_fetch
    ):
        # Multi-symbol slot; first symbol's fetch raises, second succeeds.
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
        )
        # Widen the slot's symbol list to include a bad symbol.
        engine.slots[0].symbols = ["BAD", "AAPL"]
        engine._session_start_equity = 100_000.0
        engine._cycle_count = 1

        # First call raises, then we let it succeed.
        original = patch_fetch["df"]

        def _fetch_with_first_bad(symbol, start, end, timeframe="1Day"):
            if symbol == "BAD":
                raise RuntimeError("fetch boom")
            return original, SimpleNamespace(api_calls=0)

        # Replace the engine's binding.
        import engine.trader as engmod

        engmod.fetch_symbol = _fetch_with_first_bad
        try:
            engine._run_one_cycle()
        finally:
            engmod.fetch_symbol = lambda *a, **k: (original, SimpleNamespace(api_calls=0))

        # Even with the first symbol failing, the second placed an order.
        assert broker.place_order.call_count == 1


# ── start() / stop() / max_cycles ────────────────────────────────────────────


class TestStartStop:
    def test_max_cycles_terminates_loop(self, engine_factory):
        engine, broker = engine_factory()
        engine.start(max_cycles=3)
        assert engine._cycle_count == 3
        # Sync called once on startup + once per cycle = 4.
        assert broker.sync_with_broker.call_count == 4

    def test_stop_during_cycle_exits_cleanly(self, engine_factory):
        engine, broker = engine_factory()

        # Stop after first cycle by piggy-backing on sync.
        original_sync = broker.sync_with_broker
        sync_calls = {"n": 0}

        def _sync(**kwargs):
            sync_calls["n"] += 1
            if sync_calls["n"] == 2:  # startup is #1, first cycle is #2
                engine.stop()
            return original_sync.return_value

        broker.sync_with_broker.side_effect = _sync
        engine.start(max_cycles=10)
        assert engine._cycle_count == 1


# ── shutdown ─────────────────────────────────────────────────────────────────


class TestShutdown:
    def test_cancel_orders_on_shutdown_true(self, engine_factory):
        engine, broker = engine_factory(
            config_overrides={"cancel_orders_on_shutdown": True}
        )
        broker.get_open_orders.return_value = [_open_sell_order("AAPL")]
        engine.start(max_cycles=1)
        broker.cancel_order.assert_called_once_with("o-sell")

    def test_cancel_orders_on_shutdown_false(self, engine_factory):
        engine, broker = engine_factory(
            config_overrides={"cancel_orders_on_shutdown": False}
        )
        broker.get_open_orders.return_value = [_open_sell_order("AAPL")]
        engine.start(max_cycles=1)
        broker.cancel_order.assert_not_called()


# ── slippage recording ──────────────────────────────────────────────────────


class TestSlippageRecording:
    def test_market_order_uses_model_bps(self, engine_factory):
        """MARKET entries use SLIPPAGE_MODEL_MARKET_BPS (5.0) as modeled cost."""
        from config.settings import SLIPPAGE_MODEL_MARKET_BPS

        modeled_close = 101.5
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            place_result=_filled_result("AAPL", 1, modeled_close + 0.20),
        )
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)

        assert len(engine.risk._slippage_samples) == 1
        modeled_bps, realized_bps = engine.risk._slippage_samples[0]
        # MARKET order → modeled cost is the configured baseline, not 0.
        assert modeled_bps == pytest.approx(SLIPPAGE_MODEL_MARKET_BPS)
        assert realized_bps == pytest.approx(0.20 / modeled_close * 10_000, rel=1e-3)

    def test_limit_order_models_zero_bps(self, engine_factory):
        """LIMIT entries model 0 bps — the fill price is controlled by the limit."""
        modeled_close = 101.5
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            place_result=_filled_result("AAPL", 1, modeled_close + 0.05),
        )
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        # Override the strategy's preferred order type so the Signal carries LIMIT.
        slot.strategy.preferred_order_type = OrderType.LIMIT
        # Risk also needs a limit_price on the signal — patch evaluate to return
        # a valid LIMIT RiskDecision instead of going through full sizing logic.
        from risk.manager import RiskDecision, Side
        limit_decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=1,
            entry_reference_price=modeled_close,
            stop_price=modeled_close - 5.0,
            strategy_name="fake_strategy",
            reason="test",
            order_type=OrderType.LIMIT,
            limit_price=modeled_close,
        )
        engine.risk.evaluate = MagicMock(return_value=limit_decision)
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)

        assert len(engine.risk._slippage_samples) == 1
        modeled_bps, _ = engine.risk._slippage_samples[0]
        assert modeled_bps == 0.0


# ── Multi-slot ──────────────────────────────────────────────────────────────


class TestMultiSlot:
    def test_multi_slot_processes_all_slots(self, patch_fetch):
        """Two slots with different strategies and symbols — both fire."""
        from strategies.base import StrategySlot

        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.place_order.return_value = _filled_result("AAPL", 1, 100.5)
        broker.close_position.return_value = _filled_result("AAPL", 1, 100.0)
        broker.get_open_orders.return_value = []
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

        strat_a = FakeStrategy(entries=[False] * 59 + [True], exits=[False])
        strat_b = FakeStrategy(entries=[False] * 59 + [True], exits=[False])
        strat_b.name = "fake_strategy_b"

        slots = [
            StrategySlot(strategy=strat_a, symbols=["AAPL"]),
            StrategySlot(strategy=strat_b, symbols=["MSFT"]),
        ]

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

        config = EngineConfig(
            cycle_interval_seconds=0.01,
            max_bar_age_multiplier=10.0,
            market_hours_only=False,
            cancel_orders_on_shutdown=False,
            atr_length=14,
        )

        engine = TradingEngine(
            slots=slots,
            risk=risk,
            broker=broker,
            config=config,
            clock=lambda: T0,
        )
        engine.start(max_cycles=1)
        # Both slots should have placed orders.
        assert broker.place_order.call_count == 2

    def test_legacy_single_strategy_api_still_works(self, patch_fetch):
        """Passing strategy= (no slots) still works via backward compat."""
        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.place_order.return_value = _filled_result("AAPL", 1, 100.5)
        broker.get_open_orders.return_value = []
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

        strategy = FakeStrategy(entries=[False], exits=[False])
        config = EngineConfig(
            cycle_interval_seconds=0.01,
            max_bar_age_multiplier=10.0,
            market_hours_only=False,
            cancel_orders_on_shutdown=False,
        )
        engine = TradingEngine(
            strategy=strategy,
            symbols=["AAPL"],
            risk=RiskManager(),
            broker=broker,
            config=config,
            clock=lambda: T0,
        )
        assert len(engine.slots) == 1
        assert engine.slots[0].strategy is strategy
        assert engine.slots[0].symbols == ["AAPL"]

    def test_no_strategy_no_slots_raises(self):
        """Must provide either strategy or slots."""
        with pytest.raises(ValueError, match="slots.*strategy"):
            TradingEngine(
                risk=RiskManager(),
                broker=MagicMock(),
            )


# ── Position ownership ────────────────────────────────────────────────────


class TestPositionOwnership:
    """Verify that exit signals only close positions owned by the same strategy."""

    def test_exit_ignored_when_position_owned_by_different_strategy(
        self, engine_factory
    ):
        """Strategy B's exit should not close Strategy A's position."""
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        # Mark AAPL as owned by a different strategy.
        engine._position_owners["AAPL"] = "other_strategy"

        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        broker.close_position.assert_not_called()

    def test_exit_allowed_when_position_owned_by_same_strategy(
        self, engine_factory
    ):
        """Strategy's own exit closes its own position normally."""
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        # Mark AAPL as owned by this strategy.
        engine._position_owners["AAPL"] = "fake_strategy"

        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        broker.close_position.assert_called_once_with("AAPL")
        # Ownership cleared after close.
        assert "AAPL" not in engine._position_owners

    def test_exit_allowed_when_no_owner_recorded(self, engine_factory):
        """Pre-existing positions (no recorded owner) can be closed by anyone."""
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        # No ownership recorded — should still allow close.
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        broker.close_position.assert_called_once_with("AAPL")

    def test_entry_registers_ownership(self, engine_factory):
        """A successful entry fill records the strategy as position owner."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity

        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        assert broker.place_order.call_count == 1
        assert engine._position_owners["AAPL"] == "fake_strategy"

    def test_startup_seeds_ownership_from_broker(self, engine_factory):
        """On start(), existing broker positions are assigned to matching slots."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        engine, broker = engine_factory(snapshot=_snapshot(positions=positions))
        engine.start(max_cycles=1)
        assert engine._position_owners["AAPL"] == "fake_strategy"

    def test_startup_repairs_missing_protective_stop(
        self, engine_factory, tmp_path
    ):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        startup = _snapshot(positions=positions, open_orders=[])
        cycle = _snapshot(
            positions=positions,
            open_orders=[_open_stop_order("AAPL", 95.0)],
        )
        engine, broker = engine_factory(snapshot=startup)
        broker.sync_with_broker.side_effect = [startup, cycle]
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        engine.trade_logger = tl
        tl.log(tl.build_record(
            decision=SimpleNamespace(
                symbol="AAPL",
                side=Side.BUY,
                qty=10,
                entry_reference_price=100.0,
                stop_price=95.0,
                strategy_name="fake_strategy",
                reason="test",
                order_type=OrderType.MARKET,
            ),
            result=_filled_result("AAPL", 10, 100.5),
            modeled_price=100.0,
        ))
        broker.place_protective_stop.return_value = _open_stop_order("AAPL", 95.0)

        engine.start(max_cycles=1)

        broker.place_protective_stop.assert_called_once_with(
            symbol="AAPL",
            qty=10,
            stop_price=95.0,
            client_order_id_prefix="fake_strategy-repair-stop",
        )

    def test_cycle_repairs_missing_protective_stop_after_gtc_absent(
        self, engine_factory, tmp_path
    ):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        startup = _snapshot(
            positions=positions,
            open_orders=[_open_stop_order("AAPL", 95.0)],
        )
        cycle = _snapshot(positions=positions, open_orders=[])
        engine, broker = engine_factory(snapshot=startup)
        broker.sync_with_broker.side_effect = [startup, cycle]
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        engine.trade_logger = tl
        tl.log(tl.build_record(
            decision=SimpleNamespace(
                symbol="AAPL",
                side=Side.BUY,
                qty=10,
                entry_reference_price=100.0,
                stop_price=95.0,
                strategy_name="fake_strategy",
                reason="test",
                order_type=OrderType.MARKET,
            ),
            result=_filled_result("AAPL", 10, 100.5),
            modeled_price=100.0,
        ))
        broker.place_protective_stop.return_value = _open_stop_order("AAPL", 95.0)

        engine.start(max_cycles=1)

        broker.place_protective_stop.assert_called_once()


# ── Scanner cadence ────────────────────────────────────────────────────────


class TestScannerCadence:
    def test_scanner_runs_on_first_call(self):
        """Scanner fires immediately on the first active_symbols() call."""
        from strategies.base import Scanner, StrategySlot

        class CountingScanner(Scanner):
            def __init__(self):
                self.call_count = 0

            def scan(self) -> list[str]:
                self.call_count += 1
                return ["AAPL"]

        scanner = CountingScanner()
        slot = StrategySlot(
            strategy=FakeStrategy(entries=[False], exits=[False]),
            scanner=scanner,
            scan_interval_seconds=3600,
        )
        result = slot.active_symbols()
        assert result == ["AAPL"]
        assert scanner.call_count == 1

    def test_scanner_throttled_by_interval(self):
        """Scanner does not fire again before scan_interval_seconds elapse."""
        from strategies.base import Scanner, StrategySlot

        class CountingScanner(Scanner):
            def __init__(self):
                self.call_count = 0

            def scan(self) -> list[str]:
                self.call_count += 1
                return ["AAPL", "MSFT"]

        scanner = CountingScanner()
        slot = StrategySlot(
            strategy=FakeStrategy(entries=[False], exits=[False]),
            scanner=scanner,
            scan_interval_seconds=3600,  # 1 hour
        )
        slot.active_symbols()
        assert scanner.call_count == 1

        # Second call within the interval — should return cached symbols.
        result = slot.active_symbols()
        assert result == ["AAPL", "MSFT"]
        assert scanner.call_count == 1  # still 1

    def test_scanner_fires_after_interval_elapses(self):
        """Scanner fires again once enough time has passed."""
        import time as _time
        from strategies.base import Scanner, StrategySlot

        class CountingScanner(Scanner):
            def __init__(self):
                self.call_count = 0

            def scan(self) -> list[str]:
                self.call_count += 1
                return ["AAPL"]

        scanner = CountingScanner()
        slot = StrategySlot(
            strategy=FakeStrategy(entries=[False], exits=[False]),
            scanner=scanner,
            scan_interval_seconds=0.05,  # 50ms
        )
        slot.active_symbols()
        assert scanner.call_count == 1

        _time.sleep(0.06)
        slot.active_symbols()
        assert scanner.call_count == 2


# ── Durable ownership from trade DB (10.C1) ───────────────────────────────


def _engine_with_db(patch_fetch, tmp_path, *, positions=None, snapshot=None):
    """Build an engine with a real TradeLogger backed by a tmp_path DB."""
    broker = MagicMock()
    snap = snapshot or _snapshot(positions=positions or {})
    broker.sync_with_broker.return_value = snap
    broker.place_order.return_value = _filled_result("AAPL", 1, 100.5)
    broker.close_position.return_value = _filled_result("AAPL", 1, 100.0)
    broker.get_open_orders.return_value = []
    broker._with_retry.side_effect = lambda fn, **_: fn()
    broker._api.get_clock.return_value = SimpleNamespace(is_open=False)

    strategy = FakeStrategy(entries=[False], exits=[False])
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
        cancel_orders_on_shutdown=False,
        atr_length=14,
    )
    tl = TradeLogger(path=str(tmp_path / "trades.db"))
    engine = TradingEngine(
        strategy=strategy,
        symbols=["AAPL"],
        risk=risk,
        broker=broker,
        config=cfg,
        trade_logger=tl,
        clock=lambda: T0,
    )
    return engine, broker, tl


def _write_buy(tl: TradeLogger, symbol: str, strategy: str) -> None:
    """Insert a filled buy row into a TradeLogger's DB."""
    tl.log(
        tl.build_record(
            decision=SimpleNamespace(
                symbol=symbol,
                side=Side.BUY,
                qty=10,
                entry_reference_price=100.0,
                stop_price=95.0,
                strategy_name=strategy,
                reason="test",
                order_type=OrderType.MARKET,
            ),
            result=_filled_result(symbol, 10, 100.5),
            modeled_price=100.0,
        )
    )


def _write_sell(tl: TradeLogger, symbol: str, strategy: str) -> None:
    """Insert a filled sell row into a TradeLogger's DB."""
    from reporting.logger import TradeRecord

    tl.log(
        TradeRecord(
            timestamp="2026-04-23T10:00:00+00:00",
            symbol=symbol,
            side="sell",
            qty=10,
            avg_fill_price=105.0,
            order_id="ord-sell",
            strategy=strategy,
            reason="exit signal",
            stop_price=0.0,
            entry_reference_price=100.0,
            modeled_slippage_bps=0.0,
            realized_slippage_bps=5.0,
            order_type="market",
            status="filled",
            requested_qty=10,
            filled_qty=10,
        )
    )


class TestDurableOwnershipFromDB:
    """10.C1 — _restore_ownership_from_db reads the trade log, not slot order."""

    def test_db_record_authoritative_owner(self, patch_fetch, tmp_path):
        """DB buy record → ownership assigned from DB, not slot guess."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        _write_buy(tl, "AAPL", "fake_strategy")

        snap = _snapshot(positions=positions)
        conflicts = engine._restore_ownership_from_db(snap)

        assert engine._position_owners["AAPL"] == "fake_strategy"
        assert conflicts == set()

    def test_db_unknown_strategy_becomes_conflict(self, patch_fetch, tmp_path):
        """DB buy owned by a strategy not in any slot → conflict, no assignment."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        _write_buy(tl, "AAPL", "retired_strategy")

        snap = _snapshot(positions=positions)
        conflicts = engine._restore_ownership_from_db(snap)

        assert "AAPL" not in engine._position_owners
        assert "AAPL" in conflicts

    def test_no_db_record_falls_back_to_slot_match(self, patch_fetch, tmp_path):
        """No DB record → fall back to slot-order match (AAPL in slot → assigned)."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        # No buy record written — DB is empty.

        snap = _snapshot(positions=positions)
        conflicts = engine._restore_ownership_from_db(snap)

        assert engine._position_owners["AAPL"] == "fake_strategy"
        assert conflicts == set()

    def test_db_sell_as_latest_falls_back(self, patch_fetch, tmp_path):
        """Latest DB row is a sell (position closed) → treated as no open record."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        _write_buy(tl, "AAPL", "fake_strategy")
        _write_sell(tl, "AAPL", "fake_strategy")
        # Net = closed.  DB shows no open position → fallback.

        snap = _snapshot(positions=positions)
        engine._restore_ownership_from_db(snap)

        # Fallback slot match still assigns ownership.
        assert engine._position_owners["AAPL"] == "fake_strategy"

    def test_read_all_open_owners_empty_db(self, tmp_path):
        """read_all_open_owners returns {} when the DB doesn't exist."""
        tl = TradeLogger(path=str(tmp_path / "no_trades.db"))
        assert tl.read_all_open_owners() == {}

    def test_read_all_open_owners_buy_only(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        _write_buy(tl, "AAPL", "sma_crossover")
        _write_buy(tl, "MSFT", "rsi_reversion")
        result = tl.read_all_open_owners()
        assert result == {"AAPL": "sma_crossover", "MSFT": "rsi_reversion"}

    def test_read_all_open_owners_sell_closes(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        _write_buy(tl, "AAPL", "sma_crossover")
        _write_sell(tl, "AAPL", "sma_crossover")
        result = tl.read_all_open_owners()
        assert "AAPL" not in result

    def test_read_owner_for_symbol_buy(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        _write_buy(tl, "AAPL", "sma_crossover")
        assert tl.read_owner_for_symbol("AAPL") == "sma_crossover"

    def test_read_owner_for_symbol_sell_returns_none(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        _write_buy(tl, "AAPL", "sma_crossover")
        _write_sell(tl, "AAPL", "sma_crossover")
        assert tl.read_owner_for_symbol("AAPL") is None

    def test_read_owner_for_symbol_no_db(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "no_trades.db"))
        assert tl.read_owner_for_symbol("AAPL") is None


# ── Startup reconciliation modes (10.C2) ──────────────────────────────────


class TestStartupReconciliation:
    """10.C2 — _reconcile_startup returns NORMAL/RESTRICTED; RESTRICTED auto-clears."""

    def test_no_conflicts_no_unmanaged_gives_normal(self, patch_fetch, tmp_path):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        # Pre-assign ownership so no unmanaged positions.
        engine._position_owners["AAPL"] = "fake_strategy"
        snap = _snapshot(positions=positions)

        mode = engine._reconcile_startup(snap, set())
        assert mode == "NORMAL"

    def test_conflicts_give_restricted(self, patch_fetch, tmp_path):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, _ = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        snap = _snapshot(positions=positions)

        mode = engine._reconcile_startup(snap, {"AAPL"})
        assert mode == "RESTRICTED"

    def test_unmanaged_positions_give_restricted(self, patch_fetch, tmp_path):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, _ = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        # AAPL not in _position_owners → unmanaged.
        snap = _snapshot(positions=positions)

        mode = engine._reconcile_startup(snap, set())
        assert mode == "RESTRICTED"

    def test_restricted_blocks_entries(self, patch_fetch, tmp_path):
        """When startup_mode=RESTRICTED, entry signals are suppressed."""
        engine, broker, _ = _engine_with_db(patch_fetch, tmp_path)
        engine._startup_mode = "RESTRICTED"
        engine._session_start_equity = 100_000.0
        snap = _snapshot()
        # Override strategy to emit an entry.
        engine.slots[0].strategy._entries = [False] * 59 + [True]

        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        broker.place_order.assert_not_called()

    def test_restricted_auto_clears_after_cycle(self, patch_fetch, tmp_path):
        """RESTRICTED mode becomes NORMAL after one full cycle completes."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        snap = _snapshot(positions=positions)
        engine, broker, tl = _engine_with_db(
            patch_fetch, tmp_path, positions=positions, snapshot=snap
        )
        _write_buy(tl, "AAPL", "retired_strategy")  # causes conflict → RESTRICTED

        engine.start(max_cycles=1)

        assert engine._startup_mode == "NORMAL"

    def test_normal_mode_allows_entries(self, patch_fetch, tmp_path):
        """When startup_mode=NORMAL, entries proceed through risk normally."""
        engine, broker, _ = _engine_with_db(patch_fetch, tmp_path)
        engine._startup_mode = "NORMAL"
        engine._session_start_equity = 100_000.0
        snap = _snapshot()
        engine.slots[0].strategy._entries = [False] * 59 + [True]

        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        broker.place_order.assert_called_once()
