"""Engine-level tests for the NULL-order_id REST sweep (tracker
row 89, 'Known follow-ups').

The sweep recovers single-leg ``position_lifecycle_orders`` rows
orphaned by a failed ``attach_broker_order_id`` or a bot crash
between async submit and the next cycle's attach-queue drain.

Coverage (acceptance criteria from the PR brief):

1. Outcome (a): broker returns alive order  → row attached
2. Outcome (b): broker returns terminal      → attach + advance
3. Outcome (c): broker returns 404 (unknown) → row rejected
4. Restart-gap closure on startup
5. REST budget respected
6. ``role='partial_close'`` excluded
7. Spread close rows excluded
8. PR #71 trailing-stop fallback race: no double-attach
9. Stale orphan (>1h) fires CRITICAL log
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from engine.lifecycle import PositionLifecycleStore, new_position_uid
from engine.lifecycle_orders import PositionLifecycleOrdersStore
from engine.trader import (
    EngineConfig,
    TradingEngine,
    _SUBSTRATE_NULL_ATTACH_SWEEP_LIMIT,
    _SUBSTRATE_NULL_ATTACH_SWEEP_MIN_AGE_SECONDS,
)
from execution.broker import (
    AccountState,
    AlpacaBroker,
    BrokerSnapshot,
)
from reporting.logger import TradeLogger
from risk.manager import RiskManager
from strategies.base import (
    BaseStrategy,
    OrderType,
    SignalFrame,
    StrategySlot,
)


# ── Fakes ──────────────────────────────────────────────────────────────────


class _NoopStrategy(BaseStrategy):
    name = "noop_strategy"
    preferred_order_type = OrderType.MARKET

    def _raw_signals(self, df):  # pragma: no cover - unused in sweep tests
        import pandas as pd

        return SignalFrame(
            entries=pd.Series([], dtype=bool),
            exits=pd.Series([], dtype=bool),
        )


def _make_broker_order(
    *,
    order_id: str,
    status: str = "new",
    filled_qty: float = 0.0,
    avg_price: float | None = None,
) -> SimpleNamespace:
    """Build the minimum alpaca-order-like object the sweep needs."""
    return SimpleNamespace(
        id=order_id,
        status=SimpleNamespace(value=status),
        filled_qty=str(filled_qty),
        filled_avg_price=str(avg_price) if avg_price is not None else None,
        updated_at="2026-06-20T14:30:00+00:00",
        submitted_at="2026-06-20T14:29:30+00:00",
        filled_at=None,
    )


def _make_snapshot(**overrides) -> BrokerSnapshot:
    equity = overrides.pop("equity", 100_000.0)
    return BrokerSnapshot(
        account=AccountState(
            equity=equity,
            cash=equity,
            session_start_equity=equity,
            previous_close_equity=None,
            open_positions={},
        ),
        open_orders=overrides.pop("open_orders", []),
    )


@pytest.fixture
def engine(tmp_path) -> TradingEngine:
    api = MagicMock()
    broker = AlpacaBroker(client=api)
    risk = RiskManager(
        max_position_pct=0.02,
        max_open_positions=5,
        max_gross_exposure_pct=0.50,
        atr_stop_multiplier=2.0,
        max_daily_loss_pct=0.05,
        hard_dollar_loss_cap=1_000_000.0,
        loss_streak_threshold=10,
        broker_error_threshold=1,
    )
    eng = TradingEngine(
        strategy=_NoopStrategy(),
        symbols=["AAPL"],
        risk=risk,
        broker=broker,
        trade_logger=TradeLogger(path=str(tmp_path / "trades.db")),
        config=EngineConfig(),
    )
    # Replace dispatch helpers with mocks so we can assert they
    # fired on terminal advances without invoking ownership / alerts.
    eng._maybe_dispatch_substrate_entry_fill = MagicMock()
    eng._maybe_dispatch_substrate_exit_fill = MagicMock()
    return eng


@pytest.fixture
def pos_store(engine: TradingEngine) -> PositionLifecycleStore:
    return PositionLifecycleStore(engine.trade_logger._ensure_db())


@pytest.fixture
def orders_store(engine: TradingEngine) -> PositionLifecycleOrdersStore:
    assert engine.lifecycle_orders_store is not None
    return engine.lifecycle_orders_store


def _seed_orphan(
    *,
    pos_store: PositionLifecycleStore,
    orders_store: PositionLifecycleOrdersStore,
    owner_key: str,
    cli: str,
    role: str = "entry_primary",
    side: str = "buy",
    age_seconds: int = 5 * 60,
    position_type: str = "single_leg",
) -> str:
    """Create an orphan row (status='pending', order_id=NULL) and
    backdate created_at so the age window is hit. Returns the
    position_uid."""
    uid = new_position_uid()
    pos_store.create_pending(
        position_uid=uid,
        symbol=owner_key,
        owner_key=owner_key if position_type == "single_leg" else uid,
        strategy=("credit_spread" if position_type == "spread"
                  else "sma_crossover"),
        position_type=position_type,
        entry_qty=10.0,
    )
    orders_store.insert_pending(
        position_uid=uid,
        role=role,
        client_order_id=cli,
        order_type="market",
        order_class=("mleg" if position_type == "spread"
                     else "oto" if role == "entry_primary" else "simple"),
        time_in_force="gtc",
        side=side,
        intended_qty=10.0,
    )
    old_ts = (
        datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    ).isoformat()
    orders_store._conn.execute(
        "UPDATE position_lifecycle_orders SET created_at = ? "
        "WHERE client_order_id = ?",
        (old_ts, cli),
    )
    orders_store._conn.commit()
    return uid


# ── (a) Alive order: attach ────────────────────────────────────────────────


class TestSweepAliveOrder:
    def test_attaches_alive_order(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        _seed_orphan(
            pos_store=pos_store, orders_store=orders_store,
            owner_key="AAPL", cli="cli-alive",
        )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            return_value=_make_broker_order(
                order_id="alpaca-alive", status="new",
            )
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=5,
        )

        row = orders_store.get_by_client_order_id("cli-alive")
        assert row is not None
        assert row.order_id == "alpaca-alive"
        # broker.status='new' maps to substrate 'working'; the sweep
        # runs apply_order_event so the state machine advances
        # along with the attach. The row is now reachable by the
        # regular reconciler.
        assert row.status == "working"
        assert row.submitted_at is not None


# ── (b) Terminal order: attach + advance ───────────────────────────────────


class TestSweepTerminalOrder:
    def test_attaches_and_advances_filled_order(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        _seed_orphan(
            pos_store=pos_store, orders_store=orders_store,
            owner_key="MSFT", cli="cli-filled",
        )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            return_value=_make_broker_order(
                order_id="alpaca-filled",
                status="filled",
                filled_qty=10.0,
                avg_price=420.0,
            )
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=5,
        )

        row = orders_store.get_by_client_order_id("cli-filled")
        assert row.order_id == "alpaca-filled"
        assert row.status == "filled"
        assert row.filled_qty == 10.0
        assert row.avg_fill_price == 420.0
        assert row.terminal_at is not None
        # Dispatch helpers fire on terminal advance — same as
        # _reconcile_substrate_via_rest.
        engine._maybe_dispatch_substrate_entry_fill.assert_called_once()


# ── (c) Unknown to broker: reject ──────────────────────────────────────────


class TestSweepUnknownToBroker:
    def test_marks_row_rejected_when_broker_404(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        uid = _seed_orphan(
            pos_store=pos_store, orders_store=orders_store,
            owner_key="NVDA", cli="cli-unknown",
        )
        # Broker returns None — matches the sweep's 404 contract.
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            return_value=None
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=5,
        )

        row = orders_store.get_by_client_order_id("cli-unknown")
        assert row.status == "rejected"
        assert row.order_id is None
        assert row.terminal_at is not None
        # Position-status CTE should have walked the parent out of
        # pending (no fill ever landed, only entry_primary is now
        # terminal → canceled).
        pos_row = orders_store._conn.execute(
            "SELECT status FROM position_lifecycle "
            "WHERE position_uid = ?",
            (uid,),
        ).fetchone()
        assert pos_row[0] == "canceled"


# ── (4) Startup gap closure ────────────────────────────────────────────────


class TestSweepStartupGapClosure:
    def test_startup_unbounded_sweep_recovers_all_orphans(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        for i in range(7):
            _seed_orphan(
                pos_store=pos_store, orders_store=orders_store,
                owner_key=f"SYM{i}", cli=f"cli-restart-{i}",
            )

        # Each cloid resolves to a distinct alive broker order.
        def by_cli(cli):
            return _make_broker_order(order_id=f"alpaca-{cli}")

        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            side_effect=by_cli,
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="startup", budget=None,
        )

        # All 7 orphans recovered: order_id populated; rows now
        # reachable by the regular P-3 reconciler. Broker status
        # 'new' maps to substrate 'working' via apply_order_event.
        for i in range(7):
            row = orders_store.get_by_client_order_id(
                f"cli-restart-{i}"
            )
            assert row.order_id == f"alpaca-cli-restart-{i}"
            assert row.status == "working"
        # All 7 broker calls happened.
        assert (
            engine.broker.get_order_by_client_id_for_sweep.call_count
            == 7
        )


# ── (5) REST budget respected ──────────────────────────────────────────────


class TestSweepRestBudget:
    def test_cycle_budget_caps_broker_calls(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        # Seed 4 orphans; budget=2 → only 2 broker calls.
        for i in range(4):
            _seed_orphan(
                pos_store=pos_store, orders_store=orders_store,
                owner_key=f"BUDGET{i}", cli=f"cli-bud-{i}",
            )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            side_effect=lambda cli: _make_broker_order(
                order_id=f"alpaca-{cli}",
            )
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=2,
        )

        assert (
            engine.broker.get_order_by_client_id_for_sweep.call_count
            == 2
        )
        # First 2 cloids attached (oldest-first ordering).
        assert (
            orders_store.get_by_client_order_id("cli-bud-0").order_id
            == "alpaca-cli-bud-0"
        )
        assert (
            orders_store.get_by_client_order_id("cli-bud-1").order_id
            == "alpaca-cli-bud-1"
        )
        # Last 2 still orphaned — next cycle will pick them up.
        assert (
            orders_store.get_by_client_order_id("cli-bud-2").order_id
            is None
        )
        assert (
            orders_store.get_by_client_order_id("cli-bud-3").order_id
            is None
        )

    def test_default_cycle_budget_is_five(self):
        # Pin the constant so a silent change is flagged.
        assert _SUBSTRATE_NULL_ATTACH_SWEEP_LIMIT == 5


# ── (6) Single-leg operator partial_close IS recovered ────────────────────


class TestSweepRecoversSingleLegPartialClose:
    def test_single_leg_operator_reduce_partial_close_orphan_recovered(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """Operator ``reduce-position`` submits via
        ``broker.close_position(partial_qty=...)`` which writes a
        ``role='partial_close'`` row through
        ``_lifecycle_orders_record_exit`` (execution/broker.py:2537,
        execution/broker.py:777). If the insert succeeds and the
        attach fails (or the bot crashes between submit and attach),
        the row is a single-leg partial_close orphan and the sweep
        MUST recover it — otherwise (a) the regular reconciler can't
        see NULL-order_id rows, (b) cancel-position-orders has no
        order_id to cancel, and (c) the close-side guard at
        engine/trader.py:4707-4713 blocks every future operator
        reduction attempt on this position."""
        uid = new_position_uid()
        pos_store.create_pending(
            position_uid=uid, symbol="SPY", owner_key="SPY",
            strategy="sma_crossover",
            position_type="single_leg", entry_qty=10.0,
        )
        old_ts = (
            datetime.now(timezone.utc) - timedelta(minutes=5)
        ).isoformat()
        orders_store._conn.execute(
            "INSERT INTO position_lifecycle_orders ("
            "position_uid, role, order_id, client_order_id, "
            "order_type, order_class, time_in_force, side, "
            "intended_qty, status, filled_qty, created_at, "
            "last_observed_at"
            ") VALUES (?, 'partial_close', NULL, ?, 'market', "
            "'simple', 'gtc', 'sell', 4.0, 'pending', 0.0, ?, ?)",
            (uid, "cli-sl-operator-pc", old_ts, old_ts),
        )
        orders_store._conn.commit()
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            return_value=_make_broker_order(
                order_id="alpaca-operator-pc", status="new",
            )
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=5,
        )

        engine.broker.get_order_by_client_id_for_sweep \
            .assert_called_once_with("cli-sl-operator-pc")
        row = orders_store.get_by_client_order_id("cli-sl-operator-pc")
        assert row.order_id == "alpaca-operator-pc"
        # State advanced via apply_order_event (alpaca 'new' →
        # substrate 'working'); now reachable by the regular
        # reconciler.
        assert row.status == "working"


# ── (7) Spread close rows excluded ─────────────────────────────────────────


class TestSweepExcludesSpreadClose:
    @pytest.mark.parametrize("role", ["exit", "partial_close"])
    def test_spread_close_row_never_touched(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
        role: str,
    ):
        """PR #72 §10.7 owns spread close substrate state — both
        the ``exit`` row (worker durable attach) and the
        ``partial_close`` residual placeholder (intentional NULL).
        The single-leg JOIN is the only load-bearing exclusion;
        both spread shapes must be filtered."""
        _seed_orphan(
            pos_store=pos_store, orders_store=orders_store,
            owner_key="SPY", cli=f"cli-spread-{role}",
            role=role, side="sell",
            position_type="spread",
        )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock()

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=5,
        )

        engine.broker.get_order_by_client_id_for_sweep \
            .assert_not_called()
        row = orders_store.get_by_client_order_id(
            f"cli-spread-{role}"
        )
        assert row.status == "pending"
        assert row.order_id is None


# ── (8) PR #71 trailing-stop fallback race ─────────────────────────────────


class TestSweepPr71RaceNoDoubleAttach:
    def test_query_excludes_rows_attached_out_of_band(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """First line of defense: when PR #71's trailing fallback
        (or the late normal-path attach, or an operator manual
        resolve) writes order_id between sweep cycles, the next
        sweep query must NOT return the row."""
        _seed_orphan(
            pos_store=pos_store, orders_store=orders_store,
            owner_key="TSLA", cli="cli-race",
            role="protective_stop", side="sell",
        )
        orders_store.attach_broker_order_id(
            client_order_id="cli-race", order_id="alpaca-race",
        )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock()

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=5,
        )

        engine.broker.get_order_by_client_id_for_sweep \
            .assert_not_called()

    def test_in_method_race_treats_matching_attach_as_benign(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        """Second line of defense: a racer attaches the same
        order_id between the sweep's broker call and its own
        attach_broker_order_id call. The sweep's attach raises
        ValueError; the sweep re-checks the row, sees the matching
        order_id, and proceeds without error."""
        _seed_orphan(
            pos_store=pos_store, orders_store=orders_store,
            owner_key="AMD", cli="cli-mid-race",
            role="protective_stop", side="sell",
        )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            return_value=_make_broker_order(
                order_id="alpaca-mid-race", status="new",
            )
        )

        # Wrap attach_broker_order_id so the FIRST call races a
        # PR #71-style attach in just before the sweep's own
        # attach lands — the sweep's call then raises (order_id
        # already set) and must treat the matching attach as
        # benign.
        real_attach = orders_store.attach_broker_order_id
        call_log: list[str] = []

        def racy_attach(*, client_order_id, order_id, submitted_at=None):
            if not call_log:
                # First call from the sweep: simulate the racer
                # winning by directly UPDATE'ing the row, then
                # raise the same ValueError the real attach would
                # have raised.
                orders_store._conn.execute(
                    "UPDATE position_lifecycle_orders "
                    "SET order_id = ?, submitted_at = ?, "
                    "    last_observed_at = ? "
                    "WHERE client_order_id = ?",
                    (order_id, "2026-06-20T14:29:00+00:00",
                     "2026-06-20T14:29:00+00:00", client_order_id),
                )
                orders_store._conn.commit()
                call_log.append("raced")
                raise ValueError(
                    f"row at client_order_id={client_order_id!r} "
                    f"already has order_id={order_id!r}"
                )
            return real_attach(
                client_order_id=client_order_id,
                order_id=order_id,
                submitted_at=submitted_at,
            )

        orders_store.attach_broker_order_id = racy_attach  # type: ignore

        # Should not raise.
        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=5,
        )

        # Row carries the raced-in order_id.
        row = orders_store.get_by_client_order_id("cli-mid-race")
        assert row.order_id == "alpaca-mid-race"
        # apply_order_event still ran with the broker event, so
        # the substrate state advanced past pending exactly once
        # (no double-application).
        assert row.status == "working"


# ── (R2) Starvation: failing rows must not block newer orphans ────────────


class TestSweepStarvationBackoff:
    """PR #73 review R2: if the oldest orphans repeatedly raise
    transient broker errors, the sweep must not let them eat the
    whole REST budget cycle after cycle while younger orphaned
    live orders sit unreachable.

    Mechanism: drop the SQL LIMIT (query unbounded) + in-loop
    REST budget + in-memory backoff for non-404 errors so failing
    rows are skipped on subsequent passes."""

    def test_first_orphan_failing_does_not_starve_newer_within_budget(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        # Seed 4 orphans. The oldest (id ASC) hits a transient
        # error; the next three must still be reached within the
        # budget on the SAME pass after the failing row is skipped
        # against the budget.
        for i in range(4):
            _seed_orphan(
                pos_store=pos_store, orders_store=orders_store,
                owner_key=f"STARV{i}", cli=f"cli-starv-{i}",
            )

        def lookup(cli):
            if cli == "cli-starv-0":
                raise RuntimeError("transient broker 5xx")
            return _make_broker_order(order_id=f"alpaca-{cli}")

        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            side_effect=lookup,
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=3,
        )

        # Failing row is armed for backoff; succeeding rows
        # attached.
        assert (
            "cli-starv-0" in engine._null_attach_sweep_backoff
        )
        for cli in ("cli-starv-1", "cli-starv-2"):
            row = orders_store.get_by_client_order_id(cli)
            assert row.order_id == f"alpaca-{cli}"
        # 4th orphan was past the budget on this pass; deferred
        # to next sweep.
        row3 = orders_store.get_by_client_order_id("cli-starv-3")
        assert row3.order_id is None
        # Exactly budget=3 broker calls happened — the failing
        # one counted because it actually hit the broker.
        assert (
            engine.broker.get_order_by_client_id_for_sweep.call_count
            == 3
        )

    def test_backed_off_row_skipped_on_next_sweep(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        # Three orphans; the oldest is in backoff from a prior
        # transient failure. The current sweep must skip it WITHOUT
        # consuming budget so younger orphans get their turn.
        for i in range(3):
            _seed_orphan(
                pos_store=pos_store, orders_store=orders_store,
                owner_key=f"BKOFF{i}", cli=f"cli-bkoff-{i}",
            )
        engine._null_attach_sweep_backoff["cli-bkoff-0"] = (
            datetime.now(timezone.utc) + timedelta(minutes=10)
        )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            side_effect=lambda cli: _make_broker_order(
                order_id=f"alpaca-{cli}",
            )
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=2,
        )

        # Backed-off row is untouched; broker never called for it.
        broker_call_clis = [
            call.args[0] for call in
            engine.broker.get_order_by_client_id_for_sweep.call_args_list
        ]
        assert "cli-bkoff-0" not in broker_call_clis
        assert "cli-bkoff-1" in broker_call_clis
        assert "cli-bkoff-2" in broker_call_clis
        assert orders_store.get_by_client_order_id(
            "cli-bkoff-0"
        ).order_id is None
        assert orders_store.get_by_client_order_id(
            "cli-bkoff-1"
        ).order_id == "alpaca-cli-bkoff-1"
        assert orders_store.get_by_client_order_id(
            "cli-bkoff-2"
        ).order_id == "alpaca-cli-bkoff-2"

    def test_successful_call_clears_prior_backoff(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
    ):
        # A previously-failing row whose backoff has expired must
        # have its backoff entry cleared on the next successful
        # broker call so it can be retried immediately if a future
        # transient error fires again.
        _seed_orphan(
            pos_store=pos_store, orders_store=orders_store,
            owner_key="CLEAR", cli="cli-clear",
        )
        engine._null_attach_sweep_backoff["cli-clear"] = (
            datetime.now(timezone.utc) - timedelta(minutes=1)
        )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            return_value=_make_broker_order(order_id="alpaca-clear")
        )

        engine._sweep_null_order_id_attaches(
            _make_snapshot(), reason="cycle", budget=5,
        )

        # Backoff entry cleared (expired AND broker call succeeded).
        assert "cli-clear" not in engine._null_attach_sweep_backoff
        assert (
            orders_store.get_by_client_order_id("cli-clear").order_id
            == "alpaca-clear"
        )


# ── (9) Stale orphan → CRITICAL ────────────────────────────────────────────


class TestSweepStaleOrphanCritical:
    def test_stale_orphan_emits_critical(
        self,
        engine: TradingEngine,
        pos_store: PositionLifecycleStore,
        orders_store: PositionLifecycleOrdersStore,
        caplog,
    ):
        from loguru import logger as loguru_logger

        # Backdate created_at to >1h ago → stale window triggered.
        _seed_orphan(
            pos_store=pos_store, orders_store=orders_store,
            owner_key="STALE", cli="cli-stale",
            age_seconds=2 * 3600,
        )
        engine.broker.get_order_by_client_id_for_sweep = MagicMock(
            return_value=_make_broker_order(order_id="alpaca-stale")
        )

        critical_messages = []
        sink_id = loguru_logger.add(
            lambda msg: critical_messages.append(msg.record["message"]),
            level="CRITICAL",
        )
        try:
            engine._sweep_null_order_id_attaches(
                _make_snapshot(), reason="cycle", budget=5,
            )
        finally:
            loguru_logger.remove(sink_id)

        assert any(
            "orphaned >" in m and "cli-stale" in m
            for m in critical_messages
        ), (
            f"stale-orphan CRITICAL not emitted. messages="
            f"{critical_messages}"
        )


# ── Constants smoke ────────────────────────────────────────────────────────


class TestSweepConstants:
    def test_min_age_is_sixty_seconds(self):
        assert _SUBSTRATE_NULL_ATTACH_SWEEP_MIN_AGE_SECONDS == 60
