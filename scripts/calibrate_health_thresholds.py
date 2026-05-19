#!/usr/bin/env python3
"""
Strategy Health threshold calibration helper (PLAN 11.10h trigger).

After 4+ weeks of paper operation the operator runs this script to
get **suggested** threshold adjustments for `strategies/health/thresholds.py`.
The script computes observed-distribution percentiles for each L2/L3
check across the trailing N weeks and proposes new `watch` / `degraded` /
`broken` cuts that would have produced "reasonable" verdicts on the
observed data.

This script is **advisory only**. It prints a diff-style report; the
operator manually edits `thresholds.py`. Per the v1 invariant (design
§1.2 — bot informs, operator decides), nothing in here writes config.

Usage:
    /Users/franco/trading-bot/venv/bin/python \\
        scripts/calibrate_health_thresholds.py --weeks 4

    /Users/franco/trading-bot/venv/bin/python \\
        scripts/calibrate_health_thresholds.py --weeks 8 --strategy donchian_breakout

Output:
    For each check that has measurable observations, prints:
      - current threshold tuple (watch / degraded / broken)
      - observed p50 / p90 / p99 of the metric in the window
      - suggested new tuple that would put ~80% of observations at
        HEALTHY, ~15% at WATCH, ~5% at DEGRADED, <1% at BROKEN
        (calibration heuristic — operator adjusts to taste)

Exits 1 if no usable data is available (forces operator awareness;
"no data" should not silently produce a green report).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from loguru import logger

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from reporting.logger import TradeLogger  # noqa: E402
from strategies.health.lifecycle import (  # noqa: E402
    LifecycleCounters,
    read_counters_for_period,
)
from strategies.health.thresholds import (  # noqa: E402
    _DEFAULTS,
    CheckThresholds,
    get_thresholds,
)


def _percentiles(values: list[float]) -> dict[str, float]:
    """Compute p50/p90/p95/p99 of `values`. Uses numpy.percentile
    (the same linear-interpolation method the assessor uses for
    p95 slippage, so calibration matches live behavior)."""
    if not values:
        return {p: 0.0 for p in ("p50", "p90", "p95", "p99")}
    arr = np.asarray(values, dtype=float)
    return {
        "p50": float(np.percentile(arr, 50)),
        "p90": float(np.percentile(arr, 90)),
        "p95": float(np.percentile(arr, 95)),
        "p99": float(np.percentile(arr, 99)),
    }


def _format_threshold(t: CheckThresholds) -> str:
    return (
        f"watch={t.watch:.4f}, degraded={t.degraded:.4f}, "
        f"broken={'None' if t.broken is None else f'{t.broken:.4f}'}"
    )


def _suggest_threshold(
    current: CheckThresholds, pcts: dict[str, float],
) -> CheckThresholds:
    """Heuristic suggestion: place watch at p90, degraded at p95,
    broken at p99 (or None if current is None). Operator tunes from
    there. For "below" direction checks (where lower is worse),
    invert the percentiles.

    The heuristic produces thresholds where ~10% of historical
    observations would have been WATCH, ~5% DEGRADED, ~1% BROKEN —
    a reasonable starting point. Operator adjusts per check based on
    domain knowledge.
    """
    if current.direction == "below":
        # For below-direction checks, higher percentile = better.
        # watch at p10 (low is bad), degraded at p05, broken at p01.
        # We don't have p10/p05/p01 — invert: use 100 - quantile.
        # Simpler: just flag this case and skip auto-suggest.
        return current
    return CheckThresholds(
        watch=round(pcts["p90"], 4),
        degraded=round(pcts["p95"], 4),
        broken=(
            None if current.broken is None
            else round(pcts["p99"], 4)
        ),
        direction=current.direction,
    )


def _collect_slippage_observations(
    conn: sqlite3.Connection,
    strategy_name: str,
    start: date,
    end: date,
) -> list[float]:
    """Per-trade |realized - modeled| slippage deltas for L2."""
    cursor = conn.execute(
        "SELECT realized_slippage_bps, modeled_slippage_bps "
        "FROM trades "
        "WHERE strategy = ? "
        "AND status IN ('filled', 'partial') "
        "AND realized_slippage_bps IS NOT NULL "
        "AND modeled_slippage_bps IS NOT NULL "
        "AND timestamp >= ? AND timestamp < ?",
        (strategy_name, start.isoformat(), end.isoformat()),
    )
    out: list[float] = []
    for realized, modeled in cursor.fetchall():
        try:
            out.append(abs(float(realized) - float(modeled)))
        except (TypeError, ValueError):
            continue
    return out


def _collect_drift_observations(
    conn: sqlite3.Connection,
    strategy_name: str,
    start: date,
    end: date,
) -> dict[str, list[float]]:
    """Sum lifecycle counter rows in [start, end) per ratio metric.

    Returns one list per L3 drift metric, where each list is the
    sequence of per-week ratios. The calibration heuristic produces
    thresholds against this distribution.
    """
    # Sum per-week so each entry is one week's ratio (operator-meaningful
    # granularity).
    out: dict[str, list[float]] = {
        "edge_filter_block_rate": [],
        "regime_block_rate": [],
        "risk_block_rate": [],
        "fill_rate": [],
    }
    # Walk week-by-week.
    cursor = conn.execute(
        "SELECT period_start, raw_signals, regime_blocked, "
        "edge_filter_blocked, sleeve_blocked, risk_blocked, "
        "submitted, filled_entries "
        "FROM strategy_lifecycle_counters "
        "WHERE strategy_name = ? "
        "AND period_type = 'weekly' "
        "AND period_start >= ? AND period_start < ? "
        "ORDER BY period_start ASC",
        (strategy_name, start.isoformat(), end.isoformat()),
    )
    for row in cursor.fetchall():
        _ps, raw, regime, edge, sleeve, risk, submitted, filled = row
        if raw and raw > 0:
            out["edge_filter_block_rate"].append(edge / raw)
            out["regime_block_rate"].append(regime / raw)
            out["risk_block_rate"].append(risk / raw)
        if submitted and submitted > 0:
            out["fill_rate"].append(filled / submitted)
    return out


def calibrate(
    conn: sqlite3.Connection,
    *,
    strategy_name: str,
    weeks: int,
    end_date: date,
) -> dict:
    """Compute observed distributions + suggested thresholds for one
    strategy over the trailing `weeks` window."""
    start = end_date - timedelta(weeks=weeks)
    result: dict = {
        "strategy": strategy_name,
        "window_start": start.isoformat(),
        "window_end": end_date.isoformat(),
        "weeks": weeks,
        "checks": {},
    }

    # ── L2: slippage delta p95 ────────────────────────────────────
    slippage = _collect_slippage_observations(
        conn, strategy_name, start, end_date,
    )
    if slippage:
        pcts = _percentiles(slippage)
        current = get_thresholds(
            strategy_name, "slippage_realized_vs_modeled_bps_p95",
        )
        result["checks"]["slippage_realized_vs_modeled_bps_p95"] = {
            "samples": len(slippage),
            "percentiles": pcts,
            "current": _format_threshold(current),
            "suggested": _format_threshold(
                _suggest_threshold(current, pcts),
            ),
        }

    # ── L3 drift checks ──────────────────────────────────────────
    drift_obs = _collect_drift_observations(
        conn, strategy_name, start, end_date,
    )
    for metric_key, threshold_key in [
        ("edge_filter_block_rate", "edge_filter_block_rate_drift_pct"),
        ("regime_block_rate", "regime_block_rate_drift_pct"),
        ("fill_rate", "fill_rate_drift_pct"),
    ]:
        samples = drift_obs.get(metric_key, [])
        if not samples:
            continue
        pcts = _percentiles(samples)
        try:
            current = get_thresholds(strategy_name, threshold_key)
        except KeyError:
            continue
        result["checks"][threshold_key] = {
            "samples": len(samples),
            "percentiles": pcts,
            "current": _format_threshold(current),
            "note": (
                "L3 drift thresholds compare observed-vs-envelope "
                "distance; raw block-rate distribution is a proxy. "
                "Use percentile distribution to choose tolerable "
                "drift width."
            ),
        }

    return result


def render_report(results: list[dict]) -> str:
    """Operator-readable diff report. Markdown so it can be pasted
    into a PR description or operator notebook."""
    if not results:
        return (
            "# Calibration report\n\n"
            "**No usable data** in the requested window. Check that:\n"
            "  - the bot has been running with HEALTH_COUNTERS_ENABLED=True\n"
            "  - the lifecycle counter table has rows for the requested weeks\n"
            "  - at least one strategy has produced fills in the window\n"
        )
    lines = [
        f"# Health threshold calibration — generated "
        f"{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        "",
        "**This report is advisory.** Per the v1 invariant (design §1.2)",
        "the bot never auto-applies these suggestions. The operator",
        "reviews and manually edits `strategies/health/thresholds.py`",
        "if any change is warranted.",
        "",
    ]
    for r in results:
        lines += [
            f"## {r['strategy']}",
            "",
            f"Window: `{r['window_start']}` → `{r['window_end']}` "
            f"({r['weeks']} weeks)",
            "",
        ]
        if not r["checks"]:
            lines += [
                "_No observations in this window. Either the strategy has",
                "no trades / no signals, or `HEALTH_COUNTERS_ENABLED` was",
                "False for this period._",
                "",
            ]
            continue
        for check_name, payload in sorted(r["checks"].items()):
            lines += [
                f"### {check_name}",
                "",
                f"- Samples: **{payload['samples']}**",
                f"- Percentiles: p50={payload['percentiles']['p50']:.4f}, "
                f"p90={payload['percentiles']['p90']:.4f}, "
                f"p95={payload['percentiles']['p95']:.4f}, "
                f"p99={payload['percentiles']['p99']:.4f}",
                f"- Current threshold: `{payload['current']}`",
            ]
            if "suggested" in payload:
                lines += [
                    f"- **Suggested**: `{payload['suggested']}`",
                ]
            if "note" in payload:
                lines += [f"- _{payload['note']}_"]
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
    )
    parser.add_argument(
        "--weeks", type=int, default=4,
        help="Trailing weeks of data to calibrate against (default 4).",
    )
    parser.add_argument(
        "--strategy", action="append", default=None,
        help=(
            "Limit calibration to a single strategy. Repeatable. "
            "Default: all strategies in STRATEGY_MIN_TRADES_FOR_VERDICT."
        ),
    )
    parser.add_argument(
        "--end-date", type=str, default=None,
        help="End date YYYY-MM-DD; defaults to today.",
    )
    parser.add_argument(
        "--output", type=str, default=None,
        help="Write report to this path (markdown). Default: stdout.",
    )
    args = parser.parse_args(argv)

    end_date = (
        date.fromisoformat(args.end_date)
        if args.end_date else date.today()
    )
    strategies = (
        list(args.strategy) if args.strategy
        else list(
            getattr(settings, "STRATEGY_MIN_TRADES_FOR_VERDICT", {}).keys(),
        )
    )
    if not strategies:
        logger.error(
            "no strategies to calibrate — either provide --strategy or "
            "configure STRATEGY_MIN_TRADES_FOR_VERDICT in settings."
        )
        return 1

    trade_logger = TradeLogger()
    conn = trade_logger._ensure_db()
    results = []
    try:
        for strategy_name in strategies:
            logger.info(
                f"calibrating {strategy_name} over trailing "
                f"{args.weeks} weeks ending {end_date.isoformat()}"
            )
            results.append(calibrate(
                conn, strategy_name=strategy_name,
                weeks=args.weeks, end_date=end_date,
            ))
    finally:
        trade_logger.close()

    report = render_report(results)
    if args.output:
        Path(args.output).write_text(report)
        logger.info(f"calibration report written: {args.output}")
    else:
        print(report)

    # Exit 1 if no usable data — forces operator awareness that
    # calibration is being asked for something it can't deliver.
    total_checks = sum(len(r["checks"]) for r in results)
    return 0 if total_checks > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
