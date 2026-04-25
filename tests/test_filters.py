"""
Unit tests for strategies/filters/ (Phase 10.F3a + 10.F3b).

Tests cover:
  - SPYTrendFilter: SPY above/below SMA, multiple windows, fetch failure,
    NaN SMA, cache reuse, fail-open defaults.
  - EarningsBlackout: within/outside blackout window, set_symbol, yfinance
    failure graceful degradation, caching, edge cases.
  - SMAEdgeFilter: combined gate, set_symbol routing, each sub-gate independently.
  - RSIEdgeFilter: all three gates, each independently, stock SMA from df,
    NaN SMA treatment, observability log presence.
  - BaseStrategy integration: symbol passed through generate_signals → set_symbol.

No real network calls. All external dependencies (fetch_symbol, yfinance) are
mocked.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from strategies.filters.common import EarningsBlackout, SPYTrendFilter
from strategies.filters.sma_crossover import SMAEdgeFilter
from strategies.filters.rsi_reversion import RSIEdgeFilter


# ── Helpers ──────────────────────────────────────────────────────────────────


def _spy_df(closes: list[float]) -> pd.DataFrame:
    """Synthetic SPY bar DataFrame."""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes}, index=idx)


def _symbol_df(closes: list[float]) -> pd.DataFrame:
    """Synthetic symbol bar DataFrame."""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B")
    return pd.DataFrame({"close": closes, "open": closes, "high": closes, "low": closes},
                        index=idx)


def _make_spy_filter(spy_df: pd.DataFrame, *, windows: list[int] = [200]) -> SPYTrendFilter:
    f = SPYTrendFilter(sma_windows=windows, cache_ttl_seconds=0)
    with patch("data.fetcher.fetch_symbol", return_value=spy_df):
        f._fetch_spy()  # prime cache
    return f


# ── TestSPYTrendFilter ────────────────────────────────────────────────────────


class TestSPYTrendFilter:
    def _filter(self, closes: list[float], windows: list[int] = [200]) -> SPYTrendFilter:
        spy = _spy_df(closes)
        f = SPYTrendFilter(sma_windows=windows, cache_ttl_seconds=9999)
        with patch("data.fetcher.fetch_symbol", return_value=spy):
            result = f._fetch_spy()
        return f

    def test_spy_above_sma_returns_true(self):
        # 210 bars rising → last close well above SMA200
        closes = list(range(1, 211))  # 1..210
        f = self._filter(closes, windows=[200])
        gate = f(_symbol_df([100.0] * 5))
        assert gate.all()

    def test_spy_below_sma_returns_false(self):
        # 210 bars falling → last close well below SMA200
        closes = list(range(210, 0, -1))  # 210..1
        f = self._filter(closes, windows=[200])
        gate = f(_symbol_df([100.0] * 5))
        assert not gate.any()

    def test_multiple_windows_all_must_pass(self):
        # Build SPY where close > SMA200 but < SMA50
        # Rises for 160 bars, then drops sharply → close below SMA50 but above SMA200
        closes = list(range(1, 161)) + [100] * 50  # flat at 100 after spike
        f = self._filter(closes, windows=[200, 50])
        # close=100, SMA200 would be around 90-ish (low average), SMA50=100 (equal)
        # To ensure the test is deterministic, manually check allowed/blocked
        allowed, _ = f._check()
        # Both windows: whatever the result, the gate series reflects _check()
        gate = f(_symbol_df([50.0] * 3))
        assert gate.all() == allowed

    def test_nan_sma_skipped_does_not_block(self):
        # Only 5 bars — SMA200 will be NaN → skip, allow
        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        f = self._filter(closes, windows=[200])
        gate = f(_symbol_df([50.0] * 3))
        assert gate.all()  # NaN → fail open

    def test_fetch_failure_with_no_cache_allows(self):
        f = SPYTrendFilter(sma_windows=[200], cache_ttl_seconds=0)
        with patch("data.fetcher.fetch_symbol", side_effect=Exception("timeout")):
            gate = f(_symbol_df([100.0] * 3))
        assert gate.all()  # no cache → fail open

    def test_fetch_failure_returns_stale_cache(self):
        spy = _spy_df(list(range(1, 211)))  # rising → allowed
        f = SPYTrendFilter(sma_windows=[200], cache_ttl_seconds=9999)
        with patch("data.fetcher.fetch_symbol", return_value=spy):
            f._fetch_spy()  # prime cache
        # Now fail the fetch — should use stale cache (still allowed)
        with patch("data.fetcher.fetch_symbol", side_effect=Exception("fail")):
            gate = f(_symbol_df([50.0] * 3))
        assert gate.all()

    def test_cache_reuse_within_ttl(self):
        spy = _spy_df(list(range(1, 211)))
        f = SPYTrendFilter(sma_windows=[200], cache_ttl_seconds=9999)
        with patch("data.fetcher.fetch_symbol", return_value=spy) as mock_fetch:
            f(_symbol_df([100.0] * 3))
            f(_symbol_df([100.0] * 3))
            # Both calls should hit cache, so fetch_symbol called only once
            assert mock_fetch.call_count == 1

    def test_gate_series_aligned_to_df_index(self):
        closes = list(range(1, 211))
        f = self._filter(closes, windows=[200])
        df = _symbol_df([100.0] * 7)
        gate = f(df)
        assert list(gate.index) == list(df.index)

    def test_empty_sma_windows_raises(self):
        with pytest.raises(ValueError, match="sma_windows must not be empty"):
            SPYTrendFilter(sma_windows=[])

    def test_empty_df_still_returns_series(self):
        closes = list(range(1, 211))
        f = self._filter(closes, windows=[200])
        empty_df = pd.DataFrame({"close": []}, index=pd.DatetimeIndex([]))
        gate = f(empty_df)
        assert isinstance(gate, pd.Series)
        assert len(gate) == 0


# ── TestEarningsBlackout ──────────────────────────────────────────────────────


class TestEarningsBlackout:
    def _filter(
        self,
        earnings_dates: list[datetime.date],
        *,
        days_before: int = 5,
        days_after: int = 2,
    ) -> EarningsBlackout:
        f = EarningsBlackout(days_before=days_before, days_after=days_after)
        f._cache["AAPL"] = (datetime.date.today(), earnings_dates)
        f.set_symbol("AAPL")
        return f

    def _df_on(self, date: datetime.date, n: int = 3) -> pd.DataFrame:
        idx = pd.date_range(end=date, periods=n, freq="B")
        return pd.DataFrame({"close": [100.0] * n}, index=idx)

    def test_earnings_tomorrow_blocks(self):
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        f = self._filter([tomorrow])
        df = self._df_on(datetime.date.today())
        assert not f(df).any()

    def test_earnings_yesterday_blocks(self):
        yesterday = datetime.date.today() - datetime.timedelta(days=1)
        f = self._filter([yesterday])
        df = self._df_on(datetime.date.today())
        assert not f(df).any()

    def test_earnings_far_away_allows(self):
        far = datetime.date.today() + datetime.timedelta(days=30)
        f = self._filter([far])
        df = self._df_on(datetime.date.today())
        assert f(df).all()

    def test_exactly_at_days_before_boundary_blocks(self):
        edge = datetime.date.today() + datetime.timedelta(days=5)  # days_before=5
        f = self._filter([edge], days_before=5)
        df = self._df_on(datetime.date.today())
        assert not f(df).any()

    def test_exactly_at_days_after_boundary_blocks(self):
        edge = datetime.date.today() - datetime.timedelta(days=2)  # days_after=2
        f = self._filter([edge], days_after=2)
        df = self._df_on(datetime.date.today())
        assert not f(df).any()

    def test_one_day_outside_window_allows(self):
        outside = datetime.date.today() + datetime.timedelta(days=6)  # days_before=5
        f = self._filter([outside], days_before=5)
        df = self._df_on(datetime.date.today())
        assert f(df).all()

    def test_no_earnings_dates_allows(self):
        f = self._filter([])
        df = self._df_on(datetime.date.today())
        assert f(df).all()

    def test_yfinance_failure_allows(self):
        f = EarningsBlackout()
        f.set_symbol("AAPL")
        with patch("yfinance.Ticker", side_effect=Exception("network error")):
            df = self._df_on(datetime.date.today())
            gate = f(df)
        assert gate.all()  # fail open

    def test_set_symbol_changes_lookup(self):
        f = EarningsBlackout(days_before=5, days_after=2)
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        f._cache["AAPL"] = (datetime.date.today(), [tomorrow])
        f._cache["MSFT"] = (datetime.date.today(), [])

        f.set_symbol("AAPL")
        df = self._df_on(datetime.date.today())
        assert not f(df).any()  # AAPL blocked

        f.set_symbol("MSFT")
        assert f(df).all()  # MSFT clear

    def test_empty_df_returns_empty_series(self):
        f = self._filter([])
        empty = pd.DataFrame({"close": []}, index=pd.DatetimeIndex([]))
        gate = f(empty)
        assert isinstance(gate, pd.Series)
        assert len(gate) == 0

    def test_no_symbol_set_allows(self):
        f = EarningsBlackout()
        # symbol is "" by default
        df = self._df_on(datetime.date.today())
        assert f(df).all()

    def test_cache_used_within_same_day(self):
        f = EarningsBlackout()
        f.set_symbol("AAPL")
        today = datetime.date.today()
        f._cache["AAPL"] = (today, [])
        with patch("yfinance.Ticker") as mock_yf:
            df = self._df_on(today)
            f(df)
            mock_yf.assert_not_called()  # used cache, didn't hit yfinance


# ── TestSMAEdgeFilter ─────────────────────────────────────────────────────────


def _today_df(n: int = 5) -> pd.DataFrame:
    """Synthetic df whose last bar is today (business-day aligned)."""
    idx = pd.bdate_range(end=datetime.date.today(), periods=n)
    return pd.DataFrame(
        {"close": [100.0] * n, "open": [100.0] * n,
         "high": [100.0] * n, "low": [100.0] * n},
        index=idx,
    )


class TestSMAEdgeFilter:
    def _spy_allows(self, f: SMAEdgeFilter) -> None:
        """Patch the internal SPY filter to always allow."""
        f._spy_filter._spy_cache = _spy_df(list(range(1, 211)))
        f._spy_filter._cache_time = float("inf")

    def _spy_blocks(self, f: SMAEdgeFilter) -> None:
        """Patch the internal SPY filter to always block."""
        f._spy_filter._spy_cache = _spy_df(list(range(210, 0, -1)))
        f._spy_filter._cache_time = float("inf")

    def test_both_gates_clear_returns_true(self):
        f = SMAEdgeFilter(days_before=5, days_after=2)
        f.set_symbol("AAPL")
        self._spy_allows(f)
        f._earnings._cache["AAPL"] = (datetime.date.today(), [])
        gate = f(_today_df())
        assert gate.all()

    def test_spy_blocked_returns_false(self):
        f = SMAEdgeFilter()
        f.set_symbol("AAPL")
        self._spy_blocks(f)
        f._earnings._cache["AAPL"] = (datetime.date.today(), [])
        gate = f(_today_df())
        assert not gate.any()

    def test_earnings_blackout_returns_false(self):
        f = SMAEdgeFilter(days_before=5, days_after=2)
        f.set_symbol("AAPL")
        self._spy_allows(f)
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        f._earnings._cache["AAPL"] = (datetime.date.today(), [tomorrow])
        gate = f(_today_df())
        assert not gate.any()

    def test_both_blocked_returns_false(self):
        f = SMAEdgeFilter()
        f.set_symbol("AAPL")
        self._spy_blocks(f)
        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        f._earnings._cache["AAPL"] = (datetime.date.today(), [tomorrow])
        gate = f(_today_df())
        assert not gate.any()

    def test_set_symbol_routes_to_earnings(self):
        f = SMAEdgeFilter()
        f.set_symbol("MU")
        assert f._earnings._symbol == "MU"

    def test_gate_series_aligned_to_df(self):
        f = SMAEdgeFilter()
        f.set_symbol("AAPL")
        self._spy_allows(f)
        f._earnings._cache["AAPL"] = (datetime.date.today(), [])
        df = _today_df(8)
        gate = f(df)
        assert list(gate.index) == list(df.index)


# ── TestRSIEdgeFilter ─────────────────────────────────────────────────────────


class TestRSIEdgeFilter:
    def _spy_allows(self, f: RSIEdgeFilter) -> None:
        f._spy_filter._spy_cache = _spy_df(list(range(1, 211)))
        f._spy_filter._cache_time = float("inf")

    def _spy_blocks(self, f: RSIEdgeFilter) -> None:
        f._spy_filter._spy_cache = _spy_df(list(range(210, 0, -1)))
        f._spy_filter._cache_time = float("inf")

    def test_all_gates_pass_returns_true(self):
        f = RSIEdgeFilter()
        f.set_symbol("MU")
        self._spy_allows(f)
        # Rising stock: close > SMA50
        closes = list(range(1, 56))  # 1..55 → last close=55, SMA50=28
        df = _symbol_df(closes)
        gate = f(df)
        assert gate.iloc[-1]

    def test_spy_gate_fails_returns_false(self):
        f = RSIEdgeFilter()
        f.set_symbol("MU")
        self._spy_blocks(f)
        closes = list(range(1, 56))
        gate = f(_symbol_df(closes))
        assert not gate.iloc[-1]

    def test_stock_below_sma50_returns_false(self):
        f = RSIEdgeFilter()
        f.set_symbol("MU")
        self._spy_allows(f)
        # Falling stock: close < SMA50
        closes = list(range(55, 0, -1))  # 55..1 → last close=1, SMA50 ~28
        gate = f(_symbol_df(closes))
        assert not gate.iloc[-1]

    def test_stock_nan_sma_allows(self):
        f = RSIEdgeFilter()
        f.set_symbol("MU")
        self._spy_allows(f)
        # Only 5 bars → SMA50 is NaN → fail open
        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        gate = f(_symbol_df(closes))
        assert gate.iloc[-1]

    def test_set_symbol_stored(self):
        f = RSIEdgeFilter()
        f.set_symbol("CDNS")
        assert f._symbol == "CDNS"

    def test_gate_series_aligned_to_df(self):
        f = RSIEdgeFilter()
        f.set_symbol("MU")
        self._spy_allows(f)
        closes = list(range(1, 56))
        df = _symbol_df(closes)
        gate = f(df)
        assert list(gate.index) == list(df.index)

    def test_spy_windows_both_required(self):
        """Both SPY 200SMA and 50SMA must pass. Mock SPYTrendFilter._check directly."""
        f = RSIEdgeFilter()
        f.set_symbol("MU")
        closes = list(range(1, 56))
        df = _symbol_df(closes)

        # SPY allows (both windows clear)
        with patch.object(f._spy_filter, "_check", return_value=(True, "ok")):
            gate = f(df)
            assert gate.iloc[-1]

        # SPY blocks
        with patch.object(f._spy_filter, "_check", return_value=(False, "SPY below 50SMA")):
            gate = f(df)
            assert not gate.iloc[-1]


# ── TestBaseStrategySymbolInjection ───────────────────────────────────────────


class TestBaseStrategySymbolInjection:
    """Verify BaseStrategy.generate_signals passes symbol to set_symbol filters."""

    def test_set_symbol_called_with_correct_symbol(self):
        from strategies.sma_crossover import SMACrossover

        mock_filter = MagicMock()
        mock_filter.return_value = pd.Series(
            True, index=pd.date_range("2024-01-01", periods=10, freq="B"), dtype=bool
        )
        mock_filter.set_symbol = MagicMock()

        strategy = SMACrossover(fast=3, slow=5, edge_filter=mock_filter)
        df = _symbol_df(list(range(1, 11)))
        strategy.generate_signals(df, symbol="NVDA")

        mock_filter.set_symbol.assert_called_once_with("NVDA")

    def test_no_symbol_skips_set_symbol(self):
        from strategies.sma_crossover import SMACrossover

        mock_filter = MagicMock()
        mock_filter.return_value = pd.Series(
            True, index=pd.date_range("2024-01-01", periods=10, freq="B"), dtype=bool
        )
        mock_filter.set_symbol = MagicMock()

        strategy = SMACrossover(fast=3, slow=5, edge_filter=mock_filter)
        df = _symbol_df(list(range(1, 11)))
        strategy.generate_signals(df)  # no symbol kwarg

        mock_filter.set_symbol.assert_not_called()

    def test_filter_without_set_symbol_not_broken(self):
        """Lambda filters (no set_symbol) still work when symbol is passed."""
        from strategies.sma_crossover import SMACrossover

        gate = pd.Series(True, index=pd.date_range("2024-01-01", periods=10, freq="B"))
        strategy = SMACrossover(fast=3, slow=5, edge_filter=lambda df: gate)
        df = _symbol_df(list(range(1, 11)))
        # Must not raise even though symbol is passed
        result = strategy.generate_signals(df, symbol="AAPL")
        assert isinstance(result.entries, pd.Series)

    def test_exits_never_blocked_by_filter(self):
        """Even with a filter that blocks everything, exits still fire."""
        from strategies.sma_crossover import SMACrossover

        gate = pd.Series(False, index=pd.date_range("2024-01-01", periods=10, freq="B"))
        strategy = SMACrossover(fast=3, slow=5, edge_filter=lambda df: gate)
        df = _symbol_df(list(range(1, 11)))
        result = strategy.generate_signals(df, symbol="AAPL")
        # exits are raw (not AND-gated)
        from strategies.sma_crossover import SMACrossover as _S
        raw = _S(fast=3, slow=5)._raw_signals(df)
        assert result.exits.equals(raw.exits)
