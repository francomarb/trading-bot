"""Shared-capital daily-bar simulator for Donchian parameter research."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

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
    skipped_heat: int
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


@dataclass(frozen=True)
class _EntryCandidate:
    """One next-session DAY candidate derived from a completed signal bar."""

    rank: int
    symbol: str
    signal_date: pd.Timestamp
    trigger: float
    limit: float
    reference_close: float
    atr: float

    def sizing_stop(self, stop_atr: float) -> float:
        return self.reference_close - stop_atr * self.atr


@dataclass(frozen=True)
class _EntryOrder:
    """One production-style DAY reservation admitted for this session."""

    candidate: _EntryCandidate
    quantity: int

    @property
    def rank(self) -> int:
        return self.candidate.rank

    @property
    def symbol(self) -> str:
        return self.candidate.symbol

    @property
    def trigger(self) -> float:
        return self.candidate.trigger

    @property
    def limit(self) -> float:
        return self.candidate.limit

    @property
    def atr(self) -> float:
        return self.candidate.atr

    @property
    def reserved_notional(self) -> float:
        return self.quantity * self.limit

    def reserved_risk(self, stop_atr: float) -> float:
        return self.quantity * (
            self.limit - self.candidate.sizing_stop(stop_atr)
        )


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
    sizing_stop: float | None = None,
    min_trade_notional: float = MIN_TRADE_NOTIONAL,
) -> int:
    """Apply the production sleeve floor, then risk/notional/cash caps."""
    if available < min_trade_notional:
        return 0
    risk_distance = (
        sizing_limit - sizing_stop
        if sizing_stop is not None
        else stop_atr * atr
    )
    if risk_distance <= 0:
        return 0
    risk_qty = int((equity * risk_per_trade_pct) // risk_distance)
    position_qty = int((equity * position_notional_pct) // sizing_limit)
    return max(
        0,
        min(risk_qty, position_qty, int(available // sizing_limit), int(cash // fill)),
    )


def _prepared(
    bars: dict[str, pd.DataFrame],
    entry_window: int,
    exit_window: int,
    atr_length: int,
    channel_mode: Literal["close", "high_low"],
) -> dict[str, pd.DataFrame]:
    prepared: dict[str, pd.DataFrame] = {}
    for symbol, raw in bars.items():
        if len(raw) < max(200, entry_window) + 5:
            continue
        frame = add_atr(raw.copy(), atr_length)
        entry_source = "close" if channel_mode == "close" else "high"
        exit_source = "close" if channel_mode == "close" else "low"
        frame = add_donchian_high(frame, entry_window, source=entry_source)
        frame = add_donchian_low(frame, exit_window, source=exit_source)
        close = frame["close"].astype(float)
        sma = close.rolling(200).mean()
        adv = (close * frame["volume"].astype(float)).rolling(20).mean()
        frame["entry_signal"] = close > frame[f"donchian_high_{entry_window}"]
        frame["exit_signal"] = close < frame[f"donchian_low_{exit_window}"]
        if channel_mode == "high_low":
            # At the close of t, the classic stop for t+1 is the highest high
            # through t. The shifted channel remains the active exit level in t.
            frame["next_entry_trigger"] = (
                frame["high"].astype(float).rolling(entry_window).max()
            )
        else:
            frame["next_entry_trigger"] = frame[f"donchian_high_{entry_window}"]
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
    heat_cap_pct: float | None = None,
    channel_mode: Literal["close", "high_low"] = "close",
) -> PortfolioResult:
    """Simulate one flat-start/flat-end evaluation fold using shared capital."""
    if channel_mode not in {"close", "high_low"}:
        raise ValueError(f"unsupported Donchian channel mode: {channel_mode!r}")
    if heat_cap_pct is not None and heat_cap_pct <= 0:
        raise ValueError("heat_cap_pct must be positive when configured")
    requested_bars = {symbol: bars[symbol] for symbol in symbols if symbol in bars}
    data = _prepared(
        requested_bars, entry_window, exit_window, atr_length, channel_mode
    )
    start, end = pd.Timestamp(trade_start), pd.Timestamp(trade_end)
    dates = sorted({d for df in data.values() for d in df.index if start <= d <= end})
    if not dates:
        raise ValueError("evaluation fold contains no bars")

    next_bar: dict[tuple[str, pd.Timestamp], pd.Timestamp] = {}
    for symbol, df in data.items():
        idx = list(df.index)
        next_bar.update({(symbol, idx[i]): idx[i + 1] for i in range(len(idx) - 1)})

    entry_candidates: dict[pd.Timestamp, list[_EntryCandidate]] = {}
    exit_orders: dict[pd.Timestamp, set[str]] = {}
    positions: dict[str, _Position] = {}
    last_close: dict[str, float] = {}
    cash = initial_equity
    curve: list[float] = []
    curve_dates: list[pd.Timestamp] = []
    trades: list[PortfolioTrade] = []
    skipped_capacity = skipped_heat = expired_entries = 0
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

        # Protective stops and classic intraday channel exits on positions
        # carried into this session. On a downward move the higher threshold
        # is encountered first; a gap through it fills at the open.
        for symbol in list(positions):
            if date not in data[symbol].index:
                continue
            row = data[symbol].loc[date]
            pos = positions[symbol]
            channel_stop = (
                float(row[f"donchian_low_{exit_window}"])
                if channel_mode == "high_low"
                and pd.notna(row[f"donchian_low_{exit_window}"])
                else None
            )
            active_stop = max(pos.stop, channel_stop or pos.stop)
            is_channel = channel_stop is not None and channel_stop > pos.stop
            if float(row["open"]) <= active_stop:
                close_position(
                    symbol,
                    date,
                    float(row["open"]) * (1 - slip),
                    "channel_gap" if is_channel else "stop_gap",
                )
            elif float(row["low"]) <= active_stop:
                close_position(
                    symbol,
                    date,
                    active_stop * (1 - slip),
                    "channel_intrabar" if is_channel else "stop_intrabar",
                )

        # Admit DAY stop-limit candidates in frozen liquidity order. Every
        # admitted order reserves notional and initial risk for the rest of
        # the session, even if daily OHLC later shows that it expired unfilled.
        admitted: list[_EntryOrder] = []
        for candidate in sorted(
            entry_candidates.get(date, []), key=lambda item: item.rank
        ):
            symbol = candidate.symbol
            if symbol in positions:
                continue
            if len(positions) + len(admitted) >= max_positions:
                skipped_capacity += 1
                continue
            equity_now = cash + sum(
                p.quantity * last_close.get(s, p.entry_price)
                for s, p in positions.items()
            )
            used = sum(
                p.quantity * last_close.get(s, p.entry_price)
                for s, p in positions.items()
            )
            pending_notional = sum(item.reserved_notional for item in admitted)
            available = max(
                0.0,
                equity_now * sleeve_notional_pct - used - pending_notional,
            )
            available_cash = max(0.0, cash - pending_notional)
            qty = entry_quantity(
                equity=equity_now,
                cash=available_cash,
                available=available,
                fill=candidate.limit,
                sizing_limit=candidate.limit,
                atr=candidate.atr,
                risk_per_trade_pct=risk_per_trade_pct,
                position_notional_pct=position_notional_pct,
                stop_atr=stop_atr,
                sizing_stop=candidate.sizing_stop(stop_atr),
                min_trade_notional=min_trade_notional,
            )
            if qty <= 0:
                skipped_capacity += 1
                continue
            order = _EntryOrder(candidate, qty)
            if heat_cap_pct is not None:
                open_heat = sum(
                    p.quantity * (p.entry_price - p.stop)
                    for p in positions.values()
                )
                pending_heat = sum(
                    item.reserved_risk(stop_atr) for item in admitted
                )
                if (
                    open_heat
                    + pending_heat
                    + order.reserved_risk(stop_atr)
                    > equity_now * heat_cap_pct
                ):
                    skipped_heat += 1
                    continue
            admitted.append(order)

        for order in admitted:
            symbol = order.symbol
            row = data[symbol].loc[date]
            fill = stop_limit_fill(
                open_price=float(row["open"]),
                high=float(row["high"]),
                low=float(row["low"]),
                trigger=order.trigger,
                limit=order.limit,
            )
            if fill is None:
                expired_entries += 1
                continue
            cash -= order.quantity * fill
            positions[symbol] = _Position(
                symbol,
                date,
                fill,
                fill - stop_atr * order.atr,
                order.quantity,
            )

        for symbol, df in data.items():
            if date not in df.index:
                continue
            row = df.loc[date]
            last_close[symbol] = float(row["close"])
            nxt = next_bar.get((symbol, date))
            if nxt is None or nxt > end:
                continue
            if (
                channel_mode == "close"
                and symbol in positions
                and bool(row["exit_signal"])
            ):
                exit_orders.setdefault(nxt, set()).add(symbol)
            wants_entry = (
                bool(row["entry_signal"])
                if channel_mode == "close"
                else pd.notna(row["next_entry_trigger"])
            )
            if symbol not in positions and wants_entry and bool(row["edge_allowed"]):
                label = regime.asof(date) if len(regime.index) else "BEAR"
                if label in {"TRENDING", "RANGING", "VOLATILE"}:
                    trigger = float(row["next_entry_trigger"])
                    atr = float(row[f"atr_{atr_length}"])
                    if not np.isfinite(trigger) or not np.isfinite(atr) or atr <= 0:
                        continue
                    limit = trigger + min(
                        trigger * max_chase_bps / 10_000.0,
                        atr * max_chase_atr,
                    )
                    entry_candidates.setdefault(nxt, []).append(
                        _EntryCandidate(
                            rank[symbol],
                            symbol,
                            date,
                            trigger,
                            limit,
                            float(row["close"]),
                            atr,
                        )
                    )

        curve_dates.append(date)
        curve.append(cash + sum(p.quantity * last_close.get(s, p.entry_price) for s, p in positions.items()))

    final_date = dates[-1]
    for symbol in list(positions):
        close_position(symbol, final_date, last_close[symbol] * (1 - slip), "eod")
    curve[-1] = cash
    return PortfolioResult(
        equity=pd.Series(curve, index=pd.DatetimeIndex(curve_dates), dtype=float),
        trades=tuple(trades), skipped_capacity=skipped_capacity,
        skipped_heat=skipped_heat,
        expired_entries=expired_entries, usable_symbols=len(data),
    )
