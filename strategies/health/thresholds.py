"""
Per-strategy inline Health-check thresholds.

Defaults err toward WATCH (noisy but harmless) rather than BROKEN
(cries wolf). Mis-tuned thresholds cause dashboard noise, not capital
action, so the v1 invariant (advisory only) keeps this safe.

After 4+ weeks of paper operation, the operator runs
`scripts/calibrate_health_thresholds.py` and adjusts these values
inline. See PLAN.md 11.10h.

Thresholds are inline per strategy (design §8.1) — no archetype
abstraction in v1. Refactor to `HealthThresholdProfile` is follow-up
§F8, triggered when strategy count grows past ~8 or when duplication
becomes painful.

Structure: `THRESHOLDS["strategy_name"]["check_name"]` returns a
`CheckThresholds` tuple. Helper `get_thresholds(strategy, check)`
falls back to `_DEFAULTS` when no per-strategy override exists.
"""

from __future__ import annotations

from dataclasses import dataclass


# ── Threshold tuple ────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckThresholds:
    """Three-tier health-check thresholds (design §6, §3.6).

    Semantics depend on the check direction:
      - "above" checks (slippage bps, latency ms): values *above* the
        threshold are bad. status = HEALTHY if x < watch, WATCH if
        watch ≤ x < degraded, DEGRADED if degraded ≤ x < broken,
        BROKEN if x ≥ broken.
      - "below" checks (fill rate): values *below* the threshold are
        bad. Use `direction="below"` and the thresholds are floors:
        HEALTHY if x ≥ watch, WATCH if degraded ≤ x < watch, etc.

    None for `broken` means the check tops out at DEGRADED — no BROKEN
    state possible (e.g. L3 drift checks which cannot be BROKEN per
    design §3.6).
    """

    watch: float
    degraded: float
    broken: float | None
    direction: str = "above"  # "above" | "below"

    def __post_init__(self) -> None:
        if self.direction not in ("above", "below"):
            raise ValueError(f"direction must be 'above' or 'below', got {self.direction!r}")


# ── Default thresholds (fallback when no per-strategy override) ───────
# TODO(11.10h): calibrate after 4 weeks of paper operation.

_DEFAULTS: dict[str, CheckThresholds] = {
    # ── L1 Operational ────────────────────────────────────────────
    # TODO(11.10h): calibrate stream_disconnects against observed paper rate
    "stream_disconnects_per_day": CheckThresholds(watch=1, degraded=3, broken=5),
    # TODO(11.10h): calibrate stale_data — depends on bar timeframe
    "stale_data_max_bar_age_minutes": CheckThresholds(watch=5, degraded=15, broken=30),
    "reconciliation_mismatches_24h": CheckThresholds(watch=1, degraded=3, broken=5),
    "missing_stop_repairs_24h": CheckThresholds(watch=0, degraded=1, broken=3),
    "external_close_rate": CheckThresholds(watch=0.05, degraded=0.15, broken=0.30),
    # Cycle latency is L1 too — latency degradation is operational
    "cycle_latency_p95_ms": CheckThresholds(watch=500, degraded=2_000, broken=10_000),

    # ── L2 Execution ──────────────────────────────────────────────
    # TODO(11.10h): calibrate slippage thresholds per strategy — RSI limits
    # should be tight; SMA/Donchian market orders permit more drift.
    "slippage_realized_vs_modeled_bps_p95": CheckThresholds(watch=20, degraded=50, broken=100),
    "order_rejection_rate": CheckThresholds(watch=0.02, degraded=0.05, broken=0.10),
    "timeout_cancel_rate": CheckThresholds(watch=0.05, degraded=0.15, broken=0.30),
    # Fill rate is "below" direction — low fill rate is bad
    "fill_rate": CheckThresholds(watch=0.70, degraded=0.50, broken=0.30, direction="below"),
    "partial_fill_rate": CheckThresholds(watch=0.05, degraded=0.15, broken=0.30),
    # TODO(11.10h): options spread realized vs picked — calibrate post 11.26
    "options_spread_realized_vs_picked_pct": CheckThresholds(
        watch=0.10, degraded=0.25, broken=0.50
    ),

    # ── L3 Drift ──────────────────────────────────────────────────
    # L3 cannot be BROKEN per design §3.6 — `broken=None`.
    # All are "drift from envelope" — large positive distance is bad.
    "trade_frequency_drift_pct": CheckThresholds(watch=0.30, degraded=0.60, broken=None),
    "hold_time_drift_ks_pvalue": CheckThresholds(
        watch=0.05, degraded=0.01, broken=None, direction="below"
    ),
    "edge_filter_block_rate_drift_pct": CheckThresholds(watch=0.20, degraded=0.50, broken=None),
    "regime_block_rate_drift_pct": CheckThresholds(watch=0.20, degraded=0.50, broken=None),
    "fill_rate_drift_pct": CheckThresholds(watch=0.20, degraded=0.50, broken=None),
}


# ── Per-strategy overrides ─────────────────────────────────────────────
# Empty for v1 — defaults apply across all strategies. Operator adds
# overrides here as calibration data accrues (11.10h).
# Example:
#   THRESHOLDS = {
#       "rsi_reversion": {
#           "fill_rate": CheckThresholds(watch=0.85, degraded=0.70, broken=0.50, direction="below"),
#       },
#   }

THRESHOLDS: dict[str, dict[str, CheckThresholds]] = {}


# ── Lookup helper ──────────────────────────────────────────────────────


def get_thresholds(strategy: str, check: str) -> CheckThresholds:
    """Return per-strategy thresholds, falling back to defaults.

    Raises KeyError if `check` is unknown (typo guard — better than
    silently returning a default for a misspelled check name).
    """
    per_strategy = THRESHOLDS.get(strategy, {})
    if check in per_strategy:
        return per_strategy[check]
    if check not in _DEFAULTS:
        raise KeyError(
            f"unknown health check: {check!r}. "
            f"Add a default in strategies/health/thresholds.py:_DEFAULTS."
        )
    return _DEFAULTS[check]


def list_known_checks() -> list[str]:
    """All known check names (the union of defaults + per-strategy keys)."""
    names: set[str] = set(_DEFAULTS.keys())
    for per_strategy in THRESHOLDS.values():
        names.update(per_strategy.keys())
    return sorted(names)
