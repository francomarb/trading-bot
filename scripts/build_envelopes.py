#!/usr/bin/env python3
"""
Build per-strategy reference envelopes (PLAN 11.10b).

For each strategy, run a backtest over the trailing N years at its production
config, aggregate per-trade results across the strategy's watchlist, compute
the envelope fields (R-expectancy + dollar expectancy + win rate + profit
factor with bootstrap 95% CIs, trade frequency band, hold-days band, p95
drawdown), and write data/envelopes/{strategy_name}.json with
schema_version=1.

Per design §7 the envelope is **static** in v1 — no auto-recalibration. The
operator runs this script when a strategy's production config changes.
Parameter-grid distribution (§F4) and hybrid recalibration (§F5) are
deferred. Lifecycle bands (consumed by L3 Drift) are written as `null` in
v1 — the 11.10g calibration script populates them from paper data after
4 weeks of operation.

Per-strategy failures (offline-only dependencies like OPRA quote lookup
for options strategies) are tolerated: the script writes an envelope with
null Edge metrics + a `notes` entry explaining the skip, and continues
to the next strategy. Downstream the EdgeAssessor degrades to
INSUFFICIENT/UNDETERMINED gracefully when a strategy's envelope is empty.

Usage:
    /Users/franco/trading-bot/venv/bin/python scripts/build_envelopes.py --all
    /Users/franco/trading-bot/venv/bin/python scripts/build_envelopes.py --strategy donchian_breakout
    /Users/franco/trading-bot/venv/bin/python scripts/build_envelopes.py --all --years 3 --end-date 2026-05-01
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.runner import BacktestConfig, run_backtest  # noqa: E402
from config import settings  # noqa: E402
from data.fetcher import fetch_symbol  # noqa: E402
from strategies.donchian_breakout import DonchianBreakout  # noqa: E402
from strategies.health.envelope import (  # noqa: E402
    ENVELOPE_SCHEMA_VERSION,
    StrategyEnvelope,
    envelope_path,
)
from strategies.health.stats import bootstrap_mean_ci  # noqa: E402
from strategies.rsi_reversion import RSIReversion  # noqa: E402
from strategies.sma_crossover import SMACrossover  # noqa: E402


# ── Per-strategy build spec ───────────────────────────────────────────


# Each spec encapsulates: how to construct the strategy at production
# config (no edge filter — we want the raw strategy envelope; live edge
# filtering shows up later as the L3 lifecycle drift signal), which
# symbols form its watchlist, what timeframe, and which ATR stop to
# simulate. Mirrors the forward_test.py production instantiations.
#
# Options strategies (spy_options_reversion, credit_spread) require live
# OPRA quote lookups at construction — not feasible offline. They get a
# `builder` that returns None, which the build loop interprets as
# "write a stub envelope with notes explaining the skip and continue".


def _sma_builder():
    return SMACrossover(fast=20, slow=50)


def _rsi_builder():
    return RSIReversion(period=14, oversold=30, overbought=70)


def _donchian_builder():
    return DonchianBreakout(entry_window=30, exit_window=15)


def _spy_options_builder():
    # Options strategy — backtest harness can't replay OPRA chains
    # offline. Returns None so the build loop writes a stub envelope
    # with the appropriate notes.
    return None


def _credit_spread_builder():
    # Same — options strategy with live OPRA quote dependency.
    return None


STRATEGY_SPECS: dict[str, dict] = {
    "sma_crossover": {
        "builder": _sma_builder,
        "watchlist_key": "sma_crossover",  # key in settings.STRATEGY_WATCHLISTS
        "timeframe": "1Day",
        # SMA crossover doesn't use ATR stops in production; it exits on
        # the reverse crossover. Backtest the same way.
        "atr_stop_mult": None,
        "atr_trail": False,
        # For R-multiple approximation, assume risk per trade is roughly
        # MAX_POSITION_PCT * initial_cash * stop_pct. Without an ATR stop
        # we use a conservative 5% notional as the implicit risk unit.
        "approx_stop_pct": 0.05,
    },
    "rsi_reversion": {
        "builder": _rsi_builder,
        "watchlist_key": "rsi_reversion",
        "timeframe": "1Day",
        # RSI uses limit-order exits; no explicit ATR stop in production.
        "atr_stop_mult": None,
        "atr_trail": False,
        "approx_stop_pct": 0.03,
    },
    "donchian_breakout": {
        "builder": _donchian_builder,
        "watchlist_key": "donchian_breakout",
        "timeframe": "1Day",
        # Donchian uses ATR trailing stops in production (per
        # forward_test.py — atr_trail=True validated in docs/donchian_*).
        "atr_stop_mult": settings.ATR_STOP_MULTIPLIER,
        "atr_trail": True,
        "approx_stop_pct": 0.02,  # ~2 ATR ≈ 2-3% on AI/big-tech daily
    },
    "spy_options_reversion": {
        "builder": _spy_options_builder,
        "watchlist_key": "spy_options_reversion",
        "timeframe": "1Day",
        "atr_stop_mult": None,
        "atr_trail": False,
        "approx_stop_pct": None,
        "skip_reason": (
            "spy_options_reversion requires live OPRA quote lookup at "
            "strategy construction; offline backtest harness cannot replay "
            "the options chain. Envelope will be populated from paper data "
            "by scripts/calibrate_health_thresholds.py (11.10g) after 4 "
            "weeks of operation."
        ),
    },
    "credit_spread": {
        "builder": _credit_spread_builder,
        "watchlist_key": "credit_spread",
        "timeframe": "1Day",
        "atr_stop_mult": None,
        "atr_trail": False,
        "approx_stop_pct": None,
        "skip_reason": (
            "credit_spread requires live OPRA quote lookup + IVProxyResolver "
            "at strategy construction; offline backtest cannot replay "
            "multi-leg fills. Envelope will be populated from paper data "
            "by scripts/calibrate_health_thresholds.py (11.10g) after 4 "
            "weeks of operation."
        ),
    },
}


# ── Helpers ───────────────────────────────────────────────────────────


def _utc(dt: date | datetime) -> datetime:
    if isinstance(dt, datetime):
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return datetime(dt.year, dt.month, dt.day, tzinfo=timezone.utc)


def _watchlist_for(strategy: str, spec: dict) -> list[str]:
    """Resolve the strategy's production watchlist via settings."""
    key = spec.get("watchlist_key", strategy)
    watchlists = getattr(settings, "STRATEGY_WATCHLISTS", {})
    if key in watchlists:
        return list(watchlists[key])
    logger.warning(
        f"{strategy}: no watchlist found at STRATEGY_WATCHLISTS[{key!r}]; "
        f"envelope will be empty."
    )
    return []


def _fetch_bars(
    symbols: list[str],
    start: datetime,
    end: datetime,
    timeframe: str,
) -> dict[str, pd.DataFrame]:
    """Cache-backed daily-bar fetch for a watchlist. Skips symbols with
    fetch failures or insufficient bars."""
    bars: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df, _ = fetch_symbol(sym, start, end, timeframe)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{sym}: fetch failed — {exc}")
            continue
        if df is None or df.empty:
            logger.warning(f"{sym}: no bars returned")
            continue
        df = df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
        if len(df) < 60:
            logger.warning(f"{sym}: only {len(df)} bars — skipping")
            continue
        bars[sym] = df
    return bars


def _aggregate_trades(
    strategy_builder: Callable,
    bars: dict[str, pd.DataFrame],
    *,
    atr_stop_mult: float | None,
    atr_trail: bool,
) -> tuple[list[float], list[int], list[float], dict]:
    """Run the strategy on every symbol's bars and collect:
      - per-trade dollar PnL (list[float])
      - per-trade hold-days (list[int]) — integer calendar days between entry and exit
      - per-symbol max drawdown (list[float]) for the p95 envelope
      - per-symbol stats dict (count, span_days) for trade-frequency band

    The per-trade unit is the trade itself, not the symbol — small/large
    watchlists are comparable on expectancy/win-rate but not on absolute
    trade counts. trades_per_month_band is computed per-symbol-per-month
    so cross-strategy comparison stays honest.

    Returns aggregated lists; the build function turns them into envelope
    fields with bootstrap CIs.
    """
    pnls: list[float] = []
    hold_days: list[int] = []
    drawdowns: list[float] = []
    per_symbol_trades: dict[str, dict] = {}

    cfg = BacktestConfig()  # use defaults (slippage 5bps, commission 0)
    for sym, df in bars.items():
        try:
            strategy = strategy_builder()
            result = run_backtest(
                strategy,
                df,
                cfg,
                symbol=sym,
                atr_stop_mult=atr_stop_mult,
                atr_trail=atr_trail,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"{sym}: backtest failed — {exc}")
            continue
        records = result.portfolio.trades.records_readable
        if records is None or len(records) == 0:
            per_symbol_trades[sym] = {
                "count": 0,
                "span_days": int((df.index[-1] - df.index[0]).days),
            }
            continue
        # PnL column from vectorbt records is dollar P&L per closed trade.
        symbol_pnls = [float(p) for p in records["PnL"].tolist()]
        pnls.extend(symbol_pnls)
        # Per-trade hold days: vectorbt records carry Entry Index + Exit Index
        # as timezone-aware timestamps when the input frame had a DatetimeIndex.
        if "Entry Index" in records.columns and "Exit Index" in records.columns:
            for entry_ts, exit_ts in zip(
                records["Entry Index"], records["Exit Index"]
            ):
                if pd.notna(entry_ts) and pd.notna(exit_ts):
                    days = (exit_ts - entry_ts).days
                    if days >= 0:  # defensive: skip same-bar weirdness
                        hold_days.append(int(days))
        # Per-symbol max drawdown (fraction). Used for the envelope's
        # p95_drawdown_pct field via percentile across symbols.
        try:
            max_dd = float(abs(result.stats["max_drawdown"]))
            drawdowns.append(max_dd)
        except KeyError:
            pass
        per_symbol_trades[sym] = {
            "count": len(symbol_pnls),
            "span_days": int((df.index[-1] - df.index[0]).days),
        }

    return pnls, hold_days, drawdowns, per_symbol_trades


def _trades_per_month_band(
    per_symbol_trades: dict[str, dict],
) -> tuple[float, float] | None:
    """Compute the p10/p90 band of trades-per-month across symbols.

    Per-symbol monthly rate = count / (span_days / 30.4375). Symbols with
    span_days < 30 are skipped (need at least one month of data for a
    rate to be meaningful).
    """
    rates: list[float] = []
    for st in per_symbol_trades.values():
        span = st.get("span_days", 0)
        if span < 30:
            continue
        rates.append(st["count"] / (span / 30.4375))
    if not rates:
        return None
    arr = np.asarray(rates, dtype=float)
    return (float(np.percentile(arr, 10)), float(np.percentile(arr, 90)))


def _hold_days_band(hold_days: list[int]) -> tuple[float, float] | None:
    if not hold_days:
        return None
    arr = np.asarray(hold_days, dtype=float)
    return (float(np.percentile(arr, 10)), float(np.percentile(arr, 90)))


def _p95_drawdown(drawdowns: list[float]) -> float | None:
    if not drawdowns:
        return None
    return float(np.percentile(np.asarray(drawdowns, dtype=float), 95))


def _bootstrap_metric(
    values: list[float],
    transform: Callable[[np.ndarray], float],
    *,
    n_resamples: int = 2000,
    seed: int = 0,
) -> tuple[float, float] | None:
    """Bootstrap CI for an arbitrary statistic (win rate, profit factor)
    where the per-trade reduce isn't just a mean.

    `bootstrap_mean_ci` only covers means; this generalizes for win rate
    and profit factor. Returns None on insufficient sample (N < 2).
    """
    if len(values) < 2:
        return None
    arr = np.asarray(values, dtype=float)
    n = arr.size
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_resamples, n))
    samples = arr[idx]
    stats = np.apply_along_axis(transform, 1, samples)
    return (float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5)))


def _win_rate(arr: np.ndarray) -> float:
    return float((arr > 0).sum() / arr.size) if arr.size else 0.0


def _profit_factor(arr: np.ndarray) -> float:
    if arr.size == 0:
        return 0.0
    gp = float(arr[arr > 0].sum())
    gl = float(-arr[arr < 0].sum())
    if gl == 0:
        return float("inf") if gp > 0 else 0.0
    return gp / gl


# ── Build entry ───────────────────────────────────────────────────────


def build_envelope(
    strategy_name: str,
    *,
    years: float,
    end_date: datetime | None,
    out_dir: Path | None,
) -> StrategyEnvelope:
    """Run one strategy's backtest, return a populated StrategyEnvelope.

    Always writes the envelope to disk (under `out_dir` or the default
    `data/envelopes/` location). Returns the StrategyEnvelope it wrote.

    Strategies whose `builder` returns None (options strategies that
    can't run offline) get a stub envelope with the `skip_reason` baked
    into `notes`.
    """
    if strategy_name not in STRATEGY_SPECS:
        raise ValueError(
            f"unknown strategy {strategy_name!r}; "
            f"known: {sorted(STRATEGY_SPECS.keys())}"
        )
    spec = STRATEGY_SPECS[strategy_name]
    end = end_date or (datetime.now(timezone.utc) - timedelta(hours=1))
    end = _utc(end)
    start = end - timedelta(days=int(365 * years) + 30)

    notes: list[str] = []
    backtest_config = {
        "atr_stop_mult": spec.get("atr_stop_mult"),
        "atr_trail": spec.get("atr_trail"),
        "approx_stop_pct": spec.get("approx_stop_pct"),
        "years": years,
    }

    # Stub case: options strategies that can't run offline.
    if spec.get("skip_reason"):
        notes.append(spec["skip_reason"])
        envelope = StrategyEnvelope(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            strategy=strategy_name,
            built_at=datetime.now(timezone.utc).isoformat(),
            backtest_window_start=start.date().isoformat(),
            backtest_window_end=end.date().isoformat(),
            backtest_config=backtest_config,
            notes=tuple(notes),
        )
        path = envelope_path(strategy_name, root=out_dir)
        envelope.write(path)
        logger.info(f"{strategy_name}: stub envelope → {path}")
        return envelope

    # Run the backtest pipeline.
    symbols = _watchlist_for(strategy_name, spec)
    bars = _fetch_bars(symbols, start, end, spec["timeframe"])
    if not bars:
        notes.append("no usable bars fetched for any watchlist symbol")
        envelope = StrategyEnvelope(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            strategy=strategy_name,
            built_at=datetime.now(timezone.utc).isoformat(),
            backtest_window_start=start.date().isoformat(),
            backtest_window_end=end.date().isoformat(),
            backtest_config=backtest_config,
            notes=tuple(notes),
        )
        path = envelope_path(strategy_name, root=out_dir)
        envelope.write(path)
        logger.warning(f"{strategy_name}: empty envelope (no bars) → {path}")
        return envelope

    pnls, hold_days, drawdowns, per_symbol = _aggregate_trades(
        spec["builder"],
        bars,
        atr_stop_mult=spec.get("atr_stop_mult"),
        atr_trail=bool(spec.get("atr_trail", False)),
    )

    trade_count = len(pnls)
    if trade_count == 0:
        notes.append("strategy produced zero closed trades on this window")
        envelope = StrategyEnvelope(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            strategy=strategy_name,
            built_at=datetime.now(timezone.utc).isoformat(),
            backtest_window_start=start.date().isoformat(),
            backtest_window_end=end.date().isoformat(),
            backtest_config=backtest_config,
            trade_count=0,
            trades_per_month_band=_trades_per_month_band(per_symbol),
            notes=tuple(notes),
        )
        path = envelope_path(strategy_name, root=out_dir)
        envelope.write(path)
        logger.warning(f"{strategy_name}: zero trades — envelope written → {path}")
        return envelope

    # Edge metrics (dollar)
    pnls_arr = np.asarray(pnls, dtype=float)
    expectancy_dollars = float(pnls_arr.mean())
    expectancy_dollars_ci = bootstrap_mean_ci(pnls, n_resamples=2000, seed=0)

    # R-multiple approximation. Per the design + envelope module's
    # `risk_unit_dollars` field: convert per-trade $ PnL to R using a
    # constant risk unit derived from initial_cash * max_position_pct
    # * approx_stop_pct. This is a v1 approximation — live R is exact
    # from trades.r_multiple. The two are within ~order of magnitude;
    # the bootstrap CI absorbs the scale uncertainty.
    cfg = BacktestConfig()
    approx_stop_pct = spec.get("approx_stop_pct")
    if approx_stop_pct is not None:
        risk_unit = cfg.initial_cash * settings.MAX_POSITION_PCT * approx_stop_pct
        r_values = [p / risk_unit for p in pnls]
        r_arr = np.asarray(r_values, dtype=float)
        r_expectancy = float(r_arr.mean())
        r_expectancy_ci = bootstrap_mean_ci(r_values, n_resamples=2000, seed=0)
        notes.append(
            f"r_expectancy uses constant risk_unit_dollars={risk_unit:.2f} "
            f"(initial_cash * MAX_POSITION_PCT * approx_stop_pct={approx_stop_pct}). "
            f"Live r_multiple is exact (per-trade); envelope R is an approximation."
        )
    else:
        risk_unit = None
        r_expectancy = None
        r_expectancy_ci = None

    win_rate_pt = float((pnls_arr > 0).sum() / pnls_arr.size)
    win_rate_ci = _bootstrap_metric(pnls, _win_rate)
    pf_pt = _profit_factor(pnls_arr)
    pf_ci = _bootstrap_metric(pnls, _profit_factor)

    envelope = StrategyEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy=strategy_name,
        built_at=datetime.now(timezone.utc).isoformat(),
        backtest_window_start=start.date().isoformat(),
        backtest_window_end=end.date().isoformat(),
        backtest_config=backtest_config,
        r_expectancy=r_expectancy,
        r_expectancy_ci_95=r_expectancy_ci,
        risk_unit_dollars=risk_unit,
        expectancy_dollars=expectancy_dollars,
        expectancy_dollars_ci_95=expectancy_dollars_ci,
        win_rate=win_rate_pt,
        win_rate_ci_95=win_rate_ci,
        profit_factor=pf_pt,
        profit_factor_ci_95=pf_ci,
        trade_count=trade_count,
        trades_per_month_band=_trades_per_month_band(per_symbol),
        hold_days_band=_hold_days_band(hold_days),
        p95_drawdown_pct=_p95_drawdown(drawdowns),
        # Lifecycle bands intentionally left null — 11.10g calibration
        # populates them from paper data after 4 weeks of operation.
        notes=tuple(notes),
    )

    path = envelope_path(strategy_name, root=out_dir)
    envelope.write(path)
    logger.info(
        f"{strategy_name}: envelope built ({trade_count} trades, "
        f"E[$]={expectancy_dollars:+.2f}, WR={win_rate_pt:.1%}) → {path}"
    )
    return envelope


# ── CLI ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build per-strategy reference envelopes (PLAN 11.10b)."
    )
    grp = parser.add_mutually_exclusive_group(required=True)
    grp.add_argument("--all", action="store_true", help="Build envelopes for all known strategies.")
    grp.add_argument(
        "--strategy",
        choices=sorted(STRATEGY_SPECS.keys()),
        help="Build envelope for a single strategy only.",
    )
    parser.add_argument(
        "--years", type=float, default=2.0,
        help="Trailing years of daily bars to use (default 2.0).",
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help="End date YYYY-MM-DD (UTC). Defaults to today.",
    )
    parser.add_argument(
        "--out-dir", type=str, default=None,
        help="Override output directory (default data/envelopes/).",
    )
    args = parser.parse_args()

    end_dt: datetime | None = None
    if args.end_date:
        end_dt = datetime.strptime(args.end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    out_dir = Path(args.out_dir) if args.out_dir else None

    strategies = (
        sorted(STRATEGY_SPECS.keys()) if args.all else [args.strategy]
    )

    n_ok = n_fail = 0
    for strategy in strategies:
        try:
            build_envelope(
                strategy,
                years=args.years,
                end_date=end_dt,
                out_dir=out_dir,
            )
            n_ok += 1
        except Exception as exc:  # noqa: BLE001
            logger.error(f"{strategy}: envelope build failed — {exc}")
            n_fail += 1

    logger.info(
        f"build_envelopes: {n_ok} succeeded, {n_fail} failed "
        f"(of {len(strategies)} strategies)"
    )
    return 0 if n_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
