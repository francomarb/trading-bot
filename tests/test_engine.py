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

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from engine.trader import EngineConfig, TradingEngine
from execution.broker import (
    BrokerSnapshot,
    OpenOrder,
    OrderResult,
    OrderStatus,
)
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
            symbols=["AAPL"],
            timeframe="1Day",
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
            risk=risk,
            broker=broker,
            config=cfg,
            clock=lambda: T0,
        )
        return engine, broker

    return _factory


# ── EngineConfig ─────────────────────────────────────────────────────────────


class TestEngineConfig:
    def test_empty_symbols_rejected(self):
        with pytest.raises(ValueError, match="symbols"):
            EngineConfig(symbols=[])

    def test_unsupported_timeframe_rejected(self):
        with pytest.raises(ValueError, match="timeframe"):
            EngineConfig(symbols=["AAPL"], timeframe="1Week")

    def test_negative_cycle_interval_rejected(self):
        with pytest.raises(ValueError):
            EngineConfig(symbols=["AAPL"], cycle_interval_seconds=0)

    def test_max_bar_age_multiplier_must_be_above_one(self):
        with pytest.raises(ValueError):
            EngineConfig(symbols=["AAPL"], max_bar_age_multiplier=1.0)

    def test_bar_interval_and_max_bar_age_derive_from_timeframe(self):
        cfg = EngineConfig(
            symbols=["AAPL"], timeframe="1Hour", max_bar_age_multiplier=2.0
        )
        assert cfg.bar_interval == timedelta(hours=1)
        assert cfg.max_bar_age == timedelta(hours=2)


# ── _process_symbol: every branch ────────────────────────────────────────────


class TestProcessSymbol:
    def test_entry_signal_no_position_places_order(self, engine_factory):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        engine._process_symbol("AAPL", snap)
        assert broker.place_order.call_count == 1
        decision = broker.place_order.call_args.args[0]
        assert decision.symbol == "AAPL"
        assert decision.side is Side.BUY
        broker.close_position.assert_not_called()

    def test_entry_signal_with_existing_position_no_order(self, engine_factory):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        positions = {
            "AAPL": Position("AAPL", 10, 100.0, 1010.0),
        }
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity
        engine._process_symbol("AAPL", snap)
        # Risk would reject DUPLICATE_POSITION → no place_order.
        broker.place_order.assert_not_called()
        broker.close_position.assert_not_called()

    def test_exit_signal_with_position_calls_close(self, engine_factory):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity
        engine._process_symbol("AAPL", snap)
        broker.close_position.assert_called_once_with("AAPL")
        broker.place_order.assert_not_called()

    def test_exit_signal_with_no_position_does_nothing(self, engine_factory):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        engine._process_symbol("AAPL", snap)
        broker.close_position.assert_not_called()
        broker.place_order.assert_not_called()

    def test_no_signal_no_action(self, engine_factory):
        engine, broker = engine_factory()
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        engine._process_symbol("AAPL", snap)
        broker.place_order.assert_not_called()
        broker.close_position.assert_not_called()

    def test_stale_data_skips_silently(self, engine_factory, patch_fetch):
        # Bars from 30 days ago — easily past max_bar_age (10×1day).
        old_end = T0 - timedelta(days=30)
        patch_fetch["df"] = _bars(end=old_end)
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        engine._process_symbol("AAPL", snap)
        broker.place_order.assert_not_called()

    def test_fetch_failure_caught_no_crash(self, engine_factory, patch_fetch):
        patch_fetch["raises"] = RuntimeError("boom")
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        # Should not raise.
        engine._process_symbol("AAPL", snap)
        broker.place_order.assert_not_called()

    def test_pending_close_order_blocks_redundant_close(self, engine_factory):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions, open_orders=[_open_sell_order("AAPL")])
        engine._session_start_equity = snap.account.equity
        engine._process_symbol("AAPL", snap)
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
        # Multi-symbol config; first symbol's fetch raises, second succeeds.
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            config_overrides={"symbols": ["BAD", "AAPL"]},
        )
        engine._session_start_equity = 100_000.0
        engine._cycle_count = 1

        # First call raises, then we let it succeed.
        original = patch_fetch["df"]
        call_count = {"n": 0}

        def _fetch_with_first_bad(symbol, start, end, timeframe="1Day"):
            call_count["n"] += 1
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
    def test_realized_slippage_fed_to_risk(self, engine_factory):
        # Last bar close in synthetic data: see _bars(); 60 bars, base=100,
        # closes = base + (i%7)*0.5 → close[59] = 100 + (59%7)*0.5 = 101.5
        modeled_close = 101.5
        # Realized fill 101.70 — ~19.7 bps slippage on the buy side.
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            place_result=_filled_result("AAPL", 1, modeled_close + 0.20),
        )
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        engine._process_symbol("AAPL", snap)
        # Exactly one slippage sample fed to risk.
        assert len(engine.risk._slippage_samples) == 1
        modeled_bps, realized_bps = engine.risk._slippage_samples[0]
        assert modeled_bps == 0.0
        assert realized_bps == pytest.approx(0.20 / modeled_close * 10_000, rel=1e-3)
