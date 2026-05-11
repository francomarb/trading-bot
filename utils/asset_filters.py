"""Asset-metadata filters shared by watchlist scanners.

Heuristics applied to Alpaca asset metadata (symbol/name/exchange strings)
to exclude non-stock products before any data fetching. Catches ~95% of
ETFs/ETNs/funds/preferreds/warrants/leveraged products. For the residual
~5%, scanners should confirm with a yfinance ``quoteType`` lookup on the
final candidate list.
"""

from __future__ import annotations


_EXCLUDED_SYMBOL_PATTERNS: tuple[str, ...] = (
    ".PR",   # preferred shares
    ".WS",   # warrants
    ".WT",   # warrants
    ".RT",   # rights
)

_EXCLUDED_NAME_TERMS: tuple[str, ...] = (
    " ETF",
    "ETN",
    "FUND",
    "TRUST",
    "INDEX",
    "ISHARES",
    "SPDR",
    "VANGUARD",
    "INVESCO",
    "PROSHARES",
    "DIREXION",
    "GLOBAL X",
    "BOND",
    "TREASURY",
    "PREFERRED",
    "PREFD",
    "PREF",
    "DEPOSITARY",
    "RIGHT",
    "WARRANT",
    "UNIT",
    "ULTRA",
    "BEAR",
    "BULL",
    "LEVERAGED",
    "2X",
    "3X",
)


def is_stock_like(symbol: str, name: str, exchange: str) -> bool:
    """Return True if the asset metadata looks like a regular common stock.

    False for: ETFs, ETNs, mutual funds, closed-end funds, trusts, preferreds,
    warrants, rights, units, leveraged/inverse products. Matches on the
    Alpaca-provided ``symbol``, ``name``, and ``exchange`` strings; does not
    hit any external API.

    Heuristic — not authoritative. Confirm with yfinance ``quoteType``
    downstream when correctness matters.
    """
    symbol_upper = (symbol or "").upper()
    if any(p in symbol_upper for p in _EXCLUDED_SYMBOL_PATTERNS):
        return False
    text = f"{symbol_upper} {name or ''} {exchange or ''}".upper()
    if "OTC" in text:
        return False
    return not any(term in text for term in _EXCLUDED_NAME_TERMS)
