"""Pure model and exchange-calendar helpers for pre-open observations.

This module deliberately has no persistence or engine dependencies.  It owns
the reviewed v0.2 calculation contract: session-relative checkpoints,
New-York calendar localization, reusable universe identity, quote freshness,
midpoint/spread, and split-adjusted previous-close gaps.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
UTC = timezone.utc
CONTRACT_VERSION = "0.2"


@dataclass(frozen=True)
class InstrumentSpec:
    """One stock or ETF included in a prospective universe version."""

    symbol: str
    asset_type: str
    observation_role: str
    effective_from_session: date
    effective_through_session: date | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        symbol = self.symbol.strip().upper()
        if not symbol:
            raise ValueError("instrument symbol cannot be empty")
        if self.asset_type not in {"stock", "etf"}:
            raise ValueError("asset_type must be 'stock' or 'etf'")
        if not self.observation_role.strip():
            raise ValueError("observation_role cannot be empty")
        if (
            self.effective_through_session is not None
            and self.effective_through_session < self.effective_from_session
        ):
            raise ValueError("effective_through_session precedes effective_from_session")
        object.__setattr__(self, "symbol", symbol)

    def active_on(self, session_date: date) -> bool:
        """Whether this immutable spec belongs to ``session_date`` coverage."""
        return (
            self.enabled
            and session_date >= self.effective_from_session
            and (
                self.effective_through_session is None
                or session_date <= self.effective_through_session
            )
        )


@dataclass(frozen=True)
class UniverseSpec:
    """Ordered immutable instrument set with deterministic version identity."""

    instruments: tuple[InstrumentSpec, ...]
    universe_version: str

    @classmethod
    def build(cls, instruments: Iterable[InstrumentSpec]) -> "UniverseSpec":
        ordered = tuple(instruments)
        if not ordered:
            raise ValueError("pre-open universe cannot be empty")
        symbols = [instrument.symbol for instrument in ordered]
        if len(symbols) != len(set(symbols)):
            raise ValueError("instrument symbols must be unique within a universe")
        payload = [
            {
                "symbol": item.symbol,
                "asset_type": item.asset_type,
                "observation_role": item.observation_role,
                "effective_from_session": item.effective_from_session.isoformat(),
                "effective_through_session": (
                    item.effective_through_session.isoformat()
                    if item.effective_through_session
                    else None
                ),
                "enabled": item.enabled,
            }
            for item in ordered
        ]
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:16]
        return cls(instruments=ordered, universe_version=f"u-{digest}")

    def active_instruments(self, session_date: date) -> tuple[InstrumentSpec, ...]:
        """Return session-frozen membership without mutating historical sets."""
        return tuple(item for item in self.instruments if item.active_on(session_date))


@dataclass(frozen=True)
class SessionContext:
    """Authoritative exchange session and prior real session."""

    session_date: date
    open_at: datetime
    previous_session_date: date

    def __post_init__(self) -> None:
        _require_aware(self.open_at, "open_at")


@dataclass(frozen=True)
class Checkpoint:
    """A point-in-time market target and delayed-SIP eligibility time."""

    name: str
    checkpoint_at: datetime
    available_at: datetime

    def __post_init__(self) -> None:
        _require_aware(self.checkpoint_at, "checkpoint_at")
        _require_aware(self.available_at, "available_at")


CHECKPOINT_OFFSETS: tuple[tuple[str, timedelta], ...] = (
    ("open_minus_90m", timedelta(minutes=90)),
    ("open_minus_30m", timedelta(minutes=30)),
    ("open_minus_5m", timedelta(minutes=5)),
)
SIP_AVAILABILITY_DELAY = timedelta(minutes=16)


def checkpoints_for_session(session: SessionContext) -> tuple[Checkpoint, ...]:
    """Build the three reviewed targets relative to the scheduled open."""
    result = []
    for name, offset in CHECKPOINT_OFFSETS:
        target = session.open_at.astimezone(UTC) - offset
        result.append(
            Checkpoint(
                name=name,
                checkpoint_at=target,
                available_at=target + SIP_AVAILABILITY_DELAY,
            )
        )
    return tuple(result)


def calendar_open_utc(calendar_day: object) -> datetime:
    """Normalize alpaca-py's timezone-naive ``Calendar.open`` correctly.

    alpaca-py 0.43.2 parses the exchange-local value without attaching a
    timezone.  Treating it as host-local before ``astimezone`` shifts every
    checkpoint.  This function always interprets a naive value in New York.
    """
    raw = getattr(calendar_day, "open")
    if not isinstance(raw, datetime):
        raise TypeError("calendar open must be a datetime")
    localized = raw.replace(tzinfo=NEW_YORK) if raw.tzinfo is None else raw.astimezone(NEW_YORK)
    return localized.astimezone(UTC)


def premarket_start_utc(session_date: date) -> datetime:
    """Return the same-session 04:00 ET pre-market boundary in UTC."""
    return datetime.combine(session_date, time(4, 0), tzinfo=NEW_YORK).astimezone(UTC)


@dataclass(frozen=True)
class QuoteSample:
    """Normalized historical SIP quote retained without execution filters."""

    timestamp: datetime
    bid_price: float | None
    bid_size: float | None
    bid_exchange: str | None
    ask_price: float | None
    ask_size: float | None
    ask_exchange: str | None
    conditions: tuple[str, ...] = ()
    tape: str | None = None

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "quote timestamp")


@dataclass(frozen=True)
class ReferenceClose:
    """Split-adjusted prior regular-session SIP close."""

    session_date: date
    timestamp: datetime
    price: float

    def __post_init__(self) -> None:
        _require_aware(self.timestamp, "reference timestamp")
        if self.price <= 0:
            raise ValueError("reference price must be positive")


@dataclass(frozen=True)
class CalculatedMark:
    """Pure quote/reference result ready for immutable persistence."""

    status: str
    reason_code: str | None
    freshness_state: str
    quote_age_seconds: float | None
    mark_kind: str | None
    mark_price: float | None
    spread_bps: float | None
    gap_dollars: float | None
    gap_pct: float | None


def calculate_mark(
    *,
    checkpoint_at: datetime,
    quote: QuoteSample | None,
    reference: ReferenceClose | None,
) -> CalculatedMark:
    """Apply v0.2 validity, freshness, midpoint, spread, and gap rules."""
    _require_aware(checkpoint_at, "checkpoint_at")
    if quote is None:
        return CalculatedMark(
            status="unavailable",
            reason_code="quote_unavailable",
            freshness_state="invalid",
            quote_age_seconds=None,
            mark_kind=None,
            mark_price=None,
            spread_bps=None,
            gap_dollars=None,
            gap_pct=None,
        )

    age = (checkpoint_at.astimezone(UTC) - quote.timestamp.astimezone(UTC)).total_seconds()
    if age < 0:
        freshness = "invalid"
    elif age <= 60:
        freshness = "fresh"
    elif age <= 300:
        freshness = "aging"
    else:
        freshness = "stale"

    bid = quote.bid_price
    ask = quote.ask_price
    valid_quote = (
        bid is not None
        and ask is not None
        and bid > 0
        and ask > 0
        and ask >= bid
    )
    if not valid_quote or freshness == "invalid":
        return CalculatedMark(
            status="partial",
            reason_code="invalid_quote",
            freshness_state=freshness,
            quote_age_seconds=age,
            mark_kind=None,
            mark_price=None,
            spread_bps=None,
            gap_dollars=None,
            gap_pct=None,
        )

    midpoint = (bid + ask) / 2.0
    spread_bps = (ask - bid) / midpoint * 10_000.0
    if freshness == "stale":
        return CalculatedMark(
            status="partial",
            reason_code="stale_mark",
            freshness_state=freshness,
            quote_age_seconds=age,
            mark_kind="stale_midpoint",
            mark_price=midpoint,
            spread_bps=spread_bps,
            gap_dollars=None,
            gap_pct=None,
        )
    if reference is None:
        return CalculatedMark(
            status="partial",
            reason_code="reference_unavailable",
            freshness_state=freshness,
            quote_age_seconds=age,
            mark_kind="midpoint",
            mark_price=midpoint,
            spread_bps=spread_bps,
            gap_dollars=None,
            gap_pct=None,
        )

    gap_dollars = midpoint - reference.price
    return CalculatedMark(
        status="complete",
        reason_code=None,
        freshness_state=freshness,
        quote_age_seconds=age,
        mark_kind="midpoint",
        mark_price=midpoint,
        spread_bps=spread_bps,
        gap_dollars=gap_dollars,
        gap_pct=gap_dollars / reference.price,
    )


def logical_key(
    *,
    universe_version: str,
    session_date: date,
    checkpoint: str,
    symbol: str,
    layer: str = "sip_checkpoint",
) -> str:
    """Build the contract's stable logical identity."""
    return "|".join(
        (
            CONTRACT_VERSION,
            universe_version,
            session_date.isoformat(),
            checkpoint,
            symbol.upper(),
            layer,
        )
    )


def _require_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")
