import sqlite3
from datetime import datetime, timezone

import pytest

from engine.lifecycle import (
    _CREATE_POSITION_LIFECYCLE_SQL,
    _CREATE_POSITION_LIFECYCLE_LEGS_SQL,
)
from engine.lifecycle_orders import (
    PositionLifecycleOrdersStore,
    _CREATE_POSITION_LIFECYCLE_ORDERS_SQL,
    _CREATE_POSITION_LIFECYCLE_ORDERS_INDEXES_SQL,
)
from engine.option_trailing import (
    JoinedOptionTrailingRow,
    OptionTrailingStopStore,
    _CREATE_OPTION_TRAILING_STOPS_SQL,
    _OPTION_TRAILING_STOPS_INDEXES_SQL,
    _ensure_lifecycle_order_id_column,
)


@pytest.fixture
def store() -> OptionTrailingStopStore:
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE_OPTION_TRAILING_STOPS_SQL)
    for sql in _OPTION_TRAILING_STOPS_INDEXES_SQL:
        conn.execute(sql)
    return OptionTrailingStopStore(conn)


def _bootstrap_full_schema(conn: sqlite3.Connection) -> None:
    """Set up the cross-table schema needed for FK / join tests."""
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute(_CREATE_POSITION_LIFECYCLE_SQL)
    conn.execute(_CREATE_POSITION_LIFECYCLE_LEGS_SQL)
    conn.execute(_CREATE_POSITION_LIFECYCLE_ORDERS_SQL)
    for sql in _CREATE_POSITION_LIFECYCLE_ORDERS_INDEXES_SQL:
        conn.execute(sql)
    conn.execute(_CREATE_OPTION_TRAILING_STOPS_SQL)
    for sql in _OPTION_TRAILING_STOPS_INDEXES_SQL:
        conn.execute(sql)


def _seed_position(conn: sqlite3.Connection, *, position_uid: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO position_lifecycle (
            position_uid, schema_version, created_at, symbol, owner_key,
            strategy, position_type, status, entry_qty, current_qty,
            avg_entry_price, net_realized_pnl
        ) VALUES (?, 1, ?, 'SPY260618C00746000', 'SPY',
                  'generic_single_leg_options', 'single_leg', 'open',
                  3.0, 3.0, 12.77, 0.0)
        """,
        (position_uid, now),
    )
    conn.commit()


class TestOptionTrailingStopStore:
    def test_upsert_round_trips_by_position_uid_and_occ(self, store):
        store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.13,
            alpaca_stop_order_id="stop-1",
            stop_order_status="accepted",
            last_observed_premium=20.16,
        )

        row = store.get_by_occ("SPY260618C00746000")

        assert row is not None
        assert row.position_uid == "pos_abc123"
        assert row.occ_symbol == "SPY260618C00746000"
        assert row.strategy == "generic_single_leg_options"
        assert row.hwm_premium == pytest.approx(20.16)
        assert row.current_stop_price == pytest.approx(17.13)

    def test_lifecycle_order_id_round_trips(self, store):
        store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            current_stop_price=17.13,
            alpaca_stop_order_id="stop-1",
            stop_order_status="accepted",
            last_observed_premium=20.16,
            lifecycle_order_id=42,
        )

        row = store.get_by_occ("SPY260618C00746000")

        assert row is not None
        assert row.lifecycle_order_id == 42

    def test_lifecycle_order_id_defaults_to_none(self, store):
        store.upsert(
            position_uid="pos_legacy",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=1,
            entry_premium=10.0,
            hwm_premium=10.0,
            trail_activation_pct=0.10,
            trail_pct=0.15,
        )

        row = store.get_by_occ("SPY260618C00746000")

        assert row is not None
        assert row.lifecycle_order_id is None

    def test_upsert_updates_lifecycle_order_id_on_replacement(self, store):
        store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            alpaca_stop_order_id="stop-1",
            stop_order_status="accepted",
            lifecycle_order_id=11,
        )
        # Simulate a stop replacement: trailing FK should advance to the
        # new substrate row's id.
        store.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=22.50,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            alpaca_stop_order_id="stop-2",
            stop_order_status="accepted",
            lifecycle_order_id=12,
        )

        row = store.get_by_occ("SPY260618C00746000")

        assert row.lifecycle_order_id == 12
        assert row.alpaca_stop_order_id == "stop-2"

    def test_requires_position_uid(self, store):
        with pytest.raises(ValueError, match="position_uid"):
            store.upsert(
                position_uid="",
                occ_symbol="SPY260618C00746000",
                strategy="generic_single_leg_options",
                owner_key="SPY",
                qty=1,
                entry_premium=10.0,
                hwm_premium=10.0,
                trail_activation_pct=0.10,
                trail_pct=0.15,
            )


class TestOptionTrailingFKToSubstrate:
    """PR #59 §10.4: lifecycle_order_id is the substrate authority."""

    def _store_with_substrate(
        self,
    ) -> tuple[OptionTrailingStopStore, PositionLifecycleOrdersStore,
               sqlite3.Connection]:
        conn = sqlite3.connect(":memory:")
        _bootstrap_full_schema(conn)
        _seed_position(conn, position_uid="pos_abc123")
        return (
            OptionTrailingStopStore(conn),
            PositionLifecycleOrdersStore(conn),
            conn,
        )

    def test_get_by_occ_joined_returns_substrate_status(self):
        trailing, orders, _ = self._store_with_substrate()
        # Insert the substrate row first, attach an order_id.
        substrate_id = orders.insert_pending(
            position_uid="pos_abc123",
            role="protective_stop",
            client_order_id="opt-trail-stop-cid-1",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=3.0,
            intended_stop_price=17.13,
        )
        orders.attach_broker_order_id(
            client_order_id="opt-trail-stop-cid-1",
            order_id="broker-stop-1",
        )
        # Then the trailing row pointing at it.
        trailing.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            alpaca_stop_order_id="broker-stop-1",
            stop_order_status="accepted",
            lifecycle_order_id=substrate_id,
        )

        joined = trailing.get_by_occ_joined("SPY260618C00746000")

        assert isinstance(joined, JoinedOptionTrailingRow)
        assert joined.trailing.alpaca_stop_order_id == "broker-stop-1"
        # Per insert_pending + attach_broker_order_id, the row is at
        # 'pending' with order_id populated.
        assert joined.substrate_order_id == "broker-stop-1"
        assert joined.substrate_status == "pending"
        assert joined.authoritative_order_id == "broker-stop-1"
        assert joined.authoritative_status == "pending"

    def test_get_by_occ_joined_falls_back_to_mirror_when_fk_null(self):
        trailing, _orders, conn = self._store_with_substrate()
        trailing.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=1,
            entry_premium=10.0,
            hwm_premium=10.0,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            alpaca_stop_order_id="legacy-stop",
            stop_order_status="accepted",
            lifecycle_order_id=None,
        )

        joined = trailing.get_by_occ_joined("SPY260618C00746000")

        assert joined is not None
        assert joined.substrate_order_id is None
        assert joined.substrate_status is None
        assert joined.authoritative_order_id == "legacy-stop"
        assert joined.authoritative_status == "accepted"

    def test_get_by_occ_joined_returns_none_when_no_trailing_row(self):
        trailing, _orders, _conn = self._store_with_substrate()
        assert trailing.get_by_occ_joined("SPY260618C00746000") is None

    def test_fk_violation_when_substrate_row_missing(self):
        trailing, _orders, conn = self._store_with_substrate()

        with pytest.raises(sqlite3.IntegrityError):
            trailing.upsert(
                position_uid="pos_abc123",
                occ_symbol="SPY260618C00746000",
                strategy="generic_single_leg_options",
                owner_key="SPY",
                qty=1,
                entry_premium=10.0,
                hwm_premium=10.0,
                trail_activation_pct=0.10,
                trail_pct=0.15,
                lifecycle_order_id=9999,  # no such row
            )

    def test_replacement_advances_fk_to_new_substrate_row(self):
        trailing, orders, _ = self._store_with_substrate()
        old_id = orders.insert_pending(
            position_uid="pos_abc123",
            role="protective_stop",
            client_order_id="opt-trail-stop-old",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=3.0,
            intended_stop_price=17.13,
        )
        orders.attach_broker_order_id(
            client_order_id="opt-trail-stop-old",
            order_id="broker-stop-old",
        )
        trailing.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=20.16,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            alpaca_stop_order_id="broker-stop-old",
            stop_order_status="accepted",
            lifecycle_order_id=old_id,
        )
        # Simulate engine's replacement flow: substrate writes a new
        # replacement_stop row, trailing FK updates to its id.
        new_id = orders.insert_pending(
            position_uid="pos_abc123",
            role="replacement_stop",
            client_order_id="opt-trail-stop-new",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=3.0,
            intended_stop_price=19.00,
            replaces_order_id="broker-stop-old",
        )
        orders.attach_broker_order_id(
            client_order_id="opt-trail-stop-new",
            order_id="broker-stop-new",
        )
        trailing.upsert(
            position_uid="pos_abc123",
            occ_symbol="SPY260618C00746000",
            strategy="generic_single_leg_options",
            owner_key="SPY",
            qty=3,
            entry_premium=12.77,
            hwm_premium=22.50,
            trail_activation_pct=0.10,
            trail_pct=0.15,
            alpaca_stop_order_id="broker-stop-new",
            stop_order_status="accepted",
            lifecycle_order_id=new_id,
        )

        joined = trailing.get_by_occ_joined("SPY260618C00746000")

        assert joined.trailing.lifecycle_order_id == new_id
        assert joined.substrate_order_id == "broker-stop-new"


class TestEnsureLifecycleOrderIdColumn:
    """Idempotent migration for legacy DBs created before §10.4."""

    def _legacy_schema(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """
            CREATE TABLE option_trailing_stops (
                position_uid            TEXT PRIMARY KEY,
                occ_symbol              TEXT NOT NULL UNIQUE,
                strategy                TEXT NOT NULL,
                owner_key               TEXT NOT NULL,
                qty                     REAL NOT NULL,
                entry_premium           REAL NOT NULL,
                hwm_premium             REAL NOT NULL,
                trail_activation_pct    REAL NOT NULL,
                trail_pct               REAL NOT NULL,
                current_stop_price      REAL,
                alpaca_stop_order_id    TEXT,
                stop_order_status       TEXT,
                last_observed_premium   REAL,
                last_updated_at         TEXT NOT NULL
            )
            """
        )
        return conn

    def test_adds_column_when_missing(self):
        conn = self._legacy_schema()
        columns_before = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(option_trailing_stops)"
            ).fetchall()
        }
        assert "lifecycle_order_id" not in columns_before

        _ensure_lifecycle_order_id_column(conn)

        columns_after = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(option_trailing_stops)"
            ).fetchall()
        }
        assert "lifecycle_order_id" in columns_after

    def test_idempotent_when_column_present(self):
        conn = self._legacy_schema()
        _ensure_lifecycle_order_id_column(conn)
        # Second invocation must not raise.
        _ensure_lifecycle_order_id_column(conn)
