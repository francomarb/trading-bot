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


class TestArmResult:
    def test_is_frozen(self, bars, regime):
        arm = run_arm("raw", bars, None, bars["AAA"].index[50])
        assert isinstance(arm, ArmResult)
        with pytest.raises(Exception):
            arm.name = "mutated"  # type: ignore[misc]
