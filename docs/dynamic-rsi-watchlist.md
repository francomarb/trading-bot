# Dynamic RSI Watchlist Design

## Purpose

Define a **dynamic RSI watchlist selector** (stock scanner) that builds a high-quality candidate list for the RSI mean-reversion strategy.

This document formalizes the distinction between:

- **Static watchlist**: a fixed list of symbols chosen manually
- **Dynamic watchlist / scanner**: a rules-based system that selects the best symbols to watch right now

The goal is not to predict winners directly.
The goal is to improve RSI trade quality by narrowing attention to symbols where
mean reversion has a better chance of working.

---

## Core Idea

The scanner answers:

> **Which stocks are worth watching for RSI today?**

Then RSI answers:

> **Is there an actual entry signal right now?**

This keeps responsibilities clean:

- scanner = symbol selection
- edge filter = regime / eligibility gating
- RSI strategy = entry/exit logic
- risk engine = sizing / exposure control
- execution = order placement

---

## Static Watchlist vs Dynamic Watchlist

### Static Watchlist

A manually selected list of symbols that does not change often.

Example:

- AAPL
- MSFT
- NVDA
- META
- AMZN
- GOOGL
- AVGO
- AMD
- MU
- QQQ
- SMH

#### Advantages

- simple
- stable
- easy to debug
- ideal for early paper testing

#### Disadvantages

- can become stale
- may include poor-fit symbols for RSI
- may miss stronger current opportunities

---

### Dynamic Watchlist / Scanner

A rules-based selector that rebuilds the RSI candidate list periodically.

Example concept:

- start with a broad but trusted universe
- remove illiquid or broken names
- score remaining symbols
- keep only the top N

#### Advantages

- adapts to market conditions
- improves candidate quality
- reduces noise
- avoids broken or inactive names

#### Disadvantages

- more moving parts
- harder to debug
- easier to overengineer
- can cause watchlist churn if not controlled

---

## Recommended Architecture Placement

The scanner should be a separate layer **before** RSI.

### Desired flow

```text
Universe → Scanner → Dynamic Watchlist → RSI Edge Filter → RSI Signal → Risk → Execution
```

This is important because symbol selection is not the same as signal generation.

Do **not** embed scanner logic inside the RSI strategy itself.

---

## Recommended Initial Design

### Phase 1: Hybrid model

Start with:

- a **static base universe**
- a **dynamic scanner on top of that base**

This is the safest version.

Example base universe:

- mega-cap tech
- semiconductors
- liquid growth names
- a few tech/market ETFs

This avoids scanning the entire market too early.

---

## Base Universe Characteristics

The base universe should contain stocks that are:

- liquid
- widely traded
- institutionally relevant
- easy to fill
- compatible with your existing strategy themes

Suggested examples:

- AAPL
- MSFT
- GOOGL
- AMZN
- META
- NVDA
- AVGO
- AMD
- MU
- TSLA
- NFLX
- NOW
- QQQ
- SMH
- XLK

The exact list can evolve, but the universe should remain curated.

---

## Scanner Objective

The dynamic watchlist should select symbols that are:

- healthy enough to support mean reversion
- liquid enough to trade cleanly
- volatile enough to create setups
- not so chaotic that RSI becomes noise

---

## Minimum Hard Filters

Before scoring, remove symbols that fail basic quality standards.

### Required baseline filters

- minimum price threshold
- minimum average daily dollar volume
- price above 50-day SMA
- optional: price above 200-day SMA
- volatility within acceptable range

### Example filter goals

Reject symbols that are:

- illiquid
- structurally weak
- too quiet to matter
- too chaotic for clean mean reversion

---

## Recommended Scoring Dimensions

After hard filters, rank the survivors.

### 1. Trend Quality

RSI longs work better in healthy uptrends.

Possible inputs:

- price above 50-day SMA
- price above 200-day SMA
- positive 20-day return
- rising 50-day SMA

Purpose:

- avoid broken names
- favor strong trends with controlled pullbacks

---

### 2. Liquidity Quality

The strategy must be tradeable in practice.

Possible inputs:

- average daily dollar volume
- spread quality (future enhancement)
- consistency of trading activity

Purpose:

- improve fills
- reduce slippage risk
- reduce execution noise

---

### 3. Volatility Quality

Need enough movement to create RSI setups, but not pure chaos.

Possible inputs:

- ATR / price ratio
- historical intraday range
- gap frequency (later enhancement)

Purpose:

- avoid dead names
- avoid meme-like instability

---

### 4. Pullback Potential

Favor names that produce tradable dips rather than straight collapses.

Possible inputs:

- recent RSI excursions
- tendency to revert after oversold conditions
- pullback behavior within an uptrend

Purpose:

- improve practical RSI opportunity rate

---

## Example Composite Score

A simple first version could be:

```text
score =
  trend_score
+ liquidity_score
+ pullback_score
- chaos_penalty
```

Keep the first version simple and explainable.

---

## Example Pseudocode

```python
def build_dynamic_rsi_watchlist(symbols, market_data):
    candidates = []

    for symbol in symbols:
        d = market_data[symbol]

        if d.close < d.sma50:
            continue

        if d.avg_dollar_volume < 20_000_000:
            continue

        vol_ratio = d.atr14 / d.close
        if vol_ratio < 0.015 or vol_ratio > 0.06:
            continue

        score = d.return_20d + d.sma50_slope
        candidates.append((symbol, score))

    candidates.sort(key=lambda x: x[1], reverse=True)
    return [symbol for symbol, _ in candidates[:10]]
```

This is only an example.
Exact thresholds should be tuned carefully and slowly.

---

## Refresh Frequency

Do not rebuild the watchlist too often.

### Recommended initial cadence

- once per day

### Acceptable alternative

- once per week

### Not recommended yet

- intraday watchlist changes
- minute-by-minute rescans

Daily refresh is the best tradeoff for the current architecture:

- adaptive enough
- stable enough to debug

---

## Interaction with RSI Edge Filter

The scanner and the edge filter are different layers.

### Scanner

Selects symbols worth monitoring

### Edge filter

Decides whether a signal is allowed under current conditions

That means:

- the scanner narrows the field
- the edge filter prevents known-bad regime entries
- the RSI strategy still controls the actual setup logic

---

## Interaction with Capital Allocation

A scanner should improve capital usage by reducing low-quality candidates.

However, it does **not** replace proper capital allocation.

You still need:

- per-strategy capital budgets
- max open positions
- portfolio-level exposure caps

Without those, the scanner alone cannot prevent one strategy from starving another.

---

## Success Criteria

The scanner is successful if it improves behavior, not just if it picks “good stocks.”

### Primary success criteria

- fewer RSI trades in broken names
- more RSI activity in healthy names
- better quality of candidate symbols
- cleaner use of allocated capital
- fewer obvious junk setups

### System success criteria

- no instability from daily watchlist refresh
- no confusion in logs
- no strategy ownership problems caused by watchlist updates
- watchlist updates are deterministic and explainable

### Comparative success criteria

Compared to a static watchlist, the scanner should produce:

- fewer low-quality trades
- fewer symbols that never produce meaningful setups
- better alignment with current market conditions

---

## Observability Requirements

The scanner must be observable and auditable.

### Required logs

For each refresh:

- scanner started
- base universe size
- symbols removed by each hard filter
- final candidate count
- selected symbols
- optional: top scores and why they ranked highly

### Example log lines

```text
RSI scanner started: base_universe=15
RSI scanner filtered out 3 symbols below 50 SMA
RSI scanner filtered out 2 symbols on low dollar volume
RSI scanner selected 10 symbols: AAPL, MSFT, NVDA, META, AMZN, AVGO, AMD, MU, QQQ, SMH
```

### Helpful metrics

Track:

- daily selected symbol count
- per-filter rejection counts
- watchlist turnover rate
- average score of selected symbols
- how often selected names actually generate RSI signals

---

## Failure Modes

### Common risks

- over-filtering → too few symbols
- under-filtering → noisy bad candidates
- excessive watchlist churn
- unstable thresholds
- scanner selecting symbols that conflict with RSI edge filter too often

### Stop / investigate conditions

- watchlist changes too drastically day to day
- selected symbols repeatedly fail the RSI edge filter
- scanner often produces fewer than the minimum viable count
- selected names are too volatile or too illiquid in practice

---

## Rollout Plan

### Phase 1

Use static watchlist only

### Phase 2

Use static base universe + dynamic scanner

### Phase 3

Run scanner daily with RSI in paper mode

### Phase 4

Evaluate whether scanner improves trade quality

### Phase 5

Only later consider broader market scanning or more advanced ranking

---

## Recommended Sequence for Your Bot

1. finish strategy-level capital allocation
2. activate RSI with static watchlist + edge filter
3. validate RSI behavior in paper mode
4. add dynamic watchlist scanner as version 2
5. compare scanner vs static watchlist performance and behavior

This sequencing is important.
Do not introduce the scanner before the basic RSI deployment is stable.

---

## What Not To Do Yet

Avoid:

- scanning the entire market immediately
- intraday rescans
- sentiment-driven scanner selection
- machine-learning ranking
- complex adaptive weighting
- options-aware symbol selection

These can come later.

---

## Design Philosophy

The scanner should be:

- simple
- explainable
- deterministic
- easy to audit
- separate from strategy logic

It should **not** become a hidden second strategy.

---

## Future Evolution

Later, this scanner can evolve into a more advanced selection layer.

Possible future additions:

- broader universe selection
- regime-aware scanner weights
- volatility-aware ranking
- sentiment-aware gating
- integration with `MetaSignalState`
- integration with `TradeIntent`

Long-term direction:

```text
Universe → Scanner → Watchlist → MetaSignal → Strategy → Position Expression → Execution
```

But for now, the scanner should remain a lightweight pre-selection tool.

---

## Key Insight

A dynamic RSI watchlist is not trying to predict the market.

It is trying to answer:

> **Which symbols deserve RSI attention right now?**

That alone can materially improve trade quality.

---

## Summary

Use a dynamic RSI watchlist as a stock scanner layered on top of a curated base universe.

Start simple:

- curated universe
- hard filters
- simple ranking
- daily refresh

The best first version is not the smartest one.
It is the one that is easiest to trust, debug, and validate in paper trading.
