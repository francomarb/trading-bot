"""
Unit tests for strategies/health/benchmarks.py.

Coverage:
  - buy_and_hold_return: normal positive/negative cases, edge cases
    (single bar, empty df, zero first price)
  - equal_weight_bh_return: averages across symbols, skips fetch
    failures, returns 0.0 on empty input or all-failure path
  - benchmark_symbols_for: maps each strategy to its production
    watchlist (settings) or the documented placeholder; unknown
    strategy returns empty list with a warning
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable

import pandas as pd
import pytest

from strategies.health import benchmarks as bm


# ── buy_and_hold_return ───────────────────────────────────────────────


class TestBuyAndHoldReturn:
    def test_positive_move(self):
        df = pd.DataFrame({"open": [100, 0, 0], "close": [0, 0, 110]})
        # First open = 100, last close = 110 → +10%
        assert bm.buy_and_hold_return(df) == pytest.approx(0.10)

    def test_negative_move(self):
        df = pd.DataFrame({"open": [100, 0, 0], "close": [0, 0, 90]})
        assert bm.buy_and_hold_return(df) == pytest.approx(-0.10)

    def test_empty_df_returns_zero(self):
        df = pd.DataFrame({"open": [], "close": []})
        assert bm.buy_and_hold_return(df) == 0.0

    def test_single_bar_returns_zero(self):
        df = pd.DataFrame({"open": [100], "close": [110]})
        assert bm.buy_and_hold_return(df) == 0.0

    def test_zero_first_price_returns_zero(self):
        df = pd.DataFrame({"open": [0, 0], "close": [50, 100]})
        assert bm.buy_and_hold_return(df) == 0.0

    def test_none_df_returns_zero(self):
        assert bm.buy_and_hold_return(None) == 0.0  # type: ignore[arg-type]


# ── equal_weight_bh_return ────────────────────────────────────────────


class TestEqualWeightBHReturn:
    def test_empty_symbol_list_returns_zero(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, tzinfo=timezone.utc)
        assert bm.equal_weight_bh_return([], start, end) == 0.0

    def test_blank_symbols_filtered_out(self):
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, tzinfo=timezone.utc)
        assert bm.equal_weight_bh_return(["", None, ""], start, end) == 0.0  # type: ignore[list-item]

    def test_averages_across_symbols(self, monkeypatch):
        """Mock fetch_symbol so each symbol returns a known BH return.
        Average of +10% and -5% should be +2.5%."""
        call_count = {"n": 0}

        def fake_fetch(sym, start, end, timeframe):
            call_count["n"] += 1
            if sym == "AAA":
                df = pd.DataFrame({"open": [100, 0], "close": [0, 110]})  # +10%
            else:
                df = pd.DataFrame({"open": [100, 0], "close": [0, 95]})   # -5%
            return df, None

        monkeypatch.setattr(bm, "fetch_symbol", fake_fetch)
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, tzinfo=timezone.utc)
        result = bm.equal_weight_bh_return(["AAA", "BBB"], start, end)
        assert result == pytest.approx(0.025)
        assert call_count["n"] == 2

    def test_skips_symbols_with_fetch_failure(self, monkeypatch):
        """A single failed fetch shouldn't poison the benchmark."""

        def fake_fetch(sym, start, end, timeframe):
            if sym == "BAD":
                raise RuntimeError("simulated fetch error")
            df = pd.DataFrame({"open": [100, 0], "close": [0, 120]})  # +20%
            return df, None

        monkeypatch.setattr(bm, "fetch_symbol", fake_fetch)
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, tzinfo=timezone.utc)
        # Only GOOD contributes; BAD is skipped.
        result = bm.equal_weight_bh_return(["GOOD", "BAD"], start, end)
        assert result == pytest.approx(0.20)

    def test_skips_empty_dfs(self, monkeypatch):
        def fake_fetch(sym, start, end, timeframe):
            if sym == "EMPTY":
                return pd.DataFrame({"open": [], "close": []}), None
            df = pd.DataFrame({"open": [100, 0], "close": [0, 105]})  # +5%
            return df, None

        monkeypatch.setattr(bm, "fetch_symbol", fake_fetch)
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, tzinfo=timezone.utc)
        result = bm.equal_weight_bh_return(["GOOD", "EMPTY"], start, end)
        assert result == pytest.approx(0.05)

    def test_all_failures_returns_zero(self, monkeypatch):
        """When no symbols return usable data, return 0.0 (not raise).
        EdgeAssessor interprets 0.0 benchmark as 'no comparison
        available' rather than 'strategy beat the benchmark'."""

        def fake_fetch(sym, start, end, timeframe):
            raise RuntimeError("all fetches fail")

        monkeypatch.setattr(bm, "fetch_symbol", fake_fetch)
        start = datetime(2025, 1, 1, tzinfo=timezone.utc)
        end = datetime(2025, 6, 1, tzinfo=timezone.utc)
        assert bm.equal_weight_bh_return(["A", "B", "C"], start, end) == 0.0


# ── benchmark_symbols_for ─────────────────────────────────────────────


class TestBenchmarkSymbolsFor:
    def test_sma_resolves_to_sma_watchlist(self):
        symbols = bm.benchmark_symbols_for("sma_crossover")
        # Should be the production SMA watchlist verbatim (from settings).
        from config import settings

        assert symbols == list(settings.STRATEGY_WATCHLISTS["sma_crossover"])

    def test_rsi_resolves_to_rsi_watchlist(self):
        symbols = bm.benchmark_symbols_for("rsi_reversion")
        from config import settings

        assert symbols == list(settings.STRATEGY_WATCHLISTS["rsi_reversion"])

    def test_donchian_resolves_to_its_watchlist(self):
        symbols = bm.benchmark_symbols_for("donchian_breakout")
        from config import settings

        assert symbols == list(settings.STRATEGY_WATCHLISTS["donchian_breakout"])

    def test_spy_options_resolves_to_spy(self):
        # Underlying-BH placeholder per design §5.3.
        symbols = bm.benchmark_symbols_for("spy_options_reversion")
        assert symbols == ["SPY"]

    def test_credit_spread_resolves_to_underlying_instruments(self):
        from config import settings

        symbols = bm.benchmark_symbols_for("credit_spread")
        # The strategy's CREDIT_SPREAD_INSTRUMENTS keys are the v1
        # benchmark underlying tickers.
        if getattr(settings, "CREDIT_SPREAD_INSTRUMENTS", None):
            assert symbols == list(settings.CREDIT_SPREAD_INSTRUMENTS.keys())
        else:
            assert symbols == ["SPY", "QQQ"]

    def test_unknown_strategy_returns_empty(self):
        symbols = bm.benchmark_symbols_for("nonexistent_strategy_xyz")
        assert symbols == []
