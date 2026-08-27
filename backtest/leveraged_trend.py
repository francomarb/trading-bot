"""Research harness for the underlying-SMA leveraged-ETF trend strategy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import pandas as pd

from backtest.runner import (
    BacktestConfig,
    BacktestResult,
    compute_stats,
    run_backtest,
)
from data.fetcher import fetch_symbol
from monitors.leveraged_trend import drop_in_progress_bar
from strategies.leveraged_trend import LeveragedTrend

SIP_FEED = "sip"


@dataclass(frozen=True)
class LeveragedPair:
    """One unleveraged signal asset and its benchmark-aligned 3x fund."""

    signal_asset: str
    trading_asset: str


RESEARCH_PAIRS: tuple[LeveragedPair, ...] = (
    LeveragedPair("SPY", "SPXL"),
    LeveragedPair("QQQ", "TQQQ"),
    LeveragedPair("XLK", "TECL"),
    LeveragedPair("SOXX", "SOXL"),
)


def build_pair_frame(
    signal_bars: pd.DataFrame,
    trading_bars: pd.DataFrame,
) -> pd.DataFrame:
    """Align signal closes with leveraged-fund OHLCV on common sessions."""
    if signal_bars.empty:
        raise ValueError("signal bars are empty")
    if trading_bars.empty:
        raise ValueError("trading bars are empty")
    if "close" not in signal_bars.columns:
        raise ValueError("signal bars require a 'close' column")

    execution_columns = [
        column
        for column in ("open", "high", "low", "close", "volume")
        if column in trading_bars.columns
    ]
    if "open" not in execution_columns or "close" not in execution_columns:
        raise ValueError("trading bars require 'open' and 'close' columns")
    if not signal_bars.index.is_unique or not trading_bars.index.is_unique:
        raise ValueError("signal and trading bar indexes must be unique")

    signal_columns = [
        column
        for column in ("open", "high", "low", "close", "volume")
        if column in signal_bars.columns
    ]
    signal = signal_bars[signal_columns].rename(
        columns={column: f"signal_{column}" for column in signal_columns}
    )
    frame = trading_bars[execution_columns].join(signal, how="inner")
    frame = frame.sort_index()
    if frame.empty:
        raise ValueError("signal and trading assets have no common sessions")
    if frame[["open", "close", "signal_close"]].isna().any().any():
        raise ValueError("aligned pair frame contains missing prices")
    return frame


def fetch_pair_frame(
    pair: LeveragedPair,
    *,
    start: datetime,
    end: datetime,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Fetch and align one pair from delayed consolidated SIP daily bars."""
    now = now or datetime.now(timezone.utc)
    signal_bars, _ = fetch_symbol(
        pair.signal_asset,
        start,
        end,
        timeframe="1Day",
        adjustment="all",
        feed=SIP_FEED,
    )
    trading_bars, _ = fetch_symbol(
        pair.trading_asset,
        start,
        end,
        timeframe="1Day",
        adjustment="all",
        feed=SIP_FEED,
    )
    signal_bars = drop_in_progress_bar(signal_bars, now=now)
    trading_bars = drop_in_progress_bar(trading_bars, now=now)
    return build_pair_frame(signal_bars, trading_bars)


def run_pair_backtest(
    pair: LeveragedPair,
    frame: pd.DataFrame,
    *,
    sma_length: int = 200,
    entry_days: int = 5,
    exit_days: int = 2,
    config: BacktestConfig | None = None,
) -> BacktestResult:
    """Run one signal-only configuration against one aligned pair frame."""
    strategy = LeveragedTrend(
        sma_length=sma_length,
        entry_days=entry_days,
        exit_days=exit_days,
    )
    return run_backtest(
        strategy,
        frame,
        config=config,
        symbol=f"{pair.signal_asset}->{pair.trading_asset}",
        atr_stop_mult=None,
    )


def parameter_study(
    frames: dict[LeveragedPair, pd.DataFrame],
    *,
    entry_days_values: Iterable[int],
    exit_days_values: Iterable[int],
    sma_length: int = 200,
    config: BacktestConfig | None = None,
) -> pd.DataFrame:
    """Evaluate the confirmation grid independently on every configured pair."""
    rows: list[dict[str, float | int | str | pd.Timestamp]] = []
    for pair, frame in frames.items():
        for entry_days in entry_days_values:
            for exit_days in exit_days_values:
                result = run_pair_backtest(
                    pair,
                    frame,
                    sma_length=sma_length,
                    entry_days=entry_days,
                    exit_days=exit_days,
                    config=config,
                )
                stats = result.stats
                max_drawdown = float(stats["max_drawdown"])
                rows.append(
                    {
                        "signal_asset": pair.signal_asset,
                        "trading_asset": pair.trading_asset,
                        "start": frame.index[0],
                        "end": frame.index[-1],
                        "bars": len(frame),
                        "sma_length": sma_length,
                        "entry_days": entry_days,
                        "exit_days": exit_days,
                        "total_return": float(stats["total_return"]),
                        "cagr": float(stats["cagr"]),
                        "sharpe": float(stats["sharpe"]),
                        "max_drawdown": max_drawdown,
                        "calmar": (
                            float(stats["cagr"]) / abs(max_drawdown)
                            if max_drawdown != 0
                            else float("nan")
                        ),
                        "trade_count": int(stats["trade_count"]),
                        "time_in_market": float(
                            result.portfolio.position_mask().mean()
                        ),
                        "final_equity": float(stats["final_equity"]),
                    }
                )
    if not rows:
        raise ValueError("parameter study requires at least one pair and parameter")
    return pd.DataFrame(rows)


def aggregate_parameter_study(study: pd.DataFrame) -> pd.DataFrame:
    """Summarize robustness across pairs without pretending they diversify."""
    required = {
        "entry_days",
        "exit_days",
        "cagr",
        "sharpe",
        "max_drawdown",
        "calmar",
        "trade_count",
        "time_in_market",
    }
    missing = required - set(study.columns)
    if missing:
        raise ValueError(f"study missing columns: {sorted(missing)}")
    if study.empty:
        raise ValueError("study is empty")

    grouped = study.groupby(["entry_days", "exit_days"], as_index=False)
    aggregate = grouped.agg(
        pair_count=("cagr", "size"),
        median_cagr=("cagr", "median"),
        worst_cagr=("cagr", "min"),
        median_sharpe=("sharpe", "median"),
        # Drawdowns follow vectorbt/project convention and are negative, so
        # the minimum is the deepest loss.
        worst_max_drawdown=("max_drawdown", "min"),
        median_calmar=("calmar", "median"),
        total_trades=("trade_count", "sum"),
        median_time_in_market=("time_in_market", "median"),
    )
    return aggregate.sort_values(
        ["median_calmar", "worst_cagr", "median_sharpe"],
        ascending=[False, False, False],
    ).reset_index(drop=True)


def buy_and_hold_study(
    frames: dict[LeveragedPair, pd.DataFrame],
    *,
    sma_length: int,
    config: BacktestConfig,
) -> pd.DataFrame:
    """Benchmark unleveraged and leveraged buy-and-hold after SMA warmup."""
    import vectorbt as vbt

    rows: list[dict[str, float | str]] = []
    for pair, full_frame in frames.items():
        # Begin where the strategy can first make a fully informed decision;
        # the next-session entry convention makes this the first eligible open.
        frame = full_frame.iloc[sma_length:].copy()
        if frame.empty:
            raise ValueError(f"{pair.signal_asset}: insufficient benchmark bars")
        for asset_kind, symbol, open_col, close_col in (
            ("unleveraged", pair.signal_asset, "signal_open", "signal_close"),
            ("leveraged", pair.trading_asset, "open", "close"),
        ):
            if open_col not in frame or close_col not in frame:
                raise ValueError(f"benchmark frame missing {open_col!r}/{close_col!r}")
            entries = pd.Series(False, index=frame.index, dtype=bool)
            entries.iloc[0] = True
            exits = pd.Series(False, index=frame.index, dtype=bool)
            portfolio = vbt.Portfolio.from_signals(
                close=frame[close_col],
                entries=entries,
                exits=exits,
                price=frame[open_col],
                init_cash=config.initial_cash,
                slippage=config.slippage_bps / 10_000.0,
                fixed_fees=config.commission_per_trade,
                freq=config.freq,
            )
            stats = compute_stats(portfolio, config.initial_cash)
            rows.append(
                {
                    "pair": f"{pair.signal_asset}->{pair.trading_asset}",
                    "asset_kind": asset_kind,
                    "symbol": symbol,
                    "cagr": float(stats["cagr"]),
                    "sharpe": float(stats["sharpe"]),
                    "max_drawdown": float(stats["max_drawdown"]),
                    "final_equity": float(stats["final_equity"]),
                }
            )
    return pd.DataFrame(rows)


def candidate_period_study(
    frames: dict[LeveragedPair, pd.DataFrame],
    *,
    periods: dict[str, tuple[str, str]],
    sma_length: int,
    entry_days: int,
    exit_days: int,
    config: BacktestConfig,
) -> pd.DataFrame:
    """Measure a candidate's continuous equity path inside stress windows."""
    rows: list[dict[str, float | int | str]] = []
    for pair, frame in frames.items():
        result = run_pair_backtest(
            pair,
            frame,
            sma_length=sma_length,
            entry_days=entry_days,
            exit_days=exit_days,
            config=config,
        )
        equity = result.equity_curve()
        invested = result.portfolio.position_mask()
        for period_name, (start, end) in periods.items():
            window = equity.loc[start:end]
            if len(window) < 2:
                continue
            drawdown = window / window.cummax() - 1.0
            rows.append(
                {
                    "signal_asset": pair.signal_asset,
                    "trading_asset": pair.trading_asset,
                    "period": period_name,
                    "start": window.index[0].date().isoformat(),
                    "end": window.index[-1].date().isoformat(),
                    "return": float(window.iloc[-1] / window.iloc[0] - 1.0),
                    "max_drawdown": float(drawdown.min()),
                    "time_in_market": float(invested.loc[window.index].mean()),
                }
            )
    return pd.DataFrame(rows)


def _markdown_table(frame: pd.DataFrame) -> str:
    """Render a DataFrame as Markdown without pandas' optional tabulate dep."""
    columns = [str(column) for column in frame.columns]

    def _cell(value: object) -> str:
        text = str(value)
        return text.replace("|", "\\|").replace("\n", " ")

    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for row in frame.itertuples(index=False, name=None):
        lines.append("| " + " | ".join(_cell(value) for value in row) + " |")
    return "\n".join(lines)


def write_study_report(
    study: pd.DataFrame,
    aggregate: pd.DataFrame,
    *,
    output_path: str | Path,
    config: BacktestConfig,
    benchmarks: pd.DataFrame | None = None,
    candidate_periods: pd.DataFrame | None = None,
    candidate_entry_days: int = 5,
    candidate_exit_days: int = 2,
) -> Path:
    """Write a compact, reproducible Markdown summary of a completed study."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    coverage = (
        study.groupby(["signal_asset", "trading_asset"], as_index=False)
        .agg(start=("start", "min"), end=("end", "max"), bars=("bars", "max"))
    )
    top = aggregate.head(10).copy()
    pct_columns = [
        "median_cagr",
        "worst_cagr",
        "worst_max_drawdown",
        "median_time_in_market",
    ]
    for column in pct_columns:
        top[column] = top[column].map(lambda value: f"{value:.1%}")
    for column in ("median_sharpe", "median_calmar"):
        top[column] = top[column].map(lambda value: f"{value:.2f}")

    pair_best = (
        study.sort_values(
            ["signal_asset", "calmar", "cagr", "sharpe"],
            ascending=[True, False, False, False],
        )
        .groupby("signal_asset", as_index=False)
        .head(5)
        .copy()
    )
    for column in ("cagr", "max_drawdown", "time_in_market"):
        pair_best[column] = pair_best[column].map(lambda value: f"{value:.1%}")
    for column in ("sharpe", "calmar"):
        pair_best[column] = pair_best[column].map(lambda value: f"{value:.2f}")

    benchmark_table = None
    if benchmarks is not None:
        benchmark_table = benchmarks.copy()
        for column in ("cagr", "max_drawdown"):
            benchmark_table[column] = benchmark_table[column].map(
                lambda value: f"{value:.1%}"
            )
        benchmark_table["sharpe"] = benchmark_table["sharpe"].map(
            lambda value: f"{value:.2f}"
        )
        benchmark_table["final_equity"] = benchmark_table["final_equity"].map(
            lambda value: f"${value:,.0f}"
        )

    period_table = None
    if candidate_periods is not None:
        period_table = candidate_periods.copy()
        for column in ("return", "max_drawdown", "time_in_market"):
            period_table[column] = period_table[column].map(
                lambda value: f"{value:.1%}"
            )

    lines = [
        "# Leveraged trend confirmation study",
        "",
        "This is a research result, not an activated paper or live strategy.",
        "",
        "## Contract",
        "",
        "- Signal: adjusted daily close of the unleveraged benchmark ETF versus its SMA.",
        "- Entry: configurable consecutive closes strictly above the SMA.",
        "- Exit: configurable consecutive closes strictly below the SMA.",
        "- Execution: next common session open of the 3x ETF.",
        "- Inactive state: cash.",
        "- Stops and tax rules: excluded from the baseline.",
        f"- Data: Alpaca SIP, adjustment=all; slippage={config.slippage_bps:g} bps.",
        "",
        "## Coverage",
        "",
        _markdown_table(coverage),
        "",
        "The first executable signal occurs only after the SMA warmup and the full entry confirmation. Coverage dates above include warmup bars.",
        "",
        "## Buy-and-hold context",
        "",
        "Benchmarks enter at the first open after the 200-session warmup, so they do not receive an extra pre-strategy year.",
        "",
        _markdown_table(benchmark_table) if benchmark_table is not None else "Not generated.",
        "",
        "## Cross-pair parameter summary",
        "",
        "Ranking uses median Calmar only as a navigation aid. The pairs are highly correlated and are not treated as independent evidence.",
        "",
        _markdown_table(top),
        "",
        "## Best five configurations within each pair",
        "",
        _markdown_table(pair_best[
            [
                "signal_asset",
                "trading_asset",
                "entry_days",
                "exit_days",
                "cagr",
                "sharpe",
                "max_drawdown",
                "calmar",
                "trade_count",
                "time_in_market",
            ]
        ]),
        "",
        f"## Stress windows for the {candidate_entry_days}/{candidate_exit_days} candidate",
        "",
        "These are slices of the continuous full-period equity path; strategy state is not reset at the start of each window.",
        "",
        _markdown_table(period_table) if period_table is not None else "Not generated.",
        "",
        "## Interpretation guardrails",
        "",
        "- Prefer a stable neighborhood of confirmation values over the top row.",
        "- This table is not a combined-portfolio simulation and does not resolve correlated sleeve sizing.",
        "- A final parameter choice requires period-by-period and untouched holdout validation.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
