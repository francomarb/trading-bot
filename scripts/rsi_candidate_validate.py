#!/usr/bin/env python3
"""
Validate RSI watchlist candidates against rejected controls.

This script is a report-only companion to scripts/rsi_watchlist_scan.py. It
pulls daily Alpaca bars, summarizes historical RSI oversold events, and runs
the bot's exact RSIReversion strategy through the vectorbt backtester.

Example:
    python scripts/rsi_candidate_validate.py
    python scripts/rsi_candidate_validate.py --symbols DINO CDNS --controls NVDA MSFT
    python scripts/rsi_candidate_validate.py --feed sip --end-delay-minutes 60
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

from backtest.runner import BacktestConfig, run_backtest
from indicators.technicals import add_atr, add_rsi
from scripts.sma_watchlist_scan import configure_logging, fetch_daily_bars
from strategies.rsi_reversion import RSIReversion


RULE_VERSION = "rsi_validation_v1"
DEFAULT_SYMBOLS = ["DINO", "CDNS"]
DEFAULT_CONTROLS = ["NVDA", "MSFT"]
SCANNER_CANDIDATES = [
    "DINO",
    "ENTG",
    "CDNS",
    "ALLY",
    "ABNB",
    "TFC",
    "BA",
    "SBAC",
    "WLK",
    "CCK",
    "SN",
]


@dataclass(frozen=True)
class ValidationConfig:
    """RSI validation thresholds and assumptions."""

    rsi_period: int = 14
    oversold: float = 30.0
    overbought: float = 70.0
    reversion_threshold: float = 50.0
    event_window_days: int = 10
    atr_period: int = 14
    atr_stop_multiplier: float = 2.0
    initial_cash: float = 100_000.0
    slippage_bps: float = 5.0
    commission_per_trade: float = 0.0


@dataclass(frozen=True)
class EventRecord:
    """One RSI oversold event and its forward validation result."""

    symbol: str
    date: pd.Timestamp
    close: float
    rsi: float
    hit_rsi50_10d: bool
    return_10d: float
    max_drawdown_10d: float
    stop_failed: bool
    days_to_rsi50: int | None


@dataclass(frozen=True)
class SymbolValidation:
    """Validation summary for one symbol."""

    symbol: str
    group: str
    bars: int
    start: pd.Timestamp
    end: pd.Timestamp
    events: list[EventRecord]
    backtest_stats: dict[str, float]
    buy_hold_return: float

    @property
    def event_hit_rate(self) -> float:
        return _mean([event.hit_rsi50_10d for event in self.events])

    @property
    def avg_event_return(self) -> float:
        return _mean([event.return_10d for event in self.events])

    @property
    def avg_event_drawdown(self) -> float:
        return _mean([event.max_drawdown_10d for event in self.events])

    @property
    def stop_failures(self) -> int:
        return sum(1 for event in self.events if event.stop_failed)


def validate_symbols(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    candidates: list[str],
    controls: list[str],
    config: ValidationConfig,
) -> list[SymbolValidation]:
    """Validate each symbol with raw events plus exact RSI strategy backtest."""
    out: list[SymbolValidation] = []
    group_by_symbol = {symbol: "candidate" for symbol in candidates}
    group_by_symbol.update({symbol: "control" for symbol in controls})

    for symbol in candidates + controls:
        df = bars_by_symbol.get(symbol)
        if df is None or df.empty:
            continue
        clean = df[["open", "high", "low", "close", "volume"]].copy()
        clean = clean.dropna().sort_index()
        if clean.empty:
            continue

        events = extract_oversold_events(clean, symbol=symbol, config=config)
        strategy = RSIReversion(
            period=config.rsi_period,
            oversold=config.oversold,
            overbought=config.overbought,
        )
        bt = run_backtest(
            strategy,
            clean,
            BacktestConfig(
                initial_cash=config.initial_cash,
                slippage_bps=config.slippage_bps,
                commission_per_trade=config.commission_per_trade,
            ),
            symbol=symbol,
        )
        buy_hold_return = float(clean["close"].iloc[-1] / clean["close"].iloc[0] - 1.0)
        out.append(
            SymbolValidation(
                symbol=symbol,
                group=group_by_symbol[symbol],
                bars=len(clean),
                start=clean.index[0],
                end=clean.index[-1],
                events=events,
                backtest_stats=bt.stats,
                buy_hold_return=buy_hold_return,
            )
        )
    return out


def extract_oversold_events(
    df: pd.DataFrame,
    *,
    symbol: str,
    config: ValidationConfig,
) -> list[EventRecord]:
    """Return every RSI cross below oversold and its next-N-day outcome."""
    work = add_rsi(df, config.rsi_period)
    work = add_atr(work, config.atr_period)
    rsi_col = f"rsi_{config.rsi_period}"
    atr_col = f"atr_{config.atr_period}"
    rsi = work[rsi_col]
    events = (rsi < config.oversold) & (rsi.shift(1) >= config.oversold)
    records: list[EventRecord] = []

    for idx, is_event in enumerate(events.fillna(False).tolist()):
        if not is_event or idx + 1 >= len(work):
            continue
        end_idx = min(idx + config.event_window_days, len(work) - 1)
        future = work.iloc[idx + 1 : end_idx + 1]
        if future.empty:
            continue
        row = work.iloc[idx]
        entry_close = float(row["close"])
        entry_atr = float(row[atr_col])
        if entry_close <= 0 or math.isnan(entry_atr):
            continue

        hit_mask = future[rsi_col] >= config.reversion_threshold
        days_to_rsi50 = None
        if bool(hit_mask.any()):
            hit_pos = int(hit_mask.to_numpy().argmax())
            days_to_rsi50 = hit_pos + 1

        min_low = float(future["low"].min())
        max_drawdown = min(0.0, min_low / entry_close - 1.0)
        stop_level = entry_close - config.atr_stop_multiplier * entry_atr
        records.append(
            EventRecord(
                symbol=symbol,
                date=work.index[idx],
                close=entry_close,
                rsi=float(row[rsi_col]),
                hit_rsi50_10d=bool(hit_mask.any()),
                return_10d=float(future["close"].iloc[-1] / entry_close - 1.0),
                max_drawdown_10d=max_drawdown,
                stop_failed=bool(min_low <= stop_level),
                days_to_rsi50=days_to_rsi50,
            )
        )
    return records


def render_report(
    results: list[SymbolValidation],
    *,
    feed: str,
    start: datetime,
    end: datetime,
    candidates: list[str],
    controls: list[str],
    config: ValidationConfig,
) -> str:
    """Render validation results as markdown."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# RSI Candidate Validation - {generated}",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Source scanner rule: `rsi_watchlist_v1`",
        f"- Candidates: {', '.join(candidates)}",
        f"- Controls: {', '.join(controls)}",
        f"- Alpaca feed: `{feed}`",
        f"- Data window: {start.date()} to {end.date()}",
        f"- Data end timestamp: {end.isoformat(timespec='seconds')}",
        f"- RSI: period={config.rsi_period}, oversold={config.oversold:g}, "
        f"overbought={config.overbought:g}",
        f"- Costs: slippage={config.slippage_bps:g} bps, "
        f"commission=${config.commission_per_trade:.2f}",
        "",
        "## Summary",
        "",
    ]

    if not results:
        lines.append("No validation results were produced.")
        return "\n".join(lines)

    lines.extend(
        [
            "| Group | Symbol | Bars | Events | Event Hit % | Avg 10d | Avg 10d DD | Stops | Strategy Trades | Strategy Return | Strategy MaxDD | Strategy PF | Buy/Hold |",
            "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for result in results:
        stats = result.backtest_stats
        lines.append(
            "| "
            f"{result.group} | {result.symbol} | {result.bars} | "
            f"{len(result.events)} | {_fmt_pct(result.event_hit_rate)} | "
            f"{_fmt_pct(result.avg_event_return)} | "
            f"{_fmt_pct(result.avg_event_drawdown)} | "
            f"{result.stop_failures} | {int(stats['trade_count'])} | "
            f"{_fmt_pct(stats['total_return'])} | "
            f"{_fmt_pct(stats['max_drawdown'])} | "
            f"{_fmt_float(stats['profit_factor'])} | "
            f"{_fmt_pct(result.buy_hold_return)} |"
        )

    lines.extend(
        [
            "",
            "## Event Detail",
            "",
            "| Symbol | Date | Close | RSI | Hit RSI50 <=10d | Days | 10d Return | 10d Max DD | ATR Stop Failed |",
            "|---|---|---:|---:|---|---:|---:|---:|---|",
        ]
    )
    for result in results:
        if not result.events:
            lines.append(
                f"| {result.symbol} | - | - | - | no oversold events | - | - | - | - |"
            )
            continue
        for event in result.events:
            lines.append(
                "| "
                f"{event.symbol} | {event.date.date()} | {event.close:.2f} | "
                f"{event.rsi:.1f} | {'yes' if event.hit_rsi50_10d else 'no'} | "
                f"{event.days_to_rsi50 if event.days_to_rsi50 is not None else '-'} | "
                f"{_fmt_pct(event.return_10d)} | "
                f"{_fmt_pct(event.max_drawdown_10d)} | "
                f"{'yes' if event.stop_failed else 'no'} |"
            )

    lines.extend(
        [
            "",
            "## Reading This",
            "",
            "- Event Hit % asks whether RSI recovered to 50 within 10 trading days after an oversold cross.",
            "- Strategy Return is the exact bot RSI strategy: enter on RSI cross below 30, exit on RSI cross above 70, filled next open with costs.",
            "- Buy/Hold is included as a baseline, not as the strategy benchmark.",
            "- Earnings-date overlay is not implemented yet; treat event rows near earnings as requiring manual review.",
            "- A good validation result should beat rejected controls on event quality, drawdown behavior, or exact strategy results.",
        ]
    )
    return "\n".join(lines)


def _mean(values: list[float | bool]) -> float:
    if not values:
        return 0.0
    return float(sum(float(v) for v in values) / len(values))


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
        description="Validate RSI candidates against rejected controls.",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=DEFAULT_SYMBOLS,
        help="Candidate symbols to validate.",
    )
    parser.add_argument(
        "--controls",
        nargs="+",
        default=DEFAULT_CONTROLS,
        help="Rejected/control symbols to compare against.",
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
        default=Path("logs/rsi_candidate_validation_latest.md"),
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
        f"RSI validation started: rule={RULE_VERSION} feed={args.feed} "
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

    config = ValidationConfig()
    results = validate_symbols(
        bars,
        candidates=candidates,
        controls=controls,
        config=config,
    )
    report = render_report(
        results,
        feed=args.feed,
        start=start,
        end=end,
        candidates=candidates,
        controls=controls,
        config=config,
    )
    print(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        logger.info(f"report saved: {args.output}")


if __name__ == "__main__":
    main()
