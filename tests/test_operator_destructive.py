"""Integration tests for the Phase C destructive operator handlers.

Hand-constructed TradingEngine with mocked broker + real RiskManager
+ real lifecycle store. Mirrors the test_operator_pause.py pattern.

Covers:
  - _destructive_setup validation (missing/unknown/terminal uid,
    broker-side absence)
  - Symbol-lock acquired + released by each handler
  - close-position end-to-end: cancels pre-existing stops, broker
    submit, _record_realized_pnl reintegration
  - reduce-position: exact --qty validation, partial flow, lifecycle
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
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from engine.lifecycle import PositionLifecycleStore, new_position_uid
from engine.lifecycle_orders import (
    OrderEvent,
    PositionLifecycleOrdersStore,
    apply_order_event,
)
from engine.operator_queue import OperatorCommandStore, new_command_uid
from engine.positions import owner_key_for
from engine.symbol_locks import SymbolLockRegistry
from engine.trader import TradingEngine
from execution.broker import OrderResult, OrderStatus
from reporting.logger import TradeLogger, TradeRecord
from risk.manager import AccountState, Position, RiskManager


def _build_engine(
    tmp_path,
    *,
    broker_qty: float = 10.0,
    broker_price: float = 100.0,
    symbol: str = "AAPL",
):
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
    engine._entry_prices = {owner_key_for(symbol): 95.0}
    engine._close_lifecycle_for_owner_key = lambda owner_key, external=False: None
    engine._reduce_lifecycle_for_owner_key = lambda owner_key, reduced_by: None

    # Mock broker. sync_with_broker returns a snapshot whose
    # open_positions.get(symbol) returns a Position with broker_qty.
    engine.broker = MagicMock()
    positions = {} if broker_qty <= 0 else {
        symbol: Position(
            symbol=symbol,
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


def _seed_open_lifecycle(
    engine,
    *,
    symbol: str = "AAPL",
    owner_key: str | None = None,
    strategy: str = "sma_crossover",
    qty: float = 10.0,
    entry_price: float = 95.0,
):
    uid = new_position_uid()
    engine.lifecycle_store.create_pending(
        position_uid=uid,
        symbol=symbol,
        owner_key=owner_key or symbol,
        strategy=strategy,
        position_type="single_leg",
        entry_qty=qty,
    )
    engine.lifecycle_store.mark_open(
        position_uid=uid, avg_entry_price=entry_price, current_qty=qty,
    )
    return uid


def _seed_entry_trade(
    engine,
    *,
    position_uid: str,
    symbol: str,
    strategy: str,
    qty: float,
    entry_price: float,
    initial_risk_per_share: float,
) -> None:
    """Write the production accounting anchors a partial close consumes."""
    multiplier = 100 if owner_key_for(symbol) != symbol else 1
    entered_at = "2026-08-28T14:30:00+00:00"
    engine.trade_logger.log(TradeRecord(
        timestamp=entered_at,
        symbol=symbol,
        side="buy",
        qty=qty,
        avg_fill_price=entry_price,
        order_id=f"entry-{position_uid}",
        strategy=strategy,
        reason="test entry",
        stop_price=entry_price - initial_risk_per_share,
        entry_reference_price=entry_price,
        modeled_slippage_bps=None,
        realized_slippage_bps=None,
        order_type="market",
        status="filled",
        requested_qty=qty,
        filled_qty=qty,
        initial_stop_loss=entry_price - initial_risk_per_share,
        initial_risk_per_share=initial_risk_per_share,
        initial_risk_dollars=(
            initial_risk_per_share * qty * multiplier
        ),
        entry_timestamp=entered_at,
        position_id=owner_key_for(symbol),
        position_type="single_leg",
        position_uid=position_uid,
    ))


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
    def test_reduce_uses_exact_qty(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
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
            params={"qty": 3},
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
        assert row.result["quantity_unit"] == "shares"

    def test_reduce_full_qty_rejects_use_close(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=3.0)
        pos_uid = _seed_open_lifecycle(engine)
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid, params={"qty": 3},
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "use close-position" in (row.result.get("note") or "")
        engine.broker.close_position.assert_not_called()

    def test_reduce_qty_above_current_rejects(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=3.0)
        pos_uid = _seed_open_lifecycle(engine)
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid, params={"qty": 4},
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "use close-position" in (row.result.get("note") or "")
        engine.broker.close_position.assert_not_called()

    def test_reduce_rejects_fractional_only_residual(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=3.5)
        pos_uid = _seed_open_lifecycle(engine, qty=3.5)
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid, params={"qty": 3},
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "less than one whole" in (row.result.get("note") or "")
        engine.broker.close_position.assert_not_called()

    @pytest.mark.parametrize("params", [
        {}, {"qty": 0}, {"qty": -1}, {"qty": 1.5},
        {"qty": float("nan")}, {"qty": float("inf")}, {"qty": True},
        {"qty": "bad"},
        {"pct": 50},
    ])
    def test_reduce_invalid_or_legacy_qty_rejects(self, tmp_path, params):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid, params=params,
        )
        engine._process_operator_commands()
        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        engine.broker.close_position.assert_not_called()

    @pytest.mark.parametrize(
        ("current_qty", "reduce_qty", "residual_qty"),
        [(2, 1, 1), (3, 1, 2), (3, 2, 1)],
    )
    def test_reduce_single_leg_option_by_exact_contracts(
        self, tmp_path, current_qty, reduce_qty, residual_qty,
    ):
        occ = "SPY260925C00700000"
        engine, queue = _build_engine(
            tmp_path, broker_qty=float(current_qty), broker_price=10.0, symbol=occ,
        )
        pos_uid = _seed_open_lifecycle(
            engine,
            symbol=occ,
            owner_key="SPY",
            strategy="spy_options_reversion",
            qty=float(current_qty),
        )
        engine._entry_prices["SPY"] = 10.0
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="alpaca-opt-reduce",
            symbol=occ,
            requested_qty=float(reduce_qty),
            filled_qty=float(reduce_qty),
            avg_fill_price=12.0,
            raw_status="filled",
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid,
            action="reduce-position",
            reason="reduce option exposure",
            target_position_uid=pos_uid,
            params={"qty": reduce_qty},
        )
        engine._process_operator_commands()

        engine.broker.close_position.assert_called_once_with(
            occ,
            position_uid=pos_uid,
            partial_qty=reduce_qty,
            operator_command_uid=uid,
        )
        engine._allocator.record_realized_pnl.assert_called_once_with(
            "spy_options_reversion",
            pytest.approx((12.0 - 10.0) * reduce_qty * 100),
            position_uid=pos_uid,
            is_full_close=False,
        )
        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["quantity_unit"] == "contracts"
        assert row.result["current_qty"] == float(current_qty)
        assert row.result["requested_qty"] == reduce_qty
        assert row.result["residual_qty"] == float(residual_qty)


# ── cancel-position-orders ──────────────────────────────────────


class TestDurableReduceAccounting:
    @pytest.mark.parametrize("substrate_first", [True, False])
    @pytest.mark.parametrize(
        (
            "symbol", "owner_key", "strategy", "current_qty", "reduce_qty",
            "entry_price", "close_price", "initial_risk_per_share",
            "expected_pnl", "expected_r",
        ),
        [
            (
                "AAPL", "AAPL", "sma_crossover", 10.0, 3,
                95.0, 110.0, 5.0, 45.0, 3.0,
            ),
            (
                "SPY260925C00700000", "SPY", "spy_options_reversion", 3.0, 1,
                10.0, 12.0, 2.0, 200.0, 1.0,
            ),
        ],
    )
    def test_reduce_persists_one_restart_safe_partial_close(
        self,
        tmp_path,
        substrate_first,
        symbol,
        owner_key,
        strategy,
        current_qty,
        reduce_qty,
        entry_price,
        close_price,
        initial_risk_per_share,
        expected_pnl,
        expected_r,
    ):
        """Broker truth and enriched accounting may arrive in either order."""
        engine, queue = _build_engine(
            tmp_path,
            broker_qty=current_qty,
            broker_price=entry_price,
            symbol=symbol,
        )
        position_uid = _seed_open_lifecycle(
            engine,
            symbol=symbol,
            owner_key=owner_key,
            strategy=strategy,
            qty=current_qty,
            entry_price=entry_price,
        )
        _seed_entry_trade(
            engine,
            position_uid=position_uid,
            symbol=symbol,
            strategy=strategy,
            qty=current_qty,
            entry_price=entry_price,
            initial_risk_per_share=initial_risk_per_share,
        )
        engine._entry_prices[owner_key] = entry_price
        engine._reduce_lifecycle_for_owner_key = (
            TradingEngine._reduce_lifecycle_for_owner_key.__get__(engine)
        )

        command_uid = new_command_uid()
        order_id = "operator-reduce-order"
        broker_updated_at = "2026-08-28T15:00:00+00:00"
        result = OrderResult(
            status=OrderStatus.FILLED,
            order_id=order_id,
            symbol=symbol,
            requested_qty=float(reduce_qty),
            filled_qty=float(reduce_qty),
            avg_fill_price=close_price,
            raw_status="filled",
            submitted_at=datetime(2026, 8, 28, 14, 59, tzinfo=timezone.utc),
            filled_at=datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc),
        )

        def close_position(*_args, **_kwargs):
            engine.lifecycle_orders_store.insert_pending(
                position_uid=position_uid,
                role="partial_close",
                client_order_id="operator-reduce-cloid",
                order_type="market",
                order_class="simple",
                time_in_force="day",
                side="sell",
                intended_qty=float(reduce_qty),
                origin_kind="operator",
                operator_command_uid=command_uid,
            )
            engine.lifecycle_orders_store.attach_broker_order_id(
                client_order_id="operator-reduce-cloid",
                order_id=order_id,
            )
            if substrate_first:
                apply_order_event(
                    engine.trade_logger._ensure_db(),
                    OrderEvent(
                        order_id=order_id,
                        status="filled",
                        filled_qty=float(reduce_qty),
                        avg_fill_price=close_price,
                        broker_updated_at=broker_updated_at,
                    ),
                )
            return result

        engine.broker.close_position.side_effect = close_position
        queue.insert(
            command_uid=command_uid,
            action="reduce-position",
            reason="trim exposure",
            target_position_uid=position_uid,
            params={"qty": reduce_qty},
        )
        engine._process_operator_commands()

        if not substrate_first:
            outcome = apply_order_event(
                engine.trade_logger._ensure_db(),
                OrderEvent(
                    order_id=order_id,
                    status="filled",
                    filled_qty=float(reduce_qty),
                    avg_fill_price=close_price,
                    broker_updated_at=broker_updated_at,
                ),
            )
            assert outcome.applied is True

        conn = engine.trade_logger._ensure_db()
        conn.row_factory = sqlite3.Row
        trade_rows = conn.execute(
            "SELECT * FROM trades WHERE order_id = ?", (order_id,),
        ).fetchall()
        assert len(trade_rows) == 1
        trade = trade_rows[0]
        assert trade["status"] == "partial"
        assert trade["position_uid"] == position_uid
        assert trade["realized_pnl"] == pytest.approx(expected_pnl)
        assert trade["r_multiple"] == pytest.approx(expected_r)
        assert trade["entry_timestamp"] is not None
        assert trade["exit_timestamp"] is not None
        assert trade["reason"] == "operator reduce: trim exposure"

        lifecycle = engine.lifecycle_store.get_by_position_uid(position_uid)
        assert lifecycle.status == "open"
        assert lifecycle.current_qty == pytest.approx(current_qty - reduce_qty)
        assert lifecycle.net_realized_pnl == pytest.approx(expected_pnl)

        engine._allocator.record_realized_pnl.assert_called_once_with(
            strategy,
            pytest.approx(expected_pnl),
            position_uid=position_uid,
            is_full_close=False,
        )
        summary = engine.trade_logger.read_strategy_realized_pnl_summary([strategy])
        assert summary[strategy] == {
            "realized_pnl": pytest.approx(expected_pnl),
            "hwm": pytest.approx(expected_pnl),
            "trade_count": 0.0,
            "seen_position_uids": [],
        }

        command = queue.get_by_command_uid(command_uid)
        assert command.status == "succeeded"
        assert command.result["accounting_status"] == "persisted"
        assert command.result["lifecycle_net_realized_pnl"] == pytest.approx(
            expected_pnl
        )

    def test_accounting_failure_after_fill_is_explicit_and_not_retryable(
        self, tmp_path,
    ):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        position_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED,
            order_id="reduce-filled-accounting-failed",
            symbol="AAPL",
            requested_qty=2.0,
            filled_qty=2.0,
            avg_fill_price=101.0,
            raw_status="filled",
        )
        engine._log_close = MagicMock(return_value=False)
        engine._record_realized_pnl = MagicMock()

        command_uid = new_command_uid()
        queue.insert(
            command_uid=command_uid,
            action="reduce-position",
            reason="trim exposure",
            target_position_uid=position_uid,
            params={"qty": 2},
        )
        engine._process_operator_commands()

        command = queue.get_by_command_uid(command_uid)
        assert command.status == "failed"
        assert command.result["broker_fill_occurred"] is True
        assert command.result["accounting_status"] == "failed"
        assert "DO NOT retry" in command.result["note"]
        engine._record_realized_pnl.assert_called_once()


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
        """A row with order_id=NULL counts as an error (we can't issue
        a broker cancel without an id). Per F6 semantics: when every
        cancellable row results in error and zero broker cancels
        succeed, the command is FAILED — the operator must know the
        stale orders are still live."""
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
        # F6 contract: zero successful cancels + at least one error
        # → command is FAILED, not succeeded. The substrate row is
        # still live at the broker side; the operator must investigate.
        assert row.status == "failed"
        assert any(
            "order_id not yet attached" in e.get("error", "")
            for e in row.result["errors"]
        )
        engine.broker.cancel_order.assert_not_called()


class TestReviewFindings:
    """Round-2 review fixes — F1 (status gate), F2 (position_uid
    threading), F3 (spread rejection), F5 (protection degraded), F6
    (cancel failure semantics)."""

    # ── F1: handler must NOT book accounting on non-fill results ──

    def test_close_rejected_does_not_record_pnl_or_close_lifecycle(
        self, tmp_path,
    ):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.REJECTED,
            order_id=None,
            symbol="AAPL",
            requested_qty=10.0, filled_qty=0.0, avg_fill_price=None,
            raw_status="rejected", message="broker rejected",
        )
        recorded = []
        engine._record_realized_pnl = lambda **kw: recorded.append(kw)

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        # Command marked FAILED, not succeeded.
        row = queue.get_by_command_uid(uid)
        assert row.status == "failed"
        assert "did not produce a fill" in (row.result.get("note") or "")
        # NO accounting writes.
        assert recorded == []

    def test_close_partial_result_treated_as_reduce(self, tmp_path):
        """PARTIAL on a full-close request leaves the residual at the
        broker — handler must call _record_realized_pnl with
        is_full_close=False so the lifecycle stays open at the
        residual qty, not marked closed."""
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.PARTIAL,
            order_id="alpaca-1",
            symbol="AAPL",
            requested_qty=10.0, filled_qty=4.0, avg_fill_price=108.0,
            raw_status="partially_filled",
        )
        recorded = []
        engine._record_realized_pnl = lambda **kw: recorded.append(kw)

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["is_full_close"] is False
        # _record_realized_pnl invoked with the PARTIAL qty AND
        # is_full_close=False so the lifecycle stays open.
        assert len(recorded) == 1
        assert recorded[0]["is_full_close"] is False
        assert recorded[0]["qty"] == 4.0

    def test_reduce_rejected_does_not_record_pnl(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.REJECTED,
            order_id=None, symbol="AAPL",
            requested_qty=3.0, filled_qty=0.0, avg_fill_price=None,
            raw_status="rejected",
        )
        recorded = []
        engine._record_realized_pnl = lambda **kw: recorded.append(kw)

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid, params={"qty": 3},
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "failed"
        assert recorded == []

    # ── F2: position_uid must be threaded to broker ──

    def test_close_passes_position_uid_to_broker(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED, order_id="a",
            symbol="AAPL", requested_qty=10.0, filled_qty=10.0,
            avg_fill_price=110.0, raw_status="filled",
        )
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        kwargs = engine.broker.close_position.call_args.kwargs
        assert kwargs.get("position_uid") == pos_uid, (
            "position_uid must be threaded so the broker's exit "
            "substrate row gets origin_kind='operator' tagging"
        )
        assert kwargs.get("operator_command_uid") == uid

    def test_reduce_passes_position_uid_to_broker(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED, order_id="a",
            symbol="AAPL", requested_qty=3.0, filled_qty=3.0,
            avg_fill_price=108.0, raw_status="filled",
        )
        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid, params={"qty": 3},
        )
        engine._process_operator_commands()

        kwargs = engine.broker.close_position.call_args.kwargs
        assert kwargs.get("position_uid") == pos_uid
        assert kwargs.get("operator_command_uid") == uid
        assert kwargs.get("partial_qty") == 3

    # ── F3: spread rejection ──

    @pytest.mark.parametrize(
        ("action", "params"),
        [("close-position", None), ("reduce-position", {"qty": 1})],
    )
    def test_destructive_setup_rejects_spread_lifecycles(
        self, tmp_path, action, params,
    ):
        engine, queue = _build_engine(tmp_path)
        # Seed a spread lifecycle.
        spread_uid = new_position_uid()
        engine.lifecycle_store.create_pending(
            position_uid=spread_uid,
            symbol="QQQ260710P00713000",
            owner_key="QQQ-spread-uuid",
            strategy="credit_spread",
            position_type="spread",
            entry_qty=1.0,
        )
        engine.lifecycle_store.mark_open(
            position_uid=spread_uid,
            avg_entry_price=2.50, current_qty=1.0,
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action=action, reason="t",
            target_position_uid=spread_uid, params=params,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "rejected_validation"
        assert "single_leg only" in (row.result.get("note") or "")
        engine.broker.close_position.assert_not_called()

    # ── F5: reduce-position protection_status flag ──

    def test_reduce_succeeded_carries_degraded_protection_flag(self, tmp_path):
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED, order_id="a",
            symbol="AAPL", requested_qty=3.0, filled_qty=3.0,
            avg_fill_price=108.0, raw_status="filled",
        )

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="reduce-position", reason="t",
            target_position_uid=pos_uid, params={"qty": 3},
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        # Reviewer F5: succeeded result MUST carry the degraded flag
        # so the operator knows the residual is temporarily unprotected.
        assert row.result.get("degraded") is True
        assert row.result.get("protection_status") == "pending_repair_cycle"
        assert "unprotected" in (row.result.get("protection_note") or "")

    # ── P2#1: close-position PARTIAL carries same degraded flag ──

    def test_close_partial_carries_degraded_protection_flag(self, tmp_path):
        """A PARTIAL close-position fill leaves residual at the broker
        with the protective stop cancelled — mirror the reduce-position
        contract and report `degraded=True` with `protection_status`
        so the operator sees the gap immediately."""
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.PARTIAL,
            order_id="alpaca-1",
            symbol="AAPL",
            requested_qty=10.0, filled_qty=4.0, avg_fill_price=108.0,
            raw_status="partially_filled",
        )
        engine._record_realized_pnl = lambda **kw: None

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["is_full_close"] is False
        assert row.result.get("degraded") is True
        assert row.result.get("protection_status") == "pending_repair_cycle"
        assert "unprotected" in (row.result.get("protection_note") or "")

    def test_close_full_does_not_carry_degraded_flag(self, tmp_path):
        """A clean FULL close has no residual and no protection gap —
        the degraded flag must NOT be set, otherwise the operator
        would see false alarms on every full close."""
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED, order_id="a",
            symbol="AAPL", requested_qty=10.0, filled_qty=10.0,
            avg_fill_price=110.0, raw_status="filled",
        )
        engine._record_realized_pnl = lambda **kw: None

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result["is_full_close"] is True
        assert "degraded" not in row.result
        assert "protection_status" not in row.result

    @pytest.mark.parametrize("action", ["close-position", "reduce-position"])
    def test_invalid_persisted_policy_fails_safe_to_broker_stop_reporting(
        self, tmp_path, action
    ):
        """Operator paths must not crash while interpreting legacy/corrupt
        policy text.  The shared lifecycle resolver fails safe to the stopped
        posture, so a residual is reported as awaiting stop repair."""
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.lifecycle_store.get_open_for_owner_key = MagicMock(
            return_value=SimpleNamespace(
                sizing_model="broken",
                protection_model="broken",
            )
        )
        requested_qty = 10.0 if action == "close-position" else 3.0
        engine.broker.close_position.return_value = OrderResult(
            status=(
                OrderStatus.PARTIAL
                if action == "close-position"
                else OrderStatus.FILLED
            ),
            order_id="policy-fallback",
            symbol="AAPL",
            requested_qty=requested_qty,
            filled_qty=3.0,
            avg_fill_price=108.0,
            raw_status="partially_filled",
        )
        engine._record_realized_pnl = MagicMock()

        command_uid = new_command_uid()
        queue.insert(
            command_uid=command_uid,
            action=action,
            reason="test corrupt policy fallback",
            target_position_uid=pos_uid,
            params={"qty": 3} if action == "reduce-position" else None,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(command_uid)
        assert row.status == "succeeded"
        assert row.result.get("degraded") is True
        assert row.result.get("protection_status") == "pending_repair_cycle"

    # ── N: close-position zero-fill defensive guard ──

    def test_close_filled_with_zero_qty_does_not_record_pnl(self, tmp_path):
        """A FILLED/PARTIAL status with filled_qty==0 is near-impossible
        at the broker, but the handler must NOT call
        _record_realized_pnl with qty=0 — that would log a phantom
        zero-PnL close. Mirror the reduce-position defensive guard."""
        engine, queue = _build_engine(tmp_path, broker_qty=10.0)
        pos_uid = _seed_open_lifecycle(engine)
        engine.broker.close_position.return_value = OrderResult(
            status=OrderStatus.FILLED, order_id="a",
            symbol="AAPL", requested_qty=10.0, filled_qty=0.0,
            avg_fill_price=110.0, raw_status="filled",
        )
        recorded = []
        engine._record_realized_pnl = lambda **kw: recorded.append(kw)

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="close-position", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "failed"
        assert "filled_qty<=0" in (row.result.get("note") or "")
        assert recorded == []

    # ── F6: cancel-position-orders failure semantics ──

    def test_cancel_all_broker_false_marks_failed(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        # Seed a stop row.
        engine.lifecycle_orders_store.insert_pending(
            position_uid=pos_uid, role="protective_stop",
            client_order_id="cli-stop",
            order_type="stop", order_class="simple",
            time_in_force="gtc", side="sell", intended_qty=10.0,
            intended_stop_price=90.0,
        )
        engine.lifecycle_orders_store.attach_broker_order_id(
            client_order_id="cli-stop", order_id="alpaca-stop",
        )

        # Broker reports failure.
        engine.broker.cancel_order.return_value = False

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="cancel-position-orders", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "failed", (
            "When every requested cancel returns False, the command "
            "must mark FAILED — the stop is still live at the broker."
        )

    def test_cancel_partial_failure_marks_succeeded_degraded(self, tmp_path):
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        # Two stops; broker cancels one and fails the other.
        for i, oid in enumerate(("alpaca-stop1", "alpaca-stop2")):
            engine.lifecycle_orders_store.insert_pending(
                position_uid=pos_uid, role="protective_stop",
                client_order_id=f"cli-{i}",
                order_type="stop", order_class="simple",
                time_in_force="gtc", side="sell", intended_qty=10.0,
                intended_stop_price=90.0,
            )
            engine.lifecycle_orders_store.attach_broker_order_id(
                client_order_id=f"cli-{i}", order_id=oid,
            )

        results = iter([True, False])
        engine.broker.cancel_order.side_effect = lambda *a, **kw: next(results)

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="cancel-position-orders", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result.get("degraded") is True

    def test_cancel_no_orders_to_cancel_is_clean_success(self, tmp_path):
        """No non-terminal sell-side rows → command succeeded without
        the degraded flag (operator's expectation already satisfied)."""
        engine, queue = _build_engine(tmp_path)
        pos_uid = _seed_open_lifecycle(engine)
        # No substrate rows seeded — clean lifecycle.

        uid = new_command_uid()
        queue.insert(
            command_uid=uid, action="cancel-position-orders", reason="t",
            target_position_uid=pos_uid,
        )
        engine._process_operator_commands()

        row = queue.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.result.get("degraded") is not True
        assert row.result.get("cancelled") == []
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
