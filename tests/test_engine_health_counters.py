"""
Unit tests for PLAN 11.10f — Strategy Health lifecycle counter wiring
in engine/trader.py + risk_controls state-snapshot enhancement.

The wiring is observability only: per design §12.4.1 the counters
must never affect trading decisions. These tests pin:

  - Counter emissions at each of the 7 gate boundaries
  - Mutual exclusivity (only one block-type counter increments per
    rejected entry attempt)
  - Per-cycle batching (one DB write per strategy per cycle, not 7
    per symbol)
  - Failure tolerance — counter write/upsert failure logs WARNING
    but never raises into the trading loop
  - Feature flag HEALTH_COUNTERS_ENABLED=False fully short-circuits
    the path (no DB writes, no accumulator growth, trading
    bit-identical to pre-11.10f)
  - risk_controls dict appears in state snapshot, with cooldown +
    drawdown state surfaced
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from config import settings
from engine.trader import EngineConfig, TradingEngine
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
from strategies.base import (
    BaseStrategy,
    EdgeFilterDecision,
    OrderType,
    SignalFrame,
    StrategySlot,
)
from strategies.health.lifecycle import (
    LifecycleCounters,
    read_counters_for_period,
)


# ── Test fixtures (mirror test_engine.py patterns) ────────────────────


T0 = datetime(2026, 4, 16, 14, 30, tzinfo=timezone.utc)


class _FakeStrategy(BaseStrategy):
    """Returns a configurable entry/exit pattern. `entry_on_last_bar`
    True (the test default) puts a single True on the last bar so the
    engine's `iloc[-1]` read picks it up — the canonical entry-fires
    pattern for engine tests. False = no entry."""

    name = "fake_strategy"
    preferred_order_type = OrderType.MARKET

    def __init__(self, *, entry_on_last_bar=False, edge_filter=None):
        super().__init__(edge_filter=edge_filter)
        self._entry_on_last_bar = entry_on_last_bar

    def _raw_signals(self, df):
        n = len(df)
        e = [False] * (n - 1) + [self._entry_on_last_bar]
        x = [False] * n
        return SignalFrame(
            entries=pd.Series(e, index=df.index, dtype=bool),
            exits=pd.Series(x, index=df.index, dtype=bool),
        )


def _bars(n=60, end=T0, base=100.0):
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


def _snapshot(equity=100_000.0):
    return BrokerSnapshot(
        account=AccountState(
            equity=equity,
            cash=equity,
            session_start_equity=equity,
            previous_close_equity=None,
            open_positions={},
        ),
        open_orders=[],
    )


def _filled_result(symbol="AAPL", qty=1, avg=100.5):
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


@pytest.fixture
def patch_fetch(monkeypatch):
    holder = {"df": _bars()}

    def _fetch(symbol, start, end, timeframe="1Day"):
        return holder["df"], SimpleNamespace(api_calls=0)

    monkeypatch.setattr("engine.trader.fetch_symbol", _fetch)
    return holder


@pytest.fixture
def make_engine(patch_fetch, tmp_path):
    """Construct an engine with controllable strategy + broker.
    Each test mutates the strategy / broker MagicMock as needed."""

    def _make(
        *, entries=None, exits=None,
        entry_on_last_bar=None,
        place_result=None,
        broker_extras=None,
    ):
        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.place_order.return_value = place_result or _filled_result()
        broker.close_position.return_value = _filled_result()
        broker.get_open_orders.return_value = []
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)
        if broker_extras:
            for k, v in broker_extras.items():
                setattr(broker, k, v)

        # Translate legacy `entries=[True]` API to the new flag.
        if entry_on_last_bar is None:
            entry_on_last_bar = bool(entries and entries[0])
        strategy = _FakeStrategy(entry_on_last_bar=entry_on_last_bar)
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
        engine = TradingEngine(
            strategy=strategy,
            symbols=["AAPL"],
            risk=risk,
            broker=broker,
            config=cfg,
            trade_logger=trade_logger,
            clock=lambda: T0,
        )
        return engine, broker, strategy, trade_logger

    return _make


# ── Feature flag short-circuits everything ────────────────────────────


class TestFeatureFlagOff:
    """When HEALTH_COUNTERS_ENABLED=False, the entire emission/flush
    path must no-op. No DB writes, no accumulator state, no engine
    behavior change. Belt-and-suspenders for instant operator revert."""

    def test_disabled_flag_no_counter_dict_growth(
        self, make_engine, monkeypatch,
    ):
        monkeypatch.setattr(settings, "HEALTH_COUNTERS_ENABLED", False)
        engine, broker, _, _ = make_engine(entries=[True])
        engine._run_one_cycle()
        # Accumulator must remain empty even with a raw_signal candidate.
        assert engine._cycle_lifecycle_counters == {}

    def test_disabled_flag_no_db_writes(
        self, make_engine, monkeypatch, tmp_path,
    ):
        monkeypatch.setattr(settings, "HEALTH_COUNTERS_ENABLED", False)
        engine, _, _, trade_logger = make_engine(entries=[True])
        engine._run_one_cycle()
        # Counter table exists (migration runs at TradeLogger init)
        # but has no rows.
        conn = trade_logger._ensure_db()
        rows = conn.execute(
            "SELECT COUNT(*) FROM strategy_lifecycle_counters"
        ).fetchone()
        assert rows[0] == 0

    def test_lifecycle_counter_for_returns_none_when_disabled(
        self, make_engine, monkeypatch,
    ):
        monkeypatch.setattr(settings, "HEALTH_COUNTERS_ENABLED", False)
        engine, _, _, _ = make_engine()
        assert engine._lifecycle_counter_for("any") is None


# ── Per-gate counter emissions ────────────────────────────────────────


class TestCounterEmissions:
    """Every gate boundary increments exactly the right field exactly
    once. Mutual exclusivity is the design §12.4.1 contract: if
    regime rejects, only regime_blocked++; if regime passes and edge
    filter rejects, only edge_filter_blocked++. Total blocks ≤
    raw_signals - submitted."""

    def _read_counts(self, trade_logger):
        conn = trade_logger._ensure_db()
        return read_counters_for_period(
            conn,
            strategy_name="fake_strategy",
            start=date(2026, 4, 1),
            end=date(2026, 5, 1),
            period_type="weekly",
        )

    def test_raw_signals_count_on_entry_candidate(self, make_engine):
        engine, _, _, trade_logger = make_engine(entries=[True])
        engine._run_one_cycle()
        c = self._read_counts(trade_logger)
        assert c.raw_signals == 1
        # The candidate either submitted or was blocked; total must add up.
        assert c.submitted + c.regime_blocked + c.edge_filter_blocked + \
            c.sleeve_blocked + c.risk_blocked == 1

    def test_raw_signals_zero_when_no_entry_candidate(self, make_engine):
        engine, _, _, trade_logger = make_engine(entries=[False])
        engine._run_one_cycle()
        c = self._read_counts(trade_logger)
        assert c.raw_signals == 0

    def test_submitted_and_filled_on_successful_entry(self, make_engine):
        engine, broker, _, trade_logger = make_engine(
            entries=[True], place_result=_filled_result(),
        )
        engine._run_one_cycle()
        c = self._read_counts(trade_logger)
        assert c.raw_signals == 1
        assert c.submitted == 1
        assert c.filled_entries == 1
        # No blocks
        assert c.regime_blocked == 0
        assert c.edge_filter_blocked == 0
        assert c.sleeve_blocked == 0
        assert c.risk_blocked == 0

    def test_submitted_no_fill_on_unknown_status(self, make_engine):
        """ACCEPTED/UNKNOWN reach the broker → submitted++, but
        filled_entries only increments on FILLED|PARTIAL."""
        unknown = OrderResult(
            status=OrderStatus.UNKNOWN, order_id="o", symbol="AAPL",
            requested_qty=1, filled_qty=0, avg_fill_price=None,
            raw_status=None, message="pending",
        )
        engine, _, _, trade_logger = make_engine(
            entries=[True], place_result=unknown,
        )
        engine._run_one_cycle()
        c = self._read_counts(trade_logger)
        assert c.submitted == 1
        assert c.filled_entries == 0


class TestMutualExclusivity:
    """Per design §12.4.1: only ONE block counter increments per
    rejected entry attempt. If edge filter blocks, regime_blocked
    must NOT also increment (we returned before reaching the regime
    gate)."""

    def test_edge_filter_block_does_not_increment_regime(self, make_engine):
        """Strategy with an edge filter that rejects every signal.
        raw_entry=True, last_entry=False → edge_filter_blocked
        increments; subsequent gates never reached."""

        class _RejectingFilter:
            def __call__(self, df, symbol=""):
                return EdgeFilterDecision.from_bool_series(
                    pd.Series(
                        [False] * len(df), index=df.index, dtype=bool,
                    ),
                    blocked_reasons=["test: blocking all entries"],
                )
            def set_symbol(self, symbol): pass

        # Use a custom factory invocation that injects the filter.
        # (engine_factory doesn't expose edge_filter directly.)
        engine, _, strategy, trade_logger = make_engine(entry_on_last_bar=True)
        # Inject the filter post-construction; FakeStrategy stores it
        # on the base class.
        strategy._edge_filter = _RejectingFilter()

        engine._run_one_cycle()
        conn = trade_logger._ensure_db()
        c = read_counters_for_period(
            conn, strategy_name="fake_strategy",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
            period_type="weekly",
        )
        assert c.raw_signals == 1
        assert c.edge_filter_blocked == 1
        assert c.regime_blocked == 0
        assert c.sleeve_blocked == 0
        assert c.risk_blocked == 0
        assert c.submitted == 0


class TestPerCycleBatching:
    """Per design §12.4.1: per-cycle counts accumulate in a local
    dict and flush ONCE per strategy at end of cycle — NOT 7 DB
    writes per symbol. Multi-symbol cycles produce one row, not N rows."""

    def test_one_flush_per_strategy_per_cycle(
        self, make_engine, monkeypatch, tmp_path,
    ):
        """Drive the engine with 5 symbols all raising raw signals.
        Verify the strategy_lifecycle_counters table has exactly one
        row for this strategy/week, accumulating all 5 increments."""
        # Use multi-symbol setup via direct slot construction (the
        # make_engine fixture only supports 1 symbol).
        broker = MagicMock()
        broker.sync_with_broker.return_value = _snapshot()
        broker.place_order.return_value = _filled_result()
        broker.close_position.return_value = _filled_result()
        broker.get_open_orders.return_value = []
        broker._with_retry.side_effect = lambda fn, **_: fn()
        broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

        strategy = _FakeStrategy(entry_on_last_bar=True)
        risk = RiskManager(
            max_position_pct=0.02, max_open_positions=5,
            max_gross_exposure_pct=0.50, atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0,
            loss_streak_threshold=10, broker_error_threshold=10,
        )
        cfg = EngineConfig(
            history_lookback_days=120, cycle_interval_seconds=0.01,
            max_bar_age_multiplier=10.0, market_hours_only=False,
            cancel_orders_on_shutdown=True, atr_length=14,
        )
        trade_logger = TradeLogger(path=str(tmp_path / "trades.db"))
        slot = StrategySlot(
            strategy=strategy,
            symbols=["AAPL", "MSFT", "GOOG", "NVDA", "AMZN"],
        )
        engine = TradingEngine(
            slots=[slot],
            risk=risk, broker=broker, config=cfg,
            trade_logger=trade_logger,
            clock=lambda: T0,
        )

        engine._run_one_cycle()

        # Verify ONE row only, not 5.
        conn = trade_logger._ensure_db()
        rows = conn.execute(
            "SELECT raw_signals, COUNT(*) "
            "FROM strategy_lifecycle_counters "
            "WHERE strategy_name='fake_strategy' "
            "GROUP BY raw_signals"
        ).fetchall()
        # Single row, raw_signals = 5 (one per symbol).
        # (Some may have been blocked by various gates; total should
        # still be 5 across all increments.)
        assert len(rows) == 1
        c = read_counters_for_period(
            conn, strategy_name="fake_strategy",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
            period_type="weekly",
        )
        assert c.raw_signals == 5


# ── Failure tolerance ─────────────────────────────────────────────────


class TestFailureTolerance:
    """Per design §12.4.1 hard rule: counter write failure must NEVER
    raise into the trading loop. Wrapped in try/except → log.warning."""

    def test_flush_upsert_failure_does_not_raise(
        self, make_engine, monkeypatch,
    ):
        """Inject a failing upsert_counters and verify the cycle
        completes without raising."""

        def _raises(*args, **kwargs):
            raise sqlite3.OperationalError("simulated DB outage")

        monkeypatch.setattr(
            "strategies.health.lifecycle.upsert_counters", _raises,
        )
        engine, _, _, _ = make_engine(entries=[True])
        # Should NOT raise.
        engine._run_one_cycle()

    def test_engine_continues_processing_after_flush_failure(
        self, make_engine, monkeypatch,
    ):
        """When the upsert raises, the engine's cycle still completes
        cleanly and the next cycle is queued. (Compatible cycle output:
        the cycle completion log still runs.)"""

        def _raises(*args, **kwargs):
            raise sqlite3.OperationalError("simulated DB outage")

        from strategies.health import lifecycle as _lc_mod
        monkeypatch.setattr(_lc_mod, "upsert_counters", _raises)

        engine, _, _, _ = make_engine(entry_on_last_bar=True)
        # Run two cycles back-to-back — flush failure on cycle 1
        # must not interfere with cycle 2's setup.
        engine._run_one_cycle()
        engine._run_one_cycle()
        # If we reach here without an exception, the contract holds.


# ── risk_controls state snapshot ──────────────────────────────────────


class TestRiskControlsSnapshot:
    """The state snapshot now exposes risk_controls per design §12.4.1
    for HealthAssessor L1 reads. The accessors are read-only — no
    behavior change to RiskManager or SleeveAllocator."""

    def test_snapshot_includes_risk_controls(self, make_engine, tmp_path, monkeypatch):
        engine, _, _, _ = make_engine()
        # Redirect state snapshot path to tmp.
        snapshot_path = tmp_path / "engine_state.json"
        monkeypatch.setattr(
            settings, "STATE_SNAPSHOT_PATH", str(snapshot_path),
        )
        engine._run_one_cycle()
        assert snapshot_path.exists()
        data = json.loads(snapshot_path.read_text())
        assert "risk_controls" in data
        rc = data["risk_controls"]
        assert "is_halted" in rc
        assert "halt_reason" in rc
        assert "cooldown_state" in rc
        assert "sleeve_dd_state" in rc

    def test_cooldown_snapshot_empty_when_no_losses(self):
        """Fresh RiskManager → cooldown_state is empty dict."""
        risk = RiskManager(
            max_position_pct=0.02, max_open_positions=5,
            max_gross_exposure_pct=0.50, atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0,
            loss_streak_threshold=10, broker_error_threshold=10,
        )
        assert risk.cooldown_snapshot() == {}

    def test_cooldown_snapshot_reflects_loss_streak(self):
        """Recording losses should make them appear in the snapshot."""
        risk = RiskManager(
            max_position_pct=0.02, max_open_positions=5,
            max_gross_exposure_pct=0.50, atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0,
            loss_streak_threshold=3, broker_error_threshold=10,
            loss_streak_cooldown_hours=12,
        )
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        # Two losses — below threshold of 3.
        risk.record_trade_result("x", pnl=-100.0, now=now)
        risk.record_trade_result("x", pnl=-100.0, now=now)
        snap = risk.cooldown_snapshot(now=now)
        assert "x" in snap
        assert snap["x"]["loss_streak"] == 2
        assert snap["x"]["active"] is False  # not yet hit threshold

    def test_cooldown_snapshot_active_after_threshold(self):
        risk = RiskManager(
            max_position_pct=0.02, max_open_positions=5,
            max_gross_exposure_pct=0.50, atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0,
            loss_streak_threshold=3, broker_error_threshold=10,
            loss_streak_cooldown_hours=12,
        )
        now = datetime(2026, 5, 19, 12, 0, tzinfo=timezone.utc)
        for _ in range(3):
            risk.record_trade_result("x", pnl=-100.0, now=now)
        snap = risk.cooldown_snapshot(now=now + timedelta(hours=1))
        assert snap["x"]["active"] is True
        assert snap["x"]["loss_streak"] == 3
        assert snap["x"]["until"] is not None


class TestAllocatorDrawdownSnapshot:
    """SleeveAllocator.drawdown_snapshot — read-only HWM drawdown
    state surfacing for HealthAssessor L1 reads."""

    def _build_allocator(self, *, dd_threshold=0.20):
        from risk.allocator import SleeveAllocator
        return SleeveAllocator(
            allocations={
                "x": {
                    "target_pct": 1.0, "type": "equity",
                    "priority": 1, "pool_type": "equity",
                    "can_stretch": False, "hard_max_positions": 5,
                    "max_position_pct_of_sleeve": 0.40,
                },
            },
            total_gross_pct=0.5,
            capital_pools={"equity": 1.0, "isolated_options": 0.0},
            stretch_utilization_threshold=0.80,
            default_stretch_pct=0.15,
            dd_threshold=dd_threshold,
        )

    def test_drawdown_snapshot_returns_dict_per_strategy(self):
        alloc = self._build_allocator()
        snap = alloc.drawdown_snapshot(equity=100_000.0)
        assert "x" in snap
        assert "in_drawdown" in snap["x"]
        assert snap["x"]["running_pnl"] == 0.0
        assert snap["x"]["hwm_pnl"] == 0.0
        assert snap["x"]["drawdown_dollars"] == 0.0

    def test_drawdown_snapshot_reflects_recorded_pnl(self):
        alloc = self._build_allocator()
        # Gain $5,000 then lose $3,000 — running -3000+5000=$2000,
        # HWM = $5,000, drawdown = $3,000.
        alloc.record_realized_pnl("x", 5_000.0)
        alloc.record_realized_pnl("x", -3_000.0)
        snap = alloc.drawdown_snapshot(equity=100_000.0)
        assert snap["x"]["running_pnl"] == 2_000.0
        assert snap["x"]["hwm_pnl"] == 5_000.0
        assert snap["x"]["drawdown_dollars"] == 3_000.0


# ── Counter dict reset at cycle boundary ──────────────────────────────


class TestCycleResetIsolation:
    """Each cycle's counters are independent — reset at start of
    cycle. A bad counter state in cycle N+1 shouldn't be poisoned by
    cycle N (and vice versa)."""

    def test_accumulator_reset_at_cycle_start(self, make_engine):
        """The reset at cycle start MUST clear any stale state from
        the previous cycle's accumulator dict — otherwise stale
        in-memory counts would re-flush every cycle.

        We test this by pre-populating the accumulator with bogus
        values, running a cycle (which should reset to empty before
        accumulating real counts), and asserting the bogus values
        didn't propagate to the DB."""
        engine, _, _, _ = make_engine(entry_on_last_bar=True)
        # Pre-populate with bogus state
        engine._cycle_lifecycle_counters = {
            "fake_strategy": LifecycleCounters(raw_signals=999),
        }
        engine._run_one_cycle()
        # After cycle the DB shows the REAL cycle's counts (1 raw_signal),
        # NOT the bogus 999.
        from strategies.health.lifecycle import read_counters_for_period
        conn = engine.trade_logger._ensure_db()
        c = read_counters_for_period(
            conn, strategy_name="fake_strategy",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
            period_type="weekly",
        )
        # If the reset failed, raw_signals would be 999 (or 999+1=1000).
        # Since reset clears the dict, only the real cycle's count lands.
        assert c.raw_signals == 1, (
            f"reset bug — expected real cycle count (1), got {c.raw_signals}"
        )
