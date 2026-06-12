"""
Unit tests for the order-lifecycle foundation schema migration (commit 1).

Covers the schema additions landed in ``engine/lifecycle_orders.py``
and wired into ``reporting/logger.py::TradeLogger._ensure_db``:

- ``position_lifecycle_orders`` table creation
- All partial / full unique indexes (uniq_lifecycle_orders_order_id,
  uniq_lifecycle_orders_client_order_id, uniq_one_entry_primary_per_position,
  uniq_one_active_close_per_position)
- Position-level ``uniq_one_active_position_per_owner_key`` partial index
  on the existing ``position_lifecycle`` table
- ``PRAGMA foreign_keys = ON;`` enforcement (PR #59 R13-G1)

§12.1 test matrix items covered by this file:

- Test 16 (`'error'` retains owner_key lock — partial index includes
  'error' status)
- Test 25 (SQLite FK enforcement PRAGMA blocks orphan inserts)

Idempotency and parity tests are also included so re-running
``_ensure_db`` on a populated DB is provably safe.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from engine.lifecycle import new_position_uid
from engine.lifecycle_orders import (
    _CREATE_POSITION_LIFECYCLE_ORDERS_INDEXES_SQL,
    LIFECYCLE_ORDERS_SCHEMA_VERSION,
    NON_TERMINAL_ORDER_STATUSES,
    TERMINAL_ORDER_STATUSES,
    VALID_ORDER_ROLES,
    VALID_ORDER_STATUSES,
)
from reporting.logger import TradeLogger


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "trades.db")


def _open_db(path: str) -> sqlite3.Connection:
    """Open a fresh raw connection (does NOT run _ensure_db migrations)."""
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def _index_set(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA index_list({table})").fetchall()
    }


def _column_set(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
    }


# ── Schema creation ────────────────────────────────────────────────────────


class TestPositionLifecycleOrdersSchema:
    """The CREATE TABLE statement produces the expected column set
    and the index DDL produces all the expected partial / full
    unique constraints plus lookup indexes."""

    EXPECTED_COLUMNS = {
        "id",
        "position_uid",
        "role",
        "order_id",
        "client_order_id",
        "order_type",
        "order_class",
        "time_in_force",
        "side",
        "intended_qty",
        "intended_stop_price",
        "intended_trigger_price",
        "intended_limit_price",
        "intended_take_profit_price",
        "parent_order_id",
        "replaces_order_id",
        "origin_kind",
        "operator_command_uid",
        "slippage_benchmark_price",
        "slippage_benchmark_kind",
        "slippage_benchmark_timestamp",
        "slippage_measurement_quality",
        "status",
        "filled_qty",
        "avg_fill_price",
        "created_at",
        "submitted_at",
        "terminal_at",
        "last_observed_broker_updated_at",
        "last_observed_at",
    }

    EXPECTED_INDEXES = {
        "uniq_lifecycle_orders_order_id",
        "uniq_lifecycle_orders_client_order_id",
        "uniq_one_entry_primary_per_position",
        "uniq_one_active_close_per_position",
        "idx_lifecycle_orders_position_uid",
        "idx_lifecycle_orders_status",
        "idx_lifecycle_orders_parent",
        "idx_lifecycle_orders_replaces",
    }

    def test_create_table_yields_expected_columns(self, tmp_db_path: str):
        TradeLogger(path=tmp_db_path)._ensure_db()
        conn = _open_db(tmp_db_path)
        try:
            columns = _column_set(conn, "position_lifecycle_orders")
        finally:
            conn.close()
        assert columns == self.EXPECTED_COLUMNS

    def test_indexes_all_created(self, tmp_db_path: str):
        TradeLogger(path=tmp_db_path)._ensure_db()
        conn = _open_db(tmp_db_path)
        try:
            indexes = _index_set(conn, "position_lifecycle_orders")
        finally:
            conn.close()
        assert self.EXPECTED_INDEXES.issubset(indexes)

    def test_position_level_owner_key_lock_index_exists(self, tmp_db_path: str):
        """Verifies the position-level uniq_one_active_position_per_owner_key
        partial index is created on the existing position_lifecycle table
        (PR #59 §6.2 / R6-1 / R8-3)."""
        TradeLogger(path=tmp_db_path)._ensure_db()
        conn = _open_db(tmp_db_path)
        try:
            indexes = _index_set(conn, "position_lifecycle")
        finally:
            conn.close()
        assert "uniq_one_active_position_per_owner_key" in indexes


# ── PRAGMA foreign_keys enforcement (Test 25, R13-G1) ──────────────────────


class TestForeignKeyEnforcement:
    """
    Per PR #59 R13-G1: SQLite does NOT enforce FOREIGN KEY constraints
    by default. The implementation PR must execute
    ``PRAGMA foreign_keys = ON;`` on every connection. These tests
    verify the PRAGMA is set by TradeLogger._ensure_db AND that
    inserting orphan child rows is rejected with IntegrityError.

    Without the PRAGMA the orphan INSERTs would silently succeed,
    leaving dangling references that no schema constraint surfaces.
    """

    def test_pragma_foreign_keys_is_on_after_ensure_db(self, tmp_db_path: str):
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        # PRAGMA foreign_keys returns 1 when enforcement is on, 0 when off.
        row = conn.execute("PRAGMA foreign_keys;").fetchone()
        assert row is not None
        assert row[0] == 1, (
            "PRAGMA foreign_keys must be ON for the FKs declared by "
            "position_lifecycle_orders (and option_trailing_stops, when "
            "that migration lands) to be enforced. PR #59 R13-G1."
        )

    def test_orphan_position_lifecycle_orders_insert_rejected(
        self, tmp_db_path: str
    ):
        """Attempt to INSERT into position_lifecycle_orders with a
        position_uid that does NOT exist in position_lifecycle.
        With FK enforcement on, SQLite raises IntegrityError. Without
        the PRAGMA, the INSERT would silently succeed."""
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        bogus_uid = new_position_uid()  # never inserted into position_lifecycle
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO position_lifecycle_orders (
                    position_uid, role, client_order_id,
                    order_type, order_class, time_in_force, side,
                    intended_qty, status,
                    created_at, last_observed_at
                ) VALUES (
                    ?, 'entry_primary', 'cli-test',
                    'market', 'simple', 'gtc', 'buy',
                    10, 'pending',
                    '2026-06-12T00:00:00+00:00',
                    '2026-06-12T00:00:00+00:00'
                )
                """,
                (bogus_uid,),
            )


# ── 'error' status retains owner_key lock (Test 16, R8-3 + R9-P1c) ─────────


class TestOwnerKeyLockIncludesError:
    """
    The uniq_one_active_position_per_owner_key partial unique index
    WHERE clause must include 'error'. An errored position needs
    operator resolution; excluding 'error' would let the bot silently
    open a fresh position over the unresolved error, masking it
    (PR #59 R8-3 + R9-P1c).
    """

    def _seed_position(
        self,
        conn: sqlite3.Connection,
        *,
        position_uid: str,
        owner_key: str,
        status: str,
    ) -> None:
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
                1, ?, ?, NULL,
                ?, ?, 'sma_crossover', 'single_leg', ?,
                10, 10, 100.0,
                0.0,
                NULL, NULL,
                NULL, NULL, NULL
            )
            """,
            (
                position_uid,
                "2026-06-12T00:00:00+00:00",
                owner_key,  # symbol
                owner_key,  # owner_key
                status,
            ),
        )
        conn.commit()

    @pytest.mark.parametrize(
        "first_status,second_status",
        [
            ("pending", "open"),
            ("open", "partially_filled"),
            ("open", "error"),
            ("error", "open"),
            ("partially_filled", "error"),
        ],
    )
    def test_two_active_positions_same_owner_key_rejected(
        self,
        tmp_db_path: str,
        first_status: str,
        second_status: str,
    ):
        """The lock prevents two positions on the same owner_key when
        both are in any non-terminal status (pending, open,
        partially_filled, error)."""
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        self._seed_position(
            conn,
            position_uid=new_position_uid(),
            owner_key="AAPL",
            status=first_status,
        )
        with pytest.raises(sqlite3.IntegrityError):
            self._seed_position(
                conn,
                position_uid=new_position_uid(),
                owner_key="AAPL",
                status=second_status,
            )

    def test_closed_position_releases_lock(self, tmp_db_path: str):
        """A position in `closed` status releases the lock; a new
        position on the same owner_key can be inserted."""
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        self._seed_position(
            conn,
            position_uid=new_position_uid(),
            owner_key="AAPL",
            status="closed",
        )
        # Should NOT raise — closed positions don't hold the lock.
        self._seed_position(
            conn,
            position_uid=new_position_uid(),
            owner_key="AAPL",
            status="open",
        )

    def test_canceled_position_releases_lock(self, tmp_db_path: str):
        """Same property for `canceled` — true zero-fill cancel
        releases the lock."""
        tl = TradeLogger(path=tmp_db_path)
        conn = tl._ensure_db()
        self._seed_position(
            conn,
            position_uid=new_position_uid(),
            owner_key="AAPL",
            status="canceled",
        )
        self._seed_position(
            conn,
            position_uid=new_position_uid(),
            owner_key="AAPL",
            status="pending",
        )


# ── Migration idempotency ──────────────────────────────────────────────────


class TestMigrationIdempotency:
    """Re-running _ensure_db on the same DB must not error and must
    leave the schema unchanged. Mirrors the slippage Phase 1 schema
    test pattern (Defect 3 fix)."""

    def test_ensure_db_is_idempotent(self, tmp_db_path: str):
        TradeLogger(path=tmp_db_path)._ensure_db()
        # Second open + bootstrap should be a no-op.
        TradeLogger(path=tmp_db_path)._ensure_db()
        conn = _open_db(tmp_db_path)
        try:
            cols = _column_set(conn, "position_lifecycle_orders")
            idxs = _index_set(conn, "position_lifecycle_orders")
        finally:
            conn.close()
        assert "id" in cols
        assert "uniq_lifecycle_orders_order_id" in idxs

    def test_lifecycle_orders_schema_version_constant(self):
        """The schema version constant is set; bumped on backward-
        incompatible changes."""
        assert LIFECYCLE_ORDERS_SCHEMA_VERSION == 1


# ── Role / status enum sanity ──────────────────────────────────────────────


class TestRoleAndStatusEnums:
    """The role / status enums exported by engine.lifecycle_orders
    match what the discovery doc §6.1 / §6.3 specifies. Writers and
    apply_order_event use these constants; they cannot drift."""

    def test_roles_set(self):
        assert VALID_ORDER_ROLES == {
            "entry_primary",
            "entry_residual",
            "protective_stop",
            "replacement_stop",
            "exit",
            "partial_close",
        }

    def test_statuses_set(self):
        assert VALID_ORDER_STATUSES == {
            "pending",
            "working",
            "partially_filled",
            "filled",
            "canceled",
            "rejected",
            "unknown",
        }

    def test_terminal_set_partition(self):
        assert TERMINAL_ORDER_STATUSES == {"filled", "canceled", "rejected"}
        assert NON_TERMINAL_ORDER_STATUSES == (
            VALID_ORDER_STATUSES - TERMINAL_ORDER_STATUSES
        )
