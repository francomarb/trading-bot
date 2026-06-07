"""
Bar-by-bar Donchian-breakout simulator with pluggable broker-stop policies.

Built specifically to evaluate PLAN P2 "Donchian trailing broker stop": compare
the current static-ATR stop against a Donchian-low trail and a chandelier
`HWM - k*ATR` trail under realistic gap-aware fill semantics. vectorbt's
`Portfolio.from_signals` only supports a fixed-fraction `sl_stop` (with an
optional HWM trail), which can't faithfully represent the 15-day-low trail or
a true ATR-based chandelier where the ATR distance recomputes each bar.

Production cadence assumption (matches the live engine):
  - Strategy signals fire on bar t's close.
  - Orders fill at bar t+1's open (market entry, market exit on signal).
  - Broker holds a protective stop. The stop level for bar t+1 is derived from
    data available through bar t's close; the engine replaces the stop once per
    day after computing today's level. Intraday no-stop windows are ignored
    here — they're brief in production and add no signal to the variant
    comparison.

Fill semantics on the protective stop:
  - Gap-through: if open[t] <= stop_today, fill at open[t]. This is the
    realistic outcome — the broker can't fill at the stop level when price has
    already traded through it overnight.
  - Intrabar: elif low[t] <= stop_today <= high[t], fill at stop_today. We
    assume stop-market fills cleanly at the stop level. Real-world slippage
    on the stop side is captured by `slippage_bps` applied to the fill price.
  - Signal-exit takes precedence on its own bar: when exits[i-1] is True, the
    broker stop is cancelled overnight and the position liquidates at open[i].
    (Cancelling a broker stop then submitting market exit is exactly what the
    engine does today.)

No-look-ahead invariants:
  - Stop level for bar t uses ATR and donchian_low values aligned to bar t-1's
    close. `add_donchian_low` already shifts by 1 (excludes today), so reading
    donchian_low[t-1] is the live engine's view at t-1 close.
  - Entry size is computed from entry_price (= open[t]) and initial_stop
    distance which is derived from ATR[t-1] — never from same-bar high/low.

Sizing model:
  Constant dollar risk per trade. risk_per_share = entry - initial_stop;
  shares = floor((initial_cash * risk_per_trade_pct) / risk_per_share),
  capped by affordability. Initial stop is identical across all three policies
  in this experiment (entry - 2*ATR), so sizing is identical across variants
  for any given entry. Only the *exit* mechanism differs — that is the
  apples-to-apples A/B the PLAN asks for.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np
import pandas as pd

from indicators.technicals import add_atr, add_donchian_high, add_donchian_low


# ── Stop policies ────────────────────────────────────────────────────────────


class StopPolicy(Protocol):
    """Pluggable rule for placing and trailing the broker-side protective stop."""

    name: str

    def initial_stop(self, entry_price: float, atr_at_entry: float) -> float:
        ...

    def update_stop(
        self,
        prev_stop: float,
        hwm_close: float,
        atr_today: float,
        donchian_low_today: float,
    ) -> float:
        ...


@dataclass(frozen=True)
class StaticATRStop:
    """Current production behavior: stop set once at entry, never moved."""

    k: float = 2.0
    name: str = "static_atr"

    def initial_stop(self, entry_price: float, atr_at_entry: float) -> float:
        return entry_price - self.k * atr_at_entry

    def update_stop(
        self,
        prev_stop: float,
        hwm_close: float,
        atr_today: float,
        donchian_low_today: float,
    ) -> float:
        return prev_stop


@dataclass(frozen=True)
class DonchianLowTrail:
    """
    Stop trails the rolling N-day close low, minus a wick buffer in ATRs.

    Initial stop matches the static policy (entry - initial_k * ATR_at_entry),
    so for any new entry the initial risk distance is identical across all
    three variants and per-trade sizing stays apples-to-apples. The stop only
    *ratchets up* — it never loosens.

    `donchian_low_today` is read from the same exit-window series the strategy
    uses for signal exits (close-based, already shift-by-1 inside
    `add_donchian_low`).
    """

    initial_k: float = 2.0
    buffer_atr: float = 0.5
    name: str = "donchian_low_trail"

    def initial_stop(self, entry_price: float, atr_at_entry: float) -> float:
        return entry_price - self.initial_k * atr_at_entry

    def update_stop(
        self,
        prev_stop: float,
        hwm_close: float,
        atr_today: float,
        donchian_low_today: float,
    ) -> float:
        if not np.isfinite(donchian_low_today) or not np.isfinite(atr_today):
            return prev_stop
        candidate = donchian_low_today - self.buffer_atr * atr_today
        return max(prev_stop, candidate)


@dataclass(frozen=True)
class ChandelierStop:
    """
    Stop trails the highest close since entry minus k * ATR (textbook
    chandelier exit; Le Beau / Chuck LeBeau).

    Initial stop matches the static policy. ATR is recomputed each bar, so
    chandelier loosens during volatility expansion and tightens during
    compression — different from a fixed-percentage HWM trail.
    """

    initial_k: float = 2.0
    k: float = 3.0
    name: str = "chandelier"

    def initial_stop(self, entry_price: float, atr_at_entry: float) -> float:
        return entry_price - self.initial_k * atr_at_entry

    def update_stop(
        self,
        prev_stop: float,
        hwm_close: float,
        atr_today: float,
        donchian_low_today: float,
    ) -> float:
        if not np.isfinite(hwm_close) or not np.isfinite(atr_today):
            return prev_stop
        candidate = hwm_close - self.k * atr_today
        return max(prev_stop, candidate)


# ── Trade record ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TradeRecord:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str  # 'stop_gap' | 'stop_intrabar' | 'signal' | 'eod'
    bars_held: int
    shares: int
    initial_stop: float
    risk_per_share: float
    pnl_dollars: float
    pnl_pct: float
    r_multiple: float


# ── Per-symbol stats ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SymbolResult:
    symbol: str
    policy_name: str
    bars: int
    trades: list[TradeRecord]
    equity_curve: pd.Series
    initial_cash: float
    final_equity: float
    total_return: float
    cagr: float
    sharpe: float
    max_drawdown: float
    trade_count: int
    win_rate: float
    avg_r: float
    expectancy_pct: float
    buy_hold_return: float


# ── Core simulator ──────────────────────────────────────────────────────────


def _compute_indicators(
    df: pd.DataFrame, *, entry_window: int, exit_window: int, atr_length: int
) -> tuple[pd.DataFrame, pd.Series, pd.Series, pd.Series]:
    """Return df-with-indicators plus aligned ATR, donchian_high, donchian_low."""
    out = add_atr(df, atr_length)
    out = add_donchian_high(out, entry_window)
    out = add_donchian_low(out, exit_window)
    atr = out[f"atr_{atr_length}"]
    dhigh = out[f"donchian_high_{entry_window}"]
    dlow = out[f"donchian_low_{exit_window}"]
    return out, atr, dhigh, dlow


def simulate_symbol(
    symbol: str,
    df: pd.DataFrame,
    policy: StopPolicy,
    *,
    entry_window: int = 30,
    exit_window: int = 15,
    atr_length: int = 14,
    initial_cash: float = 100_000.0,
    risk_per_trade_pct: float = 0.02,
    slippage_bps: float = 5.0,
) -> SymbolResult:
    """
    Walk daily bars and produce a per-symbol equity curve and trade list under
    the given stop policy.

    Strategy signals are Donchian System 1: entry on close > prior entry-window
    high; signal-exit on close < prior exit-window low. Signals fill at the
    next bar's open.

    `slippage_bps` is applied symmetrically to every fill price (entry, signal
    exit at open, stop fills). Matches `backtest.runner.BacktestConfig` default.
    """
    required = {"open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"{symbol}: df missing columns {sorted(missing)}")
    if len(df) < entry_window + atr_length + 5:
        raise ValueError(
            f"{symbol}: only {len(df)} bars; need at least "
            f"{entry_window + atr_length + 5} for warmup"
        )

    out, atr, dhigh, dlow = _compute_indicators(
        df,
        entry_window=entry_window,
        exit_window=exit_window,
        atr_length=atr_length,
    )

    close = out["close"].to_numpy(dtype=float)
    open_ = out["open"].to_numpy(dtype=float)
    high = out["high"].to_numpy(dtype=float)
    low = out["low"].to_numpy(dtype=float)
    atr_a = atr.to_numpy(dtype=float)
    dhigh_a = dhigh.to_numpy(dtype=float)
    dlow_a = dlow.to_numpy(dtype=float)
    dates = out.index

    # Signal series (close-based, aligned to bar t — fill at t+1 open).
    entries = close > dhigh_a
    exits = close < dlow_a

    n = len(out)
    cash = initial_cash
    equity = np.full(n, initial_cash, dtype=float)

    in_pos = False
    shares = 0
    entry_price = 0.0
    initial_stop = 0.0
    current_stop = 0.0
    risk_per_share = 0.0
    hwm_close = 0.0
    entry_bar = -1
    pending_signal_exit = False

    slip = slippage_bps / 10_000.0
    trades: list[TradeRecord] = []

    def _mark_equity(i: int) -> None:
        equity[i] = cash + (shares * close[i] if in_pos else 0.0)

    for i in range(n):
        if np.isnan(atr_a[i]) or np.isnan(dhigh_a[i]):
            equity[i] = cash
            continue

        if in_pos:
            # Pending signal-exit from yesterday's close
            if pending_signal_exit:
                fill = open_[i] * (1.0 - slip)
                pnl = (fill - entry_price) * shares
                cash += shares * fill
                trades.append(TradeRecord(
                    symbol=symbol,
                    entry_date=dates[entry_bar],
                    entry_price=entry_price,
                    exit_date=dates[i],
                    exit_price=fill,
                    exit_reason="signal",
                    bars_held=i - entry_bar,
                    shares=shares,
                    initial_stop=initial_stop,
                    risk_per_share=risk_per_share,
                    pnl_dollars=pnl,
                    pnl_pct=(fill - entry_price) / entry_price,
                    r_multiple=(fill - entry_price) / risk_per_share,
                ))
                in_pos = False
                shares = 0
                pending_signal_exit = False
                _mark_equity(i)
                continue

            # Stop check
            if open_[i] <= current_stop:
                fill = open_[i] * (1.0 - slip)
                reason = "stop_gap"
            elif low[i] <= current_stop:
                fill = current_stop * (1.0 - slip)
                reason = "stop_intrabar"
            else:
                fill = None
                reason = ""

            if fill is not None:
                pnl = (fill - entry_price) * shares
                cash += shares * fill
                trades.append(TradeRecord(
                    symbol=symbol,
                    entry_date=dates[entry_bar],
                    entry_price=entry_price,
                    exit_date=dates[i],
                    exit_price=fill,
                    exit_reason=reason,
                    bars_held=i - entry_bar,
                    shares=shares,
                    initial_stop=initial_stop,
                    risk_per_share=risk_per_share,
                    pnl_dollars=pnl,
                    pnl_pct=(fill - entry_price) / entry_price,
                    r_multiple=(fill - entry_price) / risk_per_share,
                ))
                in_pos = False
                shares = 0
                _mark_equity(i)
                continue

            hwm_close = max(hwm_close, close[i])
            current_stop = policy.update_stop(
                current_stop, hwm_close, atr_a[i], dlow_a[i]
            )
            if exits[i]:
                pending_signal_exit = True
            _mark_equity(i)
            continue

        # Not in position: check if yesterday emitted an entry signal
        if i >= 1 and entries[i - 1] and not np.isnan(atr_a[i - 1]):
            fill = open_[i] * (1.0 + slip)
            atr_at_entry = atr_a[i - 1]
            init_stop = policy.initial_stop(fill, atr_at_entry)
            rps = fill - init_stop
            if rps <= 0:
                equity[i] = cash
                continue
            risk_dollars = initial_cash * risk_per_trade_pct
            qty = int(risk_dollars // rps)
            qty = min(qty, int(cash // fill))
            if qty <= 0:
                equity[i] = cash
                continue

            entry_price = fill
            initial_stop = init_stop
            current_stop = init_stop
            risk_per_share = rps
            shares = qty
            entry_bar = i
            hwm_close = close[i]
            pending_signal_exit = bool(exits[i])
            cash -= shares * entry_price
            in_pos = True

            # Stop the entry bar itself is not checked against the protective
            # stop (production: broker stop submits after fill confirmation).
            # Still trail the stop based on today's close so tomorrow has the
            # right level.
            current_stop = policy.update_stop(
                current_stop, hwm_close, atr_a[i], dlow_a[i]
            )
            _mark_equity(i)
            continue

        equity[i] = cash

    # Force-close any open position at the final bar's close
    if in_pos:
        i = n - 1
        fill = close[i] * (1.0 - slip)
        pnl = (fill - entry_price) * shares
        cash += shares * fill
        trades.append(TradeRecord(
            symbol=symbol,
            entry_date=dates[entry_bar],
            entry_price=entry_price,
            exit_date=dates[i],
            exit_price=fill,
            exit_reason="eod",
            bars_held=i - entry_bar,
            shares=shares,
            initial_stop=initial_stop,
            risk_per_share=risk_per_share,
            pnl_dollars=pnl,
            pnl_pct=(fill - entry_price) / entry_price,
            r_multiple=(fill - entry_price) / risk_per_share,
        ))
        in_pos = False
        equity[i] = cash

    equity_series = pd.Series(equity, index=dates, name=f"{symbol}_{policy.name}")
    stats = _compute_stats(equity_series, initial_cash, trades, df)

    return SymbolResult(
        symbol=symbol,
        policy_name=policy.name,
        bars=n,
        trades=trades,
        equity_curve=equity_series,
        initial_cash=initial_cash,
        **stats,
    )


def _compute_stats(
    equity: pd.Series,
    initial_cash: float,
    trades: list[TradeRecord],
    df: pd.DataFrame,
) -> dict:
    final_equity = float(equity.iloc[-1])
    total_return = final_equity / initial_cash - 1.0
    n_years = max((equity.index[-1] - equity.index[0]).days / 365.25, 1e-9)
    cagr = (
        (final_equity / initial_cash) ** (1.0 / n_years) - 1.0
        if final_equity > 0
        else -1.0
    )

    rets = equity.pct_change().dropna()
    if len(rets) > 1 and rets.std() > 0:
        sharpe = float(rets.mean() / rets.std() * np.sqrt(252))
    else:
        sharpe = 0.0

    cummax = equity.cummax()
    dd = (equity - cummax) / cummax
    max_drawdown = float(dd.min())

    n = len(trades)
    if n:
        rs = np.array([t.r_multiple for t in trades])
        pnls = np.array([t.pnl_pct for t in trades])
        win_rate = float((pnls > 0).mean())
        avg_r = float(rs.mean())
        expectancy_pct = float(pnls.mean())
    else:
        win_rate = avg_r = expectancy_pct = 0.0

    first_open = float(df["open"].iloc[0])
    last_close = float(df["close"].iloc[-1])
    bh = last_close / first_open - 1.0 if first_open > 0 else 0.0

    return {
        "final_equity": final_equity,
        "total_return": total_return,
        "cagr": cagr,
        "sharpe": sharpe,
        "max_drawdown": max_drawdown,
        "trade_count": n,
        "win_rate": win_rate,
        "avg_r": avg_r,
        "expectancy_pct": expectancy_pct,
        "buy_hold_return": bh,
    }


# ── Multi-symbol portfolio aggregation ──────────────────────────────────────


@dataclass(frozen=True)
class PortfolioAggregate:
    policy_name: str
    n_symbols: int
    mean_total_return: float
    mean_cagr: float
    mean_sharpe: float
    mean_max_drawdown: float
    mean_buy_hold: float
    total_trades: int
    win_rate: float
    avg_r: float
    expectancy_pct: float
    pct_stop_gap: float
    pct_stop_intrabar: float
    pct_signal_exit: float
    pct_eod: float


def aggregate(results: list[SymbolResult]) -> PortfolioAggregate:
    """Equal-weight aggregate across per-symbol runs of a single policy."""
    if not results:
        raise ValueError("aggregate() requires at least one result")
    n = len(results)
    all_trades = [t for r in results for t in r.trades]
    total = len(all_trades)

    if total:
        win_rate = sum(1 for t in all_trades if t.pnl_pct > 0) / total
        avg_r = sum(t.r_multiple for t in all_trades) / total
        expectancy_pct = sum(t.pnl_pct for t in all_trades) / total
        reasons = [t.exit_reason for t in all_trades]
        pct_gap = sum(1 for r in reasons if r == "stop_gap") / total
        pct_intra = sum(1 for r in reasons if r == "stop_intrabar") / total
        pct_signal = sum(1 for r in reasons if r == "signal") / total
        pct_eod = sum(1 for r in reasons if r == "eod") / total
    else:
        win_rate = avg_r = expectancy_pct = 0.0
        pct_gap = pct_intra = pct_signal = pct_eod = 0.0

    return PortfolioAggregate(
        policy_name=results[0].policy_name,
        n_symbols=n,
        mean_total_return=sum(r.total_return for r in results) / n,
        mean_cagr=sum(r.cagr for r in results) / n,
        mean_sharpe=sum(r.sharpe for r in results) / n,
        mean_max_drawdown=sum(r.max_drawdown for r in results) / n,
        mean_buy_hold=sum(r.buy_hold_return for r in results) / n,
        total_trades=total,
        win_rate=win_rate,
        avg_r=avg_r,
        expectancy_pct=expectancy_pct,
        pct_stop_gap=pct_gap,
        pct_stop_intrabar=pct_intra,
        pct_signal_exit=pct_signal,
        pct_eod=pct_eod,
    )
