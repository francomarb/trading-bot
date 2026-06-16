# Order Lifecycle State Machine — Discovery

Status: Discovery doc, no code changes yet. Revised in response to PR #59 review.
Purpose: Document the current state of `position_lifecycle` in code, surface the holes that PR #58 (Donchian stop-limit + hybrid residual) made visible, recommend the structural shape the foundation PR should take, and capture the process learning that explains why this wasn't caught earlier.

This doc is intentionally grounded in code. Where it makes a recommendation, it says so explicitly.

---

## 1. Current `position_lifecycle` schema

DDL lives in [engine/lifecycle.py:86-132](../engine/lifecycle.py:86); the migration is executed by [`TradeLogger._ensure_db()`](../reporting/logger.py:354).

```sql
CREATE TABLE position_lifecycle (
    position_uid            TEXT PRIMARY KEY,        -- pos_<uuid4_hex>, 38 chars
    schema_version          INTEGER NOT NULL DEFAULT 1,
    created_at              TEXT    NOT NULL,        -- lifecycle creation time, NOT first fill
    closed_at               TEXT,                    -- terminal-status time
    symbol                  TEXT    NOT NULL,        -- equity ticker OR primary OCC leg
    owner_key               TEXT    NOT NULL,        -- engine.positions.owner_key_for(symbol)
    strategy                TEXT    NOT NULL,
    position_type           TEXT    NOT NULL,        -- 'single_leg' | 'spread'
    status                  TEXT    NOT NULL,        -- see status set below
    entry_qty               REAL,                    -- decision.qty (intended primary qty)
    current_qty             REAL,                    -- 0.0 at pending, updated on fills/exits
    avg_entry_price         REAL,                    -- VWAP across entry fills
    net_realized_pnl        REAL    NOT NULL DEFAULT 0.0,
    entry_order_id          TEXT,                    -- accepted by create_pending; NEVER populated
    entry_client_order_id   TEXT,                    -- bot-generated client_order_id
    first_fill_at           TEXT,
    last_fill_at            TEXT,
    metadata_json           TEXT                     -- includes synthesized=true for backfills
);

CREATE TABLE position_lifecycle_legs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    position_uid        TEXT    NOT NULL,
    symbol              TEXT    NOT NULL,
    side                TEXT    NOT NULL,
    qty                 REAL    NOT NULL,
    avg_entry_price     REAL,
    FOREIGN KEY(position_uid) REFERENCES position_lifecycle(position_uid)
);
```

Indexes: `owner_key`, `status`, `strategy`, and `legs.position_uid`.

Valid status values ([engine/lifecycle.py:146](../engine/lifecycle.py:146)):
`pending`, `open`, `partially_filled`, `closed`, `canceled`, `external_closed`, `error`.

### 1.1 What the schema does NOT carry (verified by grep)

| Field | Why we need it | Current home |
|---|---|---|
| `entry_order_id` (on a populated row) | The column exists and `create_pending` accepts it, but **zero callers pass it**. `_lifecycle_begin` at [execution/broker.py:338](../execution/broker.py:338) creates the pending row before submit (no broker id yet, correctly None), and nothing goes back to populate it after submit. Without it, every cycle / startup reconciliation path that wants to query the broker for "what happened to this lifecycle row's order" has no key to query by. | In `_suspect_orders[symbol].order_id` (in memory, narrow recovery only). |
| `entry_stop_price` | ATR-sized protective stop attached to the entry. After downtime we cannot rebuild the same stop without it. | `RiskDecision.stop_price` in memory; captured into `trades.initial_stop_loss` AFTER fill row writes. |
| `entry_take_profit_price` | Bracket OTO take-profit. None today, but a design-level gap. | Nowhere. |
| `entry_trigger_price` | STOP_LIMIT stop_price for resting orders (PR #58 needs it). | `RiskDecision` in memory. |
| `entry_max_price` | Capped market entries (PLAN 11.32) and STOP_LIMIT limit_price. | `RiskDecision` in memory. |
| `slippage_benchmark_price` | Slippage benchmark captured at submission (Phase 1 canonical name). After Phase 1 this lives on the trade row but **only after fill**. Pending row has no copy. | Captured in [engine/trader.py:1541-1559](../engine/trader.py:1541) as `slippage_ref` + `slippage_kind`, then discarded unless the order fills synchronously. |
| `slippage_benchmark_kind` | `arrival_midpoint` / `fallback_latest_close` / `unavailable` from the canonical Phase 1 taxonomy. | `_suspect_orders[symbol].modeled_price_kind` in memory. Defect 2 of the slippage Phase 1 review added it to the cache; restart loses it. |
| `slippage_benchmark_timestamp` | When the benchmark was observed. Required by Phase 1's row contract. | Nowhere durable until the fill row writes. |
| `slippage_measurement_quality` | `primary` / `fallback` / `recovered` / `unavailable` — Phase 1 taxonomy. | Nowhere durable. |

**Terminology note (P2.2 fix):** the operator directive uses `modeled_arrival_price` / `modeled_arrival_kind`. The actual Phase 1 taxonomy uses `slippage_benchmark_price`, `slippage_benchmark_kind`, `slippage_benchmark_timestamp`, `slippage_measurement_quality`. The foundation PR must reuse these exact names; otherwise we ship two parallel naming conventions for the same concept.

The downstream consequence: when `_recover_suspect_orders` adopts a recovered fill, the slippage kind is recoverable only because `_suspect_orders` carries it. When the lifecycle row instead drives recovery (e.g. after restart), the kind is lost and recovery rows fall back to `unavailable` — provable in the trade-log audit but a real information loss.

**Section 6 recommends the per-order child table where these fields belong** — not on `position_lifecycle` itself.

---

## 2. Transitions wired today vs. not

### 2.1 Where the writes happen

The store at [engine/lifecycle.py:262](../engine/lifecycle.py:262) exposes:

- `create_pending(...)` — `(none)` → `pending`
- `mark_open(...)` — `pending` / `partially_filled` → `open`
- `mark_partially_filled(...)` — `pending` → `partially_filled`
- `mark_residual(current_qty)` — current_qty update only, status unchanged
- `mark_canceled()` — `pending` → `canceled` (refuses if any fills recorded)
- `mark_closed(external=False|True)` — open/partially_filled → `closed` / `external_closed`
- `synthesize_for_existing(...)` — `(none)` → `open` (idempotent backfill of broker-open positions)

Callers:

| Caller | Method called | Path |
|---|---|---|
| [execution/broker.py:338](../execution/broker.py:338) `_lifecycle_begin` | `create_pending` | every entry `place_order` (after dry-run guard) |
| [execution/broker.py:387-413](../execution/broker.py:387) `_lifecycle_mark_filled` | `mark_open` / `mark_partially_filled` / `mark_canceled` | synchronous outcomes from `_wait_for_fill` |
| [execution/broker.py:843](../execution/broker.py:843) options worker `_on_fill` callback | `_lifecycle_mark_filled` | **async** outcomes from `OptionsExecutionWorker` |
| [execution/broker.py:1027,1039,1205,1215](../execution/broker.py:1027) `_lifecycle_mark_canceled` | `mark_canceled` | broker-rejected submissions, halted entries |
| [engine/trader.py:2467](../engine/trader.py:2467) `_close_lifecycle_for_owner_key` | `mark_closed` | every in-process exit (signal, stop fill, etc.) via `_record_realized_pnl` |
| [engine/trader.py:5927](../engine/trader.py:5927) reverse reconcile | `mark_closed(external=True)` | each cycle: any open row whose owner_key is gone from broker |
| [engine/trader.py:5874](../engine/trader.py:5874) `_reconcile_position_lifecycle` | `synthesize_for_existing` | each cycle: broker-open positions with no non-terminal lifecycle row |
| [engine/trader.py:3823](../engine/trader.py:3823) recovered entry context | `synthesize_for_existing` | startup repair when an unowned broker position is reconstructed |
| [engine/trader.py:2424](../engine/trader.py:2424) reduce path | `mark_residual` | partial close of `open`/`partially_filled` row |

### 2.2 What IS wired vs. what is NOT wired

This corrects the prior cut, which conflated single-leg options with the equity TIMEOUT path.

**Wired today:**

- **Single-leg options async lifecycle.** [execution/broker.py:822](../execution/broker.py:822) creates the pending row, `OptionsExecutionWorker` runs the order, and the `_on_fill` callback at [line 843](../execution/broker.py:843) calls `_lifecycle_mark_filled` asynchronously when the worker terminates. **This is the prototype the foundation PR should generalize.** It demonstrates that an async-callback bridge from execution to lifecycle is already operating reliably for one strategy.
- **Synchronous equity entries inside the 240s confirmation window.** `_wait_for_fill` returns terminal, `_lifecycle_mark_filled` runs immediately.
- **All exit paths in single-leg equities and single-leg options.** Routed through `_record_realized_pnl` → `_close_lifecycle_for_owner_key`.
- **Cycle-level forward backfill** (broker-open positions with no non-terminal lifecycle row).
- **Cycle-level reverse external-close** (owner_key gone from broker, with a pending-row grace window).

**NOT wired today:**

1. **Stream-driven entry fill for equities.** `_process_stream_stop_fills` only routes stop-fill events. A stream-delivered ENTRY fill event for a resting equity LIMIT or STOP_LIMIT has no path to `mark_open`. The synchronous `_wait_for_fill` covers fills inside its 240s window via stream-then-REST fallback; orders that return TIMEOUT (still working after 240s) never get their later async fill applied to the lifecycle row.
2. **Cycle-level reconciliation of non-terminal rows against broker by `entry_order_id`.** [`_reconcile_position_lifecycle`](../engine/trader.py:5800) only handles "no row exists" (synthesize) and "owner_key vanished" (external close). It does not iterate non-terminal rows and ask the broker "what happened to this order?". Two structural reasons: (a) `entry_order_id` is never populated, so there's nothing to ask by; (b) the row is position-level, so even with an order_id it can't represent the multi-order state PR #58 needs.
3. **Startup downtime fill discovery.** No path scans broker closed-order history for fills/cancels that happened during downtime and would resolve a still-pending row. PR #58 added a probe of `startup_snapshot.open_orders`, which by definition skips orders that already filled or canceled before the snapshot.
4. **Stream-driven cancellation outside the 240s window.** No hook.
5. **Stream-driven partial fill on a resting order.** No hook. Stays pending after each partial.
6. **Spread lifecycle (entries and exits).** Confirmed by `grep -n lifecycle execution/options_executor.py` returning nothing and by the absence of spread-path `create_pending` call sites. Spreads are explicitly Phase-A-deferred.

### 2.3 The §8.1 invariant — verified, and scoped to the position level

The proposal's invariant — any row with `current_qty > 0` cannot transition to `canceled` — IS enforced today at the store boundary at [engine/lifecycle.py:486-501](../engine/lifecycle.py:486):

```python
if first_fill_at is not None or (current_qty or 0.0) > 0.0:
    raise ValueError("refusing to mark ... canceled — it has fills ...")
```

`_lifecycle_mark_filled` at [execution/broker.py:399-412](../execution/broker.py:399) also defends in depth: a CANCELED/REJECTED outcome with `filled_qty > 0` routes to `mark_partially_filled` rather than `mark_canceled`.

**Important scope correction (PR #59 review-2):** §8.1 is a **position-level invariant**, not a per-order one. At the broker, an order that is partially filled and then canceled DOES terminate as `canceled` with a preserved `filled_qty`. That's normal Alpaca behavior. The position that the order partially filled remains `open` at the filled quantity. Under §6's per-order table:

- the per-order row transitions `partially_filled → canceled` and stays at `filled_qty` (this is the broker truth)
- the position-level `current_qty` is the side-signed rollup `SUM(CASE side WHEN 'buy' THEN filled_qty ELSE -filled_qty END)` across the position's orders and stays > 0 (see §6.6 for the full query)
- the §8.1 invariant applies to the position-level row's status only — it must remain `open` / `partially_filled`, never `canceled`, if any per-order row has `filled_qty > 0`

The current store-level `mark_canceled` check is still correct *for the position-level row*. The foundation PR adds a parallel per-order state machine where `partially_filled → canceled` is a valid order-level transition.

---

## 3. Current reconciliation paths

### 3.1 `_reconcile_position_lifecycle` — cycle-level

Site: [engine/trader.py:5800](../engine/trader.py:5800). Two passes per cycle:

**Forward (backfill).** Iterates `snapshot.account.open_positions`. For each broker-open position whose `owner_key` has **NO non-terminal lifecycle row** (`get_open_for_owner_key` returns rows in status `pending` / `open` / `partially_filled`, so any of those counts as "exists"), call `synthesize_for_existing` to insert an `open` row tagged `metadata.synthesized=true`. Skips OCC legs belonging to managed spreads. Skips single-leg OCC unless `_positions` already tracks it.

What this handles:
- broker positions that pre-date the lifecycle code shipping
- positions inherited across a restart whose original `pending` row was never created
- OCC options trailing stops needing a durable `position_uid`

What this does NOT handle:
- **advancing an existing pending row to open.** If a pending row exists for `owner_key=AAPL` and a broker position appears for AAPL, the forward pass sees `existing != None` and skips. The row stays pending even though a broker position now exists for it.
- distinguishing primary fill from residual fill on a hybrid entry (PR #58's split-entry shape). The forward pass treats any broker position for that owner_key as the synthesized open state.

**Reverse (close-reconcile).** For each non-terminal lifecycle row whose `owner_key` is no longer in broker positions, call `mark_closed(external=True)`. Skips spread rows. Skips `pending` rows younger than `LIFECYCLE_PENDING_GRACE_SECONDS` ([settings.py](../config/settings.py)) — added in PR-2 to avoid mass-closing legitimate in-flight entries after a startup race.

**Foundation PR addition (review-8 finding #4):** the reverse pass must also **skip `status='error'` rows**. An errored position is operator-attention required; the bot must not auto-resolve it via broker-snapshot defense even when the symbol vanishes from the broker (the vanish could itself be a consequence of the error scenario). Errored rows are released only by explicit operator action through the resolution flow. This pairs with §6.2's `uniq_one_active_position_per_owner_key` index, which retains the lock for `'error'` status — together they guarantee an unresolved error blocks both new entries on the symbol AND auto-`external_close` by reverse-pass.

What this handles:
- overnight stop fills that the bot didn't witness
- manual broker-side closes
- broker-side cancels of `pending` rows older than the grace window (these are marked `external_closed`, which loses the §8.1 distinction between "canceled" and "externally closed" — but this is intentional since the bot can no longer prove zero-fill at this remove)

What this does NOT handle:
- pending rows for orders that legitimately worked >grace_seconds before filling (rare today, routine for PR #58's resting orders)

### 3.2 `_recover_suspect_orders` — cycle-level, narrow

Site: [engine/trader.py:1844](../engine/trader.py:1844). Walks `self._suspect_orders` (in-memory dict keyed by symbol), calls `broker.reconcile_submitted_order(order_id, symbol, requested_qty)`, and routes by the returned `OrderResult.status`:

| Returned status | Action |
|---|---|
| PENDING / ACCEPTED | log, wait next cycle |
| CANCELED / REJECTED / TIMEOUT | drop the suspect record (no row update) |
| FILLED / PARTIAL + broker position present | record fill, log entry row with `quality='recovered'`, register ownership, ensure protective stop, drop |
| FILLED / PARTIAL + no broker position | warn and drop |

This is the only recovery hook for entries whose synchronous confirmation failed. The cache is in-memory; restart wipes it. The cache only holds entries from the `UNKNOWN` branch of `_process_symbol`; TIMEOUT (resting order still working) is not staged at all.

### 3.3 `_lifecycle_mark_filled` — synchronous fill path

Site: [execution/broker.py:355](../execution/broker.py:355). Called immediately after `_wait_for_fill` returns terminal:

- FILLED + qty>0 → `mark_open(avg, qty)`
- PARTIAL + qty>0 → `mark_partially_filled(avg, qty)`
- CANCELED / REJECTED + qty>0 → `mark_partially_filled` (§8.1)
- CANCELED / REJECTED + qty=0 → `mark_canceled`
- TIMEOUT / UNKNOWN → leave the row pending; rely on later reconciliation

### 3.4 `_close_lifecycle_for_owner_key` — exit path

Site: [engine/trader.py:2437](../engine/trader.py:2437). Called by every exit code-path through `_record_realized_pnl`. The guard at [line 2465](../engine/trader.py:2465) filters by `row.position_type != "single_leg"`, which means **single-leg equity AND single-leg option closes are both wired today** (single-leg options have `position_type='single_leg'`). Only **spread closes** are deferred — they have `position_type='spread'` and the function returns before touching them.

(An earlier draft of this doc said "spread/options closes are deferred to Phase C," which contradicted §2.2's correct statement that single-leg options exits are wired. PR #59 review-3 minor fix.)

### 3.5 Idempotency / exactly-once — currently implicit, must become explicit

A given broker outcome can today reach the lifecycle through multiple paths:

- WebSocket stream delivers a terminal `fill` event for an entry order
- Cycle `_recover_suspect_orders` polls the broker for the same order
- Startup `_restore_suspect_orders_from_broker` (PR #58's addition) walks open orders for the same handle

There is **no exactly-once contract.** Today's de-facto idempotency comes from:

- `mark_open` and friends are `UPDATE ... WHERE position_uid=?` — re-running them on an already-`open` row is not an error, but it overwrites `avg_entry_price` / `current_qty` with whatever the second caller passes. If stream sees `10@150.00` and a later REST poll sees `10@150.01` (mid-fill VWAP shift), the row drifts to the second caller's view.
- `_log_entry` writes to `trades` without an order_id uniqueness constraint. Two paths calling it produce duplicate fill rows.
- `_close_lifecycle_for_owner_key` reads-then-updates; if already closed it's a no-op. This is the strongest of the implicit guards.

The foundation PR must define exactly-once explicitly. Mechanism specified in §6.4. Two real constraints to honor (PR #59 review-2):

1. **Alpaca does not provide a monotonic per-event sequence.** A speculative `event_sequence INTEGER` column has no wire source. The broker DOES provide `Order.updated_at` (ISO-8601 timestamp, monotonic per order in nearly all cases, with rare backend ties that need a secondary discriminator). It also provides per-event `(status, filled_qty)` which, in the per-order state machine, is itself monotonic in state-machine order.
2. **Atomicity must come from a single SQL statement, not read-then-update.** A `SELECT ... ; UPDATE ...` pair across two statements races concurrent callers; a single `UPDATE ... WHERE (state-stale predicate)` returns rowcount and applies the event iff the row is actually stale.

§6.4 specifies the resulting API. The per-order table makes the mechanism natural because a position can have multiple distinct orders, each with its own state and its own `last_observed_broker_updated_at`. A position-level row cannot represent "stream-side primary fill has been applied but cycle-side residual fill has not."

---

## 4. The suspect-order caches and why they exist

### 4.1 `_suspect_orders` — entry recovery

[engine/trader.py:423](../engine/trader.py:423): `dict[str, SuspectOrder]`, keyed by `decision.symbol`.

Populated by [`_remember_suspect_order`](../engine/trader.py:1791) when `place_order` returns `OrderStatus.UNKNOWN`. Drained by `_recover_suspect_orders` each cycle.

**Why this cache exists separately from the lifecycle row, and why the caches reveal a structural gap:**

- The lifecycle row exists (it was created in `_lifecycle_begin` before submit), but the row carries neither the modeled-price benchmark nor the slippage-kind tag needed to write a faithful trade-log entry on recovery. Slippage Phase 1's Defect 2 fix surfaced this — `SuspectOrder` had to grow a `modeled_price_kind` field, in memory, because there was nowhere durable to put it.
- The lifecycle row also carries no `entry_order_id` (confirmed §1), so the cache is the *only* way to know which Alpaca order to reconcile.
- `RiskDecision` itself is not persisted anywhere durable. The cache holds it so `_recover_suspect_orders` can call `_log_entry` with the same shape a synchronous fill would have used.

The symbol-key fails on PR #58's hybrid: primary STOP_LIMIT and residual MARKET share `decision.symbol`. The dict assigns by symbol, so the second write overwrites the first. One submitted order's recovery handle is always lost.

The cache is memory-only: a restart drops every entry. Today this is rare for synchronous-window entries; PR #58's resting orders make it routine.

### 4.2 `_suspect_residual_orders` — PR #58-specific, parked

PR #58 added a parallel cache to handle the residual MARKET because the primary cache slot was owned by the STOP_LIMIT. Same shape, same memory-only durability. The R7 review's [P1] "Ambiguous residual recovery is still lost across restart" finding identifies this as fundamentally insufficient.

### 4.3 `_suspect_exit_orders` — exit-side mirror of the entry cache

[engine/trader.py:424](../engine/trader.py:424): `dict[str, SuspectExitOrder]`, keyed by `decision.symbol`. The exit-side parallel to `_suspect_orders`, introduced by [PR #53](https://github.com/francomarb/trading-bot/pull/53) (commits `28352a0`, `e0bedc2`) when a confirmed-by-Alpaca close lost its terminal confirmation locally and the existing recovery path declared the position external-closed before the real fill landed.

Drained by [`_recover_suspect_exit_orders`](../engine/trader.py:1979) each cycle (called from the same operator-halt-coverage path that runs `_recover_suspect_orders`). The recovery logic preserves the same invariants as the entry-side path:

- query broker order history only after the current lifecycle's entry timestamp (bounded window)
- require recovered cumulative sell quantity to explain the open quantity
- preserve broker timestamps / VWAP
- never let external-close detection race ahead of a known submitted close

This cache is the same shape and has the same restart-volatility problem as `_suspect_orders`: the durable handle for an in-flight exit lives only in memory. Foundation PR's `role='exit'` per-order rows make the handle durable and `_suspect_exit_orders` redundant — the same way `role='entry_primary'` rows replace `_suspect_orders`. The recovery invariants above must survive the migration as acceptance tests (see §10).

### 4.4 What the caches' existence reveals

**A position can have multiple distinct orders associated with it through its life: an entry, a residual entry (in hybrid strategies), a protective stop order, and an exit order.** Each has its own `order_id`, its own `intended_qty`, its own benchmark provenance, and its own status timeline. The lifecycle row carries metadata for *one* of them. Every additional order ends up either:

- shadow-state in an in-memory cache (the `_suspect_*` pattern), or
- silently un-tracked (the residual fill, the eventual exit order, the replacement stop)

The structural answer is a per-order child table linked to `position_uid`. **The foundation PR's load-bearing schema change is this table, not extending `position_lifecycle` with more columns.** See §6.

---

## 5. Explicit list of holes PR #58 surfaced

The pattern is consistent: each hole traces to one or more of (a) the lifecycle row carries one order's metadata at most, (b) recovery state is in-memory not durable, (c) recovery code only handles synchronous outcomes.

### 5.1 TIMEOUT → no recovery handle

`_poll_until_terminal` returns `TIMEOUT` for an order still working after 240s. Today's code treats TIMEOUT as terminal in `_lifecycle_mark_filled` (leaves row pending) but never stages it for recovery in `_suspect_orders`. A resting STOP_LIMIT primary therefore has no ownership in `_positions`, no fill callback, and no path to advance its lifecycle when it eventually fills.

[Review reference: R5 P1 "A working primary returned as TIMEOUT is never owned or recovered."]

### 5.2 `_suspect_orders` keyed by symbol — hybrid collides

§4.1 above. Symbol-keyed slot cannot hold both primary and residual handles for the hybrid split.

[Review reference: R6 P1 "One symbol-keyed suspect slot cannot recover both hybrid orders."]

### 5.3 Drain reads the wrong qty field

PR #58's drain logic stored `primary_qty = float(row.current_qty)`, but `current_qty` is `0.0` on a freshly-created pending row. The intended primary qty lives in `entry_qty`. Symptom of `entry_qty` and `current_qty` semantics being subtle. Foundation PR's per-order table + explicit state machine should make this impossible.

[Review reference: R6 P1 "The new drain guard reads the wrong lifecycle quantity."]

### 5.4 Pending drain over-eagerly closes on residual-only broker state

If the residual fills before the primary, the next snapshot shows broker position == residual qty (e.g. 0.88 shares). The drain logic marked the lifecycle `open` at 0.88 and discarded the cache. Later primary fills had no callback. Caused by treating *any* broker position as the completed aggregate.

[Review reference: R5 P1 "The pending-lifecycle drain consumes the cache on the residual-only position."]

### 5.5 Startup reconstruction only walks open orders

PR #58 added `_restore_suspect_orders_from_broker` walking `startup_snapshot.open_orders`. By definition this misses orders that filled or canceled during downtime. The pending lifecycle row has no `entry_order_id` lookup path. The generic missing-entry-context recovery cannot synthesize a STOP_LIMIT `RiskDecision` because it has no `entry_trigger_price` or `entry_max_price`.

[Review reference: R7 P1 "A primary that fills while the process is down still bypasses exact-order recovery."]

### 5.6 Ambiguous residual recovery lost across restart

The per-order child table is the answer, not a wider startup walk. A residual order persisted as a per-order row with its own `order_id`, `intended_qty`, and `status` survives restart. The reconciliation path queries by `order_id`, not by hopeful broker-state inference.

[Review reference: R7 P1 "Ambiguous residual recovery is still lost across restart."]

### 5.7 Two `fallback_reference_price` slippage tags fabricated

PR #58 in two places writes `modeled_arrival_kind='fallback_reference_price'` — a kind invented inside PR #58, not part of the slippage Phase 1 taxonomy. Foundation PR's rule: recovery rows preserve the original `slippage_benchmark_kind` captured at submission OR mark `unavailable`. Never fabricate.

---

## 6. Recommended foundation shape: per-order child table

This section makes the structural recommendation the operator directive explicitly invited ("position_lifecycle table OR new submitted_orders table — pick in discovery doc"). The PR #59 review correctly pushed back on extending the position-level row.

### 6.1 Why a child table, not more columns

A position has at most one identity (`position_uid`), one position-level status rollup, and one current_qty. It can have many orders over its life:

| Role | When it exists |
|---|---|
| `entry_primary` | always |
| `entry_residual` | PR #58 hybrid |
| `protective_stop` | always for equities (attached OTO) |
| `replacement_stop` | after GTC promotion (PR #47) or trail-driven replacement (option trailing) |
| `exit` | every signal exit or operator-issued close |
| `partial_close` | every reduce-position operation |

Each order has its own `order_id`, its own `intended_*` price fields, its own slippage benchmark provenance, and its own status timeline. Trying to squeeze all of these into `position_lifecycle` columns produces a wide row, duplicate semantics for "which order_id is this column about," and a maintenance trap whenever a strategy introduces a new order role.

### 6.2 Proposed schema

```sql
CREATE TABLE position_lifecycle_orders (
    id                            INTEGER PRIMARY KEY AUTOINCREMENT,
    position_uid                  TEXT    NOT NULL,
    role                          TEXT    NOT NULL,    -- entry_primary | entry_residual
                                                       -- | protective_stop | replacement_stop
                                                       -- | exit | partial_close

    -- Broker identity (order_id NULL while pending; populated once submit returns)
    order_id                      TEXT,
    client_order_id               TEXT    NOT NULL,

    -- Order intent (captured at row insert; never changes)
    order_type                    TEXT    NOT NULL,    -- market | limit | stop | stop_limit
    order_class                   TEXT    NOT NULL,    -- simple | bracket | oto | oco
    time_in_force                 TEXT    NOT NULL,    -- day | gtc | ioc | fok | opg | cls
    side                          TEXT    NOT NULL,    -- buy | sell
    intended_qty                  REAL    NOT NULL,
    intended_stop_price           REAL,                -- stop / stop_limit / OTO stop child
    intended_trigger_price        REAL,                -- stop_limit stop price
    intended_limit_price          REAL,                -- limit / capped market / stop_limit
    intended_take_profit_price    REAL,                -- bracket take-profit child

    -- Order relationships
    parent_order_id               TEXT,                -- OTO/bracket: order_id of the parent
    replaces_order_id             TEXT,                -- replacement_stop: order_id being replaced

    -- Origin (schema-compatible hooks for Phase C; bot-originated rows leave NULL)
    origin_kind                   TEXT    NOT NULL DEFAULT 'bot',  -- bot | operator
    operator_command_uid          TEXT,                -- cmd_<hex> when origin_kind='operator'

    -- Pre-fill slippage benchmark provenance (canonical Phase 1 naming).
    -- NOTE: the per-order table carries INTENT only — computed
    -- slippage_signed_bps / slippage_adverse_bps stay on `trades`.
    slippage_benchmark_price      REAL,
    slippage_benchmark_kind       TEXT,                -- arrival_midpoint | fallback_latest_close | unavailable
    slippage_benchmark_timestamp  TEXT,
    slippage_measurement_quality  TEXT,                -- primary | fallback | recovered | unavailable

    -- Lifecycle / observed state
    status                        TEXT    NOT NULL,    -- per-order state machine, see §6.3
    filled_qty                    REAL    NOT NULL DEFAULT 0.0,
    avg_fill_price                REAL,

    -- Timestamps with distinct semantics
    created_at                    TEXT    NOT NULL,    -- row insert (pre-submit allowed)
    submitted_at                  TEXT,                -- broker submit return (NULL during pending)
    terminal_at                   TEXT,                -- moved to a terminal status (filled/canceled/rejected)

    -- Idempotency anchor — broker's last-observed updated_at echoed onto the row.
    -- Combined with the state-machine ordering in §6.4, this enforces
    -- exactly-once application without depending on a fabricated event_sequence.
    last_observed_broker_updated_at TEXT,
    last_observed_at              TEXT    NOT NULL,    -- our wall-clock for the last apply

    FOREIGN KEY(position_uid) REFERENCES position_lifecycle(position_uid)
);

-- SQLite FK enforcement gotcha (PR #59 review-13 Gemini fix).
-- SQLite does NOT enforce FOREIGN KEY constraints by default — they
-- are declared in the schema but advisory unless `PRAGMA foreign_keys = ON;`
-- is executed on every connection that opens the database. Without
-- that PRAGMA, a buggy writer could insert a position_lifecycle_orders
-- row pointing at a non-existent position_uid and SQLite would happily
-- accept it. The implementation PR must add
-- `conn.execute("PRAGMA foreign_keys = ON;")` to TradeLogger._ensure_db
-- (reporting/logger.py:354) immediately after sqlite3.connect, BEFORE
-- any DDL or DML runs. Today's _ensure_db only sets PRAGMA table_info
-- (a read-only introspection PRAGMA, unrelated); the FK enforcement
-- PRAGMA is missing and must be added.

-- Unique constraints (PR #59 review-2 fix: non-unique indexes don't enforce exactly-once).
-- order_id is NULL during pending; partial unique index permits multiple NULLs
-- but rejects duplicate non-NULL ids (SQLite supports this natively).
CREATE UNIQUE INDEX uniq_lifecycle_orders_order_id
    ON position_lifecycle_orders(order_id) WHERE order_id IS NOT NULL;
CREATE UNIQUE INDEX uniq_lifecycle_orders_client_order_id
    ON position_lifecycle_orders(client_order_id);

-- A position has at most one entry_primary at the per-order level. This
-- alone does NOT prevent the bot from opening a SECOND position on the
-- same owner_key (different position_uid → uniqueness on position_uid
-- passes). PR #59 review-6 finding #1: the actual duplicate-entry
-- prevention is a position-LEVEL constraint, added separately below.
CREATE UNIQUE INDEX uniq_one_entry_primary_per_position
    ON position_lifecycle_orders(position_uid) WHERE role = 'entry_primary';

-- A position has at most one non-terminal close-side order at a time:
-- if a discretionary exit OR an operator-issued partial_close is already
-- working, the bot must NOT submit a second one. This is the durable
-- analog of today's `_spreads_pending_close` set and the
-- `_has_pending_close_order()` snapshot check — both are in-memory and
-- restart-volatile. PR #59 review-7 finding P1: foundation makes this
-- a durable constraint enforced by the database. Stop-side roles
-- (protective_stop, replacement_stop) are NOT included — replacement_stop
-- is an intentional second-stop pattern (PR #47 GTC promotion) and
-- protective_stop is OTO-paired with the entry, not a competing close.
CREATE UNIQUE INDEX uniq_one_active_close_per_position
    ON position_lifecycle_orders(position_uid)
    WHERE role IN ('exit', 'partial_close')
    AND status IN ('pending', 'working', 'partially_filled', 'unknown');

CREATE INDEX idx_lifecycle_orders_position_uid ON position_lifecycle_orders(position_uid);
CREATE INDEX idx_lifecycle_orders_status       ON position_lifecycle_orders(status);
CREATE INDEX idx_lifecycle_orders_parent       ON position_lifecycle_orders(parent_order_id);
CREATE INDEX idx_lifecycle_orders_replaces     ON position_lifecycle_orders(replaces_order_id);
```

#### Position-level uniqueness — added to existing `position_lifecycle`

Foundation PR also adds a partial unique index to the existing `position_lifecycle` table (no schema column change, index only):

```sql
-- At most one non-terminal lifecycle row per owner_key. Spreads have
-- per-instance UUID owner_keys (always unique), so multiple spreads
-- on the same underlying don't collide. Equity / single-leg options
-- get one position per owner_key (ticker / underlying), which is the
-- live duplicate-entry-prevention invariant the bot relies on today
-- (currently enforced softly via _positions[]; foundation PR makes
-- it durable). PR #59 review-6 finding #1.
--
-- 'error' is included in the lock (PR #59 review-8 finding #3 fix):
-- an errored position needs operator resolution before the symbol
-- can host a new lifecycle. Excluding 'error' would let the bot
-- silently open a fresh position over an unresolved error, masking
-- it. Operators close 'error' rows explicitly via the recovery /
-- resolution flow; only then is the owner_key released.
CREATE UNIQUE INDEX uniq_one_active_position_per_owner_key
    ON position_lifecycle(owner_key)
    WHERE status IN ('pending', 'open', 'partially_filled', 'error');
```

This is the constraint that prevents the bot from submitting a second entry for AAPL while an AAPL position is still open. The per-order `uniq_one_entry_primary_per_position` is belt-and-suspenders within the position; the position-level index is what actually guards against shared-symbol duplicates across new position_uids.

Notes:

- **`order_class` vs `order_type`** — Alpaca's `OrderClass` (simple / bracket / OTO / OCO) is distinct from `OrderType` (market / limit / stop / stop_limit). Both are needed; the bot uses `OrderClass.OTO` for every equity entry today and `OrderClass.SIMPLE` for option entries.
- **`time_in_force`** — required to honestly reproduce intent across a downtime gap. Alpaca expires GTC at 90 days and DAY at session end; both are visible to the bot only if persisted.
- **`parent_order_id` / `replaces_order_id`** — the protective-stop child of an OTO entry has `parent_order_id = entry_primary.order_id`. A GTC-promoted replacement stop ([PR #47](https://github.com/francomarb/trading-bot/pull/47)) has `replaces_order_id = previous_stop.order_id` and `parent_order_id = entry_primary.order_id`. This makes "which stop currently covers this position" a single query against the orders table.
- **`origin_kind` + `operator_command_uid`** — schema-compatible only. Foundation PR does NOT implement destructive operator commands; the columns exist so Phase C can populate them when it ships. Bot-originated rows leave `origin_kind='bot'` and `operator_command_uid` NULL.
- **No computed slippage bps on this table.** Per the cross-workstream phase-boundary feedback: the per-order table owns pre-fill *intent* and *benchmark provenance*; computed `slippage_signed_bps` / `slippage_adverse_bps` stay on `trades`. The order table is not a second slippage-reporting store.
- **`created_at` vs `submitted_at` vs `terminal_at`** — three distinct moments. `created_at` is row insert (before broker submit). `submitted_at` is when submit returned (NULL while the row is `pending`). `terminal_at` is the move to a terminal status.

`position_lifecycle` keeps its position-level fields (`status`, `current_qty`, `avg_entry_price`) as a rollup derived from the child rows in `position_lifecycle_orders`, and `net_realized_pnl` as a rollup from the `trades` table — the per-order table does not carry realized P&L (per §11's ownership boundary: `trades` is the source of truth for realized P&L and computed slippage). PR #59 review-13 Gemini fix. The position-level `entry_order_id` column is retained as a non-authoritative mirror of the `entry_primary` row's order_id for backward compatibility.

### 6.3 Per-order state machine

```
pending           — row inserted; broker submit in progress
working           — broker has accepted; order is alive (live working, resting STOP_LIMIT, etc.)
partially_filled  — 0 < filled_qty < intended_qty
filled            — filled_qty == intended_qty (terminal)
canceled          — broker-side cancel; preserves filled_qty (terminal)
rejected          — broker rejected pre-fill (terminal, filled_qty == 0)
unknown           — submitted but no confirmation; awaits reconciliation
```

Allowed transitions (R11-P1 + R12 fix — the strict-newer monotonic rule below is the real contract; the edge list is a non-exhaustive illustration of common paths):

```
pending          → working | unknown | rejected | partially_filled | filled | canceled
working          → partially_filled | filled | canceled | unknown
partially_filled → filled | canceled
unknown          → any non-rejected non-unknown state (resolved by recovery)
filled           → (terminal)
canceled         → (terminal)
rejected         → (terminal)
```

`pending → partially_filled` and `pending → filled` are legitimate via the synchronous fast path (`place_order` returns `accepted + immediately filled` before `apply_order_event` ever advances through `working`) and via recovery (REST poll observes `filled` for a never-advanced pending row). `pending → canceled` is legitimate via recovery when a previously-submitted order was canceled at the broker during a downtime window and was zero-filled — startup reconciliation observes the terminal `canceled` directly against the local `pending` row. The strict-newer rule in §6.4 admits all three patterns directly; the edge list above is illustrative, not exhaustive.

**Real contract**: any transition that strictly advances `(rank(status), filled_qty)` is allowed; nothing else is. Terminal states (`filled`, `canceled`, `rejected`) are immutable per the explicit guard in §6.4's UPDATE.

**Per-order vs. position-level invariant (PR #59 review-2 fix).** A `partially_filled → canceled` transition IS valid at the per-order level. That is how Alpaca describes a partially-filled order that gets canceled: the order terminates as `canceled` with its `filled_qty` preserved. The §8.1 invariant from the operator proposal is a *position-level* claim — the parent position must remain `open` (or `partially_filled`) at the filled quantity, never reach `canceled`. Under §6's two-table shape:

- **Per-order rows** record broker truth. `partially_filled → canceled` is allowed; `filled_qty` is preserved on the terminal row.
- **Position-level row** rolls up `current_qty` via the side-signed sum specified in §6.6 (`SUM(CASE side WHEN 'buy' THEN filled_qty ELSE -filled_qty END)`) and updates `status` accordingly. The store-boundary `mark_canceled` guard at [engine/lifecycle.py:486](../engine/lifecycle.py:486) keeps doing its job: a position whose roll-up `current_qty > 0` cannot transition to `canceled`. Belt-and-suspenders in `apply_order_event` (see §6.4).

State-machine ordering (used by §6.4 atomicity):

```
rank(pending)          = 0
rank(working)          = 1
rank(unknown)          = 1
rank(partially_filled) = 2
rank(filled)           = 3
rank(canceled)         = 3
rank(rejected)         = 3
```

An incoming event is "newer" than the row's current state iff `(rank(incoming.status), incoming.filled_qty) > (rank(row.status), row.filled_qty)` lexicographically. This is monotonic by construction of the broker's order lifecycle and is the secondary discriminator when `Order.updated_at` ties.

### 6.4 Reconciliation paths under the new shape

All three paths funnel through one API: `apply_order_event(order_id, event)`.

- **Stream**: delivers events as they arrive from the WebSocket trade_updates feed.
- **Cycle**: iterates non-terminal per-order rows, calls `broker.get_order_by_id(order_id)`, builds an `event` from the broker view, calls `apply_order_event`.
- **Startup**: same as cycle, plus walks broker closed-order history for non-terminal per-order rows that aren't in `open_orders` (downtime fill / cancel discovery).

An `event` carries: `(status, filled_qty, avg_fill_price, broker_updated_at)` plus any reconciliation metadata. No fabricated `event_sequence`.

**Atomic enforcement (PR #59 review-3 fix).** `apply_order_event` is one SQL `UPDATE ... WHERE` statement, not a read-then-update pair. The where-clause must (a) match exactly one row by `order_id` and (b) only apply if the incoming event is strictly newer in the per-order state machine. SQL operator precedence matters: a stray `OR` at the wrong level allowed an earlier draft to update unrelated rows. The corrected form:

```sql
UPDATE position_lifecycle_orders
SET status                          = :status,
    filled_qty                      = :filled_qty,
    avg_fill_price                  = :avg_fill_price,
    last_observed_broker_updated_at = :broker_updated_at,
    last_observed_at                = :now,
    terminal_at                     = CASE
        WHEN :status IN ('filled', 'canceled', 'rejected') THEN :now
        ELSE terminal_at
    END
WHERE order_id = :order_id
  -- Terminal states are immutable. No event, regardless of broker
  -- timestamp, can revive a filled / canceled / rejected order.
  AND status NOT IN ('filled', 'canceled', 'rejected')
  AND (
      -- Common case: incoming event strictly advances the per-order
      -- state machine. Same lexicographic compare as §6.3.
      (
          CASE status
              WHEN 'pending'          THEN 0
              WHEN 'working'          THEN 1
              WHEN 'unknown'          THEN 1
              WHEN 'partially_filled' THEN 2
              ELSE                         3
          END,
          filled_qty
      )
      < (:status_rank, :filled_qty)

      OR

      -- Tiebreaker ONLY when state-machine rank AND filled_qty are
      -- exactly equal — i.e. the broker re-issued the same logical
      -- state with a fresher updated_at. updated_at can never bypass
      -- the state-machine ordering; it only disambiguates equal
      -- states. (PR #59 review-3 P1 fix.)
      (
          CASE status
              WHEN 'pending'          THEN 0
              WHEN 'working'          THEN 1
              WHEN 'unknown'          THEN 1
              WHEN 'partially_filled' THEN 2
              ELSE                         3
          END = :status_rank
          AND filled_qty = :filled_qty
          AND (
              last_observed_broker_updated_at IS NULL
              OR last_observed_broker_updated_at < :broker_updated_at
          )
      )
  );
```

SQLite executes a single `UPDATE` atomically. `rowcount == 0` means the event was stale, a duplicate, or targeted a terminal-state row — log and drop. `rowcount == 1` means the event was applied; the **same transaction** then runs the trades UPSERT, the position rollup, and the position-level status update.

#### One transaction wraps all four operations

PR #59 review-4 (P1, "all accounting writes must be one transaction") makes the transaction scope explicit. The compare-and-set per-order UPDATE cannot succeed in isolation while the dependent rollups fail — that would leave the per-order row at a new state while `trades` and the position-level row reflect the old state. The right model is one transaction per `apply_order_event` covering all four writes:

```python
with conn:  # sqlite3 context manager: commits on success, rolls back on any exception
    # Step 1: compare-and-set on the per-order row (§6.4 SQL above).
    cur = conn.execute(COMPARE_AND_SET_SQL, params)
    if cur.rowcount == 0:
        raise StaleOrDuplicateEvent()      # context manager rolls back, transaction abandoned

    # Step 2: aggregate trades UPSERT keyed by order_id (§6.5 SQL).
    conn.execute(TRADES_UPSERT_SQL, fill_params)

    # Step 3: recompute position-level rollup fields:
    #   - current_qty + avg_entry_price from the per-order rows
    #     (§6.6 side-signed sum)
    #   - net_realized_pnl from `trades` (§6.6 — per-order table
    #     does not carry realized P&L; trades is the source of
    #     truth per §11's ownership boundary)
    # All three are written by one atomic UPDATE on position_lifecycle.
    conn.execute(POSITION_ROLLUP_SQL, {"position_uid": position_uid})

    # Step 4: update position-level status based on the new rollup.
    # See §6.6.1 for the exact SQL; the naive `current_qty == 0 →
    # closed` rule is WRONG because a freshly-submitted entry sitting
    # at `working` with zero fills also has current_qty == 0.
    conn.execute(POSITION_STATUS_SQL, {"position_uid": position_uid})
# implicit COMMIT here
```

If any step raises — UNIQUE conflict on a duplicate `(order_id, position_type='single_leg')` pair from a misuse (§6.5 dedup intends the UPSERT to handle dups, so a raise here means a bug), foreign-key violation, disk full, anything — the transaction rolls back entirely. There is no partial application that leaves the three tables out of sync. (PR #59 review-6 finding #6: removed a stale claim that this scenario could trip a UNIQUE on `execution_id`; that constraint does not exist in the revised model.)

The `StaleOrDuplicateEvent` path is the normal "this event was already applied or stale" outcome and is not an error from the caller's perspective; `apply_order_event` catches it and returns a `dropped` outcome to its caller. Other exceptions propagate.

`apply_order_event` is the single chokepoint that enforces, *together within one transaction*:

- **Exactly-once on the per-order row** via the atomic where-clause (the per-order row mutates at most once per broker-distinct event)
- **State-machine monotonicity** — terminal states stay terminal; updated_at is a tiebreaker WITHIN a state-machine-valid step, never a bypass
- **Idempotent aggregate state on `trades`** via the order-id-keyed UPSERT (§6.5)
- **§8.1 position-level invariant** via the rollup + status step (§6.6) — if the rollup produces `current_qty > 0`, the position-level status cannot transition to `canceled`

### 6.5 `trades` stays one row per ORDER — idempotent UPSERT via cumulative state

PR #59 review-4 (P1, "keep one persistence system") rejected an earlier draft that would have introduced per-execution rows on `trades`. That change would have created a parallel execution ledger, broken existing aggregate consumers, and required a backward-incompatible read migration. The correct shape preserves the current per-order semantics:

- `trades` continues to hold **one row per order_id** (or per logical close event for spread legs, etc.).
- That row carries **cumulative aggregate state**: `filled_qty` is the order's total filled quantity, `avg_fill_price` is the qty-weighted VWAP across all executions for that order.
- Foundation writers UPSERT keyed on `order_id`. A partial fill that later completes simply updates the same row.
- `execution_id` is **optional audit metadata**, nothing more.

#### Schema change on `trades`

```sql
-- Idempotency key for UPSERTs. Scoped to single-leg rows only:
-- log_spread_fill deliberately writes TWO leg rows per spread fill
-- that share the same combo `order_id` (one row for the short OCC
-- leg, one for the long OCC leg). A global UNIQUE(order_id) would
-- reject the second leg insert and break the existing MLEG path.
-- PR #59 review-5 correction (verified at reporting/logger.py:1111+).
-- Existing rows with NULL order_id (synthetic external close, etc.)
-- remain valid: the partial unique index permits multiple NULLs.
CREATE UNIQUE INDEX uniq_trades_order_id_single_leg
    ON trades(order_id)
    WHERE order_id IS NOT NULL AND position_type = 'single_leg';

-- Optional audit-only column; no UNIQUE, no constraints.
ALTER TABLE trades ADD COLUMN execution_id TEXT;
```

#### Writer pattern (UPSERT keyed on `order_id`, single-leg only)

SQLite's `ON CONFLICT` target must match the unique index's predicate exactly when the index is partial. Foundation writers therefore include the same `WHERE order_id IS NOT NULL AND position_type = 'single_leg'` predicate on the conflict clause. The implementation PR must add a database-level test that exercises the migration + UPSERT pair together to verify both predicates stay aligned:

```sql
-- Single-leg writers (log_entry, log_close, log_stop_fill, etc.)
INSERT INTO trades (
    order_id, symbol, strategy, side, position_type,
    filled_qty, avg_fill_price,
    slippage_benchmark_price, slippage_benchmark_kind,
    slippage_benchmark_timestamp, slippage_measurement_quality,
    execution_id,
    ...
) VALUES (
    :order_id, :symbol, :strategy, :side, 'single_leg',
    :filled_qty, :avg_fill_price,
    :slippage_benchmark_price, :slippage_benchmark_kind,
    :slippage_benchmark_timestamp, :slippage_measurement_quality,
    :execution_id,
    ...
)
ON CONFLICT(order_id) WHERE order_id IS NOT NULL AND position_type = 'single_leg'
DO UPDATE SET
    filled_qty       = excluded.filled_qty,        -- broker's latest cumulative
    avg_fill_price   = excluded.avg_fill_price,    -- broker's latest cumulative VWAP
    -- benchmark provenance is set once at first insert and never updated:
    slippage_benchmark_price       = COALESCE(trades.slippage_benchmark_price,       excluded.slippage_benchmark_price),
    slippage_benchmark_kind        = COALESCE(trades.slippage_benchmark_kind,        excluded.slippage_benchmark_kind),
    slippage_benchmark_timestamp   = COALESCE(trades.slippage_benchmark_timestamp,   excluded.slippage_benchmark_timestamp),
    slippage_measurement_quality   = COALESCE(trades.slippage_measurement_quality,   excluded.slippage_measurement_quality),
    -- execution_id is optional audit; keep the last-seen value:
    execution_id = COALESCE(excluded.execution_id, trades.execution_id);
```

`log_spread_fill` continues to use plain `INSERT` for its two leg rows (no UPSERT, no UNIQUE conflict — `position_type='spread'` rows fall outside the partial index). When spread lifecycle wiring lands in the separate spread-lifecycle PR, that PR decides its own dedup key (likely `(order_id, leg_role)` or a synthetic leg id).

The UPSERT semantics make the single-leg writer naturally idempotent across stream / cycle / startup paths: each path produces the same cumulative `filled_qty` and `avg_fill_price` for a given order, so re-application is a no-op (state values match) or an advance (cumulative qty / VWAP have grown).

#### REST recovery uses cumulative deltas, not execution_id

PR #59 review-4 (P1, "REST recovery has no execution_id") correctly notes that Alpaca's REST order-history endpoint exposes **cumulative order state only** — no per-execution detail. `legs[].id` is a child order id (OTO/bracket relationship), not an execution id, and must not be treated as one.

REST recovery path computes the newly-realized quantity from the per-order row's existing cumulative state vs. the broker's current cumulative state:

```python
# Recovery: poll broker for order, compute delta against stored state
broker_state    = self.broker.get_order_by_id(order_id)
stored_filled   = order_row.filled_qty   # cumulative as-of last apply
delta_filled    = broker_state.filled_qty - stored_filled

if delta_filled > 0:
    # Real new fill observed via REST. apply_order_event with the
    # broker's CUMULATIVE values (not the delta) — the trades UPSERT
    # is idempotent on cumulative state.
    apply_order_event(
        order_id           = order_id,
        status             = broker_state.status,
        filled_qty         = broker_state.filled_qty,        # cumulative
        avg_fill_price     = broker_state.filled_avg_price,  # cumulative VWAP
        broker_updated_at  = broker_state.updated_at,
        execution_id       = None,  # REST has no execution_id; audit-only field stays NULL
    )
elif delta_filled == 0 and broker_state.status != order_row.status:
    # Status changed without new fills (e.g. accepted → working,
    # working → canceled with zero fills). Still a valid apply.
    apply_order_event(...)
```

Stream path uses `update.execution_id` when present and writes it through as audit-only. REST and startup paths leave `execution_id` NULL. No path ever fabricates one.

### 6.6 Position rollup — side-signed sum, not raw `SUM(filled_qty)`

PR #59 review-3 P1 also caught the rollup math. A position's `current_qty` is the net of inflows (entry fills) and outflows (exit / stop / partial-close fills). A naive `SUM(filled_qty)` over all per-order rows double-counts: an entry that filled 10 and an exit that filled 10 would produce `current_qty = 20`, not `0`.

Authoritative rollup query (for single-leg long-equity / long-options positions — the only types this PR wires):

```sql
SELECT
    COALESCE(SUM(
        CASE side
            WHEN 'buy'  THEN  filled_qty
            WHEN 'sell' THEN -filled_qty
            ELSE              0
        END
    ), 0.0) AS current_qty
FROM position_lifecycle_orders
WHERE position_uid = :position_uid;
```

`avg_entry_price` is the qty-weighted average over entry-side rows only:

```sql
SELECT
    SUM(filled_qty * avg_fill_price) / NULLIF(SUM(filled_qty), 0)
FROM position_lifecycle_orders
WHERE position_uid = :position_uid
  AND role IN ('entry_primary', 'entry_residual')
  AND filled_qty > 0;
```

`net_realized_pnl` is the sum of realized P&L from `trades`, NOT from the per-order table — the per-order rows carry pre-fill intent and order-state only, and don't have a `realized_pnl` column. The accounting source of truth stays on `trades` per §11's ownership boundary. PR #59 review-13 Gemini fix:

```sql
SELECT COALESCE(SUM(realized_pnl), 0.0) AS net_realized_pnl
FROM trades
WHERE position_uid = :position_uid;
```

This query runs as part of Step 3 of `apply_order_event`'s transaction (§6.4) alongside the `current_qty` and `avg_entry_price` recomputes — all three feed into the same atomic UPDATE on `position_lifecycle`. The `trades.position_uid` column has been populated since the slippage Phase 1 / Operator Controls Phase A work and is indexed at [reporting/logger.py:262](../reporting/logger.py:262) for fast lookup.

Canceled rows with `filled_qty = 0` contribute 0 to the per-order rollups (`current_qty` and `avg_entry_price`), so the partial-then-cancel case is captured correctly. Realized-P&L rollup is similarly idempotent: a canceled order produces no new trade row, so re-applying the same event leaves `net_realized_pnl` unchanged.

The §8.1 invariant on the position-level row uses this rollup: if `current_qty > 0`, the position-level row's status stays `open` / `partially_filled`. The store-boundary `mark_canceled` guard at [engine/lifecycle.py:486](../engine/lifecycle.py:486) continues to refuse `pending → canceled` when there are fills.

**Short-position math (out of scope today).** This PR wires only long-equity / long-options strategies. If a future strategy goes short, the rollup needs to know whether the position was *opened* by a sell (short entry) or *closed* by a sell (long exit). The cleanest way to handle that later is a column or convention on the per-order row (e.g. `role` already encodes the intent: `entry_*` rows are inflows regardless of side; `exit` / `partial_close` / `protective_stop` / `replacement_stop` are outflows). For now the foundation PR assumes long-only and uses `side` as a sufficient proxy.

### 6.6.1 Position-status update — must distinguish "never filled" from "fully exited"

PR #59 review-7 P0: a naive `current_qty == 0 → 'closed'` rule wrongly marks a brand-new working entry as closed. A position whose only per-order row is an `entry_primary` at `status='working'` with `filled_qty=0` has `current_qty=0` because zero side-signed values sum to zero — but the position has never filled. Marking it `closed` would close the lifecycle before the entry ever runs.

`POSITION_STATUS_SQL` must use the position's fill history together with the current rollup, distinguish close-side from stop-side per-order roles, AND avoid reading a stale `status` value when setting `closed_at`. Earlier drafts subqueried `(SELECT status FROM position_lifecycle ...)` inside the SET clause — SQL evaluates SET-clause expressions against the **pre-update** row state, so that subquery would return the OLD status, never the just-computed new one. `closed_at` would remain NULL on every transition into `closed` / `external_closed`. PR #59 review-9 P1 fix: compute the new status once in a CTE, then reference it from both SET expressions:

```sql
-- Compute the new position-level status from the per-order rows.
-- Atomic single statement; runs as Step 4 inside apply_order_event's
-- transaction (§6.4). The CTE computes new_status once so the
-- closed_at CASE can read it (a bare subquery in SET would read the
-- pre-update value — review-9 P1a).
--
-- PR #59 review-10 P1 restructure: branch ordering is now
-- quantity-first, with non-terminal-order existence used only to
-- disambiguate the "no fills ever yet" case from the "all entries
-- terminal with zero fills" case. Earlier draft had a nested
-- "non-terminal entry/close exists" branch that incorrectly held the
-- position at 'open' when a live exit had already flattened or
-- oversold the position. The clean discriminator is fill history +
-- quantity, not order-status existence.
WITH computed AS (
    SELECT CASE
        -- (1) Have any per-order rows reached anything past 'pending'?
        --     If not, the position is 'pending' regardless of other
        --     state. (Most newly-created lifecycle rows.)
        WHEN NOT EXISTS (
            SELECT 1 FROM position_lifecycle_orders
            WHERE position_uid = :position_uid
              AND status NOT IN ('pending')
        ) THEN 'pending'

        -- (2) Has the position ever had a fill? If not, the position
        --     is either still waiting (some entry is working) or
        --     never opened (all entries terminal with zero fills).
        WHEN NOT EXISTS (
            SELECT 1 FROM position_lifecycle_orders
            WHERE position_uid = :position_uid
              AND filled_qty > 0
        ) THEN
            CASE
                -- All entries terminal with zero fills → canceled.
                WHEN NOT EXISTS (
                    SELECT 1 FROM position_lifecycle_orders
                    WHERE position_uid = :position_uid
                      AND role IN ('entry_primary', 'entry_residual')
                      AND status IN ('pending', 'working',
                                     'partially_filled', 'unknown')
                ) THEN 'canceled'
                -- Some entry still working with no fills → pending.
                -- R7-P0: a working entry with filled_qty=0 must NOT
                -- be classified as 'closed' just because the rollup
                -- happens to be zero.
                ELSE 'pending'
            END

        -- (3) Position has had at least one fill. Quantity drives
        --     status, but `closed` requires that ALL non-terminal
        --     SELL-side orders be terminal first — close-side
        --     (exit, partial_close) AND stop-side (protective_stop,
        --     replacement_stop). Any live SELL at the broker can
        --     still execute; releasing the owner_key lock before
        --     the stop is provably terminal means a fresh entry on
        --     the same symbol could be hit by the still-active stop
        --     if the broker triggers it within the cleanup window.
        --     (R12-P1 fix; supersedes R8-1's "stops don't block
        --     closure" claim. R10-P1 partially walked back by R11-P1
        --     for close-side orders; R12-P1 extends that gate to
        --     stop-side too.)

        -- (3a) Negative current_qty: data-integrity violation,
        --      surface as 'error' regardless of pending sell-side
        --      orders. The oversold condition is already realized;
        --      operator needs to see it now. (R9-P1b.)
        WHEN COALESCE((SELECT current_qty FROM position_lifecycle
                       WHERE position_uid = :position_uid), 0) < 0
        THEN 'error'

        -- (3b) current_qty == 0 AND no non-terminal sell-side
        --      orders of ANY role → fully exited, all cleanup
        --      settled, lock can release. This is the actual
        --      `closed` transition.
        WHEN COALESCE((SELECT current_qty FROM position_lifecycle
                       WHERE position_uid = :position_uid), 0) = 0
             AND NOT EXISTS (
                 SELECT 1 FROM position_lifecycle_orders
                 WHERE position_uid = :position_uid
                   AND role IN ('exit', 'partial_close',
                                'protective_stop', 'replacement_stop')
                   AND status IN ('pending', 'working',
                                  'partially_filled', 'unknown')
             )
        THEN 'closed'

        -- (3c) current_qty == 0 BUT a non-terminal sell-side order
        --      (close OR stop) still exists at the broker → stay
        --      'partially_filled'. The position is operationally
        --      flat, but the lock retains until ALL sell-side
        --      orders terminate because a live broker SELL of any
        --      role can still execute and oversell a fresh entry
        --      on the same symbol. Once the last sell-side row
        --      reaches a terminal status, the next apply_order_event
        --      re-evaluates and transitions to 'closed'.
        --
        --      The engine's stop-cleanup path issues the cancel for
        --      working stops on a flat position; the cancel itself
        --      flows through apply_order_event and advances the
        --      stop's per-order row to 'canceled', at which point
        --      this branch's NOT EXISTS check passes.
        WHEN COALESCE((SELECT current_qty FROM position_lifecycle
                       WHERE position_uid = :position_uid), 0) = 0
        THEN 'partially_filled'

        -- (3d) current_qty > 0 but less than the intended entry qty:
        --      partially filled (covers both partial-entry-still-
        --      working and post-partial-close-residual cases).
        WHEN COALESCE((SELECT current_qty FROM position_lifecycle
                       WHERE position_uid = :position_uid), 0)
             < COALESCE((SELECT entry_qty FROM position_lifecycle
                         WHERE position_uid = :position_uid), 0)
        THEN 'partially_filled'

        -- (3e) current_qty >= entry_qty: fully entered, not yet
        --      exited. Standard 'open' state.
        ELSE 'open'
    END AS new_status
)
UPDATE position_lifecycle
SET status = (SELECT new_status FROM computed),
    -- closed_at is set ONLY when status transitions to 'closed' or
    -- 'external_closed'. 'canceled', 'error', and pre-fill
    -- 'pending'/'open' transitions leave closed_at as-is. R8-P2 +
    -- R9-P1a (read new_status from CTE, not pre-update row).
    closed_at = CASE
        WHEN (SELECT new_status FROM computed) IN ('closed', 'external_closed')
        THEN COALESCE(closed_at, :now)
        ELSE closed_at
    END
WHERE position_uid = :position_uid;
```

Decision tree it implements (quantity-driven with cleanup gate — R10-P1 partially walked back by R11-P1):

1. **All per-order rows still in `pending`** → position is `pending`
2. **No fills yet across any per-order row**:
   - All entries terminal with zero fills → `canceled`
   - Otherwise (some entry still working) → `pending`
3. **At least one fill has happened:**
   - `current_qty < 0` (oversold; data-integrity violation) → `error` (immediate, regardless of any pending sell-side orders)
   - `current_qty == 0` AND no non-terminal sell-side order of any role (`exit`, `partial_close`, `protective_stop`, `replacement_stop`) → `closed`
   - `current_qty == 0` AND any non-terminal sell-side order (close OR stop) still exists → `partially_filled` (operationally flat but the owner_key lock must retain — any live broker SELL can still execute and oversell a fresh entry on the same symbol)
   - `0 < current_qty < entry_qty` → `partially_filled`
   - `current_qty >= entry_qty` → `open`

Critical properties:

- **All non-terminal sell-side orders block closure (R12-P1).** This supersedes R8-1's earlier "stops don't block closure" claim. The old reasoning was that the engine cancels stops asynchronously; the gap between "position reaches `closed`" and "engine completes the cancel" was the unbounded window. A broker-side stop on a fully-exited position is still a live SELL that could execute — and if the owner_key lock has released and a fresh entry has been opened on the same symbol, the still-active stop could fire and oversell the new entry. The `closed` transition now requires ALL non-terminal sell-side rows (close-side AND stop-side) to have reached a terminal status first.
- **`error` is immediate on oversold.** Once `current_qty < 0`, the violation is already realized — the operator needs visibility now, not after the sell-side orders finish.
- **Engine stop-cleanup flow.** When a position reaches `partially_filled` with `current_qty == 0` and only working stops remaining, the engine's stop-cleanup path issues the broker-side cancels. Each cancel flows through `apply_order_event` and advances the stop's per-order row to `canceled`. Once the last sell-side row terminates, the next event re-evaluates and the position transitions to `closed`.

`closed_at` is set only when status reaches `closed` or `external_closed`. `canceled`, `error`, and pre-fill `pending` / `open` transitions leave `closed_at` NULL.

The §8.1 invariant is preserved end-to-end: a position with any per-order row at `filled_qty > 0` cannot reach `canceled` because that requires branch (2)'s "no fills ever" check to fire. A position fully exited via either signal-exit or stop fill reaches `closed` correctly once its close-side per-order row terminates. A position with negative `current_qty` reaches `error`, surfacing the data-integrity violation rather than silently completing.

### 6.7 What this eliminates

- `_suspect_orders` cache — replaced by per-order rows in `working` / `unknown` status
- `_suspect_residual_orders` cache — same
- The "extend `position_lifecycle` with `entry_residual_order_id` / `entry_stop_order_id` / ..." path
- The shadow-state problem for replacement stops, exits, and partial closes

---

## 7. Why this wasn't caught earlier

The discovery doc would be incomplete without acknowledging that the foundation gap was visible before PR #58 ran into it. This section captures the process learning so the foundation PR's plan documents can refer back to it.

### 7.1 Three reinforcing causes

1. **Proposal §17 collapsed write-side substrate and read-side consumers into one "deferred adoption" bucket.** §17 says "let each subsystem adopt `position_uid` organically when it is next touched. A standalone 'wire it everywhere' PR is not recommended." For read-side consumers (health monitor, dashboard, PnL reporting, backtest reconciliation) this is correct — they can adopt independently. For write-side substrate (entry path, recovery, slippage benchmark capture, stop repair, exit logging) it is wrong: these aren't subsystems that *benefit* from `position_uid`, they're the substrate every read-side consumer depends on. Treating both the same encouraged write-side wiring to be deferred too.
2. **Slippage Phase 1's review caught a yellow flag and we extended the in-memory cache instead of asking what the cache was substituting for.** Defect 2 of the Phase 1 review correctly identified that `SuspectOrder` was dropping fallback provenance. The fix added `modeled_price_kind: str` to `SuspectOrder`. The right question at that moment was "why does pre-fill order intent need to live in a frozen in-memory dataclass?" The answer would have been "because there's no durable place for it" — and that would have surfaced the foundation gap. We treated the cache as the answer rather than as evidence of a missing substrate.
3. **PR #58's review cadence was the strongest signal and we read it wrong.** Eight rounds where each round finds a *structurally different* manifestation of the same root cause — order intent has nowhere durable to live, recovery state is in-memory only, the lifecycle row holds one order's metadata — is "the substrate is wrong," not "the implementation is buggy." We treated it as the second when it was the first. R3 or R4 was the latest point a deliberate "step back" audit would have caught it.

### 7.2 What would have changed the outcome

If during slippage Phase 1's Defect 2 fix we had paused and asked "should this field live on `position_lifecycle` instead of `SuspectOrder`?" — the foundation PR would have shipped in early June, slippage Phase 2 would have read from the lifecycle row, and PR #58 would have been ~3 rounds instead of 8.

### 7.3 Heuristics to carry forward

- **Adding a field to an in-memory cache is a yellow flag.** Ask whether the cache exists because the substrate is incomplete.
- **Phased rollouts that mark "consumer migration" deferred should explicitly distinguish read-side consumers from write-side substrate.** §17 should be split.
- **Review-round count climbing while each round finds structurally different bugs is a substrate-not-implementation signal.** Worth pausing for a fresh audit after R4 of any PR.

---

## 8. Glossary

- **Lifecycle row** — one row in `position_lifecycle`. The position-level identity record.
- **Per-order row** — one row in `position_lifecycle_orders` (proposed §6). The durable record of one specific order in a position's life.
- **Pending row** — lifecycle row with `status='pending'`. Created before broker submit; awaiting fill confirmation. Under the proposed shape, this status applies to the position-level row only when no order has reached `working` yet.
- **owner_key** — `engine.positions.owner_key_for(symbol)`. Equity = ticker, options = underlying ticker, spread = per-instance UUID. Used as the broker-aggregation key.
- **Recovery row** — a trade-log row written for a fill that was not observed synchronously. Currently tagged `slippage_measurement_quality='recovered'`.
- **Suspect order** — an order whose terminal state the bot didn't confirm before the synchronous timeout. Held in `_suspect_orders` (entry) or `_suspect_residual_orders` (hybrid residual). Both caches go away with the per-order table.
- **Synthesized row** — a lifecycle row created retroactively to back a broker-open position the bot didn't originate. Tagged `metadata.synthesized=true`.
- **Hybrid entry / residual** — PR #58's split-entry: whole shares as a STOP_LIMIT primary, fractional remainder as a MARKET residual. Two orders, one logical position.
- **`last_observed_broker_updated_at`** (§6.2 column / §6.4 atomicity) — the most recent `Order.updated_at` value the bot has observed for this per-order row. Anchors the staleness check; combined with state-machine rank + filled_qty ordering it enforces exactly-once application without depending on a fabricated event sequence.
- **`origin_kind` / `operator_command_uid`** (§6.2 columns) — schema-compatible hooks for future Operator Controls Phase C orders. Bot-originated rows leave `origin_kind='bot'` and `operator_command_uid` NULL. Foundation PR does NOT implement destructive operator commands.
- **`execution_id`** (§6.5) — Alpaca's per-fill identifier on a stream `trade_update` event. A single order with N partial fills produces N distinct `execution_id`s. Foundation PR persists it as **optional audit-only metadata** on `trades` — no UNIQUE constraint, no consumer reads it. The fill-row dedup key is `order_id`, paired with cumulative `filled_qty` / VWAP UPSERT semantics (§6.5). REST recovery has no `execution_id` available and leaves it NULL. PR #59 review-4 rejected an earlier draft that would have made `trades` one row per execution gated by a UNIQUE on `execution_id`; that would have created a parallel execution ledger with breaking consumer impact.
- **State-machine rank** (§6.3 / §6.4) — `pending=0`, `working=1`, `unknown=1`, `partially_filled=2`, `filled=3`, `canceled=3`, `rejected=3`. Used lexicographically with `filled_qty` to determine whether an incoming event strictly advances the per-order row.
- **Side-signed sum** (§6.6) — the position-level `current_qty` rollup is `SUM(filled_qty * sign(side))`, not raw `SUM(filled_qty)`. Entries and exits net out correctly; a canceled-after-partial-fill row contributes its preserved `filled_qty` exactly once.
- **Wired role vs. schema-only role** (§9.0) — the role column accepts six values. Foundation PR ships code paths for `entry_primary`, `protective_stop`, `replacement_stop`, `exit` (the four roles every current strategy uses). `partial_close` was wired by Operator Controls Phase C (PR #66, merged 2026-06-16). `entry_residual` remains schema-only pending PR #58 rebuild. The position-level rollup is authoritative when every order on the position uses a wired role.

---

## 9. Dependency matrix — what this foundation PR owns

Per the cross-workstream phase-boundary feedback on PR #59, the foundation work must classify every touched item so we don't silently absorb whole future phases.

### 9.0 Per-role wiring matrix (PR #59 review-3 P2 fix)

The `role` column in `position_lifecycle_orders` is an open enum (§6.2). The foundation PR ships code paths for some roles and leaves others as schema-only enum values awaiting a future PR. **The position-level rollup (§6.6) is authoritative only for positions whose every order maps to a wired role.** A position whose strategy creates an as-yet-unwired role (e.g. PR #58's `entry_residual`) would have an incomplete rollup until that role is wired.

| Role | Wired in foundation? | Code path owner | When the rollup becomes authoritative |
|---|---|---|---|
| `entry_primary` | ✅ Wired | Foundation PR. `_lifecycle_begin` already creates the pending row; foundation adds `apply_order_event` for all status transitions. | Immediately on merge. |
| `protective_stop` | ✅ Wired | Foundation PR. The OTO child stop attached at submit becomes its own per-order row with `parent_order_id = entry_primary.order_id`. | Immediately on merge. |
| `replacement_stop` | ✅ Wired | Foundation PR. PR #47's GTC-promotion path ([broker.py:1289 region](../execution/broker.py:1289)) already replaces a DAY child with a GTC; foundation writes the replacement as a new per-order row with `replaces_order_id = previous_stop.order_id`. | Immediately on merge. |
| `exit` | ✅ Wired | Foundation PR. Every `_log_close` / `_close_lifecycle_for_owner_key` path produces an `exit` per-order row. | Immediately on merge. |
| `entry_residual` | 🟡 Schema-only | PR #58 rebuild. Enum value exists; no writer in foundation. | When PR #58 rebuild wires Donchian hybrid. Until then no current strategy needs this role. |
| `partial_close` | ✅ Wired | Operator Controls Phase C (PR #66, merged 2026-06-16). `reduce-position` writes a `partial_close` per-order row tagged `origin_kind='operator'` + `operator_command_uid` via `broker.close_position(partial_qty=...)`. | Immediately on PR #66 merge. No bot-originated path creates partial closes; only the operator queue does. |

**Consequence for live readiness — and what the rollup does NOT cover.** The four wired roles cover production strategies that operate at the **single-leg level**: SMA Crossover, RSI Reversion, Donchian Breakout (classic market entries), and single-leg SPY options. The position-level rollup is authoritative for every position those strategies open.

**MLEG spreads (credit_spread strategy) remain on their existing path.** Spread entries and exits do not use any of the four wired roles. They continue to use `log_spread_fill` and the existing `trades` aggregation. The foundation PR adds no `position_lifecycle_orders` writes for spread legs, and the per-order rollup at §6.6 is **not authoritative for spread positions** — those positions remain visible through `log_spread_fill`'s rows on `trades` and through the spread-side ownership tracking in `engine.positions`. PR #59 review-4 (P2) correctly pushed back on a prior wording that implied spreads benefited from foundation wiring; they do not. Spread lifecycle wiring is the separate spread-lifecycle PR's responsibility ([3] in the recommended sequence).

`partial_close` was unlocked by Operator Controls Phase C (PR #66, merged 2026-06-16). `entry_residual` remains schema-only until PR #58 rebuild wires Donchian hybrid. Both unlock without a schema change.

### 9.1 Moved INTO this foundation (correctness prerequisites)

| Item | Why it's here |
|---|---|
| `position_lifecycle_orders` table per §6.2 | The per-order durable record is the substrate that eliminates the suspect-order caches. Required for correctness of recovery, idempotency, and §8.1 enforcement. |
| `apply_order_event` API per §6.4 | Single chokepoint for stream / cycle / startup events. Without it the three reconciliation paths cannot honor exactly-once. |
| `entry_order_id` actually populated on the position-level row (as a non-authoritative mirror of `entry_primary.order_id`) | Backward compatibility for any consumer currently reading the column. The authoritative copy lives on the per-order row. |
| Stream-driven entry-fill plumbing for equity entries | Required so a stream-delivered fill on a resting STOP_LIMIT or LIMIT advances the lifecycle. The single-leg options worker pattern is the prototype. |
| Cycle-level reconciliation of non-terminal per-order rows by `order_id` | The reverse-close pass already exists; the forward-non-terminal pass does not. Required for correctness of every non-synchronous fill path. |
| Startup downtime fill / cancel discovery against broker closed-order history | Required so a pending order that resolved during downtime can be advanced from the actual `order_id` rather than guessed from open broker state. |
| Persist pre-fill `slippage_benchmark_*` provenance on the per-order row | Required so a recovered fill propagates honest provenance into `trades` rather than fabricating `unavailable` or worse. |
| §8.1 invariant enforced at the position-level rollup (per-order rows may legitimately `partially_filled → canceled`) | Correct state machine. The store-boundary guard at [engine/lifecycle.py:486](../engine/lifecycle.py:486) stays in place. |

### 9.2 Schema-compatible only (no behavior added in this PR)

| Item | Schema artifact | Behavior in this PR |
|---|---|---|
| Operator-issued order origin | `origin_kind` (default `'bot'`), `operator_command_uid` (NULL) on `position_lifecycle_orders` | No destructive operator commands implemented. Phase C populates these columns when it ships. |
| Spread (MLEG) order lifecycle | Per-order schema is shape-compatible (`role`, `parent_order_id`, `replaces_order_id` carry MLEG legs naturally) | No spread `create_pending` / `apply_order_event` paths wired. Spread lifecycle remains deferred. |
| Future order roles beyond §6.1's six | `role TEXT` is open enum | This PR ships only the six roles enumerated in §6.1. |
| Bracket take-profit | `intended_take_profit_price` column exists | No strategy uses bracket TP today; column is reserved for future strategies. |

### 9.3 Still deferred (out of scope here)

| Item | Where it lives |
|---|---|
| ~~Operator Controls Phase B~~ ✅ SHIPPED (PR #65) | ~~Operator Controls Phase B PR~~ |
| ~~Operator Controls Phase C~~ ✅ SHIPPED (PR #66, merged 2026-06-16) | ~~Operator Controls Phase C PR~~ |
| `Position.position_uid` engine-state integration, dashboard exposure, broad alert adoption | Separate consumer PRs per §17 |
| Health monitor / sleeve allocator / PnL reporting / backtest reconciliation adoption of `position_uid` | Separate consumer PRs per §17 |
| Slippage Phase 2 consumer migration (health, risk kill switch, calibration script, dashboard display + denominator fix, legacy dual-write removal) | Slippage Phase 2 PR |
| Slippage Phase 3 historical cleanup (phantom recovery rows, pre-`8316e64` LIMIT rows) | Slippage Phase 3 PR |
| Spread lifecycle wiring | Separate spread-lifecycle PR |
| Implementation-shortfall or trigger-time-quote metrics | Out of taxonomy; not in scope anywhere current |

The PR-#58 capability (Donchian stop-limit + hybrid residual) is rebuilt AFTER this foundation lands, on top of it. Not in this PR.

---

## 10. Compensating-patch absorption matrix — implementation migration checklist

ChatGPT's two-week audit (May 29 – June 12, 2026) identified workarounds that exist in production code today because the durable per-order substrate was missing. The foundation PR absorbs these in a single migration rather than handling them as isolated follow-ups. Each subsection below is one category from the audit: the workaround the foundation replaces, what behavior must be preserved, the safety invariants and tests that must survive the migration, and the audit's recommended sequence.

This matrix is the **implementation PR's migration checklist**. Every row should be addressed in the implementation PR, not deferred.

### 10.1 Entry uncertainty, duplicate prevention, pending-row grace

| Aspect | Detail |
|---|---|
| **Workarounds today** | `_suspect_orders` (§4.1); `LIFECYCLE_PENDING_GRACE_SECONDS` reverse-pass guard (§3.1); broker-open-order duplicate checks before entry submit; PR #58's `_restore_suspect_orders_from_broker` startup walk; PR #58's residual drains and symbol-keyed ownership shims |
| **Anchor commits** | `88d1a45` (operator-queue / pending grace), `021fa63`, `4c727ae`, PR #58 R5–R7 patches `8ce0dee..00f9e53` |
| **Foundation absorption** | One `apply_order_event` path drives stream, cycle, and startup. `pending → working → partially_filled → filled` advances on observed broker state. Duplicate-entry prevention is now **layered**: (a) the position-level `uniq_one_active_position_per_owner_key` partial unique index on `position_lifecycle` per §6.2 catches duplicates at write time; (b) the per-order `uniq_one_entry_primary_per_position` constraint is belt-and-suspenders within a position; (c) the **existing broker-snapshot check before submit stays as defense-in-depth** (PR #59 review-10 P1 fix). The DB constraints catch what the bot's own state already knows; the snapshot check catches scenarios where the DB doesn't yet know about a broker-side open order (restart races, missed stream events, externally-introduced orders). Removing the snapshot check would create a parallel-state risk; keeping it does not — both layers point at the same broker reality and a successful submit requires both to pass. |
| **Preserved invariants** | (a) No duplicate entry while a non-terminal order exists for the same position. (b) Exact `client_order_id` / `order_id` reconciliation — never fuzzy match by symbol. (c) TIMEOUT and UNKNOWN remain recoverable through the next cycle/startup. (d) Slippage benchmark provenance survives restart (see §10.5). (e) A position is not marked `external_closed` while an order can still create or close it |
| **Tests that survive** | PR #58's R5–R7 acceptance tests for hybrid recovery, the duplicate-entry guard tests from `88d1a45`, the pending-grace integration test |
| **Pending-grace fallback** | `LIFECYCLE_PENDING_GRACE_SECONDS` may remain as a **bounded compatibility fallback during migration** — not the primary state model. Removed entirely only after `apply_order_event` is verified across stream / cycle / startup paths |
| **Symbol-keyed `_positions` cache** | Retained as the in-memory operational state — this is engine state, not a recovery substrate. The foundation only eliminates caches that *substitute* for the missing substrate |

### 10.2 Uncertain single-leg exits

| Aspect | Detail |
|---|---|
| **Workaround today** | `_suspect_exit_orders` (§4.3) — exit-side parallel of `_suspect_orders`. Memory-only, restart-volatile |
| **Anchor commits** | PR #53 — `28352a0` ("fix: recover uncertain single-leg exits"), `e0bedc2` ("fix: tighten exit history recovery bounds") |
| **Foundation absorption (PR #59 review-6 finding #3 fix)** | `role='exit'` rows replace `_suspect_exit_orders` for **discretionary exits only** — strategy-signaled market closes and (future) operator-issued closes. A **stop fill is NOT a new exit row.** When a protective or replacement stop fires, the existing `role='protective_stop'` or `role='replacement_stop'` row advances to `status='filled'` via `apply_order_event`. The position rollup (§6.6) handles the close correctly because the stop row's `side='sell'` contributes negatively to `current_qty` when it fills. Treating a stop fill as a new exit row would create a duplicate accounting event and break the side-signed sum |
| **Preserved invariants** | (a) Broker order-history queries only after the current lifecycle's entry timestamp. (b) Recovered cumulative sell quantity must explain the open quantity (no phantom fills). (c) Broker timestamps and VWAP preserved on the per-order row and propagated into `trades`. (d) External-close detection cannot race ahead of a known submitted close — the durable `role='exit'` row blocks the external-close path |
| **Tests that survive** | PR #53's R0–R2 acceptance tests for CIEN-style late-fill recovery, the bounded-history-window test from `e0bedc2` |
| **Cache removal timing** | After `role='exit'` is verified across stream / cycle / startup paths. Not on day-one of foundation merge |

### 10.3 Protective stop promotion, replacement, and repair

| Aspect | Detail |
|---|---|
| **Workarounds today** | PR #47's DAY-child → GTC promotion with `_reported_stop_promotion_failures` set used as identity workaround (because there's no durable handle to track which stop replaced which); PR #46's option-stop replacement hardening (`d98c801`, `c936c57`, `39e0b94`); inferring "which stop is current" from broker snapshot symbols rather than durable identity |
| **Anchor commits** | `3236593`, `1bdf05e` (PR #47 — capped equity stop durability and DAY-to-GTC replacement); `d98c801`, `c936c57`, `39e0b94` (PR #46 option-stop hardening) |
| **Foundation absorption** | Every attached protective stop becomes a `role='protective_stop'` per-order row with `parent_order_id = entry_primary.order_id`. Every replacement becomes a `role='replacement_stop'` row with both `parent_order_id` (the entry) AND `replaces_order_id` (the stop it replaces). "Which stop currently covers this position?" becomes one query against the orders table. The IDENTITY-WORKAROUND part of `_reported_stop_promotion_failures` goes away — the set previously held inferred `(parent_order_id, child_order_id)` tuples to associate "this child's promotion failed with this parent." With durable per-order identity the set re-keys directly on the failed child's `order_id`. |
| **Alert-deduplication continuity (PR #59 review-6 finding #5 fix)** | The `_reported_stop_promotion_failures` set still has a legitimate **session-local alert-deduplication** purpose — without it, every cycle's failed promotion attempt alerts again. Foundation does NOT delete the set; it re-keys the entries on the durable `order_id` of the failed replacement-stop row and otherwise leaves the alert suppression behavior unchanged. The set remains an in-memory session cache (alert dedup is session-level, not durable). What the foundation removes is only the IDENTITY workaround (inferring which child belongs to which parent), not the alert suppression itself. |
| **Preserved operational behavior** | Broker-native replace semantics (Alpaca's `ReplaceOrderRequest` path), bounded alerting on promotion failure, immediate protection repair when a managed position has no broker-side stop, GTC/DAY semantics including the 90-day GTC expiry constraint |
| **Atomic replacement requirement** | A replacement creates a NEW per-order row AND advances the replaced row's status (typically to `canceled`) inside the same transaction. The replacement must NEVER overwrite the identity of the stop it replaces — `replaces_order_id` is the linkage, not a mutation of `order_id` |
| **Tests that survive** | PR #47's GTC-promotion-after-fill acceptance test, the retry-and-bounded-alerting tests, the GTC/DAY-reconciliation-from-broker-snapshot tests |

### 10.4 Option trailing state — split responsibilities

| Aspect | Detail |
|---|---|
| **Workaround today** | `option_trailing_stops` table (defined at [engine/option_trailing.py:11](../engine/option_trailing.py:11)) mixes legitimate strategy state with duplicate order-lifecycle state |
| **Anchor commits** | `d86aa5e`, `5931361`, `d98c801`, `fdabede` (PR #40 / PR #46) |
| **Strategy state that stays** | `entry_premium`, `hwm_premium`, `trail_activation_pct`, `trail_pct`, `current_stop_price` (the strategy's derived target), `last_observed_premium`. These are strategy decisions, not broker order state. Keep the columns; keep the read/write paths |
| **Broker-order state that migrates** | `alpaca_stop_order_id`, `stop_order_status`. These ARE order-lifecycle state and become authoritative on `position_lifecycle_orders` (as a `role='protective_stop'` or `role='replacement_stop'` row keyed to the option position's `position_uid`) |
| **Migration shape (PR #59 review-6 finding #2 fix)** | A SQLite FK cannot target a column with only a *partial* unique index — and `position_lifecycle_orders.order_id` has `UNIQUE WHERE order_id IS NOT NULL`, which SQLite doesn't accept as an FK parent key. The correct FK target is the autoincrement PK: add `lifecycle_order_id INTEGER REFERENCES position_lifecycle_orders(id)` to `option_trailing_stops`. The trailing table reads broker-order identity / status via the FK join. `option_trailing_stops.alpaca_stop_order_id` and `stop_order_status` columns can remain as denormalized mirrors during migration, with the per-order row authoritative; strict column removal is deferred. |
| **Preserved operational behavior** | HWM restoration on startup, missing-HWM fail-safe alerts, durable HWM across restarts (PR #46's core durability promise) |
| **Tests that survive** | PR #46's HWM restoration / fail-safe-alert / atomic-replacement integration tests |

### 10.5 Slippage recovery patches — preserve provenance as a correctness requirement

| Aspect | Detail |
|---|---|
| **Workarounds today** | Pre-fill `slippage_benchmark_*` provenance lives in `SuspectOrder` (in memory); recovered fills depend on the cache surviving to write honest provenance to `trades` |
| **Anchor commits** | `d2d9509` ("Preserve broker timestamps in recovery paths"), `e9cde89`, `4c727ae` (slippage Phase 1 Defect 2 fix: SuspectOrder.modeled_price_kind), `e055d25` (stream stop_price hotfix), slippage Phase 1 PR #43 |
| **Foundation absorption** | Pre-fill `slippage_benchmark_price`, `_kind`, `_timestamp`, `_measurement_quality` move from `SuspectOrder` onto each `position_lifecycle_orders` row at create time. On recovery, the per-order row IS the source of provenance — the trades UPSERT propagates it via `COALESCE` (§6.5) |
| **Preserved as correctness requirements** | These are NOT disposable workarounds. The audit explicitly names: broker timestamps preserved, cumulative `filled_qty`/VWAP preserved, stream `stop_price` round-trips through the SimpleNamespace reconstruction (`e055d25`), canonical `slippage_benchmark_*` taxonomy. All must survive the migration |
| **Boundary preserved** | Computed `slippage_signed_bps` / `slippage_adverse_bps` stay on `trades`. The per-order table owns pre-fill INTENT; it does NOT become a second computed-slippage store. Phase 2 consumer migration is NOT in this PR |
| **Tests that survive** | Slippage Phase 1's full per-codepath contract tests, the Defect 1–5 fix tests, the stream `stop_price` round-trip test (`e055d25`) |

### 10.6 Position-level partial-close / accounting fixes

| Aspect | Detail |
|---|---|
| **Workarounds today** | Various ad-hoc fixes that established correct partial-close and recovery accounting semantics |
| **Anchor commits** | `7bdd238`, `a20357d` (PR #33 position_uid foundation), `e151830`, `21120a9`, `095fe19` |
| **Behaviors that survive as acceptance tests** | (a) Partial fills / closes keep the logical lifecycle `open` / `partially_filled` at the realized quantity. (b) Residual quantity is preserved across partial events. (c) Full-close trade counting happens once per `position_uid` (no double-count when stop and signal-exit interleave). (d) Recovered stop / exit fills close the lifecycle correctly via the same `_record_realized_pnl` → `_close_lifecycle_for_owner_key` path |
| **Foundation responsibility** | The §6.6 side-signed rollup must produce the same observable accounting as today. These tests stay green when the rollup math replaces the event-driven mutations |
| **What the foundation does NOT touch** | Allocator / health monitor / dashboard consumer migration. The foundation only ensures existing consumers continue to read correct values from `trades` and `position_lifecycle` |

### 10.7 MLEG partial-close pending state — defer behavior, reserve substrate

| Aspect | Detail |
|---|---|
| **Workaround today** | `_spreads_pending_close: set[str]` ([engine/trader.py:406](../engine/trader.py:406)) is in-memory only; PR #56's restart-gap risk is real but acknowledged as out-of-scope for that PR |
| **Anchor commits** | `9ccd7aa`, `1365742`, `b710aea`, `9e0f533`, `ffa0353` (PR #56) |
| **Foundation does NOT solve the spread side** | The audit is explicit: **do not** solve the spread restart gap with `engine_state.json` or a parallel persistence layer. The spread lifecycle PR is the right home — it should write combo close orders into the same per-order substrate and derive pending-close state from non-terminal order rows. The foundation PR just keeps the schema spread-compatible (§6.2's `role`, `parent_order_id` columns work for MLEG legs naturally) |
| **Foundation DOES solve the single-leg side (PR #59 review-7 P1 fix)** | For single-leg equity / single-leg option positions, the new `uniq_one_active_close_per_position` partial unique index in §6.2 IS the durable analog of `_has_pending_close_order()`. At most one non-terminal `role IN ('exit', 'partial_close')` row per `position_uid`. A second close attempt against the same position fails at the DB layer with a constraint violation — no in-memory cache required, restart-safe. The implementation PR's close path queries for any non-terminal close row before submitting; the unique index is belt-and-suspenders against races. The single-leg analog of `_spreads_pending_close` retires immediately on foundation merge; the spread-side cache stays until the spread lifecycle PR writes spread closes into the substrate |
| **Preserved until spread lifecycle PR ships** | PR #56's fail-safe partial logging, residual ownership preservation, duplicate-dispatch block via `_spreads_pending_close` (in-memory cache stays), CRITICAL alert on partial close |
| **Migration trigger** | Spread lifecycle PR writes spread entry / exit / close as `position_lifecycle_orders` rows keyed under the spread's `position_uid`. Pending-close state becomes "any non-terminal `role='exit'` order on this spread `position_uid`." `_spreads_pending_close` retires at that point |

### 10.8 PR #58 disposition — rebuild, do not cherry-pick

| Aspect | Detail |
|---|---|
| **What does NOT come back** | PR #58's lifecycle plumbing: `skip_lifecycle` shim, `_suspect_residual_orders` cache, pending drains keyed on `current_qty`, startup-open-order reconstruction, symbol-keyed TIMEOUT ownership |
| **What CAN be cherry-picked into the PR #58 rebuild** | Strategy / order-construction business logic only — STOP_LIMIT semantics in the backtest harness, `BaseStrategy.latest_trigger_price` hooks, `OrderType.STOP_LIMIT` enum + `Signal` / `RiskDecision` field additions, hybrid sizing math, pure unit tests that don't depend on the parked caches |
| **Rebuild target** | STOP_LIMIT primary + fractional residual entries implemented as TWO durable `position_lifecycle_orders` rows under ONE `position_uid` — one `role='entry_primary'` (STOP_LIMIT), one `role='entry_residual'` (MARKET). `entry_residual` becomes a wired role at that point (was schema-only per §9.0) |
| **Sequence** | PR #58 rebuild happens AFTER the foundation PR merges, smoke-confirms, and the four currently-wired roles are exercised on production. Not before |

### 10.9 Recommended migration sequence

The audit's recommended order, restated:

1. Implement per-order schema (§6.2) + store + atomic `apply_order_event` (§6.4).
2. Migrate entry, normal exit, protective stop, and replacement-stop writers / recovery; carry the §10.1–§10.5 tests forward.
3. Remove `_suspect_orders` / `_suspect_exit_orders` and reduce `LIFECYCLE_PENDING_GRACE_SECONDS` only after restart and mixed stream/REST tests pass against the new path.
4. Rebuild PR #58 on the substrate (per §10.8).
5. Wire MLEG orders in the separate spread lifecycle PR using the same table / API (per §10.7).

Everything else in the May 29 – June 12 window (risk halt coverage, feed/cache work, watchlists, research filters, EOD summary, MLEG walk pricing itself) is independent of this foundation and remains untouched.

---

## 11. Ownership boundaries between the three tables

Explicit so the implementation PR cannot silently expand any one table into another's responsibility.

| Table | Owns | Does NOT own |
|---|---|---|
| `position_lifecycle` | One row per logical position. Aggregate state (`status`, `current_qty`, `avg_entry_price`, `net_realized_pnl`). Identity (`position_uid`, `owner_key`, `strategy`, `position_type`). Lifecycle creation / termination timestamps. | Per-order metadata. Order-level state. Computed slippage values. Trade-by-trade fills. |
| `position_lifecycle_orders` (new) | Durable per-order intent (order_type, order_class, TIF, intended prices, qty). Broker identity (`order_id`, `client_order_id`). Per-order lifecycle state and reconciliation anchor. Pre-fill slippage benchmark provenance. Relationships between orders (parent / replaces). Origin (bot vs. operator command). | Position-level rollups. Computed realized slippage. Trade-row primary keys. |
| `trades` | **One row per `order_id`** carrying cumulative aggregate state (`filled_qty`, `avg_fill_price` VWAP). Realized P&L. Computed `slippage_signed_bps` / `slippage_adverse_bps`. Slippage measurement quality of the actual fill. Optional `execution_id` as audit metadata only. | Pre-submit order intent. Per-order broker state. Order relationships. Per-execution detail (out of scope; would require a separate execution ledger, deliberately not introduced). |

This boundary is the load-bearing answer to "the per-order table is not a second slippage-reporting store." Foundation PR persists pre-fill benchmark provenance on the order row; the trades row continues to be the source of truth for computed slippage. Phase 2 consumer migration reads computed values from `trades` exactly as it does today.

**Idempotency keys differ between the two tables, but both use cumulative state.** §6.4 / §6.5 spell this out:

- `position_lifecycle_orders` dedups state transitions by `(order_id, last_observed_broker_updated_at)` plus state-machine rank. Each row holds the order's most recent observed state.
- `trades` dedups fill rows by `order_id` via UPSERT. Each row holds the order's cumulative `filled_qty` and `avg_fill_price` VWAP.
- `execution_id` is audit metadata on `trades`; it is not the dedup key. REST recovery has no `execution_id` (the Alpaca REST endpoint exposes cumulative order state only) and writes NULL there.

PR #59 review-4 ("keep one persistence system") rejected an earlier draft that would have made `trades` one row per execution. Per-execution history would have been a parallel ledger with breaking consumer impact; the foundation PR explicitly does not go that route.

---

## 12. Documentation updates required when foundation PR lands

The cross-workstream phase-boundary feedback notes that previously documented phase assumptions become stale once this foundation lands. The implementation PR must update:

- **`PLAN.md`** — Live readiness gate row for slippage / operator controls needs to note that the foundation PR is in flight or merged, and that PR #58 is awaiting rebuild on it.
- **`docs/operator_controls_proposal.md`** — §17 explicitly notes that `position_uid` adoption is "deferred organic." This is partly stale: single-leg option entry async callbacks already exist in code, and the foundation PR makes the per-order substrate first-class. The proposal should be amended to call out (a) the write-side substrate vs. read-side consumer split (per §7.1 of this doc), and (b) that the per-order table is now the substrate Phase C commands write into.
- **`docs/slippage_unification_tracker.md`** — Phase 2 consumer migration scope did not previously contemplate reading pre-fill benchmark provenance from a lifecycle row. The tracker should note that the foundation PR persists this provenance, and that recovered-fill rows now have a durable source for the kind / quality tags rather than relying on `_suspect_orders` in memory.
- **PR #58 description** — should be marked draft with a top comment listing what's blocked vs. cherry-pickable per §10.8 (lifecycle plumbing rebuilds; strategy / order-construction logic and pure tests can be cherry-picked). Foundation PR description should link back to PR #58 for the rebuild scope.

These doc updates are a required part of the foundation PR, not a follow-up.

### 12.1 Database-level regression tests the implementation PR must include

Several findings during PR #59 review would have shipped as production bugs if not caught at the doc stage. The implementation PR must include database-level regression tests for each — these tests double as guards against re-introduction.

| Test | Guards against | Source |
|---|---|---|
| Atomic `apply_order_event` with two unrelated `order_id`s where one has a stale `updated_at` — assert only the matching row updates | SQL precedence regression (review-3 P0) | §6.4 |
| Terminal-state immutability — apply event with newer `updated_at` to a `filled` row; assert no update | Terminal regression (review-3 P1a) | §6.4 / §6.3 |
| Side-signed rollup correctness — entry buy 10 + exit sell 10 produces `current_qty = 0`, not `20` | `SUM(filled_qty)` regression (review-3 P1b) | §6.6 |
| MLEG two-row insert under foundation schema — `log_spread_fill` writes two rows sharing one combo `order_id`, both succeed | UNIQUE-scope regression (review-5 correction 1) | §6.5 |
| UPSERT + partial-unique-index pair — INSERT...ON CONFLICT(order_id) WHERE order_id IS NOT NULL AND position_type='single_leg' DO UPDATE actually matches the partial index | SQLite UPSERT conflict-target alignment (review-5 correction 2) | §6.5 |
| All-or-nothing transaction — force the trades UPSERT to fail mid-`apply_order_event`; assert per-order row, trades row, position rollup, and position-level status are all unchanged | One-transaction discipline (review-4 P1b) | §6.4 |
| `execution_id` NULL on REST recovery path — cumulative-delta REST recovery does not fabricate execution_id | REST has no execution_id (review-4 P1a) | §6.5 |
| `_suspect_orders` removal does not break TIMEOUT/UNKNOWN recovery — disable the cache and confirm the durable path covers every scenario the cache used to | §10.1 invariants survive migration | §10.1 |
| `_suspect_exit_orders` removal — same shape, exit-side | §10.2 invariants survive migration | §10.2 |
| `replacement_stop` atomic-replace — promote DAY → GTC; assert new row created AND replaced row advanced to `canceled` in one transaction | §10.3 atomic replacement requirement | §10.3 / §6.4 |
| **Zero-fill working entry stays `pending`** — create a position, submit entry, broker returns `working` with `filled_qty=0`; assert position status is `pending`, NOT `closed` | Naive `current_qty == 0 → closed` rule (review-7 P0) | §6.6.1 |
| **Working protective_stop blocks `closed` transition** (R12-P1 supersedes R8-1) — fully exit a position via discretionary close; assert lifecycle status stays at `partially_filled` (NOT `closed`) while the `protective_stop` row is still `working`. Engine cancels the stop; once the stop's per-order row reaches `canceled`, the next `apply_order_event` re-evaluates and the position transitions to `closed` and lock releases | All non-terminal sell-side orders block closure (review-12 P1) | §6.6.1 |
| **`closed_at` set only on `closed` / `external_closed`** — drive a position to each terminal status in turn (`closed`, `external_closed`, `canceled`, `error`); assert `closed_at` is set for the first two and NULL for the last two | closed_at conflation (review-8 P2) | §6.6.1 |
| **`closed_at` reads new status, not old** — apply event that transitions `open → closed` via `apply_order_event`; assert `closed_at` is set in the SAME UPDATE statement (CTE-based read of new_status, not pre-update value) | SQL SET-clause evaluation order (review-9 P1) | §6.6.1 |
| **Negative `current_qty` maps to `error`** — synthesize a per-order state where rollup is negative (e.g. exit row with filled_qty exceeding entry row's filled_qty); assert position status is `error`, NOT `closed` | Data-integrity violation masked as success (review-9 P1) | §6.6.1 |
| **`'error'` retains owner_key lock** — create a position, force it to `error` status; assert a second entry attempt on the same `owner_key` fails with UNIQUE constraint violation | `'error'` excluded from owner_key lock (review-8 P3) | §6.2 |
| **Reverse-pass `_reconcile_position_lifecycle` skips `'error'` rows** — set up an `'error'` row whose `owner_key` is no longer in broker positions; assert the reverse pass does NOT transition it to `external_closed` | Auto-resolving error rows via broker snapshot (review-8 P4) | §3.1 |
| **Dedupe script defaults to detection-only with no side effects** — invoke `scripts/migrate_dedupe_trades.py` without args; assert the script lists conflicts, exits non-zero, and does NOT modify any rows | Silent auto-delete based on `filled_qty` (review-8 P2) | §12.2 |
| **Migration preflight covers `'error'` status** — seed a duplicate `owner_key` pair where one is `'error'` and another is `'open'`; assert preflight detects the conflict before `CREATE UNIQUE INDEX` runs (status set matches the index's WHERE clause exactly) | Preflight-vs-index status mismatch (review-9 P1) | §12.2 |
| **Working sell-side order of ANY role blocks `closed` and lock retains** (R12-P1 supersedes R10/R11 wording) — create an open position; fully exit via discretionary close; assert that while EITHER the exit OR the protective_stop row is non-terminal, position status is `partially_filled` (NOT `closed`), `closed_at` is NULL, AND a duplicate-entry attempt on the same `owner_key` is blocked by the lock. Advance the last remaining sell-side row to `canceled` or `filled`; re-apply `apply_order_event`; assert position transitions to `closed` and lock releases. Repeat for the case where the exit terminates first and only the protective_stop remains working | Working stops can fire after lock releases and oversell a fresh entry (review-12 P1) | §6.6.1 |
| **Oversold position → `error` immediately, regardless of pending sell-side orders** — same setup as above, but apply an over-fill that brings `current_qty < 0` while the exit is still `partially_filled`; assert position status is `error` immediately (the violation is realized) and lock retains | Negative-qty must surface before sell-side orders terminate (review-9 P1b + review-11 P1) | §6.6.1 |
| **Broker-snapshot entry guard still rejects shared-symbol submit** — disable the position-level DB UNIQUE index, leave the broker-snapshot pre-submit check enabled; assert a duplicate-entry attempt is still blocked (defense-in-depth, not parallel state) | Layered duplicate-entry defense (review-10 P1) | §10.1 |
| **Direct `pending → filled` transition allowed via fast path** — submit an entry that synchronously returns broker-side `filled` (no intermediate `working` event observed); apply the resulting event; assert the per-order row advances directly from `pending` to `filled` AND the position rollup updates correctly | State-machine diagram allowing direct pending → filled (review-11 P1) | §6.3 / §6.4 |
| **Direct `pending → canceled` transition allowed via recovery** — create a pending per-order row locally; simulate broker recovery (REST poll or startup walk) returning a terminal `canceled` status with `filled_qty=0` for that order; apply the resulting event; assert the per-order row advances directly from `pending` to `canceled` (no intermediate `working` observed) AND the §8.1 invariant holds (zero fills → position-level row can reach `canceled`) | State-machine diagram missing pending → canceled edge (review-12) | §6.3 / §6.4 |
| **`PRAGMA foreign_keys = ON;` is set and FK violations are rejected** — open a TradeLogger connection; attempt to INSERT a `position_lifecycle_orders` row with `position_uid` referencing a non-existent `position_lifecycle` row; assert the INSERT fails with `sqlite3.IntegrityError`. Repeat for `option_trailing_stops.lifecycle_order_id` referencing a non-existent `position_lifecycle_orders.id`. Without the PRAGMA, both INSERTs would silently succeed, leaving dangling FK references | SQLite FK enforcement is OFF by default (review-13 Gemini) | §6.2 |
| **`net_realized_pnl` rollup pulls from `trades`, not from `position_lifecycle_orders`** — open a position, fill an entry, fill an exit at a profit; assert `position_lifecycle.net_realized_pnl` equals `SUM(trades.realized_pnl WHERE position_uid = ?)` for that position. Repeat with a partial close (multiple realized-P&L rows on `trades`); assert the rollup correctly aggregates across rows | Per-order table has no realized_pnl column; trades is the source of truth (review-13 Gemini) | §6.6 |

The first three are explicit regressions where my earlier doc draft had the bug. The next two are the corrections from review-5. Tests 11–16 are correctness conditions added in review-7 and review-8 (zero-fill working entry, working stops + closed, closed_at semantics, error lock retention, reverse-pass error skip, dedupe-script safety). Tests 14, 15, and 19 are review-9 fixes (CTE-based closed_at read, negative-qty error path, preflight/index status alignment — typo in earlier draft said "14, 18, and 19"; review-11 typo fix). Tests 20, 21, and 22 are review-10 + review-11 + review-12 fixes (owner-lock retention extended to ALL non-terminal sell-side orders including stops, oversold immediate error, broker-snapshot defense-in-depth). Tests 23 and 24 are the `pending → filled` and `pending → canceled` direct transitions admitted by the strict-newer rule. Tests 25 and 26 are Gemini's review-13 fixes (SQLite FK enforcement PRAGMA, `net_realized_pnl` rollup source from `trades`). The remaining tests are correctness conditions the audit identified that the implementation must verify rather than assume.

### 12.2 Migration prerequisites — duplicate-row detection before applying UNIQUE indexes

PR #59 review-6 finding #4: any pre-existing duplicate rows in `data/trades.db` will cause the `CREATE UNIQUE INDEX uniq_trades_order_id_single_leg` step to fail. SQLite refuses to add a unique index when existing rows violate it, and `_ensure_db()` would then raise on every bot start until manually resolved. The same risk applies to `uniq_one_active_position_per_owner_key` if any production `position_lifecycle` row pair shares an `owner_key` while both are non-terminal.

The implementation PR must include a **pre-flight duplicate check** that runs BEFORE `CREATE UNIQUE INDEX`, on every bot startup until the index exists. Shape:

```sql
-- Detect duplicates that would block the new index on `trades`.
SELECT order_id, position_type, COUNT(*) AS n
FROM trades
WHERE order_id IS NOT NULL AND position_type = 'single_leg'
GROUP BY order_id, position_type
HAVING COUNT(*) > 1;

-- Detect duplicates that would block the new index on `position_lifecycle`.
-- Status set MUST match uniq_one_active_position_per_owner_key's
-- WHERE clause exactly (§6.2). PR #59 review-9 P1 fix: 'error' was
-- previously omitted here despite being in the index, so duplicates
-- where one row is 'error' and another is 'pending'/'open'/etc.
-- would slip through preflight and break CREATE INDEX at runtime.
SELECT owner_key, COUNT(*) AS n
FROM position_lifecycle
WHERE status IN ('pending', 'open', 'partially_filled', 'error')
GROUP BY owner_key
HAVING COUNT(*) > 1;
```

If either query returns rows, the migration **must abort startup**. PR #59 review-7 P1 correctly flagged that running in a "partially migrated, mixed legacy/foundation" mode is unsafe: foundation writer code is written assuming the unique indexes exist (the UPSERT predicate matches the partial index; `apply_order_event`'s rollup math assumes single-row-per-order semantics). Continuing without the indexes would either create more duplicates or produce silently wrong rollups. A loud failure forcing operator remediation is the only safe outcome.

Concrete behavior:

1. Log an `ERROR`-level message listing every affected `order_id` / `owner_key` value, plus the full row counts.
2. Surface through the alert backend so the operator sees it (Telegram + log file).
3. **Abort startup**: `_ensure_db()` raises and `forward_test.py` / `main.py` exits non-zero. The bot does NOT continue on the legacy code path; that mode does not exist in foundation code.
4. Document a remediation script (`scripts/migrate_dedupe_trades.py` or equivalent) that the operator runs offline. PR #59 review-8 finding #2 fix: the script **does not silently auto-delete rows**. Duplicate trade rows can carry conflicting accounting data (different `realized_pnl`, different `slippage_*` values, possibly different `position_uid`s in the legacy path), and "highest `filled_qty`" is not a safe proxy for "the row to keep" when accounting columns disagree. Auto-deletion would silently drop information the operator needs to see. The script's correct shape:

   - **Detection-only mode (default)**: re-runs the queries; emits a per-conflict report listing every column where the duplicate rows disagree (filled_qty, avg_fill_price, realized_pnl, slippage_*, position_uid, position_type, timestamp, reason). Exits non-zero so it can be wired into operator dashboards / cron without surprising deletes
   - **Operator review**: the operator inspects each conflict and decides whether to KEEP, MERGE (specifying which fields come from which row), or escalate (some conflicts represent a deeper data-integrity issue and aren't safe to "fix" by deleting one side)
   - **Apply mode (`--apply`)**: takes a JSON or YAML file produced by the review step, applies the operator's decisions one transaction per conflict, prints a summary, and re-runs the detection queries to confirm zero remaining duplicates
   - Same shape for `position_lifecycle.owner_key` duplicates — operator inspection required; some pairs represent legitimate edge cases (synthesized backfill row + pre-existing pending row) that need ad-hoc resolution
   - Exits 0 only after a clean detection run on the post-apply state

5. After the script's `--apply` step runs cleanly, the next `recycle_bot.sh` applies the unique indexes and bot starts.

Real-world sources of duplicates that this protects against:
- early bugs that re-logged the same `order_id` from both stream and REST paths before the slippage Phase 1 dedup work landed
- recovered-fill rows that wrote with the same `order_id` as the original log
- any path where `log_external_close` was called twice for the same broker close
- pre-Phase-A code that created multiple non-terminal lifecycle rows for the same `owner_key`

The migration must not silently mask these — they're a signal of past inconsistency that the operator must see and remediate before the new constraints take effect. The "abort on first conflict" pattern is the same shape as the Defect 3 fix from slippage Phase 1's `_ensure_db` work: partial migrations leave the system in an unrecoverable mixed state.
