# Order Lifecycle Foundation — Implementation Tracker

Companion to [`docs/order_lifecycle_state_machine.md`](order_lifecycle_state_machine.md) (the discovery doc, merged in PR #59).
Tracks implementation progress for the foundation PR — schema + `apply_order_event` + reconciliation paths + compensating-patch absorption.

Status legend: ✅ done · 🔄 in progress · ⬜ not started · ⏸ blocked

---

## Phase 1 — Foundation PR scope

The foundation PR implements the discovery doc's §6 (schema + atomic event API + rollups), §10 (compensating-patch absorption), and §12 (tests + migration prerequisites). Strategy-side strict, write-side substrate only. Consumer migration and PR #58 rebuild are explicitly deferred.

| Phase | Scope | Branch | PR | Status |
|---|---|---|---|---|
| 1 | Substrate only: schema + `PositionLifecycleOrdersStore` + atomic `apply_order_event` + submit-time inserts + trades-writer alignment + migration preflight + dedupe script. Reconciliation wiring (WebSocket / cycle / startup), cache removal, and the remaining `_suspect_*` work are deferred to a follow-up branch (see "Planned" section). | `feat/order-lifecycle-foundation-impl` | [#60](https://github.com/francomarb/trading-bot/pull/60) | ✅ Approved across 6 review rounds; ready to merge |

---

## Commit log (time-ordered, as merged on the branch)

Two phases on `feat/order-lifecycle-foundation-impl`:

  - **A. Substrate landed** (commits 0-6): schema, store, atomic
    `apply_order_event`, trades-row writer alignment, and submit-time
    insert. No live behavior change; substrate populated but not yet
    read by consumers.
  - **B. PR #60 review-fix series** (commits 7-22): six rounds of
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
| 15 | B-r3 | Tracker restructure + numbering normalization (PR #60 round 3, fix 5) | n/a | — | ✅ |
| 16 | B-r4 | Keeper-required partition (delete-all bug); asymmetric conflict (keeper-NULL + delete-non-NULL); expanded conflict column set; detect output prints accounting evidence (PR #60 round 4, fixes P0 + P1 dedupe) | §12.2 | 8 | ✅ |
| 17 | B-r4 | Expanded `_UPSERT_IDENTITY_CONFLICT_COLUMNS` to cover `position_id`, `symbol`, `side`, `strategy`, `order_type`, `requested_qty` (PR #60 round 4, fix P2 identity) | §6.5 / §6.6 | 8 | ✅ |
| 18 | B-r4 | Tracker fixes: correct test-matrix count, scope statement reflects substrate-only, smoke-protocol items gated on planned phase (PR #60 round 4, fix P2 tracker) | n/a | — | ✅ |
| 19 | B-r5 | Schema-driven dedupe conflict comparison: invert allowlist policy so every non-noise column on both tables is conflict-checked; closes lifecycle corruption (current_qty / entry_order_id / first_fill_at) and trades gaps (qty / stop_price / execution_id) (PR #60 round 5, fixes P0 + P1) | §12.2 | 7 | ✅ |
| 20 | B-r5 | Documentation drift: tracker review-fix range now reflects current commit, commit 18 marked done, UPSERT docstring lists all 7 identity-conflict columns (PR #60 round 5, fix P2 docs) | n/a | — | ✅ |
| 21 | B-r6 | Split fingerprint exclusion from conflict-discardable: lifecycle timestamps (`opened_at` / `created_at` / `trades.timestamp`) and `schema_version` now fingerprint-tracked even though they're conflict-discardable; mixed `schema_version` now flagged as conflict (PR #60 round 6, fixes P1 + P2 schema_version) | §12.2 | 3 | ✅ |
| 22 | B-r6 | Tracker fix: mark commit 20 done, add rows for 21 + 22 (PR #60 round 6, fix P2 tracker) | n/a | — | ✅ |
| 23 | B-r7 | Pre-merge tidies: mark Phase 1 complete; remove stale schema_version-discardable comment; soften dedupe WAL warning (PR #60 round 7 follow-ups) | n/a | — | 🔄 In progress |

Substrate-landed totals: ~7100 LOC code + ~3800 LOC tests across 23 commits.

---

## Phase 2 — Consumer wiring + cache removal (`feat/order-lifecycle-consumer-wiring`)

After PR #60 merged to main on 2026-06-14, the consumer wiring landed on a follow-up branch. Substrate became load-bearing: every state transition the legacy `_suspect_orders` / `_suspect_exit_orders` caches were catching is now captured by the substrate, and the side effects fire from `_maybe_dispatch_substrate_{entry,exit}_fill` via the same code path the synchronous fast path uses.

| Step | Scope | Doc anchor | Commit(s) | Status |
|---|---|---|---|---|
| P-4 | Wire `protective_stop` role: broker OTO child + fractional GTC + repair flow get their own per-order rows | §10.3 | `6883b87` | ✅ |
| P-5 | Wire `replacement_stop` role: `promote_equity_stop_to_gtc` records lineage via `replaces_order_id` | §10.3 | `f3feaa7` | ✅ |
| P-6e | Wire `exit` role: `close_position` records the close substrate row at submit time | §10.3 | `bf3a6e4` | ✅ |
| P-1 | Wire WebSocket stream → `apply_order_event` via the new `_pending_lifecycle_events` queue + engine cycle drain | §6.4 / §10.1 | `6cd718e` | ✅ |
| P-2 | Wire cycle reconciliation: per-cycle REST walk of substrate rows whose `order_id` is absent from `snapshot.open_orders`, capped at 20/cycle | §6.4 / §10.1 / §3.1 | `7cb6366` | ✅ |
| P-3 | Wire startup reconciliation: post-restart walk of all non-terminal substrate rows, unlimited (one-shot) | §6.4 / §10.1 | `92b5157` | ✅ |
| P-6a | Extract `_apply_recovered_entry_side_effects` helper (ownership bind + entry-price cache + stop replacement + alert) | §10.1 | `1378289` | ✅ |
| P-6b | Wire `_maybe_dispatch_substrate_entry_fill` from stream + cycle + startup drains; gates on `_has_position` to avoid double-firing | §10.1 / §6.7 | `73793d7` | ✅ |
| P-6c | Delete `_suspect_orders` cache, `SuspectOrder` dataclass, `_remember_suspect_order`, `_recover_suspect_orders` | §10.1 / §6.7 | `b982870` | ✅ |
| P-7a | Wire `_maybe_dispatch_substrate_exit_fill` from same drain handlers; calls `_record_recovered_exit_fill` with `skip_trades_dedup_check=True`; idempotency via `_has_position` ownership gate (post round-1 P1 fix) | §10.2 / §6.7 | `860ce93` + `c247fe4` | ✅ |
| P-7b | Delete `_suspect_exit_orders` cache, `SuspectExitOrder` dataclass, `_recover_suspect_exit_orders`, staging branch in `_close_single_leg_position` | §10.2 / §6.7 | `ecce2da` | ✅ |
| P-8 | Doc updates: this tracker + PLAN.md (slippage_unification_tracker.md and operator_controls_proposal.md don't reference the foundation — no edits needed there) | §12 | `8bf9046` | ✅ |
| PR #61 round-1 fix P1 | Exit dispatch via ownership gate (was suppressed by its own substrate UPSERT); +2 integration tests covering the apply→dispatch sequence | §10.2 | `c247fe4` | ✅ |
| PR #61 round-1 fix P2 | Cycle REST cap counts actual calls, not rows read; tracker doc drift; NULL-order_id attach retry tracked as follow-up | §10.1 | `d181f1d` | ✅ |
| PR #61 round-2 fix | Honest CRITICAL log on orphaned-attach (was lying about retry); stale docstrings on exit-dispatch; new known follow-up for recovered-entry accounting completeness | §10.1 / §10.2 | `a08503b` | ✅ |

Consumer-wiring totals: ~2,150 LOC code + ~1,000 LOC tests across 15 commits (12 original + 3 PR #61 review-fix commits); ~500 LOC of legacy cache infrastructure deleted.

### Known follow-ups (tracked, not blocking)

- **NULL-order_id attach orphaning**: cycle and startup reconcilers walk rows with `order_id IS NOT NULL` only. A synchronous attach failure OR a bot crash between async submit and the next cycle's drain leaves the row at `pending` with `order_id=NULL`. The lifecycle-attach queue is in-memory and lost on restart — it does NOT re-drain. The substrate row is then ORPHANED until manually resolved. The CRITICAL log in `_drain_lifecycle_attaches` (engine/trader.py:4568) says exactly this so the operator knows to inspect. Recovery requires a separate REST path that walks NULL-order_id rows and resolves via `get_order_by_client_id`. Uncommon trigger; not blocking on paper, but the failure mode is more serious than the earlier "self-heals on restart" framing implied. PR #61 round-2 reviewer.

- **Exit substrate write path unexercised so far**: production DB shows 0 `role='exit'` rows. Re-investigated 2026-06-18 — the earlier framing ("closed via signal post-PR #61") was inaccurate. All four single-leg options closes that landed after PR #61 merged were `stop_triggered` (SPY260702C00724000 on 2026-06-12, SPY260702C00737000 on 2026-06-15, SPY260702C00753000 on 2026-06-16, SPY260710C00746000 on 2026-06-17 — all `spy_options_reversion`). Stop closes correctly flow through the `protective_stop` role, not `exit`, so 0 exit rows is the expected state given the actual events. There is no confirmed exit-write defect yet. Watch the next signal-driven close (equity or options) to confirm `broker.close_position` ([execution/broker.py:2482](../execution/broker.py:2482)) actually inserts the `role='exit'` row; if it doesn't, escalate. A separate underlying-vs-OCC symbol-key collision did surface on the options close path (the 2026-06-16 `SPY|sell|0|external_close_detected` row, written 12 minutes after a clean `SPY260702C00753000 stop_triggered` fill); PR #69 cleans the stale `option_trailing_stops` mirror at recovery time but the deferred lifecycle-state split (§10.4) is still the durable fix.

- ~~**Recovered-entry accounting completeness**~~ ✅ CLOSED via PR #68 (merged 2026-06-17 at `8977f22`; 4 review rounds). What landed:
  - **Engine submit path is order-type-aware** ([engine/trader.py:1960](../engine/trader.py:1960)). MARKET entries forward `kind='arrival_midpoint'`/`'fallback_latest_close'` + the captured benchmark price; LIMIT and STOP_LIMIT forward `kind='limit_price'`, `quality='unavailable'`, `benchmark_price=None` — codepath §2 in `docs/slippage_unification_design.md`. Pre-fix every entry got `'arrival_midpoint'`/`'primary'` regardless of order_type and the trades-row UPSERT's PRESERVE-FIRST-NON-NULL COALESCE locked those wrong tags in forever, exactly the dashboard/health/calibration smell PR #67 set out to eliminate.
  - **Dispatch accepts `partially_filled` events** in addition to `filled` ([engine/trader.py:2304](../engine/trader.py:2304)), matching docs/order_lifecycle_state_machine.md §3.2.
  - **Side-effects gated single-shot via `_has_position`**; accounting runs on every event so the trade row's signed/adverse refresh against the cumulative `avg_fill_price` on partial-then-final-fill recoveries.
  - **Accounting block is a focused UPDATE** of `slippage_signed_bps` and `slippage_adverse_bps` only ([engine/trader.py:2473](../engine/trader.py:2473)) — earlier rounds went through `build_record` + `trade_logger.log` which clobbered `reason` and `entry_reference_price` on synchronous-bound rows. The UPDATE is idempotent (writes the same values the synchronous `_log_entry` already wrote for normal fills) and only fires when the substrate row's `benchmark_kind` is MARKET-style and `benchmark_price > 0`. The granular CRITICAL handler fires if `single_leg_realized_slippage_bps` or the SQL execute raises.
  - **`RiskDecision` construction lives inside the ownership gate** so already-bound option LIMIT entries (`intended_stop_price=None`) don't raise `ValueError: stop_price must be positive` and emit a false-alarm CRITICAL on a healthy options sleeve.
  - **Hot-path log line** emits `slip_signed={bps} kind={kind} quality={quality}` from the new taxonomy columns ([reporting/logger.py](../reporting/logger.py)). Pre-fix it emitted `slip=Nonebps` on every row after the legacy column dropped.

  Tests (all in `tests/test_apply_order_event.py::TestEntryDispatchSlippageCompleteness` + `tests/test_engine.py::TestOrderTypeAwareSubmitSlippageTagging`):
  1. MARKET dispatch fills computed slippage after substrate write
  2. Missing-substrate-benchmark path records honest NULL
  3. Recovered LIMIT preserves `'limit_price'`/`'unavailable'`
  4. Single PARTIAL dispatches and binds ownership
  5. PARTIAL-then-FILLED refreshes slippage with final `avg_fill_price`
  6. Completeness-call failure logs CRITICAL (loguru sink assertion)
  7. Synchronous-bound row's `reason` and `entry_reference_price` preserved through accounting refresh
  8. Already-bound option LIMIT entry dispatches silently (no false CRITICAL)
  9-11. Submit-time tagging: MARKET-with-quote, MARKET-fallback-to-close, LIMIT-forced-unavailable

  Original follow-up flagged by PR #61 round-2 reviewer.

### Four-tier capture pipeline (substrate becomes load-bearing)

| Tier | Path | Trigger | Cost |
|---|---|---|---|
| 1 | Substrate-attach queue (foundation commit 12) | Worker post-submit | Free (queue) |
| 2 | WS → `apply_order_event` (P-1) | Real-time Alpaca trade_update | Free (already-paid stream) |
| 3 | Cycle reconcile (P-2) | Every 5 min for missed-terminal rows | ≤20 REST calls/cycle |
| 4 | Startup reconcile (P-3) | Each bot restart, all non-terminal rows | One-shot REST sweep |

Each tier writes through `apply_order_event`, which advances the per-order state machine, recomputes the position rollup, and updates the position-status CTE. The same dispatch (`_maybe_dispatch_substrate_{entry,exit}_fill`) fires the engine-side side effects regardless of which tier captured the transition — synchronous happy path or async recovery use one code path.

---

## §10 Compensating-patch absorption matrix progress

Each row from the discovery doc's §10 maps to one or more commits above.

| § | Category | Replaces | Commits | Status |
|---|---|---|---|---|
| 10.1 | Entry uncertainty / duplicate prevention / pending grace | `_suspect_orders`, broker-open duplicate checks, `LIFECYCLE_PENDING_GRACE_SECONDS` | substrate: 6, 9, 12; consumer wiring: P-1/P-2/P-3; cache delete: P-6 | ✅ |
| 10.2 | Uncertain single-leg exits | `_suspect_exit_orders` | substrate: 6 + P-6e; consumer wiring: P-1/P-2/P-3; cache delete: P-7 | ✅ |
| 10.3 | Protective stop promotion / replacement / repair | `_reported_stop_promotion_failures` identity workaround | P-4, P-5 | ✅ |
| 10.4 | Option trailing state split | `option_trailing_stops.alpaca_stop_order_id` denormalization | (separate follow-up) — PR #69 added recovered-close mirror cleanup (`_cleanup_option_trailing_state` at all three broker-history recovery paths) so the deferred split does not need to handle a stale-mirror-on-recovery edge case at migration time | ⬜ |
| 10.5 | Slippage recovery — preserve provenance | `SuspectOrder.modeled_price_kind` moves to per-order row | 4 (substrate column) + 9 (plumbed from engine through broker) + 11 (UPSERT preservation) | ✅ |
| 10.6 | Position-level partial-close accounting | (acceptance tests; carries forward unchanged) | 4 (rollup) | ✅ |
| 10.7 | MLEG partial-close `_spreads_pending_close` | (single-leg side solved; MLEG side deferred) | (deferred to spread lifecycle PR) | ⬜ |
| 10.8 | PR #58 disposition | (rebuild, do not cherry-pick) | PR #62 — minimal-scope rewrite on the substrate (`feat/donchian-stop-limit-v2`); PR #58 closed | ✅ |

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
| 8 | `_suspect_orders` removal preserves TIMEOUT/UNKNOWN recovery | §10.1 | P-6 (TestSubstrateEntryFillDispatchSemantics) | ✅ |
| 9 | `_suspect_exit_orders` removal preserves invariants | §10.2 | P-7 (TestSubstrateExitFillDispatchSemantics) | ✅ |
| 10 | `replacement_stop` atomic-replace | §10.3 / §6.4 | P-5 (TestReplacementStopSubstrate) | ✅ |
| 11 | Zero-fill working entry stays `pending` | §6.6.1 R7-P0 | 4 | ✅ |
| 12 | Working sell-side order blocks `closed` (R12 supersedes R8-1) | §6.6.1 R12-P1 | 4 | ✅ |
| 13 | `closed_at` set only on `closed` / `external_closed` | §6.6.1 R8-P2 | 4 | ✅ |
| 14 | `closed_at` reads new status via CTE | §6.6.1 R9-P1a | 4 | ✅ |
| 15 | Negative `current_qty` → `error` | §6.6.1 R9-P1b | 4 | ✅ |
| 16 | `'error'` retains owner_key lock | §6.2 R8-3 | 1 | ✅ |
| 17 | Reverse-pass skips `'error'` rows | §3.1 R8-4 | P-2 (TestCycleReconcileStoreQuery::test_error_status_excluded) | ✅ |
| 18 | Dedupe script detection-only default | §12.2 R8-2 | 7 | ✅ |
| 19 | Migration preflight covers `'error'` status | §12.2 R9-P1c | 2 | ✅ |
| 20 | Working sell-side order blocks `closed` AND lock retains | §6.6.1 R12-P1 | 4 | ✅ |
| 21 | Oversold position → `error` immediately | §6.6.1 R9-P1b + R11 | 4 | ✅ |
| 22 | Broker-snapshot guard defense-in-depth | §10.1 R10-P1b | P-1 (TestStreamDrainEndToEnd + TestSubstrateEntryFillDispatchSemantics — _has_position guard) | ✅ |
| 23 | Direct `pending → filled` via fast path | §6.3 / §6.4 R11-P1 | 4 | ✅ |
| 24 | Direct `pending → canceled` via recovery | §6.3 / §6.4 R12 | 4 | ✅ |
| 25 | `PRAGMA foreign_keys = ON;` enforces FKs | §6.2 R13-G1 | 1 | ✅ |
| 26 | `net_realized_pnl` rollup from `trades` (not orders table) | §6.6 R13-G2 | 4 | ✅ |

26/26 ✅ as of consumer-wiring branch. The original 21/26 (substrate phase) plus the 5 deferred items (#8, #9, #10, #17, #22) all landed via the planned consumer-wiring steps. Test anchors are listed in the table above for each item.

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

- [x] `PLAN.md` — Live readiness gate row for slippage / operator controls; foundation PR merged (#60, #61); PR #58 closed and rebuilt as PR #62
- [x] `docs/operator_controls_proposal.md` §17 amended for write-side / read-side substrate split + per-order table substrate Phase C writes into (amended 2026-06-15; Phase C subsequently shipped via PR #66 on 2026-06-16)
- [ ] `docs/slippage_unification_tracker.md` — Phase 2 scope references foundation-provided pre-fill provenance
- [x] PR #58 description — converted to draft with blocked/cherry-pickable list

---

## Smoke check protocol

### Substrate-merge smoke (PR #60) — ✅ passed 2026-06-14

After PR #60 merged, the bot recycled cleanly on the new code:

- [x] Bot recycles cleanly (migration runs idempotently)
- [x] Preflight duplicate check passes on production DB — both
      `position_lifecycle.owner_key` and `trades.order_id` dimensions
      (clean on first scan; no remediation needed)
- [x] First post-merge entry creates a `position_lifecycle_orders`
      row at status='pending' — observed (3 `entry_primary` rows
      created on production DB; one each filled / canceled / pending
      as of 2026-06-18)
- [x] Slippage benchmark provenance populated — observed
      (`arrival_midpoint`/`primary` on 1 row,
      `fallback_latest_close`/`fallback` on 2 rows; matches
      order-type-aware tagging shipped in PR #68 round-1 P1 fix)
- [x] Parity check: existing `position_lifecycle` rollups remain
      consistent with pre-merge behavior

### Full-loop smoke (consumer-wiring branch) — pending Monday's first fill

The consumer-wiring branch (`feat/order-lifecycle-consumer-wiring`)
recycled cleanly. The full pipeline (entry submit → substrate row →
WS apply → dispatch → ownership bind → recovery on any tier) needs
a live fill to validate end-to-end. Items to check after Monday's
first new entry:

- [x] Substrate `position_lifecycle_orders` row created at submission
      with `role='entry_primary'`, `status='pending'`, `order_id`
      attached on submit return
- [x] WebSocket fill event observed and applied — substrate row
      advances `pending` → `working` → `filled` (3 filled
      `entry_primary` rows on production DB)
- [x] `position_lifecycle` row transitions `pending` → `open` via
      the position-status CTE (6 currently-open positions)
- [ ] If the position later closes via a sell signal: `role='exit'`
      row created at close submit; advances to `filled`; position
      transitions to `closed` — **anomaly**: 2 single-leg options
      positions closed via signal post-PR #61 merge but ZERO `exit`
      role rows on production DB (see Known follow-ups below)
- [x] If the protective stop fires instead: `role='protective_stop'`
      row advances to `filled`; position transitions to `closed`
      (2 protective_stop rows: one canceled when position closed
      via signal, one pending against current open position)
- [x] No `_suspect_orders` / `_suspect_exit_orders` references in
      logs (caches deleted in PR #61)
- [x] `_drain_lifecycle_events` per-cycle log lines visible at
      DEBUG when fills land
- [x] Cycle and startup reconcile drains run without CRITICAL
      log lines

Once full-loop smoke passes, the PR #58 rebuild can ship. (Update 2026-06-14: the rebuild has shipped as PR #62 — `feat/donchian-stop-limit-v2`. Full-loop smoke for the rebuild itself is still waiting on the first Donchian fill against the new STOP_LIMIT path.)

---

## Rollback plan

Two phases, two rollback strategies:

### Phase 1 (PR #60 substrate) rollback

`git revert <pr-60-merge-sha> && ./recycle_bot.sh`. Schema tables remain in `data/trades.db` but no code writes them post-revert. Pre-existing rows untouched. Substrate is additive on phase 1, so revert is a clean no-op for observable behavior.

### Phase 2 (consumer wiring) rollback

The consumer-wiring branch DELETES the legacy `_suspect_orders` and `_suspect_exit_orders` caches. Reverting the cache-removal commits (`b982870`, `ecce2da`) is safe — the substrate-driven dispatch (`73793d7`, `860ce93`) and the caches can coexist (dual-write), so a partial rollback of just the deletion commits restores the safety net without breaking the substrate.

Full Phase 2 revert (all 15 commits) reverts to phase-1 substrate-only state. Safe.

A partial rollback (e.g., keeping P-1 stream wiring but reverting P-6/P-7 cache removal) is supported because the substrate dispatches gate on `_has_position` and the cache dispatches gate on `_suspect_orders` membership — both can run simultaneously without double-firing alerts.

Idempotent — safe to apply and revert any number of times during smoke validation.

---

## Open decisions

| Question | Decision | Date |
|---|---|---|
| Branch name | `feat/order-lifecycle-foundation-impl` (discovery doc was on `feat/order-lifecycle-foundation`) | 2026-06-12 |
| `option_trailing_stops` migration scope | Soft (FK reference column added; existing columns kept as denormalized mirrors during migration) | discovery doc §10.4 |
| `apply_order_event` API location | New module `engine/lifecycle_orders.py` to keep separation from `engine/lifecycle.py` (position-level store) | (TBD during commit 3) |
| Suspect-cache removal sequence | After verification of cycle / stream / startup paths, NOT day-one of merge | discovery doc §10.9 step 3 |
