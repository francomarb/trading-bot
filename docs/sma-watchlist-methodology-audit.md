# SMA Watchlist Methodology And Filter Audit (`11.75`)

**Audit date:** 2026-09-28

**Status:** Complete and promoted 2026-09-28. The report-only v3 selector,
bounded fundamentals retry, clean completed-session report, private lifecycle
reconciliation, operator approval, configuration update, and paper-engine
recycle are complete. Startup loaded the 100 ranked names plus required
lifecycle preservation, restored ownership in NORMAL mode, required no
protective-stop repair, and completed the closed-market first cycle without an
error. Representative warmed market-open latency remains an observation item.
The approved scan artifact is
[`reports/sma_durable_watchlist_scan_20260928_promoted.md`](reports/sma_durable_watchlist_scan_20260928_promoted.md).

## Decision Summary

Retire `sma_watchlist_v2` as the authority for periodic membership. Its current
trend, relative-strength, ADX, 52-week, ATR, and share-volume gates mix durable
company eligibility with temporary entry timing. A static list selected this
way stops satisfying its own rule almost immediately and turns a quarterly
refresh into a near-total rotation.

Build `sma_watchlist_v3_durable_liquid_pool` using the same responsibility split
as the active RSI and Donchian selectors:

```text
durable company pool -> 20/50 crossover -> runtime gates -> risk -> execution
```

The promoted paper cohort is **100 ranked opportunity names**, ordered by
50-session SIP dollar liquidity, plus any lifecycle-protection members outside
the ranked pool. Operator approval was recorded on 2026-09-28. The promotion
starts a new configuration-hash evidence cohort; earlier and later outcomes
must not be presented as one exact configuration.

Membership is strategy-specific, but runtime ownership is portfolio-wide.
SMA's ranked 100 overlaps 97 Donchian names and contains all 50 ranked RSI
names. The other strategies' identity hashes do not include SMA membership,
so 2026-09-28 is an explicit **effective-conditions boundary** for their
forward watches even though their hashes did not change. The engine now emits
rolling `SYMBOL_CONFLICT` counts grouped by `(blocked_strategy,
owner_strategy)` so this allocation effect is measured rather than attributed
to the signal in hindsight.

## Evidence Reviewed

- RSI v3 and Donchian v1 durable-pool procedures and their promoted reports.
- `sma_watchlist_v2`, its May/June reports, current 56-name configuration, and
  every later manual addition.
- A completed-session delayed-SIP scan of 5,758 tradable assets; 5,756 returned
  usable bars.
- A current durable-liquidity scan using the corrected shared fundamentals
  parser, affirmative solvency, and a bounded retry for whole-request provider
  failures. The clean rerun had no unresolved required-provider rejection.
- Split-sample filter ablation on the frozen 56-name `11.70` universe:
  development 2017-2022 and held-out 2023 through 2026-09-04.
- Current cycle telemetry: 215 slot-symbol evaluations normally complete in
  roughly 19-30 seconds when warm, inside the five-minute cadence.
- Durable lifecycle state and unresolved SMA entry-order state. The private
  reconciliation found no unresolved SMA entry lifecycle orders. Account and
  position details are intentionally not recorded here.

## Why V2 Cannot Drive A Refresh

The completed-session v2 run produced only nine ranked candidates. Three were
already in the configured 56 and six were new. The first rejection recorded
for current members was:

| First rejection | Current names |
|---|---:|
| Price not above SMA50/SMA150/SMA200 | 23 |
| ADX below 20 | 14 |
| SMA50/SMA150/SMA200 not aligned | 6 |
| Too far below the 52-week high | 4 |
| Relative strength below the cutoff | 3 |
| Other liquidity/fundamental failure | 3 |
| Passed the full rule | 3 |

Only four of the June fundamentals-sanitized top 30 survived the September
snapshot. One candidate also changed when the same-day incomplete daily bar was
replaced with the last completed session. This is timing-state churn, not a
stable company-pool refresh.

V2 also fails its own portfolio-hygiene contract. The configured list contains
56 names rather than the documented 10-25 or 50, and 34 are classified as
Technology. The nominal three-per-sector cap reads Alpaca sector metadata,
which is normally unavailable; `UNKNOWN` candidates bypass the cap. Later
operator additions explicitly bypassed the cap and mid-run freeze.

## V3 Durable Eligibility Contract

### Hard membership gates

- active, Alpaca-tradable, stock-like US security;
- at least 260 clean adjusted daily bars;
- latest completed-session close at least $10;
- 50-session average SIP dollar volume at least $50 million;
- market capitalization at least $2 billion;
- affirmatively established solvency: profitable, or sufficient cash runway
  under the shared durable-company policy;
- preferred share-class normalization (`GOOG`, not `GOOGL`); and
- no provider error or unknown required fact.

The selector must fail closed per required field and state whether an exclusion
is a known failure or unavailable data. It must clamp automatically to a
completed session, fail closed when the lifecycle database cannot be read, and
require unresolved entry orders to be reconciled separately before promotion.

### Diagnostics, not membership gates

- current SMA50/SMA150/SMA200 state and alignment;
- 12-to-1-month momentum and relative-strength percentile;
- 52-week-high/low position;
- ADX and DI direction;
- ATR percentage;
- current share volume;
- 20/50 crossover counts;
- free cash flow and revenue growth; and
- sector and industry concentration.

These fields remain useful for review and later pre-registered comparisons, but
none is a defensible current-snapshot hard gate for a static opportunity pool.

## Filter Decisions

| Existing filter | V3 decision | Reason |
|---|---|---|
| Dollar liquidity | Keep hard | Direct execution-quality and capacity relevance. |
| Share volume >= 500k | Remove as hard | Redundant and price-biased once dollar liquidity and a $10 price floor are enforced. |
| Market cap >= $2B | Keep hard | Durable size and financing-risk boundary. |
| Affirmative solvency | Keep hard | Survival requirement, not a return forecast. |
| Positive FCF | Diagnostic only | Not sector-neutral; rejects banks and capital-intensive/cyclical companies for accounting-structure reasons. |
| Positive revenue growth | Diagnostic only | A one-period company-state observation, not established trend-following edge. |
| Current trend stack | Diagnostic/runtime | Temporary state belongs at signal time, not quarterly membership. |
| 52-week strength / relative strength | Diagnostic only | Current momentum ranking would select recent winners and create high churn; no point-in-time walk-forward superiority exists. |
| ADX / DI | Diagnostic only | Entry-timing hypothesis without split-sample support as a pool gate. |
| ATR 1%-8% | Diagnostic/risk | Position sizing and caps already handle volatility; hard membership would confound pool selection with risk policy. |
| Three-per-sector cap | Remove as hard | Trend leadership clusters by sector. If concentration needs limiting, enforce exposure at the portfolio layer rather than deleting opportunities. |
| Broad biotech/diagnostics exclusion | Remove as hard | Over-broad industry proxy; size, liquidity, solvency, earnings controls, and risk should own the actual hazards. |
| Share-class deduplication | Keep hard | Prevents duplicate company exposure and preserves deterministic membership. |

The current top-100 durable pool was also evaluated under SMA's old strict
fundamental profile. Seventy-eight passed and 22 failed: 17 on FCF and seven on
revenue growth, with overlap. The rejected set included banks, insurers,
capital-intensive companies, and commodity cyclicals for which generic FCF or
single-period revenue is not comparable across industries. That is evidence
against using these facts as cross-industry hard membership gates; it is not a
claim that any rejected name will outperform.

## Pool-Size Comparison

The current-universe snapshot uses delayed SIP bars and durable eligibility.
Raw 20/50 crossover counts measure opportunity coverage and contention only;
they are not a survivorship-free backtest or a return forecast.

| Pool | Crossovers (252 sessions) | Active signal days | Peak same-day | Days above hard 8-position count | Zero-cross names | Median ATR% | Cap-clipped | Current-list overlap |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 25 | 62 | 50 | 3 | 0 | 2 | 3.92% | 8 | 14 |
| 50 | 119 | 83 | 4 | 0 | 3 | 3.63% | 22 | 17 |
| **100** | **243** | **142** | **5** | **0** | **3** | **3.63%** | **47** | **22** |
| 200 | 491 | 193 | 11 | 3 | 5 | 3.23% | 97 | 27 |

At the current 30% sleeve allocation, 40% per-position concentration cap,
80% deployable-gross ceiling, 0.60% risk target, and 2x ATR stop, risk sizing
binds at ATR14/close of approximately 3.12%. Calmer names are conservatively
notional-capped; this reduces risk and is not an eligibility failure.

Twenty-five and 50 names leave a slow signal with limited breadth. One hundred
roughly doubles the 50-name opportunity coverage. The eight-position column is
only the allocator's hard count ceiling, not usable sleeve-dollar capacity:
with the observed median ATR and current risk target, dollars commonly bind at
roughly three to five positions, and paper already produced `SLEEVE_FULL` at
four. Two hundred doubles raw coverage again, adds another 100 evaluations to
every cycle, and cap-clips almost half the pool. The 100-name choice therefore
rests on bounded cycle cost, broader opportunity coverage, and measurable
clipping—not a claim that five same-day signals all fit. `SLEEVE_FULL`
refusals are the forward starvation metric.

### Ranking diagnostics

Reordering the same current eligible 200 by current momentum produced 218 raw
crossovers in its first 100 and retained 51 liquidity-top-100 names. Ordering by
52-week-high proximity produced 261 crossovers and retained 52. The higher
52-week count mainly shows that selecting current leaders finds companies with
recent trend activity; it is not forward or point-in-time evidence. Both
technical rankings would rotate nearly half the cohort from one snapshot.
Liquidity remains the v3 default because it is durable, execution-relevant,
and does not claim to forecast returns.

Moving from 56 to 100 SMA names adds 44 slot-symbol evaluations. Recent warm
cycles before promotion processed 215 combinations in roughly 19-30 seconds.
Observed healthy cycles on 2026-09-28 made about 220 bar requests for 143 unique
symbols in roughly 20 seconds; no 429 was observed, showing that request count
tracks slot-symbol evaluations rather than unique symbols. The required
market-open check therefore covers both cold first-cycle and warm duration,
total stock requests, duplicate cross-slot fetches, and explicit 429/retry
log evidence—not elapsed time alone.

## Runtime Filter Ablation

The split-sample ablation used the frozen 56-name `11.70` universe, next-open
fills, the normal 2x ATR stop, and death-cross exits. Earnings was unavailable
historically and therefore omitted exactly as the runtime's fail-open behavior
would do. Results are fixed-risk R multiples and remain survivor-selected and
outlier-concentrated.

Reproduction command (uses the frozen universe and cached delayed-SIP bars):

```bash
./venv/bin/python scripts/sma_entry_quality_audit.py \
  --feed sip --end 2026-09-04 --filter-ablation
```

| Variant | Development N / mean R | Held-out N / mean R |
|---|---:|---:|
| Raw 20/50 crossover | 761 / +0.44R | 497 / +2.51R |
| Regime only | 506 / +0.53R | 417 / +2.02R |
| Stock>SMA200 only | 462 / +0.46R | 347 / +3.18R |
| Volume expansion only | 351 / +0.44R | 238 / +2.76R |
| Regime + stock>SMA200 | 349 / +0.38R | 286 / +2.65R |
| Production gates except earnings | 172 / +0.18R | 149 / +3.56R |

These values were reproduced on 2026-09-29 with the command above. The script
reported no cached bars in one or more periods for SNDK, ECG, and DASH and
prints that coverage on every run. The reproduced cache-manifest SHA-256 was
`235aa2c9b09abee5f27adcbf2fd26eb00541ca132ca478216a2f47fd7eaf4763`;
a later run with changed cache bytes therefore identifies itself as different
evidence rather than silently presenting as the same snapshot.

No gate shows a consistent incremental improvement across both periods. The
held-out means are dominated by the same extreme runner documented in `11.70`,
and today's 56-name universe replayed backward is not a point-in-time universe.
These results do not authorize removing runtime gates, but they do invalidate
claims that the gates are already proven to improve entry quality.

Runtime disposition:

- retain stock>SMA200 while the v3 pool cohort is introduced; its structural
  thesis is plausible and held-out direction is favorable, but call it
  unvalidated rather than proven;
- retain volume expansion unchanged so the universe refresh does not also
  change entry behavior; evaluate it later in a separately pre-registered
  fixed-universe experiment;
- retain the regime allow-list as a risk policy, not a demonstrated alpha
  filter; its held-out rejected group performed unusually well;
- retain the two-day pre-earnings blackout as gap-risk protection, while
  disclosing that historical point-in-time coverage is unavailable and runtime
  provider failure opens the gate; and
- keep sector COLD in warning-only mode. It records context but does not block
  SMA entries and must not be described as an active veto.

## Promotion Procedure

1. Implement a report-only v3 selector with focused unit tests.
2. Generate completed-session delayed-SIP 25/50/100/200 comparisons with the
   corrected fundamentals parser.
   Required provider errors must be cleared or explicitly resolved on a stable
   rerun; a transient fail-closed lookup cannot silently shift the boundary.
   **Complete:** the bounded-retry rerun cleared all required-provider errors.
3. Review exclusions, provider uncertainty, share classes, corporate actions,
   sector output, risk coverage, and runtime capacity.
4. Generate a private report against the real trade ledger. Fail closed if it
   cannot be read, append every required lifecycle-protection member outside
   the ranked 100, and separately reconcile unresolved SMA entry orders.
   **Reconciled:** lifecycle protection is required for promotion and no
   unresolved SMA entry lifecycle order was found; private account details are
   intentionally omitted here.
5. Obtain explicit operator approval before changing `SMA_WATCHLIST`.
   **Complete:** approval recorded and the v3 cohort promoted 2026-09-28.
6. Update configuration, this document, the SMA deployment guide,
   `strategies.md`, allocation-risk documentation, and `PLAN.md`; run focused
   and full tests.
7. Recycle only with `./recycle_bot.sh`, then verify startup ownership,
   protective stops, ranked count, cycle duration, and request/error telemetry.
   **Complete for startup:** NORMAL ownership, intact stops, correct ranked and
   protected counts, and an error-free closed-market first cycle. Record warmed
   processing latency during the next market-open cycle.
8. Treat the promoted list as a new strategy-config-hash paper cohort. Do not
   pool pre- and post-refresh outcomes as one exact configuration.
9. Segment Donchian and RSI discussion at the same promotion date because
   cross-strategy ownership changed their effective admission conditions; use
   pair-level conflict telemetry to quantify the effect.

Refresh quarterly or after a material tradability, data-quality, corporate
action, or persistent opportunity-starvation event. Do not rotate the pool for
ordinary one-scan liquidity drift.

`DOCN` remains appended only for lifecycle protection. Removing it after the
position is flat and terminal changes SMA's full watchlist payload and thus its
configuration hash. Record that boundary explicitly; until a reviewed
cross-hash compatibility manifest exists, do not silently pool the pre- and
post-removal cohorts.

## Limitations

- Current-universe pool comparisons are survivorship-biased.
- The filter ablation does not reconstruct historical membership or
  point-in-time fundamentals and earnings.
- Raw signal counts do not model shared capital, ongoing position occupancy,
  or which candidate wins deterministic evaluation order.
- Backtests are supporting context only. The promoted exact-config paper cohort
  remains the authority for strategy evaluation.
