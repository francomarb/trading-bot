"""
Unit tests for utils.iv_proxy — the per-instrument IV proxy resolver
(PLAN.md 11.29). Network I/O is stubbed via the injectable ``fetch_fn``.
"""

from __future__ import annotations

from datetime import date

import pytest

from utils.iv_proxy import IVProxyResolver, is_valid_source


class TestIsValidSource:
    def test_known_sources(self) -> None:
        assert is_valid_source("vix") is True
        assert is_valid_source("rvx") is True

    def test_case_insensitive(self) -> None:
        assert is_valid_source("VIX") is True

    def test_unknown_source(self) -> None:
        assert is_valid_source("garch") is False


class TestResolve:
    def test_vix_lookup_returns_points(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: 14.7)
        assert resolver.resolve("vix") == pytest.approx(14.7)

    def test_rvx_lookup_returns_points(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: 21.3)
        assert resolver.resolve("rvx") == pytest.approx(21.3)

    def test_source_is_case_insensitive(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: 16.0)
        assert resolver.resolve("VIX") == pytest.approx(16.0)

    def test_unknown_source_raises(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: 15.0)
        with pytest.raises(ValueError, match="unknown IV proxy source"):
            resolver.resolve("garch")

    def test_correct_ticker_is_requested(self) -> None:
        seen: list[str] = []

        def _fetch(ticker: str) -> float:
            seen.append(ticker)
            return 15.0

        resolver = IVProxyResolver(fetch_fn=_fetch)
        resolver.resolve("vix")
        resolver.resolve("rvx")
        assert seen == ["^VIX", "^RVX"]

    def test_value_is_cached_for_the_day(self) -> None:
        calls = {"n": 0}

        def _fetch(ticker: str) -> float:
            calls["n"] += 1
            return 18.0

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
        # forcing a re-fetch with a stale cache date.
        resolver = IVProxyResolver(fetch_fn=lambda ticker: 22.0)
        resolver.resolve("vix")
        # Backdate the cache so the next resolve re-fetches.
        resolver._cache["vix"] = (date(2020, 1, 1), 22.0)
        resolver._fetch_fn = lambda ticker: None  # now fail
        assert resolver.resolve("vix") == pytest.approx(22.0)

    def test_zero_or_negative_fetch_is_treated_as_failure(self) -> None:
        resolver = IVProxyResolver(fetch_fn=lambda ticker: 0.0, fallback_points=15.0)
        assert resolver.resolve("vix") == pytest.approx(15.0)
