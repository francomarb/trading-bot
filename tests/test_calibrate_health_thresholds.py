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


_SLIPPAGE_OID_COUNTER = [0]


def _seed_slippage_trade(
    conn, *, strategy, timestamp, adverse_bps, measurement_quality="primary",
):
    """Seed a row matching the Phase 2 collector query.

    `adverse_bps=None` seeds NULL (the IS NOT NULL filter excludes it).
    `measurement_quality` defaults to 'primary' so calibration includes
    the row; pass 'recovered' / 'unavailable' / anything else to verify
    the whitelist filter excludes it.
    """
    # Foundation §6.5 partial UNIQUE on trades.order_id within single_leg
    # scope means every fixture row needs a distinct order_id, or the
    # preflight fires on _ensure_db.
    _SLIPPAGE_OID_COUNTER[0] += 1
    oid = f"oid-{_SLIPPAGE_OID_COUNTER[0]:04d}"
    conn.execute(
        "INSERT INTO trades ("
        "timestamp, symbol, side, qty, avg_fill_price, order_id, "
        "strategy, reason, stop_price, entry_reference_price, "
        "slippage_signed_bps, slippage_adverse_bps, slippage_measurement_quality, "
        "order_type, status, requested_qty, filled_qty"
        ") VALUES (?, 'X', 'sell', 1.0, 100.0, ?, ?, 'exit', "
        "95.0, 100.0, ?, ?, ?, 'market', 'filled', 1.0, 1.0)",
        (
            timestamp, oid, strategy,
            adverse_bps, adverse_bps, measurement_quality,
        ),
    )
    conn.commit()


class TestSlippageCollector:
    def test_empty_returns_empty(self, db_conn):
        out = _collect_slippage_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        assert out == []

    def test_returns_adverse_values(self, db_conn):
        """Phase 2: the column is already adverse-clamped by the
        writer, so the collector just emits the column value."""
        for ts, adverse in [
            ("2026-04-15T10:00:00", 0.0),
            ("2026-04-16T10:00:00", 30.0),
            ("2026-04-17T10:00:00", 0.0),
            ("2026-04-18T10:00:00", 100.0),
        ]:
            _seed_slippage_trade(
                db_conn, strategy="x", timestamp=ts, adverse_bps=adverse,
            )
        out = _collect_slippage_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        assert sorted(out) == [0.0, 0.0, 30.0, 100.0]

    def test_excludes_other_strategies(self, db_conn):
        _seed_slippage_trade(
            db_conn, strategy="A", timestamp="2026-04-15T10:00:00",
            adverse_bps=30.0,
        )
        _seed_slippage_trade(
            db_conn, strategy="B", timestamp="2026-04-15T10:00:00",
            adverse_bps=195.0,
        )
        out = _collect_slippage_observations(
            db_conn, "A",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        assert out == [30.0]

    def test_excludes_recovered_quality_rows(self, db_conn):
        """Phase 2: calibration applies the same quality whitelist
        as `strategies/health/assessor.py:_slippage_p95_bps`. Rows
        tagged `recovered` (codepaths §5/§8/§9 — benchmark
        reconstructed from broker history) carry honest but
        synthetic measurements and shouldn't skew threshold
        proposals."""
        _seed_slippage_trade(
            db_conn, strategy="x", timestamp="2026-04-15T10:00:00",
            adverse_bps=5.0,
        )
        _seed_slippage_trade(
            db_conn, strategy="x", timestamp="2026-04-16T10:00:00",
            adverse_bps=1205.3, measurement_quality="recovered",
        )
        out = _collect_slippage_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        # Only the primary-quality fill survives.
        assert out == [5.0]

    def test_excludes_unknown_quality_rows(self, db_conn):
        """Quality whitelist fails closed — any future enum (or a
        typo) is excluded until explicitly opted in."""
        _seed_slippage_trade(
            db_conn, strategy="x", timestamp="2026-04-15T10:00:00",
            adverse_bps=5.0,
        )
        _seed_slippage_trade(
            db_conn, strategy="x", timestamp="2026-04-16T10:00:00",
            adverse_bps=900.0, measurement_quality="some_future_tier",
        )
        out = _collect_slippage_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        assert out == [5.0]

    def test_fallback_quality_rows_are_included(self, db_conn):
        """`fallback` is whitelisted alongside `primary` — SMA /
        Donchian market entries that benchmark against the latest
        close are honest execution measurements and must contribute
        to calibration."""
        _seed_slippage_trade(
            db_conn, strategy="x", timestamp="2026-04-15T10:00:00",
            adverse_bps=80.0, measurement_quality="fallback",
        )
        out = _collect_slippage_observations(
            db_conn, "x",
            start=date(2026, 4, 1), end=date(2026, 5, 1),
        )
        assert out == [80.0]


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
                adverse_bps=float(i * 2),
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
                    adverse_bps=float(i),
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
