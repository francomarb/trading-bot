"""
Unit tests for the Phase 9.5 reconciliation module.

Covers:
  - ReconciliationResult structure and gate logic
  - Reconciler paper return computation from database fills
  - Per-trade divergence matching
  - Report generation
  - Go/no-go gate with threshold checks
  - get_closed_orders broker method
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backtest.reconcile import (
    Reconciler,
    ReconciliationResult,
    TradeDivergence,
)
from execution.broker import AlpacaBroker, OrderResult, OrderStatus
from reporting.logger import TRADE_COLUMNS, TradeLogger, TradeRecord
from strategies.base import BaseStrategy, OrderType, SignalFrame


# ── Fixtures ────────────────────────────────────────────────────────────────


class _DummyStrategy(BaseStrategy):
    name = "test_strat"
    preferred_order_type = OrderType.MARKET

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        return SignalFrame(
            entries=pd.Series(False, index=df.index, dtype=bool),
            exits=pd.Series(False, index=df.index, dtype=bool),
        )


@pytest.fixture
def tmp_csv(tmp_path: Path) -> str:
    return str(tmp_path / "trades.db")


@pytest.fixture
def tmp_forward_dir(tmp_path: Path) -> str:
    return str(tmp_path / "forward_tests")


def _write_trades(path: str, rows: list[dict]) -> None:
    """Write trade records to a SQLite database via TradeLogger."""
    tl = TradeLogger(path=path)
    for row in rows:
        record = TradeRecord(
            timestamp=row["timestamp"],
            symbol=row["symbol"],
            side=row["side"],
            qty=int(float(row["qty"])),
            avg_fill_price=float(row["avg_fill_price"]) if row.get("avg_fill_price") else None,
            order_id=row.get("order_id"),
            strategy=row["strategy"],
            reason=row.get("reason", ""),
            stop_price=float(row.get("stop_price", 0)),
            entry_reference_price=float(row.get("entry_reference_price", 0)),
            modeled_slippage_bps=float(row.get("modeled_slippage_bps", 0)),
            realized_slippage_bps=float(row.get("realized_slippage_bps", 0)),
            order_type=row.get("order_type", "market"),
            status=row.get("status", "filled"),
            requested_qty=int(float(row.get("requested_qty", row["qty"]))),
            filled_qty=int(float(row.get("filled_qty", row["qty"]))),
        )
        tl.log(record)


def _make_fill(
    symbol: str = "AAPL",
    side: str = "buy",
    price: float = 150.0,
    date: str = "2026-04-20",
    strategy: str = "test_strat",
    qty: int = 10,
) -> dict:
    """Build a minimal row dict for a filled trade."""
    return {
        "timestamp": f"{date}T15:30:00+00:00",
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "avg_fill_price": str(price),
        "order_id": "test-order-001",
        "strategy": strategy,
        "reason": "test",
        "stop_price": "145.0",
        "entry_reference_price": str(price),
        "modeled_slippage_bps": "0.0",
        "realized_slippage_bps": "3.5",
        "order_type": "market",
        "status": "filled",
        "requested_qty": str(qty),
        "filled_qty": str(qty),
    }


# ── TestReconciliationResult ────────────────────────────────────────────────


class TestReconciliationResult:
    def test_go_when_thresholds_met(self):
        r = ReconciliationResult(
            strategy_name="test",
            symbols=["AAPL"],
            start_date="2026-04-01",
            end_date="2026-04-30",
            paper_return_pct=5.0,
            backtest_return_pct=6.0,
            return_divergence_pct=1.0,
            paper_trade_count=10,
            backtest_trade_count=10,
            mean_slippage_bps=3.0,
            go=True,
            reasons=["all gates passed"],
        )
        assert r.go is True

    def test_no_go_on_return_divergence(self):
        r = ReconciliationResult(
            strategy_name="test",
            symbols=["AAPL"],
            start_date="2026-04-01",
            end_date="2026-04-30",
            paper_return_pct=5.0,
            backtest_return_pct=20.0,
            return_divergence_pct=15.0,
            paper_trade_count=10,
            backtest_trade_count=10,
            go=False,
            reasons=["return divergence exceeds threshold"],
        )
        assert r.go is False


# ── TestReconciler ──────────────────────────────────────────────────────────


class TestReconciler:
    def test_paper_return_from_csv(self, tmp_csv, tmp_forward_dir):
        """Buy at 150, sell at 160 → ~6.67% return."""
        rows = [
            _make_fill(side="buy", price=150.0, date="2026-04-20"),
            _make_fill(side="sell", price=160.0, date="2026-04-25"),
        ]
        _write_trades(tmp_csv, rows)

        recon = Reconciler(
            _DummyStrategy(),
            ["AAPL"],
            "2026-04-01",
            "2026-04-30",
            trade_csv_path=tmp_csv,
            forward_test_dir=tmp_forward_dir,
        )
        paper_return = recon._compute_paper_return(
            recon._read_paper_trades()
        )
        # (160 - 150) * 10 / (150 * 10) = 6.67%
        assert abs(paper_return - 0.0667) < 0.001

    def test_paper_return_no_fills(self, tmp_csv, tmp_forward_dir):
        _write_trades(tmp_csv, [])
        recon = Reconciler(
            _DummyStrategy(),
            ["AAPL"],
            "2026-04-01",
            "2026-04-30",
            trade_csv_path=tmp_csv,
            forward_test_dir=tmp_forward_dir,
        )
        assert recon._compute_paper_return([]) == 0.0

    def test_read_paper_trades_filters_by_date(self, tmp_csv, tmp_forward_dir):
        rows = [
            _make_fill(date="2026-04-15"),  # in range
            _make_fill(date="2026-03-01"),  # before range
            _make_fill(date="2026-05-15"),  # after range
        ]
        _write_trades(tmp_csv, rows)

        recon = Reconciler(
            _DummyStrategy(),
            ["AAPL"],
            "2026-04-01",
            "2026-04-30",
            trade_csv_path=tmp_csv,
            forward_test_dir=tmp_forward_dir,
        )
        trades = recon._read_paper_trades()
        assert len(trades) == 1

    def test_read_paper_trades_filters_by_symbol(self, tmp_csv, tmp_forward_dir):
        rows = [
            _make_fill(symbol="AAPL"),
            _make_fill(symbol="TSLA"),
        ]
        _write_trades(tmp_csv, rows)

        recon = Reconciler(
            _DummyStrategy(),
            ["AAPL"],
            "2026-04-01",
            "2026-04-30",
            trade_csv_path=tmp_csv,
            forward_test_dir=tmp_forward_dir,
        )
        trades = recon._read_paper_trades()
        assert len(trades) == 1
        assert trades[0]["symbol"] == "AAPL"

    @patch("backtest.reconcile.fetch_symbol")
    @patch("backtest.reconcile.run_backtest")
    def test_run_produces_result(
        self, mock_bt, mock_fetch, tmp_csv, tmp_forward_dir
    ):
        """Full run with mocked backtest."""
        rows = [
            _make_fill(side="buy", price=150.0, date="2026-04-20"),
            _make_fill(side="sell", price=155.0, date="2026-04-25"),
        ]
        _write_trades(tmp_csv, rows)

        # Mock fetch + backtest.
        idx = pd.date_range("2026-04-01", periods=30, freq="D", tz="UTC")
        mock_df = pd.DataFrame(
            {"open": 150.0, "high": 152.0, "low": 148.0, "close": 151.0, "volume": 1000},
            index=idx,
        )
        mock_fetch.return_value = (mock_df, {})

        mock_pf = MagicMock()
        mock_pf.trades.records_readable = pd.DataFrame({
            "Entry Price": [150.0],
            "Exit Price": [155.0],
            "PnL": [50.0],
        })
        mock_bt_result = MagicMock()
        mock_bt_result.stats = {"total_return": 0.033, "trade_count": 1}
        mock_bt_result.portfolio = mock_pf
        mock_bt.return_value = mock_bt_result

        recon = Reconciler(
            _DummyStrategy(),
            ["AAPL"],
            "2026-04-01",
            "2026-04-30",
            trade_csv_path=tmp_csv,
            forward_test_dir=tmp_forward_dir,
            return_divergence_threshold=0.20,
            max_slippage_threshold=50.0,
        )
        result = recon.run()

        assert result.paper_trade_count == 2
        assert result.backtest_trade_count == 1
        assert result.go is True
        assert "all gates passed" in result.reasons

    @patch("backtest.reconcile.fetch_symbol")
    @patch("backtest.reconcile.run_backtest")
    def test_no_go_on_high_slippage(
        self, mock_bt, mock_fetch, tmp_csv, tmp_forward_dir
    ):
        # Paper fills with high slippage.
        rows = [
            {
                **_make_fill(side="buy", price=150.0),
                "realized_slippage_bps": "30.0",  # above threshold
            },
        ]
        _write_trades(tmp_csv, rows)

        mock_fetch.return_value = (pd.DataFrame(), {})

        recon = Reconciler(
            _DummyStrategy(),
            ["AAPL"],
            "2026-04-01",
            "2026-04-30",
            trade_csv_path=tmp_csv,
            forward_test_dir=tmp_forward_dir,
            return_divergence_threshold=0.50,
            max_slippage_threshold=20.0,
        )
        result = recon.run()
        assert result.go is False
        assert any("slippage" in r for r in result.reasons)

    def test_no_go_on_no_fills(self, tmp_csv, tmp_forward_dir):
        _write_trades(tmp_csv, [])
        recon = Reconciler(
            _DummyStrategy(),
            ["AAPL"],
            "2026-04-01",
            "2026-04-30",
            trade_csv_path=tmp_csv,
            forward_test_dir=tmp_forward_dir,
        )
        result = recon.run()
        assert result.go is False
        assert any("no paper fills" in r for r in result.reasons)

    @patch("backtest.reconcile.fetch_symbol")
    @patch("backtest.reconcile.run_backtest")
    def test_write_report(
        self, mock_bt, mock_fetch, tmp_csv, tmp_forward_dir
    ):
        rows = [
            _make_fill(side="buy", price=150.0, date="2026-04-20"),
        ]
        _write_trades(tmp_csv, rows)
        mock_fetch.return_value = (pd.DataFrame(), {})

        recon = Reconciler(
            _DummyStrategy(),
            ["AAPL"],
            "2026-04-01",
            "2026-04-30",
            trade_csv_path=tmp_csv,
            forward_test_dir=tmp_forward_dir,
        )
        result = recon.run()
        path = recon.write_report(result)

        assert os.path.exists(path)
        content = open(path).read()
        assert "Forward-Test Reconciliation" in content
        assert "test_strat" in content


# ── TestTradeDivergence ─────────────────────────────────────────────────────


class TestTradeDivergence:
    def test_matched_divergence(self):
        d = TradeDivergence(
            symbol="AAPL",
            side="buy",
            paper_date="2026-04-20",
            paper_price=150.0,
            backtest_price=150.5,
            price_diff_bps=33.3,
            matched=True,
        )
        assert d.matched
        assert d.price_diff_bps == 33.3

    def test_unmatched_divergence(self):
        d = TradeDivergence(
            symbol="AAPL",
            side="buy",
            paper_date="2026-04-20",
            paper_price=150.0,
            backtest_price=None,
            price_diff_bps=0.0,
            matched=False,
        )
        assert not d.matched


# ── TestGetClosedOrders ─────────────────────────────────────────────────────


class TestGetClosedOrders:
    def test_returns_order_results(self):
        mock_client = MagicMock()
        mock_order = MagicMock()
        mock_order.id = "order-1"
        mock_order.symbol = "AAPL"
        mock_order.qty = "10"
        mock_order.filled_qty = "10"
        mock_order.filled_avg_price = "150.05"
        mock_order.status = "filled"
        mock_order.side = "buy"
        mock_client.get_orders.return_value = [mock_order]

        broker = AlpacaBroker(client=mock_client)
        results = broker.get_closed_orders()

        assert len(results) == 1
        assert results[0].status == OrderStatus.FILLED
        assert results[0].symbol == "AAPL"
        assert results[0].filled_qty == 10
        assert results[0].avg_fill_price == 150.05

    def test_filters_by_symbols(self):
        mock_client = MagicMock()
        o1 = MagicMock()
        o1.id, o1.symbol, o1.qty, o1.filled_qty = "1", "AAPL", "10", "10"
        o1.filled_avg_price, o1.status, o1.side = "150.0", "filled", "buy"
        o2 = MagicMock()
        o2.id, o2.symbol, o2.qty, o2.filled_qty = "2", "TSLA", "5", "5"
        o2.filled_avg_price, o2.status, o2.side = "200.0", "filled", "buy"
        mock_client.get_orders.return_value = [o1, o2]

        broker = AlpacaBroker(client=mock_client)
        results = broker.get_closed_orders(symbols=["AAPL"])

        assert len(results) == 1
        assert results[0].symbol == "AAPL"

    def test_empty_history(self):
        mock_client = MagicMock()
        mock_client.get_orders.return_value = []
        broker = AlpacaBroker(client=mock_client)
        assert broker.get_closed_orders() == []
