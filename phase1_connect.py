"""
Trading Bot - Phase 1: Alpaca Connection Test
============================================
This script verifies:
  1. Your API keys are working
  2. You can fetch account info (balance, buying power)
  3. You can pull a live stock quote
  4. You can fetch historical OHLCV bars

Run with: python phase1_connect.py

Make sure config/.env exists with your keys first.
"""

import sys
from datetime import datetime, timedelta
import pandas as pd
import alpaca_trade_api as tradeapi
from loguru import logger

# Load config
from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_BASE_URL

# ── Logging setup ────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stdout, format="<green>{time:HH:mm:ss}</green> | <level>{level}</level> | {message}")
logger.add("logs/phase1.log", rotation="1 MB")


def connect() -> tradeapi.REST:
    """Create and return an authenticated Alpaca REST client."""
    api = tradeapi.REST(
        key_id=ALPACA_API_KEY,
        secret_key=ALPACA_SECRET_KEY,
        base_url=ALPACA_BASE_URL,
    )
    return api


def check_account(api: tradeapi.REST) -> None:
    """Print account summary."""
    logger.info("── Account Info ──────────────────────────────")
    account = api.get_account()

    print(f"  Status        : {account.status}")
    print(f"  Buying Power  : ${float(account.buying_power):,.2f}")
    print(f"  Portfolio Val : ${float(account.portfolio_value):,.2f}")
    print(f"  Cash          : ${float(account.cash):,.2f}")
    print(f"  Day Trades    : {account.daytrade_count} (PDT limit: 3/5 days)")

    if account.status != "ACTIVE":
        logger.warning(f"Account status is {account.status} — expected ACTIVE")
    else:
        logger.success("Account is ACTIVE ✓")


def check_live_quote(api: tradeapi.REST, symbol: str = "AAPL") -> None:
    """Fetch the latest quote for a symbol."""
    logger.info(f"── Live Quote: {symbol} ──────────────────────")
    try:
        quote = api.get_latest_quote(symbol, feed="iex")
        print(f"  Symbol   : {symbol}")
        print(f"  Ask      : ${quote.ap:.2f}")
        print(f"  Bid      : ${quote.bp:.2f}")
        print(f"  Spread   : ${quote.ap - quote.bp:.4f}")
        logger.success(f"Live quote for {symbol} retrieved ✓")
    except Exception as e:
        logger.error(f"Could not fetch quote: {e}")


def check_historical_bars(api: tradeapi.REST, symbol: str = "AAPL", days: int = 10) -> pd.DataFrame:
    """Fetch recent daily OHLCV bars and return as a DataFrame."""
    logger.info(f"── Historical Bars: {symbol} (last {days} days) ──")
    end = datetime.now()
    start = end - timedelta(days=days + 5)

    bars = api.get_bars(
        symbol,
        tradeapi.TimeFrame.Day,
        start=start.strftime("%Y-%m-%d"),
        end=end.strftime("%Y-%m-%d"),
        adjustment="raw",
        feed="iex",
    ).df

    bars = bars.tail(days)
    print(bars[["open", "high", "low", "close", "volume"]].to_string())
    logger.success(f"Historical bars for {symbol} retrieved ✓")
    return bars

def check_positions(api: tradeapi.REST) -> None:
    """Show any open positions (should be empty in a fresh paper account)."""
    logger.info("── Open Positions ────────────────────────────")
    positions = api.list_positions()
    if not positions:
        print("  No open positions (expected for a fresh account)")
    else:
        for p in positions:
            print(f"  {p.symbol}: {p.qty} shares @ ${float(p.avg_entry_price):.2f} | P&L: ${float(p.unrealized_pl):.2f}")
    logger.success("Positions check complete ✓")


def main():
    logger.info("═══ Trading Bot - Phase 1 Connection Test ═══")

    # Validate keys are not placeholders
    if not ALPACA_API_KEY or ALPACA_API_KEY == "your_api_key_here":
        logger.error("API key not set. Copy config/.env.example to config/.env and add your keys.")
        sys.exit(1)

    try:
        api = connect()
        logger.success("Connected to Alpaca ✓")
    except Exception as e:
        logger.error(f"Connection failed: {e}")
        sys.exit(1)

    check_account(api)
    check_live_quote(api, symbol="AAPL")
    check_historical_bars(api, symbol="AAPL", days=5)
    check_positions(api)

    logger.info("═══ Phase 1 Complete — all checks passed ✓ ═══")
    logger.info("Next: Phase 2 — build the data pipeline")


if __name__ == "__main__":
    main()
