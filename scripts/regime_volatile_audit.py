"""
Regime VOLATILE-gate audit (PLAN 11.6 prerequisite).

Replays the bot's RegimeDetector VOLATILE logic on ~7 years of SPY daily bars
and asks one question: does the gate actually fire when a human would say
"yeah, that was volatile"?

The current gate (regime/detector.py):
    atr_pct  = ATR14(SPY) / close
    window   = last 126 trading days of atr_pct
    pct_rank = (window < current_atr_pct).mean()
    VOLATILE := pct_rank >= 0.80

This script computes that signal bar-by-bar, then evaluates it against:
  1. A timeline of known stress events (2018 Q4, COVID, 2022 bear, SVB,
     2024 Aug carry unwind, 2025 tariff selloff if present).
  2. Forward SPY returns conditional on VOLATILE firing.
  3. Whether the gate sustains through crises or "renormalises away" as the
     spike bars enter the rolling window.

Run:
    /Users/franco/trading-bot/venv/bin/python scripts/regime_volatile_audit.py

Output: a series of tables printed to stdout. No files written, no plotting
required.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pandas as pd

# Make project importable when run from repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load Alpaca credentials. Worktree builds may not have their own config/.env,
# so fall back to the main repo's .env if the worktree-local one is missing.
from dotenv import load_dotenv
for env_path in [ROOT / "config" / ".env", Path("/Users/franco/trading-bot/config/.env")]:
    if env_path.exists():
        load_dotenv(env_path)
        break

# Silence loguru so the tables stay clean.
from loguru import logger
logger.remove()

from data.fetcher import fetch_symbol
from indicators.technicals import add_atr, add_sma


def fetch_spy_deep_history(start: datetime, end: datetime) -> pd.DataFrame:
    """
    Fetch SPY daily bars via yfinance for the audit only. Alpaca's IEX feed
    on this account only has ~5.8 years of history (since ~Aug 2020), which
    misses Feb 2018, Q4 2018, and the COVID crash — the most informative
    stress events for floor calibration. yfinance gives free decades of OHLC
    and is already a project dependency (used by the sector resolver).

    Returns a frame with lowercase columns and a tz-aware UTC index matching
    the shape that indicators/ and the regime detector expect.
    """
    import yfinance as yf
    df = yf.download(
        "SPY",
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        interval="1d",
        progress=False,
        auto_adjust=False,
    )
    if df.empty:
        raise RuntimeError("yfinance returned no SPY bars")
    # Flatten MultiIndex columns if present (yfinance returns ("Open","SPY") etc).
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = [c[0] for c in df.columns]
    df = df.rename(columns=str.lower)
    df = df[["open", "high", "low", "close", "volume"]]
    if df.index.tz is None:
        df.index = df.index.tz_localize("UTC")
    return df


# ── Audit parameters (mirror regime/detector.py defaults exactly) ──────────────
ATR_WINDOW          = 14
VOL_PCT_WINDOW      = 126
VOL_PCT_THRESHOLD   = 0.80
SMA_LONG_WINDOW     = 200       # for BEAR overlay
LOOKBACK_YEARS      = 12        # ~Jan 2014 → today; captures Q4 2018, COVID, 2022 bear

# Candidate absolute ATR% floors to sweep. The current gate is equivalent to
# FLOORS = [0.0]. Any floor > 0 requires BOTH the percentile rank AND atr_pct
# >= floor for the gate to fire.
FLOOR_CANDIDATES    = [0.000, 0.012, 0.015, 0.016, 0.018, 0.020]

# Known stress windows. Edit as desired; script is robust to dates that don't
# fully overlap the fetched range (it just reports "no data" for those).
CRISIS_WINDOWS = [
    ("Aug 2015 China devaluation","2015-08-17", "2015-09-30"),
    ("Feb 2018 volpocalypse",    "2018-01-26", "2018-03-09"),
    ("Q4 2018 selloff",          "2018-10-01", "2019-01-31"),
    ("COVID crash",              "2020-02-15", "2020-05-15"),
    ("2020 summer chop",         "2020-09-01", "2020-11-15"),
    ("2022 bear market",         "2022-01-03", "2022-10-31"),
    ("SVB / regional banks",     "2023-03-08", "2023-04-15"),
    ("2024 Aug carry unwind",    "2024-07-25", "2024-08-15"),
    ("2025 tariff selloff",      "2025-03-01", "2025-05-15"),
]

# "Calm" windows — we want the gate to be MOSTLY OFF here. A good floor should
# kill VOLATILE days in these without harming the catches in CRISIS_WINDOWS.
CALM_WINDOWS = [
    ("2017 grind-up",            "2017-01-01", "2017-12-31"),
    ("2021 H2 grind-up",         "2021-05-01", "2021-09-30"),
    ("2024 H1 melt-up",          "2024-01-01", "2024-06-30"),
]


@dataclass
class AuditFrame:
    """SPY daily bars annotated with the VOLATILE signal."""
    df: pd.DataFrame   # index: date. columns: close, atr_14, atr_pct, pct_rank,
                       #                       is_volatile, sma_200, is_bear

    def slice(self, start: str, end: str) -> pd.DataFrame:
        return self.df.loc[start:end]


def build_audit_frame(spy: pd.DataFrame) -> AuditFrame:
    """Replay the bot's VOLATILE logic bar-by-bar on the SPY frame."""
    df = spy.copy()
    df = add_atr(df, ATR_WINDOW)
    df = add_sma(df, SMA_LONG_WINDOW)

    atr_col  = f"atr_{ATR_WINDOW}"
    sma_col  = f"sma_{SMA_LONG_WINDOW}"
    df["atr_pct"] = df[atr_col] / df["close"]

    # Bar-by-bar rolling percentile rank — strict less-than, exactly as the
    # detector computes it.
    atr_pct = df["atr_pct"]
    pct_rank = pd.Series(index=df.index, dtype=float)
    is_volatile = pd.Series(False, index=df.index, dtype=bool)
    for i in range(len(df)):
        cur = atr_pct.iloc[i]
        if pd.isna(cur):
            continue
        lo = max(0, i - VOL_PCT_WINDOW + 1)
        window = atr_pct.iloc[lo:i + 1].dropna()
        if len(window) < 10:
            continue
        r = float((window < cur).mean())
        pct_rank.iloc[i] = r
        is_volatile.iloc[i] = (r >= VOL_PCT_THRESHOLD)

    df["pct_rank"]    = pct_rank
    df["is_volatile"] = is_volatile
    df["is_bear"]     = df["close"] < df[sma_col]
    # Forward 5-day SPY return for conditional analysis.
    df["fwd_5d_ret"]  = df["close"].pct_change(5).shift(-5)
    return AuditFrame(df=df)


def _streaks(series: pd.Series) -> list[tuple[pd.Timestamp, pd.Timestamp, int]]:
    """Return [(start, end, length), ...] of consecutive True runs."""
    out = []
    in_run = False
    start = None
    for ts, val in series.items():
        if val and not in_run:
            in_run = True
            start = ts
            length = 1
        elif val and in_run:
            length += 1
        elif not val and in_run:
            out.append((start, prev_ts, length))
            in_run = False
        prev_ts = ts
    if in_run:
        out.append((start, prev_ts, length))
    return out


def print_overall_summary(af: AuditFrame) -> None:
    df = af.df.dropna(subset=["pct_rank"])
    total = len(df)
    vol = int(df["is_volatile"].sum())
    bear = int(df["is_bear"].sum())
    vol_and_bear = int((df["is_volatile"] & df["is_bear"]).sum())

    print("=" * 78)
    print("OVERALL")
    print("=" * 78)
    print(f"  Bars analysed:                  {total}")
    print(f"  Date range:                     {df.index[0].date()} → {df.index[-1].date()}")
    print(f"  VOLATILE days:                  {vol} ({vol / total:.1%})")
    print(f"  BEAR days (SPY < SMA200):       {bear} ({bear / total:.1%})")
    print(f"  VOLATILE & BEAR days:           {vol_and_bear} "
          f"({vol_and_bear / max(vol, 1):.1%} of VOLATILE)")
    print()

    print("  ATR% distribution on VOLATILE days vs all days:")
    vd = df.loc[df["is_volatile"], "atr_pct"]
    ad = df["atr_pct"].dropna()
    print(f"    all days   — p50={ad.quantile(0.5):.4f}  "
          f"p90={ad.quantile(0.9):.4f}  max={ad.max():.4f}")
    if len(vd) > 0:
        print(f"    VOLATILE   — p50={vd.quantile(0.5):.4f}  "
              f"p90={vd.quantile(0.9):.4f}  max={vd.max():.4f}")
        print(f"    VOLATILE   — min ATR% that triggered = {vd.min():.4f} "
              f"({vd.min() * 100:.2f}% — absolute floor check)")
    print()


def print_yearly_breakdown(af: AuditFrame) -> None:
    df = af.df.dropna(subset=["pct_rank"]).copy()
    df["year"] = df.index.year
    g = df.groupby("year")
    print("=" * 78)
    print("VOLATILE DAYS BY YEAR")
    print("=" * 78)
    print(f"  {'year':<6}{'days':>6}{'VOL':>6}{'VOL%':>8}{'mean ATR% on VOL':>22}")
    for year, sub in g:
        nvol = int(sub["is_volatile"].sum())
        mean_atr_pct_vol = (
            sub.loc[sub["is_volatile"], "atr_pct"].mean()
            if nvol > 0 else float("nan")
        )
        print(f"  {year:<6}{len(sub):>6}{nvol:>6}{nvol / len(sub):>8.1%}"
              f"{mean_atr_pct_vol:>22.4f}")
    print()


def print_crisis_windows(af: AuditFrame) -> None:
    print("=" * 78)
    print("KNOWN STRESS WINDOWS — gate behaviour")
    print("=" * 78)
    print(f"  {'window':<28}{'days':>6}{'VOL':>6}{'VOL%':>8}"
          f"{'maxDD':>9}{'first→last VOL':>22}")
    for name, start, end in CRISIS_WINDOWS:
        sub = af.slice(start, end)
        if sub.empty:
            print(f"  {name:<28}  (no data)")
            continue
        n = len(sub)
        nvol = int(sub["is_volatile"].sum())
        peak = sub["close"].cummax()
        dd = (sub["close"] / peak - 1.0).min()
        vol_dates = sub.index[sub["is_volatile"]]
        if len(vol_dates) > 0:
            span = f"{vol_dates[0].date()}→{vol_dates[-1].date()}"
        else:
            span = "—"
        print(f"  {name:<28}{n:>6}{nvol:>6}{nvol / n:>8.1%}"
              f"{dd:>9.1%}  {span}")
    print()


def print_crisis_detail(af: AuditFrame) -> None:
    """For each crisis: peak→trough → first VOLATILE bar, and whether the gate
    shut off before the trough (the renormalisation-during-crisis concern)."""
    print("=" * 78)
    print("CRISIS DETAIL — did the gate stay on through the drawdown?")
    print("=" * 78)
    for name, start, end in CRISIS_WINDOWS:
        sub = af.slice(start, end)
        if sub.empty or sub["is_volatile"].sum() == 0:
            continue
        # Find the max-drawdown trough: bar with greatest close/cummax-1 deficit.
        cummax = sub["close"].cummax()
        dd_series = sub["close"] / cummax - 1.0
        trough_idx = dd_series.idxmin()
        # Peak is the cummax bar that produced that trough.
        peak_close = cummax.loc[trough_idx]
        prefix = sub.loc[:trough_idx]
        peak_idx = prefix.index[prefix["close"] == peak_close][0]
        peak_to_trough = sub.loc[peak_idx:trough_idx]
        if len(peak_to_trough) < 2:
            continue
        n_p2t = len(peak_to_trough)
        vol_p2t = int(peak_to_trough["is_volatile"].sum())
        first_vol_after_peak = peak_to_trough.index[peak_to_trough["is_volatile"]]
        first_vol_str = (
            first_vol_after_peak[0].date().isoformat()
            if len(first_vol_after_peak) > 0 else "(never)"
        )
        # Days from peak to first VOLATILE.
        lag = (
            (first_vol_after_peak[0] - peak_idx).days
            if len(first_vol_after_peak) > 0 else None
        )
        # Did the gate shut off before the trough? Find the LAST VOLATILE day
        # within peak→trough — if it's strictly before trough, the gate
        # renormalised mid-drawdown.
        if len(first_vol_after_peak) > 0:
            last_vol_p2t = first_vol_after_peak[-1]
            renorm_gap_days = (trough_idx - last_vol_p2t).days
        else:
            last_vol_p2t = None
            renorm_gap_days = None

        print(f"  {name}")
        print(f"    peak  {peak_idx.date()}  close={sub.loc[peak_idx, 'close']:.2f}")
        print(f"    trough {trough_idx.date()}  close={sub.loc[trough_idx, 'close']:.2f}  "
              f"(dd={sub.loc[trough_idx, 'close'] / sub.loc[peak_idx, 'close'] - 1:+.1%})")
        print(f"    peak→trough span:        {n_p2t} bars, "
              f"VOLATILE on {vol_p2t} ({vol_p2t / n_p2t:.0%})")
        print(f"    first VOLATILE post-peak: {first_vol_str}"
              + (f"  (+{lag}d after peak)" if lag is not None else ""))
        if last_vol_p2t is not None and renorm_gap_days is not None:
            verdict = (
                "GATE STAYED ON to trough" if renorm_gap_days <= 1
                else f"GATE SHUT OFF {renorm_gap_days}d before trough — renormalisation"
            )
            print(f"    last VOLATILE pre-trough: {last_vol_p2t.date()}  → {verdict}")
        print()


def print_forward_returns(af: AuditFrame) -> None:
    df = af.df.dropna(subset=["pct_rank", "fwd_5d_ret"])
    if df.empty:
        return
    vol = df.loc[df["is_volatile"], "fwd_5d_ret"]
    nonvol = df.loc[~df["is_volatile"], "fwd_5d_ret"]
    print("=" * 78)
    print("FORWARD 5-DAY SPY RETURN  conditional on gate state")
    print("=" * 78)
    print(f"  {'state':<14}{'N':>7}{'mean':>10}{'median':>10}"
          f"{'p10':>10}{'p90':>10}{'P(<-1%)':>10}")
    for label, s in [("VOLATILE", vol), ("non-VOLATILE", nonvol)]:
        if len(s) == 0:
            continue
        p_loss = float((s < -0.01).mean())
        print(f"  {label:<14}{len(s):>7}{s.mean():>10.3%}{s.median():>10.3%}"
              f"{s.quantile(0.1):>10.3%}{s.quantile(0.9):>10.3%}"
              f"{p_loss:>10.1%}")
    print()
    print("  If VOLATILE is doing its job, P(fwd 5d return < -1%) should be")
    print("  meaningfully higher on VOLATILE days than on non-VOLATILE days.")
    print()


def print_streaks(af: AuditFrame) -> None:
    df = af.df.dropna(subset=["pct_rank"])
    streaks = _streaks(df["is_volatile"])
    streaks.sort(key=lambda x: x[2], reverse=True)
    print("=" * 78)
    print("TOP 10 LONGEST VOLATILE STREAKS")
    print("=" * 78)
    print(f"  {'#':>3}  {'start':<12}{'end':<12}{'length (bars)':>15}")
    for i, (start, end, length) in enumerate(streaks[:10], 1):
        print(f"  {i:>3}  {start.date()!s:<12}{end.date()!s:<12}{length:>15}")
    print()


def print_floor_sweep(af: AuditFrame) -> None:
    """For each candidate ATR% floor, recompute is_volatile = (rank >= 0.80 AND
    atr_pct >= floor) and report the trade-off across crisis vs calm windows."""
    df = af.df.dropna(subset=["pct_rank"]).copy()

    print("=" * 78)
    print("ABSOLUTE-FLOOR SENSITIVITY SWEEP")
    print("=" * 78)
    print(f"  Gate definition:  pct_rank >= {VOL_PCT_THRESHOLD:.0%} "
          "AND atr_pct >= floor")
    print(f"  floor=0.0 reproduces the bot's current gate exactly.")
    print()

    # ── Headline trade-off ────────────────────────────────────────────────────
    print(f"  {'floor':>7}  {'VOL%':>7}{'crisis_catch%':>16}{'calm_falseFire%':>18}"
          f"{'P(fwd5d<-1%)':>15}{'mean_fwd5d':>13}")
    for floor in FLOOR_CANDIDATES:
        is_vol = df["is_volatile"] & (df["atr_pct"] >= floor)
        # Crisis-window catch rate: fraction of crisis days flagged VOLATILE.
        crisis_days = []
        for _name, s, e in CRISIS_WINDOWS:
            sub = df.loc[s:e]
            if not sub.empty:
                crisis_days.append(sub.index)
        crisis_idx = (
            pd.Index([]).union_many([pd.Index(x) for x in crisis_days])
            if hasattr(pd.Index, "union_many")
            else _union_indexes(crisis_days)
        )
        crisis_catch = (
            float(is_vol.loc[is_vol.index.intersection(crisis_idx)].mean())
            if len(crisis_idx) > 0 else float("nan")
        )
        # Calm-window false-fire rate.
        calm_idx = _union_indexes([df.loc[s:e].index for _n, s, e in CALM_WINDOWS])
        calm_false = (
            float(is_vol.loc[is_vol.index.intersection(calm_idx)].mean())
            if len(calm_idx) > 0 else float("nan")
        )
        vol_share = float(is_vol.mean())
        # Forward-return discriminator.
        fwd = df.loc[is_vol, "fwd_5d_ret"].dropna()
        mean_fwd = float(fwd.mean()) if len(fwd) else float("nan")
        p_loss = float((fwd < -0.01).mean()) if len(fwd) else float("nan")
        print(f"  {floor:>7.3f}  {vol_share:>7.1%}{crisis_catch:>16.1%}"
              f"{calm_false:>18.1%}{p_loss:>15.1%}{mean_fwd:>13.3%}")
    print()
    print("  Read this table: a good floor MAXIMISES crisis_catch%, MINIMISES")
    print("  calm_falseFire%, and raises P(fwd5d<-1%) meaningfully above the")
    print("  floor=0.0 baseline. mean_fwd5d is informational — closer to zero")
    print("  or negative on VOLATILE days = the gate is identifying actual risk,")
    print("  not just high-variance days that mean-revert positive.")
    print()

    # ── Per-crisis breakdown for the recommended floor ────────────────────────
    print("=" * 78)
    print("PER-CRISIS CATCH RATE @ each floor")
    print("=" * 78)
    header = "  " + f"{'window':<28}" + "".join(
        f"{f'{floor*100:.1f}%':>10}" for floor in FLOOR_CANDIDATES
    )
    print(header)
    for name, s, e in CRISIS_WINDOWS:
        sub = df.loc[s:e]
        if sub.empty:
            print(f"  {name:<28}" + "  (no data)")
            continue
        row = f"  {name:<28}"
        for floor in FLOOR_CANDIDATES:
            is_vol = sub["is_volatile"] & (sub["atr_pct"] >= floor)
            row += f"{float(is_vol.mean()):>10.0%}"
        print(row)
    print()

    print("=" * 78)
    print("PER-CALM-WINDOW FALSE-FIRE RATE @ each floor")
    print("=" * 78)
    print(header)
    for name, s, e in CALM_WINDOWS:
        sub = df.loc[s:e]
        if sub.empty:
            print(f"  {name:<28}" + "  (no data)")
            continue
        row = f"  {name:<28}"
        for floor in FLOOR_CANDIDATES:
            is_vol = sub["is_volatile"] & (sub["atr_pct"] >= floor)
            row += f"{float(is_vol.mean()):>10.0%}"
        print(row)
    print()


def print_oos_split(af: AuditFrame, split_date: str = "2023-01-01") -> None:
    """
    Out-of-sample validation: pick the best floor from the IN-sample years
    (everything before split_date), then report how it performs on the
    OUT-OF-sample years (split_date onwards) — without re-tuning.

    The "best" floor is defined as the candidate that maximises
        crisis_catch% - calm_falseFire%
    on the in-sample slice. That trade-off is the actual job of the gate.
    """
    df = af.df.dropna(subset=["pct_rank"]).copy()
    in_df  = df.loc[:split_date].iloc[:-1]   # exclusive of split_date
    out_df = df.loc[split_date:]

    print("=" * 78)
    print(f"OUT-OF-SAMPLE VALIDATION  (split @ {split_date})")
    print("=" * 78)
    print(f"  in-sample  : {in_df.index[0].date()} → {in_df.index[-1].date()}  "
          f"({len(in_df)} bars)")
    print(f"  out-of-sample: {out_df.index[0].date()} → {out_df.index[-1].date()}  "
          f"({len(out_df)} bars)")
    print()

    def _metrics(slice_df: pd.DataFrame, floor: float) -> dict:
        is_vol = slice_df["is_volatile"] & (slice_df["atr_pct"] >= floor)
        crisis_idx = _union_indexes(
            [slice_df.loc[s:e].index for _n, s, e in CRISIS_WINDOWS]
        ).intersection(slice_df.index)
        calm_idx = _union_indexes(
            [slice_df.loc[s:e].index for _n, s, e in CALM_WINDOWS]
        ).intersection(slice_df.index)
        crisis_catch = (
            float(is_vol.loc[crisis_idx].mean()) if len(crisis_idx) > 0 else float("nan")
        )
        calm_false = (
            float(is_vol.loc[calm_idx].mean()) if len(calm_idx) > 0 else float("nan")
        )
        fwd = slice_df.loc[is_vol, "fwd_5d_ret"].dropna()
        p_loss = float((fwd < -0.01).mean()) if len(fwd) else float("nan")
        return {
            "vol_share":    float(is_vol.mean()),
            "crisis_catch": crisis_catch,
            "calm_false":   calm_false,
            "p_loss":       p_loss,
            "score":        crisis_catch - calm_false,
        }

    # ── In-sample tuning ──────────────────────────────────────────────────────
    in_results = {floor: _metrics(in_df, floor) for floor in FLOOR_CANDIDATES}
    print("  IN-SAMPLE (tune here):")
    print(f"    {'floor':>7}  {'VOL%':>7}{'crisis':>10}{'calm':>10}"
          f"{'P(loss)':>10}{'score':>10}")
    best_floor = None
    best_score = -float("inf")
    for floor, m in in_results.items():
        marker = ""
        if not pd.isna(m["score"]) and m["score"] > best_score:
            best_score = m["score"]
            best_floor = floor
        print(f"    {floor:>7.3f}  {m['vol_share']:>7.1%}{m['crisis_catch']:>10.1%}"
              f"{m['calm_false']:>10.1%}{m['p_loss']:>10.1%}{m['score']:>10.1%}")
    # Mark winner.
    print(f"    → in-sample best floor = {best_floor:.3f} "
          f"(crisis_catch − calm_false = {best_score:.1%})")
    print()

    # ── Out-of-sample evaluation ──────────────────────────────────────────────
    print("  OUT-OF-SAMPLE (frozen floor, no re-tuning):")
    print(f"    {'floor':>7}  {'VOL%':>7}{'crisis':>10}{'calm':>10}"
          f"{'P(loss)':>10}{'score':>10}")
    for floor in FLOOR_CANDIDATES:
        m = _metrics(out_df, floor)
        marker = "  ← in-sample pick" if floor == best_floor else ""
        print(f"    {floor:>7.3f}  {m['vol_share']:>7.1%}{m['crisis_catch']:>10.1%}"
              f"{m['calm_false']:>10.1%}{m['p_loss']:>10.1%}{m['score']:>10.1%}"
              f"{marker}")
    print()
    print("  If the in-sample winner is also at or near the top out-of-sample,")
    print("  the floor generalises and is safe to ship. If a very different")
    print("  floor wins out-of-sample, the gate is overfit and the change")
    print("  should be reconsidered.")
    print()


def _union_indexes(idxs: list) -> pd.Index:
    """Union multiple DatetimeIndexes safely across pandas versions."""
    out = pd.DatetimeIndex([])
    for i in idxs:
        out = out.union(pd.DatetimeIndex(i))
    return out


def main() -> int:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=LOOKBACK_YEARS * 365 + 60)  # +buffer for SMA200 warmup
    print(f"Fetching SPY daily {start.date()} → {end.date()} via yfinance ...")
    spy = fetch_spy_deep_history(start, end)
    print(f"  got {len(spy)} bars  (yfinance, range "
          f"{spy.index[0].date()} → {spy.index[-1].date()})")
    print()

    af = build_audit_frame(spy)

    print_overall_summary(af)
    print_yearly_breakdown(af)
    print_crisis_windows(af)
    print_crisis_detail(af)
    print_streaks(af)
    print_forward_returns(af)
    print_floor_sweep(af)
    print_oos_split(af, split_date="2023-01-01")

    print("=" * 78)
    print("Interpretation guide")
    print("=" * 78)
    print("  • If VOLATILE% by year is HIGHER in calm years (e.g. 2017, 2024)")
    print("    than in stressed years (2020, 2022), the gate is renormalising")
    print("    and over-firing in calm regimes — diagnosed.")
    print("  • If 'GATE SHUT OFF Nd before trough' appears repeatedly in the")
    print("    crisis detail block, the gate is failing to sustain through")
    print("    sustained stress — also renormalisation.")
    print("  • If P(fwd 5d return < -1%) on VOLATILE days is NOT meaningfully")
    print("    higher than on non-VOLATILE days, the gate isn't predictive of")
    print("    downside and is paying for noise.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
