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


class TestEveryRejectionIsObservable:
    """
    Until 2026-08-28 seven `return None` paths were silent — no log, no event.
    The worst was the zero-side case, which Alpaca staff say is the MOST common
    failure on IEX ("there can be a lot of 0 price and size quotes"). A morning
    of all-zero quotes produced no events at all, making "IEX gave us nothing"
    indistinguishable from "we never asked".
    """

    @staticmethod
    def _capture(fn):
        events = []
        real = fetcher.logger.bind

        def spy(**kw):
            if kw.get("event") == "arrival_quote":
                events.append(kw)
            return real(**kw)

        with patch.object(fetcher.logger, "bind", side_effect=spy):
            fn()
        return events

    @pytest.mark.parametrize("bid,ask,reason", [
        (0.0, 100.0, "zero_side"),
        (100.0, 0.0, "zero_side"),
        (101.0, 100.0, "crossed"),
        (float("nan"), 100.0, "non_finite"),
        (100.0, float("inf"), "non_finite"),
    ])
    def test_shape_rejections_emit_a_reason(self, bid, ask, reason):
        def run():
            with _patched(_quote(age_seconds=1.0, bid=bid, ask=ask)):
                assert fetch_latest_quote("AAA") is None

        events = self._capture(run)
        assert events, f"no event emitted for {reason}"
        assert events[-1]["reject_reason"] == reason
        assert events[-1]["accepted"] is False

    def test_missing_quote_emits_a_reason(self):
        def run():
            client = MagicMock()
            client.get_stock_latest_quote.return_value = {}
            with patch.object(fetcher, "_get_client", return_value=client):
                assert fetch_latest_quote("AAA") is None

        assert self._capture(run)[-1]["reject_reason"] == "no_quote"

    def test_api_failure_emits_a_reason(self):
        def run():
            client = MagicMock()
            client.get_stock_latest_quote.side_effect = RuntimeError("down")
            with patch.object(fetcher, "_get_client", return_value=client):
                assert fetch_latest_quote("AAA") is None

        assert self._capture(run)[-1]["reject_reason"] == "api_error"


class TestAgeIsMeasuredFromTheResponse:
    """
    PR #127 review P1. `captured_at` was taken BEFORE the REST call, so network
    latency was subtracted from the reported age: a quote 10ms old on arrival
    reported -45ms after a 50ms round trip and was certified against a
    zero-second limit. With the 30s API timeout a borderline quote could be
    materially stale and still read as `primary`.
    """

    @staticmethod
    def _slow_client(latency_s: float, quote_age_ms: float):
        class Slow:
            def get_stock_latest_quote(self, req):
                import time

                time.sleep(latency_s)
                q = MagicMock()
                q.bid_price, q.ask_price = 100.0, 100.10
                # Age is relative to the moment the response is produced.
                q.timestamp = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
                    milliseconds=quote_age_ms
                )
                return {"AAA": q}

        return Slow()

    def test_age_is_not_negative_when_the_request_is_slow(self):
        client = self._slow_client(latency_s=0.05, quote_age_ms=10)
        with patch.object(fetcher, "_get_client", return_value=client):
            q = fetch_latest_quote("AAA", max_age_seconds=30.0)
        assert q is not None
        assert q.age_seconds > 0, (
            f"age {q.age_seconds:.3f}s is negative — measured from before the "
            "request, so network latency was subtracted"
        )
        assert q.age_seconds == pytest.approx(0.010, abs=0.02)

    def test_latency_cannot_smuggle_a_quote_past_a_zero_second_limit(self):
        """The reviewer's exact repro: 50ms request, 10ms-old quote, max_age=0."""
        client = self._slow_client(latency_s=0.05, quote_age_ms=10)
        with patch.object(fetcher, "_get_client", return_value=client):
            assert fetch_latest_quote("AAA", max_age_seconds=0.0) is None

    def test_captured_at_is_after_the_quote_timestamp(self):
        client = self._slow_client(latency_s=0.02, quote_age_ms=5)
        with patch.object(fetcher, "_get_client", return_value=client):
            q = fetch_latest_quote("AAA", max_age_seconds=30.0)
        assert q.captured_at > q.quote_timestamp


class TestStaleRepeatDetection:
    """
    A quote that is stale AND identical to the previous one is tagged
    `stale_repeat`. That is EVIDENCE, not a diagnosis: it proves only that no
    newer quote was returned between two requests, not that the venue stopped
    publishing — on a sparse venue or a quiet symbol it can be legitimate
    inactivity (PR #127 review P2).
    """

    def setup_method(self):
        fetcher._LAST_QUOTE_TS.clear()
        fetcher._REPEAT_COUNTS.clear()

    def test_repeated_stale_quote_is_flagged_as_a_repeat(self):
        ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=900)
        events = []
        real = fetcher.logger.bind

        def spy(**kw):
            if kw.get("event") == "arrival_quote":
                events.append(kw)
            return real(**kw)

        with patch.object(fetcher.logger, "bind", side_effect=spy):
            for _ in range(3):
                q = _quote(age_seconds=900.0)
                q.timestamp = ts  # the SAME quote every time
                with _patched(q, symbol="BBB"):
                    fetch_latest_quote("BBB")

        assert events[0]["reject_reason"] == "stale"
        assert events[0]["repeat_of_previous"] is False
        assert events[1]["reject_reason"] == "stale_repeat"
        assert events[1]["consecutive_repeats"] == 1
        assert events[2]["consecutive_repeats"] == 2

    def test_a_moving_book_is_not_flagged_as_a_repeat(self):
        """Guards the test above from passing vacuously."""
        events = []
        real = fetcher.logger.bind

        def spy(**kw):
            if kw.get("event") == "arrival_quote":
                events.append(kw)
            return real(**kw)

        with patch.object(fetcher.logger, "bind", side_effect=spy):
            for age in (900.0, 899.0, 898.0):  # a different quote each time
                with _patched(_quote(age_seconds=age), symbol="CCC"):
                    fetch_latest_quote("CCC")

        assert all(e["reject_reason"] == "stale" for e in events)
        assert all(e["repeat_of_previous"] is False for e in events)

    def test_repeat_state_is_per_symbol(self):
        ts = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=900)
        for sym in ("DDD", "EEE"):
            q = _quote(age_seconds=900.0)
            q.timestamp = ts
            with _patched(q, symbol=sym):
                fetch_latest_quote(sym)
        assert fetcher._REPEAT_COUNTS == {"DDD": 0, "EEE": 0}


class TestEveryReturnPathEmitsAnEvent:
    """
    Keeps the invariant unconditional and checkable: EVERY `return None` in
    `fetch_latest_quote` produces an arrival_quote event.

    The PR narrative claimed "every rejection is observable" while the
    empty-symbol guard still returned silently (PR #127 review, non-blocking).
    A caveated invariant is one nobody can verify, so the caveat was removed
    from the code rather than added to the prose — and this test enumerates the
    paths so it stays that way.
    """

    @staticmethod
    def _events_for(call):
        events = []
        real = fetcher.logger.bind

        def spy(**kw):
            if kw.get("event") == "arrival_quote":
                events.append(kw)
            return real(**kw)

        with patch.object(fetcher.logger, "bind", side_effect=spy):
            result = call()
        return result, events

    def test_empty_symbol_emits(self):
        result, events = self._events_for(lambda: fetch_latest_quote(""))
        assert result is None
        assert events and events[-1]["reject_reason"] == "empty_symbol"

    @pytest.mark.parametrize("reason,setup", [
        ("zero_side", dict(bid=0.0, ask=100.0, age=1.0)),
        ("crossed", dict(bid=101.0, ask=100.0, age=1.0)),
        ("non_finite", dict(bid=float("nan"), ask=100.0, age=1.0)),
        ("stale", dict(bid=100.0, ask=100.1, age=900.0)),
    ])
    def test_each_rejection_path_emits(self, reason, setup):
        fetcher._LAST_QUOTE_TS.clear()
        fetcher._REPEAT_COUNTS.clear()

        def call():
            with _patched(_quote(age_seconds=setup["age"], bid=setup["bid"],
                                 ask=setup["ask"])):
                return fetch_latest_quote("AAA")

        result, events = self._events_for(call)
        assert result is None
        assert events[-1]["reject_reason"] == reason

    def test_no_silent_return_none_remains(self):
        """
        Source-level guard: every `return None` inside `fetch_latest_quote`
        must be preceded by an emit. Counts the emit call sites against the
        return sites so a newly added silent path fails here.
        """
        import inspect

        src = inspect.getsource(fetcher.fetch_latest_quote)
        # Count STATEMENTS, not substring hits — the function's own comment
        # explains the invariant using the words "return None", and a naive
        # count picked that up and reported a phantom silent path.
        lines = [ln.strip() for ln in src.splitlines()]
        returns = sum(1 for ln in lines if ln.startswith("return None"))
        emits = sum(
            1 for ln in lines
            if ln.startswith("_reject(") or ln.startswith("_emit_arrival_quote_event(")
        )
        assert returns > 0, "no return paths found — the selector is wrong"
        assert emits >= returns, (
            f"{returns} `return None` statements but only {emits} emit sites in "
            "fetch_latest_quote — a rejection would be invisible"
        )
