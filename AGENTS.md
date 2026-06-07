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
│   ├── gonogo.py              # Go/no-go checker for live readiness
│   └── legacy_verify/         # Historical paper integration checks (manual)
├── tests/                     # 352 unit tests (pytest)
├── logs/                      # Rotating log files (gitignored)
└── phase_operator_a_identity_verify.py # Current operator-controls verification
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
- If a code change completes, advances, or materially reframes a planned item already tracked in `PLAN.md`, update the relevant `PLAN.md` entry in the same workstream and include that plan sync in the same commit. Do not leave code and plan status out of sync for tracked roadmap items.

---

## Testing Standard

Testing is **non-negotiable** for this project — this code is meant to place orders with
real money. Current changes must ship with focused unit tests. Broker-facing or
operational changes may also require a targeted manual paper check.

### Test layers

| Layer | Location | Runs | Purpose |
|---|---|---|---|
| **Unit tests** | `tests/test_*.py` | `pytest` (every change) | Fast, offline, deterministic. Cover pure logic: validation, transformations, state machines, error paths. Never hit live APIs. Mock external dependencies. |
| **Manual paper checks** | `scripts/legacy_verify/*.py`, targeted scripts, or `phase_operator_a_identity_verify.py` | Manually when relevant | End-to-end or high-level checks against Alpaca paper/local DB. Hits the network when broker-facing. |

### Rules

- **Every material change must ship unit tests.** Broker-facing or operational changes should also name the manual paper verification path used or deferred.
- Unit tests go in `tests/<module>.py::Test<ClassName>::test_<behavior>` — grouped by class, one class per logical area.
- Use the `make_ohlcv` / `clean_ohlcv` / `tmp_cache_dir` fixtures in `tests/conftest.py` for synthetic data. **Never** use live Alpaca data in unit tests.
- Mark integration-requiring tests with `@pytest.mark.integration`; they are deselected by default.
- Test the **contract**, not the implementation: accept clean input, reject every type of bad input, cover every documented error path.
- Aim for ≥ 80% coverage on pure logic. Integration-only code (thin wrappers around Alpaca SDK) may be lower, but it needs targeted paper verification before live use.

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

# Example legacy paper check (hits Alpaca paper)
python scripts/legacy_verify/phase9_verify.py
```

---

## Alpaca API Notes

- Paper trading base URL: `https://paper-api.alpaca.markets`
- Live trading base URL: `https://api.alpaca.markets`
- Data API (market data) — feed strategy:
  - **SIP subscription tiers**: real-time SIP requires the paid Algo Trader Plus
    subscription (~$99/mo). **Delayed SIP** (bars ≥15 minutes old) is available on
    the basic, no-cost tier — perfect for any offline work.
  - **Live engine, paper account**: `feed="iex"` (free real-time bars on basic
    tier). Default in `ALPACA_DATA_FEED` env var.
  - **Live engine, live account**: `feed="sip"` real-time (needs the paid
    subscription).
  - **Research / backtests / audits / calibration**: `feed="sip"` (delayed,
    free on basic tier). `data/fetcher.py` enforces the 15-minute end-clamp
    automatically. Scripts should read from `BACKTEST_DATA_FEED` in
    `config/settings.py` (default `"sip"`). SIP gives consolidated-tape volume
    so liquidity-floor thresholds are interpretable in plain English.
  - **Execution replay / reconciliation / post-mortems**: `feed="iex"`.
    Reconciling paper or live fills against what the bot expected requires
    the **same feed the bot actually saw**. Using SIP would compare apples to
    oranges. `backtest/reconcile.py` and `scripts/post_mortem.py` stay on IEX
    regardless of `BACKTEST_DATA_FEED`.
  - **Why offline ≠ live feed for research**: IEX is one venue (~2-3% of
    consolidated volume); `utils.market.apply_synthetic_sip_volume` multiplies
    daily IEX volume by 20× as an approximation. Acceptable for the live
    engine on paper (no choice); not acceptable for offline research where
    real consolidated volume matters.
  - **Cache layout**: bars are stored at
    `data/historical/{feed}/{symbol}_{timeframe}_{adjustment}.parquet`.
    Pre-feed-aware legacy files (top-level paths) are read as IEX via a
    fallback path for backward compat. The migration script
    (`scripts/migrate_cache_to_feed_aware.py`) defaults to **quarantining**
    them into `data/historical/legacy_unknown_feed/` because legacy files
    have no recorded provenance. Pass `--assume-feed=iex
    --confirm-assumed-feed` only if you're sure the bars were IEX.
  - **Feed validation**: `data/fetcher.py` strictly validates `feed` against
    `{iex, sip}` — typos raise `ValueError` rather than silently creating
    mis-tagged cache dirs.
  - **Per-symbol SIP coverage** (verified 2026-06-07): SPY and major ETFs ~2016-01-04;
    most mega-cap stocks ~2016-01-04; mid-caps 2017-2019; recent IPOs / de-SPACs at
    listing date. IEX coverage is ~4.5 years shallower for most names. Always probe
    per symbol with a wide range before generalizing.
- Use `alpaca-py` (official SDK) — this project has migrated from the deprecated `alpaca-trade-api`. Do not use `alpaca-trade-api`.
- For any broker-facing behavior, treat **Alpaca's SDK/docs as the first source of truth** — not just for orders/fills/stops, but overall. Before inventing a home-grown fix or local abstraction around execution, positions, order lifecycle, market clock/session state, streams, reconnects, reconciliation, or account behavior, **first check Alpaca's SDK/docs for the supported path, constraints, and recommended recovery model**. Prefer broker-native or SDK-recommended solutions whenever Alpaca already defines the behavior. If a custom workaround is still needed, document why the SDK/docs path was insufficient.
- Orders: support market, limit, and stop-limit types.
- Positions: always check existing positions before placing new orders.
- GTC orders: Alpaca auto-cancels GTC orders after 90 days. The RSI mean reversion strategy uses limit orders — if any are submitted as GTC and sit open long enough, they will silently expire. The engine's startup reconciliation (`sync_with_broker`) catches this, but be aware of the 90-day ceiling.

---

## Current Phase

**Phase 10 — In Progress (2026-04-29). Phases 1–9 and 9.5 complete.**

Both **SMA Crossover** and **RSI Reversion** are currently active in
`forward_test.py` in paper mode. The bot is running in tmux for the combined
Phase 10 paper-validation gate; this is now a multi-strategy operational test,
not the earlier SMA-only forward run.

Phase 10 completed to date:
- **10.B1** Live config separation (`LIVE_TRADING`, separate credentials, `trades_live.db`)
- **10.B2** Pre-flight checklist (`scripts/preflight.py`)
- **10.B3** `WatchlistSource` abstraction + `StaticWatchlistSource`; per-strategy watchlists wired
- **10.C1** Durable position ownership restored from trade DB on restart
- **10.C2** Startup reconciliation with NORMAL / RESTRICTED fail-safe modes
- **10.C3/C4** Tests + external-close detection with 3-cycle confirmation window
- **10.E1** WebSocket order/fill streaming via `TradingStream` (stream-first, REST fallback)
- **10.F1** Per-strategy capital sleeve allocator (`STRATEGY_ALLOCATIONS`, 50/50 sleeves)
- **10.F2/F3** Regime detector + engine-level `allowed_regimes` gating
- **10.F3a** SMA edge filter (`SPY > 200 SMA`, stock `> 200 SMA`, `10d vol > 30d vol`)
- **10.F3b** RSI edge filter (`SPY > 200/50 SMA`, earnings blackout, liquidity floor, no new 20-day low)
- **10.F4** RSI paper activation in `forward_test.py`
- **10.G1** `LIVE_SIZE_MULTIPLIER=0.25` applied in risk manager when live
- **10.G4** `DRY_RUN` flag — broker logs orders without submitting
- **10.G6** Fractional share sizing (`FRACTIONAL_ENABLED=True`; fractional DAY entry + standalone whole-share stop path)

Phase 10 current focus / remaining blockers before live (see `PLAN.md`):
- **10.D1/D2** Slippage kill switch calibration and enablement — this is the active work now
- **10.F6** Verify combined SMA + RSI paper logs: startup reconciliation, sleeve accounting, regime gating, attribution
- **10.G2** Hard dollar cap config for live `.env`
- **10.G5** Final live go/no-go verification
- **10.H1-H5** VPS provisioning and deployment hardening before any live transition
- Minimum **2-4 week combined SMA + RSI paper run** with documented GO/NO-GO before flipping live

Current local verification: **757 tests passing** via
`/Users/franco/trading-bot/venv/bin/pytest` on 2026-04-30. `PLAN.md`'s latest
recorded milestone is **646 unit tests passing** as of 2026-04-25, before the
2026-04-28 dashboard work and 2026-04-29 Bollinger strategy additions.

**Operational preference:**
- Do **not** create Git worktrees for this repo unless the user explicitly asks.
  Gemini / Antigravity can hang or misbehave when linked worktrees exist, so
  normal in-place branch work is preferred.
- Keep `.git/config` set to `extensions.worktreeConfig = false` for this repo.
  If any linked worktrees were created temporarily, remove them afterward and
  restore that setting to `false`.
- **Never embed credentials in the git remote URL.** The remote must stay
  token-free (`https://github.com/francomarb/trading-bot.git` or an SSH
  remote). Do **not** run `git remote set-url origin https://user:$TOKEN@…`
  to recover from auth failures, and do **not** grab `GH_TOKEN` from
  `~/.zshrc` to inject into config — that pattern silently persists the
  token into `.git/config` where any process can read it. If `git push`
  fails on auth, ask the operator or fall back to the macOS Keychain via
  the credential helper / `gh auth login`. Never paper over auth by
  embedding a token.
- **Always use `./recycle_bot.sh`** when restarting or recycling the bot, rather
  than manually killing the tmux session and calling `./start_bot.sh`.

**Immediate operational next steps:**
1. Keep `python forward_test.py` running and continue collecting real paper fills.
2. After enough fills accumulate, calibrate realized slippage vs the 5 bps model (`10.D1`).
3. If thresholds hold, enable `SLIPPAGE_DRIFT_ENABLED=True` (`10.D2`).
4. Continue the combined SMA + RSI paper-validation window and produce the Phase 10 GO/NO-GO evidence package before any live flip.

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
