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

import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd
from loguru import logger

from config import settings
from config.settings import SLIPPAGE_MODEL_MARKET_BPS
from data.fetcher import StaleDataError, close_connections, fetch_symbol, require_fresh
from execution.broker import (
    AlpacaBroker,
    BrokerSnapshot,
    OrderResult,
    OrderStatus,
)
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
from strategies.base import BaseStrategy, StrategySlot


# ── Bar-interval helpers ─────────────────────────────────────────────────────


# Mirrors `data.fetcher._TIMEFRAME_MAP` but only the bit the engine cares
# about — the wall-clock duration of one bar.
_BAR_INTERVAL: dict[str, timedelta] = {
    "1Day": timedelta(days=1),
    "1Hour": timedelta(hours=1),
    "1Min": timedelta(minutes=1),
}

# Calendar days per bar — accounts for weekends/holidays so we always
# fetch enough bars.  Conservative: 1 daily bar ≈ 1.5 calendar days,
# 1 hourly bar ≈ 1 calendar day / 6.5 trading hours.
_CALENDAR_DAYS_PER_BAR: dict[str, float] = {
    "1Day": 1.5,
    "1Hour": 1.0 / 6.5,
    "1Min": 1.0 / (6.5 * 60),
}


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

    def __post_init__(self) -> None:
        if self.cycle_interval_seconds <= 0:
            raise ValueError("cycle_interval_seconds must be > 0")
        if self.max_bar_age_multiplier <= 1:
            raise ValueError("max_bar_age_multiplier must be > 1")
        if self.atr_length < 1:
            raise ValueError("atr_length must be >= 1")
        if self.history_lookback_days < 1:
            raise ValueError("history_lookback_days must be >= 1")


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
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._running: bool = False
        self._session_start_equity: float | None = None
        self._cycle_count: int = 0
        self._last_cycle_end: float = 0.0  # monotonic timestamp

        # Position ownership: symbol → strategy_name.  Tracks which strategy
        # opened each position so that exit signals from a *different* strategy
        # watching the same symbol don't close someone else's trade.
        self._position_owners: dict[str, str] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────

    def start(self, *, max_cycles: int | None = None) -> None:
        """
        Run the loop until SIGINT, `stop()`, or `max_cycles` (if set).
        `max_cycles` is for tests / verify scripts; production calls leave
        it None and rely on signal-driven shutdown.
        """
        self._install_signal_handlers()
        self._running = True
        self._cycle_count = 0

        # Capture truth-of-the-world before any decision.
        startup_snapshot = self.broker.sync_with_broker()
        self._session_start_equity = startup_snapshot.account.equity

        all_symbols = []
        for slot in self.slots:
            all_symbols.extend(slot.active_symbols())
        unique_symbols = sorted(set(all_symbols))

        # Seed position ownership from broker state.  If we're restarting
        # with existing positions, assign each to the *first* slot whose
        # symbol list includes it.  This is best-effort — an operator who
        # reshuffles slot configs between restarts should review manually.
        # TODO Phase 10: restore ownership from trade DB (last open trade per
        # symbol) instead of guessing from slot ordering. See PLAN.md §10.
        for sym in startup_snapshot.account.open_positions:
            if sym not in self._position_owners:
                matched = False
                for slot in self.slots:
                    if sym in slot.active_symbols():
                        self._position_owners[sym] = slot.strategy.name
                        logger.info(
                            f"restart: assigned existing position {sym} "
                            f"→ '{slot.strategy.name}' (best-effort slot match)"
                        )
                        matched = True
                        break
                if not matched:
                    logger.warning(
                        f"restart: open position {sym} does not belong to any "
                        "configured slot — it will NOT be managed by this engine. "
                        "Close it manually or add it to a strategy's symbol universe."
                    )

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
                logger.info(f"cycle {cycle_id} skipped: market closed")
                return

            try:
                snapshot = self.broker.sync_with_broker(
                    session_start_equity=self._session_start_equity
                )
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

            for slot in self.slots:
                symbols = slot.active_symbols()
                for symbol in symbols:
                    try:
                        processed_symbols += 1
                        filled = self._process_symbol(
                            symbol,
                            snapshot,
                            running_account,
                            slot.strategy,
                            slot.timeframe,
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
                                open_positions=updated_positions,
                            )
                    except Exception as e:
                        # Never let one symbol kill the cycle.
                        error_count += 1
                        cycle_status = "symbol_errors"
                        logger.exception(f"{symbol}: cycle step failed: {e}")
        finally:
            duration = time.monotonic() - cycle_started_mono
            logger.info(
                f"cycle {cycle_id} complete: status={cycle_status}, "
                f"processed={processed_symbols}/{total_symbols}, "
                f"new_positions={new_positions}, errors={error_count}, "
                f"duration={duration:.1f}s, "
                f"next_cycle_in={self.config.cycle_interval_seconds:.0f}s"
            )
            # Close idle HTTP connections so they don't go stale during the
            # inter-cycle sleep (5 min default).  Fresh connections are cheap.
            close_connections()
            self._last_cycle_end = time.monotonic()

    def _process_symbol(
        self,
        symbol: str,
        snapshot: BrokerSnapshot,
        account: AccountState,
        strategy: BaseStrategy,
        timeframe: str,
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
            require_fresh(df, max_bar_age, symbol)
        except StaleDataError as e:
            logger.warning(f"{symbol}: skipping — {e}")
            self.alerts.stale_data(symbol, str(e))
            return

        # 3. Indicators (just ATR — strategy adds its own).
        df = add_atr(df, self.config.atr_length)
        atr_col = f"atr_{self.config.atr_length}"
        latest_atr = float(df[atr_col].iloc[-1])
        latest_close = float(df["close"].iloc[-1])
        latest_ts = df.index[-1]

        # 4. Signals.
        signals = strategy.generate_signals(df)
        last_entry = bool(signals.entries.iloc[-1])
        last_exit = bool(signals.exits.iloc[-1])

        position = account.open_positions.get(symbol)
        logger.info(
            f"[{strategy.name}] {symbol}: bar={latest_ts.isoformat()} "
            f"close=${latest_close:.2f} atr=${latest_atr:.2f} "
            f"entry={last_entry} exit={last_exit} "
            f"position={'OPEN ' + str(position.qty) if position else 'flat'}"
        )

        # 5. Exit branch — close before considering entries (always safe to
        # reduce risk; never blocked by halt).
        if last_exit and position is not None:
            # Only the strategy that opened the position may close it.
            owner = self._position_owners.get(symbol)
            if owner is not None and owner != strategy.name:
                logger.info(
                    f"[{strategy.name}] {symbol}: exit signal ignored — "
                    f"position owned by '{owner}'"
                )
                return
            if self._has_pending_close_order(symbol, snapshot):
                logger.info(
                    f"{symbol}: exit signal but a close order is already pending — skipping"
                )
                return
            try:
                result = self.broker.close_position(symbol)
                # close_position always uses MARKET (hard-risk exit).
                self._record_fill(result, modeled_price=latest_close, order_type="market")
                self._log_close(result, latest_close, strategy.name)
                # Release ownership.
                self._position_owners.pop(symbol, None)
            except Exception as e:
                logger.error(f"{symbol}: close_position failed: {e}")
                self.risk.record_broker_error()
                self.alerts.broker_error(f"{symbol} close_position: {e}")
            return

        # 6. Entry branch — risk is the gate.
        if not last_entry:
            return
        if position is not None:
            # Already in this position — the crossover bar persists across
            # intra-day cycles, so this is expected noise, not a real signal.
            # Risk would reject anyway; skip to avoid spamming alerts.
            return

        sig = Signal(
            symbol=symbol,
            side=Side.BUY,
            strategy_name=strategy.name,
            reference_price=latest_close,
            atr=latest_atr,
            reason=f"{strategy.name} entry @ {latest_ts.isoformat()}",
            order_type=strategy.preferred_order_type,
        )
        decision = self.risk.evaluate(sig, account)
        if isinstance(decision, RiskRejection):
            # Already logged by risk; alert the operator.
            self.alerts.order_rejection(
                symbol, strategy.name, decision.message, decision.code.value
            )
            return None
        assert isinstance(decision, RiskDecision)

        try:
            result = self.broker.place_order(decision)
            self._record_fill(
                result,
                modeled_price=latest_close,
                order_type=decision.order_type.value,
            )
            self._log_entry(decision, result, latest_close)
            if result.status in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
                self._position_owners[symbol] = strategy.name
                fill_price = result.avg_fill_price or decision.entry_reference_price
                fill_qty = int(result.filled_qty or decision.qty)
                return Position(
                    symbol=symbol,
                    qty=fill_qty,
                    avg_entry_price=fill_price,
                    market_value=fill_qty * fill_price,
                )
        except Exception as e:
            logger.error(f"{symbol}: place_order raised: {e}")
            self.risk.record_broker_error()
            self.alerts.broker_error(f"{symbol} place_order: {e}")
        return None

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

    @staticmethod
    def _has_pending_close_order(symbol: str, snapshot: BrokerSnapshot) -> bool:
        """True if there's already an open SELL order for this symbol."""
        return any(
            o.symbol == symbol and o.side is Side.SELL
            for o in snapshot.open_orders
        )

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
        if not self.config.cancel_orders_on_shutdown:
            return
        try:
            for o in self.broker.get_open_orders():
                logger.info(f"shutdown: canceling order {o.order_id} ({o.symbol})")
                self.broker.cancel_order(o.order_id)
        except Exception as e:
            logger.error(f"shutdown cleanup failed: {e}")


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
