#!/usr/bin/env python3
"""
Bollinger Squeeze backtest report.

Runs the BollingerSqueeze strategy across the BOLLINGER_WATCHLIST over a
configurable date range and renders a markdown-style summary table covering:

  - Per-symbol metrics: total return, CAGR, Sharpe, MaxDD, profit factor,
    trade count, win rate, vs buy-and-hold
  - Aggregate portfolio metrics (equally weighted across symbols)
  - Comparison rows: with edge filter (default) vs raw signal (no filter),
    so the impact of the filter on signal quality is visible

The script is read-only: it never writes to settings, never modifies a slot,
never submits an order. Output goes to stdout and (optionally) a markdown file
under logs/backtests/.

Usage:
    python scripts/backtest_bollinger_squeeze.py
    python scripts/backtest_bollinger_squeeze.py --years 5 --output logs/backtests/squeeze.md
    python scripts/backtest_bollinger_squeeze.py --symbols NVDA AVGO AMD MSFT
    python scripts/backtest_bollinger_squeeze.py --no-filter   # raw signal only

The IEX-vs-SIP feed scaling on the liquidity floor is honoured automatically
because BollingerSqueezeEdgeFilter reads ALPACA_DATA_FEED at construction time.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.runner import BacktestConfig, run_backtest, save_equity_chart
from config import settings
from data.fetcher import fetch_symbol
from strategies.bollinger_squeeze import BollingerSqueeze
from strategies.filters.bollinger_squeeze import BollingerSqueezeEdgeFilter


@dataclass(frozen=True)
class SymbolReport:
    symbol: str
    bars: int
    start: pd.Timestamp
    end: pd.Timestamp
    total_return: float
    cagr: float
    sharpe: float
    max_dd: float
    profit_factor: float
    trade_count: int
    win_rate: float
    buy_hold_return: float
    chart_path: Path | None


def configure_logging(verbose: bool) -> None:
    logger.remove()
    level = "DEBUG" if verbose else "INFO"
    logger.add(sys.stderr, level=level, format="{time:HH:mm:ss} | {level: <7} | {message}")


def fetch_bars(
    symbols: list[str], years: float, end_date: datetime | None = None
) -> dict[str, pd.DataFrame]:
    """
    Fetch daily bars over the trailing `years` years using the project fetcher.

    `end_date` (UTC) pins the backtest end. When None, defaults to "now minus
    60 minutes" — but for sweeps you should ALWAYS pass a fixed value so all
    runs see the same bars and metric differences come from strategy params,
    not from data drift between runs.
    """
    end = end_date if end_date is not None else (
        datetime.now(timezone.utc) - timedelta(minutes=60)
    )
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    start = end - timedelta(days=int(365 * years) + 30)

    from config import settings
    backtest_feed = settings.BACKTEST_DATA_FEED
    out: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        try:
            df, stats = fetch_symbol(symbol, start, end, "1Day", feed=backtest_feed)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{symbol}: fetch failed — {exc}")
            continue
        if df is None or df.empty:
            logger.warning(f"{symbol}: no bars returned")
            continue
        df = df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
        if len(df) < 60:
            logger.warning(f"{symbol}: only {len(df)} bars — skipping")
            continue
        out[symbol] = df
        logger.info(
            f"{symbol}: {len(df)} bars from {df.index[0].date()} to {df.index[-1].date()}"
        )
    return out


def buy_and_hold_return(df: pd.DataFrame) -> float:
    """Total return of buy-on-first-bar / hold-to-last-bar."""
    if len(df) < 2:
        return 0.0
    first_open = float(df["open"].iloc[0])
    last_close = float(df["close"].iloc[-1])
    if first_open <= 0:
        return 0.0
    return last_close / first_open - 1.0


def run_one(
    symbol: str,
    df: pd.DataFrame,
    *,
    use_filter: bool,
    config: BacktestConfig,
    save_chart: bool,
    chart_dir: Path,
    strategy_params: dict | None = None,
    atr_stop_mult: float | None = None,
) -> SymbolReport:
    edge = BollingerSqueezeEdgeFilter() if use_filter else None
    params = strategy_params or {}
    strategy = BollingerSqueeze(edge_filter=edge, **params)
    result = run_backtest(
        strategy, df, config, symbol=symbol, atr_stop_mult=atr_stop_mult
    )
    chart_path: Path | None = None
    if save_chart:
        chart_path = save_equity_chart(result, out_dir=chart_dir)
    s = result.stats
    return SymbolReport(
        symbol=symbol,
        bars=len(df),
        start=df.index[0],
        end=df.index[-1],
        total_return=float(s["total_return"]),
        cagr=float(s["cagr"]),
        sharpe=float(s["sharpe"]),
        max_dd=float(s["max_drawdown"]),
        profit_factor=float(s["profit_factor"]),
        trade_count=int(s["trade_count"]),
        win_rate=float(s["win_rate"]),
        buy_hold_return=buy_and_hold_return(df),
        chart_path=chart_path,
    )


def render_table(reports: list[SymbolReport], header: str) -> str:
    lines = [
        f"### {header}",
        "",
        "| Symbol | Bars | Trades | Win% | TotalR | CAGR  | Sharpe | MaxDD | ProfFact | B&H |",
        "|--------|------|--------|------|--------|-------|--------|-------|----------|-----|",
    ]
    for r in reports:
        lines.append(
            f"| {r.symbol:<6} | {r.bars:>4} | {r.trade_count:>6} | "
            f"{r.win_rate*100:>4.1f} | {r.total_return*100:>+6.1f}% | "
            f"{r.cagr*100:>+5.1f}% | {r.sharpe:>+6.2f} | "
            f"{r.max_dd*100:>+5.1f}% | {r.profit_factor:>8.2f} | "
            f"{r.buy_hold_return*100:>+5.1f}% |"
        )
    return "\n".join(lines)


def aggregate_summary(reports: list[SymbolReport]) -> str:
    if not reports:
        return "(no reports)"
    n = len(reports)
    avg_return = sum(r.total_return for r in reports) / n
    avg_cagr = sum(r.cagr for r in reports) / n
    avg_sharpe = sum(r.sharpe for r in reports if pd.notna(r.sharpe)) / max(
        1, sum(1 for r in reports if pd.notna(r.sharpe))
    )
    avg_dd = sum(r.max_dd for r in reports) / n
    total_trades = sum(r.trade_count for r in reports)
    overall_winrate = (
        sum(r.win_rate * r.trade_count for r in reports) / max(1, total_trades)
    )
    avg_bh = sum(r.buy_hold_return for r in reports) / n
    avg_pf = sum(r.profit_factor for r in reports if pd.notna(r.profit_factor)) / max(
        1, sum(1 for r in reports if pd.notna(r.profit_factor) and r.profit_factor != float("inf"))
    )

    edge_vs_bh = avg_return - avg_bh

    lines = [
        "",
        f"**Aggregate ({n} symbols, equally weighted):**",
        "",
        f"- Mean total return: **{avg_return*100:+.1f}%**  (B&H: {avg_bh*100:+.1f}%, edge: {edge_vs_bh*100:+.1f} pp)",
        f"- Mean CAGR:         **{avg_cagr*100:+.1f}%**",
        f"- Mean Sharpe:       **{avg_sharpe:+.2f}**",
        f"- Mean MaxDD:        **{avg_dd*100:+.1f}%**",
        f"- Mean profit factor:**{avg_pf:.2f}**",
        f"- Total trades:       {total_trades}  (avg per symbol: {total_trades/n:.1f})",
        f"- Overall win rate:  **{overall_winrate*100:.1f}%**",
    ]
    return "\n".join(lines)


def aggregate_metrics(reports: list[SymbolReport]) -> dict[str, float]:
    """Reduce a list of per-symbol reports to a single row of aggregate metrics."""
    if not reports:
        return {
            "mean_return": 0.0, "mean_cagr": 0.0, "mean_sharpe": 0.0,
            "mean_dd": 0.0, "total_trades": 0, "win_rate": 0.0,
            "n_symbols_traded": 0,
        }
    n = len(reports)
    sharpe_vals = [r.sharpe for r in reports if pd.notna(r.sharpe)]
    total_trades = sum(r.trade_count for r in reports)
    weighted_winrate = (
        sum(r.win_rate * r.trade_count for r in reports) / max(1, total_trades)
    )
    return {
        "mean_return": sum(r.total_return for r in reports) / n,
        "mean_cagr":   sum(r.cagr for r in reports) / n,
        "mean_sharpe": sum(sharpe_vals) / max(1, len(sharpe_vals)),
        "mean_dd":     sum(r.max_dd for r in reports) / n,
        "total_trades": total_trades,
        "win_rate":     weighted_winrate,
        "n_symbols_traded": sum(1 for r in reports if r.trade_count > 0),
    }


# ── Sweep grid ────────────────────────────────────────────────────────────────
# Each entry isolates one knob from baseline (last entry tests stacked changes).
SWEEP_GRID: list[tuple[str, dict]] = [
    ("Baseline (bb=20, min=6, roc=5)",   {"bb_length": 20, "kc_length": 20, "min_squeeze_bars": 6, "roc_lookback": 5}),
    ("Shorter bands (bb=10, min=6)",      {"bb_length": 10, "kc_length": 10, "min_squeeze_bars": 6, "roc_lookback": 5}),
    ("Lower duration (bb=20, min=4)",     {"bb_length": 20, "kc_length": 20, "min_squeeze_bars": 4, "roc_lookback": 5}),
    ("Shorter ROC (bb=20, min=6, roc=3)", {"bb_length": 20, "kc_length": 20, "min_squeeze_bars": 6, "roc_lookback": 3}),
    ("Aggressive combo (bb=10/min=4/roc=3)", {"bb_length": 10, "kc_length": 10, "min_squeeze_bars": 4, "roc_lookback": 3}),
]


# ── Predefined universes ──────────────────────────────────────────────────────
# Selection rationale for each lives in docs/bollinger_squeeze_universe_research.md.
# Keep universes here so backtest runs are reproducible by name (no copy-pasting
# symbol lists between markdown and CLI invocations).

UNIVERSES: dict[str, list[str]] = {
    # AI / Big-Tech / Semis — the user's directional thesis universe.
    # Kept in sync with config.settings.DONCHIAN_WATCHLIST (the deployment
    # universe candidate for the Donchian Breakout strategy). Updated 2026-05-01
    # to 32 names: original 23 AI core + 9 AI-adjacent names restored per user
    # direction. AI-adjacent names (ASML, CLS, CIEN, CEG, VST, BE, PWR, RGTI,
    # QBTS) capture AI infrastructure, data-centre power, quantum computing, and
    # semiconductor-equipment themes. RGTI and QBTS have <4y history; backtest
    # runs on available bars.
    "ai_bigtech": [
        # AI / Semis (primary)
        "NVDA", "AMD", "AVGO", "SMCI", "TSM", "MU", "QCOM", "ARM", "MRVL",
        # AI infrastructure / data-centre buildout
        "ANET", "VRT",
        # Big Tech
        "MSFT", "AAPL", "GOOGL", "META", "AMZN", "ORCL", "TSLA",
        # AI software (secondary)
        "PLTR", "CRWD", "NOW",
        # AI compute / quantum
        "IREN", "IONQ",
        # AI-adjacent (semiconductor equipment, networking, power, quantum)
        "ASML", "CLS", "CIEN", "CEG", "VST", "BE", "PWR", "RGTI", "QBTS",
    ],
    # Blended universe — AI core + diversifiers across non-AI sectors.
    # Hypothesis: cross-sector diversification reduces concurrent stop-out
    # cascades that drove ai_bigtech's -33.6% MeanDD on Donchian.
    # Created 2026-04-30 as a DD-reduction experiment.
    # Some names are AI-adjacent (ASML/CLS/CIEN/CEG/VST/BE/PWR/RGTI/QBTS)
    # and may not provide as much diversification as the pure
    # healthcare/financials/consumer/industrial names.
    # QBTS and RGTI have <4y history (SPAC mergers in 2022) — backtest runs
    # on whatever bars are available.
    "ai_bigtech_blend": [
        # AI core (23 — same as ai_bigtech)
        "NVDA", "AMD", "AVGO", "SMCI", "TSM", "MU", "QCOM", "ARM", "MRVL",
        "ANET", "VRT",
        "MSFT", "AAPL", "GOOGL", "META", "AMZN", "ORCL", "TSLA",
        "PLTR", "CRWD", "NOW",
        "IREN", "IONQ",
        # AI-adjacent expansions (likely correlated with AI core)
        "ASML", "CLS", "CIEN", "CEG", "VST", "BE", "PWR", "RGTI", "QBTS",
        # Healthcare (genuine diversifier)
        "LLY", "NVO", "UNH", "GMED", "ISRG",
        # Financials (genuine diversifier)
        "JPM", "SPGI", "MCO", "SOFI",
        # Defense (genuine diversifier)
        "LMT", "RTX",
        # Payments (genuine diversifier)
        "V", "MA",
        # Consumer (genuine diversifier)
        "COST", "HD",
        # Industrials (genuine diversifier)
        "CAT", "ROP",
        # Utilities (genuine diversifier)
        "NEE",
    ],
    # Purified blend — 23 AI core + 18 genuine diversifiers (41 names).
    # Removes the 9 AI-adjacent names (ASML, CLS, CIEN, CEG, VST, BE, PWR,
    # RGTI, QBTS) that proved highly correlated with the AI core and therefore
    # failed to reduce drawdown in the full 50-name blend test.
    # Hypothesis: fewer but genuinely uncorrelated diversifiers improve DD
    # without the Sharpe drag from correlated names.
    "ai_bigtech_blend_pure": [
        # AI core (23 — same as ai_bigtech)
        "NVDA", "AMD", "AVGO", "SMCI", "TSM", "MU", "QCOM", "ARM", "MRVL",
        "ANET", "VRT",
        "MSFT", "AAPL", "GOOGL", "META", "AMZN", "ORCL", "TSLA",
        "PLTR", "CRWD", "NOW",
        "IREN", "IONQ",
        # Healthcare (genuine diversifier)
        "LLY", "NVO", "UNH", "GMED", "ISRG",
        # Financials (genuine diversifier)
        "JPM", "SPGI", "MCO", "SOFI",
        # Defense (genuine diversifier)
        "LMT", "RTX",
        # Payments (genuine diversifier)
        "V", "MA",
        # Consumer (genuine diversifier)
        "COST", "HD",
        # Industrials (genuine diversifier)
        "CAT", "ROP",
        # Utilities (genuine diversifier)
        "NEE",
    ],
    # GICS Sector SPDRs — textbook TTM Squeeze application. ETFs absorb single-
    # stock noise, consolidate cleanly during sector rotation, and tend to have
    # well-defined breakout patterns.
    "sector_etfs": [
        "XLF",   # Financials
        "XLE",   # Energy
        "XLU",   # Utilities
        "XLV",   # Healthcare
        "XLI",   # Industrials
        "XLK",   # Technology
        "XLP",   # Consumer Staples
        "XLY",   # Consumer Discretionary
        "XLB",   # Materials
        "XLRE",  # Real Estate
        "XLC",   # Communications
    ],
    # Defensive mega-caps — slow-movers, genuine consolidators, low directional
    # drift between earnings. Pure thesis test for "tight coil → breakout".
    "defensive_megacaps": [
        "KO", "PEP", "PG", "CL", "KMB",     # Staples
        "JNJ", "PFE", "MRK",                # Healthcare
        "MCD", "WMT", "COST",               # Consumer
        "T", "VZ",                          # Telecom
        "MO", "SO", "DUK",                  # Tobacco / Utilities
    ],
    # REITs — known coilers around interest-rate cycles. Range-bound until
    # policy or earnings shifts; ideal squeeze candidates per literature.
    "reits": [
        "O",     # Realty Income
        "PLD",   # Prologis
        "AMT",   # American Tower
        "CCI",   # Crown Castle
        "EQIX",  # Equinix
        "SPG",   # Simon Property
        "VICI",  # VICI Properties
        "WELL",  # Welltower
        "DLR",   # Digital Realty
        "AVB",   # AvalonBay
    ],
}


def render_sweep_summary(rows: list[tuple[str, dict[str, float]]]) -> str:
    lines = [
        "### Parameter sweep — aggregate (held-constant universe & dates)",
        "",
        "| Variant | MeanRet | MeanCAGR | MeanShp | MeanDD | Trades | TradedSyms | WinRate |",
        "|---------|--------:|--------:|--------:|--------:|-------:|----------:|--------:|",
    ]
    for label, m in rows:
        lines.append(
            f"| {label:<40} | "
            f"{m['mean_return']*100:>+6.1f}% | "
            f"{m['mean_cagr']*100:>+5.1f}% | "
            f"{m['mean_sharpe']:>+6.2f} | "
            f"{m['mean_dd']*100:>+5.1f}% | "
            f"{int(m['total_trades']):>5} | "
            f"{int(m['n_symbols_traded']):>9} | "
            f"{m['win_rate']*100:>5.1f}% |"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
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
        help="Override BOLLINGER_WATCHLIST with this symbol list",
    )
    parser.add_argument(
        "--universe", type=str, default=None, choices=sorted(UNIVERSES.keys()),
        help="Use a predefined universe by name. Overrides --symbols and "
             "BOLLINGER_WATCHLIST. See UNIVERSES dict for definitions.",
    )
    parser.add_argument(
        "--no-filter", action="store_true",
        help="Run with NO edge filter (raw signal). Useful for comparing edge-filter impact.",
    )
    parser.add_argument(
        "--with-and-without-filter", action="store_true",
        help="Run BOTH variants and print both tables for comparison.",
    )
    parser.add_argument(
        "--sweep", action="store_true",
        help="Run a parameter sweep — fetches bars ONCE then runs every variant "
             "in SWEEP_GRID against the same data. All other settings (universe, "
             "dates, slippage, feed, filter) are held constant.",
    )
    parser.add_argument(
        "--charts", action="store_true",
        help="Save per-symbol equity/drawdown charts under logs/backtests/squeeze/",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Optional markdown file path to write the report",
    )
    parser.add_argument(
        "--atr-stop-mult", type=float, default=None,
        help="If set, simulate a per-trade ATR-based stop-loss at entry - mult * ATR. "
             "Mirrors the production engine's ATR_STOP_MULTIPLIER (production default 2.0).",
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
        symbols = args.symbols or list(settings.BOLLINGER_WATCHLIST)
    bars_by_sym = fetch_bars(symbols, args.years, end_date=end_date)
    if not bars_by_sym:
        logger.error("No bars fetched — aborting.")
        return 1

    cfg = BacktestConfig()
    chart_dir = Path("logs/backtests/squeeze")

    # Pin window across runs for reproducibility:
    bar_start = min(df.index[0] for df in bars_by_sym.values())
    bar_end   = max(df.index[-1] for df in bars_by_sym.values())

    sections: list[str] = []
    universe_label = args.universe or "(custom)"
    sections.append(
        f"# Bollinger Squeeze Backtest — {universe_label}\n\n"
        f"- Generated: {datetime.now(timezone.utc).isoformat()}\n"
        f"- Bars range: {bar_start.date()} → {bar_end.date()} ({args.years}y nominal)\n"
        f"- Universe: {universe_label} — {len(bars_by_sym)} of {len(symbols)} symbols\n"
        f"- Symbols: {', '.join(sorted(bars_by_sym.keys()))}\n"
        f"- Data feed: {settings.ALPACA_DATA_FEED}\n"
        f"- Slippage: {cfg.slippage_bps} bps, init_cash: ${cfg.initial_cash:,.0f}\n"
        f"- Edge filter: {'ON' if not args.no_filter else 'OFF'}\n"
    )

    if args.sweep:
        sections.append(
            "**Sweep mode — held constant across all variants:** "
            "universe, bar range, init_cash, slippage, feed, filter setting.\n"
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
                        save_chart=False,
                        chart_dir=chart_dir,
                        strategy_params=params,
                        atr_stop_mult=args.atr_stop_mult,
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

    variants: list[tuple[str, bool]]
    if args.with_and_without_filter:
        variants = [("With edge filter (default)", True), ("Raw signal — no filter", False)]
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
                    save_chart=args.charts,
                    chart_dir=chart_dir,
                    atr_stop_mult=args.atr_stop_mult,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{symbol}: backtest failed — {exc}")
                continue
            reports.append(rep)

        # Sort by Sharpe descending for readability.
        reports.sort(key=lambda r: r.sharpe if pd.notna(r.sharpe) else -1e9, reverse=True)

        sections.append(render_table(reports, header))
        sections.append(aggregate_summary(reports))

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
