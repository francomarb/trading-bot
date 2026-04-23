#!/usr/bin/env python3
"""
RSI watchlist scanner.

Builds a ranked list of RSI mean-reversion candidates using the documented
`rsi_watchlist_v1` rules in docs/rsi-watchlist-selection.md.

Data sources:
  - Alpaca Trading API: active/tradable US equity universe
  - Alpaca Market Data API: adjusted daily OHLCV bars
  - Optional Yahoo Finance fundamentals: market cap and solvency checks

Usage:
    python scripts/rsi_watchlist_scan.py
    python scripts/rsi_watchlist_scan.py --top 30 --include-fundamentals
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

from indicators.technicals import add_atr, add_rsi, add_sma
from scripts.sma_watchlist_scan import (
    AssetInfo,
    configure_logging,
    fetch_daily_bars,
    get_tradable_assets,
    _fmt_dollars,
)
from scripts.watchlist_review import RSI_PROFILE, assess_fitness, fetch_fundamentals


RULE_VERSION = "rsi_watchlist_v1"


@dataclass(frozen=True)
class ScanConfig:
    """Thresholds for the RSI watchlist scanner."""

    min_bars: int = 260
    min_market_cap: float = 10_000_000_000.0
    min_price: float = 10.0
    min_avg_volume_20: float = 1_000_000.0
    min_avg_dollar_volume_50: float = 100_000_000.0
    min_pct_of_52w_high: float = 0.80
    min_above_52w_low: float = 1.20
    min_atr_pct: float = 0.015
    max_atr_pct: float = 0.07
    min_bb_width_pct: float = 0.04
    min_oversold_events: int = 3
    min_reversion_hit_rate: float = 0.50
    max_stop_failures: int = 2
    reversion_window_days: int = 10
    oversold_threshold: float = 30.0
    reversion_threshold: float = 50.0
    atr_stop_multiplier: float = 2.0
    max_one_day_drop: float = -0.12
    max_five_day_return: float = -0.20
    max_per_sector: int = 3


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
    bb_width_pct: float
    high_52w: float
    low_52w: float
    oversold_events: int
    reversion_hit_rate: float
    avg_reversion_return_10d: float
    stop_failures: int
    one_day_return: float
    five_day_return: float
    score: float
    notes: list[str] = field(default_factory=list)


REJECTION_LABELS: dict[str, str] = {
    "insufficient_or_bad_bars": "Not enough clean daily bars for 200-day structure and 1-year RSI event checks.",
    "price": "Latest close is below the minimum price threshold.",
    "share_volume": "20-day average share volume is below the liquidity threshold.",
    "dollar_volume": "50-day average dollar volume is below the liquidity threshold.",
    "market_cap": "Market capitalization is below the RSI minimum size threshold.",
    "solvency": "RSI solvency check failed or fundamental data was unavailable.",
    "below_sma200": "Price is below SMA200, so the stock may be structurally broken.",
    "too_far_from_high": "Price is too far below the 52-week high.",
    "too_close_to_low": "Price is not far enough above the 52-week low.",
    "atr_too_low": "ATR14 / close is too low; the name may be too quiet for RSI reversion.",
    "atr_too_high": "ATR14 / close is too high; the name may be too chaotic for RSI reversion.",
    "bb_width": "Bollinger Band width is too narrow; not enough volatility for meaningful reversion.",
    "oversold_events": "Too few RSI oversold events in the last year.",
    "reversion_hit_rate": "Historical oversold events did not revert often enough.",
    "stop_failures": "Too many historical oversold events breached an ATR-style stop.",
    "one_day_drop": "One-day drop is too large; likely news/crash risk.",
    "five_day_drop": "Five-day return is too negative; likely crash/breakdown risk.",
}


def scan_candidates(
    assets: list[AssetInfo],
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    config: ScanConfig,
    include_fundamentals: bool,
    top: int,
    explain_symbols: set[str] | None = None,
) -> tuple[list[Candidate], Counter[str], dict[str, list[str]], dict[str, str]]:
    """Apply RSI v1 rules and return ranked candidates plus rejection details."""
    asset_by_symbol = {a.symbol: a for a in assets}
    rejections: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    explanations: dict[str, str] = {}
    explain_symbols = explain_symbols or set()
    ranked: list[Candidate] = []

    for symbol, df in bars_by_symbol.items():
        metric = _compute_metrics(df, config)
        if metric is None:
            _reject(symbol, "insufficient_or_bad_bars", rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = "Rejected: insufficient_or_bad_bars"
            continue

        market_cap: float | None = None
        reason = _first_rejection(metric, config)
        if reason is not None:
            _reject(symbol, reason, rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = _format_explanation(
                    reason, metric, config, market_cap=market_cap
                )
            continue

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
            if fitness.solvency_ok is False or fitness.error:
                _reject(symbol, "solvency", rejections, examples)
                if symbol in explain_symbols:
                    explanations[symbol] = _format_explanation(
                        "solvency", metric, config, market_cap=market_cap
                    )
                continue

        asset = asset_by_symbol.get(symbol, AssetInfo(symbol, symbol, "UNKNOWN"))
        score = _score_candidate(metric, config)
        ranked.append(
            Candidate(
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
                bb_width_pct=float(metric["bb_width_pct"]),
                high_52w=float(metric["high_52w"]),
                low_52w=float(metric["low_52w"]),
                oversold_events=int(metric["oversold_events"]),
                reversion_hit_rate=float(metric["reversion_hit_rate"]),
                avg_reversion_return_10d=float(metric["avg_reversion_return_10d"]),
                stop_failures=int(metric["stop_failures"]),
                one_day_return=float(metric["one_day_return"]),
                five_day_return=float(metric["five_day_return"]),
                score=score,
            )
        )
        if symbol in explain_symbols:
            explanations[symbol] = _format_explanation(
                "PASS", metric, config, market_cap=market_cap
            )

    ranked.sort(key=lambda c: c.score, reverse=True)
    ranked = _apply_sector_cap(ranked, config.max_per_sector, top)
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
        hit = bool((future["rsi_14"] >= config.reversion_threshold).any())
        if hit:
            hits += 1
        returns.append(float(future["close"].iloc[-1] / entry_close - 1.0))

        stop_level = entry_close - config.atr_stop_multiplier * entry_atr
        if bool((future["low"] <= stop_level).any()):
            stop_failures += 1

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
    if float(metric["avg_volume_20"]) < config.min_avg_volume_20:
        return "share_volume"
    if float(metric["avg_dollar_volume_50"]) < config.min_avg_dollar_volume_50:
        return "dollar_volume"
    if close <= float(metric["sma200"]):
        return "below_sma200"
    if close < config.min_pct_of_52w_high * float(metric["high_52w"]):
        return "too_far_from_high"
    if close < config.min_above_52w_low * float(metric["low_52w"]):
        return "too_close_to_low"
    atr_pct = float(metric["atr_pct"])
    if atr_pct < config.min_atr_pct:
        return "atr_too_low"
    if atr_pct > config.max_atr_pct:
        return "atr_too_high"
    if float(metric["bb_width_pct"]) < config.min_bb_width_pct:
        return "bb_width"
    if int(metric["oversold_events"]) < config.min_oversold_events:
        return "oversold_events"
    if float(metric["reversion_hit_rate"]) < config.min_reversion_hit_rate:
        return "reversion_hit_rate"
    if int(metric["stop_failures"]) > config.max_stop_failures:
        return "stop_failures"
    if float(metric["one_day_return"]) <= config.max_one_day_drop:
        return "one_day_drop"
    if float(metric["five_day_return"]) <= config.max_five_day_return:
        return "five_day_drop"
    return None


def _score_candidate(metric: dict[str, float | int], config: ScanConfig) -> float:
    """Composite rank score for already-qualified RSI candidates."""
    hit_rate_score = float(metric["reversion_hit_rate"]) * 100.0
    avg_return_score = max(
        0.0, min((float(metric["avg_reversion_return_10d"]) + 0.05) / 0.15, 1.0)
    ) * 100.0
    dollar_volume = float(metric["avg_dollar_volume_50"])
    liquidity_score = min(math.log10(max(dollar_volume, 1.0)) / 10.0, 1.0) * 100.0
    atr_pct = float(metric["atr_pct"])
    atr_mid = (config.min_atr_pct + config.max_atr_pct) / 2.0
    atr_range = (config.max_atr_pct - config.min_atr_pct) / 2.0
    atr_quality = max(0.0, 1.0 - abs(atr_pct - atr_mid) / atr_range) * 100.0
    low_distance = float(metric["close"]) / float(metric["low_52w"]) - 1.0
    structure_score = max(0.0, min(low_distance / 1.0, 1.0)) * 100.0
    stop_penalty = int(metric["stop_failures"]) * 8.0
    event_bonus = min(int(metric["oversold_events"]), 8) * 2.0

    return (
        hit_rate_score * 0.35
        + avg_return_score * 0.20
        + liquidity_score * 0.15
        + atr_quality * 0.15
        + structure_score * 0.15
        + event_bonus
        - stop_penalty
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
        f"price>={config.min_price:.2f}, Vol20>={config.min_avg_volume_20:,.0f}, "
        f"$Vol50>={_fmt_dollars(config.min_avg_dollar_volume_50)}, "
        f"ATR%={config.min_atr_pct * 100:.1f}-{config.max_atr_pct * 100:.1f}, "
        f"hit_rate>={config.min_reversion_hit_rate * 100:.0f}%."
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
        f"# RSI Watchlist Scan - {generated}",
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
        "- Liquidity and market-cap filters keep RSI in names that can absorb limit orders.",
        "- Solvency keeps RSI from buying companies where a sell-off may be terminal.",
        "- Price above SMA200 keeps entries in structurally intact names.",
        "- 52-week position avoids deep breakdowns while still allowing pullbacks.",
        "- Historical oversold-event behavior measures whether the stock actually mean-reverts.",
        "- ATR and Bollinger width require enough movement for opportunity without chaos.",
        "- One-day and five-day crash filters avoid news shocks and falling knives.",
        "",
        "## Top Candidates",
        "",
    ]
    if not candidates:
        lines.append("No candidates passed all enabled filters.")
    else:
        lines.extend(
            [
                "| Rank | Symbol | Score | Close | RSI | Hit % | Events | Avg10d | ATR % | BB Width | $Vol50 | Stops |",
                "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
            ]
        )
        for rank, candidate in enumerate(candidates, start=1):
            lines.append(
                "| "
                f"{rank} | {candidate.symbol} | {candidate.score:.1f} | "
                f"{candidate.close:.2f} | {candidate.rsi14:.1f} | "
                f"{candidate.reversion_hit_rate * 100:.1f}% | "
                f"{candidate.oversold_events} | "
                f"{candidate.avg_reversion_return_10d * 100:.1f}% | "
                f"{candidate.atr_pct * 100:.1f}% | "
                f"{candidate.bb_width_pct * 100:.1f}% | "
                f"{_fmt_dollars(candidate.avg_dollar_volume_50)} | "
                f"{candidate.stop_failures} |"
            )

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
            "- Sector caps are best-effort because Alpaca asset metadata does not include sector.",
            "- Fundamentals are checked only after technical filters pass; requested-symbol "
            "explanations can show `MarketCap=N/A` when a symbol failed earlier.",
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
    candidates, rejections, examples, explanations = scan_candidates(
        assets,
        bars,
        config=ScanConfig(),
        include_fundamentals=args.include_fundamentals,
        top=args.top,
        explain_symbols=explain_symbols,
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
