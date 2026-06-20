# CLAUDE.md — Algorithmic Trading Bot

> This file is the persistent memory and context document for Claude when assisting with this project.
> Always read this file at the start of a new session before generating any code.

---

## Reading order — non-negotiable

**Before generating any code, read [PLAN.md](PLAN.md).** Not "skim it" — read the sections relevant to where the work is going to land.

The reason: this codebase has several phased rollouts in flight simultaneously (`position_uid` lifecycle, slippage unification, strategy health monitor, MLEG close walk-and-market, etc.). A field, column, or invariant being present in one code path does NOT mean it's wired through every code path — the design docs explicitly defer parts to later phases.

If your change depends on any of these moving pieces, **follow the links from PLAN.md to the corresponding `docs/*.md` design doc** and check which phase the relevant field/invariant is currently in. Then verify the assumption against the actual code (`grep` for the field, read the call sites).

**Lesson from the PR #56 audit (2026-06-10):** four reviewer rounds caught bugs that all traced back to one root cause — building on top of `position_uid` as if it were fully wired when in fact only Phase A (equity entries → Operator CLI) was deployed. Close paths, options paths, and restart reconstruction were all explicitly deferred. Reading the Phase A commit's scope note would have caught all four in the original implementation.

When in doubt: check PLAN.md → follow the link → verify in code. Trust the design docs to be honest about what's deployed; do not trust your assumptions about "this field is obviously there everywhere."

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
| Technical Indicators | Hand-rolled (SMA, EMA, ATR, RSI, ADX, Bollinger Bands, Keltner Channels, Donchian in `indicators/technicals.py`) |
| Backtesting | vectorbt |
| API Client | alpaca-py (official SDK) |
| Options Pricing | blackscholes (Black-Scholes Delta + price for exit guards) |
| Sector Data | yfinance (sector resolver cache, VIX fetch for options strategy) |
| Dashboard | streamlit + plotly (`dashboard.py` — read-only analytics) |
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
│   ├── watchlists.py          # WatchlistSource ABC + StaticWatchlistSource
│   ├── trades.db              # Paper SQLite trade log (gitignored)
│   ├── trades_live.db         # Live SQLite trade log (gitignored)
│   ├── envelopes/             # Per-strategy backtest envelopes (build_envelopes.py)
│   ├── health_reports/        # Weekly/monthly strategy-health markdown reports
│   ├── health_state.json      # Health-monitor NEGATIVE persistence state (gitignored)
│   └── historical/            # Cached historical bars
├── indicators/
│   └── technicals.py          # SMA, EMA, ATR, RSI, ADX, Bollinger Bands, Keltner Channels, Donchian high/low (hand-rolled)
├── strategies/
│   ├── base.py                # BaseStrategy, SignalFrame, StrategySlot, WatchlistSource
│   ├── sma_crossover.py       # Trend-following: SMA crossover (ACTIVE)
│   ├── rsi_reversion.py       # Mean-reversion: RSI oversold/overbought (ACTIVE)
│   ├── bollinger_squeeze.py   # Volatility breakout: TTM-style BB squeeze (IMPLEMENTED, NOT WIRED — parked)
│   ├── donchian_breakout.py   # Trend continuation: Turtle System 1 — N-day high/low (ACTIVE, 30/15, ai_bigtech 32-name universe)
│   ├── spy_options_reversion.py # Options mean-reversion: SPY calls on RSI recovery (ACTIVE)
│   ├── filters/
│   │   ├── common.py          # SPYTrendFilter (shared macro gate)
│   │   ├── sma_crossover.py   # SMAEdgeFilter: stock > 200 SMA, volume expansion
│   │   ├── rsi_reversion.py   # RSIEdgeFilter: SPY dual macro, earnings blackout, liquidity, no-new-low
│   │   ├── bollinger_squeeze.py # BollingerSqueezeEdgeFilter: IEX-scaled liquidity, earnings blackout, exhaustion gate
│   │   ├── donchian_breakout.py # DonchianEdgeFilter: stock > 200 SMA, IEX-scaled liquidity, short earnings blackout
│   │   └── spy_options_reversion.py # SPYOptionsEdgeFilter: SPY > 100 SMA
│   └── health/                # Strategy Health & Edge Monitor v1 (PLAN 11.10 — advisory only)
│       ├── stats.py           # Bootstrap CI, one-sided t-test, EMA50/100 cross detector
│       ├── thresholds.py      # Per-strategy Health-check thresholds (calibration TODOs)
│       ├── reports.py         # HealthReport / EdgeReport / CheckResult dataclasses
│       ├── benchmarks.py      # Per-strategy equal-weight buy-and-hold benchmark
│       ├── envelope.py        # StrategyEnvelope — backtest reference bands + JSON I/O
│       ├── persistence.py     # 3-week NEGATIVE persistence state (health_state.json)
│       ├── lifecycle.py       # Gate lifecycle counter table I/O (strategy_lifecycle_counters)
│       ├── assessor.py        # HealthAssessor — L1/L2/L3 forensic checks
│       ├── edge.py            # EdgeAssessor — three-signal verdict + recommendation
│       ├── reviewer.py        # Orchestrates assessors, renders weekly/monthly reports + alerts
│       └── scheduler.py       # HealthReviewScheduler — Monday + first-of-month post-cycle hook
├── regime/
│   └── detector.py            # RegimeDetector: BEAR/VOLATILE/TRENDING/RANGING (ADX + ATR%)
├── sector/
│   ├── resolver.py            # Ticker → GICS sector via yfinance (JSON cache)
│   └── gauge.py               # Sector ETF HOT/NEUTRAL/COLD scoring + SectorMomentumFilter
├── engine/
│   └── trader.py              # TradingEngine — the live loop orchestrator
├── backtest/
│   ├── runner.py              # vectorbt backtesting harness
│   ├── reconcile.py           # Forward-test reconciliation (paper vs backtest)
│   └── spy_options_backtest.py # SPY options RSI reversion backtest
├── execution/
│   ├── broker.py              # AlpacaBroker — TradingClient wrapper + fractional path
│   ├── stream.py              # StreamManager — TradingStream WebSocket wrapper
│   └── options_executor.py    # OptionsExecutionWorker — async bracket order thread
├── utils/
│   └── options_lookup.py      # OCC contract selection (find_best_call)
├── risk/
│   ├── manager.py             # RiskManager: sizing, drawdown, stop-loss, kill switches
│   └── allocator.py           # SleeveAllocator: per-strategy capital budgets
├── reporting/
│   ├── logger.py              # TradeLogger — SQLite trade log
│   ├── metrics.py             # Sharpe, drawdown, profit factor, win rate, Kelly
│   ├── pnl.py                 # PnLTracker — daily/weekly reports
│   └── alerts.py              # AlertDispatcher — LogFile + TelegramAlertBackend + TelegramCommandListener
├── dashboard.py               # Streamlit read-only analytics dashboard
├── scripts/
│   ├── preflight.py           # Pre-flight checklist (must exit 0 before live flip)
│   ├── gonogo.py              # Go/no-go checker for live readiness
│   ├── build_envelopes.py     # Builds per-strategy backtest envelopes for the health monitor
│   ├── strategy_health_review.py # On-demand strategy health/edge report CLI
│   ├── calibrate_health_thresholds.py # Suggests health-threshold diffs from N weeks of data
│   ├── legacy_verify/         # Historical paper integration checks (manual)
│   └── *.py                   # Watchlist scanners and analysis scripts
├── tests/                     # Unit tests (pytest)
├── logs/                      # Rotating log files (gitignored)
└── phase_operator_a_identity_verify.py # Current operator-controls verification
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

## Git / Repository Conventions

- **Never use git worktrees on this repo.** Worktrees break the live bot's data
  paths (`data/trades.db`, `data/engine_state.json`, `logs/`, `data/health_state.json`)
  because the running bot expects a single canonical checkout, and they have
  caused issues with Gemini Antigravity in particular. Always operate directly
  on the main repo checkout at `/Users/franco/trading-bot`. Use branches for
  feature work, not worktrees.
- **Enforced via `.git/config`:** `extensions.worktreeConfig = false` is set
  to make worktree creation a deliberate override rather than a casual default.
  Do not flip this on.
- **Never embed credentials in the git remote URL.** The remote must stay
  token-free (`https://github.com/francomarb/trading-bot.git` or an SSH
  remote). Do **not** run `git remote set-url origin https://user:$TOKEN@…`
  to recover from auth failures, and do **not** grab `GH_TOKEN` from
  `~/.zshrc` to inject into config — that pattern silently persists the
  token into `.git/config` where any process can read it. If `git push`
  fails on auth, the right responses are: ask the operator, fall back to
  the macOS Keychain via the credential helper, or use `gh auth login`.
  Never paper over auth by embedding a token.
- Branching: default to `main` for routine work, docs, and small hotfixes.
  Use a feature branch + PR for substantive features or anything that wants
  review before landing.
- Bot lifecycle: always use the scripts (`start_bot.sh` / `stop_bot.sh` /
  `recycle_bot.sh`) — never send raw tmux keys to the bot session. Before
  running any of them yourself, **ask the operator first** and proceed only
  after explicit confirmation. The scripts are reversible enough to be
  fine when authorized, but a stop/start during market hours can drop
  an in-flight close attempt or interrupt a fill being observed, so the
  operator decides timing.

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
  - **SIP subscription tiers**: real-time SIP requires the paid Algo Trader Plus subscription (~$99/mo). **Delayed SIP** (bars ≥15 minutes old) is FREE on the basic Alpaca tier — perfect for any offline work.
  - **Live engine, paper account**: `feed="iex"` (free real-time bars on basic tier). Default in `ALPACA_DATA_FEED` env var.
  - **Live engine, live account**: `feed="sip"` real-time (needs the paid subscription).
  - **Research / backtests / audits / calibration**: `feed="sip"` (delayed, free). `data/fetcher.py` enforces the 15-min end-clamp automatically; scripts read from `BACKTEST_DATA_FEED` in `config/settings.py` (default `"sip"`). SIP gives consolidated-tape volume so liquidity-floor thresholds are interpretable in plain English. IEX is one venue (~2-3% of consolidated volume); `utils.market.apply_synthetic_sip_volume` multiplies daily IEX volume by 20× as an approximation — acceptable for the live engine which has no choice on a paper account, not acceptable for offline research.
  - **Execution replay / reconciliation / post-mortems**: `feed="iex"`. Reconciling paper or live fills against what the bot expected requires the **same feed the bot actually saw**. Using SIP here would compare apples to oranges — the bot decided on IEX volume thresholds, so replay must use IEX. `backtest/reconcile.py` and `scripts/post_mortem.py` are the canonical examples; both stay on IEX regardless of `BACKTEST_DATA_FEED`.
  - **Cache layout**: bars are stored at `data/historical/{feed}/{symbol}_{timeframe}_{adjustment}.parquet`. Pre-feed-aware legacy files (top-level paths) are read as IEX via a fallback for backward compat; the migration script (`scripts/migrate_cache_to_feed_aware.py`) defaults to **quarantining** them into `data/historical/legacy_unknown_feed/` because legacy files have no recorded provenance. Pass `--assume-feed=iex --confirm-assumed-feed` only if you're sure the bars were IEX.
  - **Feed validation**: `data/fetcher.py` strictly validates `feed` against `{iex, sip}` — typos raise `ValueError` rather than silently creating mis-tagged cache dirs.
  - **SIP coverage depth varies per symbol** — SPY back to 2016-01-04, most ai_bigtech mega-caps to 2016-01-04 (deeper than IEX by ~4.5 years), later listings at their listing dates. Always probe per symbol with a wide range before generalizing.
- Use `alpaca-py` (official SDK) — this project has migrated from the deprecated `alpaca-trade-api`. Do not use `alpaca-trade-api`.
- Orders: support market, limit, and stop-limit types.
- Positions: always check existing positions before placing new orders.
- GTC orders: Alpaca auto-cancels GTC orders after 90 days. The RSI mean reversion strategy uses limit orders — if any are submitted as GTC and sit open long enough, they will silently expire. The engine's startup reconciliation (`sync_with_broker`) catches this, but be aware of the 90-day ceiling.

---

## Current Phase and Progress

See [PLAN.md](PLAN.md) for the current phase, completed items, remaining blockers before live flip, and the notes/decisions log.
