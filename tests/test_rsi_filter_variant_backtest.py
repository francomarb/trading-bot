from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from config import settings
from regime.detector import MarketRegime, RegimeDetector
from scripts import rsi_filter_variant_backtest as harness
from sector.gauge import SectorMomentumGauge
from strategies.filters.common import SPYTrendFilter


def _bars_from_closes(closes: list[float]) -> pd.DataFrame:
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex([start + timedelta(days=i) for i in range(len(closes))])
    return pd.DataFrame(
        {
            "open": closes,
            "high": [price + 1.0 for price in closes],
            "low": [max(price - 1.0, 0.01) for price in closes],
            "close": closes,
            "volume": [1_000_000 + i * 100 for i in range(len(closes))],
        },
        index=idx,
    )


def test_spy_band_gate_matches_production_filter_at_threshold() -> None:
    last_close_at_floor = 0.99 * 49 * 100.0 / (50.0 - 0.99)
    spy = _bars_from_closes([100.0] * 49 + [last_close_at_floor])

    production = SPYTrendFilter(
        sma_windows=[50],
        sma_tolerance_pct=settings.RSI_SPY50_TOLERANCE_PCT,
    )
    production._spy_cache = spy
    production._cache_time = float("inf")

    production_allowed, _reason = production._check()
    historical_allowed = bool(harness._compute_spy_gate(spy, "band_1pct").iloc[-1])

    assert production_allowed is False
    assert historical_allowed is production_allowed


def test_spy_band_gate_matches_production_filter_inside_band() -> None:
    spy = _bars_from_closes([100.0] * 49 + [99.5])
    production = SPYTrendFilter(
        sma_windows=[50],
        sma_tolerance_pct=settings.RSI_SPY50_TOLERANCE_PCT,
    )
    production._spy_cache = spy
    production._cache_time = float("inf")

    production_allowed, _reason = production._check()
    historical_allowed = bool(harness._compute_spy_gate(spy, "band_1pct").iloc[-1])

    assert production_allowed is True
    assert historical_allowed is production_allowed


@pytest.mark.parametrize(
    "closes",
    [
        [100.0 + i * 0.2 for i in range(260)],
        [260.0 - i * 0.4 for i in range(260)],
    ],
)
def test_historical_regime_gate_matches_detector_allowed_regimes(closes: list[float]) -> None:
    spy = _bars_from_closes(closes)
    detector = RegimeDetector()

    production_regime = detector._classify(spy)
    production_allowed = production_regime not in {MarketRegime.BEAR, MarketRegime.VOLATILE}
    historical_allowed = bool(harness._compute_regime_allowed(spy).iloc[-1])

    assert historical_allowed is production_allowed


def test_historical_sector_score_matches_sector_gauge(monkeypatch) -> None:
    bars = _bars_from_closes([100.0 + i * 0.1 for i in range(240)])
    monkeypatch.setattr(settings, "SECTOR_ETFS", {"technology": "XLK"})

    historical_scores = harness._compute_sector_scores({"XLK": bars})
    gauge = SectorMomentumGauge(
        {"technology": "XLK"},
        smooth_window=settings.SECTOR_MOMENTUM_SMOOTH_WINDOW,
    )
    production_detail = gauge._compute("technology", "XLK", bars)

    assert float(historical_scores["technology"].iloc[-1]) == production_detail.score


def test_sector_gate_uses_production_cold_threshold() -> None:
    idx = pd.date_range("2026-01-01", periods=3, tz="UTC")
    edge_filter = harness.HistoricalRSIFilter(
        spy_gate=pd.Series(True, index=idx),
        regime_allowed=pd.Series(True, index=idx),
        sector_scores={"technology": pd.Series([-1.5, -2.0, -2.5], index=idx)},
        sector_by_symbol={"MSFT": "technology"},
        earnings_by_symbol={"MSFT": []},
        breakdown_mode="none",
    )
    edge_filter.set_symbol("MSFT")
    df = pd.DataFrame(
        {
            "open": [100.0, 100.0, 100.0],
            "high": [101.0, 101.0, 101.0],
            "low": [99.0, 99.0, 99.0],
            "close": [100.0, 100.0, 100.0],
            "volume": [1_000_000, 1_000_000, 1_000_000],
        },
        index=idx,
    )

    decision = edge_filter(df)

    assert decision.allowed.tolist() == [True, False, False]
