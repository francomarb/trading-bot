"""
SMA Crossover audit — replays every 20/50 SMA round trip on daily Alpaca
IEX bars and compares full exit policies (baseline death-cross+ATR stop vs
alternative exits) on an apples-to-apples basis.

Methodology notes (reviewed against ChatGPT P1 feedback 2026-06-06)
------------------------------------------------------------------

**Universe is pinned, not read from settings.** The documented results
use a frozen 40-symbol audit universe (`AUDIT_UNIVERSE` below). This is
the universe the docs and the historical numbers refer to. The current
production watchlist (`settings.SMA_WATCHLIST`) drifts as names are
added or culled — auditing against it would make every doc number a
moving target. Use `--universe current` to override.

**Alternative exits are replayed on every entry, not just death-cross
winners.** An earlier version of this audit only simulated chandelier /
gated-trail / take-profit overlays on the 174 death-cross winners,
excluding the 336 trades that died at the ATR stop. That was
selection-biased: a chandelier could have cut a losing trade earlier (or
later, or made no difference); a +10% take-profit could have captured a
small win on a trade that subsequently failed at the ATR stop. The
current implementation simulates each *complete* exit policy (death
cross + ATR stop + optional overlay) from entry to exit and compares
aggregate net P&L across **all** entries.

**Limitations not addressed by this audit.**

- Unit shares; no ATR-risk position sizing. P&L is per-share, comparable
  across policies but not to live equity-curve impact.
- No entry filters applied. Production runs `SMAEdgeFilter`,
  `SectorMomentumFilter`, `SPYTrendFilter`, regime gate, and earnings
  blackout. None of those are replayed here.
- In-sample: the full window is used for both signal generation and
  policy comparison. Walk-forward / OOS validation has not been
  performed.
- Gap-through-stop fills at the stop level, not at the gap-down open.
  This understates losses on gappy names — see the `_fill_through_stop`
  helper for the assumption.

Any operational decision (e.g., cull a name from the production
watchlist) should be supported by an audit that addresses these
limitations.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import statistics
import sys

import numpy as np
import pandas as pd

from config import settings
from data.fetcher import fetch_symbol
from indicators.technicals import add_sma, add_atr


# ── Audit configuration (pinned for reproducibility) ────────────────────────

FAST = 20
SLOW = 50
ATR_LEN = 14
ATR_STOP_MULT = 2.0
TRAIL_KS = (2.5, 3.0, 3.5)
PROFIT_TARGETS_PCT = (0.10, 0.20, 0.30, 0.50, 0.75, 1.00, 1.50)

# Profit-gated trail grid: activation threshold (in ATR units of unrealized
# profit) × trail distance (in ATR units below HWM).
GATED_GRID = [
    (2.0, 3.0),
    (2.0, 4.0),
    (3.0, 3.0),
    (3.0, 4.0),
    (3.0, 5.0),
    (4.0, 3.0),
    (4.0, 4.0),
    (4.0, 5.0),
    (5.0, 4.0),
    (5.0, 5.0),
]

# Audited universe — 40 symbols, frozen at the time of the original audit.
# Docs reference results computed on this set. Do not change without bumping
# the audit doc baseline.
AUDIT_UNIVERSE: tuple[str, ...] = (
    "SNDK", "WDC", "STX", "GSAT", "POWL", "VIAV", "VSAT", "CIEN", "ASML",
    "MSTR", "MU", "FORM", "ALB", "CSTM", "DOCN", "TTMI", "FRO", "MTZ",
    "DK", "ASX", "CAT", "HUT", "GLW", "AMD", "STRL", "INTC", "BE", "ECG",
    "MRVL", "NVT", "SQM", "TSEM", "PL", "UBER", "NVDA", "ADBE", "ANET",
    "META", "PLTR", "DUOL",
)

AUDIT_START = datetime(2018, 11, 1, tzinfo=timezone.utc)
AUDIT_END = datetime(2026, 6, 5, tzinfo=timezone.utc)


# ── Trade data model ────────────────────────────────────────────────────────


@dataclass
class Trade:
    """A single round-trip trade under a single exit policy."""

    symbol: str
    policy: str                          # human-readable policy name
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str                     # "death_cross" | "atr_stop" | "trail" | "take_profit" | "eod"
    hwm_close: float                     # max close from entry to exit
    hwm_date: pd.Timestamp
    atr_at_entry: float

    @property
    def pnl(self) -> float:
        return self.exit_price - self.entry_price

    @property
    def peak_open_profit(self) -> float:
        return self.hwm_close - self.entry_price

    @property
    def giveback_dollars(self) -> float:
        return self.hwm_close - self.exit_price

    @property
    def giveback_pct(self) -> float:
        if self.peak_open_profit <= 0:
            return float("nan")
        return self.giveback_dollars / self.peak_open_profit

    @property
    def giveback_atr(self) -> float:
        if self.atr_at_entry <= 0:
            return float("nan")
        return self.giveback_dollars / self.atr_at_entry


# ── Exit policies ───────────────────────────────────────────────────────────


def _fill_through_stop(low: float, stop: float) -> float:
    """
    Assumed fill price when intrabar low touches the stop. Returns the stop
    level (best-case: clean stop-limit fill at the trigger). A gappy bar
    that opens below the stop would fill worse in production; this audit
    intentionally understates that loss because the magnitude is
    instrument-specific and the audit's job is *relative* comparison
    across policies. Documented as a known limitation.
    """
    # If the gap was so violent the *low* was already well below the stop,
    # the live fill would also be below the stop. We do not model that.
    return stop


@dataclass
class _BarsView:
    """Cached per-symbol arrays passed into the exit-policy simulators."""

    dates: pd.DatetimeIndex
    opens: np.ndarray
    highs: np.ndarray
    lows: np.ndarray
    closes: np.ndarray
    fast: np.ndarray
    slow: np.ndarray
    atr: np.ndarray


def _prepare_bars(df: pd.DataFrame) -> _BarsView | None:
    """Add SMAs + ATR, drop warmup NaNs, and freeze arrays for the loops."""
    df = add_sma(df, FAST)
    df = add_sma(df, SLOW)
    df = add_atr(df, ATR_LEN)
    df = df.dropna(subset=[f"sma_{FAST}", f"sma_{SLOW}", f"atr_{ATR_LEN}"]).copy()
    if df.empty:
        return None
    return _BarsView(
        dates=df.index,
        opens=df["open"].to_numpy(),
        highs=df["high"].to_numpy(),
        lows=df["low"].to_numpy(),
        closes=df["close"].to_numpy(),
        fast=df[f"sma_{FAST}"].to_numpy(),
        slow=df[f"sma_{SLOW}"].to_numpy(),
        atr=df[f"atr_{ATR_LEN}"].to_numpy(),
    )


def _iter_entries(bars: _BarsView) -> list[int]:
    """
    Return the entry bar indices (i+1 — execution shifts to next open)
    for every golden-cross signal. We pair entries to exits trade-by-trade
    in the policy simulators; this helper just identifies the signal bars.
    """
    entry_indices: list[int] = []
    n = len(bars.closes)
    for i in range(1, n):
        if bars.fast[i] > bars.slow[i] and bars.fast[i - 1] <= bars.slow[i - 1]:
            if i + 1 < n:
                entry_indices.append(i + 1)
    return entry_indices


# Each policy is a function:
#   (bars: _BarsView, entry_idx: int) -> (exit_idx: int, exit_price: float,
#                                          exit_reason: str, hwm_close: float,
#                                          hwm_idx: int)
# entry_idx is the bar where we *fill* the entry (one bar after the signal).
# The simulator walks forward from entry_idx until the policy's stop fires
# or the dataset ends.


def _policy_baseline(
    bars: _BarsView, entry_idx: int
) -> tuple[int, float, str, float, int]:
    """Death-cross exit + 2.0×ATR static stop (the current production policy)."""
    entry_price = bars.opens[entry_idx]
    entry_atr = bars.atr[entry_idx - 1]    # ATR available at signal-bar close
    stop_level = entry_price - ATR_STOP_MULT * entry_atr
    hwm_close = bars.closes[entry_idx]
    hwm_idx = entry_idx
    n = len(bars.closes)

    for i in range(entry_idx + 1, n):
        # Stop check first — intrabar low touching the stop exits at the stop.
        if bars.lows[i] <= stop_level:
            return (i, _fill_through_stop(bars.lows[i], stop_level),
                    "atr_stop", hwm_close, hwm_idx)
        # Update HWM on close.
        if bars.closes[i] > hwm_close:
            hwm_close = bars.closes[i]
            hwm_idx = i
        # Death cross at bar i → exit at bar i+1 open.
        if (bars.fast[i] < bars.slow[i] and bars.fast[i - 1] >= bars.slow[i - 1]
                and i + 1 < n):
            return (i + 1, bars.opens[i + 1], "death_cross", hwm_close, hwm_idx)

    # Walked off the end — close at last bar.
    return (n - 1, bars.closes[n - 1], "eod", hwm_close, hwm_idx)


def _policy_chandelier(
    bars: _BarsView, entry_idx: int, k: float
) -> tuple[int, float, str, float, int]:
    """
    Death-cross exit + 2.0×ATR disaster stop + chandelier trail (HWM−k·ATR).
    Whichever fires first wins.
    """
    entry_price = bars.opens[entry_idx]
    entry_atr = bars.atr[entry_idx - 1]
    disaster_stop = entry_price - ATR_STOP_MULT * entry_atr
    hwm_close = bars.closes[entry_idx]
    hwm_idx = entry_idx
    n = len(bars.closes)

    for i in range(entry_idx + 1, n):
        trail_stop = hwm_close - k * entry_atr
        # The trail stop is below HWM by k·ATR. The disaster stop is fixed at
        # entry − 2·ATR. Effective stop on this bar is max(both) since either
        # firing ends the trade.
        effective_stop = max(disaster_stop, trail_stop)
        if bars.lows[i] <= effective_stop:
            reason = "trail" if trail_stop >= disaster_stop else "atr_stop"
            return (i, _fill_through_stop(bars.lows[i], effective_stop),
                    reason, hwm_close, hwm_idx)
        if bars.closes[i] > hwm_close:
            hwm_close = bars.closes[i]
            hwm_idx = i
        if (bars.fast[i] < bars.slow[i] and bars.fast[i - 1] >= bars.slow[i - 1]
                and i + 1 < n):
            return (i + 1, bars.opens[i + 1], "death_cross", hwm_close, hwm_idx)

    return (n - 1, bars.closes[n - 1], "eod", hwm_close, hwm_idx)


def _policy_gated_trail(
    bars: _BarsView, entry_idx: int, activation_k: float, trail_k: float
) -> tuple[int, float, str, float, int]:
    """
    Death-cross + 2.0×ATR disaster + chandelier trail that ARMS only after
    unrealized close-profit ≥ activation_k × ATR.
    """
    entry_price = bars.opens[entry_idx]
    entry_atr = bars.atr[entry_idx - 1]
    disaster_stop = entry_price - ATR_STOP_MULT * entry_atr
    hwm_close = bars.closes[entry_idx]
    hwm_idx = entry_idx
    armed = False
    n = len(bars.closes)

    for i in range(entry_idx + 1, n):
        # Effective stop depends on whether the trail has armed.
        if armed:
            effective_stop = max(disaster_stop, hwm_close - trail_k * entry_atr)
        else:
            effective_stop = disaster_stop
        if bars.lows[i] <= effective_stop:
            reason = "trail" if armed and effective_stop > disaster_stop else "atr_stop"
            return (i, _fill_through_stop(bars.lows[i], effective_stop),
                    reason, hwm_close, hwm_idx)
        if bars.closes[i] > hwm_close:
            hwm_close = bars.closes[i]
            hwm_idx = i
        if not armed and (bars.closes[i] - entry_price) >= activation_k * entry_atr:
            armed = True
        if (bars.fast[i] < bars.slow[i] and bars.fast[i - 1] >= bars.slow[i - 1]
                and i + 1 < n):
            return (i + 1, bars.opens[i + 1], "death_cross", hwm_close, hwm_idx)

    return (n - 1, bars.closes[n - 1], "eod", hwm_close, hwm_idx)


def _policy_take_profit(
    bars: _BarsView, entry_idx: int, target_pct: float
) -> tuple[int, float, str, float, int]:
    """
    Death-cross + 2.0×ATR disaster + fixed-% take-profit at
    entry × (1 + target_pct). Intrabar HIGH touching the target fills at
    the target price.
    """
    entry_price = bars.opens[entry_idx]
    entry_atr = bars.atr[entry_idx - 1]
    disaster_stop = entry_price - ATR_STOP_MULT * entry_atr
    target_price = entry_price * (1.0 + target_pct)
    hwm_close = bars.closes[entry_idx]
    hwm_idx = entry_idx
    n = len(bars.closes)

    for i in range(entry_idx + 1, n):
        # Order on the bar: stop first (conservative — assume worst-case
        # ordering of intrabar prices). Then take-profit. Then death cross.
        if bars.lows[i] <= disaster_stop:
            return (i, _fill_through_stop(bars.lows[i], disaster_stop),
                    "atr_stop", hwm_close, hwm_idx)
        if bars.highs[i] >= target_price:
            return (i, target_price, "take_profit", hwm_close, hwm_idx)
        if bars.closes[i] > hwm_close:
            hwm_close = bars.closes[i]
            hwm_idx = i
        if (bars.fast[i] < bars.slow[i] and bars.fast[i - 1] >= bars.slow[i - 1]
                and i + 1 < n):
            return (i + 1, bars.opens[i + 1], "death_cross", hwm_close, hwm_idx)

    return (n - 1, bars.closes[n - 1], "eod", hwm_close, hwm_idx)


# ── Per-symbol simulator ────────────────────────────────────────────────────


def simulate_symbol(
    symbol: str, df: pd.DataFrame, policy_name: str = "baseline",
    policy_args: tuple = (),
) -> list[Trade]:
    """
    Walk every 20/50 round trip in df under the named exit policy.

    policy_name in {"baseline", "chandelier", "gated", "take_profit"}.
    policy_args is positional args for the policy beyond (bars, entry_idx).
    """
    bars = _prepare_bars(df)
    if bars is None:
        return []

    policy_fn = {
        "baseline":     _policy_baseline,
        "chandelier":   _policy_chandelier,
        "gated":        _policy_gated_trail,
        "take_profit":  _policy_take_profit,
    }[policy_name]

    trades: list[Trade] = []
    n = len(bars.closes)
    next_allowed_entry = 0
    for entry_idx in _iter_entries(bars):
        # Cannot enter while in a prior position — skip overlapping signals.
        if entry_idx < next_allowed_entry:
            continue
        exit_idx, exit_price, exit_reason, hwm_close, hwm_idx = policy_fn(
            bars, entry_idx, *policy_args
        )
        trades.append(Trade(
            symbol=symbol,
            policy=f"{policy_name}{policy_args or ''}",
            entry_date=bars.dates[entry_idx],
            entry_price=bars.opens[entry_idx],
            exit_date=bars.dates[exit_idx],
            exit_price=exit_price,
            exit_reason=exit_reason,
            hwm_close=hwm_close,
            hwm_date=bars.dates[hwm_idx],
            atr_at_entry=bars.atr[entry_idx - 1],
        ))
        next_allowed_entry = exit_idx + 1
        if next_allowed_entry >= n:
            break
    return trades


# ── Audit reporting ─────────────────────────────────────────────────────────


@dataclass
class PolicyResult:
    name: str
    trades: list[Trade] = field(default_factory=list)

    @property
    def net_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def n_winners(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        return self.n_winners / self.n if self.n else float("nan")

    @property
    def avg_trade(self) -> float:
        return self.net_pnl / self.n if self.n else float("nan")


def _fmt_pct(x: float) -> str:
    if x != x:
        return "  n/a"
    return f"{x*100:5.1f}%"


def _pct(arr: list[float], p: float) -> float:
    if not arr:
        return float("nan")
    return float(np.percentile(arr, p))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--universe", choices=("audit", "current"), default="audit",
        help="audit = frozen 40-symbol AUDIT_UNIVERSE (reproduces docs); "
             "current = settings.SMA_WATCHLIST (current production list).",
    )
    p.add_argument(
        "--start", default=AUDIT_START.date().isoformat(),
        help="Start date (YYYY-MM-DD). Default: 2018-11-01.",
    )
    p.add_argument(
        "--end", default=AUDIT_END.date().isoformat(),
        help="End date (YYYY-MM-DD). Default: 2026-06-05.",
    )
    args = p.parse_args(argv)

    if args.universe == "audit":
        symbols = list(AUDIT_UNIVERSE)
    else:
        symbols = list(settings.SMA_WATCHLIST)

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)

    print(f"Auditing universe={args.universe} ({len(symbols)} symbols), "
          f"window={start.date()} → {end.date()}", file=sys.stderr)

    # Fetch all symbols once; rerun every policy on the same cached bars.
    bar_cache: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        try:
            df, _ = fetch_symbol(sym, start, end, timeframe="1Day", use_cache=True)
        except Exception as exc:
            print(f"  {sym}: fetch failed — {exc}", file=sys.stderr)
            continue
        if len(df) < SLOW + 5:
            print(f"  {sym}: only {len(df)} bars — skipped", file=sys.stderr)
            continue
        bar_cache[sym] = df

    # ── Policies to compare ─────────────────────────────────────────────
    policies: list[tuple[str, str, tuple]] = [
        ("baseline (death cross + 2.0 ATR stop)", "baseline", ()),
    ]
    for k in TRAIL_KS:
        policies.append((f"chandelier K={k}",   "chandelier", (k,)))
    for ak, tk in GATED_GRID:
        policies.append(
            (f"gated trail arm={ak} ATR / k={tk} ATR", "gated", (ak, tk))
        )
    for tgt in PROFIT_TARGETS_PCT:
        policies.append((f"take-profit +{int(tgt*100)}%", "take_profit", (tgt,)))

    results: list[PolicyResult] = []
    for label, name, pargs in policies:
        result = PolicyResult(name=label)
        for sym, df in bar_cache.items():
            result.trades.extend(simulate_symbol(sym, df, name, pargs))
        results.append(result)

    # ── Report ─────────────────────────────────────────────────────────
    baseline = results[0]
    print()
    print("=" * 80)
    print("SMA CROSSOVER — UNIFIED EXIT-POLICY COMPARISON")
    print("=" * 80)
    print(f"Window: {start.date()} → {end.date()}")
    print(f"Universe: {args.universe} ({len(bar_cache)} symbols with sufficient bars)")
    print(f"Baseline: {baseline.name}")
    print(f"  Total entries: {baseline.n}")
    print(f"  Net P&L:       ${baseline.net_pnl:>12,.0f}")
    print(f"  Win rate:      {baseline.win_rate*100:5.1f}%")
    print(f"  Avg trade:     ${baseline.avg_trade:>9.2f}")

    # Exit-reason breakdown for the baseline (to recover the old "336 ATR
    # stops / 210 death-crosses" headline).
    reason_counts: dict[str, int] = {}
    for t in baseline.trades:
        reason_counts[t.exit_reason] = reason_counts.get(t.exit_reason, 0) + 1
    print("  Exit reasons:  " + ", ".join(
        f"{k}={v}" for k, v in sorted(reason_counts.items(), key=lambda kv: -kv[1])
    ))

    # ── Baseline giveback distribution ─────────────────────────────────
    print()
    print("─" * 80)
    print("BASELINE GIVEBACK DISTRIBUTION (death-cross WINNERS only)")
    print("─" * 80)
    dc_winners = [t for t in baseline.trades
                  if t.exit_reason == "death_cross" and t.pnl > 0]
    print(f"  N = {len(dc_winners)} winners that rode the trend to its natural exit")
    if dc_winners:
        gb_pct = sorted([t.giveback_pct for t in dc_winners if t.giveback_pct == t.giveback_pct])
        gb_atr = sorted([t.giveback_atr for t in dc_winners if t.giveback_atr == t.giveback_atr])
        print(f"  Giveback as % of peak open profit:")
        print(f"    median={_fmt_pct(_pct(gb_pct, 50))}  "
              f"mean={_fmt_pct(statistics.mean(gb_pct))}  "
              f"P75={_fmt_pct(_pct(gb_pct, 75))}  "
              f"P90={_fmt_pct(_pct(gb_pct, 90))}  "
              f"max={_fmt_pct(max(gb_pct))}")
        print(f"  Giveback in ATR-units (at entry):")
        print(f"    median={_pct(gb_atr, 50):4.2f}  "
              f"P75={_pct(gb_atr, 75):4.2f}  "
              f"P90={_pct(gb_atr, 90):4.2f}")
        baseline_peak = sum(t.peak_open_profit for t in dc_winners)
        baseline_dc_pnl = sum(t.pnl for t in dc_winners)
        if baseline_peak > 0:
            print(f"  Capture ratio: {baseline_dc_pnl/baseline_peak*100:5.1f}% of peak open profit")

    # ── Policy comparison table (the headline output) ──────────────────
    print()
    print("─" * 80)
    print("FULL POLICY COMPARISON — all entries, every policy")
    print("(net P&L Δ is the apples-to-apples comparison)")
    print("─" * 80)
    print(f"  {'policy':<46} {'n':>5} {'net $':>10} {'Δ vs base':>11} "
          f"{'win%':>6} {'avg':>7}")
    for r in results:
        delta = r.net_pnl - baseline.net_pnl
        print(f"  {r.name:<46} {r.n:>5} ${r.net_pnl:>9,.0f} "
              f"${delta:>+10,.0f} {r.win_rate*100:>5.1f}% ${r.avg_trade:>6.2f}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
