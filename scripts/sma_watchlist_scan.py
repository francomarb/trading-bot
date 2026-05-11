#!/usr/bin/env python3
"""
SMA watchlist scanner.

Builds a ranked list of SMA crossover candidates using the documented
`sma_watchlist_v1` rules in docs/sma-watchlist-selection.md.

Data sources:
  - Alpaca Trading API: active/tradable US equity universe
  - Alpaca Market Data API: adjusted daily OHLCV bars
  - Optional Yahoo Finance fundamentals: existing watchlist-review checks

Usage:
    python scripts/sma_watchlist_scan.py
    python scripts/sma_watchlist_scan.py --top 30 --max-assets 500
    python scripts/sma_watchlist_scan.py --include-fundamentals --output logs/sma_scan.md
    python scripts/sma_watchlist_scan.py --feed sip --end-delay-minutes 60
    python scripts/sma_watchlist_scan.py --explain-symbols NVDA MSFT AVGO

The scanner is report-only. It does not modify config/settings.py or any live
strategy slot.
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetClass, AssetStatus
from alpaca.trading.requests import GetAssetsRequest
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from data.fetcher import _get_client, _install_timeout
from indicators.technicals import add_atr, add_sma
from scripts.watchlist_review import SMA_PROFILE, assess_fitness, fetch_fundamentals
from utils.asset_filters import is_stock_like
from utils.market import apply_synthetic_sip_volume


RULE_VERSION = "sma_watchlist_v2"


@dataclass(frozen=True)
class ScanConfig:
    """Thresholds for the SMA watchlist scanner."""

    min_bars: int = 260
    min_market_cap: float = 2_000_000_000.0
    min_price: float = 10.0
    min_avg_volume_20: float = 500_000.0
    min_avg_dollar_volume_50: float = 50_000_000.0
    min_above_52w_low: float = 1.30
    min_pct_of_52w_high: float = 0.75
    min_relative_strength_pct: float = 70.0
    min_adx: float = 20.0
    preferred_adx: float = 25.0
    min_atr_pct: float = 0.01
    max_atr_pct: float = 0.08
    max_per_sector: int = 3


@dataclass
class Candidate:
    """One symbol that passed the SMA scanner."""

    symbol: str
    name: str
    exchange: str
    sector: str
    close: float
    avg_volume_20: float
    avg_dollar_volume_50: float
    sma20: float
    sma50: float
    sma150: float
    sma200: float
    adx14: float
    plus_di14: float
    minus_di14: float
    atr_pct: float
    high_52w: float
    low_52w: float
    momentum_12m_skip_1m: float
    relative_strength_pct: float
    crossover_count_1y: int
    score: float
    notes: list[str] = field(default_factory=list)


@dataclass
class AssetInfo:
    """Small projection of an Alpaca asset."""

    symbol: str
    name: str
    exchange: str
    sector: str = "UNKNOWN"


REJECTION_LABELS: dict[str, str] = {
    "insufficient_or_bad_bars": "Not enough clean daily bars for 200-day trend and 12-month momentum checks.",
    "price": "Latest close is below the minimum price threshold.",
    "share_volume": "20-day average share volume is below the liquidity threshold.",
    "dollar_volume": "50-day average dollar volume is below the liquidity threshold.",
    "price_above_smas": "Price is not above SMA50, SMA150, and SMA200.",
    "sma_alignment": "Moving averages are not stacked as SMA50 > SMA150 > SMA200.",
    "above_52w_low": "Price is not at least 30% above the 52-week low.",
    "near_52w_high": "Price is not at least 75% of the 52-week high.",
    "adx": "ADX14 is below the minimum trend-strength threshold.",
    "di_direction": "+DI is not above -DI, so directional pressure is not bullish.",
    "atr_too_low": "ATR14 / close is too low; the name may be too quiet for SMA trend following.",
    "atr_too_high": "ATR14 / close is too high; the name may be too unstable for SMA trend following.",
    "relative_strength": "12-month momentum excluding the latest month is below the top-30% cutoff.",
    "market_cap": "Market capitalization is below the SMA minimum size threshold.",
    "fundamental_sanity": "SMA fundamental sanity check failed.",
    "etf_quotetype": "yfinance quoteType is ETF; SMA crossover applies to single-name trends, not basket products.",
    "biotech_industry": "Industry is biotech / specialty pharma / diagnostics; binary-catalyst risk is a poor fit for SMA trend-following.",
    "share_class_dup": "Duplicate share class for the same underlying company (canonical/shorter-ticker variant kept).",
}

_INDUSTRY_CACHE_PATH = Path("data/cache/symbol_industry_cache.json")

# Industries treated as binary-catalyst (clinical/regulatory event-driven).
# Matched case-insensitively after dash normalization, so em-dash and
# en-dash variants from yfinance are treated as equivalent to hyphen.
_BIOTECH_INDUSTRY_SUBSTRINGS: tuple[str, ...] = (
    "BIOTECHNOLOGY",
    "SPECIALTY & GENERIC",   # "Drug Manufacturers - Specialty & Generic"
    "DIAGNOSTICS & RESEARCH",
)


def _normalize_industry(industry: str) -> str:
    return (industry or "").upper().replace("—", "-").replace("–", "-").strip()


def _is_biotech_industry(industry: str) -> bool:
    norm = _normalize_industry(industry)
    return any(s in norm for s in _BIOTECH_INDUSTRY_SUBSTRINGS)


def _normalize_company_name(name: str) -> str:
    """Collapse share-class variants to a single company key.

    'Alphabet Inc Class A Common Stock' and 'Alphabet Inc Class C Capital
    Stock' both normalize to 'ALPHABET INC'.
    """
    if not name:
        return ""
    text = name.upper()
    for marker in (" - CLASS ", " CLASS ", " SERIES "):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx]
            break
    return text.strip().rstrip(".").strip()


def _load_industry_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        import json
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        logger.warning(f"industry cache: failed to read {path}; starting fresh")
        return {}


def _save_industry_cache(cache: dict[str, dict], path: Path) -> None:
    import json
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True), encoding="utf-8")


def _hydrate_industry_cache(
    symbols: list[str],
    cache: dict[str, dict],
    *,
    save_every: int = 50,
) -> dict[str, dict]:
    """Fill industry cache entries for any symbol not already cached.

    Hits yfinance once per uncached symbol. Failures are recorded as empty
    entries so we do not retry on every run. The cache is persisted
    incrementally so a long run can be interrupted without losing work.
    """
    missing = [s for s in symbols if s not in cache]
    if not missing:
        return cache
    import yfinance as yf
    logger.info(
        f"industry cache: hydrating {len(missing)} symbol(s) "
        f"(already cached: {len(cache)})"
    )
    new_count = 0
    skipped = 0
    for sym in missing:
        info: dict = {}
        try:
            info = yf.Ticker(sym).info or {}
        except Exception as exc:
            logger.warning(f"industry cache: {sym} fetch failed: {exc}")
        quote_type = str(info.get("quoteType", "") or "").upper()
        industry = str(info.get("industry", "") or "")
        sector = str(info.get("sector", "") or "")
        long_name = str(info.get("longName", "") or info.get("shortName", "") or "")
        # Don't persist empty lookups — yfinance occasionally rate-limits or
        # returns blank info. Persisting empties would cause the biotech /
        # ETF gates to silently fail-open on the next run.
        if not (quote_type or industry or sector or long_name):
            skipped += 1
            continue
        cache[sym] = {
            "quoteType": quote_type,
            "industry": industry,
            "sector": sector,
            "longName": long_name,
        }
        new_count += 1
        if new_count % save_every == 0:
            _save_industry_cache(cache, _INDUSTRY_CACHE_PATH)
            logger.info(f"industry cache: hydrated {new_count}/{len(missing)}")
    _save_industry_cache(cache, _INDUSTRY_CACHE_PATH)
    logger.info(
        f"industry cache: hydration complete "
        f"({new_count} new entries, {skipped} empty / not persisted)"
    )
    return cache


def get_open_sma_positions(db_path: str) -> set[str]:
    """Return symbols with a net-positive filled SMA position in the trade DB.

    Honoring these symbols on watchlist refresh prevents the engine from
    orphaning open positions whose names no longer pass the scanner.
    """
    path = Path(db_path)
    if not path.exists():
        return set()
    try:
        conn = sqlite3.connect(str(path))
        try:
            cur = conn.cursor()
            rows = cur.execute(
                """
                SELECT symbol,
                       SUM(CASE WHEN side='buy' THEN COALESCE(filled_qty, qty)
                                ELSE -COALESCE(filled_qty, qty) END) AS net
                FROM trades
                WHERE strategy='sma_crossover' AND status='filled'
                GROUP BY symbol
                HAVING net > 0
                """
            ).fetchall()
            return {str(r[0]).upper() for r in rows}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.warning(f"open-positions query failed against {db_path}: {exc}")
        return set()


def configure_logging(verbose: bool) -> None:
    """Configure concise CLI logging."""
    logger.remove()
    logger.add(
        sys.stdout,
        level="DEBUG" if verbose else "INFO",
        format="<green>{time:HH:mm:ss}</green> | <level>{level:<7}</level> | {message}",
    )


def get_tradable_assets(max_assets: int | None = None) -> list[AssetInfo]:
    """Fetch active, tradable US equities from Alpaca."""
    client = TradingClient(
        api_key=settings.ALPACA_API_KEY,
        secret_key=settings.ALPACA_SECRET_KEY,
        paper=settings.ALPACA_PAPER,
    )
    _install_timeout(client._session)

    request = GetAssetsRequest(
        status=AssetStatus.ACTIVE,
        asset_class=AssetClass.US_EQUITY,
    )
    assets = client.get_all_assets(request)
    selected: list[AssetInfo] = []
    for asset in assets:
        if not getattr(asset, "tradable", False):
            continue
        symbol = str(asset.symbol).upper()
        name = str(getattr(asset, "name", "") or "")
        exchange = str(getattr(asset, "exchange", "") or "")
        if _is_excluded_asset(symbol, name, exchange):
            continue
        selected.append(AssetInfo(symbol=symbol, name=name, exchange=exchange))

    selected = sorted(selected, key=lambda a: a.symbol)
    if max_assets is not None:
        selected = selected[:max_assets]
    return selected


def _is_excluded_asset(symbol: str, name: str, exchange: str) -> bool:
    """Reject ETFs, funds, preferreds, warrants, leveraged products, OTCs.

    Thin wrapper around the shared ``is_stock_like`` heuristic so the
    exclusion logic stays consistent with other watchlist scanners.
    """
    return not is_stock_like(symbol, name, exchange)


def fetch_daily_bars(
    symbols: list[str],
    start: datetime,
    end: datetime,
    *,
    chunk_size: int,
    feed: str,
) -> dict[str, pd.DataFrame]:
    """Fetch adjusted daily bars from Alpaca in symbol chunks."""
    client = _get_client()
    feed_enum = DataFeed.IEX if feed.lower() == "iex" else DataFeed.SIP
    out: dict[str, pd.DataFrame] = {}

    for i, chunk in enumerate(_chunks(symbols, chunk_size), start=1):
        logger.info(
            f"fetching daily bars chunk {i}: {len(chunk)} symbol(s) "
            f"{start.date()} -> {end.date()}"
        )
        request = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Day,
            start=start,
            end=end,
            adjustment=Adjustment.ALL,
            feed=feed_enum,
        )
        bars = _call_with_retry(lambda: client.get_stock_bars(request), "get_stock_bars")
        if bars is None or bars.df is None or bars.df.empty:
            continue

        df = bars.df
        if not isinstance(df.index, pd.MultiIndex):
            continue
        for symbol in chunk:
            try:
                sym_df = df.xs(symbol, level="symbol").copy()
            except KeyError:
                continue
            sym_df = sym_df[[c for c in ["open", "high", "low", "close", "volume"] if c in sym_df]]
            if sym_df.empty:
                continue
            sym_df = sym_df.sort_index()
            if feed.lower() == "iex":
                sym_df = apply_synthetic_sip_volume(sym_df, is_daily=True)
            out[symbol] = sym_df
    return out


def _call_with_retry(fn, op_desc: str, max_attempts: int = 5):
    """Small retry wrapper for scan-time Alpaca calls."""
    delay = 1.0
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return fn()
        except APIError as exc:
            last_exc = exc
            status = exc.status_code
            if status == 429 or (status is not None and 500 <= status < 600):
                logger.warning(
                    f"{op_desc} attempt {attempt}/{max_attempts} failed "
                    f"(status={status}); sleeping {delay:.1f}s"
                )
                time.sleep(delay)
                delay *= 2
                continue
            raise
        except (ConnectionError, TimeoutError) as exc:
            last_exc = exc
            logger.warning(
                f"{op_desc} attempt {attempt}/{max_attempts} network error; "
                f"sleeping {delay:.1f}s"
            )
            time.sleep(delay)
            delay *= 2
    assert last_exc is not None
    raise last_exc


def _chunks(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def scan_candidates(
    assets: list[AssetInfo],
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    config: ScanConfig,
    include_fundamentals: bool,
    top: int,
    explain_symbols: set[str] | None = None,
    protected_symbols: set[str] | None = None,
) -> tuple[list[Candidate], Counter[str], dict[str, list[str]], dict[str, str]]:
    """Apply SMA v1 rules and return ranked candidates plus rejection details."""
    asset_by_symbol = {a.symbol: a for a in assets}
    rejections: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    explanations: dict[str, str] = {}
    explain_symbols = explain_symbols or set()
    protected_symbols = {s.upper() for s in (protected_symbols or set())}

    # Step 1: Compute metrics and apply §1–§7 technical rejections.
    # RS percentile is taken against the *survivor pool* (post-technical), not
    # the full valid-bars universe, because junk/illiquid/downtrending names
    # would otherwise drag the distribution down and make the 70%-cutoff
    # easier than the doc intends (§5: "candidate universe").
    all_metrics: dict[str, dict[str, float | int]] = {}
    for symbol, df in bars_by_symbol.items():
        metric = _compute_metrics(symbol, df, config)
        if metric is None:
            _reject(symbol, "insufficient_or_bad_bars", rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = "Rejected: insufficient_or_bad_bars"
            continue
        all_metrics[symbol] = metric

    if not all_metrics:
        for symbol in explain_symbols - set(explanations):
            explanations[symbol] = "No bars returned from Alpaca."
        return [], rejections, examples, explanations

    metrics: dict[str, dict[str, float | int]] = {}
    for symbol, metric in all_metrics.items():
        reason = _first_technical_rejection(metric, config)
        if reason is not None:
            _reject(symbol, reason, rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = _format_explanation(
                    symbol, reason, metric, None, config
                )
            continue
        metrics[symbol] = metric

    # RS percentile against the survivor pool only.
    if metrics:
        survivor_momentums = pd.Series(
            {sym: float(m["momentum_12m_skip_1m"]) for sym, m in metrics.items()}
        )
        rs_pct = survivor_momentums.rank(pct=True) * 100.0
    else:
        rs_pct = pd.Series(dtype=float)

    ranked: list[Candidate] = []
    for symbol, metric in metrics.items():
        relative_strength_pct = float(rs_pct[symbol])
        if relative_strength_pct < config.min_relative_strength_pct:
            _reject(symbol, "relative_strength", rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = _format_explanation(
                    symbol, "relative_strength", metric, relative_strength_pct, config
                )
            continue

        if include_fundamentals:
            fundamentals = fetch_fundamentals(symbol)
            if (
                fundamentals.market_cap is None
                or fundamentals.market_cap < config.min_market_cap
            ):
                _reject(symbol, "market_cap", rejections, examples)
                if symbol in explain_symbols:
                    explanations[symbol] = _format_explanation(
                        symbol,
                        "market_cap",
                        metric,
                        relative_strength_pct,
                        config,
                        market_cap=fundamentals.market_cap,
                    )
                continue
            fitness = assess_fitness(fundamentals, SMA_PROFILE)
            if fitness.verdict != "✅ GOOD FIT":
                _reject(symbol, "fundamental_sanity", rejections, examples)
                if symbol in explain_symbols:
                    explanations[symbol] = _format_explanation(
                    symbol, "fundamental_sanity", metric, relative_strength_pct, config
                )
                continue

        asset = asset_by_symbol.get(symbol, AssetInfo(symbol, symbol, "UNKNOWN"))
        score = _score_candidate(metric, relative_strength_pct, config)
        ranked.append(
            Candidate(
                symbol=symbol,
                name=asset.name,
                exchange=asset.exchange,
                sector=asset.sector,
                close=float(metric["close"]),
                avg_volume_20=float(metric["avg_volume_20"]),
                avg_dollar_volume_50=float(metric["avg_dollar_volume_50"]),
                sma20=float(metric["sma20"]),
                sma50=float(metric["sma50"]),
                sma150=float(metric["sma150"]),
                sma200=float(metric["sma200"]),
                adx14=float(metric["adx14"]),
                plus_di14=float(metric["plus_di14"]),
                minus_di14=float(metric["minus_di14"]),
                atr_pct=float(metric["atr_pct"]),
                high_52w=float(metric["high_52w"]),
                low_52w=float(metric["low_52w"]),
                momentum_12m_skip_1m=float(metric["momentum_12m_skip_1m"]),
                relative_strength_pct=relative_strength_pct,
                crossover_count_1y=int(metric["crossover_count_1y"]),
                score=score,
            )
        )
        if symbol in explain_symbols:
            explanations[symbol] = _format_explanation(
                symbol, "PASS", metric, relative_strength_pct, config
            )

    # Step 2: industry-aware filters. Eager — applied to every RS-passing
    # survivor so biotech / ETF rejections show up in the rejection counter
    # and do not silently steal slots from a top-N truncation. Protected
    # (open-position) symbols bypass these checks; the engine must not
    # orphan a held position.
    industry_cache = _load_industry_cache(_INDUSTRY_CACHE_PATH)
    if ranked:
        industry_cache = _hydrate_industry_cache(
            [c.symbol for c in ranked], industry_cache
        )

    industry_kept: list[Candidate] = []
    for candidate in ranked:
        info = industry_cache.get(candidate.symbol, {})
        if candidate.symbol in protected_symbols:
            industry_kept.append(candidate)
            continue
        quote_type = (info.get("quoteType") or "").upper()
        if quote_type == "ETF":
            _reject(candidate.symbol, "etf_quotetype", rejections, examples)
            if candidate.symbol in explain_symbols:
                explanations[candidate.symbol] = (
                    f"Rejected: etf_quotetype. yfinance quoteType=ETF."
                )
            continue
        industry = info.get("industry") or ""
        if _is_biotech_industry(industry):
            _reject(candidate.symbol, "biotech_industry", rejections, examples)
            if candidate.symbol in explain_symbols:
                explanations[candidate.symbol] = (
                    f"Rejected: biotech_industry ({industry!r})."
                )
            continue
        industry_kept.append(candidate)
    ranked = industry_kept

    ranked.sort(key=lambda c: c.score, reverse=True)

    # Step 3: share-class dedup. Group by normalized company name from
    # yfinance ``longName`` (falling back to Alpaca asset name). Within a
    # group, keep the shorter ticker; break ties by higher $vol50.
    # Protected symbols are never deduped away — if both share classes are
    # held, both survive.
    grouped: dict[str, list[Candidate]] = defaultdict(list)
    for candidate in ranked:
        info = industry_cache.get(candidate.symbol, {})
        company = _normalize_company_name(info.get("longName") or candidate.name)
        key = company if company else f"__solo__:{candidate.symbol}"
        grouped[key].append(candidate)

    deduped: list[Candidate] = []
    for key, group in grouped.items():
        if len(group) == 1:
            deduped.append(group[0])
            continue
        protected_in_group = [c for c in group if c.symbol in protected_symbols]
        if protected_in_group:
            keep_set = {c.symbol for c in protected_in_group}
            for c in group:
                if c.symbol not in keep_set:
                    _reject(c.symbol, "share_class_dup", rejections, examples)
                    if c.symbol in explain_symbols:
                        explanations[c.symbol] = (
                            f"Rejected: share_class_dup (protected sibling "
                            f"{sorted(keep_set)} held)."
                        )
            deduped.extend(protected_in_group)
            continue
        group.sort(key=lambda c: (len(c.symbol), -c.avg_dollar_volume_50))
        winner = group[0]
        deduped.append(winner)
        for c in group[1:]:
            _reject(c.symbol, "share_class_dup", rejections, examples)
            if c.symbol in explain_symbols:
                explanations[c.symbol] = (
                    f"Rejected: share_class_dup (kept {winner.symbol})."
                )
    ranked = sorted(deduped, key=lambda c: c.score, reverse=True)

    ranked = _apply_sector_cap(ranked, config.max_per_sector, top)

    # Force-include protected symbols (open positions) that didn't make the cut.
    # The engine would otherwise orphan their open positions on the next
    # watchlist swap. Annotate so the operator can see why each is present.
    if protected_symbols:
        in_output = {c.symbol for c in ranked}
        missing = protected_symbols - in_output
        for sym in sorted(missing):
            asset = asset_by_symbol.get(sym, AssetInfo(sym, sym, "UNKNOWN"))
            metric = all_metrics.get(sym)
            if metric is None:
                stub = Candidate(
                    symbol=sym, name=asset.name, exchange=asset.exchange,
                    sector=asset.sector, close=float("nan"),
                    avg_volume_20=float("nan"), avg_dollar_volume_50=float("nan"),
                    sma20=float("nan"), sma50=float("nan"),
                    sma150=float("nan"), sma200=float("nan"),
                    adx14=float("nan"), plus_di14=float("nan"),
                    minus_di14=float("nan"), atr_pct=float("nan"),
                    high_52w=float("nan"), low_52w=float("nan"),
                    momentum_12m_skip_1m=float("nan"),
                    relative_strength_pct=float("nan"),
                    crossover_count_1y=0, score=float("nan"),
                    notes=["PROTECTED: open position; no bars/metrics available"],
                )
                ranked.append(stub)
                continue
            rs = float(rs_pct[sym]) if sym in rs_pct.index else float("nan")
            reason = _first_technical_rejection(metric, config)
            note_bits = ["PROTECTED: open position"]
            if reason is not None:
                note_bits.append(f"would fail: {reason}")
            elif not math.isnan(rs) and rs < config.min_relative_strength_pct:
                note_bits.append(f"would fail: relative_strength (RS%={rs:.1f})")
            ranked.append(
                Candidate(
                    symbol=sym, name=asset.name, exchange=asset.exchange,
                    sector=asset.sector,
                    close=float(metric["close"]),
                    avg_volume_20=float(metric["avg_volume_20"]),
                    avg_dollar_volume_50=float(metric["avg_dollar_volume_50"]),
                    sma20=float(metric["sma20"]),
                    sma50=float(metric["sma50"]),
                    sma150=float(metric["sma150"]),
                    sma200=float(metric["sma200"]),
                    adx14=float(metric["adx14"]),
                    plus_di14=float(metric["plus_di14"]),
                    minus_di14=float(metric["minus_di14"]),
                    atr_pct=float(metric["atr_pct"]),
                    high_52w=float(metric["high_52w"]),
                    low_52w=float(metric["low_52w"]),
                    momentum_12m_skip_1m=float(metric["momentum_12m_skip_1m"]),
                    relative_strength_pct=rs,
                    crossover_count_1y=int(metric["crossover_count_1y"]),
                    score=float("nan"),
                    notes=note_bits,
                )
            )

    for symbol in explain_symbols - set(explanations):
        explanations[symbol] = "No bars returned from Alpaca."
    return ranked, rejections, examples, explanations


def _compute_metrics(
    symbol: str,
    df: pd.DataFrame,
    config: ScanConfig,
) -> dict[str, float | int] | None:
    """Compute all technical metrics needed by the scanner."""
    required = {"open", "high", "low", "close", "volume"}
    if df.empty or not required.issubset(df.columns):
        return None
    if len(df) < config.min_bars:
        return None
    if df[list(required)].isna().any().any():
        return None

    work = add_sma(df, 20)
    work = add_sma(work, 50)
    work = add_sma(work, 150)
    work = add_sma(work, 200)
    work = add_atr(work, 14)
    work = _add_adx(work, 14)

    last = work.iloc[-1]
    if last[["sma_50", "sma_150", "sma_200", "atr_14", "adx_14"]].isna().any():
        return None

    close = float(last["close"])
    if close <= 0:
        return None

    high_52w = float(work["high"].tail(252).max())
    low_52w = float(work["low"].tail(252).min())
    avg_volume_20 = float(work["volume"].tail(20).mean())
    avg_dollar_volume_50 = float((work["close"] * work["volume"]).tail(50).mean())
    atr_pct = float(last["atr_14"] / close)

    momentum_start = float(work["close"].iloc[-253])
    momentum_end = float(work["close"].iloc[-22])
    if momentum_start <= 0:
        return None
    momentum = momentum_end / momentum_start - 1.0

    diff = work["sma_20"] - work["sma_50"]
    crossovers = ((diff > 0) & (diff.shift(1) <= 0)) | (
        (diff < 0) & (diff.shift(1) >= 0)
    )
    crossover_count = int(crossovers.tail(252).fillna(False).sum())

    return {
        "close": close,
        "sma20": float(last["sma_20"]),
        "sma50": float(last["sma_50"]),
        "sma150": float(last["sma_150"]),
        "sma200": float(last["sma_200"]),
        "sma200_20d_ago": float(work["sma_200"].iloc[-21]),
        "avg_volume_20": avg_volume_20,
        "avg_dollar_volume_50": avg_dollar_volume_50,
        "high_52w": high_52w,
        "low_52w": low_52w,
        "adx14": float(last["adx_14"]),
        "adx14_5d_ago": float(work["adx_14"].iloc[-6]),
        "plus_di14": float(last["plus_di_14"]),
        "minus_di14": float(last["minus_di_14"]),
        "atr_pct": atr_pct,
        "momentum_12m_skip_1m": momentum,
        "crossover_count_1y": crossover_count,
    }


def _add_adx(df: pd.DataFrame, length: int = 14) -> pd.DataFrame:
    """Append Wilder ADX, +DI, and -DI columns."""
    out = df.copy()
    high = out["high"]
    low = out["low"]
    close = out["close"]

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        [0.0] * len(out),
        index=out.index,
    )
    minus_dm = plus_dm.copy()
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move

    prev_close = close.shift(1)
    tr = pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    tr.iloc[0] = high.iloc[0] - low.iloc[0]

    atr = _wilder_rma(tr, length)
    plus_di = 100.0 * _wilder_rma(plus_dm, length) / atr
    minus_di = 100.0 * _wilder_rma(minus_dm, length) / atr
    denominator = plus_di + minus_di
    dx = 100.0 * (plus_di - minus_di).abs() / denominator.replace(0, math.nan)
    adx = _wilder_rma(dx, length)

    out[f"plus_di_{length}"] = plus_di
    out[f"minus_di_{length}"] = minus_di
    out[f"adx_{length}"] = adx
    return out


def _wilder_rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's RMA with SMA seed."""
    values = [float("nan")] * len(series)
    if len(series) < length:
        return pd.Series(values, index=series.index)
    seed = float(series.iloc[:length].mean())
    values[length - 1] = seed
    prev = seed
    for i in range(length, len(series)):
        current = float(series.iloc[i])
        if math.isnan(current):
            values[i] = float("nan")
            continue
        prev = (prev * (length - 1) + current) / length
        values[i] = prev
    return pd.Series(values, index=series.index)


def _first_technical_rejection(
    metric: dict[str, float | int],
    config: ScanConfig,
) -> str | None:
    close = float(metric["close"])
    if close < config.min_price:
        return "price"
    if float(metric["avg_volume_20"]) < config.min_avg_volume_20:
        return "share_volume"
    if float(metric["avg_dollar_volume_50"]) < config.min_avg_dollar_volume_50:
        return "dollar_volume"
    if not (
        close > float(metric["sma50"])
        and close > float(metric["sma150"])
        and close > float(metric["sma200"])
    ):
        return "price_above_smas"
    if not (
        float(metric["sma50"]) > float(metric["sma150"])
        and float(metric["sma150"]) > float(metric["sma200"])
    ):
        return "sma_alignment"
    if close < config.min_above_52w_low * float(metric["low_52w"]):
        return "above_52w_low"
    if close < config.min_pct_of_52w_high * float(metric["high_52w"]):
        return "near_52w_high"
    if float(metric["adx14"]) < config.min_adx:
        return "adx"
    if float(metric["plus_di14"]) <= float(metric["minus_di14"]):
        return "di_direction"
    atr_pct = float(metric["atr_pct"])
    if atr_pct < config.min_atr_pct:
        return "atr_too_low"
    if atr_pct > config.max_atr_pct:
        return "atr_too_high"
    return None


def _score_candidate(
    metric: dict[str, float | int],
    relative_strength_pct: float,
    config: ScanConfig,
) -> float:
    """Composite rank score for already-qualified candidates."""
    adx = float(metric["adx14"])
    adx_score = min(adx / 50.0, 1.0) * 100.0
    dollar_volume = float(metric["avg_dollar_volume_50"])
    liquidity_score = min(math.log10(max(dollar_volume, 1.0)) / 10.0, 1.0) * 100.0
    
    # 1. Consolidation Score: Penalize short-term exhaustion (Price vs SMA50)
    # Normal distance is 0% to 10%. At >25% above SMA50, it is highly extended.
    dist_50 = max(0.0, float(metric["close"]) / float(metric["sma50"]) - 1.0)
    consolidation_score = max(0.0, 1.0 - (dist_50 / 0.25)) * 100.0

    # 2. Freshness Score: Reward tightly coiled moving averages (SMA20 vs SMA50)
    # Tighter gap means the trend is either fresh or resting, ready for a new leg.
    coil_gap = abs(float(metric["sma20"]) / float(metric["sma50"]) - 1.0)
    freshness_score = max(0.0, 1.0 - (coil_gap / 0.10)) * 100.0

    smoothness_score = max(0.0, 100.0 - int(metric["crossover_count_1y"]) * 10.0)
    adx_slope_bonus = 5.0 if float(metric["adx14"]) > float(metric["adx14_5d_ago"]) else 0.0
    preferred_adx_bonus = 5.0 if adx >= config.preferred_adx else 0.0

    return (
        relative_strength_pct * 0.35
        + adx_score * 0.20
        + liquidity_score * 0.10
        + consolidation_score * 0.10
        + freshness_score * 0.10
        + smoothness_score * 0.15
        + adx_slope_bonus
        + preferred_adx_bonus
    )


def _apply_sector_cap(
    candidates: list[Candidate],
    max_per_sector: int,
    top: int,
) -> list[Candidate]:
    """Apply best-effort sector cap when sector metadata exists."""
    counts: Counter[str] = Counter()
    selected: list[Candidate] = []
    overflow: list[Candidate] = []
    for candidate in candidates:
        sector = candidate.sector or "UNKNOWN"
        if sector == "UNKNOWN" or counts[sector] < max_per_sector:
            selected.append(candidate)
            counts[sector] += 1
        else:
            candidate.notes.append(f"sector cap overflow: {sector}")
            overflow.append(candidate)
        if len(selected) >= top:
            break
    if len(selected) < top:
        selected.extend(overflow[: top - len(selected)])
    return selected[:top]


def _reject(
    symbol: str,
    reason: str,
    rejections: Counter[str],
    examples: dict[str, list[str]],
) -> None:
    rejections[reason] += 1
    if len(examples[reason]) < 10:
        examples[reason].append(symbol)


def _format_explanation(
    symbol: str,
    reason: str,
    metric: dict[str, float | int],
    relative_strength_pct: float | None,
    config: ScanConfig,
    market_cap: float | None = None,
) -> str:
    """Human-readable symbol-level pass/fail explanation."""
    status = "Passed all enabled filters" if reason == "PASS" else f"Rejected: {reason}"
    rs = "N/A" if relative_strength_pct is None else f"{relative_strength_pct:.1f}"
    market_cap_text = "N/A" if market_cap is None else _fmt_dollars(market_cap)
    return (
        f"{status}. "
        f"Close={float(metric['close']):.2f}; "
        f"SMA50/150/200={float(metric['sma50']):.2f}/"
        f"{float(metric['sma150']):.2f}/{float(metric['sma200']):.2f}; "
        f"SMA200_20d_ago={float(metric['sma200_20d_ago']):.2f}; "
        f"ADX14={float(metric['adx14']):.1f}; "
        f"+DI/-DI={float(metric['plus_di14']):.1f}/{float(metric['minus_di14']):.1f}; "
        f"ATR%={float(metric['atr_pct']) * 100:.1f}%; "
        f"RS%={rs}; "
        f"52w low/high={float(metric['low_52w']):.2f}/"
        f"{float(metric['high_52w']):.2f}; "
        f"MarketCap={market_cap_text}; "
        f"Vol20={float(metric['avg_volume_20']):,.0f}; "
        f"$Vol50={_fmt_dollars(float(metric['avg_dollar_volume_50']))}. "
        f"Thresholds: MarketCap>={_fmt_dollars(config.min_market_cap)}, "
        f"price>={config.min_price:.2f}, Vol20>={config.min_avg_volume_20:,.0f}, "
        f"$Vol50>={_fmt_dollars(config.min_avg_dollar_volume_50)}, "
        f"ADX>={config.min_adx:.1f}, RS%>={config.min_relative_strength_pct:.1f}."
    )


def render_report(
    candidates: list[Candidate],
    rejections: Counter[str],
    examples: dict[str, list[str]],
    explanations: dict[str, str],
    *,
    feed: str,
    assets_seen: int,
    bars_seen: int,
    include_fundamentals: bool,
    start: datetime,
    end: datetime,
) -> str:
    """Render a markdown report."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# SMA Watchlist Scan - {generated}",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Alpaca feed: `{feed}`",
        f"- Data window: {start.date()} to {end.date()}",
        f"- Data end timestamp: {end.isoformat(timespec='seconds')}",
        f"- Tradable assets considered: {assets_seen}",
        f"- Assets with bars: {bars_seen}",
        f"- Fundamentals enforced: {include_fundamentals}",
        f"- Candidates selected: {len(candidates)}",
        "",
        "## Rule Rationale",
        "",
        "- Liquidity filters reduce slippage and avoid thin, noisy names.",
        "- Price above SMA50/SMA150/SMA200 confirms the stock is already in an uptrend.",
        "- SMA50 > SMA150 > SMA200 requires trend alignment across short, medium, and long horizons.",
        "- The long-term direction filter is owned by the BEAR regime gate (SPY < 200 SMA); a per-symbol \"SMA200 rising 20 days\" rule is redundant on top of the alignment + close-above-SMA200 stack and was retired in v2.",
        "- 52-week strength keeps the watchlist near leadership instead of damaged recovery names.",
        "- Relative strength requires the stock to be a market leader before SMA is allowed to watch it.",
        "- Consolidation scoring penalizes parabolic exhaustion by checking price vs the 50-day moving average.",
        "- Freshness scoring rewards stocks whose 20-day and 50-day moving averages are tightly coiled.",
        "- ADX and +DI/-DI reduce sideways whipsaw risk and require bullish directional pressure.",
        "- ATR sanity rejects names that are too quiet to move or too chaotic for stable trend following.",
        "- Fundamental sanity keeps SMA out of deteriorating or solvency-stressed companies.",
        "- Market-cap minimum keeps SMA away from very small companies whose trends can be fragile.",
        "",
        "## Top Candidates",
        "",
    ]
    regular = [c for c in candidates if not any("PROTECTED" in n for n in c.notes)]
    protected = [c for c in candidates if any("PROTECTED" in n for n in c.notes)]

    if not regular and not protected:
        lines.append("No candidates passed all enabled filters.")
    else:
        lines.extend(
            [
                "| Rank | Symbol | Score | Close | RS % | ADX | +DI/-DI | ATR % | $Vol50 | Mom 12-1 | Crosses |",
                "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for rank, candidate in enumerate(regular, start=1):
            lines.append(
                "| "
                f"{rank} | {candidate.symbol} | {candidate.score:.1f} | "
                f"{candidate.close:.2f} | {candidate.relative_strength_pct:.1f} | "
                f"{candidate.adx14:.1f} | {candidate.plus_di14:.1f}/{candidate.minus_di14:.1f} | "
                f"{candidate.atr_pct * 100:.1f}% | {_fmt_dollars(candidate.avg_dollar_volume_50)} | "
                f"{candidate.momentum_12m_skip_1m * 100:.1f}% | {candidate.crossover_count_1y} |"
            )

    if protected:
        lines.extend(
            [
                "",
                "## Protected (Open Positions)",
                "",
                "These symbols have open SMA positions and are force-included to "
                "prevent the engine from orphaning held positions on watchlist refresh. "
                "Pass `--ignore-open-positions` to disable.",
                "",
                "| Symbol | RS % | Close | Note |",
                "|---|---:|---:|---|",
            ]
        )
        for c in protected:
            rs = "N/A" if math.isnan(c.relative_strength_pct) else f"{c.relative_strength_pct:.1f}"
            close = "N/A" if math.isnan(c.close) else f"{c.close:.2f}"
            note = "; ".join(c.notes)
            lines.append(f"| {c.symbol} | {rs} | {close} | {note} |")

    lines.extend(["", "## Rejections", ""])
    if not rejections:
        lines.append("No rejected symbols.")
    else:
        lines.extend(["| Reason | Count | Meaning | Examples |", "|---|---:|---|---|"])
        for reason, count in rejections.most_common():
            sample = ", ".join(examples.get(reason, []))
            label = REJECTION_LABELS.get(reason, "")
            lines.append(f"| `{reason}` | {count} | {label} | {sample} |")

    if explanations:
        lines.extend(["", "## Requested Symbol Explanations", ""])
        lines.extend(["| Symbol | Explanation |", "|---|---|"])
        for symbol in sorted(explanations):
            lines.append(f"| {symbol} | {explanations[symbol]} |")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This script is report-only and does not change the active bot watchlist.",
            "- Sector caps are best-effort because Alpaca asset metadata does not include sector.",
            "- If fundamentals are disabled, the report is a technical/liquidity scan only.",
            "- With `feed=sip`, Basic Alpaca accounts require the request end time to be "
            "outside the latest 15-minute restricted window.",
            "- With `feed=iex`, daily volume is multiplied by the synthetic-SIP factor "
            "(`utils.market.apply_synthetic_sip_volume`) so the scanner sees the same "
            "consolidated-tape-equivalent volume the running bot does.",
            "- Symbols with open SMA positions are force-included as protected entries "
            "and shown in a separate table. Use `--ignore-open-positions` to disable.",
        ]
    )
    return "\n".join(lines)


def _fmt_dollars(value: float) -> str:
    if value >= 1e9:
        return f"${value / 1e9:.1f}B"
    if value >= 1e6:
        return f"${value / 1e6:.0f}M"
    return f"${value:,.0f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan Alpaca assets for SMA crossover watchlist candidates.",
    )
    parser.add_argument("--top", type=int, default=30, help="Number of candidates to show.")
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=420,
        help="Calendar days of daily bars to fetch.",
    )
    parser.add_argument(
        "--max-assets",
        type=int,
        default=None,
        help="Limit number of Alpaca assets considered, useful for smoke tests.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=200,
        help="Symbols per Alpaca historical-data request.",
    )
    parser.add_argument(
        "--feed",
        choices=["iex", "sip"],
        default="sip",
        help="Alpaca market-data feed.",
    )
    parser.add_argument(
        "--end-delay-minutes",
        type=int,
        default=60,
        help=(
            "Delay scan end time by this many minutes. Basic Alpaca accounts "
            "can query SIP historical data only outside the latest 15-minute window."
        ),
    )
    parser.add_argument(
        "--include-fundamentals",
        action="store_true",
        help="Enforce SMA fundamental sanity via Yahoo Finance after technical filters.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional markdown report path.",
    )
    parser.add_argument(
        "--explain-symbols",
        nargs="+",
        default=[],
        metavar="SYM",
        help="Include pass/fail details for specific symbols in the markdown report.",
    )
    parser.add_argument(
        "--ignore-open-positions",
        action="store_true",
        help=(
            "Do not force-include symbols with open SMA positions. "
            "By default the scanner protects them so the engine does not "
            "orphan a held position on watchlist refresh."
        ),
    )
    parser.add_argument(
        "--trade-db",
        type=str,
        default=settings.TRADE_LOG_DB,
        help="Path to the trade-log SQLite DB used for open-position lookup.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    if args.end_delay_minutes < 0:
        raise ValueError("--end-delay-minutes must be >= 0")

    end = datetime.now(timezone.utc) - timedelta(minutes=args.end_delay_minutes)
    start = end - timedelta(days=args.lookback_days)

    logger.info(
        f"SMA scan started: rule={RULE_VERSION} feed={args.feed} "
        f"end_delay={args.end_delay_minutes}m"
    )
    assets = get_tradable_assets(args.max_assets)
    symbols = [asset.symbol for asset in assets]
    logger.info(f"tradable Alpaca assets after exclusions: {len(symbols)}")

    bars = fetch_daily_bars(
        symbols,
        start,
        end,
        chunk_size=args.chunk_size,
        feed=args.feed,
    )
    logger.info(f"assets with daily bars: {len(bars)}")

    explain_symbols = {sym.upper() for sym in args.explain_symbols}
    if args.ignore_open_positions:
        protected: set[str] = set()
    else:
        protected = get_open_sma_positions(args.trade_db)
        if protected:
            logger.info(f"protecting open SMA positions: {sorted(protected)}")

    candidates, rejections, examples, explanations = scan_candidates(
        assets,
        bars,
        config=ScanConfig(),
        include_fundamentals=args.include_fundamentals,
        top=args.top,
        explain_symbols=explain_symbols,
        protected_symbols=protected,
    )

    report = render_report(
        candidates,
        rejections,
        examples,
        explanations,
        feed=args.feed,
        assets_seen=len(assets),
        bars_seen=len(bars),
        include_fundamentals=args.include_fundamentals,
        start=start,
        end=end,
    )
    print(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        logger.info(f"report saved: {args.output}")


if __name__ == "__main__":
    main()
