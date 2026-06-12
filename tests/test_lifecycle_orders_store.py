"""
Unit tests for ``PositionLifecycleOrdersStore`` (foundation commit 3).

The store provides CRUD primitives over ``position_lifecycle_orders``.
``apply_order_event`` (foundation commit 4) composes these into the
atomic compare-and-set + rollup + status pipeline.

Tests verify:

- ``insert_pending`` creates a row at status='pending' with order_id NULL
- All schema constraints fire at the right times (FK to position_uid,
  unique client_order_id, unique entry_primary per position, unique
  non-terminal close per position)
- ``attach_broker_order_id`` populates order_id and submitted_at;
  refuses to overwrite or apply post-pending
- Readers return immutable ``PositionLifecycleOrderRow`` snapshots
- Role-filtered lookups (used by the §6.6.1 position-status SQL in
  the next commit)
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from engine.lifecycle import PositionLifecycleStore, new_position_uid
from engine.lifecycle_orders import (
    CLOSE_SIDE_ROLES,
    NON_TERMINAL_ORDER_STATUSES,
    PositionLifecycleOrderRow,
    PositionLifecycleOrdersStore,
    SELL_SIDE_ROLES,
    STOP_SIDE_ROLES,
)
from reporting.logger import TradeLogger


# ── Fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db_path(tmp_path: Path) -> str:
    return str(tmp_path / "trades.db")


@pytest.fixture
def conn(tmp_db_path: str) -> sqlite3.Connection:
    return TradeLogger(path=tmp_db_path)._ensure_db()


@pytest.fixture
def pos_store(conn: sqlite3.Connection) -> PositionLifecycleStore:
    return PositionLifecycleStore(conn)


@pytest.fixture
def orders_store(conn: sqlite3.Connection) -> PositionLifecycleOrdersStore:
    return PositionLifecycleOrdersStore(conn)


def _seed_position(
    pos_store: PositionLifecycleStore,
    *,
    owner_key: str = "AAPL",
    symbol: str | None = None,
    strategy: str = "sma_crossover",
    entry_qty: float = 10.0,
) -> str:
    """Create a pending position_lifecycle row and return its
    position_uid. The orders store needs a parent row for the FK
    to resolve."""
    uid = new_position_uid()
    pos_store.create_pending(
        position_uid=uid,
        symbol=symbol or owner_key,
        owner_key=owner_key,
        strategy=strategy,
        position_type="single_leg",
        entry_qty=entry_qty,
    )
    return uid


# ── insert_pending ──────────────────────────────────────────────────────────


class TestInsertPending:
    def test_creates_row_at_pending(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        row_id = orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="sma_crossover-test001",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        row = orders_store.get_by_id(row_id)
        assert row is not None
        assert row.position_uid == uid
        assert row.role == "entry_primary"
        assert row.status == "pending"
        assert row.order_id is None  # broker hasn't assigned one yet
        assert row.submitted_at is None
        assert row.terminal_at is None
        assert row.filled_qty == 0.0
        assert row.avg_fill_price is None
        assert row.origin_kind == "bot"
        assert row.operator_command_uid is None

    def test_captures_slippage_benchmark_provenance(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """§10.5: pre-fill slippage benchmark moves from in-memory
        SuspectOrder to durable per-order row."""
        uid = _seed_position(pos_store)
        row_id = orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="sma-arrival",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
            slippage_benchmark_price=150.25,
            slippage_benchmark_kind="arrival_midpoint",
            slippage_benchmark_timestamp="2026-06-12T10:00:00+00:00",
            slippage_measurement_quality="primary",
        )
        row = orders_store.get_by_id(row_id)
        assert row.slippage_benchmark_price == pytest.approx(150.25)
        assert row.slippage_benchmark_kind == "arrival_midpoint"
        assert row.slippage_benchmark_timestamp == "2026-06-12T10:00:00+00:00"
        assert row.slippage_measurement_quality == "primary"

    def test_rejects_invalid_role(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        with pytest.raises(ValueError, match="role must be one of"):
            orders_store.insert_pending(
                position_uid=uid,
                role="bogus_role",
                client_order_id="cli-x",
                order_type="market",
                order_class="simple",
                time_in_force="day",
                side="buy",
                intended_qty=1.0,
            )

    def test_rejects_invalid_origin_kind(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        with pytest.raises(ValueError, match="origin_kind"):
            orders_store.insert_pending(
                position_uid=uid,
                role="entry_primary",
                client_order_id="cli-x",
                order_type="market",
                order_class="simple",
                time_in_force="day",
                side="buy",
                intended_qty=1.0,
                origin_kind="alien",
            )

    def test_rejects_non_positive_qty(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        with pytest.raises(ValueError, match="positive"):
            orders_store.insert_pending(
                position_uid=uid,
                role="entry_primary",
                client_order_id="cli-x",
                order_type="market",
                order_class="simple",
                time_in_force="day",
                side="buy",
                intended_qty=0.0,
            )

    def test_fk_violation_on_unknown_position_uid(
        self, orders_store: PositionLifecycleOrdersStore
    ):
        """No matching position_lifecycle row → FK rejects with
        IntegrityError (PRAGMA foreign_keys = ON, per R13-G1)."""
        with pytest.raises(sqlite3.IntegrityError):
            orders_store.insert_pending(
                position_uid=new_position_uid(),
                role="entry_primary",
                client_order_id="cli-orphan",
                order_type="market",
                order_class="simple",
                time_in_force="day",
                side="buy",
                intended_qty=1.0,
            )

    def test_rejects_duplicate_client_order_id(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid_a = _seed_position(pos_store, owner_key="AAPL")
        orders_store.insert_pending(
            position_uid=uid_a,
            role="entry_primary",
            client_order_id="cli-dup",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        # Mark first position closed so we can open a second on a
        # different owner_key. Need separate parent rows for the FK.
        pos_store.mark_closed(position_uid=uid_a)
        uid_b = _seed_position(pos_store, owner_key="MSFT")
        with pytest.raises(sqlite3.IntegrityError):
            orders_store.insert_pending(
                position_uid=uid_b,
                role="entry_primary",
                client_order_id="cli-dup",  # SAME as the first row
                order_type="market",
                order_class="oto",
                time_in_force="gtc",
                side="buy",
                intended_qty=10.0,
            )

    def test_rejects_second_entry_primary_for_same_position(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """uniq_one_entry_primary_per_position is belt-and-suspenders
        per the discovery doc — the position-level lock catches the
        cross-position case; this per-order constraint catches the
        same-position case if a bug ever tried."""
        uid = _seed_position(pos_store)
        orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="cli-1",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        with pytest.raises(sqlite3.IntegrityError):
            orders_store.insert_pending(
                position_uid=uid,
                role="entry_primary",
                client_order_id="cli-2",
                order_type="market",
                order_class="oto",
                time_in_force="gtc",
                side="buy",
                intended_qty=10.0,
            )

    def test_rejects_second_non_terminal_close_for_same_position(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """uniq_one_active_close_per_position is the durable analog
        of `_spreads_pending_close` / `_has_pending_close_order()`.
        The bot must NOT submit two concurrent discretionary closes."""
        uid = _seed_position(pos_store)
        orders_store.insert_pending(
            position_uid=uid,
            role="exit",
            client_order_id="cli-exit-1",
            order_type="market",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=10.0,
        )
        with pytest.raises(sqlite3.IntegrityError):
            orders_store.insert_pending(
                position_uid=uid,
                role="exit",
                client_order_id="cli-exit-2",
                order_type="market",
                order_class="simple",
                time_in_force="gtc",
                side="sell",
                intended_qty=10.0,
            )

    def test_protective_stop_does_NOT_collide_with_exit(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """The close-side unique index intentionally excludes
        protective_stop / replacement_stop. A live exit AND a live
        protective stop on the same position is the normal OTO
        bracket arrangement."""
        uid = _seed_position(pos_store)
        orders_store.insert_pending(
            position_uid=uid,
            role="protective_stop",
            client_order_id="cli-stop",
            order_type="stop",
            order_class="oto",
            time_in_force="gtc",
            side="sell",
            intended_qty=10.0,
            intended_stop_price=95.0,
        )
        # The exit should NOT collide with the stop.
        orders_store.insert_pending(
            position_uid=uid,
            role="exit",
            client_order_id="cli-exit",
            order_type="market",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=10.0,
        )

    def test_replacement_stop_allowed_alongside_protective_stop(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """PR #47's GTC promotion creates a replacement_stop row.
        Both the original and the replacement can be non-terminal
        for a brief window — PR #59 R8-1's note about replacement
        being an intentional second-stop pattern."""
        uid = _seed_position(pos_store)
        orders_store.insert_pending(
            position_uid=uid,
            role="protective_stop",
            client_order_id="cli-stop-1",
            order_type="stop",
            order_class="oto",
            time_in_force="day",
            side="sell",
            intended_qty=10.0,
            intended_stop_price=95.0,
        )
        # Replacement coexists with the original (briefly).
        orders_store.insert_pending(
            position_uid=uid,
            role="replacement_stop",
            client_order_id="cli-stop-2",
            order_type="stop",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=10.0,
            intended_stop_price=95.0,
            replaces_order_id="ORDER-OLD",
        )


# ── attach_broker_order_id ─────────────────────────────────────────────────


class TestAttachBrokerOrderId:
    def test_populates_order_id_and_submitted_at(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="cli-attach",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-attach",
            order_id="alpaca-order-1",
        )
        row = orders_store.get_by_client_order_id("cli-attach")
        assert row is not None
        assert row.order_id == "alpaca-order-1"
        assert row.submitted_at is not None
        # last_observed_at also stamped to support the strict-newer
        # check in apply_order_event.
        assert row.last_observed_at >= row.created_at

    def test_rejects_unknown_client_order_id(
        self, orders_store: PositionLifecycleOrdersStore
    ):
        with pytest.raises(ValueError, match="unknown client_order_id"):
            orders_store.attach_broker_order_id(
                client_order_id="cli-never",
                order_id="alpaca-1",
            )

    def test_rejects_double_attach(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="cli-once",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-once",
            order_id="alpaca-1",
        )
        with pytest.raises(ValueError, match="already has order_id"):
            orders_store.attach_broker_order_id(
                client_order_id="cli-once",
                order_id="alpaca-2",
            )

    def test_rejects_attach_after_pending(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """If the row has somehow advanced past pending (e.g. through
        apply_order_event in a future commit), attach must refuse.
        Order_id is the broker-assignment moment; it cannot be set
        retroactively on a working / terminal row."""
        uid = _seed_position(pos_store)
        orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="cli-late",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        # Simulate a manual advance to 'working' (apply_order_event
        # lands in commit 4; for now bypass it directly).
        conn.execute(
            "UPDATE position_lifecycle_orders "
            "SET status = 'working', order_id = 'pre-set' "
            "WHERE client_order_id = ?",
            ("cli-late",),
        )
        conn.commit()
        with pytest.raises(ValueError, match="cannot attach order_id after pending"):
            orders_store.attach_broker_order_id(
                client_order_id="cli-late",
                order_id="alpaca-new",
            )


# ── Read helpers ───────────────────────────────────────────────────────────


class TestReads:
    def test_get_all_for_position_returns_insertion_order(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        ids = []
        for role, cli in [
            ("entry_primary", "cli-1"),
            ("protective_stop", "cli-2"),
            ("exit", "cli-3"),
        ]:
            ids.append(
                orders_store.insert_pending(
                    position_uid=uid,
                    role=role,
                    client_order_id=cli,
                    order_type="market" if role == "exit" else "stop"
                    if "stop" in role else "market",
                    order_class="oto" if role == "entry_primary" else "simple",
                    time_in_force="gtc",
                    side="buy" if role == "entry_primary" else "sell",
                    intended_qty=10.0,
                    intended_stop_price=95.0 if "stop" in role else None,
                )
            )
        rows = orders_store.get_all_for_position(uid)
        assert [r.id for r in rows] == ids

    def test_get_non_terminal_for_position(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        entry_id = orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="cli-entry",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        exit_id = orders_store.insert_pending(
            position_uid=uid,
            role="exit",
            client_order_id="cli-exit",
            order_type="market",
            order_class="simple",
            time_in_force="gtc",
            side="sell",
            intended_qty=10.0,
        )
        # Mark the entry filled directly (apply_order_event lands in
        # commit 4; for these tests we manipulate state directly).
        conn.execute(
            "UPDATE position_lifecycle_orders "
            "SET status = 'filled', filled_qty = 10.0 "
            "WHERE id = ?",
            (entry_id,),
        )
        conn.commit()
        rows = orders_store.get_non_terminal_for_position(uid)
        assert [r.id for r in rows] == [exit_id]

    def test_get_non_terminal_by_role(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        entry_id = orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="cli-entry",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        stop_id = orders_store.insert_pending(
            position_uid=uid,
            role="protective_stop",
            client_order_id="cli-stop",
            order_type="stop",
            order_class="oto",
            time_in_force="gtc",
            side="sell",
            intended_qty=10.0,
            intended_stop_price=95.0,
        )
        # Stop-side rows only.
        rows = orders_store.get_non_terminal_by_role(uid, STOP_SIDE_ROLES)
        assert [r.id for r in rows] == [stop_id]
        # Close-side: none.
        assert orders_store.get_non_terminal_by_role(uid, CLOSE_SIDE_ROLES) == []
        # Sell-side: just the stop in this setup.
        assert {r.id for r in orders_store.get_non_terminal_by_role(uid, SELL_SIDE_ROLES)} == {stop_id}

    def test_get_by_order_id_and_client_order_id(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="cli-lookup",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        # Before attach: order_id is NULL so lookup-by-order-id returns None.
        assert orders_store.get_by_order_id("alpaca-lookup") is None
        # client_order_id works pre-attach.
        row = orders_store.get_by_client_order_id("cli-lookup")
        assert row is not None and row.order_id is None
        # After attach: both lookups work.
        orders_store.attach_broker_order_id(
            client_order_id="cli-lookup",
            order_id="alpaca-lookup",
        )
        row = orders_store.get_by_order_id("alpaca-lookup")
        assert row is not None
        assert row.client_order_id == "cli-lookup"


# ── Frozen-row property ────────────────────────────────────────────────────


class TestRowImmutability:
    """Returned rows are frozen dataclasses — callers can't mutate."""

    def test_row_is_frozen(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        row_id = orders_store.insert_pending(
            position_uid=uid,
            role="entry_primary",
            client_order_id="cli-frozen",
            order_type="market",
            order_class="oto",
            time_in_force="gtc",
            side="buy",
            intended_qty=10.0,
        )
        row = orders_store.get_by_id(row_id)
        with pytest.raises((AttributeError, Exception)):
            row.status = "filled"  # type: ignore
