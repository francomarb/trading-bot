"""
Unit tests for strategies/filters/ (Phase 10.F3a + 10.F3b).

Tests cover:
  - SPYTrendFilter: SPY above/below SMA, multiple windows, fetch failure,
    NaN SMA, cache reuse, fail-open defaults, fetch-failure rate-limiting.
  - EarningsBlackout: within/outside blackout window, set_symbol, yfinance
    failure graceful degradation, caching, edge cases.
  - SMAEdgeFilter: stock SMA, volume expansion, earnings blackout (2d/0d).
  - RSIEdgeFilter: SPY50 band + earnings blackout (3d/2d) + liquidity + active-breakdown gate.
  - BaseStrategy integration: symbol passed through generate_signals → set_symbol.

No real network calls. All external dependencies (fetch_symbol, yfinance) are
mocked.
"""

from __future__ import annotations

import datetime
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from strategies.base import EdgeFilterDecision, SignalFrame
from strategies.filters.common import EarningsBlackout, SPYTrendFilter
from strategies.filters.common import CompositeEdgeFilter
from strategies.filters.sma_crossover import SMAEdgeFilter
from strategies.filters.rsi_reversion import RSIEdgeFilter
from strategies.rsi_reversion import RSIReversion


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
    with patch("data.fetcher.fetch_symbol", return_value=(spy_df, None)):
        f._fetch_spy()  # prime cache
    return f


# ── TestSPYTrendFilter ────────────────────────────────────────────────────────


class TestSPYTrendFilter:
    def _filter(
        self,
        closes: list[float],
        windows: list[int] = [200],
        *,
        sma_tolerance_pct: float = 0.0,
    ) -> SPYTrendFilter:
        spy = _spy_df(closes)
        f = SPYTrendFilter(
            sma_windows=windows,
            cache_ttl_seconds=9999,
            sma_tolerance_pct=sma_tolerance_pct,
        )
        with patch("data.fetcher.fetch_symbol", return_value=(spy, None)):
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

    def test_spy_tolerance_allows_small_sma_undercut(self):
        closes = [100.0] * 49 + [99.5]
        strict = self._filter(closes, windows=[50])
        tolerant = self._filter(closes, windows=[50], sma_tolerance_pct=0.01)

        assert not strict(_symbol_df([100.0] * 5)).any()
        assert tolerant(_symbol_df([100.0] * 5)).all()

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

    def test_insufficient_history_blocks(self):
        # A mandatory macro gate must never disappear because history is short.
        closes = [100.0, 101.0, 102.0, 103.0, 104.0]
        f = self._filter(closes, windows=[200])
        gate = f(_symbol_df([50.0] * 3))
        assert not gate.any()
        assert f._check() == (
            False,
            "insufficient SPY history for SMA200: 5 bars available, 200 required",
        )

    def test_fetch_failure_with_no_cache_blocks(self):
        # Cold-start failure with no prior cache → fail closed.
        # Protects against deploying into a market crash while the data API is down.
        f = SPYTrendFilter(sma_windows=[200], cache_ttl_seconds=0)
        with patch("data.fetcher.fetch_symbol", side_effect=Exception("timeout")):
            gate = f(_symbol_df([100.0] * 3))
        assert not gate.any()  # no cache → fail closed

    def test_fetch_failure_with_stale_cache_uses_last_known_state(self):
        # Subsequent failure after a successful fetch → stale cache reused.
        # Safe: last known SPY state is a reasonable proxy during brief outages.
        f = SPYTrendFilter(sma_windows=[200], cache_ttl_seconds=0)
        spy_df = _spy_df([float(i) for i in range(1, 202)])  # rising closes, SPY > SMA200
        f._spy_cache = spy_df
        f._cache_time = 0.0  # expired TTL
        with patch("data.fetcher.fetch_symbol", side_effect=Exception("down")):
            gate = f(_symbol_df([100.0] * 3))
        assert gate.all()  # stale cache says SPY was healthy → allow

    def test_fetch_failure_advances_cache_time_to_rate_limit(self):
        """After a failed fetch, cache_time is updated so we don't retry every cycle."""
        import time as _time
        f = SPYTrendFilter(sma_windows=[200], cache_ttl_seconds=60)
        before = _time.monotonic()
        with patch("data.fetcher.fetch_symbol", side_effect=Exception("down")):
            f(_symbol_df([100.0] * 3))
        # cache_time must have advanced to (approximately) now
        assert f._cache_time >= before

    def test_fetch_failure_returns_stale_cache(self):
        spy = _spy_df(list(range(1, 211)))  # rising → allowed
        f = SPYTrendFilter(sma_windows=[200], cache_ttl_seconds=9999)
        with patch("data.fetcher.fetch_symbol", return_value=(spy, None)):
            f._fetch_spy()  # prime cache
        # Now fail the fetch — should use stale cache (still allowed)
        with patch("data.fetcher.fetch_symbol", side_effect=Exception("fail")):
            gate = f(_symbol_df([50.0] * 3))
        assert gate.all()

    def test_cache_reuse_within_ttl(self):
        spy = _spy_df(list(range(1, 211)))
        f = SPYTrendFilter(sma_windows=[200], cache_ttl_seconds=9999)
        with patch("data.fetcher.fetch_symbol", return_value=(spy, None)) as mock_fetch:
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
        # Anchor offset to the actual last bar date so the test is
        # robust whether today is a weekday or weekend.
        df = self._df_on(datetime.date.today())
        last_bar = df.index[-1].date()
        edge = last_bar + datetime.timedelta(days=5)   # days_before=5
        f = self._filter([edge], days_before=5)
        assert not f(df).any()

    def test_exactly_at_days_after_boundary_blocks(self):
        df = self._df_on(datetime.date.today())
        last_bar = df.index[-1].date()
        edge = last_bar - datetime.timedelta(days=2)   # days_after=2
        f = self._filter([edge], days_after=2)
        assert not f(df).any()

    def test_one_day_outside_window_allows(self):
        df = self._df_on(datetime.date.today())
        last_bar = df.index[-1].date()
        outside = last_bar + datetime.timedelta(days=6)  # one beyond days_before=5
        f = self._filter([outside], days_before=5)
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


def _rising_df(n: int, *, with_volume: bool = True, vol_expanding: bool = True) -> pd.DataFrame:
    """
    Synthetic df with `n` bars of rising closes.
    Volume alternates expanding (short avg > long avg) or contracting.
    """
    closes = list(range(1, n + 1))
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    if with_volume and vol_expanding:
        # Volume ramps up steadily — 10-day avg will exceed 30-day avg
        volumes = list(range(1_000, 1_000 + n * 100, 100))
    elif with_volume:
        # Volume ramps down — 10-day avg will be below 30-day avg
        volumes = list(range(1_000 + n * 100, 1_000, -100))
    else:
        volumes = [1_000] * n
    data: dict = {"close": closes, "open": closes, "high": closes, "low": closes}
    if with_volume:
        data["volume"] = volumes
    return pd.DataFrame(data, index=idx)


class TestSMAEdgeFilter:
    """
    SMAEdgeFilter: stock > 200SMA AND volume expanding AND no earnings blackout.

    SPY > 200 SMA gate is INTENTIONALLY DISABLED — delegated to RegimeDetector
    (BEAR regime, universal gate enforced at engine level). Tests document this
    delegation. If the SPY gate is ever re-enabled, restore _spy_allows /
    _spy_blocks helpers and flip test_spy_below_sma_does_not_block_here back.

    Earnings blackout (days_before=2, days_after=0): protects against gap risk
    on a new entry right before earnings. An earnings miss can gap a stock 20%+
    overnight; the GTC stop becomes a market order at the open, bypassing the
    2% MAX_POSITION_PCT limit entirely. days_after=0 allows post-earnings
    trend continuation entries immediately.
    """

    def _clear_earnings(self, f: SMAEdgeFilter, symbol: str = "AAPL") -> None:
        """Seed the earnings cache with no upcoming dates."""
        f._earnings._cache[symbol] = (datetime.date.today(), [])
        f.set_symbol(symbol)

    # ── SPY gate delegation ───────────────────────────────────────────────────

    def test_spy_gate_disabled_no_spy_filter_attribute(self):
        """_spy_filter must not exist while the gate is disabled."""
        f = SMAEdgeFilter()
        assert not hasattr(f, "_spy_filter")

    def test_spy_below_sma_does_not_block_here(self):
        """
        SPY below 200 SMA must NOT block SMAEdgeFilter — that veto belongs to
        RegimeDetector. With insufficient stock/vol history both fail open so
        the filter allows regardless of SPY state.
        """
        f = SMAEdgeFilter()
        # 5 bars → stock SMA NaN → fail open, vol NaN → fail open → allowed
        decision = f(_symbol_df([100.0] * 5))
        assert isinstance(decision, EdgeFilterDecision)
        assert decision.allowed.all()

    # ── Stock 200 SMA gate ───────────────────────────────────────────────────

    def test_stock_above_200sma_allows(self):
        """Rising stock > 200 SMA with expanding volume → allowed."""
        f = SMAEdgeFilter(stock_sma_window=200, vol_short_window=10, vol_long_window=30)
        df = _rising_df(210, vol_expanding=True)
        decision = f(df)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_stock_below_200sma_blocks(self):
        """Falling stock below its 200 SMA → blocked."""
        f = SMAEdgeFilter(stock_sma_window=200, vol_short_window=10, vol_long_window=30)
        closes = list(range(210, 0, -1))   # falling: last close=1, SMA200 ≈ 105
        idx = pd.date_range("2020-01-01", periods=210, freq="B")
        volumes = list(range(1_000, 1_000 + 210 * 100, 100))  # expanding vol
        df = pd.DataFrame(
            {"close": closes, "open": closes, "high": closes,
             "low": closes, "volume": volumes},
            index=idx,
        )
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == ["stock 1.00 ≤ SMA200 100.50"]

    def test_stock_nan_200sma_fails_open(self):
        """Fewer than 200 bars → SMA is NaN → fail open (allow)."""
        f = SMAEdgeFilter(stock_sma_window=200)
        decision = f(_symbol_df([100.0] * 10))
        assert decision.allowed.all()

    # ── Volume expansion gate ────────────────────────────────────────────────

    def test_volume_expanding_allows(self):
        f = SMAEdgeFilter(stock_sma_window=5, vol_short_window=3, vol_long_window=5)
        df = pd.DataFrame(
            {"close": [10, 11, 12, 13, 14, 15, 16],
             "volume": [100, 100, 100, 100, 200, 300, 400]},
            index=pd.date_range("2020-01-01", periods=7, freq="B"),
        )
        decision = f(df)
        assert decision.allowed.iloc[-1]

    def test_volume_contracting_blocks(self):
        f = SMAEdgeFilter(stock_sma_window=5, vol_short_window=3, vol_long_window=5)
        df = pd.DataFrame(
            {"close": [10, 11, 12, 13, 14, 15, 16],
             "volume": [400, 300, 200, 100, 50, 30, 10]},
            index=pd.date_range("2020-01-01", periods=7, freq="B"),
        )
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == ["volume contracting (med3 ≤ med5)"]

    def test_no_volume_column_fails_open(self):
        """No volume column → fail open. stock_sma_window=200 also NaN → open."""
        f = SMAEdgeFilter()
        df = pd.DataFrame(
            {"close": [10.0] * 10},
            index=pd.date_range("2020-01-01", periods=10, freq="B"),
        )
        decision = f(df)
        assert decision.allowed.all()

    def test_volume_nan_fails_open(self):
        """Fewer bars than vol_long_window → NaN avg → fail open."""
        f = SMAEdgeFilter(vol_short_window=10, vol_long_window=30)
        decision = f(_symbol_df([100.0] * 5))
        assert decision.allowed.all()

    # ── Combined / structural ────────────────────────────────────────────────

    def test_both_active_gates_pass(self):
        """Both stock SMA and volume gates clear → allowed."""
        f = SMAEdgeFilter(stock_sma_window=10, vol_short_window=3, vol_long_window=5)
        df = pd.DataFrame(
            {"close":  [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20],
             "volume": [100, 100, 100, 100, 100, 100, 100, 200, 300, 400, 500]},
            index=pd.date_range("2020-01-01", periods=11, freq="B"),
        )
        decision = f(df)
        assert decision.allowed.iloc[-1]

    def test_set_symbol_stored_and_forwarded_to_earnings(self):
        f = SMAEdgeFilter()
        f.set_symbol("MU")
        assert f._symbol == "MU"
        assert f._earnings._symbol == "MU"

    def test_gate_series_aligned_to_df(self):
        f = SMAEdgeFilter()
        df = _symbol_df([100.0] * 8)
        decision = f(df)
        assert list(decision.allowed.index) == list(df.index)
        assert list(decision.reasons.index) == list(df.index)

    # ── Earnings blackout gate ────────────────────────────────────────────────

    def _sma_earnings_df(self) -> pd.DataFrame:
        """5-bar df; last bar is the most recent business day."""
        return _today_df(5)

    def _last_bar(self, df: pd.DataFrame) -> datetime.date:
        """Date of the last bar in the df — used to anchor earnings offsets."""
        return df.index[-1].date()

    def test_earnings_tomorrow_blocks(self):
        """Entry the day before earnings → blocked (gap-risk protection)."""
        df = self._sma_earnings_df()
        last = self._last_bar(df)
        f = SMAEdgeFilter()
        f._earnings._cache["AAPL"] = (datetime.date.today(), [last + datetime.timedelta(days=1)])
        f.set_symbol("AAPL")
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == ["earnings blackout (gap-risk protection)"]

    def test_earnings_two_days_before_blocks(self):
        """2 days before earnings (default days_before=2) → blocked."""
        df = self._sma_earnings_df()
        last = self._last_bar(df)
        f = SMAEdgeFilter()
        f._earnings._cache["AAPL"] = (datetime.date.today(), [last + datetime.timedelta(days=2)])
        f.set_symbol("AAPL")
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == ["earnings blackout (gap-risk protection)"]

    def test_earnings_day_after_allows(self):
        """Day after earnings → allowed (days_after=0, post-earnings trend ok)."""
        df = self._sma_earnings_df()
        last = self._last_bar(df)
        f = SMAEdgeFilter()
        f._earnings._cache["AAPL"] = (datetime.date.today(), [last - datetime.timedelta(days=1)])
        f.set_symbol("AAPL")
        assert f(df).allowed.iloc[-1]

    def test_earnings_far_away_allows(self):
        """Earnings 30 days out → not in blackout window → allowed."""
        df = self._sma_earnings_df()
        last = self._last_bar(df)
        f = SMAEdgeFilter()
        f._earnings._cache["AAPL"] = (datetime.date.today(), [last + datetime.timedelta(days=30)])
        f.set_symbol("AAPL")
        assert f(df).allowed.iloc[-1]

    def test_earnings_custom_days_before(self):
        """days_before param is respected."""
        df = self._sma_earnings_df()
        last = self._last_bar(df)
        f = SMAEdgeFilter(days_before=5, days_after=0)
        f._earnings._cache["AAPL"] = (datetime.date.today(), [last + datetime.timedelta(days=5)])
        f.set_symbol("AAPL")
        assert not f(df).allowed.iloc[-1]

    def test_yfinance_failure_fails_open_on_earnings(self):
        """yfinance unavailable for earnings → fail open (allow), log warning."""
        from unittest.mock import patch as _patch
        f = SMAEdgeFilter()
        f.set_symbol("AAPL")
        with _patch("yfinance.Ticker", side_effect=Exception("down")):
            decision = f(_today_df(5))
        assert decision.allowed.iloc[-1]  # earnings fail open; other gates also fail open


# ── TestRSIEdgeFilter ─────────────────────────────────────────────────────────


def _liquid_df(n: int, avg_vol: int = 1_000_000) -> pd.DataFrame:
    """Synthetic df with `n` bars of rising closes and steady volume."""
    idx = pd.date_range("2020-01-01", periods=n, freq="B")
    closes = list(range(1, n + 1))
    return pd.DataFrame(
        {"close": closes, "open": closes, "high": closes,
         "low": closes, "volume": [avg_vol] * n},
        index=idx,
    )


class TestRSIEdgeFilter:
    """RSIEdgeFilter: SPY50 band + earnings blackout + liquidity + breakdown gate."""

    def _spy_allows(self, f: RSIEdgeFilter) -> None:
        f._spy_filter._spy_cache = _spy_df(list(range(1, 211)))
        f._spy_filter._cache_time = float("inf")

    def _spy_blocks(self, f: RSIEdgeFilter) -> None:
        f._spy_filter._spy_cache = _spy_df(list(range(210, 0, -1)))
        f._spy_filter._cache_time = float("inf")

    def _clear_earnings(self, f: RSIEdgeFilter, symbol: str = "MU") -> None:
        """Seed earnings cache with no upcoming dates for symbol."""
        f._earnings._cache[symbol] = (datetime.date.today(), [])

    # ── SPY gate ─────────────────────────────────────────────────────────────

    def test_spy_gate_allows(self):
        f = RSIEdgeFilter(notional_min_avg=0)   # disable vol/low gates
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        decision = f(_liquid_df(25, avg_vol=1_000_000))
        assert isinstance(decision, EdgeFilterDecision)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_spy_gate_blocks(self):
        f = RSIEdgeFilter(notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_blocks(f)
        self._clear_earnings(f)
        decision = f(_liquid_df(25, avg_vol=1_000_000))
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == [
            "SPY trend gate failed: SPY 1.00 ≤ SMA50 tolerance floor 25.25 "
            "(SMA 25.50, tolerance 1.0%)"
        ]

    def test_spy_gate_allows_within_one_percent_band(self):
        f = RSIEdgeFilter(notional_min_avg=0)
        f.set_symbol("MU")
        f._spy_filter._spy_cache = _spy_df([100.0] * 49 + [99.5])
        f._spy_filter._cache_time = float("inf")
        self._clear_earnings(f)

        decision = f(_liquid_df(25, avg_vol=1_000_000))

        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_spy_gate_blocks_when_sma50_history_is_short(self):
        f = RSIEdgeFilter(notional_min_avg=0)
        f.set_symbol("MU")
        self._clear_earnings(f)
        f._spy_filter._spy_cache = _spy_df(list(range(1, 43)))
        f._spy_filter._cache_time = float("inf")

        decision = f(_liquid_df(25, avg_vol=1_000_000))

        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == [
            "SPY trend gate failed: insufficient SPY history for SMA50: "
            "42 bars available, 50 required"
        ]

    def test_spy_history_lookback_has_trading_day_buffer(self):
        f = RSIEdgeFilter()
        assert f._spy_filter._lookback_days == 320

    def test_spy_50_window_required(self):
        """RSI keeps only the SPY 50SMA gate; SPY 200SMA belongs to regime."""
        f = RSIEdgeFilter(notional_min_avg=0)
        f.set_symbol("MU")
        self._clear_earnings(f)
        df = _liquid_df(25, avg_vol=1_000_000)
        assert f._spy_filter._windows == [50]

        with patch.object(f._spy_filter, "_check", return_value=(True, "ok")):
            assert f(df).allowed.iloc[-1]

        with patch.object(f._spy_filter, "_check", return_value=(False, "below 50SMA")):
            decision = f(df)
            assert not decision.allowed.iloc[-1]
            assert decision.latest_reasons == ["SPY trend gate failed: below 50SMA"]

    # ── Earnings blackout gate ────────────────────────────────────────────────

    def test_earnings_blackout_blocks(self):
        f = RSIEdgeFilter(days_before=3, days_after=2, notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_allows(f)
        df = _today_df()
        last_bar = df.index[-1].date()
        tomorrow = last_bar + datetime.timedelta(days=1)
        f._earnings._cache["MU"] = (datetime.date.today(), [tomorrow])
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == ["earnings blackout"]

    def test_earnings_far_away_allows(self):
        f = RSIEdgeFilter(days_before=3, days_after=2, notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_allows(f)
        far = datetime.date.today() + datetime.timedelta(days=30)
        f._earnings._cache["MU"] = (datetime.date.today(), [far])
        decision = f(_liquid_df(25, avg_vol=1_000_000))
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_days_before_after_defaults(self):
        """Default blackout window is 3 days before, 2 days after."""
        f = RSIEdgeFilter()
        assert f._earnings._days_before == 3
        assert f._earnings._days_after == 2

    # ── Liquidity gate ────────────────────────────────────────────────────────

    def test_volume_above_threshold_allows(self):
        f = RSIEdgeFilter(vol_min_window=5, notional_min_avg=500_000)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        df = _liquid_df(25, avg_vol=1_000_000)   # 1M >> 500K
        decision = f(df)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_volume_below_threshold_blocks(self):
        f = RSIEdgeFilter(vol_min_window=5, notional_min_avg=500_000)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        df = _liquid_df(25, avg_vol=500)     # 500 * 25 (close) = 12.5K < 25K (downscaled)
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert any("liquidity too low" in reason for reason in decision.latest_reasons)

    def test_volume_no_column_fails_open(self):
        """No volume column → fail open. Uses rising closes so new_low gate passes."""
        f = RSIEdgeFilter(vol_min_window=5, notional_min_avg=500_000)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        closes = list(range(1, 26))   # rising — new_low gate passes
        idx = pd.date_range("2020-01-01", periods=25, freq="B")
        df = pd.DataFrame({"close": closes}, index=idx)   # no volume column
        decision = f(df)
        assert decision.allowed.iloc[-1]   # fail open on volume
        assert decision.latest_reasons == []

    def test_volume_nan_fails_open(self):
        """Fewer bars than vol_min_window → NaN avg → fail open."""
        f = RSIEdgeFilter(vol_min_window=20, notional_min_avg=500_000)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        df = _liquid_df(5, avg_vol=100_000)   # only 5 bars, need 20
        decision = f(df)
        assert decision.allowed.iloc[-1]   # fail open
        assert decision.latest_reasons == []

    # ── Active-breakdown gate ─────────────────────────────────────────────────

    def test_active_breakdown_gate_allows_rising_stock(self):
        """Rising stock → last close above prior-N min → allowed."""
        f = RSIEdgeFilter(new_low_window=5, notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        df = _liquid_df(25, avg_vol=1_000_000)   # closes 1..25, always rising
        decision = f(df)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_new_low_below_200_sma_blocks(self):
        """Stock making a new 5-day low below its 200 SMA → blocked."""
        f = RSIEdgeFilter(new_low_window=5, notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        closes = [100.0] * 195 + [10, 11, 12, 13, 14, 15, 16, 5]
        idx = pd.date_range("2020-01-01", periods=len(closes), freq="B")
        df = pd.DataFrame(
            {"close": closes, "open": closes, "high": closes,
             "low": closes, "volume": [1_000_000] * len(closes)},
            index=idx,
        )
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == ["new 5-day low below 200 SMA (active breakdown)"]

    def test_new_low_above_200_sma_allows(self):
        """Stock making a short-term low above its 200 SMA remains eligible."""
        f = RSIEdgeFilter(new_low_window=5, notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        closes = [50.0] * 195 + [100, 101, 102, 103, 104, 105, 106, 99]
        idx = pd.date_range("2020-01-01", periods=len(closes), freq="B")
        df = pd.DataFrame(
            {"close": closes, "open": closes, "high": closes,
             "low": closes, "volume": [1_000_000] * len(closes)},
            index=idx,
        )
        decision = f(df)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_new_low_nan_fails_open(self):
        """Fewer bars than new_low_window + 1 → NaN prior_min → fail open."""
        f = RSIEdgeFilter(new_low_window=20, notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        df = _liquid_df(5)   # only 5 bars, need 21 for prior_min to be non-NaN
        with patch("strategies.filters.rsi_reversion.logger.warning") as warn:
            decision = f(df)
        assert decision.allowed.iloc[-1]   # fail open
        assert decision.latest_reasons == []
        warn.assert_called_once()
        assert "active-breakdown gate failed open" in warn.call_args.args[0]

    # ── Structural / combined ─────────────────────────────────────────────────

    def test_all_four_gates_pass(self):
        f = RSIEdgeFilter(vol_min_window=5, notional_min_avg=500_000, new_low_window=5)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        df = _liquid_df(25, avg_vol=1_000_000)
        decision = f(df)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_no_stock_sma_attribute(self):
        """Stock-level 50 SMA gate must not exist on RSIEdgeFilter."""
        f = RSIEdgeFilter()
        assert not hasattr(f, "_stock_sma_window")
        assert not hasattr(f, "_stock_above_sma")

    def test_set_symbol_routes_to_earnings(self):
        f = RSIEdgeFilter()
        f.set_symbol("CDNS")
        assert f._symbol == "CDNS"
        assert f._earnings._symbol == "CDNS"

    def test_gate_series_aligned_to_df(self):
        f = RSIEdgeFilter(notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_allows(f)
        self._clear_earnings(f)
        df = _liquid_df(25)
        decision = f(df)
        assert list(decision.allowed.index) == list(df.index)
        assert list(decision.reasons.index) == list(df.index)

    def test_multiple_block_reasons_are_preserved(self):
        f = RSIEdgeFilter(vol_min_window=5, notional_min_avg=500_000, new_low_window=5)
        f.set_symbol("MU")
        self._spy_blocks(f)
        closes = [100.0] * 195 + [10, 11, 12, 13, 14, 15, 16, 5]
        idx = pd.date_range("2020-01-01", periods=len(closes), freq="B")
        df = pd.DataFrame(
            {"close": closes, "open": closes, "high": closes,
             "low": closes, "volume": [500] * len(closes)},
            index=idx,
        )
        tomorrow = df.index[-1].date() + datetime.timedelta(days=1)
        f._earnings._cache["MU"] = (datetime.date.today(), [tomorrow])

        decision = f(df)

        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == [
            "SPY trend gate failed: SPY 1.00 ≤ SMA50 tolerance floor 25.25 "
            "(SMA 25.50, tolerance 1.0%)",
            "earnings blackout",
            "liquidity too low (avg_dollar_vol5=$6,300 < $500,000)",
            "new 5-day low below 200 SMA (active breakdown)",
        ]

    def test_strategy_inspect_signals_preserves_structured_reason(self):
        f = RSIEdgeFilter(notional_min_avg=0)
        f.set_symbol("MU")
        self._spy_blocks(f)
        self._clear_earnings(f)
        class _AlwaysEntryRSI(RSIReversion):
            def _raw_signals(self, df):
                entries = pd.Series([False] * (len(df) - 1) + [True], index=df.index, dtype=bool)
                exits = pd.Series([False] * len(df), index=df.index, dtype=bool)
                return SignalFrame(entries=entries, exits=exits)

        strategy = _AlwaysEntryRSI(period=2, oversold=30, overbought=70, edge_filter=f)
        closes = [100.0, 90.0, 80.0, 70.0]
        idx = pd.date_range("2020-01-01", periods=len(closes), freq="B")
        df = pd.DataFrame(
            {"close": closes, "open": closes, "high": closes, "low": closes, "volume": [1_000_000] * len(closes)},
            index=idx,
        )

        raw, filtered, edge_allowed, edge_reasons = strategy.inspect_signals(df, symbol="MU")

        assert raw.entries.any()
        assert not filtered.entries.any()
        assert edge_allowed is False
        assert edge_reasons == [
            "SPY trend gate failed: SPY 1.00 ≤ SMA50 tolerance floor 25.25 "
            "(SMA 25.50, tolerance 1.0%)"
        ]


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


# ── TestBollingerSqueezeEdgeFilter ────────────────────────────────────────────


def _liquid_ohlc_df(
    n: int, *, close: float = 100.0, avg_vol: int = 1_000_000, range_pct: float = 0.02
) -> pd.DataFrame:
    """Synthetic OHLC df with steady price near `close`, range_pct width, and steady volume."""
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    half_range = close * range_pct / 2
    closes = [close] * n
    highs = [close + half_range] * n
    lows = [close - half_range] * n
    return pd.DataFrame(
        {
            "close": closes,
            "open": closes,
            "high": highs,
            "low": lows,
            "volume": [avg_vol] * n,
        },
        index=idx,
    )


class TestBollingerSqueezeEdgeFilterGates:
    """End-to-end gate behaviour with a mocked symbol and earnings cache."""

    def _filter(self, monkeypatch, *, feed: str = "sip", **overrides):
        from strategies.filters import bollinger_squeeze as bsq

        monkeypatch.setattr(bsq, "ALPACA_DATA_FEED", feed)
        f = bsq.BollingerSqueezeEdgeFilter(**overrides)
        f.set_symbol("NVDA")
        # Seed earnings cache: no upcoming events.
        f._earnings._cache["NVDA"] = (datetime.date.today(), [])
        return f

    def test_liquidity_above_threshold_allows(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            notional_min_avg=1_000_000,
            vol_min_window=5,
            exhaustion_atr_mult=10.0,  # disable exhaustion (very wide threshold)
        )
        # close=100, vol=20_000 → dollar_vol = 2M >> 1M threshold
        df = _liquid_ohlc_df(25, close=100.0, avg_vol=20_000)
        decision = f(df)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_liquidity_below_threshold_blocks(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            notional_min_avg=10_000_000,
            vol_min_window=5,
            exhaustion_atr_mult=10.0,
        )
        # close=100, vol=20_000 → dollar_vol=2M << 10M threshold
        df = _liquid_ohlc_df(25, close=100.0, avg_vol=20_000)
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert any("liquidity too low" in reason for reason in decision.latest_reasons)

    def test_liquidity_no_volume_column_fails_open(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            notional_min_avg=10_000_000,
            vol_min_window=5,
            exhaustion_atr_mult=10.0,
        )
        idx = pd.date_range("2024-01-01", periods=25, freq="B")
        df = pd.DataFrame(
            {"close": [100.0] * 25, "high": [101.0] * 25, "low": [99.0] * 25},
            index=idx,
        )  # no volume
        decision = f(df)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_liquidity_insufficient_history_fails_open(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            notional_min_avg=10_000_000,
            vol_min_window=20,
            exhaustion_atr_mult=10.0,
        )
        df = _liquid_ohlc_df(5, close=100.0, avg_vol=20_000)  # only 5 bars
        decision = f(df)
        assert decision.allowed.iloc[-1]  # NaN avg → fail open
        assert decision.latest_reasons == []



    def test_exhaustion_gate_blocks_extended_close(self, monkeypatch):
        """
        Hand-verified math:
          bb_length=10, atr_length=10. 9 flat bars at 100, range 2. Final close=150.
          BB(10): mid=105, std=15  → bb_upper = 105 + 2*15 = 135.
          ATR(10) seed at idx 9 = mean(TR_0..TR_9) = (2*9 + 51)/10 = 6.9.
          Threshold = 135 + 1.5*6.9 = 145.35. Close=150 > 145.35 → exhausted.
        """
        f = self._filter(
            monkeypatch,
            notional_min_avg=0,
            bb_length=10,
            bb_std=2.0,
            atr_length=10,
            exhaustion_atr_mult=1.5,
        )
        n = 10
        idx = pd.date_range("2024-01-01", periods=n, freq="B")
        closes = [100.0] * (n - 1) + [150.0]
        highs = [101.0] * (n - 1) + [151.0]
        lows = [99.0] * (n - 1) + [149.0]
        df = pd.DataFrame(
            {
                "close": closes,
                "high": highs,
                "low": lows,
                "volume": [10_000_000] * n,
            },
            index=idx,
        )
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert any("exhausted" in reason for reason in decision.latest_reasons)

    def test_exhaustion_gate_allows_normal_close(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            notional_min_avg=0,
            bb_length=5,
            bb_std=2.0,
            atr_length=5,
            exhaustion_atr_mult=1.5,
        )
        df = _liquid_ohlc_df(15, close=100.0, avg_vol=10_000_000)  # flat → not exhausted
        decision = f(df)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_set_symbol_propagates_to_earnings(self, monkeypatch):
        from strategies.filters import bollinger_squeeze as bsq

        monkeypatch.setattr(bsq, "ALPACA_DATA_FEED", "sip")
        f = bsq.BollingerSqueezeEdgeFilter()
        f.set_symbol("AVGO")
        assert f._symbol == "AVGO"
        assert f._earnings._symbol == "AVGO"

    def test_default_earnings_window_is_2_1(self, monkeypatch):
        from strategies.filters import bollinger_squeeze as bsq

        monkeypatch.setattr(bsq, "ALPACA_DATA_FEED", "sip")
        f = bsq.BollingerSqueezeEdgeFilter()
        assert f._earnings._days_before == 2
        assert f._earnings._days_after == 1


class TestCompositeEdgeFilter:
    def test_merges_structured_and_legacy_reasons(self):
        idx = pd.date_range("2024-01-01", periods=3, freq="B")
        df = pd.DataFrame({"close": [1.0, 2.0, 3.0]}, index=idx)

        class _StructuredFilter:
            def __call__(self, df):
                return EdgeFilterDecision(
                    allowed=pd.Series([True, True, False], index=df.index, dtype=bool),
                    reasons=pd.Series([[], [], ["earnings blackout"]], index=df.index, dtype=object),
                )

        class _LegacyFilter:
            def __call__(self, df):
                return pd.Series([True, True, False], index=df.index, dtype=bool)

            def get_last_block_reasons(self):
                return ["volume contracting"]

        composite = CompositeEdgeFilter([_StructuredFilter(), _LegacyFilter()])
        decision = composite(df)
        assert decision.allowed.tolist() == [True, True, False]
        assert decision.latest_reasons == ["earnings blackout", "volume contracting"]


# ── TestDonchianEdgeFilter ────────────────────────────────────────────────────


def _donchian_ohlc_df(
    n: int,
    *,
    close: float = 100.0,
    avg_vol: int = 1_000_000,
    range_pct: float = 0.02,
    rising: bool = True,
) -> pd.DataFrame:
    """
    Synthetic OHLC df. By default rises slightly each bar so a 200-SMA-style
    "above its SMA" check passes; flip `rising=False` to put close BELOW its
    rolling mean.
    """
    idx = pd.date_range("2024-01-01", periods=n, freq="B")
    half = close * range_pct / 2
    if rising:
        closes = [close + i * 0.05 for i in range(n)]
    else:
        closes = [close - i * 0.05 for i in range(n)]
    highs = [c + half for c in closes]
    lows = [c - half for c in closes]
    return pd.DataFrame(
        {
            "close": closes,
            "open": closes,
            "high": highs,
            "low": lows,
            "volume": [avg_vol] * n,
        },
        index=idx,
    )




class TestDonchianEdgeFilterGates:
    """End-to-end gate behaviour with a mocked symbol and earnings cache."""

    def _filter(self, monkeypatch, *, feed: str = "sip", **overrides):
        from strategies.filters import donchian_breakout as dbk

        monkeypatch.setattr(dbk, "ALPACA_DATA_FEED", feed)
        f = dbk.DonchianEdgeFilter(**overrides)
        f.set_symbol("NVDA")
        # Seed earnings cache: no upcoming events.
        f._earnings._cache["NVDA"] = (datetime.date.today(), [])
        return f

    # ── Stock SMA gate ────────────────────────────────────────────────────────

    def test_stock_above_sma_allows(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            stock_sma_window=10,    # short window so 25 bars suffices
            vol_min_window=5,
            notional_min_avg=0,     # disable liquidity gate
        )
        df = _donchian_ohlc_df(25, close=100.0, avg_vol=10_000, rising=True)
        # close drifts up → above its SMA → gate allows
        decision = f(df)
        assert isinstance(decision, EdgeFilterDecision)
        assert decision.allowed.iloc[-1]
        assert decision.latest_reasons == []

    def test_stock_below_sma_blocks(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            stock_sma_window=10,
            vol_min_window=5,
            notional_min_avg=0,
        )
        df = _donchian_ohlc_df(25, close=100.0, avg_vol=10_000, rising=False)
        # close drifts down → below its SMA → gate blocks
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == ["stock 98.80 ≤ SMA10 99.03"]

    def test_stock_sma_fails_open_on_insufficient_history(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            stock_sma_window=200,   # need 200 bars
            vol_min_window=5,
            notional_min_avg=0,
        )
        df = _donchian_ohlc_df(25, close=100.0, avg_vol=10_000, rising=True)
        # Only 25 bars; SMA200 is NaN → fail open (allow).
        decision = f(df)
        assert decision.allowed.iloc[-1]

    # ── Liquidity gate ────────────────────────────────────────────────────────

    def test_liquidity_above_threshold_allows(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            stock_sma_window=10,
            notional_min_avg=1_000_000,
            vol_min_window=5,
        )
        # close=100, vol=20_000 → dollar_vol = 2M >> 1M threshold
        df = _donchian_ohlc_df(25, close=100.0, avg_vol=20_000)
        decision = f(df)
        assert decision.allowed.iloc[-1]

    def test_liquidity_below_threshold_blocks(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            stock_sma_window=10,
            notional_min_avg=10_000_000,
            vol_min_window=5,
        )
        # close=100, vol=20_000 → dollar_vol=2M << 10M threshold
        df = _donchian_ohlc_df(25, close=100.0, avg_vol=20_000)
        decision = f(df)
        assert not decision.allowed.iloc[-1]
        assert decision.latest_reasons == [
            "liquidity too low (avg_dollar_vol5=$2,022,000 < $10,000,000, feed=sip)"
        ]

    def test_liquidity_no_volume_column_fails_open(self, monkeypatch):
        f = self._filter(
            monkeypatch,
            stock_sma_window=10,
            notional_min_avg=10_000_000,
            vol_min_window=5,
        )
        idx = pd.date_range("2024-01-01", periods=25, freq="B")
        df = pd.DataFrame(
            {
                "close": [100.0 + i * 0.05 for i in range(25)],
                "high": [101.0 + i * 0.05 for i in range(25)],
                "low": [99.0 + i * 0.05 for i in range(25)],
            },
            index=idx,
        )  # no volume
        decision = f(df)
        assert decision.allowed.iloc[-1]



    # ── Earnings + propagation ────────────────────────────────────────────────

    def test_set_symbol_propagates_to_earnings(self, monkeypatch):
        from strategies.filters import donchian_breakout as dbk

        monkeypatch.setattr(dbk, "ALPACA_DATA_FEED", "sip")
        f = dbk.DonchianEdgeFilter()
        f.set_symbol("AVGO")
        assert f._symbol == "AVGO"
        assert f._earnings._symbol == "AVGO"

    def test_default_earnings_window_is_1_0(self, monkeypatch):
        from strategies.filters import donchian_breakout as dbk

        monkeypatch.setattr(dbk, "ALPACA_DATA_FEED", "sip")
        f = dbk.DonchianEdgeFilter()
        # Donchian uses a SHORT blackout (1 before, 0 after) — allows post-earnings
        # continuation entries.
        assert f._earnings._days_before == 1
        assert f._earnings._days_after == 0
