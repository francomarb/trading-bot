# Runtime cache and cycle-latency audit

**PLAN item:** `11.71`

**Audit date:** 2026-09-20

**Scope:** current paper engine startup, market-data caches, synchronous runtime
lookups, cycle scheduling, and open-position evaluation order. This document is
an audit and proposed design only; it does not authorize or implement runtime
changes.

## 1. Decision summary

The local Parquet cache is not the main latency problem. It is small, intact,
and fast to read. The material risks are orchestration and network boundaries:

1. Sector hydration runs before the stream, broker reconciliation, ownership
   restore, and protective-stop repair. Its advertised per-symbol timeout is
   not enforced, so an unrelated Yahoo metadata lookup can indefinitely delay
   safety-critical startup work.
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

Recommendation: approve a staged implementation that first establishes safety
ordering and telemetry, then adds atomic cache contracts and explicit prewarming,
then changes scheduling and eliminates redundant daily/API work. Keep execution
settlement latency as a separately measured subproblem rather than hiding it in a
generic cache change. The implementation should reuse Alpaca's native multi-symbol
bar requests, its official trade-update stream, Python's atomic file replacement,
and established TTL/locking utilities rather than creating parallel versions of
those facilities.

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
- The latest warm-ish sector startup hydrated 31 missing classifications in about
  nine seconds with no failures. That is a healthy observation, not a bound: the
  code does not enforce the configured ten-second per-symbol timeout.

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
  bar retry loop permits five 30-second HTTP attempts and also sleeps after the
  final failed attempt, adding an unnecessary final backoff.
- Multiple extreme cycles contained 240-second order-confirmation waits. These
  waits, rather than cache reads, explain much of the worst tail.

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

## 3. Runtime data-path inventory

| Path | Source of truth and key | Current freshness / retry contract | Durability and failure posture | Assessment |
|---|---|---|---|---|
| Broker snapshot, orders, positions | Alpaca trading API; broker identifiers | Startup and every cycle; broker client semantics | Broker is authoritative; failures skip decisions | Correct safety source, but it is reached only after pre-engine sector hydration during startup |
| Sector classification | Yahoo metadata; JSON keyed only by broker symbol, with manual overrides first | No TTL or provider/schema version; two nominal retries; configured timeout unused | Whole JSON written directly after full loop; corrupt JSON becomes empty cache; missing sector degrades filters to unmapped/neutral behavior | Unbounded startup dependency, stale forever, and loses partial progress |
| OHLCV bars | Alpaca data API; `(feed, symbol, timeframe, adjustment)` Parquet plus coverage JSON | Latest interval deliberately re-fetched; a local five-attempt wrapper sits outside alpaca-py's own 429/504 retry loop; 30 s HTTP default | Per-symbol progress survives; Parquet then metadata are direct writes; no lock; bad metadata triggers refetch, bad Parquet raises | Provenance key is good; nested retries can multiply latency, while native multi-symbol requests are unused |
| Regime SPY | OHLCV cache plus in-memory SPY/regime result | 600 s TTL; failure advances TTL and reuses stale data; cold failure blocks entries | Memory only; restart cold; no maximum stale age | Safe cold posture, but stale age and retry suppression are not explicit enough |
| Sector ETF gauge | OHLCV cache plus per-ETF and per-score memory caches | 600 s TTL; stale reused after failure and TTL reset | Memory only; cold failure returns neutral | Lazy calls can enter the symbol loop; no age ceiling or provenance in the decision record |
| Earnings blackout | Yahoo `info`, `calendar`, and `earnings_dates`; per-filter-instance symbol/date cache | Once per calendar day per strategy instance; no explicit timeout/retry | Memory only; duplicate work across SMA/Donchian; failure allows entry | A large synchronous daily burst is proven in logs; restart repeats it |
| IV proxies | Yahoo VIX/VXN/RVX history; source/date memory cache shared by options strategies | Once per calendar day; no explicit timeout/retry | Memory only; stale memory reused, otherwise fixed fallback | Sharing is good, but restart and request bounds are weak; preserve existing strategy failure semantics unless separately approved |
| Option contracts | Alpaca trading API; live query by underlying, DTE/type/strike band | Up to ten pages per candidate evaluation; no local deadline/cache | Not cached; exception returns no candidate | Live chain data should not be persisted as decision truth, but the call needs a bounded request budget and timing metrics |
| OPRA quotes | Alpaca option snapshots; live OCC-symbol batch | Fresh request for selected contracts/exit legs | Not cached; invalid/missing quote skips pricing/action | Correct not to reuse stale execution quotes; add deadline/age/latency metrics |
| Stock arrival quote | Alpaca latest IEX/SIP quote; symbol | Requested only at execution benchmark time; rejects stale/wide/invalid quote | Repeat timestamp memory only; rejected quote falls back to non-execution-quality benchmark | Not a prewarm target; separately time and bound it |
| Health-review benchmarks | Alpaca SIP bars through the same durable cache | Weekly/monthly/long-window post-cycle hook | Per-symbol durable cache | Runs outside reported cycle duration but before the five-minute sleep, so it can silently extend start-to-start cadence |
| Order confirmation | Alpaca stream then REST fallback | Can synchronously wait up to roughly 240 s per order | Broker/lifecycle reconciliation provides recovery | Not a cache, but a proven dominant tail and must be separately visible |

## 4. Proposed target design

### 4.1 Safety-first startup

Startup must have two explicit stages:

1. **Safety bootstrap (blocking):** start the trade-update stream, obtain the
   broker snapshot, restore/reconcile ownership and lifecycle state, synchronize
   option and stop state, and repair missing protective stops. No Yahoo or
   historical prewarm work may run before this completes.
2. **Readiness/prewarm (bounded):** validate cache manifests, refresh required
   completed bars, sector classifications, earnings, IV proxies, and sector ETFs
   under explicit per-provider and total deadlines. Persist each successful key
   immediately. A missed readiness deadline blocks only the entry capability
   whose existing failure policy requires it; it must not undo broker safety
   bootstrap or prevent ongoing position management.

Emit distinct `safety_ready_at`, `entry_data_ready_at`, and degraded-component
states. “Engine started” must not conflate broker safety with optional entry-data
readiness.

### 4.2 Two-pass cycle ordering

Keep the existing broker/lifecycle/stop-repair prelude. Then evaluate:

1. owned open positions in their owning slots, including their signal assets;
2. symbols with working exit/entry orders where evaluation is needed;
3. flat entry candidates in allocator priority and deterministic watchlist order.

Do not simply move every ticker that appears in the broker snapshot to the front:
ownership identity must select the correct slot, and unmanaged positions must
remain explicitly unmanaged. De-duplicate shared data fetches without merging
strategy decisions.

Target service levels to validate in paper testing:

- broker reconciliation and stop repair complete within 15 s p95;
- every managed open position begins evaluation within 30 s p95 and 60 s max
  when its required provider is responsive;
- flat-symbol work is shed or deferred before an open-position deadline is
  violated.

These are proposed initial budgets, not claims about current behavior.

### 4.3 Completed-bar snapshot and prewarming

Introduce a run-scoped market-data coordinator keyed by
`(feed, symbol, timeframe, adjustment, decision_cutoff)`:

- compute the latest *completed* decision bar once per timeframe/session;
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

The coordinator should report cache hit, API range count, batch size, rows, age,
provider latency, and stale/fallback status per key. Do not build a parallel
per-symbol HTTP worker fleet while Alpaca's batch request covers the use case.
Additional concurrency may be considered only after batching is measured and
per-key single-writer locks and atomic writes exist.

### 4.4 Durable cache contract

For bars:

- write Parquet to a same-directory temporary file and atomically replace it;
- write metadata last through its own temporary-file replacement, so metadata
  can never claim data that was not committed;
- include schema version, feed/provider, adjustment, timeframe, written-at,
  coverage, last complete bar, and a data fingerprint/generation identifier;
- catch unreadable Parquet, quarantine the bad pair, and refetch instead of
  repeatedly failing the symbol;
- use a per-key process/file lock or enforce a single cache-writer service;
- retain backward-compatible reads and provide a rollback/quarantine procedure.

For small JSON caches:

- use schema-versioned atomic writes;
- persist each successful sector result immediately;
- record provider, fetched-at, and positive/negative TTLs;
- preserve manual overrides as the highest-priority layer;
- move earnings and IV series to bounded durable caches so restart does not
  recreate a market-open Yahoo burst.

Use established primitives for the generic pieces: Python `os.replace()` for
same-filesystem atomic replacement, `cachetools.TTLCache` for uncomplicated
in-memory TTL caches, and a maintained cross-process file-lock package if live
and research processes must share a writable cache root. Any imported package
must be declared directly and pinned; the current transitive availability of
`cachetools` or `tenacity` is not a dependency contract. Prefer separate
single-writer cache roots where that removes the need for locking entirely.

Changing an entry gate's fail-open/fail-closed meaning is outside this work.
Cache hardening must preserve current strategy semantics unless a separate
strategy review approves a change.

### 4.5 Bounded provider policy

Create one provider-policy layer rather than scattered timeout patches:

- explicit connect/read timeout, maximum attempts, total elapsed budget, jittered
  backoff, and retryable exception/status list per provider;
- never sleep after the last failed attempt;
- distinguish `fresh`, `stale_usable`, `fallback`, and `unavailable` results;
- retain the age of stale data rather than resetting its apparent freshness when
  a refresh fails;
- circuit-break repeated outages so 137 symbols do not independently discover
  the same provider failure;
- cap a prewarm batch and carry unfinished flat symbols to a later cycle.

Before adding any wrapper retry, account for the provider SDK's behavior. The
installed alpaca-py client already retries HTTP 429 and 504 responses internally;
the current outer five-attempt fetcher loop can therefore multiply attempts and
elapsed time. Prefer the supported SDK retry contract, with one outer *elapsed
deadline/circuit-breaker* boundary only where the SDK does not supply the needed
failure behavior. A generic retry helper may be used for idempotent reads, but
must never blindly retry order submission. Mutating broker calls continue to use
`client_order_id` plus broker reconciliation.

### 4.6 Non-overlapping fixed-rate scheduling

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
tick and next start at 09:40, not 09:42 and not immediately at 09:37.

### 4.7 Execution latency separation

Order confirmation is safety-sensitive and should not be casually shortened as
part of caching work. First split its latency from data/evaluation timing:

- emit submit-to-ack, ack-to-terminal, stream-wait, REST-fallback, and settlement
  durations;
- include order-wait time as a named cycle phase;
- measure how often a wait delays another owned position;
- then decide in a separate reviewed change whether asynchronous lifecycle
  settlement can preserve all substrate/reconciliation guarantees.

## 5. Proposed implementation sequence

### PR A — telemetry and safety ordering

- Move sector/Yahoo readiness behind broker safety bootstrap.
- Add phase timers and direct managed-position evaluation latency.
- Add two-pass owned-position-first ordering with deterministic tests.
- Add scheduled-start/overrun metrics without changing cadence yet.
- Add deterministic slow/failing-provider and slow-order tests.

This is the highest-value, lowest-semantic-change step.

### PR B — atomic caches and prewarm coordinator

- Add atomic/versioned bar and JSON persistence plus corrupt-pair quarantine.
- Add per-key single-writer protection with a maintained lock primitive, or
  isolate writable cache roots so cross-process locking is unnecessary.
- Add durable sector, earnings, and IV freshness records.
- Add completed-bar keys, shared run-scoped snapshots, and bounded Alpaca-native
  multi-symbol prewarm.
- Remove nested retry multiplication and enforce provider-level elapsed budgets.
- Preserve every existing strategy gate and feed choice.

### PR C — cadence and redundant-request removal

- Enable daily-session-aware refresh instead of five-minute daily overlap calls.
- Enable fixed-rate, skip-missed-tick scheduling.
- Add bounded flat-candidate carryover when a provider budget is exhausted.
- Calibrate alert thresholds from at least five market sessions.

### Separate execution-latency decision

- Use the new phase telemetry to assess synchronous order confirmation.
- If material after cache/scheduler work, design asynchronous settlement against
  the lifecycle substrate in its own PR and failure-injection review.

## 6. Verification and rollout gates

Each implementation PR must include offline deterministic tests for timeout,
429/5xx, connection reset, corrupt Parquet/JSON, interruption between data and
metadata commit, concurrent same-key requests, stale fallback, partial prewarm,
cycle overrun, and slow order confirmation.

Before enabling the completed behavior in regular paper hours:

1. snapshot/backup cache metadata and document rollback;
2. run the full unit suite;
3. run an off-hours cold-cache prewarm against a disposable cache root;
4. recycle only via `./recycle_bot.sh` and verify broker ownership/stops become
   ready before metadata hydration;
5. verify one market-open session with entries paused;
6. collect at least five sessions of phase and position-latency metrics;
7. confirm no strategy signal/fill drift attributable to changed bar cutoffs;
8. only then enable normal entry operation.

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
