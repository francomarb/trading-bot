"""Thin transport policy and observability for official alpaca-py clients.

alpaca-py owns request construction, authentication, pagination, response
models, and its native 429/504 retry behavior.  The concrete public clients do
not expose a request-timeout option, so this module supplies only the missing
boundary: a default requests timeout plus process-local attempt telemetry.
"""

from __future__ import annotations

import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from config import settings


_UUID_PATH_SEGMENT = re.compile(
    r"(?i)(?<=/)[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?=/|$)"
)


@dataclass(frozen=True)
class AlpacaHttpAttempt:
    """One physical HTTP attempt, including alpaca-py's internal retries."""

    sequence: int
    observed_at_monotonic: float
    method: str
    endpoint: str
    elapsed_ms: float
    status_code: int | None
    error_type: str | None
    rate_limit_remaining: str | None
    rate_limit_reset: str | None


_ATTEMPTS: deque[AlpacaHttpAttempt] = deque(maxlen=10_000)
_ATTEMPT_LOCK = threading.Lock()
_ATTEMPT_SEQUENCE = 0


def http_attempt_marker() -> int:
    """Return a marker suitable for a later ``http_attempts_since`` call."""

    with _ATTEMPT_LOCK:
        return _ATTEMPT_SEQUENCE


def http_attempts_since(marker: int) -> list[AlpacaHttpAttempt]:
    """Return retained physical attempts recorded after ``marker``."""

    with _ATTEMPT_LOCK:
        return [attempt for attempt in _ATTEMPTS if attempt.sequence > marker]


def rolling_http_attempt_count(window_seconds: float = 60.0) -> int:
    """Return physical Alpaca attempts observed in the trailing window."""

    cutoff = time.monotonic() - window_seconds
    with _ATTEMPT_LOCK:
        return sum(attempt.observed_at_monotonic >= cutoff for attempt in _ATTEMPTS)


def is_ambiguous_write_error(error: BaseException) -> bool:
    """Return whether a failed write may still have reached Alpaca.

    A connection/read timeout gives no trustworthy answer about server-side
    acceptance. Likewise, alpaca-py surfaces 429/5xx only after exhausting its
    native retry policy. Callers must reconcile these outcomes by the submitted
    client order ID instead of labeling them rejected or replaying the write.
    """

    if isinstance(
        error,
        (RequestsConnectionError, RequestsTimeout, ConnectionError, TimeoutError),
    ):
        return True
    status = getattr(error, "status_code", None)
    return status == 429 or (status is not None and 500 <= status < 600)


def _record_attempt(
    *,
    method: str,
    endpoint: str,
    elapsed_ms: float,
    status_code: int | None,
    error_type: str | None,
    rate_limit_remaining: str | None,
    rate_limit_reset: str | None,
) -> None:
    global _ATTEMPT_SEQUENCE
    with _ATTEMPT_LOCK:
        _ATTEMPT_SEQUENCE += 1
        _ATTEMPTS.append(
            AlpacaHttpAttempt(
                sequence=_ATTEMPT_SEQUENCE,
                observed_at_monotonic=time.monotonic(),
                method=method,
                endpoint=endpoint,
                elapsed_ms=elapsed_ms,
                status_code=status_code,
                error_type=error_type,
                rate_limit_remaining=rate_limit_remaining,
                rate_limit_reset=rate_limit_reset,
            )
        )


class AlpacaTimeoutAdapter(HTTPAdapter):
    """Apply the missing SDK timeout and observe every physical request."""

    def send(self, request, **kwargs):  # type: ignore[no-untyped-def]
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = (
                settings.ALPACA_HTTP_CONNECT_TIMEOUT_SECONDS,
                settings.ALPACA_HTTP_READ_TIMEOUT_SECONDS,
            )
        started = time.monotonic()
        response = None
        error_type: str | None = None
        try:
            response = super().send(request, **kwargs)
            return response
        except Exception as exc:
            error_type = type(exc).__name__
            raise
        finally:
            parsed = urlsplit(str(request.url))
            endpoint = _UUID_PATH_SEGMENT.sub("{id}", f"{parsed.netloc}{parsed.path}")
            headers = getattr(response, "headers", {}) or {}
            _record_attempt(
                method=str(getattr(request, "method", "UNKNOWN")),
                endpoint=endpoint,
                elapsed_ms=(time.monotonic() - started) * 1000.0,
                status_code=getattr(response, "status_code", None),
                error_type=error_type,
                rate_limit_remaining=headers.get("X-RateLimit-Remaining"),
                rate_limit_reset=headers.get("X-RateLimit-Reset"),
            )


def install_alpaca_transport(session) -> None:  # type: ignore[no-untyped-def]
    """Install the shared timeout/telemetry adapter on an SDK session."""

    adapter = AlpacaTimeoutAdapter()
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def configure_alpaca_client(client):  # type: ignore[no-untyped-def]
    """Apply the transport boundary to an alpaca-py REST client and return it."""

    session = getattr(client, "_session", None)
    if session is not None:
        install_alpaca_transport(session)
    return client
