# Order Lifecycle State Machine — Discovery

Status: Discovery doc, no code changes yet.
Purpose: Document the current state of `position_lifecycle` in code, surface the holes that PR #58 (Donchian stop-limit + hybrid residual) made visible, and establish a shared picture before the foundation PR begins.

This doc is intentionally descriptive, not prescriptive. The foundation PR scope is set by the operator's directive; this doc only verifies and grounds the assumptions behind it.

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
    entry_order_id          TEXT,                    -- Alpaca order id (populated after submit returns)
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

### What the schema does NOT carry

Order-intent fields needed to honestly reconstruct a non-terminal row after a process restart or a missed callback are absent:

| Field | Why we need it | Current home |
|---|---|---|
| `entry_stop_price` | ATR-sized protective stop attached to the entry. After downtime we cannot rebuild the same stop without it. | Lives only on the in-memory `RiskDecision`, captured into `data/trades.db.trades.initial_stop_loss` AFTER the fill row writes. Pending row has no copy. |
| `entry_take_profit_price` | Bracket OTO take-profit (none today, but design-level gap). | Nowhere. |
| `entry_trigger_price` | STOP_LIMIT stop_price for resting orders (rebuilt PR #58 needs it). | Nowhere. RiskDecision has it, but lifecycle row drops it. |
| `entry_max_price` | Capped market entries (PLAN 11.32) and STOP_LIMIT limit_price. | Nowhere. RiskDecision has it. |
| `modeled_arrival_price` | Slippage benchmark captured at submission. After Phase 1 of slippage unification this lives on the trade row but only after fill. Pending row has no copy. | Captured in [engine/trader.py:1541-1559](../engine/trader.py:1541) as `slippage_ref` + `slippage_kind`, then thrown away unless the order fills synchronously. |
| `modeled_arrival_kind` | `arrival_midpoint` / `fallback_latest_close` / `unavailable` — slippage taxonomy provenance. | Same as above. The `_suspect_orders` cache holds it in memory (`modeled_price_kind`), but only for the brief window between submission and confirmation. Restart wipes it. |

The downstream consequence: when `_recover_suspect_orders` adopts a recovered fill, the slippage kind is recoverable only because `_suspect_orders` carries it. When the lifecycle row instead drives recovery (e.g. after restart), the kind is lost and recovery rows fall back to `unavailable` — provable in the trade-log audit but a real information loss.

---

## 2. Transitions wired today vs. not wired

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
| [execution/broker.py:1027,1039,1205,1215](../execution/broker.py:1027) `_lifecycle_mark_canceled` | `mark_canceled` | broker-rejected submissions, halted entries |
| [engine/trader.py:2467](../engine/trader.py:2467) `_close_lifecycle_for_owner_key` | `mark_closed` | every in-process exit (signal, stop fill, etc.) via `_record_realized_pnl` |
| [engine/trader.py:5927](../engine/trader.py:5927) reverse reconcile | `mark_closed(external=True)` | each cycle: any open row whose owner_key is gone from broker |
| [engine/trader.py:5874](../engine/trader.py:5874) `_reconcile_position_lifecycle` | `synthesize_for_existing` | each cycle: broker-open positions with no open lifecycle row |
| [engine/trader.py:3823](../engine/trader.py:3823) recovered entry context | `synthesize_for_existing` | startup repair when an unowned broker position is reconstructed |
| [engine/trader.py:2424](../engine/trader.py:2424) reduce path | `mark_residual` | partial close of `open`/`partially_filled` row |

### 2.2 Which transitions are NOT wired

These are the holes — places where a state change in broker reality is not reflected in the lifecycle row:

1. **Stream-driven entry fill.** [execution/stream.py](../execution/stream.py) does not import the lifecycle store. The only stream-touching lifecycle write happens *indirectly* via `_process_stream_stop_fills` → `_record_realized_pnl` → `_close_lifecycle_for_owner_key` (close path). A stream-delivered ENTRY fill (i.e. a resting LIMIT or STOP_LIMIT that fills asynchronously) has no path to `mark_open`. Today this is mostly cosmetic because synchronous `_wait_for_fill` blocks during the 240s confirmation window and catches the fill via stream-or-REST; but for any order that returns `TIMEOUT` (resting longer than 240s) the lifecycle never advances on a later async fill.
2. **Cycle-level reconciliation of non-terminal rows against broker.** [`_reconcile_position_lifecycle`](../engine/trader.py:5800) runs once per cycle but only:
   - synthesizes rows for broker positions that lack one (forward pass), and
   - marks `external_closed` rows whose owner_key vanished (reverse pass).
   It does NOT query the broker for each non-terminal row's `entry_order_id` and advance pending → open / partially_filled / canceled accordingly. A `pending` row whose order resolved while the bot was running but the synchronous confirmation missed it (TIMEOUT, UNKNOWN, dropped stream update) stays `pending` until either the reverse-pass grace expires (then it's mass-marked `external_closed` — usually wrong) or the bot restarts and the suspect-order cache happens to repair it (only for UNKNOWN, and only if the cache survives — see §4).
3. **Startup downtime fill discovery.** [`_reconcile_position_lifecycle`](../engine/trader.py:5800) at startup catches a broker-open position with no row (forward pass synthesizes) but does NOT scan broker closed-order history for fills/cancels that happened during downtime and would resolve a still-pending row. The only ad-hoc downtime probe is the suspect-order restore from `startup_snapshot.open_orders` (PR #58 added this; see §5), which by definition skips orders that already filled or were canceled during downtime.
4. **Stream-driven cancellation.** A broker-side cancel delivered via the stream while the bot is running has no lifecycle hook. `_wait_for_fill` returns `CANCELED` for synchronous-window cancels and `_lifecycle_mark_filled` handles those. Cancels delivered after the 240s window go nowhere.
5. **Partial fill on a resting order.** Same shape as (1) — a STOP_LIMIT that fills 5 of 10 over multiple updates and then sits would need stream→`mark_partially_filled` wiring. Today the row stays `pending` after each partial.

### 2.3 The §8.1 invariant — verified

The proposal's invariant (any row with `current_qty > 0` cannot transition to `canceled`) IS enforced at the store boundary at [engine/lifecycle.py:486-501](../engine/lifecycle.py:486):

```python
if first_fill_at is not None or (current_qty or 0.0) > 0.0:
    raise ValueError("refusing to mark ... canceled — it has fills ...")
```

`_lifecycle_mark_filled` at [execution/broker.py:399-412](../execution/broker.py:399) also defends in depth: a CANCELED/REJECTED outcome with `filled_qty > 0` routes to `mark_partially_filled` rather than `mark_canceled`.

Foundation PR can rely on this invariant being honored at the API boundary. It must continue to be honored by any new transition wiring.

---

## 3. Current reconciliation paths

### 3.1 `_reconcile_position_lifecycle` — cycle-level

Site: [engine/trader.py:5800](../engine/trader.py:5800). Two passes per cycle:

**Forward (backfill).** For each broker-open position whose `owner_key` has no open lifecycle row, call `synthesize_for_existing` to insert an `open` row tagged `metadata.synthesized=true`. Skips OCC legs belonging to managed spreads. Skips single-leg OCC unless `_positions` already tracks it. Strategy is read from `_positions` if present, else `"unknown"`.

What this handles:
- broker positions that pre-date the lifecycle code shipping
- positions inherited across a restart whose original `pending` row never advanced
- options trailing stops needing a durable `position_uid` to recreate DAY stops

What this does NOT handle:
- *advancing* an existing pending row to open. If a pending row exists for `owner_key=AAPL` and a broker position appears for AAPL, the forward pass sees `existing is not None` and continues — never transitioning the pending row to open. The order is fine if the broker fill went through `_lifecycle_mark_filled` synchronously; broken if it didn't.
- distinguishing primary fill from residual fill on a hybrid entry (PR #58's split-entry shape). Forward pass treats any broker position for that owner_key as the "synthesized open" state.

**Reverse (close-reconcile).** For each non-terminal lifecycle row whose `owner_key` is no longer in broker positions, call `mark_closed(external=True)`. Skips spread rows. Skips `pending` rows younger than `LIFECYCLE_PENDING_GRACE_SECONDS` ([settings.py](../config/settings.py)) — added in PR-2 to avoid mass-closing legitimate in-flight entries after a startup race.

What this handles:
- overnight stop fills that the bot didn't witness
- manual broker-side closes
- broker-side cancels of `pending` rows older than the grace window

What this does NOT handle:
- pending rows mass-flipped to `external_closed` when the right answer is `canceled` (zero fill) — true to broker reality but loses the §8.1 distinction
- pending rows for orders that legitimately worked >grace_seconds before filling (rare with current 240s confirmation, but PR #58's resting STOP_LIMIT scenario can sit for days)

### 3.2 `_recover_suspect_orders` — cycle-level, narrow

Site: [engine/trader.py:1844](../engine/trader.py:1844). Walks `self._suspect_orders` (in-memory dict keyed by symbol), calls `broker.reconcile_submitted_order(order_id, symbol, requested_qty)`, and routes by the returned `OrderResult.status`:

| Returned status | Action |
|---|---|
| PENDING / ACCEPTED | log, wait next cycle |
| CANCELED / REJECTED / TIMEOUT | drop the suspect record (no row update) |
| FILLED / PARTIAL + broker position present | record fill, log entry row with `quality='recovered'`, register ownership, ensure protective stop, drop |
| FILLED / PARTIAL + no broker position | warn and drop |

This is the *only* recovery hook for entries whose synchronous confirmation failed. It is in-memory; the cache does not survive restart. Today the cache only ever holds an entry from `_remember_suspect_order` called from the `UNKNOWN` branch of `_process_symbol` (entry submission). TIMEOUT does not go into the cache — by design today, because TIMEOUT historically meant "give up" rather than "submitted-and-working."

### 3.3 `_lifecycle_mark_filled` — synchronous fill path

Site: [execution/broker.py:355](../execution/broker.py:355). Called immediately after `_wait_for_fill` returns terminal:

- FILLED + qty>0 → `mark_open(avg, qty)`
- PARTIAL + qty>0 → `mark_partially_filled(avg, qty)`
- CANCELED / REJECTED + qty>0 → `mark_partially_filled` (§8.1)
- CANCELED / REJECTED + qty=0 → `mark_canceled`
- TIMEOUT / UNKNOWN → leave the row pending; rely on later reconciliation

This works correctly for the SMA / RSI / current Donchian (market entry) flow because their orders fill or terminate inside the 240s window. It does NOT advance the lifecycle for orders that sit beyond the window and fill later.

### 3.4 `_close_lifecycle_for_owner_key` — exit path

Site: [engine/trader.py:2437](../engine/trader.py:2437). Called by every exit code-path through `_record_realized_pnl`. Looks up the single open row for `owner_key` and calls `mark_closed`. Single-leg only — spread/options closes are deferred to Phase C.

This catches every in-process exit, including the stop-fill stream path (because `_process_stream_stop_fills` calls `_record_realized_pnl`).

---

## 4. The suspect-order caches and why they exist

### 4.1 `_suspect_orders` — entry recovery

[engine/trader.py:423](../engine/trader.py:423): `dict[str, SuspectOrder]`, keyed by `decision.symbol`.

Populated by [`_remember_suspect_order`](../engine/trader.py:1791) when `place_order` returns `OrderStatus.UNKNOWN` — meaning Alpaca acknowledged the order_id but the bot couldn't confirm terminal status before the synchronous confirmation timeout expired. Drained by `_recover_suspect_orders` each cycle.

Why this cache exists separately from the lifecycle row:
- The lifecycle row exists (it was created in `_lifecycle_begin` before submit), but the row holds neither the modeled-price benchmark nor the slippage-kind tag needed to write a faithful trade-log entry on recovery.
- `RiskDecision` itself is not persisted anywhere durable. The cache holds it so `_recover_suspect_orders` can call `_log_entry` with the same shape a synchronous fill would have used.
- The cache also serves as the explicit allow-list: only orders the bot *intentionally* submitted are reconciled this way. Orders the bot didn't submit (orphans, manual broker positions) go through `_reconcile_position_lifecycle.synthesize_for_existing` instead.

Why the symbol-key collides on PR #58's hybrid:
- The hybrid Donchian split submits a primary STOP_LIMIT (whole shares) and a residual MARKET (fractional remainder) for the same symbol. Two separate order_ids, two separate `SuspectOrder` entries needed — but the dict key is `decision.symbol`, so the second write overwrites the first. One submitted order's recovery handle is always lost.

Why the cache is memory-only:
- A bot restart loses every entry. Any UNKNOWN-after-submit window that crosses a restart drops the recovery handle. Today (market entry strategies) this is rare because the 240s window is short. PR #58's resting orders make it routine.

### 4.2 `_suspect_residual_orders` — PR #58-specific, now parked

PR #58 added a parallel cache `self._suspect_residual_orders` to handle the fact that the residual MARKET submission also lands in TIMEOUT/UNKNOWN territory and the primary cache slot is owned by the STOP_LIMIT primary. Same shape, same memory-only durability. The PR #58 R7 review's [P1] "Ambiguous residual recovery is still lost across restart" finding identifies this as fundamentally insufficient: the residual order has no broker handle in `startup_snapshot.open_orders` if it already filled, and no in-memory handle if the process restarted.

This cache exists because `position_lifecycle` doesn't have a place to hold a second order's metadata under a single position. The foundation PR will eliminate it.

---

## 5. Explicit list of holes PR #58 surfaced

This is the consolidated list from the 8 review rounds. The pattern is consistent: each hole traces back to one or more of (a) lifecycle row is missing order-intent metadata, (b) recovery state is in-memory not durable, (c) recovery code only handles synchronous outcomes.

### 5.1 TIMEOUT → no recovery handle

`_poll_until_terminal` returns `TIMEOUT` for an order still working after 240s. The current code treats TIMEOUT as terminal in `_lifecycle_mark_filled` (leaves row pending) but never stages it for recovery in `_suspect_orders`. A resting STOP_LIMIT primary therefore has no ownership in `_positions`, no fill callback, and no path to advance its lifecycle when it eventually fills.

[Review reference: R5 P1 "A working primary returned as TIMEOUT is never owned or recovered."]

### 5.2 `_suspect_orders` keyed by symbol — hybrid collides

Confirmed in §4.1. Single-symbol slot cannot hold both primary and residual handles for the hybrid split.

[Review reference: R6 P1 "One symbol-keyed suspect slot cannot recover both hybrid orders."]

### 5.3 Drain reads the wrong qty field

PR #58's drain logic stored `primary_qty = float(row.current_qty)` but `current_qty` is 0.0 on a freshly-created pending row. The intended primary qty lives in `entry_qty`. The bug let a residual-only broker position be treated as the completed aggregate state because the guard threshold was zero. The store contract is clear; the regression is in the caller treating `current_qty` as intent.

This isn't really an "audit reveals" hole — it's a symptom of `entry_qty` and `current_qty` semantics being subtle. Foundation PR's state machine + tests should make this impossible to get wrong.

[Review reference: R6 P1 "The new drain guard reads the wrong lifecycle quantity."]

### 5.4 Pending drain over-eagerly closes on residual-only broker state

If the residual fills before the primary, the next snapshot shows broker position == residual qty (e.g. 0.88 shares). The drain logic marked the lifecycle `open` at 0.88 and discarded the cache. Later primary fills had no callback to update `current_qty` upward. Caused by treating *any* broker position as the completed aggregate.

[Review reference: R5 P1 "The pending-lifecycle drain consumes the cache on the residual-only position."]

### 5.5 Startup reconstruction only walks open orders

PR #58 added `_restore_suspect_orders_from_broker` that walks `startup_snapshot.open_orders`. By definition this misses orders that already filled or canceled during downtime. The pending lifecycle row has no `entry_order_id` lookup path. The generic missing-entry-context recovery cannot synthesize a STOP_LIMIT `RiskDecision` because it has no `entry_trigger_price` or `entry_max_price` — fails `RiskDecision.__post_init__` validation.

[Review reference: R7 P1 "A primary that fills while the process is down still bypasses exact-order recovery."]

### 5.6 Ambiguous residual recovery lost across restart

§4.2 above. Memory-only cache + no broker handle for already-filled fractional MARKET = no recovery path.

[Review reference: R7 P1 "Ambiguous residual recovery is still lost across restart."]

### 5.7 Two `fallback_reference_price` slippage tags fabricated

PR #58 in two places writes `modeled_arrival_kind='fallback_reference_price'` where no real benchmark was captured at submission. This is a new fabricated kind invented inside PR #58, not part of the slippage taxonomy. Foundation PR's rule: recovery rows preserve the original `modeled_price_kind` from `_suspect_orders` OR mark `unavailable`. Never fabricate.

(Not surfaced in the review summaries above but flagged in the operator's directive — included here for completeness.)

---

## 6. Observations that should inform foundation PR scope

These are facts the discovery surfaced. They are not design decisions — the design lives in the foundation PR's plan.

1. **Stream wiring to lifecycle does not exist for entries.** Adding it is necessary for any strategy that uses non-trivially-async orders (resting limits, STOP_LIMIT, options).
2. **Cycle-level reconciliation of pending rows by `entry_order_id` does not exist.** This is the workhorse reconciliation; missing it forces caches like `_suspect_orders` to substitute.
3. **The pending row is missing every order-intent field except qty and the two order IDs.** Without `entry_stop_price` / `entry_trigger_price` / `entry_max_price`, restart recovery cannot reconstruct what the order was supposed to be.
4. **`modeled_arrival_price` / `modeled_arrival_kind` lives only on `_suspect_orders`.** Restart loses it. The honest answer is to persist it on the lifecycle row at submission time — the same moment slippage benchmark is captured ([engine/trader.py:1541-1559](../engine/trader.py:1541)).
5. **Two separate caches (`_suspect_orders`, `_suspect_residual_orders`) exist because the lifecycle row holds at most one order's metadata per position.** A position can have multiple order_ids associated with it through its life (entry, residual, replacement stop, partial exit). The pending row carries only the entry; everything else is shadow-state.
6. **The §8.1 invariant IS enforced at the store boundary today.** Foundation PR must preserve this — both the store guard and the `_lifecycle_mark_filled` defense in depth.
7. **Synthesized rows are tagged `metadata.synthesized=true`.** Useful for downstream consumers (operator CLI distinguishes "bot-known lineage" from "rebuilt from broker state"). Foundation PR should keep the convention and apply it to any new synthesis path.

---

## 7. Glossary of terms used in this doc

- **Lifecycle row** — one row in `position_lifecycle`. The durable record of one position's identity from creation to terminal status.
- **Pending row** — lifecycle row with `status='pending'`. Created before broker submit; awaiting fill confirmation.
- **owner_key** — `engine.positions.owner_key_for(symbol)`. Equity = ticker, options = underlying ticker, spread = per-instance UUID. Used as the broker-aggregation key.
- **Recovery row** — a trade-log row written for a fill that was not observed synchronously. Currently tagged `measurement_quality='recovered'`.
- **Suspect order** — an order that left the bot but whose terminal state the bot didn't confirm before the synchronous timeout. Held in `_suspect_orders` (entry) or `_suspect_residual_orders` (hybrid residual).
- **Synthesized row** — a lifecycle row created retroactively to back a broker-open position the bot didn't originate. Tagged `metadata.synthesized=true`.
- **Drain pass** — PR #58's term for the bookkeeping that decides when a hybrid pending lifecycle row should be advanced once the broker shows a position.
- **Hybrid entry / residual** — PR #58's split-entry: whole shares as a STOP_LIMIT primary, fractional remainder as a MARKET residual. Two orders, one logical position.
