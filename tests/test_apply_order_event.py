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
from pathlib import Path

import pytest

from engine.lifecycle import PositionLifecycleStore, new_position_uid
from engine.lifecycle_orders import (
    OrderEvent,
    OrderEventOutcome,
    PositionLifecycleOrdersStore,
    apply_order_event,
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


# ── Helpers ────────────────────────────────────────────────────────────────


def _seed_position(
    pos_store: PositionLifecycleStore,
    *,
    owner_key: str = "AAPL",
    entry_qty: float = 10.0,
) -> str:
    uid = new_position_uid()
    pos_store.create_pending(
        position_uid=uid,
        symbol=owner_key,
        owner_key=owner_key,
        strategy="sma_crossover",
        position_type="single_leg",
        entry_qty=entry_qty,
    )
    return uid


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
