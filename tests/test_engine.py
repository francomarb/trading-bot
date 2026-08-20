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

import json
import os
import time
import sqlite3

from loguru import logger
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from engine.trader import (
    EngineConfig,
    TradingEngine,
    _donchian_exit_observation,
    _lookback_days,
)
from engine.lifecycle_orders import OrderEvent
from execution.broker import (
    AlpacaBroker,
    BrokerOrderAuditSnapshot,
    BrokerSnapshot,
    ClosedOrderInfo,
    OpenOrder,
    OptionQuote,
    OrderResult,
    OrderStatus,
)
from reporting.logger import TradeLogger
from risk.manager import (
    AccountState,
    Position,
    RiskDecision,
    RiskManager,
    Side,
)
from strategies.base import (
    BaseStrategy,
    EdgeFilterDecision,
    OptionTradeRejected,
    OrderType,
    SignalFrame,
    StrategySlot,
)


# ── Fakes ────────────────────────────────────────────────────────────────────


T0 = datetime(2026, 4, 16, 14, 30, tzinfo=timezone.utc)


class FakeStrategy(BaseStrategy):
    """Returns whatever entry/exit pattern the test pins on construction."""

    name = "fake_strategy"
    preferred_order_type = OrderType.MARKET

    def __init__(self, *, entries: list[bool], exits: list[bool], edge_filter=None):
        super().__init__(edge_filter=edge_filter)
        self._entries = entries
        self._exits = exits
        self.raw_calls = 0

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        self.raw_calls += 1
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


class TestDonchianExitObservation:
    """Paper evidence must preserve the exact 10/15-day comparison."""

    def test_uses_prior_close_windows_and_records_both_exit_flags(self):
        df = _bars(n=16)
        df.loc[df.index[-1], "close"] = 99.0

        observation = _donchian_exit_observation(df)

        prior_close = df["close"].iloc[:-1]
        assert observation["close"] == pytest.approx(99.0)
        assert observation["low_10"] == pytest.approx(prior_close.iloc[-10:].min())
        assert observation["low_15"] == pytest.approx(prior_close.iloc[-15:].min())
        assert observation["exit_10"] is True
        assert observation["exit_15"] is True

    def test_keeps_insufficient_history_explicit(self):
        observation = _donchian_exit_observation(_bars(n=10))

        assert observation["low_10"] is None
        assert observation["low_15"] is None
        assert observation["exit_10"] is False
        assert observation["exit_15"] is False

    def test_open_broker_position_emits_observation_without_identity_warning(
        self, engine_factory, monkeypatch
    ):
        """Broker positions carry market state, not the engine position id."""
        engine, _broker = engine_factory(entries=[False] * 60)
        strategy = engine.slots[0].strategy
        strategy.name = "donchian_breakout"
        snapshot = _snapshot(
            positions={"AAPL": Position("AAPL", 10, 100.0, 1_000.0)}
        )
        info = MagicMock()
        warning = MagicMock()
        monkeypatch.setattr("engine.trader.logger.info", info)
        monkeypatch.setattr("engine.trader.logger.warning", warning)

        engine._process_symbol(
            "AAPL", snapshot, snapshot.account, strategy, engine.slots[0].timeframe
        )

        assert any(
            "DONCHIAN_EXIT_OBSERVATION symbol=AAPL position_id=AAPL" in call.args[0]
            for call in info.call_args_list
        )
        assert not any(
            "DONCHIAN_EXIT_OBSERVATION unavailable" in call.args[0]
            for call in warning.call_args_list
        )


def _snapshot(
    *,
    equity: float = 100_000.0,
    previous_close_equity: float | None = None,
    positions: dict[str, Position] | None = None,
    open_orders: list[OpenOrder] | None = None,
) -> BrokerSnapshot:
    return BrokerSnapshot(
        account=AccountState(
            equity=equity,
            cash=equity,
            session_start_equity=equity,
            previous_close_equity=previous_close_equity,
            open_positions=positions or {},
        ),
        open_orders=open_orders or [],
    )


def _filled_result(
    symbol: str,
    qty: int,
    avg: float,
    *,
    submitted_at: datetime | None = None,
    filled_at: datetime | None = None,
    order_id: str | None = None,
) -> OrderResult:
    # Foundation §6.5: trades is one row per order_id (single-leg).
    # Use a symbol-based default so writers that call _filled_result
    # for distinct symbols get distinct order_ids (the legacy
    # ord-1 default would have UPSERTed into a single row).
    return OrderResult(
        status=OrderStatus.FILLED,
        order_id=order_id or f"ord-{symbol}",
        symbol=symbol,
        requested_qty=qty,
        filled_qty=qty,
        avg_fill_price=avg,
        raw_status="filled",
        message="ok",
        submitted_at=submitted_at,
        filled_at=filled_at,
    )


def _unknown_result(symbol: str, qty: int, order_id: str = "ord-unknown") -> OrderResult:
    return OrderResult(
        status=OrderStatus.UNKNOWN,
        order_id=order_id,
        symbol=symbol,
        requested_qty=qty,
        filled_qty=0,
        avg_fill_price=None,
        raw_status=None,
        message="submitted but not confirmed",
    )


def _rejected_result(symbol: str, qty: int) -> OrderResult:
    return OrderResult(
        status=OrderStatus.REJECTED,
        order_id="ord-rejected",
        symbol=symbol,
        requested_qty=qty,
        filled_qty=0,
        avg_fill_price=None,
        raw_status="rejected",
        message="rejected",
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

    def _fetch(symbol, start, end, timeframe="1Day", **kwargs):
        if holder["raises"] is not None:
            raise holder["raises"]
        # Return whatever the test pinned. Stats is not used by the engine,
        # so a simple namespace is fine.
        return holder["df"], SimpleNamespace(api_calls=0)

    monkeypatch.setattr("engine.trader.fetch_symbol", _fetch)
    return holder


@pytest.fixture
def engine_factory(patch_fetch, tmp_path):
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
        # Arrival-quote path: MagicMock's default __float__ returns 1.0 which
        # would pass the engine's finite-positive guard and produce nonsense
        # slippage. Explicitly return None so the entry path falls back to
        # latest_close — preserves pre-arrival-quote test semantics.
        broker.get_latest_quote_midpoint.return_value = None
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

        trade_logger = TradeLogger(path=str(tmp_path / "trades.db"))
        engine = TradingEngine(
            strategy=strategy,
            symbols=["AAPL"],
            risk=risk,
            broker=broker,
            config=cfg,
            trade_logger=trade_logger,
            clock=lambda: T0,
        )
        return engine, broker

    return _factory


# ── EngineConfig ─────────────────────────────────────────────────────────────


class TestStreamHealthObservability:
    def test_outage_and_recovery_alert_once_per_transition(self, engine_factory):
        engine, _broker = engine_factory()
        engine.alerts = MagicMock()
        engine._stream_manager = MagicMock()
        engine._stream_manager.health_snapshot.side_effect = [
            SimpleNamespace(
                connected=True,
                healthy=True,
                generation=1,
                last_rx_at=None,
                last_disconnect_at=None,
                last_reconnect_at=None,
                consecutive_failures=0,
            ),
            SimpleNamespace(
                connected=False,
                healthy=False,
                generation=1,
                last_rx_at=None,
                last_disconnect_at="2026-05-07T12:00:00+00:00",
                last_reconnect_at=None,
                consecutive_failures=1,
            ),
            SimpleNamespace(
                connected=False,
                healthy=False,
                generation=1,
                last_rx_at=None,
                last_disconnect_at="2026-05-07T12:00:00+00:00",
                last_reconnect_at=None,
                consecutive_failures=2,
            ),
            SimpleNamespace(
                connected=True,
                healthy=True,
                generation=2,
                last_rx_at=None,
                last_disconnect_at="2026-05-07T12:00:00+00:00",
                last_reconnect_at="2026-05-07T12:01:00+00:00",
                consecutive_failures=0,
            ),
        ]

        engine._observe_stream_health()  # seed
        engine._observe_stream_health()  # outage
        engine._observe_stream_health()  # no duplicate
        engine._observe_stream_health()  # recovery

        assert engine.alerts.broker_error.call_count == 1
        assert engine.alerts.broker_info.call_count == 1
        outage_msg = engine.alerts.broker_error.call_args_list[0].args[0]
        recovery_msg = engine.alerts.broker_info.call_args_list[0].args[0]
        assert "stream unhealthy" in outage_msg
        assert "stream healthy again" in recovery_msg


class TestMlegEndOfSessionBypass:
    """Engine should skip the walk and submit market when too close to the bell.

    Pinning the threshold semantics here means a future timezone or
    DST change can't silently break the EOS protection."""

    def _make_engine(self, tmp_path) -> TradingEngine:
        api = MagicMock()
        broker = AlpacaBroker(client=api)
        risk = RiskManager(
            max_position_pct=0.02, max_open_positions=5,
            max_gross_exposure_pct=0.50, atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0,
            loss_streak_threshold=10, broker_error_threshold=1,
        )
        return TradingEngine(
            strategy=FakeStrategy(entries=[False], exits=[False]),
            symbols=["AAPL"], risk=risk, broker=broker,
            trade_logger=TradeLogger(path=str(tmp_path / "trades.db")),
        )

    def _et(self, year, month, day, hour, minute):
        # Build a UTC datetime that represents the given Eastern wall time.
        # America/New_York is UTC-4 (EDT) in June.
        from zoneinfo import ZoneInfo
        return datetime(year, month, day, hour, minute, tzinfo=ZoneInfo("America/New_York"))

    def test_no_bypass_in_morning(self, tmp_path):
        engine = self._make_engine(tmp_path)
        # 09:35 ET — plenty of session left.
        morning = self._et(2026, 6, 8, 9, 35)
        assert engine._mleg_should_bypass_walk(now=morning) is False

    def test_bypass_fires_in_final_minutes(self, tmp_path):
        engine = self._make_engine(tmp_path)
        # 15:58 ET — 120 seconds to close, well under the 210s threshold.
        late = self._et(2026, 6, 8, 15, 58)
        assert engine._mleg_should_bypass_walk(now=late) is True

    def test_no_bypass_in_safe_window(self, tmp_path):
        engine = self._make_engine(tmp_path)
        # 15:52 ET — 480 seconds to close, above the 210s threshold.
        safe = self._et(2026, 6, 8, 15, 52)
        assert engine._mleg_should_bypass_walk(now=safe) is False

    def test_no_bypass_after_session_close(self, tmp_path):
        engine = self._make_engine(tmp_path)
        # 16:30 ET — session ended; the engine shouldn't be dispatching
        # closes here, but defensively we return False not True.
        after = self._et(2026, 6, 8, 16, 30)
        assert engine._mleg_should_bypass_walk(now=after) is False

    def test_no_bypass_before_session_open(self, tmp_path):
        engine = self._make_engine(tmp_path)
        # 08:30 ET — pre-market.
        pre = self._et(2026, 6, 8, 8, 30)
        assert engine._mleg_should_bypass_walk(now=pre) is False


class TestEngineConfig:
    def test_engine_binds_broker_entry_guard(self, tmp_path):
        api = MagicMock()
        broker = AlpacaBroker(client=api)
        risk = RiskManager(
            max_position_pct=0.02,
            max_open_positions=5,
            max_gross_exposure_pct=0.50,
            atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.05,
            hard_dollar_loss_cap=1_000_000.0,
            loss_streak_threshold=10,
            broker_error_threshold=1,
        )

        TradingEngine(
            strategy=FakeStrategy(entries=[False], exits=[False]),
            symbols=["AAPL"],
            risk=risk,
            broker=broker,
            trade_logger=TradeLogger(path=str(tmp_path / "trades.db")),
        )
        assert broker._entries_allowed()

        risk.record_broker_error()

        assert not broker._entries_allowed()

    def test_negative_cycle_interval_rejected(self):
        with pytest.raises(ValueError):
            EngineConfig(cycle_interval_seconds=0)

    def test_max_bar_age_multiplier_must_be_above_one(self):
        with pytest.raises(ValueError):
            EngineConfig(max_bar_age_multiplier=1.0)

    def test_daily_engine_default_keeps_200_sma_warmup_margin(self):
        from config import settings

        if settings.ENGINE_TIMEFRAME == "1Day":
            assert settings.ENGINE_HISTORY_LOOKBACK_DAYS >= 300


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
        assert _lookback_days(100, "2Min", config_lookback=10) == int(100 * 1.5) + 5


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
        broker.close_position.assert_called_once_with("AAPL", position_uid=None)
        broker.place_order.assert_not_called()

    def test_global_halt_does_not_block_exit(self, engine_factory):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        for _ in range(10):
            engine.risk.record_broker_error()
        assert engine.risk.is_halted()

        self._process(engine, "AAPL", snap)

        broker.close_position.assert_called_once_with("AAPL", position_uid=None)
        broker.place_order.assert_not_called()

    # ── Phase B PR-65 review F5: prove _process_symbol enforces the
    #    pause-entries / pause-strategy gates BEFORE broker dispatch ──

    def test_pause_entries_blocks_broker_place_order(self, engine_factory):
        """A fresh entry signal under pause-entries must NOT reach
        broker.place_order. The existing halt check is the outer gate;
        Phase B added soft pauses underneath. This proves the new gate
        is actually in `_process_symbol`, not just on the RiskManager
        flag accessor."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity

        engine.risk.pause_entries(reason="market event", command_uid="cmd_abc")
        assert engine.risk.is_entries_paused()

        self._process(engine, "AAPL", snap)

        broker.place_order.assert_not_called()
        # And the halt mechanism stayed clean — pause should NOT engage
        # the kill switch.
        assert not engine.risk.is_halted()

    def test_pause_entries_does_not_block_exits(self, engine_factory):
        """Soft pause only blocks new entries. An existing position
        with an exit signal must still close — same invariant
        `test_global_halt_does_not_block_exit` proves for halt."""
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        engine.risk.pause_entries(reason="t", command_uid="c")

        self._process(engine, "AAPL", snap)

        broker.close_position.assert_called_once_with("AAPL", position_uid=None)
        broker.place_order.assert_not_called()

    # ── Phase C PR-66 review F4: strategy exits must honor the
    #    symbol-lock so a cycle-thread strategy exit cannot race a
    #    heartbeat-thread operator close on the same owner_key. ──

    def test_strategy_exit_acquires_and_releases_symbol_lock(
        self, engine_factory,
    ):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        self._process(engine, "AAPL", snap)

        broker.close_position.assert_called_once_with(
            "AAPL", position_uid=None,
        )
        # Lock released after the close path completes.
        assert engine.symbol_locks.is_locked("AAPL") is None

    def test_strategy_exit_skipped_when_owner_key_locked_by_operator(
        self, engine_factory,
    ):
        """If the operator already holds the symbol lock (close-position
        / reduce-position in flight), the strategy exit must skip and
        let the operator command finish. Otherwise both threads can
        SELL the same shares and oversell."""
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        # Pre-acquire the lock as if an operator command is mid-flight.
        engine.symbol_locks.acquire(
            owner_key="AAPL", kind="operator_command", identifier="cmd_xyz",
        )

        self._process(engine, "AAPL", snap)

        # Strategy exit must NOT have submitted a SELL.
        broker.close_position.assert_not_called()
        # And the operator's lock is still held.
        h = engine.symbol_locks.is_locked("AAPL")
        assert h is not None
        assert h.kind == "operator_command"

    def test_pause_strategy_is_scoped_to_one_strategy(self, engine_factory):
        """pause-strategy <name> blocks new entries for that strategy
        only. Other strategies in the same process should be
        unaffected. We exercise the scope with the engine's first slot
        (whose strategy is the FakeStrategy named 'fake_strategy').
        Pausing 'sma_crossover' must NOT block 'fake_strategy'; pausing
        'fake_strategy' MUST block it."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot_strategy_name = engine.slots[0].strategy.name

        # Pausing a DIFFERENT strategy does not affect the slot's
        # strategy — entry proceeds.
        engine.risk.pause_strategy(
            strategy_name="sma_crossover", reason="other slot",
            command_uid="cmd_x",
        )
        self._process(engine, "AAPL", snap)
        assert broker.place_order.call_count == 1
        broker.place_order.reset_mock()

        # Pausing the slot's OWN strategy blocks the entry.
        engine.risk.resume_strategy(strategy_name="sma_crossover")
        engine.risk.pause_strategy(
            strategy_name=slot_strategy_name, reason="this slot",
            command_uid="cmd_y",
        )
        # Need a fresh signal bar — clear processed-signal cache so
        # the entry decision re-fires.
        engine._processed_signal_bars.clear()
        self._process(engine, "AAPL", snap)
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

    def test_processed_bar_still_runs_single_leg_emergency_exit(
        self, engine_factory, patch_fetch
    ):
        occ = "SPY260626C00730000"

        class _EmergencyExitStrategy(FakeStrategy):
            name = "spy_options_reversion"

            def __init__(self):
                super().__init__(entries=[False], exits=[False])
                self.inspect_calls = 0

            def inspect_open_positions(self, position, latest_close: float) -> bool:
                self.inspect_calls += 1
                return True

        strategy = _EmergencyExitStrategy()
        close_result = _filled_result(occ, 1, 9.0)
        engine, broker = engine_factory(close_result=close_result)
        engine._allocator = MagicMock()
        engine.slots[0].strategy = strategy
        engine._register_single_leg(strategy_name=strategy.name, symbol=occ)
        engine._entry_prices["SPY"] = 10.0
        signal_key = (strategy.name, "SPY", engine.slots[0].timeframe)
        engine._processed_signal_bars[signal_key] = pd.Timestamp(
            patch_fetch["df"].index[-1]
        )
        position = Position(occ, 1, 10.0, 1_000.0, current_price=9.0)
        snap = _snapshot(positions={occ: position})
        engine._session_start_equity = snap.account.equity

        self._process(engine, "SPY", snap)

        assert strategy.inspect_calls == 1
        assert strategy.raw_calls == 0
        broker.close_position.assert_called_once_with(occ, position_uid=None)
        broker.place_order.assert_not_called()
        assert not engine._has_position("SPY")
        assert "SPY" not in engine._entry_prices
        engine._allocator.record_realized_pnl.assert_called_once_with(
            strategy.name,
            -100.0,
            position_uid=None,
            is_full_close=True,
        )

    def test_processed_bar_still_retries_single_leg_signal_exit(
        self, engine_factory, patch_fetch
    ):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        signal_key = ("fake_strategy", "AAPL", engine.slots[0].timeframe)
        engine._processed_signal_bars[signal_key] = pd.Timestamp(
            patch_fetch["df"].index[-1]
        )
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1_010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        self._process(engine, "AAPL", snap)

        broker.close_position.assert_called_once_with("AAPL", position_uid=None)

    def test_processed_bar_signal_exit_respects_position_owner(
        self, engine_factory, patch_fetch
    ):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        engine._register_single_leg(strategy_name="donchian_breakout", symbol="AAPL")
        signal_key = ("fake_strategy", "AAPL", engine.slots[0].timeframe)
        engine._processed_signal_bars[signal_key] = pd.Timestamp(
            patch_fetch["df"].index[-1]
        )
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1_010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity
        engine._processed_signal_statuses[signal_key] = "No Signal"
        engine._processed_signal_reasons[signal_key] = ["owner mismatch"]
        statuses = {"AAPL": "stale"}
        reasons = {"AAPL": ["stale"]}

        engine._process_symbol(
            "AAPL",
            snap,
            snap.account,
            engine.slots[0].strategy,
            engine.slots[0].timeframe,
            strategy_statuses=statuses,
            strategy_reasons=reasons,
        )

        broker.close_position.assert_not_called()
        assert engine._get_owner("AAPL") == "donchian_breakout"
        assert statuses["AAPL"] == "No Signal"
        assert reasons["AAPL"] == ["owner mismatch"]

    def test_unfilled_single_leg_exit_retains_ownership_for_retry(self, engine_factory):
        engine, broker = engine_factory(
            exits=[False] * 59 + [True],
            close_result=_rejected_result("AAPL", 10),
        )
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.0
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1_010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        self._process(engine, "AAPL", snap)

        broker.close_position.assert_called_once_with("AAPL", position_uid=None)
        assert engine._has_position("AAPL")
        assert engine._entry_prices["AAPL"] == 100.0

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

    def test_protective_stop_does_not_block_signal_close(self, engine_factory):
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions, open_orders=[_open_stop_order("AAPL")])
        engine._session_start_equity = snap.account.equity

        self._process(engine, "AAPL", snap)

        broker.close_position.assert_called_once_with("AAPL", position_uid=None)

    def test_option_trade_rejected_logs_warning_and_skips_order(
        self, engine_factory, monkeypatch
    ):
        class _OptionStrategy(FakeStrategy):
            name = "spy_options_reversion"
            preferred_order_type = OrderType.LIMIT

            def build_option_execution(self, symbol, latest_close, *, notional_cap=None):
                raise OptionTradeRejected(
                    "SPY260521C00730000: spread 12.6% > 5% (bid=8.73 ask=9.90) — skipping trade."
                )

        engine, broker = engine_factory()
        engine.slots[0].strategy = _OptionStrategy(entries=[False] * 59 + [True], exits=[False] * 60)
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity

        warnings: list[str] = []
        errors: list[str] = []
        monkeypatch.setattr("engine.trader.logger.warning", lambda msg: warnings.append(msg))
        monkeypatch.setattr("engine.trader.logger.error", lambda msg: errors.append(msg))

        self._process(engine, "SPY", snap)

        broker.place_order.assert_not_called()
        assert any("Option trade rejected for SPY" in msg for msg in warnings)
        assert not any("Failed to build option execution for SPY" in msg for msg in errors)

    def test_async_option_dispatch_registers_position_with_occ_leg(
        self, engine_factory
    ):
        """The async (ACCEPTED) options path must register the Position with
        the OCC contract as its leg symbol — not the strategy's underlying.

        Regression: registering with `symbol` ("SPY") instead of
        `target_symbol` (the OCC string) left primary_leg.symbol == "SPY",
        which broke the single-leg-option contract and made
        _compute_sector_exposure() miscount SPY options as equity exposure.
        """
        occ = "SPY260521C00730000"

        class _OptionStrategy(FakeStrategy):
            name = "spy_options_reversion"
            preferred_order_type = OrderType.LIMIT

            def build_option_execution(self, symbol, latest_close, *, notional_cap=None):
                return (occ, 9.30, None, None)

        accepted = OrderResult(
            status=OrderStatus.ACCEPTED,
            order_id="ord-async",
            symbol=occ,
            requested_qty=1,
            filled_qty=0,
            avg_fill_price=None,
            raw_status="accepted",
            message="dispatched to options worker",
        )
        engine, broker = engine_factory(place_result=accepted)
        engine.slots[0].strategy = _OptionStrategy(
            entries=[False] * 59 + [True], exits=[False] * 60
        )
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity

        self._process(engine, "SPY", snap)

        # Position is keyed by the underlying, but the leg carries the OCC.
        assert "SPY" in engine._positions
        pos = engine._positions["SPY"]
        assert pos.position_id == "SPY"
        assert pos.primary_leg is not None
        assert pos.primary_leg.symbol == occ
        assert pos.strategy_name == "spy_options_reversion"

        # Sector exposure must exclude the option position (OCC leg).
        resolver = MagicMock()
        resolver.resolve.return_value = "technology"
        engine._sector_resolver = resolver
        assert engine._compute_sector_exposure() == {}
        resolver.resolve.assert_not_called()


# ── _run_one_cycle ───────────────────────────────────────────────────────────


class TestRunOneCycle:
    def test_broker_snapshot_engages_restart_safe_account_halt(
        self, engine_factory
    ):
        snap = _snapshot(equity=98_000.0, previous_close_equity=100_000.0)
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            snapshot=snap,
            market_open=True,
        )
        engine.risk.hard_dollar_loss_cap = 2_000.0
        engine.risk.max_daily_loss_pct = 0.99
        engine._session_start_equity = 98_000.0
        engine._cycle_count = 1

        engine._run_one_cycle()

        assert engine.risk.is_halted()
        assert "previous close" in (engine.risk.halt_reason() or "")
        broker.place_order.assert_not_called()

    def test_broker_snapshot_clears_stale_account_halt_before_signal_gate(
        self, engine_factory
    ):
        snap = _snapshot(equity=99_000.0, previous_close_equity=100_500.0)
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            snapshot=snap,
            market_open=True,
        )
        engine.risk.hard_dollar_loss_cap = 2_000.0
        engine.risk.max_daily_loss_pct = 0.99
        engine.risk.evaluate_account(
            AccountState(
                equity=98_000.0,
                cash=98_000.0,
                session_start_equity=98_000.0,
                previous_close_equity=100_000.0,
                open_positions={},
            )
        )
        assert engine.risk.is_halted()
        engine._session_start_equity = 98_000.0
        engine._cycle_count = 1

        engine._run_one_cycle()

        assert not engine.risk.is_halted()
        broker.place_order.assert_called_once()

    def test_market_closed_skips_cycle(self, engine_factory):
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            market_open=False,
            config_overrides={"market_hours_only": True},
        )
        engine._session_start_equity = 100_000.0
        engine._cycle_count = 1
        engine._run_one_cycle()
        broker.sync_with_broker.assert_called_once()
        broker.place_order.assert_not_called()

    def test_market_closed_cycle_still_checks_protective_stop_durability(
        self, engine_factory
    ):
        snap = _snapshot(
            positions={"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        )
        engine, broker = engine_factory(
            market_open=False,
            snapshot=snap,
            config_overrides={"market_hours_only": True},
        )
        engine._repair_missing_protective_stops = MagicMock()

        engine._run_one_cycle()

        engine._repair_missing_protective_stops.assert_called_once_with(
            snap,
            allow_residual_cleanup=False,
        )
        broker.place_order.assert_not_called()

    def test_market_closed_stop_check_does_not_close_fractional_residual(
        self, engine_factory
    ):
        snap = _snapshot(
            positions={"AAPL": Position("AAPL", 0.5, 100.0, 50.0)}
        )
        engine, _broker = engine_factory(
            market_open=False,
            snapshot=snap,
            config_overrides={"market_hours_only": True},
        )
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine.trade_logger.read_latest_open_stop_price = MagicMock(
            return_value=95.0
        )
        engine._close_fractional_residual_position = MagicMock()

        engine._run_one_cycle()

        engine._close_fractional_residual_position.assert_not_called()

    def test_market_closed_cycle_refreshes_watchlist_statuses_from_snapshot(
        self, engine_factory
    ):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine, broker = engine_factory(
            market_open=False,
            snapshot=snap,
            config_overrides={"market_hours_only": True},
        )
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._run_one_cycle()
        broker.sync_with_broker.assert_called_once()
        assert engine._watchlist_statuses["fake_strategy"]["AAPL"] == "Long"

    def test_market_closed_cycle_preserves_blocked_status_when_flat(
        self, engine_factory
    ):
        engine, broker = engine_factory(
            market_open=False,
            snapshot=_snapshot(),
            config_overrides={"market_hours_only": True},
        )
        engine._watchlist_statuses = {"fake_strategy": {"AAPL": "Regime Blocked"}}
        engine._run_one_cycle()
        broker.sync_with_broker.assert_called_once()
        assert engine._watchlist_statuses["fake_strategy"]["AAPL"] == "Regime Blocked"

    def test_market_closed_cycle_updates_last_known_regime(self, engine_factory):
        engine, broker = engine_factory(
            market_open=False,
            snapshot=_snapshot(),
            config_overrides={"market_hours_only": True},
        )
        fake_regime = MagicMock()
        fake_regime.detect.return_value = SimpleNamespace(value="ranging")
        engine._regime_detector = fake_regime
        engine._run_one_cycle()
        broker.sync_with_broker.assert_called_once()
        fake_regime.detect.assert_called_once()
        assert engine._last_regime == "ranging"

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
        broker.sync_with_broker.assert_called_once()

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

        def _fetch_with_first_bad(symbol, start, end, timeframe="1Day", **kwargs):
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

    def test_market_open_daily_cycle_ignores_in_progress_bar(
        self, engine_factory, patch_fetch
    ):
        # Alpaca daily bars are bucketed at New York midnight. During market
        # hours the latest such bar is still in progress and must be excluded
        # from live signal generation.
        patch_fetch["df"] = _bars(
            end=datetime(2026, 4, 16, 4, 0, tzinfo=timezone.utc)
        )
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            market_open=True,
        )
        engine._session_start_equity = 100_000.0
        engine._cycle_count = 1

        engine._run_one_cycle()

        broker.place_order.assert_not_called()

    def test_market_open_daily_cycle_processes_completed_bar_only_once(
        self, engine_factory, patch_fetch
    ):
        patch_fetch["df"] = _bars(
            end=datetime(2026, 4, 16, 4, 0, tzinfo=timezone.utc)
        )
        engine, broker = engine_factory(market_open=True)
        engine._session_start_equity = 100_000.0

        slot = engine.slots[0]
        assert isinstance(slot.strategy, FakeStrategy)

        engine._cycle_count = 1
        engine._run_one_cycle()
        engine._cycle_count = 2
        engine._run_one_cycle()

        assert slot.strategy.raw_calls == 1
        broker.place_order.assert_not_called()

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
        # An arrival quote is required for the fill to reach the kill
        # switch at all — the factory's default None yields a
        # `fallback_latest_close` benchmark, which is implementation
        # shortfall and is filtered out. Anchoring the quote at
        # `modeled_close` keeps the bps arithmetic below unchanged.
        broker.get_latest_quote_midpoint.return_value = modeled_close
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)

        assert len(engine.risk._slippage_samples) == 1
        modeled_bps, realized_bps = engine.risk._slippage_samples[0]
        # MARKET order → modeled cost is the configured baseline, not 0.
        assert modeled_bps == pytest.approx(SLIPPAGE_MODEL_MARKET_BPS)
        assert realized_bps == pytest.approx(0.20 / modeled_close * 10_000, rel=1e-3)

    def test_buy_adverse_fill_records_positive_signed_bps(self, engine_factory):
        """Kill-switch path uses adverse-only semantics now. A BUY filled
        ABOVE the arrival benchmark = paid more = positive signed bps,
        which is exactly the kind of drift the kill switch is supposed
        to catch. Sample should record the adverse magnitude."""
        modeled_close = 100.0
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            place_result=_filled_result("AAPL", 1, 100.20),  # paid 20¢ more
        )
        broker.get_latest_quote_midpoint.return_value = modeled_close
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        assert len(engine.risk._slippage_samples) == 1
        _, realized = engine.risk._slippage_samples[0]
        # Adverse fill → positive signed bps; not clamped.
        assert realized == pytest.approx((0.20 / 100.0) * 10_000, rel=1e-3)

    def test_buy_price_improvement_clamps_to_zero(self, engine_factory):
        """Symmetry guard: a BUY filled BELOW the arrival benchmark = got
        a better price than expected. Adverse-only semantics clamps to 0
        so a run of unusually good fills can't trip the drift kill
        switch on improvement that the strategy should be happy about.
        Mirrors the credit_spread MLEG false-positive at the engine
        kill-switch layer."""
        modeled_close = 100.0
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            place_result=_filled_result("AAPL", 1, 99.80),  # paid 20¢ less
        )
        broker.get_latest_quote_midpoint.return_value = modeled_close
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        assert len(engine.risk._slippage_samples) == 1
        _, realized = engine.risk._slippage_samples[0]
        # Price improvement → clamped to 0; kill switch sees a clean fill.
        assert realized == pytest.approx(0.0)

    def test_limit_order_skips_slippage_recording(self, engine_factory):
        """LIMIT entries do not record execution slippage — arrival price
        is not a meaningful benchmark for a resting limit fill. A buy
        limit at $100 filled at $95 is a clean fill against the limit;
        recording -500 bps against arrival would falsely trip the drift
        kill switch and the L2 health check. LIMIT execution quality
        belongs in a separate limit-fill-vs-limit-price metric (not in
        this PR's scope)."""
        modeled_close = 101.5
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            place_result=_filled_result("AAPL", 1, modeled_close + 0.05),
        )
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        slot.strategy.preferred_order_type = OrderType.LIMIT
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
        # No slippage sample recorded — the kill switch is not fed from
        # LIMIT entries (the assertion that flipped vs. the old test).
        assert len(engine.risk._slippage_samples) == 0


class TestArrivalQuoteCapture:
    """Issue B in the slippage PR: realized_slippage_bps must measure
    fill-vs-arrival (execution slippage), not fill-vs-signal-close
    (Implementation Shortfall). The engine fetches an arrival quote
    immediately before submission via broker.get_latest_quote_midpoint
    and threads it through to build_record as the slippage benchmark.
    """

    def test_arrival_quote_fetched_before_order_submission(self, engine_factory):
        """The engine must call get_latest_quote_midpoint per entry
        attempt — otherwise the slippage measurement falls back to the
        decision-time close (the Issue B failure mode)."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        broker.get_latest_quote_midpoint.assert_called_with("AAPL")

    def test_arrival_quote_used_as_slippage_benchmark_when_available(
        self, engine_factory,
    ):
        """When the broker returns a usable quote, realized_bps measures
        fill-vs-arrival, not fill-vs-decision-close."""
        modeled_close = 100.0
        fill_price = 100.20
        arrival_price = 100.15  # arrival is between decision and fill
        engine, broker = engine_factory(
            entries=[False] * 59 + [True],
            place_result=_filled_result("AAPL", 1, fill_price),
        )
        broker.get_latest_quote_midpoint.return_value = arrival_price
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)

        # Expected: realized_bps measures fill-vs-arrival, NOT fill-vs-
        # decision-close. The exact decision-time close is whatever the
        # synthetic bar fixture produced; key invariant is that the
        # realized_bps reflects (fill − arrival) / arrival × 10_000, a
        # much smaller delta than (fill − decision_close).
        assert len(engine.risk._slippage_samples) == 1
        _, realized_bps = engine.risk._slippage_samples[0]
        expected = (fill_price - arrival_price) / arrival_price * 10_000
        assert realized_bps == pytest.approx(expected, rel=1e-3)

    def test_falls_back_to_decision_close_when_quote_unavailable(
        self, engine_factory,
    ):
        """Arrival quote of None (one-sided book, API failure) → the row
        is still written, tagged as the fallback it is, but it must NOT
        reach the drift kill switch.

        Fill-vs-prior-close is implementation shortfall, not execution
        quality, and the design doc forbids feeding that family to the
        drift alarm. A broken quote feed blinds the *alarm* rather than
        poisoning it with market movement.
        """
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        broker.get_latest_quote_midpoint.return_value = None
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        assert len(engine.risk._slippage_samples) == 0

    def test_rejects_non_finite_quote_from_broker(self, engine_factory):
        """Defensive: broker returns NaN / negative / zero (Mock-style
        misbehavior) → engine treats as no quote, falls back to the
        prior close for the row, and withholds it from the kill switch
        for the same reason as the None case."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        broker.get_latest_quote_midpoint.return_value = float("nan")
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        # Must not raise into the trading loop.
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        assert len(engine.risk._slippage_samples) == 0


class TestOrderTypeAwareSubmitSlippageTagging:
    """PR #68 round-1 review P1. The engine's submit path passes
    `slippage_benchmark_kind` / `_measurement_quality` / `_price` /
    `_timestamp` into `broker.place_order` so the substrate's per-
    order row captures provenance at submit time. The trades-row
    UPSERT policy (PRESERVE-FIRST-NON-NULL via COALESCE) means
    whatever the substrate writes first survives every later UPSERT
    on the same `order_id`, including the recovery completeness
    call in `_maybe_dispatch_substrate_entry_fill`.

    Pre-fix the engine used `'arrival_midpoint'` / `'primary'` for
    every entry regardless of `decision.order_type`, so LIMIT
    entries (RSI reversion) and STOP_LIMIT entries (Donchian
    breakout) locked in a wrong provenance tag that no later
    writer could correct — the dashboard / health / calibration
    consumers saw `quality='primary'` paired with NULL
    `slippage_signed_bps`, the exact smell PR #67 set out to
    eliminate.

    Fix: when `decision.order_type` is LIMIT or STOP_LIMIT, the
    submit path forces `kind='limit_price'`, `quality='unavailable'`,
    `benchmark_price=None`, `benchmark_timestamp=None`. This
    mirrors codepath §2 in
    `docs/slippage_unification_design.md` and matches what
    `build_record` produces for non-market orders on the
    synchronous-fill `_log_entry` path.
    """

    def _patch_order_type(self, engine, order_type):
        engine.slots[0].strategy.preferred_order_type = order_type

    def test_market_entry_passes_arrival_midpoint_primary(
        self, engine_factory,
    ):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        broker.get_latest_quote_midpoint.return_value = 100.25
        self._patch_order_type(engine, OrderType.MARKET)
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol(
            "AAPL", snap, snap.account, slot.strategy, slot.timeframe,
        )
        kwargs = broker.place_order.call_args.kwargs
        assert kwargs["slippage_benchmark_kind"] == "arrival_midpoint"
        assert kwargs["slippage_measurement_quality"] == "primary"
        assert kwargs["slippage_benchmark_price"] == pytest.approx(100.25)
        assert kwargs["slippage_benchmark_timestamp"] is not None

    def test_market_entry_falls_back_to_latest_close_fallback_quality(
        self, engine_factory,
    ):
        """No arrival quote available → fallback_latest_close /
        fallback quality, benchmark_price = latest_close. This was
        already the pre-fix behaviour for MARKET; pinning here so
        the post-fix branching doesn't regress it."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        broker.get_latest_quote_midpoint.return_value = None
        self._patch_order_type(engine, OrderType.MARKET)
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol(
            "AAPL", snap, snap.account, slot.strategy, slot.timeframe,
        )
        kwargs = broker.place_order.call_args.kwargs
        assert kwargs["slippage_benchmark_kind"] == "fallback_latest_close"
        assert kwargs["slippage_measurement_quality"] == "fallback"
        assert kwargs["slippage_benchmark_price"] is not None

    def test_limit_entry_forces_limit_price_unavailable(
        self, engine_factory,
    ):
        """Even with an arrival quote available, a LIMIT decision
        gets `limit_price` / `unavailable` and a NULL benchmark_price.
        This is the canonical fix: the substrate must mirror the
        synchronous `build_record` contract so the trades-row UPSERT
        COALESCE doesn't lock in the wrong tag and survive the
        recovery completeness call."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        broker.get_latest_quote_midpoint.return_value = 100.25
        self._patch_order_type(engine, OrderType.LIMIT)
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol(
            "AAPL", snap, snap.account, slot.strategy, slot.timeframe,
        )
        kwargs = broker.place_order.call_args.kwargs
        assert kwargs["slippage_benchmark_kind"] == "limit_price"
        assert kwargs["slippage_measurement_quality"] == "unavailable"
        assert kwargs["slippage_benchmark_price"] is None
        assert kwargs["slippage_benchmark_timestamp"] is None

    # STOP_LIMIT entries (Donchian breakout) take the same
    # `is_market_order is False` branch as LIMIT in
    # `_process_symbol`'s slippage-tagging logic; covered above.
    # An end-to-end STOP_LIMIT test would require wiring an
    # ENTRY_PRICE_CAPS policy onto the fixture strategy, which
    # adds setup unrelated to the tagging branch being exercised.


class TestOptionsPathSlippageContract:
    """Verify the LIMIT + arrival-quote fix covers the options strategies
    (spy_options_reversion + credit_spread). The options paths share the
    same gates (build_record's market-only slippage, _record_fill's
    market-only kill-switch feed) so neither produces false L2 findings
    after this PR. credit_spread's MLEG fill writes through
    log_spread_fill which has its own correct fill-vs-limit slippage
    measurement — out of scope for the arrival-quote fix but verified
    here for completeness.
    """

    def test_occ_target_skips_arrival_quote_fetch_end_to_end(
        self, engine_factory,
    ):
        """Drive _process_symbol with a strategy that overrides
        target_symbol to an OCC string (the spy_options_reversion
        pattern). The engine must NOT call broker.get_latest_quote_midpoint
        with the OCC — Alpaca's stock quote endpoint can't resolve an
        OPRA symbol and would emit a warning per cycle. If the
        `is_occ_option(target_symbol)` short-circuit at trader.py:1464
        is accidentally removed, this test fails. The previous
        coverage check only verified the helper's regex, not the
        wiring — caught by code review on PR #37."""
        from execution.broker import OrderResult, OrderStatus
        from risk.manager import RiskDecision, Side

        engine, broker = engine_factory(entries=[False] * 59 + [True])
        slot = engine.slots[0]
        occ_symbol = "SPY260618C00746000"

        # Trigger the options branch: build_option_execution returns the
        # OCC contract, mirroring what the real spy_options_reversion
        # strategy does at decision time.
        slot.strategy.build_option_execution = (
            lambda *_args, **_kwargs: (occ_symbol, 12.77, 18.00, 10.00)
        )
        slot.strategy.preferred_order_type = OrderType.LIMIT

        # Bypass the contract-conflict gate — irrelevant to this test.
        engine._reject_if_contract_conflict = MagicMock(return_value=None)

        # Patch risk.evaluate to return a valid LIMIT decision so the
        # flow reaches the arrival-quote site rather than rejecting at
        # the risk layer.
        decision = RiskDecision(
            symbol=occ_symbol, side=Side.BUY, qty=1,
            entry_reference_price=12.77, stop_price=10.00,
            strategy_name="fake_strategy", reason="opt entry",
            order_type=OrderType.LIMIT, limit_price=12.85,
        )
        engine.risk.evaluate = MagicMock(return_value=decision)

        # Options entries route via the async ACCEPTED branch — return
        # ACCEPTED so the engine pre-registers and exits cleanly.
        broker.place_order.return_value = OrderResult(
            status=OrderStatus.ACCEPTED, order_id="async-1", symbol=occ_symbol,
            requested_qty=1, filled_qty=0, avg_fill_price=None,
            raw_status="accepted", message="dispatched",
        )

        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        engine._process_symbol(
            "AAPL", snap, snap.account, slot.strategy, slot.timeframe,
        )

        # Load-bearing assertion: arrival quote was never fetched for
        # the OCC target. If trader.py:1464's short-circuit regresses
        # this fails immediately.
        broker.get_latest_quote_midpoint.assert_not_called()

    def test_equity_target_does_fetch_arrival_quote(self, engine_factory):
        """Symmetry guard for the test above: when the target is a
        normal equity symbol the engine MUST fetch the arrival quote.
        Confirms the OCC short-circuit isn't accidentally broadened to
        skip equities too."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol(
            "AAPL", snap, snap.account, slot.strategy, slot.timeframe,
        )
        broker.get_latest_quote_midpoint.assert_called_with("AAPL")

    def test_options_limit_decision_writes_null_on_both_columns(self, tmp_path):
        """An options entry (LIMIT, OCC symbol) through build_record
        produces NULL on both slippage columns — covered by the
        market-only gate. Repros the spy_options_reversion path's
        post-fix contract."""
        from execution.broker import OrderResult, OrderStatus
        from reporting.logger import TradeLogger
        from risk.manager import RiskDecision, Side

        occ_symbol = "SPY260618C00746000"
        decision = RiskDecision(
            symbol=occ_symbol,
            side=Side.BUY,
            qty=3,
            entry_reference_price=12.77,
            stop_price=10.00,
            strategy_name="spy_options_reversion",
            reason="spy_options_reversion entry @ 2026-05-28T13:59:00+00:00",
            order_type=OrderType.LIMIT,
            limit_price=12.85,
        )
        result = OrderResult(
            status=OrderStatus.FILLED, order_id="opt-1", symbol=occ_symbol,
            requested_qty=3, filled_qty=3,
            avg_fill_price=12.78, raw_status="filled", message="ok",
        )
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        record = tl.build_record(decision, result, modeled_price=12.77)
        # LIMIT gate fires regardless of OCC vs equity — both columns NULL.
        assert record.modeled_slippage_bps is None
        assert record.realized_slippage_bps is None


# ── Multi-slot ──────────────────────────────────────────────────────────────


class TestMultiSlot:
    def test_multi_slot_processes_all_slots(self, patch_fetch, tmp_path):
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
            trade_logger=TradeLogger(path=str(tmp_path / "trades.db")),
            clock=lambda: T0,
        )
        engine.start(max_cycles=1)
        # Both slots should have placed orders.
        assert broker.place_order.call_count == 2

    def test_legacy_single_strategy_api_still_works(self, patch_fetch, tmp_path):
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
            trade_logger=TradeLogger(path=str(tmp_path / "trades.db")),
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
        engine._register_single_leg(strategy_name="other_strategy", symbol="AAPL")

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
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")

        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        broker.close_position.assert_called_once_with("AAPL", position_uid=None)
        # Ownership cleared after close.
        assert not engine._has_position("AAPL")

    def test_exit_allowed_when_no_owner_recorded(self, engine_factory):
        """Pre-existing positions (no recorded owner) can be closed by anyone."""
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity

        # No ownership recorded — should still allow close.
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        broker.close_position.assert_called_once_with("AAPL", position_uid=None)

    def test_entry_registers_ownership(self, engine_factory):
        """A successful entry fill records the strategy as position owner."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity

        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        assert broker.place_order.call_count == 1
        assert engine._get_owner("AAPL") == "fake_strategy"

    def test_startup_seeds_ownership_from_broker(self, engine_factory):
        """On start(), existing broker positions are assigned to matching slots."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        engine, broker = engine_factory(snapshot=_snapshot(positions=positions))
        engine.start(max_cycles=1)
        assert engine._get_owner("AAPL") == "fake_strategy"


class TestWatchlistStatuses:
    def test_baseline_pending_entry_from_open_buy_order(self, engine_factory):
        engine, _ = engine_factory()
        snap = _snapshot(open_orders=[
            OpenOrder(
                order_id="buy-1",
                symbol="AAPL",
                side=Side.BUY,
                qty=1,
                order_type=OrderType.MARKET,
                status="open",
                submitted_at=T0,
                limit_price=None,
                stop_price=None,
            )
        ])
        status = engine._baseline_watchlist_status(
            "AAPL",
            snap,
            strategy_name="fake_strategy",
            order_strategy={"buy-1": "fake_strategy"},
        )
        assert status == "Pending Entry"

    def test_regime_blocked_status_uses_real_entry_signal(self, engine_factory):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        statuses = {"AAPL": "No Signal"}
        reasons = {"AAPL": []}
        engine._process_symbol(
            "AAPL",
            snap,
            snap.account,
            slot.strategy,
            slot.timeframe,
            entry_allowed=False,
            regime_block_reason="regime bear not in allowed set ['trending']",
            strategy_statuses=statuses,
            strategy_reasons=reasons,
        )
        assert statuses["AAPL"] == "Regime Blocked"
        assert reasons["AAPL"] == ["regime bear not in allowed set ['trending']"]
        broker.place_order.assert_not_called()

    def test_filter_blocked_status_when_raw_entry_vetoed(self, engine_factory):
        class _BlockingFilter:
            def __call__(self, df):
                return EdgeFilterDecision(
                    allowed=pd.Series([False] * len(df), index=df.index, dtype=bool),
                    reasons=pd.Series(
                        [["volume contracting", "earnings blackout"] for _ in range(len(df))],
                        index=df.index,
                        dtype=object,
                    ),
                )

        strategy = FakeStrategy(
            entries=[False] * 59 + [True],
            exits=[False],
            edge_filter=_BlockingFilter(),
        )
        engine, broker = engine_factory()
        engine.slots[0].strategy = strategy
        engine.strategy = strategy
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        statuses = {"AAPL": "No Signal"}
        reasons = {"AAPL": []}
        engine._process_symbol(
            "AAPL",
            snap,
            snap.account,
            strategy,
            engine.slots[0].timeframe,
            strategy_statuses=statuses,
            strategy_reasons=reasons,
        )
        assert statuses["AAPL"] == "Filter Blocked"
        assert reasons["AAPL"] == ["volume contracting", "earnings blackout"]
        broker.place_order.assert_not_called()

    def test_filter_blocked_status_when_edge_filter_fails_without_raw_entry(
        self, engine_factory
    ):
        edge_filter = lambda df: pd.Series([False] * len(df), index=df.index, dtype=bool)
        strategy = FakeStrategy(
            entries=[False] * 60,
            exits=[False],
            edge_filter=edge_filter,
        )
        engine, broker = engine_factory()
        engine.slots[0].strategy = strategy
        engine.strategy = strategy
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        statuses = {"AAPL": "No Signal"}
        engine._process_symbol(
            "AAPL",
            snap,
            snap.account,
            strategy,
            engine.slots[0].timeframe,
            strategy_statuses=statuses,
        )
        assert statuses["AAPL"] == "Filter Blocked"
        broker.place_order.assert_not_called()

    def test_filter_blocked_status_when_legacy_filter_exposes_reasons(
        self, engine_factory
    ):
        class _LegacyBlockingFilter:
            def __call__(self, df):
                return pd.Series([False] * len(df), index=df.index, dtype=bool)

            def get_last_block_reasons(self):
                return ["legacy reason"]

        strategy = FakeStrategy(
            entries=[False] * 59 + [True],
            exits=[False],
            edge_filter=_LegacyBlockingFilter(),
        )
        engine, broker = engine_factory()
        engine.slots[0].strategy = strategy
        engine.strategy = strategy
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        statuses = {"AAPL": "No Signal"}
        reasons = {"AAPL": []}
        engine._process_symbol(
            "AAPL",
            snap,
            snap.account,
            strategy,
            engine.slots[0].timeframe,
            strategy_statuses=statuses,
            strategy_reasons=reasons,
        )
        assert statuses["AAPL"] == "Filter Blocked"
        assert reasons["AAPL"] == ["legacy reason"]
        broker.place_order.assert_not_called()

    def test_state_snapshot_includes_watchlist_statuses(self, engine_factory):
        import json
        from config import settings

        engine, _ = engine_factory()
        engine._running = True
        engine._cycle_count = 3
        engine._last_regime = "TRENDING"
        engine._session_start_equity = 100_000.0
        engine._last_cycle_equity = 100_250.0
        engine._last_snapshot = _snapshot(
            equity=100_250.0,
            previous_close_equity=99_900.0,
        )
        engine._watchlist_statuses = {
            "sma_crossover": {"AAPL": "Long", "MSFT": "Regime Blocked"}
        }
        engine._watchlist_reasons = {
            "sma_crossover": {
                "AAPL": [],
                "MSFT": ["regime bear not in allowed set ['trending']"],
            }
        }
        engine._sector_heat = {
            "generated_at": "2026-05-01T12:00:00+00:00",
            "counts": {"hot": 2, "neutral": 3, "cold": 1},
            "sectors": {
                "technology": {
                    "etf_ticker": "XLK",
                    "score": 4.0,
                    "classification": "hot",
                    "above_sma200": True,
                    "above_sma50": True,
                    "golden_cross": True,
                    "dist_sma50_pct": 0.031,
                    "vol_confirm": True,
                    "last_close": 240.5,
                }
            },
            "symbol_map": {
                "technology": [
                    {"symbol": "AAPL", "strategy": "sma_crossover"}
                ]
            },
            "unmapped": [],
        }
        engine._write_state_snapshot()
        with open(settings.STATE_SNAPSHOT_PATH) as fh:
            state = json.load(fh)
        assert state["previous_close_equity"] == 99_900.0
        assert state["daily_pnl"] == 350.0
        assert state["session_pnl"] == 250.0
        assert state["watchlist_statuses"]["sma_crossover"]["AAPL"] == "Long"
        assert state["watchlist_statuses"]["sma_crossover"]["MSFT"] == "Regime Blocked"
        assert state["watchlist_reasons"]["sma_crossover"]["MSFT"] == [
            "regime bear not in allowed set ['trending']"
        ]
        assert state["sector_heat"]["counts"]["hot"] == 2
        assert state["sector_heat"]["sectors"]["technology"]["score"] == 4.0
        assert state["sector_heat"]["symbol_map"]["technology"][0]["symbol"] == "AAPL"
        assert state["allocator"] == {}
        assert state["capital_pools"] == {}
        assert state["pending_entry_notional"] == {"strategies": {}, "pools": {}}

    def test_attribute_orders_uses_allocator_priority_when_symbols_overlap(
        self, engine_factory
    ):
        from risk.allocator import SleeveAllocator

        class LowPriorityStrategy(FakeStrategy):
            name = "low_priority"

        class HighPriorityStrategy(FakeStrategy):
            name = "high_priority"

        engine, _ = engine_factory()
        engine.slots = [
            StrategySlot(
                strategy=LowPriorityStrategy(entries=[False], exits=[False]),
                symbols=["AAPL"],
            ),
            StrategySlot(
                strategy=HighPriorityStrategy(entries=[False], exits=[False]),
                symbols=["AAPL"],
            ),
        ]
        allocator = MagicMock(spec=SleeveAllocator)
        allocator.strategy_priority.side_effect = lambda name: {
            "high_priority": 0,
            "low_priority": 5,
        }[name]
        engine._allocator = allocator

        order = OpenOrder(
            order_id="buy-1",
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            order_type=OrderType.LIMIT,
            status="open",
            submitted_at=T0,
            limit_price=100.0,
            stop_price=None,
        )
        assert engine._attribute_orders([order]) == {"buy-1": "high_priority"}

    def test_attribute_orders_logs_priority_disambiguation(
        self, engine_factory, monkeypatch
    ):
        from risk.allocator import SleeveAllocator

        class FirstStrategy(FakeStrategy):
            name = "first_strategy"

        class SecondStrategy(FakeStrategy):
            name = "second_strategy"

        engine, _ = engine_factory()
        engine.slots = [
            StrategySlot(
                strategy=FirstStrategy(entries=[False], exits=[False]),
                symbols=["AAPL"],
            ),
            StrategySlot(
                strategy=SecondStrategy(entries=[False], exits=[False]),
                symbols=["AAPL"],
            ),
        ]
        allocator = MagicMock(spec=SleeveAllocator)
        allocator.strategy_priority.side_effect = lambda name: {
            "first_strategy": 0,
            "second_strategy": 1,
        }[name]
        engine._allocator = allocator
        debug = MagicMock()
        monkeypatch.setattr("engine.trader.logger.debug", debug)

        order = OpenOrder(
            order_id="buy-1",
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            order_type=OrderType.LIMIT,
            status="open",
            submitted_at=T0,
            limit_price=100.0,
            stop_price=None,
        )

        assert engine._attribute_orders([order]) == {"buy-1": "first_strategy"}
        debug.assert_called_once()
        assert "via priority among" in debug.call_args.args[0]

    def test_attribute_orders_prefers_client_order_id_strategy_match(
        self, engine_factory
    ):
        from risk.allocator import SleeveAllocator

        class FirstStrategy(FakeStrategy):
            name = "first_strategy"

        class SecondStrategy(FakeStrategy):
            name = "second_strategy"

        engine, _ = engine_factory()
        engine.slots = [
            StrategySlot(
                strategy=FirstStrategy(entries=[False], exits=[False]),
                symbols=["ARM"],
            ),
            StrategySlot(
                strategy=SecondStrategy(entries=[False], exits=[False]),
                symbols=["ARM"],
            ),
        ]
        allocator = MagicMock(spec=SleeveAllocator)
        allocator.strategy_priority.side_effect = lambda name: {
            "first_strategy": 0,
            "second_strategy": 1,
        }[name]
        engine._allocator = allocator

        order = OpenOrder(
            order_id="buy-1",
            symbol="ARM",
            side=Side.BUY,
            qty=1,
            order_type=OrderType.LIMIT,
            status="open",
            submitted_at=T0,
            limit_price=370.79,
            stop_price=None,
            client_order_id="second_strategy-abc123",
        )

        assert engine._attribute_orders([order]) == {"buy-1": "second_strategy"}

    def test_has_pending_entry_order_blocks_duplicate_for_same_strategy(
        self, engine_factory
    ):
        engine, _ = engine_factory()
        snapshot = _snapshot(
            open_orders=[
                OpenOrder(
                    order_id="buy-1",
                    symbol="ARM",
                    side=Side.BUY,
                    qty=1,
                    order_type=OrderType.LIMIT,
                    status="open",
                    submitted_at=T0,
                    limit_price=370.79,
                    stop_price=None,
                    client_order_id="donchian_breakout-abc123",
                )
            ]
        )
        order_strategy = {"buy-1": "donchian_breakout"}

        assert engine._has_pending_entry_order(
            "ARM",
            "donchian_breakout",
            snapshot,
            order_strategy,
        )
        assert not engine._has_pending_entry_order(
            "ARM",
            "rsi_reversion",
            snapshot,
            order_strategy,
        )

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

        # P-4: lookup happens before the call; the fixture has a
        # position_lifecycle row for AAPL so position_uid resolves
        # to whatever new_position_uid() generated.
        broker.place_protective_stop.assert_called_once()
        kwargs = broker.place_protective_stop.call_args.kwargs
        assert kwargs["symbol"] == "AAPL"
        assert kwargs["qty"] == 10
        assert kwargs["stop_price"] == 95.0
        assert kwargs["client_order_id_prefix"] == "fake_strategy-repair-stop"
        assert kwargs["position_uid"].startswith("pos_")

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

    def test_reconciliation_rebuilds_existing_day_stop_as_gtc(
        self, engine_factory
    ):
        day_stop = replace(
            _open_stop_order("AAPL", 95.0),
            order_id="day-stop",
            qty=10,
            time_in_force="day",
        )
        rebuilt = replace(
            day_stop,
            order_id="gtc-stop",
            status="accepted",
            time_in_force="gtc",
        )
        snapshot = _snapshot(
            positions={"AAPL": Position("AAPL", 10, 100.0, 1010.0)},
            open_orders=[day_stop],
        )
        engine, broker = engine_factory(snapshot=snapshot)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        broker.replace_day_stop_with_standalone_gtc.return_value = rebuilt

        engine._repair_missing_protective_stops(snapshot)

        # Alpaca-verified 2026-07-09: attached OTO child stops cannot
        # be promoted in place by changing time_in_force. Reconciliation
        # cancels/rebuilds a standalone GTC stop instead.
        # This test fixture doesn't seed a lifecycle row for the
        # recovered position, so the engine's lookup returns None
        # — the substrate write is skipped, but the broker call
        # still goes out with the correct stop semantics.
        broker.replace_day_stop_with_standalone_gtc.assert_called_once()
        kwargs = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs
        assert kwargs["symbol"] == "AAPL"
        assert kwargs["stop_order_id"] == "day-stop"
        assert kwargs["qty"] == 10
        assert kwargs["stop_price"] == 95.0
        assert kwargs["client_order_id_prefix"] == (
            "fake_strategy-repair-stop-gtc"
        )
        assert "position_uid" in kwargs
        assert snapshot.open_orders == [rebuilt]
        broker.place_protective_stop.assert_not_called()

    def test_reconciliation_leaves_gtc_stop_unchanged(self, engine_factory):
        gtc_stop = replace(
            _open_stop_order("AAPL", 95.0),
            order_id="gtc-stop",
            qty=10,
            time_in_force="gtc",
        )
        snapshot = _snapshot(
            positions={"AAPL": Position("AAPL", 10, 100.0, 1010.0)},
            open_orders=[gtc_stop],
        )
        engine, broker = engine_factory(snapshot=snapshot)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")

        engine._repair_missing_protective_stops(snapshot)

        broker.replace_day_stop_with_standalone_gtc.assert_not_called()
        broker.place_protective_stop.assert_not_called()

    def test_reconciliation_reports_repeated_day_stop_rebuild_failure_once(
        self, engine_factory
    ):
        day_stop = replace(
            _open_stop_order("AAPL", 95.0),
            order_id="day-stop",
            qty=10,
            time_in_force="day",
        )
        snapshot = _snapshot(
            positions={"AAPL": Position("AAPL", 10, 100.0, 1010.0)},
            open_orders=[day_stop],
        )
        engine, broker = engine_factory(snapshot=snapshot)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        broker.replace_day_stop_with_standalone_gtc.side_effect = RuntimeError(
            "order is temporarily not rebuildable"
        )
        engine.risk.record_broker_error = MagicMock()
        engine.alerts.broker_error = MagicMock()

        engine._repair_missing_protective_stops(snapshot)
        engine._repair_missing_protective_stops(snapshot)

        assert broker.replace_day_stop_with_standalone_gtc.call_count == 2
        engine.risk.record_broker_error.assert_called_once()
        engine.alerts.broker_error.assert_called_once()

    def test_reconciliation_does_not_promote_fractional_day_stop(
        self, engine_factory
    ):
        day_stop = replace(
            _open_stop_order("AAPL", 95.0),
            order_id="day-stop",
            qty=0.5,
            time_in_force="day",
        )
        snapshot = _snapshot(
            positions={"AAPL": Position("AAPL", 0.5, 100.0, 50.0)},
            open_orders=[day_stop],
        )
        engine, broker = engine_factory(snapshot=snapshot)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._close_fractional_residual_position = MagicMock()

        engine._repair_missing_protective_stops(snapshot)

        broker.replace_day_stop_with_standalone_gtc.assert_not_called()
        engine._close_fractional_residual_position.assert_called_once_with(
            snapshot=snapshot,
            symbol="AAPL",
            owner="fake_strategy",
            position=snapshot.account.open_positions["AAPL"],
        )

    # P-6: tests for the legacy _suspect_orders / _recover_suspect_orders
    # path were removed alongside the cache. The substrate pipeline
    # (P-1 stream + P-2 cycle reconcile + P-3 startup reconcile +
    # _maybe_dispatch_substrate_entry_fill) covers the same recovery
    # behavior. See:
    #   - tests/test_apply_order_event.py::TestStreamDrainEndToEnd
    #   - tests/test_apply_order_event.py::TestCycleReconcileStoreQuery
    #   - tests/test_apply_order_event.py::TestSubstrateEntryFillDispatchSemantics
    #   - tests/test_stream.py::TestLifecycleEventQueue


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


def _engine_with_db(
    patch_fetch,
    tmp_path,
    *,
    positions=None,
    snapshot=None,
    allocator=None,
):
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
        allocator=allocator,
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
    """Insert a filled sell row into a TradeLogger's DB.

    The row is stamped with the current time so it replays AFTER the
    ``_write_buy`` row it closes — the open-state replay is ordered by
    event timestamp, not insertion order.
    """
    from reporting.logger import TradeRecord

    tl.log(
        TradeRecord(
            position_type="single_leg",
            timestamp=datetime.now(timezone.utc).isoformat(),
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
            initial_stop_loss=95.0,
            initial_risk_per_share=5.0,
            initial_risk_dollars=50.0,
            realized_pnl=50.0,
            r_multiple=1.0,
            entry_timestamp="2026-04-22T10:00:00+00:00",
            exit_timestamp="2026-04-23T10:00:00+00:00",
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

        assert engine._get_owner("AAPL") == "fake_strategy"
        assert conflicts == set()

    def test_db_unknown_strategy_becomes_conflict(self, patch_fetch, tmp_path):
        """DB buy owned by a strategy not in any slot → conflict, no assignment."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        _write_buy(tl, "AAPL", "retired_strategy")

        snap = _snapshot(positions=positions)
        conflicts = engine._restore_ownership_from_db(snap)

        assert not engine._has_position("AAPL")
        assert "AAPL" in conflicts

    def test_no_db_record_falls_back_to_slot_match(self, patch_fetch, tmp_path):
        """No DB record → fall back to slot-order match (AAPL in slot → assigned)."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        # No buy record written — DB is empty.

        snap = _snapshot(positions=positions)
        conflicts = engine._restore_ownership_from_db(snap)

        assert engine._get_owner("AAPL") == "fake_strategy"
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
        assert engine._get_owner("AAPL") == "fake_strategy"

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


class TestRestoreEntryPrices:
    """_restore_entry_prices_from_db must never seed _entry_prices with
    a reference-tainted basis — it feeds _record_realized_pnl and the
    allocator's HWM drawdown gate (2026-07 realized-P&L audit)."""

    def _write_fill_less_buy(self, tl: TradeLogger, symbol: str, strategy: str) -> None:
        """A buy row whose broker fill was never recorded — the replay
        basis falls back to entry_reference_price and is tagged."""
        from reporting.logger import TradeRecord

        tl.log(
            TradeRecord(
                timestamp=datetime.now(timezone.utc).isoformat(),
                symbol=symbol,
                side="buy",
                qty=10,
                avg_fill_price=None,
                order_id="ord-buy-no-fill",
                strategy=strategy,
                reason="test entry",
                stop_price=95.0,
                entry_reference_price=100.0,
                modeled_slippage_bps=None,
                realized_slippage_bps=None,
                order_type="market",
                status="filled",
                requested_qty=10,
                filled_qty=10,
                initial_stop_loss=95.0,
                initial_risk_per_share=5.0,
                initial_risk_dollars=50.0,
                entry_timestamp=datetime.now(timezone.utc).isoformat(),
                position_type="single_leg",
            )
        )

    def _restore(self, engine, positions):
        snap = _snapshot(positions=positions)
        engine._restore_ownership_from_db(snap)
        engine._restore_entry_prices_from_db(snap)

    def test_broker_fill_basis_restores(self, patch_fetch, tmp_path):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        _write_buy(tl, "AAPL", "fake_strategy")
        self._restore(engine, positions)
        assert engine._entry_prices["AAPL"] == pytest.approx(100.5)

    def test_reference_tainted_basis_is_not_restored(self, patch_fetch, tmp_path):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        self._write_fill_less_buy(tl, "AAPL", "fake_strategy")
        self._restore(engine, positions)
        assert "AAPL" not in engine._entry_prices

    def test_missing_source_key_is_refused_not_assumed(self, patch_fetch, tmp_path):
        """A context without entry_fill_price_source has UNKNOWN
        provenance — it must fall through to the lifecycle rescue (and
        be skipped when no lifecycle row exists), never be treated as a
        broker fill. Failing open here would invert the rule this
        function exists to enforce."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        _write_buy(tl, "AAPL", "fake_strategy")
        engine.trade_logger.read_latest_open_entry_context = MagicMock(
            return_value={
                "entry_fill_price": 123.0,
                "entry_timestamp": datetime.now(timezone.utc).isoformat(),
                "open_qty": 10.0,
            }
        )
        self._restore(engine, positions)
        assert "AAPL" not in engine._entry_prices

    def test_lifecycle_basis_rescues_tainted_replay(self, patch_fetch, tmp_path):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        self._write_fill_less_buy(tl, "AAPL", "fake_strategy")
        uid = "pos_" + "b" * 32
        engine.lifecycle_store.create_pending(
            position_uid=uid,
            symbol="AAPL",
            owner_key="AAPL",
            strategy="fake_strategy",
            position_type="single_leg",
            entry_qty=10.0,
        )
        engine.lifecycle_store.mark_open(
            position_uid=uid,
            avg_entry_price=100.5,
            current_qty=10.0,
        )
        self._restore(engine, positions)
        assert engine._entry_prices["AAPL"] == pytest.approx(100.5)


# ── Startup reconciliation modes (10.C2) ──────────────────────────────────


class TestStartupReconciliation:
    """10.C2 — _reconcile_startup returns NORMAL/RESTRICTED; RESTRICTED auto-clears."""

    def test_no_conflicts_no_unmanaged_gives_normal(self, patch_fetch, tmp_path):
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
        # Pre-assign ownership so no unmanaged positions.
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
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
        # AAPL not in _positions → unmanaged.
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

    def test_start_restores_allocator_pnl_state_from_trade_log(
        self, patch_fetch, tmp_path
    ):
        from risk.allocator import SleeveAllocator

        allocator = SleeveAllocator(
            allocations={
                "fake_strategy": {
                    "target_pct": 1.0,
                    "type": "equity",
                    "priority": 0,
                    "can_stretch": True,
                    "hard_max_positions": 8,
                    "max_position_pct_of_sleeve": 0.4,
                }
            },
            total_gross_pct=0.80,
            capital_pools={"equity": 1.0, "isolated_options": 0.0},
            stretch_utilization_threshold=0.80,
            default_stretch_pct=0.15,
            dd_threshold=0.15,
        )
        startup = _snapshot()
        cycle = _snapshot()
        engine, broker, tl = _engine_with_db(
            patch_fetch,
            tmp_path,
            snapshot=startup,
            allocator=allocator,
        )
        broker.sync_with_broker.side_effect = [startup, cycle]
        _write_buy(tl, "AAPL", "fake_strategy")
        _write_sell(tl, "AAPL", "fake_strategy")

        engine.start(max_cycles=1)

        assert allocator.pnl_summary()["fake_strategy"] == {
            "realized_pnl": pytest.approx(50.0),
            "hwm": pytest.approx(50.0),
            # trade_count + seen_position_uids restored from the trade
            # log alongside P&L/HWM (PR #56 R1) — partial closes of the
            # same position correctly count as one round trip.
            "trade_count": pytest.approx(1.0),
            "seen_position_uids": [],
        }

    def test_start_restores_entry_prices_for_open_positions(
        self, patch_fetch, tmp_path
    ):
        positions = {"AAPL": Position("AAPL", 10, 100.25, 1002.5)}
        startup = _snapshot(positions=positions)
        cycle = _snapshot(positions=positions)
        engine, broker, tl = _engine_with_db(
            patch_fetch,
            tmp_path,
            positions=positions,
            snapshot=startup,
        )
        broker.sync_with_broker.side_effect = [startup, cycle]
        decision = SimpleNamespace(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            entry_reference_price=100.0,
            stop_price=95.0,
            strategy_name="fake_strategy",
            reason="test entry",
            order_type=OrderType.MARKET,
        )
        tl.log(tl.build_record(
            decision,
            _filled_result("AAPL", 10, 100.25),
            modeled_price=100.0,
        ))

        engine.start(max_cycles=1)

        assert engine._entry_prices["AAPL"] == pytest.approx(100.25)

    def test_startup_recovers_db_open_position_already_absent_at_broker(
        self, patch_fetch, tmp_path
    ):
        startup = _snapshot()
        engine, broker, tl = _engine_with_db(
            patch_fetch,
            tmp_path,
            snapshot=startup,
        )
        broker.sync_with_broker.side_effect = [startup, startup]
        _write_buy(tl, "AAPL", "fake_strategy")
        broker.find_recent_filled_stop_order.return_value = None
        # The recovered sell must carry an execution time AFTER the buy
        # row just written (which is stamped now) — the open-state
        # replay orders rows by event timestamp, and a sell that
        # predates its buy is an impossible history.
        recovered_at = datetime.now(timezone.utc)
        broker.find_recent_filled_sell_orders.return_value = [
            ClosedOrderInfo(
                order_id="startup-exit-1",
                client_order_id=None,
                symbol="AAPL",
                side=Side.SELL,
                order_type="market",
                status=OrderStatus.FILLED,
                raw_status="filled",
                qty=10.0,
                filled_qty=10.0,
                avg_fill_price=99.0,
                stop_price=None,
                submitted_at=recovered_at + timedelta(minutes=1),
                filled_at=recovered_at + timedelta(minutes=2),
            )
        ]
        engine._record_realized_pnl = MagicMock()

        engine.start(max_cycles=1)

        assert tl.read_all_open_owners() == {}
        sell_rows = [row for row in tl.read_all() if row["side"] == "sell"]
        assert len(sell_rows) == 1
        assert sell_rows[0]["order_id"] == "startup-exit-1"
        assert sell_rows[0]["reason"] == "startup_broker_history_sell_recovered"
        engine._record_realized_pnl.assert_called_once()
        assert engine._record_realized_pnl.call_args.kwargs["external"] is True


# ── External close detection ──────────────────────────────────────────────


def _engine_with_confirm(patch_fetch, tmp_path, *, confirm: int = 3, positions=None):
    """Like _engine_with_db but with a configurable confirm cycle count."""
    engine, broker, tl = _engine_with_db(patch_fetch, tmp_path, positions=positions)
    # Patch the config with the desired confirmation window.
    object.__setattr__(engine.config, "external_close_confirm_cycles", confirm)
    return engine, broker, tl


class TestExternalCloseDetection:
    """
    Positions that disappear from the broker without the bot closing them
    (stop-out, manual liquidation) must be detected after N consecutive absent
    cycles, logged, and cleared from ownership so the trade DB stays coherent.
    """

    def test_single_absence_does_not_act(self, patch_fetch, tmp_path):
        """One absent cycle is a suspect — ownership not cleared yet."""
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=3)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._detect_external_closes(_snapshot())
        assert engine._has_position("AAPL")
        assert engine._external_close_suspects["AAPL"] == 1

    def test_two_absences_still_not_confirmed(self, patch_fetch, tmp_path):
        """Two absent cycles with confirm=3 → still suspect."""
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=3)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._detect_external_closes(_snapshot())
        engine._detect_external_closes(_snapshot())
        assert engine._has_position("AAPL")
        assert engine._external_close_suspects["AAPL"] == 2

    def test_confirmed_after_n_cycles_clears_ownership(self, patch_fetch, tmp_path):
        """After N consecutive absent cycles ownership is cleared."""
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=3)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        for _ in range(3):
            engine._detect_external_closes(_snapshot())
        assert not engine._has_position("AAPL")
        assert "AAPL" not in engine._external_close_suspects

    def test_blip_recovery_resets_counter(self, patch_fetch, tmp_path):
        """Position reappears after 2 absent cycles → counter resets, no action."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=3)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")

        engine._detect_external_closes(_snapshot())           # absent: count=1
        engine._detect_external_closes(_snapshot())           # absent: count=2
        engine._detect_external_closes(_snapshot(positions=positions))  # back

        assert engine._has_position("AAPL")
        assert "AAPL" not in engine._external_close_suspects

    def test_position_still_present_not_counted(self, patch_fetch, tmp_path):
        """Present position never increments suspect counter."""
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1000.0)}
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=3)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        snap = _snapshot(positions=positions)
        engine._detect_external_closes(snap)
        assert engine._get_owner("AAPL") == "fake_strategy"
        assert "AAPL" not in engine._external_close_suspects

    def test_synthetic_sell_written_after_confirmation(self, patch_fetch, tmp_path):
        """Synthetic sell is written only after N cycles, not before."""
        engine, _, tl = _engine_with_confirm(patch_fetch, tmp_path, confirm=3)
        _write_buy(tl, "AAPL", "fake_strategy")
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")

        engine._detect_external_closes(_snapshot())  # cycle 1 — no action yet
        assert tl.read_all_open_owners() == {"AAPL": "fake_strategy"}

        engine._detect_external_closes(_snapshot())  # cycle 2 — no action yet
        assert tl.read_all_open_owners() == {"AAPL": "fake_strategy"}

        engine._detect_external_closes(_snapshot())  # cycle 3 — confirmed
        assert tl.read_all_open_owners() == {}

    def test_synthetic_sell_reason_recorded(self, patch_fetch, tmp_path):
        """The confirmed synthetic sell row carries external_close_detected."""
        engine, _, tl = _engine_with_confirm(patch_fetch, tmp_path, confirm=2)
        _write_buy(tl, "AAPL", "fake_strategy")
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        for _ in range(2):
            engine._detect_external_closes(_snapshot())

        rows = tl.read_all()
        sell_rows = [r for r in rows if r["side"] == "sell"]
        assert len(sell_rows) == 1
        assert sell_rows[0]["reason"] == "external_close_detected"
        assert sell_rows[0]["strategy"] == "fake_strategy"

    def test_external_close_prefers_recovered_broker_stop_fill(self, patch_fetch, tmp_path):
        """If broker history proves a stop fill, use it instead of synthetic external close."""
        engine, _, tl = _engine_with_confirm(patch_fetch, tmp_path, confirm=1)
        _write_buy(tl, "AAPL", "fake_strategy")
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.0
        engine.broker.find_recent_filled_stop_order = MagicMock(
            return_value=ClosedOrderInfo(
                order_id="stop-aapl-1",
                client_order_id=None,
                symbol="AAPL",
                side=Side.SELL,
                order_type="stop",
                status=OrderStatus.FILLED,
                raw_status="filled",
                qty=10.0,
                filled_qty=10.0,
                avg_fill_price=95.0,
                stop_price=95.0,
                submitted_at=T0,
                filled_at=T0 + timedelta(minutes=1),
            )
        )

        engine._detect_external_closes(_snapshot())

        assert not engine._has_position("AAPL")
        sell_rows = [r for r in tl.read_all() if r["side"] == "sell"]
        assert len(sell_rows) == 1
        assert sell_rows[0]["order_id"] == "stop-aapl-1"
        assert sell_rows[0]["reason"] == "stop_triggered"

    def test_external_close_recovers_filled_market_sell(self, patch_fetch, tmp_path):
        """CIEN regression: a timed-out market close is rebuilt from broker history."""
        engine, _, tl = _engine_with_confirm(patch_fetch, tmp_path, confirm=1)
        _write_buy(tl, "AAPL", "fake_strategy")
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.5
        engine.broker.find_recent_filled_stop_order = MagicMock(return_value=None)
        engine.broker.find_recent_filled_sell_orders = MagicMock(
            return_value=[
                ClosedOrderInfo(
                    order_id="market-exit-1",
                    client_order_id=None,
                    symbol="AAPL",
                    side=Side.SELL,
                    order_type="market",
                    status=OrderStatus.FILLED,
                    raw_status="filled",
                    qty=10.0,
                    filled_qty=10.0,
                    avg_fill_price=99.0,
                    stop_price=None,
                    submitted_at=T0 + timedelta(minutes=1),
                    filled_at=T0 + timedelta(minutes=2),
                )
            ]
        )
        engine._record_realized_pnl = MagicMock()

        engine._detect_external_closes(_snapshot())

        sell_rows = [row for row in tl.read_all() if row["side"] == "sell"]
        assert len(sell_rows) == 1
        assert sell_rows[0]["order_id"] == "market-exit-1"
        assert sell_rows[0]["qty"] == pytest.approx(10.0)
        assert sell_rows[0]["avg_fill_price"] == pytest.approx(99.0)
        assert sell_rows[0]["realized_pnl"] == pytest.approx(-15.0)
        assert sell_rows[0]["reason"] == "broker_history_sell_recovered"
        assert sell_rows[0]["timestamp"] == (
            T0 + timedelta(minutes=2)
        ).isoformat()
        assert engine._record_realized_pnl.call_args.kwargs["external"] is True

    def test_external_close_recovers_multiple_filled_sells_in_order(
        self, patch_fetch, tmp_path
    ):
        engine, _, tl = _engine_with_confirm(patch_fetch, tmp_path, confirm=1)
        _write_buy(tl, "AAPL", "fake_strategy")
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.5
        engine.broker.find_recent_filled_stop_order = MagicMock(return_value=None)
        # Recovered sells must postdate the now-stamped buy row — the
        # open-state replay orders rows by event timestamp.
        recovered_at = datetime.now(timezone.utc)
        engine.broker.find_recent_filled_sell_orders = MagicMock(
            return_value=[
                ClosedOrderInfo(
                    order_id=f"market-exit-{index}",
                    client_order_id=None,
                    symbol="AAPL",
                    side=Side.SELL,
                    order_type="market",
                    status=OrderStatus.FILLED,
                    raw_status="filled",
                    qty=qty,
                    filled_qty=qty,
                    avg_fill_price=price,
                    stop_price=None,
                    submitted_at=recovered_at + timedelta(minutes=index),
                    filled_at=recovered_at + timedelta(minutes=index),
                )
                for index, qty, price in [(1, 4.0, 101.0), (2, 6.0, 99.0)]
            ]
        )

        engine._detect_external_closes(_snapshot())

        sell_rows = [row for row in tl.read_all() if row["side"] == "sell"]
        assert [row["order_id"] for row in sell_rows] == [
            "market-exit-1",
            "market-exit-2",
        ]
        assert tl.read_all_open_owners() == {}

    def test_external_close_does_not_recover_insufficient_sell_quantity(
        self, patch_fetch, tmp_path
    ):
        engine, _, tl = _engine_with_confirm(patch_fetch, tmp_path, confirm=1)
        _write_buy(tl, "AAPL", "fake_strategy")
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine.broker.find_recent_filled_stop_order = MagicMock(return_value=None)
        engine.broker.find_recent_filled_sell_orders = MagicMock(
            return_value=[
                ClosedOrderInfo(
                    order_id="partial-only",
                    client_order_id=None,
                    symbol="AAPL",
                    side=Side.SELL,
                    order_type="market",
                    status=OrderStatus.FILLED,
                    raw_status="filled",
                    qty=4.0,
                    filled_qty=4.0,
                    avg_fill_price=99.0,
                    stop_price=None,
                    submitted_at=T0,
                    filled_at=T0,
                )
            ]
        )

        engine._detect_external_closes(_snapshot())

        sell_rows = [row for row in tl.read_all() if row["side"] == "sell"]
        assert len(sell_rows) == 1
        assert sell_rows[0]["reason"] == "external_close_detected"
        assert sell_rows[0]["order_id"] is None

    def test_external_close_does_not_recover_excess_sell_quantity(
        self, patch_fetch, tmp_path
    ):
        engine, _, tl = _engine_with_confirm(patch_fetch, tmp_path, confirm=1)
        _write_buy(tl, "AAPL", "fake_strategy")
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine.broker.find_recent_filled_stop_order = MagicMock(return_value=None)
        engine.broker.find_recent_filled_sell_orders = MagicMock(
            return_value=[
                ClosedOrderInfo(
                    order_id="excess-sell",
                    client_order_id=None,
                    symbol="AAPL",
                    side=Side.SELL,
                    order_type="market",
                    status=OrderStatus.FILLED,
                    raw_status="filled",
                    qty=12.0,
                    filled_qty=12.0,
                    avg_fill_price=99.0,
                    stop_price=None,
                    submitted_at=T0,
                    filled_at=T0,
                )
            ]
        )

        engine._detect_external_closes(_snapshot())

        sell_rows = [row for row in tl.read_all() if row["side"] == "sell"]
        assert len(sell_rows) == 1
        assert sell_rows[0]["reason"] == "external_close_detected"
        assert sell_rows[0]["order_id"] is None

    def test_recovered_stop_fill_uses_100x_multiplier_for_occ_symbol(self, patch_fetch, tmp_path):
        """Broker-history stop recovery should apply the options contract multiplier when needed."""
        from risk.allocator import SleeveAllocator

        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=1)
        engine._entry_prices["SPY"] = 10.0
        allocator = MagicMock(spec=SleeveAllocator)
        engine._allocator = allocator

        stop_fill = ClosedOrderInfo(
            order_id="occ-stop-1",
            client_order_id=None,
            symbol="SPY260620C00730000",
            side=Side.SELL,
            order_type="stop",
            status=OrderStatus.FILLED,
            raw_status="filled",
            qty=2.0,
            filled_qty=2.0,
            avg_fill_price=15.0,
            stop_price=14.5,
            submitted_at=T0,
            filled_at=T0 + timedelta(minutes=1),
        )

        engine._record_recovered_stop_fill(
            symbol="SPY",
            owner="spy_options_reversion",
            stop_fill=stop_fill,
        )

        allocator.record_realized_pnl.assert_called_once_with(
            "spy_options_reversion",
            1000.0,
            position_uid=None,
            is_full_close=True,
        )

    def test_recovered_stop_fill_passes_broker_stop_price_and_recovered_quality(
        self, patch_fetch, tmp_path
    ):
        """
        Slippage unification Phase 1 codepath §5 — recovery path forwards
        ClosedOrderInfo.stop_price as the benchmark and tags the row
        measurement_quality='recovered'.
        """
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=1)
        engine._entry_prices["AAPL"] = 150.0

        stop_fill = ClosedOrderInfo(
            order_id="recovered-aapl-1",
            client_order_id=None,
            symbol="AAPL",
            side=Side.SELL,
            order_type="stop",
            status=OrderStatus.FILLED,
            raw_status="filled",
            qty=10.0,
            filled_qty=10.0,
            avg_fill_price=144.50,
            stop_price=145.00,
            submitted_at=T0,
            filled_at=T0 + timedelta(minutes=1),
        )

        engine.trade_logger.log_stop_fill = MagicMock()
        engine._record_recovered_stop_fill(
            symbol="AAPL",
            owner="sma_crossover",
            stop_fill=stop_fill,
        )
        kwargs = engine.trade_logger.log_stop_fill.call_args.kwargs
        assert kwargs["stop_price"] == pytest.approx(145.00)
        assert kwargs["measurement_quality"] == "recovered"

    def test_recovered_stop_fill_forwards_none_when_broker_stop_price_missing(
        self, patch_fetch, tmp_path
    ):
        """When the recovered broker order has no stop_price, log_stop_fill
        receives stop_price=None and writes the row as 'unavailable'."""
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=1)
        engine._entry_prices["AAPL"] = 150.0

        stop_fill = ClosedOrderInfo(
            order_id="recovered-aapl-2",
            client_order_id=None,
            symbol="AAPL",
            side=Side.SELL,
            order_type="stop",
            status=OrderStatus.FILLED,
            raw_status="filled",
            qty=10.0,
            filled_qty=10.0,
            avg_fill_price=144.50,
            stop_price=None,
            submitted_at=T0,
            filled_at=T0 + timedelta(minutes=1),
        )

        engine.trade_logger.log_stop_fill = MagicMock()
        engine._record_recovered_stop_fill(
            symbol="AAPL",
            owner="sma_crossover",
            stop_fill=stop_fill,
        )
        kwargs = engine.trade_logger.log_stop_fill.call_args.kwargs
        assert kwargs["stop_price"] is None
        assert kwargs["measurement_quality"] == "recovered"

    def test_multiple_positions_only_confirmed_ones_cleared(self, patch_fetch, tmp_path):
        """Only positions that hit confirm threshold are cleared."""
        positions = {"MSFT": Position("MSFT", 5, 200.0, 1000.0)}
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=3)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")   # will go absent
        engine._register_single_leg(strategy_name="fake_strategy", symbol="MSFT")   # stays present

        snap_with_msft = _snapshot(positions=positions)
        for _ in range(3):
            engine._detect_external_closes(snap_with_msft)

        assert not engine._has_position("AAPL")
        assert engine._get_owner("MSFT") == "fake_strategy"

    def test_no_owned_positions_no_op(self, patch_fetch, tmp_path):
        """With no owned positions, detect_external_closes is a no-op."""
        engine, _, _ = _engine_with_confirm(patch_fetch, tmp_path, confirm=3)
        engine._detect_external_closes(_snapshot())
        assert engine._positions == {}

    def test_log_external_close_closes_db_record(self, tmp_path):
        """log_external_close writes a sell row that closes the DB open record."""
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        _write_buy(tl, "AAPL", "sma_crossover")
        tl.log_external_close(
            symbol="AAPL",
            strategy="sma_crossover",
            reason="external_close_detected",
        )
        assert tl.read_all_open_owners() == {}
        assert tl.read_owner_for_symbol("AAPL") is None

    def test_confirm_cycles_configurable_via_engine_config(self):
        """EngineConfig validates external_close_confirm_cycles."""
        with pytest.raises(ValueError, match="external_close_confirm_cycles"):
            EngineConfig(
                cycle_interval_seconds=1,
                max_bar_age_multiplier=2,
                external_close_confirm_cycles=0,
            )


# ── Options safety fixes ──────────────────────────────────────────────────────


class TestOptionsEngineFixes:
    """Unit tests for the four options safety fixes.

    These tests call the private helpers directly rather than running a full
    engine cycle, which keeps them fast and deterministic.
    """

    def _engine(self, tmp_path) -> TradingEngine:
        from strategies.base import StrategySlot
        from data.watchlists import StaticWatchlistSource

        strategy = FakeStrategy(entries=[False], exits=[False])
        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.place_order.return_value = _filled_result("AAPL", 1, 100.0)
        broker.close_position.return_value = _filled_result("AAPL", 1, 100.0)
        broker.get_open_orders.return_value = []
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

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
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        return TradingEngine(
            strategy=strategy,
            symbols=["AAPL"],
            risk=risk,
            broker=broker,
            trade_logger=tl,
            config=EngineConfig(
                history_lookback_days=120,
                cycle_interval_seconds=0.01,
                max_bar_age_multiplier=10.0,
                market_hours_only=False,
            ),
        )

    # Fix 2: stop repair skips OCC symbols ────────────────────────────────────

    def test_stop_repair_skips_occ_symbol(self, tmp_path):
        """_repair_missing_protective_stops must not attempt equity repair on options."""
        engine = self._engine(tmp_path)
        occ = "SPY260516C00520000"
        # Pretend the engine owns the underlying
        engine._register_single_leg(strategy_name="spy_options_reversion", symbol="SPY")

        from types import SimpleNamespace
        from execution.broker import BrokerSnapshot, OrderStatus
        pos = SimpleNamespace(qty=2, symbol=occ, avg_entry_price=10.0, market_value=20.0,
                              unrealized_pl=1.0, current_price=11.0, cost_basis=20.0,
                              asset_id="x", side="long")
        snap = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0,
                cash=50_000.0,
                buying_power=50_000.0,
                open_positions={occ: pos},
            ),
            open_orders=[],
        )
        # If the OCC check is missing, place_protective_stop would be called.
        engine.broker.place_protective_stop = MagicMock()
        engine._repair_missing_protective_stops(snap)
        engine.broker.place_protective_stop.assert_not_called()

    def test_state_snapshot_maps_occ_position_detail_to_owner_key(self, tmp_path):
        """Options owned by an underlying key should still populate positions_detail."""
        import json

        from config import settings

        engine = self._engine(tmp_path)
        occ = "SPY260618C00746000"
        engine._register_single_leg(strategy_name="spy_options_reversion", symbol="SPY")
        pos = SimpleNamespace(
            qty=3.0,
            symbol=occ,
            avg_entry_price=12.77,
            market_value=4335.0,
            unrealized_pl=504.0,
            current_price=14.45,
            cost_basis=3831.0,
            asset_id="opt-1",
            side="long",
        )
        engine._running = True
        engine._session_start_equity = 100_000.0
        engine._last_cycle_equity = 100_250.0
        engine._last_snapshot = _snapshot(positions={occ: pos})

        engine._write_state_snapshot()

        with open(settings.STATE_SNAPSHOT_PATH) as fh:
            state = json.load(fh)

        assert state["open_positions"]["SPY"] == "spy_options_reversion"
        assert state["positions_detail"]["SPY"]["qty"] == 3.0
        assert state["positions_detail"]["SPY"]["avg_entry_price"] == 12.77
        assert state["positions_detail"]["SPY"]["market_value"] == 4335.0
        assert state["positions_detail"]["SPY"]["cost_basis"] == 3831.0
        assert state["positions_detail"]["SPY"]["unrealized_pnl"] == 504.0

    def test_stop_repair_reconstructs_missing_entry_context_for_managed_equity(self, tmp_path, monkeypatch):
        """If DB context is missing but broker position + owner exist, self-heal should reconstruct and repair."""
        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine.risk._stop_price_for = MagicMock(return_value=95.0)
        engine.broker.place_protective_stop = MagicMock(return_value=_open_stop_order("AAPL", 95.0))
        engine.broker.find_recent_filled_entry_order = MagicMock(return_value=None)
        monkeypatch.setattr(
            "engine.trader.fetch_symbol",
            lambda symbol, start, end, timeframe="1Day", **kwargs: (_bars(), SimpleNamespace(api_calls=0)),
        )

        pos = Position("AAPL", 10, 100.0, 1000.0)
        snap = _snapshot(
            positions={"AAPL": pos},
            open_orders=[],
        )

        engine._repair_missing_protective_stops(snap)

        stop_call = engine.broker.place_protective_stop.call_args.kwargs
        assert stop_call["symbol"] == "AAPL"
        assert stop_call["qty"] == 10
        assert stop_call["stop_price"] == 95.0
        assert engine.trade_logger.read_all_open_owners() == {"AAPL": "fake_strategy"}
        assert engine.trade_logger.read_latest_open_stop_price(
            symbol="AAPL",
            strategy="fake_strategy",
        ) == 95.0

    def test_recovered_entry_uses_broker_filled_at_timestamp(self, tmp_path, monkeypatch):
        """Recovery rows should use Alpaca's original filled_at timestamp when available."""
        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine.risk._stop_price_for = MagicMock(return_value=61.36)
        engine.broker.place_protective_stop = MagicMock(return_value=_open_stop_order("AAPL", 61.36))
        engine.broker.find_recent_filled_entry_order = MagicMock(
            return_value=ClosedOrderInfo(
                order_id="aapl-entry-1",
                client_order_id="cid-aapl",
                symbol="AAPL",
                side=Side.BUY,
                order_type="limit",
                status=OrderStatus.FILLED,
                raw_status="filled",
                qty=10.0,
                filled_qty=10.0,
                avg_fill_price=100.0,
                stop_price=None,
                submitted_at=T0,
                filled_at=T0 + timedelta(minutes=5),
            )
        )
        monkeypatch.setattr(
            "engine.trader.fetch_symbol",
            lambda symbol, start, end, timeframe="1Day", **kwargs: (_bars(base=100.0), SimpleNamespace(api_calls=0)),
        )

        pos = Position("AAPL", 10, 100.0, 1000.0)
        snap = _snapshot(
            positions={"AAPL": pos},
            open_orders=[],
        )

        engine._repair_missing_protective_stops(snap)

        rows = engine.trade_logger.read_all()
        buy_rows = [r for r in rows if r["side"] == "buy" and r["symbol"] == "AAPL"]
        assert len(buy_rows) == 1
        assert buy_rows[0]["timestamp"] == (T0 + timedelta(minutes=5)).isoformat()
        assert buy_rows[0]["entry_timestamp"] == (T0 + timedelta(minutes=5)).isoformat()
        assert buy_rows[0]["order_id"] == "aapl-entry-1"

    def test_sync_managed_stop_legs_rehydrates_managed_equity_stops_only(self, tmp_path):
        """Open broker stop orders are rehydrated into the stream manager from snapshot truth."""
        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._stream_manager = MagicMock()
        snapshot = _snapshot(
            positions={"AAPL": Position("AAPL", 10, 100.0, 1000.0)},
            open_orders=[
                OpenOrder(
                    order_id="stop-aapl",
                    symbol="AAPL",
                    side=Side.SELL,
                    qty=10,
                    order_type=OrderType.MARKET,
                    status="open",
                    submitted_at=T0,
                    limit_price=None,
                    stop_price=95.0,
                ),
                OpenOrder(
                    order_id="ignore-no-stop",
                    symbol="AAPL",
                    side=Side.SELL,
                    qty=10,
                    order_type=OrderType.MARKET,
                    status="open",
                    submitted_at=T0,
                    limit_price=None,
                    stop_price=None,
                ),
                OpenOrder(
                    order_id="ignore-unowned",
                    symbol="MSFT",
                    side=Side.SELL,
                    qty=5,
                    order_type=OrderType.MARKET,
                    status="open",
                    submitted_at=T0,
                    limit_price=None,
                    stop_price=300.0,
                ),
                OpenOrder(
                    order_id="ignore-option",
                    symbol="SPY260516C00520000",
                    side=Side.SELL,
                    qty=1,
                    order_type=OrderType.MARKET,
                    status="open",
                    submitted_at=T0,
                    limit_price=None,
                    stop_price=7.5,
                ),
            ],
        )

        engine._sync_managed_stop_legs(snapshot)

        engine._stream_manager.sync_stop_legs.assert_called_once_with({"stop-aapl"})

    def test_stop_repair_auto_closes_fractional_residual_without_whole_share_qty(self, tmp_path):
        """Managed sub-1-share remainders should be closed instead of repaired with qty=0."""
        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine.trade_logger = TradeLogger(path=str(tmp_path / "trades.db"))
        engine.trade_logger.log(engine.trade_logger.build_record(
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
        engine.broker.place_protective_stop = MagicMock()
        engine.broker.close_position = MagicMock(
            return_value=_filled_result("AAPL", 0.39, 99.5)
        )
        engine.alerts.broker_error = MagicMock()
        engine.alerts.trade_executed = MagicMock()

        snap = _snapshot(
            positions={"AAPL": Position("AAPL", 0.39, 100.0, 39.0)},
            open_orders=[],
        )

        engine._repair_missing_protective_stops(snap)

        engine.broker.place_protective_stop.assert_not_called()
        engine.broker.close_position.assert_called_once_with("AAPL", position_uid=None)
        engine.alerts.broker_error.assert_not_called()
        engine.alerts.trade_executed.assert_called_once()
        assert engine._get_owner("AAPL") is None
        assert "AAPL" not in engine._entry_prices

    def test_stop_repair_fractional_residual_respects_pending_close_order(self, tmp_path):
        """Residual cleanup must not submit a duplicate close if one is already pending."""
        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine.trade_logger = TradeLogger(path=str(tmp_path / "trades.db"))
        engine.trade_logger.log(engine.trade_logger.build_record(
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
        engine.broker.place_protective_stop = MagicMock()
        engine.broker.close_position = MagicMock()

        snap = _snapshot(
            positions={"AAPL": Position("AAPL", 0.39, 100.0, 39.0)},
            open_orders=[OpenOrder(
                order_id="close-1",
                symbol="AAPL",
                side=Side.SELL,
                qty=0.39,
                order_type=OrderType.MARKET,
                status="open",
                submitted_at=T0,
                limit_price=None,
                stop_price=None,
            )],
        )

        engine._repair_missing_protective_stops(snap)

        engine.broker.place_protective_stop.assert_not_called()
        engine.broker.close_position.assert_not_called()

    def test_stop_repair_fractional_residual_recovers_missing_stop_fill_before_cleanup(self, tmp_path):
        """GOOG-style fractional residuals should log the missing whole-share stop fill before dust cleanup."""
        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="GOOG")
        engine.trade_logger = TradeLogger(path=str(tmp_path / "trades.db"))
        engine.trade_logger.log(engine.trade_logger.build_record(
            decision=SimpleNamespace(
                symbol="GOOG",
                side=Side.BUY,
                qty=7.78,
                entry_reference_price=391.0,
                stop_price=378.85,
                strategy_name="fake_strategy",
                reason="test",
                order_type=OrderType.MARKET,
            ),
            result=_filled_result("GOOG", 7.78, 391.2),
            modeled_price=391.0,
        ))
        engine._entry_prices["GOOG"] = 391.0
        engine.broker.find_recent_filled_stop_order = MagicMock(
            return_value=ClosedOrderInfo(
                order_id="goog-stop-1",
                client_order_id=None,
                symbol="GOOG",
                side=Side.SELL,
                order_type="stop",
                status=OrderStatus.FILLED,
                raw_status="filled",
                qty=7.0,
                filled_qty=7.0,
                avg_fill_price=378.85,
                stop_price=378.85,
                submitted_at=T0,
                filled_at=T0 + timedelta(minutes=1),
            )
        )
        engine.broker.place_protective_stop = MagicMock()
        engine.broker.close_position = MagicMock(
            return_value=OrderResult(
                status=OrderStatus.FILLED,
                order_id="goog-dust-close",
                symbol="GOOG",
                requested_qty=0.78,
                filled_qty=0.78,
                avg_fill_price=379.184,
                raw_status="filled",
                message="ok",
            )
        )
        engine.alerts.trade_executed = MagicMock()

        snap = _snapshot(
            positions={"GOOG": Position("GOOG", 0.78, 391.2, 295.76)},
            open_orders=[],
        )

        engine._repair_missing_protective_stops(snap)

        rows = engine.trade_logger.read_all()
        sells = [r for r in rows if r["side"] == "sell"]
        assert len(sells) == 2
        assert sells[0]["order_id"] == "goog-stop-1"
        assert sells[0]["qty"] == pytest.approx(7.0)
        assert sells[0]["reason"] == "stop_triggered"
        assert sells[0]["timestamp"] == (T0 + timedelta(minutes=1)).isoformat()
        assert sells[0]["exit_timestamp"] == (T0 + timedelta(minutes=1)).isoformat()
        assert sells[1]["order_id"] == "goog-dust-close"
        assert sells[1]["qty"] == pytest.approx(0.78)

    def test_drain_option_rejected_clears_pre_registered_underlying_ownership(self, tmp_path):
        """Rejected option entries must clean up pre-registered underlying ownership immediately."""
        engine = self._engine(tmp_path)
        occ = "SPY260516C00520000"
        engine._register_single_leg(strategy_name="spy_options_reversion", symbol="SPY")
        engine._entry_prices["SPY"] = 12.15
        engine.broker.drain_option_fills = MagicMock(return_value=[
            (
                SimpleNamespace(
                    symbol=occ,
                    qty=3,
                    entry_reference_price=12.15,
                    strategy_name="spy_options_reversion",
                    side=Side.BUY,
                ),
                "rejected",
	                0.0,
	                None,
	                "opt-spy_options_reversion-abcd1234",
	                "pos_rejected",
	            )
	        ])

        engine._drain_option_fills()

        assert not engine._has_position("SPY")
        assert "SPY" not in engine._entry_prices

    def test_drain_option_filled_calls_register_fill_on_strategy(self, tmp_path):
        """A3 — confirmed BUY fill must anchor the strategy's trailing-stop base
        via register_fill(occ, avg_fill_price)."""
        from strategies.base import StrategySlot

        engine = self._engine(tmp_path)
        occ = "SPY260516C00520000"
        # Swap the default FakeStrategy slot for one that owns the SPY options
        # strategy name and exposes register_fill — so _strategy_by_name finds it.
        strat_mock = MagicMock()
        strat_mock.name = "spy_options_reversion"
        engine.slots = [StrategySlot(strategy=strat_mock, symbols=["SPY"])]
        engine._register_single_leg(strategy_name="spy_options_reversion", symbol="SPY")
        engine.broker.drain_option_fills = MagicMock(return_value=[
            (
                SimpleNamespace(
                    symbol=occ,
                    qty=3,
                    entry_reference_price=12.15,
                    strategy_name="spy_options_reversion",
                    side=Side.BUY,
                ),
                "filled",
	                3.0,
	                12.40,  # actual fill premium
	                "opt-spy_options_reversion-fill",
	                "pos_filled",
	            )
	        ])
        engine.broker._lifecycle_mark_filled = MagicMock()

        engine._drain_option_fills()

        lifecycle_result = engine.broker._lifecycle_mark_filled.call_args.kwargs[
            "result"
        ]
        assert engine.broker._lifecycle_mark_filled.call_args.kwargs[
            "position_uid"
        ] == "pos_filled"
        assert lifecycle_result.symbol == occ
        assert lifecycle_result.status is OrderStatus.FILLED
        assert lifecycle_result.position_uid == "pos_filled"
        strat_mock.register_fill.assert_called_once_with(occ, 12.40)
        # Entry price tracking continues to use the fill price as before.
        assert engine._entry_prices.get("SPY") == 12.40


    # Fix 3: slippage not recorded for options exits ──────────────────────────

    def test_slippage_not_recorded_for_options_exit(self, tmp_path):
        """_record_fill must be skipped when closing an OCC position."""
        engine = self._engine(tmp_path)
        engine.risk.record_fill_slippage = MagicMock()

        from execution.broker import OrderResult, OrderStatus
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="ord-1",
            symbol="SPY260516C00520000",
            requested_qty=2,
            filled_qty=2,
            avg_fill_price=14.0,   # option premium
            raw_status="filled",
            message="",
        )
        # latest_close is SPY price (~520), not the option premium.
        # Without the guard this produces ~9 800 bps of phantom slippage.
        from types import SimpleNamespace
        position = SimpleNamespace(symbol="SPY260516C00520000")
        # Simulate what the exit branch does:
        if not __import__("re").match(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$", position.symbol):
            engine._record_fill(result, modeled_price=520.0, order_type="market")
        engine.risk.record_fill_slippage.assert_not_called()

    def test_slippage_recorded_normally_for_equity_exit(self, tmp_path):
        """_record_fill is NOT skipped for plain equity symbols."""
        engine = self._engine(tmp_path)
        engine.risk.record_fill_slippage = MagicMock()

        from execution.broker import OrderResult, OrderStatus
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="ord-2",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=100.5,
            raw_status="filled",
            message="",
        )
        engine._record_fill(
            result,
            modeled_price=100.0,
            benchmark_kind="arrival_midpoint",
            measurement_quality="primary",
            order_type="market",
        )
        engine.risk.record_fill_slippage.assert_called_once()

    # Fix 4: 100x multiplier for options P&L ─────────────────────────────────

    def test_record_realized_pnl_applies_100x_for_options(self, tmp_path):
        """Options P&L must be multiplied by 100 (one contract = 100 shares)."""
        from unittest.mock import MagicMock
        from risk.allocator import SleeveAllocator

        engine = self._engine(tmp_path)
        allocator = MagicMock(spec=SleeveAllocator)
        engine._allocator = allocator
        engine._entry_prices["SPY"] = 10.0  # option premium at entry

        # 2 contracts, exit premium $15, gain = (15-10)*2*100 = $1 000
        engine._record_realized_pnl("SPY", "spy_options_reversion", 15.0, 2, multiplier=100)
        allocator.record_realized_pnl.assert_called_once_with("spy_options_reversion", 1000.0, position_uid=None, is_full_close=True)

    def test_record_realized_pnl_no_multiplier_for_equity(self, tmp_path):
        """Equity P&L uses multiplier=1 (default) — result unchanged."""
        from unittest.mock import MagicMock
        from risk.allocator import SleeveAllocator

        engine = self._engine(tmp_path)
        allocator = MagicMock(spec=SleeveAllocator)
        engine._allocator = allocator
        engine._entry_prices["AAPL"] = 100.0

        # 10 shares, exit $105, gain = (105-100)*10*1 = $50
        engine._record_realized_pnl("AAPL", "sma_crossover", 105.0, 10)
        allocator.record_realized_pnl.assert_called_once_with("sma_crossover", 50.0, position_uid=None, is_full_close=True)

    # Fix A: _log_close uses option premium, not underlying bar price ──────────

    def test_log_close_uses_premium_not_underlying_for_options(self, tmp_path):
        """_log_close must receive the fill premium, not SPY bar price, for OCC exits."""
        from unittest.mock import MagicMock, patch, call
        from execution.broker import OrderResult, OrderStatus
        from types import SimpleNamespace

        engine = self._engine(tmp_path)
        occ = "SPY260516C00520000"
        fill_premium = 14.50

        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="opt-close-1",
            symbol=occ,
            requested_qty=2,
            filled_qty=2,
            avg_fill_price=fill_premium,
            raw_status="filled",
            message="",
        )

        logged: list[tuple] = []
        original_log_close = engine._log_close
        def capture_log_close(res, modeled_price, strategy_name=""):
            logged.append((res, modeled_price, strategy_name))
        engine._log_close = capture_log_close

        # Simulate what the exit branch does for an OCC position.
        import re as _re
        _OCC_PAT = _re.compile(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$")
        position = SimpleNamespace(symbol=occ)
        SPY_BAR_CLOSE = 520.0  # this must NOT end up as modeled_price
        _close_modeled = (
            result.avg_fill_price or 0.0
            if _OCC_PAT.match(position.symbol)
            else SPY_BAR_CLOSE
        )
        engine._log_close(result, _close_modeled, "spy_options_reversion")

        assert len(logged) == 1
        _, modeled, _ = logged[0]
        assert modeled == fill_premium, (
            f"modeled_price should be the option premium ({fill_premium}), "
            f"got {modeled} (SPY bar close was {SPY_BAR_CLOSE})"
        )

    def test_log_close_uses_bar_price_for_equity(self, tmp_path):
        """_log_close keeps using latest_close for plain equity exits (no regression)."""
        from execution.broker import OrderResult, OrderStatus
        from types import SimpleNamespace
        import re as _re

        engine = self._engine(tmp_path)
        logged: list[tuple] = []
        engine._log_close = lambda res, mp, sn="": logged.append((res, mp, sn))

        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="eq-close-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=101.0,
            raw_status="filled",
            message="",
        )
        _OCC_PAT = _re.compile(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$")
        position = SimpleNamespace(symbol="AAPL")
        AAPL_BAR_CLOSE = 100.0
        _close_modeled = (
            result.avg_fill_price or 0.0
            if _OCC_PAT.match(position.symbol)
            else AAPL_BAR_CLOSE
        )
        engine._log_close(result, _close_modeled, "sma_crossover")
        assert logged[0][1] == AAPL_BAR_CLOSE

    # Fix B: stream stop fill OCC → underlying normalisation ──────────────────

    def test_log_stop_fill_writes_correct_record(self, tmp_path):
        """log_stop_fill stores the real fill price, qty, and order_type=stop."""
        import sqlite3
        from reporting.logger import TradeLogger

        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log_stop_fill(
            symbol="SPY260516C00520000",
            strategy="spy_options_reversion",
            qty=2,
            avg_fill_price=7.50,
            order_id="bracket-stop-abc",
        )

        conn = sqlite3.connect(str(tmp_path / "trades.db"))
        row = conn.execute(
            "SELECT symbol, side, qty, avg_fill_price, order_type, status, "
            "filled_qty, reason, stop_price FROM trades ORDER BY rowid DESC LIMIT 1"
        ).fetchone()
        conn.close()

        symbol, side, qty, price, order_type, status, filled_qty, reason, stop_price = row
        assert symbol == "SPY260516C00520000"
        assert side == "sell"
        assert qty == 2
        assert price == 7.50
        assert order_type == "stop"
        assert status == "filled"
        assert filled_qty == 2
        assert reason == "stop_triggered"
        assert stop_price == 7.50

class TestGenericSingleLegOptionTrailingStops:
    class GenericOptionStrategy(FakeStrategy):
        name = "generic_single_leg_options"
        trail_activation_pct = 0.10
        trail_pct = 0.15
        config = SimpleNamespace(stop_loss_multiple=0.75)

        def __init__(self, *, entries: list[bool], exits: list[bool], edge_filter=None):
            super().__init__(entries=entries, exits=exits, edge_filter=edge_filter)
            self.restored_trailing_state: list[dict] = []

        def restore_trailing_state(
            self, occ: str, *, entry_premium: float, hwm_premium: float
        ) -> None:
            self.restored_trailing_state.append(
                {
                    "occ": occ,
                    "entry_premium": entry_premium,
                    "hwm_premium": hwm_premium,
                }
            )

    def _engine(
        self,
        tmp_path,
        *,
        create_lifecycle: bool = True,
        audit_enabled: bool = False,
        stream_manager=None,
    ):
        strategy = self.GenericOptionStrategy(entries=[False], exits=[False])
        broker = MagicMock()
        broker.get_open_orders.return_value = []
        broker.get_latest_option_quote.return_value = OptionQuote(
            symbol="SPY260618C00746000",
            bid_price=21.90,
            ask_price=22.10,
            timestamp=T0,
        )
        broker.submit_option_gtc_stop.return_value = OpenOrder(
            order_id="new-stop",
            symbol="SPY260618C00746000",
            side=Side.SELL,
            qty=3,
            order_type=OrderType.MARKET,
            status="accepted",
            submitted_at=T0,
            limit_price=None,
            stop_price=17.14,
            time_in_force="gtc",
        )
        broker.replace_option_stop.return_value = OpenOrder(
            order_id="replacement-stop",
            symbol="SPY260618C00746000",
            side=Side.SELL,
            qty=3,
            order_type=OrderType.MARKET,
            status="accepted",
            submitted_at=T0,
            limit_price=None,
            stop_price=18.70,
            time_in_force="gtc",
        )
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
        engine = TradingEngine(
            strategy=strategy,
            symbols=["SPY"],
            risk=risk,
            broker=broker,
            config=EngineConfig(
                market_hours_only=False,
                option_stop_replace_audit_enabled=audit_enabled,
                option_stop_replace_audit_strategy=strategy.name,
                option_stop_replace_audit_db_path=str(
                    tmp_path / "option-stop-audit.db"
                ),
            ),
            trade_logger=TradeLogger(path=str(tmp_path / "trades.db")),
            stream_manager=stream_manager,
            clock=lambda: T0,
        )
        engine._register_single_leg(
            strategy_name=strategy.name,
            symbol="SPY260618C00746000",
        )
        engine._entry_prices["SPY"] = 12.77
        if create_lifecycle:
            engine.lifecycle_store.create_pending(
                position_uid="pos_abc123",
                symbol="SPY260618C00746000",
                owner_key="SPY",
                strategy=strategy.name,
                position_type="single_leg",
                entry_qty=3,
            )
            engine.lifecycle_store.mark_open(
                position_uid="pos_abc123",
                avg_entry_price=12.77,
                current_qty=3,
            )
        return engine, broker

    def test_recreates_gtc_stop_from_persisted_hwm(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            stop_order_status="expired",
            last_observed_premium=15.50,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.submit_option_gtc_stop.assert_called_once_with(
            symbol="SPY260618C00746000",
            qty=3.0,
            stop_price=17.14,
            client_order_id=None,
            position_uid="pos_abc123",
        )
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.position_uid == "pos_abc123"
        assert row.hwm_premium == pytest.approx(20.16)
        assert row.alpaca_stop_order_id == "new-stop"
        assert engine.strategy.restored_trailing_state[-1] == {
            "occ": "SPY260618C00746000",
            "entry_premium": 12.77,
            "hwm_premium": 20.16,
        }

    def test_enabled_audit_records_initial_stop_submit(self, tmp_path):
        stream = MagicMock()
        engine, broker = self._engine(
            tmp_path,
            audit_enabled=True,
            stream_manager=stream,
        )
        before = BrokerOrderAuditSnapshot(
            order_id="new-stop",
            status="accepted",
            stop_price=17.14,
            qty=3.0,
            time_in_force="gtc",
            submitted_at=T0,
            updated_at=T0,
            replaced_at=None,
            filled_at=None,
            filled_avg_price=None,
            replaces_order_id=None,
            raw_json='{"id":"new-stop"}',
            fetch_started_at=T0,
            fetch_ended_at=T0,
            latency_ms=4.0,
        )
        broker.get_order_audit_snapshot.return_value = before
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            stop_order_status="expired",
            last_observed_premium=15.50,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[],
        )

        engine._sync_option_trailing_stops(snapshot)

        submit_kwargs = broker.submit_option_gtc_stop.call_args.kwargs
        assert submit_kwargs["client_order_id"].startswith(
            "opt-trail-audit-"
        )
        records = engine.option_stop_audit_store.read_records(
            occ_symbol="SPY260618C00746000"
        )
        assert [row["record_type"] for row in records] == [
            "initial_submit_decision",
            "initial_submit_result",
        ]
        assert records[0]["payload"]["quote"]["bid_price"] == pytest.approx(21.90)
        assert records[0]["payload"]["requested_stop_price"] == pytest.approx(17.14)
        assert records[1]["order_id"] == "new-stop"
        assert records[1]["payload"]["broker_after"]["order_id"] == "new-stop"
        stream.register_option_stop_audit.assert_called_once()
        stream.bind_option_stop_audit_alias.assert_called_once_with(
            records[0]["correlation_id"],
            "new-stop",
        )

    def test_enabled_audit_records_option_stop_fill_context(self, tmp_path):
        engine, broker = self._engine(tmp_path, audit_enabled=True)
        engine.lifecycle_orders_store.insert_pending(
            position_uid="pos_abc123",
            role="protective_stop",
            client_order_id="stop-client-1",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=3,
            intended_stop_price=17.14,
        )
        engine.lifecycle_orders_store.attach_broker_order_id(
            client_order_id="stop-client-1",
            order_id="stop-order-1",
            submitted_at=T0.isoformat(),
        )
        broker.get_latest_option_quote.return_value = OptionQuote(
            symbol="SPY260618C00746000",
            bid_price=16.85,
            ask_price=17.05,
            timestamp=T0 + timedelta(minutes=1),
        )
        event = OrderEvent(
            order_id="stop-order-1",
            status="filled",
            filled_qty=3.0,
            avg_fill_price=16.90,
            broker_updated_at=(T0 + timedelta(minutes=1)).isoformat(),
            execution_id="exec-1",
        )

        engine._maybe_dispatch_substrate_stop_fill(
            event=event,
            snapshot=_snapshot(positions={}, open_orders=[]),
        )

        records = engine.option_stop_audit_store.read_records(
            occ_symbol="SPY260618C00746000"
        )
        assert [row["record_type"] for row in records] == ["stop_fill_context"]
        payload = records[0]["payload"]
        assert records[0]["order_id"] == "stop-order-1"
        assert payload["client_order_id"] == "stop-client-1"
        assert payload["avg_fill_price"] == pytest.approx(16.90)
        assert payload["stop_price"] == pytest.approx(17.14)
        assert payload["adverse_slippage_bps"] == pytest.approx(
            140.02
        )
        assert payload["quote_at_dispatch"]["bid_price"] == pytest.approx(16.85)
        assert payload["execution_id"] == "exec-1"

    def test_startup_backfills_legacy_occ_lifecycle_then_recreates_stop(self, tmp_path):
        engine, broker = self._engine(tmp_path, create_lifecycle=False)
        engine.alerts.option_trailing_state_unverified = MagicMock()
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[],
        )

        engine._reconcile_position_lifecycle(snapshot)
        engine._sync_option_trailing_stops(snapshot)
        engine._sync_option_trailing_stops(snapshot)

        lifecycle_row = engine.lifecycle_store.get_open_for_owner_key("SPY")
        assert lifecycle_row is not None
        assert lifecycle_row.symbol == "SPY260618C00746000"
        assert lifecycle_row.strategy == "generic_single_leg_options"
        assert lifecycle_row.metadata["synthesized"] is True
        assert lifecycle_row.legs[0].symbol == "SPY260618C00746000"
        broker.submit_option_gtc_stop.assert_called_once_with(
            symbol="SPY260618C00746000",
            qty=3.0,
            stop_price=13.17,
            client_order_id=None,
            position_uid=lifecycle_row.position_uid,
        )
        trailing_row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert trailing_row.position_uid == lifecycle_row.position_uid
        assert trailing_row.entry_premium == pytest.approx(12.77)
        assert trailing_row.hwm_premium == pytest.approx(15.50)
        engine.alerts.option_trailing_state_unverified.assert_called_once_with(
            "SPY260618C00746000",
            "generic_single_leg_options",
            15.50,
        )

    def test_adequate_gtc_stop_is_left_unchanged(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        gtc_stop = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="gtc-stop",
            qty=3,
            time_in_force="gtc",
        )
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="gtc-stop",
            stop_order_status="accepted",
            last_observed_premium=15.50,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[gtc_stop],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.replace_option_stop.assert_not_called()
        broker.submit_option_gtc_stop.assert_not_called()
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.alpaca_stop_order_id == "gtc-stop"
        assert row.current_stop_price == pytest.approx(17.14)

    def test_ratchets_hwm_and_replaces_stale_stop_intraday(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        stale = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="old-stop",
            qty=3,
            time_in_force="gtc",
        )
        broker.replace_option_stop.return_value = replace(
            broker.replace_option_stop.return_value,
            stop_price=18.70,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=6_600.0,
                    current_price=22.00,
                )
            },
            open_orders=[stale],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.replace_option_stop.assert_called_once_with(
            order_id="old-stop",
            qty=3.0,
            stop_price=18.70,
            position_uid="pos_abc123",
        )
        broker.get_order_audit_snapshot.assert_not_called()
        assert not (tmp_path / "option-stop-audit.db").exists()
        broker.cancel_order.assert_not_called()
        broker.submit_option_gtc_stop.assert_not_called()
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.hwm_premium == pytest.approx(22.00)
        assert row.current_stop_price == pytest.approx(18.70)
        assert row.alpaca_stop_order_id == "replacement-stop"

    def test_opt_in_audit_is_separate_and_owner_scoped(self, tmp_path):
        stream = MagicMock()
        engine, broker = self._engine(
            tmp_path,
            audit_enabled=True,
            stream_manager=stream,
        )
        stale = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="old-stop",
            qty=3,
            time_in_force="gtc",
        )
        before = BrokerOrderAuditSnapshot(
            order_id="old-stop",
            status="new",
            stop_price=17.14,
            qty=3,
            time_in_force="gtc",
            submitted_at=T0 - timedelta(minutes=10),
            updated_at=T0 - timedelta(minutes=10),
            replaced_at=None,
            filled_at=None,
            filled_avg_price=None,
            replaces_order_id=None,
            raw_json='{"id":"old-stop"}',
            fetch_started_at=T0,
            fetch_ended_at=T0,
            latency_ms=4.0,
        )
        after = replace(
            before,
            order_id="replacement-stop",
            stop_price=18.70,
            replaces_order_id="old-stop",
            raw_json='{"id":"replacement-stop"}',
        )
        broker.get_order_audit_snapshot.side_effect = [before, after]
        broker.replace_option_stop.return_value = replace(
            broker.replace_option_stop.return_value,
            stop_price=18.70,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=6_600.0,
                    current_price=22.00,
                )
            },
            open_orders=[stale],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.get_order_audit_snapshot.assert_any_call("old-stop")
        broker.get_order_audit_snapshot.assert_any_call("replacement-stop")
        replace_kwargs = broker.replace_option_stop.call_args.kwargs
        assert replace_kwargs["client_order_id"].startswith(
            "opt-trail-audit-"
        )
        records = engine.option_stop_audit_store.read_records(
            occ_symbol="SPY260618C00746000"
        )
        assert [row["record_type"] for row in records] == [
            "decision_replace",
            "replace_result",
        ]
        assert records[0]["order_id"] == "old-stop"
        assert records[1]["order_id"] == "replacement-stop"
        assert records[0]["payload"]["broker_before"]["order_id"] == "old-stop"
        assert datetime.fromisoformat(records[0]["recorded_at"]) > T0
        stream.register_option_stop_audit.assert_called_once()
        stream.bind_option_stop_audit_alias.assert_called_once()

        with sqlite3.connect(tmp_path / "trades.db") as conn:
            table = conn.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table'
                  AND name = 'option_stop_replace_audit'
                """
            ).fetchone()
        assert table is None

    def test_enabled_audit_ignores_non_target_strategy(self, tmp_path):
        engine, broker = self._engine(tmp_path, audit_enabled=True)
        engine.config = replace(
            engine.config,
            option_stop_replace_audit_strategy="spy_options_reversion",
        )
        stale = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="old-stop",
            qty=3,
            time_in_force="gtc",
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=6_600.0,
                    current_price=22.00,
                )
            },
            open_orders=[stale],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.get_order_audit_snapshot.assert_not_called()
        assert "client_order_id" not in broker.replace_option_stop.call_args.kwargs
        assert engine.option_stop_audit_store.read_records() == []

    def test_enabled_audit_prunes_daily_during_long_session(self, tmp_path):
        stream = MagicMock()
        stream.drain_option_stop_audit_events.return_value = []
        engine, _broker = self._engine(
            tmp_path,
            audit_enabled=True,
            stream_manager=stream,
        )
        engine.option_stop_audit_store.prune_before = MagicMock(return_value=0)
        engine._option_stop_audit_last_pruned_at = (
            datetime.now(timezone.utc) - timedelta(hours=25)
        )

        engine._drain_option_stop_audit_events()

        engine.option_stop_audit_store.prune_before.assert_called_once()

    def test_ratchet_is_deferred_when_bid_does_not_support_new_stop(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        stale = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="old-stop",
            qty=3,
            time_in_force="gtc",
        )
        broker.get_latest_option_quote.return_value = OptionQuote(
            symbol="SPY260618C00746000",
            bid_price=19.00,
            ask_price=19.20,
            timestamp=T0,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=6_600.0,
                    current_price=22.00,
                )
            },
            open_orders=[stale],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.replace_option_stop.assert_not_called()
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.hwm_premium == pytest.approx(22.00)
        assert row.current_stop_price == pytest.approx(17.14)
        assert row.alpaca_stop_order_id == "old-stop"

    def test_ratchet_is_deferred_for_stale_or_wide_quote(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        stale_stop = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="old-stop",
            qty=3,
            time_in_force="gtc",
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=6_600.0,
                    current_price=22.00,
                )
            },
            open_orders=[stale_stop],
        )

        broker.get_latest_option_quote.return_value = OptionQuote(
            symbol="SPY260618C00746000",
            bid_price=21.90,
            ask_price=22.10,
            timestamp=T0 - timedelta(seconds=31),
        )
        engine._sync_option_trailing_stops(snapshot)
        broker.replace_option_stop.assert_not_called()

        broker.get_latest_option_quote.return_value = OptionQuote(
            symbol="SPY260618C00746000",
            bid_price=20.00,
            ask_price=25.00,
            timestamp=T0,
        )
        engine._sync_option_trailing_stops(snapshot)
        broker.replace_option_stop.assert_not_called()

    def test_quote_rejection_does_not_block_day_to_gtc_maintenance(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        day_stop = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="day-stop",
            qty=3,
            time_in_force="day",
        )
        broker.get_latest_option_quote.return_value = None
        broker.replace_option_stop.return_value = replace(
            broker.replace_option_stop.return_value,
            stop_price=17.14,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=6_600.0,
                    current_price=22.00,
                )
            },
            open_orders=[day_stop],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.replace_option_stop.assert_called_once_with(
            order_id="day-stop",
            qty=3.0,
            stop_price=17.14,
            position_uid="pos_abc123",
        )
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.current_stop_price == pytest.approx(17.14)

    def test_replace_failure_keeps_existing_stop_live(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        stale = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="old-stop",
            qty=3,
            time_in_force="gtc",
        )
        broker.replace_option_stop.side_effect = RuntimeError("api down")
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=6_600.0,
                    current_price=22.00,
                )
            },
            open_orders=[stale],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.cancel_order.assert_not_called()
        broker.submit_option_gtc_stop.assert_not_called()
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.stop_order_status == "replace_failed"
        assert row.alpaca_stop_order_id == "old-stop"
        assert row.current_stop_price == pytest.approx(17.14)

    def test_adequate_day_stop_is_migrated_to_gtc(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        day_stop = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="day-stop",
            qty=3,
            time_in_force="day",
        )
        broker.replace_option_stop.return_value = replace(
            broker.replace_option_stop.return_value,
            stop_price=17.14,
        )
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="day-stop",
            stop_order_status="accepted",
            last_observed_premium=15.50,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[day_stop],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.replace_option_stop.assert_called_once_with(
            order_id="day-stop",
            qty=3.0,
            stop_price=17.14,
            position_uid="pos_abc123",
        )

    def test_qty_mismatch_is_corrected_with_atomic_replace(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        undersized_stop = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="undersized-stop",
            qty=2,
            time_in_force="gtc",
        )
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=2,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="undersized-stop",
            stop_order_status="accepted",
            last_observed_premium=15.50,
        )
        broker.replace_option_stop.return_value = replace(
            broker.replace_option_stop.return_value,
            stop_price=17.14,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[undersized_stop],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.replace_option_stop.assert_called_once_with(
            order_id="undersized-stop",
            qty=3.0,
            stop_price=17.14,
            position_uid="pos_abc123",
        )

    def test_missing_tif_uses_matching_durable_gtc_identity(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        projected_stop = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="known-stop",
            qty=3,
            time_in_force=None,
        )
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="known-stop",
            stop_order_status="accepted",
            last_observed_premium=15.50,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[projected_stop],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.replace_option_stop.assert_not_called()

    def test_recent_submit_missing_from_snapshot_does_not_duplicate(self, tmp_path):
        engine, broker = self._engine(tmp_path)
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="new-stop",
            stop_order_status="accepted",
            last_observed_premium=20.16,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.submit_option_gtc_stop.assert_not_called()
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.alpaca_stop_order_id == "new-stop"
        assert row.stop_order_status == "accepted"

    def test_external_close_cleans_option_trailing_state(self, tmp_path):
        engine, _broker = self._engine(tmp_path)
        engine.config = replace(engine.config, external_close_confirm_cycles=1)
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="new-stop",
            stop_order_status="accepted",
            last_observed_premium=20.16,
        )
        engine._lookup_recent_stop_fill = MagicMock(return_value=None)
        engine.trade_logger.log_external_close = MagicMock()
        engine.alerts.broker_error = MagicMock()

        engine._detect_external_closes(_snapshot(positions={}))

        assert engine.option_trailing_store.get_by_occ("SPY260618C00746000") is None

    def test_recovered_stop_fill_cleans_option_trailing_state(self, tmp_path):
        engine, _broker = self._engine(tmp_path)
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="stop-1",
            stop_order_status="new",
            last_observed_premium=20.16,
        )

        stop_fill = ClosedOrderInfo(
            order_id="stop-1",
            client_order_id=None,
            symbol="SPY260618C00746000",
            side=Side.SELL,
            order_type="stop",
            status=OrderStatus.FILLED,
            raw_status="filled",
            qty=3.0,
            filled_qty=3.0,
            avg_fill_price=17.14,
            stop_price=17.14,
            submitted_at=T0,
            filled_at=T0 + timedelta(minutes=1),
        )

        result = engine._record_recovered_stop_fill(
            symbol="SPY",
            owner="generic_single_leg_options",
            stop_fill=stop_fill,
        )

        assert result is True
        assert engine.option_trailing_store.get_by_occ("SPY260618C00746000") is None

    def test_recovered_stop_fill_fallback_cleans_option_trailing_state(self, tmp_path):
        engine, _broker = self._engine(tmp_path)
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="stop-1",
            stop_order_status="new",
            last_observed_premium=20.16,
        )

        stop_fill = ClosedOrderInfo(
            order_id="stop-1",
            client_order_id=None,
            symbol="SPY260618C00746000",
            side=Side.SELL,
            order_type="stop",
            status=OrderStatus.FILLED,
            raw_status="filled",
            qty=3.0,
            filled_qty=0.0,
            avg_fill_price=None,
            stop_price=17.14,
            submitted_at=T0,
            filled_at=T0 + timedelta(minutes=1),
        )

        result = engine._record_recovered_stop_fill(
            symbol="SPY",
            owner="generic_single_leg_options",
            stop_fill=stop_fill,
        )

        assert result is False
        assert engine.option_trailing_store.get_by_occ("SPY260618C00746000") is None

    def test_recovered_exit_fill_cleans_option_trailing_state_on_full_close(
        self, tmp_path
    ):
        engine, _broker = self._engine(tmp_path)
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="stop-1",
            stop_order_status="new",
            last_observed_premium=20.16,
        )
        exit_fill = ClosedOrderInfo(
            order_id="exit-1",
            client_order_id=None,
            symbol="SPY260618C00746000",
            side=Side.SELL,
            order_type="market",
            status=OrderStatus.FILLED,
            raw_status="filled",
            qty=3.0,
            filled_qty=3.0,
            avg_fill_price=17.14,
            stop_price=None,
            submitted_at=T0,
            filled_at=T0 + timedelta(minutes=1),
        )

        result = engine._record_recovered_exit_fill(
            symbol="SPY",
            owner="generic_single_leg_options",
            exit_fill=exit_fill,
            is_full_close=True,
        )

        assert result is True
        assert engine.option_trailing_store.get_by_occ("SPY260618C00746000") is None

    def test_recovered_exit_fill_keeps_option_trailing_state_on_partial_close(
        self, tmp_path
    ):
        engine, _broker = self._engine(tmp_path)
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="stop-1",
            stop_order_status="new",
            last_observed_premium=20.16,
        )
        exit_fill = ClosedOrderInfo(
            order_id="exit-1",
            client_order_id=None,
            symbol="SPY260618C00746000",
            side=Side.SELL,
            order_type="market",
            status=OrderStatus.PARTIAL,
            raw_status="partially_filled",
            qty=3.0,
            filled_qty=1.0,
            avg_fill_price=17.14,
            stop_price=None,
            submitted_at=T0,
            filled_at=T0 + timedelta(minutes=1),
        )

        result = engine._record_recovered_exit_fill(
            symbol="SPY",
            owner="generic_single_leg_options",
            exit_fill=exit_fill,
            is_full_close=False,
        )

        assert result is True
        assert engine.option_trailing_store.get_by_occ("SPY260618C00746000") is not None


# ── Option trailing FK propagation end-to-end (PR #59 §10.4) ──────────────


class TestOptionTrailingLifecycleOrderIdFK:
    """End-to-end: real broker substrate writes populate trailing FK.

    The existing TestGenericSingleLegOptionTrailingStops suite uses a
    fully-mocked broker that does not call the substrate helpers, so
    the trailing FK in those tests ends up None (which is correct
    behavior for the mocked path). This suite wraps the broker mock so
    submit_option_gtc_stop / replace_option_stop write substrate rows
    the way the production AlpacaBroker now does, then asserts that
    the engine populates trailing.lifecycle_order_id from the
    substrate row's id.
    """

    GenericOptionStrategy = (
        TestGenericSingleLegOptionTrailingStops.GenericOptionStrategy
    )

    def _engine_with_substrate_broker(self, tmp_path):
        engine, broker = TestGenericSingleLegOptionTrailingStops()._engine(
            tmp_path,
        )
        # The mocked broker doesn't share execution/broker.py's
        # _lifecycle_orders_record_* helpers. Wire its submit/replace
        # side_effects to write the same substrate rows.
        orders_store = engine.lifecycle_orders_store
        assert orders_store is not None

        submit_return = broker.submit_option_gtc_stop.return_value

        def _submit_side_effect(*, symbol, qty, stop_price, position_uid=None,
                                client_order_id_prefix="opt-trail-stop",
                                client_order_id=None):
            client_order_id = client_order_id or (
                f"{client_order_id_prefix}-{position_uid or 'na'}"
            )
            orders_store.insert_pending(
                position_uid=position_uid,
                role="protective_stop",
                client_order_id=client_order_id,
                order_type="stop",
                order_class="simple",
                time_in_force="gtc",
                side="sell",
                intended_qty=float(qty),
                intended_stop_price=float(stop_price),
            )
            orders_store.attach_broker_order_id(
                client_order_id=client_order_id,
                order_id=submit_return.order_id,
            )
            return submit_return

        broker.submit_option_gtc_stop.side_effect = _submit_side_effect

        replace_return = broker.replace_option_stop.return_value

        def _replace_side_effect(*, order_id, qty, stop_price,
                                 client_order_id_prefix="opt-trail-stop",
                                 client_order_id=None,
                                 position_uid=None):
            cid = client_order_id or (
                f"{client_order_id_prefix}-replace-{position_uid or 'na'}"
            )
            orders_store.insert_pending(
                position_uid=position_uid,
                role="replacement_stop",
                client_order_id=cid,
                order_type="stop",
                order_class="simple",
                time_in_force="gtc",
                side="sell",
                intended_qty=float(qty),
                intended_stop_price=float(stop_price),
                replaces_order_id=order_id,
            )
            orders_store.attach_broker_order_id(
                client_order_id=cid,
                order_id=replace_return.order_id,
            )
            return replace_return

        broker.replace_option_stop.side_effect = _replace_side_effect

        return engine, broker, orders_store

    def test_fresh_submit_populates_trailing_fk(self, tmp_path):
        engine, broker, orders_store = self._engine_with_substrate_broker(
            tmp_path
        )
        # Pre-seed trailing state so _sync chooses the submit branch.
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            stop_order_status="expired",
            last_observed_premium=15.50,
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=4_650.0,
                    current_price=15.50,
                )
            },
            open_orders=[],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.submit_option_gtc_stop.assert_called_once()
        substrate = orders_store.get_by_order_id("new-stop")
        assert substrate is not None
        assert substrate.role == "protective_stop"
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.lifecycle_order_id == substrate.id
        assert row.alpaca_stop_order_id == "new-stop"

    def test_replacement_advances_trailing_fk_to_new_substrate_row(
        self, tmp_path
    ):
        engine, broker, orders_store = self._engine_with_substrate_broker(
            tmp_path
        )
        # Seed: the existing protective_stop row + matching trailing row
        # with FK pointing at it.
        old_substrate_id = orders_store.insert_pending(
            position_uid="pos_abc123",
            role="protective_stop",
            client_order_id="opt-trail-stop-seed",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=3.0,
            intended_stop_price=17.14,
        )
        orders_store.attach_broker_order_id(
            client_order_id="opt-trail-stop-seed",
            order_id="old-stop",
        )
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="old-stop",
            stop_order_status="accepted",
            last_observed_premium=15.50,
            lifecycle_order_id=old_substrate_id,
        )
        stale = replace(
            _open_stop_order("SPY260618C00746000", stop_price=17.14),
            order_id="old-stop",
            qty=3,
            time_in_force="gtc",
        )
        snapshot = _snapshot(
            positions={
                "SPY260618C00746000": Position(
                    symbol="SPY260618C00746000",
                    qty=3,
                    avg_entry_price=12.77,
                    market_value=6_600.0,
                    current_price=22.00,
                )
            },
            open_orders=[stale],
        )

        engine._sync_option_trailing_stops(snapshot)

        broker.replace_option_stop.assert_called_once()
        new_substrate = orders_store.get_by_order_id("replacement-stop")
        assert new_substrate is not None
        assert new_substrate.role == "replacement_stop"
        assert new_substrate.replaces_order_id == "old-stop"
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        # FK advanced to the new substrate row, not the old one.
        assert row.lifecycle_order_id == new_substrate.id
        assert row.lifecycle_order_id != old_substrate_id
        assert row.alpaca_stop_order_id == "replacement-stop"


class TestOptionTrailingFKPreservationOnTransientError:
    """PR #71 review P1 #2 + P2: FK survives lookup failures, defends roles."""

    GenericOptionStrategy = (
        TestGenericSingleLegOptionTrailingStops.GenericOptionStrategy
    )

    def _engine(self, tmp_path):
        engine, broker = TestGenericSingleLegOptionTrailingStops()._engine(
            tmp_path
        )
        return engine, broker

    def _seed_fk(self, engine, *, fk: int) -> None:
        engine.option_trailing_store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.14,
            alpaca_stop_order_id="old-stop",
            stop_order_status="accepted",
            last_observed_premium=15.50,
            lifecycle_order_id=fk,
        )

    def test_lookup_failed_sentinel_preserves_prior_fk(self, tmp_path):
        """Transient store exception must not demote FK to NULL."""
        engine, _broker = self._engine(tmp_path)
        # Seed substrate so the FK is valid up front, then break the
        # store at read time.
        old_id = engine.lifecycle_orders_store.insert_pending(
            position_uid="pos_abc123",
            role="protective_stop",
            client_order_id="seed-cid",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=3.0,
            intended_stop_price=17.14,
        )
        engine.lifecycle_orders_store.attach_broker_order_id(
            client_order_id="seed-cid", order_id="old-stop",
        )
        self._seed_fk(engine, fk=old_id)

        engine.lifecycle_orders_store.get_by_order_id = MagicMock(
            side_effect=RuntimeError("transient store error")
        )
        engine.lifecycle_orders_store.get_by_client_order_id = MagicMock(
            side_effect=RuntimeError("transient store error")
        )

        resolved = engine._resolve_trailing_fk_or_preserve(
            broker_order_id="some-new-id",
            client_order_id="some-cid",
            previous_fk=old_id,
            previous_mirror_order_id="totally-different",
            owner="generic_single_leg_options",
            occ="SPY260618C00746000",
            load_bearing=False,
        )

        assert resolved == old_id  # preserved across the exception

    def test_preserve_skips_lookup_when_broker_order_id_unchanged(
        self, tmp_path
    ):
        """Adequate-stop / replace-failed paths must not query each cycle."""
        engine, _broker = self._engine(tmp_path)
        engine.lifecycle_orders_store.get_by_order_id = MagicMock()

        resolved = engine._resolve_trailing_fk_or_preserve(
            broker_order_id="old-stop",
            client_order_id="seed-cid",
            previous_fk=77,
            previous_mirror_order_id="old-stop",
            owner="generic_single_leg_options",
            occ="SPY260618C00746000",
            load_bearing=False,
        )

        assert resolved == 77
        engine.lifecycle_orders_store.get_by_order_id.assert_not_called()

    def test_orphan_recovery_via_client_order_id(self, tmp_path):
        """attach_broker_order_id failure leaves order_id=NULL — recover."""
        engine, _broker = self._engine(tmp_path)
        # Simulate the orphan: substrate row inserted, attach failed,
        # so order_id is NULL but client_order_id is set.
        orphan_id = engine.lifecycle_orders_store.insert_pending(
            position_uid="pos_abc123",
            role="protective_stop",
            client_order_id="orphan-cid",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=3.0,
            intended_stop_price=17.14,
        )

        # Engine resolves with the broker order_id + the client_order_id
        # we have post-submit.
        resolved = engine._lifecycle_order_id_for(
            "broker-real-id",
            client_order_id="orphan-cid",
        )

        assert resolved == orphan_id
        # Opportunistic re-attach happened in the lookup.
        substrate = engine.lifecycle_orders_store.get_by_id(orphan_id)
        assert substrate.order_id == "broker-real-id"

    def test_load_bearing_miss_emits_critical(self, tmp_path):
        """Submit/replace success path must surface orphan condition."""
        from loguru import logger as _logger
        engine, _broker = self._engine(tmp_path)

        captured: list[str] = []
        sink_id = _logger.add(
            lambda msg: captured.append(str(msg)),
            level="CRITICAL",
            format="{message}",
        )
        try:
            resolved = engine._resolve_trailing_fk_or_preserve(
                broker_order_id="ghost-stop",
                client_order_id="ghost-cid",
                previous_fk=None,
                previous_mirror_order_id=None,
                owner="generic_single_leg_options",
                occ="SPY260618C00746000",
                load_bearing=True,
            )
        finally:
            _logger.remove(sink_id)

        assert resolved is None
        assert any("FK could not be resolved" in line for line in captured)

    def test_non_load_bearing_miss_silent(self, tmp_path):
        """Preserve paths must not log CRITICAL on a genuine not-found."""
        from loguru import logger as _logger
        engine, _broker = self._engine(tmp_path)

        captured: list[str] = []
        sink_id = _logger.add(
            lambda msg: captured.append(str(msg)),
            level="CRITICAL",
            format="{message}",
        )
        try:
            resolved = engine._resolve_trailing_fk_or_preserve(
                broker_order_id="ghost-stop",
                client_order_id="ghost-cid",
                previous_fk=None,
                previous_mirror_order_id=None,
                owner="generic_single_leg_options",
                occ="SPY260618C00746000",
                load_bearing=False,
            )
        finally:
            _logger.remove(sink_id)

        assert resolved is None
        assert not any("FK could not be resolved" in line for line in captured)

    def test_authoritative_identity_rejects_role_mismatch(self, tmp_path):
        """FK pointing at an entry_primary row → fall back to mirror."""
        engine, _broker = self._engine(tmp_path)
        cross_id = engine.lifecycle_orders_store.insert_pending(
            position_uid="pos_abc123",
            role="entry_primary",
            client_order_id="entry-cid",
            order_type="market",
            order_class="simple",
            time_in_force="day",
            side="buy",
            intended_qty=3.0,
        )
        engine.lifecycle_orders_store.attach_broker_order_id(
            client_order_id="entry-cid", order_id="entry-broker-id",
        )
        self._seed_fk(engine, fk=cross_id)
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")

        order_id, status = engine._option_trailing_authoritative_identity(row)

        # Mirror, not substrate, because the substrate role is wrong.
        assert order_id == "old-stop"
        assert status == "accepted"

    def test_authoritative_identity_rejects_position_uid_mismatch(
        self, tmp_path
    ):
        """FK pointing at another position's stop → fall back to mirror."""
        engine, _broker = self._engine(tmp_path)
        # Create a second position + its protective_stop row. Different
        # owner_key so the active-position uniqueness index doesn't fire.
        engine.lifecycle_store.create_pending(
            position_uid="pos_OTHER",
            symbol="QQQ260618C00450000",
            owner_key="QQQ",
            strategy="generic_single_leg_options",
            position_type="single_leg",
            entry_qty=1,
        )
        engine.lifecycle_store.mark_open(
            position_uid="pos_OTHER",
            avg_entry_price=10.0,
            current_qty=1,
        )
        other_id = engine.lifecycle_orders_store.insert_pending(
            position_uid="pos_OTHER",
            role="protective_stop",
            client_order_id="other-cid",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=1.0,
            intended_stop_price=8.0,
        )
        engine.lifecycle_orders_store.attach_broker_order_id(
            client_order_id="other-cid", order_id="other-broker-id",
        )
        self._seed_fk(engine, fk=other_id)
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")

        order_id, status = engine._option_trailing_authoritative_identity(row)

        # Mirror, not substrate.
        assert order_id == "old-stop"
        assert status == "accepted"


# ── Shared-symbol conflict rejection (11.7 Part A) ─────────────────────────


class TestSharedSymbolConflict:
    """A second strategy cannot enter a symbol another strategy already owns."""

    def _process(self, engine, symbol, snap, slot_index: int = 0):
        slot = engine.slots[slot_index]
        return engine._process_symbol(
            symbol, snap, snap.account, slot.strategy, slot.timeframe
        )

    def test_entry_blocked_when_symbol_owned_by_other_strategy(self, engine_factory):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        engine._register_single_leg(strategy_name="rsi_reversion", symbol="AAPL")
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        result = self._process(engine, "AAPL", snap)
        assert result is None
        broker.place_order.assert_not_called()
        broker.close_position.assert_not_called()

    def test_same_strategy_re_entry_not_blocked_by_conflict_check(self, engine_factory):
        """Self-ownership must not trip the cross-strategy conflict rule.
        (Risk DUPLICATE_POSITION handles same-strategy double entries separately.)"""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        # Reaches risk; risk will not raise — no broker position so no duplicate.
        # The key assertion is: the conflict check itself does not block.
        self._process(engine, "AAPL", snap)
        # place_order called once means we got past the conflict gate.
        assert broker.place_order.call_count == 1

    def test_conflict_fires_alert_with_symbol_conflict_code(self, engine_factory):
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        engine._register_single_leg(strategy_name="rsi_reversion", symbol="AAPL")
        engine.alerts = MagicMock()
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        self._process(engine, "AAPL", snap)
        engine.alerts.order_rejection.assert_called_once()
        _, kwargs = engine.alerts.order_rejection.call_args, engine.alerts.order_rejection.call_args.args
        # 4th positional arg is the rejection code.
        code = engine.alerts.order_rejection.call_args.args[3]
        assert code == "SYMBOL_CONFLICT"

    def test_conflict_marks_watchlist_status(self, engine_factory):
        engine, _broker = engine_factory(entries=[False] * 59 + [True])
        engine._register_single_leg(strategy_name="rsi_reversion", symbol="AAPL")
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        statuses: dict[str, str] = {}
        reasons: dict[str, list[str]] = {}
        slot = engine.slots[0]
        engine._process_symbol(
            "AAPL",
            snap,
            snap.account,
            slot.strategy,
            slot.timeframe,
            strategy_statuses=statuses,
            strategy_reasons=reasons,
        )
        assert statuses["AAPL"] == "Symbol Conflict"
        assert reasons["AAPL"] == ["owned by 'rsi_reversion'"]

    def test_exit_path_unaffected_by_conflict_check(self, engine_factory):
        """Exits must never be blocked by the symbol-conflict rule —
        only entries pass through it."""
        engine, broker = engine_factory(exits=[False] * 59 + [True])
        # The owner-mismatch exit path is already gated by line 736 in
        # _process_symbol (existing behavior). The new conflict check is
        # only on the entry path. Confirm an exit still routes correctly.
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")  # this strategy owns it
        positions = {"AAPL": Position("AAPL", 10, 100.0, 1010.0)}
        snap = _snapshot(positions=positions)
        engine._session_start_equity = snap.account.equity
        self._process(engine, "AAPL", snap)
        broker.close_position.assert_called_once_with("AAPL", position_uid=None)


# ── Sector exposure observability (11.7 Part B) ────────────────────────────


class TestSectorExposure:
    """_compute_sector_exposure builds {sector_key: count} from owners."""

    def _engine(self, engine_factory, resolver):
        engine, _ = engine_factory()
        engine._sector_resolver = resolver
        return engine

    def test_empty_when_no_positions(self, engine_factory):
        resolver = MagicMock()
        resolver.resolve.return_value = "technology"
        engine = self._engine(engine_factory, resolver)
        assert engine._compute_sector_exposure() == {}
        resolver.resolve.assert_not_called()

    def test_empty_when_no_resolver(self, engine_factory):
        engine, _ = engine_factory()
        engine._sector_resolver = None
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        assert engine._compute_sector_exposure() == {}

    def test_groups_symbols_and_strategies_by_sector(self, engine_factory):
        resolver = MagicMock()
        resolver.resolve.side_effect = lambda s: {
            "AAPL": "technology",
            "MSFT": "technology",
            "JPM": "financials",
        }.get(s)
        engine = self._engine(engine_factory, resolver)
        engine._register_single_leg(strategy_name="sma_crossover", symbol="AAPL")
        engine._register_single_leg(strategy_name="donchian_breakout", symbol="MSFT")
        engine._register_single_leg(strategy_name="rsi_reversion", symbol="JPM")
        exposure = engine._compute_sector_exposure()
        assert set(exposure.keys()) == {"technology", "financials"}
        # technology has both AAPL and MSFT with their respective owners
        tech_items = {
            (item["symbol"], item["strategy"]) for item in exposure["technology"]
        }
        assert tech_items == {
            ("AAPL", "sma_crossover"),
            ("MSFT", "donchian_breakout"),
        }
        # financials has just JPM
        assert exposure["financials"] == [
            {"symbol": "JPM", "strategy": "rsi_reversion"}
        ]

    def test_unmapped_symbol_skipped(self, engine_factory):
        resolver = MagicMock()
        resolver.resolve.side_effect = lambda s: None if s == "XYZ" else "technology"
        engine = self._engine(engine_factory, resolver)
        engine._register_single_leg(strategy_name="sma_crossover", symbol="AAPL")
        engine._register_single_leg(strategy_name="sma_crossover", symbol="XYZ")
        exposure = engine._compute_sector_exposure()
        assert list(exposure.keys()) == ["technology"]
        assert exposure["technology"] == [
            {"symbol": "AAPL", "strategy": "sma_crossover"}
        ]

    def test_occ_option_symbol_excluded(self, engine_factory):
        resolver = MagicMock()
        resolver.resolve.return_value = "technology"
        engine = self._engine(engine_factory, resolver)
        # OCC contract symbol format: ROOT + YYMMDD + C/P + 8-digit strike
        engine._register_single_leg(strategy_name="sma_crossover", symbol="AAPL")
        engine._register_single_leg(
            strategy_name="spy_options_reversion",
            symbol="SPY251219C00450000",
        )
        exposure = engine._compute_sector_exposure()
        assert list(exposure.keys()) == ["technology"]
        assert exposure["technology"] == [
            {"symbol": "AAPL", "strategy": "sma_crossover"}
        ]
        # Resolver never called for the OCC symbol.
        assert all(
            call.args[0] != "SPY251219C00450000"
            for call in resolver.resolve.call_args_list
        )

    def test_resolver_exception_fails_open(self, engine_factory):
        resolver = MagicMock()
        resolver.resolve.side_effect = RuntimeError("yfinance down")
        engine = self._engine(engine_factory, resolver)
        engine._register_single_leg(strategy_name="sma_crossover", symbol="AAPL")
        # Should not raise; counts that symbol as unmapped.
        exposure = engine._compute_sector_exposure()
        assert exposure == {}


# ── Slippage unification Defect 1 fix ──────────────────────────────────────


class TestExitPathBenchmarkKind:
    """
    Codepath §3 — discretionary market exits.

    The exit path now fetches an NBBO midpoint immediately before
    submitting the close, mirroring the entry path and satisfying the
    design doc's fill-type contract §3 ("arrival midpoint at close
    submission"). Tagging:

      - equity exit, quote available   → 'arrival_midpoint' / 'primary'
      - equity exit, quote unavailable → 'fallback_latest_close' / 'fallback'
      - option exit                    → 'unavailable' / 'unavailable'

    The Phase 1 Defect 1 invariant still holds: nothing may be tagged
    'arrival_midpoint' unless an arrival midpoint was actually observed.
    Options are never tagged that way — OCC symbols are skipped entirely
    because they belong to OPRA, not the stock quote endpoint.
    """

    def _engine_with_real_logger(self, tmp_path):
        from strategies.base import StrategySlot
        from data.watchlists import StaticWatchlistSource
        from execution.broker import BrokerSnapshot

        strategy = FakeStrategy(entries=[False], exits=[False])
        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.close_position.return_value = _filled_result("AAPL", 10, 149.50)
        broker.get_open_orders.return_value = []
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

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
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        return TradingEngine(
            strategy=strategy,
            symbols=["AAPL"],
            risk=risk,
            broker=broker,
            trade_logger=tl,
            config=EngineConfig(
                history_lookback_days=120,
                cycle_interval_seconds=0.01,
                max_bar_age_multiplier=10.0,
                market_hours_only=False,
            ),
        ), broker

    @staticmethod
    def _equity_position_and_snapshot():
        from execution.broker import BrokerSnapshot
        position = SimpleNamespace(
            qty=10, symbol="AAPL", avg_entry_price=148.0,
            market_value=1480.0, unrealized_pl=0.0,
            current_price=148.0, cost_basis=1480.0,
            asset_id="x", side="long",
        )
        snap = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0,
                cash=50_000.0,
                buying_power=50_000.0,
                open_positions={"AAPL": position},
            ),
            open_orders=[],
        )
        return position, snap

    def test_equity_exit_tags_fallback_latest_close_without_quote(self, tmp_path):
        """No arrival quote → the row falls back to the prior bar close
        and says so, exactly as before this path learned to fetch."""
        engine, broker = self._engine_with_real_logger(tmp_path)
        broker.get_latest_quote_midpoint.return_value = None
        position, snap = self._equity_position_and_snapshot()
        engine._log_close = MagicMock()
        engine._close_single_leg_position(
            symbol="AAPL",
            strategy=engine.strategy,
            position=position,
            snapshot=snap,
            latest_close=149.00,
            alert_reason="exit signal",
        )
        kwargs = engine._log_close.call_args.kwargs
        assert kwargs["benchmark_kind"] == "fallback_latest_close"
        assert kwargs["measurement_quality"] == "fallback"
        # Implementation shortfall must never reach the drift alarm.
        assert len(engine.risk._slippage_samples) == 0

    def test_equity_exit_tags_arrival_midpoint_when_quote_available(self, tmp_path):
        """Quote available → the exit is measured against live market
        state, tagged as a primary execution-quality measurement, and
        feeds the drift kill switch."""
        engine, broker = self._engine_with_real_logger(tmp_path)
        broker.get_latest_quote_midpoint.return_value = 149.60
        position, snap = self._equity_position_and_snapshot()
        engine._log_close = MagicMock()
        engine._close_single_leg_position(
            symbol="AAPL",
            strategy=engine.strategy,
            position=position,
            snapshot=snap,
            latest_close=149.00,
            alert_reason="exit signal",
        )
        kwargs = engine._log_close.call_args.kwargs
        assert kwargs["benchmark_kind"] == "arrival_midpoint"
        assert kwargs["measurement_quality"] == "primary"
        # Benchmark is the arrival midpoint, not the bar close.
        assert engine._log_close.call_args.args[1] == pytest.approx(149.60)
        # Sold at 149.50 against a 149.60 arrival → adverse.
        assert len(engine.risk._slippage_samples) == 1
        _, adverse_bps = engine.risk._slippage_samples[0]
        assert adverse_bps == pytest.approx(
            (149.60 - 149.50) / 149.60 * 10_000, rel=1e-3
        )

    def test_equity_exit_quote_fetched_before_close_submitted(self, tmp_path):
        """The midpoint must be captured before the close is submitted —
        a quote read afterwards is contaminated by our own order."""
        engine, broker = self._engine_with_real_logger(tmp_path)
        call_order: list[str] = []
        broker.get_latest_quote_midpoint.side_effect = (
            lambda *_a, **_k: call_order.append("quote") or 149.60
        )
        broker.close_position.side_effect = (
            lambda *_a, **_k: call_order.append("close")
            or _filled_result("AAPL", 10, 149.50)
        )
        position, snap = self._equity_position_and_snapshot()
        engine._log_close = MagicMock()
        engine._close_single_leg_position(
            symbol="AAPL",
            strategy=engine.strategy,
            position=position,
            snapshot=snap,
            latest_close=149.00,
            alert_reason="exit signal",
        )
        assert call_order == ["quote", "close"]

    def test_option_exit_tags_unavailable(self, tmp_path):
        engine, broker = self._engine_with_real_logger(tmp_path)
        occ = "SPY260620C00520000"
        broker.close_position.return_value = _filled_result(occ, 2, 12.50)
        from execution.broker import BrokerSnapshot
        position = SimpleNamespace(
            qty=2, symbol=occ, avg_entry_price=10.0,
            market_value=2000.0, unrealized_pl=500.0,
            current_price=12.5, cost_basis=2000.0,
            asset_id="x", side="long",
        )
        snap = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0,
                cash=50_000.0,
                buying_power=50_000.0,
                open_positions={occ: position},
            ),
            open_orders=[],
        )
        engine._log_close = MagicMock()
        engine._close_single_leg_position(
            symbol="SPY",
            strategy=engine.strategy,
            position=position,
            snapshot=snap,
            latest_close=12.50,
            alert_reason="exit signal",
        )
        kwargs = engine._log_close.call_args.kwargs
        # Options have no honest benchmark at exit — must be unavailable,
        # never the fill price masquerading as 'arrival_midpoint'.
        assert kwargs["benchmark_kind"] == "unavailable"
        assert kwargs["measurement_quality"] == "unavailable"

    # P-7: test_unknown_exit_is_staged_and_reconciled_by_exact_order_id
    # removed. Exercised the legacy _suspect_exit_orders cache, which
    # the substrate exit dispatch (P-7 commit A) replaced. Coverage
    # moved to:
    #   - tests/test_apply_order_event.py::TestSubstrateExitFillDispatchSemantics
    #   - tests/test_apply_order_event.py::TestStreamDrainEndToEnd (exit row variant)


# ── P-6: TestSuspectOrderBenchmarkProvenance removed ──────────────────────
#
# Tested that the legacy _suspect_orders cache preserved the
# benchmark provenance kind (arrival_midpoint vs fallback_latest_close)
# through the recovery path. The cache is gone; the substrate's
# per-order row now carries provenance from submit time onward, and
# apply_order_event's UPSERT preserves it. Coverage moved to:
#   - tests/test_apply_order_event.py::TestExpandedUpsertPreservation
#   - tests/test_apply_order_event.py::TestTradesUpsertProvenancePreservation
#   - tests/test_broker.py::TestSlippageProvenancePlumbing


# (class deleted; coverage moved as documented above)


# ── PLAN 11.47 R1 P1-1: STOP_LIMIT substrate reconstruction ─────────────────


class TestSubstrateStopLimitReconstruction:
    """The async substrate-recovery path reconstructs a RiskDecision
    from the per-order row when the synchronous place_order path
    didn't bind ownership. STOP_LIMIT requires both
    entry_trigger_price and limit_price on the RiskDecision —
    without them __post_init__ raises and the dispatch is caught
    as CRITICAL with ownership unbound.

    Before R1 P1-1, the reconstruction at engine/trader.py:~2079
    only passed order_type=STOP_LIMIT but no trigger/limit, which
    raised ValueError every time a Donchian fill arrived via
    stream / cycle / startup reconcile.
    """

    def _engine_with_mocked_stores(self, patch_fetch, tmp_path):
        engine, _broker, _tl = _engine_with_db(patch_fetch, tmp_path)
        engine.lifecycle_orders_store = MagicMock()
        engine.lifecycle_store = MagicMock()
        return engine

    def _stop_limit_order_row(self):
        return SimpleNamespace(
            position_uid="pos_test_1",
            role="entry_primary",
            order_type="stop_limit",
            intended_qty=10.0,
            intended_stop_price=110.0,
            intended_trigger_price=121.50,
            intended_limit_price=125.00,
        )

    def _pos_row(self):
        return SimpleNamespace(
            position_uid="pos_test_1",
            strategy="donchian_breakout",
            symbol="NVDA",
        )

    def _filled_event(self):
        return SimpleNamespace(
            order_id="alpaca-ord-1",
            status="filled",
            filled_qty=10.0,
            avg_fill_price=122.0,
        )

    def test_stop_limit_reconstruction_passes_trigger_and_limit(
        self, patch_fetch, tmp_path
    ):
        engine = self._engine_with_mocked_stores(patch_fetch, tmp_path)
        engine.lifecycle_orders_store.get_by_order_id.return_value = (
            self._stop_limit_order_row()
        )
        engine.lifecycle_store.get_by_position_uid.return_value = self._pos_row()

        # Snapshot with an open NVDA position so dispatch reaches reconstruction.
        position = Position("NVDA", 10, 122.0, 1220.0)
        snapshot = _snapshot(positions={"NVDA": position})

        # Capture the RiskDecision the side-effects helper receives.
        captured: dict = {}

        def _capture(*, snapshot, position, decision, fill_price, fill_qty,
                     reason_suffix, **kwargs):
            # **kwargs so adding an optional argument to the real helper
            # does not break this stub with a confusing TypeError that
            # only surfaces as a swallowed CRITICAL in the dispatch path.
            captured["decision"] = decision
            captured.update(kwargs)

        engine._apply_recovered_entry_side_effects = _capture

        # Must not raise — would raise pre-R1-P1-1 because STOP_LIMIT
        # RiskDecision validation rejects missing trigger/limit.
        engine._maybe_dispatch_substrate_entry_fill(
            event=self._filled_event(),
            snapshot=snapshot,
        )

        decision = captured["decision"]
        assert decision is not None
        assert decision.order_type is OrderType.STOP_LIMIT
        assert decision.entry_trigger_price == 121.50
        assert decision.limit_price == 125.00
        assert decision.stop_price == 110.0
        assert decision.qty == 10.0

    def test_stop_limit_reconstruction_falls_back_when_trigger_missing(
        self, patch_fetch, tmp_path
    ):
        # Legacy substrate rows (written before P1-1 shipped) lack
        # intended_trigger_price. Dispatch should warn and fall back to
        # MARKET rather than blowing up — the side effects fire
        # regardless of order type.
        engine = self._engine_with_mocked_stores(patch_fetch, tmp_path)
        legacy_row = self._stop_limit_order_row()
        legacy_row.intended_trigger_price = None  # legacy gap
        legacy_row.intended_limit_price = None
        engine.lifecycle_orders_store.get_by_order_id.return_value = legacy_row
        engine.lifecycle_store.get_by_position_uid.return_value = self._pos_row()

        position = Position("NVDA", 10, 122.0, 1220.0)
        snapshot = _snapshot(positions={"NVDA": position})

        captured: dict = {}

        def _capture(*, snapshot, position, decision, fill_price, fill_qty,
                     reason_suffix, **kwargs):
            # **kwargs so adding an optional argument to the real helper
            # does not break this stub with a confusing TypeError that
            # only surfaces as a swallowed CRITICAL in the dispatch path.
            captured["decision"] = decision
            captured.update(kwargs)

        engine._apply_recovered_entry_side_effects = _capture

        engine._maybe_dispatch_substrate_entry_fill(
            event=self._filled_event(),
            snapshot=snapshot,
        )

        # Fell back to MARKET; side effects still fired.
        assert captured["decision"].order_type is OrderType.MARKET
        assert captured["decision"].entry_trigger_price is None
        assert captured["decision"].limit_price is None


class TestSlippageMonitorSeeding:
    """Engine-side wiring of the kill switch's restart rehydration.

    The engine owns the database read and hands plain samples to
    `RiskManager`, keeping risk free of trade-log knowledge — the same
    shape as the per-fill push. Seeding must never be able to block
    startup: an advisory monitor is not worth failing a boot over.
    """

    def _engine(self, tmp_path, trade_logger=None):
        strategy = FakeStrategy(entries=[False], exits=[False])
        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.get_open_orders.return_value = []
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)
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
        return TradingEngine(
            strategy=strategy,
            symbols=["AAPL"],
            risk=risk,
            broker=broker,
            trade_logger=trade_logger or TradeLogger(
                path=str(tmp_path / "trades.db")
            ),
            config=EngineConfig(
                history_lookback_days=120,
                cycle_interval_seconds=0.01,
                max_bar_age_multiplier=10.0,
                market_hours_only=False,
            ),
        )

    def test_seeds_pool_from_trade_log(self, tmp_path):
        from config.settings import SLIPPAGE_MODEL_MARKET_BPS

        tl = MagicMock()
        tl.read_recent_execution_quality_slippage.return_value = [1.0, 2.0, 3.0]
        engine = self._engine(tmp_path, trade_logger=tl)
        engine._seed_slippage_monitor()
        assert len(engine.risk._slippage_samples) == 3
        modeled, adverse = engine.risk._slippage_samples[0]
        assert modeled == pytest.approx(SLIPPAGE_MODEL_MARKET_BPS)
        assert adverse == pytest.approx(1.0)

    def test_seed_read_failure_is_not_fatal(self, tmp_path):
        tl = MagicMock()
        tl.read_recent_execution_quality_slippage.side_effect = RuntimeError("boom")
        engine = self._engine(tmp_path, trade_logger=tl)
        engine._seed_slippage_monitor()  # must not raise
        assert len(engine.risk._slippage_samples) == 0

    def test_seed_rejection_is_not_fatal(self, tmp_path):
        """A corrupt row that trips RiskManager's validation leaves the
        pool empty rather than killing startup."""
        tl = MagicMock()
        tl.read_recent_execution_quality_slippage.return_value = [1.0]
        engine = self._engine(tmp_path, trade_logger=tl)
        engine.risk.seed_slippage_samples = MagicMock(
            side_effect=ValueError("negative")
        )
        engine._seed_slippage_monitor()  # must not raise
        assert len(engine.risk._slippage_samples) == 0

    def test_empty_history_leaves_pool_empty(self, tmp_path):
        tl = MagicMock()
        tl.read_recent_execution_quality_slippage.return_value = []
        engine = self._engine(tmp_path, trade_logger=tl)
        engine._seed_slippage_monitor()
        assert len(engine.risk._slippage_samples) == 0

    def test_seed_requests_full_deque_window(self, tmp_path):
        tl = MagicMock()
        tl.read_recent_execution_quality_slippage.return_value = []
        engine = self._engine(tmp_path, trade_logger=tl)
        engine._seed_slippage_monitor()
        tl.read_recent_execution_quality_slippage.assert_called_once_with(
            engine.risk._slippage_samples.maxlen
        )


class TestSubstrateStopFillSeamEndToEnd:
    """The seam that broke twice: `apply_order_event` INSERTing the sell
    row, then the engine dispatching to `log_stop_fill`.

    Both previous fixes were verified only on each half in isolation.
    The first missed that the risk basis was lost because the open-state
    replay nets to zero once the sell row exists. The second missed that
    `entry_timestamp` stayed wrong because the substrate stamped `:now`
    into it and TradeLogger's upsert is PRESERVE-FIRST-NON-NULL — the
    unit test used `TradeLogger.log()` for the pre-existing row, which
    left the column NULL and quietly modelled a state production never
    reaches.

    This drives the real writers in the real order against one database.
    """

    ENTRY_TS = "2026-07-28T17:07:35.812362+00:00"
    RPS = 16.1921886113901
    STOP = 320.73781138861
    ENTRY_FILL = 339.543846
    STOP_FILL = 305.02
    QTY = 13.0

    def _setup(self, tmp_path):
        from engine.lifecycle import PositionLifecycleStore, new_position_uid
        from engine.lifecycle_orders import (
            OrderEvent,
            PositionLifecycleOrdersStore,
            apply_order_event,
        )
        from reporting.logger import TradeRecord

        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        conn = tl._ensure_db()
        pos_store = PositionLifecycleStore(conn)
        orders_store = PositionLifecycleOrdersStore(conn)

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol="AAPL", owner_key="AAPL",
            strategy="donchian_breakout", position_type="single_leg",
            entry_qty=self.QTY,
        )

        # Full entry through the substrate: the order row must exist or
        # the position-status rollup nets to a negative qty on the later
        # sell and flips the position to 'error'.
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry", order_type="stop_limit",
            order_class="oto", time_in_force="day", side="buy",
            intended_qty=self.QTY, intended_stop_price=self.STOP,
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry", order_id="broker-entry",
        )
        apply_order_event(
            conn,
            OrderEvent(
                order_id="broker-entry", status="filled",
                filled_qty=self.QTY, avg_fill_price=self.ENTRY_FILL,
                broker_updated_at=self.ENTRY_TS,
            ),
        )
        # `_log_entry` then upserts the risk basis onto that same row.
        tl.log(
            TradeRecord(
                timestamp=self.ENTRY_TS, symbol="AAPL", side="buy",
                qty=self.QTY, avg_fill_price=self.ENTRY_FILL,
                order_id="broker-entry", strategy="donchian_breakout",
                reason="donchian_breakout entry @ 2026-07-27T04:00:00+00:00",
                stop_price=self.STOP, entry_reference_price=336.93,
                modeled_slippage_bps=None, realized_slippage_bps=None,
                order_type="stop_limit", status="filled",
                requested_qty=self.QTY, filled_qty=self.QTY,
                initial_stop_loss=self.STOP, initial_risk_per_share=self.RPS,
                entry_timestamp=self.ENTRY_TS, position_uid=uid,
                # Required: the trades upsert conflict target is a
                # PARTIAL unique index predicated on
                # `position_type = 'single_leg'`. Omitting it makes the
                # incoming row fail the predicate, so no conflict is
                # detected and `_log_entry` inserts a DUPLICATE row for
                # an order the substrate already wrote.
                position_type="single_leg",
            )
        )

        orders_store.insert_pending(
            position_uid=uid, role="protective_stop",
            client_order_id="cli-stop", order_type="stop",
            order_class="simple", time_in_force="gtc", side="sell",
            intended_qty=self.QTY, intended_stop_price=self.STOP,
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-stop", order_id="broker-stop",
        )
        return tl, conn, pos_store, orders_store, uid, OrderEvent, apply_order_event

    def _engine(self, tl, pos_store, orders_store):
        strategy = FakeStrategy(entries=[False], exits=[False])
        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.get_open_orders.return_value = []
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)
        risk = RiskManager(
            max_position_pct=0.02, max_open_positions=5,
            max_gross_exposure_pct=0.50, atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0,
            loss_streak_threshold=10, broker_error_threshold=10,
        )
        engine = TradingEngine(
            strategy=strategy, symbols=["AAPL"], risk=risk, broker=broker,
            trade_logger=tl,
            config=EngineConfig(
                history_lookback_days=120, cycle_interval_seconds=0.01,
                max_bar_age_multiplier=10.0, market_hours_only=False,
            ),
        )
        engine.lifecycle_store = pos_store
        engine.lifecycle_orders_store = orders_store
        return engine

    def test_stop_fill_records_full_accounting_through_the_real_seam(
        self, tmp_path,
    ):
        tl, conn, pos_store, orders_store, uid, OrderEvent, apply_order_event = (
            self._setup(tmp_path)
        )
        engine = self._engine(tl, pos_store, orders_store)
        engine._positions["AAPL"] = SimpleNamespace(
            symbol="AAPL", strategy_name="donchian_breakout", qty=self.QTY,
        )
        engine._entry_prices["AAPL"] = self.ENTRY_FILL

        event = OrderEvent(
            order_id="broker-stop", status="filled", filled_qty=self.QTY,
            avg_fill_price=self.STOP_FILL,
            broker_updated_at="2026-07-31T13:32:27+00:00",
        )
        # 1. Substrate persists the sell row FIRST — the real ordering.
        assert apply_order_event(conn, event).applied is True
        # 2. Engine then dispatches the accounting.
        engine._maybe_dispatch_substrate_stop_fill(
            event=event, snapshot=_snapshot(positions={}, open_orders=[]),
        )

        all_rows = tl.read_all()
        rows = [r for r in all_rows
                if r["side"] == "sell" and r["order_id"] == "broker-stop"]
        assert len(rows) == 1, "the seam must produce exactly one sell row"
        row = rows[0]
        entry_row = [r for r in all_rows if r["order_id"] == "broker-entry"][0]

        # Entry timestamp is the ENTRY's, not this fill's. Compared
        # against the entry row rather than a constant, because the
        # dashboard joins the two on exact equality — that is the
        # property worth pinning.
        assert row["entry_timestamp"] == entry_row["entry_timestamp"]
        assert row["entry_timestamp"] != row["timestamp"]
        # Risk basis recovered by position_uid despite the write ordering.
        assert row["initial_risk_per_share"] == pytest.approx(self.RPS)
        assert row["initial_stop_loss"] == pytest.approx(self.STOP)
        expected_risk = self.RPS * self.QTY
        assert row["initial_risk_dollars"] == pytest.approx(expected_risk)
        # P&L and R against the broker entry basis.
        expected_pnl = (self.STOP_FILL - self.ENTRY_FILL) * self.QTY
        assert row["realized_pnl"] == pytest.approx(expected_pnl)
        assert row["r_multiple"] == pytest.approx(
            expected_pnl / expected_risk, rel=1e-6
        )
        assert row["r_multiple"] == pytest.approx(-2.13, abs=0.01)
        # Stop-gap erosion is tagged as such, and stays out of the
        # execution-quality family (PR #84).
        assert row["slippage_benchmark_kind"] == "active_stop_price"

    def test_option_stop_fill_applies_the_100x_contract_multiplier(
        self, tmp_path,
    ):
        """Ported from the removed legacy suite (2026-08-14).

        `_process_stream_stop_fills` had
        `test_stream_stop_fill_applies_100x_multiplier_for_options`. When
        that path was deleted the substrate handler had OCC coverage only
        in an AUDIT test — nothing asserted that an option stop fill books
        P&L at 100x. Losing that would understate every option stop-out by
        two orders of magnitude, silently.

        Asserts the recorded P&L rather than intercepting the call: the
        multiplier is only worth testing through what it produces.
        """
        from engine.lifecycle import new_position_uid
        from execution.broker import Position

        tl, conn, pos_store, orders_store, uid, OrderEvent, apply_order_event = (
            self._setup(tmp_path)
        )
        engine = self._engine(tl, pos_store, orders_store)

        # A purpose-built OCC position. Mutating the AAPL fixture's symbol
        # instead trips the 11.51 identity guard — that order row is already
        # stamped position_id='AAPL' — which is the guard working, not a bug.
        occ, entry_px, stop_px, qty = "SPY260821C00738000", 20.0, 18.72, 2.0
        occ_uid = new_position_uid()
        pos_store.create_pending(
            position_uid=occ_uid, symbol=occ, owner_key="SPY",
            strategy="generic_single_leg_options", position_type="single_leg",
            entry_qty=qty,
        )
        orders_store.insert_pending(
            position_uid=occ_uid, role="protective_stop",
            client_order_id="cid-occ-stop", order_type="stop",
            order_class="simple", time_in_force="gtc", side="sell",
            intended_qty=qty,
        )
        orders_store.attach_broker_order_id(
            client_order_id="cid-occ-stop", order_id="occ-broker-stop",
        )
        # Real Position, not SimpleNamespace — the realized-P&L path
        # calls dataclasses.asdict() on it.
        # NOT spy_options_reversion: OPTION_STOP_REPLACE_AUDIT_STRATEGY is
        # scoped to it, and the audit path needs fixtures unrelated to the
        # multiplier this test is about.
        engine._positions["SPY"] = Position(
            symbol=occ, qty=qty, avg_entry_price=entry_px,
            market_value=entry_px * qty * 100,
        )
        engine._entry_prices["SPY"] = entry_px

        captured: dict = {}
        real_record = engine._record_realized_pnl

        def _capture(owner_key, owner, price, qty_, **kw):
            captured.update(kw)
            captured["owner_key"] = owner_key
            captured["qty"] = qty_

        engine._record_realized_pnl = _capture

        event = OrderEvent(
            order_id="occ-broker-stop", status="filled", filled_qty=qty,
            avg_fill_price=stop_px,
            broker_updated_at="2026-07-31T13:32:27+00:00",
        )
        assert apply_order_event(conn, event).applied is True
        engine._maybe_dispatch_substrate_stop_fill(
            event=event, snapshot=_snapshot(positions={}, open_orders=[]),
        )

        row = next(r for r in tl.read_all() if r["order_id"] == "occ-broker-stop")
        assert row["symbol"] == occ, "the trade row keeps the OCC symbol"
        # The multiplier is applied in _record_realized_pnl, which is
        # captured above. Asserting the recorded P&L on the row instead
        # would need a full OCC entry fixture for log_stop_fill to derive
        # a basis from — more fixture than the property is worth.
        assert captured.get("multiplier") == 100, (
            f"option stop fill booked at multiplier "
            f"{captured.get('multiplier')} — option P&L understated 100x"
        )
        assert captured.get("owner_key") == "SPY", (
            "the OCC symbol must normalise to the underlying for ownership"
        )
        assert captured.get("qty") == pytest.approx(qty)

    def test_entry_and_exit_rows_join_on_entry_timestamp(self, tmp_path):
        """The downstream property that actually matters: the dashboard
        pairs entries to exits on exact equality of `entry_timestamp`."""
        tl, conn, pos_store, orders_store, uid, OrderEvent, apply_order_event = (
            self._setup(tmp_path)
        )
        engine = self._engine(tl, pos_store, orders_store)
        engine._positions["AAPL"] = SimpleNamespace(
            symbol="AAPL", strategy_name="donchian_breakout", qty=self.QTY,
        )
        event = OrderEvent(
            order_id="broker-stop", status="filled", filled_qty=self.QTY,
            avg_fill_price=self.STOP_FILL,
            broker_updated_at="2026-07-31T13:32:27+00:00",
        )
        apply_order_event(conn, event)
        engine._maybe_dispatch_substrate_stop_fill(
            event=event, snapshot=_snapshot(positions={}, open_orders=[]),
        )
        rows = tl.read_all()
        entry = [r for r in rows if r["side"] == "buy"][0]
        exit_row = [r for r in rows if r["side"] == "sell"][0]
        assert entry["entry_timestamp"] == exit_row["entry_timestamp"]


class TestPostFillStopReAnchor:
    """PLAN 11.54 — the DAY->GTC rebuild must price the replacement stop
    off the actual fill, not off the bracket child's reference-anchored
    price.

    Donchian's protective stop is an OTO bracket child priced at
    `reference_close - 2xATR` when the entry is SUBMITTED, but the entry is
    a resting STOP_LIMIT that fills later at its own price. Across seven
    live entries the resulting room ranged 68.4%-116.1% of the intended
    2xATR. The rebuild already cancels and re-places the order (Alpaca
    cannot modify OTO children), so re-deriving the price costs nothing.
    """

    def _stop_limit_decision(self, *, ref, stop, trigger, limit):
        return RiskDecision(
            symbol="AAPL", side=Side.BUY, qty=10.0,
            entry_reference_price=ref, stop_price=stop,
            strategy_name="donchian_breakout", reason="test",
            order_type=OrderType.STOP_LIMIT,
            entry_trigger_price=trigger, limit_price=limit,
        )

    def _day_stop_setup(self, engine_factory, stop_price):
        day_stop = replace(
            _open_stop_order("AAPL", stop_price),
            order_id="day-stop", qty=10, time_in_force="day",
        )
        rebuilt = replace(
            day_stop, order_id="gtc-stop", status="accepted", time_in_force="gtc",
        )
        snapshot = _snapshot(
            positions={"AAPL": Position("AAPL", 10, 100.0, 1010.0)},
            open_orders=[day_stop],
        )
        engine, broker = engine_factory(snapshot=snapshot)
        engine._register_single_leg(strategy_name="donchian_breakout", symbol="AAPL")
        broker.replace_day_stop_with_standalone_gtc.return_value = rebuilt
        return engine, broker, snapshot

    def test_rebuild_uses_the_fill_anchored_price(self, engine_factory):
        """AVGO 2026-08-07 shape: ref 420.68, stop 386.95 (2xATR 33.73),
        filled 426.09. The old behaviour re-placed at 386.95 (116% room);
        re-anchored it is 392.36 and room is exactly 100%."""
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 386.95)
        engine._last_atr["AAPL"] = 16.865            # 2x = 33.73, AVGO's real ATR
        decision = self._stop_limit_decision(
            ref=420.68, stop=386.95, trigger=418.49, limit=439.41,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=decision,
            fill_price=426.09,
        )
        kwargs = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs
        assert kwargs["stop_price"] == pytest.approx(392.36, abs=0.01)

    def test_fill_below_reference_moves_the_stop_down(self, engine_factory):
        """AMZN 2026-08-04 shape — the direction that leaves the stop too
        close: 68.3% of intended room before re-anchoring."""
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 264.45)
        engine._last_atr["AAPL"] = 9.84              # 2x = 19.68, AMZN's real ATR
        decision = self._stop_limit_decision(
            ref=284.12, stop=264.45, trigger=271.46, limit=285.04,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=decision,
            fill_price=277.90,
        )
        kwargs = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs
        assert kwargs["stop_price"] == pytest.approx(258.22, abs=0.01)

    def test_missing_fill_price_keeps_the_original_stop(self, engine_factory):
        """A stop that cannot be placed is worse than one placed slightly
        off — the rebuild must still happen, at the child's own price."""
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 386.95)
        decision = self._stop_limit_decision(
            ref=420.68, stop=386.95, trigger=418.49, limit=439.41,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=decision,
            fill_price=None,
        )
        kwargs = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs
        assert kwargs["stop_price"] == pytest.approx(386.95)

    def test_an_existing_gtc_stop_is_never_rebuilt(self, engine_factory):
        """THE GUARANTEE THAT PROTECTS ALREADY-OPEN POSITIONS.

        The rebuild branch fires only when the live stop is DAY. Once a
        position has its standalone GTC stop it can never be re-priced by
        this path, so shipping the re-anchor cannot move the stop on a
        position that is already open — it applies to new fills only.
        """
        gtc_stop = replace(
            _open_stop_order("AAPL", 386.95),
            order_id="gtc-stop", qty=10, time_in_force="gtc",
        )
        snapshot = _snapshot(
            positions={"AAPL": Position("AAPL", 10, 100.0, 1010.0)},
            open_orders=[gtc_stop],
        )
        engine, broker = engine_factory(snapshot=snapshot)
        engine._register_single_leg(strategy_name="donchian_breakout", symbol="AAPL")
        decision = self._stop_limit_decision(
            ref=420.68, stop=386.95, trigger=418.49, limit=439.41,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=decision,
            fill_price=426.09,
        )
        broker.replace_day_stop_with_standalone_gtc.assert_not_called()
        broker.place_protective_stop.assert_not_called()
        assert snapshot.open_orders == [gtc_stop]

    def test_repair_path_does_not_re_anchor(self, engine_factory):
        """`_repair_missing_protective_stops` runs every cycle over ALREADY
        OPEN positions. It must keep using the recorded stop price — if it
        re-anchored, shipping this change would silently move live stops on
        open positions, which is exactly what was ruled out."""
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 95.0)
        engine._repair_missing_protective_stops(snapshot)
        kwargs = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs
        assert kwargs["stop_price"] == 95.0

    def test_substrate_reconstructed_decision_still_re_anchors(self, engine_factory):
        """The production shape, which an earlier attempt could not handle.

        The substrate rebuilds the RiskDecision with
        `entry_reference_price = avg_fill_price` (engine/trader.py:2519)
        because the signal-bar close is not persisted. Deriving the offset
        from the decision therefore collapses to `fill - stop` and
        re-anchoring becomes a no-op. Taking the offset from the cached ATR
        instead sidesteps that entirely -- no schema change, no re-fetch.
        """
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 386.95)
        engine._last_atr["AAPL"] = 16.865            # AVGO's real ATR; 2x = 33.73
        reconstructed = RiskDecision(
            symbol="AAPL", side=Side.BUY, qty=10.0,
            entry_reference_price=426.09,            # == the fill, as production builds it
            stop_price=386.95,
            strategy_name="donchian_breakout", reason="substrate dispatch",
            order_type=OrderType.STOP_LIMIT,
            entry_trigger_price=418.49, limit_price=439.41,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=reconstructed,
            fill_price=426.09,
        )
        kwargs = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs
        assert kwargs["stop_price"] == pytest.approx(392.36, abs=0.01)

    def test_no_cached_atr_leaves_the_stop_untouched(self, engine_factory):
        """A stop that cannot be re-derived stays where it is. Never a gap."""
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 386.95)
        engine._last_atr.pop("AAPL", None)
        decision = self._stop_limit_decision(
            ref=420.68, stop=386.95, trigger=418.49, limit=439.41,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=decision,
            fill_price=426.09,
        )
        kwargs = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs
        assert kwargs["stop_price"] == pytest.approx(386.95)

    def test_trade_log_is_rebased_to_the_stop_actually_placed(self, engine_factory):
        """Seam test. The broker stop and the recorded risk basis must not
        disagree — r_multiple reads the log, and so does the stop-repair
        path, which would otherwise restore the pre-anchor level."""
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 386.95)
        engine._last_atr["AAPL"] = 16.865
        engine.trade_logger.rebase_entry_stop = MagicMock(return_value=True)
        decision = self._stop_limit_decision(
            ref=420.68, stop=386.95, trigger=418.49, limit=439.41,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=decision,
            fill_price=426.09,
            entry_order_id="entry-oid",
        )
        placed = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs["stop_price"]
        engine.trade_logger.rebase_entry_stop.assert_called_once()
        logged = engine.trade_logger.rebase_entry_stop.call_args.kwargs
        assert logged["order_id"] == "entry-oid"
        assert logged["new_stop_price"] == pytest.approx(placed)

    def test_no_rebase_when_the_stop_did_not_move(self, engine_factory):
        """A rebuild that re-places at the same level has nothing to
        correct — don't write to the trade log for it."""
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 392.36)
        engine._last_atr["AAPL"] = 16.865        # fill 426.09 - 33.73 = 392.36
        engine.trade_logger.rebase_entry_stop = MagicMock(return_value=True)
        decision = self._stop_limit_decision(
            ref=420.68, stop=392.36, trigger=418.49, limit=439.41,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=decision,
            fill_price=426.09,
            entry_order_id="entry-oid",
        )
        engine.trade_logger.rebase_entry_stop.assert_not_called()

    def test_a_failed_rebase_never_costs_protection(self, engine_factory):
        """Reporting accuracy is subordinate to the stop being placed. If
        the log write raises, the broker stop still stands."""
        engine, broker, snapshot = self._day_stop_setup(engine_factory, 386.95)
        engine._last_atr["AAPL"] = 16.865
        engine.trade_logger.rebase_entry_stop = MagicMock(
            side_effect=RuntimeError("db locked")
        )
        decision = self._stop_limit_decision(
            ref=420.68, stop=386.95, trigger=418.49, limit=439.41,
        )
        engine._ensure_recovered_protective_stop(
            snapshot=snapshot,
            position=snapshot.account.open_positions["AAPL"],
            decision=decision,
            fill_price=426.09,
            entry_order_id="entry-oid",
        )
        kwargs = broker.replace_day_stop_with_standalone_gtc.call_args.kwargs
        assert kwargs["stop_price"] == pytest.approx(392.36, abs=0.01)

    def test_atr_is_cached_where_the_close_already_was(self, engine_factory):
        """The value was computed every cycle and thrown away; the fix is
        to keep it. Guard against it being dropped again."""
        engine, _ = engine_factory()
        assert hasattr(engine, "_last_atr")
        assert isinstance(engine._last_atr, dict)


# ── TestIntradayEquityDrawdown ──────────────────────────────────────────────


class TestIntradayEquityDrawdown:
    """
    `max_intraday_drawdown` on the daily P&L report was $0.00 in all 71
    reports written before 2026-08-19: it read an in-memory accumulator
    fed only by `PnLTracker.record_trade_pnl`, which no production code
    path ever called. The engine now derives it from the account-equity
    path it already samples once per cycle.
    """

    def test_starts_flat_and_tracks_peak_to_trough(self, engine_factory):
        engine, _ = engine_factory()
        assert engine.max_intraday_drawdown == 0.0

        engine._observe_equity(100_000.0)
        engine._observe_equity(101_500.0)   # new peak
        engine._observe_equity(99_800.0)    # -1,700 from peak
        assert engine.max_intraday_drawdown == pytest.approx(1_700.0)

        engine._observe_equity(100_900.0)   # recovery does not shrink the max
        assert engine.max_intraday_drawdown == pytest.approx(1_700.0)

        engine._observe_equity(99_000.0)    # deeper trough from the same peak
        assert engine.max_intraday_drawdown == pytest.approx(2_500.0)

    def test_a_rising_equity_path_has_no_drawdown(self, engine_factory):
        engine, _ = engine_factory()
        for equity in (100_000.0, 100_400.0, 101_000.0):
            engine._observe_equity(equity)
        assert engine.max_intraday_drawdown == 0.0

    def test_peak_resets_on_utc_date_rollover(self, engine_factory):
        """The field is per-day but this process runs for a week at a
        time. Without the reset, Monday's peak would keep producing
        drawdown against Tuesday's equity forever."""
        engine, _ = engine_factory()
        now = datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc)
        engine._clock = lambda: now

        engine._observe_equity(105_000.0)
        engine._observe_equity(100_000.0)
        assert engine.max_intraday_drawdown == pytest.approx(5_000.0)

        now = datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)
        engine._clock = lambda: now
        engine._observe_equity(100_000.0)
        assert engine.max_intraday_drawdown == 0.0, (
            "yesterday's peak must not carry into today's intraday drawdown"
        )

        engine._observe_equity(99_250.0)
        assert engine.max_intraday_drawdown == pytest.approx(750.0)

    def test_cycle_snapshots_feed_the_drawdown(self, engine_factory):
        """Binds the wiring, not just the method: a normal open-market
        cycle must push its snapshot equity through `_observe_equity`."""
        engine, broker = engine_factory(snapshot=_snapshot(equity=100_000.0))
        broker.sync_with_broker.return_value = _snapshot(equity=100_000.0)
        engine._run_one_cycle()

        broker.sync_with_broker.return_value = _snapshot(equity=97_600.0)
        engine._run_one_cycle()

        assert engine._last_cycle_equity == pytest.approx(97_600.0)
        assert engine.max_intraday_drawdown == pytest.approx(2_400.0)

    def test_market_closed_cycles_also_feed_the_drawdown(self, engine_factory):
        """The bot holds overnight, so an extended-hours equity slide is
        a real drawdown — market-closed cycles take a snapshot too."""
        engine, broker = engine_factory(market_open=False)
        engine.config = replace(engine.config, market_hours_only=True)

        broker.sync_with_broker.return_value = _snapshot(equity=100_000.0)
        engine._run_one_cycle()
        broker.sync_with_broker.return_value = _snapshot(equity=98_900.0)
        engine._run_one_cycle()

        assert engine.max_intraday_drawdown == pytest.approx(1_100.0)


# ── TestIntradayDrawdownSurvivesRestart ─────────────────────────────────────


class TestIntradayDrawdownSurvivesRestart:
    """
    The peak and max were process-memory only. `recycle_bot.sh` writes
    the day's report at shutdown, the replacement engine starts with a
    fresh peak, and *its* shutdown overwrites that same `{day}.md` with
    only the post-restart decline — so a morning drawdown vanished from
    the report that claims to cover the day.
    """

    def _state_file(self) -> str:
        from config import settings

        return settings.EQUITY_PATH_STATE_PATH

    def test_peak_and_max_are_persisted(self, engine_factory):
        engine, _ = engine_factory()
        now = datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)
        engine._clock = lambda: now

        engine._observe_equity(100_000.0)
        engine._observe_equity(97_500.0)

        with open(self._state_file()) as fh:
            state = json.load(fh)
        assert state["day"] == "2026-08-19"
        assert state["equity_peak"] == pytest.approx(100_000.0)
        assert state["max_intraday_drawdown"] == pytest.approx(2_500.0)

    def test_same_day_restart_resumes_the_days_drawdown(self, engine_factory):
        """The reviewer's scenario end to end: session 1 takes a
        drawdown, recycle, session 2 must not report only its own."""
        now = datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)

        first, _ = engine_factory()
        first._clock = lambda: now
        first._observe_equity(100_000.0)
        first._observe_equity(96_000.0)      # -4,000 in the morning
        assert first.max_intraday_drawdown == pytest.approx(4_000.0)

        # Recycle: a brand-new engine object, same UTC day.
        second, _ = engine_factory()
        second._clock = lambda: now
        assert second.max_intraday_drawdown == 0.0, "fresh object starts clean"

        second._restore_equity_path_state()
        assert second.max_intraday_drawdown == pytest.approx(4_000.0)
        assert second._session_equity_peak == pytest.approx(100_000.0)

        # A shallower afternoon dip must not shrink the day's figure.
        second._observe_equity(99_000.0)
        assert second.max_intraday_drawdown == pytest.approx(4_000.0)

        # A deeper one extends it, measured from the restored peak.
        second._observe_equity(94_500.0)
        assert second.max_intraday_drawdown == pytest.approx(5_500.0)

    def test_restore_runs_before_the_startup_snapshot_is_observed(
        self, engine_factory,
    ):
        """Ordering matters: if the startup snapshot were observed
        first it would become a new peak and the restored one would
        never apply. Drive the real `start()`."""
        now = datetime(2026, 8, 19, 15, 0, tzinfo=timezone.utc)

        first, _ = engine_factory()
        first._clock = lambda: now
        first._observe_equity(100_000.0)

        second, broker = engine_factory(snapshot=_snapshot(equity=93_000.0))
        second._clock = lambda: now
        broker.sync_with_broker.return_value = _snapshot(equity=93_000.0)
        # max_cycles=0 still runs the full startup path (snapshot,
        # restore, seed) and then exits the loop immediately.
        second.start(max_cycles=0)

        # 100,000 restored peak vs a 93,000 startup snapshot.
        assert second.max_intraday_drawdown == pytest.approx(7_000.0)

    def test_yesterdays_state_is_ignored(self, engine_factory):
        first, _ = engine_factory()
        first._clock = lambda: datetime(2026, 8, 18, 20, 0, tzinfo=timezone.utc)
        first._observe_equity(100_000.0)
        first._observe_equity(90_000.0)

        second, _ = engine_factory()
        second._clock = lambda: datetime(2026, 8, 19, 14, 0, tzinfo=timezone.utc)
        second._restore_equity_path_state()

        assert second.max_intraday_drawdown == 0.0
        assert second._session_equity_peak is None

    def test_malformed_state_does_not_block_startup(self, engine_factory):
        engine, _ = engine_factory()
        with open(self._state_file(), "w") as fh:
            fh.write("{not json at all")

        engine._restore_equity_path_state()   # must not raise

        assert engine.max_intraday_drawdown == 0.0
        engine._observe_equity(100_000.0)
        engine._observe_equity(99_000.0)
        assert engine.max_intraday_drawdown == pytest.approx(1_000.0)

    def test_missing_state_file_is_a_normal_first_start(self, engine_factory):
        engine, _ = engine_factory()
        path = self._state_file()
        if os.path.exists(path):
            os.remove(path)

        engine._restore_equity_path_state()

        assert engine.max_intraday_drawdown == 0.0
        assert engine._session_equity_peak is None


# ── TestBrokerEquityPathReconcile ───────────────────────────────────────────


class TestBrokerEquityPathReconcile:
    """
    Per-cycle sampling cannot see equity that moved while no process was
    running — a peak or trough reached entirely between a
    `recycle_bot.sh` stop and the replacement's first cycle was
    invisible. The broker's own 1-minute series covers the whole UTC day
    regardless, and is folded in at shutdown.
    """

    DAY = "2026-08-19"

    def _at(self, hour: int = 18):
        return datetime(2026, 8, 19, hour, 0, tzinfo=timezone.utc)

    def test_broker_series_raises_a_shallower_local_figure(self, engine_factory):
        engine, broker = engine_factory()
        engine._clock = lambda: self._at()
        engine._observe_equity(100_000.0)
        engine._observe_equity(99_800.0)          # only $200 seen locally
        broker.get_intraday_equity_path.return_value = [
            100_000.0, 94_500.0, 99_000.0,        # $5,500 while we were down
        ]

        assert engine.reconcile_intraday_drawdown_from_broker(self.DAY) == pytest.approx(5_500.0)
        assert engine.max_intraday_drawdown == pytest.approx(5_500.0)

    def test_a_deeper_locally_observed_figure_is_never_lowered(self, engine_factory):
        """The broker series is 1-minute; a trough between its marks is
        real and this process saw it. Combining with `max` means a
        directly observed drawdown is never reported as smaller."""
        engine, broker = engine_factory()
        engine._clock = lambda: self._at()
        engine._observe_equity(100_000.0)
        engine._observe_equity(91_000.0)          # $9,000 observed directly
        broker.get_intraday_equity_path.return_value = [100_000.0, 97_000.0]

        assert engine.reconcile_intraday_drawdown_from_broker(self.DAY) == pytest.approx(9_000.0)

    def test_requests_the_utc_day_clamped_to_now(self, engine_factory):
        engine, broker = engine_factory()
        engine._clock = lambda: self._at(hour=18)
        broker.get_intraday_equity_path.return_value = []

        engine.reconcile_intraday_drawdown_from_broker(self.DAY)

        start, end = broker.get_intraday_equity_path.call_args.args
        assert start == datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
        assert end == self._at(hour=18), "must not request into the future"

    def test_a_completed_past_day_spans_the_full_24h(self, engine_factory):
        engine, broker = engine_factory()
        engine._clock = lambda: datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
        broker.get_intraday_equity_path.return_value = []

        engine.reconcile_intraday_drawdown_from_broker(self.DAY)

        start, end = broker.get_intraday_equity_path.call_args.args
        assert start == datetime(2026, 8, 19, 0, 0, tzinfo=timezone.utc)
        assert end == datetime(2026, 8, 20, 0, 0, tzinfo=timezone.utc)

    def test_empty_series_keeps_the_local_figure(self, engine_factory):
        engine, broker = engine_factory()
        engine._clock = lambda: self._at()
        engine._observe_equity(100_000.0)
        engine._observe_equity(98_500.0)
        broker.get_intraday_equity_path.return_value = []

        assert engine.reconcile_intraday_drawdown_from_broker(self.DAY) == pytest.approx(1_500.0)

    def test_broker_failure_keeps_the_local_figure(self, engine_factory):
        """Runs inside the shutdown handler that writes the report — it
        must degrade to the locally observed number, not explode."""
        engine, broker = engine_factory()
        engine._clock = lambda: self._at()
        engine._observe_equity(100_000.0)
        engine._observe_equity(97_750.0)
        broker.get_intraday_equity_path.side_effect = RuntimeError("api down")

        assert engine.reconcile_intraday_drawdown_from_broker(self.DAY) == pytest.approx(2_250.0)
        assert engine.max_intraday_drawdown == pytest.approx(2_250.0)

    def test_reconciled_figure_is_persisted(self, engine_factory):
        from config import settings

        engine, broker = engine_factory()
        engine._clock = lambda: self._at()
        engine._observe_equity(100_000.0)
        broker.get_intraday_equity_path.return_value = [100_000.0, 93_000.0]

        engine.reconcile_intraday_drawdown_from_broker(self.DAY)

        with open(settings.EQUITY_PATH_STATE_PATH) as fh:
            state = json.load(fh)
        assert state["day"] == self.DAY
        assert state["max_intraday_drawdown"] == pytest.approx(7_000.0)

    def test_a_malformed_day_keeps_the_local_figure(self, engine_factory):
        engine, broker = engine_factory()
        engine._clock = lambda: self._at()
        engine._observe_equity(100_000.0)
        engine._observe_equity(99_000.0)

        assert engine.reconcile_intraday_drawdown_from_broker("not-a-date") == pytest.approx(1_000.0)
        broker.get_intraday_equity_path.assert_not_called()


# ── TestHeatCapObservation ──────────────────────────────────────────────────


class TestHeatCapObservation:
    """
    PLAN 11.60, shipped observation-only: the cap computes and logs but
    refuses nothing until `STRATEGY_HEAT_CAP_ENFORCED` is flipped.
    """

    def _decision(self, qty=10, stop=90.0, strategy="donchian_breakout"):
        from risk.manager import RiskDecision, Side

        return RiskDecision(
            symbol="AAPL", side=Side.BUY, qty=qty,
            entry_reference_price=100.0, stop_price=stop,
            strategy_name=strategy, reason="test",
            order_type=OrderType.STOP_LIMIT,
            entry_trigger_price=100.0, limit_price=110.0,
        )
        # candidate risk = (limit 110 - stop 90) x qty 10 = $200

    def _wire(self, engine, *, filled=0.0, pending=0.0, equity=100_000.0):
        engine._last_cycle_equity = equity
        engine.trade_logger.read_open_risk_by_strategy_with_gaps = (
            lambda: ({"donchian_breakout": filled}, {})
        )
        engine.lifecycle_orders_store = MagicMock()
        engine.lifecycle_orders_store.read_pending_entry_reservations = (
            lambda: {"donchian_breakout": pending}
        )

    def test_strategy_without_a_cap_is_unconstrained(self, engine_factory, monkeypatch):
        engine, _ = engine_factory()
        self._wire(engine, filled=99_999.0)
        assert engine._heat_cap_allows(self._decision(strategy="sma_crossover")) is True

    def test_under_the_cap_allows(self, engine_factory):
        engine, _ = engine_factory()
        # cap = 1.6% of 100k = 1600; filled 500 + candidate 100 = 600
        self._wire(engine, filled=500.0)
        assert engine._heat_cap_allows(self._decision()) is True

    def test_over_the_cap_still_allows_in_observation_mode(self, engine_factory):
        """The whole point of shipping this way: it must not block."""
        engine, _ = engine_factory()
        self._wire(engine, filled=1_590.0)   # + candidate 100 -> 1690 > 1600
        assert engine._heat_cap_allows(self._decision()) is True

    def test_over_the_cap_logs_a_structured_record(self, engine_factory):
        engine, _ = engine_factory()
        self._wire(engine, filled=1_000.0, pending=550.0)   # +100 -> 1650 > 1600
        messages: list[str] = []
        sink = logger.add(messages.append, level="WARNING")
        try:
            engine._heat_cap_allows(self._decision())
        finally:
            logger.remove(sink)
        logged = "".join(messages)
        assert "OBSERVED" in logged
        for field in ("filled_heat", "pending_reserved", "candidate",
                      "projected", "cap=", "entry_source"):
            assert field in logged, f"{field} missing from the refusal record"

    def test_pending_reservations_count_toward_heat(self, engine_factory):
        """A resting STOP_LIMIT burst is the case the cap exists for. If
        reservations were ignored, this would sit under the cap."""
        engine, _ = engine_factory()
        self._wire(engine, filled=0.0, pending=1_550.0)   # +100 -> 1650 > 1600
        messages: list[str] = []
        sink = logger.add(messages.append, level="WARNING")
        try:
            engine._heat_cap_allows(self._decision())
        finally:
            logger.remove(sink)
        assert "OBSERVED" in "".join(messages)

    def test_enforced_mode_refuses(self, engine_factory, monkeypatch):
        from config import settings

        engine, _ = engine_factory()
        monkeypatch.setattr(settings, "STRATEGY_HEAT_CAP_ENFORCED", True)
        self._wire(engine, filled=1_590.0)
        assert engine._heat_cap_allows(self._decision()) is False

    def test_unreadable_heat_does_not_read_as_zero(self, engine_factory, monkeypatch):
        """Degraded data must not relax the cap. Observation still allows,
        but enforcement refuses rather than treating unknown as no risk."""
        from config import settings

        engine, _ = engine_factory()
        self._wire(engine)
        def boom():
            raise sqlite3.Error("db down")
        engine.trade_logger.read_open_risk_by_strategy_with_gaps = boom

        assert engine._heat_cap_allows(self._decision()) is True    # observation
        monkeypatch.setattr(settings, "STRATEGY_HEAT_CAP_ENFORCED", True)
        assert engine._heat_cap_allows(self._decision()) is False   # enforced

    def test_a_real_database_failure_refuses_under_enforcement(
        self, engine_factory, monkeypatch,
    ):
        """The test above patches the reader itself, so it cannot catch a
        failure the reader *swallows*. This one breaks the database
        underneath a real `TradeLogger` and drives the real reader.

        The bug it guards: the shared replay helper absorbs a failed
        `_ensure_db()` into an empty result, so an unavailable trade DB
        would present as zero open heat and admit the entry.
        """
        from config import settings
        from reporting.logger import TradeLogger

        engine, _ = engine_factory()
        engine._last_cycle_equity = 100_000.0
        engine.lifecycle_orders_store = MagicMock()
        engine.lifecycle_orders_store.read_pending_entry_reservations = lambda: {}

        real_logger = TradeLogger(path=str(settings.TRADE_LOG_DB))
        def _boom():
            raise sqlite3.OperationalError("unable to open database file")
        real_logger._ensure_db = _boom
        engine.trade_logger = real_logger

        assert engine._heat_cap_allows(self._decision()) is True     # observation
        monkeypatch.setattr(settings, "STRATEGY_HEAT_CAP_ENFORCED", True)
        assert engine._heat_cap_allows(self._decision()) is False    # enforced

    def test_unbounded_positions_refuse_under_enforcement(
        self, engine_factory, monkeypatch,
    ):
        """Unknown risk must not become zero risk. A position with no
        recorded initial risk makes the total a knowing understatement, so
        enforcement must refuse rather than admit against it. Observation
        continues, since its whole contract is not to block."""
        from config import settings

        engine, _ = engine_factory()
        self._wire(engine, filled=100.0)   # far under the 1600 cap
        engine.trade_logger.read_open_risk_by_strategy_with_gaps = (
            lambda: ({"donchian_breakout": 100.0}, {"donchian_breakout": ["WYFI"]})
        )

        assert engine._heat_cap_allows(self._decision()) is True     # observation
        monkeypatch.setattr(settings, "STRATEGY_HEAT_CAP_ENFORCED", True)
        assert engine._heat_cap_allows(self._decision()) is False    # enforced

    def test_unbounded_positions_are_flagged_not_silently_dropped(self, engine_factory):
        engine, _ = engine_factory()
        self._wire(engine, filled=100.0)
        engine.trade_logger.read_open_risk_by_strategy_with_gaps = (
            lambda: ({"donchian_breakout": 100.0}, {"donchian_breakout": ["WYFI"]})
        )
        messages: list[str] = []
        sink = logger.add(messages.append, level="WARNING")
        try:
            engine._heat_cap_allows(self._decision())
        finally:
            logger.remove(sink)
        logged = "".join(messages)
        assert "WYFI" in logged and "understated" in logged
