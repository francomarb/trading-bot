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
    """Build a HealthReport with L2=WATCH (so overall_status auto-computes
    to WATCH per the worst-layer rule)."""
    return HealthReport(
        strategy="donchian_breakout",
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 25),
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
        # overall_status is auto-computed to WATCH because l2_status=WATCH.
        assert parsed["overall_status"] == "WATCH"
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
        # _make_health_report has l2_status=WATCH so overall_status
        # auto-computes to WATCH (worst-layer rule).
        assert parsed["health"]["overall_status"] == "WATCH"

    def test_overall_status_in_json_reflects_computed_value(self):
        """Even if a downstream consumer reads only `overall_status`, it
        sees the worst-layer value — never a contradictory underestimate.
        This is the design contract the §1.2 invariant relies on."""
        report = HealthReport(
            strategy="x",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 8),
            l1_status=HealthStatus.HEALTHY,
            l2_status=HealthStatus.DEGRADED,  # worst
            l3_status=HealthStatus.WATCH,
        )
        parsed = json.loads(to_json(report))
        assert parsed["overall_status"] == "DEGRADED"

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


# ── Reviewer-driven invariants (PR #16 second pass) ───────────────────


class TestOverallStatusAutoComputed:
    """`HealthReport.overall_status` is computed from the worst layer
    status — `init=False` — so no caller can pass a value that
    contradicts the layer fields. Closes the dashboard-understatement
    hole the reviewer flagged."""

    def test_overall_is_worst_layer(self):
        r = HealthReport(
            strategy="x",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 8),
            l1_status=HealthStatus.HEALTHY,
            l2_status=HealthStatus.DEGRADED,
            l3_status=HealthStatus.WATCH,
        )
        assert r.overall_status == HealthStatus.DEGRADED

    def test_all_healthy_means_overall_healthy(self):
        r = HealthReport(
            strategy="x",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 8),
            l1_status=HealthStatus.HEALTHY,
            l2_status=HealthStatus.HEALTHY,
            l3_status=HealthStatus.HEALTHY,
        )
        assert r.overall_status == HealthStatus.HEALTHY

    def test_l2_broken_promotes_overall_to_broken(self):
        r = HealthReport(
            strategy="x",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 8),
            l1_status=HealthStatus.WATCH,
            l2_status=HealthStatus.BROKEN,
            l3_status=HealthStatus.HEALTHY,
        )
        assert r.overall_status == HealthStatus.BROKEN

    def test_overall_status_not_a_constructor_kwarg(self):
        """`overall_status` is init=False — passing it raises TypeError.
        This is the contract that prevents contradictory states."""
        with pytest.raises(TypeError):
            HealthReport(
                strategy="x",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 8),
                l1_status=HealthStatus.HEALTHY,
                l2_status=HealthStatus.HEALTHY,
                l3_status=HealthStatus.HEALTHY,
                overall_status=HealthStatus.BROKEN,  # type: ignore[call-arg]
            )


class TestL3BrokenRejected:
    """Design §3.6 invariant: L3 drift cannot be BROKEN — enforced at
    construction time on both CheckResult and HealthReport."""

    def test_check_result_l3_broken_raises(self):
        with pytest.raises(ValueError, match="L3 .* cannot be BROKEN"):
            CheckResult(
                name="trade_frequency_drift_pct",
                layer=Layer.L3,
                status=HealthStatus.BROKEN,
            )

    def test_check_result_l3_degraded_allowed(self):
        # DEGRADED on L3 is legal — only BROKEN is rejected.
        cr = CheckResult(
            name="trade_frequency_drift_pct",
            layer=Layer.L3,
            status=HealthStatus.DEGRADED,
        )
        assert cr.status == HealthStatus.DEGRADED

    def test_check_result_l1_l2_broken_allowed(self):
        # L1/L2 can be BROKEN — operational/execution failures are
        # legitimately "broken until fixed".
        for layer in (Layer.L1, Layer.L2):
            cr = CheckResult(
                name="some_check", layer=layer, status=HealthStatus.BROKEN
            )
            assert cr.status == HealthStatus.BROKEN

    def test_health_report_l3_broken_raises(self):
        with pytest.raises(ValueError, match="l3_status cannot be BROKEN"):
            HealthReport(
                strategy="x",
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 8),
                l1_status=HealthStatus.HEALTHY,
                l2_status=HealthStatus.HEALTHY,
                l3_status=HealthStatus.BROKEN,
            )


class TestFrozenCollectionsAreImmutable:
    """Frozen dataclasses must not silently expose mutable list state.
    Mutations would propagate to JSON output and markdown rendering."""

    def test_check_result_findings_is_tuple(self):
        cr = CheckResult(
            name="x",
            layer=Layer.L1,
            status=HealthStatus.HEALTHY,
            findings=["a", "b"],
        )
        # List input → stored as tuple. `.append` is not available.
        assert isinstance(cr.findings, tuple)
        with pytest.raises(AttributeError):
            cr.findings.append("c")  # type: ignore[attr-defined]

    def test_health_report_checks_is_tuple(self):
        cr = CheckResult(name="x", layer=Layer.L1, status=HealthStatus.HEALTHY)
        r = HealthReport(
            strategy="x",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 8),
            l1_status=HealthStatus.HEALTHY,
            l2_status=HealthStatus.HEALTHY,
            l3_status=HealthStatus.HEALTHY,
            checks=[cr],
        )
        assert isinstance(r.checks, tuple)
        with pytest.raises(AttributeError):
            r.checks.append(cr)  # type: ignore[attr-defined]

    def test_edge_report_failure_reasons_is_tuple(self):
        report = EdgeReport(
            strategy="x",
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 8),
            verdict=EdgeVerdict.NEGATIVE,
            sufficiency=Sufficiency.CONCLUSIVE,
            trade_count=60,
            min_trades_for_verdict=50,
            r_expectancy=-0.1,
            r_expectancy_ci_95=(-0.3, -0.01),
            envelope_r_expectancy_ci_95=(0.2, 0.7),
            realized_pnl=-500.0,
            expectancy_dollars=-8.0,
            expectancy_dollars_ci_95=(-25.0, -1.0),
            profit_factor=0.75,
            win_rate=0.40,
            sleeve_utilization=0.65,
            benchmark_return=0.04,
            strategy_return=-0.01,
            alpha=-0.05,
            negative_persistence_weeks=3,
            failure_reasons=["expectancy CI below zero", "EMA50<EMA100"],
        )
        assert isinstance(report.failure_reasons, tuple)
        with pytest.raises(AttributeError):
            report.failure_reasons.append("x")  # type: ignore[attr-defined]

    def test_passing_tuple_directly_works(self):
        cr = CheckResult(
            name="x",
            layer=Layer.L1,
            status=HealthStatus.HEALTHY,
            findings=("a", "b"),
        )
        assert cr.findings == ("a", "b")
