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
| Slippage model unification review (`10.D1` support) | Calibration is not trustworthy if execution slippage, implementation shortfall, stop-gap erosion, and recovery rows share one ambiguous data contract | Review and approve [`docs/slippage_unification_design.md`](docs/slippage_unification_design.md) before changing slippage persistence or dashboard semantics. **Implementation tracker:** [`docs/slippage_unification_tracker.md`](docs/slippage_unification_tracker.md) — Phase 1 complete on `feature/slippage-unification-phase1`, PR open for review; Phases 2 + 3 pending. |
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
| Donchian trailing broker stop | Today the broker-side ATR stop is set once at entry and never moved. The Donchian 15-day-low exit trails up correctly but only fires on a daily close — so a gap-down through the rising low exits at the *original* ATR stop, surrendering all accrued profit on a name that has run far from entry. Real hole on the AI/big-tech universe where individual names can move 30%+ before reversing. | The right gate is **backtest evidence**, not paper-trade count: live paper accumulates too slowly to A/B-test a design change. Run vectorbt on the `ai_bigtech` universe across multiple regimes (2018 vol, 2020 crash, 2021 melt-up, 2022 bear, 2023–24 AI rally) comparing (a) static-ATR (current) vs (b) trailing-15-day-low vs (c) chandelier `peak − k×ATR`. Before shipping the variant, think through: (i) whipsaw risk — 15-day low can be much tighter than 2×ATR on low-vol names and could clip marginal winners the current design rides through; (ii) interaction with the signal exit — if the broker stop tracks the same 15-day low, the signal exit becomes vestigial and the strategy's character shifts from daily-close-driven to intraday-driven; (iii) wick buffer — stop placed *at* the low gets clipped by noise, needs a small buffer (e.g. 0.5×ATR below the low); (iv) replace-stop cadence — daily is probably right, per-cycle (5-min) adds API churn and a brief no-stop window with no upside. If backtest is favorable, then a 4–6 week paper A/B before promoting. Do not bolt on as a hotfix. |

---

## Completed Milestones

| Area | Current State |
|---|---|
| Environment and config | Python 3.12 project, `alpaca-py`, `config/.env`, paper/live credential separation |
| Data layer | Alpaca historical bars, Parquet cache, validation, freshness checks, retry/backoff |
| Indicators | Hand-rolled SMA, EMA, ATR, RSI, ADX |
| Strategy framework | `BaseStrategy`, `SignalFrame`, `StrategySlot`, `WatchlistSource`, structured edge-filter decisions |
| Backtesting | vectorbt runner, slippage/commission modeling, look-ahead-safe next-open execution, reconciliation tooling |
| Risk manager | Position sizing, ATR stops, daily/hard-dollar halts, loss streak cooldown, broker-error and slippage kill switches |
| Broker execution | Alpaca wrapper, market/limit/OTO/fractional paths, option worker, MLEG worker, stream-first fills |
| Engine | Restart-safe cycle, startup reconciliation, external-close detection, state snapshot, per-strategy slots |
| Reporting | SQLite trade log, PnL summaries, alerts, dashboard, Strategy Health & Edge reports |
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
