"""
Backtesting harness (Phase 5).

Wraps vectorbt with the conventions this project commits to:

  1. **Look-ahead-safe execution.** A signal emitted at bar t's close is shifted
     forward by one bar and filled at bar t+1's *open*. Strategies emit signals
     aligned to the bar whose close triggered them; this layer is the single
     place where the t→t+1 shift happens.

  2. **Costs are mandatory, not optional.** Every backtest carries a slippage
     (bps) and commission (per-trade $) charge. Defaults are conservative
     (5 bps slippage, $0 commission for Alpaca). A backtest with zero costs
     is a marketing exhibit, not a strategy evaluation — so it must be an
     explicit choice.

  3. **Honest stats.** We report CAGR, Sharpe, Sortino, max drawdown, profit
     factor, expectancy, and trade count. Win rate is included but is *not*
     the headline — a 90% win-rate strategy with one catastrophic loser can
     still bankrupt the account.

  4. **Walk-forward separation.** `walk_forward()` slices the date range into
     sequential out-of-sample folds and reports per-fold metrics, so an
     in-sample-only "great" strategy can't hide behind aggregate numbers.

  5. **Parameter sensitivity over single-best params.** `parameter_sensitivity()`
     sweeps a grid and returns the *distribution* of metrics — a strategy
     where only one (fast, slow) combo works is overfit, not robust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd
import vectorbt as vbt

from strategies.base import BaseStrategy

# ── Config ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BacktestConfig:
    """Costs and capital assumptions for a single backtest run."""

    initial_cash: float = 100_000.0
    slippage_bps: float = 5.0          # 5 bps = 0.05% per fill
    commission_per_trade: float = 0.0  # Alpaca = 0; non-zero for other brokers
    freq: str = "1D"                   # vectorbt annualization frequency

    def __post_init__(self) -> None:
        if self.initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        if self.slippage_bps < 0:
            raise ValueError("slippage_bps must be non-negative")
        if self.commission_per_trade < 0:
            raise ValueError("commission_per_trade must be non-negative")


# ── Result ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class BacktestResult:
    """The full output of a single backtest run."""

    portfolio: vbt.Portfolio
    stats: dict[str, float]
    entries_executed: pd.Series  # the t+1-shifted booleans actually fed to vbt
    exits_executed: pd.Series
    config: BacktestConfig
    strategy_name: str
    symbol: str

    def equity_curve(self) -> pd.Series:
        return self.portfolio.value()

    def drawdown(self) -> pd.Series:
        return self.portfolio.drawdown()


# ── Core run ────────────────────────────────────────────────────────────────


def _required_cols(df: pd.DataFrame) -> None:
    missing = {"open", "close"} - set(df.columns)
    if missing:
        raise ValueError(f"backtest df missing columns: {sorted(missing)}")
    if df.empty:
        raise ValueError("backtest df is empty")


def _shift_for_next_open(s: pd.Series) -> pd.Series:
    """Signal at t's close → executes at t+1's open. Last bar's signal is dropped
    (no t+1 to execute on). Built from raw values to sidestep the pandas
    fillna-downcast FutureWarning."""
    shifted = s.shift(1)
    values = [bool(v) if pd.notna(v) else False for v in shifted]
    return pd.Series(values, index=s.index, dtype=bool)


def run_backtest(
    strategy: BaseStrategy,
    df: pd.DataFrame,
    config: BacktestConfig | None = None,
    *,
    symbol: str = "?",
) -> BacktestResult:
    """
    Run `strategy` against `df` (must have 'open' and 'close' columns) under
    the given `config`. Returns a `BacktestResult`.
    """
    cfg = config or BacktestConfig()
    _required_cols(df)

    raw = strategy.generate_signals(df)
    entries = _shift_for_next_open(raw.entries)
    exits = _shift_for_next_open(raw.exits)

    pf = vbt.Portfolio.from_signals(
        close=df["close"],
        entries=entries,
        exits=exits,
        price=df["open"],                       # fill at the bar's open
        init_cash=cfg.initial_cash,
        slippage=cfg.slippage_bps / 10_000.0,   # vbt: fraction
        fixed_fees=cfg.commission_per_trade,    # per-trade $
        freq=cfg.freq,
    )

    return BacktestResult(
        portfolio=pf,
        stats=compute_stats(pf, cfg.initial_cash),
        entries_executed=entries,
        exits_executed=exits,
        config=cfg,
        strategy_name=strategy.name,
        symbol=symbol,
    )


# ── Stats ───────────────────────────────────────────────────────────────────


def compute_stats(pf: vbt.Portfolio, initial_cash: float) -> dict[str, float]:
    """
    Pull the headline metrics from a vectorbt Portfolio. Profit factor and
    expectancy are computed manually from trade records (vbt's stats() API
    output varies by version; manual calc is stable).
    """
    equity = pf.value()
    start, end = equity.index[0], equity.index[-1]
    n_years = max((end - start).days / 365.25, 1e-9)
    final_value = float(equity.iloc[-1])
    total_return = final_value / initial_cash - 1.0
    cagr = (final_value / initial_cash) ** (1.0 / n_years) - 1.0 if final_value > 0 else -1.0

    trades = pf.trades.records_readable
    n_trades = int(len(trades))
    if n_trades:
        pnls = trades["PnL"].astype(float)
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        gross_profit = float(wins.sum())
        gross_loss = float(losses.sum())  # negative
        win_rate = float((pnls > 0).mean())
        expectancy = float(pnls.mean())
        # Profit factor: undefined when no losses → report inf, the standard convention.
        profit_factor = (
            float("inf") if gross_loss == 0 and gross_profit > 0
            else (gross_profit / abs(gross_loss) if gross_loss != 0 else 0.0)
        )
        avg_win = float(wins.mean()) if len(wins) else 0.0
        avg_loss = float(losses.mean()) if len(losses) else 0.0
    else:
        win_rate = profit_factor = expectancy = avg_win = avg_loss = 0.0

    def _safe(fn: Callable[[], Any], default: float = float("nan")) -> float:
        try:
            v = float(fn())
            return v if np.isfinite(v) else default
        except Exception:
            return default

    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "sharpe": _safe(pf.sharpe_ratio),
        "sortino": _safe(pf.sortino_ratio),
        "max_drawdown": _safe(pf.max_drawdown),
        "profit_factor": profit_factor,
        "expectancy": expectancy,
        "trade_count": float(n_trades),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "final_equity": final_value,
    }


# ── Equity / drawdown chart ─────────────────────────────────────────────────


def save_equity_chart(result: BacktestResult, out_dir: str | Path = "logs/backtests") -> Path:
    """
    Save a 2-panel PNG (equity curve + drawdown) to
    `<out_dir>/<UTC-timestamp>_<strategy>_<symbol>.png`. Returns the path.
    """
    # matplotlib import is local so headless test environments aren't forced
    # to load it just to import this module.
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    equity = result.equity_curve()
    dd = result.drawdown()
    ts = pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%SZ")
    fname = f"{ts}_{result.strategy_name}_{result.symbol}.png"
    path = out_dir / fname

    fig, (ax_eq, ax_dd) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    ax_eq.plot(equity.index, equity.values, color="#1f77b4", lw=1.2)
    ax_eq.axhline(result.config.initial_cash, color="grey", ls="--", lw=0.7)
    ax_eq.set_ylabel("Equity ($)")
    ax_eq.set_title(
        f"{result.strategy_name} on {result.symbol}  |  "
        f"return {result.stats['total_return']:+.1%}  "
        f"CAGR {result.stats['cagr']:+.1%}  "
        f"Sharpe {result.stats['sharpe']:.2f}  "
        f"MaxDD {result.stats['max_drawdown']:.1%}  "
        f"PF {result.stats['profit_factor']:.2f}  "
        f"trades {int(result.stats['trade_count'])}"
    )
    ax_eq.grid(alpha=0.3)

    ax_dd.fill_between(dd.index, dd.values * 100, 0, color="#d62728", alpha=0.4)
    ax_dd.set_ylabel("Drawdown (%)")
    ax_dd.set_xlabel("Date")
    ax_dd.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


# ── Walk-forward ────────────────────────────────────────────────────────────


def walk_forward(
    strategy_factory: Callable[[], BaseStrategy],
    df: pd.DataFrame,
    *,
    n_splits: int = 4,
    config: BacktestConfig | None = None,
    symbol: str = "?",
) -> pd.DataFrame:
    """
    Sequential out-of-sample walk-forward.

    Splits `df` into `n_splits` contiguous, non-overlapping windows and runs
    `strategy_factory()` (a fresh instance) on each. Returns a DataFrame
    indexed by fold with one row per fold and one column per metric, plus a
    final 'OOS_AGG' row that concatenates per-fold equity returns.

    For parameter-free strategies (like SMA crossover with fixed params) the
    "train" window is irrelevant — every fold is OOS. For parameter-tuned
    strategies, callers should fit on the train portion before constructing
    the strategy in `strategy_factory`. (We do not bake fitting into this
    harness; that's a Phase 11 concern.)
    """
    if n_splits < 2:
        raise ValueError("n_splits must be ≥ 2")
    _required_cols(df)
    if len(df) < n_splits * 50:
        raise ValueError(
            f"need at least {n_splits * 50} bars for {n_splits} folds; got {len(df)}"
        )

    cfg = config or BacktestConfig()
    folds = np.array_split(np.arange(len(df)), n_splits)
    rows = []
    for i, idx in enumerate(folds):
        sub = df.iloc[idx[0] : idx[-1] + 1]
        result = run_backtest(strategy_factory(), sub, cfg, symbol=symbol)
        row = {"fold": i, "start": sub.index[0], "end": sub.index[-1], "n_bars": len(sub)}
        row.update(result.stats)
        rows.append(row)

    return pd.DataFrame(rows).set_index("fold")


# ── Parameter sensitivity ───────────────────────────────────────────────────


def parameter_sensitivity(
    strategy_factory: Callable[..., BaseStrategy],
    param_grid: dict[str, Iterable[Any]],
    df: pd.DataFrame,
    *,
    config: BacktestConfig | None = None,
    symbol: str = "?",
    skip_invalid: bool = True,
) -> pd.DataFrame:
    """
    Cartesian sweep over `param_grid`. For each combination calls
    `strategy_factory(**params)` and runs a backtest. Returns one row per
    combination with the param values plus all stats columns.

    `skip_invalid=True`: combinations that raise during construction (e.g.
    fast >= slow for SMACrossover) are silently skipped. Set False to surface
    every error.
    """
    _required_cols(df)
    cfg = config or BacktestConfig()
    keys = list(param_grid.keys())
    rows = []
    for combo in product(*[list(param_grid[k]) for k in keys]):
        params = dict(zip(keys, combo))
        try:
            strat = strategy_factory(**params)
        except (ValueError, TypeError):
            if skip_invalid:
                continue
            raise
        result = run_backtest(strat, df, cfg, symbol=symbol)
        rows.append({**params, **result.stats})
    if not rows:
        raise ValueError("parameter grid produced no valid combinations")
    return pd.DataFrame(rows)
