"""Unit tests for data/watchlists.py (Phase 10.B3)."""

from __future__ import annotations

import pytest

from config import settings
from data.watchlists import StaticWatchlistSource, WatchlistSource
from scripts.post_mortem import SECTOR_MAP, _sector_etf_for
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


class TestRSIWatchlistPromotion:
    def test_uses_only_preferred_alphabet_share_class(self):
        assert "GOOG" in settings.RSI_WATCHLIST
        assert "GOOGL" not in settings.RSI_WATCHLIST

    def test_symbols_are_unique_and_have_post_mortem_sector_mapping(self):
        assert len(settings.RSI_WATCHLIST) == len(set(settings.RSI_WATCHLIST))
        assert set(settings.RSI_WATCHLIST) <= set(SECTOR_MAP)

    def test_ranked_pool_contains_provider_correction(self):
        ranked_pool = settings.RSI_WATCHLIST[:50]

        assert "BRK.B" in ranked_pool
        assert "BAC" not in ranked_pool
        assert SECTOR_MAP["BRK.B"] == "XLF"


class TestDonchianWatchlistPromotion:
    def test_generated_pool_precedes_any_lifecycle_preservation_members(self):
        assert settings.DONCHIAN_TARGET_POOL_SIZE == 100
        assert len(settings.DONCHIAN_WATCHLIST) >= settings.DONCHIAN_TARGET_POOL_SIZE

    def test_symbols_are_unique_and_use_preferred_alphabet_share_class(self):
        assert len(settings.DONCHIAN_WATCHLIST) == len(
            set(settings.DONCHIAN_WATCHLIST)
        )
        assert "GOOG" in settings.DONCHIAN_WATCHLIST
        assert "GOOGL" not in settings.DONCHIAN_WATCHLIST

    def test_ranked_pool_contains_profitability_parser_correction(self):
        ranked_pool = settings.DONCHIAN_WATCHLIST[
            : settings.DONCHIAN_TARGET_POOL_SIZE
        ]

        assert "ISRG" in ranked_pool
        assert "LIN" not in ranked_pool

    def test_post_mortem_uses_dynamic_sector_resolution_for_generated_names(self):
        class _Resolver:
            def resolve(self, symbol):
                return "healthcare" if symbol == "NEW" else None

        assert _sector_etf_for("NEW", _Resolver()) == "XLV"
        assert _sector_etf_for("NVDA", _Resolver()) == SECTOR_MAP["NVDA"]


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
