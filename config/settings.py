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

# Data feed selection (Phase 10)
# Use 'sip' only if you pay for the $99/mo Algo Trader Plus subscription.
# Otherwise, 'iex' is the required free tier feed for real-time market data.
ALPACA_DATA_FEED: str = os.getenv("ALPACA_DATA_FEED", "iex").lower()

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
# RSI Reversion — mean-reversion; promoted from the 2026-04-30 expanded
# backtest pass to increase signal density for the static paper-trading pool.
# This list intentionally favors breadth over the earlier narrow scanner
# snapshot so the RSI sleeve can accumulate enough trades for evaluation.
RSI_WATCHLIST = [
    "ALLY", "CDNS", "KBE", "SN", "BA", "TFC", "HON", "TMUS", "JNJ",
    "CCK", "ABNB", "PG", "SPG", "MA", "LMT", "MCD", "AAPL", "ANET",
    "CAT", "CIEN", "MCO", "AMZN", "EQIX", "RTX", "META", "HD",
    "SOFI", "ARM",
]
# Bollinger Squeeze (TTM-style volatility breakout) — IMPLEMENTED BUT NOT
# ACTIVE. Cross-universe research (docs/bollinger_squeeze_universe_research.md)
# concluded sector ETFs are the optimal universe (Sharpe +0.22, MeanDD -7.7%
# over 4y) — far better than the original AI/BigTech thesis which produced a
# negative Sharpe. The strategy is parked in a "ready to activate" state:
# this watchlist + the strategy code are correct for eventual deployment.
#
# To activate later: add a third StrategySlot in forward_test.py wired with
#   BollingerSqueeze(bb_length=10, kc_length=10, min_squeeze_bars=6, roc_lookback=5,
#                    edge_filter=BollingerSqueezeEdgeFilter())
# and decide a sleeve weight (the strategy is a low-DD diversifier, not a
# return generator — keep weight small).
BOLLINGER_WATCHLIST = [
    "XLF",   # Financials
    "XLE",   # Energy
    "XLU",   # Utilities
    "XLV",   # Healthcare
    "XLI",   # Industrials
    "XLK",   # Technology
    "XLP",   # Consumer Staples
    "XLY",   # Consumer Discretionary
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLC",   # Communications
]
# Donchian Breakout (Turtle System 1) — IMPLEMENTED, NOT YET WIRED into
# forward_test.py. Trend-continuation strategy designed to capture relentless
# uptrends in AI / Big-Tech / Semis (the user's directional thesis universe).
# Activation gate: Sharpe ≥ +0.4, ≥ 50 trades, MeanDD ≤ 25% on AI/BigTech
# backtest with 2× ATR stops. Until that gate passes, strategy is parked
# (same pattern as BollingerSqueeze).
#
# This list is the PROPOSED initial universe; user will review/edit before
# any backtest is run. See docs/donchian_breakout_strategy.md once written.
DONCHIAN_WATCHLIST = [
    # AI / Semis (primary)
    "NVDA", "AMD", "AVGO", "SMCI", "TSM", "MU", "QCOM", "ARM", "MRVL",
    # AI infrastructure / data-centre buildout
    "ANET", "VRT",
    # Big Tech
    "MSFT", "AAPL", "GOOGL", "META", "AMZN", "ORCL", "TSLA",
    # AI software (secondary)
    "PLTR", "CRWD", "NOW",
    # AI compute / quantum (post-IPO names with full 4y history)
    "IREN", "IONQ",
    # AI-adjacent: semiconductor equipment, networking, data-centre power,
    # quantum computing — highly correlated with AI core but add breadth
    "ASML",   # Semiconductor lithography — only supplier of EUV, AI capex pick-and-shovel
    "CLS",    # Celestica — contract mfg for hyperscaler AI networking hardware
    "CIEN",   # Ciena — optical networking; direct beneficiary of AI data-centre traffic
    "CEG",    # Constellation Energy — nuclear power for AI data-centre load growth
    "VST",    # Vistra — power generation; same AI-electricity demand thesis as CEG
    "BE",     # Bloom Energy — fuel-cell backup power; AI data-centre resilience play
    "PWR",    # Quanta Services — electrical infrastructure buildout for AI campuses
    "RGTI",   # Rigetti Computing — quantum hardware; early-stage AI compute adjacency
    "QBTS",   # D-Wave Quantum — quantum annealing; same early-stage bet as RGTI
]
# Full engine universe — union of all lists; preserves paper-run continuity.
#
# IMPORTANT: When adding new symbols to any of these watchlists, remember to also
# map them to their corresponding Sector ETF in `scripts/post_mortem.py`'s
# SECTOR_MAP dictionary to ensure proper Relative Strength diagnostic reporting.
WATCHLIST = list(dict.fromkeys(
    SMA_WATCHLIST + RSI_WATCHLIST + BOLLINGER_WATCHLIST + DONCHIAN_WATCHLIST
))

# ── Per-strategy dashboard metadata ─────────────────────────────────────────
# Maps strategy_name → watchlist and allowed market regimes.
# Add a new strategy here (one entry) and the dashboard picks it up automatically.
STRATEGY_WATCHLISTS: dict[str, list[str]] = {
    "sma_crossover":      SMA_WATCHLIST,
    "rsi_reversion":      RSI_WATCHLIST,
    "bollinger_squeeze":  BOLLINGER_WATCHLIST,
    "donchian_breakout":  DONCHIAN_WATCHLIST,
}
REGIME_MAX_CONSECUTIVE_FAILURES: int = 3

# ── Sector Momentum Gauge ──────────────────────────────────────────────────
# Normalized sector label → sector ETF ticker.  Used by SectorMomentumGauge
# to compute per-sector heat scores (HOT / NEUTRAL / COLD).
# Manual sector overrides — take precedence over yfinance/cache.
# Use for GICS reclassifications that don't match trading behaviour
# (e.g. Alphabet and Meta moved to Communication Services in 2018 but
# trade as technology names).
SYMBOL_SECTOR_OVERRIDES: dict[str, str] = {
    "GOOG":  "technology",
    "GOOGL": "technology",
    "META":  "technology",
}

SECTOR_ETFS: dict[str, str] = {
    "technology":     "XLK",
    "semiconductors": "SMH",
    "financials":     "XLF",
    "energy":         "XLE",
    "utilities":      "XLU",
    "healthcare":     "XLV",
    "industrials":    "XLI",
    "staples":        "XLP",
    "discretionary":  "XLY",
    "materials":      "XLB",
    "real_estate":    "XLRE",
    "communications": "XLC",
}

STRATEGY_ALLOWED_REGIMES: dict[str, set[str]] = {
    "sma_crossover":     {"TRENDING", "RANGING"},
    "rsi_reversion":     {"TRENDING", "RANGING"},
    # Squeeze fires best after compression breaks (TRENDING) or during it (RANGING).
    "bollinger_squeeze": {"TRENDING", "RANGING"},
    # Donchian whipsaws hard in RANGING regimes (every 20-day high gets faded).
    # Restrict to TRENDING only — academic literature is unanimous on this.
    "donchian_breakout": {"TRENDING"},
}

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
# Weights: SMA 0.50 / RSI 0.25 / Donchian 0.25 — sum = 1.0.
# RSI reduced from 0.50 after paper observation: ~8 trades over 4y means the
# full 0.50 sleeve was mostly idle. Donchian gets 0.25 following backtest
# validation (Sharpe +0.80 on AI/Bigtech, Mid-range 30/15 variant).
STRATEGY_ALLOCATIONS: dict[str, dict] = {
    "sma_crossover":    {"weight": 0.50, "max_positions": 5},
    "rsi_reversion":    {"weight": 0.25, "max_positions": 5},
    "donchian_breakout": {"weight": 0.25, "max_positions": 5},
}
MIN_TRADE_NOTIONAL = 100.0      # Reject entries if sleeve available < this

# Strategy-level high-water-mark drawdown gate (SleeveAllocator).
# If a strategy's cumulative realized P&L drops more than this fraction
# below its peak (HWM), new entries for that strategy are paused until
# P&L recovers. Set to 0.0 to disable. Exits are never blocked.
#   Example at $100k equity, Donchian weight 0.25:
#   sleeve budget = $100k × 0.80 × 0.25 = $20k
#   gate fires when realized PnL < HWM − 0.15 × $20k = HWM − $3k
STRATEGY_SLEEVE_DD_THRESHOLD = 0.15

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

# ── Telegram / messaging (Phase 11.13) ──────────────────────────────────────
# Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config/.env to enable.
# Leave blank (default) to disable Telegram entirely — bot runs fine without it.
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
# Set TELEGRAM_COMMANDS_ENABLED=true to enable /status and /halt commands.
# Requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to be set.
TELEGRAM_COMMANDS_ENABLED: bool = (
    os.getenv("TELEGRAM_COMMANDS_ENABLED", "false").lower() in ("true", "1", "yes")
)

# ── Dashboard (Phase 11.14) ──────────────────────────────────────────────────
# Path where the engine writes a JSON state snapshot each cycle.
# The Streamlit dashboard reads this file to show live bot status.
STATE_SNAPSHOT_PATH: str = "data/engine_state.json"
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8501"))
