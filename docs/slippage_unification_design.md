# Slippage Unification Design

Status: Phase 1 and Phase 2 + 4 merged; Phase 3 pending. See
`docs/slippage_unification_tracker.md` for as-built commit history.
Updated: 2026-06-17

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

> **Reader note:** the audit subsections below describe the state of
> the bot *before* the unification shipped (pre-2026-06-05). They
> motivated the design and are retained for context. For "what the
> bot does today" read the §Codepath Coverage Matrix below and
> `docs/slippage_unification_tracker.md`.


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
- `stop_trigger_price REAL`

Column semantics:

- `slippage_benchmark_price`
  - the exact price used for the slippage calculation on this row
- `slippage_benchmark_kind`
  - `arrival_midpoint`
  - `decision_price`
  - `fallback_latest_close`
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
- `stop_trigger_price`
  - the actual active stop threshold that released the stop-market order

### Legacy column treatment

Three existing columns are kept on the schema for historical-row
readability:

- `entry_reference_price`
- `modeled_slippage_bps`
- `realized_slippage_bps`

Their role as of Phase 2 + 4 (PR #67, merged 2026-06-17):

- `entry_reference_price`
  - remains the strategy/entry context price for P&L and context
  - no longer treated as the slippage benchmark unless the row explicitly says
    `slippage_benchmark_kind='decision_price'`
- `modeled_slippage_bps`
  - **historical only.** New rows write `NULL`. No reader consults
    this column anywhere in the codebase. Phase 3 will null out
    populated historical rows where the original benchmark was
    bad provenance (pre-`32e21c2` recovered-context rows and
    pre-`8316e64` LIMIT rows).
- `realized_slippage_bps`
  - **historical only.** Same status as `modeled_slippage_bps`.
    Phase 1 (`bf16b5a`) wrote it in parallel with
    `slippage_signed_bps`; Phase 2 + 4 (`0b0dfee`) removed the
    dual-write so every new row is `NULL`.

The Phase 1 strategy of "keep both columns populated while consumers
migrate" was deliberately compressed into a single follow-up PR
(Phase 2 + 4) so the column families never live in a "some consumers
on old, some on new" state.

## Codepath Coverage Matrix

> **Status:** the per-codepath contracts below are implemented as
> specified. Each codepath is pinned by a test in `tests/test_reporting.py`
> and `tests/test_engine.py`; the writer-side implementation lives
> in `reporting/logger.py` and the call-site tagging in
> `engine/trader.py`. See `docs/slippage_unification_tracker.md`
> for the per-codepath status table.


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

Fallback:

- if arrival midpoint unavailable, write:
  - `slippage_benchmark_kind='fallback_latest_close'`
  - `slippage_measurement_quality='fallback'`
  - the signed/adverse values against that fallback benchmark

This keeps the row queryable and trivially filterable.

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

### 4. WebSocket protective stop fill

Path:

- `_process_stream_stop_fills(...)` → `trade_logger.log_stop_fill(...)`

Must write:

- `stop_trigger_price` = active stop that actually triggered
- `slippage_benchmark_kind='active_stop_price'`
- `slippage_measurement_quality='primary'`
- `slippage_signed_bps` = fill vs active stop
- `slippage_adverse_bps`

Critical implementation rule:

- `log_stop_fill(...)` must accept `stop_price: float | None`
- on the WebSocket path, populate it from `update.order.stop_price`
- if the broker stop price is unavailable, write `NULL` slippage with
  `slippage_benchmark_kind='unavailable'` and
  `slippage_measurement_quality='unavailable'`

### 5. Broker-history recovered stop fill

Path:

- `_find_recent_filled_stop_order(...)` / `_record_recovered_stop_fill(...)`

Must write:

- same benchmark fields as normal stop fill
- `slippage_measurement_quality='recovered'`
- broker `filled_at` timestamp override

Implementation rule:

- populate the benchmark directly from the recovered broker order object's
  `stop_price`

If the broker stop price is unavailable:

- write no slippage metric
- do not synthesize from entry price or any reconstructed guess

### 6. Standalone repair-stop fill

Path:

- missing protective stop repaired via `place_protective_stop(...)`, later filled

Must write exactly the same shape as any other stop fill, using the filled stop
order's own broker `stop_price` as the benchmark.

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

Keep the short-leg row as the economic row; long-leg rows should carry `NULL`
slippage fields rather than structural zeros when possible.

### 12. Single-leg external close

Path:

- disappearance confirmed by reconcile path → `log_external_close(...)`

Must write:

- `slippage_benchmark_kind='unavailable'`
- `slippage_measurement_quality='unavailable'`
- slippage fields as `NULL`

This path must never invent slippage from stale entry or current market data.

### 13. Spread external close

Path:

- spread disappears and is reconciled externally

Must write:

- the same `unavailable` slippage contract as single-leg external close on the
  generated close rows

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

The current implementation should prefer stateless broker truth over mutable DB
state for stop-fill benchmarking.

Phase-1 requirement:

- when a stop fill is logged, the benchmark should come directly from the
  broker order object that actually filled:
  - `update.order.stop_price` on the WebSocket path
  - `ClosedOrderInfo.stop_price` on the broker-history recovery path

Why this is better:

- it matches the actual order that filled,
- it naturally handles trailed/repaired stops,
- it avoids race conditions between stop replacement and DB state updates,
- and it keeps the design aligned with the append-only preference.

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
To reduce operator churn, phase 1 can keep the legacy label with a caption or
briefly show both labels during the consumer-migration release.

## Migration Plan

Implementation status as of 2026-06-17: Phase 1 and Phase 2 + 4 are
merged. See `docs/slippage_unification_tracker.md` for as-built
details and commit shas.

### Phase 1 — Additive schema and dual-write ✅ MERGED (PR #43, `bf16b5a`, 2026-06-05)

- added the new slippage columns to `trades`
- kept writing legacy slippage columns for backward compatibility
- dual-wrote:
  - `realized_slippage_bps` mirrored `slippage_signed_bps`
  - `modeled_slippage_bps` mirrored the modeled/control value
- passive MLEG legs wrote `NULL` on the new columns; legacy long-leg
  rows retained their `0.0` structural value to avoid breaking
  consumers using `SUM(...)` / `AVG(...)` over the legacy columns
  until those consumers migrated.

### Phase 2 + 4 — Consumer migration and legacy dual-write removal ✅ MERGED (PR #67, `0b0dfee`, 2026-06-17)

Folded the original Phase 4 (legacy deprecation) into Phase 2 so the
column families never live in a "consumers split between old and new"
state.

- health assessor (`strategies/health/assessor.py:_slippage_p95_bps`)
  reads `slippage_adverse_bps` with a positive
  `slippage_measurement_quality IN ('primary','fallback')` whitelist
- calibration script (`scripts/calibrate_health_thresholds.py`)
  mirrors the assessor query
- risk kill switch (`risk/manager.py::record_fill_slippage`) parameter
  renamed `realized_bps` → `adverse_bps`; engine was already clamping
  to adverse before calling
- dashboard Recent Trades surfaces `slippage_benchmark_kind` +
  `slippage_measurement_quality` alongside the bps value
- dashboard strategy stats (`compute_strategy_stats`) reads
  `slippage_adverse_bps` with the same whitelist; numerator +
  denominator gated on the same `.notna()` mask for both single-leg
  and MLEG branches (denominator dilution fix); MLEG long-leg
  `avg_fill_price > 0` workaround removed since the unified column's
  NULL value is the structural-zero signal; column renamed
  `Avg Adverse Slippage Bps`
- `reporting/pnl.py` weekly / daily / `slippage_report` use the new
  `_adverse_bps()` helper that returns `Optional[float]` and applies
  the quality whitelist
- forward-test reconciliation (`backtest/reconcile.py`) slippage
  go/no-go gate migrated to the new column + whitelist (review
  follow-up — pre-fix it read the retired legacy column and silently
  fell back to zero on every post-Phase-2 row)
- legacy `modeled_slippage_bps` / `realized_slippage_bps` writes
  removed across `build_record`, `build_close_record`,
  `log_stop_fill`, `log_spread_fill`. New rows write NULL on both
  legacy columns
- Phase 1 market-entry-without-`modeled_price` divergence reconciled:
  legacy column no longer falls back to
  `decision.entry_reference_price`
- hot-path log line (`reporting/logger.py`) switched from
  `slip=Nonebps` (always NULL post-Phase-4) to
  `slip_signed=... kind=... quality=...` from the new taxonomy

### Phase 3 — Historical cleanup ⬜ pending

- null or annotate known-bad legacy recovery rows using a deterministic
  migration predicate:
  - `reason LIKE '%recovered entry context%'`
  - `realized_slippage_bps IS NOT NULL`
  - `timestamp < '2026-06-02T18:20:37+00:00'` (pre-`32e21c2`)
- null or annotate pre-LIMIT-carve-out rows using:
  - `order_type = 'limit'`
  - `realized_slippage_bps IS NOT NULL`
  - `timestamp < '2026-06-02T23:31:45+00:00'` (pre-`8316e64`)
- comparison relies on ISO-8601 timestamp ordering; the migration script
  should verify the stored suffix format matches `TradeRecord.timestamp`
  before executing the cleanup query.
- backfill new slippage columns only where the benchmark is provably reconstructable
- do not force speculative backfills

### Phase 4 — Legacy deprecation ✅ FOLDED INTO PHASE 2

Originally planned as "mark legacy columns compatibility-only, then
eventually stop using them." Implemented in PR #67 as a clean drop:
every Phase 2 consumer landed reading the new columns directly, and
the legacy dual-writes were removed in the same PR rather than living
on as a deprecated read path. The legacy columns remain on the schema
to keep historical rows readable; Phase 3 cleans those up.

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

1. Do we want to surface both signed and adverse execution slippage in the
   dashboard, or keep signed only in the audit views?
2. For stop orders, do we accept `active_stop_price` benchmarking first and
   defer any separate trigger-time market benchmark until quote-capture support
   exists?

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
