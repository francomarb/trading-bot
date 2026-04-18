"""
Phase 5 verification — Backtesting Harness.

Integration check: backtest SMA crossover on real AAPL daily bars (~5y),
print stats, save equity/drawdown PNG, run walk-forward across 4 folds, and
sweep a (fast, slow) parameter grid to surface the distribution of returns.

Run: python phase5_verify.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from loguru import logger

from backtest.runner import (
    BacktestConfig,
    parameter_sensitivity,
    run_backtest,
    save_equity_chart,
    walk_forward,
)
from data.fetcher import fetch_symbol
from strategies.sma_crossover import SMACrossover

logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
logger.add("logs/phase5.log", rotation="1 MB")


def _print_stats(label: str, stats: dict) -> None:
    print(f"\n── {label} ──")
    print(f"  total_return    {stats['total_return']:+.2%}")
    print(f"  CAGR            {stats['cagr']:+.2%}")
    print(f"  Sharpe          {stats['sharpe']:.2f}")
    print(f"  Sortino         {stats['sortino']:.2f}")
    print(f"  max_drawdown    {stats['max_drawdown']:.2%}")
    print(f"  profit_factor   {stats['profit_factor']:.2f}")
    print(f"  expectancy      ${stats['expectancy']:.2f}/trade")
    print(f"  trade_count     {int(stats['trade_count'])}")
    print(f"  win_rate        {stats['win_rate']:.0%}  (de-emphasized)")
    print(f"  final_equity    ${stats['final_equity']:,.2f}")


def main() -> None:
    Path("logs").mkdir(exist_ok=True)
    logger.info("═══ Phase 5 Verification — Backtesting Harness ═══")

    # ~5y of daily bars.
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * 5 + 30)
    df, _ = fetch_symbol("AAPL", start, end, "1Day")
    assert not df.empty, "no bars fetched"
    logger.success(f"Fetched {len(df)} AAPL daily bars [{df.index[0].date()} → {df.index[-1].date()}]")

    cfg = BacktestConfig(initial_cash=100_000, slippage_bps=5.0, commission_per_trade=0.0)

    # 1. Single backtest.
    result = run_backtest(SMACrossover(20, 50), df, cfg, symbol="AAPL")
    _print_stats("Single backtest: SMA(20,50) on AAPL, 5bps slippage", result.stats)
    assert result.stats["trade_count"] >= 1, "expected at least one trade over 5y"
    png = save_equity_chart(result)
    logger.success(f"Equity/DD chart → {png}")

    # 2. Walk-forward (4 contiguous OOS folds).
    wf = walk_forward(lambda: SMACrossover(20, 50), df, n_splits=4, config=cfg, symbol="AAPL")
    print("\n── Walk-forward (4 OOS folds) ──")
    print(
        wf[["start", "end", "n_bars", "total_return", "sharpe", "max_drawdown", "trade_count"]]
        .to_string()
    )
    # Sanity: per-fold OOS returns should not all be identical (proves we
    # actually evaluated different windows).
    assert wf["total_return"].nunique() > 1, "walk-forward folds collapsed to one outcome"

    # 3. Parameter sensitivity. Grid over (fast, slow). Distribution > best point.
    grid = parameter_sensitivity(
        SMACrossover,
        {"fast": [5, 10, 15, 20, 25], "slow": [30, 50, 80, 120, 200]},
        df,
        config=cfg,
        symbol="AAPL",
    )
    print("\n── Parameter sensitivity: total_return by (fast, slow) ──")
    pivot = grid.pivot(index="fast", columns="slow", values="total_return")
    print(pivot.round(3).to_string())
    print("\n  return distribution stats:")
    rdesc = grid["total_return"].describe()
    print(f"    min  {rdesc['min']:+.2%}   25%  {rdesc['25%']:+.2%}   "
          f"median {rdesc['50%']:+.2%}   75% {rdesc['75%']:+.2%}   max  {rdesc['max']:+.2%}")
    print(f"    mean {rdesc['mean']:+.2%}   std  {rdesc['std']:.2%}   "
          f"frac_positive  {(grid['total_return'] > 0).mean():.0%}")

    logger.info("═══ Phase 5 Verification — all checks passed ✓ ═══")


if __name__ == "__main__":
    main()
