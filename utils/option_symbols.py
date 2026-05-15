"""
OCC option symbol helpers — pure string utilities, no engine deps.

Lives in ``utils`` (not ``engine``) so any layer can import it without
creating a circular dependency. ``engine.positions`` re-exports the
canonical name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date


_OCC_PAT = re.compile(r"^([A-Z]{1,6})[0-9]{6}[CP][0-9]{8}$")
# Capturing variant for parse_occ_symbol: root / YYMMDD / C|P / strike×1000.
_OCC_PARTS = re.compile(r"^([A-Z]{1,6})([0-9]{2})([0-9]{2})([0-9]{2})([CP])([0-9]{8})$")


def owner_key_for(symbol: str) -> str:
    """
    Normalize a broker symbol to the engine's owner_key.

      - equity ticker  → unchanged.
      - OCC option     → underlying ticker (e.g. SPY260516C00520000 → SPY).

    For single-leg positions, ``owner_key`` is the same value used as
    ``Position.position_id`` — keeping engine state, the trade DB, and the
    Position abstraction all keyed identically.
    """
    match = _OCC_PAT.match(symbol)
    return match.group(1) if match else symbol


def is_occ_option(symbol: str) -> bool:
    """True if ``symbol`` is a valid OCC option contract string."""
    return _OCC_PAT.match(symbol) is not None


@dataclass(frozen=True)
class OccContract:
    """Parsed components of an OCC option symbol."""

    root: str               # underlying ticker, e.g. "SPY"
    expiration: date        # contract expiration date
    option_type: str        # "C" or "P"
    strike: float           # strike price in dollars


def parse_occ_symbol(symbol: str) -> OccContract:
    """
    Parse an OCC option symbol into its components.

    ``SPY260618P00689000`` → ``OccContract("SPY", date(2026, 6, 18), "P", 689.0)``.

    Raises ``ValueError`` for a non-OCC string — callers should gate with
    :func:`is_occ_option` when the input may be an equity ticker.
    """
    match = _OCC_PARTS.match(symbol)
    if match is None:
        raise ValueError(f"not a valid OCC option symbol: {symbol!r}")
    root, yy, mm, dd, opt_type, strike_raw = match.groups()
    expiration = date(2000 + int(yy), int(mm), int(dd))
    strike = int(strike_raw) / 1000.0
    return OccContract(
        root=root,
        expiration=expiration,
        option_type=opt_type,
        strike=strike,
    )
