"""Tests for the read-only Market Observatory projection."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from reporting.market_observation_reader import load_preopen_dashboard_snapshot


def _create_reader_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE observation_jobs (
                logical_key TEXT PRIMARY KEY,
                session_date TEXT NOT NULL,
                checkpoint TEXT NOT NULL,
                checkpoint_at TEXT NOT NULL
            );
            CREATE TABLE preopen_observations (
                observation_id TEXT PRIMARY KEY,
                logical_key TEXT NOT NULL,
                revision INTEGER NOT NULL,
                universe_version TEXT NOT NULL,
                layer TEXT NOT NULL,
                session_date TEXT NOT NULL,
                checkpoint TEXT NOT NULL,
                checkpoint_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                collected_at TEXT NOT NULL,
                collection_state TEXT NOT NULL,
                symbol TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                observation_role TEXT NOT NULL,
                feed TEXT NOT NULL,
                is_consolidated INTEGER NOT NULL,
                quote_timestamp TEXT,
                reference_price REAL,
                mark_price REAL,
                spread_bps REAL,
                gap_pct REAL,
                quote_age_seconds REAL,
                freshness_state TEXT,
                observation_status TEXT NOT NULL,
                reason_code TEXT
            );
            """
        )


def _insert_job(
    path: Path,
    *,
    logical_key: str,
    symbol: str,
    checkpoint: str = "open_minus_90m",
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO observation_jobs (
                logical_key, session_date, checkpoint, checkpoint_at
            ) VALUES (?, '2026-11-04', ?, '2026-11-04T13:00:00+00:00')
            """,
            (logical_key, checkpoint),
        )


def _insert_observation(
    path: Path,
    *,
    logical_key: str,
    symbol: str,
    revision: int = 1,
    gap_pct: float = 0.01,
) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO preopen_observations (
                observation_id, logical_key, revision, universe_version,
                layer, session_date, checkpoint, checkpoint_at, available_at,
                collected_at, collection_state, symbol, asset_type,
                observation_role, feed, is_consolidated, quote_timestamp,
                reference_price, mark_price, spread_bps, gap_pct,
                quote_age_seconds, freshness_state, observation_status,
                reason_code
            ) VALUES (
                ?, ?, ?, 'u-test', 'sip_checkpoint', '2026-11-04',
                'open_minus_90m', '2026-11-04T13:00:00+00:00',
                '2026-11-04T13:16:00+00:00',
                '2026-11-04T13:20:00+00:00', 'on_schedule', ?, 'etf',
                'broad_market', 'sip', 1, '2026-11-04T12:59:59+00:00',
                100.0, 101.0, 2.5, ?, 1.0, 'fresh', 'complete', NULL
            )
            """,
            (f"observation-{logical_key}-{revision}", logical_key, revision, symbol, gap_pct),
        )


class TestPreOpenDashboardReader:
    def test_missing_database_is_not_created(self, tmp_path: Path) -> None:
        path = tmp_path / "missing.db"

        snapshot = load_preopen_dashboard_snapshot(path)

        assert snapshot.rows == ()
        assert snapshot.load_error is None
        assert not path.exists()

    def test_coverage_uses_expected_jobs_and_supports_arbitrary_symbols(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "observations.db"
        _create_reader_db(path)
        _insert_job(path, logical_key="aapl-key", symbol="AAPL")
        _insert_job(path, logical_key="iwm-key", symbol="IWM")
        _insert_observation(path, logical_key="aapl-key", symbol="AAPL")

        snapshot = load_preopen_dashboard_snapshot(path)

        assert snapshot.session_date == "2026-11-04"
        assert snapshot.expected_count == 2
        assert snapshot.finalized_count == 1
        assert snapshot.complete_count == 1
        assert snapshot.fresh_count == 1
        assert [row.symbol for row in snapshot.rows] == ["AAPL"]

    def test_latest_correction_revision_is_projected(self, tmp_path: Path) -> None:
        path = tmp_path / "observations.db"
        _create_reader_db(path)
        _insert_job(path, logical_key="iwm-key", symbol="IWM")
        _insert_observation(
            path,
            logical_key="iwm-key",
            symbol="IWM",
            revision=1,
            gap_pct=0.01,
        )
        _insert_observation(
            path,
            logical_key="iwm-key",
            symbol="IWM",
            revision=2,
            gap_pct=0.02,
        )

        snapshot = load_preopen_dashboard_snapshot(path)

        assert snapshot.finalized_count == 1
        assert snapshot.rows[0].revision == 2
        assert snapshot.rows[0].gap_pct == 0.02

    def test_incomplete_schema_returns_explicit_error(self, tmp_path: Path) -> None:
        path = tmp_path / "observations.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE unrelated (value TEXT)")

        snapshot = load_preopen_dashboard_snapshot(path)

        assert snapshot.rows == ()
        assert snapshot.load_error == (
            "Market observation database is missing the expected schema."
        )
