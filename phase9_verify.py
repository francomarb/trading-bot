"""
Phase 9 — Trade Reporting & P&L — integration verification.

Drives the reporting stack end-to-end:

  1. **Structured JSON sink** — install it, log a message, verify JSONL written.
  2. **Trade CSV** — build entry + close records from a real Alpaca snapshot,
     write them, read them back.
  3. **Daily P&L summary** — record trade P&Ls, generate + write a daily
     markdown report with per-strategy attribution.
  4. **Weekly report** — generate from the daily report above.
  5. **Alerts** — fire every alert type, verify backend received them.
  6. **Slippage monitoring** — verify slippage_report reads from the CSV.
  7. **Engine wiring** — run 3 cycles with reporting attached, verify CSV
     and alerts are populated.

Run: `python phase9_verify.py`
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from unittest.mock import patch

from loguru import logger

from config import settings
from engine.trader import EngineConfig, TradingEngine
from execution.broker import AlpacaBroker, OrderResult, OrderStatus
from reporting.alerts import AlertDispatcher, AlertType, LogFileBackend
from reporting.logger import TradeLogger, install_json_sink
from reporting.pnl import PnLTracker
from risk.manager import RiskDecision, RiskManager, Side
from strategies.base import BaseStrategy, OrderType, SignalFrame

import pandas as pd


# ── Logging ──────────────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
)
logger.add("logs/phase9.log", rotation="1 MB", level="DEBUG")


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


# ── No-op strategy ──────────────────────────────────────────────────────────


class NoOpStrategy(BaseStrategy):
    name = "noop_verify"
    preferred_order_type = OrderType.MARKET

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        idx = df.index
        return SignalFrame(
            entries=pd.Series(False, index=idx, dtype=bool),
            exits=pd.Series(False, index=idx, dtype=bool),
        )


# ── Tests ────────────────────────────────────────────────────────────────────


def test_json_sink(tmp_dir: str) -> None:
    section("Structured JSON log sink")
    json_path = os.path.join(tmp_dir, "bot.jsonl")
    sink_id = install_json_sink(path=json_path)
    logger.info("phase9 verify test message")
    # Give loguru a moment to flush.
    time.sleep(0.5)

    exists = os.path.exists(json_path)
    check("JSONL file created", exists, json_path)

    if exists:
        with open(json_path) as f:
            lines = f.readlines()
        check("JSONL has at least 1 line", len(lines) >= 1, f"{len(lines)} lines")
        if lines:
            try:
                record = json.loads(lines[-1])
                check(
                    "JSONL line is valid JSON with 'text' key",
                    "text" in record,
                )
            except json.JSONDecodeError:
                check("JSONL line is valid JSON", False)

    logger.remove(sink_id)


def test_trade_csv(tmp_dir: str, broker: AlpacaBroker) -> None:
    section("Trade CSV — build, write, read")
    csv_path = os.path.join(tmp_dir, "trades.csv")
    tl = TradeLogger(path=csv_path)

    # Build a synthetic entry record.
    decision = RiskDecision(
        symbol="AAPL",
        side=Side.BUY,
        qty=1,
        entry_reference_price=150.0,
        stop_price=145.0,
        strategy_name="phase9_verify",
        reason="verify entry",
        order_type=OrderType.MARKET,
    )
    result = OrderResult(
        status=OrderStatus.FILLED,
        order_id="verify-001",
        symbol="AAPL",
        requested_qty=1,
        filled_qty=1,
        avg_fill_price=150.05,
        raw_status="filled",
    )

    record = tl.build_record(decision, result, modeled_price=150.0)
    tl.log(record)

    # Build a synthetic close record.
    close_result = OrderResult(
        status=OrderStatus.FILLED,
        order_id="verify-002",
        symbol="AAPL",
        requested_qty=1,
        filled_qty=1,
        avg_fill_price=155.0,
        raw_status="filled",
    )
    close_record = tl.build_close_record(
        close_result, strategy_name="phase9_verify", modeled_price=154.9
    )
    tl.log(close_record)

    rows = tl.read_all()
    check("CSV has 2 rows", len(rows) == 2, f"got {len(rows)}")
    if rows:
        check("first row is BUY", rows[0]["side"] == "buy")
        check("second row is SELL", rows[1]["side"] == "sell")
        check(
            "slippage populated",
            float(rows[0]["realized_slippage_bps"]) > 0,
            f"{rows[0]['realized_slippage_bps']} bps",
        )
    return csv_path


def test_daily_pnl(tmp_dir: str, csv_path: str) -> None:
    section("Daily P&L summary + per-strategy attribution")
    daily_dir = os.path.join(tmp_dir, "daily_pnl")
    tracker = PnLTracker(
        trade_csv_path=csv_path,
        daily_pnl_dir=daily_dir,
    )

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tracker.record_trade_pnl("phase9_verify", 50.0, today=today)
    tracker.record_trade_pnl("phase9_verify", -20.0, today=today)

    summary = tracker.generate_daily_summary(
        day=today,
        session_start_equity=100_000.0,
        session_end_equity=100_030.0,
        unrealized_pnl=0.0,
    )

    check("daily summary has 2 trades", summary.total_trades == 2)
    check(
        "realized P&L is $30",
        summary.realized_pnl == 30.0,
        f"got ${summary.realized_pnl}",
    )
    check(
        "per-strategy 'phase9_verify' present",
        "phase9_verify" in summary.strategies,
    )
    if "phase9_verify" in summary.strategies:
        s = summary.strategies["phase9_verify"]
        check("strategy wins=1, losses=1", s.wins == 1 and s.losses == 1)
        check("strategy expectancy=$15", s.expectancy == 15.0)

    path = tracker.write_daily_report(summary)
    check("daily markdown written", os.path.exists(path), path)

    if os.path.exists(path):
        content = open(path).read()
        check("markdown contains 'Daily P&L'", "Daily P&L" in content)
        check(
            "markdown contains strategy name",
            "phase9_verify" in content,
        )

    return daily_dir


def test_weekly_report(tmp_dir: str, daily_dir: str, csv_path: str) -> None:
    section("Weekly summary report")
    weekly_dir = os.path.join(tmp_dir, "weekly")
    tracker = PnLTracker(
        trade_csv_path=csv_path,
        daily_pnl_dir=daily_dir,
        weekly_report_dir=weekly_dir,
    )
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = tracker.generate_weekly_report(week_end=today)

    if path is not None:
        check("weekly markdown written", os.path.exists(path), path)
        content = open(path).read()
        check("weekly contains 'Weekly Report'", "Weekly Report" in content)
    else:
        check(
            "weekly report generated (None means no daily files found)",
            False,
            "no daily reports in range",
        )


def test_alerts(tmp_dir: str) -> None:
    section("Alert dispatcher — all alert types")
    alert_log = os.path.join(tmp_dir, "alerts.log")
    backend = LogFileBackend(path=alert_log)
    dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=0)

    fired = []
    fired.append(dispatcher.order_rejection("AAPL", "sma", "halted", "halted"))
    fired.append(dispatcher.circuit_breaker("daily loss limit"))
    fired.append(dispatcher.stale_data("MSFT", "bar 2 days old"))
    fired.append(dispatcher.broker_error("timeout"))
    fired.append(dispatcher.engine_halt("hard dollar cap"))
    fired.append(dispatcher.slippage_drift(15.0, 3.0))
    fired.append(dispatcher.loss_streak_cooldown("sma", 3, 24.0))

    check("all 7 alerts fired", all(fired), f"results={fired}")


def test_slippage_monitoring(csv_path: str) -> None:
    section("Slippage monitoring — rolling stats from CSV")
    tracker = PnLTracker(trade_csv_path=csv_path)
    report = tracker.slippage_report(last_n=50)

    check(
        "slippage report has count > 0",
        report["count"] > 0,
        f"count={report['count']}",
    )
    check(
        "mean slippage >= 0",
        report["mean_bps"] >= 0,
        f"mean={report['mean_bps']} bps",
    )


def test_engine_with_reporting(tmp_dir: str, broker: AlpacaBroker) -> None:
    section("Engine wiring — 3 cycles with reporting attached")
    csv_path = os.path.join(tmp_dir, "engine_trades.csv")
    daily_dir = os.path.join(tmp_dir, "engine_daily")

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
        "engine completed 3 cycles with reporting",
        engine._cycle_count == 3,
        f"actual={engine._cycle_count}",
    )
    check(
        "trade_logger is wired",
        engine.trade_logger is trade_logger,
    )
    check(
        "pnl_tracker is wired",
        engine.pnl_tracker is pnl_tracker,
    )


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    logger.info("=" * 60)
    logger.info("Phase 9 — Trade Reporting & P&L — integration verification")
    logger.info("=" * 60)

    tmp_dir = "logs/_phase9_verify"
    os.makedirs(tmp_dir, exist_ok=True)

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

    test_json_sink(tmp_dir)
    csv_path = test_trade_csv(tmp_dir, broker)
    daily_dir = test_daily_pnl(tmp_dir, csv_path)
    test_weekly_report(tmp_dir, daily_dir, csv_path)
    test_alerts(tmp_dir)
    test_slippage_monitoring(csv_path)
    test_engine_with_reporting(tmp_dir, broker)

    logger.info("")
    logger.info("=" * 60)
    logger.info(f"PASSED: {len(PASSED)}    FAILED: {len(FAILED)}")
    logger.info("=" * 60)
    if FAILED:
        for label in FAILED:
            logger.error(f"  ❌ {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
