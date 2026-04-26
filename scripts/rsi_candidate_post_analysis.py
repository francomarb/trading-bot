#!/usr/bin/env python3
"""
Post-process RSI validation winners into a promotion ranking.

The RSI scanner answers: "Which large, liquid stocks have the right setup
profile?" The validator answers: "How did these symbols behave historically?"
This post-analysis layer answers: "Which validated symbols are clean enough to
promote first?"

It intentionally penalizes deep drawdown and stop failures. A huge return with
a 60% drawdown should not outrank a more boring, more survivable candidate.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.rsi_candidate_validate import (
    SCANNER_CANDIDATES,
    ValidationConfig,
    SymbolValidation,
    validate_symbols,
)
from scripts.sma_watchlist_scan import configure_logging, fetch_daily_bars


RULE_VERSION = "rsi_post_analysis_v1"
DEFAULT_CONTROLS = ["NVDA", "MSFT", "AVGO", "AAPL"]


@dataclass(frozen=True)
class PostAnalysisConfig:
    """Promotion thresholds and scoring weights."""

    min_events: int = 5
    min_strategy_return: float = 0.20
    min_profit_factor: float = 1.20
    max_strategy_drawdown: float = -0.65
    min_event_hit_rate: float = 0.35
    max_stop_rate: float = 0.35


@dataclass(frozen=True)
class RankedCandidate:
    """One post-analysis ranked symbol."""

    symbol: str
    group: str
    score: float
    verdict: str
    reasons: list[str]
    result: SymbolValidation


def rank_results(
    results: list[SymbolValidation],
    *,
    config: PostAnalysisConfig,
) -> list[RankedCandidate]:
    """Score validation results with explicit promotion guardrails."""
    ranked = [
        RankedCandidate(
            symbol=result.symbol,
            group=result.group,
            score=_score_result(result),
            verdict=_verdict(result, config),
            reasons=_reasons(result, config),
            result=result,
        )
        for result in results
    ]
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def _score_result(result: SymbolValidation) -> float:
    stats = result.backtest_stats
    trade_return = float(stats["total_return"])
    max_drawdown = abs(float(stats["max_drawdown"]))
    profit_factor = float(stats["profit_factor"])
    if math.isinf(profit_factor):
        profit_factor = 8.0
    event_hit_rate = result.event_hit_rate
    avg_event_return = result.avg_event_return
    avg_event_drawdown = abs(result.avg_event_drawdown)
    event_count = len(result.events)
    stop_rate = result.stop_failures / event_count if event_count else 1.0

    return (
        _clamp(trade_return / 1.25, -1.0, 1.0) * 30.0
        + _clamp((profit_factor - 1.0) / 4.0, -1.0, 1.0) * 20.0
        + _clamp(event_hit_rate / 0.70, 0.0, 1.0) * 20.0
        + _clamp(avg_event_return / 0.08, -1.0, 1.0) * 15.0
        + _clamp(event_count / 15.0, 0.0, 1.0) * 10.0
        - _clamp(max_drawdown / 0.60, 0.0, 1.5) * 25.0
        - _clamp(avg_event_drawdown / 0.10, 0.0, 1.5) * 10.0
        - _clamp(stop_rate / 0.40, 0.0, 1.5) * 15.0
    )


def _verdict(result: SymbolValidation, config: PostAnalysisConfig) -> str:
    reasons = _reasons(result, config)
    if not reasons:
        return "PROMOTE"
    hard_fails = {
        "negative or weak exact strategy return",
        "profit factor below threshold",
        "strategy drawdown too deep",
    }
    if any(reason in hard_fails for reason in reasons):
        return "REJECT"
    return "WATCH"


def _reasons(result: SymbolValidation, config: PostAnalysisConfig) -> list[str]:
    stats = result.backtest_stats
    event_count = len(result.events)
    stop_rate = result.stop_failures / event_count if event_count else 1.0
    profit_factor = float(stats["profit_factor"])
    reasons: list[str] = []

    if event_count < config.min_events:
        reasons.append("too few oversold events")
    if float(stats["total_return"]) < config.min_strategy_return:
        reasons.append("negative or weak exact strategy return")
    if not math.isinf(profit_factor) and profit_factor < config.min_profit_factor:
        reasons.append("profit factor below threshold")
    if float(stats["max_drawdown"]) < config.max_strategy_drawdown:
        reasons.append("strategy drawdown too deep")
    if result.event_hit_rate < config.min_event_hit_rate:
        reasons.append("event hit rate below threshold")
    if stop_rate > config.max_stop_rate:
        reasons.append("too many ATR stop failures")
    return reasons


def render_report(
    ranked: list[RankedCandidate],
    *,
    feed: str,
    start: datetime,
    end: datetime,
    candidates: list[str],
    controls: list[str],
    post_config: PostAnalysisConfig,
    validation_config: ValidationConfig,
) -> str:
    """Render a markdown promotion-ranking report."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# RSI Candidate Post-Analysis - {generated}",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Source scanner rule: `rsi_watchlist_v1`",
        f"- Source validation rule: `rsi_validation_v1`",
        f"- Candidates: {', '.join(candidates)}",
        f"- Controls: {', '.join(controls)}",
        f"- Alpaca feed: `{feed}`",
        f"- Data window: {start.date()} to {end.date()}",
        f"- Data end timestamp: {end.isoformat(timespec='seconds')}",
        f"- RSI: period={validation_config.rsi_period}, "
        f"oversold={validation_config.oversold:g}, "
        f"overbought={validation_config.overbought:g}",
        "",
        "## Promotion Ranking",
        "",
        "| Rank | Verdict | Group | Symbol | Score | Events | Hit % | Avg 10d | Stops | Stop % | Strategy Return | MaxDD | PF | Reasons |",
        "|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]

    for rank, item in enumerate(ranked, start=1):
        result = item.result
        stats = result.backtest_stats
        events = len(result.events)
        stop_rate = result.stop_failures / events if events else 0.0
        reasons = "; ".join(item.reasons) if item.reasons else "-"
        lines.append(
            "| "
            f"{rank} | {item.verdict} | {item.group} | {item.symbol} | "
            f"{item.score:.1f} | {events} | {_fmt_pct(result.event_hit_rate)} | "
            f"{_fmt_pct(result.avg_event_return)} | {result.stop_failures} | "
            f"{_fmt_pct(stop_rate)} | {_fmt_pct(float(stats['total_return']))} | "
            f"{_fmt_pct(float(stats['max_drawdown']))} | "
            f"{_fmt_float(float(stats['profit_factor']))} | {reasons} |"
        )

    promoted = [item.symbol for item in ranked if item.verdict == "PROMOTE"]
    watch = [item.symbol for item in ranked if item.verdict == "WATCH"]
    rejected = [item.symbol for item in ranked if item.verdict == "REJECT"]
    lines.extend(
        [
            "",
            "## Buckets",
            "",
            f"- Promote first: {', '.join(promoted) if promoted else 'none'}",
            f"- Watch / needs refinement: {', '.join(watch) if watch else 'none'}",
            f"- Reject for now: {', '.join(rejected) if rejected else 'none'}",
            "",
            "## Guardrails",
            "",
            f"- Minimum events: {post_config.min_events}",
            f"- Minimum strategy return: {_fmt_pct(post_config.min_strategy_return)}",
            f"- Minimum profit factor: {post_config.min_profit_factor:.2f}",
            f"- Maximum strategy drawdown: {_fmt_pct(post_config.max_strategy_drawdown)}",
            f"- Minimum event hit rate: {_fmt_pct(post_config.min_event_hit_rate)}",
            f"- Maximum stop-failure rate: {_fmt_pct(post_config.max_stop_rate)}",
            "",
            "## Notes",
            "",
            "- This is post-processing, not a replacement for the RSI scanner.",
            "- The raw backtest lacks the bot's live Edge Filters (like SPY macro regime), so the max drawdown allowance is intentionally wide (-65%) to account for unprotected bear market exposure.",
            "- Profit factor minimum is 1.20 because this is an unprotected base strategy; edge filters in live trading will improve this.",
            "- Controls are included to sanity-check whether rejected favorites would accidentally rank well.",
            "- Earnings-date overlay is still not implemented and remains a manual review item.",
        ]
    )
    return "\n".join(lines)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fmt_pct(value: float) -> str:
    if math.isnan(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def _fmt_float(value: float) -> str:
    if math.isinf(value):
        return "inf"
    if math.isnan(value):
        return "n/a"
    return f"{value:.2f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Post-process RSI validation results into a promotion ranking.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=SCANNER_CANDIDATES,
        help="Candidate symbols to validate and rank.",
    )
    parser.add_argument(
        "--controls",
        nargs="+",
        default=DEFAULT_CONTROLS,
        help="Rejected/control symbols to include for sanity comparison.",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=1825,
        help="Calendar days of daily bars to fetch.",
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
        help="Delay scan end time for SIP historical-data restrictions.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/rsi_candidate_post_analysis_latest.md"),
        help="Markdown report path.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    candidates = [symbol.upper() for symbol in args.symbols]
    controls = [symbol.upper() for symbol in args.controls]
    symbols = list(dict.fromkeys(candidates + controls))
    end = datetime.now(timezone.utc) - timedelta(minutes=args.end_delay_minutes)
    start = end - timedelta(days=args.lookback_days)

    from loguru import logger

    logger.info(
        f"RSI post-analysis started: rule={RULE_VERSION} feed={args.feed} "
        f"symbols={','.join(symbols)}"
    )
    bars = fetch_daily_bars(
        symbols,
        start,
        end,
        chunk_size=200,
        feed=args.feed,
    )
    logger.info(f"assets with daily bars: {len(bars)}")

    validation_config = ValidationConfig()
    post_config = PostAnalysisConfig()
    results = validate_symbols(
        bars,
        candidates=candidates,
        controls=controls,
        config=validation_config,
    )
    ranked = rank_results(results, config=post_config)
    report = render_report(
        ranked,
        feed=args.feed,
        start=start,
        end=end,
        candidates=candidates,
        controls=controls,
        post_config=post_config,
        validation_config=validation_config,
    )
    print(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        logger.info(f"report saved: {args.output}")


if __name__ == "__main__":
    main()
