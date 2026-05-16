"""
Entry price guard — pure helper for PLAN 11.32.

Translates a per-strategy `EntryPriceCap` policy into a concrete worst-case
fill price ("cap") that the broker can enforce as a marketable DAY LIMIT.

Background
----------
The QCOM 2026-05-11 incident: a Donchian MARKET BUY priced from a $219 signal
close filled at ~$245 (+1205 bps) after an overnight gap. Risk sizing and the
ATR stop had been derived from $219; the actual fill produced a position
several R larger than intended. There is no native Alpaca knob that caps a
market order at submit time — see PLAN 11.32 + the linked research. The
community-endorsed pattern is to convert the market entry into a marketable
DAY LIMIT at a small chase above the reference, which the exchange enforces
as a hard ceiling.

This module is intentionally pure: it does not read quotes, broker state, or
config files. It accepts a signal-shaped tuple and a policy, returns a
decision the engine can act on. Wiring lives in the engine.

Scope (v1)
----------
- Equity MARKET entries only. LIMIT signals (RSI), spread combos (credit
  spread), and the options worker premium-cap path are unaffected — they
  already carry their own price envelopes.
- Whole-share OTO path only. The fractional path is documented as a known
  gap with low blast radius and may get the same treatment in a follow-up.

Risk caveat
-----------
The cap bounds *entry slippage*, not *risk per trade*. If the order fills at
the cap, the dollar distance from fill to the ATR stop (which is still
derived from the reference price by RiskManager) widens — by up to
`cap - reference` in absolute terms. With the recommended 500 bps / 2.0 ATR
cap and a 2.0 ATR stop, worst-case R inflation at the cap is ~2x. That is
far better than the unbounded inflation seen in the QCOM incident, and a
later iteration can re-anchor the stop on the cap if the residual R drift
proves to matter in paper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Literal


class CapAction(str, Enum):
    """What the engine should do given the policy + reference."""

    SUBMIT_AS_IS = "submit_as_is"          # no policy, or no cap applies
    CONVERT_TO_LIMIT = "convert_to_limit"  # submit as marketable DAY LIMIT at cap_price


@dataclass(frozen=True)
class EntryPriceCap:
    """
    Per-strategy entry-chase policy.

    `max_chase_bps` and `max_chase_atr_fraction` may be set independently or
    together. When both are set the *tighter* cap wins — a low-priced high-vol
    name should be capped in ATR units (so a 5% chase isn't allowed just
    because 500 bps is the absolute floor), while a mega-cap should be capped
    in bps (so a tight ATR doesn't artificially gate a small chase).

    on_breach="skip" is reserved for future use; v1 always converts.
    """

    max_chase_bps: float | None = None
    max_chase_atr_fraction: float | None = None
    on_breach: Literal["convert_to_limit", "skip"] = "convert_to_limit"

    def __post_init__(self) -> None:
        if self.max_chase_bps is None and self.max_chase_atr_fraction is None:
            raise ValueError(
                "EntryPriceCap requires at least one of "
                "max_chase_bps or max_chase_atr_fraction"
            )
        if self.max_chase_bps is not None and self.max_chase_bps <= 0:
            raise ValueError(f"max_chase_bps must be > 0, got {self.max_chase_bps}")
        if self.max_chase_atr_fraction is not None and self.max_chase_atr_fraction <= 0:
            raise ValueError(
                f"max_chase_atr_fraction must be > 0, got {self.max_chase_atr_fraction}"
            )
        if self.on_breach not in ("convert_to_limit", "skip"):
            raise ValueError(f"unknown on_breach: {self.on_breach!r}")


@dataclass(frozen=True)
class CapDecision:
    """
    What the engine should do for one entry signal.

    `cap_price` is the concrete worst-case fill price. The broker is expected
    to submit a DAY LIMIT BUY at exactly this price (for SELL: at exactly
    this price as a sell-side floor; v1 is long-only so the SELL path is
    untested and the engine should not route SELL entries through this guard).

    `diagnostics` is dict-of-floats for logging / dashboard observability.
    """

    action: CapAction
    cap_price: float | None
    diagnostics: dict = field(default_factory=dict)


def compute_cap_price(
    *,
    reference_price: float,
    atr: float,
    side: Literal["buy", "sell"],
    policy: EntryPriceCap,
) -> float:
    """
    Return the concrete cap price for a BUY (ceiling) or SELL (floor).

    Picks the tighter of the bps and ATR knobs when both are set. Caller
    must pass strictly positive `reference_price` and non-negative `atr`.
    """
    if reference_price <= 0:
        raise ValueError(f"reference_price must be > 0, got {reference_price}")
    if atr < 0:
        raise ValueError(f"atr must be >= 0, got {atr}")

    candidates: list[float] = []
    if policy.max_chase_bps is not None:
        candidates.append(reference_price * policy.max_chase_bps / 1e4)
    if policy.max_chase_atr_fraction is not None:
        candidates.append(policy.max_chase_atr_fraction * atr)

    # Defensive: __post_init__ guarantees at least one knob.
    chase = min(candidates) if candidates else 0.0

    if side == "buy":
        return reference_price + chase
    if side == "sell":
        return reference_price - chase
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


def gate_entry(
    *,
    reference_price: float,
    atr: float,
    side: Literal["buy", "sell"],
    order_type: Literal["market", "limit"],
    policy: EntryPriceCap | None,
) -> CapDecision:
    """
    Decide what to do with one entry signal.

    Returns SUBMIT_AS_IS (no change) when:
      - no policy is configured for this strategy, OR
      - the signal is already a LIMIT (the strategy is capping itself).

    Returns CONVERT_TO_LIMIT with a concrete `cap_price` otherwise. The
    engine is responsible for stamping `entry_max_price` on the resulting
    RiskDecision; the broker is responsible for honoring it as a DAY LIMIT
    + OTO with the cap as the limit price.
    """
    if policy is None or order_type == "limit":
        return CapDecision(action=CapAction.SUBMIT_AS_IS, cap_price=None)

    cap_price = compute_cap_price(
        reference_price=reference_price,
        atr=atr,
        side=side,
        policy=policy,
    )

    diagnostics: dict = {
        "reference_price": reference_price,
        "atr": atr,
        "cap_price": cap_price,
        "chase_bps": (cap_price / reference_price - 1.0) * 1e4
        if side == "buy"
        else (1.0 - cap_price / reference_price) * 1e4,
    }
    if policy.max_chase_bps is not None:
        diagnostics["policy_max_chase_bps"] = policy.max_chase_bps
    if policy.max_chase_atr_fraction is not None:
        diagnostics["policy_max_chase_atr_fraction"] = policy.max_chase_atr_fraction

    return CapDecision(
        action=CapAction.CONVERT_TO_LIMIT,
        cap_price=cap_price,
        diagnostics=diagnostics,
    )
