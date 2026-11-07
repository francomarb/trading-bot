# Dashboard Information Architecture

**Status:** Phase 1 navigation and page ownership implemented. The frozen
pre-open v0.2 collector/store and read-only Market Observatory projection are
implemented.

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
| Market Observatory | What is the surrounding market doing? | Regime, sector heat, leveraged-fund trend state, delayed-SIP pre-open pulse, future earnings clusters |
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
| `data/market_observations.db` | Versioned delayed-SIP observations, collection attempts, and coverage jobs | Pre-Open Pulse coverage and finalized observation projection; never trading or order authority |
| `data/health_reports/` | Generated advisory assessment | Strategy Health & Edge panel |

Operator and Market Observatory pages refresh every 30 seconds, while strategy
evidence refreshes every 60 seconds. Existing function-level caches remain the
guard against unnecessary broker or file reads, including slower market research
loads. There is no global sleep and no whole-dashboard timed rerun.

Market Observatory adds a second task-level selector for **Pre-Open**, **Trend
& Regime**, **Sector Breadth**, and **Earnings Clusters**. It uses conditional
routing rather than eagerly rendered tabs: only the selected tool executes.
This keeps the page short and prevents unrelated observation readers or market
research loads from running in the background.

## Market Observatory: current scope

Market Observatory owns these independently routed tools:

- market regime and sector conditions;
- the Leveraged Fund Trend Monitor for SPY/SPXL, QQQ/TQQQ, XLK/TECL, and
  SOXX/SOXL;
- the read-only delayed-SIP Pre-Open Pulse; and
- a labeled placeholder for Earnings Clusters.

The Pre-Open Pulse opens the observation database in SQLite read-only mode and
projects only finalized records; it makes no network calls and never invokes
collector or strategy code. Earnings Clusters still persists no history. This
keeps the dashboard from becoming a collection or trading boundary.

## Observation contracts

### Pre-Open Pulse

The initial configured universe is SPY, QQQ, XLK, and SOXX. The proposed
versioned contract is documented in `docs/preopen_observation_contract.md` and
keeps the collector reusable for additional US stocks and ETFs. It defines:

- the exact observation timestamp and exchange calendar;
- reference price (normally the prior regular-session close);
- source/feed and entitlement provenance;
- quote or trade timestamp, bid/ask/last semantics, and staleness;
- missing/degraded states;
- a delayed historical-SIP checkpoint record with separate checkpoint,
  availability, and collection times;
- a prospective `universe_version` so adding or removing monitored instruments
  never rewrites historical coverage.

The paper-stage proposal uses delayed historical SIP as the sole primary source.
It reconstructs a consolidated point-in-time checkpoint after the free-feed
delay; it is not an available-at-checkpoint live snapshot. IEX may later be
stored as separate diagnostic evidence, but cannot substitute for SIP or affect
primary coverage. The UI must disclose checkpoint, availability, collection,
feed, and freshness independently.

The collector, separate store, composite post-cycle scheduler, and dashboard
reader implement the frozen contract. The reader uses durable jobs only for the
coverage denominator, selects the latest finalized revision of each logical
key, and never recomputes provider history.

### Earnings Clusters

The frozen v0.1 event-ledger, point-in-time classification, reaction-window,
correction, coverage, and contamination contract is specified in
`docs/earnings_cluster_observation_contract.md`. The dashboard remains a
placeholder. The contract does not authorize ledger implementation, event
backfills, automated classification, scoring, ranking, or strategy use.

## Collection architecture

The pre-open implementation follows these boundaries; future observation
contracts must do the same:

1. acquire external market data through a focused module under `data/`;
2. persist versioned, append-only observations through `reporting/` into a
   store separate from the trade ledger;
3. use a composite `post_cycle_hook` that preserves `HealthReviewScheduler`,
   performs only due delayed-SIP work, makes no in-hook sleeps, and bounds each
   invocation. Historical point-in-time queries tolerate a late local runtime;
   a separate lightweight process remains acceptable if measured cycle impact
   exceeds the reviewed bound; and
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
