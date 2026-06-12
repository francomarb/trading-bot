"""
Unit tests for the migration preflight (foundation commit 2).

Covers the preflight pre-existing-duplicate detection wired into
``reporting.logger.TradeLogger._ensure_db`` before the new
``uniq_one_active_position_per_owner_key`` partial unique index is
applied.

Per discovery doc §12.2 (PR #59 review-7 P1b + R9-P1c): partial-
migration mode is unsafe. If duplicate rows exist that would block
the new index, the bot must abort startup with a structured
``MigrationDuplicatesFound`` exception rather than silently continuing
in a degraded mixed-mode.

§12.1 test matrix items covered by this file:

- Test 18 (dedupe-script detection-only default — preflight is its
  on-startup counterpart; both share the no-silent-mutation property)
- Test 19 (preflight status set matches the index's WHERE clause)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from engine.lifecycle import PositionLifecycleStore, new_position_uid
from engine.lifecycle_orders import (
    MigrationDuplicatesFound,
    OwnerKeyDuplicate,
    detect_owner_key_duplicates,
    run_preflight_or_raise,
)
from reporting.logger import TradeLogger


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "trades.db")


def _seed_lifecycle_row(
    conn: sqlite3.Connection,
    *,
    position_uid: str,
    owner_key: str,
    status: str,
    symbol: str | None = None,
) -> None:
    """Insert a position_lifecycle row directly bypassing the store API.
    Used to simulate pre-existing duplicates from prior bot versions."""
    conn.execute(
        """
        INSERT INTO position_lifecycle (
            schema_version, position_uid, created_at, closed_at,
            symbol, owner_key, strategy, position_type, status,
            entry_qty, current_qty, avg_entry_price,
            net_realized_pnl,
            entry_order_id, entry_client_order_id,
            first_fill_at, last_fill_at, metadata_json
        ) VALUES (
            1, ?, '2026-06-12T00:00:00+00:00', NULL,
            ?, ?, 'sma_crossover', 'single_leg', ?,
            10, 10, 100.0,
            0.0,
            NULL, NULL,
            NULL, NULL, NULL
        )
        """,
        (position_uid, symbol or owner_key, owner_key, status),
    )
    conn.commit()


# ── No-duplicates happy path ────────────────────────────────────────────────


class TestPreflightWithCleanDatabase:
    """A fresh, clean database must pass preflight silently."""

    def test_preflight_on_clean_db_returns_none(self, tmp_db_path: str):
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        # No raise — and the call returns None.
        result = run_preflight_or_raise(conn)
        assert result is None

    def test_detect_returns_empty_tuple_on_clean_db(self, tmp_db_path: str):
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        assert detect_owner_key_duplicates(conn) == ()

    def test_ensure_db_succeeds_on_clean_db(self, tmp_db_path: str):
        """The end-to-end bootstrap path must not raise on a clean DB.
        Implicit guard: commit 1's schema tests would have failed if
        preflight broke the bootstrap."""
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        # And re-open works (idempotency).
        TradeLogger(path=tmp_db_path)._ensure_db()
        assert conn is not None


# ── Duplicate detection ────────────────────────────────────────────────────


class TestPreflightDetection:
    """Preflight must surface every pre-existing duplicate cluster
    before the CREATE UNIQUE INDEX runs."""

    def _bootstrap_without_lock_index(self, tmp_db_path: str) -> sqlite3.Connection:
        """Bootstrap the schema but skip the new owner_key lock index
        creation so we can seed duplicates that would otherwise be
        rejected at INSERT time."""
        TradeLogger(path=tmp_db_path)._ensure_db()
        # Drop the lock index so we can seed duplicates (the index
        # was created idempotently; dropping it lets us simulate
        # the migration-from-legacy-DB scenario).
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "DROP INDEX IF EXISTS uniq_one_active_position_per_owner_key"
        )
        conn.commit()
        return conn

    def test_detect_pending_pending_duplicate(self, tmp_db_path: str):
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        uid_a, uid_b = new_position_uid(), new_position_uid()
        _seed_lifecycle_row(
            conn, position_uid=uid_a, owner_key="AAPL", status="pending"
        )
        _seed_lifecycle_row(
            conn, position_uid=uid_b, owner_key="AAPL", status="pending"
        )
        duplicates = detect_owner_key_duplicates(conn)
        assert len(duplicates) == 1
        dup = duplicates[0]
        assert dup.owner_key == "AAPL"
        assert dup.count == 2
        assert set(dup.position_uids) == {uid_a, uid_b}

    def test_detect_open_error_duplicate_includes_error_status(
        self, tmp_db_path: str
    ):
        """The preflight status set MUST include 'error' to match the
        index's WHERE clause. R9-P1c: an error+open duplicate would
        otherwise slip through preflight and break CREATE INDEX."""
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        uid_a, uid_b = new_position_uid(), new_position_uid()
        _seed_lifecycle_row(
            conn, position_uid=uid_a, owner_key="MSFT", status="open"
        )
        _seed_lifecycle_row(
            conn, position_uid=uid_b, owner_key="MSFT", status="error"
        )
        duplicates = detect_owner_key_duplicates(conn)
        assert len(duplicates) == 1
        assert duplicates[0].owner_key == "MSFT"
        assert duplicates[0].count == 2

    def test_detect_closed_and_open_NOT_flagged(self, tmp_db_path: str):
        """A `closed` row + an `open` row on the same owner_key is
        NOT a duplicate — only one is non-terminal. The lock predicate
        excludes terminal statuses, so preflight must too."""
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        uid_a, uid_b = new_position_uid(), new_position_uid()
        _seed_lifecycle_row(
            conn, position_uid=uid_a, owner_key="NVDA", status="closed"
        )
        _seed_lifecycle_row(
            conn, position_uid=uid_b, owner_key="NVDA", status="open"
        )
        assert detect_owner_key_duplicates(conn) == ()

    def test_detect_multiple_owner_key_clusters(self, tmp_db_path: str):
        """Preflight should surface every distinct duplicate cluster
        in the report, not just the first."""
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        for owner_key in ("AAPL", "TSLA"):
            for _ in range(2):
                _seed_lifecycle_row(
                    conn,
                    position_uid=new_position_uid(),
                    owner_key=owner_key,
                    status="pending",
                )
        # Plus a clean row that should NOT appear in the report.
        _seed_lifecycle_row(
            conn,
            position_uid=new_position_uid(),
            owner_key="GOOG",
            status="open",
        )
        duplicates = detect_owner_key_duplicates(conn)
        owner_keys = {d.owner_key for d in duplicates}
        assert owner_keys == {"AAPL", "TSLA"}


# ── Abort-startup behavior ─────────────────────────────────────────────────


class TestPreflightRaisesOnConflict:
    """When duplicates exist, the preflight must raise
    MigrationDuplicatesFound with structured detail. Partial-
    migration mode is NOT a safe alternative (PR #59 R7-P1b)."""

    def _bootstrap_without_lock_index(self, tmp_db_path: str) -> sqlite3.Connection:
        TradeLogger(path=tmp_db_path)._ensure_db()
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "DROP INDEX IF EXISTS uniq_one_active_position_per_owner_key"
        )
        conn.commit()
        return conn

    def test_run_preflight_raises_with_structured_duplicates(
        self, tmp_db_path: str
    ):
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        for _ in range(2):
            _seed_lifecycle_row(
                conn,
                position_uid=new_position_uid(),
                owner_key="AAPL",
                status="pending",
            )
        with pytest.raises(MigrationDuplicatesFound) as exc_info:
            run_preflight_or_raise(conn)
        # The exception carries structured detail consumers can iterate.
        err = exc_info.value
        assert len(err.owner_key_duplicates) == 1
        assert err.owner_key_duplicates[0].owner_key == "AAPL"
        # Stringification mentions the affected owner_key for log/alert visibility.
        assert "AAPL" in str(err)

    def test_exception_message_references_remediation_script(
        self, tmp_db_path: str
    ):
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        for _ in range(2):
            _seed_lifecycle_row(
                conn,
                position_uid=new_position_uid(),
                owner_key="AAPL",
                status="pending",
            )
        with pytest.raises(MigrationDuplicatesFound) as exc_info:
            run_preflight_or_raise(conn)
        assert "scripts/migrate_dedupe_trades.py" in str(exc_info.value)

    def test_ensure_db_aborts_startup_on_pre_existing_duplicates(
        self, tmp_db_path: str
    ):
        """End-to-end: re-open _ensure_db on a DB with pre-existing
        duplicates and assert it raises MigrationDuplicatesFound
        rather than partial-migrating in a mixed mode."""
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        for _ in range(2):
            _seed_lifecycle_row(
                conn,
                position_uid=new_position_uid(),
                owner_key="AAPL",
                status="pending",
            )
        conn.close()
        # Fresh TradeLogger opens, runs the bootstrap, encounters
        # the duplicates during preflight, raises.
        with pytest.raises(MigrationDuplicatesFound):
            TradeLogger(path=tmp_db_path)._ensure_db()


# ── No silent auto-mutation ────────────────────────────────────────────────


class TestPreflightHasNoSideEffects:
    """The preflight is detection-only — it must NOT mutate the
    database. This pairs with R8-2's no-auto-delete principle for
    the operator-runnable dedupe script. The detection runs the same
    code path used by both the on-startup preflight and the script's
    --dry-run mode."""

    def _bootstrap_without_lock_index(self, tmp_db_path: str) -> sqlite3.Connection:
        TradeLogger(path=tmp_db_path)._ensure_db()
        conn = sqlite3.connect(tmp_db_path)
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "DROP INDEX IF EXISTS uniq_one_active_position_per_owner_key"
        )
        conn.commit()
        return conn

    def test_detect_does_not_modify_rows(self, tmp_db_path: str):
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        uids = [new_position_uid() for _ in range(2)]
        for uid in uids:
            _seed_lifecycle_row(
                conn,
                position_uid=uid,
                owner_key="AAPL",
                status="pending",
            )
        # Snapshot of row count + the row data.
        rows_before = conn.execute(
            "SELECT position_uid, owner_key, status FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        detect_owner_key_duplicates(conn)
        rows_after = conn.execute(
            "SELECT position_uid, owner_key, status FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        assert rows_before == rows_after

    def test_run_preflight_or_raise_does_not_modify_rows(
        self, tmp_db_path: str
    ):
        conn = self._bootstrap_without_lock_index(tmp_db_path)
        for _ in range(2):
            _seed_lifecycle_row(
                conn,
                position_uid=new_position_uid(),
                owner_key="AAPL",
                status="pending",
            )
        rows_before = conn.execute(
            "SELECT position_uid, owner_key, status FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        with pytest.raises(MigrationDuplicatesFound):
            run_preflight_or_raise(conn)
        rows_after = conn.execute(
            "SELECT position_uid, owner_key, status FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        assert rows_before == rows_after
