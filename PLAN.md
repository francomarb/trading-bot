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
| Strategy Health threshold watch (`11.10h`) | 🔄 In progress | Run ≥4 weeks of reports, then tune false-positive thresholds |
| Credit-spread paper-watch (`11.30`, `11.41`) | 🔄 In progress | Audit open/close attempts, close timeout behavior, and execution quality |
| SPY option trailing durability | ✅ Complete | PR #46 merged: broker-premium durable HWM, GTC stop protection, atomic Alpaca replacements, missing-HWM alerting, and paper-verified GTC acceptance |
| Capped equity entry stop durability | ✅ Code complete; paper verification pending | PR #47 merged (2026-06-06): preserves DAY LIMIT + OTO entry, promotes attached stop child to GTC via `ReplaceOrderRequest` after confirmed full fill, reconciles managed DAY stops to GTC from broker snapshots, runs durability checks during market-closed cycles. Awaiting next capped paper fill to confirm DAY parent fill → GTC child replacement → exactly one GTC protective stop end-state. |
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
| Operator controls Phase A (`docs/operator_controls_proposal.md`) | Live trading needs precise, audited operator intervention rather than only the blunt stop/edit/restart options available today | PR-1: `position_uid` lifecycle identity ✅ shipped + baked 4 days; PR-2: `operator_commands` queue + `scripts/operator.py` CLI + sticky halt + gap fixes (entry recovery, reverse-reconcile grace) shipped; ready to bake on paper before destructive controls (Phase C) |

### P1 — Paper-Watch And Calibration

| Item | Why It Matters | Acceptance |
|---|---|---|
| Slippage model unification review (`10.D1` support) | Calibration is not trustworthy if execution slippage, implementation shortfall, stop-gap erosion, and recovery rows share one ambiguous data contract | Review and approve [`docs/slippage_unification_design.md`](docs/slippage_unification_design.md) before changing slippage persistence or dashboard semantics. **Implementation tracker:** [`docs/slippage_unification_tracker.md`](docs/slippage_unification_tracker.md) — Phase 1 merged (`bf16b5a`); smoke check on main in progress; Phase 2 (consumer migration) and Phase 3 (historical cleanup) pending. |
| `11.10h` Strategy Health paper-watch | Health/Edge monitor is advisory but noisy thresholds can create operator fatigue | ≥4 weeks reports reviewed; `calibrate_health_thresholds.py` output reconciled with operator judgment. *Once `position_uid` ships (Operator Controls Phase A), fold in the partial-exit trade-count fix per `docs/operator_controls_proposal.md` §17.* **Operator runbook:** envelopes are gitignored — each machine must run `scripts/build_envelopes.py` once to materialize `data/envelopes/{sma_crossover,rsi_reversion,donchian_breakout}.json` (offline backtest source). For `spy_options_reversion` / `credit_spread`, use `scripts/build_paper_envelope.py --strategy <name> --weeks N` after ≥10 closed trades have accumulated (credit_spread only became eligible once `log_spread_fill` started writing `initial_risk_dollars`). Until each envelope exists on disk, the EdgeAssessor cannot fire signal #1 (R-expectancy CI vs envelope) for that sleeve and verdicts stay UNDETERMINED. |
| `11.26` SPY options picker audit | The 10% fatal-spread threshold is paper-tested, not proven | 10-20 fills audited for spread distribution, fill rate, and realized slippage |
| `11.30` Credit-spread paper-watch | Short-premium strategy needs real fill and risk evidence before live | 20-30 completed cycles/attempts audited; IV floor, credit floor, and risk assumptions either confirmed or adjusted |
| `11.34` Credit-spread exit paper-watch | Exit triggers and paper close reliability need evidence | Exit reasons, close attempts, retry counts, and realized close prices reviewed |
| `11.41` Credit-spread close execution tuning | Mid-price closes have timed out and retried for hours in paper | Decide between unchanged, more marketable debit, or staged retry ladder based on actual close-attempt data |
| `11.46b/c` IV Rank follow-ups | IV Rank may improve SPY options and credit-spread filtering, but only after observation data exists | Run IVR observation/audit first; only gate trades if evidence supports it |

### P2 — Future Enhancements

| Item | Why It Matters | Acceptance |
|---|---|---|
| Dynamic watchlists (`11.1`) | Static universes are operationally simple; dynamic rotation needs durable ownership proven first | Dynamic source supports refresh cadence and never abandons open positions |
| Calibrated sector caps (`11.8`) | Sector exposure is observable; caps should be data-driven, not blanket | Add targeted caps only if paper exposure shows a real concentration problem |
| Dynamic strategy allocation (`11.9`) | Could improve capital efficiency once each sleeve has enough live/paper evidence | Weight suggestions based on expectancy/Sharpe with operator approval. *When implemented, key `SleeveAllocator` reserve/release on `(strategy, position_uid)` per `docs/operator_controls_proposal.md` §17.* |
| Defensive cash sweep (`11.45`) | Idle capital during prolonged BEAR/VOLATILE regimes loses purchasing power | SGOV/BIL-style posture only after strict prolonged-BEAR confirmation and recovery state machine |
| Same-underlying single-leg options ownership | Needed before adding a second single-leg options strategy on SPY/QQQ | Single-leg options can use OCC/UUID position ids without breaking exits, DB restore, allocator, dashboard |
| Operator controls Phase B (soft controls + alerts integration) | Once Phase A bakes, the operator needs `pause-entries` / `pause-strategy` / `resume-after-halt` plus Telegram migration into the queue | Per `docs/operator_controls_proposal.md` §13 Phase B. Includes `position_uid` in fill/exit alerts per §17. |
| Operator controls Phase C (destructive controls + Phase A deferrals) | Destructive `reduce-position` / `close-position` / `cancel-position-orders` plus the Phase A nice-to-haves: `Position.position_uid` field, in-memory startup re-attach, `operator_command_uid`/`client_order_id` columns on `trades`, `position_uid` in `engine_state.json` snapshot | Per `docs/operator_controls_proposal.md` §13 Phase C. Symbol-level locks, stop cancel/recreate, allocator/PnL reintegration. Deferred until Phase B ships and bakes. |
| Operator controls dashboard integration | Once Phase C lands `position_uid` in `engine_state.json`, surface it in the dashboard open-positions table | Per `docs/operator_controls_proposal.md` §17. |
| Backtest reconcile by lifecycle ID | Join paper vs backtest by synthesized lifecycle ID once `position_uid` has ≥4 weeks of paper data | Per `docs/operator_controls_proposal.md` §17. Refactor `backtest/reconcile.py`. |
| `trades.parent_position_uid` column | For rolls/derived positions | Add when the rolls/derived-positions feature is designed — bundle into that PR. Per `docs/operator_controls_proposal.md` §8. |
| Donchian trail stop — SIP re-test on 2018/2020 regimes | The IEX-only investigation (parked-ideas row below) closed for chop/bear/rally on 2021-2024 data, but pre-2020 catastrophic-gap regimes — the actual reason the PLAN concern existed — were untested. SIP basic-tier delayed data is verified to reach 2016-01-04 for AAPL/MSFT/NVDA and most ai_bigtech mega-caps. PR #50 establishes the SIP backtest infrastructure but explicitly defers the retest to a follow-up; this row IS that follow-up. Until it ships, the parked decision stands only for the regimes already tested, and the broader closure of the question is open. | **Blocking: PR #50 merged + bot recycle includes `scripts/migrate_cache_to_feed_aware.py` run.** After that: (1) Update `scripts/donchian_trail_compare.py` to default `feed=settings.BACKTEST_DATA_FEED`; (2) Add two new windows that include pre-crash lead-in time so positions are actually open when the gap-down hits — `2018_q4_vol_shock` runs **2018-07-01 → 2018-12-31** (entries allowed from July; crash days Oct 3 - Dec 24); `2020_covid_crash` runs **2019-09-01 → 2020-05-31** (entries allowed from Sep 2019; crash days Feb 20 - Mar 23, 2020). Note: `run_window()` passes `window.start` as `trade_start` to `simulate_symbol`, which blocks entries before that boundary — so the entry window must begin BEFORE the crash for the test to evaluate carried-into-crash positions, which are exactly the cases the PLAN concern is about. (3) Re-run on the full `ai_bigtech` universe with `feed="sip"`, 2016-01-01 → 2024-12-31 combined window plus all six regime sub-windows; (4) Report per-sub-window per-variant: trade_count, trades_open_during_crash_week, mean_return, Sharpe, MaxDD, %gap exits, and per-variant R-distribution on crash-exposed trades specifically; (5) Update `docs/donchian_trail_investigation.md` with R5 numbers and verdict. **Minimum sample requirements** (mandatory before any closure verdict — insufficient data → abstain, do not rubber-stamp): each of the two new sub-windows must have ≥ 25 total trades AND ≥ 10 trades open during the named crash days. If either floor isn't met, document the gap and defer closure; do not declare "no difference" on a sample too small to detect one. **Closure criteria** (apply only after minimum samples met): if all three variants stay within 0.3 pp mean return / 0.05 Sharpe / 1.0 pp MaxDD on each of the two new sub-windows AND there's no statistically meaningful divergence in the per-variant R-distribution on crash-exposed trades, the parked row promotes to full closure with the static stop retained. If chandelier or Donchian-low trail outperforms static materially on either catastrophic-gap regime — particularly on the crash-exposed trade subset — write up the case and gate a 4-6 week paper A/B before any live promotion. Either outcome is acceptable; closing the question honestly is the goal. |

---

## Completed Milestones

| Area | Current State |
|---|---|
| Environment and config | Python 3.12 project, `alpaca-py`, `config/.env`, paper/live credential separation |
| Data layer | Alpaca historical bars, Parquet cache, validation, freshness checks, retry/backoff |
| Indicators | Hand-rolled SMA, EMA, ATR, RSI, ADX |
| Strategy framework | `BaseStrategy`, `SignalFrame`, `StrategySlot`, `WatchlistSource`, structured edge-filter decisions |
| Backtesting | vectorbt runner, slippage/commission modeling, look-ahead-safe next-open execution, reconciliation tooling |
| Risk manager | Position sizing, ATR stops, prior-close daily/hard-dollar halts that re-engage after recycle, universal entry-only submit guards, loss streak cooldown, broker-error and slippage kill switches |
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
| RSI SPY-50 smoothing | Audit first; hard gate may be correct or the 50-SMA arm may be unnecessary |
| Alpaca trailing stops, VWAP/TWAP, extended hours | Not relevant at current strategy cadence/size; revisit only if a strategy needs them |
| Donchian trailing broker stop (IEX investigation, historical record only) | **IEX investigation parked 2026-06-07 R3** (originally claimed closed 2026-06-06; revised three times after reviewer findings). This row is the **historical record of what was tested on IEX-reachable regimes** — it does NOT track the retest. The active SIP retest lives in the **P2 — Future Enhancements** table above as "Donchian trail stop — SIP re-test on 2018/2020 regimes," with explicit acceptance criteria. **What this row records — findings on IEX-reachable window only (R2 numbers, 207 combined trades, ai_bigtech, production gates ON):** Donchian-low trail is a wash vs static — returns within 0.1 pp, Sharpe identical at 0.18, MaxDD within 0.1 pp. Chandelier helps marginally in chop but gives back 0.9 pp in the 2023-24 AI rally because it clips trending winners. Gap-through exits do happen (7-14% of static-stop trades) but the strategy's signal exit catches most trend failures on the next-day close — within the IEX-reachable regimes. **The 2018 Q4 vol shock and 2020 COVID crash regimes that originally motivated the PLAN concern were NOT tested** — IEX individual-stock coverage begins 2020-07-27. SIP probing (verified 2026-06-07) shows AAPL/MSFT/NVDA SIP daily bars go back to 2016-01-04, so these regimes ARE reachable on the basic Alpaca delayed-SIP tier — see the active P2 row for the retest. Full writeup: [docs/donchian_trail_investigation.md](docs/donchian_trail_investigation.md). Simulator: [backtest/donchian_trail_sim.py](backtest/donchian_trail_sim.py). Reproduce IEX numbers: `venv/bin/python scripts/donchian_trail_compare.py`. |
