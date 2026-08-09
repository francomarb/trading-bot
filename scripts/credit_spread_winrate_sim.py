"""
Credit-spread win rate under the LIVE exit rules (PLAN 11.57 step 4).

`docs/credit_spread_strategy.md` §8 projects a 72-80% win rate. That figure
is P(short put expires OTM) for a 17-delta put — but the strategy never
holds to expiration. `stop_loss_multiple = 2.0` closes the spread whenever
its mid doubles, which happens on a far shallower move than a strike
breach, so the projection describes a structure the bot does not trade.

At `profit_target_pct = 0.50` against a stop at 2x credit the payoff is
+0.5C / -1.0C, so breakeven needs 66.7% of trades won. This harness asks
whether the exit ladder actually clears that.

What it measures, and what it cannot
------------------------------------

**Path geometry, not P&L.** For each historical entry it walks the
underlying forward day by day, reprices the spread, and applies the live
exit ladder in order. "How often does the spread double before it halves"
is a question about paths and is model-able. "What would we have made" is
not: OPRA chains, multi-leg quotes and combo fills are not replayable
locally (PR #80).

Modelling choices, each stated because each moves the answer:

* **Vol follows the real VIX path**, not a frozen entry value. Vol expands
  precisely when these positions lose, so freezing it would flatter the
  result.
* **Strikes are chosen at the TRUE delta** for the configured target, so
  this measures the intended design rather than production's current
  delta bias (that is 11.57 steps 1-3).
* **No bid/ask, no volatility skew, no early assignment.** All three make
  real outcomes worse, so every win rate here is an **upper bound**.
* **Pricing is Black-Scholes at VIX** — the same model, and the same
  known bias, the live picker uses. It tracks SPY closely and understates
  QQQ badly; see the §"QQQ" note in the results. Cross-instrument
  comparisons carry that bias, so read the SPY/QQQ *asymmetry* rather
  than QQQ's absolute numbers.
* **Entries overlap.** One entry every 7 days against holds of up to 37
  days means ~5 concurrent positions, so N entries is worth substantially
  fewer independent observations than N.

Reproduce
---------

    venv/bin/python -m scripts.credit_spread_winrate_sim
    venv/bin/python -m scripts.credit_spread_winrate_sim --delta-sweep
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from blackscholes import BlackScholesPut

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from config import settings  # noqa: E402
from utils.options_lookup import _RISK_FREE_RATE  # noqa: E402

DTE_ENTRY = 37          # midpoint of the live 30-45 window
ENTRY_EVERY = 7         # mirrors min_dte_gap_between_opens
STRIKE_GRID = 1.0       # $1 strikes


# ── Pricing (pure) ──────────────────────────────────────────────────────────


def put_price(S: float, K: float, T: float, sigma: float) -> float:
    bs = BlackScholesPut(S=S, K=K, T=max(T, 1e-4), r=_RISK_FREE_RATE, sigma=sigma)
    return float(bs.price())


def put_delta(S: float, K: float, T: float, sigma: float) -> float:
    bs = BlackScholesPut(S=S, K=K, T=max(T, 1e-4), r=_RISK_FREE_RATE, sigma=sigma)
    return abs(float(bs.delta()))


def spread_mid(S: float, Ks: float, Kl: float, T: float, sigma: float) -> float:
    """Bull put spread: short the higher strike, long ``width`` below."""
    return put_price(S, Ks, T, sigma) - put_price(S, Kl, T, sigma)


def strike_at_delta(
    S: float, T: float, sigma: float, target_delta: float,
    *, floor_pct: float = 0.70,
) -> float | None:
    """Short strike on the $1 grid whose TRUE |delta| is nearest ``target``.

    Walks down from spot; stops once delta has fallen well through the
    target so a deep-OTM tail is not scanned pointlessly. Returns None if
    the search leaves the floor without finding anything.
    """
    best: float | None = None
    best_err = float("inf")
    k = float(int(S))
    while k > S * floor_pct:
        d = put_delta(S, k, T, sigma)
        err = abs(d - target_delta)
        if err < best_err:
            best, best_err = k, err
        if d < target_delta * 0.4:
            break
        k -= STRIKE_GRID
    return best


# ── Exit ladder (pure) ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExitRules:
    """The live exit triggers.

    Field order here is declarative only — the precedence that matters is
    encoded in `simulate_one` and mirrors `CreditSpread._classify_exit`.
    """

    profit_target_pct: float = 0.50
    stop_loss_multiple: float = 2.0
    time_stop_dte: int = 21
    exit_on_short_strike_breach: bool = True


@dataclass(frozen=True)
class SimResult:
    outcome: str          # profit_target | stop_loss | breach | time_stop | expired
    exit_mid: float
    held_days: int
    pnl: float            # credit - exit_mid, $/share


def simulate_one(
    *,
    credit: float,
    short_strike: float,
    long_strike: float,
    spot_path: list[float],
    vol_path: list[float],
    dte_at_entry: int = DTE_ENTRY,
    rules: ExitRules | None = None,
) -> SimResult | None:
    """Walk one spread forward and return how the live ladder closed it.

    ``spot_path`` / ``vol_path`` start at the bar AFTER entry (index 0 is
    day 1 of the hold). ``vol_path`` is in decimal sigma, not index points.

    Trigger precedence mirrors `CreditSpread._classify_exit`: profit
    target, stop loss, time stop, then short-strike breach. A spread can
    satisfy two on the same bar — a gap through the short strike also
    doubles the mid — and which one is recorded changes the outcome mix,
    so the order is load-bearing and must not drift from production.
    Outcome labels match production's too (`defensive_breach`), so the two
    can be compared without a translation table.

    Returns None when the paths run out before any trigger fires, which
    the caller should drop rather than score.
    """
    rules = rules or ExitRules()
    for i, (S, sigma) in enumerate(zip(spot_path, vol_path), start=1):
        if not np.isfinite(S) or S <= 0 or not np.isfinite(sigma) or sigma <= 0:
            continue
        dte_left = dte_at_entry - i
        mid = spread_mid(S, short_strike, long_strike, max(dte_left, 0) / 365.0, sigma)
        if mid <= rules.profit_target_pct * credit:
            return SimResult("profit_target", mid, i, credit - mid)
        if mid >= rules.stop_loss_multiple * credit:
            return SimResult("stop_loss", mid, i, credit - mid)
        if dte_left <= rules.time_stop_dte:
            return SimResult("time_stop", mid, i, credit - mid)
        if rules.exit_on_short_strike_breach and S <= short_strike:
            return SimResult("defensive_breach", mid, i, credit - mid)
    return None


def breakeven_win_rate(avg_win: float, avg_loss: float) -> float:
    """Win rate at which expectancy is zero. ``avg_loss`` is negative."""
    a, b = float(avg_win), abs(float(avg_loss))
    return b / (a + b) if (a + b) > 0 else float("nan")


# ── Historical driver ───────────────────────────────────────────────────────


def _load(symbol: str, start: datetime, end: datetime) -> pd.Series:
    from data.fetcher import fetch_symbol

    df, _ = fetch_symbol(
        symbol, start=start, end=end, timeframe="1Day",
        feed=settings.BACKTEST_DATA_FEED,
    )
    df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
    return df["close"]


def _load_vix(start: datetime, end: datetime) -> pd.Series:
    import yfinance as yf

    s = yf.Ticker("^VIX").history(start=start, end=end)["Close"]
    s.index = pd.to_datetime(s.index).tz_localize(None).normalize()
    return s


def run_symbol(
    symbol: str, *, target_delta: float, width: float,
    min_credit_pct: float, closes: pd.Series, vix: pd.Series,
) -> pd.DataFrame:
    rows = []
    idx = closes.index
    for i in range(0, len(idx) - DTE_ENTRY - 5, ENTRY_EVERY):
        S0 = float(closes.iloc[i])
        sig0 = float(vix.asof(idx[i])) / 100.0
        if not np.isfinite(sig0) or sig0 <= 0:
            continue
        T0 = DTE_ENTRY / 365.0
        Ks = strike_at_delta(S0, T0, sig0, target_delta)
        if Ks is None:
            continue
        Kl = Ks - width
        credit = spread_mid(S0, Ks, Kl, T0, sig0)
        cleared = credit > 0 and (credit / width) >= min_credit_pct
        if not cleared:
            rows.append(dict(
                symbol=symbol, entry=str(idx[i].date()),
                cred_pct=round(100 * max(credit, 0) / width, 1),
                cleared_floor=False, outcome=None, pnl=None,
                true_delta=round(put_delta(S0, Ks, T0, sig0), 3),
            ))
            continue
        j0 = i + 1
        j1 = min(i + DTE_ENTRY + 1, len(idx))
        res = simulate_one(
            credit=credit, short_strike=Ks, long_strike=Kl,
            spot_path=[float(closes.iloc[j]) for j in range(j0, j1)],
            vol_path=[float(vix.asof(idx[j])) / 100.0 for j in range(j0, j1)],
        )
        if res is None:
            continue
        rows.append(dict(
            symbol=symbol, entry=str(idx[i].date()),
            cred_pct=round(100 * credit / width, 1), cleared_floor=True,
            true_delta=round(put_delta(S0, Ks, T0, sig0), 3),
            outcome=res.outcome, held=res.held_days, pnl=round(res.pnl, 3),
        ))
    return pd.DataFrame(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Credit-spread win rate (11.57 step 4)")
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--delta-sweep", action="store_true",
                    help="also report credit vs the floor across target deltas")
    args = ap.parse_args(argv)

    start = datetime.fromisoformat(args.start).replace(tzinfo=timezone.utc)
    end = datetime.fromisoformat(args.end).replace(tzinfo=timezone.utc)
    vix = _load_vix(start, end)

    print(f"\ncredit-spread win-rate sim — feed={settings.BACKTEST_DATA_FEED}, "
          f"{args.start}→{args.end}, vol=^VIX path, entries every {ENTRY_EVERY}d")
    print("UPPER BOUND: no bid/ask, no skew, no assignment. Entries overlap.\n")

    frames = []
    for sym, cfg in settings.CREDIT_SPREAD_INSTRUMENTS.items():
        closes = _load(sym, start, end)
        frames.append(run_symbol(
            sym, target_delta=cfg["short_leg_delta"],
            width=float(cfg["spread_width"]),
            min_credit_pct=cfg["min_credit_pct_of_width"],
            closes=closes, vix=vix,
        ))
    # Drop empties before concat: an instrument where nothing cleared the
    # floor yields an all-NA frame, and pandas warns about inferring dtypes
    # from those. Dropping keeps the dtypes honest.
    frames = [f for f in frames if not f.empty]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        print("No entries generated at all — check the date range and feed.\n")
        return 0
    pd.set_option("display.width", 200)

    print("=== Credit floor ===\n")
    for sym, g in df.groupby("symbol"):
        cfg = settings.CREDIT_SPREAD_INSTRUMENTS[sym]
        print(f"  {sym} @ true Δ{cfg['short_leg_delta']:.2f} "
              f"({cfg['spread_width']:.0f}-wide): mean credit "
              f"{g['cred_pct'].mean():.1f}% of width, "
              f"{100 * g['cleared_floor'].mean():.1f}% clear the "
              f"{100 * cfg['min_credit_pct_of_width']:.0f}% floor")

    traded = df[df["cleared_floor"]].dropna(subset=["outcome"])
    if traded.empty:
        print("\nNo entry cleared the credit floor — nothing to score.\n")
        return 0

    print("\n=== Outcome mix (entries that cleared the floor) ===\n")
    mix = traded.groupby(["symbol", "outcome"]).size().unstack(fill_value=0)
    mix["total"] = mix.sum(axis=1)
    print(mix.to_string())

    print("\n=== Win rate vs breakeven ===\n")
    for sym, g in traded.groupby("symbol"):
        wins = g["pnl"] > 0
        avg_w = g.loc[wins, "pnl"].mean()
        avg_l = g.loc[~wins, "pnl"].mean()
        be = breakeven_win_rate(avg_w, avg_l)
        print(f"  {sym}: n={len(g)}  win={100 * wins.mean():.1f}%  "
              f"breakeven={100 * be:.1f}%  "
              f"avg_win=+{avg_w:.2f}  avg_loss={avg_l:.2f}  "
              f"expectancy={g['pnl'].mean():+.3f}/sh")

    print("\n  docs §8 projected 72–80% (P(expire OTM) for a 17Δ put).")

    if args.delta_sweep:
        print("\n=== Credit vs the 13% floor, by true delta ===\n")
        for sym, cfg in settings.CREDIT_SPREAD_INSTRUMENTS.items():
            closes = _load(sym, start, end)
            width = float(cfg["spread_width"])
            for d in (0.12, 0.17, 0.22, 0.30):
                vals = []
                for i in range(0, len(closes) - 45, ENTRY_EVERY):
                    S0 = float(closes.iloc[i])
                    sig = float(vix.asof(closes.index[i])) / 100.0
                    if not np.isfinite(sig) or sig <= 0:
                        continue
                    Ks = strike_at_delta(S0, DTE_ENTRY / 365.0, sig, d)
                    if Ks is None:
                        continue
                    vals.append(
                        100 * spread_mid(S0, Ks, Ks - width,
                                         DTE_ENTRY / 365.0, sig) / width
                    )
                s = pd.Series(vals)
                print(f"  {sym} Δ{d:.2f}: mean {s.mean():5.1f}% of width, "
                      f"{100 * (s >= 13).mean():5.1f}% clear 13%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
