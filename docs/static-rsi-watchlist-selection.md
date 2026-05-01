# Static RSI Watchlist Selection

## Purpose

Define the **static RSI watchlist builder** for the RSI mean-reversion
strategy.

This document is about selecting a persistent stock universe from multi-year
backtest evidence, not about finding the best RSI setups today.

The static builder answers:

> Which liquid, stock-only names have historically produced the best RSI
> mean-reversion behavior over a long validation window?

The strategy still controls entry and exit timing.

## Responsibility Split

```text
Universe -> Static Builder -> Static RSI Watchlist -> raw RSI Signal -> RSI Edge Filter -> Risk -> Execution
```

- Static builder: selects the persistent RSI universe from long-run evidence.
- RSI strategy: detects oversold/overbought threshold crossings.
- Edge filter: confirms or rejects raw RSI entries under current conditions.
- Risk manager: sizes positions and enforces stops, exposure, and kill switches.

The static builder answers:

> Which stocks deserve a place in the long-run RSI basket?

The RSI strategy answers:

> Is there an actual oversold entry signal now?

The RSI edge filter answers:

> Is this raw RSI signal allowed to trade right now?

Do not embed static-universe logic inside `RSIReversion`.
Do not treat the dynamic scanner as the source of truth for the static basket.

## Core Principle

The static RSI watchlist is **backtest-first**.

It should prefer names that:

- produce enough RSI trades to evaluate honestly
- have strong long-run profitability
- keep drawdowns survivable
- remain liquid and institutionally tradable

In plain English:

> The static basket should be built from names that actually behaved well under
> the exact RSI strategy, not from short-horizon scanner heuristics alone.

## Static Watchlist Vs Dynamic Watchlist

### Static RSI Watchlist

A persistent basket rebuilt only occasionally.

Purpose:

- maximize long-run RSI profitability
- maintain enough trade density
- keep the RSI sleeve from becoming opportunity-starved

Primary source:

- `scripts/rsi_static_universe.py`

### Dynamic RSI Watchlist

A current-state scanner for names that look attractive right now.

Purpose:

- improve short-horizon candidate quality
- adapt to current structure, liquidity, and oversold behavior

Primary source:

- `scripts/rsi_watchlist_scan.py`

These two systems should remain separate.

## Current Static Builder

Primary implementation:

- builder: `scripts/rsi_static_universe.py`
- validator: `scripts/rsi_candidate_validate.py`
- backtest report: `scripts/rsi_backtest_report.py`

Research artifacts:

- static-universe selection report: `docs/reports/rsi_static_universe_latest.md`
- static-universe backtest report: `docs/reports/rsi_static_backtest_report_latest.md`
- hybrid comparison report: `docs/reports/rsi_hybrid_comparison.md`
- combined-portfolio backtest report: `docs/reports/rsi_portfolio_backtest_latest.md`

## Static Universe Rule

A symbol is eligible for the static RSI builder only if it survives all
required hard gates and ranks well on exact-strategy backtest metrics.

### 1. Tradability

Required:

- Alpaca active US equity
- tradable through Alpaca
- stock-like product only
- sufficient clean daily bars for the validation window

Excluded:

- ETFs
- ETNs
- funds
- trusts
- indices
- warrants, rights, units, preferred-like wrappers
- blacklisted names such as `DINO`

### 2. Size And Liquidity

Required:

- market capitalization `>= 2,000,000,000`
- latest close `>= 10.00`
- 20-day average share volume `>= 500,000`
- 50-day average dollar volume `>= 50,000,000`

Rationale:

The static basket should be broad enough to produce trades, but still limited
to names that can realistically be traded under stress without turning the RSI
sleeve into a garbage collector.

### 3. Financial Survival

Required:

- solvency check must pass under `scripts/watchlist_review.py`

Rationale:

RSI can buy temporary weakness, but it should not routinely lean into names
with obvious solvency risk.

### 4. Backtest-First Validation

Required process:

- fetch the long validation window
- run the exact project `RSIReversion` strategy
- score the symbol from realized strategy behavior, not recent scanner behavior

Current validation settings:

- RSI period: `14`
- oversold: `30`
- overbought: `70`
- initial cash per symbol: `$100,000`
- slippage: `5 bps`
- commission: `$0`
- validation window: `1825` calendar days

### 5. Ranking Survivors

Primary ranking drivers:

1. Sharpe
2. total return
3. trade count
4. profit factor

Secondary quality modifiers:

1. event hit rate
2. stop-failure rate
3. drawdown penalty

Rationale:

The static basket must be profitable first, but sample size still matters.
A high-return name with almost no trades is less useful than a similarly strong
name that produces enough RSI opportunities to matter.

### 6. Basket Assembly

Current basket controls:

- target size: `24`
- sector cap: `4`
- `REJECT` names excluded automatically
- `WATCH` names allowed, but must be reviewed manually before promotion

Rationale:

The first assembly step is mechanical, but final promotion is discretionary.
The report should surface promising-but-imperfect names instead of silently
dropping them.

## Published Results

### Per-Symbol Static Basket Summary

From `docs/reports/rsi_static_backtest_report_latest.md` over
`2021-05-02` to `2026-05-01` using the exact project RSI logic:

- symbols tested: `24`
- total trades across the basket: `150`
- trades per month across the basket: `2.50`
- average per-symbol strategy return: `126.5%`
- median per-symbol strategy return: `116.3%`
- average per-symbol Sharpe: `1.08`
- median per-symbol Sharpe: `1.06`
- average per-symbol max drawdown: `-26.3%`
- median per-symbol max drawdown: `-24.5%`
- average per-symbol win rate: `90.0%`

This is a per-symbol aggregate, not a shared-capital portfolio simulation.

### True Combined-Portfolio RSI Basket Summary

From `docs/reports/rsi_portfolio_backtest_latest.md` using one shared-capital
equity curve per basket, equal cash splits on same-day entries, and
`max_positions=5`:

| Basket | Return | CAGR | Sharpe | Sortino | MaxDD | Trades | Final Equity |
|---|---:|---:|---:|---:|---:|---:|---:|
| `current` | 193.4% | 24.1% | 0.86 | 1.18 | -39.6% | 18 | $293,389.32 |
| `static` | 520.0% | 44.1% | 1.17 | 1.50 | -53.9% | 21 | $619,951.66 |
| `hybrid2` | 450.2% | 40.7% | 1.08 | 1.36 | -53.9% | 18 | $550,156.92 |

This table is the authoritative source for true portfolio-level Sharpe across
the tested RSI baskets.

## Rebuild Workflow

### 1. Run The Focused Test Suite

```bash
/Users/franco/trading-bot/venv/bin/pytest \
  tests/test_rsi_static_universe.py \
  tests/test_rsi_watchlist_scan.py \
  tests/test_rsi_candidate_post_analysis.py
```

### 2. Build The Static-Universe Report

```bash
/Users/franco/trading-bot/venv/bin/python scripts/rsi_static_universe.py \
  --feed sip \
  --end-delay-minutes 60 \
  --fundamentals-pool-size 45 \
  --output docs/reports/rsi_static_universe_latest.md
```

Outputs:

- `docs/reports/rsi_static_universe_latest.md`
- `data/rsi_static_fundamentals_cache.json`

Notes:

- the coarse prefilter is price + liquidity + stock-only + blacklist
- the `$2B` market-cap and solvency checks are applied late, on the strongest
  ranked names, to avoid hammering Yahoo Finance during broad scans
- the fundamentals cache should be reused on future rebuilds

### 3. Publish The Exact RSI Backtest Results

After the static basket is selected, publish a formal backtest report for that
exact symbol set.

Command used for the current static basket:

```bash
/Users/franco/trading-bot/venv/bin/python scripts/rsi_backtest_report.py \
  --symbols IBM ABBV CRDO WFC CVX ANET IONQ CAT OXY BE XOM RTX AXP BKNG BAC GS CEG LMT WMT LLY PG LIN AMGN TMUS \
  --comparisons ARM SPG \
  --lookback-days 1825 \
  --feed sip \
  --end-delay-minutes 60 \
  --output docs/reports/rsi_static_backtest_report_latest.md
```

This reproduces the exact RSI backtest settings used for the published static
universe report:

- strategy file: `strategies/rsi_reversion.py`
- report script: `scripts/rsi_backtest_report.py`
- RSI period: `14`
- oversold: `30`
- overbought: `70`
- fill model: next open
- initial cash: `$100,000` per symbol
- slippage: `5 bps`
- commission: `$0`
- feed: `sip`
- end delay: `60` minutes
- lookback: `1825` calendar days

Outputs:

- `docs/reports/rsi_static_backtest_report_latest.md`

Optional local diagnostics:

```bash
/Users/franco/trading-bot/venv/bin/python scripts/rsi_backtest_report.py \
  --symbols IBM ABBV CRDO WFC CVX ANET IONQ CAT OXY BE XOM RTX AXP BKNG BAC GS CEG LMT WMT LLY PG LIN AMGN TMUS \
  --comparisons ARM SPG \
  --lookback-days 1825 \
  --feed sip \
  --end-delay-minutes 60 \
  --output docs/reports/rsi_static_backtest_report_latest.md \
  --chart-dir docs/reports/rsi_static_backtests
```

The PNG charts are optional local research artifacts and do not need to be
committed to GitHub to verify the published markdown results.

### 4. Publish The True Combined-Portfolio Backtest

```bash
/Users/franco/trading-bot/venv/bin/python scripts/rsi_portfolio_backtest.py \
  --feed sip \
  --end-delay-minutes 60 \
  --max-positions 5 \
  --output docs/reports/rsi_portfolio_backtest_latest.md
```

Optional local portfolio charts:

```bash
/Users/franco/trading-bot/venv/bin/python scripts/rsi_portfolio_backtest.py \
  --feed sip \
  --end-delay-minutes 60 \
  --max-positions 5 \
  --output docs/reports/rsi_portfolio_backtest_latest.md \
  --chart-dir docs/reports/rsi_portfolio_backtests
```

### 5. Review The Result

Open:

- `docs/reports/rsi_static_universe_latest.md`
- `docs/reports/rsi_static_backtest_report_latest.md`
- `docs/reports/rsi_portfolio_backtest_latest.md`

Review, in order:

1. final basket summary vs current `RSI_WATCHLIST`
2. ranked universe and any `WATCH` names in the assembled basket
3. near misses blocked by sector caps
4. exact per-symbol backtest results
5. true combined-portfolio Sharpe, CAGR, and drawdown
6. whether total basket trade count remains in the desired range

## Hybrid Follow-Up

If the static basket is strong but one or two current-watchlist names look
materially better, build a small hybrid and compare basket summaries.

Saved example:

- `docs/reports/rsi_hybrid_comparison.md`

As of `2026-05-01`, the preferred hybrid was:

- remove: `TMUS`, `AMGN`
- add: `ARM`, `SPG`

This was chosen because it improved return and Sharpe while keeping drawdown
roughly unchanged.

## Promotion Rule

Do not update `config/settings.py` automatically.

Promote a new static RSI watchlist only after:

1. the static-universe report is generated successfully
2. the dedicated backtest report is published successfully
3. the combined-portfolio backtest is published successfully
4. the final basket clearly beats or justifies replacing the current promoted list
5. any remaining `WATCH` names are reviewed manually
6. the user approves the promoted basket

## Recommended Cadence

Quarterly rebuild:

1. rerun the focused tests
2. rebuild the static-universe report
3. refresh the dedicated backtest report
4. refresh the combined-portfolio backtest report
5. review any hybrid opportunities
6. decide whether to keep, hybridize, or promote
