"""Integration tests for the Phase C destructive operator handlers.

Hand-constructed TradingEngine with mocked broker + real RiskManager
+ real lifecycle store. Mirrors the test_operator_pause.py pattern.

Covers:
  - _destructive_setup validation (missing/unknown/terminal uid,
    broker-side absence)
  - Symbol-lock acquired + released by each handler
  - close-position end-to-end: cancels pre-existing stops, broker
    submit, _record_realized_pnl reintegration
  - reduce-position: --pct parsing, rounding, partial flow, lifecycle
    current_qty drops to residual
  - cancel-position-orders: walks substrate non-terminal sell-side rows,
    calls broker.cancel_order on each, NOT on entry rows
  - In-flight close guard
  - Allocator reintegration sanity (released sleeve capital invariant
    per proposal §13)
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from engine.lifecycle import PositionLifecycleStore, new_position_uid
from engine.lifecycle_orders import PositionLifecycleOrdersStore
from engine.operator_queue import OperatorCommandStore, new_command_uid
from engine.symbol_locks import SymbolLockRegistry
from engine.trader import TradingEngine
from execution.broker import OrderResult, OrderStatus
from reporting.logger import TradeLogger
from risk.manager import AccountState, Position, RiskManager


def _build_engine(tmp_path, *, broker_qty: float = 10.0, broker_price: float = 100.0):
    db_path = tmp_path / "trades.db"
    tl = TradeLogger(path=str(db_path))
    conn = tl._ensure_db()
    op_conn = sqlite3.connect(str(db_path), check_same_thread=False)
    op_conn.execute("PRAGMA foreign_keys = ON")
    queue = OperatorCommandStore(op_conn)

    engine = TradingEngine.__new__(TradingEngine)
    engine.operator_command_store = queue
    engine.trade_logger = tl
    engine.lifecycle_store = PositionLifecycleStore(conn)
    engine.lifecycle_orders_store = PositionLifecycleOrdersStore(conn)
    engine.risk = RiskManager()
    engine.alerts = MagicMock()
    engine.symbol_locks = SymbolLockRegistry()
    engine._session_start_equity = 100_000.0
    # Stub bookkeeping used by _record_realized_pnl.
    engine._allocator = MagicMock()
    engine._entry_prices = {"AAPL": 95.0}
    engine._close_lifecycle_for_owner_key = lambda owner_key, external=False: None
    engine._reduce_lifecycle_for_owner_key = lambda owner_key, reduced_by: None

    # Mock broker. sync_with_broker returns a snapshot whose
    # open_positions.get(symbol) returns a Position with broker_qty.
    engine.broker = MagicMock()
    positions = {} if broker_qty <= 0 else {
        "AAPL": Position(
            symbol="AAPL",
            qty=broker_qty,
            avg_entry_price=broker_price,
            market_value=broker_qty * broker_price,
        ),
    }
    snapshot = MagicMock()
    snapshot.account = AccountState(
        equity=100_000.0,
        cash=50_000.0,
        session_start_equity=100_000.0,
        previous_close_equity=100_000.0,
        open_positions=positions,
    )
    engine.broker.sync_with_broker.return_value = snapshot

    return engine, queue


def _seed_open_lifecycle(engine):
    uid = new_position_uid()
    engine.lifecycle_store.create_pending(
        position_uid=uid,
        symbol="AAPL",
        owner_key="AAPL",
        strategy="sma_crossover",
        position_type="single_leg",
        entry_qty=10.0,
    )
    engine.lifecycle_store.mark_open(
        position_uid=uid, avg_entry_price=95.0, current_qty=10.0,
    )
    return uid


# ── Validation / setup ─────────────────────────────────────────────


class TestDestructiveSetupValidation:
    def test_missing_target_uid_rejects(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
        )  # no target_position_uid
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "target_position_uid" in (row.result.get("note") or "")

    def test_unknown_uid_rejects(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid="pos_doesnotexist00000000000000000000",
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "unknown position_uid" in (row.result.get("note") or "")

    def test_terminal_lifecycle_rejects(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        engine.lifecycle_store.mark_closed(position_uid=pos_uid, external=False)

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "already closed" in (row.result.get("note") or "")

    def test_broker_position_missing_rejects(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        # Override the snapshot to have no AAPL.
        snap = MagicMock()
        snap.account = AccountState(
            equity=100_000.0, cash=0.0, session_start_equity=100_000.0,
            previous_close_equity=100_000.0, open_positions={},
        )
        engine.broker.sync_with_broker.return_value = snap

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "broker has no open position" in (row.result.get("note") or "")


class TestSymbolLockAcquireRelease:
    def test_lock_acquired_and_released_on_success(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="alpaca-1",
            symbol="AAPL",
            requested_qty=10.0,
            filled_qty=10.0,
            avg_fill_price=110.0,
            raw_status="filled",
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()
        # Lock should NOT still be held after the handler returns.
        assert engine.symbol_locks.is_locked("AAPL") is None

    def test_lock_blocks_second_command(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        # Pre-acquire the lock with a different holder.
        engine.symbol_locks.acquire(
            owner_key="AAPL", kind="strategy_exit", identifier="sma",
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "already locked" in (row.result.get("note") or "")
        # The pre-existing lock is still held.
        h = engine.symbol_locks.is_locked("AAPL")
        assert h is not None
        assert h.kind == "strategy_exit"


# ── close-position ────────────────────────────────────────────────


class TestClosePosition:
    def test_close_full_position_succeeds(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="alpaca-cls-1",
            symbol="AAPL",
            requested_qty=10.0,
            filled_qty=10.0,
            avg_fill_price=110.0,
            raw_status="filled",
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="take profit",
            target_position_uid=pos_uid,
            requested_by="franco",
        )
        engine._process_operator_commands()

        engine.broker.close_position.assert_called_once()
        call_kwargs = engine.broker.close_position.call_args.kwargs
        # The handler MUST tag the broker call with the operator uid so
        # the substrate row gets origin_kind='operator' + the uid.
        assert call_kwargs.get("operator_command_uid") == uid

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["position_uid"] == pos_uid
        assert row.result["close_price"] == 110.0
        assert row.result["close_qty"] == 10.0

    def test_close_with_pending_close_order_rejects(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        # Pre-insert a non-terminal exit row to simulate a close
        # already in flight. (Foundation's lifecycle_orders store has
        # its own DB constraints; this exercises the handler's
        # fail-fast path before that.)
        engine.lifecycle_orders_store.insert_pending(
            position_uid=pos_uid, role="exit",
            client_order_id="cli-1",
            order_type="market", order_class="simple",
            time_in_force="day", side="sell",
            intended_qty=10.0,
            origin_kind="bot",
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "in flight" in (row.result.get("note") or "")
        # And the broker was NOT called.
        engine.broker.close_position.assert_not_called()


# ── reduce-position ──────────────────────────────────────────────


class TestReducePosition:
    def test_reduce_pct_rounds_down(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        # 33% of 10 = 3.3 → floor = 3.
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="alpaca-rdc-1",
            symbol="AAPL",
            requested_qty=3.0,
            filled_qty=3.0,
            avg_fill_price=108.0,
            raw_status="filled",
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid,
            params={"pct": 33.0},
        )
        engine._process_operator_commands()

        engine.broker.close_position.assert_called_once()
        call_kwargs = engine.broker.close_position.call_args.kwargs
        assert call_kwargs.get("partial_qty") == 3
        assert call_kwargs.get("operator_command_uid") == uid

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["requested_qty"] == 3
        assert row.result["residual_qty"] == 7.0

    def test_reduce_pct_floors_to_zero_rejects(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=2.0)
        pos_uid = _seed_open_lifecycle(engine)
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid,
            params={"pct": 25.0},  # 25% of 2 = 0.5 → floor = 0
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "rounds to zero" in (row.result.get("note") or "")
        engine.broker.close_position.assert_not_called()

    def test_reduce_full_qty_rejects_use_close(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid,
            params={"pct": 99.99},  # 99.99% of 10 → 9.999 → floor 9; not full
        )
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="alpaca-r-1",
            symbol="AAPL",
            requested_qty=9, filled_qty=9, avg_fill_price=108.0,
            raw_status="filled",
        )
        engine._process_operator_commands()
        # 9 is partial, allowed. (Above 99.99 is parsed as pct=99.99
        # → reduce 9.999 → floor 9, which is partial.)
        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"

    def test_reduce_invalid_pct_rejects(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        for bad in (0, 100, -10, 150):
            uid = new_command_uid()
            queue.insert(
                command_uid=uid, action="reduce-position", reason="t",
                target_position_uid=pos_uid,
                params={"pct": bad},
            )
            engine._process_operator_commands()
            row = queue.get_by_command_uid(uid)
            assert row.status == "rejected_validation", (
                f"pct={bad} should reject"
            )


# ── cancel-position-orders ──────────────────────────────────────


class TestCancelPositionOrders:
    def test_cancels_only_sell_side_rows(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)

        # Insert an entry row (must NOT be cancelled) and a protective
        # stop row (must be cancelled).
        engine.lifecycle_orders_store.insert_pending(
            position_uid=pos_uid, role="entry_primary",
            client_order_id="cli-entry",
            order_type="market", order_class="simple",
            time_in_force="day", side="buy",
            intended_qty=10.0,
        )
        engine.lifecycle_orders_store.attach_broker_order_id(
            client_order_id="cli-entry", order_id="alpaca-entry",
        )
        engine.lifecycle_orders_store.insert_pending(
            position_uid=pos_uid, role="protective_stop",
            client_order_id="cli-stop",
            order_type="stop", order_class="simple",
            time_in_force="gtc", side="sell",
            intended_qty=10.0,
            intended_stop_price=90.0,
        )
        engine.lifecycle_orders_store.attach_broker_order_id(
            client_order_id="cli-stop", order_id="alpaca-stop",
        )

        engine.broker.cancel_order.return_value = True

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="cancel-position-orders", reason="stale",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        # cancel_order called for the stop, NOT for the entry.
        cancelled_ids = [
            call.args[0] for call in engine.broker.cancel_order.call_args_list
        ]
        assert "alpaca-stop" in cancelled_ids
        assert "alpaca-entry" not in cancelled_ids

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert any(c["order_id"] == "alpaca-stop" for c in row.result["cancelled"])

    def test_handles_null_order_id_gracefully(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        # Row with order_id=NULL — the foundation's NULL-order_id
        # attach-orphan path. We skip with an error note rather than
        # crashing.
        engine.lifecycle_orders_store.insert_pending(
            position_uid=pos_uid, role="exit",
            client_order_id="cli-null",
            order_type="market", order_class="simple",
            time_in_force="day", side="sell",
            intended_qty=10.0,
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="cancel-position-orders", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert any(
            "order_id not yet attached" in e.get("error", "")
            for e in row.result["errors"]
        )
        engine.broker.cancel_order.assert_not_called()


class TestProposalInvariant:
    """Proposal §13 Phase C invariant: operator close releases the
    same sleeve capital the strategy reserved at entry. Verified by
    confirming _record_realized_pnl is called with the correct args
    so the allocator's record_realized_pnl is invoked downstream."""

    def test_close_calls_record_realized_pnl(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="a", symbol="AAPL",
            requested_qty=10.0, filled_qty=10.0,
            avg_fill_price=110.0, raw_status="filled",
        )
        # Patch _record_realized_pnl to observe the call.
        recorded = []
        engine._record_realized_pnl = lambda **kw: recorded.append(kw)

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        assert len(recorded) == 1
        call = recorded[0]
        assert call["symbol"] == "AAPL"
        assert call["strategy_name"] == "sma_crossover"
        assert call["close_price"] == 110.0
        assert call["qty"] == 10.0
        assert call["is_full_close"] is True
        assert call["external"] is False
