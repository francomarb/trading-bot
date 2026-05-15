"""
Unit tests for strategies.credit_spread (PLAN.md 11.29).

Covers config loading/validation, the permissive base signal, the
per-instance and global position caps, the entry-execution builder
(with find_best_put_spread stubbed), and every exit trigger.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from execution.options_executor import SpreadLeg
from risk.manager import Side
from strategies.credit_spread import (
    CreditSpread,
    CreditSpreadConfig,
    CreditSpreadRejected,
    OpenSpread,
    SpreadExecutionPlan,
)
from utils.iv_proxy import IVProxyResolver
from utils.options_lookup import SpreadPick
from utils.options_ranker import Quote


# ── Helpers ─────────────────────────────────────────────────────────────────

_RAW_SPY = {
    "short_leg_delta": 0.17,
    "spread_width": 10,
    "dte_min": 30,
    "dte_max": 45,
    "iv_proxy_source": "vix",
    "min_iv_proxy": 14,
    "min_credit_pct_of_width": 0.13,
    "max_concurrent_positions": 3,
    "max_per_expiration": 1,
    "min_dte_gap_between_opens": 7,
    "profit_target_pct": 0.50,
    "stop_loss_multiple": 2.0,
    "time_stop_dte": 21,
    "exit_on_short_strike_breach": True,
    "limit_timeout_seconds": 30,
    "earnings_blackout_days": 0,
}


def _config(symbol: str = "SPY", **overrides) -> CreditSpreadConfig:
    raw = {**_RAW_SPY, **overrides}
    return CreditSpreadConfig.from_dict(symbol, raw)


def _stub_quotes(occ_symbols):
    # Not actually used — find_best_put_spread is patched in entry tests.
    return {occ: None for occ in occ_symbols}


def _strategy(config: CreditSpreadConfig | None = None, *, iv_points: float = 18.0):
    return CreditSpread(
        config or _config(),
        iv_resolver=IVProxyResolver(fetch_fn=lambda ticker: iv_points),
        quote_lookup=_stub_quotes,
    )


def _pick(
    *,
    expiration: date,
    short_strike: float = 568.0,
    long_strike: float = 558.0,
    net_credit: float = 1.45,
    short_delta: float = 0.17,
) -> SpreadPick:
    width = short_strike - long_strike
    return SpreadPick(
        short_occ=f"SPY{expiration:%y%m%d}P{int(short_strike * 1000):08d}",
        long_occ=f"SPY{expiration:%y%m%d}P{int(long_strike * 1000):08d}",
        short_strike=short_strike,
        long_strike=long_strike,
        expiration_date=expiration,
        width=width,
        net_credit=net_credit,
        max_loss=(width - net_credit) * 100,
        short_leg_delta=short_delta,
        score=0.7,
        components={"short_delta": 1.0, "net_credit": 0.15,
                    "spread_quality": 0.8, "dte": 0.9},
        runners_up=[],
    )


def _open_spread(
    *,
    position_id: str = "p1",
    expiration: date,
    net_credit: float = 1.45,
    short_strike: float = 568.0,
    opened_at: datetime | None = None,
) -> OpenSpread:
    return OpenSpread(
        position_id=position_id,
        short_occ="SPY_S",
        long_occ="SPY_L",
        short_strike=short_strike,
        long_strike=short_strike - 10,
        expiration_date=expiration,
        net_credit=net_credit,
        width=10.0,
        qty=1,
        opened_at=opened_at or datetime.now(timezone.utc),
    )


def _frame(n: int = 60) -> pd.DataFrame:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    idx = pd.DatetimeIndex([start + timedelta(days=i) for i in range(n)], tz="UTC")
    closes = [400.0 + i for i in range(n)]
    return pd.DataFrame(
        {"open": closes, "high": [c + 1 for c in closes],
         "low": [c - 1 for c in closes], "close": closes,
         "volume": [1_000] * n},
        index=idx,
    )


# ── Config ──────────────────────────────────────────────────────────────────


class TestCreditSpreadConfig:
    def test_from_dict_builds_typed_config(self):
        cfg = CreditSpreadConfig.from_dict("SPY", _RAW_SPY)
        assert cfg.symbol == "SPY"
        assert cfg.short_leg_delta == pytest.approx(0.17)
        assert cfg.spread_width == pytest.approx(10.0)
        assert cfg.dte_min == 30 and cfg.dte_max == 45
        assert cfg.exit_on_short_strike_breach is True

    def test_missing_key_raises_clear_error(self):
        bad = {k: v for k, v in _RAW_SPY.items() if k != "time_stop_dte"}
        with pytest.raises(ValueError, match="missing required key.*time_stop_dte"):
            CreditSpreadConfig.from_dict("SPY", bad)

    def test_real_settings_blocks_load(self):
        from config.settings import CREDIT_SPREAD_INSTRUMENTS
        for symbol, raw in CREDIT_SPREAD_INSTRUMENTS.items():
            cfg = CreditSpreadConfig.from_dict(symbol, raw)
            assert cfg.symbol == symbol

    def test_two_instances_carry_different_params(self):
        spy = _config("SPY", spread_width=10)
        qqq = _config("QQQ", spread_width=15)
        assert spy.spread_width == 10.0
        assert qqq.spread_width == 15.0
        assert spy.symbol == "SPY" and qqq.symbol == "QQQ"


# ── Base signal ─────────────────────────────────────────────────────────────


class TestRawSignals:
    def test_every_bar_is_a_candidate_entry(self):
        strat = _strategy()
        sig = strat._raw_signals(_frame(10))
        assert sig.entries.all()
        assert not sig.exits.any()

    def test_requires_close_column(self):
        strat = _strategy()
        with pytest.raises(ValueError, match="close"):
            strat._raw_signals(pd.DataFrame({"open": [1, 2, 3]}))

    def test_shared_sleeve_name_across_instances(self):
        assert CreditSpread(_config("SPY")).name == "credit_spread"
        assert CreditSpread(_config("QQQ")).name == "credit_spread"


# ── Open-position bookkeeping ───────────────────────────────────────────────


class TestPositionBookkeeping:
    def test_register_and_release(self):
        strat = _strategy()
        exp = date(2026, 6, 18)
        strat.register_spread(_open_spread(position_id="p1", expiration=exp))
        assert len(strat.open_spreads) == 1
        removed = strat.release_spread("p1")
        assert removed is not None and removed.position_id == "p1"
        assert strat.open_spreads == []

    def test_register_is_idempotent_on_position_id(self):
        strat = _strategy()
        exp = date(2026, 6, 18)
        strat.register_spread(_open_spread(position_id="p1", expiration=exp))
        strat.register_spread(_open_spread(position_id="p1", expiration=exp))
        assert len(strat.open_spreads) == 1

    def test_per_instance_isolation(self):
        spy = _strategy(_config("SPY"))
        qqq = _strategy(_config("QQQ"))
        spy.register_spread(_open_spread(position_id="s1", expiration=date(2026, 6, 18)))
        assert len(spy.open_spreads) == 1
        assert qqq.open_spreads == []


# ── Entry execution + caps ──────────────────────────────────────────────────


class TestBuildSpreadExecution:
    _EXP = date.today() + timedelta(days=37)

    def test_happy_path_returns_plan_with_negative_limit(self):
        strat = _strategy()
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=self._EXP, net_credit=1.45),
        ):
            plan = strat.build_spread_execution(745.0, notional_cap=2_000.0)
        assert isinstance(plan, SpreadExecutionPlan)
        assert plan.qty == 1
        # Alpaca MLEG: negative limit = net credit required.
        assert plan.limit_price == pytest.approx(-1.45)
        assert plan.net_credit == pytest.approx(1.45)
        assert len(plan.legs) == 2
        short_leg = next(l for l in plan.legs if l.side is Side.SELL)
        assert short_leg.occ_symbol == plan.short_occ
        assert all(l.opening for l in plan.legs)

    def test_rejects_when_notional_cap_zero(self):
        strat = _strategy()
        with pytest.raises(CreditSpreadRejected, match="no room"):
            strat.build_spread_execution(745.0, notional_cap=0.0)

    def test_rejects_when_no_quote_lookup_wired(self):
        strat = CreditSpread(_config(), iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0))
        with pytest.raises(CreditSpreadRejected, match="no quote_lookup"):
            strat.build_spread_execution(745.0, notional_cap=2_000.0)

    def test_rejects_when_picker_finds_nothing(self):
        strat = _strategy()
        with patch("strategies.credit_spread.find_best_put_spread", return_value=None):
            with pytest.raises(CreditSpreadRejected, match="no tradeable put spread"):
                strat.build_spread_execution(745.0, notional_cap=2_000.0)

    def test_per_instance_concurrent_cap_blocks_entry(self):
        strat = _strategy(_config(max_concurrent_positions=2))
        strat.register_spread(_open_spread(position_id="p1", expiration=date(2026, 7, 1)))
        strat.register_spread(_open_spread(position_id="p2", expiration=date(2026, 8, 1)))
        # Cap is reached before the chain is even queried.
        with patch("strategies.credit_spread.find_best_put_spread") as picker:
            with pytest.raises(CreditSpreadRejected, match="per-instance cap"):
                strat.build_spread_execution(745.0, notional_cap=2_000.0)
            picker.assert_not_called()

    def test_global_cap_blocks_entry(self):
        strat = _strategy()
        with patch("strategies.credit_spread.find_best_put_spread") as picker:
            with pytest.raises(CreditSpreadRejected, match="global cap"):
                strat.build_spread_execution(
                    745.0, notional_cap=2_000.0, total_open_credit_spreads=8,
                )
            picker.assert_not_called()

    def test_max_per_expiration_blocks_after_picker(self):
        strat = _strategy(_config(max_per_expiration=1, min_dte_gap_between_opens=0))
        # Already hold a spread on the exact expiration the picker will return.
        strat.register_spread(_open_spread(position_id="p1", expiration=self._EXP))
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=self._EXP),
        ):
            with pytest.raises(CreditSpreadRejected, match="max_per_expiration"):
                strat.build_spread_execution(745.0, notional_cap=2_000.0)

    def test_dte_stagger_blocks_near_expirations(self):
        strat = _strategy(_config(min_dte_gap_between_opens=7, max_per_expiration=5))
        # Hold a spread expiring 3 days before the picker's pick — inside the gap.
        near = self._EXP - timedelta(days=3)
        strat.register_spread(_open_spread(position_id="p1", expiration=near))
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=self._EXP),
        ):
            with pytest.raises(CreditSpreadRejected, match="DTE stagger"):
                strat.build_spread_execution(745.0, notional_cap=2_000.0)

    def test_dte_stagger_allows_well_spaced_expirations(self):
        strat = _strategy(_config(min_dte_gap_between_opens=7, max_per_expiration=5))
        far = self._EXP - timedelta(days=30)  # well outside the 7d gap
        strat.register_spread(_open_spread(position_id="p1", expiration=far))
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=self._EXP),
        ):
            plan = strat.build_spread_execution(745.0, notional_cap=2_000.0)
        assert plan.expiration_date == self._EXP


# ── Exit triggers ───────────────────────────────────────────────────────────


class TestShouldExitSpread:
    _TODAY = date(2026, 5, 14)

    def _spread(self, **kw) -> OpenSpread:
        defaults = dict(expiration=self._TODAY + timedelta(days=40), net_credit=2.00,
                        short_strike=700.0)
        defaults.update(kw)
        return _open_spread(**defaults)

    def test_profit_target_trigger(self):
        strat = _strategy(_config(profit_target_pct=0.50))
        spread = self._spread(net_credit=2.00)
        # mid decayed to 1.00 = 50% of the 2.00 credit → take profit.
        exit_, reason = strat.should_exit_spread(
            spread, spread_mid=1.00, underlying_close=745.0, today=self._TODAY,
        )
        assert exit_ is True and "profit target" in reason

    def test_stop_loss_trigger(self):
        strat = _strategy(_config(stop_loss_multiple=2.0))
        spread = self._spread(net_credit=2.00)
        # mid ballooned to 4.00 = 2× the credit → stop out.
        exit_, reason = strat.should_exit_spread(
            spread, spread_mid=4.00, underlying_close=745.0, today=self._TODAY,
        )
        assert exit_ is True and "stop loss" in reason

    def test_time_stop_trigger(self):
        strat = _strategy(_config(time_stop_dte=21))
        # Expiration is 20 days out — inside the 21 DTE time stop.
        spread = self._spread(expiration=self._TODAY + timedelta(days=20))
        exit_, reason = strat.should_exit_spread(
            spread, spread_mid=1.80, underlying_close=745.0, today=self._TODAY,
        )
        assert exit_ is True and "time stop" in reason

    def test_short_strike_breach_trigger(self):
        strat = _strategy(_config(exit_on_short_strike_breach=True))
        spread = self._spread(short_strike=700.0)
        exit_, reason = strat.should_exit_spread(
            spread, spread_mid=1.80, underlying_close=699.0, today=self._TODAY,
        )
        assert exit_ is True and "short strike breach" in reason

    def test_short_strike_breach_disabled(self):
        strat = _strategy(_config(exit_on_short_strike_breach=False))
        spread = self._spread(short_strike=700.0)
        exit_, _ = strat.should_exit_spread(
            spread, spread_mid=1.80, underlying_close=699.0, today=self._TODAY,
        )
        assert exit_ is False

    def test_no_trigger_holds_position(self):
        strat = _strategy()
        spread = self._spread(
            expiration=self._TODAY + timedelta(days=40), net_credit=2.00,
            short_strike=700.0,
        )
        # mid 1.80: above the 1.00 profit target, below the 4.00 stop; 40 DTE;
        # underlying well above the short strike → hold.
        exit_, reason = strat.should_exit_spread(
            spread, spread_mid=1.80, underlying_close=745.0, today=self._TODAY,
        )
        assert exit_ is False and reason == ""

    def test_profit_target_takes_precedence_over_time_stop(self):
        # Both conditions true — profit target is checked first.
        strat = _strategy(_config(profit_target_pct=0.50, time_stop_dte=21))
        spread = self._spread(
            expiration=self._TODAY + timedelta(days=10), net_credit=2.00,
        )
        exit_, reason = strat.should_exit_spread(
            spread, spread_mid=0.80, underlying_close=745.0, today=self._TODAY,
        )
        assert exit_ is True and "profit target" in reason


# ── evaluate_spread_exit — engine-facing wrapper (PR 3b) ────────────────────


class TestEvaluateSpreadExit:
    _TODAY = date(2026, 5, 14)

    def _spread(self) -> OpenSpread:
        return _open_spread(
            position_id="p1",
            expiration=self._TODAY + timedelta(days=40),
            net_credit=2.00,
            short_strike=700.0,
        )

    def _quotes(self, short: Quote | None, long: Quote | None):
        def _lookup(occ_symbols):
            return {"SPY_S": short, "SPY_L": long}
        return _lookup

    def test_computes_mid_and_triggers_profit_target(self):
        # short mid 1.50, long mid 0.50 → spread mid 1.00 = 50% of 2.00 credit.
        strat = CreditSpread(
            _config(profit_target_pct=0.50),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(1.45, 1.55), Quote(0.45, 0.55)),
        )
        should_exit, reason, spread_mid = strat.evaluate_spread_exit(
            self._spread(), underlying_close=745.0, today=self._TODAY,
        )
        assert should_exit is True
        assert "profit target" in reason
        assert spread_mid == pytest.approx(1.00)

    def test_no_trigger_returns_false_with_mid(self):
        # spread mid 1.80 — above profit target, below stop, not breached.
        strat = CreditSpread(
            _config(),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(2.45, 2.55), Quote(0.65, 0.75)),
        )
        should_exit, reason, spread_mid = strat.evaluate_spread_exit(
            self._spread(), underlying_close=745.0, today=self._TODAY,
        )
        assert should_exit is False
        assert reason == ""
        assert spread_mid == pytest.approx(1.80)

    def test_missing_leg_quote_holds_position(self):
        # Never exit on missing market data.
        strat = CreditSpread(
            _config(),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(1.45, 1.55), None),
        )
        should_exit, reason, spread_mid = strat.evaluate_spread_exit(
            self._spread(), underlying_close=745.0, today=self._TODAY,
        )
        assert should_exit is False
        assert spread_mid is None

    def test_quote_lookup_exception_holds_position(self):
        def _raising(_):
            raise RuntimeError("OPRA down")

        strat = CreditSpread(
            _config(),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=_raising,
        )
        should_exit, reason, spread_mid = strat.evaluate_spread_exit(
            self._spread(), underlying_close=745.0, today=self._TODAY,
        )
        assert should_exit is False
        assert spread_mid is None

    def test_no_quote_lookup_wired_holds_position(self):
        strat = CreditSpread(
            _config(),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
        )
        should_exit, _, spread_mid = strat.evaluate_spread_exit(
            self._spread(), underlying_close=745.0, today=self._TODAY,
        )
        assert should_exit is False
        assert spread_mid is None
