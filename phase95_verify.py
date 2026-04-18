"""
Phase 9.5 — Forward-Test Infrastructure — integration verification.

Verifies that the forward-test tooling works end-to-end:

  1. **get_closed_orders** — retrieves historical orders from Alpaca paper.
  2. **Reconciler** — runs against current trade CSV (may be empty) and
     produces a ReconciliationResult with the gate decision.
  3. **Report generation** — writes the go/no-go markdown report.
  4. **Engine launch** — runs 3 cycles with forward_test config to verify
     the launcher wiring.

The actual multi-week forward test is an operational step — this script
validates that the infrastructure is ready to support it.

Run: `python phase95_verify.py`
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

from backtest.reconcile import Reconciler, ReconciliationResult
from config import settings
from engine.trader import EngineConfig, TradingEngine
from execution.broker import AlpacaBroker
from reporting.alerts import AlertDispatcher
from reporting.logger import TradeLogger, install_json_sink
from reporting.pnl import PnLTracker
from risk.manager import RiskManager
from strategies.base import BaseStrategy, OrderType, SignalFrame
from strategies.sma_crossover import SMACrossover

import pandas as pd


# ── Logging ──────────────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
)
logger.add("logs/phase95.log", rotation="1 MB", level="DEBUG")


PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        logger.info(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED.append(label)
        logger.error(f"  ❌ {label}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    logger.info("")
    logger.info(f"── {title} " + "─" * (60 - len(title)))


# ── No-op strategy for engine test ──────────────────────────────────────────


class NoOpStrategy(BaseStrategy):
    name = "noop_verify95"
    preferred_order_type = OrderType.MARKET

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        return SignalFrame(
            entries=pd.Series(False, index=df.index, dtype=bool),
            exits=pd.Series(False, index=df.index, dtype=bool),
        )


# ── Tests ────────────────────────────────────────────────────────────────────


def test_get_closed_orders(broker: AlpacaBroker) -> None:
    section("get_closed_orders — Alpaca paper fill history")
    orders = broker.get_closed_orders(limit=10)
    check(
        "get_closed_orders returns a list",
        isinstance(orders, list),
        f"{len(orders)} order(s)",
    )
    # May be empty if account has no history — that's OK.
    if orders:
        first = orders[0]
        check("order has symbol", hasattr(first, "symbol") and first.symbol)
        check(
            "order has status",
            hasattr(first, "status") and first.status is not None,
        )


def test_reconciler(broker: AlpacaBroker) -> None:
    section("Reconciler — paper vs backtest comparison")

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    # Use a tmp dir for the report.
    tmp_forward_dir = "logs/_phase95_verify/forward_tests"

    recon = Reconciler(
        SMACrossover(fast=20, slow=50),
        ["AAPL"],
        week_ago,
        today,
        forward_test_dir=tmp_forward_dir,
        return_divergence_threshold=0.50,  # generous for verify
        max_slippage_threshold=100.0,      # generous for verify
    )

    result = recon.run()

    check(
        "ReconciliationResult created",
        isinstance(result, ReconciliationResult),
    )
    check(
        "strategy_name set",
        result.strategy_name == "sma_crossover",
    )
    check(
        "date range correct",
        result.start_date == week_ago and result.end_date == today,
    )
    check(
        "backtest_return_pct is a number",
        isinstance(result.backtest_return_pct, (int, float)),
        f"{result.backtest_return_pct:.2f}%",
    )
    check(
        "gate decision is boolean",
        isinstance(result.go, bool),
        f"go={result.go}",
    )
    check(
        "reasons populated",
        len(result.reasons) > 0,
        "; ".join(result.reasons),
    )

    return result, recon


def test_report_generation(
    result: ReconciliationResult, recon: Reconciler
) -> None:
    section("Report generation — go/no-go markdown")
    path = recon.write_report(result)
    check("report file exists", os.path.exists(path), path)
    if os.path.exists(path):
        content = open(path).read()
        check("report contains verdict", "GO" in content)
        check("report contains strategy name", "sma_crossover" in content)
        check("report contains return data", "Paper return" in content)


def test_engine_with_forward_test_config(broker: AlpacaBroker) -> None:
    section("Engine — 3 cycles with forward-test wiring")
    tmp_dir = "logs/_phase95_verify"
    csv_path = os.path.join(tmp_dir, "trades.csv")
    daily_dir = os.path.join(tmp_dir, "daily_pnl")

    config = EngineConfig(
        symbols=["AAPL"],
        timeframe="1Day",
        cycle_interval_seconds=2,
        market_hours_only=False,
        cancel_orders_on_shutdown=True,
    )

    trade_logger = TradeLogger(path=csv_path)
    pnl_tracker = PnLTracker(trade_csv_path=csv_path, daily_pnl_dir=daily_dir)
    alerts = AlertDispatcher(cooldown_seconds=0)

    engine = TradingEngine(
        strategy=NoOpStrategy(),
        risk=RiskManager(),
        broker=broker,
        config=config,
        trade_logger=trade_logger,
        pnl_tracker=pnl_tracker,
        alerts=alerts,
    )
    engine.start(max_cycles=3)

    check(
        "engine completed 3 cycles",
        engine._cycle_count == 3,
        f"actual={engine._cycle_count}",
    )


def test_forward_test_settings() -> None:
    section("Forward-test settings in config")
    check(
        "FORWARD_TEST_DIR configured",
        hasattr(settings, "FORWARD_TEST_DIR")
        and settings.FORWARD_TEST_DIR != "",
    )
    check(
        "FORWARD_TEST_RETURN_DIVERGENCE_PCT configured",
        hasattr(settings, "FORWARD_TEST_RETURN_DIVERGENCE_PCT")
        and settings.FORWARD_TEST_RETURN_DIVERGENCE_PCT > 0,
        f"{settings.FORWARD_TEST_RETURN_DIVERGENCE_PCT:.0%}",
    )
    check(
        "FORWARD_TEST_MAX_SLIPPAGE_BPS configured",
        hasattr(settings, "FORWARD_TEST_MAX_SLIPPAGE_BPS")
        and settings.FORWARD_TEST_MAX_SLIPPAGE_BPS > 0,
        f"{settings.FORWARD_TEST_MAX_SLIPPAGE_BPS} bps",
    )


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    logger.info("=" * 60)
    logger.info("Phase 9.5 — Forward-Test Infrastructure — verification")
    logger.info("=" * 60)

    try:
        broker = AlpacaBroker()
        snap = broker.sync_with_broker()
        logger.info(
            f"pre-flight: equity=${snap.account.equity:,.2f}, "
            f"positions={len(snap.account.open_positions)}"
        )
    except Exception as e:
        logger.exception(f"setup failed: {e}")
        return 2

    test_forward_test_settings()
    test_get_closed_orders(broker)
    result_and_recon = test_reconciler(broker)
    if result_and_recon is not None:
        result, recon = result_and_recon
        test_report_generation(result, recon)
    test_engine_with_forward_test_config(broker)

    logger.info("")
    logger.info("=" * 60)
    logger.info(f"PASSED: {len(PASSED)}    FAILED: {len(FAILED)}")
    logger.info("=" * 60)
    if FAILED:
        for label in FAILED:
            logger.error(f"  ❌ {label}")
        return 1

    logger.info("")
    logger.info("Infrastructure is ready. To start the forward test:")
    logger.info("  python forward_test.py")
    logger.info("Run for 2-4 weeks, then reconcile:")
    logger.info("  python -c \"from backtest.reconcile import Reconciler; ...\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
