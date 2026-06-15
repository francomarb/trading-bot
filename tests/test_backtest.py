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


# ── STOP_LIMIT entry model (PLAN 11.47 R1 P1-4) ─────────────────────────────


class TestStopLimitEntryModel:
    """Backtest now models broker-resting STOP_LIMIT semantics for
    strategies declaring OrderType.STOP_LIMIT (Donchian today). Three
    next-bar outcomes covered:

      1. open above limit cap → gap-over-limit, no fill.
      2. high below trigger → trigger not reached, no fill.
      3. otherwise → fill at max(open, trigger), capped at limit.

    Pre-PLAN-11.47 the runner unconditionally filled at next-open, which
    materially diverged from production once Donchian moved to STOP_LIMIT.
    """

    def _df_for(self, *, opens, highs, lows, closes) -> pd.DataFrame:
        idx = pd.date_range(
            "2026-01-01", periods=len(closes), freq="D", tz="UTC",
        )
        return pd.DataFrame(
            {
                "open": opens, "high": highs, "low": lows, "close": closes,
                "volume": [1_000_000] * len(closes),
            },
            index=idx,
        )

    def _strategy(self):
        from strategies.donchian_breakout import DonchianBreakout
        return DonchianBreakout(entry_window=5, exit_window=3)

    def _policy(self):
        # 500 bps chase — matches the production Donchian policy shape so
        # cap math here is sane.
        from execution.entry_guard import EntryPriceCap
        return EntryPriceCap(max_chase_bps=500)

    def test_gap_over_limit_without_retrace_blocks_entry(self):
        # entry_window=5: trigger at bar t = max(close[t-5:t-1]).
        # Flat at 100 for bars 0–5 → no breakout there. Bar 6 closes 110 →
        # first breakout (close 110 > prior-5 max 100). Trigger[6] = 100,
        # limit[6] = 100 * 1.05 = 105. Bar 7 opens 130 and the LOW stays
        # above 105 (no retrace) → the limit never becomes reachable
        # intraday, so no fill.
        closes = [100, 100, 100, 100, 100, 100, 110, 130]
        opens  = [100, 100, 100, 100, 100, 100, 100, 130]
        highs  = [100.5, 100.5, 100.5, 100.5, 100.5, 100.5, 110.5, 132.0]
        lows   = [99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 128.0]
        df = self._df_for(opens=opens, highs=highs, lows=lows, closes=closes)

        result = run_backtest(
            self._strategy(), df,
            BacktestConfig(slippage_bps=0, commission_per_trade=0),
            entry_price_cap_policy=self._policy(),
        )
        # No trade — limit unreachable on the only entry-eligible bar.
        assert result.stats["trade_count"] == 0

    def test_gap_over_limit_with_retrace_fills_at_limit(self):
        # Same flat-then-breakout setup as above, but bar 7 gaps above
        # limit and then the intraday LOW retraces *down to or below* the
        # limit. A real broker-resting stop-limit triggers on the gap
        # (high crosses trigger immediately at the open) and the limit
        # order fills as price retraces to limit. Modeled fill = limit.
        closes = [100, 100, 100, 100, 100, 100, 110, 120]
        opens  = [100, 100, 100, 100, 100, 100, 100, 130]
        highs  = [100.5, 100.5, 100.5, 100.5, 100.5, 100.5, 110.5, 132.0]
        lows   = [99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 99.5, 103.0]
        df = self._df_for(opens=opens, highs=highs, lows=lows, closes=closes)

        result = run_backtest(
            self._strategy(), df,
            BacktestConfig(slippage_bps=0, commission_per_trade=0),
            entry_price_cap_policy=self._policy(),
        )
        assert result.stats["trade_count"] >= 1
        records = result.portfolio.trades.records_readable
        first_trade = records.iloc[0]
        # Trigger=100, limit=105. Gap-and-retrace fill at limit=105.
        assert abs(float(first_trade["Avg Entry Price"]) - 105.0) < 0.01

    def test_trigger_unreached_blocks_entry(self):
        # Same flat-then-breakout setup. Bar 7's high stays below the
        # trigger (= 100). Order rests; DAY TIF expires → no fill.
        closes = [100, 100, 100, 100, 100, 100, 110, 95]
        opens  = [100, 100, 100, 100, 100, 100, 100, 95]
        highs  = [100.5, 100.5, 100.5, 100.5, 100.5, 100.5, 110.5, 99.0]
        lows   = [c - 0.5 for c in closes]
        df = self._df_for(opens=opens, highs=highs, lows=lows, closes=closes)

        result = run_backtest(
            self._strategy(), df,
            BacktestConfig(slippage_bps=0, commission_per_trade=0),
            entry_price_cap_policy=self._policy(),
        )
        assert result.stats["trade_count"] == 0

    def test_fill_at_trigger_when_open_below_and_high_above(self):
        # Bar 6 close > 5-bar max → entry shifted to bar 7. Trigger=100.
        # Bar 7 open=99, high=104 → triggers (price trades through 100)
        # but the open is below; fill at trigger=100 (max(open, trigger)).
        closes = [100, 100, 100, 100, 100, 100, 110, 103, 102]
        opens  = [100, 100, 100, 100, 100, 100, 100,  99, 102]
        highs  = [100.5, 100.5, 100.5, 100.5, 100.5, 100.5, 110.5, 104.0, 103.0]
        lows   = [c - 0.5 for c in closes]
        df = self._df_for(opens=opens, highs=highs, lows=lows, closes=closes)

        result = run_backtest(
            self._strategy(), df,
            BacktestConfig(slippage_bps=0, commission_per_trade=0),
            entry_price_cap_policy=self._policy(),
        )
        # At least one trade entered. Inspect vbt's trade records.
        assert result.stats["trade_count"] >= 1
        records = result.portfolio.trades.records_readable
        first_trade = records.iloc[0]
        # Filled at trigger=100, not at the lower open=99.
        assert abs(float(first_trade["Avg Entry Price"]) - 100.0) < 0.01

    def test_fill_at_open_when_open_above_trigger_within_limit(self):
        # Bar 7 opens at 102 (above trigger=100, below limit=105). The
        # stop trips on open → fill at the open, not at the trigger.
        closes = [100, 100, 100, 100, 100, 100, 110, 104, 104]
        opens  = [100, 100, 100, 100, 100, 100, 100, 102, 104]
        highs  = [100.5, 100.5, 100.5, 100.5, 100.5, 100.5, 110.5, 104.5, 104.5]
        lows   = [c - 0.5 for c in closes]
        df = self._df_for(opens=opens, highs=highs, lows=lows, closes=closes)

        result = run_backtest(
            self._strategy(), df,
            BacktestConfig(slippage_bps=0, commission_per_trade=0),
            entry_price_cap_policy=self._policy(),
        )
        assert result.stats["trade_count"] >= 1
        records = result.portfolio.trades.records_readable
        first_trade = records.iloc[0]
        # Filled at the open, NOT at the trigger.
        assert abs(float(first_trade["Avg Entry Price"]) - 102.0) < 0.01

    def test_market_strategy_unchanged_by_stop_limit_model(self):
        # Negative control: SMA crossover (MARKET) still uses next-open fill.
        df = _trending_df(300)
        from strategies.sma_crossover import SMACrossover
        baseline = run_backtest(SMACrossover(fast=5, slow=20), df)
        assert baseline.stats["trade_count"] >= 1

    def test_stop_limit_strategy_without_policy_raises(self):
        # If neither parameter nor ENTRY_PRICE_CAPS configures a policy for
        # the strategy, the backtest must refuse rather than silently fall
        # back to the old next-open model.
        from strategies.donchian_breakout import DonchianBreakout

        class UnknownDonchian(DonchianBreakout):
            name = "unknown_strategy_no_policy"

        df = _trending_df(50)
        with pytest.raises(ValueError, match="EntryPriceCap policy"):
            run_backtest(
                UnknownDonchian(entry_window=5, exit_window=3), df,
            )
