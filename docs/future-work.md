# Future Work: Sentiment Module + Options-Capable Architecture

This note captures a forward-looking architecture for evolving the trading bot from the current equity-style, pluggable strategy framework into a system that can support:

- current strategies: SMA and RSI
- future custom alpha-generating strategies
- a sentiment/meta-signal layer
- SPY options as a trade expression layer
- more advanced portfolio/risk allocation

The goal is to preserve stability now while preparing the right extension points for the next stage.

---

## 1) Current state

The bot already has strong architectural foundations:

- pluggable strategies
- a shared engine loop
- a go/no-go / risk gate
- broker/execution separation
- reporting/logging

At the moment, SMA and RSI are the only strategies. That is fine. The next evolution should **not** replace them. Instead, the architecture should be extended so new modules can sit **around** or **below** existing strategies.

Important principle:

**Strategies should continue to answer _why a trade exists_, not _how it is expressed_.**

That means SMA, RSI, and future custom strategies should remain focused on finding directional opportunities, while instrument selection and sentiment gating happen elsewhere.

---

## 2) Core design principle going forward

Do **not** make options or sentiment into one giant strategy class.

Avoid designs like:

- `SPYOptionsSentimentStrategy`
- `TrumpPostStrategy`
- `NewsAndOptionsStrategy`

These would mix together:

- signal generation
- regime/sentiment analysis
- risk logic
- contract selection
- order execution

That would reduce flexibility and make the system harder to test and maintain.

Instead, the future system should follow this model:

**Strategies discover opportunities -> Sentiment/regime decides whether they are allowed -> Risk decides whether they are safe -> Instrument selection decides whether the trade is expressed as shares or options -> Execution handles order details**

---

## 3) Where the sentiment module fits

The sentiment module should **not** replace SMA or RSI.

It should be a **meta-layer** that sits between strategy signals and final order construction.

### Role of the sentiment module

The sentiment module should answer questions like:

- Is the environment bullish, bearish, neutral, or unstable?
- Are bullish trades currently allowed?
- Are bearish trades currently allowed?
- Is options exposure allowed right now?
- Should risk be reduced because signals are mixed or noisy?
- Is the market reacting positively or negatively to news?

### What it should *not* do

The sentiment module should not directly:

- place trades
- choose options contracts
- bypass risk controls
- become the only source of entries

### Best architectural interpretation

Sentiment should act as one of:

1. **Edge / regime filter**
   - easiest near-term integration
   - can gate existing SMA/RSI entries

2. **Portfolio-level meta-signal**
   - better long-term design
   - can influence sizing, permissions, and instrument choice across all strategies

---

## 4) Recommended architectural additions

The following are the main pieces needed to advance toward a sentiment-aware, options-capable system.

### A. Trade Intent layer

Current boolean entry/exit signals are enough for simple equity strategies, but future evolution will be easier if signals become richer.

Introduce a `TradeIntent` concept.

Example fields:

- strategy_name
- symbol
- direction (bullish / bearish)
- confidence
- timeframe / horizon
- timestamp
- optional metadata (trend strength, RSI value, etc.)

Purpose:

This becomes the clean handoff between:
- the reason a trade exists
- the way the trade will be expressed

This is likely one of the safest first additions because it can be introduced without changing final execution immediately.

---

### B. MetaSignal / Sentiment Engine

Introduce a module that produces a normalized market/regime view.

Possible output:

- sentiment_score: -1.0 to +1.0
- market_regime: bullish / bearish / neutral / mixed
- trade_permission_long: bool
- trade_permission_short: bool
- options_allowed: bool
- sentiment_confidence
- notes / tags

Possible future inputs:

- news sentiment
- political posting sentiment (including Truth Social)
- macro headline classification
- social sentiment
- volatility regime
- breadth confirmation
- divergence between sentiment and price

This engine should remain independent from strategy code.

---

### C. Position Expression layer

This is one of the most important missing pieces.

Given:

- a `TradeIntent`
- current meta-signal / regime state
- current market context

the system should decide how the idea is expressed:

- no trade
- equity position
- long call
- long put
- bull call spread
- bear put spread

This layer is what translates directional opportunity into the actual instrument.

That means:

- strategies stay generic
- options logic stays isolated
- future custom strategies can reuse the same expression layer

---

### D. Option Contract Selector

Once the expression layer decides that a trade should be expressed via options, a dedicated module should choose:

- expiration
- strike(s)
- spread width
- delta or moneyness target
- quantity
- liquidity thresholds
- acceptable bid/ask spread
- open interest / volume constraints

This should be separate from both:
- strategy logic
- broker API code

---

### E. Option-aware Risk layer

The current risk/go-no-go engine likely assumes equity-style logic such as:
- stop-loss distance
- ATR-based sizing
- direct share exposure

For options, risk should also understand:

- premium at risk
- max loss for long options
- max loss for spreads
- time-to-expiry constraints
- IV regime
- total premium budget
- portfolio concentration
- event risk

This suggests risk should evolve from approving only one simple position model into approving a more general `PositionSpec`.

---

## 5) Suggested future object model

The following concepts would make the architecture clearer.

### `TradeIntent`
Represents a strategy-generated opportunity.

Example:
- strategy_name: SMA
- symbol: SPY
- direction: bullish
- confidence: 0.72
- horizon: short_swing

### `MetaSignalState`
Represents sentiment/regime context.

Example:
- sentiment_score: 0.61
- regime: bullish
- long_allowed: true
- short_allowed: false
- options_allowed: true

### `PositionSpec`
Represents the trade expression selected by the system.

Variants:
- `EquityPositionSpec`
- `OptionSingleSpec`
- `OptionSpreadSpec`

### `ExecutionPlan`
Represents broker-ready instructions after risk approval.

This separation keeps the system testable and avoids mixing concerns.

---

## 6) Proposed folder / module direction

These are suggested additions, not mandatory immediate changes.

```text
signals/
  intent.py
  meta.py

sentiment/
  engine.py
  scoring.py
  sources.py

portfolio/
  allocator.py
  expression.py

instruments/
  options.py
  selector.py

execution/
  equity_executor.py
  options_executor.py
```

Notes:
- `signals/` stores normalized internal models
- `sentiment/` builds the regime and permission state
- `portfolio/expression.py` chooses equity vs options expression
- `instruments/selector.py` chooses contracts
- `execution/` sends approved orders

---

## 7) What can be implemented now with low stability risk

These items are the best candidates to start early without disrupting the current SMA/RSI paper-trading stability.

### Safe early additions

#### 1. Add `TradeIntent`
Low risk, high future value.

This can initially be a lightweight dataclass created from existing SMA/RSI signals without changing current order placement immediately.

Benefit:
- future-proofs the strategy interface
- makes downstream testing easier
- allows later addition of sentiment and options without redoing everything

#### 2. Add a `MetaSignalState` model
Even before building a real sentiment engine, define the internal object now.

Initially it can be static or stubbed:
- long_allowed = true
- short_allowed = true
- options_allowed = false

Benefit:
- creates a clean extension point
- almost zero stability risk

#### 3. Add a Position Expression interface
Even if it only returns equity orders today, create the abstraction now.

Example conceptual API:
- input: `TradeIntent`
- output: `PositionSpec`

Initially:
- bullish intent -> equity long position
- bearish intent -> no trade or future placeholder

Benefit:
- options can later plug in without strategy rewrites

#### 4. Generalize risk approval payloads
Instead of thinking only in terms of share orders, begin evolving risk output so it can approve a generalized position specification.

This can be done carefully and incrementally.

#### 5. Add a volatility / regime placeholder
Even before full sentiment, the system can gain value from a minimal market-state layer:
- VIX regime
- trading session status
- event-day flag
- simple breadth proxy

This is simpler than full social/news sentiment and useful sooner.

---

## 8) What should wait until current stability is better

These items are better postponed until:
- SMA is stable
- RSI is implemented and tested
- capital allocation is fixed
- paper trading across multiple strategies is working well

### Defer for later

#### 1. Real sentiment/news ingestion
Examples:
- Truth Social / political post classification
- news feed NLP
- social sentiment aggregation

These are useful, but high-noise and higher maintenance.

#### 2. Full options execution
Do not rush directly into live options execution.

Before that, the bot should support:
- position expression abstraction
- option-aware risk definitions
- contract selection rules
- paper-mode validation

#### 3. Dynamic portfolio allocator
This is valuable, especially to avoid strategy capital starvation, but it should come after the basic generalized `PositionSpec` model is in place.

---

## 9) Phase-based roadmap

A practical roadmap that minimizes disruption:

### Phase 1 — stabilize current system
Focus on:
- finish SMA validation
- implement RSI
- fix capital allocation starvation
- run both in paper mode in parallel

This is the correct near-term priority.

### Phase 2 — prepare architecture
Add:
- `TradeIntent`
- `MetaSignalState`
- `PositionSpec`
- position-expression interface

These can exist while still routing everything to equity-style execution.

### Phase 3 — introduce simple regime filters
Before full sentiment, add:
- volatility state
- macro event blocks
- trend regime
- optional edge filter hooks

This will already improve trade quality.

### Phase 4 — build sentiment module
Add:
- scoring model
- external data sources
- permission rules
- long/short gating
- confidence thresholds

Use it first as a filter, not as a direct trigger.

### Phase 5 — add options paper execution
Only after previous phases:
- choose SPY as initial underlying
- define allowed structures
- start with simple, liquid expressions
- test in paper mode only

### Phase 6 — advanced custom strategies
At this stage, new strategies can be designed more freely because the bot will already support:
- richer intent generation
- regime-aware filtering
- multiple expressions of the same idea
- better portfolio control

---

## 10) Specific view on SPY options

If SPY options are eventually used, they should be treated as a **trade expression**, not a standalone strategy.

Example flow:

1. SMA or RSI generates a bullish `TradeIntent`
2. Meta-signal engine confirms bullish environment
3. Risk engine approves exposure
4. Position expression layer chooses:
   - equity long
   - SPY call
   - SPY call spread
5. Options selector chooses contract details
6. Execution submits the trade

This keeps the architecture clean and reusable.

---

## 11) Specific view on custom strategies

Future custom strategies are a good direction and likely where true edge will come from.

Examples of future strategy families:
- event-driven strategies
- volatility regime strategies
- divergence strategies
- breadth-based strategies
- sentiment-aware but still technically confirmed strategies
- hybrid strategies built around your own observations

The architecture should make it easy for a future custom strategy to plug in without needing to know:
- broker details
- option contract logic
- sentiment ingestion details

That is why the separation proposed above matters.

---

## 12) Short summary of what to do now

### Near-term priorities
1. finish SMA validation
2. implement RSI
3. fix capital allocation so one strategy cannot starve the others
4. run multiple strategies in paper mode safely

### Best low-risk future prep
1. add `TradeIntent`
2. add `MetaSignalState`
3. add `PositionSpec`
4. add a position-expression interface
5. start abstracting risk to approve generalized positions

### Later / higher-risk work
1. sentiment/news ingestion
2. options contract selection
3. options paper execution
4. advanced portfolio allocator
5. full social/political signal integration

---

## 13) Final architectural takeaway

The future bot should not become “an options bot” or “a sentiment bot.”

It should become a **strategy platform** with the following layers:

- strategy discovery
- meta/regime filtering
- risk approval
- instrument expression
- execution

That way:
- SMA and RSI stay useful
- custom strategies can be added safely
- sentiment becomes a reusable cross-cutting module
- options become just one way to express an approved thesis

That is the most stable path from the current system to a much more advanced one.
