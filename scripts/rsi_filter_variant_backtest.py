#!/usr/bin/env python3
"""Production-style RSI filter variant backtest.

This is a research harness for comparing RSI edge-filter variants using the
same shared-capital assumptions as ``scripts/rsi_portfolio_backtest.py`` plus
the ATR protective stop used by the live bot.
Unlike the older RSI reports, entries are gated by historical equivalents of
the live filters on each signal bar:

- regime gate: allow only TRENDING/RANGING, block BEAR/VOLATILE
- RSI-specific SPY 50 SMA gate mode (hard, removed, tolerance band, or grace)
- earnings blackout, fail-open when earnings data is unavailable
- 20-day average dollar-volume liquidity floor
- sector momentum block at score <= -3
- configurable stock breakdown gate
"""

from __future__ import annotations

import argparse
import contextlib
import math
import os
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
from config import settings
from data.fetcher import fetch_symbols
from indicators.technicals import add_adx, add_atr, add_rsi, add_sma
from scripts.rsi_portfolio_backtest import (
    ClosedTrade,
    HYBRID2_RSI_BASKET,
    Position,
    STATIC_RSI_BASKET,
    PortfolioBacktestResult,
    _compute_portfolio_stats,
)
from strategies.base import EdgeFilterDecision, SignalFrame
from strategies.rsi_reversion import RSIReversion


@dataclass(frozen=True)
class Variant:
    name: str
    breakdown_mode: str
    spy_gate_mode: str = "hard_50"


@dataclass(frozen=True)
class StoppedPosition(Position):
    """One open long position with the bot's ATR protective stop attached."""

    stop_price: float


class HistoricalRSIFilter:
    """Per-bar historical equivalent of the live RSI filter stack."""

    def __init__(
        self,
        *,
        spy_gate: pd.Series,
        regime_allowed: pd.Series,
        sector_scores: dict[str, pd.Series],
        sector_by_symbol: dict[str, str | None],
        earnings_by_symbol: dict[str, list[pd.Timestamp]],
        breakdown_mode: str,
        notional_min_avg: float = 10_000_000.0,
        sector_score_threshold: float = -3.0,
        days_before: int = 3,
        days_after: int = 2,
    ) -> None:
        self._spy_gate = spy_gate.astype(bool)
        self._regime_allowed = regime_allowed.astype(bool)
        self._sector_scores = sector_scores
        self._sector_by_symbol = sector_by_symbol
        self._earnings_by_symbol = earnings_by_symbol
        self._breakdown_mode = breakdown_mode
        self._notional_min_avg = float(notional_min_avg)
        self._sector_score_threshold = float(sector_score_threshold)
        self._days_before = int(days_before)
        self._days_after = int(days_after)
        self._symbol = ""

    def set_symbol(self, symbol: str) -> None:
        self._symbol = symbol

    def __call__(self, df: pd.DataFrame) -> EdgeFilterDecision:
        idx = df.index
        close = df["close"].astype(float)
        volume = df["volume"].astype(float) if "volume" in df.columns else None

        regime = self._regime_allowed.reindex(idx, method="ffill").fillna(False)
        spy = self._spy_gate.reindex(idx, method="ffill").fillna(False)
        liquid = pd.Series(True, index=idx, dtype=bool)
        if volume is not None:
            avg_dollar = (close * volume).rolling(20).mean()
            liquid = (avg_dollar >= self._notional_min_avg).where(avg_dollar.notna(), True)

        breakdown = _breakdown_gate(close, self._breakdown_mode)
        earnings = _earnings_gate(
            idx,
            self._earnings_by_symbol.get(self._symbol, []),
            days_before=self._days_before,
            days_after=self._days_after,
        )
        sector = self._sector_gate(idx)

        allowed = (regime & spy & liquid & breakdown & earnings & sector).astype(bool)
        reasons: list[list[str]] = []
        for values in zip(
            regime.tolist(),
            spy.tolist(),
            liquid.tolist(),
            breakdown.tolist(),
            earnings.tolist(),
            sector.tolist(),
            strict=False,
        ):
            row_reasons: list[str] = []
            if not values[0]:
                row_reasons.append("regime blocked")
            if not values[1]:
                row_reasons.append("SPY gate blocked")
            if not values[2]:
                row_reasons.append("liquidity below floor")
            if not values[3]:
                row_reasons.append(f"breakdown gate blocked ({self._breakdown_mode})")
            if not values[4]:
                row_reasons.append("earnings blackout")
            if not values[5]:
                row_reasons.append("cold sector")
            reasons.append(row_reasons)

        return EdgeFilterDecision(
            allowed=allowed,
            reasons=pd.Series(reasons, index=idx, dtype=object),
        )

    def _sector_gate(self, idx: pd.Index) -> pd.Series:
        sector = self._sector_by_symbol.get(self._symbol)
        if sector is None:
            return pd.Series(True, index=idx, dtype=bool)
        scores = self._sector_scores.get(sector)
        if scores is None:
            return pd.Series(True, index=idx, dtype=bool)
        score = scores.reindex(idx, method="ffill")
        return (score > self._sector_score_threshold).where(score.notna(), True).astype(bool)


class HistoricalFilteredRSI(RSIReversion):
    """RSI strategy whose entries are gated by ``HistoricalRSIFilter``."""

    def inspect_signals(
        self,
        df: pd.DataFrame,
        *,
        symbol: str | None = None,
    ) -> tuple[SignalFrame, SignalFrame, bool | None, list[str]]:
        return super().inspect_signals(df, symbol=symbol)


def _breakdown_gate(close: pd.Series, mode: str) -> pd.Series:
    if mode == "none":
        return pd.Series(True, index=close.index, dtype=bool)
    if mode == "new_low_20":
        prior_min = close.shift(1).rolling(20).min()
        return (close > prior_min).where(prior_min.notna(), True).astype(bool)
    if mode == "new_low_60":
        prior_min = close.shift(1).rolling(60).min()
        return (close > prior_min).where(prior_min.notna(), True).astype(bool)
    if mode == "new_low_20_and_below_sma200":
        prior_min = close.shift(1).rolling(20).min()
        new_low = (close <= prior_min).where(prior_min.notna(), False)
        sma200 = close.rolling(200).mean()
        below_200 = (close < sma200).where(sma200.notna(), False)
        return (~(new_low & below_200)).astype(bool)
    raise ValueError(f"unknown breakdown mode: {mode}")


def _earnings_gate(
    idx: pd.Index,
    earnings_dates: list[pd.Timestamp],
    *,
    days_before: int,
    days_after: int,
) -> pd.Series:
    if not earnings_dates:
        return pd.Series(True, index=idx, dtype=bool)
    blocked = pd.Series(False, index=idx, dtype=bool)
    normalized_idx = pd.Series(pd.to_datetime(idx).date, index=idx)
    for earnings_date in earnings_dates:
        ed = earnings_date.date()
        delta = normalized_idx.map(lambda d: (d - ed).days)
        blocked |= delta.between(-days_before, days_after)
    return (~blocked).astype(bool)


def _fetch_earnings_dates(symbols: list[str]) -> dict[str, list[pd.Timestamp]]:
    """Fetch historical earnings dates via yfinance, fail-open per live policy."""
    out: dict[str, list[pd.Timestamp]] = {}
    try:
        import yfinance as yf
    except Exception:
        return {symbol: [] for symbol in symbols}
    for symbol in symbols:
        dates: list[pd.Timestamp] = []
        try:
            with open(os.devnull, "w") as devnull:
                with contextlib.redirect_stdout(devnull), contextlib.redirect_stderr(devnull):
                    hist = yf.Ticker(symbol).earnings_dates
            if hist is not None and not hist.empty:
                dates = [pd.Timestamp(ts) for ts in hist.index]
        except Exception:
            dates = []
        out[symbol] = dates
    return out


def _compute_regime_allowed(spy: pd.DataFrame) -> pd.Series:
    """Vectorized equivalent of RegimeDetector for historical bars."""
    work = add_sma(spy.copy(), 200)
    work = add_sma(work, 50)
    work = add_atr(work, 14)
    work = add_adx(work, 14)
    close = work["close"].astype(float)
    sma200 = work["sma_200"].astype(float)
    bear = close < sma200

    atr_pct = work["atr_14"].astype(float) / close
    percentile = atr_pct.rolling(126, min_periods=10).apply(
        lambda x: float((x[:-1] < x[-1]).mean()) if len(x) else np.nan,
        raw=True,
    )
    volatile = (percentile >= 0.80) & (atr_pct >= 0.012)
    return (~(bear | volatile)).where(sma200.notna(), False).astype(bool)


def _compute_spy50_gate(spy: pd.DataFrame) -> pd.Series:
    work = add_sma(spy.copy(), 50)
    return (work["close"].astype(float) > work["sma_50"].astype(float)).where(
        work["sma_50"].notna(), False
    ).astype(bool)


def _compute_spy_gate(spy: pd.DataFrame, mode: str) -> pd.Series:
    work = add_sma(spy.copy(), 50)
    close = work["close"].astype(float)
    sma50 = work["sma_50"].astype(float)
    if mode == "hard_50":
        gate = close > sma50
    elif mode == "none":
        gate = pd.Series(True, index=work.index, dtype=bool)
    elif mode == "band_1pct":
        gate = close >= sma50 * 0.99
    elif mode == "grace_3d":
        gate = (close > sma50).rolling(3, min_periods=1).max().fillna(0).astype(bool)
    else:
        raise ValueError(f"unknown SPY gate mode: {mode}")
    return gate.where(sma50.notna(), False).astype(bool)


def _compute_sector_scores(bars_by_symbol: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
    scores: dict[str, pd.Series] = {}
    for sector, ticker in settings.SECTOR_ETFS.items():
        df = bars_by_symbol.get(ticker)
        if df is None or df.empty or len(df) < 200:
            continue
        work = add_sma(df.copy(), 200)
        work = add_sma(work, 50)
        close = work["close"].astype(float)
        sma50 = work["sma_50"].astype(float)
        sma200 = work["sma_200"].astype(float)
        dist = ((close - sma50) / sma50.replace(0, np.nan)).fillna(0.0)
        vol = work["volume"].astype(float)
        raw = (
            np.where(close > sma200, 1, -1)
            + np.where(close > sma50, 1, -1)
            + np.where(sma50 > sma200, 1, -1)
            + np.where(dist > 0.02, 1, 0)
            - np.where(dist < -0.02, 1, 0)
            + np.where(vol.rolling(10).mean() > vol.rolling(20).mean(), 1, 0)
        )
        series = pd.Series(raw, index=work.index, dtype=float)
        series[work["sma_200"].isna() | work["sma_50"].isna()] = np.nan
        scores[sector] = series.rolling(settings.SECTOR_MOMENTUM_SMOOTH_WINDOW).mean().round(1)
    return scores


def _sector_mapping(symbols: list[str]) -> dict[str, str | None]:
    from sector.resolver import SectorResolver

    resolver = SectorResolver(valid_sectors=set(settings.SECTOR_ETFS))
    resolver.hydrate(symbols)
    return {symbol: resolver.resolve(symbol) for symbol in symbols}


def _shift_for_next_open(signal: pd.Series) -> pd.Series:
    """Signal at close of t becomes executable at open of t+1."""
    shifted = signal.shift(1)
    values = [bool(v) if pd.notna(v) else False for v in shifted]
    return pd.Series(values, index=signal.index, dtype=bool)


def _build_stop_aware_matrices(
    bars_by_symbol: dict[str, pd.DataFrame | None],
    *,
    symbols: list[str],
    strategy: RSIReversion,
) -> tuple[
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
    pd.DataFrame,
]:
    """Return aligned open/low/close/entry/limit/exit/stop matrices."""
    open_map: dict[str, pd.Series] = {}
    low_map: dict[str, pd.Series] = {}
    close_map: dict[str, pd.Series] = {}
    entry_map: dict[str, pd.Series] = {}
    limit_map: dict[str, pd.Series] = {}
    exit_map: dict[str, pd.Series] = {}
    stop_map: dict[str, pd.Series] = {}

    for symbol in symbols:
        df = bars_by_symbol.get(symbol)
        if df is None or df.empty:
            continue
        clean = df[["open", "high", "low", "close", "volume"]].dropna().sort_index()
        if clean.empty:
            continue
        raw = strategy.generate_signals(clean, symbol=symbol)
        with_atr = add_atr(clean.copy(), settings.ATR_LENGTH)
        stop_on_signal_bar = (
            with_atr["close"].astype(float)
            - settings.ATR_STOP_MULTIPLIER * with_atr[f"atr_{settings.ATR_LENGTH}"].astype(float)
        )

        open_map[symbol] = clean["open"]
        low_map[symbol] = clean["low"]
        close_map[symbol] = clean["close"]
        entry_map[symbol] = _shift_for_next_open(raw.entries).reindex(clean.index, fill_value=False)
        limit_map[symbol] = clean["close"].where(raw.entries).shift(1).reindex(clean.index)
        exit_map[symbol] = _shift_for_next_open(raw.exits).reindex(clean.index, fill_value=False)
        stop_map[symbol] = stop_on_signal_bar.shift(1).reindex(clean.index)

    open_df = pd.DataFrame(open_map).sort_index()
    low_df = pd.DataFrame(low_map).reindex(open_df.index)
    close_df = pd.DataFrame(close_map).reindex(open_df.index)
    entries_df = pd.DataFrame(entry_map).reindex(open_df.index, fill_value=False)
    limits_df = pd.DataFrame(limit_map).reindex(open_df.index)
    exits_df = pd.DataFrame(exit_map).reindex(open_df.index, fill_value=False)
    stops_df = pd.DataFrame(stop_map).reindex(open_df.index)
    return open_df, low_df, close_df, entries_df, limits_df, exits_df, stops_df


def _simulate_stop_aware_portfolio(
    open_prices: pd.DataFrame,
    low_prices: pd.DataFrame,
    close_prices: pd.DataFrame,
    entries: pd.DataFrame,
    entry_limits: pd.DataFrame,
    exits: pd.DataFrame,
    stop_prices: pd.DataFrame,
    *,
    basket_name: str,
    config: BacktestConfig,
    max_positions: int,
) -> PortfolioBacktestResult:
    """Simulate shared cash, limit entries, RSI exits, and ATR stops."""
    if open_prices.empty or close_prices.empty:
        raise ValueError("open_prices and close_prices must be non-empty")
    for name, frame in {
        "low_prices": low_prices,
        "close_prices": close_prices,
        "entries": entries,
        "entry_limits": entry_limits,
        "exits": exits,
        "stop_prices": stop_prices,
    }.items():
        if not frame.index.equals(open_prices.index):
            raise ValueError(f"{name} must share the same index as open_prices")
    if max_positions < 1:
        raise ValueError("max_positions must be >= 1")

    slippage = config.slippage_bps / 10_000.0
    cash = float(config.initial_cash)
    positions: dict[str, StoppedPosition] = {}
    closed_trades: list[ClosedTrade] = []
    equity_points: list[float] = []
    gross_exposure_points: list[float] = []
    open_position_points: list[float] = []

    for ts in open_prices.index:
        open_row = open_prices.loc[ts]
        low_row = low_prices.loc[ts]
        close_row = close_prices.loc[ts]
        exit_row = exits.loc[ts]
        entry_row = entries.loc[ts]
        limit_row = entry_limits.loc[ts]
        stop_row = stop_prices.loc[ts]

        for symbol in list(positions.keys()):
            position = positions[symbol]
            open_px = open_row.get(symbol)
            low_px = low_row.get(symbol)
            if pd.isna(open_px) or pd.isna(low_px):
                continue

            stop_hit = float(low_px) <= position.stop_price
            exit_hit = symbol in exit_row.index and bool(exit_row[symbol])
            if not stop_hit and not exit_hit:
                continue

            positions.pop(symbol)
            if stop_hit:
                raw_fill = min(float(open_px), position.stop_price)
            else:
                raw_fill = float(open_px)
            fill_px = raw_fill * (1.0 - slippage)
            proceeds = position.shares * fill_px
            cash += proceeds - config.commission_per_trade
            pnl = (
                (fill_px - position.entry_price) * position.shares
                - (2.0 * config.commission_per_trade)
            )
            invested = position.entry_price * position.shares
            closed_trades.append(
                ClosedTrade(
                    symbol=symbol,
                    entry_time=position.entry_time,
                    exit_time=ts,
                    entry_price=position.entry_price,
                    exit_price=fill_px,
                    shares=position.shares,
                    pnl=pnl,
                    return_pct=pnl / invested if invested > 0 else 0.0,
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
            and not pd.isna(low_row.get(symbol))
            and not pd.isna(limit_row.get(symbol))
            and not pd.isna(stop_row.get(symbol))
            and float(low_row[symbol]) <= float(limit_row[symbol])
            and float(stop_row[symbol]) > 0.0
        ][:available_slots]

        if candidate_symbols:
            alloc_cash = cash / len(candidate_symbols)
            for symbol in candidate_symbols:
                fill_px = min(float(open_row[symbol]), float(limit_row[symbol]))
                spendable = alloc_cash - config.commission_per_trade
                if fill_px <= 0 or spendable <= 0:
                    continue
                shares = math.floor(spendable / fill_px)
                if shares <= 0:
                    continue
                entry_cost = shares * fill_px + config.commission_per_trade
                if entry_cost > cash:
                    shares = max(math.floor((cash - config.commission_per_trade) / fill_px), 0)
                    entry_cost = shares * fill_px + config.commission_per_trade
                if shares <= 0 or entry_cost > cash:
                    continue
                cash -= entry_cost
                positions[symbol] = StoppedPosition(
                    symbol=symbol,
                    shares=float(shares),
                    entry_price=fill_px,
                    entry_time=ts,
                    last_mark_price=float(close_row[symbol]),
                    stop_price=float(stop_row[symbol]),
                )

        gross_exposure = 0.0
        for symbol, position in list(positions.items()):
            close_px = close_row.get(symbol)
            if not pd.isna(close_px):
                positions[symbol] = StoppedPosition(
                    symbol=position.symbol,
                    shares=position.shares,
                    entry_price=position.entry_price,
                    entry_time=position.entry_time,
                    last_mark_price=float(close_px),
                    stop_price=position.stop_price,
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


def _run_variant(
    *,
    variant: Variant,
    basket_name: str,
    symbols: list[str],
    bars_by_symbol: dict[str, pd.DataFrame],
    spy_gate: pd.Series,
    regime_allowed: pd.Series,
    sector_scores: dict[str, pd.Series],
    sector_by_symbol: dict[str, str | None],
    earnings_by_symbol: dict[str, list[pd.Timestamp]],
    config: BacktestConfig,
    max_positions: int,
) -> PortfolioBacktestResult:
    edge_filter = HistoricalRSIFilter(
        spy_gate=spy_gate,
        regime_allowed=regime_allowed,
        sector_scores=sector_scores,
        sector_by_symbol=sector_by_symbol,
        earnings_by_symbol=earnings_by_symbol,
        breakdown_mode=variant.breakdown_mode,
    )
    strategy = HistoricalFilteredRSI(edge_filter=edge_filter)
    open_df, low_df, close_df, entries_df, limits_df, exits_df, stop_df = (
        _build_stop_aware_matrices(
            {symbol: bars_by_symbol.get(symbol) for symbol in symbols},
            symbols=symbols,
            strategy=strategy,
        )
    )
    return _simulate_stop_aware_portfolio(
        open_df,
        low_df,
        close_df,
        entries_df,
        limits_df,
        exits_df,
        stop_df,
        basket_name=f"{basket_name}:{variant.name}",
        config=config,
        max_positions=max_positions,
    )


def _render(results: list[PortfolioBacktestResult], *, feed: str, start: datetime, end: datetime) -> str:
    lines = [
        f"# RSI Filter Variant Backtest - {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        f"- Feed: `{feed}`",
        f"- Window: {start.date()} to {end.date()}",
        "- Shared-capital portfolio, next-session RSI limit-touch entries, ATR protective stops, 5 bps slippage on market exits/stops, $0 commission.",
        "- Filters modeled per historical bar: regime, selected SPY50 policy, earnings fail-open, liquidity, sector score <= -3 block, and selected breakdown gate.",
        "",
        "| Variant | Symbols | Trades | Return | CAGR | Sharpe | Sortino | MaxDD | Win % | PF | Avg Util | Avg Open Pos | Final Equity |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for result in results:
        stats = result.stats
        lines.append(
            f"| {result.basket_name} | {len(result.symbols)} | {int(stats['trade_count'])} | "
            f"{_pct(stats['total_return'])} | {_pct(stats['cagr'])} | {_num(stats['sharpe'])} | "
            f"{_num(stats['sortino'])} | {_pct(stats['max_drawdown'])} | {_pct(stats['win_rate'])} | "
            f"{_num(stats['profit_factor'])} | {_pct(stats['avg_gross_utilization'])} | "
            f"{stats['avg_open_positions']:.2f} | ${stats['final_equity']:,.2f} |"
        )
    return "\n".join(lines) + "\n"


def _pct(value: float) -> str:
    return "n/a" if math.isnan(value) else f"{value * 100:.1f}%"


def _num(value: float) -> str:
    if math.isnan(value):
        return "n/a"
    if math.isinf(value):
        return "inf"
    return f"{value:.2f}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run RSI filter variant backtests.")
    parser.add_argument("--basket", choices=["current", "static", "hybrid2"], default="current")
    parser.add_argument("--study", choices=["breakdown", "spy50"], default="breakdown")
    parser.add_argument("--lookback-days", type=int, default=1825)
    parser.add_argument("--feed", choices=["sip"], default="sip")
    parser.add_argument("--end-delay-minutes", type=int, default=60)
    parser.add_argument("--max-positions", type=int, default=5)
    parser.add_argument("--output", type=Path, default=Path("logs/rsi_filter_variant_backtest_latest.md"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    end = datetime.now(timezone.utc) - timedelta(minutes=args.end_delay_minutes)
    start = end - timedelta(days=args.lookback_days)

    basket_map = {
        "current": list(settings.RSI_WATCHLIST),
        "static": STATIC_RSI_BASKET,
        "hybrid2": HYBRID2_RSI_BASKET,
    }
    symbols = basket_map[args.basket]
    support_symbols = ["SPY", *settings.SECTOR_ETFS.values()]
    all_symbols = list(dict.fromkeys([*symbols, *support_symbols]))
    bars_by_symbol = fetch_symbols(
        all_symbols,
        start,
        end,
        "1Day",
        adjustment="all",
        feed=args.feed,
        use_cache=True,
    )
    spy = bars_by_symbol.get("SPY")
    if spy is None or spy.empty:
        raise RuntimeError("SPY bars are required for regime/SPY gates")

    variants = [
        Variant("current_new_low_20", "new_low_20"),
        Variant("option1_new_low_60", "new_low_60"),
        Variant("option2_new_low_20_and_below_sma200", "new_low_20_and_below_sma200"),
        Variant("reference_no_breakdown_gate", "none"),
    ]
    spy50_variants = [
        Variant("spy50_hard_with_option2_breakdown", "new_low_20_and_below_sma200", "hard_50"),
        Variant("spy50_removed_with_option2_breakdown", "new_low_20_and_below_sma200", "none"),
        Variant("spy50_1pct_band_with_option2_breakdown", "new_low_20_and_below_sma200", "band_1pct"),
        Variant("spy50_3day_grace_with_option2_breakdown", "new_low_20_and_below_sma200", "grace_3d"),
    ]
    if args.study == "spy50":
        variants = spy50_variants
    sector_by_symbol = _sector_mapping(symbols)
    earnings_by_symbol = _fetch_earnings_dates(symbols)
    sector_scores = _compute_sector_scores(bars_by_symbol)
    regime_allowed = _compute_regime_allowed(spy)
    cfg = BacktestConfig()

    results = [
        _run_variant(
            variant=variant,
            basket_name=args.basket,
            symbols=symbols,
            bars_by_symbol=bars_by_symbol,
            spy_gate=_compute_spy_gate(spy, variant.spy_gate_mode),
            regime_allowed=regime_allowed,
            sector_scores=sector_scores,
            sector_by_symbol=sector_by_symbol,
            earnings_by_symbol=earnings_by_symbol,
            config=cfg,
            max_positions=args.max_positions,
        )
        for variant in variants
    ]
    report = _render(results, feed=args.feed, start=start, end=end)
    print(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
