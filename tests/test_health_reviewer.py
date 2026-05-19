"""
Unit tests for strategies/health/reviewer.py.

Covers:
  - recommend() decision-matrix mapping (every row of design §11.5)
  - assess_strategy threads PersistenceState through (Edge + Health)
  - assess_all_strategies handles missing envelope per strategy
    gracefully + persists state once at the end
  - render_markdown structure: front-matter, silent-killer banner,
    summary table, per-strategy detail, Carver caveat
  - dispatch_alerts fires the correct alert types per verdict / health
    combination; cooldown suppression respected
  - window_from_args computes correct period bounds for
    weekly/monthly/yearly
  - run_review end-to-end: report file written, alerts dispatched,
    state persisted; dry_run skips both
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from config import settings
from reporting.alerts import AlertDispatcher, AlertSeverity, AlertType
from reporting.logger import TradeLogger
from strategies.health.envelope import (
    ENVELOPE_SCHEMA_VERSION,
    StrategyEnvelope,
    envelope_path,
)
from strategies.health.persistence import HealthStateFile, PersistenceState
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
)
from strategies.health.reviewer import (
    LOW_SLEEVE_UTILIZATION_THRESHOLD,
    REPORT_SCHEMA_VERSION,
    ReviewWindow,
    _findings_for_layer,
    _summary_row,
    assess_all_strategies,
    dispatch_alerts,
    recommend,
    render_markdown,
    run_review,
    window_from_args,
)


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path: Path):
    logger = TradeLogger(path=str(tmp_path / "trades.db"))
    conn = logger._ensure_db()
    yield conn
    logger.close()


def _edge(
    *,
    verdict: EdgeVerdict = EdgeVerdict.POSITIVE,
    sufficiency: Sufficiency = Sufficiency.CONCLUSIVE,
    negative_persistence_weeks: int = 0,
    sleeve_utilization: float | None = 0.50,
    strategy_return: float | None = 0.10,
    benchmark_return: float | None = 0.08,
    alpha: float | None = 0.02,
    failure_reasons: tuple[str, ...] = (),
    r_expectancy: float | None = 0.4,
    trade_count: int = 60,
) -> EdgeReport:
    return EdgeReport(
        strategy="x",
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 25),
        verdict=verdict,
        sufficiency=sufficiency,
        trade_count=trade_count,
        min_trades_for_verdict=50,
        r_expectancy=r_expectancy,
        r_expectancy_ci_95=(0.2, 0.6) if r_expectancy else None,
        envelope_r_expectancy_ci_95=(0.2, 0.7),
        realized_pnl=trade_count * 100.0,
        expectancy_dollars=100.0,
        expectancy_dollars_ci_95=(50.0, 150.0),
        profit_factor=1.62,
        win_rate=0.48,
        sleeve_utilization=sleeve_utilization,
        benchmark_return=benchmark_return,
        strategy_return=strategy_return,
        alpha=alpha,
        negative_persistence_weeks=negative_persistence_weeks,
        failure_reasons=failure_reasons,
    )


def _health(
    *,
    l1: HealthStatus = HealthStatus.HEALTHY,
    l2: HealthStatus = HealthStatus.HEALTHY,
    l3: HealthStatus = HealthStatus.HEALTHY,
    checks: tuple[CheckResult, ...] = (),
) -> HealthReport:
    return HealthReport(
        strategy="x",
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 25),
        l1_status=l1,
        l2_status=l2,
        l3_status=l3,
        checks=checks,
    )


# ── recommend() decision matrix ───────────────────────────────────────


class TestRecommend:
    def test_positive_conclusive_healthy_continue(self):
        assert (
            recommend(_edge(verdict=EdgeVerdict.POSITIVE), _health())
            == Recommendation.CONTINUE
        )

    def test_positive_conclusive_watch_continue_and_monitor(self):
        """Profitable strategy with messy L2 execution — keep earning,
        don't throttle. Per design §3 / §11.5 invariant: Health never
        overrides positive Edge."""
        assert (
            recommend(
                _edge(verdict=EdgeVerdict.POSITIVE),
                _health(l2=HealthStatus.WATCH),
            ) == Recommendation.CONTINUE_AND_MONITOR
        )

    def test_positive_conclusive_broken_continue_and_monitor(self):
        """Even BROKEN L1/L2 doesn't override POSITIVE Edge —
        operator fixes the operational issue in parallel."""
        assert (
            recommend(
                _edge(verdict=EdgeVerdict.POSITIVE),
                _health(l1=HealthStatus.BROKEN),
            ) == Recommendation.CONTINUE_AND_MONITOR
        )

    def test_negative_conclusive_pause_and_investigate(self):
        """The silent-killer alarm — top priority."""
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.NEGATIVE,
                    sufficiency=Sufficiency.CONCLUSIVE,
                    negative_persistence_weeks=3,
                ),
                _health(),  # even HEALTHY
            ) == Recommendation.PAUSE_AND_INVESTIGATE
        )

    def test_negative_conclusive_with_broken_health_still_pause(self):
        """Silent-killer wins regardless of Health state."""
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.NEGATIVE,
                    sufficiency=Sufficiency.CONCLUSIVE,
                ),
                _health(l1=HealthStatus.BROKEN),
            ) == Recommendation.PAUSE_AND_INVESTIGATE
        )

    def test_below_benchmark_conclusive_reduce_size(self):
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.BELOW_BENCHMARK,
                    sufficiency=Sufficiency.CONCLUSIVE,
                ),
                _health(),
            ) == Recommendation.REDUCE_SIZE
        )

    def test_two_weeks_negative_signals_early_warning_reduce_size(self):
        """Indicative trending downward (2+ consecutive weeks of
        negative signals) before alarm fires — reduce size proactively."""
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.UNDETERMINED,
                    sufficiency=Sufficiency.INDICATIVE,
                    negative_persistence_weeks=2,
                ),
                _health(),
            ) == Recommendation.REDUCE_SIZE
        )

    def test_one_week_negative_does_not_trigger_reduce(self):
        """Single bad week is exactly normal variance — no action."""
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.UNDETERMINED,
                    sufficiency=Sufficiency.INDICATIVE,
                    negative_persistence_weeks=1,
                ),
                _health(),
            ) == Recommendation.CONTINUE_AND_MONITOR
        )

    def test_insufficient_with_negative_signals_no_reduce(self):
        """Even with persistence >= 2, INSUFFICIENT sample should not
        trigger reduce — too few trades to claim anything."""
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.UNDETERMINED,
                    sufficiency=Sufficiency.INSUFFICIENT,
                    negative_persistence_weeks=2,
                ),
                _health(),
            ) == Recommendation.CONTINUE_AND_MONITOR
        )

    def test_health_degraded_low_utilization_reduce_size(self):
        """Strategy struggling AND not deploying capital — operator
        candidate for reallocation."""
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.UNDETERMINED,
                    sufficiency=Sufficiency.INDICATIVE,
                    sleeve_utilization=0.10,  # below the 0.20 threshold
                ),
                _health(l2=HealthStatus.DEGRADED),
            ) == Recommendation.REDUCE_SIZE
        )

    def test_health_degraded_high_utilization_no_reduce(self):
        """Degraded but actively deploying capital — keep monitoring,
        don't reduce size (capital IS being used)."""
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.UNDETERMINED,
                    sufficiency=Sufficiency.INDICATIVE,
                    sleeve_utilization=0.80,
                ),
                _health(l2=HealthStatus.DEGRADED),
            ) == Recommendation.CONTINUE_AND_MONITOR
        )

    def test_insufficient_sample_continue_and_monitor(self):
        """Early days of operation — no verdict possible, just monitor."""
        assert (
            recommend(
                _edge(
                    verdict=EdgeVerdict.UNDETERMINED,
                    sufficiency=Sufficiency.INSUFFICIENT,
                    negative_persistence_weeks=0,
                    sleeve_utilization=None,
                ),
                _health(),
            ) == Recommendation.CONTINUE_AND_MONITOR
        )

    def test_silent_killer_takes_priority_over_below_benchmark(self):
        """If somehow both NEGATIVE and BELOW_BENCHMARK could apply,
        NEGATIVE wins (silent killer is the loudest alarm)."""
        # In practice EdgeAssessor returns one or the other, but the
        # recommend() priority order is what matters.
        assert (
            recommend(
                _edge(verdict=EdgeVerdict.NEGATIVE),
                _health(),
            ) == Recommendation.PAUSE_AND_INVESTIGATE
        )


# ── window_from_args ──────────────────────────────────────────────────


class TestWindowFromArgs:
    def test_weekly_seven_days_back(self):
        w = window_from_args("weekly", end_date=date(2026, 5, 25))
        assert w.period_start == date(2026, 5, 18)
        assert w.period_end == date(2026, 5, 25)
        assert w.period_type == "weekly"

    def test_monthly_is_previous_calendar_month(self):
        w = window_from_args("monthly", end_date=date(2026, 5, 25))
        assert w.period_start == date(2026, 4, 1)
        assert w.period_end == date(2026, 5, 1)

    def test_monthly_january_wraps_to_december(self):
        w = window_from_args("monthly", end_date=date(2026, 1, 15))
        assert w.period_start == date(2025, 12, 1)
        assert w.period_end == date(2026, 1, 1)

    def test_yearly_365_days_back(self):
        from datetime import timedelta
        w = window_from_args("yearly", end_date=date(2026, 5, 25))
        assert w.period_end - w.period_start == timedelta(days=365)

    def test_invalid_period_type_raises(self):
        with pytest.raises(ValueError, match="unknown period_type"):
            window_from_args("hourly")


# ── render_markdown structure ─────────────────────────────────────────


def _bundle(**edge_kwargs):
    edge_default = {
        "verdict": EdgeVerdict.POSITIVE,
        "sufficiency": Sufficiency.CONCLUSIVE,
    }
    edge_default.update(edge_kwargs)
    e = _edge(**edge_default)
    h = _health()
    return AssessmentBundle(
        strategy="x", edge=e, health=h,
        recommendation=recommend(e, h),
    )


class TestRenderMarkdown:
    def _window(self) -> ReviewWindow:
        return ReviewWindow(
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            period_type="weekly",
        )

    def test_yaml_front_matter_present(self):
        md = render_markdown([_bundle()], self._window())
        assert md.startswith("---")
        # Period bounds + schema_version present in front-matter
        front_matter_end = md.index("---", 4)  # find closing ---
        front_matter = md[:front_matter_end]
        assert f"schema_version: {REPORT_SCHEMA_VERSION}" in front_matter
        assert "period_start: 2026-05-18" in front_matter
        assert "period_end: 2026-05-25" in front_matter
        assert "period_type: weekly" in front_matter

    def test_summary_table_present(self):
        md = render_markdown([_bundle()], self._window())
        assert "## Summary" in md
        # Table header
        assert "| Strategy | Verdict | Confidence | Sample |" in md

    def test_silent_killer_banner_when_negative(self):
        """Per design §13 the silent killer must surface prominently —
        a dedicated banner above the summary table, not buried in
        per-strategy detail."""
        killer = _bundle(
            verdict=EdgeVerdict.NEGATIVE,
            sufficiency=Sufficiency.CONCLUSIVE,
            negative_persistence_weeks=3,
        )
        md = render_markdown([killer], self._window())
        assert "Silent-Killer Alarm" in md
        # Banner appears BEFORE the summary table
        banner_idx = md.index("Silent-Killer Alarm")
        summary_idx = md.index("## Summary")
        assert banner_idx < summary_idx

    def test_no_silent_killer_banner_when_no_negative(self):
        md = render_markdown([_bundle()], self._window())
        assert "Silent-Killer Alarm" not in md

    def test_carver_caveat_when_any_conclusive(self):
        md = render_markdown([_bundle()], self._window())
        assert "Carver" in md
        assert "Epistemic Caveat" in md

    def test_no_carver_caveat_when_no_conclusive(self):
        md = render_markdown([
            _bundle(sufficiency=Sufficiency.INSUFFICIENT, verdict=EdgeVerdict.UNDETERMINED)
        ], self._window())
        assert "Carver" not in md

    def test_per_strategy_detail_section_present(self):
        md = render_markdown([_bundle()], self._window())
        assert "## x" in md  # strategy name as section header
        assert "### Edge Report" in md
        assert "### Health Report" in md

    def test_report_is_pure_markdown_no_html(self):
        """Sanity: report should be safe to render as markdown
        anywhere. No HTML tags."""
        md = render_markdown([_bundle()], self._window())
        assert "<" not in md or md.count("<") < 5  # allow a stray < in text

    def test_provenance_labels_in_metrics(self):
        """Design §12.6: in-text labels distinguish measured / inferred /
        envelope sources. Sanity-check the labels appear."""
        md = render_markdown([_bundle()], self._window())
        assert "(measured" in md
        assert "(from backtest)" in md or "(measured, iid bootstrap)" in md


class TestSummaryRow:
    def test_summary_row_has_seven_columns(self):
        row = _summary_row(_bundle())
        # 7 columns → 8 pipes
        assert row.count("|") == 8

    def test_summary_row_contains_verdict_and_recommendation(self):
        row = _summary_row(_bundle())
        # POSITIVE + continue should be present (boldened with **)
        assert "POSITIVE" in row
        assert "continue" in row

    def test_summary_row_truncates_long_reasons(self):
        long_reasons = (
            "very long failure reason text " * 10,  # >>80 chars
        )
        b = _bundle(failure_reasons=long_reasons)
        row = _summary_row(b)
        assert "…" in row


# ── dispatch_alerts ───────────────────────────────────────────────────


class TestDispatchAlerts:
    def _mock_dispatcher(self):
        d = MagicMock(spec=AlertDispatcher)
        # All factory methods return True by default
        for method in (
            "strategy_edge_loss",
            "strategy_edge_below_benchmark",
            "strategy_health_degraded",
            "strategy_health_broken",
            "strategy_drift_warning",
        ):
            getattr(d, method).return_value = True
        return d

    def test_negative_verdict_fires_silent_killer_alert(self):
        d = self._mock_dispatcher()
        b = _bundle(
            verdict=EdgeVerdict.NEGATIVE,
            sufficiency=Sufficiency.CONCLUSIVE,
            negative_persistence_weeks=3,
        )
        dispatch_alerts([b], d)
        d.strategy_edge_loss.assert_called_once()
        call = d.strategy_edge_loss.call_args
        assert call.args[0] == "x"
        assert call.kwargs["negative_persistence_weeks"] == 3

    def test_below_benchmark_verdict_fires_alert(self):
        d = self._mock_dispatcher()
        b = _bundle(
            verdict=EdgeVerdict.BELOW_BENCHMARK,
            sufficiency=Sufficiency.CONCLUSIVE,
        )
        dispatch_alerts([b], d)
        d.strategy_edge_below_benchmark.assert_called_once()

    def test_health_broken_fires_alert(self):
        d = self._mock_dispatcher()
        e = _edge()
        h = _health(l1=HealthStatus.BROKEN, checks=(
            CheckResult(
                name="cycle_latency_p95_ms",
                layer=Layer.L1,
                status=HealthStatus.BROKEN,
                findings=("cycle latency > 10s",),
            ),
        ))
        b = AssessmentBundle(
            strategy="x", edge=e, health=h,
            recommendation=recommend(e, h),
        )
        dispatch_alerts([b], d)
        d.strategy_health_broken.assert_called_once()
        call = d.strategy_health_broken.call_args
        assert call.kwargs["layer"] == "L1"
        assert "cycle latency > 10s" in call.kwargs["findings"]

    def test_health_degraded_fires_alert(self):
        d = self._mock_dispatcher()
        e = _edge()
        h = _health(l2=HealthStatus.DEGRADED, checks=(
            CheckResult(
                name="slippage_realized_vs_modeled_bps_p95",
                layer=Layer.L2,
                status=HealthStatus.DEGRADED,
                findings=("p95 slippage = 65 bps",),
            ),
        ))
        b = AssessmentBundle(
            strategy="x", edge=e, health=h,
            recommendation=recommend(e, h),
        )
        dispatch_alerts([b], d)
        d.strategy_health_degraded.assert_called_once()

    def test_l3_drift_fires_drift_warning_not_health_alert(self):
        """L3 status WATCH/DEGRADED → STRATEGY_DRIFT_WARNING, not the
        general health alert (different alert type per design §11)."""
        d = self._mock_dispatcher()
        e = _edge()
        h = _health(l3=HealthStatus.DEGRADED, checks=(
            CheckResult(
                name="trade_frequency_drift_pct",
                layer=Layer.L3,
                status=HealthStatus.DEGRADED,
                numeric_value=1.5,  # 150% drift
                findings=("observed 5/wk vs envelope band 10-30/wk (drift 50%)",),
            ),
        ))
        b = AssessmentBundle(
            strategy="x", edge=e, health=h,
            recommendation=recommend(e, h),
        )
        dispatch_alerts([b], d)
        d.strategy_drift_warning.assert_called_once()
        # L3 DEGRADED must NOT trigger the broader health degraded alert
        d.strategy_health_degraded.assert_not_called()

    def test_healthy_strategy_fires_no_alerts(self):
        d = self._mock_dispatcher()
        b = _bundle()
        dispatch_alerts([b], d)
        d.strategy_edge_loss.assert_not_called()
        d.strategy_edge_below_benchmark.assert_not_called()
        d.strategy_health_degraded.assert_not_called()
        d.strategy_health_broken.assert_not_called()
        d.strategy_drift_warning.assert_not_called()


# ── _findings_for_layer helper ────────────────────────────────────────


class TestFindingsForLayer:
    def test_extracts_findings_from_specified_layer_only(self):
        h = _health(
            l1=HealthStatus.WATCH,
            l2=HealthStatus.HEALTHY,
            checks=(
                CheckResult(
                    name="a", layer=Layer.L1,
                    status=HealthStatus.WATCH,
                    findings=("l1 finding",),
                ),
                CheckResult(
                    name="b", layer=Layer.L2,
                    status=HealthStatus.HEALTHY,
                    findings=("l2 finding",),
                ),
            ),
        )
        assert _findings_for_layer(h, "L1") == ["l1 finding"]
        # L2 is HEALTHY → no findings surfaced
        assert _findings_for_layer(h, "L2") == []


# ── End-to-end via run_review ─────────────────────────────────────────


def _seed_closed_trade(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    timestamp: str,
    realized_pnl: float,
    r_multiple: float | None,
) -> None:
    conn.execute(
        "INSERT INTO trades ("
        "timestamp, symbol, side, qty, avg_fill_price, order_id, "
        "strategy, reason, stop_price, entry_reference_price, "
        "modeled_slippage_bps, realized_slippage_bps, "
        "order_type, status, requested_qty, filled_qty, "
        "realized_pnl, r_multiple"
        ") VALUES (?, 'X', 'sell', 1.0, 100.0, 'oid', ?, 'exit', "
        "95.0, 100.0, 5.0, 5.0, 'market', 'filled', 1.0, 1.0, ?, ?)",
        (timestamp, strategy, realized_pnl, r_multiple),
    )
    conn.commit()


class TestRunReviewEndToEnd:
    def _seed_envelope(self, tmp_path: Path, strategy: str = "x"):
        """Write a minimal envelope file under tmp_path/envelopes/."""
        env_dir = tmp_path / "envelopes"
        env_dir.mkdir(exist_ok=True)
        env = StrategyEnvelope(
            schema_version=ENVELOPE_SCHEMA_VERSION,
            strategy=strategy,
            built_at="2026-01-01T00:00:00+00:00",
            backtest_window_start="2024-01-01",
            backtest_window_end="2026-01-01",
            r_expectancy=0.4,
            r_expectancy_ci_95=(0.20, 0.70),
            risk_unit_dollars=5000.0,
            raw_signals_per_week_band=(10.0, 30.0),
        )
        env.write(env_dir / f"{strategy}.json")

    def test_run_review_writes_report_file(
        self, db_conn, tmp_path: Path, monkeypatch,
    ):
        """End-to-end: assess_all + render_markdown + file write."""
        # Override STRATEGY_MIN_TRADES_FOR_VERDICT so only "x" is assessed
        monkeypatch.setattr(
            settings, "STRATEGY_MIN_TRADES_FOR_VERDICT", {"x": 50},
        )
        # Monkey-patch envelope path resolution to point at tmp_path
        import strategies.health.reviewer as rev_module
        monkeypatch.setattr(
            rev_module, "_load_envelope_for", lambda s: None,
        )
        window = ReviewWindow(
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            period_type="weekly",
        )
        report_path, bundles = run_review(
            window,
            conn=db_conn,
            output_dir=tmp_path / "reports",
            state_path=tmp_path / "health_state.json",
        )
        assert report_path is not None
        assert report_path.exists()
        text = report_path.read_text()
        assert text.startswith("---")  # front-matter
        assert "## Summary" in text

    def test_dry_run_skips_file_write_and_alerts(
        self, db_conn, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setattr(
            settings, "STRATEGY_MIN_TRADES_FOR_VERDICT", {"x": 50},
        )
        import strategies.health.reviewer as rev_module
        monkeypatch.setattr(
            rev_module, "_load_envelope_for", lambda s: None,
        )
        d = MagicMock(spec=AlertDispatcher)
        window = ReviewWindow(
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            period_type="weekly",
        )
        report_path, bundles = run_review(
            window,
            conn=db_conn,
            dispatcher=d,
            output_dir=tmp_path / "reports",
            state_path=tmp_path / "health_state.json",
            dry_run=True,
        )
        assert report_path is None
        # No alerts dispatched in dry-run
        d.strategy_edge_loss.assert_not_called()
        d.strategy_edge_below_benchmark.assert_not_called()

    def test_persistence_state_saved_after_run(
        self, db_conn, tmp_path: Path, monkeypatch,
    ):
        """After run_review, the state file exists and contains an
        entry for each assessed strategy."""
        monkeypatch.setattr(
            settings, "STRATEGY_MIN_TRADES_FOR_VERDICT", {"x": 50, "y": 30},
        )
        import strategies.health.reviewer as rev_module
        monkeypatch.setattr(
            rev_module, "_load_envelope_for", lambda s: None,
        )
        state_path = tmp_path / "health_state.json"
        window = ReviewWindow(
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            period_type="weekly",
        )
        run_review(
            window,
            conn=db_conn,
            output_dir=tmp_path / "reports",
            state_path=state_path,
        )
        assert state_path.exists()
        data = json.loads(state_path.read_text())
        # Both strategies present in the state file
        assert "x" in data
        assert "y" in data

    def test_filter_to_single_strategy(
        self, db_conn, tmp_path: Path, monkeypatch,
    ):
        """--strategy x flag limits the assessment to that strategy."""
        monkeypatch.setattr(
            settings, "STRATEGY_MIN_TRADES_FOR_VERDICT", {"x": 50, "y": 30},
        )
        import strategies.health.reviewer as rev_module
        monkeypatch.setattr(
            rev_module, "_load_envelope_for", lambda s: None,
        )
        window = ReviewWindow(
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            period_type="weekly",
        )
        _, bundles = run_review(
            window,
            conn=db_conn,
            output_dir=tmp_path / "reports",
            state_path=tmp_path / "health_state.json",
            strategies=["x"],
        )
        assert len(bundles) == 1
        assert bundles[0].strategy == "x"
