"""
Position abstraction (Phase 11.27).

Generalizes the engine from `dict[symbol, strategy_name]` ownership to a
`Position`-based model keyed by `position_id`. A single logical position can
carry one or many legs:

  - single-leg equity   → one PositionLeg, symbol = ticker
  - single-leg option   → one PositionLeg, symbol = OCC string
  - spread (future)     → two or more PositionLegs sharing a position_id

For single-leg positions, `position_id` equals the legacy `owner_key`:
  - equity:  position_id = ticker (e.g. "AAPL")
  - option:  position_id = underlying ticker (e.g. "SPY"), leg symbol = OCC
The DB backfill mirrors this: legacy equity rows get
`position_id = symbol`, and legacy option rows get
`position_id = owner_key_for(symbol)` (the underlying), so engine
lookups and stored rows key on the same value.

Spread support arrives in PR 2/3. PR 1 only ships the abstraction so that
the engine no longer cares whether a position has one leg or many.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Iterable

# Re-exported from utils.option_symbols so callers that don't want to pull
# in the engine package can use the same normalizer.
from utils.option_symbols import (
    is_occ_option,
    owner_key_for as _owner_key_for,
)


# ── Constants ───────────────────────────────────────────────────────────────


SINGLE_LEG = "single_leg"
SPREAD = "spread"


VALID_POSITION_TYPES = frozenset({SINGLE_LEG, SPREAD})

# Order/leg side. Stored uppercase so spread net-credit math (which keys on
# side == "SELL") cannot be silently broken by callers passing 'sell' / 'Sell'.
BUY = "BUY"
SELL = "SELL"
VALID_SIDES = frozenset({BUY, SELL})

CONTRACT_MULTIPLIER = 100


# ── Dataclasses ─────────────────────────────────────────────────────────────


@dataclass
class PositionLeg:
    """
    One leg of a position. Equity positions and single-leg option positions
    have exactly one leg; multi-leg option strategies (e.g. credit spreads)
    have two or more.

    Attributes:
        symbol:       OCC string for options, ticker for equities.
        qty:          Signed share/contract quantity (positive = long).
        entry_price:  Per-share fill price at open (None if unknown).
        entry_time:   Fill timestamp (None if unknown).
        side:         "BUY" or "SELL" at open. Case-insensitive on input
                      (e.g. "sell" / "Sell") but normalized to uppercase
                      so net-credit math cannot silently flip sign.
    """

    symbol: str
    qty: float
    entry_price: float | None = None
    entry_time: datetime | None = None
    side: str = BUY

    def __post_init__(self) -> None:
        if not isinstance(self.side, str):
            raise TypeError(
                f"side must be a string, got {type(self.side).__name__}"
            )
        normalized = self.side.upper()
        if normalized not in VALID_SIDES:
            raise ValueError(
                f"side must be one of {sorted(VALID_SIDES)}, got {self.side!r}"
            )
        self.side = normalized


@dataclass
class Position:
    """
    A logical position composed of one or more legs, owned by one strategy.

    The ``position_id`` is the engine's primary key in ``_positions``:
      - For single-leg positions, it equals the equity ticker or option
        underlying (i.e. ``owner_key_for(symbol)``).
      - For spreads (PR 2/3), it is a UUID assigned at submission time.

    Attributes:
        position_id:    Stable identifier across legs.
        position_type:  "single_leg" or "spread".
        strategy_name:  Owning strategy (e.g. "rsi_reversion").
        legs:           One or more PositionLeg entries.
    """

    position_id: str
    position_type: str
    strategy_name: str
    legs: list[PositionLeg] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.position_type not in VALID_POSITION_TYPES:
            raise ValueError(
                f"position_type must be one of {sorted(VALID_POSITION_TYPES)}, "
                f"got {self.position_type!r}"
            )
        if self.position_type == SINGLE_LEG and len(self.legs) > 1:
            raise ValueError(
                "single_leg position cannot have more than one leg"
            )
        if not self.position_id:
            raise ValueError("position_id must be a non-empty string")
        if not self.strategy_name:
            raise ValueError("strategy_name must be a non-empty string")

    # ── Convenience accessors ───────────────────────────────────────────

    @property
    def is_single_leg(self) -> bool:
        return self.position_type == SINGLE_LEG

    @property
    def is_spread(self) -> bool:
        return self.position_type == SPREAD

    @property
    def primary_leg(self) -> PositionLeg | None:
        """The first leg, or None if no legs have been recorded yet."""
        return self.legs[0] if self.legs else None

    @property
    def entry_price(self) -> float | None:
        """
        Entry price summary used by realized-P&L bookkeeping.

        For single-leg: the leg's entry price.
        For spread:     the net credit per contract (positive number),
                        computed as the sum of leg-signed entry prices.
                        Returns None if any leg lacks an entry price.
        """
        if not self.legs:
            return None
        if self.is_single_leg:
            return self.legs[0].entry_price
        # Spread: net credit = short premium - long premium.
        # Convention: a leg opened by SELL contributes +entry_price,
        # a leg opened by BUY contributes -entry_price. Side values are
        # normalized to uppercase in PositionLeg.__post_init__ so the
        # comparison below cannot be sidestepped by a 'sell' / 'Sell'.
        prices = []
        for leg in self.legs:
            if leg.entry_price is None:
                return None
            sign = 1.0 if leg.side == SELL else -1.0
            prices.append(sign * leg.entry_price)
        return float(sum(prices))

    def symbols(self) -> list[str]:
        """All leg symbols (OCC strings or tickers)."""
        return [leg.symbol for leg in self.legs]


# ── Helpers ─────────────────────────────────────────────────────────────────


def owner_key_for(symbol: str) -> str:
    """
    Compute the engine's owner_key for a raw broker symbol.

    Delegates to :func:`utils.option_symbols.owner_key_for`. Re-exported
    here so engine call sites can keep the historical import path.

    For single-leg positions, owner_key == position_id by convention.
    """
    return _owner_key_for(symbol)


def make_single_leg(
    *,
    strategy_name: str,
    symbol: str,
    qty: float = 0.0,
    entry_price: float | None = None,
    entry_time: datetime | None = None,
    side: str = "BUY",
) -> Position:
    """
    Construct a single-leg Position. ``position_id`` is derived from the
    symbol (underlying for OCC option strings, ticker for equities) to
    preserve the existing engine keying.
    """
    position_id = owner_key_for(symbol)
    leg = PositionLeg(
        symbol=symbol,
        qty=qty,
        entry_price=entry_price,
        entry_time=entry_time,
        side=side,
    )
    return Position(
        position_id=position_id,
        position_type=SINGLE_LEG,
        strategy_name=strategy_name,
        legs=[leg],
    )


def new_spread_id() -> str:
    """Allocate a fresh UUID for a multi-leg position (used by PR 2/3)."""
    return uuid.uuid4().hex


def make_spread(
    *,
    strategy_name: str,
    position_id: str,
    legs: list[PositionLeg],
) -> Position:
    """
    Construct a multi-leg (spread) Position.

    Unlike ``make_single_leg``, the ``position_id`` is caller-supplied — it
    is a UUID (see ``new_spread_id``), not derived from a symbol, because a
    spread spans multiple OCC contracts and has no single owner_key.
    """
    if len(legs) < 2:
        raise ValueError(
            f"make_spread requires at least 2 legs, got {len(legs)}"
        )
    return Position(
        position_id=position_id,
        position_type=SPREAD,
        strategy_name=strategy_name,
        legs=list(legs),
    )


def view_owner_map(positions: Iterable[Position]) -> dict[str, str]:
    """
    Build the legacy ``dict[owner_key, strategy_name]`` view from Position
    records. Used to bridge the old SleeveAllocator interface in PR 1 without
    forcing an allocator refactor.

    Spreads contribute one entry per position_id (since the allocator counts
    *positions* per strategy, not legs).
    """
    return {pos.position_id: pos.strategy_name for pos in positions}


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def broker_position_current_price(symbol: str, broker_position: Any) -> float | None:
    """
    Best-effort current per-share/contract price from a broker position object.

    Alpaca reports option market value at contract multiplier scale
    (premium × qty × 100). If ``current_price`` is unavailable, infer option
    premium by dividing market value by qty and by ``CONTRACT_MULTIPLIER``.
    """
    if broker_position is None:
        return None
    explicit = _as_float(_field(broker_position, "current_price"))
    if explicit is not None:
        return explicit
    qty = _as_float(_field(broker_position, "qty"))
    market_value = _as_float(_field(broker_position, "market_value"))
    if qty in (None, 0.0) or market_value is None:
        return None
    divisor = qty * (CONTRACT_MULTIPLIER if is_occ_option(symbol) else 1.0)
    if divisor == 0:
        return None
    return abs(market_value / divisor)


def _leg_snapshot(
    *,
    symbol: str,
    role: str,
    side: str,
    qty: float,
    broker_positions: dict[str, Any],
) -> dict[str, Any]:
    broker_pos = broker_positions.get(symbol)
    return {
        "symbol": symbol,
        "role": role,
        "side": side,
        "qty": qty,
        "avg_entry_price": _as_float(_field(broker_pos, "avg_entry_price")),
        "current_price": broker_position_current_price(symbol, broker_pos),
        "market_value": _as_float(_field(broker_pos, "market_value")),
        "unrealized_pnl": _as_float(
            _field(
                broker_pos,
                "unrealized_pl",
                _field(broker_pos, "unrealized_pnl"),
            )
        ),
    }


def _credit_spread_status(
    *,
    entry_net_price: float,
    current_exit_price: float | None,
    underlying_price: float | None,
    short_strike: float,
    dte: int,
    stop_loss_multiple: float,
    time_stop_dte: int | None,
) -> str:
    if underlying_price is not None and underlying_price <= short_strike:
        return "tested"
    if (
        current_exit_price is not None
        and entry_net_price > 0
        and current_exit_price >= stop_loss_multiple * entry_net_price
    ):
        return "tested"
    if time_stop_dte is not None and dte <= time_stop_dte:
        return "watch"
    if (
        current_exit_price is not None
        and entry_net_price > 0
        and current_exit_price > entry_net_price
    ):
        return "watch"
    if underlying_price is not None and underlying_price > 0:
        distance_pct = (underlying_price - short_strike) / underlying_price
        if distance_pct <= 0.03:
            return "watch"
    return "healthy"


def build_credit_spread_snapshot(
    *,
    position_id: str,
    strategy: str,
    underlying: str,
    short_occ: str,
    long_occ: str,
    short_strike: float,
    long_strike: float,
    expiration: date | str,
    entry_net_price: float,
    width: float,
    qty: int | float,
    broker_positions: dict[str, Any] | None = None,
    underlying_price: float | None = None,
    pending_close: bool = False,
    today: date | None = None,
    stop_loss_multiple: float = 2.0,
    time_stop_dte: int | None = None,
) -> dict[str, Any]:
    """
    Build a normalized multi-leg valuation snapshot for a bull put credit spread.

    The returned dict is intentionally JSON-serializable so it can be written
    directly into ``engine_state.json`` and rendered by the dashboard.
    """
    broker_positions = broker_positions or {}
    today = today or datetime.now(timezone.utc).date()
    expiration_date = (
        expiration
        if isinstance(expiration, date)
        else date.fromisoformat(str(expiration))
    )
    qty_f = abs(float(qty))
    entry = float(entry_net_price)
    width_f = float(width)
    max_profit = entry * CONTRACT_MULTIPLIER * qty_f
    max_loss = max(0.0, width_f - entry) * CONTRACT_MULTIPLIER * qty_f

    short_leg = _leg_snapshot(
        symbol=short_occ,
        role="short",
        side=SELL,
        qty=-qty_f,
        broker_positions=broker_positions,
    )
    long_leg = _leg_snapshot(
        symbol=long_occ,
        role="long",
        side=BUY,
        qty=qty_f,
        broker_positions=broker_positions,
    )
    short_price = short_leg["current_price"]
    long_price = long_leg["current_price"]
    current_exit_price = (
        max(0.0, short_price - long_price)
        if short_price is not None and long_price is not None
        else None
    )
    unrealized_pnl = (
        (entry - current_exit_price) * CONTRACT_MULTIPLIER * qty_f
        if current_exit_price is not None
        else None
    )
    dte = (expiration_date - today).days
    distance = (
        underlying_price - float(short_strike)
        if underlying_price is not None
        else None
    )
    distance_pct = (
        distance / underlying_price
        if distance is not None and underlying_price not in (None, 0.0)
        else None
    )
    status = _credit_spread_status(
        entry_net_price=entry,
        current_exit_price=current_exit_price,
        underlying_price=underlying_price,
        short_strike=float(short_strike),
        dte=dte,
        stop_loss_multiple=float(stop_loss_multiple),
        time_stop_dte=time_stop_dte,
    )

    return {
        "position_id": position_id,
        "strategy": strategy,
        "structure": "put_credit_spread",
        "underlying": underlying,
        "short_occ": short_occ,
        "long_occ": long_occ,
        "qty": qty_f,
        "expiration": expiration_date.isoformat(),
        "dte": dte,
        "pending_close": bool(pending_close),
        "entry_net_price": entry,
        "current_exit_price": current_exit_price,
        "unrealized_pnl": unrealized_pnl,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "risk_used": max_loss,
        "underlying_price": underlying_price,
        "short_strike": float(short_strike),
        "long_strike": float(long_strike),
        "width": width_f,
        "distance_to_short_strike": distance,
        "distance_to_short_strike_pct": distance_pct,
        "status": status,
        "legs": [short_leg, long_leg],
    }
