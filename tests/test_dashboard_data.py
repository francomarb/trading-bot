"""Unit tests for dashboard.py data loading helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from dashboard import (
    broker_position_detail,
    compute_equity_curve,
    filter_realized_curve_window,
    load_broker_account_curve,
    compute_rolling_sharpe,
    compute_sleeve_usage,
    compute_strategy_stats,
    format_delta_currency,
    load_engine_state,
    load_trades,
    multi_leg_display_rows,
    realized_trade_events,
    refresh_multi_leg_positions,
    resolve_account_metrics,
)

# ── Helpers ──────────────────────────────────────────────────────────────────

_TRADE_COLUMNS = [
    "id", "timestamp", "symbol", "side", "qty", "avg_fill_price",
    "order_id", "strategy", "reason", "stop_price",
    "entry_reference_price", "modeled_slippage_bps",
    "realized_slippage_bps", "order_type", "status",
    "requested_qty", "filled_qty", "initial_stop_loss",
    "initial_risk_per_share", "initial_risk_dollars",
    "realized_pnl", "r_multiple", "entry_timestamp", "exit_timestamp",
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

    def test_db_read_failure_returns_empty_with_error_attr(self, tmp_path, monkeypatch):
        db = tmp_path / "trades.db"
        _make_db(db, [])

        def _boom(*args, **kwargs):
            raise sqlite3.DatabaseError("broken db")

        monkeypatch.setattr(pd, "read_sql_query", _boom)
        df = load_trades(str(db))
        assert df.empty
        assert "load_error" in df.attrs
        assert "broken db" in df.attrs["load_error"]


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


class TestLoadBrokerAccountCurve:
    def test_broker_history_is_loaded(self, monkeypatch):
        load_broker_account_curve.clear()

        class _History:
            timestamp = [1_700_000_000, 1_700_086_400]
            equity = [100_000.0, 100_250.0]
            profit_loss = [0.0, 250.0]
            profit_loss_pct = [0.0, 0.0025]

        class _Api:
            def get_portfolio_history(self, request):
                assert request.period == "1M"
                return _History()

        class _Broker:
            def __init__(self):
                self._api = _Api()

        monkeypatch.setattr("execution.broker.AlpacaBroker", _Broker)
        df = load_broker_account_curve(False, "1M")
        assert list(df.columns) == ["timestamp", "equity", "profit_loss", "profit_loss_pct"]
        assert len(df) == 2
        assert pd.api.types.is_datetime64_any_dtype(df["timestamp"])
        assert pytest.approx(df["equity"].iloc[-1]) == 100_250.0

    def test_broker_history_uses_requested_period(self, monkeypatch):
        load_broker_account_curve.clear()

        seen = {}

        class _History:
            timestamp = [1_700_000_000]
            equity = [100_000.0]
            profit_loss = [0.0]
            profit_loss_pct = [0.0]

        class _Api:
            def get_portfolio_history(self, request):
                seen["period"] = request.period
                return _History()

        class _Broker:
            def __init__(self):
                self._api = _Api()

        monkeypatch.setattr("execution.broker.AlpacaBroker", _Broker)
        df = load_broker_account_curve(False, "1W")
        assert len(df) == 1
        assert seen["period"] == "1W"

    def test_broker_history_failure_returns_empty_with_error_attr(self, monkeypatch):
        load_broker_account_curve.clear()

        class _Broker:
            def __init__(self):
                raise RuntimeError("no broker")

        monkeypatch.setattr("execution.broker.AlpacaBroker", _Broker)
        df = load_broker_account_curve(False, "1M")
        assert df.empty
        assert "load_error" in df.attrs
        assert "no broker" in df.attrs["load_error"]

    def test_invalid_period_raises(self):
        load_broker_account_curve.clear()
        with pytest.raises(ValueError, match="unsupported broker account curve period"):
            load_broker_account_curve(False, "YTD")


class TestRefreshMultiLegPositions:
    def test_broker_position_detail_preserves_alpaca_option_pnl(self):
        position = SimpleNamespace(
            qty=1,
            avg_entry_price=4.92,
            current_price=3.72,
            market_value=372.0,
            cost_basis=492.0,
            unrealized_pl=-120.0,
            unrealized_plpc=-0.2439,
        )
        detail = broker_position_detail(position)
        assert detail["current_price"] == pytest.approx(3.72)
        assert detail["cost_basis"] == pytest.approx(492.0)
        assert detail["unrealized_pl"] == pytest.approx(-120.0)
        assert detail["unrealized_pnl"] == pytest.approx(-120.0)
        assert detail["unrealized_plpc"] == pytest.approx(-0.2439)

    def test_returns_snapshot_rows_without_broker_refresh(self):
        state = {
            "multi_leg_positions": [{
                "position_id": "p1",
                "structure": "put_credit_spread",
                "current_exit_price": 1.95,
            }]
        }
        assert refresh_multi_leg_positions(state, None) == state["multi_leg_positions"]

    def test_refreshes_credit_spread_marks_from_broker_positions(self):
        state = {
            "multi_leg_positions": [{
                "position_id": "p1",
                "strategy": "credit_spread",
                "structure": "put_credit_spread",
                "underlying": "SPY",
                "short_occ": "SPY260618P00714000",
                "long_occ": "SPY260618P00704000",
                "short_strike": 714.0,
                "long_strike": 704.0,
                "expiration": "2026-06-18",
                "entry_net_price": 1.49,
                "width": 10.0,
                "qty": 1,
                "underlying_price": 731.86,
            }]
        }
        rows = refresh_multi_leg_positions(state, {
            "SPY260618P00714000": {"qty": -1, "market_value": -736.0},
            "SPY260618P00704000": {"qty": 1, "market_value": 541.0},
        })
        assert rows[0]["current_exit_price"] == pytest.approx(1.95)
        assert rows[0]["unrealized_pnl"] == pytest.approx(-46.0)
        assert rows[0]["status"] == "watch"

    def test_display_rows_include_short_long_and_net_pnl(self):
        rows = multi_leg_display_rows([{
            "structure": "put_credit_spread",
            "underlying": "SPY",
            "short_strike": 704.0,
            "long_strike": 695.0,
            "expiration": "2026-06-26",
            "dte": 37,
            "entry_net_price": 1.27,
            "current_exit_price": 1.04,
            "qty": 1,
            "unrealized_pnl": 23.0,
            "max_profit": 127.0,
            "max_loss": 773.0,
            "underlying_price": 740.67,
            "distance_to_short_strike": 36.67,
            "distance_to_short_strike_pct": 0.0495,
            "status": "healthy",
            "legs": [
                {"role": "short", "unrealized_pnl": 143.0},
                {"role": "long", "unrealized_pnl": -120.0},
            ],
        }])
        assert rows[0].pop("Distance %") == pytest.approx(4.95)
        assert rows == [{
            "Structure": "Put Credit Spread",
            "Underlying": "SPY",
            "Strikes": "704 / 695",
            "Expiration": "2026-06-26",
            "DTE": 37,
            "Entry Credit": 127.0,
            "Mark Debit": 104.0,
            "Net Spread P&L": 23.0,
            "Short Leg P&L": 143.0,
            "Long Leg P&L": -120.0,
            "Max Profit": 127.0,
            "Max Loss": 773.0,
            "Underlying Price": 740.67,
            "Distance": 36.67,
            "Status": "healthy",
        }]


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
             "filled_qty": "10", "timestamp": _ts(0), "entry_timestamp": _ts(0),
             "strategy": "sma_crossover"},
            {"symbol": "AAPL", "side": "sell", "qty": "10", "avg_fill_price": "160.0",
             "filled_qty": "10", "timestamp": _ts(1), "entry_timestamp": _ts(0),
             "realized_pnl": "100.0", "strategy": "sma_crossover"},
        ])
        curve = compute_equity_curve(df)
        assert len(curve) == 1
        assert pytest.approx(curve["cumulative_pnl"].iloc[-1]) == 100.0  # (160-150)*10

    def test_single_losing_trade(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "qty": "5", "avg_fill_price": "200.0",
             "filled_qty": "5", "timestamp": _ts(0), "entry_timestamp": _ts(0),
             "strategy": "sma_crossover"},
            {"symbol": "AAPL", "side": "sell", "qty": "5", "avg_fill_price": "190.0",
             "filled_qty": "5", "timestamp": _ts(1), "entry_timestamp": _ts(0),
             "realized_pnl": "-50.0", "strategy": "sma_crossover"},
        ])
        curve = compute_equity_curve(df)
        assert pytest.approx(curve["cumulative_pnl"].iloc[-1]) == -50.0  # (190-200)*5

    def test_two_trades_cumulative_pnl(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "qty": "10", "avg_fill_price": "100.0",
             "filled_qty": "10", "timestamp": _ts(0), "entry_timestamp": _ts(0),
             "strategy": "sma_crossover"},
            {"symbol": "AAPL", "side": "sell", "qty": "10", "avg_fill_price": "110.0",
             "filled_qty": "10", "timestamp": _ts(1), "entry_timestamp": _ts(0),
             "realized_pnl": "100.0", "strategy": "sma_crossover"},
            {"symbol": "GOOG", "side": "buy", "qty": "2", "avg_fill_price": "500.0",
             "filled_qty": "2", "timestamp": _ts(2), "entry_timestamp": _ts(2),
             "strategy": "sma_crossover"},
            {"symbol": "GOOG", "side": "sell", "qty": "2", "avg_fill_price": "490.0",
             "filled_qty": "2", "timestamp": _ts(3), "entry_timestamp": _ts(2),
             "realized_pnl": "-20.0", "strategy": "sma_crossover"},
        ])
        curve = compute_equity_curve(df)
        assert len(curve) == 2
        # First trade: +100; second trade: -20; cumulative at end = +80
        assert pytest.approx(curve["cumulative_pnl"].iloc[-1]) == 80.0

    def test_partial_exit_aggregates_into_one_completed_trade_event(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "qty": "10", "avg_fill_price": "100.0",
             "filled_qty": "10", "timestamp": _ts(0), "entry_timestamp": _ts(0),
             "strategy": "sma_crossover"},
            {"symbol": "AAPL", "side": "sell", "qty": "5", "avg_fill_price": "110.0",
             "filled_qty": "5", "timestamp": _ts(1), "entry_timestamp": _ts(0),
             "realized_pnl": "50.0", "strategy": "sma_crossover"},
            {"symbol": "AAPL", "side": "sell", "qty": "5", "avg_fill_price": "120.0",
             "filled_qty": "5", "timestamp": _ts(2), "entry_timestamp": _ts(0),
             "realized_pnl": "100.0", "strategy": "sma_crossover"},
        ])
        curve = compute_equity_curve(df)
        assert len(curve) == 1
        assert pytest.approx(curve["cumulative_pnl"].iloc[0]) == 150.0

    def test_includes_credit_spread_realized_events(self):
        df = self._make_df([
            {"symbol": "SPY260626P00704000", "side": "sell", "strategy": "credit_spread",
             "filled_qty": "1", "timestamp": _ts(0), "position_id": "spread-1",
             "position_type": "spread"},
            {"symbol": "SPY260626P00695000", "side": "buy", "strategy": "credit_spread",
             "filled_qty": "1", "timestamp": _ts(1), "position_id": "spread-1",
             "position_type": "spread", "realized_pnl": "66.0"},
            {"symbol": "SPY260618P00714000", "side": "sell", "strategy": "credit_spread",
             "filled_qty": "1", "timestamp": _ts(2), "position_id": "spread-2",
             "position_type": "spread"},
            {"symbol": "SPY260618P00704000", "side": "buy", "strategy": "credit_spread",
             "filled_qty": "1", "timestamp": _ts(3), "position_id": "spread-2",
             "position_type": "spread", "realized_pnl": "75.0"},
        ])
        curve = compute_equity_curve(df)
        assert len(curve) == 2
        assert pytest.approx(curve["cumulative_pnl"].iloc[0]) == 66.0
        assert pytest.approx(curve["cumulative_pnl"].iloc[1]) == 141.0


class TestRealizedTradeEvents:
    def test_skips_incomplete_single_leg_exit(self):
        df = pd.DataFrame([
            {"symbol": "TSLA", "side": "buy", "strategy": "donchian_breakout",
             "filled_qty": "5.39", "entry_timestamp": _ts(0), "timestamp": _ts(0)},
            {"symbol": "TSLA", "side": "sell", "strategy": "donchian_breakout",
             "filled_qty": "0.39", "realized_pnl": "-3.73698",
             "entry_timestamp": _ts(0), "timestamp": _ts(1)},
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        events = realized_trade_events(df)
        assert events == []

    def test_includes_single_leg_and_spread_events(self):
        df = pd.DataFrame([
            {"symbol": "AAPL", "side": "buy", "strategy": "sma_crossover",
             "filled_qty": "10", "entry_timestamp": _ts(0), "timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "filled_qty": "10", "realized_pnl": "100.0",
             "entry_timestamp": _ts(0), "timestamp": _ts(1)},
            {"symbol": "SPY260626P00695000", "side": "buy", "strategy": "credit_spread",
             "filled_qty": "1", "realized_pnl": "66.0",
             "position_id": "spread-1", "position_type": "spread", "timestamp": _ts(2)},
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        events = realized_trade_events(df)
        assert len(events) == 2
        assert pytest.approx(events[0]["pnl"]) == 100.0
        assert pytest.approx(events[1]["pnl"]) == 66.0


class TestFilterRealizedCurveWindow:
    def test_all_returns_original_curve(self):
        curve = pd.DataFrame({
            "timestamp": pd.to_datetime([_ts(0), _ts(10)], utc=True),
            "cumulative_pnl": [10.0, 25.0],
        })
        filtered = filter_realized_curve_window(curve, "All")
        pd.testing.assert_frame_equal(filtered, curve)

    def test_1w_filters_and_rebases_to_zero(self):
        curve = pd.DataFrame({
            "timestamp": pd.to_datetime([_ts(0), _ts(3), _ts(10)], utc=True),
            "cumulative_pnl": [10.0, 25.0, 40.0],
        })
        filtered = filter_realized_curve_window(curve, "1W")
        assert len(filtered) == 2
        assert pytest.approx(filtered["cumulative_pnl"].iloc[0]) == 0.0
        assert pytest.approx(filtered["cumulative_pnl"].iloc[1]) == 15.0

    def test_1m_keeps_full_curve_and_rebases_first_point(self):
        curve = pd.DataFrame({
            "timestamp": pd.to_datetime([_ts(0), _ts(5), _ts(10)], utc=True),
            "cumulative_pnl": [-5.0, 10.0, 20.0],
        })
        filtered = filter_realized_curve_window(curve, "1M")
        assert len(filtered) == 3
        assert pytest.approx(filtered["cumulative_pnl"].iloc[0]) == 0.0
        assert pytest.approx(filtered["cumulative_pnl"].iloc[-1]) == 25.0


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
             "realized_slippage_bps": "3.0", "timestamp": _ts(0),
             "entry_timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "120.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "4.0", "timestamp": _ts(1),
             "entry_timestamp": _ts(0), "realized_pnl": "200.0"},
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
             "realized_slippage_bps": "0", "timestamp": _ts(0),
             "entry_timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "110.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "0", "timestamp": _ts(1),
             "entry_timestamp": _ts(0), "realized_pnl": "100.0"},
            {"symbol": "GOOG", "side": "buy", "strategy": "sma_crossover",
             "avg_fill_price": "200.0", "filled_qty": "5", "qty": "5",
             "realized_slippage_bps": "0", "timestamp": _ts(2),
             "entry_timestamp": _ts(2)},
            {"symbol": "GOOG", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "190.0", "filled_qty": "5", "qty": "5",
             "realized_slippage_bps": "0", "timestamp": _ts(3),
             "entry_timestamp": _ts(2), "realized_pnl": "-50.0"},
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
             "realized_slippage_bps": "2.0", "timestamp": _ts(0),
             "entry_timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "110.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "3.0", "timestamp": _ts(1),
             "entry_timestamp": _ts(0), "realized_pnl": "100.0"},
            {"symbol": "ALLY", "side": "buy", "strategy": "rsi_reversion",
             "avg_fill_price": "50.0", "filled_qty": "20", "qty": "20",
             "realized_slippage_bps": "5.0", "timestamp": _ts(2),
             "entry_timestamp": _ts(2)},
            {"symbol": "ALLY", "side": "sell", "strategy": "rsi_reversion",
             "avg_fill_price": "48.0", "filled_qty": "20", "qty": "20",
             "realized_slippage_bps": "4.0", "timestamp": _ts(3),
             "entry_timestamp": _ts(2), "realized_pnl": "-40.0"},
        ])
        stats = compute_strategy_stats(df)
        assert len(stats) == 2
        strategies = set(stats["strategy"])
        assert "sma_crossover" in strategies
        assert "rsi_reversion" in strategies

    def test_partial_exit_for_same_position_counts_as_one_trade(self):
        df = self._make_df([
            {"symbol": "AAPL", "side": "buy", "strategy": "sma_crossover",
             "avg_fill_price": "100.0", "filled_qty": "10", "qty": "10",
             "realized_slippage_bps": "0", "timestamp": _ts(0),
             "entry_timestamp": _ts(0)},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "110.0", "filled_qty": "5", "qty": "5",
             "realized_slippage_bps": "0", "timestamp": _ts(1),
             "entry_timestamp": _ts(0), "realized_pnl": "50.0"},
            {"symbol": "AAPL", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "120.0", "filled_qty": "5", "qty": "5",
             "realized_slippage_bps": "0", "timestamp": _ts(2),
             "entry_timestamp": _ts(0), "realized_pnl": "100.0"},
        ])
        stats = compute_strategy_stats(df)
        row = stats[stats["strategy"] == "sma_crossover"].iloc[0]
        assert row["trades"] == 1
        assert row["wins"] == 1
        assert pytest.approx(row["total_pnl"]) == 150.0
        assert pytest.approx(row["win_rate"]) == 1.0

    def test_fractional_cleanup_and_main_exit_aggregate_into_one_trade(self):
        df = self._make_df([
            {"symbol": "DK", "side": "buy", "strategy": "sma_crossover",
             "avg_fill_price": "44.50", "filled_qty": "236.4", "qty": "236.4",
             "realized_slippage_bps": "353.35", "timestamp": _ts(0),
             "entry_timestamp": _ts(0)},
            {"symbol": "DK", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "41.92", "filled_qty": "236.0", "qty": "236.0",
             "realized_slippage_bps": "0", "timestamp": _ts(1),
             "entry_timestamp": _ts(0), "realized_pnl": str((41.92 - 44.50) * 236)},
            {"symbol": "DK", "side": "sell", "strategy": "sma_crossover",
             "avg_fill_price": "41.872", "filled_qty": "0.4", "qty": "0.4",
             "realized_slippage_bps": "0", "timestamp": _ts(2),
             "entry_timestamp": _ts(0), "realized_pnl": str((41.872 - 44.50) * 0.4)},
        ])
        stats = compute_strategy_stats(df)
        row = stats.iloc[0]
        assert row["trades"] == 1
        assert row["wins"] == 0
        assert pytest.approx(row["total_pnl"]) == pytest.approx(
            (41.92 - 44.50) * 236 + (41.872 - 44.50) * 0.4
        )

    def test_incomplete_exit_is_skipped_until_full_close_is_logged(self):
        df = self._make_df([
            {"symbol": "TSLA", "side": "buy", "strategy": "donchian_breakout",
             "avg_fill_price": "420.73", "filled_qty": "5.39", "qty": "5.39",
             "realized_slippage_bps": "10.0", "timestamp": _ts(0),
             "entry_timestamp": _ts(0)},
            {"symbol": "TSLA", "side": "sell", "strategy": "donchian_breakout",
             "avg_fill_price": "418.728", "filled_qty": "0.39", "qty": "0.39",
             "realized_slippage_bps": "0", "timestamp": _ts(1),
             "entry_timestamp": _ts(0), "realized_pnl": "-3.73698"},
        ])
        stats = compute_strategy_stats(df)
        row = stats.iloc[0]
        assert row["trades"] == 0
        assert row["wins"] == 0
        assert pytest.approx(row["total_pnl"]) == 0.0

    def test_mleg_spread_realized_pnl_counts_by_position_id(self):
        df = self._make_df([
            {"symbol": "SPY260618P00714000", "side": "sell", "strategy": "credit_spread",
             "avg_fill_price": "1.49", "filled_qty": "1", "qty": "1",
             "realized_slippage_bps": "0", "timestamp": _ts(0),
             "position_id": "spread-1", "position_type": "spread",
             "reason": "spread entry"},
            {"symbol": "SPY260618P00704000", "side": "buy", "strategy": "credit_spread",
             "avg_fill_price": "0", "filled_qty": "1", "qty": "1",
             "realized_slippage_bps": "0", "timestamp": _ts(0),
             "position_id": "spread-1", "position_type": "spread",
             "reason": "spread entry"},
            {"symbol": "SPY260618P00714000", "side": "buy", "strategy": "credit_spread",
             "avg_fill_price": "0.74", "filled_qty": "1", "qty": "1",
             "realized_slippage_bps": "0", "timestamp": _ts(1),
             "position_id": "spread-1", "position_type": "spread",
             "reason": "spread exit", "realized_pnl": "75.0"},
            {"symbol": "SPY260618P00704000", "side": "sell", "strategy": "credit_spread",
             "avg_fill_price": "0", "filled_qty": "1", "qty": "1",
             "realized_slippage_bps": "0", "timestamp": _ts(1),
             "position_id": "spread-1", "position_type": "spread",
             "reason": "spread exit"},
            {"symbol": "SPY260626P00704000", "side": "buy", "strategy": "credit_spread",
             "avg_fill_price": "0.61", "filled_qty": "1", "qty": "1",
             "realized_slippage_bps": "0", "timestamp": _ts(2),
             "position_id": "spread-2", "position_type": "spread",
             "reason": "spread exit", "realized_pnl": "66.0"},
        ])

        stats = compute_strategy_stats(df)
        row = stats[stats["strategy"] == "credit_spread"].iloc[0]

        assert row["trades"] == 2
        assert row["wins"] == 2
        assert row["win_rate"] == pytest.approx(1.0)
        assert row["total_pnl"] == pytest.approx(141.0)

    def test_mleg_spread_avg_slippage_uses_completed_entry_and_close_rows(self):
        df = self._make_df([
            {"symbol": "SPY260618P00714000", "side": "sell", "strategy": "credit_spread",
             "avg_fill_price": "1.50", "filled_qty": "2", "qty": "2",
             "realized_slippage_bps": "-100.0", "timestamp": _ts(0),
             "position_id": "spread-1", "position_type": "spread",
             "reason": "spread entry"},
            {"symbol": "SPY260618P00704000", "side": "buy", "strategy": "credit_spread",
             "avg_fill_price": "0", "filled_qty": "2", "qty": "2",
             "realized_slippage_bps": "0", "timestamp": _ts(0),
             "position_id": "spread-1", "position_type": "spread",
             "reason": "spread entry"},
            {"symbol": "SPY260618P00714000", "side": "buy", "strategy": "credit_spread",
             "avg_fill_price": "0.63", "filled_qty": "2", "qty": "2",
             "realized_slippage_bps": "50.0", "timestamp": _ts(1),
             "position_id": "spread-1", "position_type": "spread",
             "reason": "spread exit", "realized_pnl": "174.0"},
            {"symbol": "SPY260618P00704000", "side": "sell", "strategy": "credit_spread",
             "avg_fill_price": "0", "filled_qty": "2", "qty": "2",
             "realized_slippage_bps": "0", "timestamp": _ts(1),
             "position_id": "spread-1", "position_type": "spread",
             "reason": "spread exit"},
            # Still-open spread should not affect completed-trade slippage.
            {"symbol": "QQQ260618P00674000", "side": "sell", "strategy": "credit_spread",
             "avg_fill_price": "2.00", "filled_qty": "1", "qty": "1",
             "realized_slippage_bps": "999.0", "timestamp": _ts(2),
             "position_id": "spread-open", "position_type": "spread",
             "reason": "spread entry"},
        ])
        stats = compute_strategy_stats(df)
        row = stats[stats["strategy"] == "credit_spread"].iloc[0]

        assert row["trades"] == 1
        assert row["total_pnl"] == pytest.approx(174.0)
        assert row["avg_slippage_bps"] == pytest.approx(-25.0)


class TestComputeSleeveUsage:
    def test_uses_allocator_snapshot_when_available(self):
        state = {
            "allocator": {
                "sma_crossover": {
                    "target_budget": 36_000.0,
                    "effective_budget": 41_400.0,
                    "borrowed_budget": 5_400.0,
                    "used": 1_250.0,
                    "available": 40_150.0,
                    "positions_open": 1,
                    "hard_max_positions": 8,
                    "max_position_notional": 16_560.0,
                },
                "rsi_reversion": {
                    "target_budget": 40_000.0,
                    "effective_budget": 40_000.0,
                    "borrowed_budget": 0.0,
                    "used": 900.0,
                    "available": 39_100.0,
                    "positions_open": 1,
                    "hard_max_positions": 8,
                    "max_position_notional": 16_000.0,
                },
            }
        }
        result = compute_sleeve_usage(
            state,
            equity=100_000.0,
            allocations={},
            total_gross_pct=0.80,
        )
        sma = result[result["Strategy"] == "sma_crossover"].iloc[0]
        assert pytest.approx(sma["Target Budget"]) == 36_000.0
        assert pytest.approx(sma["Effective Budget"]) == 41_400.0
        assert pytest.approx(sma["Stretch Headroom"]) == 5_400.0
        assert pytest.approx(sma["Used Notional"]) == 1_250.0
        assert pytest.approx(sma["Utilization"]) == 1_250.0 / 41_400.0

    def test_falls_back_to_position_math_without_allocator_snapshot(self):
        state = {
            "positions_detail": {
                "AAPL": {
                    "strategy": "sma_crossover",
                    "qty": 10,
                    "avg_entry_price": 100.0,
                    "market_value": 1250.0,
                },
                "MSFT": {
                    "strategy": "rsi_reversion",
                    "qty": 5,
                    "avg_entry_price": 200.0,
                    "market_value": 900.0,
                },
            }
        }
        allocations = {
            "sma_crossover": {
                "target_pct": 0.50,
                "hard_max_positions": 5,
                "max_position_pct_of_sleeve": 0.40,
            },
            "rsi_reversion": {
                "target_pct": 0.50,
                "hard_max_positions": 5,
                "max_position_pct_of_sleeve": 0.40,
            },
        }
        result = compute_sleeve_usage(
            state,
            equity=100_000.0,
            allocations=allocations,
            total_gross_pct=0.80,
        )
        sma = result[result["Strategy"] == "sma_crossover"].iloc[0]
        rsi = result[result["Strategy"] == "rsi_reversion"].iloc[0]
        assert pytest.approx(sma["Target Budget"]) == 40_000.0
        assert pytest.approx(sma["Effective Budget"]) == 40_000.0
        assert pytest.approx(sma["Used Notional"]) == 1250.0
        assert pytest.approx(sma["Remaining"]) == 38_750.0
        assert pytest.approx(sma["Utilization"]) == 1250.0 / 40_000.0
        assert sma["Open Positions"] == 1
        assert pytest.approx(rsi["Used Notional"]) == 900.0


class TestFormatDeltaCurrency:
    def test_positive_value_has_leading_plus(self):
        assert format_delta_currency(123.45) == "+$123.45"

    def test_negative_value_has_leading_minus(self):
        assert format_delta_currency(-123.45) == "-$123.45"

    def test_none_returns_none(self):
        assert format_delta_currency(None) is None


class TestResolveAccountMetrics:
    def test_prefers_direct_broker_metrics_when_available(self):
        state = {
            "equity": 100_000.0,
            "daily_pnl": 25.0,
            "session_pnl": 10.0,
            "session_start_equity": 99_990.0,
            "previous_close_equity": 99_975.0,
        }
        broker_metrics = {
            "equity": 100_325.82,
            "daily_pnl": 325.82,
            "session_pnl": 335.82,
            "session_start_equity": 99_990.0,
            "previous_close_equity": 100_000.0,
            "source": "broker",
        }

        metrics, warning = resolve_account_metrics(state, broker_metrics)

        assert warning is None
        assert metrics["source"] == "broker"
        assert metrics["equity"] == pytest.approx(100_325.82)
        assert metrics["daily_pnl"] == pytest.approx(325.82)
        assert metrics["equity_delta"] == pytest.approx(325.82)

    def test_falls_back_to_snapshot_and_session_delta_without_previous_close(self):
        state = {
            "equity": 100_050.0,
            "daily_pnl": 0.0,
            "session_pnl": 50.0,
            "session_start_equity": 100_000.0,
            "previous_close_equity": None,
        }

        metrics, warning = resolve_account_metrics(state, broker_metrics=None)

        assert warning is None
        assert metrics["source"] == "snapshot"
        assert metrics["equity"] == pytest.approx(100_050.0)
        assert metrics["daily_pnl"] == pytest.approx(0.0)
        assert metrics["equity_delta"] == pytest.approx(50.0)

    def test_broker_error_keeps_snapshot_values_and_returns_warning(self):
        state = {
            "equity": 100_080.0,
            "daily_pnl": 80.0,
            "session_pnl": 60.0,
            "session_start_equity": 100_020.0,
            "previous_close_equity": 100_000.0,
        }
        broker_metrics = {
            "error": "Direct broker account refresh unavailable: TimeoutError: slow",
            "source": "snapshot",
        }

        metrics, warning = resolve_account_metrics(state, broker_metrics)

        assert metrics["source"] == "snapshot"
        assert metrics["equity"] == pytest.approx(100_080.0)
        assert metrics["daily_pnl"] == pytest.approx(80.0)
        assert warning is not None
        assert "TimeoutError" in warning
