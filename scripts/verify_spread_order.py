"""
Integration merge gate for PLAN.md 11.28 — multi-leg (MLEG) order plumbing.

Places a real defined-risk SPY bull put credit spread on the Alpaca **paper**
account, confirms the combo order shape (order_class=MLEG, two legs), then
cancels it. Nothing is left open.

This is the one thing unit tests cannot prove: that Alpaca actually accepts
the MLEG request shape this codebase builds. PR 3 (the credit-spread
strategy) builds directly on top of this plumbing, so it must be verified
against the live paper API first.

The order is submitted asking for a net credit equal to the full strike
width — an impossible fill — so it sits working until we cancel it. The
spread itself is selected by ``find_best_put_spread`` from the real chain,
so this also smoke-tests the picker end to end against live data.

Run:  python scripts/verify_spread_order.py
Exit:  0 = all checks passed, 1 = a check failed.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timedelta, timezone

from loguru import logger

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from data.fetcher import fetch_symbol
from execution.broker import AlpacaBroker, OrderStatus
from execution.options_executor import SpreadLeg
from risk.manager import Side
from utils.options_lookup import find_best_put_spread
from utils.options_ranker import Quote


# ── Logging ─────────────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
)
logger.add("logs/verify_spread_order.log", rotation="1 MB", level="DEBUG")


PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(label)
        logger.info(f"  PASS  {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED.append(label)
        logger.error(f"  FAIL  {label}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    logger.info("")
    logger.info(f"-- {title} " + "-" * (58 - len(title)))


# ── Config ──────────────────────────────────────────────────────────────────

SYMBOL = "SPY"
SPREAD_WIDTH = 10.0
TARGET_SHORT_DELTA = 0.17
# IV proxy is PR 3 scope; the picker takes IV as a parameter, so we pass a
# plausible fixed value here purely to drive the Black-Scholes delta estimate.
IV_FOR_DELTA_ESTIMATE = 0.15
MIN_DTE = 30
MAX_DTE = 45
MAX_LOSS_PER_POSITION = 2_000.0


def _build_quote_lookup():
    """Resolve a batch of OCC symbols via Alpaca's option snapshot endpoint."""
    from alpaca.data.historical.option import OptionHistoricalDataClient
    from alpaca.data.requests import OptionSnapshotRequest

    data_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)

    def _lookup(occ_symbols: list[str]) -> dict[str, "Quote | None"]:
        if not occ_symbols:
            return {}
        try:
            snapshot = data_client.get_option_snapshot(
                OptionSnapshotRequest(symbol_or_symbols=occ_symbols)
            )
        except Exception as e:
            logger.warning(f"OPRA snapshot batch failed: {e}")
            return {occ: None for occ in occ_symbols}
        out: dict[str, "Quote | None"] = {}
        for occ in occ_symbols:
            entry = snapshot.get(occ)
            if entry is None or entry.latest_quote is None:
                out[occ] = None
                continue
            q = entry.latest_quote
            bid = float(q.bid_price)
            ask = float(q.ask_price)
            out[occ] = Quote(bid=bid, ask=ask) if bid > 0 and ask > 0 else None
        return out

    return _lookup


def _latest_close(symbol: str = SYMBOL) -> float:
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=10)
    df, _ = fetch_symbol(symbol, start, end, timeframe="1Day")
    return float(df["close"].iloc[-1])


def _reset_lingering_mleg_orders(broker: AlpacaBroker) -> None:
    """Cancel any working SPY option orders so the script is idempotent."""
    snap = broker.sync_with_broker()
    for o in snap.open_orders:
        # OCC option symbols start with the underlying ticker.
        if o.symbol and o.symbol.startswith(SYMBOL) and len(o.symbol) > len(SYMBOL):
            logger.info(f"pre-flight: canceling lingering option order {o.order_id}")
            broker.cancel_order(o.order_id)
    time.sleep(1.0)


def main() -> int:
    logger.info("=" * 64)
    logger.info("MLEG spread-order integration verify (PLAN.md 11.28 merge gate)")
    logger.info(f"paper={ALPACA_PAPER}  symbol={SYMBOL}  width=${SPREAD_WIDTH:.0f}")
    logger.info("=" * 64)

    if not ALPACA_PAPER:
        logger.error("Refusing to run: this script must target the PAPER account.")
        return 1

    broker = AlpacaBroker()

    # ── 1. Pre-flight ───────────────────────────────────────────────────────
    section("pre-flight — clear lingering SPY option orders")
    _reset_lingering_mleg_orders(broker)
    logger.info("pre-flight complete")

    # ── 2. Pick a real spread from the live chain ───────────────────────────
    section("find_best_put_spread — pick a real spread from the chain")
    underlying_price = _latest_close()
    logger.info(f"{SYMBOL} latest close = ${underlying_price:.2f}")
    pick = find_best_put_spread(
        SYMBOL,
        underlying_price,
        min_dte=MIN_DTE,
        max_dte=MAX_DTE,
        spread_width=SPREAD_WIDTH,
        target_short_delta=TARGET_SHORT_DELTA,
        iv=IV_FOR_DELTA_ESTIMATE,
        max_loss_per_position=MAX_LOSS_PER_POSITION,
        quote_lookup=_build_quote_lookup(),
    )
    check("picker returned a spread", pick is not None)
    if pick is None:
        logger.error("Cannot proceed without a spread pick — aborting.")
        return _summary()
    logger.info(
        f"picked {pick.short_occ} / {pick.long_occ} "
        f"width=${pick.width:.0f} net_credit=${pick.net_credit:.2f}/sh "
        f"max_loss=${pick.max_loss:,.0f} score={pick.score:.2f}"
    )
    check("long strike is below short strike", pick.long_strike < pick.short_strike)
    check("width matches request", abs(pick.width - SPREAD_WIDTH) <= 1.0)

    # ── 3. Submit the MLEG combo (unfillable limit → sits working) ──────────
    section("place_spread_order — submit a working MLEG combo")
    legs = [
        SpreadLeg(occ_symbol=pick.short_occ, side=Side.SELL, opening=True),
        SpreadLeg(occ_symbol=pick.long_occ, side=Side.BUY, opening=True),
    ]
    # Ask for a net credit == full width: an impossible fill, so the order
    # stays working until we cancel it. This isolates "did Alpaca accept the
    # MLEG shape?" from "did it fill?".
    unfillable_credit = round(pick.width, 2)
    logger.info(
        f"submitting 1× combo at net credit ${unfillable_credit:.2f} "
        "(intentionally unfillable)"
    )
    result = broker.place_spread_order(
        legs=legs,
        qty=1,
        limit_price=unfillable_credit,
        strategy_name="verify_spread",
    )
    logger.info(
        f"place_spread_order → status={result.status.value} "
        f"order_id={result.order_id} raw={result.raw_status} msg={result.message}"
    )
    check(
        "MLEG order accepted by Alpaca",
        result.status in {OrderStatus.ACCEPTED, OrderStatus.PENDING}
        and result.order_id is not None,
        f"status={result.status.value}",
    )
    if result.order_id is None:
        logger.error("No order id returned — cannot verify shape or cancel.")
        return _summary()

    # ── 4. Verify the combo order shape via the raw API ─────────────────────
    section("verify combo shape — order_class=MLEG, two legs")
    time.sleep(1.5)  # let Alpaca register the order
    order = None
    try:
        order = broker._api.get_order_by_id(result.order_id)
    except Exception as e:
        logger.error(f"get_order_by_id failed: {e}")
    check("order is retrievable by id", order is not None)
    if order is not None:
        order_class = getattr(order, "order_class", None)
        oc_val = order_class.value if hasattr(order_class, "value") else str(order_class)
        check("order_class is mleg", oc_val == "mleg", f"order_class={oc_val}")
        legs_out = getattr(order, "legs", None) or []
        check("combo has two legs", len(legs_out) == 2, f"legs={len(legs_out)}")
        leg_syms = {getattr(leg, "symbol", None) for leg in legs_out}
        check(
            "both picked legs present in the combo",
            {pick.short_occ, pick.long_occ}.issubset(leg_syms),
            f"legs={sorted(s for s in leg_syms if s)}",
        )

    # ── 5. Cancel and confirm ───────────────────────────────────────────────
    section("cancel — leave nothing open")
    canceled = broker.cancel_order(result.order_id)
    check("cancel_order returned True", canceled)
    time.sleep(1.5)
    final = None
    try:
        final = broker._api.get_order_by_id(result.order_id)
    except Exception as e:
        logger.warning(f"post-cancel get_order_by_id failed: {e}")
    if final is not None:
        final_status = (
            final.status.value if hasattr(final.status, "value") else str(final.status)
        )
        check(
            "order reached a terminal canceled/expired state",
            final_status in {"canceled", "expired", "pending_cancel"},
            f"status={final_status}",
        )

    # Final safety net — make sure no SPY option order is still working.
    _reset_lingering_mleg_orders(broker)
    return _summary()


def _summary() -> int:
    section("summary")
    logger.info(f"passed: {len(PASSED)}   failed: {len(FAILED)}")
    if FAILED:
        for label in FAILED:
            logger.error(f"  FAIL  {label}")
        logger.error("MERGE GATE: FAILED")
        return 1
    logger.info("MERGE GATE: PASSED — MLEG plumbing verified against Alpaca paper")
    return 0


if __name__ == "__main__":
    sys.exit(main())
