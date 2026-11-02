# Pre-Open Market Observation Contract

**Status:** Proposed v0.1. Review and freeze this contract before implementing
collection, persistence, or dashboard calculations.

## 1. Purpose and boundary

Record a small, trustworthy description of the US equity market before the
regular open. The initial configured cohort is:

- SPY — broad US large-cap market;
- QQQ — Nasdaq-100 / growth-heavy market;
- XLK — technology sector;
- SOXX — semiconductor sector.

The first use is the read-only Market Observatory. This contract does not
authorize a strategy filter, trade-timing rule, position hold, order delay,
allocation change, or execution control. Promotion from observation to action
requires a separate evidence review and design.

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
`universe_version`. Adding, removing, relabeling, or changing the effective
dates of an instrument creates a new universe version. It does not require a
new `contract_version` unless capture or calculation semantics change.

Universe changes are prospective. Newly added symbols are not backfilled into
the as-observed cohort, and removed symbols are never deleted from history.
Coverage denominators use the universe version effective for that session.

Symbol-specific freshness, gap, or quality thresholds are not allowed in the
initial implementation. If a less-liquid stock produces stale, wide, or
one-sided pre-market data, those are valid observations. Any later per-symbol
policy requires evidence and a contract version change rather than a hidden
exception.

## 2. Provider and feed contract

The initial provider is Alpaca because it is already the bot's authenticated
market-data boundary and exposes explicit feed provenance.

| Layer | Feed | Timing | Meaning |
|---|---|---|---|
| `as_observed` | `iex` | Captured within the checkpoint window | Free real-time view from the IEX venue only; not consolidated NBBO or total-market truth |
| `sip_enrichment` | historical `sip` | No earlier than 16 minutes after capture | Consolidated-tape context aligned to the original capture time; never overwrites the IEX record |
| `late_research` | `iex` | Optional capture after a missed deadline | Research-only view at its actual later timestamp; never fills the missed as-observed checkpoint |
| reference close | historical `sip` | Available before the pre-market checkpoints | Previous regular-session consolidated daily-bar close, split-adjusted |

Alpaca documents IEX as a single exchange and SIP as all US exchanges. Its
latest endpoints also expose `delayed_sip`, while recent SIP historical queries
without real-time entitlement must end at least 15 minutes in the past. The
implementation should use historical SIP for enrichment so it can select data
at or before the original observation timestamp instead of accepting a later
rolling delayed snapshot.

Primary references:

- <https://docs.alpaca.markets/us/docs/market-data-faq>
- <https://docs.alpaca.markets/us/reference/stocklatestquotes-1>
- <https://docs.alpaca.markets/us/reference/stockquotes-1>

The feed is always passed explicitly. Provider defaults are not accepted as
provenance.

The preferred live acquisition is one multi-symbol stock snapshot request with
`feed=iex`, providing independently timestamped latest quotes and trades for all
instruments in the active universe version. Missing symbols may receive one
bounded same-feed retry within the checkpoint deadline; each row retains its
actual receipt time and endpoint kind. There is no alternate-feed fallback that
could silently change provenance inside one checkpoint.

## 3. Session and checkpoint contract

The exchange session comes from Alpaca's calendar, not weekday arithmetic or a
hard-coded 09:30 open. `session_date` is the calendar date in
`America/New_York`; stored timestamps are timezone-aware UTC.

Checkpoints are relative to the scheduled regular-session open:

| Checkpoint | Target | Capture deadline | Intended view |
|---|---:|---:|---|
| `open_minus_90m` | open − 90 minutes | target + 10 minutes | Early pre-market condition |
| `open_minus_30m` | open − 30 minutes | target + 10 minutes | Later participation and direction |
| `open_minus_5m` | open − 5 minutes | open − 30 seconds | Open-imminent condition without sampling after the bell |

The collector may capture only at or after the target. For a live IEX response,
`observed_at` is the UTC time when the response became available locally; it is
never replaced by the target time. `received_at` is the response receipt time
and is therefore equal to `observed_at` for the base live row. For later SIP
enrichment, `observed_at` copies the parent IEX time while `received_at` records
the later download time. If no collection occurs before the deadline, the
cohort eventually receives an explicit `missed` row rather than a reconstructed
as-observed value.

On non-session days no rows are due. The calendar remains authoritative for
special sessions. While the calendar is unavailable, the collector does not
infer an open or create guessed rows; it logs the operational failure and
returns normally. After calendar service recovers, reconciliation creates
explicit `missed` rows for any valid session/checkpoints that passed uncovered.

## 4. Price and reference semantics

### 4.1 Previous-close reference

`reference_price` is the previous real trading session's SIP daily-bar close
with `adjustment=split`. Split adjustment is required so a split effective on
the observation day does not create a false gap. Dividend adjustment is not
used: an ex-dividend move is part of the observed opening price change and may
be annotated later rather than erased.

The record retains the reference session, timestamp, feed, adjustment, and
price. If the reference is unavailable, quote/trade observations may still be
stored, but all gap fields are `NULL`, `observation_status` is `partial`, and
`reason_code` is `reference_unavailable`.

### 4.2 As-observed mark

Store bid, ask, and last trade independently. Derive the display mark in this
order:

1. quote midpoint when bid and ask are both positive and `ask >= bid`;
2. last eligible trade when the quote is invalid or absent;
3. unavailable when neither is usable.

A one-sided or crossed quote is retained for provenance but cannot produce a
midpoint. A stale quote may still produce a descriptive mark, but its stale
state must remain visible and cannot be promoted to a fresh observation.

Derived values are:

```text
midpoint = (bid_price + ask_price) / 2
spread_bps = (ask_price - bid_price) / midpoint * 10_000
gap_dollars = mark_price - reference_price
gap_pct = gap_dollars / reference_price
```

Do not store a bullish/bearish score or composite market signal. The dashboard
may format the sign of `gap_pct` as up, down, or unchanged while always showing
the numeric value.

## 5. Freshness and quality states

Quote age and trade age are computed independently against `observed_at`. For
SIP enrichment, this is the copied parent IEX time, so a quote fetched 16
minutes later is evaluated against the point it describes rather than its later
download time.

| Age | State |
|---:|---|
| 0–60 seconds | `fresh` |
| >60–300 seconds | `aging` |
| >300 seconds | `stale` |
| missing or timestamp in the future by more than 2 seconds | `invalid` |

These bands describe data recency; they are not strategy thresholds. Capture
timeliness is tracked separately:

- `on_time`: received no later than the checkpoint deadline;
- `late`: received after the deadline and stored only as research evidence,
  never as the missing checkpoint's as-observed replacement;
- `missed`: no response was captured by the deadline.

The row-level `observation_status` vocabulary is:

```text
complete
partial
unavailable
missed
provider_error
entitlement_error
```

`complete` requires a usable mark and reference. `partial` means some provider
data exists but the mark, reference, or one side of the quote is missing.
Errors retain a normalized `reason_code`; exception text is diagnostic metadata,
not a grouping key.

## 6. Immutable record model

Persist observations in a separate `data/market_observations.db`; do not add
them to `trades.db`. The trade ledger must remain about executed and operational
trading evidence.

The logical key is:

```text
contract_version | universe_version | session_date | checkpoint | symbol | layer
```

where `layer` is `as_observed`, `sip_enrichment`, or `late_research`. Each
append-only record has an integer `revision` beginning at 1. Collector retries
use `INSERT OR IGNORE` for revision 1. A correction appends a higher revision
with `supersedes_id` and a required correction reason; it never updates or
deletes the original row.

Required fields:

| Group | Fields |
|---|---|
| Identity | `observation_id`, `logical_key`, `contract_version`, `universe_version`, `revision`, `supersedes_id`, `layer` |
| Schedule | `session_date`, `checkpoint`, `scheduled_at`, `deadline_at`, `observed_at`, `received_at` |
| Instrument | `symbol`, `asset_type`, `observation_role`, `currency` |
| Provenance | `provider`, `feed`, `endpoint_kind`, `is_consolidated`, `parent_observation_id` |
| Quote | `quote_timestamp`, `bid_price`, `bid_size`, `bid_exchange`, `ask_price`, `ask_size`, `ask_exchange`, `quote_conditions`, `quote_tape` |
| Trade | `trade_timestamp`, `trade_price`, `trade_size`, `trade_exchange`, `trade_conditions`, `trade_tape` |
| Reference | `reference_session_date`, `reference_timestamp`, `reference_price`, `reference_feed`, `reference_adjustment` |
| Derived | `mark_kind`, `mark_price`, `spread_bps`, `gap_dollars`, `gap_pct`, `quote_age_seconds`, `trade_age_seconds`, `freshness_state`, `capture_state` |
| Outcome | `observation_status`, `reason_code`, `diagnostic`, `created_at` |

Prices and sizes use SQLite `REAL`; timestamps use UTC ISO-8601 text; conditions
use canonical JSON arrays. `diagnostic` is length-limited and must not contain
credentials, request headers, or raw authenticated responses.

Provider and market-value fields are nullable for `missed`, `provider_error`,
and `entitlement_error` outcomes. Their absence is preserved as `NULL`, never a
zero price or size.

Database constraints must enforce the enum vocabularies, timezone-aware input
at the Python boundary, positive reference prices, non-negative sizes, and
`revision >= 1`.

## 7. SIP enrichment contract

Enrichment begins no earlier than `observed_at + 16 minutes`. For each base IEX
observation:

1. query historical SIP quotes and trades over a bounded lookback ending at
   the original `observed_at`;
2. choose the latest eligible event whose source timestamp is not later than
   `observed_at`;
3. compute the SIP mark and gap against the exact stored reference used by the
   base row; and
4. append a `sip_enrichment` row linked through `parent_observation_id`.

The initial lookback is five minutes. If no eligible quote or trade exists, the
enrichment row is `unavailable`; it must not reach forward in time. The UI shows
IEX as-observed and SIP-enriched values side by side with their timestamps and
never silently substitutes one for the other.

An entitlement or provider failure is evidence about coverage and receives its
own row. It is not retried indefinitely and cannot affect the trading cycle.

## 8. Complete-cohort and failure behavior

Each valid session expects three base rows per instrument active in that
session's universe version. The initial four-instrument cohort therefore expects
12 rows. Coverage is reported against the recorded session denominator, with
complete, partial, missed, and unavailable counts. Enrichment coverage is
reported separately.

Missing records are first-class. A reconciliation pass may append a `missed`
row after a deadline, but may not backfill the price and call it as-observed.
Historical research fetched later is a separately labeled layer and is not part
of prospective capture coverage.

All collection and persistence failures are isolated from `TradingEngine`:

- exceptions are logged and converted to an observation outcome where possible;
- the hook returns normally;
- no strategy, risk, execution, or operator-control state is mutated; and
- database lock/contention uses a bounded timeout and skips rather than delaying
  the trading cycle.

## 9. Dashboard presentation contract

Market Observatory initially shows:

- session and checkpoint time;
- every instrument in the session's configured universe, initially SPY, QQQ,
  XLK, and SOXX, with mark and gap percentage;
- mark kind, feed, quote/trade timestamp, age, and freshness;
- bid/ask spread where valid;
- IEX as-observed and SIP enrichment as distinct columns or views;
- cohort coverage and explicit missed/unavailable reasons.

The dashboard does not show recommendations, confidence in future returns,
strategy permissions, or suggested actions. Interpretive labels come from
`observation_role`, not symbol-specific UI branches. Instruments are not
combined into a single score in the observation phase.

## 10. Implementation acceptance gates

Implementation may begin after this contract is reviewed and frozen. The first
collector PR must provide offline deterministic tests for:

- session-relative checkpoint calculation, including DST and non-session days;
- a scheduling path that demonstrably meets checkpoint deadlines without
  depending on the duration of a full strategy scan;
- arbitrary stock and ETF symbols (for example AAPL and IWM), proving the core
  has no SPY/QQQ/XLK/SOXX-specific branches;
- universe-version validation, session-boundary changes, and historical
  coverage denominators that do not change when the current universe changes;
- checkpoint deadlines and explicit missed rows;
- quote validity, midpoint/trade fallback, gap, spread, and age calculations;
- split-adjusted reference semantics and missing references;
- immutable idempotent inserts, correction revisions, and parent linkage;
- SIP alignment that never selects an event after `observed_at`;
- complete/partial/unavailable/error states;
- database failures that cannot raise into the engine hook; and
- dashboard reads that never mutate the observation store.

A manual paper check should then verify one complete market session, including
the expected records for that session's frozen universe version (12 for the
initial cohort) and delayed enrichment coverage. No strategy integration is
part of that check.

### Existing-code boundaries

- `data.market_calendar.trading_sessions()` currently preserves session dates
  only. The collector needs a focused calendar adapter that retains Alpaca's
  timezone-aware scheduled open; it must not infer 09:30 or change the cache-gap
  helper's established return contract.
- `data.fetcher.fetch_latest_quote()` is an execution-arrival benchmark with
  execution-specific age/spread rejection and an `ALPACA_DATA_FEED` switch. It
  must not be reused as the observation record model. A focused module under
  `data/` should retain raw normalized quote and trade fields even when they are
  stale, wide, one-sided, or otherwise partial.
- Persistence belongs under `reporting/` and in
  `data/market_observations.db`, separate from `TradeLogger` and the trade
  ledger.
- The current `post_cycle_hook` is occupied by `HealthReviewScheduler` and full
  strategy scans are not a timing guarantee. Scheduler selection is an explicit
  implementation decision and must satisfy the checkpoint deadline tests above
  without delaying the trading engine.

## 11. Explicitly deferred

- streaming collection;
- overnight/BOATS data;
- activating additional US stocks or ETFs before reviewing initial-cohort data
  quality (the collector remains designed to support them);
- index values, options, crypto, non-US instruments, and non-equity sessions;
- earnings-cluster observations;
- alerts based on gap magnitude;
- composite scores;
- historical outcome studies; and
- any observation-to-action integration.
