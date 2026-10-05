# Runtime cache and cycle-latency audit

**PLAN item:** `11.71`

**Audit date:** 2026-09-20; focused reliability implementation 2026-10-05

**Scope:** current paper engine startup, market-data caches, synchronous runtime
lookups, cycle scheduling, and open-position evaluation order. This document
records the audit, the approved target design, and the implemented focused
reliability slices.

## 1. Decision summary

The local Parquet cache is not the main latency problem. It is small, intact,
and fast to read. The material risks are orchestration and network boundaries:

1. At audit time, sector hydration ran before the stream, broker reconciliation,
   ownership restore, and protective-stop repair. This PR moves classification
   refresh and the sector-ETF heat snapshot after that safety bootstrap. The
   sector refresh after that safety bootstrap. The resolver now enforces a real
   per-symbol deadline plus a 30-second total startup budget and stops after one
   abandoned provider worker, so optional metadata cannot accumulate hung calls.
2. The five-minute setting is a post-cycle sleep. A seven-minute cycle therefore
   starts the next cycle about twelve minutes after the prior start.
3. Daily strategies re-request the newest daily interval on every five-minute
   cycle even though decisions use the prior completed daily bar. A representative
   healthy 167-item cycle made approximately 172 bar API range requests.
4. Open positions are not explicitly evaluated ahead of flat candidates.
   Protective-stop repair and broker/lifecycle reconciliation already run first,
   which limits the risk, but signal-based exits can still wait behind metadata,
   data refreshes, flat-symbol evaluations, and synchronous order confirmation.
5. The longest observed cycles are mixed-origin. Cold/daily refresh work matters,
   but several extreme tails were dominated by one or more synchronous order-fill
   waits. A cache-only patch would not fix the largest cycle overruns.
6. Several caches are memory-only or written non-atomically. Restarting discards
   earnings, IV, regime, and sector-score freshness; an interrupted sector hydrate
   loses the whole batch, while an interrupted bar write can leave Parquet and its
   coverage sidecar out of sync.

The first slice fixes sector-classification staleness and durability:
successful Yahoo results carry provider/schema/fetch provenance, become eligible
for refresh after 90 days, and are refreshed oldest-first under a ten-attempt
startup budget. Missing classifications take priority, manual overrides remain
authoritative, a failed refresh preserves the last known classification, and every
attempt is atomically persisted before continuing. Legacy rows without provenance
are treated as stale and migrate gradually through the same bounded budget.

The focused reliability slice now also applies one connect/read timeout policy to
every runtime Alpaca REST client, leaves alpaca-py's native 429/504 retries in
charge, limits the outer retry to safe reads, and never blindly retries a write.
An ambiguous equity, single-leg-option, or MLEG entry/close submit remains
`UNKNOWN`, retains ownership, and is recovered by its durable `client_order_id`.
Each MLEG walk rung publishes its client ID before the write, so recovery targets
the exact attempted order. Bar files are written through same-directory atomic replacement;
an unreadable or structurally invalid Parquet/metadata pair is moved to quarantine
and refetched from the same feed.

Runtime logs now separate active time from wall/suspended time and report startup
safety readiness, metadata delay, cycle start lag, major cycle phases, physical
HTTP attempts and failures, the trailing-60-second attempt count, slow endpoints,
direct managed-position evaluation time, post-cycle hooks, and stream-versus-REST
order waits. This observation layer does not shorten the safety-sensitive order
confirmation window or change signal behavior.

## 2. Evidence and baseline

### 2.1 Current universe and cache state

The deployed configuration contains 217 slot evaluations per cycle: 56 SMA,
53 RSI (including protected members), 101 Donchian (100 ranked plus one
protected member), one SPY-options signal, four leveraged-trend trade assets,
and two credit-spread underlyings. Leveraged trend also reads four signal assets.
There are 143 unique active slot symbols, or 145 unique symbols when those
separate signal assets are included. Sector hydration covers 137 unique
SMA/RSI/Donchian equities.

At audit time:

- `data/historical` was about 19 MB and contained 374 feed-aware Parquet files
  with 374 matching metadata files.
- No feed-aware Parquet/metadata orphan pairs were found, and sampled full
  validation found no unreadable required Parquet or malformed required metadata.
- Reading all 145 cached IEX daily Parquet files sequentially took about 0.13 s.
- All required option-signal, leveraged trade/signal, and sector-ETF caches were
  present.
- 34 of 139 required main IEX daily series were not yet cached. These are chiefly
  the newly deployed universe members and will otherwise become cold work during
  the first market-open sweep.
- At audit time, the latest warm-ish sector startup hydrated 31 missing classifications in about
  nine seconds with no failures. That is a healthy observation, not the configured
  bound: yfinance bounds each underlying request at 30 seconds, but the resolver's
  configured ten-second resolver timeout was then unused. The focused reliability
  slice now enforces it and the total startup budget.

### 2.2 Observed cycle latency

The following is derived from local structured logs from 2026-08-20 through
2026-09-18. It excludes market-closed cycles. The new 217-evaluation configuration
was deployed on a weekend and therefore has no market-open baseline yet.

| Evaluations | Cycles | Median | p95 | Maximum | Over 60 s | Over 300 s |
|---:|---:|---:|---:|---:|---:|---:|
| 132 | 375 | 16.3 s | 39.1 s | 376.0 s | 1.6% | 0.8% |
| 136 | 400 | 14.5 s | 99.2 s | 388.4 s | 5.2% | 0.5% |
| 167 | 179 | 17.1 s | 30.5 s | 733.8 s | 4.5% | 1.1% |

For the 167-evaluation cohort, cycles without a new fill had a 17.0 s median
and 26.1 s p95. This confirms that normal warm operation is comfortably below
five minutes, but it does not provide a deadline guarantee.

Across 903 same-session start intervals from these cohorts, the indicative
median was 316.5 s and p95 was 497.8 s. The existing logs do not carry a formal
scheduled-start timestamp, so this is evidence of drift rather than a complete
scheduler service-level measurement.

Representative log inspection found:

- Healthy 167-item cycles commonly issued about 172 bar API range requests and
  still completed in the teens because the calls were fast.
- A daily earnings-cache refresh produced 108 synchronous Yahoo refreshes and a
  roughly 164 s cycle.
- Repeated SPY 504 responses produced market-closed cycles around 184 s. The
  local bar wrapper understates its real retry count: each of its five outer
  attempts can contain four alpaca-py attempts, with three SDK sleeps, and the
  outer wrapper also sleeps after its final failure. With 30-second request
  timeouts, the code-level upper bound is approximately 676 seconds (about
  11 minutes) for one symbol. That is arithmetic from the installed retry
  contracts, not a measured production duration.
- Multiple extreme cycles contained 240-second order-confirmation waits. These
  waits, rather than cache reads, explain much of the worst tail.

The request burst is also close to the account's provider ceiling. Alpaca's
current free market-data plan documents 200 API calls per minute; a representative
healthy cycle emitted approximately 172 bar API ranges in 17 seconds before
quotes, option-chain requests, pagination, or retries. The deployed 217-evaluation
universe can therefore self-induce 429 responses even while latency looks healthy.
The narrow implementation must measure actual HTTP requests per rolling minute,
not only logical fetch ranges. Source: <https://alpaca.markets/data>.

### 2.3 Position-to-evaluation latency

Current telemetry does not emit a first-class `position_to_evaluation_ms`
metric. A reconstruction from complete 167-item cycles found 215 log-visible
open-position evaluations:

- in cycles completing within 60 s: median 0.7 s, p95 1.0 s, maximum 1.4 s;
- across all cycles: median 0.8 s, p95 499.8 s, maximum 578.2 s.

The fast normal result is accidental placement in the current slot/watchlist
order, not an invariant. The tail shows that an earlier symbol's synchronous
execution wait can delay a later open position. The reconstruction also cannot
see every exit path, so the implementation must add a direct metric before this
can become an operational service level.

### 2.4 Controlled interruption result

An isolated temporary-directory failure probe confirmed the code-level analysis:

- interrupting sector hydration after two successful lookups left zero new
  entries in the durable JSON file because the entire batch is saved only after
  the loop;
- interrupting a bar-cache write between Parquet and metadata left the new
  Parquet present with no sidecar. The next read can recover by refetching, but
  the pair is not transactional and a corrupt Parquet read is not caught.

No production cache was altered for these probes.

### 2.5 Post-promotion observation through 2026-10-03

After the SMA durable pool increased the engine to 260 slot-symbol evaluations,
85 market-open cycles completed at 27.6 seconds median and 47.7 seconds p95.
Four exceeded 60 seconds and two exceeded 300 seconds; the maximum was 485.2
seconds. No genuine provider 429 appeared. Normal warmed operation therefore
remains acceptable, while the long tail still supports the bounded-retry,
deadline, and telemetry work below. These statistics cover only cycles that
ran; they do not establish continuous market-hour coverage.

Every post-promotion session through 2026-10-02 had substantial gaps in which
no engine cycle started. PR #171 was deployed after market on 2026-10-01, so it
could directly flag only the Oct 2 gaps:

| Session | Market-open cycles | Market minutes inside gaps over 15 minutes |
|---|---:|---:|
| 2026-09-29 | 11 | 322 / 390 |
| 2026-09-30 | 24 | 266 / 390 |
| 2026-10-01 | 10 | 333 / 390 |
| 2026-10-02 | 40 | 167 / 390 |

The macOS power log resolves these historical gaps: it records repeated idle
sleep and dark-wake periods overlapping the missing cycles, primarily while the
laptop was on battery. Stream messages during some gaps came from brief
maintenance wakes and do not demonstrate continuous runtime. After the full
wake on October 2, the engine detected the wall-clock gap, completed one slow
recovery cycle, and then returned to its normal five-minute cadence through the
close. No cycle-scheduler defect was demonstrated.

The launcher previously used `caffeinate -s`, whose system-sleep assertion is
valid only on AC power. It now uses `caffeinate -i -s`: `-i` prevents ordinary
idle sleep on battery or AC, while `-s` retains the stronger AC assertion.
Clamshell sleep, depleted power, deliberate sleep or shutdown, and connectivity
loss remain accepted limitations of a laptop paper runtime. Wall-clock gap
detection continues to disclose their effect on evidence coverage; production
availability belongs to the deferred VPS/systemd work, not to this local audit.

## 3. Runtime data-path inventory

| Path | Source of truth and key | Current freshness / retry contract | Durability and failure posture | Assessment |
|---|---|---|---|---|
| Broker snapshot, orders, positions | Alpaca trading API; broker identifiers | Startup and every cycle; broker client semantics | Broker is authoritative; failures skip decisions | Safety bootstrap now completes before optional sector metadata and heat work |
| Sector classification | Yahoo metadata; JSON keyed by broker symbol, with manual overrides first | 90-day TTL; ten lookups maximum; 10 s per-symbol and 30 s total elapsed deadlines | Each result is atomically persisted; failed refresh retains the last known value; one timed-out daemon lookup stops the pass; missing sector keeps existing neutral behavior | Startup is bounded without moving optional work ahead of broker safety |
| OHLCV bars | Alpaca data API; `(feed, symbol, timeframe, adjustment)` Parquet plus coverage JSON | Latest interval deliberately re-fetched; alpaca-py owns 429/504 retries; at most two safe-read attempts for uncovered transport/other 5xx failures; 5 s connect / 20 s read ceiling per physical request | Same-directory temporary writes publish data then metadata; unreadable/invalid pairs move to `data/historical/quarantine/` and refetch on the same feed | Retry multiplication and repeated corrupt-file failures are removed; native multi-symbol requests remain event-gated |
| Regime SPY | OHLCV cache plus in-memory SPY/regime result | 600 s TTL; failure advances TTL and reuses stale data; cold failure blocks entries | Memory only; restart cold; no maximum stale age | Safe cold posture, but stale age and retry suppression are not explicit enough |
| Sector ETF gauge | OHLCV cache plus per-ETF and per-score memory caches | 600 s TTL; stale reused after failure and TTL reset | Memory only; cold failure returns neutral | Lazy calls can enter the symbol loop; no age ceiling or provenance in the decision record |
| Earnings blackout | Yahoo `info`, `calendar`, and `earnings_dates`; per-filter-instance symbol/date cache | Once per calendar day per strategy instance; no explicit timeout/retry | Memory only; duplicate work across SMA/Donchian; failure allows entry | A large synchronous daily burst is proven in logs; restart repeats it |
| IV proxies | Yahoo VIX/VXN/RVX history; source/date memory cache shared by options strategies | Once per calendar day; no explicit timeout/retry | Memory only; stale memory reused, otherwise fixed fallback | Sharing is good, but restart and request bounds are weak; preserve existing strategy failure semantics unless separately approved |
| Option contracts | Alpaca trading API; live query by underlying, DTE/type/strike band | Up to ten pages per candidate evaluation; no local deadline/cache | Not cached; exception returns no candidate | Live chain data should not be persisted as decision truth, but the call needs a bounded request budget and timing metrics |
| OPRA quotes | Alpaca option snapshots; live OCC-symbol batch | Fresh request for selected contracts/exit legs | Not cached; invalid/missing quote skips pricing/action | Correct not to reuse stale execution quotes; add deadline/age/latency metrics |
| Stock arrival quote | Alpaca latest IEX/SIP quote; symbol | Requested only at execution benchmark time; rejects stale/wide/invalid quote | Repeat timestamp memory only; rejected quote falls back to non-execution-quality benchmark | Not a prewarm target; separately time and bound it |
| Health-review benchmarks | Alpaca SIP bars through the same durable cache | Weekly/monthly/long-window post-cycle hook | Per-symbol durable cache | Separately timed; still extends start-to-start cadence before sleep |
| Order confirmation | Alpaca stream then REST fallback | Can synchronously wait up to roughly 240 s per order | Broker/lifecycle reconciliation provides recovery | Stream, REST fallback, and total wait are now explicit; asynchronous settlement remains a separate design |

## 4. Proposed target design

### 4.1 Safety-first startup

Startup must have two explicit stages:

1. **Safety bootstrap (blocking):** start the trade-update stream, obtain the
   broker snapshot, restore/reconcile ownership and lifecycle state, synchronize
   option and stop state, and repair missing protective stops. No Yahoo or
   historical prewarm work may run before this completes.
2. **Sector hydration (bounded):** after safety bootstrap, resolve missing sector
   classifications under the implemented lookup-count budget and atomically
   persist each attempt. Real per-symbol and total elapsed deadlines are now
   enforced. A metadata failure cannot undo completed
   broker safety bootstrap. Broader bars/earnings/IV prewarming remains
   event-gated; the sector-ETF heat snapshot is part of this post-safety hook.

The implementation emits separate `safety_ready` and optional-metadata durations,
so “engine started” no longer hides which stage consumed time. A structured
degraded-component state belongs with any later durable Yahoo/prewarm design.

### 4.2 Candidate two-pass cycle ordering — not yet authorized

Keep the existing broker/lifecycle/stop-repair prelude. Then evaluate:

1. owned open positions in their owning slots, including their signal assets;
2. symbols with working exit/entry orders where evaluation is needed;
3. flat entry candidates in allocator priority and deterministic watchlist order.

Do not simply move every ticker that appears in the broker snapshot to the front:
ownership identity must select the correct slot, and unmanaged positions must
remain explicitly unmanaged. De-duplicate shared data fetches without merging
strategy decisions.

If telemetry shows flat work is delaying owned positions, initial service levels
to evaluate would be:

- broker reconciliation and stop repair complete within 15 s p95;
- every managed open position begins evaluation within 30 s p95 and 60 s max
  when its required provider is responsive;
- flat-symbol work is shed or deferred before an open-position deadline is
  violated.

These are hypotheses, not claims about current behavior and not acceptance
criteria for the narrow first implementation. Normal observed position latency is
already sub-second; order waits, rather than watchlist ordering, explain the tail.

### 4.3 Candidate completed-bar batching and prewarming — event-gated follow-up

If the request-volume event gate is met, prefer a small run-scoped market-data
batch keyed by `(feed, symbol, timeframe, adjustment, decision_cutoff)`:

- compute the latest *completed* decision bar once per timeframe/session. A daily
  session becomes eligible only after the exchange calendar's close plus a
  configured settlement buffer; the prior session must then be overlap-fetched
  once at its first eligible refresh so late/revised bars remain repairable;
- prewarm required keys with Alpaca's native multi-symbol
  `StockBarsRequest.symbol_or_symbols` in bounded chunks grouped by identical
  feed, timeframe, adjustment, and missing range;
- split each batch response back into the existing per-symbol cache keys so one
  missing/bad symbol cannot invalidate the other symbols' durable progress;
- return one immutable frame/snapshot to every consumer during that cycle;
- share SPY, sector ETFs, leveraged signal assets, and overlapping symbols;
- refresh daily strategies once when a new completed daily session is available,
  not every five minutes while the current daily bar is incomplete;
- continue refreshing the SPY 5-minute option signal on its five-minute boundary;
- never reuse a live quote or option snapshot as historical decision truth.

That follow-up should report cache hit, API range count, batch size, rows, age,
provider latency, and stale/fallback status per key. Do not build a parallel
per-symbol HTTP worker fleet while Alpaca's batch request covers the use case.
Additional concurrency may be considered only after batching is measured and
per-key single-writer protection is proven necessary. Do not advance coverage
when the settlement refresh returns an empty or partial response.

### 4.4 Durable cache contract

For bars:

- write Parquet to a same-directory temporary file and atomically replace it;
- write metadata last through its own temporary-file replacement, so metadata
  can never claim data that was not committed;
- catch unreadable Parquet, quarantine the bad pair, and refetch instead of
  repeatedly failing the symbol;
- retain backward-compatible reads and provide a rollback/quarantine procedure.

Those atomic-write and quarantine changes are cheap and belong in the narrow
implementation. Versioned manifests, generation fingerprints, and generalized
cross-process locks are deferred unless a real concurrent-writer or migration
failure demonstrates the need.

For small JSON caches:

- sector results now carry provider/schema/fetch metadata, refresh after 90 days,
  and migrate legacy rows through a ten-attempt oldest-first startup budget;
- each sector lookup result is atomically persisted immediately; a failed refresh
  retains the last known classification and records the attempt so failures cannot
  monopolize every later refresh budget;
- manual overrides remain the highest-priority layer and are not provider-refreshed;
- the budget intentionally makes recovery from an empty/corrupt cache or a large
  watchlist addition gradual across restarts; unresolved mappings fail open under
  the current strategy policy while old valid mappings remain usable;
- consider durable earnings and IV series only if telemetry shows their daily
  refresh burst remains operationally material.

Use established primitives for the generic pieces: Python `os.replace()` for
same-filesystem atomic replacement, `cachetools.TTLCache` for uncomplicated
in-memory TTL caches if a direct pinned dependency is justified, and a maintained
cross-process file-lock package only if live and research processes must share a
writable cache root. The current transitive availability of `cachetools` or
`tenacity` is not a dependency contract. Prefer separate single-writer cache
roots where that removes the need for locking entirely.

Changing an entry gate's fail-open/fail-closed meaning is outside this work.
Cache hardening must preserve current strategy semantics unless a separate
strategy review approves a change.

### 4.5 Bounded provider policy

The focused implementation uses two contained policies: a real per-symbol/total
deadline for sector hydration, and one bounded retry layer for idempotent Alpaca
reads outside the SDK's native coverage. It never sleeps after the last failed
attempt and emits actual physical attempt counts. A generic provider framework, stale-result type,
circuit breaker, or flat-symbol carryover queue is deferred until an observed
failure requires it.

Before adding any wrapper retry, account for the provider SDK's behavior. The
installed alpaca-py client already retries HTTP 429 and 504 responses internally.
The former outer five-attempt loop could multiply those attempts and is removed.
The SDK contract remains authoritative, with a small outer retry only for safe
reads and uncovered transport/other 5xx failures. The bot never blindly retries
order submission. Equity, single-leg-option, and MLEG workers make one bot-level
attempt; transport, 429, and 5xx ambiguity keeps the durable row non-terminal
until exact `client_order_id` reconciliation proves whether Alpaca accepted it.

### 4.6 Candidate non-overlapping fixed-rate scheduling — deferred

Retain a single engine thread and prohibit overlapping cycles. Replace the
post-cycle fixed delay with monotonic scheduled starts:

- schedule nominal starts every five minutes;
- after an overrun, skip missed ticks and wait for the next future boundary;
- never launch catch-up cycles back-to-back;
- keep operator heartbeat/stop handling independent and interruptible;
- measure `scheduled_start`, `actual_start`, `start_lag`, `duration`,
  `overrun`, `skipped_ticks`, and `next_scheduled_start`;
- include post-cycle hook time in start-to-start telemetry even if it remains
  outside core cycle duration.

These are standard scheduler semantics (`max_instances=1`, coalesce missed runs,
and apply a misfire deadline), but the critical engine does not need a general
job scheduler to obtain them. Prefer a small, directly tested monotonic-deadline
loop so shutdown, operator commands, lifecycle state, and ordering stay on the
existing engine thread. APScheduler is suitable for non-critical reports or
prewarm jobs if persistent scheduling later becomes necessary; do not introduce
it into the order-management path merely to replace a few deadline calculations.

For example, a cycle starting at 09:30 and ending at 09:37 would skip the 09:35
tick and next start at 09:40, not 09:42 and not immediately at 09:37. This is not
authorized by the audit: five strategy identities trade completed daily bars,
and the one five-minute sleeve has not yet shown that schedule drift changes
outcomes. First emit scheduled-start lag and overrun telemetry.

### 4.7 Execution latency separation

Order confirmation is safety-sensitive and is not shortened here. The focused
slice emits stream wait, REST fallback, and total synchronous wait while the cycle
also reports strategy-evaluation and owned-position latency. If that evidence
shows repeated material blocking, a separate asynchronous-settlement proposal
must add submit-to-ack/ack-to-terminal/settlement detail and prove every lifecycle,
stop-protection, and restart-reconciliation guarantee before behavior changes.

## 5. Implemented boundary and follow-ups outside this PR

### Focused reliability PR

- Refresh stale sector classifications under a bounded
  per-startup lookup budget; add provider/schema/fetched-at provenance; preserve
  stale-on-error behavior; fairly rotate failed missing/stale entries; atomically
  persist each attempt; and move classification plus heat refresh behind broker
  reconciliation, ownership restore, lifecycle reconciliation, and protective-stop
  repair.
- Enforce a real per-symbol and total hydration deadline; after one provider call
  outlives its deadline, stop the pass instead of accumulating stuck workers.
- Remove retry multiplication between the local fetcher and alpaca-py, and never
  sleep after the last failed attempt. Retain one explicitly bounded policy for
  idempotent reads.
- Catch unreadable Parquet, quarantine the corrupt data/metadata pair, and refetch
  without changing feed or strategy semantics.
- Apply the shared timeout/attempt adapter to runtime trading, stock-data, option-
  data, and calendar clients. Emit phase durations, physical HTTP attempts and
  failures, rolling-minute volume, slow endpoints, order-wait time, start lag,
  active-versus-suspended time, and direct managed position-to-evaluation latency.
- Preserve ambiguous equity, single-leg-option, and MLEG entry/close submissions
  as non-terminal. Publish each spread-walk rung's exact client ID before submit,
  retain ownership/close locks, and reconcile against Alpaca rather than retrying
  or treating an uncertain write as rejection.
- Add deterministic slow/hung Yahoo, 429/504, corrupt-Parquet, interruption, and
  slow-order tests.

No cycle reordering, scheduler change, cache coordinator, generalized locking,
earnings/IV persistence, or asynchronous order settlement is included.

### Explicit follow-ups outside this PR

- **SDK upgrade / stream review:** test alpaca-py 0.44 separately, including its
  newer stream reconnect/data-timeout behavior, before replacing the bot's proven
  heartbeat, backoff, and resync layer. Remove the local REST adapter only if a
  future public client constructor exposes equivalent request timeouts.
- **Request reduction:** use Alpaca's native multi-symbol bar request only if the
  new rolling counter approaches the account limit, a real 429 occurs, or measured
  duplicate refresh work is material. Add per-key logical-range/cache-hit summaries
  with that batching change rather than another HTTP wrapper.
- **Yahoo metadata:** move earnings/IV refresh to shared durable prewarm only if
  the new phase/endpoint logs show it remains a material recurring delay. Preserve
  each strategy's current fail-open/fallback semantics in that separate design.
- **Cycle scheduling and evaluation order:** consider fixed-rate non-overlapping
  starts or owned-position-first processing only after start-lag and owned-position
  telemetry demonstrate a decision-relevant breach.
- **Order settlement:** consider asynchronous equity settlement only after the
  order-wait evidence shows repeated material blocking and a separate proposal
  proves lifecycle, stop-protection, and restart-reconciliation safety.

### Event gate: native batching and completed-daily refresh

Proceed only after the new counter measures the true burst, or a 429 occurs. A
rolling-minute request count approaching the documented 200/minute Basic-plan
limit is sufficient evidence; latency need not be bad first. Use Alpaca-native
multi-symbol requests and the explicit settled-session overlap rule in §4.3.

### Other event-gated decisions

- Consider owned-position-first ordering only when flat work—not an earlier order
  wait—causes a managed-position latency breach.
- Consider fixed-rate scheduling only when measured start lag misses a relevant
  five-minute decision boundary.
- Add locks/manifests only after a demonstrated concurrent-writer or migration
  failure.
- Consider asynchronous order settlement only after phase telemetry quantifies
  its frequency and a separate design proves lifecycle/reconciliation safety.

## 6. Verification and rollout gates

The focused slices include offline deterministic tests for freshness,
legacy migration, bounded fair selection, stale-on-error behavior, manual override
precedence, interrupted persistence, post-safety ordering, and hook failure
isolation, real sector deadlines, SDK-owned 429/504 behavior, connection retries,
no final retry sleep, corrupt-Parquet quarantine, interrupted atomic writes,
physical-attempt telemetry, and non-retried mutation ambiguity. Event-gated
follow-ups add tests for their own contracts rather than front-loading unused
infrastructure.

Before enabling the focused implementation in regular paper hours:

1. run the full unit suite;
2. prove with deterministic injection that an interrupted sector refresh preserves
   prior progress, 429/504 is not multiplied outside the SDK, safe transport errors
   stop at the configured attempt count, and mutations are not repeated;
3. prove corrupt Parquet is quarantined and the next fetch stays on the requested
   feed;
4. recycle only via `./recycle_bot.sh` and verify broker ownership/stops become
   ready before metadata hydration;
5. inspect the first normal cycle's startup, phase, request, position, and order-
   wait log fields and confirm no strategy signal or decision-bar semantics changed.

Paper entries do not need to be paused for a calendar day, and there is no
five-session bake requirement. Acceptance is tied to the named events and
invariants; ordinary paper evidence collection should continue.

The Donchian ranked pool remains capped at 100 throughout this work.

## 7. Reuse boundaries and primary references

The design intentionally distinguishes generic infrastructure from trading
policy:

- Alpaca `StockBarsRequest` supports a symbol or list of symbols:
  <https://alpaca.markets/sdks/python/api_reference/data/stock/requests.html>.
- Alpaca's official `TradingStream` supplies account trade updates:
  <https://alpaca.markets/sdks/python/api_reference/trading/stream.html>.
- Alpaca order requests expose `client_order_id` for caller identity and
  reconciliation:
  <https://alpaca.markets/sdks/python/api_reference/trading/requests.html>.
- alpaca-py's current retry constants are three retries, a three-second wait, and
  status codes 429/504:
  <https://github.com/alpacahq/alpaca-py/blob/master/alpaca/common/constants.py>.
- The concrete REST clients still lack a public request-timeout option; the open
  SDK issue documents that gap:
  <https://github.com/alpacahq/alpaca-py/issues/415>.
- APScheduler documents single-instance jobs, missed-start deadlines, and
  coalescing; these define the desired semantics even if the critical loop
  remains local:
  <https://apscheduler.readthedocs.io/en/master/userguide.html>.
- Python documents same-filesystem `os.replace()` as atomic:
  <https://docs.python.org/3.12/library/os.html#os.replace>.
- Retry/backoff requires bounded attempts and idempotent operations:
  <https://docs.aws.amazon.com/wellarchitected/latest/framework/rel_mitigate_interaction_failure_limit_retries.html>.

The remaining custom logic is necessarily domain-specific: completed-market-bar
cutoffs, IEX/SIP provenance, per-strategy stale/fallback policy, position-to-slot
ownership, and lifecycle reconciliation. Those rules cannot be delegated to a
generic cache or scheduler without losing trading semantics.
