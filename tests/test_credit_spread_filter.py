"""
Unit tests for strategies.filters.credit_spread.CreditSpreadEdgeFilter
(PLAN.md 11.29). The IV proxy resolver is stubbed so the suite stays offline.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from strategies.filters.credit_spread import CreditSpreadEdgeFilter
from utils.iv_proxy import IVProxyResolver


def _stub_resolver(iv_points: float) -> IVProxyResolver:
    return IVProxyResolver(fetch_fn=lambda ticker: iv_points)


def _frame(closes: list[float]) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex([start + timedelta(days=i) for i in range(len(closes))], tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1 for c in closes],
            "low": [c - 1 for c in closes],
            "close": closes,
            "volume": [1_000] * len(closes),
        },
        index=idx,
    )


def _uptrend(n: int = 60, start: float = 400.0, step: float = 1.0) -> pd.DataFrame:
    """A rising series — the latest close sits above its own SMA."""
    return _frame([start + step * i for i in range(n)])


def _downtrend(n: int = 60, start: float = 500.0, step: float = 1.0) -> pd.DataFrame:
    """A falling series — the latest close sits below its own SMA."""
    return _frame([start - step * i for i in range(n)])


class TestConstruction:
    def test_rejects_unknown_iv_source(self):
        with pytest.raises(ValueError, match="unknown iv_proxy_source"):
            CreditSpreadEdgeFilter(iv_proxy_source="garch", min_iv_proxy=14)


class TestTrendGate:
    def test_uptrend_above_sma_allowed(self):
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            iv_resolver=_stub_resolver(20.0),
        )
        f.set_symbol("SPY")
        decision = f(_uptrend())
        assert decision.latest_allowed is True

    def test_downtrend_below_sma_blocked(self):
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            iv_resolver=_stub_resolver(20.0),
        )
        f.set_symbol("SPY")
        decision = f(_downtrend())
        assert decision.latest_allowed is False
        assert any("downtrend" in r for r in decision.latest_reasons)

    def test_insufficient_history_fails_open_on_trend(self):
        # Fewer bars than the SMA window → trend gate fails open (True).
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            iv_resolver=_stub_resolver(20.0),
        )
        f.set_symbol("SPY")
        decision = f(_frame([400.0, 401.0, 402.0]))
        assert decision.latest_allowed is True


class TestIvGate:
    def test_iv_below_floor_blocks(self):
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            iv_resolver=_stub_resolver(11.0),  # below the 14 floor
        )
        f.set_symbol("SPY")
        decision = f(_uptrend())
        assert decision.latest_allowed is False
        assert any("IV proxy" in r for r in decision.latest_reasons)

    def test_iv_at_floor_allowed(self):
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            iv_resolver=_stub_resolver(14.0),  # exactly at the floor
        )
        f.set_symbol("SPY")
        decision = f(_uptrend())
        assert decision.latest_allowed is True

    def test_failed_iv_fetch_fails_open(self):
        # Resolver with no cache + failing fetch → falls back to ~15 points,
        # which clears a 14 floor, so the gate degrades to allow.
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: None, fallback_points=15.0),
        )
        f.set_symbol("SPY")
        decision = f(_uptrend())
        assert decision.latest_allowed is True


class TestEarningsGate:
    def test_etf_zero_blackout_skips_earnings_gate(self):
        # earnings_blackout_days=0 → no EarningsBlackout instantiated, no
        # yfinance lookups. Uptrend + good IV → allowed.
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            earnings_blackout_days=0,
            iv_resolver=_stub_resolver(20.0),
        )
        f.set_symbol("QQQ")
        assert f._earnings is None
        decision = f(_uptrend())
        assert decision.latest_allowed is True

    def test_single_name_blackout_instantiates_earnings_filter(self):
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            earnings_blackout_days=3,
            iv_resolver=_stub_resolver(20.0),
        )
        assert f._earnings is not None


class TestCombinedGates:
    def test_all_gates_must_pass(self):
        # Downtrend fails even with good IV.
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            iv_resolver=_stub_resolver(25.0),
        )
        f.set_symbol("SPY")
        assert f(_downtrend()).latest_allowed is False

    def test_empty_frame_allows_all(self):
        f = CreditSpreadEdgeFilter(
            iv_proxy_source="vix", min_iv_proxy=14,
            iv_resolver=_stub_resolver(20.0),
        )
        f.set_symbol("SPY")
        decision = f(_frame([]))
        assert decision.allowed.empty
