"""
OCC option symbol helpers — pure string utilities, no engine deps.

Lives in ``utils`` (not ``engine``) so any layer can import it without
creating a circular dependency. ``engine.positions`` re-exports the
canonical name.
"""

from __future__ import annotations

import re


_OCC_PAT = re.compile(r"^([A-Z]{1,6})[0-9]{6}[CP][0-9]{8}$")


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
