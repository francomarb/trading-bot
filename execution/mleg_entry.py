"""
Bounded entry walk for multi-leg (MLEG) credit spreads.

Why this module exists
----------------------
The MLEG *entry* path submits one limit at the decision-time net credit,
waits ``MLEG_ENTRY_WATCH_TIMEOUT_SECONDS``, and cancels. It never
concedes. Measured over every entry attempt in the logs from 2026-05-01
to 2026-08-11: **13 fills out of 49 attempts (27%)** — 16% in May, 33%
in June, 50% in July, 0-for-3 in August. That is not a regression; it is
the rate the strategy has always run at. Thirteen positions have ever
opened.

The close path already solved the same problem with ``mleg_close.py``'s
walk-and-market: step the limit toward the market, giving the book a real
chance to interact at several price levels instead of one. This module is
the entry-side counterpart.

What is deliberately DIFFERENT from the close walk
--------------------------------------------------
A close that does not fill leaves risk on the book, so ``mleg_close``
ends every urgent profile with a ``market`` order — the autonomous-exit
guarantee. **An entry that does not fill is a non-event.** There is
always another cycle, and no position is at risk. So the entry walk is
bounded on every axis and *cannot* reach market:

1. **No market step, ever.** A profile containing ``"market"`` is
   rejected at construction. Entry is limit-only by type, not by
   convention.
2. **Never past the credit floor.** ``min_credit_pct_of_width`` is the
   selection rule that says a spread is worth owning. Conceding below it
   would buy a fill by trading a spread the strategy already judged not
   worth having.
3. **Never past the risk budget.** Conceding credit *raises* max loss —
   ``max_loss = (width − credit) × 100 × qty`` — so the sleeve budget
   approved at decision time goes stale the moment the walk moves. The
   budget is re-derived into the floor rather than checked after the
   fact.
4. **Never below the standing bid.** Offering less credit than someone
   is already bidding concedes for nothing.

Bounds 2 and 3 collapse into a single number (``EntryWalkBounds.
credit_floor``), so there is exactly one value to reason about and one
value to test.

Sign convention
---------------
This module works in **positive credit per share** throughout — "how much
am I willing to accept" — because that is the unit the floor, the width
and the risk budget are all expressed in. Alpaca's MLEG convention
(negative limit = net credit) is applied at exactly one place,
``EntryWalkStep.limit_price``, mirroring the single negation the
single-shot path already does in ``credit_spread.py``. Keeping the walk
in credit units and negating once is the whole reason the sign is not
smeared across the loop.

Pure logic: no broker calls, no sleeps, no I/O. The executor drives it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from config import settings
from execution.mleg_close import MlegQuote
from utils.safe_expr import compile_price_expression

__all__ = [
    "EntryWalkStep",
    "EntryWalkBounds",
    "MlegEntryWalk",
    "resolve_mleg_entry_profile",
]

# Alpaca options contracts are 100 shares. Named rather than inlined so the
# max-loss algebra below reads as the same formula the strategy uses.
_CONTRACT_MULTIPLIER = 100


def _ceil_to_cents(value: float) -> float:
    """Smallest whole-cent price >= ``value``.

    The epsilon absorbs binary-float representation error only (0.13 ×
    15.0 is 1.9500000000000002, not 1.95); it is far smaller than a cent
    so it can never round a genuinely-below-floor price up past the
    bound.
    """
    return math.ceil(value * 100 - 1e-6) / 100


@dataclass(frozen=True)
class EntryWalkStep:
    """One resolved step of the entry walk.

    Attributes:
        step_number: 1-indexed position in the profile (for logging).
        total_steps: Length of the profile (for logging).
        price_expr: The original expression, e.g. ``"mid - 0.5*(mid-bid)"``.
        credit: Net credit per share this step offers. Always positive-
            credit units, always >= the binding floor.
        limit_price: ``credit`` in Alpaca MLEG convention (negative =
            net credit). This is the only place the sign flips.
        duration_seconds: How long to rest at this price before advancing.
        clamp: Why ``credit`` differs from the raw expression value —
            ``"bid"``, ``"floor"``, or None. Purely diagnostic, but it is
            the field that tells you whether the walk had any room.
    """

    step_number: int
    total_steps: int
    price_expr: str
    credit: float
    limit_price: float
    duration_seconds: int
    clamp: str | None = None

    @property
    def is_market(self) -> bool:
        """Always False, and deliberately not a field.

        ``SpreadExecutionWorker._submit_walk_step`` branches on this to
        decide between a limit and a market request. Exposing it as a
        read-only property lets the entry walk reuse that submit path
        verbatim while making the market branch *unreachable* — there is
        no assignment, constructor argument or mutation that can flip
        it. The bound is enforced by the type, not by the caller
        remembering.
        """
        return False


@dataclass(frozen=True)
class EntryWalkBounds:
    """The limits the walk may not cross, collapsed into one floor.

    ``max_loss_budget`` is the sleeve's approved per-position maximum
    loss in dollars. It is optional only so tests and any future caller
    without a sleeve can construct bounds; production always passes it.
    """

    width: float
    qty: int
    min_credit_pct_of_width: float
    max_loss_budget: float | None = None

    def __post_init__(self) -> None:
        if self.width <= 0:
            raise ValueError(f"EntryWalkBounds: width must be > 0, got {self.width}")
        if self.qty <= 0:
            raise ValueError(f"EntryWalkBounds: qty must be > 0, got {self.qty}")
        if not (0.0 <= self.min_credit_pct_of_width < 1.0):
            raise ValueError(
                "EntryWalkBounds: min_credit_pct_of_width must be in [0, 1), "
                f"got {self.min_credit_pct_of_width}"
            )

    def max_loss_at(self, credit: float) -> float:
        """Max loss in dollars if the spread is opened at ``credit``."""
        return (self.width - credit) * _CONTRACT_MULTIPLIER * self.qty

    @property
    def selection_floor(self) -> float:
        """Credit below which the spread is not worth owning."""
        return self.min_credit_pct_of_width * self.width

    @property
    def budget_floor(self) -> float:
        """Credit below which max loss would breach the approved budget.

        Solving ``(width − c) × mult × qty <= budget`` for ``c``. Returns
        ``-inf`` when no budget was supplied, so it never binds.
        """
        if self.max_loss_budget is None:
            return float("-inf")
        return self.width - self.max_loss_budget / (_CONTRACT_MULTIPLIER * self.qty)

    @property
    def credit_floor(self) -> float:
        """The tightest binding floor — the walk never offers below this."""
        return max(self.selection_floor, self.budget_floor)


def resolve_mleg_entry_profile(
    *,
    strategy_name: str,
    instrument_overrides: "list[tuple[str, int]] | None" = None,
) -> list[tuple[str, int]]:
    """Look up the entry-walk profile for a strategy.

    Resolution order (first match wins), mirroring
    ``resolve_mleg_close_profile``:
      1. ``instrument_overrides`` passed by the strategy.
      2. ``settings.MLEG_ENTRY_WALK_PROFILE_OVERRIDES_BY_STRATEGY[strategy]``.
      3. ``settings.MLEG_ENTRY_WALK_PROFILE`` (global default).
    """
    if instrument_overrides:
        return instrument_overrides
    by_strat = settings.MLEG_ENTRY_WALK_PROFILE_OVERRIDES_BY_STRATEGY.get(strategy_name)
    if by_strat:
        return by_strat
    return settings.MLEG_ENTRY_WALK_PROFILE


class MlegEntryWalk:
    """Stateful iterator over a bounded entry-walk profile.

    Lifecycle mirrors ``MlegCloseScheduler`` so the executor loops look
    alike::

        step = walk.next_step(quote)   # None => no acceptable price left
        submit limit at step.limit_price; wait step.duration_seconds
        if filled: stop
        walk.advance()

    ``next_step`` returning None is a normal, expected outcome: it means
    every remaining price would breach a bound. The caller cancels and
    gives up on this cycle — it must NOT fall back to a market order.
    """

    def __init__(
        self,
        profile: list[tuple[str, int]],
        *,
        bounds: EntryWalkBounds,
        position_id: str,
    ) -> None:
        if not profile:
            raise ValueError("MlegEntryWalk: profile must be non-empty")
        self._compiled: list[tuple[str, int, Callable]] = []
        for expr, duration in profile:
            if expr == "market":
                # The one bound that is structural rather than numeric. An
                # entry that misses is free; buying a fill at any price is
                # the failure mode this whole module exists to prevent.
                raise ValueError(
                    "MlegEntryWalk: 'market' is not a legal entry step — an "
                    "unfilled entry is a non-event, so the entry walk is "
                    "limit-only by construction (see module docstring)"
                )
            if duration <= 0:
                raise ValueError(
                    f"MlegEntryWalk: step {expr!r} needs a positive duration, "
                    f"got {duration}"
                )
            fn = compile_price_expression(expr, allowed={"mid", "bid", "ask"})
            self._compiled.append((expr, duration, fn))
        self._bounds = bounds
        self._position_id = position_id
        self._current_step = 0

    @property
    def bounds(self) -> EntryWalkBounds:
        return self._bounds

    @property
    def position_id(self) -> str:
        return self._position_id

    @property
    def total_steps(self) -> int:
        return len(self._compiled)

    @property
    def current_step_number(self) -> int:
        return self._current_step + 1

    @property
    def exhausted(self) -> bool:
        return self._current_step >= len(self._compiled)

    def advance(self) -> None:
        self._current_step += 1

    def next_step(self, quote: MlegQuote) -> EntryWalkStep | None:
        """Resolve the current step against ``quote``.

        Returns None when the walk is exhausted, or when no price at this
        step clears every bound. Does not advance.
        """
        if self.exhausted:
            return None

        # Quantise the floor UP to a whole cent before any comparison.
        # Orders are priced in cents, so a floor of 1.9500000000000002
        # (which is what 0.13 × 15.0 evaluates to in binary floating
        # point) must not make the legitimate 1.95 rung look like a
        # breach — that would silently delete the most useful step of
        # the ladder and offer 1.96 instead, every time.
        floor = _ceil_to_cents(self._bounds.credit_floor)

        # If the best the book will ever pay is under our floor, no step
        # in the remaining profile can clear it — stop now rather than
        # walking through prices that cannot fill.
        if quote.ask < floor:
            return None

        expr, duration, fn = self._compiled[self._current_step]
        raw = float(fn(quote.as_bindings()))

        credit = raw
        clamp: str | None = None
        # Never offer below the standing bid: someone is already willing
        # to pay that, so conceding past it buys nothing.
        if credit < quote.bid:
            credit, clamp = quote.bid, "bid"

        credit = round(credit, 2)
        # Never concede past the floor. Clamping (rather than bailing)
        # means the floor price itself is always offered as the last
        # useful rung of the ladder.
        if credit < floor:
            credit, clamp = floor, "floor"

        return EntryWalkStep(
            step_number=self._current_step + 1,
            total_steps=len(self._compiled),
            price_expr=expr,
            credit=credit,
            limit_price=-credit,
            duration_seconds=duration,
            clamp=clamp,
        )
