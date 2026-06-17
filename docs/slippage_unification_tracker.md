# Slippage Unification — Implementation Tracker

Companion to [`docs/slippage_unification_design.md`](slippage_unification_design.md).
This file tracks implementation progress across the three planned PRs.

Status legend: ✅ done · 🔄 in progress · ⬜ not started · ⏸ blocked

---

## Phase 1 — As-built summary

Phase 1 is complete on `feature/slippage-unification-phase1` with five
review-response commits added after the first ChatGPT/Gemini pass. Total
~1700 LOC including tests, 2093/2093 tests passing.

Review-response commits (review-1):

| Defect | Severity | Fix commit |
|---|---|---|
| 1. Exit path mis-tagged as arrival_midpoint | High | `5b4e3ff` |
| 2. SuspectOrder loses fallback provenance | Medium | `4c727ae` |
| 3. Migration failure poisoned cached connection | Medium | `a059406` |
| 4. Non-finite stop_price accepted | Medium | `78d4567` |
| 5. Undocumented parity exceptions | Low | (this commit) |

Documented Phase 1 divergence (intentional, deferred to Phase 2 consumer
migration):

- **Market entry without `modeled_price`** — legacy
  `realized_slippage_bps` still falls back to
  `decision.entry_reference_price`; new `slippage_signed_bps` is NULL.
  Aligning would change consumer-visible numbers today; defer to
  Phase 2. Pinned by
  `test_market_entry_without_benchmark_legacy_still_uses_decision_price`.

What landed:

- **6 nullable taxonomy columns** on `trades` (`slippage_benchmark_price`,
  `slippage_benchmark_kind`, `slippage_benchmark_timestamp`,
  `slippage_measurement_quality`, `slippage_signed_bps`,
  `slippage_adverse_bps`) plus `stop_trigger_price`. Idempotent ALTER on
  every bootstrap; pre-existing rows stay NULL.
- **`Literal[...]` enum aliases** (`SlippageBenchmarkKind`,
  `SlippageMeasurementQuality`) in `reporting.logger` so every writer
  call site is type-checked.
- **Stateless stop benchmarking** — `log_stop_fill` now accepts
  `stop_price: float | None` directly from the broker order at fill
  time. WebSocket path reads `update.order.stop_price`; recovery path
  reads `ClosedOrderInfo.stop_price`. No mutable open-position state.
  This is the load-bearing fix from the design.
- **All 13 codepaths** tag rows with explicit benchmark kind + quality.
  Default inference in `build_record` / `build_close_record` preserves
  prior behavior for any future caller that omits the parameters.
- **Dual-write preserved** — every populated row writes both the new
  taxonomy columns and the legacy `realized_slippage_bps` /
  `modeled_slippage_bps`. Where both are non-NULL they MUST agree, and
  five focused parity tests guard against drift.
- **Honest unavailable rows** — `log_external_close` no longer
  fabricates `0.0` for rows that never had real measurements (a
  deliberate change from the prior placeholder; Phase 2 consumers
  must handle NULL).

What did NOT land in Phase 1 (deferred as planned):

- Consumer migration (health, risk, calibration, dashboard) — Phase 2.
- Dashboard denominator dilution fix — Phase 2.
- Stop legacy dual-writes — Phase 2 (folded in from prior Phase 4).
- Historical row cleanup migration — Phase 3.

---

## Phase plan (Option A)

| Phase | Scope | Branch | PR | Status |
|---|---|---|---|---|
| 1 | Schema + writers + dual-write to legacy + 13 codepath tests | `feature/slippage-unification-phase1` | #43 merged `bf16b5a` | ✅ Merged |
| Smoke check | 2 days paper run on main; spot-check rows per codepath | main | — | 🔄 In progress |
| 2 + 4 | Consumer migration (health, risk, calibration, dashboard, pnl) + dashboard denominator dilution fix + drop legacy dual-writes | `feature/slippage-unification-phase2` | — | 🔄 In progress |
| 3 | Historical cleanup migration (phantom recovery rows + pre-`8316e64` LIMIT rows) | `feature/slippage-unification-phase3` | — | ⬜ Not started |

Calendar estimate: ~2 weeks total, 3 PRs.

---

## Phase 1 — Commit checklist

Branch: `feature/slippage-unification-phase1`

| # | Commit | LOC est. | Tests | Status |
|---|---|---:|---:|---|
| 0 | Implementation tracker + PLAN.md pointer | ~50 | — | ✅ `c9d4cb3` |
| 1 | Add slippage taxonomy columns to trades schema (idempotent ALTER TABLE) | ~70 | 4 | ✅ `be33be7` |
| 2 | Add `stop_price` parameter to `log_stop_fill`; dual-write legacy | ~110 | 5 | ✅ `f0113a7` |
| 3 | Wire WebSocket stop fill to broker `stop_price` (codepath 4) | ~30 | 1 + 3 existing assertion updates | ✅ `793f7fb` |
| 4 | Wire recovery stop fill to broker `stop_price` (codepaths 5, 6) | ~30 | 2 | ✅ `df0812c` |
| 5 | Tag single-leg entry/exit codepaths with benchmark kind (codepaths 1, 2, 3, 7, 9); add `benchmark_kind` + `benchmark_price` params to `build_close_record` | ~200 | 10 | ✅ `b5e05fe` |
| 6 | Tag option and spread codepaths (10, 11) | ~110 | 3 | ✅ `5b3d879` |
| 7 | Tag external-close and recovered-context codepaths (8, 12, 13); stop writing `0.0` from `log_external_close` | ~80 | 3 | ✅ `0c0d508` |
| 8 | Cross-cutting legacy-mirror parity assertion | ~120 | 5 | ✅ `e7c85b2` |

Total estimate: ~325 LOC + ~250 LOC tests.

---

## Phase 1 — Codepath coverage status

Per the design doc's matrix. Each row must end Phase 1 with the correct
`slippage_benchmark_kind` + `slippage_measurement_quality` pair pinned by a
test.

| # | Codepath | Site | Expected kind | Expected quality | Status |
|---|---|---|---|---|---|
| 1 | Single-leg market entry | `engine/trader.py:1591` `_log_entry` | `arrival_midpoint` / `fallback_latest_close` | `primary` / `fallback` | ✅ |
| 2 | Single-leg limit entry | `reporting/logger.py:425` `build_record` | `limit_price` | `unavailable` | ✅ |
| 3 | Discretionary market exit | `reporting/logger.py:504` `build_close_record` via `_close_single_leg_position` | equity `fallback_latest_close` / option `unavailable` (Defect 1 fix) | `fallback` / `unavailable` | ✅ |
| 4 | WebSocket stop fill | `engine/trader.py:3530` | `active_stop_price` | `primary` | ✅ |
| 5 | Broker-history recovered stop fill | `engine/trader.py:2974` | `active_stop_price` | `recovered` | ✅ |
| 6 | Standalone repair-stop fill | (falls through 4/5) | `active_stop_price` | `primary`/`recovered` | ✅ (via 4/5) |
| 7 | Fractional residual cleanup exit | `engine/trader.py:2509` `_log_close` via `_close_fractional_residual_position` | `unavailable` | `unavailable` | ✅ |
| 8 | Recovered missing-entry-context row | `engine/trader.py:3135, 3150` | `unavailable` | `recovered` | ✅ |
| 9 | Suspect-order recovery resolved filled | `engine/trader.py:1774` | `arrival_midpoint` (benchmark preserved) | `recovered` | ✅ |
| 10 | Async single-leg option fill | `engine/trader.py` `_drain_option_fills` | `limit_price` | `unavailable` | ✅ (via build_record) |
| 11 | Spread entry/exit fill | `reporting/logger.py:662` `log_spread_fill` | short leg `combo_limit` / long leg `unavailable` | `primary` / `unavailable` | ✅ |
| 12 | Single-leg external close | `reporting/logger.py:624` `log_external_close` | `unavailable` | `unavailable` | ✅ |
| 13 | Spread external close | `engine/trader.py:3381` | `unavailable` | `unavailable` | ✅ (via log_spread_fill) |

---

## Phase 1 — Schema additions

New columns on `trades` (all `NULL`-default, no indexes):

```sql
ALTER TABLE trades ADD COLUMN slippage_benchmark_price REAL;
ALTER TABLE trades ADD COLUMN slippage_benchmark_kind TEXT;
ALTER TABLE trades ADD COLUMN slippage_benchmark_timestamp TEXT;
ALTER TABLE trades ADD COLUMN slippage_measurement_quality TEXT;
ALTER TABLE trades ADD COLUMN slippage_signed_bps REAL;
ALTER TABLE trades ADD COLUMN slippage_adverse_bps REAL;
ALTER TABLE trades ADD COLUMN stop_trigger_price REAL;
```

Enum values (enforced via `Literal[...]` in `TradeRecord`):

- `slippage_benchmark_kind`: `arrival_midpoint`, `decision_price`, `fallback_latest_close`, `active_stop_price`, `combo_limit`, `limit_price`, `unavailable`
- `slippage_measurement_quality`: `primary`, `fallback`, `recovered`, `unavailable`

---

## Smoke check plan (between Phase 1 and Phase 2)

Goal: confirm writers fire correctly across codepaths that actually trigger
in ~2 paper days on main after PR #43 merged. Not exhaustive — coverage
gaps get caught in Phase 2 review.

**Operator action required after merge:** `./recycle_bot.sh` to pick up the
new code. Migration runs idempotently on the first `_ensure_db()` call.

Checklist:

- [ ] Bot recycles cleanly on main (no schema migration errors in logs)
- [ ] Spot-check 5+ entry rows: `slippage_benchmark_kind IS NOT NULL`
- [ ] Spot-check any exit rows: parity holds where expected
- [ ] If any stop fires: confirm `stop_trigger_price IS NOT NULL`, `kind='active_stop_price'`
- [ ] If any spread fills: confirm long-leg row has NULL slippage, not `0.0`
- [ ] Dashboard still renders without errors (consumers still read legacy columns)
- [ ] Health report still runs (`scripts/strategy_health_review.py`)
- [ ] Risk kill switch does not trip on bogus values

Spot-check queries (run against `data/trades.db`):

```sql
-- 1. Writers populate new columns across all kinds we expect to see
SELECT slippage_benchmark_kind, slippage_measurement_quality, COUNT(*)
FROM trades
WHERE timestamp >= '<merge-date>'
GROUP BY slippage_benchmark_kind, slippage_measurement_quality;

-- 2. Parity guard — zero rows expected
SELECT COUNT(*) AS drifted
FROM trades
WHERE slippage_signed_bps IS NOT NULL
  AND realized_slippage_bps IS NOT NULL
  AND ABS(realized_slippage_bps - slippage_signed_bps) > 0.01;

-- 3. External close rows write NULL on both column families
SELECT realized_slippage_bps, slippage_signed_bps, COUNT(*)
FROM trades
WHERE reason LIKE '%external%' AND timestamp >= '<merge-date>'
GROUP BY realized_slippage_bps, slippage_signed_bps;
```

Rollback plan if smoke surfaces a regression: `git revert bf16b5a && ./recycle_bot.sh`.
New writes return to legacy-only behavior; pre-existing rows untouched;
smoke-period rows keep their new columns populated but no consumer reads them.

---

## Phase 2 + 4 — Consumer migration scope

Branch: `feature/slippage-unification-phase2`

- [ ] `strategies/health/assessor.py` reads `slippage_adverse_bps` (and filters by `measurement_quality`)
- [ ] `risk/manager.py` kill switch reads `slippage_adverse_bps`
- [ ] `scripts/calibrate_health_thresholds.py` reads `slippage_adverse_bps`
- [ ] `dashboard.py` Recent Trades displays `slippage_benchmark_kind` + `measurement_quality` alongside slippage bps
- [ ] `dashboard.py:710` denominator dilution fix — `IS NOT NULL` mask on slippage_denom
- [ ] `reporting/pnl.py` weekly/daily/slippage reports read `slippage_adverse_bps`; skip NULL rows
- [ ] Stop dual-writing `realized_slippage_bps` / `modeled_slippage_bps` on new rows (Phase 4 fold-in)
- [ ] Update tests that read legacy columns
- [ ] **Reconcile Phase 1 divergence**: market-entry path without `modeled_price` — align legacy `realized_slippage_bps` with new (NULL instead of decision-price fallback). See `test_market_entry_without_benchmark_legacy_still_uses_decision_price` for the pinned current behavior.

### Commit checklist

Branch: `feature/slippage-unification-phase2`

| # | Commit | Notes | Status |
|---|---|---|---|
| 0 | Tracker kickoff + commit checklist | this commit | ⬜ |
| 1 | `strategies/health/assessor.py` → `slippage_adverse_bps`; quality whitelist `IN ('primary','fallback')` | drops legacy `reason NOT LIKE` defensive filter (superseded by quality column) | ⬜ |
| 2 | `scripts/calibrate_health_thresholds.py` → same | mirrors assessor query shape | ⬜ |
| 3 | `RiskManager.record_fill_slippage` param rename `realized_bps` → `adverse_bps` | engine already clamps to adverse before calling; pure naming + docs | ⬜ |
| 4 | Dashboard Recent Trades surfaces `slippage_benchmark_kind` + `slippage_measurement_quality` | + `load_trades` empty-frame columns | ⬜ |
| 5 | Dashboard `compute_strategy_stats` → `slippage_adverse_bps`; numerator + denominator from same `.notna()` mask | rename column to `Avg Adverse Slippage Bps`; MLEG branch parallel | ⬜ |
| 6 | `reporting/pnl.py` weekly/daily/slippage reports → `slippage_adverse_bps`; skip NULL rows | no silent zero-defaults | ⬜ |
| 7 | Drop legacy dual-writes across writers; reconcile Phase 1 divergence; swap parity tests for no-legacy tests | + MLEG long-leg legacy-NULL test | ⬜ |
| 8 | Tracker + PLAN.md sync | mark Phase 2+4 ✅; P1 row updated | ⬜ |

---

## Phase 3 — Historical cleanup scope

Branch: `feature/slippage-unification-phase3`

Two deterministic predicates (idempotent migration script with dry-run mode):

```sql
-- Phantom recovery rows (pre-32e21c2)
UPDATE trades
SET realized_slippage_bps = NULL, modeled_slippage_bps = NULL
WHERE reason LIKE '%recovered entry context%'
  AND realized_slippage_bps IS NOT NULL
  AND timestamp < '2026-06-02T18:20:37+00:00';

-- Pre-LIMIT-carve-out limit rows (pre-8316e64)
UPDATE trades
SET realized_slippage_bps = NULL, modeled_slippage_bps = NULL
WHERE order_type = 'limit'
  AND realized_slippage_bps IS NOT NULL
  AND timestamp < '2026-06-02T23:31:45+00:00';
```

- [ ] Script runs in `--dry-run` mode, prints affected row count per predicate
- [ ] Backup `trades.db` before destructive run
- [ ] Verify post-run row counts match dry-run prediction
- [ ] Health / dashboard re-render confirms cleaner averages

---

## Open items / decisions made

| Question | Decision | Date |
|---|---|---|
| `TradeRecord` extension shape | In-place, no sub-dataclass | 2026-06-04 |
| Test fixture location | `tests/conftest.py` (`slippage_row_factory`) | 2026-06-04 |
| PR shape for Phase 1 | Single PR, 8 commits | 2026-06-04 |
| Codepath 7 (fractional residual) handling | Write `unavailable`/`unavailable`/NULL; `build_close_record` gets explicit `benchmark_kind` + `benchmark_price` params | 2026-06-04 |
| Phase 4 (drop legacy writes) | Folded into Phase 2 PR | 2026-06-04 |
| Burn-in duration | 2-day smoke check (paper bot, not live) | 2026-06-04 |
