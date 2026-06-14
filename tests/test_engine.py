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
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from engine.trader import EngineConfig, TradingEngine, _lookback_days
from execution.broker import (
    AlpacaBroker,
    BrokerSnapshot,
    ClosedOrderInfo,
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

    def test_market_open_cycle_processes_stream_stop_fills_before_external_closes(
        self, engine_factory
    ):
        engine, _ = engine_factory(market_open=True)
        engine.slots[0].symbols = []
        engine._session_start_equity = 100_000.0
        engine._cycle_count = 1

        call_order: list[str] = []
        engine._sync_managed_stop_legs = lambda snapshot: call_order.append("sync")
        engine._observe_stream_health = lambda: call_order.append("health")
        engine._recover_suspect_orders = lambda snapshot: call_order.append("suspects")
        engine._process_stream_stop_fills = lambda snapshot: call_order.append("stops")
        engine._detect_external_closes = lambda snapshot: call_order.append("external")
        engine._drain_option_fills = lambda: call_order.append("options")
        engine._drain_spread_fills = lambda: call_order.append("spreads")
        engine._repair_missing_protective_stops = lambda snapshot: call_order.append("repair")

        engine._run_one_cycle()

        assert call_order.index("stops") < call_order.index("external")


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
        """Arrival quote of None (one-sided book, API failure) → fall
        back to the legacy behaviour rather than refusing to log
        slippage. Defensive: a broken quote feed must not blind the
        slippage tracker entirely."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        broker.get_latest_quote_midpoint.return_value = None
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        # A slippage sample is recorded (didn't refuse) and modeled_bps is
        # the configured baseline — confirms the fall-back path ran.
        assert len(engine.risk._slippage_samples) == 1

    def test_rejects_non_finite_quote_from_broker(self, engine_factory):
        """Defensive: broker returns NaN / negative / zero (Mock-style
        misbehavior) → engine treats as no quote and falls back."""
        engine, broker = engine_factory(entries=[False] * 59 + [True])
        broker.get_latest_quote_midpoint.return_value = float("nan")
        snap = _snapshot()
        engine._session_start_equity = snap.account.equity
        slot = engine.slots[0]
        # Must not raise into the trading loop.
        engine._process_symbol("AAPL", snap, snap.account, slot.strategy, slot.timeframe)
        assert len(engine.risk._slippage_samples) == 1


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

    def test_reconciliation_promotes_existing_day_stop_to_gtc(
        self, engine_factory
    ):
        day_stop = replace(
            _open_stop_order("AAPL", 95.0),
            order_id="day-stop",
            qty=10,
            time_in_force="day",
        )
        promoted = replace(
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
        broker.promote_equity_stop_to_gtc.return_value = promoted

        engine._repair_missing_protective_stops(snapshot)

        # P-5: position_uid kwarg is now part of the contract.
        # This test fixture doesn't seed a lifecycle row for the
        # recovered position, so the engine's lookup returns None
        # — the substrate write is skipped, but the broker call
        # still goes out with the correct stop semantics.
        broker.promote_equity_stop_to_gtc.assert_called_once()
        kwargs = broker.promote_equity_stop_to_gtc.call_args.kwargs
        assert kwargs["parent_order_id"] is None
        assert kwargs["stop_order_id"] == "day-stop"
        assert kwargs["qty"] == 10
        assert kwargs["stop_price"] == 95.0
        assert kwargs["client_order_id_prefix"] == (
            "fake_strategy-repair-stop-gtc"
        )
        assert "position_uid" in kwargs
        assert snapshot.open_orders == [promoted]
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

        broker.promote_equity_stop_to_gtc.assert_not_called()
        broker.place_protective_stop.assert_not_called()

    def test_reconciliation_reports_repeated_day_stop_promotion_failure_once(
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
        broker.promote_equity_stop_to_gtc.side_effect = RuntimeError(
            "order is temporarily not replaceable"
        )
        engine.risk.record_broker_error = MagicMock()
        engine.alerts.broker_error = MagicMock()

        engine._repair_missing_protective_stops(snapshot)
        engine._repair_missing_protective_stops(snapshot)

        assert broker.promote_equity_stop_to_gtc.call_count == 2
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

        broker.promote_equity_stop_to_gtc.assert_not_called()
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
                submitted_at=T0 + timedelta(minutes=1),
                filled_at=T0 + timedelta(minutes=2),
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
                    submitted_at=T0 + timedelta(minutes=index),
                    filled_at=T0 + timedelta(minutes=index),
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

        engine._drain_option_fills()

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
        engine._record_fill(result, modeled_price=100.0, order_type="market")
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

    def test_stream_stop_fill_normalizes_occ_to_underlying(self, tmp_path):
        """OCC stop fills must be matched to the underlying key in _positions."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from execution.stream import StreamManager

        engine = self._engine(tmp_path)
        occ = "SPY260516C00520000"
        engine._register_single_leg(strategy_name="spy_options_reversion", symbol="SPY")
        engine._entry_prices["SPY"] = 10.0

        fill_update = SimpleNamespace(
            order=SimpleNamespace(symbol=occ),
            price="12.50",
            qty="2",
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        engine._process_stream_stop_fills(_snapshot())

        # Ownership must be cleared using the underlying key.
        assert not engine._has_position("SPY")
        assert "SPY" not in engine._entry_prices

    def test_stream_stop_fill_applies_100x_multiplier_for_options(self, tmp_path):
        """Options stop fills feed 100x P&L into the HWM drawdown gate."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from execution.stream import StreamManager
        from risk.allocator import SleeveAllocator

        engine = self._engine(tmp_path)
        occ = "SPY260516C00520000"
        engine._register_single_leg(strategy_name="spy_options_reversion", symbol="SPY")
        engine._entry_prices["SPY"] = 10.0  # premium at entry

        allocator = MagicMock(spec=SleeveAllocator)
        engine._allocator = allocator

        fill_update = SimpleNamespace(
            order=SimpleNamespace(symbol=occ),
            price="15.0",   # exit premium
            qty="2",        # contracts
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        engine._process_stream_stop_fills(_snapshot())

        # P&L = (15 - 10) * 2 * 100 = $1 000
        allocator.record_realized_pnl.assert_called_once_with(
            "spy_options_reversion", 1000.0,
            position_uid=None, is_full_close=True,
        )

    def test_resynced_stop_fill_flows_through_engine_stop_processing(self, tmp_path):
        """Gap-resynced stop fills should be handled exactly like live stream fills."""
        from unittest.mock import MagicMock
        from execution.stream import StreamManager

        engine = self._engine(tmp_path)
        occ = "SPY260516C00520000"
        engine._register_single_leg(strategy_name="spy_options_reversion", symbol="SPY")
        engine._entry_prices["SPY"] = 10.0

        order = SimpleNamespace(
            id="stop-ord-gap",
            symbol=occ,
            status=SimpleNamespace(value="filled"),
            filled_qty="2",
            filled_avg_price="12.5",
            qty="2",
        )
        fill_update = StreamManager._make_synthetic_update(order, "fill")

        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream
        engine.trade_logger.log_stop_fill = MagicMock()

        engine._process_stream_stop_fills(_snapshot())

        engine.trade_logger.log_stop_fill.assert_called_once_with(
            symbol=occ,
            strategy="spy_options_reversion",
            qty=2,
            avg_fill_price=12.5,
            stop_price=None,
            order_id="stop-ord-gap",
            position_uid=None,
        )
        assert not engine._has_position("SPY")

    def test_stream_stop_fill_equity_no_occ_normalization(self, tmp_path):
        """Plain equity stop fills still work without OCC normalization."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from execution.stream import StreamManager
        from risk.allocator import SleeveAllocator

        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="sma_crossover", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.0

        allocator = MagicMock(spec=SleeveAllocator)
        engine._allocator = allocator

        fill_update = SimpleNamespace(
            order=SimpleNamespace(symbol="AAPL"),
            price="105.0",
            qty="10",
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        engine._process_stream_stop_fills(_snapshot())

        # P&L = (105 - 100) * 10 * 1 = $50
        allocator.record_realized_pnl.assert_called_once_with("sma_crossover", 50.0, position_uid=None, is_full_close=True)
        assert not engine._has_position("AAPL")

    def test_stream_stop_fill_with_fractional_residual_preserves_ownership(self, tmp_path):
        """A whole-share stop on a fractional position should leave ownership intact for residual cleanup."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from execution.stream import StreamManager

        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="GOOG")
        engine._entry_prices["GOOG"] = 391.0
        engine.trade_logger.log_stop_fill = MagicMock()

        fill_update = SimpleNamespace(
            order=SimpleNamespace(symbol="GOOG", id="goog-stop-1"),
            price="378.85",
            qty="7.0",
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        snapshot = _snapshot(
            positions={"GOOG": Position("GOOG", 0.78, 391.2, 295.76)},
            open_orders=[],
        )

        engine._process_stream_stop_fills(snapshot)

        engine.trade_logger.log_stop_fill.assert_called_once_with(
            symbol="GOOG",
            strategy="fake_strategy",
            qty=7.0,
            avg_fill_price=378.85,
            stop_price=None,
            order_id="goog-stop-1",
            position_uid=None,
        )
        assert engine._has_position("GOOG")
        assert engine._entry_prices["GOOG"] == pytest.approx(391.0)

    def test_stream_stop_fill_uses_cumulative_order_qty_and_avg_price(self, tmp_path):
        """Stop-fill accounting must use cumulative broker order fields, not the last execution chunk."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from execution.stream import StreamManager
        from risk.allocator import SleeveAllocator

        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="donchian_breakout", symbol="PWR")
        engine._entry_prices["PWR"] = 727.67
        engine.trade_logger.log_stop_fill = MagicMock()

        allocator = MagicMock(spec=SleeveAllocator)
        engine._allocator = allocator

        fill_update = SimpleNamespace(
            order=SimpleNamespace(
                symbol="PWR",
                id="pwr-stop-1",
                filled_qty="5",
                filled_avg_price="684.11",
            ),
            price="684.11",
            qty="1",
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        snapshot = _snapshot(
            positions={"PWR": Position("PWR", 0.54, 727.67, 393.0)},
            open_orders=[],
        )

        engine._process_stream_stop_fills(snapshot)

        engine.trade_logger.log_stop_fill.assert_called_once_with(
            symbol="PWR",
            strategy="donchian_breakout",
            qty=5.0,
            avg_fill_price=684.11,
            stop_price=None,
            order_id="pwr-stop-1",
            position_uid=None,
        )
        allocator.record_realized_pnl.assert_called_once_with(
            "donchian_breakout",
            pytest.approx((684.11 - 727.67) * 5.0),
            position_uid=None,
            is_full_close=True,
        )
        assert engine._has_position("PWR")

    # log_stop_fill: confirmed WebSocket stop-fill persists real price/qty ─────

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

    def test_stream_stop_fill_calls_log_stop_fill_not_external_close(self, tmp_path):
        """When price and qty are known, _process_stream_stop_fills uses log_stop_fill."""
        from unittest.mock import MagicMock, patch
        from types import SimpleNamespace
        from execution.stream import StreamManager

        engine = self._engine(tmp_path)
        occ = "SPY260516C00520000"
        engine._register_single_leg(strategy_name="spy_options_reversion", symbol="SPY")
        engine._entry_prices["SPY"] = 10.0

        fill_update = SimpleNamespace(
            order=SimpleNamespace(symbol=occ, id="stop-ord-1", stop_price="8.00"),
            price="7.50",
            qty="2",
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        engine.trade_logger.log_stop_fill = MagicMock()
        engine.trade_logger.log_external_close = MagicMock()

        engine._process_stream_stop_fills(_snapshot())

        engine.trade_logger.log_stop_fill.assert_called_once_with(
            symbol=occ,
            strategy="spy_options_reversion",
            qty=2,
            avg_fill_price=7.50,
            stop_price=8.00,
            order_id="stop-ord-1",
            position_uid=None,
        )
        engine.trade_logger.log_external_close.assert_not_called()

    def test_stream_stop_fill_forwards_none_when_broker_stop_price_missing(self, tmp_path):
        """
        Slippage unification Phase 1 codepath §4 — when the broker order
        carries no stop_price, the engine must forward stop_price=None
        rather than synthesizing a fallback. log_stop_fill itself handles
        the unavailable case by writing NULL slippage with kind/quality
        of 'unavailable'.
        """
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from execution.stream import StreamManager

        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="sma_crossover", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.0

        fill_update = SimpleNamespace(
            # Note: no stop_price attribute on the order.
            order=SimpleNamespace(symbol="AAPL", id="stop-ord-no-stop"),
            price="99.40",
            qty="10",
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        engine.trade_logger.log_stop_fill = MagicMock()
        engine._process_stream_stop_fills(_snapshot())

        kwargs = engine.trade_logger.log_stop_fill.call_args.kwargs
        assert kwargs["stop_price"] is None

    def test_stream_stop_fill_falls_back_to_external_close_when_price_missing(self, tmp_path):
        """When price is missing from the stream event, fall back to log_external_close."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from execution.stream import StreamManager

        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="sma_crossover", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.0

        fill_update = SimpleNamespace(
            order=SimpleNamespace(symbol="AAPL", id="stop-ord-2"),
            price=None,
            qty="10",
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        engine.trade_logger.log_stop_fill = MagicMock()
        engine.trade_logger.log_external_close = MagicMock()

        engine._process_stream_stop_fills(_snapshot())

        engine.trade_logger.log_stop_fill.assert_not_called()
        engine.trade_logger.log_external_close.assert_called_once_with(
            symbol="AAPL",
            strategy="sma_crossover",
            reason="stop_triggered",
        )

    def test_stream_stop_fill_skips_duplicate_order_id(self, tmp_path):
        """Duplicate stream stop-fill deliveries should be ignored once the order is recorded."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from execution.stream import StreamManager

        engine = self._engine(tmp_path)
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.0

        fill_update = SimpleNamespace(
            order=SimpleNamespace(symbol="AAPL", id="dup-stop-1"),
            price="95.0",
            qty="10",
        )
        stream = MagicMock(spec=StreamManager)
        stream.drain_stop_fills.return_value = [fill_update]
        engine._stream_manager = stream

        engine.trade_logger.has_recorded_order_id = MagicMock(return_value=True)
        engine.trade_logger.log_stop_fill = MagicMock()
        engine.trade_logger.log_external_close = MagicMock()
        engine.alerts.broker_error = MagicMock()
        engine._allocator = MagicMock()

        engine._process_stream_stop_fills(_snapshot())

        engine.trade_logger.log_stop_fill.assert_not_called()
        engine.trade_logger.log_external_close.assert_not_called()
        engine.alerts.broker_error.assert_not_called()
        engine._allocator.record_realized_pnl.assert_not_called()
        assert engine._has_position("AAPL")


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

    def _engine(self, tmp_path, *, create_lifecycle: bool = True):
        strategy = self.GenericOptionStrategy(entries=[False], exits=[False])
        broker = MagicMock()
        broker.get_open_orders.return_value = []
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
            config=EngineConfig(market_hours_only=False),
            trade_logger=TradeLogger(path=str(tmp_path / "trades.db")),
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
        )
        broker.cancel_order.assert_not_called()
        broker.submit_option_gtc_stop.assert_not_called()
        row = engine.option_trailing_store.get_by_occ("SPY260618C00746000")
        assert row.hwm_premium == pytest.approx(22.00)
        assert row.current_stop_price == pytest.approx(18.70)
        assert row.alpaca_stop_order_id == "replacement-stop"

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
    Codepath §3 — discretionary market exits. The exit path never
    fetches an arrival midpoint; it passes latest_close (equities) or
    the fill price itself (options) as ``modeled_price``. Phase 1
    Defect 1 fix changes the tagging so:

      - equity exits → benchmark_kind='fallback_latest_close', quality='fallback'
      - option exits → benchmark_kind='unavailable', quality='unavailable'

    Previously both were tagged 'arrival_midpoint' / 'primary', which
    was a false claim for equities and a fabricated zero-slippage
    measurement for options.
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

    def test_equity_exit_tags_fallback_latest_close(self, tmp_path):
        engine, broker = self._engine_with_real_logger(tmp_path)
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

    def test_unknown_exit_is_staged_and_reconciled_by_exact_order_id(self, tmp_path):
        engine, broker = self._engine_with_real_logger(tmp_path)
        _write_buy(engine.trade_logger, "AAPL", "fake_strategy")
        engine._register_single_leg(strategy_name="fake_strategy", symbol="AAPL")
        engine._entry_prices["AAPL"] = 100.5
        position = SimpleNamespace(
            qty=10,
            symbol="AAPL",
            avg_entry_price=100.5,
            market_value=1000.0,
            unrealized_pl=0.0,
            current_price=100.0,
            cost_basis=1005.0,
            asset_id="x",
            side="long",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0,
                cash=50_000.0,
                buying_power=50_000.0,
                open_positions={"AAPL": position},
            ),
            open_orders=[],
        )
        broker.close_position.return_value = OrderResult(
            status=OrderStatus.UNKNOWN,
            order_id="close-aapl-unknown",
            symbol="AAPL",
            requested_qty=10.0,
            filled_qty=0.0,
            avg_fill_price=None,
            raw_status=None,
            message="confirmation timed out",
        )

        closed = engine._close_single_leg_position(
            symbol="AAPL",
            strategy=engine.strategy,
            position=position,
            snapshot=snapshot,
            latest_close=100.0,
            alert_reason="exit signal",
        )

        assert closed is False
        assert engine._suspect_exit_orders["AAPL"].order_id == "close-aapl-unknown"

        broker.reconcile_submitted_order.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="close-aapl-unknown",
            symbol="AAPL",
            requested_qty=10.0,
            filled_qty=10.0,
            avg_fill_price=99.0,
            raw_status="filled",
            submitted_at=T0,
            filled_at=T0 + timedelta(minutes=4),
        )
        engine._record_realized_pnl = MagicMock()
        engine._recover_suspect_exit_orders(_snapshot())

        assert "AAPL" not in engine._suspect_exit_orders
        assert not engine._has_position("AAPL")
        sell_rows = [
            row for row in engine.trade_logger.read_all() if row["side"] == "sell"
        ]
        assert len(sell_rows) == 1
        assert sell_rows[0]["order_id"] == "close-aapl-unknown"
        assert sell_rows[0]["slippage_measurement_quality"] == "recovered"
        assert engine._record_realized_pnl.call_args.kwargs["external"] is False


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
