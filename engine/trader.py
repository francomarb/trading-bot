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
import os
import re
import signal
import time
from dataclasses import dataclass, field
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
    view_owner_map,
)
from execution.entry_guard import CapAction, gate_entry
from execution.broker import (
    AlpacaBroker,
    BrokerSnapshot,
    OrderResult,
    OrderStatus,
)
from execution.options_executor import SpreadLeg
from indicators.technicals import add_atr
from risk.manager import (
    AccountState,
    Position,
    RiskDecision,
    RiskManager,
    RiskRejection,
    Side,
    Signal,
)
from reporting.alerts import AlertDispatcher
from reporting.logger import TradeLogger
from reporting.pnl import PnLTracker
from strategies.base import BaseStrategy, OrderType, StrategySlot
from strategies.credit_spread import CreditSpreadRejected, OpenSpread
from strategies.spy_options_reversion import OptionTradeRejected
from utils.option_symbols import parse_occ_symbol

from regime.detector import MarketRegime

if TYPE_CHECKING:
    from execution.stream import StreamManager
    from regime.detector import RegimeDetector
    from risk.allocator import SleeveAllocator
    from sector.resolver import SectorResolver


# Matches any OCC option symbol: underlying (1–6 letters) + YYMMDD + C/P + 8-digit strike.
_OCC_PAT = re.compile(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$")


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


@dataclass(frozen=True)
class SuspectOrder:
    """Bot-submitted order accepted by Alpaca but not yet locally confirmed."""

    decision: RiskDecision
    order_id: str
    modeled_price: float


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
        self.trade_logger = trade_logger or TradeLogger()
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

        # Exact bot-submitted orders that were accepted by Alpaca but whose
        # fill state could not be confirmed locally (e.g. stream/REST failure
        # after submit). These are the only unknown positions we will ever try
        # to adopt automatically.
        self._suspect_orders: dict[str, SuspectOrder] = {}

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

        self._sync_managed_stop_legs(startup_snapshot)
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

            risk_state = self.risk.halt_reason() or "healthy"
            logger.info(
                f"cycle {cycle_id} broker state: "
                f"positions={len(snapshot.account.open_positions)}, "
                f"open_orders={len(snapshot.open_orders)}, "
                f"risk={risk_state}"
            )

            self._sync_managed_stop_legs(snapshot)
            self._observe_stream_health()
            self._recover_suspect_orders(snapshot)
            self._process_stream_stop_fills(snapshot)
            self._detect_external_closes(snapshot)
            self._drain_option_fills()
            self._drain_spread_fills()
            self._repair_missing_protective_stops(snapshot)

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
            df, stats = fetch_symbol(symbol, start, end, timeframe=timeframe)
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
            if hasattr(strategy, "evaluate_spread_exit"):
                self._process_credit_spread_exits(
                    strategy=strategy,
                    underlying=symbol,
                    underlying_close=latest_close,
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
            try:
                result = self.broker.close_position(position.symbol)
                # close_position always uses MARKET (hard-risk exit).
                # Skip slippage recording for options: latest_close is the SPY
                # bar price, not the option premium — the comparison is meaningless
                # and would inject thousands of spurious bps into the drift monitor.
                if not _OCC_PAT.match(position.symbol):
                    self._record_fill(result, modeled_price=latest_close, order_type="market")
                # For options, modeled_price is the actual fill premium; using
                # the underlying bar close (~$520) here would corrupt the audit trail.
                _close_modeled = (
                    result.avg_fill_price or 0.0
                    if _OCC_PAT.match(position.symbol)
                    else latest_close
                )
                self._log_close(result, _close_modeled, strategy.name)
                if result.status in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
                    if strategy_statuses is not None:
                        strategy_statuses[symbol] = "No Signal"
                    if strategy_reasons is not None:
                        strategy_reasons[symbol] = []
                    close_price = result.avg_fill_price or latest_close
                    close_qty = float(result.filled_qty or (position.qty if position else 0))
                    self.alerts.trade_executed(
                        symbol=symbol,
                        strategy=strategy.name,
                        side="sell",
                        qty=close_qty,
                        price=close_price,
                        reason="exit signal",
                    )
                    # Feed realized P&L into the HWM drawdown gate.
                    # Options: multiply by 100 (each contract = 100 shares).
                    _pnl_mult = 100 if _OCC_PAT.match(position.symbol) else 1
                    self._record_realized_pnl(symbol, strategy.name, close_price, close_qty, multiplier=_pnl_mult)
                # Release ownership and cached entry price.
                self._pop_position(symbol)
                self._entry_prices.pop(symbol, None)
                self._mark_signal_bar_processed(
                    signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
                )
            except Exception as e:
                logger.error(f"{symbol}: close_position failed: {e}")
                self.risk.record_broker_error()
                self.alerts.broker_error(f"{symbol} close_position: {e}")
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

        # Shared-symbol conflict (11.7 Part A) — another strategy already owns
        # this symbol via ownership pre-registration (async options) or a
        # confirmed entry that has not yet appeared in the broker snapshot.
        # Two strategies cannot hold the same position simultaneously: the
        # second strategy's fill would overwrite the first's ownership record
        # and leave one position unmanaged. Same-cycle ties are resolved by
        # allocator priority via _slots_by_priority — the higher-priority slot
        # pre-registers first, so by the time the lower-priority slot reaches
        # this check the owner is already set. Exits never reach this code path.
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
        # combo — it bypasses RiskManager.evaluate (the spread's defined-risk
        # structure, capped by the sleeve notional, IS the risk control) but
        # all the engine-level guards above (halt, daily-loss, broker-error,
        # regime gate, sleeve) have already been applied.
        if hasattr(strategy, "build_spread_execution"):
            self._enter_credit_spread(
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

        sig = Signal(
            symbol=target_symbol,
            side=Side.BUY,
            strategy_name=strategy.name,
            reference_price=target_price,
            atr=latest_atr,
            reason=f"{strategy.name} entry @ {latest_ts.isoformat()}",
            order_type=strategy.preferred_order_type,
            limit_price=target_price if strategy.preferred_order_type is OrderType.LIMIT else None,
            take_profit_price=take_profit,
            stop_price_override=stop_price,
            entry_max_price=entry_max_price,
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

        try:
            result = self.broker.place_order(decision)
            # PLAN 11.10f: lifecycle counter — submitted increments
            # once per place_order call (regardless of fill status).
            # ACCEPTED, FILLED, PARTIAL, UNKNOWN all count as submitted
            # — the order reached the broker.
            _lc = self._lifecycle_counter_for(strategy.name)
            if _lc is not None:
                _lc.submitted += 1
            if result.status is OrderStatus.UNKNOWN:
                self._remember_suspect_order(
                    decision, result, modeled_price=latest_close
                )
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
                modeled_price=latest_close,
                order_type=decision.order_type.value,
            )
            self._log_entry(decision, result, latest_close)
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

    def _remember_suspect_order(
        self,
        decision: RiskDecision,
        result: OrderResult,
        *,
        modeled_price: float,
    ) -> None:
        """
        Persist a narrow recovery handle for submit-succeeded/confirm-failed
        entries. Recovery is tied to the exact order_id returned by Alpaca.
        """
        if result.order_id is None:
            msg = (
                f"{decision.symbol}: confirmation failed but no order_id was "
                "returned; cannot stage suspect-order recovery"
            )
            logger.error(msg)
            self.risk.record_broker_error()
            self.alerts.broker_error(msg)
            return

        self._suspect_orders[decision.symbol] = SuspectOrder(
            decision=decision,
            order_id=result.order_id,
            modeled_price=modeled_price,
        )
        logger.warning(
            f"{decision.symbol}: staged suspect order recovery for "
            f"{result.order_id} [{decision.strategy_name}]"
        )

    def _recover_suspect_orders(self, snapshot: BrokerSnapshot) -> None:
        """Recover only exact bot-submitted orders that lost confirmation."""
        for symbol, suspect in list(self._suspect_orders.items()):
            try:
                result = self.broker.reconcile_submitted_order(
                    order_id=suspect.order_id,
                    symbol=symbol,
                    requested_qty=suspect.decision.qty,
                )
            except Exception as e:
                logger.warning(
                    f"{symbol}: suspect order {suspect.order_id} reconciliation "
                    f"failed: {e}"
                )
                continue

            if result.status in {OrderStatus.PENDING, OrderStatus.ACCEPTED}:
                logger.warning(
                    f"{symbol}: suspect order {suspect.order_id} still "
                    f"{result.status.value}; waiting for next cycle"
                )
                continue

            if result.status in {OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.TIMEOUT}:
                logger.warning(
                    f"{symbol}: suspect order {suspect.order_id} resolved as "
                    f"{result.status.value}; dropping recovery state"
                )
                self._suspect_orders.pop(symbol, None)
                continue

            position = snapshot.account.open_positions.get(symbol)
            if result.status in {OrderStatus.FILLED, OrderStatus.PARTIAL} and position is not None:
                fill_price = result.avg_fill_price or suspect.decision.entry_reference_price
                fill_qty = float(result.filled_qty or position.qty or suspect.decision.qty)
                self._record_fill(
                    result,
                    modeled_price=suspect.modeled_price,
                    order_type=suspect.decision.order_type.value,
                )
                self._log_entry(suspect.decision, result, suspect.modeled_price)
                self._register_single_leg(
                    strategy_name=suspect.decision.strategy_name,
                    symbol=symbol,
                )
                self._entry_prices[symbol] = fill_price
                self._ensure_recovered_protective_stop(
                    snapshot=snapshot,
                    position=position,
                    decision=suspect.decision,
                )
                self.alerts.trade_executed(
                    symbol=symbol,
                    strategy=suspect.decision.strategy_name,
                    side="buy",
                    qty=fill_qty,
                    price=fill_price,
                    reason=f"{suspect.decision.reason} (recovered)",
                )
                logger.warning(
                    f"{symbol}: recovered filled suspect order "
                    f"{suspect.order_id}; adopted position for "
                    f"'{suspect.decision.strategy_name}'"
                )
                self._suspect_orders.pop(symbol, None)
                continue

            logger.warning(
                f"{symbol}: suspect order {suspect.order_id} resolved as "
                f"{result.status.value} but no broker position was present; "
                "dropping recovery state without adopting"
            )
            self._suspect_orders.pop(symbol, None)

    def _ensure_recovered_protective_stop(
        self,
        *,
        snapshot: BrokerSnapshot,
        position: Position,
        decision: RiskDecision,
    ) -> None:
        """Place the intended stop immediately for a recovered entry if missing."""
        symbol = decision.symbol
        if self._has_protective_stop_order(symbol, snapshot):
            return

        stop_qty = abs(int(position.qty))
        if stop_qty < 1:
            logger.warning(
                f"{symbol}: recovered position qty={position.qty} has no "
                "whole-share stop quantity; fractional remainder will rely on "
                "strategy exits until reduced or closed"
            )
            return

        repaired = self.broker.place_protective_stop(
            symbol=symbol,
            qty=stop_qty,
            stop_price=decision.stop_price,
            client_order_id_prefix=f"{decision.strategy_name}-recover-stop",
        )
        snapshot.open_orders.append(repaired)
        logger.warning(
            f"{symbol}: restored protective stop immediately after suspect "
            f"order recovery at ${decision.stop_price:.2f}"
        )

    # ── Post-fill bookkeeping ────────────────────────────────────────────

    def _record_fill(
        self,
        result: OrderResult,
        *,
        modeled_price: float,
        order_type: str = "market",
    ) -> None:
        """
        Feed the realized vs. modeled slippage into the Phase 6 drift kill
        switch. Modeled fill = the bar close we acted on; realized fill =
        what Alpaca actually gave us.

        Modeled slippage uses SLIPPAGE_MODEL_MARKET_BPS for MARKET orders
        (matches the backtest default of 5 bps) and 0 bps for LIMIT orders
        (the fill price is controlled by the limit).
        """
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            return
        if result.avg_fill_price is None or modeled_price <= 0:
            return
        modeled_bps = (
            0.0 if order_type == "limit" else SLIPPAGE_MODEL_MARKET_BPS
        )
        realized_bps = (
            abs(result.avg_fill_price - modeled_price) / modeled_price * 10_000
        )
        self.risk.record_fill_slippage(
            modeled_bps=modeled_bps, realized_bps=realized_bps
        )

    def _log_entry(
        self,
        decision: RiskDecision,
        result: OrderResult,
        modeled_price: float,
    ) -> None:
        """Log an entry fill to the trade database."""
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            return
        try:
            record = self.trade_logger.build_record(
                decision, result, modeled_price=modeled_price
            )
            self.trade_logger.log(record)
        except Exception as e:
            logger.error(f"trade logging failed: {e}")

    def _log_close(
        self,
        result: OrderResult,
        modeled_price: float,
        strategy_name: str = "",
    ) -> None:
        """Log an exit fill to the trade database."""
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            return
        try:
            record = self.trade_logger.build_close_record(
                result,
                strategy_name=strategy_name or self.strategy.name,
                modeled_price=modeled_price,
            )
            self.trade_logger.log(record)
        except Exception as e:
            logger.error(f"trade logging (close) failed: {e}")

    def _record_realized_pnl(
        self,
        symbol: str,
        strategy_name: str,
        close_price: float,
        qty: float,
        multiplier: int = 1,
    ) -> None:
        """
        Compute and report realized P&L for a closed position to the
        SleeveAllocator's HWM drawdown gate.

        Called from all three close paths:
          - Signal-based exit (_process_symbol exit branch)
          - WebSocket stop-leg fill (_process_stream_stop_fills)
          - External close detection (_detect_external_closes) — approximate

        Pass multiplier=100 for options contracts (each contract = 100 shares).
        Equity callers omit it and get the default of 1.

        Startup restores entry prices for still-open positions from the trade log,
        so normal restart/reconcile flows continue feeding the HWM gate. If the
        entry price is still unavailable, the update is conservatively skipped.
        """
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
        self._allocator.record_realized_pnl(strategy_name, realized_pnl)

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
        result = self.broker.close_position(position.symbol)
        close_price = float(
            result.avg_fill_price
            or getattr(position, "current_price", 0.0)
            or getattr(position, "avg_entry_price", 0.0)
            or 0.0
        )
        self._log_close(result, close_price, owner)
        if result.status in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            close_qty = float(result.filled_qty or position.qty or 0.0)
            self.alerts.trade_executed(
                symbol=symbol,
                strategy=owner,
                side="sell",
                qty=close_qty,
                price=close_price,
                reason="fractional residual cleanup",
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
        """True if there's already an open SELL order for this symbol."""
        return any(
            TradingEngine._is_matching_symbol(symbol, o.symbol) and o.side is Side.SELL
            for o in snapshot.open_orders
        )

    @staticmethod
    def _has_protective_stop_order(symbol: str, snapshot: BrokerSnapshot) -> bool:
        """True if there's already an open SELL stop order for this symbol."""
        return any(
            TradingEngine._is_matching_symbol(symbol, o.symbol) and o.side is Side.SELL and o.stop_price is not None
            for o in snapshot.open_orders
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

    def _record_recovered_stop_fill(
        self,
        *,
        symbol: str,
        owner: str,
        stop_fill,
    ) -> bool:
        """Persist a broker-recovered stop fill and feed realized P&L once."""
        price = stop_fill.avg_fill_price
        qty = float(stop_fill.filled_qty or 0.0)
        raw_symbol = getattr(stop_fill, "symbol", None) or symbol
        if price is None or qty <= 0:
            self.trade_logger.log_external_close(
                symbol=raw_symbol,
                strategy=owner,
                reason="stop_triggered",
            )
            return False

        self.trade_logger.log_stop_fill(
            symbol=raw_symbol,
            strategy=owner,
            qty=qty,
            avg_fill_price=price,
            order_id=stop_fill.order_id,
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
        return True

    def _repair_missing_protective_stops(self, snapshot: BrokerSnapshot) -> None:
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
            if self._has_protective_stop_order(symbol, snapshot):
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

            stop_qty = abs(int(position.qty))
            if stop_qty < 1:
                self._close_fractional_residual_position(
                    snapshot=snapshot,
                    symbol=symbol,
                    owner=owner,
                    position=position,
                )
                continue

            try:
                repaired = self.broker.place_protective_stop(
                    symbol=symbol,
                    qty=stop_qty,
                    stop_price=stop_price,
                    client_order_id_prefix=f"{owner}-repair-stop",
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
            raw_df, _stats = fetch_symbol(symbol, start, end, timeframe=slot.timeframe)
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
        self._log_entry(recovered_decision, recovered_result, latest_close)
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

            price = float(update.price) if update.price is not None else None
            qty = float(update.qty or 0)
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
                    self.trade_logger.log_stop_fill(
                        symbol=raw_symbol,
                        strategy=owner,
                        qty=qty,
                        avg_fill_price=price,
                        order_id=order_id,
                    )
                else:
                    # Price or qty unavailable — fall back to the synthetic record.
                    self.trade_logger.log_external_close(
                        symbol=raw_symbol,
                        strategy=owner,
                        reason="stop_triggered",
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
        for decision, status_str, filled_qty, avg_fill_price, order_id in fills:
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
            )
            self._record_fill(result, modeled_price=decision.entry_reference_price, order_type="limit")
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

    def _count_open_credit_spreads(self) -> int:
        """
        Count open spread positions across ALL credit_spread instances — the
        global ``MAX_TOTAL_CONCURRENT_CREDIT_SPREADS`` counter passed into
        ``build_spread_execution``.

        NOTE (PLAN.md 11.31): the ``"credit_spread"`` literal is correct while
        that is the only multi-leg strategy. A second one would need this
        filter generalized to the duck-typed ``build_spread_execution`` hook.
        """
        return sum(
            1 for p in self._positions.values()
            if p.is_spread and p.strategy_name == "credit_spread"
        )

    def _credit_spreads_snapshot(self) -> list[dict]:
        """
        Build the ``credit_spreads`` state-snapshot field — one dict per open
        spread, with the economics the dashboard renders. Sourced from the
        owning strategy's ``OpenSpread`` view (kept in sync by the entry /
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

    def _enter_credit_spread(
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
        Credit-spread entry: build the spread plan, dispatch the async MLEG
        combo, and pre-register the spread Position. The combo fill confirms
        asynchronously via ``_drain_spread_fills`` (or rolls the
        pre-registration back on cancel/reject).
        """
        def _done(status: str) -> None:
            if strategy_statuses is not None:
                strategy_statuses[symbol] = status
            if strategy_reasons is not None:
                strategy_reasons[symbol] = []
            self._mark_signal_bar_processed(
                signal_key, signal_bar, strategy_statuses, strategy_reasons, symbol
            )

        if notional_cap is None or notional_cap <= 0:
            logger.info(
                f"[{strategy.name}] {symbol}: credit spread skipped — "
                f"no sleeve notional available"
            )
            _done("No Signal")
            return

        total_open = self._count_open_credit_spreads()
        try:
            plan = strategy.build_spread_execution(
                underlying_close,
                notional_cap=notional_cap,
                total_open_credit_spreads=total_open,
            )
        except CreditSpreadRejected as e:
            logger.info(f"[{strategy.name}] {symbol}: credit spread rejected — {e}")
            _done("No Signal")
            return
        except Exception as e:
            logger.error(
                f"[{strategy.name}] {symbol}: build_spread_execution failed: {e}"
            )
            _done("No Signal")
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
        # (the existing _enter_credit_spread path was previously
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
        strategy.register_spread(OpenSpread(
            position_id=position_id,
            short_occ=plan.short_occ,
            long_occ=plan.long_occ,
            short_strike=plan.short_strike,
            long_strike=plan.long_strike,
            expiration_date=plan.expiration_date,
            net_credit=plan.net_credit,
            width=plan.width,
            qty=plan.qty,
        ))
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
            filled_qty, avg_fill_price, order_id,
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
                    # _enter_credit_spread (after dispatch_spread_order
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
                        self.trade_logger.log_spread_fill(
                            position_id=position_id,
                            strategy=strategy_name,
                            short_occ=plan.short_occ,
                            long_occ=plan.long_occ,
                            qty=float(filled_qty or plan.qty),
                            net_price=net_credit,
                            order_id=order_id,
                            opening=True,
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
                continue

            # ── Spread CLOSE ─────────────────────────────────────────────
            self._spreads_pending_close.discard(position_id)
            if filled:
                released = (
                    strategy.release_spread(position_id)
                    if strategy is not None else None
                )
                self._pop_position(position_id)
                self._spread_owner_strategy.pop(position_id, None)
                short_occ = released.short_occ if released is not None else position_id
                long_occ = released.long_occ if released is not None else ""
                close_qty = float(
                    filled_qty or (released.qty if released is not None else 1)
                )

                # The spread IS closed regardless — but only record P&L when
                # we have a real fill price. A stream "filled" event whose
                # REST follow-up failed reaches here with avg_fill_price=None;
                # treating that as a $0 debit would fabricate a full-credit
                # winner and inflate the allocator's HWM / drawdown gate. In
                # that case leave realized P&L unset (not zero) and warn.
                realized_pnl: float | None = None
                exit_reason = "spread exit"
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
                                strategy_name, realized_pnl
                            )
                    logger.info(
                        f"[{strategy_name}] credit spread CLOSED — "
                        f"position_id={position_id[:8]} "
                        f"net_debit=${net_debit:.2f}/sh realized_pnl="
                        f"{'n/a' if realized_pnl is None else f'${realized_pnl:+,.2f}'} "
                        f"order={order_id}"
                    )
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

    def _process_credit_spread_exits(
        self,
        *,
        strategy: BaseStrategy,
        underlying: str,
        underlying_close: float,
    ) -> None:
        """
        Evaluate exit triggers for every open spread this strategy holds and
        dispatch a closing MLEG combo for any that fire. A position with a
        close already in flight (``_spreads_pending_close``) is skipped so a
        stale signal cannot double-submit.
        """
        open_spreads = getattr(strategy, "open_spreads", [])
        if not open_spreads:
            return
        today = self._clock().date()
        for open_spread in list(open_spreads):
            position_id = open_spread.position_id
            if position_id in self._spreads_pending_close:
                continue
            try:
                should_exit, reason, spread_mid = strategy.evaluate_spread_exit(
                    open_spread,
                    underlying_close=underlying_close,
                    today=today,
                )
            except Exception as e:
                logger.error(
                    f"[{strategy.name}] {underlying}: evaluate_spread_exit failed "
                    f"for {position_id[:8]}: {e}"
                )
                continue
            if not should_exit:
                continue

            # Close the spread: pass the original opening legs — the broker's
            # dispatch_spread_order(closing=True) reverses them into the
            # *_TO_CLOSE trade. limit_price is a positive net debit; use the
            # current spread mid, falling back to the width (a marketable,
            # generous debit) when the mid is unavailable.
            debit = (
                round(spread_mid, 2)
                if spread_mid is not None and spread_mid > 0
                else round(open_spread.width, 2)
            )
            legs = [
                SpreadLeg(occ_symbol=open_spread.short_occ, side=Side.SELL, opening=True),
                SpreadLeg(occ_symbol=open_spread.long_occ, side=Side.BUY, opening=True),
            ]
            logger.info(
                f"[{strategy.name}] {underlying}: closing spread "
                f"{open_spread.short_occ}/{open_spread.long_occ} "
                f"position_id={position_id[:8]} — {reason} (debit ${debit:.2f})"
            )
            try:
                result = self.broker.dispatch_spread_order(
                    legs=legs,
                    qty=open_spread.qty,
                    limit_price=debit,
                    strategy_name=strategy.name,
                    position_id=position_id,
                    closing=True,
                )
            except Exception as e:
                logger.error(
                    f"[{strategy.name}] {underlying}: spread close dispatch raised: {e}"
                )
                self.risk.record_broker_error()
                self.alerts.broker_error(f"{underlying} spread close: {e}")
                continue
            if result.status is OrderStatus.ACCEPTED:
                self._spreads_pending_close.add(position_id)
            else:
                logger.warning(
                    f"[{strategy.name}] {underlying}: spread close dispatch returned "
                    f"{result.status.value} for {position_id[:8]}"
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

    def _credit_spread_strategy_for(self, underlying: str) -> BaseStrategy | None:
        """The configured CreditSpread instance trading ``underlying``, if any.

        NOTE (PLAN.md 11.31): the ``"credit_spread"`` name check is correct
        while that is the only multi-leg strategy. A second one would need
        this generalized so its restarted spreads also reconstruct.
        """
        for slot in self.slots:
            strategy = slot.strategy
            if (
                strategy.name == "credit_spread"
                and hasattr(strategy, "build_spread_execution")
                and underlying in slot.active_symbols()
            ):
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
            CreditSpread instance must be configured for the underlying —
            otherwise the spread cannot be safely managed and its underlying
            is added to ``conflicts`` (→ RESTRICTED startup mode);
          - on success the two-leg ``Position``, ``_spread_owner_strategy``
            entry, and the strategy's ``OpenSpread`` view are all rebuilt.

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

            strategy = self._credit_spread_strategy_for(underlying)
            if strategy is None:
                logger.warning(
                    f"restart: spread {position_id[:8]} ({underlying}) has no "
                    f"configured credit_spread strategy — declaring a conflict "
                    "(RESTRICTED). Close it manually or restore the slot."
                )
                conflicts.add(underlying)
                spread_leg_occs.update(leg_symbols)
                continue

            qty = int(record.get("qty") or 1)
            net_credit = float(record.get("net_credit") or 0.0)
            width = short_leg.strike - long_leg.strike
            self._positions[position_id] = make_spread(
                strategy_name=strategy_name,
                position_id=position_id,
                legs=[
                    PositionLeg(symbol=short_occ, qty=-float(qty), side="SELL"),
                    PositionLeg(symbol=long_occ, qty=float(qty), side="BUY"),
                ],
            )
            self._spread_owner_strategy[position_id] = strategy
            strategy.register_spread(OpenSpread(
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
        """Restore entry prices for currently-open broker positions from the trade log."""
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
            entry_price = float(context.get("entry_reference_price") or 0.0)
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
            "cooldown_state": {},
            "sleeve_dd_state": {},
        }
        try:
            out["is_halted"] = bool(self.risk.is_halted())
            out["halt_reason"] = self.risk.halt_reason()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"risk halt accessor failed: {exc}")
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
            for sym, strat in self._owners_view().items():
                pos = broker_positions.get(sym)
                positions_detail[sym] = {
                    "strategy": strat,
                    "qty": pos.qty if pos else None,
                    "avg_entry_price": pos.avg_entry_price if pos else None,
                    "market_value": pos.market_value if pos else None,
                    "unrealized_pnl": (
                        pos.market_value - pos.qty * pos.avg_entry_price
                        if pos else None
                    ),
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
        if self.config.cancel_orders_on_shutdown:
            try:
                for o in self.broker.get_open_orders():
                    logger.info(f"shutdown: canceling order {o.order_id} ({o.symbol})")
                    self.broker.cancel_order(o.order_id)
            except Exception as e:
                logger.error(f"shutdown cleanup failed: {e}")
        if self._stream_manager is not None:
            self._stream_manager.stop()


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
