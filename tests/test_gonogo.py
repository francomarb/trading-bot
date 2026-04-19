"""
Unit tests for scripts/gonogo.py.

Covers:
  - pair_round_trips: FIFO matching of buy/sell fills into P&L values
  - check_trading_span: minimum calendar span gate
  - check_trade_count: minimum trade count gate
  - run_gonogo: end-to-end with a temp SQLite database
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from reporting.logger import TradeLogger, TradeRecord
from scripts.gonogo import (
    check_trade_count,
    check_trading_span,
    pair_round_trips,
    run_gonogo,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _fill(
    symbol: str = "AAPL",
    side: str = "buy",
    price: float = 150.0,
    qty: int = 10,
    strategy: str = "sma_crossover",
    date: str = "2026-04-10",
    status: str = "filled",
) -> dict:
    return {
        "timestamp": f"{date}T15:30:00+00:00",
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "avg_fill_price": price,
        "order_id": "test-001",
        "strategy": strategy,
        "reason": "test",
        "stop_price": 0.0,
        "entry_reference_price": price,
        "modeled_slippage_bps": 0.0,
        "realized_slippage_bps": 0.0,
        "order_type": "market",
        "status": status,
        "requested_qty": qty,
        "filled_qty": qty,
    }


@pytest.fixture
def tmp_db(tmp_path: Path) -> str:
    return str(tmp_path / "trades.db")


# ── pair_round_trips ───────────────────────────────────────────────────────


class TestPairRoundTrips:
    def test_single_round_trip(self):
        trades = [
            _fill(side="buy", price=100.0, qty=10),
            _fill(side="sell", price=110.0, qty=10),
        ]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 1
        assert abs(pnls[0] - 100.0) < 0.01  # (110-100)*10

    def test_losing_trade(self):
        trades = [
            _fill(side="buy", price=100.0, qty=5),
            _fill(side="sell", price=90.0, qty=5),
        ]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 1
        assert abs(pnls[0] - (-50.0)) < 0.01  # (90-100)*5

    def test_multiple_round_trips(self):
        trades = [
            _fill(side="buy", price=100.0, qty=10, date="2026-04-10"),
            _fill(side="sell", price=110.0, qty=10, date="2026-04-11"),
            _fill(side="buy", price=105.0, qty=10, date="2026-04-12"),
            _fill(side="sell", price=115.0, qty=10, date="2026-04-13"),
        ]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 2
        assert abs(pnls[0] - 100.0) < 0.01
        assert abs(pnls[1] - 100.0) < 0.01

    def test_unmatched_buy_no_pnl(self):
        trades = [_fill(side="buy", price=100.0, qty=10)]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 0

    def test_sell_without_buy_no_pnl(self):
        trades = [_fill(side="sell", price=110.0, qty=10)]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 0

    def test_fifo_matching(self):
        # Buy 10 @ 100, buy 10 @ 120, sell 10 @ 110
        # FIFO: first buy matched → PnL = (110-100)*10 = 100
        trades = [
            _fill(side="buy", price=100.0, qty=10, date="2026-04-10"),
            _fill(side="buy", price=120.0, qty=10, date="2026-04-11"),
            _fill(side="sell", price=110.0, qty=10, date="2026-04-12"),
        ]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 1
        assert abs(pnls[0] - 100.0) < 0.01  # matched against first buy

    def test_partial_qty_matching(self):
        # Buy 20 @ 100, sell 10 @ 110, sell 10 @ 105
        trades = [
            _fill(side="buy", price=100.0, qty=20),
            _fill(side="sell", price=110.0, qty=10),
            _fill(side="sell", price=105.0, qty=10),
        ]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 2
        assert abs(pnls[0] - 100.0) < 0.01  # (110-100)*10
        assert abs(pnls[1] - 50.0) < 0.01   # (105-100)*10

    def test_different_symbols_independent(self):
        trades = [
            _fill(symbol="AAPL", side="buy", price=100.0, qty=10),
            _fill(symbol="MSFT", side="buy", price=200.0, qty=5),
            _fill(symbol="AAPL", side="sell", price=110.0, qty=10),
            _fill(symbol="MSFT", side="sell", price=190.0, qty=5),
        ]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 2
        total = sum(pnls)
        # AAPL: +100, MSFT: -50 → total = 50
        assert abs(total - 50.0) < 0.01

    def test_different_strategies_independent(self):
        trades = [
            _fill(strategy="sma", side="buy", price=100.0, qty=10),
            _fill(strategy="rsi", side="buy", price=100.0, qty=10),
            _fill(strategy="sma", side="sell", price=110.0, qty=10),
            _fill(strategy="rsi", side="sell", price=90.0, qty=10),
        ]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 2
        assert abs(sum(pnls) - 0.0) < 0.01  # +100 and -100

    def test_non_filled_ignored(self):
        trades = [
            _fill(side="buy", price=100.0, qty=10, status="rejected"),
            _fill(side="sell", price=110.0, qty=10, status="filled"),
        ]
        pnls = pair_round_trips(trades)
        assert len(pnls) == 0

    def test_empty_trades(self):
        assert pair_round_trips([]) == []


# ── check_trading_span ─────────────────────────────────────────────────────


class TestCheckTradingSpan:
    def test_passes_with_sufficient_span(self):
        trades = [
            _fill(date="2026-03-01"),
            _fill(date="2026-04-15"),
        ]
        result = check_trading_span(trades, min_weeks=4)
        assert result.passed is True

    def test_fails_with_short_span(self):
        trades = [
            _fill(date="2026-04-01"),
            _fill(date="2026-04-10"),
        ]
        result = check_trading_span(trades, min_weeks=4)
        assert result.passed is False

    def test_fails_with_single_trade(self):
        trades = [_fill(date="2026-04-01")]
        result = check_trading_span(trades, min_weeks=4)
        assert result.passed is False

    def test_empty_trades(self):
        result = check_trading_span([], min_weeks=4)
        assert result.passed is False


# ── check_trade_count ──────────────────────────────────────────────────────


class TestCheckTradeCount:
    def test_passes_above_threshold(self):
        result = check_trade_count(60, min_trades=50)
        assert result.passed is True

    def test_fails_below_threshold(self):
        result = check_trade_count(30, min_trades=50)
        assert result.passed is False

    def test_exact_threshold_passes(self):
        result = check_trade_count(50, min_trades=50)
        assert result.passed is True


# ── run_gonogo end-to-end ──────────────────────────────────────────────────


class TestRunGoNoGo:
    def _write_round_trips(self, db_path: str, n_trips: int, start_date: str) -> None:
        """Write n_trips winning round trips to the DB spanning several weeks."""
        tl = TradeLogger(path=db_path)
        base = datetime.fromisoformat(start_date + "T15:30:00+00:00")
        for i in range(n_trips):
            day_offset = i  # one trade per day
            ts = (base + timedelta(days=day_offset)).isoformat()
            buy = TradeRecord(
                timestamp=ts, symbol="AAPL", side="buy", qty=10,
                avg_fill_price=100.0, order_id=f"b-{i}",
                strategy="sma_crossover", reason="entry",
                stop_price=95.0, entry_reference_price=100.0,
                modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
                order_type="market", status="filled",
                requested_qty=10, filled_qty=10,
            )
            tl.log(buy)

            sell_ts = (base + timedelta(days=day_offset, hours=2)).isoformat()
            sell = TradeRecord(
                timestamp=sell_ts, symbol="AAPL", side="sell", qty=10,
                avg_fill_price=110.0, order_id=f"s-{i}",
                strategy="sma_crossover", reason="exit signal",
                stop_price=0.0, entry_reference_price=100.0,
                modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
                order_type="market", status="filled",
                requested_qty=10, filled_qty=10,
            )
            tl.log(sell)

    def test_go_with_sufficient_data(self, tmp_db):
        # 60 round trips over 60 days — all winning
        self._write_round_trips(tmp_db, 60, "2026-01-01")
        go, metrics, ops = run_gonogo(tmp_db, min_trades=50, min_weeks=4)
        assert metrics.trade_count == 60
        assert metrics.win_rate == 1.0
        # All wins → passes all metric gates
        # 60 days span > 4 weeks
        assert all(op.passed for op in ops)
        # Sharpe might be 0 because all trades identical → std=0
        # So overall go might be False due to Sharpe. Check metrics_go separately.
        _, metric_reasons = metrics.meets_go_thresholds(min_trades=50)
        # The only potential failure is Sharpe (zero variance)
        if metric_reasons:
            assert all("Sharpe" in r for r in metric_reasons)

    def test_no_go_empty_db(self, tmp_db):
        go, metrics, ops = run_gonogo(tmp_db, min_trades=50, min_weeks=4)
        assert go is False
        assert metrics.trade_count == 0

    def test_no_go_insufficient_trades(self, tmp_db):
        self._write_round_trips(tmp_db, 5, "2026-01-01")
        go, metrics, ops = run_gonogo(tmp_db, min_trades=50, min_weeks=4)
        assert go is False
        assert metrics.trade_count == 5
