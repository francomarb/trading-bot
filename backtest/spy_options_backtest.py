"""
SPY Options Reversion — production-mirrored backtest + VIX-percentile gate audit.

This backtest is deliberately wired to mirror the LIVE decision path so its
numbers answer a production question (see the "backtests must mirror production"
project standard). Prior to 2026-07 this file gated on SPY>200SMA with no regime
gate — that overstated the edge ~2.5× and did not reflect what the bot trades.

What it mirrors
---------------
- RSI(14) Wilder's RMA, entry on cross up through threshold 45 (forward_test.py).
- Edge gate 1: SPY close > 100-day SMA (SPYOptionsEdgeFilter).
- Regime gate: entries only in {TRENDING, RANGING}. BEAR (SPY<200SMA) and
  VOLATILE (ATR% ≥ 80th pct of trailing 126 AND ATR% ≥ 1.2%) block — mirrors
  regime/detector.py.
- Edge gate 2 (the enhancement under test): in TRENDING regime, require today's
  VIX at or above SPY_OPTIONS_MIN_VIX_PERCENTILE of its trailing-252 range
  (≤-percentile, mirroring IVProxyResolver.resolve_rank). RANGING is exempt.
- Exit stack: trailing (act 10% / trail 15%), hard SL 25%, delta floor 0.30,
  Wednesday-of-expiry-week time stop, 300% take-profit cap. Contract: BS call,
  strike = close × 0.995, first Friday inside [14, 28] DTE.

Caveats (read before trusting absolute %s)
-----------------------------------------
Daily Black-Scholes pricing with NO bid/ask spread, NO commission, and NO
intraday premium noise. The daily model cannot see the intraday 25% swings that
trip the hard stop live, so it UNDER-states stop churn. Treat absolute totals as
an optimistic ceiling; the RELATIVE comparisons (baseline vs gate, IV buckets,
regime cross-tab) are the trustworthy signal.

Data: yfinance SPY + ^VIX daily, auto-adjusted. VIX is required and only
yfinance provides it here.

Run:  venv/bin/python backtest/spy_options_backtest.py
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import norm

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings

# ── Production-mirrored parameters ────────────────────────────────────────────
RSI_THRESHOLD = 45
MIN_DTE, MAX_DTE = 14, 28
ITM_OFFSET = 0.005          # target_strike_pct 0.995
R = 0.05
DELTA_FLOOR = 0.30
TP_MULT = 3.00
TRAIL_ACT, TRAIL_PCT = 0.10, 0.15
MIN_VIX_PCTILE = settings.SPY_OPTIONS_MIN_VIX_PERCENTILE  # single source of truth
START, END = "2018-01-01", "2025-12-31"


# ── Data ──────────────────────────────────────────────────────────────────────

def _download() -> pd.DataFrame:
    import yfinance as yf
    spy = yf.download("SPY", start=START, end=END, auto_adjust=True, progress=False)
    vix = yf.download("^VIX", start=START, end=END, auto_adjust=True, progress=False)
    for d in (spy, vix):
        if isinstance(d.columns, pd.MultiIndex):
            d.columns = d.columns.get_level_values(0)
    spy.index = pd.to_datetime(spy.index).tz_localize(None)
    vix.index = pd.to_datetime(vix.index).tz_localize(None)
    df = spy[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.join(vix["Close"].rename("vix"), how="left")
    df["vix"] = df["vix"].ffill().fillna(20.0)
    return df


# ── Indicators (mirror indicators/technicals.py + regime/detector.py) ─────────

def _wilder(s: pd.Series, length: int) -> pd.Series:
    return s.ewm(alpha=1.0 / length, adjust=False).mean()


def _wilder_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    rs = _wilder(gain, length) / _wilder(loss, length).replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0)


def _adx_atr(df: pd.DataFrame, length: int = 14) -> tuple[pd.Series, pd.Series]:
    high, low, close = df["high"], df["low"], df["close"]
    up, down = high.diff(), -low.diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = pd.concat([high - low, (high - close.shift()).abs(),
                    (low - close.shift()).abs()], axis=1).max(axis=1)
    atr = _wilder(tr, length)
    plus_di = 100 * _wilder(plus_dm, length) / atr
    minus_di = 100 * _wilder(minus_dm, length) / atr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return _wilder(dx.fillna(0.0), length), atr


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["rsi"] = _wilder_rsi(df["close"])
    df["sma100"] = df["close"].rolling(100).mean()
    df["sma200"] = df["close"].rolling(200).mean()
    df["sma50"] = df["close"].rolling(50).mean()
    df["sma50_slope"] = df["sma50"] - df["sma50"].shift(5)
    adx, atr = _adx_atr(df)
    df["adx"] = adx
    df["atr_pct"] = atr / df["close"]
    df["atr_pctile"] = df["atr_pct"].rolling(126, min_periods=10).apply(
        lambda w: (w[:-1] < w[-1]).mean(), raw=True)
    # Trailing-252 VIX ≤-percentile, mirroring IVProxyResolver.resolve_rank.
    df["vix_pctile"] = df["vix"].rolling(252, min_periods=240).apply(
        lambda w: (w <= w[-1]).mean(), raw=True)
    return df


def _regime(row) -> str:
    if pd.isna(row["sma200"]):
        return "RANGING"
    if row["close"] < row["sma200"]:
        return "BEAR"
    if (not pd.isna(row["atr_pctile"]) and row["atr_pctile"] >= 0.80
            and row["atr_pct"] >= 0.012):
        return "VOLATILE"
    adx = row["adx"]
    if pd.isna(adx):
        return "RANGING"
    if adx >= 25:
        return "TRENDING"
    if adx <= 20:
        return "RANGING"
    return "TRENDING" if row["sma50_slope"] > 0 else "RANGING"


# ── Option pricing ────────────────────────────────────────────────────────────

def _bs_call(S, K, T, r, sigma) -> float:
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def _bs_delta(S, K, T, r, sigma) -> float:
    if T <= 0 or sigma <= 0:
        return 1.0 if S > K else 0.0
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    return float(norm.cdf(d1))


def _next_friday(from_date: date, min_dte: int, max_dte: int) -> date | None:
    d, end = from_date + timedelta(days=min_dte), from_date + timedelta(days=max_dte)
    while d <= end:
        if d.weekday() == 4:
            return d
        d += timedelta(days=1)
    return None


# ── Backtest core ─────────────────────────────────────────────────────────────

def run(df: pd.DataFrame, *, sl_pct: float = 0.25, regime_gate: bool = True,
        vix_gate: bool = False, min_vix_pctile: float = MIN_VIX_PCTILE,
        sma_window: int = 100) -> list[dict]:
    """Run one production-mirrored backtest. Records full entry context per trade."""
    df = df.copy()
    df["regime"] = df.apply(_regime, axis=1)
    smacol = f"sma{sma_window}"
    idx = df.index
    trades: list[dict] = []
    open_pos: list[dict] = []

    for i in range(201, len(df)):
        row = df.iloc[i]
        today = idx[i].date()
        S, sigma = float(row["close"]), float(row["vix"]) / 100.0

        still = []
        for p in open_pos:
            T = max((p["expiry"] - today).days / 365.0, 0.001)
            val = _bs_call(S, p["strike"], T, R, sigma)
            delta = _bs_delta(S, p["strike"], T, R, sigma)
            pnl = (val - p["entry_premium"]) / p["entry_premium"]
            reason = None
            if today >= p["expiry"] - timedelta(days=2):
                reason = "time_stop"
            elif delta < DELTA_FLOOR:
                reason = "delta_floor"
            elif pnl <= -sl_pct:
                reason = "sl"
            else:
                p["hwm"] = max(p.get("hwm", val), val)
                if p["hwm"] >= p["entry_premium"] * (1 + TRAIL_ACT) and \
                        val < p["hwm"] * (1 - TRAIL_PCT):
                    reason = "trail"
                elif pnl >= TP_MULT - 1:
                    reason = "tp"
            if reason:
                p.update(exit_date=today, exit_premium=val, pnl_pct=pnl, exit_reason=reason)
                trades.append(p)
            else:
                still.append(p)
        open_pos = still

        if open_pos:
            continue
        prev_rsi = float(df.iloc[i - 1]["rsi"])
        if not ((prev_rsi < RSI_THRESHOLD) and (float(row["rsi"]) >= RSI_THRESHOLD)):
            continue
        sma_val = row[smacol]
        if pd.isna(sma_val) or S <= float(sma_val):
            continue
        if regime_gate and row["regime"] not in ("TRENDING", "RANGING"):
            continue
        # Gate 2: VIX percentile — enforced only in TRENDING (mirrors filter).
        if vix_gate and row["regime"] == "TRENDING":
            if pd.isna(row["vix_pctile"]) or row["vix_pctile"] < min_vix_pctile:
                continue
        exp = _next_friday(today, MIN_DTE, MAX_DTE)
        if exp is None:
            continue
        K = S * (1 - ITM_OFFSET)
        T = max((exp - today).days / 365.0, 0.001)
        prem = _bs_call(S, K, T, R, sigma)
        if prem <= 0:
            continue
        open_pos.append(dict(
            entry_date=today, entry_price=S, entry_premium=prem, strike=K, expiry=exp,
            entry_rsi=float(row["rsi"]), entry_vix=float(row["vix"]),
            entry_vix_pctile=float(row["vix_pctile"]) if not pd.isna(row["vix_pctile"]) else np.nan,
            entry_adx=float(row["adx"]) if not pd.isna(row["adx"]) else np.nan,
            entry_regime=row["regime"],
            sma_dist=(S / float(sma_val) - 1),
        ))
    return trades


def metrics(trades: list[dict]) -> dict:
    if not trades:
        return dict(n=0, win=np.nan, avg=np.nan, total=np.nan, pf=np.nan, hold=np.nan)
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    gp, gl = sum(wins), abs(sum(p for p in pnls if p <= 0))
    hold = [(t["exit_date"] - t["entry_date"]).days for t in trades]
    return dict(n=len(trades), win=len(wins) / len(pnls), avg=np.mean(pnls),
                total=np.sum(pnls), pf=(gp / gl if gl else np.inf), hold=np.mean(hold))


def _exits(trades: list[dict]) -> dict:
    from collections import Counter
    return dict(Counter(t["exit_reason"] for t in trades))


def _line(label: str, t: list[dict]) -> str:
    m = metrics(t)
    return (f"{label:28s} | n={m['n']:3d} win={m['win']*100:4.0f}% "
            f"avg={m['avg']*100:+6.1f}% total={m['total']*100:+7.0f}% pf={m['pf']:.2f}")


def main() -> None:
    print(f"Downloading SPY + VIX (yfinance, {START[:4]}–{END[:4]})…")
    df = _prep(_download())
    print(f"{len(df)} bars  {df.index[0].date()} → {df.index[-1].date()}\n")

    base = run(df, regime_gate=True, vix_gate=False)
    gated = run(df, regime_gate=True, vix_gate=True)

    print("=" * 92)
    print(f"PRODUCTION-MIRRORED — VIX-percentile gate (TRENDING requires "
          f"VIX pctile ≥ {MIN_VIX_PCTILE:.2f})")
    print("=" * 92)
    print(_line("baseline (regime only)", base) + f"  exits={_exits(base)}")
    print(_line("+ VIX-pctile gate", gated) + f"  exits={_exits(gated)}")

    print("\n" + "=" * 92)
    print("REGIME × IVR CROSS-TAB (baseline)  —  total P&L% | n")
    print("=" * 92)
    print(f"  {'':10s} {'VIXpct<gate':>16s} {'VIXpct>=gate':>16s}")
    for reg in ("TRENDING", "RANGING"):
        cells = []
        for lo, hi in [(0.0, MIN_VIX_PCTILE), (MIN_VIX_PCTILE, 1.01)]:
            sub = [t for t in base if t["entry_regime"] == reg
                   and not np.isnan(t["entry_vix_pctile"]) and lo <= t["entry_vix_pctile"] < hi]
            m = metrics(sub)
            cells.append(f"{m['total']*100:+6.0f}%|n={m['n']}" if m["n"] else "—")
        print(f"  {reg:10s} {cells[0]:>16s} {cells[1]:>16s}")

    print("\n" + "=" * 92)
    print("STOP-LOSS SWEEP (gated config)")
    print("=" * 92)
    for sl in [0.20, 0.25, 0.30, 0.35, 0.40, 0.50]:
        print(_line(f"SL={sl*100:.0f}%", run(df, regime_gate=True, vix_gate=True, sl_pct=sl)))

    print("\n" + "=" * 92)
    print("WINNER vs LOSER ANATOMY (baseline)")
    print("=" * 92)

    def anat(g):
        if not g:
            return "n=0"
        f = lambda k: np.nanmean([t[k] for t in g])
        hold = np.mean([(t["exit_date"] - t["entry_date"]).days for t in g])
        return (f"n={len(g):3d}  VIX={f('entry_vix'):5.1f}  VIXpct={f('entry_vix_pctile'):.2f}  "
                f"RSI={f('entry_rsi'):4.1f}  ADX={f('entry_adx'):4.1f}  hold={hold:.1f}d")
    print(f"  WINNERS  {anat([t for t in base if t['pnl_pct'] > 0])}")
    print(f"  LOSERS   {anat([t for t in base if t['pnl_pct'] <= 0])}")
    print()
    print("Caveat: daily BS pricing, no spread/commission/intraday noise — absolute")
    print("totals are an optimistic ceiling; trust the relative comparisons above.")


if __name__ == "__main__":
    sys.exit(main())
