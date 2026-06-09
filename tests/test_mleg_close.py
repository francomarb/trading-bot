"""
Unit tests for execution.mleg_close — generic walk-and-market close scheduler.

The scheduler is the core of the walk-and-market PR. These tests pin:

  - Profile resolution: per-instrument → per-strategy → global fallback
  - Decision dataclass validation (rejects unknown reasons)
  - Quote sanity (rejects inverted bid/ask)
  - Step iteration: each step's price is correctly computed
  - Advance + exhaustion semantics
  - Market sentinel handling
  - Profile compilation rejects bad expressions at construction time
"""

from __future__ import annotations

import math

import pytest

from config import settings
from execution.mleg_close import (
    MlegCloseDecision,
    MlegCloseScheduler,
    MlegCloseStep,
    MlegQuote,
    resolve_mleg_close_profile,
)
from utils.safe_expr import UnsafeExpressionError


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def realistic_quote() -> MlegQuote:
    """A spread quote roughly matching Friday QQQ's mid-stress conditions."""
    return MlegQuote(mid=4.60, bid=4.12, ask=5.08)


@pytest.fixture
def stop_loss_profile() -> list[tuple[str, int]]:
    """The production stop_loss profile."""
    return list(settings.MLEG_CLOSE_PROFILES["stop_loss"])


# ── MlegQuote ───────────────────────────────────────────────────────────────


class TestMlegQuote:
    def test_normal_quote_constructs(self):
        q = MlegQuote(mid=4.60, bid=4.12, ask=5.08)
        assert q.mid == 4.60
        assert q.bid == 4.12
        assert q.ask == 5.08

    def test_quote_at_bid_ask_endpoints_allowed(self):
        # mid == bid or mid == ask should be allowed — edge case but
        # legitimate when bid-ask is tight (mid rounds to one or the other).
        MlegQuote(mid=4.12, bid=4.12, ask=5.08)
        MlegQuote(mid=5.08, bid=4.12, ask=5.08)

    def test_inverted_quote_rejected(self):
        with pytest.raises(ValueError, match="bid<=mid<=ask"):
            MlegQuote(mid=4.60, bid=5.10, ask=5.08)  # bid > ask
        with pytest.raises(ValueError, match="bid<=mid<=ask"):
            MlegQuote(mid=10.0, bid=4.12, ask=5.08)  # mid > ask

    def test_as_bindings_matches_safe_expr_signature(self):
        q = MlegQuote(mid=4.60, bid=4.12, ask=5.08)
        bindings = q.as_bindings()
        assert set(bindings.keys()) == {"mid", "bid", "ask"}
        assert all(isinstance(v, float) for v in bindings.values())


# ── MlegCloseDecision ───────────────────────────────────────────────────────


class TestMlegCloseDecision:
    def test_close_decision_with_valid_reason(self):
        d = MlegCloseDecision(
            should_close=True,
            reason="stop_loss",
            detail="stop loss — mid $4.60 ≥ 2× $2.26",
            position_id="pos-001",
            initial_mid=4.60, initial_bid=4.12, initial_ask=5.08,
        )
        assert d.should_close
        assert d.reason == "stop_loss"

    def test_close_decision_unknown_reason_rejected(self):
        with pytest.raises(ValueError, match="unknown reason"):
            MlegCloseDecision(
                should_close=True,
                reason="not_a_real_reason",
                detail="whatever",
                position_id="pos-001",
                initial_mid=4.60, initial_bid=4.12, initial_ask=5.08,
            )

    def test_close_decision_missing_reason_when_closing_rejected(self):
        with pytest.raises(ValueError, match="reason required"):
            MlegCloseDecision(
                should_close=True,
                reason=None,
                detail="",
                position_id="pos-001",
                initial_mid=4.60, initial_bid=4.12, initial_ask=5.08,
            )

    def test_no_close_decision_allows_no_reason(self):
        # When should_close=False, reason is meaningless and not required.
        d = MlegCloseDecision(
            should_close=False, reason=None, detail="",
            position_id="pos-001",
            initial_mid=4.60, initial_bid=4.12, initial_ask=5.08,
        )
        assert not d.should_close
        assert d.reason is None


# ── Profile resolver ────────────────────────────────────────────────────────


class TestProfileResolver:
    def test_global_default_used_when_no_overrides(self):
        profile = resolve_mleg_close_profile(
            reason="stop_loss", strategy_name="credit_spread",
        )
        assert profile == settings.MLEG_CLOSE_PROFILES["stop_loss"]

    def test_instrument_override_wins(self, monkeypatch):
        custom = [("mid", 60), ("ask", 30), ("market", 0)]
        profile = resolve_mleg_close_profile(
            reason="stop_loss", strategy_name="credit_spread",
            instrument_overrides={"stop_loss": custom},
        )
        assert profile == custom

    def test_strategy_override_used_when_no_instrument_override(self, monkeypatch):
        custom = [("mid", 45), ("market", 0)]
        monkeypatch.setattr(
            settings, "MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY",
            {"credit_spread": {"stop_loss": custom}},
        )
        profile = resolve_mleg_close_profile(
            reason="stop_loss", strategy_name="credit_spread",
        )
        assert profile == custom

    def test_partial_instrument_override_falls_through_for_other_reasons(self, monkeypatch):
        custom_stop = [("mid", 60), ("market", 0)]
        # Instrument only overrides stop_loss; time_stop should fall through
        # to the global default.
        sl = resolve_mleg_close_profile(
            reason="stop_loss", strategy_name="credit_spread",
            instrument_overrides={"stop_loss": custom_stop},
        )
        assert sl == custom_stop
        ts = resolve_mleg_close_profile(
            reason="time_stop", strategy_name="credit_spread",
            instrument_overrides={"stop_loss": custom_stop},  # no time_stop key
        )
        assert ts == settings.MLEG_CLOSE_PROFILES["time_stop"]

    def test_unknown_reason_raises(self):
        with pytest.raises(KeyError, match="No MLEG close profile"):
            resolve_mleg_close_profile(
                reason="nonexistent", strategy_name="credit_spread",
            )


# ── MlegCloseScheduler — construction ──────────────────────────────────────


class TestSchedulerConstruction:
    def test_empty_profile_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            MlegCloseScheduler([], reason="stop_loss", position_id="p1")

    def test_invalid_expression_rejected_at_construction(self):
        # Pre-compilation means a typo fails NOW, not during a close.
        bad_profile = [("foobar", 30), ("market", 0)]
        with pytest.raises(UnsafeExpressionError, match="Unknown name"):
            MlegCloseScheduler(bad_profile, reason="stop_loss", position_id="p1")

    def test_construction_with_production_profile_succeeds(self, stop_loss_profile):
        scheduler = MlegCloseScheduler(
            stop_loss_profile, reason="stop_loss", position_id="p1",
        )
        assert scheduler.reason == "stop_loss"
        assert scheduler.position_id == "p1"
        assert scheduler.total_steps == len(stop_loss_profile)
        assert scheduler.current_step_number == 1
        assert not scheduler.exhausted

    def test_has_market_fallback_detected(self, stop_loss_profile):
        # stop_loss profile ends with market.
        s_with = MlegCloseScheduler(
            stop_loss_profile, reason="stop_loss", position_id="p1",
        )
        assert s_with.has_market_fallback
        # profit_target ends with a limit step, NOT market.
        s_without = MlegCloseScheduler(
            list(settings.MLEG_CLOSE_PROFILES["profit_target"]),
            reason="profit_target", position_id="p1",
        )
        assert not s_without.has_market_fallback


# ── MlegCloseScheduler — iteration ──────────────────────────────────────────


class TestSchedulerIteration:
    def test_first_step_is_mid_in_stop_loss_profile(
        self, stop_loss_profile, realistic_quote,
    ):
        s = MlegCloseScheduler(
            stop_loss_profile, reason="stop_loss", position_id="p1",
        )
        step = s.next_step(realistic_quote)
        assert step is not None
        assert step.step_number == 1
        assert step.total_steps == 6  # 5 limit + 1 market
        assert step.price_expr == "mid"
        assert step.limit_price == pytest.approx(4.60)
        assert not step.is_market
        assert step.duration_seconds == 30

    def test_intermediate_step_computes_correct_price(
        self, stop_loss_profile, realistic_quote,
    ):
        s = MlegCloseScheduler(
            stop_loss_profile, reason="stop_loss", position_id="p1",
        )
        s.advance()  # step 1 → 2
        step = s.next_step(realistic_quote)
        # step 2 is "mid + 0.25*(ask-mid)"
        # = 4.60 + 0.25*(5.08-4.60) = 4.60 + 0.12 = 4.72
        assert step.limit_price == pytest.approx(4.72)
        assert step.price_expr == "mid + 0.25*(ask-mid)"

    def test_step_does_not_advance_implicitly(
        self, stop_loss_profile, realistic_quote,
    ):
        # next_step() is pure — calling it twice returns the same step.
        s = MlegCloseScheduler(
            stop_loss_profile, reason="stop_loss", position_id="p1",
        )
        step1 = s.next_step(realistic_quote)
        step2 = s.next_step(realistic_quote)
        assert step1 == step2

    def test_market_step_has_nan_price(self, stop_loss_profile, realistic_quote):
        s = MlegCloseScheduler(
            stop_loss_profile, reason="stop_loss", position_id="p1",
        )
        # Advance to the market step (last one in the profile).
        for _ in range(s.total_steps - 1):
            s.advance()
        step = s.next_step(realistic_quote)
        assert step is not None
        assert step.is_market
        assert math.isnan(step.limit_price)
        assert step.duration_seconds == 0

    def test_advance_past_end_idempotent(
        self, stop_loss_profile, realistic_quote,
    ):
        s = MlegCloseScheduler(
            stop_loss_profile, reason="stop_loss", position_id="p1",
        )
        for _ in range(s.total_steps + 5):
            s.advance()
        assert s.exhausted
        assert s.next_step(realistic_quote) is None

    def test_each_step_in_production_profile_is_monotonic_or_market(
        self, stop_loss_profile, realistic_quote,
    ):
        """Each successive step's price must be ≥ the previous (we're walking UP)."""
        s = MlegCloseScheduler(
            stop_loss_profile, reason="stop_loss", position_id="p1",
        )
        last_price: float | None = None
        for _ in range(s.total_steps):
            step = s.next_step(realistic_quote)
            assert step is not None
            if not step.is_market:
                if last_price is not None:
                    assert step.limit_price >= last_price, (
                        f"non-monotonic walk at step {step.step_number}: "
                        f"{last_price} → {step.limit_price}"
                    )
                last_price = step.limit_price
            s.advance()


# ── Per-exit-reason profile sanity ──────────────────────────────────────────


class TestProductionProfilesSanity:
    """Sanity-check the production profiles against the spec."""

    def test_stop_loss_ends_with_market(self):
        profile = settings.MLEG_CLOSE_PROFILES["stop_loss"]
        assert profile[-1][0] == "market"

    def test_time_stop_ends_with_market(self):
        profile = settings.MLEG_CLOSE_PROFILES["time_stop"]
        assert profile[-1][0] == "market"

    def test_defensive_breach_ends_with_market(self):
        profile = settings.MLEG_CLOSE_PROFILES["defensive_breach"]
        assert profile[-1][0] == "market"

    def test_profit_target_does_not_end_with_market(self):
        # Winners should never auto-escalate to market.
        profile = settings.MLEG_CLOSE_PROFILES["profit_target"]
        assert profile[-1][0] != "market"

    def test_all_profiles_are_compilable(self, realistic_quote):
        # If any production profile breaks construction, settings would have
        # rejected it at import. But test explicitly here so a future
        # config change can't silently weaken that check.
        for reason, profile in settings.MLEG_CLOSE_PROFILES.items():
            s = MlegCloseScheduler(
                list(profile), reason=reason, position_id="p1",
            )
            # Walk through every step — every non-market step should produce
            # a price between bid and ask on a realistic quote.
            for _ in range(s.total_steps):
                step = s.next_step(realistic_quote)
                assert step is not None
                if not step.is_market:
                    assert realistic_quote.bid <= step.limit_price <= realistic_quote.ask, (
                        f"{reason} step {step.step_number} ({step.price_expr}) "
                        f"= {step.limit_price} outside bid={realistic_quote.bid} "
                        f"ask={realistic_quote.ask}"
                    )
                s.advance()

    def test_total_walk_window_fits_within_cycle(self):
        """Sum of non-market step durations must leave buffer in a 300s cycle.

        We don't have a strict bound, but flag if a profile somehow exceeds
        the 300s cycle (would break the once-per-cycle assumption).
        """
        for reason, profile in settings.MLEG_CLOSE_PROFILES.items():
            total = sum(dur for _, dur in profile)
            assert total < 300, (
                f"profile['{reason}'] total duration {total}s ≥ 300s cycle"
            )
