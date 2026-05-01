#!/usr/bin/env python3
"""
Combined-portfolio RSI basket backtest.

This script differs from the existing RSI research reports:

- `scripts/rsi_static_universe.py` ranks symbols by per-symbol backtest results
- `scripts/rsi_backtest_report.py` publishes per-symbol exact backtests
- this script simulates a *shared-capital* RSI sleeve with one equity curve

It answers:

    "How does a real basket behave when RSI symbols compete for the same cash?"

Model assumptions for v1:

- long-only
- shared cash across the whole basket
- equal cash split across new entries on the same day
- max-open-positions cap
- signal at close of t executes at open of t+1
- no ATR stop modeling here; exits come from the exact RSI strategy
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.runner import BacktestConfig
from config.settings import RSI_WATCHLIST
from data.fetcher import fetch_symbols
from strategies.rsi_reversion import RSIReversion


STATIC_RSI_BASKET = [
    "IBM", "ABBV", "CRDO", "WFC", "CVX", "ANET", "IONQ", "CAT", "OXY", "BE",
    "XOM", "RTX", "AXP", "BKNG", "BAC", "GS", "CEG", "LMT", "WMT", "LLY",
    "PG", "LIN", "AMGN", "TMUS",
]

HYBRID2_RSI_BASKET = [
    "IBM", "ABBV", "CRDO", "WFC", "CVX", "ANET", "IONQ", "CAT", "OXY", "BE",
    "XOM", "RTX", "AXP", "BKNG", "BAC", "GS", "CEG", "LMT", "WMT", "LLY",
    "PG", "LIN", "ARM", "SPG",
]


@dataclass(frozen=True)
class Position:
    """One open long position in the combined RSI portfolio."""

    symbol: str
    shares: float
    entry_price: float
    entry_time: pd.Timestamp
    last_mark_price: float


@dataclass(frozen=True)
class ClosedTrade:
    """One completed position round-trip."""

    symbol: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    return_pct: float


@dataclass(frozen=True)
class PortfolioBacktestResult:
    """Combined-basket backtest result with a shared capital pool."""

    basket_name: str
    symbols: list[str]
    equity_curve: pd.Series
    drawdown: pd.Series
    stats: dict[str, float]
    trades: list[ClosedTrade]


def _shift_for_next_open(signal: pd.Series) -> pd.Series:
    """Signal at close of t becomes executable at open of t+1."""
    shifted = signal.shift(1)
    values = [bool(v) if pd.notna(v) else False for v in shifted]
    return pd.Series(values, index=signal.index, dtype=bool)


def build_signal_matrices(
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    symbols: list[str],
    strategy: RSIReversion,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Return aligned open/close/entry/exit matrices for the combined basket."""
    open_map: dict[str, pd.Series] = {}
    close_map: dict[str, pd.Series] = {}
    entry_map: dict[str, pd.Series] = {}
    exit_map: dict[str, pd.Series] = {}

    for symbol in symbols:
        df = bars_by_symbol.get(symbol)
        if df is None or df.empty:
            continue
        clean = df[["open", "close"]].dropna().sort_index()
        if clean.empty:
            continue
        raw = strategy.generate_signals(df[["open", "high", "low", "close", "volume"]], symbol=symbol)
        open_map[symbol] = clean["open"]
        close_map[symbol] = clean["close"]
        entry_map[symbol] = _shift_for_next_open(raw.entries).reindex(clean.index, fill_value=False)
        exit_map[symbol] = _shift_for_next_open(raw.exits).reindex(clean.index, fill_value=False)

    open_df = pd.DataFrame(open_map).sort_index()
    close_df = pd.DataFrame(close_map).sort_index()
    entries_df = pd.DataFrame(entry_map).reindex(open_df.index, fill_value=False)
    exits_df = pd.DataFrame(exit_map).reindex(open_df.index, fill_value=False)
    return open_df, close_df, entries_df, exits_df


def simulate_portfolio_from_signals(
    open_prices: pd.DataFrame,
    close_prices: pd.DataFrame,
    entries: pd.DataFrame,
    exits: pd.DataFrame,
    *,
    basket_name: str,
    config: BacktestConfig,
    max_positions: int,
) -> PortfolioBacktestResult:
    """Simulate a shared-cash long-only basket with next-open fills."""
    if open_prices.empty or close_prices.empty:
        raise ValueError("open_prices and close_prices must be non-empty")
    if not open_prices.index.equals(close_prices.index):
        raise ValueError("open_prices and close_prices must share the same index")
    if not entries.index.equals(open_prices.index) or not exits.index.equals(open_prices.index):
        raise ValueError("entries/exits must share the same index as open_prices")
    if max_positions < 1:
        raise ValueError("max_positions must be >= 1")

    slippage = config.slippage_bps / 10_000.0
    cash = float(config.initial_cash)
    positions: dict[str, Position] = {}
    closed_trades: list[ClosedTrade] = []
    equity_points: list[float] = []
    gross_exposure_points: list[float] = []
    open_position_points: list[float] = []

    for ts in open_prices.index:
        open_row = open_prices.loc[ts]
        close_row = close_prices.loc[ts]
        exit_row = exits.loc[ts]
        entry_row = entries.loc[ts]

        # Exit first so capital freed at the open is immediately reusable.
        for symbol in list(positions.keys()):
            if symbol not in exit_row.index or not bool(exit_row[symbol]):
                continue
            open_px = open_row.get(symbol)
            if pd.isna(open_px):
                continue
            position = positions.pop(symbol)
            fill_px = float(open_px) * (1.0 - slippage)
            proceeds = position.shares * fill_px
            cash += proceeds - config.commission_per_trade
            pnl = (
                (fill_px - position.entry_price) * position.shares
                - (2.0 * config.commission_per_trade)
            )
            invested = position.entry_price * position.shares
            return_pct = pnl / invested if invested > 0 else 0.0
            closed_trades.append(
                ClosedTrade(
                    symbol=symbol,
                    entry_time=position.entry_time,
                    exit_time=ts,
                    entry_price=position.entry_price,
                    exit_price=fill_px,
                    shares=position.shares,
                    pnl=pnl,
                    return_pct=return_pct,
                )
            )

        available_slots = max_positions - len(positions)
        candidate_symbols = [
            symbol
            for symbol in open_prices.columns
            if available_slots > 0
            and symbol not in positions
            and symbol in entry_row.index
            and bool(entry_row[symbol])
            and not pd.isna(open_row.get(symbol))
        ]
        candidate_symbols = candidate_symbols[:available_slots]

        if candidate_symbols:
            alloc_cash = cash / len(candidate_symbols)
            for symbol in candidate_symbols:
                fill_px = float(open_row[symbol]) * (1.0 + slippage)
                spendable = alloc_cash - config.commission_per_trade
                if fill_px <= 0 or spendable <= 0:
                    continue
                shares = spendable / fill_px
                if shares <= 0:
                    continue
                entry_cost = shares * fill_px + config.commission_per_trade
                if entry_cost > cash:
                    shares = max((cash - config.commission_per_trade) / fill_px, 0.0)
                    entry_cost = shares * fill_px + config.commission_per_trade
                if shares <= 0 or entry_cost > cash:
                    continue
                cash -= entry_cost
                positions[symbol] = Position(
                    symbol=symbol,
                    shares=shares,
                    entry_price=fill_px,
                    entry_time=ts,
                    last_mark_price=float(close_row[symbol]),
                )

        gross_exposure = 0.0
        for symbol, position in list(positions.items()):
            close_px = close_row.get(symbol)
            if not pd.isna(close_px):
                positions[symbol] = Position(
                    symbol=position.symbol,
                    shares=position.shares,
                    entry_price=position.entry_price,
                    entry_time=position.entry_time,
                    last_mark_price=float(close_px),
                )
            gross_exposure += positions[symbol].shares * positions[symbol].last_mark_price

        equity = cash + gross_exposure
        equity_points.append(equity)
        gross_exposure_points.append(gross_exposure)
        open_position_points.append(float(len(positions)))

    equity_curve = pd.Series(equity_points, index=open_prices.index, name="equity")
    drawdown = equity_curve / equity_curve.cummax() - 1.0
    stats = _compute_portfolio_stats(
        equity_curve,
        closed_trades,
        initial_cash=config.initial_cash,
        gross_exposure=pd.Series(gross_exposure_points, index=open_prices.index),
        open_positions=pd.Series(open_position_points, index=open_prices.index),
    )
    return PortfolioBacktestResult(
        basket_name=basket_name,
        symbols=list(open_prices.columns),
        equity_curve=equity_curve,
        drawdown=drawdown,
        stats=stats,
        trades=closed_trades,
    )


def _compute_portfolio_stats(
    equity_curve: pd.Series,
    trades: list[ClosedTrade],
    *,
    initial_cash: float,
    gross_exposure: pd.Series,
    open_positions: pd.Series,
) -> dict[str, float]:
    """Compute headline metrics for the shared-capital portfolio."""
    start = equity_curve.index[0]
    end = equity_curve.index[-1]
    n_years = max((end - start).days / 365.25, 1e-9)
    final_equity = float(equity_curve.iloc[-1])
    total_return = final_equity / initial_cash - 1.0
    cagr = (final_equity / initial_cash) ** (1.0 / n_years) - 1.0 if final_equity > 0 else -1.0

    period_returns = equity_curve.pct_change().dropna()
    if len(period_returns) >= 2 and float(period_returns.std(ddof=0)) > 0.0:
        sharpe = float((period_returns.mean() / period_returns.std(ddof=0)) * math.sqrt(252.0))
    else:
        sharpe = float("nan")
    downside = period_returns[period_returns < 0]
    if len(downside) >= 1 and float(downside.std(ddof=0)) > 0.0:
        sortino = float((period_returns.mean() / downside.std(ddof=0)) * math.sqrt(252.0))
    else:
        sortino = float("nan")

    drawdown = equity_curve / equity_curve.cummax() - 1.0
    max_drawdown = float(drawdown.min())
    max_dd_end = drawdown.idxmin()
    max_dd_start = equity_curve.loc[:max_dd_end].idxmax()
    max_dd_days = float((max_dd_end - max_dd_start).days)

    pnls = pd.Series([trade.pnl for trade in trades], dtype=float)
    if not pnls.empty:
        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        gross_profit = float(wins.sum())
        gross_loss = float(losses.sum())
        win_rate = float((pnls > 0).mean())
        expectancy = float(pnls.mean())
        profit_factor = (
            float("inf") if gross_loss == 0.0 and gross_profit > 0.0
            else (gross_profit / abs(gross_loss) if gross_loss != 0.0 else 0.0)
        )
        avg_win = float(wins.mean()) if not wins.empty else 0.0
        avg_loss = float(losses.mean()) if not losses.empty else 0.0
    else:
        win_rate = expectancy = avg_win = avg_loss = 0.0
        profit_factor = 0.0

    utilization = float((gross_exposure / equity_curve.replace(0.0, np.nan)).fillna(0.0).mean())
    avg_open_positions = float(open_positions.mean())

    return {
        "total_return": float(total_return),
        "cagr": float(cagr),
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "max_dd_days": max_dd_days,
        "profit_factor": float(profit_factor),
        "expectancy": float(expectancy),
        "trade_count": float(len(trades)),
        "win_rate": float(win_rate),
        "avg_win": float(avg_win),
        "avg_loss": float(avg_loss),
        "final_equity": float(final_equity),
        "avg_gross_utilization": utilization,
        "avg_open_positions": avg_open_positions,
    }


def save_portfolio_chart(result: PortfolioBacktestResult, out_dir: str | Path) -> Path:
    """Save one equity/drawdown chart for the combined portfolio result."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    ts = pd.Timestamp.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = out_path / f"{ts}_rsi_portfolio_{result.basket_name}.png"

    fig, (ax_eq, ax_dd) = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    ax_eq.plot(result.equity_curve.index, result.equity_curve.values, color="#1f77b4", lw=1.2)
    ax_eq.set_ylabel("Equity ($)")
    ax_eq.set_title(
        f"{result.basket_name} | return {result.stats['total_return']:+.1%} | "
        f"CAGR {result.stats['cagr']:+.1%} | Sharpe {result.stats['sharpe']:.2f} | "
        f"MaxDD {result.stats['max_drawdown']:.1%} | trades {int(result.stats['trade_count'])}"
    )
    ax_eq.grid(alpha=0.3)

    ax_dd.fill_between(result.drawdown.index, result.drawdown.values * 100, 0, color="#d62728", alpha=0.4)
    ax_dd.set_ylabel("Drawdown (%)")
    ax_dd.set_xlabel("Date")
    ax_dd.grid(alpha=0.3)

    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return path


def render_report(results: list[PortfolioBacktestResult], *, feed: str, start: datetime, end: datetime, max_positions: int) -> str:
    """Render a markdown report comparing combined RSI baskets."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# RSI Portfolio Backtest - {generated}",
        "",
        "- This report uses one shared-capital equity curve per basket.",
        "- Entries split available cash equally across same-day new positions.",
        f"- Max simultaneous positions: `{max_positions}`",
        f"- Alpaca feed: `{feed}`",
        f"- Data window: {start.date()} to {end.date()}",
        f"- Data end timestamp: {end.isoformat(timespec='seconds')}",
        "",
        "## Basket Comparison",
        "",
        "| Basket | Symbols | Trades | Return | CAGR | Sharpe | Sortino | MaxDD | MaxDD Days | Win % | PF | Avg Util | Avg Open Pos | Final Equity |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        stats = result.stats
        lines.append(
            f"| {result.basket_name} | {len(result.symbols)} | {int(stats['trade_count'])} | "
            f"{_fmt_pct(stats['total_return'])} | {_fmt_pct(stats['cagr'])} | {_fmt_float(stats['sharpe'])} | "
            f"{_fmt_float(stats['sortino'])} | {_fmt_pct(stats['max_drawdown'])} | {int(stats['max_dd_days'])} | "
            f"{_fmt_pct(stats['win_rate'])} | {_fmt_float(stats['profit_factor'])} | {_fmt_pct(stats['avg_gross_utilization'])} | "
            f"{stats['avg_open_positions']:.2f} | ${stats['final_equity']:,.2f} |"
        )

    lines.extend(["", "## Notes", ""])
    lines.append("- Unlike the per-symbol reports, Sharpe here is a true combined-portfolio Sharpe.")
    lines.append("- This still does not model limit-order queueing or ATR stop legs; exits come from the exact RSI strategy.")
    return "\n".join(lines)


def _fmt_pct(value: float) -> str:
    if math.isnan(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def _fmt_float(value: float) -> str:
    if math.isinf(value):
        return "inf"
    if math.isnan(value):
        return "n/a"
    return f"{value:.2f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a shared-capital RSI basket backtest.")
    parser.add_argument(
        "--baskets",
        nargs="+",
        default=["current", "static", "hybrid2"],
        choices=["current", "static", "hybrid2"],
    )
    parser.add_argument("--lookback-days", type=int, default=1825)
    parser.add_argument("--feed", choices=["iex", "sip"], default="sip")
    parser.add_argument("--end-delay-minutes", type=int, default=60)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("docs/reports/rsi_portfolio_backtest_latest.md"))
    parser.add_argument("--chart-dir", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end = datetime.now(timezone.utc) - timedelta(minutes=args.end_delay_minutes)
    start = end - timedelta(days=args.lookback_days)

    basket_map = {
        "current": list(RSI_WATCHLIST),
        "static": STATIC_RSI_BASKET,
        "hybrid2": HYBRID2_RSI_BASKET,
    }
    all_symbols = list(dict.fromkeys(symbol for basket in args.baskets for symbol in basket_map[basket]))
    bars_by_symbol = fetch_symbols(
        all_symbols,
        start,
        end,
        "1Day",
        adjustment="all",
        feed=args.feed,
        use_cache=True,
    )

    strategy = RSIReversion()
    cfg = BacktestConfig()
    results: list[PortfolioBacktestResult] = []
    for basket_name in args.baskets:
        symbols = basket_map[basket_name]
        basket_bars = {symbol: bars_by_symbol.get(symbol) for symbol in symbols}
        open_df, close_df, entries_df, exits_df = build_signal_matrices(
            basket_bars,
            symbols=symbols,
            strategy=strategy,
        )
        result = simulate_portfolio_from_signals(
            open_df,
            close_df,
            entries_df,
            exits_df,
            basket_name=basket_name,
            config=cfg,
            max_positions=args.max_positions,
        )
        if args.chart_dir is not None:
            save_portfolio_chart(result, args.chart_dir)
        results.append(result)

    report = render_report(results, feed=args.feed, start=start, end=end, max_positions=args.max_positions)
    print(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
