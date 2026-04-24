"""Unit tests for data/watchlists.py (Phase 10.B3)."""

from __future__ import annotations

import pytest

from data.watchlists import StaticWatchlistSource, WatchlistSource
from strategies.base import StrategySlot


# ── Helpers ──────────────────────────────────────────────────────────────────


class _FakeStrategy:
    name = "fake"
    preferred_order_type = None

    def required_bars(self) -> int:
        return 50

    def generate_signals(self, df):
        raise NotImplementedError


# ── StaticWatchlistSource ────────────────────────────────────────────────────


class TestStaticWatchlistSource:
    def test_is_watchlist_source(self):
        src = StaticWatchlistSource(["AAPL"], name="test")
        assert isinstance(src, WatchlistSource)

    def test_name_property(self):
        src = StaticWatchlistSource(["AAPL"], name="sma")
        assert src.name == "sma"

    def test_symbols_returns_list(self):
        src = StaticWatchlistSource(["AAPL", "MSFT"], name="test")
        assert src.symbols() == ["AAPL", "MSFT"]

    def test_symbols_returns_copy(self):
        src = StaticWatchlistSource(["AAPL"], name="test")
        result = src.symbols()
        result.append("HACK")
        # Mutating the returned list must not affect the source.
        assert src.symbols() == ["AAPL"]

    def test_construction_copies_input(self):
        original = ["AAPL", "MSFT"]
        src = StaticWatchlistSource(original, name="test")
        original.append("HACK")
        assert src.symbols() == ["AAPL", "MSFT"]

    def test_default_name(self):
        src = StaticWatchlistSource(["AAPL"])
        assert src.name == "static"

    def test_empty_symbols_raises(self):
        with pytest.raises(ValueError, match="at least one symbol"):
            StaticWatchlistSource([])

    def test_empty_name_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            StaticWatchlistSource(["AAPL"], name="")

    def test_whitespace_name_raises(self):
        with pytest.raises(ValueError, match="non-empty"):
            StaticWatchlistSource(["AAPL"], name="   ")

    def test_name_is_stripped(self):
        src = StaticWatchlistSource(["AAPL"], name=" sma ")
        assert src.name == "sma"


# ── StrategySlot with watchlist_source ──────────────────────────────────────


class TestStrategySlotWatchlistSource:
    def test_slot_accepts_watchlist_source(self):
        src = StaticWatchlistSource(["AAPL", "MSFT"], name="sma")
        slot = StrategySlot(strategy=_FakeStrategy(), watchlist_source=src)
        assert slot.watchlist_source is src

    def test_active_symbols_from_watchlist_source(self):
        src = StaticWatchlistSource(["AAPL", "MSFT"], name="sma")
        slot = StrategySlot(strategy=_FakeStrategy(), watchlist_source=src)
        assert slot.active_symbols() == ["AAPL", "MSFT"]

    def test_watchlist_source_takes_precedence_over_symbols(self):
        src = StaticWatchlistSource(["AAPL"], name="sma")
        slot = StrategySlot(
            strategy=_FakeStrategy(),
            symbols=["IGNORED"],
            watchlist_source=src,
        )
        assert slot.active_symbols() == ["AAPL"]

    def test_watchlist_source_takes_precedence_over_scanner(self):
        class _Scanner:
            def scan(self):
                return ["SCANNER_SYM"]

        src = StaticWatchlistSource(["AAPL"], name="sma")
        slot = StrategySlot(
            strategy=_FakeStrategy(),
            watchlist_source=src,
            scanner=_Scanner(),
        )
        assert slot.active_symbols() == ["AAPL"]

    def test_slot_still_accepts_plain_symbols(self):
        slot = StrategySlot(strategy=_FakeStrategy(), symbols=["AAPL"])
        assert slot.active_symbols() == ["AAPL"]

    def test_slot_raises_with_no_symbols_no_scanner_no_source(self):
        with pytest.raises(ValueError, match="symbols, a watchlist_source, or a scanner"):
            StrategySlot(strategy=_FakeStrategy())
