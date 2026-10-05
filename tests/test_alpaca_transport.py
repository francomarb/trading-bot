"""Tests for the thin alpaca-py transport boundary."""

from types import SimpleNamespace

import requests
from requests.adapters import HTTPAdapter

from config import settings
from utils.alpaca_transport import (
    AlpacaTimeoutAdapter,
    http_attempt_marker,
    http_attempts_since,
    rolling_http_attempt_count,
)


def test_adapter_applies_default_timeout_and_records_physical_attempt(monkeypatch):
    seen = {}

    def fake_send(self, request, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status_code=200, headers={"X-RateLimit-Remaining": "199"})

    monkeypatch.setattr(HTTPAdapter, "send", fake_send)
    marker = http_attempt_marker()
    request = requests.Request(
        "GET",
        "https://paper-api.alpaca.markets/v2/orders/"
        "123e4567-e89b-42d3-a456-426614174000",
    ).prepare()

    response = AlpacaTimeoutAdapter().send(request)

    assert response.status_code == 200
    assert seen["timeout"] == (
        settings.ALPACA_HTTP_CONNECT_TIMEOUT_SECONDS,
        settings.ALPACA_HTTP_READ_TIMEOUT_SECONDS,
    )
    attempts = http_attempts_since(marker)
    assert len(attempts) == 1
    assert attempts[0].endpoint.endswith("/v2/orders/{id}")
    assert attempts[0].rate_limit_remaining == "199"
    assert rolling_http_attempt_count() >= 1
