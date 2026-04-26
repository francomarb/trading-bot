# RSI Watchlist Selection

## Purpose

Define stock-selection rules for the RSI mean-reversion strategy.

RSI Reversion should not watch every stock that becomes oversold. It should
watch liquid, financially survivable names where a short-term sell-off is more
likely to be temporary than terminal.

This document defines the candidate universe. The strategy still controls entry
and exit timing.

## Responsibility Split

```text
Universe -> Watchlist Source -> RSI Watchlist -> raw RSI Signal -> RSI Edge Filter -> Risk -> Execution
```

- Watchlist source: selects symbols eligible for RSI mean reversion.
- RSI strategy: detects oversold/overbought threshold crossings.
- Edge filter: confirms or rejects raw RSI entries under current conditions.
- Risk manager: sizes positions and enforces stops, exposure, and kill switches.

The watchlist selector answers:

> Which stocks are safe and useful enough to watch for mean-reversion setups?

The RSI strategy answers:

> Is there an actual oversold entry signal now?

The RSI edge filter answers:

> Is this raw RSI signal allowed to trade right now?

Do not embed universe-selection logic inside `RSIReversion`.
Do not embed MACD, EMA5/EMA10, or other entry-confirmation rules in scanner
scripts. Those belong in an RSI edge filter when used as confirmation/vetoes,
or in a clearly named RSI strategy variant if they redefine the signal timing.

## Core Principle

RSI Reversion is a liquidity-provision / contrarian strategy.

It should buy temporary weakness in strong or stable names, not catch collapsing
businesses.

In plain English:

> Buy the dip only when the company, liquidity, and market regime make a bounce
> plausible.

## Current Strategy

Implemented strategy:

- file: `strategies/rsi_reversion.py`
- default RSI period: 14
- default entry: RSI crosses below 30
- default exit: RSI crosses above 70
- order type: LIMIT
- status: implemented, not yet active

The watchlist rules below are independent of those signal parameters.

## RSI Candidate Rule

A symbol is eligible for the RSI reversion watchlist only if all hard filters
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
- warrants, rights, units, and thin structured products
- symbols with unreliable or incomplete daily data

### 2. Size And Liquidity

Required:

- market capitalization >= 2,000,000,000
- latest close >= 10.00
- 20-day average share volume >= 1,000,000
- 50-day average dollar volume >= 100,000,000

Rationale:

Mean-reversion entries often happen during fast sell-offs. The strategy must be
able to place limit orders in names with enough depth to avoid poor fills and
wide spreads.

### 3. Financial Survival

Required:

- profitable, or cash runway >= 12 months

Preferred but not mandatory:

- annual free cash flow > 0
- YoY revenue growth > 0

Excluded:

- known solvency crisis
- unresolved bank-debt or refinancing crisis
- pre-profit story stock with inadequate runway

Rationale:

RSI Reversion can tolerate weaker fundamentals than SMA because the setup is a
temporary oversold bounce. It still cannot buy names where the sell-off may be
the market pricing bankruptcy or permanent impairment.

### 4. Market Regime Gate

Required before allowing new RSI entries:

- SPY close > SPY SMA200
- SPY close > SPY SMA50

Rationale:

Long-only mean reversion degrades in broad market downtrends. In those regimes,
oversold can become more oversold.

This is an edge-filter rule, not a watchlist-membership rule. The scanner should
report it, but the engine should gate entries at runtime.

### 5. Symbol Structure

Required:

- close > SMA200
- close >= 0.60 * 52-week high
- close >= 1.20 * 52-week low

Preferred:

- close > SMA50

Rationale:

RSI should buy pullbacks in structurally intact names. A stock below its 200-day
average or far from its highs may be in a breakdown, not a dip.

Unlike SMA, RSI does not require `SMA50 > SMA150 > SMA200`. That stack is too
trend-following-heavy and would remove many useful pullback candidates.

### 6. Mean-Reversion Character

Required:

- at least 3 RSI(14) oversold events in the last 252 trading days
- at least 50% of those events reverted to RSI(14) >= 50 within 10 trading days
- no more than 2 ATR-stop-style failures in the last 252 trading days

Rationale:

Not every stock mean-reverts well. Some trend smoothly and rarely become
oversold; others fall and keep falling. The watchlist should prefer names that
have historically snapped back after oversold readings.

### 7. Volatility Window

Required:

- ATR14 / close >= 0.015
- ATR14 / close <= 0.07
- Bollinger Band width 20,2 >= 0.04

Rationale:

RSI needs enough movement to create profitable oversold setups, but not so much
chaos that stops dominate. Bollinger Band width prevents selecting dead names
with no meaningful reversion opportunity.

### 8. Avoid News Shock And Crash Risk

Reject if any of these are true:

- one-day drop <= -12%
- 5-day return <= -20%
- earnings occurred today or will occur next trading day
- obvious split/merger/corporate-action distortion in the recent bars

Rationale:

Short-term reversal can be overwhelmed by genuine new information. Large
news-driven sell-offs are not the same as ordinary liquidity-driven pullbacks.

If earnings-calendar data is unavailable, this rule should be logged as
`not_checked`, not silently ignored.

### 9. Sector And Correlation Hygiene

Required:

- no more than 3 symbols per sector in the final RSI watchlist
- avoid selecting multiple highly correlated versions of the same trade
- avoid overlap with SMA unless strategy ownership and capital sleeves are
  explicit

Rationale:

Mean-reversion trades cluster during sell-offs. Sector concentration can cause
many "independent" RSI entries to become one large correlated bet.

## Ranking Survivors

If more symbols pass than the target list size allows, rank survivors.

Recommended ranking inputs:

1. historical oversold reversion hit rate
2. average 10-day return after RSI oversold events
3. liquidity: 50-day dollar volume
4. volatility quality: ATR% in the middle of the allowed range
5. structural strength: close above SMA200 and distance from 52-week low
6. solvency strength: profitable, positive FCF, positive revenue growth
7. sector diversification

Do not rank by deepest current RSI alone. Deeply oversold can mean "cheap", but
it can also mean "broken".

## Post-Analysis Promotion Layer

The scanner creates candidates. It does not automatically promote every
candidate into the active RSI watchlist.

Before a symbol enters the static RSI pool, run post-analysis over the scanner
survivors. Current implementation:

- scanner: `scripts/rsi_watchlist_scan.py`
- validator: `scripts/rsi_candidate_validate.py`
- post-analysis ranker: `scripts/rsi_candidate_post_analysis.py`
- report: `logs/rsi_candidate_post_analysis_latest.md`

Initial promoted RSI pool:

- ALLY
- CDNS
- CCK
- SN
- TFC

Current post-analysis guardrails:

- minimum oversold events: 5
- minimum exact-strategy return: 20%
- minimum profit factor: 1.20
- maximum exact-strategy drawdown: -65%
- minimum event hit rate: 35%
- maximum ATR-stop-failure rate: 35%

Rationale:

A stock can pass the scanner because it often bounces after oversold readings,
but still be a poor fit for the exact bot strategy if the exit rule produces
large drawdowns or poor realized trade outcomes. Post-analysis is the promotion
step that catches that difference.

## Stop-Loss And Re-Entry Guardrail

ATR stop-loss orders protect an individual RSI trade. They do not, by
themselves, protect the strategy from repeatedly buying the same symbol while it
keeps falling.

Before RSI is activated in paper mode, add or verify a symbol-level re-entry
guardrail after stop-outs:

- if a symbol stops out, block new RSI entries in that symbol for a cooldown
  window
- require a fresh setup after cooldown rather than immediately rebuying the next
  oversold print
- track repeated stop-outs by symbol, not only by strategy
- demote or disable a symbol after repeated RSI stop-outs during the paper
  window

Recommended first-version rule:

- after one RSI stop-out: symbol cooldown for at least 10 trading days
- after two RSI stop-outs in a rolling 60 trading-day window: disable the symbol
  for RSI until the next watchlist refresh

Rationale:

Mean reversion can be correct often and still fail badly when a stock enters a
real breakdown. The bot must avoid repeated stop-out and rebuy behavior.

## What Stays Out Of The Core Rule

These should not be hard first-version admission rules:

- analyst ratings
- social media sentiment
- intraday order-flow imbalance
- options implied volatility
- news NLP
- averaging down / martingale scaling

Reason:

They add data dependencies and operational complexity before the basic RSI
watchlist has been proven in paper mode.

## Static To Dynamic Migration

Recommended rollout:

1. Write this rulebook.
2. Build `scripts/rsi_watchlist_scan.py` as report-only.
3. Run the scanner against delayed SIP data with fundamentals enabled.
4. Compare current `settings.RSI_WATCHLIST` against scanner output.
5. Promote a static RSI list before the Phase 10 combined SMA + RSI paper run.
6. Keep dynamic RSI scanning in report-only mode during that first paper window.
7. Only later allow a dynamic watchlist source to drive RSI directly.

The scanner must log:

- rule version
- data feed
- data timestamp
- base universe size
- symbols removed by each hard filter
- final candidate count
- selected symbols
- market-regime state
- warnings for unavailable earnings or sector data

## Dynamic Watchlist Guardrails

Dynamic RSI watchlists must follow the same guardrails as other dynamic lists:

- do not remove a symbol with an open position unless exit behavior is explicit
- do not change the active watchlist mid-paper-run used for reconciliation
- cache every generated list with timestamp and rule version
- log rejection counts for every hard filter
- run new selectors in report-only mode first
- preserve strategy ownership across restarts

Scanner membership is not position ownership.

## Rule Version

Current rule version: `rsi_watchlist_v1`

Changing any hard threshold creates a new rule version.

Examples:

- changing minimum market cap from 10B to 5B
- removing the `close > SMA200` requirement
- changing ATR% bounds
- changing the required historical oversold reversion hit rate
- adding an earnings-calendar hard block

## Sources

- StockCharts ChartSchool, "RSI(2)": summarizes Larry Connors' RSI(2)
  approach, including the 200-day SMA trend filter and deeply oversold RSI
  entries. This supports using long-term trend context around RSI pullbacks.
- Larry Connors and Cesar Alvarez, *Short Term Trading Strategies That Work*
  (2008): practitioner source for RSI(2), buying pullbacks rather than
  breakouts, and using trend filters. Use as strategy inspiration, not as a
  complete production rulebook.
- Avellaneda and Lee, "Statistical Arbitrage in the U.S. Equities Market"
  (Quantitative Finance, 2010): mean-reversion/contrarian equity strategies are
  modeled on residual/idiosyncratic returns; volume information improved
  ETF-based signals in their tests. This supports liquidity and sector-aware
  screening.
- Liew and Roberts, "U.S. Equity Mean-Reversion Examined" (Risks, 2013):
  describes mean reversion as liquidity provision after prices move away from
  equilibrium, supporting the idea that RSI reversion should avoid structural
  breakdowns and focus on tradable/liquid names.
- Blitz, Huij, and Martens, "Residual Momentum" / residual reversal literature,
  and "Short-term residual reversal" (Journal of Financial Markets, 2013):
  short-term reversal effects can persist even in large-cap stocks after costs,
  supporting a large-cap, liquid-universe approach.
- StockCharts and Bollinger Band educational material: lower-band/RSI
  combinations are commonly used to identify stretched short-term moves, but
  trend context matters because price can "walk the band" in strong trends.
- Existing project docs: `docs/RSI-edge-filter.md` requires market and symbol
  trend gates, and `scripts/watchlist_review.py` already treats RSI FCF/revenue
  as informational while enforcing solvency.
