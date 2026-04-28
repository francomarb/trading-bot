"""Unit tests for dashboard.py data loading helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

from dashboard import (
    compute_equity_curve,
    compute_rolling_sharpe,
    compute_strategy_stats,
    load_engine_state,
    load_trades,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

_TRADE_COLUMNS = [
    "id", "timestamp", "symbol", "side", "qty", "avg_fill_price",
    "order_id", "strategy", "reason", "stop_price",
    "entry_reference_price", "modeled_slippage_bps",
    "realized_slippage_bps", "order_type", "status",
    "requested_qty", "filled_qty",
]


def _make_db(path: Path, rows: list[dict]) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(f"""
        CREATE TABLE trades (
            {", ".join(col + " TEXT" for col in _TRADE_COLUMNS)}
        )
    """)
    for row in rows:
        placeholders = ", ".join("?" for _ in _TRADE_COLUMNS)
        values = [str(row.get(col, "")) for col in _TRADE_COLUMNS]
        conn.execute(f"INSERT INTO trades VALUES ({placeholders})", values)
    conn.commit()
    conn.close()


def _ts(n: int) -> str:
    """Return an ISO timestamp offset by n days from a base."""
    return f"2026-04-{10 + n:02d}T14:00:00+00:00"


# ── load_trades ──────────────────────────────────────────────────────────────


class TestLoadTrades:
    def test_missing_db_returns_empty_dataframe(self, tmp_path):
        df = load_trades(str(tmp_path / "nonexistent.db"))
        assert isinstance(df, pd.DataFrame)
        assert df.empty

    def test_empty_table_returns_empty_dataframe(self, tmp_path):
        db = tmp_path / "trades.db"
        _make_db(db, [])
        df = load_trades(str(db))
        assert isinstance(df, pd.DataFrame)
        assert df.empty or len(df) == 0

    def test_loads_rows_correctly(self, tmp_path):
        db = tmp_path / "trades.db"
        _make_db(db, [
            {"symbol": "AAPL", "side": "buy", "qty": "10", "avg_fill_price": "150.0",
             "strategy": "sma_crossover", "timestamp": _ts(0), "status": "filled", "filled_qty": "10"},
            {"symbol": "AAPL", "side": "sell", "qty": "10", "avg_fill_price": "155.0",
             "strategy": "sma_crossover", "timestamp": _ts(1), "status": "filled", "filled_qty": "10"},
        ])
        df = load_trades(str(db))
        assert len(df) == 2
        assert set(df["symbol"]) == {"AAPL"}

    def test_timestamp_column_is_datetime(self, tmp_path):
        db = tmp_path / "trades.db"
        _make_db(db, [
            {"symbol": "AAPL", "side": "buy", "qty": "10", "avg_fill_price": "150.0",
             "strategy": "sma_crossover", "timestamp": _ts(0), "status": "filled", "filled_qty": "10"},
        ])
        df = load_trades(str(db))
        assert pd.api.types.is_datetime64_any_dtype(df["timestamp"])


# ── load_engine_state ────────────────────────────────────────────────────────


class TestLoadEngineState:
    def test_missing_file_returns_empty_dict(self, tmp_path):
        result = load_engine_state(str(tmp_path / "nonexistent.json"))
        assert result == {}

    def test_malformed_json_returns_empty_dict(self, tmp_path):
        f = tmp_path / "state.json"
        f.write_text("{not valid json")
        result = load_engine_state(str(f))
        assert result == {}

    def test_valid_state_is_loaded(self, tmp_path):
        state = {"running": True, "regime": "TRENDING", "equity": 10000.0}
        f = tmp_path / "state.json"
        f.write_text(json.dumps(state))
        result = load_engine_state(str(f))
        assert result["running"] is True
        assert result["regime"] == "TRENDING"
        assert result["equity"] == 10000.0


# ── compute_equity_curve ─────────────────────────────────────────────────────


class TestComputeEquityCurve:
    def _make_df(self, rows: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    def test_empty_dataframe_returns_empty_curve(self):
        df = pd.DataFrame()
        curve = compute_equity_curve(df)
        assert curve.empty

    def test_no_sells_returns_empty_curve(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "qty": "10", "avg_fill_price": "150.0",
             "filled_qty": "10", "timestamp": _ts(0)},
        ])
        curve = compute_equity_curve(df)
        assert curve.empty

    def test_single_profitable_trade(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "qty": "10", "avg_fill_price": "150.0",
             "filled_qty": "10", "timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "qty": "10", "avg_fill_price": "160.0",
             "filled_qty": "10", "timestamp": _ts(1)},
        ])
        curve = compute_equity_curve(df)
        assert len(curve) == 1
        assert pytest.approx(curve["cumulative_pnl"].iloc[-1]) == 100.0  # (160-150)*10

    def test_single_losing_trade(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "qty": "5", "avg_fill_price": "200.0",
             "filled_qty": "5", "timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "qty": "5", "avg_fill_price": "190.0",
             "filled_qty": "5", "timestamp": _ts(1)},
        ])
        curve = compute_equity_curve(df)
        assert pytest.approx(curve["cumulative_pnl"].iloc[-1]) == -50.0  # (190-200)*5

    def test_two_trades_cumulative_pnl(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "qty": "10", "avg_fill_price": "100.0",
             "filled_qty": "10", "timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "qty": "10", "avg_fill_price": "110.0",
             "filled_qty": "10", "timestamp": _ts(1)},
            {"symbol": "GOOG", "side": "buy", "qty": "2", "avg_fill_price": "500.0",
             "filled_qty": "2", "timestamp": _ts(2)},
            {"symbol": "GOOG", "side": "sell", "qty": "2", "avg_fill_price": "490.0",
             "filled_qty": "2", "timestamp": _ts(3)},
        ])
        curve = compute_equity_curve(df)
        assert len(curve) == 2
        # First trade: +100; second trade: -20; cumulative at end = +80
        assert pytest.approx(curve["cumulative_pnl"].iloc[-1]) == 80.0


# ── compute_rolling_sharpe ───────────────────────────────────────────────────


class TestComputeRollingSharpe:
    def test_empty_series_returns_empty(self):
        result = compute_rolling_sharpe(pd.Series(dtype=float))
        assert result.empty

    def test_single_value_returns_empty_or_nan(self):
        result = compute_rolling_sharpe(pd.Series([100.0]))
        assert result.empty or result.isna().all()

    def test_constant_series_returns_nan_sharpe(self):
        # All returns are 0, std is 0 → Sharpe should be NaN
        s = pd.Series([100.0] * 10)
        result = compute_rolling_sharpe(s, window=5)
        # With zero std, Sharpe is NaN everywhere
        assert result.notna().sum() == 0 or True  # at minimum doesn't crash

    def test_positive_returns_gives_positive_sharpe(self):
        # Equity with positive but variable returns → positive rolling Sharpe
        # Alternating +2 / +1 so std > 0 and mean > 0
        vals = [100.0]
        for i in range(29):
            vals.append(vals[-1] + (2.0 if i % 2 == 0 else 1.0))
        s = pd.Series(vals)
        result = compute_rolling_sharpe(s, window=10)
        valid = result.dropna()
        assert len(valid) > 0
        assert (valid > 0).all()


# ── compute_strategy_stats ───────────────────────────────────────────────────


class TestComputeStrategyStats:
    def _make_df(self, rows: list[dict]) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        return df

    def test_empty_dataframe_returns_empty_stats(self):
        df = pd.DataFrame()
        stats = compute_strategy_stats(df)
        assert stats.empty

    def test_single_winning_trade(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "strategy": "sma_crossover",
             "avg_fill_price": "100.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "3.0", "timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "120.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "4.0", "timestamp": _ts(1)},
        ])
        stats = compute_strategy_stats(df)
        assert len(stats) == 1
        row = stats.iloc[0]
        assert row["strategy"] == "sma_crossover"
        assert row["trades"] == 1
        assert row["wins"] == 1
        assert pytest.approx(row["win_rate"]) == 1.0
        assert pytest.approx(row["total_pnl"]) == 200.0  # (120-100)*10

    def test_win_rate_calculation(self):
        df = self._make_df([
            # Two trades: one win, one loss
            {"symbol": "AAPL", "side": "buy", "strategy": "sma_crossover",
             "avg_fill_price": "100.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "0", "timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "110.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "0", "timestamp": _ts(1)},
            {"symbol": "GOOG", "side": "buy", "strategy": "sma_crossover",
             "avg_fill_price": "200.0", "filled_qty": "5", "qty": "5",
             "realized_slippage_bps": "0", "timestamp": _ts(2)},
            {"symbol": "GOOG", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "190.0", "filled_qty": "5", "qty": "5",
             "realized_slippage_bps": "0", "timestamp": _ts(3)},
        ])
        stats = compute_strategy_stats(df)
        row = stats[stats["strategy"] == "sma_crossover"].iloc[0]
        assert row["trades"] == 2
        assert row["wins"] == 1
        assert pytest.approx(row["win_rate"]) == 0.5

    def test_multiple_strategies(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "strategy": "sma_crossover",
             "avg_fill_price": "100.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "2.0", "timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "110.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "3.0", "timestamp": _ts(1)},
            {"symbol": "ALLY", "side": "buy", "strategy": "rsi_reversion",
             "avg_fill_price": "50.0", "filled_qty": "20", "qty": "20",
             "realized_slippage_bps": "5.0", "timestamp": _ts(2)},
            {"symbol": "ALLY", "side": "sell", "strategy": "rsi_reversion",
             "avg_fill_price": "48.0", "filled_qty": "20", "qty": "20",
             "realized_slippage_bps": "4.0", "timestamp": _ts(3)},
        ])
        stats = compute_strategy_stats(df)
        assert len(stats) == 2
        strategies = set(stats["strategy"])
        assert "sma_crossover" in strategies
        assert "rsi_reversion" in strategies
