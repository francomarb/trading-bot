"""
Sector Resolver — maps stock tickers to normalized sector labels.

Uses a multi-layer lookup with persistent JSON caching:
  1. Manual overrides for deliberate trading classifications
  2. Fresh local cache entries (data/cache/sector_map.json)
  3. yfinance metadata (industry-first, then sector fallback)

Hydration refreshes a bounded number of missing or stale entries at startup.
During live trading, only cache reads occur — no API calls.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from loguru import logger


_CACHE_SCHEMA_VERSION = 1
_CACHE_PROVIDER = "yfinance"


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
    max_age_days
        Age after which a successful provider classification is eligible for
        refresh.  Legacy entries without provenance are immediately stale.
    max_refreshes_per_hydrate
        Maximum provider lookups attempted by one ``hydrate()`` call.  Missing
        symbols are prioritized, then the least-recently-attempted stale ones.
    clock
        UTC clock injection used by deterministic tests.
    """

    def __init__(
        self,
        cache_path: Path = Path("data/cache/sector_map.json"),
        valid_sectors: set[str] | None = None,
        per_symbol_timeout: float = 10.0,
        max_retries: int = 2,
        max_age_days: int = 90,
        max_refreshes_per_hydrate: int = 10,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_age_days <= 0:
            raise ValueError("max_age_days must be positive")
        if max_refreshes_per_hydrate <= 0:
            raise ValueError("max_refreshes_per_hydrate must be positive")
        self._cache_path = cache_path
        self._valid_sectors = valid_sectors or set()
        self._per_symbol_timeout = per_symbol_timeout
        self._max_retries = max_retries
        self._max_age = timedelta(days=max_age_days)
        self._max_refreshes_per_hydrate = max_refreshes_per_hydrate
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._cache: dict[str, dict[str, object]] = self._load_cache()

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
        normalized = entry.get("normalized")
        return normalized if isinstance(normalized, str) else None

    def hydrate(self, symbols: list[str]) -> None:
        """Refresh a bounded set of missing or stale symbols at startup.

        Missing classifications are attempted first.  Successful cache entries
        older than ``max_age_days`` are then refreshed from oldest to newest.
        Each result is atomically persisted before the next lookup, so an
        interrupted run retains all prior progress.  A failed refresh preserves
        the last known classification and records the attempt for fair rotation
        on the next startup.
        """
        unique_symbols = list(dict.fromkeys(symbols))
        candidates = [
            symbol for symbol in unique_symbols
            if not self._is_overridden(symbol) and self._needs_refresh(symbol)
        ]
        candidates.sort(key=self._refresh_priority)
        selected = candidates[:self._max_refreshes_per_hydrate]
        if not selected:
            logger.info(f"sector resolver: all {len(unique_symbols)} symbols fresh")
            return

        logger.info(
            f"sector resolver: refreshing {len(selected)} missing/stale symbols "
            f"(of {len(candidates)} due, {len(unique_symbols)} total; "
            f"budget={self._max_refreshes_per_hydrate})"
        )
        resolved = 0
        failed = 0
        for symbol in selected:
            entry = self._lookup_with_retry(symbol)
            attempted_at = self._utc_now_iso()
            if entry is not None:
                self._cache[symbol] = {
                    **entry,
                    "provider": _CACHE_PROVIDER,
                    "schema_version": _CACHE_SCHEMA_VERSION,
                    "fetched_at": attempted_at,
                    "last_attempted_at": attempted_at,
                }
                resolved += 1
            else:
                prior = self._cache.get(symbol, {})
                self._cache[symbol] = {
                    "sector": prior.get("sector", ""),
                    "industry": prior.get("industry", ""),
                    "normalized": prior.get("normalized"),
                    "provider": prior.get("provider", _CACHE_PROVIDER),
                    "schema_version": prior.get(
                        "schema_version", _CACHE_SCHEMA_VERSION
                    ),
                    "fetched_at": prior.get("fetched_at"),
                    "last_attempted_at": attempted_at,
                }
                failed += 1
            self._save_cache()

        logger.info(
            f"sector resolver: refreshed {resolved} symbols, {failed} failed, "
            f"{len(candidates) - len(selected)} deferred by budget, "
            f"{len(self._cache)} total cached"
        )

    def _is_overridden(self, symbol: str) -> bool:
        """Return whether an operator override owns this classification."""
        try:
            from config import settings
            return symbol in settings.SYMBOL_SECTOR_OVERRIDES
        except Exception:
            return False

    def _needs_refresh(self, symbol: str) -> bool:
        """Return whether a cache entry is missing, invalid, legacy, or stale."""
        entry = self._cache.get(symbol)
        if not entry or not entry.get("normalized"):
            return True
        if entry.get("provider") != _CACHE_PROVIDER:
            return True
        if entry.get("schema_version") != _CACHE_SCHEMA_VERSION:
            return True
        fetched_at = self._parse_timestamp(entry.get("fetched_at"))
        if fetched_at is None:
            return True
        return self._now() - fetched_at >= self._max_age

    def _refresh_priority(self, symbol: str) -> tuple[int, datetime, str]:
        """Prioritize missing values, then the least recently attempted entry."""
        entry = self._cache.get(symbol, {})
        missing_rank = 0 if not entry.get("normalized") else 1
        attempted = self._parse_timestamp(entry.get("last_attempted_at"))
        fetched = self._parse_timestamp(entry.get("fetched_at"))
        oldest = attempted or fetched or datetime.min.replace(tzinfo=timezone.utc)
        return missing_rank, oldest, symbol

    def _now(self) -> datetime:
        """Return an aware UTC timestamp from the configured clock."""
        value = self._clock()
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _utc_now_iso(self) -> str:
        """Return a stable RFC 3339 UTC timestamp for cache metadata."""
        return self._now().isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_timestamp(value: object) -> datetime | None:
        """Parse cache timestamps, treating malformed/naive values as stale."""
        if not isinstance(value, str) or not value:
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return None
        return parsed.astimezone(timezone.utc)

    # ── Lookup chain ─────────────────────────────────────────────────────

    def _lookup_with_retry(self, symbol: str) -> dict[str, object] | None:
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

    def _lookup_yfinance(self, symbol: str) -> dict[str, object] | None:
        """Fetch sector/industry from yfinance and normalize."""
        import yfinance as yf

        # Alpaca uses dot-class symbols while Yahoo uses hyphens (BRK.B vs
        # BRK-B). Normalize only at the provider boundary; cache keys and all
        # runtime ownership continue to use the broker symbol.
        ticker = yf.Ticker(symbol.replace(".", "-"))
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

    def _load_cache(self) -> dict[str, dict[str, object]]:
        """Load cache from disk. Returns empty dict on any error."""
        if not self._cache_path.exists():
            return {}
        try:
            with open(self._cache_path) as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {
                    str(symbol): entry
                    for symbol, entry in data.items()
                    if isinstance(entry, dict)
                }
            return {}
        except Exception as exc:
            logger.warning(f"sector resolver: cache load failed: {exc}")
            return {}

    def _save_cache(self) -> None:
        """Atomically persist the complete cache to disk."""
        temp_path: Path | None = None
        try:
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self._cache_path.parent,
                prefix=f".{self._cache_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_path = Path(handle.name)
                json.dump(self._cache, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self._cache_path)
        except Exception as exc:
            logger.warning(f"sector resolver: cache save failed: {exc}")
            if temp_path is not None:
                with contextlib.suppress(OSError):
                    temp_path.unlink()
