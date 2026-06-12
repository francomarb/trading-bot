# Order Lifecycle Foundation — Implementation Tracker

Companion to [`docs/order_lifecycle_state_machine.md`](order_lifecycle_state_machine.md) (the discovery doc, merged in PR #59).
Tracks implementation progress for the foundation PR — schema + `apply_order_event` + reconciliation paths + compensating-patch absorption.

Status legend: ✅ done · 🔄 in progress · ⬜ not started · ⏸ blocked

---

## Phase 1 — Foundation PR scope

The foundation PR implements the discovery doc's §6 (schema + atomic event API + rollups), §10 (compensating-patch absorption), and §12 (tests + migration prerequisites). Strategy-side strict, write-side substrate only. Consumer migration and PR #58 rebuild are explicitly deferred.

| Phase | Scope | Branch | PR | Status |
|---|---|---|---|---|
| 1 | Schema + `apply_order_event` + reconciliation paths + cache removal + 26-test matrix | `feat/order-lifecycle-foundation-impl` | — | 🔄 In progress |

---

## Commit checklist

Each commit is reviewable in isolation; each ends with green tests.

| # | Commit | Doc section | LOC est. | Tests | Status |
|---|---|---|---|---|---|
| 0 | Implementation tracker + PLAN.md pointer | n/a | ~150 | — | 🔄 In progress |
| 1 | Schema: `position_lifecycle_orders` table + indexes + position-level UNIQUE + PRAGMA foreign_keys | §6.2 / R13-G1 | ~400 | 17 | 🔄 In progress |
| 2 | Migration preflight: duplicate detection + abort-startup on conflict | §12.2 | ~250 | 12 | 🔄 In progress |
| 3 | `PositionLifecycleOrdersStore` — CRUD operations on per-order rows | §6.2 / §6.3 | ~450 | 20 | 🔄 In progress |
| 4 | `apply_order_event` — atomic compare-and-set + trades UPSERT + rollup + status | §6.4 / §6.5 / §6.6 / §6.6.1 | ~500 | 10 | ⬜ |
| 5 | Trades schema: `execution_id` column + UPSERT semantics + partial UNIQUE index | §6.5 / R5 fixes | ~200 | 4 | ⬜ |
| 6 | Wire WebSocket stream → `apply_order_event` | §6.4 / §10.1 | ~300 | 4 | ⬜ |
| 7 | Wire cycle reconciliation (`_reconcile_position_lifecycle`) → `apply_order_event` | §6.4 / §10.1 / §3.1 | ~300 | 4 | ⬜ |
| 8 | Wire startup reconciliation: downtime fill/cancel walk against closed-order history | §6.4 / §10.1 | ~250 | 3 | ⬜ |
| 9 | Wire `protective_stop` role: broker OTO child gets its own per-order row | §10.3 | ~150 | 2 | ⬜ |
| 10 | Wire `replacement_stop` role: PR #47 GTC promotion uses durable identity | §10.3 | ~150 | 2 | ⬜ |
| 11 | Remove `_suspect_orders` cache (post-verification) | §10.1 / §6.7 | ~200 | 2 | ⬜ |
| 12 | Remove `_suspect_exit_orders` cache (post-verification) | §10.2 / §6.7 | ~150 | 2 | ⬜ |
| 13 | Doc updates: PLAN.md, operator_controls_proposal.md, slippage_unification_tracker.md | §12 | ~50 | — | ⬜ |

Total estimate: ~3000 LOC code + ~1500 LOC tests, ~14 commits.

---

## §10 Compensating-patch absorption matrix progress

Each row from the discovery doc's §10 maps to one or more commits above.

| § | Category | Replaces | Commits | Status |
|---|---|---|---|---|
| 10.1 | Entry uncertainty / duplicate prevention / pending grace | `_suspect_orders`, broker-open duplicate checks, `LIFECYCLE_PENDING_GRACE_SECONDS` | 6, 7, 8, 11 | ⬜ |
| 10.2 | Uncertain single-leg exits | `_suspect_exit_orders` | 6, 7, 12 | ⬜ |
| 10.3 | Protective stop promotion / replacement / repair | `_reported_stop_promotion_failures` identity workaround | 9, 10 | ⬜ |
| 10.4 | Option trailing state split | `option_trailing_stops.alpaca_stop_order_id` denormalization | (separate follow-up) | ⬜ |
| 10.5 | Slippage recovery — preserve provenance | `SuspectOrder.modeled_price_kind` moves to per-order row | 4, 6, 7 | ⬜ |
| 10.6 | Position-level partial-close accounting | (acceptance tests; carries forward unchanged) | 4 (rollup) | ⬜ |
| 10.7 | MLEG partial-close `_spreads_pending_close` | (single-leg side solved; MLEG side deferred) | (deferred to spread lifecycle PR) | ⬜ |
| 10.8 | PR #58 disposition | (rebuild, do not cherry-pick) | (deferred until foundation merges) | ⬜ |

---

## §12.1 Regression test matrix progress

All 26 tests from the discovery doc must land in the implementation PR. Each test row identifies the regression it guards against.

| # | Test (abbreviated) | Doc anchor | Commit | Status |
|---|---|---|---|---|
| 1 | Atomic apply_order_event — two unrelated order_ids, only matching updates | §6.4 R3-P0 | 4 | ⬜ |
| 2 | Terminal-state immutability | §6.4 R3-P1a | 4 | ⬜ |
| 3 | Side-signed rollup correctness | §6.6 R3-P1b | 4 | ⬜ |
| 4 | MLEG two-row insert succeeds | §6.5 R5-C1 | 5 | ⬜ |
| 5 | UPSERT + partial-unique-index pair | §6.5 R5-C2 | 5 | ⬜ |
| 6 | All-or-nothing transaction on failure | §6.4 R4-P1b | 4 | ⬜ |
| 7 | execution_id NULL on REST recovery | §6.5 R4-P1a | 8 | ⬜ |
| 8 | `_suspect_orders` removal preserves TIMEOUT/UNKNOWN recovery | §10.1 | 11 | ⬜ |
| 9 | `_suspect_exit_orders` removal preserves invariants | §10.2 | 12 | ⬜ |
| 10 | `replacement_stop` atomic-replace | §10.3 / §6.4 | 10 | ⬜ |
| 11 | Zero-fill working entry stays `pending` | §6.6.1 R7-P0 | 4 | ⬜ |
| 12 | Working sell-side order blocks `closed` (R12 supersedes R8-1) | §6.6.1 R12-P1 | 4 | ⬜ |
| 13 | `closed_at` set only on `closed` / `external_closed` | §6.6.1 R8-P2 | 4 | ⬜ |
| 14 | `closed_at` reads new status via CTE | §6.6.1 R9-P1a | 4 | ⬜ |
| 15 | Negative `current_qty` → `error` | §6.6.1 R9-P1b | 4 | ⬜ |
| 16 | `'error'` retains owner_key lock | §6.2 R8-3 | 1 | ⬜ |
| 17 | Reverse-pass skips `'error'` rows | §3.1 R8-4 | 7 | ⬜ |
| 18 | Dedupe script detection-only default | §12.2 R8-2 | 2 | ⬜ |
| 19 | Migration preflight covers `'error'` status | §12.2 R9-P1c | 2 | ⬜ |
| 20 | Working sell-side order blocks `closed` AND lock retains | §6.6.1 R12-P1 | 4 | ⬜ |
| 21 | Oversold position → `error` immediately | §6.6.1 R9-P1b + R11 | 4 | ⬜ |
| 22 | Broker-snapshot guard defense-in-depth | §10.1 R10-P1b | 6 | ⬜ |
| 23 | Direct `pending → filled` via fast path | §6.3 / §6.4 R11-P1 | 4 | ⬜ |
| 24 | Direct `pending → canceled` via recovery | §6.3 / §6.4 R12 | 4 | ⬜ |
| 25 | `PRAGMA foreign_keys = ON;` enforces FKs | §6.2 R13-G1 | 1 | ⬜ |
| 26 | `net_realized_pnl` rollup from `trades` (not orders table) | §6.6 R13-G2 | 4 | ⬜ |

---

## Migration prerequisites (§12.2)

Per discovery doc §12.2, the implementation PR must include a duplicate-row preflight that runs BEFORE `CREATE UNIQUE INDEX` on every startup. If duplicates exist:

1. Log `ERROR`-level message listing affected `order_id` / `owner_key` values
2. Surface through alert backend
3. `_ensure_db()` raises; bot exits non-zero
4. Operator runs `scripts/migrate_dedupe_trades.py` offline (detection / review / apply modes)
5. Next `recycle_bot.sh` applies the unique indexes cleanly

Implementation PR includes:
- [ ] Pre-flight detection queries (commit 2)
- [ ] `_ensure_db()` raises on duplicates (commit 2)
- [ ] `scripts/migrate_dedupe_trades.py` with three modes (commit 2)
- [ ] PLAN.md operator-runbook entry for the script (commit 13)

---

## Documentation updates (§12 / §11)

The foundation PR must update (per discovery doc §12):

- [ ] `PLAN.md` — Live readiness gate row for slippage / operator controls; foundation PR in flight or merged; PR #58 awaiting rebuild
- [ ] `docs/operator_controls_proposal.md` §17 amended for write-side / read-side substrate split + per-order table substrate Phase C writes into
- [ ] `docs/slippage_unification_tracker.md` — Phase 2 scope references foundation-provided pre-fill provenance
- [ ] PR #58 description — (already done — converted to draft with blocked/cherry-pickable list)

---

## Smoke check protocol (post-merge)

Foundation PR merges to main. Bot recycles on main with the new code. Smoke check verifies before declaring foundation green:

- [ ] Bot recycles cleanly (migration runs idempotently)
- [ ] Pre-flight duplicate check passes on production DB (or operator runs dedupe script first)
- [ ] First post-merge entry creates a `position_lifecycle_orders` row at `pending` → `working` / `filled`
- [ ] First post-merge exit creates a `role='exit'` row; position transitions to `closed` after exit terminates
- [ ] First post-merge stop fill advances the existing `protective_stop` row to `filled`
- [ ] `_suspect_orders` cache is empty (or absent) throughout
- [ ] Parity check: per-order rollups (`current_qty`, `avg_entry_price`, `net_realized_pnl`) match the legacy values on `position_lifecycle`

Once smoke passes, PR #58 rebuild can start.

---

## Rollback plan

`git revert <foundation-merge-sha> && ./recycle_bot.sh`. The new tables remain in `data/trades.db` but no code writes them post-revert. Pre-existing rows are untouched. The `_suspect_orders` / `_suspect_exit_orders` caches are restored by the revert.

Idempotent — safe to apply and revert any number of times during smoke validation.

---

## Open decisions

| Question | Decision | Date |
|---|---|---|
| Branch name | `feat/order-lifecycle-foundation-impl` (discovery doc was on `feat/order-lifecycle-foundation`) | 2026-06-12 |
| `option_trailing_stops` migration scope | Soft (FK reference column added; existing columns kept as denormalized mirrors during migration) | discovery doc §10.4 |
| `apply_order_event` API location | New module `engine/lifecycle_orders.py` to keep separation from `engine/lifecycle.py` (position-level store) | (TBD during commit 3) |
| Suspect-cache removal sequence | After verification of cycle / stream / startup paths, NOT day-one of merge | discovery doc §10.9 step 3 |
