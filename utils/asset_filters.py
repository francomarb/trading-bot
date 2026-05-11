"""Asset-metadata filters shared by watchlist scanners.

Heuristics applied to Alpaca asset metadata (symbol/name/exchange strings)
to exclude non-stock products before any data fetching. Catches ~95% of
ETFs/ETNs/funds/preferreds/warrants/leveraged products. For the residual
~5%, scanners should confirm with a yfinance ``quoteType`` lookup on the
final candidate list.

Matching is word-bounded — terms only match when they appear as full
tokens, not as substrings of unrelated words. This prevents false
positives like "UnitedHealth"/"Unity" being caught by a bare ``UNIT``
substring, or "Bulldog" being caught by ``BULL``.
"""

from __future__ import annotations

import re


_EXCLUDED_SYMBOL_PATTERNS: tuple[str, ...] = (
    ".PR",   # preferred shares (e.g. BRK.PRA)
    ".WS",   # warrants
    ".WT",   # warrants
    ".RT",   # rights
)

# Word-bounded tokens. Each is matched as a full word (or multi-word
# phrase, with whitespace) in the combined symbol+name+exchange string,
# uppercased. ``\b`` boundaries prevent substring false-positives.
#
# Notes on specific entries:
#   - "UNITS" (plural) — standard SPAC pre-split unit naming. Singular
#     "UNIT" is intentionally excluded because it shadows real companies
#     (e.g. Unit Corporation, oil & gas).
#   - "TRUST" — catches ETF Trust names but also matches legitimate
#     trust-named companies (Northern Trust, T Rowe Price Trust). Accepted
#     trade-off; yfinance ``quoteType`` is the authoritative downstream
#     check.
#   - "BOND" — matches generic bond ETFs/funds. Rare collision with
#     surname-style company names.
_EXCLUDED_NAME_TERMS: tuple[str, ...] = (
    "ETF",
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
    "UNITS",
    "ULTRA",
    "BEAR",
    "BULL",
    "LEVERAGED",
    "2X",
    "3X",
    "DAILY TARGET",
)

_EXCLUDED_NAME_REGEX = re.compile(
    r"\b(?:" + "|".join(re.escape(t) for t in _EXCLUDED_NAME_TERMS) + r")\b"
)

_OTC_REGEX = re.compile(r"\bOTC\b")


def is_stock_like(symbol: str, name: str, exchange: str) -> bool:
    """Return True if the asset metadata looks like a regular common stock.

    False for: ETFs, ETNs, mutual funds, closed-end funds, trusts, preferreds,
    warrants, rights, units (SPAC), leveraged/inverse products. Matches on
    the Alpaca-provided ``symbol``, ``name``, and ``exchange`` strings;
    does not hit any external API.

    Term matching is word-bounded — "UnitedHealth", "United Airlines", and
    "Unity Software" all pass even though they contain "UNIT" as a
    substring. Likewise "Bulldog Industries" is not caught by "BULL".

    Heuristic — not authoritative. Confirm with yfinance ``quoteType``
    downstream when correctness matters.
    """
    symbol_upper = (symbol or "").upper()
    if any(p in symbol_upper for p in _EXCLUDED_SYMBOL_PATTERNS):
        return False
    text = f"{symbol_upper} {name or ''} {exchange or ''}".upper()
    if _OTC_REGEX.search(text):
        return False
    if _EXCLUDED_NAME_REGEX.search(text):
        return False
    return True
