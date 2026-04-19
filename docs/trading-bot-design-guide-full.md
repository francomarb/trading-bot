# Trading Bot Design Guide (Full Version)

## Purpose

This document captures the full set of recommendations for building an automated trading bot around simple, robust, reasonably profitable strategies that can operate with minimal human intervention.

The core objective is not to build a magic machine that prints money.  
The real objective is to build a system that:

- removes emotional decision-making
- applies consistent execution
- survives changing market conditions
- maintains controlled risk
- achieves positive expectancy over time

---

## The Honest Take

Your idea is directionally correct:

> Equip a bot with a few simple but profitable strategies that can run without emotion and do reasonably well without human intervention.

That is exactly the right instinct.

But there is an important reality:

> “Simple + profitable + fully autonomous” is one of the hardest combinations in trading.

Why?

Because simple strategies can work, but they only work reliably when wrapped in:

- strong risk management
- realistic execution assumptions
- regime awareness
- diversification across strategy types
- monitoring for edge decay

The thing that usually kills trading systems is not emotion alone.  
It is also:

- bad position sizing
- lack of stop discipline
- overfitting
- assuming one strategy works in all environments
- ignoring how market regimes change

So yes, removing emotion is a real edge.  
But automation alone does not create profitability.  
The structure around the strategy is what gives it a chance.

---

## Core Philosophy

Do **not** think in terms of:

> One bot with a few entry signals

Think in terms of:

> A portfolio of small, simple, dumb strategies plus a strict risk layer

That mindset is much stronger.

Why this works better:

- simple systems are easier to test and debug
- small strategies are less likely to be overfit
- different strategies can perform in different regimes
- risk can be centralized and controlled
- failure in one strategy does not destroy the account

A surprisingly large number of retail traders obsess over finding the “perfect signal.”

In practice, a more successful design is:

1. detect market conditions
2. choose an appropriate strategy for those conditions
3. size positions conservatively
4. exit cleanly
5. monitor strategy health
6. disable what stops working

---

## What Actually Works in Practice

Instead of trying to make one strategy handle all conditions, use regime-specific logic.

Markets behave differently in different states:

| Market Type | Characteristics | What Usually Works Better |
|---|---|---|
| Trending | Sustained directional move | Trend following |
| Sideways / choppy | Oscillating around a range | Mean reversion |
| Volatile | Large directional bursts or disorder | Breakouts / volatility systems |
| Low volatility | Slower stable conditions | Carry / income / selective trend |

The key insight:

> A strategy can be good and still lose money for long periods if it is applied in the wrong market regime.

That is why regime detection is critical.

---

## Regime-Based Design

A bot should decide what kind of market it is in before deciding what to do.

This can be kept simple.

Possible regime inputs:

- realized volatility
- ATR expansion or contraction
- trend strength
- moving-average slope
- ADX
- distance from long-term moving averages
- broad market filter such as SPY above or below 200-day MA

Example logic:

```python
if volatility > threshold:
    use_breakout_strategy()
elif trend_strength > threshold:
    use_trend_strategy()
else:
    use_mean_reversion()
```

This does not need to be perfect.  
It just needs to be good enough to stop the bot from using the wrong tool in the wrong environment.

---

## Keep the Strategies Extremely Simple

One of the strongest recommendations is this:

> Keep your strategies boring.

That usually beats fancy systems.

Complicated strategies often look better in backtests and worse in real trading.  
Simple strategies often look unimpressive in backtests and survive better in live trading.

### Strategy 1: Trend Following

This works surprisingly well over time because markets do trend, especially high-momentum sectors.

Example concept:

- buy when price is above the 50-day moving average
- sell when price falls below the 50-day moving average

Possible refinement:

- require 50-day MA above 200-day MA
- require volume confirmation
- require market index confirmation

Why it works:
- it catches sustained moves
- it avoids fighting strong trends
- it works especially well in growth-led bull markets

Weakness:
- it suffers in choppy markets
- it can get whipsawed repeatedly

---

### Strategy 2: Mean Reversion

This works better when markets are range-bound and oversold conditions bounce.

Example concept:

- buy when RSI < 30
- sell when RSI > 60

Possible refinement:

- only use in sideways markets
- avoid if broad market is breaking down
- use short holding periods
- exit faster than trend strategies

Why it works:
- many assets revert after short-term panic or exhaustion
- it can produce frequent small wins

Weakness:
- it gets run over in strong downtrends
- buying “cheap” can become buying into collapse

---

### Strategy 3: Breakout Strategy

This is useful when volatility compresses and price explodes into a move.

Example concept:

- buy when price breaks a 20-day high
- sell using trailing stop

Possible refinement:

- require volume expansion
- require volatility contraction before breakout
- avoid extended names that are already overbought without structure

Why it works:
- captures fast directional moves
- useful around news or post-consolidation expansions

Weakness:
- false breakouts are common
- bad entries can lead to immediate reversals

---

## The Most Important Insight: Risk Management Matters More Than Entry

This is probably the single most important recommendation in the whole document:

> Your edge usually comes more from risk control and exits than from the entry signal itself.

Many traders spend 90% of their effort on entry rules.  
That is backwards.

A mediocre entry with good sizing and good exits can outperform a great entry with poor risk discipline.

---

## Risk Management Is 80% of the System

If you want the bot to survive without emotional intervention, risk management has to be strict and automatic.

Non-negotiable components:

- max risk per trade
- hard stop-loss
- portfolio exposure cap
- kill switch
- correlated exposure limit

### Baseline Rules

A strong starting framework:

- risk only 1–2% of equity per trade
- always define the stop before entering
- cap gross exposure to 30–50% while testing
- set a daily max loss threshold
- prevent stacking too many correlated positions

### Example Position Sizing

```python
risk_per_trade = 0.01  # 1%
stop_loss = entry_price * 0.95
position_size = account_size * risk_per_trade / (entry_price - stop_loss)
```

This matters because it converts strategy logic into survivable trade sizing.

### Risk Controls Worth Implementing

- hard stop-loss on every trade
- max daily drawdown
- max weekly drawdown
- cooldown period after a loss streak
- capital cap per strategy
- capital cap per sector
- cap on simultaneous open positions
- reduced sizing in high volatility environments

### Practical Kill Switches

Examples:

- stop opening new trades after -3% in a day
- disable strategy after 5 consecutive losses
- stop trading after slippage exceeds threshold
- stop trading if market data feed fails
- stop trading if broker API returns repeated errors

These are boring, but they are exactly the kind of controls that keep an automated bot from doing something catastrophic.

---

## Expect Strategies to Stop Working

This is another reality most people ignore:

> Even good strategies go through drawdowns, decay, and long periods of underperformance.

That does not necessarily mean the strategy is broken.  
But it does mean the bot needs a way to manage underperformance.

### Strategy Lifecycle Reality

A strategy usually goes through:

1. a strong performance phase
2. a drawdown phase
3. either recovery or long-term decay

### What the Bot Should Do

- track performance per strategy
- measure rolling expectancy
- measure rolling Sharpe or simplified efficiency metrics
- reduce capital to weak strategies
- temporarily disable clearly degrading strategies

This matters because no strategy should get permanent blind trust.

---

## Backtesting Is Necessary, But Dangerous

You absolutely should backtest.  
But you should distrust beautiful backtests.

The biggest dangers:

- curve fitting
- lookahead bias
- survivorship bias
- unrealistic fills
- ignoring transaction costs
- ignoring slippage

### Rule of Thumb

> If the backtest looks too perfect, it is probably overfit.

That is a very useful mental model.

A realistic backtest should include:

- commissions
- spread assumptions
- slippage
- realistic order fill behavior
- delayed signals if relevant
- corporate actions if trading equities

### Better Testing Sequence

A stronger process:

1. backtest
2. walk-forward test
3. out-of-sample test
4. paper trade
5. very small live deployment
6. gradual scale-up

This is much better than going from backtest straight to real money.

---

## Execution Matters More Than Most People Think

Even with a good strategy, execution can erode the edge.

Key execution problems:

- slippage
- latency
- partial fills
- spread widening
- API errors
- stale data

For swing trading, latency is less important than for HFT.  
But slippage and order handling still matter.

### Order Type Considerations

**Market orders**
- fill fast
- worse pricing
- more slippage in volatile names

**Limit orders**
- better pricing
- may not fill
- can miss moves

You need to match order type to strategy.

Examples:

- breakouts may tolerate marketable orders
- mean reversion may benefit more from patient limit entries
- exits on hard risk events may need immediate execution

---

## My Strong Recommendation for You: Build a Hybrid Swing Bot

If I were building this for someone with a growth-stock or tech-heavy orientation, I would avoid ultra-fast systems.

I would build a **hybrid swing bot**.

### Why a Hybrid Swing Bot

- less competition than HFT
- easier infrastructure
- easier to debug
- lower sensitivity to milliseconds
- works better with strong thematic leaders like semiconductors, large-cap tech, or sector ETFs
- easier to align with macro and market regime filters

### Good Universe Candidates

Examples of the type of universe I would consider:

- large-cap tech leaders
- semiconductor names
- liquid growth stocks
- liquid sector ETFs
- broad index ETFs

The actual symbols matter less than these properties:

- liquid
- tradeable spreads
- strong institutional participation
- enough volatility to create opportunity
- not so illiquid that execution becomes a nightmare

---

## The Three Strategy Types I’d Start With

If you want “a few simple strategies,” I would not start with ten.  
I would start with two or three, max.

### 1. Trend Rider

Purpose:
- ride sustained moves in strong names

Typical logic:
- price above medium-term moving average
- broad market in risk-on mode
- breakout or pullback entry in direction of trend

Best environment:
- bullish markets
- sector leadership periods
- momentum-led expansions

Why it fits well:
- strong growth names often trend for long periods
- it works with institutional money flows instead of fighting them

---

### 2. Dip Buyer

Purpose:
- buy controlled pullbacks in established uptrends

Typical logic:
- asset above long-term moving average
- short-term oversold signal
- buy after pullback into support or lower band
- exit on bounce or trend resumption

Best environment:
- healthy uptrends
- orderly corrections
- names with strong sponsorship

Risk:
- dip buying in a broken trend is dangerous

This is why you need regime filters and broad-market filters.

---

### 3. Volatility Breakout

Purpose:
- capture sharp directional expansions

Typical logic:
- volatility contraction
- range compression
- breakout above recent range high
- trailing stop for exit

Best environment:
- post-consolidation expansions
- news-driven moves
- strong momentum shifts

Risk:
- fake breakouts can be frequent

This strategy benefits heavily from volume filters and broad-market confirmation.

---

## Add a Simple “Edge Layer”

This is a very strong improvement and still keeps the system simple.

Examples of edge-enhancing filters:

- only trade long if SPY is above 200-day moving average
- only allow trend trades if market breadth is healthy
- skip trades near earnings unless running a specific earnings-breakout strategy
- reduce size when VIX or realized volatility is elevated
- avoid low-liquidity names
- avoid trading during major macro event windows if the strategy is not designed for them

The point is not to over-filter everything.  
The point is to keep the bot out of obviously bad situations.

---

## Brutal Truth About Fully Autonomous Bots

Here is the blunt version:

> Even a well-built bot will not produce smooth, constant profits.

It will:

- have losing streaks
- have drawdowns
- underperform at times
- need maintenance
- need monitoring
- occasionally fail in weird ways

The goal should **not** be:

> “Always profitable”

The real goal should be:

> “Positive expectancy over time with controlled drawdowns and repeatable discipline”

That is realistic.

---

## What I Would Prioritize in Development

This is the part I feel strongest about:

### Step 1: Start With One Strategy Only

Do not start with a whole strategy zoo.

Build one strategy.  
Make it clean.  
Make it testable.  
Make it observable.

Why:
- easier debugging
- easier attribution of performance
- easier to find execution problems
- easier to confirm whether the logic actually works

### Step 2: Paper Trade It

Run it in paper trading long enough to observe:

- trade frequency
- signal quality
- drawdown behavior
- API issues
- logging issues
- slippage assumptions vs reality

### Step 3: Add Live Capital Slowly

Start with tiny capital.

The purpose of early live trading is not profit maximization.  
It is reality testing.

### Step 4: Add the Second Strategy Only After You Validate the First

This is important because many people keep adding complexity before proving anything.

Complexity should only be added after a baseline edge is demonstrated.

---

## A Better Mental Model for the Whole Bot

Think of the system as six layers:

### 1. Data Layer
Responsible for:
- market data
- indicator calculations
- signal inputs
- data integrity checks

### 2. Regime Detector
Responsible for:
- identifying trend / chop / volatility state
- gating which strategies are allowed to run

### 3. Strategy Engine
Responsible for:
- generating entries
- generating exits
- producing confidence or ranking if needed

### 4. Risk Engine
Responsible for:
- sizing
- stop placement
- portfolio exposure caps
- correlation limits
- kill switches

### 5. Execution Engine
Responsible for:
- sending orders
- tracking status
- reconciling fills
- handling retries and failures

### 6. Monitoring and Analytics
Responsible for:
- logs
- alerts
- performance per strategy
- error reporting
- health checks

This modular approach is much better than writing one giant script.

---

## Principles for Clean Architecture

If you are writing this with Claude Code, I would strongly encourage the following architecture principles:

- separate strategy logic from execution logic
- separate signal generation from position sizing
- centralize risk checks
- log every decision path
- make each strategy independently switchable
- keep configuration externalized
- make the system testable in simulation and paper mode

This makes iteration much easier.

---

## Monitoring Requirements

Even if the system is automated, I would not call it truly “set and forget.”

At minimum, you want visibility into:

- daily PnL
- open positions
- recent fills
- strategy-by-strategy performance
- API failures
- stop-loss events
- slippage statistics
- strategy disable events

### Alerts Worth Adding

- max daily loss hit
- broker rejected order
- stale data feed
- missing market data
- repeated exception in one module
- strategy auto-disabled
- unusual slippage
- position stuck or unmatched vs broker state

Automation is strongest when it is observable.

---

## Common Failure Modes

These are the most common things that break retail bots:

### 1. Overfitting
The system looks amazing in backtests and falls apart live.

### 2. Overtrading
Too many trades, too much noise, too many fees.

### 3. Ignoring Correlation
Three different trades that are really the same trade.

### 4. Weak Exit Logic
Entries are okay, exits are inconsistent.

### 5. No Kill Switch
The bot keeps trading through a broken environment or software failure.

### 6. Hidden Execution Assumptions
Strategy assumes fills that are not achievable.

### 7. No Regime Awareness
Trend strategy deployed in chop, mean reversion deployed in breakdown.

### 8. Complexity Creep
Too many indicators, filters, and exceptions without real benefit.

---

## What “Reasonably Well” Should Mean

If the goal is for the bot to “do reasonably well,” define that clearly.

A realistic definition might be:

- positive expectancy over a long horizon
- controlled drawdowns
- no catastrophic account damage
- repeatable behavior
- measurable and attributable performance

That is much better than expecting constant monthly gains.

### Healthy Expectations

A good system can still have:

- 40–60% win rates
- multiple consecutive losses
- ugly months
- strategy-specific drawdowns
- long flat periods

That does not make it bad.  
It makes it real.

---

## What I Would Avoid Early

I would avoid these in the first version:

- machine learning
- reinforcement learning
- very short-term intraday scalping
- options complexity unless that is the whole point of the bot
- dozens of indicators
- dynamic regime models that are too hard to interpret
- trading illiquid names

The first version should prove discipline and structure, not sophistication.

---

## Suggested Development Path

Here is the development path I would recommend.

### Phase 1: Baseline
- build one strategy
- make signals deterministic
- add logging
- add stop-loss
- add paper mode

### Phase 2: Validation
- backtest honestly
- paper trade for multiple weeks
- compare expected fills to actual paper fills
- review trade logs manually

### Phase 3: Controlled Live Test
- deploy with small capital
- watch execution quality
- measure real slippage
- refine kill switches

### Phase 4: Add Strategy Diversification
- add second strategy
- track metrics independently
- split capital conservatively

### Phase 5: Portfolio Management Layer
- capital allocation by strategy
- exposure caps
- correlation controls
- strategy throttling based on performance

This is much safer than jumping straight to a large multi-strategy engine.

---

## Key Design Principles Summary

These are the condensed principles I would want the bot to embody:

- simplicity beats complexity
- strategy selection should depend on market regime
- risk management matters more than entry precision
- multiple small edges are better than one fragile edge
- automation removes emotion but not risk
- execution assumptions must be realistic
- every strategy should be observable and measurable
- losing streaks are normal
- drawdown control is a feature, not an afterthought
- the bot should fail safely

---

## Final Recommendation

If you want this to actually have a chance:

1. start with **one** simple strategy
2. make risk controls non-negotiable
3. paper trade before real money
4. add only 2–3 strategies max at first
5. use regime filters
6. monitor strategy health continuously
7. optimize for robustness, not beauty

The biggest edge your bot can have is not that it is “smart.”

It is that it is:

- disciplined
- consistent
- risk-aware
- modular
- boring enough to survive

---

## Final Principle

The goal is not:

> Always winning trades

The goal is:

> Positive expectancy over time with controlled drawdowns

That is the right target for an automated trading system.

