# Capital Allocation Reference

This document consolidates the ideas from:

- `Trading_Bot_Allocation_Strategy.pdf`
- `Ideal_Capital_Allocation.md`
- `capital-allocation.md`

It is meant to serve as a clean reference for two related concepts:

1. Practical enhancements we can make to the current capital allocator now
2. The long-term "ideal" allocator built around a conservative Kelly framework

This is a design reference, not a direct implementation spec. Formula examples
and JSON examples are included because they make the concepts easier to reason
about, but they should be treated as illustrative unless explicitly promoted
into production config and code.

## 1. Core Goals

The allocator should optimize for:

- capital protection first
- better use of idle cash
- clear separation between shareable and non-shareable capital
- volatility-aware sizing instead of naive dollar buckets
- a clean path from static sleeves to data-driven dynamic allocation

## 2. Summary

The near-term recommendation is to keep the current sleeve allocator, but make
it more flexible and more informative:

- split the account into an `equity pool` and an `isolated options vault`
- let equity strategies borrow idle capital from each other under controlled rules
- use priority ordering when capital is scarce
- keep sizing tied to risk-to-stop, not just sleeve dollars
- log initial risk and realized `R` so a future Kelly allocator has real inputs

The long-term recommendation is to move toward a conservative
multi-strategy `Quarter-Kelly` allocator that:

- scales strategies based on realized edge
- normalizes for volatility
- penalizes high correlation between strategies
- updates weights periodically from actual trade history

## 3. Immediate Enhancements to the Current Allocator

### 3.1 Dual-Pool Architecture

The account should be treated as two separate capital pools:

- `equity pool`: shared by SMA, Donchian, RSI, and other cash equity strategies
- `isolated options vault`: reserved for SPY options or other options strategies

Recommended baseline split:

- `equity pool`: `95%`
- `isolated options vault`: `5%`

Rationale:

- preserve dedicated convex-risk capital for options
- prevent equity strategies from consuming capital reserved for options
- keep allocator behavior simple and predictable

Important correction:

Some earlier notes referenced an equity `T+2` versus options `T+1` settlement
mismatch. In U.S. markets, the standard settlement cycle for most equities is
now `T+1` as of May 28, 2024, which aligns with options settlement timing.
The case for an isolated options vault should therefore be framed mainly as:

- risk segregation
- buying-power discipline
- operational clarity

not as a reliance on an equity `T+2` versus options `T+1` distinction.

### 3.2 Elastic Equity Sleeves

Within the equity pool, strategies should be allowed to use idle capital from
other equity sleeves, but only under guardrails.

Recommended rule:

- each equity strategy has a `target_pct`
- if a strategy is at its sleeve cap and another equity sleeve is idle, it may
  borrow from that idle equity capital
- options capital is never lent to equity strategies

Recommended defaults:

- borrowing only allowed within the `equity pool`
- borrowing only allowed while total account utilization is below `80%`
- a strategy may stretch up to `115%` of its target sleeve before needing
  explicit reallocation or the next rebalance cycle

Example:

- SMA target = `45%`
- stretch cap = `45% x 1.15 = 51.75%`

This is not meant to create uncontrolled drift. It is a tactical efficiency
patch for avoiding rejected high-conviction trades while another sleeve is idle.

### 3.3 Waterfall Priority

When equity capital is scarce, not every valid signal should be treated equally.
The allocator should fund higher-priority strategies first.

Illustrative priority order:

- `0`: isolated options sleeve
- `1`: RSI Reversion
- `2`: Donchian Breakout
- `3`: SMA Crossover

Interpretation:

- lower number = higher priority
- if capital is tight, a priority `1` signal should be funded before a
  priority `3` signal

This matters most when:

- account utilization is already high
- multiple strategies fire in the same cycle
- one strategy is time-sensitive or empirically higher quality

### 3.4 ATR-Based Risk Sizing

Capital allocation should not rely only on sleeve percentages. Position sizing
should stay anchored to risk.

Illustrative formula:

```text
Units = (Available_Equity_Pool x Risk_Per_Trade) / (ATR x Stop_Multiplier)
```

For the common `2 x ATR` stop:

```text
Units = (Available_Equity_Pool x Risk_Per_Trade) / (ATR x 2)
```

This helps:

- avoid oversizing volatile symbols
- prevent a strategy from "hogging" the pool with superficially cheap shares
- keep per-trade risk more comparable across different names

Note:

This logic already aligns with the current risk-manager philosophy better than
simple dollar-allocation thinking. The allocator should decide how much capital
is available; the risk manager should still decide how much risk a trade may take.

### 3.5 Regime-Aware Capital Tilting

An optional enhancement is to tilt capital between broad strategy buckets based
on market regime.

Example bucket split:

- `trend bucket`: SMA + Donchian
- `range bucket`: RSI

Illustrative behavior:

- in strong trend regimes, allow more effective capital to flow toward trend strategies
- in choppy/range regimes, preserve more effective capacity for mean reversion

This should be treated as a second-order enhancement, not a substitute for
basic sleeve controls.

### 3.6 Required Logging for Future Allocation Logic

To graduate from static sleeves to dynamic allocation, the trade log needs more
than entry and exit prices. At minimum, each trade should persist:

- `initial_stop_loss`
- `initial_risk_per_share`
- `initial_risk_dollars`
- `r_multiple`
- `strategy_name`
- `entry_date`
- `exit_date`
- realized P&L

Illustrative formulas:

```text
Initial_Risk_Per_Share = Entry_Price - Stop_Loss
```

```text
R_Multiple = (Exit_Price - Entry_Price) / (Entry_Price - Stop_Loss)
```

The exact sign conventions can be standardized later, but the key point is:
the database must record enough information to reconstruct normalized outcomes,
not just raw dollars.

## 4. Example Near-Term Configuration Schema

This JSON is an example reference, not a mandated final schema:

```json
{
  "sma_crossover": {
    "target_pct": 0.45,
    "type": "equity",
    "priority": 3,
    "can_stretch": true
  },
  "rsi_reversion": {
    "target_pct": 0.25,
    "type": "equity",
    "priority": 1,
    "can_stretch": true
  },
  "donchian_breakout": {
    "target_pct": 0.25,
    "type": "equity",
    "priority": 2,
    "can_stretch": true
  },
  "spy_options_reversion": {
    "target_pct": 0.05,
    "type": "isolated",
    "priority": 0,
    "can_stretch": false
  }
}
```

Suggested field meanings:

- `target_pct`: baseline share of account capital
- `type`: whether the sleeve is shareable (`equity`) or isolated
- `priority`: funding precedence under scarce capital
- `can_stretch`: whether the sleeve may temporarily borrow idle capacity

Optional future fields:

- `min_pct`
- `max_pct`
- `hard_isolated`
- `kelly_multiplier`
- `vol_target_pct`
- `correlation_group`

## 5. The Ideal Allocator: Quarter-Kelly Multi-Strategy Framework

### 5.1 Why Kelly

The Kelly Criterion is a growth-optimal framework for sizing bets. In theory,
it maximizes long-term capital growth when the edge and payoff distribution are
known with sufficient accuracy.

In practice, the raw Kelly output is too aggressive for real trading systems
because:

- inputs are noisy
- trade distributions are fat-tailed
- edge changes over time
- strategy correlation creates hidden concentration

That is why the preferred production form is:

- `Quarter-Kelly`, not Full Kelly

### 5.2 Single-Strategy Kelly Example

One common version:

```text
f* = (bp - q) / b
```

Where:

- `f*` = optimal fraction of capital
- `p` = probability of a win
- `q` = probability of a loss = `1 - p`
- `b` = gain per unit risk

An equivalent formulation seen in the source notes:

```text
f* = (p / a) - (q / b)
```

Where:

- `a` = fraction lost in a losing trade
- `b` = fraction gained in a winning trade

Quarter-Kelly then becomes:

```text
Quarter_Kelly = 0.25 x f*
```

### 5.3 Why Multi-Strategy Kelly Is Different

A portfolio with several live strategies cannot allocate each strategy in isolation.
It should solve for a vector of weights that reflects:

- each strategy's estimated edge
- each strategy's volatility
- covariance across strategies

This matters because two individually good strategies can still create too much
portfolio risk if they are strongly correlated.

### 5.4 Volatility Normalization

Before or alongside Kelly sizing, each strategy should be volatility-aware.

Practical intuition:

- if Strategy A is roughly twice as volatile as Strategy B, Strategy A should
  not receive the same raw weight unless its edge is clearly superior

This keeps one volatile sleeve from dominating portfolio behavior.

### 5.5 Correlation Penalties

A good allocator should treat highly correlated strategies as partially the same bet.

Example:

- if SMA and Donchian have correlation above `0.7`, the allocator should reduce
  their combined effective weight relative to two truly independent strategies

This can be implemented later through:

- covariance matrix inputs
- correlation-based penalty factors
- grouping rules for "same-risk" sleeves

### 5.6 Recommended Kelly Guardrails

Even once the allocator becomes Kelly-driven, it should never be unconstrained.

Recommended production guardrails:

- use `Quarter-Kelly` only
- require a minimum sample size before Kelly affects live weights
- cap rebalance changes per cycle
- enforce hard floor and ceiling allocations
- preserve isolated options vault rules
- keep existing risk-manager hard limits fully active

Illustrative examples:

- no Kelly-driven changes before `50+` trades per strategy
- no more than `5 percentage points` of allocation change per rebalance
- do not let any single strategy exceed a fixed portfolio cap without explicit approval

## 6. Example Kelly-Era Configuration Schema

This example shows how a later dynamic allocator might extend the simpler sleeve config:

```json
{
  "sma_crossover": {
    "base_target_pct": 0.45,
    "type": "equity",
    "priority": 3,
    "kelly_multiplier": 0.25,
    "min_pct": 0.20,
    "max_pct": 0.55,
    "vol_target_pct": 0.35,
    "correlation_group": "trend"
  },
  "rsi_reversion": {
    "base_target_pct": 0.25,
    "type": "equity",
    "priority": 1,
    "kelly_multiplier": 0.25,
    "min_pct": 0.10,
    "max_pct": 0.35,
    "vol_target_pct": 0.25,
    "correlation_group": "mean_reversion"
  },
  "donchian_breakout": {
    "base_target_pct": 0.25,
    "type": "equity",
    "priority": 2,
    "kelly_multiplier": 0.25,
    "min_pct": 0.10,
    "max_pct": 0.35,
    "vol_target_pct": 0.30,
    "correlation_group": "trend"
  },
  "spy_options_reversion": {
    "base_target_pct": 0.05,
    "type": "isolated",
    "priority": 0,
    "kelly_multiplier": 0.10,
    "min_pct": 0.03,
    "max_pct": 0.07,
    "hard_isolated": true,
    "correlation_group": "options"
  }
}
```

Again: this is not a final schema. It is an example of the kinds of controls
the ideal allocator will likely need.

## 7. Transition Path

### Phase 1: Improve the Existing Sleeve Model

Implement:

- dual-pool architecture
- elastic equity borrowing
- waterfall priority
- continued ATR/risk-based sizing

### Phase 2: Improve Data Capture

Add or verify logging for:

- initial stop
- initial risk
- R-multiple
- strategy-level outcomes

### Phase 3: Generate Advisory Kelly Signals

Do not immediately trade on Kelly. First:

- compute estimated Kelly fractions from actual trade history
- compare recommended weights against static sleeve behavior
- evaluate stability across rolling windows

### Phase 4: Introduce Conservative Dynamic Rebalancing

After sufficient sample size and review:

- let Kelly adjust sleeve targets within hard min/max caps
- introduce volatility normalization
- introduce correlation penalties

## 8. Recommended Defaults

These defaults best reconcile the three source documents into one rational starting point:

- `95%` equity pool / `5%` isolated options vault
- elastic borrowing only within the equity pool
- stretch allowed only while account utilization is below `80%`
- stretch cap of `115%` of target sleeve
- waterfall priority active when capital is scarce
- maintain ATR-based risk sizing
- require `R` logging before any Kelly work
- use `Quarter-Kelly` only, never Full Kelly

## 9. Open Design Questions

These should be finalized before implementation:

1. Should the elastic borrowing trigger be `80%` or `85%` utilization?
2. Should regime tilting be part of the first allocator upgrade or a later phase?
3. Should Kelly operate daily, weekly, or only after a minimum number of new trades?
4. Should correlation penalties be continuous or based on coarse grouping rules?
5. Should options remain permanently fixed at `5%`, or be allowed a narrow Kelly-bounded band later?

## 10. Practical Conclusion

The right next step is not to jump straight into Kelly. The right next step is:

- make the current sleeve allocator more efficient
- isolate options capital clearly
- keep position sizing risk-based
- collect the normalized trade data needed for statistical allocation

Only after the bot has enough reliable strategy history should it graduate to a
conservative, covariance-aware Quarter-Kelly allocator.

## Sources

- [Trading_Bot_Allocation_Strategy.pdf](/Users/franco/trading-bot/Trading_Bot_Allocation_Strategy.pdf)
- [Ideal_Capital_Allocation.md](/Users/franco/trading-bot/Ideal_Capital_Allocation.md)
- [capital-allocation.md](/Users/franco/trading-bot/capital-allocation.md)
- For the `T+1` settlement update used to rationalize the isolation discussion:
  [FINRA: Understanding Settlement Cycles](https://www.finra.org/investors/insights/understanding-settlement-cycles)
  and [FINRA: Final Reminder - T+1 Settlement](https://www.finra.org/filing-reporting/technical-notice/final-reminder-t-1-settlement-052224)
