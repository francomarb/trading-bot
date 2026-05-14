"""
IV proxy resolver (PLAN.md 11.29).

The credit-spread strategy gates entries on an implied-volatility proxy —
it only sells premium when premium is rich enough. Alpaca paper does not
stream live Greeks, so each instrument points at a published volatility
index instead:

  * SPY, QQQ  → VIX  (``^VIX``)  — SPX-family, tracks both closely
  * IWM       → RVX  (``^RVX``)  — the Russell 2000 volatility index

Values are returned in **index points** (VIX convention: ``14.5`` means
14.5% annualized vol), matching how ``min_iv_proxy`` is expressed in
``CREDIT_SPREAD_INSTRUMENTS``. Callers that need a decimal sigma for a
Black-Scholes calculation divide by 100.

Network I/O (yfinance) is isolated behind an injectable ``fetch_fn`` so the
resolver is trivial to unit-test offline. Fetches are cached per calendar
day to keep the engine's hot loop off the network.
"""

from __future__ import annotations

from datetime import date
from typing import Callable

from loguru import logger


# Proxy source name (as written in CREDIT_SPREAD_INSTRUMENTS) → index ticker.
_SOURCE_TICKERS: dict[str, str] = {
    "vix": "^VIX",
    "rvx": "^RVX",
}

# Used when a live fetch fails and there is no cached value yet. ~VIX 15 is a
# neutral long-run level; a stale proxy should neither force nor block trades
# on its own — the gate is one of several.
_DEFAULT_FALLBACK_POINTS = 15.0


# fetch_fn signature: (yfinance_ticker: str) -> float | None (index points).
FetchFn = Callable[[str], "float | None"]


def _yfinance_fetch(ticker: str) -> float | None:
    """Default fetcher: latest daily close of a volatility index via yfinance."""
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker).history(period="1d")
        if hist.empty:
            return None
        return float(hist["Close"].iloc[-1])
    except Exception as e:
        logger.debug(f"iv_proxy: yfinance fetch failed for {ticker}: {e}")
        return None


class IVProxyResolver:
    """
    Resolves a per-instrument IV proxy source to a current index value.

    Caches each source's value for the calendar day — the proxy moves slowly
    enough that intraday refreshes add network risk without adding signal.
    """

    def __init__(
        self,
        *,
        fetch_fn: FetchFn | None = None,
        fallback_points: float = _DEFAULT_FALLBACK_POINTS,
    ) -> None:
        self._fetch_fn: FetchFn = fetch_fn or _yfinance_fetch
        self._fallback = fallback_points
        # source → (date, value_points)
        self._cache: dict[str, tuple[date, float]] = {}

    def resolve(self, source: str) -> float:
        """
        Return the current IV proxy for ``source`` in index points.

        ``source`` must be a key of ``_SOURCE_TICKERS`` (e.g. ``"vix"``).
        On a fetch failure the last cached value is reused; if nothing has
        ever been cached, the fallback is returned and a warning is logged.

        Raises ``ValueError`` for an unknown source — a config typo should
        fail loudly, not silently trade on a fallback.
        """
        key = source.lower()
        ticker = _SOURCE_TICKERS.get(key)
        if ticker is None:
            raise ValueError(
                f"unknown IV proxy source {source!r} — "
                f"valid sources: {sorted(_SOURCE_TICKERS)}"
            )

        today = date.today()
        cached = self._cache.get(key)
        if cached is not None and cached[0] == today:
            return cached[1]

        value = self._fetch_fn(ticker)
        if value is not None and value > 0:
            self._cache[key] = (today, value)
            logger.debug(f"iv_proxy: {key} ({ticker}) refreshed → {value:.2f}")
            return value

        # Fetch failed — reuse a stale cached value if we have one.
        if cached is not None:
            logger.warning(
                f"iv_proxy: {key} fetch failed; reusing stale value "
                f"{cached[1]:.2f} from {cached[0]}"
            )
            return cached[1]

        logger.warning(
            f"iv_proxy: {key} fetch failed and no cache — "
            f"using fallback {self._fallback:.2f}"
        )
        return self._fallback


def is_valid_source(source: str) -> bool:
    """True if ``source`` is a recognized IV proxy source name."""
    return source.lower() in _SOURCE_TICKERS
