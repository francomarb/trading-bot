"""Run the pre-registered PLAN 11.73 Donchian fixed-cohort comparison."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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


def sensitivity_favorable(
    sensitivity: dict[str, object], metric: str,
) -> bool:
    """Return whether both concentration checks favor the challenger."""
    if metric not in {"return", "sharpe"}:
        raise ValueError(f"unsupported sensitivity metric: {metric}")
    return bool(
        float(sensitivity[f"year_challenger_{metric}"])
        > float(sensitivity[f"year_control_{metric}"])
        and float(sensitivity[f"symbol_challenger_{metric}"])
        > float(sensitivity[f"symbol_control_{metric}"])
    )


def render_report(
    folds: dict[tuple[int, int], dict[int, PortfolioResult]],
    selected: dict[int, tuple[int, int]],
    training: dict[int, dict[tuple[int, int], PortfolioResult]],
    shadow: dict[tuple[int, int], PortfolioResult],
    coverage: dict[str, tuple[object, object, int]],
    sensitivities: dict[tuple[int, int], dict[str, object]],
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
    control_pair = (30, 15)
    control = _stitched_stats([folds[control_pair][y] for y in HOLDOUT_YEARS])
    lines += [
        "", "## Frozen implemented verdict and metric ambiguity", "",
        "Every challenger is evaluated against the same conjunctive rule; the report does not choose one challenger after viewing the results. Criterion 5 was pre-registered as remaining `directionally favorable` but did not name return or Sharpe. The implementation used return before the corrected 30/10 result existed. Both readings are disclosed below rather than retroactively choosing one.", "",
        "| Challenger | Years won | Sharpe edge | DD difference | Mean R | Remove-best-year (return; Sharpe) | Remove-best-symbol (return; Sharpe) | Return-rule verdict |",
        "|---|---:|---:|---:|---:|---|---|---|",
    ]
    passing: list[tuple[int, int]] = []
    sharpe_passing: list[tuple[int, int]] = []
    for pair in VARIANTS:
        if pair == control_pair:
            continue
        challenger = _stitched_stats([folds[pair][y] for y in HOLDOUT_YEARS])
        years_won = sum(
            folds[pair][year].stats["return"]
            > folds[control_pair][year].stats["return"]
            for year in HOLDOUT_YEARS
        )
        sharpe_edge = challenger["sharpe"] - control["sharpe"]
        dd_difference = challenger["max_drawdown"] - control["max_drawdown"]
        sensitivity = sensitivities[pair]
        return_sensitivity_favorable = sensitivity_favorable(
            sensitivity, "return"
        )
        sharpe_sensitivity_favorable = sensitivity_favorable(
            sensitivity, "sharpe"
        )
        criteria = (
            years_won >= 4,
            sharpe_edge >= 0.15,
            dd_difference >= -0.03,
            challenger["mean_r"] > 0,
            return_sensitivity_favorable,
        )
        if all(criteria):
            passing.append(pair)
        if all((*criteria[:4], sharpe_sensitivity_favorable)):
            sharpe_passing.append(pair)
        lines.append(
            f"| {pair[0]}/{pair[1]} | {years_won}/5 | {sharpe_edge:+.2f} | "
            f"{100*dd_difference:+.1f}pp | {challenger['mean_r']:+.2f}R | "
            f"{sensitivity['best_year']}: "
            f"{100*float(sensitivity['year_challenger_return']):+.1f}% vs "
            f"{100*float(sensitivity['year_control_return']):+.1f}% "
            f"({'PASS' if sensitivity['year_return_favorable'] else 'FAIL'}); "
            f"{float(sensitivity['year_challenger_sharpe']):+.2f} vs "
            f"{float(sensitivity['year_control_sharpe']):+.2f} "
            f"({'PASS' if sensitivity['year_sharpe_favorable'] else 'FAIL'}) | "
            f"ex {sensitivity['best_symbol']}: "
            f"{100*float(sensitivity['symbol_challenger_return']):+.1f}% vs "
            f"{100*float(sensitivity['symbol_control_return']):+.1f}% "
            f"({'PASS' if sensitivity['symbol_return_favorable'] else 'FAIL'}); "
            f"{float(sensitivity['symbol_challenger_sharpe']):+.2f} vs "
            f"{float(sensitivity['symbol_control_sharpe']):+.2f} "
            f"({'PASS' if sensitivity['symbol_sharpe_favorable'] else 'FAIL'}) | "
            f"C1 {'P' if criteria[0] else 'F'}, C2 {'P' if criteria[1] else 'F'}, "
            f"C3 {'P' if criteria[2] else 'F'}, C4 {'P' if criteria[3] else 'F'}, "
            f"C5 {'P' if criteria[4] else 'F'} |"
        )
    lines.append("")
    if passing:
        labels = ", ".join(f"{pair[0]}/{pair[1]}" for pair in passing)
        lines.append(
            f"**Decision: {labels} cleared the historical proposal rule. This only authorizes discussing a separately reviewed paper cohort; it does not change the frozen 30/15 paper configuration, authorize promotion, or override forward evidence.**"
        )
    else:
        lines.append(
            "**Implemented return-rule decision: retain 30/15.** No challenger cleared every mandatory criterion under the return reading used by the frozen implementation."
        )
    sharpe_labels = ", ".join(
        f"{pair[0]}/{pair[1]}" for pair in sharpe_passing
    ) or "none"
    thirty_ten = sensitivities[(30, 10)]
    lines += [
        "",
        f"Under a Sharpe reading of criterion 5, the full-rule passers would be: **{sharpe_labels}**. For 30/10 specifically, remove-{thirty_ten['best_year']} Sharpe is {float(thirty_ten['year_challenger_sharpe']):+.2f} versus {float(thirty_ten['year_control_sharpe']):+.2f} for the control and the ex-{thirty_ten['best_symbol']} Sharpe is {float(thirty_ten['symbol_challenger_sharpe']):+.2f} versus {float(thirty_ten['symbol_control_sharpe']):+.2f}. The metric ambiguity cannot be resolved after seeing the result, so production remains 30/15 pending forward evidence. The historical direction nevertheless makes close-based 30/10 the leading candidate if a separately pre-registered paper experiment is later authorized.",
    ]
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
              "- Production parity includes the allocator's pre-sizing $100 minimum remaining-sleeve-capacity check and conservative STOP_LIMIT quantity from the worst permitted limit down to the pre-fill reference-anchored stop. Earlier drafts omitted the floor and then divided risk by only the post-fill 2 ATR protection distance; both corrections materially changed headline metrics, so these estimates are not precise forecasts of any variant's edge.",
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
    sensitivities: dict[tuple[int, int], dict[str, object]] = {}
    for challenger_pair in VARIANTS:
        if challenger_pair == control_pair:
            continue
        best_year = max(
            HOLDOUT_YEARS,
            key=lambda year: (
                folds[challenger_pair][year].stats["return"]
                - folds[control_pair][year].stats["return"]
            ),
        )
        remaining_years = [year for year in HOLDOUT_YEARS if year != best_year]
        year_control = _stitched_stats(
            [folds[control_pair][year] for year in remaining_years]
        )
        year_challenger = _stitched_stats(
            [folds[challenger_pair][year] for year in remaining_years]
        )
        challenger_trades = [
            trade
            for year in HOLDOUT_YEARS
            for trade in folds[challenger_pair][year].trades
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
                _run(
                    bars,
                    regime,
                    without_best,
                    pair,
                    f"{year}-01-01",
                    f"{year}-12-31",
                )
                for year in HOLDOUT_YEARS
            ]
            for pair in (control_pair, challenger_pair)
        }
        symbol_control = _stitched_stats(symbol_results[control_pair])
        symbol_challenger = _stitched_stats(symbol_results[challenger_pair])
        sensitivities[challenger_pair] = {
            "best_year": best_year,
            "year_control_return": year_control["return"],
            "year_challenger_return": year_challenger["return"],
            "year_control_sharpe": year_control["sharpe"],
            "year_challenger_sharpe": year_challenger["sharpe"],
            "year_return_favorable": year_challenger["return"] > year_control["return"],
            "year_sharpe_favorable": year_challenger["sharpe"] > year_control["sharpe"],
            "best_symbol": best_symbol,
            "symbol_control_return": symbol_control["return"],
            "symbol_challenger_return": symbol_challenger["return"],
            "symbol_control_sharpe": symbol_control["sharpe"],
            "symbol_challenger_sharpe": symbol_challenger["sharpe"],
            "symbol_return_favorable": symbol_challenger["return"] > symbol_control["return"],
            "symbol_sharpe_favorable": symbol_challenger["sharpe"] > symbol_control["sharpe"],
        }
    coverage = {s: (df.index.min(), df.index.max(), len(df)) for s, df in bars.items()}
    Path(args.output).write_text(
        render_report(folds, selected, training, shadow, coverage, sensitivities)
    )
    print(f"wrote {args.output}; usable bars {len(bars)}/{len(symbols)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
