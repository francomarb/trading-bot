"""Alpaca adapters for the delayed-SIP pre-open observation contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockQuotesRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest
from requests.exceptions import ConnectionError as RequestsConnectionError
from requests.exceptions import Timeout as RequestsTimeout

from config.settings import ALPACA_API_KEY, ALPACA_PAPER, ALPACA_SECRET_KEY
from data.preopen_observation import (
    NEW_YORK,
    QuoteSample,
    ReferenceClose,
    SessionContext,
    calendar_open_utc,
    premarket_start_utc,
)
from utils.alpaca_transport import configure_alpaca_client


@dataclass(frozen=True)
class ProviderFailure(Exception):
    """Normalized provider outcome used by the scheduler retry policy."""

    classification: str
    reason_code: str
    diagnostic: str
    retryable: bool

    def __str__(self) -> str:
        return self.diagnostic


class AlpacaSessionCalendar:
    """Fetch and cache authoritative current/prior exchange sessions."""

    def __init__(self, client: TradingClient | None = None) -> None:
        self._client = client
        self._cache: dict[date, SessionContext | None] = {}

    def _get_client(self) -> TradingClient:
        if self._client is None:
            self._client = configure_alpaca_client(
                TradingClient(
                    ALPACA_API_KEY,
                    ALPACA_SECRET_KEY,
                    paper=ALPACA_PAPER,
                )
            )
        return self._client

    def session_context(self, session_date: date) -> SessionContext | None:
        """Return context only when ``session_date`` is an exchange session."""
        if session_date in self._cache:
            return self._cache[session_date]
        contexts = self.session_contexts(session_date, session_date)
        return contexts[0] if contexts else None

    def session_contexts(self, start: date, end: date) -> tuple[SessionContext, ...]:
        """Return authoritative contexts for every exchange session in range."""
        if start > end:
            return ()
        if all(day in self._cache for day in _date_range(start, end)):
            return tuple(
                context
                for day in _date_range(start, end)
                if (context := self._cache[day]) is not None
            )
        days = self._get_client().get_calendar(
            GetCalendarRequest(
                start=start - timedelta(days=14),
                end=end,
            )
        )
        ordered = sorted(days, key=lambda item: item.date)
        by_date = {item.date: index for index, item in enumerate(ordered)}
        contexts: list[SessionContext] = []
        for session_date in _date_range(start, end):
            current_index = by_date.get(session_date)
            if current_index is None:
                self._cache[session_date] = None
                continue
            if current_index == 0:
                raise RuntimeError("calendar response omitted the previous session")
            context = SessionContext(
                session_date=session_date,
                open_at=calendar_open_utc(ordered[current_index]),
                previous_session_date=ordered[current_index - 1].date,
            )
            self._cache[session_date] = context
            contexts.append(context)
        return tuple(contexts)


class AlpacaPreopenProvider:
    """Historical SIP quote/reference reader with explicit request shapes."""

    def __init__(self, client: StockHistoricalDataClient | None = None) -> None:
        self._client = client

    def _get_client(self) -> StockHistoricalDataClient:
        if self._client is None:
            self._client = configure_alpaca_client(
                StockHistoricalDataClient(
                    api_key=ALPACA_API_KEY,
                    secret_key=ALPACA_SECRET_KEY,
                )
            )
        return self._client

    def latest_quote(self, symbol: str, *, session_date: date, checkpoint_at: datetime) -> QuoteSample | None:
        """Fetch one symbol's latest SIP quote at/before the checkpoint."""
        request = StockQuotesRequest(
            symbol_or_symbols=symbol,
            start=premarket_start_utc(session_date),
            end=checkpoint_at.astimezone(timezone.utc),
            limit=1,
            sort=Sort.DESC,
            feed=DataFeed.SIP,
        )
        try:
            quote_set = self._get_client().get_stock_quotes(request)
        except ProviderFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized at provider boundary
            raise classify_provider_exception(exc) from exc
        quotes = [] if quote_set is None else quote_set.data.get(symbol, [])
        if not quotes:
            return None
        quote = quotes[0]
        quote_timestamp = quote.timestamp.astimezone(timezone.utc)
        window_start = premarket_start_utc(session_date)
        window_end = checkpoint_at.astimezone(timezone.utc)
        if not window_start <= quote_timestamp <= window_end:
            raise ProviderFailure(
                "provider_error",
                "quote_outside_checkpoint_window",
                f"{symbol} quote timestamp fell outside requested window",
                False,
            )
        return QuoteSample(
            timestamp=quote_timestamp,
            bid_price=quote.bid_price,
            bid_size=quote.bid_size,
            bid_exchange=quote.bid_exchange,
            ask_price=quote.ask_price,
            ask_size=quote.ask_size,
            ask_exchange=quote.ask_exchange,
            conditions=tuple(quote.conditions or ()),
            tape=quote.tape,
        )

    def reference_closes(
        self,
        symbols: tuple[str, ...],
        *,
        previous_session_date: date,
    ) -> dict[str, ReferenceClose]:
        """Fetch one split-adjusted SIP daily close for every returned symbol."""
        start = datetime.combine(previous_session_date, datetime.min.time(), tzinfo=NEW_YORK)
        end = start + timedelta(days=1)
        request = StockBarsRequest(
            symbol_or_symbols=list(symbols),
            timeframe=TimeFrame.Day,
            start=start.astimezone(timezone.utc),
            end=end.astimezone(timezone.utc),
            adjustment=Adjustment.SPLIT,
            feed=DataFeed.SIP,
        )
        try:
            bar_set = self._get_client().get_stock_bars(request)
        except ProviderFailure:
            raise
        except Exception as exc:  # noqa: BLE001 - normalized at provider boundary
            raise classify_provider_exception(exc) from exc
        result: dict[str, ReferenceClose] = {}
        if bar_set is None:
            return result
        for symbol in symbols:
            bars = bar_set.data.get(symbol, [])
            matching = [
                bar
                for bar in bars
                if bar.timestamp.astimezone(NEW_YORK).date() == previous_session_date
            ]
            if not matching:
                continue
            bar = matching[-1]
            result[symbol] = ReferenceClose(
                session_date=previous_session_date,
                timestamp=bar.timestamp,
                price=float(bar.close),
            )
        return result


def classify_provider_exception(exc: Exception) -> ProviderFailure:
    """Classify provider failures exactly as the reviewed retry contract."""
    diagnostic = f"{type(exc).__name__}: {exc}"[:500]
    if isinstance(exc, (RequestsConnectionError, RequestsTimeout, TimeoutError, ConnectionError)):
        return ProviderFailure("transient_failure", "transport_error", diagnostic, True)
    if isinstance(exc, APIError):
        status = exc.status_code
        try:
            message = exc.message
        except Exception:  # malformed provider error body
            message = str(exc)
        normalized = message.lower()
        if status == 403 and (
            "recent sip" in normalized
            or "querying recent sip data" in normalized
            or "subscription does not permit querying recent" in normalized
        ):
            return ProviderFailure(
                "not_yet_available",
                "sip_delay_not_elapsed",
                diagnostic,
                True,
            )
        if status in {408, 429} or (status is not None and status >= 500):
            return ProviderFailure("transient_failure", f"http_{status}", diagnostic, True)
        if status in {401, 403}:
            return ProviderFailure("entitlement_error", f"http_{status}", diagnostic, False)
        if status is not None and 400 <= status < 500:
            return ProviderFailure("provider_error", f"http_{status}", diagnostic, False)
    return ProviderFailure("provider_error", "unexpected_provider_error", diagnostic, False)


def _date_range(start: date, end: date) -> tuple[date, ...]:
    return tuple(
        start + timedelta(days=offset)
        for offset in range((end - start).days + 1)
    )
