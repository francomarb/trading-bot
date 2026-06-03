"""
Unit tests for scripts/calibrate_health_thresholds.py.

Covers:
  - Empty DB → exit 1 (forces operator awareness, no silent green)
  - Seeded slippage data → percentiles computed correctly
  - Seeded lifecycle counters → block-rate distributions computed
  - Report rendering produces valid markdown structure
  - --strategy filter limits the calibration
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from pathlib import Path

import pytest

from config import settings
from reporting.logger import TradeLogger
from scripts.calibrate_health_thresholds import (
    _collect_drift_observations,
    _collect_slippage_observations,
    _percentiles,
    _suggest_threshold,
    calibrate,
    main,
    render_report,
)
from strategies.health.lifecycle import LifecycleCounters, upsert_counters
from strategies.health.thresholds import CheckThresholds


# ── _percentiles ──────────────────────────────────────────────────────


class TestPercentiles:
    def test_empty_returns_zeros(self):
        out = _percentiles([])
        for p in ("p50", "p90", "p95", "p99"):
            assert out[p] == 0.0

    def test_single_value(self):
        out = _percentiles([42.0])
        # All percentiles of a single sample equal the sample.
        for p in ("p50", "p90", "p95", "p99"):
            assert out[p] == 42.0

    def test_uniform_distribution(self):
        out = _percentiles(list(range(100)))  # 0..99
        # numpy linear interpolation: p50 ≈ 49.5, p95 ≈ 94.05
        assert 49.0 < out["p50"] < 51.0
        assert 94.0 < out["p95"] < 95.0


# ── _suggest_threshold ────────────────────────────────────────────────


class TestSuggestThreshold:
    def test_above_direction_suggests_from_percentiles(self):
        current = CheckThresholds(watch=10, degraded=20, broken=30)
        pcts = {"p50": 5.0, "p90": 15.0, "p95": 25.0, "p99": 40.0}
        suggested = _suggest_threshold(current, pcts)
        assert suggested.watch == 15.0  # p90
        assert suggested.degraded == 25.0  # p95
        assert suggested.broken == 40.0  # p99
        assert suggested.direction == "above"

    def test_below_direction_unchanged(self):
        """Below-direction checks aren't auto-suggested; return as-is."""
        current = CheckThresholds(
            watch=0.7, degraded=0.5, broken=0.3, direction="below",
        )
        pcts = {"p50": 0.6, "p90": 0.4, "p95": 0.3, "p99": 0.2}
        suggested = _suggest_threshold(current, pcts)
        # Returns unchanged.
        assert suggested == current

    def test_l3_drift_with_none_broken(self):
        """L3 drift checks have broken=None; suggestion preserves that."""
        current = CheckThresholds(watch=0.2, degraded=0.5, broken=None)
        pcts = {"p50": 0.1, "p90": 0.3, "p95": 0.5, "p99": 0.7}
        suggested = _suggest_threshold(current, pcts)
        assert suggested.broken is None


# ── DB collectors ────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path: Path):
    logger = TradeLogger(path=str(tmp_path / "trades.db"))
    conn = logger._ensure_db()
    yield conn
    logger.close()


def _seed_slippage_trade(conn, *, strategy, timestamp, realized, modeled):
    conn.execute(
        "INSERT INTO trades ("
        "timestamp, symbol, side, qty, avg_fill_price, order_id, "
        "strategy, reason, stop_price, entry_reference_price, "
        "modeled_slippage_bps, realized_slippage_bps, "
        "order_type, status, requested_qty, filled_qty"
        ") VALUES (?, 'X', 'sell', 1.0, 100.0, 'oid', ?, 'exit', "
        "95.0, 100.0, ?, ?, 'market', 'filled', 1.0, 1.0)",
        (timestamp, strategy, modeled, realized),
    )
    conn.commit()


class TestSlippageCollector:
    def test_empty_returns_empty(self, db_conn):
        out = _collect_slippage_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        assert out == []

    def test_returns_adverse_delta_only(self, db_conn):
        """Adverse-only semantics: only positive `realized - modeled`
        contributes to the calibration sample. Price improvement
        (negative `realized - modeled`) clamps to 0. Mirrors the live
        L2 check's `max(0, realized - modeled)` so calibration learns
        from the same distribution the assessor sees."""
        for ts, realized, modeled in [
            ("2026-04-15T10:00:00", 5.0, 5.0),     # zero drift → 0
            ("2026-04-16T10:00:00", 35.0, 5.0),    # adverse +30 → 30
            ("2026-04-17T10:00:00", -95.0, 5.0),   # price-improvement → 0
            ("2026-04-18T10:00:00", 105.0, 5.0),   # adverse +100 → 100
        ]:
            _seed_slippage_trade(
                db_conn, strategy="x", timestamp=ts,
                realized=realized, modeled=modeled,
            )
        out = _collect_slippage_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        # Adverse-only: two zeros + two adverse drifts; the -95 row
        # (which the old abs() formula would have counted as 100)
        # contributes 0 under the new semantics.
        assert sorted(out) == [0.0, 0.0, 30.0, 100.0]

    def test_excludes_other_strategies(self, db_conn):
        _seed_slippage_trade(
            db_conn, strategy="A", timestamp="2026-04-15T10:00:00",
            realized=35.0, modeled=5.0,
        )
        _seed_slippage_trade(
            db_conn, strategy="B", timestamp="2026-04-15T10:00:00",
            realized=200.0, modeled=5.0,
        )
        out = _collect_slippage_observations(
            db_conn, "A",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        assert out == [30.0]

    def test_excludes_recovered_context_rows(self, db_conn):
        """Reviewer P2 #1: calibration must apply the same defensive
        filter as the live L2 check. Legacy recovered-entry-context
        rows carry phantom slippage (1200+ bps) that would skew the
        proposed WATCH/DEGRADED/BROKEN thresholds upward — even though
        the live assessor learned to skip them in PR #37."""
        _seed_slippage_trade(
            db_conn, strategy="x", timestamp="2026-04-15T10:00:00",
            realized=10.0, modeled=5.0,  # clean fill, delta=5
        )
        # Mimic the legacy recovered-context shape: same columns, but
        # `reason` carries the marker the engine's recovery path used to
        # write. Use the raw INSERT so we can override the `reason`
        # column (the helper hardcodes 'exit').
        db_conn.execute(
            "INSERT INTO trades ("
            "timestamp, symbol, side, qty, avg_fill_price, order_id, "
            "strategy, reason, stop_price, entry_reference_price, "
            "modeled_slippage_bps, realized_slippage_bps, "
            "order_type, status, requested_qty, filled_qty"
            ") VALUES ('2026-04-16T10:00:00', 'X', 'buy', 1.0, 100.0, "
            "'oid', 'x', 'x recovered entry context', 95.0, 100.0, "
            "0.0, 1205.3, 'market', 'filled', 1.0, 1.0)"
        )
        db_conn.commit()
        out = _collect_slippage_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        # Only the clean fill survives — the phantom 1205.3 bps row is
        # excluded by the defensive `reason NOT LIKE` filter. Without
        # the filter, calibration would learn p90/p95/p99 from a sample
        # of [5.0, 1205.3] and propose massively inflated thresholds.
        assert out == [5.0]


class TestDriftCollector:
    def test_empty_returns_zero_lists(self, db_conn):
        out = _collect_drift_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        # All metric keys present, all lists empty.
        for key in (
            "edge_filter_block_rate", "regime_block_rate",
            "risk_block_rate", "fill_rate",
        ):
            assert out[key] == []

    def test_computes_per_week_ratios(self, db_conn):
        # Week 1: 100 raw, 50 edge-blocked, 80 submitted, 70 filled
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 4, 13),
            period_end=date(2026, 4, 20),
            strategy_name="x",
            counters=LifecycleCounters(
                raw_signals=100, edge_filter_blocked=50,
                submitted=80, filled_entries=70,
            ),
        )
        # Week 2: 200 raw, 100 edge-blocked, 150 submitted, 140 filled
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 4, 20),
            period_end=date(2026, 4, 27),
            strategy_name="x",
            counters=LifecycleCounters(
                raw_signals=200, edge_filter_blocked=100,
                submitted=150, filled_entries=140,
            ),
        )
        out = _collect_drift_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        # Both weeks: edge_filter_block_rate = 0.5
        assert out["edge_filter_block_rate"] == pytest.approx([0.5, 0.5])
        # Week 1 fill rate = 70/80 = 0.875; week 2 = 140/150 ≈ 0.933
        assert out["fill_rate"][0] == pytest.approx(0.875)
        assert out["fill_rate"][1] == pytest.approx(140 / 150)


# ── calibrate end-to-end ──────────────────────────────────────────────


class TestCalibrate:
    def test_empty_db_returns_empty_checks(self, db_conn):
        out = calibrate(
            db_conn, strategy_name="x", weeks=4,
            end_date=date(2026, 5, 1),
        )
        assert out["strategy"] == "x"
        assert out["checks"] == {}

    def test_with_slippage_data_includes_check(self, db_conn):
        # Seed 20 trades within the 4-week window (Apr 5 → May 1).
        for i in range(20):
            day = 5 + i  # 05..24 — all within 4-week trailing window
            _seed_slippage_trade(
                db_conn, strategy="x",
                timestamp=f"2026-04-{day:02d}T10:00:00",
                realized=5.0 + i * 2.0,
                modeled=5.0,
            )
        out = calibrate(
            db_conn, strategy_name="x", weeks=4,
            end_date=date(2026, 5, 1),
        )
        # Slippage check present.
        assert (
            "slippage_realized_vs_modeled_bps_p95" in out["checks"]
        )
        payload = out["checks"]["slippage_realized_vs_modeled_bps_p95"]
        assert payload["samples"] == 20
        assert "suggested" in payload


class TestRenderReport:
    def test_empty_results_renders_no_data_message(self):
        report = render_report([])
        assert "No usable data" in report
        assert "HEALTH_COUNTERS_ENABLED" in report

    def test_with_data_renders_markdown_sections(self):
        results = [{
            "strategy": "x",
            "window_start": "2026-04-01",
            "window_end": "2026-05-01",
            "weeks": 4,
            "checks": {
                "slippage_realized_vs_modeled_bps_p95": {
                    "samples": 50,
                    "percentiles": {
                        "p50": 5.0, "p90": 25.0,
                        "p95": 40.0, "p99": 90.0,
                    },
                    "current": "watch=20, degraded=50, broken=100",
                    "suggested": "watch=25, degraded=40, broken=90",
                },
            },
        }]
        report = render_report(results)
        assert "# Health threshold calibration" in report
        assert "## x" in report
        assert "slippage_realized_vs_modeled_bps_p95" in report
        assert "Suggested" in report

    def test_strategy_with_no_observations_notes_absence(self):
        results = [{
            "strategy": "y",
            "window_start": "2026-04-01",
            "window_end": "2026-05-01",
            "weeks": 4,
            "checks": {},
        }]
        report = render_report(results)
        assert "No observations" in report
        assert "y" in report


# ── CLI exit code ─────────────────────────────────────────────────────


class TestMainExitCode:
    def test_exits_1_when_no_data(self, monkeypatch):
        """Empty DB → no usable data → exit 1. Forces operator
        awareness rather than silently returning a green report."""
        monkeypatch.setattr(
            settings, "STRATEGY_MIN_TRADES_FOR_VERDICT", {"x": 50},
        )
        rc = main(["--weeks", "4"])
        assert rc == 1

    def test_exits_0_when_data_present(self, monkeypatch, tmp_path):
        """With seeded slippage data, exit 0."""
        monkeypatch.setattr(
            settings, "STRATEGY_MIN_TRADES_FOR_VERDICT", {"x": 50},
        )
        # Seed via the isolate_runtime_artifacts fixture's redirected
        # trades.db path.
        from reporting.logger import TradeLogger as _TL
        tl = _TL()
        conn = tl._ensure_db()
        try:
            for i in range(20):
                day = 5 + i
                _seed_slippage_trade(
                    conn, strategy="x",
                    timestamp=f"2026-04-{day:02d}T10:00:00",
                    realized=5.0 + i,
                    modeled=5.0,
                )
        finally:
            tl.close()

        rc = main(["--weeks", "4", "--end-date", "2026-05-01"])
        assert rc == 0

    def test_strategy_filter(self, monkeypatch, capsys):
        """--strategy limits the calibration to that strategy."""
        monkeypatch.setattr(
            settings, "STRATEGY_MIN_TRADES_FOR_VERDICT",
            {"sma_crossover": 30, "rsi_reversion": 50},
        )
        main(["--weeks", "4", "--strategy", "sma_crossover"])
        out = capsys.readouterr().out
        # Only sma_crossover should appear in the report, not rsi.
        assert "sma_crossover" in out
        assert "rsi_reversion" not in out
