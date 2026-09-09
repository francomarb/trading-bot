#!/usr/bin/env python3
"""
RSI watchlist scanner.

Builds a ranked 50-name RSI mean-reversion opportunity pool using the
forward-oriented rules in docs/rsi-watchlist-selection.md.

Data sources:
  - Alpaca Trading API: active/tradable US equity universe
  - Alpaca Market Data API: adjusted daily OHLCV bars
  - Optional Yahoo Finance fundamentals: market cap and solvency checks

Usage:
    python scripts/rsi_watchlist_scan.py
    python scripts/rsi_watchlist_scan.py --top 50 --include-fundamentals
    python scripts/rsi_watchlist_scan.py --feed sip --end-delay-minutes 60
    python scripts/rsi_watchlist_scan.py --explain-symbols AAPL MSFT NVDA

The scanner is report-only. It does not modify config/settings.py or any live
strategy slot.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from indicators.technicals import add_atr, add_rsi, add_sma
from scripts.sma_watchlist_scan import (
    AssetInfo,
    configure_logging,
    fetch_daily_bars,
    get_tradable_assets,
    _fmt_dollars,
)
from scripts.watchlist_review import RSI_PROFILE, assess_fitness, fetch_fundamentals


RULE_VERSION = "rsi_watchlist_v3_durable_company_pool"
DEFAULT_POOL_SIZE = 50
EXCLUDED_SHARE_CLASSES: dict[str, str] = {"GOOGL": "GOOG"}
ALWAYS_PRESERVE_SYMBOLS: frozenset[str] = frozenset(
    EXCLUDED_SHARE_CLASSES.values()
)


@dataclass(frozen=True)
class ScanConfig:
    """Durable company-eligibility thresholds for the RSI watchlist."""

    min_bars: int = 260
    min_market_cap: float = 2_000_000_000.0
    min_price: float = 10.0
    min_avg_dollar_volume_50: float = 50_000_000.0
    # These parameters define reference-only historical characterization.
    # They never include, exclude, or rank a company.
    reversion_window_days: int = 10
    oversold_threshold: float = 30.0
    reversion_threshold: float = 50.0
    atr_stop_multiplier: float = 2.0


@dataclass
class Candidate:
    """One symbol that passed the RSI scanner."""

    symbol: str
    name: str
    exchange: str
    sector: str
    close: float
    avg_volume_20: float
    avg_dollar_volume_50: float
    market_cap: float | None
    sma50: float
    sma200: float
    rsi14: float
    atr_pct: float
    median_atr_pct_252: float
    bb_width_pct: float
    high_52w: float
    low_52w: float
    oversold_events: int
    reversion_hit_rate: float
    avg_reversion_return_10d: float
    stop_failures: int
    one_day_return: float
    five_day_return: float
    notes: list[str] = field(default_factory=list)


def _candidate_from_metric(
    symbol: str,
    metric: dict[str, float | int],
    asset: AssetInfo,
    *,
    market_cap: float | None,
    notes: list[str] | None = None,
) -> Candidate:
    """Build a report candidate from computed, non-decision metrics."""
    return Candidate(
        symbol=symbol,
        name=asset.name,
        exchange=asset.exchange,
        sector=asset.sector,
        close=float(metric["close"]),
        avg_volume_20=float(metric["avg_volume_20"]),
        avg_dollar_volume_50=float(metric["avg_dollar_volume_50"]),
        market_cap=market_cap,
        sma50=float(metric["sma50"]),
        sma200=float(metric["sma200"]),
        rsi14=float(metric["rsi14"]),
        atr_pct=float(metric["atr_pct"]),
        median_atr_pct_252=float(metric["median_atr_pct_252"]),
        bb_width_pct=float(metric["bb_width_pct"]),
        high_52w=float(metric["high_52w"]),
        low_52w=float(metric["low_52w"]),
        oversold_events=int(metric["oversold_events"]),
        reversion_hit_rate=float(metric["reversion_hit_rate"]),
        avg_reversion_return_10d=float(metric["avg_reversion_return_10d"]),
        stop_failures=int(metric["stop_failures"]),
        one_day_return=float(metric["one_day_return"]),
        five_day_return=float(metric["five_day_return"]),
        notes=list(notes or []),
    )


def get_open_rsi_positions(db_path: str) -> set[str]:
    """Return symbols currently owned by RSI according to the trade ledger."""
    if not Path(db_path).exists():
        return set()
    try:
        from reporting.logger import TradeLogger

        trade_logger = TradeLogger(path=db_path)
        try:
            owners = trade_logger.read_all_open_owners()
        finally:
            trade_logger.close()
        return {
            symbol.upper()
            for symbol, strategy in owners.items()
            if strategy == "rsi_reversion"
        }
    except Exception as exc:
        from loguru import logger

        logger.warning(f"could not read open RSI positions from {db_path}: {exc}")
        return set()


REJECTION_LABELS: dict[str, str] = {
    "insufficient_or_bad_bars": (
        "Not enough clean daily bars for durable review and reference metrics."
    ),
    "price": "Latest close is below the minimum price threshold.",
    "dollar_volume": "50-day average dollar volume is below the liquidity threshold.",
    "market_cap": "Market capitalization is below the RSI minimum size threshold.",
    "solvency": (
        "Solvency was not affirmatively established or the fundamentals request failed."
    ),
    "nonpreferred_share_class": (
        "Excluded by share-class policy; use GOOG for Alphabet exposure, never GOOGL."
    ),
}


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
    """Apply RSI pool rules and return ranked candidates plus protected holdings."""
    asset_by_symbol = {a.symbol: a for a in assets}
    rejections: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    explanations: dict[str, str] = {}
    explain_symbols = explain_symbols or set()
    protected_symbols = {symbol.upper() for symbol in (protected_symbols or set())}
    ranked: list[Candidate] = []
    prequalified: list[tuple[str, dict[str, float | int]]] = []

    for symbol, df in bars_by_symbol.items():
        if symbol in EXCLUDED_SHARE_CLASSES:
            _reject(symbol, "nonpreferred_share_class", rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = (
                    "Rejected: nonpreferred_share_class; use "
                    f"{EXCLUDED_SHARE_CLASSES[symbol]}."
                )
            continue
        metric = _compute_metrics(df, config)
        if metric is None:
            _reject(symbol, "insufficient_or_bad_bars", rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = "Rejected: insufficient_or_bad_bars"
            continue

        reason = _first_rejection(metric, config)
        if reason is not None:
            _reject(symbol, reason, rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = _format_explanation(
                    reason, metric, config, market_cap=None
                )
            continue
        prequalified.append((symbol, metric))

    # Dollar liquidity is the only automatic ordering input. It is relatively
    # durable, directly relevant to execution, and does not encode realized RSI
    # outcomes. Fundamentals are reviewed in that order to avoid thousands of
    # slow, rate-limited Yahoo requests after removing temporary technical gates.
    prequalified.sort(
        key=lambda item: float(item[1]["avg_dollar_volume_50"]),
        reverse=True,
    )
    review_goal = max(top * 2, top + 25)
    for symbol, metric in prequalified:
        if (
            include_fundamentals
            and len(ranked) >= review_goal
            and symbol not in explain_symbols
            and symbol not in ALWAYS_PRESERVE_SYMBOLS
        ):
            continue

        market_cap: float | None = None
        if include_fundamentals:
            fundamentals = fetch_fundamentals(symbol)
            market_cap = fundamentals.market_cap
            if market_cap is None or market_cap < config.min_market_cap:
                _reject(symbol, "market_cap", rejections, examples)
                if symbol in explain_symbols:
                    explanations[symbol] = _format_explanation(
                        "market_cap", metric, config, market_cap=market_cap
                    )
                continue
            fitness = assess_fitness(fundamentals, RSI_PROFILE)
            if fitness.solvency_ok is not True or fitness.error:
                _reject(symbol, "solvency", rejections, examples)
                if symbol in explain_symbols:
                    explanations[symbol] = _format_explanation(
                        "solvency", metric, config, market_cap=market_cap
                    )
                continue

        asset = asset_by_symbol.get(symbol, AssetInfo(symbol, symbol, "UNKNOWN"))
        ranked.append(
            _candidate_from_metric(
                symbol,
                metric,
                asset,
                market_cap=market_cap,
            )
        )
        if symbol in explain_symbols:
            explanations[symbol] = _format_explanation(
                "PASS", metric, config, market_cap=market_cap
            )

    # Preserve liquidity ordering, with explicit share-class preferences as
    # the only deterministic override.
    ranked.sort(key=lambda c: c.avg_dollar_volume_50, reverse=True)
    ranked = _select_pool(ranked, top)

    # A static watchlist is also the engine's signal-exit evaluation universe.
    # Retain any currently held RSI name even when it fails this refresh or
    # falls below the top-N cutoff; otherwise only its broker stop would remain
    # active and the strategy would stop evaluating its normal quick exit.
    selected_symbols = {candidate.symbol for candidate in ranked}
    for symbol in sorted(protected_symbols - selected_symbols):
        metric = _compute_metrics(bars_by_symbol.get(symbol, pd.DataFrame()), config)
        asset = asset_by_symbol.get(symbol, AssetInfo(symbol, symbol, "UNKNOWN"))
        if metric is None:
            nan = float("nan")
            ranked.append(
                Candidate(
                    symbol=symbol,
                    name=asset.name,
                    exchange=asset.exchange,
                    sector=asset.sector,
                    close=nan,
                    avg_volume_20=nan,
                    avg_dollar_volume_50=nan,
                    market_cap=None,
                    sma50=nan,
                    sma200=nan,
                    rsi14=nan,
                    atr_pct=nan,
                    median_atr_pct_252=nan,
                    bb_width_pct=nan,
                    high_52w=nan,
                    low_52w=nan,
                    oversold_events=0,
                    reversion_hit_rate=nan,
                    avg_reversion_return_10d=nan,
                    stop_failures=0,
                    one_day_return=nan,
                    five_day_return=nan,
                    notes=["PROTECTED: open RSI position; metrics unavailable"],
                )
            )
            continue
        ranked.append(
            _candidate_from_metric(
                symbol,
                metric,
                asset,
                market_cap=None,
                notes=["PROTECTED: open RSI position; outside refreshed top pool"],
            )
        )
    for symbol in explain_symbols - set(explanations):
        explanations[symbol] = "No bars returned from Alpaca."
    return ranked, rejections, examples, explanations


def _compute_metrics(
    df: pd.DataFrame,
    config: ScanConfig,
) -> dict[str, float | int] | None:
    """Compute RSI scanner metrics for a single symbol."""
    required = {"open", "high", "low", "close", "volume"}
    if df.empty or not required.issubset(df.columns):
        return None
    if len(df) < config.min_bars:
        return None
    if df[list(required)].isna().any().any():
        return None

    work = add_sma(df, 20)
    work = add_sma(work, 50)
    work = add_sma(work, 200)
    work = add_atr(work, 14)
    work = add_rsi(work, 14)
    work = _add_bollinger_width(work, 20, 2.0)

    last = work.iloc[-1]
    needed = ["sma_50", "sma_200", "atr_14", "rsi_14", "bb_width_pct_20_2"]
    if last[needed].isna().any():
        return None

    close = float(last["close"])
    if close <= 0:
        return None

    reversion = _oversold_reversion_stats(work, config)
    avg_volume_20 = float(work["volume"].tail(20).mean())
    avg_dollar_volume_50 = float((work["close"] * work["volume"]).tail(50).mean())
    one_day_return = float(work["close"].pct_change().iloc[-1])
    five_day_return = float(work["close"].pct_change(5).iloc[-1])

    return {
        "close": close,
        "sma50": float(last["sma_50"]),
        "sma200": float(last["sma_200"]),
        "avg_volume_20": avg_volume_20,
        "avg_dollar_volume_50": avg_dollar_volume_50,
        "high_52w": float(work["high"].tail(252).max()),
        "low_52w": float(work["low"].tail(252).min()),
        "rsi14": float(last["rsi_14"]),
        "atr_pct": float(last["atr_14"] / close),
        "median_atr_pct_252": float(
            (work["atr_14"] / work["close"]).tail(252).median()
        ),
        "bb_width_pct": float(last["bb_width_pct_20_2"]),
        "oversold_events": int(reversion["events"]),
        "reversion_hit_rate": float(reversion["hit_rate"]),
        "avg_reversion_return_10d": float(reversion["avg_return"]),
        "stop_failures": int(reversion["stop_failures"]),
        "one_day_return": one_day_return,
        "five_day_return": five_day_return,
    }


def _add_bollinger_width(
    df: pd.DataFrame,
    length: int = 20,
    num_std: float = 2.0,
) -> pd.DataFrame:
    """Append Bollinger Band width as a fraction of close."""
    out = df.copy()
    mid = out["close"].rolling(length, min_periods=length).mean()
    std = out["close"].rolling(length, min_periods=length).std(ddof=0)
    upper = mid + num_std * std
    lower = mid - num_std * std
    out[f"bb_width_pct_{length}_{int(num_std)}"] = (upper - lower) / out["close"]
    return out


def _oversold_reversion_stats(
    df: pd.DataFrame,
    config: ScanConfig,
) -> dict[str, float | int]:
    """Evaluate historical RSI oversold events over the trailing year."""
    recent = df.tail(252).copy()
    rsi = recent["rsi_14"]
    closes = recent["close"]
    atr = recent["atr_14"]
    event_mask = (rsi < config.oversold_threshold) & (
        rsi.shift(1) >= config.oversold_threshold
    )
    event_indices = [i for i, value in enumerate(event_mask.fillna(False)) if value]

    events = 0
    hits = 0
    returns: list[float] = []
    stop_failures = 0
    for idx in event_indices:
        if idx + 1 >= len(recent):
            continue
        end_idx = min(idx + config.reversion_window_days, len(recent) - 1)
        future = recent.iloc[idx + 1 : end_idx + 1]
        if future.empty:
            continue
        entry_close = float(closes.iloc[idx])
        entry_atr = float(atr.iloc[idx])
        if entry_close <= 0 or math.isnan(entry_atr):
            continue

        events += 1
        stop_level = entry_close - config.atr_stop_multiplier * entry_atr
        
        hit = False
        stop_breached = False
        
        # Evaluate path-dependency day-by-day
        for _, bar in future.iterrows():
            if float(bar["low"]) <= stop_level:
                stop_breached = True
                stop_failures += 1
                break  # Stopped out before or on the same day it could revert
                
            if float(bar["rsi_14"]) >= config.reversion_threshold:
                hit = True
                break  # Successfully reverted!

        if hit and not stop_breached:
            hits += 1
            
        returns.append(float(future["close"].iloc[-1] / entry_close - 1.0))

    hit_rate = hits / events if events else 0.0
    avg_return = sum(returns) / len(returns) if returns else 0.0
    return {
        "events": events,
        "hit_rate": hit_rate,
        "avg_return": avg_return,
        "stop_failures": stop_failures,
    }


def _first_rejection(
    metric: dict[str, float | int],
    config: ScanConfig,
) -> str | None:
    close = float(metric["close"])
    if close < config.min_price:
        return "price"
    if float(metric["avg_dollar_volume_50"]) < config.min_avg_dollar_volume_50:
        return "dollar_volume"
    return None


def _select_pool(candidates: list[Candidate], top: int) -> list[Candidate]:
    """Select the liquidity leaders while enforcing share-class preferences."""
    if top <= 0:
        return []
    selected = candidates[:top]
    selected_symbols = {candidate.symbol for candidate in selected}
    for symbol in ALWAYS_PRESERVE_SYMBOLS:
        if symbol in selected_symbols:
            continue
        preferred = next(
            (candidate for candidate in candidates if candidate.symbol == symbol),
            None,
        )
        if preferred is None:
            continue
        preferred.notes.append("PREFERRED SHARE CLASS: retain GOOG, never GOOGL")
        displaced = selected[-1]
        selected[-1] = preferred
        selected_symbols.discard(displaced.symbol)
        selected_symbols.add(symbol)
    return selected


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
    reason: str,
    metric: dict[str, float | int],
    config: ScanConfig,
    *,
    market_cap: float | None = None,
) -> str:
    """Human-readable symbol-level pass/fail explanation."""
    status = "Passed all enabled filters" if reason == "PASS" else f"Rejected: {reason}"
    market_cap_text = "N/A" if market_cap is None else _fmt_dollars(market_cap)
    return (
        f"{status}. "
        f"Close={float(metric['close']):.2f}; "
        f"SMA50/200={float(metric['sma50']):.2f}/{float(metric['sma200']):.2f}; "
        f"RSI14={float(metric['rsi14']):.1f}; "
        f"ATR%={float(metric['atr_pct']) * 100:.1f}%; "
        f"BBWidth%={float(metric['bb_width_pct']) * 100:.1f}%; "
        f"52w low/high={float(metric['low_52w']):.2f}/{float(metric['high_52w']):.2f}; "
        f"Events={int(metric['oversold_events'])}; "
        f"HitRate={float(metric['reversion_hit_rate']) * 100:.1f}%; "
        f"Avg10d={float(metric['avg_reversion_return_10d']) * 100:.1f}%; "
        f"StopFailures={int(metric['stop_failures'])}; "
        f"1d={float(metric['one_day_return']) * 100:.1f}%; "
        f"5d={float(metric['five_day_return']) * 100:.1f}%; "
        f"MarketCap={market_cap_text}; "
        f"Vol20={float(metric['avg_volume_20']):,.0f}; "
        f"$Vol50={_fmt_dollars(float(metric['avg_dollar_volume_50']))}. "
        f"Thresholds: MarketCap>={_fmt_dollars(config.min_market_cap)}, "
        f"price>={config.min_price:.2f}, "
        f"$Vol50>={_fmt_dollars(config.min_avg_dollar_volume_50)}, "
        "solvency=affirmatively established. Other metrics are reference-only."
    )


def _is_protected(candidate: Candidate) -> bool:
    """Return whether a candidate is present only to manage an open position."""
    return any(note.startswith("PROTECTED:") for note in candidate.notes)


def _nearest_rank(values: list[float], percentile: float) -> float:
    """Return a deterministic nearest-rank percentile for a small report set."""
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


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
    target_size: int,
    start: datetime,
    end: datetime,
) -> str:
    """Render a markdown report."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# RSI Watchlist Scan - {generated}",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Alpaca feed: `{feed}`",
        f"- Data window: {start.date()} to {end.date()}",
        f"- Data end timestamp: {end.isoformat(timespec='seconds')}",
        f"- Tradable assets considered: {assets_seen}",
        f"- Assets with bars: {bars_seen}",
        f"- Fundamentals enforced: {include_fundamentals}",
        f"- Target opportunity-pool size: {target_size}",
        f"- Ranked candidates selected: {sum(not _is_protected(c) for c in candidates)}",
        f"- Protected open-position additions: {sum(_is_protected(c) for c in candidates)}",
        "",
        "## Rule Rationale",
        "",
        "- Price, dollar liquidity, market cap, and affirmative solvency are the only company eligibility gates.",
        "- Dollar liquidity orders eligible companies because it is durable and directly relevant to execution quality.",
        "- Sector concentration is accepted; no sector cap or diversification reranking is applied.",
        "- Alphabet share-class policy is explicit: preserve eligible GOOG and never select GOOGL.",
        "- SMA200, 52-week location, volatility, recent returns, and historical RSI outcomes are reference-only columns.",
        "- The active RSI3 strategy and its runtime gates decide whether an eligible company can actually enter.",
        "",
        "## Top Candidates",
        "",
    ]
    regular = [candidate for candidate in candidates if not _is_protected(candidate)]
    protected = [candidate for candidate in candidates if _is_protected(candidate)]
    if not regular:
        lines.append("No candidates passed all enabled filters.")
    else:
        lines.extend(
            [
                "| Rank | Symbol | Mkt Cap | Close | RSI14 | Hit % | Events | Avg10d | ATR % | Median ATR % | BB Width | $Vol50 | Stops |",
                "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for rank, candidate in enumerate(regular, start=1):
            market_cap = (
                _fmt_dollars(candidate.market_cap)
                if candidate.market_cap is not None
                else "N/A"
            )
            lines.append(
                "| "
                f"{rank} | {candidate.symbol} | {market_cap} | "
                f"{candidate.close:.2f} | "
                f"{candidate.rsi14:.1f} | "
                f"{candidate.reversion_hit_rate * 100:.1f}% | "
                f"{candidate.oversold_events} | "
                f"{candidate.avg_reversion_return_10d * 100:.1f}% | "
                f"{candidate.atr_pct * 100:.1f}% | "
                f"{candidate.median_atr_pct_252 * 100:.1f}% | "
                f"{candidate.bb_width_pct * 100:.1f}% | "
                f"{_fmt_dollars(candidate.avg_dollar_volume_50)} | "
                f"{candidate.stop_failures} |"
            )

    if protected:
        lines.extend(
            [
                "",
                "## Protected Open RSI Positions",
                "",
                "These symbols are retained outside the 50-name opportunity pool until "
                "their RSI positions are flat, so normal strategy exits remain active.",
                "",
                "| Symbol | Note |",
                "|---|---|",
            ]
        )
        for candidate in protected:
            lines.append(f"| {candidate.symbol} | {'; '.join(candidate.notes)} |")

    current_symbols = list(settings.RSI_WATCHLIST)
    current_set = set(current_symbols)
    overlap = [candidate.symbol for candidate in regular if candidate.symbol in current_set]
    additions_needed = max(target_size - len(current_symbols), 0)
    expansion_additions = [
        candidate.symbol for candidate in regular if candidate.symbol not in current_set
    ][:additions_needed]
    lines.extend(
        [
            "",
            "## Expansion-Only Review",
            "",
            f"- Current active pool: {len(current_symbols)} symbols",
            f"- Current symbols also in the refreshed top {target_size}: "
            f"{len(overlap)} ({', '.join(overlap) if overlap else 'none'})",
            f"- Additions needed to reach {target_size} without one-scan removals: "
            f"{additions_needed}",
            f"- Highest-ranked nonmembers for review: "
            f"{', '.join(expansion_additions) if expansion_additions else 'none'}",
            "- This is a stability-first review list, not an automatic promotion. "
            "Operator approval remains required.",
        ]
    )

    atr_candidates = [
        candidate
        for candidate in regular
        if math.isfinite(candidate.atr_pct) and candidate.atr_pct > 0
    ]
    protected_atr_candidates = [
        candidate
        for candidate in protected
        if math.isfinite(candidate.atr_pct) and candidate.atr_pct > 0
    ]
    atr_values = [candidate.atr_pct for candidate in atr_candidates]
    allocation = settings.STRATEGY_ALLOCATIONS["rsi_reversion"]
    baseline_position_cap = (
        settings.MAX_GROSS_EXPOSURE_PCT
        * float(allocation["target_pct"])
        * float(allocation["max_position_pct_of_sleeve"])
    )
    risk_target = float(allocation["risk_per_trade_pct"])
    atr_binding_threshold = risk_target / (2.0 * baseline_position_cap)
    clipped = [
        candidate.symbol
        for candidate in atr_candidates
        if candidate.atr_pct < atr_binding_threshold
    ]
    lines.extend(["", "## Risk-Target Coverage", ""])
    if atr_values:
        lines.extend(
            [
                f"- Ranked symbols measured: {len(atr_values)}",
                f"- ATR14/close: min={min(atr_values):.2%}, "
                f"p10={_nearest_rank(atr_values, 0.10):.2%}, "
                f"median={_nearest_rank(atr_values, 0.50):.2%}, "
                f"p90={_nearest_rank(atr_values, 0.90):.2%}",
                f"- Baseline per-position cap: {baseline_position_cap:.2%} of equity",
                f"- RSI risk target: {risk_target:.2%} of equity",
                f"- Risk sizing binds at ATR14/close >= {atr_binding_threshold:.2%}; "
                f"{len(clipped)} symbol(s) are conservatively cap-clipped: "
                f"{', '.join(clipped) if clipped else 'none'}",
                "- This is a coverage check, not a reason to change the risk target automatically.",
            ]
        )
        protected_clipped = [
            candidate.symbol
            for candidate in protected_atr_candidates
            if candidate.atr_pct < atr_binding_threshold
        ]
        if protected_atr_candidates:
            lines.insert(
                len(lines) - 1,
                "- Protected open-position coverage is reported separately: "
                f"{len(protected_atr_candidates)} measured; "
                f"{len(protected_clipped)} conservatively cap-clipped"
                + (f" ({', '.join(protected_clipped)})" if protected_clipped else "")
                + ".",
            )
    else:
        lines.append("ATR coverage unavailable because no candidate had valid metrics.")

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
            "- Earnings-calendar blocking is not implemented yet; treat as `not_checked`.",
            "- Sector concentration is accepted by design; the scanner does not cap or rerank sectors.",
            "- GOOGL is always excluded; eligible GOOG is preserved in the selected pool.",
            "- Fundamentals are checked in dollar-liquidity order until a 2x candidate "
            "review buffer qualifies; this avoids rate-limited full-universe lookups.",
            "- Fundamental rejection counts cover that reviewed buffer, not every "
            "operationally eligible symbol.",
            "- If fundamentals are disabled, market cap and solvency are not enforced.",
            "- With `feed=sip`, Basic Alpaca accounts require the request end time to be "
            "outside the latest 15-minute restricted window.",
            "- With `feed=iex`, volume is IEX venue volume, not consolidated market volume.",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scan Alpaca assets for RSI mean-reversion watchlist candidates.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_POOL_SIZE,
        help="Number of ranked opportunity-pool candidates to show.",
    )
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
        help="Enforce market-cap and solvency checks via Yahoo Finance.",
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
        help="Do not append currently held RSI symbols as protected members.",
    )
    parser.add_argument(
        "--trade-db",
        type=str,
        default=settings.TRADE_LOG_DB,
        help="Trade-log database used to find currently held RSI symbols.",
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

    from loguru import logger

    logger.info(
        f"RSI scan started: rule={RULE_VERSION} feed={args.feed} "
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
    protected_symbols = (
        set()
        if args.ignore_open_positions
        else get_open_rsi_positions(args.trade_db)
    )
    candidates, rejections, examples, explanations = scan_candidates(
        assets,
        bars,
        config=ScanConfig(),
        include_fundamentals=args.include_fundamentals,
        top=args.top,
        explain_symbols=explain_symbols,
        protected_symbols=protected_symbols,
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
        target_size=args.top,
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
