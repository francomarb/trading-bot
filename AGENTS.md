# AGENTS.md — Algorithmic Trading Bot

> This file is the persistent memory and context document for Codex when assisting with this project.
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
| Technical Indicators | Hand-rolled (SMA, EMA, ATR, RSI in `indicators/technicals.py`) |
| Backtesting | vectorbt |
| API Client | alpaca-py (official SDK) |
| Trade Logging | sqlite3 (`data/trades.db`) |
| Environment Config | python-dotenv (loaded from `config/.env`) |
| Logging | loguru (rotating file + console sinks) |

---

## Project Structure

```
trading-bot/
├── AGENTS.md                  # This file
├── docs/
│   └── architecture.md        # Architecture guide and go/no-go framework
├── PLAN.md                    # Phased build plan and progress tracker
├── requirements.txt           # Pinned dependencies
├── main.py                    # Entry point
├── forward_test.py            # Launches engine for multi-week paper runs
├── start_bot.sh               # tmux + caffeinate launcher
├── config/
│   ├── .env                   # API keys (never committed)
│   └── settings.py            # Centralized config (symbols, risk params, etc.)
├── data/
│   ├── fetcher.py             # Market data retrieval via StockHistoricalDataClient
│   ├── trades.db              # SQLite trade log (gitignored)
│   └── historical/            # Cached historical bars
├── indicators/
│   └── technicals.py          # SMA, EMA, ATR, RSI (hand-rolled)
├── strategies/
│   ├── base.py                # BaseStrategy, SignalFrame, StrategySlot, Scanner
│   ├── sma_crossover.py       # Trend-following: SMA crossover
│   └── rsi_reversion.py       # Mean-reversion: RSI oversold/overbought
├── engine/
│   └── trader.py              # TradingEngine — the live loop orchestrator
├── backtest/
│   ├── runner.py              # vectorbt backtesting harness
│   └── reconcile.py           # Forward-test reconciliation (paper vs backtest)
├── execution/
│   └── broker.py              # AlpacaBroker — TradingClient wrapper
├── risk/
│   └── manager.py             # RiskManager: sizing, drawdown, stop-loss
├── reporting/
│   ├── logger.py              # TradeLogger — SQLite trade log
│   ├── metrics.py             # Sharpe, drawdown, profit factor, win rate
│   ├── pnl.py                 # PnLTracker — daily/weekly reports
│   └── alerts.py              # AlertDispatcher with pluggable backends
├── scripts/
│   └── gonogo.py              # Go/no-go checker for live readiness
├── tests/                     # 352 unit tests (pytest)
├── logs/                      # Rotating log files (gitignored)
└── phase*_verify.py           # Integration verification scripts per phase
```

---

## Environment Variables

Stored in `config/.env` (never commit this file):

```
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_PAPER=true        # Set to false for live trading
```

---

## Sensitive Information Policy

`AGENTS.md` is safe to commit only because it must never contain real secrets or
private account details.

Never add:
- Real Alpaca API keys, secret keys, tokens, passwords, or webhook URLs
- Account numbers, live account IDs, personal financial identifiers, or tax details
- Exact live/paper account balances, buying power, open positions, order IDs, or fills
- Raw `.env` contents, broker exports, private logs, or database rows
- Personal contact details beyond public/project-safe attribution

Use placeholders such as `your_key_here`, `$...`, `<redacted>`, or high-level summaries
instead. If operational evidence is needed, summarize the behavior without copying
sensitive values.

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

**Phase 10 — In Progress (2026-04-24). Phases 1–9 and 9.5 complete.**

Only **SMA Crossover** is currently active in `forward_test.py`.
RSI Reversion is implemented and backtested but not yet running. RSI activation
is a Phase 10 paper-mode deliverable (10.F4).

Phase 10 completed to date (2026-04-24):
- **10.B1** Live config separation (`LIVE_TRADING` flag, separate credentials, `trades_live.db`)
- **10.B2** Pre-flight checklist (`scripts/preflight.py`)
- **10.B3** `WatchlistSource` abstraction + `StaticWatchlistSource`; `forward_test.py` wired
- **10.C1** Durable position ownership restored from trade DB on restart
- **10.C2** Startup reconciliation with NORMAL / RESTRICTED fail-safe modes
- **10.C3/C4** Tests + external-close detection with 3-cycle confirmation window
- **10.E1** WebSocket order/fill streaming via `TradingStream` (stream-first, REST fallback)
- **10.G1** `LIVE_SIZE_MULTIPLIER=0.25` applied in risk manager when live
- **10.G4** `DRY_RUN` flag — broker logs orders without submitting

Phase 10 remaining blockers before live (see PLAN.md):
- 10.D1/D2 Slippage kill switch calibration (needs ≥10 real fills)
- 10.F1–F5 Multi-strategy portfolio layer (capital allocation, regime gating, RSI activation)
- 10.F3a/F3b SMA + RSI edge filters
- 10.G2 Hard dollar cap config; 10.G5 Go/no-go verification
- Minimum 2-week SMA + RSI combined paper run before any live flip

Total: 510 unit tests passing.

**Next steps:**
1. Let `python forward_test.py` run through next market week (Monday 2026-04-28).
2. After ≥10 real fills → enable slippage kill switch (10.D1/D2).
3. Implement SMA edge filter (10.F3a) and RSI edge filter (10.F3b).
4. Then capital allocation + regime gating (10.F1–F3) + RSI activation (10.F4).

---

## Manual Restart Verification (Phase 10 operational gate — completed 2026-04-24)

Verified live with MU + NVDA open. Both restored from trade DB record; NORMAL
mode confirmed. Expected startup log pattern for reference:

**Position found and assigned from DB:**
```
restart: assigned existing position MU → 'sma_crossover' (trade DB record)
engine starting: 1 slot(s) [sma_crossover(16)], 16 unique symbol(s),
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
