"""
Unit tests for strategies/health/assessor.py — HealthAssessor.

Covers:
  - L1 stub generation when engine_state lacks risk_controls (pre-11.10f)
  - L1 cooldown check fires WATCH when state shows active cooldown
  - L2 slippage p95 computation + threshold classification
  - L2 partial-fill rate
  - L3 trade-frequency drift vs envelope band
  - L3 block-rate drift checks (edge filter, regime, fill rate)
  - L3 invariant: never BROKEN (drift is gradual)
  - Per-layer status = worst-of-layer aggregation
  - HealthReport overall_status auto-computes from layers
  - Degraded inputs: no envelope, no lifecycle counters, no trades
  - load_engine_state handles missing/malformed file gracefully
  - _classify direction = above / below variants
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from reporting.logger import TradeLogger
from strategies.health.assessor import (
    HealthAssessor,
    HealthInputs,
    _classify,
    _worst,
    load_engine_state,
)
from strategies.health.envelope import StrategyEnvelope, ENVELOPE_SCHEMA_VERSION
from strategies.health.lifecycle import LifecycleCounters, upsert_counters
from strategies.health.reports import HealthStatus, Layer
from strategies.health.thresholds import CheckThresholds


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path: Path):
    logger = TradeLogger(path=str(tmp_path / "trades.db"))
    conn = logger._ensure_db()
    yield conn
    logger.close()


def _seed_filled_trade(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    timestamp: str,
    status: str = "filled",
    realized_slippage_bps: float = 5.0,
    modeled_slippage_bps: float = 5.0,
) -> None:
    """Seed a row matching the L2 check queries."""
    conn.execute(
        "INSERT INTO trades ("
        "timestamp, symbol, side, qty, avg_fill_price, order_id, "
        "strategy, reason, stop_price, entry_reference_price, "
        "modeled_slippage_bps, realized_slippage_bps, "
        "order_type, status, requested_qty, filled_qty"
        ") VALUES (?, 'X', 'sell', 1.0, 100.0, 'oid', ?, 'exit', "
        "95.0, 100.0, ?, ?, 'market', ?, 1.0, 1.0)",
        (
            timestamp, strategy, modeled_slippage_bps, realized_slippage_bps,
            status,
        ),
    )
    conn.commit()


def _make_envelope(
    *,
    raw_band: tuple[float, float] | None = (10.0, 30.0),
    edge_band: tuple[float, float] | None = (0.40, 0.80),
    regime_band: tuple[float, float] | None = (0.05, 0.20),
    fill_band: tuple[float, float] | None = (0.85, 1.0),
) -> StrategyEnvelope:
    return StrategyEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy="x",
        built_at="2026-05-18T00:00:00+00:00",
        backtest_window_start="2024-05-18",
        backtest_window_end="2026-05-18",
        raw_signals_per_week_band=raw_band,
        edge_filter_block_rate_band=edge_band,
        regime_block_rate_band=regime_band,
        fill_rate_band=fill_band,
    )


def _standard_inputs(
    db_conn,
    *,
    engine_state: dict | None = None,
    envelope: StrategyEnvelope | None = None,
    strategy: str = "x",
) -> HealthInputs:
    return HealthInputs(
        strategy_name=strategy,
        period_start=date(2026, 5, 18),
        period_end=date(2026, 5, 25),
        envelope=envelope,
        conn=db_conn,
        engine_state=engine_state or {},
    )


# ── _classify direction logic ────────────────────────────────────────


class TestClassify:
    def test_above_healthy_below_watch(self):
        t = CheckThresholds(watch=10, degraded=20, broken=30)
        assert _classify(5, t, layer=Layer.L1) == HealthStatus.HEALTHY

    def test_above_watch_below_degraded(self):
        t = CheckThresholds(watch=10, degraded=20, broken=30)
        assert _classify(15, t, layer=Layer.L1) == HealthStatus.WATCH

    def test_above_degraded_below_broken(self):
        t = CheckThresholds(watch=10, degraded=20, broken=30)
        assert _classify(25, t, layer=Layer.L1) == HealthStatus.DEGRADED

    def test_above_broken_returns_broken_for_l1(self):
        t = CheckThresholds(watch=10, degraded=20, broken=30)
        assert _classify(50, t, layer=Layer.L1) == HealthStatus.BROKEN

    def test_above_broken_caps_at_degraded_for_l3(self):
        """L3 cannot be BROKEN per design §3.6 — even above the broken
        threshold, classification caps at DEGRADED."""
        t = CheckThresholds(watch=10, degraded=20, broken=30)
        assert _classify(50, t, layer=Layer.L3) == HealthStatus.DEGRADED

    def test_below_direction_healthy_when_above_watch(self):
        t = CheckThresholds(watch=0.7, degraded=0.5, broken=0.3, direction="below")
        assert _classify(0.9, t, layer=Layer.L2) == HealthStatus.HEALTHY

    def test_below_direction_watch_between_thresholds(self):
        t = CheckThresholds(watch=0.7, degraded=0.5, broken=0.3, direction="below")
        assert _classify(0.6, t, layer=Layer.L2) == HealthStatus.WATCH

    def test_below_direction_degraded_just_above_broken(self):
        t = CheckThresholds(watch=0.7, degraded=0.5, broken=0.3, direction="below")
        assert _classify(0.4, t, layer=Layer.L2) == HealthStatus.DEGRADED

    def test_below_direction_broken_at_floor(self):
        t = CheckThresholds(watch=0.7, degraded=0.5, broken=0.3, direction="below")
        assert _classify(0.2, t, layer=Layer.L2) == HealthStatus.BROKEN

    def test_l3_broken_threshold_none_caps_at_degraded(self):
        """L3 drift checks have broken=None per design; classification
        caps at DEGRADED."""
        t = CheckThresholds(watch=0.20, degraded=0.50, broken=None)
        assert _classify(0.80, t, layer=Layer.L3) == HealthStatus.DEGRADED


# ── _worst aggregation ────────────────────────────────────────────────


class TestWorst:
    def test_empty_list_is_healthy(self):
        assert _worst([]) == HealthStatus.HEALTHY

    def test_all_healthy(self):
        assert _worst([HealthStatus.HEALTHY] * 3) == HealthStatus.HEALTHY

    def test_picks_worst(self):
        assert _worst([
            HealthStatus.HEALTHY,
            HealthStatus.WATCH,
            HealthStatus.DEGRADED,
            HealthStatus.HEALTHY,
        ]) == HealthStatus.DEGRADED

    def test_broken_wins(self):
        assert _worst([
            HealthStatus.HEALTHY, HealthStatus.WATCH, HealthStatus.BROKEN,
        ]) == HealthStatus.BROKEN


# ── load_engine_state ─────────────────────────────────────────────────


class TestLoadEngineState:
    def test_missing_file_returns_empty_dict(self, tmp_path: Path):
        assert load_engine_state(tmp_path / "missing.json") == {}

    def test_malformed_json_returns_empty(self, tmp_path: Path):
        path = tmp_path / "bad.json"
        path.write_text("{not valid json")
        # Defensive: never raises, returns empty dict
        assert load_engine_state(path) == {}

    def test_valid_json_returned(self, tmp_path: Path):
        path = tmp_path / "good.json"
        payload = {"cycle_count": 42, "timestamp": "2026-05-18"}
        path.write_text(json.dumps(payload))
        assert load_engine_state(path) == payload


# ── L1 stubs (engine wiring pending) ──────────────────────────────────


class TestL1Stubs:
    def test_l1_returns_stubs_when_engine_state_empty(self, db_conn):
        """Without risk_controls in engine_state (pre-11.10f), L1
        produces stub checks marked HEALTHY with 'pending wiring' notes."""
        report = HealthAssessor().assess(_standard_inputs(db_conn))
        l1_checks = [c for c in report.checks if c.layer == Layer.L1]
        assert len(l1_checks) > 0
        # All stubs are HEALTHY (no real signal) but findings say so.
        stubs = [c for c in l1_checks if "pending" in " ".join(c.findings).lower()]
        assert len(stubs) > 0, "expected stub checks with pending-wiring findings"

    def test_l1_cooldown_active_fires_watch(self, db_conn):
        """When engine_state.risk_controls.cooldown_state shows active,
        the strategy_cooldown check returns WATCH (informational)."""
        engine_state = {
            "risk_controls": {
                "cooldown_state": {
                    "x": {"active": True, "until": "2026-05-25T18:00:00"},
                },
            },
        }
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, engine_state=engine_state)
        )
        cd = next(
            c for c in report.checks if c.name == "strategy_cooldown"
        )
        assert cd.status == HealthStatus.WATCH
        assert any("cooldown" in f.lower() for f in cd.findings)

    def test_l1_cooldown_inactive_is_healthy(self, db_conn):
        engine_state = {
            "risk_controls": {
                "cooldown_state": {
                    "x": {"active": False},
                },
            },
        }
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, engine_state=engine_state)
        )
        cd = next(c for c in report.checks if c.name == "strategy_cooldown")
        assert cd.status == HealthStatus.HEALTHY


# ── L2 slippage and partial-fill ──────────────────────────────────────


class TestL2Checks:
    def test_l2_slippage_classifies_correctly(self, db_conn):
        # Seed trades with realized_slippage 30 bps above modeled.
        for i in range(10):
            _seed_filled_trade(
                db_conn, strategy="x",
                timestamp=f"2026-05-{18 + i % 7:02d}T{i:02d}:00:00",
                realized_slippage_bps=35.0,
                modeled_slippage_bps=5.0,
            )
        # 30 bps delta is between watch (20) and degraded (50) → WATCH.
        report = HealthAssessor().assess(_standard_inputs(db_conn))
        slip = next(
            c for c in report.checks
            if c.name == "slippage_realized_vs_modeled_bps_p95"
        )
        assert slip.status == HealthStatus.WATCH
        assert slip.numeric_value == pytest.approx(30.0)

    def test_l2_no_trades_is_healthy_with_no_data_finding(self, db_conn):
        """Empty window → no_data finding, HEALTHY status."""
        report = HealthAssessor().assess(_standard_inputs(db_conn))
        slip = next(
            c for c in report.checks
            if c.name == "slippage_realized_vs_modeled_bps_p95"
        )
        assert slip.status == HealthStatus.HEALTHY
        assert any("no observations" in f for f in slip.findings)

    def test_l2_partial_fill_rate_classification(self, db_conn):
        # 9 full fills, 1 partial = 10% partial rate
        # Threshold default: watch=0.05, degraded=0.15 → WATCH
        for i in range(9):
            _seed_filled_trade(
                db_conn, strategy="x",
                timestamp=f"2026-05-{18 + i % 7:02d}T{i:02d}:00:00",
                status="filled",
            )
        _seed_filled_trade(
            db_conn, strategy="x",
            timestamp="2026-05-19T12:00:00",
            status="partial",
        )
        report = HealthAssessor().assess(_standard_inputs(db_conn))
        pf = next(c for c in report.checks if c.name == "partial_fill_rate")
        assert pf.status == HealthStatus.WATCH
        assert pf.numeric_value == pytest.approx(0.10)


# ── L3 drift checks ──────────────────────────────────────────────────


class TestL3DriftChecks:
    def test_l3_trade_frequency_within_band_is_healthy(self, db_conn):
        """Counters showing 21 signals/week — within envelope band
        (10-30). Drift from midpoint (20) is 5% → HEALTHY."""
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=21),
        )
        env = _make_envelope(raw_band=(10.0, 30.0))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        tf = next(c for c in report.checks if c.name == "trade_frequency_drift_pct")
        assert tf.status == HealthStatus.HEALTHY
        assert tf.numeric_value is not None
        assert tf.numeric_value < 0.30  # within WATCH default

    def test_l3_trade_frequency_severe_drift_is_degraded_not_broken(self, db_conn):
        """Counters showing 60 signals/week vs envelope band (10-30,
        midpoint=20). Drift = (60-20)/20 = 200%. Defaults: watch=30%,
        degraded=60%. 200% > 60% → DEGRADED (NOT BROKEN — L3 cap)."""
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=60),
        )
        env = _make_envelope(raw_band=(10.0, 30.0))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        tf = next(c for c in report.checks if c.name == "trade_frequency_drift_pct")
        # L3 cap: must be DEGRADED, never BROKEN
        assert tf.status == HealthStatus.DEGRADED

    def test_l3_no_envelope_band_is_healthy(self, db_conn):
        """Envelope present but no raw_signals_per_week_band → drift
        check returns HEALTHY with 'no envelope band' finding."""
        env = _make_envelope(raw_band=None)
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        tf = next(c for c in report.checks if c.name == "trade_frequency_drift_pct")
        assert tf.status == HealthStatus.HEALTHY
        assert any("envelope band unavailable" in f for f in tf.findings)

    def test_l3_no_envelope_at_all_is_healthy(self, db_conn):
        """No envelope → all L3 drift checks degrade to HEALTHY."""
        report = HealthAssessor().assess(_standard_inputs(db_conn, envelope=None))
        l3_checks = [c for c in report.checks if c.layer == Layer.L3]
        for c in l3_checks:
            assert c.status == HealthStatus.HEALTHY

    def test_l3_empty_counters_is_healthy(self, db_conn):
        """Counters table empty (no engine wiring yet) → L3 checks
        return HEALTHY rather than crashing."""
        env = _make_envelope()
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        l3_checks = [c for c in report.checks if c.layer == Layer.L3]
        for c in l3_checks:
            assert c.status == HealthStatus.HEALTHY

    def test_l3_block_rate_drift_above_band(self, db_conn):
        """100 raw signals, 100 edge-filter blocked → 100% block rate.
        Envelope band (0.40, 0.80) — 1.0 is outside upper bound. Drift
        = (1.0 - 0.80) / 0.80 = 25%. Defaults watch=20%, degraded=50%
        → WATCH."""
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=100, edge_filter_blocked=100),
        )
        env = _make_envelope(edge_band=(0.40, 0.80))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        ef = next(
            c for c in report.checks if c.name == "edge_filter_block_rate_drift_pct"
        )
        assert ef.status == HealthStatus.WATCH
        assert ef.numeric_value == pytest.approx(0.25)

    def test_l3_fill_rate_drift_zero_submitted(self, db_conn):
        """Fill-rate uses submitted as denominator. Zero submitted →
        HEALTHY (no data, not failure)."""
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=10, submitted=0, filled_entries=0),
        )
        env = _make_envelope()
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        fr = next(c for c in report.checks if c.name == "fill_rate_drift_pct")
        assert fr.status == HealthStatus.HEALTHY


# ── Layer aggregation and overall_status ──────────────────────────────


class TestLayerAggregation:
    def test_overall_status_auto_computed_from_layers(self, db_conn):
        """A WATCH L2 + HEALTHY L1/L3 → overall_status = WATCH."""
        for i in range(10):
            _seed_filled_trade(
                db_conn, strategy="x",
                timestamp=f"2026-05-{18 + i % 7:02d}T{i:02d}:00:00",
                realized_slippage_bps=35.0,
                modeled_slippage_bps=5.0,
            )
        report = HealthAssessor().assess(_standard_inputs(db_conn))
        # L2 has the slippage WATCH; L1 mostly stubs (HEALTHY); L3 healthy.
        assert report.l2_status == HealthStatus.WATCH
        assert report.overall_status == HealthStatus.WATCH

    def test_per_layer_status_is_worst_in_layer(self, db_conn):
        """One DEGRADED L3 drift makes L3 status DEGRADED even if
        other L3 checks are HEALTHY."""
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=80),  # 4x envelope mid
        )
        env = _make_envelope(raw_band=(10.0, 30.0))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        assert report.l3_status == HealthStatus.DEGRADED
        # overall = worst across layers
        assert report.overall_status == HealthStatus.DEGRADED


# ── End-to-end "empty everything" smoke ───────────────────────────────


class TestEmptyEverything:
    def test_empty_db_empty_state_no_envelope_returns_healthy(self, db_conn):
        """First run after install: nothing seeded, no envelope, no
        engine state. Must return a valid HealthReport with status
        HEALTHY (everything degrades gracefully)."""
        report = HealthAssessor().assess(_standard_inputs(db_conn))
        assert report.overall_status == HealthStatus.HEALTHY
        assert report.strategy == "x"
        # Checks were generated even with no data
        assert len(report.checks) > 0


# ── PR #19 reviewer regressions (in-band drift + p95 floor) ───────────


class TestInBandDriftRegression:
    """PR #19 reviewer caught the midpoint-distance drift formula
    reporting in-band values as drift. With band (10, 30) and observed
    = 10, the old formula said 50% drift; the correct answer is 0
    (10 IS the lower bound, fully inside the band)."""

    def _seed(self, db_conn, raw: int):
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=raw),
        )

    def test_observed_equal_lower_bound_is_zero_drift(self, db_conn):
        """Trade-freq band (10, 30); observed = 10/week → drift = 0,
        status = HEALTHY. Pre-fix this returned 50% drift → WATCH."""
        self._seed(db_conn, raw=10)  # 10 signals in 7 days ≈ 10/week
        env = _make_envelope(raw_band=(10.0, 30.0))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        tf = next(c for c in report.checks if c.name == "trade_frequency_drift_pct")
        assert tf.status == HealthStatus.HEALTHY
        assert tf.numeric_value == pytest.approx(0.0)

    def test_observed_equal_upper_bound_is_zero_drift(self, db_conn):
        """Observed = 30/week, band (10, 30) → drift = 0."""
        self._seed(db_conn, raw=30)
        env = _make_envelope(raw_band=(10.0, 30.0))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        tf = next(c for c in report.checks if c.name == "trade_frequency_drift_pct")
        assert tf.status == HealthStatus.HEALTHY
        assert tf.numeric_value == pytest.approx(0.0)

    def test_observed_in_middle_of_band_is_zero_drift(self, db_conn):
        """Observed = 20/week, band (10, 30) → still drift = 0."""
        self._seed(db_conn, raw=20)
        env = _make_envelope(raw_band=(10.0, 30.0))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        tf = next(c for c in report.checks if c.name == "trade_frequency_drift_pct")
        assert tf.numeric_value == pytest.approx(0.0)

    def test_observed_below_band_uses_lower_bound_as_denom(self, db_conn):
        """Observed = 5/week, band (10, 30) → drift = (10-5)/10 = 50%
        (NOT 75% which would be the old midpoint formula)."""
        self._seed(db_conn, raw=5)
        env = _make_envelope(raw_band=(10.0, 30.0))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        tf = next(c for c in report.checks if c.name == "trade_frequency_drift_pct")
        assert tf.numeric_value == pytest.approx(0.50)

    def test_observed_above_band_uses_upper_bound_as_denom(self, db_conn):
        """Observed = 60/week, band (10, 30) → drift = (60-30)/30 = 100%."""
        self._seed(db_conn, raw=60)
        env = _make_envelope(raw_band=(10.0, 30.0))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        tf = next(c for c in report.checks if c.name == "trade_frequency_drift_pct")
        assert tf.numeric_value == pytest.approx(1.0)

    def test_block_rate_band_with_zero_lower_bound(self, db_conn):
        """Regime block rate band (0, 0.10) — a zero-anchored band
        should not divide-by-zero. Observed = 0.05 (in band) → 0 drift.
        Observed = 0.20 → drift = (0.20 - 0.10)/0.10 = 100%."""
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=100, regime_blocked=5),
        )
        env = _make_envelope(regime_band=(0.0, 0.10))
        report = HealthAssessor().assess(
            _standard_inputs(db_conn, envelope=env)
        )
        rg = next(
            c for c in report.checks if c.name == "regime_block_rate_drift_pct"
        )
        # 5/100 = 0.05 is in the band → drift = 0
        assert rg.numeric_value == pytest.approx(0.0)
        assert rg.status == HealthStatus.HEALTHY


class TestP95SlippageRegression:
    """PR #19 reviewer caught int(0.95 * (n - 1)) flooring: on n=2 it
    returned the smaller sample as p95, hiding the bad fill entirely.
    numpy.percentile uses linear interpolation between order statistics
    — correct on small samples."""

    def test_p95_of_two_samples_returns_near_larger(self, db_conn):
        """Two fills: 0 bps slippage, 100 bps slippage. p95 must NOT
        be 0 (which the old floor-formula returned, hiding the bad
        fill)."""
        _seed_filled_trade(
            db_conn, strategy="x",
            timestamp="2026-05-19T09:00:00",
            realized_slippage_bps=5.0, modeled_slippage_bps=5.0,  # delta=0
        )
        _seed_filled_trade(
            db_conn, strategy="x",
            timestamp="2026-05-20T09:00:00",
            realized_slippage_bps=105.0, modeled_slippage_bps=5.0,  # delta=100
        )
        report = HealthAssessor().assess(_standard_inputs(db_conn))
        slip = next(
            c for c in report.checks
            if c.name == "slippage_realized_vs_modeled_bps_p95"
        )
        # numpy.percentile([0, 100], 95) = 95 (linear interpolation
        # between 0 and 100 at the 0.95 mark of the [0, 1] range
        # mapped onto two order statistics). Pre-fix this was 0.
        assert slip.numeric_value == pytest.approx(95.0)
        # 95 bps is well above the watch threshold (20) → DEGRADED
        # (just under the broken threshold of 100).
        assert slip.status == HealthStatus.DEGRADED

    def test_p95_of_three_samples_not_median(self, db_conn):
        """Three samples: pre-fix returned the median (the middle
        sample), hiding the outlier. numpy.percentile correctly
        weights toward the top."""
        for delta, day in [(0, 18), (50, 19), (200, 20)]:
            _seed_filled_trade(
                db_conn, strategy="x",
                timestamp=f"2026-05-{day:02d}T09:00:00",
                realized_slippage_bps=delta + 5.0,
                modeled_slippage_bps=5.0,
            )
        report = HealthAssessor().assess(_standard_inputs(db_conn))
        slip = next(
            c for c in report.checks
            if c.name == "slippage_realized_vs_modeled_bps_p95"
        )
        # numpy.percentile([0, 50, 200], 95) = 185.0 by linear interp;
        # certainly NOT 50 (the median, what the buggy floor returned).
        assert slip.numeric_value > 100.0
        assert slip.status == HealthStatus.BROKEN

