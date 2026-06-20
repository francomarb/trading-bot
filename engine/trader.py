"""
Trading engine — the live loop.

`TradingEngine` orchestrates the modules built in earlier phases into a single
restart-safe runnable bot. Each cycle does, in order:

    sync_with_broker → for each strategy slot:
        fetch bars → freshness check → add indicators →
        strategy.generate_signals → risk.evaluate → broker.place_order → log

Design principles (CLAUDE.md / PLAN.md):

  1. **Broker is the source of truth.** Every cycle starts with
     `broker.sync_with_broker()`. Local cached state is never trusted across
     cycles for go/no-go decisions.

  2. **Restart-safe.** On startup the engine takes a broker snapshot before
     anything else. If the bot was killed mid-trade, the next startup sees
     reality (positions + open orders) rather than assuming clean state.

  3. **No stale-data trades.** `require_fresh` raises if the last bar is
     older than `bar_interval × max_bar_age_multiplier`. A live cycle
     refuses to trade on stale inputs — silence beats wrong action.

  4. **Exception containment.** Any error inside a per-symbol step is
     caught, logged at ERROR, and the engine continues to the next symbol /
     next cycle. A flaky data fetch must not crash the loop.

  5. **Market hours by default.** Cycles outside the regular session are
     skipped (configurable). Reduces wasted API calls and protects against
     the after-hours data quality gap.

  6. **Graceful shutdown on SIGINT.** Sets `_running = False`; the loop
     completes its current sleep and exits cleanly. Optionally cancels
     open orders on the way out (configurable — some workflows want orders
     left for next session).

  7. **Multi-strategy.** The engine accepts a list of `StrategySlot`
     objects, each binding a strategy to its own symbol universe (and
     optionally a Scanner for dynamic discovery). Risk and broker are
     shared across all slots — one account, one equity pool.
"""

from __future__ import annotations

import json
import math
import os
import re
import signal
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable
from zoneinfo import ZoneInfo

import pandas as pd
from loguru import logger

from config import settings
from config.settings import SLIPPAGE_MODEL_MARKET_BPS
from data.fetcher import StaleDataError, close_connections, fetch_symbol, require_fresh
from engine.positions import (
    Position,
    PositionLeg,
    build_credit_spread_snapshot,
    make_single_leg,
    make_spread,
    new_spread_id,
    owner_key_for,
    spread_substrate_uid,
    view_owner_map,
)
from engine.option_trailing import OptionTrailingStopStore
from execution.entry_guard import CapAction, compute_cap_price, gate_entry
from execution.broker import (
    AlpacaBroker,
    BrokerOrderAuditSnapshot,
    BrokerSnapshot,
    OptionQuote,
    OrderResult,
    OrderStatus,
)
from execution.options_executor import SpreadLeg
from execution.mleg_close import (
    MlegCloseScheduler,
    MlegQuote,
    resolve_mleg_close_profile,
)
from indicators.technicals import add_atr
from risk.manager import (
    AccountState,
    Position,
    RejectionCode,
    RiskDecision,
    RiskManager,
    RiskRejection,
    Side,
    Signal,
)
from reporting.alerts import AlertDispatcher
from reporting.logger import TradeLogger, single_leg_realized_slippage_bps
from reporting.pnl import PnLTracker
from strategies.base import (
    BaseStrategy,
    MultiLegTradeRejected,
    OptionTradeRejected,
    OrderType,
    StrategySlot,
)
from utils.option_symbols import is_occ_option, parse_occ_symbol

from regime.detector import MarketRegime

if TYPE_CHECKING:
    from execution.stream import StreamManager
    from regime.detector import RegimeDetector
    from risk.allocator import SleeveAllocator
    from sector.resolver import SectorResolver


# Matches any OCC option symbol: underlying (1–6 letters) + YYMMDD + C/P + 8-digit strike.
_OCC_PAT = re.compile(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$")


# P-2: cap on substrate cycle-reconciliation REST calls per cycle.
# A large gap (e.g., after a long stream outage) catches up over
# several cycles rather than blowing the per-cycle Alpaca API
# budget in one shot.
_SUBSTRATE_CYCLE_RECONCILE_LIMIT = 20


@dataclass(frozen=True)
class _OptionStopAuditContext:
    """One enabled, strategy-scoped replacement audit window."""

    correlation_id: str
    decision_at: datetime
    expires_at: datetime
    broker_before: BrokerOrderAuditSnapshot | None


# P-2: Alpaca REST order.status → substrate status. Same intent as
# stream's _STREAM_EVENT_TO_SUBSTRATE_STATUS but maps the FINAL
# status string the REST endpoint returns (not the event-stream
# verbs). Non-material statuses (pending_*, suspended) are absent
# from the map so they're skipped — the substrate doesn't advance
# on them.
_ALPACA_STATUS_TO_SUBSTRATE_STATUS = {
    "new": "working",
    "accepted": "working",
    "partially_filled": "partially_filled",
    "filled": "filled",
    "canceled": "canceled",
    "expired": "canceled",
    "replaced": "canceled",
    "rejected": "rejected",
    "done_for_day": "canceled",
    "stopped": "filled",
}


# ── Bar-interval helpers ─────────────────────────────────────────────────────


# Mirrors `data.fetcher._TIMEFRAME_MAP` but only the bit the engine cares
# about — the wall-clock duration of one bar.
_BAR_INTERVAL: dict[str, timedelta] = {
    "1Day": timedelta(days=1),
    "1Hour": timedelta(hours=1),
    "5Min": timedelta(minutes=5),
    "1Min": timedelta(minutes=1),
}

# Calendar days per bar — accounts for weekends/holidays so we always
# fetch enough bars.  Conservative: 1 daily bar ≈ 1.5 calendar days,
# 1 hourly bar ≈ 1 calendar day / 6.5 trading hours.
_CALENDAR_DAYS_PER_BAR: dict[str, float] = {
    "1Day": 1.5,
    "1Hour": 1.0 / 6.5,
    "5Min": 5.0 / (6.5 * 60),
    "1Min": 1.0 / (6.5 * 60),
}

_NEW_YORK_TZ = ZoneInfo("America/New_York")


def _lookback_days(required_bars: int, timeframe: str, config_lookback: int) -> int:
    """Compute calendar days of lookback to guarantee at least `required_bars` bars."""
    days_per_bar = _CALENDAR_DAYS_PER_BAR.get(timeframe, 1.5)
    strategy_days = int(required_bars * days_per_bar) + 5  # +5 day buffer
    return max(strategy_days, config_lookback)


_OPERATOR_HALT_REASON_PREFIXES = ("operator_halt:", "operator_halt_sticky:")


def _is_operator_halt_reason(reason: str) -> bool:
    """Return True when the active RiskManager halt was engaged by an
    operator command (vs. an independent risk gate).

    Per F2 (PR-2 review): `RiskManager.reset_kill_switches()` is a
    global clear, so the operator CLI's `resume-after-halt` must
    refuse unless the halt it would clear is specifically an
    operator-issued one. The two prefixes here are the exact strings
    `_apply_operator_halt` and `_restore_sticky_halt_state` write.
    """
    if not reason:
        return False
    return any(reason.startswith(p) for p in _OPERATOR_HALT_REASON_PREFIXES)


def _finite_or_none(value) -> float | None:
    """Return ``float(value)`` when finite and positive, else ``None``.

    Used by the arrival-price capture path so a misbehaving broker
    (returning Mock / NaN / negative / zero) can never inject a bad
    number into the slippage benchmark. Zero is rejected because a
    zero quote midpoint isn't a valid arrival price for any equity and
    would produce a divide-by-zero downstream.
    """
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f <= 0:
        return None
    return f


# ── Config ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngineConfig:
    """
    Engine-level knobs. Defaults pull from `config.settings`.

    Symbol lists and timeframes live on each ``StrategySlot`` — the engine
    config only carries parameters that apply to the loop itself (cycle
    cadence, market-hours gating, ATR length, etc.).
    """

    history_lookback_days: int = settings.ENGINE_HISTORY_LOOKBACK_DAYS
    cycle_interval_seconds: float = settings.ENGINE_CYCLE_INTERVAL_SECONDS
    max_bar_age_multiplier: float = settings.ENGINE_MAX_BAR_AGE_MULTIPLIER
    market_hours_only: bool = settings.ENGINE_MARKET_HOURS_ONLY
    cancel_orders_on_shutdown: bool = settings.ENGINE_CANCEL_ORDERS_ON_SHUTDOWN
    atr_length: int = settings.ATR_LENGTH
    external_close_confirm_cycles: int = settings.ENGINE_EXTERNAL_CLOSE_CONFIRM_CYCLES
    option_trailing_quote_max_age_seconds: float = (
        settings.OPTION_TRAILING_QUOTE_MAX_AGE_SECONDS
    )
    option_trailing_max_spread_pct: float = settings.OPTION_TRAILING_MAX_SPREAD_PCT
    option_trailing_stop_bid_buffer_pct: float = (
        settings.OPTION_TRAILING_STOP_BID_BUFFER_PCT
    )
    option_stop_replace_audit_enabled: bool = (
        settings.OPTION_STOP_REPLACE_AUDIT_ENABLED
    )
    option_stop_replace_audit_strategy: str = (
        settings.OPTION_STOP_REPLACE_AUDIT_STRATEGY
    )
    option_stop_replace_audit_db_path: str = (
        settings.OPTION_STOP_REPLACE_AUDIT_DB_PATH
    )
    option_stop_replace_audit_window_seconds: float = (
        settings.OPTION_STOP_REPLACE_AUDIT_WINDOW_SECONDS
    )
    option_stop_replace_audit_retention_days: int = (
        settings.OPTION_STOP_REPLACE_AUDIT_RETENTION_DAYS
    )

    def __post_init__(self) -> None:
        if self.cycle_interval_seconds <= 0:
            raise ValueError("cycle_interval_seconds must be > 0")
        if self.max_bar_age_multiplier <= 1:
            raise ValueError("max_bar_age_multiplier must be > 1")
        if self.atr_length < 1:
            raise ValueError("atr_length must be >= 1")
        if self.history_lookback_days < 1:
            raise ValueError("history_lookback_days must be >= 1")
        if self.external_close_confirm_cycles < 1:
            raise ValueError("external_close_confirm_cycles must be >= 1")
        if self.option_trailing_quote_max_age_seconds <= 0:
            raise ValueError("option_trailing_quote_max_age_seconds must be > 0")
        if not 0 <= self.option_trailing_max_spread_pct < 1:
            raise ValueError("option_trailing_max_spread_pct must be in [0, 1)")
        if not 0 <= self.option_trailing_stop_bid_buffer_pct < 1:
            raise ValueError("option_trailing_stop_bid_buffer_pct must be in [0, 1)")
        if (
            self.option_stop_replace_audit_enabled
            and self.option_stop_replace_audit_window_seconds <= 0
        ):
            raise ValueError("option_stop_replace_audit_window_seconds must be > 0")
        if (
            self.option_stop_replace_audit_enabled
            and self.option_stop_replace_audit_retention_days <= 0
        ):
            raise ValueError("option_stop_replace_audit_retention_days must be > 0")


# P-7: SuspectExitOrder dataclass removed. Substrate exit rows
# (created by close_position via _lifecycle_orders_record_exit)
# now carry the same recovery state — order_id, symbol, owner_key
# (via JOIN to position_lifecycle), intended_qty, slippage benchmark
# provenance. _maybe_dispatch_substrate_exit_fill is the recovery
# trigger.


# ── Engine ───────────────────────────────────────────────────────────────────


class TradingEngine:
    """
    The main loop. Supports one or many strategy slots, all sharing the
    same risk manager and broker (one account, one equity pool).

    Each slot binds a strategy to a symbol list (and optionally a scanner).
    Per cycle the engine iterates over slots → symbols, generating signals
    and routing through risk → execution.
    """

    def __init__(
        self,
        *,
        risk: RiskManager,
        broker: AlpacaBroker,
        slots: list[StrategySlot] | None = None,
        # Legacy single-strategy API — wraps into one slot.
        strategy: BaseStrategy | None = None,
        symbols: list[str] | None = None,
        config: EngineConfig | None = None,
        trade_logger: TradeLogger | None = None,
        pnl_tracker: PnLTracker | None = None,
        alerts: AlertDispatcher | None = None,
        stream_manager: "StreamManager | None" = None,
        regime_detector: "RegimeDetector | None" = None,
        allocator: "SleeveAllocator | None" = None,
        sector_resolver: "SectorResolver | None" = None,
        # Injection seam for tests — production should leave this as None.
        clock: callable = None,  # type: ignore[assignment]
    ) -> None:
        self.config = config or EngineConfig()

        # Build the slot list.
        if slots is not None:
            self.slots = list(slots)
        elif strategy is not None:
            # Backward-compat: single strategy → one slot.
            self.slots = [
                StrategySlot(
                    strategy=strategy,
                    symbols=symbols or list(settings.WATCHLIST),
                )
            ]
        else:
            raise ValueError("provide either 'slots' or 'strategy'")

        if not self.slots:
            raise ValueError("at least one StrategySlot is required")

        # Legacy accessor — points to the first slot's strategy for code
        # that still references engine.strategy (e.g. startup log, close log).
        self.strategy = self.slots[0].strategy

        self.risk = risk
        self.broker = broker
        # Every engine-owned AlpacaBroker gets the same last-mile entry guard.
        # bind_entry_guard preserves any stricter callback supplied by the
        # caller, while making the safety net independent of entrypoint wiring.
        # Phase B extends the guard to also reject when pause-entries is
        # active. Per-strategy pause stays engine-only (the broker doesn't
        # see strategy context at submit time); pause-entries IS visible
        # at this boundary and adds a second defense-in-depth check.
        self.broker.bind_entry_guard(
            lambda: not self.risk.is_halted() and not self.risk.is_entries_paused()
        )
        self.trade_logger = trade_logger or TradeLogger()
        # Operator Controls Phase A — wire the lifecycle store to the
        # broker so equity entry paths persist `position_uid` pending →
        # open transitions. Best-effort: if the broker already has a
        # store wired (e.g. tests injecting one), don't overwrite.
        # Sharing the TradeLogger's connection keeps all schema in one
        # DB and ensures _ensure_db() has run before the store is used.
        try:
            from engine.lifecycle import PositionLifecycleStore
            self.lifecycle_store = PositionLifecycleStore(
                self.trade_logger._ensure_db()
            )
            if (
                hasattr(self.broker, "_lifecycle_store")
                and getattr(self.broker, "_lifecycle_store", None) is None
            ):
                self.broker._lifecycle_store = self.lifecycle_store
        except Exception as exc:
            logger.warning(f"lifecycle store init skipped: {exc}")
            self.lifecycle_store = None
        # Foundation commit 6 — per-order substrate store. Same DB connection
        # so the FK from position_lifecycle_orders.position_uid →
        # position_lifecycle.position_uid resolves locally. Best-effort: if
        # init fails the broker simply won't insert per-order rows and the
        # engine falls back to the position-level lifecycle exclusively.
        try:
            from engine.lifecycle_orders import PositionLifecycleOrdersStore
            self.lifecycle_orders_store = PositionLifecycleOrdersStore(
                self.trade_logger._ensure_db()
            )
            if (
                hasattr(self.broker, "_lifecycle_orders_store")
                and getattr(self.broker, "_lifecycle_orders_store", None) is None
            ):
                self.broker._lifecycle_orders_store = self.lifecycle_orders_store
        except Exception as exc:
            logger.warning(f"lifecycle_orders store init skipped: {exc}")
            self.lifecycle_orders_store = None
        try:
            self.option_trailing_store = OptionTrailingStopStore(
                self.trade_logger._ensure_db()
            )
        except Exception as exc:
            logger.warning(f"option trailing store init skipped: {exc}")
            self.option_trailing_store = None
        self.option_stop_audit_store = None
        self._option_stop_audit_last_pruned_at: datetime | None = None
        if self.config.option_stop_replace_audit_enabled:
            try:
                from engine.option_stop_audit import OptionStopReplaceAuditStore
                self.option_stop_audit_store = OptionStopReplaceAuditStore(
                    self.config.option_stop_replace_audit_db_path
                )
                cutoff = datetime.now(timezone.utc) - timedelta(
                    days=self.config.option_stop_replace_audit_retention_days
                )
                pruned = self.option_stop_audit_store.prune_before(cutoff)
                self._option_stop_audit_last_pruned_at = datetime.now(
                    timezone.utc
                )
                logger.info(
                    "temporary option-stop replacement audit enabled for "
                    f"strategy={self.config.option_stop_replace_audit_strategy} "
                    f"db={self.config.option_stop_replace_audit_db_path} "
                    f"retention={self.config.option_stop_replace_audit_retention_days}d"
                    + (f" pruned={pruned}" if pruned else "")
                )
            except Exception as exc:
                logger.warning(f"option stop audit store init skipped: {exc}")
                self.option_stop_audit_store = None
        # Operator Controls Phase A PR-2 — operator command queue store.
        # PR-65 review F3 (Phase B): the store gets its OWN sqlite3
        # connection (separate from TradeLogger's) so cross-thread
        # access from the heartbeat thread + Telegram listener cannot
        # share a transaction with cycle-thread trades/lifecycle writes.
        # Two distinct connections to the same DB file are coordinated
        # by SQLite's file-level locking; the store's own threading
        # RLock serialises its multi-thread callers internally.
        try:
            import sqlite3 as _sqlite3
            from engine.operator_queue import OperatorCommandStore
            # Ensure schema is materialised before opening the
            # dedicated connection (TradeLogger._ensure_db runs the
            # full migration once).
            self.trade_logger._ensure_db()
            op_conn = _sqlite3.connect(
                self.trade_logger._path, check_same_thread=False
            )
            op_conn.execute("PRAGMA foreign_keys = ON")
            # PR-65 R2 (Q): busy_timeout asks SQLite to retry briefly
            # on `SQLITE_BUSY` if the trade-logger connection holds
            # the database lock when the heartbeat tries to claim a
            # row. 5s is well above any real lock duration (cycle
            # writes are sub-millisecond) and well under the heartbeat
            # interval, so contention surfaces as a transparent retry
            # rather than as a logged "queue claim failed" warning.
            op_conn.execute("PRAGMA busy_timeout = 5000")
            self._operator_command_conn = op_conn
            self.operator_command_store = OperatorCommandStore(op_conn)
        except Exception as exc:
            logger.warning(f"operator command store init skipped: {exc}")
            self._operator_command_conn = None
            self.operator_command_store = None
        # Operator Controls Phase C — symbol-lock registry.
        # In-memory mutex over owner_key strings; wraps the foundation's
        # DB-level uniqueness constraints (uniq_one_active_position_per_owner_key
        # + uniq_one_active_close_per_position) with fast failure +
        # audit context. Engine-process-local; restart drops everything
        # (a Phase C command crossing restart resumes via the
        # operator_commands queue's executing/indeterminate state, not
        # through this registry). See proposal §11.1.
        from engine.symbol_locks import SymbolLockRegistry
        self.symbol_locks = SymbolLockRegistry()
        self.pnl_tracker = pnl_tracker or PnLTracker()
        self.alerts = alerts or AlertDispatcher()
        self._stream_manager = stream_manager
        self._regime_detector = regime_detector
        self._allocator = allocator
        self._sector_resolver = sector_resolver
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._running: bool = False
        self._session_start_equity: float | None = None
        self._cycle_count: int = 0
        self._last_cycle_end: float = 0.0  # monotonic timestamp
        # Operator Controls Phase B — fast operator-command heartbeat.
        # Daemon thread polls the operator_commands queue every
        # OPERATOR_COMMAND_HEARTBEAT_SECONDS. Started in `start()`,
        # stopped by setting `_running=False` (the thread checks the flag
        # in a sleep loop). Daemon=True so a stuck join during shutdown
        # cannot prevent process exit.
        self._operator_heartbeat_thread: threading.Thread | None = None
        self._operator_heartbeat_stop = threading.Event()
        self._last_regime: str | None = None
        self._regime_fail_count: int = 0
        self._last_cycle_equity: float | None = None
        self._last_snapshot: "BrokerSnapshot | None" = None
        self._last_stream_healthy: bool | None = None

        # PLAN 11.10f — Strategy Health lifecycle counter accumulator.
        # Per-cycle counts are accumulated here in a local dict and
        # flushed ONCE at end of cycle via lifecycle.upsert_counters
        # — 1 DB write per (strategy, week-bucket), not 7 per symbol.
        # Reset at start of every cycle so each cycle's counts are
        # independent before flush. Gated by settings.HEALTH_COUNTERS_ENABLED
        # — flag off = empty dict, no upsert, zero engine surface area.
        # See design §12.4.1 for the contract: observability only,
        # never affects trading decisions.
        from strategies.health.lifecycle import LifecycleCounters as _LC  # noqa
        self._cycle_lifecycle_counters: dict[str, _LC] = {}

        # Position ownership: position_id → Position. Tracks which strategy
        # opened each position so that exit signals from a *different*
        # strategy watching the same symbol don't close someone else's trade.
        # For single-leg positions the position_id == owner_key_for(symbol)
        # (equity ticker, or option underlying for OCC). PR 1 part 2 only
        # populates single-leg entries; spreads land with the credit-spread
        # strategy in 11.28/11.29.
        self._positions: dict[str, Position] = {}

        # Credit-spread positions (11.29 PR 3b): position_id → the owning
        # CreditSpread strategy instance. Multiple credit_spread instances
        # share one allocator sleeve but each manages its own underlying, so
        # the spread-fill drain and exit paths need to route a position_id
        # back to the instance that opened it.
        self._spread_owner_strategy: dict[str, BaseStrategy] = {}
        # position_id → SpreadExecutionPlan for spreads pending their async
        # combo fill — lets _drain_spread_fills finalize or roll back.
        self._pending_spread_plans: dict[str, object] = {}
        # position_ids with a closing combo in flight — skipped by the exit
        # path so a stale "should exit" signal cannot double-submit a close.
        self._spreads_pending_close: set[str] = set()

        # Entry fill prices: symbol → avg fill price at entry ($/share).
        # Used to compute realized P&L when a position closes, which is fed
        # into SleeveAllocator.record_realized_pnl() for the HWM drawdown gate.
        self._entry_prices: dict[str, float] = {}

        # Consecutive-cycle absence counter for external-close confirmation.
        # symbol → number of consecutive cycles absent from broker positions.
        # Only after external_close_confirm_cycles consecutive misses do we
        # treat the position as genuinely gone (guards against API blips).
        self._external_close_suspects: dict[str, int] = {}

        # P-6 + P-7: _suspect_orders and _suspect_exit_orders both
        # removed. The substrate capture pipeline (P-1 stream + P-2
        # cycle reconcile + P-3 startup reconcile) observes UNKNOWN-
        # at-submit resolutions and _maybe_dispatch_substrate_
        # {entry,exit}_fill fires the engine-side side effects on
        # each captured fill.

        # DAY-stop promotion is retried from every broker snapshot, but a
        # persistent rejection should count as one broker incident rather than
        # tripping the rolling broker-error halt on the same order each cycle.
        self._reported_stop_promotion_failures: set[tuple[str, str]] = set()

        # Startup mode set by _reconcile_startup. NORMAL → full trading.
        # RESTRICTED → exits only (one cycle, then auto-clears to NORMAL).
        # HALT → no new entries until manual reset_kill_switches().
        self._startup_mode: str = "NORMAL"

        # Latest per-strategy watchlist statuses for dashboard display.
        # strategy_name -> {symbol -> status}
        self._watchlist_statuses: dict[str, dict[str, str]] = {}
        self._watchlist_reasons: dict[str, dict[str, list[str]]] = {}
        self._sector_heat: dict | None = None
        self._last_underlying_prices: dict[str, float] = {}

        # Sector exposure observability (11.7 Part B). Maps normalized sector
        # key → count of open equity positions in that sector. OCC option
        # symbols and unmapped tickers are excluded. Recomputed each cycle
        # in _write_state_snapshot; INFO-logged on change. No auto-block.
        self._last_sector_exposure: dict[str, int] = {}

        # Daily decision gate: (strategy_name, symbol, timeframe) → completed-bar
        # timestamp already evaluated this session. This prevents the 5-minute
        # loop from reprocessing the same daily signal bar all day long.
        self._processed_signal_bars: dict[tuple[str, str, str], pd.Timestamp] = {}
        self._processed_signal_statuses: dict[tuple[str, str, str], str] = {}
        self._processed_signal_reasons: dict[tuple[str, str, str], list[str]] = {}

        # Rolling rejection timestamps for HealthAssessor (24h windowed counts
        # exposed via engine_state.json). PLAN 11.44:
        #   symbol_conflicts  — equity-level cross-strategy collisions
        #                       (e.g. two equity strategies trying to own AAPL).
        #   contract_conflicts — leg-level cross-strategy collisions on the
        #                       exact OCC contract (single-leg vs single-leg,
        #                       single-leg vs MLEG leg, or MLEG leg vs MLEG leg).
        # Two separate buckets because the remediation differs: symbol conflicts
        # are usually a slot-config overlap; contract conflicts indicate two
        # options pickers landing on the same strike/expiry and would corrupt
        # ownership tracking if not blocked (positions aggregate at the broker
        # by exact symbol).
        self._symbol_conflicts: list[datetime] = []
        self._contract_conflicts: list[datetime] = []

    # ── Position bookkeeping helpers (PR 11.27) ──────────────────────────

    def _owners_view(self) -> dict[str, str]:
        """
        Legacy ``dict[position_id, strategy_name]`` view of ``_positions``.

        Used at the boundary with ``SleeveAllocator`` (which still consumes
        the flat-dict shape) and in the state snapshot's ``open_positions``
        field that the dashboard reads.
        """
        return view_owner_map(self._positions.values())

    def _get_owner(self, symbol: str) -> str | None:
        """Return owning strategy_name for a broker symbol (OCC-aware), or None."""
        pos = self._positions.get(owner_key_for(symbol))
        return pos.strategy_name if pos else None

    def _has_position(self, symbol: str) -> bool:
        """True if a Position is tracked for ``symbol`` (OCC-normalized)."""
        return owner_key_for(symbol) in self._positions

    def _register_single_leg(
        self,
        *,
        strategy_name: str,
        symbol: str,
    ) -> Position:
        """Create + store a single-leg Position keyed by its owner_key.

        Idempotent: if a Position already exists for this owner_key,
        returns the existing record (does not overwrite the strategy)."""
        key = owner_key_for(symbol)
        existing = self._positions.get(key)
        if existing is not None:
            return existing
        pos = make_single_leg(strategy_name=strategy_name, symbol=symbol)
        self._positions[pos.position_id] = pos
        return pos

    def _pop_position(self, symbol: str) -> str | None:
        """Remove the Position for ``symbol`` (OCC-aware). Return strategy_name."""
        pos = self._positions.pop(owner_key_for(symbol), None)
        return pos.strategy_name if pos else None

    # ── Contract-level conflict (PLAN 11.44) ─────────────────────────────

    def _contract_owner(self, occ: str) -> tuple[str, str] | None:
        """
        Return ``(strategy_name, position_id)`` for any tracked position that
        already holds ``occ`` as a leg, else ``None``.

        Leg-level, strategy-agnostic scan — applies equally to single-leg
        option positions and to any leg of any multi-leg position. Used by
        the dispatch-time contract-conflict guard.

        Two strategies cannot independently hold the same OCC because the
        broker aggregates positions by exact symbol: combined qty under a
        single cost basis means the engine's per-strategy ownership map
        physically cannot represent shared ownership. Long-vs-short
        compounds the hazard — positions net at the broker and one
        strategy's exit could silently flip the other into the wrong side.
        """
        for pos in self._positions.values():
            for leg in pos.legs:
                if leg.symbol == occ:
                    return (pos.strategy_name, pos.position_id)
        return None

    def _reject_if_contract_conflict(
        self,
        *,
        strategy_name: str,
        symbol: str,
        occs: "list[str]",
    ) -> tuple[str, str] | None:
        """
        Check every OCC in ``occs`` against ``_contract_owner``. If any leg is
        already owned by a different strategy, fire the ``CONTRACT_CONFLICT``
        alert, increment the rolling counter, and return the first colliding
        ``(other_strategy_name, occ)`` pair. Returns ``None`` when clear.

        Side-effect on alerts/counters is intentional — keeps the two dispatch
        paths (single-leg options and MLEG) symmetric and free of duplicated
        rejection plumbing.
        """
        for occ in occs:
            owner = self._contract_owner(occ)
            if owner is not None and owner[0] != strategy_name:
                other_strategy, _ = owner
                reason = (
                    f"contract {occ} already owned by '{other_strategy}'"
                )
                logger.info(
                    f"[{strategy_name}] {symbol}: entry blocked — {reason}"
                )
                self.alerts.order_rejection(
                    symbol, strategy_name, reason, "CONTRACT_CONFLICT"
                )
                self._contract_conflicts.append(datetime.now(timezone.utc))
                return (other_strategy, occ)
        return None

    @staticmethod
    def _prune_window(
        timestamps: "list[datetime]", *, window: timedelta
    ) -> int:
        """In-place prune of ``timestamps`` older than ``window``. Returns the
        remaining count.

        Tiny helper shared by the conflict counters — keeps the list bounded
        (entries are short-lived; in practice at most a handful per day) and
        gives the snapshot writer the windowed count without a second pass.
        """
        cutoff = datetime.now(timezone.utc) - window
        # Slice from the first index whose timestamp is still within the window.
        keep_from = 0
        for ts in timestamps:
            if ts >= cutoff:
                break
            keep_from += 1
        if keep_from:
            del timestamps[:keep_from]
        return len(timestamps)

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(
        self,
        *,
        max_cycles: int | None = None,
        post_cycle_hook: "Callable[[], None] | None" = None,
    ) -> None:
        """
        Run the loop until SIGINT, `stop()`, or `max_cycles` (if set).
        `max_cycles` is for tests / verify scripts; production calls leave
        it None and rely on signal-driven shutdown.

        `post_cycle_hook` (PLAN 11.10g) is an optional callable invoked
        after each completed cycle. The engine doesn't know what the
        callback does — it's the integration point for forward_test.py's
        Monday-completed-week + first-of-month monthly health-review
        scheduler. The hook is wrapped in try/except so a hook failure
        cannot crash the trading loop (same hard rule as the lifecycle
        counter flush — design §12.4.1 invariant).
        """
        self._install_signal_handlers()
        self._running = True
        self._cycle_count = 0

        # Start WebSocket stream before the first broker snapshot so that
        # fills from positions placed immediately after startup are not missed.
        if self._stream_manager is not None:
            self._stream_manager.start()

        # Capture truth-of-the-world before any decision.
        startup_snapshot = self.broker.sync_with_broker()
        self._session_start_equity = startup_snapshot.account.equity
        self._last_snapshot = startup_snapshot

        # Recover broker-proven exits before ownership restoration. A position
        # that filled while the process was down is absent from the snapshot,
        # so it would never enter _positions and the normal cycle-level
        # external-close detector could not reconcile its stale DB row.
        self._reconcile_vanished_db_positions(startup_snapshot)

        all_symbols = []
        for slot in self.slots:
            all_symbols.extend(slot.active_symbols())
        unique_symbols = sorted(set(all_symbols))

        # Restore position ownership from the trade DB (10.C1) and determine
        # startup mode (10.C2). This replaces the old best-effort slot-match.
        conflict_symbols = self._restore_ownership_from_db(startup_snapshot)
        self._restore_runtime_state_from_db(startup_snapshot)
        self._startup_mode = self._reconcile_startup(
            startup_snapshot, conflict_symbols
        )

        # Operator Controls Phase A — reconcile lifecycle table with
        # broker reality after startup. Forward direction: synthesize
        # lifecycle rows for broker-open positions with no row yet (so
        # the operator CLI can see and act on them). Reverse direction:
        # mark lifecycle rows closed if their owner_key no longer has a
        # broker position (catches overnight stop fills, external
        # closes, etc.). Best-effort: never raises into the cycle path.
        self._reconcile_position_lifecycle(startup_snapshot)

        # P-3: per-order substrate startup reconciliation. Walk every
        # non-terminal position_lifecycle_orders row whose order_id
        # is NOT in the broker's current open_orders, fetch its
        # current state via REST, and apply via apply_order_event.
        # Catches fill / cancel / expiration events that landed at
        # the broker while the bot was down (overnight, weekend,
        # crash window). Runs after _reconcile_position_lifecycle so
        # the position_uid FKs already resolve. Best-effort: each
        # row's failure is isolated and logged CRITICAL.
        try:
            self._reconcile_substrate_startup(startup_snapshot)
        except Exception as exc:
            logger.critical(
                f"substrate startup reconcile raised: "
                f"{type(exc).__name__}: {exc}. Cycle path will retry "
                f"non-terminal rows over time via the cycle "
                f"reconciler."
            )
        # §10.7 spread close reconciler — closes the restart-gap.
        try:
            self._reconcile_substrate_spread_closes(
                startup_snapshot, reason="startup",
            )
        except Exception as exc:
            logger.critical(
                f"spread close startup reconcile raised: "
                f"{type(exc).__name__}: {exc}. Cycle path will retry "
                f"non-terminal rows over time via the cycle "
                f"reconciler."
            )

        # Operator Controls Phase A PR-2 — restore sticky halt from disk.
        # If a halt was engaged before the previous shutdown, re-engage
        # the kill switch immediately so the first cycle blocks entries.
        # Persisted state lives outside the SQLite DB so corruption in
        # one file does not lock out the other.
        # Phase B extends this to also restore pause-entries and
        # pause-strategy flags from the same file.
        self._restore_control_state()

        # Operator Controls Phase B — start the fast heartbeat that
        # drains the operator queue every OPERATOR_COMMAND_HEARTBEAT_SECONDS.
        # Independent of the trading cycle so commands drain regardless
        # of market state and at sub-second latency. Best-effort: if
        # initialisation fails the trading loop still runs (commands
        # would fall back to expiry).
        self._start_operator_heartbeat()

        self._sync_managed_stop_legs(startup_snapshot)
        self._sync_option_trailing_stops(startup_snapshot)
        self._repair_missing_protective_stops(startup_snapshot)

        slot_desc = ", ".join(
            f"{s.strategy.name}({len(s.active_symbols())})"
            for s in self.slots
        )
        logger.info(
            f"engine starting: {len(self.slots)} slot(s) [{slot_desc}], "
            f"{len(unique_symbols)} unique symbol(s), "
            f"session_start_equity=${self._session_start_equity:,.2f}, "
            f"open_positions={len(startup_snapshot.account.open_positions)}, "
            f"open_orders={len(startup_snapshot.open_orders)}"
        )

        try:
            while self._running:
                self._cycle_count += 1
                self._run_one_cycle()
                # PLAN 11.10g: optional per-cycle hook (forward_test.py
                # wires the Monday-completed-week + first-of-month
                # health-review scheduler here).
                # Wrapped in try/except so a hook failure never crashes
                # the trading loop — same hard rule as
                # _flush_lifecycle_counters.
                if post_cycle_hook is not None:
                    try:
                        post_cycle_hook()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            f"post_cycle_hook failed (trading not "
                            f"affected): {exc}"
                        )
                if max_cycles is not None and self._cycle_count >= max_cycles:
                    logger.info(f"reached max_cycles={max_cycles}, stopping")
                    break
                if self._running:
                    self._sleep(self.config.cycle_interval_seconds)
        finally:
            self._shutdown()

    def stop(self) -> None:
        """Signal the loop to exit at the next safe point."""
        if self._running:
            logger.info("engine stop requested")
        self._running = False
        # Heartbeat shutdown is idempotent and also re-run in
        # `_shutdown()` (the `finally:` after `start()`). Calling it
        # here gives operators an immediate stop signal even if the
        # cycle thread is mid-sleep; the finally clause is the
        # guarantee that covers max_cycles / exception / restart-loop
        # paths where `stop()` was never explicitly called.
        # (PR-65 review F2.)
        self._stop_operator_heartbeat()

    def _stop_operator_heartbeat(self) -> None:
        """Idempotent heartbeat shutdown. Wakes the sleeper, joins with
        a bounded timeout, and clears the thread handle. Safe to call
        multiple times.

        Called from:
          - `stop()` for explicit operator/SIGINT shutdown
          - `_shutdown()` finally block for max_cycles / exception /
            restart-loop wrapping
          - Tests that need deterministic cleanup
        """
        # Wake the sleeper so it observes the stop flag immediately
        # rather than waiting up to HEARTBEAT_SECONDS.
        self._operator_heartbeat_stop.set()
        thread = self._operator_heartbeat_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
            if thread.is_alive():
                logger.warning(
                    "operator heartbeat thread did not exit within 2s; "
                    "daemon=True ensures it won't block process exit"
                )
        self._operator_heartbeat_thread = None

    # ── Per-cycle pipeline ───────────────────────────────────────────────

    def _run_one_cycle(self) -> None:
        """
        One full sweep across all strategy slots and their symbols. Wraps
        the whole cycle in a try/except so one bad cycle never crashes the loop.
        """
        cycle_id = self._cycle_count
        now_mono = time.monotonic()
        cycle_started_mono = now_mono
        total_symbols = sum(len(slot.active_symbols()) for slot in self.slots)
        processed_symbols = 0
        new_positions = 0
        error_count = 0
        cycle_status = "ok"

        # PLAN 11.10f: reset the per-cycle lifecycle counter accumulator
        # at the start of each cycle. Flushed via _flush_lifecycle_counters
        # at end-of-cycle (in the finally block). Gated by the feature flag
        # so flag=False keeps the dict empty and the flush no-ops.
        self._cycle_lifecycle_counters = {}

        # Detect sleep gaps — if wall-clock time since the last cycle end is
        # much larger than the configured interval, the machine likely slept.
        if self._last_cycle_end > 0:
            gap = now_mono - self._last_cycle_end
            expected = self.config.cycle_interval_seconds
            if gap > expected * 3 and gap > 60:
                missed = int(gap / expected) - 1
                logger.warning(
                    f"sleep gap detected: {gap:.0f}s elapsed since last cycle "
                    f"(expected ~{expected:.0f}s), ~{missed} cycle(s) missed"
                )
                self.alerts.engine_halt(
                    f"sleep gap: {gap:.0f}s, ~{missed} cycles missed"
                )

        try:
            market_state = "not_checked"
            if self.config.market_hours_only:
                market_open = self._market_open()
                market_state = "open" if market_open else "closed"
            else:
                market_open = True
                market_state = "not_enforced"

            logger.info(
                f"cycle {cycle_id} start: "
                f"market={market_state}, symbols={total_symbols}, "
                f"slots={len(self.slots)}"
            )

            # Operator Controls Phase B — queue polling moved to the
            # `_operator_heartbeat_thread` daemon (started in `start()`),
            # which drains commands every OPERATOR_COMMAND_HEARTBEAT_SECONDS
            # independent of the trading cycle. The earlier per-cycle
            # poll (Phase A PR-2 F1 fix) is no longer required because
            # the heartbeat runs regardless of market state and at
            # sub-second drain latency.

            if not market_open:
                cycle_status = "market_closed"
                try:
                    snapshot = self.broker.sync_with_broker(
                        session_start_equity=self._session_start_equity
                    )
                    self._last_cycle_equity = snapshot.account.equity
                    self._last_snapshot = snapshot
                    if self._regime_detector is not None:
                        try:
                            current_regime = self._regime_detector.detect()
                            regime_str = current_regime.value
                            if (
                                self._last_regime is not None
                                and regime_str != self._last_regime
                            ):
                                self.alerts.regime_shift(self._last_regime, regime_str)
                            self._last_regime = regime_str
                        except Exception as exc:
                            logger.warning(
                                f"market-closed regime detection failed: {exc} — "
                                "keeping last known regime"
                            )
                    order_strategy = self._attribute_orders(snapshot.open_orders)
                    self._sync_managed_stop_legs(snapshot)
                    self._drain_option_stop_audit_events()
                    # P-7: _recover_suspect_exit_orders removed.
                    # Substrate pipeline + _maybe_dispatch_substrate_
                    # exit_fill cover the close-side recovery.
                    self._repair_missing_protective_stops(
                        snapshot,
                        allow_residual_cleanup=False,
                    )
                    self._refresh_watchlist_statuses(
                        snapshot,
                        order_strategy=order_strategy,
                        preserve_existing=True,
                    )
                except Exception as e:
                    logger.warning(
                        f"market-closed snapshot refresh failed: {e} — "
                        "keeping last known watchlist statuses"
                    )
                logger.info(f"cycle {cycle_id} skipped: market closed")
                return

            try:
                snapshot = self.broker.sync_with_broker(
                    session_start_equity=self._session_start_equity
                )
                self._last_cycle_equity = snapshot.account.equity
                self._last_snapshot = snapshot
            except Exception as e:
                cycle_status = "sync_failed"
                logger.error(f"sync_with_broker failed: {e}; skipping cycle")
                self.risk.record_broker_error()
                self.alerts.broker_error(str(e))
                return

            self.risk.evaluate_account(snapshot.account)
            risk_state = self.risk.halt_reason() or "healthy"
            logger.info(
                f"cycle {cycle_id} broker state: "
                f"positions={len(snapshot.account.open_positions)}, "
                f"open_orders={len(snapshot.open_orders)}, "
                f"risk={risk_state}"
            )

            self._sync_managed_stop_legs(snapshot)
            self._observe_stream_health()
            self._drain_option_stop_audit_events()
            # P-6 + P-7: _recover_suspect_orders and
            # _recover_suspect_exit_orders both removed. Substrate
            # pipeline (P-1/P-2/P-3) observes UNKNOWN-at-submit
            # resolutions on both entry and exit roles; the
            # _maybe_dispatch_substrate_{entry,exit}_fill helpers
            # fire the engine-side side effects.
            self._process_stream_stop_fills(snapshot)
            self._detect_external_closes(snapshot)
            self._drain_lifecycle_attaches()
            self._drain_lifecycle_events(snapshot)
            self._reconcile_substrate_cycle(snapshot)
            # §10.7: spread close reconciler — runs every cycle so
            # rows whose broker order became terminal while the
            # stream was disconnected get resolved without waiting
            # for next startup.
            self._reconcile_substrate_spread_closes(
                snapshot, reason="cycle",
            )
            self._drain_option_fills()
            self._drain_spread_fills()
            self._sync_option_trailing_stops(snapshot)
            self._repair_missing_protective_stops(snapshot)
            # Operator command poll moved earlier in the cycle so it
            # runs even when the market is closed — see the F1-fix
            # block before the `if not market_open` branch.

            # Daily-loss / hard-dollar gates can fire on any signal; halt state
            # is sticky until manual reset, so we don't need to short-circuit
            # here — every per-symbol evaluate() call respects it.
            if self.risk.is_halted():
                reason = self.risk.halt_reason() or "unknown"
                logger.warning(
                    f"risk halted ({reason}) — no new entries "
                    "this cycle, but continuing to monitor"
                )
                self.alerts.engine_halt(reason)

            # Maintain a running account that updates after each intra-cycle fill so
            # the gross-exposure and max-positions caps see positions opened earlier
            # in the same cycle — not just what the broker reported at cycle start.
            running_account = snapshot.account

            # Regime detection — runs once per cycle, before any slot.
            # Exits are never blocked by regime; only new entries are gated.
            current_regime = None
            if self._regime_detector is not None:
                try:
                    current_regime = self._regime_detector.detect()
                    regime_str = current_regime.value
                    if (
                        self._last_regime is not None
                        and regime_str != self._last_regime
                    ):
                        self.alerts.regime_shift(self._last_regime, regime_str)
                    self._last_regime = regime_str
                    self._regime_fail_count = 0
                except Exception as exc:
                    self._regime_fail_count += 1
                    max_failures = settings.REGIME_MAX_CONSECUTIVE_FAILURES
                    if self._regime_fail_count >= max_failures:
                        logger.error(
                            f"regime detection failed {self._regime_fail_count} "
                            f"consecutive times: {exc} — falling back to BEAR "
                            "(fail-closed)"
                        )
                        current_regime = MarketRegime.BEAR
                    elif self._last_regime is not None:
                        logger.warning(
                            f"regime detection failed "
                            f"({self._regime_fail_count}x): {exc} — using "
                            f"last known regime: {self._last_regime}"
                        )
                        current_regime = MarketRegime(self._last_regime)
                    else:
                        logger.warning(
                            f"regime detection failed "
                            f"({self._regime_fail_count}x), no prior regime "
                            "— defaulting to RANGING"
                        )
                        current_regime = MarketRegime.RANGING

            # BEAR defensive sweep — runs at cycle level so the override is
            # never gated by per-symbol bar fetch failures, stale-data
            # rejections, or empty decision frames. The slot loop below
            # may still try the BEAR override per-symbol, but those calls
            # become no-ops via _spreads_pending_close after the sweep
            # already dispatched the close.
            if current_regime is MarketRegime.BEAR:
                self._sweep_bear_spread_exits()

            # Order attribution — computed once per cycle for sleeve accounting.
            # Maps order_id → strategy_name for pending buy entries using
            # watchlist membership. Used by SleeveAllocator to count open
            # orders against the correct strategy's sleeve budget.
            order_strategy = self._attribute_orders(snapshot.open_orders)
            
            # Cancel any entry limit orders that have exceeded the STALE_LIMIT_MAX_AGE_SECONDS
            self._cleanup_stale_orders(snapshot, order_strategy)


            self._watchlist_statuses = {}
            self._watchlist_reasons = {}
            for slot in self._slots_by_priority():
                # Per-slot regime gate: block new entries if current regime is
                # not in the slot's allowed set. Exits always proceed.
                entry_allowed = True
                if current_regime is not None and slot.allowed_regimes is not None:
                    if current_regime not in slot.allowed_regimes:
                        entry_allowed = False
                        logger.info(
                            f"[{slot.strategy.name}] regime={current_regime.value} "
                            f"not in allowed_regimes="
                            f"{sorted(r.value for r in slot.allowed_regimes)} "
                            "— new entries blocked this cycle"
                        )

                symbols = slot.active_symbols()
                strategy_statuses: dict[str, str] = {}
                strategy_reasons: dict[str, list[str]] = {}
                self._watchlist_statuses[slot.strategy.name] = strategy_statuses
                self._watchlist_reasons[slot.strategy.name] = strategy_reasons
                for symbol in symbols:
                    strategy_statuses[symbol] = self._baseline_watchlist_status(
                        symbol,
                        snapshot,
                        strategy_name=slot.strategy.name,
                        order_strategy=order_strategy,
                    )
                    strategy_reasons[symbol] = []
                    try:
                        processed_symbols += 1
                        filled = self._process_symbol(
                            symbol,
                            snapshot,
                            running_account,
                            slot.strategy,
                            slot.timeframe,
                            market_open=market_open,
                            entry_allowed=entry_allowed,
                            regime_block_reason=(
                                f"regime {current_regime.value} not in allowed set "
                                f"{sorted(r.value for r in slot.allowed_regimes)}"
                                if current_regime is not None and slot.allowed_regimes is not None and not entry_allowed
                                else None
                            ),
                            current_regime=current_regime,
                            order_strategy=order_strategy,
                            strategy_statuses=strategy_statuses,
                            strategy_reasons=strategy_reasons,
                        )
                        if filled is not None:
                            new_positions += 1
                            # Merge the new position into the running account so
                            # the next symbol's risk.evaluate() sees it.
                            updated_positions = {
                                **running_account.open_positions,
                                filled.symbol: filled,
                            }
                            running_account = AccountState(
                                equity=running_account.equity,
                                cash=running_account.cash - filled.market_value,
                                session_start_equity=running_account.session_start_equity,
                                previous_close_equity=running_account.previous_close_equity,
                                open_positions=updated_positions,
                            )
                    except Exception as e:
                        # Never let one symbol kill the cycle.
                        error_count += 1
                        cycle_status = "symbol_errors"
                        logger.exception(f"{symbol}: cycle step failed: {e}")
        finally:
            # RESTRICTED mode auto-clears after one cycle — anomalies were
            # logged at startup; a full clean cycle proves state is coherent.
            if self._startup_mode == "RESTRICTED":
                logger.info(
                    "startup_mode RESTRICTED → NORMAL "
                    "(cleared after first full cycle)"
                )
                self._startup_mode = "NORMAL"

            duration = time.monotonic() - cycle_started_mono
            logger.info(
                f"cycle {cycle_id} complete: status={cycle_status}, "
                f"processed={processed_symbols}/{total_symbols}, "
                f"new_positions={new_positions}, errors={error_count}, "
                f"duration={duration:.1f}s, "
                f"next_cycle_in={self.config.cycle_interval_seconds:.0f}s"
            )
            # PLAN 11.10f: flush per-cycle lifecycle counters via single
            # upsert per strategy. Wrapped in try/except: a write
            # failure logs WARNING and continues — must NEVER raise into
            # the trading loop (design §12.4.1 hard rule).
            self._flush_lifecycle_counters()
            self._write_state_snapshot()
            # Close idle HTTP connections so they don't go stale during the
            # inter-cycle sleep (5 min default).  Fresh connections are cheap.
            close_connections()
            self.broker.close_connections()
            self._last_cycle_end = time.monotonic()

    def _process_symbol(
        self,
        symbol: str,
        snapshot: BrokerSnapshot,
        account: AccountState,
        strategy: BaseStrategy,
        timeframe: str,
        *,
        market_open: bool | None = None,
        entry_allowed: bool = True,
        regime_block_reason: str | None = None,
        current_regime: MarketRegime | None = None,
        order_strategy: dict[str, str] | None = None,
        strategy_statuses: dict[str, str] | None = None,
        strategy_reasons: dict[str, list[str]] | None = None,
    ) -> Position | None:
        """
        The full per-symbol decision path. Returns a Position if an entry was
        filled this call (so the cycle loop can update its running AccountState),
        otherwise None. Any expected exception type (StaleDataError, etc.) is
        caught and logged at WARNING/ERROR; the outer `_run_one_cycle` catches
        anything unexpected.
        """
        # 1. Fetch bars — use enough lookback to satisfy the strategy.
        end = self._clock()
        lookback_days = _lookback_days(
            strategy.required_bars(), timeframe, self.config.history_lookback_days
        )
        start = end - timedelta(days=lookback_days)
        try:
            # Live engine path — use the bot's runtime data feed.
            from config.settings import ALPACA_DATA_FEED
            df, stats = fetch_symbol(
                symbol, start, end, timeframe=timeframe, feed=ALPACA_DATA_FEED
            )
        except Exception as e:
            logger.error(f"{symbol}: fetch failed: {e}")
            return
        if df.empty:
            logger.warning(f"{symbol}: fetch returned no bars")
            return

        # 2. Freshness gate.
        max_bar_age = _BAR_INTERVAL[timeframe] * self.config.max_bar_age_multiplier
        try:
            require_fresh(df, max_bar_age, symbol, now=self._clock())
        except StaleDataError as e:
            logger.warning(f"{symbol}: skipping — {e}")
            self.alerts.stale_data(symbol, str(e))
            return

        decision_df, using_prior_completed_bar = self._decision_frame(
            df, timeframe, market_open=market_open
        )
        if decision_df.empty:
            logger.info(
                f"[{strategy.name}] {symbol}: skipping — no completed {timeframe} bar available yet"
            )
            return

        signal_bar = pd.Timestamp(decision_df.index[-1])
        signal_key = (strategy.name, symbol, timeframe)
        signal_bar_already_processed = self._should_skip_processed_signal_bar(
            signal_key, signal_bar
        )

        # 3. Indicators (just ATR — strategy adds its own).
        df = add_atr(decision_df, self.config.atr_length)
        atr_col = f"atr_{self.config.atr_length}"
        latest_atr = float(df[atr_col].iloc[-1])
        latest_close = float(df["close"].iloc[-1])
        latest_ts = df.index[-1]
        self._last_underlying_prices[symbol] = latest_close

        if signal_bar_already_processed:
            processed_owner_conflict = False
            if hasattr(strategy, "evaluate_spread_exit"):
                self._process_credit_spread_exits(
                    strategy=strategy,
                    underlying=symbol,
                    underlying_close=latest_close,
                    current_regime=current_regime,
                )
            else:
                position = self._get_position_for(symbol, snapshot)
                if position is not None:
                    closed = self._process_single_leg_emergency_exit(
                        symbol=symbol,
                        strategy=strategy,
                        position=position,
                        snapshot=snapshot,
                        latest_close=latest_close,
                    )
                    if not closed:
                        try:
                            signals = strategy._raw_signals(df)
                            if bool(signals.exits.iloc[-1]):
                                owner = self._get_owner(symbol)
                                if owner is not None and owner != strategy.name:
                                    processed_owner_conflict = True
                                    logger.debug(
                                        f"[{strategy.name}] {symbol}: processed-bar exit ignored — "
                                        f"position owned by '{owner}'"
                                    )
                                else:
                                    self._close_single_leg_position(
                                        symbol=symbol,
                                        strategy=strategy,
                                        position=position,
                                        snapshot=snapshot,
                                        latest_close=latest_close,
                                        alert_reason="exit signal",
                                    )
                        except Exception as e:
                            logger.error(
                                f"[{strategy.name}] {symbol}: processed-bar exit check failed: {e}"
                            )
            if (
                strategy_statuses is not None
                and signal_key in self._processed_signal_statuses
            ):
                strategy_statuses[symbol] = self._processed_signal_statuses[signal_key]
            if (
                strategy_reasons is not None
                and signal_key in self._processed_signal_reasons
            ):
                strategy_reasons[symbol] = list(self._processed_signal_reasons[signal_key])
            if processed_owner_conflict:
                return
            return

        # 4. Signals.
        raw_signals, signals, edge_allowed, edge_reasons = strategy.inspect_signals(
            df,
            symbol=symbol,
        )
        raw_entry = bool(raw_signals.entries.iloc[-1])
        last_entry = bool(signals.entries.iloc[-1])
        last_exit = bool(signals.exits.iloc[-1])

        # PLAN 11.10f: lifecycle counter — raw_signals + gate-order
        # attribution. Per design §12.4.1 the documented gate order is
        # regime → edge filter → sleeve → risk. The trade-decision
        # control flow below (`if not last_entry: return; if not
        # entry_allowed: return`) returns at the first branch even
        # when regime would also reject, so attributing in those
        # inline branches would always credit edge_filter for the
        # regime+filter overlap. We do the attribution HERE (before
        # any decision returns) so regime_blocked is credited first
        # when both conditions hold. The decision flow itself is
        # unchanged — only the counter assignment.
        # PR #21 reviewer fix.
        if raw_entry:
            _lc = self._lifecycle_counter_for(strategy.name)
            if _lc is not None:
                _lc.raw_signals += 1
                if not entry_allowed:
                    # Regime gate takes priority per the design's
                    # documented gate order.
                    _lc.regime_blocked += 1
                elif not last_entry:
                    # Regime allowed but edge filter cut the candidate.
                    _lc.edge_filter_blocked += 1
                # else: candidate passes both gates; sleeve/risk
                # counters increment downstream when applicable.

        position = self._get_position_for(symbol, snapshot)
        logger.info(
            f"[{strategy.name}] {symbol}: bar={latest_ts.isoformat()} "
            f"close=${latest_close:.2f} atr=${latest_atr:.2f} "
            f"entry={last_entry} exit={last_exit} "
            f"position={'OPEN ' + str(position.qty) if position else 'flat'}"
        )
        if using_prior_completed_bar:
            logger.debug(
                f"[{strategy.name}] {symbol}: using prior completed {timeframe} bar "
                f"{latest_ts.isoformat()} for live decisions"
            )

        # Credit-spread exit path (11.29 PR 3b). These strategies hold
        # multi-leg positions the engine tracks by position_id, not by the
        # underlying symbol — so the regular position-based exit branch below
        # never sees them. Run it here, before any entry gating, so spread
        # exits are never blocked by halt / regime / sleeve. Entry continues
        # below: a credit-spread strategy can open new spreads while holding
        # others, subject to its own per-instance caps.
        if hasattr(strategy, "evaluate_spread_exit"):
            self._process_credit_spread_exits(
                strategy=strategy,
                underlying=symbol,
                underlying_close=latest_close,
                current_regime=current_regime,
            )

        # 5. Exit branch — close before considering entries (always safe to
        # reduce risk; never blocked by halt).
        emergency_exit = False
        if not last_exit and position is not None:
            try:
                emergency_exit = strategy.inspect_open_positions(position, latest_close)
                if emergency_exit:
                    logger.warning(f"[{strategy.name}] {symbol}: EMERGENCY EXIT triggered by strategy hook.")
            except Exception as e:
                logger.error(f"[{strategy.name}] {symbol}: inspect_open_positions failed: {e}")

        if (last_exit or emergency_exit) and position is not None:
            # Only the strategy that opened the position may close it.
            owner = self._get_owner(symbol)
            if owner is not None and owner != strategy.name:
                logger.info(
                    f"[{strategy.name}] {symbol}: exit signal ignored — "
                    f"position owned by '{owner}'"
                )
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return
            if self._has_pending_close_order(symbol, snapshot):
                if strategy_statuses is not None:
                    strategy_statuses[symbol] = "Pending Exit"
                if strategy_reasons is not None:
                    strategy_reasons[symbol] = []
                logger.info(
                    f"{symbol}: exit signal but a close order is already pending — skipping"
                )
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return
            closed = self._close_single_leg_position(
                symbol=symbol,
                strategy=strategy,
                position=position,
                snapshot=snapshot,
                latest_close=latest_close,
                alert_reason="exit signal",
            )
            if closed:
                if strategy_statuses is not None:
                    strategy_statuses[symbol] = "No Signal"
                if strategy_reasons is not None:
                    strategy_reasons[symbol] = []
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
            return

        # 6. Entry branch — risk is the gate.
        if not last_entry:
            if position is None and strategy_statuses is not None:
                current_status = strategy_statuses.get(symbol)
                if raw_entry and current_status == "No Signal":
                    strategy_statuses[symbol] = "Filter Blocked"
                    if strategy_reasons is not None:
                        strategy_reasons[symbol] = list(edge_reasons)
                elif (
                    edge_allowed is False
                    and current_status == "No Signal"
                ):
                    strategy_statuses[symbol] = "Filter Blocked"
                    if strategy_reasons is not None:
                        strategy_reasons[symbol] = list(edge_reasons)
            # PLAN 11.10f: edge_filter_blocked counter is now incremented
            # by the gate-order attribution block at the top of
            # _process_symbol (see comment there) so regime takes
            # priority when both conditions hold.
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return
        if not entry_allowed:
            if position is None and strategy_statuses is not None:
                strategy_statuses[symbol] = "Regime Blocked"
                if strategy_reasons is not None:
                    strategy_reasons[symbol] = (
                        [regime_block_reason] if regime_block_reason else []
                    )
            logger.debug(
                f"[{strategy.name}] {symbol}: entry blocked by regime gate"
            )
            # PLAN 11.10f: regime_blocked counter incremented by the
            # gate-order attribution block at the top of
            # _process_symbol — see comment there.
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return
        if self._startup_mode != "NORMAL":
            logger.info(
                f"[{strategy.name}] {symbol}: entry blocked — "
                f"startup_mode={self._startup_mode}"
            )
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return
        if self._entry_blocked_by_existing_position(strategy, position):
            # Already in this position — the crossover bar persists across
            # intra-day cycles, so this is expected noise, not a real signal.
            # Risk would reject anyway; skip to avoid spamming alerts.
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return
        if self._has_pending_entry_order(
            symbol,
            strategy.name,
            snapshot,
            order_strategy or {},
        ):
            if strategy_statuses is not None:
                strategy_statuses[symbol] = "Pending Entry"
            if strategy_reasons is not None:
                strategy_reasons[symbol] = []
            logger.info(
                f"[{strategy.name}] {symbol}: entry skipped — a buy order is already pending"
            )
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return

        if self.risk.is_halted():
            reason = self.risk.halt_reason() or "global risk halt active"
            logger.info(
                f"[{strategy.name}] {symbol}: entry blocked — {reason}"
            )
            self.alerts.order_rejection(
                symbol, strategy.name, reason, RejectionCode.HALTED.value
            )
            if strategy_statuses is not None:
                strategy_statuses[symbol] = "Risk Blocked"
            if strategy_reasons is not None:
                strategy_reasons[symbol] = [reason]
            _lc = self._lifecycle_counter_for(strategy.name)
            if _lc is not None:
                _lc.risk_blocked += 1
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return None

        # Operator Controls Phase B — soft entry pauses.
        # Layered under the halt gate above. The order matters:
        # halt → pause-entries (global) → pause-strategy (scoped).
        # Each branch logs its own reason, fires an order_rejection
        # alert with HALTED code (the rejection taxonomy doesn't yet
        # distinguish soft pause; the alert reason text carries the
        # discriminator), and increments the risk_blocked counter
        # so the health monitor sees the block.
        if self.risk.is_entries_paused():
            reason = (
                self.risk.entries_paused_reason()
                or "entries paused by operator"
            )
            soft_reason = f"pause-entries: {reason}"
            logger.info(
                f"[{strategy.name}] {symbol}: entry blocked — {soft_reason}"
            )
            self.alerts.order_rejection(
                symbol, strategy.name, soft_reason, RejectionCode.HALTED.value
            )
            if strategy_statuses is not None:
                strategy_statuses[symbol] = "Paused"
            if strategy_reasons is not None:
                strategy_reasons[symbol] = [soft_reason]
            _lc = self._lifecycle_counter_for(strategy.name)
            if _lc is not None:
                _lc.risk_blocked += 1
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return None

        if self.risk.is_strategy_paused(strategy.name):
            paused = self.risk.paused_strategies_snapshot().get(strategy.name, {})
            soft_reason = (
                f"pause-strategy {strategy.name}: "
                f"{paused.get('reason') or 'paused by operator'}"
            )
            logger.info(
                f"[{strategy.name}] {symbol}: entry blocked — {soft_reason}"
            )
            self.alerts.order_rejection(
                symbol, strategy.name, soft_reason, RejectionCode.HALTED.value
            )
            if strategy_statuses is not None:
                strategy_statuses[symbol] = "Paused"
            if strategy_reasons is not None:
                strategy_reasons[symbol] = [soft_reason]
            _lc = self._lifecycle_counter_for(strategy.name)
            if _lc is not None:
                _lc.risk_blocked += 1
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return None

        # Shared-symbol conflict (11.7 Part A, refined by PLAN 11.44).
        #
        # Asymmetry by ownership-model keying:
        #   * Equity and single-leg-options positions are keyed in ``_positions``
        #     by ``owner_key_for(symbol)`` (equity ticker or option underlying).
        #     Two of them on the same underlying ticker cannot coexist — the
        #     map can only hold one record per owner_key, so a second
        #     pre-registration would either be silently dropped (clobbering
        #     ownership / entry-price attribution) or aggregate into one
        #     broker position with ambiguous attribution. Both incoming
        #     equity strategies and incoming single-leg options strategies
        #     must therefore pass this check.
        #   * MLEG (spread) positions are keyed by UUID and never occupy the
        #     underlying slot, so MLEG strategies skip the underlying check
        #     entirely. The contract-level guard at dispatch
        #     (``_reject_if_contract_conflict``) is the operative safety net
        #     for them — it blocks the exact-OCC overlap case (the only
        #     scenario that would aggregate at the broker).
        #
        # ``_get_owner`` only finds single-leg owners by construction (the
        # underlying-key lookup misses UUID-keyed spreads), so an existing
        # spread on the same underlying does NOT block an incoming
        # single-leg options strategy here — exactly the headline
        # 2026-05-29 case (spy_options_reversion + credit_spread on SPY).
        #
        # Future expansion: allowing two single-leg options strategies on
        # the same underlying requires moving single-leg option Positions
        # off the underlying-keyed ``_positions`` slot (e.g. UUID or full
        # OCC). Tracked as follow-up to PLAN 11.44.
        is_mleg_strategy = hasattr(strategy, "build_spread_execution")
        if not is_mleg_strategy:
            existing_owner = self._get_owner(symbol)
            if existing_owner is not None and existing_owner != strategy.name:
                logger.info(
                    f"[{strategy.name}] {symbol}: entry blocked — "
                    f"symbol already owned by '{existing_owner}'"
                )
                self.alerts.order_rejection(
                    symbol,
                    strategy.name,
                    f"symbol already owned by '{existing_owner}'",
                    "SYMBOL_CONFLICT",
                )
                self._symbol_conflicts.append(datetime.now(timezone.utc))
                if strategy_statuses is not None:
                    strategy_statuses[symbol] = "Symbol Conflict"
                if strategy_reasons is not None:
                    strategy_reasons[symbol] = [f"owned by '{existing_owner}'"]
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return None

        # Sleeve check — must pass before risk sizing.
        # Narrows the notional budget available to this strategy without
        # bypassing any global risk control. Exits are never sleeve-gated.
        notional_cap: float | None = None
        if self._allocator is not None:
            from risk.allocator import SleeveRejection
            sleeve = self._allocator.check(
                strategy_name=strategy.name,
                account=account,
                open_orders=snapshot.open_orders,
                position_owners=self._owners_view(),
                order_strategy=order_strategy or {},
                additional_used_notional=self._multi_leg_risk_notional_by_strategy(),
            )
            if isinstance(sleeve, SleeveRejection):
                logger.info(
                    f"[{strategy.name}] {symbol}: "
                    f"sleeve blocked — {sleeve.message}"
                )
                self.alerts.order_rejection(
                    symbol, strategy.name, sleeve.message, sleeve.code.value
                )
                # PLAN 11.10f: lifecycle counter — sleeve_blocked.
                # Mutual exclusivity per design §12.4.1: edge filter
                # and regime have already passed by the time we reach
                # the sleeve check.
                _lc = self._lifecycle_counter_for(strategy.name)
                if _lc is not None:
                    _lc.sleeve_blocked += 1
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return None
            notional_cap = sleeve.max_position_notional

        # Credit-spread entry path (11.29 PR 3b). A credit-spread strategy
        # exposes build_spread_execution and is dispatched as an atomic MLEG
        # combo. Defined-risk sizing is capped by the sleeve notional; global
        # account loss limits and sticky halts are enforced by the universal
        # entry gate above and again at the broker's final submit boundary.
        if hasattr(strategy, "build_spread_execution"):
            self._enter_multi_leg(
                strategy=strategy,
                symbol=symbol,
                underlying_close=latest_close,
                notional_cap=notional_cap,
                signal_key=signal_key,
                signal_bar=signal_bar,
                strategy_statuses=strategy_statuses,
                strategy_reasons=strategy_reasons,
            )
            return None

        target_symbol = symbol
        target_price = latest_close
        take_profit = None
        stop_price = None

        if hasattr(strategy, "build_option_execution"):
            try:
                opt_sym, opt_price, opt_tp, opt_sl = strategy.build_option_execution(
                    symbol, latest_close, notional_cap=notional_cap
                )
                target_symbol = opt_sym
                target_price = opt_price
                take_profit = opt_tp
                stop_price = opt_sl
                logger.info(f"[{strategy.name}] Option Execution override: {symbol} -> {target_symbol} at ${target_price:.2f}")
            except OptionTradeRejected as e:
                logger.warning(
                    f"[{strategy.name}] Option trade rejected for {symbol}: {e}"
                )
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return None
            except Exception as e:
                logger.error(f"[{strategy.name}] Failed to build option execution for {symbol}: {e}")
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return None

            # PLAN 11.44: contract-level conflict guard. The picker resolved
            # the OCC; reject before order submission if another strategy
            # already owns the exact contract. For single-leg options this
            # is the second of two checks — the underlying-level
            # ``_get_owner`` check above already blocks another single-leg
            # owner of the same underlying (the single-leg ``_positions``
            # slot can only hold one), so the case reached here is the
            # exact-OCC clash against an MLEG leg owner (whose UUID-keyed
            # position does not occupy the underlying slot).
            if is_occ_option(target_symbol):
                if self._reject_if_contract_conflict(
                    strategy_name=strategy.name,
                    symbol=symbol,
                    occs=[target_symbol],
                ) is not None:
                    if strategy_statuses is not None:
                        strategy_statuses[symbol] = "Contract Conflict"
                    if strategy_reasons is not None:
                        strategy_reasons[symbol] = [
                            f"{target_symbol} owned by another strategy"
                        ]
                    self._mark_signal_bar_processed(
                        signal_key, signal_bar, strategy_statuses,
                        strategy_reasons, symbol,
                    )
                    return None

        # PLAN 11.32: gate MARKET entries through the per-strategy price cap.
        # Options/spread paths build their own envelopes upstream and pass
        # `is_option`/strategy hooks; the cap is for plain equity MARKET entries
        # only. We key the policy by strategy name and only act when the
        # strategy itself declares MARKET as its preferred order type.
        entry_max_price: float | None = None
        if (
            not hasattr(strategy, "build_option_execution")
            and strategy.preferred_order_type is OrderType.MARKET
        ):
            policy = settings.ENTRY_PRICE_CAPS.get(strategy.name)
            cap_decision = gate_entry(
                reference_price=target_price,
                atr=latest_atr,
                side="buy",
                order_type="market",
                policy=policy,
            )
            if cap_decision.action is CapAction.CONVERT_TO_LIMIT:
                entry_max_price = cap_decision.cap_price
                logger.info(
                    f"[entry-guard] {strategy.name} {symbol}: capping market "
                    f"entry at ${entry_max_price:.2f} "
                    f"(ref ${target_price:.2f}, atr {latest_atr:.2f}, "
                    f"chase {cap_decision.diagnostics['chase_bps']:.1f}bps)"
                )

        # PLAN 11.47 STOP_LIMIT branch: equity strategies that declare
        # STOP_LIMIT (Donchian today) emit a broker-resting stop-limit
        # entry. The trigger comes from the strategy (the level that
        # produced the signal — the prior-N-day high for Donchian); the
        # limit cap reuses the PLAN 11.32 EntryPriceCap policy but
        # anchored to the trigger, not to the close. The protective stop
        # leg is the usual ATR stop attached via OTO, which the substrate
        # P-4 dispatch records automatically.
        signal_order_type = strategy.preferred_order_type
        signal_limit_price: float | None = None
        entry_trigger_price: float | None = None
        if (
            not hasattr(strategy, "build_option_execution")
            and signal_order_type is OrderType.STOP_LIMIT
        ):
            policy = settings.ENTRY_PRICE_CAPS.get(strategy.name)
            if policy is None:
                logger.error(
                    f"[entry-guard] {strategy.name} {symbol}: STOP_LIMIT strategy "
                    f"requires an ENTRY_PRICE_CAPS policy to anchor the limit "
                    f"leg; skipping entry"
                )
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return None
            try:
                trigger = float(strategy.compute_entry_trigger(df))
            except Exception as e:
                logger.error(
                    f"[entry-guard] {strategy.name} {symbol}: "
                    f"compute_entry_trigger failed: {e}"
                )
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return None
            entry_trigger_price = trigger
            signal_limit_price = compute_cap_price(
                reference_price=trigger,
                atr=latest_atr,
                side="buy",
                policy=policy,
            )
            chase_bps = (signal_limit_price / trigger - 1.0) * 1e4
            logger.info(
                f"[entry-guard] {strategy.name} {symbol}: STOP_LIMIT "
                f"trigger=${trigger:.2f}, limit=${signal_limit_price:.2f} "
                f"(close=${target_price:.2f}, atr={latest_atr:.2f}, "
                f"chase {chase_bps:.1f}bps)"
            )
        elif signal_order_type is OrderType.LIMIT:
            signal_limit_price = target_price

        sig = Signal(
            symbol=target_symbol,
            side=Side.BUY,
            strategy_name=strategy.name,
            reference_price=target_price,
            atr=latest_atr,
            reason=f"{strategy.name} entry @ {latest_ts.isoformat()}",
            order_type=signal_order_type,
            limit_price=signal_limit_price,
            take_profit_price=take_profit,
            stop_price_override=stop_price,
            entry_max_price=entry_max_price,
            entry_trigger_price=entry_trigger_price,
        )
        decision = self.risk.evaluate(sig, account, notional_cap=notional_cap)
        if isinstance(decision, RiskRejection):
            # Already logged by risk; alert the operator.
            self.alerts.order_rejection(
                symbol, strategy.name, decision.message, decision.code.value
            )
            # PLAN 11.10f: lifecycle counter — risk_blocked.
            # Mutual exclusivity per §12.4.1 — edge/regime/sleeve all
            # passed.
            _lc = self._lifecycle_counter_for(strategy.name)
            if _lc is not None:
                _lc.risk_blocked += 1
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
            return None
        assert isinstance(decision, RiskDecision)

        # Arrival-price benchmark for execution-quality slippage measurement
        # (industry TCA: Implementation Shortfall vs Arrival Price). Capture
        # the NBBO midpoint immediately before submission so the eventual
        # fill is compared against the live market state, not against the
        # decision-time bar close (which would conflate execution slippage
        # with signal-to-fill alpha decay — Issue B in the slippage PR).
        # Falls back to latest_close when the quote is unavailable (one-sided
        # book, pre-market gap, API failure, broker mock in tests); the
        # `_finite_or_none` guard rejects any non-numeric / non-finite
        # return so a misbehaving broker can never inject a Mock / NaN
        # into the slippage math.
        #
        # Skip the fetch for OCC option symbols entirely — they belong to
        # OPRA, not the stock quote endpoint, so a `get_stock_latest_quote`
        # call against `SPY260618C00746000` would raise on every cycle
        # (caught and logged warning, but noisy). Options entries are
        # LIMIT-typed and gated by build_record's market-only slippage
        # check anyway, so the fetch's result wouldn't be used.
        if is_occ_option(target_symbol):
            arrival_price: float | None = None
        else:
            arrival_price = _finite_or_none(
                self.broker.get_latest_quote_midpoint(target_symbol)
            )
        # Slippage unification — tag which benchmark we're actually
        # using so the new taxonomy columns are honest. See codepaths
        # §1 (MARKET) and §2 (LIMIT) in
        # docs/slippage_unification_design.md.
        #
        # Order-type-aware (PR #68 round-1 review P1): for LIMIT and
        # STOP_LIMIT entries, arrival-price slippage isn't a meaningful
        # execution-quality metric. The substrate must mirror codepath
        # §2 (kind='limit_price', quality='unavailable', NULL
        # benchmark_price) at submit time so the trades-row UPSERT's
        # PRESERVE-FIRST-NON-NULL COALESCE policy doesn't lock in
        # 'arrival_midpoint' / 'primary' on a LIMIT row, which would
        # then survive every later UPSERT (including the recovery
        # completeness call in _maybe_dispatch_substrate_entry_fill).
        # The synchronous-fill path is also affected: if the stream's
        # 'accepted' event apply_order_event races _log_entry's UPSERT,
        # the substrate's submit-time tag wins via PRESERVE-FIRST and
        # the row ends with the wrong kind/quality even on the
        # non-recovery codepath.
        is_market_order = decision.order_type is OrderType.MARKET
        slippage_kind: str | None
        slippage_ref: float | None
        slippage_quality: str
        if is_market_order:
            slippage_ref = (
                arrival_price if arrival_price is not None else latest_close
            )
            if arrival_price is not None:
                slippage_kind = "arrival_midpoint"
                slippage_quality = "primary"
            elif latest_close is not None:
                slippage_kind = "fallback_latest_close"
                slippage_quality = "fallback"
            else:
                slippage_kind = None  # build_record will default to 'unavailable'
                slippage_quality = "unavailable"
        else:
            # LIMIT / STOP_LIMIT — passive fill or stop-triggered limit
            # fill. Neither uses arrival-price as a meaningful benchmark.
            slippage_ref = None
            slippage_kind = "limit_price"
            slippage_quality = "unavailable"
        try:
            # PR #60 commit 9 fix E: thread the arrival benchmark we
            # just computed (and its provenance kind) through to the
            # broker so the per-order substrate row records the exact
            # observation §10.5 of the discovery doc requires.
            # Recovery rows that hit apply_order_event later inherit
            # this via the COALESCE policy in TradeLogger.log.
            _now_iso = self._clock().isoformat()
            result = self.broker.place_order(
                decision,
                slippage_benchmark_price=slippage_ref,
                slippage_benchmark_kind=slippage_kind,
                slippage_benchmark_timestamp=(
                    _now_iso if slippage_ref is not None else None
                ),
                slippage_measurement_quality=slippage_quality,
            )
            # PLAN 11.10f: lifecycle counter — submitted increments
            # once per place_order call (regardless of fill status).
            # ACCEPTED, FILLED, PARTIAL, UNKNOWN all count as submitted
            # — the order reached the broker.
            _lc = self._lifecycle_counter_for(strategy.name)
            if _lc is not None:
                _lc.submitted += 1
            if result.status is OrderStatus.UNKNOWN:
                # P-6: substrate pipeline now owns recovery. The
                # per-order substrate row was already inserted at
                # submit time (foundation commit 6) and the broker
                # order_id was attached on the successful submit
                # return — when the order's terminal state arrives
                # via stream / cycle reconcile / startup reconcile,
                # apply_order_event advances the substrate row and
                # _maybe_dispatch_substrate_entry_fill fires the
                # engine-side side effects. No cache state required.
                #
                # Defensive log for the genuinely-broken case (no
                # order_id) the legacy _remember_suspect_order
                # handled with risk.record_broker_error + alert.
                if result.order_id is None:
                    msg = (
                        f"{decision.symbol}: place_order returned "
                        f"UNKNOWN with no order_id — broker submit "
                        f"path failed before id assignment; substrate "
                        f"row will sit at pending until manual "
                        f"investigation"
                    )
                    logger.error(msg)
                    self.risk.record_broker_error()
                    self.alerts.broker_error(msg)
                if strategy_statuses is not None:
                    strategy_statuses[symbol] = "Pending Entry"
                if strategy_reasons is not None:
                    strategy_reasons[symbol] = []
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return None
            if result.status is OrderStatus.ACCEPTED:
                # Options worker dispatched asynchronously — pre-register
                # ownership so the position is managed when it arrives in the
                # broker snapshot. The actual fill is logged via drain_option_fills().
                # Register with target_symbol (the OCC contract) so the
                # Position's leg carries the real option symbol; owner_key_for()
                # still keys the position by the underlying. _entry_prices stays
                # keyed by `symbol` (== the owner key).
                self._register_single_leg(strategy_name=strategy.name, symbol=target_symbol)
                self._entry_prices[symbol] = target_price
                logger.info(
                    f"[{strategy.name}] {symbol}: options order dispatched "
                    f"({target_symbol}), ownership pre-registered"
                )
                if strategy_statuses is not None:
                    strategy_statuses[symbol] = "Pending Entry"
                if strategy_reasons is not None:
                    strategy_reasons[symbol] = []
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return None
            self._record_fill(
                result,
                modeled_price=slippage_ref,
                order_type=decision.order_type.value,
                side=decision.side.value,
            )
            self._log_entry(
                decision,
                result,
                slippage_ref,
                benchmark_kind=slippage_kind,
            )
            if result.status in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
                # PLAN 11.10f: lifecycle counter — filled_entries.
                # Per design §12.4.1: one entry that opened a position
                # counts as 1, regardless of fill quantity. Partial
                # fills that successfully opened the intended position
                # count as 1; full fills also count as 1.
                _lc = self._lifecycle_counter_for(strategy.name)
                if _lc is not None:
                    _lc.filled_entries += 1
                # target_symbol == symbol for equities; the OCC contract for
                # synchronous option fills. Register with it so the leg carries
                # the real traded symbol.
                self._register_single_leg(strategy_name=strategy.name, symbol=target_symbol)
                if strategy_statuses is not None:
                    strategy_statuses[symbol] = "Long"
                if strategy_reasons is not None:
                    strategy_reasons[symbol] = []
                fill_price = result.avg_fill_price or decision.entry_reference_price
                fill_qty = float(result.filled_qty or decision.qty)
                # Cache entry price for HWM P&L gate (used on close).
                self._entry_prices[symbol] = fill_price
                self.alerts.trade_executed(
                    symbol=symbol,
                    strategy=strategy.name,
                    side="buy",
                    qty=fill_qty,
                    price=fill_price,
                    reason=sig.reason,
                )
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
                return Position(
                    symbol=symbol,
                    qty=fill_qty,
                    avg_entry_price=fill_price,
                    market_value=fill_qty * fill_price,
                )
            if strategy_statuses is not None:
                strategy_statuses[symbol] = "Pending Entry"
            if strategy_reasons is not None:
                strategy_reasons[symbol] = []
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )
        except Exception as e:
            logger.error(f"{symbol}: place_order raised: {e}")
            self.risk.record_broker_error()
            self.alerts.broker_error(f"{symbol} place_order: {e}")
        return None

    def _decision_frame(
        self,
        df: pd.DataFrame,
        timeframe: str,
        *,
        market_open: bool | None,
    ) -> tuple[pd.DataFrame, bool]:
        """
        Return the bars that are safe for live signal generation.

        For Alpaca daily bars, the latest bar during market hours is the current
        in-progress session, emitted and updated throughout the day. For live
        daily strategies we therefore drop that bar and trade only on the prior
        completed daily candle so live matches the backtest contract
        (signal at bar close, execute next bar open).
        """
        if (
            timeframe != "1Day"
            or not market_open
            or df.empty
        ):
            return df, False

        latest_ts = pd.Timestamp(df.index[-1])
        latest_ny = latest_ts.tz_convert(_NEW_YORK_TZ)
        now_ny = pd.Timestamp(self._clock()).tz_convert(_NEW_YORK_TZ)
        is_daily_bucket_start = (
            latest_ny.hour == 0
            and latest_ny.minute == 0
            and latest_ny.second == 0
            and latest_ny.microsecond == 0
        )
        if is_daily_bucket_start and latest_ny.date() == now_ny.date():
            return df.iloc[:-1], True
        return df, False

    def _should_skip_processed_signal_bar(
        self,
        key: tuple[str, str, str],
        bar_ts: pd.Timestamp,
    ) -> bool:
        """True when this completed daily bar was already evaluated earlier."""
        if key[2] != "1Day":
            return False
        return self._processed_signal_bars.get(key) == bar_ts

    def _mark_signal_bar_processed(
        self,
        key: tuple[str, str, str],
        bar_ts: pd.Timestamp,
        strategy_statuses: dict[str, str] | None,
        strategy_reasons: dict[str, list[str]] | None,
        symbol: str,
    ) -> None:
        """Remember that this completed daily bar has been handled this session."""
        if key[2] != "1Day":
            return
        self._processed_signal_bars[key] = bar_ts
        if strategy_statuses is not None:
            self._processed_signal_statuses[key] = strategy_statuses.get(
                symbol, "No Signal"
            )
        if strategy_reasons is not None:
            self._processed_signal_reasons[key] = list(
                strategy_reasons.get(symbol, [])
            )

    def _apply_recovered_entry_side_effects(
        self,
        *,
        snapshot: BrokerSnapshot,
        position: Position,
        decision: RiskDecision,
        fill_price: float,
        fill_qty: float,
        reason_suffix: str = "(recovered)",
    ) -> None:
        """Fire the engine-side side effects that happen when a
        bot-submitted entry is adopted on the recovery path.

        Extracted from the body of _recover_suspect_orders so two
        callers can share it:

          - Legacy suspect-cache path (this commit): when
            _recover_suspect_orders confirms a UNKNOWN-then-FILLED
            entry against the broker.
          - Substrate-driven path (next commit): when the substrate
            observes a fill on an entry_primary row whose engine
            ownership hasn't been bound yet (recovery via stream /
            cycle / startup reconcile rather than via the cache).

        Side effects:
          1. Register strategy ownership of the symbol so signals
             can act on it.
          2. Cache the entry price for the HWM drawdown gate.
          3. Submit a protective stop if the broker doesn't already
             have one attached (recovered entries lose their
             original stop_loss leg when confirmation fails before
             OTO completion).
          4. Fire the trade_executed alert.

        Trade-log writes (_record_fill / _log_entry) are NOT in
        this helper — they're called inline by each caller because
        the substrate's apply_order_event UPSERT consolidates with
        TradeLogger.log on the same order_id, and the two paths
        capture provenance differently. Keeping that distinction
        explicit at the call site.

        Idempotent on items 1-2-3: register/cache/stop check
        already-present state and no-op if so. NOT idempotent on
        item 4 (alert) — callers must de-dup at their own layer
        (the suspect path pops the cache; the substrate path uses
        _has_position).
        """
        symbol = decision.symbol
        self._register_single_leg(
            strategy_name=decision.strategy_name, symbol=symbol,
        )
        self._entry_prices[symbol] = fill_price
        self._ensure_recovered_protective_stop(
            snapshot=snapshot, position=position, decision=decision,
        )
        # PR-65 review F1: `result` was not in scope (helper takes
        # decision/position/fill_price/fill_qty only). Look up the uid
        # via the lifecycle store using owner_key, which works for both
        # the substrate-driven recovery path and any future caller.
        self.alerts.trade_executed(
            symbol=symbol,
            strategy=decision.strategy_name,
            side="buy",
            qty=fill_qty,
            price=fill_price,
            reason=f"{decision.reason} {reason_suffix}".strip(),
            position_uid=self._lookup_position_uid_for_owner(
                owner_key_for(symbol)
            ),
        )

    def _maybe_dispatch_substrate_entry_fill(
        self,
        *,
        event: "Any",
        snapshot: "BrokerSnapshot | None",
    ) -> None:
        """P-6 commit B: fire the engine-side recovery side effects
        when the substrate observes an entry_primary fill that the
        synchronous place_order path didn't already handle.

        Called from _drain_lifecycle_events (stream) and
        _reconcile_substrate_via_rest (cycle / startup) after each
        successful apply_order_event. De-dup guard: if
        _has_position(symbol) is already true, the synchronous
        path bound ownership at submit time and the side effects
        already fired — skip.

        Concretely this replaces the cache-driven path the legacy
        _suspect_orders cache provides: on the rare submit-succeeds-
        but-confirm-fails scenario, the cache used to hold the
        decision metadata and fire the side effects on the next
        cycle's broker reconciliation. The substrate now observes
        the fill via stream / cycle / startup reconcile and this
        dispatch fires the same side effects from the same
        helper (_apply_recovered_entry_side_effects).

        Best-effort: any failure is logged at CRITICAL and absorbed
        so a bad dispatch can't stall the cycle. The substrate has
        already recorded the fill; the engine just hasn't bound
        ownership.
        """
        # PR #68 round-1 review P3: accept partially_filled in addition
        # to filled. docs/order_lifecycle_state_machine.md §3.2 lists
        # FILLED / PARTIAL + broker position present as the recovery
        # trigger. The substrate's canonical string for partial fills
        # is 'partially_filled' (engine/lifecycle_orders.py
        # VALID_ORDER_STATUSES).
        #
        # Single-shot via _has_position applies to the side-effects
        # block ONLY (ownership bind / stop replacement / alert). The
        # accounting-completeness block runs on every event so the
        # row's signed/adverse refresh against the cumulative
        # avg_fill_price. See round-2 review P2.
        if event.status not in {"filled", "partially_filled"}:
            return
        if float(event.filled_qty or 0.0) <= 0:
            return
        if event.avg_fill_price is None:
            return
        if self.lifecycle_orders_store is None or self.lifecycle_store is None:
            return
        try:
            order_row = self.lifecycle_orders_store.get_by_order_id(
                event.order_id,
            )
            if order_row is None or order_row.role != "entry_primary":
                return
            pos_row = self.lifecycle_store.get_by_position_uid(
                order_row.position_uid,
            )
            if pos_row is None:
                return
            symbol = pos_row.symbol
            from risk.manager import RiskDecision, Side
            # PR #68 round-2 review P2: split the gate. The side-effects
            # block (ownership bind / stop replacement / alert) must
            # run exactly once via _has_position, but the accounting
            # completeness block must run on EVERY filled /
            # partially_filled event so the row's signed/adverse
            # refresh to the cumulative avg_fill_price. Without
            # this, a partial-then-final-fill recovery would persist
            # the first-partial slippage (e.g. row.avg_fill_price=
            # 150.50 alongside slippage computed against 150.40).
            ownership_already_bound = self._has_position(symbol)

            # ── Side effects (single-shot via ownership gate) ──
            #
            # PR #68 round-3 review P2: the RiskDecision construction
            # used by the side-effects helper lives INSIDE this branch
            # because option LIMIT entry substrate rows are
            # intentionally created with intended_stop_price=None (per
            # execution/broker.py — option exits are strategy-managed,
            # not stop-managed) and RiskDecision.__post_init__ rejects
            # stop_price <= 0. Pre-fix the round-2 hoist outside the
            # gate raised on every async option fill that arrived
            # after the synchronous path had already bound ownership,
            # emitting a false CRITICAL on the live options sleeve.
            # The focused-UPDATE accounting block below doesn't need a
            # RiskDecision; it reads everything off `order_row` and
            # `event`.
            if not ownership_already_bound:
                if snapshot is None:
                    logger.warning(
                        f"substrate entry-fill dispatch: skipped "
                        f"side-effects for order_id={event.order_id} "
                        f"symbol={symbol} — no snapshot available "
                        f"(cycle drains may run without one in some "
                        f"code paths)"
                    )
                else:
                    position = snapshot.account.open_positions.get(symbol)
                    if position is None:
                        logger.warning(
                            f"substrate entry-fill dispatch: skipped "
                            f"side-effects for order_id={event.order_id} "
                            f"symbol={symbol} — broker reports no open "
                            f"position; nothing to bind ownership against"
                        )
                    else:
                        # Build the recovery decision from the substrate
                        # row's original order_type. PLAN 11.47 R1 P1-1:
                        # STOP_LIMIT requires both entry_trigger_price
                        # and limit_price; without them
                        # RiskDecision.__post_init__ raises and gets
                        # caught by the outer CRITICAL handler.
                        recovered_order_type = OrderType(order_row.order_type)
                        recovered_extra_kwargs: dict = {}
                        if recovered_order_type is OrderType.STOP_LIMIT:
                            if (
                                order_row.intended_trigger_price is not None
                                and order_row.intended_limit_price is not None
                            ):
                                recovered_extra_kwargs["entry_trigger_price"] = (
                                    float(order_row.intended_trigger_price)
                                )
                                recovered_extra_kwargs["limit_price"] = float(
                                    order_row.intended_limit_price
                                )
                            else:
                                recovered_order_type = OrderType.MARKET
                        elif recovered_order_type is OrderType.LIMIT:
                            if order_row.intended_limit_price is not None:
                                recovered_extra_kwargs["limit_price"] = float(
                                    order_row.intended_limit_price
                                )
                            else:
                                recovered_order_type = OrderType.MARKET
                        recovered_decision = RiskDecision(
                            symbol=symbol,
                            side=Side.BUY,
                            qty=float(order_row.intended_qty),
                            entry_reference_price=float(event.avg_fill_price),
                            stop_price=float(order_row.intended_stop_price or 0.0),
                            strategy_name=pos_row.strategy,
                            reason="substrate dispatch",
                            order_type=recovered_order_type,
                            **recovered_extra_kwargs,
                        )
                        self._apply_recovered_entry_side_effects(
                            snapshot=snapshot,
                            position=position,
                            decision=recovered_decision,
                            fill_price=float(event.avg_fill_price),
                            fill_qty=float(event.filled_qty),
                            reason_suffix="(substrate)",
                        )
                        logger.warning(
                            f"substrate entry-fill dispatch: bound "
                            f"ownership for {symbol} ({pos_row.strategy}) "
                            f"via order_id={event.order_id}"
                        )

            # ── Accounting completeness (runs on every event) ──
            # The substrate's apply_order_event UPSERT writes the
            # trade row with provenance fields (slippage_benchmark_
            # price / kind / timestamp / quality) threaded through
            # from the substrate's submit-time capture, but it
            # leaves slippage_signed_bps / slippage_adverse_bps as
            # NULL because computing them needs modeled_price vs
            # avg_fill_price math.
            #
            # PR #68 round-2 review P2: do a focused UPDATE of just
            # the two computed columns rather than going through
            # build_record + tl.log. Pre-round-2 the dispatch's
            # tl.log UPSERT carried a full TradeRecord that included
            # reason='substrate dispatch' and entry_reference_price=
            # avg_fill_price, both of which got clobbered onto the
            # synchronous _log_entry path's correct values via
            # tl.log's default `excluded.<col>` LATEST-WINS semantics
            # for those columns (e.g. row.reason='golden cross entry'
            # → 'substrate dispatch' after a normal sync fill's
            # substrate event applied). The focused UPDATE touches
            # exclusively slippage_signed_bps and slippage_adverse_
            # bps, preserving every other audit field already on the
            # row.
            #
            # Idempotency: for synchronous-fill paths the values
            # we'd write equal the values _log_entry already wrote
            # (same modeled_price, same broker avg_fill_price). For
            # recovered partial-then-final-fill, this UPDATE refreshes
            # to the cumulative avg_fill_price.
            #
            # Gate on a populated, positive benchmark_price + a
            # MARKET-style benchmark_kind. LIMIT / STOP_LIMIT entries
            # (PR #68 round-1 P1 fix) carry kind='limit_price' /
            # benchmark_price=None, so we skip them — codepath §2
            # in docs/slippage_unification_design.md persists NULL
            # slippage by design.
            #
            # Best-effort: failures here don't unbind ownership or
            # invalidate the substrate-written row; they just mean
            # the row retains whatever computed slippage was already
            # there (NULL on recovery, sync-computed on synchronous).
            modeled_price = order_row.slippage_benchmark_price
            benchmark_kind = order_row.slippage_benchmark_kind
            can_compute_slippage = (
                modeled_price is not None
                and modeled_price > 0
                and benchmark_kind in ("arrival_midpoint", "fallback_latest_close")
            )
            if can_compute_slippage:
                try:
                    signed_bps = single_leg_realized_slippage_bps(
                        side="buy",
                        reference_price=float(modeled_price),
                        actual_fill_price=float(event.avg_fill_price),
                    )
                    adverse_bps = max(0.0, signed_bps)
                    conn = self.trade_logger._ensure_db()
                    conn.execute(
                        "UPDATE trades "
                        "SET slippage_signed_bps = ?, "
                        "    slippage_adverse_bps = ? "
                        "WHERE order_id = ? "
                        "  AND position_type = 'single_leg'",
                        (
                            round(signed_bps, 2),
                            round(adverse_bps, 2),
                            event.order_id,
                        ),
                    )
                    conn.commit()
                except Exception as log_exc:
                    logger.critical(
                        f"substrate entry-fill dispatch: trade-log "
                        f"completeness call FAILED for order_id="
                        f"{event.order_id}: "
                        f"{type(log_exc).__name__}: {log_exc}. Substrate "
                        f"row remains the source of truth; computed "
                        f"slippage fields stay NULL on this row."
                    )
        except Exception as exc:
            logger.critical(
                f"substrate entry-fill dispatch FAILED for "
                f"order_id={event.order_id}: "
                f"{type(exc).__name__}: {exc}. Substrate row "
                f"already recorded the fill; engine ownership not "
                f"bound. Next restart will recover from trades DB."
            )

    def _maybe_dispatch_substrate_exit_fill(
        self,
        *,
        event: "Any",
        snapshot: "BrokerSnapshot | None",
    ) -> None:
        """P-7 commit A: fire the engine-side recovery side effects
        when the substrate observes an exit-role fill that the
        synchronous close path didn't already handle.

        Mirrors _maybe_dispatch_substrate_entry_fill but for the
        close path: when an exit row reaches filled, write the
        trade row via _record_recovered_exit_fill (with
        skip_trades_dedup_check=True since apply_order_event
        already UPSERTed) and clear engine ownership state
        (_pop_position, _entry_prices, _external_close_suspects).

        De-dup guards (in order):
          1. event.status == 'filled'
          2. event.filled_qty > 0
          3. event.avg_fill_price is not None
          4. substrate row exists with role='exit'
          5. position_lifecycle row exists for the position_uid
          6. _has_position(symbol) — engine still owns the symbol.
             This is the PRIMARY idempotency signal (PR #61 round-1
             fix P1): after the first dispatch fires, _pop_position
             clears ownership; subsequent observations skip here.

        Safe to fire multiple times across stream / cycle / startup
        observations of the same fill.

        Best-effort: any failure logs CRITICAL and absorbs. The
        substrate already recorded the fill at the substrate
        level; the engine-side cleanup not happening means the
        position stays "owned" in the engine's view until the
        next restart, when _restore_ownership_from_db picks up
        the truth from the trade log (which the substrate
        UPSERT also wrote).
        """
        if event.status != "filled":
            return
        if float(event.filled_qty or 0.0) <= 0:
            return
        if event.avg_fill_price is None:
            return
        if self.lifecycle_orders_store is None or self.lifecycle_store is None:
            return
        try:
            order_row = self.lifecycle_orders_store.get_by_order_id(
                event.order_id,
            )
            if order_row is None or order_row.role != "exit":
                return
            pos_row = self.lifecycle_store.get_by_position_uid(
                order_row.position_uid,
            )
            if pos_row is None:
                return
            symbol = pos_row.symbol
            owner = pos_row.strategy
            # PR #61 round-1 fix P1: dedup via ownership, NOT via
            # has_recorded_order_id. apply_order_event has already
            # UPSERTed the trade row by the time we get here, so
            # the legacy "is this order_id in trades?" check would
            # always short-circuit. We instead gate on
            # _has_position: if the engine doesn't own the
            # symbol, the dispatch either already fired (and
            # popped ownership) or this is a phantom event for a
            # position never bound — skip.
            if not self._has_position(symbol):
                return
            # Build a synthetic exit_fill object matching the
            # OrderResult shape _record_recovered_exit_fill expects.
            exit_fill = OrderResult(
                status=OrderStatus.FILLED,
                order_id=event.order_id,
                symbol=symbol,
                requested_qty=float(order_row.intended_qty),
                filled_qty=float(event.filled_qty),
                avg_fill_price=float(event.avg_fill_price),
                raw_status="filled",
                message="substrate exit-fill dispatch",
            )
            wrote = self._record_recovered_exit_fill(
                symbol=symbol,
                owner=owner,
                exit_fill=exit_fill,
                modeled_price=float(
                    order_row.slippage_benchmark_price or 0.0
                ),
                benchmark_kind=(
                    order_row.slippage_benchmark_kind or "unavailable"
                ),
                alert_reason="substrate exit dispatch",
                # Substrate already wrote the trade row via
                # apply_order_event's UPSERT — bypass the legacy
                # dedup check so P&L / alert / cleanup actually fire.
                skip_trades_dedup_check=True,
            )
            if wrote:
                self._pop_position(symbol)
                self._entry_prices.pop(symbol, None)
                self._external_close_suspects.pop(symbol, None)
                logger.warning(
                    f"substrate exit-fill dispatch: cleared "
                    f"ownership for {symbol} ({owner}) via "
                    f"order_id={event.order_id}"
                )
        except Exception as exc:
            logger.critical(
                f"substrate exit-fill dispatch FAILED for "
                f"order_id={event.order_id}: "
                f"{type(exc).__name__}: {exc}. Substrate row "
                f"already recorded the fill; engine state not "
                f"updated. Next restart will recover from trades "
                f"DB."
            )

    def _ensure_recovered_protective_stop(
        self,
        *,
        snapshot: BrokerSnapshot,
        position: Position,
        decision: RiskDecision,
    ) -> None:
        """Place the intended stop immediately for a recovered entry if missing."""
        symbol = decision.symbol
        existing = self._protective_stop_order(symbol, snapshot)
        if existing is not None:
            if str(existing.time_in_force or "").lower() == "day":
                # P-5: look up position_uid for the replacement_stop
                # substrate row.
                _promote_uid: str | None = None
                if self.lifecycle_store is not None:
                    try:
                        _row = self.lifecycle_store.get_open_for_owner_key(
                            owner_key_for(symbol),
                        )
                        if _row is not None:
                            _promote_uid = _row.position_uid
                    except Exception as exc:
                        logger.debug(
                            f"promote-stop position_uid lookup raised "
                            f"{type(exc).__name__}: {exc} — proceeding"
                        )
                promoted = self.broker.promote_equity_stop_to_gtc(
                    parent_order_id=None,
                    stop_order_id=existing.order_id,
                    qty=abs(int(position.qty)),
                    stop_price=float(existing.stop_price),
                    client_order_id_prefix=(
                        f"{decision.strategy_name}-recover-stop-gtc"
                    ),
                    position_uid=_promote_uid,
                )
                snapshot.open_orders.remove(existing)
                snapshot.open_orders.append(promoted)
            return

        stop_qty = abs(int(position.qty))
        if stop_qty < 1:
            logger.warning(
                f"{symbol}: recovered position qty={position.qty} has no "
                "whole-share stop quantity; fractional remainder will rely on "
                "strategy exits until reduced or closed"
            )
            return

        # P-4: look up position_uid for the substrate write.
        _recover_uid: str | None = None
        if self.lifecycle_store is not None:
            try:
                _row = self.lifecycle_store.get_open_for_owner_key(
                    owner_key_for(symbol),
                )
                if _row is not None:
                    _recover_uid = _row.position_uid
            except Exception as exc:
                logger.debug(
                    f"recover-stop position_uid lookup raised "
                    f"{type(exc).__name__}: {exc} — proceeding without "
                    f"substrate row"
                )
        repaired = self.broker.place_protective_stop(
            symbol=symbol,
            qty=stop_qty,
            stop_price=decision.stop_price,
            client_order_id_prefix=f"{decision.strategy_name}-recover-stop",
            position_uid=_recover_uid,
        )
        snapshot.open_orders.append(repaired)
        logger.warning(
            f"{symbol}: restored protective stop immediately after suspect "
            f"order recovery at ${decision.stop_price:.2f}"
        )

    # P-7: _recover_suspect_exit_orders removed. Substrate exit row
    # → _maybe_dispatch_substrate_exit_fill replaces this path.

    # ── Post-fill bookkeeping ────────────────────────────────────────────

    def _record_fill(
        self,
        result: OrderResult,
        *,
        modeled_price: float,
        order_type: str = "market",
        side: str = "buy",
    ) -> None:
        """
        Feed the adverse-clamped fill slippage into the Phase 6 drift
        kill switch. Modeled fill = the arrival price at submission
        (NBBO mid); realized fill = what Alpaca actually gave us.

        MARKET orders only. LIMIT orders are skipped because arrival
        price is not a meaningful execution-quality benchmark for them —
        a resting limit at $100 filled at $95 looks like -500 bps against
        arrival but is a clean fill against the limit.

        The kill switch consumes **adverse-only** magnitude — the same
        quantity persisted as `slippage_adverse_bps` on `trades` (Phase 2
        slippage unification). We compute signed slippage via
        `single_leg_realized_slippage_bps` (positive = adverse fill /
        negative = price improvement) and clamp `max(0, signed)` before
        recording. A run of unusually good fills must not trip the
        drift halt — that's the conceptual bug shared with the L2 Health
        check's previous abs() semantics (PR follow-up after the
        credit_spread DEGRADED false positive on a sample of
        zero-slippage + price-improvement fills).
        """
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            return
        if order_type != "market":
            return
        if result.avg_fill_price is None or modeled_price <= 0:
            return
        modeled_bps = SLIPPAGE_MODEL_MARKET_BPS
        signed_bps = single_leg_realized_slippage_bps(
            side=side,
            reference_price=modeled_price,
            actual_fill_price=result.avg_fill_price,
        )
        adverse_bps = max(0.0, signed_bps)
        self.risk.record_fill_slippage(
            modeled_bps=modeled_bps, adverse_bps=adverse_bps
        )

    def _log_entry(
        self,
        decision: RiskDecision,
        result: OrderResult,
        modeled_price: float,
        *,
        record_slippage: bool = True,
        timestamp_override: datetime | None = None,
        benchmark_kind: str | None = None,
        measurement_quality: str | None = None,
    ) -> None:
        """Log an entry fill to the trade database.

        ``record_slippage=False`` is used by the recovered-entry-context
        path. When the engine reconstructs a position whose original
        arrival quote is unrecoverable (Issue A in the slippage PR), no
        honest pre-trade benchmark exists; writing NULL on both slippage
        columns is correct, vs. synthesizing a phantom number from the
        current bar close that would inflate the L2 health check's p95.

        ``timestamp_override`` is reserved for recovery / reconciliation
        paths. When Alpaca broker history exposes the original execution
        time (``filled_at``), recovered rows should use that broker time
        rather than the later cycle when the engine noticed and repaired
        the gap.

        ``benchmark_kind`` lets the call site declare whether
        ``modeled_price`` represents an arrival midpoint or a fallback
        (latest_close). Passed through to ``build_record`` for the new
        slippage taxonomy columns. See codepath §1 in
        docs/slippage_unification_design.md.

        ``measurement_quality`` is set to 'recovered' by the suspect-order
        recovery path (codepath §9). Default inference picks 'primary' or
        'fallback' based on benchmark_kind.
        """
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            return
        try:
            # Operator Controls Phase A — thread position_uid through
            # to the trade row when the broker attached one. None for
            # legacy/options/spread paths is harmless (column is
            # nullable and indexed for show-position joins).
            record = self.trade_logger.build_record(
                decision,
                result,
                modeled_price=modeled_price,
                position_uid=getattr(result, "position_uid", None),
                record_slippage=record_slippage,
                timestamp_override=timestamp_override,
                benchmark_kind=benchmark_kind,
                measurement_quality=measurement_quality,
            )
            self.trade_logger.log(record)
        except Exception as e:
            logger.error(f"trade logging failed: {e}")

    def _log_close(
        self,
        result: OrderResult,
        modeled_price: float,
        strategy_name: str = "",
        *,
        benchmark_kind: str | None = None,
        measurement_quality: str | None = None,
        timestamp_override: datetime | None = None,
        reason: str = "exit signal",
    ) -> None:
        """Log an exit fill to the trade database.

        ``benchmark_kind`` defaults to None which makes ``build_close_record``
        assume 'arrival_midpoint' (correct for normal discretionary
        exits). The fractional residual cleanup call site passes
        'unavailable' so the row honestly reports no benchmark — see
        codepath §7 in docs/slippage_unification_design.md.

        PR #56 R1: look up the open lifecycle row's position_uid so it
        gets persisted on the close row. Without this, restart
        reconstruction of the allocator's trade-count dedup state would
        fall through to "each row counts as one" for single-leg closes.
        """
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            return
        # Look up position_uid from the lifecycle store. Best-effort —
        # failures don't block the log write, but the record is
        # written with position_uid=None and restart dedup will treat
        # it as legacy.
        position_uid: str | None = None
        try:
            row = self.lifecycle_store.get_open_for_owner_key(
                owner_key_for(result.symbol),
            )
            if row is not None:
                position_uid = row.position_uid
        except Exception as exc:
            logger.debug(
                f"_log_close: position_uid lookup raised "
                f"{type(exc).__name__}: {exc} — proceeding without"
            )
        try:
            record = self.trade_logger.build_close_record(
                result,
                strategy_name=strategy_name or self.strategy.name,
                modeled_price=modeled_price,
                benchmark_kind=benchmark_kind,
                measurement_quality=measurement_quality,
                timestamp_override=timestamp_override,
                reason=reason,
                position_uid=position_uid,
            )
            self.trade_logger.log(record)
        except Exception as e:
            logger.error(f"trade logging (close) failed: {e}")

    def _record_recovered_exit_fill(
        self,
        *,
        symbol: str,
        owner: str,
        exit_fill,
        modeled_price: float = 0.0,
        benchmark_kind: str = "unavailable",
        alert_reason: str = "broker-history exit recovery",
        is_full_close: bool | None = None,
        external: bool = False,
        skip_trades_dedup_check: bool = False,
    ) -> bool:
        """Persist one broker-confirmed non-stop exit exactly once.

        ``skip_trades_dedup_check`` (PR #61 round-1 review fix P1):
        the substrate-driven exit dispatch calls this AFTER
        apply_order_event has already UPSERTed the trade row, so
        has_recorded_order_id returns True and the legacy dedup
        gate would short-circuit before firing P&L / alert /
        ownership cleanup. Substrate callers pass True; their
        idempotency comes from the dispatch's _has_position gate
        (the engine pops ownership at the end of this helper, so
        subsequent substrate observations see no position and
        skip the dispatch).

        Legacy callers (broker-history exit recovery, external-
        close confirmation) keep the default False — they predate
        the substrate write and need the has_recorded_order_id
        check to avoid double-writing.
        """
        order_id = getattr(exit_fill, "order_id", None)
        if not order_id:
            return False
        if (
            not skip_trades_dedup_check
            and self.trade_logger.has_recorded_order_id(order_id)
        ):
            return False
        price = getattr(exit_fill, "avg_fill_price", None)
        qty = float(getattr(exit_fill, "filled_qty", 0.0) or 0.0)
        if price is None or qty <= 0:
            return False
        result = (
            exit_fill
            if isinstance(exit_fill, OrderResult)
            else OrderResult(
                status=exit_fill.status,
                order_id=exit_fill.order_id,
                symbol=exit_fill.symbol,
                requested_qty=exit_fill.qty,
                filled_qty=exit_fill.filled_qty,
                avg_fill_price=exit_fill.avg_fill_price,
                raw_status=exit_fill.raw_status,
                submitted_at=exit_fill.submitted_at,
                filled_at=exit_fill.filled_at,
            )
        )
        quality = "recovered" if benchmark_kind != "unavailable" else "unavailable"
        self._record_fill(
            result,
            modeled_price=modeled_price,
            order_type="market",
            side="sell",
        )
        self._log_close(
            result,
            modeled_price,
            owner,
            benchmark_kind=benchmark_kind,
            measurement_quality=quality,
            timestamp_override=result.filled_at or result.submitted_at,
            reason=alert_reason,
        )
        if is_full_close is None:
            is_full_close = result.status is OrderStatus.FILLED
        self._record_realized_pnl(
            symbol,
            owner,
            float(price),
            qty,
            multiplier=100 if _OCC_PAT.match(result.symbol) else 1,
            external=external,
            is_full_close=is_full_close,
        )
        self.alerts.trade_executed(
            symbol=symbol,
            strategy=owner,
            side="sell",
            qty=qty,
            price=float(price),
            reason=alert_reason,
        )
        logger.warning(
            f"{symbol}: recovered missed exit fill from broker truth — "
            f"qty={qty} price={price} order_id={order_id}"
        )
        if is_full_close:
            self._cleanup_option_trailing_state(
                result.symbol,
                reason="broker-history exit recovery",
            )
        return True

    def _record_realized_pnl(
        self,
        symbol: str,
        strategy_name: str,
        close_price: float,
        qty: float,
        multiplier: int = 1,
        *,
        external: bool = False,
        is_full_close: bool = True,
    ) -> None:
        """
        Compute and report realized P&L for a closed position to the
        SleeveAllocator's HWM drawdown gate, and close the matching
        position_lifecycle row (Operator Controls Phase A).

        Called from all three close paths:
          - Signal-based exit (_process_symbol exit branch) — external=False
          - WebSocket stop-leg fill (_process_stream_stop_fills) — external=False
          - External close detection (_detect_external_closes) — external=True

        Pass multiplier=100 for options contracts (each contract = 100 shares).
        Equity callers omit it and get the default of 1.

        ``is_full_close`` controls whether the lifecycle row gets
        transitioned to a terminal status:

          - ``True``  (default): the close was full — mark the row
                      ``closed`` (or ``external_closed`` when
                      ``external=True``). All stop-fill / external-close
                      / fractional-residual call sites use this — the
                      semantic at those sites is always "position fully
                      gone."
          - ``False``: the broker reported a PARTIAL close result, so a
                      residual broker/engine position remains. The
                      lifecycle row stays open at the residual quantity
                      — ``_reduce_lifecycle_for_owner_key`` subtracts
                      the closed qty from ``current_qty`` via
                      ``mark_residual`` so the operator CLI shows
                      accurate size. Full partial-close accounting
                      (per-event realized R, ``net_realized_pnl``
                      accumulation) remains a Phase C concern per the
                      implementation plan.

        Startup restores entry prices for still-open positions from the trade log,
        so normal restart/reconcile flows continue feeding the HWM gate. If the
        entry price is still unavailable, the update is conservatively skipped.

        The lifecycle close is done first and is independent of whether
        the realized-PnL update can proceed — operator CLI accuracy
        must not depend on entry-price availability.
        """
        # Look up position_uid BEFORE the lifecycle close transition —
        # the row may be flipped to closed below and become harder to
        # find. The allocator uses this for trade-count deduplication
        # (partial closes of the same position must not double-count).
        position_uid: str | None = None
        try:
            row = self.lifecycle_store.get_open_for_owner_key(
                owner_key_for(symbol),
            )
            if row is not None:
                position_uid = row.position_uid
        except Exception as exc:
            logger.debug(
                f"[{strategy_name}] {symbol}: position_uid lookup raised "
                f"{type(exc).__name__}: {exc} — proceeding without"
            )

        # Operator Controls Phase A — update the matching lifecycle
        # row. Done first so it happens regardless of whether the
        # allocator update below proceeds. Best-effort: wrapped in
        # try/except so store failures never propagate into the close
        # path.
        if is_full_close:
            self._close_lifecycle_for_owner_key(
                owner_key=owner_key_for(symbol),
                external=external,
            )
        else:
            # Partial close — drop current_qty by the close qty so the
            # operator CLI shows the residual rather than the stale
            # entry quantity. If the reduction takes the row to zero
            # (shouldn't happen on a PARTIAL result, but defensive
            # against fill-event rounding) the helper falls back to a
            # full close so the row reaches a terminal status.
            self._reduce_lifecycle_for_owner_key(
                owner_key=owner_key_for(symbol),
                reduced_by=float(qty),
            )

        if self._allocator is None:
            return
        entry_price = self._entry_prices.get(symbol)
        if entry_price is None or entry_price <= 0.0 or qty <= 0:
            logger.debug(
                f"[{strategy_name}] {symbol}: skipping P&L update — "
                f"entry_price={entry_price} qty={qty}"
            )
            return
        realized_pnl = (close_price - entry_price) * qty * multiplier
        logger.debug(
            f"[{strategy_name}] {symbol}: realized_pnl={realized_pnl:+.2f} "
            f"({qty}x{multiplier} @ {close_price:.2f} vs entry {entry_price:.2f})"
        )
        # Pass position_uid + is_full_close so the allocator
        # deduplicates and only increments trade_count when the round
        # trip is complete (PR #56 R1 + R2 fixes). A partial close
        # event contributes to realized P&L but does NOT increment the
        # completed-trades counter — the round trip isn't done yet.
        self._allocator.record_realized_pnl(
            strategy_name, realized_pnl,
            position_uid=position_uid,
            is_full_close=is_full_close,
        )

    def _reduce_lifecycle_for_owner_key(
        self,
        *,
        owner_key: str,
        reduced_by: float,
    ) -> None:
        """Best-effort lifecycle reduction after an in-process partial
        close.

        Looks up the single open lifecycle row for ``owner_key``,
        subtracts ``reduced_by`` from its ``current_qty`` and writes
        the residual via ``mark_residual``. If the residual reaches
        zero (e.g. when fill-event rounding ends up at exactly the
        entry qty), falls back to ``mark_closed`` so the row reaches a
        terminal status rather than sitting at qty=0 indefinitely.

        Phase A scope: equity single-leg only. Spread / options
        partial reductions land with the Phase C lifecycle wiring
        for those workers. Wrapped in try/except so store failures
        never raise into the close path.
        """
        if self.lifecycle_store is None:
            return
        try:
            row = self.lifecycle_store.get_open_for_owner_key(owner_key)
            if row is None:
                return
            if row.position_type != "single_leg":
                return
            prior_qty = float(row.current_qty or 0.0)
            residual = prior_qty - float(reduced_by)
            if residual <= 0.0:
                # Defensive: the engine called us with is_full_close=False
                # but the math says the position is now flat. Mark closed.
                self.lifecycle_store.mark_closed(
                    position_uid=row.position_uid,
                    external=False,
                )
                logger.debug(
                    f"lifecycle: partial reduce zeroed out "
                    f"{row.position_uid[:18]}… ({owner_key}) — marked closed"
                )
                return
            self.lifecycle_store.mark_residual(
                position_uid=row.position_uid,
                current_qty=residual,
            )
            logger.debug(
                f"lifecycle: {row.position_uid[:18]}… ({owner_key}) "
                f"reduced to current_qty={residual} (was {prior_qty})"
            )
        except Exception as exc:
            logger.warning(
                f"lifecycle reduce failed for {owner_key}: {exc}"
            )

    def _close_lifecycle_for_owner_key(
        self,
        *,
        owner_key: str,
        external: bool = False,
    ) -> None:
        """Best-effort lifecycle close for an in-process exit.

        Looks up the single open lifecycle row for `owner_key` and
        transitions it to `closed` (or `external_closed` if `external`
        is True). Silently no-ops if no open row exists or if the
        lifecycle store is unavailable.

        This is what makes the operator CLI's `positions` accurate
        between restarts: every close path (signal exit, stop fill,
        external close) flows through `_record_realized_pnl`, which
        delegates here.
        """
        if self.lifecycle_store is None:
            return
        try:
            row = self.lifecycle_store.get_open_for_owner_key(owner_key)
            if row is None:
                return
            # Phase A scope: only act on equity single-leg rows. Spread
            # and options lifecycle close transitions are bundled into
            # Phase C with the rest of the options/spread lifecycle
            # wiring.
            if row.position_type != "single_leg":
                return
            self.lifecycle_store.mark_closed(
                position_uid=row.position_uid,
                external=external,
            )
            logger.debug(
                f"lifecycle: marked {row.position_uid[:18]}… "
                f"({owner_key}) "
                f"{'external_closed' if external else 'closed'}"
            )
        except Exception as exc:
            logger.warning(
                f"lifecycle close failed for {owner_key}: {exc}"
            )

    def _cleanup_option_trailing_state(
        self,
        raw_symbol: str,
        *,
        reason: str,
    ) -> None:
        """Delete durable option trailing metadata after a full OCC close.

        Scope post-§10.4: this helper deletes the *strategy state* row
        (entry_premium, hwm_premium, trail_*) on a full close. The
        per-order broker state for the underlying stop is now owned by
        the order-lifecycle substrate (``position_lifecycle_orders``),
        which reaches its own terminal status through ``apply_order_event``
        independently — this helper does not touch substrate rows.

        Pre-§10.4 this helper also cleaned the broker-state mirror
        (``alpaca_stop_order_id`` / ``stop_order_status``) embedded on
        the trailing row; the mirror is gone alongside the trailing row
        on delete, so behavior at the call sites is unchanged. The PR
        #69 recovery cleanup tests stay green.
        """
        if not is_occ_option(raw_symbol):
            return
        if self.option_trailing_store is None:
            return
        try:
            self.option_trailing_store.delete_by_occ(raw_symbol)
        except Exception as exc:
            logger.warning(
                f"{raw_symbol}: option trailing cleanup failed after "
                f"{reason}: {exc}"
            )

    # ── Operator Controls Phase A PR-2 — command queue + sticky halt ──
    # `operator_halt:` / `operator_halt_sticky:` are the two reason
    # prefixes _apply_operator_halt and _restore_sticky_halt_state set
    # on the RiskManager. Any other halt reason came from
    # daily-loss / hard-dollar / broker-error / slippage-drift gates
    # and must NOT be cleared by an operator resume.

    def _restore_control_state(self) -> None:
        """Re-engage halt / soft pauses from disk on startup.

        Reads ``settings.OPERATOR_CONTROL_STATE_PATH``. The file is
        written by `_persist_control_state` whenever the operator queue
        changes any flag (halt, pause-entries, pause-strategy). Absent
        file → no flags set (normal startup).

        Best-effort: file-format errors log a warning and continue with
        no flags. This prevents a malformed JSON from locking the bot
        out of startup. The operator can manually re-issue commands via
        the CLI if needed.

        **Intentionally independent of the operator command store.**
        The control-state JSON lives outside the SQLite DB precisely so
        DB corruption cannot defeat recovery. The only prerequisite is
        the risk manager — read the file, set the flags, done. (PR-2
        reviewer finding F3.)

        Phase B compatibility: a pre-Phase-B JSON (`{halted: true,
        reason: ..., command_uid: ...}`) is still valid input — the
        absent `entries_paused` and `paused_strategies` keys default
        to "not paused".
        """
        path = settings.OPERATOR_CONTROL_STATE_PATH
        try:
            if not os.path.exists(path):
                return
            with open(path, "r") as fh:
                state = json.load(fh)
            if not isinstance(state, dict):
                return
        except Exception as exc:
            logger.warning(
                f"control state restore skipped (path={path}): {exc}"
            )
            return

        # Halt restore (existing behavior, preserved).
        if state.get("halted"):
            try:
                reason = str(state.get("reason") or "sticky halt from prior session")
                command_uid = state.get("command_uid")
                note = f"operator_halt_sticky: {reason}"
                if command_uid:
                    note = f"{note} (cmd={command_uid[:18]}…)"
                self.risk._engage_kill_switch(note)
                logger.warning(f"sticky halt restored from {path}: {reason}")
            except Exception as exc:
                logger.warning(
                    f"sticky halt restore: kill switch engage failed: {exc}"
                )

        # Phase B — pause-entries restore.
        if state.get("entries_paused"):
            try:
                reason = str(state.get("entries_paused_reason") or "restored from prior session")
                command_uid = state.get("entries_paused_command_uid")
                self.risk.pause_entries(reason=reason, command_uid=command_uid)
                logger.warning(
                    f"pause-entries restored from {path}: {reason}"
                )
            except Exception as exc:
                logger.warning(
                    f"pause-entries restore: pause failed: {exc}"
                )

        # Phase B — pause-strategy restore.
        paused_strategies = state.get("paused_strategies") or {}
        if isinstance(paused_strategies, dict):
            for strategy_name, meta in paused_strategies.items():
                if not isinstance(meta, dict):
                    continue
                try:
                    reason = str(meta.get("reason") or "restored from prior session")
                    command_uid = meta.get("command_uid")
                    self.risk.pause_strategy(
                        strategy_name=strategy_name,
                        reason=reason,
                        command_uid=command_uid,
                    )
                    logger.warning(
                        f"pause-strategy '{strategy_name}' restored "
                        f"from {path}: {reason}"
                    )
                except Exception as exc:
                    logger.warning(
                        f"pause-strategy restore failed for "
                        f"{strategy_name!r}: {exc}"
                    )

    # Backward-compat alias retained so tests written against the
    # PR-2 method name keep working. Same behavior; the rename to
    # `_restore_control_state` reflects the broader scope (halt +
    # pauses, not just halt).
    _restore_sticky_halt_state = _restore_control_state

    def _persist_control_state(
        self,
        *,
        halt_command_uid: str | None = None,
        halt_reason_override: str | None = None,
    ) -> None:
        """Atomically snapshot RiskManager halt + pause state to disk.

        Called by every operator-halt / pause handler after it mutates
        RiskManager state. If no flag is set, the file is removed
        (absence = clean state). Otherwise the payload includes only
        the active flags plus their metadata.

        ``halt_command_uid`` is threaded through only because the
        RiskManager doesn't track which command engaged the halt
        (the kill switch was originally engaged for non-operator reasons
        too — daily-loss, hard-dollar, etc.). The operator-halt handler
        passes its own uid here so the persisted record carries audit
        identity. Pause uids come from RiskManager state directly
        because pauses are operator-only.

        ``halt_reason_override`` preserves the historical contract that
        the persisted JSON carries the BARE command reason (e.g.
        "market event") — not the prefixed form RiskManager stores
        ("operator_halt: market event"). The prefix is re-applied on
        restore. When None, the persister reads RiskManager directly
        (e.g. a Phase B pause persist that doesn't know the halt
        reason context).
        """
        path = settings.OPERATOR_CONTROL_STATE_PATH
        parent = os.path.dirname(path)
        if parent:
            try:
                os.makedirs(parent, exist_ok=True)
            except Exception as exc:
                logger.warning(
                    f"control state persist: makedirs failed: {exc}"
                )
                return

        # Compose the payload from current RiskManager state.
        payload: dict = {}
        if self.risk.is_halted():
            payload["halted"] = True
            payload["reason"] = (
                halt_reason_override
                if halt_reason_override is not None
                else (self.risk.halt_reason() or "")
            )
            payload["command_uid"] = halt_command_uid
        if self.risk.is_entries_paused():
            payload["entries_paused"] = True
            payload["entries_paused_reason"] = (
                self.risk.entries_paused_reason() or ""
            )
            payload["entries_paused_command_uid"] = getattr(
                self.risk, "_entries_paused_command_uid", None
            )
        paused = self.risk.paused_strategies_snapshot()
        if paused:
            payload["paused_strategies"] = paused
        if payload:
            payload["set_at"] = datetime.now(timezone.utc).isoformat()

        try:
            if payload:
                tmp = path + ".tmp"
                with open(tmp, "w") as fh:
                    json.dump(payload, fh, indent=2)
                os.replace(tmp, path)
            else:
                if os.path.exists(path):
                    os.remove(path)
        except Exception as exc:
            logger.warning(f"control state persist failed: {exc}")

    # Backward-compat alias for the old `_persist_sticky_halt(halted,
    # reason, command_uid)` signature. The new `_persist_control_state`
    # reads from RiskManager directly, so this shim ensures RiskManager
    # is consistent with the requested halt state before snapshotting.
    # New code should call `_persist_control_state` directly.
    def _persist_sticky_halt(
        self,
        *,
        halted: bool,
        reason: str | None,
        command_uid: str | None,
    ) -> None:
        # The handlers already set RiskManager state before calling
        # this — but the old contract took explicit args. Honour both:
        # if the requested halt state differs from the manager's
        # current state, the handler hasn't mutated risk yet, so we
        # do nothing here (the payload is built from RiskManager).
        # `halt_command_uid` is the only piece RiskManager doesn't
        # carry; thread it through.
        self._persist_control_state(
            halt_command_uid=command_uid if halted else None,
        )

    def _start_operator_heartbeat(self) -> None:
        """Spawn the daemon thread that drains operator commands.

        Phase B: replaces the per-cycle queue poll. Calls
        ``_process_operator_commands`` once every
        ``OPERATOR_COMMAND_HEARTBEAT_SECONDS`` seconds. Idempotent —
        safe to call once per engine start.
        """
        if self.operator_command_store is None:
            # No queue → nothing to poll. Skip silently; the engine
            # otherwise functions correctly.
            return
        if (
            self._operator_heartbeat_thread is not None
            and self._operator_heartbeat_thread.is_alive()
        ):
            return  # already running; defensive against double-start
        self._operator_heartbeat_stop.clear()
        thread = threading.Thread(
            target=self._operator_heartbeat_loop,
            name="operator-heartbeat",
            daemon=True,
        )
        self._operator_heartbeat_thread = thread
        thread.start()
        logger.info(
            f"operator heartbeat started "
            f"(interval={settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS}s)"
        )

    def _operator_heartbeat_loop(self) -> None:
        """Thread body: drain commands until `_running=False`.

        Wrapped so any exception inside `_process_operator_commands`
        logs and continues — the heartbeat must survive transient
        queue errors. Sleeping via `_operator_heartbeat_stop.wait()`
        lets `stop()` interrupt the sleep immediately for clean
        shutdown.
        """
        interval = max(1, int(settings.OPERATOR_COMMAND_HEARTBEAT_SECONDS))
        while self._running:
            try:
                self._process_operator_commands()
            except Exception as exc:
                logger.warning(
                    f"operator heartbeat: drain raised "
                    f"{type(exc).__name__}: {exc} (continuing)"
                )
            # Wait returns True when set; we exit immediately. False
            # means timeout — continue draining.
            if self._operator_heartbeat_stop.wait(timeout=interval):
                break
        logger.debug("operator heartbeat thread exiting")

    def _process_operator_commands(self) -> None:
        """Drain one queued operator command per call.

        Called by the heartbeat thread (Phase B). Phase A routes
        ``halt`` and ``resume-after-halt``; Phase B adds the four
        soft-pause actions. Every
        other action is terminated with
        ``status='rejected_unsupported_phase_a'`` so the operator sees
        an audited refusal rather than a silent no-op.

        Best-effort: a queue I/O failure logs a warning and never
        aborts the cycle. Same discipline as
        ``_reconcile_position_lifecycle`` and the trade-log paths.
        """
        if self.operator_command_store is None:
            return
        try:
            claimed = self.operator_command_store.claim_next_pending(
                expiry_seconds=settings.OPERATOR_COMMAND_EXPIRY_SECONDS,
            )
        except Exception as exc:
            logger.warning(f"operator queue: claim failed: {exc}")
            return
        if claimed is None:
            return

        logger.info(
            f"operator command: action={claimed.action} "
            f"cmd={claimed.command_uid[:18]}… by={claimed.requested_by} "
            f"reason={claimed.reason!r}"
        )

        try:
            if claimed.action == "halt":
                self._apply_operator_halt(claimed)
            elif claimed.action == "resume-after-halt":
                self._apply_operator_resume_after_halt(claimed)
            elif claimed.action == "pause-entries":
                self._apply_operator_pause_entries(claimed)
            elif claimed.action == "resume-entries":
                self._apply_operator_resume_entries(claimed)
            elif claimed.action == "pause-strategy":
                self._apply_operator_pause_strategy(claimed)
            elif claimed.action == "resume-strategy":
                self._apply_operator_resume_strategy(claimed)
            elif claimed.action == "close-position":
                self._apply_operator_close_position(claimed)
            elif claimed.action == "reduce-position":
                self._apply_operator_reduce_position(claimed)
            elif claimed.action == "cancel-position-orders":
                self._apply_operator_cancel_position_orders(claimed)
            else:
                # Defensive — claim_next_pending only returns rows
                # written via insert(), which validates against
                # VALID_ACTIONS. But future phases may extend the
                # table; be explicit until those handlers exist.
                self.operator_command_store.mark_rejected(
                    command_uid=claimed.command_uid,
                    status="rejected_unsupported_phase_a",
                    result={
                        "note": f"action {claimed.action!r} not implemented yet",
                    },
                )
                logger.warning(
                    f"operator command {claimed.command_uid[:18]}…: "
                    f"action {claimed.action!r} not supported in current phase"
                )
        except Exception as exc:
            logger.error(
                f"operator command {claimed.command_uid[:18]}…: "
                f"handler raised: {exc}"
            )
            try:
                self.operator_command_store.mark_failed(
                    command_uid=claimed.command_uid,
                    result={"error": str(exc)},
                )
            except Exception as inner:
                logger.warning(
                    f"operator command failed-mark also failed: {inner}"
                )

    def _apply_operator_halt(self, command) -> None:
        """Handle the ``halt`` operator command.

        Engages the existing kill switch via
        ``RiskManager._engage_kill_switch`` (no new halt state
        machine). Persists the sticky-halt JSON so the next restart
        re-engages immediately. Records ``succeeded`` with the
        engaged-state result.
        """
        prior_halted = bool(self.risk.is_halted())
        prior_reason = self.risk.halt_reason()
        note = f"operator_halt: {command.reason}"
        try:
            self.risk._engage_kill_switch(note)
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"kill switch engage failed: {exc}"},
            )
            return
        self._persist_control_state(
            halt_command_uid=command.command_uid,
            halt_reason_override=command.reason,  # bare reason, prefix added on restore
        )
        try:
            self.alerts.engine_halt(
                f"operator halt: {command.reason} "
                f"(cmd={command.command_uid[:18]}…)"
            )
        except Exception as exc:
            logger.warning(f"operator halt alert failed: {exc}")
        self.operator_command_store.mark_succeeded(
            command_uid=command.command_uid,
            result={
                "halted": True,
                "prior_halted": prior_halted,
                "prior_reason": prior_reason,
            },
        )
        logger.warning(f"engine halted by operator: {command.reason}")

    def _apply_operator_resume_after_halt(self, command) -> None:
        """Handle the ``resume-after-halt`` operator command.

        Per proposal §5.4 the resume path requires reconciliation
        before clearing the halt — we re-run the existing
        ``_reconcile_startup`` against a fresh snapshot. On
        RESTRICTED, the resume is refused and the operator is told to
        re-check state before re-issuing.

        On success: clears the kill switch via
        ``RiskManager.reset_kill_switches`` (existing primitive),
        deletes the sticky-halt file, records ``succeeded``.

        **F2 fix:** `RiskManager.reset_kill_switches` is a global
        clear — it erases daily-loss, hard-dollar, broker-error, and
        slippage-drift halts in addition to operator halts. We must
        refuse to resume unless the active halt is specifically an
        operator halt (reason matching `operator_halt:` or
        `operator_halt_sticky:`). A non-operator risk halt has its
        own recovery semantics and must not be cleared by the
        operator CLI as a side effect of a "resume" command.
        """
        # Refuse if no halt is engaged — the operator may be confused
        # about state. Audit the refusal rather than silently no-op.
        if not self.risk.is_halted():
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={"note": "no active halt to resume"},
            )
            logger.info(
                f"operator resume-after-halt {command.command_uid[:18]}…: "
                "no active halt"
            )
            # Clear any stale sticky-halt file defensively.
            self._persist_sticky_halt(halted=False, reason=None, command_uid=None)
            return

        # F2: refuse to resume if the active halt isn't an operator halt.
        # `RiskManager.reset_kill_switches` clears every halt cause, so
        # a permissive resume would silently erase independent risk
        # gates (daily-loss, hard-dollar, broker-error, slippage-drift)
        # the operator never intended to touch.
        current_reason = self.risk.halt_reason() or ""
        if not _is_operator_halt_reason(current_reason):
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={
                    "note": (
                        "active halt is not an operator halt — refusing "
                        "to resume because RiskManager.reset_kill_switches "
                        "would also clear independent risk-gate halts"
                    ),
                    "active_halt_reason": current_reason,
                },
            )
            logger.warning(
                f"operator resume-after-halt {command.command_uid[:18]}…: "
                f"refused — active halt is not an operator halt "
                f"(reason={current_reason!r})"
            )
            return

        # Re-reconcile against a fresh broker snapshot before clearing.
        try:
            snapshot = self.broker.sync_with_broker(
                session_start_equity=self._session_start_equity
            )
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"sync_with_broker failed: {exc}"},
            )
            return
        try:
            conflict_symbols = self._restore_ownership_from_db(snapshot)
            mode = self._reconcile_startup(snapshot, conflict_symbols)
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"reconcile failed: {exc}"},
            )
            return
        if mode != "NORMAL":
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={
                    "note": "reconciliation did not yield NORMAL mode",
                    "mode": mode,
                },
            )
            logger.warning(
                f"operator resume-after-halt {command.command_uid[:18]}…: "
                f"refused — reconcile mode={mode}"
            )
            return

        try:
            self.risk.reset_kill_switches()
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"reset_kill_switches failed: {exc}"},
            )
            return
        self._persist_sticky_halt(halted=False, reason=None, command_uid=None)
        try:
            self.alerts.engine_halt(
                f"operator resume-after-halt: {command.reason} "
                f"(cmd={command.command_uid[:18]}…) — kill switch cleared"
            )
        except Exception as exc:
            logger.warning(f"operator resume alert failed: {exc}")
        self.operator_command_store.mark_succeeded(
            command_uid=command.command_uid,
            result={"halted": False, "reconcile_mode": mode},
        )
        logger.info(f"engine resumed by operator: {command.reason}")

    def _lookup_position_uid_for_owner(self, owner_key: str) -> str | None:
        """Phase B §17.2 — uid lookup for exit alerts.

        Looks up the currently-open lifecycle row for an owner_key and
        returns its position_uid. Returns None when the lifecycle store
        is unavailable (legacy build) or no open row exists (pre-PR-1
        position not yet backfilled). The alert helper accepts uid=None
        gracefully — the message simply omits the `[pos_...]` suffix.
        """
        if self.lifecycle_store is None or not owner_key:
            return None
        try:
            row = self.lifecycle_store.get_open_for_owner_key(owner_key)
            return row.position_uid if row is not None else None
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"position_uid lookup failed for {owner_key!r}: {exc}")
            return None

    # ── Operator soft pauses (Phase B) ────────────────────────────────
    #
    # Soft pauses are flag flips on the RiskManager — they do NOT engage
    # the kill switch. The engine-side handlers:
    #   1. Mutate RiskManager state (pause_entries / pause_strategy / ...)
    #   2. Call `_persist_control_state()` so the next restart re-applies
    #   3. Fire an info alert (non-CRITICAL — soft pauses are operational
    #      gestures, not emergencies)
    #   4. Transition the queue row to `succeeded`
    #
    # All four handlers are idempotent at the RiskManager layer: if the
    # operator re-issues `pause-entries` when already paused, RiskManager
    # returns False from pause_entries() and the result-json carries
    # `already_paused: true`. The command still records as `succeeded`
    # because the desired end-state is met.

    def _apply_operator_pause_entries(self, command) -> None:
        """Handle ``pause-entries``: globally block new entries.

        Does NOT engage the kill switch — soft pause and halt are
        independent gates (halt is the stricter outer gate; pause-entries
        layers under it). Exits, stops, reconciliation, allocator
        updates, and lifecycle reconciles continue.
        """
        try:
            state_changed = self.risk.pause_entries(
                reason=command.reason,
                command_uid=command.command_uid,
            )
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"pause_entries failed: {exc}"},
            )
            return
        self._persist_control_state()
        try:
            # PR-65 review F4: INFO-severity operator_action alert,
            # not the CRITICAL engine_halt. A routine pause-entries is
            # not an emergency.
            self.alerts.operator_action(
                f"operator pause-entries: {command.reason} "
                f"(cmd={command.command_uid[:18]}…) — new entries blocked"
            )
        except Exception as exc:
            logger.warning(f"pause-entries alert failed: {exc}")
        self.operator_command_store.mark_succeeded(
            command_uid=command.command_uid,
            result={
                "entries_paused": True,
                "already_paused": not state_changed,
            },
        )
        logger.warning(f"entries paused by operator: {command.reason}")

    def _apply_operator_resume_entries(self, command) -> None:
        """Handle ``resume-entries``: clear the global pause flag.

        Unlike `resume-after-halt`, no reconciliation is required —
        soft pause is a flag flip. The next entry attempt stops being
        gated by the pause check.
        """
        try:
            state_changed = self.risk.resume_entries()
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"resume_entries failed: {exc}"},
            )
            return
        self._persist_control_state()
        try:
            # PR-65 review F4: INFO-severity operator_action alert.
            self.alerts.operator_action(
                f"operator resume-entries: {command.reason} "
                f"(cmd={command.command_uid[:18]}…) — entries unblocked"
            )
        except Exception as exc:
            logger.warning(f"resume-entries alert failed: {exc}")
        self.operator_command_store.mark_succeeded(
            command_uid=command.command_uid,
            result={
                "entries_paused": False,
                "already_resumed": not state_changed,
            },
        )
        logger.info(f"entries resumed by operator: {command.reason}")

    def _apply_operator_pause_strategy(self, command) -> None:
        """Handle ``pause-strategy``: block new entries for one strategy.

        The strategy name is carried in ``command.target_strategy``.
        Other strategies and existing position management for the
        paused strategy continue unaffected.
        """
        strategy_name = (command.target_strategy or "").strip()
        if not strategy_name:
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={"note": "target_strategy is required for pause-strategy"},
            )
            logger.warning(
                f"pause-strategy {command.command_uid[:18]}…: "
                "no target_strategy supplied"
            )
            return

        try:
            state_changed = self.risk.pause_strategy(
                strategy_name=strategy_name,
                reason=command.reason,
                command_uid=command.command_uid,
            )
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"pause_strategy failed: {exc}"},
            )
            return
        self._persist_control_state()
        try:
            # PR-65 review F4: INFO-severity operator_action alert.
            self.alerts.operator_action(
                f"operator pause-strategy {strategy_name}: {command.reason} "
                f"(cmd={command.command_uid[:18]}…) — "
                f"{strategy_name} entries blocked"
            )
        except Exception as exc:
            logger.warning(f"pause-strategy alert failed: {exc}")
        self.operator_command_store.mark_succeeded(
            command_uid=command.command_uid,
            result={
                "strategy": strategy_name,
                "paused": True,
                "already_paused": not state_changed,
            },
        )
        logger.warning(
            f"strategy '{strategy_name}' paused by operator: {command.reason}"
        )

    def _apply_operator_resume_strategy(self, command) -> None:
        """Handle ``resume-strategy``: clear the per-strategy pause."""
        strategy_name = (command.target_strategy or "").strip()
        if not strategy_name:
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={"note": "target_strategy is required for resume-strategy"},
            )
            logger.warning(
                f"resume-strategy {command.command_uid[:18]}…: "
                "no target_strategy supplied"
            )
            return

        try:
            state_changed = self.risk.resume_strategy(strategy_name=strategy_name)
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"resume_strategy failed: {exc}"},
            )
            return
        self._persist_control_state()
        try:
            # PR-65 review F4: INFO-severity operator_action alert.
            self.alerts.operator_action(
                f"operator resume-strategy {strategy_name}: {command.reason} "
                f"(cmd={command.command_uid[:18]}…) — "
                f"{strategy_name} entries unblocked"
            )
        except Exception as exc:
            logger.warning(f"resume-strategy alert failed: {exc}")
        self.operator_command_store.mark_succeeded(
            command_uid=command.command_uid,
            result={
                "strategy": strategy_name,
                "paused": False,
                "already_resumed": not state_changed,
            },
        )
        logger.info(
            f"strategy '{strategy_name}' resumed by operator: {command.reason}"
        )

    # ── Operator destructive position controls (Phase C) ──────────────
    #
    # Three actions: close-position / reduce-position / cancel-position-orders.
    # All three are driven by `target_position_uid` on the command row;
    # the operator CLI requires a `--confirm <position_uid_short>` token
    # so a typo cannot fire a state change against the wrong lifecycle.
    #
    # The handlers share a validation+lock prologue (`_destructive_setup`):
    #
    #   1. Look up the lifecycle row by position_uid; reject if absent
    #      or already terminal.
    #   2. Resolve the broker position by owner_key; reject if absent
    #      (the lifecycle and broker disagree — operator should
    #      investigate before mutating).
    #   3. Acquire the symbol-lock with `kind='operator_command'` and
    #      `identifier=command.command_uid`. On conflict reject with
    #      `rejected_validation` and the existing holder's identity.
    #   4. Return (lifecycle_row, broker_position, lock_holder) to the
    #      handler. The handler is responsible for releasing the lock
    #      in its own finally clause.
    #
    # The post-action accounting (PnL, allocator, lifecycle close) flows
    # through the existing `_record_realized_pnl` path so operator-
    # driven exits look identical to automatic exits downstream
    # (proposal §13 Phase C invariant). The broker's close_position
    # submit path will tag the substrate row with
    # `origin_kind='operator'` + `operator_command_uid` so the audit
    # trail is durable on the per-order table.

    def _destructive_setup(
        self,
        command,
    ) -> tuple[object | None, object | None, object | None]:
        """Shared validation + lock acquisition for destructive
        handlers.

        Returns ``(lifecycle_row, broker_position, lock_holder)`` on
        success and ``(None, None, None)`` after writing a rejection
        / failure on the command. Handlers must release the returned
        ``lock_holder`` in their finally clause; the lock is
        engine-process-local and acquire/release pairs are required for
        correctness across cycle/heartbeat threads.
        """
        if not command.target_position_uid:
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={"note": "target_position_uid is required"},
            )
            logger.warning(
                f"{command.action} {command.command_uid[:18]}…: "
                "no target_position_uid supplied"
            )
            return None, None, None

        if self.lifecycle_store is None:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": "lifecycle store not initialised"},
            )
            return None, None, None

        try:
            lifecycle_row = self.lifecycle_store.get_by_position_uid(
                command.target_position_uid
            )
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"lifecycle lookup failed: {exc}"},
            )
            return None, None, None

        if lifecycle_row is None:
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={"note": f"unknown position_uid {command.target_position_uid!r}"},
            )
            return None, None, None

        # PR-66 review F3: v1 destructive controls are equity / single-
        # leg-options only (matches the broker.close_position scope).
        # An operator targeting a spread position_uid would otherwise
        # close one OCC leg via close_position(symbol), breaking the
        # spread atomicity. Spread destructive is a separate PR
        # alongside the spread-lifecycle PR.
        if lifecycle_row.position_type != "single_leg":
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={
                    "note": (
                        f"destructive controls v1 support single_leg "
                        f"only; got position_type="
                        f"{lifecycle_row.position_type!r}. Spread/MLEG "
                        "destructive controls are deferred to the "
                        "spread-lifecycle PR."
                    ),
                    "position_type": lifecycle_row.position_type,
                },
            )
            return None, None, None

        # Terminal-state guard — proposal §9 / §14 invariants.
        if lifecycle_row.status in {
            "closed", "external_closed", "canceled", "error",
        }:
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={
                    "note": (
                        f"position is already {lifecycle_row.status}; "
                        "cannot mutate a terminal lifecycle"
                    ),
                    "lifecycle_status": lifecycle_row.status,
                },
            )
            return None, None, None

        # Broker position presence — operator should not act on a
        # lifecycle whose broker side is gone (the next reverse
        # reconcile will mark it external_closed; meanwhile we don't
        # have a position to cancel/close/reduce).
        try:
            snapshot = self.broker.sync_with_broker(
                session_start_equity=self._session_start_equity,
            )
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"sync_with_broker failed: {exc}"},
            )
            return None, None, None

        broker_position = snapshot.account.open_positions.get(
            lifecycle_row.symbol
        )
        # For options/spreads we MAY have multiple legs; broker_position
        # being None when the lifecycle is single_leg + open is a real
        # mismatch. For Phase C v1 we only support single-leg positions
        # (proposal §13 Phase C; spread destructive is a separate PR
        # alongside the spread lifecycle PR).
        if broker_position is None:
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={
                    "note": (
                        f"broker has no open position for "
                        f"{lifecycle_row.symbol!r}; lifecycle and broker "
                        "are out of sync — investigate before mutating"
                    ),
                    "lifecycle_status": lifecycle_row.status,
                },
            )
            return None, None, None

        # Acquire the symbol lock.
        holder = self.symbol_locks.acquire(
            owner_key=lifecycle_row.owner_key,
            kind="operator_command",
            identifier=command.command_uid,
        )
        if holder is None:
            existing = self.symbol_locks.is_locked(lifecycle_row.owner_key)
            self.operator_command_store.mark_rejected(
                command_uid=command.command_uid,
                status="rejected_validation",
                result={
                    "note": (
                        f"owner_key {lifecycle_row.owner_key!r} is "
                        f"already locked by {existing!s}; retry once "
                        "the in-flight action terminates"
                    ),
                    "lock_holder": str(existing) if existing else None,
                },
            )
            return None, None, None

        return lifecycle_row, broker_position, holder

    def _apply_operator_cancel_position_orders(self, command) -> None:
        """Handle ``cancel-position-orders``: cancel every non-terminal
        protective_stop / replacement_stop / exit / partial_close row
        for the position. Does NOT submit any new orders.

        Useful before a manual close (to remove a stale protective
        stop) or when broker-side state has drifted from the engine's
        view.
        """
        lifecycle_row, _broker_pos, holder = self._destructive_setup(command)
        if lifecycle_row is None:
            return

        cancelled: list[dict] = []
        errors: list[dict] = []
        try:
            if self.lifecycle_orders_store is None:
                self.operator_command_store.mark_failed(
                    command_uid=command.command_uid,
                    result={"error": "lifecycle_orders_store not initialised"},
                )
                return

            non_terminal = self.lifecycle_orders_store.get_non_terminal_for_position(
                lifecycle_row.position_uid
            )
            # We only cancel sell-side / stop-side rows. Entry rows
            # (`entry_primary` / `entry_residual`) belong to the bot's
            # opening flow and should NOT be cancelled by this command
            # — that would orphan the position.
            cancellable_roles = {
                "protective_stop", "replacement_stop",
                "exit", "partial_close",
            }
            for row in non_terminal:
                if row.role not in cancellable_roles:
                    continue
                if row.order_id is None:
                    # Row exists but submission hasn't completed; the
                    # foundation's lifecycle-attach queue will write
                    # the order_id when the broker confirms. We can't
                    # cancel without an id; skip with a note.
                    errors.append({
                        "row_id": row.id,
                        "role": row.role,
                        "error": "order_id not yet attached; cannot cancel",
                    })
                    continue
                try:
                    ok = self.broker.cancel_order(row.order_id)
                    cancelled.append({
                        "order_id": row.order_id,
                        "role": row.role,
                        "broker_ok": bool(ok),
                    })
                except Exception as exc:
                    errors.append({
                        "order_id": row.order_id,
                        "role": row.role,
                        "error": str(exc),
                    })

            # PR-66 review F6: differentiate full-success from partial-
            # or full-failure so the operator knows whether the stale
            # orders are actually gone.
            #   - all cancels broker_ok=True and no errors → succeeded
            #   - any cancel returned False or raised, but at least one
            #     succeeded → succeeded with degraded=True flag
            #   - zero successful cancels (all failed/errored) → failed
            successful_cancels = [c for c in cancelled if c["broker_ok"]]
            broker_rejected_cancels = [c for c in cancelled if not c["broker_ok"]]
            has_any_failure = bool(errors) or bool(broker_rejected_cancels)

            if not non_terminal:
                # No cancellable rows to begin with — that's a successful
                # no-op (operator's expectation was already satisfied).
                self.operator_command_store.mark_succeeded(
                    command_uid=command.command_uid,
                    result={
                        "position_uid": lifecycle_row.position_uid,
                        "owner_key": lifecycle_row.owner_key,
                        "cancelled": [],
                        "errors": [],
                        "note": "no non-terminal sell-side orders found",
                    },
                )
            elif not successful_cancels and has_any_failure:
                # All requested cancels failed — operator's stop/exit
                # is still live at the broker.
                self.operator_command_store.mark_failed(
                    command_uid=command.command_uid,
                    result={
                        "position_uid": lifecycle_row.position_uid,
                        "owner_key": lifecycle_row.owner_key,
                        "cancelled": cancelled,
                        "errors": errors,
                        "note": (
                            "every requested cancel failed; stale orders "
                            "remain live at the broker — investigate "
                            "before retrying"
                        ),
                    },
                )
            elif has_any_failure:
                # Partial failure — some cancels succeeded, others
                # didn't. The operator needs to know which ones.
                self.operator_command_store.mark_succeeded(
                    command_uid=command.command_uid,
                    result={
                        "position_uid": lifecycle_row.position_uid,
                        "owner_key": lifecycle_row.owner_key,
                        "cancelled": cancelled,
                        "errors": errors,
                        "degraded": True,
                        "note": (
                            "some cancels succeeded, others failed or "
                            "returned False from the broker; review the "
                            "errors / broker_ok fields"
                        ),
                    },
                )
            else:
                self.operator_command_store.mark_succeeded(
                    command_uid=command.command_uid,
                    result={
                        "position_uid": lifecycle_row.position_uid,
                        "owner_key": lifecycle_row.owner_key,
                        "cancelled": cancelled,
                        "errors": [],
                    },
                )
            try:
                degraded_tag = ""
                if has_any_failure and successful_cancels:
                    degraded_tag = " DEGRADED"
                elif has_any_failure:
                    degraded_tag = " FAILED"
                self.alerts.operator_action(
                    f"operator cancel-position-orders{degraded_tag}: "
                    f"{lifecycle_row.symbol} "
                    f"({lifecycle_row.position_uid[:18]}…) "
                    f"— {len(successful_cancels)} cancelled, "
                    f"{len(broker_rejected_cancels) + len(errors)} failed"
                )
            except Exception as exc:
                logger.warning(f"cancel-position-orders alert failed: {exc}")
            logger.warning(
                f"cancel-position-orders by operator: "
                f"{lifecycle_row.symbol} ok={len(successful_cancels)} "
                f"failed={len(broker_rejected_cancels) + len(errors)} "
                f"reason={command.reason!r}"
            )
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"cancel-position-orders failed: {exc}"},
            )
        finally:
            self.symbol_locks.release(
                owner_key=lifecycle_row.owner_key, holder=holder,
            )

    def _apply_operator_close_position(self, command) -> None:
        """Handle ``close-position``: full close of one lifecycle by uid.

        Flow per proposal §11:
          1. Validate + acquire lock via _destructive_setup.
          2. If a non-terminal close-side row already exists, reject
             with a clear message — operator should cancel the in-flight
             close first via cancel-position-orders.
          3. Cancel any non-terminal protective_stop row first so the
             close order doesn't race the stop.
          4. Submit a market/marketable close via broker.close_position.
             The broker writes a substrate row with role='exit',
             origin_kind='operator', operator_command_uid=command.uid.
          5. Wait for the close fill via the heartbeat-driven substrate
             dispatch (TIMEOUT path returns the queue row as failed).
          6. Run the existing _record_realized_pnl path so allocator +
             PnL accounting match automatic exits (proposal §13
             invariant).
          7. Release lock; mark command succeeded.
        """
        lifecycle_row, broker_pos, holder = self._destructive_setup(command)
        if lifecycle_row is None:
            return

        try:
            # In-flight close guard — proposal §11 + §14.
            # The DB's uniq_one_active_close_per_position partial
            # unique index will also block, but failing fast here gives
            # the operator a cleaner audit message.
            if self.lifecycle_orders_store is not None:
                non_terminal = self.lifecycle_orders_store.get_non_terminal_for_position(
                    lifecycle_row.position_uid
                )
                close_roles = {"exit", "partial_close"}
                pending_close = [r for r in non_terminal if r.role in close_roles]
                if pending_close:
                    self.operator_command_store.mark_rejected(
                        command_uid=command.command_uid,
                        status="rejected_validation",
                        result={
                            "note": (
                                "a close-side order is already in flight; "
                                "issue cancel-position-orders first to "
                                "abort it, then retry"
                            ),
                            "pending_close_order_ids": [
                                r.order_id for r in pending_close
                            ],
                        },
                    )
                    return

                # Cancel any protective stop before sending the close —
                # proposal §11.
                stop_roles = {"protective_stop", "replacement_stop"}
                for r in non_terminal:
                    if r.role not in stop_roles or r.order_id is None:
                        continue
                    try:
                        self.broker.cancel_order(r.order_id)
                    except Exception as exc:
                        logger.warning(
                            f"close-position {command.command_uid[:18]}…: "
                            f"failed to cancel stop {r.order_id}: {exc} "
                            "(continuing; broker will reconcile)"
                        )

            # Submit the close. The broker's close_position attaches
            # operator_command_uid to the substrate row written by
            # foundation's submit-time insert path.
            try:
                result = self.broker.close_position(
                    lifecycle_row.symbol,
                    # PR-66 review F2: position_uid is required for the
                    # broker's exit substrate write — without it,
                    # _lifecycle_orders_record_exit no-ops and the
                    # promised origin_kind='operator' /
                    # operator_command_uid tagging never reaches the
                    # per-order table.
                    position_uid=lifecycle_row.position_uid,
                    operator_command_uid=command.command_uid,
                )
            except Exception as exc:
                self.operator_command_store.mark_failed(
                    command_uid=command.command_uid,
                    result={"error": f"broker.close_position raised: {exc}"},
                )
                return

            # PR-66 review F1: gate accounting on result.status.
            # Mirror _close_single_leg_position — a REJECTED, TIMEOUT,
            # UNKNOWN, or zero-fill result must NOT advance lifecycle
            # state, must NOT call _record_realized_pnl, and must NOT
            # mark the command succeeded. Otherwise a failed close at
            # the broker would still locally close the lifecycle at
            # $0.00 PnL and tell the operator "succeeded".
            if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
                self.operator_command_store.mark_failed(
                    command_uid=command.command_uid,
                    result={
                        "position_uid": lifecycle_row.position_uid,
                        "owner_key": lifecycle_row.owner_key,
                        "broker_status": getattr(result.status, "value", str(result.status)),
                        "broker_order_id": result.order_id,
                        "raw_status": result.raw_status,
                        "message": result.message,
                        "note": (
                            "broker close did not produce a fill; "
                            "lifecycle and PnL accounting were NOT "
                            "advanced. The operator may retry once the "
                            "broker state stabilises."
                        ),
                    },
                )
                logger.error(
                    f"close-position by operator: {lifecycle_row.symbol} "
                    f"broker returned status={result.status} "
                    f"(filled_qty={result.filled_qty}); "
                    f"lifecycle and accounting NOT advanced"
                )
                return

            close_price = result.avg_fill_price or 0.0
            close_qty = float(result.filled_qty or 0.0)
            # PR-66 review N: same defensive zero-fill guard as
            # reduce-position. A FILLED / PARTIAL status with zero
            # filled_qty is near-impossible at the broker, but if it
            # somehow surfaces here we must NOT advance accounting —
            # _record_realized_pnl with qty=0 would log a phantom
            # zero-PnL close.
            if close_qty <= 0:
                self.operator_command_store.mark_failed(
                    command_uid=command.command_uid,
                    result={
                        "position_uid": lifecycle_row.position_uid,
                        "symbol": lifecycle_row.symbol,
                        "note": "result.filled_qty<=0 despite FILLED/PARTIAL status",
                        "broker_status": getattr(result.status, "value", str(result.status)),
                        "broker_order_id": result.order_id,
                    },
                )
                logger.error(
                    f"close-position by operator: {lifecycle_row.symbol} "
                    f"status={result.status} but filled_qty<=0; "
                    f"lifecycle and accounting NOT advanced"
                )
                return
            # PARTIAL on a full close: residual remains at the broker.
            # Treat this as a REDUCE (is_full_close=False) so the
            # lifecycle row's current_qty drops by the partial fill
            # but does NOT transition to closed. The operator should
            # re-issue close-position to finish the job, OR rely on
            # the normal exit path on the next cycle.
            is_full_close = result.status is OrderStatus.FILLED

            # Accounting reintegration (proposal §13 Phase C invariant):
            # operator-driven exits flow through the same accounting
            # path as automatic exits so the SleeveAllocator HWM gate
            # + realized PnL look identical downstream.
            self._record_realized_pnl(
                symbol=lifecycle_row.symbol,
                strategy_name=lifecycle_row.strategy,
                close_price=close_price,
                qty=close_qty,
                multiplier=100 if _OCC_PAT.match(lifecycle_row.symbol) else 1,
                external=False,
                is_full_close=is_full_close,
            )

            # PR-66 review P2#1: a PARTIAL close-position fill leaves
            # residual at the broker with the same protection gap as a
            # reduce-position partial — broker.close_position cancelled
            # the sibling protective stop before submitting, so the
            # residual sits unprotected until the next cycle's
            # _repair_missing_protective_stops pass. Mirror the
            # reduce-position contract: report 'degraded' +
            # protection_status so the operator sees the gap
            # immediately. A clean FULL close has no residual, so no
            # degraded flag.
            result_payload = {
                "position_uid": lifecycle_row.position_uid,
                "owner_key": lifecycle_row.owner_key,
                "symbol": lifecycle_row.symbol,
                "close_price": close_price,
                "close_qty": close_qty,
                "is_full_close": is_full_close,
                "broker_status": getattr(result.status, "value", str(result.status)),
                "broker_order_id": result.order_id,
            }
            if not is_full_close:
                result_payload["degraded"] = True
                result_payload["protection_status"] = "pending_repair_cycle"
                result_payload["protection_note"] = (
                    "broker.close_position cancelled the protective "
                    "stop before the partial close; residual is "
                    "unprotected until the engine's per-cycle stop "
                    "repair pass restores it, OR until the operator "
                    "re-issues close-position to finish the job"
                )
            # Mark queue row succeeded. The lifecycle row's pending→closed
            # transition runs inside _record_realized_pnl via
            # _close_lifecycle_for_owner_key (when is_full_close=True);
            # on PARTIAL the row stays open at the residual qty.
            self.operator_command_store.mark_succeeded(
                command_uid=command.command_uid,
                result=result_payload,
            )
            try:
                degraded_tag = (
                    "" if is_full_close
                    else " — DEGRADED: residual unprotected until "
                         "next cycle's stop repair"
                )
                self.alerts.operator_action(
                    f"operator close-position "
                    f"({'FULL' if is_full_close else 'PARTIAL'}): "
                    f"{lifecycle_row.symbol} "
                    f"({lifecycle_row.position_uid[:18]}…) "
                    f"@ ${close_price:.2f} × {close_qty}"
                    f"{degraded_tag} — {command.reason}"
                )
            except Exception as exc:
                logger.warning(f"close-position alert failed: {exc}")
            degraded_log = (
                "" if is_full_close
                else " DEGRADED protection until repair-cycle"
            )
            logger.warning(
                f"close-position by operator "
                f"({'FULL' if is_full_close else 'PARTIAL'}): "
                f"{lifecycle_row.symbol} close_qty={close_qty} "
                f"close_price={close_price}{degraded_log} "
                f"reason={command.reason!r}"
            )
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"close-position handler failed: {exc}"},
            )
        finally:
            self.symbol_locks.release(
                owner_key=lifecycle_row.owner_key, holder=holder,
            )

    def _apply_operator_reduce_position(self, command) -> None:
        """Handle ``reduce-position``: partial close by pct.

        Flow per proposal §10 + §11:
          1. Validate + acquire lock via _destructive_setup.
          2. Read --pct from command.params (default 50 if absent).
          3. Compute reduce_qty from broker_pos.qty at command-execution
             time (NOT at CLI submit time). Round down for whole-share
             equities; reject if reduce_qty rounds to zero. Reject if
             reduce_qty >= broker_pos.qty (use close-position instead).
          4. Same pending-close / stop-cancel prologue as close-position.
          5. Submit a market reduce via broker. Substrate row tagged
             role='partial_close', origin_kind='operator', uid.
          6. On fill: call _record_realized_pnl with
             is_full_close=False so the lifecycle's current_qty drops
             to residual via _reduce_lifecycle_for_owner_key.
          7. Re-submit a protective stop for the residual? Phase C
             leaves stop recreation to the next cycle's automatic stop
             repair path (engine/trader.py:_repair_missing_protective_stops)
             rather than racing here — the broker may have lots of
             intermediate state during the reduce, and the repair pass
             is already exercised in production.

        Phase C scope: equity single-leg only (matches the broker
        helper's existing path). Spread/option reduce is out of scope
        per the proposal §10 deferrals.
        """
        lifecycle_row, broker_pos, holder = self._destructive_setup(command)
        if lifecycle_row is None:
            return

        try:
            # --pct comes through params_json on the command row. The
            # CLI carries it; we validate the type + range here.
            params = command.params or {}
            try:
                pct = float(params.get("pct", 50))
            except (TypeError, ValueError):
                self.operator_command_store.mark_rejected(
                    command_uid=command.command_uid,
                    status="rejected_validation",
                    result={"note": "params.pct must be numeric"},
                )
                return
            if not (0 < pct < 100):
                self.operator_command_store.mark_rejected(
                    command_uid=command.command_uid,
                    status="rejected_validation",
                    result={"note": f"params.pct must be in (0, 100); got {pct}"},
                )
                return

            current_qty = float(broker_pos.qty or 0.0)
            if current_qty <= 0:
                self.operator_command_store.mark_rejected(
                    command_uid=command.command_uid,
                    status="rejected_validation",
                    result={"note": "broker reports non-positive qty"},
                )
                return

            # Quantity rounding per proposal §10. Whole-share for now;
            # fractional positions will need TIF + order-type
            # constraint checking before they can take fractional close
            # — leave fractional reduce out of Phase C v1.
            raw_reduce = current_qty * pct / 100.0
            import math
            reduce_qty = math.floor(raw_reduce)
            if reduce_qty <= 0:
                self.operator_command_store.mark_rejected(
                    command_uid=command.command_uid,
                    status="rejected_validation",
                    result={
                        "note": (
                            f"computed reduce qty rounds to zero "
                            f"(pct={pct}, current_qty={current_qty})"
                        ),
                    },
                )
                return
            if reduce_qty >= current_qty:
                self.operator_command_store.mark_rejected(
                    command_uid=command.command_uid,
                    status="rejected_validation",
                    result={
                        "note": (
                            f"computed reduce qty ({reduce_qty}) covers "
                            f"the full position ({current_qty}); use "
                            "close-position instead"
                        ),
                    },
                )
                return

            # In-flight close guard (same as close-position).
            if self.lifecycle_orders_store is not None:
                non_terminal = self.lifecycle_orders_store.get_non_terminal_for_position(
                    lifecycle_row.position_uid
                )
                close_roles = {"exit", "partial_close"}
                pending_close = [r for r in non_terminal if r.role in close_roles]
                if pending_close:
                    self.operator_command_store.mark_rejected(
                        command_uid=command.command_uid,
                        status="rejected_validation",
                        result={
                            "note": (
                                "a close-side order is already in flight; "
                                "cancel it first"
                            ),
                        },
                    )
                    return

            # Submit the reduce. Foundation's broker close path with a
            # qty override produces a substrate row tagged
            # role='partial_close' when partial_qty is supplied.
            try:
                result = self.broker.close_position(
                    lifecycle_row.symbol,
                    # PR-66 review F2: same fix as close-position.
                    position_uid=lifecycle_row.position_uid,
                    partial_qty=reduce_qty,
                    operator_command_uid=command.command_uid,
                )
            except Exception as exc:
                self.operator_command_store.mark_failed(
                    command_uid=command.command_uid,
                    result={"error": f"broker.close_position(partial) raised: {exc}"},
                )
                return

            # PR-66 review F1: same status gate as close-position.
            # A REJECTED/TIMEOUT/UNKNOWN/zero-fill reduce must NOT
            # advance lifecycle current_qty or PnL — otherwise the
            # row would silently report a phantom reduction.
            if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
                self.operator_command_store.mark_failed(
                    command_uid=command.command_uid,
                    result={
                        "position_uid": lifecycle_row.position_uid,
                        "symbol": lifecycle_row.symbol,
                        "broker_status": getattr(result.status, "value", str(result.status)),
                        "broker_order_id": result.order_id,
                        "raw_status": result.raw_status,
                        "message": result.message,
                        "note": (
                            "broker reduce did not produce a fill; "
                            "lifecycle and PnL accounting were NOT "
                            "advanced."
                        ),
                    },
                )
                logger.error(
                    f"reduce-position by operator: {lifecycle_row.symbol} "
                    f"broker returned status={result.status} "
                    f"(filled_qty={result.filled_qty}); "
                    f"lifecycle and accounting NOT advanced"
                )
                return

            close_price = result.avg_fill_price or 0.0
            filled_qty = float(result.filled_qty or 0.0)
            if filled_qty <= 0:
                # Defensive: status is FILLED/PARTIAL but filled_qty is
                # zero — shouldn't happen but if it does, do not write
                # phantom accounting.
                self.operator_command_store.mark_failed(
                    command_uid=command.command_uid,
                    result={
                        "note": "result.filled_qty<=0 despite FILLED/PARTIAL status",
                        "broker_status": getattr(result.status, "value", str(result.status)),
                    },
                )
                return

            # PnL + lifecycle current_qty drop. is_full_close=False so
            # _record_realized_pnl calls _reduce_lifecycle_for_owner_key
            # to update current_qty to the residual (Phase A helper).
            self._record_realized_pnl(
                symbol=lifecycle_row.symbol,
                strategy_name=lifecycle_row.strategy,
                close_price=close_price,
                qty=filled_qty,
                multiplier=100 if _OCC_PAT.match(lifecycle_row.symbol) else 1,
                external=False,
                is_full_close=False,
            )

            # PR-66 review F5: cancel-sibling-first inside
            # broker.close_position cleared any protective stop along
            # with the reduce. Mark the row 'succeeded' but flag the
            # `protection_status` so the operator knows the residual is
            # temporarily unprotected until the next cycle's
            # `_repair_missing_protective_stops` runs. Per-cycle repair
            # is bounded by ENGINE_CYCLE_INTERVAL_SECONDS (default 300s);
            # for shorter exposure the operator can fire a fresh halt or
            # full close. Recreating the stop inline here would require
            # waiting on the partial fill confirmation, looking up the
            # strategy's risk parameters, and ordering it sequentially —
            # significant extra complexity for v1. v1 contract: report
            # the degraded state, let cycle repair handle it.
            self.operator_command_store.mark_succeeded(
                command_uid=command.command_uid,
                result={
                    "position_uid": lifecycle_row.position_uid,
                    "symbol": lifecycle_row.symbol,
                    "pct": pct,
                    "requested_qty": reduce_qty,
                    "filled_qty": filled_qty,
                    "close_price": close_price,
                    "residual_qty": max(0.0, current_qty - filled_qty),
                    "broker_status": getattr(result.status, "value", str(result.status)),
                    "broker_order_id": result.order_id,
                    "degraded": True,
                    "protection_status": "pending_repair_cycle",
                    "protection_note": (
                        "broker.close_position cancelled the protective "
                        "stop before the reduce; residual is unprotected "
                        "until the engine's per-cycle stop repair pass "
                        "restores it"
                    ),
                },
            )
            try:
                self.alerts.operator_action(
                    f"operator reduce-position: {lifecycle_row.symbol} "
                    f"({lifecycle_row.position_uid[:18]}…) "
                    f"−{filled_qty} @ ${close_price:.2f} "
                    f"({pct:.0f}% of {current_qty}) "
                    f"— DEGRADED: residual unprotected until next "
                    f"cycle's stop repair — {command.reason}"
                )
            except Exception as exc:
                logger.warning(f"reduce-position alert failed: {exc}")
            logger.warning(
                f"reduce-position by operator: {lifecycle_row.symbol} "
                f"reduced={filled_qty}/{current_qty} (pct={pct}) "
                f"DEGRADED protection until repair-cycle "
                f"reason={command.reason!r}"
            )
        except Exception as exc:
            self.operator_command_store.mark_failed(
                command_uid=command.command_uid,
                result={"error": f"reduce-position handler failed: {exc}"},
            )
        finally:
            self.symbol_locks.release(
                owner_key=lifecycle_row.owner_key, holder=holder,
            )

    def _close_fractional_residual_position(
        self,
        *,
        snapshot: BrokerSnapshot,
        symbol: str,
        owner: str,
        position: Position,
    ) -> None:
        """Auto-close a managed residual equity stub that cannot carry a broker stop."""
        stop_fill = self._lookup_recent_stop_fill(symbol=symbol, owner=owner)
        if stop_fill is not None:
            self._record_recovered_stop_fill(
                symbol=symbol,
                owner=owner,
                stop_fill=stop_fill,
            )

        if self._has_pending_close_order(symbol, snapshot):
            logger.info(
                f"{symbol}: residual fractional position has a close order "
                "pending — skipping duplicate dust cleanup"
            )
            return

        logger.warning(
            f"{symbol}: auto-closing residual fractional position qty={position.qty} "
            "because it cannot carry a whole-share protective stop"
        )
        # P-6: thread position_uid through so the broker writes an
        # exit substrate row alongside the close. Lookup is best-
        # effort; failure logs DEBUG and we proceed without the
        # substrate write (matches the protective_stop pattern).
        _exit_uid: str | None = None
        if self.lifecycle_store is not None:
            try:
                _row = self.lifecycle_store.get_open_for_owner_key(
                    owner_key_for(position.symbol),
                )
                if _row is not None:
                    _exit_uid = _row.position_uid
            except Exception as exc:
                logger.debug(
                    f"residual-close position_uid lookup raised "
                    f"{type(exc).__name__}: {exc} — proceeding"
                )
        result = self.broker.close_position(
            position.symbol, position_uid=_exit_uid,
        )
        close_price = float(
            result.avg_fill_price
            or getattr(position, "current_price", 0.0)
            or getattr(position, "avg_entry_price", 0.0)
            or 0.0
        )
        # Slippage unification (Phase 1) codepath §7 — fractional
        # residual cleanups have no honest arrival benchmark (the
        # close_price fallback chain is the fill price itself or
        # position.current_price, neither of which is a slippage
        # benchmark). Tag the row 'unavailable' so the new taxonomy
        # columns honestly report no measurement; the whole-share stop
        # row already carries the meaningful exit slippage.
        self._log_close(
            result,
            close_price,
            owner,
            benchmark_kind="unavailable",
            measurement_quality="unavailable",
        )
        if result.status in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            close_qty = float(result.filled_qty or position.qty or 0.0)
            self.alerts.trade_executed(
                symbol=symbol,
                strategy=owner,
                side="sell",
                qty=close_qty,
                price=close_price,
                reason="fractional residual cleanup",
                position_uid=getattr(result, "position_uid", None)
                    or self._lookup_position_uid_for_owner(owner),
            )
            self._record_realized_pnl(symbol, owner, close_price, close_qty)
            self._pop_position(symbol)
            self._entry_prices.pop(symbol, None)

    def _attribute_orders(
        self, open_orders: list
    ) -> dict[str, str]:
        """
        Build order_id → strategy_name for pending buy orders.

        Logic:
          - SELL orders are skipped (exits don't consume sleeve budget).
          - If the order's symbol already has an open position, skip it —
            it's a close order placed by the owner strategy.
          - Otherwise, find the first slot whose watchlist includes the symbol
            and attribute the order to that slot's strategy.

        This is computed once per cycle and passed into _process_symbol so
        SleeveAllocator can count open limit orders against the correct sleeve.
        """
        result: dict[str, str] = {}
        slots = self._slots_by_priority()
        for order in open_orders:
            if order.side is Side.SELL:
                continue
            if self._has_position(order.symbol):
                # Close / reduce order for an existing position — skip.
                continue
            client_order_id = getattr(order, "client_order_id", None)
            if isinstance(client_order_id, str) and client_order_id:
                for slot in slots:
                    if client_order_id.startswith(f"{slot.strategy.name}-"):
                        result[order.order_id] = slot.strategy.name
                        break
                if order.order_id in result:
                    continue
            matches = [slot for slot in slots if order.symbol in slot.active_symbols()]
            if not matches:
                continue
            chosen = matches[0]
            result[order.order_id] = chosen.strategy.name
            if len(matches) > 1:
                logger.debug(
                    f"{order.symbol}: attributed pending order {order.order_id} "
                    f"to '{chosen.strategy.name}' via priority among "
                    f"{[slot.strategy.name for slot in matches]}"
                )
        return result

    @staticmethod
    def _has_pending_entry_order(
        symbol: str,
        strategy_name: str,
        snapshot: BrokerSnapshot,
        order_strategy: dict[str, str],
    ) -> bool:
        """True when the broker already has a pending BUY entry for this strategy/symbol."""
        return any(
            o.symbol == symbol
            and o.side is Side.BUY
            and order_strategy.get(o.order_id) == strategy_name
            for o in snapshot.open_orders
        )

    def _compute_sector_exposure(self) -> dict[str, list[dict]]:
        """
        Build {sector_key: [{"symbol": s, "strategy": owner}, ...]} of open
        equity positions per sector (11.7 Part B).

        Pure observability — never auto-blocks. OCC option symbols are excluded
        (no meaningful sector mapping for index options). Tickers the resolver
        cannot map are silently skipped (fail-open). Returns an empty dict if
        no resolver was injected. Use ``{k: len(v) for k, v in ...}`` for raw
        counts.
        """
        if self._sector_resolver is None or not self._positions:
            return {}
        grouped: dict[str, list[dict]] = {}
        for position_id, position in self._positions.items():
            # OCC option positions: position_id is the underlying ticker but
            # the leg carries the raw OCC string. Skip them — index options
            # don't have a single tradable sector mapping.
            leg = position.primary_leg
            if leg is not None and _OCC_PAT.match(leg.symbol):
                continue
            symbol = position_id
            owner = position.strategy_name
            try:
                sector = self._sector_resolver.resolve(symbol)
            except Exception as exc:
                logger.debug(f"sector resolve failed for {symbol}: {exc}")
                continue
            if sector is None:
                continue
            grouped.setdefault(sector, []).append(
                {"symbol": symbol, "strategy": owner}
            )
        return grouped

    def _slots_by_priority(self) -> list[StrategySlot]:
        """Return slots ordered by allocator priority when available."""
        if self._allocator is None:
            return list(self.slots)
        return sorted(
            self.slots,
            key=lambda slot: (
                self._allocator.strategy_priority(slot.strategy.name),
                self.slots.index(slot),
            ),
        )

    @staticmethod
    def _has_pending_close_order(symbol: str, snapshot: BrokerSnapshot) -> bool:
        """True if there's already a non-stop SELL close order for this symbol."""
        return any(
            TradingEngine._is_matching_symbol(symbol, o.symbol)
            and o.side is Side.SELL
            and o.stop_price is None
            for o in snapshot.open_orders
        )

    @staticmethod
    def _has_protective_stop_order(symbol: str, snapshot: BrokerSnapshot) -> bool:
        """True if there's already an open SELL stop order for this symbol."""
        return TradingEngine._protective_stop_order(symbol, snapshot) is not None

    @staticmethod
    def _protective_stop_order(
        symbol: str, snapshot: BrokerSnapshot
    ) -> OpenOrder | None:
        """Return the open SELL stop protecting a symbol, if present."""
        return next((
            o
            for o in snapshot.open_orders
            if TradingEngine._is_matching_symbol(symbol, o.symbol)
            and o.side is Side.SELL
            and o.stop_price is not None
        ), None)

    @staticmethod
    def _option_position_premium(position) -> float | None:
        """Best-effort option premium from a broker position snapshot."""
        current = getattr(position, "current_price", None)
        if current is not None:
            try:
                premium = float(current)
                if premium > 0:
                    return premium
            except (TypeError, ValueError):
                pass
        try:
            qty = abs(float(getattr(position, "qty", 0.0) or 0.0))
            market_value = abs(float(getattr(position, "market_value", 0.0) or 0.0))
        except (TypeError, ValueError):
            return None
        if qty <= 0 or market_value <= 0:
            return None
        return market_value / (qty * 100.0)

    @staticmethod
    def _compute_option_trailing_floor(
        *,
        entry_premium: float,
        hwm_premium: float,
        trail_activation_pct: float,
        trail_pct: float,
        stop_loss_multiple: float,
    ) -> float:
        """Return the broker stop price implied by the durable trail state."""
        hard_floor = entry_premium * stop_loss_multiple
        if hwm_premium >= entry_premium * (1.0 + trail_activation_pct):
            hard_floor = max(hard_floor, hwm_premium * (1.0 - trail_pct))
        return round(max(hard_floor, 0.01), 2)

    # Sentinel returned by _lifecycle_order_id_for when the substrate
    # lookup raised — distinct from None (genuinely-not-found) so the
    # caller can preserve an existing FK across transient store
    # errors instead of silently demoting to mirror-authoritative
    # identity (PR #71 review P1 #2).
    _LIFECYCLE_LOOKUP_FAILED: object = object()

    def _lifecycle_order_id_for(
        self,
        order_id: str | None,
        *,
        client_order_id: str | None = None,
        previous_fk: int | None = None,
    ):
        """Resolve a broker order_id to its position_lifecycle_orders.id.

        Used to populate ``option_trailing_stops.lifecycle_order_id``
        at every trailing-row upsert (PR #59 §10.4).

        Return values:
          - ``int``: substrate row id (happy path).
          - ``None``: substrate not configured, no order_id supplied,
            or the substrate row genuinely does not exist yet.
          - ``self._LIFECYCLE_LOOKUP_FAILED`` sentinel: the lookup
            raised. The caller MUST preserve any pre-existing FK
            rather than overwriting with None — a transient store
            error must not demote substrate-authoritative identity
            to mirror-only.

        When the order_id-keyed lookup returns no row AND a
        ``client_order_id`` is supplied, falls back to looking up by
        ``client_order_id``. This recovers the orphan case where
        ``_lifecycle_orders_record_stop`` successfully wrote the
        pending row but the subsequent ``attach_broker_order_id``
        failed (PR #71 review P1 #1, partial mitigation). On a hit
        the helper opportunistically re-attaches the broker order_id
        so future lookups by order_id succeed.

        ``previous_fk`` is informational only — used by callers that
        want to log a state transition.
        """
        if self.lifecycle_orders_store is None or not order_id:
            return None
        try:
            row = self.lifecycle_orders_store.get_by_order_id(order_id)
            if row is None and client_order_id:
                fallback = self.lifecycle_orders_store.get_by_client_order_id(
                    client_order_id
                )
                if fallback is not None and fallback.order_id is None:
                    # Pending substrate row that never got its order_id
                    # attached. Re-attach now using the broker id we
                    # have. Wrapped so a second failure here still
                    # surfaces the row id we already know.
                    try:
                        self.lifecycle_orders_store.attach_broker_order_id(
                            client_order_id=client_order_id,
                            order_id=order_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            f"lifecycle_orders attach retry failed for "
                            f"client_order_id={client_order_id} → "
                            f"order_id={order_id}: "
                            f"{type(exc).__name__}: {exc} — substrate "
                            f"row id={fallback.id} reused without "
                            f"attached order_id"
                        )
                row = fallback if fallback is not None else row
        except Exception as exc:
            logger.warning(
                f"lifecycle_order_id lookup raised "
                f"{type(exc).__name__}: {exc} for order_id={order_id} "
                f"(client_order_id={client_order_id}); preserving "
                f"previous FK={previous_fk}"
            )
            return self._LIFECYCLE_LOOKUP_FAILED
        return None if row is None else row.id

    def _resolve_trailing_fk_or_preserve(
        self,
        *,
        broker_order_id: str | None,
        client_order_id: str | None,
        previous_fk: int | None,
        previous_mirror_order_id: str | None,
        owner: str,
        occ: str,
        load_bearing: bool,
    ) -> int | None:
        """Resolve the FK target for a trailing-row upsert.

        Encapsulates the §10.4 preservation contract (PR #71 review
        P1 #2). Behavior:

          1. If ``broker_order_id`` is unchanged from
             ``previous_mirror_order_id`` AND ``previous_fk`` is
             populated, the FK already points at the right substrate
             row — no need to re-query; return ``previous_fk``.
          2. Otherwise call ``_lifecycle_order_id_for`` and act on
             its return:
             - ``int`` → use it.
             - ``None`` (genuine not-found) → return ``None``; if
               ``load_bearing`` is True (post-broker-accept on a
               fresh submit / replacement), emit CRITICAL so the
               orphan condition is visible to the operator.
             - lookup-failed sentinel → preserve ``previous_fk``
               (which may itself be None on legacy rows; in that
               case the caller stays mirror-authoritative for this
               cycle and the next cycle retries).
        """
        if (
            broker_order_id
            and previous_fk is not None
            and broker_order_id == previous_mirror_order_id
        ):
            return previous_fk
        resolved = self._lifecycle_order_id_for(
            broker_order_id,
            client_order_id=client_order_id,
            previous_fk=previous_fk,
        )
        if resolved is self._LIFECYCLE_LOOKUP_FAILED:
            return previous_fk
        if resolved is None and load_bearing and broker_order_id:
            logger.critical(
                f"[{owner}] {occ}: option trailing FK could not be "
                f"resolved for broker order_id={broker_order_id} "
                f"(client_order_id={client_order_id}). The substrate "
                f"row for this stop is orphaned — likely "
                f"insert_pending or attach_broker_order_id failure "
                f"during stop create/replace. Trailing row will fall "
                f"back to mirror-authoritative identity for this "
                f"position. Investigate substrate health."
            )
        return resolved

    def _recent_option_stop_submit_pending(self, row) -> bool:
        """True when a just-submitted option stop may not show in snapshots yet.

        Reads broker identity / status through the lifecycle_order_id FK
        when populated (substrate-authoritative per PR #59 §10.4); falls
        back to the denormalized mirror columns otherwise. Both paths
        preserve the original predicate: a known order_id with a still-
        accepting status that was last updated within the retry grace.
        """
        if row is None:
            return False
        order_id, status = self._option_trailing_authoritative_identity(row)
        if not order_id:
            return False
        if status not in {"accepted", "new", "pending_new", "open"}:
            return False
        try:
            updated_at = datetime.fromisoformat(row.last_updated_at)
        except (TypeError, ValueError):
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)
        retry_grace = max(120.0, float(self.config.cycle_interval_seconds) * 2.0)
        return 0.0 <= age.total_seconds() <= retry_grace

    # Roles a trailing row's FK is allowed to point at — the broker
    # stop is either the original protective_stop or its post-ratchet
    # replacement_stop. Anything else (entry_primary, exit, etc.) is a
    # cross-wiring bug or recovery race; we MUST NOT consult it for
    # duplicate-submit suppression (PR #71 review P2).
    _ALLOWED_TRAILING_FK_ROLES: frozenset[str] = frozenset(
        {"protective_stop", "replacement_stop"}
    )

    def _option_trailing_authoritative_identity(
        self, row
    ) -> tuple[str | None, str | None]:
        """Return (order_id, status) preferring substrate over the mirror.

        Substrate identity / status (joined via lifecycle_order_id) is
        the canonical post-§10.4 source. The denormalized mirror columns
        on ``option_trailing_stops`` remain as a fallback for legacy
        rows whose FK is still NULL (e.g. trailing state for a broker
        stop that predates the foundation, before cycle reconciliation
        has had a chance to backfill the substrate row).

        Defensive validation (PR #71 review P2): the FK only proves the
        substrate row exists. On a fresh DB the SQLite FK enforces the
        relationship; on legacy DBs the FK constraint is absent and a
        bad write or recovery race could leave the FK pointing at an
        unrelated row. Before trusting the substrate fields we verify
        that ``position_uid`` matches and ``role`` is a stop role; on
        mismatch we log CRITICAL and fall back to the mirror columns
        so a wrong substrate row never influences duplicate-submit
        suppression.
        """
        mirror_order_id = getattr(row, "alpaca_stop_order_id", None)
        mirror_status = getattr(row, "stop_order_status", None)
        lifecycle_order_id = getattr(row, "lifecycle_order_id", None)
        if self.lifecycle_orders_store is None or not lifecycle_order_id:
            return mirror_order_id, mirror_status
        try:
            substrate = self.lifecycle_orders_store.get_by_id(
                int(lifecycle_order_id)
            )
        except Exception as exc:
            logger.debug(
                f"option trailing substrate lookup raised "
                f"{type(exc).__name__}: {exc} for "
                f"lifecycle_order_id={lifecycle_order_id}"
            )
            return mirror_order_id, mirror_status
        if substrate is None:
            return mirror_order_id, mirror_status
        # Defensive validation — see docstring.
        row_position_uid = getattr(row, "position_uid", None)
        if (
            row_position_uid is not None
            and substrate.position_uid != row_position_uid
        ):
            logger.critical(
                f"option trailing FK position_uid mismatch: trailing row "
                f"{getattr(row, 'occ_symbol', '?')} "
                f"position_uid={row_position_uid} but "
                f"position_lifecycle_orders.id={lifecycle_order_id} has "
                f"position_uid={substrate.position_uid} (role="
                f"{substrate.role}). Falling back to mirror columns for "
                f"this read; investigate cross-wiring or recovery race."
            )
            return mirror_order_id, mirror_status
        if substrate.role not in self._ALLOWED_TRAILING_FK_ROLES:
            logger.critical(
                f"option trailing FK role mismatch: trailing row "
                f"{getattr(row, 'occ_symbol', '?')} → "
                f"position_lifecycle_orders.id={lifecycle_order_id} has "
                f"role={substrate.role!r} (expected one of "
                f"{sorted(self._ALLOWED_TRAILING_FK_ROLES)}). Falling "
                f"back to mirror columns for this read; investigate "
                f"cross-wiring or recovery race."
            )
            return mirror_order_id, mirror_status
        return (
            substrate.order_id or mirror_order_id,
            substrate.status or mirror_status,
        )

    def _option_ratchet_quote_rejection(
        self,
        *,
        quote: OptionQuote | None,
        desired_stop: float,
    ) -> str | None:
        """Return why a quote cannot safely support a higher option stop."""
        if quote is None:
            return "latest two-sided option quote unavailable"
        bid = float(quote.bid_price)
        ask = float(quote.ask_price)
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask <= 0:
            return f"invalid option quote bid=${bid:.2f} ask=${ask:.2f}"
        if ask < bid:
            return f"crossed option quote bid=${bid:.2f} ask=${ask:.2f}"

        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age_seconds = (
            now.astimezone(timezone.utc) - quote.timestamp.astimezone(timezone.utc)
        ).total_seconds()
        if (
            age_seconds < -5.0
            or age_seconds > self.config.option_trailing_quote_max_age_seconds
        ):
            return (
                f"stale option quote age={age_seconds:.1f}s "
                f"(max={self.config.option_trailing_quote_max_age_seconds:.1f}s)"
            )

        midpoint = (bid + ask) / 2.0
        spread_pct = (ask - bid) / midpoint if midpoint > 0 else math.inf
        if spread_pct > self.config.option_trailing_max_spread_pct:
            return (
                f"wide option quote bid=${bid:.2f} ask=${ask:.2f} "
                f"spread={spread_pct:.1%} "
                f"(max={self.config.option_trailing_max_spread_pct:.1%})"
            )

        max_safe_stop = bid * (1.0 - self.config.option_trailing_stop_bid_buffer_pct)
        if desired_stop > max_safe_stop + 1e-9:
            return (
                f"bid=${bid:.2f} does not support stop=${desired_stop:.2f} "
                f"with {self.config.option_trailing_stop_bid_buffer_pct:.1%} buffer"
            )
        return None

    def _option_stop_audit_active_for(self, owner: str) -> bool:
        """Return whether temporary replace diagnostics apply to this owner."""
        return (
            self.option_stop_audit_store is not None
            and owner == self.config.option_stop_replace_audit_strategy
        )

    def _record_option_stop_audit(
        self,
        *,
        correlation_id: str,
        record_type: str,
        strategy: str,
        occ_symbol: str,
        order_id: str | None,
        payload: dict[str, object],
        recorded_at: datetime | None = None,
    ) -> bool:
        """Append diagnostic evidence without affecting order behavior."""
        if self.option_stop_audit_store is None:
            return False
        try:
            self.option_stop_audit_store.append(
                correlation_id=correlation_id,
                recorded_at=recorded_at or datetime.now(timezone.utc),
                record_type=record_type,
                strategy=strategy,
                occ_symbol=occ_symbol,
                order_id=order_id,
                payload=payload,
            )
            return True
        except Exception as exc:
            logger.warning(f"option-stop audit write failed: {exc}")
            return False

    def _begin_option_stop_audit(
        self, *, owner: str, order_id: str
    ) -> _OptionStopAuditContext | None:
        """Open a real-time forensic window for one target-strategy decision."""
        if not self._option_stop_audit_active_for(owner):
            return None
        decision_at = datetime.now(timezone.utc)
        return _OptionStopAuditContext(
            correlation_id=f"optstop_{uuid.uuid4().hex}",
            decision_at=decision_at,
            expires_at=decision_at + timedelta(
                seconds=self.config.option_stop_replace_audit_window_seconds
            ),
            broker_before=self.broker.get_order_audit_snapshot(order_id),
        )

    def _record_option_stop_audit_decision(
        self,
        *,
        audit: _OptionStopAuditContext,
        record_type: str,
        owner: str,
        occ: str,
        lifecycle_row,
        existing,
        position,
        quote: OptionQuote | None,
        reason: str,
        entry_premium: float,
        hwm: float,
        desired_stop: float,
        requested_stop: float | None,
        qty: float,
        replacement_client_order_id: str | None = None,
    ) -> bool:
        """Record the shared decision evidence for deferrals and replacements."""
        return self._record_option_stop_audit(
            correlation_id=audit.correlation_id,
            record_type=record_type,
            strategy=owner,
            occ_symbol=occ,
            order_id=existing.order_id,
            recorded_at=audit.decision_at,
            payload={
                "position_uid": lifecycle_row.position_uid,
                "reason": reason,
                "association_expires_at": audit.expires_at,
                "cached_order": {
                    "stop_price": existing.stop_price,
                    "qty": existing.qty,
                    "time_in_force": existing.time_in_force,
                    "status": existing.status,
                },
                "broker_before": (
                    asdict(audit.broker_before)
                    if audit.broker_before is not None
                    else None
                ),
                "quote": asdict(quote) if quote is not None else None,
                "position_current_price": getattr(
                    position, "current_price", None
                ),
                "position_market_value": getattr(
                    position, "market_value", None
                ),
                "entry_premium": entry_premium,
                "hwm_premium": hwm,
                "desired_stop_price": desired_stop,
                "requested_stop_price": requested_stop,
                "requested_qty": qty,
                "replacement_client_order_id": replacement_client_order_id,
            },
        )

    def _drain_option_stop_audit_events(self) -> None:
        """Persist bounded stream evidence on the engine's DB thread."""
        if self.option_stop_audit_store is None:
            return
        try:
            now = datetime.now(timezone.utc)
            last_pruned = self._option_stop_audit_last_pruned_at
            if (
                last_pruned is None
                or now - last_pruned >= timedelta(hours=24)
            ):
                cutoff = now - timedelta(
                    days=self.config.option_stop_replace_audit_retention_days
                )
                self.option_stop_audit_store.prune_before(cutoff)
                self._option_stop_audit_last_pruned_at = now
            if self._stream_manager is None:
                return
            for record in self._stream_manager.drain_option_stop_audit_events():
                self.option_stop_audit_store.append(**record)
        except Exception as exc:
            logger.warning(f"option-stop audit event drain failed: {exc}")

    def _sync_option_trailing_stops(self, snapshot: BrokerSnapshot) -> None:
        """Create or atomically ratchet durable GTC single-leg option stops."""
        if self.option_trailing_store is None:
            return
        open_stop_by_occ = {
            o.symbol: o
            for o in snapshot.open_orders
            if is_occ_option(o.symbol) and o.side is Side.SELL and o.stop_price is not None
        }
        for occ, position in snapshot.account.open_positions.items():
            if not is_occ_option(occ):
                continue
            owner_key = owner_key_for(occ)
            owner = self._get_owner(owner_key)
            if owner is None:
                continue
            lifecycle_row = (
                self.lifecycle_store.get_open_for_owner_key(owner_key)
                if self.lifecycle_store is not None
                else None
            )
            if lifecycle_row is None:
                logger.warning(
                    f"[{owner}] {occ}: cannot sync option trailing stop — "
                    "missing position_uid lifecycle row"
                )
                continue
            strategy = self._strategy_by_name(owner)
            if strategy is None or not hasattr(strategy, "trail_pct"):
                continue
            entry_premium = float(
                self._entry_prices.get(owner_key)
                or getattr(position, "avg_entry_price", 0.0)
                or 0.0
            )
            current_premium = self._option_position_premium(position)
            if entry_premium <= 0 and current_premium is not None:
                entry_premium = current_premium
            if entry_premium <= 0:
                logger.warning(
                    f"[{owner}] {occ}: cannot sync option trailing stop — "
                    "missing entry premium"
                )
                continue

            row = self.option_trailing_store.get_by_occ(occ)
            hwm = max(
                entry_premium,
                row.hwm_premium if row is not None else entry_premium,
                current_premium if current_premium is not None else entry_premium,
            )
            if row is None:
                logger.warning(
                    f"[{owner}] {occ}: no durable option HWM was recoverable; "
                    f"initializing conservatively at ${hwm:.2f}"
                )
                self.alerts.option_trailing_state_unverified(
                    occ, owner, hwm
                )
            restore = getattr(strategy, "restore_trailing_state", None)
            if restore is not None:
                try:
                    restore(occ, entry_premium=entry_premium, hwm_premium=hwm)
                except Exception as exc:
                    logger.warning(
                        f"[{owner}] {occ}: strategy trailing-state restore failed: {exc}"
                    )
            trail_activation_pct = float(getattr(strategy, "trail_activation_pct", 0.10))
            trail_pct = float(getattr(strategy, "trail_pct", 0.15))
            config = getattr(strategy, "config", None)
            stop_loss_multiple = float(getattr(config, "stop_loss_multiple", 0.75))
            desired_stop = self._compute_option_trailing_floor(
                entry_premium=entry_premium,
                hwm_premium=hwm,
                trail_activation_pct=trail_activation_pct,
                trail_pct=trail_pct,
                stop_loss_multiple=stop_loss_multiple,
            )
            qty = abs(float(getattr(position, "qty", 0.0) or 0.0))
            existing = open_stop_by_occ.get(occ)
            if existing is None and self._recent_option_stop_submit_pending(row):
                # Submit-pending retry: preserve the substrate FK already
                # written by the prior cycle's submit_option_gtc_stop.
                self.option_trailing_store.upsert(
                    position_uid=lifecycle_row.position_uid,
                    occ_symbol=occ,
                    strategy=owner,
                    owner_key=owner_key,
                    qty=qty,
                    entry_premium=entry_premium,
                    hwm_premium=hwm,
                    trail_activation_pct=trail_activation_pct,
                    trail_pct=trail_pct,
                    current_stop_price=desired_stop,
                    alpaca_stop_order_id=row.alpaca_stop_order_id,
                    stop_order_status=row.stop_order_status,
                    last_observed_premium=current_premium,
                    lifecycle_order_id=row.lifecycle_order_id,
                )
                logger.debug(
                    f"[{owner}] {occ}: recently submitted option stop "
                    f"{row.alpaca_stop_order_id} not present in snapshot yet; "
                    "skipping duplicate submit"
                )
                continue

            existing_tif = (
                str(existing.time_in_force).lower()
                if existing is not None and existing.time_in_force is not None
                else None
            )
            existing_is_known_gtc = existing_tif == "gtc" or (
                existing is not None
                and existing_tif is None
                and row is not None
                and row.alpaca_stop_order_id == existing.order_id
                and row.stop_order_status
                in {"accepted", "new", "pending_new", "open", "replace_failed"}
            )
            existing_qty_matches = (
                existing is not None and abs(float(existing.qty) - qty) <= 1e-9
            )
            # Keep an adequate durable stop. Legacy DAY stops are replaced with GTC.
            if (
                existing is not None
                and (existing.stop_price or 0.0) >= desired_stop
                and existing_is_known_gtc
                and existing_qty_matches
            ):
                # Adequate-stop preserve: the broker stop is unchanged,
                # so if the prior trailing row already had a FK pointing
                # at it, keep that FK (skip the substrate query). A
                # lookup-failed sentinel on a re-resolve must also
                # preserve the existing FK rather than demote.
                self.option_trailing_store.upsert(
                    position_uid=lifecycle_row.position_uid,
                    occ_symbol=occ,
                    strategy=owner,
                    owner_key=owner_key,
                    qty=qty,
                    entry_premium=entry_premium,
                    hwm_premium=hwm,
                    trail_activation_pct=trail_activation_pct,
                    trail_pct=trail_pct,
                    current_stop_price=float(existing.stop_price or desired_stop),
                    alpaca_stop_order_id=existing.order_id,
                    stop_order_status=existing.status,
                    last_observed_premium=current_premium,
                    lifecycle_order_id=self._resolve_trailing_fk_or_preserve(
                        broker_order_id=existing.order_id,
                        client_order_id=existing.client_order_id,
                        previous_fk=(
                            row.lifecycle_order_id if row is not None else None
                        ),
                        previous_mirror_order_id=(
                            row.alpaca_stop_order_id if row is not None else None
                        ),
                        owner=owner,
                        occ=occ,
                        load_bearing=False,
                    ),
                )
                continue

            # Alpaca's replace endpoint keeps protection at the broker boundary:
            # a rejected replacement leaves the existing stop in place.
            if existing is not None:
                existing_stop = float(existing.stop_price or desired_stop)
                replacement_stop = max(desired_stop, existing_stop)
                price_ratchet = desired_stop > existing_stop + 0.005
                maintenance_replacement = (
                    not existing_is_known_gtc or not existing_qty_matches
                )
                audit = self._begin_option_stop_audit(
                    owner=owner,
                    order_id=existing.order_id,
                )
                quote: OptionQuote | None = None
                quote_rejection: str | None = None
                if price_ratchet:
                    quote = self.broker.get_latest_option_quote(occ)
                    quote_rejection = self._option_ratchet_quote_rejection(
                        quote=quote,
                        desired_stop=desired_stop,
                    )
                    if quote_rejection is not None:
                        if maintenance_replacement:
                            replacement_stop = existing_stop
                            logger.info(
                                f"[{owner}] {occ}: option stop price ratchet "
                                f"deferred ({quote_rejection}); applying required "
                                f"TIF/qty maintenance at existing ${existing_stop:.2f}"
                            )
                        else:
                            # Deferred-ratchet preserve: existing stop
                            # stays live unchanged; preserve prior FK
                            # when the broker order id is unchanged.
                            self.option_trailing_store.upsert(
                                position_uid=lifecycle_row.position_uid,
                                occ_symbol=occ,
                                strategy=owner,
                                owner_key=owner_key,
                                qty=qty,
                                entry_premium=entry_premium,
                                hwm_premium=hwm,
                                trail_activation_pct=trail_activation_pct,
                                trail_pct=trail_pct,
                                current_stop_price=existing_stop,
                                alpaca_stop_order_id=existing.order_id,
                                stop_order_status=existing.status,
                                last_observed_premium=current_premium,
                                lifecycle_order_id=(
                                    self._resolve_trailing_fk_or_preserve(
                                        broker_order_id=existing.order_id,
                                        client_order_id=(
                                            existing.client_order_id
                                        ),
                                        previous_fk=(
                                            row.lifecycle_order_id
                                            if row is not None else None
                                        ),
                                        previous_mirror_order_id=(
                                            row.alpaca_stop_order_id
                                            if row is not None else None
                                        ),
                                        owner=owner,
                                        occ=occ,
                                        load_bearing=False,
                                    )
                                ),
                            )
                            logger.info(
                                f"[{owner}] {occ}: option stop ratchet "
                                f"${existing_stop:.2f} -> ${desired_stop:.2f} "
                                f"deferred — {quote_rejection}; existing broker "
                                "stop remains active"
                            )
                            if audit is not None:
                                self._record_option_stop_audit_decision(
                                    audit=audit,
                                    record_type="decision_deferred",
                                    owner=owner,
                                    occ=occ,
                                    lifecycle_row=lifecycle_row,
                                    existing=existing,
                                    position=position,
                                    quote=quote,
                                    reason=quote_rejection,
                                    entry_premium=entry_premium,
                                    hwm=hwm,
                                    desired_stop=desired_stop,
                                    requested_stop=None,
                                    qty=qty,
                                )
                            continue
                replacement_client_order_id = (
                    f"opt-trail-audit-{uuid.uuid4().hex[:10]}"
                    if audit is not None else None
                )
                audit_created = False
                if audit is not None:
                    reason = (
                        f"{quote_rejection}; proceeding with TIF/qty maintenance"
                        if quote_rejection is not None
                        else (
                            "price ratchet"
                            if price_ratchet
                            else "TIF/qty maintenance"
                        )
                    )
                    audit_created = self._record_option_stop_audit_decision(
                        audit=audit,
                        record_type="decision_replace",
                        owner=owner,
                        occ=occ,
                        lifecycle_row=lifecycle_row,
                        existing=existing,
                        position=position,
                        quote=quote,
                        reason=reason,
                        entry_premium=entry_premium,
                        hwm=hwm,
                        desired_stop=desired_stop,
                        requested_stop=replacement_stop,
                        qty=qty,
                        replacement_client_order_id=(
                            replacement_client_order_id
                        ),
                    )
                    if audit_created and self._stream_manager is not None:
                        self._stream_manager.register_option_stop_audit(
                            correlation_id=audit.correlation_id,
                            strategy=owner,
                            occ_symbol=occ,
                            aliases={
                                existing.order_id,
                                replacement_client_order_id,
                            },
                            expires_at=audit.expires_at,
                        )
                replace_call_start = (
                    datetime.now(timezone.utc) if audit_created else None
                )
                replace_call_started_mono = (
                    time.monotonic() if audit_created else None
                )
                try:
                    if replacement_client_order_id is not None:
                        new_order = self.broker.replace_option_stop(
                            order_id=existing.order_id,
                            qty=qty,
                            stop_price=replacement_stop,
                            client_order_id=replacement_client_order_id,
                            position_uid=lifecycle_row.position_uid,
                        )
                    else:
                        new_order = self.broker.replace_option_stop(
                            order_id=existing.order_id,
                            qty=qty,
                            stop_price=replacement_stop,
                            position_uid=lifecycle_row.position_uid,
                        )
                except Exception as exc:
                    replace_call_end = (
                        datetime.now(timezone.utc) if audit_created else None
                    )
                    if audit_created and audit is not None:
                        assert replace_call_start is not None
                        assert replace_call_started_mono is not None
                        assert replace_call_end is not None
                        self._record_option_stop_audit(
                            correlation_id=audit.correlation_id,
                            record_type="replace_failed",
                            strategy=owner,
                            occ_symbol=occ,
                            order_id=existing.order_id,
                            recorded_at=replace_call_end,
                            payload={
                                "replace_call_start": replace_call_start,
                                "replace_call_end": replace_call_end,
                                "replace_call_latency_ms": (
                                    time.monotonic() - replace_call_started_mono
                                ) * 1000.0,
                                "error": str(exc),
                            },
                        )
                    logger.error(
                        f"[{owner}] {occ}: failed to replace option trailing stop "
                        f"{existing.order_id} with GTC @ ${desired_stop:.2f}: {exc}"
                    )
                    self.risk.record_broker_error()
                    self.alerts.broker_error(
                        f"{occ} option trailing stop replacement: {exc}"
                    )
                    # Replacement failed at the broker — the old stop
                    # is still live, so preserve the FK at the original
                    # substrate row (no new replacement_stop row was
                    # written because the broker call raised).
                    self.option_trailing_store.upsert(
                        position_uid=lifecycle_row.position_uid,
                        occ_symbol=occ,
                        strategy=owner,
                        owner_key=owner_key,
                        qty=qty,
                        entry_premium=entry_premium,
                        hwm_premium=hwm,
                        trail_activation_pct=trail_activation_pct,
                        trail_pct=trail_pct,
                        current_stop_price=float(existing.stop_price or desired_stop),
                        alpaca_stop_order_id=existing.order_id,
                        stop_order_status="replace_failed",
                        last_observed_premium=current_premium,
                        lifecycle_order_id=(
                            self._resolve_trailing_fk_or_preserve(
                                broker_order_id=existing.order_id,
                                client_order_id=existing.client_order_id,
                                previous_fk=(
                                    row.lifecycle_order_id
                                    if row is not None else None
                                ),
                                previous_mirror_order_id=(
                                    row.alpaca_stop_order_id
                                    if row is not None else None
                                ),
                                owner=owner,
                                occ=occ,
                                load_bearing=False,
                            )
                        ),
                    )
                    continue
                replace_call_end = (
                    datetime.now(timezone.utc) if audit_created else None
                )
                if (
                    audit_created
                    and audit is not None
                    and self._stream_manager is not None
                ):
                    self._stream_manager.bind_option_stop_audit_alias(
                        audit.correlation_id,
                        new_order.order_id,
                    )
                broker_after = (
                    self.broker.get_order_audit_snapshot(new_order.order_id)
                    if audit_created
                    else None
                )
                if audit_created and audit is not None:
                    assert replace_call_start is not None
                    assert replace_call_started_mono is not None
                    assert replace_call_end is not None
                    self._record_option_stop_audit(
                        correlation_id=audit.correlation_id,
                        record_type="replace_result",
                        strategy=owner,
                        occ_symbol=occ,
                        order_id=new_order.order_id,
                        recorded_at=replace_call_end,
                        payload={
                            "replaces_order_id": existing.order_id,
                            "replace_call_start": replace_call_start,
                            "replace_call_end": replace_call_end,
                            "replace_call_latency_ms": (
                                time.monotonic() - replace_call_started_mono
                            ) * 1000.0,
                            "broker_after": (
                                asdict(broker_after)
                                if broker_after is not None else None
                            ),
                        },
                    )
                # Replacement succeeded: FK advances to the brand-new
                # replacement_stop substrate row just written by
                # broker.replace_option_stop. The old protective_stop
                # row terminates asynchronously when apply_order_event
                # observes the canceled event for the superseded order.
                # load_bearing=True so a missed lookup after broker
                # accept raises CRITICAL (orphan condition).
                self.option_trailing_store.upsert(
                    position_uid=lifecycle_row.position_uid,
                    occ_symbol=occ,
                    strategy=owner,
                    owner_key=owner_key,
                    qty=qty,
                    entry_premium=entry_premium,
                    hwm_premium=hwm,
                    trail_activation_pct=trail_activation_pct,
                    trail_pct=trail_pct,
                    current_stop_price=float(new_order.stop_price or desired_stop),
                    alpaca_stop_order_id=new_order.order_id,
                    stop_order_status=new_order.status,
                    last_observed_premium=current_premium,
                    lifecycle_order_id=self._resolve_trailing_fk_or_preserve(
                        broker_order_id=new_order.order_id,
                        client_order_id=new_order.client_order_id,
                        previous_fk=(
                            row.lifecycle_order_id if row is not None else None
                        ),
                        previous_mirror_order_id=None,
                        owner=owner,
                        occ=occ,
                        load_bearing=True,
                    ),
                )
                logger.info(
                    f"[{owner}] {occ}: option GTC trailing stop replaced — "
                    f"hwm=${hwm:.2f} stop=${float(new_order.stop_price or desired_stop):.2f}"
                )
                continue

            try:
                new_order = self.broker.submit_option_gtc_stop(
                    symbol=occ,
                    qty=qty,
                    stop_price=desired_stop,
                    position_uid=lifecycle_row.position_uid,
                )
            except Exception as exc:
                logger.error(
                    f"[{owner}] {occ}: failed to submit option GTC trailing stop "
                    f"@ ${desired_stop:.2f}: {exc}"
                )
                self.risk.record_broker_error()
                self.alerts.broker_error(f"{occ} option trailing stop: {exc}")
                # Fresh submit failed at the broker — no substrate row
                # was written. FK is None until the next cycle re-attempts.
                self.option_trailing_store.upsert(
                    position_uid=lifecycle_row.position_uid,
                    occ_symbol=occ,
                    strategy=owner,
                    owner_key=owner_key,
                    qty=qty,
                    entry_premium=entry_premium,
                    hwm_premium=hwm,
                    trail_activation_pct=trail_activation_pct,
                    trail_pct=trail_pct,
                    current_stop_price=desired_stop,
                    alpaca_stop_order_id=None,
                    stop_order_status="submit_failed",
                    last_observed_premium=current_premium,
                    lifecycle_order_id=None,
                )
                continue
            # Fresh submit succeeded: FK points at the brand-new
            # protective_stop substrate row just written by
            # broker.submit_option_gtc_stop. load_bearing=True so a
            # missed lookup after broker accept raises CRITICAL
            # (orphan condition).
            self.option_trailing_store.upsert(
                position_uid=lifecycle_row.position_uid,
                occ_symbol=occ,
                strategy=owner,
                owner_key=owner_key,
                qty=qty,
                entry_premium=entry_premium,
                hwm_premium=hwm,
                trail_activation_pct=trail_activation_pct,
                trail_pct=trail_pct,
                current_stop_price=desired_stop,
                alpaca_stop_order_id=new_order.order_id,
                stop_order_status=new_order.status,
                last_observed_premium=current_premium,
                lifecycle_order_id=self._resolve_trailing_fk_or_preserve(
                    broker_order_id=new_order.order_id,
                    client_order_id=new_order.client_order_id,
                    previous_fk=(
                        row.lifecycle_order_id if row is not None else None
                    ),
                    previous_mirror_order_id=None,
                    owner=owner,
                    occ=occ,
                    load_bearing=True,
                ),
            )
            logger.info(
                f"[{owner}] {occ}: option GTC trailing stop synced — "
                f"hwm=${hwm:.2f} stop=${desired_stop:.2f}"
            )

    @staticmethod
    def _get_position_for(symbol: str, snapshot: BrokerSnapshot):
        """Get the position for the symbol or its corresponding option contract."""
        position = snapshot.account.open_positions.get(symbol)
        if position is not None:
            return position
        import re
        pat = re.compile(rf"^{re.escape(symbol)}[0-9]{{6}}[CP][0-9]{{8}}$")
        for pos_symbol, pos in snapshot.account.open_positions.items():
            if pat.match(pos_symbol):
                return pos
        return None

    @staticmethod
    def _is_matching_symbol(target: str, actual: str) -> bool:
        """Return True if actual matches target exactly or is an OCC option of target."""
        if actual == target:
            return True
        import re
        return bool(re.match(rf"^{re.escape(target)}[0-9]{{6}}[CP][0-9]{{8}}$", actual))

    def _sync_managed_stop_legs(self, snapshot: BrokerSnapshot) -> None:
        """Rehydrate tracked protective stop ids from broker open orders."""
        if self._stream_manager is None:
            return
        stop_ids: set[str] = set()
        for order in snapshot.open_orders:
            if order.side is not Side.SELL or order.stop_price is None:
                continue
            if _OCC_PAT.match(order.symbol):
                continue
            if self._get_owner(order.symbol) is None:
                continue
            stop_ids.add(order.order_id)
        self._stream_manager.sync_stop_legs(stop_ids)

    def _lookup_recent_stop_fill(
        self,
        *,
        symbol: str,
        owner: str,
        until: datetime | None = None,
    ):
        """Return a recoverable recent filled protective stop for ``symbol`` if present."""
        context = self.trade_logger.read_latest_open_entry_context(
            symbol=symbol,
            strategy=owner,
        )
        after = None
        if context is not None and context.get("entry_timestamp"):
            try:
                after = datetime.fromisoformat(str(context["entry_timestamp"]))
            except Exception:
                after = None
        if after is None:
            after = datetime.now(timezone.utc) - timedelta(days=30)

        stop_fill = self.broker.find_recent_filled_stop_order(
            symbol=symbol,
            after=after,
            until=until,
        )
        if stop_fill is None:
            return None
        order_id = getattr(stop_fill, "order_id", None)
        if not isinstance(order_id, str) or not order_id:
            return None
        if self.trade_logger.has_recorded_order_id(order_id):
            return None
        return stop_fill

    def _lookup_recent_exit_fills(
        self,
        *,
        symbol: str,
        owner: str,
        until: datetime | None = None,
    ) -> list:
        """
        Return unrecorded filled SELL orders that fully explain a vanished position.

        A valid entry timestamp is required so an older lifecycle's sale cannot
        be attached to the current trade. Partial exits are returned
        chronologically only when their cumulative quantity accounts for the
        remaining trade-log quantity.
        """
        context = self.trade_logger.read_latest_open_entry_context(
            symbol=symbol,
            strategy=owner,
        )
        if context is None or not context.get("entry_timestamp"):
            return []
        try:
            after = datetime.fromisoformat(str(context["entry_timestamp"]))
        except (TypeError, ValueError):
            return []
        open_qty = float(context.get("open_qty") or 0.0)
        if open_qty <= 0:
            return []

        fills = self.broker.find_recent_filled_sell_orders(
            symbol=symbol,
            after=after,
            until=until,
        )
        unrecorded = [
            fill
            for fill in fills
            if not self.trade_logger.has_recorded_order_id(fill.order_id)
        ]
        recovered_qty = sum(float(fill.filled_qty or 0.0) for fill in unrecorded)
        if abs(recovered_qty - open_qty) > 1e-9:
            logger.warning(
                f"{symbol}: broker history found {recovered_qty} versus "
                f"{open_qty} open unrecorded SELL quantity; refusing mismatched "
                "vanished-position reconstruction"
            )
            return []
        return unrecorded

    def _reconcile_vanished_db_positions(self, snapshot: BrokerSnapshot) -> None:
        """Recover broker-proven exits for DB-open positions absent at startup."""
        broker_symbols = set(snapshot.account.open_positions)
        broker_owner_keys = {owner_key_for(symbol) for symbol in broker_symbols}
        for symbol, owner in self.trade_logger.read_all_open_owners().items():
            if symbol in broker_symbols or owner_key_for(symbol) in broker_owner_keys:
                continue
            try:
                stop_fill = self._lookup_recent_stop_fill(
                    symbol=symbol,
                    owner=owner,
                    until=snapshot.fetched_at,
                )
                if stop_fill is not None:
                    self._record_recovered_stop_fill(
                        symbol=symbol,
                        owner=owner,
                        stop_fill=stop_fill,
                    )
                    continue
                exit_fills = self._lookup_recent_exit_fills(
                    symbol=symbol,
                    owner=owner,
                    until=snapshot.fetched_at,
                )
                if not exit_fills:
                    logger.warning(
                        f"restart: {symbol} is open in the trade DB but absent "
                        "from Alpaca, with no complete broker fill history to "
                        "reconstruct the close"
                    )
                    continue
                for index, exit_fill in enumerate(exit_fills):
                    self._record_recovered_exit_fill(
                        symbol=symbol,
                        owner=owner,
                        exit_fill=exit_fill,
                        alert_reason="startup_broker_history_sell_recovered",
                        is_full_close=index == len(exit_fills) - 1,
                        external=True,
                    )
                logger.warning(
                    f"restart: reconciled vanished {symbol} from "
                    f"{len(exit_fills)} filled SELL order(s) in Alpaca history"
                )
            except Exception as exc:
                logger.warning(
                    f"restart: broker-history reconciliation failed for "
                    f"{symbol}: {exc}"
                )

    def _record_recovered_stop_fill(
        self,
        *,
        symbol: str,
        owner: str,
        stop_fill,
    ) -> bool:
        """Persist a broker-recovered stop fill and feed realized P&L once.

        Recovery rows should preserve the broker's original execution time
        when available (``filled_at`` / ``submitted_at``) so the audit trail
        reflects when the stop actually happened, not when the engine later
        discovered the missed fill.
        """
        price = stop_fill.avg_fill_price
        qty = float(stop_fill.filled_qty or 0.0)
        raw_symbol = getattr(stop_fill, "symbol", None) or symbol
        if price is None or qty <= 0:
            self.trade_logger.log_external_close(
                symbol=raw_symbol,
                strategy=owner,
                reason="stop_triggered",
            )
            # Operator Controls Phase A — _record_realized_pnl is not
            # called on this fallback (missing price/qty), so close
            # the lifecycle row directly. Matches the log_external_close
            # semantic above. Mirrors the WebSocket stop-fill fallback
            # fix from the F7 patch.
            self._close_lifecycle_for_owner_key(
                owner_key=owner_key_for(raw_symbol),
                external=True,
            )
            self._cleanup_option_trailing_state(
                raw_symbol,
                reason="broker-history stop fallback",
            )
            return False

        # Slippage unification (Phase 1) — pass the recovered broker
        # order's actual stop_price so the slippage benchmark matches
        # the active stop that fired, not the original initial_stop_loss.
        # quality='recovered' tags the row for downstream consumers per
        # codepath §5 in docs/slippage_unification_design.md. Defect 4
        # fix: use _finite_or_none so NaN/+inf/-inf can't poison the
        # benchmark.
        recovered_stop_price = _finite_or_none(
            getattr(stop_fill, "stop_price", None)
        )
        # PR #56 R1: source position_uid for the stop-fill record so
        # restart reconstruction of the allocator's trade-count dedup
        # matches live behavior.
        stop_fill_position_uid: str | None = None
        try:
            _row = self.lifecycle_store.get_open_for_owner_key(
                owner_key_for(raw_symbol),
            )
            if _row is not None:
                stop_fill_position_uid = _row.position_uid
        except Exception as exc:
            logger.debug(
                f"log_stop_fill: position_uid lookup raised "
                f"{type(exc).__name__}: {exc} — proceeding without"
            )
        self.trade_logger.log_stop_fill(
            symbol=raw_symbol,
            strategy=owner,
            qty=qty,
            avg_fill_price=price,
            stop_price=recovered_stop_price,
            measurement_quality="recovered",
            order_id=stop_fill.order_id,
            timestamp_override=stop_fill.filled_at or stop_fill.submitted_at,
            position_uid=stop_fill_position_uid,
        )
        pnl_multiplier = 100 if _OCC_PAT.match(raw_symbol) else 1
        self._record_realized_pnl(
            symbol,
            owner,
            price,
            qty,
            multiplier=pnl_multiplier,
        )
        logger.warning(
            f"{raw_symbol}: recovered missed protective stop fill from broker history "
            f"— qty={qty} price={price} order_id={stop_fill.order_id}"
        )
        self._cleanup_option_trailing_state(
            raw_symbol,
            reason="broker-history stop recovery",
        )
        return True

    def _repair_missing_protective_stops(
        self,
        snapshot: BrokerSnapshot,
        *,
        allow_residual_cleanup: bool = True,
    ) -> None:
        """
        Ensure every managed broker position still has a protective stop.

        Alpaca expires GTC orders after 90 days, and earlier runs also left
        some positions unprotected because attached stops were submitted as DAY.
        This reconciliation restores the original fixed stop from the trade log
        whenever a managed position has no broker-side stop order.
        """
        for symbol, position in snapshot.account.open_positions.items():
            if _OCC_PAT.match(symbol):
                # Options positions use Alpaca-managed bracket stop legs.
                # Equity-style stop repair does not apply to OCC symbols.
                continue
            owner = self._get_owner(symbol)
            if owner is None:
                continue
            stop_qty = abs(int(position.qty))
            existing = self._protective_stop_order(symbol, snapshot)
            if existing is not None:
                if str(existing.time_in_force or "").lower() != "day":
                    continue
                if stop_qty < 1:
                    if allow_residual_cleanup:
                        self._close_fractional_residual_position(
                            snapshot=snapshot,
                            symbol=symbol,
                            owner=owner,
                            position=position,
                        )
                    else:
                        logger.debug(
                            f"{symbol}: deferring fractional residual cleanup "
                            "and DAY-stop promotion until a market-open cycle"
                        )
                    continue
                failure_key = (symbol, existing.order_id)
                # P-5: look up position_uid for the replacement_stop
                # substrate row.
                _promote_uid: str | None = None
                if self.lifecycle_store is not None:
                    try:
                        _row = self.lifecycle_store.get_open_for_owner_key(
                            owner_key_for(symbol),
                        )
                        if _row is not None:
                            _promote_uid = _row.position_uid
                    except Exception as exc:
                        logger.debug(
                            f"repair-promote position_uid lookup raised "
                            f"{type(exc).__name__}: {exc} — proceeding"
                        )
                try:
                    promoted = self.broker.promote_equity_stop_to_gtc(
                        parent_order_id=None,
                        stop_order_id=existing.order_id,
                        qty=stop_qty,
                        stop_price=float(existing.stop_price),
                        client_order_id_prefix=f"{owner}-repair-stop-gtc",
                        position_uid=_promote_uid,
                    )
                    self._reported_stop_promotion_failures.discard(failure_key)
                    snapshot.open_orders.remove(existing)
                    snapshot.open_orders.append(promoted)
                    logger.warning(
                        f"{symbol}: promoted DAY protective stop "
                        f"{existing.order_id} to GTC as {promoted.order_id}"
                    )
                except Exception as e:
                    msg = (
                        f"{symbol}: failed to promote DAY protective stop "
                        f"{existing.order_id} to GTC: {e}"
                    )
                    if failure_key not in self._reported_stop_promotion_failures:
                        self._reported_stop_promotion_failures.add(failure_key)
                        logger.error(msg)
                        self.risk.record_broker_error()
                        self.alerts.broker_error(msg)
                    else:
                        logger.debug(f"{msg} (already reported; retrying)")
                continue

            stop_price = self.trade_logger.read_latest_open_stop_price(
                symbol=symbol,
                strategy=owner,
            )
            if stop_price is None:
                stop_price = self._reconstruct_missing_entry_context(
                    snapshot=snapshot,
                    symbol=symbol,
                    owner=owner,
                    position=position,
                )
                if stop_price is None:
                    msg = (
                        f"{symbol}: managed position owned by '{owner}' has no "
                        "protective stop and no recoverable stop price in trade log"
                    )
                    logger.error(msg)
                    self.alerts.broker_error(msg)
                    continue

            if stop_qty < 1:
                if allow_residual_cleanup:
                    self._close_fractional_residual_position(
                        snapshot=snapshot,
                        symbol=symbol,
                        owner=owner,
                        position=position,
                    )
                else:
                    logger.debug(
                        f"{symbol}: deferring fractional residual cleanup "
                        "until a market-open cycle"
                    )
                continue

            try:
                # P-4: look up position_uid so the broker can record
                # a protective_stop substrate row alongside the
                # repair stop.
                _repair_uid: str | None = None
                if self.lifecycle_store is not None:
                    try:
                        _row = self.lifecycle_store.get_open_for_owner_key(
                            owner_key_for(symbol),
                        )
                        if _row is not None:
                            _repair_uid = _row.position_uid
                    except Exception as exc:
                        logger.debug(
                            f"repair stop position_uid lookup raised "
                            f"{type(exc).__name__}: {exc} — proceeding "
                            f"without substrate row"
                        )
                repaired = self.broker.place_protective_stop(
                    symbol=symbol,
                    qty=stop_qty,
                    stop_price=stop_price,
                    client_order_id_prefix=f"{owner}-repair-stop",
                    position_uid=_repair_uid,
                )
                logger.warning(
                    f"{symbol}: restored missing protective stop at "
                    f"${stop_price:.2f} as {repaired.order_id}"
                )
            except Exception as e:
                msg = f"{symbol}: failed to restore missing protective stop: {e}"
                logger.error(msg)
                self.risk.record_broker_error()
                self.alerts.broker_error(msg)

    def _reconstruct_missing_entry_context(
        self,
        *,
        snapshot: BrokerSnapshot,
        symbol: str,
        owner: str,
        position: Position,
    ) -> float | None:
        """
        Best-effort fallback when a managed equity position has no trade-log context.

        Uses the assigned strategy plus the latest completed bar to reconstruct
        the original-style stop, then persists a recovered entry record from the
        broker position so normal stop-repair can continue.

        When Alpaca order history can identify the original filled entry,
        the recovered trade row should reuse broker ``filled_at`` rather
        than the later restart/recovery time. The row is a reconstruction,
        not a new fill.
        """
        if _OCC_PAT.match(symbol):
            return None

        slot = next(
            (
                s
                for s in self.slots
                if s.strategy.name == owner and symbol in s.active_symbols()
            ),
            None,
        )
        if slot is None:
            return None

        end = self._clock()
        lookback_days = _lookback_days(
            slot.strategy.required_bars(),
            slot.timeframe,
            self.config.history_lookback_days,
        )
        start = end - timedelta(days=lookback_days)
        try:
            # Live engine path — same feed as the original trading cycle.
            from config.settings import ALPACA_DATA_FEED
            raw_df, _stats = fetch_symbol(
                symbol, start, end, timeframe=slot.timeframe, feed=ALPACA_DATA_FEED
            )
        except Exception as e:
            logger.warning(
                f"{symbol}: failed to reconstruct missing entry context from market data: {e}"
            )
            return None
        if raw_df.empty:
            return None

        decision_df, _using_prior_completed_bar = self._decision_frame(
            raw_df,
            slot.timeframe,
            market_open=self._market_open(),
        )
        if decision_df.empty:
            return None

        df = add_atr(decision_df, self.config.atr_length)
        latest_atr = float(df[f"atr_{self.config.atr_length}"].iloc[-1])
        latest_close = float(df["close"].iloc[-1])
        entry_price = float(
            getattr(position, "avg_entry_price", 0.0) or latest_close
        )

        signal = Signal(
            symbol=symbol,
            side=Side.BUY,
            strategy_name=owner,
            reference_price=latest_close,
            atr=latest_atr,
            reason=f"{owner} recovered entry context",
            order_type=slot.strategy.preferred_order_type,
            limit_price=latest_close
            if slot.strategy.preferred_order_type is OrderType.LIMIT
            else None,
        )
        stop_price = self.risk._stop_price_for(signal)

        recovered_decision = RiskDecision(
            symbol=symbol,
            side=Side.BUY,
            qty=float(position.qty),
            entry_reference_price=entry_price,
            stop_price=stop_price,
            strategy_name=owner,
            reason=f"{owner} recovered entry context",
            order_type=slot.strategy.preferred_order_type,
            limit_price=entry_price
            if slot.strategy.preferred_order_type is OrderType.LIMIT
            else None,
        )
        recovered_result = OrderResult(
            status=OrderStatus.FILLED,
            order_id=None,
            symbol=symbol,
            requested_qty=float(position.qty),
            filled_qty=float(position.qty),
            avg_fill_price=entry_price,
            raw_status="recovered",
            message="recovered from broker position",
        )
        recovered_fill = None
        try:
            recovered_fill = self.broker.find_recent_filled_entry_order(symbol=symbol)
        except Exception as e:
            logger.warning(
                f"{symbol}: could not fetch historical filled entry for recovery timestamp: {e}"
            )
        if recovered_fill is not None:
            recovered_result = replace(
                recovered_result,
                order_id=recovered_fill.order_id,
            )
        # Issue A: recovered rows have no honest arrival-price benchmark
        # — the original submission happened before the bot's current
        # process started. Write NULL on slippage columns rather than
        # synthesizing a phantom from the gap between today's bar close
        # and the historical broker avg_entry. Defensive double-fix
        # paired with the assessor filter for legacy rows already
        # written prior to this change.
        recovered_timestamp = None
        if recovered_fill is not None:
            recovered_timestamp = recovered_fill.filled_at or recovered_fill.submitted_at

        # Operator Controls Phase A PR-2 — Gap 1 fix.
        # The recovery path reconstructs a position the engine
        # already missed; without this hook the trade-log row
        # lands with position_uid=NULL until the next startup
        # backfill rescues it. Synthesize the lifecycle row now
        # so the operator CLI sees identity right away and so
        # downstream subsystems (option_trailing) can key off
        # position_uid for this recovered position.
        # synthesize_for_existing is idempotent: if a row already
        # exists for the owner_key, it returns that uid unchanged.
        if self.lifecycle_store is not None:
            try:
                owner_key_val = owner_key_for(symbol)
                recovered_uid = self.lifecycle_store.synthesize_for_existing(
                    symbol=symbol,
                    owner_key=owner_key_val,
                    strategy=owner,
                    position_type="single_leg",
                    current_qty=float(position.qty),
                    avg_entry_price=entry_price,
                    first_fill_at=(
                        recovered_timestamp.isoformat()
                        if recovered_timestamp is not None
                        else None
                    ),
                    backfill_note=(
                        "synthesized at entry-context recovery "
                        f"from broker position (qty={float(position.qty)})"
                    ),
                )
                recovered_result = replace(
                    recovered_result,
                    position_uid=recovered_uid,
                )
            except Exception as exc:
                logger.warning(
                    f"{symbol}: recovery lifecycle synthesis failed: {exc}"
                )

        # Slippage unification (Phase 1) codepath §8 — reconstructed
        # entry context has no honest arrival benchmark (the live
        # submission predates this process), but the row is still
        # tagged quality='recovered' so consumers can isolate
        # reconstruction rows from real live entries.
        self._log_entry(
            recovered_decision,
            recovered_result,
            latest_close,
            record_slippage=False,
            timestamp_override=recovered_timestamp,
            measurement_quality="recovered",
        )
        self._entry_prices[symbol] = entry_price
        logger.warning(
            f"{symbol}: reconstructed missing entry context for '{owner}' "
            f"using broker avg_entry=${entry_price:.2f} and ATR stop=${stop_price:.2f}"
        )
        return stop_price

    def _cleanup_stale_orders(self, snapshot: BrokerSnapshot, order_strategy: dict[str, str]) -> None:
        """
        Cancel any entry LIMIT orders that have been open for too long.
        This prevents 'ghost fills' where a limit order from days ago
        suddenly executes after market conditions have completely changed.
        """
        now = datetime.now(timezone.utc)
        max_age = settings.STALE_LIMIT_MAX_AGE_SECONDS
        
        for order in snapshot.open_orders:
            # Only consider orders that are identified as ENTRY orders
            strategy_name = order_strategy.get(order.order_id)
            if not strategy_name:
                continue
                
            # Only consider LIMIT orders
            if getattr(order.order_type, "value", str(order.order_type)) != "limit":
                continue
                
            # Check age
            age_seconds = (now - order.submitted_at).total_seconds()
            if age_seconds > max_age:
                logger.warning(
                    f"{order.symbol}: canceling stale entry limit order "
                    f"{order.order_id} ({age_seconds:.0f}s old, max {max_age}s) "
                    f"for strategy '{strategy_name}'"
                )
                self.broker.cancel_order(order.order_id)

    # ── Startup ownership + reconciliation (10.C1 / 10.C2) ─────────────

    def _detect_external_closes(self, snapshot: BrokerSnapshot) -> None:
        """
        Detect positions that disappeared from the broker without the bot
        placing the closing order (stop-out, manual liquidation, margin call).

        A position must be absent for ``config.external_close_confirm_cycles``
        consecutive cycles before we act. This guards against transient broker
        API blips that return incomplete position data — a single-cycle absence
        is treated as a suspect, not a confirmed close.

        When confirmed:
          - Logs a WARNING and fires an alert.
          - Writes a synthetic sell to the trade DB so ``read_all_open_owners``
            does not treat the stale buy record as open on the next restart.
          - Clears ownership so stop-repair logic ignores the symbol.

        If a suspected position reappears (API blip recovered), the counter
        resets silently.

        With WebSocket order/fill streaming (Phase 10), genuine stop-outs and
        manual liquidations will be detected via fill events with the real fill
        price. This method then serves only as a fallback for WebSocket gaps.
        """
        confirm = self.config.external_close_confirm_cycles

        for symbol, tracked_position in list(self._positions.items()):
            position_present = self._get_position_for(symbol, snapshot) is not None
            if tracked_position.is_spread:
                broker_symbols = set(snapshot.account.open_positions)
                present_legs = [
                    leg.symbol for leg in tracked_position.legs
                    if leg.symbol in broker_symbols
                ]
                if len(present_legs) == len(tracked_position.legs):
                    position_present = True
                elif present_legs:
                    missing_legs = [
                        leg.symbol for leg in tracked_position.legs
                        if leg.symbol not in broker_symbols
                    ]
                    msg = (
                        f"{symbol}: spread owned by '{tracked_position.strategy_name}' "
                        f"is partially present at broker; missing leg(s) {missing_legs}. "
                        "Leaving ownership intact for manual reconciliation."
                    )
                    logger.warning(msg)
                    self.alerts.broker_error(msg)
                    self._external_close_suspects.pop(symbol, None)
                    continue

            if position_present:
                # Position is present — reset any suspect counter and continue.
                self._external_close_suspects.pop(symbol, None)
                continue

            count = self._external_close_suspects.get(symbol, 0) + 1
            self._external_close_suspects[symbol] = count

            if count < confirm:
                logger.debug(
                    f"{symbol}: absent from broker positions "
                    f"({count}/{confirm} cycles) — awaiting confirmation"
                )
                continue

            # Confirmed absent for `confirm` consecutive cycles.
            owner = tracked_position.strategy_name
            self._external_close_suspects.pop(symbol, None)
            try:
                if tracked_position.is_spread:
                    self._pop_position(symbol)
                    msg = (
                        f"{symbol}: position owned by '{owner}' absent for "
                        f"{confirm} consecutive cycle(s) — declared externally closed "
                        "(stop-out, manual liquidation, or margin call)"
                    )
                    logger.warning(msg)
                    self.alerts.broker_error(msg)
                    strategy = self._spread_owner_strategy.pop(symbol, None)
                    released = (
                        strategy.release_spread(symbol)
                        if strategy is not None and hasattr(strategy, "release_spread")
                        else None
                    )
                    short_occ = released.short_occ if released is not None else tracked_position.legs[0].symbol
                    long_occ = released.long_occ if released is not None else tracked_position.legs[1].symbol
                    qty = float(released.qty if released is not None else abs(tracked_position.legs[0].qty))
                    # Recompute basis when `released` is available so the
                    # external-close row carries initial_risk_dollars even
                    # though realized_pnl is unknown. Falls back to the
                    # logger's DB lookup when released is None.
                    ext_close_risk_dollars: float | None = None
                    if released is not None:
                        ext_spread_max_loss = (
                            (released.width - released.net_credit) * 100.0
                        )
                        if ext_spread_max_loss > 0:
                            ext_close_risk_dollars = ext_spread_max_loss * qty
                    self.trade_logger.log_spread_fill(
                        position_id=symbol,
                        strategy=owner,
                        short_occ=short_occ,
                        long_occ=long_occ,
                        qty=qty,
                        net_price=0.0,
                        order_id=None,
                        opening=False,
                        realized_pnl=None,
                        reason="external_close_detected",
                        initial_risk_dollars=ext_close_risk_dollars,
                    )
                else:
                    stop_fill = self._lookup_recent_stop_fill(symbol=symbol, owner=owner)
                    self._pop_position(symbol)
                    if stop_fill is not None:
                        self._record_recovered_stop_fill(
                            symbol=symbol,
                            owner=owner,
                            stop_fill=stop_fill,
                        )
                        logger.warning(
                            f"{symbol}: position owned by '{owner}' absent for "
                            f"{confirm} consecutive cycle(s) — reconciled as "
                            "protective stop fill from broker history"
                        )
                    else:
                        exit_fills = self._lookup_recent_exit_fills(
                            symbol=symbol,
                            owner=owner,
                        )
                        if exit_fills:
                            for index, exit_fill in enumerate(exit_fills):
                                self._record_recovered_exit_fill(
                                    symbol=symbol,
                                    owner=owner,
                                    exit_fill=exit_fill,
                                    alert_reason="broker_history_sell_recovered",
                                    is_full_close=index == len(exit_fills) - 1,
                                    external=True,
                                )
                            logger.warning(
                                f"{symbol}: position owned by '{owner}' absent for "
                                f"{confirm} consecutive cycle(s) — reconciled from "
                                f"{len(exit_fills)} filled SELL order(s) in broker history"
                            )
                        else:
                            msg = (
                                f"{symbol}: position owned by '{owner}' absent for "
                                f"{confirm} consecutive cycle(s) — declared externally closed "
                                "(stop-out, manual liquidation, or margin call)"
                            )
                            logger.warning(msg)
                            self.alerts.broker_error(msg)
                            self.trade_logger.log_external_close(
                                symbol=symbol,
                                strategy=owner,
                                reason="external_close_detected",
                            )
                            # Operator Controls Phase A — close the
                            # lifecycle row directly when no real fill can
                            # be recovered from broker history.
                            self._close_lifecycle_for_owner_key(
                                owner_key=owner_key_for(symbol),
                                external=True,
                            )
                    leg = tracked_position.primary_leg
                    if leg is not None:
                        self._cleanup_option_trailing_state(
                            leg.symbol,
                            reason="external close",
                        )
                    self._entry_prices.pop(symbol, None)
            except Exception as e:
                logger.error(f"{symbol}: failed to log external close: {e}")

    def _process_stream_stop_fills(self, snapshot: BrokerSnapshot) -> None:
        """
        Drain WebSocket stop-leg fill events from the stream manager.

        When a protective stop triggers, Alpaca sends a fill event for the
        stop-leg order. StreamManager accumulates these in drain_stop_fills().
        We process them here each cycle — before _detect_external_closes —
        so the ownership map is already cleared when the cycle-count fallback
        runs (which then finds no owned symbols absent, and does nothing).

        This gives immediate detection of stop-outs rather than waiting for
        external_close_confirm_cycles cycles.
        """
        if self._stream_manager is None:
            return

        for update in self._stream_manager.drain_stop_fills():
            raw_symbol = update.order.symbol
            # OCC bracket stop legs carry the full OCC string (e.g. SPY260516C00520000).
            # _positions is keyed by the underlying ("SPY") for OCC option fills,
            # so normalise before lookup; keep raw_symbol for logging and
            # trade-DB calls.
            _occ_m = _OCC_PAT.match(raw_symbol)
            symbol = owner_key_for(raw_symbol)

            if not self._has_position(symbol):
                logger.debug(
                    f"stream stop fill for unowned {raw_symbol} — already handled"
                )
                continue

            raw_cum_qty = getattr(update.order, "filled_qty", None)
            cum_qty = float(raw_cum_qty or 0) if raw_cum_qty is not None else 0.0
            raw_cum_avg = getattr(update.order, "filled_avg_price", None)
            cum_avg = float(raw_cum_avg) if raw_cum_avg is not None else None
            # Stream trade updates carry per-execution chunk fields on
            # update.qty/update.price, but stop-fill accounting must use the
            # cumulative order fill quantity / VWAP to avoid under-recording
            # multi-execution stop orders.
            qty = cum_qty if cum_qty > 0 else float(update.qty or 0)
            price = cum_avg if cum_avg is not None else (
                float(update.price) if update.price is not None else None
            )
            owner = self._get_owner(symbol)
            if owner is None:
                logger.debug(
                    f"stream stop fill for unowned {raw_symbol} — already handled"
                )
                continue
            order_id = getattr(update.order, "id", None)
            if self.trade_logger.has_recorded_order_id(order_id):
                logger.debug(
                    f"{raw_symbol}: duplicate protective stop fill "
                    f"{order_id} ignored — already recorded"
                )
                continue
            msg = (
                f"{raw_symbol}: protective stop triggered (WebSocket) — "
                f"qty={qty} price={price} strategy={owner}"
            )
            residual_position = self._get_position_for(symbol, snapshot)
            residual_qty = (
                float(residual_position.qty)
                if residual_position is not None and not _occ_m
                else 0.0
            )
            logger.warning(msg)
            self.alerts.broker_error(msg)
            # Feed realized P&L into the HWM drawdown gate.
            # Options: multiply by 100 (each contract = 100 shares).
            if price is not None and qty > 0:
                _pnl_mult = 100 if _occ_m else 1
                self._record_realized_pnl(symbol, owner, price, qty, multiplier=_pnl_mult)
            try:
                if price is not None and qty > 0:
                    # Slippage unification (Phase 1) — extract the broker
                    # order's actual stop trigger price so log_stop_fill can
                    # benchmark against the active stop that fired, not the
                    # original initial_stop_loss. See codepath §4 in
                    # docs/slippage_unification_design.md. Defect 4 fix:
                    # use _finite_or_none so NaN/+inf/-inf can't poison
                    # the benchmark even if a misbehaving stream payload
                    # delivers a malformed stop_price.
                    broker_stop_price = _finite_or_none(
                        getattr(update.order, "stop_price", None)
                    )
                    # PR #56 R1: source position_uid for the stop-fill
                    # record so restart reconstruction of the
                    # allocator's trade-count dedup matches live behavior.
                    _stop_position_uid: str | None = None
                    try:
                        _row = self.lifecycle_store.get_open_for_owner_key(
                            owner_key_for(raw_symbol),
                        )
                        if _row is not None:
                            _stop_position_uid = _row.position_uid
                    except Exception as exc:
                        logger.debug(
                            f"log_stop_fill: position_uid lookup raised "
                            f"{type(exc).__name__}: {exc} — proceeding without"
                        )
                    stop_log_kwargs = {
                        "symbol": raw_symbol,
                        "strategy": owner,
                        "qty": qty,
                        "avg_fill_price": price,
                        "stop_price": broker_stop_price,
                        "order_id": order_id,
                        "position_uid": _stop_position_uid,
                    }
                    stop_timestamp = (
                        getattr(update.order, "filled_at", None)
                        or getattr(update.order, "submitted_at", None)
                    )
                    if isinstance(stop_timestamp, str):
                        try:
                            stop_timestamp = datetime.fromisoformat(
                                stop_timestamp.replace("Z", "+00:00")
                            )
                        except ValueError:
                            stop_timestamp = None
                    if stop_timestamp is not None:
                        stop_log_kwargs["timestamp_override"] = stop_timestamp
                    self.trade_logger.log_stop_fill(
                        **stop_log_kwargs,
                    )
                else:
                    # Price or qty unavailable — fall back to the synthetic record.
                    self.trade_logger.log_external_close(
                        symbol=raw_symbol,
                        strategy=owner,
                        reason="stop_triggered",
                    )
                    # Operator Controls Phase A — _record_realized_pnl
                    # was skipped on this branch (no price/qty), so we
                    # close the lifecycle row directly. Matches the
                    # trade-log call above which records as external.
                    self._close_lifecycle_for_owner_key(
                        owner_key=owner_key_for(raw_symbol),
                        external=True,
                    )
            except Exception as e:
                logger.error(f"{raw_symbol}: failed to log stop fill: {e}")

            self._external_close_suspects.pop(symbol, None)
            if residual_qty > 1e-9 and not _occ_m:
                logger.info(
                    f"{raw_symbol}: protective stop left residual qty={residual_qty} "
                    "— preserving ownership for residual cleanup"
                )
                continue

            self._pop_position(symbol)
            self._entry_prices.pop(symbol, None)
            self._cleanup_option_trailing_state(
                raw_symbol,
                reason="stream stop fill",
            )

    def _drain_lifecycle_attaches(self) -> None:
        """Apply per-order substrate attaches enqueued by background
        worker threads.

        PR #60 round 2 fix (finding 2): the shared sqlite3 connection
        is opened on the main thread; background workers
        (OptionsExecutionWorker today, others in commits 11+) cannot
        call attach_broker_order_id directly. They enqueue
        (client_order_id, broker_order_id, submitted_at); we drain
        and apply on this — the connection-owning — thread.

        Best-effort: an individual attach failure is logged at
        CRITICAL and absorbed so a broken substrate row cannot stall
        the cycle. Same discipline as
        ``_lifecycle_orders_attach_order_id`` in the broker."""
        if self.lifecycle_orders_store is None:
            return
        attaches = self.broker.drain_lifecycle_attaches()
        for client_order_id, order_id, submitted_at in attaches:
            try:
                self.lifecycle_orders_store.attach_broker_order_id(
                    client_order_id=client_order_id,
                    order_id=order_id,
                    submitted_at=submitted_at,
                )
            except Exception as exc:
                # PR #61 round-2 fix P2-2 honesty: the cycle and
                # startup reconcilers query `order_id IS NOT NULL`
                # only, so they do NOT pick up rows whose attach
                # failed (order_id still NULL). The lifecycle-
                # attach queue itself is in-memory and lost on bot
                # restart. This row is ORPHANED until a NULL-order_id
                # recovery path lands (tracker: 'Known follow-ups').
                # The broker order is live; operator must inspect.
                logger.critical(
                    f"queued lifecycle_orders.attach FAILED for "
                    f"{client_order_id} → {order_id}: {exc}. "
                    f"SUBSTRATE ROW ORPHANED — order_id NULL means "
                    f"REST reconcilers skip it. Inspect manually."
                )

    def _drain_lifecycle_events(
        self, snapshot: "BrokerSnapshot | None" = None,
    ) -> None:
        """P-1: apply OrderEvents enqueued by the WS thread.

        StreamManager translates each material Alpaca trade_update
        into an ``engine.lifecycle_orders.OrderEvent`` on the
        WebSocket thread and enqueues it on
        ``_pending_lifecycle_events``. This drain runs on the cycle
        thread (the connection-owning thread) and applies each via
        ``apply_order_event`` so the substrate state machine
        advances pending → working → partially_filled → filled /
        canceled / rejected.

        Outcomes:
          - ``applied=True``: substrate advanced; debug-log.
          - ``applied=False, reason='unknown_order'``: the broker
            event was for an order with no substrate row (legacy
            orders submitted before P-4..P-6 shipped, or orders
            created by a path that doesn't insert substrate rows).
            Debug-log; the legacy paths still own these.
          - ``applied=False, reason='stale_or_duplicate'``: the
            event was older than the row's last_observed_broker
            _updated_at, or the row was already in a terminal
            state. Normal — debug-log.
          - Any exception: CRITICAL log + continue. One bad event
            cannot stall the cycle.

        After P-6/P-7 the suspect caches are gone. The remaining
        legacy stream-driven trade-logging paths
        (``_process_stream_stop_fills`` for protective-stop fills,
        ``_detect_external_closes`` for unexpected broker closes)
        continue to run alongside this drain; their writes
        converge with the substrate UPSERT per the trades-
        UPSERT preservation policy (foundation commits 8 / 11 / 14).
        """
        if self.lifecycle_orders_store is None or self._stream_manager is None:
            return
        from engine.lifecycle_orders import apply_order_event

        events = self._stream_manager.drain_lifecycle_events()
        for event in events:
            try:
                outcome = apply_order_event(
                    self.lifecycle_orders_store._conn,
                    event,
                    reason="stream",
                )
            except Exception as exc:
                logger.critical(
                    f"apply_order_event raised for order_id="
                    f"{event.order_id} status={event.status}: "
                    f"{type(exc).__name__}: {exc}. Cycle continues; "
                    f"investigate substrate health."
                )
                continue
            if outcome.applied:
                logger.debug(
                    f"substrate advanced: order_id={event.order_id} "
                    f"→ status={event.status} "
                    f"(position {outcome.new_status})"
                )
                self._maybe_dispatch_substrate_entry_fill(
                    event=event, snapshot=snapshot,
                )
                self._maybe_dispatch_substrate_exit_fill(
                    event=event, snapshot=snapshot,
                )
            elif outcome.reason in {"unknown_order", "stale_or_duplicate"}:
                logger.debug(
                    f"substrate skipped event for order_id="
                    f"{event.order_id} status={event.status}: "
                    f"{outcome.reason}"
                )
            else:
                logger.info(
                    f"substrate dropped event for order_id="
                    f"{event.order_id} status={event.status}: "
                    f"{outcome.reason}"
                )

    def _reconcile_substrate_cycle(self, snapshot: "BrokerSnapshot") -> None:
        """P-2: cycle-time substrate reconciliation against broker state.

        Defense-in-depth for events the WebSocket missed (reconnect
        gaps, dropped frames, late-binding race conditions). Each
        cycle, walk up to ``_SUBSTRATE_CYCLE_RECONCILE_LIMIT``
        non-terminal substrate rows whose order_id is NOT in the
        snapshot's open_orders and apply the truth via REST.

        Rows whose order_id IS in snapshot.open_orders are skipped
        — those are still active at the broker and the stream
        owns their state transitions.
        """
        self._reconcile_substrate_via_rest(
            snapshot,
            reason="cycle",
            limit=_SUBSTRATE_CYCLE_RECONCILE_LIMIT,
        )

    def _reconcile_substrate_startup(self, snapshot: "BrokerSnapshot") -> None:
        """P-3: post-startup substrate reconciliation against broker state.

        On restart, walk ALL non-terminal substrate rows whose
        order_id is NOT in the broker's current open_orders. These
        are orders that became terminal at the broker while the bot
        was down — fill events, cancellations, expirations the
        stream wasn't connected to receive.

        No per-call limit: startup is a one-shot operation and the
        backlog could be arbitrarily large after a long downtime.
        Per-row failures log CRITICAL and continue; one bad row
        cannot stall startup.
        """
        self._reconcile_substrate_via_rest(
            snapshot, reason="startup", limit=None,
        )

    def _reconcile_substrate_via_rest(
        self,
        snapshot: "BrokerSnapshot",
        *,
        reason: str,
        limit: int | None,
    ) -> None:
        """Shared implementation for P-2 (cycle) and P-3 (startup)
        substrate-vs-broker reconciliation.

        Algorithm:
          1. Query substrate non-terminal rows with order_id NOT
             NULL.
          2. Build broker_open_by_id from snapshot.open_orders.
          3. For each substrate row:
               - If order_id IS in open_orders → stream owns it;
                 skip.
               - Else → fetch get_order_by_id via REST, translate
                 to OrderEvent, apply.
          4. Per-row failures log CRITICAL and continue.

        ``reason`` flows through to apply_order_event so the
        substrate's audit reflects whether each advance came from
        cycle or startup reconciliation.
        """
        if self.lifecycle_orders_store is None:
            return
        try:
            # PR #61 round-1 fix P2-1: query unbounded so the limit
            # caps actual REST calls, not rows read. Round-1 reviewer
            # noted that the old 'limit at query time' could let 20
            # long-lived GTC stops fill every batch and permanently
            # starve newer terminal rows of reconciliation.
            rows = self.lifecycle_orders_store.get_non_terminal_with_order_id(
                limit=None,
            )
        except Exception as exc:
            logger.critical(
                f"substrate {reason} reconcile: get_non_terminal "
                f"query failed: {type(exc).__name__}: {exc}"
            )
            return
        if not rows:
            return
        open_by_id = {
            o.order_id: o for o in snapshot.open_orders
            if getattr(o, "order_id", None)
        }
        from engine.lifecycle_orders import apply_order_event

        rest_calls = 0
        for row in rows:
            oid = row.order_id
            if oid is None:
                continue
            if oid in open_by_id:
                # Still active at the broker; the stream owns it.
                continue
            # Enforce the REST-call cap on candidates that
            # actually need a fetch, AFTER the open-orders filter.
            if limit is not None and rest_calls >= limit:
                logger.debug(
                    f"substrate {reason} reconcile: hit REST cap "
                    f"({limit}); remaining rows catch up next pass"
                )
                break
            rest_calls += 1
            # Order is non-terminal in substrate but absent from
            # broker open orders → terminal at the broker. Fetch
            # the truth.
            try:
                broker_order = self.broker._with_retry(
                    lambda oid=oid: self.broker._api.get_order_by_id(oid),
                    op_desc=f"substrate_reconcile_{reason}({oid})",
                )
            except Exception as exc:
                logger.critical(
                    f"substrate {reason} reconcile: get_order_by_id "
                    f"failed for {oid}: {type(exc).__name__}: {exc}."
                )
                continue
            event = self._build_substrate_event_from_broker_order(
                broker_order, oid,
            )
            if event is None:
                continue
            try:
                outcome = apply_order_event(
                    self.lifecycle_orders_store._conn,
                    event,
                    reason=reason,
                )
            except Exception as exc:
                logger.critical(
                    f"substrate {reason} reconcile: apply_order_event "
                    f"raised for {oid} status={event.status}: "
                    f"{type(exc).__name__}: {exc}"
                )
                continue
            if outcome.applied:
                logger.info(
                    f"substrate {reason} reconcile: order_id={oid} "
                    f"advanced to status={event.status} "
                    f"(position {outcome.new_status})"
                )
                self._maybe_dispatch_substrate_entry_fill(
                    event=event, snapshot=snapshot,
                )
                self._maybe_dispatch_substrate_exit_fill(
                    event=event, snapshot=snapshot,
                )
            else:
                logger.debug(
                    f"substrate {reason} reconcile: order_id={oid} "
                    f"no-op ({outcome.reason})"
                )

    @staticmethod
    def _build_substrate_event_from_broker_order(
        broker_order, order_id: str,
    ) -> "Any | None":
        """Translate an Alpaca order (REST result) to an OrderEvent.
        Mirrors stream._build_substrate_event but reads from the
        REST order object's status / filled_qty / filled_avg_price /
        updated_at, not from a trade_update payload."""
        from engine.lifecycle_orders import OrderEvent

        raw_status = getattr(broker_order, "status", None)
        status_val = (
            raw_status.value if hasattr(raw_status, "value")
            else str(raw_status) if raw_status is not None else None
        )
        if status_val is None:
            return None
        substrate_status = _ALPACA_STATUS_TO_SUBSTRATE_STATUS.get(status_val)
        if substrate_status is None:
            return None

        raw_filled = getattr(broker_order, "filled_qty", None)
        try:
            filled_qty = float(raw_filled) if raw_filled is not None else 0.0
        except (TypeError, ValueError):
            filled_qty = 0.0

        raw_avg = getattr(broker_order, "filled_avg_price", None)
        try:
            avg_fill_price = (
                float(raw_avg) if raw_avg is not None else None
            )
        except (TypeError, ValueError):
            avg_fill_price = None

        updated_at_raw = (
            getattr(broker_order, "updated_at", None)
            or getattr(broker_order, "filled_at", None)
            or getattr(broker_order, "submitted_at", None)
        )
        if updated_at_raw is None:
            updated_at = datetime.now(timezone.utc).isoformat()
        else:
            updated_at = str(updated_at_raw)

        return OrderEvent(
            order_id=order_id,
            status=substrate_status,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            broker_updated_at=updated_at,
            execution_id=None,
        )

    def _reconcile_substrate_spread_closes(
        self, snapshot: "BrokerSnapshot", *, reason: str,
    ) -> None:
        """§10.7: walk non-terminal spread close substrate rows and
        resolve any whose broker order is now terminal.

        Separate from `_reconcile_substrate_via_rest` because spreads
        cannot go through `apply_order_event` (single-leg-scoped
        trades UPSERT, position-rollup CTE doesn't apply to spread
        sides). Uses `mark_terminal_after_dispatch` to advance per-
        order rows individually.

        ``reason`` is 'cycle' or 'startup' — flows through to logs so
        the operator can grep advance events by source. Cycle should
        run every cycle; startup once at boot. The query is bounded
        by the number of open spreads × 2 close roles, so unlike
        single-leg there's no need for a REST-call cap.

        Skips rows with order_id IS NULL — the partial_close
        residual placeholder has no broker order to query and is
        cleared either by the next dispatch cycle (which advances
        the same row) or by an operator action.
        """
        if (
            self.lifecycle_orders_store is None
            or self.lifecycle_store is None
        ):
            return
        try:
            rows = (
                self.lifecycle_orders_store
                .get_non_terminal_spread_close_rows()
            )
        except Exception as exc:
            logger.critical(
                f"spread close {reason} reconcile: query failed: "
                f"{type(exc).__name__}: {exc}"
            )
            return
        if not rows:
            return
        open_by_id = {
            o.order_id: o for o in snapshot.open_orders
            if getattr(o, "order_id", None)
        }
        for row in rows:
            oid = row.order_id
            if oid is None:
                continue  # placeholder row; nothing to fetch
            if oid in open_by_id:
                continue  # still working at the broker
            try:
                broker_order = self.broker._with_retry(
                    lambda oid=oid: self.broker._api.get_order_by_id(oid),
                    op_desc=f"spread_close_reconcile_{reason}({oid})",
                )
            except Exception as exc:
                logger.critical(
                    f"spread close {reason} reconcile: get_order_by_id "
                    f"failed for {oid}: {type(exc).__name__}: {exc}"
                )
                continue
            event = self._build_substrate_event_from_broker_order(
                broker_order, oid,
            )
            if event is None:
                continue
            try:
                self.lifecycle_orders_store.mark_terminal_after_dispatch(
                    client_order_id=row.client_order_id,
                    broker_order_id=oid,
                    status=event.status,
                    filled_qty=event.filled_qty,
                    avg_fill_price=event.avg_fill_price,
                    broker_updated_at=event.broker_updated_at,
                )
                logger.info(
                    f"spread close {reason} reconcile: order_id={oid} "
                    f"role={row.role} advanced to status={event.status} "
                    f"qty={event.filled_qty}"
                )
            except Exception as exc:
                logger.critical(
                    f"spread close {reason} reconcile: "
                    f"mark_terminal_after_dispatch raised for "
                    f"client_order_id={row.client_order_id}: "
                    f"{type(exc).__name__}: {exc}"
                )

    def _drain_option_fills(self) -> None:
        """
        Process async fill events reported by OptionsExecutionWorker threads.

        Each cycle, background workers may have resolved their async option
        entry orders (fill, cancel, or reject). This method logs fills to the
        trade DB and updates entry prices; it cleans up pre-registered
        ownership on non-fill outcomes so external-close detection doesn't
        generate spurious warnings.
        """
        import re as _re
        fills = self.broker.drain_option_fills()
        for decision, status_str, filled_qty, avg_fill_price, order_id, position_uid in fills:
            mapped = {"filled": OrderStatus.FILLED, "partially_filled": OrderStatus.PARTIAL}.get(
                status_str, OrderStatus.CANCELED
            )
            result = OrderResult(
                status=mapped,
                order_id=order_id,
                symbol=decision.symbol,
                requested_qty=decision.qty,
                filled_qty=filled_qty,
                avg_fill_price=avg_fill_price,
                raw_status=status_str,
                message=f"options async fill: {status_str}",
                position_uid=position_uid,
            )
            self._record_fill(
                result,
                modeled_price=decision.entry_reference_price,
                order_type="limit",
                side=decision.side.value,
            )
            self._log_entry(decision, result, decision.entry_reference_price)
            if result.status in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
                # PLAN 11.10f: lifecycle counter — filled_entries for
                # the async options fill path. The submitted++ already
                # happened in _process_symbol when broker.place_order
                # returned ACCEPTED; this is the deferred fill confirm.
                # Counter accumulator is reset each cycle but option
                # fills land asynchronously across cycles — that's OK,
                # they just land in whichever cycle's bucket they
                # arrive in, accumulated into the same weekly row via
                # upsert ON CONFLICT.
                _lc = self._lifecycle_counter_for(decision.strategy_name)
                if _lc is not None:
                    _lc.filled_entries += 1
                # Update entry price with actual fill price; find the underlying
                # symbol that was pre-registered when the worker was dispatched.
                underlying = owner_key_for(decision.symbol)
                if underlying != decision.symbol and avg_fill_price:
                    if underlying in self._positions:
                        self._entry_prices[underlying] = avg_fill_price
                    strategy = self._strategy_by_name(decision.strategy_name)
                    if self.option_trailing_store is not None:
                        try:
                            self.option_trailing_store.upsert(
                                position_uid=position_uid,
                                occ_symbol=decision.symbol,
                                strategy=decision.strategy_name,
                                owner_key=underlying,
                                qty=float(filled_qty or decision.qty),
                                entry_premium=float(avg_fill_price),
                                hwm_premium=float(avg_fill_price),
                                trail_activation_pct=float(
                                    getattr(strategy, "trail_activation_pct", 0.10)
                                ),
                                trail_pct=float(getattr(strategy, "trail_pct", 0.15)),
                                current_stop_price=round(
                                    float(avg_fill_price)
                                    * float(
                                        getattr(
                                            getattr(strategy, "config", None),
                                            "stop_loss_multiple",
                                            0.75,
                                        )
                                    ),
                                    2,
                                ),
                                stop_order_status="pending_sync",
                                last_observed_premium=float(avg_fill_price),
                            )
                        except Exception as e:
                            logger.warning(
                                f"[{decision.strategy_name}] option trailing "
                                f"state seed failed for {decision.symbol}: {e}"
                            )
                    # A3: anchor the strategy's trailing-stop base to the
                    # confirmed fill premium so the activation threshold is
                    # measured against actual cost basis, not the first
                    # Black-Scholes valuation. Opt-in via register_fill so
                    # strategies without trailing logic are unaffected.
                    register_fill = getattr(strategy, "register_fill", None)
                    if register_fill is not None and avg_fill_price:
                        try:
                            register_fill(decision.symbol, float(avg_fill_price))
                        except Exception as e:
                            logger.warning(
                                f"[{decision.strategy_name}] register_fill failed "
                                f"for {decision.symbol}: {e}"
                            )
                self.alerts.trade_executed(
                    symbol=decision.symbol,
                    strategy=decision.strategy_name,
                    side="buy",
                    qty=filled_qty,
                    price=avg_fill_price or decision.entry_reference_price,
                    reason=f"{decision.strategy_name} options entry",
                )
            else:
                # Order was canceled/rejected — remove pre-registered ownership
                # so the symbol is not mistakenly tracked as an open position.
                underlying = owner_key_for(decision.symbol)
                pre_registered = self._positions.get(underlying)
                if (
                    underlying != decision.symbol
                    and pre_registered is not None
                    and pre_registered.strategy_name == decision.strategy_name
                ):
                    logger.info(
                        f"[{decision.strategy_name}] options order canceled/rejected "
                        f"({decision.symbol}) — removing pre-registered ownership"
                    )
                    self._pop_position(underlying)
                    self._entry_prices.pop(underlying, None)

    def _process_single_leg_emergency_exit(
        self,
        *,
        symbol: str,
        strategy: BaseStrategy,
        position,
        snapshot: BrokerSnapshot,
        latest_close: float,
    ) -> bool:
        """
        Run risk-reducing strategy hooks even when the signal bar was already
        processed. Entries stay de-duped; option time/delta/trailing exits do not.
        """
        try:
            emergency_exit = strategy.inspect_open_positions(position, latest_close)
        except Exception as e:
            logger.error(f"[{strategy.name}] {symbol}: inspect_open_positions failed: {e}")
            return False
        if not emergency_exit:
            return False

        logger.warning(
            f"[{strategy.name}] {symbol}: EMERGENCY EXIT triggered by strategy hook."
        )
        owner = self._get_owner(symbol)
        if owner is not None and owner != strategy.name:
            logger.info(
                f"[{strategy.name}] {symbol}: emergency exit ignored — "
                f"position owned by '{owner}'"
            )
            return False
        if self._has_pending_close_order(symbol, snapshot):
            logger.info(
                f"{symbol}: emergency exit but a close order is already pending — skipping"
            )
            return False

        return self._close_single_leg_position(
            symbol=symbol,
            strategy=strategy,
            position=position,
            snapshot=snapshot,
            latest_close=latest_close,
            alert_reason="emergency exit",
        )

    def _close_single_leg_position(
        self,
        *,
        symbol: str,
        strategy: BaseStrategy,
        position,
        snapshot: BrokerSnapshot,
        latest_close: float,
        alert_reason: str,
    ) -> bool:
        """Close a single-leg position and perform shared exit bookkeeping."""
        if self._has_pending_close_order(symbol, snapshot):
            logger.info(f"{symbol}: close requested but a close order is already pending — skipping")
            return False

        # PR-66 review F4: honor the Phase C symbol lock so a strategy
        # exit cannot race an operator close-position / reduce-position
        # on the same owner_key. The heartbeat thread (which runs the
        # operator handlers) is concurrent with the cycle thread (which
        # runs strategy exits); without the lock both could submit
        # SELL orders to Alpaca for the same shares and the second
        # would oversell. Acquire under kind='strategy_exit'; if the
        # operator already holds the lock, skip this strategy exit —
        # the operator command is in flight and will close the
        # position itself. The next cycle's signal will fire again if
        # the operator command was a partial reduce, or it'll see the
        # position gone if the operator command was a full close.
        owner_key_str = owner_key_for(position.symbol)
        lock_holder = None
        registry = getattr(self, "symbol_locks", None)
        if registry is not None:
            lock_holder = registry.acquire(
                owner_key=owner_key_str,
                kind="strategy_exit",
                identifier=strategy.name,
            )
            if lock_holder is None:
                existing = registry.is_locked(owner_key_str)
                logger.warning(
                    f"{symbol}: strategy exit deferred — owner_key locked "
                    f"by {existing!s}; operator command in flight will "
                    "handle the close"
                )
                return False

        try:
            # P-6: thread position_uid for the exit substrate row.
            _exit_uid: str | None = None
            if self.lifecycle_store is not None:
                try:
                    _row = self.lifecycle_store.get_open_for_owner_key(
                        owner_key_str,
                    )
                    if _row is not None:
                        _exit_uid = _row.position_uid
                except Exception as exc:
                    logger.debug(
                        f"{symbol}: exit position_uid lookup raised "
                        f"{type(exc).__name__}: {exc} — proceeding"
                    )
            try:
                result = self.broker.close_position(
                    position.symbol, position_uid=_exit_uid,
                )
            except Exception as e:
                logger.error(f"{symbol}: close_position failed: {e}")
                self.risk.record_broker_error()
                self.alerts.broker_error(f"{symbol} close_position: {e}")
                return False

            return self._finish_close_single_leg(
                symbol=symbol,
                strategy=strategy,
                position=position,
                latest_close=latest_close,
                alert_reason=alert_reason,
                result=result,
            )
        finally:
            if lock_holder is not None and registry is not None:
                registry.release(owner_key=owner_key_str, holder=lock_holder)

    def _finish_close_single_leg(
        self,
        *,
        symbol: str,
        strategy: BaseStrategy,
        position,
        latest_close: float,
        alert_reason: str,
        result,
    ) -> bool:
        """Post-broker-call bookkeeping for `_close_single_leg_position`.

        Extracted (PR-66 review F4 fix) so the symbol-lock acquire/release
        can wrap the broker submit in a clean try/finally without nesting
        the entire 80-line accounting body inside the try.
        """
        if not _OCC_PAT.match(position.symbol):
            # Close path is a SELL for long equity positions — side
            # matters for the kill switch's signed slippage so adverse
            # close fills (sold below modeled) are captured and price-
            # improvement close fills (sold above modeled) clamp to 0.
            self._record_fill(
                result, modeled_price=latest_close,
                order_type="market", side="sell",
            )
        # Slippage unification (Phase 1) Defect 1 fix — the exit path
        # never fetches an arrival midpoint, so tagging the row as
        # 'arrival_midpoint' was a false claim. For equities we still
        # have the latest bar close as a measurement reference, which
        # is honestly a fallback; for options we have nothing better
        # than the fill price itself, which yields a structural zero
        # and must be reported as 'unavailable' instead.
        if _OCC_PAT.match(position.symbol):
            close_modeled = result.avg_fill_price or 0.0
            close_benchmark_kind: str = "unavailable"
            close_measurement_quality: str = "unavailable"
        else:
            close_modeled = latest_close
            close_benchmark_kind = "fallback_latest_close"
            close_measurement_quality = "fallback"
        self._log_close(
            result,
            close_modeled,
            strategy.name,
            benchmark_kind=close_benchmark_kind,
            measurement_quality=close_measurement_quality,
        )
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            # P-7: _suspect_exit_orders staging removed. The substrate
            # exit row (P-6e: close_position writes role='exit' at
            # submit time) is already in place. When the close
            # eventually resolves at the broker, the substrate capture
            # pipeline (P-1 stream / P-2 cycle reconcile / P-3 startup
            # reconcile) advances the row and
            # _maybe_dispatch_substrate_exit_fill fires
            # _record_recovered_exit_fill + ownership cleanup.
            logger.warning(
                f"[{strategy.name}] {symbol}: close did not fill "
                f"(status={result.status.value}); ownership retained for retry"
            )
            return False

        close_price = result.avg_fill_price or latest_close
        close_qty = float(result.filled_qty or (position.qty if position else 0))
        self.alerts.trade_executed(
            symbol=symbol,
            strategy=strategy.name,
            side="sell",
            qty=close_qty,
            price=close_price,
            reason=alert_reason,
            position_uid=getattr(result, "position_uid", None)
                or self._lookup_position_uid_for_owner(symbol),
        )
        pnl_mult = 100 if _OCC_PAT.match(position.symbol) else 1
        # Operator Controls Phase A — only close the lifecycle row when
        # this exit was actually full. On a PARTIAL result a residual
        # position is still tracked by the broker and engine
        # (ownership is preserved by the if-FILLED guard below), so the
        # lifecycle row must stay open or the operator CLI will hide a
        # real managed residual.
        self._record_realized_pnl(
            symbol,
            strategy.name,
            close_price,
            close_qty,
            multiplier=pnl_mult,
            is_full_close=(result.status is OrderStatus.FILLED),
        )
        if result.status is OrderStatus.FILLED:
            self._pop_position(symbol)
            self._entry_prices.pop(symbol, None)
            self._cleanup_option_trailing_state(
                position.symbol,
                reason="close",
            )
        return True

    def _strategy_by_name(self, name: str) -> BaseStrategy | None:
        """Resolve a configured strategy instance from its ``name``.

        Returns ``None`` if no slot is configured with that name (e.g. the
        slot was removed but a previously-dispatched fill still drains).
        """
        for slot in self.slots:
            if slot.strategy.name == name:
                return slot.strategy
        return None

    # ── Credit-spread entry / drain / exit (11.29 PR 3b) ─────────────────

    @staticmethod
    def _entry_blocked_by_existing_position(
        strategy: BaseStrategy, position
    ) -> bool:
        """
        The single-leg "already in this position, skip re-entry" guard.

        **Skipped for multi-leg (credit-spread) strategies.** They manage
        concurrency via their own per-instance caps inside
        ``build_spread_execution`` (max_concurrent_positions,
        max_per_expiration, DTE staggering) — and ``_get_position_for()``
        regex-matches a spread *leg* OCC to the underlying, so a held spread
        would otherwise look like an "existing position" and silently block
        every subsequent spread on that underlying. The cross-strategy
        symbol-conflict check (``_get_owner``) still runs afterward, so an
        unrelated single-leg owner of the same symbol is still blocked.
        """
        if hasattr(strategy, "build_spread_execution"):
            return False
        return position is not None

    def _count_open_spreads(self) -> int:
        """
        Count every tracked multi-leg position — the engine-wide MLEG
        concurrency total passed into ``build_spread_execution`` as
        ``total_open_spreads``.

        Generalized from the original ``"credit_spread"``-only check (PLAN.md
        11.31): every spread strategy contributes to the same global total
        because they all consume the same MLEG execution and buying-power
        resources. A future spread strategy that wants its own independent
        counter can introduce one alongside this.
        """
        return sum(1 for p in self._positions.values() if p.is_spread)

    def _credit_spreads_snapshot(self) -> list[dict]:
        """
        Build the ``credit_spreads`` state-snapshot field — one dict per open
        spread, with the economics the dashboard renders. Sourced from the
        owning strategy's open-spread view (kept in sync by the entry /
        drain paths).
        """
        out: list[dict] = []
        for position_id, strategy in self._spread_owner_strategy.items():
            getter = getattr(strategy, "get_open_spread", None)
            spread = getter(position_id) if callable(getter) else None
            if spread is None:
                continue
            out.append({
                "position_id": position_id,
                "strategy": strategy.name,
                "underlying": owner_key_for(spread.short_occ),
                "short_occ": spread.short_occ,
                "long_occ": spread.long_occ,
                "short_strike": spread.short_strike,
                "long_strike": spread.long_strike,
                "width": spread.width,
                "expiration": str(spread.expiration_date),
                "net_credit": spread.net_credit,
                "qty": spread.qty,
                "pending_close": position_id in self._spreads_pending_close,
            })
        return out

    def _multi_leg_positions_snapshot(self) -> list[dict]:
        """
        Build the normalized multi-leg state snapshot consumed by the dashboard.

        Credit spreads are the first producer. The shape is intentionally not
        named after credit_spread so future MLEG strategies can reuse it.
        """
        out: list[dict] = []
        broker_positions = (
            self._last_snapshot.account.open_positions
            if self._last_snapshot is not None else {}
        )
        today = self._clock().date()
        for position_id, strategy in self._spread_owner_strategy.items():
            getter = getattr(strategy, "get_open_spread", None)
            spread = getter(position_id) if callable(getter) else None
            if spread is None:
                continue
            underlying = owner_key_for(spread.short_occ)
            config = getattr(strategy, "config", None)
            out.append(build_credit_spread_snapshot(
                position_id=position_id,
                strategy=strategy.name,
                underlying=underlying,
                short_occ=spread.short_occ,
                long_occ=spread.long_occ,
                short_strike=spread.short_strike,
                long_strike=spread.long_strike,
                expiration=spread.expiration_date,
                entry_net_price=spread.net_credit,
                width=spread.width,
                qty=spread.qty,
                broker_positions=broker_positions,
                underlying_price=self._last_underlying_prices.get(underlying),
                pending_close=position_id in self._spreads_pending_close,
                today=today,
                stop_loss_multiple=float(
                    getattr(config, "stop_loss_multiple", 2.0)
                ),
                time_stop_dte=getattr(config, "time_stop_dte", None),
            ))
        return out

    def _multi_leg_risk_notional_by_strategy(self) -> dict[str, float]:
        """
        Defined-risk multi-leg positions consume sleeve by max loss, not by
        broker leg market value. Kept strategy-name agnostic for future MLEG
        strategies that expose the same ``get_open_spread`` view.
        """
        totals: dict[str, float] = {}
        for position_id, strategy in self._spread_owner_strategy.items():
            getter = getattr(strategy, "get_open_spread", None)
            spread = getter(position_id) if callable(getter) else None
            if spread is None:
                continue
            max_loss = max(0.0, float(spread.width) - float(spread.net_credit))
            notional = max_loss * 100.0 * abs(float(spread.qty))
            totals[strategy.name] = totals.get(strategy.name, 0.0) + notional
        return totals

    def _spread_positions_for(
        self, underlying: str, strategy_name: str
    ) -> list[Position]:
        """Open spread Positions on ``underlying`` owned by ``strategy_name``.

        The short leg's OCC string carries the underlying ticker, so a
        spread belongs to ``underlying`` when ``owner_key_for(short_leg)``
        matches it."""
        out: list[Position] = []
        for pos in self._positions.values():
            if not pos.is_spread or pos.strategy_name != strategy_name:
                continue
            primary = pos.primary_leg
            if primary is not None and owner_key_for(primary.symbol) == underlying:
                out.append(pos)
        return out

    # ── Spread lifecycle substrate (§10.7 MLEG spread lifecycle PR) ──
    #
    # Mirror of the single-leg ``_lifecycle_begin`` / ``_lifecycle_mark_*``
    # helpers that live on ``execution.broker.AlpacaBroker``. Spreads
    # dispatch via the engine's ``_enter_multi_leg`` / ``_drain_spread_fills``
    # paths rather than ``broker.place_order``, so the lifecycle helpers
    # need an engine-side home.
    #
    # All three follow the same try/except → logger.warning discipline:
    # a DB I/O failure must never abort the spread submit or finalize
    # path. ``self.lifecycle_store is None`` is a legal state for the
    # legacy / test wiring and the helpers are no-ops in that case.

    def _lifecycle_begin_spread(
        self,
        *,
        position_id: str,
        strategy_name: str,
        symbol: str,
        qty: int,
        entry_client_order_id: str | None = None,
    ) -> str | None:
        """Write a ``pending`` position_lifecycle row for a spread before
        the broker submission goes out. Returns the substrate
        ``position_uid`` (or None if no store is configured / persistence
        failed).

        The substrate uid is derived deterministically from the spread's
        raw ``position_id`` via ``spread_substrate_uid`` so the engine's
        in-memory ``_positions[position_id]`` and substrate rollups can
        be joined without a side mapping table.
        """
        if self.lifecycle_store is None:
            return None
        try:
            uid = spread_substrate_uid(position_id)
            self.lifecycle_store.create_pending(
                position_uid=uid,
                symbol=symbol,
                owner_key=position_id,
                strategy=strategy_name,
                position_type="spread",
                entry_qty=float(qty),
                entry_client_order_id=entry_client_order_id,
            )
            return uid
        except Exception as exc:
            logger.warning(
                f"spread lifecycle.create_pending failed for "
                f"position_id={position_id[:8]} ({strategy_name}): {exc}"
            )
            return None

    def _lifecycle_mark_spread_open(
        self,
        *,
        position_id: str,
        avg_entry_price: float,
        current_qty: float,
    ) -> None:
        """Transition the spread's substrate row to ``open`` after the
        combo fill confirms. Best-effort; logs and absorbs failures so
        the drain path cannot crash on a substrate I/O error."""
        if self.lifecycle_store is None:
            return
        try:
            self.lifecycle_store.mark_open(
                position_uid=spread_substrate_uid(position_id),
                avg_entry_price=float(avg_entry_price),
                current_qty=float(current_qty),
            )
        except Exception as exc:
            logger.warning(
                f"spread lifecycle.mark_open failed for "
                f"position_id={position_id[:8]}: {exc}"
            )

    def _lifecycle_mark_spread_canceled(self, position_id: str) -> None:
        """Cancel the spread's substrate pending row when the open dispatch
        rolls back (canceled / rejected with zero fill)."""
        if self.lifecycle_store is None:
            return
        try:
            self.lifecycle_store.mark_canceled(
                position_uid=spread_substrate_uid(position_id),
            )
        except Exception as exc:
            logger.warning(
                f"spread lifecycle.mark_canceled skipped for "
                f"position_id={position_id[:8]}: {exc}"
            )

    # ── Spread close substrate (§10.7 — close-row writes per §6.2) ──
    #
    # The per-order substrate ``position_lifecycle_orders`` carries one
    # row per spread close attempt. The pre-submit insert is what gives
    # us restart-safe duplicate-dispatch prevention: the
    # ``uniq_one_active_close_per_position`` partial unique index blocks
    # any concurrent insert of a second non-terminal close row on the
    # same ``position_uid``.
    #
    # Client_order_id is engine-generated (deterministic from
    # position_id + a timestamp + uuid suffix) and threaded to the
    # broker via dispatch_spread_order so the drain can join back.

    def _new_spread_close_client_order_id(
        self, *, position_id: str, role: str
    ) -> str:
        """Engine-side client_order_id for a spread close substrate row.
        Format: ``spr-{role}-{position_id_prefix}-{rand}``."""
        import uuid as _uuid
        suffix = _uuid.uuid4().hex[:8]
        return f"spr-{role}-{position_id[:8]}-{suffix}"

    def _lifecycle_orders_insert_spread_close(
        self,
        *,
        position_id: str,
        role: str,
        client_order_id: str,
        qty: float,
        intended_limit_price: float | None = None,
    ) -> bool:
        """Insert a pending close-side per-order row before dispatch.

        Returns True iff the row was written (or the store is no-op).
        Returns False when ``uniq_one_active_close_per_position`` blocks
        the insert — that is the substrate-backed duplicate-dispatch
        guard firing and the caller must NOT submit the close.

        Other exceptions (FK miss, ValueError, transient I/O) log
        CRITICAL and return False so the dispatch is skipped — the
        substrate is load-bearing for restart safety and silently
        skipping it would re-open the gap.
        """
        if (
            self.lifecycle_orders_store is None
            or self.lifecycle_store is None
        ):
            return True  # legacy / test path — broker still dispatches
        try:
            self.lifecycle_orders_store.insert_pending(
                position_uid=spread_substrate_uid(position_id),
                role=role,
                client_order_id=client_order_id,
                order_type="limit",  # MLEG close uses a net-debit limit
                order_class="mleg",
                time_in_force="day",  # Alpaca mleg is DAY-TIF only
                side="buy",  # net-debit close = buy back the spread
                intended_qty=float(qty),
                intended_limit_price=intended_limit_price,
            )
            return True
        except sqlite3.IntegrityError as exc:
            # Two distinct SQLite IntegrityError shapes land here and
            # they have OPPOSITE meanings:
            #
            #   1. UNIQUE constraint failed on
            #      uniq_one_active_close_per_position → there's
            #      already a non-terminal close row on this
            #      position_uid. This IS the substrate-backed
            #      duplicate-dispatch block (R6 analog) — return
            #      False so the caller skips dispatch.
            #
            #   2. FOREIGN KEY constraint failed → the parent
            #      position_lifecycle row for this position_uid is
            #      missing. This is a setup gap (e.g. a test fixture
            #      that pre-registers spread state without calling
            #      _lifecycle_begin_spread, or a pre-C1 backfill
            #      window). Log CRITICAL but DON'T block the
            #      dispatch — better to lose substrate tracking on
            #      this one close than to regress the engine's
            #      ability to close spreads when the parent row is
            #      unexpectedly missing.
            msg = str(exc).lower()
            if "foreign key" in msg:
                logger.critical(
                    f"spread close substrate FK failure for "
                    f"position_id={position_id[:8]} role={role}: {exc} — "
                    f"parent position_lifecycle row missing; dispatching "
                    f"close without substrate tracking. Investigate "
                    f"why _lifecycle_begin_spread was not called for "
                    f"this position."
                )
                return True
            logger.info(
                f"spread close substrate REFUSED for "
                f"position_id={position_id[:8]} role={role}: {exc} — "
                f"another close is already pending; not dispatching."
            )
            return False
        except Exception as exc:
            logger.critical(
                f"spread close substrate insert FAILED for "
                f"position_id={position_id[:8]} role={role}: "
                f"{type(exc).__name__}: {exc} — aborting dispatch to "
                f"avoid restart-gap regression."
            )
            return False

    def _lifecycle_orders_finalize_spread_close(
        self,
        *,
        client_order_id: str,
        broker_order_id: str | None,
        status: str,
        filled_qty: float,
        avg_fill_price: float | None,
    ) -> None:
        """Advance the per-order close row to its terminal status when
        the worker drain reports back.

        Post-submit absorption: failures here log CRITICAL but don't
        re-raise — the broker order is already terminal and crashing
        the drain would prevent the engine from releasing in-memory
        spread state. Cycle reconciliation retries the attach.
        """
        if self.lifecycle_orders_store is None:
            return
        try:
            self.lifecycle_orders_store.mark_terminal_after_dispatch(
                client_order_id=client_order_id,
                broker_order_id=broker_order_id,
                status=status,
                filled_qty=float(filled_qty),
                avg_fill_price=avg_fill_price,
            )
        except Exception as exc:
            logger.critical(
                f"spread close substrate finalize FAILED for "
                f"client_order_id={client_order_id}: "
                f"{type(exc).__name__}: {exc}. Cycle reconciliation "
                f"will retry."
            )

    def _enter_multi_leg(
        self,
        *,
        strategy: BaseStrategy,
        symbol: str,
        underlying_close: float,
        notional_cap: float | None,
        signal_key: tuple,
        signal_bar: "pd.Timestamp",
        strategy_statuses: dict[str, str] | None,
        strategy_reasons: dict[str, list[str]] | None,
    ) -> None:
        """
        Generic MLEG entry path: build the spread plan, dispatch the async
        MLEG combo, and pre-register the multi-leg Position. The combo fill
        confirms asynchronously via ``_drain_spread_fills`` (or rolls the
        pre-registration back on cancel/reject).

        Any strategy that exposes ``build_spread_execution`` is routed here —
        credit spreads today, future iron condors / butterflies / ratio
        spreads tomorrow. The naming is deliberately strategy-agnostic
        (PLAN.md 11.31 generalized the MLEG plumbing; 11.44 renamed this
        entry point to match).
        """
        def _done(status: str) -> None:
            if strategy_statuses is not None:
                strategy_statuses[symbol] = status
            if strategy_reasons is not None:
                strategy_reasons[symbol] = []
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )

        if self.risk.is_halted():
            reason = self.risk.halt_reason() or "global risk halt active"
            logger.info(
                f"[{strategy.name}] {symbol}: MLEG entry blocked — {reason}"
            )
            self.alerts.order_rejection(
                symbol, strategy.name, reason, RejectionCode.HALTED.value
            )
            _done("Risk Blocked")
            return

        if notional_cap is None or notional_cap <= 0:
            logger.info(
                f"[{strategy.name}] {symbol}: credit spread skipped — "
                f"no sleeve notional available"
            )
            _done("No Signal")
            return

        total_open = self._count_open_spreads()
        try:
            plan = strategy.build_spread_execution(
                underlying_close,
                notional_cap=notional_cap,
                total_open_spreads=total_open,
            )
        except MultiLegTradeRejected as e:
            logger.info(f"[{strategy.name}] {symbol}: multi-leg entry rejected — {e}")
            _done("No Signal")
            return
        except Exception as e:
            logger.error(
                f"[{strategy.name}] {symbol}: build_spread_execution failed: {e}"
            )
            _done("No Signal")
            return

        # PLAN 11.44: contract-level conflict guard. The plan resolved every
        # leg OCC; reject before dispatch if any leg collides with a contract
        # already owned by a different strategy. Distinct OCCs on the same
        # underlying are intentionally allowed by the underlying-level skip
        # upstream — only exact-contract overlap (which would aggregate at the
        # broker into one shared position) is blocked here.
        leg_occs = [leg.occ_symbol for leg in plan.legs]
        if self._reject_if_contract_conflict(
            strategy_name=strategy.name,
            symbol=symbol,
            occs=leg_occs,
        ) is not None:
            _done("Contract Conflict")
            return

        if self.risk.is_halted():
            reason = self.risk.halt_reason() or "global risk halt active"
            logger.info(
                f"[{strategy.name}] {symbol}: MLEG entry canceled before "
                f"dispatch — {reason}"
            )
            self.alerts.order_rejection(
                symbol, strategy.name, reason, RejectionCode.HALTED.value
            )
            _done("Risk Blocked")
            return

        position_id = new_spread_id()
        try:
            result = self.broker.dispatch_spread_order(
                legs=plan.legs,
                qty=plan.qty,
                limit_price=plan.limit_price,
                strategy_name=strategy.name,
                position_id=position_id,
            )
        except Exception as e:
            logger.error(f"[{strategy.name}] {symbol}: dispatch_spread_order raised: {e}")
            self.risk.record_broker_error()
            self.alerts.broker_error(f"{symbol} dispatch_spread_order: {e}")
            _done("No Signal")
            return

        if result.status is not OrderStatus.ACCEPTED:
            logger.warning(
                f"[{strategy.name}] {symbol}: spread dispatch returned "
                f"{result.status.value} — not pre-registering"
            )
            _done("No Signal")
            return

        # PLAN 11.10f: lifecycle counter — submitted++ for the
        # credit-spread MLEG path. Equity/options strategies increment
        # submitted after broker.place_order; credit_spread uses
        # dispatch_spread_order so it needs its own increment site
        # (the existing _enter_multi_leg path was previously
        # missing this — PR #21 reviewer caught it). Without this,
        # L3 submitted_per_raw_signal drift was unusable for
        # credit_spread.
        _lc = self._lifecycle_counter_for(strategy.name)
        if _lc is not None:
            _lc.submitted += 1

        # Pre-register the spread Position + the strategy's open-spread view.
        # The combo fill confirms via _drain_spread_fills (or rolls back).
        legs = [
            PositionLeg(symbol=plan.short_occ, qty=-float(plan.qty), side="SELL"),
            PositionLeg(symbol=plan.long_occ, qty=float(plan.qty), side="BUY"),
        ]
        self._positions[position_id] = make_spread(
            strategy_name=strategy.name,
            position_id=position_id,
            legs=legs,
        )
        self._spread_owner_strategy[position_id] = strategy
        self._pending_spread_plans[position_id] = plan
        strategy.register_spread(plan.to_open_spread(position_id=position_id))
        # §10.7 spread lifecycle PR: write the substrate pending row
        # AFTER pre-registration so the drain path's mark_open finds a
        # row to advance. Best-effort — failure here logs and leaves the
        # spread tracked at the engine level so the close path still
        # works; cycle reconciliation will not see a substrate row but
        # the legacy in-memory ownership is preserved.
        self._lifecycle_begin_spread(
            position_id=position_id,
            strategy_name=strategy.name,
            symbol=plan.short_occ,
            qty=plan.qty,
        )
        logger.info(
            f"[{strategy.name}] {symbol}: credit spread dispatched "
            f"{plan.short_occ}/{plan.long_occ} width=${plan.width:.0f} "
            f"net_credit=${plan.net_credit:.2f}/sh max_loss=${plan.max_loss:,.0f} "
            f"position_id={position_id[:8]} — pre-registered"
        )
        _done("Pending Entry")

    def _drain_spread_fills(self) -> None:
        """
        Process async MLEG combo fill events from ``SpreadExecutionWorker``
        threads.

          * open  + FILLED   → finalize the pre-registered spread Position
                               (log to the trade DB, fire the alert).
          * open  + CANCELED → roll the pre-registration back.
          * close + FILLED   → drop the Position, release it on the strategy,
                               log the close to the trade DB.
          * close + CANCELED → leave the Position open; clear the pending-close
                               flag so the exit path re-evaluates next cycle.
        """
        for (
            position_id, strategy_name, closing, status,
            filled_qty, avg_fill_price, order_id, submitted_limit_price,
        ) in self.broker.drain_spread_fills():
            strategy = self._spread_owner_strategy.get(position_id)
            filled = status in ("filled", "partially_filled")

            if not closing:
                # ── Spread OPEN ──────────────────────────────────────────
                plan = self._pending_spread_plans.pop(position_id, None)
                if filled:
                    # PLAN 11.10f: lifecycle counter — filled_entries++
                    # for the credit-spread MLEG path. Multi-leg combo
                    # = 1 entry per design §12.4.1, regardless of leg
                    # count. Paired with the submitted++ in
                    # _enter_multi_leg (after dispatch_spread_order
                    # returned ACCEPTED). PR #21 reviewer fix.
                    _lc = self._lifecycle_counter_for(strategy_name)
                    if _lc is not None:
                        _lc.filled_entries += 1
                    net_credit = (
                        abs(avg_fill_price)
                        if avg_fill_price is not None
                        else (plan.net_credit if plan is not None else 0.0)
                    )
                    logger.info(
                        f"[{strategy_name}] credit spread OPENED — "
                        f"position_id={position_id[:8]} qty={filled_qty} "
                        f"net_credit=${net_credit:.2f}/sh order={order_id}"
                    )
                    if plan is not None:
                        open_qty = float(filled_qty or plan.qty)
                        # §10.7 spread lifecycle PR: transition the
                        # substrate row to 'open' once the combo fill
                        # lands. The pending row was created in
                        # _enter_multi_leg; if it never made it (store
                        # I/O failure), this mark_open is a no-op via
                        # the helper's swallowed exception path.
                        self._lifecycle_mark_spread_open(
                            position_id=position_id,
                            avg_entry_price=(
                                abs(avg_fill_price)
                                if avg_fill_price is not None
                                else (plan.net_credit if plan is not None else 0.0)
                            ),
                            current_qty=open_qty,
                        )
                        # plan.max_loss is $/contract; multiplier of 100 is
                        # already folded in by SpreadExecutionPlan. The
                        # short-leg row stores this so the close path can
                        # recover the R-multiple basis without depending on
                        # `released` being in scope.
                        spread_risk_dollars = (
                            float(plan.max_loss) * open_qty
                            if plan.max_loss > 0 else None
                        )
                        self.trade_logger.log_spread_fill(
                            position_id=position_id,
                            strategy=strategy_name,
                            short_occ=plan.short_occ,
                            long_occ=plan.long_occ,
                            qty=open_qty,
                            net_price=net_credit,
                            order_id=order_id,
                            opening=True,
                            submitted_limit_price=submitted_limit_price,
                            initial_risk_dollars=spread_risk_dollars,
                        )
                        self.alerts.trade_executed(
                            symbol=plan.short_occ,
                            strategy=strategy_name,
                            side="sell",
                            qty=float(filled_qty or plan.qty),
                            price=net_credit,
                            reason=f"{strategy_name} spread entry",
                        )
                else:
                    logger.info(
                        f"[{strategy_name}] credit spread open {status} — "
                        f"position_id={position_id[:8]} rolling back pre-registration"
                    )
                    self._pop_position(position_id)
                    self._spread_owner_strategy.pop(position_id, None)
                    if strategy is not None:
                        strategy.release_spread(position_id)
                    # §10.7 spread lifecycle PR: roll the substrate
                    # pending row back to 'canceled' so the owner-key
                    # lock is released. Mirrors broker's _lifecycle_
                    # mark_canceled on single-leg open rejections.
                    self._lifecycle_mark_spread_canceled(position_id)
                continue

            # ── Spread CLOSE ─────────────────────────────────────────────
            self._spreads_pending_close.discard(position_id)
            # §10.7: find the substrate close row this drain event
            # corresponds to. The pre-dispatch insert puts exactly one
            # non-terminal close row per position_uid (guaranteed by
            # uniq_one_active_close_per_position). On legacy / test
            # paths where the substrate isn't wired, this is None and
            # the substrate-finalize calls below become no-ops.
            substrate_close_row = None
            if self.lifecycle_orders_store is not None:
                try:
                    rows = self.lifecycle_orders_store.get_non_terminal_by_role(
                        spread_substrate_uid(position_id),
                        ("exit", "partial_close"),
                    )
                    if rows:
                        substrate_close_row = rows[0]
                except Exception as exc:
                    logger.warning(
                        f"spread close substrate lookup failed for "
                        f"position_id={position_id[:8]}: {exc}"
                    )
            if filled:
                # PR #56 R5: peek at the open spread BEFORE releasing so
                # we can detect a partial close (close_qty < released.qty)
                # and refuse to corrupt state. Alpaca documents MLEG
                # combos as atomic per-leg, but quantity-wise a 2-contract
                # close fill of 1 contract is structurally possible.
                # Previously the engine released the entire position
                # unconditionally → residual at the broker, orphaned at
                # the strategy.
                peeked: OpenSpread | None = (
                    strategy.get_open_spread(position_id)
                    if strategy is not None
                    and hasattr(strategy, "get_open_spread")
                    else None
                )
                close_qty = float(
                    filled_qty or (peeked.qty if peeked is not None else 1)
                )
                full_close_combo: bool = (
                    peeked is None or close_qty >= peeked.qty
                )
                if not full_close_combo:
                    # Defensive: don't release/pop/log. Fire CRITICAL so the
                    # operator reconciles manually. The position stays open
                    # at the strategy/engine level; the residual stream event
                    # (when the rest fills) will land here again with
                    # full_close_combo=True and proceed normally.
                    #
                    # PR #56 R6: re-add to _spreads_pending_close so the
                    # next cycle's _process_credit_spread_exits skips this
                    # position and does NOT dispatch a duplicate close
                    # order at the original full qty. The position remains
                    # "pending close" until the broker reconciles the
                    # residual fill (or the operator intervenes).
                    # Line 4924's unconditional `discard` cleared the
                    # pending state at the top of the close branch; this
                    # re-arms it.
                    #
                    # KNOWN RESIDUAL RISK (PLAN.md P2 follow-up):
                    # _spreads_pending_close is in-memory only. A bot
                    # restart between this partial detection and the
                    # residual fill loses the marker — restart restores
                    # the spread as open with residual qty (R5), this set
                    # starts empty, and the next cycle may dispatch a
                    # duplicate close. The CRITICAL alert below is the
                    # current mitigation: operator reconciliation closes
                    # the gap within minutes (the typical restart window).
                    # See PLAN.md "MLEG partial-close residual
                    # reconciliation" for the design space.
                    self._spreads_pending_close.add(position_id)
                    # §10.7: substrate-backed R6 — finalize the
                    # original exit row at broker truth ('filled' with
                    # the partial qty), then insert a NEW pending
                    # partial_close row so the position remains
                    # locked against duplicate dispatch across
                    # restarts. The unique index would block this
                    # insert if the original row hadn't been
                    # transitioned to terminal first.
                    if substrate_close_row is not None:
                        self._lifecycle_orders_finalize_spread_close(
                            client_order_id=substrate_close_row.client_order_id,
                            broker_order_id=order_id,
                            status="filled",
                            filled_qty=close_qty,
                            avg_fill_price=avg_fill_price,
                        )
                        residual_cloid = (
                            self._new_spread_close_client_order_id(
                                position_id=position_id,
                                role="partial_close",
                            )
                        )
                        # Residual qty = open_qty - close_qty.
                        residual_qty = float(peeked.qty - close_qty)
                        self._lifecycle_orders_insert_spread_close(
                            position_id=position_id,
                            role="partial_close",
                            client_order_id=residual_cloid,
                            qty=residual_qty,
                            intended_limit_price=None,
                        )
                    logger.critical(
                        f"[{strategy_name}] credit spread PARTIAL close detected — "
                        f"position_id={position_id[:8]} close_qty={close_qty} < "
                        f"open_qty={peeked.qty} — state NOT released; "
                        f"position remains pending close (no duplicate dispatch); "
                        f"awaiting residual fill or operator reconciliation."
                    )
                    try:
                        self.alerts.broker_error(
                            f"credit_spread partial close: "
                            f"position_id={position_id[:8]} "
                            f"close_qty={close_qty}/{peeked.qty} "
                            f"(state preserved, awaiting residual)"
                        )
                    except Exception:
                        pass
                    # Still log the partial event so the trade-log is
                    # honest about what happened and the dollar math
                    # reflects the partial P&L.
                    if peeked is not None and avg_fill_price is not None:
                        partial_net_debit = abs(avg_fill_price)
                        partial_pnl = (
                            (peeked.net_credit - partial_net_debit)
                            * close_qty * 100.0
                        )
                        if self._allocator is not None:
                            self._allocator.record_realized_pnl(
                                strategy_name, partial_pnl,
                                position_uid=position_id,
                                is_full_close=False,
                            )
                        try:
                            self.trade_logger.log_spread_fill(
                                position_id=position_id,
                                strategy=strategy_name,
                                short_occ=peeked.short_occ,
                                long_occ=peeked.long_occ,
                                qty=close_qty,
                                net_price=partial_net_debit,
                                order_id=order_id,
                                opening=False,
                                realized_pnl=partial_pnl,
                                reason="spread exit (partial)",
                                is_full_close=False,
                            )
                        except Exception as exc:
                            logger.error(
                                f"[{strategy_name}] partial-close trade-log "
                                f"write failed: {exc}"
                            )
                    continue
                released = (
                    strategy.release_spread(position_id)
                    if strategy is not None else None
                )
                self._pop_position(position_id)
                self._spread_owner_strategy.pop(position_id, None)
                # §10.7: full close — finalize the substrate row at
                # 'filled' so the position is unlocked.
                if substrate_close_row is not None:
                    self._lifecycle_orders_finalize_spread_close(
                        client_order_id=substrate_close_row.client_order_id,
                        broker_order_id=order_id,
                        status="filled",
                        filled_qty=close_qty,
                        avg_fill_price=avg_fill_price,
                    )
                # §10.7: also transition the position_lifecycle row
                # to closed so the parent shows the right rollup.
                if self.lifecycle_store is not None:
                    try:
                        self.lifecycle_store.mark_closed(
                            position_uid=spread_substrate_uid(position_id),
                        )
                    except Exception as exc:
                        logger.warning(
                            f"spread lifecycle.mark_closed skipped for "
                            f"position_id={position_id[:8]}: {exc}"
                        )
                short_occ = released.short_occ if released is not None else position_id
                long_occ = released.long_occ if released is not None else ""

                # The spread IS closed regardless — but only record P&L when
                # we have a real fill price. A stream "filled" event whose
                # REST follow-up failed reaches here with avg_fill_price=None;
                # treating that as a $0 debit would fabricate a full-credit
                # winner and inflate the allocator's HWM / drawdown gate. In
                # that case leave realized P&L unset (not zero) and warn.
                realized_pnl: float | None = None
                exit_reason = "spread exit"
                # PR #56 R1+R2+R4: determine full/partial close status ONCE,
                # outside both the price-availability branches AND the
                # allocator/log paths, so a single value drives the trade-log
                # row's status column and the allocator's is_full_close. Only
                # depends on released and close_qty — independent of whether
                # the fill price was available. Spreads are treated as fully
                # closed when the close fill quantity matches the open qty.
                full_close = (
                    released is not None
                    and close_qty >= released.qty
                )
                if avg_fill_price is None:
                    net_debit = 0.0
                    exit_reason = "spread exit (fill price unavailable)"
                    logger.warning(
                        f"[{strategy_name}] credit spread CLOSED but the combo "
                        f"fill price was unavailable — position_id="
                        f"{position_id[:8]} order={order_id}; realized P&L not "
                        "recorded (position still released)"
                    )
                else:
                    net_debit = abs(avg_fill_price)
                    if released is not None:
                        # Realized P&L = (credit collected − debit paid) × qty
                        # × 100. Feed the allocator HWM / sleeve-drawdown gate
                        # and persist it on the close row so it survives a
                        # restart via read_strategy_realized_pnl_summary.
                        realized_pnl = (
                            (released.net_credit - net_debit) * close_qty * 100.0
                        )
                        if self._allocator is not None:
                            self._allocator.record_realized_pnl(
                                strategy_name, realized_pnl,
                                position_uid=position_id,
                                is_full_close=full_close,
                            )
                    logger.info(
                        f"[{strategy_name}] credit spread CLOSED — "
                        f"position_id={position_id[:8]} "
                        f"net_debit=${net_debit:.2f}/sh realized_pnl="
                        f"{'n/a' if realized_pnl is None else f'${realized_pnl:+,.2f}'} "
                        f"order={order_id}"
                    )
                # `released` (OpenSpread) carries width and net_credit from
                # the original entry. When present, recompute the max-loss
                # basis the EdgeAssessor uses for R-multiple. When absent
                # (restart edge case), the logger falls back to the open
                # row's stored basis.
                close_risk_dollars: float | None = None
                if released is not None:
                    spread_max_loss = (
                        (released.width - released.net_credit) * 100.0
                    )
                    if spread_max_loss > 0:
                        close_risk_dollars = spread_max_loss * close_qty
                # PR #56 R4: pass is_full_close so the trade-log row's
                # status column matches what the live allocator saw
                # (full_close computed above against released.qty).
                # Without this, restart restoration via R3's
                # status='filled' gate would mis-count a partial close
                # as a completed round trip.
                self.trade_logger.log_spread_fill(
                    position_id=position_id,
                    strategy=strategy_name,
                    short_occ=short_occ,
                    long_occ=long_occ,
                    qty=close_qty,
                    net_price=net_debit,
                    order_id=order_id,
                    opening=False,
                    realized_pnl=realized_pnl,
                    reason=exit_reason,
                    is_full_close=full_close,
                    submitted_limit_price=submitted_limit_price,
                    initial_risk_dollars=close_risk_dollars,
                )
                self.alerts.trade_executed(
                    symbol=short_occ,
                    strategy=strategy_name,
                    side="buy",
                    qty=close_qty,
                    price=net_debit,
                    reason=f"{strategy_name} {exit_reason}",
                )
            else:
                # Close did not fill — the position stays open. Clearing the
                # pending-close flag (above) lets the exit path retry next cycle.
                logger.warning(
                    f"[{strategy_name}] credit spread close {status} — "
                    f"position_id={position_id[:8]} still open, will retry"
                )
                # §10.7: release the substrate close row so the
                # uniq_one_active_close_per_position guard doesn't
                # block the next-cycle retry. Mirrors the in-memory
                # _spreads_pending_close.discard above.
                if substrate_close_row is not None:
                    self._lifecycle_orders_finalize_spread_close(
                        client_order_id=substrate_close_row.client_order_id,
                        broker_order_id=order_id,
                        status="canceled" if status == "canceled" else "rejected",
                        filled_qty=float(filled_qty or 0.0),
                        avg_fill_price=avg_fill_price,
                    )

    def _sweep_bear_spread_exits(self) -> None:
        """Cycle-level defensive close of every open spread when the regime
        is BEAR.

        Runs once per cycle, before ``_process_symbol``, so the override is
        not gated by per-symbol data paths (bar fetch failure, stale-data
        rejection, empty decision frame). The BEAR short-circuit in
        ``_process_credit_spread_exits`` already skips the quote lookup and
        ``evaluate_spread_exit`` — ``underlying_close`` is therefore unused
        on this path and a sentinel ``0.0`` is passed.

        Idempotent: positions already in ``_spreads_pending_close`` (from a
        prior cycle or another caller in the same cycle) are skipped, so any
        subsequent per-symbol invocation in the slot loop is a no-op.
        """
        for slot in self.slots:
            strategy = slot.strategy
            if not hasattr(strategy, "evaluate_spread_exit"):
                continue
            open_spreads = getattr(strategy, "open_spreads", None)
            if not open_spreads:
                continue
            underlying = getattr(getattr(strategy, "config", None), "symbol", "?")
            try:
                self._process_credit_spread_exits(
                    strategy=strategy,
                    underlying=underlying,
                    underlying_close=0.0,
                    current_regime=MarketRegime.BEAR,
                )
            except Exception as e:
                logger.error(
                    f"[{strategy.name}] BEAR sweep failed for {underlying}: {e}"
                )

    def _mleg_should_bypass_walk(self, *, now: datetime) -> bool:
        """
        True iff the time remaining in the regular session is below
        ``settings.MLEG_END_OF_SESSION_BYPASS_SECONDS``.

        When True, the close-dispatch path substitutes a market-only
        profile so the position closes autonomously before the bell —
        Alpaca's mleg orders are day-TIF only, and an unfilled walk
        active at 15:59 EDT would not get its remaining steps in.

        Outside regular trading hours, returns False — the engine's
        ``market_hours_only`` config already gates whether the close
        cycle runs at all.
        """
        # NYSE close is 16:00 America/New_York. Use zoneinfo (3.9+).
        try:
            from zoneinfo import ZoneInfo
        except ImportError:  # pragma: no cover — Python <3.9 not supported
            return False
        eastern = ZoneInfo("America/New_York")
        now_et = now.astimezone(eastern)
        # If outside the session entirely, no bypass (the engine shouldn't
        # be dispatching closes during the pre/after hours window — but be
        # defensive).
        close_today = now_et.replace(
            hour=16, minute=0, second=0, microsecond=0,
        )
        if now_et >= close_today:
            return False
        # Only the regular session matters; if it's before 09:30 ET we are
        # not in a session and the threshold doesn't apply.
        open_today = now_et.replace(
            hour=9, minute=30, second=0, microsecond=0,
        )
        if now_et < open_today:
            return False
        seconds_left = (close_today - now_et).total_seconds()
        return seconds_left < settings.MLEG_END_OF_SESSION_BYPASS_SECONDS

    def _process_credit_spread_exits(
        self,
        *,
        strategy: BaseStrategy,
        underlying: str,
        underlying_close: float,
        current_regime: MarketRegime | None = None,
    ) -> None:
        """
        Evaluate exit triggers for every open spread this strategy holds and
        dispatch a closing MLEG combo for any that fire. A position with a
        close already in flight (``_spreads_pending_close``) is skipped so a
        stale signal cannot double-submit.

        When ``current_regime`` is ``BEAR`` the engine short-circuits to a
        defensive close for every open spread (docs/credit_spread_strategy.md
        — "Regime exit | Regime shifts to BEAR mid-trade | Defensive
        override"). The override deliberately skips the strategy's quote-driven
        ``evaluate_spread_exit`` so a quote outage cannot suppress the
        defensive exit; ``limit_price`` falls back to the spread width, the
        same marketable debit used elsewhere when no mid is available.
        """
        open_spreads = getattr(strategy, "open_spreads", [])
        if not open_spreads:
            return
        bear_override = current_regime is MarketRegime.BEAR
        today = self._clock().date()
        for open_spread in list(open_spreads):
            position_id = open_spread.position_id
            if position_id in self._spreads_pending_close:
                continue
            # Build the typed close decision. BEAR override produces a
            # synthetic decision; otherwise the strategy decides.
            if bear_override:
                decision_reason = "defensive_breach"
                decision_detail = "regime shift to BEAR — defensive override"
                decision_should_close = True
                # Quote-less defensive close — the scheduler will be
                # short-circuited to a market-only profile below.
                decision_mid = float("nan")
            elif hasattr(strategy, "evaluate_close"):
                try:
                    decision = strategy.evaluate_close(
                        open_spread,
                        underlying_close=underlying_close,
                        today=today,
                    )
                except Exception as e:
                    logger.error(
                        f"[{strategy.name}] {underlying}: evaluate_close failed "
                        f"for {position_id[:8]}: {e}"
                    )
                    continue
                decision_reason = decision.reason
                decision_detail = decision.detail
                decision_should_close = decision.should_close
                decision_mid = decision.initial_mid
            else:
                # Backward-compat: strategy implements only the legacy
                # ``evaluate_spread_exit`` shape. Map to the typed reason set
                # by parsing the legacy detail string. This path is
                # intentionally lossy and exists only so older test fixtures
                # / future MLEG strategies that haven't migrated still work.
                try:
                    legacy_exit, legacy_reason, legacy_mid = (
                        strategy.evaluate_spread_exit(
                            open_spread,
                            underlying_close=underlying_close,
                            today=today,
                        )
                    )
                except Exception as e:
                    logger.error(
                        f"[{strategy.name}] {underlying}: evaluate_spread_exit failed "
                        f"for {position_id[:8]}: {e}"
                    )
                    continue
                decision_should_close = legacy_exit
                decision_detail = legacy_reason
                decision_mid = legacy_mid if legacy_mid is not None else float("nan")
                # Best-effort string → typed mapping.
                _detail_lc = legacy_reason.lower()
                if "profit" in _detail_lc:
                    decision_reason = "profit_target"
                elif "stop" in _detail_lc:
                    decision_reason = "stop_loss"
                elif "time" in _detail_lc:
                    decision_reason = "time_stop"
                else:
                    decision_reason = "defensive_breach"

            if not decision_should_close:
                continue

            # Resolve the close profile from settings (instrument override
            # → per-strategy override → global default). The BEAR
            # short-circuit and the EOS bypass both substitute a
            # market-only profile to guarantee an autonomous exit.
            instrument_overrides = None
            instrument_cfg = getattr(strategy.config, "_instrument_overrides", None) \
                if hasattr(strategy, "config") else None
            if isinstance(instrument_cfg, dict):
                instrument_overrides = instrument_cfg
            try:
                resolved_profile = resolve_mleg_close_profile(
                    reason=decision_reason or "stop_loss",
                    strategy_name=strategy.name,
                    instrument_overrides=instrument_overrides,
                )
            except KeyError as e:
                logger.error(
                    f"[{strategy.name}] {underlying}: profile resolution failed "
                    f"for {position_id[:8]}: {e} — using market-only fallback"
                )
                resolved_profile = [("market", 0)]

            # End-of-session bypass: if there's not enough time left in the
            # regular session for the full walk + safety buffer, skip the
            # walk and submit market directly.
            eos_bypass = self._mleg_should_bypass_walk(now=self._clock())
            if bear_override or eos_bypass:
                effective_profile = [("market", 0)]
            else:
                effective_profile = list(resolved_profile)

            try:
                scheduler = MlegCloseScheduler(
                    effective_profile,
                    reason=decision_reason or "stop_loss",
                    position_id=position_id,
                )
            except Exception as e:
                logger.error(
                    f"[{strategy.name}] {underlying}: scheduler construction "
                    f"failed for {position_id[:8]}: {e} — falling back to market"
                )
                scheduler = MlegCloseScheduler(
                    [("market", 0)],
                    reason=decision_reason or "stop_loss",
                    position_id=position_id,
                )

            # Quote provider for the walk steps. BEAR override + EOS bypass
            # use a market-only profile so a quote isn't required, but we
            # still pass a provider (it returns None safely if needed).
            if hasattr(strategy, "build_close_quote_provider"):
                try:
                    quote_provider = strategy.build_close_quote_provider(open_spread)
                except Exception as e:
                    logger.warning(
                        f"[{strategy.name}] {underlying}: build_close_quote_provider "
                        f"raised: {e} — using market-only profile"
                    )
                    scheduler = MlegCloseScheduler(
                        [("market", 0)],
                        reason=decision_reason or "stop_loss",
                        position_id=position_id,
                    )
                    quote_provider = lambda: None  # noqa: E731
            else:
                # Strategy doesn't expose a quote provider — degrade to market.
                scheduler = MlegCloseScheduler(
                    [("market", 0)],
                    reason=decision_reason or "stop_loss",
                    position_id=position_id,
                )
                quote_provider = lambda: None  # noqa: E731

            # limit_price is the *initial* submitted hint; the actual prices
            # used during the walk come from the scheduler. We pass the
            # decision's initial_mid (or width as fallback) for telemetry
            # consistency with the existing dispatch contract.
            debit = (
                round(decision_mid, 2)
                if (decision_mid == decision_mid and decision_mid > 0)  # NaN check
                else round(open_spread.width, 2)
            )
            legs = [
                SpreadLeg(occ_symbol=open_spread.short_occ, side=Side.SELL, opening=True),
                SpreadLeg(occ_symbol=open_spread.long_occ, side=Side.BUY, opening=True),
            ]
            walk_mode = "market-only" if effective_profile == [("market", 0)] else "walk-and-market"
            logger.info(
                f"[{strategy.name}] {underlying}: closing spread "
                f"{open_spread.short_occ}/{open_spread.long_occ} "
                f"position_id={position_id[:8]} — {decision_detail} "
                f"(reason={decision_reason}, mode={walk_mode}, debit-hint ${debit:.2f})"
            )
            # FYI-only alert at walk start — operator sees it via Telegram
            # post-fact; does not block any decisions.
            try:
                self.alerts.mleg_close_walk_started(
                    strategy_name=strategy.name,
                    underlying=underlying,
                    position_id=position_id,
                    reason=decision_reason or "unknown",
                    mode=walk_mode,
                    initial_mid=decision_mid,
                )
            except Exception:
                pass  # alert failures must never block the close

            # Closure capturing the close context for per-step telemetry.
            # Each step the worker executes calls this back with the
            # resolved price, status, and timings — we log structured
            # records that future analysis (per the review trigger in
            # docs/credit_spread_strategy.md) can grep from bot.jsonl.
            _close_strategy = strategy.name
            _close_underlying = underlying
            _close_position_id = position_id
            _close_reason = decision_reason
            _alerts = self.alerts
            def _on_walk_step(
                *,
                step_number: int,
                total_steps: int,
                price_expr: str,
                is_market: bool,
                limit_price: float,
                duration_seconds: int,
                terminal_status: str,
            ) -> None:
                logger.bind(
                    event="mleg_walk_step",
                    strategy=_close_strategy,
                    underlying=_close_underlying,
                    position_id=_close_position_id,
                    reason=_close_reason,
                    step_number=step_number,
                    total_steps=total_steps,
                    price_expr=price_expr,
                    is_market=is_market,
                    limit_price=None if is_market else limit_price,
                    duration_seconds=duration_seconds,
                    terminal_status=terminal_status,
                ).info(
                    f"[{_close_strategy}] {_close_underlying}: walk step "
                    f"{step_number}/{total_steps} {terminal_status} "
                    f"(expr={price_expr!r}, market={is_market})"
                )
                # FYI alert when we hit the market fallback step.
                if is_market and terminal_status in ("filled", "rejected"):
                    try:
                        _alerts.mleg_close_market_fallback(
                            strategy_name=_close_strategy,
                            underlying=_close_underlying,
                            position_id=_close_position_id,
                            reason=_close_reason or "unknown",
                            terminal_status=terminal_status,
                        )
                    except Exception:
                        pass

            # §10.7: substrate-backed duplicate-dispatch block. Insert
            # the pending close row BEFORE dispatch — the
            # `uniq_one_active_close_per_position` partial unique index
            # is the durable analog of `_spreads_pending_close`. If it
            # refuses (already a non-terminal close on this position),
            # skip dispatch entirely. Survives restart because the row
            # is committed to the DB.
            close_cloid = self._new_spread_close_client_order_id(
                position_id=position_id, role="exit",
            )
            substrate_inserted = self._lifecycle_orders_insert_spread_close(
                position_id=position_id,
                role="exit",
                client_order_id=close_cloid,
                qty=float(open_spread.qty),
                intended_limit_price=float(debit),
            )
            if not substrate_inserted:
                # Either the duplicate-dispatch guard fired (already a
                # non-terminal close pending) or the substrate insert
                # failed critically. Either way, don't dispatch. Also
                # add to the legacy in-memory set so any reads still on
                # it during the C5 migration window see consistent
                # state.
                self._spreads_pending_close.add(position_id)
                continue
            try:
                result = self.broker.dispatch_spread_order(
                    legs=legs,
                    qty=open_spread.qty,
                    limit_price=debit,
                    strategy_name=strategy.name,
                    position_id=position_id,
                    closing=True,
                    close_scheduler=scheduler,
                    quote_provider=quote_provider,
                    on_walk_step=_on_walk_step,
                )
            except Exception as e:
                logger.error(
                    f"[{strategy.name}] {underlying}: spread close dispatch raised: {e}"
                )
                self.risk.record_broker_error()
                self.alerts.broker_error(f"{underlying} spread close: {e}")
                # Roll the substrate row back so the position isn't
                # locked out of a future close attempt.
                self._lifecycle_orders_finalize_spread_close(
                    client_order_id=close_cloid,
                    broker_order_id=None,
                    status="canceled",
                    filled_qty=0.0,
                    avg_fill_price=None,
                )
                continue
            if result.status is OrderStatus.ACCEPTED:
                self._spreads_pending_close.add(position_id)
            else:
                logger.warning(
                    f"[{strategy.name}] {underlying}: spread close dispatch returned "
                    f"{result.status.value} for {position_id[:8]}"
                )
                # Non-ACCEPTED dispatch — release the substrate row.
                self._lifecycle_orders_finalize_spread_close(
                    client_order_id=close_cloid,
                    broker_order_id=None,
                    status="rejected",
                    filled_qty=0.0,
                    avg_fill_price=None,
                )

    def _restore_ownership_from_db(self, snapshot: BrokerSnapshot) -> set[str]:
        """
        Restore ``_positions`` from the trade DB (10.C1).

        For each symbol in the broker's open positions:
        - If the trade DB records a still-open buy for that symbol, use the
          logged strategy as the authoritative owner.
        - If the logged strategy is no longer in any configured slot, log a
          WARNING and mark the symbol as a conflict.
        - If the DB has no record (new account or DB gap), fall back to
          best-effort slot-order match with a WARNING.

        OCC option symbols (e.g. SPY260516C00520000) are keyed under their
        underlying ticker ("SPY") in _positions — owner_key_for() handles the
        normalization, matching how the engine tracks options during normal
        operation via _get_position_for().

        Returns the set of conflict underlying keys (DB owner no longer in any slot).
        """
        db_owners = self.trade_logger.read_all_open_owners()
        known_strategy_names = {slot.strategy.name for slot in self.slots}
        conflicts: set[str] = set()

        # Multi-leg credit spreads first: reconstruct the full two-leg
        # Position (and the owning strategy's OpenSpread view) from the trade
        # DB, and collect the leg OCC symbols so the single-leg loop below
        # skips them. Without this, a spread leg would fall through to the
        # best-effort slot match and get mis-assigned as a standalone
        # position — for SPY, potentially to the spy_options_reversion slot,
        # which could then close one leg and leave a naked short put.
        spread_leg_occs = self._restore_spread_positions(snapshot, conflicts)

        for sym in snapshot.account.open_positions:
            if sym in spread_leg_occs:
                continue  # leg of a reconstructed spread — already handled

            # For OCC option symbols, ownership is stored under the underlying.
            owner_key = owner_key_for(sym)
            is_option = owner_key != sym

            if owner_key in self._positions:
                continue  # already assigned (shouldn't happen at startup)

            # DB lookup: try the exact broker symbol first (OCC string or equity),
            # then fall back to the underlying ticker for options.
            db_owner = db_owners.get(sym)
            if db_owner is None and is_option:
                db_owner = db_owners.get(owner_key)

            if db_owner is not None:
                if db_owner in known_strategy_names:
                    self._register_single_leg(strategy_name=db_owner, symbol=sym)
                    logger.info(
                        f"restart: assigned existing position {sym} "
                        f"→ '{db_owner}' (owner_key='{owner_key}', trade DB record)"
                    )
                else:
                    logger.warning(
                        f"restart: open position {sym} was opened by strategy "
                        f"'{db_owner}' which is no longer configured — "
                        "position will not be managed. Close it manually."
                    )
                    conflicts.add(owner_key)
            else:
                # No DB record — fall back to best-effort slot-order match.
                # For options, match the underlying ticker against slot symbols.
                lookup = owner_key if is_option else sym
                matched = False
                for slot in self.slots:
                    if lookup in slot.active_symbols():
                        self._register_single_leg(
                            strategy_name=slot.strategy.name,
                            symbol=sym,
                        )
                        logger.warning(
                            f"restart: no DB record for {sym}; assigned to "
                            f"'{slot.strategy.name}' via underlying '{lookup}' "
                            "(best-effort slot match)"
                        )
                        matched = True
                        break
                if not matched:
                    logger.warning(
                        f"restart: open position {sym} (owner_key='{owner_key}') "
                        "does not belong to any configured slot — it will NOT be "
                        "managed by this engine. Close it manually or add it to a "
                        "strategy's symbol universe."
                    )

        return conflicts

    def _spread_strategy_for(
        self, underlying: str, *, strategy_name: str | None = None
    ) -> BaseStrategy | None:
        """The configured spread strategy that owns ``underlying``, if any.

        Generalized from the original credit-spread-only lookup (PLAN.md
        11.31). A slot qualifies when its strategy exposes the spread
        dispatch hook (``build_spread_execution``) and lists ``underlying``
        in its active symbols. When ``strategy_name`` is supplied (e.g. on
        restart, where the DB row records which strategy owned the spread),
        the match is narrowed to that exact strategy so a future second
        spread strategy on the same underlying cannot accidentally claim
        positions it did not open.
        """
        for slot in self.slots:
            strategy = slot.strategy
            if not hasattr(strategy, "build_spread_execution"):
                continue
            if strategy_name is not None and strategy.name != strategy_name:
                continue
            if underlying in slot.active_symbols():
                return strategy
        return None

    def _restore_spread_positions(
        self, snapshot: BrokerSnapshot, conflicts: set[str]
    ) -> set[str]:
        """
        Rebuild open multi-leg credit-spread Positions from the trade DB
        (11.29 PR 3b).

        For each open spread in the trade log:
          - both leg OCCs must be present in the broker snapshot, and a
            spread strategy instance must be configured for the underlying —
            otherwise the spread cannot be safely managed and its underlying
            is added to ``conflicts`` (→ RESTRICTED startup mode);
          - on success the two-leg ``Position``, ``_spread_owner_strategy``
            entry, and the strategy's open-spread view are all rebuilt.

        Returns the set of leg OCC symbols belonging to reconstructed spreads
        so the single-leg restore loop skips them.
        """
        spread_leg_occs: set[str] = set()
        for record in self.trade_logger.read_open_spread_positions():
            position_id = record["position_id"]
            leg_symbols = record["leg_symbols"]
            strategy_name = record["strategy"]

            # Parse both legs; a bull put spread's short leg is the higher strike.
            try:
                parsed = sorted(
                    ((occ, parse_occ_symbol(occ)) for occ in leg_symbols),
                    key=lambda pair: pair[1].strike,
                )
            except ValueError as e:
                logger.warning(
                    f"restart: spread {position_id[:8]} has an unparseable leg "
                    f"({leg_symbols}) — {e}; left unmanaged"
                )
                continue
            long_occ, long_leg = parsed[0]
            short_occ, short_leg = parsed[1]
            underlying = short_leg.root

            # Both legs must still be open at the broker.
            broker_positions = snapshot.account.open_positions
            missing = [occ for occ in leg_symbols if occ not in broker_positions]
            if missing:
                logger.warning(
                    f"restart: spread {position_id[:8]} ({underlying}) is open in "
                    f"the trade DB but leg(s) {missing} are absent from the broker "
                    "— declaring a conflict (RESTRICTED)"
                )
                conflicts.add(underlying)
                spread_leg_occs.update(leg_symbols)
                continue

            strategy = self._spread_strategy_for(
                underlying, strategy_name=strategy_name
            )
            if strategy is None:
                logger.warning(
                    f"restart: spread {position_id[:8]} ({underlying}) has no "
                    f"configured spread strategy '{strategy_name}' — declaring a "
                    "conflict (RESTRICTED). Close it manually or restore the slot."
                )
                conflicts.add(underlying)
                spread_leg_occs.update(leg_symbols)
                continue

            qty = int(record.get("qty") or 1)
            net_credit = float(record.get("net_credit") or 0.0)
            width = short_leg.strike - long_leg.strike
            build_record = getattr(strategy, "build_open_spread_record", None)
            if not callable(build_record):
                logger.warning(
                    f"restart: spread {position_id[:8]} ({underlying}) strategy "
                    f"'{strategy_name}' cannot rebuild an open spread record — "
                    "declaring a conflict (RESTRICTED)."
                )
                conflicts.add(underlying)
                spread_leg_occs.update(leg_symbols)
                continue
            self._positions[position_id] = make_spread(
                strategy_name=strategy_name,
                position_id=position_id,
                legs=[
                    PositionLeg(symbol=short_occ, qty=-float(qty), side="SELL"),
                    PositionLeg(symbol=long_occ, qty=float(qty), side="BUY"),
                ],
            )
            self._spread_owner_strategy[position_id] = strategy
            strategy.register_spread(build_record(
                position_id=position_id,
                short_occ=short_occ,
                long_occ=long_occ,
                short_strike=short_leg.strike,
                long_strike=long_leg.strike,
                expiration_date=short_leg.expiration,
                net_credit=net_credit,
                width=width,
                qty=qty,
            ))
            spread_leg_occs.update(leg_symbols)
            # §10.7 spread lifecycle PR: backfill the substrate row for
            # spreads that pre-date this PR. Idempotent — if a row
            # already exists under owner_key=position_id, the helper
            # returns the existing uid and does nothing. Synthesized
            # rows carry metadata.synthesized=true so dashboards /
            # operator CLI can distinguish backfills from bot-originated
            # rows.
            if self.lifecycle_store is not None:
                try:
                    self.lifecycle_store.synthesize_for_existing(
                        symbol=short_occ,
                        owner_key=position_id,
                        strategy=strategy_name,
                        position_type="spread",
                        current_qty=float(qty),
                        avg_entry_price=net_credit,
                        position_uid=spread_substrate_uid(position_id),
                        backfill_note=(
                            "synthesized at startup from reconstructed spread"
                        ),
                    )
                except Exception as exc:
                    logger.warning(
                        f"spread lifecycle backfill failed for "
                        f"position_id={position_id[:8]}: {exc}"
                    )
            logger.info(
                f"restart: reconstructed spread {position_id[:8]} ({underlying}) "
                f"{short_occ}/{long_occ} width=${width:.0f} "
                f"net_credit=${net_credit:.2f}/sh → '{strategy_name}'"
            )
        return spread_leg_occs

    def _restore_runtime_state_from_db(self, snapshot: BrokerSnapshot) -> None:
        """Restore allocator P&L/HWM state and open-position entry prices from the trade log."""
        self._restore_allocator_pnl_from_db()
        self._restore_entry_prices_from_db(snapshot)

    def _restore_allocator_pnl_from_db(self) -> None:
        """Rehydrate allocator cumulative realized P&L / HWM from the trade log."""
        if self._allocator is None:
            return
        summary = self.trade_logger.read_strategy_realized_pnl_summary(
            self._allocator.strategies()
        )
        self._allocator.restore_pnl_summary(summary)

    def _restore_entry_prices_from_db(self, snapshot: BrokerSnapshot) -> None:
        """Restore actual fill bases for currently-open broker positions."""
        for sym in snapshot.account.open_positions:
            owner_key = owner_key_for(sym)
            occ_m = _OCC_PAT.match(sym)
            owner = self._get_owner(sym)
            if owner is None:
                continue
            context = self.trade_logger.read_latest_open_entry_context(
                symbol=sym,
                strategy=owner,
            )
            if context is None and occ_m:
                context = self.trade_logger.read_latest_open_entry_context(
                    symbol=owner_key,
                    strategy=owner,
                )
            if context is None:
                continue
            entry_price = float(context.get("entry_fill_price") or 0.0)
            if entry_price <= 0.0:
                continue
            self._entry_prices[owner_key] = entry_price
            logger.info(
                f"restart: restored entry price for {sym} "
                f"(owner_key='{owner_key}') at ${entry_price:.2f}"
            )

    def _reconcile_startup(
        self, snapshot: BrokerSnapshot, conflict_symbols: set[str]
    ) -> str:
        """
        Determine the startup mode based on broker state and ownership conflicts.

        NORMAL     — no anomalies; full trading.
        RESTRICTED — anomalies found (conflicts or unresolvable positions);
                     entries blocked for one cycle, then auto-clears to NORMAL.
        HALT       — reserved for manual kill-switch; this method never sets it.

        Returns the mode string.
        """
        if conflict_symbols:
            logger.warning(
                f"startup: RESTRICTED mode — ownership conflicts for "
                f"{sorted(conflict_symbols)}. No new entries this cycle."
            )
            return "RESTRICTED"

        # Check for broker positions with no ownership at all.
        # OCC option positions are owned under their underlying ticker, so we
        # must resolve the owner key before checking _positions.
        managed_spread_legs = {
            leg.symbol
            for position in self._positions.values()
            if position.is_spread
            for leg in position.legs
        }
        unmanaged = [
            sym
            for sym in snapshot.account.open_positions
            if owner_key_for(sym) not in self._positions
            and sym not in managed_spread_legs
        ]
        if unmanaged:
            logger.warning(
                f"startup: RESTRICTED mode — unmanaged broker positions: "
                f"{sorted(unmanaged)}. No new entries this cycle."
            )
            return "RESTRICTED"

        logger.info("startup: NORMAL mode — ownership verified")
        return "NORMAL"

    # ── Position lifecycle reconciliation (Operator Controls Phase A) ──

    def _reconcile_position_lifecycle(self, snapshot: "BrokerSnapshot") -> None:
        """Make the `position_lifecycle` table consistent with broker reality.

        Two passes, both idempotent and best-effort (any DB error is
        logged and swallowed so the cycle is never affected):

        1. **Forward (backfill)**: for each broker-open equity position
           with no matching open lifecycle row, synthesize a row in
           `open` status. Lets the operator CLI see and act on
           positions that existed before this code shipped (or were
           opened in a window where lifecycle persistence failed).

        2. **Reverse (close-reconcile)**: for each open lifecycle row
           whose `owner_key` is no longer present in broker positions,
           mark the row `external_closed`. Catches overnight stop
           fills, manual broker-side closes, and any other path that
           closed a position without the bot's normal close flow.

        Multi-leg spread lifecycle integration remains deferred. Managed
        single-leg OCC options are included here because option trailing
        stops require a durable ``position_uid`` before their DAY broker
        stops can be recreated on startup.
        """
        if self.lifecycle_store is None:
            return
        try:
            # Forward pass — synthesize for any managed broker
            # position that lacks an open lifecycle row.
            #
            # snapshot.account.open_positions maps str -> risk.manager.Position
            # (a frozen dataclass with .qty / .avg_entry_price). Reading
            # the dataclass fields directly is mandatory — calling
            # float(position) raises TypeError and (because the outer
            # except is broad) silently skipped every backfill in the
            # PR-1 first cut.
            managed_spread_legs = {
                leg.symbol
                for tracked in self._positions.values()
                if tracked.is_spread
                for leg in tracked.legs
            }
            for sym, position in snapshot.account.open_positions.items():
                owner = owner_key_for(sym)
                existing = self.lifecycle_store.get_open_for_owner_key(owner)
                if existing is not None:
                    continue
                pos = self._positions.get(owner)
                is_occ = bool(_OCC_PAT.match(sym))
                if is_occ:
                    if sym in managed_spread_legs:
                        continue
                    if (
                        pos is None
                        or not pos.is_single_leg
                        or pos.primary_leg is None
                        or pos.primary_leg.symbol != sym
                    ):
                        continue
                strategy = pos.strategy_name if pos is not None else "unknown"
                qty_val = float(getattr(position, "qty", 0.0) or 0.0)
                avg_val = getattr(position, "avg_entry_price", None)
                avg_val = float(avg_val) if avg_val else None
                legs = ()
                if is_occ:
                    from engine.lifecycle import PositionLifecycleLeg
                    legs = (
                        PositionLifecycleLeg(
                            position_uid="pending",
                            symbol=sym,
                            side="BUY",
                            qty=qty_val,
                            avg_entry_price=avg_val,
                        ),
                    )
                self.lifecycle_store.synthesize_for_existing(
                    symbol=sym,
                    owner_key=owner,
                    strategy=strategy,
                    position_type="single_leg",
                    current_qty=qty_val,
                    avg_entry_price=avg_val,
                    legs=legs,
                    backfill_note=(
                        "synthesized at startup from broker OCC option state"
                        if is_occ
                        else "synthesized at startup from broker state"
                    ),
                )
        except Exception as exc:
            logger.warning(f"lifecycle backfill failed: {exc}")

        try:
            # Reverse pass — close any lifecycle row whose owner_key is
            # no longer at the broker.
            #
            # PR-2 gap 2 fix: pending rows younger than
            # LIFECYCLE_PENDING_GRACE_SECONDS are skipped — they may
            # represent an in-flight entry whose broker confirmation
            # hasn't landed yet. Without this grace, a bot restart
            # mid-submit would mass-close legitimate pending rows as
            # external_closed before _lifecycle_mark_filled gets a
            # chance to transition them.
            broker_owners = {
                owner_key_for(s) for s in snapshot.account.open_positions
            }
            grace_seconds = int(settings.LIFECYCLE_PENDING_GRACE_SECONDS)
            now_utc = datetime.now(timezone.utc)
            for row in self.lifecycle_store.get_open():
                if row.position_type != "single_leg":
                    continue
                if row.owner_key in broker_owners:
                    continue
                if row.status == "pending":
                    try:
                        created = datetime.fromisoformat(row.created_at)
                    except (ValueError, TypeError):
                        created = None
                    if (
                        created is not None
                        and (now_utc - created).total_seconds() < grace_seconds
                    ):
                        logger.debug(
                            f"lifecycle: skipping pending row "
                            f"{row.position_uid[:18]}… ({row.owner_key}) "
                            f"within {grace_seconds}s grace window"
                        )
                        continue
                self.lifecycle_store.mark_closed(
                    position_uid=row.position_uid,
                    external=True,
                )
                logger.info(
                    f"lifecycle: marked {row.position_uid[:18]}… "
                    f"({row.owner_key}) external_closed — no longer "
                    f"at broker"
                )
        except Exception as exc:
            logger.warning(f"lifecycle close-reconcile failed: {exc}")

    # ── State snapshot (Phase 11.14) ─────────────────────────────────────

    def _observe_stream_health(self) -> None:
        """Log/alert websocket outage and recovery transitions once per change."""
        if self._stream_manager is None:
            return

        health = self._stream_manager.health_snapshot()
        if self._last_stream_healthy is None:
            self._last_stream_healthy = health.healthy
            return

        if health.healthy == self._last_stream_healthy:
            return

        self._last_stream_healthy = health.healthy
        if not health.healthy:
            msg = (
                "stream unhealthy — websocket disconnected; REST/order "
                "reconciliation fallbacks remain active"
            )
            logger.warning(msg)
            self.alerts.broker_error(msg)
            return

        msg = (
            f"stream healthy again (generation={health.generation}, "
            f"reconnected_at={health.last_reconnect_at})"
        )
        logger.info(msg)
        self.alerts.broker_info(msg)

    # ── PLAN 11.10f: Strategy Health lifecycle counters ───────────
    # All counter operations are observability-only and gated by
    # settings.HEALTH_COUNTERS_ENABLED. Per design §12.4.1 hard rule:
    # counter writes MUST NEVER raise into the trading loop — every
    # call site here is wrapped in try/except → logger.warning. The
    # feature flag is the additional belt-and-suspenders revert path.

    def _lifecycle_counter_for(self, strategy_name: str):
        """Return the LifecycleCounters accumulator for `strategy_name`
        in the current cycle, lazy-creating on first access. Returns
        None when the feature flag is off — caller should test for
        None before incrementing."""
        if not settings.HEALTH_COUNTERS_ENABLED:
            return None
        from strategies.health.lifecycle import LifecycleCounters
        if strategy_name not in self._cycle_lifecycle_counters:
            self._cycle_lifecycle_counters[strategy_name] = LifecycleCounters()
        return self._cycle_lifecycle_counters[strategy_name]

    def _flush_lifecycle_counters(self) -> None:
        """Flush the per-cycle accumulator to the
        strategy_lifecycle_counters table via single upsert per
        strategy. ON CONFLICT DO UPDATE accumulates into the existing
        weekly row — see strategies/health/lifecycle.upsert_counters.

        Period bucket = ISO Monday → next Monday (consistent week
        alignment). Wrapped in try/except — counter write failure
        must NEVER raise into the trading loop (design §12.4.1).
        Per-cycle flush is one DB write per strategy, NOT 7 per
        symbol — that's the §12.4.1 batching requirement.
        """
        if not settings.HEALTH_COUNTERS_ENABLED:
            return
        if not self._cycle_lifecycle_counters:
            return
        try:
            from datetime import timedelta as _td
            from strategies.health.lifecycle import upsert_counters
            # Use the engine's injected clock so tests can pin the
            # date deterministically; production passes the default
            # `datetime.now(UTC)` lambda which gives wall-clock time.
            today = self._clock().date()
            # ISO Monday of the week containing `today` (weekday() returns 0 for Mon).
            week_start = today - _td(days=today.weekday())
            week_end = week_start + _td(days=7)
            conn = self.trade_logger._ensure_db()
            for strategy_name, counters in self._cycle_lifecycle_counters.items():
                upsert_counters(
                    conn,
                    period_type="weekly",
                    period_start=week_start,
                    period_end=week_end,
                    strategy_name=strategy_name,
                    counters=counters,
                )
        except Exception as exc:
            # Observability only — never let counter I/O affect trading.
            logger.warning(
                f"lifecycle counter flush failed (observability only, "
                f"trading not affected): {exc}"
            )

    def _risk_controls_snapshot(self, equity: float) -> dict:
        """Read-only snapshot of existing risk-control state for the
        HealthAssessor L1 layer (PLAN 11.10f).

        Calls the new accessors on RiskManager + SleeveAllocator
        (added in this PR). Fail-safe: every field defaults to empty/
        None if the underlying object doesn't expose the accessor —
        keeps the snapshot writeable even with custom risk/allocator
        subclasses that don't yet implement these methods.
        """
        out: dict = {
            "is_halted": False,
            "halt_reason": None,
            "entries_paused": False,
            "entries_paused_reason": None,
            "paused_strategies": {},
            "cooldown_state": {},
            "sleeve_dd_state": {},
        }
        try:
            out["is_halted"] = bool(self.risk.is_halted())
            out["halt_reason"] = self.risk.halt_reason()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"risk halt accessor failed: {exc}")
        # Phase B — soft entry pauses, fail-safe defaults if RiskManager
        # is a custom subclass that doesn't yet expose these accessors.
        try:
            if hasattr(self.risk, "is_entries_paused"):
                out["entries_paused"] = bool(self.risk.is_entries_paused())
                out["entries_paused_reason"] = self.risk.entries_paused_reason()
            if hasattr(self.risk, "paused_strategies_snapshot"):
                out["paused_strategies"] = self.risk.paused_strategies_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"soft pause accessor failed: {exc}")
        try:
            if hasattr(self.risk, "cooldown_snapshot"):
                out["cooldown_state"] = self.risk.cooldown_snapshot()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"cooldown_snapshot failed: {exc}")
        try:
            if (
                self._allocator is not None
                and hasattr(self._allocator, "drawdown_snapshot")
            ):
                out["sleeve_dd_state"] = self._allocator.drawdown_snapshot(equity)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"drawdown_snapshot failed: {exc}")
        return out

    def _write_state_snapshot(self) -> None:
        """
        Write a JSON snapshot of engine state to STATE_SNAPSHOT_PATH.

        Written atomically (tmp → replace) so the dashboard never reads a
        partial file. Errors are swallowed — a failed snapshot must not
        affect the trading loop.
        """
        try:
            path = settings.STATE_SNAPSHOT_PATH
            equity = self._last_cycle_equity or self._session_start_equity or 0.0
            start_equity = self._session_start_equity or equity
            previous_close_equity = (
                self._last_snapshot.account.previous_close_equity
                if self._last_snapshot is not None else None
            )
            market_day_pnl = (
                equity - previous_close_equity
                if previous_close_equity is not None else equity - start_equity
            )
            # Build enriched position map with entry price and unrealized P&L.
            positions_detail: dict[str, dict] = {}
            broker_positions = (
                self._last_snapshot.account.open_positions
                if self._last_snapshot else {}
            )
            broker_positions_by_owner = {
                owner_key_for(sym): pos for sym, pos in broker_positions.items()
            }
            for sym, strat in self._owners_view().items():
                pos = broker_positions.get(sym) or broker_positions_by_owner.get(sym)
                unrealized_pnl = None
                cost_basis = None
                current_price = None
                if pos:
                    unrealized_pnl = getattr(pos, "unrealized_pl", None)
                    if unrealized_pnl is None:
                        unrealized_pnl = getattr(pos, "unrealized_pnl", None)
                    cost_basis = getattr(pos, "cost_basis", None)
                    current_price = getattr(pos, "current_price", None)
                    if unrealized_pnl is None:
                        unrealized_pnl = pos.market_value - pos.qty * pos.avg_entry_price
                positions_detail[sym] = {
                    "strategy": strat,
                    "qty": pos.qty if pos else None,
                    "avg_entry_price": pos.avg_entry_price if pos else None,
                    "current_price": current_price,
                    "market_value": pos.market_value if pos else None,
                    "cost_basis": cost_basis,
                    "unrealized_pnl": unrealized_pnl,
                }

            allocator_snapshot: dict[str, dict] = {"strategies": {}, "pools": {}}
            sleeve_usage: dict[str, float] = {}
            pending_entry_notional: dict[str, dict] = {
                "strategies": {},
                "pools": {},
            }
            if self._allocator is not None and self._last_snapshot is not None:
                order_strategy = self._attribute_orders(self._last_snapshot.open_orders)
                allocator_snapshot = self._allocator.snapshot(
                    self._last_snapshot.account,
                    self._last_snapshot.open_orders,
                    self._owners_view(),
                    order_strategy,
                    additional_used_notional=self._multi_leg_risk_notional_by_strategy(),
                )
                sleeve_usage = {
                    name: detail["used"]
                    for name, detail in allocator_snapshot["strategies"].items()
                }
                pending_entry_notional = {
                    "strategies": {
                        name: detail["pending_entry_notional"]
                        for name, detail in allocator_snapshot["strategies"].items()
                    },
                    "pools": {
                        name: detail["pending_entry_notional"]
                        for name, detail in allocator_snapshot["pools"].items()
                    },
                }
            # Sector exposure snapshot (11.7 Part B). Pure observability —
            # log INFO when composition changes since the prior cycle so the
            # operator can spot tilt drift in real time.
            sector_exposure = self._compute_sector_exposure()
            exposure_counts = {k: len(v) for k, v in sector_exposure.items()}
            if exposure_counts != self._last_sector_exposure:
                if exposure_counts:
                    summary = ", ".join(
                        f"{k}={v}" for k, v in sorted(exposure_counts.items())
                    )
                    logger.info(f"sector exposure changed: {{{summary}}}")
                else:
                    logger.info("sector exposure changed: (empty)")
                self._last_sector_exposure = dict(exposure_counts)

            state = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "running": self._running,
                "cycle_count": self._cycle_count,
                "regime": self._last_regime,
                "stream_health": (
                    None if self._stream_manager is None else {
                        "connected": (health := self._stream_manager.health_snapshot()).connected,
                        "healthy": health.healthy,
                        "generation": health.generation,
                        "last_rx_at": (
                            health.last_rx_at.isoformat()
                            if health.last_rx_at is not None else None
                        ),
                        "last_disconnect_at": (
                            health.last_disconnect_at.isoformat()
                            if health.last_disconnect_at is not None else None
                        ),
                        "last_reconnect_at": (
                            health.last_reconnect_at.isoformat()
                            if health.last_reconnect_at is not None else None
                        ),
                        "consecutive_failures": health.consecutive_failures,
                    }
                ),
                "equity": equity,
                "session_start_equity": start_equity,
                "previous_close_equity": previous_close_equity,
                "daily_pnl": market_day_pnl,
                "session_pnl": equity - start_equity,
                "open_positions": self._owners_view(),
                "positions_detail": positions_detail,
                "credit_spreads": self._credit_spreads_snapshot(),
                "multi_leg_positions": self._multi_leg_positions_snapshot(),
                # PLAN 11.44: rolling 24h cross-strategy conflict counts for
                # HealthAssessor L1. ``symbol_conflicts_24h`` covers the
                # equity underlying-level rule (always was the documented
                # field); ``contract_conflicts_24h`` is the new leg-level
                # rule that catches two options strategies landing on the
                # same exact OCC.
                "symbol_conflicts_24h": self._prune_window(
                    self._symbol_conflicts, window=timedelta(hours=24)
                ),
                "contract_conflicts_24h": self._prune_window(
                    self._contract_conflicts, window=timedelta(hours=24)
                ),
                "allocator": allocator_snapshot["strategies"],
                "capital_pools": allocator_snapshot["pools"],
                "pending_entry_notional": pending_entry_notional,
                "sleeve_usage": sleeve_usage,
                "watchlist_statuses": self._watchlist_statuses,
                "watchlist_reasons": self._watchlist_reasons,
                "sector_heat": self._sector_heat,
                "sector_exposure": sector_exposure,
                "live_trading": settings.LIVE_TRADING,
                # PLAN 11.10f: surface existing risk-control state for
                # HealthAssessor L1 checks. ALL FIELDS ARE READ-ONLY
                # SNAPSHOTS of state that already exists — no behavior
                # change to RiskManager or SleeveAllocator. The fields
                # consumed by HealthAssessor are documented in
                # strategies/health/assessor.py:_l1_checks.
                "risk_controls": self._risk_controls_snapshot(equity),
            }
            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w") as fh:
                json.dump(state, fh, indent=2)
            os.replace(tmp, path)
        except Exception as exc:
            logger.debug(f"_write_state_snapshot failed: {exc}")

    def _baseline_watchlist_status(
        self,
        symbol: str,
        snapshot: BrokerSnapshot,
        *,
        strategy_name: str,
        order_strategy: dict[str, str],
    ) -> str:
        """Best-known status before the current cycle's decision path runs."""
        position = snapshot.account.open_positions.get(symbol)
        owner = self._get_owner(symbol)
        if position is not None and owner == strategy_name:
            if self._has_pending_close_order(symbol, snapshot):
                return "Pending Exit"
            return "Long"

        for order in snapshot.open_orders:
            if (
                order.symbol == symbol
                and order.side is Side.BUY
                and order_strategy.get(order.order_id) == strategy_name
            ):
                return "Pending Entry"

        return "No Signal"

    def _refresh_watchlist_statuses(
        self,
        snapshot: BrokerSnapshot,
        *,
        order_strategy: dict[str, str],
        preserve_existing: bool,
    ) -> None:
        """Refresh dashboard watchlist statuses from broker truth and prior state."""
        previous = self._watchlist_statuses if preserve_existing else {}
        previous_reasons = self._watchlist_reasons if preserve_existing else {}
        refreshed: dict[str, dict[str, str]] = {}
        refreshed_reasons: dict[str, dict[str, list[str]]] = {}
        for slot in self.slots:
            strat_name = slot.strategy.name
            strat_previous = previous.get(strat_name, {})
            strat_previous_reasons = previous_reasons.get(strat_name, {})
            strat_statuses: dict[str, str] = {}
            strat_reasons: dict[str, list[str]] = {}
            for symbol in slot.active_symbols():
                baseline = self._baseline_watchlist_status(
                    symbol,
                    snapshot,
                    strategy_name=strat_name,
                    order_strategy=order_strategy,
                )
                prior = strat_previous.get(symbol)
                prior_reasons = strat_previous_reasons.get(symbol, [])
                if (
                    preserve_existing
                    and baseline == "No Signal"
                    and prior in {"Regime Blocked", "Filter Blocked"}
                ):
                    strat_statuses[symbol] = prior
                    strat_reasons[symbol] = list(prior_reasons)
                else:
                    strat_statuses[symbol] = baseline
                    strat_reasons[symbol] = []
            refreshed[strat_name] = strat_statuses
            refreshed_reasons[strat_name] = strat_reasons
        self._watchlist_statuses = refreshed
        self._watchlist_reasons = refreshed_reasons

    # ── Market hours ─────────────────────────────────────────────────────

    def _market_open(self) -> bool:
        """
        Ask Alpaca whether the regular session is open. Network failure
        falls back to "closed" — better to skip a cycle than to trade in
        the dark on a clock-API blip.
        """
        try:
            clock = self.broker._with_retry(
                self.broker._api.get_clock, op_desc="get_clock"
            )
            return bool(clock.is_open)
        except Exception as e:
            logger.warning(f"get_clock failed ({e}); treating market as closed")
            return False

    # ── Sleep / signals / shutdown ───────────────────────────────────────

    def _sleep(self, seconds: float) -> None:
        """
        Sleep responsive to `stop()`: wake every second to re-check the
        running flag so SIGINT doesn't have to wait out a 5-minute cycle.
        """
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    def _install_signal_handlers(self) -> None:
        def _handle(signum, _frame):
            logger.warning(f"received signal {signum}, shutting down")
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handle)
            signal.signal(signal.SIGTERM, _handle)
        except ValueError:
            # signal.signal can only be called from the main thread; in
            # tests we may be on a worker thread. Safe to ignore — tests
            # drive shutdown via stop() directly.
            pass

    def _shutdown(self) -> None:
        logger.info(f"engine stopped after {self._cycle_count} cycle(s)")
        # PR-65 review F2: covers max_cycles / exception / restart-loop
        # paths that exit the start() while-loop without calling
        # `stop()`. _stop_operator_heartbeat is idempotent so explicit
        # stop() callers see no double-stop side effects.
        self._running = False
        self._stop_operator_heartbeat()
        if self.config.cancel_orders_on_shutdown:
            try:
                for o in self.broker.get_open_orders():
                    logger.info(f"shutdown: canceling order {o.order_id} ({o.symbol})")
                    self.broker.cancel_order(o.order_id)
            except Exception as e:
                logger.error(f"shutdown cleanup failed: {e}")
        if self._stream_manager is not None:
            self._stream_manager.stop()
        # main: option-stop audit cleanup (PRs #63/#64).
        self._drain_option_stop_audit_events()
        if self.option_stop_audit_store is not None:
            try:
                self.option_stop_audit_store.close()
            except Exception as exc:
                logger.warning(f"option-stop audit close failed: {exc}")
        # PR-65 review F3: close the operator-queue's dedicated SQLite
        # connection. Cycle-thread connections (TradeLogger / lifecycle
        # store) are closed elsewhere.
        op_conn = getattr(self, "_operator_command_conn", None)
        if op_conn is not None:
            try:
                op_conn.close()
            except Exception as exc:
                logger.debug(f"operator queue connection close failed: {exc}")
            self._operator_command_conn = None
        # PR-65 R2 (N): drop the store reference too so a leaked
        # post-shutdown caller (e.g. a TelegramCommandListener that
        # races shutdown) sees the same None as a never-initialised
        # store, instead of operating on a closed connection.
        self.operator_command_store = None


# ── CLI entry point ──────────────────────────────────────────────────────────


def main() -> None:
    """`python -m engine.trader` — run the engine with the default config."""
    from reporting.logger import install_json_sink
    from strategies.sma_crossover import SMACrossover

    install_json_sink()
    slot = StrategySlot(
        strategy=SMACrossover(fast=20, slow=50),
        symbols=list(settings.WATCHLIST),
    )
    risk = RiskManager()
    broker = AlpacaBroker()
    engine = TradingEngine(slots=[slot], risk=risk, broker=broker)
    engine.start()


if __name__ == "__main__":
    main()
