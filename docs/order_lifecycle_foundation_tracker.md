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

## Commit log (time-ordered, as merged on the branch)

Two phases on `feat/order-lifecycle-foundation-impl`:

  - **A. Substrate landed** (commits 0-6): schema, store, atomic
    `apply_order_event`, trades-row writer alignment, and submit-time
    insert. No live behavior change; substrate populated but not yet
    read by consumers.
  - **B. PR #60 review-fix series** (commits 7-14): three rounds of
    ChatGPT review against (A), each landed as 2–3 focused fix
    commits. End-state ahead of the consumer-wiring phase.

| Git # | Phase | Commit | Doc anchor | Tests | Status |
|---|---|---|---|---|---|
| 0 | A | Tracker + PLAN.md pointer | n/a | — | ✅ |
| 1 | A | Schema: `position_lifecycle_orders` + indexes + position-level UNIQUE + `PRAGMA foreign_keys=ON` | §6.2 / R13-G1 | 17 | ✅ |
| 2 | A | Migration preflight: `position_lifecycle.owner_key` duplicates + abort-startup | §12.2 | 12 | ✅ |
| 3 | A | `PositionLifecycleOrdersStore` CRUD | §6.2 / §6.3 | 20 | ✅ |
| 4 | A | `apply_order_event` — atomic CAS + trades UPSERT + rollup + status CTE; `execution_id` column | §6.4 / §6.5 / §6.6 / §6.6.1 | 16 | ✅ |
| 5 | A | Trades partial UNIQUE + `TradeLogger.log` UPSERT semantics | §6.5 / R5 | 1 + fixture updates | ✅ |
| 6 | A | Submit-time substrate insert (equity OTO / fractional / options) + attach order_id on submit return | §6.3 / §10.1 | 6 | ✅ |
| 7 | B-r1 | Trades-side migration preflight + `scripts/migrate_dedupe_trades.py` (detect / review / apply); plain UPDATE backfill (PR #60 review round 1, fix A) | §12.2 / R8-2 | 18 | ✅ |
| 8 | B-r1 | Status-only events skip trades UPSERT; COALESCE-preserve provenance / audit (PR #60 round 1, fixes B + F) | §6.5 / §6.6 | 9 | ✅ |
| 9 | B-r1 | Options durable identity (`on_submitted`); fail-closed substrate policy; persist slippage provenance (PR #60 round 1, fixes C + D + E) | §10.5 / §10.3 | 12 | ✅ |
| 10 | B-r2 | Dedupe review scopes to detected rows; BACKFILL respects explicit `position_type`; apply hardening — snapshot fingerprint + rowcount + FKs ON + post-apply rescan (PR #60 round 2, fixes 1 + 5 + 7) | §12.2 | 7 | ✅ |
| 11 | B-r2 | Expand COALESCE set to risk anchors + entry/exit timestamps + `modeled_slippage_bps`; `position_uid` identity-conflict refusal (PR #60 round 2, fix 6) | §6.5 / §6.6 | 8 | ✅ |
| 12 | B-r2 | Queue `on_submitted` to engine thread (thread-safety); roll back pending position lifecycle on substrate-failure re-raise; broaden fail-closed to all exceptions when store configured (PR #60 round 2, fixes 2 + 3 + 4) | §6.4 / §10.3 | 5 | ✅ |
| 13 | B-r3 | Dedupe partition validation; reject clusters requiring MERGE (accounting conflict); BACKFILL widening to single_leg-with-null-position_id (PR #60 round 3, fixes 1 + 3 + 4) | §12.2 | 7 | ✅ |
| 14 | B-r3 | `_UPSERT_LATEST_NON_NULL_COLUMNS` bucket for computed accounting (`realized_pnl`, `r_multiple`, slippage_*) (PR #60 round 3, fix 2) | §6.5 / §6.6 | 5 | ✅ |
| 15 | B-r3 | This tracker restructure + numbering normalization (PR #60 round 3, fix 5) | n/a | — | 🔄 In progress |

Substrate-landed totals: ~5500 LOC code + ~3200 LOC tests across 16 commits.

---

## Planned (next phase — not yet started)

Consumer wiring + cache removal. These were originally listed as commits 7–13 in the very first draft of this tracker; the PR #60 review series consumed those numbers and shifted them out. They will land as a sequence of commits AFTER PR #60 merges, on a follow-up branch.

| Step | Scope | Doc anchor | LOC est. | Tests | Status |
|---|---|---|---|---|---|
| P-1 | Wire WebSocket stream → `apply_order_event` (drains via the lifecycle-attach queue introduced in commit 12) | §6.4 / §10.1 | ~300 | 4 | ⬜ |
| P-2 | Wire cycle reconciliation (`_reconcile_position_lifecycle`) → `apply_order_event` | §6.4 / §10.1 / §3.1 | ~300 | 4 | ⬜ |
| P-3 | Wire startup reconciliation: downtime fill/cancel walk against closed-order history | §6.4 / §10.1 | ~250 | 3 | ⬜ |
| P-4 | Wire `protective_stop` role: broker OTO child gets its own per-order row | §10.3 | ~150 | 2 | ⬜ |
| P-5 | Wire `replacement_stop` role: PR #47 GTC promotion uses durable identity | §10.3 | ~150 | 2 | ⬜ |
| P-6 | Remove `_suspect_orders` cache (after smoke-test verification) | §10.1 / §6.7 | ~200 | 2 | ⬜ |
| P-7 | Remove `_suspect_exit_orders` cache (after smoke-test verification) | §10.2 / §6.7 | ~150 | 2 | ⬜ |
| P-8 | Doc updates: PLAN.md, operator_controls_proposal.md §17, slippage_unification_tracker.md | §12 | ~50 | — | ⬜ |

---

## §10 Compensating-patch absorption matrix progress

Each row from the discovery doc's §10 maps to one or more commits above.

| § | Category | Replaces | Commits | Status |
|---|---|---|---|---|
| 10.1 | Entry uncertainty / duplicate prevention / pending grace | `_suspect_orders`, broker-open duplicate checks, `LIFECYCLE_PENDING_GRACE_SECONDS` | substrate-side prepared in 6, 9, 12; cache removal P-6 | 🔄 substrate done; consumer pending |
| 10.2 | Uncertain single-leg exits | `_suspect_exit_orders` | substrate-side prepared in 6; cache removal P-7 | 🔄 substrate done; consumer pending |
| 10.3 | Protective stop promotion / replacement / repair | `_reported_stop_promotion_failures` identity workaround | P-4, P-5 | ⬜ |
| 10.4 | Option trailing state split | `option_trailing_stops.alpaca_stop_order_id` denormalization | (separate follow-up) | ⬜ |
| 10.5 | Slippage recovery — preserve provenance | `SuspectOrder.modeled_price_kind` moves to per-order row | 4 (substrate column) + 9 (plumbed from engine through broker) + 11 (UPSERT preservation) | ✅ |
| 10.6 | Position-level partial-close accounting | (acceptance tests; carries forward unchanged) | 4 (rollup) | ✅ |
| 10.7 | MLEG partial-close `_spreads_pending_close` | (single-leg side solved; MLEG side deferred) | (deferred to spread lifecycle PR) | ⬜ |
| 10.8 | PR #58 disposition | (rebuild, do not cherry-pick) | (deferred until foundation merges) | ⬜ |

---

## §12.1 Regression test matrix progress

All 26 tests from the discovery doc must land in the implementation PR. Each row maps a regression test to the git commit that landed it (or to the planned consumer-wiring step that will land it).

Statuses below cover the SUBSTRATE-LANDED portion of the PR. Items still in the "planned" phase remain ⬜ until P-1..P-7 ship.

| # | Test (abbreviated) | Doc anchor | Commit | Status |
|---|---|---|---|---|
| 1 | Atomic apply_order_event — two unrelated order_ids, only matching updates | §6.4 R3-P0 | 4 | ✅ |
| 2 | Terminal-state immutability | §6.4 R3-P1a | 4 | ✅ |
| 3 | Side-signed rollup correctness | §6.6 R3-P1b | 4 | ✅ |
| 4 | MLEG two-row insert succeeds | §6.5 R5-C1 | 5 | ✅ |
| 5 | UPSERT + partial-unique-index pair | §6.5 R5-C2 | 5 | ✅ |
| 6 | All-or-nothing transaction on failure | §6.4 R4-P1b | 4 | ✅ |
| 7 | execution_id NULL on REST recovery | §6.5 R4-P1a | 8 | ✅ |
| 8 | `_suspect_orders` removal preserves TIMEOUT/UNKNOWN recovery | §10.1 | P-6 | ⬜ |
| 9 | `_suspect_exit_orders` removal preserves invariants | §10.2 | P-7 | ⬜ |
| 10 | `replacement_stop` atomic-replace | §10.3 / §6.4 | P-5 | ⬜ |
| 11 | Zero-fill working entry stays `pending` | §6.6.1 R7-P0 | 4 | ✅ |
| 12 | Working sell-side order blocks `closed` (R12 supersedes R8-1) | §6.6.1 R12-P1 | 4 | ✅ |
| 13 | `closed_at` set only on `closed` / `external_closed` | §6.6.1 R8-P2 | 4 | ✅ |
| 14 | `closed_at` reads new status via CTE | §6.6.1 R9-P1a | 4 | ✅ |
| 15 | Negative `current_qty` → `error` | §6.6.1 R9-P1b | 4 | ✅ |
| 16 | `'error'` retains owner_key lock | §6.2 R8-3 | 1 | ✅ |
| 17 | Reverse-pass skips `'error'` rows | §3.1 R8-4 | P-2 | ⬜ |
| 18 | Dedupe script detection-only default | §12.2 R8-2 | 7 | ✅ |
| 19 | Migration preflight covers `'error'` status | §12.2 R9-P1c | 2 | ✅ |
| 20 | Working sell-side order blocks `closed` AND lock retains | §6.6.1 R12-P1 | 4 | ✅ |
| 21 | Oversold position → `error` immediately | §6.6.1 R9-P1b + R11 | 4 | ✅ |
| 22 | Broker-snapshot guard defense-in-depth | §10.1 R10-P1b | P-1 | ⬜ |
| 23 | Direct `pending → filled` via fast path | §6.3 / §6.4 R11-P1 | 4 | ✅ |
| 24 | Direct `pending → canceled` via recovery | §6.3 / §6.4 R12 | 4 | ✅ |
| 25 | `PRAGMA foreign_keys = ON;` enforces FKs | §6.2 R13-G1 | 1 | ✅ |
| 26 | `net_realized_pnl` rollup from `trades` (not orders table) | §6.6 R13-G2 | 4 | ✅ |

20/26 landed in the substrate phase. The 6 remaining (#8, #9, #10, #17, #22) all depend on consumer wiring and ship with P-1 / P-2 / P-5 / P-6 / P-7.

---

## Migration prerequisites (§12.2)

Per discovery doc §12.2, the implementation PR must include a duplicate-row preflight that runs BEFORE `CREATE UNIQUE INDEX` on every startup. If duplicates exist:

1. Log `ERROR`-level message listing affected `order_id` / `owner_key` values
2. Surface through alert backend
3. `_ensure_db()` raises; bot exits non-zero
4. Operator runs `scripts/migrate_dedupe_trades.py` offline (detection / review / apply modes)
5. Next `recycle_bot.sh` applies the unique indexes cleanly

Implementation PR includes:
- [x] Pre-flight detection queries — `position_lifecycle.owner_key` (commit 2) + `trades.order_id` (commit 7)
- [x] `_ensure_db()` raises on duplicates (commit 2)
- [x] `scripts/migrate_dedupe_trades.py` with three modes (commit 7) + partition validation + accounting-conflict rejection (commits 10, 13)
- [ ] PLAN.md operator-runbook entry for the script (planned P-8)

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
