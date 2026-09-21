"""Run the pre-registered PLAN 11.73 Donchian fixed-cohort comparison."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from backtest.donchian_portfolio_sim import PortfolioResult, simulate_portfolio
from config.settings import DONCHIAN_TARGET_POOL_SIZE, DONCHIAN_WATCHLIST
from scripts.donchian_trail_compare import classify_spy_regime, load_bars

VARIANTS = ((20, 10), (30, 10), (30, 15), (55, 20))
HOLDOUT_YEARS = tuple(range(2021, 2026))


def load_cached_sip_bars(symbol: str) -> pd.DataFrame | None:
    """Load the already-warmed SIP daily cache without provider refreshes."""
    path = Path("data/historical/sip") / f"{symbol.upper()}_1Day_all.parquet"
    if not path.exists():
        return None
    frame = pd.read_parquet(path).sort_index()
    required = ["open", "high", "low", "close", "volume"]
    if not set(required).issubset(frame.columns):
        return None
    frame = frame[required].dropna()
    if frame.index.tz is None:
        frame.index = frame.index.tz_localize("UTC")
    return frame


def _run(
    bars: dict[str, pd.DataFrame], regime: pd.Series, symbols: list[str],
    pair: tuple[int, int], start: str, end: str,
) -> PortfolioResult:
    return simulate_portfolio(
        bars, regime, symbols, entry_window=pair[0], exit_window=pair[1],
        trade_start=pd.Timestamp(start, tz="UTC"), trade_end=pd.Timestamp(end, tz="UTC"),
    )


def _fmt(result: PortfolioResult) -> str:
    s = result.stats
    return (
        f"{100*s['return']:+.1f}%<br>{s['sharpe']:+.2f}<br>"
        f"{100*s['max_drawdown']:.1f}%<br>{int(s['trades'])}<br>"
        f"{100*s['win_rate']:.1f}%<br>{s['mean_r']:+.2f}R<br>"
        f"{result.skipped_capacity}"
    )


def _fmt_inline(result: PortfolioResult) -> str:
    s = result.stats
    return (
        f"return {100*s['return']:+.1f}%, Sharpe {s['sharpe']:+.2f}, "
        f"max DD {100*s['max_drawdown']:.1f}%, {int(s['trades'])} trades, "
        f"win rate {100*s['win_rate']:.1f}%, mean {s['mean_r']:+.2f}R, "
        f"{result.skipped_capacity} capacity skips"
    )


def select_variant(training: dict[tuple[int, int], PortfolioResult]) -> tuple[int, int]:
    """Apply the pre-registered 0.02-Sharpe tie and defensive tie-breaks."""
    best_sharpe = max(result.stats["sharpe"] for result in training.values())
    eligible = [
        pair for pair, result in training.items()
        if result.stats["sharpe"] >= best_sharpe - 0.02
    ]
    return max(
        eligible,
        key=lambda pair: (
            training[pair].stats["max_drawdown"], pair[0], pair[1]
        ),
    )


def _stitched_stats(results: list[PortfolioResult]) -> dict[str, float]:
    daily = pd.concat([r.equity.pct_change().dropna() for r in results])
    equity = (1.0 + daily).cumprod()
    dd = equity / equity.cummax() - 1.0
    trades = [t for r in results for t in r.trades]
    return {
        "return": float(equity.iloc[-1] - 1.0),
        "sharpe": float(daily.mean() / daily.std() * np.sqrt(252)) if daily.std() > 0 else 0.0,
        "max_drawdown": float(dd.min()),
        "trades": float(len(trades)),
        "mean_r": float(np.mean([t.r_multiple for t in trades])) if trades else 0.0,
    }


def render_report(
    folds: dict[tuple[int, int], dict[int, PortfolioResult]],
    selected: dict[int, tuple[int, int]],
    training: dict[int, dict[tuple[int, int], PortfolioResult]],
    shadow: dict[tuple[int, int], PortfolioResult],
    coverage: dict[str, tuple[object, object, int]],
    sensitivity: dict[str, object],
) -> str:
    lines = [
        "# Donchian Parameter Rebaseline Result", "",
        "**Generated:** 2026-09-21", "",
        "> Fixed-current-cohort temporal study; not a survivorship-free universe backtest.", "",
        "## Held-out annual folds", "",
        "Values: return | Sharpe | max drawdown | trades | win rate | mean R | capacity skips.", "",
        "| Year | 20/10 | 30/10 | 30/15 control | 55/20 | Expanding-history selection |",
        "|---:|---|---|---|---|---|",
    ]
    for year in HOLDOUT_YEARS:
        cells = [_fmt(folds[p][year]) for p in VARIANTS]
        pick = selected[year]
        lines.append(f"| {year} | " + " | ".join(cells) + f" | {pick[0]}/{pick[1]} |")

    lines += ["", "## Stitched held-out results", "",
              "| Variant | Return | Sharpe | Max drawdown | Trades | Mean R |",
              "|---|---:|---:|---:|---:|---:|"]
    for pair in VARIANTS:
        s = _stitched_stats([folds[pair][y] for y in HOLDOUT_YEARS])
        lines.append(
            f"| {pair[0]}/{pair[1]} | {100*s['return']:+.1f}% | {s['sharpe']:+.2f} | "
            f"{100*s['max_drawdown']:.1f}% | {int(s['trades'])} | {s['mean_r']:+.2f}R |"
        )
    chosen = _stitched_stats([folds[selected[y]][y] for y in HOLDOUT_YEARS])
    lines.append(
        f"| Expanding selection | {100*chosen['return']:+.1f}% | {chosen['sharpe']:+.2f} | "
        f"{100*chosen['max_drawdown']:.1f}% | {int(chosen['trades'])} | {chosen['mean_r']:+.2f}R |"
    )
    lines += ["", "## Held-out exit-reason mix", "",
              "| Variant | Stop gap | Intrabar stop | Signal | Fold-end close |",
              "|---|---:|---:|---:|---:|"]
    for pair in VARIANTS:
        reasons = [t.exit_reason for y in HOLDOUT_YEARS for t in folds[pair][y].trades]
        total = max(len(reasons), 1)
        lines.append(
            f"| {pair[0]}/{pair[1]} | {100*reasons.count('stop_gap')/total:.1f}% | "
            f"{100*reasons.count('stop_intrabar')/total:.1f}% | "
            f"{100*reasons.count('signal')/total:.1f}% | {100*reasons.count('eod')/total:.1f}% |"
        )
    control = _stitched_stats([folds[(30, 15)][y] for y in HOLDOUT_YEARS])
    challenger = _stitched_stats([folds[(55, 20)][y] for y in HOLDOUT_YEARS])
    years_won = sum(
        folds[(55, 20)][year].stats["return"] > folds[(30, 15)][year].stats["return"]
        for year in HOLDOUT_YEARS
    )
    sharpe_edge = challenger["sharpe"] - control["sharpe"]
    dd_difference = challenger["max_drawdown"] - control["max_drawdown"]
    c1 = years_won >= 4
    c2 = sharpe_edge >= 0.15
    c3 = dd_difference >= -0.03
    c4 = challenger["mean_r"] > 0
    c5 = bool(sensitivity["year_favorable"] and sensitivity["symbol_favorable"])
    passed = c1 and c2 and c3 and c4 and c5
    lines += ["", "## Pre-registered verdict", "",
              f"The strongest challenger, 55/20, beat 30/15 on return in **{years_won} of 5** held-out years; the required bar was 4 of 5.", "",
              f"Its stitched Sharpe advantage was **{sharpe_edge:+.2f}** (required at least +0.15), maximum-drawdown difference was **{100*dd_difference:+.1f} percentage points** (must not be worse by more than 3), and mean R was **{challenger['mean_r']:+.2f}R**.", "",
              "### Concentration sensitivities", "",
              f"- Remove-best-year: excluded {sensitivity['best_year']}. The remaining stitched return was {100*float(sensitivity['year_challenger_return']):+.1f}% for 55/20 versus {100*float(sensitivity['year_control_return']):+.1f}% for 30/15 — **{'PASS' if sensitivity['year_favorable'] else 'FAIL'}**.",
              f"- Remove-best-symbol: excluded {sensitivity['best_symbol']}, the largest realized 55/20 P&L contributor. The rerun return was {100*float(sensitivity['symbol_challenger_return']):+.1f}% for 55/20 versus {100*float(sensitivity['symbol_control_return']):+.1f}% for 30/15 — **{'PASS' if sensitivity['symbol_favorable'] else 'FAIL'}**.", "",
              f"Criteria: C1 {'PASS' if c1 else 'FAIL'}, C2 {'PASS' if c2 else 'FAIL'}, C3 {'PASS' if c3 else 'FAIL'}, C4 {'PASS' if c4 else 'FAIL'}, C5 {'PASS' if c5 else 'FAIL'}.", ""]
    if passed:
        lines.append(
            "**Decision: the backtest authorizes proposing 55/20 for a separate paper-configuration review; it does not authorize promotion or override forward evidence.**"
        )
    else:
        lines.append(
            "**Decision: retain 30/15.** At least one mandatory pre-registered criterion failed; do not salvage 55/20 by changing the rule after seeing the result."
        )
    lines += ["", "## Selection audit", ""]
    for year in HOLDOUT_YEARS:
        scores = ", ".join(
            f"{p[0]}/{p[1]}={training[year][p].stats['sharpe']:+.2f}" for p in VARIANTS
        )
        lines.append(f"- {year}: selected {selected[year][0]}/{selected[year][1]} from prior-history Sharpe ({scores}).")
    lines += ["", "## Partial 2026 shadow (non-decision)", ""]
    for pair in VARIANTS:
        lines.append(f"- {pair[0]}/{pair[1]}: {_fmt_inline(shadow[pair])}")
    lines += ["", "## Coverage and limitations", "",
              f"- Frozen ranked pool: {len(coverage)} symbols; lifecycle-only SPCX excluded.",
              "- Production parity includes the allocator's pre-sizing $100 minimum remaining-sleeve-capacity check. The first published draft omitted it; review showed that residual-capacity handling materially changes headline metrics, so these estimates support the no-change decision rather than precise expected returns.",
              "- Coverage is listing/provider dependent; no pre-listing history is fabricated.",
              "- Earnings blackout is unmodeled; current-cohort survivorship and selection bias remain.",
              "- See `docs/donchian_parameter_rebaseline.md` for the frozen contract and decision rule.", ""]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="docs/reports/donchian_parameter_rebaseline_latest.md")
    parser.add_argument(
        "--cache-only", action="store_true",
        help="require the warmed SIP parquet cohort; never refresh from Alpaca",
    )
    args = parser.parse_args()
    symbols = list(DONCHIAN_WATCHLIST[:DONCHIAN_TARGET_POOL_SIZE])
    bars: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        frame = load_cached_sip_bars(symbol) if args.cache_only else load_bars(symbol)
        if frame is not None and not frame.empty:
            bars[symbol] = frame
    missing = [symbol for symbol in symbols if symbol not in bars]
    if missing:
        raise RuntimeError(f"Donchian cohort incomplete ({len(bars)}/100); missing={missing}")
    spy = load_cached_sip_bars("SPY") if args.cache_only else load_bars("SPY")
    if spy is None or spy.empty:
        raise RuntimeError("SPY bars unavailable")
    regime = classify_spy_regime(spy)
    folds = {p: {} for p in VARIANTS}
    training: dict[int, dict[tuple[int, int], PortfolioResult]] = {}
    selected: dict[int, tuple[int, int]] = {}
    for year in HOLDOUT_YEARS:
        training[year] = {
            p: _run(bars, regime, symbols, p, "2017-01-01", f"{year-1}-12-31") for p in VARIANTS
        }
        selected[year] = select_variant(training[year])
        for pair in VARIANTS:
            folds[pair][year] = _run(
                bars, regime, symbols, pair, f"{year}-01-01", f"{year}-12-31"
            )
    today = datetime.now(timezone.utc).date().isoformat()
    shadow = {p: _run(bars, regime, symbols, p, "2026-01-01", today) for p in VARIANTS}
    control_pair = (30, 15)
    challenger_pair = (55, 20)
    best_year = max(
        HOLDOUT_YEARS,
        key=lambda year: (
            folds[challenger_pair][year].stats["return"]
            - folds[control_pair][year].stats["return"]
        ),
    )
    remaining_years = [year for year in HOLDOUT_YEARS if year != best_year]
    year_control = _stitched_stats([folds[control_pair][y] for y in remaining_years])
    year_challenger = _stitched_stats([folds[challenger_pair][y] for y in remaining_years])
    challenger_trades = [
        trade for year in HOLDOUT_YEARS for trade in folds[challenger_pair][year].trades
    ]
    pnl_by_symbol: dict[str, float] = {}
    for trade in challenger_trades:
        pnl_by_symbol[trade.symbol] = pnl_by_symbol.get(trade.symbol, 0.0) + (
            (trade.exit_price - trade.entry_price) * trade.quantity
        )
    best_symbol = max(pnl_by_symbol, key=pnl_by_symbol.get)
    without_best = [symbol for symbol in symbols if symbol != best_symbol]
    symbol_results = {
        pair: [
            _run(bars, regime, without_best, pair, f"{year}-01-01", f"{year}-12-31")
            for year in HOLDOUT_YEARS
        ]
        for pair in (control_pair, challenger_pair)
    }
    symbol_control = _stitched_stats(symbol_results[control_pair])
    symbol_challenger = _stitched_stats(symbol_results[challenger_pair])
    sensitivity: dict[str, object] = {
        "best_year": best_year,
        "year_control_return": year_control["return"],
        "year_challenger_return": year_challenger["return"],
        "year_favorable": year_challenger["return"] > year_control["return"],
        "best_symbol": best_symbol,
        "symbol_control_return": symbol_control["return"],
        "symbol_challenger_return": symbol_challenger["return"],
        "symbol_favorable": symbol_challenger["return"] > symbol_control["return"],
    }
    coverage = {s: (df.index.min(), df.index.max(), len(df)) for s, df in bars.items()}
    Path(args.output).write_text(
        render_report(folds, selected, training, shadow, coverage, sensitivity)
    )
    print(f"wrote {args.output}; usable bars {len(bars)}/{len(symbols)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
