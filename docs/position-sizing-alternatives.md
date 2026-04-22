# Position Sizing Alternatives

## Purpose

Document the main position sizing approaches used by trading bots, with a focus on:

- small accounts
- multi-strategy systems
- high-priced stocks
- fractional share support
- clean integration with capital allocation

This document exists to help evaluate alternatives before finalizing the bot's live-trading sizing model.

---

## Background

With a small account (for example, ~$2,000), stock price alone can create major distortions if sizing is not handled carefully.

Examples:
- a $35 stock and a $900 stock should not be treated the same if the bot is buying "1 share"
- whole-share sizing can make expensive stocks impossible or oversized
- low-priced stocks can become unintentionally overweighted

The key question is:

> How should the bot decide **how much to buy** in a way that is consistent, scalable, and easy to reason about?

---

## Core Design Principle

The sizing system should optimize for:

- consistency across symbols
- compatibility with small capital
- compatibility with multi-strategy capital allocation
- low implementation complexity
- easy debugging
- safe behavior under edge cases

---

## Main Alternatives

### 1. Whole-Share Sizing

#### Description

Trade a fixed number of shares, usually 1 or another simple integer amount.

Example:
- buy 1 share of AAPL
- buy 1 share of NVDA

#### Advantages

- very simple
- easy to understand
- easy to implement

#### Disadvantages

- inconsistent exposure across stocks
- expensive stocks dominate capital
- cheap stocks are under-allocated
- unsuitable for small accounts
- poor fit for strategy-level capital allocation

#### Verdict

Not appropriate as the primary sizing model for this bot.

---

### 2. Notional-Based Sizing (Dollar-Based)

#### Description

Size every trade in dollars first, then convert to shares.

Example:
- desired trade size = $200
- shares = 200 / price

#### Advantages

- consistent capital exposure across symbols
- works well with small accounts
- naturally supports fractional shares
- easy to integrate with strategy budgets
- simple and elegant

#### Disadvantages

- requires fractional support for expensive stocks
- can produce very small share quantities
- needs minimum trade threshold to avoid tiny orders

#### Example

If target notional is $200:

- AAPL at $180 → 1.11 shares
- NVDA at $900 → 0.22 shares
- BAC at $35 → 5.71 shares

#### Verdict

Best default approach for small-account multi-strategy trading.

---

### 3. Equal-Weight / Target-Weight Sizing

#### Description

Allocate capital by portfolio or strategy weights.

Example:
- total capital = $2,000
- SMA bucket = 60% = $1,200
- RSI bucket = 40% = $800
- SMA can hold 4 positions → target ~$300 per position

#### Advantages

- integrates naturally with strategy-level capital allocation
- easy to reason about
- good fit for portfolio construction
- easy to explain in logs and docs

#### Disadvantages

- still needs a notional-to-share conversion at execution
- does not by itself account for trade risk or stop distance
- assumes equal sizing is acceptable within the strategy

#### Verdict

Very good foundation when combined with notional-based execution.

---

### 4. Fixed Fractional Risk Sizing

#### Description

Size each trade based on the dollar amount willing to lose, relative to stop distance.

Example:
- account = $2,000
- risk per trade = 1% = $20
- stop distance = $4
- shares = $20 / $4 = 5 shares

#### Advantages

- mathematically tied to downside risk
- more professional risk control
- avoids over-sizing trades with wide stops

#### Disadvantages

- requires clearly defined stop-loss logic
- depends on reliable stop distance estimation
- more complex than needed for early live deployment
- harder to debug than simple notional sizing

#### Verdict

Strong future enhancement, but not the best first sizing model for v1 live.

---

### 5. Hybrid Whole-Share / Fractional Execution

#### Description

Size in dollars first, compute ideal shares, then:

- use whole shares when rounding down stays close enough to target
- use fractional shares when whole-share rounding would distort exposure too much

This treats fractional as an execution detail, not as a separate sizing philosophy.

#### Example

Target notional = $200

- price = $48 → 4.17 shares  
  Could use 4 shares if the difference is acceptable.

- price = $180 → 1.11 shares  
  Could use 1 share or fractional depending on tolerance.

- price = $900 → 0.22 shares  
  Fractional required.

#### Advantages

- keeps notional-based logic intact
- reduces unnecessary fractional precision when whole shares are good enough
- feels operationally clean
- works well for small and medium accounts

#### Disadvantages

- requires a tolerance rule
- slightly more branching than pure notional sizing
- still needs minimum trade threshold

#### Verdict

Excellent practical option if implemented simply.

---

## A Common But Weaker Idea

### Conditional Fractional Logic Based on Share Price

#### Example idea

> If share price exceeds X% of allocated capital, use fractional shares. Otherwise use whole shares.

#### Why it sounds attractive

- intuitive
- seems efficient
- feels like fractional is used only when needed

#### Why it is weaker than notional-first sizing

- creates inconsistent behavior between similar trades
- makes share price, not target exposure, the key decision variable
- introduces unnecessary branching
- can bias the portfolio toward lower-priced stocks

#### Verdict

Do not use share price percentage as the primary sizing decision rule.

If hybrid execution is used, it should still start from target notional, not from share price thresholds.

---

## Recommended Approach for This Bot

## Near-Term Recommendation

Use:

1. strategy-level capital allocation
2. target notional per position
3. fractional shares when needed
4. optional whole-share rounding when close enough
5. minimum trade threshold

This gives:

- consistency
- simplicity
- good small-account support
- easy integration with SMA + RSI

---

## Suggested Model

### Step 1 — Strategy Budget

Example:
- total capital = $2,000
- SMA = 60% = $1,200
- RSI = 40% = $800

### Step 2 — Position Budget

Example:
- SMA max positions = 4 → target ~$300 each
- RSI max positions = 5 → target ~$160 each

### Step 3 — Convert Notional to Shares

```python
shares_raw = target_notional / price
```

### Step 4 — Execution Decision

Possible rule:
- if rounded whole-share notional is within 5%–10% of target, use whole shares
- otherwise use fractional shares

### Step 5 — Minimum Trade Threshold

If resulting trade notional is below a minimum threshold (for example $50–$100), skip the trade.

---

## Example Hybrid Rule

```python
shares_raw = target_notional / price
shares_whole = int(shares_raw)

whole_notional = shares_whole * price
difference_pct = abs(target_notional - whole_notional) / target_notional

if shares_whole >= 1 and difference_pct <= 0.10:
    shares_final = shares_whole
else:
    shares_final = shares_raw
```

This is only an example.  
The exact tolerance should be simple and conservative.

---

## Observability Requirements

The sizing layer should log:

- strategy name
- target notional
- current price
- raw computed shares
- rounded whole-share candidate
- final shares used
- final notional
- whether fractional was used
- reason for skipping if trade is rejected

### Example log

```text
position sizing: strategy=rsi target_notional=160.00 price=182.50 raw_shares=0.8767 whole_shares=0 final_shares=0.8767 fractional_used=true final_notional=160.00
```

---

## Edge Cases to Handle

### 1. Very High-Priced Stocks

Examples:
- NVDA
- AVGO

Requirement:
- fractional support or skip logic

---

### 2. Very Low-Priced Stocks

Requirement:
- ensure liquidity filters exist
- avoid excessively large share counts in thin names

---

### 3. Tiny Trades

Requirement:
- minimum trade threshold
- skip meaningless trades

---

### 4. Rounding to Zero

If whole-share rounding leads to zero shares:
- use fractional if allowed
- otherwise skip the trade

---

### 5. Insufficient Capital

If budgeted capital is not available due to pending orders or allocation limits:
- reject trade cleanly
- log reason explicitly

---

## Interaction with Other Systems

The sizing model must work with:

- strategy-level capital allocation
- max positions per strategy
- portfolio-level exposure caps
- fractional-share capable execution
- future risk overlays

Sizing should remain **centralized**, not duplicated across strategies.

---

## What Not to Do Yet

Avoid:

- Kelly sizing
- highly dynamic risk models
- volatility-adjusted portfolio construction
- complicated multi-factor sizing formulas
- share-price-threshold sizing rules as primary logic

These can be explored later once the live system is stable.

---

## Future Evolution

After the bot is stable live, sizing can evolve toward:

- fixed fractional risk sizing using stop distance
- ATR-based volatility-aware sizing
- dynamic strategy allocation based on performance
- drawdown-aware throttling
- portfolio correlation-aware sizing

But the first live version should prefer clarity and robustness.

---

## Decision Summary

### Best current options

#### Option A — Pure Notional-Based Sizing
Best for simplicity and consistency.

#### Option B — Notional-Based Sizing with Hybrid Whole/Fractional Execution
Best practical compromise if you want cleaner execution behavior when whole shares are close enough.

---

## Final Recommendation

For this bot, the best near-term live approach is:

- **strategy allocation determines target capital**
- **target capital determines target notional per position**
- **execution converts notional to shares**
- **fractional shares are used naturally when needed**
- **whole shares may be used when they are close enough to target**
- **tiny trades are skipped**

This preserves consistency while keeping implementation clean.

---

## Key Insight

The sizing system should answer:

> How much capital should this trade use?

It should **not** answer:

> Is this stock too expensive for whole shares?

That question is secondary.  
Target exposure comes first.
