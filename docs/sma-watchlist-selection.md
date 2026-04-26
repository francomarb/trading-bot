# SMA Watchlist Selection

## Purpose

Define the stock-selection rules for the SMA crossover strategy.

The SMA crossover strategy should not scan for "interesting" stocks. It should
watch only liquid stocks that are already in broad, confirmed uptrends. The
strategy then waits for its own 20/50 SMA crossover signal.

This document is intended to be stable. Changes to these rules should be treated
as strategy-spec changes and versioned deliberately.

## Responsibility Split

Symbol selection and trade timing are separate concerns.

```text
Universe -> Watchlist Source -> Strategy Watchlist -> SMA Signal -> Risk -> Execution
```

- Universe: the broad set of symbols that may be considered.
- Watchlist source: static list or dynamic selector that returns symbols.
- SMA strategy: entry and exit logic only.
- Risk manager: sizing, exposure, loss limits, and kill switches.
- Execution: broker order placement.

The SMA strategy answers:

> Is there a valid 20/50 SMA crossover signal now?

The watchlist selector answers:

> Which symbols are even eligible for SMA trend following?

Do not embed candidate-selection logic inside `SMACrossover`.

## Architecture Direction

The bot should support two watchlist styles:

### Static Watchlist

A handpicked list of symbols in config.

Use this for:

- early paper testing
- stable reconciliation windows
- debugging
- deliberately curated portfolios

Properties:

- simple
- explainable
- low churn
- easy to reproduce

### Dynamic Watchlist

A rules-based watchlist source that rebuilds symbols periodically.

Use this only after the static strategy is operationally stable.

Properties:

- adaptive
- more complex
- easier to overfit
- must be deterministic and auditable

### Generic Contract

Dynamic watchlists should be implemented behind a generic interface, not as an
SMA-only special case.

Conceptual contract:

```python
class WatchlistSource:
    name: str

    def symbols(self) -> list[str]:
        """Return the current symbols to watch."""
```

Possible implementations:

- `StaticWatchlistSource`
- `ConfigWatchlistSource`
- `AlpacaAssetUniverseSource`
- `CsvWatchlistSource`
- `DynamicFilteredWatchlistSource`

Dynamic sources may use a strategy-specific selector or filter internally, but
the engine should only care that a list of symbols is returned.

Examples:

- SMA dynamic watchlist uses a trend-following selector.
- RSI dynamic watchlist uses a mean-reversion selector.
- A future breakout strategy uses a breakout/liquidity selector.

This keeps the engine scalable and avoids hard-coding strategy-specific scanner
logic into the orchestration layer.

### Dynamic Watchlist Guardrails

Dynamic watchlists are operationally riskier than static lists. They must be
introduced with guardrails before they can drive live or paper trading.

Required guardrails:

- do not remove a symbol with an open position unless ownership and exit rules
  are explicit
- do not change the active watchlist mid-paper-run when the run is being used
  for reconciliation or GO/NO-GO analysis
- cache every generated watchlist with timestamp, rule version, data timestamp,
  and selected symbols
- log rejection counts for every hard filter
- log manual overrides separately from rule-driven selections
- run new dynamic selectors in report-only mode before allowing them to drive a
  strategy slot
- cap turnover so small daily data changes cannot churn the entire watchlist
- keep strategy ownership durable across restarts before enabling dynamic
  multi-strategy watchlists

If a dynamic watchlist changes while a position is open, the position remains
owned by the strategy that opened it. Watchlist membership is not the same as
position ownership.

## Current State

Today, SMA uses a static list in `config/settings.py`:

```python
SMA_WATCHLIST = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "META", "NVDA", "AVGO", "AMD",
    "MU", "TSLA", "ORCL", "ANET", "MRVL", "MELI", "GS", "BAC",
]
```

The active paper engine passes this static list into `StrategySlot`.

No automated SMA candidate selector exists yet.

## SMA Candidate Rule

A symbol is eligible for the SMA crossover watchlist only if all hard filters
pass.

### 1. Tradability

Required:

- Alpaca active US equity
- tradable through Alpaca
- regular listed security, not OTC
- at least 260 daily bars available

Excluded:

- leveraged ETFs
- inverse ETFs
- symbols with unreliable or incomplete daily data
- manually flagged names that cannot be reconciled or traded cleanly

### 2. Liquidity

Required:

- market capitalization >= 2,000,000,000
- latest close >= 10.00
- 20-day average share volume >= 500,000
- 50-day average dollar volume >= 50,000,000

Rationale:

SMA crossover is a market-order trend strategy. It needs clean fills and should
not depend on thin liquidity. The market-cap floor keeps the watchlist away
from very small companies where one news cycle, financing event, or thin holder
base can overwhelm the technical trend.

### 3. Confirmed Uptrend

Required:

- close > SMA50
- close > SMA150
- close > SMA200
- SMA50 > SMA150
- SMA150 > SMA200
- SMA200 today > SMA200 from 20 trading days ago

Rationale:

This follows the same broad consensus as trend-template style screens: trade
stocks already in sustained Stage 2-style uptrends, not stocks that merely
look cheap or recently bounced.

### 4. 52-Week Strength

Required:

- close >= 1.30 * 52-week low
- close >= 0.75 * 52-week high

Rationale:

Trend-following candidates should be close to leadership, not deep in damaged
recovery territory.

### 5. Relative Strength

Required:

- 12-month momentum excluding the most recent month ranks in the top 30% of
  the candidate universe

Equivalent:

- relative strength percentile >= 70

Rationale:

Academic and practitioner evidence both favor buying strength over weakness.
SMA crossover should focus on leaders, not laggards.

### 6. Trend Strength

Required:

- ADX14 >= 20
- +DI > -DI

Preferred:

- ADX14 >= 25
- ADX14 rising over the last 5 trading days

Rationale:

Moving average crossovers are vulnerable to sideways-market whipsaws. ADX is a
trend-strength filter; it does not replace the SMA signal.

### 7. Volatility Sanity

Required:

- ATR14 / close >= 0.01
- ATR14 / close <= 0.08

Rationale:

Reject names that are too quiet to move or too chaotic for stable trend
following.

### 8. Fundamental Sanity

Required for SMA:

- annual free cash flow > 0
- YoY revenue growth > 0
- profitable, or if unprofitable, cash runway >= 18 months

Excluded:

- pre-profit "story" names with no earnings anchor
- companies with unresolved solvency concerns
- active bank-debt crisis or similar financing stress

Rationale:

The existing watchlist review logic treats deteriorating fundamentals as a poor
fit for SMA trend following. SMA should not depend on a pure narrative stock
holding its trend after sentiment changes.

### 9. Portfolio Hygiene

Required:

- no more than 3 symbols per sector in the final SMA watchlist
- no symbol should be added or removed mid-paper-run
- watchlist changes must be made before a new paper/reconciliation window

Preferred:

- final list size between 10 and 25 symbols
- keep stable names unless a hard filter fails
- document every manual override

Rationale:

Concentration and watchlist churn can contaminate forward-test results and make
strategy attribution harder.

## Ranking Survivors

If more symbols pass than the target list size allows, rank survivors.

Recommended ranking inputs:

1. relative strength percentile
2. Consolidation Score (penalizes parabolic exhaustion above SMA50)
3. Freshness / Coil Score (rewards tightness between SMA20 and SMA50)
4. ADX14 level and slope
5. 50-day average dollar volume
6. trend smoothness, measured by fewer 20/50 crossovers over the last year
7. sector diversification

Do not rank by recent one-day price jump alone.

## What Stays Out Of The Core Rule

These may be useful later, but should not be hard admission rules for the first
SMA watchlist selector:

- RSI overbought or oversold
- MACD crossover
- Bollinger Band breakout
- SuperTrend
- news sentiment
- analyst ratings
- social media attention

Reason:

Most of these either duplicate moving-average information, belong to a different
strategy, or introduce data dependencies that make the first implementation less
auditable.

## Static To Dynamic Migration

Recommended rollout:

1. Keep the current static `SMA_WATCHLIST` during the active paper run.
2. Use this document to review the static list after the run.
3. Implement a generic watchlist-source abstraction.
4. Add a dynamic SMA selector behind that abstraction.
5. Run the selector in report-only mode first.
6. Compare static vs dynamic candidates for several weeks.
7. Only then allow the dynamic source to drive a paper-trading slot.

The dynamic selector must log:

- base universe size
- symbols removed by each hard filter
- final candidate count
- selected symbols
- rule version
- data timestamp

## Rule Version

Current rule version: `sma_watchlist_v1`

Changing any hard threshold creates a new rule version.

Examples:

- changing minimum market cap from 10B to 5B
- changing minimum dollar volume from 50M to 25M
- replacing ADX with another trend-strength indicator
- removing the 52-week high/low constraints
- changing the relative strength percentile

Adding a new data source without changing rules does not require a new strategy
rule version, but should still be noted in implementation docs.

## Sources

- Mark Minervini-style trend template: price above 50/150/200 day moving
  averages, moving-average alignment, rising 200-day average, near 52-week high,
  and relative strength.
- StockCharts Technical Rank methodology: relative strength across multiple
  timeframes, with heavier weight on long-term and medium-term trend.
- StockCharts ADX scans: liquid stocks, average price above 10, price above
  50-day SMA, ADX above 20, and +DI above -DI for long trend candidates.
- AQR and academic trend-following literature: time-series momentum has shown
  persistence across markets and long histories.
- Jegadeesh and Titman momentum research: stocks with strong 3-to-12 month past
  returns tend to outperform over intermediate horizons.
- Existing project rule: SMA requires positive FCF, revenue growth, and solvency
  checks from `scripts/watchlist_review.py`.
