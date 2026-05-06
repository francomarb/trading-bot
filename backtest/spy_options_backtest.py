"""
SPY Options Reversion — Parameter Grid Backtest

Methodology
-----------
- Daily SPY bars from yfinance, 2019-01-01 to 2025-12-31.
- RSI(14) Wilder's RMA, same implementation as the live strategy.
- Entry signal: RSI crosses above threshold (prev < threshold, curr >= threshold)
  AND SPY close > 200-day SMA (edge filter).
- One position at a time — new signals skipped while a position is open.
- Contract: Black-Scholes call, strike = close × (1 - itm_offset), first Friday
  expiry inside [min_dte, max_dte] calendar days.
- Option priced at entry using VIX close as implied vol; tracked daily thereafter.
- Exit: TP hit, SL hit, or Wednesday time stop (close of Wednesday in expiry week).
- r = 0.05 (risk-free rate), multiplier = 100 (standard contract).

Output
------
Ranked table of parameter combinations by total P&L.
"""

import sys
from datetime import date, timedelta
from itertools import product

import numpy as np
import pandas as pd

# ── Data download ────────────────────────────────────────────────────────────

def _download() -> tuple[pd.DataFrame, pd.Series]:
    import yfinance as yf
    spy = yf.download("SPY", start="2019-01-01", end="2025-12-31",
                      auto_adjust=True, progress=False)
    vix = yf.download("^VIX", start="2019-01-01", end="2025-12-31",
                      auto_adjust=True, progress=False)

    spy.columns = spy.columns.get_level_values(0) if isinstance(spy.columns, pd.MultiIndex) else spy.columns
    vix.columns = vix.columns.get_level_values(0) if isinstance(vix.columns, pd.MultiIndex) else vix.columns

    spy.index = pd.to_datetime(spy.index).tz_localize(None)
    vix.index = pd.to_datetime(vix.index).tz_localize(None)

    vix_close = vix["Close"].rename("vix")
    df = spy[["Open", "High", "Low", "Close", "Volume"]].copy()
    df.columns = ["open", "high", "low", "close", "volume"]
    df = df.join(vix_close, how="left")
    df["vix"] = df["vix"].ffill().fillna(20.0)  # fallback VIX=20 if missing
    return df, df["close"]


# ── RSI (Wilder's RMA) ───────────────────────────────────────────────────────

def _wilder_rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)

    # First value: simple mean of first `length` bars
    avg_gain = gain.ewm(alpha=1.0 / length, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / length, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(100.0)
    return rsi


# ── Option pricing ───────────────────────────────────────────────────────────

def _bs_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
    """Black-Scholes call price. Returns 0 on degenerate inputs."""
    from scipy.stats import norm
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0)
    d1 = (np.log(S / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    return float(S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2))


def _next_friday(from_date: date, min_dte: int, max_dte: int) -> date | None:
    """Return first Friday expiry between min_dte and max_dte calendar days out."""
    min_d = from_date + timedelta(days=min_dte)
    max_d = from_date + timedelta(days=max_dte)
    d = min_d
    while d <= max_d:
        if d.weekday() == 4:  # Friday
            return d
        d += timedelta(days=1)
    return None


# ── Single backtest run ──────────────────────────────────────────────────────

def _run(
    df: pd.DataFrame,
    *,
    rsi_threshold: float,
    min_dte: int,
    max_dte: int,
    tp_pct: float,
    sl_pct: float,
    sma_filter: bool = True,
    max_positions: int = 1,
    r: float = 0.05,
    itm_offset: float = 0.005,
    # Trailing stop — if both are set, tp_pct is ignored
    trail_activation_pct: float | None = None,
    trail_pct: float | None = None,
) -> list[dict]:
    close = df["close"]
    vix = df["vix"]
    rsi = _wilder_rsi(close, length=14)
    sma200 = close.rolling(200).mean()

    trades: list[dict] = []
    # Each open position: dict with entry_price, entry_premium, expiry, entry_date, strike
    open_positions: list[dict] = []

    dates = df.index.tolist()
    closes = close.values
    vixxs = vix.values
    rsis = rsi.values
    smas = sma200.values

    for i in range(201, len(dates)):
        today = dates[i].date() if hasattr(dates[i], "date") else dates[i]
        S = float(closes[i])
        sigma = float(vixxs[i]) / 100.0
        curr_rsi = float(rsis[i])
        prev_rsi = float(rsis[i - 1])
        sma_val = float(smas[i])

        # ── Check exits for all open positions ──────────────────────────────
        use_trailing = trail_activation_pct is not None and trail_pct is not None
        still_open = []
        for pos in open_positions:
            T = max((pos["expiry"] - today).days / 365.0, 0.001)
            opt_val = _bs_call(S, pos["strike"], T, r, sigma)
            pnl_pct = (opt_val - pos["entry_premium"]) / pos["entry_premium"]

            expiry_wednesday = pos["expiry"] - timedelta(days=2)
            exit_reason = None
            if today >= expiry_wednesday:
                exit_reason = "time_stop"
            elif pnl_pct <= -sl_pct:
                exit_reason = "sl"
            elif use_trailing:
                # Update HWM and check trailing stop
                if "hwm" not in pos:
                    pos["hwm"] = opt_val
                else:
                    pos["hwm"] = max(pos["hwm"], opt_val)
                base = pos["entry_premium"]
                hwm = pos["hwm"]
                if hwm >= base * (1.0 + trail_activation_pct):
                    trail_floor = hwm * (1.0 - trail_pct)
                    if opt_val < trail_floor:
                        exit_reason = "trail"
            elif pnl_pct >= tp_pct:
                exit_reason = "tp"

            if exit_reason:
                trades.append({
                    "entry_date":    pos["entry_date"],
                    "exit_date":     today,
                    "entry_spy":     pos["entry_price"],
                    "strike":        pos["strike"],
                    "expiry":        pos["expiry"],
                    "entry_premium": pos["entry_premium"],
                    "exit_premium":  opt_val,
                    "pnl_pct":       pnl_pct,
                    "exit_reason":   exit_reason,
                })
            else:
                still_open.append(pos)
        open_positions = still_open

        # ── Check entry signal ───────────────────────────────────────────────
        if len(open_positions) < max_positions:
            entry_signal = (prev_rsi < rsi_threshold) and (curr_rsi >= rsi_threshold)
            if sma_filter and (pd.isna(sma_val) or S <= sma_val):
                entry_signal = False

            if entry_signal:
                exp = _next_friday(today, min_dte, max_dte)
                if exp is not None:
                    K = S * (1.0 - itm_offset)
                    T = max((exp - today).days / 365.0, 0.001)
                    premium = _bs_call(S, K, T, r, sigma)
                    if premium > 0:
                        open_positions.append({
                            "entry_price":   S,
                            "entry_premium": premium,
                            "expiry":        exp,
                            "entry_date":    today,
                            "strike":        K,
                        })

    # Force-close any remaining open positions at last bar
    i = len(dates) - 1
    today = dates[i].date() if hasattr(dates[i], "date") else dates[i]
    S = float(closes[i])
    sigma = float(vixxs[i]) / 100.0
    for pos in open_positions:
        T = max((pos["expiry"] - today).days / 365.0, 0.001)
        opt_val = _bs_call(S, pos["strike"], T, r, sigma)
        pnl_pct = (opt_val - pos["entry_premium"]) / pos["entry_premium"]
        trades.append({
            "entry_date":    pos["entry_date"],
            "exit_date":     today,
            "entry_spy":     pos["entry_price"],
            "strike":        pos["strike"],
            "expiry":        pos["expiry"],
            "entry_premium": pos["entry_premium"],
            "exit_premium":  opt_val,
            "pnl_pct":       pnl_pct,
            "exit_reason":   "end_of_data",
        })

    return trades


# ── Aggregate metrics ────────────────────────────────────────────────────────

def _metrics(trades: list[dict]) -> dict:
    if not trades:
        return {
            "n_trades": 0,
            "win_rate": float("nan"),
            "avg_pnl": float("nan"),
            "total_pnl": float("nan"),
            "profit_factor": float("nan"),
            "avg_hold_days": float("nan"),
        }
    pnls = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_profit = sum(wins) if wins else 0.0
    gross_loss = abs(sum(losses)) if losses else 0.0
    hold_days = [
        (t["exit_date"] - t["entry_date"]).days
        for t in trades
        if isinstance(t["entry_date"], date) and isinstance(t["exit_date"], date)
    ]
    return {
        "n_trades": len(trades),
        "win_rate": len(wins) / len(pnls),
        "avg_pnl": float(np.mean(pnls)),
        "total_pnl": float(np.sum(pnls)),
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else float("inf"),
        "avg_hold_days": float(np.mean(hold_days)) if hold_days else float("nan"),
    }


def _period_pnl(trades: list[dict], label: str, year_from: int, year_to: int) -> float:
    subset = [
        t for t in trades
        if isinstance(t["entry_date"], date)
        and year_from <= t["entry_date"].year <= year_to
    ]
    return float(np.sum([t["pnl_pct"] for t in subset])) if subset else 0.0


# ── Grid search ─────────────────────────────────────────────────────────────

def run_grid(df: pd.DataFrame) -> pd.DataFrame:
    rsi_thresholds = [40, 45, 50]
    tp_pcts        = [0.15, 0.20, 0.25, 0.30]
    sl_pcts        = [0.20, 0.25, 0.30]
    dte_windows    = [(10, 21), (14, 28)]
    max_pos_list   = [1, 2]

    rows = []
    combos = list(product(rsi_thresholds, tp_pcts, sl_pcts, dte_windows, max_pos_list))
    print(f"Running {len(combos)} parameter combinations...\n")

    for rsi_thr, tp, sl, (min_dte, max_dte), max_pos in combos:
        trades = _run(
            df,
            rsi_threshold=rsi_thr,
            min_dte=min_dte,
            max_dte=max_dte,
            tp_pct=tp,
            sl_pct=sl,
            sma_filter=True,
            max_positions=max_pos,
        )
        m = _metrics(trades)
        rows.append({
            "rsi_thr":     rsi_thr,
            "tp_%":        int(tp * 100),
            "sl_%":        int(sl * 100),
            "dte":         f"{min_dte}-{max_dte}",
            "max_pos":     max_pos,
            "trades":      m["n_trades"],
            "win_%":       round(m["win_rate"] * 100, 1) if not np.isnan(m["win_rate"]) else "—",
            "avg_pnl_%":   round(m["avg_pnl"] * 100, 1) if not np.isnan(m["avg_pnl"]) else "—",
            "total_pnl_%": round(m["total_pnl"] * 100, 1) if not np.isnan(m["total_pnl"]) else "—",
            "pf":          round(m["profit_factor"], 2) if not np.isinf(m["profit_factor"]) else "∞",
            "hold_d":      round(m["avg_hold_days"], 1) if not np.isnan(m["avg_hold_days"]) else "—",
            "covid_%":     round(_period_pnl(trades, "covid", 2020, 2020) * 100, 1),
            "2022_%":      round(_period_pnl(trades, "2022", 2022, 2022) * 100, 1),
            "2023+_%":     round(_period_pnl(trades, "2023+", 2023, 2025) * 100, 1),
        })

    result = pd.DataFrame(rows)
    if not result.empty:
        result = result.sort_values("total_pnl_%", ascending=False)
    return result


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Downloading SPY + VIX data (2019–2025)...")
    df, _ = _download()
    print(f"Downloaded {len(df)} daily bars ({df.index[0].date()} → {df.index[-1].date()})\n")

    result = run_grid(df)

    pd.set_option("display.max_rows", 300)
    pd.set_option("display.width", 140)

    # ── Top 20 by total P&L (≥8 trades for statistical relevance) ──
    valid = result[result["trades"] >= 8].copy()
    print("=" * 130)
    print("SPY OPTIONS REVERSION — TOP 20 by Total P&L  (≥8 trades)  sorted ↓")
    print("=" * 130)
    if valid.empty:
        print("No combinations with ≥8 trades. Showing top 20 overall:")
        top = result.head(20)
    else:
        top = valid.head(20)
    print(top.to_string(index=False))

    # ── Full table ──
    print()
    print("=" * 130)
    print("FULL GRID — all combinations, sorted by total P&L")
    print("=" * 130)
    print(result.to_string(index=False))

    print()
    print("Columns: rsi_thr=RSI threshold  tp_%=take-profit  sl_%=stop-loss")
    print("         dte=DTE window  200sma=Y/N whether SPY>200SMA filter was on")
    print("         total_pnl_%=cumulative sum of per-trade P&L %  pf=profit factor")
    print("         covid_%=2020 trades total  2022_%=2022 total  2023+_%=2023-2025 total")

    def _show_trades(label: str, trades: list[dict]) -> None:
        print(f"\n── {label} ──")
        rows = []
        for t in trades:
            rows.append({
                "entry":     str(t.get("entry_date", "?")),
                "exit":      str(t.get("exit_date", "?")),
                "spy@entry": round(float(t.get("entry_spy", 0)), 2),
                "strike":    round(float(t.get("strike", 0)), 2),
                "expiry":    str(t.get("expiry", "?")),
                "entry_$":   round(float(t.get("entry_premium", 0)), 2),
                "exit_$":    round(float(t.get("exit_premium", 0)), 2),
                "pnl_%":     round(float(t.get("pnl_pct", 0)) * 100, 1),
                "reason":    t.get("exit_reason", "?"),
            })
        if rows:
            print(pd.DataFrame(rows).to_string(index=False))

    # ── Per-trade detail: best combo with max_pos=1 and max_pos=2 ──
    for mp in [1, 2]:
        subset = valid[valid["max_pos"] == mp] if not valid.empty else result[result["max_pos"] == mp]
        if subset.empty:
            subset = result[result["max_pos"] == mp]
        if subset.empty:
            continue
        br = subset.iloc[0]
        rsi_b = int(br["rsi_thr"]); tp_b = float(br["tp_%"]) / 100
        sl_b  = float(br["sl_%"]) / 100
        dte_b = br["dte"].split("-"); min_b, max_b = int(dte_b[0]), int(dte_b[1])
        bt = _run(df, rsi_threshold=rsi_b, min_dte=min_b, max_dte=max_b,
                  tp_pct=tp_b, sl_pct=sl_b, sma_filter=True, max_positions=mp)
        _show_trades(
            f"Per-trade: rsi={rsi_b} tp={int(tp_b*100)}% sl={int(sl_b*100)}% "
            f"DTE {min_b}-{max_b} max_pos={mp}  "
            f"({len(bt)} trades  win={round(sum(1 for t in bt if t['pnl_pct']>0)/len(bt)*100,1) if bt else 0}%)",
            bt,
        )

    # ═══════════════════════════════════════════════════════════════════════════
    # TRAILING STOP COMPARISON
    # Baseline: RSI 45, TP 20%, SL 25%, DTE 14-28 (calibrated params)
    # Variants: same RSI/SL/DTE, trailing stop with different activation/trail combos
    # ═══════════════════════════════════════════════════════════════════════════
    print()
    print("=" * 110)
    print("TRAILING STOP COMPARISON  (RSI 45, SL 25%, DTE 14-28, max_pos=1, SMA-100 filter)")
    print("Baseline = fixed TP 20% | Trailing = HWM trail replaces TP, SL stays at 25%")
    print("=" * 110)

    _BASE_RSI, _BASE_SL, _BASE_MIN_DTE, _BASE_MAX_DTE = 45, 0.25, 14, 28

    baseline_trades = _run(
        df, rsi_threshold=_BASE_RSI, min_dte=_BASE_MIN_DTE, max_dte=_BASE_MAX_DTE,
        tp_pct=0.20, sl_pct=_BASE_SL, sma_filter=True, max_positions=1,
    )

    trail_variants = [
        ("trail act=10% trail=15% (live default)", 0.10, 0.15),
        ("trail act=10% trail=10%",                0.10, 0.10),
        ("trail act=10% trail=20%",                0.10, 0.20),
        ("trail act=15% trail=15%",                0.15, 0.15),
        ("trail act=15% trail=10%",                0.15, 0.10),
        ("trail act=20% trail=15%",                0.20, 0.15),
    ]

    comp_rows = []
    all_variant_trades: dict[str, list[dict]] = {}

    def _win_pct(t: list[dict]) -> float:
        return round(sum(1 for x in t if x["pnl_pct"] > 0) / len(t) * 100, 1) if t else 0.0

    bm = _metrics(baseline_trades)
    comp_rows.append({
        "variant":       "baseline TP=20%",
        "trades":        bm["n_trades"],
        "win_%":         round(bm["win_rate"] * 100, 1),
        "avg_pnl_%":     round(bm["avg_pnl"] * 100, 2),
        "total_pnl_%":   round(bm["total_pnl"] * 100, 1),
        "pf":            round(bm["profit_factor"], 2) if not np.isinf(bm["profit_factor"]) else "∞",
        "avg_hold_d":    round(bm["avg_hold_days"], 1),
        "covid_%":       round(_period_pnl(baseline_trades, "covid", 2020, 2020) * 100, 1),
        "2022_%":        round(_period_pnl(baseline_trades, "2022", 2022, 2022) * 100, 1),
        "2023+_%":       round(_period_pnl(baseline_trades, "2023+", 2023, 2025) * 100, 1),
        "sl_exits":      sum(1 for t in baseline_trades if t["exit_reason"] == "sl"),
        "tp_exits":      sum(1 for t in baseline_trades if t["exit_reason"] == "tp"),
        "time_exits":    sum(1 for t in baseline_trades if t["exit_reason"] == "time_stop"),
        "trail_exits":   0,
    })

    for label, act, trail in trail_variants:
        vt = _run(
            df, rsi_threshold=_BASE_RSI, min_dte=_BASE_MIN_DTE, max_dte=_BASE_MAX_DTE,
            tp_pct=0.20, sl_pct=_BASE_SL, sma_filter=True, max_positions=1,
            trail_activation_pct=act, trail_pct=trail,
        )
        all_variant_trades[label] = vt
        vm = _metrics(vt)
        comp_rows.append({
            "variant":       label,
            "trades":        vm["n_trades"],
            "win_%":         round(vm["win_rate"] * 100, 1),
            "avg_pnl_%":     round(vm["avg_pnl"] * 100, 2),
            "total_pnl_%":   round(vm["total_pnl"] * 100, 1),
            "pf":            round(vm["profit_factor"], 2) if not np.isinf(vm["profit_factor"]) else "∞",
            "avg_hold_d":    round(vm["avg_hold_days"], 1),
            "covid_%":       round(_period_pnl(vt, "covid", 2020, 2020) * 100, 1),
            "2022_%":        round(_period_pnl(vt, "2022", 2022, 2022) * 100, 1),
            "2023+_%":       round(_period_pnl(vt, "2023+", 2023, 2025) * 100, 1),
            "sl_exits":      sum(1 for t in vt if t["exit_reason"] == "sl"),
            "tp_exits":      sum(1 for t in vt if t["exit_reason"] == "tp"),
            "time_exits":    sum(1 for t in vt if t["exit_reason"] == "time_stop"),
            "trail_exits":   sum(1 for t in vt if t["exit_reason"] == "trail"),
        })

    comp_df = pd.DataFrame(comp_rows)
    print(comp_df.to_string(index=False))

    # ── Per-trade detail for baseline vs live-default trailing stop ──
    _show_trades(
        f"Baseline (fixed TP=20%, SL=25%)  [{len(baseline_trades)} trades  "
        f"win={_win_pct(baseline_trades)}%]",
        baseline_trades,
    )
    live_label = "trail act=10% trail=15% (live default)"
    live_trades = all_variant_trades[live_label]
    _show_trades(
        f"Live default (trailing act=10% trail=15%, SL=25%)  [{len(live_trades)} trades  "
        f"win={_win_pct(live_trades)}%]",
        live_trades,
    )
