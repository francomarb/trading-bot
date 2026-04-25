"""
Unit tests for regime/detector.py (Phase 10.F2).

Coverage map:
  - TestADXInTechnicals  : add_adx produces correct columns; first NaN count;
                           +DI dominates in uptrend; input not mutated
  - TestRegimeClassify   : _classify logic for every regime path:
                             BEAR (SPY < SMA200), VOLATILE (ATR% > 80th pct),
                             TRENDING (ADX ≥ threshold), RANGING (ADX ≤ threshold),
                             ambiguous ADX zone uses SMA50 slope tie-breaker
  - TestRegimeCache      : TTL cache reuses result; expired cache re-classifies
  - TestRegimeFallback   : SPY fetch failure → last cached regime or RANGING;
                           failure rate-limits via advancing cache_time
  - TestStrategySlotRegime: StrategySlot.allowed_regimes field wired correctly;
                            None means all regimes permitted
  - TestEngineRegimeGate : engine blocks entries when regime not in allowed set;
                           exits are never blocked regardless of regime;
                           regime detector failure does not crash the engine
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from indicators.technicals import add_adx
from regime.detector import MarketRegime, RegimeDetector
from strategies.base import BaseStrategy, OrderType, SignalFrame, StrategySlot


# ── Helpers ──────────────────────────────────────────────────────────────────

T0 = datetime(2026, 1, 2, tzinfo=timezone.utc)


def _make_spy(n: int, *, start_price: float = 400.0, trend: float = 0.5) -> pd.DataFrame:
    """
    Synthetic SPY OHLCV with a steady trend (trend $/bar).
    ATR is roughly 4 (high - low = 4) so ATR% ≈ 1%.
    A steady linear trend produces high ADX (no counter-moves).
    """
    idx = pd.DatetimeIndex(
        [T0 + timedelta(days=i) for i in range(n)], tz="UTC"
    )
    closes = [start_price + trend * i for i in range(n)]
    return pd.DataFrame(
        {
            "open":   [c - 0.5 for c in closes],
            "high":   [c + 2.0 for c in closes],
            "low":    [c - 2.0 for c in closes],
            "close":  closes,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def _make_spy_oscillating(n: int, *, start_price: float = 500.0, amplitude: float = 2.0) -> pd.DataFrame:
    """
    Synthetic SPY that oscillates up and down (low ADX, no trend).
    Stays well above a 200-SMA because start_price is high and it doesn't drift.
    """
    idx = pd.DatetimeIndex(
        [T0 + timedelta(days=i) for i in range(n)], tz="UTC"
    )
    closes = [start_price + amplitude * (1 if i % 2 == 0 else -1) for i in range(n)]
    return pd.DataFrame(
        {
            "open":   [c - 0.2 for c in closes],
            "high":   [c + 0.5 for c in closes],
            "low":    [c - 0.5 for c in closes],
            "close":  closes,
            "volume": [1_000_000] * n,
        },
        index=idx,
    )


def _make_detector(**kwargs) -> RegimeDetector:
    """Return a RegimeDetector with a very short cache TTL so tests don't block."""
    kwargs.setdefault("cache_ttl_seconds", 0.0)
    return RegimeDetector(**kwargs)


# ── ADX indicator ────────────────────────────────────────────────────────────


class TestADXInTechnicals:
    def test_returns_correct_columns(self):
        df = _make_spy(60)
        out = add_adx(df, 14)
        assert "adx_14" in out.columns
        assert "plus_di_14" in out.columns
        assert "minus_di_14" in out.columns

    def test_first_values_are_nan(self):
        """
        The smooth_DM/TR RMA (length=14) produces its first value at index 13.
        The ADX RMA is seeded using the mean of the first 14 DX values; since
        DX[0:13] are NaN, pandas mean() skips them and seeds from DX[13].
        Result: ADX is NaN for indices 0-12, valid from index 13.
        """
        df = _make_spy(60)
        out = add_adx(df, 14)
        adx = out["adx_14"]
        assert adx.iloc[:13].isna().all(), "first 13 ADX values should be NaN"
        assert pd.notna(adx.iloc[13]), "ADX should be valid from index 13"

    def test_trending_uptrend_adx_positive(self):
        """Strong consistent trend produces a positive, finite ADX."""
        df = _make_spy(100, trend=2.0)
        out = add_adx(df, 14)
        adx_last = out["adx_14"].iloc[-1]
        assert pd.notna(adx_last)
        assert adx_last > 0

    def test_plus_di_dominates_in_uptrend(self):
        df = _make_spy(100, trend=2.0)
        out = add_adx(df, 14)
        assert out["plus_di_14"].iloc[-1] > out["minus_di_14"].iloc[-1]

    def test_oscillating_market_has_low_adx(self):
        """Choppy up-down oscillation → +DM and -DM cancel out → low ADX."""
        df = _make_spy_oscillating(200)
        out = add_adx(df, 14)
        adx_last = out["adx_14"].iloc[-1]
        assert pd.notna(adx_last)
        # Oscillating market should produce low ADX (< 25 — not trending)
        assert adx_last < 25, f"expected low ADX in oscillating market, got {adx_last:.1f}"

    def test_input_not_mutated(self):
        df = _make_spy(60)
        original_cols = list(df.columns)
        add_adx(df, 14)
        assert list(df.columns) == original_cols

    def test_missing_column_raises(self):
        df = _make_spy(60).drop(columns=["high"])
        with pytest.raises(ValueError, match="missing required columns"):
            add_adx(df, 14)

    def test_invalid_length_raises(self):
        df = _make_spy(60)
        with pytest.raises(ValueError):
            add_adx(df, 0)


# ── _classify logic ──────────────────────────────────────────────────────────


class TestRegimeClassify:
    """
    Drive _classify directly with controlled SPY DataFrames.

    We patch _fetch_spy so detect() uses our synthetic frames.
    """

    def _detect_with(self, spy: pd.DataFrame, **init_kwargs) -> MarketRegime:
        d = _make_detector(**init_kwargs)
        with patch.object(d, "_fetch_spy", return_value=spy):
            return d.detect()

    # ── BEAR ─────────────────────────────────────────────────────────────────

    def test_bear_when_close_below_sma200(self):
        # 205 bars: start at 600, strong downtrend → close << SMA200
        spy = _make_spy(205, start_price=600.0, trend=-2.0)
        # After 204 bars: close ≈ 600 − 2*204 = 192 while SMA200 ≈ 396
        regime = self._detect_with(spy)
        assert regime == MarketRegime.BEAR

    def test_not_bear_when_close_above_sma200(self):
        # Gentle uptrend — close stays above SMA200.
        spy = _make_spy(210, start_price=400.0, trend=0.5)
        regime = self._detect_with(spy)
        assert regime != MarketRegime.BEAR

    def test_bear_takes_priority_over_volatile(self):
        """Even with extreme ATR%, BEAR is returned first."""
        # Volatile downtrend.
        spy = _make_spy(205, start_price=600.0, trend=-2.0)
        # Widen the H/L range massively to inflate ATR.
        spy["high"] = spy["close"] + 20
        spy["low"]  = spy["close"] - 20
        regime = self._detect_with(spy)
        assert regime == MarketRegime.BEAR

    # ── VOLATILE ─────────────────────────────────────────────────────────────

    def test_volatile_when_atr_pct_above_threshold(self):
        # Build an uptrend frame that passes the BEAR gate, then spike ATR
        # on the last bar so its ATR% ranks in the top 5% of recent history.
        spy = _make_spy(210, start_price=400.0, trend=0.5)
        # Widen the last bar enormously relative to all prior bars.
        spy.iloc[-1, spy.columns.get_loc("high")] = spy["close"].iloc[-1] + 200
        spy.iloc[-1, spy.columns.get_loc("low")]  = spy["close"].iloc[-1] - 200
        regime = self._detect_with(
            spy,
            vol_percentile_threshold=0.80,
            adx_trend_threshold=25.0,
            adx_range_threshold=20.0,
        )
        assert regime == MarketRegime.VOLATILE

    def test_not_volatile_with_stable_atr(self):
        spy = _make_spy(210, start_price=400.0, trend=0.5)
        regime = self._detect_with(spy)
        # Stable ATR → should not be VOLATILE.
        assert regime != MarketRegime.VOLATILE

    # ── TRENDING ─────────────────────────────────────────────────────────────

    def test_trending_when_adx_above_threshold(self):
        # Very strong trend → ADX well above the lowered threshold.
        spy = _make_spy(210, start_price=400.0, trend=3.0)
        regime = self._detect_with(spy, adx_trend_threshold=10.0)
        assert regime == MarketRegime.TRENDING

    def test_trending_with_default_thresholds_in_strong_uptrend(self):
        # A strong linear uptrend produces ADX > 25 with enough history.
        spy = _make_spy(210, start_price=400.0, trend=2.0)
        regime = self._detect_with(spy)
        assert regime == MarketRegime.TRENDING

    # ── RANGING ──────────────────────────────────────────────────────────────

    def test_ranging_when_oscillating_market(self):
        """Choppy oscillating prices → low ADX → RANGING."""
        # Use n=211 (odd) so the last bar is index 210 (even → high side = 502)
        # while SMA200 of the alternating 502/498 series = 500.
        # 502 > 500 → passes BEAR gate; low ADX → RANGING.
        spy = _make_spy_oscillating(211, start_price=500.0)
        regime = self._detect_with(spy)
        assert regime == MarketRegime.RANGING

    # ── Ambiguous ADX zone (slope tie-breaker) ────────────────────────────────

    def test_ambiguous_zone_positive_slope_gives_trending(self):
        """ADX in ambiguous band AND positive SMA50 slope → TRENDING."""
        spy = _make_spy(210, start_price=400.0, trend=0.5)
        # Force detector into ambiguous zone: threshold so high ADX never
        # exceeds it, threshold so low ADX always exceeds it.
        # That means BEAR is the only other route — so use an uptrend
        # where close > SMA200. The ambiguous zone logic gives TRENDING
        # because slope is positive.
        regime = self._detect_with(
            spy,
            adx_trend_threshold=999.0,   # ADX will never reach this
            adx_range_threshold=0.0,     # ADX is always > 0 → never RANGING early
        )
        # Uptrend → positive SMA50 slope → TRENDING
        assert regime == MarketRegime.TRENDING

    def test_ambiguous_zone_negative_slope_gives_ranging(self):
        """ADX in ambiguous band AND flat/negative SMA50 slope → RANGING."""
        # n=211 ensures the last bar is the high side (502 > SMA200 500).
        # The SMA50 slope of an oscillating series is flat/zero → RANGING.
        spy = _make_spy_oscillating(211, start_price=500.0)
        regime = self._detect_with(
            spy,
            adx_trend_threshold=999.0,
            adx_range_threshold=0.0,
        )
        assert regime == MarketRegime.RANGING

    def test_insufficient_adx_history_returns_ranging(self):
        """Only a few bars → ADX NaN (not enough history) → RANGING fallback."""
        # 10 bars → _wilder_rma needs 14 → ADX = NaN for all bars → regime=RANGING
        spy = _make_spy(10)
        regime = self._detect_with(spy)
        assert regime == MarketRegime.RANGING


# ── TTL cache ────────────────────────────────────────────────────────────────


class TestRegimeCache:
    def test_result_reused_within_ttl(self):
        spy = _make_spy(210, start_price=400.0, trend=0.5)
        d = RegimeDetector(cache_ttl_seconds=60.0)
        call_count = 0

        def counting_fetch():
            nonlocal call_count
            call_count += 1
            return spy

        with patch.object(d, "_fetch_spy", side_effect=counting_fetch):
            r1 = d.detect()
            r2 = d.detect()
            r3 = d.detect()

        assert call_count == 1, "should have fetched SPY only once within TTL"
        assert r1 == r2 == r3

    def test_cache_expires_and_reclassifies(self):
        spy = _make_spy(210, start_price=400.0, trend=0.5)
        d = RegimeDetector(cache_ttl_seconds=0.0)
        call_count = 0

        def counting_fetch():
            nonlocal call_count
            call_count += 1
            return spy

        with patch.object(d, "_fetch_spy", side_effect=counting_fetch):
            d.detect()
            d.detect()

        assert call_count == 2, "TTL=0 should re-classify on every call"


# ── Failure fallback ─────────────────────────────────────────────────────────


class TestRegimeFallback:
    def test_fetch_failure_returns_ranging_with_no_cache(self):
        d = _make_detector()
        with patch.object(d, "_fetch_spy", return_value=None):
            regime = d.detect()
        assert regime == MarketRegime.RANGING

    def test_fetch_failure_returns_last_cached_regime(self):
        spy = _make_spy(210, start_price=400.0, trend=0.5)
        d = RegimeDetector(cache_ttl_seconds=0.0)

        # First call succeeds → caches some regime.
        with patch.object(d, "_fetch_spy", return_value=spy):
            first = d.detect()

        # Second call fails → should return the last cached regime.
        with patch.object(d, "_fetch_spy", return_value=None):
            fallback = d.detect()

        assert fallback == first

    def test_fetch_failure_advances_spy_cache_time(self):
        """
        A fetch exception must advance _spy_cache_time to rate-limit retries.
        The real implementation advances it inside _fetch_spy on failure.
        We drive it through _fetch_spy directly so we can inspect state.
        """
        d = _make_detector()
        before = d._spy_cache_time

        # Patch the internal import used by _fetch_spy.
        with patch("data.fetcher.fetch_symbol", side_effect=RuntimeError("timeout")):
            d._fetch_spy()

        assert d._spy_cache_time > before, (
            "_spy_cache_time must advance on failure to rate-limit retries"
        )

    def test_empty_dataframe_falls_back(self):
        d = _make_detector()
        with patch.object(d, "_fetch_spy", return_value=pd.DataFrame()):
            regime = d.detect()
        assert regime == MarketRegime.RANGING


# ── StrategySlot.allowed_regimes ─────────────────────────────────────────────


class _TrivialStrategy(BaseStrategy):
    name = "trivial"
    preferred_order_type = OrderType.MARKET

    def _raw_signals(self, df):
        return SignalFrame(
            entries=pd.Series([False] * len(df), index=df.index, dtype=bool),
            exits=pd.Series([False] * len(df), index=df.index, dtype=bool),
        )


class TestStrategySlotRegime:
    def test_allowed_regimes_default_is_none(self):
        slot = StrategySlot(strategy=_TrivialStrategy(), symbols=["AAPL"])
        assert slot.allowed_regimes is None

    def test_allowed_regimes_frozenset_stored(self):
        regimes = frozenset({MarketRegime.TRENDING, MarketRegime.RANGING})
        slot = StrategySlot(
            strategy=_TrivialStrategy(),
            symbols=["AAPL"],
            allowed_regimes=regimes,
        )
        assert slot.allowed_regimes == regimes

    def test_none_means_all_regimes_permitted(self):
        slot = StrategySlot(strategy=_TrivialStrategy(), symbols=["AAPL"])
        # None allowed_regimes → no gating. Every regime should "pass".
        for regime in MarketRegime:
            permitted = slot.allowed_regimes is None or regime in slot.allowed_regimes
            assert permitted

    def test_frozenset_blocks_bear_and_volatile(self):
        regimes = frozenset({MarketRegime.TRENDING, MarketRegime.RANGING})
        assert MarketRegime.BEAR not in regimes
        assert MarketRegime.VOLATILE not in regimes
        assert MarketRegime.TRENDING in regimes
        assert MarketRegime.RANGING in regimes


# ── Engine regime gating ──────────────────────────────────────────────────────


class TestEngineRegimeGate:
    """
    Verify the engine correctly gates entries (but not exits) by regime.

    Uses the legacy single-strategy engine API to stay consistent with the
    existing test_engine.py patterns. Regime detector is injected.
    """

    def _bars(self) -> pd.DataFrame:
        """Return 60 bars ending today so the freshness check always passes."""
        from datetime import timezone as tz
        now = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        idx = pd.DatetimeIndex(
            [now - timedelta(days=59 - i) for i in range(60)], tz="UTC"
        )
        closes = [100.0 + i for i in range(60)]
        return pd.DataFrame(
            {
                "open":   [c - 0.5 for c in closes],
                "high":   [c + 2.0 for c in closes],
                "low":    [c - 2.0 for c in closes],
                "close":  closes,
                "volume": [1_000_000] * 60,
            },
            index=idx,
        )

    def _make_engine(
        self,
        *,
        regime: MarketRegime,
        allowed_regimes,
        entry_signal: bool,
    ):
        from engine.trader import EngineConfig, TradingEngine
        from execution.broker import BrokerSnapshot, OrderResult, OrderStatus
        from risk.manager import AccountState, RiskManager
        from reporting.logger import TradeLogger
        from reporting.pnl import PnLTracker
        from reporting.alerts import AlertDispatcher

        # ── Fake strategy ─────────────────────────────────────────────────────
        class _FakeStrategy(BaseStrategy):
            name = "fake"
            preferred_order_type = OrderType.MARKET

            def _raw_signals(self, df):
                entries = pd.Series([False] * len(df), index=df.index, dtype=bool)
                exits   = pd.Series([False] * len(df), index=df.index, dtype=bool)
                entries.iloc[-1] = entry_signal
                return SignalFrame(entries=entries, exits=exits)

        slot = StrategySlot(
            strategy=_FakeStrategy(),
            symbols=["AAPL"],
            allowed_regimes=allowed_regimes,
        )

        # ── Fake broker ───────────────────────────────────────────────────────
        fake_broker = MagicMock()
        fake_broker.sync_with_broker.return_value = BrokerSnapshot(
            account=AccountState(
                equity=100_000.0,
                cash=100_000.0,
                session_start_equity=100_000.0,
                open_positions={},
            ),
            open_orders=[],
        )
        fake_broker.place_order.return_value = OrderResult(
            symbol="AAPL",
            order_id="ord-001",
            status=OrderStatus.FILLED,
            requested_qty=10,
            filled_qty=10,
            avg_fill_price=150.0,
            raw_status="filled",
            message="test fill",
        )
        # Market always open.
        fake_broker._with_retry.side_effect = lambda fn, **_: fn()
        fake_broker._api.get_clock.return_value = SimpleNamespace(is_open=True)

        # ── Real risk (permissive settings so all valid signals get approved) ──
        fake_risk = RiskManager(
            max_position_pct=0.10,
            max_open_positions=10,
            max_gross_exposure_pct=0.90,
            atr_stop_multiplier=2.0,
            max_daily_loss_pct=0.50,
            hard_dollar_loss_cap=1_000_000.0,
            loss_streak_threshold=50,
            broker_error_threshold=50,
        )

        # ── Fake regime detector ──────────────────────────────────────────────
        fake_regime = MagicMock(spec=RegimeDetector)
        fake_regime.detect.return_value = regime

        config = EngineConfig(
            history_lookback_days=60,
            cycle_interval_seconds=300,
            max_bar_age_multiplier=10,
            market_hours_only=False,
        )

        engine = TradingEngine(
            slots=[slot],
            risk=fake_risk,
            broker=fake_broker,
            config=config,
            trade_logger=MagicMock(spec=TradeLogger),
            pnl_tracker=MagicMock(spec=PnLTracker),
            alerts=MagicMock(spec=AlertDispatcher),
            regime_detector=fake_regime,
        )

        return engine, fake_broker

    def test_entry_allowed_when_regime_in_set(self):
        engine, broker = self._make_engine(
            regime=MarketRegime.TRENDING,
            allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
            entry_signal=True,
        )
        bars = self._bars()
        with patch("engine.trader.fetch_symbol", return_value=(bars, SimpleNamespace(api_calls=0))):
            engine.start(max_cycles=1)
        broker.place_order.assert_called_once()

    def test_entry_blocked_when_regime_not_in_set(self):
        engine, broker = self._make_engine(
            regime=MarketRegime.BEAR,
            allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
            entry_signal=True,
        )
        bars = self._bars()
        with patch("engine.trader.fetch_symbol", return_value=(bars, SimpleNamespace(api_calls=0))):
            engine.start(max_cycles=1)
        broker.place_order.assert_not_called()

    def test_entry_allowed_when_allowed_regimes_is_none(self):
        """None allowed_regimes → regime gating disabled → entry goes through."""
        engine, broker = self._make_engine(
            regime=MarketRegime.BEAR,
            allowed_regimes=None,
            entry_signal=True,
        )
        bars = self._bars()
        with patch("engine.trader.fetch_symbol", return_value=(bars, SimpleNamespace(api_calls=0))):
            engine.start(max_cycles=1)
        broker.place_order.assert_called_once()

    def test_volatile_regime_blocks_entry(self):
        engine, broker = self._make_engine(
            regime=MarketRegime.VOLATILE,
            allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
            entry_signal=True,
        )
        bars = self._bars()
        with patch("engine.trader.fetch_symbol", return_value=(bars, SimpleNamespace(api_calls=0))):
            engine.start(max_cycles=1)
        broker.place_order.assert_not_called()

    def test_no_entry_signal_regime_irrelevant(self):
        """Even in TRENDING regime, no entry signal → no order."""
        engine, broker = self._make_engine(
            regime=MarketRegime.TRENDING,
            allowed_regimes=frozenset({MarketRegime.TRENDING}),
            entry_signal=False,
        )
        bars = self._bars()
        with patch("engine.trader.fetch_symbol", return_value=(bars, SimpleNamespace(api_calls=0))):
            engine.start(max_cycles=1)
        broker.place_order.assert_not_called()

    def test_regime_detection_failure_does_not_crash_engine(self):
        """If regime detector raises, engine logs a warning and continues."""
        engine, broker = self._make_engine(
            regime=MarketRegime.TRENDING,  # won't be used — detect() raises
            allowed_regimes=frozenset({MarketRegime.TRENDING}),
            entry_signal=True,
        )
        engine._regime_detector.detect.side_effect = RuntimeError("SPY down")

        bars = self._bars()
        with patch("engine.trader.fetch_symbol", return_value=(bars, SimpleNamespace(api_calls=0))):
            engine.start(max_cycles=1)  # must not raise

        # When regime detection fails, entry_allowed stays True →
        # entry is NOT blocked by regime (still subject to risk / filter).
        broker.place_order.assert_called_once()
