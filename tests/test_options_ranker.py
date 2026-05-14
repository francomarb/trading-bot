"""
Unit tests for utils.options_ranker — pure logic, no I/O.

Covers the scoring formula, the hard filters (affordability, fatal spread,
invalid quote, premium outlier), and the ordering guarantees of the top
pick relative to runners-up.
"""

from __future__ import annotations

from datetime import date

import pytest

from datetime import timedelta

from utils.options_ranker import (
    Candidate,
    Quote,
    FATAL_SPREAD_PCT,
    PREMIUM_OUTLIER_MULTIPLIER,
    SOFT_SPREAD_PCT,
    STRIKE_TOLERANCE_PCT,
    SpreadCandidate,
    WEIGHT_DTE,
    WEIGHT_NET_CREDIT,
    WEIGHT_PREMIUM_EFF,
    WEIGHT_SHORT_DELTA,
    WEIGHT_SPREAD,
    WEIGHT_SPREAD_QUALITY,
    WEIGHT_STRIKE,
    rank_call_candidates,
    rank_put_spread_candidates,
)


EXPIRY = date(2026, 5, 22)


def _c(symbol: str, strike: float) -> Candidate:
    return Candidate(occ_symbol=symbol, strike=strike, expiration_date=EXPIRY)


class TestQuote:
    def test_mid_and_spread_pct(self):
        q = Quote(bid=10.0, ask=10.40)
        assert q.mid == pytest.approx(10.20)
        assert q.spread_pct == pytest.approx(0.40 / 10.20)

    def test_zero_mid_yields_infinite_spread(self):
        q = Quote(bid=0.0, ask=0.0)
        assert q.spread_pct == float("inf")


class TestScoringFormula:
    def test_perfect_strike_zero_spread_essentially_full_score(self):
        # Strike == target, zero spread, premium = 1% of budget (cheap).
        cands = [_c("X", 100.0)]
        quotes = {"X": Quote(bid=1.00, ask=1.00)}
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        assert result.best is not None
        # All three sub-scores are at-or-near 1.0 → composite very close to 1.0
        # (premium efficiency is 1 − 100/10000 = 0.99).
        assert result.best.score == pytest.approx(1.0, abs=0.01)
        # Components sanity:
        comps = result.best.components
        assert comps["strike_proximity"] == pytest.approx(1.0)
        assert comps["spread_quality"] == pytest.approx(1.0)
        assert comps["premium_efficiency"] == pytest.approx(0.99)

    def test_strike_proximity_dominates_when_tied_otherwise(self):
        cands = [_c("FAR", 105.0), _c("NEAR", 100.0)]
        quotes = {
            "FAR": Quote(bid=2.00, ask=2.02),
            "NEAR": Quote(bid=2.00, ask=2.02),
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        assert result.best.occ_symbol == "NEAR"

    def test_tight_spread_beats_wide_spread_at_same_strike_distance(self):
        # Two candidates equidistant from target; tighter spread wins.
        cands = [_c("WIDE", 99.0), _c("TIGHT", 101.0)]
        quotes = {
            "WIDE":  Quote(bid=1.70, ask=1.90),   # 11% spread
            "TIGHT": Quote(bid=1.79, ask=1.81),   # 1% spread
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        # WIDE is dropped by FATAL_SPREAD filter (>10%), only TIGHT remains.
        assert result.best.occ_symbol == "TIGHT"

    def test_tight_spread_runner_up_with_modest_strike_distance(self):
        # Closer strike but wider (yet not fatal) spread vs further strike
        # with tighter spread. Strike weight is dominant; closer should win
        # unless the spread gap is large enough to overcome 0.45 weighting.
        cands = [_c("CLOSE_WIDE", 100.0), _c("FAR_TIGHT", 102.0)]
        quotes = {
            "CLOSE_WIDE": Quote(bid=1.80, ask=1.88),  # ~4.3% spread
            "FAR_TIGHT":  Quote(bid=1.80, ask=1.81),  # ~0.5% spread
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        # CLOSE_WIDE: strike=1.0, spread=~0.13, premium~0.98 → 0.45*1.0 + 0.35*0.13 + 0.2*0.98 = 0.692
        # FAR_TIGHT:  strike=0.33, spread=0.90, premium~0.98 → 0.45*0.33 + 0.35*0.90 + 0.2*0.98 = 0.660
        # Close wins — verifies strike dominance is preserved.
        assert result.best.occ_symbol == "CLOSE_WIDE"


class TestHardFilters:
    def test_unaffordable_candidate_dropped(self):
        cands = [_c("CHEAP", 100.0), _c("PRICEY", 100.0)]
        quotes = {
            "CHEAP":  Quote(bid=2.00, ask=2.02),
            "PRICEY": Quote(bid=20.00, ask=20.20),
        }
        # Budget of $1,000/contract excludes PRICEY ($2,020) but allows CHEAP ($202).
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=1_000.0,
        )
        assert result.best.occ_symbol == "CHEAP"
        rejected_syms = [c.occ_symbol for c, _ in result.rejected]
        assert "PRICEY" in rejected_syms

    def test_fatal_spread_dropped(self):
        cands = [_c("OK", 100.0), _c("WIDE", 100.5)]
        quotes = {
            "OK":   Quote(bid=2.00, ask=2.02),
            "WIDE": Quote(bid=1.80, ask=2.40),  # ~28% spread, > 10%
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        assert result.best.occ_symbol == "OK"
        rejected_syms = [c.occ_symbol for c, _ in result.rejected]
        assert "WIDE" in rejected_syms

    def test_invalid_quote_dropped(self):
        cands = [_c("OK", 100.0), _c("BAD", 100.5)]
        quotes = {
            "OK":  Quote(bid=2.00, ask=2.02),
            "BAD": Quote(bid=0.0, ask=2.00),
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        rejected_syms = [c.occ_symbol for c, _ in result.rejected]
        assert "BAD" in rejected_syms
        assert result.best.occ_symbol == "OK"

    def test_missing_quote_dropped(self):
        cands = [_c("HAS_QUOTE", 100.0), _c("NO_QUOTE", 100.5)]
        quotes = {"HAS_QUOTE": Quote(bid=2.00, ask=2.02)}
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        rejected_syms = [c.occ_symbol for c, _ in result.rejected]
        assert "NO_QUOTE" in rejected_syms
        assert any("no quote" in reason for _, reason in result.rejected)

    def test_premium_outlier_dropped_when_at_least_three_candidates(self):
        # Four neighbors at ~$2; one outlier at $5 (>2× median 2). Should drop.
        cands = [
            _c("A", 99.0),
            _c("B", 100.0),
            _c("C", 100.5),
            _c("OUTLIER", 101.0),
        ]
        quotes = {
            "A":       Quote(bid=2.00, ask=2.02),
            "B":       Quote(bid=2.00, ask=2.02),
            "C":       Quote(bid=2.00, ask=2.02),
            "OUTLIER": Quote(bid=4.95, ask=5.00),
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        rejected_syms = [c.occ_symbol for c, _ in result.rejected]
        assert "OUTLIER" in rejected_syms

    def test_no_outlier_filter_with_fewer_than_three_survivors(self):
        # With only 2 quotable candidates, median is unreliable — keep both.
        cands = [_c("A", 100.0), _c("B", 101.0)]
        quotes = {
            "A": Quote(bid=2.00, ask=2.02),
            "B": Quote(bid=5.00, ask=5.05),  # 2.5× A but median filter skipped
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0,
            max_premium_per_contract=10_000.0,
        )
        # Both survive; A wins on strike + premium efficiency.
        rejected_syms = [c.occ_symbol for c, _ in result.rejected]
        assert "B" not in rejected_syms
        assert result.best.occ_symbol == "A"


class TestEmptyAndDegenerate:
    def test_no_candidates_returns_none(self):
        result = rank_call_candidates(
            [], {},
            target_strike=100.0, max_premium_per_contract=10_000.0,
        )
        assert result.best is None
        assert result.picks == []
        assert result.rejected == []

    def test_all_unaffordable_returns_none(self):
        cands = [_c("X", 100.0), _c("Y", 101.0)]
        quotes = {
            "X": Quote(bid=20.00, ask=20.10),
            "Y": Quote(bid=20.00, ask=20.10),
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0, max_premium_per_contract=500.0,
        )
        assert result.best is None
        assert len(result.rejected) == 2


class TestRunnersUp:
    def test_picks_ordered_best_to_worst(self):
        cands = [
            _c("BEST",  100.0),
            _c("MID",   100.5),
            _c("WORST", 102.0),
        ]
        quotes = {
            "BEST":  Quote(bid=2.00, ask=2.02),
            "MID":   Quote(bid=2.00, ask=2.05),
            "WORST": Quote(bid=2.00, ask=2.10),
        }
        result = rank_call_candidates(
            cands, quotes,
            target_strike=100.0, max_premium_per_contract=10_000.0,
        )
        ordered = [p.occ_symbol for p in result.picks]
        assert ordered == ["BEST", "MID", "WORST"]
        # Scores must be strictly non-increasing.
        scores = [p.score for p in result.picks]
        assert scores == sorted(scores, reverse=True)


# ── Put-spread ranker (11.28) ───────────────────────────────────────────────

# DTE-aware: candidates must expire in the future relative to date.today(),
# so build expirations as offsets from today rather than a fixed date.
_TODAY = date.today()


def _spread_exp(dte: int) -> date:
    return _TODAY + timedelta(days=dte)


def _spread(
    name: str,
    *,
    short_strike: float,
    long_strike: float,
    short_delta: float,
    dte: int = 37,
) -> SpreadCandidate:
    exp = _spread_exp(dte)
    return SpreadCandidate(
        short_leg=Candidate(f"{name}_S", short_strike, exp),
        long_leg=Candidate(f"{name}_L", long_strike, exp),
        short_leg_delta=short_delta,
    )


class TestSpreadCandidate:
    def test_width_is_strike_difference(self):
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17)
        assert s.width == pytest.approx(10.0)


class TestSpreadScoringFormula:
    def test_ideal_spread_scores_near_one(self):
        # On-target delta, fat credit, tight quotes, on-target DTE.
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17, dte=37)
        quotes = {
            "X_S": Quote(bid=4.90, ask=5.00),   # mid 4.95
            "X_L": Quote(bid=1.20, ask=1.25),   # mid 1.225 → net credit ~3.725/sh
        }
        result = rank_put_spread_candidates(
            [s], quotes,
            target_short_delta=0.17,
            target_dte=37,
            max_loss_per_position=5_000.0,
        )
        pick = result.best
        assert pick is not None
        # delta + dte components are perfect; credit ~0.37 of width; tight spreads.
        assert pick.components["short_delta"] == pytest.approx(1.0)
        assert pick.components["dte"] == pytest.approx(1.0)
        assert pick.score > 0.65

    def test_net_credit_and_max_loss_math(self):
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17)
        quotes = {
            "X_S": Quote(bid=3.00, ask=3.00),   # mid 3.00
            "X_L": Quote(bid=1.00, ask=1.00),   # mid 1.00 → net credit 2.00/sh
        }
        result = rank_put_spread_candidates(
            [s], quotes,
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
            min_credit_pct_of_width=0.15,  # credit 2.00 on width 10 = 20%
        )
        pick = result.best
        assert pick.net_credit == pytest.approx(2.00)
        # max loss = (width 10 − credit 2) × 100 = 800
        assert pick.max_loss == pytest.approx(800.0)

    def test_components_weighted_into_score(self):
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17, dte=37)
        quotes = {"X_S": Quote(4.90, 5.00), "X_L": Quote(1.20, 1.25)}
        result = rank_put_spread_candidates(
            [s], quotes,
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        pick = result.best
        c = pick.components
        expected = (
            WEIGHT_SHORT_DELTA * c["short_delta"]
            + WEIGHT_NET_CREDIT * c["net_credit"]
            + WEIGHT_SPREAD_QUALITY * c["spread_quality"]
            + WEIGHT_DTE * c["dte"]
        )
        assert pick.score == pytest.approx(expected)


class TestSpreadHardFilters:
    def _good_quotes(self, name: str) -> dict[str, Quote]:
        return {f"{name}_S": Quote(4.90, 5.00), f"{name}_L": Quote(1.20, 1.25)}

    def test_missing_leg_quote_rejected(self):
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17)
        result = rank_put_spread_candidates(
            [s], {"X_S": Quote(4.90, 5.00)},  # long leg quote missing
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        assert result.picks == []
        assert len(result.rejected) == 1
        assert "no quote" in result.rejected[0][1]

    def test_invalid_leg_quote_rejected(self):
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17)
        quotes = {"X_S": Quote(4.90, 5.00), "X_L": Quote(0.0, 0.0)}
        result = rank_put_spread_candidates(
            [s], quotes,
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        assert result.picks == []
        assert "invalid long quote" in result.rejected[0][1]

    def test_short_delta_outside_window_rejected(self):
        # target 0.17, window ±0.05 → 0.30 is well outside.
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.30)
        result = rank_put_spread_candidates(
            [s], self._good_quotes("X"),
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        assert result.picks == []
        assert "short delta" in result.rejected[0][1]

    def test_short_delta_inside_window_survives(self):
        # 0.17 + 0.04 is within the ±0.05 window — must survive.
        s = _spread(
            "X", short_strike=500.0, long_strike=490.0,
            short_delta=0.21,
        )
        result = rank_put_spread_candidates(
            [s], self._good_quotes("X"),
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        assert result.best is not None

    def test_non_positive_net_credit_rejected(self):
        # Short mid < long mid → debit, not credit.
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17)
        quotes = {"X_S": Quote(1.00, 1.00), "X_L": Quote(2.00, 2.00)}
        result = rank_put_spread_candidates(
            [s], quotes,
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        assert result.picks == []
        assert "non-positive net credit" in result.rejected[0][1]

    def test_thin_credit_rejected(self):
        # width 10, min 25% → need ≥ 2.50/sh. Provide 1.00/sh.
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17)
        quotes = {"X_S": Quote(2.00, 2.00), "X_L": Quote(1.00, 1.00)}
        result = rank_put_spread_candidates(
            [s], quotes,
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
            min_credit_pct_of_width=0.25,
        )
        assert result.picks == []
        assert "thin credit" in result.rejected[0][1]

    def test_max_loss_over_sleeve_cap_rejected(self):
        # width 10, credit 3.00 (passes 25% floor) → max loss 700. Cap at 500.
        s = _spread("X", short_strike=500.0, long_strike=490.0, short_delta=0.17)
        quotes = {"X_S": Quote(4.00, 4.00), "X_L": Quote(1.00, 1.00)}
        result = rank_put_spread_candidates(
            [s], quotes,
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=500.0,
        )
        assert result.picks == []
        assert "max loss" in result.rejected[0][1]

    def test_non_positive_width_rejected(self):
        # long strike above short strike → width ≤ 0.
        s = _spread("X", short_strike=490.0, long_strike=500.0, short_delta=0.17)
        result = rank_put_spread_candidates(
            [s], self._good_quotes("X"),
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        assert result.picks == []
        assert "width" in result.rejected[0][1]


class TestSpreadOrderingAndDegenerate:
    def test_empty_candidates_yields_empty_result(self):
        result = rank_put_spread_candidates(
            [], {}, target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        assert result.picks == []
        assert result.rejected == []
        assert result.best is None

    def test_picks_ordered_best_to_worst(self):
        # BEST: on-target delta. WORST: delta near the window edge (still
        # inside, but a worse delta-proximity score).
        best = _spread("BEST", short_strike=500.0, long_strike=490.0,
                       short_delta=0.17, dte=37)
        worst = _spread("WORST", short_strike=500.0, long_strike=490.0,
                        short_delta=0.21, dte=37)
        quotes = {
            "BEST_S": Quote(4.90, 5.00), "BEST_L": Quote(1.20, 1.25),
            "WORST_S": Quote(4.90, 5.00), "WORST_L": Quote(1.20, 1.25),
        }
        result = rank_put_spread_candidates(
            [worst, best], quotes,
            target_short_delta=0.17, target_dte=37,
            max_loss_per_position=5_000.0,
        )
        assert [p.candidate.short_leg.occ_symbol for p in result.picks] == [
            "BEST_S", "WORST_S",
        ]
        scores = [p.score for p in result.picks]
        assert scores == sorted(scores, reverse=True)
