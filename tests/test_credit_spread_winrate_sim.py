"""
Unit tests for scripts.credit_spread_winrate_sim (PLAN 11.57 step 4).

Covers the pure pieces: pricing helpers, strike selection at a target
delta, the exit ladder and its trigger precedence, and the breakeven
formula. The historical driver is I/O and is exercised by running the
script; nothing here touches the network.
"""

from __future__ import annotations

import pytest

from scripts.credit_spread_winrate_sim import (
    ExitRules,
    breakeven_win_rate,
    put_delta,
    put_price,
    simulate_one,
    spread_mid,
    strike_at_delta,
)


class TestPricing:
    def test_spread_mid_is_positive_and_below_width(self):
        mid = spread_mid(740.0, 700.0, 685.0, 37 / 365, 0.18)
        assert 0 < mid < 15.0

    def test_spread_mid_rises_as_spot_falls(self):
        far = spread_mid(760.0, 700.0, 685.0, 37 / 365, 0.18)
        near = spread_mid(715.0, 700.0, 685.0, 37 / 365, 0.18)
        assert near > far

    def test_spread_mid_rises_with_volatility(self):
        lo = spread_mid(740.0, 700.0, 685.0, 37 / 365, 0.12)
        hi = spread_mid(740.0, 700.0, 685.0, 37 / 365, 0.30)
        assert hi > lo

    def test_put_price_and_delta_agree_on_moneyness(self):
        otm_d = put_delta(740.0, 700.0, 37 / 365, 0.18)
        itm_d = put_delta(690.0, 700.0, 37 / 365, 0.18)
        assert otm_d < 0.5 < itm_d
        assert put_price(690.0, 700.0, 37 / 365, 0.18) > put_price(
            740.0, 700.0, 37 / 365, 0.18
        )


class TestStrikeAtDelta:
    @pytest.mark.parametrize("target", [0.12, 0.17, 0.25])
    def test_selected_strike_is_close_to_the_target_delta(self, target):
        S, T, sig = 740.0, 37 / 365, 0.18
        k = strike_at_delta(S, T, sig, target)
        assert k is not None
        assert put_delta(S, k, T, sig) == pytest.approx(target, abs=0.02)

    def test_lower_target_delta_selects_a_lower_strike(self):
        S, T, sig = 740.0, 37 / 365, 0.18
        assert strike_at_delta(S, T, sig, 0.12) < strike_at_delta(S, T, sig, 0.25)

    def test_strike_is_below_spot_for_a_put_credit_spread(self):
        S = 740.0
        assert strike_at_delta(S, 37 / 365, 0.18, 0.17) < S

    def test_returns_the_nearest_strike_even_for_an_unreachable_target(self):
        """The search returns its best candidate rather than failing — a
        0.49 target on a $1 grid lands just below spot, not at None."""
        k = strike_at_delta(740.0, 37 / 365, 0.18, 0.49)
        assert k is not None and k <= 740.0

    def test_returns_none_only_when_the_search_window_is_empty(self):
        """`None` is a guard for a degenerate floor, not a normal outcome.
        The caller skips the entry on it; pinned so the guard is not
        deleted as unreachable."""
        assert strike_at_delta(740.0, 37 / 365, 0.18, 0.17, floor_pct=1.0) is None


class TestExitLadder:
    _BASE = dict(credit=2.00, short_strike=700.0, long_strike=685.0)

    def _flat(self, spot, n=40, vol=0.18):
        return dict(spot_path=[spot] * n, vol_path=[vol] * n)

    def test_profit_target_fires_when_the_mid_halves(self):
        """Spot well above the strike with time decay -> mid collapses."""
        res = simulate_one(**self._BASE, **self._flat(790.0))
        assert res is not None
        assert res.outcome == "profit_target"
        assert res.pnl > 0

    def test_stop_fires_when_the_mid_doubles(self):
        res = simulate_one(**self._BASE, **self._flat(706.0))
        assert res is not None
        assert res.outcome in ("stop_loss", "defensive_breach")
        assert res.pnl < 0

    def test_profit_target_takes_precedence_over_the_time_stop(self):
        """Both can be satisfiable on the same bar; the ladder's order
        decides which is recorded, and that changes the outcome mix."""
        res = simulate_one(
            **self._BASE,
            **self._flat(800.0),
            rules=ExitRules(profit_target_pct=0.99, time_stop_dte=36),
        )
        assert res.outcome == "profit_target"

    def test_stop_takes_precedence_over_breach(self):
        """A gap through the short strike also doubles the mid. The stop
        is checked first, so it is what gets recorded."""
        res = simulate_one(
            **self._BASE,
            spot_path=[600.0] * 5, vol_path=[0.18] * 5,
        )
        assert res.outcome == "stop_loss"

    def test_breach_can_fire_without_the_stop(self):
        """Disable the stop and the same path exits on the breach rule.

        DTE is still 32 here, well clear of the 21 time stop, so breach is
        genuinely the trigger rather than a mislabelled time stop.
        """
        res = simulate_one(
            **self._BASE,
            spot_path=[698.0] * 5, vol_path=[0.18] * 5,
            rules=ExitRules(stop_loss_multiple=99.0),
        )
        assert res.outcome == "defensive_breach"

    def test_time_stop_outranks_breach_exactly_as_production_does(self):
        """`CreditSpread._classify_exit` checks time_stop BEFORE breach.
        An earlier version of this simulator had them swapped, which could
        mislabel the outcome mix in evidence runs even where P&L matched.
        """
        # Construct a genuine collision: stay ABOVE the strike until the
        # time stop is reachable, then breach on the very bar DTE hits 21.
        # (Sitting under the strike from bar 1 breaches long before the
        # time stop and never tests the precedence at all.)
        above, below = 730.0, 690.0
        spot = [above] * 15 + [below] * 25          # bar 16 -> dte_left == 21
        res = simulate_one(
            **self._BASE,
            spot_path=spot, vol_path=[0.18] * len(spot),
            rules=ExitRules(profit_target_pct=0.01, stop_loss_multiple=99.0),
        )
        assert res.held_days == 16                  # both triggers true here
        assert res.outcome == "time_stop"

        # Same bar, breach disabled -> still the time stop, confirming the
        # collision was real rather than the breach simply never firing.
        res2 = simulate_one(
            **self._BASE,
            spot_path=spot, vol_path=[0.18] * len(spot),
            rules=ExitRules(profit_target_pct=0.01, stop_loss_multiple=99.0,
                            exit_on_short_strike_breach=False),
        )
        assert res2.held_days == 16 and res2.outcome == "time_stop"

    def test_time_stop_fires_when_nothing_else_does(self):
        res = simulate_one(
            **self._BASE,
            **self._flat(730.0),
            rules=ExitRules(profit_target_pct=0.01, stop_loss_multiple=99.0,
                            exit_on_short_strike_breach=False),
        )
        assert res.outcome == "time_stop"
        assert res.held_days == 37 - 21

    def test_pnl_is_credit_minus_exit_mid(self):
        res = simulate_one(**self._BASE, **self._flat(790.0))
        assert res.pnl == pytest.approx(2.00 - res.exit_mid)

    def test_exhausted_path_returns_none_rather_than_scoring(self):
        res = simulate_one(
            **self._BASE,
            spot_path=[730.0] * 2, vol_path=[0.18] * 2,
            rules=ExitRules(profit_target_pct=0.01, stop_loss_multiple=99.0,
                            time_stop_dte=0, exit_on_short_strike_breach=False),
        )
        assert res is None

    def test_non_finite_bars_are_skipped_not_scored(self):
        res = simulate_one(
            **self._BASE,
            spot_path=[float("nan"), 0.0, -1.0, 790.0],
            vol_path=[0.18, 0.18, 0.18, 0.18],
        )
        assert res is not None
        assert res.held_days == 4      # the three bad bars were skipped over


class TestBreakevenWinRate:
    def test_the_live_payoff_needs_two_thirds(self):
        """+0.5C on a win against -1.0C on a loss."""
        assert breakeven_win_rate(0.5, -1.0) == pytest.approx(2 / 3, abs=1e-6)

    def test_symmetric_payoff_needs_half(self):
        assert breakeven_win_rate(1.0, -1.0) == pytest.approx(0.5)

    def test_favourable_payoff_needs_less(self):
        assert breakeven_win_rate(3.0, -1.0) == pytest.approx(0.25)

    def test_degenerate_inputs_do_not_raise(self):
        import math

        assert math.isnan(breakeven_win_rate(0.0, 0.0))
