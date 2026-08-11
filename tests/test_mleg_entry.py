"""
Unit tests for execution.mleg_entry — the bounded MLEG entry walk.

Every test here exists because removing the bound it covers would let the
bot buy a fill by giving away something it was not supposed to give away.
The walk is on the order-placement path, so each bound is asserted against
the value the *broker would receive*, not against an intermediate.
"""

from __future__ import annotations

import pytest

from config import settings
from execution.mleg_close import MlegQuote
from execution.mleg_entry import (
    EntryWalkBounds,
    MlegEntryWalk,
    resolve_mleg_entry_profile,
)

# The real QQQ pick from 2026-08-10 08:45:53 — width $15, credit $2.01,
# max_loss $1,298, floor 13%. Used so the numbers in these tests are the
# numbers the bot actually saw.
QQQ_WIDTH = 15.0
QQQ_QUOTE = MlegQuote(mid=2.01, bid=1.80, ask=2.22)

# A wide profile that walks the whole way to the bid, so bound tests are
# not silently vacuous just because the default profile stops short.
TO_THE_BID = [("mid", 30), ("mid - 0.5*(mid-bid)", 30), ("bid", 30)]


def bounds(**kw) -> EntryWalkBounds:
    base = dict(
        width=QQQ_WIDTH, qty=1, min_credit_pct_of_width=0.13, max_loss_budget=3322.0
    )
    base.update(kw)
    return EntryWalkBounds(**base)


def walk_all(walk: MlegEntryWalk, quote: MlegQuote):
    """Drive the walk to exhaustion, collecting every offered step."""
    steps = []
    while not walk.exhausted:
        s = walk.next_step(quote)
        if s is None:
            break
        steps.append(s)
        walk.advance()
    return steps


class TestMarketIsStructurallyIllegal:
    """The single most important bound. An unfilled entry costs nothing, so
    there is never a reason to cross the spread to get one. This is enforced
    by type at construction rather than by a runtime check the loop could
    skip."""

    def test_market_step_is_rejected(self):
        with pytest.raises(ValueError, match="not a legal entry step"):
            MlegEntryWalk(
                [("mid", 30), ("market", 0)], bounds=bounds(), position_id="p"
            )

    def test_market_rejected_even_when_it_is_the_only_step(self):
        with pytest.raises(ValueError, match="not a legal entry step"):
            MlegEntryWalk([("market", 0)], bounds=bounds(), position_id="p")

    def test_no_step_can_ever_be_a_market_order(self):
        """The walk's public surface has no way to express 'market' — every
        step carries a finite limit price."""
        w = MlegEntryWalk(TO_THE_BID, bounds=bounds(), position_id="p")
        for s in walk_all(w, QQQ_QUOTE):
            assert s.limit_price == pytest.approx(-s.credit)
            assert s.credit > 0


class TestSelectionFloor:
    def test_walk_never_offers_below_the_credit_floor(self):
        """13% of $15 = $1.95. The bid is $1.80, so a walk to the bid WOULD
        breach the floor if it were unbounded — that is the point of the
        fixture."""
        b = bounds()
        w = MlegEntryWalk(TO_THE_BID, bounds=b, position_id="p")
        steps = walk_all(w, QQQ_QUOTE)
        assert steps, "fixture must produce steps or it proves nothing"
        assert QQQ_QUOTE.bid < b.selection_floor, "fixture must actually bind"
        for s in steps:
            assert s.credit >= b.selection_floor - 1e-9, (
                f"step {s.step_number} offered {s.credit} below floor "
                f"{b.selection_floor}"
            )

    def test_the_floor_rung_is_offered_exactly_not_a_cent_above(self):
        """0.13 × 15.0 is 1.9500000000000002 in binary floating point. A
        naive `credit < floor` comparison after rounding bumps the floor
        rung to 1.96 — deleting the most useful step of the ladder and
        asking for MORE credit at the exact moment we meant to concede."""
        w = MlegEntryWalk(TO_THE_BID, bounds=bounds(), position_id="p")
        credits = [s.credit for s in walk_all(w, QQQ_QUOTE)]
        assert 1.95 in credits, f"floor rung 1.95 never offered; got {credits}"
        assert 1.96 not in credits

    def test_a_floor_above_the_ask_stops_the_walk_entirely(self):
        """If the book will never pay our floor there is no price to walk
        to, and submitting anything is pointless order traffic."""
        w = MlegEntryWalk(
            TO_THE_BID, bounds=bounds(min_credit_pct_of_width=0.90), position_id="p"
        )
        assert w.next_step(QQQ_QUOTE) is None


class TestRiskBudget:
    """Conceding credit RAISES max loss, so the budget approved at decision
    time is stale the moment the walk moves. This is the bound that makes
    'do not overexpose to get a fill' true rather than aspirational."""

    def test_budget_floor_solves_the_max_loss_equation(self):
        b = bounds(max_loss_budget=1300.0)
        # (15 − c) × 100 × 1 <= 1300  =>  c >= 2.00
        assert b.budget_floor == pytest.approx(2.00)

    def test_max_loss_never_exceeds_the_budget_at_any_step(self):
        b = bounds(max_loss_budget=1300.0)
        w = MlegEntryWalk(TO_THE_BID, bounds=b, position_id="p")
        steps = walk_all(w, QQQ_QUOTE)
        assert steps
        for s in steps:
            assert b.max_loss_at(s.credit) <= 1300.0 + 1e-6, (
                f"step {s.step_number} at credit {s.credit} implies max loss "
                f"${b.max_loss_at(s.credit):,.2f} > $1,300 budget"
            )

    def test_budget_binds_when_it_is_tighter_than_selection(self):
        """Selection floor is 1.95; a $1,290 budget demands 2.10. The
        tighter of the two must win — otherwise a generous selection rule
        would silently authorise a budget breach."""
        b = bounds(max_loss_budget=1290.0)
        assert b.selection_floor == pytest.approx(1.95)
        assert b.budget_floor == pytest.approx(2.10)
        assert b.credit_floor == pytest.approx(2.10)
        w = MlegEntryWalk(TO_THE_BID, bounds=b, position_id="p")
        for s in walk_all(w, QQQ_QUOTE):
            assert s.credit >= 2.10 - 1e-9

    def test_selection_binds_when_it_is_tighter_than_budget(self):
        b = bounds(max_loss_budget=99_000.0)
        assert b.credit_floor == pytest.approx(b.selection_floor)

    def test_absent_budget_never_binds(self):
        assert bounds(max_loss_budget=None).budget_floor == float("-inf")

    def test_budget_scales_with_quantity(self):
        """Two contracts double the loss at the same per-share credit, so
        the same dollar budget must demand more credit."""
        one = bounds(qty=1, max_loss_budget=2600.0).budget_floor
        two = bounds(qty=2, max_loss_budget=2600.0).budget_floor
        assert two > one


class TestBidClamp:
    def test_never_offers_below_the_standing_bid(self):
        """Someone is already bidding 1.80; offering 1.70 concedes a dime
        for nothing."""
        b = bounds(min_credit_pct_of_width=0.0)  # take the floor out of the way
        w = MlegEntryWalk(
            [("mid - 3.0*(mid-bid)", 30)], bounds=b, position_id="p"
        )
        step = w.next_step(QQQ_QUOTE)
        assert step.credit == pytest.approx(QQQ_QUOTE.bid)
        assert step.clamp == "bid"


class TestWalkShape:
    def test_concedes_monotonically_downward(self):
        """Each rung must ask for no more than the last, or it is not a
        concession ladder."""
        w = MlegEntryWalk(TO_THE_BID, bounds=bounds(), position_id="p")
        credits = [s.credit for s in walk_all(w, QQQ_QUOTE)]
        assert credits == sorted(credits, reverse=True)

    def test_first_step_is_the_mid_the_single_shot_path_would_have_used(self):
        """The walk must not be a worse opening offer than what it replaces
        — step 1 is exactly today's behaviour."""
        w = MlegEntryWalk(
            resolve_mleg_entry_profile(strategy_name="credit_spread"),
            bounds=bounds(), position_id="p",
        )
        assert w.next_step(QQQ_QUOTE).credit == pytest.approx(QQQ_QUOTE.mid)

    def test_limit_price_is_negated_exactly_once(self):
        """Alpaca MLEG convention: negative limit = net credit. A sign slip
        here submits a debit order that pays to open a credit spread."""
        w = MlegEntryWalk(TO_THE_BID, bounds=bounds(), position_id="p")
        for s in walk_all(w, QQQ_QUOTE):
            assert s.limit_price < 0
            assert s.limit_price == pytest.approx(-s.credit)

    def test_prices_are_whole_cents(self):
        w = MlegEntryWalk(TO_THE_BID, bounds=bounds(), position_id="p")
        for s in walk_all(w, QQQ_QUOTE):
            assert round(s.credit, 2) == s.credit

    def test_exhausted_walk_returns_none(self):
        w = MlegEntryWalk([("mid", 30)], bounds=bounds(), position_id="p")
        assert w.next_step(QQQ_QUOTE) is not None
        w.advance()
        assert w.exhausted
        assert w.next_step(QQQ_QUOTE) is None

    def test_clamp_reports_whether_the_walk_had_room(self):
        """The August picks land ~1 cent above the floor, so every rung
        clamps. That field is how we tell 'the walk isn't helping' apart
        from 'the walk had nowhere to go'."""
        tight = MlegQuote(mid=1.96, bid=1.70, ask=2.10)
        w = MlegEntryWalk(TO_THE_BID, bounds=bounds(), position_id="p")
        clamps = [s.clamp for s in walk_all(w, tight)]
        assert clamps.count("floor") >= 1


class TestConstruction:
    def test_empty_profile_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            MlegEntryWalk([], bounds=bounds(), position_id="p")

    def test_zero_duration_step_rejected(self):
        """A rung nobody can rest on is not a rung."""
        with pytest.raises(ValueError, match="positive duration"):
            MlegEntryWalk([("mid", 0)], bounds=bounds(), position_id="p")

    def test_bad_expression_rejected_at_construction(self):
        with pytest.raises(Exception):
            MlegEntryWalk([("__import__('os')", 30)], bounds=bounds(), position_id="p")

    @pytest.mark.parametrize("bad", [dict(width=0.0), dict(qty=0),
                                     dict(min_credit_pct_of_width=1.0),
                                     dict(min_credit_pct_of_width=-0.1)])
    def test_nonsense_bounds_rejected(self, bad):
        with pytest.raises(ValueError):
            bounds(**bad)


class TestShippedProfile:
    """The profile that will actually run."""

    def test_contains_no_market_step(self):
        assert all(e != "market" for e, _ in settings.MLEG_ENTRY_WALK_PROFILE)

    def test_total_duration_does_not_extend_the_entry_window(self):
        """The walk changes how many prices we offer, NOT how long an entry
        sits live. If this ever exceeds the timeout, entries silently stay
        exposed longer than the single-shot path they replaced."""
        total = sum(d for _, d in settings.MLEG_ENTRY_WALK_PROFILE)
        assert total <= settings.MLEG_ENTRY_WATCH_TIMEOUT_SECONDS

    def test_it_is_constructible_and_walks_down(self):
        w = MlegEntryWalk(
            settings.MLEG_ENTRY_WALK_PROFILE, bounds=bounds(), position_id="p"
        )
        credits = [s.credit for s in walk_all(w, MlegQuote(mid=3.42, bid=3.05, ask=3.79))]
        assert len(credits) == len(settings.MLEG_ENTRY_WALK_PROFILE)
        assert credits[0] > credits[-1], "profile must actually concede"


class TestSettingsValidation:
    def test_a_market_step_in_config_fails_at_import_time(self):
        with pytest.raises(ValueError, match="not a legal entry step"):
            settings._validate_mleg_entry_profile(
                [("mid", 30), ("market", 0)], context="test"
            )

    def test_a_walk_longer_than_the_timeout_is_rejected(self):
        long_profile = [("mid", 120), ("bid", 120)]
        assert sum(d for _, d in long_profile) > settings.MLEG_ENTRY_WATCH_TIMEOUT_SECONDS
        with pytest.raises(ValueError, match="exceeds"):
            settings._validate_mleg_entry_profile(long_profile, context="test")

    def test_zero_duration_rejected_in_config(self):
        with pytest.raises(ValueError, match="positive duration"):
            settings._validate_mleg_entry_profile([("mid", 0)], context="test")


class TestEngineWiring:
    """``TradingEngine._build_entry_walk`` — the seam where the bounds get
    their real values. A walk built with the wrong budget is worse than no
    walk, so these assert the numbers that reach ``EntryWalkBounds``."""

    def _engine(self):
        from engine.trader import TradingEngine
        return TradingEngine.__new__(TradingEngine)  # no __init__; pure method

    def _plan(self, width=15.0, qty=1):
        class P:
            short_occ, long_occ = "QQQ260918P00679000", "QQQ260918P00664000"
        P.width, P.qty = width, qty
        return P

    def _strategy(self, *, floor=0.13, provider=object()):
        class Cfg:
            min_credit_pct_of_width = floor

        class S:
            name = "credit_spread"
            config = Cfg()

            def build_entry_quote_provider(self, plan):
                return provider
        return S()

    def _build(self, engine, strategy, plan, cap):
        return engine._build_entry_walk(
            strategy=strategy, symbol="QQQ", plan=plan,
            position_id="p1", notional_cap=cap,
        )

    def test_sleeve_cap_becomes_the_walk_budget(self):
        """`notional_cap` is handed to the picker as `max_loss_per_position`
        (strategies/credit_spread.py). The walk must be held to the SAME
        number — otherwise conceding credit could push max loss past the
        budget selection was approved against."""
        walk, provider = self._build(
            self._engine(), self._strategy(), self._plan(), 3322.0
        )
        assert walk is not None and provider is not None
        assert walk.bounds.max_loss_budget == pytest.approx(3322.0)

    def test_strategy_credit_floor_becomes_the_selection_floor(self):
        walk, _ = self._build(
            self._engine(), self._strategy(floor=0.20), self._plan(), 3322.0
        )
        assert walk.bounds.min_credit_pct_of_width == pytest.approx(0.20)
        assert walk.bounds.selection_floor == pytest.approx(3.00)  # 0.20 × 15

    def test_plan_width_and_qty_reach_the_bounds(self):
        walk, _ = self._build(
            self._engine(), self._strategy(), self._plan(width=10.0, qty=3), 3322.0
        )
        assert walk.bounds.width == pytest.approx(10.0)
        assert walk.bounds.qty == 3

    def test_disabled_flag_falls_back_to_single_shot(self, monkeypatch):
        monkeypatch.setattr(settings, "MLEG_ENTRY_WALK_ENABLED", False)
        assert self._build(
            self._engine(), self._strategy(), self._plan(), 3322.0
        ) == (None, None)

    def test_strategy_without_the_hook_falls_back(self):
        class Bare:
            name = "iron_condor"
        assert self._build(self._engine(), Bare(), self._plan(), 3322.0) == (None, None)

    def test_missing_credit_floor_falls_back_rather_than_guessing(self):
        """No floor means no bound. The old single-shot path is the safe
        answer; inventing a default would be the unsafe one."""
        class Cfg:
            pass

        class S:
            name = "credit_spread"
            config = Cfg()

            def build_entry_quote_provider(self, plan):
                return object()
        assert self._build(self._engine(), S(), self._plan(), 3322.0) == (None, None)

    def test_a_raising_strategy_falls_back_instead_of_propagating(self):
        class S:
            name = "credit_spread"
            config = type("C", (), {"min_credit_pct_of_width": 0.13})()

            def build_entry_quote_provider(self, plan):
                raise RuntimeError("quote lookup exploded")
        assert self._build(self._engine(), S(), self._plan(), 3322.0) == (None, None)

    def test_nonsense_plan_falls_back_instead_of_building_bad_bounds(self):
        """width=0 would make EntryWalkBounds raise. Falling back is right;
        dispatching with unvalidated bounds would not be."""
        assert self._build(
            self._engine(), self._strategy(), self._plan(width=0.0), 3322.0
        ) == (None, None)

    def test_absent_cap_still_builds_but_with_no_budget_bound(self):
        walk, _ = self._build(self._engine(), self._strategy(), self._plan(), None)
        assert walk is not None
        assert walk.bounds.max_loss_budget is None
        assert walk.bounds.credit_floor == pytest.approx(walk.bounds.selection_floor)


class TestAttemptWindowIsPreserved:
    """Review finding (PR #103): collapsed rungs must not shorten the attempt.

    The executor waits out a rung and THEN cancels, so skipping a
    duplicate-priced rung leaves no live order resting during its slot. A
    180s profile whose rungs 2-3 collapse onto the floor would rest an
    order for only 60s — worse than the single-shot path it replaces, and
    worst in exactly the thin-credit regime that causes the collapse.
    """

    def test_total_hold_equals_the_profile_total_when_rungs_collapse(self):
        """The August case: bid 1.80 under a 1.95 floor, so rungs 2 and 3
        both clamp to 1.95 and merge into rung 2's submit."""
        profile = [("mid", 60), ("mid - 0.34*(mid-bid)", 60), ("mid - 0.67*(mid-bid)", 60)]
        w = MlegEntryWalk(profile, bounds=bounds(), position_id="p")
        steps = walk_all(w, QQQ_QUOTE)
        credits = [s.credit for s in steps]
        assert len(set(credits)) < len(profile), (
            "fixture must actually collapse rungs or it proves nothing"
        )
        assert sum(s.duration_seconds for s in steps) == sum(d for _, d in profile)

    def test_total_hold_is_preserved_when_nothing_collapses(self):
        profile = [("mid", 60), ("mid - 0.34*(mid-bid)", 60), ("mid - 0.67*(mid-bid)", 60)]
        wide = MlegQuote(mid=3.42, bid=3.05, ask=3.79)
        w = MlegEntryWalk(profile, bounds=bounds(), position_id="p")
        steps = walk_all(w, wide)
        assert len(set(s.credit for s in steps)) == 3, "fixture must NOT collapse"
        assert sum(s.duration_seconds for s in steps) == 180

    def test_merged_step_carries_the_combined_hold(self):
        w = MlegEntryWalk(
            [("mid", 60), ("bid", 30), ("bid", 45)], bounds=bounds(), position_id="p"
        )
        first = w.next_step(QQQ_QUOTE)
        assert first.duration_seconds == 60 and first.rungs_merged == 1
        w.advance()
        merged = w.next_step(QQQ_QUOTE)
        assert merged.rungs_merged == 2
        assert merged.duration_seconds == 75, "combined hold of the two clamped rungs"

    def test_advance_consumes_every_merged_rung(self):
        """If advance() only stepped by one, the absorbed rungs would be
        walked again and the same price submitted twice."""
        w = MlegEntryWalk(
            [("bid", 30), ("bid", 30), ("bid", 30)], bounds=bounds(), position_id="p"
        )
        step = w.next_step(QQQ_QUOTE)
        assert step.rungs_merged == 3
        w.advance()
        assert w.exhausted

    def test_every_profile_rung_is_accounted_for_exactly_once(self):
        """Sum of rungs_merged across the walk == number of profile rungs.
        Neither dropped (short window) nor double-counted (long window)."""
        for quote in (QQQ_QUOTE, MlegQuote(mid=3.42, bid=3.05, ask=3.79)):
            profile = settings.MLEG_ENTRY_WALK_PROFILE
            w = MlegEntryWalk(profile, bounds=bounds(), position_id="p")
            steps = walk_all(w, quote)
            assert sum(s.rungs_merged for s in steps) == len(profile), (
                f"rung accounting broken for quote {quote}"
            )
