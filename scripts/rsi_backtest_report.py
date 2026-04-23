#!/usr/bin/env python3
"""
Formal RSI backtest report for promoted and comparison symbols.

This is a review artifact for Phase 10 RSI activation. It runs the exact
RSIReversion strategy through the project backtester, saves equity/drawdown
charts, and renders a compact markdown report.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.runner import BacktestConfig, BacktestResult, run_backtest, save_equity_chart
from config.settings import RSI_WATCHLIST
from scripts.rsi_candidate_validate import ValidationConfig, extract_oversold_events
from scripts.sma_watchlist_scan import configure_logging, fetch_daily_bars
from strategies.rsi_reversion import RSIReversion


RULE_VERSION = "rsi_backtest_report_v1"
DEFAULT_COMPARISONS = ["ABNB", "DINO"]


@dataclass(frozen=True)
class SymbolBacktest:
    """Backtest plus contextual validation stats for one symbol."""

    symbol: str
    group: str
    bars: int
    start: pd.Timestamp
    end: pd.Timestamp
    result: BacktestResult
    buy_hold_return: float
    event_count: int
    event_hit_rate: float
    avg_event_return: float
    stop_failures: int
    chart_path: Path


def run_symbol_backtests(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    promoted: list[str],
    comparisons: list[str],
    backtest_config: BacktestConfig,
    validation_config: ValidationConfig,
    chart_dir: Path,
) -> list[SymbolBacktest]:
    """Run exact RSI backtests and save charts for all requested symbols."""
    out: list[SymbolBacktest] = []
    group_by_symbol = {symbol: "promoted" for symbol in promoted}
    group_by_symbol.update({symbol: "comparison" for symbol in comparisons})
    strategy = RSIReversion(
        period=validation_config.rsi_period,
        oversold=validation_config.oversold,
        overbought=validation_config.overbought,
    )

    for symbol in promoted + comparisons:
        df = bars_by_symbol.get(symbol)
        if df is None or df.empty:
            continue
        clean = df[["open", "high", "low", "close", "volume"]].copy()
        clean = clean.dropna().sort_index()
        if clean.empty:
            continue

        result = run_backtest(strategy, clean, backtest_config, symbol=symbol)
        chart_path = save_equity_chart(result, chart_dir)
        events = extract_oversold_events(clean, symbol=symbol, config=validation_config)
        buy_hold_return = float(clean["close"].iloc[-1] / clean["close"].iloc[0] - 1.0)
        out.append(
            SymbolBacktest(
                symbol=symbol,
                group=group_by_symbol[symbol],
                bars=len(clean),
                start=clean.index[0],
                end=clean.index[-1],
                result=result,
                buy_hold_return=buy_hold_return,
                event_count=len(events),
                event_hit_rate=_mean([event.hit_rsi50_10d for event in events]),
                avg_event_return=_mean([event.return_10d for event in events]),
                stop_failures=sum(1 for event in events if event.stop_failed),
                chart_path=chart_path,
            )
        )
    return out


def render_report(
    rows: list[SymbolBacktest],
    *,
    feed: str,
    start: datetime,
    end: datetime,
    promoted: list[str],
    comparisons: list[str],
    backtest_config: BacktestConfig,
    validation_config: ValidationConfig,
) -> str:
    """Render a formal markdown backtest report."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# RSI Backtest Report - {generated}",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Strategy: `RSIReversion(period={validation_config.rsi_period}, "
        f"oversold={validation_config.oversold:g}, "
        f"overbought={validation_config.overbought:g})`",
        f"- Promoted symbols: {', '.join(promoted)}",
        f"- Comparison symbols: {', '.join(comparisons)}",
        f"- Alpaca feed: `{feed}`",
        f"- Data window: {start.date()} to {end.date()}",
        f"- Data end timestamp: {end.isoformat(timespec='seconds')}",
        f"- Initial cash per symbol: ${backtest_config.initial_cash:,.0f}",
        f"- Costs: slippage={backtest_config.slippage_bps:g} bps, "
        f"commission=${backtest_config.commission_per_trade:.2f}",
        "",
        "## Summary",
        "",
    ]

    if not rows:
        lines.append("No backtest rows were produced.")
        return "\n".join(lines)

    lines.extend(
        [
            "| Group | Symbol | Trades | Return | CAGR | Sharpe | Sortino | MaxDD | MaxDD Days | Win % | PF | Expectancy | Final Equity | Buy/Hold | Events | Hit % | Avg10d | Stops | Chart |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in sorted(rows, key=lambda r: (r.group != "promoted", r.symbol)):
        stats = row.result.stats
        chart = row.chart_path
        chart_link = f"[chart]({chart})"
        lines.append(
            "| "
            f"{row.group} | {row.symbol} | {int(stats['trade_count'])} | "
            f"{_fmt_pct(stats['total_return'])} | {_fmt_pct(stats['cagr'])} | "
            f"{_fmt_float(stats['sharpe'])} | {_fmt_float(stats['sortino'])} | "
            f"{_fmt_pct(stats['max_drawdown'])} | {int(stats['max_dd_days'])} | "
            f"{_fmt_pct(stats['win_rate'])} | {_fmt_float(stats['profit_factor'])} | "
            f"${stats['expectancy']:,.2f} | ${stats['final_equity']:,.2f} | "
            f"{_fmt_pct(row.buy_hold_return)} | {row.event_count} | "
            f"{_fmt_pct(row.event_hit_rate)} | {_fmt_pct(row.avg_event_return)} | "
            f"{row.stop_failures} | {chart_link} |"
        )

    promoted_rows = [row for row in rows if row.group == "promoted"]
    if promoted_rows:
        avg_return = _mean([row.result.stats["total_return"] for row in promoted_rows])
        avg_drawdown = _mean([row.result.stats["max_drawdown"] for row in promoted_rows])
        avg_pf = _mean([
            8.0 if math.isinf(row.result.stats["profit_factor"])
            else row.result.stats["profit_factor"]
            for row in promoted_rows
        ])
        lines.extend(
            [
                "",
                "## Promoted Pool Aggregate",
                "",
                f"- Average per-symbol strategy return: {_fmt_pct(avg_return)}",
                f"- Average per-symbol max drawdown: {_fmt_pct(avg_drawdown)}",
                f"- Average capped profit factor: {_fmt_float(avg_pf)}",
                f"- Symbols tested: {len(promoted_rows)}",
            ]
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- This is a per-symbol backtest, not a combined portfolio simulation.",
            "- The strategy is the current bot RSI logic: entry below RSI 30, exit above RSI 70, next-open fills.",
            "- ATR stop counts are contextual event diagnostics; the vectorbt run does not execute broker OTO stop legs.",
            "- Buy/Hold is shown only as context. RSI is a tactical strategy, so it can lag buy-and-hold in strong trends.",
            "- Large max drawdown means the current RSI exit/stop behavior still needs paper validation before activation.",
            "- Charts are saved alongside the report for visual inspection of equity curve pain periods.",
        ]
    )
    return "\n".join(lines)


def _mean(values: list[float | bool]) -> float:
    if not values:
        return 0.0
    return float(sum(float(value) for value in values) / len(values))


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
        description="Generate a formal RSI backtest report.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=RSI_WATCHLIST,
        help="Promoted RSI symbols to backtest.",
    )
    parser.add_argument(
        "--comparisons",
        nargs="+",
        default=DEFAULT_COMPARISONS,
        help="Rejected/watch symbols to include for comparison.",
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
        default=Path("logs/rsi_backtest_report_latest.md"),
        help="Markdown report path.",
    )
    parser.add_argument(
        "--chart-dir",
        type=Path,
        default=Path("logs/rsi_backtests"),
        help="Directory for equity/drawdown charts.",
    )
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    promoted = [symbol.upper() for symbol in args.symbols]
    comparisons = [symbol.upper() for symbol in args.comparisons]
    symbols = list(dict.fromkeys(promoted + comparisons))
    end = datetime.now(timezone.utc) - timedelta(minutes=args.end_delay_minutes)
    start = end - timedelta(days=args.lookback_days)

    from loguru import logger

    logger.info(
        f"RSI backtest report started: rule={RULE_VERSION} feed={args.feed} "
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

    backtest_config = BacktestConfig()
    validation_config = ValidationConfig()
    rows = run_symbol_backtests(
        bars,
        promoted=promoted,
        comparisons=comparisons,
        backtest_config=backtest_config,
        validation_config=validation_config,
        chart_dir=args.chart_dir,
    )
    report = render_report(
        rows,
        feed=args.feed,
        start=start,
        end=end,
        promoted=promoted,
        comparisons=comparisons,
        backtest_config=backtest_config,
        validation_config=validation_config,
    )
    print(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        logger.info(f"report saved: {args.output}")


if __name__ == "__main__":
    main()
