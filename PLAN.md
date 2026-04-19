# PLAN.md — Algorithmic Trading Bot Build Plan

> Tracks the phased development plan, deliverables per phase, and current progress.
> Update the status column as each item is completed.
>
> **Goal:** A bot that can eventually operate with real capital. Every phase is designed
> with the assumption that this code will one day place orders with real money.

---

## Progress Summary

| Phase | Title | Status |
|---|---|---|
| 1 | Environment Setup | ✅ Complete |
| 2 | Market Data Pipeline (+ Local Cache) | ✅ Complete |
| 3 | Technical Indicators | ✅ Complete |
| 4 | Strategy Framework | ✅ Complete |
| 5 | Backtesting Harness (+ Validation Rigor) | ✅ Complete |
| 6 | Risk Management | ✅ Complete |
| 7 | Broker Integration & Order Execution | ✅ Complete |
| 8 | Trading Engine (Main Loop) | ✅ Complete |
| 9 | Trade Reporting & P&L | ✅ Complete |
| 9.5 | Forward-Test (Paper, Multi-Week) | 🔄 In Progress (infrastructure complete, awaiting multi-week run) |
| 10 | Live Trading Transition | ⬜ Not Started |
| 11 | Multi-Strategy, Regime Detection & Portfolio Layer | ⬜ Not Started *(post-live)* |

**Legend:** ⬜ Not Started · 🔄 In Progress · ✅ Complete

> **Testing gate:** every phase ships both (a) unit tests in `tests/test_*.py` (offline,
> fast, run on every change via `pytest`) and (b) an integration script `phase<N>_verify.py`
> that proves the exit criteria against live Alpaca paper. See CLAUDE.md → Testing Standard.

---

## Success Metric

The goal is **not** "always profitable." The goal is:

> **Positive expectancy over time with controlled drawdowns.**

A healthy system can still have 40–60% win rates, losing streaks, ugly months, and long
flat periods. That is normal. Catastrophic account damage is not. Design for the
latter, accept the former.

## Guiding Principles (apply to every phase)

1. **Unsafe by construction is forbidden.** Risk checks cannot be bypassed — they are a
   required argument to order placement, not an afterthought.
2. **The broker is the source of truth.** On startup, always reconcile local state with
   Alpaca positions/orders. Never trust local state alone.
3. **Never act on stale data.** Every cycle checks the freshness of the latest bar and
   refuses to trade if it's too old (configurable threshold).
4. **Fail loud, fail safe.** On any error in the decision path, default to *no action*
   and log at ERROR level. Silent failures are the enemy.
5. **Backtest ≠ reality.** A backtest without slippage, walk-forward validation, and
   look-ahead bias checks is a storytelling tool, not an edge test.
6. **Paper forward-test before live.** No strategy goes to real money without multi-week
   paper runs where live fills are reconciled against backtest predictions.
7. **Risk > Entry.** Edge comes more from sizing, stops, and exits than from the entry
   signal. Spend effort accordingly.
8. **Boring beats fancy.** Simple strategies that look unimpressive in backtests often
   survive live. Complicated strategies that look beautiful usually don't.
9. **One strategy first.** Prove discipline and structure with a single strategy before
   adding any diversification. Complexity is added only after a baseline edge is
   demonstrated live.
10. **Strategies decay.** Every strategy goes through strong performance, drawdown, and
    either recovery or decay. The bot must monitor per-strategy health and never grant
    permanent blind trust.

---

## Architectural Mental Model (6 layers)

The system is a pipeline of 6 layers. Each cycle flows top-to-bottom:

1. **Data** — market data, indicators, signal inputs, integrity checks *(Phases 2–3)*
2. **Regime Detector** — identifies trend/chop/volatility state; gates which strategies
   are allowed to run. *(Minimal "edge filters" in Phase 4; full regime layer in Phase 11.)*
3. **Strategy** — generates entries and exits. Pure function of data. *(Phase 4)*
4. **Risk** — position sizing, stop placement, exposure caps, kill switches.
   **Mandatory gate between Strategy and Execution.** *(Phase 6)*
5. **Execution** — order submission, status tracking, fill reconciliation, retries. *(Phase 7)*
6. **Monitoring & Analytics** — logs, alerts, per-strategy attribution, health checks,
   slippage drift. *(Phases 1 + 9)*

---

## Phase Details

---

### Phase 1 — Environment Setup
**Goal:** Reproducible, working local environment with verified Alpaca connectivity.

| # | Deliverable | Status |
|---|---|---|
| 1.1 | `requirements.txt` with pinned deps (alpaca-trade-api, pandas, pandas-ta, vectorbt, python-dotenv, loguru) | ✅ |
| 1.2 | `config/.env` with API keys | ✅ |
| 1.3 | `config/settings.py` — centralized config object | ✅ |
| 1.4 | `phase1_connect.py` — connection test script (account info, live quote, bars, positions) | ✅ |
| 1.5 | Verified: script runs, returns paper account balance | ✅ (2026-04-14: $100k equity, ACTIVE) |

**Exit Criteria:** Running `python phase1_connect.py` prints account equity from Alpaca paper environment. ✅

---

### Phase 2 — Market Data Pipeline (+ Local Cache)
**Goal:** Reliably fetch, cache, and serve OHLCV data for any symbol and timeframe —
without burning the Alpaca data API rate limits during backtesting iteration.

| # | Deliverable | Status |
|---|---|---|
| 2.1 | `data/fetcher.py` — fetch historical bars (1Day, 1Hour, 1Min) via Alpaca | ✅ |
| 2.2 | Support for multiple symbols in a single call (`fetch_symbols`) | ✅ |
| 2.3 | Data returned as clean `pd.DataFrame` with timezone-aware DatetimeIndex | ✅ |
| 2.4 | Data validation: no NaNs in OHLCV, numeric dtypes, monotonic index, duplicate-timestamp removal at merge seams | ✅ |
| 2.5 | **Local Parquet cache** in `data/historical/` keyed by `(symbol, timeframe, adjustment)`; sidecar `.meta.json` tracks the *requested* covered window so weekends/holidays don't trigger phantom refetches | ✅ |
| 2.6 | **Stale-data check** — `is_fresh(df, max_age)` + `require_fresh()` that raises `StaleDataError` | ✅ |
| 2.7 | **Rate-limit-aware retry** — `_with_retry` wrapper, exponential backoff on HTTP 429 and 5xx/network errors | ✅ |
| 2.8 | Verified: `phase2_verify.py` — cold→cached (0 API calls on warm), multi-symbol, freshness, validation, range-extension partial cache hit | ✅ (2026-04-14) |
| 2.9 | **Unit tests** in `tests/test_fetcher.py` — 35 tests covering `_validate`, `_to_utc`, `_missing_ranges`, `is_fresh`/`require_fresh`, cache round-trip, `_with_retry` (429/5xx/4xx/max-attempts/network). Pytest infrastructure in place (`pytest.ini`, `tests/conftest.py` with offline fixtures). | ✅ (2026-04-14, 35/35 passing, 69% coverage; uncovered lines are live-API paths exercised by `phase2_verify.py`) |

**Exit Criteria:** `data/fetcher.py` returns a validated DataFrame for any symbol; second call for the same range is served from cache with zero API requests. ✅

---

### Phase 3 — Technical Indicators
**Goal:** Small, reliable indicator library. Build indicators only as strategies require
them — no speculative surface area.

| # | Deliverable | Status |
|---|---|---|
| 3.1 | `indicators/technicals.py` — pure functions with type hints and docstrings | ✅ |
| 3.2 | Initial set: **SMA, EMA, ATR** (hand-rolled, not pandas-ta — see 3.5) | ✅ |
| 3.3 | Each function accepts a DataFrame, returns a copy with new column named `{ind}_{length}` (`sma_20`, `ema_50`, `atr_14`) | ✅ |
| 3.4 | Unit tests `tests/test_technicals.py` — 25 tests with hand-computed expected values (EMA seeded by SMA-of-first-N per Wilder convention; ATR uses Wilder's RMA) | ✅ |
| 3.5 | **Hand-rolled, not pandas-ta.** Rationale: pandas-ta is on a lightly-maintained fork with past pandas 2.x breakage. SMA/EMA/ATR are ~5 lines each; eliminating the dep removes a real-money risk path. Documented in module docstring. Can reintroduce pandas-ta selectively for complex indicators later. | ✅ |
| 3.6 | Integration check `phase3_verify.py` — compute full indicator stack on live AAPL bars, assert shape + sanity properties | ✅ (2026-04-14) |

**Exit Criteria:** SMA, EMA, ATR functions pass 25 unit tests with hand-computed expected values. Integration script runs the full stack on live bars and prints indicator values. ✅

---

### Phase 4 — Strategy Framework
**Goal:** Abstract strategy interface aligned with vectorbt conventions, plus one
concrete working strategy.

| # | Deliverable | Status |
|---|---|---|
| 4.1 | `strategies/base.py` — abstract `BaseStrategy` class | ✅ |
| 4.2 | Strategy interface returns **separate `entries` and `exits` boolean Series** (vectorbt-native convention), not a conflated `{1,-1,0}` column | ✅ |
| 4.3 | `strategies/sma_crossover.py` — SMA crossover strategy (fast/slow MA, configurable windows) | ✅ |
| 4.4 | **Look-ahead bias guard** — all indicators use only data available *at signal bar close*; execution assumed on *next bar's open* | ✅ |
| 4.5 | Unit tests in `tests/test_strategies.py` using synthetic price paths with known crossover points | ✅ |
| 4.6 | Strategies are pure functions of input data — no network calls, no broker state | ✅ |
| 4.7 | **Edge-filter hook** — strategies can optionally require a market-regime condition (e.g. `SPY > 200-day MA`) before emitting long entries. Implemented as a composable filter, not hard-coded. This is the minimal regime awareness for the first strategy; the full regime detector is Phase 11. | ✅ |
| 4.8 | **Preferred order type declared on the strategy** — e.g. `SMACrossover.preferred_order_type = OrderType.MARKET`. Trend/breakout strategies will use market; mean-reversion will use limit. Consumed by Phase 7 execution. | ✅ |

**Exit Criteria:** `SMACrossover.generate_signals()` returns correct entries/exits on synthetic data with known crossover points; tests pass.

---

### Phase 5 — Backtesting Harness (+ Validation Rigor)
**Goal:** Honest backtesting that surfaces — not hides — weak strategies. A backtest
without slippage, walk-forward validation, and look-ahead checks is a marketing tool, not an edge test.

| # | Deliverable | Status |
|---|---|---|
| 5.1 | `backtest/runner.py` — vectorbt-based backtesting runner | ✅ |
| 5.2 | Accepts any `BaseStrategy` + symbol(s) + date range | ✅ |
| 5.3 | **Slippage model**: configurable bps slippage on fills (default 5 bps) | ✅ |
| 5.4 | **Commission model**: configurable per-trade cost (Alpaca = 0, but framework supports non-zero) | ✅ |
| 5.5 | **Execution-timing convention**: signals generated on bar close → fills on *next bar's open* (no look-ahead) | ✅ |
| 5.6 | Stats output: total return, **CAGR, Sharpe, Sortino, max drawdown, profit factor, expectancy, trade count**. (Win rate included but de-emphasized — it's a noisy metric.) | ✅ |
| 5.7 | Equity curve + drawdown chart saved to `logs/backtests/<timestamp>_<strategy>.png` | ✅ |
| 5.8 | **Walk-forward validation harness** — split date range into rolling train/test windows; report out-of-sample performance separately from in-sample | ✅ |
| 5.9 | **Parameter sensitivity report** — run backtest across a grid of params (e.g. SMA fast 5-30, slow 30-200) and show the *distribution* of returns, not just the best point. Flag strategies where performance is a knife-edge. | ✅ |
| 5.10 | Verified: backtest SMA crossover on AAPL 2020–2025 with walk-forward split; print full stats | ✅ |

**Exit Criteria:** Backtest runs end-to-end with slippage, commission, and look-ahead-safe timing. Walk-forward and parameter-sensitivity reports are generated. Equity curve PNG saved.

**Note on survivorship bias:** for single-symbol backtests on large-cap liquid names (AAPL, etc.) survivorship is not a concern. If/when this bot trades a dynamic universe, revisit.

---

### Phase 6 — Risk Management
**Goal:** Risk rules are the *gatekeeper* between strategy signals and order placement.
Built before the broker so that the broker's `place_order()` API *requires* a validated
risk decision as input. Unsafe order placement is impossible by construction.

| # | Deliverable | Status |
|---|---|---|
| 6.1 | `risk/manager.py` — `RiskManager` class | ✅ |
| 6.2 | `RiskDecision` dataclass — the only legitimate input to `place_order()`; carries sized qty, stop price, reason | ✅ |
| 6.3 | Position sizing: fixed-fractional (% equity per trade, from `MAX_POSITION_PCT`) | ✅ |
| 6.4 | Max open positions limit | ✅ |
| 6.5 | Max daily loss circuit breaker (halt trading for the day if equity down `MAX_DAILY_LOSS_PCT` from session start) | ✅ |
| 6.6 | **Hard dollar kill switch** (absolute $ loss cap, not just %) — halt and log CRITICAL | ✅ |
| 6.7 | Per-trade ATR-based stop-loss calculation — stop price is **always defined before entering**, never post-hoc | ✅ |
| 6.8 | **Duplicate-order prevention** — refuse to open a new position if one already exists for the symbol (unless strategy explicitly supports pyramiding) | ✅ |
| 6.9 | **Loss-streak cooldown** — disable a strategy for N hours after M consecutive losses (configurable, per-strategy) | ✅ |
| 6.10 | **Broker-error-streak kill switch** — halt trading if broker API returns repeated errors within a window (suggests systemic issue) | ✅ |
| 6.11 | **Slippage-drift kill switch** — halt if realized slippage over the last K trades exceeds modeled slippage by a threshold (edge eroded or market broken) | ✅ |
| 6.12 | **Gross exposure cap** — cap total gross exposure at 30–50% of equity during initial live deployment (configurable) | ✅ |
| 6.13 | Unit tests in `tests/test_risk.py` covering every rejection path | ✅ (53 tests, 98% coverage on `risk/`) |

**Exit Criteria:** `RiskManager.evaluate(signal, account_state)` returns either a `RiskDecision` or a typed rejection reason. Every rule has a test that confirms it blocks a violating trade.

**Future extensions (Phase 11):** correlation limits across positions, per-sector capital caps,
per-strategy capital caps, dynamic sizing based on volatility regime. These are deferred
until multiple strategies exist — a single-strategy MVP doesn't benefit from them.

---

### Phase 7 — Broker Integration & Order Execution
**Goal:** Place, monitor, reconcile, and cancel orders on Alpaca paper account — with
the risk layer enforced by the API shape.

| # | Deliverable | Status |
|---|---|---|
| 7.1 | `execution/broker.py` — `AlpacaBroker` class | ✅ |
| 7.2 | **`place_order(decision: RiskDecision)` — requires a RiskDecision, cannot be bypassed** | ✅ |
| 7.3 | Methods: `get_account()`, `get_positions()`, `get_open_orders()`, `cancel_order()`, `close_position()` | ✅ |
| 7.4 | Support market and limit order types; bracket orders (entry + stop + optional target) for strategies with ATR stops. **Order type is chosen by the strategy** (see 4.8) — execution layer does not hard-code it. Hard-risk exits (stop-outs, circuit breakers) always use immediate/market orders regardless of strategy preference. | ✅ |
| 7.5 | **Order status polling with timeout + partial-fill handling** — return a typed result: filled, partial, rejected, timeout | ✅ |
| 7.6 | **State reconciliation helper** — `sync_with_broker()` queries Alpaca for positions + open orders; treated as source of truth | ✅ |
| 7.7 | **Rate-limit-aware retry** with exponential backoff on 429 / transient network errors | ✅ |
| 7.8 | Verified: place a paper market order for 1 share of AAPL via a `RiskDecision`, confirm fill, cancel a pending order | ✅ (2026-04-16) |

**Exit Criteria:** `AlpacaBroker.place_order()` successfully submits a paper trade via a `RiskDecision`. `sync_with_broker()` returns current truth. Partial fills and timeouts are handled, not crashed on.

---

### Phase 8 — Trading Engine (Main Loop)
**Goal:** Orchestrate all modules into a single runnable, restart-safe bot.

| # | Deliverable | Status |
|---|---|---|
| 8.1 | `engine/trader.py` — main trading loop | ✅ |
| 8.2 | On each cycle: **sync_with_broker → fetch data → freshness check → indicators → signals → risk check → execute → log** | ✅ |
| 8.3 | **Restart safety** — on startup, reconcile against broker state before any action; do not assume local state is valid | ✅ |
| 8.4 | **Stale-data guard** — refuse to trade if latest bar older than threshold (e.g. > 2× bar interval) | ✅ |
| 8.5 | Configurable run interval (per timeframe) | ✅ |
| 8.6 | Market hours check (only trade during regular session unless configured otherwise) | ✅ |
| 8.7 | Graceful shutdown on SIGINT — cancel open orders? (policy decision, configurable) | ✅ |
| 8.8 | **Exception containment** — any exception in the decision path → log ERROR, skip cycle, continue loop. Never crash on a data blip. | ✅ |
| 8.9 | Verified: bot runs 5+ full cycles on paper, logs all steps, survives simulated data fetch failure | ✅ (2026-04-16) |

**Exit Criteria:** `python -m engine.trader` runs complete paper trading cycles without errors, recovers from transient failures, and reconciles state correctly on restart.

---

### Phase 9 — Trade Reporting & P&L
**Goal:** Full observability into what the bot did and why. Logging infrastructure
(loguru + rotating files) was established in Phase 1; this phase builds the
trade-level and P&L reporting on top.

| # | Deliverable | Status |
|---|---|---|
| 9.1 | Structured JSON logging sink alongside human-readable console sink | ✅ |
| 9.2 | Trade log (SQLite `data/trades.db`): `timestamp, symbol, side, qty, price, order_id, strategy, reason, stop_price, modeled_slippage_bps, realized_slippage_bps` | ✅ |
| 9.3 | Daily P&L summary: realized + unrealized P&L, number of trades, largest win/loss, max intraday drawdown | ✅ |
| 9.4 | **Per-strategy attribution** — P&L, trade count, expectancy, profit factor broken out *per strategy* (even with one strategy today, the schema supports N strategies). Feeds strategy health monitoring in Phase 11. | ✅ |
| 9.5 | **Continuous slippage monitoring** — rolling comparison of realized vs. modeled slippage; feeds the Phase 6.11 slippage-drift kill switch | ✅ |
| 9.6 | Weekly summary report (markdown file) | ✅ |
| 9.7 | Alerts (log-file backend, pluggable for Slack/email) on: order rejection, circuit-breaker trip, loss-streak cooldown, broker-error-streak, stale data feed, slippage drift, engine halt. Duplicate suppression with configurable cooldown. | ✅ |
| 9.8 | Verified: logs + trade DB + daily summary + per-strategy attribution written correctly after a paper trade cycle | ✅ (2026-04-16) |

**Exit Criteria:** Every trade is in the SQLite DB with slippage data. Every day produces a P&L summary with per-strategy breakdown. All operator-critical events alert.

---

### Phase 9.5 — Forward-Test (Paper, Multi-Week)
**Goal:** The single most important gate before live money. Run the live paper bot for
multiple weeks and **reconcile realized fills against what the backtest predicted**.
If reality diverges significantly from backtest, the strategy does not go live.

| # | Deliverable | Status |
|---|---|---|
| 9.5.1 | Run paper bot continuously for **minimum 2 weeks, target 4 weeks**, on target strategy + symbols | ⬜ (run `python forward_test.py`) |
| 9.5.2 | `backtest/reconcile.py` — script that, given a date range, compares: (a) paper fills from trade DB vs. (b) backtest-predicted fills on the same bars | ✅ |
| 9.5.3 | Report per-trade divergence: price deviation in bps, matched/unmatched fills | ✅ |
| 9.5.4 | Report aggregate divergence: realized paper return vs. backtest return for the same window | ✅ |
| 9.5.5 | **Divergence decision gate** — return divergence threshold (10%) + mean slippage threshold (20bps). Auto go/no-go. | ✅ |
| 9.5.6 | Document the forward-test results and the go/no-go decision in `logs/forward_tests/<strategy>_<date>.md` | ✅ |
| 9.5.7 | `forward_test.py` — launcher script with full reporting wired up | ✅ |
| 9.5.8 | `get_closed_orders` on `AlpacaBroker` for fill history retrieval | ✅ |
| 9.5.9 | Verified: infrastructure verified against live paper — reconciler, report generation, engine wiring | ✅ (2026-04-16) |

**Exit Criteria:** Multi-week paper run completes; realized P&L reconciles with backtest expectations within committed threshold. Go/no-go decision documented.

---

### Phase 10 — Live Trading Transition
**Goal:** Safely switch from paper to live trading with multiple independent guardrails.

| # | Deliverable | Status |
|---|---|---|
| 10.1 | `config/settings.py` — `LIVE_TRADING` flag + separate `.env` keys for live vs. paper | ⬜ |
| 10.2 | Pre-flight checklist script: validates keys point to live endpoint, buying power, all risk params set, forward-test go/no-go on file | ⬜ |
| 10.3 | **Position-size multiplier** (default 0.25x) for the first N weeks of live trading | ⬜ |
| 10.4 | **Hard dollar kill switch** enabled with a conservative cap for initial live run (e.g. $500) | ⬜ |
| 10.5 | **Manual approval prompt** before the very first live order — prints order details, requires typed confirmation | ⬜ |
| 10.6 | **Dry-run mode** — one final option where engine runs live-connected but logs orders instead of placing them; used for the final sanity check | ⬜ |
| 10.7 | Separate live trade DB (never co-mingled with paper history) | ⬜ |
| 10.8 | **WebSocket order streaming** — replace `get_order()` polling with `alpaca-py` `TradingStream` for real-time order updates (fills, partial fills, rejections). With real capital, faster state updates reduce exposure to stale order state. Current REST polling is fine for paper but not acceptable for live. | ⬜ |
| 10.9 | **Enable slippage-drift kill switch** — set `SLIPPAGE_DRIFT_ENABLED=True` in `.env` after validating paper fill data confirms the 5 bps model and 3× multiplier thresholds are correctly calibrated. Currently disabled to avoid false halts during paper trading. | ⬜ |
| 10.10 | **Durable position ownership** — on restart, restore strategy→symbol ownership from trade DB (last open-but-not-closed trade per symbol) instead of best-effort slot ordering. Guards against misattribution in multi-strategy setups after a crash. See `TODO Phase 10` in `engine/trader.py`. | ⬜ |
| 10.11 | **Harden restart reconciliation** — if broker positions, open orders, and trade DB state cannot be fully reconciled on startup, log CRITICAL and enter a safe (no-new-entries) mode until operator confirms. Current behavior is best-effort. | ⬜ |
| 10.12 | Verified: bot connects to live Alpaca endpoint in dry-run, then with 1-share manual-approval order | ⬜ |

**Exit Criteria:** Bot can switch to live via config. First live order requires explicit human approval. Hard dollar cap is enforced. Paper and live data are never co-mingled. Order state is maintained via WebSocket streaming, not polling. Slippage-drift kill switch is enabled and calibrated.

---

### Phase 11 — Multi-Strategy, Regime Detection & Portfolio Layer
**Goal:** Once the single-strategy bot has demonstrated a baseline edge in live trading
(positive expectancy, survived a drawdown, execution quality as expected), expand to a
portfolio of 2–3 strategies with regime-aware gating and portfolio-level risk controls.

**Do not start this phase** until the single-strategy bot has run live for a meaningful
window (minimum 4–8 weeks live) with acceptable performance. Complexity is earned.

| # | Deliverable | Status |
|---|---|---|
| 11.1 | **Regime Detector module** (`regime/detector.py`) — classifies current state from inputs like realized vol, ATR expansion/contraction, MA slope, ADX, SPY vs. 200-day MA. Output: typed regime enum (trending / chop / volatile / low-vol). | ⬜ |
| 11.2 | Strategy registration declares which regimes it's allowed to run in. Engine only invokes strategies whose regime gate is currently true. | ⬜ |
| 11.3 | Second strategy: **Dip Buyer** (RSI-based mean reversion, gated to non-downtrend regime with strong long-term uptrend intact) | ⬜ |
| 11.4 | Third strategy *(optional)*: **Volatility Breakout** (range compression + breakout, gated to volatility-expansion regime) | ⬜ |
| 11.5 | **Per-strategy capital allocation** — each strategy gets a configurable share of equity; can't exceed its cap | ⬜ |
| 11.6 | **Correlation cap** — block new position if it would push correlated exposure (symbol-level or sector-level) above threshold | ⬜ |
| 11.7 | **Per-sector capital cap** (e.g. ≤ 40% in semiconductors) | ⬜ |
| 11.8 | **Strategy health monitor** — rolling expectancy + rolling Sharpe per strategy; automatic capital reduction or disable when performance degrades beyond a pre-committed threshold | ⬜ |
| 11.9 | **Strategy re-enable workflow** — disabled strategies require manual review + a fresh paper forward-test before being re-enabled with capital | ⬜ |
| 11.10 | Dashboard / report: per-strategy P&L, regime history, capital allocation over time | ⬜ |
| 11.11 | **Websocket streaming** (`data/stream.py`) — if any intraday strategy is added, replace REST polling with `alpaca-py` `StockDataStream` for real-time bars/quotes/trades. Daily-bar strategies can continue on REST polling; streaming is only needed when sub-minute latency matters. | ⬜ |

**Exit Criteria:** Bot runs 2–3 strategies concurrently, each gated to its appropriate regime, with portfolio-level caps enforced and automatic health-based capital adjustments. A degrading strategy is automatically throttled without operator intervention.

---

## Notes & Decisions Log

| Date | Note |
|---|---|
| — | Project initialized. Stack confirmed: Python 3.12, Alpaca, pandas, pandas-ta, vectorbt. |
| — | Starting with SMA crossover as the first strategy; architecture is strategy-agnostic. |
| — | Paper trading only until Phase 10. |
| 2026-04-14 | Phase 1 verified complete. Stack standardized on `alpaca-trade-api` v3.2.0 (legacy SDK) + `loguru`, not `alpaca-py` + stdlib logging. Directory layout: `execution/`, `strategies/`, `backtest/` (singular). `.env` lives in `config/.env`. CLAUDE.md updated to match. |
| 2026-04-14 | **Plan hardened for real-money operation.** Key changes: (a) Risk Management moved before Broker (Phases 6 ↔ 7) so `place_order()` requires a `RiskDecision` — unsafe path is impossible by construction. (b) Phase 2 gains a local Parquet cache + stale-data guard + rate-limit retry. (c) Phase 5 gains slippage/commission models, look-ahead-safe timing, walk-forward validation, and parameter-sensitivity reports. (d) New Phase 9.5 forward-test (2–4 weeks paper) with backtest reconciliation as a mandatory gate before live. (e) Phase 8 engine adds restart-safe broker reconciliation, stale-data guard, and exception containment. (f) Phase 10 adds hard dollar kill switch, position-size multiplier, dry-run mode. (g) Strategy signal convention switched from `{1,-1,0}` column to vectorbt-native entries/exits. Indicator set trimmed to SMA/EMA/ATR (YAGNI). Win rate de-emphasized vs. profit factor / expectancy. |
| 2026-04-14 | **Phase 3 complete.** Built `indicators/technicals.py` with `add_sma`, `add_ema`, `add_atr`. **Decision: hand-rolled, not pandas-ta.** pandas-ta is lightly maintained and has had pandas 2.x breakage; these three indicators are ~5 lines each, so the dependency isn't worth the risk on a real-money code path. EMA uses SMA-of-first-N seeding (Wilder convention); ATR uses Wilder's RMA smoothing. All functions are pure (return a copy). 25 unit tests with hand-computed expected values (tests function as spec, not just regression). Integration `phase3_verify.py` computes the full stack on 200d AAPL bars and validates shape/non-negativity/range bounds. Full suite: 60 tests passing in 0.32s. |
| 2026-04-14 | **Testing standard formalized.** Every phase now requires both (a) unit tests in `tests/test_*.py` (offline, fast, `pytest`) and (b) integration script `phase<N>_verify.py` (hits Alpaca paper). Pytest infrastructure: `pytest.ini` with `integration` marker (deselected by default), `tests/conftest.py` with offline fixtures (`make_ohlcv`, `clean_ohlcv`, `tmp_cache_dir` that redirects `CACHE_DIR` via monkeypatch). Rule: ≥80% coverage on pure logic; live-API wrappers exempt (covered by integration). Pinned `pytest`, `pytest-cov`, `freezegun` in requirements.txt. Added Testing Standard section to CLAUDE.md. |
| 2026-04-14 | **Phase 2 complete.** Built `data/fetcher.py` with per-symbol Parquet cache (`data/historical/{SYM}_{TF}_{ADJ}.parquet`) + sidecar meta (`.meta.json`) tracking the requested coverage window. Sidecar approach avoids phantom refetches at weekend/holiday boundaries (where first/last bar is strictly inside the requested range). Retry wrapper handles 429/5xx/network with exponential backoff. `is_fresh`/`require_fresh`/`StaleDataError` in place for Phase 8 live gates. Default adjustment=`all` (split + dividend) for clean backtests; IEX feed (paper-compatible). Verified via `phase2_verify.py` — 5/5 tests pass: cold fetch (61 rows, 1 API call), warm fetch (0 API calls), multi-symbol, freshness helpers, validation rejection paths, and range extension (wider window refetches only the new portion). Added `pyarrow` to requirements.txt. |
| 2026-04-15 | **Phase 5 complete.** Built `backtest/runner.py` on top of vectorbt 0.28.5 with `BacktestConfig` (init cash, slippage_bps default 5, commission_per_trade default 0) and `BacktestResult` (portfolio + stats + executed signals). **Key design decisions:** (a) **Look-ahead-safe execution lives in exactly one place** — `_shift_for_next_open` shifts strategy entries/exits forward by 1 bar; vbt then fills at `df["open"]` of that bar. Strategies stay aligned to signal-bar close, the runner is the single point of t→t+1 translation. (b) **Costs are mandatory** — non-zero defaults; explicit `slippage_bps=0` is the only way to get a no-cost backtest, so omitting costs by accident is impossible. (c) **Stats are honest** — Sharpe/Sortino/MaxDD via vbt; profit factor/expectancy/win-rate computed manually from `pf.trades.records_readable` for stability. Profit factor with zero losses → `inf` (standard convention). Win rate reported but de-emphasized in chart title. (d) **Walk-forward** uses sequential disjoint OOS folds (`np.array_split`); param-search/fitting deferred to Phase 11 (single-strategy MVP doesn't need it). (e) **Parameter sensitivity** sweeps cartesian grid, returns DataFrame of params + stats; invalid combos (e.g. SMA fast≥slow) silently skipped by default — surfaces robustness via *distribution*, not best-point. 23 unit tests including hand-pinned execution-timing test (signal at bar 2 → fill exactly at bar 3's open price), cost monotonicity (slippage/commission both reduce returns), profit-factor edge cases, walk-forward fold disjointness. Full suite: 107/107 passing in 6.8s. Integration `phase5_verify.py` on 5y AAPL daily: SMA(20,50) returns +29% / CAGR +5.2% / Sharpe 0.45 / MaxDD -28% / 15 trades over 5y; all 4 walk-forward folds positive; (5×5) param grid shows 88% of combos positive (median +37%, range -10% to +80%) — robust, not knife-edge. Equity/DD chart saved to `logs/backtests/`. |
| 2026-04-14 | **Phase 4 complete.** Built `strategies/base.py` (abstract `BaseStrategy`, frozen `SignalFrame` dataclass, `OrderType` enum, edge-filter hook) and `strategies/sma_crossover.py` (SMA crossover with param validation, vectorbt-native entries/exits). **Key design decisions:** (a) Signal convention is separate boolean entries/exits Series — not `{1,-1,0}` — directly consumable by vectorbt in Phase 5. (b) Look-ahead safety via `shift(1)`: cross-up requires yesterday's diff ≤ 0 AND today's > 0, so a monotonic uptrend (where fast>slow from the first bar both are defined) correctly emits **no** entry — no "before" state was ever observed. (c) Edge filter AND-gates entries but **never** blocks exits (always able to reduce risk). Missing/NaN gate values default to False ("regime not confirmed"). (d) Strategy declares `preferred_order_type` (MARKET for trend); Phase 7 execution reads this, hard-risk exits always override with market. 24 strategy unit tests including `TestLookAheadGuard` (truncate input at every cut point, assert past signals byte-identical). Full suite: 84/84 passing. Integration `phase4_verify.py` on 400d AAPL daily: 3 entries, 3 exits, look-ahead guard verified on real bars, edge filter reduces entries as expected. |
| 2026-04-16 | **Phase 7 complete.** Built `execution/broker.py` — `AlpacaBroker` is the only component that talks to Alpaca for order placement, and its `place_order(decision: RiskDecision)` raises `TypeError` on any other input — the Phase 6 risk gate is structurally enforced. **Key design decisions:** (a) **OTO not bracket.** Entries submit as `order_class="oto"` with a `stop_loss` leg; bracket would also require a take-profit which trend-following strategies don't have. The stop is live the moment the entry fills — there's no window where a position exists without protection. (b) **Typed terminal results.** `place_order` polls `get_order` until terminal (filled / rejected / canceled) or `poll_timeout`; partial fills at timeout surface as `PARTIAL` (not lost), no fills as `TIMEOUT`. The return is always an `OrderResult` with a defined `OrderStatus` — never a raw Alpaca object. (c) **`close_position` cancels sibling orders first.** Hard-risk exits would otherwise hit Alpaca's "insufficient qty available" error because the OTO stop_loss leg reserves the shares. A hard exit must not fail because of an attached stop. Caught by the live verify and pinned with a regression test. (d) **`sync_with_broker` returns a `BrokerSnapshot`** (account + positions + open_orders) — Phase 8's engine will call this at startup and on every cycle; the broker is the source of truth, never local state. (e) **Bridge to Phase 6:** `Signal` and `RiskDecision` gained `order_type: OrderType` (default MARKET) + `limit_price: float | None`, validated in both `Signal` (`INVALID_SIGNAL` rejection) and `RiskDecision.__post_init__` so a malformed limit decision is unconstructable. (f) **Retry wrapper** is broker-local (intentional duplication of `data/fetcher._with_retry` — both modules stay independently retry-aware): exponential backoff on 429 + 5xx + network; non-429 4xx raise immediately (our bug, not a transient blip). 25 unit tests with a mocked REST client (sleep patched out so the suite stays sub-second), covering: TypeError on non-RiskDecision, oto kwargs shape with rounded prices and unique client_order_id, every terminal-state mapping (filled / partial / timeout / rejected at submit / rejected after submit / canceled), polling that eventually sees fill, sibling-cancel before close_position, position normalisation, snapshot bundling, and retry on 429 / 503 / network with give-up after max_attempts. Full suite 185/185 passing in ~13s; broker.py at 96%, risk/ at 97% (uncovered lines are integration-only paths exercised by the verify scripts). Integration `phase7_verify.py` against live paper: 18/18 checks pass — risk-gate enforcement, market entry filled in <2s with stop_loss leg confirmed live in open_orders, far-from-market limit on MSFT submitted then canceled cleanly, and `close_position` liquidates AAPL after canceling the sibling stop. Account left clean. |
| 2026-04-15 | **Phase 6 complete.** Built `risk/manager.py` — `RiskManager` is the mandatory gate between strategy `Signal` and broker `place_order`. `RiskDecision` is the only legitimate input to Phase 7's `place_order` (frozen dataclass with self-validating `__post_init__`: positive qty, long-stop strictly below entry, etc.). **Key design decisions:** (a) **Stop is defined pre-entry** via `entry - k*ATR` (default k=2.0), then qty = floor(equity*MAX_POSITION_PCT / stop_distance), so $ loss-to-stop is bounded to MAX_POSITION_PCT regardless of symbol volatility. (b) **Multiple independent kill switches** (daily-loss %, hard $ cap, broker-error streak, slippage drift) — once any trips, the manager stays halted until operator-only `reset_kill_switches()`. (c) **Per-strategy loss-streak cooldown** (default 3 losses → 24h disable) is keyed by `strategy_name` so one bad strategy doesn't block others. (d) **Sizing also capped by gross exposure budget and cash on hand**, with distinct rejection codes for each so operator diagnostics are unambiguous (`GROSS_EXPOSURE_CAP` vs `INSUFFICIENT_CASH` vs `POSITION_TOO_SMALL`). (e) MVP is long-only; `SELL` side returns `UNSUPPORTED_SIDE` rather than silently sizing as a long. (f) State is in-memory; Phase 8/9 will hydrate from the trade log on restart — `RiskManager` API stays the same. 53 unit tests covering every rejection path + sizing math + kill-switch persistence + cooldown elapse + per-strategy isolation. Full suite 160/160 passing in ~13s; risk/ at 98% coverage. Integration `phase6_verify.py` runs against live Alpaca paper account ($100k equity, AAPL ATR ≈ $5.81): happy path approves 172 shares with $1,998.83 risk-to-stop (vs $2,000 cap), and all 8 rule families verify end-to-end (18/18 checks pass). |
| 2026-04-16 | **Phase 9.5 infrastructure complete.** Built `backtest/reconcile.py` — `Reconciler` compares paper fills (from trade CSV) against backtest predictions (rerun on the same bars) for a given date range. **Key design decisions:** (a) **Two-gate decision.** Return divergence gate (paper vs backtest total return, default threshold 10 percentage points) + slippage gate (mean realized slippage, default threshold 20bps). Either failing → NO-GO; the strategy returns to Phase 5 for re-analysis. (b) **Per-trade divergence.** Each paper fill is matched against the closest backtest entry/exit price. Price diff reported in bps with matched/unmatched flag. (c) **Paper return from CSV.** Sequential buy→sell pairs per symbol, P&L as fraction of entry cost. No position is assumed to carry across days if only one side is in the CSV. (d) **Report as markdown.** Written to `logs/forward_tests/<strategy>_<date>.md` with verdict (GO/NO-GO), aggregate returns, trade counts, slippage stats, gate reasons, and per-trade divergence table. (e) **`forward_test.py` launcher.** Starts the engine with production config (all 5 watchlist symbols, daily bars, 5-min cycles, market hours only) and full Phase 9 reporting wired up. On shutdown writes a daily P&L summary. Includes a docstring with the reconciliation command for after the multi-week run. (f) **`get_closed_orders`** added to `AlpacaBroker` — retrieves historical filled orders with optional date range and symbol filtering, returns typed `OrderResult` objects. (g) **Config** — 3 new settings: `FORWARD_TEST_DIR`, `FORWARD_TEST_RETURN_DIVERGENCE_PCT` (0.10), `FORWARD_TEST_MAX_SLIPPAGE_BPS` (20.0). 15 unit tests covering reconciliation logic, CSV filtering, gate decisions, report generation, and `get_closed_orders`. Full suite 256/256 passing in ~7s. Integration `phase95_verify.py` against live paper: 17/17 checks pass — config settings, closed order retrieval (10 historical orders found), reconciler produces a result (correctly NO-GO with 0 paper fills), report written, engine runs 3 cycles with forward-test wiring. **Remaining:** operational step — run `python forward_test.py` for 2-4 weeks, then reconcile. |
| 2026-04-16 | **Phase 9 complete.** Built `reporting/` package — three modules providing full observability into the bot's trading activity. **Key design decisions:** (a) **`TradeLogger`** (`reporting/logger.py`) writes an append-only CSV (`logs/trades.csv`) with 16 columns including both modeled and realized slippage in bps. Separate `build_record` (for entries via RiskDecision + OrderResult) and `build_close_record` (for exits without a RiskDecision). (b) **Structured JSON sink** via `install_json_sink()` — adds a JSONL loguru sink with 10MB rotation / 30-day retention alongside the human-readable console output. (c) **`PnLTracker`** (`reporting/pnl.py`) tracks intraday P&L in-memory (peak/trough for drawdown), generates `DailySummary` with per-strategy `StrategyStats` (trade count, P&L, win rate, expectancy, profit factor, slippage). Daily reports are markdown files in `logs/daily_pnl/`. Weekly reports aggregate daily summaries into `logs/weekly_reports/`. `slippage_report(last_n)` provides rolling slippage stats from the trade CSV. (d) **`AlertDispatcher`** (`reporting/alerts.py`) fires alerts through pluggable backends (MVP: `LogFileBackend` writing to `logs/alerts.log`). Supports 8 alert types (order rejection, circuit breaker, loss-streak cooldown, stale data, slippage drift, broker error, engine halt, position mismatch). Duplicate suppression via configurable cooldown (default 5 min per type+symbol+strategy key). Backend failures are caught and logged — an alert failure never crashes the bot. Slack/email backends can be added by subclassing `AlertBackend`. (e) **Engine integration** — `TradingEngine` now accepts optional `trade_logger`, `pnl_tracker`, and `alerts` (defaults created if not provided). Entry fills → `_log_entry` → CSV. Exit fills → `_log_close` → CSV. Risk rejections → `alerts.order_rejection`. Stale data → `alerts.stale_data`. Broker errors → `alerts.broker_error`. Halt state → `alerts.engine_halt`. (f) **Config** — 5 new settings in `config/settings.py` (TRADE_LOG_CSV, DAILY_PNL_DIR, WEEKLY_REPORT_DIR, JSON_LOG_FILE, ALERT_LOG_FILE). 35 unit tests (reporting/ at 94% coverage). Full suite 241/241 passing in ~8s. Integration `phase9_verify.py` exercises all 7 sections end-to-end on live paper: JSONL sink, trade CSV round-trip, daily P&L with per-strategy attribution, weekly report, all 7 alert types, slippage monitoring, and engine wiring with 3 live cycles; 23/23 checks pass. |
| 2026-04-16 | **Phase 8 complete.** Built `engine/trader.py` — `TradingEngine` orchestrates the full pipeline: `sync_with_broker → fetch → freshness → indicators → signals → risk → execute → log`. **Key design decisions:** (a) **Broker is the source of truth.** Every cycle starts with `broker.sync_with_broker()`; on startup the engine captures a snapshot before any decision, so a killed-mid-trade restart reconciles against reality. (b) **Stale-data guard.** `require_fresh(df, max_bar_age)` raises `StaleDataError` if the latest bar is older than `bar_interval × max_bar_age_multiplier` — silence beats wrong action. (c) **Exception containment at two levels.** Per-symbol errors are caught and logged (one flaky fetch doesn't skip the other symbols); per-cycle errors are caught (one bad cycle doesn't crash the loop). (d) **Redundant-close prevention.** `_has_pending_close_order` checks for an existing SELL order before issuing another close. (e) **Market hours gating.** Calls `broker._api.get_clock()` with retry; network failure falls back to "closed" (fail-safe). (f) **Graceful SIGINT/SIGTERM.** Sets `_running = False`; responsive sleep (1s tick) means shutdown latency ≤ 1s. Optionally cancels open orders on the way out (configurable). (g) **Slippage fed back to risk.** `_record_fill` computes realized vs. modeled slippage in bps and feeds `risk.record_fill_slippage` — the Phase 6.11 drift kill switch is wired end-to-end. (h) **Clock injection seam.** `_clock` callable lets tests control "now" without `freezegun`. (i) **`EngineConfig` frozen dataclass** with validation (bad timeframe, empty watchlist, invalid multiplier) and derived properties (`bar_interval`, `max_bar_age`). 21 unit tests (engine coverage 84%) covering config validation, per-symbol decision paths (entry/exit/stale/flat/halted/pending-close), cycle-level containment, start/stop lifecycle, shutdown order-cancel, and slippage recording. Full suite 206/206 passing in ~8s. Integration `phase8_verify.py` runs 5 cycles on live paper with a NoOpStrategy (never trades), injects a simulated fetch failure on cycle 3 — engine survives and completes all 5 cycles; 5/5 checks pass. |
| 2026-04-19 | **Future Alpaca features noted (not actionable now).** (a) **Trailing stop orders** — Alpaca supports trail_price / trail_percent on stop orders. Could replace fixed ATR stops for trend-following strategies (SMA crossover) to let winners run longer. Evaluate in Phase 11. (b) **VWAP/TWAP execution** — available via Elite Smart Router for minimizing market impact. Not relevant at current position sizes; revisit if scaling up. (c) **24/5 extended hours trading** — Alpaca supports overnight/extended hours. Current market hours gate correctly restricts to regular session, which is appropriate for daily-bar strategies. Available if a future strategy warrants it. |
| 2026-04-14 | **Integrated `trading-bot-design-guide-full.md` recommendations.** (a) Added explicit success metric: "positive expectancy with controlled drawdowns" — not "always profitable." (b) Expanded Guiding Principles with Risk>Entry, Boring beats fancy, One strategy first, Strategies decay. (c) Added 6-layer architectural mental model (Data → Regime → Strategy → Risk → Execution → Monitoring). (d) Phase 4 gains minimal edge-filter hook (e.g. SPY>200MA gate) and strategy-declared preferred order type. (e) Phase 6 gains loss-streak cooldown, broker-error-streak kill switch, slippage-drift kill switch, gross exposure cap. (f) Phase 7 clarifies strategy chooses order type; hard-risk exits always immediate. (g) Phase 9 gains per-strategy P&L attribution (even for single-strategy MVP — schema ready for N strategies) + continuous slippage monitoring feeding the 6.11 kill switch. (h) New **Phase 11** for multi-strategy + full regime detection + correlation/sector caps + strategy health auto-disable. Explicitly gated on "do not start until single-strategy live has proven out." Correlation/sector/per-strategy caps deferred there from Phase 6 as YAGNI for MVP. |
