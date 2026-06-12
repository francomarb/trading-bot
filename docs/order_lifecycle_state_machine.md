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
- the position-level `current_qty` is the rollup of `SUM(filled_qty)` across the position's orders' realized quantities and stays > 0
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

### 4.3 What the caches' existence reveals

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

-- Unique constraints (PR #59 review-2 fix: non-unique indexes don't enforce exactly-once).
-- order_id is NULL during pending; partial unique index permits multiple NULLs
-- but rejects duplicate non-NULL ids (SQLite supports this natively).
CREATE UNIQUE INDEX uniq_lifecycle_orders_order_id
    ON position_lifecycle_orders(order_id) WHERE order_id IS NOT NULL;
CREATE UNIQUE INDEX uniq_lifecycle_orders_client_order_id
    ON position_lifecycle_orders(client_order_id);

-- A position has at most one entry_primary and at most one open exit at a time.
-- Other roles (replacement_stop, partial_close) are intentionally many-per-position.
CREATE UNIQUE INDEX uniq_one_entry_primary_per_position
    ON position_lifecycle_orders(position_uid) WHERE role = 'entry_primary';

CREATE INDEX idx_lifecycle_orders_position_uid ON position_lifecycle_orders(position_uid);
CREATE INDEX idx_lifecycle_orders_status       ON position_lifecycle_orders(status);
CREATE INDEX idx_lifecycle_orders_parent       ON position_lifecycle_orders(parent_order_id);
CREATE INDEX idx_lifecycle_orders_replaces     ON position_lifecycle_orders(replaces_order_id);
```

Notes:

- **`order_class` vs `order_type`** — Alpaca's `OrderClass` (simple / bracket / OTO / OCO) is distinct from `OrderType` (market / limit / stop / stop_limit). Both are needed; the bot uses `OrderClass.OTO` for every equity entry today and `OrderClass.SIMPLE` for option entries.
- **`time_in_force`** — required to honestly reproduce intent across a downtime gap. Alpaca expires GTC at 90 days and DAY at session end; both are visible to the bot only if persisted.
- **`parent_order_id` / `replaces_order_id`** — the protective-stop child of an OTO entry has `parent_order_id = entry_primary.order_id`. A GTC-promoted replacement stop ([PR #47](https://github.com/francomarb/trading-bot/pull/47)) has `replaces_order_id = previous_stop.order_id` and `parent_order_id = entry_primary.order_id`. This makes "which stop currently covers this position" a single query against the orders table.
- **`origin_kind` + `operator_command_uid`** — schema-compatible only. Foundation PR does NOT implement destructive operator commands; the columns exist so Phase C can populate them when it ships. Bot-originated rows leave `origin_kind='bot'` and `operator_command_uid` NULL.
- **No computed slippage bps on this table.** Per the cross-workstream phase-boundary feedback: the per-order table owns pre-fill *intent* and *benchmark provenance*; computed `slippage_signed_bps` / `slippage_adverse_bps` stay on `trades`. The order table is not a second slippage-reporting store.
- **`created_at` vs `submitted_at` vs `terminal_at`** — three distinct moments. `created_at` is row insert (before broker submit). `submitted_at` is when submit returned (NULL while the row is `pending`). `terminal_at` is the move to a terminal status.

`position_lifecycle` keeps its position-level fields (`status`, `current_qty`, `avg_entry_price`, `net_realized_pnl`) as a rollup derived from the child rows. The position-level `entry_order_id` column is retained as a non-authoritative mirror of the `entry_primary` row's order_id for backward compatibility.

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

Allowed transitions:

```
pending  →  working | unknown | rejected
working  →  partially_filled | filled | canceled | unknown
partially_filled
         →  filled | canceled
unknown  →  any of the above (resolved by reconciliation)
filled   →  (terminal)
canceled →  (terminal)
rejected →  (terminal)
```

**Per-order vs. position-level invariant (PR #59 review-2 fix).** A `partially_filled → canceled` transition IS valid at the per-order level. That is how Alpaca describes a partially-filled order that gets canceled: the order terminates as `canceled` with its `filled_qty` preserved. The §8.1 invariant from the operator proposal is a *position-level* claim — the parent position must remain `open` (or `partially_filled`) at the filled quantity, never reach `canceled`. Under §6's two-table shape:

- **Per-order rows** record broker truth. `partially_filled → canceled` is allowed; `filled_qty` is preserved on the terminal row.
- **Position-level row** rolls up `current_qty = SUM(filled_qty across non-replaced realized orders)` and `status` accordingly. The store-boundary `mark_canceled` guard at [engine/lifecycle.py:486](../engine/lifecycle.py:486) keeps doing its job: a position whose roll-up `current_qty > 0` cannot transition to `canceled`. Belt-and-suspenders in `apply_order_event` (see §6.4).

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

SQLite executes a single `UPDATE` atomically. `rowcount == 0` means the event was stale, a duplicate, or targeted a terminal-state row — log and drop. `rowcount == 1` means the event was applied; the caller then runs the position-rollup update inside the same transaction (see §6.6).

`apply_order_event` is the single chokepoint that enforces:

- **Exactly-once** via the atomic where-clause above (the per-order row mutates at most once per broker-distinct event)
- **State-machine monotonicity** — terminal states stay terminal; updated_at is a tiebreaker WITHIN a state-machine-valid step, never a bypass
- **§8.1 position-level invariant** via the rollup step (§6.6)
- **Fill dedup on `trades`** — but **not** via `(order_id, updated_at)`. See §6.5 below for the correct dedup key.

### 6.5 Fill dedup on `trades` — keyed by `execution_id`, not order-snapshot timestamp

The order row's idempotency (§6.4) and the fill row's idempotency are **distinct mechanisms** keyed on **distinct identifiers**. Earlier drafts conflated them; the PR #59 review-3 P1 correctly flagged this.

| Concern | Key | Mechanism |
|---|---|---|
| **Per-order state transitions** on `position_lifecycle_orders` | `(order_id, last_observed_broker_updated_at)` plus state-machine rank | Atomic `UPDATE ... WHERE` per §6.4. `updated_at` is a per-order snapshot, fine for this. |
| **Per-fill rows** on `trades` | `execution_id` from the broker trade-update event | Each Alpaca trade-update fill event carries a unique `execution_id`. A single order with N partial fills produces N distinct `execution_id`s, all with the same `order_id`. `order_id + updated_at` cannot distinguish them — both share the order-level snapshot. |

`trades` gains an `execution_id TEXT` column with a `UNIQUE` partial index:

```sql
ALTER TABLE trades ADD COLUMN execution_id TEXT;
CREATE UNIQUE INDEX uniq_trades_execution_id
    ON trades(execution_id) WHERE execution_id IS NOT NULL;
```

Fill writers (`_log_entry`, `_log_close`, `log_stop_fill`, `log_spread_fill`) extract `execution_id` from the broker payload — present on stream `trade_update` events at `update.execution_id` and on REST recovery via the order's `legs[].id` / `event_id` field — and pass it through. Dedup at the SQL layer; the writer becomes idempotent because a duplicate insert violates the unique index.

Recovery / historical rows whose `execution_id` cannot be honestly reconstructed (recovered-entry-context, external close detection) write `NULL` rather than fabricating an id. The partial unique index permits multiple NULLs.

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

Canceled rows with `filled_qty = 0` contribute 0 to both rollups, so the partial-then-cancel case (per-order goes `partially_filled → canceled` with preserved `filled_qty > 0`) is captured correctly: the realized partial counts toward the position's current_qty even though the per-order row is terminal at `canceled`.

The §8.1 invariant on the position-level row uses this rollup: if `current_qty > 0`, the position-level row's status stays `open` / `partially_filled`. The store-boundary `mark_canceled` guard at [engine/lifecycle.py:486](../engine/lifecycle.py:486) continues to refuse `pending → canceled` when there are fills.

**Short-position math (out of scope today).** This PR wires only long-equity / long-options strategies. If a future strategy goes short, the rollup needs to know whether the position was *opened* by a sell (short entry) or *closed* by a sell (long exit). The cleanest way to handle that later is a column or convention on the per-order row (e.g. `role` already encodes the intent: `entry_*` rows are inflows regardless of side; `exit` / `partial_close` / `protective_stop` / `replacement_stop` are outflows). For now the foundation PR assumes long-only and uses `side` as a sufficient proxy.

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
- **`execution_id`** (§6.5) — Alpaca's per-fill identifier on a `trade_update` event. A single order with N partial fills produces N distinct `execution_id`s. This is the correct dedup key for `trades` rows; `order_id + updated_at` is wrong because both share the order-level snapshot. The foundation PR adds `execution_id` as a `UNIQUE` partial column on `trades`.
- **State-machine rank** (§6.3 / §6.4) — `pending=0`, `working=1`, `unknown=1`, `partially_filled=2`, `filled=3`, `canceled=3`, `rejected=3`. Used lexicographically with `filled_qty` to determine whether an incoming event strictly advances the per-order row.
- **Side-signed sum** (§6.6) — the position-level `current_qty` rollup is `SUM(filled_qty * sign(side))`, not raw `SUM(filled_qty)`. Entries and exits net out correctly; a canceled-after-partial-fill row contributes its preserved `filled_qty` exactly once.
- **Wired role vs. schema-only role** (§9.0) — the role column accepts six values. Foundation PR ships code paths for `entry_primary`, `protective_stop`, `replacement_stop`, `exit` (the four roles every current strategy uses). `entry_residual` waits for PR #58 rebuild; `partial_close` waits for Operator Controls Phase C. The position-level rollup is authoritative when every order on the position uses a wired role.

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
| `partial_close` | 🟡 Schema-only | Operator Controls Phase C. Enum value exists; no writer in foundation. | When Phase C ships destructive operator commands. Until then no path creates partial closes. |

**Consequence for live readiness:** the four wired roles cover every production strategy in flight today (SMA, RSI, Donchian classic, single-leg options, spread legs as a `protective_stop`-equivalent). The position-level rollup is authoritative for every position those strategies open. The two schema-only roles unlock without a schema change when their owning PRs land.

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
| Operator Controls Phase B (soft controls, command heartbeat, command alerts, Telegram queue migration) | Operator Controls Phase B PR |
| Operator Controls Phase C (destructive commands, symbol/owner-key locking, stop cancel/recreate, allocator + P&L reintegration, command execution recovery) | Operator Controls Phase C PR |
| `Position.position_uid` engine-state integration, dashboard exposure, broad alert adoption | Separate consumer PRs per §17 |
| Health monitor / sleeve allocator / PnL reporting / backtest reconciliation adoption of `position_uid` | Separate consumer PRs per §17 |
| Slippage Phase 2 consumer migration (health, risk kill switch, calibration script, dashboard display + denominator fix, legacy dual-write removal) | Slippage Phase 2 PR |
| Slippage Phase 3 historical cleanup (phantom recovery rows, pre-`8316e64` LIMIT rows) | Slippage Phase 3 PR |
| Spread lifecycle wiring | Separate spread-lifecycle PR |
| Implementation-shortfall or trigger-time-quote metrics | Out of taxonomy; not in scope anywhere current |

The PR-#58 capability (Donchian stop-limit + hybrid residual) is rebuilt AFTER this foundation lands, on top of it. Not in this PR.

---

## 10. Ownership boundaries between the three tables

Explicit so the implementation PR cannot silently expand any one table into another's responsibility.

| Table | Owns | Does NOT own |
|---|---|---|
| `position_lifecycle` | One row per logical position. Aggregate state (`status`, `current_qty`, `avg_entry_price`, `net_realized_pnl`). Identity (`position_uid`, `owner_key`, `strategy`, `position_type`). Lifecycle creation / termination timestamps. | Per-order metadata. Order-level state. Computed slippage values. Trade-by-trade fills. |
| `position_lifecycle_orders` (new) | Durable per-order intent (order_type, order_class, TIF, intended prices, qty). Broker identity (`order_id`, `client_order_id`). Per-order lifecycle state and reconciliation anchor. Pre-fill slippage benchmark provenance. Relationships between orders (parent / replaces). Origin (bot vs. operator command). | Position-level rollups. Computed realized slippage. Trade-row primary keys. |
| `trades` | Executed fills (one row per terminal fill event). Realized P&L. Computed `slippage_signed_bps` / `slippage_adverse_bps`. Slippage measurement quality of the actual fill. **`execution_id` for fill-event dedup (§6.5).** | Pre-submit order intent. Per-order broker state. Order relationships. |

This boundary is the load-bearing answer to "the per-order table is not a second slippage-reporting store." Foundation PR persists pre-fill benchmark provenance on the order row; the trades row continues to be the source of truth for computed slippage. Phase 2 consumer migration reads computed values from `trades` exactly as it does today.

**Idempotency lives on different keys on each table.** §6.4 / §6.5 spell this out: `position_lifecycle_orders` dedups state transitions by `(order_id, last_observed_broker_updated_at)` plus state-machine rank. `trades` dedups fill rows by `execution_id`. Confusing the two — using `updated_at` as a fill key or `execution_id` as an order key — produces either duplicate fill rows or refused state transitions; the doc explicitly separates them so callers cannot mix them up.

---

## 11. Documentation updates required when foundation PR lands

The cross-workstream phase-boundary feedback notes that previously documented phase assumptions become stale once this foundation lands. The implementation PR must update:

- **`PLAN.md`** — Live readiness gate row for slippage / operator controls needs to note that the foundation PR is in flight or merged, and that PR #58 is awaiting rebuild on it.
- **`docs/operator_controls_proposal.md`** — §17 explicitly notes that `position_uid` adoption is "deferred organic." This is partly stale: single-leg option entry async callbacks already exist in code, and the foundation PR makes the per-order substrate first-class. The proposal should be amended to call out (a) the write-side substrate vs. read-side consumer split (per §7.1 of this doc), and (b) that the per-order table is now the substrate Phase C commands write into.
- **`docs/slippage_unification_tracker.md`** — Phase 2 consumer migration scope did not previously contemplate reading pre-fill benchmark provenance from a lifecycle row. The tracker should note that the foundation PR persists this provenance, and that recovered-fill rows now have a durable source for the kind / quality tags rather than relying on `_suspect_orders` in memory.
- **PR #58 description** — should be marked draft with a top comment listing what's blocked vs. cherry-pickable (per the operator's directive). Foundation PR description should link back to PR #58 for the rebuild scope.

These doc updates are a required part of the foundation PR, not a follow-up.
