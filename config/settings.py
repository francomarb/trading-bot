import os
from dotenv import load_dotenv

load_dotenv("config/.env")

# ── Paper vs live environment selection (Phase 10.B1) ───────────────────────
# Set LIVE_TRADING=true only after scripts/preflight.py exits 0.
# All credential and DB routing derives from this single flag.
LIVE_TRADING: bool = os.getenv("LIVE_TRADING", "false").lower() in ("true", "1", "yes")

_ALPACA_API_KEY_PAPER: str | None = os.getenv("ALPACA_API_KEY")
_ALPACA_SECRET_KEY_PAPER: str | None = os.getenv("ALPACA_SECRET_KEY")
_ALPACA_API_KEY_LIVE: str | None = os.getenv("ALPACA_API_KEY_LIVE")
_ALPACA_SECRET_KEY_LIVE: str | None = os.getenv("ALPACA_SECRET_KEY_LIVE")

ALPACA_API_KEY: str | None = _ALPACA_API_KEY_LIVE if LIVE_TRADING else _ALPACA_API_KEY_PAPER
ALPACA_SECRET_KEY: str | None = _ALPACA_SECRET_KEY_LIVE if LIVE_TRADING else _ALPACA_SECRET_KEY_PAPER
ALPACA_PAPER: bool = not LIVE_TRADING

# Derived base URL — used only by legacy verify scripts; alpaca-py uses the
# `paper=` flag on TradingClient directly.
ALPACA_BASE_URL = (
    "https://paper-api.alpaca.markets" if ALPACA_PAPER
    else "https://api.alpaca.markets"
)

# Strategy-specific watchlists
# SMA Crossover — trend-following; static list promoted from:
#   /Users/franco/trading-bot/scripts/sma_watchlist_scan.py
#   rule=sma_watchlist_v1, feed=sip, end_delay=60m, fundamentals=True
#   generated 2026-04-26; report: logs/sma_scan_v2.md
# MU and NVDA are non-scanner exceptions, retained solely for ownership
# continuity while open paper positions exist. Remove them after the strategy
# exits if they still fail the scan.
SMA_WATCHLIST = [
    "TERN", "GOOG", "WT", "GOOGL", "TD", "IYZ", "RY", "MS",
    "CM", "JAZZ", "BK", "BMO", "WDC", "FIGS", "VLUE",
    "MU", "NVDA", "PG",
]
# RSI Reversion — mean-reversion; static list promoted from:
#   /Users/franco/trading-bot/scripts/rsi_watchlist_scan.py
#   /Users/franco/trading-bot/scripts/rsi_candidate_validate.py
#   /Users/franco/trading-bot/scripts/rsi_candidate_post_analysis.py
#   scanner_rule=rsi_watchlist_v1, validation_rule=rsi_validation_v1,
#   post_rule=rsi_post_analysis_v1, feed=sip, end_delay=60m
#   generated 2026-04-26; report: logs/rsi_post_analysis_temp_v2.md
# RSI is implemented but not active in forward_test.py yet. Keep this as the
# first paper-mode RSI pool unless the post-analysis guardrails are changed.
RSI_WATCHLIST = [
    "ALLY", "CDNS", "KBE", "SN", "DINO", "BA", "TFC", "HON", "TMUS", "JNJ",
]
# Full engine universe — union of both lists; preserves paper-run continuity.
#
# IMPORTANT: When adding new symbols to any of these watchlists, remember to also
# map them to their corresponding Sector ETF in `scripts/post_mortem.py`'s 
# SECTOR_MAP dictionary to ensure proper Relative Strength diagnostic reporting.
WATCHLIST = list(dict.fromkeys(SMA_WATCHLIST + RSI_WATCHLIST))

# ── Capital allocation (Phase 10.F1) ────────────────────────────────────────
# Per-strategy sleeve budgets. Each entry maps strategy_name →
#   weight:        fraction of gross capital for this strategy (must sum ≤ 1.0)
#   max_positions: hard cap on simultaneous open positions for this strategy
#
# Gross notional ceiling per strategy = equity × MAX_GROSS_EXPOSURE_PCT × weight
# Per-position notional budget        = ceiling / max_positions
#
# Example at $100k equity, 80% gross, 50/50, max_positions=5:
#   each sleeve = $100k × 0.80 × 0.50 = $40,000
#   per position = $40,000 ÷ 5          = $8,000
#
# Idle sleeve capital stays locked to its strategy (no cross-borrowing).
# Dynamic reallocation is a Phase 11 item.
# Weights start 50/50 — rebalance after ≥4 weeks of combined paper data.
STRATEGY_ALLOCATIONS: dict[str, dict] = {
    "sma_crossover": {"weight": 0.50, "max_positions": 5},
    "rsi_reversion":  {"weight": 0.50, "max_positions": 5},
}
MIN_TRADE_NOTIONAL = 100.0      # Reject entries if sleeve available < this

# ── Risk settings (Phase 6) ──────────────────────────────────────────────────
# Position sizing
MAX_POSITION_PCT = 0.02         # Risk no more than 2% of equity per trade (loss-to-stop)
MAX_POSITION_NOTIONAL_PCT = 0.10 # Cap one position at 10% notional so 5 can fit in 80% gross
MAX_OPEN_POSITIONS = 10         # Global cap; per-strategy limit enforced by sleeve
MAX_GROSS_EXPOSURE_PCT = 0.80   # 80% of equity tradeable across all strategies

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
# Enable the slippage-drift kill switch. Disabled by default during paper trading
# because modeled slippage has not yet been calibrated against real fills.
# Enable once you have enough paper fills to validate the threshold (≥ min_samples).
# Must be True before going live (Phase 10).
SLIPPAGE_DRIFT_ENABLED = False
# Expected execution cost for MARKET orders in bps. Matches the backtest default
# (runner.py slippage_bps=5). LIMIT orders model 0 bps (price is controlled).
SLIPPAGE_MODEL_MARKET_BPS = 5.0

# ── Live-trading safety overrides (Phase 10.G) ──────────────────────────────
# Scale calculated position sizes to this fraction when LIVE_TRADING=True.
# Default 0.25 = start live at 25% of the paper-tested size.
# Raise to 1.0 once you are confident in live execution quality.
LIVE_SIZE_MULTIPLIER: float = float(os.getenv("LIVE_SIZE_MULTIPLIER", "0.25"))

# Dry-run mode: log order decisions but do not submit to the broker.
# Useful for verifying the live environment before committing real capital.
# Set DRY_RUN=true in the environment; never rely on code-level default alone.
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

# Fractional share sizing (Phase 10.G6).
# When True: market orders use round(notional/price, 2) instead of floor(),
# and the broker submits a DAY entry + standalone GTC stop (floor(qty) whole
# shares). When False: exact current behaviour — floor() everywhere, OTO GTC
# entry+stop submitted atomically. Disable once account exceeds ~$10k and
# whole-share rounding error becomes negligible.
# Applies to MARKET orders only — LIMIT/GTC orders (RSI) always use floor().
FRACTIONAL_ENABLED: bool = True

# ── Engine settings (Phase 8 / 10) ──────────────────────────────────────────
ATR_LENGTH = 14                     # ATR window the engine uses for stops
ENGINE_TIMEFRAME = "1Day"           # Bar timeframe for the live loop
ENGINE_HISTORY_LOOKBACK_DAYS = 300  # Calendar days of stock history per cycle.
                                    # 300 cd ≈ 206 trading days — required by
                                    # SMAEdgeFilter's stock 200-day SMA gate.
ENGINE_CYCLE_INTERVAL_SECONDS = 300 # 5 min between cycles for daily strategy
ENGINE_MAX_BAR_AGE_MULTIPLIER = 4.0 # Stale guard: refuse to trade if last bar
                                    # is older than (bar_interval × multiplier)
ENGINE_MARKET_HOURS_ONLY = True     # Only trade during regular session
# Preserve OTO stop-loss legs across bot restarts. Manual liquidation paths
# explicitly cancel sibling orders before closing a position.
ENGINE_CANCEL_ORDERS_ON_SHUTDOWN = False
# Consecutive cycles a managed position must be absent from the broker before
# it is declared externally closed (stop-out / manual liquidation). Protects
# against transient API blips that return incomplete position data.
# With WebSocket streaming (Phase 10), this becomes a fallback for gap periods.
ENGINE_EXTERNAL_CLOSE_CONFIRM_CYCLES = 3

# ── Reporting settings (Phase 9) ────────────────────────────────────────────
TRADE_LOG_CSV = "logs/trades.csv"           # Legacy CSV trade log (deprecated)
TRADE_LOG_DB_PAPER = "data/trades.db"       # Paper-trading SQLite log
TRADE_LOG_DB_LIVE = "data/trades_live.db"   # Live-trading SQLite log (separate to prevent cross-contamination)
TRADE_LOG_DB = TRADE_LOG_DB_LIVE if LIVE_TRADING else TRADE_LOG_DB_PAPER
DAILY_PNL_DIR = "logs/daily_pnl"            # Daily P&L markdown summaries
WEEKLY_REPORT_DIR = "logs/weekly_reports"    # Weekly summary markdowns
JSON_LOG_FILE = "logs/bot.jsonl"            # Structured JSON log sink
ALERT_LOG_FILE = "logs/alerts.log"          # Dedicated alert log file

# ── Forward-test settings (Phase 10) ───────────────────────────────────────
FORWARD_TEST_DIR = "logs/forward_tests"          # Go/no-go decision docs
# Divergence gate: if |paper_return - backtest_return| exceeds this, no-go.
FORWARD_TEST_RETURN_DIVERGENCE_PCT = 0.10        # 10 percentage points
# Slippage gate: if mean realized slippage exceeds this, no-go.
FORWARD_TEST_MAX_SLIPPAGE_BPS = 20.0             # 20 bps mean
