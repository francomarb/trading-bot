"""
Sector Momentum Gauge — classifies sector ETFs as HOT / NEUTRAL / COLD.

This is a **context provider**, not a gate.  It computes and exposes sector
health information.  Strategies and edge filters query it to make their own
decisions about whether to enter, warn, or ignore.

Scoring (daily bars, per sector ETF):

    +1  ETF close > SMA(200)         (else -1)
    +1  ETF close > SMA(50)          (else -1)
    +1  SMA(50) > SMA(200)           (else -1)  — golden/death cross state
    +1  distance from SMA(50) > +2%  (else -1 if < -2%, else 0)
    +1  volume 10d avg > 20d avg     (confirmation only, never -1)

Thresholds:
    HOT      score >= 3
    COLD     score <= -2
    NEUTRAL  everything else
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum

import pandas as pd
from loguru import logger

from indicators.technicals import add_sma


class SectorMomentum(Enum):
    HOT = "hot"
    NEUTRAL = "neutral"
    COLD = "cold"


@dataclass(frozen=True)
class SectorScoreDetail:
    """Full breakdown of a sector's momentum score."""

    sector: str
    etf_ticker: str
    score: int
    classification: SectorMomentum
    above_sma200: bool
    above_sma50: bool
    golden_cross: bool
    dist_sma50_pct: float
    vol_confirm: bool
    last_close: float | None


# ── Thresholds ────────────��──────────────────────────────────────────────────

_HOT_THRESHOLD = 3
_COLD_THRESHOLD = -2
_DIST_SMA50_HOT_PCT = 0.02
_DIST_SMA50_COLD_PCT = -0.02


class SectorMomentumGauge:
    """Computes per-sector heat scores from sector ETF daily bars.

    Parameters
    ----------
    sector_etfs
        Mapping of normalized sector key → ETF ticker
        (e.g. ``{"semiconductors": "SMH", "financials": "XLF"}``).
    cache_ttl_seconds
        How long to reuse a cached ETF fetch (default 600 s, same as
        ``RegimeDetector`` and ``SPYTrendFilter``).
    lookback_days
        Calendar days of ETF history to fetch (default 300, covers 200
        trading days with buffer for weekends/holidays).
    """

    def __init__(
        self,
        sector_etfs: dict[str, str],
        cache_ttl_seconds: float = 600.0,
        lookback_days: int = 300,
    ) -> None:
        self._sector_etfs = dict(sector_etfs)
        self._cache_ttl = cache_ttl_seconds
        self._lookback_days = lookback_days
        self._etf_cache: dict[str, tuple[pd.DataFrame, float]] = {}
        self._score_cache: dict[str, tuple[SectorScoreDetail, float]] = {}

    # ── Public API ───────────────────────────────────────────────────────

    def classify(self, sector: str) -> SectorMomentum:
        """Return HOT / NEUTRAL / COLD for one sector."""
        detail = self.get_details(sector)
        return detail.classification

    def classify_all(self) -> dict[str, SectorMomentum]:
        """Batch classify all configured sectors."""
        return {s: self.classify(s) for s in self._sector_etfs}

    def get_score(self, sector: str) -> int:
        """Raw numeric score for strategies that want granularity."""
        return self.get_details(sector).score

    def get_details(self, sector: str) -> SectorScoreDetail:
        """Full breakdown: score, individual signals, ETF ticker, last price."""
        now = time.monotonic()
        cached = self._score_cache.get(sector)
        if cached is not None and (now - cached[1]) < self._cache_ttl:
            return cached[0]

        etf_ticker = self._sector_etfs.get(sector)
        if etf_ticker is None:
            detail = SectorScoreDetail(
                sector=sector,
                etf_ticker="N/A",
                score=0,
                classification=SectorMomentum.NEUTRAL,
                above_sma200=False,
                above_sma50=False,
                golden_cross=False,
                dist_sma50_pct=0.0,
                vol_confirm=False,
                last_close=None,
            )
            self._score_cache[sector] = (detail, now)
            return detail

        df = self._fetch_etf(etf_ticker)
        detail = self._compute(sector, etf_ticker, df)
        self._score_cache[sector] = (detail, now)
        return detail

    # ── Data fetching ───────────────��────────────────────────────────────

    def _fetch_etf(self, ticker: str) -> pd.DataFrame | None:
        """Fetch ETF bars, reusing cache within TTL."""
        now = time.monotonic()
        cached = self._etf_cache.get(ticker)
        if cached is not None and (now - cached[1]) < self._cache_ttl:
            return cached[0]

        try:
            import datetime
            from config.settings import ALPACA_DATA_FEED
            from data.fetcher import fetch_symbol

            end = datetime.datetime.now(datetime.timezone.utc)
            start = end - datetime.timedelta(days=self._lookback_days)
            # Live engine path — match the bot's runtime feed.
            df, _stats = fetch_symbol(
                ticker, start, end, timeframe="1Day", feed=ALPACA_DATA_FEED
            )
            self._etf_cache[ticker] = (df, now)
            logger.debug(
                f"SectorMomentumGauge: fetched {len(df)} bars for {ticker}"
            )
            return df
        except Exception as exc:
            if cached is not None:
                logger.warning(
                    f"SectorMomentumGauge: failed to fetch {ticker} — {exc}. "
                    "Using stale cache."
                )
                self._etf_cache[ticker] = (cached[0], now)
                return cached[0]
            logger.warning(
                f"SectorMomentumGauge: failed to fetch {ticker} and no "
                f"prior cache — {exc}. Returning NEUTRAL."
            )
            self._etf_cache[ticker] = (pd.DataFrame(), now)
            return None

    # ── Scoring ──────────────��───────────────────────────────────────────

    def _compute(
        self, sector: str, etf_ticker: str, df: pd.DataFrame | None
    ) -> SectorScoreDetail:
        """Compute the composite score from SMA distances + volume."""
        if df is None or df.empty or len(df) < 200:
            return SectorScoreDetail(
                sector=sector,
                etf_ticker=etf_ticker,
                score=0,
                classification=SectorMomentum.NEUTRAL,
                above_sma200=False,
                above_sma50=False,
                golden_cross=False,
                dist_sma50_pct=0.0,
                vol_confirm=False,
                last_close=df["close"].iloc[-1] if df is not None and not df.empty else None,
            )

        df = add_sma(df, 200)
        df = add_sma(df, 50)

        last = df.iloc[-1]
        close = float(last["close"])
        sma200 = float(last["sma_200"])
        sma50 = float(last["sma_50"])

        above_sma200 = close > sma200
        above_sma50 = close > sma50
        golden_cross = sma50 > sma200
        dist_sma50_pct = (close - sma50) / sma50 if sma50 != 0 else 0.0

        vol_confirm = False
        if "volume" in df.columns:
            vol = df["volume"].astype(float)
            vol_10d = vol.iloc[-10:].mean() if len(vol) >= 10 else 0.0
            vol_20d = vol.iloc[-20:].mean() if len(vol) >= 20 else 0.0
            vol_confirm = vol_10d > vol_20d and vol_20d > 0

        score = 0
        score += 1 if above_sma200 else -1
        score += 1 if above_sma50 else -1
        score += 1 if golden_cross else -1
        if dist_sma50_pct > _DIST_SMA50_HOT_PCT:
            score += 1
        elif dist_sma50_pct < _DIST_SMA50_COLD_PCT:
            score -= 1
        if vol_confirm:
            score += 1

        if score >= _HOT_THRESHOLD:
            classification = SectorMomentum.HOT
        elif score <= _COLD_THRESHOLD:
            classification = SectorMomentum.COLD
        else:
            classification = SectorMomentum.NEUTRAL

        return SectorScoreDetail(
            sector=sector,
            etf_ticker=etf_ticker,
            score=score,
            classification=classification,
            above_sma200=above_sma200,
            above_sma50=above_sma50,
            golden_cross=golden_cross,
            dist_sma50_pct=dist_sma50_pct,
            vol_confirm=vol_confirm,
            last_close=close,
        )
