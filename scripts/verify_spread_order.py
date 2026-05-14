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
from pathlib import Path

# Allow `python scripts/verify_spread_order.py` from the repo root — put the
# project root on sys.path so `config`, `execution`, etc. import cleanly.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER
from data.fetcher import fetch_symbol
from execution.broker import AlpacaBroker, OrderStatus
from execution.options_executor import SpreadLeg
from risk.manager import Side
from utils.option_symbols import owner_key_for
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
# This is a *plumbing* check, not a strategy-tuning check — the script
# overrides the submit limit price to the full width anyway, so the picked
# spread's credit quality is irrelevant. A permissive floor just guarantees
# the picker returns *a* structurally valid pair to submit. (The production
# min_credit_pct_of_width for the credit-spread strategy is a PR 3 config
# decision; observed real ~17Δ $10-wide SPY spreads collect ~13–17% of
# width, so the design doc's 0.25 default needs revisiting there.)
VERIFY_MIN_CREDIT_PCT = 0.05


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


def _is_spy_option(symbol: str | None) -> bool:
    """True for an OCC option contract on SYMBOL (excludes the equity)."""
    return bool(symbol) and symbol != SYMBOL and owner_key_for(symbol) == SYMBOL


def _reset_lingering_spy_options(broker: AlpacaBroker) -> None:
    """
    Cancel working SPY option orders AND close SPY option positions so the
    script is idempotent across runs. Option legs are closed individually
    with market orders — robust cleanup that does not depend on having the
    original combo's leg list.
    """
    snap = broker.sync_with_broker()
    for o in snap.open_orders:
        if _is_spy_option(o.symbol):
            logger.info(f"cleanup: canceling lingering option order {o.order_id}")
            broker.cancel_order(o.order_id)
    for occ in list(snap.account.open_positions):
        if _is_spy_option(occ):
            logger.info(f"cleanup: closing lingering option position {occ}")
            try:
                broker.close_position(occ, poll_timeout=15.0)
            except Exception as e:
                logger.warning(f"cleanup: failed to close {occ}: {e}")
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
    section("pre-flight — clear lingering SPY option orders + positions")
    _reset_lingering_spy_options(broker)
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
        min_credit_pct_of_width=VERIFY_MIN_CREDIT_PCT,
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
    # Alpaca MLEG limit-price convention (confirmed by this merge gate):
    #   positive = net debit you will pay,  negative = net credit you require.
    # Demand a near-impossible credit (≈ full width) so the order sits
    # working until we cancel it — isolating "did Alpaca accept the MLEG
    # shape?" from "did it fill?". A *positive* limit here would mean "pay
    # any debit up to that amount" and fill instantly.
    unfillable_limit = -round(SPREAD_WIDTH - 0.01, 2)
    logger.info(
        f"submitting 1× combo at limit {unfillable_limit:.2f} "
        f"(demands a ${abs(unfillable_limit):.2f} credit — intentionally unfillable)"
    )
    result = broker.place_spread_order(
        legs=legs,
        qty=1,
        limit_price=unfillable_limit,
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
    # Check the pre-cancel state first: with the corrected negative limit the
    # order should still be working, not filled.
    pre_cancel_status = None
    try:
        pre = broker._api.get_order_by_id(result.order_id)
        pre_cancel_status = (
            pre.status.value if hasattr(pre.status, "value") else str(pre.status)
        )
    except Exception:
        pass
    check(
        "order is still working before cancel (negative limit did not fill)",
        pre_cancel_status not in {"filled", "partially_filled"},
        f"pre-cancel status={pre_cancel_status}",
    )

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

    # Final safety net — cancel any working SPY option order and close any
    # SPY option position (covers a freak fill despite the unfillable limit).
    section("post-run cleanup — leave the paper account flat")
    _reset_lingering_spy_options(broker)
    snap = broker.sync_with_broker()
    leftover = [s for s in snap.account.open_positions if _is_spy_option(s)]
    check("no SPY option positions remain", not leftover, f"leftover={leftover}")
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
