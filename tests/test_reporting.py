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
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
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
    TRADE_CSV_COLUMNS,
    TradeLogger,
    TradeRecord,
    install_json_sink,
    mleg_realized_slippage_bps,
)
from reporting.pnl import DailySummary, PnLTracker, StrategyStats
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
        assert record.initial_stop_loss == 145.0
        assert record.initial_risk_per_share == 5.0
        assert record.initial_risk_dollars == 50.0
        assert record.entry_timestamp is not None
        assert record.exit_timestamp is None

    def test_slippage_calculation(self, sample_decision, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)
        # |150.05 - 150.0| / 150.0 * 10_000 = 3.33 bps
        assert record.modeled_slippage_bps == 0.0
        assert abs(record.realized_slippage_bps - 3.33) < 0.1

    def test_append_multiple_records(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        for _ in range(3):
            record = tl.build_record(sample_decision, sample_result)
            tl.log(record)
        rows = tl.read_all()
        assert len(rows) == 3

    def test_read_all_empty(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        assert tl.read_all() == []

    def test_build_close_record(self, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_close_record(
            sample_result, strategy_name="sma_crossover", modeled_price=150.0
        )
        assert record.side == "sell"
        assert record.strategy == "sma_crossover"
        assert record.reason == "exit signal"
        assert record.stop_price == 0.0
        assert record.exit_timestamp is not None

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
        assert record.realized_pnl == pytest.approx(100.0)
        assert record.initial_risk_dollars == pytest.approx(50.0)
        assert record.r_multiple == pytest.approx(2.0)

    def test_read_latest_open_entry_context_public_wrapper(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        tl.log(tl.build_record(sample_decision, sample_result, modeled_price=150.0))

        context = tl.read_latest_open_entry_context(
            symbol="AAPL",
            strategy="sma_crossover",
        )

        assert context is not None
        assert context["entry_reference_price"] == pytest.approx(150.0)
        assert context["initial_risk_per_share"] == pytest.approx(5.0)

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
        assert tl.has_recorded_order_id("stop-goog-1") is True

    def test_read_strategy_realized_pnl_summary_reconstructs_hwm(self, tmp_csv):
        tl = TradeLogger(path=tmp_csv)
        rows = [
            TradeRecord(
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
        assert summary["rsi_reversion"] == {"realized_pnl": 0.0, "hwm": 0.0}

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
        assert record.initial_risk_per_share == pytest.approx(1.0)
        assert record.initial_risk_dollars == pytest.approx(200.0)

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
        assert record.initial_risk_dollars == pytest.approx(200.0)
        assert record.realized_pnl == pytest.approx(200.0)
        assert record.r_multiple == pytest.approx(1.0)

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
        assert stop_record["initial_risk_dollars"] == pytest.approx(200.0)
        assert stop_record["realized_pnl"] == pytest.approx(-200.0)
        assert stop_record["r_multiple"] == pytest.approx(-1.0)

    def test_as_dict_has_all_columns(self, sample_decision, sample_result):
        tl = TradeLogger(path="/dev/null")
        record = tl.build_record(sample_decision, sample_result)
        d = record.as_dict()
        assert set(d.keys()) == set(TRADE_CSV_COLUMNS)

    def test_no_fill_price_zero_slippage(self, sample_decision):
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
        assert record.realized_slippage_bps == 0.0

    def test_read_trades_in_range(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        # Log 3 records — all will have today's timestamp
        for _ in range(3):
            record = tl.build_record(sample_decision, sample_result)
            tl.log(record)
        today = record.timestamp[:10]
        rows = tl.read_trades_in_range(today, today)
        assert len(rows) == 3
        # Out-of-range returns nothing
        assert tl.read_trades_in_range("1999-01-01", "1999-12-31") == []

    def test_read_recent(self, tmp_csv, sample_decision, sample_result):
        tl = TradeLogger(path=tmp_csv)
        for _ in range(5):
            record = tl.build_record(sample_decision, sample_result)
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

        assert by_symbol[self._SHORT]["entry_reference_price"] == pytest.approx(1.45)
        assert by_symbol[self._SHORT]["realized_slippage_bps"] == pytest.approx(-344.83)
        assert by_symbol[self._LONG]["realized_slippage_bps"] == pytest.approx(0.0)

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
        assert by_symbol[self._SHORT]["realized_slippage_bps"] == pytest.approx(500.0)
        assert by_symbol[self._LONG]["realized_slippage_bps"] == pytest.approx(0.0)

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


# ── TestPnLTracker ──────────────────────────────────────────────────────────


class TestPnLTracker:
    def test_record_trade_pnl(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("sma_crossover", 100.0, today="2026-04-16")
        tracker.record_trade_pnl("sma_crossover", -50.0, today="2026-04-16")

        summary = tracker.generate_daily_summary(day="2026-04-16")
        assert summary.total_trades == 2
        assert summary.realized_pnl == 50.0
        assert summary.largest_win == 100.0
        assert summary.largest_loss == -50.0

    def test_per_strategy_attribution(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("strat_a", 200.0, today="2026-04-16")
        tracker.record_trade_pnl("strat_a", -80.0, today="2026-04-16")
        tracker.record_trade_pnl("strat_b", 50.0, today="2026-04-16")

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

    def test_intraday_drawdown(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("s", 100.0, today="2026-04-16")
        tracker.record_trade_pnl("s", -150.0, today="2026-04-16")  # peak 100, now -50
        tracker.record_trade_pnl("s", 80.0, today="2026-04-16")   # now 30

        summary = tracker.generate_daily_summary(day="2026-04-16")
        # Max drawdown from peak=100 to trough=-50 = 150
        assert summary.max_intraday_drawdown == 150.0

    def test_day_reset(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("s", 100.0, today="2026-04-16")
        tracker.record_trade_pnl("s", -200.0, today="2026-04-17")  # new day

        summary = tracker.generate_daily_summary(day="2026-04-17")
        assert summary.total_trades == 1
        assert summary.realized_pnl == -200.0
        assert summary.max_intraday_drawdown == 200.0

    def test_write_daily_report(self, tmp_csv, tmp_daily_dir):
        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        tracker.record_trade_pnl("sma_crossover", 100.0, today="2026-04-16")

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
        assert report["mean_bps"] == 0.0

    def test_slippage_report_from_db(self, tmp_csv, tmp_daily_dir, sample_decision, sample_result):
        # Write some trade records to the database first.
        tl = TradeLogger(path=tmp_csv)
        for _ in range(5):
            record = tl.build_record(sample_decision, sample_result, modeled_price=150.0)
            tl.log(record)

        tracker = PnLTracker(
            trade_csv_path=tmp_csv, daily_pnl_dir=tmp_daily_dir
        )
        report = tracker.slippage_report(last_n=5)
        assert report["count"] == 5
        assert report["mean_bps"] > 0

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
        tracker.record_trade_pnl("s", 100.0, today="2026-04-16")
        summary = tracker.generate_daily_summary(day="2026-04-16")
        tracker.write_daily_report(summary)

        path = tracker.generate_weekly_report(week_end="2026-04-16")
        assert path is not None
        assert os.path.exists(path)
        content = open(path).read()
        assert "Weekly Report" in content
        assert "2026-04-16" in content


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
        dispatcher.engine_halt("hard dollar cap")
        dispatcher.slippage_drift(15.0, 3.0)
        dispatcher.loss_streak_cooldown("sma", 3, 24.0)

        assert len(backend.alerts) == 8
        types = {a.alert_type for a in backend.alerts}
        assert AlertType.ORDER_REJECTION in types
        assert AlertType.CIRCUIT_BREAKER in types
        assert AlertType.STALE_DATA in types
        assert AlertType.BROKER_ERROR in types
        assert AlertType.BROKER_INFO in types
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
