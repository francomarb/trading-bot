"""
Unit tests for SPYOptionsReversionStrategy.

Time stop and Delta floor live in inspect_open_positions (they need the OCC
symbol to know the specific contract's expiry).  _raw_signals only emits RSI
entry signals; its exit series is always False.
"""

import re
import sys
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from strategies.base import EdgeFilterDecision
from strategies.filters.spy_options_reversion import SPYOptionsEdgeFilter
from strategies.spy_options_reversion import SPYOptionsConfig, SPYOptionsReversionStrategy
from utils.iv_proxy import IVProxyResolver

_ET = ZoneInfo("America/New_York")


def _stub_iv_resolver(sigma: float) -> IVProxyResolver:
    """Build a stub IVProxyResolver such that ``resolve("vix") / 100.0 == sigma``.

    The strategy's ``_fetch_vix()`` divides the resolver's index-points
    scalar by 100, so injecting points = sigma * 100 makes the strategy see
    the requested decimal sigma without touching yfinance.
    """
    points = sigma * 100.0
    return IVProxyResolver(
        fetch_fn=lambda ticker: pd.Series(
            [points], index=[pd.Timestamp(date.today())]
        )
    )


@pytest.fixture(autouse=True)
def _restore_blackscholes_module():
    """Tests install fake blackscholes modules; restore the real module afterward."""
    original = sys.modules.get("blackscholes")
    yield
    if original is None:
        sys.modules.pop("blackscholes", None)
    else:
        sys.modules["blackscholes"] = original


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_df(n: int = 30, close: float = 520.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-02 09:30", periods=n, freq="5min", tz="US/Eastern")
    return pd.DataFrame({"close": [close] * n}, index=idx)


def _occ(underlying: str, expiry: date, call_put: str, strike: float) -> str:
    exp = expiry.strftime("%y%m%d")
    strike_str = f"{int(strike * 1000):08d}"
    return f"{underlying}{exp}{call_put}{strike_str}"


def _position(
    symbol: str,
    *,
    current_price: float | None = None,
    qty: float | None = None,
    market_value: float | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol,
        current_price=current_price,
        qty=qty,
        market_value=market_value,
    )


# ── _raw_signals ──────────────────────────────────────────────────────────────

class TestRawSignals:
    def test_returns_false_exits_always(self):
        strat = SPYOptionsReversionStrategy()
        df = _make_df(30)
        signals = strat._raw_signals(df)
        assert not signals.exits.any(), "_raw_signals should never emit exit signals"

    def test_too_few_bars_returns_all_false(self):
        strat = SPYOptionsReversionStrategy(rsi_length=14)
        df = _make_df(10)
        signals = strat._raw_signals(df)
        assert not signals.entries.any()
        assert not signals.exits.any()

    def test_rsi_entry_fires_on_recovery_cross(self):
        """Entry fires when RSI crosses 30 upward (prev < 30, current >= 30)."""
        strat = SPYOptionsReversionStrategy(rsi_length=14, rsi_threshold=30)
        # 20 flat bars to warm RSI, then a sharp dip, then recovery
        closes = [520.0] * 20 + [480.0] * 5 + [521.0]
        idx = pd.date_range("2026-01-02 09:30", periods=len(closes), freq="5min", tz="US/Eastern")
        df = pd.DataFrame({"close": closes}, index=idx)
        signals = strat._raw_signals(df)
        # At least one entry should fire during/after the recovery
        assert signals.entries.any()


class TestSPYOptionsEdgeFilterDecision:
    def test_returns_edge_filter_decision_when_allowed(self):
        df = _make_df(30)
        gate = pd.Series(True, index=df.index, dtype=bool)
        edge = SPYOptionsEdgeFilter()
        edge._spy_filter = MagicMock(return_value=gate)
        edge._spy_filter.last_reason = "SPY above all SMAs [100]"

        decision = edge(df)

        assert isinstance(decision, EdgeFilterDecision)
        assert bool(decision.allowed.iloc[-1]) is True
        assert decision.latest_reasons == []

    def test_returns_edge_filter_decision_with_block_reason(self):
        df = _make_df(30)
        gate = pd.Series(False, index=df.index, dtype=bool)
        edge = SPYOptionsEdgeFilter()
        edge._spy_filter = MagicMock(return_value=gate)
        edge._spy_filter.last_reason = "SPY 480.00 ≤ SMA100 500.00"

        decision = edge(df)

        assert isinstance(decision, EdgeFilterDecision)
        assert bool(decision.allowed.iloc[-1]) is False
        assert decision.latest_reasons == [
            "SPY trend gate failed: SPY 480.00 ≤ SMA100 500.00"
        ]

    def test_reports_insufficient_history_reason(self):
        df = _make_df(30)
        edge = SPYOptionsEdgeFilter()
        edge._spy_filter._spy_cache = _make_df(75)
        edge._spy_filter._cache_time = float("inf")

        decision = edge(df)

        assert not decision.latest_allowed
        assert decision.latest_reasons == [
            "SPY trend gate failed: insufficient SPY history for SMA100: "
            "75 bars available, 100 required"
        ]


def _iv_resolver_pct(values) -> IVProxyResolver:
    """Stub resolver whose trailing series yields a controllable VIX percentile.

    ``resolve_rank('vix').percentile`` == fraction of ``values`` ≤ the last
    value; ``sufficient`` == (len(values) >= 240).
    """
    vals = list(values)
    idx = pd.date_range("2025-01-01", periods=len(vals), freq="D")
    return IVProxyResolver(fetch_fn=lambda ticker: pd.Series(vals, index=idx))


# Trailing series presets keyed to the 240-day sufficiency floor.
_VIX_HIGH = [15.0] * 249 + [30.0]   # percentile 1.00, sufficient
_VIX_LOW = [15.0] * 249 + [10.0]    # percentile 0.004, sufficient
_VIX_INSUFFICIENT = [15.0] * 100 + [30.0]  # percentile high but < 240 days


def _edge_with(regime, vix_values, *, spy_ok=True):
    """Build an edge filter with a mocked SPY gate and a stub VIX resolver."""
    edge = SPYOptionsEdgeFilter(iv_resolver=_iv_resolver_pct(vix_values))
    edge._spy_filter = MagicMock(
        side_effect=lambda df: pd.Series(bool(spy_ok), index=df.index, dtype=bool)
    )
    edge._spy_filter.last_reason = "SPY above all SMAs [100]" if spy_ok else "SPY 480 ≤ SMA100 500"
    if regime is not None:
        edge.set_regime(regime)
    return edge


class TestSPYOptionsVixPercentileGate:
    """Gate 2: VIX percentile is enforced only in TRENDING (PLAN 11.46b)."""

    def test_ranging_ignores_low_vix(self):
        # RANGING trades on the SPY gate alone — low VIX does not block.
        decision = _edge_with("ranging", _VIX_LOW)(_make_df(30))
        assert bool(decision.latest_allowed) is True
        assert decision.latest_reasons == []

    def test_trending_allows_high_vix(self):
        decision = _edge_with("trending", _VIX_HIGH)(_make_df(30))
        assert bool(decision.latest_allowed) is True
        assert decision.latest_reasons == []

    def test_trending_blocks_low_vix(self):
        decision = _edge_with("trending", _VIX_LOW)(_make_df(30))
        assert bool(decision.latest_allowed) is False
        assert len(decision.latest_reasons) == 1
        assert "VIX percentile" in decision.latest_reasons[0]

    def test_trending_blocks_insufficient_history_fail_closed(self):
        decision = _edge_with("trending", _VIX_INSUFFICIENT)(_make_df(30))
        assert bool(decision.latest_allowed) is False
        assert "VIX percentile unavailable" in decision.latest_reasons[0]

    def test_no_regime_does_not_enforce_vix(self):
        # Back-compat / offline callers: regime never injected → gate 2 off.
        decision = _edge_with(None, _VIX_LOW)(_make_df(30))
        assert bool(decision.latest_allowed) is True
        assert decision.latest_reasons == []

    def test_spy_gate_failure_blocks_regardless_of_vix(self):
        decision = _edge_with("trending", _VIX_HIGH, spy_ok=False)(_make_df(30))
        assert bool(decision.latest_allowed) is False
        assert any("SPY trend gate failed" in r for r in decision.latest_reasons)

    def test_both_gates_fail_reports_both_reasons(self):
        decision = _edge_with("trending", _VIX_LOW, spy_ok=False)(_make_df(30))
        assert bool(decision.latest_allowed) is False
        reasons = decision.latest_reasons
        assert any("SPY trend gate failed" in r for r in reasons)
        assert any("VIX percentile" in r for r in reasons)

    def test_accepts_marketregime_enum(self):
        from regime.detector import MarketRegime

        decision = _edge_with(MarketRegime.TRENDING, _VIX_LOW)(_make_df(30))
        assert bool(decision.latest_allowed) is False
        assert "VIX percentile" in decision.latest_reasons[0]

    def test_default_threshold_matches_settings_single_source(self):
        from config import settings

        edge = SPYOptionsEdgeFilter(iv_resolver=_iv_resolver_pct(_VIX_HIGH))
        assert edge._min_vix_percentile == settings.SPY_OPTIONS_MIN_VIX_PERCENTILE


class TestInspectSignalsRegimeInjection:
    """BaseStrategy.inspect_signals forwards current_regime to set_regime filters."""

    def test_current_regime_injected_into_edge_filter(self):
        recorded = {}

        class _RecordingFilter:
            last_reason = ""

            def set_regime(self, regime):
                recorded["regime"] = regime

            def __call__(self, df):
                return pd.Series(True, index=df.index, dtype=bool)

        strat = SPYOptionsReversionStrategy(
            rsi_length=14, rsi_threshold=30, edge_filter=_RecordingFilter()
        )
        strat.inspect_signals(_make_df(30), symbol="SPY", current_regime="trending")
        assert recorded.get("regime") == "trending"

    def test_no_regime_leaves_filter_uninjected(self):
        recorded = {}

        class _RecordingFilter:
            last_reason = ""

            def set_regime(self, regime):
                recorded["regime"] = regime

            def __call__(self, df):
                return pd.Series(True, index=df.index, dtype=bool)

        strat = SPYOptionsReversionStrategy(
            rsi_length=14, rsi_threshold=30, edge_filter=_RecordingFilter()
        )
        strat.inspect_signals(_make_df(30), symbol="SPY")  # no current_regime
        assert "regime" not in recorded


class TestSPYOptionsStrategyEdgeFilterIntegration:
    def test_raw_rsi_entry_can_be_vetoed_with_structured_reasons(self):
        closes = [520.0] * 20 + [480.0] * 5 + [521.0]
        idx = pd.date_range(
            "2026-01-02 09:30",
            periods=len(closes),
            freq="5min",
            tz="US/Eastern",
        )
        df = pd.DataFrame({"close": closes}, index=idx)

        gate = pd.Series(False, index=df.index, dtype=bool)
        edge = SPYOptionsEdgeFilter()
        edge._spy_filter = MagicMock(return_value=gate)
        edge._spy_filter.last_reason = "SPY 480.00 ≤ SMA100 500.00"
        strat = SPYOptionsReversionStrategy(
            rsi_length=14,
            rsi_threshold=30,
            edge_filter=edge,
        )

        raw, filtered, edge_allowed, edge_reasons = strat.inspect_signals(df, symbol="SPY")

        assert raw.entries.any()
        assert not filtered.entries.any()
        assert edge_allowed is False
        assert edge_reasons == [
            "SPY trend gate failed: SPY 480.00 ≤ SMA100 500.00"
        ]


class TestBuildOptionExecution:
    def test_passes_notional_cap_through_picker_and_returns_pick_premium(self):
        """Strategy converts notional_cap to the per-contract budget cap
        and trusts the ContractPick.premium that comes back."""
        from utils.options_lookup import ContractPick
        from utils.options_ranker import Candidate, Quote, ScoredPick

        strat = SPYOptionsReversionStrategy()
        occ_symbol = "SPY260521C00730000"
        pick = ContractPick(
            occ_symbol=occ_symbol,
            premium=4.90,
            spread_pct=0.04,
            score=0.85,
            components={
                "strike_proximity": 1.0,
                "spread_quality": 0.20,
                "premium_efficiency": 0.85,
            },
            runners_up=[],
        )

        with patch(
            "strategies.spy_options_reversion.find_best_call",
            return_value=pick,
        ) as mock_picker:
            opt_sym, premium, take_profit, stop_loss = strat.build_option_execution(
                "SPY", 733.71, notional_cap=2_000.0,
            )

        kwargs = mock_picker.call_args.kwargs
        assert kwargs["max_premium_per_contract"] == 2_000.0
        assert callable(kwargs["quote_lookup"])
        assert kwargs["min_dte"] == 14
        assert kwargs["max_dte"] == 28
        assert kwargs["target_delta"] == 0.55
        assert kwargs["target_strike_pct"] == 0.995
        assert opt_sym == occ_symbol
        assert premium == 4.90
        assert take_profit == 14.70
        assert stop_loss == 3.68

    def test_uses_configured_entry_picker_and_exit_multipliers(self):
        from utils.options_lookup import ContractPick

        config = SPYOptionsConfig(
            min_dte=10,
            max_dte=20,
            target_delta=0.45,
            target_strike_pct=1.01,
            take_profit_multiple=2.5,
            stop_loss_multiple=0.70,
        )
        injected_lookup = MagicMock(name="quote_lookup")
        strat = SPYOptionsReversionStrategy(config=config, quote_lookup=injected_lookup)
        pick = ContractPick(
            occ_symbol="SPY260521C00730000",
            premium=4.00,
            spread_pct=0.04,
            score=0.85,
            components={},
            runners_up=[],
        )

        with patch(
            "strategies.spy_options_reversion.find_best_call",
            return_value=pick,
        ) as mock_picker:
            _sym, _premium, take_profit, stop_loss = strat.build_option_execution(
                "SPY", 733.71, notional_cap=2_000.0,
            )

        kwargs = mock_picker.call_args.kwargs
        assert kwargs["min_dte"] == 10
        assert kwargs["max_dte"] == 20
        assert kwargs["target_delta"] == 0.45
        assert kwargs["target_strike_pct"] == 1.01
        assert kwargs["quote_lookup"] is injected_lookup
        assert take_profit == 10.00
        assert stop_loss == 2.80

    def test_rejects_when_no_pick_available(self):
        from strategies.spy_options_reversion import OptionTradeRejected

        strat = SPYOptionsReversionStrategy()
        with patch(
            "strategies.spy_options_reversion.find_best_call",
            return_value=None,
        ):
            with pytest.raises(OptionTradeRejected, match="No tradeable option"):
                strat.build_option_execution(
                    "SPY", 733.71, notional_cap=2_000.0,
                )

    def test_rejects_when_notional_cap_missing(self):
        from strategies.spy_options_reversion import OptionTradeRejected

        strat = SPYOptionsReversionStrategy()
        with pytest.raises(OptionTradeRejected, match="sleeve has no room"):
            strat.build_option_execution(
                "SPY", 733.71, notional_cap=0.0,
            )

    def test_uses_injected_quote_lookup(self):
        """A5 — when an explicit quote_lookup is injected at construction,
        build_option_execution passes it through to the picker without
        instantiating a production OptionHistoricalDataClient."""
        from utils.options_lookup import ContractPick

        injected_lookup = MagicMock(name="injected_quote_lookup")
        strat = SPYOptionsReversionStrategy(quote_lookup=injected_lookup)
        pick = ContractPick(
            occ_symbol="SPY260521C00730000",
            premium=4.90,
            spread_pct=0.04,
            score=0.85,
            components={
                "strike_proximity": 1.0,
                "spread_quality": 0.20,
                "premium_efficiency": 0.85,
            },
            runners_up=[],
        )

        with patch(
            "strategies.spy_options_reversion.find_best_call",
            return_value=pick,
        ) as mock_picker, patch(
            "strategies.spy_options_reversion.build_opra_quote_lookup"
        ) as mock_builder:
            strat.build_option_execution("SPY", 733.71, notional_cap=2_000.0)

        # The injected lookup is used directly; the production builder is not called.
        assert mock_picker.call_args.kwargs["quote_lookup"] is injected_lookup
        mock_builder.assert_not_called()

    def test_lazy_default_quote_lookup_built_once_and_cached(self):
        """A5 — without an injection, the production lookup is built once on
        first use and reused on subsequent calls (was: rebuilt every call)."""
        from utils.options_lookup import ContractPick

        strat = SPYOptionsReversionStrategy()  # no injection
        pick = ContractPick(
            occ_symbol="SPY260521C00730000",
            premium=4.90,
            spread_pct=0.04,
            score=0.85,
            components={
                "strike_proximity": 1.0,
                "spread_quality": 0.20,
                "premium_efficiency": 0.85,
            },
            runners_up=[],
        )
        sentinel_lookup = MagicMock(name="sentinel_quote_lookup")

        with patch(
            "strategies.spy_options_reversion.find_best_call",
            return_value=pick,
        ), patch(
            "strategies.spy_options_reversion.build_opra_quote_lookup",
            return_value=sentinel_lookup,
        ) as mock_builder:
            strat.build_option_execution("SPY", 733.71, notional_cap=2_000.0)
            strat.build_option_execution("SPY", 733.71, notional_cap=2_000.0)

        mock_builder.assert_called_once()
        assert strat._quote_lookup is sentinel_lookup


# ── inspect_open_positions: time stop ─────────────────────────────────────────

class TestTimeStop:
    def _strat(self) -> SPYOptionsReversionStrategy:
        # Stub resolver so the IVR observation log path doesn't need network.
        return SPYOptionsReversionStrategy(iv_resolver=_stub_iv_resolver(0.18))

    def _friday_expiry_two_weeks_out(self) -> date:
        """Return a Friday at least 10 days from today."""
        d = date.today() + timedelta(days=10)
        while d.weekday() != 4:  # 4 = Friday
            d += timedelta(days=1)
        return d

    def test_no_exit_before_expiry_wednesday(self):
        expiry = self._friday_expiry_two_weeks_out()
        expiry_wednesday = expiry - timedelta(days=2)
        # Simulate: it's Tuesday (one day before expiry Wednesday), 4 PM ET
        tuesday = expiry_wednesday - timedelta(days=1)
        now = datetime.combine(tuesday, datetime.min.time().replace(hour=16), tzinfo=_ET)

        strat = self._strat()
        sym = _occ("SPY", expiry, "C", 520.0)
        pos = _position(sym)

        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: now if tz else datetime.now()
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            # Patch blackscholes to return safe delta
            with patch("strategies.spy_options_reversion.BlackScholesCall", create=True) as mock_bs:
                mock_bs.return_value.delta.return_value = 0.55
                # Import patch
                import sys
                fake_bs = MagicMock()
                fake_bs.BlackScholesCall.return_value.delta.return_value = 0.55
                sys.modules.setdefault("blackscholes", fake_bs)
                result = strat.inspect_open_positions(pos, 520.0)
        assert not result, "Should not exit before expiry Wednesday"

    def test_exit_on_expiry_wednesday_after_330(self):
        expiry = self._friday_expiry_two_weeks_out()
        expiry_wednesday = expiry - timedelta(days=2)
        now_et = datetime.combine(
            expiry_wednesday, datetime.min.time().replace(hour=15, minute=35), tzinfo=_ET
        )

        strat = self._strat()
        sym = _occ("SPY", expiry, "C", 520.0)
        pos = _position(sym)

        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            result = strat.inspect_open_positions(pos, 520.0)
        assert result, "Should exit on expiry Wednesday after 3:30 PM ET"

    def test_no_exit_on_expiry_wednesday_before_330(self):
        expiry = self._friday_expiry_two_weeks_out()
        expiry_wednesday = expiry - timedelta(days=2)
        now_et = datetime.combine(
            expiry_wednesday, datetime.min.time().replace(hour=15, minute=25), tzinfo=_ET
        )

        strat = self._strat()
        sym = _occ("SPY", expiry, "C", 520.0)
        pos = _position(sym)

        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            import sys, unittest.mock as _mock
            fake_bs = _mock.MagicMock()
            fake_bs.BlackScholesCall.return_value.delta.return_value = 0.55
            sys.modules.setdefault("blackscholes", fake_bs)
            result = strat.inspect_open_positions(pos, 520.0)
        assert not result, "Should not exit on expiry Wednesday before 3:30 PM ET"

    def test_ignores_put_contracts(self):
        expiry = self._friday_expiry_two_weeks_out()
        sym = _occ("SPY", expiry, "P", 520.0)  # PUT — not a call
        strat = self._strat()
        result = strat.inspect_open_positions(_position(sym), 520.0)
        assert not result, "Should not exit puts (strategy only trades calls)"

    def test_ignores_non_occ_symbol(self):
        strat = self._strat()
        result = strat.inspect_open_positions(_position("SPY"), 520.0)
        assert not result


# ── inspect_open_positions: Delta floor ──────────────────────────────────────

class TestDeltaFloor:
    def _strat_with_cached_vix(self) -> SPYOptionsReversionStrategy:
        return SPYOptionsReversionStrategy(iv_resolver=_stub_iv_resolver(0.18))

    def _safe_time_sym(self) -> tuple[str, datetime]:
        """OCC symbol + a 'now' time safely before expiry Wednesday."""
        expiry = date.today() + timedelta(days=14)
        while expiry.weekday() != 4:
            expiry += timedelta(days=1)
        sym = _occ("SPY", expiry, "C", 520.0)
        # Monday of that week, 10 AM ET — safely before Wednesday 3:30 PM
        monday = expiry - timedelta(days=4)
        now_et = datetime.combine(monday, datetime.min.time().replace(hour=10), tzinfo=_ET)
        return sym, now_et

    def _run(self, strat, sym, spy_price, delta_val, bs_price: float = 10.0) -> bool:
        _, now_et = self._safe_time_sym()
        pos = _position(sym)
        import sys, unittest.mock as _mock
        fake_bs = _mock.MagicMock()
        call_obj = fake_bs.BlackScholesCall.return_value
        call_obj.delta.return_value = delta_val
        call_obj.price = bs_price  # Guard 3 reads call.price as an attribute
        sys.modules["blackscholes"] = fake_bs

        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            return strat.inspect_open_positions(pos, spy_price)

    def test_no_exit_above_floor(self):
        strat = self._strat_with_cached_vix()
        sym, _ = self._safe_time_sym()
        assert not self._run(strat, sym, 520.0, 0.55)

    def test_exit_below_floor(self):
        strat = self._strat_with_cached_vix()
        sym, _ = self._safe_time_sym()
        assert self._run(strat, sym, 520.0, 0.25)

    def test_exit_exactly_at_floor(self):
        strat = self._strat_with_cached_vix()
        sym, _ = self._safe_time_sym()
        # 0.30 is below the floor threshold (delta < 0.30 triggers exit)
        # exactly 0.30 should NOT trigger (condition is strict <)
        assert not self._run(strat, sym, 520.0, 0.30)

    def test_supports_blackscholes_price_method(self):
        strat = self._strat_with_cached_vix()
        sym, now_et = self._safe_time_sym()
        pos = _position(sym)
        import unittest.mock as _mock

        fake_bs = _mock.MagicMock()
        call_obj = fake_bs.BlackScholesCall.return_value
        call_obj.delta.return_value = 0.55
        call_obj.price.return_value = 10.0
        sys.modules["blackscholes"] = fake_bs

        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            assert not strat.inspect_open_positions(pos, 520.0)


# ── inspect_open_positions: trailing stop ────────────────────────────────────

class TestTrailingStop:
    """Guard 3: HWM-based trailing stop — activates after trail_activation_pct gain,
    exits when value drops trail_pct below peak."""

    def _safe_expiry_and_sym(self) -> tuple[date, str, datetime]:
        expiry = date.today() + timedelta(days=14)
        while expiry.weekday() != 4:
            expiry += timedelta(days=1)
        sym = _occ("SPY", expiry, "C", 520.0)
        monday = expiry - timedelta(days=4)
        now_et = datetime.combine(monday, datetime.min.time().replace(hour=10), tzinfo=_ET)
        return expiry, sym, now_et

    def _run_cycle(self, strat, sym, now_et, bs_price: float, delta: float = 0.55) -> bool:
        """Run one engine cycle with the given B-S price and delta."""
        import sys, unittest.mock as _mock
        fake_bs = _mock.MagicMock()
        call_obj = fake_bs.BlackScholesCall.return_value
        call_obj.delta.return_value = delta
        call_obj.price = bs_price
        sys.modules["blackscholes"] = fake_bs

        pos = _position(
            sym,
            current_price=bs_price,
            qty=1,
            market_value=bs_price * 100.0,
        )
        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            return strat.inspect_open_positions(pos, 520.0)

    def _strat(self, activation=0.10, trail=0.15) -> SPYOptionsReversionStrategy:
        return SPYOptionsReversionStrategy(
            trail_activation_pct=activation,
            trail_pct=trail,
            iv_resolver=_stub_iv_resolver(0.18),
        )

    def test_no_exit_before_activation_threshold(self):
        """Value 5% above base — not past 10% activation, so no trail exit."""
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat()
        # First cycle sets base=10.0, HWM=10.0
        result = self._run_cycle(strat, sym, now_et, bs_price=10.0)
        assert not result
        # Second cycle: value at 10.5 (+5%), HWM=10.5 — still below 11.0 (10% activation)
        result = self._run_cycle(strat, sym, now_et, bs_price=10.5)
        assert not result

    def test_no_exit_when_above_trail_floor(self):
        """Value exceeds activation threshold, then stays above trail floor — no exit."""
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat(activation=0.10, trail=0.15)
        # base = 10.0, cycle 1
        self._run_cycle(strat, sym, now_et, bs_price=10.0)
        # HWM = 12.0 — activates (20% above base > 10% threshold)
        self._run_cycle(strat, sym, now_et, bs_price=12.0)
        # Current = 11.0 — trail floor = 12.0 * 0.85 = 10.2. 11.0 > 10.2 → no exit
        result = self._run_cycle(strat, sym, now_et, bs_price=11.0)
        assert not result

    def test_exit_when_below_trail_floor(self):
        """After activation, value drops below HWM × (1 - trail_pct) — exit fires."""
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat(activation=0.10, trail=0.15)
        # base = 10.0
        self._run_cycle(strat, sym, now_et, bs_price=10.0)
        # HWM = 12.0 — activates (20% above base)
        self._run_cycle(strat, sym, now_et, bs_price=12.0)
        # Current = 9.0 — trail floor = 12.0 * 0.85 = 10.2. 9.0 < 10.2 → EXIT
        result = self._run_cycle(strat, sym, now_et, bs_price=9.0)
        assert result

    def test_hwm_state_cleared_after_trail_exit(self):
        """HWM and base dicts are emptied when the trailing stop fires."""
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat(activation=0.10, trail=0.15)
        self._run_cycle(strat, sym, now_et, bs_price=10.0)
        self._run_cycle(strat, sym, now_et, bs_price=12.0)
        self._run_cycle(strat, sym, now_et, bs_price=9.0)  # triggers exit
        assert sym not in strat._position_hwm
        assert sym not in strat._position_base

    def test_hwm_tracks_new_highs(self):
        """HWM updates to the highest value seen across cycles."""
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat()
        self._run_cycle(strat, sym, now_et, bs_price=10.0)
        self._run_cycle(strat, sym, now_et, bs_price=14.0)
        self._run_cycle(strat, sym, now_et, bs_price=12.0)  # pull-back, HWM stays at 14
        assert abs(strat._position_hwm[sym] - 14.0) < 1e-6

    def test_broker_premium_drives_trail_instead_of_theoretical_value(self):
        """A high B-S estimate cannot create or trigger a phantom premium HWM."""
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat(activation=0.10, trail=0.15)
        strat.register_fill(sym, 10.0)

        import sys, unittest.mock as _mock
        fake_bs = _mock.MagicMock()
        call_obj = fake_bs.BlackScholesCall.return_value
        call_obj.delta.return_value = 0.55
        call_obj.price = 20.0
        sys.modules["blackscholes"] = fake_bs

        pos = _position(sym, current_price=10.50, qty=1, market_value=1_050.0)
        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = (
                lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            )
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            result = strat.inspect_open_positions(pos, 520.0)

        assert not result
        assert strat._position_hwm[sym] == pytest.approx(10.50)

    def test_missing_broker_premium_skips_trail_without_mutating_hwm(self):
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat(activation=0.10, trail=0.15)
        strat.restore_trailing_state(
            sym,
            entry_premium=10.0,
            hwm_premium=15.0,
        )

        import sys, unittest.mock as _mock
        fake_bs = _mock.MagicMock()
        call_obj = fake_bs.BlackScholesCall.return_value
        call_obj.delta.return_value = 0.55
        call_obj.price = 20.0
        sys.modules["blackscholes"] = fake_bs

        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = (
                lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            )
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            result = strat.inspect_open_positions(_position(sym), 520.0)

        assert not result
        assert strat._position_hwm[sym] == pytest.approx(15.0)
        assert strat._position_base[sym] == pytest.approx(10.0)

    def test_durable_broker_hwm_triggers_on_current_broker_premium(self):
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat(activation=0.10, trail=0.15)
        strat.restore_trailing_state(
            sym,
            entry_premium=10.0,
            hwm_premium=15.0,
        )

        import sys, unittest.mock as _mock
        fake_bs = _mock.MagicMock()
        call_obj = fake_bs.BlackScholesCall.return_value
        call_obj.delta.return_value = 0.55
        call_obj.price = 16.0
        sys.modules["blackscholes"] = fake_bs

        pos = _position(sym, current_price=12.0, qty=1, market_value=1_200.0)
        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = (
                lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            )
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            result = strat.inspect_open_positions(pos, 520.0)

        assert result
        assert sym not in strat._position_hwm

    def test_not_activated_below_threshold(self):
        """A small gain (< activation) never triggers the trail check."""
        _, sym, now_et = self._safe_expiry_and_sym()
        strat = self._strat(activation=0.20, trail=0.15)  # 20% activation
        self._run_cycle(strat, sym, now_et, bs_price=10.0)
        # 15% gain — below 20% activation, so even a big drop won't trail-exit
        result = self._run_cycle(strat, sym, now_et, bs_price=9.0)
        assert not result  # Guard 3 inactive; SL at -25% would need 7.5, not 9.0

    def test_hwm_state_cleared_after_time_stop(self):
        """HWM state is cleaned up when Guard 1 (time stop) fires."""
        expiry = date.today() + timedelta(days=14)
        while expiry.weekday() != 4:
            expiry += timedelta(days=1)
        sym = _occ("SPY", expiry, "C", 520.0)
        expiry_wednesday = expiry - timedelta(days=2)
        now_et = datetime.combine(
            expiry_wednesday, datetime.min.time().replace(hour=15, minute=35), tzinfo=_ET
        )

        strat = self._strat()
        # Seed HWM state as if position was tracked
        strat._position_hwm[sym] = 12.0
        strat._position_base[sym] = 10.0

        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            result = strat.inspect_open_positions(_position(sym), 520.0)

        assert result  # time stop fired
        assert sym not in strat._position_hwm
        assert sym not in strat._position_base


# ── register_fill: trailing-stop base anchored to fill premium ───────────────

class TestRegisterFill:
    """A3 — register_fill anchors _position_base to the actual fill premium
    so the trailing-stop activation threshold is measured against true cost
    basis, not the first Black-Scholes valuation."""

    def _strat(self) -> SPYOptionsReversionStrategy:
        return SPYOptionsReversionStrategy(
            trail_activation_pct=0.10,
            trail_pct=0.15,
            iv_resolver=_stub_iv_resolver(0.18),
        )

    def test_anchors_base_and_hwm_to_premium(self):
        strat = self._strat()
        strat.register_fill("SPY260618C00520000", 4.20)
        assert strat._position_base["SPY260618C00520000"] == 4.20
        assert strat._position_hwm["SPY260618C00520000"] == 4.20

    def test_preserves_higher_hwm_already_observed(self):
        # Race: inspect_open_positions already saw a higher B-S value before
        # the fill confirmation arrived. The higher HWM must be preserved so
        # the trail floor doesn't regress.
        strat = self._strat()
        strat._position_hwm["SPY260618C00520000"] = 5.10
        strat.register_fill("SPY260618C00520000", 4.20)
        assert strat._position_base["SPY260618C00520000"] == 4.20
        assert strat._position_hwm["SPY260618C00520000"] == 5.10

    def test_restore_trailing_state_rehydrates_without_lowering_hwm(self):
        strat = self._strat()
        strat._position_hwm["SPY260618C00520000"] = 22.00

        strat.restore_trailing_state(
            "SPY260618C00520000",
            entry_premium=12.77,
            hwm_premium=20.16,
        )

        assert strat._position_base["SPY260618C00520000"] == 12.77
        assert strat._position_hwm["SPY260618C00520000"] == 22.00

    def test_ignores_invalid_premium(self):
        strat = self._strat()
        strat.register_fill("SPY260618C00520000", 0.0)
        strat.register_fill("SPY260618C00520000", -1.0)
        strat.register_fill("SPY260618C00520000", None)  # type: ignore[arg-type]
        assert "SPY260618C00520000" not in strat._position_base
        assert "SPY260618C00520000" not in strat._position_hwm

    def test_anchored_base_overrides_lazy_first_bs_seeding(self):
        # End-to-end: with register_fill called before the first cycle, the
        # activation threshold is anchored to the fill premium — not the
        # B-S value observed on cycle 1.
        expiry = date.today() + timedelta(days=14)
        while expiry.weekday() != 4:
            expiry += timedelta(days=1)
        sym = _occ("SPY", expiry, "C", 520.0)
        monday = expiry - timedelta(days=4)
        now_et = datetime.combine(
            monday, datetime.min.time().replace(hour=10), tzinfo=_ET
        )
        strat = self._strat()
        # Real fill at $4.00 — anchor the base.
        strat.register_fill(sym, 4.00)

        # Cycle 1: B-S happens to value at $5.00 (underlying moved post-fill).
        # Without register_fill, base would be lazily set to 5.00 here and
        # the trail wouldn't activate until 5.50. With register_fill, base
        # stays at 4.00 and the trail activates at 4.40.
        import sys, unittest.mock as _mock
        fake_bs = _mock.MagicMock()
        call_obj = fake_bs.BlackScholesCall.return_value
        call_obj.delta.return_value = 0.55
        call_obj.price = 5.00
        sys.modules["blackscholes"] = fake_bs
        with patch("strategies.spy_options_reversion.datetime") as mock_dt:
            mock_dt.now.side_effect = lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
            mock_dt.combine = datetime.combine
            mock_dt.strptime = datetime.strptime
            strat.inspect_open_positions(_position(sym), 520.0)
        assert strat._position_base[sym] == 4.00  # not overwritten by 5.00


# ── _fetch_vix delegation to shared resolver (11.46) ─────────────────────────
#
# The strategy's own VIX cache was removed in 11.46 — caching is now the
# responsibility of the shared ``IVProxyResolver`` and is tested in
# ``tests/test_iv_proxy.py``. The strategy is only responsible for the
# conversion from index points to decimal sigma.


class TestFetchVixDelegation:
    def test_divides_resolver_points_by_100(self):
        strat = SPYOptionsReversionStrategy(iv_resolver=_stub_iv_resolver(0.20))
        assert abs(strat._fetch_vix() - 0.20) < 1e-9

    def test_uses_resolver_fallback_on_failure(self):
        # No prior cache + failing fetch → resolver falls back to 15.0
        # index points → 0.15 sigma.
        resolver = IVProxyResolver(fetch_fn=lambda ticker: None, fallback_points=15.0)
        strat = SPYOptionsReversionStrategy(iv_resolver=resolver)
        assert strat._fetch_vix() == pytest.approx(0.15)


# ── IVR observation logging (11.46) ──────────────────────────────────────────


class TestIVRObservationLogging:
    """The strategy emits a structured SPY_OPTIONS_IVR line at every entry
    and at each exit trigger — pure evidence accumulation, never gates
    behavior. PLAN 11.46b is the paper-watch verdict that consumes this
    evidence base."""

    def _capture_logs(self, monkeypatch):
        from loguru import logger
        records: list[str] = []
        handler_id = logger.add(lambda msg: records.append(str(msg)), level="INFO")
        yield records
        logger.remove(handler_id)

    def test_entry_logs_ivr_with_resolved_rank(self):
        from loguru import logger
        from utils.options_lookup import ContractPick

        records: list[str] = []
        handler_id = logger.add(lambda msg: records.append(str(msg)), level="INFO")
        try:
            # Resolver with enough series to compute a defined rank.
            resolver = IVProxyResolver(
                fetch_fn=lambda ticker: pd.Series(
                    [10.0, 15.0, 20.0],
                    index=pd.date_range(end=pd.Timestamp(date.today()), periods=3, freq="D"),
                ),
                lookback_floor=3,
            )
            # Pre-warm the cache — production flow: the credit-spread filter
            # warms the shared resolver earlier in the cycle, so by the time
            # an entry's `cache_only=True` observation log fires, the cache
            # is populated. We model that here explicitly.
            resolver.resolve_rank("vix")
            strat = SPYOptionsReversionStrategy(iv_resolver=resolver)
            pick = ContractPick(
                occ_symbol="SPY260521C00730000",
                premium=4.90,
                spread_pct=0.04,
                score=0.85,
                components={},
                runners_up=[],
            )
            with patch(
                "strategies.spy_options_reversion.find_best_call",
                return_value=pick,
            ):
                strat.build_option_execution("SPY", 733.71, notional_cap=2_000.0)
        finally:
            logger.remove(handler_id)

        ivr_lines = [r for r in records if "SPY_OPTIONS_IVR" in r]
        assert len(ivr_lines) == 1, ivr_lines
        line = ivr_lines[0]
        assert "entry" in line
        assert "symbol=SPY260521C00730000" in line
        assert "rank=" in line
        assert "percentile=" in line
        assert "current=20.00" in line
        assert "sufficient=True" in line
        # P3 — as_of reflects the data's last close date, not just today.
        assert f"as_of={date.today().isoformat()}" in line

    def test_exit_time_stop_logs_ivr(self):
        from loguru import logger

        records: list[str] = []
        handler_id = logger.add(lambda msg: records.append(str(msg)), level="INFO")
        try:
            strat = SPYOptionsReversionStrategy(iv_resolver=_stub_iv_resolver(0.18))
            expiry = date.today() + timedelta(days=14)
            while expiry.weekday() != 4:
                expiry += timedelta(days=1)
            sym = _occ("SPY", expiry, "C", 520.0)
            # Wednesday of expiry week, 3:35 PM ET — past the time stop.
            expiry_wed = expiry - timedelta(days=2)
            now_et = datetime.combine(
                expiry_wed,
                datetime.min.time().replace(hour=15, minute=35),
                tzinfo=_ET,
            )
            with patch("strategies.spy_options_reversion.datetime") as mock_dt:
                mock_dt.now.side_effect = (
                    lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
                )
                mock_dt.combine = datetime.combine
                mock_dt.strptime = datetime.strptime
                assert strat.inspect_open_positions(_position(sym), 520.0) is True
        finally:
            logger.remove(handler_id)

        ivr_lines = [r for r in records if "SPY_OPTIONS_IVR" in r]
        assert any("exit_time_stop" in line and sym in line for line in ivr_lines)

    def test_time_stop_does_not_trigger_resolver_fetch_on_cold_cache(self):
        """P2 regression — the time-stop exit fires *before* `_fetch_vix`
        warms the resolver cache, so the IVR observation log must use
        ``cache_only=True``. On a cold cache it should log a sufficient=False
        snapshot without invoking the network ``fetch_fn``."""
        from loguru import logger

        calls = {"n": 0}

        def _fetch(ticker: str) -> pd.Series:
            calls["n"] += 1
            return pd.Series(
                [18.0], index=[pd.Timestamp(date.today())]
            )

        records: list[str] = []
        handler_id = logger.add(lambda msg: records.append(str(msg)), level="INFO")
        try:
            resolver = IVProxyResolver(fetch_fn=_fetch)  # cold cache
            strat = SPYOptionsReversionStrategy(iv_resolver=resolver)
            expiry = date.today() + timedelta(days=14)
            while expiry.weekday() != 4:
                expiry += timedelta(days=1)
            sym = _occ("SPY", expiry, "C", 520.0)
            expiry_wed = expiry - timedelta(days=2)
            now_et = datetime.combine(
                expiry_wed,
                datetime.min.time().replace(hour=15, minute=35),
                tzinfo=_ET,
            )
            with patch("strategies.spy_options_reversion.datetime") as mock_dt:
                mock_dt.now.side_effect = (
                    lambda tz=None: now_et if tz == _ET else datetime.now(timezone.utc)
                )
                mock_dt.combine = datetime.combine
                mock_dt.strptime = datetime.strptime
                assert strat.inspect_open_positions(_position(sym), 520.0) is True
        finally:
            logger.remove(handler_id)

        # The critical assertion: no synchronous yfinance fetch landed in the
        # time-stop exit path. The log line still emitted with sufficient=False.
        assert calls["n"] == 0
        ivr_lines = [r for r in records if "SPY_OPTIONS_IVR" in r]
        assert len(ivr_lines) == 1
        assert "exit_time_stop" in ivr_lines[0]
        assert "sufficient=False" in ivr_lines[0]

    def test_resolver_failure_does_not_break_decision_path(self):
        """Defensive try/except: an IVR resolver glitch must never raise into
        the entry path. The strategy still returns a valid pick; the log
        path emits a warning instead of a structured line."""
        from utils.options_lookup import ContractPick

        class _BrokenResolver:
            def resolve(self, source: str) -> float:
                return 15.0  # _fetch_vix path stays clean

            def resolve_rank(self, source: str):
                raise RuntimeError("resolver glitch")

        strat = SPYOptionsReversionStrategy(iv_resolver=_BrokenResolver())
        pick = ContractPick(
            occ_symbol="SPY260521C00730000",
            premium=4.90,
            spread_pct=0.04,
            score=0.85,
            components={},
            runners_up=[],
        )
        with patch(
            "strategies.spy_options_reversion.find_best_call",
            return_value=pick,
        ):
            occ, premium, *_ = strat.build_option_execution(
                "SPY", 733.71, notional_cap=2_000.0
            )
        assert occ == "SPY260521C00730000"
        assert premium == 4.90
