"""
Unit tests for strategies/health/edge.py — EdgeAssessor.

Covers:
  - Decision matrix exhaustively (POSITIVE, NEGATIVE, BELOW_BENCHMARK,
    UNDETERMINED) across each sufficiency tier
  - Three-signal NEGATIVE logic (all three must agree + persistence ≥3)
  - Sufficiency floor protection (no NEGATIVE below floor)
  - 3-week persistence requirement (1-week NEG doesn't trip alarm)
  - PersistenceState mutation/threading across consecutive weeks
  - Closed-trade DB read filters (timestamp window, side='sell' OR
    position_type='spread', status filter, non-finite filtering)
  - No-envelope case → UNDETERMINED (can't compare R to band)
  - Empty trades → UNDETERMINED
  - BELOW_BENCHMARK requires both benchmark_return AND nominal sleeve
  - Sleeve utilization computation
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from reporting.logger import TradeLogger
from strategies.health.edge import (
    EdgeAssessor,
    EdgeInputs,
    NEGATIVE_PERSISTENCE_WEEKS_REQUIRED,
    _cumulative,
    _read_closed_trades,
    _sleeve_utilization,
)
from strategies.health.envelope import StrategyEnvelope, ENVELOPE_SCHEMA_VERSION
from strategies.health.persistence import PersistenceState
from strategies.health.reports import EdgeVerdict, Sufficiency


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path: Path):
    logger = TradeLogger(path=str(tmp_path / "trades.db"))
    conn = logger._ensure_db()
    yield conn
    logger.close()


def _seed_closed_trade(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    timestamp: str,
    realized_pnl: float,
    r_multiple: float | None,
    side: str = "sell",
    status: str = "filled",
    position_type: str | None = None,
) -> None:
    """Insert one closed-trade row matching the EdgeAssessor query
    filters. Keeps tests close to the production schema."""
    conn.execute(
        "INSERT INTO trades ("
        "timestamp, symbol, side, qty, avg_fill_price, order_id, "
        "strategy, reason, stop_price, entry_reference_price, "
        "modeled_slippage_bps, realized_slippage_bps, "
        "order_type, status, requested_qty, filled_qty, "
        "realized_pnl, r_multiple, position_type"
        ") VALUES (?, 'X', ?, 1.0, 100.0, 'oid', ?, 'exit', "
        "95.0, 100.0, 5.0, 5.0, 'market', ?, 1.0, 1.0, ?, ?, ?)",
        (timestamp, side, strategy, status, realized_pnl, r_multiple, position_type),
    )
    conn.commit()


def _make_envelope(
    strategy: str = "donchian_breakout",
    *,
    r_ci: tuple[float, float] | None = (0.20, 0.70),
    raw_signals_band: tuple[float, float] | None = (12.0, 38.0),
) -> StrategyEnvelope:
    return StrategyEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy=strategy,
        built_at="2026-05-18T00:00:00+00:00",
        backtest_window_start="2024-05-18",
        backtest_window_end="2026-05-18",
        r_expectancy=0.4,
        r_expectancy_ci_95=r_ci,
        risk_unit_dollars=5000.0,
        raw_signals_per_week_band=raw_signals_band,
    )


# ── DB read ───────────────────────────────────────────────────────────


class TestReadClosedTrades:
    def test_empty_table_returns_empty(self, db_conn):
        t = _read_closed_trades(
            db_conn, strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
        )
        assert t.r_multiples == ()
        assert t.pnls == ()
        assert t.total_pnl == 0.0

    def test_strategy_filter_isolates(self, db_conn):
        _seed_closed_trade(db_conn, strategy="A",
                           timestamp="2026-05-20", realized_pnl=100, r_multiple=1.0)
        _seed_closed_trade(db_conn, strategy="B",
                           timestamp="2026-05-20", realized_pnl=200, r_multiple=2.0)
        t = _read_closed_trades(
            db_conn, strategy_name="A",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
        )
        assert t.r_multiples == (1.0,)
        assert t.pnls == (100.0,)

    def test_window_filter_inclusive_start_exclusive_end(self, db_conn):
        for ts, pnl in [
            ("2026-05-17", 100),  # before window
            ("2026-05-18", 200),  # at start (inclusive)
            ("2026-05-24", 300),  # in window
            ("2026-05-25", 400),  # at end (exclusive)
        ]:
            _seed_closed_trade(
                db_conn, strategy="x",
                timestamp=ts, realized_pnl=pnl, r_multiple=1.0,
            )
        t = _read_closed_trades(
            db_conn, strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
        )
        assert sorted(t.pnls) == [200.0, 300.0]

    def test_status_filter_excludes_open_orders(self, db_conn):
        _seed_closed_trade(db_conn, strategy="x", timestamp="2026-05-20",
                           realized_pnl=100, r_multiple=1.0, status="filled")
        _seed_closed_trade(db_conn, strategy="x", timestamp="2026-05-20",
                           realized_pnl=999, r_multiple=99.0, status="accepted")
        t = _read_closed_trades(
            db_conn, strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
        )
        assert t.pnls == (100.0,)

    def test_side_buy_only_excluded_unless_spread(self, db_conn):
        _seed_closed_trade(db_conn, strategy="x", timestamp="2026-05-20",
                           realized_pnl=100, r_multiple=1.0, side="buy")
        _seed_closed_trade(db_conn, strategy="x", timestamp="2026-05-20",
                           realized_pnl=200, r_multiple=2.0, side="buy",
                           position_type="spread")
        t = _read_closed_trades(
            db_conn, strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
        )
        # First buy (no spread tag) is excluded; spread close is included.
        assert t.pnls == (200.0,)

    def test_null_r_multiple_kept_in_pnls_only(self, db_conn):
        """Pre-11.27 rows may have NULL r_multiple but valid PnL.
        Those count in dollar metrics but not R-based metrics."""
        _seed_closed_trade(db_conn, strategy="x", timestamp="2026-05-20",
                           realized_pnl=100, r_multiple=None)
        _seed_closed_trade(db_conn, strategy="x", timestamp="2026-05-20",
                           realized_pnl=200, r_multiple=2.0)
        t = _read_closed_trades(
            db_conn, strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
        )
        assert t.pnls == (100.0, 200.0)
        assert t.r_multiples == (2.0,)


# ── Sufficiency floor protection ──────────────────────────────────────


class TestSufficiencyGate:
    def _seed_n(self, conn, *, n: int, r: float, strategy: str = "x"):
        # All within the standard 2026-05-18..2026-05-25 window.
        for i in range(n):
            day = 18 + (i % 7)
            _seed_closed_trade(
                conn, strategy=strategy,
                timestamp=f"2026-05-{day:02d}T{i % 24:02d}:{i % 60:02d}:00",
                realized_pnl=r * 100.0, r_multiple=r,
            )

    def test_below_half_floor_is_insufficient_undetermined(self, db_conn):
        # 9 trades, floor=50, half-floor=25 → INSUFFICIENT
        self._seed_n(db_conn, n=9, r=-2.0)
        env = _make_envelope()
        report, _ = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=PersistenceState(),
            min_trades_floor=50,
        ))
        assert report.sufficiency == Sufficiency.INSUFFICIENT
        assert report.verdict == EdgeVerdict.UNDETERMINED

    def test_between_half_and_floor_is_indicative_undetermined(self, db_conn):
        self._seed_n(db_conn, n=30, r=-2.0)  # floor=50, n=30 → INDICATIVE
        env = _make_envelope()
        report, _ = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=PersistenceState(),
            min_trades_floor=50,
        ))
        assert report.sufficiency == Sufficiency.INDICATIVE
        # Even with overwhelming negative R, INDICATIVE → never NEGATIVE
        assert report.verdict == EdgeVerdict.UNDETERMINED


# ── Three-week persistence requirement ────────────────────────────────


class TestPersistenceGating:
    def _seed_overwhelming_negative(self, conn, *, n: int):
        # All within the standard 7-day test window.
        for i in range(n):
            day = 18 + (i % 7)
            _seed_closed_trade(
                conn, strategy="x",
                timestamp=f"2026-05-{day:02d}T{i % 24:02d}:{i % 60:02d}:00",
                realized_pnl=-200.0, r_multiple=-2.0,
            )

    def test_one_week_negative_does_not_fire(self, db_conn):
        """All signals agree, sufficiency CONCLUSIVE, but persistence
        only 1 week → still UNDETERMINED, alarm doesn't fire."""
        self._seed_overwhelming_negative(db_conn, n=120)  # > 100 for EMA
        env = _make_envelope()
        report, new_state = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=PersistenceState(),  # starts fresh
            min_trades_floor=50,
        ))
        assert report.sufficiency == Sufficiency.CONCLUSIVE
        # Persistence count is now 1 — below 3, so alarm doesn't fire
        assert new_state.negative_weeks == 1
        # Per design §9, verdict still NEGATIVE only at persistence ≥3.
        # Below that, verdict stays UNDETERMINED (signals tripped but
        # alarm withheld).
        assert report.verdict == EdgeVerdict.UNDETERMINED

    def test_two_weeks_negative_still_does_not_fire(self, db_conn):
        self._seed_overwhelming_negative(db_conn, n=120)
        env = _make_envelope()
        # State already at 1 NEG week (last week was bad)
        prev_state = PersistenceState(
            negative_weeks=1, last_check="2026-05-18", last_verdict="NEGATIVE",
        )
        report, new_state = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=prev_state,
            min_trades_floor=50,
        ))
        assert new_state.negative_weeks == 2
        assert report.verdict == EdgeVerdict.UNDETERMINED

    def test_third_negative_week_fires_alarm(self, db_conn):
        self._seed_overwhelming_negative(db_conn, n=120)
        env = _make_envelope()
        # State at 2 NEG weeks already — this is week 3
        prev_state = PersistenceState(
            negative_weeks=2, last_check="2026-05-18", last_verdict="NEGATIVE",
        )
        report, new_state = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=prev_state,
            min_trades_floor=50,
        ))
        assert new_state.negative_weeks == NEGATIVE_PERSISTENCE_WEEKS_REQUIRED
        assert report.verdict == EdgeVerdict.NEGATIVE
        assert report.negative_persistence_weeks == 3

    def test_intervening_positive_resets_counter(self, db_conn):
        """If week 2 is POSITIVE, the counter resets and the next
        NEG week starts from 1, not 3."""
        # Mostly positive trades — all within the test window.
        for i in range(120):
            day = 18 + (i % 7)
            _seed_closed_trade(
                db_conn, strategy="x",
                timestamp=f"2026-05-{day:02d}T{i % 24:02d}:{i % 60:02d}:00",
                realized_pnl=200.0, r_multiple=2.0,
            )
        env = _make_envelope(r_ci=(0.20, 0.70))
        prev_state = PersistenceState(
            negative_weeks=2, last_check="2026-05-11", last_verdict="NEGATIVE",
        )
        report, new_state = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=prev_state,
            min_trades_floor=50,
        ))
        # Positive R → no NEGATIVE signal → counter resets
        assert new_state.negative_weeks == 0


# ── Three-signal logic ────────────────────────────────────────────────


class TestThreeSignalLogic:
    def test_ema_signal_unavailable_below_100_trades_blocks_negative(self, db_conn):
        """The EMA50/100 cross detector requires >= 100 samples to
        claim a downward cross. With 80 strongly-negative trades, the
        CI and t-test signals fire but EMA cross can't — NOT all three
        agree, so NEGATIVE doesn't fire even with persistence-3 ready."""
        for i in range(80):  # less than EMA slow_length=100
            day = 18 + (i % 7)
            _seed_closed_trade(
                db_conn, strategy="x",
                timestamp=f"2026-05-{day:02d}T{i % 24:02d}:{i % 60:02d}:00",
                realized_pnl=-500.0, r_multiple=-2.0,
            )
        env = _make_envelope(r_ci=(0.20, 0.70))
        prev_state = PersistenceState(
            negative_weeks=2, last_check="2026-05-18", last_verdict="NEGATIVE",
        )
        report, new_state = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=prev_state,
            min_trades_floor=50,
        ))
        # CI and t-test would trip with 80 strongly-negative trades, but
        # EMA cross requires >= 100 samples — signal 3 is silent. With
        # only 2/3 agreeing, NEGATIVE doesn't fire.
        assert report.verdict != EdgeVerdict.NEGATIVE
        # Persistence counter should reset (signals didn't all agree).
        assert new_state.negative_weeks == 0


# ── POSITIVE / BELOW_BENCHMARK / no-envelope ─────────────────────────


class TestPositiveAndBelowBenchmark:
    def _seed_positive_set(self, conn, *, n: int = 60):
        # Strong positive R; CI should be > 0. All within test window.
        for i in range(n):
            day = 18 + (i % 7)
            _seed_closed_trade(
                conn, strategy="x",
                timestamp=f"2026-05-{day:02d}T{i % 24:02d}:{i % 60:02d}:00",
                realized_pnl=500.0, r_multiple=0.4,
            )

    def test_positive_verdict_strong_data(self, db_conn):
        self._seed_positive_set(db_conn, n=60)
        env = _make_envelope(r_ci=(0.20, 0.70))
        report, _ = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=PersistenceState(),
            min_trades_floor=50,
        ))
        assert report.sufficiency == Sufficiency.CONCLUSIVE
        assert report.verdict == EdgeVerdict.POSITIVE
        assert report.failure_reasons == ()
        # R-expectancy reported
        assert report.r_expectancy is not None
        assert report.r_expectancy > 0

    def test_below_benchmark_when_underperforming(self, db_conn):
        """POSITIVE conditions met, but strategy_return <
        benchmark_return → verdict is BELOW_BENCHMARK."""
        # PnL totals to a modest 60 * $500 = $30,000 on $40k sleeve =
        # 75% return. Set a benchmark of 80% so strategy under-performs.
        self._seed_positive_set(db_conn, n=60)
        env = _make_envelope(r_ci=(0.20, 0.70))
        report, _ = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=PersistenceState(),
            min_trades_floor=50,
            benchmark_return=0.80,  # 80% benchmark
            nominal_sleeve_dollars=40000.0,  # $40k sleeve
        ))
        assert report.verdict == EdgeVerdict.BELOW_BENCHMARK
        assert report.strategy_return is not None
        assert report.benchmark_return == 0.80
        assert report.alpha is not None
        assert report.alpha < 0

    def test_no_envelope_means_undetermined(self, db_conn):
        """Without an envelope, R-expectancy CI can't be compared to a
        band → no POSITIVE possible → UNDETERMINED."""
        self._seed_positive_set(db_conn, n=60)
        report, _ = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=None,
            conn=db_conn,
            persistence_state=PersistenceState(),
            min_trades_floor=50,
        ))
        assert report.verdict == EdgeVerdict.UNDETERMINED
        # Profitability metrics still reported
        assert report.r_expectancy is not None
        assert report.expectancy_dollars is not None

    def test_benchmark_without_sleeve_no_below_benchmark(self, db_conn):
        """BELOW_BENCHMARK needs BOTH benchmark_return and
        nominal_sleeve_dollars (to compute strategy_return)."""
        self._seed_positive_set(db_conn, n=60)
        env = _make_envelope(r_ci=(0.20, 0.70))
        report, _ = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=PersistenceState(),
            min_trades_floor=50,
            benchmark_return=0.80,
            nominal_sleeve_dollars=None,  # missing
        ))
        # Without sleeve dollars, strategy_return is None → can't compare
        assert report.alpha is None
        # Verdict falls back to POSITIVE (or UNDETERMINED), never
        # BELOW_BENCHMARK without alpha.
        assert report.verdict in (EdgeVerdict.POSITIVE, EdgeVerdict.UNDETERMINED)


# ── Empty / edge cases ───────────────────────────────────────────────


class TestEmptyAndEdge:
    def test_empty_trades_is_insufficient_undetermined(self, db_conn):
        env = _make_envelope()
        report, _ = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=PersistenceState(),
            min_trades_floor=50,
        ))
        assert report.sufficiency == Sufficiency.INSUFFICIENT
        assert report.verdict == EdgeVerdict.UNDETERMINED
        assert report.trade_count == 0
        assert report.realized_pnl == 0.0

    def test_cumulative_helper(self):
        assert _cumulative([]) == []
        assert _cumulative([1.0]) == [1.0]
        assert _cumulative([1.0, 2.0, -0.5]) == [1.0, 3.0, 2.5]

    def test_sleeve_utilization_none_without_sleeve(self):
        assert _sleeve_utilization([100.0, -50.0], None) is None

    def test_sleeve_utilization_zero_sleeve(self):
        assert _sleeve_utilization([100.0, -50.0], 0.0) is None

    def test_sleeve_utilization_positive(self):
        # |100|+|-50| = 150 / 1000 sleeve = 0.15
        assert _sleeve_utilization([100.0, -50.0], 1000.0) == pytest.approx(0.15)


# ── Persistence threading across consecutive weeks ────────────────────


class TestPersistenceThreading:
    """End-to-end: three weeks of NEGATIVE in sequence should result
    in negative_weeks=3 and the final verdict NEGATIVE, mirroring
    how the reviewer (11.10e) will use the assessor across weekly
    runs."""

    def test_three_consecutive_negative_weeks_e2e(self, db_conn):
        # Seed three weeks of overwhelming negative data — 40 trades per
        # week, all within their respective week windows so each week's
        # read returns CONCLUSIVE sample size with all-negative R.
        for week_idx, (week_start, week_end) in enumerate([
            (date(2026, 5, 18), date(2026, 5, 25)),
            (date(2026, 5, 25), date(2026, 6, 1)),
            (date(2026, 6, 1), date(2026, 6, 8)),
        ]):
            for i in range(120):  # > 100 so EMA cross detector has enough
                day_offset = i % 7
                day = (week_start.toordinal() + day_offset)
                ts_date = date.fromordinal(day).isoformat()
                _seed_closed_trade(
                    db_conn, strategy="x",
                    timestamp=f"{ts_date}T{i % 24:02d}:{i % 60:02d}:00",
                    realized_pnl=-200.0, r_multiple=-2.0,
                )

        env = _make_envelope(r_ci=(0.20, 0.70))
        state = PersistenceState()

        for week_idx, (week_start, week_end) in enumerate([
            (date(2026, 5, 18), date(2026, 5, 25)),
            (date(2026, 5, 25), date(2026, 6, 1)),
            (date(2026, 6, 1), date(2026, 6, 8)),
        ], start=1):
            report, state = EdgeAssessor().assess(EdgeInputs(
                strategy_name="x",
                period_start=week_start,
                period_end=week_end,
                envelope=env,
                conn=db_conn,
                persistence_state=state,
                min_trades_floor=50,
            ))
            if week_idx < NEGATIVE_PERSISTENCE_WEEKS_REQUIRED:
                assert report.verdict == EdgeVerdict.UNDETERMINED, (
                    f"week {week_idx}: expected UNDETERMINED, "
                    f"got {report.verdict}"
                )
                assert state.negative_weeks == week_idx
            else:
                assert report.verdict == EdgeVerdict.NEGATIVE
                assert state.negative_weeks == NEGATIVE_PERSISTENCE_WEEKS_REQUIRED


# ── PR #19 reviewer regression: same-day rerun idempotency ────────────


class TestSameDayRerunDoesNotTripEarly:
    """The persistence projection used `state.negative_weeks + 1`
    directly, bypassing apply_verdict's same-day-same-verdict
    idempotency. If a week-2 NEGATIVE assessment had already been
    saved (state: negative_weeks=2, last_check=period_end), an
    operator re-running the same period would project 3 and fire
    the alarm prematurely even though apply_verdict would correctly
    leave state at 2.

    The fix uses a two-step apply_verdict: provisional update with
    the eligibility flag (so idempotency holds), then verdict
    computation reads new_state.negative_weeks, then final apply
    with the verdict for last_verdict accuracy."""

    def _seed_overwhelming_negative_in_window(self, conn, *, n: int = 120):
        for i in range(n):
            day = 18 + (i % 7)
            _seed_closed_trade(
                conn, strategy="x",
                timestamp=f"2026-05-{day:02d}T{i % 24:02d}:{i % 60:02d}:00",
                realized_pnl=-200.0, r_multiple=-2.0,
            )

    def test_same_day_rerun_does_not_increment_persistence(self, db_conn):
        """First run on 2026-05-25 with state.negative_weeks=2 saves
        as negative_weeks=3 with last_check=2026-05-25 — alarm fires.
        Second run on the SAME day must NOT push to negative_weeks=4
        (idempotency)."""
        self._seed_overwhelming_negative_in_window(db_conn)
        env = _make_envelope(r_ci=(0.20, 0.70))
        prev_state = PersistenceState(
            negative_weeks=2, last_check="2026-05-18", last_verdict="NEGATIVE",
        )
        # First run: week 3, NEGATIVE fires
        report1, state1 = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=prev_state,
            min_trades_floor=50,
        ))
        assert report1.verdict == EdgeVerdict.NEGATIVE
        assert state1.negative_weeks == 3
        # Second run: same period, state already saved at 3.
        report2, state2 = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=state1,  # the saved post-run state
            min_trades_floor=50,
        ))
        # Verdict stays NEGATIVE (alarm already firing); state stays
        # at 3, NOT incremented to 4.
        assert report2.verdict == EdgeVerdict.NEGATIVE
        assert state2.negative_weeks == 3

    def test_same_day_rerun_at_week_2_does_not_trip_early(self, db_conn):
        """The exact scenario the reviewer flagged: week-2 state saved,
        operator reruns the SAME period — must NOT project negative_weeks
        + 1 = 3 and fire the alarm. Pre-fix this bug fired NEGATIVE on
        the rerun even though apply_verdict's idempotency would have
        kept state at 2."""
        self._seed_overwhelming_negative_in_window(db_conn)
        env = _make_envelope(r_ci=(0.20, 0.70))
        # State as if the week-2 assessment was already saved (last
        # week's run completed and persisted as NEGATIVE-eligible).
        saved_state = PersistenceState(
            negative_weeks=2,
            last_check="2026-05-25",  # this is the END of the week-2 period
            last_verdict="NEGATIVE",
        )
        # Operator re-runs the SAME period (period_end=2026-05-25).
        report, state = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            envelope=env,
            conn=db_conn,
            persistence_state=saved_state,
            min_trades_floor=50,
        ))
        # Critical assertion: verdict must NOT be NEGATIVE; state must
        # stay at 2. The alarm fires only at week 3 (the NEXT week's
        # run with period_end=2026-06-01).
        assert report.verdict != EdgeVerdict.NEGATIVE
        assert state.negative_weeks == 2

    def test_next_week_rerun_correctly_increments_to_3(self, db_conn):
        """Sanity: the NEXT week (period_end=2026-06-01) DOES
        increment to 3 and fire the alarm. Demonstrates the
        idempotency fix didn't break the normal flow."""
        # Seed both week-2 AND week-3 data so each period read is
        # CONCLUSIVE on its own.
        for week_start in [date(2026, 5, 18), date(2026, 5, 25)]:
            for i in range(120):
                day_offset = i % 7
                day = (week_start.toordinal() + day_offset)
                ts_date = date.fromordinal(day).isoformat()
                _seed_closed_trade(
                    db_conn, strategy="x",
                    timestamp=f"{ts_date}T{i % 24:02d}:{i % 60:02d}:00",
                    realized_pnl=-200.0, r_multiple=-2.0,
                )
        env = _make_envelope(r_ci=(0.20, 0.70))
        saved_state = PersistenceState(
            negative_weeks=2, last_check="2026-05-25", last_verdict="NEGATIVE",
        )
        # NEW period: period_end=2026-06-01
        report, state = EdgeAssessor().assess(EdgeInputs(
            strategy_name="x",
            period_start=date(2026, 5, 25),
            period_end=date(2026, 6, 1),
            envelope=env,
            conn=db_conn,
            persistence_state=saved_state,
            min_trades_floor=50,
        ))
        # Different date → not idempotent → counter increments → alarm fires
        assert report.verdict == EdgeVerdict.NEGATIVE
        assert state.negative_weeks == 3
