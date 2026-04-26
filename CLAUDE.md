# CLAUDE.md — Algorithmic Trading Bot

> This file is the persistent memory and context document for Claude when assisting with this project.
> Always read this file at the start of a new session before generating any code.

---

## Project Overview

A Python-based algorithmic trading bot built incrementally, starting with paper trading and progressing toward live trading. The bot is designed to be modular, testable, and strategy-agnostic.

---

## Stack

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Broker / Exchange | Alpaca Markets (paper trading → live) |
| Data Manipulation | pandas |
| Technical Indicators | Hand-rolled (SMA, EMA, ATR, RSI, ADX in `indicators/technicals.py`) |
| Backtesting | vectorbt |
| API Client | alpaca-py (official SDK) |
| Trade Logging | sqlite3 (`data/trades.db` paper / `data/trades_live.db` live) |
| Environment Config | python-dotenv (loaded from `config/.env`) |
| Logging | loguru (rotating file + console sinks) |

---

## Project Structure

```
trading-bot/
├── CLAUDE.md                  # This file
├── docs/
│   └── architecture.md        # Architecture guide and go/no-go framework
├── PLAN.md                    # Phased build plan and progress tracker
├── requirements.txt           # Pinned dependencies
├── main.py                    # Entry point
├── forward_test.py            # Launches engine for multi-week paper runs
├── start_bot.sh               # tmux + caffeinate launcher
├── stop_bot.sh                # Graceful shutdown
├── recycle_bot.sh             # stop + start (picks up code changes)
├── config/
│   ├── .env                   # API keys and runtime flags (never committed)
│   └── settings.py            # Centralized config (symbols, risk params, etc.)
├── data/
│   ├── fetcher.py             # Market data retrieval via StockHistoricalDataClient
│   ├── trades.db              # Paper SQLite trade log (gitignored)
│   ├── trades_live.db         # Live SQLite trade log (gitignored)
│   └── historical/            # Cached historical bars
├── indicators/
│   └── technicals.py          # SMA, EMA, ATR, RSI, ADX (hand-rolled)
├── strategies/
│   ├── base.py                # BaseStrategy, SignalFrame, StrategySlot, WatchlistSource
│   ├── sma_crossover.py       # Trend-following: SMA crossover
│   ├── rsi_reversion.py       # Mean-reversion: RSI oversold/overbought
│   └── filters/
│       ├── common.py          # SPYTrendFilter (shared macro gate)
│       ├── sma_crossover.py   # SMAEdgeFilter: stock > 200 SMA, volume expansion
│       └── rsi_reversion.py   # RSIEdgeFilter: SPY dual macro, earnings blackout, liquidity, no-new-low
├── regime/
│   └── detector.py            # RegimeDetector: BEAR/VOLATILE/TRENDING/RANGING (ADX + ATR%)
├── engine/
│   └── trader.py              # TradingEngine — the live loop orchestrator
├── backtest/
│   ├── runner.py              # vectorbt backtesting harness
│   └── reconcile.py           # Forward-test reconciliation (paper vs backtest)
├── execution/
│   └── broker.py              # AlpacaBroker — TradingClient wrapper + fractional path
├── risk/
│   ├── manager.py             # RiskManager: sizing, drawdown, stop-loss, kill switches
│   └── allocator.py           # SleeveAllocator: per-strategy capital budgets
├── reporting/
│   ├── logger.py              # TradeLogger — SQLite trade log
│   ├── metrics.py             # Sharpe, drawdown, profit factor, win rate
│   ├── pnl.py                 # PnLTracker — daily/weekly reports
│   └── alerts.py              # AlertDispatcher with pluggable backends
├── scripts/
│   ├── preflight.py           # Pre-flight checklist (must exit 0 before live flip)
│   ├── gonogo.py              # Go/no-go checker for live readiness
│   └── *.py                   # Watchlist scanners and analysis scripts
├── tests/                     # 646 unit tests (pytest)
├── logs/                      # Rotating log files (gitignored)
└── phase*_verify.py           # Integration verification scripts per phase
```

---

## Environment Variables

Stored in `config/.env` (never commit this file):

```
# Paper credentials (default)
ALPACA_API_KEY=your_paper_key
ALPACA_SECRET_KEY=your_paper_secret

# Live credentials (only used when LIVE_TRADING=true)
ALPACA_API_KEY_LIVE=your_live_key
ALPACA_SECRET_KEY_LIVE=your_live_secret

# Runtime flags
LIVE_TRADING=false          # Set true only after preflight.py exits 0
DRY_RUN=false               # Log orders without submitting (sanity check)
LIVE_SIZE_MULTIPLIER=0.25   # Scale live position sizes to 25% at launch
```

All credential and DB routing derives from `LIVE_TRADING`. Do not set
`ALPACA_PAPER` directly — it is derived automatically in `config/settings.py`.

---

## Key Design Principles

1. **Paper trading first** — all development and strategy validation happens on Alpaca's paper environment before any live capital is used.
2. **Modular architecture** — each concern (data, indicators, strategy, risk, execution) lives in its own module with clear interfaces.
3. **Strategy-agnostic engine** — the trading engine does not know about specific strategies; strategies implement a common interface.
4. **Risk management is non-negotiable** — every trade is subject to position sizing rules and maximum drawdown limits before execution.
5. **Backtesting before deployment** — every strategy must be backtested with vectorbt before being run live (even on paper).
6. **Complete, runnable code** — every code artifact generated in this project must be fully runnable with no placeholders.
7. **Incremental builds** — each phase produces working, testable code before moving to the next.

---

## Coding Conventions

- All functions have type hints and docstrings.
- Logging uses `loguru` (`from loguru import logger`), not `print()` for control flow. `print()` is OK for user-facing tabular output in smoke-test scripts.
- Config values (symbols, timeframes, thresholds) live in `config/settings.py`, never hardcoded.
- Exceptions are caught and logged; the bot never silently swallows errors.
- All monetary values are handled as `float`; quantities as `int` where Alpaca requires whole shares, or `float` for fractional.

---

## Testing Standard

Testing is **non-negotiable** for this project — this code is meant to place orders with
real money. Every phase has both:

### Two test layers per phase

| Layer | Location | Runs | Purpose |
|---|---|---|---|
| **Unit tests** | `tests/test_*.py` | `pytest` (every change) | Fast, offline, deterministic. Cover pure logic: validation, transformations, state machines, error paths. Never hit live APIs. Mock external dependencies. |
| **Integration / verification** | `phase<N>_verify.py` | Manually at phase boundaries | End-to-end against live Alpaca paper account. Proves the phase's exit criteria. Hits the network. |

### Rules

- **Every phase must ship unit tests.** A phase is not complete until both unit tests and the `phase<N>_verify.py` script pass.
- Unit tests go in `tests/<module>.py::Test<ClassName>::test_<behavior>` — grouped by class, one class per logical area.
- Use the `make_ohlcv` / `clean_ohlcv` / `tmp_cache_dir` fixtures in `tests/conftest.py` for synthetic data. **Never** use live Alpaca data in unit tests.
- Mark integration-requiring tests with `@pytest.mark.integration`; they are deselected by default.
- Test the **contract**, not the implementation: accept clean input, reject every type of bad input, cover every documented error path.
- Aim for ≥ 80% coverage on pure logic. Integration-only code (thin wrappers around Alpaca SDK) may be lower — it's exercised by `phase<N>_verify.py`.

### Running tests

The project virtualenv is at `venv/` inside the project root. Always invoke
pytest via its full path — never search outside the project directory:

```bash
# Correct — use the project venv directly
/Users/franco/trading-bot/venv/bin/pytest

# Never do this — it searches the entire home directory and triggers
# macOS permission prompts for Desktop, Documents, and every other folder:
# find /Users/franco -name pytest   ❌
```

```bash
# With coverage
/Users/franco/trading-bot/venv/bin/pytest --cov=<module> --cov-report=term-missing

# Phase integration check (hits Alpaca paper)
python phase2_verify.py
```

---

## Alpaca API Notes

- Paper trading base URL: `https://paper-api.alpaca.markets`
- Live trading base URL: `https://api.alpaca.markets`
- Data API (market data): use `feed="iex"` on a paper account (SIP requires paid subscription).
- Use `alpaca-py` (official SDK) — this project has migrated from the deprecated `alpaca-trade-api`. Do not use `alpaca-trade-api`.
- Orders: support market, limit, and stop-limit types.
- Positions: always check existing positions before placing new orders.
- GTC orders: Alpaca auto-cancels GTC orders after 90 days. The RSI mean reversion strategy uses limit orders — if any are submitted as GTC and sit open long enough, they will silently expire. The engine's startup reconciliation (`sync_with_broker`) catches this, but be aware of the 90-day ceiling.

---

## Current Phase

**Phase 10 — In Progress (2026-04-26). Phases 1–9 and 9.5 complete.**
**Tagged: `v1.0.0-beta.0`**

Both **SMA Crossover** and **RSI Reversion** are active in `forward_test.py`.

### Phase 10 completed items

| Item | Description |
|---|---|
| **10.B1** | Live config separation (`LIVE_TRADING` flag, separate credentials, `trades_live.db`) |
| **10.B2** | Pre-flight checklist (`scripts/preflight.py`) |
| **10.B3** | `WatchlistSource` abstraction + `StaticWatchlistSource`; `forward_test.py` wired |
| **10.C1** | Durable position ownership restored from trade DB on restart |
| **10.C2** | Startup reconciliation with NORMAL / RESTRICTED fail-safe modes |
| **10.C3/C4** | External-close detection with 3-cycle confirmation window |
| **10.E1** | WebSocket order/fill streaming via `TradingStream` (stream-first, REST fallback) |
| **10.F1** | `SleeveAllocator` in `risk/allocator.py` — 50/50 SMA/RSI, $8k per-position cap, `MAX_GROSS_EXPOSURE_PCT` → 0.80 |
| **10.F2** | `RegimeDetector` in `regime/detector.py` — BEAR/VOLATILE/TRENDING/RANGING (ADX + ATR% percentile) |
| **10.F3** | Engine regime gating — `StrategySlot.allowed_regimes`; exits never blocked |
| **10.F3a** | `SMAEdgeFilter` — stock > 200 SMA, volume expansion (10d > 30d avg) |
| **10.F3b** | `RSIEdgeFilter` — SPY dual macro, earnings blackout (3/2), liquidity floor, no-new-low |
| **10.F4** | RSI paper activation — both strategies running with full gating |
| **10.G1** | `LIVE_SIZE_MULTIPLIER=0.25` in risk manager when live |
| **10.G4** | `DRY_RUN` flag — broker logs orders without submitting |
| **10.G6** | Fractional share sizing — `FRACTIONAL_ENABLED` flag; DAY entry + standalone GTC stop; disable at ~$10k |

### Phase 10 remaining before live flip

| Item | Description | Blocker |
|---|---|---|
| **10.D1** | Review paper fills, compute mean realized slippage | ≥10 fills needed |
| **10.D2** | Enable `SLIPPAGE_DRIFT_ENABLED=True` | Blocked on D1 |
| **10.F6** | Operational verify — both strategies gated and sleeve-capped in logs | Runs as bot runs |
| **10.G2** | Set `HARD_DOLLAR_LOSS_CAP` in live `.env` (config only) | Anytime |
| **10.G5** | Pre-flight passes on live endpoint; dry-run cycle; manual approval for first order | After G2 |
| **10.H1–H5** | Cloud VPS, systemd service, key management, remote monitoring, log shipping | Post-paper |

Minimum gate before live flip: **D1 + D2 + G2 + G5 + ≥2 weeks combined SMA+RSI paper run.**

**Total: 646 unit tests passing.**

---

## Manual Restart Verification (Phase 10 operational gate — completed 2026-04-24)

Verified live with MU + NVDA open. Both restored from trade DB record; NORMAL
mode confirmed. Expected startup log pattern for reference (2 slots active):

**Position found and assigned from DB:**
```
restart: assigned existing position MU → 'sma_crossover' (trade DB record)
engine starting: 2 slot(s) [sma_crossover(16), rsi_reversion(5)], 21 unique symbol(s),
  session_start_equity=$..., open_positions=2, open_orders=2
```

**Position not in any slot (unmanaged):**
```
WARNING | restart: open position TSLA does not belong to any configured slot —
  it will NOT be managed by this engine. Close it manually or add it to a
  strategy's symbol universe.
```

**What to do if you see the WARNING:**
Close the unmanaged position manually on the Alpaca paper dashboard.
It will not be touched by the engine until you add it to the watchlist.
