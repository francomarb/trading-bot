# Static RSI Watchlist Workflow

This document explains how to regenerate the static RSI watchlist using the
current research scripts.

## Goal

Build a `20-30` name static RSI universe that is:

- stock-only
- liquid
- market cap `>= $2B`
- profitability-first
- evaluated with the exact project RSI strategy over a multi-year window

This workflow is intentionally separate from the dynamic scanner in
`scripts/rsi_watchlist_scan.py`.

## High-Level Flow

1. Run the focused RSI tests.
2. Build the static-universe report with `scripts/rsi_static_universe.py`.
3. Review the final basket, the ranked universe, and any `WATCH` names.
4. If needed, build a small hybrid from the best current-watchlist names.
5. Promote a new watchlist only after manual review.

## Scripts Involved

### `scripts/rsi_static_universe.py`

Primary static-universe builder.

Responsibilities:

- fetch the tradable stock universe from Alpaca
- apply a coarse recent-window filter:
  - blacklist
  - stock-only product exclusions
  - minimum price
  - minimum share volume
  - minimum dollar volume
- run the exact RSI validation/backtest over the long window
- rank symbols by backtest-first metrics
- apply the hard market-cap / solvency gate to the strongest ranked names
- assemble the final basket with a sector cap
- compare the resulting basket against the current promoted `RSI_WATCHLIST`

### `scripts/rsi_candidate_validate.py`

Validation companion used by the static builder.

Responsibilities:

- extract RSI oversold events
- run the exact `RSIReversion` strategy through the backtester
- emit per-symbol strategy statistics used by the static builder ranking

### `scripts/rsi_watchlist_scan.py`

Dynamic scanner for current-state opportunities.

Use this only for the dynamic RSI view, not for the authoritative static
watchlist rebuild.

## Test Before Running

Run the focused RSI suite:

```bash
/Users/franco/trading-bot/venv/bin/pytest \
  tests/test_rsi_static_universe.py \
  tests/test_rsi_watchlist_scan.py \
  tests/test_rsi_candidate_post_analysis.py
```

Expected result as of `2026-05-01`:

```text
21 passed
```

## Generate the Static RSI Report

Run the static builder with SIP data and the late-stage fundamentals cache:

```bash
/Users/franco/trading-bot/venv/bin/python scripts/rsi_static_universe.py \
  --feed sip \
  --end-delay-minutes 60 \
  --fundamentals-pool-size 45 \
  --output logs/rsi_static_universe_latest.md
```

Primary outputs:

- report: `logs/rsi_static_universe_latest.md`
- fundamentals cache: `data/rsi_static_fundamentals_cache.json`

On the first run, the cache file may not exist yet. The script creates it.

## What the Static Builder Does

### Stage 1: Recent prefilter

Current hard gates from `StaticUniverseConfig`:

- minimum price: `$10`
- minimum 20-day average share volume: `500,000`
- minimum 50-day average dollar volume: `$50M`
- stock-only product filter
- blacklist: currently `DINO`

### Stage 2: Long-window validation

The builder fetches the long validation window and reuses the exact project RSI
validation logic from `scripts/rsi_candidate_validate.py`.

### Stage 3: Backtest-first ranking

Primary ranking drivers:

- Sharpe
- total return
- trade count
- profit factor

Secondary quality modifiers:

- event hit rate
- stop-failure rate
- drawdown penalty

### Stage 4: Late-stage fundamentals gate

To avoid hammering Yahoo Finance during broad scans, the hard market-cap and
solvency check is applied only to the strongest ranked names.

Late-stage hard gates:

- market cap `>= $2B`
- solvency check from `scripts/watchlist_review.py`

Cache file:

- `data/rsi_static_fundamentals_cache.json`

This cache should be reused for future quarterly rebuilds.

### Stage 5: Basket assembly

The builder assembles the final basket with:

- target size: `24`
- sector cap: `4`

The report includes:

- final selected basket
- ranked universe
- near misses
- recent-prefilter rejection counts
- direct comparison against the current promoted `RSI_WATCHLIST`

## How To Review The Result

Open:

- `logs/rsi_static_universe_latest.md`

Check, in order:

1. Final basket summary vs current `RSI_WATCHLIST`
2. Any `WATCH` names that slipped into the final basket
3. Near misses blocked by sector caps
4. Whether trade count stays near the target range (`~150` trades over the standardized window)

## Hybrid Follow-Up

If the static basket looks strong but still contains a few weak `WATCH` names,
build a hybrid by:

1. identifying the weakest `WATCH` names already in the static basket
2. identifying the strongest current-watchlist names not already in the static basket
3. swapping a small number of names
4. comparing basket summaries

Saved example:

- `logs/rsi_hybrid_comparison.md`

As of `2026-05-01`, the preferred hybrid was `Hybrid 2`:

- remove: `TMUS`, `AMGN`
- add: `ARM`, `SPG`

## Promotion Rule

Do not update `config/settings.py` automatically.

Promote a new RSI watchlist only after:

1. the static builder report is generated successfully
2. the final basket clearly beats or justifies replacing the current promoted list
3. any remaining questionable `WATCH` names are reviewed
4. the user approves the promoted basket

## Recommended Cadence

Quarterly rebuild:

1. refresh the static report
2. reuse the fundamentals cache
3. review the final basket and near misses
4. decide whether to keep, hybridize, or promote
