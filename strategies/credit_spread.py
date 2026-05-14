"""
Credit spread strategy (Phase 11.29) — underlying-agnostic bull put credit
spreads.

One ``CreditSpread`` instance runs per underlying, configured from a
``CREDIT_SPREAD_INSTRUMENTS`` block. All instances share the ``credit_spread``
allocator sleeve (``name`` is a fixed class attribute) — the per-instance
identity is the configured underlying symbol.

This module is the *logic* layer (PR 3a). It is fully unit-testable but is
**not engine-wired** — nothing calls ``build_spread_execution`` /
``should_exit_spread`` / ``register_spread`` yet. The engine MLEG-dispatch
path, the ``forward_test.py`` slot, the ``STRATEGY_ALLOCATIONS`` rebalance,
and the dashboard rendering all land in PR 3b.

Interface the engine (PR 3b) will wire:

  * ``_raw_signals`` — permissive: every bar is a candidate entry. The real
    gating is the edge filter (trend + IV + earnings), the per-instance
    position caps, and chain/spread availability.
  * ``build_spread_execution(underlying_price, *, notional_cap,
    total_open_credit_spreads)`` — runs the caps, the multi-leg picker, and
    returns a ``SpreadExecutionPlan`` (legs + qty + a *negative* limit price,
    per the Alpaca MLEG credit convention). Raises ``CreditSpreadRejected``
    when no entry is available.
  * ``should_exit_spread(spread, *, spread_mid, underlying_close, today)`` —
    evaluates the profit-target / stop-loss / time-stop / short-strike-breach
    exit triggers. The regime-exit override is an engine-level concern.
  * ``register_spread`` / ``release_spread`` — the engine keeps the strategy's
    per-instance open-position view in sync as fills and closes happen.

See docs/credit_spread_strategy.md for the full design.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone

import pandas as pd
from loguru import logger

from execution.options_executor import SpreadLeg
from risk.manager import Side
from strategies.base import BaseStrategy, EdgeFilter, SignalFrame
from utils.iv_proxy import IVProxyResolver
from utils.options_lookup import SpreadPick, find_best_put_spread
from utils.options_ranker import Quote


# Quote-lookup callback: list[occ] → {occ: Quote | None}.
from typing import Callable

QuoteLookup = Callable[[list[str]], dict[str, "Quote | None"]]


_CONTRACT_MULTIPLIER = 100
_REQUIRED_BARS = 60  # enough history for the edge filter's 50-day SMA


class CreditSpreadRejected(Exception):
    """Raised by ``build_spread_execution`` when no entry is available."""


# ── Config ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CreditSpreadConfig:
    """
    Typed view of one ``CREDIT_SPREAD_INSTRUMENTS`` block.

    ``from_dict`` validates that every required key is present and coerces
    types — a config typo fails loudly here, not silently at first trade.
    """

    symbol: str
    short_leg_delta: float
    spread_width: float
    dte_min: int
    dte_max: int
    iv_proxy_source: str
    min_iv_proxy: float
    min_credit_pct_of_width: float
    max_concurrent_positions: int
    max_per_expiration: int
    min_dte_gap_between_opens: int
    profit_target_pct: float
    stop_loss_multiple: float
    time_stop_dte: int
    exit_on_short_strike_breach: bool
    limit_timeout_seconds: int
    earnings_blackout_days: int

    _REQUIRED_KEYS = frozenset({
        "short_leg_delta", "spread_width", "dte_min", "dte_max",
        "iv_proxy_source", "min_iv_proxy", "min_credit_pct_of_width",
        "max_concurrent_positions", "max_per_expiration",
        "min_dte_gap_between_opens", "profit_target_pct", "stop_loss_multiple",
        "time_stop_dte", "exit_on_short_strike_breach", "limit_timeout_seconds",
        "earnings_blackout_days",
    })

    @classmethod
    def from_dict(cls, symbol: str, raw: dict) -> "CreditSpreadConfig":
        """Build a config from a raw instrument dict, validating keys."""
        missing = cls._REQUIRED_KEYS - raw.keys()
        if missing:
            raise ValueError(
                f"CreditSpreadConfig for {symbol!r} is missing required "
                f"key(s): {sorted(missing)}"
            )
        return cls(
            symbol=symbol,
            short_leg_delta=float(raw["short_leg_delta"]),
            spread_width=float(raw["spread_width"]),
            dte_min=int(raw["dte_min"]),
            dte_max=int(raw["dte_max"]),
            iv_proxy_source=str(raw["iv_proxy_source"]),
            min_iv_proxy=float(raw["min_iv_proxy"]),
            min_credit_pct_of_width=float(raw["min_credit_pct_of_width"]),
            max_concurrent_positions=int(raw["max_concurrent_positions"]),
            max_per_expiration=int(raw["max_per_expiration"]),
            min_dte_gap_between_opens=int(raw["min_dte_gap_between_opens"]),
            profit_target_pct=float(raw["profit_target_pct"]),
            stop_loss_multiple=float(raw["stop_loss_multiple"]),
            time_stop_dte=int(raw["time_stop_dte"]),
            exit_on_short_strike_breach=bool(raw["exit_on_short_strike_breach"]),
            limit_timeout_seconds=int(raw["limit_timeout_seconds"]),
            earnings_blackout_days=int(raw["earnings_blackout_days"]),
        )


# ── Position + execution-plan records ───────────────────────────────────────


@dataclass
class OpenSpread:
    """
    One credit spread this strategy instance currently holds open.

    The engine (PR 3b) keeps this view in sync via ``register_spread`` /
    ``release_spread`` as combo fills and closes arrive.
    """

    position_id: str
    short_occ: str
    long_occ: str
    short_strike: float
    long_strike: float
    expiration_date: date
    net_credit: float            # $/share collected at open
    width: float
    qty: int
    opened_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


@dataclass(frozen=True)
class SpreadExecutionPlan:
    """
    What ``build_spread_execution`` returns — the engine submits this via
    ``broker.place_spread_order`` / ``SpreadExecutionWorker``.

    ``limit_price`` is **negative** — the Alpaca MLEG convention is that a
    negative limit is a net credit required (confirmed by the 11.28 merge
    gate). It equals ``-net_credit`` rounded to cents.
    """

    legs: list[SpreadLeg]
    qty: int
    limit_price: float           # negative = net credit required
    short_occ: str
    long_occ: str
    short_strike: float
    long_strike: float
    expiration_date: date
    net_credit: float            # $/share, positive
    max_loss: float              # $ per contract
    width: float


# ── Strategy ────────────────────────────────────────────────────────────────


class CreditSpread(BaseStrategy):
    """
    Underlying-agnostic bull put credit spread strategy.

    All instances share the ``credit_spread`` allocator sleeve; the
    configured underlying (``config.symbol``) is the per-instance identity.
    """

    name = "credit_spread"

    def __init__(
        self,
        config: CreditSpreadConfig,
        *,
        edge_filter: EdgeFilter | None = None,
        iv_resolver: IVProxyResolver | None = None,
        quote_lookup: QuoteLookup | None = None,
    ) -> None:
        super().__init__(edge_filter=edge_filter)
        self.config = config
        self.symbol = config.symbol
        self._iv_resolver = iv_resolver or IVProxyResolver()
        # Production wires the real OPRA snapshot lookup in PR 3b; tests inject
        # a stub. None is allowed at construction so the strategy can be built
        # for unit tests that never reach build_spread_execution.
        self._quote_lookup = quote_lookup
        # Per-instance open positions, kept in sync by the engine (PR 3b).
        self._open_spreads: dict[str, OpenSpread] = {}

    def required_bars(self) -> int:
        return _REQUIRED_BARS

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        """
        Permissive base signal: every bar is a candidate entry.

        A credit spread has no price-crossing trigger — selling premium is
        opportunistic. The real gating is the edge filter (trend + IV +
        earnings), the per-instance position caps, and chain/spread
        availability — all applied downstream. Exits are never signalled
        here; they run through ``should_exit_spread``.
        """
        if "close" not in df.columns:
            raise ValueError("CreditSpread requires a 'close' column")
        entries = pd.Series(True, index=df.index, dtype=bool)
        exits = pd.Series(False, index=df.index, dtype=bool)
        return SignalFrame(entries=entries, exits=exits)

    # ── Open-position bookkeeping (engine callbacks, PR 3b) ──────────────

    def register_spread(self, spread: OpenSpread) -> None:
        """Record a newly-opened spread. Idempotent on ``position_id``."""
        self._open_spreads[spread.position_id] = spread

    def release_spread(self, position_id: str) -> OpenSpread | None:
        """Drop a closed spread; returns the removed record (or None)."""
        return self._open_spreads.pop(position_id, None)

    @property
    def open_spreads(self) -> list[OpenSpread]:
        """The spreads this instance currently holds open."""
        return list(self._open_spreads.values())

    # ── Entry caps ───────────────────────────────────────────────────────

    def _caps_reject_reason(
        self,
        *,
        target_expiration: date,
        total_open_credit_spreads: int,
    ) -> str | None:
        """
        Return a human-readable reason if any position cap blocks a new entry
        on ``target_expiration``, else None.

        Enforced here (per-instance): max_concurrent_positions,
        max_per_expiration, min_dte_gap_between_opens. The global
        MAX_TOTAL_CONCURRENT_CREDIT_SPREADS is passed in by the engine
        (PR 3b) since one instance cannot see the others.
        """
        from config.settings import MAX_TOTAL_CONCURRENT_CREDIT_SPREADS

        if total_open_credit_spreads >= MAX_TOTAL_CONCURRENT_CREDIT_SPREADS:
            return (
                f"global cap reached "
                f"({total_open_credit_spreads}/{MAX_TOTAL_CONCURRENT_CREDIT_SPREADS} "
                "concurrent credit spreads)"
            )

        open_spreads = self._open_spreads.values()
        if len(self._open_spreads) >= self.config.max_concurrent_positions:
            return (
                f"per-instance cap reached "
                f"({len(self._open_spreads)}/{self.config.max_concurrent_positions} "
                f"open on {self.symbol})"
            )

        same_expiry = sum(
            1 for s in open_spreads if s.expiration_date == target_expiration
        )
        if same_expiry >= self.config.max_per_expiration:
            return (
                f"max_per_expiration reached ({same_expiry}/"
                f"{self.config.max_per_expiration} on {target_expiration})"
            )

        # DTE staggering — the new expiration must be at least
        # min_dte_gap_between_opens days from the most recently opened spread.
        gap = self.config.min_dte_gap_between_opens
        if gap > 0 and self._open_spreads:
            most_recent = max(open_spreads, key=lambda s: s.opened_at)
            delta_days = abs((target_expiration - most_recent.expiration_date).days)
            if delta_days < gap:
                return (
                    f"DTE stagger: new expiry {target_expiration} is {delta_days}d "
                    f"from the most recent open ({most_recent.expiration_date}), "
                    f"need ≥ {gap}d"
                )

        return None

    # ── Entry execution ──────────────────────────────────────────────────

    def build_spread_execution(
        self,
        underlying_price: float,
        *,
        notional_cap: float,
        total_open_credit_spreads: int = 0,
    ) -> SpreadExecutionPlan:
        """
        Select a bull put credit spread and build the order plan.

        ``notional_cap`` is the sleeve's per-position dollar budget — for a
        defined-risk spread this is the collateral, i.e. the max loss cap
        passed to the picker. ``total_open_credit_spreads`` is the live
        global count across all credit-spread instances (the engine supplies
        it in PR 3b; defaults to 0 for unit tests of a single instance).

        Raises ``CreditSpreadRejected`` if a cap blocks the entry, no
        tradeable spread is available, or the picked spread violates a
        per-expiration / staggering cap.
        """
        if notional_cap is None or notional_cap <= 0:
            raise CreditSpreadRejected(
                f"{self.symbol}: notional_cap=${notional_cap} — sleeve has no room."
            )
        if self._quote_lookup is None:
            raise CreditSpreadRejected(
                f"{self.symbol}: no quote_lookup wired — cannot price the chain."
            )

        # Cheap caps first — concurrent/global caps don't need a chain query.
        # (The per-expiration / staggering caps need the picked expiration,
        # so they are re-checked after the picker runs.)
        early_reject = self._caps_reject_reason(
            target_expiration=date.max,  # placeholder — expiry caps re-checked below
            total_open_credit_spreads=total_open_credit_spreads,
        )
        # date.max can't trip the per-expiration / stagger caps, so a
        # non-None reason here is a true concurrent/global-cap block.
        if early_reject is not None and (
            "global cap" in early_reject or "per-instance cap" in early_reject
        ):
            raise CreditSpreadRejected(f"{self.symbol}: {early_reject}")

        iv_points = self._iv_resolver.resolve(self.config.iv_proxy_source)
        pick: SpreadPick | None = find_best_put_spread(
            self.symbol,
            underlying_price,
            min_dte=self.config.dte_min,
            max_dte=self.config.dte_max,
            spread_width=self.config.spread_width,
            target_short_delta=self.config.short_leg_delta,
            iv=iv_points / 100.0,  # picker wants a decimal sigma
            max_loss_per_position=notional_cap,
            min_credit_pct_of_width=self.config.min_credit_pct_of_width,
            quote_lookup=self._quote_lookup,
        )
        if pick is None:
            raise CreditSpreadRejected(
                f"{self.symbol}: no tradeable put spread "
                f"(width=${self.config.spread_width:.0f}, "
                f"target Δ {self.config.short_leg_delta:.2f}, "
                f"budget ${notional_cap:,.0f})"
            )

        # Now that we know the expiration, re-check the expiry-sensitive caps.
        cap_reject = self._caps_reject_reason(
            target_expiration=pick.expiration_date,
            total_open_credit_spreads=total_open_credit_spreads,
        )
        if cap_reject is not None:
            raise CreditSpreadRejected(f"{self.symbol}: {cap_reject}")

        legs = [
            SpreadLeg(occ_symbol=pick.short_occ, side=Side.SELL, opening=True),
            SpreadLeg(occ_symbol=pick.long_occ, side=Side.BUY, opening=True),
        ]
        # Alpaca MLEG sign convention (11.28 merge gate): a negative limit
        # price is a net credit required. We demand at least the picker's
        # estimated net credit.
        limit_price = -round(pick.net_credit, 2)

        logger.info(
            f"[{self.name}] {self.symbol} spread plan: "
            f"{pick.short_occ}/{pick.long_occ} "
            f"width=${pick.width:.0f} net_credit=${pick.net_credit:.2f}/sh "
            f"max_loss=${pick.max_loss:,.0f} limit={limit_price:.2f} "
            f"shortΔ={pick.short_leg_delta:.3f} score={pick.score:.2f}"
        )
        return SpreadExecutionPlan(
            legs=legs,
            qty=1,
            limit_price=limit_price,
            short_occ=pick.short_occ,
            long_occ=pick.long_occ,
            short_strike=pick.short_strike,
            long_strike=pick.long_strike,
            expiration_date=pick.expiration_date,
            net_credit=pick.net_credit,
            max_loss=pick.max_loss,
            width=pick.width,
        )

    # ── Exit triggers ────────────────────────────────────────────────────

    def should_exit_spread(
        self,
        spread: OpenSpread,
        *,
        spread_mid: float,
        underlying_close: float,
        today: date,
    ) -> tuple[bool, str]:
        """
        Evaluate the exit triggers from docs/credit_spread_strategy.md §4 for
        one open spread. Returns ``(should_exit, reason)``.

        ``spread_mid`` is the current cost to buy the spread back (a debit,
        $/share). At open the strategy collected ``spread.net_credit``.

        Triggers (first match wins):
          * profit target — spread_mid ≤ profit_target_pct × net_credit
          * stop loss     — spread_mid ≥ stop_loss_multiple × net_credit
          * time stop     — DTE ≤ time_stop_dte
          * short breach  — underlying close ≤ short strike (if enabled)

        The regime-exit override (BEAR mid-trade) is an engine-level concern
        and is not evaluated here.
        """
        cfg = self.config
        credit = spread.net_credit

        if credit > 0 and spread_mid <= cfg.profit_target_pct * credit:
            return True, (
                f"profit target — mid ${spread_mid:.2f} ≤ "
                f"{cfg.profit_target_pct:.0%} × ${credit:.2f} credit"
            )
        if credit > 0 and spread_mid >= cfg.stop_loss_multiple * credit:
            return True, (
                f"stop loss — mid ${spread_mid:.2f} ≥ "
                f"{cfg.stop_loss_multiple:.1f}× ${credit:.2f} credit"
            )

        dte = (spread.expiration_date - today).days
        if dte <= cfg.time_stop_dte:
            return True, f"time stop — {dte} DTE ≤ {cfg.time_stop_dte}"

        if cfg.exit_on_short_strike_breach and underlying_close <= spread.short_strike:
            return True, (
                f"short strike breach — underlying ${underlying_close:.2f} ≤ "
                f"short strike ${spread.short_strike:.2f}"
            )

        return False, ""
