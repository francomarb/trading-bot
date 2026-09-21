# Donchian Deployment Validation

**Roadmap item:** `11.74`

**Status:** IN PROGRESS — research extension pre-registered; production settings unchanged

**Pre-registration date:** 2026-09-21

## Governing assessment

Donchian is classified as:

> **Plausible core signal, unproven deployment, strongly adverse initial regime.**

Backtests are hypothesis and robustness evidence. Matched-configuration paper
outcomes, reconciliation, and operational evidence remain authoritative. No
historical result in this work can graduate the strategy or explain away poor
forward results.

## Original five-part requirement and current status

1. **Repair the production-mirror and exit-observation evidence defects —
   complete.** Lifecycle-exact reconciliation now uses recorded strategy
   identity and regime. Donchian's 30/10-versus-30/15 observation uses the
   correct owner identity and prior-close channel definition.
2. **Freeze the universe and configuration — active.** The frozen paper cohort
   begins with the corrected 100-name durable-liquidity pool and its current
   configuration hash. Later universe, channel, filter, sizing, regime, feed,
   or execution changes start a new cohort rather than being pooled into this
   one.
3. **Run a pre-registered production-like portfolio simulation — partial.**
   `11.73` implemented shared account cash, the production 12% baseline sleeve
   budget, 4.8% per-position notional cap, 0.40% entry-risk target, whole-share
   sizing, the allocator's pre-sizing $100 availability floor, DAY STOP_LIMIT
   behavior, fixed fill-anchored 2 ATR protection, eight-position concurrency,
   edge/regime gates, and deterministic candidate order. It deliberately did
   not enforce the pre-registered 4R heat cap and omitted allocator stretch.
4. **Use a holdout and compare the implementation families — partial.** The
   fixed 2021–2025 folds compared close-based 20/10, 30/10, 30/15, and 55/20
   on the expanded pool. They did not test classic prior-high/prior-low Turtle
   semantics or the original 32-name universe. Those missing comparisons are
   the scope of this extension. The already inspected years must not be
   relabelled as a new untouched holdout.
5. **Collect enough forward evidence — open.** Do not reconsider Donchian for
   live use before at least 25 trusted exits under one frozen configuration and
   enough distinct entry clusters have resolved. Twenty-five symbols entering
   the same market breakout are not 25 independent observations.

## Frozen research extension

The extension answers only the comparisons omitted by `11.73`. It must reuse
the shared-capital simulator rather than the old per-symbol production-mirror
script.

### Universes

- `ai_bigtech`: the frozen original 32-name operator-supplied research basket;
- `durable_100`: the ranked first 100 names in the corrected active Donchian
  pool, excluding lifecycle-only protection members.

### Strategy families

- **Current 30/15 close/close control:** entry close above the prior 30 closes;
  exit close below the prior 15 closes, followed by the production-style next
  session order behavior.
- **Classic 20/10 high/low:** entry at a breakout of the prior 20 session highs
  and exit at a break of the prior 10 session lows.
- **Classic 55/20 high/low:** the slower Turtle System 2 comparison using the
  same high/low semantics.

The classic variants are implementation-family comparisons, not parameter
challengers inside the current close-based implementation. The report must not
call close-based 20/10 "classic."

### Portfolio contract

Hold constant across every cell:

- delayed consolidated SIP daily bars and full-history indicator warmup;
- the stock SMA200 and $20M consolidated-dollar-volume filters;
- the current SPY regime policy: TRENDING, RANGING, and VOLATILE allowed;
  BEAR and unknown blocked;
- production baseline sleeve capital, per-position notional cap, whole-share
  sizing, minimum-availability floor, and eight-position ceiling;
- DAY STOP_LIMIT entry/chase behavior and deterministic liquidity order;
- fill-anchored static 2 ATR protective stops, including gap-through behavior;
- **the pre-registered 4R / 1.60%-of-equity heat cap enforced**, including
  pending DAY-entry reservations rather than filled positions alone;
- the same modeled exit/stop transaction cost used by `11.73`;
- no earnings blackout unless trustworthy point-in-time history is available;
  omission must remain explicit; and
- no allocator stretch. This isolates the Donchian baseline budget and must be
  labelled as a conservative deployment boundary rather than exact full-book
  arbitration.

### Evaluation boundary

The historical period is a fixed-cohort sensitivity study because current
universe membership and the 2021–2025 results are already known. Report each
calendar year, the common fixed period, exit-reason mix, capacity and heat
refusals, contributor concentration, and usable-history coverage. Do not select
a winner by adding or changing variants after results are viewed.

The genuinely untouched evidence begins prospectively after this
pre-registration. Paper outcomes must remain grouped by exact configuration
hash and entry cluster. Historical diagnostics can justify a separately
reviewed paper experiment; they cannot satisfy the 25-exit/independent-cluster
graduation requirement.

## Decision rules

- The current paper configuration remains close-based 30/15 throughout this
  study.
- A historical challenger is only a hypothesis for a separate paper cohort.
- No variant is promoted merely for the best aggregate return or Sharpe.
- Dependence on one year, one symbol, or one clustered market episode defeats
  a durability claim.
- Donchian remains **paper-only, no graduation** until the forward-evidence
  requirement is met and reviewed by the operator.
