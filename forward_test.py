"""
Forward-test launcher (Phase 9.5).

Starts the trading engine on paper with full reporting wired up for a
multi-week forward test. Run with:

    python forward_test.py

The bot runs continuously until SIGINT (Ctrl+C). On shutdown it writes
a daily P&L report for the session. After the multi-week run, use
`backtest/reconcile.py` to compare paper fills against backtest predictions.

Reconcile after the run:

    python -c "
    from backtest.reconcile import Reconciler
    from strategies.sma_crossover import SMACrossover
    r = Reconciler(SMACrossover(20,50), ['AAPL','MSFT','GOOGL','AMZN','NVDA'],
                   '2026-04-17', '2026-05-15')
    result = r.run()
    r.write_report(result)
    print('GO' if result.go else 'NO-GO', result.reasons)
    "
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from loguru import logger

from config import settings
from engine.trader import EngineConfig, TradingEngine
from execution.broker import AlpacaBroker
from reporting.alerts import AlertDispatcher
from reporting.logger import TradeLogger, install_json_sink
from reporting.pnl import PnLTracker
from risk.manager import RiskManager
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


def main() -> None:
    logger.info("=" * 60)
    logger.info("Forward Test — Paper Trading (Phase 9.5)")
    logger.info("=" * 60)

    # JSON structured log.
    install_json_sink()

    # Strategy — same params as backtest baseline.
    strategy = SMACrossover(fast=20, slow=50)

    # Risk manager with production settings.
    risk = RiskManager()

    # Broker (paper).
    broker = AlpacaBroker()

    # Reporting.
    trade_logger = TradeLogger()
    pnl_tracker = PnLTracker()
    alerts = AlertDispatcher()

    # Engine config — production settings.
    config = EngineConfig(
        symbols=list(settings.WATCHLIST),
        timeframe=settings.ENGINE_TIMEFRAME,
        history_lookback_days=settings.ENGINE_HISTORY_LOOKBACK_DAYS,
        cycle_interval_seconds=settings.ENGINE_CYCLE_INTERVAL_SECONDS,
        max_bar_age_multiplier=settings.ENGINE_MAX_BAR_AGE_MULTIPLIER,
        market_hours_only=True,
        cancel_orders_on_shutdown=True,
    )

    engine = TradingEngine(
        strategy=strategy,
        risk=risk,
        broker=broker,
        config=config,
        trade_logger=trade_logger,
        pnl_tracker=pnl_tracker,
        alerts=alerts,
    )

    logger.info(
        f"strategy={strategy.name}, symbols={config.symbols}, "
        f"timeframe={config.timeframe}, cycle={config.cycle_interval_seconds}s"
    )
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
