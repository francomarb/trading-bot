"""
Donchian entry-gate A/B: does the production gate stack earn its cost?

Production restricts Donchian entries with two gates (see
``config.settings.STRATEGY_ALLOWED_REGIMES`` and
``strategies.filters.donchian_breakout.DonchianEdgeFilter``):

  1. **SPY regime gate** — TRENDING only. Stated premise, verbatim from
     ``settings.py``: *"Donchian whipsaws hard in RANGING regimes (every
     20-day high gets faded). Restrict to TRENDING only — academic
     literature is unanimous on this."*
  2. **DonchianEdgeFilter** — stock > 200 SMA, 20-day avg dollar volume
     >= $20M (rule 2, earnings blackout, has no offline equivalent).

This harness varies **only** ``entry_mask`` across four arms and holds every
other knob identical, so any difference is attributable to the gates alone.
``tests/test_donchian_gate_ab.py`` asserts that invariant rather than
asserting it in prose.

It also runs the decisive test of gate 1's premise: bucket **raw-signal**
trades by the SPY regime in force on their entry bar and compare expectancy.
If RANGING entries do not have materially worse expectancy than TRENDING
entries, the premise is false for this universe regardless of what the
aggregate returns say.

Method follows ``docs/donchian_trail_investigation.md``, which took five
review rounds to get right. The traps it documents, all applied here:

  - **SIP feed** (``BACKTEST_DATA_FEED``), not IEX — consolidated-tape volume
    is what makes the $20M liquidity floor interpretable.
  - **Filter mask computed on FULL cached history, then reindexed.** Computing
    it on a sliced window leaves SMA200 as NaN, and the filter *fails open* on
    NaN, silently admitting entries production would block (PR #49 R2 P1).
  - **Production regime defaults** (126 / 0.80 / 5) via the parity-tested
    ``classify_spy_regime`` (PR #49 R2 P1).
  - **``trade_start`` excludes warmup entries** from metrics (PR #49 R1 P1).
    The ungated baseline quoted in the trail investigation lacks this and is
    warmup-contaminated; do not compare against it.

Reproduce:

    PYTHONPATH=. venv/bin/python -m scripts.donchian_gate_ab

Precedence note: per ``11.56`` re-open condition (4), a *good* backtest result
is not a reason to ship a change; a *bad* one is a reason not to. And per
[[feedback_trade_log_outranks_the_model]], where this model disagrees with the
live trade log the trade log wins. ``--validate`` runs that comparison.
"""

from __future__ import annotations

import argparse
import collections
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.donchian_trail_sim import (  # noqa: E402
    PortfolioAggregate,
    StaticATRStop,
    TradeRecord,
    aggregate,
    simulate_symbol,
)
from config.settings import BACKTEST_DATA_FEED, TRADE_LOG_DB  # noqa: E402
from scripts.backtest_bollinger_squeeze import UNIVERSES  # noqa: E402
from scripts.donchian_trail_compare import (  # noqa: E402
    classify_spy_regime,
    load_bars,
    per_symbol_filter_mask,
)

# ── Held constant across every arm ──────────────────────────────────────────
# Only `entry_mask` may differ. tests/test_donchian_gate_ab.py asserts this.
SIM_CONSTANTS: dict[str, float | int] = {
    "entry_window": 30,
    "exit_window": 15,
    "atr_length": 14,
    "initial_cash": 100_000.0,
    "risk_per_trade_pct": 0.02,
    "slippage_bps": 5.0,
}
POLICY = StaticATRStop(k=2.0)  # current production stop

DEFAULT_TRADE_START = "2016-11-01"  # SMA200 populated for 2016-01-04 listers
DEFAULT_END = "2026-08-18"
DEFAULT_UNIVERSE = "ai_bigtech"

ARM_RAW = "A raw signal (no gates)"
ARM_REGIME = "B regime gate only"
ARM_FILTER = "C edge filter only"
ARM_BOTH = "D both (production)"


@dataclass(frozen=True)
class ArmResult:
    """One arm's portfolio aggregate plus its trade list for slicing."""

    name: str
    agg: PortfolioAggregate
    trades: list[TradeRecord]


def load_universe_bars(
    symbols: list[str], end: pd.Timestamp, *, min_bars: int = 260
) -> dict[str, pd.DataFrame]:
    """Load SIP bars per symbol, truncated at `end`, dropping thin histories."""
    bars: dict[str, pd.DataFrame] = {}
    for symbol in symbols:
        df = load_bars(symbol)
        if df is None or df.empty:
            logger.warning(f"{symbol}: no cached bars, skipped")
            continue
        df = df[df.index <= end]
        if len(df) < min_bars:
            logger.warning(f"{symbol}: only {len(df)} bars <= {end.date()}, skipped")
            continue
        bars[symbol] = df
    return bars


def build_masks(
    bars: dict[str, pd.DataFrame], regime: pd.Series
) -> tuple[dict[str, pd.Series], dict[str, pd.Series]]:
    """
    Build the two gate masks per symbol.

    The filter mask is computed on each symbol's FULL history before any
    slicing — computing it on a window leaves SMA200 NaN, and the filter fails
    open on NaN (mirroring production), silently admitting blocked entries.

    Returns (regime_masks, filter_masks), both aligned to each symbol's index.
    """
    regime_masks: dict[str, pd.Series] = {}
    filter_masks: dict[str, pd.Series] = {}
    for symbol, df in bars.items():
        aligned = regime.reindex(df.index).ffill().fillna("RANGING")
        regime_masks[symbol] = (aligned == "TRENDING").astype(bool)
        filter_masks[symbol] = per_symbol_filter_mask(df).reindex(df.index).fillna(False).astype(bool)
    return regime_masks, filter_masks


def run_arm(
    name: str,
    bars: dict[str, pd.DataFrame],
    mask_for: dict[str, pd.Series] | None,
    trade_start: pd.Timestamp,
) -> ArmResult:
    """Simulate every symbol under one arm. Only `entry_mask` varies by arm."""
    results = []
    for symbol, df in bars.items():
        mask = None if mask_for is None else mask_for[symbol]
        results.append(
            simulate_symbol(
                symbol, df, POLICY, trade_start=trade_start, entry_mask=mask, **SIM_CONSTANTS
            )
        )
    trades = [t for r in results for t in r.trades]
    return ArmResult(name=name, agg=aggregate(results), trades=trades)


def regime_at_entry(trades: list[TradeRecord], regime: pd.Series) -> dict[str, list[float]]:
    """Bucket trade R-multiples by the SPY regime in force on the entry bar."""
    buckets: dict[str, list[float]] = collections.defaultdict(list)
    lookup = regime.copy()
    lookup.index = lookup.index.normalize()
    for t in trades:
        key = t.entry_date.normalize()
        label = lookup.get(key)
        if label is None:
            prior = lookup[lookup.index <= key]
            label = prior.iloc[-1] if len(prior) else "UNKNOWN"
        buckets[str(label)].append(t.r_multiple)
    return buckets


def _fmt_stats(rs: list[float]) -> str:
    n = len(rs)
    if not n:
        return f"{0:>7}{'—':>9}{'—':>9}{'—':>9}"
    wins = sum(1 for r in rs if r > 0)
    return f"{n:>7}{100 * wins / n:>8.1f}%{sum(rs) / n:>9.2f}{sum(rs):>9.1f}"


def render_arms(arms: list[ArmResult]) -> str:
    lines = [
        "",
        "=" * 96,
        "ARM COMPARISON — only entry_mask differs; all other knobs identical",
        "=" * 96,
        f"{'arm':28}{'trades':>8}{'win%':>8}{'mean R':>9}{'mean ret%':>11}{'Sharpe':>9}{'maxDD%':>9}{'buy&hold%':>12}",
        "-" * 96,
    ]
    for a in arms:
        g = a.agg
        lines.append(
            f"{a.name:28}{g.total_trades:>8}{100 * g.win_rate:>8.1f}{g.avg_r:>9.2f}"
            f"{100 * g.mean_total_return:>11.1f}{g.mean_sharpe:>9.2f}"
            f"{100 * g.mean_max_drawdown:>9.1f}{100 * g.mean_buy_hold:>12.1f}"
        )
    lines += [
        "",
        "Exit-reason mix (%) — per feedback_backtests_report_exit_reasons",
        f"{'arm':28}{'gap':>9}{'intrabar':>10}{'signal':>9}{'eod':>8}",
        "-" * 96,
    ]
    for a in arms:
        g = a.agg
        lines.append(
            f"{a.name:28}{100 * g.pct_stop_gap:>9.1f}{100 * g.pct_stop_intrabar:>10.1f}"
            f"{100 * g.pct_signal_exit:>9.1f}{100 * g.pct_eod:>8.1f}"
        )
    return "\n".join(lines)


def render_premise_test(raw: ArmResult, regime: pd.Series) -> str:
    """
    The decisive test of gate 1. Production blocks every non-TRENDING entry on
    the premise that they get faded. Bucket the ungated trades by entry regime
    and read the expectancy directly.
    """
    buckets = regime_at_entry(raw.trades, regime)
    lines = [
        "",
        "=" * 96,
        "GATE 1 PREMISE TEST — raw-signal trades bucketed by SPY regime at entry",
        '  premise: "Donchian whipsaws hard in RANGING regimes (every 20-day high gets faded)"',
        "=" * 96,
        f"{'regime at entry':20}{'trades':>7}{'win%':>9}{'mean R':>9}{'sum R':>9}   gate blocks these?",
        "-" * 96,
    ]
    for label in ("TRENDING", "RANGING", "VOLATILE", "BEAR"):
        rs = buckets.get(label, [])
        blocked = "kept" if label == "TRENDING" else "BLOCKED"
        lines.append(f"{label:20}{_fmt_stats(rs)}   {blocked}")
    return "\n".join(lines)


def render_per_year(arms: list[ArmResult], regime: pd.Series) -> str:
    """Per-year sum-R for the raw and production arms — does the gate help in bad years?"""
    by_arm: dict[str, dict[int, list[float]]] = {}
    for a in arms:
        d: dict[int, list[float]] = collections.defaultdict(list)
        for t in a.trades:
            d[t.entry_date.year].append(t.r_multiple)
        by_arm[a.name] = d
    years = sorted({y for d in by_arm.values() for y in d})
    lines = [
        "",
        "=" * 96,
        "PER-YEAR sum R — the gate exists to protect bad years. Does it?",
        "=" * 96,
        f"{'year':>6}" + "".join(f"{a.name.split()[0] + ' n/sumR':>20}" for a in arms),
        "-" * 96,
    ]
    for y in years:
        row = f"{y:>6}"
        for a in arms:
            rs = by_arm[a.name].get(y, [])
            row += f"{len(rs):>8}/{sum(rs):>10.1f}" if rs else f"{'—':>19} "
        lines.append(row)
    return "\n".join(lines)


def render_live_validation(prod: ArmResult, live_start: pd.Timestamp, db_path: str) -> str:
    """
    Compare the production arm against the live trade log over the window the
    bot has actually been trading. Per [[feedback_trade_log_outranks_the_model]],
    a disagreement means the model is presumed broken, not the log.
    """
    sim = [t.r_multiple for t in prod.trades if t.entry_date >= live_start]
    sim_wins = sum(1 for t in prod.trades if t.entry_date >= live_start and t.pnl_dollars > 0)
    lines = [
        "",
        "=" * 96,
        f"MODEL vs LIVE — entries on/after {live_start.date()} (the bot's actual trading life)",
        "=" * 96,
    ]
    if sim:
        lines.append(
            f"  MODEL: n={len(sim):3}  win%={100 * sim_wins / len(sim):5.1f}"
            f"  mean R={sum(sim) / len(sim):+.2f}"
        )
    try:
        con = sqlite3.connect(db_path)
        rows = con.execute(
            "SELECT r_multiple, realized_pnl FROM trades "
            "WHERE strategy='donchian_breakout' AND r_multiple IS NOT NULL "
            "AND realized_pnl IS NOT NULL"
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        lines.append(f"  LIVE : unavailable ({exc})")
        return "\n".join(lines)
    if rows:
        lr = [r[0] for r in rows]
        lw = sum(1 for r in rows if r[1] > 0)
        lines.append(
            f"  LIVE : n={len(lr):3}  win%={100 * lw / len(lr):5.1f}"
            f"  mean R={sum(lr) / len(lr):+.2f}"
        )
        lines.append(
            "\n  Close agreement validates the model on the overlap window. It does NOT"
            "\n  license shipping a gate change — see 11.56 re-open condition (4)."
        )
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--universe", default=DEFAULT_UNIVERSE, choices=sorted(UNIVERSES))
    p.add_argument("--trade-start", default=DEFAULT_TRADE_START)
    p.add_argument("--end", default=DEFAULT_END)
    p.add_argument("--live-start", default="2026-05-01", help="first live Donchian entry")
    p.add_argument("--validate", action="store_true", help="compare against the live trade log")
    args = p.parse_args()

    trade_start = pd.Timestamp(args.trade_start, tz="UTC")
    end = pd.Timestamp(args.end, tz="UTC")
    symbols = sorted(UNIVERSES[args.universe])

    print(
        f"feed={BACKTEST_DATA_FEED}  universe={args.universe}({len(symbols)})  "
        f"trade_start={trade_start.date()}  end={end.date()}"
    )
    print(f"held constant: {SIM_CONSTANTS}, policy={POLICY.name}(k={POLICY.k})")

    spy = load_bars("SPY")
    if spy is None or spy.empty:
        logger.error("SPY bars unavailable — cannot classify regime")
        return 1
    spy = spy[spy.index <= end]
    regime = classify_spy_regime(spy)
    print(f"SPY regime distribution: {regime.value_counts().to_dict()}")

    bars = load_universe_bars(symbols, end)
    print(f"symbols with usable history: {len(bars)}/{len(symbols)}")
    if not bars:
        logger.error("no symbols loaded")
        return 1

    regime_masks, filter_masks = build_masks(bars, regime)
    both_masks = {s: (regime_masks[s] & filter_masks[s]) for s in bars}

    arms = [
        run_arm(ARM_RAW, bars, None, trade_start),
        run_arm(ARM_REGIME, bars, regime_masks, trade_start),
        run_arm(ARM_FILTER, bars, filter_masks, trade_start),
        run_arm(ARM_BOTH, bars, both_masks, trade_start),
    ]

    print(render_arms(arms))
    print(render_premise_test(arms[0], regime))
    print(render_per_year([arms[0], arms[3]], regime))
    if args.validate:
        print(
            render_live_validation(
                arms[3], pd.Timestamp(args.live_start, tz="UTC"), str(TRADE_LOG_DB)
            )
        )

    print(
        "\nLimitations: earnings blackout (DonchianEdgeFilter rule 2) has no offline"
        "\nequivalent and is unmodeled in every arm. Per-symbol runs are independent"
        "\n($100k each) — these are NOT portfolio returns: no shared capital, no"
        "\nallocator budget, no sector cap, no correlated-entry limit. Entries fill at"
        "\nthe next bar's open; production uses STOP_LIMIT with a chase cap."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
