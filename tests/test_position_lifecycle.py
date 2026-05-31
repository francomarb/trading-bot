"""Unit tests for `engine.lifecycle` — `PositionLifecycleStore`.

Operator Controls Phase A foundation. Verifies:

- Schema creation through `TradeLogger._ensure_db()` is idempotent.
- `position_uid` generation and `client_order_id_for` helper formats.
- Lifecycle transitions: pending → open → closed, partial fill paths,
  and the §8.1 invariant that a partially-filled-then-cancelled entry
  cannot be marked `canceled`.
- Spread legs round-trip.
- Read helpers return what was written.
- The PRIMARY KEY constraint rejects duplicate `position_uid`.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from engine.lifecycle import (
    PositionLifecycleLeg,
    PositionLifecycleStore,
    VALID_POSITION_TYPES,
    VALID_STATUSES,
    _CREATE_POSITION_LIFECYCLE_INDEXES_SQL,
    _CREATE_POSITION_LIFECYCLE_LEGS_SQL,
    _CREATE_POSITION_LIFECYCLE_SQL,
    client_order_id_for,
    new_position_uid,
)
from reporting.logger import TradeLogger


@pytest.fixture
def store(tmp_path) -> PositionLifecycleStore:
    """A store backed by a fresh sqlite DB with schema applied via
    TradeLogger._ensure_db() — the same path production uses."""
    db_path = tmp_path / "trades.db"
    tl = TradeLogger(path=str(db_path))
    conn = tl._ensure_db()
    return PositionLifecycleStore(conn)


@pytest.fixture
def raw_conn(tmp_path) -> sqlite3.Connection:
    """A raw sqlite connection with only the lifecycle DDL applied —
    used to test the DDL itself in isolation from TradeLogger."""
    conn = sqlite3.connect(":memory:")
    conn.execute(_CREATE_POSITION_LIFECYCLE_SQL)
    conn.execute(_CREATE_POSITION_LIFECYCLE_LEGS_SQL)
    for sql in _CREATE_POSITION_LIFECYCLE_INDEXES_SQL:
        conn.execute(sql)
    conn.commit()
    return conn


class TestIDGenerators:
    def test_new_position_uid_format(self):
        uid = new_position_uid()
        assert uid.startswith("pos_")
        # uuid4 hex is 32 chars; prefix adds 4
        assert len(uid) == 36

    def test_new_position_uid_unique(self):
        uids = {new_position_uid() for _ in range(100)}
        assert len(uids) == 100

    def test_client_order_id_for_basic(self):
        uid = "pos_abcdef0123456789abcdef0123456789"
        coid = client_order_id_for("sma_crossover", uid)
        assert coid == "sma_crossover-abcdef0123"

    def test_client_order_id_for_with_suffix(self):
        uid = "pos_abcdef0123456789abcdef0123456789"
        coid = client_order_id_for("sma_crossover", uid, suffix="reduce")
        assert coid == "sma_crossover-abcdef0123-reduce"

    def test_client_order_id_for_rejects_empty_strategy(self):
        with pytest.raises(ValueError, match="strategy_name"):
            client_order_id_for("", "pos_abc123")

    def test_client_order_id_for_rejects_bad_uid(self):
        with pytest.raises(ValueError, match="position_uid"):
            client_order_id_for("sma", "not_a_pos_uid")


class TestSchemaMigration:
    def test_ensure_db_creates_lifecycle_tables(self, tmp_path):
        db_path = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db_path))
        conn = tl._ensure_db()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "position_lifecycle" in tables
        assert "position_lifecycle_legs" in tables

    def test_ensure_db_is_idempotent(self, tmp_path):
        """Running migration twice produces no errors and no schema drift."""
        db_path = tmp_path / "trades.db"
        TradeLogger(path=str(db_path))._ensure_db()
        # Second logger on the same path — must not raise or duplicate work.
        conn2 = TradeLogger(path=str(db_path))._ensure_db()
        # Tables still present and uniquely named.
        names = [
            r[0]
            for r in conn2.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name LIKE 'position_lifecycle%'"
            ).fetchall()
        ]
        assert sorted(names) == ["position_lifecycle", "position_lifecycle_legs"]

    def test_trades_has_position_uid_column_after_migration(self, tmp_path):
        db_path = tmp_path / "trades.db"
        conn = TradeLogger(path=str(db_path))._ensure_db()
        cols = {row[1] for row in conn.execute("PRAGMA table_info(trades)")}
        assert "position_uid" in cols

    def test_indexes_created(self, tmp_path):
        db_path = tmp_path / "trades.db"
        conn = TradeLogger(path=str(db_path))._ensure_db()
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_position_lifecycle%' "
                "OR name = 'idx_trades_position_uid'"
            ).fetchall()
        }
        assert "idx_trades_position_uid" in indexes
        assert "idx_position_lifecycle_owner_key" in indexes
        assert "idx_position_lifecycle_status" in indexes
        assert "idx_position_lifecycle_legs_uid" in indexes


class TestCreatePending:
    def test_creates_row_in_pending_status(self, store):
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            entry_qty=10.0,
            entry_client_order_id="sma_crossover-abcd123456",
        )
        row = store.get_by_position_uid(uid)
        assert row is not None
        assert row.status == "pending"
        assert row.symbol == "NVDA"
        assert row.owner_key == "NVDA"
        assert row.strategy == "sma_crossover"
        assert row.entry_qty == 10.0
        assert row.current_qty == 0.0
        assert row.avg_entry_price is None
        assert row.entry_client_order_id == "sma_crossover-abcd123456"
        assert row.first_fill_at is None

    def test_rejects_invalid_position_type(self, store):
        with pytest.raises(ValueError, match="position_type"):
            store.create_pending(
                position_uid=new_position_uid(),
                symbol="NVDA",
                owner_key="NVDA",
                strategy="sma_crossover",
                position_type="weird_thing",
                entry_qty=10.0,
            )

    def test_rejects_duplicate_position_uid(self, store):
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            entry_qty=10.0,
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.create_pending(
                position_uid=uid,
                symbol="NVDA",
                owner_key="NVDA",
                strategy="sma_crossover",
                position_type="single_leg",
                entry_qty=10.0,
            )

    def test_persists_legs_for_spread(self, store):
        uid = new_position_uid()
        legs = [
            PositionLifecycleLeg(uid, "SPY250620C00450000", "buy", 1.0, 5.20),
            PositionLifecycleLeg(uid, "SPY250620C00460000", "sell", 1.0, 2.40),
        ]
        store.create_pending(
            position_uid=uid,
            symbol="SPY",
            owner_key=uid,  # spread owner_key = position UUID
            strategy="credit_spread",
            position_type="spread",
            entry_qty=1.0,
            legs=legs,
        )
        row = store.get_by_position_uid(uid)
        assert len(row.legs) == 2
        symbols = {leg.symbol for leg in row.legs}
        assert symbols == {"SPY250620C00450000", "SPY250620C00460000"}

    def test_metadata_stored_as_json(self, store):
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            entry_qty=10.0,
            metadata={"signal_strength": 0.84, "notes": "test"},
        )
        row = store.get_by_position_uid(uid)
        assert row.metadata["signal_strength"] == 0.84
        assert row.metadata["notes"] == "test"


class TestTransitions:
    def _seed(self, store) -> str:
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA",
            owner_key="NVDA",
            strategy="sma_crossover",
            position_type="single_leg",
            entry_qty=10.0,
        )
        return uid

    def test_mark_open_transitions_status(self, store):
        uid = self._seed(store)
        store.mark_open(
            position_uid=uid,
            avg_entry_price=884.20,
            current_qty=10.0,
        )
        row = store.get_by_position_uid(uid)
        assert row.status == "open"
        assert row.avg_entry_price == 884.20
        assert row.current_qty == 10.0
        assert row.first_fill_at is not None
        assert row.last_fill_at is not None

    def test_mark_partially_filled(self, store):
        uid = self._seed(store)
        store.mark_partially_filled(
            position_uid=uid,
            avg_entry_price=884.20,
            current_qty=4.0,
        )
        row = store.get_by_position_uid(uid)
        assert row.status == "partially_filled"
        assert row.current_qty == 4.0

    def test_first_fill_at_is_idempotent(self, store):
        """Subsequent mark_open calls must not overwrite first_fill_at
        (so fill timing is recorded only once)."""
        uid = self._seed(store)
        store.mark_open(
            position_uid=uid, avg_entry_price=100.0, current_qty=10.0,
            first_fill_at="2026-05-30T10:00:00+00:00",
        )
        row1 = store.get_by_position_uid(uid)
        store.mark_open(
            position_uid=uid, avg_entry_price=100.0, current_qty=10.0,
            first_fill_at="2026-05-30T11:00:00+00:00",  # later, should be ignored
        )
        row2 = store.get_by_position_uid(uid)
        assert row1.first_fill_at == row2.first_fill_at == "2026-05-30T10:00:00+00:00"

    def test_mark_canceled_zero_fills(self, store):
        uid = self._seed(store)
        store.mark_canceled(position_uid=uid)
        row = store.get_by_position_uid(uid)
        assert row.status == "canceled"
        assert row.closed_at is not None

    def test_mark_canceled_rejects_after_fills(self, store):
        """Proposal §8.1 invariant: partially-filled-then-cancelled must
        STAY open at filled quantity, never transition to 'canceled'."""
        uid = self._seed(store)
        store.mark_partially_filled(
            position_uid=uid, avg_entry_price=100.0, current_qty=4.0,
        )
        with pytest.raises(ValueError, match="§8.1"):
            store.mark_canceled(position_uid=uid)
        # Status unchanged.
        assert store.get_by_position_uid(uid).status == "partially_filled"

    def test_mark_canceled_rejects_unknown_uid(self, store):
        with pytest.raises(ValueError, match="unknown position_uid"):
            store.mark_canceled(position_uid="pos_doesnotexist00000000000000000000")

    def test_mark_closed_sets_closed_at(self, store):
        uid = self._seed(store)
        store.mark_open(
            position_uid=uid, avg_entry_price=100.0, current_qty=10.0,
        )
        store.mark_closed(position_uid=uid, net_realized_pnl=250.0)
        row = store.get_by_position_uid(uid)
        assert row.status == "closed"
        assert row.closed_at is not None
        assert row.current_qty == 0.0
        assert row.net_realized_pnl == 250.0

    def test_mark_closed_external(self, store):
        uid = self._seed(store)
        store.mark_open(
            position_uid=uid, avg_entry_price=100.0, current_qty=10.0,
        )
        store.mark_closed(position_uid=uid, external=True)
        row = store.get_by_position_uid(uid)
        assert row.status == "external_closed"


class TestMarkResidual:
    """`mark_residual` updates current_qty after a partial close
    without changing status — the row stays open at the residual
    quantity."""

    def _seed_open(self, store) -> str:
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        store.mark_open(
            position_uid=uid, avg_entry_price=884.20, current_qty=10.0,
        )
        return uid

    def test_updates_current_qty_only(self, store):
        uid = self._seed_open(store)
        store.mark_residual(position_uid=uid, current_qty=6.0)
        row = store.get_by_position_uid(uid)
        assert row.status == "open"  # status unchanged
        assert row.current_qty == 6.0
        assert row.entry_qty == 10.0  # entry intent preserved
        assert row.avg_entry_price == 884.20  # avg entry preserved

    def test_updates_last_fill_at(self, store):
        uid = self._seed_open(store)
        store.mark_residual(
            position_uid=uid, current_qty=6.0,
            last_fill_at="2026-05-31T15:30:00+00:00",
        )
        row = store.get_by_position_uid(uid)
        assert row.last_fill_at == "2026-05-31T15:30:00+00:00"

    def test_rejects_zero_or_negative(self, store):
        uid = self._seed_open(store)
        with pytest.raises(ValueError, match="mark_closed"):
            store.mark_residual(position_uid=uid, current_qty=0.0)
        with pytest.raises(ValueError, match="mark_closed"):
            store.mark_residual(position_uid=uid, current_qty=-1.0)

    def test_preserves_first_fill_at(self, store):
        """Partial-close residual updates must NOT overwrite the
        original entry fill timestamp."""
        uid = self._seed_open(store)
        original = store.get_by_position_uid(uid).first_fill_at
        store.mark_residual(position_uid=uid, current_qty=6.0)
        assert store.get_by_position_uid(uid).first_fill_at == original


class TestReads:
    def _seed_three(self, store) -> tuple[str, str, str]:
        uid_a, uid_b, uid_c = (new_position_uid() for _ in range(3))
        for uid, sym in [(uid_a, "NVDA"), (uid_b, "MU"), (uid_c, "AAPL")]:
            store.create_pending(
                position_uid=uid,
                symbol=sym,
                owner_key=sym,
                strategy="sma_crossover",
                position_type="single_leg",
                entry_qty=5.0,
            )
        return uid_a, uid_b, uid_c

    def test_get_open_returns_only_non_terminal(self, store):
        uid_a, uid_b, uid_c = self._seed_three(store)
        store.mark_open(
            position_uid=uid_a, avg_entry_price=100.0, current_qty=5.0,
        )
        store.mark_open(
            position_uid=uid_b, avg_entry_price=100.0, current_qty=5.0,
        )
        store.mark_closed(position_uid=uid_b)
        # A: open, B: closed, C: pending
        open_rows = store.get_open()
        uids = {r.position_uid for r in open_rows}
        assert uids == {uid_a, uid_c}
        statuses = {r.status for r in open_rows}
        assert statuses == {"open", "pending"}

    def test_get_open_for_owner_key_returns_latest(self, store):
        """When two non-terminal rows exist for the same owner_key
        (rare edge case), return the most recently created."""
        uid_old = new_position_uid()
        store.create_pending(
            position_uid=uid_old, symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=5.0,
        )
        uid_new = new_position_uid()
        store.create_pending(
            position_uid=uid_new, symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=5.0,
        )
        row = store.get_open_for_owner_key("NVDA")
        assert row is not None
        assert row.position_uid == uid_new

    def test_get_open_for_owner_key_excludes_closed(self, store):
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid, symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=5.0,
        )
        store.mark_open(
            position_uid=uid, avg_entry_price=100.0, current_qty=5.0,
        )
        store.mark_closed(position_uid=uid)
        assert store.get_open_for_owner_key("NVDA") is None

    def test_get_by_position_uid_unknown_returns_none(self, store):
        assert store.get_by_position_uid("pos_doesnotexist000000000000000000") is None

    def test_get_legs_for_returns_inserted_legs(self, store):
        uid = new_position_uid()
        legs = [
            PositionLifecycleLeg(uid, "SPY250620C00450000", "buy", 1.0, 5.20),
            PositionLifecycleLeg(uid, "SPY250620C00460000", "sell", 1.0, 2.40),
        ]
        store.create_pending(
            position_uid=uid, symbol="SPY", owner_key=uid,
            strategy="credit_spread", position_type="spread",
            entry_qty=1.0, legs=legs,
        )
        got = store.get_legs_for(uid)
        assert len(got) == 2
        sides = {leg.side for leg in got}
        assert sides == {"buy", "sell"}


class TestValidatorsAndInvariants:
    def test_all_valid_statuses_are_known(self):
        """Documentation invariant — keeps the enum and the docstring
        in sync. Adding a new status without updating VALID_STATUSES
        would let invalid values slip through to the DB."""
        expected = {
            "pending", "open", "partially_filled", "closed",
            "canceled", "external_closed", "error",
        }
        assert VALID_STATUSES == expected

    def test_all_valid_position_types_are_known(self):
        assert VALID_POSITION_TYPES == {"single_leg", "spread"}

    def test_uid_validator_rejects_missing_prefix(self, store):
        with pytest.raises(ValueError, match="pos_"):
            store.create_pending(
                position_uid="not_a_uid",
                symbol="NVDA", owner_key="NVDA",
                strategy="sma_crossover", position_type="single_leg",
                entry_qty=5.0,
            )
