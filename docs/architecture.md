# Trading Bot — Architecture Guide

## Overview

This document defines the target architecture for the Alpaca trading bot. It is the source of truth for structural decisions, coding conventions, and the go/no-go framework for live capital deployment. All refactoring and new development should align with this guide.

The bot is built in Python using `alpaca-py`. Four strategies are active in paper trading as of Phase 10 (2026-05-06): SMA Crossover (trend-following), RSI Reversion (mean-reversion), Donchian Breakout (trend-continuation), and SPY Options RSI Reversion (options mean-reversion). The final live GO/NO-GO gate is the combined four-strategy paper run after Phase 10 safety work is complete.

---

## Layered Architecture

The bot is organized into seven layers. Each layer has a single responsibility and communicates only with the layer directly above or below it.

```
┌─────────────────────────────────────────┐
│           Engine (trader.py)            │  Orchestrates the live loop
├─────────────────────────────────────────┤
│         Regime Detector                 │  BEAR/VOLATILE/TRENDING/RANGING gate
├─────────────────────────────────────────┤
│              Data Layer                 │  Historical bars, freshness checks
├─────────────────────────────────────────┤
│     Indicators + Strategy Interface     │  Technical indicators, BaseStrategy
├─────────────────────────────────────────┤
│        Risk Manager + Allocator         │  Position sizing, sleeve budgets, kill switches
├─────────────────────────────────────────┤
│         Execution Layer (alpaca-py)     │  TradingClient, order management, paper↔live
│         Options Execution Worker        │  Async bracket-order thread for options
├─────────────────────────────────────────┤
│         Reporting & Monitoring          │  Trade log (SQLite), PnL, metrics, alerts
└─────────────────────────────────────────┘
```

At the signal level, strategy entries pass through a narrower trade-permission pipeline:

```
Watchlist → raw strategy signal → edge filter (+ sector momentum) → regime gate → sleeve check → risk → execution
```

- **Strategy** owns setup detection (crossover, RSI extreme, breakout, options RSI recovery)
- **Edge filter** owns current-condition permission (stock trend, macro state, earnings)
- **Regime detector** gates entire strategies by market regime
- **Sleeve allocator** enforces per-strategy capital budgets
- **Risk manager** owns sizing, stops, exposure limits, and kill switches
- **Execution** only places orders after all upstream gates pass

For options, the execution path diverges after the risk manager: the broker detects the OCC symbol and dispatches an `OptionsExecutionWorker` thread that handles the async bracket order lifecycle independently of the engine loop.

---

## Project Structure

```
trading-bot/
│
├── CLAUDE.md                  # Claude Code session context
├── PLAN.md                    # Phased build plan and progress tracker
├── requirements.txt           # Pinned dependencies
├── main.py                    # Entry point (delegates to forward_test.py)
├── forward_test.py            # Launches engine for multi-week paper runs
├── start_bot.sh               # tmux + caffeinate launcher
├── stop_bot.sh                # Graceful shutdown
├── recycle_bot.sh             # stop + start (picks up code changes without restart)
│
├── config/
│   ├── .env                   # API keys and runtime flags (never committed)
│   └── settings.py            # Centralized config (symbols, risk params, etc.)
│
├── data/
│   ├── __init__.py
│   ├── fetcher.py             # Fetches OHLCV bars via StockHistoricalDataClient
│   ├── trades.db              # Paper SQLite trade log (gitignored)
│   ├── trades_live.db         # Live SQLite trade log (gitignored)
│   ├── historical/            # Cached historical bars (Parquet, gitignored)
│   └── cache/
│       └── sector_map.json    # Persistent ticker→sector cache (populated at startup)
│
├── indicators/
│   ├── __init__.py
│   └── technicals.py          # SMA, EMA, ATR, RSI, ADX (hand-rolled, no pandas-ta)
│
├── strategies/
│   ├── __init__.py
│   ├── base.py                # BaseStrategy, SignalFrame, StrategySlot, WatchlistSource
│   ├── sma_crossover.py       # Trend-following: SMA crossover
│   ├── rsi_reversion.py       # Mean-reversion: RSI oversold/overbought
│   ├── donchian_breakout.py   # Trend-continuation: Turtle System 1 (30/15, ai_bigtech)
│   ├── spy_options_reversion.py  # Options mean-reversion: SPY calls on RSI recovery
│   └── filters/
│       ├── common.py          # SPYTrendFilter + CompositeEdgeFilter
│       ├── sma_crossover.py   # SMAEdgeFilter: stock > 200 SMA, volume expansion
│       ├── rsi_reversion.py   # RSIEdgeFilter: SPY dual macro, earnings, liquidity, no-new-low
│       ├── donchian_breakout.py      # DonchianEdgeFilter: stock > 200 SMA, liquidity, earnings
│       ├── spy_options_reversion.py  # SPYOptionsEdgeFilter: SPY > 100 SMA
│       └── sector_momentum.py # SectorMomentumFilter: HOT/NEUTRAL/COLD gate adapter
│
├── sector/
│   ├── __init__.py
│   ├── resolver.py            # SectorResolver: ticker → sector (yfinance, JSON cache)
│   └── gauge.py               # SectorMomentumGauge: ETF-based HOT/NEUTRAL/COLD scoring
│
├── regime/
│   ├── __init__.py
│   └── detector.py            # RegimeDetector: BEAR/VOLATILE/TRENDING/RANGING
│
├── engine/
│   ├── __init__.py
│   └── trader.py              # TradingEngine — the live loop orchestrator
│
├── risk/
│   ├── __init__.py
│   ├── manager.py             # RiskManager: sizing, drawdown, stop-loss, kill switches
│   └── allocator.py           # SleeveAllocator: per-strategy capital budgets
│
├── execution/
│   ├── __init__.py
│   ├── broker.py              # AlpacaBroker — TradingClient wrapper + equity/options routing
│   ├── options_executor.py    # OptionsExecutionWorker — async bracket-order thread
│   └── stream.py              # StreamManager — WebSocket fill/order streaming
│
├── utils/
│   └── options_lookup.py      # find_best_call: selects ITM call by delta + DTE from Alpaca chain
│
├── reporting/
│   ├── __init__.py
│   ├── logger.py              # TradeLogger — writes to SQLite (log, log_stop_fill, log_external_close)
│   ├── metrics.py             # Computes Sharpe, drawdown, profit factor, win rate
│   ├── pnl.py                 # PnLTracker — daily/weekly P&L reports
│   └── alerts.py              # AlertDispatcher with pluggable backends
│
├── backtest/
│   ├── __init__.py
│   ├── runner.py              # vectorbt backtesting harness
│   ├── reconcile.py           # Forward-test reconciliation (paper vs backtest)
│   └── spy_options_backtest.py  # SPY options strategy backtester (daily proxy)
│
├── scripts/
│   ├── __init__.py
│   ├── gonogo.py              # Go/no-go checker for live readiness
│   ├── post_mortem.py         # Post-trade diagnostic reporting (RS, MA trends)
│   ├── preflight.py           # Pre-flight checklist (must exit 0 before live flip)
│   ├── rsi_backtest_report.py
│   ├── rsi_candidate_post_analysis.py
│   ├── rsi_candidate_validate.py
│   ├── rsi_watchlist_scan.py
│   ├── sma_watchlist_scan.py
│   └── watchlist_review.py
│
├── tests/
│   ├── conftest.py            # Shared fixtures (make_ohlcv, tmp_cache_dir, etc.)
│   ├── test_strategies.py
│   ├── test_technicals.py
│   ├── test_risk.py
│   ├── test_allocator.py
│   ├── test_broker.py
│   ├── test_engine.py
│   ├── test_fetcher.py
│   ├── test_backtest.py
│   ├── test_reporting.py
│   ├── test_reconcile.py
│   ├── test_metrics.py
│   ├── test_regime.py
│   ├── test_filters.py
│   ├── test_gonogo.py
│   ├── test_sector_gauge.py
│   ├── test_sector_resolver.py
│   └── test_spy_options_reversion.py  # 21 tests: signals, guards, trailing stop, edge filter
│
├── docs/                      # Architecture and strategy documentation
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

### 2. Regime Detector

`regime/detector.py` — `RegimeDetector` classifies the current market environment each cycle and gates which strategies are allowed to open new positions.

**Four regimes:**

| Regime | Condition | SMA | RSI | Donchian | SPY Options |
|---|---|---|---|---|---|
| BEAR | SPY < 200-day SMA | ❌ | ❌ | ❌ | ❌ |
| VOLATILE | ATR% above 80th percentile of trailing 126 bars | ❌ | ❌ | ❌ | ❌ |
| TRENDING | ADX ≥ 25 | ✅ | ✅ | ✅ | ✅ |
| RANGING | ADX ≤ 20 (or ambiguous zone, SMA50 slope tie-break) | ✅ | ✅ | ❌ | ✅ |

**Classification priority:** BEAR → VOLATILE → TRENDING/RANGING (ADX + slope).

**Graduated fail-safe:** on the first failure, the engine uses the last known regime (logged as WARNING). After `REGIME_MAX_CONSECUTIVE_FAILURES` (default 3) consecutive failures it falls back to BEAR (fail-closed, logged as ERROR). On the very first call with no prior regime it defaults to RANGING. The consecutive failure counter resets to 0 on any successful detection. Exits are never blocked by regime.

**TTL caching:** SPY bars are fetched once per cycle and cached for `ENGINE_CYCLE_INTERVAL_SECONDS`. The detector is called once per cycle, not per symbol.

Each `StrategySlot` declares `allowed_regimes: frozenset[MarketRegime] | None`. `None` means no gating. The engine checks the current regime against the slot's allowed set before processing entries.

### 2b. Sector Momentum Gauge

`sector/gauge.py` — `SectorMomentumGauge` is a **context provider**, not a gate. It scores each sector ETF as HOT, NEUTRAL, or COLD using a 5-signal composite score based on daily bars. Strategies and edge filters query it to make informed entry decisions. The gauge never directly blocks a trade — that decision belongs to the consuming filter.

**Scoring (per sector ETF):**

| Signal | Score |
|---|---|
| ETF close > SMA(200) | +1 (else -1) |
| ETF close > SMA(50) | +1 (else -1) |
| SMA(50) > SMA(200) — golden cross state | +1 (else -1) |
| Distance from SMA(50) > +2% | +1 (if < -2% → -1, else 0) |
| 10-day avg volume > 20-day avg volume | +1 (confirmation only, never -1) |

**Classification:** HOT (score ≥ 3), COLD (score ≤ -2), NEUTRAL otherwise.

**Fail behavior:** If ETF data is unavailable or fewer than 200 bars exist, returns NEUTRAL.

**TTL caching:** ETF bars and score results are cached per `cache_ttl_seconds` (default 600s). Called once per cycle via edge filters — not per symbol.

`sector/resolver.py` — `SectorResolver` maps stock tickers to sector labels using yfinance metadata cached in `data/cache/sector_map.json`. Hydrated once at startup (`resolver.hydrate(all_symbols)`) so no API calls occur during the live trading loop. Industry takes priority over sector in normalization (NVDA → industry="Semiconductors" → `"semiconductors"`, not `"technology"`). ETFs return `None`. Unknown symbols fail open.

**Sector ETF registry** (`SECTOR_ETFS` in `config/settings.py`): 12 sectors mapped to ETFs (SMH for semiconductors, XLK for technology, XLF for financials, etc.).

See `docs/regime_flowchart.md` for the full regime + sector interaction diagram.

---

### 3. Indicators

Hand-rolled technical indicators in `indicators/technicals.py`. Each function is pure (returns a new DataFrame, never mutates input) with predictable column names (`sma_{length}`, `rsi_{length}`, etc.).

Currently provided:
- `add_sma(df, length)` — Simple Moving Average
- `add_ema(df, length)` — Exponential Moving Average (Wilder seed)
- `add_atr(df, length)` — Average True Range (Wilder/RMA)
- `add_rsi(df, length)` — Relative Strength Index (Wilder/RMA)
- `add_adx(df, length)` — Average Directional Index (used by regime detector)

Design decision: indicators are hand-rolled (~5–10 lines each) rather than using pandas-ta. This eliminates a dependency risk on a library with pandas 2.x breakage history, on a code path that will manage real money.

### 4. Strategy Interface

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

**Options extension points on BaseStrategy:**

Strategies that trade options may implement two additional methods:

```python
def build_option_execution(
    self, symbol: str, underlying_price: float
) -> tuple[str, float, float, float] | None:
    """
    Called by the broker when an options entry is approved.
    Returns (occ_symbol, limit_price, take_profit, stop_loss)
    or None to abort the entry (e.g. spread too wide, no suitable contract).
    """

def inspect_open_positions(self, position, latest_close: float) -> bool:
    """
    Called every engine cycle for each open position.
    Returns True to trigger an immediate market exit.
    Used for time stops, delta floors, trailing stops, or any mid-trade guard
    that cannot be expressed as a bar-level exit signal.
    """
```

Neither method has a default implementation in `BaseStrategy` — only strategies that trade options need them. The engine calls `inspect_open_positions` before processing the entry/exit signal branch for that symbol.

**Edge filter contract:**
- The repo standard is a structured edge-filter decision carrying both `allowed` and `reasons`
- `BaseStrategy` centrally normalizes both supported edge-filter return types:
  - `EdgeFilterDecision` — preferred for new or upgraded filters
  - `pd.Series[bool]` — compatibility-only fallback for legacy or trivial filters
- `inspect_signals(...)` is the canonical observability seam: exposes raw signals, filtered signals, latest edge allow/block state, and latest block reasons
- `_raw_signals(df)` computes the strategy's unfiltered setup first
- `generate_signals(df)` AND-gates entries with the edge filter
- Exits are never blocked by the edge filter
- Missing, NaN, or false filter values block entries by default

**Filter file layout:**
- Strategy-specific edge filters live in `strategies/filters/<strategy_name>.py`
- Shared filter helpers live in `strategies/filters/common.py` — includes `SPYTrendFilter` and `CompositeEdgeFilter`
- `CompositeEdgeFilter` composes normalized edge-filter decisions internally and preserves all blocking reasons for the latest bar
- `SectorMomentumFilter` (`strategies/filters/sector_momentum.py`) is a reusable adapter that queries the `SectorMomentumGauge` and applies a configurable `sector_entry_policy` ("block" | "warn" | "pass")
- New filters with meaningful operator-facing diagnostics should return `EdgeFilterDecision`; plain boolean `pd.Series` is being phased out as the primary authoring style

**StrategySlot:**
Each slot binds a strategy to its symbol universe, timeframe, and allowed regimes. The engine iterates over slots each cycle.

**Current strategies:**

| Strategy | File | Status | Order Type | Allowed Regimes | Sleeve |
|---|---|---|---|---|---|
| SMA Crossover | `sma_crossover.py` | **Paper Trading** | MARKET | TRENDING, RANGING | 45% |
| RSI Reversion | `rsi_reversion.py` | **Paper Trading** | LIMIT | TRENDING, RANGING | 25% |
| Donchian Breakout | `donchian_breakout.py` | **Paper Trading** | MARKET | TRENDING only | 25% |
| SPY Options RSI Reversion | `spy_options_reversion.py` | **Paper Trading** | LIMIT (async bracket) | TRENDING, RANGING | 5% |

### 5. Risk Manager + Sleeve Allocator

The Risk Manager sits between strategy signals and the execution layer. No order reaches the broker without passing through it. The Sleeve Allocator enforces per-strategy capital budgets before the risk manager is invoked.

#### Sleeve Allocator (`risk/allocator.py`)

`SleeveAllocator` divides deployable capital across strategy pools and strategies. Each strategy has a `target_pct`, a pool type (`equity` or isolated options), a concentration cap, and a hard count ceiling.

```
Deployable capital  = equity × MAX_GROSS_EXPOSURE_PCT
Target budget       = deployable_capital × target_pct
Max one position    = effective_budget × max_position_pct_of_sleeve
```

At $100k equity, 80% deployable gross, 95/5 pool split, and a 40% equity concentration cap:
- SMA target sleeve: $100k × 0.80 × 0.45 = $36,000 → up to $16,560 in one position when stretched
- RSI target sleeve: $100k × 0.80 × 0.25 = $20,000 → up to $9,200 in one position when stretched
- Donchian target sleeve: $100k × 0.80 × 0.25 = $20,000 → up to $9,200 in one position when stretched
- Options sleeve: $100k × 0.80 × 0.05 = $4,000 isolated → one position max

RiskManager still sizes from stop-risk first; the allocator only caps strategy capital. Equity sleeves may borrow idle equity-pool capital up to 115% of target while total deployable utilization remains below 80%. Rejection codes remain `SLEEVE_FULL` (capital exhausted), `SLEEVE_MAX_POSITIONS` (hard safety ceiling), and `SLEEVE_DRAWDOWN`.

The options sleeve uses the same `SleeveAllocator` and HWM drawdown gate as equity strategies. P&L is recorded with a 100× contract multiplier so the drawdown gate operates on real dollar amounts, not premium points.

#### Risk Manager (`risk/manager.py`)

**Responsibilities:**
- Validate signals against current portfolio state
- Enforce ATR-based position sizing (risk no more than 2% of equity per trade)
- Enforce daily loss limits (halt bot if breached)
- Apply stop-loss levels to every order (ATR-based for equities; bracket legs for options)
- Track current exposure per symbol and overall
- Loss-streak cooldown per strategy
- Broker-error-streak kill switch
- Slippage-drift kill switch

For options, fractional sizing is disabled (`and not is_option` guard in `_size_position()`). The 100× contract multiplier is applied only in P&L accounting — the risk manager sizes by contract count.

**Key risk parameters (from `config/settings.py`):**

| Rule | Description | Default |
|---|---|---|
| `MAX_POSITION_PCT` | Max % of equity risked per trade (loss-to-stop) | 2% |
| `MAX_POSITION_NOTIONAL_PCT` | Max notional for one position as % of equity | 10% |
| `MAX_OPEN_POSITIONS` | Max concurrent global positions | 10 |
| `MAX_GROSS_EXPOSURE_PCT` | Max total gross notional as % of equity | 80% |
| `MAX_DAILY_LOSS_PCT` | Halt for the session if equity down this much | 5% |
| `HARD_DOLLAR_LOSS_CAP` | Absolute $ loss cap from session start | $2,000 |
| `ATR_STOP_MULTIPLIER` | Stop = entry − k × ATR (equities only) | 2.0 |
| `LOSS_STREAK_THRESHOLD` | Disable strategy after N consecutive losses | 3 |
| `STRATEGY_ALLOCATIONS` | Per-strategy target %, pool type, priority, hard count limit, and concentration cap | see above |

### 6. Execution Layer

A thin wrapper (`AlpacaBroker`) around `alpaca-py`'s `TradingClient`. Translates approved risk decisions into actual orders. The broker routes equity and options orders down separate paths detected by the OCC symbol format.

**Paper vs live routing** is controlled by the `LIVE_TRADING` flag in `config/.env`. All credential and DB routing derives from this single flag — never set `ALPACA_PAPER` directly.

#### Equity order paths

| Path | Condition | TIF | Stop |
|---|---|---|---|
| OTO GTC | Whole-share MARKET or LIMIT | GTC | Attached stop-loss leg (OTO bracket) |
| Fractional DAY | `FRACTIONAL_ENABLED=True` and `floor(qty) ≠ qty` | DAY | Standalone GTC stop submitted after fill confirmation |

**Fractional shares (`FRACTIONAL_ENABLED`):** Alpaca fractional orders require DAY TIF and cannot use OTO order class. The broker routes fractional quantities to `_place_fractional_order()`: DAY market entry first, then a standalone GTC stop for `floor(qty)` whole shares after confirmed fill. If `floor(qty) == 0` (qty < 1 share), no stop is submitted and the position exits via engine signals. Disable `FRACTIONAL_ENABLED` once the account exceeds ~$10k.

#### Options order path (`execution/options_executor.py`)

Options orders are detected by matching the OCC symbol format (`^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$`, compiled as `_OCC_PAT` at module level in `engine/trader.py`). When an OCC symbol is detected, the broker dispatches an `OptionsExecutionWorker` thread and returns `OrderResult(status=ACCEPTED)` immediately so the engine loop is not blocked.

**OptionsExecutionWorker lifecycle:**

1. Calls `build_option_execution(symbol, underlying_price)` on the strategy to get `(occ_symbol, limit_price, take_profit, stop_loss)`. If `None` is returned (e.g. spread too wide), the worker exits without placing an order.
2. Submits a limit entry order for the OCC symbol via `TradingClient`.
3. Polls `StreamManager` for a fill event for up to 60 seconds. If no fill arrives, cancels the order and exits.
4. On confirmed fill: submits take-profit and stop-loss legs as a bracket OTO order.
5. Reports the fill back to the engine via a queue drained by `drain_option_fills()` each cycle.

**DRY_RUN guard:** the dry-run check fires before the worker is created — no thread is spawned and no order is submitted during dry runs.

#### OCC symbol handling in the engine

`_OCC_PAT` in `engine/trader.py` is the single authoritative regex for detecting options positions anywhere in the engine. All engine paths that behave differently for options key off this pattern:

| Path | OCC-specific behaviour |
|---|---|
| `_repair_missing_protective_stops` | Skips OCC symbols — bracket stop legs are managed by Alpaca, not the engine |
| `_record_fill` (slippage monitor) | Skipped for OCC exits — underlying bar price vs option premium produces meaningless bps |
| `_log_close` | Uses `result.avg_fill_price` (option premium) as `modeled_price` instead of the underlying bar close |
| `_record_realized_pnl` | Accepts `multiplier=100` for options; default 1 for equities |
| `_process_stream_stop_fills` | Normalizes OCC → underlying before `_position_owners` lookup; logs with `log_stop_fill` |
| `inspect_open_positions` exit | Calls `_OCC_PAT`-gated multiplier; skips `_record_fill`; uses premium as modeled price |

**WebSocket stream stop fills:** When a bracket stop leg fills, the stream delivers the event with the OCC symbol. `_process_stream_stop_fills` normalizes the OCC string to the underlying ticker (the key in `_position_owners`), records the real P&L with the 100× multiplier, and calls `log_stop_fill` to persist the confirmed execution. If price or qty is absent from the stream event, it falls back to `log_external_close`.

**Known limitation:** `_position_owners` is keyed by the underlying ticker (`"SPY"`), not the OCC string. Two options strategies trading the same underlying simultaneously would collide. Safe for any number of strategies on distinct underlyings. See PLAN.md item 11.23 for the migration plan.

#### Other execution rules

- `DRY_RUN=True` logs orders without submitting (final sanity check before live)
- `LIVE_SIZE_MULTIPLIER=0.25` scales live position sizes to 25% at launch
- Order errors are caught, logged, and never crash the bot
- Position ownership is tracked per strategy to prevent cross-strategy interference
- WebSocket streaming (Phase 10.E1) is the primary fill notification path; REST polling is the fallback

### 7. Reporting & Monitoring

Every trade is logged to SQLite for the go/no-go evaluation. This layer also computes live performance metrics and sends alerts.

**Trade logs (SQLite):**
- `data/trades.db` — paper trading (never mixed with live data)
- `data/trades_live.db` — live trading (separate file to prevent cross-contamination)

The `TradeLogger` inserts a row for every fill. Three write paths:

| Method | Used when | Fill price recorded |
|---|---|---|
| `log(build_record(...))` | Entry fills (all strategies) | `result.avg_fill_price` |
| `log(build_close_record(...))` | Signal-based exit fills | `result.avg_fill_price`; `modeled_price` is the premium for options, bar close for equities |
| `log_stop_fill(symbol, strategy, qty, avg_fill_price)` | Confirmed WebSocket bracket stop fills | Exact stream fill price and qty |
| `log_external_close(symbol, strategy, reason)` | Inferred external closes (no confirmed fill event) | `NULL` — price unknown |

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

**Pre-flight checklist (`scripts/preflight.py`):**
Must exit 0 before any live capital is committed. Validates: credentials point to the live endpoint, buying power meets minimum, `SLIPPAGE_DRIFT_ENABLED=True`, dry-run cycle passes, go/no-go file on disk with GO verdict.

---

## Go/No-Go Framework

Before committing live capital, ALL of the following must be satisfied:

1. Minimum **50 closed trades** in paper trading (statistical significance)
2. Paper trading period spans **at least 4 weeks** across varying market conditions, with all four strategies active
3. All five metrics meet their thresholds (see table above)
4. Bot has run for at least **72 hours continuously** without crashes or errors
5. Risk manager daily halt has never been triggered without being intentional
6. `scripts/preflight.py` exits 0 against the live endpoint
7. `SLIPPAGE_DRIFT_ENABLED=True` — kill switch calibrated from real fills

Run the checker: `python scripts/gonogo.py` (exit code 0 = GO, 1 = NO-GO).

For slow daily-bar strategies, 50 closed trades may not be attainable in a 2–4 week paper window. In that case, forward-test reconciliation and operational stability are the primary stabilization gates.

---

## Adding a New Strategy — Checklist

When implementing any new equity strategy:

1. Create `strategies/<strategy_name>.py`
2. Inherit from `BaseStrategy`
3. Set `name` class attribute — unique lowercase string
4. Set `preferred_order_type` — `OrderType.MARKET` or `OrderType.LIMIT`
5. Implement `_raw_signals(df) -> SignalFrame` — entries/exits boolean Series
6. Override `required_bars()` if the strategy needs more than 50 bars
7. Create `strategies/filters/<strategy_name>.py` with an edge filter returning `EdgeFilterDecision`
8. Add an entry to `STRATEGY_ALLOCATIONS` in `config/settings.py`
9. Add unit tests in `tests/test_strategies.py` and `tests/test_filters.py`
10. Add a `StrategySlot` with `allowed_regimes` in `forward_test.py`
11. Update `docs/strategies.md`

### Additional steps for options strategies

12. Implement `build_option_execution(symbol, underlying_price) -> tuple | None` — returns `(occ_symbol, limit_price, take_profit, stop_loss)` or `None` to abort
13. Implement `inspect_open_positions(position, latest_close) -> bool` — mid-trade exit guards (time stop, delta floor, trailing stop, etc.)
14. Use `utils/options_lookup.find_best_call` (or an equivalent) to select the contract
15. Add tests for `build_option_execution`, `inspect_open_positions`, and each exit guard in `tests/test_<strategy_name>.py`
16. **Check PLAN.md item 11.23** before adding a second options strategy on the same underlying — `_position_owners` is keyed by the underlying ticker and two strategies on the same underlying would collide

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
| `blackscholes` | Black-Scholes option pricing for mid-trade delta and value guards |
| `yfinance` | VIX daily fetch for Black-Scholes sigma input (options only) |

---

## What to Avoid

- Never place orders directly from a strategy — all orders go through Risk Manager
- Never hardcode API keys, URLs, or trading parameters
- Never use `alpaca-trade-api` (deprecated) — use `alpaca-py` only
- Never assume the market is open — always check the market clock
- Never let an unhandled exception crash the bot silently — use structured error handling and logging
- Never commit `.env`, `data/trades.db`, or `data/trades_live.db` to version control
- Never use `pandas-ta` — indicators are hand-rolled to eliminate dependency risk
- Never set `LIVE_TRADING=true` before `scripts/preflight.py` exits 0
- Never mix paper and live trade logs — they are separate databases by design
- Never call `build_option_execution` or `inspect_open_positions` from inside the risk manager — these are strategy concerns, called by the broker and engine respectively
- Never key new options ownership state by the OCC string without first resolving the 11.23 migration — use the underlying ticker for now and document the limitation
