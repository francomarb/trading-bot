"""
Tests for the Donchian entry-gate A/B harness (`scripts/donchian_gate_ab.py`).

The comparison is only meaningful if every arm is identical except for
`entry_mask`. Per [[feedback_assert_constants_in_comparative_tests]], that
invariant is asserted here rather than claimed in the module docstring — drift
in any other knob would silently turn the headline number into noise.

The second group guards the trap that cost the trail investigation a review
round (PR #49 R2 P1): the edge-filter mask must be computed on each symbol's
FULL history. Computed on a slice, SMA200 is NaN over the warmup and the
filter *fails open*, admitting entries production would block.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scripts.donchian_gate_ab import (
    SIM_CONSTANTS,
    ArmResult,
    build_masks,
    regime_at_entry,
    run_arm,
)


def _bars(n: int = 400, seed: int = 0, start: str = "2020-01-01") -> pd.DataFrame:
    """Deterministic synthetic OHLCV with a real trend so entries actually fire."""
    rng = np.random.default_rng(seed)
    idx = pd.date_range(start, periods=n, freq="B", tz="UTC")
    steps = rng.normal(0.4, 2.0, n).cumsum()
    close = 100.0 + steps
    high = close + np.abs(rng.normal(1.0, 0.4, n))
    low = close - np.abs(rng.normal(1.0, 0.4, n))
    return pd.DataFrame(
        {
            "open": close - rng.normal(0, 0.3, n),
            "high": np.maximum(high, close),
            "low": np.minimum(low, close),
            "close": close,
            "volume": rng.integers(5_000_000, 20_000_000, n).astype(float),
        },
        index=idx,
    )


@pytest.fixture
def bars() -> dict[str, pd.DataFrame]:
    return {"AAA": _bars(seed=1), "BBB": _bars(seed=2)}


@pytest.fixture
def regime(bars) -> pd.Series:
    """Alternating 40-bar regime blocks so every branch is exercised."""
    idx = bars["AAA"].index
    labels = ["TRENDING", "RANGING", "BEAR", "VOLATILE"]
    return pd.Series([labels[(i // 40) % 4] for i in range(len(idx))], index=idx, dtype=object)


class TestArmsDifferOnlyByEntryMask:
    """The A/B is worthless if any knob other than entry_mask drifts."""

    def test_every_arm_passes_identical_sim_constants(self, bars, regime, monkeypatch):
        seen: list[dict] = []
        import scripts.donchian_gate_ab as mod

        real = mod.simulate_symbol

        def spy_sim(symbol, df, policy, **kwargs):
            seen.append({k: v for k, v in kwargs.items() if k != "entry_mask"})
            return real(symbol, df, policy, **kwargs)

        monkeypatch.setattr(mod, "simulate_symbol", spy_sim)

        regime_masks, filter_masks = build_masks(bars, regime)
        trade_start = bars["AAA"].index[50]
        for name, mask in [
            ("raw", None),
            ("regime", regime_masks),
            ("filter", filter_masks),
            ("both", {s: regime_masks[s] & filter_masks[s] for s in bars}),
        ]:
            run_arm(name, bars, mask, trade_start)

        assert seen, "simulate_symbol was never called — the spy did not bind"
        # Every call across every arm must carry byte-identical constants.
        assert all(call == seen[0] for call in seen)
        for key, value in SIM_CONSTANTS.items():
            assert seen[0][key] == value
        assert seen[0]["trade_start"] == trade_start

    def test_same_policy_object_across_arms(self, bars, regime, monkeypatch):
        import scripts.donchian_gate_ab as mod

        policies: list[object] = []
        real = mod.simulate_symbol

        def spy_sim(symbol, df, policy, **kwargs):
            policies.append(policy)
            return real(symbol, df, policy, **kwargs)

        monkeypatch.setattr(mod, "simulate_symbol", spy_sim)
        regime_masks, _ = build_masks(bars, regime)
        ts = bars["AAA"].index[50]
        run_arm("raw", bars, None, ts)
        run_arm("regime", bars, regime_masks, ts)

        assert policies, "policy never observed"
        assert all(p is policies[0] for p in policies)

    def test_gated_entries_only_occur_on_allowed_bars(self, bars, regime):
        """
        Every gated entry must sit on a bar its mask allows, and gating must
        not increase trade count.

        Note the cohort caveat this test made concrete: gated entries are NOT
        a subset of ungated ones. Suppressing an entry frees the simulator to
        take a *later* entry that the raw arm was still holding through, so
        the arms produce different trade sets. The comparison is over complete
        policy paths, not matched trades — the same caveat
        `docs/donchian_trail_investigation.md` records for its stop variants.
        """
        regime_masks, filter_masks = build_masks(bars, regime)
        ts = bars["AAA"].index[50]
        masks = {s: regime_masks[s] & filter_masks[s] for s in bars}
        raw = run_arm("raw", bars, None, ts)
        both = run_arm("both", bars, masks, ts)

        assert both.agg.total_trades <= raw.agg.total_trades
        assert both.trades, "fixture produced no gated trades to check"
        for t in both.trades:
            # Entry signal fires on the prior close and fills at this open, so
            # the mask is consulted on the signal bar.
            idx = bars[t.symbol].index
            pos = idx.get_loc(t.entry_date)
            assert masks[t.symbol].iloc[max(pos - 1, 0)], (
                f"{t.symbol} entered {t.entry_date} on a bar the mask blocks"
            )

    def test_arms_are_not_matched_trade_sets(self, bars, regime):
        """Documents the cohort caveat above as an executable fact."""
        regime_masks, filter_masks = build_masks(bars, regime)
        ts = bars["AAA"].index[50]
        raw = run_arm("raw", bars, None, ts)
        both = run_arm(
            "both", bars, {s: regime_masks[s] & filter_masks[s] for s in bars}, ts
        )
        raw_keys = {(t.symbol, t.entry_date) for t in raw.trades}
        both_keys = {(t.symbol, t.entry_date) for t in both.trades}
        assert both_keys - raw_keys, (
            "expected the gated arm to open at least one entry the raw arm "
            "never took (it was still in a position there)"
        )


class TestMaskConstruction:
    def test_regime_mask_is_trending_only(self, bars, regime):
        regime_masks, _ = build_masks(bars, regime)
        m = regime_masks["AAA"]
        aligned = regime.reindex(bars["AAA"].index).ffill().fillna("RANGING")
        assert (m == (aligned == "TRENDING")).all()
        assert m.any() and not m.all(), "fixture must exercise both branches"

    def test_filter_mask_matches_full_history_computation(self, bars, regime):
        """
        Guards PR #49 R2 P1. Computing the filter on a slice leaves SMA200 NaN
        over the warmup, where the filter fails open — so a sliced computation
        is MORE permissive. Assert the harness matches the full-history result
        and that the sliced shortcut would actually have differed.
        """
        from scripts.donchian_trail_compare import per_symbol_filter_mask

        # A series that rises then rolls over, so in the tail price sits BELOW
        # its own SMA200 — the full-history mask is False there while a sliced
        # computation has no SMA200 at all and fails open to True.
        rising = _bars(n=300, seed=7)
        falling = rising.copy().iloc[:120]
        falling.index = pd.date_range(
            rising.index[-1] + pd.Timedelta(days=1), periods=120, freq="B", tz="UTC"
        )
        drop = np.linspace(0, -60, 120)
        for col in ("open", "high", "low", "close"):
            falling[col] = rising[col].iloc[-1] + drop
        df = pd.concat([rising, falling])
        local = {"AAA": df}

        _, filter_masks = build_masks(local, regime.reindex(df.index).ffill().bfill())
        expected = per_symbol_filter_mask(df).reindex(df.index).fillna(False)
        assert (filter_masks["AAA"] == expected).all()

        sliced = df.iloc[-150:]
        sliced_mask = per_symbol_filter_mask(sliced)
        overlap = filter_masks["AAA"].reindex(sliced.index)
        assert not (sliced_mask == overlap).all(), (
            "sliced and full-history masks are identical here, so this test "
            "cannot detect the fail-open regression it exists to catch"
        )
        # And the direction must be the documented one: sliced is more permissive.
        assert sliced_mask.sum() > overlap.sum()

    def test_both_mask_is_conjunction(self, bars, regime):
        regime_masks, filter_masks = build_masks(bars, regime)
        both = regime_masks["AAA"] & filter_masks["AAA"]
        assert (both <= regime_masks["AAA"]).all()
        assert (both <= filter_masks["AAA"]).all()


class TestRegimeAtEntry:
    def test_buckets_by_entry_bar_regime(self, bars, regime):
        ts = bars["AAA"].index[50]
        raw = run_arm("raw", bars, None, ts)
        buckets = regime_at_entry(raw.trades, regime)

        assert sum(len(v) for v in buckets.values()) == len(raw.trades)
        lookup = regime.copy()
        lookup.index = lookup.index.normalize()
        for label, rs in buckets.items():
            for t in raw.trades:
                if t.r_multiple in rs and t.entry_date.normalize() in lookup.index:
                    break
        # Spot-check one trade lands in the bucket its entry bar names.
        t = raw.trades[0]
        assert t.r_multiple in buckets[str(lookup[t.entry_date.normalize()])]

    def test_no_trades_yields_empty_buckets(self, regime):
        assert regime_at_entry([], regime) == {}


class TestPreRegisteredCriteria:
    """
    Guards the arm-E pre-registration in
    docs/donchian_regime_gate_investigation.md §10. The thresholds are the
    commitment; a silent retune to fit a result is the failure mode these
    tests exist to make loud.
    """

    def test_thresholds_match_the_registered_values(self):
        from scripts import donchian_gate_ab as mod

        assert mod.PREREG_2022_MIN_SUM_R == -20.0
        assert mod.PREREG_MIN_RETURN_GAIN_PP == 5.0
        assert mod.PREREG_MAX_DD_WORSENING_PP == 3.0

    @staticmethod
    def _arm(name: str, *, ret: float, dd: float, r2022: float) -> ArmResult:
        from backtest.donchian_trail_sim import PortfolioAggregate, TradeRecord

        ts = pd.Timestamp("2022-06-01", tz="UTC")
        trade = TradeRecord(
            symbol="AAA", entry_date=ts, entry_price=100.0, exit_date=ts,
            exit_price=100.0, exit_reason="signal", bars_held=1, shares=1,
            initial_stop=90.0, risk_per_share=10.0, pnl_dollars=0.0,
            pnl_pct=0.0, r_multiple=r2022,
        )
        agg = PortfolioAggregate(
            policy_name=name, n_symbols=1, mean_total_return=ret, mean_cagr=0.0,
            mean_sharpe=0.0, mean_max_drawdown=dd, mean_buy_hold=0.0,
            total_trades=1, win_rate=0.0, avg_r=r2022, expectancy_pct=0.0,
            pct_stop_gap=0.0, pct_stop_intrabar=0.0, pct_signal_exit=1.0, pct_eod=0.0,
        )
        return ArmResult(name=name, agg=agg, trades=[trade])

    def test_passes_when_all_three_criteria_are_met(self):
        from scripts.donchian_gate_ab import render_prereg_verdict

        prod = self._arm("D", ret=0.274, dd=-0.150, r2022=-9.1)
        good = self._arm("E", ret=0.400, dd=-0.160, r2022=-12.0)
        out = render_prereg_verdict(good, prod)
        assert "VERDICT: PASS" in out
        assert out.count("[PASS]") == 3

    def test_c1_failure_rejects_even_with_a_great_aggregate(self):
        """The decisive criterion: lose the 2022 protection and it is rejected."""
        from scripts.donchian_gate_ab import render_prereg_verdict

        prod = self._arm("D", ret=0.274, dd=-0.150, r2022=-9.1)
        bad = self._arm("E", ret=0.900, dd=-0.150, r2022=-45.0)
        out = render_prereg_verdict(bad, prod)
        assert "VERDICT: REJECTED" in out
        assert "C1" in out.split("REJECTED")[1]

    def test_c3_failure_rejects_on_drawdown(self):
        from scripts.donchian_gate_ab import render_prereg_verdict

        prod = self._arm("D", ret=0.274, dd=-0.150, r2022=-9.1)
        bad = self._arm("E", ret=0.500, dd=-0.190, r2022=-10.0)
        out = render_prereg_verdict(bad, prod)
        assert "VERDICT: REJECTED" in out and "C3" in out.split("REJECTED")[1]

    def test_boundaries_are_inclusive_as_written(self):
        from scripts.donchian_gate_ab import render_prereg_verdict

        prod = self._arm("D", ret=0.274, dd=-0.150, r2022=-9.1)
        edge = self._arm("E", ret=0.324, dd=-0.180, r2022=-20.0)
        assert "VERDICT: PASS" in render_prereg_verdict(edge, prod)


class TestBearOnlyMask:
    def test_blocks_only_bear(self, bars, regime):
        _, filter_masks = build_masks(bars, regime)
        aligned = regime.reindex(bars["AAA"].index).ffill().fillna("RANGING")
        mask = ((aligned != "BEAR") & filter_masks["AAA"]).astype(bool)

        assert not mask[aligned == "BEAR"].any(), "BEAR bars must be blocked"
        allowed = mask[(aligned != "BEAR") & filter_masks["AAA"]]
        assert allowed.all(), "non-BEAR bars passing the filter must be allowed"

    def test_is_strictly_looser_than_the_trending_only_gate(self, bars, regime):
        regime_masks, filter_masks = build_masks(bars, regime)
        aligned = regime.reindex(bars["AAA"].index).ffill().fillna("RANGING")
        bear_only = ((aligned != "BEAR") & filter_masks["AAA"]).astype(bool)
        production = (regime_masks["AAA"] & filter_masks["AAA"]).astype(bool)

        assert (production <= bear_only).all(), "must admit a superset of production"
        assert bear_only.sum() > production.sum(), "fixture must exercise the difference"


class TestLiveValidationWindow:
    """
    Regression guard for the P2 found in review of PR #111: `--live-start`
    filtered the simulated side only, while the SQL loaded every closed
    Donchian row. The block then reported a MODEL vs LIVE comparison over two
    different windows while claiming to validate an overlap.

    The default happened to be valid when it shipped (all 24 live rows begin
    on the default start), which is exactly why it needed a test rather than
    an eyeball.
    """

    @staticmethod
    def _db(tmp_path, rows: list[tuple[str | None, float, float]]) -> str:
        import sqlite3

        path = tmp_path / "trades.db"
        con = sqlite3.connect(path)
        con.execute(
            "CREATE TABLE trades (strategy TEXT, entry_timestamp TEXT, "
            "r_multiple REAL, realized_pnl REAL)"
        )
        con.executemany(
            "INSERT INTO trades (strategy, entry_timestamp, r_multiple, realized_pnl) "
            "VALUES ('donchian_breakout', ?, ?, ?)",
            rows,
        )
        con.commit()
        con.close()
        return str(path)

    @staticmethod
    def _arm() -> ArmResult:
        from backtest.donchian_trail_sim import PortfolioAggregate

        agg = PortfolioAggregate(
            policy_name="D", n_symbols=1, mean_total_return=0.0, mean_cagr=0.0,
            mean_sharpe=0.0, mean_max_drawdown=0.0, mean_buy_hold=0.0,
            total_trades=0, win_rate=0.0, avg_r=0.0, expectancy_pct=0.0,
            pct_stop_gap=0.0, pct_stop_intrabar=0.0, pct_signal_exit=0.0, pct_eod=0.0,
        )
        return ArmResult(name="D", agg=agg, trades=[])

    def test_live_rows_before_live_start_are_excluded(self, tmp_path):
        from scripts.donchian_gate_ab import render_live_validation

        db = self._db(tmp_path, [
            ("2026-03-10T14:00:00+00:00", +5.0, 500.0),   # pre-window, big winner
            ("2026-04-20T14:00:00+00:00", +5.0, 500.0),   # pre-window, big winner
            ("2026-06-02T14:00:00+00:00", -1.0, -100.0),  # in-window
            ("2026-07-15T14:00:00+00:00", -1.0, -100.0),  # in-window
        ])
        out = render_live_validation(
            self._arm(), pd.Timestamp("2026-05-01", tz="UTC"), db
        )
        # Only the two in-window losers may count: n=2, 0% wins, mean R -1.00.
        assert "n=  2" in out, out
        assert "win%=  0.0" in out, out
        assert "mean R=-1.00" in out, out

    def test_moving_live_start_moves_the_live_side_too(self, tmp_path):
        """The exact failure: the model side moved, the live side did not."""
        from scripts.donchian_gate_ab import render_live_validation

        db = self._db(tmp_path, [
            ("2026-05-02T14:00:00+00:00", -1.0, -100.0),
            ("2026-07-15T14:00:00+00:00", +3.0, 300.0),
        ])
        arm = self._arm()
        may = render_live_validation(arm, pd.Timestamp("2026-05-01", tz="UTC"), db)
        july = render_live_validation(arm, pd.Timestamp("2026-07-01", tz="UTC"), db)

        assert "n=  2" in may and "mean R=+1.00" in may, may
        assert "n=  1" in july and "mean R=+3.00" in july, july

    def test_boundary_row_on_live_start_is_included(self, tmp_path):
        from scripts.donchian_gate_ab import render_live_validation

        db = self._db(tmp_path, [("2026-05-01T09:30:00+00:00", -1.0, -100.0)])
        out = render_live_validation(
            self._arm(), pd.Timestamp("2026-05-01", tz="UTC"), db
        )
        assert "n=  1" in out, out

    def test_rows_without_entry_timestamp_are_reported_not_dropped(self, tmp_path):
        from scripts.donchian_gate_ab import render_live_validation

        db = self._db(tmp_path, [
            ("2026-06-02T14:00:00+00:00", -1.0, -100.0),
            (None, -2.0, -200.0),
        ])
        out = render_live_validation(
            self._arm(), pd.Timestamp("2026-05-01", tz="UTC"), db
        )
        assert "n=  1" in out, out
        assert "1 closed row(s) have no entry_timestamp" in out, out


class TestArmResult:
    def test_is_frozen(self, bars, regime):
        arm = run_arm("raw", bars, None, bars["AAA"].index[50])
        assert isinstance(arm, ArmResult)
        with pytest.raises(Exception):
            arm.name = "mutated"  # type: ignore[misc]
