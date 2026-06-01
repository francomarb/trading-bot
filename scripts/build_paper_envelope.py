#!/usr/bin/env python3
"""
Paper-data envelope builder for the two options sleeves.

Per design §15 Q7 + the build_envelopes.py stub comments at lines
200-221, the spy_options_reversion and credit_spread envelopes cannot be
built offline (no replayable OPRA chains). The design plan was that
`scripts/calibrate_health_thresholds.py` would populate them from paper
trades after 4 weeks. That script ended up calibrating L1/L2/L3 *Health*
thresholds only — Edge envelopes were never wired. This script closes
that gap.

Reads closed-trade R-multiples + realized P&L from `data/trades.db`
for the requested window, plus weekly rows from
`strategy_lifecycle_counters`, and writes a
`StrategyEnvelope` JSON to `data/envelopes/{strategy}.json` keyed to
paper-derived stats. The EdgeAssessor (`strategies/health/edge.py`)
then has a band to compare against, which is what unblocks the §5.2
verdict signal for options strategies.

Refusal modes — design §8 says false envelopes are worse than no envelope:

  - `--min-trades` floor (default 10) not met → exit 2
  - All r_multiples NULL → exit 3 with a hint pointing at the spread
    R-multiple wiring (depends on `log_spread_fill` writing
    `initial_risk_dollars`)
  - Fewer than 4 weekly lifecycle counter rows → exit 4

Per the v1 invariant (design §1.2), this script is advisory: the
operator runs it and inspects the resulting JSON before letting the
EdgeAssessor consume it. The file is written under `data/envelopes/`
which is gitignored — each operator/machine generates its own.

Usage:
    /Users/franco/trading-bot/venv/bin/python \\
        scripts/build_paper_envelope.py \\
        --strategy credit_spread --weeks 8

    /Users/franco/trading-bot/venv/bin/python \\
        scripts/build_paper_envelope.py \\
        --strategy spy_options_reversion --weeks 6 --min-trades 8
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from strategies.health.envelope import (  # noqa: E402
    FilterFidelity,
    StrategyEnvelope,
    envelope_path,
)
from strategies.health.stats import (  # noqa: E402
    bootstrap_mean_ci,
    profit_factor as compute_profit_factor,
    win_rate as compute_win_rate,
)


SCHEMA_VERSION = 1
MIN_WEEKLY_LIFECYCLE_ROWS = 4

# Exit codes — distinct so callers / tests can assert specifically.
EXIT_INSUFFICIENT_TRADES = 2
EXIT_ALL_R_NULL = 3
EXIT_INSUFFICIENT_LIFECYCLE = 4


# ── Data classes (internal) ────────────────────────────────────────────


class _ClosedTradeRow:
    """One closed-trade row read from `trades`. Plain attribute holder."""

    __slots__ = (
        "r_multiple",
        "realized_pnl",
        "entry_timestamp",
        "exit_timestamp",
    )

    def __init__(
        self,
        *,
        r_multiple: float | None,
        realized_pnl: float,
        entry_timestamp: str | None,
        exit_timestamp: str | None,
    ):
        self.r_multiple = r_multiple
        self.realized_pnl = realized_pnl
        self.entry_timestamp = entry_timestamp
        self.exit_timestamp = exit_timestamp


# ── DB readers ────────────────────────────────────────────────────────


def _read_closed_trades(
    conn: sqlite3.Connection,
    *,
    strategy_name: str,
    period_start: date,
    period_end: date,
) -> list[_ClosedTradeRow]:
    """Closed-trade rows for `strategy_name` in `[period_start, period_end)`.

    Mirrors `edge.py::_read_closed_trades` filters so the envelope's
    sample matches what the live assessor sees: side='sell' OR
    position_type='spread', filled or partial, realized_pnl present.
    """
    cursor = conn.execute(
        "SELECT r_multiple, realized_pnl, entry_timestamp, exit_timestamp "
        "FROM trades "
        "WHERE strategy = ? "
        "AND (side = 'sell' OR position_type = 'spread') "
        "AND status IN ('filled', 'partial') "
        "AND realized_pnl IS NOT NULL "
        "AND timestamp >= ? "
        "AND timestamp < ? "
        "ORDER BY id ASC",
        (strategy_name, period_start.isoformat(), period_end.isoformat()),
    )
    rows: list[_ClosedTradeRow] = []
    for r_mult, pnl, entry_ts, exit_ts in cursor.fetchall():
        if pnl is None:
            continue
        try:
            pnl_f = float(pnl)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(pnl_f):
            continue
        r_value: float | None = None
        if r_mult is not None:
            try:
                r_value = float(r_mult)
            except (TypeError, ValueError):
                r_value = None
            if r_value is not None and not math.isfinite(r_value):
                r_value = None
        rows.append(_ClosedTradeRow(
            r_multiple=r_value,
            realized_pnl=pnl_f,
            entry_timestamp=entry_ts,
            exit_timestamp=exit_ts,
        ))
    return rows


def _read_weekly_lifecycle_rows(
    conn: sqlite3.Connection,
    *,
    strategy_name: str,
    period_start: date,
    period_end: date,
) -> list[dict[str, int]]:
    """Per-week counter rows for `strategy_name` in window.

    Returns one dict per weekly row stored in `strategy_lifecycle_counters`
    with `period_type='weekly'`. Aggregation across weeks happens in the
    envelope-band computation; we keep rows separate here so the bands
    reflect week-to-week variation.
    """
    cursor = conn.execute(
        "SELECT raw_signals, regime_blocked, edge_filter_blocked, "
        "sleeve_blocked, risk_blocked, submitted, filled_entries "
        "FROM strategy_lifecycle_counters "
        "WHERE strategy_name = ? "
        "AND period_type = 'weekly' "
        "AND period_start >= ? "
        "AND period_end <= ? "
        "ORDER BY period_start ASC",
        (strategy_name, period_start.isoformat(), period_end.isoformat()),
    )
    out: list[dict[str, int]] = []
    for row in cursor.fetchall():
        out.append({
            "raw_signals": int(row[0]),
            "regime_blocked": int(row[1]),
            "edge_filter_blocked": int(row[2]),
            "sleeve_blocked": int(row[3]),
            "risk_blocked": int(row[4]),
            "submitted": int(row[5]),
            "filled_entries": int(row[6]),
        })
    return out


# ── Band / metric helpers ─────────────────────────────────────────────


def _p10_p90(values: Sequence[float]) -> tuple[float, float] | None:
    """Return (p10, p90) of `values`, or None when fewer than 2 samples."""
    finite = [float(v) for v in values if math.isfinite(float(v))]
    if len(finite) < 2:
        return None
    finite.sort()
    # Simple linear-interpolation quantiles. numpy would be overkill here
    # and would couple this script to numpy at import time.
    def _q(p: float) -> float:
        idx = p * (len(finite) - 1)
        lo = int(math.floor(idx))
        hi = int(math.ceil(idx))
        if lo == hi:
            return finite[lo]
        frac = idx - lo
        return finite[lo] + frac * (finite[hi] - finite[lo])
    return (_q(0.10), _q(0.90))


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator > 0 else None


def _hold_days(row: _ClosedTradeRow) -> float | None:
    if row.entry_timestamp is None or row.exit_timestamp is None:
        return None
    try:
        entry = datetime.fromisoformat(row.entry_timestamp)
        exit_ = datetime.fromisoformat(row.exit_timestamp)
    except ValueError:
        return None
    delta = (exit_ - entry).total_seconds() / 86400.0
    return delta if math.isfinite(delta) and delta >= 0 else None


def _cumulative_r_drawdown_pct(r_values: Sequence[float]) -> float | None:
    """p95 drawdown depth on the cumulative-R equity curve.

    Drawdown depth is `peak − current` in R units. With cumulative R
    starting at 0, expressing this as a percentage of the running peak
    requires a non-zero peak; for early-stage paper data we report the
    raw R magnitude of the worst-95% drawdown event instead so the
    envelope field has a value the assessor can compare against.
    """
    finite = [float(v) for v in r_values if math.isfinite(float(v))]
    if len(finite) < 2:
        return None
    cumulative = 0.0
    peak = 0.0
    drawdowns: list[float] = []
    for r in finite:
        cumulative += r
        if cumulative > peak:
            peak = cumulative
        drawdowns.append(peak - cumulative)
    if not drawdowns:
        return None
    drawdowns.sort()
    idx = 0.95 * (len(drawdowns) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return float(drawdowns[lo])
    frac = idx - lo
    return float(drawdowns[lo] + frac * (drawdowns[hi] - drawdowns[lo]))


# ── Builder ───────────────────────────────────────────────────────────


class PaperEnvelopeError(SystemExit):
    """Refusal sentinel — raised for the explicit refusal modes the design
    requires (insufficient trades, all R NULL, insufficient lifecycle rows).
    Carries an exit code so the operator + tests can assert the failure mode.
    """

    def __init__(self, code: int, message: str):
        super().__init__(code)
        self.code = code
        self.message = message


def build_envelope(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    period_start: date,
    period_end: date,
    min_trades: int,
    now: datetime | None = None,
) -> StrategyEnvelope:
    """Compute a `StrategyEnvelope` from paper data in the window.

    Pure function — does not touch the filesystem. The caller writes the
    JSON. Raises `PaperEnvelopeError` for the explicit refusal modes.
    """
    closed = _read_closed_trades(
        conn,
        strategy_name=strategy,
        period_start=period_start,
        period_end=period_end,
    )
    if len(closed) < min_trades:
        raise PaperEnvelopeError(
            code=EXIT_INSUFFICIENT_TRADES,
            message=(
                f"refusing to build envelope: {strategy} has {len(closed)} "
                f"closed trades in [{period_start}..{period_end}); "
                f"--min-trades={min_trades}. Wait for more paper data or "
                f"widen --weeks."
            ),
        )

    r_values = [t.r_multiple for t in closed if t.r_multiple is not None]
    pnls = [t.realized_pnl for t in closed]

    if not r_values:
        raise PaperEnvelopeError(
            code=EXIT_ALL_R_NULL,
            message=(
                f"refusing to build envelope: {strategy} has "
                f"{len(closed)} closed trades but every r_multiple is NULL. "
                f"For credit_spread, this means log_spread_fill is not yet "
                f"writing initial_risk_dollars at open (the R-multiple basis). "
                f"For spy_options_reversion, check that single-leg close paths "
                f"could find their entry context. The Edge envelope cannot be "
                f"fit without R-multiples — fix the upstream capture first."
            ),
        )

    weekly_rows = _read_weekly_lifecycle_rows(
        conn,
        strategy_name=strategy,
        period_start=period_start,
        period_end=period_end,
    )
    if len(weekly_rows) < MIN_WEEKLY_LIFECYCLE_ROWS:
        raise PaperEnvelopeError(
            code=EXIT_INSUFFICIENT_LIFECYCLE,
            message=(
                f"refusing to build envelope: {strategy} has "
                f"{len(weekly_rows)} weekly lifecycle rows in "
                f"[{period_start}..{period_end}); need >= "
                f"{MIN_WEEKLY_LIFECYCLE_ROWS} for (p10, p90) bands. "
                f"Confirm HEALTH_COUNTERS_ENABLED=True and widen --weeks."
            ),
        )

    # ── Edge metrics ────────────────────────────────────────────────
    r_expectancy = sum(r_values) / len(r_values)
    r_ci = bootstrap_mean_ci(r_values, seed=0)
    expectancy_dollars = sum(pnls) / len(pnls)
    dollars_ci = bootstrap_mean_ci(pnls, seed=0)
    pf = compute_profit_factor(pnls)
    wr = compute_win_rate(pnls)
    # Bootstrap CI on win-rate via resampling the binary array.
    binary_wins = [1.0 if p > 0 else 0.0 for p in pnls]
    wr_ci = bootstrap_mean_ci(binary_wins, seed=0)

    # ── Behavior bands ─────────────────────────────────────────────
    hold_days_values = [d for d in (_hold_days(t) for t in closed) if d is not None]
    hold_days_band = _p10_p90(hold_days_values)
    p95_drawdown = _cumulative_r_drawdown_pct(r_values)

    # trades_per_month: extrapolate from window length. Single point
    # estimate — the band is the (rate, rate) tuple when window is too
    # short for a meaningful spread.
    window_days = (period_end - period_start).days
    trades_per_month: tuple[float, float] | None = None
    if window_days > 0:
        rate = len(closed) * (30.0 / window_days)
        # Construct a band by partitioning closes into halves and computing
        # each half's rate; widens with sample. Cheap proxy until block
        # bootstrap (design §F3) lands.
        midpoint = period_start + timedelta(days=window_days // 2)
        first_half = [t for t in closed if t.exit_timestamp and
                      datetime.fromisoformat(t.exit_timestamp).date() < midpoint]
        second_half = [t for t in closed if t.exit_timestamp and
                       datetime.fromisoformat(t.exit_timestamp).date() >= midpoint]
        half_days = max(window_days // 2, 1)
        rates = [
            len(first_half) * (30.0 / half_days),
            len(second_half) * (30.0 / half_days),
            rate,
        ]
        trades_per_month = (min(rates), max(rates))

    # ── Lifecycle bands ────────────────────────────────────────────
    def _series(key: str) -> list[int]:
        return [int(w[key]) for w in weekly_rows]

    raw_series = _series("raw_signals")
    raw_signals_band = _p10_p90([float(v) for v in raw_series])

    edge_block_rates = [
        _ratio(w["edge_filter_blocked"], w["raw_signals"])
        for w in weekly_rows
    ]
    regime_block_rates = [
        _ratio(w["regime_blocked"], w["raw_signals"])
        for w in weekly_rows
    ]
    risk_block_rates = [
        _ratio(w["risk_blocked"], w["raw_signals"])
        for w in weekly_rows
    ]
    submitted_rates = [
        _ratio(w["submitted"], w["raw_signals"])
        for w in weekly_rows
    ]
    fill_rates = [
        _ratio(w["filled_entries"], w["submitted"])
        for w in weekly_rows
    ]

    def _band(rates: list[float | None]) -> tuple[float, float] | None:
        finite = [r for r in rates if r is not None and math.isfinite(r)]
        return _p10_p90(finite)

    # ── Construction ───────────────────────────────────────────────
    built_at = (now or datetime.now(timezone.utc)).isoformat()
    note = (
        f"paper-derived envelope: {strategy}, "
        f"window {period_start}..{period_end}, "
        f"N={len(closed)} closed trades ({len(r_values)} with r_multiple), "
        f"weekly_rows={len(weekly_rows)}"
    )

    return StrategyEnvelope(
        schema_version=SCHEMA_VERSION,
        strategy=strategy,
        built_at=built_at,
        backtest_window_start=period_start.isoformat(),
        backtest_window_end=period_end.isoformat(),
        backtest_config={
            "source": "paper",
            "window_start": period_start.isoformat(),
            "window_end": period_end.isoformat(),
            "n_trades": len(closed),
            "n_r_values": len(r_values),
            "n_weekly_lifecycle_rows": len(weekly_rows),
        },
        filter_fidelity=FilterFidelity.PRODUCTION_FAITHFUL,
        r_expectancy=r_expectancy,
        r_expectancy_ci_95=r_ci,
        risk_unit_dollars=None,
        expectancy_dollars=expectancy_dollars,
        expectancy_dollars_ci_95=dollars_ci,
        win_rate=wr,
        win_rate_ci_95=wr_ci,
        profit_factor=pf,
        profit_factor_ci_95=None,
        sharpe=None,
        cagr=None,
        trade_count=len(closed),
        trades_per_month_band=trades_per_month,
        hold_days_band=hold_days_band,
        p95_drawdown_pct=p95_drawdown,
        raw_signals_per_week_band=raw_signals_band,
        edge_filter_block_rate_band=_band(edge_block_rates),
        regime_block_rate_band=_band(regime_block_rates),
        risk_block_rate_band=_band(risk_block_rates),
        submitted_per_raw_signal_band=_band(submitted_rates),
        fill_rate_band=_band(fill_rates),
        notes=(note,),
    )


# ── CLI ───────────────────────────────────────────────────────────────


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build a StrategyEnvelope JSON from paper-trade data. "
            "Intended for spy_options_reversion / credit_spread (design "
            "§15 Q7). Refuses on insufficient sample or NULL R-multiples."
        ),
    )
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--weeks", type=int, default=8)
    parser.add_argument("--min-trades", type=int, default=10)
    parser.add_argument(
        "--db",
        default=None,
        help="Override path to trades.db (defaults to settings.TRADE_LOG_DB).",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Override output path (defaults to data/envelopes/<strategy>.json).",
    )
    parser.add_argument(
        "--end-date",
        default=None,
        help="ISO date for the window end (default: today UTC).",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)

    end_date = (
        date.fromisoformat(args.end_date)
        if args.end_date else datetime.now(timezone.utc).date()
    )
    start_date = end_date - timedelta(weeks=args.weeks)

    db_path = args.db or settings.TRADE_LOG_DB
    if not Path(db_path).exists():
        print(f"error: trades.db not found at {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path)
    try:
        try:
            envelope = build_envelope(
                conn,
                strategy=args.strategy,
                period_start=start_date,
                period_end=end_date,
                min_trades=args.min_trades,
            )
        except PaperEnvelopeError as exc:
            print(f"error: {exc.message}", file=sys.stderr)
            return exc.code
    finally:
        conn.close()

    out_path = Path(args.out) if args.out else envelope_path(args.strategy)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(envelope.to_json())

    print(f"wrote {out_path}")
    print(
        f"  strategy={envelope.strategy} "
        f"window={start_date}..{end_date} "
        f"N={envelope.trade_count} "
        f"r_expectancy={envelope.r_expectancy:.4f} "
        f"r_expectancy_ci={envelope.r_expectancy_ci_95}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
