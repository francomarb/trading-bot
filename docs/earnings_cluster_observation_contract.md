# Earnings Clusters Observation Contract v0.1

**Status:** Frozen v0.1 contract with ledger, operator CLI, standalone collectors,
and read-only export implemented. Collection defaults off; operational setup,
prospective evidence, and dashboard presentation remain pending. No strategy
input, paper order, or trading authorization exists. See
[`earnings_observation_operations.md`](earnings_observation_operations.md).

**Contract version:** `earnings-cluster-observation-v0.1`

**Frozen:** 2026-11-07

**Related research:**
[`cluster_earnings_reaction_research.md`](cluster_earnings_reaction_research.md)

---

## 1. Purpose and safety boundary

The first Earnings Clusters deliverable is an auditable chronological ledger,
not a signal. It records what a registered company reported, which cluster
dimensions the report addressed, and how the reporter and registered candidates
reacted relative to a pre-registered benchmark after the relevant information
became public.

The v0.1 contract deliberately does **not** calculate:

- a bullish or bearish cluster score;
- a confidence score;
- an expected return;
- a candidate direction or rank;
- an option-implied move reconstructed from current data;
- an entry, exit, size, permission, or risk adjustment; or
- any input consumed by `TradingEngine`, a strategy, risk, or execution.

The dashboard is a read-only projection. It is never a collection, manual-entry,
or correction surface. Any later observation-to-action proposal requires a
separate reviewed consumer contract after stable prospective evidence exists.

## 2. Evidence layers and the first cohorts

Every event carries one immutable `registration_mode`:

| Mode | Definition | Evidence use |
|---|---|---|
| `prospective` | The event identity, issuer role, cluster memberships, expected timing, and required windows were registered before the release. | Eligible for prospective coverage and later research. |
| `retrospective_seed` | Any required registration occurred after the release or after its outcome was observable. | Schema/operations dry-run only; never pooled with prospective evidence. |

A source discovered solely to correct an earlier record has
`knowledge_mode=correction_only`. That describes the new source/revision, not
the event registration, and never changes a retrospective event to prospective.

The calendar-2026 Q3 reporting round may be loaded only as a bounded mixed pilot.
Events already released when this contract is merged are
`retrospective_seed`. A still-unreleased event may be `prospective` only if it
is registered before its release with a sufficiently precise issuer-sourced
schedule. The first fully prospective cohort is the next complete reporting
round registered before its first source event.

Fiscal quarters are issuer-specific. `cycle_id` groups a calendar reporting
round; `fiscal_period_label` preserves the issuer's own reported period. The
system never rewrites different fiscal calendars into a fictional common
quarter.

## 3. Versioned protocol identity

The protocol identity freezes these items together:

```text
contract_version
registry_version
cycle_id
reaction_window_version
classification_rubric_version
```

Changing an issuer, source edge, candidate edge, benchmark, required reaction
window, label vocabulary, or timing rule creates a new version effective only
for events not yet registered. Registration permanently binds an event to its
protocol and registry versions, even when the release has not occurred.
Historical event denominators and memberships remain attached to the versions
registered for that event.

A mid-cycle registry change therefore cannot re-register or duplicate an
existing event under the new version. New events may use the new version; an
incorrect existing registration remains auditable, receives a `data_quality`
flag and cited correction note, and cannot silently change its denominator,
membership, or frozen reaction jobs.

## 4. Frozen v0.1 registry

### 4.1 Registered issuers

Issuer identity is separate from ticker identity. Alphabet has one earnings
event even though it has two listed share classes; v0.1 uses `GOOGL` for market
reaction measurement and retains `GOOG` as an alias.

| `issuer_id` | Market symbol | Aliases | Registered role |
|---|---|---|---|
| `microsoft` | MSFT | — | hyperscaler / AI-capex source |
| `meta` | META | — | hyperscaler / AI-capex source |
| `alphabet` | GOOGL | GOOG | hyperscaler / AI-capex source |
| `amazon` | AMZN | — | hyperscaler / AI-capex source |
| `nvidia` | NVDA | — | accelerator / compute source |
| `amd` | AMD | — | accelerator / compute source |
| `broadcom` | AVGO | — | accelerator, custom-silicon, networking source |
| `tsmc` | TSM | — | semiconductor-manufacturing source; US ADR reaction |
| `asml` | ASML | — | semiconductor-manufacturing source; US ADR reaction |
| `micron` | MU | — | memory source |
| `apple` | AAPL | — | mega-cap context only |
| `tesla` | TSLA | — | mega-cap context only |

The following issuers are registered as `candidate_schedule_only` in v0.1:

| `issuer_id` | Market symbol | Initial cluster availability |
|---|---|---|
| `arista` | ANET | hyperscaler AI capex; accelerated compute |
| `marvell` | MRVL | hyperscaler AI capex; accelerated compute |
| `vertiv` | VRT | hyperscaler AI capex; accelerated compute |
| `eaton` | ETN | hyperscaler AI capex |
| `applied_materials` | AMAT | semiconductor manufacturing |
| `lam_research` | LRCX | semiconductor manufacturing |

Candidate-only events retain append-only schedules so the dashboard can show
whether a candidate had already reported at a historical cutoff. They do not
require fundamental labels or own-report reaction jobs and cannot contribute
cluster evidence in v0.1. Issuers that are both sources and candidates retain
their source role; before their release, their candidate availability may still
be shown.

Context-only events are visible but contribute to no AI-infrastructure cluster.
They cannot acquire relevance after their outcome without a new prospective
registry version.

### 4.2 Directed two-hop relationships

v0.1 registers explicit `source -> cluster` and `cluster -> candidate` edges.
It does not manufacture every source/candidate pair and assigns no numeric
weight. A later ranking study may propose pair-level edges only through a new
version.

| `cluster_id` | Registered sources | Registered observable candidates | Primary benchmark |
|---|---|---|---|
| `hyperscaler_ai_capex` | MSFT, META, GOOGL, AMZN | NVDA, AMD, AVGO, ANET, MRVL, VRT, ETN | QQQ |
| `accelerated_compute` | NVDA, AMD, AVGO | NVDA, AMD, AVGO, MU, ANET, MRVL, VRT | SOXX |
| `semiconductor_manufacturing` | TSM, ASML | TSM, ASML, NVDA, AMD, AVGO, AMAT, LRCX | SOXX |
| `memory` | MU | MU, NVDA, AMD, AVGO | SOXX |
| `mega_cap_context` | AAPL, TSLA | none | QQQ |

`SPY` is stored as a broad-market diagnostic for every event but is not a
substitute for the registered primary benchmark. Candidate membership means
only that the symbol's contemporaneous price is observed alongside a source
event and its report-status-at-cutoff is retained. It does not imply direction,
rank, or tradability.

## 5. Event registration and exact time semantics

All persisted timestamps are timezone-aware UTC. New-York exchange-local time
is derived with `America/New_York`; host-local time is never used to interpret
an issuer or exchange timestamp.

### 5.1 Append-only schedule knowledge

Each schedule statement is an immutable revision with:

- `known_at`;
- `scheduled_release_at` when exact;
- `schedule_precision`: `exact`, `before_open`, `after_close`, `date_only`, or
  `unknown`;
- issuer-source citation and access time;
- `supersedes_id` and correction reason where applicable; and
- the operator or collector that recorded it.

Only `exact`, `before_open`, or `after_close` issuer-sourced schedules can make
an unreleased event prospectively registered. `date_only` and third-party
calendar values remain visible but do not satisfy prospective timing coverage.

### 5.2 Actual release and call times

The event retains separately:

- `actual_release_at`;
- `actual_release_time_kind`: `issuer_exact`, `sec_acceptance_proxy`,
  `operator_bounded`, or `unknown`;
- `call_scheduled_at` and `call_started_at`, when issuer-sourced;
- `fundamental_frozen_at`; and
- each reaction window's `target_at`, `available_at`, and `collected_at`.

An SEC acceptance timestamp is provenance for a filing, not proof that the
issuer first released the information at that instant. It is labeled as a
proxy and cannot silently become `issuer_exact`.

### 5.3 Reaction session

The exchange calendar defines the first regular session eligible to measure a
standard response:

1. a release strictly after the immediately preceding scheduled regular close
   and at or before five minutes before the next open uses that next session;
2. a release timestamped exactly at the scheduled close, during the regular
   session, or inside the five-minute open buffer is `irregular_timing`; and
3. an unknown release time has no inferred standard session.

Irregular and unknown events remain in coverage. Their standard windows are
`unavailable` or explicitly contaminated; the system never guesses BMO/AMC
from a date alone.

Same-close events cannot consume one another's later reactions. Chronological
eligibility is timestamp-based, never date-based.

## 6. Source and provenance contract

### 6.1 Accepted sources

| Data | v0.1 source | Policy |
|---|---|---|
| Schedule | Issuer investor-relations announcement or event page | Manual registration with URL, publication/access time, and precision. |
| Reported results and guidance | Issuer release/presentation and SEC 8-K, 10-Q, 10-K, 6-K, or 20-F | Cite document identity; preserve disagreements. |
| Structured reported facts | SEC submissions/companyfacts where the filing taxonomy is comparable | Optional aid, never a substitute for reading cluster-relevant guidance. |
| Call commentary | Issuer-published transcript/material only | Optional. Webcast-only operator notes are separately labeled and cannot satisfy required source coverage. |
| Reporter/candidate/benchmark prices | Alpaca historical delayed SIP | Regular-session observations only, collected after entitlement delay. |
| Exchange sessions | Alpaca calendar adapter already used by the project | No inferred holidays or opens. |

Official references verified for this contract:

- [SEC EDGAR APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces)
  provide unauthenticated submissions and XBRL company facts; access must follow
  SEC fair-access policy.
- [SEC developer resources](https://www.sec.gov/about/developer-resources)
  specify declared automated access and a current aggregate ceiling of ten
  requests per second. An implementation must use a project-safe identifying
  user agent and a materially lower bounded rate.
- [Alpaca's market-data FAQ](https://docs.alpaca.markets/us/docs/market-data-faq)
  states that free historical SIP queries require an `end` at least 15 minutes
  old. v0.1 uses a 16-minute eligibility buffer, consistent with the existing
  pre-open contract.

### 6.2 Stored citation, not copied corpus

For every source retain source type, issuer, title, publication time, access
time, URL, SEC accession when applicable, and a content hash where lawful and
available. Store a short operator-authored evidence paraphrase. Do not archive
full licensed transcripts, articles, or authenticated responses in the repo or
database.

Source disagreements are records, not overwrite instructions. The as-observed
view shows the source available at its cutoff; a corrected view may append a
reviewed reconciliation.

### 6.3 Explicitly unavailable in v0.1

The project has no trustworthy free point-in-time source for:

- consensus revenue/EPS estimates;
- historical option chains, executable bid/ask, IV, or Greeks;
- full licensed call transcripts;
- news sentiment or NLP labels; or
- an authoritative automated earnings calendar.

Those fields are `unavailable`, not zero, current-chain reconstructions, or
values copied later from an unsourced website. Company guidance may be compared
with the company's own prior guidance when both cited versions exist; that is
not called a consensus beat or miss.

## 7. Manual fundamental classification rubric

v0.1 stores dimension-level observations, not an overall strong/average/weak
grade. Each label belongs to one `(event_id, cluster_id, dimension)` and must
cite at least one accepted source.

Dimensions are:

```text
demand
capex
supply
pricing
guidance
margins
execution
```

The normalized label vocabulary is:

```text
positive
neutral
negative
mixed
not_applicable
unavailable
```

Every label revision retains:

- source IDs and a concise evidence paraphrase;
- `frozen_at` and author;
- `label_context`: `before_primary_reaction_available`,
  `after_primary_reaction_available`, or `retrospective`;
- cluster and rubric versions;
- disagreement notes; and
- superseded revision and correction reason, when applicable.

`label_context` describes what the workflow could have shown at freeze time; it
does not claim the human operator was unaware of all market prices. Only
`before_primary_reaction_available` labels qualify for the highest prospective
coverage grade.

No arithmetic maps these labels into a score in v0.1. The dashboard may group
and filter them but cannot combine them into a directional state.

## 8. Frozen market-reaction windows

### 8.1 Required marks

All market measurements use `feed=sip`, `adjustment=split`, USD, and identical
endpoints for the observed symbol and benchmark.

| Window | Baseline | Target | Earliest free-feed collection |
|---|---|---|---|
| `close_to_open_30m` | Previous regular-session split-adjusted daily close | Split-adjusted SIP one-minute bar close for the bar ending at reaction-session open + 30 minutes | Target + 16 minutes |
| `close_to_close_1` | Same baseline | Reaction-session split-adjusted SIP daily close | Scheduled close + 16 minutes |
| `close_to_close_3` | Same baseline | Third regular-session split-adjusted SIP daily close | Third scheduled close + 16 minutes |

After-hours, overnight, release-to-first-trade, and call-relative windows are
deferred. They require different feeds and liquidity semantics and cannot be
silently mixed with regular-session SIP observations.

The open+30 mark requires the exact one-minute interval ending at the target;
the collector cannot substitute a later bar or silently carry an earlier bar
forward. A missing, invalid, or non-positive bar is retained as partial
provenance but produces no return. This keeps split adjustment consistent with
the daily baseline and avoids treating an unadjusted quote as an adjusted
price across a corporate action.

### 8.2 Returns and benchmarks

For observed-symbol return `r` and benchmark return `b` over identical
endpoints:

```text
raw_return = target_price / baseline_price - 1
benchmark_return = benchmark_target / benchmark_baseline - 1
abnormal_return = raw_return - benchmark_return
```

This is a transparent arithmetic difference, not beta-adjusted alpha. Each
source event creates reaction jobs for the reporter plus the union of candidates
registered for its clusters. Every observed symbol is paired with each
applicable cluster's primary benchmark and with one SPY diagnostic; identical
logical jobs are deduplicated. Candidate schedule status at the source-event
cutoff is stored separately so a later reporter is never mistaken for one that
had not yet reported. Market marks are shared; benchmark views are not
duplicate provider fetches.

v0.1 defines no positive/negative threshold. The dashboard shows signed numeric
returns and completeness only.

### 8.3 Window states

```text
pending
complete
partial
unavailable
provider_error
entitlement_error
```

`pending` is a durable expected job, not a fabricated observation row. A window
becomes final only after its target matures and provider work succeeds or reaches
the reviewed terminal failure policy. Collection timing is separate from mark
freshness and event registration mode.

## 9. Contamination and independence

Flags use a controlled vocabulary:

```text
macro_release
fed_or_rates
regulatory_or_legal
geopolitical
market_dislocation
issuer_non_earnings_news
peer_same_window
call_overlap
irregular_timing
data_quality
other_cited
unknown
```

Each flag has `known_at`, affected windows, severity (`note`, `material`, or
`exclusion_candidate`), citations, and an operator rationale. Flags append; they
never delete an event. v0.1 does not automatically exclude or down-weight an
event. Dashboard coverage shows clean and flagged counts separately and keeps
all registered events visible.

Several reporters reflecting one common macro shock are not independent
confirmations. v0.1 records `shared_driver_group_id` when known but computes no
confidence count.

## 10. Append-only storage model

Implementation should use a dedicated `data/earnings_observations.db`, separate
from both `trades.db` and the pre-open store. The workflow is irregular,
human-reviewed, and event-oriented; it should not add lock contention or schema
coupling to the trading or daily pre-open ledgers.

Runtime configuration belongs in `config/settings.py` and initially includes:

```text
EARNINGS_OBSERVATIONS_ENABLED
EARNINGS_OBSERVATION_DB_PATH
EARNINGS_PROTOCOL_VERSION
EARNINGS_REGISTRY_PATH
```

The enable switch controls collection only. It cannot enable a dashboard value
or strategy consumer.

### 10.1 Required relations

| Relation | Purpose |
|---|---|
| `earnings_protocol_versions` | Immutable contract/rubric/window identity. |
| `earnings_registry_versions` | Frozen issuer, source-cluster, cluster-candidate, and benchmark definitions. |
| `earnings_cycles` | Reporting-round identity and prospective cutoff. |
| `earnings_events` | Stable event identity, issuer, event role (`source`, `context`, or `candidate_schedule_only`), fiscal period, cycle, registration mode, and registered versions. |
| `earnings_schedule_revisions` | What timing was known when, with precision and citations. |
| `earnings_event_fact_revisions` | Actual release/call facts and their provenance. |
| `earnings_sources` | Immutable source metadata and content hashes where permitted. |
| `earnings_label_revisions` | Manual dimension labels, context, author, citations, and corrections. |
| `earnings_reaction_jobs` | Frozen expected event/window/symbol/benchmark work. |
| `earnings_reaction_observations` | Final observed-symbol and benchmark marks, returns, provenance, and revisions. |
| `earnings_contamination_revisions` | Window-specific cited contamination flags. |
| `earnings_reaction_plans` | Append-only calendar timing derived from a release-fact revision; explicit recollection requests. |
| `earnings_reaction_attempts` | Durable per-job/per-plan retry outcomes, atomically committed with final observations. |

The stable event key is:

```text
contract_version | registry_version | cycle_id | issuer_id | fiscal_period_label
```

The reaction logical key is:

```text
event_id | reaction_window_version | window | observed_symbol | benchmark_symbol
```

Revision 1 is the first final record. A correction appends revision 2 or higher
with `supersedes_id` and a required reason. Attempts and pending jobs are not
observation revisions. No correction changes the registered denominator,
registration mode, protocol version, or as-observed cutoff.

### 10.2 Required provenance and validation

- Timestamps are aware UTC at the Python boundary.
- Prices are positive SQLite `REAL`; missing values are `NULL`, never zero.
- Enum values use database checks plus typed validation.
- Source URLs and diagnostics are length-bounded and sanitized.
- No credentials, authenticated headers, raw broker responses, account data,
  or copyrighted source bodies are persisted.
- Foreign-issuer ADR identity and currency remain explicit; v0.1 market
  reactions use the registered USD ADR only.
- Writes use short transactions, bounded lock timeouts, and idempotent logical
  keys.

## 11. As-observed and corrected views

The read model exposes two explicit projections:

1. `as_observed`: the latest revision whose `known_at` or `frozen_at` is no
   later than the requested historical cutoff; and
2. `corrected`: the latest reviewed revision available now.

The dashboard defaults to `as_observed` for a selected event and labels any
corrected view. It never mixes later sources or labels into an earlier
chronological snapshot. A correction can improve data quality but cannot make a
retrospective event prospective.

## 12. Coverage and completeness

Cycle denominators come from frozen registered events and reaction jobs, not
from rows that happened to collect successfully.

The dashboard reports separately:

- registered events: prospective vs retrospective seed;
- source/context observation events vs candidate-schedule-only events;
- schedule precision coverage;
- authoritative fundamental-source coverage;
- dimension labels: frozen-before-reaction, late, retrospective, unavailable;
- reaction windows: pending, complete, partial, unavailable, error;
- clean vs contamination-flagged events; and
- source-cluster and cluster-candidate registry coverage.

A source or context event is `observation_complete` only when:

1. release timing is final or explicitly unavailable;
2. at least one issuer or SEC fundamental source is recorded, or source
   unavailability is finalized;
3. every applicable dimension has a final label including explicit
   `not_applicable`/`unavailable`; and
4. every frozen reporter/candidate reaction job across all three required
   market windows has a final outcome, including explicit unavailable/error
   states.

Completion is a data-integrity state, not a favorable result or a strategy
permission.

## 13. Collection and operator workflow

Earnings collection should initially be a standalone bounded process, not a
`TradingEngine.post_cycle_hook`. The cadence is irregular, manual labels need
review, SEC access has its own fair-access policy, and no trading-cycle timing
benefit justifies coupling it to the engine.

The implementation phase should provide:

1. a versioned registry loader that registers expected events and jobs;
2. a bounded SEC/issuer metadata collector with declared user agent and rate
   limits;
3. a delayed-SIP reaction collector that processes only mature jobs, never
   sleeps inside bot code, and can recover missed windows historically;
4. a validated operator CLI for append-only schedules, citations, labels,
   contamination flags, and corrections; and
5. a read-only dashboard reader using SQLite `mode=ro`.

The unsecured Streamlit dashboard must expose no write, approve, classify,
correct, rank, or trade controls.

## 14. Dashboard presentation contract

The routed **Earnings Clusters** view initially shows:

- cycle and protocol/registry version;
- prospective vs retrospective coverage;
- a chronological event timeline with schedule precision and registration
  mode;
- fundamental dimension labels with citations and label context;
- raw and benchmark-adjusted numeric reactions by matured window;
- pending/unavailable/error states without fabricated zeroes;
- cluster memberships and still-unreported registered candidates;
- contamination flags and shared-driver groups; and
- an explicit as-observed/corrected-view label.

It does not show an aggregate cluster direction, confidence score, candidate
rank, hypothetical trade, expected return, option payoff, recommendation, or
strategy permission. The dashboard performs no provider calls and does not
create or mutate the database.

## 15. Implementation acceptance gates

The later implementation must provide deterministic offline tests for:

- registry versioning, aliases, Alphabet's one-event/two-share-class identity,
  foreign ADR identity, immutable event-version assignment, mid-cycle registry
  changes without duplicate identities/jobs, and historical denominator
  stability;
- prospective vs retrospective registration at the exact release cutoff;
- schedule revisions and point-in-time `known_at` queries;
- DST, holidays, special sessions, BMO/AMC, an exact-close release boundary,
  same-close events, five-minute buffer, intraday release, and unknown-time
  behavior;
- source precedence, SEC-acceptance proxy labeling, disagreements, missing
  sources, and citation validation;
- every classification dimension/enum, explicit unavailable states, label
  context, correction revisions, and no aggregate-score calculation;
- exact reaction maturity, delayed-SIP eligibility, exact open+30 bar selection,
  no later/earlier bar substitution, split adjustment, and benchmark endpoint
  equality;
- raw/benchmark/abnormal return calculations, reporter/candidate job expansion,
  logical-key deduplication, candidate report status at cutoff, and
  multi-cluster benchmark views;
- contamination flags, shared-driver groups, and all-events-visible coverage;
- append-only idempotency, atomic attempt/final writes, lock timeout, recovery,
  and correction chains;
- as-observed views that exclude later revisions and corrected views that never
  rewrite registration mode;
- dashboard reads that use `mode=ro`, make no network call, and never create a
  missing database; and
- architecture tests proving no engine, strategy, risk, broker-order, or
  execution consumer imports the earnings observation reader.

A manual paper-stage check must then register one event before release, append
at least one cited label, collect all matured SIP windows, render both views,
restart safely, and prove zero trading effects. One full prospective reporting
round is the minimum operational evidence before proposing scoring or ranking.

## 16. Explicitly deferred

- event backfill beyond the bounded retrospective-seed pilot;
- automated fundamental classification or NLP;
- full call transcripts and news ingestion;
- consensus beat/miss data;
- historical or live option chains and implied moves;
- after-hours/overnight market reactions;
- peer baskets, beta adjustment, learned weights, or decay;
- aggregate fundamental, reaction, or confidence scores;
- candidate directions, rankings, hypothetical trades, or outcome P&L;
- alerts based on labels or returns;
- dashboard write controls;
- `TradingEngine` or strategy consumption; and
- paper or live orders.

## 17. Next reviewed phases

1. **Contract review:** merge this document only after the registry, timestamps,
   free-source boundaries, and absence of scoring are accepted.
2. **Ledger implementation:** schema, registry loader, operator CLI, bounded
   collectors, correction model, and deterministic tests in a separate PR.
3. **Read-only presentation:** dashboard reader and Earnings Clusters view in a
   separate PR after the ledger produces a sanitized fixture.
4. **Prospective watch:** collect one complete cycle and review coverage,
   timing, label repeatability, contamination, and operational failures.
5. **Later research proposal:** only then decide whether scoring, candidate
   ranking, retrospective study, or shadow observation deserves a new contract.

### Ledger implementation notes

The ledger implementation supplies phase 2 plus a reusable read-only export;
phase 3 dashboard rendering remains separate. The operator workflow and exact
retry/collection bounds are in `earnings_observation_operations.md`.

Knowledge clocks are assigned when records are written. BMO/AMC registration
without an exact timestamp conservatively requires registration before the
scheduled New York date begins. If later facts reveal late registration, the
original mode remains immutable but a separate timing-conflict flag prevents
it from qualifying as verified prospective timing. An operator-bounded actual
release cannot determine a standard reaction window and finalizes unavailable.

Release-fact corrections and explicit provider recollection append timing-plan
revisions while preserving frozen job identities and denominators. Current
completeness requires observations for the current plan. Previous observations
remain available in historical projections and as explicitly prior records.
No corrected result inherits an earlier knowledge clock.
