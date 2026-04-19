import os
from dotenv import load_dotenv

load_dotenv("config/.env")

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
ALPACA_PAPER = os.getenv("ALPACA_PAPER", "true").lower() in ("true", "1", "yes")

# Derived base URL — used only by legacy verify scripts; alpaca-py uses the
# `paper=` flag on TradingClient directly.
ALPACA_BASE_URL = (
    "https://paper-api.alpaca.markets" if ALPACA_PAPER
    else "https://api.alpaca.markets"
)

# Trading universe
WATCHLIST = [
    # Original large-cap tech
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA",
    # Near or at SMA(20,50) crossover — added for forward-test signal coverage
    "JPM", "DAL", "BAC", "GS", "PINS", "UBER", "COIN", "RIVN",
]

# ── Risk settings (Phase 6) ──────────────────────────────────────────────────
# Position sizing
MAX_POSITION_PCT = 0.02         # Risk no more than 2% of equity per trade (loss-to-stop)
MAX_OPEN_POSITIONS = 5          # Cap concurrent open positions
MAX_GROSS_EXPOSURE_PCT = 0.50   # Cap total gross notional at 50% of equity (initial live)

# Stop-loss
ATR_STOP_MULTIPLIER = 2.0       # Stop = entry - k * ATR (long); always defined pre-entry

# Daily / hard kill switches
MAX_DAILY_LOSS_PCT = 0.05       # Halt for the session if equity down 5% from session start
HARD_DOLLAR_LOSS_CAP = 2_000.0  # Absolute $ loss cap from session start; CRITICAL halt

# Loss-streak cooldown (per strategy)
LOSS_STREAK_THRESHOLD = 3       # Disable strategy after N consecutive losing trades
LOSS_STREAK_COOLDOWN_HOURS = 24 # ... for this many hours

# Broker-error-streak kill switch
BROKER_ERROR_STREAK_THRESHOLD = 5    # Halt all trading if N broker errors ...
BROKER_ERROR_WINDOW_SECONDS = 300    # ... within this rolling window (5 min)

# Slippage-drift kill switch
SLIPPAGE_DRIFT_MIN_SAMPLES = 10      # Need at least this many fills before judging
SLIPPAGE_DRIFT_MULTIPLIER = 3.0      # Halt if mean realized slippage > k * mean modeled

# ── Engine settings (Phase 8) ────────────────────────────────────────────────
ATR_LENGTH = 14                     # ATR window the engine uses for stops
ENGINE_TIMEFRAME = "1Day"           # Bar timeframe for the live loop
ENGINE_HISTORY_LOOKBACK_DAYS = 200  # How much history to keep loaded per cycle
ENGINE_CYCLE_INTERVAL_SECONDS = 300 # 5 min between cycles for daily strategy
ENGINE_MAX_BAR_AGE_MULTIPLIER = 2.5 # Stale guard: refuse to trade if last bar
                                    # is older than (bar_interval × multiplier)
ENGINE_MARKET_HOURS_ONLY = True     # Only trade during regular session
ENGINE_CANCEL_ORDERS_ON_SHUTDOWN = True  # Tidy on SIGINT

# ── Reporting settings (Phase 9) ────────────────────────────────────────────
TRADE_LOG_CSV = "logs/trades.csv"           # Legacy CSV trade log (deprecated)
TRADE_LOG_DB = "data/trades.db"             # SQLite trade log
DAILY_PNL_DIR = "logs/daily_pnl"            # Daily P&L markdown summaries
WEEKLY_REPORT_DIR = "logs/weekly_reports"    # Weekly summary markdowns
JSON_LOG_FILE = "logs/bot.jsonl"            # Structured JSON log sink
ALERT_LOG_FILE = "logs/alerts.log"          # Dedicated alert log file

# ── Forward-test settings (Phase 9.5) ──────────────────────────────────────
FORWARD_TEST_DIR = "logs/forward_tests"          # Go/no-go decision docs
# Divergence gate: if |paper_return - backtest_return| exceeds this, no-go.
FORWARD_TEST_RETURN_DIVERGENCE_PCT = 0.10        # 10 percentage points
# Slippage gate: if mean realized slippage exceeds this, no-go.
FORWARD_TEST_MAX_SLIPPAGE_BPS = 20.0             # 20 bps mean
