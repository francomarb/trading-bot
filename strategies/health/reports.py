"""
Report dataclasses for the Strategy Health & Edge Monitor.

Design references:
  - design §5 EdgeReport (the verdict layer)
  - design §6 HealthReport (the forensic layer)
  - design §3.6 per-layer verdict labels (HEALTHY/WATCH/DEGRADED/BROKEN)
  - design §11.5 recommendation taxonomy (continue / continue and monitor /
    reduce size / pause and investigate)
  - design §12.6 output format

Every report is a pure value object. Serialization to JSON is provided
for the future machine-readable twin (§F12) — v1 only emits markdown
but the dataclasses are JSON-friendly from day one.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from enum import Enum


# ── Enums ──────────────────────────────────────────────────────────────


class HealthStatus(str, Enum):
    """Per-layer (and overall HealthReport) verdict label (design §3.6).

    BROKEN is reserved for L1 / L2 — L3 (drift) cannot be BROKEN because
    drift is gradual by nature.
    """

    HEALTHY = "HEALTHY"
    WATCH = "WATCH"
    DEGRADED = "DEGRADED"
    BROKEN = "BROKEN"


class EdgeVerdict(str, Enum):
    """EdgeReport verdict (design §5.2)."""

    POSITIVE = "POSITIVE"
    NEGATIVE = "NEGATIVE"
    BELOW_BENCHMARK = "BELOW_BENCHMARK"
    UNDETERMINED = "UNDETERMINED"  # When sample is INSUFFICIENT / INDICATIVE


class Sufficiency(str, Enum):
    """Sample-size sufficiency tag (design §8).

    Operator-facing phrasing rendered by the reviewer:
      - INSUFFICIENT: "Insufficient sample — no verdict yet."
      - INDICATIVE: "Operationally healthy but statistically inconclusive."
      - CONCLUSIVE: "Statistically degraded after N trades." (NEGATIVE)
                    "Statistically confirmed working." (POSITIVE)
    """

    INSUFFICIENT = "INSUFFICIENT"
    INDICATIVE = "INDICATIVE"
    CONCLUSIVE = "CONCLUSIVE"


class Recommendation(str, Enum):
    """Operator-facing recommendation (design §11.5).

    `disable pending review` is owned by 11.11, never emitted by 11.10.
    """

    CONTINUE = "continue"
    CONTINUE_AND_MONITOR = "continue and monitor"
    REDUCE_SIZE = "reduce size"
    PAUSE_AND_INVESTIGATE = "pause and investigate"


class Layer(str, Enum):
    """Health check layer (design §6)."""

    L1 = "L1"  # Operational
    L2 = "L2"  # Execution
    L3 = "L3"  # Drift


# ── Sufficiency computation ────────────────────────────────────────────


def sufficiency_for(n: int, floor: int) -> Sufficiency:
    """Map a sample size to a sufficiency tag given a per-strategy floor.

    Thresholds per design §8:
      - INSUFFICIENT: N < 0.5 × floor
      - INDICATIVE:   0.5 × floor ≤ N < floor
      - CONCLUSIVE:   N ≥ floor

    The 0.5×floor boundary is conservative — preferred error mode is
    INSUFFICIENT (no verdict) over a premature CONCLUSIVE.
    """
    if n < 0 or floor < 1:
        raise ValueError(f"invalid sufficiency inputs: n={n}, floor={floor}")
    half_floor = floor / 2.0
    if n < half_floor:
        return Sufficiency.INSUFFICIENT
    if n < floor:
        return Sufficiency.INDICATIVE
    return Sufficiency.CONCLUSIVE


# ── Result dataclasses ─────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    """One Health check outcome.

    `findings` is operator-readable prose ("slippage p95 = 42 bps (limit
    50)"). `numeric_value` is the underlying metric for dashboard/report
    rendering. `threshold_breached` names which threshold was hit (e.g.
    "degraded_above"); empty string if HEALTHY.
    """

    name: str
    layer: Layer
    status: HealthStatus
    numeric_value: float | None = None
    threshold_breached: str = ""
    findings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class HealthReport:
    """Per-strategy health report (design §6).

    `overall_status` is the worst layer status. Per design's Edge/Health
    primacy: a BROKEN HealthReport never auto-disables a strategy.
    """

    strategy: str
    period_start: date
    period_end: date
    overall_status: HealthStatus
    l1_status: HealthStatus
    l2_status: HealthStatus
    l3_status: HealthStatus
    checks: list[CheckResult] = field(default_factory=list)


@dataclass(frozen=True)
class EdgeReport:
    """Per-strategy edge report (design §5).

    `r_expectancy` and `r_expectancy_ci_95` are the primary verdict
    inputs (sizing-invariant). Dollar metrics are secondary context.
    Envelope comparison is point estimate vs the backtest's bootstrap CI
    band — present in this report so the operator sees both.

    `negative_persistence_weeks` is the current count of consecutive
    weekly checks where the verdict has been NEGATIVE. Alarm fires only
    at 3 (design §9). Always 0 when verdict is not NEGATIVE.
    """

    strategy: str
    period_start: date
    period_end: date
    verdict: EdgeVerdict
    sufficiency: Sufficiency
    trade_count: int
    min_trades_for_verdict: int
    # R-expectancy block (primary)
    r_expectancy: float | None
    r_expectancy_ci_95: tuple[float, float] | None
    envelope_r_expectancy_ci_95: tuple[float, float] | None
    # Dollar block (secondary context)
    realized_pnl: float
    expectancy_dollars: float | None
    expectancy_dollars_ci_95: tuple[float, float] | None
    profit_factor: float | None
    win_rate: float | None
    # Capital efficiency
    sleeve_utilization: float | None
    # Benchmark comparison
    benchmark_return: float | None
    strategy_return: float | None
    alpha: float | None
    # Persistence (design §9)
    negative_persistence_weeks: int
    # Top failure reasons (used in summary table) — empty when verdict POSITIVE
    failure_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AssessmentBundle:
    """The pair the reviewer emits per strategy."""

    strategy: str
    edge: EdgeReport
    health: HealthReport
    recommendation: Recommendation


# ── JSON serialization ─────────────────────────────────────────────────


def _json_default(obj: object) -> object:
    """Custom encoder for date, Enum, tuple."""
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, tuple):
        return list(obj)
    raise TypeError(f"unserializable type: {type(obj).__name__}")


def to_json(report: object) -> str:
    """Serialize any of the report dataclasses to JSON.

    Frozen dataclasses → dict via asdict; Enums → .value; date → isoformat;
    tuples → lists. Suitable for the v1 markdown report front-matter and
    for the eventual JSON twin (§F12).
    """
    return json.dumps(asdict(report), default=_json_default, indent=2)
