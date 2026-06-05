# Slippage Unification — Implementation Tracker

Companion to [`docs/slippage_unification_design.md`](slippage_unification_design.md).
This file tracks implementation progress across the three planned PRs.

Status legend: ✅ done · 🔄 in progress · ⬜ not started · ⏸ blocked

---

## Phase plan (Option A)

| Phase | Scope | Branch | PR | Status |
|---|---|---|---|---|
| 1 | Schema + writers + dual-write to legacy + 13 codepath tests | `feature/slippage-unification-phase1` | — | 🔄 In progress |
| Smoke check | 2 days paper run; spot-check rows per codepath | (no branch) | — | ⬜ Not started |
| 2 + 4 | Consumer migration (health, risk, calibration, dashboard) + dashboard denominator dilution fix + drop legacy dual-writes | `feature/slippage-unification-phase2` | — | ⬜ Not started |
| 3 | Historical cleanup migration (phantom recovery rows + pre-`8316e64` LIMIT rows) | `feature/slippage-unification-phase3` | — | ⬜ Not started |

Calendar estimate: ~2 weeks total, 3 PRs.

---

## Phase 1 — Commit checklist

Branch: `feature/slippage-unification-phase1`

| # | Commit | LOC est. | Tests | Status |
|---|---|---:|---:|---|
| 0 | Implementation tracker + PLAN.md pointer | ~50 | — | 🔄 In progress |
| 1 | Add slippage taxonomy columns to trades schema (idempotent ALTER TABLE) | ~30 | 1 | ⬜ |
| 2 | Add `stop_price` parameter to `log_stop_fill`; dual-write legacy | ~80 | 1 | ⬜ |
| 3 | Wire WebSocket stop fill to broker `stop_price` (codepath 4) | ~10 | 1 | ⬜ |
| 4 | Wire recovery stop fill to broker `stop_price` (codepaths 5, 6) | ~15 | 1 | ⬜ |
| 5 | Tag single-leg entry/exit codepaths with benchmark kind (codepaths 1, 2, 3, 7, 9); add `benchmark_kind` + `benchmark_price` params to `build_close_record` | ~80 | 5 | ⬜ |
| 6 | Tag option and spread codepaths (10, 11) | ~50 | 2 | ⬜ |
| 7 | Tag external-close and recovered-context codepaths (8, 12, 13); stop writing `0.0` from `log_external_close` | ~40 | 3 | ⬜ |
| 8 | Cross-cutting legacy-mirror parity assertion | ~20 | 1 | ⬜ |

Total estimate: ~325 LOC + ~250 LOC tests.

---

## Phase 1 — Codepath coverage status

Per the design doc's matrix. Each row must end Phase 1 with the correct
`slippage_benchmark_kind` + `slippage_measurement_quality` pair pinned by a
test.

| # | Codepath | Site | Expected kind | Expected quality | Status |
|---|---|---|---|---|---|
| 1 | Single-leg market entry | `engine/trader.py:1591` `_log_entry` | `arrival_midpoint` / `fallback_latest_close` | `primary` / `fallback` | ⬜ |
| 2 | Single-leg limit entry | `reporting/logger.py:425` `build_record` | `limit_price` | `unavailable` | ⬜ |
| 3 | Discretionary market exit | `reporting/logger.py:504` `build_close_record` | `arrival_midpoint` | `primary` | ⬜ |
| 4 | WebSocket stop fill | `engine/trader.py:3530` | `active_stop_price` | `primary` | ⬜ |
| 5 | Broker-history recovered stop fill | `engine/trader.py:2974` | `active_stop_price` | `recovered` | ⬜ |
| 6 | Standalone repair-stop fill | (falls through 4/5) | `active_stop_price` | `primary`/`recovered` | ⬜ |
| 7 | Fractional residual cleanup exit | `engine/trader.py:2509` `_log_close` via `_close_fractional_residual_position` | `unavailable` | `unavailable` | ⬜ |
| 8 | Recovered missing-entry-context row | `engine/trader.py:3135, 3150` | `unavailable` | `recovered` | ⬜ |
| 9 | Suspect-order recovery resolved filled | `engine/trader.py:1774` | same as codepath 1, else `unavailable` | `primary`/`unavailable` | ⬜ |
| 10 | Async single-leg option fill | `engine/trader.py` `_drain_option_fills` | `limit_price` | `unavailable` | ⬜ |
| 11 | Spread entry/exit fill | `reporting/logger.py:662` `log_spread_fill` | short leg `combo_limit` / long leg `unavailable` | `primary` / `unavailable` | ⬜ |
| 12 | Single-leg external close | `reporting/logger.py:624` `log_external_close` | `unavailable` | `unavailable` | ⬜ |
| 13 | Spread external close | `engine/trader.py:3381` | `unavailable` | `unavailable` | ⬜ |

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

Goal: confirm writers fire correctly across codepaths that actually trigger in
two paper days. Not exhaustive — coverage gaps get caught in Phase 2 review.

- [ ] Bot recycles cleanly on Phase 1 branch (no schema migration errors)
- [ ] Spot-check 5+ entry rows: `slippage_benchmark_kind IS NOT NULL`
- [ ] Spot-check any exit rows: `slippage_signed_bps` matches legacy `realized_slippage_bps` exactly
- [ ] If any stop fires: confirm `stop_trigger_price IS NOT NULL`, `kind='active_stop_price'`
- [ ] If any spread fills: confirm long-leg row has NULL slippage, not `0.0`
- [ ] Dashboard still renders without errors (consumers still read legacy columns)

---

## Phase 2 + 4 — Consumer migration scope

Branch: `feature/slippage-unification-phase2`

- [ ] `strategies/health/assessor.py` reads `slippage_adverse_bps` (and filters by `measurement_quality`)
- [ ] `risk/manager.py` kill switch reads `slippage_adverse_bps`
- [ ] `scripts/calibrate_health_thresholds.py` reads `slippage_adverse_bps`
- [ ] `dashboard.py` Recent Trades displays `slippage_benchmark_kind` + `measurement_quality` alongside slippage bps
- [ ] `dashboard.py:710` denominator dilution fix — `IS NOT NULL` mask on slippage_denom
- [ ] Stop dual-writing `realized_slippage_bps` / `modeled_slippage_bps` on new rows (Phase 4 fold-in)
- [ ] Update tests that read legacy columns

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
