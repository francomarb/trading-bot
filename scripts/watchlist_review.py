#!/usr/bin/env python3
"""
Watchlist Strategy Fitness Review — Lynch-inspired six-month checkup.

Evaluates each watchlist symbol against the requirements of each active
strategy, drawing on three fundamental checks from Lynch's *Beating the
Street* (1993):

  1. FCF positivity  — free cash flow > 0  (Ch. 15, Phelps Dodge analysis)
  2. Revenue growth  — trailing-12-month revenue grew YoY  (Ch. 21)
  3. Cash solvency   — profitable, OR unprofitable with adequate cash runway
                       (Golden Rules: "make sure it has the cash to pay the
                       medical bills")

Each strategy has a different requirement profile:
  - SMA Crossover (trend-following): FCF and revenue growth are required —
    deteriorating fundamentals undermine the trend signal.
  - RSI Reversion (mean-reversion): FCF and revenue growth are informational
    only — oversold / cash-burning setups are the entry thesis. Solvency is
    always required (shorter floor: 12 months).

Designed to be run every six months before any watchlist change and before the
Phase 10 live flip. Has zero impact on the running engine — read-only.

Usage:
    python scripts/watchlist_review.py
    python scripts/watchlist_review.py --output logs/watchlist_review_2026-04.md
    python scripts/watchlist_review.py --symbols AAPL MSFT RIVN
    python scripts/watchlist_review.py --strategy sma_crossover

Exit codes:
    0 — all symbols are GOOD FIT for all evaluated strategies
    1 — one or more symbols are POOR FIT, MARGINAL, or ERROR

Data source:  Yahoo Finance via yfinance (annual financials, ~1-quarter lag).
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

import pandas as pd
import yfinance as yf
from loguru import logger

from config import settings


# ── Constants ─────────────────────────────────────────────────────────────────

MIN_SMA_CASH_RUNWAY_MONTHS: int = 18
"""Unprofitable companies must have at least this many months of cash runway for SMA."""

MIN_RSI_CASH_RUNWAY_MONTHS: int = 12
"""Unprofitable companies must have at least this many months of cash runway for RSI."""


# ── Data classes ──────────────────────────────────────────────────────────────


@dataclass
class CheckProfile:
    """Strategy-specific check requirements."""

    strategy_name: str
    display_name: str
    fcf_required: bool       # if True, negative FCF → POOR FIT
    revenue_required: bool   # if True, declining revenue → POOR FIT
    min_cash_runway_months: int  # hard solvency floor (solvency is always required)


# Module-level profile constants
SMA_PROFILE = CheckProfile(
    strategy_name="sma_crossover",
    display_name="SMA Crossover",
    fcf_required=True,
    revenue_required=True,
    min_cash_runway_months=MIN_SMA_CASH_RUNWAY_MONTHS,
)
RSI_PROFILE = CheckProfile(
    strategy_name="rsi_reversion",
    display_name="RSI Reversion",
    fcf_required=False,   # negative FCF is informational — oversold on cash burn is the setup
    revenue_required=False,  # declining revenue is informational — factored into oversold price
    min_cash_runway_months=MIN_RSI_CASH_RUNWAY_MONTHS,
)
ALL_PROFILES: list[CheckProfile] = [SMA_PROFILE, RSI_PROFILE]


@dataclass
class SymbolFundamentals:
    """Raw fundamental snapshot for one symbol. No check booleans."""

    symbol: str

    # Raw metrics ($, not $M — raw yfinance values)
    fcf_annual: Optional[float] = None          # Annual free cash flow, $
    revenue_growth_pct: Optional[float] = None  # YoY revenue growth, %
    is_profitable: Optional[bool] = None        # Net income > 0
    cash_runway_months: Optional[float] = None  # Only set when unprofitable

    # Error message if the data fetch failed entirely
    error: Optional[str] = None


@dataclass
class StrategyFitness:
    """Per-strategy verdict for one symbol."""

    symbol: str
    strategy_name: str
    display_name: str
    fcf_required: bool
    revenue_required: bool
    min_cash_runway_months: int

    fcf_ok: Optional[bool] = None
    revenue_ok: Optional[bool] = None
    solvency_ok: Optional[bool] = None
    error: Optional[str] = None

    @property
    def verdict(self) -> str:
        """Compute verdict from check results and profile requirements."""
        if self.error:
            return "⚠️  ERROR"
        # Determine if any required check fails or solvency fails
        required_failed = False
        optional_failed = False

        # FCF check
        if self.fcf_ok is False:
            if self.fcf_required:
                required_failed = True
            else:
                optional_failed = True

        # Revenue check
        if self.revenue_ok is False:
            if self.revenue_required:
                required_failed = True
            else:
                optional_failed = True

        # Solvency is always required
        if self.solvency_ok is False:
            required_failed = True

        if required_failed:
            return "❌ POOR FIT"
        if optional_failed:
            return "⚠️  MARGINAL"
        return "✅ GOOD FIT"

    @staticmethod
    def _chk(ok: Optional[bool]) -> str:
        """Single-character check indicator."""
        if ok is True:
            return "✓"
        if ok is False:
            return "✗"
        return "–"


# ── Pure assessment function ──────────────────────────────────────────────────


def assess_fitness(
    fundamentals: SymbolFundamentals, profile: CheckProfile
) -> StrategyFitness:
    """
    Pure function: produce a StrategyFitness from raw fundamentals and a profile.

    No I/O. No side effects.
    """
    fitness = StrategyFitness(
        symbol=fundamentals.symbol,
        strategy_name=profile.strategy_name,
        display_name=profile.display_name,
        fcf_required=profile.fcf_required,
        revenue_required=profile.revenue_required,
        min_cash_runway_months=profile.min_cash_runway_months,
    )

    if fundamentals.error:
        fitness.error = fundamentals.error
        return fitness

    # FCF check
    if fundamentals.fcf_annual is not None:
        fitness.fcf_ok = fundamentals.fcf_annual > 0

    # Revenue growth check
    if fundamentals.revenue_growth_pct is not None:
        fitness.revenue_ok = fundamentals.revenue_growth_pct > 0.0

    # Solvency check
    if fundamentals.is_profitable is True:
        fitness.solvency_ok = True
    elif fundamentals.cash_runway_months is not None:
        fitness.solvency_ok = (
            fundamentals.cash_runway_months >= profile.min_cash_runway_months
        )

    return fitness


# ── Data fetching ─────────────────────────────────────────────────────────────


def _row_latest(df: pd.DataFrame, *keys: str) -> Optional[float]:
    """
    Return the most-recent non-null value for the first matching row key.

    yfinance DataFrames have metric names as the index and dates as columns,
    ordered most-recent first.  Returns the raw float value (dollars) or None
    if no matching key is found or all values are NaN.
    """
    for key in keys:
        if key in df.index:
            series = df.loc[key].dropna()
            if not series.empty:
                return float(series.iloc[0])
    return None


def fetch_fundamentals(symbol: str) -> SymbolFundamentals:
    """
    Fetch annual fundamentals from Yahoo Finance and return raw data.

    All monetary values are stored in raw dollars (as returned by yfinance).
    The report formatter converts to $M/$B for display.
    """
    result = SymbolFundamentals(symbol=symbol)
    try:
        ticker = yf.Ticker(symbol)
        cf = ticker.cashflow
        inc = ticker.income_stmt
        bs = ticker.balance_sheet

        # ── FCF ──────────────────────────────────────────────────────────────
        # Try "Free Cash Flow" directly; fall back to OCF + CapEx (capex is
        # negative in yfinance, so addition gives the correct sign).
        fcf = _row_latest(cf, "Free Cash Flow")
        if fcf is None:
            ocf = _row_latest(cf, "Operating Cash Flow")
            capex = _row_latest(cf, "Capital Expenditure")
            if ocf is not None and capex is not None:
                fcf = ocf + capex
        result.fcf_annual = fcf

        # ── Revenue growth ────────────────────────────────────────────────────
        if "Total Revenue" in inc.index:
            rev_series = inc.loc["Total Revenue"].dropna()
            if len(rev_series) >= 2:
                r_now = float(rev_series.iloc[0])
                r_prior = float(rev_series.iloc[1])
                if r_prior != 0:
                    result.revenue_growth_pct = (r_now - r_prior) / abs(r_prior) * 100

        # ── Solvency (cash runway for unprofitable companies) ─────────────────
        net_income = _row_latest(inc, "Net Income")
        cash = _row_latest(
            bs,
            "Cash Cash Equivalents And Short Term Investments",
            "Cash And Cash Equivalents",
        )

        if net_income is not None:
            result.is_profitable = net_income > 0
            if not result.is_profitable:
                # Unprofitable: compute cash runway
                annual_burn = abs(net_income)
                if cash is not None and annual_burn > 0:
                    result.cash_runway_months = (cash / annual_burn) * 12

    except Exception as exc:
        result.error = str(exc)
        logger.warning(f"{symbol}: fundamental data fetch error — {exc}")

    return result


# ── Report formatting ─────────────────────────────────────────────────────────


def _fmt_dollars(val: Optional[float]) -> str:
    """Format a raw dollar value as $XM or $XB."""
    if val is None:
        return "N/A"
    abs_val = abs(val)
    sign = "-" if val < 0 else ""
    if abs_val >= 1e9:
        return f"{sign}${abs_val / 1e9:.1f}B"
    return f"{sign}${abs_val / 1e6:.0f}M"


def _fmt_growth(val: Optional[float]) -> str:
    if val is None:
        return "N/A"
    return f"{val:+.1f}%"


def format_report(
    symbol_results: list[tuple[SymbolFundamentals, list[StrategyFitness]]],
) -> str:
    """Render the full markdown strategy fitness review report."""
    today = date.today().isoformat()

    # Determine which profiles are in use
    profiles_seen: list[tuple[str, str]] = []  # (strategy_name, display_name)
    if symbol_results:
        for fitness in symbol_results[0][1]:
            profiles_seen.append((fitness.strategy_name, fitness.display_name))

    lines: list[str] = [
        f"# Watchlist Strategy Fitness Review — {today}",
        "",
        "Lynch-inspired six-month checkup (*Beating the Street*, 1993).",
        "",
        "Three checks are applied per symbol:",
        "",
        "| Check | Criterion | Source |",
        "|-------|-----------|--------|",
        "| FCF positivity | Annual free cash flow > 0 | Ch. 15 |",
        "| Revenue growth | YoY revenue growth > 0% | Ch. 21 |",
        "| Cash solvency | Profitable, or adequate cash runway | Golden Rules |",
        "",
        "Strategy requirements differ:",
        "- **SMA Crossover**: FCF and revenue growth are *required* (failing → POOR FIT); "
        f"solvency floor: {MIN_SMA_CASH_RUNWAY_MONTHS} months.",
        "- **RSI Reversion**: FCF and revenue growth are *informational* (failing → MARGINAL); "
        f"solvency floor: {MIN_RSI_CASH_RUNWAY_MONTHS} months.",
        "",
        "> Data: Yahoo Finance annual financials (~1-quarter lag).",
        "> Rerun after each quarterly earnings season.",
        "",
        "---",
        "",
        "## Strategy Fitness Matrix",
        "",
    ]

    # Build matrix header
    header_cols = " | ".join(f"{dn}" for _, dn in profiles_seen)
    sep_cols = " | ".join(":-------------:" for _ in profiles_seen)
    lines.append(f"| Symbol | {header_cols} |")
    lines.append(f"|--------|{sep_cols}|")

    for fundamentals, fitness_list in symbol_results:
        verdict_cols = " | ".join(f" {f.verdict} " for f in fitness_list)
        lines.append(f"| {fundamentals.symbol:<6} |{verdict_cols}|")

    # ── Flagged symbols section ────────────────────────────────────────────────
    flagged = [
        (fund, fl)
        for fund, fl in symbol_results
        if any(f.verdict != "✅ GOOD FIT" for f in fl)
    ]

    if flagged:
        lines += ["", "---", "", "## Flagged Symbols", ""]
        lines.append(
            "Only symbols with at least one non-GOOD-FIT verdict appear here."
        )
        lines.append("")

        for fundamentals, fitness_list in flagged:
            lines.append(f"### {fundamentals.symbol}")
            lines.append("")

            for fitness in fitness_list:
                if fitness.verdict == "✅ GOOD FIT":
                    continue

                lines.append(f"**{fitness.display_name}: {fitness.verdict}**")

                if fitness.error:
                    lines.append(f"- ⚠️  Failed to fetch fundamental data: `{fitness.error}`")
                    lines.append(
                        "  Check that the ticker is valid and Yahoo Finance "
                        "has coverage for this symbol."
                    )
                else:
                    # FCF line
                    if fitness.fcf_ok is False:
                        fcf_str = _fmt_dollars(fundamentals.fcf_annual)
                        if fitness.fcf_required:
                            lines.append(
                                f"- FCF: {StrategyFitness._chk(False)} {fcf_str} "
                                "← required for this strategy"
                            )
                        else:
                            lines.append(
                                f"- FCF: {StrategyFitness._chk(False)} {fcf_str} "
                                "(informational — oversold on cash burn is the entry setup)"
                            )
                    elif fitness.fcf_ok is True:
                        lines.append(
                            f"- FCF: {StrategyFitness._chk(True)} "
                            f"{_fmt_dollars(fundamentals.fcf_annual)}"
                        )
                    else:
                        lines.append(f"- FCF: {StrategyFitness._chk(None)} N/A")

                    # Revenue growth line
                    if fitness.revenue_ok is False:
                        rev_str = _fmt_growth(fundamentals.revenue_growth_pct)
                        if fitness.revenue_required:
                            lines.append(
                                f"- Revenue Growth: {StrategyFitness._chk(False)} {rev_str} YoY "
                                "← required for this strategy"
                            )
                        else:
                            lines.append(
                                f"- Revenue Growth: {StrategyFitness._chk(False)} {rev_str} YoY "
                                "(informational — declining revenue is priced into the oversold condition)"
                            )
                    elif fitness.revenue_ok is True:
                        lines.append(
                            f"- Revenue Growth: {StrategyFitness._chk(True)} "
                            f"{_fmt_growth(fundamentals.revenue_growth_pct)} YoY"
                        )
                    else:
                        lines.append(f"- Revenue Growth: {StrategyFitness._chk(None)} N/A")

                    # Solvency line
                    if fitness.solvency_ok is True:
                        if fundamentals.is_profitable:
                            sol_str = "profitable"
                        elif fundamentals.cash_runway_months is not None:
                            sol_str = (
                                f"{fundamentals.cash_runway_months:.0f} months runway "
                                f"(≥ {fitness.min_cash_runway_months} required)"
                            )
                        else:
                            sol_str = "profitable"
                        lines.append(f"- Solvency: {StrategyFitness._chk(True)} {sol_str}")
                    elif fitness.solvency_ok is False:
                        if fundamentals.cash_runway_months is not None:
                            sol_str = (
                                f"{fundamentals.cash_runway_months:.0f} months runway "
                                f"(≥ {fitness.min_cash_runway_months} required)"
                            )
                        else:
                            sol_str = f"insufficient (≥ {fitness.min_cash_runway_months} required)"
                        lines.append(f"- Solvency: {StrategyFitness._chk(False)} {sol_str}")
                    else:
                        lines.append(f"- Solvency: {StrategyFitness._chk(None)} N/A")

                lines.append("")

    # ── Summary counts ─────────────────────────────────────────────────────────
    all_fitness = [f for _, fl in symbol_results for f in fl]
    good_count = sum(1 for f in all_fitness if f.verdict == "✅ GOOD FIT")
    poor_count = sum(1 for f in all_fitness if f.verdict == "❌ POOR FIT")
    marginal_count = sum(1 for f in all_fitness if f.verdict == "⚠️  MARGINAL")
    error_count = sum(1 for f in all_fitness if f.verdict == "⚠️  ERROR")

    lines += [
        "---",
        "",
        "## Results",
        "",
        f"- Symbols reviewed: {len(symbol_results)}",
        f"- Strategy assessments: {len(all_fitness)} "
        f"({len(symbol_results)} symbols × {len(profiles_seen)} strategies)",
        f"- ✅ GOOD FIT: {good_count}",
        f"- ⚠️  MARGINAL: {marginal_count}",
        f"- ❌ POOR FIT: {poor_count}",
        f"- ⚠️  ERROR: {error_count}",
        "",
        "## Legend",
        "",
        "- ✓ check passed  |  ✗ check failed  |  – data unavailable",
        "- ✅ GOOD FIT  = all required checks pass; optional failures are absent or N/A",
        "- ⚠️  MARGINAL  = no required check fails, but an optional check is False",
        "- ❌ POOR FIT  = a required check fails, or solvency fails",
        "- ⚠️  ERROR     = data fetch failed entirely",
        "",
        "## Recommended Actions",
        "",
        "- ❌ POOR FIT: remove from that strategy's watchlist before Phase 10",
        "- ⚠️  MARGINAL: acceptable for RSI reversion with reduced position size; "
        "do not add to SMA",
        "- ✅ GOOD FIT: no action required; recheck in 6 months",
    ]

    return "\n".join(lines)


# ── Orchestration ─────────────────────────────────────────────────────────────


def run_review(
    symbols: list[str],
    profiles: list[CheckProfile],
    output_path: Optional[Path] = None,
) -> list[tuple[SymbolFundamentals, list[StrategyFitness]]]:
    """
    Fetch fundamentals for all symbols, assess fitness for each profile,
    generate the report, and optionally save it. Always prints to stdout.
    """
    logger.info(
        f"watchlist review: fetching fundamentals for {len(symbols)} symbol(s) "
        f"against {len(profiles)} profile(s)"
    )
    results: list[tuple[SymbolFundamentals, list[StrategyFitness]]] = []
    for sym in symbols:
        logger.info(f"  → {sym}")
        fundamentals = fetch_fundamentals(sym)
        fitness_list = [assess_fitness(fundamentals, p) for p in profiles]
        results.append((fundamentals, fitness_list))

    report = format_report(results)

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(report, encoding="utf-8")
        logger.info(f"watchlist review: report saved → {output_path}")

    print(report)
    return results


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Watchlist strategy fitness review (Lynch six-month checkup).",
    )
    parser.add_argument(
        "--symbols",
        nargs="+",
        default=settings.WATCHLIST,
        metavar="SYM",
        help="Symbols to review (default: WATCHLIST from config/settings.py)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="FILE",
        help="Save markdown report to FILE (default: stdout only)",
    )
    parser.add_argument(
        "--strategy",
        choices=["sma_crossover", "rsi_reversion", "all"],
        default="all",
        help="Which strategy profiles to evaluate (default: all)",
    )
    args = parser.parse_args()

    # Resolve profiles
    if args.strategy == "sma_crossover":
        profiles = [SMA_PROFILE]
    elif args.strategy == "rsi_reversion":
        profiles = [RSI_PROFILE]
    else:
        profiles = ALL_PROFILES

    results = run_review(args.symbols, profiles, output_path=args.output)

    # Exit 1 if any non-GOOD-FIT verdict
    all_fitness = [f for _, fl in results for f in fl]
    not_good = sum(1 for f in all_fitness if f.verdict != "✅ GOOD FIT")
    sys.exit(0 if not_good == 0 else 1)


if __name__ == "__main__":
    main()
