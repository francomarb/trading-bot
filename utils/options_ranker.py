"""
Options contract ranker (11.25).

Pure logic — given a list of candidate call contracts, their quotes, and a
budget, produce a ranked list of picks scored by:
  * strike proximity to a slightly-ITM target,
  * spread quality (bid–ask tightness),
  * premium efficiency (cheaper preferred when other factors tie).

Hard filters (drop the candidate before scoring) handle affordability,
broken spreads, and premium outliers. The strategy/picker calls
``rank_call_candidates`` and chooses the top entry.

No network I/O lives here — quote fetching is the caller's responsibility,
passed in as a dict ``{occ_symbol: Quote}``. This keeps the module fast
to test offline and trivial to reason about.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field


# Tunables (mirrored in scoring tests). Keep strike proximity dominant so the
# picker stays in the intended slightly-ITM neighborhood — see 11.25 design.
WEIGHT_STRIKE = 0.45
WEIGHT_SPREAD = 0.35
WEIGHT_PREMIUM_EFF = 0.20

STRIKE_TOLERANCE_PCT = 0.03   # used as the score denominator only
FATAL_SPREAD_PCT = 0.10       # drop entirely if spread is wider than this
SOFT_SPREAD_PCT = 0.05        # spread that scores zero on the spread axis
PREMIUM_OUTLIER_MULTIPLIER = 2.0  # drop candidates whose mid > N× median(mid)


@dataclass(frozen=True)
class Quote:
    """A bid/ask snapshot for one OCC contract. ``mid`` is computed."""

    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        m = self.mid
        if m <= 0:
            return float("inf")
        return (self.ask - self.bid) / m


@dataclass(frozen=True)
class Candidate:
    """A contract under consideration before scoring."""

    occ_symbol: str
    strike: float
    expiration_date: object  # datetime.date — kept Any for ranker independence


@dataclass(frozen=True)
class ScoredPick:
    """A scored candidate. Higher ``score`` wins."""

    candidate: Candidate
    quote: Quote
    score: float
    components: dict[str, float] = field(default_factory=dict)
    premium_per_contract: float = 0.0

    @property
    def occ_symbol(self) -> str:
        return self.candidate.occ_symbol


@dataclass(frozen=True)
class RankResult:
    """Output of ``rank_call_candidates`` — top pick plus full audit trail."""

    picks: list[ScoredPick]              # ranked best→worst (survivors only)
    rejected: list[tuple[Candidate, str]]  # (candidate, reason) for hard-filter drops

    @property
    def best(self) -> ScoredPick | None:
        return self.picks[0] if self.picks else None


def _strike_proximity_score(strike: float, target_strike: float) -> float:
    """1.0 at the target, 0.0 once we're STRIKE_TOLERANCE_PCT × target away."""
    if target_strike <= 0:
        return 0.0
    distance_pct = abs(strike - target_strike) / target_strike
    return max(0.0, 1.0 - distance_pct / STRIKE_TOLERANCE_PCT)


def _spread_score(spread_pct: float) -> float:
    """1.0 at zero spread, 0.0 at SOFT_SPREAD_PCT, clamped."""
    if spread_pct <= 0:
        return 1.0
    return max(0.0, 1.0 - spread_pct / SOFT_SPREAD_PCT)


def _premium_efficiency_score(premium: float, max_premium: float) -> float:
    """1.0 when the contract is free, 0.0 when it uses the entire budget."""
    if max_premium <= 0:
        return 0.0
    return max(0.0, 1.0 - premium / max_premium)


def rank_call_candidates(
    candidates: list[Candidate],
    quotes: dict[str, Quote],
    *,
    target_strike: float,
    max_premium_per_contract: float,
    contract_multiplier: int = 100,
) -> RankResult:
    """
    Rank a list of call candidates by composite quality.

    Parameters
    ----------
    candidates
        Pre-filtered list of contracts (typically the top-K strike-nearest
        candidates from the chain).
    quotes
        ``{occ_symbol: Quote}`` for every candidate. Missing entries cause
        the candidate to be dropped (no quote → can't score spread).
    target_strike
        Slightly-ITM target (underlying × 0.995 for ~0.55-delta call on SPY).
    max_premium_per_contract
        Per-contract budget in $ from the sleeve allocator (``notional_cap /
        contract_multiplier``).
    contract_multiplier
        Shares per contract; 100 for standard equity options.

    Returns
    -------
    RankResult with ``picks`` ranked best→worst and ``rejected`` listing
    every dropped candidate with a reason for log explainability.
    """
    rejected: list[tuple[Candidate, str]] = []
    survivors: list[tuple[Candidate, Quote, float]] = []  # (cand, quote, premium)

    quoted_mids: list[float] = []
    for cand in candidates:
        q = quotes.get(cand.occ_symbol)
        if q is None:
            rejected.append((cand, "no quote"))
            continue
        if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
            rejected.append((cand, f"invalid quote bid={q.bid:.2f} ask={q.ask:.2f}"))
            continue
        if q.spread_pct > FATAL_SPREAD_PCT:
            rejected.append((cand, f"spread {q.spread_pct:.1%} > {FATAL_SPREAD_PCT:.0%}"))
            continue
        premium = q.mid * contract_multiplier
        if premium > max_premium_per_contract:
            rejected.append((
                cand,
                f"premium ${premium:,.0f} > sleeve cap ${max_premium_per_contract:,.0f}",
            ))
            continue
        survivors.append((cand, q, premium))
        quoted_mids.append(q.mid)

    # Premium-sanity outlier filter — applied on the pre-budget mids so a
    # quote that's 3× neighbors gets dropped even if it happens to fit
    # the budget. Needs at least 3 candidates to compute a stable median.
    if len(quoted_mids) >= 3:
        median_mid = statistics.median(quoted_mids)
        ceiling = median_mid * PREMIUM_OUTLIER_MULTIPLIER
        cleaned: list[tuple[Candidate, Quote, float]] = []
        for cand, q, premium in survivors:
            if q.mid > ceiling:
                rejected.append((
                    cand,
                    f"premium outlier mid=${q.mid:.2f} > {PREMIUM_OUTLIER_MULTIPLIER:.0f}× median ${median_mid:.2f}",
                ))
                continue
            cleaned.append((cand, q, premium))
        survivors = cleaned

    picks: list[ScoredPick] = []
    for cand, q, premium in survivors:
        s_strike = _strike_proximity_score(cand.strike, target_strike)
        s_spread = _spread_score(q.spread_pct)
        s_premium = _premium_efficiency_score(premium, max_premium_per_contract)
        score = (
            WEIGHT_STRIKE * s_strike
            + WEIGHT_SPREAD * s_spread
            + WEIGHT_PREMIUM_EFF * s_premium
        )
        picks.append(ScoredPick(
            candidate=cand,
            quote=q,
            score=score,
            components={
                "strike_proximity": s_strike,
                "spread_quality": s_spread,
                "premium_efficiency": s_premium,
            },
            premium_per_contract=premium,
        ))

    picks.sort(key=lambda p: p.score, reverse=True)
    return RankResult(picks=picks, rejected=rejected)
