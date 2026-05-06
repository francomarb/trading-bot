"""
Unit tests for SPYOptionsReversionStrategy.

Time stop and Delta floor live in inspect_open_positions (they need the OCC
symbol to know the specific contract's expiry).  _raw_signals only emits RSI
entry signals; its exit series is always False.
"""

import re
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from strategies.spy_options_reversion import SPYOptionsReversionStrategy

_ET = ZoneInfo("America/New_York")


# ── helpers ──────────────────────────────────────────────────────────────────

def _make_df(n: int = 30, close: float = 520.0) -> pd.DataFrame:
    idx = pd.date_range("2026-01-02 09:30", periods=n, freq="5min", tz="US/Eastern")
    return pd.DataFrame({"close": [close] * n}, index=idx)


def _occ(underlying: str, expiry: date, call_put: str, strike: float) -> str:
    exp = expiry.strftime("%y%m%d")
    strike_str = f"{int(strike * 1000):08d}"
    return f"{underlying}{exp}{call_put}{strike_str}"


def _position(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol)


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


# ── inspect_open_positions: time stop ─────────────────────────────────────────

class TestTimeStop:
    def _strat(self) -> SPYOptionsReversionStrategy:
        s = SPYOptionsReversionStrategy()
        # Pre-cache VIX so yfinance is never called
        s._vix_date = date.today()
        s._vix_sigma = 0.18
        return s

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
        s = SPYOptionsReversionStrategy()
        s._vix_date = date.today()
        s._vix_sigma = 0.18
        return s

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

    def _run(self, strat, sym, spy_price, delta_val) -> bool:
        _, now_et = self._safe_time_sym()
        pos = _position(sym)
        import sys, unittest.mock as _mock
        fake_bs = _mock.MagicMock()
        fake_bs.BlackScholesCall.return_value.delta.return_value = delta_val
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


# ── VIX cache ─────────────────────────────────────────────────────────────────

class TestVixCache:
    def test_caches_within_same_day(self):
        strat = SPYOptionsReversionStrategy()
        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame({"Close": [20.0]})
            first = strat._fetch_vix()
            second = strat._fetch_vix()
            assert mock_ticker.call_count == 1, "Should only fetch once per day"
        assert abs(first - 0.20) < 1e-6
        assert first == second

    def test_returns_fallback_on_yfinance_error(self):
        strat = SPYOptionsReversionStrategy()
        with patch("yfinance.Ticker", side_effect=RuntimeError("network down")):
            sigma = strat._fetch_vix()
        assert sigma == 0.15  # default fallback

    def test_refreshes_on_new_day(self):
        strat = SPYOptionsReversionStrategy()
        yesterday = date.today() - timedelta(days=1)
        strat._vix_date = yesterday
        strat._vix_sigma = 0.12

        with patch("yfinance.Ticker") as mock_ticker:
            mock_ticker.return_value.history.return_value = pd.DataFrame({"Close": [25.0]})
            sigma = strat._fetch_vix()

        assert abs(sigma - 0.25) < 1e-6, "Should fetch fresh value on new day"
        assert strat._vix_date == date.today()
