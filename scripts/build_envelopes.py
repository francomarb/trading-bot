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
from strategies.filters.donchian_breakout import DonchianEdgeFilter  # noqa: E402
from strategies.filters.rsi_reversion import RSIEdgeFilter  # noqa: E402
from strategies.filters.sma_crossover import SMAEdgeFilter  # noqa: E402
from strategies.health.envelope import (  # noqa: E402
    ENVELOPE_SCHEMA_VERSION,
    FilterFidelity,
    StrategyEnvelope,
    envelope_path,
)
from strategies.health.stats import bootstrap_mean_ci  # noqa: E402
from strategies.rsi_reversion import RSIReversion  # noqa: E402
from strategies.sma_crossover import SMACrossover  # noqa: E402


# PR #17 second pass — replaces the earlier "SectorMomentumFilter omitted"
# note which was incomplete. The reviewer correctly identified that even
# the wired filters don't faithfully replay production gating:
#
#   - **Stock-level gates DO replay correctly per-bar** (200-SMA, 20-day
#     volume, 20-day low, IEX liquidity, ATR — all computed from the
#     symbol's own historical df). These are real envelope-shaping
#     filters and they work as intended.
#
#   - **Earnings blackout** is symbol-aware. The backtest now passes
#     `symbol=` through `generate_signals` (backtest/runner.py was
#     fixed in this PR), so EarningsBlackout can resolve. Whether
#     it actually rejects depends on whether the offline earnings
#     cache has data for the symbol over the backtest window.
#
#   - **SPY trend gate (SPYTrendFilter / SMA200 / SMA50)** is a
#     live-cycle filter: it fetches CURRENT SPY state at filter
#     construction. During the historical backtest, that same
#     build-time SPY snapshot is applied to every bar, instead of
#     replaying per-bar historical SPY state. This means an envelope
#     built on a day when SPY > 200 SMA produces a different envelope
#     than one built on a day when SPY < 200 SMA. **Real fix would
#     require a historical-SPY-injection mode on SPYTrendFilter** —
#     out of v1 scope; logged as follow-up §F-future (post-11.10h).
#
#   - **SectorMomentumFilter** is intentionally omitted entirely
#     (offline-unfriendly state).
#
# Net effect: the envelope OVER-counts production-allowed signals
# (live gating rejects more than we capture). Trade-frequency and
# block-rate bands should be read with this caveat. The envelope is
# tagged FilterFidelity.PARTIAL_STOCK_GATES_ONLY so the EdgeAssessor
# (11.10d) widens its drift bands accordingly.
FILTER_FIDELITY_NOTE = (
    "filter_fidelity=partial_stock_gates_only. Stock-level gates "
    "(200-SMA, volume, 20-day low, liquidity, ATR) replay per-bar "
    "correctly from the symbol's df. Earnings blackout is symbol-aware "
    "and now receives the symbol context (PR #17 fix to "
    "backtest/runner.py), but its replay quality depends on whether the "
    "offline earnings cache has data for the backtest window. SPY trend "
    "gates (SMA200/SMA50) use the build-time SPY snapshot and apply "
    "that same decision to every historical bar — not per-bar historical "
    "SPY state. SectorMomentumFilter is omitted entirely (offline-"
    "unfriendly). Net: envelope OVER-counts production-allowed signals; "
    "trade-frequency / block-rate bands are upper bounds. The 11.10g "
    "calibration script can re-tune from live paper counters."
)


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
    # Production wiring (forward_test.py) uses CompositeEdgeFilter([
    # SMAEdgeFilter, SectorMomentumFilter]); we wire SMAEdgeFilter only —
    # see SECTOR_FILTER_OMITTED_NOTE for rationale.
    return SMACrossover(fast=20, slow=50, edge_filter=SMAEdgeFilter())


def _rsi_builder():
    return RSIReversion(
        period=14, oversold=30, overbought=70, edge_filter=RSIEdgeFilter()
    )


def _donchian_builder():
    return DonchianBreakout(
        entry_window=30, exit_window=15,
        edge_filter=DonchianEdgeFilter(feed_label=settings.BACKTEST_DATA_FEED)
    )


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
            "the options chain. Build the envelope from paper data once "
            "≥10 closed trades have accumulated: "
            "`scripts/build_paper_envelope.py --strategy spy_options_reversion "
            "--weeks N`. (Note: scripts/calibrate_health_thresholds.py tunes "
            "L1/L2/L3 Health thresholds only; it does not build Edge envelopes.)"
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
            "multi-leg fills. Build the envelope from paper data once "
            "≥10 closed trades have accumulated: "
            "`scripts/build_paper_envelope.py --strategy credit_spread "
            "--weeks N`. (Note: scripts/calibrate_health_thresholds.py tunes "
            "L1/L2/L3 Health thresholds only; it does not build Edge envelopes.)"
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
    from config import settings
    backtest_feed = settings.BACKTEST_DATA_FEED
    bars: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df, _ = fetch_symbol(sym, start, end, timeframe, feed=backtest_feed)
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


def _is_finite(v: object) -> bool:
    """True iff `v` is a finite scalar or a tuple of finite scalars."""
    if v is None:
        return True  # None is fine — represents "no value", not "non-finite"
    if isinstance(v, (int, float)):
        return bool(np.isfinite(v))
    if isinstance(v, tuple):
        return all(_is_finite(x) for x in v)
    return True


def _coerce_finite(
    a: tuple[str, object],
    b: tuple[str, object],
) -> tuple[object, object, list[str]]:
    """Return (a_value, b_value, dropped_names) replacing any non-finite
    scalar / tuple-of-scalars with None.

    Used to keep envelope JSON standard-compliant (no `Infinity` /
    `NaN`). Caller appends `dropped_names` to envelope notes so the
    operator knows which fields got nulled.
    """
    dropped: list[str] = []
    a_name, a_val = a
    b_name, b_val = b
    if not _is_finite(a_val):
        dropped.append(a_name)
        a_val = None
    if not _is_finite(b_val):
        dropped.append(b_name)
        b_val = None
    return a_val, b_val, dropped


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
            filter_fidelity=FilterFidelity.NOT_BACKTESTED,
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
            filter_fidelity=FilterFidelity.PARTIAL_STOCK_GATES_ONLY,
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
            filter_fidelity=FilterFidelity.PARTIAL_STOCK_GATES_ONLY,
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

    # R-multiple approximation. vectorbt uses all-in sizing by default
    # (one position consumes ~all available cash), so the per-trade
    # position notional is approximately `initial_cash`. The stop loss
    # at `approx_stop_pct` therefore costs `initial_cash * approx_stop_pct`
    # dollars if hit — that's 1R for envelope purposes.
    #
    # **Do not multiply by MAX_POSITION_PCT here.** MAX_POSITION_PCT is
    # the *production* risk fraction (loss-to-stop ÷ equity); production
    # sizes positions smaller (~40k notional for $100k equity at 2% risk
    # and a 5% stop) but the live R-multiple from trades.r_multiple
    # already normalizes for that. R is sizing-invariant by design — for
    # the same underlying move, vectorbt-R and live-R should match if
    # both denominators reflect their respective position notional.
    #
    # PR #17 reviewer caught the original `* MAX_POSITION_PCT` multiplication
    # which deflated risk_unit_dollars ~50× and would have inflated
    # envelope R-expectancy / CIs by the same factor, making
    # live-vs-envelope comparison unusable.
    cfg = BacktestConfig()
    approx_stop_pct = spec.get("approx_stop_pct")
    if approx_stop_pct is not None:
        risk_unit = cfg.initial_cash * approx_stop_pct
        r_values = [p / risk_unit for p in pnls]
        r_arr = np.asarray(r_values, dtype=float)
        r_expectancy = float(r_arr.mean())
        r_expectancy_ci = bootstrap_mean_ci(r_values, n_resamples=2000, seed=0)
        notes.append(
            f"r_expectancy uses risk_unit_dollars={risk_unit:.2f} "
            f"(initial_cash * approx_stop_pct={approx_stop_pct}; "
            f"vectorbt's all-in sizing means position notional ≈ initial_cash). "
            f"Live r_multiple is exact (per-trade); envelope R is an "
            f"approximation but is sizing-invariant when compared correctly."
        )
    else:
        risk_unit = None
        r_expectancy = None
        r_expectancy_ci = None

    win_rate_pt = float((pnls_arr > 0).sum() / pnls_arr.size)
    win_rate_ci = _bootstrap_metric(pnls, _win_rate)
    pf_pt = _profit_factor(pnls_arr)
    pf_ci = _bootstrap_metric(pnls, _profit_factor)

    # ── Normalize non-finite numerics before envelope construction ──
    # All-winning backtests produce profit_factor=+inf; bootstrap
    # resamples can produce inf CI bounds (e.g. a resample of all-wins).
    # Inf serializes as JSON "Infinity" which is not standard JSON and
    # breaks strict parsers (jq, browser JSON.parse). Convert any
    # non-finite to None and append a note so the operator knows.
    pf_pt, pf_ci, drops = _coerce_finite(
        ("profit_factor", pf_pt),
        ("profit_factor_ci", pf_ci),
    )
    r_expectancy, r_expectancy_ci, r_drops = _coerce_finite(
        ("r_expectancy", r_expectancy),
        ("r_expectancy_ci", r_expectancy_ci),
    )
    expectancy_dollars_ci, _, ed_drops = _coerce_finite(
        ("expectancy_dollars_ci", expectancy_dollars_ci),
        ("_", None),
    )
    win_rate_ci, _, wr_drops = _coerce_finite(
        ("win_rate_ci", win_rate_ci),
        ("_", None),
    )
    if drops or r_drops or ed_drops or wr_drops:
        dropped = drops + r_drops + ed_drops + wr_drops
        notes.append(
            f"non-finite values replaced with null (envelope JSON stays "
            f"standard-compliant): {dropped}. Typically caused by "
            f"all-winning backtests (profit_factor=+inf) or sparse-loss "
            f"bootstrap resamples."
        )

    # Document the filter-fidelity gap so the assessor and operator
    # know exactly which gates are replayed correctly and which leak
    # live-cycle state. See FILTER_FIDELITY_NOTE for the precise
    # accounting.
    notes.append(FILTER_FIDELITY_NOTE)

    envelope = StrategyEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy=strategy_name,
        built_at=datetime.now(timezone.utc).isoformat(),
        backtest_window_start=start.date().isoformat(),
        backtest_window_end=end.date().isoformat(),
        backtest_config=backtest_config,
        filter_fidelity=FilterFidelity.PARTIAL_STOCK_GATES_ONLY,
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
