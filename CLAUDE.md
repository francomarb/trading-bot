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
├── CLAUDE.md                  # This file
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

```bash
# Unit tests (fast, offline — run constantly during dev)
pytest

# With coverage
pytest --cov=<module> --cov-report=term-missing

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

---

## Current Phase

**Phases 1–9 complete. Phase 9.5 infrastructure complete. Architecture
alignment refactoring complete (2026-04-19).**

Refactoring accomplished:
- Migrated from `alpaca-trade-api` to `alpaca-py` (official SDK)
- Paper/Live toggle via `ALPACA_PAPER` boolean
- `BaseStrategy.required_bars()` with calendar-day conversion in engine
- RSI indicator + `RSIReversion` strategy (mean-reversion, limit orders)
- SQLite trade log (migrated from CSV)
- Live metrics module (`compute_metrics` → `MetricsSnapshot`)
- Go/no-go checker script (`scripts/gonogo.py`)

Total: 352 unit tests passing + 17/17 live-paper integration checks.

**Next steps:**
1. Run `python forward_test.py` for 2–4 weeks (operational, not code).
2. Check readiness: `python scripts/gonogo.py` (exit 0 = GO).
3. After the run, reconcile: `Reconciler(strategy, symbols, start, end).run()`.
4. If GO → proceed to Phase 10 (Live Trading Transition).
5. If NO-GO → return to Phase 5 for strategy re-analysis.

See `PLAN.md` for full phase breakdown and progress tracking.
