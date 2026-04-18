"""
Phase 6 — Risk Management — integration verification.

Connects to the live Alpaca paper account, pulls a real bar history for AAPL,
computes ATR from it, and runs `RiskManager.evaluate` against the actual
account state. Then walks through every kill-switch and rejection path to
prove the gatekeeper behaves correctly under live-shaped inputs.

Not a substitute for `pytest tests/test_risk.py` — those are the contract.
This script proves that the contract holds end-to-end on real account /
market data.

Run: `python phase6_verify.py`
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone

import alpaca_trade_api as tradeapi
from loguru import logger

from config.settings import (
    ALPACA_API_KEY,
    ALPACA_BASE_URL,
    ALPACA_SECRET_KEY,
)
from data.fetcher import fetch_symbol
from indicators.technicals import add_atr
from risk.manager import (
    AccountState,
    Position,
    RejectionCode,
    RiskDecision,
    RiskManager,
    RiskRejection,
    Side,
    Signal,
)


# ── Logging ──────────────────────────────────────────────────────────────────


logger.remove()
logger.add(
    sys.stdout,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
    level="INFO",
)
logger.add("logs/phase6.log", rotation="1 MB", level="DEBUG")


# ── Helpers ──────────────────────────────────────────────────────────────────


PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """Assert-but-keep-going: prints PASS/FAIL and tracks results."""
    if condition:
        PASSED.append(label)
        logger.info(f"  ✅ {label}" + (f" — {detail}" if detail else ""))
    else:
        FAILED.append(label)
        logger.error(f"  ❌ {label}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    logger.info("")
    logger.info(f"── {title} ─────────────────────────────────")


# ── Live setup ──────────────────────────────────────────────────────────────


def _live_account() -> AccountState:
    """Pull real Alpaca paper account state into the AccountState shape."""
    api = tradeapi.REST(
        key_id=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        base_url=ALPACA_BASE_URL,
    )
    acct = api.get_account()
    equity = float(acct.equity)
    cash = float(acct.cash)
    positions: dict[str, Position] = {}
    for p in api.list_positions():
        positions[p.symbol] = Position(
            symbol=p.symbol,
            qty=int(float(p.qty)),
            avg_entry_price=float(p.avg_entry_price),
            market_value=float(p.market_value),
        )
    logger.info(
        f"live account: equity=${equity:,.2f}, cash=${cash:,.2f}, "
        f"open positions={len(positions)}"
    )
    return AccountState(
        equity=equity,
        cash=cash,
        session_start_equity=equity,  # treat now as session start for the test
        open_positions=positions,
    )


def _live_atr(symbol: str = "AAPL", length: int = 14) -> tuple[float, float]:
    """Fetch recent daily bars and return (latest_close, latest_atr)."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=120)
    df, stats = fetch_symbol(symbol, start, end, timeframe="1Day")
    df = add_atr(df, length)
    latest_close = float(df["close"].iloc[-1])
    latest_atr = float(df[f"atr_{length}"].iloc[-1])
    logger.info(
        f"{symbol} bars: {len(df)} rows, latest close=${latest_close:.2f}, "
        f"ATR({length})=${latest_atr:.2f} ({stats.api_calls} API call(s))"
    )
    return latest_close, latest_atr


# ── Tests ────────────────────────────────────────────────────────────────────


def test_happy_path(account: AccountState, price: float, atr: float) -> RiskDecision | None:
    section("Happy path: live account + live ATR → RiskDecision")
    mgr = RiskManager()
    sig = Signal(
        symbol="AAPL",
        side=Side.BUY,
        strategy_name="sma_crossover",
        reference_price=price,
        atr=atr,
        reason="phase6 verify happy path",
    )
    result = mgr.evaluate(sig, account)

    check("evaluate returns RiskDecision", isinstance(result, RiskDecision))
    if not isinstance(result, RiskDecision):
        logger.error(f"  rejection was: {result}")
        return None

    expected_stop = price - 2.0 * atr
    check(
        "stop = entry - 2*ATR",
        abs(result.stop_price - expected_stop) < 1e-6,
        f"stop=${result.stop_price:.2f}, expected ${expected_stop:.2f}",
    )
    check(
        "stop strictly below entry (long)",
        result.stop_price < result.entry_reference_price,
    )
    risk_dollars = result.qty * (result.entry_reference_price - result.stop_price)
    cap = account.equity * 0.02
    check(
        "$ loss-to-stop ≤ 2% of equity",
        risk_dollars <= cap + 1e-6,
        f"risk=${risk_dollars:,.2f}, cap=${cap:,.2f}",
    )
    check("qty is positive integer", isinstance(result.qty, int) and result.qty > 0)
    return result


def test_duplicate_position(account: AccountState, price: float, atr: float) -> None:
    section("Rule 6.8: duplicate position guard")
    mgr = RiskManager()
    fake = AccountState(
        equity=account.equity,
        cash=account.cash,
        session_start_equity=account.session_start_equity,
        open_positions={
            "AAPL": Position("AAPL", 1, price, price),
            **account.open_positions,
        },
    )
    result = mgr.evaluate(
        Signal("AAPL", Side.BUY, "sma_crossover", price, atr),
        fake,
    )
    check(
        "duplicate position rejected",
        isinstance(result, RiskRejection)
        and result.code is RejectionCode.DUPLICATE_POSITION,
    )


def test_max_positions(account: AccountState, price: float, atr: float) -> None:
    section("Rule 6.4: max open positions cap")
    mgr = RiskManager(max_open_positions=2)
    full = AccountState(
        equity=account.equity,
        cash=account.cash,
        session_start_equity=account.session_start_equity,
        open_positions={
            "MSFT": Position("MSFT", 1, 400.0, 400.0),
            "GOOG": Position("GOOG", 1, 140.0, 140.0),
        },
    )
    result = mgr.evaluate(
        Signal("AAPL", Side.BUY, "sma_crossover", price, atr),
        full,
    )
    check(
        "max-open-positions rejected",
        isinstance(result, RiskRejection)
        and result.code is RejectionCode.MAX_POSITIONS_REACHED,
    )


def test_daily_loss_circuit(account: AccountState, price: float, atr: float) -> None:
    section("Rule 6.5/6.6: daily loss circuit breaker + halt persistence")
    mgr = RiskManager(max_daily_loss_pct=0.05, hard_dollar_loss_cap=1e9)
    drawdown = AccountState(
        equity=account.equity * 0.94,  # down 6%
        cash=account.cash,
        session_start_equity=account.equity,
        open_positions=account.open_positions,
    )
    r1 = mgr.evaluate(
        Signal("AAPL", Side.BUY, "sma_crossover", price, atr),
        drawdown,
    )
    check(
        "daily-loss limit trips on -6%",
        isinstance(r1, RiskRejection) and r1.code is RejectionCode.DAILY_LOSS_LIMIT,
    )
    check("manager is now halted", mgr.is_halted())
    # Even a healthy account is now blocked.
    r2 = mgr.evaluate(
        Signal("MSFT", Side.BUY, "sma_crossover", price, atr),
        account,
    )
    check(
        "subsequent signals get HALTED",
        isinstance(r2, RiskRejection) and r2.code is RejectionCode.HALTED,
    )
    mgr.reset_kill_switches()
    check("reset_kill_switches clears halt", not mgr.is_halted())


def test_loss_streak_cooldown(account: AccountState, price: float, atr: float) -> None:
    section("Rule 6.9: per-strategy loss-streak cooldown")
    mgr = RiskManager(loss_streak_threshold=3, loss_streak_cooldown_hours=24)
    for _ in range(3):
        mgr.record_trade_result("sma_crossover", -100.0)
    r = mgr.evaluate(
        Signal("AAPL", Side.BUY, "sma_crossover", price, atr),
        account,
    )
    check(
        "strategy disabled after 3 losses",
        isinstance(r, RiskRejection) and r.code is RejectionCode.STRATEGY_COOLDOWN,
    )
    # Different strategy is unaffected.
    r2 = mgr.evaluate(
        Signal("AAPL", Side.BUY, "other_strategy", price, atr),
        account,
    )
    check("other strategy still allowed", isinstance(r2, RiskDecision))


def test_broker_error_streak() -> None:
    section("Rule 6.10: broker-error streak kill switch")
    mgr = RiskManager(broker_error_threshold=3, broker_error_window_seconds=60)
    now = datetime.now(timezone.utc)
    for i in range(3):
        mgr.record_broker_error(now=now + timedelta(seconds=i))
    check("kill switch engaged after 3 errors in window", mgr.is_halted())
    check(
        "halt reason mentions broker errors",
        "broker errors" in (mgr.halt_reason() or ""),
    )


def test_slippage_drift() -> None:
    section("Rule 6.11: slippage drift kill switch")
    mgr = RiskManager(slippage_min_samples=5, slippage_drift_multiplier=3.0)
    for _ in range(5):
        mgr.record_fill_slippage(modeled_bps=5.0, realized_bps=20.0)
    check("kill switch engaged on realized 20 vs modeled 5 (>3x)", mgr.is_halted())
    check(
        "halt reason mentions slippage drift",
        "slippage drift" in (mgr.halt_reason() or ""),
    )


def test_gross_exposure_cap(account: AccountState, price: float, atr: float) -> None:
    section("Rule 6.12: gross exposure cap")
    mgr = RiskManager(max_gross_exposure_pct=0.10)
    big_position = AccountState(
        equity=account.equity,
        cash=account.cash,
        session_start_equity=account.session_start_equity,
        open_positions={
            "MSFT": Position(
                "MSFT", 1, account.equity * 0.10, account.equity * 0.10
            ),
        },
    )
    r = mgr.evaluate(
        Signal("AAPL", Side.BUY, "sma_crossover", price, atr),
        big_position,
    )
    check(
        "rejected when gross exposure already at cap",
        isinstance(r, RiskRejection) and r.code is RejectionCode.GROSS_EXPOSURE_CAP,
    )


# ── Main ─────────────────────────────────────────────────────────────────────


def main() -> int:
    logger.info("=" * 60)
    logger.info("Phase 6 — Risk Management — integration verification")
    logger.info("=" * 60)

    try:
        account = _live_account()
        price, atr = _live_atr("AAPL")
    except Exception as e:
        logger.error(f"setup failed: {e}")
        return 2

    test_happy_path(account, price, atr)
    test_duplicate_position(account, price, atr)
    test_max_positions(account, price, atr)
    test_daily_loss_circuit(account, price, atr)
    test_loss_streak_cooldown(account, price, atr)
    test_broker_error_streak()
    test_slippage_drift()
    test_gross_exposure_cap(account, price, atr)

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
