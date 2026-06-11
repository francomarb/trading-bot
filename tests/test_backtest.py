"""
Unit tests for backtest/runner.py.

The synthetic price paths are constructed so that the trade outcomes are
known by hand — tests function as a spec, not just regression.

Coverage:
  - BacktestConfig validation
  - run_backtest: required columns, look-ahead-safe shift (signal at t → fill
    at t+1's open), slippage and commission flow into trade economics
  - compute_stats: profit factor, expectancy, win rate, trade count on a
    hand-crafted set of trades
  - save_equity_chart: writes a non-empty PNG to a tmp dir
  - walk_forward: produces n_splits rows, each fold disjoint
  - parameter_sensitivity: cartesian product, skips invalid combos
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from backtest.runner import (
    BacktestConfig,
    BacktestResult,
    parameter_sensitivity,
    run_backtest,
    save_equity_chart,
    walk_forward,
)
from strategies.base import BaseStrategy, SignalFrame
from strategies.sma_crossover import SMACrossover


# ── Helpers ──────────────────────────────────────────────────────────────────


def _ohlcv(closes: list[float], opens: list[float] | None = None) -> pd.DataFrame:
    """Build a minimal tz-aware OHLCV frame from a closes list."""
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz="UTC")
    if opens is None:
        opens = closes
    return pd.DataFrame(
        {
            "open": opens,
            "high": [max(o, c) + 0.01 for o, c in zip(opens, closes)],
            "low": [min(o, c) - 0.01 for o, c in zip(opens, closes)],
            "close": closes,
            "volume": [1_000_000] * len(closes),
        },
        index=idx,
    )


class _ScriptedStrategy(BaseStrategy):
    """Emit entries/exits at predetermined indices."""

    name = "scripted"

    def __init__(self, entry_idx: list[int], exit_idx: list[int]):
        super().__init__()
        self._ei = set(entry_idx)
        self._xi = set(exit_idx)

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        n = len(df)
        e = pd.Series([i in self._ei for i in range(n)], index=df.index)
        x = pd.Series([i in self._xi for i in range(n)], index=df.index)
        return SignalFrame(entries=e, exits=x)


def _trending_df(n: int = 300, seed: int = 7) -> pd.DataFrame:
    """A noisy upward-drifting series long enough for SMA(20,50)."""
    rng = np.random.default_rng(seed)
    rets = rng.normal(0.0008, 0.015, n)
    close = 100 * (1 + pd.Series(rets)).cumprod()
    idx = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close.index = idx
    return pd.DataFrame(
        {
            "open": close.shift(1).fillna(close.iloc[0]).values * 1.001,
            "high": (close * 1.01).values,
            "low": (close * 0.99).values,
            "close": close.values,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


# ── BacktestConfig ───────────────────────────────────────────────────────────


class TestBacktestConfig:
    def test_defaults_are_conservative(self):
        c = BacktestConfig()
        assert c.initial_cash == 100_000.0
        assert c.slippage_bps == 5.0
        assert c.commission_per_trade == 0.0

    def test_negative_cash_rejected(self):
        with pytest.raises(ValueError, match="initial_cash"):
            BacktestConfig(initial_cash=0)

    def test_negative_slippage_rejected(self):
        with pytest.raises(ValueError, match="slippage"):
            BacktestConfig(slippage_bps=-1.0)

    def test_negative_commission_rejected(self):
        with pytest.raises(ValueError, match="commission"):
            BacktestConfig(commission_per_trade=-0.01)


# ── run_backtest core contract ──────────────────────────────────────────────


class TestRunBacktest:
    def test_missing_open_column_raises(self):
        df = pd.DataFrame(
            {"close": [1, 2, 3]},
            index=pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC"),
        )
        with pytest.raises(ValueError, match="open"):
            run_backtest(SMACrossover(2, 3), df)

    def test_empty_df_raises(self):
        df = pd.DataFrame(
            {"open": [], "close": []},
            index=pd.DatetimeIndex([], tz="UTC"),
        )
        with pytest.raises(ValueError, match="empty"):
            run_backtest(SMACrossover(2, 3), df)

    def test_returns_backtest_result(self):
        df = _trending_df(200)
        r = run_backtest(SMACrossover(20, 50), df, symbol="X")
        assert isinstance(r, BacktestResult)
        assert r.symbol == "X"
        assert r.strategy_name == "sma_crossover"
        assert "total_return" in r.stats
        assert "sharpe" in r.stats

    def test_no_trades_when_strategy_silent(self):
        df = _trending_df(200)
        # Strategy that emits nothing.
        r = run_backtest(_ScriptedStrategy([], []), df)
        assert r.stats["trade_count"] == 0
        # No trades ⇒ flat equity at initial_cash.
        assert r.stats["final_equity"] == pytest.approx(r.config.initial_cash)
        assert r.stats["total_return"] == pytest.approx(0.0)


class TestExecutionTiming:
    """The critical look-ahead rule: signal at t fills at t+1's open."""

    def test_entry_fills_at_next_bar_open(self):
        # Closes flat then jumps; opens defined separately so we can pin
        # the fill price exactly.
        closes = [10, 10, 10, 10, 10]
        opens =  [10, 10, 10, 12, 10]   # bar 3's open is 12
        df = _ohlcv(closes, opens)

        # Signal entry at bar 2's close → must fill at bar 3's open = 12.
        # Exit at bar 3's close → fills at bar 4's open = 10.
        strat = _ScriptedStrategy(entry_idx=[2], exit_idx=[3])
        cfg = BacktestConfig(
            initial_cash=10_000, slippage_bps=0, commission_per_trade=0
        )
        r = run_backtest(strat, df, cfg)
        trades = r.portfolio.trades.records_readable
        assert len(trades) == 1
        assert trades.iloc[0]["Avg Entry Price"] == pytest.approx(12.0)
        assert trades.iloc[0]["Avg Exit Price"] == pytest.approx(10.0)
        # Loss of ~$2 per share, no fees.
        assert trades.iloc[0]["PnL"] < 0

    def test_signal_on_last_bar_does_not_execute(self):
        # If a strategy emits at the very last bar, there is no t+1 to fill on.
        closes = [10, 11, 12, 13, 14]
        df = _ohlcv(closes)
        strat = _ScriptedStrategy(entry_idx=[4], exit_idx=[])
        r = run_backtest(strat, df, BacktestConfig(initial_cash=10_000))
        assert r.stats["trade_count"] == 0


class TestCosts:
    """Slippage and commission must move trade economics in the right direction."""

    def test_slippage_reduces_returns(self):
        df = _trending_df(250)
        strat = SMACrossover(10, 30)
        r0 = run_backtest(strat, df, BacktestConfig(slippage_bps=0))
        r1 = run_backtest(strat, df, BacktestConfig(slippage_bps=50))
        if r0.stats["trade_count"] > 0:
            assert r1.stats["total_return"] < r0.stats["total_return"]

    def test_commission_reduces_returns(self):
        df = _trending_df(250)
        strat = SMACrossover(10, 30)
        r0 = run_backtest(strat, df, BacktestConfig(commission_per_trade=0))
        r1 = run_backtest(strat, df, BacktestConfig(commission_per_trade=50))
        if r0.stats["trade_count"] > 0:
            assert r1.stats["total_return"] < r0.stats["total_return"]


# ── compute_stats: hand-crafted trades ──────────────────────────────────────


class TestComputeStats:
    def _two_trade_df(self):
        # 8 bars, two scripted trades:
        #   trade 1: enter @ open[1]=100, exit @ open[3]=110 → win
        #   trade 2: enter @ open[4]=110, exit @ open[6]=100 → loss
        opens = [100, 100, 105, 110, 110, 108, 100, 100]
        closes = opens
        df = _ohlcv(closes, opens)
        strat = _ScriptedStrategy(entry_idx=[0, 3], exit_idx=[2, 5])
        cfg = BacktestConfig(initial_cash=10_000, slippage_bps=0, commission_per_trade=0)
        return run_backtest(strat, df, cfg)

    def test_profit_factor_and_expectancy(self):
        r = self._two_trade_df()
        trades = r.portfolio.trades.records_readable
        assert len(trades) == 2
        pnls = trades["PnL"].astype(float).tolist()
        wins = sum(p for p in pnls if p > 0)
        losses = sum(p for p in pnls if p < 0)
        assert r.stats["trade_count"] == 2
        assert r.stats["win_rate"] == pytest.approx(0.5)
        assert r.stats["expectancy"] == pytest.approx(np.mean(pnls))
        assert r.stats["profit_factor"] == pytest.approx(wins / abs(losses))

    def test_profit_factor_inf_when_no_losses(self):
        # All wins.
        opens = [100, 100, 110, 100, 100, 110]
        df = _ohlcv(opens, opens)
        strat = _ScriptedStrategy(entry_idx=[0, 3], exit_idx=[1, 4])
        # entry@open[1]=100, exit@open[2]=110 → win; same again → win
        r = run_backtest(strat, df, BacktestConfig(initial_cash=10_000, slippage_bps=0))
        assert r.stats["trade_count"] == 2
        assert r.stats["profit_factor"] == float("inf")

    def test_zero_trades_zero_stats(self):
        df = _trending_df(100)
        r = run_backtest(_ScriptedStrategy([], []), df)
        assert r.stats["trade_count"] == 0
        assert r.stats["profit_factor"] == 0.0
        assert r.stats["expectancy"] == 0.0
        assert r.stats["win_rate"] == 0.0


# ── Equity chart ────────────────────────────────────────────────────────────


class TestSaveEquityChart:
    def test_writes_png(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        df = _trending_df(150)
        r = run_backtest(SMACrossover(10, 30), df, symbol="SYN")

        def fake_render(_result: BacktestResult, path: Path) -> None:
            path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 2048)

        monkeypatch.setattr("backtest.runner._render_equity_chart", fake_render)
        p = save_equity_chart(r, out_dir=tmp_path)
        assert p.exists()
        assert p.suffix == ".png"
        assert p.stat().st_size > 1000  # sanity: not an empty file


# ── Walk-forward ────────────────────────────────────────────────────────────


class TestWalkForward:
    def test_n_splits_must_be_at_least_2(self):
        df = _trending_df(200)
        with pytest.raises(ValueError, match="n_splits"):
            walk_forward(lambda: SMACrossover(10, 30), df, n_splits=1)

    def test_too_few_bars_raises(self):
        df = _trending_df(60)
        with pytest.raises(ValueError, match="bars"):
            walk_forward(lambda: SMACrossover(10, 30), df, n_splits=4)

    def test_returns_one_row_per_fold(self):
        df = _trending_df(400)
        wf = walk_forward(lambda: SMACrossover(10, 30), df, n_splits=4)
        assert len(wf) == 4
        # Folds are contiguous and disjoint.
        for i in range(len(wf) - 1):
            assert wf.iloc[i]["end"] < wf.iloc[i + 1]["start"]
        for col in ("total_return", "sharpe", "max_drawdown", "trade_count"):
            assert col in wf.columns


# ── Parameter sensitivity ───────────────────────────────────────────────────


class TestParameterSensitivity:
    def test_full_grid(self):
        df = _trending_df(300)
        ps = parameter_sensitivity(
            SMACrossover,
            {"fast": [5, 10], "slow": [30, 50]},
            df,
        )
        # 2 × 2 = 4 valid combos (all have fast < slow).
        assert len(ps) == 4
        assert set(ps.columns) >= {"fast", "slow", "total_return", "sharpe"}

    def test_invalid_combos_skipped(self):
        df = _trending_df(300)
        # fast=50 vs slow=20 is invalid (fast must be < slow); skipped.
        ps = parameter_sensitivity(
            SMACrossover,
            {"fast": [10, 50], "slow": [20, 60]},
            df,
        )
        # Valid combos: (10,20), (10,60), (50,60). (50,20) is skipped.
        assert len(ps) == 3
        assert all(row["fast"] < row["slow"] for _, row in ps.iterrows())

    def test_skip_invalid_false_propagates_error(self):
        df = _trending_df(300)
        with pytest.raises(ValueError, match="strictly less"):
            parameter_sensitivity(
                SMACrossover,
                {"fast": [50], "slow": [20]},
                df,
                skip_invalid=False,
            )

    def test_empty_grid_raises(self):
        df = _trending_df(300)
        with pytest.raises(ValueError, match="no valid"):
            parameter_sensitivity(
                SMACrossover,
                {"fast": [50], "slow": [20]},   # only invalid combo
                df,
            )


# ── PLAN 11.47: stop-limit entry semantics in backtest ──────────────────────


class TestStopLimitBacktestSemantics:
    """The backtest must simulate broker stop-limit fill behavior or any
    Donchian audit comparing paper vs backtest will diverge invisibly."""

    def _df_with_explicit_oh(self, rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
        # rows = [(open, high, low, close), ...]
        idx = pd.date_range("2026-01-01", periods=len(rows), freq="D", tz="UTC")
        return pd.DataFrame(
            {
                "open":  [r[0] for r in rows],
                "high":  [r[1] for r in rows],
                "low":   [r[2] for r in rows],
                "close": [r[3] for r in rows],
                "volume": [1_000_000] * len(rows),
            },
            index=idx,
        )

    class _FixedTriggerStrategy(BaseStrategy):
        """Always signal entry at the given index; trigger is a constant."""

        name = "fixed_trigger"

        def __init__(self, *, entry_idx: int, trigger: float):
            super().__init__()
            self._entry_idx = entry_idx
            self._trigger = trigger

        def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
            n = len(df)
            e = pd.Series([i == self._entry_idx for i in range(n)], index=df.index)
            x = pd.Series(False, index=df.index)
            return SignalFrame(entries=e, exits=x)

        def trigger_prices(self, df: pd.DataFrame) -> pd.Series:
            return pd.Series([self._trigger] * len(df), index=df.index)

    def test_gap_up_over_limit_no_fill(self):
        """Entry signal fires at bar 5 (trigger=245). Bar 6 opens at $277
        — well above the 500 bps cap of ~$257. Limit refuses → no fill."""
        from backtest.runner import run_backtest, BacktestConfig

        rows = []
        # Bars 0-5: flat at $240, then entry signal at bar 5.
        for _ in range(5):
            rows.append((240.0, 241.0, 239.0, 240.0))
        # Bar 5 (signal): close at $245 (= trigger).
        rows.append((242.0, 246.0, 241.0, 245.0))
        # Bar 6 (execution): gap up to $277.
        rows.append((277.0, 280.0, 275.0, 278.0))
        # Bars 7-10: hold.
        for _ in range(4):
            rows.append((278.0, 279.0, 277.0, 278.0))
        df = self._df_with_explicit_oh(rows)

        result = run_backtest(
            self._FixedTriggerStrategy(entry_idx=5, trigger=245.0),
            df,
            BacktestConfig(initial_cash=10_000.0),
            stop_limit_entries=True,
            stop_limit_chase_bps=500,
            stop_limit_chase_atr_fraction=None,
        )
        # No trade should have executed.
        assert result.portfolio.trades.records.shape[0] == 0

    def test_gap_down_below_trigger_no_fill(self):
        """Signal at bar 5 (trigger=$245). Bar 6 opens at $228, never
        rallies above $240. Stop never triggers → no fill."""
        from backtest.runner import run_backtest, BacktestConfig

        rows = []
        for _ in range(5):
            rows.append((240.0, 241.0, 239.0, 240.0))
        rows.append((242.0, 246.0, 241.0, 245.0))
        # Bar 6: gap down, never reaches trigger.
        rows.append((228.0, 240.0, 227.0, 235.0))
        for _ in range(4):
            rows.append((235.0, 236.0, 234.0, 235.0))
        df = self._df_with_explicit_oh(rows)

        result = run_backtest(
            self._FixedTriggerStrategy(entry_idx=5, trigger=245.0),
            df,
            BacktestConfig(initial_cash=10_000.0),
            stop_limit_entries=True,
            stop_limit_chase_bps=500,
            stop_limit_chase_atr_fraction=None,
        )
        assert result.portfolio.trades.records.shape[0] == 0

    def test_clean_intraday_breakout_fills_at_trigger(self):
        """Signal at bar 5 (trigger=$245). Bar 6 opens at $244 (below
        trigger), rallies through $245 intraday to $250. Stop fires at
        $245, limit allows → fill at $245."""
        from backtest.runner import run_backtest, BacktestConfig

        rows = []
        for _ in range(5):
            rows.append((240.0, 241.0, 239.0, 240.0))
        rows.append((242.0, 246.0, 241.0, 245.0))
        # Bar 6: open below trigger, high crosses trigger.
        rows.append((244.0, 250.0, 243.0, 249.0))
        for _ in range(4):
            rows.append((249.0, 250.0, 248.0, 249.0))
        df = self._df_with_explicit_oh(rows)

        result = run_backtest(
            self._FixedTriggerStrategy(entry_idx=5, trigger=245.0),
            df,
            BacktestConfig(initial_cash=10_000.0),
            stop_limit_entries=True,
            stop_limit_chase_bps=500,
            stop_limit_chase_atr_fraction=None,
        )
        assert result.portfolio.trades.records.shape[0] == 1
        # Fill at max(trigger, open) = max(245, 244) = 245.
        # (slippage in vectorbt is applied multiplicatively from this base.)
        entry_price = result.portfolio.trades.records["entry_price"].iloc[0]
        assert entry_price == pytest.approx(245.0, rel=0.01)

    def test_modest_gap_up_within_cap_fills_at_open(self):
        """Signal at bar 5 (trigger=$245). Bar 6 opens at $250 (within
        500 bps cap of $257.25). Stop fires immediately at open, limit
        allows → fill at $250."""
        from backtest.runner import run_backtest, BacktestConfig

        rows = []
        for _ in range(5):
            rows.append((240.0, 241.0, 239.0, 240.0))
        rows.append((242.0, 246.0, 241.0, 245.0))
        rows.append((250.0, 253.0, 249.0, 252.0))
        for _ in range(4):
            rows.append((252.0, 253.0, 251.0, 252.0))
        df = self._df_with_explicit_oh(rows)

        result = run_backtest(
            self._FixedTriggerStrategy(entry_idx=5, trigger=245.0),
            df,
            BacktestConfig(initial_cash=10_000.0),
            stop_limit_entries=True,
            stop_limit_chase_bps=500,
            stop_limit_chase_atr_fraction=None,
        )
        assert result.portfolio.trades.records.shape[0] == 1
        entry_price = result.portfolio.trades.records["entry_price"].iloc[0]
        # Fill at max(trigger, open) = max(245, 250) = 250.
        assert entry_price == pytest.approx(250.0, rel=0.01)

    def test_strategy_without_trigger_prices_raises_when_flag_set(self):
        """If a non-STOP_LIMIT strategy is run with stop_limit_entries=True,
        the runner must fail loudly rather than silently fall back to
        next-open semantics (which would silently diverge from production)."""
        from backtest.runner import run_backtest, BacktestConfig

        rows = [(100.0, 101.0, 99.0, 100.0)] * 10
        df = self._df_with_explicit_oh(rows)
        with pytest.raises(ValueError, match="trigger_prices"):
            run_backtest(
                _ScriptedStrategy(entry_idx=[5], exit_idx=[]),
                df,
                BacktestConfig(initial_cash=10_000.0),
                stop_limit_entries=True,
            )

    # ── PR #58 review P2 #4: gap-and-retrace fills ───────────────────────

    def test_gap_up_over_limit_with_retrace_fills_at_limit(self):
        """Signal at bar 5 (trigger=$245). Bar 6 opens at $260 (above the
        500 bps cap of $257.25) BUT retraces to a low of $250 intraday.
        Production behavior: the triggered limit rests in the book and
        fills when a matching offer at-or-below the limit becomes
        available. Backtest must model this — earlier `open <= limit`
        gate incorrectly rejected this case."""
        from backtest.runner import run_backtest, BacktestConfig

        rows = []
        for _ in range(5):
            rows.append((240.0, 241.0, 239.0, 240.0))
        rows.append((242.0, 246.0, 241.0, 245.0))
        # Bar 6: gap up (open=$260, above limit), but retrace down to $250
        # (below limit). High=$262, Low=$250.
        rows.append((260.0, 262.0, 250.0, 254.0))
        for _ in range(4):
            rows.append((254.0, 255.0, 253.0, 254.0))
        df = self._df_with_explicit_oh(rows)

        result = run_backtest(
            self._FixedTriggerStrategy(entry_idx=5, trigger=245.0),
            df,
            BacktestConfig(initial_cash=10_000.0),
            stop_limit_entries=True,
            stop_limit_chase_bps=500,  # limit ≈ $257.25
            stop_limit_chase_atr_fraction=None,
        )
        # Trade fires — the limit was reachable on the retrace.
        assert result.portfolio.trades.records.shape[0] == 1
        entry_price = result.portfolio.trades.records["entry_price"].iloc[0]
        # Fill price clamps at the limit ($257.25), not the open ($260).
        # vbt adds default 5 bps slippage on top → ~$257.38.
        assert entry_price == pytest.approx(257.25, rel=0.001)

    def test_gap_up_over_limit_without_retrace_still_no_fill(self):
        """Sanity: even with the new `low <= limit` gate, a bar that
        gaps above the limit AND never retraces back down still rejects.
        This locks in the original 'no chase' guarantee."""
        from backtest.runner import run_backtest, BacktestConfig

        rows = []
        for _ in range(5):
            rows.append((240.0, 241.0, 239.0, 240.0))
        rows.append((242.0, 246.0, 241.0, 245.0))
        # Bar 6: gap up, low stays above limit ($257.25 cap).
        rows.append((277.0, 280.0, 275.0, 278.0))
        for _ in range(4):
            rows.append((278.0, 279.0, 277.0, 278.0))
        df = self._df_with_explicit_oh(rows)

        result = run_backtest(
            self._FixedTriggerStrategy(entry_idx=5, trigger=245.0),
            df,
            BacktestConfig(initial_cash=10_000.0),
            stop_limit_entries=True,
            stop_limit_chase_bps=500,
            stop_limit_chase_atr_fraction=None,
        )
        assert result.portfolio.trades.records.shape[0] == 0

    # ── PR #58 review P1 #2: auto-enable for STOP_LIMIT strategies ───────

    def test_stop_limit_entries_auto_enables_for_stop_limit_strategies(self):
        """A strategy that declares preferred_order_type=STOP_LIMIT MUST
        get stop-limit fill semantics in the backtest even when callers
        don't explicitly pass stop_limit_entries=True. Otherwise every
        Donchian backtest silently uses next-open fills and diverges from
        production."""
        from backtest.runner import run_backtest, BacktestConfig
        from strategies.base import OrderType

        # A strategy whose default order type is STOP_LIMIT.
        class _AutoStopLimitStrategy(self._FixedTriggerStrategy.__bases__[0]):
            name = "auto_stop_limit"
            preferred_order_type = OrderType.STOP_LIMIT

            def __init__(self, *, entry_idx, trigger):
                super().__init__()
                self._entry_idx = entry_idx
                self._trigger = trigger

            def _raw_signals(self, df):
                n = len(df)
                e = pd.Series([i == self._entry_idx for i in range(n)], index=df.index)
                x = pd.Series(False, index=df.index)
                return SignalFrame(entries=e, exits=x)

            def trigger_prices(self, df):
                return pd.Series([self._trigger] * len(df), index=df.index)

        rows = []
        for _ in range(5):
            rows.append((240.0, 241.0, 239.0, 240.0))
        rows.append((242.0, 246.0, 241.0, 245.0))
        # Bar 6: gap up well above the limit AND no retrace. Production
        # would refuse this fill; if auto-enable did NOT fire, vbt would
        # use the bar.open ($277) as the entry price and we'd see a trade.
        rows.append((277.0, 280.0, 275.0, 278.0))
        for _ in range(4):
            rows.append((278.0, 279.0, 277.0, 278.0))
        df = self._df_with_explicit_oh(rows)

        result = run_backtest(
            _AutoStopLimitStrategy(entry_idx=5, trigger=245.0),
            df,
            BacktestConfig(initial_cash=10_000.0),
            # NOTE: stop_limit_entries NOT passed — should auto-enable.
        )
        # If auto-enable fires, the gap-up over the limit is rejected.
        # If it did NOT fire, vbt would fill at bar.open ($277) and we'd
        # see one trade record.
        assert result.portfolio.trades.records.shape[0] == 0
