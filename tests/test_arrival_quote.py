"""
Arrival-quote staleness guard and provenance capture (10.D1).

Why this exists — the 2026-08-27 audit of the execution-quality slippage pool:
8 of 15 MARKET samples were measured against a benchmark that did not describe
the market. Verified against the SIP consolidated tape, the FILL landed inside
the traded minute range while the BENCHMARK sat outside it (ARM's benchmark was
3.3% below the market, NVT's 2.2% above). All 8 were within 10 minutes of the
open; the 7 samples after 10:30 ET sat within ±6 bps of their fills.

The defect is symmetric, and `adverse = max(0, signed)` hid half of it: ARM
logged a −428 bps "price improvement", which a market order cannot achieve.

`fetch_latest_quote_midpoint` had no staleness check at all, and the quote's own
timestamp was never recorded, so the age could not be checked after the fact.
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

import data.fetcher as fetcher
from data.fetcher import ArrivalQuote, fetch_latest_quote, fetch_latest_quote_midpoint


def _quote(*, age_seconds: float | None, bid: float = 100.0, ask: float = 100.10):
    q = MagicMock()
    q.bid_price = bid
    q.ask_price = ask
    if age_seconds is None:
        q.timestamp = None
    else:
        q.timestamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=age_seconds)
    return q


def _patched(quote, symbol="AAA"):
    client = MagicMock()
    client.get_stock_latest_quote.return_value = {symbol: quote}
    return patch.object(fetcher, "_get_client", return_value=client)


class TestStalenessGuard:
    def test_fresh_quote_is_accepted(self):
        with _patched(_quote(age_seconds=2.0)):
            q = fetch_latest_quote("AAA")
        assert isinstance(q, ArrivalQuote)
        assert q.midpoint == pytest.approx(100.05)
        assert q.age_seconds == pytest.approx(2.0, abs=0.5)

    def test_stale_quote_is_rejected(self):
        """A stale quote must yield None so the caller falls back, rather than
        being certified as an execution-quality benchmark."""
        with _patched(_quote(age_seconds=600.0)):
            assert fetch_latest_quote("AAA") is None

    def test_threshold_is_the_boundary(self):
        from config.settings import ARRIVAL_QUOTE_MAX_AGE_SECONDS as cap

        with _patched(_quote(age_seconds=cap - 1)):
            assert fetch_latest_quote("AAA") is not None
        with _patched(_quote(age_seconds=cap + 1)):
            assert fetch_latest_quote("AAA") is None

    def test_caller_can_override_the_threshold(self):
        with _patched(_quote(age_seconds=120.0)):
            assert fetch_latest_quote("AAA", max_age_seconds=10) is None
            assert fetch_latest_quote("AAA", max_age_seconds=600) is not None

    def test_quote_without_a_timestamp_is_rejected_not_assumed_fresh(self):
        """Age unknowable means it cannot be certified. Fail closed."""
        with _patched(_quote(age_seconds=None)):
            assert fetch_latest_quote("AAA") is None

    def test_naive_timestamp_is_treated_as_utc_not_crashed_on(self):
        q = _quote(age_seconds=1.0)
        q.timestamp = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
        with _patched(q):
            assert fetch_latest_quote("AAA") is not None


class TestProvenanceCapture:
    def test_records_the_quotes_own_timestamp_not_the_capture_time(self):
        """
        The specific gap this closes: only `now` was recorded, so a quote of
        any age looked identical in the data.
        """
        q = _quote(age_seconds=7.0)
        with _patched(q):
            got = fetch_latest_quote("AAA")
        assert got.quote_timestamp == q.timestamp
        assert got.captured_at > got.quote_timestamp
        assert got.age_seconds == pytest.approx(7.0, abs=0.5)

    def test_records_spread_so_stale_can_be_told_from_fresh_but_wide(self):
        """
        Staleness is only one hypothesis for the audit finding; the other is a
        fresh but unrepresentative IEX book. Without spread there is no way to
        tell them apart.
        """
        with _patched(_quote(age_seconds=1.0, bid=100.0, ask=102.0)):
            q = fetch_latest_quote("AAA")
        assert q.spread_bps == pytest.approx(198.0, abs=1.0)

    def test_event_is_emitted_for_rejected_quotes_too(self):
        """A rejected quote is exactly the sample calibration needs to see."""
        events = []
        real_bind = fetcher.logger.bind

        def spy(**kw):
            if kw.get("event") == "arrival_quote":
                events.append(kw)
            return real_bind(**kw)

        with patch.object(fetcher.logger, "bind", side_effect=spy):
            with _patched(_quote(age_seconds=900.0)):
                fetch_latest_quote("AAA")
        assert events, "no arrival_quote event emitted for a rejected quote"
        assert events[0]["accepted"] is False
        assert events[0]["reject_reason"] == "stale"
        assert events[0]["age_seconds"] == pytest.approx(900.0, abs=1.0)

    def test_instrumentation_failure_never_breaks_the_fetch(self):
        with patch.object(fetcher, "_emit_arrival_quote_event",
                          side_effect=RuntimeError("boom")):
            with _patched(_quote(age_seconds=1.0)):
                with pytest.raises(RuntimeError):
                    fetch_latest_quote("AAA")
        # The real emitter swallows its own errors — verify that directly.
        fetcher._emit_arrival_quote_event(
            "AAA", 1.0, 1.0, 1.0, float("nan"),
            quote_timestamp=None, captured_at=dt.datetime.now(dt.timezone.utc),
            age_seconds=None, accepted=True, reason=None,
        )


class TestQuoteShapeValidation:
    """
    PR #127 review P2-1. The docstring claimed to reject crossed books since it
    was written, but only ever tested for a zero side. A crossed quote was
    accepted with a plausible finite midpoint and a NEGATIVE spread, and
    `_finite_or_none` does not catch it — so it would be certified
    `arrival_midpoint` / `primary` and enter the calibration pool.
    """

    def test_crossed_book_is_rejected(self):
        """Reproduced in review: bid=101, ask=100 -> mid 100.50, spread -99.50."""
        with _patched(_quote(age_seconds=1.0, bid=101.0, ask=100.0)):
            assert fetch_latest_quote("AAA") is None

    def test_locked_book_is_allowed_and_records_zero_spread(self):
        """Deliberate: a locked book is a real state with an unambiguous
        midpoint. The zero spread is recorded so it can be revisited if it
        correlates with bad benchmarks."""
        with _patched(_quote(age_seconds=1.0, bid=100.0, ask=100.0)):
            q = fetch_latest_quote("AAA")
        assert q is not None
        assert q.midpoint == pytest.approx(100.0)
        assert q.spread_bps == pytest.approx(0.0)

    @pytest.mark.parametrize("bid,ask", [
        (float("nan"), 100.0), (100.0, float("nan")),
        (float("inf"), 100.0), (100.0, float("inf")),
        (float("-inf"), 100.0),
    ])
    def test_non_finite_prices_are_rejected(self, bid, ask):
        """NaN/inf survive every `<= 0` comparison and produce a NaN/inf
        midpoint. Rejected here rather than relying on a caller's guard."""
        with _patched(_quote(age_seconds=1.0, bid=bid, ask=ask)):
            assert fetch_latest_quote("AAA") is None

    def test_a_crossed_quote_never_yields_a_negative_spread(self):
        """The invariant: no accepted quote may report a negative spread."""
        for bid, ask in [(101.0, 100.0), (100.5, 100.0), (200.0, 1.0)]:
            with _patched(_quote(age_seconds=1.0, bid=bid, ask=ask)):
                q = fetch_latest_quote("AAA")
            assert q is None or q.spread_bps >= 0


class TestExistingBehaviourPreserved:
    def test_midpoint_wrapper_still_returns_a_float(self):
        with _patched(_quote(age_seconds=1.0)):
            assert fetch_latest_quote_midpoint("AAA") == pytest.approx(100.05)

    def test_midpoint_wrapper_returns_none_when_rejected(self):
        with _patched(_quote(age_seconds=900.0)):
            assert fetch_latest_quote_midpoint("AAA") is None

    @pytest.mark.parametrize("bid,ask", [(0.0, 100.0), (100.0, 0.0), (0.0, 0.0)])
    def test_one_sided_book_still_rejected(self, bid, ask):
        with _patched(_quote(age_seconds=1.0, bid=bid, ask=ask)):
            assert fetch_latest_quote("AAA") is None

    def test_empty_symbol_returns_none(self):
        assert fetch_latest_quote("") is None

    def test_api_failure_returns_none_and_does_not_raise(self):
        client = MagicMock()
        client.get_stock_latest_quote.side_effect = RuntimeError("API down")
        with patch.object(fetcher, "_get_client", return_value=client):
            assert fetch_latest_quote("AAA") is None
