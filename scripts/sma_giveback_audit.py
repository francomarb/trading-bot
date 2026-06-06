"""
SMA Crossover giveback audit — measures profit surrendered between high-water
mark and the 20/50 death-cross exit, and compares vs. a chandelier trail.

For each symbol in SMA_WATCHLIST:
  1. Pull daily bars over the maximum available Alpaca range.
  2. Walk every 20/50 SMA crossover round trip on close-to-close logic:
       entry  = bar where SMA20 crosses above SMA50  (exec next open)
       exit_A = bar where SMA20 crosses below SMA50  (exec next open)
       exit_B = static ATR stop (entry - 2 * ATR14)  if hit first
  3. For each WINNING trade exited on the death cross, compute:
       giveback_$   = HWM_close - exit_price
       giveback_pct = (HWM - exit) / (HWM - entry)   # fraction of peak profit
       giveback_atr = (HWM - exit) / ATR_at_entry    # ATR units
  4. Simulate a chandelier overlay (HWM_close - K * ATR14) for K in {2.5, 3.0, 3.5}
     and report captured profit vs. the death-cross baseline.

Output is print-only (tabular). No DB writes, no log spam.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import statistics
import sys

import numpy as np
import pandas as pd

from config import settings
from data.fetcher import fetch_symbol
from indicators.technicals import add_sma, add_atr


FAST = 20
SLOW = 50
ATR_LEN = 14
ATR_STOP_MULT = 2.0
TRAIL_KS = (2.5, 3.0, 3.5)

# Profit-gated trail grid: activation threshold (in ATR units of unrealized
# profit) × trail distance (in ATR units below HWM).
# - activation = 0   → trail is live from entry (equivalent to plain chandelier)
# - activation = N   → trail arms only after (close - entry) >= N * ATR
PROFIT_TARGETS_PCT = (0.10, 0.20, 0.30, 0.50, 0.75, 1.00, 1.50)

GATED_GRID = [
    # (activation_atr, trail_atr)
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


@dataclass
class Trade:
    symbol: str
    entry_date: pd.Timestamp
    entry_price: float
    exit_date: pd.Timestamp
    exit_price: float
    exit_reason: str          # "death_cross" | "atr_stop"
    hwm_close: float
    hwm_date: pd.Timestamp
    atr_at_entry: float
    # Chandelier-overlay results (only populated when exit_reason='death_cross')
    chandelier_exits: dict = None  # {k: (exit_date, exit_price)}
    # Profit-gated trail overlays — keyed by (activation_atr, trail_atr).
    # Value is (exit_date, exit_price, armed_bool).
    gated_exits: dict = None
    # Fixed-% take-profit overlays — keyed by target pct (0.50 = +50%).
    # Value is (exit_date, exit_price, hit_bool).
    pt_exits: dict = None

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


def simulate_symbol(symbol: str, df: pd.DataFrame) -> list[Trade]:
    """Walk through every 20/50 crossover round trip on the bar series."""
    df = add_sma(df, FAST)
    df = add_sma(df, SLOW)
    df = add_atr(df, ATR_LEN)
    df = df.dropna(subset=[f"sma_{FAST}", f"sma_{SLOW}", f"atr_{ATR_LEN}"]).copy()
    if df.empty:
        return []

    fast = df[f"sma_{FAST}"].values
    slow = df[f"sma_{SLOW}"].values
    atr = df[f"atr_{ATR_LEN}"].values
    opens = df["open"].values
    closes = df["close"].values
    lows = df["low"].values
    highs = df["high"].values
    dates = df.index

    trades: list[Trade] = []
    in_pos = False
    entry_idx = None
    entry_price = None
    entry_atr = None
    stop_level = None
    hwm_close = None
    hwm_idx = None

    n = len(df)
    for i in range(1, n):
        diff_now = fast[i] - slow[i]
        diff_prev = fast[i - 1] - slow[i - 1]

        if not in_pos:
            # Golden cross at bar i → enter at bar i+1's open.
            if diff_now > 0 and diff_prev <= 0 and i + 1 < n:
                entry_idx = i + 1
                entry_price = opens[i + 1]
                entry_atr = atr[i]
                stop_level = entry_price - ATR_STOP_MULT * entry_atr
                hwm_close = closes[entry_idx]
                hwm_idx = entry_idx
                in_pos = True
            continue

        # In a position — check stop first (intrabar low touches stop),
        # then check death cross at this bar's close → exit next open.
        if lows[i] <= stop_level:
            trades.append(Trade(
                symbol=symbol,
                entry_date=dates[entry_idx],
                entry_price=entry_price,
                exit_date=dates[i],
                exit_price=stop_level,
                exit_reason="atr_stop",
                hwm_close=hwm_close,
                hwm_date=dates[hwm_idx],
                atr_at_entry=entry_atr,
            ))
            in_pos = False
            continue

        # Update HWM on this bar's close.
        if closes[i] > hwm_close:
            hwm_close = closes[i]
            hwm_idx = i

        # Death cross at bar i → exit at bar i+1's open.
        if diff_now < 0 and diff_prev >= 0 and i + 1 < n:
            exit_price = opens[i + 1]
            # Chandelier overlay on the same trade: walk forward from
            # entry, compute trailing stop each bar, record when it
            # would have fired (or fall back to the death-cross exit).
            chandelier_exits = {}
            for k in TRAIL_KS:
                hwm_k = closes[entry_idx]
                trail_exit_price = None
                trail_exit_date = None
                for j in range(entry_idx, i + 1):
                    if closes[j] > hwm_k:
                        hwm_k = closes[j]
                    trail_stop = hwm_k - k * entry_atr
                    # Check intrabar low against trail stop, but only
                    # starting the bar AFTER entry (no same-bar stop hit).
                    if j > entry_idx and lows[j] <= trail_stop:
                        trail_exit_price = trail_stop
                        trail_exit_date = dates[j]
                        break
                if trail_exit_price is None:
                    # Trail never fired — same exit as death cross.
                    chandelier_exits[k] = (dates[i + 1], exit_price)
                else:
                    chandelier_exits[k] = (trail_exit_date, trail_exit_price)

            # Profit-gated trail overlays.
            gated_exits = {}
            for activation_k, trail_k in GATED_GRID:
                hwm_g = closes[entry_idx]
                armed = False
                g_exit_price = None
                g_exit_date = None
                for j in range(entry_idx, i + 1):
                    if closes[j] > hwm_g:
                        hwm_g = closes[j]
                    # Arm the trail once unrealized close-profit clears the
                    # activation threshold. Once armed, stays armed.
                    if not armed and (closes[j] - entry_price) >= activation_k * entry_atr:
                        armed = True
                    if armed and j > entry_idx:
                        trail_stop = hwm_g - trail_k * entry_atr
                        if lows[j] <= trail_stop:
                            g_exit_price = trail_stop
                            g_exit_date = dates[j]
                            break
                if g_exit_price is None:
                    gated_exits[(activation_k, trail_k)] = (dates[i + 1], exit_price, armed)
                else:
                    gated_exits[(activation_k, trail_k)] = (g_exit_date, g_exit_price, armed)

            # Fixed-% take-profit overlays.
            pt_exits = {}
            for tgt in PROFIT_TARGETS_PCT:
                target_price = entry_price * (1.0 + tgt)
                pt_exit_price = None
                pt_exit_date = None
                # Walk bars from the day AFTER entry through the death-cross
                # exit bar. If the intrabar high touches target, fill at target.
                for j in range(entry_idx + 1, i + 2):
                    if j >= n:
                        break
                    if highs[j] >= target_price:
                        pt_exit_price = target_price
                        pt_exit_date = dates[j]
                        break
                if pt_exit_price is None:
                    pt_exits[tgt] = (dates[i + 1], exit_price, False)
                else:
                    pt_exits[tgt] = (pt_exit_date, pt_exit_price, True)

            trades.append(Trade(
                symbol=symbol,
                entry_date=dates[entry_idx],
                entry_price=entry_price,
                exit_date=dates[i + 1],
                exit_price=exit_price,
                exit_reason="death_cross",
                hwm_close=hwm_close,
                hwm_date=dates[hwm_idx],
                atr_at_entry=entry_atr,
                chandelier_exits=chandelier_exits,
                gated_exits=gated_exits,
                pt_exits=pt_exits,
            ))
            in_pos = False

    return trades


def fmt_pct(x: float) -> str:
    if x != x:  # NaN
        return "  n/a"
    return f"{x*100:5.1f}%"


def main() -> None:
    start = datetime(2018, 11, 1, tzinfo=timezone.utc)
    end = datetime(2026, 6, 5, tzinfo=timezone.utc)

    symbols = list(settings.SMA_WATCHLIST)
    print(f"Auditing {len(symbols)} symbols from {start.date()} to {end.date()}", file=sys.stderr)

    all_trades: list[Trade] = []
    for sym in symbols:
        try:
            df, _ = fetch_symbol(sym, start, end, timeframe="1Day", use_cache=True)
        except Exception as exc:
            print(f"  {sym}: fetch failed — {exc}", file=sys.stderr)
            continue
        if len(df) < SLOW + 5:
            print(f"  {sym}: only {len(df)} bars — skipped", file=sys.stderr)
            continue
        trades = simulate_symbol(sym, df)
        all_trades.extend(trades)
        n_dc = sum(1 for t in trades if t.exit_reason == "death_cross")
        n_st = sum(1 for t in trades if t.exit_reason == "atr_stop")
        print(f"  {sym:<6} bars={len(df)} trades={len(trades)} (death_cross={n_dc}, atr_stop={n_st})",
              file=sys.stderr)

    # ── Aggregate stats ─────────────────────────────────────────────────
    print()
    print("=" * 76)
    print("SMA CROSSOVER — GIVEBACK AUDIT")
    print("=" * 76)
    print(f"Window: {start.date()} → {end.date()}")
    print(f"Watchlist: {len(symbols)} symbols")
    print(f"Total round trips: {len(all_trades)}")

    dc = [t for t in all_trades if t.exit_reason == "death_cross"]
    st = [t for t in all_trades if t.exit_reason == "atr_stop"]
    print(f"  Death-cross exits: {len(dc)}")
    print(f"  ATR-stop exits:    {len(st)}")

    dc_winners = [t for t in dc if t.pnl > 0]
    dc_losers = [t for t in dc if t.pnl <= 0]
    print(f"  Death-cross winners: {len(dc_winners)}")
    print(f"  Death-cross losers (cross fired before stop): {len(dc_losers)}")

    if not dc_winners:
        print("\nNo winning death-cross trades found — nothing to analyze.")
        return

    print()
    print("─" * 76)
    print("GIVEBACK ON WINNING DEATH-CROSS TRADES")
    print("─" * 76)

    gb_pct = sorted([t.giveback_pct for t in dc_winners if t.giveback_pct == t.giveback_pct])
    gb_atr = sorted([t.giveback_atr for t in dc_winners if t.giveback_atr == t.giveback_atr])
    gb_dol = sorted([t.giveback_dollars for t in dc_winners])

    def pct(arr, p):
        if not arr: return float("nan")
        return float(np.percentile(arr, p))

    print(f"  Giveback as % of peak open profit:")
    print(f"    median  = {fmt_pct(pct(gb_pct, 50))}")
    print(f"    mean    = {fmt_pct(statistics.mean(gb_pct))}")
    print(f"    P25     = {fmt_pct(pct(gb_pct, 25))}")
    print(f"    P75     = {fmt_pct(pct(gb_pct, 75))}")
    print(f"    P90     = {fmt_pct(pct(gb_pct, 90))}")
    print(f"    max     = {fmt_pct(max(gb_pct))}")

    print(f"  Giveback in ATR-units (at entry):")
    print(f"    median  = {pct(gb_atr, 50):5.2f} ATR")
    print(f"    mean    = {statistics.mean(gb_atr):5.2f} ATR")
    print(f"    P75     = {pct(gb_atr, 75):5.2f} ATR")
    print(f"    P90     = {pct(gb_atr, 90):5.2f} ATR")

    # ── Chandelier comparison ──────────────────────────────────────────
    print()
    print("─" * 76)
    print("CHANDELIER OVERLAY — DEATH-CROSS WINNERS ONLY")
    print("(captured profit per trade vs. waiting for the death cross)")
    print("─" * 76)
    baseline_pnl = sum(t.pnl for t in dc_winners)
    baseline_peak = sum(t.peak_open_profit for t in dc_winners)
    print(f"  Baseline (death-cross exit):")
    print(f"    sum captured  = ${baseline_pnl:>10,.0f}")
    print(f"    sum peak open = ${baseline_peak:>10,.0f}")
    print(f"    capture ratio = {baseline_pnl/baseline_peak*100:5.1f}% of peak")

    for k in TRAIL_KS:
        captured = 0.0
        early_exits = 0
        same_as_dc = 0
        for t in dc_winners:
            ex_date, ex_price = t.chandelier_exits[k]
            captured += (ex_price - t.entry_price)
            if ex_date < t.exit_date:
                early_exits += 1
            else:
                same_as_dc += 1
        print(f"  Chandelier K={k}:")
        print(f"    sum captured  = ${captured:>10,.0f}  "
              f"(Δ vs death-cross: ${captured-baseline_pnl:+,.0f}, "
              f"{(captured-baseline_pnl)/baseline_pnl*100:+5.1f}%)")
        print(f"    capture ratio = {captured/baseline_peak*100:5.1f}% of peak")
        print(f"    trail bit early on {early_exits}/{len(dc_winners)} winners "
              f"({early_exits/len(dc_winners)*100:.0f}%)")

    # ── Profit-gated trail comparison ──────────────────────────────────
    print()
    print("─" * 76)
    print("PROFIT-GATED TRAIL — DEATH-CROSS WINNERS ONLY")
    print("(arm trail only after unrealized profit >= activation_atr * ATR)")
    print("─" * 76)
    print(f"  Baseline (death-cross): captured ${baseline_pnl:,.0f}, "
          f"capture ratio {baseline_pnl/baseline_peak*100:.1f}%")
    print()
    print(f"  {'activation':>10} {'trail K':>8} {'captured':>10} "
          f"{'Δ vs base':>10} {'capture%':>9} {'armed%':>7} "
          f"{'bit early':>10} {'med gb%':>8}")
    for activation_k, trail_k in GATED_GRID:
        captured = 0.0
        armed_count = 0
        early_count = 0
        givebacks_pct = []
        for t in dc_winners:
            ex_date, ex_price, armed = t.gated_exits[(activation_k, trail_k)]
            captured += (ex_price - t.entry_price)
            if armed:
                armed_count += 1
            if ex_date < t.exit_date:
                early_count += 1
            # Giveback under this rule: HWM_close - exit_price
            gb = t.hwm_close - ex_price
            peak = t.hwm_close - t.entry_price
            if peak > 0:
                givebacks_pct.append(gb / peak)
        med_gb = statistics.median(givebacks_pct) if givebacks_pct else float("nan")
        print(f"  {activation_k:>10.1f} {trail_k:>8.1f} "
              f"${captured:>9,.0f} "
              f"{captured-baseline_pnl:>+9,.0f} "
              f"{captured/baseline_peak*100:>8.1f}% "
              f"{armed_count/len(dc_winners)*100:>6.0f}% "
              f"{early_count:>4}/{len(dc_winners):<4} "
              f"{med_gb*100:>7.1f}%")

    # ── Fixed-% take-profit comparison ─────────────────────────────────
    print()
    print("─" * 76)
    print("FIXED PROFIT-TARGET — APPLIED TO ALL DEATH-CROSS TRADES")
    print("(if intrabar high touches entry*(1+target), exit at target instead)")
    print("─" * 76)
    # For ALL death-cross trades (winners + losers): under the take-profit rule,
    # a trade exits at target if it ever traded there; otherwise it exits where
    # the death cross would have. Losers were already losers — the TP doesn't
    # fire on them. So sum captured = TP winners + non-TP-fired trades at their
    # death-cross exit.
    dc_all = dc  # all 210 death-cross trades (incl. losers)
    dc_baseline_pnl = sum(t.pnl for t in dc_all)
    print(f"  Baseline (all {len(dc_all)} death-cross trades, no TP):")
    print(f"    sum pnl       = ${dc_baseline_pnl:>10,.0f}")
    print(f"    winners       = {len(dc_winners)}/{len(dc_all)}")
    print()
    print(f"  {'target':>8} {'hit count':>10} {'hit rate':>9} "
          f"{'captured':>10} {'Δ vs base':>11} {'win rate':>9} "
          f"{'avg trade':>10}")
    for tgt in PROFIT_TARGETS_PCT:
        captured = 0.0
        hits = 0
        wins = 0
        for t in dc_all:
            ex_date, ex_price, hit = t.pt_exits[tgt]
            trade_pnl = ex_price - t.entry_price
            captured += trade_pnl
            if hit:
                hits += 1
            if trade_pnl > 0:
                wins += 1
        avg = captured / len(dc_all)
        delta = captured - dc_baseline_pnl
        print(f"  {tgt*100:>6.0f}%  {hits:>6}/{len(dc_all):<4} "
              f"{hits/len(dc_all)*100:>7.1f}% "
              f"${captured:>9,.0f} "
              f"${delta:>+10,.0f} "
              f"{wins/len(dc_all)*100:>7.1f}% "
              f"${avg:>9.2f}")

    # Show per-target lift on winners alone (since losers are unaffected,
    # this isolates the take-profit's contribution).
    print()
    print(f"  Among the {len(dc_winners)} winners only:")
    print(f"  {'target':>8} {'hit on winners':>16} {'capped $':>10} "
          f"{'rode out $':>11} {'net captured':>13}")
    for tgt in PROFIT_TARGETS_PCT:
        capped = 0.0
        rode = 0.0
        n_hit = 0
        for t in dc_winners:
            ex_date, ex_price, hit = t.pt_exits[tgt]
            if hit:
                capped += (ex_price - t.entry_price)
                n_hit += 1
            else:
                rode += t.pnl
        net = capped + rode
        delta = net - baseline_pnl
        print(f"  {tgt*100:>6.0f}%  {n_hit:>6}/{len(dc_winners):<4} "
              f"({n_hit/len(dc_winners)*100:>4.0f}%) "
              f"${capped:>9,.0f} "
              f"${rode:>10,.0f} "
              f"${net:>10,.0f} "
              f"(Δ ${delta:+,.0f})")

    # ── Per-symbol summary (top contributors) ──────────────────────────
    print()
    print("─" * 76)
    print("TOP 10 WORST GIVEBACKS (single trades, by $)")
    print("─" * 76)
    print(f"  {'symbol':<8} {'entry':>10} {'exit':>10} {'hwm':>10} "
          f"{'gb $':>10} {'gb %peak':>10} {'gb ATR':>9}")
    for t in sorted(dc_winners, key=lambda x: -x.giveback_dollars)[:10]:
        print(f"  {t.symbol:<8} {str(t.entry_date.date()):>10} "
              f"{str(t.exit_date.date()):>10} {str(t.hwm_date.date()):>10} "
              f"{t.giveback_dollars:>9.2f} {fmt_pct(t.giveback_pct):>10} "
              f"{t.giveback_atr:>8.2f}x")


if __name__ == "__main__":
    main()
