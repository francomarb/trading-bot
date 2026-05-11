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
    rank_call_candidates,
)

_client: TradingClient | None = None

_STRIKE_WINDOW_PCT = 0.03
_PAGE_LIMIT = 200
_MAX_PAGES = 10
_TOP_K_TO_QUOTE = 5  # cap quote fetches at this many strike-nearest candidates


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
