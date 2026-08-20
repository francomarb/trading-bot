"""
Unit tests for the Phase 9 reporting module.

Covers:
  - TradeLogger: SQLite creation, record building, append, read-back
  - PnLTracker: daily summary, per-strategy attribution, intraday drawdown,
    slippage stats, weekly report generation
  - AlertDispatcher: fire, cooldown suppression, backend failure isolation
  - install_json_sink: creates the sink without crashing
  - Engine integration: _log_entry / _log_close wiring
"""

from __future__ import annotations

import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from execution.broker import OrderResult, OrderStatus
from reporting.alerts import (
    Alert,
    AlertBackend,
    AlertDispatcher,
    AlertSeverity,
    AlertType,
    LogFileBackend,
)
from reporting.logger import (
    ENTRY_BASIS_BROKER_FILL,
    ENTRY_BASIS_MIXED,
    ENTRY_BASIS_REFERENCE_FALLBACK,
    TRADE_CSV_COLUMNS,
    TradeLogger,
    TradeRecord,
    install_json_sink,
    mleg_realized_slippage_bps,
    single_leg_realized_slippage_bps,
)
from reporting.logger import (
    CALIBRATION_GRADE_QUALITIES,
    EXECUTION_QUALITY_KINDS,
    IMPLEMENTATION_SHORTFALL_KINDS,
    STOP_GAP_KINDS,
    UNMEASURED_KINDS,
    SlippageBenchmarkKind,
    execution_quality_sql,
    is_execution_quality_measurement,
)
from reporting.pnl import (
    DailySummary,
    PnLTracker,
    StrategyStats,
    _instrument_class,
    _slippage_by_instrument,
    max_drawdown_from_equity_path,
    unrealized_pnl_from_positions,
)
from risk.manager import RiskDecision, Side
from strategies.base import OrderType


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_csv(tmp_path: Path) -> str:
    return str(tmp_path / "trades.db")


@pytest.fixture
def tmp_daily_dir(tmp_path: Path) -> str:
    return str(tmp_path / "daily_pnl")


@pytest.fixture
def tmp_weekly_dir(tmp_path: Path) -> str:
    return str(tmp_path / "weekly_reports")


@pytest.fixture
def sample_decision() -> RiskDecision:
    return RiskDecision(
        symbol="AAPL",
        side=Side.BUY,
        qty=10,
        entry_reference_price=150.0,
        stop_price=145.0,
        strategy_name="sma_crossover",
        reason="test entry",
        order_type=OrderType.MARKET,
    )


@pytest.fixture
def sample_result() -> OrderResult:
    return OrderResult(
        status=OrderStatus.FILLED,
        order_id="abc123",
        symbol="AAPL",
        requested_qty=10,
        filled_qty=10,
        avg_fill_price=150.05,
        raw_status="filled",
        message="filled 10 @ 150.05",
    )


# ── TestTradeLogger ─────────────────────────────────────────────────────────


class TestTradeLogger:
    def test_db_created_with_trades_table(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)
        tl.log(record)

        conn = sqlite3.connect(tmp_csv)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trades'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_record_fields(self, sample_decision, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)

        assert record.symbol == "AAPL"
        assert record.side == "buy"
        assert record.qty == 10
        assert record.avg_fill_price == 150.05
        assert record.order_id == "abc123"
        assert record.strategy == "sma_crossover"
        assert record.stop_price == 145.0
        assert record.entry_reference_price == 150.0
        assert record.order_type == "market"
        assert record.status == "filled"
        # PLAN 11.53: `initial_stop_loss` records the stop the BROKER
        # placed, reported on the OrderResult. This fixture builds a bare
        # OrderResult with no `placed_stop_price`, which is the honest
        # signal for "this path did not report a stop" — so it falls back
        # to the decision's level rather than inferring a re-anchor that
        # may not have happened. Whether the stop was re-anchored depends
        # on the execution path, not the decision.
        assert record.initial_stop_loss == pytest.approx(145.0)
        # Risk per share is fill-to-stop: 150.05 − 145.00.
        assert record.initial_risk_per_share == pytest.approx(5.05)
        assert record.initial_risk_dollars == pytest.approx(50.5)
        assert record.entry_timestamp is not None
        assert record.exit_timestamp is None

    def test_slippage_calculation(self, sample_decision, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)
        # Buy entry: paying above the modeled price is adverse.
        # Phase 2: legacy `modeled_slippage_bps` / `realized_slippage_bps`
        # are no longer written; the unified `slippage_*` columns are
        # the sole source of truth.
        assert record.modeled_slippage_bps is None
        assert record.realized_slippage_bps is None
        assert record.slippage_signed_bps == pytest.approx(3.33)
        assert record.slippage_adverse_bps == pytest.approx(3.33)

    def test_single_leg_slippage_is_signed_by_side(self):
        assert single_leg_realized_slippage_bps(
            side="buy",
            reference_price=100.0,
            actual_fill_price=99.50,
        ) == pytest.approx(-50.0)
        assert single_leg_realized_slippage_bps(
            side="sell",
            reference_price=100.0,
            actual_fill_price=99.50,
        ) == pytest.approx(50.0)

    def test_append_multiple_records(self, tmp_csv, sample_decision, sample_result):
        """Foundation §6.5: trades is one row per order_id (single-leg).
        Three distinct order_ids produce three rows. Re-logging the
        same order_id UPSERTs."""
        from dataclasses import replace
        tl = TradeLogger(path=tmp_csv)
        for i in range(3):
            result = replace(sample_result, order_id=f"abc{i}")
            record = tl.build_record(sample_decision, result)
            tl.log(record)
        rows = tl.read_all()
        assert len(rows) == 3

    def test_log_with_same_order_id_upserts(
        self, tmp_csv, sample_decision, sample_result
    ):
        """Foundation §6.5: re-logging the same single-leg order_id
        updates the existing row rather than appending. R5-C1 + R5-C2."""
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(sample_decision, sample_result)
        tl.log(record)
        tl.log(record)
        tl.log(record)
        rows = tl.read_all()
        assert len(rows) == 1
        assert rows[0]["order_id"] == sample_result.order_id

    def test_read_all_empty(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        assert tl.read_all() == []

    def test_build_close_record(self, sample_result):
        tl = TradeLogger(path="/dev/null")
        # Phase 2: build_close_record requires an explicit benchmark_kind
        # to compute the new taxonomy columns. Use 'arrival_midpoint' to
        # match the close-time arrival price the engine passes from the
        # quote midpoint at submit.
        record = tl.build_close_record(
            sample_result, strategy_name="sma_crossover", modeled_price=150.0,
            benchmark_kind="arrival_midpoint",
        )
        assert record.side == "sell"
        assert record.strategy == "sma_crossover"
        assert record.reason == "exit signal"
        assert record.stop_price == 0.0
        assert record.exit_timestamp is not None
        # Phase 2: legacy columns NULL on new rows.
        assert record.modeled_slippage_bps is None
        assert record.realized_slippage_bps is None
        # Sell exit: filling above the modeled price is price improvement.
        assert record.slippage_signed_bps == pytest.approx(-3.33)
        # Adverse-only column clamps to 0 for price improvement.
        assert record.slippage_adverse_bps == pytest.approx(0.0)

    def test_build_close_record_computes_realized_pnl_and_r(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        tl.log(tl.build_record(sample_decision, sample_result, modeled_price=150.0))
        close_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="sell-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=160.0,
            raw_status="filled",
            message="filled 10 @ 160.0",
        )
        record = tl.build_close_record(
            close_result,
            strategy_name="sma_crossover",
            modeled_price=160.0,
        )
        assert record.realized_pnl == pytest.approx(99.5)
        # PLAN 11.53: risk basis is fill-to-stop (150.05 − 145.00 = 5.05),
        # inherited from the entry row, so R measures the loss against the
        # risk actually on the table rather than the planned one.
        assert record.initial_risk_dollars == pytest.approx(50.5)
        assert record.r_multiple == pytest.approx(99.5 / 50.5, abs=0.001)

    def test_read_latest_open_entry_context_public_wrapper(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        tl.log(tl.build_record(sample_decision, sample_result, modeled_price=150.0))

        context = tl.read_latest_open_entry_context(
            symbol="AAPL",
            strategy="sma_crossover",
        )

        assert context is not None
        assert context["entry_reference_price"] == pytest.approx(150.0)
        assert context["entry_fill_price"] == pytest.approx(150.05)
        # PLAN 11.53: fill-to-stop (150.05 − 145.00), not reference-to-stop.
        assert context["initial_risk_per_share"] == pytest.approx(5.05)

    def test_open_entry_context_tracks_weighted_fill_basis_after_partial_sell(
        self, tmp_csv
    ):
        tl = TradeLogger(path=tmp_csv)
        first_decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            entry_reference_price=99.0,
            stop_price=95.0,
            strategy_name="sma_crossover",
            reason="first entry",
            order_type=OrderType.MARKET,
        )
        first_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="buy-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=100.0,
            raw_status="filled",
            message="filled",
        )
        tl.log(tl.build_record(first_decision, first_result, modeled_price=99.0))
        partial_close = OrderResult(
            status=OrderStatus.PARTIAL,
            order_id="sell-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=4,
            avg_fill_price=110.0,
            raw_status="partially_filled",
            message="partial",
        )
        partial_close_record = tl.build_close_record(
            partial_close,
            strategy_name="sma_crossover",
            modeled_price=110.0,
        )
        assert partial_close_record.realized_pnl == pytest.approx(40.0)
        tl.log(partial_close_record)
        second_decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=2,
            entry_reference_price=119.0,
            stop_price=114.0,
            strategy_name="sma_crossover",
            reason="add",
            order_type=OrderType.MARKET,
        )
        second_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="buy-2",
            symbol="AAPL",
            requested_qty=2,
            filled_qty=2,
            avg_fill_price=120.0,
            raw_status="filled",
            message="filled",
        )
        tl.log(tl.build_record(second_decision, second_result, modeled_price=119.0))

        context = tl.read_latest_open_entry_context(
            symbol="AAPL",
            strategy="sma_crossover",
        )

        assert context is not None
        assert context["entry_fill_price"] == pytest.approx(105.0)
        assert context["entry_reference_price"] == pytest.approx(99.0)

    def test_open_entry_context_falls_back_for_legacy_missing_fill_price(
        self, tmp_csv, sample_decision
    ):
        tl = TradeLogger(path=tmp_csv)
        legacy_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="legacy-buy",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=None,
            raw_status="filled",
            message="legacy row",
        )
        tl.log(tl.build_record(
            sample_decision,
            legacy_result,
            modeled_price=150.0,
        ))

        context = tl.read_latest_open_entry_context(
            symbol="AAPL",
            strategy="sma_crossover",
        )

        assert context is not None
        assert context["entry_fill_price"] == pytest.approx(150.0)

    def test_partial_stop_fill_preserves_open_owner_context(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        decision = RiskDecision(
            symbol="GOOG",
            side=Side.BUY,
            qty=7.78,
            entry_reference_price=391.0,
            stop_price=378.85,
            strategy_name="donchian_breakout",
            reason="test entry",
            order_type=OrderType.MARKET,
        )
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="buy-goog",
            symbol="GOOG",
            requested_qty=7.78,
            filled_qty=7.78,
            avg_fill_price=391.2,
            raw_status="filled",
            message="filled 7.78 @ 391.2",
        )
        tl.log(tl.build_record(decision, result, modeled_price=391.0))
        tl.log_stop_fill(
            symbol="GOOG",
            strategy="donchian_breakout",
            qty=7.0,
            avg_fill_price=378.85,
            order_id="stop-goog-1",
        )

        assert tl.read_owner_for_symbol("GOOG") == "donchian_breakout"
        assert tl.read_all_open_owners() == {"GOOG": "donchian_breakout"}
        assert tl.read_latest_open_stop_price(
            symbol="GOOG",
            strategy="donchian_breakout",
        ) == pytest.approx(378.85)
        context = tl.read_latest_open_entry_context(
            symbol="GOOG",
            strategy="donchian_breakout",
        )
        assert context is not None
        assert context["entry_reference_price"] == pytest.approx(391.0)
        assert context["entry_fill_price"] == pytest.approx(391.2)
        stop_record = tl.read_recent(1)[0]
        assert stop_record["realized_pnl"] == pytest.approx(-86.45)
        assert tl.has_recorded_order_id("stop-goog-1") is True

    def test_read_realized_pnl_events_for_day_filters_by_exit_date(self, tmp_csv):
        """The EOD-summary helper returns only closes whose
        ``exit_timestamp`` falls on the supplied UTC date — and includes
        single-leg AND spread, filled AND partial."""
        tl = TradeLogger(path=tmp_csv)

        def _close_row(*, day, pnl, status, position_type="single_leg",
                       side="sell", uid=None) -> TradeRecord:
            ts = f"{day}T15:30:00+00:00"
            return TradeRecord(
                timestamp=ts, symbol="X", side=side, qty=1,
                avg_fill_price=100.0,
                order_id=f"c-{day}-{pnl}",
                strategy="sma_crossover", reason="exit signal",
                stop_price=0.0, entry_reference_price=100.0,
                modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
                order_type="market", status=status,
                requested_qty=1, filled_qty=1,
                realized_pnl=pnl,
                entry_timestamp=f"{day}T13:00:00+00:00",
                exit_timestamp=ts,
                position_type=position_type, position_uid=uid,
            )

        tl.log(_close_row(day="2026-06-09", pnl=100.0, status="filled"))
        tl.log(_close_row(day="2026-06-09", pnl=-50.0, status="filled"))
        # Same UID partial → still contributes its dollar slice for the day.
        tl.log(_close_row(day="2026-06-09", pnl=-20.0, status="partial",
                          uid="pos-X"))
        # Different day — must NOT appear.
        tl.log(_close_row(day="2026-06-10", pnl=300.0, status="filled"))

        events = tl.read_realized_pnl_events_for_day("2026-06-09")
        assert sorted(p for _, p in events) == [-50.0, -20.0, 100.0]
        # 06-10 only sees its own row.
        assert tl.read_realized_pnl_events_for_day("2026-06-10") == [
            ("sma_crossover", 300.0),
        ]
        # Unknown day returns empty without raising.
        assert tl.read_realized_pnl_events_for_day("2026-06-30") == []

    def test_read_realized_pnl_events_for_day_handles_missing_db(self, tmp_csv):
        """Missing-DB path returns []; never raises."""
        tl = TradeLogger(path="/nonexistent/path/trades.db")
        assert tl.read_realized_pnl_events_for_day("2026-06-09") == []

    def test_read_realized_pnl_events_in_range_is_inclusive_of_both_ends(
        self, tmp_csv,
    ):
        """The weekly report passes the window's first and last day, so
        an exclusive bound would silently drop a Monday or a Sunday of
        P&L from every digest."""
        tl = TradeLogger(path=tmp_csv)
        for day, pnl in [
            ("2026-06-07", -1.0),   # day before the window
            ("2026-06-08", 10.0),   # window start
            ("2026-06-10", 20.0),   # middle
            ("2026-06-14", 30.0),   # window end
            ("2026-06-15", -2.0),   # day after the window
        ]:
            _log_close(tl, day=day, symbol="X", strategy="sma", pnl=pnl,
                       seq=int(day[-2:]))

        events = tl.read_realized_pnl_events_in_range("2026-06-08", "2026-06-14")
        assert sorted(p for _, p in events) == [10.0, 20.0, 30.0]

    def test_read_realized_pnl_events_for_day_delegates_to_the_range_query(
        self, tmp_csv,
    ):
        """One definition of "which rows are a realized close", so the
        daily and weekly reports cannot drift apart."""
        tl = TradeLogger(path=tmp_csv)
        _log_close(tl, day="2026-06-09", symbol="X", strategy="sma", pnl=42.0)

        assert (
            tl.read_realized_pnl_events_for_day("2026-06-09")
            == tl.read_realized_pnl_events_in_range("2026-06-09", "2026-06-09")
            == [("sma", 42.0)]
        )

    def test_read_realized_pnl_events_in_range_handles_missing_db(self):
        """Missing-DB path returns []; never raises."""
        tl = TradeLogger(path="/nonexistent/path/trades.db")
        assert tl.read_realized_pnl_events_in_range("2026-06-01", "2026-06-30") == []

    def test_read_strategy_realized_pnl_summary_reconstructs_hwm(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        rows = [
            TradeRecord(
                position_type="single_leg",
                timestamp="2026-04-22T10:00:00+00:00",
                symbol=f"SYM{i}",
                side="sell",
                qty=1,
                avg_fill_price=100.0,
                order_id=f"sell-{i}",
                strategy="sma_crossover",
                reason="exit signal",
                stop_price=0.0,
                entry_reference_price=100.0,
                modeled_slippage_bps=0.0,
                realized_slippage_bps=0.0,
                order_type="market",
                status="filled",
                requested_qty=1,
                filled_qty=1,
                initial_stop_loss=95.0,
                initial_risk_per_share=5.0,
                initial_risk_dollars=5.0,
                realized_pnl=pnl,
                r_multiple=None,
                entry_timestamp="2026-04-21T10:00:00+00:00",
                exit_timestamp="2026-04-22T10:00:00+00:00",
            )
            for i, pnl in enumerate([100.0, -50.0, 25.0], start=1)
        ]
        for row in rows:
            tl.log(row)

        summary = tl.read_strategy_realized_pnl_summary(["sma_crossover", "rsi_reversion"])

        assert summary["sma_crossover"]["realized_pnl"] == pytest.approx(75.0)
        assert summary["sma_crossover"]["hwm"] == pytest.approx(100.0)
        # trade_count counts distinct positions (PR #56 R1). The three
        # legacy rows in this fixture have no position_uid → each counts
        # as one, so trade_count is 3.
        assert summary["sma_crossover"]["trade_count"] == pytest.approx(3.0)
        assert summary["rsi_reversion"] == {
            "realized_pnl": 0.0, "hwm": 0.0,
            "trade_count": 0.0, "seen_position_uids": [],
        }

    def test_partial_close_row_alone_does_not_increment_trade_count(self, tmp_csv):
        """PR #56 R3: a partial-close row whose position has not yet
        been fully closed must NOT count as a completed round trip
        during restart restoration. The reviewer's bug scenario:

          - Strategy at N=floor-1 (drawdown gate observational).
          - Position opens, partially closes → row with status='partial',
            position_uid='X', realized_pnl=partial-P&L.
          - Bot restarts. Restart reads the partial row.
          - Before the fix: trade_count was prematurely incremented at
            restart, flipping the gate to normal tier while the residual
            was still open.
          - After the fix: trade_count only advances when the eventual
            status='filled' row is written.

        Live and restart must use the same definition of "completed
        round trip" — matching the live allocator's is_full_close=True
        gating.
        """
        tl = TradeLogger(path=tmp_csv)

        def _close_row(*, status: str, uid: str | None, pnl: float, i: int) -> TradeRecord:
            return TradeRecord(
                position_type="single_leg",
                timestamp=f"2026-04-22T10:0{i}:00+00:00",
                symbol=f"SYM{i}",
                side="sell",
                qty=1, avg_fill_price=100.0,
                order_id=f"close-{i}",
                strategy="sma_crossover",
                reason="exit signal",
                stop_price=0.0, entry_reference_price=100.0,
                modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
                order_type="market", status=status,
                requested_qty=1, filled_qty=1,
                initial_stop_loss=95.0, initial_risk_per_share=5.0,
                initial_risk_dollars=5.0,
                realized_pnl=pnl, r_multiple=None,
                entry_timestamp="2026-04-21T10:00:00+00:00",
                exit_timestamp=f"2026-04-22T10:0{i}:00+00:00",
                position_uid=uid,
            )

        # Step 1: a partial-close row exists, residual still open.
        tl.log(_close_row(status="partial", uid="pos-mid-trade", pnl=-50.0, i=0))
        summary = tl.read_strategy_realized_pnl_summary(["sma_crossover"])
        # P&L from the partial IS counted — dollar math is honest.
        assert summary["sma_crossover"]["realized_pnl"] == pytest.approx(-50.0)
        # But trade_count is NOT incremented — round trip incomplete.
        assert summary["sma_crossover"]["trade_count"] == 0
        # And the UID is NOT marked as seen, so when the full close
        # arrives later it will correctly count once.
        assert summary["sma_crossover"]["seen_position_uids"] == []

        # Step 2: the residual eventually fully closes.
        tl.log(_close_row(status="filled", uid="pos-mid-trade", pnl=-25.0, i=1))
        summary = tl.read_strategy_realized_pnl_summary(["sma_crossover"])
        # Cumulative P&L = -50 + -25 = -75 (both events contribute).
        assert summary["sma_crossover"]["realized_pnl"] == pytest.approx(-75.0)
        # NOW trade_count increments to 1 (one completed round trip).
        assert summary["sma_crossover"]["trade_count"] == 1
        assert summary["sma_crossover"]["seen_position_uids"] == ["pos-mid-trade"]

    def test_partial_only_with_legacy_uid_also_excluded(self, tmp_csv):
        """The status='filled' gate applies even to legacy rows without
        a position_uid. A partial row with no UID still contributes to
        P&L but does not increment trade_count."""
        tl = TradeLogger(path=tmp_csv)
        tl.log(TradeRecord(
            position_type="single_leg",
            timestamp="2026-04-22T10:00:00+00:00",
            symbol="X", side="sell", qty=1, avg_fill_price=100.0,
            order_id="c-0", strategy="sma_crossover",
            reason="exit signal",
            stop_price=0.0, entry_reference_price=100.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
            order_type="market", status="partial",
            requested_qty=1, filled_qty=1,
            realized_pnl=-100.0,
            position_uid=None,  # legacy
        ))
        summary = tl.read_strategy_realized_pnl_summary(["sma_crossover"])
        assert summary["sma_crossover"]["realized_pnl"] == pytest.approx(-100.0)
        assert summary["sma_crossover"]["trade_count"] == 0

    def test_close_records_with_position_uid_dedup_on_restart(self, tmp_csv):
        """PR #56 R2: when close rows have position_uid (the live path
        after the logger fix), restart reconstruction must count
        distinct positions — partial closes of the same position do
        NOT inflate trade_count."""
        tl = TradeLogger(path=tmp_csv)
        # Position pos-A: two partial closes (-50 + -25). One round trip.
        # Position pos-B: one close (+100). One round trip.
        # Position pos-C: one close (-30). One round trip.
        # Two legacy rows: no position_uid. Each counts as one.
        rows = [
            ("pos-A", -50.0),
            ("pos-A", -25.0),       # second partial of same position
            ("pos-B", 100.0),
            ("pos-C", -30.0),
            (None, -10.0),          # legacy
            (None, -5.0),           # legacy
        ]
        for i, (uid, pnl) in enumerate(rows):
            tl.log(TradeRecord(
                position_type="single_leg",
                timestamp=f"2026-04-22T10:0{i}:00+00:00",
                symbol=f"SYM{i}",
                side="sell",
                qty=1, avg_fill_price=100.0,
                order_id=f"close-{i}",
                strategy="sma_crossover",
                reason="exit signal",
                stop_price=0.0, entry_reference_price=100.0,
                modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
                order_type="market", status="filled",
                requested_qty=1, filled_qty=1,
                initial_stop_loss=95.0, initial_risk_per_share=5.0,
                initial_risk_dollars=5.0,
                realized_pnl=pnl, r_multiple=None,
                entry_timestamp="2026-04-21T10:00:00+00:00",
                exit_timestamp=f"2026-04-22T10:0{i}:00+00:00",
                position_uid=uid,
            ))

        summary = tl.read_strategy_realized_pnl_summary(["sma_crossover"])
        # Realized P&L sums everything: -50 -25 +100 -30 -10 -5 = -20
        assert summary["sma_crossover"]["realized_pnl"] == pytest.approx(-20.0)
        # trade_count: 3 distinct UIDs + 2 legacy = 5 round trips
        # (the two pos-A rows count as 1).
        assert summary["sma_crossover"]["trade_count"] == pytest.approx(5.0)
        # seen_position_uids reflects the distinct UIDs.
        assert summary["sma_crossover"]["seen_position_uids"] == [
            "pos-A", "pos-B", "pos-C",
        ]

    def test_option_entry_uses_contract_multiplier_for_initial_risk(self):
        tl = TradeLogger(path="/dev/null")
        decision = RiskDecision(
            symbol="SPY260616C00520000",
            side=Side.BUY,
            qty=2,
            entry_reference_price=3.50,
            stop_price=2.50,
            strategy_name="spy_options_reversion",
            reason="test option entry",
            order_type=OrderType.LIMIT,
            limit_price=3.50,
        )
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="opt-buy-1",
            symbol="SPY260616C00520000",
            requested_qty=2,
            filled_qty=2,
            avg_fill_price=3.55,
            raw_status="filled",
            message="filled 2 @ 3.55",
        )
        record = tl.build_record(decision, result, modeled_price=3.50)
        # PLAN 11.53: risk per share is fill-to-stop, not reference-to-stop.
        # Filled at 3.55 against a 2.50 stop = 1.05 of premium at risk; the
        # old reference-based 1.00 understated it. The multiplier assertion
        # below is what this test is actually about and is unaffected.
        assert record.initial_risk_per_share == pytest.approx(1.05)
        assert record.initial_risk_dollars == pytest.approx(1.05 * 2 * 100)

    def test_option_close_uses_contract_multiplier_for_realized_pnl(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        decision = RiskDecision(
            symbol="SPY260616C00520000",
            side=Side.BUY,
            qty=2,
            entry_reference_price=3.50,
            stop_price=2.50,
            strategy_name="spy_options_reversion",
            reason="test option entry",
            order_type=OrderType.LIMIT,
            limit_price=3.50,
        )
        entry_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="opt-buy-1",
            symbol="SPY260616C00520000",
            requested_qty=2,
            filled_qty=2,
            avg_fill_price=3.55,
            raw_status="filled",
            message="filled 2 @ 3.55",
        )
        tl.log(tl.build_record(decision, entry_result, modeled_price=3.50))
        close_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="opt-sell-1",
            symbol="SPY260616C00520000",
            requested_qty=2,
            filled_qty=2,
            avg_fill_price=4.50,
            raw_status="filled",
            message="filled 2 @ 4.50",
        )
        record = tl.build_close_record(
            close_result,
            strategy_name="spy_options_reversion",
            modeled_price=4.50,
        )
        # PLAN 11.53: 1.05 of premium at risk (fill 3.55 → stop 2.50)
        # × 2 contracts × 100. The multiplier is what this test pins.
        assert record.initial_risk_dollars == pytest.approx(210.0)
        assert record.realized_pnl == pytest.approx(190.0)
        assert record.r_multiple == pytest.approx(190.0 / 210.0, abs=0.001)

    def test_option_stop_fill_uses_contract_multiplier(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        decision = RiskDecision(
            symbol="SPY260616C00520000",
            side=Side.BUY,
            qty=2,
            entry_reference_price=3.50,
            stop_price=2.50,
            strategy_name="spy_options_reversion",
            reason="test option entry",
            order_type=OrderType.LIMIT,
            limit_price=3.50,
        )
        entry_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="opt-buy-1",
            symbol="SPY260616C00520000",
            requested_qty=2,
            filled_qty=2,
            avg_fill_price=3.55,
            raw_status="filled",
            message="filled 2 @ 3.55",
        )
        tl.log(tl.build_record(decision, entry_result, modeled_price=3.50))

        tl.log_stop_fill(
            symbol="SPY260616C00520000",
            strategy="spy_options_reversion",
            qty=2,
            avg_fill_price=2.50,
            order_id="opt-stop-1",
        )

        stop_record = tl.read_recent(1)[0]
        # PLAN 11.53: 1.05 of premium at risk (fill 3.55 → stop 2.50)
        # × 2 × 100. The stop filled below its trigger, so the loss is
        # slightly more than 1R — which is exactly what a stop-gap looks
        # like, and was previously hidden by the understated basis.
        assert stop_record["initial_risk_dollars"] == pytest.approx(210.0)
        assert stop_record["realized_pnl"] == pytest.approx(-210.0)
        assert stop_record["r_multiple"] == pytest.approx(-1.0)

    def test_stop_fill_records_slippage_against_intended_stop(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            entry_reference_price=100.0,
            stop_price=95.0,
            strategy_name="sma_crossover",
            reason="test entry",
            order_type=OrderType.MARKET,
        )
        entry_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="entry-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=100.0,
            raw_status="filled",
            message="filled",
        )
        tl.log(tl.build_record(decision, entry_result, modeled_price=100.0))

        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10,
            avg_fill_price=94.50,
            # Phase 2 stop benchmarking: broker stop_price is the
            # authoritative reference; caller (WS or recovery path)
            # passes it directly. Without it the row records
            # unavailable / unavailable.
            stop_price=95.0,
            order_id="stop-1",
        )

        stop_record = tl.read_recent(1)[0]
        # Phase 2: legacy columns NULL on new rows.
        assert stop_record["modeled_slippage_bps"] is None
        assert stop_record["realized_slippage_bps"] is None
        # Fill 94.50 below stop 95.0 on a sell → adverse fill.
        assert stop_record["slippage_signed_bps"] == pytest.approx(52.63, abs=0.01)
        assert stop_record["slippage_adverse_bps"] == pytest.approx(52.63, abs=0.01)
        assert stop_record["slippage_benchmark_kind"] == "active_stop_price"

    def test_stop_fill_records_negative_slippage_for_price_improvement(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            entry_reference_price=100.0,
            stop_price=95.0,
            strategy_name="sma_crossover",
            reason="test entry",
            order_type=OrderType.MARKET,
        )
        entry_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="entry-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=100.0,
            raw_status="filled",
            message="filled",
        )
        tl.log(tl.build_record(decision, entry_result, modeled_price=100.0))

        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10,
            avg_fill_price=95.50,
            stop_price=95.0,
            order_id="stop-1",
        )

        stop_record = tl.read_recent(1)[0]
        # Phase 2: legacy columns NULL on new rows.
        assert stop_record["modeled_slippage_bps"] is None
        assert stop_record["realized_slippage_bps"] is None
        # Fill 95.50 above stop 95.0 on a sell → price improvement.
        assert stop_record["slippage_signed_bps"] == pytest.approx(-52.63, abs=0.01)
        # Adverse-only column clamps to 0 for price improvement.
        assert stop_record["slippage_adverse_bps"] == pytest.approx(0.0)

    def test_as_dict_has_all_columns(self, sample_decision, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, sample_result)
        d = record.as_dict()
        assert set(d.keys()) == set(TRADE_CSV_COLUMNS)

    def test_no_fill_price_records_null_slippage(self, sample_decision):
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="x",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=None,
            raw_status="filled",
        )
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, result, modeled_price=150.0)
        # Phase 2: no fill price → no honest measurement.
        # Legacy columns NULL (dual-write removed); new columns
        # also NULL (no computed value).
        assert record.realized_slippage_bps is None
        assert record.modeled_slippage_bps is None
        assert record.slippage_signed_bps is None
        assert record.slippage_adverse_bps is None

    def test_read_trades_in_range(self, tmp_csv, sample_decision, sample_result):
        """Foundation §6.5: distinct order_ids → distinct rows; UPSERT
        on same order_id."""
        from dataclasses import replace
        tl = TradeLogger(path=tmp_csv)
        record = None
        for i in range(3):
            result = replace(sample_result, order_id=f"abc{i}")
            record = tl.build_record(sample_decision, result)
            tl.log(record)
        today = record.timestamp[:10]
        rows = tl.read_trades_in_range(today, today)
        assert len(rows) == 3
        # Out-of-range returns nothing
        assert tl.read_trades_in_range("1999-01-01", "1999-12-31") == []

    def test_read_recent(self, tmp_csv, sample_decision, sample_result):
        from dataclasses import replace
        tl = TradeLogger(path=tmp_csv)
        for i in range(5):
            result = replace(sample_result, order_id=f"abc{i}")
            record = tl.build_record(sample_decision, result)
            tl.log(record)
        recent = tl.read_recent(3)
        assert len(recent) == 3

    def test_close_and_reopen(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(sample_decision, sample_result)
        tl.log(record)
        tl.close()
        # After close, read_all should still work (reconnects lazily)
        tl2 = TradeLogger(path=tmp_csv)
        assert len(tl2.read_all()) == 1

    def test_existing_db_is_migrated_with_new_columns(self, tmp_csv):
        conn = sqlite3.connect(tmp_csv)
        conn.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                avg_fill_price REAL,
                order_id TEXT,
                strategy TEXT NOT NULL,
                reason TEXT NOT NULL,
                stop_price REAL,
                entry_reference_price REAL,
                modeled_slippage_bps REAL,
                realized_slippage_bps REAL,
                order_type TEXT,
                status TEXT NOT NULL,
                requested_qty REAL,
                filled_qty REAL
            )
            """
        )
        conn.commit()
        conn.close()

        tl = TradeLogger(path=tmp_csv)
        tl.read_all()

        conn = sqlite3.connect(tmp_csv)
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(trades)").fetchall()
        }
        conn.close()
        assert "initial_risk_dollars" in cols
        assert "r_multiple" in cols
        assert "exit_timestamp" in cols
        assert "position_id" in cols
        assert "position_type" in cols

    def test_position_id_backfill_populates_existing_rows(self, tmp_csv):
        """Pre-11.27 rows are backfilled to position_id = owner_key_for(symbol):
        equities keep symbol, OCC options collapse to the underlying."""
        conn = sqlite3.connect(tmp_csv)
        conn.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                avg_fill_price REAL,
                order_id TEXT,
                strategy TEXT NOT NULL,
                reason TEXT NOT NULL,
                stop_price REAL,
                entry_reference_price REAL,
                modeled_slippage_bps REAL,
                realized_slippage_bps REAL,
                order_type TEXT,
                status TEXT NOT NULL,
                requested_qty REAL,
                filled_qty REAL
            )
            """
        )
        # Seed two legacy rows that pre-date PR 11.27.
        conn.executemany(
            "INSERT INTO trades (timestamp, symbol, side, qty, strategy, reason, "
            "status) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("2026-04-01T00:00:00Z", "AAPL", "buy", 10, "sma_crossover", "entry", "filled"),
                ("2026-04-02T00:00:00Z", "SPY260516C00520000", "buy", 1, "spy_options_reversion", "entry", "filled"),
            ],
        )
        conn.commit()
        conn.close()

        # Opening the logger triggers the migration + backfill.
        TradeLogger(path=tmp_csv).read_all()

        conn = sqlite3.connect(tmp_csv)
        rows = conn.execute(
            "SELECT symbol, position_id, position_type FROM trades ORDER BY id"
        ).fetchall()
        conn.close()
        # OCC option rows get normalized to the underlying ticker so the
        # stored position_id matches engine.positions.owner_key_for().
        assert rows == [
            ("AAPL", "AAPL", "single_leg"),
            ("SPY260516C00520000", "SPY", "single_leg"),
        ]

    def test_position_id_backfill_is_idempotent(self, tmp_csv, sample_decision, sample_result):
        """Re-opening an already-migrated DB must not overwrite explicit position_ids."""
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(sample_decision, sample_result)
        tl.log(record)
        tl.close()

        # Hand-edit one row to a UUID (simulates a future spread write).
        conn = sqlite3.connect(tmp_csv)
        conn.execute(
            "UPDATE trades SET position_id = 'deadbeef', position_type = 'spread' WHERE id = 1"
        )
        conn.commit()
        conn.close()

        # Re-open: backfill must NOT touch the row we hand-edited.
        TradeLogger(path=tmp_csv).read_all()

        conn = sqlite3.connect(tmp_csv)
        row = conn.execute(
            "SELECT position_id, position_type FROM trades WHERE id = 1"
        ).fetchone()
        conn.close()
        assert row == ("deadbeef", "spread")

    def test_new_record_writes_position_id_and_type(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(sample_decision, sample_result)
        tl.log(record)
        tl.close()

        conn = sqlite3.connect(tmp_csv)
        row = conn.execute(
            "SELECT position_id, position_type FROM trades WHERE id = 1"
        ).fetchone()
        conn.close()
        # sample_decision uses an equity ticker, so owner_key == symbol.
        assert row == (sample_decision.symbol, "single_leg")

    def test_option_record_writes_underlying_as_position_id(self, tmp_csv):
        """OCC option fills must store position_id = underlying ticker."""
        # build a synthetic option entry via log_external_close, which
        # exercises the same owner_key_for() normalization path.
        tl = TradeLogger(path=tmp_csv)
        tl.log_external_close(
            symbol="SPY260516C00520000",
            strategy="spy_options_reversion",
            reason="test_synthetic",
        )
        tl.close()

        conn = sqlite3.connect(tmp_csv)
        row = conn.execute(
            "SELECT symbol, position_id, position_type FROM trades WHERE id = 1"
        ).fetchone()
        conn.close()
        assert row == ("SPY260516C00520000", "SPY", "single_leg")


# ── TestSpreadLogging (11.29) ───────────────────────────────────────────────


class TestSpreadLogging:
    _SHORT = "SPY260618P00689000"
    _LONG = "SPY260618P00674000"

    def test_open_writes_one_row_per_leg(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, order_id="combo-1", opening=True,
        )
        rows = tl.read_all()
        assert len(rows) == 2
        assert all(r["position_id"] == "uuid-1" for r in rows)
        assert all(r["position_type"] == "spread" for r in rows)
        by_symbol = {r["symbol"]: r for r in rows}
        # Short leg sold to open, carries the net credit; long leg bought, 0.0.
        assert by_symbol[self._SHORT]["side"] == "sell"
        assert by_symbol[self._SHORT]["avg_fill_price"] == pytest.approx(2.54)
        assert by_symbol[self._LONG]["side"] == "buy"
        assert by_symbol[self._LONG]["avg_fill_price"] == pytest.approx(0.0)

    def test_close_reverses_leg_sides(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.10, order_id="combo-close", opening=False,
        )
        rows = tl.read_all()
        by_symbol = {r["symbol"]: r for r in rows}
        assert by_symbol[self._SHORT]["side"] == "buy"   # bought back to close
        assert by_symbol[self._LONG]["side"] == "sell"   # long leg sold

    def test_a_market_fallback_close_records_no_execution_quality_reading(
        self, tmp_csv
    ):
        """Review follow-up (PR #103): a market-fallback close fill has no
        submitted limit, so it must not be filed under `combo_limit`.

        `combo_limit` is defined as "spread fill vs the combo limit we
        submitted" and lives in EXECUTION_QUALITY_KINDS, which feeds the
        calibration set. Carrying the last walk rung forward would put a
        market fill's slippage into that family against a price nobody
        submitted.
        """
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-mkt", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=6.10, order_id="combo-mkt", opening=False,
            submitted_limit_price=None,
        )
        short = next(r for r in tl.read_all() if r["symbol"] == self._SHORT)
        assert short["slippage_benchmark_kind"] == "unavailable"
        assert short["slippage_measurement_quality"] == "unavailable"
        assert short["slippage_benchmark_price"] in (None, "")

    def test_a_limit_close_still_records_combo_limit(self, tmp_csv):
        """The contrast case — without this, the test above would pass on a
        logger that had simply stopped measuring spreads entirely."""
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-lim", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=6.10, order_id="combo-lim", opening=False,
            submitted_limit_price=6.00,
        )
        short = next(r for r in tl.read_all() if r["symbol"] == self._SHORT)
        assert short["slippage_benchmark_kind"] == "combo_limit"

    def test_open_writes_status_filled(self, tmp_csv):
        """Spread opens are atomic — always written as status='filled'.

        The is_full_close parameter doesn't apply to opens; the logger
        treats opening=True as always-full regardless.
        """
        tl = TradeLogger(path=tmp_csv)
        # Even with is_full_close=False, opens are still 'filled'.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
            is_full_close=False,
        )
        rows = tl.read_all()
        assert all(r["status"] == "filled" for r in rows)

    def test_partial_close_writes_status_partial(self, tmp_csv):
        """PR #56 R4: a partial close (is_full_close=False) writes
        status='partial' so restart restoration via the R3 filter
        correctly does NOT count it as a completed round trip."""
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, order_id="combo-open", opening=True,
        )
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.10, order_id="combo-partial", opening=False,
            realized_pnl=-150.0,
            is_full_close=False,
        )
        rows = tl.read_all()
        # Open rows are 'filled' (atomic). Close rows are 'partial'.
        statuses = {(r["side"], r["status"]) for r in rows[2:]}  # close rows
        assert statuses == {("buy", "partial"), ("sell", "partial")}

    def test_partial_spread_close_reconstructs_with_residual_qty(self, tmp_csv):
        """PR #56 R5: a 2-contract spread with a 1-contract partial
        close must reconstruct as OPEN with residual qty=1, not as
        empty (dropped) or full qty=2.

        Reviewer's smoking-gun: prior to R5, read_open_spread_positions
        treated any exit_timestamp as fully closed, dropping the
        spread entirely. The remaining 1 contract was orphaned at
        the broker.
        """
        tl = TradeLogger(path=tmp_csv)
        # Open a 2-contract spread.
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=2, net_price=2.50, opening=True,
        )
        # Partial close — 1 contract.
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.20, opening=False,
            realized_pnl=-130.0,
            is_full_close=False,
        )
        # Restart: read_open_spread_positions must return the spread
        # with residual qty=1, not [].
        opens = tl.read_open_spread_positions()
        assert len(opens) == 1
        rec = opens[0]
        assert rec["position_id"] == "uuid-X"
        assert rec["qty"] == pytest.approx(1.0)
        assert rec["net_credit"] == pytest.approx(2.50)
        assert set(rec["leg_symbols"]) == {self._SHORT, self._LONG}

    def test_full_spread_close_still_reconstructs_as_closed(self, tmp_csv):
        """The R5 fix must NOT break the existing full-close path.
        A status='filled' close row drops the spread from open positions.
        """
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=2, net_price=2.50, opening=True,
        )
        # Full close — status='filled' (default is_full_close=True).
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=2, net_price=1.10, opening=False,
            realized_pnl=-280.0,
        )
        assert tl.read_open_spread_positions() == []

    def test_partial_then_full_close_reconstructs_as_closed(self, tmp_csv):
        """Partial → eventual full close: spread is fully closed.
        The full-close row's status='filled' marks the position closed
        regardless of any preceding partial rows."""
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=2, net_price=2.50, opening=True,
        )
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.20, opening=False,
            realized_pnl=-130.0,
            is_full_close=False,
        )
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.10, opening=False,
            realized_pnl=-140.0,
            is_full_close=True,
        )
        assert tl.read_open_spread_positions() == []

    def test_partial_spread_close_does_not_inflate_restart_trade_count(self, tmp_csv):
        """Full end-to-end R4 invariant: a partial spread close logged
        via log_spread_fill must NOT count as a completed round trip
        when the trade log is replayed at restart. Matches the live
        allocator's is_full_close=False semantic.

        This is the smoking-gun integration test for the R4 fix:
        before R4, partial spread closes silently wrote status='filled'
        and restart would mis-count them.
        """
        tl = TradeLogger(path=tmp_csv)
        # Open the spread.
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=2, net_price=2.50, opening=True,
        )
        # Partial close — half the contracts.
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.20, opening=False,
            realized_pnl=-130.0,
            is_full_close=False,
        )
        # Restart: read back the summary.
        summary = tl.read_strategy_realized_pnl_summary(["credit_spread"])
        # P&L of the partial IS counted (dollar math is honest).
        assert summary["credit_spread"]["realized_pnl"] == pytest.approx(-130.0)
        # But trade_count is NOT incremented — round trip incomplete.
        assert summary["credit_spread"]["trade_count"] == 0
        assert summary["credit_spread"]["seen_position_uids"] == []

        # The residual eventually fully closes — NOW the round trip counts.
        tl.log_spread_fill(
            position_id="uuid-X", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.10, opening=False,
            realized_pnl=-120.0,
            is_full_close=True,
        )
        summary = tl.read_strategy_realized_pnl_summary(["credit_spread"])
        assert summary["credit_spread"]["realized_pnl"] == pytest.approx(-250.0)
        assert summary["credit_spread"]["trade_count"] == 1
        assert summary["credit_spread"]["seen_position_uids"] == ["uuid-X"]

    def test_spread_legs_excluded_from_single_leg_owner_views(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
        )
        # The long-leg row is side='buy' — without the position_type filter
        # it would be mistaken for a standalone open single-leg position.
        assert tl.read_all_open_owners() == {}
        assert tl.read_owner_for_symbol(self._LONG) is None
        assert tl.read_owner_for_symbol(self._SHORT) is None

    def test_read_open_spread_positions_reconstructs_open_spread(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=2, net_price=2.54, opening=True,
        )
        opens = tl.read_open_spread_positions()
        assert len(opens) == 1
        rec = opens[0]
        assert rec["position_id"] == "uuid-1"
        assert rec["strategy"] == "credit_spread"
        assert set(rec["leg_symbols"]) == {self._SHORT, self._LONG}
        assert rec["net_credit"] == pytest.approx(2.54)
        assert rec["qty"] == pytest.approx(2)

    def test_closed_spread_is_not_returned(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
        )
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.10, opening=False,
        )
        assert tl.read_open_spread_positions() == []

    def test_multiple_open_spreads_returned_separately(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
        )
        tl.log_spread_fill(
            position_id="uuid-2", strategy="credit_spread",
            short_occ="QQQ260618P00689000", long_occ="QQQ260618P00674000",
            qty=1, net_price=2.10, opening=True,
        )
        opens = tl.read_open_spread_positions()
        assert {r["position_id"] for r in opens} == {"uuid-1", "uuid-2"}

    def test_close_realized_pnl_rides_short_leg_row(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
        )
        # Close at a $0.80 debit → realized = (2.54 − 0.80) × 1 × 100 = 174.0.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=0.80, opening=False, realized_pnl=174.0,
        )
        rows = tl.read_all()
        close_rows = [r for r in rows if r["reason"] == "spread exit"]
        # realized_pnl lands on exactly one row — the short leg (side='buy').
        with_pnl = [r for r in close_rows if r["realized_pnl"] is not None]
        assert len(with_pnl) == 1
        assert with_pnl[0]["side"] == "buy"
        assert with_pnl[0]["realized_pnl"] == pytest.approx(174.0)

    def test_open_credit_slippage_is_logged_on_short_leg(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.50, order_id="combo-1", opening=True,
            submitted_limit_price=-1.45,
        )
        rows = tl.read_all()
        by_symbol = {r["symbol"]: r for r in rows}

        # Phase 2: legacy `realized_slippage_bps` is NULL on new rows
        # (dual-write removed). Slippage now lives in
        # `slippage_signed_bps` (and adverse-clamped in
        # `slippage_adverse_bps`).
        assert by_symbol[self._SHORT]["entry_reference_price"] == pytest.approx(1.45)
        assert by_symbol[self._SHORT]["realized_slippage_bps"] is None
        assert by_symbol[self._SHORT]["slippage_signed_bps"] == pytest.approx(-344.83)
        # Opening credit 1.50 above limit 1.45 → price improvement, adverse = 0.
        assert by_symbol[self._SHORT]["slippage_adverse_bps"] == pytest.approx(0.0)
        # Long leg: structural NULL on both legacy and new columns.
        assert by_symbol[self._LONG]["realized_slippage_bps"] is None
        assert by_symbol[self._LONG]["modeled_slippage_bps"] is None
        assert by_symbol[self._LONG]["slippage_signed_bps"] is None
        assert by_symbol[self._LONG]["slippage_adverse_bps"] is None

    def test_close_debit_slippage_is_logged_on_short_leg(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=0.63, order_id="combo-close",
            opening=False, realized_pnl=82.0, submitted_limit_price=0.60,
        )
        rows = tl.read_all()
        by_symbol = {r["symbol"]: r for r in rows}

        assert by_symbol[self._SHORT]["entry_reference_price"] == pytest.approx(0.60)
        # Phase 2: legacy NULL; new taxonomy carries the measurement.
        assert by_symbol[self._SHORT]["realized_slippage_bps"] is None
        assert by_symbol[self._SHORT]["slippage_signed_bps"] == pytest.approx(500.0)
        assert by_symbol[self._SHORT]["slippage_adverse_bps"] == pytest.approx(500.0)
        # Long leg: structural NULL across both column families.
        assert by_symbol[self._LONG]["realized_slippage_bps"] is None
        assert by_symbol[self._LONG]["slippage_signed_bps"] is None

    def test_realized_pnl_summary_rolls_in_credit_spread_pnl(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
        )
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=0.80, opening=False, realized_pnl=174.0,
        )
        summary = tl.read_strategy_realized_pnl_summary(["credit_spread"])
        # Counted once (not double-counted across the two close-leg rows),
        # and the spread open rows contribute nothing.
        assert summary["credit_spread"]["realized_pnl"] == pytest.approx(174.0)
        assert summary["credit_spread"]["hwm"] == pytest.approx(174.0)

    def test_realized_pnl_summary_reflects_a_losing_spread(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=1.45, opening=True,
        )
        # Closed at a $3.00 debit → realized = (1.45 − 3.00) × 100 = −155.0.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=3.00, opening=False, realized_pnl=-155.0,
        )
        summary = tl.read_strategy_realized_pnl_summary(["credit_spread"])
        assert summary["credit_spread"]["realized_pnl"] == pytest.approx(-155.0)
        # P&L never went positive — HWM stays at the 0.0 baseline.
        assert summary["credit_spread"]["hwm"] == pytest.approx(0.0)


class TestSpreadRMultiple:
    """log_spread_fill must populate initial_risk_dollars + r_multiple so
    the EdgeAssessor (strategies/health/edge.py:413) can count credit-spread
    closes as R-multiple-bearing trades. Per design §5.1, R-multiple is the
    primary verdict input; before this wiring spreads were structurally
    excluded from the Edge sample.
    """

    _SHORT = "SPY260618P00689000"
    _LONG = "SPY260618P00674000"

    def test_open_writes_initial_risk_dollars_on_short_leg(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        # Bull put: width=15, net_credit=2.54/sh → max_loss/contract = $1246;
        # one contract → $1246 initial_risk_dollars.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
            initial_risk_dollars=1246.0,
        )
        rows = tl.read_all()
        by_symbol = {r["symbol"]: r for r in rows}
        assert by_symbol[self._SHORT]["initial_risk_dollars"] == pytest.approx(1246.0)
        assert by_symbol[self._LONG]["initial_risk_dollars"] is None
        # No r_multiple on open rows — it is meaningful only on closes.
        assert by_symbol[self._SHORT]["r_multiple"] is None
        assert by_symbol[self._LONG]["r_multiple"] is None

    def test_close_computes_r_multiple_from_caller_supplied_basis(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        # Close at $0.80 debit → realized = (2.54−0.80)×100 = 174.0.
        # r_multiple = 174.0 / 1246.0 ≈ 0.1397.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=0.80, opening=False,
            realized_pnl=174.0, initial_risk_dollars=1246.0,
        )
        rows = tl.read_all()
        by_symbol = {r["symbol"]: r for r in rows}
        assert by_symbol[self._SHORT]["initial_risk_dollars"] == pytest.approx(1246.0)
        assert by_symbol[self._SHORT]["r_multiple"] == pytest.approx(174.0 / 1246.0)
        # Long leg stays clean.
        assert by_symbol[self._LONG]["initial_risk_dollars"] is None
        assert by_symbol[self._LONG]["r_multiple"] is None

    def test_close_falls_back_to_open_row_basis(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
            initial_risk_dollars=1246.0,
        )
        # Caller omits initial_risk_dollars (external-close path) — logger
        # must look up the open row's stored basis to compute r_multiple.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=0.80, opening=False, realized_pnl=174.0,
        )
        rows = tl.read_all()
        close_short = next(
            r for r in rows
            if r["symbol"] == self._SHORT and r["realized_pnl"] is not None
        )
        assert close_short["initial_risk_dollars"] == pytest.approx(1246.0)
        assert close_short["r_multiple"] == pytest.approx(174.0 / 1246.0)

    def test_close_with_no_realized_pnl_leaves_r_multiple_null(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        # external_close_detected path: realized_pnl unknown, basis known.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=0.0, opening=False,
            realized_pnl=None, initial_risk_dollars=1246.0,
            reason="external_close_detected",
        )
        rows = tl.read_all()
        by_symbol = {r["symbol"]: r for r in rows}
        # Basis recorded for audit, r_multiple stays NULL.
        assert by_symbol[self._SHORT]["initial_risk_dollars"] == pytest.approx(1246.0)
        assert by_symbol[self._SHORT]["r_multiple"] is None

    def test_close_with_no_basis_available_leaves_r_multiple_null(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        # Legacy/pre-fix open row had no initial_risk_dollars.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=2.54, opening=True,
        )
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=0.80, opening=False, realized_pnl=174.0,
        )
        # Close still writes — design §5.1 tolerates NULL r_multiple and the
        # dollar P&L still flows to the allocator's HWM/drawdown gate.
        close_short = next(
            r for r in tl.read_all()
            if r["symbol"] == self._SHORT and r["realized_pnl"] is not None
        )
        assert close_short["initial_risk_dollars"] is None
        assert close_short["r_multiple"] is None
        assert close_short["realized_pnl"] == pytest.approx(174.0)

    def test_close_handles_zero_basis_without_division_error(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        # Defensive: a degenerate (width − net_credit) ≤ 0 must not raise.
        tl.log_spread_fill(
            position_id="uuid-1", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=1, net_price=0.80, opening=False,
            realized_pnl=174.0, initial_risk_dollars=0.0,
        )
        close_short = next(
            r for r in tl.read_all()
            if r["symbol"] == self._SHORT and r["realized_pnl"] is not None
        )
        # Zero basis is treated as "no basis" — r_multiple stays NULL and
        # the column is not coerced to a spurious value.
        assert close_short["initial_risk_dollars"] is None
        assert close_short["r_multiple"] is None

    def test_multi_contract_basis_scales_with_qty(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        # 5 contracts × $1246 max loss = $6230 basis; realized = 5×174 = $870;
        # r_multiple = 870 / 6230 ≈ 0.1397 — identical to single-contract case
        # (R-multiple is sizing-invariant per design §5.1).
        tl.log_spread_fill(
            position_id="uuid-5", strategy="credit_spread",
            short_occ=self._SHORT, long_occ=self._LONG,
            qty=5, net_price=0.80, opening=False,
            realized_pnl=870.0, initial_risk_dollars=6230.0,
        )
        close_short = next(
            r for r in tl.read_all()
            if r["symbol"] == self._SHORT and r["realized_pnl"] is not None
        )
        assert close_short["r_multiple"] == pytest.approx(870.0 / 6230.0)
        assert close_short["r_multiple"] == pytest.approx(174.0 / 1246.0)


class TestBuildRecordSlippageSemantics:
    """`build_record` now distinguishes "honest measurement" from "no
    benchmark available." When the engine cannot provide an arrival
    price (Issue A: recovered-entry-context), both slippage columns
    must be NULL so the L2 Health check naturally excludes the row.
    When the engine does provide an arrival price (Issue B: equity
    entries), the realized bps measures fill-vs-arrival, the canonical
    execution-quality benchmark per industry TCA practice.
    """

    def test_record_slippage_false_writes_null_on_both_columns(
        self, tmp_csv, sample_decision, sample_result,
    ):
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(
            sample_decision, sample_result,
            modeled_price=150.0,        # would normally drive slippage
            record_slippage=False,      # but caller knows there's no honest benchmark
        )
        tl.log(record)
        conn = sqlite3.connect(tmp_csv)
        row = conn.execute(
            "SELECT modeled_slippage_bps, realized_slippage_bps "
            "FROM trades WHERE symbol = ?",
            (sample_decision.symbol,),
        ).fetchone()
        assert row[0] is None  # modeled_slippage_bps
        assert row[1] is None  # realized_slippage_bps

    def test_record_slippage_default_true_writes_new_columns_legacy_null(
        self, tmp_csv, sample_decision, sample_result,
    ):
        """Phase 2: default `record_slippage=True` on a MARKET entry
        populates the new taxonomy columns; legacy columns stay NULL
        (dual-write removed)."""
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(
            sample_decision, sample_result, modeled_price=150.0,
        )
        tl.log(record)
        conn = sqlite3.connect(tmp_csv)
        row = conn.execute(
            "SELECT modeled_slippage_bps, realized_slippage_bps, "
            "slippage_signed_bps, slippage_adverse_bps "
            "FROM trades WHERE symbol = ?",
            (sample_decision.symbol,),
        ).fetchone()
        assert row[0] is None  # modeled_slippage_bps (legacy)
        assert row[1] is None  # realized_slippage_bps (legacy)
        assert row[2] is not None  # slippage_signed_bps
        assert row[3] is not None  # slippage_adverse_bps

    def test_signed_bps_computed_against_arrival_price_not_signal_close(
        self, tmp_csv,
    ):
        """The Issue B fix (now living in the new taxonomy column):
        `slippage_signed_bps` measures fill-vs-arrival, not
        fill-vs-signal-close. Same fill price, different
        `modeled_price` → different signed bps."""
        from execution.broker import OrderResult, OrderStatus
        from risk.manager import RiskDecision, Side
        from strategies.base import OrderType

        decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            entry_reference_price=100.0,   # decision-time bar close
            stop_price=95.0,
            strategy_name="t",
            reason="t",
            order_type=OrderType.MARKET,
        )
        result = OrderResult(
            status=OrderStatus.FILLED, order_id="x", symbol="AAPL",
            requested_qty=10, filled_qty=10,
            avg_fill_price=101.0,           # actual fill price
            raw_status="filled", message="ok",
        )
        tl = TradeLogger(path=tmp_csv)
        # Case 1: arrival price IS the signal close → signed bps
        # reflects pure execution slippage from $100 to $101 = 100 bps.
        rec_old = tl.build_record(decision, result, modeled_price=100.0)
        # Case 2: arrival price is $100.95 (price drifted toward the
        # fill before submission) → signed bps shrinks because the
        # arrival benchmark already absorbed most of the drift; only
        # ($101.00 - $100.95) / $100.95 × 10000 ≈ 5 bps left as
        # execution slippage.
        rec_new = tl.build_record(decision, result, modeled_price=100.95)
        assert rec_old.slippage_signed_bps == pytest.approx(100.0, rel=1e-3)
        assert rec_new.slippage_signed_bps == pytest.approx(
            (1.00 - 0.95) / 100.95 * 10_000, rel=1e-2,
        )
        # Legacy columns are NULL across the board (Phase 2 dual-write
        # removed).
        assert rec_old.realized_slippage_bps is None
        assert rec_new.realized_slippage_bps is None

    def test_limit_order_writes_null_on_both_slippage_columns(self, tmp_csv):
        """Reviewer P2 #2: LIMIT orders must not record execution-slippage
        bps against the arrival price. A buy limit at $100 filled at $95
        is good execution (or alpha capture, depending on framing) — the
        arrival-price formula would mark it as -500 bps and the L2
        check's abs() wrapper would flag it as BROKEN. NULL on both
        columns is the honest answer; the IS NOT NULL filter on the L2
        query naturally excludes them."""
        from execution.broker import OrderResult, OrderStatus
        from risk.manager import RiskDecision, Side
        from strategies.base import OrderType

        decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            entry_reference_price=100.0,
            stop_price=95.0,
            strategy_name="rsi_reversion",
            reason="rsi limit entry",
            order_type=OrderType.LIMIT,
            limit_price=100.0,
        )
        result = OrderResult(
            status=OrderStatus.FILLED, order_id="x", symbol="AAPL",
            requested_qty=10, filled_qty=10,
            avg_fill_price=95.0,   # filled well below limit — good fill
            raw_status="filled", message="ok",
        )
        tl = TradeLogger(path=tmp_csv)
        # Even with arrival-price-style modeled_price supplied, LIMIT is
        # gated and writes NULL on both columns.
        record = tl.build_record(decision, result, modeled_price=100.05)
        assert record.modeled_slippage_bps is None
        assert record.realized_slippage_bps is None

    def test_market_order_with_arrival_price_still_records(self, tmp_csv):
        """Sanity: MARKET orders with arrival-price modeled_price continue
        to record real slippage (regression guard against over-broadly
        gating the LIMIT carve-out). Phase 2: measurement now lives in
        the new taxonomy columns; legacy stays NULL."""
        from execution.broker import OrderResult, OrderStatus
        from risk.manager import RiskDecision, Side
        from strategies.base import OrderType

        decision = RiskDecision(
            symbol="AAPL", side=Side.BUY, qty=10,
            entry_reference_price=100.0, stop_price=95.0,
            strategy_name="t", reason="t",
            order_type=OrderType.MARKET,
        )
        result = OrderResult(
            status=OrderStatus.FILLED, order_id="x", symbol="AAPL",
            requested_qty=10, filled_qty=10, avg_fill_price=100.10,
            raw_status="filled", message="ok",
        )
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(decision, result, modeled_price=100.05)
        # Legacy columns NULL (Phase 2 dual-write removed).
        assert record.modeled_slippage_bps is None
        assert record.realized_slippage_bps is None
        # New taxonomy carries the measurement.
        assert record.slippage_signed_bps is not None
        assert record.slippage_adverse_bps is not None
        # Measured against arrival ($100.05), not decision ($100).
        expected = (100.10 - 100.05) / 100.05 * 10_000
        assert record.slippage_signed_bps == pytest.approx(expected, rel=1e-2)

    def test_record_slippage_false_still_populates_risk_basis(
        self, tmp_csv, sample_decision, sample_result,
    ):
        """record_slippage=False only suppresses slippage columns; the
        risk-basis fields (initial_stop_loss, initial_risk_per_share,
        initial_risk_dollars) still get populated so R-multiple math
        works on the close row."""
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(
            sample_decision, sample_result,
            modeled_price=150.0, record_slippage=False,
        )
        tl.log(record)
        conn = sqlite3.connect(tmp_csv)
        row = conn.execute(
            "SELECT initial_stop_loss, initial_risk_per_share, initial_risk_dollars "
            "FROM trades WHERE symbol = ?",
            (sample_decision.symbol,),
        ).fetchone()
        assert row[0] is not None
        assert row[1] is not None
        assert row[2] is not None


class TestMlegRealizedSlippageBps:
    def test_open_credit_improvement_is_negative(self):
        assert mleg_realized_slippage_bps(
            opening=True,
            submitted_limit_price=-1.45,
            actual_net_price=-1.50,
        ) == pytest.approx(-344.83)

    def test_open_credit_shortfall_is_positive(self):
        assert mleg_realized_slippage_bps(
            opening=True,
            submitted_limit_price=-1.45,
            actual_net_price=-1.40,
        ) == pytest.approx(344.83)

    def test_close_debit_improvement_is_negative(self):
        assert mleg_realized_slippage_bps(
            opening=False,
            submitted_limit_price=0.60,
            actual_net_price=0.58,
        ) == pytest.approx(-333.33)

    def test_close_debit_overpay_is_positive(self):
        assert mleg_realized_slippage_bps(
            opening=False,
            submitted_limit_price=0.60,
            actual_net_price=0.63,
        ) == pytest.approx(500.0)


# ── TestSlippageUnificationSchema ──────────────────────────────────────────


class TestSlippageUnificationSchema:
    """
    Phase 1 of slippage unification — see docs/slippage_unification_design.md.

    Asserts that the additive schema columns exist on freshly created DBs
    and that the migration path (ALTER TABLE) is idempotent for pre-existing
    DBs that lack the columns.
    """

    _NEW_COLUMNS = [
        "slippage_benchmark_price",
        "slippage_benchmark_kind",
        "slippage_benchmark_timestamp",
        "slippage_measurement_quality",
        "slippage_signed_bps",
        "slippage_adverse_bps",
        "stop_trigger_price",
    ]

    def _column_set(self, db_path: str) -> set[str]:
        conn = sqlite3.connect(db_path)
        try:
            return {row[1] for row in conn.execute("PRAGMA table_info(trades)").fetchall()}
        finally:
            conn.close()

    def test_fresh_db_has_all_slippage_columns(self, tmp_csv):
        TradeLogger(path=tmp_csv)._ensure_db()
        columns = self._column_set(tmp_csv)
        for col in self._NEW_COLUMNS:
            assert col in columns, f"missing column on fresh DB: {col}"

    def test_migration_adds_columns_to_legacy_db(self, tmp_path):
        """A pre-existing DB without the new columns gets them via ALTER.

        Foundation §6.5: the legacy seed schema must include the
        columns the foundation's partial UNIQUE index references
        (order_id, position_type) so the index creation succeeds.
        Earlier slippage-Phase-1 tests used a stripped-down schema
        without these; the foundation widens the migration's
        cross-column dependency surface.
        """
        db_path = str(tmp_path / "legacy.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                qty REAL NOT NULL,
                strategy TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                order_id TEXT,
                position_type TEXT
            )
            """
        )
        conn.commit()
        conn.close()

        before = self._column_set(db_path)
        for col in self._NEW_COLUMNS:
            assert col not in before, f"precondition: {col} should be absent"

        TradeLogger(path=db_path)._ensure_db()

        after = self._column_set(db_path)
        for col in self._NEW_COLUMNS:
            assert col in after, f"migration failed to add: {col}"

    def test_ensure_db_is_idempotent(self, tmp_csv):
        """Running _ensure_db twice on the same DB must not error."""
        TradeLogger(path=tmp_csv)._ensure_db()
        # Re-open and re-bootstrap — proves ALTER guards work.
        TradeLogger(path=tmp_csv)._ensure_db()
        columns = self._column_set(tmp_csv)
        for col in self._NEW_COLUMNS:
            assert col in columns

    def test_bootstrap_failure_does_not_cache_poisoned_connection(self, tmp_path):
        """Defect 3 fix — if any step of _ensure_db raises after the
        connection is opened, the connection must NOT be cached.
        The next call to _ensure_db should retry from scratch and
        succeed (assuming the underlying issue is gone).

        Simulates failure by patching a downstream DDL import to raise
        once, then succeed on retry."""
        from unittest.mock import patch

        db_path = str(tmp_path / "boot_fail.db")
        tl = TradeLogger(path=db_path)

        # Inject failure at one of the deferred-import DDL constants.
        # The real import path is exercised inside _ensure_db.
        call_count = {"n": 0}
        real_import = __import__

        def flaky_import(name, *args, **kwargs):
            if name == "engine.lifecycle" and call_count["n"] == 0:
                call_count["n"] += 1
                raise RuntimeError("simulated mid-bootstrap failure")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=flaky_import):
            with pytest.raises(RuntimeError, match="simulated"):
                tl._ensure_db()

        # Cached connection must NOT have been published after the failure.
        assert tl._conn is None, "poisoned connection cached after bootstrap failure"

        # Retry succeeds — proves the migration is idempotent across the
        # post-failure recovery path.
        conn = tl._ensure_db()
        assert conn is not None
        # All new columns are present after the successful retry.
        existing = {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
        for col in self._NEW_COLUMNS:
            assert col in existing

    def test_trade_record_dataclass_defaults_remain_none(self):
        """Direct dataclass construction without writer logic leaves new
        fields None. This guards against any future caller that builds a
        TradeRecord directly bypassing the writer-side default inference."""
        record = TradeRecord(
            position_type="single_leg",
            timestamp="2026-06-05T00:00:00+00:00",
            symbol="AAPL",
            side="buy",
            qty=1.0,
            avg_fill_price=100.0,
            order_id="x",
            strategy="s",
            reason="r",
            stop_price=0.0,
            entry_reference_price=100.0,
            modeled_slippage_bps=None,
            realized_slippage_bps=None,
            order_type="market",
            status="filled",
            requested_qty=1.0,
            filled_qty=1.0,
        )
        for col in self._NEW_COLUMNS:
            assert getattr(record, col) is None


# ── TestBuildRecordSlippageContract ────────────────────────────────────────


class TestBuildRecordSlippageContract:
    """
    Phase 1 commit 5 of slippage unification — codepaths §1, §2 in
    docs/slippage_unification_design.md. build_record now populates the
    new taxonomy columns and accepts explicit benchmark_kind/quality
    parameters so callers can declare arrival vs fallback.
    """

    def test_market_entry_with_arrival_midpoint_writes_primary(
        self, sample_decision, sample_result
    ):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=150.0,
            benchmark_kind="arrival_midpoint",
        )
        assert record.slippage_benchmark_kind == "arrival_midpoint"
        assert record.slippage_measurement_quality == "primary"
        assert record.slippage_benchmark_price == pytest.approx(150.0)
        assert record.slippage_benchmark_timestamp is not None
        assert record.slippage_signed_bps is not None
        assert record.slippage_adverse_bps == max(0.0, record.slippage_signed_bps)

    def test_market_entry_with_fallback_close_writes_fallback_quality(
        self, sample_decision, sample_result
    ):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=150.0,
            benchmark_kind="fallback_latest_close",
        )
        assert record.slippage_benchmark_kind == "fallback_latest_close"
        assert record.slippage_measurement_quality == "fallback"

    def test_market_entry_without_benchmark_writes_unavailable(
        self, sample_decision, sample_result
    ):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=None,
        )
        assert record.slippage_benchmark_kind == "unavailable"
        assert record.slippage_measurement_quality == "unavailable"
        assert record.slippage_signed_bps is None
        assert record.slippage_adverse_bps is None

    def test_market_entry_without_benchmark_legacy_and_new_both_null(
        self, sample_decision, sample_result
    ):
        """Phase 2 reconciles the Phase 1 divergence. Pre-Phase 2 the
        legacy `realized_slippage_bps` column fell back to
        `decision.entry_reference_price` as a fabricated benchmark
        when `modeled_price` was None, while the new
        `slippage_signed_bps` honestly reported 'unavailable'.

        With Phase 1 dual-write removed, both column families now
        agree: when there's no honest benchmark, both legacy AND new
        columns are NULL. No more silent decision-price fallback."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=None,
        )
        # Legacy cols: NULL (dual-write removed, divergence reconciled).
        assert record.realized_slippage_bps is None
        assert record.modeled_slippage_bps is None
        # New cols: also NULL — honestly unavailable.
        assert record.slippage_signed_bps is None
        assert record.slippage_adverse_bps is None
        assert record.slippage_benchmark_kind == "unavailable"
        assert record.slippage_measurement_quality == "unavailable"

    def test_limit_entry_writes_limit_price_unavailable(self, sample_result):
        from risk.manager import RiskDecision
        decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            stop_price=145.0,
            entry_reference_price=150.0,
            strategy_name="rsi_reversion",
            reason="rsi oversold",
            order_type=OrderType.LIMIT,
            limit_price=149.50,
        )
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(decision, sample_result, modeled_price=150.0)
        assert record.slippage_benchmark_kind == "limit_price"
        assert record.slippage_measurement_quality == "unavailable"
        assert record.slippage_signed_bps is None
        assert record.slippage_adverse_bps is None
        # Legacy columns also NULL — preserves the LIMIT carve-out (8316e64).
        assert record.realized_slippage_bps is None

    def test_record_slippage_false_writes_unavailable(
        self, sample_decision, sample_result
    ):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=150.0,
            record_slippage=False,
        )
        assert record.slippage_benchmark_kind == "unavailable"
        assert record.slippage_measurement_quality == "unavailable"

    def test_measurement_quality_recovered_override(
        self, sample_decision, sample_result
    ):
        """Codepath §9 — suspect-order recovery tags rows quality='recovered'."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=150.0,
            benchmark_kind="arrival_midpoint",
            measurement_quality="recovered",
        )
        assert record.slippage_benchmark_kind == "arrival_midpoint"
        assert record.slippage_measurement_quality == "recovered"

    def test_legacy_columns_null_on_new_market_rows(
        self, sample_decision, sample_result
    ):
        """Phase 2 replacement for the Phase 1 parity invariant. New
        rows write NULL on the legacy columns regardless of whether
        the new taxonomy columns are populated."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=150.0,
            benchmark_kind="arrival_midpoint",
        )
        # New columns populated against modeled_price.
        assert record.slippage_signed_bps is not None
        assert record.slippage_adverse_bps is not None
        # Legacy columns NULL — dual-write removed.
        assert record.realized_slippage_bps is None
        assert record.modeled_slippage_bps is None


# ── TestBuildCloseRecordSlippageContract ───────────────────────────────────


class TestBuildCloseRecordSlippageContract:
    """
    Phase 1 commit 5 of slippage unification — codepaths §3, §7.
    build_close_record accepts benchmark_kind/quality so the fractional
    residual cleanup path can honestly declare 'unavailable' rather than
    inheriting the stop-fill slippage.
    """

    def _make_close_result(self):
        return OrderResult(
            status=OrderStatus.FILLED,
            order_id="exit-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=152.0,
            raw_status="filled",
        )

    def test_default_kind_is_unavailable_safe(self):
        """Defect 1 fix — caller that omits benchmark_kind gets a safe
        'unavailable' tag, never a fabricated 'arrival_midpoint'. The
        real exit caller in engine/trader.py declares fallback or
        unavailable per equity-vs-option as appropriate."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_close_record(
            self._make_close_result(),
            strategy_name="sma_crossover",
            modeled_price=151.50,
        )
        assert record.slippage_benchmark_kind == "unavailable"
        assert record.slippage_measurement_quality == "unavailable"
        assert record.slippage_signed_bps is None
        assert record.slippage_adverse_bps is None

    def test_explicit_arrival_midpoint_kind_writes_primary(self):
        """When the caller declares an honest arrival_midpoint benchmark,
        the row gets primary quality and the computed slippage."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_close_record(
            self._make_close_result(),
            strategy_name="sma_crossover",
            modeled_price=151.50,
            benchmark_kind="arrival_midpoint",
        )
        assert record.slippage_benchmark_kind == "arrival_midpoint"
        assert record.slippage_measurement_quality == "primary"
        assert record.slippage_benchmark_price == pytest.approx(151.50)
        assert record.slippage_signed_bps is not None

    def test_explicit_fallback_kind_writes_fallback_quality(self):
        """Defect 1 fix — equity exits using latest_close as a fallback
        proxy must be tagged fallback_latest_close / fallback."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_close_record(
            self._make_close_result(),
            strategy_name="sma_crossover",
            modeled_price=151.50,
            benchmark_kind="fallback_latest_close",
        )
        assert record.slippage_benchmark_kind == "fallback_latest_close"
        assert record.slippage_measurement_quality == "fallback"
        assert record.slippage_benchmark_price == pytest.approx(151.50)
        assert record.slippage_signed_bps is not None

    def test_unavailable_kind_writes_null_metrics(self):
        """Fractional residual cleanup contract — pass 'unavailable' to
        opt out of metric computation entirely."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_close_record(
            self._make_close_result(),
            strategy_name="sma_crossover",
            modeled_price=152.0,  # fill price itself — not a real benchmark
            benchmark_kind="unavailable",
            measurement_quality="unavailable",
        )
        assert record.slippage_benchmark_kind == "unavailable"
        assert record.slippage_measurement_quality == "unavailable"
        assert record.slippage_benchmark_price is None
        assert record.slippage_signed_bps is None
        assert record.slippage_adverse_bps is None

    def test_modeled_price_zero_forces_unavailable(self):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_close_record(
            self._make_close_result(),
            strategy_name="sma_crossover",
            modeled_price=0.0,
        )
        assert record.slippage_benchmark_kind == "unavailable"
        assert record.slippage_measurement_quality == "unavailable"
        assert record.slippage_signed_bps is None

    def test_explicit_unavailable_also_nulls_legacy_columns(self):
        """Defect 5 fix — when caller declares benchmark_kind='unavailable'
        (codepath §7 fractional residual cleanup), legacy columns must
        also be NULL. Without this, legacy realized_slippage_bps would
        carry a structural ~0 value computed against the fill price
        itself, while the new columns honestly say 'unavailable' —
        legacy and new would silently divergence on a 'no measurement'
        case."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_close_record(
            self._make_close_result(),
            strategy_name="sma_crossover",
            modeled_price=152.0,  # fill price; no honest benchmark
            benchmark_kind="unavailable",
            measurement_quality="unavailable",
        )
        assert record.slippage_benchmark_kind == "unavailable"
        assert record.slippage_signed_bps is None
        # Legacy columns also NULL — no fabricated value.
        assert record.realized_slippage_bps is None
        assert record.modeled_slippage_bps is None


# ── TestNoLegacyWritesOnNewRows ────────────────────────────────────────────


class TestNoLegacyWritesOnNewRows:
    """
    Phase 2 replacement for Phase 1's TestSlippageDualWriteParity.

    Phase 1 dual-wrote the new taxonomy columns alongside the legacy
    `realized_slippage_bps` / `modeled_slippage_bps` and pinned that
    both columns agreed where both were populated. Phase 2 retires the
    legacy writes entirely — consumers (health / risk / calibration /
    dashboard / pnl) now read the new columns directly. The invariant
    to pin is the new one: every writer codepath writes NULL on the
    legacy columns regardless of whether the new columns are populated.

    Codepaths covered (same set the dual-write parity tests covered):
      - Codepath §1 market entry
      - Codepath §3 discretionary market exit
      - Codepath §4/§5 stop fills (with broker stop_price)
      - Codepath §11 short leg of spread fills
      - Codepath §11 long leg of spread fills (structural-NULL guard:
        the long-leg row legacy column must not silently revert to
        the old 0.0 structural zero that the dashboard worked
        around with its avg_fill_price > 0 filter)
    """

    def _open_db(self, path: str):
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        return conn

    def test_market_entry_legacy_columns_null(
        self, tmp_csv, sample_decision, sample_result
    ):
        tl = TradeLogger(path=tmp_csv)
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=150.0,
            benchmark_kind="arrival_midpoint",
        )
        tl.log(record)
        conn = self._open_db(tmp_csv)
        try:
            row = dict(conn.execute(
                "SELECT realized_slippage_bps, modeled_slippage_bps, "
                "slippage_signed_bps, slippage_adverse_bps FROM trades "
                "WHERE reason = ? ORDER BY id DESC LIMIT 1",
                (sample_decision.reason,),
            ).fetchone())
        finally:
            conn.close()
        # Legacy NULL; new populated.
        assert row["realized_slippage_bps"] is None
        assert row["modeled_slippage_bps"] is None
        assert row["slippage_signed_bps"] is not None
        assert row["slippage_adverse_bps"] is not None

    def test_market_exit_legacy_columns_null(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id="exit-no-legacy-1",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=152.0,
            raw_status="filled",
        )
        record = tl.build_close_record(
            result,
            strategy_name="sma_crossover",
            modeled_price=151.50,
            benchmark_kind="arrival_midpoint",
        )
        tl.log(record)
        conn = self._open_db(tmp_csv)
        try:
            row = dict(conn.execute(
                "SELECT realized_slippage_bps, modeled_slippage_bps, "
                "slippage_signed_bps, slippage_adverse_bps FROM trades "
                "WHERE order_id = 'exit-no-legacy-1'"
            ).fetchone())
        finally:
            conn.close()
        assert row["realized_slippage_bps"] is None
        assert row["modeled_slippage_bps"] is None
        assert row["slippage_signed_bps"] is not None
        assert row["slippage_adverse_bps"] is not None

    def test_stop_fill_legacy_columns_null(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10.0,
            avg_fill_price=144.50,
            stop_price=145.00,
            order_id="stop-no-legacy-1",
        )
        conn = self._open_db(tmp_csv)
        try:
            row = dict(conn.execute(
                "SELECT realized_slippage_bps, modeled_slippage_bps, "
                "slippage_signed_bps, slippage_adverse_bps FROM trades "
                "WHERE order_id = 'stop-no-legacy-1'"
            ).fetchone())
        finally:
            conn.close()
        assert row["realized_slippage_bps"] is None
        assert row["modeled_slippage_bps"] is None
        assert row["slippage_signed_bps"] is not None
        assert row["slippage_adverse_bps"] is not None

    def test_spread_short_leg_legacy_columns_null(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="spread-no-legacy-1",
            strategy="credit_spread",
            short_occ="SPY260620P00510000",
            long_occ="SPY260620P00505000",
            qty=1.0,
            net_price=0.55,
            submitted_limit_price=0.60,
            opening=True,
            initial_risk_dollars=445.0,
        )
        conn = self._open_db(tmp_csv)
        try:
            row = dict(conn.execute(
                "SELECT realized_slippage_bps, modeled_slippage_bps, "
                "slippage_signed_bps, slippage_adverse_bps FROM trades "
                "WHERE side='sell' AND position_id='spread-no-legacy-1'"
            ).fetchone())
        finally:
            conn.close()
        assert row["realized_slippage_bps"] is None
        assert row["modeled_slippage_bps"] is None
        assert row["slippage_signed_bps"] is not None
        assert row["slippage_adverse_bps"] is not None

    def test_spread_long_leg_legacy_columns_null_no_structural_zero(
        self, tmp_csv,
    ):
        """Regression guard: pre-Phase 2 the long-leg row carried
        `realized_slippage_bps = 0.0` (a structural zero from the
        long leg being the non-economic side of the combo). The
        dashboard worked around this by filtering on
        `avg_fill_price > 0`. With Phase 2 the long-leg row must be
        NULL across both column families — both legacy AND new —
        so the structural-zero dilution can't creep back via the
        legacy column."""
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="spread-long-leg-null-1",
            strategy="credit_spread",
            short_occ="SPY260620P00510000",
            long_occ="SPY260620P00505000",
            qty=1.0,
            net_price=0.55,
            submitted_limit_price=0.60,
            opening=True,
            initial_risk_dollars=445.0,
        )
        conn = self._open_db(tmp_csv)
        try:
            row = dict(conn.execute(
                "SELECT realized_slippage_bps, modeled_slippage_bps, "
                "slippage_signed_bps, slippage_adverse_bps FROM trades "
                "WHERE side='buy' AND position_id='spread-long-leg-null-1'"
            ).fetchone())
        finally:
            conn.close()
        # Both column families NULL — no structural zero on the legacy
        # column that downstream consumers could mistake for a real
        # measurement.
        assert row["realized_slippage_bps"] is None
        assert row["modeled_slippage_bps"] is None
        assert row["slippage_signed_bps"] is None
        assert row["slippage_adverse_bps"] is None

    def test_adverse_is_signed_clamped_to_zero(self, tmp_csv):
        """Adverse_bps is max(0, signed_bps) on every row where signed is
        non-NULL. The clamp lives in writer code, not at read time."""
        tl = TradeLogger(path=tmp_csv)
        # Sell @ 145.50 vs stop 145.00 — favorable execution (signed < 0)
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10.0,
            avg_fill_price=145.50,
            stop_price=145.00,
            order_id="stop-favorable-1",
        )
        conn = self._open_db(tmp_csv)
        try:
            row = dict(conn.execute(
                "SELECT slippage_signed_bps, slippage_adverse_bps FROM trades "
                "WHERE order_id = 'stop-favorable-1'"
            ).fetchone())
        finally:
            conn.close()
        assert row["slippage_signed_bps"] < 0  # price improvement
        assert row["slippage_adverse_bps"] == 0.0


# ── TestExternalCloseAndRecoveredContextContract ───────────────────────────


class TestExternalCloseAndRecoveredContextContract:
    """
    Phase 1 commit 7 of slippage unification — codepaths §8, §12, §13.

    §8 — Recovered missing-entry-context row: build_record with
        record_slippage=False; quality='recovered' so consumers can
        isolate reconstruction rows.
    §12 — log_external_close: row writes 'unavailable' on the new
        columns and NULL on legacy columns (replacing the prior 0.0
        placeholder).
    §13 — Spread external close: log_spread_fill called without
        submitted_limit_price → 'unavailable' on both legs (covered
        by commit 6 tests already; one regression check here).
    """

    def test_log_external_close_writes_unavailable_and_null(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_external_close(
            symbol="AAPL",
            strategy="sma_crossover",
            reason="external_close_detected",
        )
        conn = sqlite3.connect(tmp_csv)
        conn.row_factory = sqlite3.Row
        try:
            row = dict(conn.execute(
                "SELECT * FROM trades ORDER BY id DESC LIMIT 1"
            ).fetchone())
        finally:
            conn.close()
        assert row["slippage_benchmark_kind"] == "unavailable"
        assert row["slippage_measurement_quality"] == "unavailable"
        assert row["slippage_signed_bps"] is None
        assert row["slippage_adverse_bps"] is None
        # Legacy column also moves to NULL — these rows never had real
        # measurements. Phase 2 consumer migration adapts to the change.
        assert row["realized_slippage_bps"] is None
        assert row["modeled_slippage_bps"] is None

    def test_recovered_entry_context_writes_quality_recovered(
        self, sample_decision, sample_result
    ):
        """Codepath §8 — when record_slippage=False AND caller passes
        measurement_quality='recovered', the row gets quality='recovered'
        with kind='unavailable' and NULL metrics."""
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(
            sample_decision,
            sample_result,
            modeled_price=150.0,
            record_slippage=False,
            measurement_quality="recovered",
        )
        assert record.slippage_benchmark_kind == "unavailable"
        assert record.slippage_measurement_quality == "recovered"
        assert record.slippage_signed_bps is None
        assert record.slippage_adverse_bps is None

    def test_spread_external_close_writes_unavailable(self, tmp_csv):
        """Codepath §13 — spread external close calls log_spread_fill
        without submitted_limit_price. Both legs get 'unavailable'."""
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="spread-ext-1",
            strategy="credit_spread",
            short_occ="SPY260620P00510000",
            long_occ="SPY260620P00505000",
            qty=1.0,
            net_price=0.0,
            opening=False,
            realized_pnl=None,
            reason="external_close_detected",
        )
        conn = sqlite3.connect(tmp_csv)
        conn.row_factory = sqlite3.Row
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT * FROM trades WHERE position_id='spread-ext-1'"
            )]
        finally:
            conn.close()
        assert len(rows) == 2
        for row in rows:
            assert row["slippage_benchmark_kind"] == "unavailable"
            assert row["slippage_measurement_quality"] == "unavailable"
            assert row["slippage_signed_bps"] is None
            assert row["slippage_adverse_bps"] is None


# ── TestOptionAndSpreadSlippageContract ────────────────────────────────────


class TestOptionAndSpreadSlippageContract:
    """
    Phase 1 commit 6 of slippage unification — codepaths §10, §11 in
    docs/slippage_unification_design.md.

    Codepath §10 (async single-leg option fill) routes through _log_entry
    with a LIMIT decision, so it inherits build_record's limit_price /
    unavailable contract automatically; this class pins that contract.

    Codepath §11 (spread fill) is bespoke — the short leg carries the
    economic combo_limit measurement; the long leg writes NULL on the
    new columns to distinguish structural zeros from real measurements.
    """

    def test_limit_option_entry_writes_limit_price_unavailable(self, sample_result):
        """Codepath §10 — single-leg options are LIMIT-typed and produce
        the same limit_price / unavailable shape as any other LIMIT entry."""
        from risk.manager import RiskDecision
        decision = RiskDecision(
            symbol="SPY260620C00520000",
            side=Side.BUY,
            qty=2,
            stop_price=0.01,
            entry_reference_price=10.00,
            strategy_name="spy_options_reversion",
            reason="rsi recovery",
            order_type=OrderType.LIMIT,
            limit_price=10.00,
        )
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(decision, sample_result, modeled_price=10.00)
        assert record.slippage_benchmark_kind == "limit_price"
        assert record.slippage_measurement_quality == "unavailable"
        assert record.slippage_signed_bps is None
        assert record.slippage_adverse_bps is None

    def test_spread_short_leg_writes_combo_limit_primary(self, tmp_csv):
        """Codepath §11 — short leg carries the economic combo_limit measurement."""
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="spread-1",
            strategy="credit_spread",
            short_occ="SPY260620P00510000",
            long_occ="SPY260620P00505000",
            qty=1.0,
            net_price=0.55,
            submitted_limit_price=0.60,
            opening=True,
            initial_risk_dollars=445.0,
        )
        conn = sqlite3.connect(tmp_csv)
        conn.row_factory = sqlite3.Row
        try:
            short_row = dict(conn.execute(
                "SELECT * FROM trades WHERE side='sell' AND position_id='spread-1'"
            ).fetchone())
            long_row = dict(conn.execute(
                "SELECT * FROM trades WHERE side='buy' AND position_id='spread-1'"
            ).fetchone())
        finally:
            conn.close()
        # Short leg: economic measurement
        assert short_row["slippage_benchmark_kind"] == "combo_limit"
        assert short_row["slippage_measurement_quality"] == "primary"
        assert short_row["slippage_benchmark_price"] == pytest.approx(0.60)
        assert short_row["slippage_signed_bps"] is not None
        # Opening credit short of 0.55 vs limit 0.60 — credit shortfall
        # is adverse: (0.60 - 0.55) / 0.60 * 10_000 ≈ 833.33 bps.
        assert short_row["slippage_signed_bps"] == pytest.approx(833.33, abs=0.1)
        assert short_row["slippage_adverse_bps"] == pytest.approx(833.33, abs=0.1)
        # Long leg: structural — NULL across both column families.
        # Phase 2 drops the legacy 0.0 structural zero; downstream
        # SUM/AVG consumers read slippage_adverse_bps and gate on
        # `.notna()`.
        assert long_row["slippage_benchmark_kind"] == "unavailable"
        assert long_row["slippage_measurement_quality"] == "unavailable"
        assert long_row["slippage_signed_bps"] is None
        assert long_row["slippage_adverse_bps"] is None
        assert long_row["realized_slippage_bps"] is None
        assert long_row["modeled_slippage_bps"] is None

    def test_spread_without_submitted_limit_writes_unavailable(self, tmp_csv):
        """When no submitted_limit_price is provided, both legs write
        'unavailable' rather than fabricating a benchmark."""
        tl = TradeLogger(path=tmp_csv)
        tl.log_spread_fill(
            position_id="spread-2",
            strategy="credit_spread",
            short_occ="SPY260620P00510000",
            long_occ="SPY260620P00505000",
            qty=1.0,
            net_price=0.55,
            submitted_limit_price=None,
            opening=True,
            initial_risk_dollars=445.0,
        )
        conn = sqlite3.connect(tmp_csv)
        conn.row_factory = sqlite3.Row
        try:
            short_row = dict(conn.execute(
                "SELECT * FROM trades WHERE side='sell' AND position_id='spread-2'"
            ).fetchone())
        finally:
            conn.close()
        assert short_row["slippage_benchmark_kind"] == "unavailable"
        assert short_row["slippage_measurement_quality"] == "unavailable"
        assert short_row["slippage_signed_bps"] is None


# ── TestLogStopFillSlippageContract ────────────────────────────────────────


class TestLogStopFillSlippageContract:
    """
    Phase 1 commit 2 of slippage unification — see codepath §4 in
    docs/slippage_unification_design.md. log_stop_fill now reads its
    benchmark from the broker-provided stop_price (not initial_stop_loss),
    populates the new taxonomy columns, and dual-writes the legacy
    columns for Phase 1 compatibility.
    """

    def _read_one_stop_row(self, db_path: str) -> dict:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                "SELECT * FROM trades WHERE reason = 'stop_triggered' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None, "expected one stop row"
        return dict(row)

    def test_with_broker_stop_writes_active_stop_primary(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10.0,
            avg_fill_price=144.50,
            stop_price=145.00,
            order_id="stop-broker-1",
        )
        row = self._read_one_stop_row(tmp_csv)
        assert row["slippage_benchmark_kind"] == "active_stop_price"
        assert row["slippage_measurement_quality"] == "primary"
        assert row["slippage_benchmark_price"] == pytest.approx(145.00)
        assert row["stop_trigger_price"] == pytest.approx(145.00)
        assert row["slippage_benchmark_timestamp"] is not None
        # Sell @ 144.50 vs reference 145.00 → adverse by 50/145 ≈ 34.48 bps
        assert row["slippage_signed_bps"] == pytest.approx(34.48, abs=0.05)
        assert row["slippage_adverse_bps"] == pytest.approx(34.48, abs=0.05)

    def test_without_broker_stop_writes_unavailable(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10.0,
            avg_fill_price=144.50,
            stop_price=None,
            order_id="stop-no-broker",
        )
        row = self._read_one_stop_row(tmp_csv)
        assert row["slippage_benchmark_kind"] == "unavailable"
        assert row["slippage_measurement_quality"] == "unavailable"
        assert row["slippage_benchmark_price"] is None
        assert row["slippage_signed_bps"] is None
        assert row["slippage_adverse_bps"] is None
        assert row["stop_trigger_price"] is None

    def test_recovery_path_can_set_quality_recovered(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10.0,
            avg_fill_price=144.50,
            stop_price=145.00,
            measurement_quality="recovered",
            order_id="stop-recovered-1",
        )
        row = self._read_one_stop_row(tmp_csv)
        assert row["slippage_benchmark_kind"] == "active_stop_price"
        assert row["slippage_measurement_quality"] == "recovered"

    def test_legacy_columns_null_on_new_stop_fill_rows(self, tmp_csv):
        """Phase 2 replacement for the Phase 1 dual-write parity pin.
        Legacy `realized_slippage_bps` / `modeled_slippage_bps` are
        no longer written on new rows regardless of whether the
        broker provided a stop_price; the new taxonomy is the sole
        source of truth."""
        tl = TradeLogger(path=tmp_csv)
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10.0,
            avg_fill_price=144.50,
            stop_price=145.00,
            order_id="stop-no-legacy-2",
        )
        row = self._read_one_stop_row(tmp_csv)
        assert row["realized_slippage_bps"] is None
        assert row["modeled_slippage_bps"] is None
        assert row["slippage_signed_bps"] is not None
        assert row["slippage_adverse_bps"] is not None

    def test_malformed_broker_stop_writes_unavailable(self, tmp_csv):
        """Defect 4 fix — every form of malformed stop_price must be
        rejected without raising. Covers: +inf, -inf, NaN, zero,
        negative, and non-numeric strings like 'bad' (which would
        raise ValueError on float() if not caught). log_stop_fill
        writes the row as 'unavailable' for all of them."""
        import math as _math
        tl = TradeLogger(path=tmp_csv)
        bad_values: list = [_math.inf, -_math.inf, _math.nan, 0.0, -1.50, "bad", "1.2.3"]
        for i, bad in enumerate(bad_values):
            tl.log_stop_fill(
                symbol="AAPL",
                strategy="sma_crossover",
                qty=10.0,
                avg_fill_price=144.50,
                stop_price=bad,
                order_id=f"stop-bad-{i}",
            )
            row = self._read_one_stop_row(tmp_csv)
            assert row["slippage_benchmark_kind"] == "unavailable", (
                f"malformed stop_price={bad!r} should produce 'unavailable'"
            )
            assert row["slippage_signed_bps"] is None

    def test_missing_broker_stop_writes_null_across_both_column_families(
        self, tmp_csv,
    ):
        """Phase 2: when the broker stop_price is missing, both new
        AND legacy columns report 'unavailable' / NULL. Pre-Phase 2
        the legacy column still fell back to `initial_stop_loss` so
        unmigrated consumers saw a benchmark; with Phase 1 dual-write
        removed there is no longer a legacy fallback path."""
        tl = TradeLogger(path=tmp_csv)
        # Seed an open entry context so initial_stop_loss is populated.
        conn = tl._ensure_db()
        conn.execute(
            """
            INSERT INTO trades (
                timestamp, symbol, side, qty, avg_fill_price, strategy,
                reason, status, requested_qty, filled_qty,
                initial_stop_loss, initial_risk_per_share, entry_reference_price,
                entry_timestamp, position_id, position_type
            ) VALUES (?, ?, 'buy', 10, 150.0, 'sma_crossover', 'entry', 'filled',
                      10, 10, 145.0, 5.0, 150.0, ?, ?, 'single_leg')
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                "AAPL",
                datetime.now(timezone.utc).isoformat(),
                "AAPL",
            ),
        )
        conn.commit()

        tl.log_stop_fill(
            symbol="AAPL",
            strategy="sma_crossover",
            qty=10.0,
            avg_fill_price=144.50,
            stop_price=None,
            order_id="stop-no-legacy-fallback",
        )
        row = self._read_one_stop_row(tmp_csv)
        # New cols: unavailable.
        assert row["slippage_benchmark_kind"] == "unavailable"
        assert row["slippage_signed_bps"] is None
        # Legacy cols: also NULL — no silent initial_stop_loss fallback
        # (Phase 1 dual-write removed in Phase 2).
        assert row["realized_slippage_bps"] is None
        assert row["modeled_slippage_bps"] is None


# ── Open-state replay chronology + entry-basis provenance ──────────────────


def _single_leg_row(
    *,
    symbol: str,
    side: str,
    qty: float,
    fill: float | None,
    reference: float,
    timestamp: str,
    order_id: str,
    strategy: str = "fake_strategy",
    reason: str = "test",
    order_type: str = "market",
    entry_timestamp: str | None = None,
    stop_price: float = 0.0,
    initial_stop_loss: float | None = None,
    initial_risk_per_share: float | None = None,
    initial_risk_dollars: float | None = None,
    realized_pnl: float | None = None,
    r_multiple: float | None = None,
    exit_timestamp: str | None = None,
) -> TradeRecord:
    """Build a minimal filled single-leg trades row with explicit
    timestamps, so tests can write out-of-order (backfilled) histories."""
    return TradeRecord(
        timestamp=timestamp,
        symbol=symbol,
        side=side,
        qty=qty,
        avg_fill_price=fill,
        order_id=order_id,
        strategy=strategy,
        reason=reason,
        stop_price=stop_price,
        entry_reference_price=reference,
        modeled_slippage_bps=None,
        realized_slippage_bps=None,
        order_type=order_type,
        status="filled",
        requested_qty=qty,
        filled_qty=qty,
        initial_stop_loss=initial_stop_loss,
        initial_risk_per_share=initial_risk_per_share,
        initial_risk_dollars=initial_risk_dollars,
        realized_pnl=realized_pnl,
        r_multiple=r_multiple,
        entry_timestamp=entry_timestamp or timestamp,
        exit_timestamp=exit_timestamp,
        position_id=symbol,
        position_type="single_leg",
    )


class TestReplayChronologyAndBasisSource:
    """The open-state replay must be chronological (event timestamp,
    then id), and it must record the provenance of the weighted entry
    basis. Regression tests for the trade-320 blend and the trade-285/
    288/290/301 reference-basis P&L corruption (2026-07 audit)."""

    T1 = "2026-05-01T10:00:00+00:00"
    T2 = "2026-05-10T10:00:00+00:00"
    T3 = "2026-06-01T10:00:00+00:00"
    T4 = "2026-06-09T10:00:00+00:00"

    def test_fractional_full_close_normalizes_before_reentry(self, tmp_csv):
        """A complete fractional close must not leave float dust that
        suppresses ownership and entry context for the next position.

        AVGO regression: 7.19 shares closed as 7 + 0.19, then a new
        7-share Donchian entry arrived after restart.
        """
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="AVGO", side="buy", qty=7.19, fill=427.08,
            reference=427.08, timestamp=self.T1, order_id="old-entry",
            stop_price=400.69,
        ))
        tl.log(_single_leg_row(
            symbol="AVGO", side="sell", qty=7.0, fill=400.79,
            reference=427.08, timestamp=self.T2, order_id="old-stop",
            exit_timestamp=self.T2,
        ))
        tl.log(_single_leg_row(
            symbol="AVGO", side="sell", qty=0.19, fill=402.19,
            reference=427.08, timestamp=self.T3, order_id="old-residual",
            exit_timestamp=self.T3,
        ))
        tl.log(_single_leg_row(
            symbol="AVGO", side="buy", qty=7.0, fill=426.09,
            reference=420.68, timestamp=self.T4, order_id="new-entry",
            stop_price=386.95,
        ))

        state = tl._read_single_leg_open_state()["AVGO"]
        assert state["open_qty"] == 7.0
        assert state["strategy"] == "fake_strategy"
        assert state["entry_timestamp"] == self.T4
        assert state["entry_fill_price"] == pytest.approx(426.09)
        assert tl.read_all_open_owners() == {"AVGO": "fake_strategy"}
        assert tl.read_latest_open_stop_price(
            symbol="AVGO", strategy="fake_strategy",
        ) == pytest.approx(386.95)

    def test_backfilled_exit_does_not_blend_adjacent_positions(self, tmp_csv):
        """A reconciliation-backfilled exit (higher id, earlier
        timestamp) must close position 1 BEFORE position 2's entry is
        replayed — id-ordered replay blended both into one basis."""
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="QCOM", side="buy", qty=14.0, fill=245.0,
            reference=245.0, timestamp=self.T1, order_id="b1",
        ))
        tl.log(_single_leg_row(
            symbol="QCOM", side="buy", qty=16.0, fill=228.0,
            reference=251.0, timestamp=self.T3, order_id="b2",
        ))
        # Backfilled exit of position 1: written last, executed between
        # the two entries.
        tl.log(_single_leg_row(
            symbol="QCOM", side="sell", qty=14.0, fill=195.5,
            reference=245.0, timestamp=self.T2, order_id="s1",
            exit_timestamp=self.T2,
        ))

        state = tl._read_single_leg_open_state()["QCOM"]
        assert state["open_qty"] == pytest.approx(16.0)
        assert state["entry_fill_price"] == pytest.approx(228.0)
        assert state["entry_timestamp"] == self.T3
        assert state["entry_fill_price_source"] == ENTRY_BASIS_BROKER_FILL

    def test_stop_fill_books_against_correct_position_basis(self, tmp_csv):
        """End-to-end trade-320 regression: the stop fill after a
        backfilled exit books P&L against position 2's broker fill."""
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="QCOM", side="buy", qty=14.0, fill=245.0,
            reference=245.0, timestamp=self.T1, order_id="b1",
        ))
        tl.log(_single_leg_row(
            symbol="QCOM", side="buy", qty=16.0, fill=228.0,
            reference=251.0, timestamp=self.T3, order_id="b2",
        ))
        tl.log(_single_leg_row(
            symbol="QCOM", side="sell", qty=14.0, fill=195.5,
            reference=245.0, timestamp=self.T2, order_id="s1",
            exit_timestamp=self.T2,
        ))

        tl.log_stop_fill(
            symbol="QCOM",
            strategy="fake_strategy",
            qty=16.0,
            avg_fill_price=195.0,
            stop_price=195.5,
            order_id="s2",
            timestamp_override=datetime.fromisoformat(self.T4),
        )
        row = next(r for r in tl.read_all() if r["order_id"] == "s2")
        assert row["realized_pnl"] == pytest.approx((195.0 - 228.0) * 16.0)
        assert row["entry_timestamp"] == self.T3

    def test_stop_repair_reads_current_position_stop_level(self, tmp_csv):
        """read_latest_open_stop_price feeds the engine's
        _repair_missing_protective_stops — a LIVE-MONEY consumer of the
        replayed state. Under the old id-ordered replay, this
        trade-320-shaped history left stop_price stale from position 1:
        the June 2026 QCOM broker stop was repaired at the May
        position's 195.57 instead of the intended 218.64 and rode ~$370
        of avoidable loss. The chronological replay must return the
        CURRENT position's stop."""
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="QCOM", side="buy", qty=14.0, fill=245.0,
            reference=245.0, timestamp=self.T1, order_id="b1",
            stop_price=195.57,
        ))
        tl.log(_single_leg_row(
            symbol="QCOM", side="buy", qty=16.0, fill=228.0,
            reference=251.0, timestamp=self.T3, order_id="b2",
            stop_price=218.64,
        ))
        # Backfilled partial exit of position 1 (higher id, earlier
        # execution) — under id-order it replayed AFTER buy 2, leaving
        # open_qty > 0 at buy 2's turn so position 1's stop survived.
        tl.log(_single_leg_row(
            symbol="QCOM", side="sell", qty=14.0, fill=195.5,
            reference=245.0, timestamp=self.T2, order_id="s1",
            exit_timestamp=self.T2,
        ))

        stop = tl.read_latest_open_stop_price(
            symbol="QCOM", strategy="fake_strategy",
        )
        assert stop == pytest.approx(218.64)

    def test_basis_source_broker_fill(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="AAPL", side="buy", qty=10.0, fill=100.5,
            reference=100.0, timestamp=self.T1, order_id="b1",
        ))
        context = tl.read_latest_open_entry_context(
            symbol="AAPL", strategy="fake_strategy",
        )
        assert context["entry_fill_price"] == pytest.approx(100.5)
        assert context["entry_fill_price_source"] == ENTRY_BASIS_BROKER_FILL

    def test_basis_source_reference_fallback(self, tmp_csv):
        """A buy row with no broker fill falls back to the reference
        price for qty/ownership reconstruction — tagged, not silent."""
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="AAPL", side="buy", qty=10.0, fill=None,
            reference=100.0, timestamp=self.T1, order_id="b1",
        ))
        context = tl.read_latest_open_entry_context(
            symbol="AAPL", strategy="fake_strategy",
        )
        assert context["entry_fill_price"] == pytest.approx(100.0)
        assert (
            context["entry_fill_price_source"] == ENTRY_BASIS_REFERENCE_FALLBACK
        )

    def test_basis_source_mixed_on_scale_in(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="AAPL", side="buy", qty=10.0, fill=100.0,
            reference=100.0, timestamp=self.T1, order_id="b1",
        ))
        tl.log(_single_leg_row(
            symbol="AAPL", side="buy", qty=10.0, fill=None,
            reference=110.0, timestamp=self.T2, order_id="b2",
        ))
        state = tl._read_single_leg_open_state()["AAPL"]
        assert state["entry_fill_price"] == pytest.approx(105.0)
        assert state["entry_fill_price_source"] == ENTRY_BASIS_MIXED

    def test_basis_source_resets_after_full_close(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="AAPL", side="buy", qty=10.0, fill=None,
            reference=100.0, timestamp=self.T1, order_id="b1",
        ))
        tl.log(_single_leg_row(
            symbol="AAPL", side="sell", qty=10.0, fill=99.0,
            reference=99.0, timestamp=self.T2, order_id="s1",
            exit_timestamp=self.T2,
        ))
        tl.log(_single_leg_row(
            symbol="AAPL", side="buy", qty=5.0, fill=101.0,
            reference=101.5, timestamp=self.T3, order_id="b2",
        ))
        state = tl._read_single_leg_open_state()["AAPL"]
        assert state["entry_fill_price"] == pytest.approx(101.0)
        assert state["entry_fill_price_source"] == ENTRY_BASIS_BROKER_FILL


class TestExitBasisRefusalAndLifecycleRescue:
    """Exit writers must never book realized P&L against a
    reference-tainted basis (PLAN 'Durable Design Decisions'). When the
    replay basis is unusable, the lifecycle substrate's
    ``avg_entry_price`` is the fallback; failing that, realized_pnl
    stays honestly NULL."""

    T1 = "2026-05-01T10:00:00+00:00"
    UID = "pos_" + "a" * 32

    def _entry_without_fill(self, tmp_csv) -> TradeLogger:
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="AAPL", side="buy", qty=10.0, fill=None,
            reference=100.0, timestamp=self.T1, order_id="b1",
            initial_risk_per_share=5.0,
        ))
        return tl

    def _mark_lifecycle_open(self, tl: TradeLogger, avg_entry_price: float) -> None:
        from engine.lifecycle import PositionLifecycleStore

        store = PositionLifecycleStore(tl._ensure_db())
        store.create_pending(
            position_uid=self.UID,
            symbol="AAPL",
            owner_key="AAPL",
            strategy="fake_strategy",
            position_type="single_leg",
            entry_qty=10.0,
        )
        store.mark_open(
            position_uid=self.UID,
            avg_entry_price=avg_entry_price,
            current_qty=10.0,
        )

    def test_stop_fill_refuses_reference_basis(self, tmp_csv):
        tl = self._entry_without_fill(tmp_csv)
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="fake_strategy",
            qty=10.0,
            avg_fill_price=95.0,
            stop_price=95.0,
            order_id="s1",
        )
        row = next(r for r in tl.read_all() if r["order_id"] == "s1")
        assert row["realized_pnl"] is None
        assert row["r_multiple"] is None

    def test_stop_fill_uses_lifecycle_basis_when_replay_unusable(self, tmp_csv):
        tl = self._entry_without_fill(tmp_csv)
        self._mark_lifecycle_open(tl, avg_entry_price=101.5)
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="fake_strategy",
            qty=10.0,
            avg_fill_price=95.0,
            stop_price=95.0,
            order_id="s1",
            position_uid=self.UID,
        )
        row = next(r for r in tl.read_all() if r["order_id"] == "s1")
        assert row["realized_pnl"] == pytest.approx((95.0 - 101.5) * 10.0)
        # r_multiple stays consistent with the booked P&L.
        assert row["r_multiple"] == pytest.approx(-65.0 / 50.0)

    def test_stop_fill_books_normally_against_broker_fill(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log(_single_leg_row(
            symbol="AAPL", side="buy", qty=10.0, fill=100.5,
            reference=100.0, timestamp=self.T1, order_id="b1",
        ))
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="fake_strategy",
            qty=10.0,
            avg_fill_price=95.0,
            stop_price=95.0,
            order_id="s1",
        )
        row = next(r for r in tl.read_all() if r["order_id"] == "s1")
        assert row["realized_pnl"] == pytest.approx((95.0 - 100.5) * 10.0)

    def test_build_close_record_refuses_reference_basis(self, tmp_csv, sample_result):
        tl = self._entry_without_fill(tmp_csv)
        record = tl.build_close_record(
            sample_result,
            strategy_name="fake_strategy",
            modeled_price=96.0,
            benchmark_kind="arrival_midpoint",
        )
        assert record.realized_pnl is None
        assert record.r_multiple is None

    def test_build_close_record_uses_lifecycle_basis(self, tmp_csv, sample_result):
        tl = self._entry_without_fill(tmp_csv)
        self._mark_lifecycle_open(tl, avg_entry_price=101.5)
        record = tl.build_close_record(
            sample_result,
            strategy_name="fake_strategy",
            modeled_price=96.0,
            benchmark_kind="arrival_midpoint",
            position_uid=self.UID,
        )
        assert record.realized_pnl == pytest.approx(
            (sample_result.avg_fill_price - 101.5) * 10.0
        )

    def test_read_lifecycle_entry_basis_contract(self, tmp_csv):
        tl = self._entry_without_fill(tmp_csv)
        assert tl.read_lifecycle_entry_basis(None) is None
        assert tl.read_lifecycle_entry_basis("pos_" + "f" * 32) is None
        self._mark_lifecycle_open(tl, avg_entry_price=101.5)
        assert tl.read_lifecycle_entry_basis(self.UID) == pytest.approx(101.5)

    def test_missing_context_and_lifecycle_warns_loudly(self, tmp_csv):
        """The honest-NULL worst case (no replay state, no lifecycle
        row) must be traceable from the log — the allocator gate will
        never count this exit."""
        from loguru import logger as loguru_logger

        messages: list[str] = []
        sink_id = loguru_logger.add(messages.append, level="WARNING")
        try:
            tl = TradeLogger(path=tmp_csv)
            tl.log_stop_fill(
                symbol="AAPL",
                strategy="fake_strategy",
                qty=10.0,
                avg_fill_price=95.0,
                stop_price=95.0,
                order_id="s1",
                position_uid="pos_" + "c" * 32,
            )
        finally:
            loguru_logger.remove(sink_id)
        row = next(r for r in tl.read_all() if r["order_id"] == "s1")
        assert row["realized_pnl"] is None
        logged = "".join(messages)
        assert "no entry basis available" in logged
        assert "log_stop_fill" in logged
        assert "pos_" + "c" * 32 in logged


# ── TestPnLTracker ──────────────────────────────────────────────────────────


def _log_close(tl, *, day: str, symbol: str, strategy: str, pnl: float, seq: int = 1):
    """Write one realized-P&L close row — what the engine logs on exit."""
    tl.log(TradeRecord(
        timestamp=f"{day}T15:{seq:02d}:00+00:00",
        symbol=symbol, side="sell", qty=1, avg_fill_price=100.0,
        order_id=f"{symbol}-close-{seq}",
        strategy=strategy, reason="exit signal",
        stop_price=0.0, entry_reference_price=100.0,
        modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
        order_type="market", status="filled",
        requested_qty=1, filled_qty=1,
        realized_pnl=pnl,
        entry_timestamp=f"{day}T13:00:00+00:00",
        exit_timestamp=f"{day}T15:{seq:02d}:00+00:00",
        position_type="single_leg",
    ))


class TestMaxDrawdownFromEquityPath:
    """Backs the daily report's `Max intraday drawdown` when reconciled
    against the broker's own intraday equity series."""

    def test_empty_and_rising_paths_have_no_drawdown(self):
        assert max_drawdown_from_equity_path([]) == 0.0
        assert max_drawdown_from_equity_path([100.0]) == 0.0
        assert max_drawdown_from_equity_path([100.0, 101.0, 105.0]) == 0.0

    def test_uses_running_peak_not_range(self):
        """A low that precedes the high is not a drawdown. Taken from
        the live paper account on 2026-08-19: trough 100,828.40 at
        14:15Z, peak 101,615.53 at 16:19Z. The true peak-to-trough
        decline was $577.88; `max - min` would claim $787.13 — a drop
        that never happened.
        """
        path = [101_113.43, 100_828.40, 101_615.53, 101_037.65, 101_408.36]

        assert max_drawdown_from_equity_path(path) == pytest.approx(577.88)
        assert max(path) - min(path) == pytest.approx(787.13)

    def test_worst_decline_wins_over_a_later_shallower_one(self):
        path = [100.0, 90.0, 100.0, 95.0]
        assert max_drawdown_from_equity_path(path) == pytest.approx(10.0)

    def test_decline_is_measured_from_the_preceding_peak(self):
        path = [100.0, 120.0, 80.0]
        assert max_drawdown_from_equity_path(path) == pytest.approx(40.0)

    def test_none_samples_are_skipped_not_treated_as_zero(self):
        """Alpaca returns nulls for minutes it cannot value. Reading one
        as $0 equity would manufacture a six-figure drawdown out of a
        reporting gap."""
        path = [100_000.0, None, 99_500.0, None]
        assert max_drawdown_from_equity_path(path) == pytest.approx(500.0)


class TestUnrealizedPnLFromPositions:
    """`unrealized_pnl_from_positions` — the daily report's unrealized figure."""

    def test_sums_broker_unrealized_across_positions(self):
        positions = [
            SimpleNamespace(symbol="AAPL", unrealized_pl=250.5),
            SimpleNamespace(symbol="NVDA", unrealized_pl=-100.25),
            SimpleNamespace(symbol="PLTR", unrealized_pl=0.0),
        ]
        assert unrealized_pnl_from_positions(positions) == pytest.approx(150.25)

    def test_empty_book_is_zero(self):
        assert unrealized_pnl_from_positions([]) == 0.0

    def test_reads_the_real_position_dataclass(self):
        """The helper is duck-typed on `.unrealized_pl`; production feeds
        it `BrokerSnapshot.account.open_positions.values()`. Pin the
        field name to the actual dataclass so a rename fails here rather
        than quietly restoring $0.00 to the report."""
        from risk.manager import Position

        positions = {
            "AAPL": Position(
                symbol="AAPL", qty=10, avg_entry_price=200.0,
                market_value=2_100.0, unrealized_pl=100.0,
            ),
            "NVDA": Position(
                symbol="NVDA", qty=5, avg_entry_price=220.0,
                market_value=1_075.0, unrealized_pl=-25.0,
            ),
        }
        assert unrealized_pnl_from_positions(positions.values()) == pytest.approx(75.0)

    def test_missing_unrealized_is_skipped_and_warned(self):
        """A None must not silently become 0.0 — that is the exact
        failure mode this function exists to end. It is skipped, and the
        understatement is logged with the symbol."""
        positions = [
            SimpleNamespace(symbol="AAPL", unrealized_pl=300.0),
            SimpleNamespace(symbol="NVDA", unrealized_pl=None),
        ]
        from loguru import logger as loguru_logger

        messages: list[str] = []
        sink_id = loguru_logger.add(messages.append, level="WARNING")
        try:
            total = unrealized_pnl_from_positions(positions)
        finally:
            loguru_logger.remove(sink_id)

        assert total == pytest.approx(300.0)
        logged = "".join(messages)
        assert "NVDA" in logged
        assert "understates" in logged


class TestPnLTracker:
    def test_eod_summary_reads_trade_log_when_no_in_memory_events(
        self, tmp_csv, tmp_daily_dir,
    ):
        """The well-known EOD bug: production never calls record_trade_pnl,
        so the in-memory list was always empty and EOD reported
        ``P&L=$+0.00, trades=0`` even when the trade DB had real closes.

        The fix sources events from ``read_realized_pnl_events_for_day``
        — restart-safe and matches the actual trade log.
        """
        # Write real close rows to the trade DB — simulating what the
        # engine actually logs during a normal cycle.
        tl = TradeLogger(path=tmp_csv)
        for i, pnl in enumerate([100.0, -50.0, 25.0], start=1):
            tl.log(TradeRecord(
                timestamp=f"2026-06-09T15:{i:02d}:00+00:00",
                symbol=f"SYM{i}", side="sell", qty=1, avg_fill_price=100.0,
                order_id=f"close-{i}",
                strategy="sma_crossover", reason="exit signal",
                stop_price=0.0, entry_reference_price=100.0,
                modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
                order_type="market", status="filled",
                requested_qty=1, filled_qty=1,
                realized_pnl=pnl,
                entry_timestamp="2026-06-09T13:00:00+00:00",
                exit_timestamp=f"2026-06-09T15:{i:02d}:00+00:00",
                position_type="single_leg",
            ))

        # PnLTracker wired to the SAME DB — but no record_trade_pnl ever called.
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir,
        )
        summary = tracker.generate_daily_summary(day="2026-06-09")

        # Before the fix: total_trades=0, realized_pnl=0.0.
        # After: the trade log is the source of truth.
        assert summary.total_trades == 3, (
            "EOD must read the trade log, not the empty in-memory list"
        )
        assert summary.realized_pnl == pytest.approx(75.0)
        assert summary.largest_win == pytest.approx(100.0)
        assert summary.largest_loss == pytest.approx(-50.0)
        # Per-strategy attribution still works.
        assert summary.strategies["sma_crossover"].trade_count == 3

    def test_eod_summary_survives_bot_recycle_midday(
        self, tmp_csv, tmp_daily_dir,
    ):
        """The original symptom on 2026-06-05 / 06-09 was that a midday
        bot recycle wiped the in-memory accumulator, so EOD showed 0
        trades even though the trade DB had the morning's closes. The
        DB-backed source is restart-safe.
        """
        tl = TradeLogger(path=tmp_csv)
        # Morning close (pre-recycle).
        tl.log(TradeRecord(
            timestamp="2026-06-09T11:25:00+00:00",
            symbol="QCOM", side="sell", qty=16, avg_fill_price=195.51,
            order_id="qcom-close-1",
            strategy="donchian_breakout", reason="exit signal",
            stop_price=0.0, entry_reference_price=236.58,
            modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
            order_type="market", status="filled",
            requested_qty=16, filled_qty=16,
            realized_pnl=-657.04,
            entry_timestamp="2026-04-22T13:00:00+00:00",
            exit_timestamp="2026-06-09T11:25:00+00:00",
            position_type="single_leg",
        ))
        # Simulate bot recycle — a FRESH PnLTracker (in-memory state empty).
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir,
        )
        # EOD fires later in the day (after recycle).
        summary = tracker.generate_daily_summary(day="2026-06-09")
        # The morning close is faithfully in the summary.
        assert summary.total_trades == 1
        assert summary.realized_pnl == pytest.approx(-657.04)
        assert summary.strategies["donchian_breakout"].trade_count == 1

    def test_per_strategy_attribution(self, tmp_csv, tmp_daily_dir):
        tl = TradeLogger(path=tmp_csv)
        _log_close(tl, day="2026-04-16", symbol="AAA", strategy="strat_a", pnl=200.0, seq=1)
        _log_close(tl, day="2026-04-16", symbol="BBB", strategy="strat_a", pnl=-80.0, seq=2)
        _log_close(tl, day="2026-04-16", symbol="CCC", strategy="strat_b", pnl=50.0, seq=3)
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )

        summary = tracker.generate_daily_summary(day="2026-04-16")
        assert "strat_a" in summary.strategies
        assert "strat_b" in summary.strategies

        a = summary.strategies["strat_a"]
        assert a.trade_count == 2
        assert a.total_pnl == 120.0
        assert a.wins == 1
        assert a.losses == 1
        assert a.win_rate == 0.5
        assert a.expectancy == 60.0

        b = summary.strategies["strat_b"]
        assert b.trade_count == 1
        assert b.profit_factor == float("inf")  # no losses

    def test_only_the_requested_days_closes_are_counted(self, tmp_csv, tmp_daily_dir):
        """Day scoping lives in the SQL, not in a resettable in-memory
        accumulator, so an adjacent day's closes never leak in."""
        tl = TradeLogger(path=tmp_csv)
        _log_close(tl, day="2026-04-16", symbol="AAA", strategy="s", pnl=100.0, seq=1)
        _log_close(tl, day="2026-04-17", symbol="BBB", strategy="s", pnl=-200.0, seq=2)
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )

        summary = tracker.generate_daily_summary(day="2026-04-17")
        assert summary.total_trades == 1
        assert summary.realized_pnl == -200.0

    def test_unrealized_and_drawdown_reach_the_report(self, tmp_csv, tmp_daily_dir):
        """Both fields shipped hardcoded at $0.00 for 71 daily reports:
        `unrealized_pnl` was never passed by the only production caller,
        and `max_intraday_drawdown` came from an accumulator nothing
        fed. Assert the caller's values survive into the markdown.
        """
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        summary = tracker.generate_daily_summary(
            day="2026-04-16",
            session_start_equity=100_000.0,
            session_end_equity=99_500.0,
            unrealized_pnl=-1_234.56,
            max_intraday_drawdown=780.25,
        )

        assert summary.unrealized_pnl == -1_234.56
        assert summary.max_intraday_drawdown == 780.25

        content = open(tracker.write_daily_report(summary)).read()
        assert "| Unrealized P&L | $-1,234.56 |" in content
        assert "| Max intraday drawdown | $780.25 |" in content

    def test_write_daily_report(self, tmp_csv, tmp_daily_dir):
        tl = TradeLogger(path=tmp_csv)
        _log_close(
            tl, day="2026-04-16", symbol="AAA",
            strategy="sma_crossover", pnl=100.0,
        )
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )

        summary = tracker.generate_daily_summary(
            day="2026-04-16",
            session_start_equity=100_000.0,
            session_end_equity=100_100.0,
        )
        path = tracker.write_daily_report(summary)

        assert os.path.exists(path)
        content = open(path).read()
        assert "Daily P&L" in content
        assert "sma_crossover" in content
        assert "$100.00" in content

    def test_no_trades_summary(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        summary = tracker.generate_daily_summary(day="2026-04-16")
        assert summary.total_trades == 0
        assert summary.realized_pnl == 0.0

    def test_slippage_report_empty(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        report = tracker.slippage_report()
        assert report["count"] == 0
        # PLAN 11.50: no pooled `mean_bps` — equity and option basis
        # points are not comparable, so there is nothing to combine.
        assert report["by_instrument"] == {}

    def test_slippage_report_from_db(self, tmp_csv, tmp_daily_dir, sample_decision, sample_result):
        # Foundation §6.5: distinct order_ids → distinct rows.
        from dataclasses import replace
        tl = TradeLogger(path=tmp_csv)
        for i in range(5):
            result = replace(sample_result, order_id=f"ord-{i}")
            record = tl.build_record(sample_decision, result, modeled_price=150.0)
            tl.log(record)

        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        report = tracker.slippage_report(last_n=5)
        assert report["count"] == 5
        assert report["by_instrument"]["equity"]["count"] == 5
        assert report["by_instrument"]["equity"]["mean_bps"] > 0

    def test_slippage_report_excludes_recovered_quality_rows(
        self, tmp_csv, tmp_daily_dir, sample_decision, sample_result,
    ):
        """Phase 2 quality-whitelist regression guard.

        `_adverse_bps()` must gate on `slippage_measurement_quality
        IN ('primary','fallback')`, mirroring health / calibration /
        reconcile / dashboard. Pre-fix the helper only skipped
        NULL/non-numeric values, so a `recovered` row (e.g.
        reconstructed stop fill from broker history) with a huge
        adverse value would still flow into operator-facing weekly
        / daily / `slippage_report` outputs.

        Setup: one primary market entry (small adverse) + one
        manually-seeded recovered row with adverse 999 bps. Expected:
        the recovered row contributes nothing; count == 1, mean ==
        the primary row's adverse value.
        """
        from dataclasses import replace
        from reporting.logger import TradeRecord

        tl = TradeLogger(path=tmp_csv)
        # Measured market entry — flows through normal build_record so
        # it carries primary quality + a small computed adverse value.
        market_result = replace(sample_result, order_id="m-primary")
        primary_record = tl.build_record(
            sample_decision, market_result, modeled_price=150.0,
        )
        tl.log(primary_record)

        # Recovered row — bypass build_record to plant a row with
        # quality='recovered' and a huge adverse value. Pre-fix
        # `_adverse_bps` would have accepted it.
        recovered = TradeRecord(
            position_type="single_leg",
            timestamp="2026-04-15T10:00:00+00:00",
            symbol="MSFT", side="buy", qty=10, avg_fill_price=200.0,
            order_id="m-recovered",
            strategy="sma_crossover", reason="recovered entry context",
            stop_price=195.0, entry_reference_price=200.0,
            modeled_slippage_bps=None, realized_slippage_bps=None,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            slippage_signed_bps=999.0,
            slippage_adverse_bps=999.0,
            slippage_measurement_quality="recovered",
            slippage_benchmark_kind="arrival_midpoint",
        )
        tl.log(recovered)

        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir,
        )
        report = tracker.slippage_report(last_n=10)
        # Only the primary row participates. Without the quality
        # whitelist the recovered 999 bps row would push mean toward
        # ~500.
        assert report["count"] == 1
        primary_signed = primary_record.slippage_adverse_bps
        eq = report["by_instrument"]["equity"]
        assert eq["mean_bps"] == pytest.approx(primary_signed, abs=0.01)
        assert eq["max_bps"] == pytest.approx(primary_signed, abs=0.01)

    def test_slippage_report_skips_null_rows_in_count_and_mean(
        self, tmp_csv, tmp_daily_dir, sample_decision, sample_result,
    ):
        """Phase 2 slippage unification — rows with NULL
        slippage_adverse_bps must NOT default to 0. The pre-Phase-2
        code did `t.get("realized_slippage_bps", 0)` and silently
        diluted the mean toward zero when a window mixed measured
        rows with LIMIT/external-close paths that have no benchmark.

        Setup: one measured market entry (>0 bps adverse against
        the modeled 150.0 benchmark) + one LIMIT order which
        deliberately writes NULL on the new slippage columns.
        Report's `count` reflects only the measured row; `mean_bps`
        equals that measured row's value, not half of it.
        """
        from dataclasses import replace
        from risk.manager import RiskDecision
        from execution.broker import OrderType, Side
        tl = TradeLogger(path=tmp_csv)
        # Measured market entry.
        market_result = replace(sample_result, order_id="m-1")
        tl.log(tl.build_record(
            sample_decision, market_result, modeled_price=150.0,
        ))
        # LIMIT entry — NULL slippage on the new columns.
        limit_decision = RiskDecision(
            symbol="AAPL",
            side=Side.BUY,
            qty=10,
            stop_price=145.0,
            entry_reference_price=150.0,
            strategy_name="rsi_reversion",
            reason="rsi limit",
            order_type=OrderType.LIMIT,
            limit_price=149.50,
        )
        limit_result = replace(sample_result, order_id="l-1")
        tl.log(tl.build_record(
            limit_decision, limit_result, modeled_price=150.0,
        ))

        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir,
        )
        report = tracker.slippage_report(last_n=10)
        # Count is the number of measured rows, not the row count.
        assert report["count"] == 1
        # Mean equals the single measured row — undiluted by the
        # LIMIT row's NULL slippage.
        assert report["by_instrument"]["equity"]["mean_bps"] > 0

    def test_weekly_report_no_dailies(self, tmp_csv, tmp_daily_dir, tmp_weekly_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv,
            daily_pnl_dir=tmp_daily_dir,
            weekly_report_dir=tmp_weekly_dir,
        )
        result = tracker.generate_weekly_report(week_end="2026-04-16")
        assert result is None

    def test_weekly_report_with_dailies(self, tmp_csv, tmp_daily_dir, tmp_weekly_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv,
            daily_pnl_dir=tmp_daily_dir,
            weekly_report_dir=tmp_weekly_dir,
        )
        # Create a daily report first.
        _log_close(
            tl=TradeLogger(path=tmp_csv), day="2026-04-16",
            symbol="AAA", strategy="s", pnl=100.0,
        )
        summary = tracker.generate_daily_summary(day="2026-04-16")
        tracker.write_daily_report(summary)

        path = tracker.generate_weekly_report(week_end="2026-04-16")
        assert path is not None
        assert os.path.exists(path)
        content = open(path).read()
        assert "Weekly Report" in content
        assert "2026-04-16" in content

    def test_weekly_report_carries_realized_pnl(
        self, tmp_csv, tmp_daily_dir, tmp_weekly_dir,
    ):
        """The weekly report contained no dollar figure at all — only
        fill counts and slippage. It was written before `realized_pnl`
        was a column on `trades`, and the comment saying we only have
        "fills, not round-trips" outlived the schema that made it true.
        Two locals (`total_pnl`, `strat_pnls`) sat here unread.
        """
        tl = TradeLogger(path=tmp_csv)
        _log_close(tl, day="2026-04-13", symbol="AAA", strategy="donchian", pnl=-292.80, seq=1)
        _log_close(tl, day="2026-04-16", symbol="BBB", strategy="donchian", pnl=-271.11, seq=2)
        tracker = PnLTracker(
            trade_csv_path=tmp_csv,
            daily_pnl_dir=tmp_daily_dir,
            weekly_report_dir=tmp_weekly_dir,
        )
        for day in ("2026-04-13", "2026-04-16"):
            tracker.write_daily_report(tracker.generate_daily_summary(day=day))

        content = open(tracker.generate_weekly_report(week_end="2026-04-16")).read()

        assert "| Realized P&L | $-563.91 |" in content
        assert "| Closed trades | 2 |" in content
        assert "## Per-Strategy Attribution" in content
        assert "### donchian" in content
        assert "| P&L | $-563.91 |" in content
        assert "| Largest loss | $-292.80 |" in content

    def test_weekly_pnl_equals_the_sum_of_its_daily_reports(
        self, tmp_csv, tmp_daily_dir, tmp_weekly_dir,
    ):
        """The weekly and daily reports read realized P&L through the
        same query for exactly this reason. If someone re-hand-rolls
        either side's row filter, the two disagree and the weekly digest
        is where nobody would notice.
        """
        tl = TradeLogger(path=tmp_csv)
        # Spread of strategies, signs, and a zero-P&L close; plus a
        # partial, which counts, and a row outside the window, which
        # must not.
        _log_close(tl, day="2026-04-13", symbol="AAA", strategy="sma", pnl=140.5, seq=1)
        _log_close(tl, day="2026-04-14", symbol="BBB", strategy="rsi", pnl=-60.25, seq=2)
        _log_close(tl, day="2026-04-14", symbol="CCC", strategy="sma", pnl=0.0, seq=3)
        _log_close(tl, day="2026-04-16", symbol="DDD", strategy="donchian", pnl=-12.75, seq=4)
        _log_close(tl, day="2026-04-09", symbol="EEE", strategy="sma", pnl=9_999.0, seq=5)
        tracker = PnLTracker(
            trade_csv_path=tmp_csv,
            daily_pnl_dir=tmp_daily_dir,
            weekly_report_dir=tmp_weekly_dir,
        )

        daily_total = 0.0
        for day in ("2026-04-13", "2026-04-14", "2026-04-16"):
            summary = tracker.generate_daily_summary(day=day)
            tracker.write_daily_report(summary)
            daily_total += summary.realized_pnl

        content = open(tracker.generate_weekly_report(week_end="2026-04-16")).read()

        assert daily_total == pytest.approx(67.5)
        assert f"| Realized P&L | ${daily_total:,.2f} |" in content, content
        assert "9,999" not in content, "a close outside the window leaked in"
        # The zero-P&L close is a trade but neither a win nor a loss.
        assert "| Closed trades | 4 |" in content

    def test_weekly_report_with_no_closes_says_zero_not_nothing(
        self, tmp_csv, tmp_daily_dir, tmp_weekly_dir,
    ):
        """A week of entries and no exits is a real state. It must
        report $0.00 over 0 closed trades, not omit the row."""
        tracker = PnLTracker(
            trade_csv_path=tmp_csv,
            daily_pnl_dir=tmp_daily_dir,
            weekly_report_dir=tmp_weekly_dir,
        )
        tracker.write_daily_report(tracker.generate_daily_summary(day="2026-04-16"))

        content = open(tracker.generate_weekly_report(week_end="2026-04-16")).read()

        assert "| Realized P&L | $0.00 |" in content
        assert "| Closed trades | 0 |" in content
        assert "## Per-Strategy Attribution" not in content


# ── TestStrategyStats ───────────────────────────────────────────────────────


class TestStrategyStats:
    def test_win_rate(self):
        s = StrategyStats(strategy_name="x", trade_count=4, wins=3, losses=1)
        assert s.win_rate == 0.75

    def test_expectancy(self):
        s = StrategyStats(strategy_name="x", trade_count=2, total_pnl=100.0)
        assert s.expectancy == 50.0

    def test_profit_factor_with_losses(self):
        s = StrategyStats(
            strategy_name="x",
            trade_count=3,
            trade_pnls=[100.0, 50.0, -30.0],
        )
        assert abs(s.profit_factor - 5.0) < 0.01  # 150/30

    def test_profit_factor_no_losses(self):
        s = StrategyStats(
            strategy_name="x",
            trade_count=2,
            trade_pnls=[100.0, 50.0],
        )
        assert s.profit_factor == float("inf")

    def test_profit_factor_no_trades(self):
        s = StrategyStats(strategy_name="x")
        assert s.profit_factor == 0.0


# ── TestAlertDispatcher ─────────────────────────────────────────────────────


class _CollectorBackend(AlertBackend):
    """Test backend that collects alerts in a list."""

    def __init__(self):
        self.alerts: list[Alert] = []

    def send(self, alert: Alert) -> None:
        self.alerts.append(alert)


class TestAlertDispatcher:
    def test_fire_sends_to_backend(self):
        backend = _CollectorBackend()
        dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=0)
        alert = Alert(
            alert_type=AlertType.ORDER_REJECTION,
            severity=AlertSeverity.WARNING,
            message="test",
            symbol="AAPL",
        )
        sent = dispatcher.fire(alert)
        assert sent is True
        assert len(backend.alerts) == 1
        assert backend.alerts[0].message == "test"

    def test_cooldown_suppression(self):
        backend = _CollectorBackend()
        dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=600)
        alert = Alert(
            alert_type=AlertType.ORDER_REJECTION,
            severity=AlertSeverity.WARNING,
            message="test",
            symbol="AAPL",
        )
        assert dispatcher.fire(alert) is True
        assert dispatcher.fire(alert) is False  # suppressed
        assert len(backend.alerts) == 1

    def test_different_symbols_not_suppressed(self):
        backend = _CollectorBackend()
        dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=600)
        a1 = Alert(
            alert_type=AlertType.STALE_DATA,
            severity=AlertSeverity.WARNING,
            message="stale",
            symbol="AAPL",
        )
        a2 = Alert(
            alert_type=AlertType.STALE_DATA,
            severity=AlertSeverity.WARNING,
            message="stale",
            symbol="MSFT",
        )
        assert dispatcher.fire(a1) is True
        assert dispatcher.fire(a2) is True
        assert len(backend.alerts) == 2

    def test_backend_failure_does_not_raise(self):
        class _FailingBackend(AlertBackend):
            def send(self, alert):
                raise RuntimeError("boom")

        dispatcher = AlertDispatcher(
            backends=[_FailingBackend()], cooldown_seconds=0
        )
        alert = Alert(
            alert_type=AlertType.BROKER_ERROR,
            severity=AlertSeverity.WARNING,
            message="test",
        )
        # Should not raise.
        sent = dispatcher.fire(alert)
        assert sent is True

    def test_convenience_methods(self):
        backend = _CollectorBackend()
        dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=0)

        dispatcher.order_rejection("AAPL", "sma", "halted", "halted")
        dispatcher.circuit_breaker("daily loss limit")
        dispatcher.stale_data("MSFT", "2 days old")
        dispatcher.broker_error("timeout")
        dispatcher.broker_info("reconnected")
        dispatcher.option_trailing_state_unverified("SPY260618C00746000", "spy", 12.5)
        dispatcher.engine_halt("hard dollar cap")
        dispatcher.slippage_drift(15.0, 3.0)
        dispatcher.loss_streak_cooldown("sma", 3, 24.0)

        assert len(backend.alerts) == 9
        types = {a.alert_type for a in backend.alerts}
        assert AlertType.ORDER_REJECTION in types
        assert AlertType.CIRCUIT_BREAKER in types
        assert AlertType.STALE_DATA in types
        assert AlertType.BROKER_ERROR in types
        assert AlertType.BROKER_INFO in types
        assert AlertType.OPTION_TRAILING_STATE_UNVERIFIED in types
        assert AlertType.ENGINE_HALT in types
        assert AlertType.SLIPPAGE_DRIFT in types
        assert AlertType.LOSS_STREAK_COOLDOWN in types

    def test_alert_format(self):
        alert = Alert(
            alert_type=AlertType.ORDER_REJECTION,
            severity=AlertSeverity.WARNING,
            message="order rejected",
            symbol="AAPL",
            strategy="sma",
            details={"code": "halted"},
        )
        fmt = alert.format()
        assert "[WARNING]" in fmt
        assert "[order_rejection]" in fmt
        assert "[AAPL]" in fmt
        assert "[sma]" in fmt
        assert "code=halted" in fmt


# ── TestLogFileBackend ──────────────────────────────────────────────────────


class TestLogFileBackend:
    def test_send_does_not_raise(self, tmp_path):
        path = str(tmp_path / "alerts.log")
        backend = LogFileBackend(path=path)
        alert = Alert(
            alert_type=AlertType.BROKER_ERROR,
            severity=AlertSeverity.WARNING,
            message="test",
        )
        backend.send(alert)  # Should not raise.


# ── TestInstallJsonSink ─────────────────────────────────────────────────────


class TestInstallJsonSink:
    def test_creates_sink(self, tmp_path):
        from loguru import logger as _logger

        path = str(tmp_path / "bot.jsonl")
        sink_id = install_json_sink(path=path)
        assert isinstance(sink_id, int)
        _logger.remove(sink_id)


# ── TestEngineReportingWiring ───────────────────────────────────────────────


class TestEngineReportingWiring:
    """Verify the engine calls reporting hooks correctly."""

    def _make_engine(self, tmp_csv, monkeypatch):
        """Build an engine with mocked broker and reporting wired to tmp."""
        from engine.trader import EngineConfig, TradingEngine
        from risk.manager import RiskManager
        from strategies.base import BaseStrategy, SignalFrame

        class _Strat(BaseStrategy):
            name = "test"
            preferred_order_type = OrderType.MARKET

            def _raw_signals(self, df):
                return SignalFrame(
                    entries=pd.Series(False, index=df.index, dtype=bool),
                    exits=pd.Series(False, index=df.index, dtype=bool),
                )

        broker = MagicMock()
        broker.sync_with_broker.return_value = MagicMock(
            account=MagicMock(
                equity=100_000.0,
                cash=50_000.0,
                session_start_equity=100_000.0,
                open_positions={},
            ),
            open_orders=[],
        )

        config = EngineConfig(
            cycle_interval_seconds=1,
            market_hours_only=False,
        )

        trade_logger = TradeLogger(path=tmp_csv)
        backend = _CollectorBackend()
        alerts = AlertDispatcher(backends=[backend], cooldown_seconds=0)

        engine = TradingEngine(
            strategy=_Strat(),
            symbols=["AAPL"],
            risk=RiskManager(),
            broker=broker,
            config=config,
            trade_logger=trade_logger,
            alerts=alerts,
        )
        return engine, trade_logger, backend

    def test_engine_has_reporting_attributes(self, tmp_csv, monkeypatch):
        engine, tl, backend = self._make_engine(tmp_csv, monkeypatch)
        assert engine.trade_logger is tl
        assert engine.alerts is not None
        assert engine.pnl_tracker is not None

    def test_entry_fill_logged_to_db(self, tmp_csv, monkeypatch, sample_decision, sample_result):
        engine, tl, backend = self._make_engine(tmp_csv, monkeypatch)
        engine._log_entry(sample_decision, sample_result, 150.0)

        rows = tl.read_all()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL"
        assert rows[0]["side"] == "buy"

    def test_close_fill_logged_to_db(self, tmp_csv, monkeypatch, sample_result):
        engine, tl, backend = self._make_engine(tmp_csv, monkeypatch)
        engine._log_close(sample_result, 150.0)

        rows = tl.read_all()
        assert len(rows) == 1
        assert rows[0]["side"] == "sell"

    def test_non_filled_not_logged(self, tmp_csv, monkeypatch, sample_decision):
        engine, tl, backend = self._make_engine(tmp_csv, monkeypatch)
        rejected_result = OrderResult(
            status=OrderStatus.REJECTED,
            order_id="x",
            symbol="AAPL",
            requested_qty=10,
            filled_qty=0,
            avg_fill_price=None,
            raw_status="rejected",
        )
        engine._log_entry(sample_decision, rejected_result, 150.0)
        assert tl.read_all() == []


# ── Slippage metric families ────────────────────────────────────────────────


class TestSlippageFamilies:
    """The classification that keeps three incommensurable measurements
    from being averaged together.

    `slippage_adverse_bps` is one column holding execution quality,
    implementation shortfall and stop-gap erosion, told apart only by
    `slippage_benchmark_kind`. The design doc forbids feeding the latter
    two to the drift kill switch; before this classification existed
    that rule lived only in prose and nothing enforced it.
    """

    def test_every_benchmark_kind_is_classified(self):
        """The guard that stops this from rotting.

        Adding a benchmark kind to the Literal without placing it in a
        family fails here, rather than silently defaulting it into (or
        out of) the drift alarm months later.
        """
        declared = set(SlippageBenchmarkKind.__args__)
        classified = (
            EXECUTION_QUALITY_KINDS
            | IMPLEMENTATION_SHORTFALL_KINDS
            | STOP_GAP_KINDS
            | UNMEASURED_KINDS
        )
        assert classified == declared, (
            "every SlippageBenchmarkKind must belong to exactly one family; "
            f"unclassified={declared - classified} "
            f"unknown={classified - declared}"
        )

    def test_families_are_disjoint(self):
        families = [
            EXECUTION_QUALITY_KINDS,
            IMPLEMENTATION_SHORTFALL_KINDS,
            STOP_GAP_KINDS,
            UNMEASURED_KINDS,
        ]
        for i, left in enumerate(families):
            for right in families[i + 1:]:
                assert not (left & right), f"overlapping families: {left & right}"

    def test_fallback_latest_close_is_not_execution_quality(self):
        """The specific bug: a fill measured against a prior bar close
        reports overnight market movement, not execution cost. On
        2026-07-29 a market exit after a 9% gap booked 918 bps this way.
        """
        assert "fallback_latest_close" in IMPLEMENTATION_SHORTFALL_KINDS
        assert not is_execution_quality_measurement(
            "fallback_latest_close", "fallback"
        )

    def test_arrival_midpoint_is_execution_quality(self):
        assert is_execution_quality_measurement("arrival_midpoint", "primary")

    def test_stop_gap_is_not_execution_quality(self):
        assert not is_execution_quality_measurement("active_stop_price", "primary")

    @pytest.mark.parametrize("quality", ["recovered", "unavailable", "typo", None])
    def test_reconstructed_qualities_rejected_even_for_good_kind(self, quality):
        """Both dimensions must hold. A reconstructed benchmark is not a
        live observation even when its kind is right."""
        assert not is_execution_quality_measurement("arrival_midpoint", quality)

    @pytest.mark.parametrize("kind", [None, "", "not_a_kind"])
    def test_unknown_kinds_fail_closed(self, kind):
        assert not is_execution_quality_measurement(kind, "primary")

    def test_sql_predicate_matches_python_predicate(self, tmp_path):
        """The SQL consumers and the in-process kill switch must agree.

        Divergence here is invisible in production: the alarm would
        judge live fills against a differently-shaped history.
        """
        db = tmp_path / "t.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades (kind TEXT, quality TEXT)"
        )
        combos = [
            (k, q)
            for k in list(SlippageBenchmarkKind.__args__) + ["not_a_kind"]
            for q in ["primary", "fallback", "recovered", "unavailable", "typo"]
        ]
        conn.executemany("INSERT INTO trades VALUES (?, ?)", combos)
        conn.commit()

        fragment, params = execution_quality_sql()
        fragment = fragment.replace(
            "slippage_benchmark_kind", "kind"
        ).replace("slippage_measurement_quality", "quality")
        sql_rows = set(
            conn.execute(
                f"SELECT kind, quality FROM trades WHERE {fragment}", params
            ).fetchall()
        )
        conn.close()

        python_rows = {
            (k, q) for k, q in combos if is_execution_quality_measurement(k, q)
        }
        assert sql_rows == python_rows

    def test_calibration_grade_qualities_exclude_reconstructed(self):
        assert "recovered" not in CALIBRATION_GRADE_QUALITIES
        assert "unavailable" not in CALIBRATION_GRADE_QUALITIES


class TestExecutionQualitySlippageSeedRead:
    """`read_recent_execution_quality_slippage` feeds the kill switch's
    startup rehydration. Its selection must match `_record_fill`'s."""

    def _seed(self, tl, *, kind, quality, adverse, order_type="market", oid):
        tl.log(
            TradeRecord(
                position_type="single_leg",
                timestamp="2026-07-01T10:00:00+00:00",
                symbol="AAPL",
                side="sell",
                qty=1,
                avg_fill_price=100.0,
                order_id=oid,
                strategy="sma_crossover",
                reason="exit signal",
                stop_price=0.0,
                entry_reference_price=100.0,
                modeled_slippage_bps=None,
                realized_slippage_bps=None,
                order_type=order_type,
                status="filled",
                requested_qty=1,
                filled_qty=1,
                slippage_signed_bps=adverse,
                slippage_adverse_bps=adverse,
                slippage_benchmark_kind=kind,
                slippage_measurement_quality=quality,
            )
        )

    def test_excludes_implementation_shortfall_rows(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._seed(tl, kind="arrival_midpoint", quality="primary",
                   adverse=2.0, oid="a")
        self._seed(tl, kind="fallback_latest_close", quality="fallback",
                   adverse=918.3, oid="b")
        assert tl.read_recent_execution_quality_slippage(50) == [2.0]

    def test_excludes_non_market_orders(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._seed(tl, kind="arrival_midpoint", quality="primary",
                   adverse=3.0, order_type="limit", oid="a")
        assert tl.read_recent_execution_quality_slippage(50) == []

    def test_excludes_recovered_quality(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._seed(tl, kind="arrival_midpoint", quality="recovered",
                   adverse=4.0, oid="a")
        assert tl.read_recent_execution_quality_slippage(50) == []

    def test_returns_chronological_order(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        for i, value in enumerate([1.0, 2.0, 3.0]):
            self._seed(tl, kind="arrival_midpoint", quality="primary",
                       adverse=value, oid=f"o{i}")
        assert tl.read_recent_execution_quality_slippage(50) == [1.0, 2.0, 3.0]

    def test_limit_keeps_most_recent(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        for i, value in enumerate([1.0, 2.0, 3.0]):
            self._seed(tl, kind="arrival_midpoint", quality="primary",
                       adverse=value, oid=f"o{i}")
        assert tl.read_recent_execution_quality_slippage(2) == [2.0, 3.0]

    def test_missing_db_returns_empty(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "nope.db"))
        assert tl.read_recent_execution_quality_slippage(50) == []


class TestStopFillRiskContextByPositionUid:
    """Regression guard for the read-after-write hazard in the
    substrate stop-fill path.

    `_read_latest_open_entry_context` answers "what is open right now?"
    by netting buys against sells across the trade log. The substrate
    persists a stop's sell row via `apply_order_event` and only then
    dispatches `log_stop_fill`, so that replay nets to zero and reports
    the position flat — leaving the risk fields NULL even though the
    entry row is present. PR #76 made the substrate the primary stop
    handler, so this is the normal path.

    Production evidence: ANET 2026-07-17 (-$854.11, should be -1.10R)
    and AAPL 2026-07-31 (-$448.81, should be -2.13R) both booked a NULL
    r_multiple and an entry_timestamp stamped at exit time.
    """

    UID = "pos_testuid0000000000000000000000"

    def _log_entry_row(self, tl, *, qty=13.0, fill=339.543846,
                       rps=16.1921886113901, stop=320.73781138861,
                       uid=None, symbol="AAPL", order_id=None):
        """`order_id` is settable because a broker order belongs to exactly
        ONE lifecycle. Two distinct positions must not share one — see
        `test_does_not_cross_positions`."""
        tl.log(
            TradeRecord(
                position_type="single_leg",
                timestamp="2026-07-28T17:07:35+00:00",
                symbol=symbol,
                side="buy",
                qty=qty,
                avg_fill_price=fill,
                order_id=order_id or f"entry-{symbol}",
                strategy="donchian_breakout",
                reason="donchian_breakout entry @ 2026-07-27T04:00:00+00:00",
                stop_price=stop,
                entry_reference_price=336.93,
                modeled_slippage_bps=None,
                realized_slippage_bps=None,
                order_type="stop_limit",
                status="filled",
                requested_qty=qty,
                filled_qty=qty,
                initial_stop_loss=stop,
                initial_risk_per_share=rps,
                entry_timestamp="2026-07-28T17:07:35+00:00",
                position_uid=uid if uid is not None else self.UID,
            )
        )

    EXIT_TS = "2026-07-31T13:32:27+00:00"

    def _log_substrate_sell_row(self, tl, *, qty=13.0, price=305.02,
                                uid=None, symbol="AAPL",
                                entry_timestamp="__exit__"):
        """Mimic `apply_order_event` persisting the sell row FIRST.

        Faithfulness matters here. An earlier version of this fixture
        went through `TradeLogger.log()` without an `entry_timestamp`,
        which left the column NULL — so the PRESERVE-FIRST-NON-NULL
        upsert happily took the correct value later and the test passed
        while production stayed broken. `_TRADES_UPSERT_SQL` is the real
        writer, so this goes through it directly. Pass
        `entry_timestamp=None` to model the fixed behaviour, or a
        timestamp to model the pre-fix behaviour that stamped exit time
        into the entry column.
        """
        if entry_timestamp == "__exit__":
            entry_timestamp = self.EXIT_TS
        conn = tl._ensure_db()
        conn.execute(
            "INSERT INTO trades (timestamp, symbol, side, qty, avg_fill_price, "
            " order_id, strategy, reason, stop_price, entry_reference_price, "
            " order_type, status, requested_qty, filled_qty, entry_timestamp, "
            " position_type, position_uid) "
            "VALUES (?, ?, 'sell', ?, ?, ?, 'donchian_breakout', "
            "'stop_triggered', 0.0, 0.0, 'stop', 'filled', ?, ?, ?, "
            "'single_leg', ?)",
            (self.EXIT_TS, symbol, qty, price, f"substrate-sell-{symbol}",
             qty, qty, entry_timestamp,
             uid if uid is not None else self.UID),
        )
        conn.commit()

    def test_risk_context_resolves_when_replay_sees_position_flat(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_entry_row(tl)
        self._log_substrate_sell_row(tl)
        # Precondition: the open-state replay now nets to zero, which is
        # exactly why the old code lost the risk basis.
        assert tl._read_latest_open_entry_context(
            symbol="AAPL", strategy="donchian_breakout"
        ) is None
        ctx = tl.read_entry_context_by_uid(self.UID)
        assert ctx is not None
        assert ctx["initial_risk_per_share"] == pytest.approx(16.1921886113901)
        assert ctx["initial_stop_loss"] == pytest.approx(320.73781138861)
        assert ctx["entry_timestamp"] == "2026-07-28T17:07:35+00:00"

    def _seed_lifecycle_row(self, tl, *, uid=None, symbol="AAPL",
                            avg_entry_price=339.543846):
        """The substrate parent row. Production has this, and it is what
        supplies the P&L basis once the open-state replay reports flat —
        the pre-existing fallback this fix mirrors for the risk fields."""
        conn = tl._ensure_db()
        conn.execute(
            "INSERT OR REPLACE INTO position_lifecycle "
            "(position_uid, schema_version, created_at, symbol, owner_key, "
            " strategy, position_type, status, entry_qty, current_qty, "
            " avg_entry_price) "
            "VALUES (?, 1, ?, ?, ?, 'donchian_breakout', 'single_leg', "
            "'closed', 13.0, 0.0, ?)",
            (uid if uid is not None else self.UID, "2026-07-28T17:07:35+00:00",
             symbol, symbol, avg_entry_price),
        )
        conn.commit()

    def test_stop_fill_books_r_multiple_despite_write_ordering(self, tmp_path):
        """End-to-end: the AAPL scenario must book -2.13R, not NULL."""
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_entry_row(tl)
        self._seed_lifecycle_row(tl)
        self._log_substrate_sell_row(tl)  # substrate wrote it first
        tl.log_stop_fill(
            symbol="AAPL",
            strategy="donchian_breakout",
            qty=13.0,
            avg_fill_price=305.02,
            stop_price=320.73781138861,
            order_id="substrate-sell-AAPL",
            position_uid=self.UID,
        )
        row = [r for r in tl.read_all() if r["side"] == "sell"][-1]
        expected_risk = 16.1921886113901 * 13.0
        assert row["initial_risk_per_share"] == pytest.approx(16.1921886113901)
        assert row["initial_risk_dollars"] == pytest.approx(expected_risk)
        assert row["initial_stop_loss"] == pytest.approx(320.73781138861)
        # -448.81 / 210.50 = -2.13R
        assert row["r_multiple"] == pytest.approx(
            (305.02 - 339.543846) * 13.0 / expected_risk, rel=1e-6
        )
        assert row["r_multiple"] == pytest.approx(-2.13, abs=0.01)

    def test_entry_timestamp_is_the_entry_not_the_exit(self, tmp_path):
        """Third symptom: entry_timestamp was being stamped at exit time,
        corrupting hold-time analysis and breaking the entry/exit join.

        Models the substrate writing NULL for a closing row's
        entry_timestamp (see `_TRADES_UPSERT_SQL`), which is what lets
        the accounting writer's correct value survive the
        PRESERVE-FIRST-NON-NULL upsert.
        """
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_entry_row(tl)
        self._seed_lifecycle_row(tl)
        self._log_substrate_sell_row(tl, entry_timestamp=None)
        tl.log_stop_fill(
            symbol="AAPL", strategy="donchian_breakout", qty=13.0,
            avg_fill_price=305.02, stop_price=320.73781138861,
            order_id="substrate-sell-AAPL", position_uid=self.UID,
        )
        row = [r for r in tl.read_all() if r["side"] == "sell"][-1]
        assert row["entry_timestamp"] == "2026-07-28T17:07:35+00:00"

    def test_preexisting_wrong_entry_timestamp_is_preserved_not_corrected(
        self, tmp_path,
    ):
        """Pins WHY the historical rows needed a database repair.

        `entry_timestamp` is PRESERVE-FIRST-NON-NULL in the upsert, so a
        wrong value already on the row wins permanently over the correct
        one supplied moments later. Fixing the writer stops NEW rows
        being poisoned; it cannot heal rows already written. This test
        documents that boundary — if upsert policy ever changes so the
        later value wins, this test fails and the repair rationale needs
        revisiting.
        """
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_entry_row(tl)
        self._seed_lifecycle_row(tl)
        # Pre-fix substrate behaviour: exit time stamped into the entry column.
        self._log_substrate_sell_row(tl, entry_timestamp=self.EXIT_TS)
        tl.log_stop_fill(
            symbol="AAPL", strategy="donchian_breakout", qty=13.0,
            avg_fill_price=305.02, stop_price=320.73781138861,
            order_id="substrate-sell-AAPL", position_uid=self.UID,
        )
        row = [r for r in tl.read_all() if r["side"] == "sell"][-1]
        assert row["entry_timestamp"] == self.EXIT_TS
        # The risk fields still recover — they are inserted NULL by the
        # substrate, so preserve-first takes the correct later value.
        assert row["r_multiple"] == pytest.approx(-2.13, abs=0.01)

    def test_no_uid_leaves_fields_null_rather_than_zero(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        assert tl.read_entry_context_by_uid(None) is None
        assert tl.read_entry_context_by_uid("") is None

    def test_entry_without_risk_basis_still_yields_entry_timestamp(self, tmp_path):
        """Risk basis and entry timestamp are recovered independently.

        Entries predating risk-field recording legitimately have no
        basis, and the fallback must not invent one — but there is no
        reason for that to cost them their entry timestamp too. An
        earlier version gated the whole lookup on the risk basis and
        lost both.
        """
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_entry_row(tl, rps=None, stop=None)
        ctx = tl.read_entry_context_by_uid(self.UID)
        assert ctx is not None
        assert ctx["initial_risk_per_share"] is None
        assert ctx["initial_stop_loss"] is None
        assert ctx["entry_timestamp"] == "2026-07-28T17:07:35+00:00"

    def test_no_risk_basis_leaves_r_null_but_dates_the_entry(self, tmp_path):
        """End-to-end of the decoupling: a WYFI-shaped position (entry
        with no risk basis) books a correct entry_timestamp and a NULL
        r_multiple, rather than losing both."""
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_entry_row(tl, rps=None, stop=None)
        self._seed_lifecycle_row(tl)
        self._log_substrate_sell_row(tl, entry_timestamp=None)
        tl.log_stop_fill(
            symbol="AAPL", strategy="donchian_breakout", qty=13.0,
            avg_fill_price=305.02, stop_price=320.73781138861,
            order_id="substrate-sell-AAPL", position_uid=self.UID,
        )
        row = [r for r in tl.read_all() if r["side"] == "sell"][-1]
        assert row["entry_timestamp"] == "2026-07-28T17:07:35+00:00"
        assert row["r_multiple"] is None
        assert row["initial_risk_dollars"] is None
        # P&L still resolves via the lifecycle basis.
        assert row["realized_pnl"] == pytest.approx(
            (305.02 - 339.543846) * 13.0
        )

    def test_own_closing_row_is_not_mistaken_for_the_entry(self, tmp_path):
        """If the entry was never logged to `trades`, the lookup must
        not read the closing row it is about to write and recover its
        exit time as the entry time."""
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_substrate_sell_row(tl, entry_timestamp=self.EXIT_TS)
        assert tl.read_entry_context_by_uid(
            self.UID, exclude_order_id="substrate-sell-AAPL"
        ) is None

    def test_unknown_uid_returns_none(self, tmp_path):
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_entry_row(tl)
        assert tl.read_entry_context_by_uid("pos_doesnotexist") is None

    def test_does_not_cross_positions(self, tmp_path):
        """A prior closed position in the same symbol must not supply
        the basis for a later one."""
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        # Distinct order_ids: these are two separate round trips. Reusing
        # one id previously "worked" only because the second row carried a
        # NULL position_type, missed the partial unique index, and inserted
        # a duplicate instead of upserting — the exact 11.51 hazard. With
        # the field required the UPSERT fires and the identity check
        # correctly refuses to move an order between lifecycles.
        self._log_entry_row(tl, rps=99.0, stop=100.0, uid="pos_old",
                            order_id="entry-AAPL-old")
        self._log_entry_row(tl, rps=16.19, stop=320.74, uid="pos_new",
                            order_id="entry-AAPL-new")
        ctx = tl.read_entry_context_by_uid("pos_new")
        assert ctx["initial_risk_per_share"] == pytest.approx(16.19)

    def test_context_path_still_wins_when_available(self, tmp_path):
        """When the replay does find the open position (the normal
        synchronous exit path), nothing changes — the fallback is only
        consulted if the risk basis is still missing."""
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        self._log_entry_row(tl)
        ctx = tl._read_latest_open_entry_context(
            symbol="AAPL", strategy="donchian_breakout"
        )
        assert ctx is not None
        assert ctx["initial_risk_per_share"] == pytest.approx(16.1921886113901)


class TestRecordedRiskBasisMatchesTheRealStop:
    """`initial_risk_per_share` must describe the distance from the fill to
    the stop that actually exists — on BOTH order types.

    It previously recorded `reference − stop`, which on a STOP_LIMIT entry
    describes neither: sizing divides by `limit − stop` (worst permitted
    fill) while the real room is `fill − stop`. AAPL 2026-07-28 had all
    three differ — 29.20 sized, 18.81 actual, 16.19 recorded — so its
    r_multiple was computed against a number that described nothing.
    """

    def _result(self, fill, qty=13.0, placed_stop=None, reported=True):
        """`reported=True` models a broker path that knows the answer —
        including reporting `placed_stop=None` for "no stop was placed".
        `reported=False` models a legacy/recovery caller that says nothing.
        """
        return OrderResult(
            status=OrderStatus.FILLED, order_id="oid", symbol="AAPL",
            requested_qty=qty, filled_qty=qty, avg_fill_price=fill,
            raw_status="filled", placed_stop_price=placed_stop,
            stop_placement_reported=reported,
        )

    def _decision(self, **kw):
        from risk.manager import RiskDecision, Side
        base = dict(
            symbol="AAPL", side=Side.BUY, qty=13.0,
            entry_reference_price=336.93, stop_price=320.74,
            strategy_name="sma_crossover", reason="entry",
        )
        base.update(kw)
        return RiskDecision(**base)

    def test_fractional_path_reports_its_reanchored_stop(self, tmp_csv):
        """The fractional path submits the stop after the fill and reports
        the re-anchored level, so the row records exactly that."""
        tl = TradeLogger(path=tmp_csv)
        anchored = 339.54 - 16.19
        rec = tl.build_record(
            self._decision(),
            self._result(339.54, placed_stop=anchored),
            modeled_price=336.93,
        )
        assert rec.initial_stop_loss == pytest.approx(anchored, abs=0.01)
        assert rec.initial_risk_per_share == pytest.approx(16.19, abs=0.01)

    def test_whole_share_oto_records_the_bracket_child_not_a_reanchor(self, tmp_csv):
        """Review finding: routing is by `math.floor(qty) != qty`, so a
        whole-share MARKET entry takes the OTO path and its stop is
        attached BEFORE the fill. Inferring a re-anchor here recorded a
        stop that did not exist at the broker."""
        tl = TradeLogger(path=tmp_csv)
        rec = tl.build_record(
            self._decision(qty=13.0),
            self._result(339.54, placed_stop=320.74),   # OTO child level
            modeled_price=336.93,
        )
        assert rec.initial_stop_loss == pytest.approx(320.74)
        assert rec.initial_risk_per_share == pytest.approx(18.81, abs=0.01)

    def test_unreported_path_falls_back_to_the_decision(self, tmp_csv):
        """A path that says nothing about the stop (legacy caller, recovery
        row) falls back rather than guessing a re-anchor happened."""
        tl = TradeLogger(path=tmp_csv)
        rec = tl.build_record(
            self._decision(),
            self._result(339.54, reported=False),
            modeled_price=336.93,
        )
        assert rec.initial_stop_loss == pytest.approx(320.74)
        assert rec.initial_risk_per_share == pytest.approx(18.81, abs=0.01)

    def test_fractional_under_one_share_records_no_stop(self, tmp_csv):
        """Review finding: Alpaca stops require whole shares, so a
        fractional entry below one share gets NO stop at all. Falling back
        to the decision's level would state that protection exists when it
        does not — the same lie `placed_stop_price` was added to remove.
        """
        tl = TradeLogger(path=tmp_csv)
        rec = tl.build_record(
            self._decision(qty=0.5),
            self._result(100.5, qty=0.5, placed_stop=None, reported=True),
            modeled_price=100.0,
        )
        assert rec.initial_stop_loss is None
        # No stop means no bounded risk — so no risk basis, and no R.
        assert rec.initial_risk_per_share is None
        assert rec.initial_risk_dollars is None

    def test_rejected_stop_submission_records_no_stop(self, tmp_csv):
        """Same contract when the broker rejects the stop: the position is
        unprotected until `_repair_missing_protective_stops` catches it,
        and the row must not claim otherwise."""
        tl = TradeLogger(path=tmp_csv)
        rec = tl.build_record(
            self._decision(qty=8.5),
            self._result(100.5, qty=8.5, placed_stop=None, reported=True),
            modeled_price=100.0,
        )
        assert rec.initial_stop_loss is None
        assert rec.initial_risk_per_share is None

    def test_reported_none_and_unreported_none_differ(self, tmp_csv):
        """The distinction the flag exists for: identical
        `placed_stop_price=None`, opposite meanings."""
        tl = TradeLogger(path=tmp_csv)
        no_stop = tl.build_record(
            self._decision(), self._result(339.54, reported=True),
            modeled_price=336.93,
        )
        unknown = tl.build_record(
            self._decision(), self._result(339.54, reported=False),
            modeled_price=336.93,
        )
        assert no_stop.initial_stop_loss is None
        assert unknown.initial_stop_loss == pytest.approx(320.74)

    def test_stop_limit_risk_is_fill_to_the_bracket_stop(self, tmp_csv):
        """The bracket stop stays where it was submitted, so the recorded
        risk must be `fill − stop`, not `reference − stop`."""
        from risk.manager import OrderType
        tl = TradeLogger(path=tmp_csv)
        d = self._decision(
            strategy_name="donchian_breakout",
            order_type=OrderType.STOP_LIMIT, limit_price=349.94,
            entry_trigger_price=333.75,
        )
        rec = tl.build_record(
            d, self._result(339.54, placed_stop=320.74), modeled_price=336.93,
        )
        # Stop is the bracket child — unchanged by the fill.
        assert rec.initial_stop_loss == pytest.approx(320.74)
        # Real room, not the 16.19 the old expression produced.
        assert rec.initial_risk_per_share == pytest.approx(18.81, abs=0.01)
        assert rec.initial_risk_dollars == pytest.approx(18.81 * 13, abs=0.2)

    def test_no_fill_price_falls_back_to_the_reference_offset(self, tmp_csv):
        from risk.manager import RiskDecision, Side
        tl = TradeLogger(path=tmp_csv)
        d = RiskDecision(
            symbol="AAPL", side=Side.BUY, qty=13.0,
            entry_reference_price=336.93, stop_price=320.74,
            strategy_name="sma_crossover", reason="entry",
        )
        res = OrderResult(
            status=OrderStatus.PARTIAL, order_id="oid", symbol="AAPL",
            requested_qty=13.0, filled_qty=13.0, avg_fill_price=None,
            raw_status="partially_filled",
        )
        rec = tl.build_record(d, res, modeled_price=336.93)
        assert rec.initial_risk_per_share == pytest.approx(16.19, abs=0.01)


class TestSlippageNeverPoolsAcrossInstruments:
    """PLAN 11.50 — basis points are a percentage of price, so equity and
    option figures cannot share a mean.

    An 8¢ execution miss is 2 bps on a $400 stock and 1,212 bps on a
    $0.66 contract. Pooling them made equity execution of 1.6 bps read as
    75.6 bps on the live trade log — a 15x apparent degradation that was
    purely an artefact of option contracts being cheap.
    """

    def _row(self, symbol, bps, oid):
        return {
            "symbol": symbol,
            "slippage_adverse_bps": bps,
            "slippage_benchmark_kind": "arrival_midpoint",
            "slippage_measurement_quality": "primary",
            "order_id": oid,
        }

    def test_equity_and_option_are_reported_separately(self):
        rows = [
            self._row("AAPL", 2.0, "a"),
            self._row("MSFT", 1.2, "b"),
            self._row("SPY260821C00738000", 1212.0, "c"),
        ]
        out = _slippage_by_instrument(rows)
        assert out["equity"]["count"] == 2
        assert out["equity"]["mean_bps"] == pytest.approx(1.6)
        assert out["option"]["count"] == 1
        assert out["option"]["mean_bps"] == pytest.approx(1212.0)
        # The pooled mean would have been ~405 — a number describing
        # neither instrument. It must not be derivable from the result.
        assert "mean_bps" not in out

    def test_option_volume_cannot_swamp_the_equity_figure(self):
        """The live shape: many cheap option rows, few expensive equity
        ones. Equity execution must stay legible."""
        rows = [self._row(f"EQ{i}", 1.6, f"e{i}") for i in range(3)]
        # Strikes vary so the symbols stay valid OCC (6-digit date, then
        # C/P, then an 8-digit strike) — an invalid symbol would silently
        # classify as equity and hide the very effect under test.
        rows += [
            self._row(f"SPY260821C{700 + i:05d}000", 88.6, f"o{i}")
            for i in range(17)
        ]
        out = _slippage_by_instrument(rows)
        assert out["equity"]["mean_bps"] == pytest.approx(1.6)
        assert out["option"]["mean_bps"] == pytest.approx(88.6)

    def test_class_with_no_measurements_is_absent_not_zero(self):
        """Same rule as the dashboard's blank cell: an absent measurement
        must not read as perfect execution."""
        out = _slippage_by_instrument([self._row("AAPL", 2.0, "a")])
        assert "option" not in out

    def test_unmeasured_rows_are_excluded_entirely(self):
        rows = [
            self._row("AAPL", 2.0, "a"),
            # LIMIT entry — no honest execution benchmark.
            {"symbol": "MSFT", "slippage_adverse_bps": None,
             "slippage_benchmark_kind": "limit_price",
             "slippage_measurement_quality": "unavailable", "order_id": "b"},
        ]
        out = _slippage_by_instrument(rows)
        assert out["equity"]["count"] == 1

    @pytest.mark.parametrize("symbol,expected", [
        ("AAPL", "equity"),
        ("SPY", "equity"),
        ("SPY260821C00738000", "option"),
        ("QQQ260814P00687000", "option"),
        (None, "equity"),
        ("", "equity"),
    ])
    def test_instrument_classification(self, symbol, expected):
        assert _instrument_class(symbol) == expected


class TestWeeklyReportDailyLinks:
    """Review finding: the weekly report's daily links were all broken.

    Markdown links resolve relative to the file containing them. The
    weekly report lives in `logs/weekly_reports/` while the dailies live
    in `logs/daily_pnl/`, so `[2026-07-31](2026-07-31.md)` pointed at
    `logs/weekly_reports/2026-07-31.md` — a path that never exists.

    Harmless while nothing generated this report; every link in the
    first live one (PR #91) was dead.
    """

    def _setup(self, tmp_path):
        daily = tmp_path / "daily"
        weekly = tmp_path / "weekly"
        daily.mkdir()
        for day in ("2026-07-31", "2026-08-01"):
            (daily / f"{day}.md").write_text(f"# Daily — {day}\n")
        return PnLTracker(
            trade_csv_path=str(tmp_path / "t.db"),
            daily_pnl_dir=str(daily),
            weekly_report_dir=str(weekly),
        ), daily, weekly

    def test_links_resolve_from_the_weekly_report_directory(self, tmp_path):
        tracker, daily, weekly = self._setup(tmp_path)
        path = tracker.generate_weekly_report(week_end="2026-08-01")
        assert path is not None
        body = Path(path).read_text()
        targets = re.findall(r"\]\(([^)]+\.md)\)", body)
        assert targets, "expected daily links in the report"
        for target in targets:
            resolved = (Path(path).parent / target).resolve()
            assert resolved.exists(), f"dead link: {target} -> {resolved}"

    def test_links_are_not_bare_filenames(self, tmp_path):
        """The specific regression: a bare `{day}.md` resolves inside the
        weekly directory, where nothing lives."""
        tracker, _, _ = self._setup(tmp_path)
        path = tracker.generate_weekly_report(week_end="2026-08-01")
        body = Path(path).read_text()
        assert "](2026-07-31.md)" not in body

    def test_links_follow_custom_directories(self, tmp_path):
        """Computed with `relpath`, not a hardcoded `../daily_pnl/`, so
        overriding either directory keeps the links correct."""
        tracker, daily, weekly = self._setup(tmp_path)
        path = tracker.generate_weekly_report(week_end="2026-08-01")
        body = Path(path).read_text()
        for target in re.findall(r"\]\(([^)]+\.md)\)", body):
            assert (Path(path).parent / target).resolve().parent == daily.resolve()


class TestRebaseEntryStop:
    """PLAN 11.54 — after the post-fill rebuild moves the protective stop to
    `fill - k*ATR`, the trade row must describe the stop that now exists.

    Two things break otherwise: r_multiple is measured against a cancelled
    bracket child, and `_repair_missing_protective_stops` restores the OLD
    level from `read_latest_open_stop_price` if the GTC stop ever
    disappears -- silently undoing the re-anchor.
    """

    def _seed(self, tl, *, order_id="entry-1", fill=426.09, qty=7.0,
              stop=386.95, symbol="AVGO"):
        """Write an entry row the way the AVGO 2026-08-07 fill did."""
        from risk.manager import RiskDecision, Side
        decision = RiskDecision(
            symbol=symbol, side=Side.BUY, qty=qty,
            entry_reference_price=420.68, stop_price=stop,
            strategy_name="donchian_breakout", reason="entry",
        )
        result = OrderResult(
            status=OrderStatus.FILLED, order_id=order_id, symbol=symbol,
            requested_qty=qty, filled_qty=qty, avg_fill_price=fill,
            raw_status="filled", placed_stop_price=stop,
            stop_placement_reported=True,
        )
        tl.log(tl.build_record(decision, result, modeled_price=420.68))
        return decision, result

    def _log_fill(self, tl, *, filled, stop, requested=33.0, fill=124.0309):
        """Log NOW's entry at a given cumulative filled quantity.

        `requested_qty` is constant — one broker order — while `filled_qty`
        advances. Tying them together (as `_seed` does) trips the 11.51
        identity guard, which is correct: it would look like two orders.
        """
        from risk.manager import RiskDecision, Side
        decision = RiskDecision(
            symbol="NOW", side=Side.BUY, qty=requested,
            entry_reference_price=124.03, stop_price=stop,
            strategy_name="donchian_breakout", reason="entry",
        )
        result = OrderResult(
            status=OrderStatus.FILLED if filled >= requested else OrderStatus.PARTIAL,
            order_id="entry-1", symbol="NOW", requested_qty=requested,
            filled_qty=filled, avg_fill_price=fill, raw_status="x",
            placed_stop_price=stop, stop_placement_reported=True,
        )
        tl.log(tl.build_record(decision, result, modeled_price=124.03))

    def _row(self, tl, order_id="entry-1"):
        return next(r for r in tl.read_all() if r["order_id"] == order_id)

    def test_rebases_stop_and_recomputes_the_risk_basis(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        self._seed(tl)
        assert self._row(tl)["initial_stop_loss"] == pytest.approx(386.95)

        assert tl.rebase_entry_stop(order_id="entry-1", new_stop_price=392.36)

        row = self._row(tl)
        assert row["stop_price"] == pytest.approx(392.36)
        assert row["initial_stop_loss"] == pytest.approx(392.36)
        # |426.09 - 392.36| = 33.73, i.e. exactly the 2xATR offset.
        assert row["initial_risk_per_share"] == pytest.approx(33.73, abs=0.01)
        assert row["initial_risk_dollars"] == pytest.approx(33.73 * 7, abs=0.1)

    def test_repair_lookup_returns_the_new_stop_afterwards(self, tmp_csv):
        """The consequence that matters most: if the GTC stop vanishes, the
        repair path must restore the re-anchored level, not the old one."""
        tl = TradeLogger(path=tmp_csv)
        self._seed(tl)
        before = tl.read_latest_open_stop_price(
            symbol="AVGO", strategy="donchian_breakout",
        )
        assert before == pytest.approx(386.95)

        tl.rebase_entry_stop(order_id="entry-1", new_stop_price=392.36)

        after = tl.read_latest_open_stop_price(
            symbol="AVGO", strategy="donchian_breakout",
        )
        assert after == pytest.approx(392.36)

    def test_risk_basis_matches_what_build_record_would_have_written(self, tmp_csv):
        """The rebased row must be indistinguishable from one written with
        the re-anchored stop in the first place -- otherwise the two paths
        disagree about the same position."""
        tl = TradeLogger(path=tmp_csv)
        self._seed(tl)
        tl.rebase_entry_stop(order_id="entry-1", new_stop_price=392.36)
        rebased = self._row(tl)

        fresh = TradeLogger(path=tmp_csv + ".2")
        self._seed(fresh, order_id="entry-2", stop=392.36)
        direct = self._row(fresh, "entry-2")

        for col in ("stop_price", "initial_stop_loss",
                    "initial_risk_per_share", "initial_risk_dollars"):
            assert rebased[col] == pytest.approx(direct[col]), col

    def test_rebase_mid_fill_records_what_is_filled_then_the_fill_corrects_it(
        self, tmp_csv
    ):
        """NOW 2026-08-10: the rebase fires on fill CONFIRMATION, not
        COMPLETION, so it can legitimately see a partial.

        The first fix for this preferred `requested_qty` to jump ahead to
        the eventual size. PR #105 review rejected that: if the remainder is
        cancelled, the row then claims risk that was never carried. The
        value follows the fills instead — `initial_risk_dollars` is
        LATEST-NON-NULL, so the completing fill rewrites it.
        """
        tl = TradeLogger(path=tmp_csv)
        # requested stays 33 throughout — the ORDER was always for 33.
        # Only filled moves, 24 -> 33. (`_seed` ties the two together, which
        # trips the 11.51 identity guard, so build the results here.)
        self._log_fill(tl, filled=24.0, stop=111.58)
        assert tl.rebase_entry_stop(order_id="entry-1", new_stop_price=110.7645)
        row = self._row(tl)
        rps = row["initial_risk_per_share"]
        assert row["initial_risk_dollars"] == pytest.approx(rps * 24.0, abs=0.01), (
            "mid-fill, the honest record is the 24 shares actually held"
        )

        # The completing fill arrives; the value must follow it up to 33.
        self._log_fill(tl, filled=33.0, stop=110.7645)
        row = self._row(tl)
        assert row["qty"] == pytest.approx(33.0)
        assert row["initial_risk_dollars"] == pytest.approx(
            row["initial_risk_per_share"] * 33.0, abs=0.01
        ), "the completing fill did not correct the recorded risk"
    def test_complete_fill_still_uses_the_filled_quantity(self, tmp_csv):
        """No behaviour change once the fill is done — requested and filled
        agree, so nothing is inferred."""
        tl = TradeLogger(path=tmp_csv)
        self._seed(tl)
        tl.rebase_entry_stop(order_id="entry-1", new_stop_price=392.36)
        row = self._row(tl)
        assert row["initial_risk_dollars"] == pytest.approx(
            row["qty"] * row["initial_risk_per_share"], abs=0.01)

    def test_recorded_risk_always_equals_qty_times_risk_per_share(self, tmp_csv):
        """The invariant: risk_dollars is a pure function of the other two
        columns, so a stored value that disagrees with them is the defect.

        Checked after a rebase AND after a subsequent fill, because the
        value now moves — a snapshot at one instant would not prove the
        invariant holds across the transition.
        """
        tl = TradeLogger(path=tmp_csv)
        self._log_fill(tl, filled=24.0, stop=111.58)
        tl.rebase_entry_stop(order_id="entry-1", new_stop_price=110.7645)
        row = self._row(tl)
        assert row["initial_risk_dollars"] == pytest.approx(
            row["qty"] * row["initial_risk_per_share"], abs=0.01)

        self._log_fill(tl, filled=33.0, stop=110.7645)
        row = self._row(tl)
        assert row["initial_risk_dollars"] == pytest.approx(
            row["qty"] * row["initial_risk_per_share"], abs=0.01)
    def test_unknown_order_id_is_a_no_op(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        self._seed(tl)
        assert tl.rebase_entry_stop(order_id="nope", new_stop_price=392.36) is False
        assert self._row(tl)["initial_stop_loss"] == pytest.approx(386.95)

    @pytest.mark.parametrize("bad", [0.0, -1.0])
    def test_non_positive_stop_is_refused(self, tmp_csv, bad):
        tl = TradeLogger(path=tmp_csv)
        self._seed(tl)
        assert tl.rebase_entry_stop(order_id="entry-1", new_stop_price=bad) is False
        assert self._row(tl)["initial_stop_loss"] == pytest.approx(386.95)

    def test_empty_order_id_is_refused(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        self._seed(tl)
        assert tl.rebase_entry_stop(order_id="", new_stop_price=392.36) is False

    def test_only_the_targeted_row_moves(self, tmp_csv):
        """Keyed on order_id so a later round trip on the same symbol is
        never touched."""
        tl = TradeLogger(path=tmp_csv)
        self._seed(tl, order_id="entry-1")
        self._seed(tl, order_id="entry-2", fill=430.00, stop=390.00)

        tl.rebase_entry_stop(order_id="entry-1", new_stop_price=392.36)

        assert self._row(tl, "entry-1")["initial_stop_loss"] == pytest.approx(392.36)
        assert self._row(tl, "entry-2")["initial_stop_loss"] == pytest.approx(390.00)


class TestPositionTypeIsRequired:
    """PLAN 11.51 — the trades UPSERT targets a PARTIAL unique index
    predicated on `position_type = 'single_leg'`.

    A partial index does not cover rows failing its predicate, so a row
    written with NULL there misses the conflict entirely: no error, no
    constraint violation, no log line — the INSERT succeeds as a SECOND
    row for an order_id that already exists, and both flow into replay,
    P&L attribution and slippage aggregates.

    Production was never hit (all five writers set it, zero NULL rows),
    so the exposure was always a FUTURE writer forgetting — which an
    optional field gave no feedback about.
    """

    def _kwargs(self, **over):
        base = dict(
            timestamp="2026-08-10T12:00:00+00:00", symbol="AAPL", side="buy",
            qty=10.0, avg_fill_price=100.0, order_id="oid-1",
            strategy="donchian_breakout", reason="entry", stop_price=95.0,
            entry_reference_price=100.0, modeled_slippage_bps=None,
            realized_slippage_bps=None, order_type="market", status="filled",
            requested_qty=10.0, filled_qty=10.0,
        )
        base.update(over)
        return base

    def test_omitting_position_type_is_a_construction_error(self):
        """The failure mode is forgetting, so it must fail at the earliest
        possible point rather than silently duplicating a row later."""
        with pytest.raises(TypeError, match="position_type"):
            TradeRecord(**self._kwargs())

    def test_explicit_none_is_refused_at_the_write(self):
        """Defence in depth: the dataclass stops omission, this stops a
        deliberate None. Dataclasses do not enforce types at runtime."""
        rec = TradeRecord(**self._kwargs(), position_type=None)
        tl = TradeLogger(path=":memory:")
        with pytest.raises(ValueError, match="position_type"):
            tl.log(rec)

    @pytest.mark.parametrize("bad", [
        "single-leg",    # hyphen instead of underscore
        "single_leg ",   # trailing whitespace
        "SINGLE_LEG",    # wrong case
        "singleleg",
        "junk",
        "",
    ])
    def test_invalid_values_are_refused_not_just_none(self, bad):
        """Review finding on PR #99. Rejecting only None left the same
        hole open with a different bad value: the UPSERT applies when
        `position_type = 'single_leg'` exactly, so ANY other string falls
        outside the partial index and appends a duplicate row.

        Measured before this guard: each of these produced 2 rows for one
        order_id where 'single_leg' produced 1.
        """
        rec = TradeRecord(**self._kwargs(), position_type=bad)
        tl = TradeLogger(path=":memory:")
        with pytest.raises(ValueError, match="position_type"):
            tl.log(rec)

    def test_invalid_value_would_otherwise_have_duplicated(self, tmp_path):
        """Pins WHY the value matters, not just that it is checked — a
        future refactor that relaxes this must fail here."""
        tl = TradeLogger(path=str(tmp_path / "t.db"))
        tl.log(TradeRecord(**self._kwargs(), position_type="single_leg"))
        tl.log(TradeRecord(**self._kwargs(), position_type="single_leg"))
        assert len([r for r in tl.read_all() if r["order_id"] == "oid-1"]) == 1

    def test_spread_legs_still_append_by_design(self, tmp_path):
        """Not a bug and must not be "fixed": two OCC legs of one combo
        share a broker order_id, so spread rows fall outside the partial
        index deliberately. The live DB's 21 duplicate order_ids are all
        spreads."""
        tl = TradeLogger(path=str(tmp_path / "t.db"))
        tl.log(TradeRecord(**self._kwargs(), position_type="spread"))
        tl.log(TradeRecord(**self._kwargs(), position_type="spread"))
        assert len([r for r in tl.read_all() if r["order_id"] == "oid-1"]) == 2

    def test_vocabulary_is_shared_with_the_position_model(self):
        """Imported, not redefined — `Position` already validates the same
        vocabulary and the two must not drift apart."""
        from engine.positions import VALID_POSITION_TYPES
        assert VALID_POSITION_TYPES == {"single_leg", "spread"}

    def test_single_leg_and_spread_both_construct(self):
        for pt in ("single_leg", "spread"):
            assert TradeRecord(**self._kwargs(), position_type=pt).position_type == pt

    def test_a_second_row_for_one_order_id_upserts_instead_of_duplicating(
        self, tmp_path
    ):
        """The behaviour the required field guarantees. Before it, a
        NULL-typed second write appended — demonstrated 2026-07-31, when a
        fixture without the field produced two `broker-entry` rows."""
        tl = TradeLogger(path=str(tmp_path / "t.db"))
        tl.log(TradeRecord(**self._kwargs(), position_type="single_leg"))
        tl.log(TradeRecord(**self._kwargs(filled_qty=10.0, status="filled"),
                           position_type="single_leg"))
        rows = [r for r in tl.read_all() if r["order_id"] == "oid-1"]
        assert len(rows) == 1, f"order_id duplicated into {len(rows)} rows"

    def test_every_production_writer_sets_it(self):
        """Parity guard. Five writers build a TradeRecord internally; a new
        one that forgets is now a construction error, but this also fails
        loudly if someone reintroduces a default."""
        import inspect
        from reporting import logger as lg

        src = inspect.getsource(lg)
        for writer in ("def build_record", "def build_close_record",
                       "def log_stop_fill", "def log_spread_fill",
                       "def log_external_close"):
            assert writer in src, f"{writer} vanished — update this guard"
        fields = TradeRecord.__dataclass_fields__["position_type"]
        import dataclasses
        assert fields.default is dataclasses.MISSING, (
            "position_type regained a default — the 11.51 hazard is back"
        )


class TestRiskBasisQty:
    """PLAN 11.58(a) round 2 — the partial-fill freeze, and the
    over-recording regression the first fix introduced.

    `initial_risk_dollars` records risk that was actually CARRIED. The
    intended size has its own column (`risk_budget_dollars`), so basing
    this one on `requested_qty` would make 11.48's "deployed vs budget"
    compare a number against itself.
    """

    def _decision(self):
        from risk.manager import RiskDecision, Side, OrderType
        return RiskDecision(
            symbol="SMCI", side=Side.BUY, qty=73, entry_reference_price=39.17,
            stop_price=33.83, strategy_name="donchian_breakout", reason="entry",
            order_type=OrderType.STOP_LIMIT, limit_price=40.0,
            entry_trigger_price=39.17,
        )

    def _result(self, status, filled):
        from execution.broker import OrderResult
        return OrderResult(status=status, order_id="ord-1", symbol="SMCI",
                           requested_qty=73, filled_qty=filled,
                           avg_fill_price=39.46, raw_status="x", message="")

    def _row(self, tmp_csv, sequence):
        import sqlite3
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=tmp_csv)
        for status, filled in sequence:
            tl.log(tl.build_record(self._decision(), self._result(status, filled),
                                   modeled_price=39.46))
        con = sqlite3.connect(tmp_csv); con.row_factory = sqlite3.Row
        return con.execute(
            "SELECT qty, initial_risk_per_share rps, initial_risk_dollars ird "
            "FROM trades"
        ).fetchone()

    def test_helper_returns_the_filled_quantity(self):
        from reporting.logger import risk_basis_qty
        assert risk_basis_qty(54) == 54.0
        assert risk_basis_qty(73) == 73.0
        assert risk_basis_qty(None) == 0.0

    def test_a_partial_that_completes_records_the_full_risk(self, tmp_csv):
        """SMCI 2026-08-14: filled 54 + 18 + 1 = 73, logged first at 54.
        The value must follow the fills to 73."""
        from execution.broker import OrderStatus
        r = self._row(tmp_csv, [(OrderStatus.PARTIAL, 54), (OrderStatus.FILLED, 73)])
        assert r["qty"] == 73
        assert r["ird"] == pytest.approx(r["rps"] * 73)

    def test_a_partial_that_is_cancelled_records_only_what_was_held(self, tmp_csv):
        """PR #105 review, P1. The first fix preferred `requested_qty`, so a
        54-of-73 fill whose remainder is cancelled claimed risk for 73 —
        overstating by 35% and understating that trade's R-multiple by the
        same amount. This is the case that exposes it."""
        from execution.broker import OrderStatus
        r = self._row(tmp_csv, [(OrderStatus.PARTIAL, 54), (OrderStatus.CANCELED, 54)])
        assert r["qty"] == 54
        assert r["ird"] == pytest.approx(r["rps"] * 54), (
            f"recorded {r['ird']} for 54 shares actually held — that is "
            f"{r['ird'] / r['rps']:.0f} shares of risk"
        )

    def test_multi_slice_fill_lands_on_the_final_quantity(self, tmp_csv):
        from execution.broker import OrderStatus
        r = self._row(tmp_csv, [(OrderStatus.PARTIAL, 54), (OrderStatus.PARTIAL, 72),
                                (OrderStatus.FILLED, 73)])
        assert r["ird"] == pytest.approx(r["rps"] * 73)

    def test_a_clean_full_fill_is_unaffected(self, tmp_csv):
        from execution.broker import OrderStatus
        r = self._row(tmp_csv, [(OrderStatus.FILLED, 73)])
        assert r["ird"] == pytest.approx(r["rps"] * 73)

    def test_nothing_filled_records_null_not_zero(self, tmp_csv):
        """A stored 0.0 survives IS NOT NULL guards and reads as a real
        measurement — the failure mode PLAN 11.58(b) tracks."""
        from execution.broker import OrderStatus
        r = self._row(tmp_csv, [(OrderStatus.ACCEPTED, 0)])
        assert r["ird"] is None

    def test_the_value_must_follow_fills_not_be_pinned(self):
        """The freeze is fixed by the upsert policy, not by guessing the
        eventual size. If this column were preserve-first-non-null again,
        the first partial would stick forever."""
        from reporting.logger import TradeLogger as T
        assert "initial_risk_dollars" in T._UPSERT_LATEST_NON_NULL_COLUMNS
        assert "initial_risk_dollars" not in T._UPSERT_COALESCE_COLUMNS

    def test_both_writers_use_the_same_basis(self):
        """The original defect was two call sites with contradictory rules."""
        import inspect
        from reporting import logger as L
        entry_src = inspect.getsource(L.TradeLogger.build_record)
        rebase_src = inspect.getsource(L.TradeLogger.rebase_entry_stop)
        assert "risk_basis_qty(" in entry_src
        assert "risk_basis_qty(" in rebase_src
        assert "filled_qty or result.requested_qty" not in entry_src


# ── TestOpenRiskLedger ──────────────────────────────────────────────────────


def _entry_row(*, symbol, strategy, qty, fill, stop, order_id, ird, seq=1,
               requested=None):
    """One entry row as the logger upserts it.

    `requested_qty` is an identity column and never changes across the
    order's lifetime; `filled_qty` is what grows tranche by tranche, and
    `initial_risk_dollars` is rewritten on each fill event.
    """
    return TradeRecord(
        timestamp=f"2026-08-0{seq}T13:30:00+00:00",
        symbol=symbol, side="buy", qty=qty, avg_fill_price=fill,
        order_id=order_id, strategy=strategy, reason="entry",
        stop_price=stop, entry_reference_price=fill,
        modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
        order_type="stop_limit", status="filled",
        requested_qty=requested if requested is not None else qty,
        filled_qty=qty,
        initial_stop_loss=stop,
        initial_risk_per_share=abs(fill - stop),
        initial_risk_dollars=ird,
        position_type="single_leg",
    )


class TestOpenRiskLedger:
    """PLAN 11.60 heat ledger — `read_open_risk_by_strategy_with_gaps`."""

    def test_multi_tranche_fill_uses_the_maintained_aggregate(self, tmp_csv):
        """The defect this guards: `initial_risk_per_share` is
        PRESERVE-FIRST-NON-NULL and freezes at the first tranche's price,
        while `initial_risk_dollars` is LATEST-NON-NULL and follows the
        fills. Reading per-share × final qty mis-states the position."""
        tl = TradeLogger(path=tmp_csv)
        # One order for 100. Tranche 1 fills 40 @ 100 (stop 90 -> rps 10).
        tl.log(_entry_row(symbol="AAA", strategy="donchian_breakout", qty=40,
                          requested=100, fill=100.0, stop=90.0,
                          order_id="e1", ird=400.0))
        # Tranche 2 completes it; cumulative 100 @ a 102 average -> ird 1200.
        tl.log(_entry_row(symbol="AAA", strategy="donchian_breakout", qty=100,
                          requested=100, fill=102.0, stop=90.0,
                          order_id="e1", ird=1200.0, seq=2))

        totals, gaps = tl.read_open_risk_by_strategy_with_gaps()

        assert gaps == {}
        # Correct: the maintained aggregate, 1200.
        # The frozen-per-share bug would give 10 (tranche 1) x 100 = 1000.
        assert totals["donchian_breakout"] == pytest.approx(1200.0)

    def test_partial_exit_pro_rates_the_contribution(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log(_entry_row(symbol="BBB", strategy="donchian_breakout", qty=100,
                          fill=100.0, stop=90.0, order_id="e2", ird=1000.0))
        # Sell 40 of 100 — 60% of the position remains.
        tl.log(TradeRecord(
            timestamp="2026-08-05T15:00:00+00:00",
            symbol="BBB", side="sell", qty=40, avg_fill_price=105.0,
            order_id="x2", strategy="donchian_breakout", reason="partial exit",
            stop_price=0.0, entry_reference_price=100.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
            order_type="market", status="filled",
            requested_qty=40, filled_qty=40, realized_pnl=200.0,
            entry_timestamp="2026-08-01T13:30:00+00:00",
            exit_timestamp="2026-08-05T15:00:00+00:00",
            position_type="single_leg",
        ))

        totals, _ = tl.read_open_risk_by_strategy_with_gaps()
        assert totals["donchian_breakout"] == pytest.approx(600.0)

    def test_position_without_recorded_risk_is_reported_as_a_gap(self, tmp_csv):
        """Risk that was never bounded must not read as zero."""
        tl = TradeLogger(path=tmp_csv)
        row = _entry_row(symbol="CCC", strategy="donchian_breakout", qty=10,
                         fill=100.0, stop=90.0, order_id="e3", ird=None)
        tl.log(row)

        totals, gaps = tl.read_open_risk_by_strategy_with_gaps()
        assert "donchian_breakout" not in totals
        assert gaps["donchian_breakout"] == ["CCC"]

    def test_fully_exited_position_contributes_nothing(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        tl.log(_entry_row(symbol="DDD", strategy="donchian_breakout", qty=10,
                          fill=100.0, stop=90.0, order_id="e4", ird=100.0))
        tl.log(TradeRecord(
            timestamp="2026-08-05T15:00:00+00:00",
            symbol="DDD", side="sell", qty=10, avg_fill_price=105.0,
            order_id="x4", strategy="donchian_breakout", reason="exit",
            stop_price=0.0, entry_reference_price=100.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10, realized_pnl=50.0,
            entry_timestamp="2026-08-01T13:30:00+00:00",
            exit_timestamp="2026-08-05T15:00:00+00:00",
            position_type="single_leg",
        ))
        totals, gaps = tl.read_open_risk_by_strategy_with_gaps()
        assert totals == {} and gaps == {}
