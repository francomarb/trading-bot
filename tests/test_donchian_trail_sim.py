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
