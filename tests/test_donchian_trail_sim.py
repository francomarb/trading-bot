"""
Unit tests for `backtest.donchian_trail_sim`.

Coverage targets:
  - Stop policies update correctly and never loosen (monotonic ratchet).
  - Static stop is invariant once placed.
  - Donchian-low trail honors the wick buffer.
  - Chandelier uses HWM-close - k*ATR.
  - Simulator fill semantics: gap-through at open, intrabar at stop level,
    signal exit at next open, EOD position closes at last close.
  - No look-ahead: stop level for bar t cannot reference bar t high/low.
  - Initial sizing is identical across policies (so any A/B difference comes
    from the exit, not from sizing drift).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from backtest.donchian_trail_sim import (
    ChandelierStop,
    DonchianLowTrail,
    PortfolioAggregate,
    StaticATRStop,
    aggregate,
    simulate_symbol,
)


# ── Stop policy unit tests ───────────────────────────────────────────────────


class TestStaticATRStop:
    def test_initial_stop_is_entry_minus_k_atr(self) -> None:
        p = StaticATRStop(k=2.0)
        assert p.initial_stop(entry_price=100.0, atr_at_entry=3.0) == pytest.approx(94.0)

    def test_stop_never_moves(self) -> None:
        p = StaticATRStop(k=2.0)
        # Even with HWM, ATR, donchian-low all changing, the stop stays put.
        assert p.update_stop(
            prev_stop=94.0,
            hwm_close=140.0,
            atr_today=2.0,
            donchian_low_today=130.0,
        ) == 94.0


class TestDonchianLowTrail:
    def test_initial_stop_matches_static(self) -> None:
        # Initial stop is the same as the static policy so per-trade sizing
        # is identical for the A/B comparison.
        p = DonchianLowTrail(initial_k=2.0, buffer_atr=0.5)
        assert p.initial_stop(entry_price=100.0, atr_at_entry=3.0) == pytest.approx(94.0)

    def test_trails_up_with_donchian_low_minus_buffer(self) -> None:
        p = DonchianLowTrail(initial_k=2.0, buffer_atr=0.5)
        # donchian_low = 110, atr = 2 → candidate = 110 - 0.5*2 = 109. Higher
        # than prev_stop 94, so trail moves up.
        assert p.update_stop(
            prev_stop=94.0,
            hwm_close=120.0,
            atr_today=2.0,
            donchian_low_today=110.0,
        ) == pytest.approx(109.0)

    def test_never_loosens(self) -> None:
        p = DonchianLowTrail(initial_k=2.0, buffer_atr=0.5)
        # donchian_low dropped to 90 → candidate 89. prev_stop is already 109.
        # Stop must NOT go down to 89.
        assert p.update_stop(
            prev_stop=109.0,
            hwm_close=120.0,
            atr_today=2.0,
            donchian_low_today=90.0,
        ) == 109.0

    def test_ignores_nan_inputs(self) -> None:
        p = DonchianLowTrail()
        # During warmup donchian_low is NaN — keep prev_stop unchanged.
        assert p.update_stop(
            prev_stop=94.0,
            hwm_close=100.0,
            atr_today=2.0,
            donchian_low_today=float("nan"),
        ) == 94.0


class TestChandelierStop:
    def test_initial_stop_matches_static(self) -> None:
        p = ChandelierStop(initial_k=2.0, k=3.0)
        assert p.initial_stop(entry_price=100.0, atr_at_entry=3.0) == pytest.approx(94.0)

    def test_trails_up_with_hwm_minus_k_atr(self) -> None:
        p = ChandelierStop(initial_k=2.0, k=3.0)
        # HWM = 130, ATR = 2 → candidate = 130 - 6 = 124. Higher than 94.
        assert p.update_stop(
            prev_stop=94.0,
            hwm_close=130.0,
            atr_today=2.0,
            donchian_low_today=110.0,
        ) == pytest.approx(124.0)

    def test_never_loosens_when_atr_expands(self) -> None:
        p = ChandelierStop(initial_k=2.0, k=3.0)
        # ATR doubled — candidate = 130 - 12 = 118. Below prev_stop 124.
        # Chandelier must NOT loosen.
        assert p.update_stop(
            prev_stop=124.0,
            hwm_close=130.0,
            atr_today=4.0,
            donchian_low_today=110.0,
        ) == 124.0


# ── Simulator behavior ──────────────────────────────────────────────────────


def _build_synthetic_breakout(n_warmup: int = 50, n_trend: int = 30) -> pd.DataFrame:
    """
    Build a synthetic OHLC frame: flat warmup → step-up breakout → uptrend.

    The flat warmup means donchian_high computes a low, stable value. The
    breakout bar pushes close above the prior high. The subsequent uptrend
    keeps making new highs so the strategy stays in.
    """
    start = datetime(2024, 1, 2, tzinfo=timezone.utc)
    rows = []
    # Phase 1: flat range close=100, daily high=101, low=99
    for i in range(n_warmup):
        rows.append({"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0})
    # Phase 2: step uptrend (close rises by $1/bar)
    for i in range(n_trend):
        c = 100.0 + (i + 1) * 1.0
        rows.append({"open": c - 0.5, "high": c + 0.5, "low": c - 1.0, "close": c})

    idx = pd.DatetimeIndex(
        [start + timedelta(days=i) for i in range(len(rows))], tz="UTC"
    )
    df = pd.DataFrame(rows, index=idx)
    df["volume"] = 1000
    return df


class TestSimulatorBehavior:
    def test_eod_close_on_final_bar(self) -> None:
        # Trend never breaks → position is force-closed at the last bar's close.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=30)
        result = simulate_symbol(
            "TEST", df, StaticATRStop(k=2.0),
            entry_window=30, exit_window=15, atr_length=14,
            slippage_bps=0.0,
        )
        assert result.trade_count >= 1
        # The last open trade closes at the last bar's close with reason 'eod'.
        last = result.trades[-1]
        assert last.exit_reason == "eod"
        assert last.exit_date == df.index[-1]

    def test_gap_through_fill_at_open(self) -> None:
        # Build: trend until a sudden gap-down that opens BELOW the static stop.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=10)
        # Insert a gap-down on the next bar: open well below prior close
        gap_open = 80.0  # far below the entry-bar stop ~98 region
        last_close = df["close"].iloc[-1]
        new_bar = pd.DataFrame(
            {"open": gap_open, "high": gap_open + 0.5, "low": gap_open - 0.5,
             "close": gap_open, "volume": 1000},
            index=[df.index[-1] + timedelta(days=1)],
        )
        df_with_gap = pd.concat([df, new_bar])
        result = simulate_symbol(
            "TEST", df_with_gap, StaticATRStop(k=2.0),
            entry_window=30, exit_window=15, atr_length=14,
            slippage_bps=0.0,
        )
        # The trade that was open before the gap must close with reason
        # 'stop_gap' at a fill price equal to the gap-open.
        stop_gap_trades = [t for t in result.trades if t.exit_reason == "stop_gap"]
        assert len(stop_gap_trades) >= 1
        gapped = stop_gap_trades[-1]
        assert gapped.exit_price == pytest.approx(gap_open, abs=1e-9)
        assert last_close > gap_open  # sanity: it actually gapped

    def test_intrabar_stop_fills_at_stop_level(self) -> None:
        # Build a trend, then a bar whose open is ABOVE the stop but whose
        # intraday low pierces it. The stop must fill at the stop level, not
        # at the low.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=5)
        # After 5 trend bars, close is around 105. With ATR ~1.0 (flat warmup
        # tightens ATR significantly) and k=2.0, the stop is well above 100.
        # Insert a probe bar: open at last close, low piercing the stop region,
        # then recover.
        last_close = df["close"].iloc[-1]
        probe_open = last_close
        probe_low = last_close - 20.0  # deep wick down
        probe_close = last_close - 1.0
        probe_high = last_close + 0.5
        new_bar = pd.DataFrame(
            {"open": probe_open, "high": probe_high, "low": probe_low,
             "close": probe_close, "volume": 1000},
            index=[df.index[-1] + timedelta(days=1)],
        )
        df_with_probe = pd.concat([df, new_bar])
        result = simulate_symbol(
            "TEST", df_with_probe, StaticATRStop(k=2.0),
            entry_window=30, exit_window=15, atr_length=14,
            slippage_bps=0.0,
        )
        intrabar_trades = [t for t in result.trades if t.exit_reason == "stop_intrabar"]
        # If the wick was deep enough relative to the stop, we got an intrabar fill.
        assert len(intrabar_trades) >= 1
        t = intrabar_trades[-1]
        # The fill price equals the stop level (= initial_stop for the static
        # policy). Slippage is zero so they match exactly.
        assert t.exit_price == pytest.approx(t.initial_stop, abs=1e-9)
        # Fill price is strictly above the bar's low — the stop fired before
        # the low printed.
        assert t.exit_price > probe_low

    def test_no_look_ahead_in_static_stop(self) -> None:
        # The static stop is fully determined by the entry-bar fill and the
        # ATR value as-of the bar BEFORE entry. Inserting a wild future bar
        # must not retroactively change the trade record up to that point.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=8)
        result_baseline = simulate_symbol(
            "TEST", df, StaticATRStop(k=2.0),
            entry_window=30, exit_window=15, atr_length=14,
            slippage_bps=0.0,
        )
        baseline_trades = result_baseline.trades

        # Append a wild future bar
        wild_bar = pd.DataFrame(
            {"open": 50.0, "high": 51.0, "low": 49.0, "close": 50.0, "volume": 1000},
            index=[df.index[-1] + timedelta(days=1)],
        )
        df2 = pd.concat([df, wild_bar])
        result2 = simulate_symbol(
            "TEST", df2, StaticATRStop(k=2.0),
            entry_window=30, exit_window=15, atr_length=14,
            slippage_bps=0.0,
        )
        # Any trade that closed BEFORE the new bar must be byte-identical
        # (same entry, exit, fill price). The EOD-closed trade in baseline
        # is the only one that can change (it was force-closed earlier).
        wild_date = df2.index[-1]
        for t_old in baseline_trades:
            if t_old.exit_reason == "eod":
                continue  # this gets re-resolved with the new data
            match = [t for t in result2.trades
                     if t.entry_date == t_old.entry_date
                     and t.exit_date == t_old.exit_date]
            assert len(match) == 1, f"missing match for {t_old}"
            t_new = match[0]
            assert t_new.exit_price == pytest.approx(t_old.exit_price, abs=1e-9)
            assert t_new.exit_reason == t_old.exit_reason

    def test_initial_sizing_identical_across_policies(self) -> None:
        # The initial stop is set by initial_k * ATR_at_entry — same across
        # all three policies — so for any first trade, position size should
        # match. Anything diverging means a policy is sneaking different
        # risk into the sizing.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=10)
        kwargs = dict(
            entry_window=30, exit_window=15, atr_length=14,
            slippage_bps=0.0, initial_cash=100_000.0, risk_per_trade_pct=0.02,
        )
        r_static = simulate_symbol("T", df, StaticATRStop(k=2.0), **kwargs)
        r_low = simulate_symbol("T", df, DonchianLowTrail(initial_k=2.0, buffer_atr=0.5), **kwargs)
        r_chan = simulate_symbol("T", df, ChandelierStop(initial_k=2.0, k=3.0), **kwargs)
        assert r_static.trades and r_low.trades and r_chan.trades
        assert r_static.trades[0].shares == r_low.trades[0].shares == r_chan.trades[0].shares
        assert r_static.trades[0].initial_stop == pytest.approx(r_low.trades[0].initial_stop)
        assert r_static.trades[0].initial_stop == pytest.approx(r_chan.trades[0].initial_stop)

    def test_donchian_low_trail_captures_more_profit_in_strong_trend(self) -> None:
        # In a clean unbroken uptrend the Donchian-low trail rides up, while
        # the static stop sits at the original level. When we force a
        # gap-down at the end, the trail should book a smaller giveback.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=25)
        # End with a deep gap that takes out both stops. Static stop sits
        # near 96-97 (entry ~100, ATR ~2, k=2). Trail rides up near 120+.
        # Gap to 90 takes both out.
        gap_open = 90.0
        new_bar = pd.DataFrame(
            {"open": gap_open, "high": gap_open + 0.5, "low": gap_open - 0.5,
             "close": gap_open, "volume": 1000},
            index=[df.index[-1] + timedelta(days=1)],
        )
        df_with_gap = pd.concat([df, new_bar])

        kwargs = dict(
            entry_window=30, exit_window=15, atr_length=14,
            slippage_bps=0.0, initial_cash=100_000.0, risk_per_trade_pct=0.02,
        )
        r_static = simulate_symbol("T", df_with_gap, StaticATRStop(k=2.0), **kwargs)
        r_low = simulate_symbol(
            "T", df_with_gap, DonchianLowTrail(initial_k=2.0, buffer_atr=0.5), **kwargs
        )
        # Both should have exactly one trade that exits via stop_gap.
        s_trade = r_static.trades[-1]
        l_trade = r_low.trades[-1]
        assert s_trade.exit_reason == "stop_gap"
        # The trailing variant's exit price should be HIGHER than the static —
        # it ratcheted the stop up during the trend, so the gap fills at a
        # better level (or at the same level if the gap blew through both,
        # but the open should still be above the static stop here).
        assert l_trade.exit_price >= s_trade.exit_price


# ── Aggregation ─────────────────────────────────────────────────────────────


class TestTradeStartAndEntryMask:
    """
    Guards against the warmup-leak bug reviewers flagged: production-realistic
    backtests need indicators warmed up on bars before window.start, but no
    entry should fill on those bars and no warmup bar should pollute the
    window's equity-curve metrics.
    """

    def test_trade_start_blocks_pre_window_entries(self) -> None:
        # Build a frame where the warmup region triggers an entry too. Without
        # trade_start that entry counts in the trade list and skews returns.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=20)
        # Choose trade_start AFTER the breakout would have occurred so the
        # gated run records zero trades — proves the gate works.
        trade_start = df.index[60]  # 10 bars into the trend
        result_ungated = simulate_symbol(
            "T", df, StaticATRStop(k=2.0), slippage_bps=0.0,
        )
        result_gated = simulate_symbol(
            "T", df, StaticATRStop(k=2.0), slippage_bps=0.0,
            trade_start=trade_start,
        )
        # Ungated had at least one trade (the breakout near bar 50).
        assert result_ungated.trade_count >= 1
        # Gated: every trade's entry_date >= trade_start.
        for t in result_gated.trades:
            assert t.entry_date >= trade_start

    def test_warmup_excluded_from_metrics(self) -> None:
        # equity_curve is sliced to in-window; total_return is computed off
        # the in-window starting cash, not the pre-warmup starting cash.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=20)
        trade_start = df.index[55]
        result = simulate_symbol(
            "T", df, StaticATRStop(k=2.0), slippage_bps=0.0,
            trade_start=trade_start,
        )
        assert result.equity_curve.index[0] >= trade_start
        # bars now reflects in-window bar count, not the full frame.
        assert result.bars == len(df) - 55

    def test_entry_mask_blocks_entries(self) -> None:
        # Mask all entries off → zero trades regardless of how strong the
        # breakout is.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=20)
        mask = pd.Series(False, index=df.index, dtype=bool)
        result = simulate_symbol(
            "T", df, StaticATRStop(k=2.0), slippage_bps=0.0,
            entry_mask=mask,
        )
        assert result.trade_count == 0

    def test_entry_mask_index_mismatch_raises(self) -> None:
        df = _build_synthetic_breakout(n_warmup=50, n_trend=20)
        bad_mask = pd.Series(True, index=df.index[:-1], dtype=bool)
        with pytest.raises(ValueError, match="entry_mask"):
            simulate_symbol(
                "T", df, StaticATRStop(k=2.0), slippage_bps=0.0,
                entry_mask=bad_mask,
            )

    def test_entry_mask_partial_allow(self) -> None:
        # Allow only a window in the middle of the trend. Entries outside that
        # window must not fire.
        df = _build_synthetic_breakout(n_warmup=50, n_trend=20)
        mask = pd.Series(False, index=df.index, dtype=bool)
        mask.iloc[55:65] = True  # allow only bars 55..64
        result = simulate_symbol(
            "T", df, StaticATRStop(k=2.0), slippage_bps=0.0,
            entry_mask=mask,
        )
        for t in result.trades:
            # The triggering close was on bar i-1; the entry fills on bar i.
            # Mask is read on the signal bar (close). Just check the trade
            # entered within (or shortly after) the mask window.
            entry_pos = df.index.get_loc(t.entry_date)
            assert 56 <= entry_pos <= 66, (
                f"trade entered at position {entry_pos}, outside expected window"
            )


class TestFilterMaskOnFullHistory:
    """
    PR #49 follow-up flagged that computing the DonchianEdgeFilter mask on the
    sliced 50-bar-warmup window left SMA200 NaN for ~150 bars and silently
    failed open, allowing entries production would block (93 in 2022 window,
    58 in 2023-24). The fix is to compute the filter on full cached history
    then reindex onto the window. This test pins that behavior so a future
    refactor can't reintroduce the leak.
    """

    def _build_long_history(self, n: int = 400) -> pd.DataFrame:
        # 400 daily bars with a clear uptrend so close > SMA200 from bar ~200 on.
        start = datetime(2022, 1, 3, tzinfo=timezone.utc)
        idx = pd.DatetimeIndex(
            [start + timedelta(days=i) for i in range(n)], tz="UTC"
        )
        prices = [100.0 + i * 0.5 for i in range(n)]
        return pd.DataFrame({
            "open":  prices,
            "high":  [p + 0.5 for p in prices],
            "low":   [p - 0.5 for p in prices],
            "close": prices,
            "volume": [10_000_000] * n,
        }, index=idx)

    def test_filter_on_full_history_then_reindex_keeps_sma200_valid(self) -> None:
        from scripts.donchian_trail_compare import per_symbol_filter_mask

        full = self._build_long_history(n=400)
        full_mask = per_symbol_filter_mask(full)

        # The mask must be valid (non-NaN, computed against a real SMA200)
        # at every bar from index 199 onward.
        for i in range(200, len(full)):
            assert not pd.isna(full_mask.iloc[i]), (
                f"bar {i} filter mask is NaN — SMA200 not computed"
            )

        # Now simulate the bug fix: take a window that starts at bar 250 with
        # only 50 warmup bars (window slice starts at bar 200). The OLD path
        # would compute SMA200 on this 50-bar warmup → NaN for the first 200
        # bars of the slice → fail open. The NEW path uses the full-history
        # mask reindexed to the slice → real SMA200 at the boundary.
        sliced = full.iloc[200:]
        bad_mask = per_symbol_filter_mask(sliced)  # the buggy path
        good_mask = full_mask.reindex(sliced.index).fillna(False)  # the fixed path

        # At bar position 0 of the slice (= bar 200 of full), the buggy mask
        # is True ONLY because SMA200 is NaN and the filter fails open.
        # The good mask reflects the real SMA200 evaluation.
        assert bad_mask.iloc[0] == True  # noqa: E712  fails open on NaN SMA
        # The good mask carries a real evaluation. In this synthetic uptrend
        # close > SMA200 by bar 200, so good_mask should be True too — but for
        # the right reason (not because SMA was missing).
        from indicators.technicals import add_sma
        full_with_sma = add_sma(full, 200)
        assert pd.notna(full_with_sma["sma_200"].iloc[200])  # SMA200 IS computable
        # And the production-faithful mask matches the structurally-correct value.
        assert good_mask.iloc[0] == (full["close"].iloc[200] > full_with_sma["sma_200"].iloc[200])


class TestRegimeParity:
    """
    Single-source-of-truth parity check between the comparison harness's
    per-bar SPY regime classifier and the live `RegimeDetector._classify`.
    PR #49 follow-up flagged that my defaults diverged from production
    (vol_pct_window 252 vs 126, threshold 0.90 vs 0.80, sma_slope_bars 20
    vs 5). This test pins the parity so a future drift breaks the build.
    """

    def _build_spy_frame(self, n: int = 600) -> pd.DataFrame:
        """
        Build a synthetic SPY-like frame deterministically. We don't need
        realistic SPY — we just need each regime branch (BEAR / VOLATILE /
        TRENDING / RANGING) to actually fire at some point so the parity
        check exercises all of them.
        """
        import numpy as np
        rng = np.random.default_rng(42)
        # Mix of regimes: 200 quiet uptrend, 100 volatile chop, 150 downtrend, 150 trend up
        prices = []
        p = 100.0
        for _ in range(200):
            p *= 1.0 + rng.normal(0.0005, 0.005)
            prices.append(p)
        for _ in range(100):
            p *= 1.0 + rng.normal(0.0, 0.025)  # vol spike
            prices.append(p)
        for _ in range(150):
            p *= 1.0 + rng.normal(-0.003, 0.012)  # bear
            prices.append(p)
        for _ in range(150):
            p *= 1.0 + rng.normal(0.0015, 0.008)  # trend up
            prices.append(p)
        prices = prices[:n]
        start = datetime(2022, 1, 3, tzinfo=timezone.utc)
        idx = pd.DatetimeIndex(
            [start + timedelta(days=i) for i in range(n)], tz="UTC"
        )
        df = pd.DataFrame({
            "open":  prices,
            "high":  [p * 1.005 for p in prices],
            "low":   [p * 0.995 for p in prices],
            "close": prices,
            "volume": [1_000_000] * n,
        }, index=idx)
        return df

    def test_classifier_defaults_match_regime_detector(self) -> None:
        """The per-bar classifier's last-bar value must match
        RegimeDetector._classify when both are run on the same SPY frame
        with production defaults."""
        from regime.detector import MarketRegime, RegimeDetector
        from scripts.donchian_trail_compare import classify_spy_regime

        spy = self._build_spy_frame(n=600)
        det = RegimeDetector()  # production defaults
        regimes_series = classify_spy_regime(spy)

        # Sample as-of dates spaced through the series so each regime branch
        # actually gets exercised. Need at least 200 bars for SMA200, so
        # start sampling at bar 250.
        sample_positions = list(range(250, len(spy), 25))
        assert len(sample_positions) >= 5

        mismatches = []
        for pos in sample_positions:
            as_of_frame = spy.iloc[: pos + 1]
            live_regime = det._classify(as_of_frame)
            series_label = regimes_series.iloc[pos]
            if live_regime.value.upper() != series_label:
                mismatches.append(
                    f"pos {pos} ({as_of_frame.index[-1].date()}): "
                    f"live={live_regime.value.upper()} series={series_label}"
                )

        assert not mismatches, (
            "Regime classifier diverged from RegimeDetector at "
            f"{len(mismatches)}/{len(sample_positions)} sample points:\n"
            + "\n".join(mismatches[:10])
        )


class TestAggregate:
    def test_empty_results_raises(self) -> None:
        with pytest.raises(ValueError):
            aggregate([])

    def test_aggregate_computes_mean_metrics(self) -> None:
        df = _build_synthetic_breakout(n_warmup=50, n_trend=15)
        r1 = simulate_symbol("A", df, StaticATRStop(k=2.0), slippage_bps=0.0)
        r2 = simulate_symbol("B", df, StaticATRStop(k=2.0), slippage_bps=0.0)
        agg = aggregate([r1, r2])
        assert isinstance(agg, PortfolioAggregate)
        assert agg.n_symbols == 2
        assert agg.policy_name == "static_atr"
        assert agg.total_trades == len(r1.trades) + len(r2.trades)
        # Exit-reason fractions must sum to 1 when there are trades.
        if agg.total_trades:
            total_pct = (
                agg.pct_stop_gap + agg.pct_stop_intrabar
                + agg.pct_signal_exit + agg.pct_eod
            )
            assert total_pct == pytest.approx(1.0, abs=1e-9)
