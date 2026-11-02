# Dashboard Information Architecture

**Status:** Phase 1 navigation and page ownership implemented. Market-observation
collection contracts are deliberately deferred.

## Goal

Keep the operator's first view short and decision-oriented while giving market
research room to grow. The dashboard is a read-only projection of existing
broker, engine, trade-ledger, and report state. It is not an alternate engine or
an order-control surface.

## Page ownership

| Page | Primary question | Contents |
|---|---|---|
| Overview | Does anything need attention now? | Session/status strip, attention items, exposure, compact account curve, largest positions, market summary, latest activity |
| Positions & Risk | What do we own and how much risk is deployed? | Open positions, lifecycle identity, sleeve usage, multi-leg economics, drawdown, capital pools, sector exposure |
| Strategies & Performance | How are the sleeves behaving? | Performance curve, realized statistics, per-strategy results, Health & Edge reports, watchlists |
| Market Observatory | What is the surrounding market doing? | Regime, sector heat, leveraged-fund trend state, future pre-open pulse, future earnings clusters |
| Activity & Execution | What happened and how was it executed? | Recent trade/activity records and benchmarked slippage provenance |

The same metric may be summarized on Overview and explored on its owning page,
but full primary tables should have only one owner. This avoids competing views
of positions, P&L, or strategy state.

## Source and refresh boundaries

| Source | Authority | Current dashboard use |
|---|---|---|
| Alpaca broker reads | Current account, positions, and market clock | Status, exposure, marks, account curve |
| `data/engine_state.json` | Last engine-cycle snapshot | Runtime freshness, regime, allocator and risk state |
| Paper/live SQLite trade log | Durable trade, lifecycle, and execution evidence | Position identity, realized performance, activity, slippage |
| `data/health_reports/` | Generated advisory assessment | Strategy Health & Edge panel |

Operator and Market Observatory pages refresh every 30 seconds, while strategy
evidence refreshes every 60 seconds. Existing function-level caches remain the
guard against unnecessary broker or file reads, including slower market research
loads. There is no global sleep and no whole-dashboard timed rerun.

## Market Observatory: current scope

Phase 1 moves existing market-context views into one owner:

- market regime and sector conditions;
- the Leveraged Fund Trend Monitor for SPY/SPXL, QQQ/TQQQ, XLK/TECL, and
  SOXX/SOXL; and
- labeled placeholders for Pre-Open Pulse and Earnings Clusters.

The placeholders intentionally make no network calls and persist no synthetic
history. They prevent the navigation redesign from silently defining a research
contract.

## Deferred observation contracts

### Pre-Open Pulse

The initial configured universe is SPY, QQQ, XLK, and SOXX. The proposed
versioned contract is documented in `docs/preopen_observation_contract.md` and
keeps the collector reusable for additional US stocks and ETFs. It defines:

- the exact observation timestamp and exchange calendar;
- reference price (normally the prior regular-session close);
- source/feed and entitlement provenance;
- quote or trade timestamp, bid/ask/last semantics, and staleness;
- missing/degraded states; and
- an append-only observation identity that distinguishes an as-observed record
  from any later delayed-SIP enrichment; and
- a prospective `universe_version` so adding or removing monitored instruments
  never rewrites historical coverage.

The paper-stage design may use free real-time IEX for the available-now view and
delayed SIP for later consolidated enrichment. The UI must label those views and
must never present IEX as consolidated SIP/NBBO truth.

No collector or persistence implementation is authorized until that contract is
reviewed and frozen.

### Earnings Clusters

The event ledger, point-in-time classifications, correction policy, coverage,
and contamination rules are specified as open work in
`docs/cluster_earnings_reaction_research.md`. The dashboard placeholder does not
authorize event backfills or automated classification.

## Architecture for later collection

When an observation contract is approved:

1. acquire external market data through a focused module under `data/`;
2. persist versioned, append-only observations through `reporting/` into a
   store separate from the trade ledger;
3. use scheduling that can meet the contract's checkpoint deadlines without
   blocking or depending on the duration of a full strategy scan. The engine's
   existing `post_cycle_hook` is a single callback, not an assumed scheduler for
   this work; if it is used, add a composite wrapper rather than replacing
   `HealthReviewScheduler`. A separate lightweight process remains acceptable
   if deadline tests show the cycle hook is insufficient; and
4. have the dashboard read the persisted observation model, not call strategy
   code or infer historical state from current quotes.

Collection failures must be visible but non-gating: they cannot raise into the
trading cycle or change an order decision.

## Observation-to-action boundary

No current dashboard observation is a strategy input. Promotion from observation
to action is a separate project requiring a stable data contract, coverage and
failure analysis, prospective evidence, an explicit strategy/risk consumer, and
tests proving unavailable or stale data fails according to the reviewed policy.
That later work may reuse persisted observation data; it must not make the
Streamlit dashboard part of the trading path.
