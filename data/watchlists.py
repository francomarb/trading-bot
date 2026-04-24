"""Watchlist abstraction (Phase 10, item 10.B3).

A WatchlistSource decouples strategy slots from hard-coded symbol lists.
The engine calls .symbols() and gets a list back without caring whether the
list comes from a static config, a file, or a live scanner script.

Phase 10 ships StaticWatchlistSource only. DynamicWatchlistSource (calls
an external module/script at runtime) is Phase 11 — deferred until durable
position ownership is proven stable.
"""

from __future__ import annotations

from abc import ABC, abstractmethod


class WatchlistSource(ABC):
    """
    Abstract watchlist provider.

    Subclasses supply a symbol list by implementing ``symbols()``. The
    ``name`` property is used in logs and ownership attribution — it must be
    stable across restarts so the trade DB can match records to the right slot.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier for this watchlist (used in logs and the trade DB)."""

    @abstractmethod
    def symbols(self) -> list[str]:
        """Return the current symbol list."""


class StaticWatchlistSource(WatchlistSource):
    """
    Wraps a fixed list of symbols validated at construction.

    The list never changes at runtime — suitable for manually-curated
    watchlists that are reviewed and promoted before each phase.
    """

    def __init__(self, symbols: list[str], *, name: str = "static") -> None:
        if not symbols:
            raise ValueError("StaticWatchlistSource requires at least one symbol")
        if not name or not name.strip():
            raise ValueError("name must be a non-empty string")
        self._symbols = list(symbols)
        self._name = name.strip()

    @property
    def name(self) -> str:
        return self._name

    def symbols(self) -> list[str]:
        return list(self._symbols)
