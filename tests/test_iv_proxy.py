"""
Unit tests for utils.iv_proxy — the per-instrument IV proxy data layer
(PLAN.md 11.29 + 11.46). Network I/O is stubbed via the injectable ``fetch_fn``.

The 11.46 refactor widened ``fetch_fn`` from ``(ticker) -> float | None`` to
``(ticker) -> pd.Series | None``. Tests use ``_stub_series(value)`` to wrap a
scalar in a one-element Series for the existing scalar-style fixtures, and
``_series(values)`` to build multi-element series for IV Rank coverage.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pytest

from utils.iv_proxy import (
    IVProxyResolver,
    IVRankSnapshot,
    is_valid_source,
)


# ── helpers ──────────────────────────────────────────────────────────────────


def _stub_series(value: float) -> pd.Series:
    """Wrap a scalar in a one-element Series — matches the widened fetch_fn
    contract while keeping legacy scalar-style fixtures readable."""
    return pd.Series([float(value)], index=[pd.Timestamp(date.today())])


def _series(values: list[float], end: date | None = None) -> pd.Series:
    """Build a daily-indexed Series ending today (or ``end``) from ``values``.
    Used for IV Rank fixtures where the trailing distribution matters."""
    end = end or date.today()
    idx = pd.date_range(end=pd.Timestamp(end), periods=len(values), freq="D")
    return pd.Series([float(v) for v in values], index=idx)


# ── is_valid_source ──────────────────────────────────────────────────────────


class TestIsValidSource:
    def test_known_sources(self) -> None:
        assert is_valid_source("vix") is True
        assert is_valid_source("rvx") is True

    def test_case_insensitive(self) -> None:
        assert is_valid_source("VIX") is True

    def test_unknown_source(self) -> None:
        assert is_valid_source("garch") is False


# ── resolve() — scalar contract (pre-11.46 compatibility) ────────────────────


class TestResolve:
    def test_vix_lookup_returns_points(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: _stub_series(14.7))
        assert resolver.resolve("vix") == pytest.approx(14.7)

    def test_rvx_lookup_returns_points(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: _stub_series(21.3))
        assert resolver.resolve("rvx") == pytest.approx(21.3)

    def test_source_is_case_insensitive(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: _stub_series(16.0))
        assert resolver.resolve("VIX") == pytest.approx(16.0)

    def test_unknown_source_raises(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: _stub_series(15.0))
        with pytest.raises(ValueError, match="unknown IV proxy source"):
            resolver.resolve("garch")

    def test_correct_ticker_is_requested(self) -> None:
        seen: list[str] = []

        def _fetch(ticker: str) -> pd.Series:
            seen.append(ticker)
            return _stub_series(15.0)

        resolver = IVProxyResolver(fetch_fn=_fetch)
        resolver.resolve("vix")
        resolver.resolve("rvx")
        assert seen == ["^VIX", "^RVX"]

    def test_value_is_cached_for_the_day(self) -> None:
        calls = {"n": 0}

        def _fetch(ticker: str) -> pd.Series:
            calls["n"] += 1
            return _stub_series(18.0)

        resolver = IVProxyResolver(fetch_fn=_fetch)
        first = resolver.resolve("vix")
        second = resolver.resolve("vix")
        assert first == second == pytest.approx(18.0)
        assert calls["n"] == 1  # second call served from cache

    def test_failed_fetch_with_no_cache_uses_fallback(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: None, fallback_points=15.0)
        assert resolver.resolve("vix") == pytest.approx(15.0)

    def test_failed_fetch_reuses_stale_cache(self) -> None:
        # Seed a cached value via a successful fetch, then make fetch fail by
        # backdating the cache so the next resolve re-fetches.
        resolver = IVProxyResolver(fetch_fn=lambda ticker: _stub_series(22.0))
        resolver.resolve("vix")
        # Backdate the cache so the next resolve re-fetches.
        resolver._cache["vix"] = (date(2020, 1, 1), _stub_series(22.0))
        resolver._fetch_fn = lambda ticker: None  # now fail
        assert resolver.resolve("vix") == pytest.approx(22.0)

    def test_empty_series_treated_as_failure(self) -> None:
        resolver = IVProxyResolver(
            fetch_fn=lambda ticker: pd.Series([], dtype=float),
            fallback_points=15.0,
        )
        assert resolver.resolve("vix") == pytest.approx(15.0)

    def test_non_positive_prints_are_dropped(self) -> None:
        # Vol index ≤ 0 is bad data; resolver should drop and surface the
        # positive tail.
        s = pd.Series([0.0, -1.0, 17.5], index=pd.date_range(end=pd.Timestamp(date.today()), periods=3, freq="D"))
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        assert resolver.resolve("vix") == pytest.approx(17.5)

    def test_scalar_derivation_pinned_to_last_close(self) -> None:
        # Gemini-review acceptance: resolve() == float(series.iloc[-1]).
        s = _series([10.0, 12.0, 14.0, 16.0, 18.5])
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        assert resolver.resolve("vix") == pytest.approx(18.5)


# ── resolve_rank() — IV Rank + Percentile (11.46) ────────────────────────────


class TestResolveRank:
    def test_returns_iv_rank_snapshot_dataclass(self) -> None:
        s = _series([10.0] + [12.0] * 240)  # 241 days, mixed → well-defined rank
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert isinstance(snap, IVRankSnapshot)
        assert snap.source == "vix"
        assert snap.as_of == date.today()

    def test_endpoint_rank_at_maximum(self) -> None:
        # Series rising monotonically, latest = max → rank = 1.0.
        s = _series(list(range(1, 261)))  # 260 days, latest = 260 = max
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.rank == pytest.approx(1.0)
        assert snap.percentile == pytest.approx(1.0)
        assert snap.sufficient is True

    def test_endpoint_rank_at_minimum(self) -> None:
        # Series falling, latest = min → rank = 0.0.
        s = _series(list(range(260, 0, -1)))
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.rank == pytest.approx(0.0)
        # Percentile = fraction <= current = 1/260 (only itself).
        assert snap.percentile == pytest.approx(1.0 / 260.0)

    def test_midpoint_rank_known_series(self) -> None:
        # min=10, max=30, current=20 → rank=(20-10)/(30-10)=0.5.
        values = [10.0, 30.0] * 130  # 260 days alternating, last=30. Need last=20.
        values = [10.0, 20.0, 30.0] * 86 + [10.0, 20.0]  # length 260, last=20
        s = _series(values)
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.rank == pytest.approx(0.5)
        assert snap.sufficient is True

    def test_percentile_diverges_from_rank_on_skewed_series(self) -> None:
        # 240 quiet days at 10, then 20 spike days at 50, latest = 30.
        # min=10, max=50, current=30 → rank=(30-10)/(50-10)=0.5.
        # percentile = fraction ≤ 30 = 240/261 ≈ 0.92.
        values = [10.0] * 240 + [50.0] * 20 + [30.0]
        s = _series(values)
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.rank == pytest.approx(0.5)
        assert snap.percentile == pytest.approx(241.0 / 261.0)
        # The two metrics disagree by > 0.4 — exactly the point of exposing both.
        assert abs(snap.percentile - snap.rank) > 0.4

    def test_insufficient_history_flag(self) -> None:
        # < 240 trading days → sufficient=False, but rank/percentile still computed.
        s = _series([10.0, 15.0, 20.0])
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.sufficient is False
        assert snap.lookback_days_used == 3
        assert snap.rank == pytest.approx(1.0)  # latest=20=max
        assert snap.percentile == pytest.approx(1.0)

    def test_sufficiency_floor_at_240_days(self) -> None:
        # Exactly 240 trading days → sufficient=True.
        s = _series([float(i) for i in range(1, 241)])
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.lookback_days_used == 240
        assert snap.sufficient is True

    def test_sufficiency_floor_one_below(self) -> None:
        # 239 days → not yet sufficient (Gemini-flagged floor).
        s = _series([float(i) for i in range(1, 240)])
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.lookback_days_used == 239
        assert snap.sufficient is False

    def test_constant_series_returns_none(self) -> None:
        # max == min → no spread to rank against. rank=None, percentile=None,
        # sufficient=False (Gemini-review point D — do not fabricate a 1.0
        # percentile on a degenerate series).
        s = _series([15.0] * 250)
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.rank is None
        assert snap.percentile is None
        assert snap.sufficient is False
        assert snap.lookback_days_used == 250
        assert snap.current == pytest.approx(15.0)

    def test_nan_closes_are_dropped_not_filled(self) -> None:
        # NaN entries dropped; rank computed on the clean series only —
        # no forward-fill which would bias min/max.
        values = [10.0, float("nan"), 12.0, float("nan"), 14.0, 16.0, 20.0]
        s = _series(values)
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        # After dropna: [10, 12, 14, 16, 20]. current=20=max → rank=1.0.
        assert snap.lookback_days_used == 5
        assert snap.rank == pytest.approx(1.0)

    def test_empty_series_returns_fallback_snapshot(self) -> None:
        # No fetch ever succeeded → snapshot reports the fallback and
        # sufficient=False so callers can fail-open or fail-closed as they
        # see fit.
        resolver = IVProxyResolver(
            fetch_fn=lambda ticker: None, fallback_points=15.0
        )
        snap = resolver.resolve_rank("vix")
        assert snap.current == pytest.approx(15.0)
        assert snap.rank is None
        assert snap.percentile is None
        assert snap.lookback_days_used == 0
        assert snap.sufficient is False

    def test_unknown_source_raises_on_rank_too(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: _stub_series(15.0))
        with pytest.raises(ValueError, match="unknown IV proxy source"):
            resolver.resolve_rank("garch")

    def test_resolve_and_resolve_rank_share_cache(self) -> None:
        # One fetch should suffice for both APIs on the same day.
        calls = {"n": 0}

        def _fetch(ticker: str) -> pd.Series:
            calls["n"] += 1
            return _series([10.0, 12.0, 14.0, 18.0])

        resolver = IVProxyResolver(fetch_fn=_fetch)
        scalar = resolver.resolve("vix")
        snap = resolver.resolve_rank("vix")
        assert calls["n"] == 1
        assert scalar == pytest.approx(snap.current)

    def test_failed_fetch_with_stale_cache_still_ranks(self) -> None:
        # Fetch failure + prior cached series → rank computed against the stale
        # series, snapshot reports its actual length.
        seeded = _series([float(i) for i in range(1, 261)])  # 260 days, max=260
        resolver = IVProxyResolver(fetch_fn=lambda ticker: seeded)
        resolver.resolve_rank("vix")
        # Backdate cache so the next call re-fetches; then make fetch fail.
        resolver._cache["vix"] = (date.today() - timedelta(days=2), seeded)
        resolver._fetch_fn = lambda ticker: None
        snap = resolver.resolve_rank("vix")
        assert snap.lookback_days_used == 260
        assert snap.sufficient is True
        assert snap.rank == pytest.approx(1.0)  # latest = 260 = max

    def test_lookback_floor_is_configurable(self) -> None:
        # Tests can tighten the floor to exercise sufficiency on smaller fixtures.
        s = _series([float(i) for i in range(1, 11)])  # 10 days
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s, lookback_floor=10)
        snap = resolver.resolve_rank("vix")
        assert snap.lookback_days_used == 10
        assert snap.sufficient is True


# ── resolve_rank(cache_only=True) — P2 fix (no synchronous fetch in
#    observation paths so a cold cache cannot stall a critical decision) ──────


class TestResolveRankCacheOnly:
    def test_no_fetch_when_cache_empty(self) -> None:
        calls = {"n": 0}

        def _fetch(ticker: str) -> pd.Series:
            calls["n"] += 1
            return _series([10.0, 12.0, 14.0])

        resolver = IVProxyResolver(fetch_fn=_fetch)
        snap = resolver.resolve_rank("vix", cache_only=True)
        # The network path was never taken — the cold-cache snapshot reports
        # an empty lookback and not-sufficient. The fallback current is
        # surfaced so the log line is still well-formed.
        assert calls["n"] == 0
        assert snap.lookback_days_used == 0
        assert snap.sufficient is False
        assert snap.rank is None
        assert snap.percentile is None
        assert snap.current == pytest.approx(15.0)  # fallback

    def test_uses_cached_series_when_present(self) -> None:
        # Warm the cache via a normal call, then assert cache_only=True
        # reuses it without a second fetch.
        calls = {"n": 0}

        def _fetch(ticker: str) -> pd.Series:
            calls["n"] += 1
            return _series([10.0, 12.0, 14.0, 16.0, 20.0])

        resolver = IVProxyResolver(fetch_fn=_fetch, lookback_floor=5)
        resolver.resolve_rank("vix")  # warms cache
        assert calls["n"] == 1
        snap = resolver.resolve_rank("vix", cache_only=True)
        assert calls["n"] == 1  # still no second fetch
        assert snap.current == pytest.approx(20.0)
        assert snap.rank == pytest.approx(1.0)
        assert snap.sufficient is True

    def test_uses_stale_prior_day_cache(self) -> None:
        # A stale prior-day cache is still preferable to triggering a
        # network call from a critical decision path.
        s = _series([float(i) for i in range(1, 11)])
        resolver = IVProxyResolver(
            fetch_fn=lambda ticker: (_ for _ in ()).throw(
                RuntimeError("fetch must not be called")
            ),
            lookback_floor=5,
        )
        resolver._cache["vix"] = (date.today() - timedelta(days=2), s)
        snap = resolver.resolve_rank("vix", cache_only=True)
        # No exception — fetch_fn was never invoked.
        assert snap.lookback_days_used == 10
        assert snap.sufficient is True


# ── as_of derived from series.index[-1] — P3 fix (weekend/holiday/stale
#    cache should report data date, not the bot's wall-clock date) ────────────


class TestAsOfDerivation:
    def test_as_of_reflects_last_close_date_not_today(self) -> None:
        # Series ends two days ago (weekend / holiday scenario).
        end = date.today() - timedelta(days=2)
        s = _series([10.0, 12.0, 14.0], end=end)
        resolver = IVProxyResolver(fetch_fn=lambda ticker: s)
        snap = resolver.resolve_rank("vix")
        assert snap.as_of == end
        assert snap.as_of != date.today()

    def test_as_of_falls_back_to_today_when_no_series(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: None)
        snap = resolver.resolve_rank("vix")
        assert snap.as_of == date.today()

    def test_as_of_stale_cache_reports_stale_data_date(self) -> None:
        # Stale series cached, fetch fails on next call → snapshot's as_of
        # reflects the stale series's last index, not today.
        stale_end = date.today() - timedelta(days=5)
        stale_series = _series([10.0, 12.0, 14.0], end=stale_end)
        resolver = IVProxyResolver(fetch_fn=lambda ticker: stale_series)
        resolver.resolve_rank("vix")
        # Backdate the cache so the next call re-fetches, then make fetch fail.
        resolver._cache["vix"] = (
            date.today() - timedelta(days=1),
            stale_series,
        )
        resolver._fetch_fn = lambda ticker: None
        snap = resolver.resolve_rank("vix")
        assert snap.as_of == stale_end


# ── VXN + per-source fallbacks (PLAN 11.57) ─────────────────────────────────


class TestVxnSource:
    """QQQ moved from VIX to VXN on 2026-08-09.

    Evidence, from the ten live QQQ credit spreads: QQQ realized vol ran
    16%→30% across them while VIX FELL 18.4→15.0. The picker priced QQQ
    risk off VIX, so every delta it computed drifted toward more risk
    while reporting less.
    """

    def test_vxn_is_a_known_source(self) -> None:
        assert is_valid_source("vxn") is True
        assert is_valid_source("VXN") is True

    def test_vxn_resolves_to_the_nasdaq_vol_index(self) -> None:
        seen: list[str] = []

        def fetch(ticker: str):
            seen.append(ticker)
            return pd.Series([21.0, 22.5], index=pd.to_datetime(["2026-08-06", "2026-08-07"]))

        assert IVProxyResolver(fetch_fn=fetch).resolve("vxn") == pytest.approx(22.5)
        assert seen == ["^VXN"]

    def test_vix_and_vxn_are_cached_independently(self) -> None:
        """One resolver serves both instruments; a QQQ lookup must not
        return the SPY series or vice versa."""
        def fetch(ticker: str):
            level = {"^VIX": 15.0, "^VXN": 23.0}[ticker]
            return pd.Series([level], index=pd.to_datetime(["2026-08-07"]))

        r = IVProxyResolver(fetch_fn=fetch)
        assert r.resolve("vix") == pytest.approx(15.0)
        assert r.resolve("vxn") == pytest.approx(23.0)
        assert r.resolve("vix") == pytest.approx(15.0)   # cached, not clobbered


class TestPerSourceFallback:
    """A single VIX-scaled fallback understates any index that habitually
    trades above VIX — and understating sigma is precisely what makes the
    picker sell a fatter strike than it believes."""

    def test_vxn_falls_back_higher_than_vix(self) -> None:
        r = IVProxyResolver(fetch_fn=lambda ticker: None)
        assert r.resolve("vix") == pytest.approx(15.0)
        assert r.resolve("vxn") == pytest.approx(19.0)
        assert r.resolve("vxn") > r.resolve("vix")

    def test_explicit_fallback_overrides_every_source(self) -> None:
        """Callers that pass fallback_points mean it — existing tests and
        callers rely on that behaviour."""
        r = IVProxyResolver(fetch_fn=lambda ticker: None, fallback_points=12.0)
        assert r.resolve("vix") == pytest.approx(12.0)
        assert r.resolve("vxn") == pytest.approx(12.0)

    def test_fallback_only_applies_with_no_cache(self) -> None:
        calls = {"n": 0}

        def fetch(ticker: str):
            calls["n"] += 1
            return (
                pd.Series([26.0], index=pd.to_datetime(["2026-08-07"]))
                if calls["n"] == 1 else None
            )

        r = IVProxyResolver(fetch_fn=fetch)
        assert r.resolve("vxn") == pytest.approx(26.0)
        r._cache["vxn"] = (date(2020, 1, 1), r._cache["vxn"][1])   # force stale
        # Stale real data beats a synthetic neutral level.
        assert r.resolve("vxn") == pytest.approx(26.0)
