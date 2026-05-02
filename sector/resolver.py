"""
Sector Resolver — maps stock tickers to normalized sector labels.

Uses a multi-layer lookup with persistent JSON caching:
  1. Local cache (data/cache/sector_map.json)
  2. yfinance metadata (industry-first, then sector fallback)

The resolver is hydrated at startup before the trading loop begins.
During live trading, only cache reads occur — no API calls.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from pathlib import Path

from loguru import logger


# ── Industry → sector key (checked first) ───────────────────────────────────

_INDUSTRY_MAP: dict[str, str] = {
    "semiconductors": "semiconductors",
    "semiconductor equipment & materials": "semiconductors",
    "semiconductor memory": "semiconductors",
    "semiconductor equipment": "semiconductors",
    "fabless semiconductors": "semiconductors",
    "solar": "energy",
    "oil & gas e&p": "energy",
    "oil & gas integrated": "energy",
    "oil & gas midstream": "energy",
    "oil & gas refining & marketing": "energy",
    "uranium": "energy",
}

# ── Sector → sector key (fallback when industry doesn't match) ──────────────

_SECTOR_MAP: dict[str, str] = {
    "technology": "technology",
    "information technology": "technology",
    "financial services": "financials",
    "financials": "financials",
    "energy": "energy",
    "utilities": "utilities",
    "healthcare": "healthcare",
    "health care": "healthcare",
    "industrials": "industrials",
    "consumer staples": "staples",
    "consumer defensive": "staples",
    "consumer discretionary": "discretionary",
    "consumer cyclical": "discretionary",
    "basic materials": "materials",
    "materials": "materials",
    "real estate": "real_estate",
    "communication services": "communications",
}


class SectorResolver:
    """Lazy-loading sector resolver with persistent JSON cache.

    Parameters
    ----------
    cache_path
        Path to the JSON cache file.  Created if it does not exist.
    valid_sectors
        Set of normalized sector keys (from ``settings.SECTOR_ETFS``).
        Lookups that normalize to a key outside this set are discarded.
    per_symbol_timeout
        Seconds before a single yfinance lookup is abandoned.
    max_retries
        Number of retries per symbol during ``hydrate()``.
    """

    def __init__(
        self,
        cache_path: Path = Path("data/cache/sector_map.json"),
        valid_sectors: set[str] | None = None,
        per_symbol_timeout: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self._cache_path = cache_path
        self._valid_sectors = valid_sectors or set()
        self._per_symbol_timeout = per_symbol_timeout
        self._max_retries = max_retries
        self._cache: dict[str, dict] = self._load_cache()

    # ── Public API ───────────────────────────────────────────────────────

    def resolve(self, symbol: str) -> str | None:
        """Return normalized sector label or ``None`` if unmappable.

        Checks ``settings.SYMBOL_SECTOR_OVERRIDES`` first so manual
        corrections survive cache refreshes.  Falls back to the JSON cache.
        Only reads from cache — never triggers an API call.
        Call ``hydrate()`` at startup to populate the cache.
        """
        try:
            from config import settings
            override = settings.SYMBOL_SECTOR_OVERRIDES.get(symbol)
            if override is not None:
                return override
        except Exception:
            pass

        entry = self._cache.get(symbol)
        if entry is None:
            return None
        return entry.get("normalized")

    def hydrate(self, symbols: list[str]) -> None:
        """Bulk-resolve symbols at startup.

        Skips symbols already in the cache.  Persists new lookups to the
        JSON file after each successful resolution.
        """
        missing = [s for s in symbols if s not in self._cache]
        if not missing:
            logger.info(f"sector resolver: all {len(symbols)} symbols cached")
            return

        logger.info(
            f"sector resolver: hydrating {len(missing)} uncached symbols "
            f"(of {len(symbols)} total)"
        )
        resolved = 0
        failed = 0
        for symbol in missing:
            entry = self._lookup_with_retry(symbol)
            if entry is not None:
                self._cache[symbol] = entry
                resolved += 1
            else:
                failed += 1

        self._save_cache()
        logger.info(
            f"sector resolver: hydrated {resolved} symbols, "
            f"{failed} failed, {len(self._cache)} total cached"
        )

    # ── Lookup chain ─────────────────────────────────────────────────────

    def _lookup_with_retry(self, symbol: str) -> dict | None:
        """Attempt yfinance lookup with retries and timeout."""
        for attempt in range(1, self._max_retries + 1):
            try:
                result = self._lookup_yfinance(symbol)
                if result is not None:
                    return result
                return None
            except Exception as exc:
                if attempt < self._max_retries:
                    backoff = attempt * 1.0
                    logger.debug(
                        f"sector resolver: {symbol} attempt {attempt} "
                        f"failed ({exc}), retrying in {backoff}s"
                    )
                    time.sleep(backoff)
                else:
                    logger.warning(
                        f"sector resolver: {symbol} failed after "
                        f"{self._max_retries} attempts: {exc}"
                    )
        return None

    def _lookup_yfinance(self, symbol: str) -> dict | None:
        """Fetch sector/industry from yfinance and normalize."""
        import yfinance as yf

        ticker = yf.Ticker(symbol)
        with open(os.devnull, "w") as devnull:
            with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                info = ticker.info

        if not info:
            return None

        quote_type = str(info.get("quoteType", "")).upper()
        if quote_type == "ETF":
            logger.debug(f"sector resolver: {symbol} is an ETF, skipping")
            return None

        raw_industry = str(info.get("industry", "")).strip()
        raw_sector = str(info.get("sector", "")).strip()

        normalized = self._normalize(raw_industry, raw_sector)
        if normalized is None:
            logger.debug(
                f"sector resolver: {symbol} unmapped — "
                f"industry={raw_industry!r}, sector={raw_sector!r}"
            )
            return None

        entry = {
            "sector": raw_sector,
            "industry": raw_industry,
            "normalized": normalized,
        }
        logger.debug(f"sector resolver: {symbol} → {normalized}")
        return entry

    def _normalize(
        self, raw_industry: str, raw_sector: str
    ) -> str | None:
        """Normalize raw yfinance strings to a standard sector key.

        Industry is checked first — this ensures semiconductor stocks
        (NVDA, MU, AMD) map to "semiconductors" rather than "technology".
        """
        industry_lower = raw_industry.lower()
        if industry_lower in _INDUSTRY_MAP:
            key = _INDUSTRY_MAP[industry_lower]
            if not self._valid_sectors or key in self._valid_sectors:
                return key

        sector_lower = raw_sector.lower()
        if sector_lower in _SECTOR_MAP:
            key = _SECTOR_MAP[sector_lower]
            if not self._valid_sectors or key in self._valid_sectors:
                return key

        return None

    # ── Cache persistence ────────────────────────────────────────────────

    def _load_cache(self) -> dict[str, dict]:
        """Load cache from disk. Returns empty dict on any error."""
        if not self._cache_path.exists():
            return {}
        try:
            with open(self._cache_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
        except Exception as exc:
            logger.warning(f"sector resolver: cache load failed: {exc}")
            return {}

    def _save_cache(self) -> None:
        """Persist cache to disk."""
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._cache_path, "w") as f:
                json.dump(self._cache, f, indent=2, sort_keys=True)
        except Exception as exc:
            logger.warning(f"sector resolver: cache save failed: {exc}")
