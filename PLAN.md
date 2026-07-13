# PLAN.md — Trading Bot Roadmap

> Purpose: keep the active roadmap, live-readiness gates, and unresolved follow-ups
> easy to scan. Historical implementation detail belongs in git history and the
> focused docs under `docs/`.

---

## Current Operating State

The bot is running in Alpaca paper mode as a five-sleeve portfolio:

| Sleeve | Strategy | Instruments | Status | Notes |
|---|---|---|---|---|
| Equity | SMA Crossover | Static SMA watchlist | Paper active | Trend-following; market entries; sector COLD warns |
| Equity | RSI Reversion | Static RSI watchlist | Paper active | Mean-reversion; limit entries; sector COLD blocks |
| Equity | Donchian Breakout | AI/big-tech watchlist | Paper active | Trend-continuation; TRENDING regime only |
| Isolated options | SPY Options Reversion | SPY calls | Paper active | Single-leg options; underlying-keyed ownership |
| Isolated options | Credit Spread | SPY + QQQ bull put spreads | Paper active | MLEG combos; UUID-keyed positions; SPY/QQQ share one sleeve |

Runtime posture:

- Launch method: local `tmux` via `./start_bot.sh` / `./recycle_bot.sh`
- Current live posture: **not live**, paper validation continues
- Current required live blockers: slippage drift calibration, final paper GO/NO-GO, live hard-cap config, VPS deployment hardening
- Current ownership model: `_positions: dict[position_id, Position]`
  - Equity and single-leg options use `owner_key_for(symbol)` as `position_id`
  - Spreads use UUID `position_id`
  - Single-leg + MLEG on the same underlying can coexist if OCC legs do not overlap
  - Two single-leg options strategies on the same underlying are still blocked until the single-leg option ownership model changes

Current allocation model:

| Pool | Share of deployable capital | Sleeves |
|---|---:|---|
| `equity` | 85% | SMA 40%, RSI 20%, Donchian 25% |
| `isolated_options` | 15% | SPY Options 5%, Credit Spread 10% |

---

## Live Readiness Gates

These are the items that must be green before any live flip.

| Gate | Status | Action |
|---|---|---|
| Combined five-sleeve paper run | 🔄 In progress | Continue collecting fills and operational evidence |
| Slippage calibration (`10.D1`) | ⏸ Needs evidence | Audit enough real paper fills; compare realized vs modeled slippage |
| Slippage drift enabled (`10.D2`) | ⬜ Blocked by calibration | Set `SLIPPAGE_DRIFT_ENABLED=True` only after calibration is sane |
| Strategy Health threshold watch (`11.10h`) | 🟢 Calendar gate met; nothing actionable to tune yet | 4 weekly reports (W21–W24) + May monthly accumulated. Sufficiency floors already recalibrated 2026-06-01 (commit `f5e301f`) from 30–50 down to 8–25. All L1/L2/L3 health checks HEALTHY across every report; all Edge verdicts UNDETERMINED for sample-size reasons, not threshold reasons — no false positives to tune. Run `scripts/calibrate_health_thresholds.py` periodically as a dry-run sanity check. Real follow-up is now §F15/§F16 (mark-to-market block on EdgeReport + total-P&L-aware recommendation prose), surfaced 2026-06-04. |
| RSI SPY50 band paper-watch (`11.23`) | 🔄 In progress | PR #70 relaxes RSI's hard SPY50 gate to a 1% band. Corrected SIP research with production sector COLD threshold (`score <= -2`) improved return materially but raised modeled 5y MaxDD from -18.4% to -30.4%; before live, verify paper behavior does not exceed the modeled drawdown envelope or produce clustered dip-buying in deteriorating markets. |
| Credit-spread paper-watch (`11.30`, `11.41`) | 🔄 In progress | Audit open/close attempts, close timeout behavior, and execution quality |
| SPY option trailing durability | 🔄 Diagnostic enabled, evidence collection in progress | PR #46 merged durable HWM, GTC protection, and atomic Alpaca replacements. PR #63 merged quote-quality hardening for price ratchets. The June 12/15 immediate-fill mechanism remains unresolved. PR #64 diagnostic (`OPTION_STOP_REPLACE_AUDIT_ENABLED=true`, owner-gated to `spy_options_reversion`) enabled in `config/.env` on 2026-06-19 and bot recycled — capturing broker/stream evidence to `data/diagnostics/option_stop_replace_audit.db` without changing trading behavior. This PR extends that same disposable diagnostic to initial option stop submits and stop-fill quote context so ordinary stop slippage can be explained too; replacement evidence remains captured as before. Dump tool: `scripts/dump_option_stop_audit.py`. Disable by flipping flag false, recycling, deleting the disposable DB. PR #71 (§10.4) split the trailing row's broker-state mirror onto the per-order substrate — every option stop submit / replace now writes a `role='protective_stop'` / `role='replacement_stop'` row, and the trailing FK (`option_trailing_stops.lifecycle_order_id`) is the canonical handle for broker identity / status. The PR #64 audit DB is untouched (separate DB, separate write path); audit reads continue as before. On the next ratchet cycle, expect new substrate rows for `spy_options_reversion` and trailing FK populated; verify both during paper bake. |
| Capped equity entry stop durability | 🔧 Alpaca contract fix in review | PR #47's original `ReplaceOrderRequest(time_in_force=GTC)` promotion assumption was disproven by Alpaca paper on 2026-07-09 (`time_in_force cannot be changed for advanced orders`) and checked against Alpaca docs/SDK the same day: OTO child replacement is not supported beyond documented price fields. Current fix branch preserves capped DAY LIMIT/STOP_LIMIT + OTO entry, records the attached child with its real DAY TIF, then rebuilds durable protection as cancel-child + standalone simple GTC stop after confirmed full fill / snapshot reconciliation. Awaiting PR review and next capped paper fill to confirm DAY child cancel → one standalone GTC protective stop end-state. |
| Single-leg exit fill durability | ✅ Shipped via PR #53 (2026-06-08); reconfirmed on substrate via PR #61 (2026-06-14) | CIEN's broker-accepted-late-fill incident is closed: `broker.close_position` writes `role='exit'` at submit ([execution/broker.py:2482](execution/broker.py:2482)), `_unknown_after_submit` preserves the broker order_id on REST timeouts ([execution/broker.py:2508](execution/broker.py:2508)), and `_maybe_dispatch_substrate_exit_fill` ([engine/trader.py:2513](engine/trader.py:2513)) recovers across WS / cycle / startup tiers gated by `_has_position`. The CIEN production row self-repaired via startup SELL-history reconciliation (7.59 @ $462.00, `broker_history_sell_recovered`, quality `unavailable`). Awaiting a post-PR-61 signal-driven equity or options close to exercise the substrate exit-role write end-to-end. |
| Live hard dollar cap (`10.G2`) | ⬜ Set at live flip | Configure launch-only cap in live `.env` |
| Preflight + dry run (`10.G5`) | ✅ Code complete | Re-run immediately before live flip |
| VPS deployment (`10.H1-H5`) | ⬜ Not started | Provision production runtime, systemd, secure env, log shipping |
| Final GO/NO-GO package | ⬜ Not started | Summarize paper evidence, operational stability, and open risks |

---

## Active Work Queue

### P0 — Live Blockers

| Item | Why It Matters | Acceptance |
|---|---|---|
| Slippage kill-switch calibration | Live trading must halt if execution quality drifts beyond modeled edge | Paper fill audit shows thresholds are reasonable; `SLIPPAGE_DRIFT_ENABLED=True` before live |
| Five-sleeve paper GO/NO-GO | The current bot is broader than the old SMA/RSI gate; evidence must cover all active sleeves | Documented GO/NO-GO report covering entries, exits, attribution, startup reconciliation, allocator behavior, health reports |
| VPS/systemd deployment | Local Mac + tmux is acceptable for paper, not for real capital | VPS provisioned, secrets deployed safely, `systemd` restarts bot on crash/boot, logs are recoverable |
| Live `.env` hard cap | Launch-only protection against sizing or order-loop bugs | `HARD_DOLLAR_LOSS_CAP` set conservatively for live launch and verified by preflight |
| Operator controls Phase A + B + C (`docs/operator_controls_proposal.md`) | Live trading needs precise, audited operator intervention rather than only the blunt stop/edit/restart options available today | Phase A (PR #33 + #41): `position_uid` lifecycle identity + `operator_commands` queue + sticky halt CLI ✅ shipped + baked ≥10 days. Phase B (PR #65): 4 soft-control actions, fast 5s heartbeat, Telegram `/halt` migrated to queue, `position_uid` in fill/exit alerts ✅ shipped + baked. Phase C (PR #66, merged 2026-06-16): `close-position` / `reduce-position` / `cancel-position-orders` with symbol-lock registry, `--confirm <position_uid_short>` typo-guard, allocator + PnL reintegration via existing `_record_realized_pnl` path, substrate row tagging (`origin_kind='operator'` + `operator_command_uid`) ✅ shipped, baking on paper. The §17 amendment confirmed obsolescence of the original Phase A "deferred items" table: `Position.position_uid` field, snapshot integration, `operator_command_uid`/`client_order_id` columns on `trades` — all superseded by the order-lifecycle foundation. Operator controls is feature-complete for v1; remaining work is paper-bake evidence before live. |
| Order lifecycle foundation (`docs/order_lifecycle_state_machine.md`) | The bot loses track of orders that don't fill within 240 seconds and recovery state is in-memory only. PR #58's eight-round review traced every bug back to this missing durable per-order substrate. Foundation absorbs `_suspect_orders`, `_suspect_exit_orders`, `LIFECYCLE_PENDING_GRACE_SECONDS`, stop-promotion identity workarounds, and option-trailing broker-state duplication into one substrate with atomic `apply_order_event` | Phase 1 substrate **merged** (PR #60, 2026-06-14, 24 commits). Phase 2 consumer wiring **landed** on `feat/order-lifecycle-consumer-wiring` (15 commits including 3 PR #61 review-fix commits): submit-time substrate writes for all four roles, four-tier capture pipeline (attach queue + WS + cycle reconcile + startup reconcile), substrate-driven entry/exit dispatch, `_suspect_orders` and `_suspect_exit_orders` caches deleted. Awaiting Monday's first fill to validate the full pipeline end-to-end. Discovery doc: [`docs/order_lifecycle_state_machine.md`](docs/order_lifecycle_state_machine.md) (PR #59). Tracker: [`docs/order_lifecycle_foundation_tracker.md`](docs/order_lifecycle_foundation_tracker.md). PR #58 (Donchian STOP_LIMIT, original 8-round attempt) was closed; the minimal-scope rewrite landed in PR #62 (`feat/donchian-stop-limit-v2`, 4 feature commits + R1 review fixes for substrate trigger persistence, short resting-order confirm timeout, worst-permitted-fill sizing, and STOP_LIMIT-aware backtest semantics). §10.4 (option trailing state split) shipped on `feat/option-trailing-state-split` (PR #71; 7 feature + 3 review-fix commits): options-side P-4/P-5 substrate writes in `submit_option_gtc_stop` / `replace_option_stop`, `option_trailing_stops.lifecycle_order_id` FK column populated at every trailing upsert via a preservation-contract resolver (preserves prior FK across transient store errors and when broker order_id is unchanged), read-path migrated through the FK join with `position_uid` + role guards, `client_order_id` fallback opportunistically recovers from `attach_broker_order_id` failures, load-bearing-miss after broker accept emits CRITICAL, denormalized mirror columns retained during migration. Strict column removal + Phase 2 consumer migration (dashboard / health monitor) deferred to follow-up cleanup PRs. |

### P1 — Paper-Watch And Calibration

| Item | Why It Matters | Acceptance |
|---|---|---|
| Slippage model unification review (`10.D1` support) | Calibration is not trustworthy if execution slippage, implementation shortfall, stop-gap erosion, and recovery rows share one ambiguous data contract | Review and approve [`docs/slippage_unification_design.md`](docs/slippage_unification_design.md) before changing slippage persistence or dashboard semantics. **Implementation tracker:** [`docs/slippage_unification_tracker.md`](docs/slippage_unification_tracker.md) — Phase 1 merged (`bf16b5a`); Phase 2 + 4 merged on `feature/slippage-unification-phase2` (consumer migration across `strategies/health/assessor.py`, `risk/manager.py`, `scripts/calibrate_health_thresholds.py`, `dashboard.py`, `reporting/pnl.py` + legacy `realized_slippage_bps`/`modeled_slippage_bps` dual-write removed + dashboard denominator-dilution fix + Phase 1 divergence reconciled); Phase 3 (historical row cleanup) pending. |
| `11.10h` Strategy Health paper-watch | Health/Edge monitor is advisory but noisy thresholds can create operator fatigue | **Status 2026-06-18**: ≥4 weeks of reports landed (W21–W24 + monthly_2026-05). Sufficiency floors recalibrated 2026-06-01 (commit `f5e301f`) from 30–50 down to 8–25 — explicit operator trade-off for verdicts reachable within ~6 months of operation. Sample counts still below floor on every sleeve (high-water: donchian 6 of 25 in May, 3 of 25 in W24). All L1/L2/L3 checks HEALTHY across every report — no operator fatigue surfaced, no false positives to tune. Real follow-up reframed to §F15/§F16 (mark-to-market block on EdgeReport + total-P&L-aware recommendation prose) — driven by donchian_breakout reading R=-0.84 in May while open positions held large unrealized winners. Active work: (a) run `scripts/calibrate_health_thresholds.py` as a dry-run sanity check (now reads `slippage_adverse_bps` with quality whitelist post-PR #67), confirm no actionable diffs against current data; (b) watch for first NEGATIVE Edge verdict to inform threshold tuning. The partial-exit trade-count fix promised "once `position_uid` ships" was reframed in proposal §17.2 as organic-adoption-when-next-touched and is no longer scheduled here. **Operator runbook:** envelopes are gitignored — each machine must run `scripts/build_envelopes.py` once to materialize `data/envelopes/{sma_crossover,rsi_reversion,donchian_breakout}.json` (offline backtest source). For `spy_options_reversion` / `credit_spread`, use `scripts/build_paper_envelope.py --strategy <name> --weeks N` after ≥10 closed trades have accumulated. All five envelope files are present on the current machine. |
| `11.23` RSI SPY50 band paper-watch | The 1% SPY50 tolerance deliberately trades higher opportunity capture for higher drawdown risk | Track RSI candidate count, fills, clustered entries, and realized/open drawdown after PR #70. Acceptance before live: no evidence that the relaxed gate is producing clustered dip-buying in deteriorating markets, and paper RSI drawdown remains inside the corrected production-threshold modeled -30.4% 5y SIP MaxDD envelope. |
| `11.26` SPY options picker audit | The 10% fatal-spread threshold is paper-tested, not proven | 10-20 fills audited for spread distribution, fill rate, and realized slippage |
| `11.30` Credit-spread paper-watch | Short-premium strategy needs real fill and risk evidence before live | 20-30 completed cycles/attempts audited; IV floor, credit floor, and risk assumptions either confirmed or adjusted |
| `11.34` Credit-spread exit paper-watch | Exit triggers and paper close reliability need evidence | Exit reasons, close attempts, retry counts, and realized close prices reviewed |
| `11.41` Credit-spread close execution tuning | Mid-price closes have timed out and retried for hours in paper | Decide between unchanged, more marketable debit, or staged retry ladder based on actual close-attempt data |
| `11.46b` SPY options IV-Rank gate | ✅ Graduated observation → gate (`feat/spy-options-vix-percentile-gate`) | The 11.46b VIX observation logs + a production-mirrored backtest (`backtest/spy_options_backtest.py`, 2018–2025) + the live paper sample (6/6) all agree: the oversold-bounce edge splits by regime × implied vol. Toxic quadrant = TRENDING + complacent VIX (backtest −104%/8 trades; all 3 live losers). `SPYOptionsEdgeFilter` now enforces a VIX **≤-percentile** floor (`SPY_OPTIONS_MIN_VIX_PERCENTILE`=0.60, NOT the min-max rank) **only in TRENDING**; RANGING is a mean-reversion regime and trades on SPY>100SMA alone. Regime is detected at the engine and passed to the filter via `set_regime` (regime stays out of the filter's own logic). Backtest: baseline +400%/pf1.69 → gated +504%/pf2.33. **Paper-watch before live:** confirm the gate blocks low-VIX TRENDING entries and passes elevated-VIX ones on live paper; watch for any RANGING entries (untested live). |
| `11.46c` Credit-spread IV-Rank follow-up | IV Rank may also improve credit-spread filtering, but only after observation data exists | Run IVR observation/audit for credit_spread first; only gate trades if evidence supports it |
| `11.48` Allocator risk-target reconciliation | 2026-07 sizing audit: `MAX_POSITION_PCT=2%` is unreachable under the per-position concentration caps, so the caps size ~every equity entry — per-trade risk ranged $55–$1,900 (29×), dollar risk scales with ATR% (inverse risk parity), and freed capital goes to whichever signal is scanned first (ARM $7.4k vs MRVL $570 one minute apart). The four-control architecture from `docs/capital_allocation_reference.md` §3.4 is sound; the risk parameter went stale when the caps landed (`dc65435`, `d9e1c72`) | **PROPOSED** — review [`docs/allocator_risk_target_reconciliation.md`](docs/allocator_risk_target_reconciliation.md) and sign off on the per-strategy risk targets (§6: donchian 0.40% / sma 0.60% / rsi 0.25%, derived from measured watchlist ATR% coverage). Then one PR per §7 (per-strategy `risk_per_trade_pct` + binding-order tests + cap-clip visibility logging). Paper acceptance per §8: first 6 entries within ±10% of target risk, no sleeve-starvation regression (v1 failure mode). Prerequisite for trustworthy Kelly inputs (11.9) |
| `11.47` SPY options secondary levers (parked) | Backtest hints the hard SL and trailing stop may be suboptimal, but they are second-order vs the VIX gate | SL sweep is ~P&L-neutral 20–50% in the daily-BS model (which can't see intraday premium noise that trips the live 25% stop); winners ride to time-stop while the trailing stop exits mediocre trades early. Re-open with: widen SL to ~35% and/or loosen the trail, tested on a fill-aware model or ≥10 more live closes. Reproduce via `backtest/spy_options_backtest.py` stop-loss sweep. |

### P2 — Future Enhancements

| Item | Why It Matters | Acceptance |
|---|---|---|
| Dynamic watchlists (`11.1`) | Static universes are operationally simple; dynamic rotation needs durable ownership proven first | Dynamic source supports refresh cadence and never abandons open positions |
| Calibrated sector caps (`11.8`) | Sector exposure is observable; caps should be data-driven, not blanket | Add targeted caps only if paper exposure shows a real concentration problem |
| Dynamic strategy allocation (`11.9`) | Could improve capital efficiency once each sleeve has enough live/paper evidence | Weight suggestions based on expectancy/Sharpe with operator approval. *When implemented, key `SleeveAllocator` reserve/release on `(strategy, position_uid)` per `docs/operator_controls_proposal.md` §17.* |
| Defensive cash sweep (`11.45`) | Idle capital during prolonged BEAR/VOLATILE regimes loses purchasing power | SGOV/BIL-style posture only after strict prolonged-BEAR confirmation and recovery state machine |
| Same-underlying single-leg options ownership | Needed before adding a second single-leg options strategy on SPY/QQQ | Single-leg options can use OCC/UUID position ids without breaking exits, DB restore, allocator, dashboard |
| ~~Operator controls Phase B (soft controls + alerts integration)~~ ✅ SHIPPED — moved to P0 row above. | — | — |
| ~~Operator controls Phase C (destructive controls)~~ ✅ SHIPPED — moved to the P0 row above. The original "Phase A deferrals" sub-list (`Position.position_uid` field, in-memory startup re-attach, `operator_command_uid`/`client_order_id` columns on `trades`, `position_uid` in `engine_state.json`) was retired via the §17 amendment when the order-lifecycle foundation absorbed each one. The columns live on `position_lifecycle_orders` instead of `trades`; `apply_order_event` carries `position_uid` through substrate so the in-memory `Position` field is obsolete. | — | — |
| Operator controls dashboard integration | Surface `position_uid` in the dashboard's open-positions table by reading the per-order substrate directly. Independent of operator controls Phase A/B/C, which have shipped. | Per `docs/operator_controls_proposal.md` §17.2. Reads from `position_lifecycle_orders` rather than `engine_state.json`. |
| Backtest reconcile by lifecycle ID | Join paper vs backtest by synthesized lifecycle ID once `position_uid` has ≥4 weeks of paper data | Per `docs/operator_controls_proposal.md` §17. Refactor `backtest/reconcile.py`. |
| `trades.parent_position_uid` column | For rolls/derived positions | Add when the rolls/derived-positions feature is designed — bundle into that PR. Per `docs/operator_controls_proposal.md` §8. |
| **PR #71 follow-up: option trailing mirror column removal** | The §10.4 split kept `option_trailing_stops.alpaca_stop_order_id` / `stop_order_status` as denormalized mirrors so legacy rows and load-bearing read paths (`_recent_option_stop_submit_pending`, `_option_trailing_authoritative_identity`) have a fallback while substrate adoption proves out. Removing the columns is the cleanup that finishes the migration; nothing operational depends on it. | After ≥2 weeks of paper bake on PR #71 confirms the FK is populated on every ratchet and the substrate-authoritative read path never falls back to the mirror in normal operation: (a) audit `option_trailing_stops` for any rows where `lifecycle_order_id IS NULL` and a live broker stop exists; (b) drop the two columns + `_option_trailing_authoritative_identity` fallback branches; (c) remove `OptionTrailingStopRow.alpaca_stop_order_id` / `stop_order_status` fields and update callers; (d) PR #69's `_cleanup_option_trailing_state` helper stays — its scope was already shifted to "drop strategy state" in PR #71. |
| **PR #71 follow-up: Phase 2 consumer migration of trailing-stop reads** | Downstream consumers (dashboard, health monitor, allocator, reporting) currently read `option_trailing_stops` via `get_by_occ` and the mirror columns. Per `docs/operator_controls_proposal.md` §17.2, they should read substrate-authoritative identity through `OptionTrailingStopStore.get_by_occ_joined` (returns `JoinedOptionTrailingRow`) when next touched. | Organic adoption: each consumer migrates when next revised. Not a horizontal refactor PR. Tracked here so a future agent touching any of those consumers knows the right read API; blocks "mirror column removal" above only if a consumer can't be migrated. |
| **Stop-fill legacy fallback removal** | PR #76 makes the order-lifecycle substrate the primary handler for `protective_stop` / `replacement_stop` fills and leaves `_process_stream_stop_fills()` only as a logged compatibility fallback for orders with no substrate row. We do not want that bridge to become permanent. | After 1-2 weeks of paper operation show zero "legacy stop-fill fallback handling" logs and all active stop paths (fractional whole-share stops, repaired equity stops, DAY-child cancel + standalone GTC rebuilds, option GTC/trailing stops) have substrate rows, remove the legacy `_stop_fills` queue / fallback branch and update tests/docs to treat substrate stop dispatch as the only normal path. |
| ~~**MLEG partial-close residual reconciliation**~~ ✅ SHIPPED on `feat/mleg-partial-close-residual` (PR #72, merged 2026-06-20; 5 feature + 2 review-fix + 1 whitespace = 8 commits). Per the §10.7 load-bearing constraint, option (b) — spread closes write into `position_lifecycle_orders` under their `position_uid`, and pending-close state is derived from non-terminal rows. The substrate's `uniq_one_active_close_per_position` partial unique index is the durable, restart-safe duplicate-dispatch block; `_spreads_pending_close` retired. Partial fills insert a `partial_close` placeholder row that survives restart (the actual gap closure). Cycle + startup reconcilers walk non-terminal close rows and advance any whose broker order resolved during downtime. Review fix R1 (`a5f8352`) added per-submit eager attach via `SpreadExecutionWorker.on_submitted` queued through the broker; review fix R2 (`991d5dc`) added crash-durable substrate write from the worker thread via its own sqlite3 connection — closes the one-cycle-interval crash window the queue alone left open. Worker-side `_watch_to_terminal` partial-fill behavior kept as WAIT (no cancel-and-retry) per evidence: zero historical spread partial-fill events on paper. PR #56 R6 fail-safe behaviors all preserved. Two PR #72 follow-up rows below. | — |
| **PR #72 follow-up: spread `entry_primary` per-order substrate rows** | C1 wrote the parent `position_lifecycle` row for spreads + a startup backfill, but spread entries do NOT write `position_lifecycle_orders` rows (deferred per PR body). Today the spread path stamps `current_qty` directly via `mark_open` instead of rolling it up from order rows via the position-status CTE — the CTE doesn't currently apply to spreads. Mostly cosmetic for v1 (the close-path substrate, which carries the restart-gap closure, doesn't need entry rows to FK against). Becomes load-bearing only if a future consumer (operator command audit, multi-leg P&L attribution) wants the spread entry to flow through the same `apply_order_event` pipeline as single-leg. 2026-06-26 note: spread/MLEG close rows now use `apply_order_event` for lifecycle state without auto-writing a fake `position_type='single_leg'` trade row; user-facing spread economics remain owned by `log_spread_fill(position_type='spread')`. | Wire `_lifecycle_orders_insert_pending(role='entry_primary')` into `_enter_multi_leg` + `_drain_spread_fills` open branch; thread the engine-generated combo cloid through `dispatch_spread_order` → `SpreadExecutionWorker` as the broker cloid (same eager-attach + durable-write plumbing C10 added for closes). Reuse the spread-safe `apply_order_event` boundary so the position-status CTE can run without colliding with the single-leg `_TRADES_UPSERT_SQL` predicate. Tests covering normal fill, partial entry, and cancel paths. |
| **PR #72 follow-up: operator command to clear stuck `partial_close` placeholder** | After a partial close, the engine inserts a non-terminal `partial_close` row to keep the position locked against duplicate dispatch. Per the §10.7 WAIT decision the engine does NOT auto-progress this row — operator confirms the broker resolved the residual and clears the placeholder. Today that means manual SQL (`UPDATE position_lifecycle_orders SET status='canceled', terminal_at=... WHERE client_order_id=... AND role='partial_close'`). Fragile; not exercised on paper because zero partial fills have fired. Becomes load-bearing the first time a partial actually fires in production. | New operator CLI / queue command `clear-spread-placeholder <position_uid_short> --confirm <prefix>` that idempotently marks any non-terminal `partial_close` row for the position as `canceled`, logs to the operator command queue (`origin_kind='operator'`, `operator_command_uid` populated), and refreshes the engine's in-memory state. Symbol-lock registry already exists from Phase C. Acceptance: a partial-fill simulated test followed by the operator command leaves the position unlocked AND the substrate row terminal. |

---

## Completed Milestones

| Area | Current State |
|---|---|
| Environment and config | Python 3.12 project, `alpaca-py`, `config/.env`, paper/live credential separation |
| Data layer | Alpaca historical bars, Parquet cache, validation, freshness checks, retry/backoff |
| Indicators | Hand-rolled SMA, EMA, ATR, RSI, ADX |
| Strategy framework | `BaseStrategy`, `SignalFrame`, `StrategySlot`, `WatchlistSource`, structured edge-filter decisions |
| Backtesting | vectorbt runner, slippage/commission modeling, look-ahead-safe next-open execution, reconciliation tooling |
| Risk manager | Position sizing, ATR stops, prior-close daily/hard-dollar halts that re-engage after same-day recycle and recompute after broker baseline rollover, universal entry-only submit guards, loss streak cooldown, broker-error and slippage kill switches |
| Broker execution | Alpaca wrapper, market/limit/OTO/fractional paths, option worker, MLEG worker, stream-first fills |
| Engine | Restart-safe cycle, startup reconciliation, external-close detection, state snapshot, per-strategy slots |
| Reporting | SQLite trade log, PnL summaries, alerts, dashboard (now with monthly health report tabs), Strategy Health & Edge reports |
| Allocator | 85/15 pool model, per-strategy sleeves, stretch borrowing for equity only, HWM drawdown gate |
| Regime and sector context | BEAR/VOLATILE/TRENDING/RANGING detector with ATR% floor; sector resolver/gauge and strategy-specific policies |
| Active strategy set | SMA, RSI, Donchian, SPY Options, Credit Spread SPY/QQQ |
| Position abstraction | Single-leg and spread `Position` model with UUID spread ownership and exact-OCC conflict guard |

---

## Completed Phase Summary

| Phase | Summary | Status |
|---|---|---|
| 1 | Environment setup and Alpaca paper connectivity | ✅ Complete |
| 2 | Market data pipeline, cache, freshness, validation | ✅ Complete |
| 3 | Technical indicator library | ✅ Complete |
| 4 | Strategy framework and SMA baseline | ✅ Complete |
| 5 | Backtesting and reconciliation harness | ✅ Complete |
| 6 | Risk management gate | ✅ Complete |
| 7 | Broker integration and order execution | ✅ Complete |
| 8 | Trading engine main loop | ✅ Complete |
| 9 | Trade reporting, PnL, alerts | ✅ Complete |
| 9.5 | Forward-test infrastructure | ✅ Complete, folded into Phase 10 paper gate |
| 10 | Pre-live stabilization | 🔄 Mostly code-complete; live blockers remain |
| 11 | Advanced portfolio/options enhancements | 🔄 In progress; many items are paper-watch follow-ups |

---

## Durable Design Decisions

- Paper first, live only after evidence.
- Broker state is source of truth; startup reconciliation is mandatory.
- Strategies are pure signal generators; execution and risk live outside strategies.
- Single-leg trades pass through `RiskManager.evaluate`; defined-risk MLEG spreads use strategy max-loss sizing plus sleeve gates.
- Exits are never blocked by edge filters, regime gates, or sleeve capacity.
- Mandatory macro SMA entry gates fail closed when history is insufficient;
  an unavailable indicator must never silently weaken an entry policy.
- New filters should return `EdgeFilterDecision`, not only boolean Series.
- Options and spreads must use broker-supported Alpaca SDK paths; avoid home-grown execution behavior when the SDK provides a native route.
- Slippage metrics must separate execution quality, implementation shortfall, and stop-gap erosion; no operator-facing slippage number should exist without an explicit stored benchmark contract. See `docs/slippage_unification_design.md`.
- Realized P&L and restart state use broker fill cost basis, never the strategy's decision/reference price; reference prices remain execution-quality and signal-analysis metadata.
- Do not add a second same-underlying single-leg options strategy until the ownership model supports it.
- Do not tune paper-watch parameters pre-emptively; audit first, change second.
- `position_uid` is project-wide lifecycle identity — generated by `engine.lifecycle.new_position_uid()` before broker submission, persisted in the `position_lifecycle` table. Phase A wires it through the operator CLI only; other subsystems (health monitor, allocator, reconcile, alerts, dashboard) integrate later, each in its own PR, per `docs/operator_controls_proposal.md` §17.

---

## Deferred Or Parked Ideas

These are intentionally not active roadmap items unless promoted with fresh evidence.

| Idea | Current Disposition |
|---|---|
| Bollinger Squeeze strategy | Implemented but parked; low backtest Sharpe and better suited to options/multi-timeframe overlays |
| Blanket sector caps | Rejected for now; would paralyze tech/semis-heavy strategy universes |
| VIX as primary regime detector | Superseded by ATR% floor audit; VIX remains useful for options context |
| Per-symbol cooldown after loss | Audit first; blanket cross-strategy cooldown likely conflicts with Donchian/SMA semantics |
| SMA RSI-overbought gate | Audit first; trend-following entries may naturally occur at high RSI |
| RSI SPY-50 smoothing | Reframed by PR #70: hard gate relaxed to a 1% band. Further smoothing/removal is parked until paper-watch evidence shows the band is still over-protective. |
| Alpaca trailing stops, VWAP/TWAP, extended hours | Not relevant at current strategy cadence/size; revisit only if a strategy needs them |
| Donchian trailing broker stop | **CLOSED 2026-06-08 — static stop retained.** Question: should the broker-side ATR stop trail the trade up, instead of sitting at `entry − 2×ATR` forever? Worry: a gap-down past the rising 15-day-low exits at the original stop and gives back all accrued profit. **Tested on SIP across 2016-04 → 2024-12** (21+ symbols, ai_bigtech, production gates ON, 536 trades on the combined window) including both catastrophic-gap regimes that motivated the PLAN concern: 2018 Q4 vol shock and March 2020 COVID crash. **Findings**: on the combined window, static +18.4% / Shp +0.28 / MaxDD −14.3%; Donchian-low trail +17.3% / +0.27 / −13.8%; chandelier +15.7% / +0.27 / −12.9%. Variants identical to two decimals on Sharpe. **Crash-exposed subset (16 trades March 2020)**: chandelier mean R −0.16 vs static +1.03 — chandelier UNDER-performs the static stop on the exact scenarios the PLAN concern named; Donchian-low trail +1.14 within noise. **2018 Q4 vol shock under-samples** (6 trades) because the SPY TRENDING-only regime gate kept the strategy out of BEAR Q4 — production would also have done this. **Verdict**: neither trail variant clears the bar in any reachable regime. The catastrophic-gap fear was real but rare and the strategy's signal exit catches most trend failures one bar later. Full writeup: [docs/donchian_trail_investigation.md](docs/donchian_trail_investigation.md). Simulator: [backtest/donchian_trail_sim.py](backtest/donchian_trail_sim.py). Reproduce: `venv/bin/python scripts/donchian_trail_compare.py`. **Re-open only with**: a documented live giveback event showing the static stop materially surrendered P&L on a specific gap-down through a then-vestigial level (the aggregate evidence here would need to be over-ridden by a real case study, not a hypothetical). |
