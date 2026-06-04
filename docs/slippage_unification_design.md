# Slippage Unification Design

Status: Review draft, not implemented
Updated: 2026-06-04

## Goal

Define one coherent slippage model for the bot so that:

- execution-quality metrics are measured consistently,
- risk controls consume the right slippage signal,
- dashboard/operator views are auditable from stored data,
- recovery/reconciliation rows do not pollute execution-quality analysis,
- future strategies do not invent their own slippage semantics.

This document is intentionally design-first. It reflects the current bot, the
latest slippage-related fixes already merged, and the gaps still visible in the
database and dashboard today.

## Why This Review Was Needed

The bot currently uses the word "slippage" for multiple different concepts:

- execution quality vs arrival price,
- implementation shortfall vs decision price,
- stop-gap erosion vs the intended stop level,
- combo slippage vs submitted spread limit,
- adverse-only drift for kill-switch logic,
- signed price-improvement numbers for audit.

Those are all legitimate metrics, but they are not interchangeable. Right now
some of them share the same columns, some are derived from different benchmark
prices, and some are not auditable from the trade row that the dashboard shows.

The result is exactly what surfaced in the dashboard audit:

- `Recent Trades` can show correct signed slippage values that cannot be
  verified from the visible stored prices.
- historical recovery rows can still contain legacy phantom slippage.
- strategy-level average slippage can look "better" than recent trades because
  it is averaging a different population of rows.
- stop-fill slippage is currently closer to "gap through stop" than true broker
  execution quality.

## Relevant Recent Commits

These are the key recent slippage-related changes already in the repo:

- `32e21c2` Fix two L2 slippage measurement defects
  - fixed phantom recovery-row slippage
  - moved equity market-entry slippage to arrival-price style benchmarking
- `8316e64` Apply reviewer P2 fixes: calibration filter + LIMIT carve-out
  - excluded LIMIT orders from arrival-price slippage
- `49becd9` Verify options coverage + skip stock-quote fetch for OCC symbols
  - confirmed single-leg options LIMIT paths do not record arrival-price slippage
- `2425839` Switch L2 slippage metric to adverse-only semantics
  - health, calibration, and kill switch now clamp price improvement to zero
- `d2d9509` Preserve broker timestamps in recovery paths
  - recovered rows now use Alpaca `filled_at` where available

These changes improved correctness materially, but they did not fully unify the
metric model or the persistence contract.

## Current-State Audit

### 1. Single-leg market entries

Current behavior:

- The engine captures `slippage_ref = arrival midpoint` when available, else
  falls back to `latest_close`.
- `realized_slippage_bps` is computed against that `slippage_ref`.
- The trade row stores `entry_reference_price = decision.entry_reference_price`,
  not the actual slippage benchmark.

Implication:

- the slippage number may be correct,
- but the trade row is not self-auditable because the benchmark used for the
  calculation is not persisted as its own field.

### 2. Single-leg limit entries

Current behavior:

- `build_record(...)` writes `NULL` for both slippage columns on LIMIT entries.
- this is deliberate and correct for the current scope.

Implication:

- no fake arrival-price slippage is recorded for passive fills,
- but no explicit separate "limit execution quality" metric exists either.

### 3. Single-leg discretionary market exits

Current behavior:

- exit slippage is computed against the modeled market-exit reference supplied
  by the engine at close time.
- the row again stores that benchmark in the overloaded
  `entry_reference_price` column.

Implication:

- the metric exists,
- but the column name no longer matches the semantic meaning.

### 4. Stop-triggered exits

Current behavior:

- `log_stop_fill(...)` computes `realized_slippage_bps` against
  `initial_stop_loss`.
- the row stores:
  - `avg_fill_price` = actual fill,
  - `entry_reference_price` = original entry reference,
  - `stop_price` = actual fill price on the stop row,
  - `initial_stop_loss` = original stop from the opening row.

Important consequence:

- this is not a pure execution-quality metric,
- it is effectively measuring fill vs stop level,
- and because the active stop can be repaired or trailed after entry, using
  `initial_stop_loss` can become the wrong benchmark for later fills.

This is the most important conceptual gap in the current model.

### 5. Recovery/reconciliation rows

Current behavior:

- recovered-entry-context rows now correctly write `NULL` slippage going
  forward.
- historical legacy rows may still contain phantom signed slippage from the
  pre-fix path.

Implication:

- health/calibration queries defensively filter them out,
- but operator-facing dashboard tables can still show them unless we clean or
  annotate legacy rows.

### 6. MLEG spreads

Current behavior:

- combo slippage is measured against the submitted combo limit on the economic
  short leg.
- the paired long-leg row carries `0.0`.

Implication:

- the strategy-level logic can mostly reconstruct the intended value,
- but row-level recent-trades display is noisy because two rows represent one
  combo event and one of them is a structural zero rather than "not
  applicable."

### 7. Dashboard and reporting consumers

Current behavior:

- `Recent Trades` renders raw signed `realized_slippage_bps` from the DB.
- `Strategy Realized P&L` average slippage is a weighted average over completed
  realized rows only, not all recent fills.
- the kill switch and health reports use adverse-only semantics.

Implication:

- the same word "slippage" currently refers to:
  - signed raw fill-level audit values,
  - adverse-only drift for controls,
  - and completed-trade weighted averages for realized reporting.

This is not necessarily wrong, but it must be explicit.

## External Best-Practice Review

### Transaction cost analysis and benchmark discipline

The broad industry pattern is:

- use a clearly defined benchmark price,
- keep execution-quality metrics separate from broader implementation shortfall,
- preserve price improvement rather than hiding it,
- and evaluate best execution with enough metadata to reconstruct what was
  measured.

Useful references:

- [CFA Institute — Trade Strategy and Execution](https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/trade-strategy-execution)
  - implementation shortfall is the standard for total trade-cost measurement
  - benchmark choice depends on the question being asked
- [QuestDB — Post-trade analysis overview](https://questdb.com/docs/cookbook/sql/finance/post-trade-overview/)
  - slippage is the difference between execution and a reference price
  - TCA is fundamentally "trade joined to market state at the relevant time"
- [QuestDB — Slippage per fill](https://questdb.com/docs/cookbook/sql/finance/slippage/)
  - per-fill slippage should be tied to the market state at execution
- [SEC — Trade Execution: What Every Investor Should Know](https://www.sec.gov/about/reports-publications/investorpubstradexec)
  - price improvement is a meaningful best-execution factor
- [SEC Rule 605 FAQ](https://www.sec.gov/rules-regulations/staff-guidance/trading-markets-frequently-asked-questions/frequently-asked-questions-rule-605-regulation-nms)
  - execution-quality reporting is benchmark-sensitive and should use the
    appropriate executable quote context

### Practical interpretation for this bot

The best-practice takeaway is not "one slippage metric for everything." It is:

1. one metric per question,
2. one explicit benchmark per metric,
3. one clear storage contract per fill type,
4. and clear separation between audit metrics and risk-control metrics.

## Design Principles

1. Do not overload one column with multiple semantic meanings.
2. Keep signed execution-quality metrics for audit and broker review.
3. Derive adverse-only metrics for risk controls from the signed metric; do not
   replace the signed value.
4. Distinguish execution quality from implementation shortfall.
5. Distinguish stop-gap erosion from execution quality.
6. Persist the actual benchmark used, not just the computed bps result.
7. Recovered rows must never fabricate slippage metrics from unrelated prices.
8. Append-only remains the preferred write pattern for fills and order events.

## Proposed Metric Taxonomy

### A. Execution slippage

Question answered:

- "How good was the actual fill relative to the market state when the order
  became executable?"

Examples:

- market entry vs arrival midpoint,
- market discretionary exit vs arrival midpoint,
- spread fill vs submitted combo limit.

Storage:

- keep signed value,
- derive adverse-only value for controls.

### B. Implementation shortfall

Question answered:

- "How much alpha decayed between strategy decision and actual fill?"

Examples:

- decision bar close vs later market entry fill,
- signal close vs next-open fill after overnight drift.

This is useful, but it is not the same as broker execution quality.

### C. Stop gap / stop shortfall

Question answered:

- "How far through my active stop level did I actually fill?"

This is a risk-protection erosion metric, not a broker-quality metric.

For stop orders this is often the operator-facing number that matters most.

### D. Limit surplus / passive fill quality

Question answered:

- "How favorable was the limit fill relative to the submitted limit?"

This is the right place for passive fills that are currently written as
slippage `NULL`.

## Proposed Persistence Model

### Recommendation

Keep the existing `trades` table as the source of truth and extend it with a
small set of explicit slippage fields. Do not introduce a separate metrics
table unless the simpler design proves insufficient later.

This keeps the fix:

- reviewable,
- easy to query,
- compatible with the current dashboard/reporting code,
- and much less likely to create another round of edge-case bugs.

### Minimal additive columns on `trades`

Add these columns:

- `slippage_benchmark_price REAL`
- `slippage_benchmark_kind TEXT`
- `slippage_benchmark_timestamp TEXT`
- `slippage_measurement_quality TEXT`
- `slippage_signed_bps REAL`
- `slippage_adverse_bps REAL`
- `slippage_expected_bps REAL`

Optional but strongly recommended for stop rows:

- `stop_trigger_price REAL`

Column semantics:

- `slippage_benchmark_price`
  - the exact price used for the slippage calculation on this row
- `slippage_benchmark_kind`
  - `arrival_midpoint`
  - `decision_price`
  - `active_stop_price`
  - `combo_limit`
  - `limit_price`
  - `unavailable`
- `slippage_benchmark_timestamp`
  - when the benchmark was observed or defined
- `slippage_measurement_quality`
  - `primary`
  - `fallback`
  - `recovered`
  - `unavailable`
- `slippage_signed_bps`
  - positive = adverse, negative = price improvement
- `slippage_adverse_bps`
  - `max(0, slippage_signed_bps)` for rows where slippage exists
- `slippage_expected_bps`
  - currently the modeled bps used by controls, e.g. `5.0` for market orders
- `stop_trigger_price`
  - the actual active stop threshold that released the stop-market order

### Legacy column treatment

Keep these existing columns for compatibility:

- `entry_reference_price`
- `modeled_slippage_bps`
- `realized_slippage_bps`

But redefine their role during migration:

- `entry_reference_price`
  - remains the strategy/entry context price for P&L and context
  - no longer treated as the slippage benchmark unless the row explicitly says
    `slippage_benchmark_kind='decision_price'`
- `modeled_slippage_bps`
  - legacy compatibility mirror of `slippage_expected_bps`
- `realized_slippage_bps`
  - legacy compatibility mirror of `slippage_signed_bps`

This gives us a clean path forward without breaking current consumers all at
once.

## Codepath Coverage Matrix

This is the key operational requirement: every codepath that can create a fill
row must set slippage fields deliberately rather than inheriting whatever
happens to be nearby.

### 1. Normal single-leg market entry

Path:

- `_process_symbol(...)` → `broker.place_order(...)` → `_record_fill(...)` /
  `_log_entry(...)`

Must write:

- `slippage_benchmark_kind='arrival_midpoint'` when quote available
- `slippage_measurement_quality='primary'`
- `slippage_signed_bps`
- `slippage_adverse_bps`
- `slippage_expected_bps`

Fallback:

- if arrival midpoint unavailable, either
  - write no slippage metric at all, or
  - write `slippage_benchmark_kind='decision_price'` with
    `slippage_measurement_quality='fallback'`

Preferred default: write the fallback explicitly so the row stays queryable,
but make it trivially filterable.

### 2. Normal single-leg limit entry

Path:

- same as above, but `decision.order_type='limit'`

Must write:

- no execution slippage
- `slippage_benchmark_kind='limit_price'`
- `slippage_measurement_quality='unavailable'`
- slippage fields as `NULL`

This preserves the current LIMIT carve-out without pretending the metric exists.

### 3. Normal discretionary market exit

Path:

- `close_position(...)` → `build_close_record(...)`

Must write:

- `slippage_benchmark_kind='arrival_midpoint'`
- `slippage_measurement_quality='primary'` or `fallback`
- `slippage_signed_bps`
- `slippage_adverse_bps`
- `slippage_expected_bps`

### 4. WebSocket protective stop fill

Path:

- `_process_stream_stop_fills(...)` → `trade_logger.log_stop_fill(...)`

Must write:

- `stop_trigger_price` = active stop that actually triggered
- `slippage_benchmark_kind='active_stop_price'`
- `slippage_measurement_quality='primary'`
- `slippage_signed_bps` = fill vs active stop
- `slippage_adverse_bps`
- `slippage_expected_bps`

### 5. Broker-history recovered stop fill

Path:

- `_find_recent_filled_stop_order(...)` / `_record_recovered_stop_fill(...)`

Must write:

- same benchmark fields as normal stop fill
- `slippage_measurement_quality='recovered'`
- broker `filled_at` timestamp override

If the active stop price cannot be reconstructed honestly:

- write no slippage metric
- do not synthesize from entry price or current stop guess

### 6. Standalone repair-stop fill

Path:

- missing protective stop repaired via `place_protective_stop(...)`, later filled

Must write exactly the same shape as any other stop fill.

This codepath is historically high risk and must not be special-cased.

### 7. Fractional residual cleanup exit

Path:

- post-stop/manual cleanup of fractional residual share

Must write:

- either a true market-exit slippage benchmark if one is available at cleanup
  time,
- or `NULL` slippage with `slippage_measurement_quality='unavailable'`

It must not inherit the stop slippage of the main whole-share exit.

### 8. Recovered missing-entry-context row

Path:

- `_reconstruct_missing_entry_context(...)`

Must write:

- broker `filled_at` timestamp when available
- `slippage_measurement_quality='recovered'`
- all slippage fields `NULL`

This remains the correct contract unless the original pre-trade benchmark can
actually be recovered, which today it cannot.

### 9. Suspect-order recovery that later resolves filled

Path:

- `_recover_suspect_orders(...)`

Must write:

- the same slippage contract the original live path would have written,
- using the broker `filled_at` timestamp,
- provided the original benchmark captured at submission still exists in the
  recovery state

If the benchmark was not preserved:

- write `NULL`, not a synthetic value.

### 10. Async single-leg option fills

Path:

- `_drain_option_fills(...)`

Must write:

- current LIMIT contract for `spy_options_reversion`
- no arrival-price execution slippage

### 11. Spread entry / exit fills

Path:

- `log_spread_fill(...)`

Must write:

- `slippage_benchmark_kind='combo_limit'`
- `slippage_measurement_quality='primary'`
- `slippage_signed_bps`
- `slippage_adverse_bps`
- `slippage_expected_bps=0.0` unless we later define a modeled combo threshold

Keep the short-leg row as the economic row; long-leg rows should carry `NULL`
slippage fields rather than structural zeros when possible.

## Proposed Fill-Type Contract

### 1. Equity single-leg market entry

Persist:

- `execution_slippage`
  - benchmark: arrival midpoint
  - signed/adverse/expected
- `implementation_shortfall`
  - benchmark: decision price

If arrival midpoint is unavailable:

- either do not write `execution_slippage`,
- or write it with `benchmark_kind=fallback_latest_close` and
  `measurement_quality=fallback`.

The key rule is that fallback rows must be explicitly tagged and easily
filterable.

### 2. Equity single-leg limit entry

Persist:

- no `execution_slippage` vs arrival price
- optional `limit_surplus` vs submitted limit
- optional `implementation_shortfall` if desired

### 3. Discretionary market exit

Persist:

- `execution_slippage`
  - benchmark: arrival midpoint at close submission
- optional `implementation_shortfall`
  - benchmark: strategy exit decision price if we want that family

### 4. Stop-triggered exit

Persist two separate metrics:

- one stored slippage metric:
  - benchmark: active stop price that actually released the order
  - this is the operator-critical stop-gap measurement
- optional future second metric:
  - benchmark: trigger-time arrival/market benchmark, only if we can capture it
    honestly later

Important design decision:

If the bot cannot observe a trustworthy trigger-time quote benchmark, it should
still write the active-stop slippage metric, and leave any separate
execution-quality stop metric absent rather than pretending they are the same
thing.

### 5. Single-leg options LIMIT entry

Persist:

- no arrival-price execution slippage
- optional `limit_surplus`

### 6. MLEG spread entry/exit

Persist:

- one `combo_limit_execution` metric per combo event
- benchmark: submitted net combo limit
- signed/adverse values on the economic spread event, not duplicated as zeros on
  non-economic leg rows

Recommended representation:

- keep the fill rows as they are for audit,
- but set passive-leg slippage fields to `NULL` rather than `0.0` when the
  schema migration is in place.

### 7. Recovery/reconciliation rows

Persist:

- no execution slippage unless the original benchmark is truly recoverable,
- broker `filled_at` timestamps when available,
- explicit `measurement_quality=recovered` on any recovered metric.

Default rule:

- recovered fills may recover timestamps and order ids,
- they must not invent slippage benchmarks.

## Stop Lifecycle Requirement

The current fill-only trade table is not enough to guarantee correct stop-gap
measurement once stops are repaired or trailed.

Minimal requirement:

- whenever a protective stop is submitted or replaced, the latest open-position
  context in the DB must reflect the active stop price that would be used if
  the stop filled next.

Preferred lightweight implementation:

- extend the existing open-position context logic so stop submit/replace paths
  update the current active stop reference in a durable way,
- rather than adding a full new event table immediately.

If later audits show we need a complete stop-event ledger, we can add it then.
It should not be phase 1 of this fix.

## How Risk / Health / Dashboard Should Consume The New Model

### Risk kill switch

Use:

- `slippage_adverse_bps`

Do not use:

- signed slippage directly,
- implementation shortfall,
- stop-gap erosion.

### Health reports and calibration

Use:

- the same `slippage_adverse_bps` family,
- with explicit filters on `measurement_quality`.

### Dashboard Recent Trades

Show, at minimum:

- `Slippage Bps`
- `Benchmark`
- `Benchmark Kind`
- `Quality`

Optional:

- the section does not need a dozen new columns; it just needs enough context
  to make the one slippage number auditable.

### Dashboard Strategy Realized P&L

Rename and define clearly:

- `Avg Adverse Slippage Bps`
  - if the operator wants a risk-oriented number

Optionally also add:

- `Avg Signed Slippage Bps`
  - if we want pure TCA-style reporting

The current ambiguity should be removed.

## Migration Plan

### Phase 1 — Additive schema and dual-write

- add the new slippage columns to `trades`
- keep writing legacy slippage columns for backward compatibility
- start dual-writing:
  - `realized_slippage_bps` mirrors `slippage_signed_bps`
  - `modeled_slippage_bps` mirrors `slippage_expected_bps`

### Phase 2 — Consumer migration

- move health assessor to `slippage_adverse_bps`
- move calibration script to `slippage_adverse_bps`
- move dashboard recent-trades display to the new benchmark fields
- move strategy-level average slippage to an explicit chosen field

### Phase 3 — Historical cleanup

- null or annotate known-bad legacy recovery rows
- backfill new slippage columns only where the benchmark is provably reconstructable
- do not force speculative backfills

### Phase 4 — Legacy deprecation

- mark legacy `modeled_slippage_bps` / `realized_slippage_bps` columns as
  compatibility-only
- eventually stop using them as the source of truth

## Specific Current Issues This Design Resolves

1. `Recent Trades` can no longer show an unverifiable slippage number without
   also showing its benchmark.
2. Strategy-level slippage averages can be defined explicitly as either signed
   or adverse-only.
3. Recovery rows stop contaminating execution-quality analytics.
4. Stop-triggered fills stop pretending that stop-gap erosion and broker
   execution quality are the same metric.
5. MLEG leg rows stop carrying structural `0.0` values that look like real
   measurements.

## Recommended Implementation Decision

Implement the final solution as:

- a minimal additive extension of the existing `trades` table,
- a strict per-codepath write contract,
- explicit benchmark fields,
- explicit signed vs adverse slippage fields,
- and lightweight durable active-stop tracking in the existing open-position
  context.

Do not add a separate metrics table or stop-event ledger in phase 1. The
problem is real, but the smallest solid fix is still enough here.

## Open Review Questions

These should be answered before implementation starts:

1. For market-entry rows where arrival midpoint is unavailable, do we:
   - write a tagged fallback metric, or
   - write no execution-slippage metric at all?
2. Do we want to surface both signed and adverse execution slippage in the
   dashboard, or keep signed only in the audit views?
3. For LIMIT orders, do we want a first-class `limit_surplus` metric now, or
   keep them metric-silent outside realized P&L?
4. For stop orders, do we accept `stop_gap` first and defer true
   trigger-time `execution_slippage` until quote-capture support exists?

## Recommendation Summary

Use three clearly separated concepts going forward:

- execution-quality slippage for fill benchmarking,
- implementation shortfall for signal/decision decay,
- stop-gap slippage for protective-stop erosion.

But keep the implementation small:

- persist the exact benchmark on each trade row,
- keep signed values for audit,
- derive adverse-only values for controls,
- and make every fill/recovery/stop codepath write those fields deliberately.
