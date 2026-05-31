"""Regression tests for Operator Controls Phase A PR-1 reviewer findings.

Each test corresponds to one finding from the PR review and would have
caught the original bug:

  - F1: backfill iterated `dict[str, Position]` as if values were floats.
  - F2: in-process exits never transitioned the lifecycle row to
        `closed`; on next restart they'd be mislabeled `external_closed`.
  - F3: broker-terminal `CANCELED` / `REJECTED` after submit left the
        pending lifecycle row leaking indefinitely.
  - F5: dry-run preflight created lifecycle rows that no broker order
        ever backed.

F4 was doc-only (no code path), so it has no test.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from engine.lifecycle import PositionLifecycleStore, new_position_uid
from execution.broker import (
    AlpacaBroker,
    OrderResult,
    OrderStatus,
)
from reporting.logger import TradeLogger
from risk.manager import (
    AccountState,
    Position,
    RiskDecision,
    Side,
)
from strategies.base import OrderType


@pytest.fixture
def store_and_path(tmp_path):
    db_path = tmp_path / "trades.db"
    tl = TradeLogger(path=str(db_path))
    conn = tl._ensure_db()
    return PositionLifecycleStore(conn), tl, str(db_path)


def _decision(symbol: str = "NVDA", qty: float = 10.0) -> RiskDecision:
    return RiskDecision(
        symbol=symbol,
        side=Side.BUY,
        qty=qty,
        order_type=OrderType.MARKET,
        stop_price=850.0,
        entry_reference_price=884.20,
        strategy_name="sma_crossover",
        reason="test",
    )


def _broker(
    *,
    lifecycle_store: PositionLifecycleStore | None = None,
    dry_run: bool = False,
) -> AlpacaBroker:
    """A real AlpacaBroker with mocked Alpaca client.

    We don't go anywhere near the network — we set up a fake API client
    and exercise the lifecycle helpers directly.
    """
    api = MagicMock()
    broker = AlpacaBroker.__new__(AlpacaBroker)
    broker._api = api
    broker._max_attempts = 1
    broker._base_delay = 0.0
    broker._time_in_force = "gtc"
    broker._stream_manager = None
    broker._dry_run = dry_run
    broker._pending_option_fills = []
    broker._pending_option_lock = MagicMock()
    broker._pending_spread_fills = []
    broker._pending_spread_lock = MagicMock()
    broker._lifecycle_store = lifecycle_store
    return broker


# ─────────────────────────────────────────────────────────────────────
# F1 — backfill must read Position dataclass fields, not float(value)
# ─────────────────────────────────────────────────────────────────────


class TestBackfillIteratesPositions:
    """Reviewer F1: `snapshot.account.open_positions` is `dict[str, Position]`,
    not `dict[str, float]`. Treating values as floats raises TypeError that
    the broad except swallows, breaking the backfill for every open
    position. This test fails against the original PR-1 code."""

    def test_synthesize_succeeds_for_position_dataclass(self, tmp_path, monkeypatch):
        # Build a minimal engine just so we can call _reconcile_position_lifecycle.
        from engine.trader import TradingEngine

        db_path = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db_path))
        store = PositionLifecycleStore(tl._ensure_db())

        engine = TradingEngine.__new__(TradingEngine)
        engine.lifecycle_store = store
        engine._positions = {}

        snapshot = MagicMock()
        snapshot.account = MagicMock()
        snapshot.account.open_positions = {
            "NVDA": Position(
                symbol="NVDA",
                qty=10.0,
                avg_entry_price=884.20,
                market_value=9001.00,
            ),
            "MU": Position(
                symbol="MU",
                qty=34.0,
                avg_entry_price=116.30,
                market_value=4114.00,
            ),
        }

        engine._reconcile_position_lifecycle(snapshot)

        rows = store.get_open()
        owner_keys = {r.owner_key for r in rows}
        assert owner_keys == {"NVDA", "MU"}, (
            "Backfill must synthesize a row for every broker-open equity "
            "position — the original PR-1 code raised TypeError on "
            "float(Position) and silently skipped them all."
        )
        nvda_row = next(r for r in rows if r.owner_key == "NVDA")
        assert nvda_row.current_qty == 10.0
        assert nvda_row.avg_entry_price == 884.20


# ─────────────────────────────────────────────────────────────────────
# F2 — in-process exits must close the lifecycle row
# ─────────────────────────────────────────────────────────────────────


class TestInProcessExitClosesLifecycle:
    """Reviewer F2: a normal SMA / RSI exit (or stop fill, or recovered
    stop fill) must transition the lifecycle row to `closed`, not leave
    it `open` until restart where the reconcile pass mislabels it
    `external_closed`."""

    def test_record_realized_pnl_closes_lifecycle(self, tmp_path):
        from engine.trader import TradingEngine

        db_path = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db_path))
        store = PositionLifecycleStore(tl._ensure_db())

        # Seed an open lifecycle row for NVDA.
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

        # Minimal engine with the bookkeeping fields the helper touches.
        engine = TradingEngine.__new__(TradingEngine)
        engine.lifecycle_store = store
        engine._allocator = None  # PnL path no-ops
        engine._entry_prices = {"NVDA": 884.20}

        engine._record_realized_pnl(
            symbol="NVDA",
            strategy_name="sma_crossover",
            close_price=900.0,
            qty=10.0,
        )

        row = store.get_by_position_uid(uid)
        assert row.status == "closed", (
            "Normal in-process exit must transition lifecycle to 'closed' — "
            "leaving it open would let the operator CLI keep showing it as "
            "open until the next restart, where the reconcile pass would "
            "then mislabel it as external_closed."
        )

    def test_record_realized_pnl_external_marks_external_closed(self, tmp_path):
        from engine.trader import TradingEngine

        db_path = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db_path))
        store = PositionLifecycleStore(tl._ensure_db())
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

        engine = TradingEngine.__new__(TradingEngine)
        engine.lifecycle_store = store
        engine._allocator = None
        engine._entry_prices = {}

        engine._record_realized_pnl(
            symbol="NVDA",
            strategy_name="sma_crossover",
            close_price=0.0,
            qty=0.0,
            external=True,
        )

        row = store.get_by_position_uid(uid)
        assert row.status == "external_closed"


# ─────────────────────────────────────────────────────────────────────
# F3 — broker-terminal cancel/reject must not leak pending rows
# ─────────────────────────────────────────────────────────────────────


class TestPendingRowDoesNotLeak:
    """Reviewer F3: when Alpaca terminally cancels/rejects after accepting
    submission (zero fills), _wait_for_fill returns an OrderResult with
    status CANCELED/REJECTED. The pre-submit pending lifecycle row must
    transition to `canceled`."""

    def test_canceled_zero_fill_marks_canceled(self, store_and_path):
        store, _, _ = store_and_path
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        broker = _broker(lifecycle_store=store)

        result = OrderResult(
            status=OrderStatus.CANCELED,
            order_id="alpaca-cancel-1",
            symbol="NVDA",
            requested_qty=10.0,
            filled_qty=0.0,
            avg_fill_price=None,
            raw_status="canceled",
            message="broker canceled",
        )
        broker._lifecycle_mark_filled(position_uid=uid, result=result)

        row = store.get_by_position_uid(uid)
        assert row.status == "canceled", (
            "Zero-fill broker cancel must transition lifecycle to canceled — "
            "the original PR-1 code only handled FILLED/PARTIAL, leaving "
            "the pending row leaking forever."
        )

    def test_rejected_zero_fill_marks_canceled(self, store_and_path):
        store, _, _ = store_and_path
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        broker = _broker(lifecycle_store=store)

        result = OrderResult(
            status=OrderStatus.REJECTED,
            order_id="alpaca-rej-1",
            symbol="NVDA",
            requested_qty=10.0,
            filled_qty=0.0,
            avg_fill_price=None,
            raw_status="rejected",
            message="broker rejected",
        )
        broker._lifecycle_mark_filled(position_uid=uid, result=result)

        assert store.get_by_position_uid(uid).status == "canceled"

    def test_canceled_with_partial_fill_stays_partially_filled(self, store_and_path):
        """Proposal §8.1 — partial fill + cancel must NOT become 'canceled'.
        It stays at the filled quantity (partially_filled)."""
        store, _, _ = store_and_path
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        broker = _broker(lifecycle_store=store)

        result = OrderResult(
            status=OrderStatus.CANCELED,
            order_id="alpaca-cancel-2",
            symbol="NVDA",
            requested_qty=10.0,
            filled_qty=4.0,
            avg_fill_price=884.20,
            raw_status="canceled",
            message="partial then broker canceled rest",
        )
        broker._lifecycle_mark_filled(position_uid=uid, result=result)

        row = store.get_by_position_uid(uid)
        assert row.status == "partially_filled", (
            "Per proposal §8.1, a partially-filled-then-cancelled entry "
            "stays partially_filled — never transitions to canceled."
        )
        assert row.current_qty == 4.0

    def test_timeout_leaves_row_pending(self, store_and_path):
        store, _, _ = store_and_path
        uid = new_position_uid()
        store.create_pending(
            position_uid=uid,
            symbol="NVDA", owner_key="NVDA",
            strategy="sma_crossover", position_type="single_leg",
            entry_qty=10.0,
        )
        broker = _broker(lifecycle_store=store)

        result = OrderResult(
            status=OrderStatus.TIMEOUT,
            order_id="alpaca-1",
            symbol="NVDA",
            requested_qty=10.0,
            filled_qty=0.0,
            avg_fill_price=None,
            raw_status="pending_new",
            message="confirm timeout",
        )
        broker._lifecycle_mark_filled(position_uid=uid, result=result)

        # TIMEOUT/UNKNOWN intentionally leave the row pending so a
        # future reconcile pass observes broker truth.
        assert store.get_by_position_uid(uid).status == "pending"


# ─────────────────────────────────────────────────────────────────────
# F5 — dry-run must not write lifecycle rows
# ─────────────────────────────────────────────────────────────────────


class TestDryRunSkipsLifecycle:
    """Reviewer F5: a preflight dry-run must NOT persist pending
    lifecycle rows. The original PR-1 code wrote the row before the
    dry-run guard, so dev/preflight runs leaked rows that looked like
    real positions to the operator CLI."""

    def test_place_order_dry_run_writes_no_lifecycle_row(self, store_and_path):
        store, _, _ = store_and_path
        broker = _broker(lifecycle_store=store, dry_run=True)

        result = broker.place_order(_decision())

        # Dry-run returns a synthetic FILLED OrderResult.
        assert result.status == OrderStatus.FILLED
        # And — crucially — no lifecycle row was written.
        assert store.get_open() == [], (
            "Dry-run must not persist lifecycle rows. The original PR-1 "
            "code created a pending row before checking dry_run, leaking "
            "rows on every preflight run."
        )

    def test_place_fractional_order_dry_run_writes_no_lifecycle_row(self, store_and_path):
        store, _, _ = store_and_path
        broker = _broker(lifecycle_store=store, dry_run=True)
        # Fractional qty to force the fractional path.
        decision = _decision(qty=1.5)

        result = broker._place_fractional_order(
            decision, poll_timeout=0.1, poll_interval=0.05,
        )
        assert result.status == OrderStatus.FILLED
        assert store.get_open() == []


# ─────────────────────────────────────────────────────────────────────
# F6 — partial close must NOT close the lifecycle row
# ─────────────────────────────────────────────────────────────────────


class TestPartialCloseLeavesLifecycleOpen:
    """Reviewer F6: _close_single_leg_position calls _record_realized_pnl
    for both FILLED and PARTIAL results, but only removes engine
    ownership on FILLED. So a PARTIAL close has a residual broker/engine
    position. If _record_realized_pnl unconditionally closes the
    lifecycle row, the operator CLI hides a real managed residual."""

    def test_partial_close_keeps_lifecycle_open(self, tmp_path):
        from engine.trader import TradingEngine

        db_path = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db_path))
        store = PositionLifecycleStore(tl._ensure_db())

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

        engine = TradingEngine.__new__(TradingEngine)
        engine.lifecycle_store = store
        engine._allocator = None
        engine._entry_prices = {"NVDA": 884.20}

        # Simulate the PARTIAL-close branch of _close_single_leg_position.
        engine._record_realized_pnl(
            symbol="NVDA",
            strategy_name="sma_crossover",
            close_price=900.0,
            qty=4.0,  # only 4 of 10 shares filled
            is_full_close=False,
        )

        row = store.get_by_position_uid(uid)
        assert row.status == "open", (
            "PARTIAL close must leave lifecycle row open — the engine "
            "still tracks the residual, so the operator CLI must keep "
            "surfacing the position."
        )

    def test_full_close_still_closes_lifecycle(self, tmp_path):
        """Sanity: the default is_full_close=True path still closes the row."""
        from engine.trader import TradingEngine

        db_path = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db_path))
        store = PositionLifecycleStore(tl._ensure_db())

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

        engine = TradingEngine.__new__(TradingEngine)
        engine.lifecycle_store = store
        engine._allocator = None
        engine._entry_prices = {"NVDA": 884.20}

        engine._record_realized_pnl(
            symbol="NVDA",
            strategy_name="sma_crossover",
            close_price=900.0,
            qty=10.0,
            # is_full_close defaults to True
        )
        assert store.get_by_position_uid(uid).status == "closed"


# ─────────────────────────────────────────────────────────────────────
# F7 — stop-fill fallback (missing price/qty) must close the lifecycle
# ─────────────────────────────────────────────────────────────────────


class TestStopFillFallbackClosesLifecycle:
    """Reviewer F7: when a WebSocket stop-fill event arrives without a
    usable price/qty, _process_stream_stop_fills falls back to
    log_external_close. The lifecycle row must also be closed
    (external=True) so the operator CLI is not left showing the
    position as open until restart."""

    def test_fallback_branch_closes_lifecycle_external(self, tmp_path):
        from engine.trader import TradingEngine

        db_path = tmp_path / "trades.db"
        tl = TradeLogger(path=str(db_path))
        store = PositionLifecycleStore(tl._ensure_db())

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

        engine = TradingEngine.__new__(TradingEngine)
        engine.lifecycle_store = store

        # Drive the fallback close path directly: this mirrors what the
        # stop-fill fallback at engine/trader.py:2569 does after this
        # patch.
        engine._close_lifecycle_for_owner_key(
            owner_key="NVDA",
            external=True,
        )

        row = store.get_by_position_uid(uid)
        assert row.status == "external_closed", (
            "Stop-fill fallback (no price/qty) must transition the "
            "lifecycle row in-process — leaving it open would let the "
            "operator CLI keep showing the position until next restart."
        )
