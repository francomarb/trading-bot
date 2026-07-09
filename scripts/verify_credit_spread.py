"""
Integration verify for the credit-spread strategy (PLAN.md 11.29 PR 3b).

Exercises the strategy's decision pipeline against **live Alpaca paper
data** — no orders are placed. PR 2's ``scripts/verify_spread_order.py``
already proved MLEG combo submission against the paper API; this script
proves the layer on top: that the edge filter, IV proxy, and
``build_spread_execution`` resolve a real spread from the live chain.

For each configured underlying (SPY, QQQ) it:
  1. fetches real daily bars and runs ``CreditSpreadEdgeFilter`` — reports
     ALLOWED / BLOCKED + reasons;
  2. runs ``build_spread_execution`` with a realistic sleeve notional —
     reports the chosen spread or the rejection reason.

A *rejection* (thin credit, no spread near target delta, edge filter
blocked) is a valid outcome — the merge gate only fails if the pipeline
raises or returns something malformed.

Run:  python scripts/verify_credit_spread.py
Exit:  0 = pipeline healthy, 1 = a check failed.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Allow `python scripts/verify_credit_spread.py` from the repo root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger

from config import settings
from data.fetcher import fetch_symbol
from strategies.credit_spread import (
    CreditSpread,
    CreditSpreadConfig,
    CreditSpreadRejected,
    SpreadExecutionPlan,
)
from strategies.filters.credit_spread import CreditSpreadEdgeFilter
from utils.iv_proxy import IVProxyResolver
from utils.options_lookup import build_opra_quote_lookup


# ── Logging ─────────────────────────────────────────────────────────────────

logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
)
logger.add("logs/verify_credit_spread.log", rotation="1 MB", level="DEBUG")


PASSED: list[str] = []
FAILED: list[str] = []

# A plausible per-position sleeve notional (max-loss cap). With
# CREDIT_SPREAD_SLEEVE_BUDGET_PCT=0.10 on a ~$108k account the sleeve is
# ~$8.6k; max_position_pct_of_sleeve=0.40 → ~$3.4k per position.
VERIFY_NOTIONAL_CAP = 3_000.0


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


def _fetch_daily(symbol: str):
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=160)  # ≥ 60 bars for the 50-day SMA gate
    df, _ = fetch_symbol(symbol, start, end, timeframe="1Day")
    return df


def _verify_underlying(
    symbol: str,
    *,
    iv_resolver: IVProxyResolver,
    quote_lookup,
) -> None:
    section(f"{symbol} — edge filter + build_spread_execution")
    config = CreditSpreadConfig.from_dict(
        symbol, settings.CREDIT_SPREAD_INSTRUMENTS[symbol]
    )
    strategy = CreditSpread(
        config,
        edge_filter=CreditSpreadEdgeFilter(
            iv_proxy_source=config.iv_proxy_source,
            min_iv_proxy=config.min_iv_proxy,
            trend_sma_buffer_pct=config.trend_sma_buffer_pct,
            earnings_blackout_days=config.earnings_blackout_days,
            iv_resolver=iv_resolver,
        ),
        iv_resolver=iv_resolver,
        quote_lookup=quote_lookup,
    )

    # 1. Edge filter against real bars.
    try:
        df = _fetch_daily(symbol)
    except Exception as e:
        check(f"{symbol}: daily bars fetched", False, str(e))
        return
    check(f"{symbol}: daily bars fetched", not df.empty, f"{len(df)} bars")
    if df.empty:
        return

    underlying_close = float(df["close"].iloc[-1])
    try:
        _raw, signals, edge_allowed, edge_reasons = strategy.inspect_signals(
            df, symbol=symbol
        )
    except Exception as e:
        check(f"{symbol}: edge filter ran", False, str(e))
        return
    check(f"{symbol}: edge filter ran", True,
          f"edge_allowed={edge_allowed} close=${underlying_close:.2f}")
    if edge_allowed is False:
        logger.info(f"  {symbol}: edge filter BLOCKED — {list(edge_reasons)}")

    # 2. build_spread_execution against the live chain.
    try:
        plan = strategy.build_spread_execution(
            underlying_close,
            notional_cap=VERIFY_NOTIONAL_CAP,
            total_open_credit_spreads=0,
        )
    except CreditSpreadRejected as e:
        # A rejection is a valid outcome — the pipeline ran cleanly.
        logger.info(f"  {symbol}: build_spread_execution rejected — {e}")
        check(f"{symbol}: build_spread_execution pipeline healthy", True,
              "rejected (valid outcome)")
        return
    except Exception as e:
        check(f"{symbol}: build_spread_execution pipeline healthy", False, str(e))
        return

    # A plan came back — sanity-check its shape.
    ok = (
        isinstance(plan, SpreadExecutionPlan)
        and len(plan.legs) == 2
        and plan.qty >= 1
        and plan.limit_price < 0          # negative = net credit (MLEG convention)
        and plan.long_strike < plan.short_strike
        and plan.max_loss <= VERIFY_NOTIONAL_CAP
    )
    check(f"{symbol}: build_spread_execution returned a valid plan", ok)
    logger.info(
        f"  {symbol}: {plan.short_occ}/{plan.long_occ} "
        f"width=${plan.width:.0f} net_credit=${plan.net_credit:.2f}/sh "
        f"max_loss=${plan.max_loss:,.0f} limit={plan.limit_price:.2f}"
    )


def main() -> int:
    logger.info("=" * 64)
    logger.info("Credit-spread strategy integration verify (PLAN.md 11.29 PR 3b)")
    logger.info(f"paper={settings.ALPACA_PAPER}  "
                f"underlyings={list(settings.CREDIT_SPREAD_INSTRUMENTS)}")
    logger.info("=" * 64)

    if not settings.ALPACA_PAPER:
        logger.error("Refusing to run: this script must target the PAPER account.")
        return 1

    iv_resolver = IVProxyResolver()
    quote_lookup = build_opra_quote_lookup()

    for symbol in settings.CREDIT_SPREAD_INSTRUMENTS:
        try:
            _verify_underlying(
                symbol, iv_resolver=iv_resolver, quote_lookup=quote_lookup
            )
        except Exception as e:  # noqa: BLE001 — top-level guard per underlying
            check(f"{symbol}: verify ran without raising", False, str(e))

    section("summary")
    logger.info(f"passed: {len(PASSED)}   failed: {len(FAILED)}")
    if FAILED:
        for label in FAILED:
            logger.error(f"  FAIL  {label}")
        logger.error("VERIFY: FAILED")
        return 1
    logger.info("VERIFY: PASSED — credit-spread decision pipeline healthy on paper data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
