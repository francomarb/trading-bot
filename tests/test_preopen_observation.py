"""Contract tests for the pure pre-open observation model."""

from __future__ import annotations

import os
import time as system_time
from datetime import date, datetime, timezone

import pytest
from alpaca.trading.models import Calendar

from data.preopen_observation import (
    InstrumentSpec,
    QuoteSample,
    ReferenceClose,
    SessionContext,
    UniverseSpec,
    calculate_mark,
    calendar_open_utc,
    checkpoints_for_session,
)


UTC = timezone.utc


class TestCalendarContract:
    @pytest.mark.parametrize("host_tz", ["UTC", "America/Chicago"])
    def test_actual_sdk_naive_open_is_interpreted_in_new_york(
        self, monkeypatch, host_tz,
    ):
        if not hasattr(system_time, "tzset"):
            pytest.skip("host timezone switching requires time.tzset")
        original = os.environ.get("TZ")
        try:
            monkeypatch.setenv("TZ", host_tz)
            system_time.tzset()
            day = Calendar(
                date="2026-07-06",
                open="09:30",
                close="16:00",
            )
            assert calendar_open_utc(day) == datetime(
                2026, 7, 6, 13, 30, tzinfo=UTC
            )
        finally:
            if original is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original
            system_time.tzset()

    def test_checkpoints_are_relative_to_special_session_open(self):
        session = SessionContext(
            session_date=date(2026, 11, 27),
            open_at=datetime(2026, 11, 27, 14, 30, tzinfo=UTC),
            previous_session_date=date(2026, 11, 25),
        )
        checkpoints = checkpoints_for_session(session)
        assert [item.name for item in checkpoints] == [
            "open_minus_90m", "open_minus_30m", "open_minus_5m"
        ]
        assert checkpoints[0].checkpoint_at == datetime(2026, 11, 27, 13, 0, tzinfo=UTC)
        assert checkpoints[0].available_at == datetime(2026, 11, 27, 13, 16, tzinfo=UTC)


class TestUniverseContract:
    def test_arbitrary_stock_and_etf_are_supported(self):
        universe = UniverseSpec.build(
            (
                InstrumentSpec("AAPL", "stock", "single_name", date(2026, 1, 2)),
                InstrumentSpec("IWM", "etf", "small_cap", date(2026, 1, 2)),
            )
        )
        assert [item.symbol for item in universe.active_instruments(date(2026, 1, 5))] == [
            "AAPL", "IWM"
        ]

    def test_enabled_change_creates_new_universe_version(self):
        enabled = UniverseSpec.build(
            (InstrumentSpec("AAPL", "stock", "single_name", date(2026, 1, 2)),)
        )
        disabled = UniverseSpec.build(
            (
                InstrumentSpec(
                    "AAPL", "stock", "single_name", date(2026, 1, 2), enabled=False
                ),
            )
        )
        assert enabled.universe_version != disabled.universe_version


class TestMarkCalculation:
    def test_fresh_midpoint_and_gap(self):
        checkpoint = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
        quote = QuoteSample(
            timestamp=datetime(2026, 11, 4, 12, 59, 50, tzinfo=UTC),
            bid_price=101.0,
            bid_size=10,
            bid_exchange="P",
            ask_price=103.0,
            ask_size=12,
            ask_exchange="Q",
        )
        reference = ReferenceClose(
            session_date=date(2026, 11, 3),
            timestamp=datetime(2026, 11, 3, 20, 0, tzinfo=UTC),
            price=100.0,
        )
        result = calculate_mark(
            checkpoint_at=checkpoint, quote=quote, reference=reference
        )
        assert result.status == "complete"
        assert result.freshness_state == "fresh"
        assert result.mark_price == 102.0
        assert result.gap_dollars == 2.0
        assert result.gap_pct == pytest.approx(0.02)

    def test_stale_quote_is_partial_and_has_no_gap(self):
        checkpoint = datetime(2026, 11, 4, 13, 0, tzinfo=UTC)
        quote = QuoteSample(
            timestamp=datetime(2026, 11, 4, 12, 45, tzinfo=UTC),
            bid_price=99.0,
            bid_size=1,
            bid_exchange=None,
            ask_price=101.0,
            ask_size=1,
            ask_exchange=None,
        )
        reference = ReferenceClose(
            session_date=date(2026, 11, 3),
            timestamp=datetime(2026, 11, 3, 20, 0, tzinfo=UTC),
            price=100.0,
        )
        result = calculate_mark(
            checkpoint_at=checkpoint, quote=quote, reference=reference
        )
        assert result.status == "partial"
        assert result.reason_code == "stale_mark"
        assert result.mark_kind == "stale_midpoint"
        assert result.gap_pct is None

    @pytest.mark.parametrize(
        "bid,ask",
        [(None, 101.0), (100.0, None), (0.0, 101.0), (102.0, 101.0)],
    )
    def test_invalid_quotes_never_use_trade_fallback(self, bid, ask):
        quote = QuoteSample(
            timestamp=datetime(2026, 11, 4, 12, 59, 50, tzinfo=UTC),
            bid_price=bid,
            bid_size=1,
            bid_exchange=None,
            ask_price=ask,
            ask_size=1,
            ask_exchange=None,
        )
        result = calculate_mark(
            checkpoint_at=datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
            quote=quote,
            reference=None,
        )
        assert result.status == "partial"
        assert result.mark_price is None
