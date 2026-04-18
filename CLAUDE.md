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
| Technical Indicators | pandas-ta |
| Backtesting | vectorbt |
| API Client | alpaca-trade-api (legacy SDK, v3.2.0) |
| Environment Config | python-dotenv (loaded from `config/.env`) |
| Scheduling | APScheduler or cron |
| Logging | loguru (rotating file + console sinks) |

---

## Project Structure (Target)

```
trading-bot/
├── CLAUDE.md                  # This file
├── PLAN.md                    # Phased build plan and progress tracker
├── requirements.txt           # Pinned dependencies
├── phase1_connect.py          # Phase 1 Alpaca connectivity smoke test
├── main.py                    # Entry point (wires engine together)
├── config/
│   ├── .env                   # API keys (never committed)
│   └── settings.py            # Centralized config (symbols, risk params, etc.)
├── data/
│   ├── fetcher.py             # Market data retrieval via Alpaca
│   ├── historical/            # Cached historical bars
│   └── db/                    # Local persistence
├── strategies/                # Strategy framework + concrete strategies
├── backtest/                  # vectorbt backtesting harness
├── execution/                 # Alpaca broker wrapper, order execution
├── risk/                      # RiskManager: sizing, drawdown, stop-loss
├── logs/                      # Rotating log files
└── notebooks/                 # Exploratory analysis
```

---

## Environment Variables

Stored in `config/.env` (never commit this file):

```
ALPACA_API_KEY=your_key_here
ALPACA_SECRET_KEY=your_secret_here
ALPACA_BASE_URL=https://paper-api.alpaca.markets   # paper trading
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
- Use `alpaca-trade-api` (legacy SDK, v3.2.0) — this project has standardized on it. Do not migrate to `alpaca-py` without explicit user approval.
- Orders: support market, limit, and stop-limit types.
- Positions: always check existing positions before placing new orders.

---

## Current Phase

**Phases 1–9 complete. Phase 9.5 infrastructure complete (awaiting run).**
Phase 9.5 tooling verified 2026-04-16: `backtest/reconcile.py` compares paper
fills against backtest predictions with a two-gate decision (return divergence
≤10% + mean slippage ≤20bps). `forward_test.py` launches the engine with full
reporting for multi-week paper runs. `get_closed_orders` on `AlpacaBroker`
retrieves Alpaca fill history. Total 256 unit tests + 17/17 live-paper
integration checks pass.

**Next steps:**
1. Run `python forward_test.py` for 2–4 weeks (operational, not code).
2. After the run, reconcile: `Reconciler(strategy, symbols, start, end).run()`.
3. If GO → proceed to Phase 10 (Live Trading Transition).
4. If NO-GO → return to Phase 5 for strategy re-analysis.

See `PLAN.md` for full phase breakdown and progress tracking.
