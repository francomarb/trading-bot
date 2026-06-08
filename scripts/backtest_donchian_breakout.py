#!/usr/bin/env python3
"""
Donchian Channel Breakout backtest harness — Turtle System 1 (and variants).

Runs the DonchianBreakout strategy across configurable universes over a
configurable date range and renders a markdown-style summary table.

Mirrors `scripts/backtest_bollinger_squeeze.py` in structure so cross-strategy
results are directly comparable. Uses the same UNIVERSES, fetch_bars, and
BacktestConfig as the BB Squeeze harness.

The script is read-only: it never writes to settings, never modifies a slot,
never submits an order. Output goes to stdout and (optionally) a markdown file
under logs/backtests/.

Usage:
    python scripts/backtest_donchian_breakout.py
    python scripts/backtest_donchian_breakout.py --sweep --universe ai_bigtech \
        --years 4 --end-date 2026-04-28 --atr-stop-mult 2.0 \
        --output logs/backtests/donchian_sweep_ai.md
    python scripts/backtest_donchian_breakout.py --symbols NVDA AVGO MSFT --no-filter

The IEX-vs-SIP feed scaling on the liquidity floor is honoured automatically
because DonchianEdgeFilter reads ALPACA_DATA_FEED at construction time.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.runner import BacktestConfig, run_backtest
from config import settings
from scripts.backtest_bollinger_squeeze import (
    UNIVERSES,
    aggregate_metrics,
    buy_and_hold_return,
    configure_logging,
    fetch_bars,
)
from strategies.donchian_breakout import DonchianBreakout
from strategies.filters.donchian_breakout import DonchianEdgeFilter


# ── Sweep grid ────────────────────────────────────────────────────────────────
# Every variant runs against the same universe & dates so results are
# apples-to-apples. Variants chosen to span the parameter space:
#   - Aggressive: catches more entries; more whipsaws
#   - System 1 (Turtle short-term): the literature default
#   - Hybrid 20/20: looser exit, lets winners run longer
#   - Hybrid 55/10: longer entry window, tight exit
#   - System 2 (Turtle long-term): fewer, cleaner signals
SWEEP_GRID: list[tuple[str, dict]] = [
    ("Aggressive (10/5)",        {"entry_window": 10, "exit_window": 5}),
    ("System 1 (20/10) default", {"entry_window": 20, "exit_window": 10}),
    ("Mid-range (30/15)",        {"entry_window": 30, "exit_window": 15}),
    ("Hybrid (55/10)",           {"entry_window": 55, "exit_window": 10}),
    ("System 2 (55/20)",         {"entry_window": 55, "exit_window": 20}),
]


# ── Per-symbol report ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolReport:
    symbol: str
    bars: int
    trade_count: int
    win_rate: float
    total_return: float
    cagr: float
    sharpe: float
    max_dd: float
    profit_factor: float
    buy_hold: float


def run_one(
    symbol: str,
    df: pd.DataFrame,
    *,
    use_filter: bool,
    config: BacktestConfig,
    strategy_params: dict | None = None,
    atr_stop_mult: float | None = None,
    atr_trail: bool = False,
) -> SymbolReport:
    edge = DonchianEdgeFilter(feed_label=settings.BACKTEST_DATA_FEED) if use_filter else None
    params = strategy_params or {}
    strategy = DonchianBreakout(edge_filter=edge, **params)
    result = run_backtest(
        strategy, df, config, symbol=symbol,
        atr_stop_mult=atr_stop_mult, atr_trail=atr_trail,
    )
    s = result.stats
    return SymbolReport(
        symbol=symbol,
        bars=len(df),
        trade_count=int(s["trade_count"]),
        win_rate=float(s["win_rate"]),
        total_return=float(s["total_return"]),
        cagr=float(s["cagr"]),
        sharpe=float(s["sharpe"]) if pd.notna(s["sharpe"]) else float("nan"),
        max_dd=float(s["max_drawdown"]),
        profit_factor=float(s["profit_factor"]) if pd.notna(s["profit_factor"]) else float("nan"),
        buy_hold=buy_and_hold_return(df),
    )


# ── Rendering ────────────────────────────────────────────────────────────────


def render_table(reports: list[SymbolReport], header: str) -> str:
    lines = [
        f"### {header}\n",
        "| Symbol | Bars | Trades | Win% | TotalR | CAGR  | Sharpe | MaxDD | ProfFact | B&H |",
        "|--------|------|--------|------|--------|-------|--------|-------|----------|-----|",
    ]
    for r in reports:
        sharpe_str = f"{r.sharpe:>+5.2f}" if pd.notna(r.sharpe) else "  n/a"
        pf_str = f"{r.profit_factor:>5.2f}" if pd.notna(r.profit_factor) and r.profit_factor != float("inf") else "  inf" if r.profit_factor == float("inf") else "  n/a"
        lines.append(
            f"| {r.symbol:<6} | {r.bars:>4} | {r.trade_count:>6} | {r.win_rate*100:>4.1f} | "
            f"{r.total_return*100:>+5.1f}% | {r.cagr*100:>+4.1f}% | {sharpe_str} | "
            f"{r.max_dd*100:>+4.1f}% | {pf_str} | {r.buy_hold*100:>+4.1f}% |"
        )
    return "\n".join(lines) + "\n"


def render_aggregate(reports: list[SymbolReport]) -> str:
    if not reports:
        return "_no symbols traded_\n"
    n = len(reports)
    sharpes = [r.sharpe for r in reports if pd.notna(r.sharpe)]
    total_trades = sum(r.trade_count for r in reports)
    weighted_winrate = (
        sum(r.win_rate * r.trade_count for r in reports) / max(1, total_trades)
    )
    mean_buy_hold = sum(r.buy_hold for r in reports) / n
    mean_return = sum(r.total_return for r in reports) / n
    return (
        f"\n**Aggregate ({n} symbols, equally weighted):**\n\n"
        f"- Mean total return: **{mean_return*100:+.1f}%**  "
        f"(B&H: {mean_buy_hold*100:+.1f}%, edge: {(mean_return - mean_buy_hold)*100:+.1f} pp)\n"
        f"- Mean CAGR:         **{sum(r.cagr for r in reports)/n*100:+.1f}%**\n"
        f"- Mean Sharpe:       **{sum(sharpes)/max(1,len(sharpes)):+.2f}**\n"
        f"- Mean MaxDD:        **{sum(r.max_dd for r in reports)/n*100:+.1f}%**\n"
        f"- Total trades:       {total_trades}  "
        f"(avg per symbol: {total_trades/n:.1f})\n"
        f"- Overall win rate:  **{weighted_winrate*100:.1f}%**\n"
    )


def render_sweep_summary(rows: list[tuple[str, dict[str, float]]]) -> str:
    lines = [
        "### Parameter sweep — aggregate (held-constant universe & dates)",
        "",
        "| Variant | MeanRet | MeanCAGR | MeanShp | MeanDD | Trades | TradedSyms | WinRate |",
        "|---------|--------:|--------:|--------:|--------:|-------:|----------:|--------:|",
    ]
    for label, m in rows:
        lines.append(
            f"| {label:<32} | "
            f"{m['mean_return']*100:>+6.1f}% | "
            f"{m['mean_cagr']*100:>+5.1f}% | "
            f"{m['mean_sharpe']:>+6.2f} | "
            f"{m['mean_dd']*100:>+5.1f}% | "
            f"{int(m['total_trades']):>5} | "
            f"{int(m['n_symbols_traded']):>9} | "
            f"{m['win_rate']*100:>5.1f}% |"
        )
    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Donchian Breakout backtest")
    parser.add_argument(
        "--years", type=float, default=4.0,
        help="Years of trailing daily history to backtest (default 4)",
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help="Pin backtest end-date (YYYY-MM-DD UTC). Strongly recommended for "
             "sweeps and re-runs — ensures all variants see the same bars.",
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Override DONCHIAN_WATCHLIST with this symbol list",
    )
    parser.add_argument(
        "--universe", type=str, default=None, choices=sorted(UNIVERSES.keys()),
        help="Use a predefined universe by name. Overrides --symbols and "
             "DONCHIAN_WATCHLIST.",
    )
    parser.add_argument(
        "--no-filter", action="store_true",
        help="Run with NO edge filter (raw signal). Useful for comparing "
             "edge-filter impact.",
    )
    parser.add_argument(
        "--with-and-without-filter", action="store_true",
        help="Run BOTH variants and print both tables for comparison.",
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="Run the parameter sweep — fetches bars ONCE then runs every "
             "variant in SWEEP_GRID against the same data. All other settings "
             "(universe, dates, slippage, feed, filter) are held constant.",
    )
    parser.add_argument(
        "--atr-stop-mult", type=float, default=None,
        help="If set, simulate a per-trade ATR-based stop-loss at "
             "entry - mult × ATR. Mirrors the production engine's "
             "ATR_STOP_MULTIPLIER (production default 2.0). Donchian is "
             "EXPECTED to benefit from stops (unlike BB Squeeze).",
    )
    parser.add_argument(
        "--atr-trail", action="store_true",
        help="When set with --atr-stop-mult, makes the ATR stop a TRAILING "
             "stop that ratchets up as price moves favourably. Locks in "
             "gains and is the textbook DD-reducer for trend-following "
             "strategies. Has no effect without --atr-stop-mult.",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Optional markdown file path to write the report",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    configure_logging(args.verbose)

    end_date: datetime | None = None
    if args.end_date:
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if args.universe:
        symbols = list(UNIVERSES[args.universe])
        logger.info(f"using universe '{args.universe}' — {len(symbols)} symbol(s)")
    else:
        symbols = args.symbols or list(settings.DONCHIAN_WATCHLIST)

    bars_by_sym = fetch_bars(symbols, args.years, end_date=end_date)
    if not bars_by_sym:
        logger.error("No bars fetched — aborting.")
        return 1

    cfg = BacktestConfig()

    bar_start = min(df.index[0] for df in bars_by_sym.values())
    bar_end   = max(df.index[-1] for df in bars_by_sym.values())

    universe_label = args.universe or "(custom)"
    sections: list[str] = []
    sections.append(
        f"# Donchian Breakout Backtest — {universe_label}\n\n"
        f"- Generated: {datetime.now(timezone.utc).isoformat()}\n"
        f"- Bars range: {bar_start.date()} → {bar_end.date()} ({args.years}y nominal)\n"
        f"- Universe: {universe_label} — {len(bars_by_sym)} of {len(symbols)} symbols\n"
        f"- Symbols: {', '.join(sorted(bars_by_sym.keys()))}\n"
        f"- Data feed: {settings.BACKTEST_DATA_FEED} (from `settings.BACKTEST_DATA_FEED`)\n"
        f"- Slippage: {cfg.slippage_bps} bps, init_cash: ${cfg.initial_cash:,.0f}\n"
        f"- Edge filter: {'ON' if not args.no_filter else 'OFF'}\n"
        f"- ATR stops: {f'{args.atr_stop_mult}× ATR' + (' (TRAILING)' if args.atr_trail else ' (fixed)') if args.atr_stop_mult else 'OFF (signal exit only)'}\n"
    )

    if args.sweep:
        sections.append(
            "**Sweep mode — held constant across all variants:** "
            "universe, bar range, init_cash, slippage, feed, filter setting, "
            "ATR-stop setting.\n"
        )
        sweep_rows: list[tuple[str, dict[str, float]]] = []
        for label, params in SWEEP_GRID:
            logger.info(f"sweep variant: {label}")
            reports: list[SymbolReport] = []
            for symbol, df in bars_by_sym.items():
                try:
                    rep = run_one(
                        symbol, df,
                        use_filter=not args.no_filter,
                        config=cfg,
                        strategy_params=params,
                        atr_stop_mult=args.atr_stop_mult,
                        atr_trail=args.atr_trail,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"{symbol} ({label}): {exc}")
                    continue
                reports.append(rep)
            sweep_rows.append((label, aggregate_metrics(reports)))

        sections.append(render_sweep_summary(sweep_rows))

        full_report = "\n\n".join(sections)
        print(full_report)
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(full_report)
            logger.info(f"wrote report to {out_path}")
        return 0

    # Non-sweep: single-variant per-symbol detail
    variants: list[tuple[str, bool]]
    if args.with_and_without_filter:
        variants = [
            ("With edge filter (default)", True),
            ("Raw signal — no filter", False),
        ]
    elif args.no_filter:
        variants = [("Raw signal — no filter", False)]
    else:
        variants = [("With edge filter (default)", True)]

    for header, use_filter in variants:
        reports: list[SymbolReport] = []
        for symbol, df in bars_by_sym.items():
            try:
                rep = run_one(
                    symbol, df,
                    use_filter=use_filter,
                    config=cfg,
                    atr_stop_mult=args.atr_stop_mult,
                    atr_trail=args.atr_trail,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{symbol}: {exc}")
                continue
            reports.append(rep)

        reports.sort(key=lambda r: r.sharpe if pd.notna(r.sharpe) else -1e9, reverse=True)
        sections.append(render_table(reports, header))
        sections.append(render_aggregate(reports))

    full_report = "\n\n".join(sections)
    print(full_report)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(full_report)
        logger.info(f"wrote report to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
