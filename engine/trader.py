"""
Trading engine — the live loop (Phase 8).

`TradingEngine` orchestrates the modules built in Phases 2–7 into a single
restart-safe runnable bot. Each cycle does, in order:

    sync_with_broker → fetch bars → freshness check → add indicators →
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

  7. **Read-only by default for state changes.** The decision path is the
     *only* place that calls `broker.place_order` / `close_position`. Risk
     is the only producer of `RiskDecision`. Both contracts come from
     Phases 6/7 and the engine respects them.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd
from loguru import logger

from config import settings
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
    RiskDecision,
    RiskManager,
    RiskRejection,
    Side,
    Signal,
)
from reporting.alerts import AlertDispatcher
from reporting.logger import TradeLogger
from reporting.pnl import PnLTracker
from strategies.base import BaseStrategy


# ── Bar-interval helpers ─────────────────────────────────────────────────────


# Mirrors `data.fetcher._TIMEFRAME_MAP` but only the bit the engine cares
# about — the wall-clock duration of one bar.
_BAR_INTERVAL: dict[str, timedelta] = {
    "1Day": timedelta(days=1),
    "1Hour": timedelta(hours=1),
    "1Min": timedelta(minutes=1),
}


# ── Config ───────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EngineConfig:
    """All knobs the engine exposes. Defaults pull from `config.settings`."""

    symbols: list[str] = field(default_factory=lambda: list(settings.WATCHLIST))
    timeframe: str = settings.ENGINE_TIMEFRAME
    history_lookback_days: int = settings.ENGINE_HISTORY_LOOKBACK_DAYS
    cycle_interval_seconds: float = settings.ENGINE_CYCLE_INTERVAL_SECONDS
    max_bar_age_multiplier: float = settings.ENGINE_MAX_BAR_AGE_MULTIPLIER
    market_hours_only: bool = settings.ENGINE_MARKET_HOURS_ONLY
    cancel_orders_on_shutdown: bool = settings.ENGINE_CANCEL_ORDERS_ON_SHUTDOWN
    atr_length: int = settings.ATR_LENGTH

    def __post_init__(self) -> None:
        if not self.symbols:
            raise ValueError("EngineConfig.symbols must not be empty")
        if self.timeframe not in _BAR_INTERVAL:
            raise ValueError(
                f"unsupported timeframe '{self.timeframe}'. "
                f"Supported: {list(_BAR_INTERVAL)}"
            )
        if self.cycle_interval_seconds <= 0:
            raise ValueError("cycle_interval_seconds must be > 0")
        if self.max_bar_age_multiplier <= 1:
            raise ValueError("max_bar_age_multiplier must be > 1")
        if self.atr_length < 1:
            raise ValueError("atr_length must be >= 1")
        if self.history_lookback_days < 1:
            raise ValueError("history_lookback_days must be >= 1")

    @property
    def bar_interval(self) -> timedelta:
        return _BAR_INTERVAL[self.timeframe]

    @property
    def max_bar_age(self) -> timedelta:
        return self.bar_interval * self.max_bar_age_multiplier


# ── Engine ───────────────────────────────────────────────────────────────────


class TradingEngine:
    """
    The main loop. Wire one strategy + one risk manager + one broker.

    Concurrency is intentionally simple: a single thread cycling through
    symbols sequentially. Per-symbol latency (network + polling) is the
    bottleneck, but for a daily/hourly strategy on <10 symbols this is
    deeply non-binding.
    """

    def __init__(
        self,
        *,
        strategy: BaseStrategy,
        risk: RiskManager,
        broker: AlpacaBroker,
        config: EngineConfig | None = None,
        trade_logger: TradeLogger | None = None,
        pnl_tracker: PnLTracker | None = None,
        alerts: AlertDispatcher | None = None,
        # Injection seam for tests — production should leave this as None.
        clock: callable = None,  # type: ignore[assignment]
    ) -> None:
        self.strategy = strategy
        self.risk = risk
        self.broker = broker
        self.config = config or EngineConfig()
        self.trade_logger = trade_logger or TradeLogger()
        self.pnl_tracker = pnl_tracker or PnLTracker()
        self.alerts = alerts or AlertDispatcher()
        self._clock = clock or (lambda: datetime.now(timezone.utc))

        self._running: bool = False
        self._session_start_equity: float | None = None
        self._cycle_count: int = 0

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

        # Phase 8.3: capture truth-of-the-world before any decision.
        startup_snapshot = self.broker.sync_with_broker()
        self._session_start_equity = startup_snapshot.account.equity
        logger.info(
            f"engine starting: {len(self.config.symbols)} symbol(s), "
            f"strategy={self.strategy.name}, timeframe={self.config.timeframe}, "
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
        One full sweep across all configured symbols. Wraps the whole cycle
        in a try/except so one bad cycle never crashes the loop.
        """
        cycle_id = self._cycle_count
        logger.info(f"── cycle {cycle_id} ──────────────────────────────")

        if self.config.market_hours_only and not self._market_open():
            logger.info("market closed — skipping cycle")
            return

        try:
            snapshot = self.broker.sync_with_broker(
                session_start_equity=self._session_start_equity
            )
        except Exception as e:
            logger.error(f"sync_with_broker failed: {e}; skipping cycle")
            self.risk.record_broker_error()
            self.alerts.broker_error(str(e))
            return

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

        for symbol in self.config.symbols:
            try:
                self._process_symbol(symbol, snapshot)
            except Exception as e:
                # Phase 8.8: never let one symbol kill the cycle.
                logger.exception(f"{symbol}: cycle step failed: {e}")

        # Close idle HTTP connections so they don't go stale during the
        # inter-cycle sleep (5 min default).  Fresh connections are cheap.
        close_connections()

    def _process_symbol(self, symbol: str, snapshot: BrokerSnapshot) -> None:
        """
        The full per-symbol decision path. Any expected exception type
        (StaleDataError, etc.) is caught and logged at WARNING/ERROR; the
        outer `_run_one_cycle` catches anything unexpected.
        """
        # 1. Fetch bars.
        end = self._clock()
        start = end - timedelta(days=self.config.history_lookback_days)
        try:
            df, stats = fetch_symbol(symbol, start, end, timeframe=self.config.timeframe)
        except Exception as e:
            logger.error(f"{symbol}: fetch failed: {e}")
            return
        if df.empty:
            logger.warning(f"{symbol}: fetch returned no bars")
            return

        # 2. Freshness gate.
        try:
            require_fresh(df, self.config.max_bar_age, symbol)
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
        signals = self.strategy.generate_signals(df)
        last_entry = bool(signals.entries.iloc[-1])
        last_exit = bool(signals.exits.iloc[-1])

        position = snapshot.account.open_positions.get(symbol)
        logger.info(
            f"{symbol}: bar={latest_ts.isoformat()} close=${latest_close:.2f} "
            f"atr=${latest_atr:.2f} entry={last_entry} exit={last_exit} "
            f"position={'OPEN ' + str(position.qty) if position else 'flat'}"
        )

        # 5. Exit branch — close before considering entries (always safe to
        # reduce risk; never blocked by halt).
        if last_exit and position is not None:
            if self._has_pending_close_order(symbol, snapshot):
                logger.info(
                    f"{symbol}: exit signal but a close order is already pending — skipping"
                )
                return
            try:
                result = self.broker.close_position(symbol)
                self._record_fill(result, modeled_price=latest_close)
                self._log_close(result, latest_close)
            except Exception as e:
                logger.error(f"{symbol}: close_position failed: {e}")
                self.risk.record_broker_error()
                self.alerts.broker_error(f"{symbol} close_position: {e}")
            return

        # 6. Entry branch — risk is the gate.
        if not last_entry:
            return
        if position is not None:
            # Risk would reject this anyway (DUPLICATE_POSITION); short-circuit
            # for clarity. Still let risk evaluate so the rejection is logged
            # uniformly via the same path.
            pass

        sig = Signal(
            symbol=symbol,
            side=Side.BUY,
            strategy_name=self.strategy.name,
            reference_price=latest_close,
            atr=latest_atr,
            reason=f"{self.strategy.name} entry @ {latest_ts.isoformat()}",
            order_type=self.strategy.preferred_order_type,
        )
        decision = self.risk.evaluate(sig, snapshot.account)
        if isinstance(decision, RiskRejection):
            # Already logged by risk; alert the operator.
            self.alerts.order_rejection(
                symbol, self.strategy.name, decision.message, decision.code.value
            )
            return
        assert isinstance(decision, RiskDecision)

        try:
            result = self.broker.place_order(decision)
            self._record_fill(result, modeled_price=latest_close)
            self._log_entry(decision, result, latest_close)
        except Exception as e:
            logger.error(f"{symbol}: place_order raised: {e}")
            self.risk.record_broker_error()
            self.alerts.broker_error(f"{symbol} place_order: {e}")

    # ── Post-fill bookkeeping ────────────────────────────────────────────

    def _record_fill(self, result: OrderResult, *, modeled_price: float) -> None:
        """
        Feed the realized vs. modeled slippage into the Phase 6 drift kill
        switch. Modeled fill = the bar close we acted on; realized fill =
        what Alpaca actually gave us. Phase 9 will persist this to the
        trade CSV — for now it just feeds the live drift detector.
        """
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            return
        if result.avg_fill_price is None or modeled_price <= 0:
            return
        # Bps cost on the buy side: positive slippage = paid more than expected.
        # We model 0bps (we use the close as our reference). Realized is the
        # absolute deviation in bps. Both are ≥ 0 by contract.
        modeled_bps = 0.0
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
        """Log an entry fill to the trade CSV."""
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
    ) -> None:
        """Log an exit fill to the trade CSV."""
        if result.status not in {OrderStatus.FILLED, OrderStatus.PARTIAL}:
            return
        try:
            record = self.trade_logger.build_close_record(
                result,
                strategy_name=self.strategy.name,
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
    strategy = SMACrossover(fast=20, slow=50)
    risk = RiskManager()
    broker = AlpacaBroker()
    engine = TradingEngine(strategy=strategy, risk=risk, broker=broker)
    engine.start()


if __name__ == "__main__":
    main()
