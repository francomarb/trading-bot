"""Shared-capital daily-bar simulator for Donchian parameter research."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from config.settings import MIN_TRADE_NOTIONAL
from indicators.technicals import add_atr, add_donchian_high, add_donchian_low


@dataclass(frozen=True)
class PortfolioTrade:
    symbol: str
    entry_date: pd.Timestamp
    exit_date: pd.Timestamp
    entry_price: float
    exit_price: float
    initial_stop: float
    quantity: int
    exit_reason: str

    @property
    def r_multiple(self) -> float:
        risk = self.entry_price - self.initial_stop
        return (self.exit_price - self.entry_price) / risk


@dataclass(frozen=True)
class PortfolioResult:
    equity: pd.Series
    trades: tuple[PortfolioTrade, ...]
    skipped_capacity: int
    expired_entries: int
    usable_symbols: int

    @property
    def stats(self) -> dict[str, float]:
        returns = self.equity.pct_change().dropna()
        sharpe = (
            float(returns.mean() / returns.std() * np.sqrt(252))
            if len(returns) > 1 and returns.std() > 0
            else 0.0
        )
        drawdown = self.equity / self.equity.cummax() - 1.0
        rs = [trade.r_multiple for trade in self.trades]
        return {
            "return": float(self.equity.iloc[-1] / self.equity.iloc[0] - 1.0),
            "sharpe": sharpe,
            "max_drawdown": float(drawdown.min()),
            "trades": float(len(self.trades)),
            "win_rate": float(np.mean([r > 0 for r in rs])) if rs else 0.0,
            "mean_r": float(np.mean(rs)) if rs else 0.0,
        }


@dataclass
class _Position:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    stop: float
    quantity: int


def stop_limit_fill(
    *, open_price: float, high: float, low: float, trigger: float, limit: float
) -> float | None:
    """Return the production-style DAY stop-limit fill, or ``None`` if expired."""
    if high < trigger or low > limit:
        return None
    return limit if open_price > limit else max(open_price, trigger)


def entry_quantity(
    *,
    equity: float,
    cash: float,
    available: float,
    fill: float,
    sizing_limit: float,
    atr: float,
    risk_per_trade_pct: float,
    position_notional_pct: float,
    stop_atr: float,
    min_trade_notional: float = MIN_TRADE_NOTIONAL,
) -> int:
    """Apply the production sleeve floor, then risk/notional/cash caps."""
    if available < min_trade_notional:
        return 0
    risk_qty = int((equity * risk_per_trade_pct) // (stop_atr * atr))
    position_qty = int((equity * position_notional_pct) // sizing_limit)
    return max(
        0,
        min(risk_qty, position_qty, int(available // sizing_limit), int(cash // fill)),
    )


def _prepared(
    bars: dict[str, pd.DataFrame], entry_window: int, exit_window: int, atr_length: int
) -> dict[str, pd.DataFrame]:
    prepared: dict[str, pd.DataFrame] = {}
    for symbol, raw in bars.items():
        if len(raw) < max(200, entry_window) + 5:
            continue
        frame = add_atr(raw.copy(), atr_length)
        frame = add_donchian_high(frame, entry_window)
        frame = add_donchian_low(frame, exit_window)
        close = frame["close"].astype(float)
        sma = close.rolling(200).mean()
        adv = (close * frame["volume"].astype(float)).rolling(20).mean()
        frame["entry_signal"] = close > frame[f"donchian_high_{entry_window}"]
        frame["exit_signal"] = close < frame[f"donchian_low_{exit_window}"]
        stock_gate = (close > sma).where(sma.notna(), other=True)
        liquidity_gate = (adv >= 20_000_000.0).where(adv.notna(), other=True)
        frame["edge_allowed"] = stock_gate & liquidity_gate
        prepared[symbol] = frame
    return prepared


def simulate_portfolio(
    bars: dict[str, pd.DataFrame],
    regime: pd.Series,
    symbols: list[str],
    *,
    entry_window: int,
    exit_window: int,
    trade_start: pd.Timestamp,
    trade_end: pd.Timestamp,
    initial_equity: float = 100_000.0,
    risk_per_trade_pct: float = 0.004,
    sleeve_notional_pct: float = 0.12,
    position_notional_pct: float = 0.048,
    max_positions: int = 8,
    atr_length: int = 14,
    stop_atr: float = 2.0,
    max_chase_bps: float = 500.0,
    max_chase_atr: float = 2.0,
    exit_slippage_bps: float = 5.0,
    min_trade_notional: float = MIN_TRADE_NOTIONAL,
) -> PortfolioResult:
    """Simulate one flat-start/flat-end evaluation fold using shared capital."""
    requested_bars = {symbol: bars[symbol] for symbol in symbols if symbol in bars}
    data = _prepared(requested_bars, entry_window, exit_window, atr_length)
    start, end = pd.Timestamp(trade_start), pd.Timestamp(trade_end)
    dates = sorted({d for df in data.values() for d in df.index if start <= d <= end})
    if not dates:
        raise ValueError("evaluation fold contains no bars")

    next_bar: dict[tuple[str, pd.Timestamp], pd.Timestamp] = {}
    for symbol, df in data.items():
        idx = list(df.index)
        next_bar.update({(symbol, idx[i]): idx[i + 1] for i in range(len(idx) - 1)})

    entry_orders: dict[pd.Timestamp, list[tuple[int, str, pd.Timestamp]]] = {}
    exit_orders: dict[pd.Timestamp, set[str]] = {}
    positions: dict[str, _Position] = {}
    last_close: dict[str, float] = {}
    cash = initial_equity
    curve: list[float] = []
    curve_dates: list[pd.Timestamp] = []
    trades: list[PortfolioTrade] = []
    skipped_capacity = expired_entries = 0
    slip = exit_slippage_bps / 10_000.0

    def close_position(symbol: str, date: pd.Timestamp, price: float, reason: str) -> None:
        nonlocal cash
        pos = positions.pop(symbol)
        cash += pos.quantity * price
        trades.append(PortfolioTrade(
            symbol=symbol, entry_date=pos.entry_date, exit_date=date,
            entry_price=pos.entry_price, exit_price=price,
            initial_stop=pos.stop, quantity=pos.quantity, exit_reason=reason,
        ))

    rank = {symbol: i for i, symbol in enumerate(symbols)}
    for date in dates:
        # Signal exits have the same precedence as the established audit simulator.
        for symbol in sorted(exit_orders.get(date, set()), key=rank.get):
            if symbol in positions and date in data[symbol].index:
                close_position(symbol, date, float(data[symbol].at[date, "open"]) * (1 - slip), "signal")

        # Protective stops on positions carried into this session.
        for symbol in list(positions):
            if date not in data[symbol].index:
                continue
            row = data[symbol].loc[date]
            pos = positions[symbol]
            if float(row["open"]) <= pos.stop:
                close_position(symbol, date, float(row["open"]) * (1 - slip), "stop_gap")
            elif float(row["low"]) <= pos.stop:
                close_position(symbol, date, pos.stop * (1 - slip), "stop_intrabar")

        # DAY stop-limit entries, deterministic in frozen liquidity rank order.
        for _, symbol, signal_date in sorted(entry_orders.get(date, [])):
            if symbol in positions:
                continue
            row = data[symbol].loc[date]
            signal = data[symbol].loc[signal_date]
            trigger = float(signal[f"donchian_high_{entry_window}"])
            atr = float(signal[f"atr_{atr_length}"])
            limit = trigger + min(trigger * max_chase_bps / 10_000.0, atr * max_chase_atr)
            fill = stop_limit_fill(
                open_price=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), trigger=trigger, limit=limit,
            )
            if fill is None:
                expired_entries += 1
                continue
            if len(positions) >= max_positions:
                skipped_capacity += 1
                continue
            equity_now = cash + sum(
                p.quantity * last_close.get(s, p.entry_price) for s, p in positions.items()
            )
            used = sum(p.quantity * last_close.get(s, p.entry_price) for s, p in positions.items())
            available = max(0.0, equity_now * sleeve_notional_pct - used)
            qty = entry_quantity(
                equity=equity_now, cash=cash, available=available,
                fill=fill, sizing_limit=limit, atr=atr,
                risk_per_trade_pct=risk_per_trade_pct,
                position_notional_pct=position_notional_pct,
                stop_atr=stop_atr, min_trade_notional=min_trade_notional,
            )
            if qty <= 0:
                skipped_capacity += 1
                continue
            cash -= qty * fill
            positions[symbol] = _Position(symbol, date, fill, fill - stop_atr * atr, qty)

        for symbol, df in data.items():
            if date not in df.index:
                continue
            row = df.loc[date]
            last_close[symbol] = float(row["close"])
            nxt = next_bar.get((symbol, date))
            if nxt is None or nxt > end:
                continue
            if symbol in positions and bool(row["exit_signal"]):
                exit_orders.setdefault(nxt, set()).add(symbol)
            if symbol not in positions and bool(row["entry_signal"]) and bool(row["edge_allowed"]):
                label = regime.asof(date) if len(regime.index) else "BEAR"
                if label in {"TRENDING", "RANGING", "VOLATILE"}:
                    entry_orders.setdefault(nxt, []).append((rank[symbol], symbol, date))

        curve_dates.append(date)
        curve.append(cash + sum(p.quantity * last_close.get(s, p.entry_price) for s, p in positions.items()))

    final_date = dates[-1]
    for symbol in list(positions):
        close_position(symbol, final_date, last_close[symbol] * (1 - slip), "eod")
    curve[-1] = cash
    return PortfolioResult(
        equity=pd.Series(curve, index=pd.DatetimeIndex(curve_dates), dtype=float),
        trades=tuple(trades), skipped_capacity=skipped_capacity,
        expired_entries=expired_entries, usable_symbols=len(data),
    )
