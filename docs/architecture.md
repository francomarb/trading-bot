# Trading Bot — Architecture Guide

## Overview

This document defines the target architecture for the Alpaca trading bot. It is the source of truth for structural decisions, coding conventions, and the go/no-go framework for live capital deployment. All refactoring and new development should align with this guide.

The bot is built in Python using `alpaca-py`, currently paper trading the SMA crossover strategy. RSI mean-reversion is implemented and backtested but not yet active — it will be added after SMA paper stabilization and Phase 10 safety work. The final live GO/NO-GO gate is the combined SMA + RSI paper run after Phase 10 is implemented.

---

## Layered Architecture

The bot is organized into six layers. Each layer has a single responsibility and communicates only with the layer directly above or below it.

```
┌─────────────────────────────────────────┐
│           Engine (trader.py)            │  Orchestrates the live loop
├─────────────────────────────────────────┤
│              Data Layer                 │  Historical bars, freshness checks
├─────────────────────────────────────────┤
│     Indicators + Strategy Interface     │  Technical indicators, BaseStrategy
├─────────────────────────────────────────┤
│             Risk Manager                │  Position sizing, drawdown limits, stop-loss
├─────────────────────────────────────────┤
│         Execution Layer (alpaca-py)     │  TradingClient, order management, paper↔live
├─────────────────────────────────────────┤
│         Reporting & Monitoring          │  Trade log (SQLite), PnL, metrics, alerts
└─────────────────────────────────────────┘
```

At the signal level, strategy entries pass through a narrower trade-permission
pipeline:

```text
Watchlist -> raw strategy signal -> edge filter confirms/rejects -> risk -> execution
```

The strategy owns setup detection. The edge filter owns current-condition
permission. Risk owns sizing, stops, exposure limits, and kill switches.
Execution only places orders after risk approval.

---

## Project Structure

```
trading-bot/
│
├── CLAUDE.md                  # Claude Code session context
├── architecture.md            # This file
├── PLAN.md                    # Phased build plan and progress tracker
├── requirements.txt           # Pinned dependencies
├── main.py                    # Entry point (delegates to forward_test.py)
├── forward_test.py            # Launches engine for multi-week paper runs
├── start_bot.sh               # tmux + caffeinate launcher
│
├── config/
│   ├── .env                   # API keys (never committed)
│   └── settings.py            # Centralized config (symbols, risk params, etc.)
│
├── data/
│   ├── __init__.py
│   ├── fetcher.py             # Fetches OHLCV bars via StockHistoricalDataClient
│   ├── trades.db              # SQLite trade log (gitignored)
│   └── historical/            # Cached historical bars (Parquet, gitignored)
│
├── indicators/
│   ├── __init__.py
│   └── technicals.py          # SMA, EMA, ATR, RSI (hand-rolled, no pandas-ta)
│
├── strategies/
│   ├── __init__.py
│   ├── base.py                # BaseStrategy, SignalFrame, StrategySlot, Scanner
│   ├── sma_crossover.py       # Trend-following: SMA crossover
│   └── rsi_reversion.py       # Mean-reversion: RSI oversold/overbought
│
├── engine/
│   ├── __init__.py
│   └── trader.py              # TradingEngine — the live loop orchestrator
│
├── risk/
│   ├── __init__.py
│   └── manager.py             # RiskManager class
│
├── execution/
│   ├── __init__.py
│   └── broker.py              # AlpacaBroker — TradingClient wrapper
│
├── reporting/
│   ├── __init__.py
│   ├── logger.py              # TradeLogger — writes to SQLite (data/trades.db)
│   ├── metrics.py             # Computes Sharpe, drawdown, profit factor, win rate
│   ├── pnl.py                 # PnLTracker — daily/weekly P&L reports
│   └── alerts.py              # AlertDispatcher with pluggable backends
│
├── backtest/
│   ├── __init__.py
│   ├── runner.py              # vectorbt backtesting harness
│   └── reconcile.py           # Forward-test reconciliation (paper vs backtest)
│
├── scripts/
│   ├── __init__.py
│   └── gonogo.py              # Go/no-go checker for live readiness
│
├── tests/
│   ├── conftest.py            # Shared fixtures (make_ohlcv, tmp_cache_dir, etc.)
│   ├── test_strategies.py
│   ├── test_technicals.py
│   ├── test_risk.py
│   ├── test_broker.py
│   ├── test_engine.py
│   ├── test_fetcher.py
│   ├── test_backtest.py
│   ├── test_reporting.py
│   ├── test_reconcile.py
│   ├── test_metrics.py
│   └── test_gonogo.py
│
├── logs/                      # Rotating log files (gitignored)
├── phase*_verify.py           # Integration verification scripts per phase
└── .gitignore
```

---

## Layer Specifications

### 1. Data Layer

Responsibilities:
- Fetch historical OHLCV bars for backtesting and signal generation
- Enforce data freshness — stale bars are rejected (configurable multiplier)
- Cache bars locally to reduce API calls
- Respect market hours via the Alpaca market clock API

Key rules:
- Use `StockHistoricalDataClient` for historical data
- Market clock must be checked before any order is placed
- Data fetching is the only layer allowed to call Alpaca market data endpoints
- Retry logic with exponential backoff on transient errors (429, 5xx)

### 2. Indicators

Hand-rolled technical indicators in `indicators/technicals.py`. Each function is pure (returns a new DataFrame, never mutates input) with predictable column names (`sma_{length}`, `rsi_{length}`, etc.).

Currently provided:
- `add_sma(df, length)` — Simple Moving Average
- `add_ema(df, length)` — Exponential Moving Average (Wilder seed)
- `add_atr(df, length)` — Average True Range (Wilder/RMA)
- `add_rsi(df, length)` — Relative Strength Index (Wilder/RMA)

Design decision: indicators are hand-rolled (~5-10 lines each) rather than using pandas-ta. This eliminates a dependency risk on a library with pandas 2.x breakage history, on a code path that will manage real money.

### 3. Strategy Interface

Every strategy conforms to the `BaseStrategy` interface. The engine never needs to know which strategy is running.

**BaseStrategy contract (`strategies/base.py`):**

```python
class BaseStrategy(ABC):
    name: str                                    # Class attribute, unique identifier
    preferred_order_type: OrderType = OrderType.MARKET

    def __init__(self, *, edge_filter: EdgeFilter | None = None): ...

    @abstractmethod
    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame: ...

    def generate_signals(self, df: pd.DataFrame) -> SignalFrame: ...

    def required_bars(self) -> int:
        """Minimum bars needed for a valid signal. Default: 50."""
        return 50
```

**SignalFrame contract:**
- `entries`: boolean Series — True on bars where a new long position should open
- `exits`: boolean Series — True on bars where an open position should close
- Both share the same DatetimeIndex as the input bars
- Signals at bar t depend only on data up to and including t (no look-ahead)
- The engine shifts execution to bar t+1's open

**Edge filter contract:**
- A strategy may be constructed with `edge_filter(df) -> pd.Series[bool]`
- `_raw_signals(df)` computes the strategy's unfiltered setup first
- `generate_signals(df)` AND-gates entries with the edge filter
- Exits are never blocked by the edge filter
- Missing, NaN, or false filter values block entries by default
- Use edge filters for confirmation or veto rules such as market regime,
  symbol trend, volatility gates, MACD confirmation, or EMA5/EMA10 confirmation
- Do not use edge filters for universe selection; scanners/watchlist sources own
  symbol selection

**StrategySlot:**
Each slot binds a strategy to its symbol universe and timeframe. The engine iterates over slots each cycle. An optional `Scanner` can refresh symbols dynamically.

**Current strategies:**

| Strategy | File | Status | Order Type | Signal logic |
|---|---|---|---|---|
| SMA Crossover | `sma_crossover.py` | **Active (paper trading)** | MARKET | Fast SMA crosses above/below slow SMA |
| RSI Reversion | `rsi_reversion.py` | Implemented, not yet active | LIMIT | RSI crosses below oversold / above overbought |

**Why SMA + RSI complement each other (planned):**
SMA crossover is a trend-following strategy — it performs well in trending markets but suffers in sideways/ranging conditions. RSI mean reversion is the opposite — it performs well when prices oscillate in a range. Running both simultaneously will provide natural regime diversification. RSI Reversion will be added to `forward_test.py` as a second `StrategySlot` after SMA paper stabilization and Phase 10 safety work.

### 4. Risk Manager

The Risk Manager sits between strategy signals and the execution layer. No order reaches the broker without passing through it.

**Responsibilities:**
- Validate signals against current portfolio state
- Enforce ATR-based position sizing (risk no more than 2% of equity per trade)
- Enforce daily loss limits (halt bot if breached)
- Apply stop-loss levels to every order (ATR-based)
- Track current exposure per symbol and overall
- Loss-streak cooldown per strategy
- Broker-error-streak kill switch
- Slippage-drift kill switch

**Key risk parameters (from `config/settings.py`):**

| Rule | Description | Default |
|---|---|---|
| `MAX_POSITION_PCT` | Max % of equity risked per trade (loss-to-stop) | 2% |
| `MAX_OPEN_POSITIONS` | Max concurrent open positions | 5 |
| `MAX_GROSS_EXPOSURE_PCT` | Max total gross notional as % of equity | 50% |
| `MAX_DAILY_LOSS_PCT` | Halt for the session if equity down this much | 5% |
| `HARD_DOLLAR_LOSS_CAP` | Absolute $ loss cap from session start | $2,000 |
| `ATR_STOP_MULTIPLIER` | Stop = entry - k × ATR | 2.0 |
| `LOSS_STREAK_THRESHOLD` | Disable strategy after N consecutive losses | 3 |

### 5. Execution Layer

A thin wrapper (`AlpacaBroker`) around `alpaca-py`'s `TradingClient`. Translates approved risk decisions into actual orders.

**Key rules:**
- Paper vs live is controlled **only** by the `ALPACA_PAPER` environment variable
- `TradingClient(api_key=, secret_key=, paper=ALPACA_PAPER)`
- All entry orders include a stop-loss (OTO bracket)
- Order errors are caught, logged, and never crash the bot
- Position ownership is tracked per strategy to prevent cross-strategy interference

### 6. Reporting & Monitoring

Every trade is logged to SQLite for the go/no-go evaluation. This layer also computes live performance metrics and sends alerts.

**Trade log (SQLite — `data/trades.db`):**

The `TradeLogger` inserts a row for every fill (entry and exit). Schema includes timestamp, symbol, side, qty, avg_fill_price, strategy, slippage data, and status.

**Metrics computed (`reporting/metrics.py`):**

| Metric | Formula | Go/No-Go threshold |
|---|---|---|
| Sharpe Ratio | (Mean return − Risk-free rate) / Std dev of returns | > 1.0 |
| Max Drawdown | Largest peak-to-trough drop | < 15% |
| Profit Factor | Gross profit / Gross loss | > 1.3 |
| Win Rate | Winning trades / Total trades | > 45% |
| Avg Win / Avg Loss | Mean winning PnL / Mean losing PnL | > 1.5 |

Metrics are computed by `compute_metrics()` from a list of per-trade P&L values. The `MetricsSnapshot.meets_go_thresholds()` method checks all five gates at once.

**Go/no-go checker (`scripts/gonogo.py`):**
CLI tool that reads the trade DB, pairs buy/sell fills into round-trip P&Ls, computes all metrics, and renders a final GO/NO-GO verdict. Supports `--json` for machine-readable output.

---

## Go/No-Go Framework

Before committing live capital, ALL of the following must be satisfied:

1. Minimum **50 closed trades** in paper trading (statistical significance)
2. Paper trading period spans **at least 4 weeks** across varying market conditions
3. All five metrics meet their thresholds (see table above)
4. Bot has run for at least **72 hours continuously** without crashes or errors
5. Risk manager daily halt has never been triggered without being intentional
6. Paper ↔ Live toggle has been tested and confirmed working

Run the checker: `python scripts/gonogo.py` (exit code 0 = GO, 1 = NO-GO).

For slow daily-bar strategies such as the SMA-only Phase 9.5 run, 50 closed
trades may not be attainable in a 2-4 week paper window. In that case,
forward-test reconciliation and operational stability are stabilization gates.
The final live-readiness GO/NO-GO gate happens after Phase 10, with both SMA and
RSI active in paper mode and the full pre-live safeguard set implemented.

---

## Adding a New Strategy — Checklist

When implementing any new strategy:

1. Create `strategies/<strategy_name>.py`
2. Inherit from `BaseStrategy`
3. Set `name` class attribute — unique lowercase string
4. Set `preferred_order_type` — `OrderType.MARKET` or `OrderType.LIMIT`
5. Implement `_raw_signals(df) -> SignalFrame` — entries/exits boolean Series
6. Override `required_bars()` if the strategy needs more than 50 bars
7. Add unit tests in `tests/test_strategies.py`
8. Add a `StrategySlot` in `forward_test.py` to include the strategy in paper trading
9. Update this document's strategy table

---

## Key Libraries

| Library | Purpose |
|---|---|
| `alpaca-py` | Alpaca broker integration (official SDK) |
| `pandas` | OHLCV bar manipulation and indicator calculation |
| `vectorbt` | Backtesting harness |
| `loguru` | Structured logging (rotating file + console sinks) |
| `sqlite3` | Trade logging (built into Python) |
| `python-dotenv` | Environment variable management |

---

## What to Avoid

- Never place orders directly from a strategy — all orders go through Risk Manager
- Never hardcode API keys, URLs, or trading parameters
- Never use `alpaca-trade-api` (deprecated) — use `alpaca-py` only
- Never assume the market is open — always check the market clock
- Never let an unhandled exception crash the bot silently — use structured error handling and logging
- Never commit `.env` or `data/trades.db` to version control
- Never use `pandas-ta` — indicators are hand-rolled to eliminate dependency risk
