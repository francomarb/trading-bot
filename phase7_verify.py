"""
Phase 7 — Broker Integration & Order Execution — integration verification.

Drives `AlpacaBroker` end-to-end against the live Alpaca paper account:

  1. sync_with_broker → snapshot of account / positions / open orders
  2. **Risk-gate enforcement** — place_order rejects a non-RiskDecision arg
  3. **place_order(decision)** for 1 share AAPL via a real RiskManager-built
     RiskDecision; expect FILLED with avg_fill_price set, then a stop_loss
     leg active in open_orders
  4. **cancel_order** — submit a far-from-market limit, then cancel it
  5. **close_position** — clean up the AAPL position (uses MARKET)

Pre-flight: any pre-existing AAPL position or AAPL order is force-cleared
first so the script is idempotent across runs.

Run: `python phase7_verify.py`
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

from data.fetcher import fetch_symbol
from execution.broker import AlpacaBroker, OrderStatus
from indicators.technicals import add_atr
from risk.manager import (
    RiskDecision,
    RiskManager,
    Side,
    Signal,
)
from strategies.base import OrderType


# ── Logging ──────────────────────────────────────────────────────────────────


logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
)
logger.add("logs/phase7.log", rotation="1 MB", level="DEBUG")


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


# ── Helpers ──────────────────────────────────────────────────────────────────


SYMBOL = "AAPL"


def _latest_close_and_atr(symbol: str = SYMBOL, length: int = 14) -> tuple[float, float]:
    """Pull recent daily bars to derive a sensible reference price + ATR."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=120)
    df, _ = fetch_symbol(symbol, start, end, timeframe="1Day")
    df = add_atr(df, length)
    return float(df["close"].iloc[-1]), float(df[f"atr_{length}"].iloc[-1])


def _reset_paper_state(broker: AlpacaBroker) -> None:
    """Cancel any open AAPL orders and close any open AAPL position."""
    snap = broker.sync_with_broker()
    for o in snap.open_orders:
        if o.symbol == SYMBOL:
            logger.info(f"pre-flight: canceling lingering order {o.order_id}")
            broker.cancel_order(o.order_id)
    if SYMBOL in snap.account.open_positions:
        logger.info(f"pre-flight: closing lingering {SYMBOL} position")
        broker.close_position(SYMBOL, poll_timeout=20.0)
    # Brief pause for Alpaca to settle.
    time.sleep(1.0)


# ── Tests ────────────────────────────────────────────────────────────────────


def test_sync_with_broker(broker: AlpacaBroker) -> None:
    section("sync_with_broker — broker is the source of truth")
    snap = broker.sync_with_broker()
    check(
        "snapshot has positive equity",
        snap.account.equity > 0,
        f"equity=${snap.account.equity:,.2f}",
    )
    check(
        "snapshot has cash >= 0",
        snap.account.cash >= 0,
        f"cash=${snap.account.cash:,.2f}",
    )
    check(
        "open_orders is a list",
        isinstance(snap.open_orders, list),
        f"{len(snap.open_orders)} open order(s)",
    )


def test_risk_gate_enforcement(broker: AlpacaBroker) -> None:
    section("Risk-gate enforcement — place_order rejects non-RiskDecision")
    try:
        broker.place_order({"symbol": SYMBOL, "qty": 1})  # type: ignore[arg-type]
        check("dict input rejected", False, "no TypeError raised")
    except TypeError as e:
        check("dict input rejected with TypeError", "RiskDecision" in str(e))


def _build_one_share_decision(
    broker: AlpacaBroker, price: float, atr: float
) -> RiskDecision | None:
    """
    Use a real RiskManager but tighten max_position_pct so the sized qty
    rounds down to 1 share — keeps the verify cheap and idempotent.
    Math: risk_dollars = equity * pct ; qty = floor(risk_dollars / (k*ATR)).
    Pick pct so qty == 1.
    """
    snap = broker.sync_with_broker()
    equity = snap.account.equity
    stop_distance = 2.0 * atr
    # Target risk_dollars in [stop_distance, 2*stop_distance) → qty == 1
    target_risk = stop_distance * 1.5
    pct = target_risk / equity
    if not (0 < pct < 1):
        logger.error(f"could not derive a valid pct (equity={equity}, atr={atr})")
        return None

    mgr = RiskManager(max_position_pct=pct)
    sig = Signal(
        symbol=SYMBOL,
        side=Side.BUY,
        strategy_name="phase7_verify",
        reference_price=price,
        atr=atr,
        reason="phase7 1-share sanity order",
        order_type=OrderType.MARKET,
    )
    result = mgr.evaluate(sig, snap.account)
    if not isinstance(result, RiskDecision):
        logger.error(f"RiskManager rejected our test signal: {result}")
        return None
    if result.qty != 1:
        logger.warning(
            f"sized qty was {result.qty}, expected 1 — "
            f"verify will still run but uses {result.qty} shares"
        )
    return result


def test_place_order_market(broker: AlpacaBroker, price: float, atr: float) -> None:
    section("place_order(market) → FILLED with stop_loss attached")
    decision = _build_one_share_decision(broker, price, atr)
    if decision is None:
        check("RiskManager produced a decision", False)
        return
    check(
        "RiskManager produced a RiskDecision",
        isinstance(decision, RiskDecision),
        f"qty={decision.qty}, stop=${decision.stop_price:.2f}",
    )

    result = broker.place_order(decision, poll_timeout=30.0, poll_interval=1.0)
    check(
        "order reached terminal status",
        result.is_terminal,
        f"status={result.status.value}",
    )
    check(
        "order FILLED",
        result.status is OrderStatus.FILLED,
        f"filled {result.filled_qty}/{result.requested_qty} @ "
        f"${result.avg_fill_price}",
    )
    check(
        "filled_qty matches requested_qty",
        result.filled_qty == result.requested_qty,
    )
    check(
        "avg_fill_price is populated",
        result.avg_fill_price is not None and result.avg_fill_price > 0,
    )

    # Give Alpaca a beat to register the OTO stop_loss leg as a child order.
    time.sleep(2.0)
    snap = broker.sync_with_broker()
    check(
        f"position open for {SYMBOL}",
        SYMBOL in snap.account.open_positions,
    )
    if SYMBOL in snap.account.open_positions:
        pos = snap.account.open_positions[SYMBOL]
        check(
            "position qty matches fill",
            pos.qty == result.filled_qty,
            f"qty={pos.qty}",
        )

    stop_orders = [
        o for o in snap.open_orders
        if o.symbol == SYMBOL and o.stop_price is not None
    ]
    check(
        "OTO stop_loss leg is live as an open order",
        len(stop_orders) >= 1,
        f"{len(stop_orders)} stop order(s) at "
        f"{[o.stop_price for o in stop_orders]}",
    )


def test_cancel_order(broker: AlpacaBroker, price: float, atr: float) -> None:
    section("cancel_order — submit far limit, then cancel")
    # Limit way below market so it won't fill before we cancel.
    far_limit = round(price * 0.50, 2)
    stop = round(far_limit * 0.95, 2)
    snap = broker.sync_with_broker()

    # Use a different symbol so the duplicate-position guard doesn't block us
    # (we still hold AAPL from the previous test).
    test_symbol = "MSFT"
    msft_close, msft_atr = _latest_close_and_atr(test_symbol)
    msft_far_limit = round(msft_close * 0.50, 2)
    msft_stop = round(msft_far_limit * 0.95, 2)

    decision = RiskDecision(
        symbol=test_symbol,
        side=Side.BUY,
        qty=1,
        entry_reference_price=msft_far_limit,
        stop_price=msft_stop,
        strategy_name="phase7_verify",
        reason="phase7 cancel test",
        order_type=OrderType.LIMIT,
        limit_price=msft_far_limit,
    )

    # Don't poll long — we want the order live, not filled.
    result = broker.place_order(decision, poll_timeout=2.0, poll_interval=0.5)
    check(
        "limit order accepted (not filled, since far below market)",
        result.order_id is not None
        and result.status in {OrderStatus.TIMEOUT, OrderStatus.PENDING, OrderStatus.ACCEPTED},
        f"status={result.status.value}",
    )
    if result.order_id is None:
        return

    canceled = broker.cancel_order(result.order_id)
    check("cancel_order returned True", canceled)

    time.sleep(1.5)
    snap = broker.sync_with_broker()
    still_open = [o for o in snap.open_orders if o.order_id == result.order_id]
    check(
        "order no longer in open_orders",
        len(still_open) == 0,
    )


def test_close_position(broker: AlpacaBroker) -> None:
    section("close_position — liquidate AAPL with market order (cleanup)")
    snap = broker.sync_with_broker()
    if SYMBOL not in snap.account.open_positions:
        logger.warning(f"no {SYMBOL} position to close — skipping")
        return
    result = broker.close_position(SYMBOL, poll_timeout=30.0)
    check(
        "close_position reached terminal",
        result.is_terminal,
        f"status={result.status.value}",
    )
    check(
        "position closed",
        result.status is OrderStatus.FILLED,
        f"filled {result.filled_qty} @ ${result.avg_fill_price}",
    )

    time.sleep(2.0)
    snap = broker.sync_with_broker()
    check(
        f"{SYMBOL} no longer in open_positions",
        SYMBOL not in snap.account.open_positions,
    )


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    logger.info("=" * 60)
    logger.info("Phase 7 — Broker Integration — integration verification")
    logger.info("=" * 60)

    try:
        broker = AlpacaBroker()
        price, atr = _latest_close_and_atr()
        logger.info(f"{SYMBOL} latest close=${price:.2f}, ATR(14)=${atr:.2f}")
        _reset_paper_state(broker)
    except Exception as e:
        logger.exception(f"setup failed: {e}")
        return 2

    test_sync_with_broker(broker)
    test_risk_gate_enforcement(broker)
    test_place_order_market(broker, price, atr)
    test_cancel_order(broker, price, atr)
    test_close_position(broker)

    # Final cleanup — leave the account exactly how we found it.
    try:
        _reset_paper_state(broker)
    except Exception as e:
        logger.warning(f"final cleanup hiccup: {e}")

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
