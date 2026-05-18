"""
Unit tests for strategies/health/lifecycle.py + the
strategy_lifecycle_counters table migration in reporting/logger.py.

Coverage:
  - Migration: table exists after TradeLogger init, with the expected
    columns + UNIQUE constraint.
  - Upsert semantics: same (period_type, period_start, strategy)
    accumulates (additive, not overwrite). Multiple distinct keys
    coexist.
  - Idempotency on identical inserts: re-inserting the same counter
    delta increments by 2× (additive); operator must guard against
    double-counts at the caller layer (engine wraps in cycle batching).
  - Input validation: bad period_type rejected; empty strategy
    rejected; bad date ordering rejected.
  - Read API: sums across rows in [start, end); zero-rows returns
    zero-filled LifecycleCounters; period_type filter works.
  - LifecycleCounters arithmetic: __add__ field-wise; as_dict shape.
  - Threading: concurrent upserts to distinct strategies all land
    without lost updates.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from reporting.logger import TradeLogger
from strategies.health.lifecycle import (
    LIFECYCLE_TABLE_SCHEMA_VERSION,
    VALID_PERIOD_TYPES,
    LifecycleCounters,
    list_strategies,
    read_counters_for_period,
    upsert_counters,
)


# ── Helpers ───────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path: Path):
    """A TradeLogger-initialized SQLite connection in tmp_path."""
    logger = TradeLogger(path=str(tmp_path / "trades.db"))
    conn = logger._ensure_db()
    yield conn
    logger.close()


# ── Migration ─────────────────────────────────────────────────────────


class TestMigration:
    def test_table_exists_after_trade_logger_init(self, db_conn):
        rows = db_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='strategy_lifecycle_counters'"
        ).fetchall()
        assert len(rows) == 1

    def test_table_has_expected_columns(self, db_conn):
        cols = {
            row[1]: row[2]
            for row in db_conn.execute(
                "PRAGMA table_info(strategy_lifecycle_counters)"
            ).fetchall()
        }
        # Required structure from design §12.4.1.
        for required in (
            "id", "schema_version", "period_start", "period_end",
            "period_type", "strategy_name",
            "raw_signals", "regime_blocked", "edge_filter_blocked",
            "sleeve_blocked", "risk_blocked", "submitted", "filled_entries",
        ):
            assert required in cols, f"missing column {required}"

    def test_unique_constraint_present(self, db_conn):
        """The UNIQUE constraint on (period_type, period_start, strategy_name)
        is the backbone of the upsert pattern — without it duplicates
        accumulate as separate rows instead of summing."""
        # First insert succeeds.
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start="2026-05-18",
            period_end="2026-05-25",
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=1),
        )
        # Raw INSERT (bypassing ON CONFLICT) must fail.
        with pytest.raises(sqlite3.IntegrityError):
            db_conn.execute(
                "INSERT INTO strategy_lifecycle_counters "
                "(period_start, period_end, period_type, strategy_name, "
                "raw_signals, regime_blocked, edge_filter_blocked, "
                "sleeve_blocked, risk_blocked, submitted, filled_entries) "
                "VALUES (?, ?, ?, ?, 1, 0, 0, 0, 0, 0, 0)",
                ("2026-05-18", "2026-05-25", "weekly", "x"),
            )

    def test_migration_idempotent(self, tmp_path: Path):
        """Reinitializing TradeLogger on the same DB must not raise."""
        path = str(tmp_path / "trades.db")
        TradeLogger(path=path)._ensure_db().close()
        TradeLogger(path=path)._ensure_db().close()
        TradeLogger(path=path)._ensure_db().close()  # third time, why not

    def test_schema_version_defaults_to_1(self, db_conn):
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start="2026-05-18",
            period_end="2026-05-25",
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=5),
        )
        row = db_conn.execute(
            "SELECT schema_version FROM strategy_lifecycle_counters "
            "WHERE strategy_name='x'"
        ).fetchone()
        assert row[0] == LIFECYCLE_TABLE_SCHEMA_VERSION


# ── Upsert semantics ──────────────────────────────────────────────────


class TestUpsertSemantics:
    def test_insert_new_row(self, db_conn):
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start="2026-05-18",
            period_end="2026-05-25",
            strategy_name="donchian_breakout",
            counters=LifecycleCounters(raw_signals=10, submitted=4),
        )
        rows = db_conn.execute(
            "SELECT raw_signals, submitted FROM strategy_lifecycle_counters "
            "WHERE strategy_name='donchian_breakout'"
        ).fetchall()
        assert rows == [(10, 4)]

    def test_upsert_accumulates_not_overwrites(self, db_conn):
        """Per design §12.4.1: per-cycle flushes within the same period
        add to the same row. Re-upserting with new counts MUST add,
        not replace."""
        kwargs = {
            "period_type": "weekly",
            "period_start": "2026-05-18",
            "period_end": "2026-05-25",
            "strategy_name": "donchian_breakout",
        }
        upsert_counters(
            db_conn, **kwargs, counters=LifecycleCounters(raw_signals=10)
        )
        upsert_counters(
            db_conn, **kwargs, counters=LifecycleCounters(raw_signals=5)
        )
        row = db_conn.execute(
            "SELECT raw_signals FROM strategy_lifecycle_counters "
            "WHERE strategy_name='donchian_breakout'"
        ).fetchone()
        assert row[0] == 15  # accumulated, not overwritten

    def test_distinct_strategies_coexist(self, db_conn):
        kwargs_common = {
            "period_type": "weekly",
            "period_start": "2026-05-18",
            "period_end": "2026-05-25",
        }
        upsert_counters(
            db_conn, **kwargs_common, strategy_name="a",
            counters=LifecycleCounters(raw_signals=1),
        )
        upsert_counters(
            db_conn, **kwargs_common, strategy_name="b",
            counters=LifecycleCounters(raw_signals=2),
        )
        upsert_counters(
            db_conn, **kwargs_common, strategy_name="c",
            counters=LifecycleCounters(raw_signals=3),
        )
        rows = db_conn.execute(
            "SELECT strategy_name, raw_signals FROM strategy_lifecycle_counters "
            "ORDER BY strategy_name"
        ).fetchall()
        assert rows == [("a", 1), ("b", 2), ("c", 3)]

    def test_distinct_periods_coexist_same_strategy(self, db_conn):
        for start, end in [
            ("2026-05-18", "2026-05-25"),
            ("2026-05-25", "2026-06-01"),
            ("2026-06-01", "2026-06-08"),
        ]:
            upsert_counters(
                db_conn,
                period_type="weekly",
                period_start=start,
                period_end=end,
                strategy_name="donchian_breakout",
                counters=LifecycleCounters(raw_signals=1),
            )
        count = db_conn.execute(
            "SELECT COUNT(*) FROM strategy_lifecycle_counters "
            "WHERE strategy_name='donchian_breakout'"
        ).fetchone()[0]
        assert count == 3

    def test_weekly_and_monthly_for_same_period_coexist(self, db_conn):
        """period_type is part of the UNIQUE constraint; weekly and
        monthly rows for the same start can both exist."""
        common = {
            "period_start": "2026-05-01",
            "period_end": "2026-05-08",
            "strategy_name": "x",
        }
        upsert_counters(
            db_conn, period_type="weekly", **common,
            counters=LifecycleCounters(raw_signals=1),
        )
        upsert_counters(
            db_conn,
            period_type="monthly",
            period_start="2026-05-01",
            period_end="2026-06-01",
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=10),
        )
        rows = db_conn.execute(
            "SELECT period_type, raw_signals FROM strategy_lifecycle_counters "
            "WHERE strategy_name='x' ORDER BY period_type"
        ).fetchall()
        assert rows == [("monthly", 10), ("weekly", 1)]


# ── Input validation ──────────────────────────────────────────────────


class TestInputValidation:
    def test_invalid_period_type_rejected(self, db_conn):
        with pytest.raises(ValueError, match="period_type"):
            upsert_counters(
                db_conn,
                period_type="hourly",  # not in VALID_PERIOD_TYPES
                period_start="2026-05-18",
                period_end="2026-05-25",
                strategy_name="x",
                counters=LifecycleCounters(),
            )

    def test_empty_strategy_rejected(self, db_conn):
        with pytest.raises(ValueError, match="strategy_name"):
            upsert_counters(
                db_conn,
                period_type="weekly",
                period_start="2026-05-18",
                period_end="2026-05-25",
                strategy_name="",
                counters=LifecycleCounters(),
            )

    def test_period_end_not_after_start_rejected(self, db_conn):
        with pytest.raises(ValueError, match="period_end"):
            upsert_counters(
                db_conn,
                period_type="weekly",
                period_start="2026-05-25",
                period_end="2026-05-18",  # before start
                strategy_name="x",
                counters=LifecycleCounters(),
            )

    def test_same_start_end_rejected(self, db_conn):
        with pytest.raises(ValueError, match="period_end"):
            upsert_counters(
                db_conn,
                period_type="weekly",
                period_start="2026-05-18",
                period_end="2026-05-18",  # zero-width period
                strategy_name="x",
                counters=LifecycleCounters(),
            )

    def test_date_object_accepted(self, db_conn):
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=date(2026, 5, 18),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=1),
        )
        row = db_conn.execute(
            "SELECT period_start, period_end FROM strategy_lifecycle_counters "
            "WHERE strategy_name='x'"
        ).fetchone()
        assert row == ("2026-05-18", "2026-05-25")

    def test_timestamp_with_time_component_rejected(self, db_conn):
        """Storing 'Mon 16:30' instead of 'Mon' would break UNIQUE
        idempotency between cycles — reject upfront."""
        with pytest.raises(ValueError):
            upsert_counters(
                db_conn,
                period_type="weekly",
                period_start="2026-05-18T12:00:00",  # has time
                period_end="2026-05-25",
                strategy_name="x",
                counters=LifecycleCounters(),
            )

    def test_datetime_object_rejected(self, db_conn):
        """PR #18 reviewer regression: datetime IS a date subclass.
        An unguarded `isinstance(value, date)` would accept
        `datetime.now()` and store a full ISO timestamp, fragmenting
        UNIQUE keys between cycle flushes at different wall-clock
        times. Reject explicitly so the engine wiring in 11.10f
        has to call .date() at the call site."""
        dt = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
        with pytest.raises(TypeError, match="datetime input rejected"):
            upsert_counters(
                db_conn,
                period_type="weekly",
                period_start=dt,
                period_end="2026-05-25",
                strategy_name="x",
                counters=LifecycleCounters(),
            )

    def test_naive_datetime_also_rejected(self, db_conn):
        """Naive datetime (no tzinfo) is still a date subclass — reject."""
        naive = datetime(2026, 5, 18, 0, 0, 0)
        with pytest.raises(TypeError, match="datetime input rejected"):
            upsert_counters(
                db_conn,
                period_type="weekly",
                period_start="2026-05-18",
                period_end=naive,
                strategy_name="x",
                counters=LifecycleCounters(),
            )

    def test_datetime_date_method_works(self, db_conn):
        """Documented workaround: callers with a datetime call .date()."""
        dt = datetime(2026, 5, 18, 16, 30, 0, tzinfo=timezone.utc)
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start=dt.date(),
            period_end=date(2026, 5, 25),
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=1),
        )
        row = db_conn.execute(
            "SELECT period_start, period_end FROM strategy_lifecycle_counters "
            "WHERE strategy_name='x'"
        ).fetchone()
        assert row == ("2026-05-18", "2026-05-25")

    def test_two_flushes_with_different_times_same_day_accumulate(self, db_conn):
        """Regression for the exact fragmentation scenario the reviewer
        described: two cycle flushes within the same week, intended to
        target the same period row. If the engine wiring (11.10f)
        properly uses .date(), both upserts land on the UNIQUE key and
        accumulate. (Pre-fix, passing the raw datetimes would have
        produced two distinct rows.)"""
        morning = datetime(2026, 5, 18, 9, 35, 0, tzinfo=timezone.utc)
        afternoon = datetime(2026, 5, 18, 15, 50, 0, tzinfo=timezone.utc)
        # The engine must coerce to .date() at the call site. This test
        # documents the contract: when callers do so, accumulation works.
        for ts, raw in [(morning, 3), (afternoon, 7)]:
            upsert_counters(
                db_conn,
                period_type="weekly",
                period_start=ts.date(),
                period_end=date(2026, 5, 25),
                strategy_name="x",
                counters=LifecycleCounters(raw_signals=raw),
            )
        # One row, raw_signals = 10 (accumulated, not 2 rows of 3 + 7).
        rows = db_conn.execute(
            "SELECT COUNT(*), SUM(raw_signals) FROM strategy_lifecycle_counters "
            "WHERE strategy_name='x'"
        ).fetchone()
        assert rows == (1, 10)


# ── Read API ──────────────────────────────────────────────────────────


class TestReadCounters:
    def test_empty_table_returns_zeros(self, db_conn):
        result = read_counters_for_period(
            db_conn,
            strategy_name="any",
            start="2026-05-18",
            end="2026-05-25",
        )
        assert result == LifecycleCounters()  # all zeros

    def test_single_row_returns_its_values(self, db_conn):
        upsert_counters(
            db_conn,
            period_type="weekly",
            period_start="2026-05-18",
            period_end="2026-05-25",
            strategy_name="x",
            counters=LifecycleCounters(
                raw_signals=10, regime_blocked=2, submitted=5,
            ),
        )
        result = read_counters_for_period(
            db_conn, strategy_name="x",
            start="2026-05-18", end="2026-05-26",
        )
        assert result.raw_signals == 10
        assert result.regime_blocked == 2
        assert result.submitted == 5

    def test_sums_across_multiple_weekly_rows(self, db_conn):
        """4 consecutive weeks of weekly rows summed for the month."""
        for start, end, raw in [
            ("2026-05-04", "2026-05-11", 10),
            ("2026-05-11", "2026-05-18", 12),
            ("2026-05-18", "2026-05-25", 8),
            ("2026-05-25", "2026-06-01", 15),
        ]:
            upsert_counters(
                db_conn, period_type="weekly",
                period_start=start, period_end=end,
                strategy_name="x",
                counters=LifecycleCounters(raw_signals=raw),
            )
        result = read_counters_for_period(
            db_conn, strategy_name="x",
            start="2026-05-04", end="2026-06-01",
            period_type="weekly",
        )
        assert result.raw_signals == 10 + 12 + 8 + 15

    def test_end_is_exclusive(self, db_conn):
        """[start, end) — period_start == end is excluded."""
        for start, end in [
            ("2026-05-18", "2026-05-25"),
            ("2026-05-25", "2026-06-01"),  # this row's start == 'end' arg below
        ]:
            upsert_counters(
                db_conn, period_type="weekly",
                period_start=start, period_end=end,
                strategy_name="x",
                counters=LifecycleCounters(raw_signals=1),
            )
        result = read_counters_for_period(
            db_conn, strategy_name="x",
            start="2026-05-18", end="2026-05-25",  # excludes the second row
            period_type="weekly",
        )
        assert result.raw_signals == 1

    def test_period_type_filter_excludes_other_types(self, db_conn):
        upsert_counters(
            db_conn, period_type="weekly",
            period_start="2026-05-04", period_end="2026-05-11",
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=10),
        )
        upsert_counters(
            db_conn, period_type="monthly",
            period_start="2026-05-01", period_end="2026-06-01",
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=100),
        )
        weekly = read_counters_for_period(
            db_conn, strategy_name="x",
            start="2026-05-01", end="2026-06-01",
            period_type="weekly",
        )
        monthly = read_counters_for_period(
            db_conn, strategy_name="x",
            start="2026-05-01", end="2026-06-01",
            period_type="monthly",
        )
        no_filter = read_counters_for_period(
            db_conn, strategy_name="x",
            start="2026-05-01", end="2026-06-01",
        )
        assert weekly.raw_signals == 10
        assert monthly.raw_signals == 100
        assert no_filter.raw_signals == 110  # both, without filter

    def test_different_strategy_isolated(self, db_conn):
        upsert_counters(
            db_conn, period_type="weekly",
            period_start="2026-05-18", period_end="2026-05-25",
            strategy_name="x",
            counters=LifecycleCounters(raw_signals=10),
        )
        upsert_counters(
            db_conn, period_type="weekly",
            period_start="2026-05-18", period_end="2026-05-25",
            strategy_name="y",
            counters=LifecycleCounters(raw_signals=99),
        )
        result = read_counters_for_period(
            db_conn, strategy_name="x",
            start="2026-05-18", end="2026-05-25",
        )
        assert result.raw_signals == 10  # NOT 109

    def test_read_validation_mirrors_upsert(self, db_conn):
        with pytest.raises(ValueError, match="strategy_name"):
            read_counters_for_period(
                db_conn, strategy_name="",
                start="2026-05-18", end="2026-05-25",
            )
        with pytest.raises(ValueError, match="end"):
            read_counters_for_period(
                db_conn, strategy_name="x",
                start="2026-05-25", end="2026-05-18",
            )


# ── list_strategies ───────────────────────────────────────────────────


class TestListStrategies:
    def test_empty_table_returns_empty_list(self, db_conn):
        assert list_strategies(db_conn) == []

    def test_returns_distinct_sorted(self, db_conn):
        for s in ["zzz", "aaa", "mmm", "aaa"]:
            upsert_counters(
                db_conn, period_type="weekly",
                period_start="2026-05-18", period_end="2026-05-25",
                strategy_name=s,
                counters=LifecycleCounters(raw_signals=1),
            )
        assert list_strategies(db_conn) == ["aaa", "mmm", "zzz"]


# ── LifecycleCounters arithmetic ──────────────────────────────────────


class TestLifecycleCountersArithmetic:
    def test_add_is_field_wise(self):
        a = LifecycleCounters(raw_signals=10, regime_blocked=2, submitted=5)
        b = LifecycleCounters(raw_signals=3, edge_filter_blocked=4, filled_entries=2)
        c = a + b
        assert c.raw_signals == 13
        assert c.regime_blocked == 2
        assert c.edge_filter_blocked == 4
        assert c.submitted == 5
        assert c.filled_entries == 2

    def test_add_rejects_non_counter(self):
        a = LifecycleCounters(raw_signals=1)
        assert a.__add__(42) is NotImplemented

    def test_as_dict_shape(self):
        c = LifecycleCounters(raw_signals=1, risk_blocked=2)
        d = c.as_dict()
        # All 7 counter fields present in dict
        for k in ("raw_signals", "regime_blocked", "edge_filter_blocked",
                  "sleeve_blocked", "risk_blocked", "submitted",
                  "filled_entries"):
            assert k in d
        assert d["raw_signals"] == 1
        assert d["risk_blocked"] == 2


# ── Concurrent upsert safety ──────────────────────────────────────────


class TestConcurrentUpserts:
    """SQLite serializes writes via its journal — even from multiple
    threads with the same connection. The upsert API should never lose
    updates between distinct strategies running in parallel."""

    def test_distinct_strategies_concurrent(self, tmp_path: Path):
        """10 threads each upsert their own strategy. All 10 rows must
        exist and have correct counts at the end."""
        # One TradeLogger per thread to avoid sqlite3 connection
        # sharing issues (sqlite3 connections aren't thread-safe by
        # default; design is one-connection-per-thread for writes).
        path = str(tmp_path / "trades.db")
        TradeLogger(path=path)._ensure_db().close()  # ensure schema

        def upsert_for(idx: int) -> None:
            logger = TradeLogger(path=path)
            conn = logger._ensure_db()
            upsert_counters(
                conn,
                period_type="weekly",
                period_start="2026-05-18",
                period_end="2026-05-25",
                strategy_name=f"s_{idx}",
                counters=LifecycleCounters(raw_signals=idx),
            )
            logger.close()

        threads = [threading.Thread(target=upsert_for, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        verify = TradeLogger(path=path)
        conn = verify._ensure_db()
        try:
            rows = conn.execute(
                "SELECT strategy_name, raw_signals "
                "FROM strategy_lifecycle_counters ORDER BY strategy_name"
            ).fetchall()
            assert len(rows) == 10
            # Each strategy_i should have raw_signals=i
            by_strategy = {name: raw for name, raw in rows}
            for i in range(10):
                assert by_strategy[f"s_{i}"] == i
        finally:
            verify.close()


# ── VALID_PERIOD_TYPES exposed ────────────────────────────────────────


class TestValidPeriodTypes:
    def test_constant_exposes_weekly_and_monthly(self):
        assert "weekly" in VALID_PERIOD_TYPES
        assert "monthly" in VALID_PERIOD_TYPES
