"""
Trading-session calendar (PLAN 11.52).

The bar cache cannot tell "these sessions are missing" from "the market was
shut" without an independent list of real trading days. That distinction is
the whole difference between a silent hole and a weekend.

**Independent of the bar cache, on purpose.** 11.52 records that a sweep
using SPY as the session reference reported "clean" while 92 of 106 symbols
were holed — SPY is cached by the same code and shared the July gap. Any
reference drawn from `data/historical/` is disqualified. This asks Alpaca.

Cached to one JSON file: sessions are static history plus a known forward
schedule, so a refresh is only needed when a request runs past what is
already stored.

**Fails open.** Every entry point returns ``None`` when the calendar is
unavailable, and callers keep their pre-11.52 behaviour on ``None``. A
market-data outage must never block a fetch — the cost of that is worse
than the hole it would prevent.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from loguru import logger

_CACHE_PATH = Path(__file__).resolve().parent / "historical" / ".market_calendar.json"

# Pad forward requests so a run near the edge doesn't refetch every call.
_FORWARD_PAD = timedelta(days=90)

# Process-local memo: (covered_lo, covered_hi, sessions)
_memo: tuple[date, date, set[date]] | None = None


def _load_disk() -> tuple[date, date, set[date]] | None:
    if not _CACHE_PATH.exists():
        return None
    try:
        raw = json.loads(_CACHE_PATH.read_text())
        lo = date.fromisoformat(raw["covered_start"])
        hi = date.fromisoformat(raw["covered_end"])
        sessions = {date.fromisoformat(d) for d in raw["sessions"]}
        return lo, hi, sessions
    except (json.JSONDecodeError, KeyError, ValueError) as e:
        logger.warning(f"market calendar: bad cache file, ignoring ({e})")
        return None


def _save_disk(lo: date, hi: date, sessions: set[date]) -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps({
            "covered_start": lo.isoformat(),
            "covered_end": hi.isoformat(),
            "sessions": sorted(d.isoformat() for d in sessions),
        }))
    except OSError as e:  # pragma: no cover - disk failure
        logger.warning(f"market calendar: could not persist cache ({e})")


def _fetch(lo: date, hi: date) -> set[date] | None:
    """Ask Alpaca for real sessions in [lo, hi]. None on any failure."""
    try:
        from alpaca.trading.client import TradingClient
        from alpaca.trading.requests import GetCalendarRequest

        from config.settings import (
            ALPACA_API_KEY, ALPACA_PAPER, ALPACA_SECRET_KEY,
        )

        client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=ALPACA_PAPER)
        days = client.get_calendar(GetCalendarRequest(start=lo, end=hi))
        return {d.date if isinstance(d.date, date) else date.fromisoformat(str(d.date))
                for d in days}
    except Exception as e:
        logger.warning(
            f"market calendar: fetch failed ({type(e).__name__}: {e}) — "
            "session-gap detection is disabled for this call"
        )
        return None


def trading_sessions(start: date, end: date) -> set[date] | None:
    """
    Real trading sessions in [start, end], or ``None`` if unavailable.

    ``None`` is a legitimate answer and callers must treat it as "cannot
    check", never as "no sessions" — reading it as an empty set would make
    every day look like a hole.
    """
    global _memo
    if start > end:
        return set()

    for src in (_memo, _load_disk()):
        if src and src[0] <= start and end <= src[1]:
            _memo = src
            return {d for d in src[2] if start <= d <= end}

    known = _memo or _load_disk()
    lo = min(start, known[0]) if known else start
    hi = max(end, known[1]) if known else end
    hi = max(hi, date.today() + _FORWARD_PAD)

    fetched = _fetch(lo, hi)
    if fetched is None:
        return None

    _memo = (lo, hi, fetched)
    _save_disk(lo, hi, fetched)
    return {d for d in fetched if start <= d <= end}


def missing_sessions(
    start: date, end: date, have: set[date], *, ignore: set[date] | None = None
) -> set[date] | None:
    """
    Sessions in [start, end] that should exist but are absent from ``have``.

    ``ignore`` is the set already confirmed unavailable upstream — feed-depth
    boundaries and delisted stretches — so they are not reported forever.
    Returns ``None`` when the calendar could not be consulted.
    """
    sessions = trading_sessions(start, end)
    if sessions is None:
        return None
    return sessions - have - (ignore or set())


def _reset_for_tests() -> None:
    """Drop the process memo so tests don't leak state into each other."""
    global _memo
    _memo = None
