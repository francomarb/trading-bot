"""
Forward-test launcher (Phase 10).

Starts the trading engine on paper with full reporting wired up for a
multi-week forward test. Run with:

    python forward_test.py

The bot runs continuously until SIGINT (Ctrl+C). On shutdown it writes
a daily P&L report for the session. After the multi-week run, use
`backtest/reconcile.py` to compare paper fills against backtest predictions.

Reconcile after the run:

    python -c "
    from backtest.reconcile import Reconciler
    from config import settings
    from strategies.sma_crossover import SMACrossover
    r = Reconciler(
        SMACrossover(20, 50),
        list(settings.SMA_WATCHLIST),
        'YYYY-MM-DD',
        'YYYY-MM-DD',
    )
    result = r.run()
    r.write_report(result)
    print('GO' if result.go else 'NO-GO', result.reasons)
    "
"""

from __future__ import annotations

import sys
import subprocess
from datetime import datetime, timezone

from loguru import logger

from config import settings
from engine.trader import EngineConfig, TradingEngine
from execution.broker import AlpacaBroker
from execution.stream import StreamManager
from regime.detector import MarketRegime, RegimeDetector
from risk.allocator import SleeveAllocator
from reporting.alerts import AlertDispatcher
from reporting.logger import TradeLogger, install_json_sink
from reporting.pnl import PnLTracker
from risk.manager import RiskManager
from data.watchlists import StaticWatchlistSource
from strategies.base import StrategySlot
from strategies.filters.sma_crossover import SMAEdgeFilter
from strategies.sma_crossover import SMACrossover


# ── Logging ──────────────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stdout,
    format=(
        "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
        "{message}"
    ),
    level="INFO",
)
logger.add("logs/forward_test.log", rotation="10 MB", retention="90 days", level="DEBUG")


def _git_version() -> str:
    """Return a concise git identity for the running bot code."""
    try:
        result = subprocess.run(
            ["git", "describe", "--always", "--dirty", "--tags"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def main() -> None:
    logger.info("=" * 60)
    logger.info("Forward Test — Paper Trading (Phase 10)")
    logger.info("=" * 60)
    logger.info(f"bot version: {_git_version()}")
    logger.info(f"python={sys.version.split()[0]} paper={settings.ALPACA_PAPER}")

    # JSON structured log.
    install_json_sink()

    # ── Capital sleeve allocator (Phase 10.F1) ──────────────────────────
    # Enforces per-strategy gross-notional budgets so SMA cannot starve RSI
    # (or vice versa). Weights are 50/50 until ≥4 weeks of combined paper
    # data justify a rebalance. Idle sleeve capital stays locked.
    allocator = SleeveAllocator(
        allocations=settings.STRATEGY_ALLOCATIONS,
        total_gross_pct=settings.MAX_GROSS_EXPOSURE_PCT,
        min_trade_notional=settings.MIN_TRADE_NOTIONAL,
    )

    # ── Regime detector (Phase 10.F2) ───────────────────────────────────
    # Classifies SPY into BEAR / VOLATILE / TRENDING / RANGING once per
    # cycle with a 10-minute TTL cache. Each slot's allowed_regimes gates
    # new entries; exits are never blocked.
    regime = RegimeDetector()

    # ── Strategy slots ──────────────────────────────────────────────────
    slots = [
        StrategySlot(
            strategy=SMACrossover(fast=20, slow=50, edge_filter=SMAEdgeFilter()),
            watchlist_source=StaticWatchlistSource(
                list(settings.SMA_WATCHLIST), name="sma"
            ),
            # SMA crossover works in both trending and ranging markets;
            # blocked only in BEAR and VOLATILE where edge and risk/reward
            # degrade significantly.
            allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
        ),
        # Add more slots here as new strategies are built:
        # StrategySlot(
        #     strategy=RSIReversion(...),
        #     watchlist_source=StaticWatchlistSource(
        #         list(settings.RSI_WATCHLIST), name="rsi"
        #     ),
        #     allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
        # ),
    ]

    # Risk manager with production settings (shared across all slots).
    risk = RiskManager()

    # WebSocket stream for real-time fill/stop-out detection (Phase 10.E1).
    stream = StreamManager(
        api_key=settings.ALPACA_API_KEY or "",
        secret_key=settings.ALPACA_SECRET_KEY or "",
        paper=settings.ALPACA_PAPER,
    )

    # Broker (paper) — stream wired for stream-first fill detection.
    broker = AlpacaBroker(stream_manager=stream)

    # Reporting.
    trade_logger = TradeLogger()
    pnl_tracker = PnLTracker()
    alerts = AlertDispatcher()

    # Engine config (engine-level only; symbols/timeframes live on slots).
    config = EngineConfig(
        history_lookback_days=settings.ENGINE_HISTORY_LOOKBACK_DAYS,
        cycle_interval_seconds=settings.ENGINE_CYCLE_INTERVAL_SECONDS,
        max_bar_age_multiplier=settings.ENGINE_MAX_BAR_AGE_MULTIPLIER,
        market_hours_only=True,
        cancel_orders_on_shutdown=settings.ENGINE_CANCEL_ORDERS_ON_SHUTDOWN,
    )

    engine = TradingEngine(
        slots=slots,
        risk=risk,
        broker=broker,
        config=config,
        trade_logger=trade_logger,
        pnl_tracker=pnl_tracker,
        alerts=alerts,
        stream_manager=stream,
        regime_detector=regime,
        allocator=allocator,
    )

    slot_desc = ", ".join(
        f"{s.strategy.name}({s.active_symbols()})" for s in slots
    )
    logger.info(f"slots: {slot_desc}")
    logger.info(f"cycle={config.cycle_interval_seconds}s")
    logger.info("starting engine — Ctrl+C to stop")
    logger.info("after multi-week run, use backtest/reconcile.py to evaluate")

    try:
        engine.start()
    finally:
        # Write a daily summary on shutdown.
        try:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            snap = broker.sync_with_broker()
            summary = pnl_tracker.generate_daily_summary(
                day=today,
                session_start_equity=engine._session_start_equity or snap.account.equity,
                session_end_equity=snap.account.equity,
            )
            pnl_tracker.write_daily_report(summary)
        except Exception as e:
            logger.error(f"shutdown P&L report failed: {e}")

        logger.info("forward test session ended")


if __name__ == "__main__":
    main()
