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
| 10 | Live Trading Transition | 🔄 In Progress — SMA + RSI both paper-live; tagged `v1.0.0-beta.0`; blocked on ≥10 fills for slippage gate + 2-week combined run |
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
   are allowed to run. *(Minimal "edge filters" in Phase 4; production regime layer
   moves into Phase 10 before SMA + RSI can go live.)*
3. **Strategy** — generates entries and exits. Pure function of data. *(Phase 4)*
4. **Risk** — position sizing, stop placement, exposure caps, kill switches.
   **Mandatory gate between Strategy and Execution.** *(Phase 6)*
5. **Execution** — order submission, status tracking, fill reconciliation, retries. *(Phase 7)*
6. **Monitoring & Analytics** — logs, alerts, per-strategy attribution, health checks,
   slippage drift. *(Phases 1 + 9)*

---

## Watchlist Curation Philosophy

> *Source: Lynch, Peter — One Up on Wall Street (full read, 2026-04-19)*

Lynch classifies every stock into six types. The type determines which strategy fits
it — and using the wrong strategy on the wrong type is a primary source of losses.

| Lynch Type | Description | Best strategy fit |
|---|---|---|
| **Fast Grower** | 20–25% earnings growth, expanding into new markets | SMA crossover (multi-year uptrends) |
| **Stalwart** | Large, profitable, 10–12% growth, brand moat | SMA crossover (slower, steadier trends) |
| **Turnaround** | Battered/depressed, coming back | RSI reversion + fundamental filter |
| **Cyclical** | Autos, airlines, banks, chemicals — expand/contract with economy | RSI reversion at cycle troughs; **avoid** for SMA trend |
| **Slow Grower** | Mature, growing ~GNP rate, dividend-heavy utilities | Neither strategy — no edge |
| **Asset Play** | Undervalued assets on balance sheet | Neither strategy |

### Current watchlist assessment

| Symbol | Lynch Type | SMA fit | Flag |
|---|---|---|---|
| AAPL | Stalwart | ✅ | — |
| MSFT | Fast Grower → Stalwart | ✅ | — |
| GOOGL | Fast Grower → Stalwart | ✅ | — |
| AMZN | Fast Grower | ✅ | — |
| NVDA | Fast Grower (hot industry) | ⚠️ | AI narrative-driven; high P/E; sharp reversal risk if narrative breaks. Lynch Ch. 9: *"hot stocks in hot industries... the ones that go up the most come down the most"* |
| JPM | Financial Cyclical | ⚠️ | Earnings tied to rate/credit cycle; expect whipsaw on rate reversals |
| BAC | Financial Cyclical | ⚠️ | Same as JPM |
| GS | Financial Cyclical | ⚠️ | M&A/IPO revenues highly cyclical; trend collapses fast at cycle turn |
| DAL | Pure Cyclical (airline) | ❌ | Lynch's textbook example of the worst investment: no moat, commodity pricing, fuel/labour cost exposure. SMA crossover will be late to exit. Better candidate for RSI reversion at cycle trough. |
| PINS | Fading Fast Grower | ⚠️ | User growth decelerating; watch for PEG expansion |
| UBER | Turnaround → Fast Grower | ⚠️ | Now profitable, network moat exists; still early track record |
| COIN | Crypto Cyclical | ❌ | Boom/bust directly tied to crypto cycle, not earnings-driven trends. Extremely high volatility; SMA will give very late exit signals. Better for RSI reversion during crypto drawdowns. |
| RIVN | Pre-profit / Whisper Stock | ❌ | No earnings, cash-burning. Lynch Ch. 15: *"I've lost money on every single whisper stock I've ever bought."* No P/E ratio, no fundamental trend anchor. Remove before going live. |

### Rules for watchlist changes

- **Do not change the watchlist mid-paper-run** — it invalidates the forward-test
  reconciliation comparison. Review and revise *after* the paper run concludes and
  *before* Phase 10 live flip.
- **Pre-screen for live watchlist** (Lynch Ch. 13): positive earnings for ≥ 2 consecutive
  years, PEG ratio < 1.5 at entry, no active bank-debt crisis, not in a single sector
  with > 3 other symbols.
- **Cyclicals (DAL, COIN) are not removed — they are relabelled.** Keep them as
  RSI mean-reversion candidates for the Phase 10 SMA + RSI paper gate; remove them
  from the SMA crossover slot.
- **RIVN** should be removed entirely before going live. Pre-profit companies provide
  no earnings anchor for a sustained trend.
- **Per-strategy watchlist wiring (Phase 10 item 10.B3):** after the current SMA-only paper run, update `forward_test.py` so the SMA slot uses `settings.SMA_WATCHLIST` and the RSI slot uses `settings.RSI_WATCHLIST`. Each strategy watches only its own symbol universe. `WATCHLIST` stays in settings as a convenience union for review scripts only — it must not drive any strategy slot.

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
| 4.7 | **Edge-filter hook** — strategies can optionally require a market-regime condition (e.g. `SPY > 200-day MA`) before emitting long entries. Implemented as a composable filter, not hard-coded. This is the minimal regime awareness for the first strategy; the production regime detector is Phase 10. Strategy-specific filters should live in `strategies/filters/<strategy_name>.py`, with reusable helpers in `strategies/filters/common.py`. | ✅ |
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

**Note on limit order fill realism *(Hilpisch, Ch. 6 — Event-Based Backtesting)*:**
Vectorized backtesting (vectorbt) assumes a limit order fills whenever price touches the
limit level during a bar. In reality a limit order may sit unfilled, partially fill, or be
skipped entirely if price gaps through it. For market-order strategies like SMA crossover
this is not a concern. For **RSI reversion**, which relies on limit entries, vectorized
backtesting will systematically overestimate fill rates and therefore overestimate returns.
When RSI reversion is activated for the Phase 10 SMA + RSI paper gate, consider supplementing the vectorbt backtest
with an event-based backtester that processes each bar and explicitly checks whether the
limit was touched and held long enough to fill. See `backtest/runner.py` for the existing
harness; an event-based variant would sit alongside it, not replace it.

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

**Interim SMA sizing guardrail:** `MAX_POSITION_NOTIONAL_PCT` caps each position's
notional size so a tight ATR stop cannot let one SMA entry consume the whole gross
exposure budget. This is a paper-run crutch for the SMA-only forward test, not the
final portfolio model.

**Future extensions:** fixed per-strategy capital caps and minimum concentration guardrails
move into Phase 10 before SMA + RSI can go live. Dynamic sizing based on volatility regime,
advanced correlation limits, and automatic capital reallocation remain Phase 11 enhancements.

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

**Broker note:** If/when fractional equity execution is enabled, Alpaca supports
fractional orders only with `DAY` time in force. `GTC` protective stops and
`GTC` entry orders therefore remain a whole-share-only path unless fractional
execution gets an explicit Alpaca-specific fallback.

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
**Goal:** Stabilize the paper bot under real market conditions and gather evidence
about SMA behavior before Phase 10. Run the live paper bot for multiple weeks and
**reconcile realized fills against what the backtest predicted**. If reality diverges
significantly from backtest, return to Phase 5/6 analysis before expanding the system.

This is **not** the final live-capital GO/NO-GO gate. The final gate happens after
Phase 10 hardening, with both SMA and RSI active in paper mode, fixed strategy
capital sleeves, durable ownership, startup reconciliation, and the remaining
pre-live safeguards implemented.

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

**SMA-only sample-size note:** a daily SMA trend strategy may not produce 50
closed trades during a 2-4 week paper run. For Phase 9.5, the primary gate is
paper-vs-backtest reconciliation over the same bars plus operational stability.
The 50 closed-trade threshold remains a stricter live-readiness/statistical gate,
not a realistic expectation for the current SMA-only forward test.

**Exit Criteria:** The SMA paper run is operationally stable, startup/shutdown/restart
behavior is clean, realized fills reconcile with backtest expectations within the
committed threshold, and any defects discovered during paper trading are fixed or
promoted into Phase 10 blockers.

---

### Phase 10 — Live Trading Transition
**Goal:** Safely switch from paper to live trading with multiple independent guardrails.

**Implementation order matters.** Items are grouped by dependency and risk. Do not
skip ahead. Each group must be paper-validated before the next begins.

---

#### Blocker classification

| Label | Meaning |
|---|---|
| 🔴 HARD BLOCKER | Bot must not go live without this |
| 🟡 REQUIRED | Must be done in Phase 10, not blocking start but blocking flip to live |
| 🟢 LIVE GATE | Final checks run immediately before the live flip |

---

#### Group A — Manual prerequisite (no code, do first)

| # | Deliverable | Complexity | Status |
|---|---|---|---|
| 10.A1 | **Manual restart verification** — with at least one open paper position: stop the bot, restart it, confirm startup logs show ownership assignment (`restart: assigned existing position X → 'sma_crossover'`) and that any unmanaged symbol emits a WARNING. Instructions in CLAUDE.md. | Operational | ✅ (2026-04-24: MU + NVDA restored from trade DB record; NORMAL mode confirmed) |

---

#### Group B — Config and separation (low risk, do next)

| # | Deliverable | Complexity | Blocker | Status |
|---|---|---|---|---|
| 10.B1 | **Live config separation** — `LIVE_TRADING` flag in `config/settings.py`; separate `.env` keys for live vs. paper (`ALPACA_API_KEY_LIVE`, `ALPACA_SECRET_KEY_LIVE`); separate trade DB path (`data/trades_live.db`) so paper and live fills are never co-mingled. | Low (~20 lines) | 🔴 | ✅ (2026-04-24) |
| 10.B2 | **Pre-flight checklist script** — validates: keys point to live endpoint, buying power meets minimum, go/no-go file on disk with GO verdict, all risk params set, `SLIPPAGE_DRIFT_ENABLED=True`, dry-run passes. Exits non-zero if any check fails. | Low (~50 lines) | 🟡 | ✅ (2026-04-24) |
| 10.B3 | **`WatchlistSource` abstraction + per-strategy wiring** — introduce `data/watchlists.py` with a `WatchlistSource` base class (`name: str`, `symbols() -> list[str]`) and a `StaticWatchlistSource` implementation. `StrategySlot` accepts a `WatchlistSource` instead of a raw list and calls `.symbols()` each cycle — it has no knowledge of whether the source is static or dynamic. Wire `forward_test.py`: SMA slot → `StaticWatchlistSource(settings.SMA_WATCHLIST)`, RSI slot → `StaticWatchlistSource(settings.RSI_WATCHLIST)`. `WATCHLIST` stays in settings as a convenience union for review scripts only. `DynamicWatchlistSource` (calls a configurable scanner module and expects a `list[str]` back) is deferred to Phase 11 — dynamic sources require durable ownership to be proven stable first so a symbol rotating out of the scanner while a position is open is handled correctly. Unit tests: `StaticWatchlistSource.symbols()` returns the configured list; `StrategySlot` calls the source rather than holding a raw list; SMA and RSI slots return their respective symbols with no cross-contamination. | Low (~40 lines) | 🔴 | ✅ (2026-04-24) |

---

#### Group C — Safety hardening (🔴 hard blockers, implement + paper-validate before live)

| # | Deliverable | Complexity | Blocker | Status |
|---|---|---|---|---|
| 10.C1 | **Durable position ownership** — on restart, restore `_position_owners` from the trade DB instead of best-effort slot-order matching. See design below. | Medium (~60 lines) | 🔴 | ✅ (2026-04-24) |
| 10.C2 | **Startup reconciliation + fail-safe mode** — cross-check broker positions, open orders, trade DB, and ownership state on startup. Enter RESTRICTED mode (exits only, no new entries) on any medium mismatch; HALT on critical mismatch. See design below. | Medium (~80 lines) | 🔴 | ✅ (2026-04-24) |
| 10.C3 | **Tests for 10.C1 and 10.C2** — restart with pre-existing broker positions; durable ownership restored correctly; reconciliation detects and classifies mismatches. | Medium | 🔴 | ✅ (2026-04-24) |
| 10.C4 | **External close detection with confirmation window** — each cycle, cross-check `_position_owners` against broker positions. A position absent for `ENGINE_EXTERNAL_CLOSE_CONFIRM_CYCLES` (default 3) consecutive cycles is declared externally closed (stop-out, manual liquidation, margin call): log WARNING, fire alert, write synthetic sell to trade DB (closes stale buy record so restarts are not misled), clear ownership. Confirmation window guards against transient broker API blips returning incomplete data. With WebSocket order streaming (10.E1), genuine closes are detected via fill events; this method becomes a fallback for WebSocket gap periods only. | Low (~50 lines) | 🔴 | ✅ (2026-04-24) |

> **Paper validation gate (active — started 2026-04-24):** Groups A–C (including 10.C4
> external close detection) are complete and the bot is running with the new startup
> reconciliation. Run for at least one week, confirm startup logs are clean on each restart,
> and verify no false external-close detections before proceeding to Group D.

> **Allocator dependency:** Do not enable per-strategy capital buckets until 10.C1 and
> 10.C2 are complete. Bucket accounting depends on durable ownership because every
> open broker position must map unambiguously to exactly one strategy after restart.

---

#### Group D — Slippage kill switch calibration (requires paper fill data)

| # | Deliverable | Complexity | Blocker | Status |
|---|---|---|---|---|
| 10.D1 | **Review paper fill data** — query `data/trades.db`, compute mean realized slippage across all fills. If mean realized ≤ 3× modeled (5 bps), the current thresholds are calibrated. If not, adjust `SLIPPAGE_MODEL_MARKET_BPS` in `config/settings.py` before enabling. | Operational | 🔴 | ⬜ |
| 10.D2 | **Enable slippage-drift kill switch** — set `SLIPPAGE_DRIFT_ENABLED=True` in `config/.env`. Requires 10.D1 to confirm thresholds are reasonable. Kill switch must be active for live trading. | Low (1 config line) | 🔴 | ⬜ |

---

#### Group E — Infrastructure (highest complexity, do after C and D)

| # | Deliverable | Complexity | Blocker | Status |
|---|---|---|---|---|
| 10.E1 | **WebSocket order streaming** (`alpaca-py` `TradingStream`) — replace `_poll_until_terminal` REST polling with a stream handler for real-time fill/rejection/partial-fill events. Also wires stop-leg fills into `_record_fill` so slippage tracking covers OTO exits. Current REST polling is acceptable for paper but not live. | High (~200 lines, async) | 🔴 | ✅ (2026-04-24) |

> **Paper validation gate:** After implementing 10.E1, run the paper bot for at least one
> week with streaming active. Confirm stop-leg fills appear in slippage samples and that
> the stream reconnects cleanly after network interruptions before proceeding to Group F.

---

#### Group F — Pre-live multi-strategy portfolio layer (hard blockers for SMA + RSI)

| # | Deliverable | Complexity | Blocker | Status |
|---|---|---|---|---|
| 10.F1 | **Per-strategy capital allocation** — `SleeveAllocator` (`risk/allocator.py`): computes per-strategy gross-notional budgets from `equity × MAX_GROSS_EXPOSURE_PCT × weight`. 50/50 split, `MAX_GROSS_EXPOSURE_PCT` raised to 0.80 (was 0.50). Idle sleeve capital stays locked (no borrowing). Open limit orders count at full notional. `RiskManager.evaluate()` gains `notional_cap` param; `_size_position` caps qty at remaining sleeve. Engine gains `allocator` param; `_attribute_orders()` maps order_id → strategy for pending buys; sleeve check sits before `risk.evaluate()` in entry branch. `forward_test.py` wired. 28 allocator tests. | Medium | 🔴 | ✅ (2026-04-25) |
| 10.F2 | **Regime Detector module** (`regime/detector.py`) — four regimes: BEAR (SPY < SMA200), VOLATILE (ATR% > 80th pct of 126-bar window), TRENDING (ADX ≥ 25), RANGING (ADX ≤ 20; ambiguous zone uses SMA50 slope). TTL-cached (10 min). Fail-safe: last cached regime or RANGING if SPY unavailable. `add_adx()` + `_wilder_rma()` added to `indicators/technicals.py`. `StrategySlot.allowed_regimes` frozenset field added. Engine calls `detect()` once per cycle; per-slot gate blocks new entries only (exits never blocked). `forward_test.py` wired with SMA slot allowing TRENDING + RANGING. 35 regime unit tests. | Medium | 🔴 | ✅ (2026-04-25) |
| 10.F3 | **Strategy regime registration/gating** — `StrategySlot.allowed_regimes` frozenset; engine per-slot check in `_run_one_cycle`; `entry_allowed` flag threaded through `_process_symbol`. Completed as part of 10.F2. | Medium | 🔴 | ✅ (2026-04-25) |
| 10.F3a | **SMA edge filter** — `strategies/filters/sma_crossover.py`. Three entry gates: (1) **SPY > 200 SMA** — macro regime; (2) **stock close > stock 200 SMA** — structural strength, avoids crossovers in structurally weak names; (3) **10-day avg volume > 30-day avg volume** — confirms institutional participation. Earnings blackout excluded (trend-following benefits from earnings catalysts). All gates fail open on insufficient history. `ENGINE_HISTORY_LOOKBACK_DAYS` bumped to 300 (≈206 trading days) to support the stock 200-day SMA gate. Deferred to Phase 11: RSI-at-entry overbought gate (>70) and same-day concentration cap on correlated signals. 51 filter unit tests. | Medium | 🔴 | ✅ (2026-04-25) |
| 10.F3b | **RSI edge filter** — `strategies/filters/rsi_reversion.py`. Four entry gates: (1) **SPY > 200 SMA AND SPY > 50 SMA** — dual macro gate; (2) **earnings blackout (3 days before / 2 days after)** — binary events and post-earnings follow-through; (3) **20-day avg volume ≥ 500K shares** — liquidity floor for limit-order fill quality; (4) **no new 20-day low** — blocks active individual stock breakdowns that the SPY gates cannot see. Stock 50-day SMA gate intentionally excluded: oversold RSI stocks are typically below their 50 SMA — filtering there removes exactly the trades the strategy is designed to take. Observability: `RSI_FILTER_ALLOWED` / `RSI_FILTER_BLOCKED` logged per signal with specific reasons. Phase 11 deferred: SPY 50 SMA cliff-edge smoothing (11.23). 58 filter unit tests. | Medium | 🔴 | ✅ (2026-04-25) |
| 10.F4 | **RSI paper activation slot** — add `RSIReversion` to the paper engine with `settings.RSI_WATCHLIST`, RSI edge filter (10.F3b) wired in, strategy-specific allocation, limit-order behavior, and attribution verified. Do not enable live. | Medium | 🔴 | ✅ (2026-04-25) |
| 10.F5 | **Minimum portfolio concentration guardrails** — deferred to Phase 11. Rationale: SMA and RSI watchlists have zero symbol overlap so shared-symbol double-exposure is moot; sector concentration risk is partially mitigated by the sleeve cap (max 5 positions × $8k) and regime gating; the real fix is watchlist curation rather than a code guardrail. See 11.6/11.7. | N/A | Moved to 11.6/11.7 |
| 10.F6 | **Verified** — unit tests pass; startup reconciliation recomputes sleeve exposure before first entry; paper logs show both SMA and RSI gated by regime and allocation. | Operational | 🔴 | ⬜ |

> **Pre-live SMA + RSI paper gate:** After Groups A-F are complete, run the combined
> SMA + RSI bot in Alpaca paper mode for **minimum 2 weeks, target 4 weeks**. This is
> separate from the current SMA-only Phase 9.5 run because durable ownership, startup
> reconciliation, TradingStream order state handling, RSI limit orders, regime gating,
> and per-strategy allocation all change the execution surface. The run must produce a
> documented GO/NO-GO report before any SMA + RSI live flip.

---

#### Group G — Live trading gates (run immediately before the live flip)

| # | Deliverable | Complexity | Blocker | Status |
|---|---|---|---|---|
| 10.G1 | **Position-size multiplier** — `LIVE_SIZE_MULTIPLIER = 0.25` config setting; risk manager scales final `qty` by this when `LIVE_TRADING=True`. Limits exposure while calibrating live fills. | Low (~15 lines) | 🟢 | ✅ (2026-04-24) |
| 10.G2 | **Hard dollar cap** — set `HARD_DOLLAR_LOSS_CAP` to a conservative value (e.g. $500) in the live `.env`. Already implemented in RiskManager; this is a config decision only. | Low (config) | 🟢 | ⬜ |
| 10.G3 | **Manual approval prompt** — dropped; not needed given planned test coverage before going live. | N/A | N/A | 🚫 dropped |
| 10.G4 | **Dry-run mode** — `DRY_RUN=True` config flag; broker logs orders instead of placing them. Final sanity check before real orders. | Low (~15 lines) | 🟢 | ✅ (2026-04-24) |
| 10.G5 | **Verified** — pre-flight checklist passes; dry-run connects to live Alpaca endpoint and logs at least one cycle; first real order requires manual approval; hard cap enforced. | Operational | 🟢 | ⬜ |
| 10.G6 | **Fractional share sizing** — `FRACTIONAL_ENABLED` flag in `config/settings.py`. When True: `risk/manager.py` uses `math.floor(x * 100) / 100` (2 dp) for MARKET orders; LIMIT/GTC orders always use `math.floor()`. `execution/broker.py` routes fractional qty (floor(qty) ≠ qty) to `_place_fractional_order()`: DAY entry + standalone GTC stop for floor(qty) whole shares. Live-size multiplier updated to use fractional floor when applicable. When False: byte-for-byte identical to original whole-share behaviour. Disable once account > ~$10k. Unit tests: 5 broker tests + risk sizing tests updated. | Medium (~80 lines) | 🟢 | ✅ (2026-04-25) |

---

#### Design: Durable position ownership (10.C1)

The trade DB already records `strategy` on every fill. On restart, query net open
position per (symbol, strategy) pair instead of guessing from slot ordering.

**Query:**
```sql
SELECT symbol, strategy,
       SUM(CASE WHEN side='buy' THEN qty ELSE 0 END) -
       SUM(CASE WHEN side='sell' THEN qty ELSE 0 END) AS net_qty
FROM trades
WHERE symbol = ?
GROUP BY symbol, strategy
HAVING net_qty > 0
ORDER BY MAX(timestamp) DESC
LIMIT 1;
```

**Startup flow:**
```
For each open broker position:
  a. Run query against trade DB
  b. If net_qty > 0 found → assign ownership from DB, log INFO
  c. If no DB record → fallback to slot-order match, log WARNING ("fallback used")
  d. If matches no slot at all → log WARNING ("unmanaged, will not be traded")
  e. If two strategies both show net_qty > 0 → log CRITICAL, engage kill switch
```

**Edge cases:**
- Partial fill with no exit: net_qty = filled qty, still positive → correctly open
- Position opened before DB existed: no rows → falls through to slot-order fallback
- Manually opened position (dashboard): no rows → fallback → WARNING
- DB missing/corrupt: catch all exceptions, skip DB lookup, fallback for all symbols, log ERROR

**Implementation location:** New `_restore_ownership_from_db()` method on `TradingEngine`,
called from `start()` after `broker.sync_with_broker()`. See `TODO Phase 10` comment
in `engine/trader.py`.

---

#### Design: Startup reconciliation + fail-safe mode (10.C2)

**Four sources cross-checked at startup:**
- `broker_positions`: symbols with open shares (from broker snapshot)
- `broker_open_orders`: symbols with pending orders (from broker snapshot)
- `db_open_positions`: (symbol, strategy) pairs with net_qty > 0 (from trade DB)
- `engine_owners`: what `_restore_ownership_from_db()` assigned

**Mismatch classification:**

| Check | Severity | Engine behavior |
|---|---|---|
| Broker position, DB record, slot match → all agree | — | NORMAL |
| Broker position, no DB record | MEDIUM | RESTRICTED: log WARNING, assign fallback |
| Broker position, no matching slot | MEDIUM | RESTRICTED: log WARNING, skip symbol |
| DB open record, no broker position | LOW | Log INFO (position closed elsewhere) |
| Orphan stop order (open order, no position) | MEDIUM | RESTRICTED: log WARNING |
| Two DB strategies claim same symbol net_qty > 0 | HIGH | HALT: log CRITICAL, engage kill switch |

**Startup modes:**
```
All checks clean    → NORMAL  (full operation)
Any MEDIUM issue    → RESTRICTED (existing positions managed, no new entries)
Any HIGH issue      → HALT (no new entries or exits; broker stops still protect positions)
```

**Fail-safe rules:**
1. Exits always fire in RESTRICTED mode — never block risk reduction
2. HALT mode requires operator `reset_kill_switches()` to clear
3. RESTRICTED mode auto-clears after one clean cycle (MEDIUM issues may self-heal
   as positions close naturally)
4. Reconciliation verdict logged in startup summary and written to alert log
5. Broker OTO stop legs remain active regardless of engine mode — they protect
   positions even if the engine is fully halted

**Implementation location:** New `_reconcile_startup()` method on `TradingEngine`,
called from `start()` after `_restore_ownership_from_db()`. Returns
`"NORMAL" | "RESTRICTED" | "HALT"`. Engine stores mode in `self._startup_mode`
and checks it before opening new entries.

---

#### Design: Per-strategy capital allocation (10.F1)

Use a conservative sleeve model before introducing dynamic optimizers. The first
implementation allocates from a total portfolio budget into fixed strategy buckets,
while preserving existing global kill switches.

**Baseline model:**
- `TOTAL_ALLOCATABLE_GROSS_PCT` caps strategy-managed gross exposure as a share of equity.
  Start at the existing `MAX_GROSS_EXPOSURE_PCT` value unless the go/no-go review says otherwise.
- `STRATEGY_ALLOCATIONS` maps strategy name to target sleeve weight, e.g.
  `{"sma_crossover": 0.60, "rsi_reversion": 0.40}`.
- A strategy's max gross notional is `equity * TOTAL_ALLOCATABLE_GROSS_PCT * strategy_weight`.
- Existing global caps remain authoritative: hard dollar cap, daily loss cap, broker-error
  kill switch, slippage-drift kill switch, total gross exposure cap, and cash availability.
- Per-strategy caps add an inner guardrail: max sleeve notional, max open positions per
  strategy, and max loss-to-stop per sleeve.

**Accounting source of truth:**
- Open strategy exposure must be computed from broker positions plus durable ownership
  restored from the trade DB. In-memory `_position_owners` alone is not sufficient.
- If any open position cannot be mapped to exactly one strategy, enter exits-only
  restricted mode and block new entries until the mismatch is resolved.
- Shared symbols are allowed only when ownership is durable. One Alpaca position per
  symbol means the first implementation should continue to block simultaneous ownership
  of the same symbol by multiple strategies unless sub-position accounting is added.

**Order flow:**
- Strategies continue to emit signals only.
- A portfolio/allocation layer converts a signal into a strategy-scoped budget
  constraint before `RiskManager.evaluate()` sizes the final order.
- `RiskManager` still enforces final safety checks using the full account state; the
  allocator narrows available capital, it never bypasses risk.

**Tests required before enabling SMA + RSI paper multi-strategy:**
- Strategy A cannot consume Strategy B's sleeve budget.
- Strategy A can trade while Strategy B is in loss-streak cooldown, assuming global
  risk is healthy.
- A full sleeve rejects new entries with a distinct allocator rejection reason.
- Restart restores ownership from DB, recomputes sleeve exposure, and preserves bucket
  limits before the first new entry.
- Global gross cap still rejects trades even when the individual strategy sleeve has room.

---

**Exit Criteria:** Pre-flight checklist passes. Bot connects to live Alpaca in dry-run
mode. Position ownership is restored from trade DB on restart. Startup reconciliation
is clean or RESTRICTED with operator awareness. Slippage kill switch is enabled and
calibrated against paper fills. WebSocket order streaming is active and paper-validated.
Per-strategy capital allocation, regime detection, strategy gating, and RSI paper
activation are implemented. The combined SMA + RSI paper run completes for minimum
2 weeks, target 4 weeks, with a documented GO/NO-GO report. First real order requires
explicit typed approval. Hard dollar cap ($500 initial) enforced. Paper and live
trade DBs are separate files.

---

#### Group H — Cloud Infrastructure *(Deferred — requires a VPS)*

> **Why deferred:** Running live trading from a local Mac with tmux + caffeinate is
> not appropriate for real capital. A power outage, sleep cycle, or Wi-Fi drop kills
> the process and can leave open positions unmanaged. This group is not a blocker for
> the paper run or the initial live flip, but **must be resolved before sustained live
> operation** (i.e. within the first few weeks of live trading).
>
> *Hilpisch, Python for Algorithmic Trading, Ch. 10:* "A simple loss of the web
> connection or a brief power outage might bring down the whole algorithm, leaving,
> for example, unintended open positions in the portfolio."

| # | Deliverable | Complexity | Status |
|---|---|---|---|
| 10.H1 | **Provision a cloud VPS** — DigitalOcean, Hetzner, Linode, or equivalent. Minimum spec: 1 vCPU, 1 GB RAM, SSD, reliable uptime SLA (99.9%+). Prefer a region close to Alpaca's data centers (US East). | Operational | ⬜ |
| 10.H2 | **Systemd service unit** — `trading-bot.service` that starts `python forward_test.py` on boot, restarts automatically on crash (`Restart=always`, `RestartSec=10`). Replaces `start_bot.sh` + tmux for production. | Low (~20 lines config) | ⬜ |
| 10.H3 | **Secure key management on VPS** — `config/.env` copied to the VPS via `scp` (never committed). SSH key-only access. Firewall (`ufw`) allowing only SSH inbound. | Operational | ⬜ |
| 10.H4 | **Remote monitoring** — `reporting/monitor.py` ZeroMQ PUB socket that publishes cycle events (fills, rejections, kill-switch trips, halts). Companion `scripts/monitor_client.py` SUB script runs locally. Allows real-time observation without SSH. See Hilpisch Ch. 10 pattern. | Medium (~80 lines) | ⬜ |
| 10.H5 | **Log shipping** — either `rsync` cron or a lightweight agent to pull `logs/` from the VPS daily. Ensures logs survive a VPS rebuild. | Low | ⬜ |

**Notes:**
- Until 10.H1–10.H3 are done, keep `start_bot.sh` (tmux) as the launch mechanism and
  accept the local-machine reliability risk during early live operation.
- 10.H4 (ZeroMQ monitor) can be implemented locally before the VPS is provisioned —
  it will still work over `localhost` for local observation during the paper run.
- `systemd` is Linux-only; on the VPS this replaces caffeinate entirely.

---

### Phase 11 — Advanced Multi-Strategy Portfolio Enhancements
**Goal:** After the Phase 10 SMA + RSI pre-live gate is complete, improve portfolio
intelligence beyond the minimum safe two-strategy launch: optional third strategy,
dynamic allocation, richer concentration controls, health-based throttling, reporting,
and intraday-specific infrastructure if needed.

**Boundary:** The critical pre-live subset moved into Phase 10 Group F. Regime detection,
strategy regime gating, RSI paper activation, fixed per-strategy allocation, and minimum
concentration guardrails are no longer Phase 11 nice-to-haves; they are blockers before
SMA + RSI can trade live. Phase 11 should not be used to smuggle new complexity into the
pre-live checklist unless the item is promoted back into Phase 10 with a clear blocker
reason.

| # | Deliverable | Status |
|---|---|---|
| 11.1 | **Regime Detector module** — moved to Phase 10 Group F because regime gating is required before SMA + RSI can trade live. | Moved to 10.F2 |
| 11.2 | **Strategy regime registration/gating** — moved to Phase 10 Group F. Exits remain always allowed; entries are regime-gated. | Moved to 10.F3 |
| 11.3 | **Second strategy: Dip Buyer / RSI Reversion paper activation** — moved to Phase 10 Group F for the mandatory SMA + RSI paper gate. | Moved to 10.F4 |
| 11.4 | **`DynamicWatchlistSource`** — implement the dynamic variant of the `WatchlistSource` abstraction built in 10.B3. Config declares the source as dynamic and points to a scanner module (e.g. `scripts.rsi_watchlist_scan`); `DynamicWatchlistSource` calls the module and expects a `list[str]` back. Refresh cadence is configurable (daily recommended). Requires durable ownership (10.C1) and startup reconciliation (10.C2) to be proven stable — a symbol rotating out of the scanner while a position is open must not silently abandon the position. Guardrails: cache every generated list with timestamp and rule version; log rejection counts per filter; cap daily turnover; run in report-only mode before allowing the source to drive a live slot. | ⬜ |
| 11.4b | Third strategy *(optional)*: **Volatility Breakout** (range compression + breakout, gated to volatility-expansion regime) | ⬜ |
| 11.5 | **Dynamic per-strategy capital allocation and cross-sleeve borrowing.** Fixed 50/50 sleeves are the Phase 10 starting point — correct for an unknown baseline, wrong if one strategy demonstrably outperforms the other over a long run. Phase 11 adds two enhancements: (1) *Evidence-based weight rebalancing* — after ≥ 4 weeks of combined SMA + RSI paper data (minimum viable sample), compare per-strategy rolling Sharpe ratio and expectancy from the trade DB. If one strategy's Sharpe is materially higher (threshold TBD, e.g. > 0.3 Sharpe difference sustained over 20+ trades), shift sleeve weights toward it in configurable steps (e.g. 5% increments, floor 20%, ceiling 80%). Rebalancing is logged and requires a human sign-off before the first live weight change — automated rebalancing is paper-only until the mechanism is validated. (2) *Controlled cross-sleeve borrowing* — today idle RSI sleeve capital is completely locked even when the SMA sleeve has high-conviction signals with no remaining room. A borrowing policy allows SMA to draw from RSI's idle budget up to a configurable cap (e.g. borrow ≤ 20% of the idle sleeve) when RSI has zero open positions and zero pending orders, and vice versa. Borrowed capital is repaid immediately when the lending strategy's own signal fires. Borrowing is only permitted when the lending sleeve is genuinely idle (not merely partially used) to prevent the borrowing strategy from starving the lender mid-cycle. This is a Phase 11 item because it requires the ownership and reconciliation infrastructure (10.C1/C2) to be proven reliable before adding cross-strategy state dependency. | ⬜ |
| 11.6 | **Portfolio concentration guardrails** — sector cap (block new entry if ≥N open positions in same GICS sector) and shared-symbol conflict rejection (if SMA and RSI both want the same symbol, gate the second entry). Deferred from 10.F5: current watchlists have zero overlap so the shared-symbol case is moot; sector risk is already partially contained by the sleeve (max 5 × $8k) and regime gating. Revisit when watchlists expand or account size grows to where semiconductor concentration in SMA becomes a meaningful single-event risk. | ⬜ |
| 11.7 | **Advanced per-sector capital cap** — hard notional ceiling per GICS sector across all strategies combined (e.g. ≤ 40% of gross exposure in semiconductors). Builds on 11.6's sector tracking infrastructure. | ⬜ |
| 11.8 | **Strategy health monitor** — rolling expectancy + rolling Sharpe per strategy; automatic capital reduction or disable when performance degrades beyond a pre-committed threshold | ⬜ |
| 11.9 | **Strategy re-enable workflow** — disabled strategies require manual review + a fresh paper forward-test before being re-enabled with capital | ⬜ |
| 11.10 | Dashboard / report: per-strategy P&L, regime history, capital allocation over time | ⬜ |
| 11.11 | **Intraday market-data stream** (`data/stream.py`) — deferred until an intraday strategy exists. This would use `alpaca-py` `StockDataStream` for real-time bars/quotes/trades and is **not** the same as Phase 10's mandatory `TradingStream` order/fill websocket. | ⬜ |
| 11.12 | **Event-based backtester for limit orders** — supplement `backtest/runner.py` with a bar-by-bar event-driven harness that models limit order fill realism (price must touch and hold). Required for honest RSI reversion backtesting. *(Hilpisch, Ch. 6 — Event-Based Backtesting)* | ⬜ |
| 11.13 | **ML edge filter** — train a direction classifier (logistic regression or AdaBoost) on lagged log-return features; plug it into `BaseStrategy` via the existing `edge_filter` hook to gate SMA/RSI entries during unfavourable regimes. The `edge_filter(df) -> pd.Series[bool]` interface in `strategies/base.py` is already designed for this — zero architecture changes needed. *(Hilpisch, Ch. 5 — ML-Based Strategy + Ch. 10 — Online Algorithm)* | ⬜ |
| 11.14 | **Incremental online signal generation** — if any intraday strategy is added, refactor `generate_signals()` from full-history recomputation to an incremental deque-based algorithm that processes one new bar at a time. Full-history recomputation is fine for daily bars; wasteful at 1-minute resolution. *(Hilpisch, Ch. 7 — Online Algorithm pattern)* | ⬜ |
| 11.15 | **Fundamental health filter for RSI mean-reversion** — buying oversold stocks without a balance sheet check is structurally exposed to catastrophic losses: an oversold stock with heavy *bank* debt (callable on demand) can go to zero before it reverts. Pre-screen RSI universe for: (a) positive earnings for ≥ 2 years, (b) net cash positive or long-term (funded) debt only — no bank debt crisis, (c) inventory growth not outpacing revenue growth (Lynch's red flag for cyclicals). *(Lynch, One Up on Wall Street, Ch. 13 — Some Famous Numbers; Ch. 19 — The Cyclical)* | ⬜ |
| 11.16 | **Category-aware ATR stop multiplier** — the current flat `ATR_STOP_MULTIPLIER = 2.0` is applied to all symbols equally. Lynch's framework implies fast growers and turnarounds need wider stops (higher volatility, larger legitimate swings) than stalwarts. Consider per-symbol or per-category multiplier config. *(Lynch, One Up on Wall Street, Ch. 11 — Two-Minute Drill)* | ⬜ |
| 11.17 | **Sector concentration cap on watchlist** — Lynch Ch. 9 documents how hot-industry stocks rise together and fall together (disk drives 1981–83, oil service stocks, home shopping). Cap the watchlist at ≤ 3 symbols per GICS sector to prevent a single sector rotation from producing a correlated drawdown across multiple open positions simultaneously. | ⬜ |
| 11.18 | **Watchlist two-stage pre-screen** — before applying the SMA crossover signal, filter the candidate universe by: PEG ratio < 1.5 (Lynch: *"the P/E of any fairly priced company will equal its growth rate"*), ≥ 2 years positive earnings, not a pure cyclical (airlines, autos, basic materials). Cyclicals route to RSI reversion only. *(Lynch, One Up on Wall Street, Ch. 13)* | ⬜ |
| 11.19 | **Per-symbol cooldown after losing exit** — after a stop-out or other losing exit, block new entries in that same symbol for a configurable number of bars or hours unless a stronger re-entry rule is explicitly satisfied. Current protection is only per-strategy loss-streak cooldown; this would reduce immediate symbol-level re-entry churn. | ⬜ |
| 11.20 | **SMA earnings-gap guardrail** — moved to Phase 10 item 10.F3a. | Moved to 10.F3a |
| 11.21 | **RSI-at-entry overbought gate for SMA crossover** — block SMA entries where RSI on the crossover bar is already ≥ 70. A crossover into overbought territory has significantly lower continuation probability; the best setups see RSI in the 50–65 range at entry (momentum building, not exhausted). Deferred from 10.F3a — requires forward-test data to calibrate the threshold before hardcoding. | ⬜ |
| 11.22 | **Same-day concentration cap on correlated SMA signals** — when ≥ N symbols in the SMA watchlist all cross over on the same day (broad market rip), limit new entries to the top N ranked by volume expansion or crossover angle. Prevents deploying a large chunk of capital into highly correlated positions simultaneously. Minimum guardrail (sector cap) handled by 10.F5; full signal-level concentration control is this item. Deferred from 10.F3a. | ⬜ |
| 11.23 | **SPY gate cliff-edge smoothing for RSI filter** — the current hard cutoff (SPY crosses 50 SMA → all RSI entries blocked immediately) is abrupt. A brief SPY dip below the 50 SMA on one bar can block an entire cycle of valid reversion setups. Smoother alternative: require SPY to be below the 50 SMA for N consecutive bars (e.g. 3) before engaging the block. Hard gates are kept for Phase 10 because they are operationally auditable; this refinement is deferred until forward-test data shows how often brief SPY dips produce false lockouts. Deferred from 10.F3b. | ⬜ |
| 11.24 | **Sentiment overlay — bidirectional signal design and strategy-specific interaction.** Sentiment is not a binary risk-off flag. A Trump post announcing an end to a trade war is strongly positive and should *confirm* trend entries; the same source announcing new tariffs is strongly negative and should *block* them. The system must handle both directions, with strategy-aware logic for how each direction affects each strategy. **SentimentScore data model:** `scope` (market \| sector \| symbol), `valence` (float −1.0 to +1.0, where −1.0 = extreme negative, +1.0 = extreme positive), `confidence` (float 0–1, how many independent sources agree), `source_type` (POLITICAL \| NEWS \| SOCIAL_RETAIL \| EARNINGS_RUMOR), `decay_half_life_minutes` (how long before the signal loses relevance — a tariff tweet ~120 min, a peace deal ~2 days, a WSB post ~30 min). Effective score at engine cycle time: `valence × confidence × exp(−ln(2) × elapsed / half_life)`. **Strategy-specific interaction matrix (rough design, to be calibrated with paper data):** (1) *Strong negative macro (valence < −0.6, scope=market)* — block all new entries in both SMA and RSI; this is a regime-layer concern, not strategy-level. Feed into `RegimeDetector` as a SENTIMENT_BEAR override tier above the technical BEAR classification. Example: tariff announcement, war escalation, Fed emergency meeting signal. (2) *Mild negative macro (−0.6 ≤ valence < −0.2, scope=market)* — no effect on SMA (mild pullbacks are normal within trends and can generate the best crossover entries); blocks RSI entries (a mild macro headwind means oversold names may keep falling — the reversion is not yet safe). This asymmetry is intentional: SMA benefits from volatility, RSI is hurt by it. (3) *Strong positive macro (valence > 0.5, scope=market)* — confirms SMA trend entries (momentum environment); confirms RSI reversion entries (risk-on means snapped-back stocks follow through). Never used to initiate trades on its own — only as a confirmation layer on top of an existing technical signal. Example: ceasefire/peace deal, surprise positive Fed signal, large stimulus announcement. (4) *Negative symbol-level (valence < −0.3, scope=symbol)* — block that specific symbol in both strategies. For RSI this is especially important: an oversold stock with active negative news is not a technical oversold — it is a fundamental deterioration. The distinction matters: technical oversold reverts; fundamental oversold may not. (5) *Positive symbol-level (valence > 0.3, scope=symbol)* — confirms entry for the specific symbol if the technical signal already exists; does not initiate. (6) *Retail frenzy (source=SOCIAL_RETAIL, mention velocity spike)* — bidirectional but strategy-specific: for RSI, a WSB mention spike into an oversold name *may* confirm the reversion trigger (the crowd provides the snap-back energy); for SMA, a WSB spike into a trending name signals *potential top* — unsustainable momentum driven by retail, not institutional — consider blocking or reducing size. **Integration architecture:** Two insertion points depending on scope. Market-scope sentiment → `RegimeDetector._sentiment_override()` — a SENTIMENT_BEAR override sits above the technical classification and can block all entries independently of SPY/ADX readings; clears automatically when effective score decays below threshold. Symbol-scope sentiment → `SentimentEdgeFilter` implementing the existing `edge_filter` hook in `BaseStrategy` — strategy registers which sentiment sources and thresholds apply; engine passes the symbol's current `SentimentScore` into the filter the same way it passes bars today. **Asymmetry principle (non-negotiable):** negative sentiment has hard veto power; positive sentiment is additive confirmation only. The system must never open a position solely because sentiment is positive. **Data sources (priority order):** (1) Alpaca News API — already in the subscription, provides scored headlines per symbol and market-wide; zero new credentials; start here. (2) StockTwits streaming — symbol-tagged posts with built-in bullish/bearish labels; free tier available; good for retail frenzy detection. (3) X (Twitter) — political figure monitoring (curated watchlist of high-market-impact accounts); paid API required for reliable real-time access; high priority for macro event detection. (4) Reddit official API — WSB mention count + upvote velocity; useful for retail frenzy; rate-limited on free tier. (5) Polygon.io / Benzinga — paid, institutional-grade news feed with sub-second latency; evaluate after paper validation proves sentiment is additive. **Latency constraint:** the 5-minute engine cycle is too slow for political event response (a tariff tweet can move a sector 5% in under 60 seconds). Political/macro sentiment requires a dedicated lightweight listener process running alongside the engine, writing to a shared state store (SQLite or Redis) that the engine reads at cycle start. Symbol-level sentiment from news APIs can tolerate the 5-minute cycle. **Backtesting caveat:** historical social data is expensive and sparse; the sentiment layer cannot be honestly backtested with the same rigor as technical signals. Paper-validate for at minimum 4 weeks before treating it as a live gate. | ⬜ |

| 11.25 | **VIX integration in the regime detector.** The current `RegimeDetector` classifies market state entirely from SPY price action: SMA200 (trend direction), ATR% percentile (volatility level), and ADX (trend strength). This works but has a blind spot: SPY can be technically TRENDING while VIX is already pricing in an imminent reversal — the options market sees risk that price has not yet confirmed. Adding VIX as a second input creates a cross-asset confirmation layer. **Proposed integration:** fetch daily VIX bars alongside SPY (VIX is available via Alpaca as `$VIX` or via CBOE data); compute a rolling VIX percentile over the same 126-bar trailing window used for ATR% (approximately 6 months). Map VIX percentile to a regime modifier: VIX > 80th percentile → upgrade current regime toward VOLATILE regardless of SPY ATR%; VIX < 20th percentile (complacency) → relax VOLATILE classification, lean toward TRENDING or RANGING. **Why this matters for each strategy:** (1) SMA Crossover — a technically TRENDING market with VIX > 80th percentile has historically lower follow-through on crossover entries; the high VIX often precedes a whipsaw that stops out the position before it runs. VIX confirmation would reduce false positives in late-stage volatile trends. (2) RSI Reversion — elevated VIX is actually a *favourable* signal for reversion: high fear means stocks are more aggressively oversold than fundamentals justify, and the snap-back when fear recedes is sharper. A high-VIX RANGING regime could be permitted for RSI even when it is blocked for SMA. This is a meaningful differentiation that SPY-only classification cannot make. **Architecture:** `RegimeDetector._fetch_vix()` parallel to `_fetch_spy()`; VIX percentile cached with same TTL; `_classify()` gains a `vix_pct` parameter alongside existing inputs. The regime enum stays the same — VIX modifies how the classification thresholds are applied, it does not add new regime states. Deferred from 10.F2 — requires forward-test data to validate that VIX percentile improves classification accuracy before adding the complexity. | ⬜ |
| 11.26 | **Kelly criterion for evidence-based sleeve weight optimization.** The 50/50 sleeve split in Phase 10 is a prior — the right answer when no data exists. Kelly gives the mathematically optimal capital fraction for a strategy once win rate and average win/loss ratio are known from real trade data. For a portfolio of two strategies, the fractional Kelly weight for each strategy is: `f* = (p × b − q) / b` where `p` = win rate, `q` = 1 − p, `b` = avg_win / avg_loss. The ratio `f_SMA / (f_SMA + f_RSI)` gives the Kelly-implied sleeve split. **Prerequisites before this is useful:** (1) ≥ 100 closed trades per strategy (Kelly is meaningless on small samples — it will overfit noise and recommend extreme allocations); (2) both strategies running live simultaneously so the win/loss data reflects the actual correlated execution environment, not separate paper periods; (3) a half-Kelly or quarter-Kelly multiplier applied to the raw output (full Kelly maximises geometric growth but produces drawdowns most operators cannot stomach — fractional Kelly is standard practice). **Integration:** `SleeveAllocator` gains an optional `kelly_weights(trade_log) -> dict[str, float]` method that reads closed trades from the trade DB, computes per-strategy Kelly fractions, applies the fractional multiplier, normalises to sum ≤ 1.0, and returns suggested weight updates. The method is advisory — a human reviews the output and updates `STRATEGY_ALLOCATIONS` in settings manually; no automated weight changes in Phase 11. A dashboard item (11.10) should display the current Kelly-implied weights alongside the active weights so drift is visible over time. **Relationship to 11.5:** Kelly answers *what the weights should be*; 11.5 answers *how the weights get changed and whether idle capital can be borrowed*. They are complementary — Kelly provides the signal, 11.5 provides the mechanism. | ⬜ |

**Exit Criteria:** Advanced portfolio enhancements run safely on top of the Phase 10
SMA + RSI baseline. Optional third strategies, dynamic allocation, advanced concentration
caps, dashboards, and health-based throttling are paper-validated before any live capital
is assigned to those new behaviors.

---

## Notes & Decisions Log

| Date | Note |
|---|---|
| 2026-04-25 | **10.G6 complete — Fractional share sizing. 646 tests passing.** `FRACTIONAL_ENABLED: bool = True` added to `config/settings.py`. `risk/manager.py`: `RiskDecision.qty` type widened to `float`; `_size_position()` uses `_floor = math.floor(x*100)/100` for MARKET orders when enabled, `math.floor` for LIMIT/GTC and when disabled — zero behavioral change on disable. Live-size multiplier updated: fractional path uses `max(0.01, floor(qty*mult*100)/100)` instead of `max(1, floor(...))`. `execution/broker.py`: routes `floor(qty) ≠ qty` to `_place_fractional_order()` — DAY market entry then standalone GTC stop for `floor(qty)` whole shares after confirmed fill; if `floor(qty)==0` no stop is submitted (position exits via engine signals); dry-run aware. Whole-share OTO GTC path untouched. 5 new broker tests; 3 risk tests updated (INSUFFICIENT_CASH threshold, POSITION_TOO_SMALL uses LIMIT order type, `_signal` helper gains `order_type`/`limit_price` params). |
| 2026-04-25 | **10.F4 complete — RSI paper activation. 641 tests passing.** `RSIReversion(period=14, oversold=30, overbought=70, edge_filter=RSIEdgeFilter())` added as second slot in `forward_test.py`. `RSI_WATCHLIST` (ALLY, CDNS, CCK, SN, TFC) wired via `StaticWatchlistSource`. `allowed_regimes={TRENDING, RANGING}` — blocked in BEAR and VOLATILE; RSI edge filter adds a second BEAR block via SPY > 200/50 SMA check. Sleeve allocator provides $8k per-position cap from the 50% RSI sleeve. Both strategies now running in paper. |
| 2026-04-25 | **10.F1 complete — Per-strategy capital sleeve allocator. 630 tests passing.** `risk/allocator.py`: `SleeveAllocator` with `SleeveCapacity` / `SleeveRejection` return types. Budget = `equity × MAX_GROSS_EXPOSURE_PCT × weight`. Used = positions owned by strategy + pending buy-order notional (limit_price × qty). Idle capital stays locked — no cross-borrowing (Phase 11 item). `MAX_GROSS_EXPOSURE_PCT` raised 0.50 → 0.80; `MAX_OPEN_POSITIONS` raised 5 → 10 (per-strategy constraint now handled by sleeve). `STRATEGY_ALLOCATIONS = {"sma_crossover": 0.50, "rsi_reversion": 0.50}` in settings. `RiskManager.evaluate()` gains `notional_cap: float | None` param; `_size_position()` caps qty at `floor(notional_cap / price)` after all other caps. Engine gains `allocator` param + `_attribute_orders()` helper (order_id → strategy_name via watchlist membership). Sleeve check sits before `risk.evaluate()` in entry branch; rejection logged + alerted. `forward_test.py` wired. 28 new tests. |
| 2026-04-25 | **10.F2 + 10.F3 complete — Regime Detector and engine gating. 602 tests passing.** `regime/detector.py`: `MarketRegime` enum (BEAR/VOLATILE/TRENDING/RANGING) + `RegimeDetector` class with TTL-cached `detect()`. Classification priority: BEAR (SPY < SMA200) → VOLATILE (ATR% above 80th pct of trailing 126-bar history) → TRENDING (ADX ≥ 25) / RANGING (ADX ≤ 20) / ambiguous ADX zone uses SMA50 slope tie-breaker. Fail-safe: last cached regime or RANGING on SPY fetch failure. `add_adx()` + `_wilder_rma()` added to `indicators/technicals.py`. `StrategySlot.allowed_regimes: frozenset \| None` field (None = no gating). Engine `_run_one_cycle` calls `detect()` once per cycle; per-slot check blocks new entries when regime not in allowed set; exits never blocked. `_process_symbol` gains `entry_allowed` param. `forward_test.py` wired: SMA slot allows `{TRENDING, RANGING}`; BEAR and VOLATILE block new SMA longs. VIX integration deferred to Phase 11. 35 new regime unit tests (602 total). |
| 2026-04-25 | **RSIEdgeFilter hardened with liquidity and breakdown gates. 568 tests passing.** Added two new entry gates to `RSIEdgeFilter`: (1) **20-day avg volume ≥ 500K shares** — RSI reversion uses limit orders; thinly traded stocks fill partially and exit wide, destroying the edge on paper; fails open on missing column or insufficient bars; (2) **no new 20-day low** — blocks entries when the stock is in active individual breakdown (consecutive lower lows that the SPY macro gates cannot detect); `close > shift(1).rolling(20).min()`; fails open on insufficient history. `days_after` bumped 1→2 (post-earnings options unwinding and analyst follow-through typically run 2 days). Phase 11 item 11.23 added: SPY 50 SMA cliff-edge smoothing (N-bar confirmation before engaging block). 58 filter unit tests. |
| 2026-04-25 | **10.F3a and 10.F3b filters refined. 561 tests passing.** SMAEdgeFilter gained two new gates: (1) stock close > stock 200-day SMA (structural strength — avoids crossovers in broken names); (2) 10-day avg volume > 30-day avg volume (confirms institutional participation). Earnings blackout removed from SMA — trend-following benefits from earnings catalysts, not hurt by them. RSIEdgeFilter: stock 50-day SMA gate removed (contradicted the strategy's own edge — oversold stocks are typically below their 50 SMA); earnings blackout added (3 days before / 1 day after) which is the correct home for binary-event risk on a mean-reversion strategy. `ENGINE_HISTORY_LOOKBACK_DAYS` bumped 200→300 (≈206 trading days) to support the stock 200 SMA gate. SPYTrendFilter rate-limits retry/log to once per TTL on fetch failure (prevents log spam during API outages). Two Phase 11 items deferred: RSI-at-entry overbought gate (11.21) and same-day concentration cap on correlated signals (11.22). |
| 2026-04-24 | **10.E1, 10.G1, 10.G4 complete. 510 tests passing.** `execution/stream.py` — `StreamManager` wraps alpaca-py `TradingStream` in a daemon thread; `watch(order_id)` returns a `threading.Event` that fires on any terminal event; `register_stop_leg(id)` routes stop fills into `drain_stop_fills()`. `AlpacaBroker` gains `stream_manager` and `dry_run` params; `_wait_for_fill` uses stream event first, falls back to REST on timeout. Engine starts/stops stream in `start()`/`_shutdown()`, calls `_process_stream_stop_fills()` each cycle for immediate stop-out detection (before the 3-cycle cycle-count fallback). `LIVE_SIZE_MULTIPLIER=0.25` applied in `RiskManager.evaluate()` when `LIVE_TRADING=True`. `DRY_RUN=false` env flag causes broker to log but not submit orders. 10.G3 dropped (manual approval prompt not needed). 26 new stream tests. Paper validation gate for 10.E1 active. |
| 2026-04-24 | **Phase 10 Groups A–C complete. Paper validation gate active.** 10.A1 verified live: bot recycled with MU + NVDA open positions; both restored from trade DB record (`trade DB record` in log, not `best-effort slot match`); NORMAL mode confirmed. 10.B1: `LIVE_TRADING` flag + derived credentials + `TRADE_LOG_DB_PAPER`/`TRADE_LOG_DB_LIVE` routing. 10.B2: `scripts/preflight.py` 8-point live checklist. 10.B3: `data/watchlists.py` `WatchlistSource` ABC + `StaticWatchlistSource`; `StrategySlot` gains `watchlist_source` field (precedence over scanner and symbols); `forward_test.py` wired. 10.C1: `_restore_ownership_from_db()` replaces TODO slot-match; `TradeLogger.read_all_open_owners()` + `read_owner_for_symbol()`. 10.C2: `_reconcile_startup()` returns NORMAL/RESTRICTED; RESTRICTED auto-clears after one cycle; entry branch respects mode. 10.C3: 116 new tests (480 total). 10.C4: `_detect_external_closes()` runs each cycle with 3-cycle confirmation window; `TradeLogger.log_external_close()` writes synthetic sell to close stale DB records. Next market open Monday 2026-04-27. |
| 2026-04-23 | **Per-strategy watchlist wiring added as Phase 10 item 10.B3.** Each strategy must watch only its own symbol universe. `forward_test.py` currently passes the union `WATCHLIST` to the SMA slot; before RSI is activated alongside SMA this must be corrected so SMA uses `settings.SMA_WATCHLIST` and RSI uses `settings.RSI_WATCHLIST`. Without this fix, RSI would inherit SMA symbols and SMA would inherit RSI symbols, producing incorrect signal generation, wrong attribution, and sleeve accounting errors. `WATCHLIST` stays in settings as a convenience union for review scripts only. |
| 2026-04-23 | **Edge filters for SMA and RSI promoted into Phase 10.** Both strategies require strategy-level entry filters before going live — the regime detector (10.F2/F3) operates at the portfolio level and is not a substitute. SMA edge filter (10.F3a): SPY>200SMA market-trend gate + earnings-blackout veto wired into `strategies/filters/sma_crossover.py`; promoted from Phase 11 item 11.20. RSI edge filter (10.F3b): SPY>200SMA, SPY>50SMA, stock>50SMA Tier 1 gates from `docs/RSI-edge-filter.md` wired into `strategies/filters/rsi_reversion.py`; required before RSI paper activation (10.F4). Exits are never blocked by either filter. |
| 2026-04-23 | **Fractional share sizing added as Phase 10 item 10.G6.** Fractional shares address two problems on a small live account: (1) stocks priced above the notional budget (e.g. AVGO, NVDA) round to 0 whole shares and are skipped entirely; (2) stocks in the $300–600 range where whole-share rounding produces 20–50% notional distortion from target. Both problems are most acute at account launch and shrink as the account grows — a larger notional budget means whole-share errors become negligible. Implementation: replace `floor(notional_budget / price)` with `round(notional_budget / price, 2)` when `FRACTIONAL_ENABLED=True`. Alpaca fractional orders are `DAY`-only (no GTC stop leg) — engine-side stop monitoring (fresh `DAY` stop each session open) covers the missing OTO leg. |
| 2026-04-22 | **Interim SMA notional guardrail added after contaminated paper run.** `MAX_POSITION_NOTIONAL_PCT` now caps each position's notional exposure so one tight-stop SMA trade cannot starve the rest of the SMA sleeve. This is intentionally a temporary crutch for the SMA-only forward test; Phase 10 Group F still owns the real fix via fixed per-strategy capital allocation, durable ownership, and sleeve-level exposure accounting before SMA + RSI run together. |
| 2026-04-22 | **Phase 9.5 reclassified as stabilization, not final live GO/NO-GO.** The SMA-only paper run should continue until the bot is operationally stable and reconciles reasonably against the same-window backtest. The actual live-readiness gate moves to the post-Phase-10 combined SMA + RSI paper run, after fixed capital allocation, durable ownership, startup reconciliation, and remaining pre-live safeguards are implemented. |
| 2026-04-21 | **Pre-live multi-strategy gates moved into Phase 10.** Phase 10 Group F now owns the critical SMA + RSI blockers: fixed per-strategy capital allocation, production regime detector, strategy regime gating, RSI paper activation, and minimum concentration guardrails. Phase 10 exit criteria now require a combined SMA + RSI Alpaca paper run for minimum 2 weeks, target 4 weeks, with a documented GO/NO-GO report before any SMA + RSI live flip. Phase 11 is now reserved for advanced enhancements such as optional third strategies, dynamic allocation, richer caps, dashboards, and health-based throttling. |
| 2026-04-20 | **Multi-strategy watchlist architecture implemented.** `config/settings.py` now has `SMA_WATCHLIST` (10 symbols: stalwarts/fast growers + large-cap financials) and `RSI_WATCHLIST` (11 symbols: stalwarts + cyclicals + financial cyclicals). `WATCHLIST` is computed as the ordered union of both lists plus RIVN (kept for paper-run continuity, not yet assigned to either strategy). The watchlist review script was redesigned around strategy-specific `CheckProfile` objects: SMA profile requires positive FCF and revenue growth (failing either = POOR FIT); RSI profile treats both as informational only (failing = MARGINAL, not POOR FIT). Solvency is always required for both, with different floors (18-month for SMA, 12-month for RSI). Output is a strategy-fitness matrix showing GOOD FIT / MARGINAL / POOR FIT per symbol per strategy. A scanner (`scripts/scanner.py`) is deferred to Phase 11 when RSI is live and has consumers; scanner design: Stage 1 Alpaca equity screener (fast universe filter) → Stage 2 technical pre-screen (ATR, volume) → Stage 3 yfinance fundamental routing (same CheckProfile logic). Full suite: 424 tests passing. |
| 2026-04-20 | **Book review: Lynch — *Beating the Street* (full read). Three solid-keep findings implemented immediately; two "keep with caution" and six skip decisions made.** Implemented: (1) `scripts/watchlist_review.py` — Lynch six-month checkup operationalised as a runnable script (Ch. 15 FCF check, Ch. 21 revenue growth check, Golden Rules cash solvency check). Outputs a markdown report; exit 0 = all pass, exit 1 = failures found. Run before any watchlist change and before the Phase 10 live flip. 48 unit tests added. `yfinance` added to requirements.txt. (2) FCF positivity and cash solvency are the admission gate for any new RSI reversion symbol — coded into the script. (3) Revenue growth ≥ 0% YoY required for consumer/growth names in RSI universe. **Deliberately skipped:** January Effect (academically arbitraged since ~2000), sum-of-parts valuation (priced in continuously by sell-side), "lousy industry" preference (captured by existing market-cap/beta filters), pent-up demand indicator (data-intensive, well-known to institutions), insider buying Form 4 (low signal for large caps), Russell/S&P P/E spread (wrong tool for 13-symbol universe). **"Keep with caution" deferred:** cyclical P/E inversion filter and P/E divergence stop tightener — both were judged likely to create more problems than they solve at current stage; neither is implemented. |
| 2026-04-20 | **Book review: Lynch — *One Up on Wall Street* (full read).** Five findings folded into the plan: (1) *Ch. 3 + 11* — Six-category framework (Fast Grower / Stalwart / Turnaround / Cyclical / Slow Grower / Asset Play) determines which strategy fits each stock. SMA crossover suits Stalwarts and Fast Growers; RSI reversion suits Turnarounds and Cyclicals. Current watchlist assessment added to **Watchlist Curation Philosophy** section: AAPL/MSFT/GOOGL/AMZN ✅ for trend-following; JPM/BAC/GS/NVDA/PINS/UBER ⚠️ acceptable with caveats; DAL/COIN/RIVN ❌ wrong category for SMA — reclassify DAL/COIN as RSI reversion candidates, remove RIVN before Phase 10 (do not change mid-paper-run). (2) *Ch. 13* — PEG < 1.5 + ≥2 years positive earnings as a two-stage pre-screen before any symbol enters the RSI watchlist (logged as Phase 11 item 11.18). (3) *Ch. 13 + 19* — Fundamental health filter for RSI reversion: reject positions if inventory/sales ratio rising, earnings declining, or bank debt present (item 11.15). (4) *Ch. 11* — Category-aware ATR stop multiplier: wider stops (k=3.0) for cyclicals, tighter (k=1.5) for stalwarts (item 11.16). (5) *Ch. 15* — Sector concentration cap ≤ 3 symbols per GICS sector to prevent correlated drawdowns; the minimum guardrail is now Phase 10 Group F, with advanced refinement in Phase 11. |
| 2026-04-19 | **Book review: Hilpisch — *Python for Algorithmic Trading* (full read).** Three findings folded into the plan: (1) *Ch. 4 + 10* — VaR, max drawdown duration, and Kelly criterion added to `backtest/runner.py` `compute_stats()` and `scripts/gonogo.py`. Kelly is informational-only until ~200 trades (RSI reversion will provide the volume). (2) *Ch. 6* — Vectorized backtesting overestimates limit order fill rates; an event-based backtester is needed for honest RSI reversion validation (logged as Phase 11 item 11.12). (3) *Ch. 5 + 7* — ML direction classifier maps directly onto the existing `edge_filter` hook in `BaseStrategy` (11.13); incremental online signal generation is needed only if intraday strategies are added (11.14). Cloud deployment pattern (Ch. 10 ZeroMQ + systemd) logged in Phase 10 Group H. Nothing from the book required immediate code changes beyond the stats improvements. |
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
| 2026-04-19 | **Phase 10 redesigned after safety review.** Full review cycle surfaced: (a) slippage kill switch had a latent bug (modeled_bps hardcoded 0, epsilon trick caused false halt — fixed); (b) position ownership is in-memory only and will misattribute after restart in multi-strategy setups — durable ownership from trade DB designed and deferred to Phase 10; (c) startup reconciliation is best-effort — full cross-check of broker/orders/DB/ownership designed and deferred to Phase 10; (d) REST polling for order state acceptable for paper but not live — WebSocket TradingStream required before live. Phase 10 restructured into Groups A–H with hard-blocker classification, implementation order, and embedded design for 10.C1 (durable ownership), 10.C2 (reconciliation), and 10.F1 (per-strategy allocation). Paper-validation gates added between major risk groups. |
| 2026-04-19 | **Future Alpaca features noted (not actionable now).** (a) **Trailing stop orders** — Alpaca supports trail_price / trail_percent on stop orders. Could replace fixed ATR stops for trend-following strategies (SMA crossover) to let winners run longer. Evaluate in Phase 11. (b) **VWAP/TWAP execution** — available via Elite Smart Router for minimizing market impact. Not relevant at current position sizes; revisit if scaling up. (c) **24/5 extended hours trading** — Alpaca supports overnight/extended hours. Current market hours gate correctly restricts to regular session, which is appropriate for daily-bar strategies. Available if a future strategy warrants it. |
| 2026-04-14 | **Integrated `trading-bot-design-guide-full.md` recommendations.** (a) Added explicit success metric: "positive expectancy with controlled drawdowns" — not "always profitable." (b) Expanded Guiding Principles with Risk>Entry, Boring beats fancy, One strategy first, Strategies decay. (c) Added 6-layer architectural mental model (Data → Regime → Strategy → Risk → Execution → Monitoring). (d) Phase 4 gains minimal edge-filter hook (e.g. SPY>200MA gate) and strategy-declared preferred order type. (e) Phase 6 gains loss-streak cooldown, broker-error-streak kill switch, slippage-drift kill switch, gross exposure cap. (f) Phase 7 clarifies strategy chooses order type; hard-risk exits always immediate. (g) Phase 9 gains per-strategy P&L attribution (even for single-strategy MVP — schema ready for N strategies) + continuous slippage monitoring feeding the 6.11 kill switch. (h) Phase 10 now owns the pre-live multi-strategy safety layer; Phase 11 owns advanced enhancements after that baseline is proven. |
