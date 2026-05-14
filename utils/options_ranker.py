"""
Options contract ranker (11.25, extended for spreads in 11.28).

Pure logic — given candidate contracts, their quotes, and budget/target
constraints, produce a ranked list of picks.

Single-leg call picking (11.25): ``rank_call_candidates`` scores by
  * strike proximity to a slightly-ITM target,
  * spread quality (bid–ask tightness),
  * premium efficiency (cheaper preferred when other factors tie).

Two-leg put-spread picking (11.28): ``rank_put_spread_candidates`` scores a
list of ``SpreadCandidate`` pairs (a sold higher-strike put + a bought
lower-strike put — a bull put credit spread) by
  * short-leg delta proximity to a target (e.g. 0.17),
  * net credit relative to spread width,
  * combined bid/ask spread quality of both legs,
  * DTE proximity to a mid-DTE target.

Hard filters (drop the candidate before scoring) handle affordability,
broken spreads, premium outliers, thin credits, and out-of-window deltas.
The strategy/picker calls the appropriate ``rank_*`` function and chooses
the top entry.

No network I/O lives here — quote fetching and delta estimation are the
caller's responsibility, passed in as plain data. This keeps the module
fast to test offline and trivial to reason about.
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

# ── Put-spread tunables (11.28) ─────────────────────────────────────────────
# Composite weights for rank_put_spread_candidates — see credit_spread design
# doc §5. Delta proximity dominates; DTE is the tiebreaker.
WEIGHT_SHORT_DELTA = 0.40
WEIGHT_NET_CREDIT = 0.30
WEIGHT_SPREAD_QUALITY = 0.20
WEIGHT_DTE = 0.10

SHORT_DELTA_WINDOW = 0.05     # hard filter: |delta − target| must be within this
DTE_TOLERANCE_DAYS = 15.0     # DTE score reaches 0.0 this many days off target


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


# ── Put-spread ranking (11.28) ──────────────────────────────────────────────


@dataclass(frozen=True)
class SpreadCandidate:
    """
    A bull put credit spread under consideration before scoring.

    A bull put spread sells a higher-strike put and buys a lower-strike put
    with the same expiration. ``short_leg`` is the sold put, ``long_leg`` is
    the bought put that caps the loss. ``short_leg_delta`` is the |delta|
    estimate for the short leg (e.g. 0.17), supplied by the picker — the
    ranker does no Greeks math itself.
    """

    short_leg: Candidate
    long_leg: Candidate
    short_leg_delta: float

    @property
    def width(self) -> float:
        """Strike width in $/share (short strike − long strike)."""
        return self.short_leg.strike - self.long_leg.strike


@dataclass(frozen=True)
class ScoredSpread:
    """A scored put-spread candidate. Higher ``score`` wins."""

    candidate: SpreadCandidate
    short_quote: Quote
    long_quote: Quote
    net_credit: float            # $/share: short mid − long mid
    max_loss: float              # $ per contract: (width − net_credit) × mult
    score: float
    components: dict[str, float] = field(default_factory=dict)

    @property
    def width(self) -> float:
        return self.candidate.width

    @property
    def short_occ(self) -> str:
        return self.candidate.short_leg.occ_symbol

    @property
    def long_occ(self) -> str:
        return self.candidate.long_leg.occ_symbol


@dataclass(frozen=True)
class SpreadRankResult:
    """Output of ``rank_put_spread_candidates`` — ranked picks + audit trail."""

    picks: list[ScoredSpread]                       # ranked best→worst
    rejected: list[tuple[SpreadCandidate, str]]     # (candidate, reason)

    @property
    def best(self) -> ScoredSpread | None:
        return self.picks[0] if self.picks else None


def _short_delta_score(delta: float, target_delta: float) -> float:
    """1.0 at the target delta, 0.0 once SHORT_DELTA_WINDOW away."""
    if SHORT_DELTA_WINDOW <= 0:
        return 0.0
    return max(0.0, 1.0 - abs(delta - target_delta) / SHORT_DELTA_WINDOW)


def _net_credit_score(net_credit: float, width: float) -> float:
    """
    Credit as a fraction of width — more credit per unit of width is better.
    A spread collecting credit == width would score 1.0 (not achievable in
    practice); a zero-credit spread scores 0.0. Clamped to [0, 1].
    """
    if width <= 0:
        return 0.0
    return max(0.0, min(1.0, net_credit / width))


def _dte_score(dte: float, target_dte: float) -> float:
    """1.0 at the target DTE, decaying to 0.0 DTE_TOLERANCE_DAYS away."""
    if DTE_TOLERANCE_DAYS <= 0:
        return 0.0
    return max(0.0, 1.0 - abs(dte - target_dte) / DTE_TOLERANCE_DAYS)


def rank_put_spread_candidates(
    candidates: list[SpreadCandidate],
    quotes: dict[str, Quote],
    *,
    target_short_delta: float,
    target_dte: float,
    max_loss_per_position: float,
    min_credit_pct_of_width: float = 0.25,
    contract_multiplier: int = 100,
) -> SpreadRankResult:
    """
    Rank bull put credit spreads by composite quality.

    Parameters
    ----------
    candidates
        Paired-leg candidates from the multi-leg picker. Each carries a
        short-leg |delta| estimate (the picker computes it; the ranker does
        not touch Greeks).
    quotes
        ``{occ_symbol: Quote}`` for every leg of every candidate. A candidate
        is dropped if either leg has no quote or a broken quote.
    target_short_delta
        Desired |delta| for the short put (e.g. 0.17). Candidates outside
        ``[target ± SHORT_DELTA_WINDOW]`` are dropped before scoring.
    target_dte
        Mid-DTE target in days for the DTE-proximity score component. DTE is
        derived from each candidate's leg ``expiration_date`` minus today.
        (Pass the candidate's own DTE via ``SpreadCandidate`` — see the
        picker; the ranker reads ``short_leg.expiration_date``.)
    max_loss_per_position
        Affordability cap in $ from the sleeve allocator. ``(width −
        net_credit) × contract_multiplier`` must not exceed this.
    min_credit_pct_of_width
        Thin-credit floor: net credit must be ≥ this fraction of the strike
        width (default 25%).
    contract_multiplier
        Shares per contract; 100 for standard equity options.

    Returns
    -------
    ``SpreadRankResult`` with ``picks`` ranked best→worst and ``rejected``
    listing every dropped candidate with a human-readable reason.

    Notes
    -----
    Net credit is estimated mid-to-mid (short mid − long mid), consistent
    with how ``rank_call_candidates`` treats single-leg premium. The actual
    fill happens at a combo limit price set by the execution layer.
    """
    from datetime import date as _date

    rejected: list[tuple[SpreadCandidate, str]] = []
    survivors: list[tuple[SpreadCandidate, Quote, Quote, float, float, float]] = []
    # tuple: (candidate, short_quote, long_quote, net_credit, max_loss, dte)
    today = _date.today()

    for cand in candidates:
        width = cand.width
        if width <= 0:
            rejected.append((cand, f"non-positive width {width:.2f}"))
            continue

        short_q = quotes.get(cand.short_leg.occ_symbol)
        long_q = quotes.get(cand.long_leg.occ_symbol)
        if short_q is None or long_q is None:
            missing = cand.short_leg.occ_symbol if short_q is None else cand.long_leg.occ_symbol
            rejected.append((cand, f"no quote for {missing}"))
            continue
        for label, q in (("short", short_q), ("long", long_q)):
            if q.bid <= 0 or q.ask <= 0 or q.ask < q.bid:
                rejected.append((
                    cand,
                    f"invalid {label} quote bid={q.bid:.2f} ask={q.ask:.2f}",
                ))
                break
        else:
            # Short-leg delta window.
            if abs(cand.short_leg_delta - target_short_delta) > SHORT_DELTA_WINDOW:
                rejected.append((
                    cand,
                    f"short delta {cand.short_leg_delta:.3f} outside "
                    f"{target_short_delta:.2f} ± {SHORT_DELTA_WINDOW:.2f}",
                ))
                continue

            net_credit = short_q.mid - long_q.mid
            if net_credit <= 0:
                rejected.append((
                    cand,
                    f"non-positive net credit ${net_credit:.2f}/sh",
                ))
                continue
            if net_credit < min_credit_pct_of_width * width:
                rejected.append((
                    cand,
                    f"thin credit ${net_credit:.2f}/sh < "
                    f"{min_credit_pct_of_width:.0%} × ${width:.2f} width",
                ))
                continue

            max_loss = (width - net_credit) * contract_multiplier
            if max_loss > max_loss_per_position:
                rejected.append((
                    cand,
                    f"max loss ${max_loss:,.0f} > sleeve cap "
                    f"${max_loss_per_position:,.0f}",
                ))
                continue

            exp = cand.short_leg.expiration_date
            dte = float((exp - today).days) if isinstance(exp, _date) else target_dte
            survivors.append((cand, short_q, long_q, net_credit, max_loss, dte))

    picks: list[ScoredSpread] = []
    for cand, short_q, long_q, net_credit, max_loss, dte in survivors:
        s_delta = _short_delta_score(cand.short_leg_delta, target_short_delta)
        s_credit = _net_credit_score(net_credit, cand.width)
        combined_spread_pct = statistics.mean(
            (short_q.spread_pct, long_q.spread_pct)
        )
        s_spread = _spread_score(combined_spread_pct)
        s_dte = _dte_score(dte, target_dte)
        score = (
            WEIGHT_SHORT_DELTA * s_delta
            + WEIGHT_NET_CREDIT * s_credit
            + WEIGHT_SPREAD_QUALITY * s_spread
            + WEIGHT_DTE * s_dte
        )
        picks.append(ScoredSpread(
            candidate=cand,
            short_quote=short_q,
            long_quote=long_q,
            net_credit=net_credit,
            max_loss=max_loss,
            score=score,
            components={
                "short_delta": s_delta,
                "net_credit": s_credit,
                "spread_quality": s_spread,
                "dte": s_dte,
            },
        ))

    picks.sort(key=lambda p: p.score, reverse=True)
    return SpreadRankResult(picks=picks, rejected=rejected)
