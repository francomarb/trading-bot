"""
HealthAssessor — the forensic layer.

Reads engine state + lifecycle counters + trade slippage data, runs
L1 (operational) / L2 (execution) / L3 (drift) checks against the
inline thresholds (strategies/health/thresholds.py) and the
strategy's envelope (for L3 drift bands), returns a HealthReport.

Per design §3 (Edge/Health primacy) and §1.2 (advisory-only invariant):
**Health is forensics, never the verdict.** A BROKEN Health on a
profitable strategy still gets "keep untouched, fix in parallel" from
the reviewer. This module ONLY computes the report; the reviewer
(11.10e) combines it with the EdgeReport for the recommendation.

Defensive about its inputs:
  - engine_state.json may be missing fields that 11.10f will add
    (`risk_controls`); those checks return HEALTHY with a "not yet
    wired" finding rather than crashing.
  - Lifecycle counters table may be empty (engine wiring is 11.10f);
    L3 drift checks return WATCH/HEALTHY accordingly.
  - Envelope may be missing or have null lifecycle bands; checks that
    need a band silently degrade to HEALTHY with an explanatory
    finding.

This degraded-input handling is deliberate — v1 ships before the
engine wiring, so the first weeks of operation will have partial
data. The reviewer surfaces what's there.

Per design §3.6: L3 checks cannot return BROKEN — that's enforced at
CheckResult construction (it raises ValueError). The drift checks here
return WATCH or DEGRADED at most.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from strategies.health.envelope import StrategyEnvelope
from strategies.health.lifecycle import (
    LifecycleCounters,
    read_counters_for_period,
)
from strategies.health.reports import (
    CheckResult,
    HealthReport,
    HealthStatus,
    Layer,
)
from strategies.health.thresholds import CheckThresholds, get_thresholds


def _drift_from_band(observed: float, band: tuple[float, float]) -> float:
    """Drift of `observed` from envelope band `[lo, hi]`.

    Inside the band → 0 (the band IS the expected range; nothing to
    flag). Outside the band → distance to the nearest bound, expressed
    as a fraction of that bound's magnitude.

    PR #19 reviewer caught the original midpoint-distance formula:
    `abs(observed - midpoint) / midpoint` reported in-band values as
    drift (a band of 10–30 with observed=10 was reported as 50% drift
    even though 10 is the band's lower bound). The correct semantics
    is "drift kicks in when we leave the band, measured from the
    boundary we crossed."

    Defensive: bound magnitude floors at 1e-9 to avoid divide-by-zero
    on zero-anchored bands (e.g., a regime_block_rate_band like
    (0.0, 0.10) is legal — most weeks see no regime blocks).
    """
    lo, hi = band
    if lo <= observed <= hi:
        return 0.0
    if observed < lo:
        denom = max(abs(lo), 1e-9)
        return (lo - observed) / denom
    # observed > hi
    denom = max(abs(hi), 1e-9)
    return (observed - hi) / denom


# ── Inputs ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HealthInputs:
    """All inputs the HealthAssessor needs to produce one strategy's
    HealthReport over one period window.

    `engine_state` is the parsed contents of `data/engine_state.json`
    (or empty dict when the file doesn't exist yet — first run, or
    pre-11.10f wiring). Health checks degrade gracefully on missing
    fields.

    `envelope` provides the L3 drift bands. None or all-null bands →
    drift checks return HEALTHY with "no baseline" finding.
    """

    strategy_name: str
    period_start: date
    period_end: date
    envelope: StrategyEnvelope | None
    conn: sqlite3.Connection
    engine_state: dict[str, Any]


# ── Threshold → status mapping ───────────────────────────────────────


def _classify(
    value: float,
    thresholds: CheckThresholds,
    *,
    layer: Layer,
) -> HealthStatus:
    """Apply a CheckThresholds tuple to a numeric value, returning the
    matching HealthStatus.

    `direction="above"` (default): higher is worse.
      x < watch          → HEALTHY
      watch <= x < degraded → WATCH
      degraded <= x < broken → DEGRADED
      x >= broken        → BROKEN (or DEGRADED if broken is None / layer is L3)

    `direction="below"`: lower is worse.
      x >= watch         → HEALTHY
      degraded <= x < watch → WATCH
      broken <= x < degraded → DEGRADED
      x < broken         → BROKEN (or DEGRADED if broken is None / layer is L3)

    L3 invariant per design §3.6: drift checks cannot be BROKEN. Even
    if the thresholds define a `broken` value, this function caps L3
    at DEGRADED. (Construction-time guard in CheckResult also enforces
    this, so this is defense-in-depth.)
    """
    if thresholds.direction == "above":
        if value < thresholds.watch:
            return HealthStatus.HEALTHY
        if value < thresholds.degraded:
            return HealthStatus.WATCH
        if thresholds.broken is not None and value >= thresholds.broken:
            return (
                HealthStatus.DEGRADED if layer is Layer.L3 else HealthStatus.BROKEN
            )
        return HealthStatus.DEGRADED
    # "below" direction
    if value >= thresholds.watch:
        return HealthStatus.HEALTHY
    if value >= thresholds.degraded:
        return HealthStatus.WATCH
    if thresholds.broken is not None and value < thresholds.broken:
        return (
            HealthStatus.DEGRADED if layer is Layer.L3 else HealthStatus.BROKEN
        )
    return HealthStatus.DEGRADED


# ── L1 Operational checks ────────────────────────────────────────────


def _l1_checks(
    strategy_name: str,
    engine_state: dict[str, Any],
    conn: sqlite3.Connection,
    period_start: date,
    period_end: date,
) -> list[CheckResult]:
    """Operational checks per design §6 L1. Pulls from engine_state.json.

    Many of these depend on 11.10f wiring the snapshot with new fields
    (cooldown state, drift switches, cycle latency). Until then, the
    checks that find their data return real verdicts; the others
    return HEALTHY with "not yet wired" findings so the report stays
    consistent.
    """
    out: list[CheckResult] = []

    # ── Halted / cooldown state ───────────────────────────────────
    # If the engine snapshot indicates the strategy is currently
    # halted or in cooldown, surface as WATCH (informational — the
    # operator likely caused this; not a Health failure on its own).
    risk_controls = engine_state.get("risk_controls", {})
    if "cooldown_state" in risk_controls:
        # 11.10f will add this. Structure: dict of strategy → state.
        st = risk_controls.get("cooldown_state", {}).get(strategy_name)
        if st and st.get("active"):
            out.append(CheckResult(
                name="strategy_cooldown",
                layer=Layer.L1,
                status=HealthStatus.WATCH,
                findings=[
                    f"strategy currently in cooldown until "
                    f"{st.get('until', 'unknown')}",
                ],
            ))
        else:
            out.append(_healthy("strategy_cooldown", Layer.L1, "no cooldown active"))
    else:
        out.append(_not_yet_wired("strategy_cooldown", Layer.L1))

    # ── Reconciliation mismatches ────────────────────────────────
    # 11.10f will add `risk_controls.reconciliation_mismatches_24h` or
    # similar; for now, parse from JSON logs is not implemented.
    out.append(_not_yet_wired("reconciliation_mismatches_24h", Layer.L1))
    out.append(_not_yet_wired("missing_stop_repairs_24h", Layer.L1))
    out.append(_not_yet_wired("stream_disconnects_per_day", Layer.L1))
    out.append(_not_yet_wired("cycle_latency_p95_ms", Layer.L1))

    # ── Ownership conflicts (already alerted, count in engine state) ──
    # Two parallel buckets (PLAN 11.44):
    #   * symbol_conflicts_24h   — equity-level slot overlap rejections
    #     (e.g. two equity strategies both want AAPL).
    #   * contract_conflicts_24h — leg-level exact-OCC rejections across
    #     single-leg and MLEG option strategies. Distinct from symbol
    #     conflicts because the remediation differs (picker tuning vs
    #     slot config) and an unblocked contract conflict would corrupt
    #     ownership at the broker (positions aggregate by exact symbol).
    for field, label in (
        ("symbol_conflicts_24h", "symbol-conflict"),
        ("contract_conflicts_24h", "contract-conflict"),
    ):
        value = engine_state.get(field)
        if value is None:
            continue
        try:
            thresh = get_thresholds(strategy_name, "reconciliation_mismatches_24h")
            status = _classify(float(value), thresh, layer=Layer.L1)
            out.append(CheckResult(
                name=field,
                layer=Layer.L1,
                status=status,
                numeric_value=float(value),
                findings=[
                    f"{int(value)} {label} event(s) in last 24h"
                ],
            ))
        except KeyError:
            out.append(_healthy(field, Layer.L1))

    return out


# ── L2 Execution checks ──────────────────────────────────────────────


def _l2_checks(
    strategy_name: str,
    conn: sqlite3.Connection,
    period_start: date,
    period_end: date,
) -> list[CheckResult]:
    """Execution checks per design §6 L2. Reads from trades table for
    slippage / fill / partial-fill metrics."""
    out: list[CheckResult] = []

    # ── Realized slippage p95 ────────────────────────────────────
    slippage_p95 = _slippage_p95_bps(
        conn, strategy_name, period_start, period_end,
    )
    if slippage_p95 is None:
        out.append(_no_data("slippage_realized_vs_modeled_bps_p95", Layer.L2))
    else:
        try:
            thresh = get_thresholds(
                strategy_name, "slippage_realized_vs_modeled_bps_p95",
            )
            status = _classify(slippage_p95, thresh, layer=Layer.L2)
            out.append(CheckResult(
                name="slippage_realized_vs_modeled_bps_p95",
                layer=Layer.L2,
                status=status,
                numeric_value=slippage_p95,
                findings=[f"p95 realized slippage = {slippage_p95:.1f} bps"],
            ))
        except KeyError:
            out.append(_healthy(
                "slippage_realized_vs_modeled_bps_p95", Layer.L2,
            ))

    # ── Partial fill rate ───────────────────────────────────────
    partial_rate = _partial_fill_rate(
        conn, strategy_name, period_start, period_end,
    )
    if partial_rate is None:
        out.append(_no_data("partial_fill_rate", Layer.L2))
    else:
        try:
            thresh = get_thresholds(strategy_name, "partial_fill_rate")
            status = _classify(partial_rate, thresh, layer=Layer.L2)
            out.append(CheckResult(
                name="partial_fill_rate",
                layer=Layer.L2,
                status=status,
                numeric_value=partial_rate,
                findings=[f"partial fills = {partial_rate:.1%} of orders"],
            ))
        except KeyError:
            out.append(_healthy("partial_fill_rate", Layer.L2))

    # ── Other L2 checks (signal-to-fill conversion, time-to-fill, etc.)
    # ── depend on data not yet captured pre-11.10f.
    out.append(_not_yet_wired("order_rejection_rate", Layer.L2))
    out.append(_not_yet_wired("timeout_cancel_rate", Layer.L2))

    return out


def _slippage_p95_bps(
    conn: sqlite3.Connection,
    strategy_name: str,
    period_start: date,
    period_end: date,
) -> float | None:
    """p95 of |realized_slippage_bps - modeled_slippage_bps| over the
    window. None if no fills with slippage data."""
    cursor = conn.execute(
        "SELECT realized_slippage_bps, modeled_slippage_bps "
        "FROM trades "
        "WHERE strategy = ? "
        "AND status IN ('filled', 'partial') "
        "AND realized_slippage_bps IS NOT NULL "
        "AND modeled_slippage_bps IS NOT NULL "
        "AND timestamp >= ? "
        "AND timestamp < ?",
        (strategy_name, period_start.isoformat(), period_end.isoformat()),
    )
    deltas = []
    for realized, modeled in cursor.fetchall():
        try:
            deltas.append(abs(float(realized) - float(modeled)))
        except (TypeError, ValueError):
            continue
    if not deltas:
        return None
    # PR #19 reviewer caught the original `int(0.95 * (n - 1))` floor:
    # with n=2 it returned the lower sample as p95 (hiding the bad
    # fill); with n=3 it returned the median. numpy.percentile uses
    # the standard linear interpolation between order statistics —
    # correct on small samples.
    return float(np.percentile(deltas, 95))


def _partial_fill_rate(
    conn: sqlite3.Connection,
    strategy_name: str,
    period_start: date,
    period_end: date,
) -> float | None:
    """`partial` status count / (`filled` + `partial`) over the window.
    None if no fills."""
    cursor = conn.execute(
        "SELECT status, COUNT(*) FROM trades "
        "WHERE strategy = ? "
        "AND status IN ('filled', 'partial') "
        "AND timestamp >= ? "
        "AND timestamp < ? "
        "GROUP BY status",
        (strategy_name, period_start.isoformat(), period_end.isoformat()),
    )
    counts = dict(cursor.fetchall())
    filled = counts.get("filled", 0)
    partial = counts.get("partial", 0)
    total = filled + partial
    if total == 0:
        return None
    return partial / total


# ── L3 Drift checks ──────────────────────────────────────────────────


def _l3_checks(
    strategy_name: str,
    envelope: StrategyEnvelope | None,
    conn: sqlite3.Connection,
    period_start: date,
    period_end: date,
) -> list[CheckResult]:
    """Drift checks per design §6 L3. Compares live lifecycle counter
    ratios against envelope bands. **L3 cannot be BROKEN** (drift is
    gradual by nature); CheckResult enforces this at construction.

    When envelope is None or all lifecycle bands are null → checks
    return HEALTHY with "no envelope baseline" findings. When the
    counter table is empty (pre-11.10f) → same degraded handling.
    """
    out: list[CheckResult] = []

    counters = read_counters_for_period(
        conn,
        strategy_name=strategy_name,
        start=period_start,
        end=period_end,
    )
    raw_signals_total = counters.raw_signals

    # ── Trade frequency vs envelope band ─────────────────────────
    band = envelope.raw_signals_per_week_band if envelope is not None else None
    if band is None:
        out.append(_no_envelope_band("trade_frequency_drift_pct", Layer.L3))
    elif raw_signals_total == 0:
        out.append(CheckResult(
            name="trade_frequency_drift_pct",
            layer=Layer.L3,
            status=HealthStatus.HEALTHY,
            findings=["lifecycle counters empty — engine wiring pending (11.10f)"],
        ))
    else:
        # Period length in weeks → expected raw_signals
        weeks = max((period_end - period_start).days / 7.0, 1e-9)
        observed_per_week = raw_signals_total / weeks
        drift_pct = _drift_from_band(observed_per_week, band)
        thresh = get_thresholds(strategy_name, "trade_frequency_drift_pct")
        status = _classify(drift_pct, thresh, layer=Layer.L3)
        out.append(CheckResult(
            name="trade_frequency_drift_pct",
            layer=Layer.L3,
            status=status,
            numeric_value=drift_pct,
            findings=[
                f"observed {observed_per_week:.1f}/wk vs envelope "
                f"band {band[0]:.1f}-{band[1]:.1f}/wk "
                f"(drift {drift_pct:.1%})"
            ],
        ))

    # ── Block-rate drift checks (edge filter, regime, fill rate) ──
    out.extend(_drift_ratio_check(
        check_name="edge_filter_block_rate_drift_pct",
        numerator=counters.edge_filter_blocked,
        denominator=raw_signals_total,
        envelope_band=(
            envelope.edge_filter_block_rate_band
            if envelope is not None else None
        ),
        strategy_name=strategy_name,
        threshold_key="edge_filter_block_rate_drift_pct",
        label="edge filter block rate",
    ))
    out.extend(_drift_ratio_check(
        check_name="regime_block_rate_drift_pct",
        numerator=counters.regime_blocked,
        denominator=raw_signals_total,
        envelope_band=(
            envelope.regime_block_rate_band
            if envelope is not None else None
        ),
        strategy_name=strategy_name,
        threshold_key="regime_block_rate_drift_pct",
        label="regime block rate",
    ))
    out.extend(_drift_ratio_check(
        check_name="fill_rate_drift_pct",
        numerator=counters.filled_entries,
        denominator=counters.submitted,
        envelope_band=(
            envelope.fill_rate_band if envelope is not None else None
        ),
        strategy_name=strategy_name,
        threshold_key="fill_rate_drift_pct",
        label="fill rate",
    ))

    return out


def _drift_ratio_check(
    *,
    check_name: str,
    numerator: int,
    denominator: int,
    envelope_band: tuple[float, float] | None,
    strategy_name: str,
    threshold_key: str,
    label: str,
) -> list[CheckResult]:
    """Generic drift-from-band check for ratio metrics (block rates,
    fill rate). Returns a list (always 1 element) for compositional
    convenience with the L3 builder."""
    if envelope_band is None:
        return [_no_envelope_band(check_name, Layer.L3)]
    if denominator == 0:
        return [CheckResult(
            name=check_name,
            layer=Layer.L3,
            status=HealthStatus.HEALTHY,
            findings=[
                f"{label}: no observations yet "
                f"(denominator=0; pending lifecycle counters from 11.10f)"
            ],
        )]
    observed = numerator / denominator
    drift_pct = _drift_from_band(observed, envelope_band)
    thresh = get_thresholds(strategy_name, threshold_key)
    status = _classify(drift_pct, thresh, layer=Layer.L3)
    return [CheckResult(
        name=check_name,
        layer=Layer.L3,
        status=status,
        numeric_value=drift_pct,
        findings=[
            f"{label}: observed {observed:.1%} vs envelope band "
            f"{envelope_band[0]:.1%}-{envelope_band[1]:.1%} "
            f"(drift {drift_pct:.1%})"
        ],
    )]


# ── Convenience constructors for common "no data" results ────────────


def _healthy(name: str, layer: Layer, finding: str = "") -> CheckResult:
    return CheckResult(
        name=name,
        layer=layer,
        status=HealthStatus.HEALTHY,
        findings=[finding] if finding else (),
    )


def _not_yet_wired(name: str, layer: Layer) -> CheckResult:
    """A check whose data source is engine wiring (11.10f) that hasn't
    landed yet. Status HEALTHY but `findings` says so explicitly so the
    operator knows the check exists and where its data will come from."""
    return CheckResult(
        name=name,
        layer=layer,
        status=HealthStatus.HEALTHY,
        findings=[
            "check stub — data source pending engine wiring (11.10f)"
        ],
    )


def _no_data(name: str, layer: Layer) -> CheckResult:
    """A check whose data SHOULD be available but isn't (no trades in
    window, etc.). HEALTHY — absence of data is not a failure."""
    return CheckResult(
        name=name,
        layer=layer,
        status=HealthStatus.HEALTHY,
        findings=["no observations in window"],
    )


def _no_envelope_band(name: str, layer: Layer) -> CheckResult:
    """L3 drift check with no envelope band to compare against (or
    null band). HEALTHY — can't claim drift without a baseline."""
    return CheckResult(
        name=name,
        layer=layer,
        status=HealthStatus.HEALTHY,
        findings=[
            "envelope band unavailable — "
            "L3 drift check skipped (11.10g calibration pending)"
        ],
    )


# ── Engine state loader ──────────────────────────────────────────────


def load_engine_state(path: str | Path | None = None) -> dict[str, Any]:
    """Read `data/engine_state.json` if it exists, else empty dict.

    Defensive: returns `{}` on missing file, unparseable JSON, or any
    I/O error. The health monitor must never crash because the engine
    snapshot is missing or malformed — it just returns a HealthReport
    with the affected checks degraded.
    """
    if path is None:
        from config import settings
        path = settings.STATE_SNAPSHOT_PATH
    p = Path(path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


# ── HealthAssessor ────────────────────────────────────────────────────


class HealthAssessor:
    """Forensic layer — runs L1/L2/L3 checks, returns HealthReport.

    Stateless — all per-call state flows via HealthInputs. The reviewer
    (11.10e) iterates strategies and calls assess() per strategy.
    """

    def assess(self, inputs: HealthInputs) -> HealthReport:
        l1 = _l1_checks(
            inputs.strategy_name,
            inputs.engine_state,
            inputs.conn,
            inputs.period_start,
            inputs.period_end,
        )
        l2 = _l2_checks(
            inputs.strategy_name,
            inputs.conn,
            inputs.period_start,
            inputs.period_end,
        )
        l3 = _l3_checks(
            inputs.strategy_name,
            inputs.envelope,
            inputs.conn,
            inputs.period_start,
            inputs.period_end,
        )

        # Per-layer status = worst status among checks at that layer.
        l1_status = _worst([c.status for c in l1])
        l2_status = _worst([c.status for c in l2])
        l3_status = _worst([c.status for c in l3])

        return HealthReport(
            strategy=inputs.strategy_name,
            period_start=inputs.period_start,
            period_end=inputs.period_end,
            l1_status=l1_status,
            l2_status=l2_status,
            l3_status=l3_status,
            checks=tuple(l1 + l2 + l3),
            # overall_status auto-computes from layers (HealthReport
            # __post_init__).
        )


def _worst(statuses: list[HealthStatus]) -> HealthStatus:
    """Worst HealthStatus from a list (HEALTHY < WATCH < DEGRADED < BROKEN)."""
    if not statuses:
        return HealthStatus.HEALTHY
    rank = {
        HealthStatus.HEALTHY: 0,
        HealthStatus.WATCH: 1,
        HealthStatus.DEGRADED: 2,
        HealthStatus.BROKEN: 3,
    }
    return max(statuses, key=rank.__getitem__)
