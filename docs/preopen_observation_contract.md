# Pre-Open Market Observation Contract

**Status:** Proposed v0.2. Review and freeze this contract before implementing
collection, persistence, or dashboard calculations.

## 1. Purpose and boundary

Record a small, trustworthy description of the US equity market at defined
times before the regular open. The initial configured cohort is:

- SPY — broad US large-cap market;
- QQQ — Nasdaq-100 / growth-heavy market;
- XLK — technology sector; and
- SOXX — semiconductor sector.

The first use is the read-only Market Observatory. This contract does not
authorize a strategy filter, trade-timing rule, position hold, order delay,
allocation change, or execution control. Promotion from observation to action
requires a separate evidence review and design.

These are point-in-time market records reconstructed from consolidated
historical data. They are not claims about what the bot knew at the checkpoint.
The free data becomes queryable only after Alpaca's delayed-SIP window.

### 1.1 Reusable universe contract

The collector, calculations, persistence, and dashboard read model are
symbol-agnostic for US-listed stocks and ETFs supported by Alpaca's stock-data
API. SPY, QQQ, XLK, and SOXX are configuration, not branches in collection or
calculation code.

Each configured instrument has:

| Field | Contract |
|---|---|
| `symbol` | Uppercase provider symbol; unique within the universe version |
| `asset_type` | `stock` or `etf`; descriptive, not a calculation switch |
| `observation_role` | Human-readable context such as `broad_market`, `technology`, `semiconductors`, or `single_name`; never a strategy permission |
| `effective_from_session` | First exchange session for prospective collection |
| `effective_through_session` | Optional final session; historical records remain readable |
| `enabled` | Operational collection flag; changes take effect only at a session boundary |

The ordered set of instrument specifications is identified by
`universe_version`. Adding, removing, enabling, disabling, relabeling, or
changing the effective dates of an instrument creates a new universe version.
It does not require a new `contract_version` unless collection or calculation
semantics change.

Universe changes are prospective. Newly added symbols are not backfilled into
the monitored cohort, and removed symbols are never deleted from history.
Coverage denominators use the universe version effective for that session.

Symbol-specific freshness, gap, or quality thresholds are not allowed in the
initial implementation. If a less-liquid stock produces stale, wide, or
one-sided pre-market data, those are valid quality findings. Any later
per-symbol policy requires evidence and a contract version change rather than a
hidden exception.

## 2. Provider and feed contract

The initial provider is Alpaca because it is already the bot's authenticated
market-data boundary and exposes explicit feed provenance.

| Layer | Feed | Timing | Meaning |
|---|---|---|---|
| `sip_checkpoint` | historical `sip` | Query no earlier than checkpoint + 16 minutes | Consolidated quote at or before the checkpoint; sole primary observation and sole coverage source |
| `iex_diagnostic` | historical or prospectively captured `iex` | Optional, deferred | Single-venue comparison evidence only; never substitutes for SIP or contributes to primary coverage |
| reference close | historical `sip` | Available before collection | Previous regular-session consolidated daily-bar close, split-adjusted |

Alpaca documents IEX as a single exchange and SIP as all US exchanges. Recent
SIP historical queries without real-time entitlement must end at least 15
minutes in the past. The implementation therefore queries historical SIP with
an end time equal to the checkpoint and selects an event at or before that
time. It must not use a rolling delayed snapshot whose own observation time is
later than the checkpoint.

Primary references:

- <https://docs.alpaca.markets/us/docs/market-data-faq>
- <https://docs.alpaca.markets/us/reference/stockquotes-1>

The feed is always passed explicitly. Provider defaults are not accepted as
provenance. Quote selection uses one historical SIP request per due symbol with
`sort=desc` and `limit=1`, bounded by that symbol's pre-market start and
checkpoint. Alpaca applies `limit` across a multi-symbol response rather than
per symbol, so a multi-symbol latest-quote request cannot prove absence or
return every symbol's latest quote without paging the entire quote stream. The
previous-close daily-bar request may remain batched because the expected one
bar per symbol fits in one verified page for the initial cohort. The adapter
must verify that every due symbol is represented and paginate or chunk a larger
future universe before treating a reference as missing. Each instrument retains
its independent source timestamp and outcome.

IEX is not a fallback. If SIP is missing, stale, unavailable, or fails, the
primary row records that condition. An optional IEX diagnostic layer may be
implemented only after the primary collector is working and must remain
visibly separate in storage, coverage, and the UI.

## 3. Session and checkpoint contract

The exchange session comes from Alpaca's calendar, not weekday arithmetic or a
hard-coded 09:30 open. Alpaca SDK `Calendar.open` values are timezone-naive in
the currently pinned SDK. The adapter must interpret them in
`America/New_York` by attaching that zone explicitly before conversion to UTC;
it must never apply the host machine's local timezone implicitly.

`session_date` is the exchange calendar date in `America/New_York`; stored
timestamps are timezone-aware UTC.

Checkpoints are relative to the scheduled regular-session open:

| Checkpoint | Checkpoint time | Earliest normal availability | Intended view |
|---|---:|---:|---|
| `open_minus_90m` | open - 90 minutes | checkpoint + 16 minutes | Early pre-market condition |
| `open_minus_30m` | open - 30 minutes | checkpoint + 16 minutes | Later participation and direction |
| `open_minus_5m` | open - 5 minutes | checkpoint + 16 minutes | Open-imminent point in time, available after the bell |

For a normal 09:30 ET open, the rows describe 08:00, 09:00, and 09:25 ET and
become available at approximately 08:16, 09:16, and 09:41 ET. The last row
cannot support a 09:30 decision under the free delayed feed. Any future
observation-to-action design must prove that its chosen checkpoint is available
before the action it proposes to influence.

The collector stores three distinct times:

- `checkpoint_at`: the market time being described;
- `available_at`: the earliest configured time at which the free SIP query is
  expected to be entitled; and
- `collected_at`: when the response was actually received and normalized.

Because historical SIP is point-in-time addressable, a sleeping laptop or
temporary outage may collect the same checkpoint later. Such a row is still a
primary checkpoint observation; collection timeliness is reported separately
and it must never be presented as information that was available live.

On non-session days no rows are due. The calendar remains authoritative for
special sessions. While the calendar is unavailable, the collector does not
infer an open or create guessed rows. It logs the operational failure and
returns normally; a later run may recover the historical checkpoint after the
calendar service returns.

## 4. Price and reference semantics

### 4.1 Previous-close reference

`reference_price` is the previous real trading session's SIP daily-bar close
with `adjustment=split`. Split adjustment is required so a split effective on
the observation day does not create a false gap. Dividend adjustment is not
used: an ex-dividend move is part of the observed opening price change and may
be annotated later rather than erased.

The record retains the reference session, timestamp, feed, adjustment, and
price. If the reference is unavailable, quote data may still be stored, but all
gap fields are `NULL`, `observation_status` is `partial`, and `reason_code` is
`reference_unavailable`.

### 4.2 Checkpoint mark

The v0.2 primary mark is quote midpoint only. Query a bounded historical SIP
quote window ending at `checkpoint_at`, then choose the latest quote whose
source timestamp is not later than the checkpoint.

A valid midpoint requires positive bid and ask prices and `ask >= bid`:

```text
midpoint = (bid_price + ask_price) / 2
spread_bps = (ask_price - bid_price) / midpoint * 10_000
gap_dollars = midpoint - reference_price
gap_pct = gap_dollars / reference_price
```

One-sided and crossed quotes are retained for provenance but cannot produce a
mark. Last trades may be stored as diagnostics, but v0.2 does not use a trade
fallback. This avoids silently defining tape-specific eligible-trade
conditions before that policy has been researched and versioned.

A stale quote is retained with `mark_kind=stale_midpoint` for descriptive
inspection, but `gap_dollars` and `gap_pct` are `NULL`. It cannot produce a
complete observation.

Do not store a bullish/bearish score or composite market signal. The dashboard
may format the sign of a valid `gap_pct` as up, down, or unchanged while always
showing the numeric value.

## 5. Freshness and quality states

Quote age is computed as `checkpoint_at - quote_timestamp`, not from collection
time. A quote after the checkpoint is never eligible.

| Age | State |
|---:|---|
| 0-60 seconds | `fresh` |
| >60-300 seconds | `aging` |
| >300 seconds | `stale` |
| missing, negative, or otherwise invalid | `invalid` |

These bands describe data recency; they are not strategy thresholds.
Collection timeliness is separate:

- `on_schedule`: collected no later than `available_at + 10 minutes`; and
- `delayed_collection`: collected after that boundary.

An expected logical key with no finalized observation is reported as pending or
not collected by the coverage query; it is not represented by a fabricated
market-data row.

The row-level `observation_status` vocabulary is:

```text
complete
partial
unavailable
provider_error
entitlement_error
```

`complete` requires a valid fresh or aging quote midpoint and a valid
reference. `partial` means some provider data exists but the quote is stale or
invalid, or the reference is missing. `unavailable` means the provider returned
no eligible quote in the bounded lookback. Errors retain a normalized
`reason_code`; exception text is diagnostic metadata, not a grouping key.

## 6. Immutable record and retry model

Persist observations in a separate `data/market_observations.db`; do not add
them to `trades.db`. The trade ledger must remain about executed and operational
trading evidence.

The primary logical key is:

```text
contract_version | universe_version | session_date | checkpoint | symbol | layer
```

where the primary `layer` is `sip_checkpoint`. Each finalized observation has
an integer `revision` beginning at 1. A correction appends a higher revision
with `supersedes_id` and a required correction reason; it never updates or
deletes the original row.

Collection attempts are not observation revisions. Store them in a separate
append-only attempt relation keyed by the primary logical key and
`attempt_number`.

One **collection round** is the counting unit. A round issues one
`sort=desc`, `limit=1` quote request per due symbol plus the batched or cached
reference-close request needed to resolve the included keys. Every logical key
included in that round gains one attempt, even though reference work and the
round identity are shared. All per-symbol requests belong to the same hook
invocation; the scheduler does not spend one invocation per symbol.

Attempt outcomes are classified as follows:

- transport/DNS/connection failures, connect or read timeouts, HTTP 408, HTTP
  429, and HTTP 5xx are `transient_failure`;
- an HTTP 403 whose normalized provider error says the query is inside the
  recent-SIP entitlement delay is transient `not_yet_available`; it remains
  pending and follows the same backoff rather than becoming a permanent gap;
- other authentication, authorization, or entitlement failures (normally HTTP
  401/403) are non-retryable `entitlement_error`;
- other provider HTTP 4xx request failures are non-retryable `provider_error`;
- a successful response with no quote in the eligible window is the final data
  outcome `unavailable` for that request's single symbol, not a provider
  failure; and
- a local persistence lock/failure is not a provider attempt outcome. It is
  logged, leaves the key pending, and is retried by a later hook.

A transient failure leaves the observation pending. Retry delays after
successive transient rounds are 5 minutes, 15 minutes, 1 hour, and then 6 hours
between later rounds. `retry_deadline_at` is seven calendar days after the
first actual provider attempt. The first hook on or after that deadline gets
one final recovery round; only a transient failure in that round finalizes the
revision-1 `provider_error`. A process that is not running makes no attempt, and
if it resumes after the deadline it still receives that final recovery round.
A success or non-retryable outcome finalizes exactly one revision-1 observation
with `attempt_count`; an earlier attempt cannot block it. Corrections remain the
only reason to create revision 2 or higher.

Required finalized-observation fields:

| Group | Fields |
|---|---|
| Identity | `observation_id`, `logical_key`, `contract_version`, `universe_version`, `revision`, `supersedes_id`, `layer` |
| Schedule | `session_date`, `checkpoint`, `checkpoint_at`, `available_at`, `first_attempt_at`, `retry_deadline_at`, `collected_at`, `collection_state` |
| Instrument | `symbol`, `asset_type`, `observation_role`, `currency` |
| Provenance | `provider`, `feed`, `endpoint_kind`, `is_consolidated`, `attempt_count` |
| Quote | `quote_timestamp`, `bid_price`, `bid_size`, `bid_exchange`, `ask_price`, `ask_size`, `ask_exchange`, `quote_conditions`, `quote_tape` |
| Optional trade diagnostics | `trade_timestamp`, `trade_price`, `trade_size`, `trade_exchange`, `trade_conditions`, `trade_tape` |
| Reference | `reference_session_date`, `reference_timestamp`, `reference_price`, `reference_feed`, `reference_adjustment` |
| Derived | `mark_kind`, `mark_price`, `spread_bps`, `gap_dollars`, `gap_pct`, `quote_age_seconds`, `freshness_state` |
| Outcome | `observation_status`, `reason_code`, `diagnostic`, `created_at` |

Each attempt records the logical key, attempt number, round ID, start/end time,
provider, feed, endpoint set, outcome classification (including
`not_yet_available` where applicable), next eligible retry time, and a
sanitized diagnostic. Prices and sizes use SQLite `REAL`; timestamps use UTC
ISO-8601 text; conditions use canonical JSON arrays. Diagnostics are
length-limited and must not contain credentials, request headers, or raw
authenticated responses.

Provider and market-value fields are nullable for `provider_error` and
`entitlement_error` outcomes. Their absence is preserved as `NULL`, never a
zero price or size. Database constraints must enforce the enum vocabularies,
timezone-aware input at the Python boundary, positive reference prices,
non-negative sizes, monotonic attempt numbers, and `revision >= 1`.

## 7. Historical SIP selection contract

For every due checkpoint and instrument:

1. wait until at least `available_at`;
2. make that symbol's historical SIP quote request with `sort=desc`, `limit=1`,
   a start of 04:00 `America/New_York` on the session date, and an end of
   exactly `checkpoint_at`;
3. select the latest quote whose source timestamp is not later than
   `checkpoint_at`;
4. calculate freshness against `checkpoint_at` and derive a midpoint only when
   the quote is valid;
5. obtain the exact split-adjusted SIP reference close; and
6. finalize one `sip_checkpoint` observation after a data success,
   non-retryable failure, or failed final recovery round.

The query must not reach forward in time. A stale selected quote remains
`partial` with `reason_code=stale_mark` and no gap. No quote during that
session's pre-market window is `unavailable`. The pre-market-session window
makes stale observations reachable without carrying a previous evening's quote
into the new session. Provider retries follow the classified recovery policy in
section 6 and cannot affect the trading cycle.

## 8. Complete-cohort and failure behavior

Each valid session expects three primary rows per instrument active in that
session's universe version. The initial four-instrument cohort therefore
expects 12 finalized `sip_checkpoint` rows. Primary coverage is computed only
from those rows, with complete, partial, unavailable, and error counts.
Optional IEX diagnostics never increase, reduce, or replace primary coverage.

Collection timeliness is reported separately from data completeness. A later
successful historical query may be complete even though collection was
delayed; the UI must show both facts.

All collection and persistence failures are isolated from `TradingEngine`:

- exceptions are logged and converted to an attempt/final outcome where
  possible;
- the hook returns normally;
- no strategy, risk, execution, or operator-control state is mutated; and
- database lock/contention uses a bounded timeout and skips rather than
  delaying the trading cycle.

## 9. Scheduling and existing-code boundaries

The initial implementation should add the SIP collector to a composite
`post_cycle_hook` that invokes the existing `HealthReviewScheduler` and then the
observation scheduler. It must not replace or alter health-review scheduling.
The observation scheduler performs only currently due work, makes at most one
shared collection round covering all eligible keys per invocation, uses one
latest-quote request per included symbol, never sleeps in the hook, and returns
normally on failure.

Historical SIP remains queryable after its entitlement delay, so this design
does not depend on a scan completing inside a narrow live checkpoint window.
If measured cycle impact exceeds the reviewed bound, collection moves to a
separate lightweight process without changing the observation contract.

Other boundaries:

- `data.market_calendar.trading_sessions()` currently preserves session dates
  only. Add a focused adapter retaining Alpaca's scheduled open and explicitly
  localizing the SDK's naive value to `America/New_York`; do not change the
  cache-gap helper's return contract.
- `data.fetcher.fetch_latest_quote()` is an execution-arrival benchmark with
  execution-specific rejection rules and an `ALPACA_DATA_FEED` switch. Do not
  reuse it as the observation model.
- Persistence belongs under `reporting/` and in
  `data/market_observations.db`, separate from `TradeLogger`.
- The dashboard reads finalized observations only and never performs provider
  collection or mutates the store.

## 10. Dashboard presentation contract

Market Observatory initially shows:

- session and checkpoint time, plus when the record became available and was
  collected;
- every instrument in the session's configured universe, initially SPY, QQQ,
  XLK, and SOXX, with valid midpoint and gap percentage;
- feed, quote timestamp, age, freshness, and bid/ask spread;
- explicit delayed-collection, stale, unavailable, and error reasons; and
- primary SIP cohort coverage.

The dashboard labels these as delayed consolidated checkpoint observations,
not live pre-open snapshots. It does not show recommendations, confidence in
future returns, strategy permissions, or suggested actions. Interpretive
labels come from `observation_role`, not symbol-specific UI branches.
Instruments are not combined into a single score in the observation phase.

## 11. Implementation acceptance gates

Implementation may begin after this contract is reviewed and frozen. The first
collector PR must provide offline deterministic tests for:

- session-relative checkpoint calculation, including DST, special sessions,
  and non-session days;
- the actual pinned Alpaca SDK `Calendar` model under at least two host
  timezones, proving the naive scheduled open is interpreted in
  `America/New_York`;
- arbitrary stock and ETF symbols (for example AAPL and IWM), proving the core
  has no SPY/QQQ/XLK/SOXX-specific branches;
- universe-version validation, including `enabled` changes, session-boundary
  changes, and historical denominators that do not change with current config;
- eligibility delay and historical collection after a missed local runtime;
- latest-at-or-before quote selection that never reaches past the checkpoint;
- one `sort=desc`, `limit=1` request per due symbol, proving a successful
  response for one symbol cannot make another symbol `unavailable`;
- pre-market-session selection that can retain a stale same-session quote but
  cannot carry a previous evening's quote forward;
- quote validity, midpoint, spread, gap, and freshness calculations, including
  stale rows with no gap and no trade fallback;
- split-adjusted reference semantics and missing references;
- append-only per-key attempts from shared collection rounds, transient versus
  non-retryable classification, backoff and final recovery deadline, one final
  revision 1, correction revisions, and idempotent restart behavior;
- recent-SIP-delay HTTP 403 responses remaining pending as
  `not_yet_available`, while genuine authentication/entitlement errors finalize;
- complete, partial, unavailable, provider-error, and entitlement-error states;
- composite-hook preservation of `HealthReviewScheduler`, bounded/no-sleep
  collector behavior, and failures that cannot raise into the engine; and
- dashboard reads that never mutate the observation store.

A manual paper check should then verify one complete market session, including
the expected 12 primary records for the initial frozen universe version,
primary coverage, collection-timeliness labels, and zero strategy effects.

## 12. Evidence behind the feed decision

A reviewer compared the 2026-10-29, 2026-10-30, and 2026-11-02 sessions across
SPY, QQQ, XLK, and SOXX at all three checkpoints. Historical SIP supplied a
fresh eligible quote for all 36 symbol/checkpoint cases, with the oldest 18
seconds at the checkpoint. IEX supplied 0 of 12 fresh quotes at 08:00, 4 of 12
at 09:00, and 3 of 12 at 09:25. At the 08:00 `open_minus_90m` checkpoint, some
IEX values were from the previous evening or roughly 64 hours old; gap
differences versus SIP had a median absolute difference around 0.9 percentage
points, a maximum around 6.1 points, and disagreed in sign in 3 of 12 cases.
Later-checkpoint gap differences were materially smaller when IEX had usable
data; freshness coverage, rather than a claim of uniformly large gap error,
remains the reason SIP is primary.

This is decision evidence, not a universal provider benchmark. Limitations:

- only three sessions and four highly liquid ETFs were examined;
- the check used historical IEX because a prospective live snapshot sample was
  not yet available;
- no single-name stocks, split sessions, or early-close sessions were sampled;
  and
- provider behavior and entitlements may change.

IEX may be reconsidered as a primary source only through a new reviewed
contract version backed by a prospective, pre-registered study across enough
sessions and both ETFs and single names. That study must demonstrate reliable
checkpoint coverage, bounded quote age and spreads, and acceptable direction
and gap fidelity against consolidated SIP. Until then IEX remains optional
diagnostic evidence only.

## 13. Explicitly deferred

- IEX diagnostic collection;
- streaming or paid real-time SIP collection;
- overnight/BOATS data;
- activating additional US stocks or ETFs before reviewing initial-cohort data
  quality (the collector remains designed to support them);
- index values, options, crypto, non-US instruments, and non-equity sessions;
- trade-fallback eligibility rules;
- earnings-cluster observations;
- alerts based on gap magnitude;
- composite scores;
- historical outcome studies; and
- any observation-to-action integration.
