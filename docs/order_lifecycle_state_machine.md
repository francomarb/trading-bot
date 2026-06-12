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

### 2.3 The §8.1 invariant — verified

The proposal's invariant (any row with `current_qty > 0` cannot transition to `canceled`) IS enforced at the store boundary at [engine/lifecycle.py:486-501](../engine/lifecycle.py:486):

```python
if first_fill_at is not None or (current_qty or 0.0) > 0.0:
    raise ValueError("refusing to mark ... canceled — it has fills ...")
```

`_lifecycle_mark_filled` at [execution/broker.py:399-412](../execution/broker.py:399) also defends in depth: a CANCELED/REJECTED outcome with `filled_qty > 0` routes to `mark_partially_filled` rather than `mark_canceled`.

Foundation PR must continue to honor this invariant — both at the store API boundary and per-order, once a per-order table exists.

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

Site: [engine/trader.py:2437](../engine/trader.py:2437). Called by every exit code-path through `_record_realized_pnl`. Single-leg only — spread/options closes are deferred to Phase C.

### 3.5 Idempotency / exactly-once — currently implicit, must become explicit

A given broker outcome can today reach the lifecycle through multiple paths:

- WebSocket stream delivers a terminal `fill` event for an entry order
- Cycle `_recover_suspect_orders` polls the broker for the same order
- Startup `_restore_suspect_orders_from_broker` (PR #58's addition) walks open orders for the same handle

There is **no exactly-once contract.** Today's de-facto idempotency comes from:

- `mark_open` and friends are `UPDATE ... WHERE position_uid=?` — re-running them on an already-`open` row is not an error, but it overwrites `avg_entry_price` / `current_qty` with whatever the second caller passes. If stream sees `10@150.00` and a later REST poll sees `10@150.01` (mid-fill VWAP shift), the row drifts to the second caller's view.
- `_log_entry` writes to `trades` without an order_id uniqueness constraint. Two paths calling it produce duplicate fill rows.
- `_close_lifecycle_for_owner_key` reads-then-updates; if already closed it's a no-op. This is the strongest of the implicit guards.

The foundation PR must define exactly-once explicitly. Recommended shape, applicable per-order on the table proposed in §6:

> Every observed broker outcome is keyed by `(position_uid, order_id, event_kind, event_sequence)` and applied at most once. The per-order row carries `last_observed_event_sequence`; an incoming event with `sequence <= last_observed_event_sequence` is logged and dropped. Stream, cycle, and startup all funnel through the same `apply_order_event(...)` API.

The per-order table makes this natural because a position can have multiple distinct orders (each with its own sequence). A position-level row cannot represent "stream-side primary fill has been applied but cycle-side residual fill has not."

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
    order_id                      TEXT,                -- broker order id (NULL until submitted)
    client_order_id               TEXT    NOT NULL,
    order_type                    TEXT    NOT NULL,    -- market | limit | stop | stop_limit
    side                          TEXT    NOT NULL,    -- buy | sell
    intended_qty                  REAL    NOT NULL,
    intended_stop_price           REAL,                -- for stop / stop_limit / oto stop child
    intended_trigger_price        REAL,                -- for stop_limit
    intended_limit_price          REAL,                -- for limit / capped market / stop_limit
    slippage_benchmark_price      REAL,                -- canonical Phase 1 naming
    slippage_benchmark_kind       TEXT,                -- arrival_midpoint | fallback_latest_close
                                                       -- | unavailable
    slippage_benchmark_timestamp  TEXT,
    slippage_measurement_quality  TEXT,                -- primary | fallback | recovered | unavailable
    status                        TEXT    NOT NULL,    -- per-order state machine, see §6.3
    filled_qty                    REAL    NOT NULL DEFAULT 0.0,
    avg_fill_price                REAL,
    submitted_at                  TEXT    NOT NULL,
    last_observed_at              TEXT    NOT NULL,
    last_observed_event_sequence  INTEGER NOT NULL DEFAULT 0,   -- idempotency key
    terminal_at                   TEXT,
    FOREIGN KEY(position_uid) REFERENCES position_lifecycle(position_uid)
);

CREATE INDEX idx_lifecycle_orders_position_uid ON position_lifecycle_orders(position_uid);
CREATE INDEX idx_lifecycle_orders_order_id     ON position_lifecycle_orders(order_id);
CREATE INDEX idx_lifecycle_orders_status       ON position_lifecycle_orders(status);
```

`position_lifecycle` keeps its position-level fields (status, current_qty, avg_entry_price, net_realized_pnl) as a rollup — derived from the child rows. The position-level `entry_order_id` column becomes legacy and is left in place as a non-authoritative mirror of `entry_primary`'s order_id for backward compatibility.

### 6.3 Per-order state machine

```
pending           — row inserted, broker submit in progress
working           — broker has accepted; order is alive (live working, resting STOP_LIMIT, etc.)
partially_filled  — filled_qty > 0 < intended_qty
filled            — filled_qty == intended_qty (terminal)
canceled          — terminal with zero fills (§8.1 enforced here, too)
rejected          — terminal pre-fill broker rejection
unknown           — submitted but no confirmation; awaits reconciliation
```

`pending → working → partially_filled → filled` is the happy path. `pending` exists only as a brief moment between row insert and broker submit return; in practice many transitions skip straight to `working` or `unknown`.

§8.1 invariant applies per-order: a `partially_filled` order cannot transition to `canceled`; the broker-side cancel of a partially-filled order leaves the order `partially_filled` at its filled quantity.

### 6.4 Reconciliation paths under the new shape

All three paths share the same `apply_order_event(position_uid, order_id, event)` API:

- **Stream** delivers events as they arrive. Each event has an `event_sequence` (monotonic per order from the broker).
- **Cycle** iterates non-terminal per-order rows, calls broker `get_order_by_id(order_id)`, translates the broker view into an `event`, calls `apply_order_event`.
- **Startup** does the same as cycle, but also walks broker closed-order history for `order_id`s belonging to non-terminal per-order rows that aren't in `open_orders`.

`apply_order_event` is the single chokepoint that enforces:

- exactly-once via `event_sequence`
- §8.1 invariant
- position-level rollup updates (`position_lifecycle.current_qty`, etc.)
- trade-log row creation (with the same `event_sequence` echoed in a new `trades.order_event_sequence` column to make duplicates detectable post-hoc)

### 6.5 What this eliminates

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
- **`event_sequence`** (proposed §6.4) — monotonic per-order counter sourced from the broker that lets stream / cycle / startup paths apply at most one update per observed event.
