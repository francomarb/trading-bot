"""
Unit tests for utils.options_ranker — pure logic, no I/O.

Covers the scoring formula, the hard filters (affordability, fatal spread,
invalid quote, premium outlier), and the ordering guarantees of the top
pick relative to runners-up.
"""

from __future__ import annotations

from datetime import date

import pytest

from utils.options_ranker import (
    Candidate,
    Quote,
    FATAL_SPREAD_PCT,
    PREMIUM_OUTLIER_MULTIPLIER,
    SOFT_SPREAD_PCT,
    STRIKE_TOLERANCE_PCT,
    WEIGHT_PREMIUM_EFF,
    WEIGHT_SPREAD,
    WEIGHT_STRIKE,
    rank_call_candidates,
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
