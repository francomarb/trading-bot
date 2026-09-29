# SMA Watchlist Selection

**Active rule:** `sma_watchlist_v3_durable_liquid_pool`

**Promoted:** 2026-09-28

**Ranked pool:** 100 companies, followed only by temporary lifecycle-preservation members

This document is the authoritative SMA watchlist specification. The historical
`sma_watchlist_v2` technical snapshot method is retired and must not be used to
refresh production or paper configuration.

## Responsibility Split

The watchlist answers:

> Which companies are durable and practical enough for SMA to monitor?

The strategy and runtime gates answer:

> Is one of those companies a good trade now?

```text
Tradable stock universe
    -> durable eligibility
    -> liquidity-ranked top 100
    -> 20/50 crossover
    -> runtime entry gates
    -> portfolio and risk checks
    -> execution
```

Watchlist membership is not a buy recommendation. A member may remain inactive
for months without a valid crossover.

## Hard Membership Gates

A ranked company must satisfy every requirement using completed-session data:

- active and tradable through Alpaca;
- stock-like US security rather than an ETF, fund, warrant, unit, right, OTC
  security, or other non-operating-company instrument;
- at least 260 clean adjusted daily bars;
- latest close at least $10;
- 50-session average SIP dollar volume at least $50 million;
- market capitalization at least $2 billion;
- affirmative solvency under the shared durable-company policy: profitable, or
  enough cash runway for at least 12 months;
- preferred share class (`GOOG`, not both `GOOG` and `GOOGL`); and
- all required provider facts available without an unresolved error.

Required facts fail closed. A provider error, unknown market capitalization, or
unknown solvency cannot silently admit a company or shift the ranked boundary.
The selector makes one bounded retry for a whole-request fundamentals failure.

## Ranking

Eligible companies are ordered by descending 50-session average SIP dollar
volume. The first 100 form the ranked opportunity pool.

Liquidity rank is an execution-priority measure, not a forecast. Rank 1 is the
most liquid eligible company, not the company with the highest expected return.
The strategy applies the same signal and gates to every member.

The target of 100 was chosen because the promotion scan found:

| Pool | Raw 20/50 crossovers | Active days | Peak same-day signals | Days above hard 8-position count |
|---:|---:|---:|---:|---:|
| 50 | 119 | 83 | 4 | 0 |
| **100** | **243** | **142** | **5** | **0** |
| 200 | 491 | 193 | 11 | 3 |

These are current-universe opportunity counts, not a survivorship-free return
backtest. The eight-position column is a hard count ceiling, not the actual
sleeve-dollar ceiling; risk-sized dollars can produce `SLEEVE_FULL` around
three to five positions. One hundred was selected because it roughly doubled
coverage versus 50 with bounded cycle cost and disclosed clipping. Two hundred
added another 100 evaluations and substantially more clipping. Forward
`SLEEVE_FULL` refusals determine whether the 100-name pool starves candidates.

## Diagnostics That Do Not Control Membership

The report records these for review, but they do not admit, reject, or rank a
company:

- current SMA50/SMA150/SMA200 state or alignment;
- 12-to-1-month momentum and relative strength;
- position within the 52-week range;
- ADX and directional indicators;
- ATR percentage;
- share volume independent of dollar liquidity;
- historical 20/50 crossover counts;
- free cash flow and recent revenue growth; and
- sector or industry concentration.

These are temporary market states, non-sector-neutral accounting facts, or
portfolio context. Using them as quarterly membership gates caused excessive
turnover and duplicated decisions that belong at signal, risk, or portfolio
time.

## Runtime Trade Gates

After membership, an entry still requires all applicable runtime controls:

1. a fresh bullish 20-day/50-day SMA crossover;
2. an allowed market regime (`TRENDING` or `RANGING`);
3. stock close above its 200-day SMA;
4. 10-session median volume above 30-session median volume;
5. no earnings announcement within the two-day pre-earnings blackout;
6. no ownership, pending-order, symbol-lock, or conflicting-position issue;
7. available sleeve position count, capital, gross exposure, and risk budget;
8. successful broker and lifecycle preflight.

The sector gauge is warning-only for SMA. A COLD or unknown sector supplies
context but does not veto an entry.

The stock-SMA, volume, and earnings filters fail open when their documented
inputs are unavailable. This is existing runtime behavior and is distinct from
the watchlist selector's fail-closed durable membership rules.

## Risk And Capacity

- SMA targets 0.60% of account equity in initial stop risk.
- The protective stop is two ATR below the fill-based entry anchor.
- Position, sleeve, cash, and gross-exposure caps may reduce approved quantity.
- The sleeve hard maximum is eight positions.
- Volatility changes position size; it is not a membership rejection.

At the current allocation and caps, the position notional cap starts reducing
the risk target at approximately 3.12% ATR/close. This is expected and is
reported as risk-target coverage rather than treated as a bad company.

## Lifecycle Preservation

The ranked pool always occupies the first 100 settings entries. If an existing
SMA-owned position falls outside the new pool, append it after the ranked names
until it is flat and terminal. Never remove it merely because a refresh changed
membership. Unresolved entry orders must also be reconciled before promotion.

Lifecycle-preservation members do not change the ranked target and must not be
described as selector winners.

## Refresh And Promotion Procedure

Refresh quarterly, or after a material tradability, corporate-action,
data-quality, or persistent opportunity-starvation event. Do not rotate the
pool for ordinary one-scan liquidity movement.

Every refresh must:

1. run `scripts/sma_durable_watchlist_scan.py` with delayed SIP data and at
   least a 1,440-minute completed-session delay;
2. compare the nested 25/50/100/200 pools and inspect rejection reasons;
3. resolve every required-provider error or reproduce a clean stable rerun;
4. privately reconcile lifecycle ownership and unresolved SMA entry orders;
5. obtain explicit operator approval before editing `SMA_WATCHLIST`;
6. preserve required lifecycle members outside the ranked 100;
7. update configuration, this specification, deployment documentation, and
   `PLAN.md` together;
8. run focused and full tests;
9. recycle only with `./recycle_bot.sh`; and
10. verify startup ownership, protective stops, ranked count, cycle duration,
    market-data request volume, and errors.

A promotion starts a new strategy configuration-hash evidence cohort. Do not
pool its paper outcomes with the previous exact configuration. Removing an
appended lifecycle-preservation member such as `DOCN` also changes the full
watchlist hash; record that later boundary explicitly rather than treating it
as the same exact cohort.

## Implementation And Evidence

- Active configuration: `config/settings.py::SMA_WATCHLIST`
- Active selector: `scripts/sma_durable_watchlist_scan.py`
- Methodology and filter audit:
  [`sma-watchlist-methodology-audit.md`](sma-watchlist-methodology-audit.md)
- Approved scan artifact:
  [`reports/sma_durable_watchlist_scan_20260928_promoted.md`](reports/sma_durable_watchlist_scan_20260928_promoted.md)

`scripts/sma_watchlist_scan.py` contains the retired v2 research implementation
and shared market-data helpers still imported by research scripts. Its command
line is opt-in historical reproduction only; it is not an alternative active
selector.

## Version History

- `sma_watchlist_v3_durable_liquid_pool` — active from 2026-09-28. Separates
  durable company membership from time-sensitive trade gating and ranks by
  dollar liquidity.
- `sma_watchlist_v2` — retired 2026-09-28. Mixed current trend, relative
  strength, ADX, ATR, fundamentals, and sector caps into membership; retained
  only for historical report reproduction.
- `sma_watchlist_v1` — retired initial technical-selection method.
