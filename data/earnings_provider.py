"""Bounded official Alpaca adapters and SEC public metadata retrieval."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime, timedelta
from typing import Any

import requests
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetCalendarRequest

from config import settings
from data.earnings_observation import Citation, NEW_YORK, Session, utc_stamp, UTC
from data.preopen_provider import classify_provider_exception
from utils.alpaca_transport import configure_alpaca_client


class EarningsCalendar:
    """Use the project's calendar API with closes required by earnings windows."""

    def __init__(self, client: Any = None) -> None:
        self.client = client

    def sessions(self, release: datetime) -> tuple[Session, ...]:
        """Fetch a bounded interval including prior and three subsequent sessions."""
        if self.client is None:
            self.client = configure_alpaca_client(
                TradingClient(
                    settings.ALPACA_API_KEY,
                    settings.ALPACA_SECRET_KEY,
                    paper=settings.ALPACA_PAPER,
                )
            )
        day = utc_stamp(release).astimezone(NEW_YORK).date()
        pad = timedelta(days=settings.EARNINGS_CALENDAR_PAD_DAYS)
        rows = self.client.get_calendar(
            GetCalendarRequest(start=day - pad, end=day + pad)
        )
        sessions = []
        for row in rows:
            opened = (
                row.open.replace(tzinfo=NEW_YORK)
                if row.open.tzinfo is None
                else row.open
            )
            closed = (
                row.close.replace(tzinfo=NEW_YORK)
                if row.close.tzinfo is None
                else row.close
            )
            sessions.append(
                Session(session_date=row.date, open_at=opened, close_at=closed)
            )
        if len({s.session_date for s in sessions}) != len(sessions):
            raise ValueError("duplicate calendar sessions")
        return tuple(sessions)


class EarningsSIPProvider:
    """Exact split-adjusted marks with no cross-run cache."""

    def __init__(self, client: Any = None) -> None:
        self.client = client

    def mark(
        self, symbol: str, target: datetime, *, minute: bool, now: datetime
    ) -> dict | None:
        """Return the exact target bar, never a nearby or future bar."""
        target, now = utc_stamp(target), utc_stamp(now)
        if now < target + timedelta(minutes=settings.EARNINGS_SIP_DELAY_MINUTES):
            raise ValueError("SIP target has not matured")
        if self.client is None:
            self.client = configure_alpaca_client(
                StockHistoricalDataClient(
                    settings.ALPACA_API_KEY, settings.ALPACA_SECRET_KEY
                )
            )
        target_day = target.astimezone(NEW_YORK).date()
        start = (
            target - timedelta(minutes=1)
            if minute
            else datetime.combine(target_day, datetime.min.time(), NEW_YORK).astimezone(
                UTC
            )
        )
        # Inclusive API end: exclude the minute beginning at the target.
        end = target - timedelta(microseconds=1) if minute else target
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            start=start,
            end=end,
            timeframe=TimeFrame.Minute if minute else TimeFrame.Day,
            adjustment=Adjustment.SPLIT,
            feed=DataFeed.SIP,
            asof=target_day.isoformat(),
            limit=100,
        )
        try:
            response = self.client.get_stock_bars(request)
        except Exception as exc:
            raise classify_provider_exception(exc) from exc
        rows = [] if response is None else response.data.get(symbol, [])
        matches = [row for row in rows if utc_stamp(row.timestamp) == start]
        if len(matches) > 1:
            raise ValueError("multiple bars for the exact mark")
        result = None
        if matches:
            price = float(matches[0].close)
            if math.isfinite(price) and price > 0:
                result = {
                    "price": price,
                    "bar_at": start.isoformat(),
                    "target_at": target.isoformat(),
                    "feed": "sip",
                    "adjustment": "split",
                    "currency": "USD",
                    "endpoint": "historical_bars",
                    "timeframe": "1Min" if minute else "1Day",
                }
        return result


class SECMetadataProvider:
    """One bounded recent-submissions request; operator selects the exact accession."""

    def __init__(self, user_agent: str, session: Any = None) -> None:
        if (
            len(user_agent) < 10
            or "@" not in user_agent
            or any(ord(c) < 32 for c in user_agent)
        ):
            raise ValueError(
                "set EARNINGS_SEC_USER_AGENT to an identifying application and contact email"
            )
        self.user_agent = user_agent
        self.session = session or requests.Session()
        self.last_request = float("-inf")

    def citation(
        self,
        *,
        cik: str,
        accession: str,
        issuer_id: str,
        symbol: str,
        now: datetime,
        paraphrase: str,
    ) -> Citation:
        """Read SEC metadata only; never treat filing acceptance as issuer release."""
        if not re.fullmatch(r"\d{1,10}", cik) or not re.fullmatch(
            r"\d{10}-\d{2}-\d{6}", accession
        ):
            raise ValueError("invalid CIK or accession")
        if (
            time.monotonic() - self.last_request
            < settings.EARNINGS_SEC_MIN_INTERVAL_SECONDS
        ):
            raise ValueError("SEC request interval not elapsed; retry later")
        self.last_request = time.monotonic()
        url = f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"
        with self.session.get(
            url,
            headers={"User-Agent": self.user_agent},
            timeout=(5, 15),
            stream=True,
            allow_redirects=False,
        ) as response:
            response.raise_for_status()
            if response.status_code != 200:
                raise ValueError("unexpected SEC response")
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > settings.EARNINGS_SEC_MAX_BYTES:
                    raise ValueError("SEC response exceeds byte bound")
                chunks.append(chunk)
        payload = b"".join(chunks)
        data = json.loads(payload)
        if int(data["cik"]) != int(cik):
            raise ValueError("SEC issuer identity mismatch")
        if symbol not in data.get("tickers", []):
            raise ValueError("SEC CIK does not match the registered symbol")
        recent = data["filings"]["recent"]
        index = recent["accessionNumber"].index(accession)
        form = recent["form"][index]
        if form not in {"8-K", "10-Q", "10-K", "6-K", "20-F"}:
            raise ValueError("unsupported earnings source form")
        document = recent["primaryDocument"][index]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", document):
            raise ValueError("unexpected SEC document name")
        accepted = datetime.fromisoformat(
            recent["acceptanceDateTime"][index].replace("Z", "+00:00")
        )
        return Citation(
            issuer_id=issuer_id,
            source_type="sec_filing",
            title=f"SEC {form} {accession}",
            url=f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{document}",
            published_at=accepted,
            accessed_at=now,
            paraphrase=paraphrase,
            accession=accession,
            content_hash=hashlib.sha256(payload).hexdigest(),
        )
