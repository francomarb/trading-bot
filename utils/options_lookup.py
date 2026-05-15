"""
Options Contract Resolver (11.25).

Finds the highest-scoring affordable call option contract for the given
underlying, using ``utils.options_ranker`` to rank by composite quality
(strike proximity + spread quality + premium efficiency) instead of
just the mathematically closest strike.

The caller must provide a per-contract budget cap and a quote-lookup
callback. Sleeve-budget enforcement is built in, so a contract that
breaches the per-position sleeve cap is rejected by the picker itself
rather than later by the risk manager.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from loguru import logger

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import AssetStatus, ContractType

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from utils.options_ranker import (
    Candidate,
    Quote,
    ScoredPick,
    ScoredSpread,
    SpreadCandidate,
    rank_call_candidates,
    rank_put_spread_candidates,
)

_client: TradingClient | None = None

_STRIKE_WINDOW_PCT = 0.03
_PAGE_LIMIT = 200
_MAX_PAGES = 10
_TOP_K_TO_QUOTE = 5  # cap quote fetches at this many strike-nearest candidates

# ── Put-spread picker tunables (11.28) ──────────────────────────────────────
_RISK_FREE_RATE = 0.05            # B-S risk-free rate; refresh quarterly
_SPREAD_STRIKE_FLOOR_PCT = 0.80   # query put strikes down to this × underlying
_SPREAD_DELTA_PREFILTER = 0.12    # keep shorts within this of target before quoting
_SPREAD_WIDTH_MATCH_TOL = 1.00    # $ tolerance when matching the long-leg strike
_SPREAD_TOP_K_TO_QUOTE = 6        # cap quote fetches at this many short candidates


# Quote-lookup callback signature. Returns ``None`` for an OCC symbol that
# has no quotable data (so the ranker can skip it cleanly).
QuoteLookup = Callable[[list[str]], dict[str, "Quote | None"]]


@dataclass(frozen=True)
class ContractPick:
    """
    Result of ``find_best_call``. Carries enough detail that the strategy
    can place an order without re-fetching the same quote.
    """

    occ_symbol: str
    premium: float            # midpoint $/share (× 100 for per-contract cost)
    spread_pct: float         # (ask − bid) / mid
    score: float              # composite 0.0–1.0
    components: dict[str, float]
    runners_up: list[ScoredPick]  # next-best (top 3 for log explainability)


def _get_client() -> TradingClient:
    global _client
    if _client is None:
        _client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            paper=ALPACA_PAPER,
        )
    return _client


def find_best_call(
    symbol: str,
    underlying_price: float,
    *,
    min_dte: int,
    max_dte: int,
    max_premium_per_contract: float,
    quote_lookup: QuoteLookup,
    target_delta: float = 0.55,  # accepted for symmetry; ITM proxy is fixed
) -> ContractPick | None:
    """
    Find the best-scoring affordable call contract.

    Parameters
    ----------
    symbol
        Underlying ticker (e.g. ``"SPY"``).
    underlying_price
        Current underlying close.
    min_dte, max_dte
        Days-to-expiration window for the chain query.
    max_premium_per_contract
        Sleeve-derived per-contract budget cap in $ (notional_cap; the ranker
        excludes contracts whose midpoint × 100 exceeds this).
    quote_lookup
        Callback ``list[occ_symbol] → {occ_symbol: Quote | None}``. Called
        once for the top-K strike-nearest candidates.
    target_delta
        Accepted for API symmetry. Alpaca paper does not stream live Greeks
        so we approximate ~0.55 delta with a slightly-ITM strike target
        (underlying × 0.995).

    Returns
    -------
    ``ContractPick`` with the chosen contract, its quote, and the score
    breakdown; ``None`` if no contract survived candidate filtering or
    ranking.
    """
    client = _get_client()

    now = datetime.now(timezone.utc).date()
    min_date = now + timedelta(days=min_dte)
    max_date = now + timedelta(days=max_dte)

    target_strike = underlying_price * 0.995
    strike_floor = round(target_strike * (1.0 - _STRIKE_WINDOW_PCT), 2)
    strike_ceiling = round(target_strike * (1.0 + _STRIKE_WINDOW_PCT), 2)

    contracts = []
    page_token: str | None = None
    pages = 0

    try:
        while pages < _MAX_PAGES:
            req = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                status=AssetStatus.ACTIVE,
                expiration_date_gte=min_date.isoformat(),
                expiration_date_lte=max_date.isoformat(),
                type=ContractType.CALL,
                strike_price_gte=f"{strike_floor:.2f}",
                strike_price_lte=f"{strike_ceiling:.2f}",
                limit=_PAGE_LIMIT,
                page_token=page_token,
            )
            response = client.get_option_contracts(req)
            page_contracts = response.option_contracts or []
            contracts.extend(page_contracts)
            page_token = response.next_page_token
            pages += 1
            if not page_token:
                break
    except Exception as e:
        logger.error(f"Failed to fetch option contracts for {symbol}: {e}")
        return None

    if not contracts:
        logger.warning(
            f"No active call contracts found for {symbol} between {min_date} and "
            f"{max_date} near strike band ${strike_floor:.2f}-${strike_ceiling:.2f}."
        )
        return None

    contracts = [
        c for c in contracts
        if strike_floor <= float(c.strike_price) <= strike_ceiling
    ]
    if not contracts:
        logger.warning(
            f"All returned call contracts for {symbol} fell outside the expected "
            f"strike band ${strike_floor:.2f}-${strike_ceiling:.2f}; skipping."
        )
        return None

    by_expiry = defaultdict(list)
    for c in contracts:
        by_expiry[c.expiration_date].append(c)
    if not by_expiry:
        return None

    expirations = sorted(list(by_expiry.keys()))
    best_expiry = expirations[0]
    expiry_contracts = by_expiry[best_expiry]

    # Cap quote fetches: pick the K closest-strike candidates for this
    # expiration, then quote-rank them. K=5 covers SPY's strike density
    # comfortably without over-fetching.
    expiry_contracts.sort(
        key=lambda c: abs(float(c.strike_price) - target_strike)
    )
    top_k = expiry_contracts[:_TOP_K_TO_QUOTE]
    candidates = [
        Candidate(
            occ_symbol=c.symbol,
            strike=float(c.strike_price),
            expiration_date=c.expiration_date,
        )
        for c in top_k
    ]

    try:
        quote_map = quote_lookup([c.occ_symbol for c in candidates])
    except Exception as e:
        logger.error(f"Quote lookup failed for {symbol}: {e}")
        return None

    # Strip out None entries so the ranker only sees real quotes.
    quotes: dict[str, Quote] = {
        occ: q for occ, q in quote_map.items() if q is not None
    }

    result = rank_call_candidates(
        candidates,
        quotes,
        target_strike=target_strike,
        max_premium_per_contract=max_premium_per_contract,
    )

    if result.best is None:
        rejected_summary = "; ".join(
            f"{c.occ_symbol}: {r}" for c, r in result.rejected
        ) or "no candidates"
        logger.warning(
            f"No tradeable call contract for {symbol} "
            f"(expiry {best_expiry}, target ${target_strike:.2f}) — {rejected_summary}"
        )
        return None

    top = result.best
    runners_up = result.picks[1:4]  # log up to 3 alternates
    if runners_up:
        runner_str = "; ".join(
            f"{p.occ_symbol} score={p.score:.2f}" for p in runners_up
        )
    else:
        runner_str = "none"
    logger.info(
        f"Resolved Option: {top.occ_symbol} "
        f"strike=${top.candidate.strike:.2f} expiry={top.candidate.expiration_date} "
        f"underlying=${underlying_price:.2f} target=${target_strike:.2f} "
        f"premium=${top.quote.mid:.2f} spread={top.quote.spread_pct:.1%} "
        f"score={top.score:.2f} "
        f"[strike={top.components['strike_proximity']:.2f} "
        f"spread={top.components['spread_quality']:.2f} "
        f"premium={top.components['premium_efficiency']:.2f}] "
        f"runners_up=[{runner_str}]"
    )

    return ContractPick(
        occ_symbol=top.occ_symbol,
        premium=top.quote.mid,
        spread_pct=top.quote.spread_pct,
        score=top.score,
        components=dict(top.components),
        runners_up=runners_up,
    )


# ── Multi-leg put-spread picker (11.28) ─────────────────────────────────────


@dataclass(frozen=True)
class SpreadPick:
    """
    Result of ``find_best_put_spread`` — a chosen bull put credit spread plus
    its economics, so the strategy can place a combo order without re-fetching
    quotes.
    """

    short_occ: str
    long_occ: str
    short_strike: float
    long_strike: float
    expiration_date: object       # datetime.date
    width: float                  # $/share
    net_credit: float             # $/share (short mid − long mid)
    max_loss: float               # $ per contract
    short_leg_delta: float        # |delta| estimate for the short leg
    score: float                  # composite 0.0–1.0
    components: dict[str, float]
    runners_up: list[ScoredSpread]


def build_opra_quote_lookup() -> QuoteLookup:
    """
    Construct a ``QuoteLookup`` callback that resolves a batch of OCC symbols
    to ``Quote`` objects via Alpaca's option-snapshot endpoint.

    Returns ``None`` for any symbol without a valid live two-sided quote so
    the ranker/picker drops it cleanly. Shared by the credit-spread strategy
    (entry picker + spread-exit mid pricing); the single-leg
    ``spy_options_reversion`` strategy still carries its own equivalent.
    """
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionSnapshotRequest

    data_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    def _lookup(occ_symbols: list[str]) -> dict[str, "Quote | None"]:
        if not occ_symbols:
            return {}
        try:
            snapshot = data_client.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=occ_symbols)
            )
        except Exception as e:
            logger.warning(f"OPRA snapshot batch failed: {e}")
            return {occ: None for occ in occ_symbols}
        out: dict[str, "Quote | None"] = {}
        for occ in occ_symbols:
            entry = snapshot.get(occ)
            if entry is None or entry.latest_quote is None:
                out[occ] = None
                continue
            q = entry.latest_quote
            bid = float(q.bid_price)
            ask = float(q.ask_price)
            out[occ] = Quote(bid=bid, ask=ask) if bid > 0 and ask > 0 else None
        return out

    return _lookup


def estimate_put_delta(
    *,
    underlying_price: float,
    strike: float,
    dte_days: float,
    iv: float,
    risk_free_rate: float = _RISK_FREE_RATE,
) -> float:
    """
    Estimate the |delta| of a put via Black-Scholes.

    Alpaca paper does not stream live Greeks, so the picker approximates
    them. ``iv`` is supplied by the caller (PR 3 wires the per-instrument
    IV proxy; until then callers pass an explicit volatility). Pure function
    — no I/O, trivially testable.

    Returns the absolute delta (puts have negative delta; the config and the
    ranker both work with the magnitude, e.g. a "17-delta short put" → 0.17).
    """
    from blackscholes import BlackScholesPut

    T = max(dte_days / 365.0, 0.001)
    put = BlackScholesPut(
        S=underlying_price, K=strike, T=T, r=risk_free_rate, sigma=iv
    )
    return abs(float(put.delta()))


def find_best_put_spread(
    symbol: str,
    underlying_price: float,
    *,
    min_dte: int,
    max_dte: int,
    spread_width: float,
    target_short_delta: float,
    iv: float,
    max_loss_per_position: float,
    quote_lookup: QuoteLookup,
    min_credit_pct_of_width: float = 0.25,
    risk_free_rate: float = _RISK_FREE_RATE,
) -> SpreadPick | None:
    """
    Find the best-scoring affordable bull put credit spread.

    A bull put spread sells a higher-strike put and buys a put
    ``spread_width`` below it (same expiration) to cap the loss.

    Parameters
    ----------
    symbol
        Underlying ticker (e.g. ``"SPY"``).
    underlying_price
        Current underlying close.
    min_dte, max_dte
        Days-to-expiration window for the chain query. The expiration whose
        DTE is closest to the window midpoint is chosen.
    spread_width
        Target strike width in $ (short strike − long strike).
    target_short_delta
        Desired |delta| for the short leg (e.g. 0.17).
    iv
        Volatility for the Black-Scholes delta estimate. PR 3 supplies the
        per-instrument IV proxy; callers pass an explicit value until then.
    max_loss_per_position
        Affordability cap in $ from the sleeve allocator.
    quote_lookup
        Callback ``list[occ_symbol] → {occ_symbol: Quote | None}``. Called
        once for all legs of the short-listed candidates.
    min_credit_pct_of_width
        Thin-credit floor passed through to the ranker (default 25%).
    risk_free_rate
        Black-Scholes risk-free rate for the delta estimate.

    Returns
    -------
    ``SpreadPick`` for the top-scoring spread, or ``None`` if no spread
    survived candidate construction or ranking.
    """
    client = _get_client()

    now = datetime.now(timezone.utc).date()
    min_date = now + timedelta(days=min_dte)
    max_date = now + timedelta(days=max_dte)

    # Query OTM puts down to a floor below the underlying — wide enough to
    # cover both the ~target-delta short strikes and their long legs.
    strike_floor = round(underlying_price * _SPREAD_STRIKE_FLOOR_PCT, 2)
    strike_ceiling = round(underlying_price, 2)

    contracts = []
    page_token: str | None = None
    pages = 0
    try:
        while pages < _MAX_PAGES:
            req = GetOptionContractsRequest(
                underlying_symbols=[symbol],
                status=AssetStatus.ACTIVE,
                expiration_date_gte=min_date.isoformat(),
                expiration_date_lte=max_date.isoformat(),
                type=ContractType.PUT,
                strike_price_gte=f"{strike_floor:.2f}",
                strike_price_lte=f"{strike_ceiling:.2f}",
                limit=_PAGE_LIMIT,
                page_token=page_token,
            )
            response = client.get_option_contracts(req)
            contracts.extend(response.option_contracts or [])
            page_token = response.next_page_token
            pages += 1
            if not page_token:
                break
    except Exception as e:
        logger.error(f"Failed to fetch put contracts for {symbol}: {e}")
        return None

    if not contracts:
        logger.warning(
            f"No active put contracts for {symbol} between {min_date} and "
            f"{max_date} in strike band ${strike_floor:.2f}-${strike_ceiling:.2f}."
        )
        return None

    # Group by expiration, choose the one closest to the DTE-window midpoint.
    by_expiry: dict[object, list] = defaultdict(list)
    for c in contracts:
        by_expiry[c.expiration_date].append(c)
    target_dte = (min_dte + max_dte) / 2.0
    chosen_expiry = min(
        by_expiry.keys(),
        key=lambda exp: abs((exp - now).days - target_dte),
    )
    expiry_contracts = by_expiry[chosen_expiry]
    chosen_dte = (chosen_expiry - now).days

    # strike → contract for the chosen expiration (one contract per strike).
    by_strike: dict[float, object] = {}
    for c in expiry_contracts:
        by_strike[float(c.strike_price)] = c
    strikes_sorted = sorted(by_strike)

    def _nearest_strike(target: float) -> float | None:
        """Closest available strike within the width-match tolerance."""
        if not strikes_sorted:
            return None
        nearest = min(strikes_sorted, key=lambda s: abs(s - target))
        return nearest if abs(nearest - target) <= _SPREAD_WIDTH_MATCH_TOL else None

    # Build short-leg candidates: estimate each strike's delta, keep those
    # near the target, pair each with a long leg `spread_width` below.
    scored_shorts: list[tuple[float, object, float]] = []  # (|Δ−target|, contract, delta)
    for strike, contract in by_strike.items():
        delta = estimate_put_delta(
            underlying_price=underlying_price,
            strike=strike,
            dte_days=chosen_dte,
            iv=iv,
            risk_free_rate=risk_free_rate,
        )
        if abs(delta - target_short_delta) <= _SPREAD_DELTA_PREFILTER:
            scored_shorts.append((abs(delta - target_short_delta), contract, delta))

    if not scored_shorts:
        logger.warning(
            f"No put strikes near {target_short_delta:.2f} delta for {symbol} "
            f"(expiry {chosen_expiry}, iv={iv:.2f})."
        )
        return None

    scored_shorts.sort(key=lambda t: t[0])
    candidates: list[SpreadCandidate] = []
    for _, short_contract, short_delta in scored_shorts[:_SPREAD_TOP_K_TO_QUOTE]:
        short_strike = float(short_contract.strike_price)
        long_strike = _nearest_strike(short_strike - spread_width)
        if long_strike is None or long_strike >= short_strike:
            continue
        long_contract = by_strike[long_strike]
        candidates.append(SpreadCandidate(
            short_leg=Candidate(
                occ_symbol=short_contract.symbol,
                strike=short_strike,
                expiration_date=chosen_expiry,
            ),
            long_leg=Candidate(
                occ_symbol=long_contract.symbol,
                strike=long_strike,
                expiration_date=chosen_expiry,
            ),
            short_leg_delta=short_delta,
        ))

    if not candidates:
        logger.warning(
            f"No put-spread pairs for {symbol} — could not match long legs "
            f"${spread_width:.2f} below the short strikes (expiry {chosen_expiry})."
        )
        return None

    leg_symbols = []
    for c in candidates:
        leg_symbols.append(c.short_leg.occ_symbol)
        leg_symbols.append(c.long_leg.occ_symbol)
    try:
        quote_map = quote_lookup(leg_symbols)
    except Exception as e:
        logger.error(f"Quote lookup failed for {symbol} put spread: {e}")
        return None
    quotes: dict[str, Quote] = {
        occ: q for occ, q in quote_map.items() if q is not None
    }

    result = rank_put_spread_candidates(
        candidates,
        quotes,
        target_short_delta=target_short_delta,
        target_dte=target_dte,
        max_loss_per_position=max_loss_per_position,
        min_credit_pct_of_width=min_credit_pct_of_width,
    )

    if result.best is None:
        rejected_summary = "; ".join(
            f"{c.short_leg.occ_symbol}/{c.long_leg.occ_symbol}: {r}"
            for c, r in result.rejected
        ) or "no candidates"
        logger.warning(
            f"No tradeable put spread for {symbol} "
            f"(expiry {chosen_expiry}, target Δ {target_short_delta:.2f}) — "
            f"{rejected_summary}"
        )
        return None

    top = result.best
    runners_up = result.picks[1:4]
    runner_str = "; ".join(
        f"{p.short_occ}/{p.long_occ} score={p.score:.2f}" for p in runners_up
    ) or "none"
    logger.info(
        f"Resolved Put Spread: {top.short_occ} / {top.long_occ} "
        f"strikes=${top.candidate.short_leg.strike:.2f}/"
        f"${top.candidate.long_leg.strike:.2f} expiry={chosen_expiry} "
        f"width=${top.width:.2f} net_credit=${top.net_credit:.2f}/sh "
        f"max_loss=${top.max_loss:,.0f} shortΔ={top.candidate.short_leg_delta:.3f} "
        f"score={top.score:.2f} "
        f"[delta={top.components['short_delta']:.2f} "
        f"credit={top.components['net_credit']:.2f} "
        f"spread={top.components['spread_quality']:.2f} "
        f"dte={top.components['dte']:.2f}] runners_up=[{runner_str}]"
    )

    return SpreadPick(
        short_occ=top.short_occ,
        long_occ=top.long_occ,
        short_strike=top.candidate.short_leg.strike,
        long_strike=top.candidate.long_leg.strike,
        expiration_date=chosen_expiry,
        width=top.width,
        net_credit=top.net_credit,
        max_loss=top.max_loss,
        short_leg_delta=top.candidate.short_leg_delta,
        score=top.score,
        components=dict(top.components),
        runners_up=runners_up,
    )
