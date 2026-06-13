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


# ── Trades-side duplicate detection (PR #60 commit 7 review) ────────────────


def _make_legacy_trades_conn(path: str) -> sqlite3.Connection:
    """Open a raw sqlite3 connection and bootstrap a minimal trades
    table mirroring the pre-PR-60 schema (no partial UNIQUE on
    order_id). Used to simulate a legacy DB shape against which the
    foundation migration must run preflight before mutating anything.
    """
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS trades ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT, symbol TEXT, side TEXT, qty REAL, "
        "avg_fill_price REAL, order_id TEXT, strategy TEXT, "
        "reason TEXT, stop_price REAL, entry_reference_price REAL, "
        "modeled_slippage_bps REAL, realized_slippage_bps REAL, "
        "order_type TEXT, status TEXT, requested_qty REAL, "
        "filled_qty REAL, position_type TEXT)"
    )
    return conn


def _seed_trades_row(
    conn: sqlite3.Connection,
    *,
    order_id: str | None,
    position_type: str | None,
    timestamp: str = "2026-06-12T00:00:00+00:00",
    symbol: str = "AAPL",
) -> int:
    """Insert a trades row with explicit order_id/position_type,
    returning the new row id. Used to simulate legacy data shapes."""
    cur = conn.execute(
        """
        INSERT INTO trades (
            timestamp, symbol, side, qty, avg_fill_price,
            order_id, strategy, reason, stop_price,
            entry_reference_price, modeled_slippage_bps,
            realized_slippage_bps, order_type, status,
            requested_qty, filled_qty, position_type
        ) VALUES (
            ?, ?, 'buy', 10, 100.0,
            ?, 'sma_crossover', 'test', 95.0,
            100.0, 0.0,
            0.0, 'market', 'filled',
            10, 10, ?
        )
        """,
        (timestamp, symbol, order_id, position_type),
    )
    conn.commit()
    return int(cur.lastrowid)


class TestTradesOrderIdDuplicateDetection:
    """detect_trades_order_id_duplicates and the integrated preflight
    must catch every duplicate that would conflict with the new
    uniq_trades_order_id_single_leg partial UNIQUE — at the moment
    the index is created, AND after _BACKFILL_SQL flips legacy NULL
    rows to single_leg scope."""

    def test_clean_db_no_trades_duplicates(self, tmp_db_path):
        from engine.lifecycle_orders import detect_trades_order_id_duplicates
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        # Empty trades table → clean.
        assert detect_trades_order_id_duplicates(conn) == ()

    def test_two_single_leg_rows_same_order_id_detected(self, tmp_db_path):
        """Two rows already at position_type='single_leg' with the same
        order_id is the prototypical violation. Uses a legacy-shape
        trades table (no partial UNIQUE yet) to simulate the state
        at which preflight is supposed to run."""
        from engine.lifecycle_orders import detect_trades_order_id_duplicates
        conn = _make_legacy_trades_conn(tmp_db_path)
        _seed_trades_row(conn, order_id="ord-X", position_type="single_leg")
        _seed_trades_row(conn, order_id="ord-X", position_type="single_leg")
        dupes = detect_trades_order_id_duplicates(conn)
        assert len(dupes) == 1
        assert dupes[0].order_id == "ord-X"
        assert dupes[0].count == 2
        assert len(dupes[0].trade_ids) == 2

    def test_two_null_position_type_rows_same_order_id_detected(
        self, tmp_db_path,
    ):
        """Pre-BACKFILL legacy rows: position_type=NULL on both.
        BACKFILL would move both into single_leg scope and collide,
        so preflight must catch them BEFORE BACKFILL runs."""
        from engine.lifecycle_orders import detect_trades_order_id_duplicates
        conn = _make_legacy_trades_conn(tmp_db_path)
        _seed_trades_row(conn, order_id="ord-Y", position_type=None)
        _seed_trades_row(conn, order_id="ord-Y", position_type=None)
        dupes = detect_trades_order_id_duplicates(conn)
        assert len(dupes) == 1
        assert dupes[0].order_id == "ord-Y"

    def test_mixed_null_and_single_leg_same_order_id_detected(
        self, tmp_db_path,
    ):
        """Half-migrated DB: one row at single_leg, another at NULL.
        BACKFILL would promote the NULL to single_leg and collide.
        Preflight must surface this."""
        from engine.lifecycle_orders import detect_trades_order_id_duplicates
        conn = _make_legacy_trades_conn(tmp_db_path)
        _seed_trades_row(conn, order_id="ord-Z", position_type="single_leg")
        _seed_trades_row(conn, order_id="ord-Z", position_type=None)
        dupes = detect_trades_order_id_duplicates(conn)
        assert len(dupes) == 1
        assert dupes[0].order_id == "ord-Z"

    def test_spread_rows_sharing_order_id_NOT_flagged(self, tmp_db_path):
        """Spread legs deliberately share order_id (one combo order,
        two OCC leg rows) and live OUTSIDE the partial UNIQUE scope.
        Preflight must not flag them."""
        from engine.lifecycle_orders import detect_trades_order_id_duplicates
        conn = _make_legacy_trades_conn(tmp_db_path)
        _seed_trades_row(
            conn, order_id="combo-1", position_type="spread",
            symbol="SPY260618C00500000",
        )
        _seed_trades_row(
            conn, order_id="combo-1", position_type="spread",
            symbol="SPY260618C00510000",
        )
        assert detect_trades_order_id_duplicates(conn) == ()

    def test_null_order_id_rows_NOT_flagged(self, tmp_db_path):
        """Rows with order_id IS NULL are outside the partial UNIQUE
        predicate by construction."""
        from engine.lifecycle_orders import detect_trades_order_id_duplicates
        conn = _make_legacy_trades_conn(tmp_db_path)
        _seed_trades_row(conn, order_id=None, position_type="single_leg")
        _seed_trades_row(conn, order_id=None, position_type="single_leg")
        assert detect_trades_order_id_duplicates(conn) == ()

    def test_preflight_raises_with_trades_duplicates_in_payload(
        self, tmp_db_path,
    ):
        """run_preflight_or_raise carries the trades duplicates on the
        exception so the operator alert can render them."""
        conn = _make_legacy_trades_conn(tmp_db_path)
        _seed_trades_row(conn, order_id="ord-W", position_type="single_leg")
        _seed_trades_row(conn, order_id="ord-W", position_type="single_leg")
        with pytest.raises(MigrationDuplicatesFound) as exc:
            run_preflight_or_raise(conn)
        assert len(exc.value.trades_order_id_duplicates) == 1
        assert exc.value.trades_order_id_duplicates[0].order_id == "ord-W"
        # And owner_key dimension is empty (no position_lifecycle).
        assert exc.value.owner_key_duplicates == ()

    def test_preflight_message_lists_both_dimensions(self, tmp_db_path):
        """The exception's str() form lists both owner_key and trades
        duplicate clusters so the operator alert renders everything
        in a single diagnostic blob."""
        # Build both dimensions on a single legacy-shape conn.
        conn = _make_legacy_trades_conn(tmp_db_path)
        _seed_trades_row(conn, order_id="ord-T", position_type="single_leg")
        _seed_trades_row(conn, order_id="ord-T", position_type="single_leg")
        conn.execute(
            "CREATE TABLE position_lifecycle ("
            "schema_version INTEGER, position_uid TEXT, "
            "created_at TEXT, closed_at TEXT, symbol TEXT, "
            "owner_key TEXT, strategy TEXT, position_type TEXT, "
            "status TEXT, entry_qty REAL, current_qty REAL, "
            "avg_entry_price REAL, net_realized_pnl REAL, "
            "entry_order_id TEXT, entry_client_order_id TEXT, "
            "first_fill_at TEXT, last_fill_at TEXT, metadata_json TEXT)"
        )
        conn.execute(
            "INSERT INTO position_lifecycle "
            "(schema_version, position_uid, owner_key, status, strategy) "
            "VALUES (1, 'p1', 'AAPL', 'open', 'sma')"
        )
        conn.execute(
            "INSERT INTO position_lifecycle "
            "(schema_version, position_uid, owner_key, status, strategy) "
            "VALUES (1, 'p2', 'AAPL', 'open', 'sma')"
        )
        conn.commit()
        with pytest.raises(MigrationDuplicatesFound) as exc:
            run_preflight_or_raise(conn)
        msg = str(exc.value)
        assert "owner_key" in msg
        assert "trades.order_id" in msg
        assert "ord-T" in msg
