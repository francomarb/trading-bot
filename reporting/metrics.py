"""
Live performance metrics (Step 6).

Computes the five go/no-go metrics from a stream of closed-trade P&Ls:

  1. Sharpe Ratio  — (mean return - risk-free) / std dev of returns
  2. Max Drawdown  — largest peak-to-trough equity drop (as fraction)
  3. Profit Factor — gross profit / gross loss
  4. Win Rate      — winning trades / total trades
  5. Avg Win / Avg Loss ratio

Architecture thresholds (from architecture.md):

  | Metric          | Go/No-Go threshold |
  |-----------------|-------------------|
  | Sharpe Ratio    | > 1.0             |
  | Max Drawdown    | < 15%             |
  | Profit Factor   | > 1.3             |
  | Win Rate        | > 45%             |
  | Avg Win/Avg Loss| > 1.5             |

Design principles:
  - Pure functions of data: every metric is computed from a list of floats
    (per-trade P&L values). No I/O, no state, no side effects.
  - The `MetricsSnapshot` dataclass holds a point-in-time summary. The
    `compute_metrics()` factory builds one from a P&L list.
  - The `meets_go_thresholds()` method checks all five gates at once.
  - Annualization for Sharpe assumes 252 trading days per year.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pandas as pd


# ── Constants ───────────────────────────────────────────────────────────────

TRADING_DAYS_PER_YEAR = 252
DEFAULT_RISK_FREE_RATE = 0.0  # Per-trade, not annualized

# Go/no-go thresholds (from architecture.md)
SHARPE_THRESHOLD = 1.0
MAX_DRAWDOWN_THRESHOLD = 0.15  # 15%
PROFIT_FACTOR_THRESHOLD = 1.3
WIN_RATE_THRESHOLD = 0.45  # 45%
AVG_WIN_LOSS_THRESHOLD = 1.5


# ── Kelly Criterion ─────────────────────────────────────────────────────────


def kelly_fraction(
    returns: "pd.Series",
    risk_free_rate: float = 0.0,
    freq: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """
    Compute the Kelly optimal fraction for a strategy.

    Uses the continuous-time form (Hilpisch, *Python for Algorithmic Trading*,
    Ch. 10):

        f* = (μ - r) / σ²

    where μ and σ² are the *annualized* mean excess return and variance of the
    strategy's period returns.

    Args:
        returns:         Period log or simple return series for the *strategy*
                         (not the benchmark). Typically from the backtest equity
                         curve: ``equity.pct_change().dropna()``.
        risk_free_rate:  Annualized risk-free rate (default 0.0 — appropriate
                         during paper trading when not modelling cash yield).
        freq:            Trading periods per year used for annualization
                         (default 252 for daily bars).

    Returns:
        Full Kelly fraction (float). Practitioners typically deploy *half Kelly*
        (f*/2) to reduce variance. A negative return means the strategy has
        negative expected excess return and should not be traded.
    """
    import pandas as pd  # local import — avoids making pandas a hard dep here

    n = len(returns)
    if n < 2:
        return 0.0

    mu = float(returns.mean() * freq)
    sigma2 = float(returns.var() * freq)
    if sigma2 == 0.0:
        return 0.0
    return (mu - risk_free_rate) / sigma2


# ── MetricsSnapshot ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MetricsSnapshot:
    """Point-in-time performance metrics computed from closed-trade P&Ls."""

    trade_count: int
    sharpe_ratio: float
    max_drawdown_pct: float  # As a fraction, e.g. 0.12 = 12%
    profit_factor: float
    win_rate: float  # As a fraction, e.g. 0.55 = 55%
    avg_win_loss_ratio: float
    total_pnl: float
    mean_pnl: float
    largest_win: float
    largest_loss: float

    def meets_go_thresholds(self, *, min_trades: int = 50) -> tuple[bool, list[str]]:
        """
        Check all five go/no-go gates. Returns (go, reasons).

        `min_trades` enforces the statistical significance requirement
        from architecture.md (default: 50 closed trades).
        """
        reasons: list[str] = []

        if self.trade_count < min_trades:
            reasons.append(
                f"insufficient trades: {self.trade_count} < {min_trades}"
            )

        if self.sharpe_ratio <= SHARPE_THRESHOLD:
            reasons.append(
                f"Sharpe {self.sharpe_ratio:.2f} <= {SHARPE_THRESHOLD}"
            )

        if self.max_drawdown_pct >= MAX_DRAWDOWN_THRESHOLD:
            reasons.append(
                f"max drawdown {self.max_drawdown_pct:.1%} >= "
                f"{MAX_DRAWDOWN_THRESHOLD:.0%}"
            )

        if self.profit_factor <= PROFIT_FACTOR_THRESHOLD:
            reasons.append(
                f"profit factor {self.profit_factor:.2f} <= "
                f"{PROFIT_FACTOR_THRESHOLD}"
            )

        if self.win_rate <= WIN_RATE_THRESHOLD:
            reasons.append(
                f"win rate {self.win_rate:.1%} <= {WIN_RATE_THRESHOLD:.0%}"
            )

        if self.avg_win_loss_ratio <= AVG_WIN_LOSS_THRESHOLD:
            reasons.append(
                f"avg win/loss {self.avg_win_loss_ratio:.2f} <= "
                f"{AVG_WIN_LOSS_THRESHOLD}"
            )

        go = len(reasons) == 0
        return go, reasons

    def format_report(self, *, min_trades: int = 50) -> str:
        """Human-readable summary of all metrics."""
        go, reasons = self.meets_go_thresholds(min_trades=min_trades)
        verdict = "GO" if go else "NO-GO"

        lines = [
            "# Performance Metrics",
            "",
            f"| Metric | Value | Threshold | Status |",
            f"|--------|-------|-----------|--------|",
            f"| Trades | {self.trade_count} | >= {min_trades} | "
            f"{'PASS' if self.trade_count >= min_trades else 'FAIL'} |",
            f"| Sharpe Ratio | {self.sharpe_ratio:.2f} | > {SHARPE_THRESHOLD} | "
            f"{'PASS' if self.sharpe_ratio > SHARPE_THRESHOLD else 'FAIL'} |",
            f"| Max Drawdown | {self.max_drawdown_pct:.1%} | < {MAX_DRAWDOWN_THRESHOLD:.0%} | "
            f"{'PASS' if self.max_drawdown_pct < MAX_DRAWDOWN_THRESHOLD else 'FAIL'} |",
            f"| Profit Factor | {self.profit_factor:.2f} | > {PROFIT_FACTOR_THRESHOLD} | "
            f"{'PASS' if self.profit_factor > PROFIT_FACTOR_THRESHOLD else 'FAIL'} |",
            f"| Win Rate | {self.win_rate:.1%} | > {WIN_RATE_THRESHOLD:.0%} | "
            f"{'PASS' if self.win_rate > WIN_RATE_THRESHOLD else 'FAIL'} |",
            f"| Avg Win/Loss | {self.avg_win_loss_ratio:.2f} | > {AVG_WIN_LOSS_THRESHOLD} | "
            f"{'PASS' if self.avg_win_loss_ratio > AVG_WIN_LOSS_THRESHOLD else 'FAIL'} |",
            "",
            f"| Total P&L | ${self.total_pnl:,.2f} |",
            f"| Mean P&L | ${self.mean_pnl:,.2f} |",
            f"| Largest Win | ${self.largest_win:,.2f} |",
            f"| Largest Loss | ${self.largest_loss:,.2f} |",
            "",
            f"**Verdict: {verdict}**",
        ]

        if reasons:
            lines.append("")
            lines.append("Failures:")
            for r in reasons:
                lines.append(f"  - {r}")

        return "\n".join(lines)


# ── Computation ─────────────────────────────────────────────────────────────


def compute_metrics(
    trade_pnls: list[float],
    *,
    risk_free_rate: float = DEFAULT_RISK_FREE_RATE,
    annualize: bool = True,
) -> MetricsSnapshot:
    """
    Build a MetricsSnapshot from a list of per-trade P&L values.

    Each element in `trade_pnls` is the dollar P&L of one completed
    round-trip trade (positive = profit, negative = loss).

    Parameters:
        trade_pnls: List of per-trade P&L values (dollars).
        risk_free_rate: Per-trade risk-free rate for Sharpe (default 0).
        annualize: If True, annualize Sharpe by sqrt(252). If False, raw.
    """
    n = len(trade_pnls)

    if n == 0:
        return MetricsSnapshot(
            trade_count=0,
            sharpe_ratio=0.0,
            max_drawdown_pct=0.0,
            profit_factor=0.0,
            win_rate=0.0,
            avg_win_loss_ratio=0.0,
            total_pnl=0.0,
            mean_pnl=0.0,
            largest_win=0.0,
            largest_loss=0.0,
        )

    total = sum(trade_pnls)
    mean = total / n

    wins = [p for p in trade_pnls if p > 0]
    losses = [p for p in trade_pnls if p < 0]
    flat = [p for p in trade_pnls if p == 0]

    # ── Sharpe Ratio ────────────────────────────────────────────────────
    excess = [p - risk_free_rate for p in trade_pnls]
    mean_excess = sum(excess) / n
    if n < 2:
        std = 0.0
    else:
        variance = sum((x - mean_excess) ** 2 for x in excess) / (n - 1)
        std = math.sqrt(variance)

    if std == 0:
        sharpe = 0.0
    else:
        sharpe = mean_excess / std
        if annualize:
            sharpe *= math.sqrt(TRADING_DAYS_PER_YEAR)

    # ── Max Drawdown ────────────────────────────────────────────────────
    # Compute as fraction of peak equity, starting from 0.
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for pnl in trade_pnls:
        cumulative += pnl
        if cumulative > peak:
            peak = cumulative
        dd = peak - cumulative
        if peak > 0 and dd / peak > max_dd:
            max_dd = dd / peak

    # ── Profit Factor ───────────────────────────────────────────────────
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss == 0:
        profit_factor = float("inf") if gross_win > 0 else 0.0
    else:
        profit_factor = gross_win / gross_loss

    # ── Win Rate ────────────────────────────────────────────────────────
    win_rate = len(wins) / n

    # ── Avg Win / Avg Loss ──────────────────────────────────────────────
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    if avg_loss == 0:
        avg_win_loss = float("inf") if avg_win > 0 else 0.0
    else:
        avg_win_loss = avg_win / avg_loss

    return MetricsSnapshot(
        trade_count=n,
        sharpe_ratio=round(sharpe, 4),
        max_drawdown_pct=round(max_dd, 4),
        profit_factor=round(profit_factor, 4),
        win_rate=round(win_rate, 4),
        avg_win_loss_ratio=round(avg_win_loss, 4),
        total_pnl=round(total, 2),
        mean_pnl=round(mean, 2),
        largest_win=round(max(trade_pnls), 2),
        largest_loss=round(min(trade_pnls), 2),
    )
