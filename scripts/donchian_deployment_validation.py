"""Run the pre-registered PLAN 11.74 Donchian deployment comparison."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.donchian_portfolio_sim import PortfolioResult, simulate_portfolio
from config.settings import DONCHIAN_TARGET_POOL_SIZE, DONCHIAN_WATCHLIST
from scripts.backtest_bollinger_squeeze import UNIVERSES
from scripts.donchian_parameter_rebaseline import load_cached_sip_bars
from scripts.donchian_trail_compare import classify_spy_regime


@dataclass(frozen=True)
class Variant:
    label: str
    entry_window: int
    exit_window: int
    channel_mode: Literal["close", "high_low"]


VARIANTS = (
    Variant("current close 30/15", 30, 15, "close"),
    Variant("classic high/low 20/10", 20, 10, "high_low"),
    Variant("classic high/low 55/20", 55, 20, "high_low"),
)
YEARS = tuple(range(2017, 2026))


def _run(
    bars: dict[str, pd.DataFrame],
    regime: pd.Series,
    symbols: list[str],
    variant: Variant,
    start: str,
    end: str,
) -> PortfolioResult:
    return simulate_portfolio(
        bars,
        regime,
        symbols,
        entry_window=variant.entry_window,
        exit_window=variant.exit_window,
        channel_mode=variant.channel_mode,
        heat_cap_pct=0.016,
        trade_start=pd.Timestamp(start, tz="UTC"),
        trade_end=pd.Timestamp(end, tz="UTC"),
    )


def _cluster_count(result: PortfolioResult) -> int:
    """Count entry bursts separated by more than five business sessions."""
    dates = sorted({trade.entry_date.date() for trade in result.trades})
    if not dates:
        return 0
    clusters = 1
    previous = dates[0]
    for current in dates[1:]:
        if np.busday_count(previous, current) > 5:
            clusters += 1
        previous = current
    return clusters


def _top_contributor(result: PortfolioResult) -> tuple[str | None, float | None]:
    pnl_by_symbol: dict[str, float] = {}
    for trade in result.trades:
        pnl_by_symbol[trade.symbol] = pnl_by_symbol.get(trade.symbol, 0.0) + (
            (trade.exit_price - trade.entry_price) * trade.quantity
        )
    if not pnl_by_symbol:
        return None, None
    symbol = max(pnl_by_symbol, key=pnl_by_symbol.get)
    positive = sum(max(value, 0.0) for value in pnl_by_symbol.values())
    share = pnl_by_symbol[symbol] / positive if positive > 0 else None
    return symbol, share


def _fmt_pct(value: float) -> str:
    return f"{100 * value:+.1f}%"


def render_report(
    universes: dict[str, list[str]],
    full: dict[tuple[str, str], PortfolioResult],
    yearly: dict[tuple[str, str, int], PortfolioResult],
    without_top: dict[tuple[str, str], PortfolioResult],
) -> str:
    lines = [
        "# Donchian Deployment Validation Result",
        "",
        "**Generated:** 2026-09-21",
        "",
        "> Historical fixed-cohort sensitivity only. Forward paper evidence has higher authority.",
        "",
        "The 2021–2025 years were inspected by `11.73`; they are fixed comparison folds here, not a newly untouched holdout. Production remains close-based 30/15.",
        "",
        "## Common-period portfolio results (2017–2025)",
        "",
        "Every cell enforces the pre-registered 4R heat cap, including pending DAY-entry reservations. Production currently runs this cap in observation-only mode (`STRATEGY_HEAT_CAP_ENFORCED=False`), so the classic results are conditional on a separately approved enforcement change.",
        "",
        "| Universe | Variant | Return | Sharpe | Max DD | Trades | Entry clusters | Mean R | Capacity skips | Heat skips |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for universe_name in universes:
        for variant in VARIANTS:
            result = full[(universe_name, variant.label)]
            stats = result.stats
            lines.append(
                f"| {universe_name} | {variant.label} | "
                f"{_fmt_pct(stats['return'])} | {stats['sharpe']:+.2f} | "
                f"{_fmt_pct(stats['max_drawdown'])} | {int(stats['trades'])} | "
                f"{_cluster_count(result)} | {stats['mean_r']:+.2f}R | "
                f"{result.skipped_capacity} | {result.skipped_heat} |"
            )

    lines += [
        "",
        "## Fixed annual comparison",
        "",
        "| Universe | Year | Current close 30/15 | Classic high/low 20/10 | Classic high/low 55/20 |",
        "|---|---:|---:|---:|---:|",
    ]
    for universe_name in universes:
        for year in YEARS:
            cells = []
            for variant in VARIANTS:
                stats = yearly[(universe_name, variant.label, year)].stats
                cells.append(
                    f"{_fmt_pct(stats['return'])} / {stats['sharpe']:+.2f} / "
                    f"{int(stats['trades'])} trades"
                )
            lines.append(
                f"| {universe_name} | {year} | " + " | ".join(cells) + " |"
            )

    lines += [
        "",
        "## Contributor concentration",
        "",
        "The no-top-symbol column is a full rerun after removing that variant's largest realized contributor; it is not simple subtraction.",
        "",
        "| Universe | Variant | Top contributor | Share of positive P&L | Return without top contributor |",
        "|---|---|---|---:|---:|",
    ]
    for universe_name, symbols in universes.items():
        for variant in VARIANTS:
            result = full[(universe_name, variant.label)]
            symbol, share = _top_contributor(result)
            sensitivity = without_top[(universe_name, variant.label)]
            lines.append(
                f"| {universe_name} | {variant.label} | {symbol or 'unavailable'} | "
                f"{100 * share:.1f}% | {_fmt_pct(sensitivity.stats['return'])} |"
                if share is not None
                else f"| {universe_name} | {variant.label} | unavailable | unavailable | unavailable |"
            )

    lines += [
        "",
        "## Exit-reason mix",
        "",
        "| Universe | Variant | Protective gap | Protective intraday | Close signal | Channel gap | Channel intraday | Fold end |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    reasons = (
        "stop_gap", "stop_intrabar", "signal", "channel_gap",
        "channel_intrabar", "eod",
    )
    for universe_name in universes:
        for variant in VARIANTS:
            result = full[(universe_name, variant.label)]
            observed = [trade.exit_reason for trade in result.trades]
            denominator = max(len(observed), 1)
            cells = [f"{100 * observed.count(reason) / denominator:.1f}%" for reason in reasons]
            lines.append(
                f"| {universe_name} | {variant.label} | " + " | ".join(cells) + " |"
            )

    lines += [
        "",
        "## Interpretation boundary",
        "",
        "- `ai_bigtech_32` preserves the original operator-supplied membership and list order; `durable_100` preserves the promoted liquidity ranking. Capacity and heat therefore reflect each deployment's actual deterministic ordering.",
        "- The universes are present-day frozen cohorts, not point-in-time membership histories. Survivorship and selection bias prevent an unbiased absolute-return claim.",
        "- Earnings blackout remains omitted because trustworthy point-in-time history is unavailable.",
        "- Daily OHLC cannot resolve every intraday path when several levels trade in one session; the simulator applies the documented deterministic ordering.",
        "- STOP_LIMIT quantity uses the production worst-limit-to-reference-stop distance; post-fill protection then re-anchors to fill minus 2 ATR.",
        "- The original pre-registration (`997e247`) named deterministic liquidity order and fill-anchored stops. The result commit (`becc25b`) amended those terms for parity: preserve each frozen universe's actual order and size from the worst limit to the reference-anchored stop. The amendments were applied uniformly, but were not part of the original frozen wording.",
        "- Production does not currently enforce the 4R heat cap. Because the cap rejected thousands of classic candidates while never binding current close-based 30/15 here, any classic paper experiment must separately authorize enforcement or these modeled classic results do not describe its behavior.",
        "- Allocator stretch and cross-sleeve competition are omitted. The 12% Donchian baseline is a conservative isolated deployment boundary.",
        "- These results can motivate a separately reviewed paper cohort. They cannot change the current 30/15 configuration, satisfy the 25-exit requirement, or authorize live graduation.",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        default="docs/reports/donchian_deployment_validation_latest.md",
    )
    args = parser.parse_args()

    universes = {
        "ai_bigtech_32": list(UNIVERSES["ai_bigtech"]),
        "durable_100": list(DONCHIAN_WATCHLIST[:DONCHIAN_TARGET_POOL_SIZE]),
    }
    all_symbols = sorted({symbol for symbols in universes.values() for symbol in symbols})
    bars: dict[str, pd.DataFrame] = {}
    for symbol in all_symbols:
        frame = load_cached_sip_bars(symbol)
        if frame is None or frame.empty:
            raise RuntimeError(f"missing warmed SIP cache for {symbol}")
        bars[symbol] = frame
    spy = load_cached_sip_bars("SPY")
    if spy is None or spy.empty:
        raise RuntimeError("missing warmed SIP cache for SPY")
    regime = classify_spy_regime(spy)

    full: dict[tuple[str, str], PortfolioResult] = {}
    yearly: dict[tuple[str, str, int], PortfolioResult] = {}
    without_top: dict[tuple[str, str], PortfolioResult] = {}
    for universe_name, symbols in universes.items():
        for variant in VARIANTS:
            result = _run(
                bars, regime, symbols, variant, "2017-01-01", "2025-12-31"
            )
            full[(universe_name, variant.label)] = result
            for year in YEARS:
                yearly[(universe_name, variant.label, year)] = _run(
                    bars,
                    regime,
                    symbols,
                    variant,
                    f"{year}-01-01",
                    f"{year}-12-31",
                )
            top_symbol, _ = _top_contributor(result)
            reduced = [symbol for symbol in symbols if symbol != top_symbol]
            without_top[(universe_name, variant.label)] = _run(
                bars, regime, reduced, variant, "2017-01-01", "2025-12-31"
            )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_report(universes, full, yearly, without_top), encoding="utf-8"
    )
    print(f"Wrote {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
