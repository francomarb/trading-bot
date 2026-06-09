"""
Generic walk-and-market close scheduler for multi-leg (MLEG) options strategies.

Why this module exists
----------------------
Closing a multi-leg options position by submitting a single limit at the
mid and waiting is unreliable under stressed conditions: the mid often
isn't a fillable price when the bid-ask widens, and a single 180-second
wait followed by cancel-and-retry-next-cycle leaves positions open across
cycles (sometimes across days — see Friday 2026-06-05 QQQ post-mortem).

The replacement pattern is "walk-and-market":

  1. Submit a limit at the mid.
  2. Wait some seconds. If filled, done.
  3. If not, cancel and walk the limit a bit toward the ask.
  4. Repeat through several limit prices.
  5. Eventually fall back to a market order.

The walk gives the order book real opportunities to interact at each
price level before escalating, while the market fallback guarantees an
autonomous exit without operator intervention. Per-exit-reason profiles
let urgent closes (stop-loss, time-stop) reach market faster than
patient ones (profit-target — which never escalates to market).

Reusability
-----------
This module is intentionally strategy-agnostic. Credit spread is the
first consumer; future MLEG strategies (iron condor, calendar, etc.)
plug in by emitting one of the ``MLEG_CLOSE_REASONS`` codes from
``config.settings`` and letting the engine dispatch through the same
scheduler.

The scheduler itself is **pure logic** — no broker calls, no I/O. The
executor that drives it handles submit/cancel/await/replace; the
scheduler only answers "what's the next price and how long do I wait?"
This makes it trivially unit-testable and reusable across executors.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from config import settings
from utils.safe_expr import compile_price_expression

__all__ = [
    "MlegCloseDecision",
    "MlegCloseScheduler",
    "MlegCloseStep",
    "MlegQuote",
    "resolve_mleg_close_profile",
]


# ── Data types ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MlegQuote:
    """Market data inputs needed to evaluate a price expression.

    ``mid``, ``bid``, ``ask`` are the spread's *net* prices (sum of leg
    bid-asks with the appropriate signs). The scheduler doesn't know how
    those are computed — that's the caller's job — but it relies on them
    being self-consistent (``bid <= mid <= ask``).
    """

    mid: float
    bid: float
    ask: float

    def __post_init__(self) -> None:
        # Defensive sanity check — silent inversions cause hard-to-debug
        # walk behaviour where every step looks like the same price.
        if not (self.bid <= self.mid <= self.ask):
            raise ValueError(
                f"MlegQuote violates bid<=mid<=ask: "
                f"bid={self.bid}, mid={self.mid}, ask={self.ask}"
            )

    def as_bindings(self) -> Mapping[str, float]:
        """Format for ``utils.safe_expr.compile_price_expression``."""
        return {"mid": self.mid, "bid": self.bid, "ask": self.ask}


@dataclass(frozen=True)
class MlegCloseStep:
    """A single resolved step in the walk-and-market sequence.

    Attributes:
        step_number: 1-indexed step in the profile (for logging).
        total_steps: Total number of steps in the profile (for logging).
        price_expr: The original expression string (for logging) — e.g.
            ``"mid + 0.25*(ask-mid)"`` or the sentinel ``"market"``.
        is_market: True for the final market sentinel. When True,
            ``limit_price`` is undefined; the executor submits a MARKET
            order and does not wait.
        limit_price: Computed price for this step (NaN when is_market).
        duration_seconds: Wait window before advancing to the next step
            (0 for the market step).
    """

    step_number: int
    total_steps: int
    price_expr: str
    is_market: bool
    limit_price: float
    duration_seconds: int


@dataclass(frozen=True)
class MlegCloseDecision:
    """
    A strategy's typed close decision, consumed by the engine.

    Strategies fill this in when they decide a position should close. The
    engine then:
      1. Resolves the appropriate close profile from settings (per-instrument
         override → per-strategy override → global).
      2. Optionally substitutes an end-of-session market-only profile.
      3. Constructs a ``MlegCloseScheduler``.
      4. Dispatches to the executor.

    Attributes:
        should_close: Trigger flag.
        reason: One of ``settings.MLEG_CLOSE_REASONS``. Required when
            ``should_close`` is True; ignored otherwise.
        detail: Human-readable elaboration (e.g. "stop loss — mid $4.60 ≥ 2× $2.26 credit").
        position_id: The owning position's stable identifier.
        initial_mid: Net mid at decision time (telemetry).
        initial_bid: Net bid at decision time (telemetry).
        initial_ask: Net ask at decision time (telemetry).
    """

    should_close: bool
    reason: str | None
    detail: str
    position_id: str
    initial_mid: float
    initial_bid: float
    initial_ask: float

    def __post_init__(self) -> None:
        if self.should_close:
            if self.reason is None:
                raise ValueError("MlegCloseDecision: reason required when should_close=True")
            if self.reason not in settings.MLEG_CLOSE_REASONS:
                raise ValueError(
                    f"MlegCloseDecision: unknown reason {self.reason!r}; "
                    f"must be one of {sorted(settings.MLEG_CLOSE_REASONS)}"
                )


# ── Profile resolver ────────────────────────────────────────────────────────


def resolve_mleg_close_profile(
    *,
    reason: str,
    strategy_name: str,
    instrument_overrides: dict[str, list[tuple[str, int]]] | None = None,
) -> list[tuple[str, int]]:
    """
    Look up the close profile for a ``(reason, strategy, instrument)`` combo.

    Resolution order (first match wins):
      1. ``instrument_overrides[reason]`` — per-instrument override passed
         in by the strategy (e.g. ``CREDIT_SPREAD_INSTRUMENTS["SPY"]["close_profiles"]``).
      2. ``settings.MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY[strategy_name][reason]``.
      3. ``settings.MLEG_CLOSE_PROFILES[reason]`` (global default).

    Raises:
        KeyError: if no profile is configured at any layer for ``reason``.

    The returned profile is the raw list of ``(expr, duration)`` tuples;
    the caller passes it to ``MlegCloseScheduler``.
    """
    if instrument_overrides and reason in instrument_overrides:
        return instrument_overrides[reason]

    by_strat = settings.MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY.get(strategy_name, {})
    if reason in by_strat:
        return by_strat[reason]

    if reason in settings.MLEG_CLOSE_PROFILES:
        return settings.MLEG_CLOSE_PROFILES[reason]

    raise KeyError(
        f"No MLEG close profile configured for reason={reason!r} "
        f"on strategy={strategy_name!r}"
    )


# ── Scheduler ───────────────────────────────────────────────────────────────


class MlegCloseScheduler:
    """
    Stateful iterator over a walk-and-market profile.

    Each call to ``next_step(quote)`` returns the resolved price + duration
    for the *current* step using the supplied quote. Call ``advance()`` after
    the step's wait window expires (and the order didn't fill).

    Lifecycle:
      - Construct with a profile, reason, position_id.
      - Loop: ``step = scheduler.next_step(quote)`` →
              submit limit / market →
              wait ``step.duration_seconds`` for fill →
              if filled: stop. else: ``scheduler.advance()``.
      - When ``scheduler.exhausted`` is True or the market step has been
        submitted, the close attempt is done (filled or not).

    Pure logic. No broker calls, no sleeps, no I/O.
    """

    def __init__(
        self,
        profile: list[tuple[str, int]],
        *,
        reason: str,
        position_id: str,
    ) -> None:
        if not profile:
            raise ValueError("MlegCloseScheduler: profile must be non-empty")
        # Pre-compile every non-sentinel step so a typo blows up here, not
        # mid-close. (Settings validation also runs this — belt and braces.)
        self._compiled: list[tuple[str, int, bool, Callable | None]] = []
        for expr, duration in profile:
            if expr == "market":
                self._compiled.append((expr, duration, True, None))
            else:
                fn = compile_price_expression(expr, allowed={"mid", "bid", "ask"})
                self._compiled.append((expr, duration, False, fn))
        self._reason = reason
        self._position_id = position_id
        self._current_step = 0

    # ── Read-only properties ────────────────────────────────────────────

    @property
    def reason(self) -> str:
        return self._reason

    @property
    def position_id(self) -> str:
        return self._position_id

    @property
    def total_steps(self) -> int:
        return len(self._compiled)

    @property
    def current_step_number(self) -> int:
        """1-indexed step about to be returned by next_step; total_steps+1 when exhausted."""
        return self._current_step + 1

    @property
    def exhausted(self) -> bool:
        """True once advance() has moved past the last step."""
        return self._current_step >= len(self._compiled)

    @property
    def has_market_fallback(self) -> bool:
        """True if the profile ends with a market step."""
        return self._compiled[-1][2] if self._compiled else False

    @property
    def current_step_is_market(self) -> bool:
        """
        True iff the *current* step (the one ``next_step`` would resolve) is
        the market sentinel.

        Lets the executor decide whether to fetch a quote before advancing —
        market steps don't need market data to build the request. Critical
        for the autonomous-fallback guarantee: a quote outage at the
        moment the walk reaches market must NOT cause us to skip the
        market order. Otherwise the bot's strongest exit signal becomes
        the most fragile to network conditions — exactly backwards.
        """
        if self.exhausted:
            return False
        return self._compiled[self._current_step][2]

    # ── Iteration ───────────────────────────────────────────────────────

    def next_step(self, quote: "MlegQuote | None" = None) -> MlegCloseStep | None:
        """
        Resolve and return the current step.

        Returns None if the scheduler is exhausted. Does NOT advance; the
        caller must call ``advance()`` after the step's wait window expires.

        ``quote`` is required for limit steps (the price expression needs
        ``bid``/``mid``/``ask`` bindings). For market steps it is ignored —
        callers may pass ``None``. This split lets the walk loop execute
        the market fallback even when the quote feed is temporarily down,
        preserving the autonomous-exit guarantee.

        Raises ``ValueError`` if ``quote`` is None and the current step
        is a limit step.
        """
        if self.exhausted:
            return None
        expr, duration, is_market, fn = self._compiled[self._current_step]
        if is_market:
            limit_price = float("nan")
        else:
            if quote is None:
                raise ValueError(
                    f"MlegCloseScheduler: step {self._current_step + 1} "
                    f"({expr!r}) is a limit step and requires a quote"
                )
            assert fn is not None  # defensive — compile guarantees this
            limit_price = fn(quote.as_bindings())
        return MlegCloseStep(
            step_number=self._current_step + 1,
            total_steps=len(self._compiled),
            price_expr=expr,
            is_market=is_market,
            limit_price=limit_price,
            duration_seconds=duration,
        )

    def advance(self) -> None:
        """Advance to the next step. Idempotent past the end."""
        if not self.exhausted:
            self._current_step += 1
