"""
Unit tests for strategies/health/reports.py.

Coverage:
  - sufficiency_for: every boundary case (n=0, n=floor/2-1, n=floor/2,
    n=floor-1, n=floor, n>>floor); input validation.
  - Dataclass construction: HealthReport / EdgeReport / CheckResult /
    AssessmentBundle round-trip.
  - JSON serializer: handles date, Enum, tuple; output is valid JSON;
    every dataclass type serializes without error.
  - Enum string values match the design's operator-facing strings
    (so the markdown reviewer can render them verbatim).
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from strategies.health.reports import (
    AssessmentBundle,
    CheckResult,
    EdgeReport,
    EdgeVerdict,
    HealthReport,
    HealthStatus,
    Layer,
    Recommendation,
    Sufficiency,
    sufficiency_for,
    to_json,
)


# ── sufficiency_for ───────────────────────────────────────────────────


class TestSufficiencyFor:
    def test_zero_is_insufficient(self):
        assert sufficiency_for(0, 50) == Sufficiency.INSUFFICIENT

    def test_below_half_floor_is_insufficient(self):
        # floor=50 → half-floor=25 → n=24 is INSUFFICIENT
        assert sufficiency_for(24, 50) == Sufficiency.INSUFFICIENT

    def test_at_half_floor_is_indicative(self):
        # n=25, floor=50 → at boundary, INDICATIVE
        assert sufficiency_for(25, 50) == Sufficiency.INDICATIVE

    def test_below_floor_is_indicative(self):
        assert sufficiency_for(49, 50) == Sufficiency.INDICATIVE

    def test_at_floor_is_conclusive(self):
        assert sufficiency_for(50, 50) == Sufficiency.CONCLUSIVE

    def test_above_floor_is_conclusive(self):
        assert sufficiency_for(1000, 50) == Sufficiency.CONCLUSIVE

    def test_negative_n_raises(self):
        with pytest.raises(ValueError):
            sufficiency_for(-1, 50)

    def test_zero_floor_raises(self):
        with pytest.raises(ValueError):
            sufficiency_for(10, 0)

    def test_negative_floor_raises(self):
        with pytest.raises(ValueError):
            sufficiency_for(10, -1)

    def test_low_floor_boundaries(self):
        # floor=2 → half=1.0 → n=0 INSUFF, n=1 INDIC, n=2 CONCL
        assert sufficiency_for(0, 2) == Sufficiency.INSUFFICIENT
        assert sufficiency_for(1, 2) == Sufficiency.INDICATIVE
        assert sufficiency_for(2, 2) == Sufficiency.CONCLUSIVE


# ── Enum values (design contract) ─────────────────────────────────────


class TestEnumValues:
    def test_recommendation_strings_match_design(self):
        """Markdown reviewer renders these strings verbatim — they
        must match design §11.5 wording exactly."""
        assert Recommendation.CONTINUE.value == "continue"
        assert Recommendation.CONTINUE_AND_MONITOR.value == "continue and monitor"
        assert Recommendation.REDUCE_SIZE.value == "reduce size"
        assert Recommendation.PAUSE_AND_INVESTIGATE.value == "pause and investigate"

    def test_health_status_strings(self):
        for s in HealthStatus:
            # Design §3.6: HEALTHY / WATCH / DEGRADED / BROKEN
            assert s.value in ("HEALTHY", "WATCH", "DEGRADED", "BROKEN")

    def test_edge_verdict_strings(self):
        for v in EdgeVerdict:
            assert v.value in ("POSITIVE", "NEGATIVE", "BELOW_BENCHMARK", "UNDETERMINED")

    def test_sufficiency_strings(self):
        for s in Sufficiency:
            assert s.value in ("INSUFFICIENT", "INDICATIVE", "CONCLUSIVE")

    def test_layer_strings(self):
        assert Layer.L1.value == "L1"
        assert Layer.L2.value == "L2"
        assert Layer.L3.value == "L3"


# ── Dataclass construction ────────────────────────────────────────────


def _make_health_report() -> HealthReport:
    return HealthReport(
        strategy="donchian_breakout",
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 25),
        overall_status=HealthStatus.HEALTHY,
        l1_status=HealthStatus.HEALTHY,
        l2_status=HealthStatus.WATCH,
        l3_status=HealthStatus.HEALTHY,
        checks=[
            CheckResult(
                name="slippage_realized_vs_modeled_bps_p95",
                layer=Layer.L2,
                status=HealthStatus.WATCH,
                numeric_value=27.0,
                threshold_breached="watch_above",
                findings=["slippage p95 = 27 bps (watch threshold 20)"],
            ),
        ],
    )


def _make_edge_report() -> EdgeReport:
    return EdgeReport(
        strategy="donchian_breakout",
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 25),
        verdict=EdgeVerdict.POSITIVE,
        sufficiency=Sufficiency.CONCLUSIVE,
        trade_count=58,
        min_trades_for_verdict=50,
        r_expectancy=0.42,
        r_expectancy_ci_95=(0.18, 0.65),
        envelope_r_expectancy_ci_95=(0.20, 0.70),
        realized_pnl=2_412.0,
        expectancy_dollars=41.6,
        expectancy_dollars_ci_95=(18.0, 65.0),
        profit_factor=1.62,
        win_rate=0.48,
        sleeve_utilization=0.71,
        benchmark_return=0.04,
        strategy_return=0.06,
        alpha=0.02,
        negative_persistence_weeks=0,
        failure_reasons=[],
    )


class TestDataclassConstruction:
    def test_health_report_immutable(self):
        report = _make_health_report()
        with pytest.raises(Exception):  # FrozenInstanceError
            report.strategy = "other"  # type: ignore[misc]

    def test_edge_report_immutable(self):
        report = _make_edge_report()
        with pytest.raises(Exception):
            report.verdict = EdgeVerdict.NEGATIVE  # type: ignore[misc]

    def test_check_result_immutable(self):
        cr = CheckResult(name="x", layer=Layer.L1, status=HealthStatus.HEALTHY)
        with pytest.raises(Exception):
            cr.status = HealthStatus.BROKEN  # type: ignore[misc]

    def test_assessment_bundle_holds_both(self):
        bundle = AssessmentBundle(
            strategy="donchian_breakout",
            edge=_make_edge_report(),
            health=_make_health_report(),
            recommendation=Recommendation.CONTINUE,
        )
        assert bundle.recommendation == Recommendation.CONTINUE
        assert bundle.edge.strategy == bundle.health.strategy

    def test_edge_report_optional_fields_accept_none(self):
        """Strategies with no envelope, no benchmark, no R data still
        produce a valid EdgeReport — `verdict=UNDETERMINED`."""
        report = EdgeReport(
            strategy="brand_new_strategy",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            verdict=EdgeVerdict.UNDETERMINED,
            sufficiency=Sufficiency.INSUFFICIENT,
            trade_count=3,
            min_trades_for_verdict=50,
            r_expectancy=None,
            r_expectancy_ci_95=None,
            envelope_r_expectancy_ci_95=None,
            realized_pnl=0.0,
            expectancy_dollars=None,
            expectancy_dollars_ci_95=None,
            profit_factor=None,
            win_rate=None,
            sleeve_utilization=None,
            benchmark_return=None,
            strategy_return=None,
            alpha=None,
            negative_persistence_weeks=0,
        )
        assert report.verdict == EdgeVerdict.UNDETERMINED


# ── JSON serialization ────────────────────────────────────────────────


class TestToJson:
    def test_health_report_round_trip(self):
        report = _make_health_report()
        text = to_json(report)
        parsed = json.loads(text)
        assert parsed["strategy"] == "donchian_breakout"
        assert parsed["overall_status"] == "HEALTHY"
        assert parsed["period_start"] == "2026-05-18"
        assert parsed["checks"][0]["layer"] == "L2"
        assert parsed["checks"][0]["status"] == "WATCH"

    def test_edge_report_round_trip(self):
        report = _make_edge_report()
        text = to_json(report)
        parsed = json.loads(text)
        assert parsed["verdict"] == "POSITIVE"
        assert parsed["sufficiency"] == "CONCLUSIVE"
        # Tuple → list per the encoder
        assert parsed["r_expectancy_ci_95"] == [0.18, 0.65]

    def test_assessment_bundle_round_trip(self):
        bundle = AssessmentBundle(
            strategy="donchian_breakout",
            edge=_make_edge_report(),
            health=_make_health_report(),
            recommendation=Recommendation.CONTINUE,
        )
        text = to_json(bundle)
        parsed = json.loads(text)
        assert parsed["recommendation"] == "continue"
        assert parsed["edge"]["verdict"] == "POSITIVE"
        assert parsed["health"]["overall_status"] == "HEALTHY"

    def test_none_optional_fields_serialize_as_null(self):
        report = EdgeReport(
            strategy="x",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 8),
            verdict=EdgeVerdict.UNDETERMINED,
            sufficiency=Sufficiency.INSUFFICIENT,
            trade_count=0,
            min_trades_for_verdict=50,
            r_expectancy=None,
            r_expectancy_ci_95=None,
            envelope_r_expectancy_ci_95=None,
            realized_pnl=0.0,
            expectancy_dollars=None,
            expectancy_dollars_ci_95=None,
            profit_factor=None,
            win_rate=None,
            sleeve_utilization=None,
            benchmark_return=None,
            strategy_return=None,
            alpha=None,
            negative_persistence_weeks=0,
        )
        text = to_json(report)
        parsed = json.loads(text)
        assert parsed["r_expectancy"] is None
        assert parsed["r_expectancy_ci_95"] is None
