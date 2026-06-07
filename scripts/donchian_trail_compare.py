#!/usr/bin/env python3
"""
Donchian trailing-stop comparison harness — PLAN P2 investigation.

Runs three protective-stop variants against the production Donchian Breakout
config (30/15 windows, ATR x2 initial stop) on the ai_bigtech universe across
multiple regime windows:

  (A) static_atr         — current production behavior; stop never moves
  (B) donchian_low_trail — stop ratchets up with rolling 15-day-low minus
                           0.5 x ATR wick buffer
  (C) chandelier         — stop ratchets up with HWM-close minus 3 x ATR

Output is a markdown report with per-window aggregate tables (mean total
return, mean Sharpe, mean MaxDD, total trades, exit-reason mix). The PLAN
question is whether (B) or (C) reduces giveback during gap-down-through-
rising-low scenarios without sacrificing too much in whipsaw on quieter
names.

Usage:
    venv/bin/python scripts/donchian_trail_compare.py
    venv/bin/python scripts/donchian_trail_compare.py \\
        --output logs/backtests/donchian_trail_compare.md

The script is read-only: never writes to settings, never modifies a slot,
never submits an order. It uses ONLY cached bars under data/historical/; if
a symbol lacks data for a window, that symbol is skipped for that window
and the report notes the coverage drop.
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

from backtest.donchian_trail_sim import (  # noqa: E402
    ChandelierStop,
    DonchianLowTrail,
    PortfolioAggregate,
    StaticATRStop,
    StopPolicy,
    SymbolResult,
    aggregate,
    simulate_symbol,
)
from indicators.technicals import add_adx, add_atr, add_sma  # noqa: E402
from scripts.backtest_bollinger_squeeze import UNIVERSES  # noqa: E402


# ── Regime windows ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RegimeWindow:
    name: str
    start: str  # YYYY-MM-DD
    end: str
    description: str


WINDOWS: list[RegimeWindow] = [
    RegimeWindow(
        name="2021_melt_up",
        start="2021-04-01",
        end="2021-12-31",
        description="2021 melt-up Q2-Q4 — quiet uptrend, tests trail whipsaw cost. "
                    "Window starts April so the 200-SMA filter has time to "
                    "populate (individual ai_bigtech stocks' first available "
                    "bar on the Alpaca IEX paper feed is 2020-07-27).",
    ),
    RegimeWindow(
        name="2022_bear",
        start="2022-01-01",
        end="2022-12-31",
        description="2022 bear — multi-month drawdown, staircase declines",
    ),
    RegimeWindow(
        name="2023_2024_ai_rally",
        start="2023-01-01",
        end="2024-12-31",
        description="2023-24 AI rally — deployment-target regime, large running winners",
    ),
    RegimeWindow(
        name="2021_2024_combined",
        start="2021-04-01",
        end="2024-12-31",
        description="Combined 2021-2024 — full sweep; per-year slice tables below",
    ),
]


# ── Stop policy variants ────────────────────────────────────────────────────


def build_policies() -> list[StopPolicy]:
    return [
        StaticATRStop(k=2.0),
        DonchianLowTrail(initial_k=2.0, buffer_atr=0.5),
        ChandelierStop(initial_k=2.0, k=3.0),
    ]


# ── Production-realistic entry gates ────────────────────────────────────────
#
# These mirror the live engine's combined "go" decision at bar t close. Without
# them the simulator trades every Donchian high regardless of macro regime or
# per-symbol structural strength, which is materially different from live
# behavior and inflates the trade count under all three stop variants. PR #49
# review flagged this; fixed here.
#
# What's modeled:
#   1. SPY regime — TRENDING only (live: StrategySlot.allowed_regimes =
#      {MarketRegime.TRENDING}). Per-bar classification replicates
#      regime.detector.RegimeDetector._classify() priority:
#        BEAR     → SPY close < SPY 200-SMA
#        VOLATILE → ATR%(14) percentile >= 90% AND absolute floor >= 1.2%
#        TRENDING → ADX(14) >= 25, OR (ADX in 20..25 AND SPY 50-SMA slope > 0)
#        RANGING  → otherwise
#   2. Stock structural strength — close > 200-SMA (DonchianEdgeFilter rule 1)
#   3. Liquidity — 20-day avg dollar volume >= $20M (DonchianEdgeFilter rule 3)
#
# What's NOT modeled (limitation, documented in the writeup):
#   - Earnings blackout (DonchianEdgeFilter rule 2) — needs offline earnings
#     calendar; production reads from yfinance/cache. Skipping it means we
#     allow a small fraction of trades that production would block; the
#     direction of the bias is the same across all three stop variants so it
#     doesn't favor one over the other.
#   - Sector momentum "warn" mode — doesn't block entries in production for
#     Donchian (it's a soft filter that only warns), so no effect.


def classify_spy_regime(spy: pd.DataFrame, *,
                        sma_long: int = 200, atr_window: int = 14,
                        adx_window: int = 14, sma_short: int = 50,
                        vol_pct_window: int = 126, vol_pct_threshold: float = 0.80,
                        vol_atr_pct_floor: float = 0.012,
                        adx_trend: float = 25.0, adx_range: float = 20.0,
                        sma_slope_bars: int = 5) -> pd.Series:
    """
    Per-bar SPY regime classification, replicating RegimeDetector._classify().
    Returns a Series of strings ('BEAR' | 'VOLATILE' | 'TRENDING' | 'RANGING')
    aligned to spy.index. NaN values during warmup are reported as 'RANGING'
    (the conservative default matching the live detector).

    Defaults track ``regime.detector.RegimeDetector`` exactly:
    vol_pct_window=126 (~6 mo), vol_pct_threshold=0.80, sma_slope_bars=5.
    Parity is exercised by
    ``tests/test_donchian_trail_sim.py::TestRegimeParity``.
    """
    df = add_sma(spy, sma_long)
    df = add_sma(df, sma_short)
    df = add_atr(df, atr_window)
    df = add_adx(df, adx_window)

    close = df["close"]
    sma_long_s = df[f"sma_{sma_long}"]
    sma_short_s = df[f"sma_{sma_short}"]
    atr_pct = df[f"atr_{atr_window}"] / close
    adx_s = df[f"adx_{adx_window}"]

    # Rolling ATR% percentile rank, matching RegimeDetector._classify exactly:
    # `(window_valid < current_atr_pct).mean()` where window_valid is the
    # current `vol_pct_window` slice including the current bar. The current
    # bar contributes False to the comparison (not strictly less than itself),
    # so the denominator is N (not N-1).
    atr_pct_rank = atr_pct.rolling(vol_pct_window, min_periods=10).apply(
        lambda w: (w < w[-1]).mean(),
        raw=True,
    )

    # 50-SMA slope: simple level diff over `sma_slope_bars`.
    sma_short_slope = sma_short_s.diff(sma_slope_bars)

    regimes = pd.Series("RANGING", index=df.index, dtype=object)

    # BEAR has highest priority.
    bear_mask = (close < sma_long_s) & sma_long_s.notna()
    regimes[bear_mask] = "BEAR"

    # VOLATILE (only on bars not already BEAR).
    volatile_mask = (
        ~bear_mask
        & (atr_pct_rank >= vol_pct_threshold)
        & (atr_pct >= vol_atr_pct_floor)
        & atr_pct_rank.notna()
    )
    regimes[volatile_mask] = "VOLATILE"

    # TRENDING / RANGING from ADX + slope tiebreaker (only on bars not already labelled).
    remaining = ~(bear_mask | volatile_mask)
    high_adx = (adx_s >= adx_trend) & adx_s.notna()
    low_adx = (adx_s <= adx_range) & adx_s.notna()
    ambiguous = adx_s.notna() & ~high_adx & ~low_adx

    trending_mask = remaining & (high_adx | (ambiguous & (sma_short_slope > 0)))
    regimes[trending_mask] = "TRENDING"
    # All other `remaining` bars stay as 'RANGING' (default).

    return regimes


def per_symbol_filter_mask(
    df: pd.DataFrame, *, sma_window: int = 200,
    vol_window: int = 20, notional_min_avg: float = 20_000_000.0,
) -> pd.Series:
    """
    Per-bar DonchianEdgeFilter gate (rules 1 + 3; rule 2 earnings blackout
    deferred). True on bars where entries are allowed under the filter.
    Mirrors strategies.filters.donchian_breakout.DonchianEdgeFilter logic:
    fails open during warmup (NaN SMA / NaN avg volume → allow).
    """
    close = df["close"].astype(float)
    sma = close.rolling(sma_window).mean()
    stock_above = (close > sma).where(sma.notna(), other=True).astype(bool)

    if "volume" in df.columns:
        dollar_vol = close * df["volume"].astype(float)
        avg = dollar_vol.rolling(vol_window).mean()
        liq = (avg >= notional_min_avg).where(avg.notna(), other=True).astype(bool)
    else:
        liq = pd.Series(True, index=df.index, dtype=bool)

    return (stock_above & liq).astype(bool)


# ── Bar loading from cache only ─────────────────────────────────────────────


def load_cached_bars(symbol: str) -> pd.DataFrame | None:
    """
    Read the all-history parquet for `symbol` from data/historical/.
    Returns None if missing or unreadable. Never touches the network.
    """
    path = ROOT / "data" / "historical" / f"{symbol}_1Day_all.parquet"
    if not path.exists():
        return None
    try:
        df = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"{symbol}: failed to read {path.name} — {exc}")
        return None
    cols = {"open", "high", "low", "close"}
    if not cols.issubset(set(df.columns)):
        logger.warning(f"{symbol}: missing OHLC cols in cache")
        return None
    df = df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


def slice_window(df: pd.DataFrame, window: RegimeWindow, *, warmup_bars: int = 50) -> pd.DataFrame | None:
    """
    Return the sub-frame covering `window` plus `warmup_bars` bars of
    indicator warmup before window.start. Returns None if the symbol can't
    contribute enough bars to the window.
    """
    start = pd.Timestamp(window.start, tz="UTC")
    end = pd.Timestamp(window.end, tz="UTC") + pd.Timedelta(days=1)
    # Warmup region: take `warmup_bars` rows preceding `start`.
    pre = df[df.index < start]
    if len(pre) < warmup_bars:
        return None
    warmup = pre.iloc[-warmup_bars:]
    in_window = df[(df.index >= start) & (df.index < end)]
    if len(in_window) < 30:  # need at least ~1 month of in-window bars to be meaningful
        return None
    return pd.concat([warmup, in_window])


# ── Per-window run ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class WindowRun:
    window: RegimeWindow
    policy_aggregates: dict[str, PortfolioAggregate]
    symbols_traded: dict[str, list[str]]  # policy_name → traded symbols (could differ if filter skipped)
    per_symbol: dict[str, list[SymbolResult]]  # policy_name → list of SymbolResult


def run_window(
    symbols: list[str],
    window: RegimeWindow,
    policies: list[StopPolicy],
    *,
    entry_window: int,
    exit_window: int,
    atr_length: int,
    initial_cash: float,
    risk_per_trade_pct: float,
    slippage_bps: float,
    spy_regime: pd.Series,
    apply_production_gates: bool,
) -> WindowRun:
    logger.info(f"window {window.name}: loading bars for {len(symbols)} symbols")
    # Keep BOTH the full-history bars (for filter computation — DonchianEdge
    # filter rule 1 needs SMA200, which requires 200 bars of warmup) AND the
    # window-sliced bars (for the simulator). PR #49 follow-up flagged that
    # computing filters on the sliced 50-bar-warmup window left SMA200 NaN
    # for ~150 bars and silently failed open, allowing entries production
    # would block. Mirror live behavior: filter mask uses full cached history,
    # then is reindexed onto the sliced window.
    bars_full_by_sym: dict[str, pd.DataFrame] = {}
    bars_by_sym: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        df_full = load_cached_bars(sym)
        if df_full is None:
            continue
        sliced = slice_window(df_full, window)
        if sliced is None:
            continue
        bars_full_by_sym[sym] = df_full
        bars_by_sym[sym] = sliced

    if not bars_by_sym:
        logger.warning(f"window {window.name}: zero symbols had usable cached bars")

    trade_start_ts = pd.Timestamp(window.start, tz="UTC")

    policy_aggregates: dict[str, PortfolioAggregate] = {}
    symbols_traded: dict[str, list[str]] = {}
    per_symbol: dict[str, list[SymbolResult]] = {}

    for policy in policies:
        results: list[SymbolResult] = []
        traded: list[str] = []
        for sym, df in bars_by_sym.items():
            entry_mask: pd.Series | None = None
            if apply_production_gates:
                # SPY regime aligned to this symbol's bars; default to RANGING
                # for missing dates (conservative, blocks entries).
                spy_aligned = spy_regime.reindex(df.index).fillna("RANGING")
                regime_ok = (spy_aligned == "TRENDING")
                # Filter on full history then reindex — keeps SMA200 valid at
                # the window boundary when there are enough pre-window bars
                # (>=200 for the first valid SMA200 value).
                filter_full = per_symbol_filter_mask(bars_full_by_sym[sym])
                filter_ok = filter_full.reindex(df.index).fillna(False).astype(bool)
                entry_mask = (regime_ok & filter_ok).astype(bool)
            try:
                res = simulate_symbol(
                    sym, df, policy,
                    entry_window=entry_window,
                    exit_window=exit_window,
                    atr_length=atr_length,
                    initial_cash=initial_cash,
                    risk_per_trade_pct=risk_per_trade_pct,
                    slippage_bps=slippage_bps,
                    trade_start=trade_start_ts,
                    entry_mask=entry_mask,
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"{sym} [{policy.name}]: {exc}")
                continue
            results.append(res)
            traded.append(sym)

        if results:
            policy_aggregates[policy.name] = aggregate(results)
        symbols_traded[policy.name] = traded
        per_symbol[policy.name] = results

    return WindowRun(
        window=window,
        policy_aggregates=policy_aggregates,
        symbols_traded=symbols_traded,
        per_symbol=per_symbol,
    )


# ── Per-year slice of combined run ──────────────────────────────────────────


def per_year_slice_aggregates(
    combined: WindowRun, years: list[int]
) -> dict[int, dict[str, PortfolioAggregate]]:
    """
    For each year in `years`, recompute aggregates on each policy by filtering
    the SymbolResult equity curves and trades to that year. Returns
    {year: {policy_name: PortfolioAggregate}}.

    Trade-level metrics (trade_count, win_rate, exit-reason mix, avg_r, etc.)
    are recomputed by filtering trades whose ENTRY date falls in the year.
    Equity-curve metrics (total_return, sharpe, max_drawdown) are recomputed
    on the sub-curve for that year.
    """
    out: dict[int, dict[str, PortfolioAggregate]] = {}
    for year in years:
        start = pd.Timestamp(f"{year}-01-01", tz="UTC")
        end = pd.Timestamp(f"{year + 1}-01-01", tz="UTC")
        per_policy: dict[str, PortfolioAggregate] = {}
        for policy_name, results in combined.per_symbol.items():
            sliced: list[SymbolResult] = []
            for r in results:
                trades = [t for t in r.trades if start <= t.entry_date < end]
                eq = r.equity_curve[(r.equity_curve.index >= start) & (r.equity_curve.index < end)]
                if eq.empty:
                    continue
                # Build a SymbolResult-like with recomputed stats on the sub-curve.
                init = float(eq.iloc[0])
                final = float(eq.iloc[-1])
                tot_ret = final / init - 1.0 if init > 0 else 0.0
                # We only need fields aggregate() reads — use SymbolResult with
                # minimal recomputation. Sharpe & DD from the sub-curve:
                rets = eq.pct_change().dropna()
                import numpy as np
                sharpe = float(rets.mean() / rets.std() * np.sqrt(252)) if (len(rets) > 1 and rets.std() > 0) else 0.0
                cummax = eq.cummax()
                dd_series = (eq - cummax) / cummax
                dd = float(dd_series.min()) if len(dd_series) else 0.0
                # Buy-hold on the sub-window per symbol
                bh = 0.0  # not meaningful per-year for combined — skip
                cagr = tot_ret  # 1-year window — CAGR = total_return
                sliced.append(SymbolResult(
                    symbol=r.symbol,
                    policy_name=r.policy_name,
                    bars=len(eq),
                    trades=trades,
                    equity_curve=eq,
                    initial_cash=init,
                    final_equity=final,
                    total_return=tot_ret,
                    cagr=cagr,
                    sharpe=sharpe,
                    max_drawdown=dd,
                    trade_count=len(trades),
                    win_rate=(sum(1 for t in trades if t.pnl_pct > 0) / len(trades)) if trades else 0.0,
                    avg_r=(sum(t.r_multiple for t in trades) / len(trades)) if trades else 0.0,
                    expectancy_pct=(sum(t.pnl_pct for t in trades) / len(trades)) if trades else 0.0,
                    buy_hold_return=bh,
                ))
            if sliced:
                per_policy[policy_name] = aggregate(sliced)
        out[year] = per_policy
    return out


# ── Rendering ───────────────────────────────────────────────────────────────


def render_window_table(run: WindowRun) -> str:
    lines = [
        f"### {run.window.name} — {run.window.description}",
        "",
        f"- Window: {run.window.start} → {run.window.end}",
    ]
    policies = list(run.policy_aggregates.keys())
    if not policies:
        return "\n".join(lines + ["", "_no symbols had usable cached bars for this window_", ""])

    n_syms = max(len(run.symbols_traded.get(p, [])) for p in policies)
    lines.append(f"- Symbols traded: {n_syms} (out of {len(UNIVERSES['ai_bigtech'])} in ai_bigtech)")
    lines.extend([
        "",
        "| Policy             |  MeanRet | MeanCAGR | MeanShp | MeanDD | Trades | Win% | AvgR | %Gap | %Intra | %Sig | %EOD |",
        "|--------------------|---------:|---------:|--------:|-------:|-------:|-----:|-----:|-----:|-------:|-----:|-----:|",
    ])
    for policy_name, agg in run.policy_aggregates.items():
        lines.append(
            f"| {policy_name:<18} | "
            f"{agg.mean_total_return*100:>+6.1f}% | "
            f"{agg.mean_cagr*100:>+6.1f}% | "
            f"{agg.mean_sharpe:>+6.2f} | "
            f"{agg.mean_max_drawdown*100:>+5.1f}% | "
            f"{agg.total_trades:>6} | "
            f"{agg.win_rate*100:>4.1f} | "
            f"{agg.avg_r:>+4.2f} | "
            f"{agg.pct_stop_gap*100:>4.1f} | "
            f"{agg.pct_stop_intrabar*100:>5.1f} | "
            f"{agg.pct_signal_exit*100:>4.1f} | "
            f"{agg.pct_eod*100:>4.1f} |"
        )
    return "\n".join(lines) + "\n"


def render_per_year_tables(
    combined: WindowRun, per_year: dict[int, dict[str, PortfolioAggregate]]
) -> str:
    out = ["### Combined run — per-year slice tables", ""]
    for year, per_policy in per_year.items():
        if not per_policy:
            continue
        out.append(f"#### {year}")
        out.append("")
        out.append("| Policy             | TotalRet | Sharpe | MaxDD | Trades | Win% | AvgR | %Gap | %Intra | %Sig | %EOD |")
        out.append("|--------------------|---------:|-------:|------:|-------:|-----:|-----:|-----:|-------:|-----:|-----:|")
        for policy_name, agg in per_policy.items():
            out.append(
                f"| {policy_name:<18} | "
                f"{agg.mean_total_return*100:>+6.1f}% | "
                f"{agg.mean_sharpe:>+5.2f} | "
                f"{agg.mean_max_drawdown*100:>+4.1f}% | "
                f"{agg.total_trades:>6} | "
                f"{agg.win_rate*100:>4.1f} | "
                f"{agg.avg_r:>+4.2f} | "
                f"{agg.pct_stop_gap*100:>4.1f} | "
                f"{agg.pct_stop_intrabar*100:>5.1f} | "
                f"{agg.pct_signal_exit*100:>4.1f} | "
                f"{agg.pct_eod*100:>4.1f} |"
            )
        out.append("")
    return "\n".join(out)


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Donchian trail-stop comparison")
    parser.add_argument(
        "--universe", default="ai_bigtech",
        choices=sorted(UNIVERSES.keys()),
        help="Universe to backtest (default ai_bigtech)",
    )
    parser.add_argument(
        "--entry-window", type=int, default=30,
        help="Donchian entry window in bars (default 30 = production)",
    )
    parser.add_argument(
        "--exit-window", type=int, default=15,
        help="Donchian exit window AND trail rolling-low window (default 15 = production)",
    )
    parser.add_argument(
        "--atr-length", type=int, default=14,
        help="ATR window for stop placement (default 14 = production)",
    )
    parser.add_argument(
        "--slippage-bps", type=float, default=5.0,
        help="Slippage applied to every fill (default 5 bps = backtest standard)",
    )
    parser.add_argument(
        "--initial-cash", type=float, default=100_000.0,
        help="Per-symbol initial cash for the equity curve (default 100k)",
    )
    parser.add_argument(
        "--risk-per-trade-pct", type=float, default=0.02,
        help="Risk per trade as fraction of initial cash (default 2%%)",
    )
    parser.add_argument(
        "--no-production-gates", action="store_true",
        help="Skip production entry gates (SPY TRENDING regime + Donchian "
             "edge filter). Use for sanity comparison against an ungated run. "
             "Default: gates ON (matches live behavior).",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Optional markdown output path (defaults to logs/backtests/<timestamp>_donchian_trail_compare.md)",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logger.remove()
    logger.add(
        sys.stderr,
        level="DEBUG" if args.verbose else "INFO",
        format="<level>{level: <8}</level> | {message}",
    )

    symbols = list(UNIVERSES[args.universe])
    policies = build_policies()
    apply_gates = not args.no_production_gates

    # Load SPY history once and classify regime per bar. SPY is needed even
    # in --no-production-gates mode because we always want the regime context
    # in the report. If SPY is unavailable, fall back to a permissive series.
    spy_df = load_cached_bars("SPY")
    if spy_df is None:
        if apply_gates:
            logger.error(
                "Production gates requested but SPY bars not in cache. "
                "Run scripts/audit_donchian_history.py to backfill, then "
                "re-run. Aborting."
            )
            return 1
        spy_regime = pd.Series(dtype=object)
    else:
        spy_regime = classify_spy_regime(spy_df)
        regime_counts = spy_regime.value_counts().to_dict()
        logger.info(
            f"SPY regime distribution over {len(spy_regime)} bars: "
            f"{regime_counts}"
        )

    runs: list[WindowRun] = []
    for window in WINDOWS:
        runs.append(run_window(
            symbols, window, policies,
            entry_window=args.entry_window,
            exit_window=args.exit_window,
            atr_length=args.atr_length,
            initial_cash=args.initial_cash,
            risk_per_trade_pct=args.risk_per_trade_pct,
            slippage_bps=args.slippage_bps,
            spy_regime=spy_regime,
            apply_production_gates=apply_gates,
        ))

    combined = next((r for r in runs if r.window.name == "2021_2024_combined"), None)
    per_year_tables = ""
    if combined is not None and combined.policy_aggregates:
        per_year = per_year_slice_aggregates(combined, years=[2021, 2022, 2023, 2024])
        per_year_tables = render_per_year_tables(combined, per_year)

    sections = [
        "# Donchian Trail-Stop Comparison",
        "",
        f"- Generated: {datetime.now(timezone.utc).isoformat()}",
        f"- Universe: {args.universe} ({len(symbols)} symbols)",
        f"- Strategy params: entry={args.entry_window}, exit={args.exit_window}, ATR={args.atr_length}",
        f"- Slippage: {args.slippage_bps} bps, init_cash: ${args.initial_cash:,.0f}, risk/trade: {args.risk_per_trade_pct*100:.1f}%",
        f"- Production gates: {'ON (SPY TRENDING-only + DonchianEdgeFilter rules 1+3)' if apply_gates else 'OFF (ungated — every Donchian high entered)'}",
        "- Stop variants compared:",
        "  - **static_atr** — entry - 2 x ATR_at_entry (current production)",
        "  - **donchian_low_trail** — initial = entry - 2 x ATR; ratchets up with rolling 15-low minus 0.5 x ATR buffer",
        "  - **chandelier** — initial = entry - 2 x ATR; ratchets up with HWM_close - 3 x ATR",
        "",
        "Exit-reason mix columns: %Gap = stop filled at open after gap-through; %Intra = stop filled intrabar at stop level; %Sig = Donchian signal exit; %EOD = position open at end of window.",
        "",
    ]
    for run in runs:
        sections.append(render_window_table(run))
    if per_year_tables:
        sections.append(per_year_tables)

    report = "\n".join(sections)
    print(report)

    if args.output:
        out_path = Path(args.output)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out_path = ROOT / "logs" / "backtests" / f"{ts}_donchian_trail_compare.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)
    logger.info(f"wrote report to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
