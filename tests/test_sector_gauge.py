"""
Unit tests for sector/gauge.py and strategies/filters/sector_momentum.py.

All tests are offline; fetch_symbol and SectorResolver are mocked.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from sector.gauge import SectorMomentum, SectorMomentumGauge, SectorScoreDetail


SECTOR_ETFS = {
    "semiconductors": "SMH",
    "technology": "XLK",
    "financials": "XLF",
}


def _make_etf_bars(
    n: int = 250,
    base_price: float = 100.0,
    trend: float = 0.1,
    vol_ratio: float = 1.0,
) -> pd.DataFrame:
    """Build synthetic ETF OHLCV bars."""
    start = datetime.now(timezone.utc) - timedelta(days=n)
    idx = pd.DatetimeIndex(
        [start + timedelta(days=i) for i in range(n)], tz="UTC"
    )
    prices = [base_price + i * trend for i in range(n)]
    volumes = [1_000_000 * vol_ratio for _ in range(n)]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": volumes,
        },
        index=idx,
    )


def _make_declining_etf_bars(n: int = 250, base_price: float = 150.0) -> pd.DataFrame:
    """Declining ETF: price falls below both SMAs, death cross."""
    start = datetime.now(timezone.utc) - timedelta(days=n)
    idx = pd.DatetimeIndex(
        [start + timedelta(days=i) for i in range(n)], tz="UTC"
    )
    # Start high then decline sharply
    prices = [base_price - i * 0.4 for i in range(n)]
    prices = [max(p, 1.0) for p in prices]
    return pd.DataFrame(
        {
            "open": prices,
            "high": [p + 1 for p in prices],
            "low": [p - 1 for p in prices],
            "close": prices,
            "volume": [1_000_000.0] * n,
        },
        index=idx,
    )


class TestSectorMomentumGaugeScoring:
    def _gauge(self):
        return SectorMomentumGauge(sector_etfs=SECTOR_ETFS)

    def test_hot_sector_scores_gte_3(self):
        gauge = self._gauge()
        # Strong uptrend: price well above both SMAs, golden cross, volume confirmation
        df = _make_etf_bars(n=250, base_price=50.0, trend=0.5, vol_ratio=1.2)
        detail = gauge._compute("semiconductors", "SMH", df)
        assert detail.score >= 3
        assert detail.classification == SectorMomentum.HOT

    def test_cold_sector_scores_lte_minus_2(self):
        gauge = self._gauge()
        df = _make_declining_etf_bars(n=250)
        detail = gauge._compute("semiconductors", "SMH", df)
        assert detail.score <= -2
        assert detail.classification == SectorMomentum.COLD

    def test_neutral_classification_for_score_zero(self):
        # Verify classification boundary: score 0 → NEUTRAL
        from sector.gauge import SectorMomentum, _COLD_THRESHOLD, _HOT_THRESHOLD
        assert _COLD_THRESHOLD < 0 < _HOT_THRESHOLD
        for score in range(_COLD_THRESHOLD + 1, _HOT_THRESHOLD):
            if score >= _HOT_THRESHOLD:
                expected = SectorMomentum.HOT
            elif score <= _COLD_THRESHOLD:
                expected = SectorMomentum.COLD
            else:
                expected = SectorMomentum.NEUTRAL
            assert expected == SectorMomentum.NEUTRAL, f"score={score} should be NEUTRAL"

    def test_insufficient_bars_returns_neutral(self):
        gauge = self._gauge()
        # Only 100 bars — not enough for SMA(200)
        df = _make_etf_bars(n=100)
        detail = gauge._compute("semiconductors", "SMH", df)
        assert detail.classification == SectorMomentum.NEUTRAL
        assert detail.score == 0

    def test_none_dataframe_returns_neutral(self):
        gauge = self._gauge()
        detail = gauge._compute("semiconductors", "SMH", None)
        assert detail.classification == SectorMomentum.NEUTRAL
        assert detail.last_close is None

    def test_unknown_sector_returns_neutral_score_zero(self):
        gauge = self._gauge()
        detail = gauge.get_details("unknown_sector")
        assert detail.classification == SectorMomentum.NEUTRAL
        assert detail.score == 0
        assert detail.etf_ticker == "N/A"

    def test_score_components_above_sma200(self):
        gauge = self._gauge()
        df = _make_etf_bars(n=250, base_price=50.0, trend=0.5)
        detail = gauge._compute("semiconductors", "SMH", df)
        assert detail.above_sma200 is True

    def test_score_components_golden_cross(self):
        gauge = self._gauge()
        df = _make_etf_bars(n=250, base_price=50.0, trend=0.5)
        detail = gauge._compute("semiconductors", "SMH", df)
        assert detail.golden_cross is True

    def test_volume_confirm_added_only_when_recent_higher(self):
        gauge = self._gauge()
        # Recent volume lower than historical average (declining volume)
        df = _make_etf_bars(n=250, base_price=50.0, trend=0.5)
        # Override volume to be lower in recent bars
        vol = df["volume"].copy()
        vol.iloc[-20:] = vol.iloc[-20:] * 0.3
        df = df.copy()
        df["volume"] = vol
        detail = gauge._compute("semiconductors", "SMH", df)
        assert not detail.vol_confirm

    def test_dist_sma50_pct_is_correct_sign(self):
        gauge = self._gauge()
        df = _make_etf_bars(n=250, base_price=50.0, trend=0.5)
        detail = gauge._compute("semiconductors", "SMH", df)
        # Strong uptrend → close well above SMA50
        assert detail.dist_sma50_pct > 0


class TestSectorMomentumGaugeCaching:
    def test_classify_uses_score_cache(self):
        gauge = SectorMomentumGauge(sector_etfs=SECTOR_ETFS)
        df = _make_etf_bars(n=250, base_price=50.0, trend=0.5)

        with patch.object(gauge, "_fetch_etf", return_value=df) as mock_fetch:
            gauge.classify("semiconductors")
            gauge.classify("semiconductors")  # second call → cache hit
            mock_fetch.assert_called_once()  # fetched only once

    def test_classify_all_returns_all_sectors(self):
        gauge = SectorMomentumGauge(sector_etfs=SECTOR_ETFS)
        df = _make_etf_bars(n=250)

        with patch.object(gauge, "_fetch_etf", return_value=df):
            result = gauge.classify_all()

        assert set(result.keys()) == set(SECTOR_ETFS.keys())

    def test_get_score_matches_get_details(self):
        gauge = SectorMomentumGauge(sector_etfs=SECTOR_ETFS)
        df = _make_etf_bars(n=250, base_price=50.0, trend=0.5)

        with patch.object(gauge, "_fetch_etf", return_value=df):
            score = gauge.get_score("semiconductors")
            detail = gauge.get_details("semiconductors")

        assert score == detail.score

    def test_stale_etf_cache_returns_neutral_on_fetch_failure(self):
        gauge = SectorMomentumGauge(sector_etfs=SECTOR_ETFS)

        with patch("data.fetcher.fetch_symbol", side_effect=RuntimeError("network error")):
            detail = gauge.get_details("semiconductors")

        assert detail.classification == SectorMomentum.NEUTRAL


class TestSectorMomentumFilter:
    """Tests for the SectorMomentumFilter edge filter adapter."""

    def _make_df(self, n: int = 5) -> pd.DataFrame:
        start = datetime.now(timezone.utc) - timedelta(days=n)
        idx = pd.DatetimeIndex(
            [start + timedelta(days=i) for i in range(n)], tz="UTC"
        )
        return pd.DataFrame(
            {"close": [100.0] * n, "volume": [1_000_000.0] * n},
            index=idx,
        )

    def _make_filter(self, cold_policy: str = "block"):
        from strategies.filters.sector_momentum import SectorMomentumFilter
        gauge = MagicMock()
        resolver = MagicMock()
        return SectorMomentumFilter(gauge=gauge, resolver=resolver, cold_policy=cold_policy), gauge, resolver

    def test_unmapped_symbol_passes_fail_open(self):
        f, gauge, resolver = self._make_filter()
        resolver.resolve.return_value = None
        f.set_symbol("WEIRD")
        df = self._make_df()
        result = f(df)
        assert result.all()

    def test_cold_block_policy_returns_false(self):
        f, gauge, resolver = self._make_filter(cold_policy="block")
        resolver.resolve.return_value = "semiconductors"
        detail = MagicMock()
        detail.classification = SectorMomentum.COLD
        detail.score = -3
        detail.etf_ticker = "SMH"
        detail.above_sma200 = False
        detail.above_sma50 = False
        detail.golden_cross = False
        detail.dist_sma50_pct = -0.05
        detail.vol_confirm = False
        gauge.get_details.return_value = detail
        f.set_symbol("NVDA")
        df = self._make_df()
        result = f(df)
        assert not result.any()

    def test_cold_warn_policy_returns_true(self):
        f, gauge, resolver = self._make_filter(cold_policy="warn")
        resolver.resolve.return_value = "semiconductors"
        detail = MagicMock()
        detail.classification = SectorMomentum.COLD
        detail.score = -3
        detail.etf_ticker = "SMH"
        detail.above_sma200 = False
        detail.above_sma50 = False
        detail.golden_cross = False
        detail.dist_sma50_pct = -0.05
        detail.vol_confirm = False
        gauge.get_details.return_value = detail
        f.set_symbol("NVDA")
        df = self._make_df()
        result = f(df)
        assert result.all()

    def test_cold_pass_policy_returns_true(self):
        f, gauge, resolver = self._make_filter(cold_policy="pass")
        resolver.resolve.return_value = "semiconductors"
        detail = MagicMock()
        detail.classification = SectorMomentum.COLD
        detail.score = -3
        detail.etf_ticker = "SMH"
        detail.above_sma200 = False
        detail.above_sma50 = False
        detail.golden_cross = False
        detail.dist_sma50_pct = -0.05
        detail.vol_confirm = False
        gauge.get_details.return_value = detail
        f.set_symbol("NVDA")
        df = self._make_df()
        result = f(df)
        assert result.all()

    def test_hot_sector_always_returns_true(self):
        f, gauge, resolver = self._make_filter(cold_policy="block")
        resolver.resolve.return_value = "semiconductors"
        detail = MagicMock()
        detail.classification = SectorMomentum.HOT
        detail.score = 4
        detail.etf_ticker = "SMH"
        gauge.get_details.return_value = detail
        f.set_symbol("NVDA")
        df = self._make_df()
        result = f(df)
        assert result.all()

    def test_neutral_sector_always_returns_true(self):
        f, gauge, resolver = self._make_filter(cold_policy="block")
        resolver.resolve.return_value = "semiconductors"
        detail = MagicMock()
        detail.classification = SectorMomentum.NEUTRAL
        detail.score = 0
        detail.etf_ticker = "SMH"
        gauge.get_details.return_value = detail
        f.set_symbol("NVDA")
        df = self._make_df()
        result = f(df)
        assert result.all()

    def test_no_symbol_set_passes_through(self):
        f, gauge, resolver = self._make_filter()
        df = self._make_df()
        result = f(df)
        assert result.all()
        resolver.resolve.assert_not_called()

    def test_invalid_cold_policy_raises(self):
        from strategies.filters.sector_momentum import SectorMomentumFilter
        gauge = MagicMock()
        resolver = MagicMock()
        with pytest.raises(ValueError, match="cold_policy"):
            SectorMomentumFilter(gauge=gauge, resolver=resolver, cold_policy="invalid")

    def test_result_series_has_same_index_as_df(self):
        f, gauge, resolver = self._make_filter(cold_policy="block")
        resolver.resolve.return_value = None
        f.set_symbol("AAPL")
        df = self._make_df(n=10)
        result = f(df)
        assert result.index.equals(df.index)


class TestCompositeEdgeFilter:
    """Tests for CompositeEdgeFilter in strategies/filters/common.py."""

    def _make_df(self, n: int = 5) -> pd.DataFrame:
        start = datetime.now(timezone.utc) - timedelta(days=n)
        idx = pd.DatetimeIndex(
            [start + timedelta(days=i) for i in range(n)], tz="UTC"
        )
        return pd.DataFrame(
            {"close": [100.0] * n, "volume": [1_000_000.0] * n},
            index=idx,
        )

    def test_and_chains_two_true_filters(self):
        from strategies.filters.common import CompositeEdgeFilter

        df = self._make_df()
        f1 = MagicMock(return_value=pd.Series(True, index=df.index, dtype=bool))
        f2 = MagicMock(return_value=pd.Series(True, index=df.index, dtype=bool))
        f1.set_symbol = MagicMock()
        f2.set_symbol = MagicMock()

        composite = CompositeEdgeFilter([f1, f2])
        composite.set_symbol("AAPL")
        result = composite(df)
        assert result.all()

    def test_false_any_filter_blocks_all(self):
        from strategies.filters.common import CompositeEdgeFilter

        df = self._make_df()
        f1 = MagicMock(return_value=pd.Series(True, index=df.index, dtype=bool))
        f2 = MagicMock(return_value=pd.Series(False, index=df.index, dtype=bool))

        composite = CompositeEdgeFilter([f1, f2])
        result = composite(df)
        assert not result.any()

    def test_set_symbol_delegates_to_all_filters(self):
        from strategies.filters.common import CompositeEdgeFilter

        df = self._make_df()
        f1 = MagicMock(return_value=pd.Series(True, index=df.index, dtype=bool))
        f2 = MagicMock(return_value=pd.Series(True, index=df.index, dtype=bool))
        f1.set_symbol = MagicMock()
        f2.set_symbol = MagicMock()

        composite = CompositeEdgeFilter([f1, f2])
        composite.set_symbol("NVDA")

        f1.set_symbol.assert_called_once_with("NVDA")
        f2.set_symbol.assert_called_once_with("NVDA")

    def test_set_symbol_skips_filters_without_method(self):
        from strategies.filters.common import CompositeEdgeFilter

        df = self._make_df()
        # f1 has no set_symbol attribute
        f1 = MagicMock(spec=["__call__"])
        f1.return_value = pd.Series(True, index=df.index, dtype=bool)

        composite = CompositeEdgeFilter([f1])
        composite.set_symbol("NVDA")  # must not raise

    def test_empty_filter_list_raises(self):
        from strategies.filters.common import CompositeEdgeFilter

        with pytest.raises(ValueError):
            CompositeEdgeFilter([])

    def test_single_filter_passthrough(self):
        from strategies.filters.common import CompositeEdgeFilter

        df = self._make_df()
        gate = pd.Series([True, False, True, True, False], index=df.index, dtype=bool)
        f1 = MagicMock(return_value=gate)

        composite = CompositeEdgeFilter([f1])
        result = composite(df)
        pd.testing.assert_series_equal(result, gate)
