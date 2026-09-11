"""Production-mirror SMA entry-quality audit (PLAN 11.70).

This script is research-only.  It does not change the paper strategy.  It
compares the control with three fixed entry/stop policies on locally cached
SIP daily bars:

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

``extension_cap``
    Skip a next-open fill more than 2 ATR above signal-day SMA50.  The 2-ATR
    threshold comes from the geometry under investigation (where the normal
    stop reaches SMA50), not a parameter sweep.

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
import math
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd

from config import settings
from indicators.technicals import add_atr, add_sma
from scripts.donchian_trail_compare import classify_spy_regime
from strategies.health.stats import bootstrap_mean_ci


FAST = 20
SLOW = 50
LONG_SMA = 200
ATR_LENGTH = settings.ATR_LENGTH
ATR_STOP_MULTIPLIER = settings.ATR_STOP_MULTIPLIER
STRUCTURE_BUFFER_ATR = 0.5
PULLBACK_OFFSET_ATR = 0.5
PULLBACK_WAIT_SESSIONS = 5
EXTENSION_CAP_ATR = 2.0
DEV_START = pd.Timestamp("2017-01-01", tz="UTC")
DEV_END = pd.Timestamp("2022-12-31", tz="UTC")
HOLDOUT_START = pd.Timestamp("2023-01-01", tz="UTC")
DEFAULT_END = pd.Timestamp("2026-09-04", tz="UTC")
MIN_HOLDOUT_TRADES = 30
MIN_RETENTION = 0.60
BOOTSTRAP_SEED = 1170
POLICIES = ("control", "structure_stop", "pullback", "extension_cap")
VARIANT_POLICIES = POLICIES[1:]

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
    skips: tuple["PolicySkip", ...]


@dataclass(frozen=True)
class PolicySkip:
    """A valid signal not entered by this policy, with explicit attribution."""

    symbol: str
    signal_date: pd.Timestamp
    reason: str


@dataclass(frozen=True)
class PeriodSpec:
    """A named sample window; role is stable even if display order changes."""

    role: str
    label: str
    start: pd.Timestamp
    end: pd.Timestamp


@dataclass(frozen=True)
class PolicyDecision:
    """Machine-testable result of the review-hardened promotion gate."""

    policy: str
    passed: bool
    holdout_trades: int
    retention: float
    expectancy_improved_both: bool
    holdout_drawdown_not_worse: bool
    holdout_delta_ci: tuple[float, float] | None
    holdout_ci_positive: bool


def build_period_specs(end: pd.Timestamp) -> tuple[PeriodSpec, ...]:
    """Return explicitly keyed development and held-out windows."""
    return (
        PeriodSpec(
            role="development",
            label="DEVELOPMENT 2017-01-01..2022-12-31",
            start=DEV_START,
            end=DEV_END,
        ),
        PeriodSpec(
            role="held_out",
            label=f"HELD OUT 2023-01-01..{end.date()}",
            start=HOLDOUT_START,
            end=end,
        ),
    )


def _utc_index(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted frame with a UTC DatetimeIndex."""
    out = frame.copy()
    index = pd.DatetimeIndex(out.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    else:
        index = index.tz_convert("UTC")
    out.index = index
    out = out.sort_index()
    if out.index.has_duplicates:
        duplicates = out.index[out.index.duplicated()].unique()
        sample = ", ".join(str(value) for value in duplicates[:3])
        raise ValueError(f"bar index contains duplicate timestamp(s): {sample}")
    return out


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


def _positive_finite(value: object, *, field: str, symbol: str, date: pd.Timestamp) -> float:
    """Return a valid positive float or fail loudly on poisoned audit input."""
    result = float(value)
    if not np.isfinite(result) or result <= 0:
        raise ValueError(
            f"{symbol} {date.date()}: {field} must be finite and positive, got {value!r}"
        )
    return result


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
    if not np.isfinite(entry_price) or entry_price <= 0:
        raise ValueError(f"entry fill must be finite and positive, got {entry_price!r}")
    if (
        not np.isfinite(stop_price)
        or stop_price <= 0
        or stop_price >= entry_price
    ):
        raise ValueError(
            f"stop must be finite, positive, and below the entry fill; got {stop_price!r}"
        )
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
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    period_positions = np.flatnonzero((bars.index >= start) & (bars.index <= end))
    if len(period_positions) == 0:
        return PeriodResult((), (), ())
    period_end_i = int(period_positions[-1])
    candidates = [
        int(i) for i in np.flatnonzero(valid_signals.to_numpy())
        if start <= bars.index[int(i)] <= end and int(i) + 1 <= period_end_i
    ]
    opportunities = tuple(bars.index[i] for i in candidates)
    trades: list[AuditTrade] = []
    skips: list[PolicySkip] = []
    next_signal_i = 0
    for signal_i in candidates:
        if signal_i < next_signal_i:
            skips.append(PolicySkip(symbol, bars.index[signal_i], "policy_overlap"))
            continue
        signal_atr = _positive_finite(
            bars[f"atr_{ATR_LENGTH}"].iloc[signal_i],
            field="signal ATR",
            symbol=symbol,
            date=bars.index[signal_i],
        )

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
                skips.append(PolicySkip(symbol, bars.index[signal_i], "pullback_unfilled"))
                continue
            # A resting order that fills several sessions later uses the ATR
            # known before its fill session, matching the engine's refreshed
            # last-ATR cache.  This intentionally differs from control only
            # because control always fills on the immediately following bar.
            entry_atr = _positive_finite(
                bars[f"atr_{ATR_LENGTH}"].iloc[entry_i - 1],
                field="pullback fill ATR",
                symbol=symbol,
                date=bars.index[entry_i - 1],
            )
            stop_price = entry_price - ATR_STOP_MULTIPLIER * entry_atr
        else:
            entry_i = signal_i + 1
            entry_price = float(bars["open"].iloc[entry_i])
            control_stop = entry_price - ATR_STOP_MULTIPLIER * signal_atr
            if policy == "structure_stop":
                sma50 = float(bars[f"sma_{SLOW}"].iloc[signal_i])
                stop_price = min(control_stop, sma50 - STRUCTURE_BUFFER_ATR * signal_atr)
            elif policy == "extension_cap":
                sma50 = float(bars[f"sma_{SLOW}"].iloc[signal_i])
                if entry_price > sma50 + EXTENSION_CAP_ATR * signal_atr:
                    skips.append(PolicySkip(symbol, bars.index[signal_i], "extension_cap"))
                    continue
                stop_price = control_stop
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
        exit_i = int(bars.index.get_indexer([trade.exit_date])[0])
        if exit_i < 0:
            raise RuntimeError(f"exit date {trade.exit_date} disappeared from bar index")
        next_signal_i = exit_i + 1
    return PeriodResult(tuple(trades), opportunities, tuple(skips))


def summarize(trades: Iterable[AuditTrade]) -> dict[str, float]:
    """Return the fixed-risk policy metrics."""
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
            "max_stop_streak": 0.0,
            "stop_clusters_4": 0.0,
            "four_stop_window_rate": float("nan"),
        }
    returns = np.asarray([trade.r_multiple for trade in ordered], dtype=float)
    curve = np.concatenate(([0.0], np.cumsum(returns)))
    running_high = np.maximum.accumulate(curve)
    max_drawdown = float(np.max(running_high - curve))
    stop_flags = [trade.exit_reason == "atr_stop" for trade in ordered]
    streak = max_streak = 0
    for stopped in stop_flags:
        streak = streak + 1 if stopped else 0
        max_streak = max(max_streak, streak)
    four_windows = [all(stop_flags[i : i + 4]) for i in range(max(0, len(stop_flags) - 3))]
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
        "max_stop_streak": float(max_streak),
        "stop_clusters_4": float(sum(four_windows)),
        "four_stop_window_rate": (
            float(np.mean(four_windows)) if four_windows else float("nan")
        ),
    }


def avoided_and_blocked(
    control: Iterable[AuditTrade],
    variant: Iterable[AuditTrade],
    skips: Iterable[PolicySkip] | None = None,
) -> tuple[int, int]:
    """Count control outcomes explicitly declined by a variant's own rule.

    ``policy_overlap`` skips are excluded: they are path-dependent bookkeeping,
    not evidence that the pullback or extension predicate selected that trade.
    """
    variant_signals = {(t.symbol, t.signal_date) for t in variant}
    attributable = (
        {
            (skip.symbol, skip.signal_date)
            for skip in skips
            if skip.reason != "policy_overlap"
        }
        if skips is not None
        else None
    )
    avoided = blocked = 0
    for trade in control:
        if (trade.symbol, trade.signal_date) in variant_signals:
            continue
        if attributable is not None and (trade.symbol, trade.signal_date) not in attributable:
            continue
        if trade.r_multiple > 0:
            blocked += 1
        elif trade.r_multiple < 0:
            avoided += 1
    return avoided, blocked


def paired_policy_deltas(
    control: Iterable[AuditTrade], variant: Iterable[AuditTrade]
) -> list[float]:
    """Align policies by signal and return variant-minus-control R deltas.

    A policy that declines a valid opportunity receives 0R for that signal.
    The union also preserves signals a different non-overlap path admits only
    in the variant.  This makes the CI compare policy opportunity sets rather
    than flattering a selective policy by averaging only its fills.
    """
    def _by_signal(trades: Iterable[AuditTrade]) -> dict[tuple[str, pd.Timestamp], float]:
        result: dict[tuple[str, pd.Timestamp], float] = {}
        for trade in trades:
            key = (trade.symbol, trade.signal_date)
            if key in result:
                raise ValueError(f"duplicate trade signal in policy result: {key}")
            result[key] = trade.r_multiple
        return result

    control_map = _by_signal(control)
    variant_map = _by_signal(variant)
    keys = sorted(set(control_map) | set(variant_map))
    return [variant_map.get(key, 0.0) - control_map.get(key, 0.0) for key in keys]


def policy_delta_ci(
    control: Iterable[AuditTrade], variant: Iterable[AuditTrade]
) -> tuple[float, float] | None:
    """Deterministic 95% bootstrap CI for mean opportunity-level R delta."""
    return bootstrap_mean_ci(
        paired_policy_deltas(control, variant),
        confidence=0.95,
        n_resamples=5000,
        seed=BOOTSTRAP_SEED,
    )


@dataclass(frozen=True)
class SelectionChance:
    """Pullback/cap skipped-outcome counts versus random chance at same rate."""

    losers_not_entered: int
    winners_not_entered: int
    expected_losers_not_entered: float
    expected_winners_not_entered: float
    worse_or_equal_p_value: float


def _hypergeom_upper_tail(*, population: int, winners: int, draws: int, observed: int) -> float:
    """Exact P[X >= observed] for a hypergeometric random variable."""
    denominator = math.comb(population, draws)
    upper = min(winners, draws)
    lower = max(observed, 0, draws - (population - winners))
    numerator = sum(
        math.comb(winners, value) * math.comb(population - winners, draws - value)
        for value in range(lower, upper + 1)
    )
    return numerator / denominator


def selection_vs_chance(
    control: Iterable[AuditTrade],
    variant: Iterable[AuditTrade],
    skips: Iterable[PolicySkip] | None = None,
) -> SelectionChance | None:
    """Compare skipped control outcomes with random skipping at the same rate."""
    control_list = list(control)
    losers, winners = avoided_and_blocked(control_list, variant, skips)
    skipped = losers + winners
    if not control_list or skipped == 0:
        return None
    total_winners = sum(trade.r_multiple > 0 for trade in control_list)
    total_losers = sum(trade.r_multiple < 0 for trade in control_list)
    return SelectionChance(
        losers_not_entered=losers,
        winners_not_entered=winners,
        expected_losers_not_entered=skipped * total_losers / len(control_list),
        expected_winners_not_entered=skipped * total_winners / len(control_list),
        worse_or_equal_p_value=_hypergeom_upper_tail(
            population=len(control_list),
            winners=total_winners,
            draws=skipped,
            observed=winners,
        ),
    )


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
    results: dict[str, PeriodResult],
) -> None:
    control = results["control"].trades
    print(f"\n{period}")
    print(
        "policy          N   mean R  median R  total R   max DD  stops  marks  "
        "control losses not entered  control winners not entered"
    )
    for policy in POLICIES:
        metrics = summarize(results[policy].trades)
        avoided, blocked = avoided_and_blocked(
            control, results[policy].trades, results[policy].skips
        )
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
    print(
        "control stop-cluster base rate: "
        f"max streak={int(control_metrics['max_stop_streak'])}; "
        f"four-stop windows={int(control_metrics['stop_clusters_4'])}; "
        f"window rate={_fmt(control_metrics['four_stop_window_rate'], pct=True)}"
    )
    for policy in ("pullback", "extension_cap"):
        chance = selection_vs_chance(
            control, results[policy].trades, results[policy].skips
        )
        if chance is not None:
            print(
                f"{policy} selection vs random at same skip count: "
                f"losses not entered={chance.losers_not_entered} "
                f"(random {chance.expected_losers_not_entered:.1f}); "
                f"winners not entered={chance.winners_not_entered} "
                f"(random {chance.expected_winners_not_entered:.1f}); "
                f"P(random omits >= observed winners)="
                f"{chance.worse_or_equal_p_value:.3f}"
            )


def evaluate_decisions(
    *,
    development: dict[str, PeriodResult],
    held_out: dict[str, PeriodResult],
) -> tuple[PolicyDecision, ...]:
    """Evaluate variants using explicit sample roles and paired uncertainty."""
    control_dev = summarize(development["control"].trades)
    control_holdout = summarize(held_out["control"].trades)
    decisions: list[PolicyDecision] = []
    for policy in VARIANT_POLICIES:
        dev_m = summarize(development[policy].trades)
        holdout_m = summarize(held_out[policy].trades)
        retention = (
            holdout_m["trades"] / control_holdout["trades"]
            if control_holdout["trades"] else 0.0
        )
        same_direction = (
            dev_m["expectancy_r"] > control_dev["expectancy_r"]
            and holdout_m["expectancy_r"] > control_holdout["expectancy_r"]
        )
        delta_ci = policy_delta_ci(
            held_out["control"].trades, held_out[policy].trades
        )
        ci_positive = delta_ci is not None and delta_ci[0] > 0.0
        drawdown_ok = holdout_m["max_drawdown_r"] <= control_holdout["max_drawdown_r"]
        passed = all((
            holdout_m["trades"] >= MIN_HOLDOUT_TRADES,
            retention >= MIN_RETENTION,
            same_direction,
            drawdown_ok,
            ci_positive,
        ))
        decisions.append(PolicyDecision(
            policy=policy,
            passed=passed,
            holdout_trades=int(holdout_m["trades"]),
            retention=retention,
            expectancy_improved_both=same_direction,
            holdout_drawdown_not_worse=drawdown_ok,
            holdout_delta_ci=delta_ci,
            holdout_ci_positive=ci_positive,
        ))
    return tuple(decisions)


def _decision_check(
    *,
    development: dict[str, PeriodResult],
    held_out: dict[str, PeriodResult],
) -> None:
    print("\nREVIEW-HARDENED DECISION CHECK")
    for decision in evaluate_decisions(development=development, held_out=held_out):
        ci_text = (
            "n/a"
            if decision.holdout_delta_ci is None
            else f"[{decision.holdout_delta_ci[0]:.2f}, {decision.holdout_delta_ci[1]:.2f}]R"
        )
        print(
            f"{decision.policy}: {'PASS' if decision.passed else 'NO CHANGE'} — "
            f"holdout N={decision.holdout_trades}, "
            f"retention={decision.retention:.1%}, "
            f"dev+holdout expectancy improvement="
            f"{decision.expectancy_improved_both}, "
            f"holdout DD not worse={decision.holdout_drawdown_not_worse}, "
            f"paired delta 95% CI={ci_text}, "
            f"CI > 0={decision.holdout_ci_positive}"
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
    regimes = classify_spy_regime(_utc_index(spy_raw))
    period_specs = build_period_specs(end)
    all_results: dict[str, dict[str, PeriodResult]] = {}
    missing: list[str] = []
    insufficient: list[str] = []
    for period in period_specs:
        trades_by_policy: dict[str, list[AuditTrade]] = {name: [] for name in POLICIES}
        opportunities_by_policy: dict[str, list[pd.Timestamp]] = {
            name: [] for name in POLICIES
        }
        skips_by_policy: dict[str, list[PolicySkip]] = {name: [] for name in POLICIES}
        for symbol in AUDIT_UNIVERSE:
            raw = _load_cached_bars(symbol, args.feed)
            if raw is None:
                if symbol not in missing:
                    missing.append(symbol)
                continue
            bars = prepare_symbol_bars(raw)
            if not ((bars.index >= period.start) & (bars.index <= period.end)).any():
                if symbol not in insufficient:
                    insufficient.append(symbol)
                continue
            signals = valid_signal_mask(
                bars, regimes, symbol=symbol, earnings=earnings
            )
            for policy in POLICIES:
                result = simulate_period(
                    symbol=symbol,
                    bars=bars,
                    valid_signals=signals,
                    start=period.start,
                    end=period.end,
                    policy=policy,
                )
                trades_by_policy[policy].extend(result.trades)
                opportunities_by_policy[policy].extend(result.opportunities)
                skips_by_policy[policy].extend(result.skips)
        all_results[period.role] = {
            policy: PeriodResult(
                tuple(trades_by_policy[policy]),
                tuple(opportunities_by_policy[policy]),
                tuple(skips_by_policy[policy]),
            )
            for policy in POLICIES
        }

    print("SMA ENTRY QUALITY AUDIT — PLAN 11.70")
    print(f"feed={args.feed}; frozen universe={len(AUDIT_UNIVERSE)}; end={end.date()}")
    print(
        "earnings gate="
        + (f"CSV supplied ({len(earnings)} symbols)" if args.earnings_csv else "UNAVAILABLE / production fail-open")
    )
    print(f"missing cache={missing or 'none'}")
    print(f"no bars in one or more periods={insufficient or 'none'}")
    for period in period_specs:
        _print_table(period.label, all_results[period.role])
    _decision_check(
        development=all_results["development"],
        held_out=all_results["held_out"],
    )
    print("\nLimitations: current-watchlist survivorship bias; earnings coverage as labeled; ")
    print("realized-exit-sequence drawdown (not daily portfolio MTM); no sleeve/notional caps.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
