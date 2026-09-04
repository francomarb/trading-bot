# PLAN.md — Trading Bot Roadmap

> Purpose: keep the active roadmap, live-readiness gates, and unresolved follow-ups
> easy to scan. Historical implementation detail belongs in git history and the
> focused docs under `docs/`.

---

## Current Operating State

The bot is running in Alpaca paper mode as a six-sleeve portfolio:

| Sleeve | Strategy | Instruments | Status | Notes |
|---|---|---|---|---|
| Equity | SMA Crossover | Static SMA watchlist | Paper active | Trend-following; market entries; sector COLD warns |
| Equity | RSI Reversion | Static RSI watchlist | Paper active | RSI3 quick-exit reset; limit entries; stock SMA200/liquidity gates |
| Equity | Donchian Breakout | AI/big-tech watchlist | Paper active | Trend-continuation; TRENDING/RANGING/VOLATILE, BEAR blocked |
| Equity | Leveraged Trend | SPXL, TQQQ, TECL, SOXL | Paper active | Four slots; signal exits |
| Isolated options | SPY Options Reversion | SPY calls | Paper active | Single-leg options; underlying-keyed ownership |
| Isolated options | Credit Spread | SPY + QQQ bull put spreads | Paper active | MLEG combos; UUID-keyed positions; SPY/QQQ share one sleeve |

Runtime posture:

- Launch method: local `tmux` via `./start_bot.sh` / `./recycle_bot.sh`
- Current execution mode: **not live**, paper validation continues
- Current live posture: strategies graduate individually from paper development; none is preselected for live use
- Before an actual live launch: at least one strategy must be operator-approved, slippage calibration must pass, the live-size throttle must be verified, and deployment hardening must be complete
- VPS work is intentionally deferred until the operator is satisfied that one or more strategies merit live consideration
- Current ownership model: `_positions: dict[position_id, Position]`
  - Equity and single-leg options use `owner_key_for(symbol)` as `position_id`
  - Spreads use UUID `position_id`
  - Single-leg + MLEG on the same underlying can coexist if OCC legs do not overlap
  - Two single-leg options strategies on the same underlying are still blocked until the single-leg option ownership model changes

Current allocation model:

| Pool | Share of deployable capital | Sleeves |
|---|---:|---|
| `equity` | 85% | SMA 30%, RSI 15%, Donchian 15%, Leveraged Trend 25% |
| `isolated_options` | 15% | SPY Options 5%, Credit Spread 10% |

---

## Live Readiness Gates

Paper evidence is assessed per strategy. One strategy's weak or incomplete evidence
does not block another strategy from graduating, and meeting the criteria makes a
strategy eligible for operator review rather than automatically authorizing it for live use.

### Strategy Graduation Criteria

A strategy may be considered for live inclusion only when:

- It has a meaningful paper sample for its trading frequency, including completed entries and exits over enough time to avoid judging a short favorable streak.
- Net results and expectancy remain positive after realistic fees and slippage across meaningful subperiods, and profitability is not dominated by one exceptional winner.
- Drawdown, loss streaks, and capital usage stay within the strategy's documented risk limits.
- Evidence covers more than one market condition where practical, or clearly states which conditions remain untested.
- Entries, exits, protective orders, attribution, accounting, restart recovery, and operator controls work reliably for that strategy's instruments.
- The operator reviews the evidence and explicitly approves inclusion. Graduation is a decision, not an automatic threshold crossing.

Strategies that have not graduated remain in paper development and continue collecting
evidence or receiving reviewed improvements; they do not prevent an approved strategy
from proceeding.

| Gate | Status | Action |
|---|---|---|
| Per-strategy paper graduation | 🔄 In progress | Collect and review each strategy's evidence against the graduation criteria above. No strategy is preselected, and the portfolio does not require every strategy to pass together. |
| Slippage calibration (`10.D1`) | 🔄 2 of 10 qualifying fills | The clean post-guard fills are 1.85 and 2.16 adverse bps. Keep collecting; review thresholds after at least 10 execution-quality MARKET fills. |
| Slippage drift enabled (`10.D2`) | ⬜ Blocked by calibration | Set `SLIPPAGE_DRIFT_ENABLED=True` only after calibration is sane. Pre-fix this would have halted the bot on ~the 10th market fill at 13× the threshold for reasons unrelated to execution quality. The sample pool now also survives restarts (`RiskManager.seed_slippage_samples`, seeded at engine start) — before, `_slippage_samples` reset to empty every restart and could rarely reach `SLIPPAGE_DRIFT_MIN_SAMPLES=10` in one process lifetime. |
| Strategy Health threshold watch (`11.10h`) | ✅ Closed — no tuning | The watch produced no false alarms and no defensible threshold change. Continue routine scheduled reports. |
| RSI3 simplification paper-watch (`11.23`) | 🔄 6 entries / 4 completed exits | The reset is producing trades, but four outcomes are too few to judge expectancy or clustered dip-buying. |
| Credit-spread paper-watch (`11.30`, `11.34`, `11.41`) | ✅ Closed — operation proven, profitability weak | Entry and exit mechanics work. Further optimization waits for `11.63` bounded-entry-walk evidence. |
| SPY option trailing durability | ✅ Closed — incident not reproduced after hardening | The temporary diagnostic was retired 2026-09-04 after capturing 23 replacement decisions/results, 22 matching stream replacement events, two stop-fill contexts, and no diagnostic failure records. Reopen only on a new incident. |
| Capped equity entry stop durability | ✅ **CLOSED 2026-08-14.** Seven capped fills exercised durable DAY-child-to-GTC-stop rebuilding, including one real failed rebuild that alerted and was repaired without duplicate sell exposure. | Closed; reopen only on a new durability incident. Detailed forensics remain in `docs/deferred_followups.md`. |
| Single-leg exit fill durability | ✅ Reconfirmed end to end | Post-PR #61 substrate exit rows now include filled SMA, RSI, and SPY-options closes. The former “awaiting a signal-driven close” note is obsolete. |
| Live launch throttle (`10.G2`) | ⬜ Set at live flip | The flat `HARD_DOLLAR_LOSS_CAP` was retired 2026-09-01 (tripped on ordinary market noise once the account grew; did not scale). Account drawdown is owned by `MAX_DAILY_LOSS_PCT` (5%, scales); the launch-only "start tiny" gate is now `LIVE_SIZE_MULTIPLIER` ≤ 0.25, verified by preflight. |
| Preflight + dry run (`10.G5`) | ✅ Code complete | Re-run immediately before live flip |
| VPS deployment (`10.H1-H5`) | ⏸ Deferred by operator | Resume only after the operator is satisfied that at least one strategy merits live consideration. Then provision the production runtime, systemd, secure env, and log shipping. |
| Strategy graduation evidence package | ⬜ Tooling not started | Build the reviewed per-strategy report described below; it informs an operator decision and never approves a strategy automatically. |

---

## Active Work Queue

### Current Priority View — 2026-09-04

| Priority | Item | Current State | Next Action |
|---:|---|---|---|
| 1 | Trustworthy strategy graduation report | Contract and implementation are not started | Design the per-strategy report from authoritative lifecycle/P&L data, then implement it in a reviewed PR |
| 2 | `11.62` portfolio heat ceiling | Open design problem; no portfolio-wide initial-risk ceiling exists | Audit interactions with per-sleeve `11.60`, then propose a simple policy before implementation |

Evidence still collecting: slippage calibration **2/10**; RSI3 **6 entries / 4 completed exits**; credit-spread bounded entry walk **3/~20 attempts**; Donchian heat-cap observation **1 would-block event**; leveraged trend **4 open entries / 0 exits**. `11.41a` and `11.54a` remain event-gated and require no work until their trigger occurs. VPS and live-flip tasks remain deferred by operator decision.

### P0 — Live Blockers

| Item | Why It Matters | Acceptance |
|---|---|---|
| Slippage kill-switch calibration | Live trading must halt if execution quality drifts beyond modeled edge | Paper fill audit shows thresholds are reasonable; `SLIPPAGE_DRIFT_ENABLED=True` before live |
| Trustworthy strategy graduation report | The removed April-era checker assumed FIFO long-only fills, mixed every strategy, and produced one automatic verdict from raw dollar P&L. That model cannot represent the current bot. | Build a per-strategy, configuration-epoch report from authoritative realized-P&L events. Cover single-leg, MLEG, and partial closes; fees/slippage; normalized return or R; drawdown; sample/span; subperiod consistency; outlier dependence; and operational evidence. Report facts and uncertainty without automatic approval; the operator makes the decision. Wire the approved strategy set into preflight only after this contract is reviewed. |
| VPS/systemd deployment | **Deferred until at least one strategy merits live consideration.** Local Mac + tmux remains the paper-development environment. | After the operator authorizes this work: VPS provisioned, secrets deployed safely, `systemd` restarts bot on crash/boot, logs are recoverable |
| Live `.env` launch throttle | Launch-only protection: start sizes small | `LIVE_SIZE_MULTIPLIER` ≤ 0.25 verified by preflight (replaced the retired `HARD_DOLLAR_LOSS_CAP`). Malfunction is caught by the broker-error-streak and slippage-drift kill switches, not a flat dollar floor. |
| ~~Operator controls Phase A + B + C (`docs/operator_controls_proposal.md`)~~ | ✅ **PAPER-VALIDATED 2026-09-02.** Pause/resume, cancel, full close, exact-share equity reduce, exact-contract single-leg option reduce, residual GTC protection, durable P&L, and restart recovery all passed. The full-close defect found during the drill was fixed in PR #137; its repaired row and both reductions restored exactly once after recycle with NORMAL startup. | Closed. Genuine unexpected-protection latch clearing remains unit-tested and should be exercised operationally only when a real latch occurs; do not manufacture unsafe broker state for evidence. |
| Order lifecycle foundation (`docs/order_lifecycle_state_machine.md`) | ✅ **Shipped and exercised.** Durable per-order state, submit-time writes, stream/cycle/startup reconciliation, and substrate-driven dispatch are active. Filled substrate exits now exist for SMA, RSI, SPY options, Donchian, and credit spreads. | Foundation evidence is complete; track optional cleanup in its own follow-ups. |

### P1 — Paper-Watch And Calibration

| Item | Why It Matters | Acceptance |
|---|---|---|
| ~~`11.26` evidence-integrity corrections~~ | ✅ **FIXED in PR #146.** Terminal outcomes retain their broker status, cumulative partial fills are preserved, and lifecycle P&L refreshes after stop accounting with startup repair for stale parents. | Closed. Corrected evidence was used for the picker verdict below. |
| ~~Operator reduce degraded-accounting parity~~ | ✅ **FIXED in PR #141.** A filled reduction with missing durable realized P&L reports accounting failure without booking an in-memory estimate into the allocator. | Closed. Durable-write and missing-P&L parity tests pass; a lifecycle projection failure still permits allocator accounting when durable P&L exists. |
| ~~Operator command expiry margin~~ | ✅ **FIXED in PR #142.** Heartbeats keep their short freshness window; engine-thread commands get 15 minutes after submission to reach a safe cycle boundary. | Closed. Expiration is scoped to the caller's action lane and covered by timing tests. |
| Slippage model unification review (`10.D1` support) | ✅ **COMPLETE.** Execution quality, implementation shortfall, and stop-gap erosion have separate contracts. Versioning excludes incompatible arrival-midpoint history from every current consumer. | Phases 1, 2, and 4 shipped. **Phase 3 cleanup closed as unnecessary 2026-09-04:** only 3 legacy rows match its predicates, while all 17 unversioned arrival rows are already excluded from calibration, kill switch, health, P&L, dashboard statistics, and reconciliation. Preserve the raw audit history; do not mutate `trades.db`. See [`docs/slippage_unification_tracker.md`](docs/slippage_unification_tracker.md). |
| ~~`11.10h` Strategy Health paper-watch~~ | ✅ **CLOSED — no tuning.** The calendar gate and calibration dry run found no false alarms or defensible threshold change. | Keep routine reports; reopen only on a real false positive or a sufficiently sampled negative verdict. |
| `11.23` RSI3 simplification paper-watch | 🔄 **6 entries / 4 completed exits since the reset.** The strategy is no longer starved, but the sample is too small to judge expectancy, drawdown, or clustered dip-buying. | Keep collecting; do not restore removed filters from four outcomes. |
| ~~`11.26` SPY options picker audit~~ | ✅ **CLOSED — picker accepted; hard spread ceiling tightened to 6%.** The current-picker cohort produced 11/14 fills (78.6%); picked spreads were 0.55% p50 / 1.27% p95, no fill exceeded 5%, and worst adverse entry drift was 3.5 bps. Contracts stayed within 17–24 DTE and near the 0.5%-ITM target. | Keep the scoring weights and 180-second timeout unchanged. Strategy profitability remains a separate graduation decision. |
| ~~`11.30` Credit-spread paper-watch~~ | ✅ **CLOSED — mechanically operational, not profitable in the observed sample.** Fourteen completed cycles and at least 49 historical attempts satisfy the evidence gate. | Do not reopen broad tuning. Collect bounded-entry-walk evidence under `11.63` before considering another optimization. |
| ~~`11.34` Credit-spread exit paper-watch~~ | ✅ **CLOSED — exit reliability accepted.** Fourteen cycles closed successfully. Older typed reasons are unavailable after normal log rotation. | Keep the current exit behavior; do not fabricate missing reasons or duplicate them into `trades.reason`. |
| ~~`11.41` Credit-spread close execution tuning~~ | ✅ **CLOSED — current limit walk retained.** Every observed close resolved on a limit rung and no market fallback fired. | Reopen only on a concrete close failure or a market-fallback event. |
| `11.41a` Arrival-midpoint capture for MLEG market-fallback closes | ⏸ **0 market fallbacks observed.** There is no evidence set to analyze. | Decide separately whether to add best-effort, non-gating midpoint capture before the first event; do not pool future market fills with limit-fill calibration. |
| `11.63` Credit-spread entry bounded walk | 🔄 **3 post-change attempts: 1 fill, 2 cancels.** The target is about 20 attempts; the pre-change baseline was 13 fills in 49 attempts (27%). | Keep collecting clamp and fill evidence. Do not tune the credit floor from three attempts. |
| ~~`11.46b` SPY options VIX-percentile gate~~ | ✅ **PAPER-VALIDATED.** Logs show 672 low-VIX TRENDING blocks, 6 elevated-VIX TRENDING allows, and 2 RANGING-path fills. The gate behaves as designed. | Keep the 0.60 threshold unchanged. Future outcome evidence belongs to strategy graduation; no separate gate work remains. |
| `11.46c` Credit-spread IV-Rank follow-up | IV Rank may also improve credit-spread filtering, but only after observation data exists | Run IVR observation/audit for credit_spread first; only gate trades if evidence supports it |
| ~~`11.48` Allocator risk-target reconciliation~~ ✅ **VERIFIED 2026-08-28** | Per-strategy stop-risk targets now size normal equity entries; exposure caps only reduce quantity. | Five unclipped paper entries matched their targets within broker quantity granularity. PWR and KBE were valid sleeve-cap clips, and ARM was ordinary whole-share rounding followed by a favourable fill. Decision budget, approved risk, binding cap, and launch multiplier now persist through late fills. Fill-time risk is separate; no allocation-lottery or sleeve-starvation regression was found. |
| ~~`11.49` Stop-gap erosion reporting~~ | ✅ **SHIPPED IN PR #88.** Health reports and the dashboard show stop-gap dollars and bps separately from execution-quality slippage. | Closed; informational only, with no health threshold. |
| ~~`11.53` Protective stop anchored to signal close instead of fill~~ | ✅ **CLOSED 2026-08-29.** Five SMA/fractional MARKET entries exercised the fill-anchor path; all four material-drift entries deployed about 100% of budget, and the two completed stop exits reconciled to about −1R. A small-drift entry caused harmless replacement churn. | No further work. Donchian STOP_LIMIT sizing remains separate under `11.54`. |
| ~~`11.54` Donchian STOP_LIMIT risk deployment~~ | ✅ **CLOSED — conservative under-deployment accepted.** Fill-anchored stops work and six completed stop exits reconciled near −1R. Sizing against the limit cap preserves the worst-fill risk guarantee. | Do not add trigger-based sizing complexity. Possible foregone upside belongs to `11.54a`, after winning trades exist. |
| `11.54a` Donchian STOP_LIMIT opportunity-cost audit | ⏸ **PARKED — no winning current-configuration exits yet.** Conservative sizing may leave profit on the table, but a loss-only cohort cannot measure that cost. | After winning current-configuration STOP_LIMIT trades close, compare realized profit with the same exits at full risk-budget deployment. Investigation only; any sizing change requires separate evidence and review. |
| ~~`11.58` Trade evidence integrity: risk dollars and zero reference prices~~ | ✅ **CLOSED 2026-09-03.** Exact-zero values were missing-data sentinels, not real prices, and did not corrupt realized P&L or slippage. | Genuine references are preserved; unknown and structural MLEG references use NULL. Historical zero sentinels migrate to NULL without invented backfills. |
| ~~`11.55` Strategy scale-out experiment~~ | ❌ **RETRACTED.** Once the production 2×ATR stop was included, every tested SMA scale-out variant underperformed baseline. | Do not implement for SMA. The separate bot-wide partial-exit contract remains `11.65`. |
| ~~`11.56` Donchian early-drawdown entry filter~~ | ✅ **CLOSED — evidence did not support a rule.** Early drawdown did not separate winners from losers, and candidate thresholds mainly selected dates and the retired sizing regime. | Reopen only after at least 30 closed trades under current sizing and fill-anchored stops, using a pre-registered test. |
| ~~`11.57` Credit-spread delta-input correction~~ | ✅ **SHIPPED.** Strike selection now uses live spot; QQQ uses VXN-scaled volatility while SPY uses VIX; concurrency is limited to one spread per instrument. | Implementation closed. Continue outcome and entry-walk evidence under `11.63`; revisit only if new pick logs show material target-delta drift. |
| ~~`11.59` Donchian regime gate~~ | ✅ **SHIPPED IN PR #113.** Donchian allows TRENDING, RANGING, and VOLATILE regimes while blocking BEAR; unknown regime data fails closed. | Keep pre/post-2026-08-18 evidence separate. No further gate work without new evidence. |
| `11.60` Donchian correlated-entry heat cap | 🔄 **OBSERVATION MODE ACTIVE; enforcement off.** PR #119 shipped a 1.60%-of-equity Donchian heat ceiling with `STRATEGY_HEAT_CAP_ENFORCED=False`. One live candidate would have been blocked. | Keep collecting would-block outcomes. One event is not enough to authorize enforcement. |
| ~~`11.67` Align stock arrival quotes with the configured live feed~~ | ✅ **FIXED 2026-09-03.** `ALPACA_DATA_FEED` now controls both live stock bars and stock arrival quotes, and `arrival_quote` events record the feed actually requested. Invalid values fail closed. | Keep the main switch at `iex` until real-time SIP entitlement is active and verified. Offline research, leveraged trend's completed delayed-SIP daily bars, and OPRA option quotes remain deliberate exceptions. |
| `11.47` SPY options secondary levers (parked) | Backtest hints the hard SL and trailing stop may be suboptimal, but they are second-order vs the VIX gate | SL sweep is ~P&L-neutral 20–50% in the daily-BS model (which can't see intraday premium noise that trips the live 25% stop); winners ride to time-stop while the trailing stop exits mediocre trades early. Re-open with: widen SL to ~35% and/or loosen the trail, tested on a fill-aware model or ≥10 more live closes. Reproduce via `backtest/spy_options_backtest.py` stop-loss sweep. |

### P2 — Future Enhancements

| Item | Why It Matters | Acceptance |
|---|---|---|
| `11.64-I` Leveraged-index trend implementation | ✅ **MERGED AND PAPER ACTIVE.** Typed notional/signal-exit lifecycle policy, cold-start target reconciliation, four-slot dashboard aggregation, and paper-only activation are deployed. | Implementation review is complete. Operational evidence now belongs to `11.64`. |
| `11.64` Leveraged-index trend strategy | 🔄 **PAPER EVIDENCE COLLECTING.** Four entries are open and none has exited; there is not yet an outcome sample. Live activation remains unauthorized. | Collect entry/exit behavior, then complete effective-exposure (`11.66`), holdout, correlation, concentration, product-filter, and pre-2016 stress reviews before any live proposal. |
| `11.65` Strategy-driven partial exits / scale-out contract | **OPEN — bot-wide future feature; separate from operator reduction.** The substrate supports partial-close orders, residual quantity, and lifecycle P&L. Operator `reduce-position --qty` can manually reduce single-leg equities or options in whole units, but `SignalFrame.exits` remains a boolean full-close signal. | Design a typed strategy partial-exit intent. MLEG support remains separate. Require restart, duplicate-dispatch, partial-fill, accounting, and residual-protection tests before any strategy uses it. |
| `11.66` Leverage-aware effective-exposure admission | **OPEN — required before live leveraged allocation, not for paper evidence.** Current gross exposure measures ETF notional, while stored metadata also shows stated/stress exposure. | Design a separate configurable portfolio limit using lifecycle-authoritative leverage assumptions. Do not infer leverage from ticker names or enforce an uncalibrated stress multiplier. |
| ~~Lifecycle-first startup ownership restoration~~ | ✅ **MERGED IN PR #145 AND PAPER-EXERCISED 2026-09-04.** Broker truth decides what exists; lifecycle identity restores single-leg ownership before legacy trade replay. | Twelve broker positions restored from lifecycle rows and startup entered NORMAL mode. MLEG spreads intentionally remain trade-ledger restored. |
| `11.52b` Daily-bar readiness assumption | **PARKED.** The 15:00 UTC readiness cutoff is correct for the US equities the bot trades, but would be wrong for other sessions. | Reopen only if the bot adds a non-US-equities session, US hours change, or feed publication timing changes; then retain each calendar day's actual open time. |
| ~~`11.52a` Delayed-SIP cache-gap boundary~~ | ✅ **SHIPPED IN PR #102.** Repair requests stay inside the delayed-SIP request window and do not retire the current session before its bar is available. | Closed. |
| ~~`11.52` Bar-cache gap integrity~~ | ✅ **FIXED.** Cache coverage follows returned bars, diagnostics cannot truncate cache files, and independent calendar checks repair interior gaps with a two-strike absent-session rule. | Closed; reopen only on a verified cache hole. |
| ~~`11.51` Trade position-type integrity~~ | ✅ **FIXED.** `TradeRecord.position_type` is required and validated against the canonical vocabulary, preserving the single-leg order-id uniqueness constraint. | Closed. |
| ~~`11.50` Cross-instrument slippage aggregation~~ | ✅ **SHIPPED.** P&L reports segment execution-quality bps by instrument class instead of pooling equities and options. | Closed. |
| Dynamic watchlists (`11.1`) | Static universes are operationally simple; dynamic rotation needs durable ownership proven first | Dynamic source supports refresh cadence and never abandons open positions |
| `11.48b` Long-term risk-target maintenance | Static targets depend on periodic watchlist-volatility checks; drift is safe because caps only reduce risk. | Park until dynamic watchlists. Then add an automated coverage and clip-rate check before changing targets. |
| `11.62` Portfolio-level heat ceiling | **OPEN — design required.** Per-sleeve heat caps do not bound total initial risk across the book. | Audit current aggregate heat and interactions with `11.60`, then propose a simple fail-closed portfolio ceiling before implementation. |
| `11.61` Entry-candidate ranking | **OPEN.** When capital or heat binds, fixed watchlist order decides which eligible symbol wins. | First log contended candidates and the chosen order. Rank only on observable cost, diversification, and risk efficiency—not predicted returns. |
| Calibrated sector caps (`11.8`) | Sector exposure is observable; caps should be data-driven, not blanket | Add targeted caps only if paper exposure shows a real concentration problem |
| Dynamic strategy allocation (`11.9`) | Could improve capital efficiency once each sleeve has enough live/paper evidence | Weight suggestions based on expectancy/Sharpe with operator approval. *When implemented, key `SleeveAllocator` reserve/release on `(strategy, position_uid)` per `docs/operator_controls_proposal.md` §17.* |
| Defensive cash sweep (`11.45`) | Idle capital during prolonged BEAR/VOLATILE regimes loses purchasing power | SGOV/BIL-style posture only after strict prolonged-BEAR confirmation and recovery state machine |
| Same-underlying single-leg options ownership | Needed before adding a second single-leg options strategy on SPY/QQQ | Single-leg options can use OCC/UUID position ids without breaking exits, DB restore, allocator, dashboard |
| Operator controls dashboard integration | Surface `position_uid` in the dashboard's open-positions table by reading the per-order substrate directly. Independent of operator controls Phase A/B/C, which have shipped. | Per `docs/operator_controls_proposal.md` §17.2. Reads from `position_lifecycle_orders` rather than `engine_state.json`. |
| Backtest reconcile by lifecycle ID | **OPEN — eligible but low priority.** The original four-week data-age gate is satisfied; no current decision requires lifecycle-exact matching. | Refactor `backtest/reconcile.py` only when a paper/backtest investigation needs lifecycle-exact joins. |
| `trades.parent_position_uid` column | For rolls/derived positions | Add when the rolls/derived-positions feature is designed — bundle into that PR. Per `docs/operator_controls_proposal.md` §8. |
| Option-trailing mirror column removal | **OPEN — not a simple column drop.** `alpaca_stop_order_id` is duplicated, but `stop_order_status` also carries attempt outcomes the normalized order substrate cannot represent. | Add an attempt-outcome model and broker-status adapter, migrate remaining readers, then remove mirrors only after parity evidence. |
| Option-trailing consumer migration | **OPEN — organic adoption.** Readers should use `get_by_occ_joined` for substrate-authoritative order identity. | Migrate each dashboard/health/reporting consumer when next changed; no horizontal refactor is justified alone. |
| ~~Stop-fill legacy fallback removal~~ | ✅ **SHIPPED 2026-08-14.** Substrate events are the sole immediate stop-fill path; broker-history reconciliation remains the recovery path for a rare missing substrate row. | Closed; reopen only if the substrate-write CRITICAL occurs. |
| ~~MLEG partial-close residual reconciliation~~ | ✅ **SHIPPED IN PR #72.** Spread close rows and duplicate-dispatch locks are durable across restart. | Closed. |
| Spread `entry_primary` per-order substrate rows | **OPEN — low priority.** Spread parents and close orders are durable, but spread entry orders are not yet recorded in the per-order substrate. A shipped guard preserves correct parent state meanwhile. | Wire entry rows when a real consumer needs them; cover fill, partial, cancel, and restart paths. |
| ~~Spread lifecycle realized-P&L rollup~~ | ✅ **FIXED IN PR #123.** Spread rows now use the canonical lifecycle identity and refresh the parent after partial or full closes. | Closed. Current paper audit found 6 spread parents and zero realized-P&L mismatches. |
| Operator command for a stuck MLEG `partial_close` placeholder | **EVENT-GATED.** No spread partial fill has occurred; manual SQL remains the emergency procedure. | Build the operator command only when a real partial fill makes the placeholder load-bearing, or as part of broader MLEG operator work. |

---

## Completed Milestones

| Area | Current State |
|---|---|
| Environment and config | Python 3.12 project, `alpaca-py`, `config/.env`, paper/live credential separation |
| Data layer | Alpaca historical bars, Parquet cache, validation, freshness checks, retry/backoff |
| Indicators | Hand-rolled SMA, EMA, ATR, RSI, ADX |
| Strategy framework | `BaseStrategy`, `SignalFrame`, `StrategySlot`, `WatchlistSource`, structured edge-filter decisions |
| Backtesting | vectorbt runner, slippage/commission modeling, look-ahead-safe next-open execution, reconciliation tooling |
| Risk manager | Position sizing, ATR stops, prior-close daily (percentage) halt that re-engages after same-day recycle and recomputes after broker baseline rollover, universal entry-only submit guards, loss streak cooldown, broker-error and slippage kill switches |
| Broker execution | Alpaca wrapper, market/limit/OTO/fractional paths, option worker, MLEG worker, stream-first fills |
| Engine | Restart-safe cycle, startup reconciliation, external-close detection, state snapshot, per-strategy slots |
| Reporting | SQLite trade log, PnL summaries (daily report carries real unrealized P&L and equity-path intraday drawdown as of 2026-08-19), alerts, dashboard (now with monthly health report tabs), Strategy Health & Edge reports |
| Allocator | 85/15 pool model, per-strategy sleeves, stretch borrowing for equity only, HWM drawdown gate (paper observation-only by default; live protected) |
| Regime and sector context | BEAR/VOLATILE/TRENDING/RANGING detector with ATR% floor; sector resolver/gauge and strategy-specific policies |
| Active strategy set | SMA, RSI, Donchian, Leveraged Trend, SPY Options, Credit Spread SPY/QQQ |
| Position abstraction | Lifecycle-first single-leg startup ownership; UUID-keyed spreads remain trade-ledger reconstructed; exact-OCC conflict guard |

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
- Operator-facing report fields must come from authoritative runtime data, never silent defaults. Daily/weekly P&L wiring now uses trade-log realized P&L, broker position unrealized P&L, and the persisted/broker-reconciled intraday equity path.
- Per-strategy stop-risk targets size equity entries (`STRATEGY_RISK_PER_TRADE_PCT`, 11.48). Decision budget, approved risk, binding cap, and launch multiplier are stored before submission; fill-time risk is separate. Recheck target coverage when watchlists change.
- Do not add a second same-underlying single-leg options strategy until the ownership model supports it.
- Do not tune paper-watch parameters pre-emptively; audit first, change second.
- `position_uid` is project-wide lifecycle identity, generated before broker submission and persisted in `position_lifecycle`. Operator controls, the per-order substrate, and single-leg startup ownership use it today. Remaining dashboard/reconcile consumers adopt it only when their contracts require it; MLEG startup reconstruction remains trade-ledger based until spread entry substrate data is complete.

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
| Donchian trailing broker stop | ✅ **CLOSED — static stop retained.** The 2016–2024 SIP comparison found no meaningful risk-adjusted benefit from Donchian-low or chandelier trailing stops. Reopen only for a documented live giveback case that materially contradicts the aggregate result. See `docs/donchian_trail_investigation.md`. |
