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
    "trend_sma_buffer_pct": 0.0,
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
        assert cfg.trend_sma_buffer_pct == pytest.approx(0.0)
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
        spy = _config("SPY", spread_width=10, trend_sma_buffer_pct=0.0)
        qqq = _config("QQQ", spread_width=15, trend_sma_buffer_pct=0.01)
        assert spy.spread_width == 10.0
        assert qqq.spread_width == 15.0
        assert spy.trend_sma_buffer_pct == pytest.approx(0.0)
        assert qqq.trend_sma_buffer_pct == pytest.approx(0.01)
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


class TestEvaluateClose:
    """Typed close decision used by the engine's walk-and-market dispatch."""

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

    def test_profit_target_maps_to_typed_reason(self):
        strat = CreditSpread(
            _config(profit_target_pct=0.50),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(1.45, 1.55), Quote(0.45, 0.55)),
        )
        decision = strat.evaluate_close(
            self._spread(), underlying_close=745.0, today=self._TODAY,
        )
        assert decision.should_close is True
        assert decision.reason == "profit_target"
        assert "profit target" in decision.detail
        assert decision.position_id == "p1"
        assert decision.initial_mid == pytest.approx(1.00)
        # Net bid/ask of the spread: short_bid - long_ask, short_ask - long_bid
        assert decision.initial_bid == pytest.approx(1.45 - 0.55)
        assert decision.initial_ask == pytest.approx(1.55 - 0.45)

    def test_stop_loss_maps_to_typed_reason(self):
        # Short 2.45/2.55, Long 0.20/0.30 → spread mid 2.10 = stop at 2.0×1.00 credit.
        strat = CreditSpread(
            _config(stop_loss_multiple=2.0),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(2.45, 2.55), Quote(0.20, 0.30)),
        )
        # Custom spread with net_credit = 1.00 so stop trigger fires.
        spread = _open_spread(
            position_id="p1",
            expiration=self._TODAY + timedelta(days=40),
            net_credit=1.00,
            short_strike=700.0,
        )
        decision = strat.evaluate_close(
            spread, underlying_close=745.0, today=self._TODAY,
        )
        assert decision.reason == "stop_loss"

    def test_time_stop_maps_to_typed_reason(self):
        strat = CreditSpread(
            _config(time_stop_dte=21),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(1.95, 2.05), Quote(0.45, 0.55)),
        )
        spread = _open_spread(
            position_id="p1",
            # 20 days from today — below the 21-DTE threshold.
            expiration=self._TODAY + timedelta(days=20),
            net_credit=2.00,
            short_strike=700.0,
        )
        decision = strat.evaluate_close(
            spread, underlying_close=745.0, today=self._TODAY,
        )
        assert decision.reason == "time_stop"

    def test_short_strike_breach_maps_to_defensive_breach(self):
        # exit_on_short_strike_breach=True; underlying below short strike fires.
        strat = CreditSpread(
            _config(exit_on_short_strike_breach=True),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(1.95, 2.05), Quote(0.45, 0.55)),
        )
        decision = strat.evaluate_close(
            self._spread(),
            underlying_close=699.0,  # below 700 short strike
            today=self._TODAY,
        )
        assert decision.reason == "defensive_breach"

    def test_no_trigger_returns_no_close(self):
        strat = CreditSpread(
            _config(),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(2.45, 2.55), Quote(0.65, 0.75)),
        )
        decision = strat.evaluate_close(
            self._spread(), underlying_close=745.0, today=self._TODAY,
        )
        assert decision.should_close is False
        assert decision.reason is None
        assert decision.detail == ""

    def test_missing_quote_returns_no_close_with_nan_quotes(self):
        # Never close on missing market data.
        strat = CreditSpread(
            _config(),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=self._quotes(Quote(1.45, 1.55), None),
        )
        decision = strat.evaluate_close(
            self._spread(), underlying_close=745.0, today=self._TODAY,
        )
        assert decision.should_close is False
        # NaN sentinel — caller distinguishes from a real 0.0 mid.
        import math
        assert math.isnan(decision.initial_mid)


# ── Pick-time instrumentation (PLAN 11.57) ──────────────────────────────────


class TestPickInputInstrumentation:
    """
    The picker prices strikes against live quotes but labels them with a
    Black-Scholes delta computed from the prior session's close. Neither
    input was recorded, so realised-vs-target delta was only answerable by
    reconstruction. These tests pin the event that makes it answerable from
    logs -- and pin that instrumentation can never break a trade.
    """

    _EXP = date.today() + timedelta(days=37)

    def _capture(
        self,
        strat,
        spot_live,
        *,
        spot_at_decision=745.0,
        short_strike=568.0,
    ):
        """Run a successful pick, returning the credit_spread_pick payload."""
        events: list[dict] = []

        def sink(message):
            extra = message.record["extra"]
            if extra.get("event") == "credit_spread_pick":
                events.append(dict(extra))

        from loguru import logger

        sink_id = logger.add(sink, level="DEBUG")
        try:
            with patch(
                "strategies.credit_spread.find_best_put_spread",
                return_value=_pick(
                    expiration=self._EXP,
                    short_strike=short_strike,
                    long_strike=short_strike - 10,
                    short_delta=0.17,
                ),
            ), patch(
                "data.fetcher.fetch_latest_quote_midpoint",
                return_value=spot_live,
            ):
                strat.build_spread_execution(spot_at_decision, notional_cap=2_000.0)
        finally:
            logger.remove(sink_id)
        return events

    def test_records_both_spots_and_the_vol_input(self):
        events = self._capture(_strategy(iv_points=18.0), spot_live=740.0)
        assert len(events) == 1
        e = events[0]
        # The stale spot the delta estimate actually used...
        assert e["spot_at_decision"] == pytest.approx(745.0)
        # ...and the live spot the quotes were struck against.
        assert e["spot_live"] == pytest.approx(740.0)
        assert e["iv_proxy_source"] == "vix"
        assert e["iv_proxy_points"] == pytest.approx(18.0)
        assert e["target_short_delta"] == pytest.approx(0.17)
        assert e["est_short_delta"] == pytest.approx(0.17)

    def test_spot_drift_is_signed_relative_to_the_decision_spot(self):
        events = self._capture(_strategy(), spot_live=738.55, spot_at_decision=745.0)
        # (738.55 - 745) / 745 = -0.866%
        assert events[0]["spot_drift_pct"] == pytest.approx(-0.8658, abs=1e-3)

    def test_delta_at_live_spot_rises_when_the_underlying_fell(self):
        """
        The whole point of the event: a put's delta grows as spot falls
        toward the strike, so a stale high decision spot understates the
        delta actually sold.

        Compared across two runs of the SAME model rather than against
        ``est_short_delta`` -- that field is whatever the picker returned
        (a fixture constant here), so comparing to it would test the stub.
        """
        strike = 715.0
        unchanged = self._capture(
            _strategy(), spot_live=745.0, spot_at_decision=745.0, short_strike=strike
        )[0]
        fell = self._capture(
            _strategy(), spot_live=700.0, spot_at_decision=745.0, short_strike=strike
        )[0]

        # Same strike, same vol, same DTE — only the live spot differs.
        assert fell["est_short_delta_at_live_spot"] > unchanged["est_short_delta_at_live_spot"]
        # Spot below the strike puts the short leg ITM: |delta| past 0.5.
        assert fell["est_short_delta_at_live_spot"] > 0.5
        # And the decision-spot record is untouched by the live reading.
        assert fell["spot_at_decision"] == pytest.approx(745.0)

    def test_credit_pct_of_width_is_recorded(self):
        events = self._capture(_strategy(), spot_live=745.0)
        # _pick defaults: net_credit 1.45 on a 10-wide spread.
        assert events[0]["credit_pct_of_width"] == pytest.approx(0.145)

    def test_live_spot_unavailable_degrades_to_none_not_an_exception(self):
        events = self._capture(_strategy(), spot_live=None)
        e = events[0]
        assert e["spot_live"] is None
        assert e["spot_drift_pct"] is None
        assert e["est_short_delta_at_live_spot"] is None
        # The stale-spot inputs are still recorded.
        assert e["spot_at_decision"] == pytest.approx(745.0)

    def test_instrumentation_failure_never_blocks_the_trade(self):
        """Observational code must not be able to cost a fill."""
        strat = _strategy()
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=self._EXP),
        ), patch(
            "data.fetcher.fetch_latest_quote_midpoint",
            side_effect=RuntimeError("broker down"),
        ):
            plan = strat.build_spread_execution(745.0, notional_cap=2_000.0)
        assert isinstance(plan, SpreadExecutionPlan)
        assert plan.limit_price == pytest.approx(-1.45)


class TestConcurrencyCapParity:
    """
    The allocator's hard cap and the strategy's global cap are two reads of
    one intent. Documentation that "these match" rots (see
    feedback_single_source_of_truth_params), so settings asserts it at
    import -- this pins that the assertion exists and that the throttle is
    actually in force.
    """

    def test_global_cap_matches_allocator_hard_max(self):
        from config import settings

        assert (
            settings.STRATEGY_ALLOCATIONS["credit_spread"]["hard_max_positions"]
            == settings.MAX_TOTAL_CONCURRENT_CREDIT_SPREADS
        )

    def test_throttle_allows_one_spread_per_instrument(self):
        """Global 2 against per-instance 1 = exactly one open spread each.

        It was 1 while the delta bias was being diagnosed, which made SPY
        and QQQ compete for a single slot. They are testing different
        questions now — SPY has 3 closed trades, QQQ is running the
        0.17Δ-against-VXN hypothesis — so sharing one slot would starve
        both. The global cap must stay at the instrument count so neither
        can crowd the other out.
        """
        from config import settings

        n_instruments = len(settings.CREDIT_SPREAD_INSTRUMENTS)
        assert settings.MAX_TOTAL_CONCURRENT_CREDIT_SPREADS == n_instruments == 2
        for sym, cfg in settings.CREDIT_SPREAD_INSTRUMENTS.items():
            assert cfg["max_concurrent_positions"] == 1, sym

    def test_one_instrument_cannot_take_both_global_slots(self):
        """QQQ holding a spread must not be able to claim SPY's slot.

        The global cap alone would allow it — 2 slots, and nothing in that
        number says who may use them. What prevents it is the per-instance
        cap of 1: each instance counts only its OWN open spreads
        (`self._open_spreads`), so QQQ is refused a second entry while SPY
        is still free to take the remaining slot.
        """
        exp = date.today() + timedelta(days=37)
        # Explicit: the _RAW_SPY helper defaults to 3, production is 1.
        qqq = _strategy(_config("QQQ", spread_width=15, max_concurrent_positions=1))
        qqq.register_spread(_open_spread(position_id="q1", expiration=exp))

        # QQQ tries for a second while one global slot is still open.
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=exp + timedelta(days=14)),
        ), patch("data.fetcher.fetch_latest_quote_midpoint", return_value=740.0):
            with pytest.raises(CreditSpreadRejected, match="per-instance cap"):
                qqq.build_spread_execution(
                    745.0, notional_cap=2_000.0, total_open_spreads=1,
                )

        # SPY, a separate instance, still gets the free slot.
        spy = _strategy(_config("SPY", max_concurrent_positions=1))
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=exp),
        ), patch("data.fetcher.fetch_latest_quote_midpoint", return_value=745.0):
            plan = spy.build_spread_execution(
                745.0, notional_cap=2_000.0, total_open_spreads=1,
            )
        assert isinstance(plan, SpreadExecutionPlan)

    def test_the_second_slot_closes_once_both_instruments_hold_one(self):
        """With both slots used the global cap binds, and the message says
        so — not the per-instance one."""
        exp = date.today() + timedelta(days=37)
        spy = _strategy(_config("SPY", max_concurrent_positions=1))
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=exp),
        ), patch("data.fetcher.fetch_latest_quote_midpoint", return_value=745.0):
            with pytest.raises(CreditSpreadRejected, match="global cap"):
                spy.build_spread_execution(
                    745.0, notional_cap=2_000.0, total_open_spreads=2,
                )

    def test_global_cap_cannot_starve_an_instrument(self):
        """Per-instance caps summing above the global cap means the slower
        instrument gets crowded out — the failure this raise fixed."""
        from config import settings

        per_instance_total = sum(
            c["max_concurrent_positions"]
            for c in settings.CREDIT_SPREAD_INSTRUMENTS.values()
        )
        assert settings.MAX_TOTAL_CONCURRENT_CREDIT_SPREADS >= per_instance_total

    def test_both_instruments_remain_eligible_under_the_throttle(self):
        """
        The throttle caps exposure; it must not silently disable QQQ, which
        is where the delta bias under investigation lives.
        """
        from config import settings

        assert set(settings.CREDIT_SPREAD_INSTRUMENTS) == {"SPY", "QQQ"}
        for cfg in settings.CREDIT_SPREAD_INSTRUMENTS.values():
            assert cfg["max_concurrent_positions"] >= 1


class TestSelectionSpot:
    """PLAN 11.57 step 3 — contract selection must run against the LIVE
    spot, not the prior session's close.

    `underlying_price` is the bar close `_decision_frame` hands a 1Day
    slot. That is correct for the *signal* (the trend gate lives in the
    edge filter and still uses it), but the picker bounds its strike scan
    and computes its Black-Scholes delta from the same number while
    ranking LIVE quotes — so a stale spot describes a market that has
    moved on.

    These assert the value the picker actually receives. The pre-existing
    entry tests stub `find_best_put_spread` and pass either way, which is
    precisely why they cannot cover this.
    """

    _EXP = date.today() + timedelta(days=37)
    _BAR_CLOSE = 745.0

    def _run(self, *, live_spot, bar_close=None):
        """Returns (positional_spot_passed_to_picker, pick_event)."""
        events: list[dict] = []

        def sink(message):
            extra = message.record["extra"]
            if extra.get("event") == "credit_spread_pick":
                events.append(dict(extra))

        from loguru import logger

        sink_id = logger.add(sink, level="DEBUG")
        try:
            with patch(
                "strategies.credit_spread.find_best_put_spread",
                return_value=_pick(expiration=self._EXP),
            ) as picker, patch(
                "data.fetcher.fetch_latest_quote_midpoint",
                **({"side_effect": live_spot}
                   if isinstance(live_spot, Exception)
                   else {"return_value": live_spot}),
            ):
                _strategy().build_spread_execution(
                    self._BAR_CLOSE if bar_close is None else bar_close,
                    notional_cap=2_000.0,
                )
        finally:
            logger.remove(sink_id)
        return picker.call_args.args[1], (events[0] if events else None)

    def test_picker_receives_the_live_spot_not_the_bar_close(self):
        spot, event = self._run(live_spot=738.20)
        assert spot == pytest.approx(738.20)
        assert spot != pytest.approx(self._BAR_CLOSE)
        assert event["spot_used"] == pytest.approx(738.20)
        assert event["spot_source"] == "live_quote"
        # The bar close is still recorded, for the drift measurement.
        assert event["spot_at_decision"] == pytest.approx(self._BAR_CLOSE)

    def test_falls_back_to_the_bar_close_when_no_live_quote(self):
        """A slightly stale spot beats no spread at all."""
        spot, event = self._run(live_spot=None)
        assert spot == pytest.approx(self._BAR_CLOSE)
        assert event["spot_used"] == pytest.approx(self._BAR_CLOSE)
        assert event["spot_source"] == "bar_close_fallback"
        assert event["spot_live"] is None

    def test_quote_lookup_raising_does_not_break_the_entry(self):
        spot, event = self._run(live_spot=RuntimeError("broker down"))
        assert spot == pytest.approx(self._BAR_CLOSE)
        assert event["spot_source"] == "bar_close_fallback"

    @pytest.mark.parametrize("bad", [0.0, -5.0, float("nan")])
    def test_unusable_quote_values_fall_back(self, bad):
        spot, event = self._run(live_spot=bad)
        assert spot == pytest.approx(self._BAR_CLOSE)
        assert event["spot_source"] == "bar_close_fallback"

    def test_only_one_quote_fetch_per_pick(self):
        """Selection and the audit event share one lookup — the earlier
        version fetched a second time purely to log it."""
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=self._EXP),
        ), patch(
            "data.fetcher.fetch_latest_quote_midpoint", return_value=740.0
        ) as quote:
            _strategy().build_spread_execution(745.0, notional_cap=2_000.0)
        assert quote.call_count == 1

    def test_delta_and_strike_bounds_see_the_same_spot(self):
        """The whole point of the fix: one spot drives both halves of
        selection, so the delta label describes the strike that was
        actually scanned."""
        spot, event = self._run(live_spot=700.0)
        assert spot == pytest.approx(700.0)
        # est_short_delta_at_live_spot is computed at the live spot too,
        # so with selection now on that spot the two agree by construction.
        assert event["spot_used"] == pytest.approx(event["spot_live"])


class TestQqqVolProxy:
    """PLAN 11.57 — QQQ risk must be measured against QQQ's volatility.

    Measured across the ten live QQQ credit spreads: QQQ sold 4.61% OTM
    against SPY's 4.44% — nearly the same distance — but carried only
    0.55 sigma of cushion against SPY's 1.09, because QQQ's realized vol
    was 23.0% against SPY's 10.6%. Over the same trades VIX fell
    18.4 -> 15.0 while QQQ vol ran 16% -> 30%.
    """

    def test_qqq_target_delta_returns_to_017_against_the_fixed_proxy(self):
        """Not a revert of PR #80. The number labels "whatever strike my
        model calls a 17% chance", so it points somewhere different once
        the model is fixed: 0.17-with-VIX gave ~0.6-0.8 sigma of real
        cushion (what lost); 0.17-with-VXN gives ~0.95, near SPY's 1.09.

        0.12 against the corrected proxy targets ~1.17 sigma, which the
        ten real fills extrapolate to ~10.7% of width -- under the 13%
        floor, i.e. permanently idle.
        """
        from config.settings import CREDIT_SPREAD_INSTRUMENTS

        assert CREDIT_SPREAD_INSTRUMENTS["QQQ"]["short_leg_delta"] == pytest.approx(0.17)

    def test_target_delta_and_credit_floor_are_mutually_satisfiable(self):
        """The pair that broke QQQ: a delta target so far out that the
        credit floor can never be met is not a conservative setting, it is
        an idle one. Pins that the two are not silently re-set into
        contradiction."""
        from config.settings import CREDIT_SPREAD_INSTRUMENTS

        for sym, cfg in CREDIT_SPREAD_INSTRUMENTS.items():
            assert cfg["short_leg_delta"] >= 0.15, (
                f"{sym}: delta {cfg['short_leg_delta']} is far enough OTM that "
                f"the {cfg['min_credit_pct_of_width']:.0%} credit floor is "
                "unlikely to be reachable — see PLAN 11.57"
            )

    def test_qqq_prices_risk_off_the_nasdaq_vol_index(self):
        from config.settings import CREDIT_SPREAD_INSTRUMENTS

        assert CREDIT_SPREAD_INSTRUMENTS["QQQ"]["iv_proxy_source"] == "vxn"

    def test_spy_still_uses_vix(self):
        """VIX is the correct index for SPY; only QQQ was mismatched."""
        from config.settings import CREDIT_SPREAD_INSTRUMENTS

        assert CREDIT_SPREAD_INSTRUMENTS["SPY"]["iv_proxy_source"] == "vix"

    def test_min_iv_proxy_was_rescaled_with_the_source(self):
        """14 was a VIX level. VXN averaged ~1.24x VIX over 2016-2026, so
        leaving 14 in place would have quietly loosened the premium gate
        rather than preserving it."""
        from config.settings import CREDIT_SPREAD_INSTRUMENTS

        spy = CREDIT_SPREAD_INSTRUMENTS["SPY"]
        qqq = CREDIT_SPREAD_INSTRUMENTS["QQQ"]
        assert spy["min_iv_proxy"] == 14
        assert qqq["min_iv_proxy"] == 17
        assert qqq["min_iv_proxy"] / spy["min_iv_proxy"] == pytest.approx(1.21, abs=0.05)

    def test_every_configured_source_is_resolvable(self):
        """A source name that the resolver does not know would raise at the
        first trade, not at import."""
        from config.settings import CREDIT_SPREAD_INSTRUMENTS
        from utils.iv_proxy import is_valid_source

        for sym, cfg in CREDIT_SPREAD_INSTRUMENTS.items():
            assert is_valid_source(cfg["iv_proxy_source"]), sym

    def test_the_strategy_asks_its_resolver_for_the_configured_source(self):
        """Wiring check: the config value must actually reach resolve()."""
        asked: list[str] = []

        class _Spy(IVProxyResolver):
            def resolve(self, source):           # type: ignore[override]
                asked.append(source)
                return 22.0

        strat = CreditSpread(
            _config("QQQ", iv_proxy_source="vxn", spread_width=15),
            iv_resolver=_Spy(fetch_fn=lambda t: None),
            quote_lookup=_stub_quotes,
        )
        with patch(
            "strategies.credit_spread.find_best_put_spread",
            return_value=_pick(expiration=date.today() + timedelta(days=37)),
        ), patch("data.fetcher.fetch_latest_quote_midpoint", return_value=740.0):
            strat.build_spread_execution(745.0, notional_cap=2_000.0)
        assert asked == ["vxn"]
