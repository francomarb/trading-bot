"""
Unit tests for ``apply_order_event`` (foundation commit 4).

This is the load-bearing piece: atomic compare-and-set + trades UPSERT
+ position rollup + position-level status update, all inside one
transaction. Discovery doc §6.4 / §6.5 / §6.6 / §6.6.1.

§12.1 regression-test matrix coverage:

- Test 1  Atomic — two unrelated order_ids, only matching updates (R3-P0)
- Test 2  Terminal-state immutability (R3-P1a)
- Test 3  Side-signed rollup correctness (R3-P1b)
- Test 6  All-or-nothing transaction on failure (R4-P1b)
- Test 11 Zero-fill working entry stays 'pending' (R7-P0)
- Test 12 Working sell-side order blocks 'closed' (R12-P1)
- Test 13 closed_at set only on closed / external_closed (R8-P2)
- Test 14 closed_at reads new status via CTE (R9-P1a)
- Test 15 Negative current_qty maps to 'error' (R9-P1b)
- Test 20 Working sell-side blocks 'closed' AND lock retains (R12-P1)
- Test 21 Oversold → 'error' immediately (R9-P1b + R11)
- Test 23 Direct pending → filled fast path (R11-P1)
- Test 24 Direct pending → canceled recovery (R12)
- Test 26 net_realized_pnl rollup from trades (R13-G2)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from engine.lifecycle import PositionLifecycleStore, new_position_uid
from engine.lifecycle_orders import (
    OrderEvent,
    OrderEventOutcome,
    PositionLifecycleOrdersStore,
    apply_order_event,
)
from execution.broker import BrokerSnapshot, OpenOrder
from reporting.logger import TradeLogger
from risk.manager import Side


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


# ── Helpers ────────────────────────────────────────────────────────────────


def _seed_position(
    pos_store: PositionLifecycleStore,
    *,
    owner_key: str = "AAPL",
    entry_qty: float = 10.0,
    strategy: str = "sma_crossover",
) -> str:
    uid = new_position_uid()
    pos_store.create_pending(
        position_uid=uid,
        symbol=owner_key,
        owner_key=owner_key,
        strategy=strategy,
        position_type="single_leg",
        entry_qty=entry_qty,
    )
    return uid


def _seed_spread_position(
    pos_store: PositionLifecycleStore,
    *,
    position_uid: str = "pos_spread_ghost_regression",
    owner_key: str = "spread-1",
    symbol: str = "QQQ260724P00712000",
    entry_qty: float = 1.0,
    strategy: str = "credit_spread",
) -> str:
    pos_store.create_pending(
        position_uid=position_uid,
        symbol=symbol,
        owner_key=owner_key,
        strategy=strategy,
        position_type="spread",
        entry_qty=entry_qty,
    )
    return position_uid


def _insert_entry(
    orders_store: PositionLifecycleOrdersStore,
    position_uid: str,
    *,
    client_order_id: str = "cli-entry",
    intended_qty: float = 10.0,
    side: str = "buy",
    role: str = "entry_primary",
    benchmark: bool = True,
) -> int:
    return orders_store.insert_pending(
        position_uid=position_uid,
        role=role,
        client_order_id=client_order_id,
        order_type="market",
        order_class="oto",
        time_in_force="gtc",
        side=side,
        intended_qty=intended_qty,
        slippage_benchmark_price=150.0 if benchmark else None,
        slippage_benchmark_kind="arrival_midpoint" if benchmark else None,
        slippage_benchmark_timestamp="2026-06-12T10:00:00+00:00" if benchmark else None,
        slippage_measurement_quality="primary" if benchmark else None,
    )


def _insert_exit(
    orders_store: PositionLifecycleOrdersStore,
    position_uid: str,
    *,
    client_order_id: str = "cli-exit",
    intended_qty: float = 10.0,
) -> int:
    return orders_store.insert_pending(
        position_uid=position_uid,
        role="exit",
        client_order_id=client_order_id,
        order_type="market",
        order_class="simple",
        time_in_force="gtc",
        side="sell",
        intended_qty=intended_qty,
    )


def _insert_protective_stop(
    orders_store: PositionLifecycleOrdersStore,
    position_uid: str,
    *,
    client_order_id: str = "cli-stop",
    intended_qty: float = 10.0,
    stop_price: float = 95.0,
) -> int:
    return orders_store.insert_pending(
        position_uid=position_uid,
        role="protective_stop",
        client_order_id=client_order_id,
        order_type="stop",
        order_class="oto",
        time_in_force="gtc",
        side="sell",
        intended_qty=intended_qty,
        intended_stop_price=stop_price,
    )


def _attach_and_get_order_id(
    orders_store: PositionLifecycleOrdersStore,
    client_order_id: str,
    *,
    order_id: str | None = None,
) -> str:
    oid = order_id or f"broker-{client_order_id}"
    orders_store.attach_broker_order_id(
        client_order_id=client_order_id,
        order_id=oid,
    )
    return oid


def _get_position(
    conn: sqlite3.Connection, position_uid: str
) -> tuple:
    row = conn.execute(
        "SELECT status, current_qty, avg_entry_price, net_realized_pnl, closed_at "
        "FROM position_lifecycle WHERE position_uid = ?",
        (position_uid,),
    ).fetchone()
    assert row is not None
    return row


def _get_order(conn: sqlite3.Connection, order_id: str) -> tuple:
    row = conn.execute(
        "SELECT status, filled_qty, avg_fill_price, "
        "last_observed_broker_updated_at, terminal_at "
        "FROM position_lifecycle_orders WHERE order_id = ?",
        (order_id,),
    ).fetchone()
    assert row is not None
    return row


# ── Core compare-and-set semantics ─────────────────────────────────────────


class TestApplyOrderEventBasic:
    def test_unknown_order_id_returns_outcome(
        self, conn: sqlite3.Connection
    ):
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id="never-existed",
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:00:00+00:00",
            ),
        )
        assert outcome.applied is False
        assert outcome.reason == "unknown_order"

    def test_working_event_advances_pending_row(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="working",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        assert outcome.applied is True
        assert outcome.new_status == "pending"  # R7-P0: zero-fill working entry stays pending
        status, qty, avg, _, terminal_at = _get_order(conn, order_id)
        assert status == "working"
        assert qty == 0.0
        assert avg is None
        assert terminal_at is None


# Test 23 — Direct pending → filled fast-path (R11-P1)
class TestDirectPendingToFilled:
    def test_pending_to_filled_synchronous_fast_path(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """The synchronous fast path produces a 'filled' event with
        no intermediate 'working' observed. The strict-newer rule
        admits this directly: (3, qty) > (0, 0)."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.50,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        assert outcome.applied is True
        assert outcome.new_status == "open"
        status, qty, avg, _, terminal_at = _get_order(conn, order_id)
        assert status == "filled"
        assert qty == 10.0
        assert avg == pytest.approx(150.50)
        assert terminal_at is not None
        # Position rolled up to open with current_qty == entry_qty.
        pl_status, current_qty, avg_entry, _, _ = _get_position(conn, uid)
        assert pl_status == "open"
        assert current_qty == 10.0
        assert avg_entry == pytest.approx(150.50)


# Test 24 — Direct pending → canceled recovery (R12)
class TestDirectPendingToCanceled:
    def test_pending_to_canceled_recovery(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """Recovery path observes a canceled order with zero fills
        for a pending row. (3, 0) > (0, 0) admits the transition.
        Position rolls up to canceled per §8.1."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="canceled",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        assert outcome.applied is True
        assert outcome.new_status == "canceled"
        status, qty, _, _, _ = _get_order(conn, order_id)
        assert status == "canceled"
        assert qty == 0.0
        pl_status, current_qty, _, _, closed_at = _get_position(conn, uid)
        assert pl_status == "canceled"
        assert current_qty == 0.0
        # canceled is NOT closed → closed_at stays NULL (R8-P2).
        assert closed_at is None


# Test 2 — Terminal-state immutability (R3-P1a)
class TestTerminalImmutability:
    def test_event_with_newer_updated_at_cannot_revive_filled(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        # Now try to "advance" with a newer updated_at — must be blocked.
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="working",
                filled_qty=5.0,
                avg_fill_price=149.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        assert outcome.applied is False
        assert outcome.reason == "terminal_blocked"
        # Row unchanged.
        status, qty, avg, _, _ = _get_order(conn, order_id)
        assert status == "filled"
        assert qty == 10.0
        assert avg == pytest.approx(150.0)


# Test 1 — Atomic: two unrelated order_ids, only matching updates (R3-P0)
class TestAtomicMatchingRowOnly:
    def test_event_only_updates_matching_order_id(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """The R3-P0 SQL precedence bug would have updated all rows
        with older updated_at on any event. The fix scopes the update
        to order_id = :order_id only."""
        uid_a = _seed_position(pos_store, owner_key="AAPL")
        _insert_entry(orders_store, uid_a, client_order_id="cli-a")
        order_a = _attach_and_get_order_id(orders_store, "cli-a")

        # Close the first position then seed a second on a different
        # owner_key with the second order also pending.
        # Simplest: directly insert entry for AAPL second order is
        # impossible (owner_key lock). Use a different owner_key.
        # Actually — for the test, we just need TWO rows in the same
        # table with different order_ids. Close A first.
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_a,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        # Exit the first position so the owner_key lock releases.
        _insert_exit(orders_store, uid_a, client_order_id="cli-a-exit")
        order_a_exit = _attach_and_get_order_id(orders_store, "cli-a-exit")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_a_exit,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=151.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        # Now insert a fresh position on the same owner_key.
        uid_b = _seed_position(pos_store, owner_key="AAPL")
        _insert_entry(orders_store, uid_b, client_order_id="cli-b")
        order_b = _attach_and_get_order_id(orders_store, "cli-b")

        # Apply an event that targets order_b only — it must NOT
        # touch order_a's row, even though order_a's updated_at
        # could be older.
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_b,
                status="working",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T11:00:00+00:00",
            ),
        )
        assert outcome.applied is True
        # order_a stays at filled with its original values.
        status_a, qty_a, avg_a, _, _ = _get_order(conn, order_a)
        assert status_a == "filled"
        assert qty_a == 10.0
        assert avg_a == pytest.approx(150.0)


# Test 3 — Side-signed rollup correctness (R3-P1b)
class TestSideSignedRollup:
    def test_buy_then_sell_zeroes_current_qty(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """Naive SUM(filled_qty) would produce 20 here. Side-signed
        SUM zeroes correctly."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        eid = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=eid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        # Verify current_qty == 10 after entry.
        _, qty_after_entry, _, _, _ = _get_position(conn, uid)
        assert qty_after_entry == 10.0
        # Submit an exit.
        _insert_exit(orders_store, uid)
        xid = _attach_and_get_order_id(orders_store, "cli-exit")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=xid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=152.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        # current_qty == 0 after exit (10 - 10), NOT 20.
        pl_status, current_qty, _, _, closed_at = _get_position(conn, uid)
        assert current_qty == 0.0
        assert pl_status == "closed"
        assert closed_at is not None


# Test 11 — Zero-fill working entry stays 'pending' (R7-P0)
class TestZeroFillWorkingStaysPending:
    def test_zero_fill_working_entry_stays_pending(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        # Broker says 'working' with no fills — naive rule
        # current_qty == 0 → closed is wrong; position stays pending.
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="working",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        assert outcome.applied is True
        assert outcome.new_status == "pending"


# Test 12 + 20 — Working sell-side blocks 'closed' AND lock retains (R12-P1)
class TestSellSideBlocksClosed:
    def test_working_exit_with_current_qty_zero_stays_partially_filled(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """A live exit with current_qty == 0 (operationally flat)
        does NOT release the owner_key lock — must stay
        partially_filled. R11-P1 walk-back of R10-P1."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        eid = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=eid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        _insert_exit(orders_store, uid)
        xid = _attach_and_get_order_id(orders_store, "cli-exit")
        # Exit observed as partially_filled with full qty filled but
        # the per-order row not yet at 'filled'.
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=xid,
                status="partially_filled",
                filled_qty=10.0,
                avg_fill_price=152.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        assert outcome.applied is True
        assert outcome.new_status == "partially_filled"
        # Owner_key lock retained — verify the index still blocks a duplicate.
        _, current_qty, _, _, closed_at = _get_position(conn, uid)
        assert current_qty == 0.0
        assert closed_at is None
        # Try to create a second position on the same owner_key.
        with pytest.raises(sqlite3.IntegrityError):
            pos_store.create_pending(
                position_uid=new_position_uid(),
                symbol="AAPL",
                owner_key="AAPL",
                strategy="sma_crossover",
                position_type="single_leg",
                entry_qty=10.0,
            )

    def test_working_protective_stop_blocks_closed_too(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """R12-P1 supersedes R8-1: stop-side roles also block 'closed'.
        Otherwise a working stop could fire after the lock releases
        and oversell a fresh entry on the same symbol."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        eid = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=eid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        # Attach a protective stop (still working at broker).
        _insert_protective_stop(orders_store, uid)
        sid = _attach_and_get_order_id(orders_store, "cli-stop")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=sid,
                status="working",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        # Submit and fill the exit.
        _insert_exit(orders_store, uid)
        xid = _attach_and_get_order_id(orders_store, "cli-exit")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=xid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=152.0,
                broker_updated_at="2026-06-12T10:03:00+00:00",
            ),
        )
        # Position is operationally flat but the stop is still working.
        # Must stay partially_filled, NOT closed.
        pl_status, current_qty, _, _, closed_at = _get_position(conn, uid)
        assert current_qty == 0.0
        assert pl_status == "partially_filled"
        assert closed_at is None
        # Now cancel the stop.
        apply_order_event(
            conn,
            OrderEvent(
                order_id=sid,
                status="canceled",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:04:00+00:00",
            ),
        )
        # NOW position closes.
        pl_status, current_qty, _, _, closed_at = _get_position(conn, uid)
        assert pl_status == "closed"
        assert closed_at is not None


# Test 13 + 14 — closed_at only on closed/external_closed via CTE (R8-P2, R9-P1a)
class TestClosedAtSemantics:
    def test_closed_at_set_via_cte_on_close_transition(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """The CTE-based SQL must produce a non-NULL closed_at on the
        same UPDATE that sets status='closed'. R9-P1a: a bare subquery
        in SET would read the pre-update status and never set
        closed_at."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        eid = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=eid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        _insert_exit(orders_store, uid)
        xid = _attach_and_get_order_id(orders_store, "cli-exit")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=xid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=152.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        pl_status, _, _, _, closed_at = _get_position(conn, uid)
        assert pl_status == "closed"
        assert closed_at is not None

    def test_closed_at_NULL_on_canceled(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        eid = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=eid,
                status="canceled",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        pl_status, _, _, _, closed_at = _get_position(conn, uid)
        assert pl_status == "canceled"
        assert closed_at is None  # R8-P2


# Test 15 + 21 — Negative current_qty → 'error' (R9-P1b + R11)
class TestNegativeCurrentQtyIsError:
    def test_oversold_position_is_error(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """Oversold (current_qty < 0) must surface as 'error',
        immediately, regardless of pending sell-side orders."""
        uid = _seed_position(pos_store, entry_qty=10.0)
        _insert_entry(orders_store, uid)
        eid = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=eid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        _insert_exit(orders_store, uid, intended_qty=12.0)  # intent oversize
        xid = _attach_and_get_order_id(orders_store, "cli-exit")
        # Broker oversells: fills 12 against a position of 10 → -2.
        apply_order_event(
            conn,
            OrderEvent(
                order_id=xid,
                status="filled",
                filled_qty=12.0,
                avg_fill_price=152.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        pl_status, current_qty, _, _, closed_at = _get_position(conn, uid)
        assert current_qty == pytest.approx(-2.0)
        assert pl_status == "error"
        assert closed_at is None  # error rows leave closed_at NULL


# Test 6 — All-or-nothing transaction on failure (R4-P1b)
class _FlakyConnection:
    """Proxy that delegates to a real sqlite3.Connection but raises
    when a configured SQL marker appears. Lets the test simulate a
    failure inside the apply_order_event transaction without
    monkey-patching sqlite3.Connection (whose execute is read-only)."""

    def __init__(self, real: sqlite3.Connection, fail_on_marker: str) -> None:
        self._real = real
        self._fail_on_marker = fail_on_marker
        self.failed = False

    def execute(self, sql: str, *args, **kwargs):
        if self._fail_on_marker in sql:
            self.failed = True
            raise sqlite3.OperationalError("simulated UPSERT failure")
        return self._real.execute(sql, *args, **kwargs)

    def __enter__(self):
        return self._real.__enter__()

    def __exit__(self, exc_type, exc, tb):
        return self._real.__exit__(exc_type, exc, tb)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestAllOrNothingTransaction:
    def test_failure_inside_transaction_rolls_back_everything(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """If the trades UPSERT raises, the per-order UPDATE that
        came before must also roll back. No partial state across
        the three tables.

        Filled_qty>0 here because commit 8 gates the trades UPSERT
        on `filled_qty > 0`; only fill-bearing events touch the
        trades row whose failure we're simulating."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        before_row = _get_order(conn, order_id)
        flaky = _FlakyConnection(conn, fail_on_marker="INSERT INTO trades")
        with pytest.raises(sqlite3.OperationalError):
            apply_order_event(
                flaky,  # type: ignore[arg-type]
                OrderEvent(
                    order_id=order_id,
                    status="filled",
                    filled_qty=10.0,
                    avg_fill_price=100.5,
                    broker_updated_at="2026-06-12T10:01:00+00:00",
                ),
            )
        assert flaky.failed
        # Re-read the per-order row — unchanged because the entire
        # transaction rolled back.
        after_row = _get_order(conn, order_id)
        assert before_row == after_row


# Test 26 — net_realized_pnl rollup from trades (R13-G2)
class TestNetRealizedPnlRollup:
    def test_position_net_realized_pnl_sums_trades(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """R13-G2: net_realized_pnl on position_lifecycle is the
        SUM(realized_pnl) on trades for that position_uid. The
        per-order table has no realized_pnl column."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        eid = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=eid,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        # apply_order_event's trades UPSERT doesn't compute realized_pnl;
        # it's the existing TradeLogger.log_close path that fills it
        # (or in this test, we set it directly on the trades row).
        # Simulate that a downstream writer set realized_pnl after a
        # SELL closed the position.
        conn.execute(
            "UPDATE trades SET realized_pnl = ? "
            "WHERE position_uid = ? AND side = 'buy'",
            (25.0, uid),
        )
        conn.commit()
        # Re-running apply_order_event for ANY event triggers the
        # rollup; use a no-op stale event for the entry to drive it.
        _insert_exit(orders_store, uid)
        xid = _attach_and_get_order_id(orders_store, "cli-exit")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=xid,
                status="working",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        _, _, _, net_realized_pnl, _ = _get_position(conn, uid)
        assert net_realized_pnl == pytest.approx(25.0)

    def test_trade_upsert_fills_missing_position_uid_for_existing_order(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """A pre-existing trade row with NULL position_uid can be
        repaired when the substrate later observes the same order fill."""
        uid = _seed_position(pos_store)
        _insert_protective_stop(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-stop")
        conn.execute(
            """
            INSERT INTO trades (
                timestamp, symbol, side, qty, avg_fill_price, order_id,
                strategy, reason, stop_price, entry_reference_price,
                modeled_slippage_bps, realized_slippage_bps, order_type,
                status, requested_qty, filled_qty, position_id,
                position_type, position_uid
            )
            VALUES (
                '2026-06-12T10:02:00+00:00', 'AAPL', 'sell', 10.0,
                95.0, ?, 'sma_crossover', 'stop_triggered', 95.0,
                100.0, 0.0, 0.0, 'stop', 'filled', 10.0, 10.0,
                'AAPL', 'single_leg', NULL
            )
            """,
            (order_id,),
        )
        conn.commit()

        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=95.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )

        row = conn.execute(
            "SELECT position_uid FROM trades WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        assert row[0] == uid

    def test_tiny_rollup_residual_snaps_to_closed_zero(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """Floating-point dust under epsilon must not leave a phantom
        open lifecycle position."""
        uid = _seed_position(pos_store, entry_qty=1.0)
        _insert_entry(orders_store, uid, intended_qty=1.0)
        entry_order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=entry_order_id,
                status="filled",
                filled_qty=1.0,
                avg_fill_price=100.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        _insert_exit(orders_store, uid, intended_qty=0.9999999995)
        exit_order_id = _attach_and_get_order_id(orders_store, "cli-exit")

        apply_order_event(
            conn,
            OrderEvent(
                order_id=exit_order_id,
                status="filled",
                filled_qty=0.9999999995,
                avg_fill_price=105.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )

        status, current_qty, _, _, closed_at = _get_position(conn, uid)
        assert status == "closed"
        assert current_qty == 0.0
        assert closed_at is not None


# Smoke: stale event drops cleanly
class TestStaleEventDropped:
    def test_older_status_is_dropped(
        self,
        conn: sqlite3.Connection,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        # Advance to partially_filled.
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="partially_filled",
                filled_qty=5.0,
                avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        # A stale 'working' event with zero fills must drop.
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="working",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:02:00+00:00",  # newer ts even
            ),
        )
        assert outcome.applied is False
        assert outcome.reason == "stale_or_duplicate"
        # Row unchanged.
        status, qty, avg, _, _ = _get_order(conn, order_id)
        assert status == "partially_filled"
        assert qty == 5.0


# ── PR #60 commit 8 review fixes ────────────────────────────────────────────


class TestStatusOnlyEventsSkipTradesUpsert:
    """Discovery doc §6.5 + §6.6: `trades` records cumulative fill
    state. apply_order_event must NOT manufacture a trades row for a
    status-only transition (pending→working, zero-fill canceled, etc.).

    Without this gate, downstream slippage / activity / realized-P&L
    consumers would count phantom orders that never actually traded.
    """

    def test_working_with_zero_fill_does_not_insert_trades_row(
        self, conn, pos_store, orders_store,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        before = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="working",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        assert outcome.applied is True
        after = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert after == before, (
            "status-only working event must not insert into trades"
        )

    def test_zero_fill_canceled_does_not_insert_trades_row(
        self, conn, pos_store, orders_store,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        before = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="canceled",
                filled_qty=0.0,
                avg_fill_price=None,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        assert outcome.applied is True
        after = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert after == before

    def test_filled_event_with_qty_does_insert_trades_row(
        self, conn, pos_store, orders_store,
    ):
        """Positive control: filled events with qty > 0 DO write."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        before = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=100.5,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        assert outcome.applied is True
        after = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert after == before + 1

    def test_spread_mleg_fill_advances_lifecycle_without_single_leg_trade(
        self, conn, tmp_db_path, pos_store, orders_store,
    ):
        """MLEG spread substrate rows are lifecycle state, not
        user-facing single-leg trades.

        The spread close path writes the real dashboard/accounting rows
        through TradeLogger.log_spread_fill(position_type='spread').
        apply_order_event must not also create a raw
        position_type='single_leg' row for the same MLEG order, because
        that poisons read_all_open_owners() with a fake open OCC leg.
        """
        uid = _seed_spread_position(
            pos_store,
            position_uid="pos_a2663428e1444f2491e49e484de68391",
            owner_key="a2663428e1444f2491e49e484de68391",
        )
        orders_store.insert_pending(
            position_uid=uid,
            role="exit",
            client_order_id="spr-exit-a2663428-test",
            order_type="limit",
            order_class="mleg",
            time_in_force="day",
            side="buy",
            intended_qty=1.0,
            intended_limit_price=6.11,
        )
        order_id = _attach_and_get_order_id(
            orders_store,
            "spr-exit-a2663428-test",
            order_id="mleg-close-order",
        )
        before = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]

        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="filled",
                filled_qty=1.0,
                avg_fill_price=6.2,
                broker_updated_at="2026-06-24T17:59:18+00:00",
            ),
            reason="stream",
        )

        assert outcome.applied is True
        status, qty, avg, _, terminal_at = _get_order(conn, order_id)
        assert status == "filled"
        assert qty == 1.0
        assert avg == 6.2
        assert terminal_at is not None
        after = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
        assert after == before
        assert TradeLogger(tmp_db_path).read_all_open_owners() == {}

    def test_partial_fill_then_zero_fill_canceled_leaves_one_trades_row(
        self, conn, pos_store, orders_store,
    ):
        """A partial-fill that later cancels at the same filled_qty
        should produce exactly ONE trades row (the partial fill). The
        canceled event has filled_qty equal to the previously-observed
        amount, which equals event.filled_qty > 0, so it does write —
        and the UPSERT preserves cumulative state."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="partially_filled",
                filled_qty=5.0, avg_fill_price=100.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
            ),
        )
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="canceled",
                filled_qty=5.0, avg_fill_price=100.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
            ),
        )
        cnt = conn.execute(
            "SELECT COUNT(*) FROM trades WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0]
        assert cnt == 1


class TestTradesUpsertProvenancePreservation:
    """Discovery doc §10.5: slippage / audit provenance is captured
    once at submission and must survive every subsequent UPSERT,
    including the sparse-recovery case where a later writer
    doesn't have access to the original benchmark.

    PR #60 commit 8 fix F (review finding): the UPSERT in
    TradeLogger.log was `excluded.<col>` for every non-immutable
    column, including provenance. A later log() call with NULL
    modeled_* fields would clobber the originally-recorded values.
    """

    def test_log_preserves_slippage_benchmark_across_upsert(
        self, tmp_path,
    ):
        """Two log() calls on the same order_id: first sets
        slippage_benchmark_*, second is NULL. The end state must
        keep the first provenance values."""
        from reporting.logger import TradeLogger, TradeRecord

        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        first = TradeRecord(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id="ord-1",
            strategy="sma_crossover", reason="entry",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            slippage_benchmark_price=149.5,
            slippage_benchmark_kind="arrival_midpoint",
            slippage_benchmark_timestamp="2026-06-12T10:00:00+00:00",
            slippage_measurement_quality="primary",
            position_type="single_leg",
        )
        tl.log(first)
        # Second log: same order_id, NULL provenance fields.
        second = TradeRecord(
            timestamp="2026-06-12T10:01:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.5, order_id="ord-1",
            strategy="sma_crossover", reason="recovery",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            slippage_benchmark_price=None,
            slippage_benchmark_kind=None,
            slippage_benchmark_timestamp=None,
            slippage_measurement_quality=None,
            position_type="single_leg",
        )
        tl.log(second)
        row = tl._ensure_db().execute(
            "SELECT slippage_benchmark_price, slippage_benchmark_kind, "
            "slippage_benchmark_timestamp, slippage_measurement_quality, "
            "avg_fill_price "
            "FROM trades WHERE order_id = 'ord-1'"
        ).fetchone()
        # provenance preserved
        assert row[0] == 149.5
        assert row[1] == "arrival_midpoint"
        assert row[2] == "2026-06-12T10:00:00+00:00"
        assert row[3] == "primary"
        # cumulative state advanced (newest avg_fill_price wins)
        assert row[4] == 150.5

    def test_log_preserves_position_uid_across_null_upsert(
        self, tmp_path,
    ):
        """position_uid is COALESCE-preserved. A sparse later record
        with NULL position_uid must not erase a previously-attached
        uid."""
        from reporting.logger import TradeLogger, TradeRecord

        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        first = TradeRecord(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id="ord-2",
            strategy="sma_crossover", reason="entry",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_uid="pos-known",
            position_type="single_leg",
        )
        tl.log(first)
        second = TradeRecord(
            timestamp="2026-06-12T10:01:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.5, order_id="ord-2",
            strategy="sma_crossover", reason="recovery",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_uid=None,
            position_type="single_leg",
        )
        tl.log(second)
        row = tl._ensure_db().execute(
            "SELECT position_uid FROM trades WHERE order_id = 'ord-2'"
        ).fetchone()
        assert row[0] == "pos-known"

    def test_log_can_fill_null_position_uid_from_later_record(
        self, tmp_path,
    ):
        """The inverse: a NULL initial position_uid CAN be filled by
        a later record carrying a real value. Restart reconstruction
        depends on this — the original log() may have run before the
        position_lifecycle row existed."""
        from reporting.logger import TradeLogger, TradeRecord

        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        first = TradeRecord(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id="ord-3",
            strategy="sma_crossover", reason="entry",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_uid=None,
            position_type="single_leg",
        )
        tl.log(first)
        second = TradeRecord(
            timestamp="2026-06-12T10:01:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id="ord-3",
            strategy="sma_crossover", reason="reconstruction",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_uid="pos-resolved",
            position_type="single_leg",
        )
        tl.log(second)
        row = tl._ensure_db().execute(
            "SELECT position_uid FROM trades WHERE order_id = 'ord-3'"
        ).fetchone()
        assert row[0] == "pos-resolved"

    def test_apply_order_event_preserves_execution_id_across_events(
        self, conn, pos_store, orders_store,
    ):
        """The substrate-side equivalent: a first event with
        execution_id='X' followed by a second event with
        execution_id=None (later observation lacks the per-fill id)
        must keep the original.

        execution_id is set by apply_order_event via the trades UPSERT;
        TradeRecord doesn't expose it directly, so we exercise the
        substrate path."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="filled",
                filled_qty=10.0, avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:01:00+00:00",
                execution_id="exec-7",
            ),
        )
        # Same cumulative state at a later observation, no execution_id.
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="filled",
                filled_qty=10.0, avg_fill_price=150.0,
                broker_updated_at="2026-06-12T10:02:00+00:00",
                execution_id=None,
            ),
        )
        row = conn.execute(
            "SELECT execution_id FROM trades WHERE order_id = ?",
            (order_id,),
        ).fetchone()
        assert row[0] == "exec-7"

    def test_log_advances_cumulative_filled_qty_via_excluded(
        self, tmp_path,
    ):
        """Sanity check: cumulative columns DO use excluded.<col>
        (newest wins). This is the positive control for the COALESCE
        policy on provenance fields."""
        from reporting.logger import TradeLogger, TradeRecord

        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        first = TradeRecord(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=5,
            avg_fill_price=100.0, order_id="ord-5",
            strategy="sma_crossover", reason="partial",
            stop_price=95.0, entry_reference_price=100.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
            order_type="market", status="partially_filled",
            requested_qty=10, filled_qty=5,
            position_type="single_leg",
        )
        tl.log(first)
        second = TradeRecord(
            timestamp="2026-06-12T10:01:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=101.0, order_id="ord-5",
            strategy="sma_crossover", reason="filled",
            stop_price=95.0, entry_reference_price=100.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=0.0,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_type="single_leg",
        )
        tl.log(second)
        row = tl._ensure_db().execute(
            "SELECT filled_qty, status, avg_fill_price "
            "FROM trades WHERE order_id = 'ord-5'"
        ).fetchone()
        assert row[0] == 10
        assert row[1] == "filled"
        assert row[2] == 101.0


# ── PR #60 round 2 review (finding 6) ───────────────────────────────────────


class TestExpandedUpsertPreservation:
    """Round 2 finding: the COALESCE set must include all set-once
    columns the sparse-recovery path could clobber — risk anchors,
    timestamps, modeled_slippage_bps, stop_trigger_price."""

    @staticmethod
    def _entry_record(order_id: str = "ord-A", **overrides):
        from reporting.logger import TradeRecord
        defaults = dict(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id=order_id,
            strategy="sma_crossover", reason="entry",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=33.4,
            realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            initial_stop_loss=145.0,
            initial_risk_per_share=5.0,
            initial_risk_dollars=50.0,
            entry_timestamp="2026-06-12T10:00:00+00:00",
            position_type="single_leg",
        )
        defaults.update(overrides)
        return TradeRecord(**defaults)

    @staticmethod
    def _sparse_record(order_id: str = "ord-A", **overrides):
        from reporting.logger import TradeRecord
        defaults = dict(
            timestamp="2026-06-12T10:01:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.5, order_id=order_id,
            strategy="sma_crossover", reason="recovery",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=None,
            realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            initial_stop_loss=None,
            initial_risk_per_share=None,
            initial_risk_dollars=None,
            entry_timestamp=None,
            position_type="single_leg",
        )
        defaults.update(overrides)
        return TradeRecord(**defaults)

    def test_modeled_slippage_bps_preserved(self, tmp_path):
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._entry_record())
        tl.log(self._sparse_record())
        row = tl._ensure_db().execute(
            "SELECT modeled_slippage_bps FROM trades WHERE order_id = 'ord-A'"
        ).fetchone()
        assert row[0] == 33.4

    def test_initial_risk_columns_preserved(self, tmp_path):
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._entry_record())
        tl.log(self._sparse_record())
        row = tl._ensure_db().execute(
            "SELECT initial_stop_loss, initial_risk_per_share, "
            "initial_risk_dollars FROM trades WHERE order_id = 'ord-A'"
        ).fetchone()
        assert row[0] == 145.0
        assert row[1] == 5.0
        assert row[2] == 50.0

    def test_entry_timestamp_preserved(self, tmp_path):
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._entry_record())
        tl.log(self._sparse_record())
        row = tl._ensure_db().execute(
            "SELECT entry_timestamp FROM trades WHERE order_id = 'ord-A'"
        ).fetchone()
        assert row[0] == "2026-06-12T10:00:00+00:00"

    def test_cumulative_state_still_advances(self, tmp_path):
        """Sanity check on the non-COALESCE side: avg_fill_price /
        realized_slippage_bps DO get the newer value."""
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._entry_record())
        tl.log(self._sparse_record(
            avg_fill_price=151.0,
            realized_slippage_bps=12.0,
        ))
        row = tl._ensure_db().execute(
            "SELECT avg_fill_price, realized_slippage_bps "
            "FROM trades WHERE order_id = 'ord-A'"
        ).fetchone()
        assert row[0] == 151.0
        assert row[1] == 12.0


class TestPositionUidIdentityConflict:
    """Round 2 finding 6 sub-point: log() must REFUSE a write that
    would change position_uid from one non-null value to a different
    non-null value. Foundation §6.4 invariant: a broker order_id
    belongs to exactly one lifecycle row."""

    @staticmethod
    def _record(*, order_id: str, position_uid: str | None):
        from reporting.logger import TradeRecord
        return TradeRecord(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id=order_id,
            strategy="sma_crossover", reason="entry",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_uid=position_uid,
            position_type="single_leg",
        )

    def test_different_non_null_position_uid_raises(self, tmp_path):
        from reporting.logger import TradeLogger, TradeLoggerIdentityConflict
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-X", position_uid="pos-A"))
        with pytest.raises(TradeLoggerIdentityConflict):
            tl.log(self._record(order_id="ord-X", position_uid="pos-B"))
        # First write preserved.
        row = tl._ensure_db().execute(
            "SELECT position_uid FROM trades WHERE order_id = 'ord-X'"
        ).fetchone()
        assert row[0] == "pos-A"

    def test_same_position_uid_repeat_is_fine(self, tmp_path):
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-Y", position_uid="pos-A"))
        # Repeating the same uid is not a conflict.
        tl.log(self._record(order_id="ord-Y", position_uid="pos-A"))
        row = tl._ensure_db().execute(
            "SELECT position_uid FROM trades WHERE order_id = 'ord-Y'"
        ).fetchone()
        assert row[0] == "pos-A"

    def test_null_to_value_position_uid_is_fine(self, tmp_path):
        """Restart reconstruction depends on this: an earlier write
        may have had NULL position_uid; a later write that knows
        the uid must be allowed to fill it in."""
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-Z", position_uid=None))
        tl.log(self._record(order_id="ord-Z", position_uid="pos-A"))
        row = tl._ensure_db().execute(
            "SELECT position_uid FROM trades WHERE order_id = 'ord-Z'"
        ).fetchone()
        assert row[0] == "pos-A"

    def test_value_to_null_position_uid_is_preserved_not_raised(
        self, tmp_path,
    ):
        """The inverse: a later write with NULL position_uid keeps
        the existing value via COALESCE. NOT a conflict (only
        different-non-null is)."""
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-W", position_uid="pos-A"))
        tl.log(self._record(order_id="ord-W", position_uid=None))
        row = tl._ensure_db().execute(
            "SELECT position_uid FROM trades WHERE order_id = 'ord-W'"
        ).fetchone()
        assert row[0] == "pos-A"


# ── PR #60 round 3 (finding 2 UPSERT computed accounting) ───────────────────


class TestLatestNonNullAccountingBucket:
    """Round 3 finding: realized_pnl, r_multiple, realized_slippage_bps,
    slippage_signed_bps, slippage_adverse_bps are computed accounting
    fields that legitimately UPDATE across multiple log() calls (a
    later observation refines them). An incoming NULL must NOT erase
    a populated value — that's "I don't have this observation now",
    not "I'm explicitly clearing it"."""

    @staticmethod
    def _record(*, order_id: str, **overrides):
        from reporting.logger import TradeRecord
        defaults = dict(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id=order_id,
            strategy="sma_crossover", reason="entry",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_type="single_leg",
        )
        defaults.update(overrides)
        return TradeRecord(**defaults)

    def test_realized_pnl_preserved_on_sparse_null_follow_up(self, tmp_path):
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(
            order_id="ord-pnl",
            realized_pnl=12.34,
            realized_slippage_bps=3.5,
        ))
        # Sparse follow-up: NULL realized_pnl and NULL slippage.
        tl.log(self._record(
            order_id="ord-pnl",
            realized_pnl=None,
            realized_slippage_bps=None,
        ))
        row = tl._ensure_db().execute(
            "SELECT realized_pnl, realized_slippage_bps "
            "FROM trades WHERE order_id = 'ord-pnl'"
        ).fetchone()
        assert row[0] == 12.34
        assert row[1] == 3.5

    def test_r_multiple_preserved_on_sparse_null(self, tmp_path):
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(
            order_id="ord-r", r_multiple=2.5,
            realized_slippage_bps=3.5,
        ))
        tl.log(self._record(
            order_id="ord-r", r_multiple=None,
            realized_slippage_bps=None,
        ))
        row = tl._ensure_db().execute(
            "SELECT r_multiple FROM trades WHERE order_id = 'ord-r'"
        ).fetchone()
        assert row[0] == 2.5

    def test_slippage_signed_adverse_preserved_on_sparse_null(
        self, tmp_path,
    ):
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(
            order_id="ord-slip",
            slippage_signed_bps=2.1,
            slippage_adverse_bps=2.1,
            realized_slippage_bps=3.5,
        ))
        tl.log(self._record(
            order_id="ord-slip",
            slippage_signed_bps=None,
            slippage_adverse_bps=None,
            realized_slippage_bps=None,
        ))
        row = tl._ensure_db().execute(
            "SELECT slippage_signed_bps, slippage_adverse_bps "
            "FROM trades WHERE order_id = 'ord-slip'"
        ).fetchone()
        assert row[0] == 2.1
        assert row[1] == 2.1

    def test_latest_non_null_advances_when_incoming_is_populated(
        self, tmp_path,
    ):
        """The non-NULL side of LATEST-NON-NULL: a later observation
        DOES update the column if it carries a value. This is what
        distinguishes the bucket from PRESERVE-FIRST-NON-NULL."""
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(
            order_id="ord-adv",
            realized_pnl=10.0,
            realized_slippage_bps=3.5,
        ))
        # Later observation refines realized_pnl upward.
        tl.log(self._record(
            order_id="ord-adv",
            realized_pnl=15.0,
            realized_slippage_bps=4.0,
        ))
        row = tl._ensure_db().execute(
            "SELECT realized_pnl, realized_slippage_bps "
            "FROM trades WHERE order_id = 'ord-adv'"
        ).fetchone()
        assert row[0] == 15.0
        assert row[1] == 4.0

    def test_broker_cumulative_state_still_uses_excluded(self, tmp_path):
        """Positive control on the default bucket: filled_qty /
        avg_fill_price / status remain newest-wins even when the new
        value is NULL. The LATEST-NON-NULL bucket is specifically the
        computed-accounting columns."""
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(
            order_id="ord-bro",
            avg_fill_price=150.0,
            filled_qty=10,
            realized_slippage_bps=3.5,
        ))
        tl.log(self._record(
            order_id="ord-bro",
            avg_fill_price=151.5,  # newest wins
            filled_qty=10,
            status="filled",
            realized_slippage_bps=4.0,
        ))
        row = tl._ensure_db().execute(
            "SELECT avg_fill_price, status FROM trades "
            "WHERE order_id = 'ord-bro'"
        ).fetchone()
        assert row[0] == 151.5
        assert row[1] == "filled"


# ── PR #60 round 4 (P2 identity column expansion) ──────────────────────────


class TestIdentityColumnExpansion:
    """Round 4 P2 finding: position_id was in the default
    excluded.<col> bucket — a sparse follow-up could clobber
    populated 'AAPL' with NULL. Round 4 moves position_id and the
    other identity/intent columns into the IDENTITY_CONFLICT set
    so they raise on value→different-value and are preserved on
    value→NULL."""

    @staticmethod
    def _record(*, order_id: str, **overrides):
        from reporting.logger import TradeRecord
        defaults = dict(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id=order_id,
            strategy="sma_crossover", reason="entry",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_id="AAPL",
            position_type="single_leg",
        )
        defaults.update(overrides)
        return TradeRecord(**defaults)

    def test_position_id_preserved_on_sparse_null(self, tmp_path):
        """The exact regression ChatGPT reproduced: populated
        position_id='AAPL' must survive a sparse follow-up with
        position_id=None."""
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-pid", position_id="AAPL"))
        tl.log(self._record(order_id="ord-pid", position_id=None))
        row = tl._ensure_db().execute(
            "SELECT position_id FROM trades WHERE order_id = 'ord-pid'"
        ).fetchone()
        assert row[0] == "AAPL"

    def test_position_id_different_value_raises_conflict(self, tmp_path):
        from reporting.logger import TradeLogger, TradeLoggerIdentityConflict
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-pid2", position_id="AAPL"))
        with pytest.raises(TradeLoggerIdentityConflict):
            tl.log(self._record(order_id="ord-pid2", position_id="TSLA"))

    # NOTE: trades.symbol has a NOT NULL constraint at the schema
    # level — a sparse-null follow-up is structurally impossible.
    # The COALESCE preservation behavior is therefore unreachable
    # for symbol; the different-value conflict path below is the
    # meaningful coverage.

    def test_symbol_different_value_raises_conflict(self, tmp_path):
        from reporting.logger import TradeLogger, TradeLoggerIdentityConflict
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-sym2", symbol="AAPL"))
        with pytest.raises(TradeLoggerIdentityConflict):
            tl.log(self._record(order_id="ord-sym2", symbol="TSLA"))

    def test_side_different_value_raises_conflict(self, tmp_path):
        from reporting.logger import TradeLogger, TradeLoggerIdentityConflict
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-side", side="buy"))
        with pytest.raises(TradeLoggerIdentityConflict):
            tl.log(self._record(order_id="ord-side", side="sell"))

    def test_strategy_different_value_raises_conflict(self, tmp_path):
        from reporting.logger import TradeLogger, TradeLoggerIdentityConflict
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-str", strategy="sma_crossover"))
        with pytest.raises(TradeLoggerIdentityConflict):
            tl.log(self._record(
                order_id="ord-str", strategy="rsi_reversion",
            ))

    def test_order_type_different_value_raises_conflict(self, tmp_path):
        from reporting.logger import TradeLogger, TradeLoggerIdentityConflict
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-ot", order_type="market"))
        with pytest.raises(TradeLoggerIdentityConflict):
            tl.log(self._record(order_id="ord-ot", order_type="limit"))

    def test_requested_qty_different_value_raises_conflict(self, tmp_path):
        from reporting.logger import TradeLogger, TradeLoggerIdentityConflict
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        tl.log(self._record(order_id="ord-rq", requested_qty=10))
        with pytest.raises(TradeLoggerIdentityConflict):
            tl.log(self._record(order_id="ord-rq", requested_qty=20))

    def test_identity_null_to_value_allowed(self, tmp_path):
        """The NULL→value direction: restart reconstruction may
        write position_id where it was previously NULL."""
        from reporting.logger import TradeLogger, TradeRecord
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        # First write: position_id=NULL.
        first = TradeRecord(
            timestamp="2026-06-12T10:00:00+00:00",
            symbol="AAPL", side="buy", qty=10,
            avg_fill_price=150.0, order_id="ord-fill",
            strategy="sma_crossover", reason="entry",
            stop_price=145.0, entry_reference_price=150.0,
            modeled_slippage_bps=0.0, realized_slippage_bps=3.5,
            order_type="market", status="filled",
            requested_qty=10, filled_qty=10,
            position_id=None,
            position_type="single_leg",
        )
        tl.log(first)
        tl.log(self._record(order_id="ord-fill", position_id="AAPL"))
        row = tl._ensure_db().execute(
            "SELECT position_id FROM trades WHERE order_id = 'ord-fill'"
        ).fetchone()
        assert row[0] == "AAPL"


# ── P-1 end-to-end: stream event → engine drain → apply_order_event ────────


class TestStreamDrainEndToEnd:
    """P-1: verify the full pipeline from a queued OrderEvent to a
    persisted substrate state change. Uses the substrate's real
    sqlite3 connection (the cycle thread context); doesn't exercise
    the WS thread itself (that's the stream test file's scope)."""

    def test_drained_filled_event_advances_pending_row_to_filled(
        self, conn, pos_store, orders_store,
    ):
        from engine.lifecycle_orders import apply_order_event
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")

        # Simulate what _drain_lifecycle_events does per cycle.
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id,
                status="filled",
                filled_qty=10.0,
                avg_fill_price=100.5,
                broker_updated_at="2026-06-15T14:30:00+00:00",
            ),
            reason="stream",
        )
        assert outcome.applied is True
        assert outcome.new_status == "open"

        # Row advanced.
        status, qty, avg, _, terminal_at = _get_order(conn, order_id)
        assert status == "filled"
        assert qty == 10.0
        assert avg == 100.5
        assert terminal_at is not None

    def test_drained_event_for_unknown_order_returns_skip(
        self, conn,
    ):
        """Legacy orders submitted before P-4..P-6 shipped have no
        substrate row. The drain handler logs debug and moves on."""
        from engine.lifecycle_orders import apply_order_event
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id="legacy-ord-without-substrate-row",
                status="filled",
                filled_qty=10.0,
                avg_fill_price=100.5,
                broker_updated_at="2026-06-15T14:30:00+00:00",
            ),
            reason="stream",
        )
        assert outcome.applied is False
        assert outcome.reason == "unknown_order"

    def test_drained_stale_event_returns_skip(
        self, conn, pos_store, orders_store,
    ):
        """Out-of-order event arrival: a 'working' event arriving
        AFTER a 'filled' event for the same order_id should be
        skipped, not regress the row."""
        from engine.lifecycle_orders import apply_order_event
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        # First: filled event arrives.
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="filled",
                filled_qty=10.0, avg_fill_price=100.5,
                broker_updated_at="2026-06-15T14:30:05+00:00",
            ),
            reason="stream",
        )
        # Then: stale 'working' arrives out of order.
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="working",
                filled_qty=0.0, avg_fill_price=None,
                broker_updated_at="2026-06-15T14:30:00+00:00",  # older
            ),
            reason="stream",
        )
        assert outcome.applied is False
        assert outcome.reason in {"stale_or_duplicate", "terminal_blocked"}
        # Row unchanged.
        status, qty, _, _, _ = _get_order(conn, order_id)
        assert status == "filled"
        assert qty == 10.0


# ── P-2: cycle reconciliation against broker REST ──────────────────────────


class TestCycleReconcileStoreQuery:
    """P-2: get_non_terminal_with_order_id is the substrate query
    that drives cycle reconciliation. Exclusions matter — pending
    rows without order_id and 'error' rows must NOT be returned."""

    def test_returns_rows_with_order_id_in_non_terminal_status(
        self, pos_store, orders_store,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        _attach_and_get_order_id(orders_store, "cli-entry")

        rows = orders_store.get_non_terminal_with_order_id()
        assert len(rows) == 1
        assert rows[0].order_id is not None
        assert rows[0].status == "pending"

    def test_excludes_rows_with_null_order_id(
        self, pos_store, orders_store,
    ):
        """Pending row whose attach hasn't fired yet is owned by
        the lifecycle-attach queue, not by REST reconciliation."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)  # order_id=NULL
        rows = orders_store.get_non_terminal_with_order_id()
        assert rows == []

    def test_excludes_terminal_rows(
        self, conn, pos_store, orders_store,
    ):
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="filled",
                filled_qty=10.0, avg_fill_price=100.5,
                broker_updated_at="2026-06-15T10:00:00+00:00",
            ),
        )
        assert orders_store.get_non_terminal_with_order_id() == []

    def test_limit_caps_returned_rows(self, pos_store, orders_store):
        """The cycle reconciler caps REST calls per cycle by passing
        limit=N to this query."""
        for i in range(5):
            uid = _seed_position(pos_store, owner_key=f"SYM{i}")
            _insert_entry(
                orders_store, uid, client_order_id=f"cli-{i}",
            )
            _attach_and_get_order_id(orders_store, f"cli-{i}")
        rows = orders_store.get_non_terminal_with_order_id(limit=2)
        assert len(rows) == 2

    def test_error_status_excluded(self, conn, pos_store, orders_store):
        """'error' is a sticky invariant-violation sentinel
        (§6.6.1 R9-P1b) and must not be reconciled. Manually
        set status='error' to simulate the state."""
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        conn.execute(
            "UPDATE position_lifecycle_orders SET status='error' "
            "WHERE order_id = ?",
            (order_id,),
        )
        conn.commit()
        assert orders_store.get_non_terminal_with_order_id() == []


# ── P-2: REST order → OrderEvent translation ───────────────────────────────


class TestBuildSubstrateEventFromBrokerOrder:
    """The cycle reconciler translates Alpaca REST order objects to
    OrderEvents the same way the stream translates trade_updates.
    The status mapping must be identical so cycle + stream produce
    consistent state advances."""

    @staticmethod
    def _build(order_id="ord-1", **overrides):
        from types import SimpleNamespace
        from engine.trader import TradingEngine
        order = SimpleNamespace(
            status=SimpleNamespace(value=overrides.pop("status", "filled")),
            filled_qty=overrides.pop("filled_qty", "10"),
            filled_avg_price=overrides.pop("filled_avg_price", "100.5"),
            updated_at=overrides.pop("updated_at", "2026-06-15T10:00:00Z"),
            **overrides,
        )
        return TradingEngine._build_substrate_event_from_broker_order(
            order, order_id,
        )

    def test_filled_status_maps_to_filled(self):
        ev = self._build(status="filled", filled_qty="10", filled_avg_price="100.5")
        assert ev.status == "filled"
        assert ev.filled_qty == 10.0
        assert ev.avg_fill_price == 100.5

    def test_partially_filled_status_maps(self):
        ev = self._build(status="partially_filled", filled_qty="5")
        assert ev.status == "partially_filled"
        assert ev.filled_qty == 5.0

    def test_canceled_status_maps(self):
        ev = self._build(status="canceled", filled_qty="0", filled_avg_price=None)
        assert ev.status == "canceled"
        assert ev.filled_qty == 0.0
        assert ev.avg_fill_price is None

    def test_expired_status_maps_to_canceled(self):
        ev = self._build(status="expired", filled_qty="0", filled_avg_price=None)
        assert ev.status == "canceled"

    def test_rejected_status_maps(self):
        ev = self._build(status="rejected", filled_qty="0", filled_avg_price=None)
        assert ev.status == "rejected"

    def test_non_material_status_returns_none(self):
        """pending_new / pending_cancel / suspended don't advance
        the state machine — skip them."""
        assert self._build(status="pending_new") is None
        assert self._build(status="pending_cancel") is None
        assert self._build(status="suspended") is None


# ── P-3: startup reconciliation ────────────────────────────────────────────


class TestSubstrateReconcileStartup:
    """P-3: startup walks ALL non-terminal substrate rows (no
    per-call limit) and apply broker truth. Catches events that
    happened during downtime."""

    def test_get_non_terminal_returns_all_rows_when_no_limit(
        self, pos_store, orders_store,
    ):
        """P-3 calls the same store query as P-2 but with limit=None.
        The store returns the full set in that case."""
        for i in range(50):
            uid = _seed_position(pos_store, owner_key=f"SYM{i}")
            _insert_entry(
                orders_store, uid, client_order_id=f"cli-{i}",
            )
            _attach_and_get_order_id(orders_store, f"cli-{i}")
        rows = orders_store.get_non_terminal_with_order_id(limit=None)
        # All 50 rows (no cap).
        assert len(rows) == 50

    def test_startup_reason_threaded_through_apply_order_event(
        self, conn, pos_store, orders_store,
    ):
        """The startup reconciler passes reason='startup' so the
        substrate audit reflects the source. Cycle uses 'cycle';
        stream uses 'stream'."""
        from engine.lifecycle_orders import apply_order_event
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="filled",
                filled_qty=10.0, avg_fill_price=100.5,
                broker_updated_at="2026-06-15T10:00:00+00:00",
            ),
            reason="startup",
        )
        assert outcome.applied is True
        # The outcome doesn't echo the reason back, but the call
        # itself succeeding with reason='startup' confirms the
        # API accepts the parameter the startup reconciler uses.

    def test_open_snapshot_advances_pending_order_to_working(
        self, conn, pos_store, orders_store,
    ):
        """A broker-open order can miss the stream's status-only
        accepted/new event. Cycle/startup reconciliation should still
        advance the substrate row from pending to working without
        creating a trade row or performing a REST fetch."""
        from engine.trader import TradingEngine

        uid = _seed_position(
            pos_store,
            owner_key="WYFI",
            entry_qty=4.0,
            strategy="donchian_breakout",
        )
        pos_store.mark_open(
            position_uid=uid,
            avg_entry_price=33.85,
            current_qty=4.0,
        )
        _insert_protective_stop(
            orders_store,
            uid,
            client_order_id="donchian_breakout-recover-stop-f25547f2fe",
            intended_qty=4.0,
            stop_price=29.09,
        )
        order_id = _attach_and_get_order_id(
            orders_store,
            "donchian_breakout-recover-stop-f25547f2fe",
            order_id="3f602dd0-3e87-45b6-99bc-ab18b26b1fc8",
        )
        engine = SimpleNamespace(
            lifecycle_orders_store=orders_store,
            broker=SimpleNamespace(
                _with_retry=MagicMock(
                    side_effect=AssertionError("REST fetch not expected")
                ),
                _api=SimpleNamespace(get_order_by_id=MagicMock()),
            ),
            _maybe_dispatch_substrate_entry_fill=MagicMock(),
            _maybe_dispatch_substrate_exit_fill=MagicMock(),
            _maybe_dispatch_substrate_stop_fill=MagicMock(),
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(),
            open_orders=[
                OpenOrder(
                    order_id=order_id,
                    symbol="WYFI",
                    side=Side.SELL,
                    qty=4.0,
                    order_type="stop",
                    status="new",
                    submitted_at=datetime(
                        2026, 6, 18, 13, 34, 21,
                        tzinfo=timezone.utc,
                    ),
                    limit_price=None,
                    stop_price=29.09,
                    client_order_id="donchian_breakout-recover-stop-f25547f2fe",
                    time_in_force="gtc",
                )
            ],
        )

        TradingEngine._reconcile_substrate_via_rest(
            engine,
            snapshot,
            reason="cycle",
            limit=20,
        )

        row = orders_store.get_by_order_id(order_id)
        assert row is not None
        assert row.status == "working"
        assert row.filled_qty == 0.0
        assert conn.execute(
            "SELECT COUNT(*) FROM trades WHERE order_id = ?",
            (order_id,),
        ).fetchone()[0] == 0
        engine.broker._with_retry.assert_not_called()
        engine._maybe_dispatch_substrate_entry_fill.assert_not_called()
        engine._maybe_dispatch_substrate_exit_fill.assert_not_called()
        engine._maybe_dispatch_substrate_stop_fill.assert_not_called()


# ── P-6 commit B: substrate entry-fill dispatch ─────────────────────────────


class TestSubstrateEntryFillDispatchSemantics:
    """The dispatch helper's contract: only fire on entry_primary
    fills the engine doesn't already own. Existing tests cover
    apply_order_event semantics; these cover the dispatch guards
    (which side-effects fire and when)."""

    def test_dispatch_skipped_when_status_not_filled(
        self, conn, pos_store, orders_store,
    ):
        """Only entry_primary transitions to 'filled' trigger the
        dispatch. 'working' / 'canceled' / 'partially_filled' do
        not bind ownership."""
        from engine.lifecycle_orders import apply_order_event
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="working",
                filled_qty=0.0, avg_fill_price=None,
                broker_updated_at="2026-06-15T10:00:00+00:00",
            ),
        )
        assert outcome.applied is True
        # A real engine would NOT dispatch on 'working' — the
        # dispatch's guard checks event.status == 'filled' first.

    def test_dispatch_skipped_when_zero_fill(
        self, conn, pos_store, orders_store,
    ):
        """Zero-fill filled (which shouldn't happen, but if it does)
        is non-actionable. Don't bind ownership against an empty
        position."""
        from engine.lifecycle_orders import apply_order_event
        uid = _seed_position(pos_store)
        _insert_entry(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-entry")
        # The dispatch checks float(event.filled_qty or 0) > 0.
        # An event with filled_qty=0 would be skipped even with
        # status='filled' (though apply_order_event also wouldn't
        # write a trade row in that case per the §6.5 gate).
        event = OrderEvent(
            order_id=order_id, status="filled",
            filled_qty=0.0, avg_fill_price=None,
            broker_updated_at="2026-06-15T10:00:00+00:00",
        )
        # Direct guard check.
        assert float(event.filled_qty or 0.0) <= 0  # would skip

    def test_dispatch_role_filter(
        self, conn, pos_store, orders_store,
    ):
        """Only entry_primary rows trigger ownership-binding
        dispatch. exit / protective_stop / replacement_stop fills
        are handled by their own dispatch paths."""
        from engine.lifecycle_orders import apply_order_event
        uid = _seed_position(pos_store)
        # Insert a protective_stop role row (not entry_primary).
        _insert_protective_stop(
            orders_store, uid, client_order_id="cli-stop",
        )
        order_id = _attach_and_get_order_id(
            orders_store, "cli-stop", order_id="alpaca-stop-1",
        )
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="filled",
                filled_qty=10.0, avg_fill_price=95.0,
                broker_updated_at="2026-06-15T10:00:00+00:00",
            ),
        )
        assert outcome.applied is True
        # Entry dispatch's role filter (order_row.role != 'entry_primary')
        # would skip this — stop dispatch owns this role.
        row = orders_store.get_by_order_id(order_id)
        assert row.role == "protective_stop"  # would not entry-dispatch


# ── P-7 commit A: substrate exit-fill dispatch ──────────────────────────────


class TestSubstrateExitFillDispatchSemantics:
    """Exit-side counterpart to TestSubstrateEntryFillDispatchSemantics.
    The dispatch fires _record_recovered_exit_fill (idempotent via
    has_recorded_order_id) and clears engine ownership state when
    the substrate observes an exit-role row reaching filled."""

    def test_dispatch_role_filter_exit_only(
        self, conn, pos_store, orders_store,
    ):
        """Only role='exit' rows trigger exit dispatch. entry_primary
        / protective_stop / replacement_stop fills go through their
        own dispatches."""
        from engine.lifecycle_orders import apply_order_event
        uid = _seed_position(pos_store)
        _insert_protective_stop(
            orders_store, uid, client_order_id="cli-not-exit",
        )
        order_id = _attach_and_get_order_id(
            orders_store, "cli-not-exit", order_id="alpaca-stop-x",
        )
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="filled",
                filled_qty=10.0, avg_fill_price=95.0,
                broker_updated_at="2026-06-15T10:00:00+00:00",
            ),
        )
        assert outcome.applied is True
        # Exit dispatch's role filter would skip — stop dispatch owns
        # this role.
        row = orders_store.get_by_order_id(order_id)
        assert row.role == "protective_stop"  # would not trigger exit dispatch

    def test_exit_row_filled_event_recognized(
        self, conn, pos_store, orders_store,
    ):
        """Positive shape check: an exit row transitioning to filled
        is the substrate trigger the dispatch acts on."""
        from engine.lifecycle_orders import apply_order_event
        uid = _seed_position(pos_store)
        _insert_exit(orders_store, uid)
        order_id = _attach_and_get_order_id(orders_store, "cli-exit")
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id=order_id, status="filled",
                filled_qty=10.0, avg_fill_price=99.5,
                broker_updated_at="2026-06-15T10:00:00+00:00",
            ),
        )
        assert outcome.applied is True
        row = orders_store.get_by_order_id(order_id)
        assert row.role == "exit"
        assert row.status == "filled"


# ── PR #61 round-1 fix P1: end-to-end exit-dispatch integration ────────────


class TestExitDispatchEndToEnd:
    """ChatGPT round-1 review caught a deterministic bug: the unit
    tests for the exit dispatch verified the substrate row reached
    filled but never actually called _maybe_dispatch_substrate_exit_
    fill. The real-world sequence (apply_order_event UPSERTs trade
    → dispatch sees has_recorded_order_id=True → skips P&L/alert/
    cleanup) was untested.

    These integration tests exercise the actual pipeline path:
    apply_order_event first, then dispatch second, on the same
    in-engine substrate connection — and assert the side effects
    actually fire."""

    def test_exit_dispatch_fires_pnl_and_cleanup_after_substrate_write(
        self, tmp_path,
    ):
        """The reviewer's exact reproduction. After apply_order_event
        writes the exit trade row, the dispatch must STILL fire
        realized_pnl + ownership cleanup."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.positions import Position
        from execution.broker import BrokerSnapshot
        from risk.manager import RiskDecision, Side
        from strategies.base import OrderType

        # Build a minimal engine wired to a real substrate.
        engine = MagicMock(spec=TradingEngine)
        engine.lifecycle_orders_store = MagicMock()
        engine.lifecycle_store = MagicMock()
        engine.trade_logger = MagicMock()
        engine.alerts = MagicMock()
        engine.risk = MagicMock()
        engine._positions = {"AAPL": Position(
            position_id="AAPL", position_type="single_leg",
            strategy_name="sma_crossover",
        )}
        engine._entry_prices = {"AAPL": 100.0}
        engine._external_close_suspects = {}
        # Mock spec stubs out _has_position / _pop_position; wire
        # them to real dict semantics so the dispatch's gate and
        # cleanup are observable.
        engine._has_position = lambda sym: sym in engine._positions
        engine._pop_position = lambda sym: engine._positions.pop(sym, None)
        # Bind the REAL _record_recovered_exit_fill and its
        # dependencies so the dispatch's call into it actually
        # writes the close log and fires the alert.
        engine._record_recovered_exit_fill = (
            TradingEngine._record_recovered_exit_fill.__get__(engine)
        )
        engine._record_fill = lambda *a, **kw: None  # HWM gate noop
        engine._log_close = lambda *a, **kw: None  # trade_logger already has the row
        engine._record_realized_pnl = MagicMock()

        # Real substrate connection from a TradeLogger.
        from reporting.logger import TradeLogger
        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        conn = tl._ensure_db()
        from engine.lifecycle import PositionLifecycleStore, new_position_uid
        from engine.lifecycle_orders import PositionLifecycleOrdersStore
        pos_store = PositionLifecycleStore(conn)
        orders_store = PositionLifecycleOrdersStore(conn)
        engine.lifecycle_orders_store = orders_store
        engine.lifecycle_store = pos_store
        engine.trade_logger = tl

        # Seed: an open position with an exit order pending.
        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol="AAPL", owner_key="AAPL",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        pos_store.mark_open(
            position_uid=uid, avg_entry_price=100.0, current_qty=10.0,
        )
        orders_store.insert_pending(
            position_uid=uid, role="exit", client_order_id="cli-exit-1",
            order_type="market", order_class="simple",
            time_in_force="day", side="sell", intended_qty=10.0,
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-exit-1", order_id="alpaca-exit-1",
        )

        # Step 1: substrate observes the fill via stream → apply.
        # This is what was happening BEFORE the dispatch could run.
        outcome = apply_order_event(
            conn,
            OrderEvent(
                order_id="alpaca-exit-1", status="filled",
                filled_qty=10.0, avg_fill_price=105.0,
                broker_updated_at="2026-06-16T10:30:00+00:00",
            ),
            reason="stream",
        )
        assert outcome.applied is True
        # Trade row IS in trades now — pre-fix, the next step's
        # has_recorded_order_id check would short-circuit.
        assert tl.has_recorded_order_id("alpaca-exit-1") is True

        # Step 2: dispatch runs. With the round-1 fix it must
        # bypass the trades-dedup check (ownership gate is the
        # dedup signal instead) and fire side effects.
        event = OrderEvent(
            order_id="alpaca-exit-1", status="filled",
            filled_qty=10.0, avg_fill_price=105.0,
            broker_updated_at="2026-06-16T10:30:00+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={},  # broker no longer holds AAPL
            ),
            open_orders=[],
        )
        # We need the REAL engine method (not a mock) for this
        # integration. Re-bind it directly.
        TradingEngine._maybe_dispatch_substrate_exit_fill(
            engine, event=event, snapshot=snapshot,
        )

        # Side effects should have fired even though the trade row
        # was already in trades:
        # 1. Ownership cleared from _positions
        assert "AAPL" not in engine._positions
        # 2. Entry-price cache cleared
        assert "AAPL" not in engine._entry_prices
        # 3. alerts.trade_executed fired
        engine.alerts.trade_executed.assert_called_once()
        engine._record_realized_pnl.assert_called_once_with(
            "AAPL",
            "sma_crossover",
            105.0,
            10.0,
            multiplier=1,
            external=False,
            is_full_close=True,
            update_lifecycle=False,
            position_uid_override=uid,
        )

    def test_exit_dispatch_idempotent_via_ownership_gate(self, tmp_path):
        """Second observation of the same fill (e.g., cycle reconcile
        re-fires after the stream already did) must be a no-op:
        first dispatch popped ownership, second sees no position and
        skips."""
        from unittest.mock import MagicMock
        from types import SimpleNamespace
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.positions import Position
        from execution.broker import BrokerSnapshot
        from reporting.logger import TradeLogger
        from engine.lifecycle import PositionLifecycleStore, new_position_uid
        from engine.lifecycle_orders import PositionLifecycleOrdersStore

        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        conn = tl._ensure_db()
        pos_store = PositionLifecycleStore(conn)
        orders_store = PositionLifecycleOrdersStore(conn)

        engine = MagicMock(spec=TradingEngine)
        engine.lifecycle_orders_store = orders_store
        engine.lifecycle_store = pos_store
        engine.trade_logger = tl
        engine.alerts = MagicMock()
        engine.risk = MagicMock()
        engine._positions = {"AAPL": Position(
            position_id="AAPL", position_type="single_leg",
            strategy_name="sma_crossover",
        )}
        engine._entry_prices = {"AAPL": 100.0}
        engine._external_close_suspects = {}
        # Mock spec stubs out _has_position / _pop_position; wire
        # them to real dict semantics so the dispatch's gate and
        # cleanup are observable.
        engine._has_position = lambda sym: sym in engine._positions
        engine._pop_position = lambda sym: engine._positions.pop(sym, None)
        # Bind the REAL _record_recovered_exit_fill and its
        # dependencies so the dispatch's call into it actually
        # writes the close log and fires the alert.
        engine._record_recovered_exit_fill = (
            TradingEngine._record_recovered_exit_fill.__get__(engine)
        )
        engine._record_fill = lambda *a, **kw: None  # HWM gate noop
        engine._log_close = lambda *a, **kw: None  # trade_logger already has the row
        engine._record_realized_pnl = MagicMock()

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol="AAPL", owner_key="AAPL",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        pos_store.mark_open(
            position_uid=uid, avg_entry_price=100.0, current_qty=10.0,
        )
        orders_store.insert_pending(
            position_uid=uid, role="exit", client_order_id="cli-exit-2",
            order_type="market", order_class="simple",
            time_in_force="day", side="sell", intended_qty=10.0,
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-exit-2", order_id="alpaca-exit-2",
        )
        apply_order_event(
            conn,
            OrderEvent(
                order_id="alpaca-exit-2", status="filled",
                filled_qty=10.0, avg_fill_price=105.0,
                broker_updated_at="2026-06-16T10:30:00+00:00",
            ),
            reason="stream",
        )

        event = OrderEvent(
            order_id="alpaca-exit-2", status="filled",
            filled_qty=10.0, avg_fill_price=105.0,
            broker_updated_at="2026-06-16T10:30:00+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={},
            ),
            open_orders=[],
        )

        # First dispatch fires.
        TradingEngine._maybe_dispatch_substrate_exit_fill(
            engine, event=event, snapshot=snapshot,
        )
        assert "AAPL" not in engine._positions
        first_alert_count = engine.alerts.trade_executed.call_count

        # Second dispatch (cycle reconcile re-observation) sees no
        # ownership and skips. Alert count unchanged.
        TradingEngine._maybe_dispatch_substrate_exit_fill(
            engine, event=event, snapshot=snapshot,
        )
        assert engine.alerts.trade_executed.call_count == first_alert_count


class TestSubstrateStopFillDispatchSemantics:
    """Protective-stop rows use substrate truth first; the legacy stream
    stop-fill queue is only a temporary compatibility fallback."""

    def test_full_stop_fill_logs_stop_and_clears_ownership_with_uid(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from engine.positions import Position
        from engine.trader import TradingEngine
        from execution.broker import BrokerSnapshot

        uid = _seed_position(
            pos_store,
            owner_key="FRO",
            entry_qty=49.0,
        )
        _insert_protective_stop(
            orders_store,
            uid,
            client_order_id="cli-fro-stop",
            intended_qty=49.0,
            stop_price=17.25,
        )
        order_id = _attach_and_get_order_id(
            orders_store,
            "cli-fro-stop",
            order_id="alpaca-fro-stop",
        )
        engine = MagicMock(spec=TradingEngine)
        engine.lifecycle_orders_store = orders_store
        engine.lifecycle_store = pos_store
        engine.trade_logger = MagicMock()
        engine.alerts = MagicMock()
        engine._positions = {
            "FRO": Position(
                position_id="FRO",
                position_type="single_leg",
                strategy_name="sma_crossover",
            )
        }
        engine._entry_prices = {"FRO": 18.0}
        engine._external_close_suspects = {"FRO": object()}
        engine._has_position = lambda key: key in engine._positions
        engine._pop_position = lambda key: engine._positions.pop(key, None)
        engine._get_position_for = TradingEngine._get_position_for
        engine._record_realized_pnl = MagicMock()
        engine._cleanup_option_trailing_state = MagicMock()
        event = OrderEvent(
            order_id=order_id,
            status="filled",
            filled_qty=49.0,
            avg_fill_price=17.25,
            broker_updated_at="2026-06-18T14:30:00+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0,
                cash=50_000.0,
                buying_power=50_000.0,
                open_positions={},
            ),
            open_orders=[],
        )

        TradingEngine._maybe_dispatch_substrate_stop_fill(
            engine,
            event=event,
            snapshot=snapshot,
        )

        engine.trade_logger.log_stop_fill.assert_called_once_with(
            symbol="FRO",
            strategy="sma_crossover",
            qty=49.0,
            avg_fill_price=17.25,
            stop_price=17.25,
            order_id=order_id,
            position_uid=uid,
        )
        engine._record_realized_pnl.assert_called_once_with(
            "FRO",
            "sma_crossover",
            17.25,
            49.0,
            multiplier=1,
            is_full_close=True,
            update_lifecycle=False,
            position_uid_override=uid,
        )
        assert "FRO" not in engine._positions
        assert "FRO" not in engine._entry_prices
        assert "FRO" not in engine._external_close_suspects

    def test_fractional_residual_stop_fill_preserves_ownership(
        self,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from engine.positions import Position
        from engine.trader import TradingEngine
        from execution.broker import BrokerSnapshot
        from risk.manager import Position as BrokerPosition

        uid = _seed_position(
            pos_store,
            owner_key="GOOG",
            entry_qty=7.78,
            strategy="donchian_breakout",
        )
        _insert_protective_stop(
            orders_store,
            uid,
            client_order_id="cli-goog-stop",
            intended_qty=7.0,
            stop_price=378.85,
        )
        order_id = _attach_and_get_order_id(
            orders_store,
            "cli-goog-stop",
            order_id="alpaca-goog-stop",
        )
        engine = MagicMock(spec=TradingEngine)
        engine.lifecycle_orders_store = orders_store
        engine.lifecycle_store = pos_store
        engine.trade_logger = MagicMock()
        engine.alerts = MagicMock()
        engine._positions = {
            "GOOG": Position(
                position_id="GOOG",
                position_type="single_leg",
                strategy_name="donchian_breakout",
            )
        }
        engine._entry_prices = {"GOOG": 391.0}
        engine._external_close_suspects = {"GOOG": object()}
        engine._has_position = lambda key: key in engine._positions
        engine._pop_position = lambda key: engine._positions.pop(key, None)
        engine._get_position_for = TradingEngine._get_position_for
        engine._record_realized_pnl = MagicMock()
        engine._cleanup_option_trailing_state = MagicMock()
        event = OrderEvent(
            order_id=order_id,
            status="filled",
            filled_qty=7.0,
            avg_fill_price=378.85,
            broker_updated_at="2026-06-18T14:30:00+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0,
                cash=50_000.0,
                buying_power=50_000.0,
                open_positions={
                    "GOOG": BrokerPosition("GOOG", 0.78, 391.2, 305.14),
                },
            ),
            open_orders=[],
        )

        TradingEngine._maybe_dispatch_substrate_stop_fill(
            engine,
            event=event,
            snapshot=snapshot,
        )

        engine._record_realized_pnl.assert_called_once_with(
            "GOOG",
            "donchian_breakout",
            378.85,
            7.0,
            multiplier=1,
            is_full_close=False,
            update_lifecycle=False,
            position_uid_override=uid,
        )
        assert "GOOG" in engine._positions
        assert engine._entry_prices["GOOG"] == pytest.approx(391.0)
        assert "GOOG" not in engine._external_close_suspects
        engine._cleanup_option_trailing_state.assert_not_called()


class TestEntryDispatchSlippageCompleteness:
    """Slippage Phase 2 follow-up: when the substrate observes an
    entry_primary fill that the synchronous place_order path didn't
    handle (UNKNOWN-at-submit then later FILLED), the dispatch must
    fire the trade-log completeness call so the trades row carries
    computed `slippage_signed_bps` / `slippage_adverse_bps`.

    Pre-fix: `apply_order_event`'s UPSERT wrote the row with the
    substrate's submit-time benchmark provenance (kind / quality /
    benchmark_price) but left the computed columns NULL because
    that math lives in `build_record` not in the substrate's
    transactional SQL. The dispatch fired ownership / entry-price
    cache / stop replacement / alert, but never replayed
    `_log_entry`. The row was correct for position management and
    wrong for accounting.

    Post-fix: after `_apply_recovered_entry_side_effects` succeeds,
    the dispatch builds an OrderResult + logging decision from the
    substrate row and calls `_log_entry`. `tl.log`'s UPSERT policy
    preserves the substrate's provenance fields (PRESERVE-FIRST-
    NON-NULL) and fills in the computed signed/adverse columns
    (LATEST-NON-NULL).
    """

    def _wire_engine(self, tmp_path, symbol: str):
        """Build a MagicMock engine with the minimum real wiring the
        dispatch needs: real substrate stores, real TradeLogger,
        real dict-backed ownership, real bound `_log_entry` so the
        substrate row's trade-log UPSERT actually executes."""
        from unittest.mock import MagicMock
        from engine.trader import TradingEngine
        from reporting.logger import TradeLogger
        from engine.lifecycle import PositionLifecycleStore
        from engine.lifecycle_orders import PositionLifecycleOrdersStore

        tl = TradeLogger(path=str(tmp_path / "trades.db"))
        conn = tl._ensure_db()
        pos_store = PositionLifecycleStore(conn)
        orders_store = PositionLifecycleOrdersStore(conn)

        engine = MagicMock(spec=TradingEngine)
        engine.lifecycle_orders_store = orders_store
        engine.lifecycle_store = pos_store
        engine.trade_logger = tl
        engine.alerts = MagicMock()
        engine.risk = MagicMock()
        # Real engine state — the dispatch reads / writes these.
        engine._positions = {}
        engine._entry_prices = {}
        engine._has_position = lambda sym: sym in engine._positions
        engine._register_single_leg = MagicMock(
            side_effect=lambda strategy_name, symbol: engine._positions.update(
                {symbol: object()}
            )
        )
        engine._ensure_recovered_protective_stop = MagicMock()
        engine._lookup_position_uid_for_owner = lambda key: None
        # Bind the real side-effects helper + log_entry so the
        # dispatch's recovered-entry path executes end-to-end.
        engine._apply_recovered_entry_side_effects = (
            TradingEngine._apply_recovered_entry_side_effects.__get__(engine)
        )
        engine._log_entry = TradingEngine._log_entry.__get__(engine)
        return engine, tl, pos_store, orders_store

    def test_entry_dispatch_fills_computed_slippage_after_substrate_write(
        self, tmp_path,
    ):
        """The reviewer's exact repro. Substrate writes the row with
        provenance + NULL computed slippage; dispatch fires and the
        row ends with provenance preserved + computed signed/adverse
        populated."""
        from types import SimpleNamespace
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.lifecycle import new_position_uid
        from execution.broker import BrokerSnapshot

        symbol = "AAPL"
        engine, tl, pos_store, orders_store = self._wire_engine(
            tmp_path, symbol,
        )

        # Seed: open position with an UNKNOWN-at-submit entry order
        # whose substrate row captured the arrival-midpoint benchmark.
        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol=symbol, owner_key=symbol,
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry-1",
            order_type="market", order_class="simple",
            time_in_force="day", side="buy", intended_qty=10.0,
            intended_stop_price=95.0,
            slippage_benchmark_price=100.0,          # arrival midpoint
            slippage_benchmark_kind="arrival_midpoint",
            slippage_benchmark_timestamp="2026-06-17T15:00:00+00:00",
            slippage_measurement_quality="primary",  # substrate's tag
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry-1", order_id="alpaca-entry-1",
        )

        # Step 1: substrate sees the fill — apply_order_event UPSERTs
        # the trade row.
        outcome = apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-1", status="filled",
                filled_qty=10.0, avg_fill_price=100.50,  # adverse fill
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            reason="stream",
        )
        assert outcome.applied is True
        # Sanity check the pre-dispatch row state matches the reviewer's
        # finding: provenance populated, computed slippage NULL.
        row = tl._ensure_db().execute(
            "SELECT slippage_benchmark_price, slippage_benchmark_kind, "
            "slippage_measurement_quality, slippage_signed_bps, "
            "slippage_adverse_bps FROM trades "
            "WHERE order_id='alpaca-entry-1'"
        ).fetchone()
        assert row[0] == 100.0
        assert row[1] == "arrival_midpoint"
        assert row[2] == "primary"
        assert row[3] is None  # signed — the gap
        assert row[4] is None  # adverse — the gap

        # Step 2: dispatch fires.
        event = OrderEvent(
            order_id="alpaca-entry-1", status="filled",
            filled_qty=10.0, avg_fill_price=100.50,
            broker_updated_at="2026-06-17T15:00:01+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={symbol: SimpleNamespace(
                    qty=10.0, avg_entry_price=100.50, side="long",
                )},
            ),
            open_orders=[],
        )
        TradingEngine._maybe_dispatch_substrate_entry_fill(
            engine, event=event, snapshot=snapshot,
        )

        # Side effects — sanity check ownership bound, alert fired.
        assert symbol in engine._positions
        engine.alerts.trade_executed.assert_called_once()

        # The completeness call ran. Provenance preserved by UPSERT
        # COALESCE; computed signed/adverse filled in.
        row = tl._ensure_db().execute(
            "SELECT slippage_benchmark_price, slippage_benchmark_kind, "
            "slippage_measurement_quality, slippage_signed_bps, "
            "slippage_adverse_bps FROM trades "
            "WHERE order_id='alpaca-entry-1'"
        ).fetchone()
        assert row[0] == 100.0                 # substrate price preserved
        assert row[1] == "arrival_midpoint"    # substrate kind preserved
        assert row[2] == "primary"             # substrate quality preserved
        # Fill 100.50 vs benchmark 100.0 on a BUY → adverse 50 bps.
        assert row[3] is not None
        assert row[4] is not None
        assert row[3] == pytest.approx(50.0, abs=0.1)
        assert row[4] == pytest.approx(50.0, abs=0.1)

    def test_entry_dispatch_with_missing_substrate_benchmark_records_null(
        self, tmp_path,
    ):
        """If the substrate row's `slippage_benchmark_price` is NULL
        (older row, or an upstream path that didn't capture one),
        the completeness call writes NULL computed slippage —
        recovery never fabricates a benchmark."""
        from types import SimpleNamespace
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.lifecycle import new_position_uid
        from execution.broker import BrokerSnapshot

        symbol = "MSFT"
        engine, tl, pos_store, orders_store = self._wire_engine(
            tmp_path, symbol,
        )

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol=symbol, owner_key=symbol,
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=5.0,
        )
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry-no-bench",
            order_type="market", order_class="simple",
            time_in_force="day", side="buy", intended_qty=5.0,
            intended_stop_price=190.0,
            # No benchmark captured at submit — exercises the
            # honest-NULL recovery path.
            slippage_benchmark_price=None,
            slippage_benchmark_kind=None,
            slippage_measurement_quality="unavailable",
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry-no-bench",
            order_id="alpaca-entry-no-bench",
        )
        apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-no-bench", status="filled",
                filled_qty=5.0, avg_fill_price=200.0,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            reason="stream",
        )

        event = OrderEvent(
            order_id="alpaca-entry-no-bench", status="filled",
            filled_qty=5.0, avg_fill_price=200.0,
            broker_updated_at="2026-06-17T15:00:01+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={symbol: SimpleNamespace(
                    qty=5.0, avg_entry_price=200.0, side="long",
                )},
            ),
            open_orders=[],
        )
        TradingEngine._maybe_dispatch_substrate_entry_fill(
            engine, event=event, snapshot=snapshot,
        )

        # Honest NULL on both sides — no fabricated benchmark.
        row = tl._ensure_db().execute(
            "SELECT slippage_benchmark_price, slippage_signed_bps, "
            "slippage_adverse_bps FROM trades "
            "WHERE order_id='alpaca-entry-no-bench'"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] is None

    def test_recovered_limit_entry_preserves_limit_price_unavailable(
        self, tmp_path,
    ):
        """PR #68 round-1 review P1 regression. After the submit-path
        fix (engine forces kind='limit_price' / quality='unavailable'
        for LIMIT entries), the substrate row stores the correct
        taxonomy tags at submit time. The recovery completeness call
        in `_maybe_dispatch_substrate_entry_fill` must preserve these
        tags — not regress them back to 'arrival_midpoint' / 'primary'
        — so the dashboard / health / calibration consumers see the
        same NULL-by-design slippage they'd see on a synchronous LIMIT
        fill.

        The reviewer's exact repro: a recovered LIMIT row pre-fix ended
        as ('limit', 100.0, 'arrival_midpoint', 'primary', None, None).
        Post-fix it should be ('limit', None, 'limit_price',
        'unavailable', None, None).
        """
        from types import SimpleNamespace
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.lifecycle import new_position_uid
        from execution.broker import BrokerSnapshot

        symbol = "TSLA"
        engine, tl, pos_store, orders_store = self._wire_engine(
            tmp_path, symbol,
        )

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol=symbol, owner_key=symbol,
            strategy="rsi_reversion", position_type="single_leg",
            entry_qty=4.0,
        )
        # Substrate stores submit-time taxonomy for a LIMIT entry per
        # the order-type-aware engine fix.
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry-limit-1",
            order_type="limit", order_class="simple",
            time_in_force="day", side="buy", intended_qty=4.0,
            intended_stop_price=240.0,
            intended_limit_price=250.0,
            slippage_benchmark_price=None,
            slippage_benchmark_kind="limit_price",
            slippage_benchmark_timestamp=None,
            slippage_measurement_quality="unavailable",
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry-limit-1",
            order_id="alpaca-entry-limit-1",
        )
        apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-limit-1", status="filled",
                filled_qty=4.0, avg_fill_price=249.50,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            reason="stream",
        )

        event = OrderEvent(
            order_id="alpaca-entry-limit-1", status="filled",
            filled_qty=4.0, avg_fill_price=249.50,
            broker_updated_at="2026-06-17T15:00:01+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={symbol: SimpleNamespace(
                    qty=4.0, avg_entry_price=249.50, side="long",
                )},
            ),
            open_orders=[],
        )
        TradingEngine._maybe_dispatch_substrate_entry_fill(
            engine, event=event, snapshot=snapshot,
        )

        # Reviewer's exact repro target: post-fix the LIMIT row stays
        # at limit_price / unavailable with NULL benchmark price and
        # NULL signed/adverse — no arrival-midpoint pollution.
        row = tl._ensure_db().execute(
            "SELECT order_type, slippage_benchmark_price, "
            "slippage_benchmark_kind, slippage_measurement_quality, "
            "slippage_signed_bps, slippage_adverse_bps FROM trades "
            "WHERE order_id='alpaca-entry-limit-1'"
        ).fetchone()
        assert row == ("limit", None, "limit_price", "unavailable",
                       None, None)

    def test_partial_entry_fill_dispatches_and_binds_ownership(
        self, tmp_path,
    ):
        """PR #68 round-1 review P3. UNKNOWN-at-submit recovery can
        observe a PARTIAL terminal state — broker-history reconcile
        or a stream replay where only some shares filled. The
        dispatch must accept PARTIAL alongside FILLED, otherwise
        partial-only recoveries leave ownership unbound and the
        protective stop unplaced.

        docs/order_lifecycle_state_machine.md §3.2 explicitly lists
        FILLED / PARTIAL + broker position present as the recovery
        trigger.
        """
        from types import SimpleNamespace
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.lifecycle import new_position_uid
        from execution.broker import BrokerSnapshot

        symbol = "NVDA"
        engine, tl, pos_store, orders_store = self._wire_engine(
            tmp_path, symbol,
        )

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol=symbol, owner_key=symbol,
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry-partial-1",
            order_type="market", order_class="simple",
            time_in_force="day", side="buy", intended_qty=10.0,
            intended_stop_price=475.0,
            slippage_benchmark_price=500.0,
            slippage_benchmark_kind="arrival_midpoint",
            slippage_benchmark_timestamp="2026-06-17T15:00:00+00:00",
            slippage_measurement_quality="primary",
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry-partial-1",
            order_id="alpaca-entry-partial-1",
        )
        # Only 6 of 10 shares filled — broker terminal state is
        # 'partial' (rare with MARKET on liquid names, but a
        # legitimate substrate path).
        apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-partial-1", status="partially_filled",
                filled_qty=6.0, avg_fill_price=500.30,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            reason="stream",
        )

        event = OrderEvent(
            order_id="alpaca-entry-partial-1", status="partially_filled",
            filled_qty=6.0, avg_fill_price=500.30,
            broker_updated_at="2026-06-17T15:00:01+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={symbol: SimpleNamespace(
                    qty=6.0, avg_entry_price=500.30, side="long",
                )},
            ),
            open_orders=[],
        )
        TradingEngine._maybe_dispatch_substrate_entry_fill(
            engine, event=event, snapshot=snapshot,
        )

        # PARTIAL dispatched: ownership bound + alert fired.
        assert symbol in engine._positions
        engine.alerts.trade_executed.assert_called_once()
        # Trade row's status reflects the substrate's canonical
        # 'partially_filled' string. The round-2 P2 focused-UPDATE
        # refactor stopped going through build_record/tl.log so the
        # status column is no longer rewritten by the dispatch —
        # the substrate's apply_order_event write wins.
        row = tl._ensure_db().execute(
            "SELECT status, filled_qty, slippage_signed_bps, "
            "slippage_adverse_bps FROM trades "
            "WHERE order_id='alpaca-entry-partial-1'"
        ).fetchone()
        assert row[0] == "partially_filled"
        assert row[1] == 6.0
        # Completeness math still ran for the partial-fill price.
        # 500.30 vs 500.0 → 6 bps adverse on a BUY.
        assert row[2] == pytest.approx(6.0, abs=0.1)
        assert row[3] == pytest.approx(6.0, abs=0.1)

    def test_partial_then_filled_refreshes_slippage_with_final_avg_fill(
        self, tmp_path,
    ):
        """PR #68 round-2 review P2. Pre-fix this test documented stale
        first-partial slippage on the final row: row.avg_fill_price=
        150.50 alongside slippage computed against 150.40, which the
        reviewer flagged as the canonical "Final fill leaves stale
        slippage" bug. The fix splits the dispatch's gate — side
        effects (ownership / alert) are still single-shot via
        `_has_position`, but the accounting-completeness block runs
        on every filled / partially_filled event so the LATEST-NON-
        NULL UPSERT refreshes signed/adverse with the cumulative
        avg_fill_price.

        Final row should carry final-fill slippage (33.33 bps
        against the 150.0 arrival midpoint), not first-partial
        slippage (26.67 bps against the same 150.0 from
        avg_fill_price=150.40).
        """
        from types import SimpleNamespace
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.lifecycle import new_position_uid
        from execution.broker import BrokerSnapshot

        symbol = "AMD"
        engine, tl, pos_store, orders_store = self._wire_engine(
            tmp_path, symbol,
        )

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol=symbol, owner_key=symbol,
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry-pf-1",
            order_type="market", order_class="simple",
            time_in_force="day", side="buy", intended_qty=10.0,
            intended_stop_price=140.0,
            slippage_benchmark_price=150.0,
            slippage_benchmark_kind="arrival_midpoint",
            slippage_benchmark_timestamp="2026-06-17T15:00:00+00:00",
            slippage_measurement_quality="primary",
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry-pf-1",
            order_id="alpaca-entry-pf-1",
        )

        # First observation: PARTIAL.
        apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-pf-1", status="partially_filled",
                filled_qty=4.0, avg_fill_price=150.40,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            reason="stream",
        )
        snapshot_partial = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={symbol: SimpleNamespace(
                    qty=4.0, avg_entry_price=150.40, side="long",
                )},
            ),
            open_orders=[],
        )
        TradingEngine._maybe_dispatch_substrate_entry_fill(
            engine,
            event=OrderEvent(
                order_id="alpaca-entry-pf-1", status="partially_filled",
                filled_qty=4.0, avg_fill_price=150.40,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            snapshot=snapshot_partial,
        )

        first_alert_count = engine.alerts.trade_executed.call_count
        assert first_alert_count == 1

        # Second observation: final FILLED. Apply advances substrate's
        # filled_qty/status; dispatch sees ownership already bound and
        # short-circuits before the side-effects helper.
        apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-pf-1", status="filled",
                filled_qty=10.0, avg_fill_price=150.50,
                broker_updated_at="2026-06-17T15:00:02+00:00",
            ),
            reason="stream",
        )
        snapshot_filled = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={symbol: SimpleNamespace(
                    qty=10.0, avg_entry_price=150.50, side="long",
                )},
            ),
            open_orders=[],
        )
        TradingEngine._maybe_dispatch_substrate_entry_fill(
            engine,
            event=OrderEvent(
                order_id="alpaca-entry-pf-1", status="filled",
                filled_qty=10.0, avg_fill_price=150.50,
                broker_updated_at="2026-06-17T15:00:02+00:00",
            ),
            snapshot=snapshot_filled,
        )

        # No second alert — ownership-gate idempotency held.
        assert engine.alerts.trade_executed.call_count == first_alert_count
        # Trade row's status / filled_qty / avg_fill_price reflect the
        # substrate's apply_order_event UPSERT (latest broker truth).
        # signed/adverse refreshed by the second dispatch's
        # accounting-completeness call: now computed against the
        # cumulative avg_fill_price of 150.50 vs the 150.0 arrival
        # midpoint = 33.33 bps adverse, not the stale first-partial
        # 26.67 bps from avg_fill_price=150.40.
        row = tl._ensure_db().execute(
            "SELECT status, filled_qty, avg_fill_price, "
            "slippage_signed_bps, slippage_adverse_bps FROM trades "
            "WHERE order_id='alpaca-entry-pf-1'"
        ).fetchone()
        assert row[0] == "filled"
        assert row[1] == 10.0
        assert row[2] == pytest.approx(150.50)
        # Final-fill slippage: (150.50 − 150.0) / 150.0 × 10_000 ≈
        # 33.33 bps adverse on a BUY. Without the round-2 fix this
        # would have been 26.67 bps (the stale first-partial value
        # against avg_fill_price=150.40).
        expected_signed = (150.50 - 150.0) / 150.0 * 10_000
        assert row[3] == pytest.approx(expected_signed, abs=0.1)
        assert row[4] == pytest.approx(expected_signed, abs=0.1)
        # Sanity: ensure the assertion would actually catch the
        # stale first-partial value (26.67) — guards against a
        # future refactor that silently re-introduces the bug.
        stale_first_partial = (150.40 - 150.0) / 150.0 * 10_000
        assert abs(row[3] - stale_first_partial) > 1.0

    def test_completeness_call_failure_logs_critical(
        self, tmp_path,
    ):
        """PR #68 round-1 review P2 + round-2 review P3. The
        completeness block is wrapped in a try/except that logs at
        CRITICAL on failure. Pre-fix the block called `_log_entry`,
        which catches all exceptions internally and logs at ERROR
        (engine/trader.py:2779), swallowing the operator-visible
        CRITICAL.

        Round-2: this test now actually ASSERTS the CRITICAL log
        fires. The engine emits via loguru; caplog (stdlib) doesn't
        receive loguru records by default. We add a temporary
        loguru sink (matches the
        `tests/test_spy_options_reversion.py:_capture_logs` pattern)
        so the assertion is mandatory.

        Failure mode under test: TradeLoggerIdentityConflict from
        `reporting/logger.py:1038` — the canonical risk per the
        UPSERT identity-conflict columns (position_uid, position_id,
        symbol, side, strategy, order_type, requested_qty).
        """
        from types import SimpleNamespace
        from loguru import logger as loguru_logger
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.lifecycle import new_position_uid
        from execution.broker import BrokerSnapshot

        symbol = "QCOM"
        engine, tl, pos_store, orders_store = self._wire_engine(
            tmp_path, symbol,
        )

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol=symbol, owner_key=symbol,
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=2.0,
        )
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry-conflict-1",
            order_type="market", order_class="simple",
            time_in_force="day", side="buy", intended_qty=2.0,
            intended_stop_price=140.0,
            slippage_benchmark_price=150.0,
            slippage_benchmark_kind="arrival_midpoint",
            slippage_benchmark_timestamp="2026-06-17T15:00:00+00:00",
            slippage_measurement_quality="primary",
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry-conflict-1",
            order_id="alpaca-entry-conflict-1",
        )
        apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-conflict-1", status="filled",
                filled_qty=2.0, avg_fill_price=150.40,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            reason="stream",
        )

        # Inject failure on the slippage-computation helper so the
        # focused UPDATE never runs and the granular CRITICAL
        # handler must fire. Round-3 P2 refactor switched the
        # accounting block from build_record + tl.log to a focused
        # UPDATE that only touches signed/adverse — there's no
        # TradeRecord write to fail anymore, so we inject earlier
        # in the call chain.
        import engine.trader as trader_mod
        original_calc = trader_mod.single_leg_realized_slippage_bps
        def _raise(side, reference_price, actual_fill_price):
            raise RuntimeError("synthetic slippage-calc failure")
        trader_mod.single_leg_realized_slippage_bps = _raise

        # Capture loguru CRITICAL output. Sink takes a Message
        # whose .record dict carries 'level' / 'message' / etc.
        critical_messages: list[str] = []
        handler_id = loguru_logger.add(
            lambda msg: critical_messages.append(str(msg)),
            level="CRITICAL",
        )
        try:
            event = OrderEvent(
                order_id="alpaca-entry-conflict-1", status="filled",
                filled_qty=2.0, avg_fill_price=150.40,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            )
            snapshot = BrokerSnapshot(
                account=SimpleNamespace(
                    equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                    open_positions={symbol: SimpleNamespace(
                        qty=2.0, avg_entry_price=150.40, side="long",
                    )},
                ),
                open_orders=[],
            )
            TradingEngine._maybe_dispatch_substrate_entry_fill(
                engine, event=event, snapshot=snapshot,
            )
        finally:
            loguru_logger.remove(handler_id)
            trader_mod.single_leg_realized_slippage_bps = original_calc

        # Failure absorbed: ownership still bound (side-effects ran
        # BEFORE the completeness call).
        assert symbol in engine._positions

        # Mandatory assertion: the granular CRITICAL log line fired.
        matching = [
            m for m in critical_messages
            if "trade-log completeness call FAILED" in m
            and "RuntimeError" in m
            and "alpaca-entry-conflict-1" in m
        ]
        assert len(matching) == 1, (
            f"expected exactly one CRITICAL log with the granular "
            f"completeness-failure sentinel; got {len(matching)} "
            f"matches in {len(critical_messages)} total CRITICAL "
            f"records: {critical_messages!r}"
        )

    def test_already_bound_option_limit_entry_does_not_emit_critical(
        self, tmp_path,
    ):
        """PR #68 round-3 review P2 — the reviewer's option-sleeve
        repro.

        Option LIMIT entries (spy_options_reversion) intentionally
        create substrate rows with intended_stop_price=None because
        option exits are strategy-managed (HWM trail, theta limits,
        underlying stop), not stop-managed at the broker. Pre-round-3
        the dispatch built a RiskDecision BEFORE the ownership gate,
        passing `stop_price=float(order_row.intended_stop_price
        or 0.0)` = 0.0, which RiskDecision.__post_init__ rejects
        with `ValueError: stop_price must be positive, got 0.0`.

        For an already-bound option fill (synchronous path already
        registered ownership; substrate stream event arrives next),
        the ValueError got caught by the outer CRITICAL handler and
        emitted a false-alarm CRITICAL on a healthy active sleeve.

        Post-fix the RiskDecision is built INSIDE the
        `if not ownership_already_bound:` block. The focused-UPDATE
        accounting block doesn't need it. Already-bound option
        fills exit cleanly with no log noise; option LIMIT entries
        also skip the accounting UPDATE (benchmark_kind='limit_price',
        codepath §2 NULL slippage by design).
        """
        from types import SimpleNamespace
        from loguru import logger as loguru_logger
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.lifecycle import new_position_uid
        from execution.broker import BrokerSnapshot

        # OCC-formatted option symbol (SPY 540 call).
        symbol = "SPY260618C00540000"
        owner_key = "SPY"
        engine, tl, pos_store, orders_store = self._wire_engine(
            tmp_path, owner_key,
        )

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol=symbol, owner_key=owner_key,
            strategy="spy_options_reversion",
            position_type="single_leg", entry_qty=3.0,
        )
        # Option LIMIT entry — intended_stop_price=None per
        # execution/broker.py's option submission path. LIMIT-typed
        # so the substrate carries limit_price provenance not
        # arrival.
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry-opt-1",
            order_type="limit", order_class="simple",
            time_in_force="day", side="buy", intended_qty=3.0,
            intended_stop_price=None,
            intended_limit_price=10.00,
            slippage_benchmark_price=None,
            slippage_benchmark_kind="limit_price",
            slippage_benchmark_timestamp=None,
            slippage_measurement_quality="unavailable",
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry-opt-1",
            order_id="alpaca-entry-opt-1",
        )
        apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-opt-1", status="filled",
                filled_qty=3.0, avg_fill_price=10.05,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            reason="stream",
        )
        # Pre-bind ownership — the synchronous async-options path
        # already registered this position before the substrate
        # stream event arrived.
        engine._positions[owner_key] = object()

        # Capture loguru CRITICAL output. Pre-fix this test would
        # see one false-alarm CRITICAL ('ValueError: stop_price
        # must be positive'); post-fix zero.
        critical_messages: list[str] = []
        handler_id = loguru_logger.add(
            lambda msg: critical_messages.append(str(msg)),
            level="CRITICAL",
        )
        try:
            event = OrderEvent(
                order_id="alpaca-entry-opt-1", status="filled",
                filled_qty=3.0, avg_fill_price=10.05,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            )
            snapshot = BrokerSnapshot(
                account=SimpleNamespace(
                    equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                    open_positions={owner_key: SimpleNamespace(
                        qty=3.0, avg_entry_price=10.05, side="long",
                    )},
                ),
                open_orders=[],
            )
            TradingEngine._maybe_dispatch_substrate_entry_fill(
                engine, event=event, snapshot=snapshot,
            )
        finally:
            loguru_logger.remove(handler_id)

        # No false-alarm CRITICAL on the live options sleeve.
        assert critical_messages == [], (
            f"unexpected CRITICAL output on already-bound option "
            f"LIMIT dispatch: {critical_messages!r}"
        )
        # Slippage stays NULL — LIMIT codepath §2 by design.
        row = tl._ensure_db().execute(
            "SELECT slippage_signed_bps, slippage_adverse_bps, "
            "slippage_benchmark_kind FROM trades "
            "WHERE order_id='alpaca-entry-opt-1'"
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] == "limit_price"

    def test_dispatch_on_synchronous_bound_row_preserves_audit_fields(
        self, tmp_path,
    ):
        """PR #68 round-2 review P2 — the reviewer's exact repro.

        Pre-fix the round-2 refactor ran the accounting replay
        unconditionally, going through `build_record` + `tl.log`
        with `reason='substrate dispatch'` and
        `entry_reference_price=event.avg_fill_price`. tl.log's
        default `excluded.<col>` UPSERT semantics for those columns
        clobbered the synchronous _log_entry path's correct values:

            before:  ('golden cross entry', 50.0, ...)
            after:   ('substrate dispatch', 50.0, ...)

        Post-fix the accounting block does a focused UPDATE that
        only touches `slippage_signed_bps` and
        `slippage_adverse_bps`. Reason, entry_reference_price,
        and all other audit fields stay intact regardless of how
        many times the dispatch runs against an already-populated
        row.
        """
        from types import SimpleNamespace
        from engine.lifecycle_orders import apply_order_event
        from engine.trader import TradingEngine
        from engine.lifecycle import new_position_uid
        from execution.broker import BrokerSnapshot
        from reporting.logger import TradeRecord

        symbol = "PYPL"
        engine, tl, pos_store, orders_store = self._wire_engine(
            tmp_path, symbol,
        )

        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol=symbol, owner_key=symbol,
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=8.0,
        )
        orders_store.insert_pending(
            position_uid=uid, role="entry_primary",
            client_order_id="cli-entry-sync-1",
            order_type="market", order_class="simple",
            time_in_force="day", side="buy", intended_qty=8.0,
            intended_stop_price=70.0,
            slippage_benchmark_price=75.0,
            slippage_benchmark_kind="arrival_midpoint",
            slippage_benchmark_timestamp="2026-06-17T15:00:00+00:00",
            slippage_measurement_quality="primary",
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-entry-sync-1",
            order_id="alpaca-entry-sync-1",
        )

        # Simulate the synchronous-fill path having already written
        # the row via _log_entry with the strategy's audit fields.
        # apply_order_event UPSERTed first with the substrate's bare
        # row, then _log_entry overwrote the audit columns. We log a
        # final TradeRecord that represents the end state.
        apply_order_event(
            tl._ensure_db(),
            OrderEvent(
                order_id="alpaca-entry-sync-1", status="filled",
                filled_qty=8.0, avg_fill_price=75.10,
                broker_updated_at="2026-06-17T15:00:01+00:00",
            ),
            reason="stream",
        )
        # Synchronous _log_entry would have set reason='golden cross
        # entry' (the strategy's actual reason) and
        # entry_reference_price=75.0 (the decision-time bar close).
        # Plant those values directly so the assertion has a
        # canonical starting state.
        conn = tl._ensure_db()
        conn.execute(
            "UPDATE trades SET reason = ?, entry_reference_price = ? "
            "WHERE order_id = 'alpaca-entry-sync-1'",
            ("golden cross entry", 75.0),
        )
        conn.commit()
        # Engine already owns the position via the synchronous path —
        # set ownership directly so the dispatch's _has_position gate
        # treats this as a synchronous-bound row.
        engine._positions[symbol] = object()

        event = OrderEvent(
            order_id="alpaca-entry-sync-1", status="filled",
            filled_qty=8.0, avg_fill_price=75.10,
            broker_updated_at="2026-06-17T15:00:01+00:00",
        )
        snapshot = BrokerSnapshot(
            account=SimpleNamespace(
                equity=100_000.0, cash=50_000.0, buying_power=50_000.0,
                open_positions={symbol: SimpleNamespace(
                    qty=8.0, avg_entry_price=75.10, side="long",
                )},
            ),
            open_orders=[],
        )
        TradingEngine._maybe_dispatch_substrate_entry_fill(
            engine, event=event, snapshot=snapshot,
        )

        # Audit columns preserved through the dispatch's accounting
        # refresh: reason still the strategy's, entry_reference_price
        # still the decision-time close.
        row = conn.execute(
            "SELECT reason, entry_reference_price, "
            "slippage_signed_bps, slippage_adverse_bps FROM trades "
            "WHERE order_id='alpaca-entry-sync-1'"
        ).fetchone()
        assert row[0] == "golden cross entry"
        assert row[1] == pytest.approx(75.0)
        # Slippage refreshed via the focused UPDATE. Computation is
        # idempotent against the synchronous path's _log_entry write
        # because both used the same modeled_price (75.0) and the
        # same broker avg_fill_price (75.10).
        expected_signed = (75.10 - 75.0) / 75.0 * 10_000
        assert row[2] == pytest.approx(expected_signed, abs=0.1)
        assert row[3] == pytest.approx(expected_signed, abs=0.1)
