"""
Unit tests for strategies/base.py and strategies/sma_crossover.py.

Every signal is derived from a hand-crafted synthetic price path where the
crossover bars are known in advance — the tests function as a spec.

Coverage:
  - SignalFrame contract (shape, dtype, index alignment)
  - BaseStrategy abstract: cannot instantiate, subclass contract
  - Edge filter: AND-gates entries, never blocks exits, handles missing index
  - SMACrossover param validation
  - SMACrossover signals on canonical up-then-down price path
  - SMACrossover on constant / monotonic paths (no spurious signals)
  - **Look-ahead guard**: truncating the input must not change past signals
  - Purity: repeated calls are identical; input df is not mutated
"""

from __future__ import annotations

import pandas as pd
import pytest

from strategies.base import BaseStrategy, OrderType, SignalFrame
from strategies.sma_crossover import SMACrossover


# ── Helpers ──────────────────────────────────────────────────────────────────


def _df(closes: list[float]) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=len(closes), freq="D", tz="UTC")
    return pd.DataFrame({"close": closes}, index=idx)


# ── SignalFrame ──────────────────────────────────────────────────────────────


class TestSignalFrame:
    def test_construct_with_matching_indices(self):
        idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
        sf = SignalFrame(
            entries=pd.Series([False, True, False], index=idx),
            exits=pd.Series([False, False, True], index=idx),
        )
        assert sf.entries.sum() == 1
        assert sf.exits.sum() == 1

    def test_mismatched_indices_raises(self):
        idx_a = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
        idx_b = pd.date_range("2026-02-01", periods=3, freq="D", tz="UTC")
        with pytest.raises(ValueError, match="same index"):
            SignalFrame(
                entries=pd.Series([False] * 3, index=idx_a),
                exits=pd.Series([False] * 3, index=idx_b),
            )

    def test_non_boolean_raises(self):
        idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
        with pytest.raises(ValueError, match="boolean"):
            SignalFrame(
                entries=pd.Series([0, 1, 0], index=idx),  # int, not bool
                exits=pd.Series([False] * 3, index=idx),
            )


# ── BaseStrategy abstract contract ───────────────────────────────────────────


class TestBaseStrategyAbstract:
    def test_cannot_instantiate_base(self):
        with pytest.raises(TypeError):
            BaseStrategy()  # type: ignore[abstract]

    def test_subclass_without_raw_signals_cannot_instantiate(self):
        class Broken(BaseStrategy):
            name = "broken"

        with pytest.raises(TypeError):
            Broken()  # type: ignore[abstract]

    def test_subclass_with_raw_signals_works(self):
        class Minimal(BaseStrategy):
            name = "minimal"

            def _raw_signals(self, df):
                empty = pd.Series([False] * len(df), index=df.index)
                return SignalFrame(entries=empty, exits=empty)

        s = Minimal()
        sig = s.generate_signals(_df([1, 2, 3]))
        assert sig.entries.sum() == 0
        assert sig.exits.sum() == 0


# ── Edge filter ──────────────────────────────────────────────────────────────


class TestEdgeFilter:
    def _always_true_strategy(self):
        class AlwaysEnter(BaseStrategy):
            name = "always"

            def _raw_signals(self, df):
                entries = pd.Series([True] * len(df), index=df.index)
                exits = pd.Series([False] * len(df), index=df.index)
                return SignalFrame(entries=entries, exits=exits)

        return AlwaysEnter

    def test_no_filter_passes_raw(self):
        df = _df([1, 2, 3])
        sig = self._always_true_strategy()().generate_signals(df)
        assert sig.entries.sum() == 3

    def test_filter_blocks_entries_where_false(self):
        df = _df([1, 2, 3, 4, 5])
        gate = pd.Series([True, False, True, False, True], index=df.index)
        sig = self._always_true_strategy()(edge_filter=lambda _df: gate).generate_signals(df)
        assert sig.entries.tolist() == [True, False, True, False, True]

    def test_filter_does_not_block_exits(self):
        class AlwaysExit(BaseStrategy):
            name = "always_exit"

            def _raw_signals(self, df):
                return SignalFrame(
                    entries=pd.Series([False] * len(df), index=df.index),
                    exits=pd.Series([True] * len(df), index=df.index),
                )

        df = _df([1, 2, 3])
        gate = pd.Series([False] * len(df), index=df.index)
        sig = AlwaysExit(edge_filter=lambda _df: gate).generate_signals(df)
        assert sig.exits.sum() == 3, "edge filter must never block exits"

    def test_filter_missing_index_treated_as_false(self):
        # If the filter is built from SPY data that only covers part of our
        # window, missing dates must default to False (regime unknown ⇒ no entry).
        df = _df([1, 2, 3, 4])
        partial_gate = pd.Series([True, True], index=df.index[:2])
        sig = self._always_true_strategy()(
            edge_filter=lambda _df: partial_gate
        ).generate_signals(df)
        assert sig.entries.tolist() == [True, True, False, False]

    def test_filter_must_return_series(self):
        df = _df([1, 2, 3])
        sig = self._always_true_strategy()(edge_filter=lambda _df: True)
        with pytest.raises(TypeError, match="pd.Series"):
            sig.generate_signals(df)


# ── SMACrossover param validation ───────────────────────────────────────────


class TestSMACrossoverParams:
    def test_fast_must_be_less_than_slow(self):
        with pytest.raises(ValueError, match="strictly less"):
            SMACrossover(fast=20, slow=20)
        with pytest.raises(ValueError, match="strictly less"):
            SMACrossover(fast=50, slow=20)

    def test_windows_must_be_positive(self):
        with pytest.raises(ValueError, match="positive"):
            SMACrossover(fast=0, slow=5)
        with pytest.raises(ValueError, match="positive"):
            SMACrossover(fast=-1, slow=5)

    def test_windows_must_be_int(self):
        with pytest.raises(TypeError):
            SMACrossover(fast=3.5, slow=10)  # type: ignore[arg-type]

    def test_default_order_type_is_market(self):
        assert SMACrossover().preferred_order_type == OrderType.MARKET

    def test_missing_close_column_raises(self):
        idx = pd.date_range("2026-01-01", periods=3, freq="D", tz="UTC")
        bad = pd.DataFrame({"open": [1, 2, 3]}, index=idx)
        with pytest.raises(ValueError, match="close"):
            SMACrossover(2, 3).generate_signals(bad)


# ── SMACrossover signals on known paths ─────────────────────────────────────


class TestSMACrossoverSignals:
    def test_no_signals_on_flat_prices(self):
        df = _df([10.0] * 20)
        sig = SMACrossover(fast=3, slow=5).generate_signals(df)
        assert sig.entries.sum() == 0
        assert sig.exits.sum() == 0

    def test_no_signals_on_monotonic_uptrend(self):
        """
        On a pure monotonic uptrend, the fast SMA is above the slow SMA from
        the very first bar both are defined. We never observed a "before"
        state where fast was below, so no *crossover* has occurred — only a
        persistent regime. Emitting no entry is the correct, look-ahead-safe
        behavior. (If you really wanted to enter on "first confirmation,"
        that's a different strategy, not a crossover.)
        """
        df = _df(list(range(1, 30)))
        sig = SMACrossover(fast=3, slow=5).generate_signals(df)
        assert sig.entries.sum() == 0, "no confirmed cross ⇒ no entry"
        assert sig.exits.sum() == 0

    def test_entry_and_exit_on_v_shape(self):
        """
        V-shape: falling → rising → falling.

        Falling leg establishes fast < slow. When the rising leg begins, fast
        eventually crosses above slow ⇒ entry. When the final falling leg
        begins, fast crosses back below slow ⇒ exit. Both crosses have a
        valid `prev_diff` because the opposite regime existed first.
        """
        leg_down = list(range(30, 15, -1))       # 30..16  (15 bars, fast < slow)
        leg_up = list(range(17, 36))             # 17..35  (19 bars, creates cross-up)
        leg_down2 = list(range(34, 14, -1))      # 34..15  (20 bars, creates cross-down)
        df = _df(leg_down + leg_up + leg_down2)

        sig = SMACrossover(fast=3, slow=7).generate_signals(df)

        assert sig.entries.sum() >= 1, "cross-up during rising leg must fire entry"
        assert sig.exits.sum() >= 1, "cross-down during final leg must fire exit"

        entry_idx = sig.entries[sig.entries].index.min()
        exit_idx = sig.exits[sig.exits].index.min()
        assert entry_idx < exit_idx, "entry must precede exit"

    def test_signals_are_false_during_nan_prefix(self):
        df = _df(list(range(1, 15)))
        sig = SMACrossover(fast=3, slow=7).generate_signals(df)
        # slow=7 ⇒ first usable index is 6. Bars 0..5 must all be False.
        assert not sig.entries.iloc[:6].any()
        assert not sig.exits.iloc[:6].any()

    def test_entries_and_exits_never_simultaneous(self):
        leg_down = list(range(30, 15, -1))
        leg_up = list(range(17, 36))
        leg_down2 = list(range(34, 14, -1))
        df = _df(leg_down + leg_up + leg_down2)
        sig = SMACrossover(fast=3, slow=7).generate_signals(df)
        both = sig.entries & sig.exits
        assert not both.any(), "a single bar cannot be both entry and exit"


# ── Look-ahead guard ─────────────────────────────────────────────────────────


class TestLookAheadGuard:
    """
    The critical invariant: signals at bar t must depend only on data up to
    and including t. If we truncate the input to [0:t+1] and re-compute,
    the signals from 0..t must be byte-identical to what the full series
    produced for those same bars.
    """

    def test_truncating_input_preserves_past_signals(self):
        # Path with several crossovers.
        path = (
            list(range(10, 25))
            + list(range(23, 10, -1))
            + list(range(11, 22))
            + list(range(20, 10, -1))
        )
        df = _df(path)
        strat = SMACrossover(fast=3, slow=7)
        full = strat.generate_signals(df)

        # For every cut point after the warmup, truncated signals must match.
        for cut in range(10, len(df)):
            truncated = strat.generate_signals(df.iloc[: cut + 1])
            pd.testing.assert_series_equal(
                full.entries.iloc[: cut + 1],
                truncated.entries,
                check_names=False,
            )
            pd.testing.assert_series_equal(
                full.exits.iloc[: cut + 1],
                truncated.exits,
                check_names=False,
            )


# ── Purity ───────────────────────────────────────────────────────────────────


class TestPurity:
    def test_repeated_calls_are_deterministic(self):
        df = _df(list(range(1, 30)))
        strat = SMACrossover(fast=3, slow=7)
        a = strat.generate_signals(df)
        b = strat.generate_signals(df)
        pd.testing.assert_series_equal(a.entries, b.entries, check_names=False)
        pd.testing.assert_series_equal(a.exits, b.exits, check_names=False)

    def test_input_df_is_not_mutated(self):
        df = _df(list(range(1, 30)))
        original_cols = set(df.columns)
        SMACrossover(fast=3, slow=7).generate_signals(df)
        assert set(df.columns) == original_cols, "strategy must not add columns to input"
