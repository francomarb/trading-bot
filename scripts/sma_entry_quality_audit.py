"""Production-mirror SMA entry-quality audit (PLAN 11.70).

This script is research-only.  It does not change the paper strategy.  It
compares three pre-registered entry/stop policies on locally cached SIP daily
bars:

``control``
    A production-valid 20/50 cross fills at the next session's open.  The stop
    is anchored to the fill at ``fill - 2 * signal-day ATR``.

``structure_stop``
    The same entry, but the stop is the lower of the control stop and
    ``signal-day SMA50 - 0.5 * ATR``.  Quantity changes so every trade still
    starts with exactly 1R of risk.

``pullback``
    A valid cross arms a five-session buy limit at
    ``signal close - 0.5 * ATR``.  It expires unfilled after five sessions and
    is cancelled if the 20/50 bullish state has failed at a prior close.  A
    fill uses the normal 2-ATR stop, anchored to the actual fill with the ATR
    available before that session.

The production gates mirrored here are the stock-above-SMA200 gate, 10-day
median volume > 30-day median volume, and the exact SPY regime classifier.
Sector momentum is observation-only in production (``warn``), so it cannot
block this replay.  Historical earnings dates are not point-in-time complete
in the runtime yfinance source; an optional CSV can supply them.  Without it,
the earnings gate fails open exactly as production does and the report labels
coverage as unavailable.

The development and held-out periods are evaluated independently so an open
development trade cannot leak a held-out exit into policy selection.  Open
positions at a period boundary are marked at the last close and reported as
``period_mark``.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from config import settings
from indicators.technicals import add_adx, add_atr, add_sma


FAST = 20
SLOW = 50
LONG_SMA = 200
ATR_LENGTH = 14
ATR_STOP_MULTIPLIER = 2.0
STRUCTURE_BUFFER_ATR = 0.5
PULLBACK_OFFSET_ATR = 0.5
PULLBACK_WAIT_SESSIONS = 5
DEV_START = pd.Timestamp("2017-01-01", tz="UTC")
DEV_END = pd.Timestamp("2022-12-31", tz="UTC")
HOLDOUT_START = pd.Timestamp("2023-01-01", tz="UTC")
DEFAULT_END = pd.Timestamp("2026-09-04", tz="UTC")
MIN_HOLDOUT_TRADES = 30
MIN_RETENTION = 0.60

# Frozen when 11.70 started.  Do not replace this with settings.SMA_WATCHLIST:
# a drifting universe would make the audit irreproducible.
AUDIT_UNIVERSE: tuple[str, ...] = (
    "SNDK", "WDC", "STX", "GSAT", "POWL", "VIAV", "VSAT", "CIEN",
    "ASML", "MSTR", "MU", "FORM", "ALB", "CSTM", "DOCN", "TTMI",
    "FRO", "MTZ", "DK", "ASX", "CAT", "HUT", "GLW", "AMD", "STRL",
    "INTC", "BE", "ECG", "MRVL", "NVT", "SQM", "TSEM", "PL", "UBER",
    "DASH", "NVDA", "ADBE", "ANET", "META", "PLTR", "DUOL", "TSM",
    "DELL", "LSCC", "LRCX", "NOK", "FLEX", "SANM", "ATI", "COHU", "AA",
    "CRWD", "NET", "PWR", "VIST", "VST",
)


@dataclass(frozen=True)
class AuditTrade:
    """One independently sized, fixed-risk simulated trade."""

    symbol: str
    policy: str
    signal_date: pd.Timestamp
    entry_date: pd.Timestamp
    entry_price: float
    stop_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str

    @property
    def initial_risk_per_share(self) -> float:
        return self.entry_price - self.stop_price

    @property
    def r_multiple(self) -> float:
        return (self.exit_price - self.entry_price) / self.initial_risk_per_share


@dataclass(frozen=True)
class PeriodResult:
    """Policy output plus the valid control opportunities for one period."""

    trades: tuple[AuditTrade, ...]
    opportunities: tuple[pd.Timestamp, ...]


def _utc_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted frame with a UTC DatetimeIndex."""
    out = frame.copy()
    index = pd.DatetimeIndex(out.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    out.index = index
    return out.sort_index()


def prepare_symbol_bars(frame: pd.DataFrame) -> pd.DataFrame:
    """Add the exact signal, stop, and symbol-filter inputs."""
    out = _utc_index(frame)
    out = add_sma(out, FAST)
    out = add_sma(out, SLOW)
    out = add_sma(out, LONG_SMA)
    out = add_atr(out, ATR_LENGTH)
    diff = out[f"sma_{FAST}"] - out[f"sma_{SLOW}"]
    out["golden_cross"] = (diff > 0) & (diff.shift(1) <= 0)
    out["death_cross"] = (diff < 0) & (diff.shift(1) >= 0)
    stock_sma = out[f"sma_{LONG_SMA}"]
    out["stock_gate"] = (out["close"] > stock_sma).where(
        stock_sma.notna(), other=True
    )
    short_volume = out["volume"].astype(float).rolling(10).median()
    long_volume = out["volume"].astype(float).rolling(30).median()
    out["volume_gate"] = (short_volume > long_volume).where(
        short_volume.notna() & long_volume.notna(), other=True
    )
    return out


def classify_spy_regimes(frame: pd.DataFrame) -> pd.Series:
    """Vectorized historical mirror of ``RegimeDetector._classify``."""
    spy = _utc_index(frame)
    spy = add_sma(spy, 200)
    spy = add_sma(spy, 50)
    spy = add_atr(spy, 14)
    spy = add_adx(spy, 14)
    atr_pct = spy["atr_14"] / spy["close"]
    regimes: list[str] = []
    for i in range(len(spy)):
        close = float(spy["close"].iloc[i])
        sma200 = spy["sma_200"].iloc[i]
        if pd.notna(sma200) and close < float(sma200):
            regimes.append("BEAR")
            continue

        current_atr_pct = atr_pct.iloc[i]
        volatile = False
        if pd.notna(current_atr_pct):
            window = atr_pct.iloc[max(0, i - 125) : i + 1].dropna()
            if len(window) >= 10:
                rank = float((window < float(current_atr_pct)).mean())
                volatile = rank >= 0.80 and float(current_atr_pct) >= 0.012
        if volatile:
            regimes.append("VOLATILE")
            continue

        adx = spy["adx_14"].iloc[i]
        if pd.isna(adx) or float(adx) <= 20.0:
            regimes.append("RANGING")
        elif float(adx) >= 25.0:
            regimes.append("TRENDING")
        else:
            prior_i = i - 5
            slope = (
                float(spy["sma_50"].iloc[i]) - float(spy["sma_50"].iloc[prior_i])
                if prior_i >= 0
                and pd.notna(spy["sma_50"].iloc[i])
                and pd.notna(spy["sma_50"].iloc[prior_i])
                else None
            )
            regimes.append("TRENDING" if slope is not None and slope > 0 else "RANGING")
    return pd.Series(regimes, index=spy.index, dtype="object")


def load_earnings_csv(path: Path | None) -> dict[str, set[pd.Timestamp]]:
    """Load ``symbol,date`` earnings rows, normalized to UTC dates."""
    if path is None:
        return {}
    frame = pd.read_csv(path)
    required = {"symbol", "date"}
    if not required.issubset(frame.columns):
        raise ValueError(f"earnings CSV must contain columns {sorted(required)}")
    result: dict[str, set[pd.Timestamp]] = {}
    for row in frame.itertuples(index=False):
        date = pd.Timestamp(row.date)
        date = date.tz_localize("UTC") if date.tzinfo is None else date.tz_convert("UTC")
        result.setdefault(str(row.symbol).upper(), set()).add(date.normalize())
    return result


def _earnings_allowed(
    symbol: str,
    signal_date: pd.Timestamp,
    earnings: dict[str, set[pd.Timestamp]],
) -> bool:
    """Mirror SMA's two-calendar-day pre-earnings blackout."""
    dates = earnings.get(symbol, set())
    day = signal_date.normalize()
    return not any(pd.Timedelta(0) <= event - day <= pd.Timedelta(days=2) for event in dates)


def valid_signal_mask(
    bars: pd.DataFrame,
    regimes: pd.Series,
    *,
    symbol: str,
    earnings: dict[str, set[pd.Timestamp]],
) -> pd.Series:
    """Return production-valid SMA entry signals for a symbol."""
    aligned_regime = regimes.reindex(bars.index)
    regime_gate = aligned_regime.isin(settings.STRATEGY_ALLOWED_REGIMES["sma_crossover"])
    earnings_gate = pd.Series(
        [_earnings_allowed(symbol, day, earnings) for day in bars.index],
        index=bars.index,
        dtype=bool,
    )
    return (
        bars["golden_cross"].fillna(False)
        & bars["stock_gate"].fillna(False)
        & bars["volume_gate"].fillna(False)
        & regime_gate.fillna(False)
        & earnings_gate
    ).astype(bool)


def _stop_fill(row: pd.Series, stop: float) -> float | None:
    """Return a realistic long stop-market fill, including gap-through."""
    if float(row.open) <= stop:
        return float(row.open)
    if float(row.low) <= stop:
        return stop
    return None


def _exit_trade(
    *,
    symbol: str,
    policy: str,
    bars: pd.DataFrame,
    signal_i: int,
    entry_i: int,
    entry_price: float,
    stop_price: float,
    period_end_i: int,
) -> AuditTrade:
    """Walk the production stop/death-cross exits from an actual fill."""
    if stop_price <= 0 or stop_price >= entry_price:
        raise ValueError("stop must be positive and below the entry fill")
    exit_i = period_end_i
    exit_price = float(bars["close"].iloc[period_end_i])
    exit_reason = "period_mark"
    for i in range(entry_i, period_end_i + 1):
        stop_fill = _stop_fill(bars.iloc[i], stop_price)
        if stop_fill is not None:
            exit_i, exit_price, exit_reason = i, stop_fill, "atr_stop"
            break
        # A death cross can print on the entry session after the morning fill.
        # The engine observes that completed bar on its next cycle and exits at
        # the following session's market, so entry-day crossunders must not be
        # discarded.
        if bool(bars["death_cross"].iloc[i]) and i + 1 <= period_end_i:
            exit_i = i + 1
            exit_price = float(bars["open"].iloc[exit_i])
            exit_reason = "death_cross"
            break
    return AuditTrade(
        symbol=symbol,
        policy=policy,
        signal_date=bars.index[signal_i],
        entry_date=bars.index[entry_i],
        entry_price=entry_price,
        stop_price=stop_price,
        exit_date=bars.index[exit_i],
        exit_price=exit_price,
        exit_reason=exit_reason,
    )


def simulate_period(
    *,
    symbol: str,
    bars: pd.DataFrame,
    valid_signals: pd.Series,
    start: pd.Timestamp,
    end: pd.Timestamp,
    policy: str,
) -> PeriodResult:
    """Simulate one policy with per-symbol non-overlap inside one period."""
    if policy not in {"control", "structure_stop", "pullback"}:
        raise ValueError(f"unknown policy {policy!r}")
    period_positions = np.flatnonzero((bars.index >= start) & (bars.index <= end))
    if len(period_positions) == 0:
        return PeriodResult((), ())
    period_end_i = int(period_positions[-1])
    candidates = [
        int(i) for i in np.flatnonzero(valid_signals.to_numpy())
        if start <= bars.index[int(i)] <= end and int(i) + 1 <= period_end_i
    ]
    opportunities = tuple(bars.index[i] for i in candidates)
    trades: list[AuditTrade] = []
    next_signal_i = 0
    for signal_i in candidates:
        if signal_i < next_signal_i:
            continue
        signal_atr = float(bars[f"atr_{ATR_LENGTH}"].iloc[signal_i])
        if not np.isfinite(signal_atr) or signal_atr <= 0:
            continue

        if policy == "pullback":
            limit_price = float(bars["close"].iloc[signal_i]) - PULLBACK_OFFSET_ATR * signal_atr
            entry_i: int | None = None
            entry_price: float | None = None
            last_wait_i = min(signal_i + PULLBACK_WAIT_SESSIONS, period_end_i)
            for i in range(signal_i + 1, last_wait_i + 1):
                # A failure at yesterday's close cancels before today's session.
                if i > signal_i + 1 and not (
                    float(bars[f"sma_{FAST}"].iloc[i - 1])
                    > float(bars[f"sma_{SLOW}"].iloc[i - 1])
                ):
                    break
                row = bars.iloc[i]
                if float(row.open) <= limit_price:
                    entry_i, entry_price = i, float(row.open)
                    break
                if float(row.low) <= limit_price:
                    entry_i, entry_price = i, limit_price
                    break
            if entry_i is None or entry_price is None:
                continue
            entry_atr = float(bars[f"atr_{ATR_LENGTH}"].iloc[entry_i - 1])
            stop_price = entry_price - ATR_STOP_MULTIPLIER * entry_atr
        else:
            entry_i = signal_i + 1
            entry_price = float(bars["open"].iloc[entry_i])
            control_stop = entry_price - ATR_STOP_MULTIPLIER * signal_atr
            if policy == "structure_stop":
                sma50 = float(bars[f"sma_{SLOW}"].iloc[signal_i])
                stop_price = min(control_stop, sma50 - STRUCTURE_BUFFER_ATR * signal_atr)
            else:
                stop_price = control_stop

        trade = _exit_trade(
            symbol=symbol,
            policy=policy,
            bars=bars,
            signal_i=signal_i,
            entry_i=entry_i,
            entry_price=entry_price,
            stop_price=stop_price,
            period_end_i=period_end_i,
        )
        trades.append(trade)
        exit_i = int(bars.index.get_loc(trade.exit_date))
        next_signal_i = exit_i + 1
    return PeriodResult(tuple(trades), opportunities)


def summarize(trades: Iterable[AuditTrade]) -> dict[str, float]:
    """Return the pre-registered fixed-risk policy metrics."""
    ordered = sorted(trades, key=lambda trade: (trade.exit_date, trade.symbol))
    if not ordered:
        return {
            "trades": 0.0,
            "expectancy_r": float("nan"),
            "median_r": float("nan"),
            "total_r": 0.0,
            "max_drawdown_r": float("nan"),
            "stop_rate": float("nan"),
            "win_rate": float("nan"),
            "period_marks": 0.0,
            "largest_winner_r": float("nan"),
            "largest_winner_share": float("nan"),
        }
    returns = np.asarray([trade.r_multiple for trade in ordered], dtype=float)
    curve = np.concatenate(([0.0], np.cumsum(returns)))
    running_high = np.maximum.accumulate(curve)
    max_drawdown = float(np.max(running_high - curve))
    return {
        "trades": float(len(ordered)),
        "expectancy_r": float(np.mean(returns)),
        "median_r": float(np.median(returns)),
        "total_r": float(np.sum(returns)),
        "max_drawdown_r": max_drawdown,
        "stop_rate": float(np.mean([t.exit_reason == "atr_stop" for t in ordered])),
        "win_rate": float(np.mean(returns > 0)),
        "period_marks": float(sum(t.exit_reason == "period_mark" for t in ordered)),
        "largest_winner_r": float(np.max(returns)),
        "largest_winner_share": (
            float(np.max(returns) / np.sum(returns))
            if np.sum(returns) > 0 and np.max(returns) > 0
            else float("nan")
        ),
    }


def avoided_and_blocked(
    control: Iterable[AuditTrade], variant: Iterable[AuditTrade]
) -> tuple[int, int]:
    """Count unfilled control losers avoided and winners blocked by a variant."""
    variant_signals = {(t.symbol, t.signal_date) for t in variant}
    avoided = blocked = 0
    for trade in control:
        if (trade.symbol, trade.signal_date) in variant_signals:
            continue
        if trade.r_multiple > 0:
            blocked += 1
        elif trade.r_multiple < 0:
            avoided += 1
    return avoided, blocked


def _load_cached_bars(symbol: str, feed: str) -> pd.DataFrame | None:
    path = Path("data/historical") / feed / f"{symbol}_1Day_all.parquet"
    if not path.exists():
        return None
    return pd.read_parquet(path)


def _fmt(value: float, *, pct: bool = False) -> str:
    if not np.isfinite(value):
        return "n/a"
    return f"{100 * value:.1f}%" if pct else f"{value:.2f}"


def _print_table(
    period: str,
    results: dict[str, list[AuditTrade]],
) -> None:
    control = results["control"]
    print(f"\n{period}")
    print("policy          N   mean R  median R  total R   max DD  stops  marks  losers avoided  winners blocked")
    for policy in ("control", "structure_stop", "pullback"):
        metrics = summarize(results[policy])
        avoided, blocked = avoided_and_blocked(control, results[policy])
        print(
            f"{policy:<15} {int(metrics['trades']):>3} "
            f"{_fmt(metrics['expectancy_r']):>7}R "
            f"{_fmt(metrics['median_r']):>8}R "
            f"{_fmt(metrics['total_r']):>8}R "
            f"{_fmt(metrics['max_drawdown_r']):>7}R "
            f"{_fmt(metrics['stop_rate'], pct=True):>6} "
            f"{int(metrics['period_marks']):>6} "
            f"{avoided:>15} {blocked:>16}"
        )
    control_metrics = summarize(control)
    print(
        "control concentration: largest winner="
        f"{_fmt(control_metrics['largest_winner_r'])}R "
        f"({_fmt(control_metrics['largest_winner_share'], pct=True)} of net R); "
        f"win rate={_fmt(control_metrics['win_rate'], pct=True)}"
    )


def _decision_check(
    dev: dict[str, list[AuditTrade]], holdout: dict[str, list[AuditTrade]]
) -> None:
    print("\nPRE-REGISTERED DECISION CHECK")
    control_dev = summarize(dev["control"])
    control_holdout = summarize(holdout["control"])
    for policy in ("structure_stop", "pullback"):
        dev_m = summarize(dev[policy])
        holdout_m = summarize(holdout[policy])
        retention = (
            holdout_m["trades"] / control_holdout["trades"]
            if control_holdout["trades"] else 0.0
        )
        same_direction = (
            dev_m["expectancy_r"] > control_dev["expectancy_r"]
            and holdout_m["expectancy_r"] > control_holdout["expectancy_r"]
        )
        passed = all((
            holdout_m["trades"] >= MIN_HOLDOUT_TRADES,
            retention >= MIN_RETENTION,
            same_direction,
            holdout_m["max_drawdown_r"] <= control_holdout["max_drawdown_r"],
        ))
        print(
            f"{policy}: {'PASS' if passed else 'NO CHANGE'} — "
            f"holdout N={int(holdout_m['trades'])}, retention={retention:.1%}, "
            f"dev+holdout expectancy improvement={same_direction}, "
            f"holdout DD not worse="
            f"{holdout_m['max_drawdown_r'] <= control_holdout['max_drawdown_r']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--feed", default=settings.BACKTEST_DATA_FEED, choices=("iex", "sip"))
    parser.add_argument("--end", default=DEFAULT_END.date().isoformat())
    parser.add_argument(
        "--earnings-csv",
        type=Path,
        help="Optional point-in-time earnings calendar with symbol,date columns.",
    )
    args = parser.parse_args(argv)
    end = pd.Timestamp(args.end, tz="UTC")
    earnings = load_earnings_csv(args.earnings_csv)

    spy_raw = _load_cached_bars("SPY", args.feed)
    if spy_raw is None:
        print(f"missing cached {args.feed} SPY daily bars")
        return 1
    regimes = classify_spy_regimes(spy_raw)
    period_specs = {
        "DEVELOPMENT 2017-01-01..2022-12-31": (DEV_START, DEV_END),
        f"HELD OUT 2023-01-01..{end.date()}": (HOLDOUT_START, end),
    }
    all_results: dict[str, dict[str, list[AuditTrade]]] = {}
    missing: list[str] = []
    insufficient: list[str] = []
    for period_name, (start, period_end) in period_specs.items():
        policies = {name: [] for name in ("control", "structure_stop", "pullback")}
        for symbol in AUDIT_UNIVERSE:
            raw = _load_cached_bars(symbol, args.feed)
            if raw is None:
                if symbol not in missing:
                    missing.append(symbol)
                continue
            bars = prepare_symbol_bars(raw)
            if not ((bars.index >= start) & (bars.index <= period_end)).any():
                if symbol not in insufficient:
                    insufficient.append(symbol)
                continue
            signals = valid_signal_mask(
                bars, regimes, symbol=symbol, earnings=earnings
            )
            for policy in policies:
                result = simulate_period(
                    symbol=symbol,
                    bars=bars,
                    valid_signals=signals,
                    start=start,
                    end=period_end,
                    policy=policy,
                )
                policies[policy].extend(result.trades)
        all_results[period_name] = policies

    print("SMA ENTRY QUALITY AUDIT — PLAN 11.70")
    print(f"feed={args.feed}; frozen universe={len(AUDIT_UNIVERSE)}; end={end.date()}")
    print(
        "earnings gate="
        + (f"CSV supplied ({len(earnings)} symbols)" if args.earnings_csv else "UNAVAILABLE / production fail-open")
    )
    print(f"missing cache={missing or 'none'}")
    print(f"no bars in one or more periods={insufficient or 'none'}")
    for period_name, policies in all_results.items():
        _print_table(period_name, policies)
    dev_name, holdout_name = period_specs
    _decision_check(all_results[dev_name], all_results[holdout_name])
    print("\nLimitations: current-watchlist survivorship bias; earnings coverage as labeled; ")
    print("realized-exit-sequence drawdown (not daily portfolio MTM); no sleeve/notional caps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
