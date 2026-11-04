"""Offline tests for Alpaca request shape and provider error classification."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest
from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.models import BarSet, QuoteSet

from data.preopen_provider import (
    AlpacaPreopenProvider,
    ProviderFailure,
    classify_provider_exception,
)


UTC = timezone.utc


class _FakeClient:
    def __init__(self, *, quote_timestamp=None):
        self.quote_requests = []
        self.bar_requests = []
        self.quote_timestamp = quote_timestamp or datetime(
            2026, 11, 4, 12, 59, 50, tzinfo=UTC
        )

    def get_stock_quotes(self, request):
        self.quote_requests.append(request)
        symbol = request.symbol_or_symbols
        return QuoteSet(
            {
                symbol: [
                    {
                        "t": self.quote_timestamp,
                        "bp": 100.0,
                        "bs": 10,
                        "bx": "P",
                        "ap": 100.2,
                        "as": 12,
                        "ax": "Q",
                        "c": [],
                        "z": "C",
                    }
                ]
            }
        )

    def get_stock_bars(self, request):
        self.bar_requests.append(request)
        return BarSet(
            {
                symbol: [
                    {
                        "t": datetime(2026, 11, 3, 5, 0, tzinfo=UTC),
                        "o": 99.0,
                        "h": 101.0,
                        "l": 98.0,
                        "c": 100.0,
                        "v": 1_000,
                        "n": 100,
                        "vw": 100.0,
                    }
                ]
                for symbol in request.symbol_or_symbols
            }
        )


class TestQuoteRequestShape:
    def test_one_descending_limit_one_request_per_symbol(self):
        client = _FakeClient()
        provider = AlpacaPreopenProvider(client)
        for symbol in ("SPY", "QQQ", "XLK", "SOXX"):
            quote = provider.latest_quote(
                symbol,
                session_date=date(2026, 11, 4),
                checkpoint_at=datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
            )
            assert quote is not None
        assert [request.symbol_or_symbols for request in client.quote_requests] == [
            "SPY", "QQQ", "XLK", "SOXX"
        ]
        assert all(request.limit == 1 for request in client.quote_requests)
        assert all(request.sort == Sort.DESC for request in client.quote_requests)
        assert all(request.feed == DataFeed.SIP for request in client.quote_requests)

    def test_quote_after_checkpoint_is_rejected(self):
        client = _FakeClient(
            quote_timestamp=datetime(2026, 11, 4, 13, 0, 1, tzinfo=UTC)
        )
        provider = AlpacaPreopenProvider(client)
        with pytest.raises(ProviderFailure) as caught:
            provider.latest_quote(
                "SPY",
                session_date=date(2026, 11, 4),
                checkpoint_at=datetime(2026, 11, 4, 13, 0, tzinfo=UTC),
            )
        assert caught.value.reason_code == "quote_outside_checkpoint_window"
        assert caught.value.retryable is False

    def test_reference_request_is_batched_and_complete(self):
        client = _FakeClient()
        provider = AlpacaPreopenProvider(client)
        result = provider.reference_closes(
            ("SPY", "QQQ", "XLK", "SOXX"),
            previous_session_date=date(2026, 11, 3),
        )
        assert set(result) == {"SPY", "QQQ", "XLK", "SOXX"}
        assert len(client.bar_requests) == 1


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


class _HttpError:
    def __init__(self, status_code):
        self.response = _Response(status_code)
        self.request = object()


class TestProviderClassification:
    def test_recent_sip_403_is_retryable_not_yet_available(self):
        error = APIError(
            json.dumps(
                {
                    "code": 40310000,
                    "message": "subscription does not permit querying recent SIP data",
                }
            ),
            _HttpError(403),
        )
        failure = classify_provider_exception(error)
        assert failure.classification == "not_yet_available"
        assert failure.retryable is True

    def test_genuine_entitlement_403_is_non_retryable(self):
        error = APIError(
            json.dumps({"code": 40310000, "message": "forbidden"}),
            _HttpError(403),
        )
        failure = classify_provider_exception(error)
        assert failure.classification == "entitlement_error"
        assert failure.retryable is False
