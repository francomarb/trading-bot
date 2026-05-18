"""
Per-strategy benchmark returns for EdgeReport vs-benchmark comparison.

Design §5.3 (Grinold-Kahn canonical benchmarking): benchmark against the
universe the strategy is *expressing a view on*, not against SPY. A
trend-following strategy that returns +18% while its watchlist's BH
returned +25% is destroying value despite positive raw P&L.

Per-strategy benchmark mapping:
  - SMA crossover         → equal-weight BH of SMA_WATCHLIST
  - RSI reversion         → equal-weight BH of RSI_WATCHLIST
  - Donchian breakout     → equal-weight BH of ai_bigtech 32-name universe
  - SPY options reversion → delta-equivalent SPY shares (callers compute
                            from the strategy's delta exposure history;
                            this module provides the SPY BH primitive)
  - Credit spreads        → underlying-BH (SPY/QQQ) — v1 placeholder; the
                            short-vol replicator (SVXY) is follow-up §F11

The fetcher's Parquet cache (data/fetcher.py) handles repeat calls
cheaply — first run is ~30s for 50 symbols × 1 year of dailies;
subsequent calls are near-zero. Daily bars don't change retroactively
so the cache stays warm.

This module is pure: no Alpaca calls beyond what `fetch_symbol` already
does, no state, no side effects. Returns are floats; callers compose
them into EdgeReport.
"""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

import pandas as pd
from loguru import logger

from data.fetcher import fetch_symbol


def buy_and_hold_return(df: pd.DataFrame) -> float:
    """Total return of buy-on-first-bar / hold-to-last-bar.

    Uses first bar's open and last bar's close — mirrors the convention
    in `scripts/backtest_bollinger_squeeze.py:buy_and_hold_return` so
    backtest reports and health reports use the same definition.

    Returns 0.0 on insufficient data or invalid first price.
    """
    if df is None or len(df) < 2:
        return 0.0
    first_open = float(df["open"].iloc[0])
    last_close = float(df["close"].iloc[-1])
    if first_open <= 0:
        return 0.0
    return last_close / first_open - 1.0


def equal_weight_bh_return(
    symbols: Iterable[str],
    start: datetime,
    end: datetime,
    *,
    timeframe: str = "1Day",
) -> float:
    """Equal-weight average of buy-and-hold returns over `[start, end]`.

    Each symbol contributes one BH return; the result is the simple mean
    across all symbols that returned usable bars. Symbols with fetch
    failures or insufficient bars are skipped with a WARN log — fail-open
    so a single illiquid name doesn't poison the benchmark.

    Returns 0.0 when no symbols return usable data (defensive default;
    EdgeAssessor treats a 0.0 benchmark as "no comparison available"
    rather than "strategy beat the benchmark").

    Cost: O(N_symbols) calls to `fetch_symbol`, which is cache-backed —
    typical weekly/monthly recompute is sub-second after warm-up.
    """
    sym_list = [s for s in symbols if s]
    if not sym_list:
        return 0.0
    returns: list[float] = []
    for sym in sym_list:
        try:
            df, _ = fetch_symbol(sym, start, end, timeframe)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"benchmark fetch failed for {sym} [{start.date()}..{end.date()}]: {exc}"
            )
            continue
        if df is None or df.empty:
            logger.warning(f"benchmark: no bars for {sym}")
            continue
        ret = buy_and_hold_return(df)
        returns.append(ret)
    if not returns:
        return 0.0
    return sum(returns) / len(returns)


# ── Per-strategy benchmark resolvers (design §5.3 table) ──────────────
# The build script + EdgeAssessor look up which symbols to fetch for
# each strategy by calling these. Kept as functions (not a dict) so the
# resolution can do conditional things later (e.g. credit spread
# multi-instrument benchmarks summing SPY+QQQ allocations) without
# breaking call sites.


def benchmark_symbols_for(strategy: str) -> list[str]:
    """Return the symbols whose equal-weight BH defines the benchmark
    for `strategy`.

    Lazy import of `config.settings` so this module is importable in
    contexts where settings hasn't been initialized (e.g. envelope-build
    smoke tests).

    For options strategies: returns the underlying ticker(s) — the
    EdgeAssessor handles the delta-equivalent / underlying-BH framing
    based on the strategy's position composition.
    """
    from config import settings

    # SMA / RSI / Donchian → their own watchlists (verbatim from settings).
    watchlists = getattr(settings, "STRATEGY_WATCHLISTS", {})
    if strategy in watchlists:
        return list(watchlists[strategy])

    # Options strategies → underlying tickers (v1 placeholder).
    # Credit spread benchmark short-vol replicator is follow-up §F11.
    if strategy == "spy_options_reversion":
        return ["SPY"]
    if strategy == "credit_spread":
        # Per design §5.3 table: underlying-BH (SPY/QQQ) for v1.
        # Reads the CREDIT_SPREAD_INSTRUMENTS config to discover what's
        # actually configured; falls back to SPY+QQQ if absent.
        instruments = getattr(settings, "CREDIT_SPREAD_INSTRUMENTS", None)
        if instruments:
            return list(instruments.keys())
        return ["SPY", "QQQ"]

    logger.warning(
        f"benchmark_symbols_for({strategy!r}): no benchmark mapping defined; "
        f"returning empty list (EdgeAssessor will report 'no benchmark available')."
    )
    return []
