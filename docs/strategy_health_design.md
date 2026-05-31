# Strategy Health & Edge Monitor — v1 Design

**Status:** Implemented (v1) — shipped 2026-05-19 across PRs #16–#22 (PLAN.md 11.10a–g). Code lives in `strategies/health/`. The remaining open item is **11.10h** — a 4-week paper-watch + threshold calibration pass (no new code until the tuning PR). This doc is the as-built design reference; deviations made during implementation, if any, are noted inline.
**PLAN.md item:** 11.10
**Related items:** 11.9 (dynamic capital allocation — downstream consumer), 11.11 (re-enable workflow — manual gate for quarantined strategies), 11.12 (Kelly criterion — advisory, complementary), 11.14 (read-only dashboard — render target)
**Future work:** see [strategy_health_future.md](strategy_health_future.md) for the follow-up roadmap (PSR/DSR/MinTRL, CUSUM, signal-lifecycle table, MAE/MFE, auto-throttle, etc.) — deliberately split out so this doc stays focused on what ships now.
**Author note:** Written so future sessions can pick up cold. This doc records *why* each design choice was made. An earlier ChatGPT-drafted spec was reviewed and ingested — its strongest ideas (concrete verdict/recommendation labels, operator-facing sufficiency phrasing, schema thinking) are folded in; its weaker structural choice (Statistical inside Health) is not, with reasons in §3.5.

---

## 1. Objective

Build a per-strategy assessment system that runs continuously and produces honest, actionable verdicts on whether each strategy is **(a) functioning correctly** and **(b) worth running**. The system must:

- Catch the **silent-killer case** — a strategy with clean execution that is steadily losing money — loudly and early. This is the single most important detection.
- Not over-react to **normal drawdown / small-sample noise**. Auto-disabling a healthy strategy during a routine 3-sigma drawdown is the failure mode that destroys edge.
- Separate **the verdict** (keep / cut / reallocate — driven by Edge) from **the forensics** (what's broken or fragile — driven by Health). A profitable strategy with messy execution is still earning; operator alarm fatigue is worse than the noise.
- **Inform the operator; never act autonomously.** Every operational decision stays with the operator. See §1.2.

### 1.1 Success criteria — what the operator needs

These four bullets are the user's stated priorities. **Every design decision must trace back to one or more of these.** If a choice doesn't, it is scope creep.

| # | What the operator needs | How v1 delivers it |
|---|---|---|
| 1 | **Knowing whether each strategy is actually healthy in live paper conditions** | Weekly L1–L3 + EdgeReport per strategy written to disk; dashboard panel shows both scorecards; on-demand CLI. Operator never derives "is X healthy?" from raw logs. |
| 2 | **Distinguishing "bad execution" from "bad edge"** | Edge/Health primacy inversion (§3): bad execution = `continue and monitor`, fix in parallel, do not throttle. Bad edge = silent-killer alarm + `pause and investigate`. Visually and operationally separable in every report. |
| 3 | **Distinguishing "temporary drawdown" from "real degradation"** | `min_trades_for_verdict` floor per strategy + three independent statistical signals must agree for NEGATIVE + **3-week persistence requirement** before alarm fires (§9). A single bad week is exactly what normal variance looks like; three consecutive bad weeks is harder to write off. |
| 4 | **Being able to make sizing / pause / continue decisions with confidence** | Four-label recommendation set (`continue` / `continue and monitor` / `reduce size` / `pause and investigate`) — each carries sufficiency, driving checks, and measured/inferred/envelope provenance (§11.5, §12.4). |

**Failure modes this design is explicitly trying to prevent:**

- *Silent bleeder.* (#1 + #2.) Strategy looks fine on the dashboard while losing money — caught by silent-killer alarm.
- *Strategy-killer ratchet.* (#3.) Auto-disabling on noise — prevented by `min_trades` floor + persistence + advisory-only invariant.
- *Alarm fatigue from cosmetic health issues.* (#2.) Telegram pings about slippage on a profitable strategy — prevented by Health alerts being INFO-only when Edge is positive.
- *"The backtest said so" recommendations.* (#4.) Continuing a bleeding strategy because its backtest looked good — prevented by recommendations being derived from live observed Edge, not backtest opinion.
- *Untraceable verdicts.* (#4.) Operator can't tell which numbers are measured vs inferred vs from backtest — prevented by in-text source labels on every metric.

### 1.2 v1 invariant — the bot informs, the operator decides

**This is the strongest design rule in v1 and overrides anything else in this document.** The bot's job in v1 is to **compute assessments and emit reports/alerts**. It does not take action. Every operational decision — change a sleeve weight, pause a strategy, reduce size, re-enable after quarantine — is made by the **operator reading the reports**, not by the bot acting on its own conclusions.

**What the bot is allowed to do in v1:**
- Compute `EdgeReport` and `HealthReport` per strategy on weekly/monthly cadence
- Write markdown reports to `data/health_reports/`
- Render a read-only dashboard panel
- Send Telegram alerts on defined state transitions (including the silent-killer alarm)
- Track persistence state across weekly checks (e.g. "this strategy has been NEGATIVE for 2 of 3 weeks") so the next assessment can use it
- Attach a textual `recommendation` field to each report row as **a suggestion for the operator to consider**

**What the bot is NOT allowed to do in v1:**
- Modify `STRATEGY_ALLOCATIONS` or any sleeve weight
- Halt a strategy, set it to cooldown, or skip its signals
- Reduce position sizing or change `MAX_POSITION_PCT`
- Cancel pending orders based on health verdicts
- Trigger any change to `RiskManager`, `SleeveAllocator`, or `TradingEngine` *behavior* based on its own assessments
- Track whether the operator followed a recommendation (this is not a feedback-loop system in v1)

**Interaction with existing automated risk controls.** The bot already has automated controls that *do* take action: per-strategy loss-streak cooldown (`RiskManager`), per-strategy sleeve drawdown (`SleeveAllocator`), daily-loss circuit breaker, slippage-drift kill switch, broker-error-streak kill switch, entry-price guard (11.32). **These remain fully active in v1 and v1+.** Strategy Health is purely additive — it *reads* their current state as inputs to its Health L1 checks (e.g. "strategy currently in cooldown for 2h:14m") but never disables, bypasses, or modifies them. The invariant above applies only to the new monitor; it does not relax existing automated risk controls.

**The word "recommendation" in this doc always means "information the operator considers when deciding"** — never "an action the bot intends to take." If a future reader sees "recommendation" and thinks "automation," they are reading the doc wrong. The recommendation is text in a report; nothing in the bot's runtime reads it.

**Why this invariant is non-negotiable for v1:** the bot acting on its own health assessments adds a new failure mode (wrong assessment → unwanted action → capital impact) on top of the already-hard problem of *correct* assessment. v1 must prove the assessments themselves are useful before we trust them to drive behavior.

**When this invariant gets relaxed:** future work only (see [strategy_health_future.md](strategy_health_future.md)), only after lived experience with v1's recommendations, only with explicit per-feature gating, and only with auto-restore-to-baseline guardrails. The pattern will mirror existing automated risk controls (loss-streak cooldown, slippage drift kill switch).

### 1.3 v1 acceptance test against success criteria

| Criterion | v1 covers? | What does the covering |
|---|---|---|
| 1. Healthy in live paper conditions | ✅ | EdgeReport + HealthReport, weekly markdown, dashboard panel, CLI |
| 2. Bad execution vs bad edge | ✅ | Edge/Health primacy inversion + decision matrix (§4) |
| 3. Temporary drawdown vs real degradation | ✅ (heuristic, honest about it) | `min_trades_for_verdict` floor + three statistical signals must agree + **3-week persistence requirement** before NEGATIVE alarm (§9) |
| 4. Sizing / pause / continue with confidence | ✅ | Four-label recommendation set including `reduce size` as advisory (§11.5); each carries sufficiency + driving checks + provenance |

Note: rigorous-statistics replacements (PSR/DSR/MinTRL, CUSUM, block bootstrap) are deferred — see [strategy_health_future.md](strategy_health_future.md). v1's three-signal + persistence + floor is sufficient defense against the named failure modes; rigorous detectors are added only if v1 observation justifies them.

**Reviewer-driven additions folded into v1 (after first review pass):**
- §1.2 explicitly clarifies that existing automated risk controls (cooldown, sleeve drawdown, slippage drift halt, entry-price guard, etc.) remain fully active in v1+ — the invariant applies only to the new monitor and never relaxes existing controls.
- §5.1 + §5.2 + §9 — R-multiple expectancy is promoted to first-class primary metric (dollar expectancy retained as secondary context). Sizing-invariant; reads existing `r_multiple` column in `trades` table at zero new instrumentation cost. Cumulative-R is also the equity curve used for the EMA50/EMA100 cross.
- §12.4.1 — aggregate signal-lifecycle counters added to v1 as a new SQLite table `strategy_lifecycle_counters` (7 counter fields keyed by `(period_type, period_start, strategy_name)`). `data/health_state.json` is *separate* — small verdict-persistence state only (§12.4.2). Closes the gap where L3 drift claims would otherwise have been aspirational; full per-cycle `signal_lifecycle` SQL table with `reasons_json` taxonomy remains follow-up §F6.

### 1.4 Anti-goal: what the original PLAN.md framing got wrong

The original 11.10 entry read: *"rolling expectancy + rolling Sharpe per strategy; automatic capital reduction or disable when performance degrades beyond a threshold."* Rejected for three reasons:

1. **Sample size.** With per-strategy trade rates of 10–50 trades/year (SMA crossover lowest, Donchian batchy on 32 names highest), a rolling-window Sharpe is statistical noise on the time horizons that matter. A strategy with perfect edge will hit rolling Sharpe < 0.3 for multi-month stretches just from variance.
2. **Auto-disable ratchet.** Killing a strategy on a noisy metric is irreversible (gated by 11.11 manual re-enable). One bad month kills the strategy for one good month back. Over time the bot loses every strategy.
3. **Health/Edge conflation.** "Performance degrades" mixes operational issues, execution issues, and statistical underperformance into one number, hiding the most important diagnostic information — whether the strategy is bleeding because it broke or because the edge is gone.

---

## 2. Scope & non-goals

**In scope (v1):**
- Per-strategy `EdgeReport` and `HealthReport`, computed weekly (and on-demand)
- Static reference envelope per strategy from `backtest/runner.py` (point estimate)
- Streamlit dashboard panel rendering both scorecards
- Telegram alerts for silent-killer + defined transitions
- On-demand CLI for weekly/monthly/yearly reviews

**Explicitly out of scope (v1):**
- Auto-throttle (graduated sleeve weight reduction) — see future doc
- Auto-disable / auto-quarantine — stays manual via 11.11 forever
- Portfolio-level health (correlation drift, regime-mix performance) — sleeve allocator + 11.9 own portfolio capital decisions; 11.10 stays per-strategy
- Cross-strategy comparison ranking — each strategy evaluated against its own envelope and benchmark
- ML / model-based decay detection
- Hybrid envelope recalibration from paper data — static envelope only in v1
- Any per-cycle lifecycle SQL table or additional historical SQL tables beyond `strategy_lifecycle_counters` (the one aggregate table added in §12.4.1) — v1 otherwise reuses existing `data/trades.db`, `engine_state.json`, `logs/bot.jsonl`

---

## 3. Mental model — Edge is the verdict, Health is forensics

The system produces **two scorecards per strategy, with strict primacy:**

| Scorecard | Question it answers | Drives |
|---|---|---|
| **EdgeReport** | Is this strategy worth running? | `continue` / `reduce size` / `pause` recommendations |
| **HealthReport** | Is the system functioning correctly? | Forensics + fragility warning; **never overrides Edge** |

The primacy inversion is the most important rule in this design:

- **A perfectly healthy strategy that is losing money is the case we most need to catch.** Everything looks green on a normal dashboard; the operator assumes things are fine; capital bleeds silently. This is the alarm the monitor exists to make loud.
- **An unhealthy strategy that is profitable should be left alone.** Telegram noise about "slippage 28bps above modeled" or "RSI filter block rate elevated" is operator-fatigue spam when the strategy is earning. Log it, surface on dashboard for the curious, but no alarm and no throttle.

### Health layers (forensics only)

| Layer | Question | Examples |
|---|---|---|
| **L1 Operational** | Is the strategy *running* and *seeing the world correctly*? | Stream connected; regime gate firing; watchlist non-empty; sector resolver hits; cycle latency; reconciliation mismatches; missing stop repairs |
| **L2 Execution** | When it does trade, is execution honest? | Realized vs modeled slippage; fill rate; order rejection rate; timeout/cancel rate; spread for options; signal-to-fill conversion |
| **L3 Drift** | Is the live signal distribution diverging from backtest? *(Leading indicator of future edge loss.)* | Trade frequency vs envelope; hold-time distribution; edge-filter block rate; concurrent-position clustering |

L3 is explicitly **leading-indicator** in nature — it warns of edge erosion before it shows up in PnL, but does not itself constitute an edge verdict. The realized statistical underperformance question moves entirely into EdgeReport.

### 3.5 Cross-reference to the earlier ChatGPT draft

The earlier draft proposed four Health categories (A Operational / B Execution / C Statistical / D Regime-Behavioral). Our design folds Statistical entirely into EdgeReport (where it drives the verdict) and splits Regime-Behavioral across L3 Drift and EdgeReport §5.4. Mapping for cross-spec navigation:

| ChatGPT draft category | Our location | Why moved |
|---|---|---|
| A. Operational Health | `HealthReport.L1` | Same |
| B. Execution Health | `HealthReport.L2` | Same |
| C. Statistical Health | `EdgeReport` §5.1 + §5.2 | Statistical *is* the edge verdict — keeping it inside Health is what enabled the silent-killer case to hide. Moved out so verdict has somewhere clean to live. |
| D. Regime / Behavioral Health | `HealthReport.L3` + `EdgeReport` §5.4 | Signal-shape drift → L3 (leading indicator). Capital efficiency → EdgeReport §5.4 (utilization is an economic question, not a health question). |

### 3.6 Per-layer verdict labels

Each layer's `CheckResult` carries one of four labels:

- **HEALTHY** — check passes within expected envelope
- **WATCH** — single soft signal, no action; surfaces on dashboard
- **DEGRADED** — sustained or multi-signal deviation; investigation prompt
- **BROKEN** — operational/execution failure that requires fix (L1/L2 only; L3 cannot be BROKEN — drift is by nature gradual)

`HealthReport.overall_status` is the worst of the three layers. **A `BROKEN` Health verdict does not auto-disable the strategy** — per the Edge/Health primacy, only EdgeReport can recommend pause. A `BROKEN` Health on a profitable strategy still gets "keep untouched, fix in parallel."

---

## 4. Decision matrix

| Edge verdict | Health state | Recommendation | Alert |
|---|---|---|---|
| POSITIVE, CONCLUSIVE | HEALTHY / WATCH | `continue` | none |
| POSITIVE, CONCLUSIVE | DEGRADED / BROKEN | **`continue and monitor`** (keep earning; investigate health in parallel) | `STRATEGY_HEALTH_*` INFO only |
| **NEGATIVE, CONCLUSIVE** | Any | **`pause and investigate` — SILENT KILLER** | `STRATEGY_EDGE_LOSS` CRITICAL |
| BELOW-BENCHMARK + CONCLUSIVE | Any | `reduce size` (investigate regime mismatch) | `STRATEGY_EDGE_BELOW_BENCHMARK` WARN |
| INDICATIVE + trending downward (2+ wk) | Any | `reduce size` (early warning) | none |
| Health DEGRADED + low sleeve utilization | Any Edge | `reduce size` (over-allocated) | none |
| INSUFFICIENT / INDICATIVE | HEALTHY / WATCH | `continue and monitor` | none |
| INSUFFICIENT / INDICATIVE | BROKEN | `continue and monitor` + existing operational alert | existing health alert path |

Two invariants:

1. **No combination of Health alone can recommend `pause`.** Health drives operator visibility; only Edge drives keep/cut.
2. **`STRATEGY_EDGE_LOSS` requires CONCLUSIVE sample + 3-week persistence.** Below either bar, system says nothing — refusing to declare a strategy dead on small samples is the discipline that protects edge during normal drawdowns.

---

## 5. EdgeReport — the verdict layer

Three blocks. Every metric carries `(value, 95% CI, N, sufficiency)`.

### 5.1 Profitability

Both **R-multiple expectancy** and **dollar expectancy** are reported. **R-expectancy is primary; dollar expectancy is secondary context.**

Why R is primary: dollar expectancy shifts with sleeve weight changes (so a strategy looks "worse" simply because the operator reduced its allocation) and mixes options/equity strategies on incomparable scales. R-expectancy is sizing-normalized — `realized_pnl / initial_risk_dollars` — so it answers "is this strategy behaving as designed?" independent of capital allocated to it. The `trades` table already has an `r_multiple` column populated for closed trades, so this is zero new instrumentation cost.

- Realized P&L (window + lifetime), dollar
- **R-expectancy per trade with iid bootstrap 95% CI** (primary verdict input)
- **Dollar expectancy per trade with iid bootstrap 95% CI** (secondary)
- **Profit factor with bootstrap CI** (computed on dollar P&L; PF is a ratio so it's already sizing-invariant)
- **Sleeve return on allocated capital** (PnL ÷ sleeve $ × days, time-weighted)
- **Sleeve return on deployed capital** (only when actually in positions) — separates "strategy is bad" from "strategy is starved/idle"
- **Realized R-expectancy vs envelope** — point estimate vs the envelope's R-expectancy CI band (envelope JSON includes R-expectancy alongside dollar expectancy)

### 5.2 Edge verdict — v1 logic

Three signals combine to produce the verdict, **all computed on R-expectancy (not dollars):**

1. **R-expectancy CI vs envelope** — observed R-expectancy CI excludes zero AND lies below envelope R-expectancy CI
2. **One-sided t-test on R-expectancy against zero** — `H0: R-expectancy ≥ 0`, reject at α=0.05
3. **Equity-curve EMA50/EMA100 crossover** — slower than 20/50 to filter routine drawdowns; matches the slower trade rate of our strategies. Equity curve here is **cumulative R** (not cumulative dollars), for the same sizing-invariance reason.

**NEGATIVE verdict** requires all three signals to agree AND `N ≥ min_trades_for_verdict` AND the NEGATIVE state has held for **3 consecutive weekly checks** (§9).

**POSITIVE verdict** requires R-expectancy CI > 0 AND realized R-expectancy within envelope R-expectancy CI band. No persistence requirement.

**BELOW-BENCHMARK verdict** requires Edge POSITIVE AND realized return < benchmark return + CONCLUSIVE sample. (Benchmark comparison stays in dollar/return space because benchmarks are buy-and-hold returns — there is no "R-equivalent" of a passive index.)

### 5.3 Edge vs benchmark

| Strategy | Benchmark | Edge metric |
|---|---|---|
| SMA crossover | Equal-weight BH of `SMA_WATCHLIST` over same window | Alpha = strategy return − benchmark return on same capital |
| RSI reversion | Equal-weight BH of `RSI_WATCHLIST` | Alpha |
| Donchian breakout | Equal-weight BH of `ai_bigtech` 32-name universe | Alpha |
| SPY options reversion | Delta-equivalent SPY shares held over same windows | Premium efficiency = realized P&L ÷ premium paid |
| Credit spreads | Underlying BH (SPY / QQQ buy-and-hold over assessment window) — v1 placeholder | Alpha vs underlying; richer short-vol replicator is a follow-up |

**Choice of benchmark per strategy is canonical Grinold-Kahn** (*Active Portfolio Management*) — benchmark against the universe the strategy is *expressing a view on*, not against SPY. A trend strategy that returns +18% while SPY's same-watchlist BH returned +25% is destroying value despite positive raw P&L.

### 5.4 Capital efficiency

- **Sleeve utilization** — `mean(deployed capital ÷ sleeve cap)` over window. Low + positive expectancy → starved (signal for 11.9).
- **Idle days** — % of session-hours with zero open positions.
- **R-multiple distribution** — distribution of realized P&L in initial-risk units (`stop_distance × qty`).

---

## 6. HealthReport — the forensic layer

### L1 Operational checks

- Stream connectivity (websocket reconnection events in window)
- Watchlist non-empty and source not throwing
- Regime gate firing on schedule
- Sector resolver cache hit rate
- Cycle latency vs target
- Edge filter throwing exceptions
- Cycles processed in window vs expected
- Stale-data incidents (last bar age above threshold per symbol)
- Reconciliation mismatches (engine state vs broker position)
- Missing stop repairs (positions without GTC stop after grace window)
- Ownership conflicts (`SYMBOL_CONFLICT` + `CONTRACT_CONFLICT` events — already alerted at 11.7A and 11.44; surfaced as `symbol_conflicts_24h` / `contract_conflicts_24h` engine-state counters)
- External close detections (positions closed outside the engine)
- Strategy halted / cooldown state (current state, time-in-state)
- Alert frequency by severity in window (a baseline-aware spike is itself a signal)

### L2 Execution checks

- **Realized slippage in bps vs modeled** (existing slippage drift tracker at 6.11 — health monitor reads it, doesn't duplicate)
- Order rejection rate (orders submitted vs rejected by broker)
- Timeout / cancel rate (`ORDER_CONFIRM_TIMEOUT_SECONDS` exceedances)
- Fill rate (orders submitted vs orders filled within session)
- Signal-to-fill conversion (signals generated vs positions actually opened)
- Average time from signal to fill (median + p95)
- For options: realized spread at fill vs picked spread (the 11.26 audit data)
- Stop-fill timing (broker-side stop trigger latency)
- Partial fill rate

### L3 Drift checks

- **Trade frequency vs envelope band** — observed `trades_per_month` falls in the envelope's `[p10, p90]` interval?
- **Hold-time distribution drift** — KS test of observed hold times vs envelope distribution
- **Entry-bar chase distribution drift** — for strategies with entry caps (11.32)
- **Average holding time** drift vs envelope
- **Exposure utilization** drift (mean concurrent positions vs envelope)
- **Concurrent-position clustering** (do entries cluster in time vs backtest expectation?)
- **Edge-filter block rate** — strategy generating fewer/more signals than backtest baseline?

Per-cycle signal lifecycle with `reasons_json` taxonomy, MAE/MFE drift, and CUSUM are deferred — see [strategy_health_future.md](strategy_health_future.md). Aggregate lifecycle counters (raw_signals / regime_blocked / edge_filter_blocked / sleeve_blocked / risk_blocked / submitted / filled_entries per period) ship in v1 via the `strategy_lifecycle_counters` table — see §12.4.1.

---

## 7. Reference envelope

Each strategy ships with a `StrategyEnvelope` JSON file derived from a **single backtest run at the strategy's current production config**:

```
data/envelopes/{strategy_name}.json
{
  "schema_version": 1,
  "strategy": "donchian_breakout",
  "built_at": "2026-05-18T...",
  "backtest_config": {...},                     // exact params used

  // Edge metrics — R-multiple is primary (sizing-invariant); dollars secondary
  "r_expectancy": 0.42,                         // mean R per trade
  "r_expectancy_ci_95": [0.18, 0.65],           // bootstrap from backtest trades
  "expectancy_dollars": 142.0,
  "expectancy_dollars_ci_95": [85.0, 199.0],
  "win_rate": 0.48,
  "win_rate_ci_95": [0.41, 0.55],
  "profit_factor": 1.62,
  "profit_factor_ci_95": [1.21, 2.14],

  // Behavior bands (used by L3 Drift checks in §6)
  "trades_per_month_band": [4, 11],             // p10, p90
  "hold_days_band": [2, 18],
  "p95_drawdown_pct": 0.12,

  // Signal-lifecycle bands (used by L3 Drift against §12.4.1 counters)
  "raw_signals_per_week_band": [12, 38],        // p10, p90 across backtest weeks
  "edge_filter_block_rate_band": [0.55, 0.78],  // edge_filter_blocked / raw_signals
  "regime_block_rate_band": [0.02, 0.18],       // regime_blocked / raw_signals
  "risk_block_rate_band": [0.00, 0.05],         // risk_blocked / raw_signals
  "submitted_per_raw_signal_band": [0.08, 0.25],
  "fill_rate_band": [0.85, 1.0]                 // filled_entries / submitted
}
```

Built by `scripts/build_envelopes.py`. Operator-readable, git-friendly, regenerated when the strategy's production config changes. **Static** — no auto-recalibration in v1. Parameter-grid distribution + hybrid paper recalibration are follow-ups (see future doc).

The lifecycle bands are derived from the backtest by simulating the same gating pipeline (regime → edge filter → sleeve → risk) the live engine runs, so live counters compare apples-to-apples.

---

## 8. Sufficiency framework

v1 uses a simple per-strategy config:

```python
# As-built name in config/settings.py is STRATEGY_MIN_TRADES_FOR_VERDICT.
STRATEGY_MIN_TRADES_FOR_VERDICT = {
    "sma_crossover": 30,
    "rsi_reversion": 25,         # lowered from initial 50 — RSI's tight filters
                                 # (SPY trend, earnings blackout, no-new-low) gate
                                 # very heavily in some regimes; observed 2-month
                                 # zero-trade stretches in paper. 25 is reachable.
                                 # The "RSI isn't firing" case is handled by L3
                                 # Drift (trade-frequency vs envelope), not by
                                 # withholding an Edge verdict forever.
    "donchian_breakout": 50,
    "spy_options_reversion": 40,
    "credit_spread": 50,
}
```

Numbers are conservative — err on the side of INSUFFICIENT, because declaring a strategy dead on a small sample is the failure mode we most want to avoid. Hand-picked heuristic; honest about being one. MinTRL-based rigorous replacement is a follow-up.

**Important design separation:** if a strategy is producing zero trades, that surfaces via the L3 Drift check (`trade frequency vs envelope band`), not by the Edge verdict staying INSUFFICIENT forever. The two are independent — Edge verdict speaks to "is the strategy losing money when it trades?"; L3 Drift speaks to "is the strategy firing at the expected rate?" Operator sees both.

Sufficiency tags + operator-facing phrasing in reports:

| Tag | Threshold | Operator-facing phrasing |
|---|---|---|
| **INSUFFICIENT** | `N < 0.5 × min_trades_for_verdict` | *"Insufficient sample — no verdict yet. {N} of ~{floor} trades needed."* |
| **INDICATIVE** | `0.5 × floor ≤ N < floor` | *"Operationally healthy but statistically inconclusive"* (Health OK) / *"Execution issue despite insufficient trade count"* (Health WARN/BROKEN) |
| **CONCLUSIVE** | `N ≥ floor` | *"Statistically degraded after {N} trades / {M} days"* (Edge NEGATIVE) / *"Statistically confirmed working"* (Edge POSITIVE) |

These phrasings go directly into the weekly/monthly markdown and the dashboard summary row — the operator should not have to translate sufficiency math themselves. Numbers grayed out at INSUFFICIENT; normally with CI at INDICATIVE+.

Practical implication per active strategy (rough order of magnitude):

| Strategy | Trades/year (estimate) | Time to CONCLUSIVE |
|---|---|---|
| SMA crossover | 10–30 | 1–3 years |
| RSI reversion | 30–100 | 6–18 months |
| Donchian breakout | 50–150 | 4–12 months |
| SPY options reversion | 50–150 | 4–10 months |
| Credit spreads | 60–200 | 3–10 months |

This is the honest answer to Carver's "~10 years to distinguish skill from luck on a single strategy" — for low-rate strategies it really is long, and the system should say so rather than pretend otherwise. See §13.

### 8.1 Inline thresholds per strategy

v1 stores Health-check thresholds in `strategies/health/thresholds.py` — a flat dict per strategy with a handful of numbers per check (e.g. `{"slippage_warn_bps": 20, "slippage_degraded_bps": 50, "slippage_broken_bps": 100}`). No archetype abstraction. With 5 strategies, this is cleaner than abstracting prematurely. Archetype-based `HealthThresholdProfile` is a follow-up if strategy count grows past ~8.

---

## 9. Combining the Edge signals (v1)

Three independent signals + persistence requirement. All operate on **R-expectancy (sizing-normalized), not dollar expectancy** — see §5.1 for why:

1. **R-expectancy CI vs envelope** — observed R-expectancy CI (iid bootstrap) excludes zero AND lies below envelope R-expectancy CI
2. **One-sided t-test on R-expectancy against zero** — `H0: R-expectancy ≥ 0`, reject at α=0.05
3. **Cumulative-R equity curve EMA50/EMA100 crossover** — slower than 20/50 to filter routine drawdowns

**NEGATIVE verdict rule:** all three signals agree, AND `N ≥ min_trades_for_verdict`, AND the NEGATIVE state has held for **3 consecutive weekly checks**. The persistence requirement is the most important defense against false positives — a single bad week is exactly what normal variance looks like; three consecutive bad weeks is harder to write off.

**POSITIVE verdict rule:** **R-expectancy CI > 0** AND **realized R-expectancy within envelope R-expectancy CI band**. No persistence requirement.

**Rationale:** false positives on the silent-killer alarm cause operator distrust of the whole system. The 3-week persistence window means up to ~3 weeks of bleeding before alarm — acceptable given the alternative (firing on every routine drawdown then crying wolf). Three statistical signals × three weeks is the v1 substitute for PSR/DSR's mathematical rigor.

---

## 10. Cadence

| Cadence | What runs | Output |
|---|---|---|
| **Weekly (Monday, completed Mon→Mon week)** | Full L1–L3 + EdgeReport per strategy | Markdown report in `data/health_reports/weekly_YYYY-WW.md`; Telegram summary |
| **Monthly** | Same + envelope-vs-actual charts + prior-period comparison | Markdown in `data/health_reports/monthly_YYYY-MM.md`; Telegram summary |
| **On-demand CLI** | `scripts/strategy_health_review.py --window {weekly,monthly,yearly} [--strategy X]` | stdout + markdown file |

Continuous per-cycle L1/L2 in the engine snapshot is deferred — v1 ships weekly-report-only to keep engine surface area minimal.

---

## 11. Alert taxonomy

New alert types in `reporting/alerts.py`:

| Alert | Severity | Trigger | Auto-action |
|---|---|---|---|
| `STRATEGY_EDGE_LOSS` | CRITICAL | Edge NEGATIVE + CONCLUSIVE + 3-week persistence | None (advisory). Operator considers quarantine. |
| `STRATEGY_EDGE_BELOW_BENCHMARK` | WARN | Edge positive but < benchmark + CONCLUSIVE | None. Investigate regime mismatch. |
| `STRATEGY_HEALTH_DEGRADED` | INFO (forensic only) | L1/L2 DEGRADED, Edge positive | None. Surfaces on dashboard. |
| `STRATEGY_HEALTH_BROKEN` | WARN | L1/L2 BROKEN, Edge positive | None. Investigation prompt. |
| `STRATEGY_DRIFT_WARNING` | INFO | L3 drift detected, Edge positive | None. Leading indicator. |

Health alerts are **deliberately INFO when Edge is positive** to prevent alarm fatigue. They never escalate to CRITICAL on Health alone.

### 11.5 Recommendation taxonomy

Each `(EdgeReport, HealthReport)` pair produces one of four concrete operator-facing recommendations:

| Recommendation | When | Notes |
|---|---|---|
| **continue** | Edge POSITIVE + CONCLUSIVE + Health HEALTHY | Default state for a well-functioning strategy. |
| **continue and monitor** | Edge POSITIVE INDICATIVE *or* Health WATCH/DEGRADED/BROKEN with Edge positive | Active observation, no capital change. Most common state during early paper. |
| **reduce size** | (a) Edge INDICATIVE + trending downward across 2+ consecutive weekly checks; (b) Edge POSITIVE-but-BELOW-BENCHMARK + CONCLUSIVE; (c) Health DEGRADED + low sleeve utilization | **v1: advisory only — no auto-action, no specific weight multiplier.** Operator manually adjusts `STRATEGY_ALLOCATIONS` if they agree. |
| **pause and investigate** | Edge NEGATIVE + CONCLUSIVE + persistence | The silent-killer alarm's recommendation. Operator decides whether to quarantine. |

`disable pending review` is owned by 11.11, not 11.10. 11.10 only ever recommends.

Recommendations are **derived from observed Edge + Health, never from backtest opinion alone** — the backtest envelope provides the reference distribution against which live behavior is measured, but the recommendation always reflects live data. This prevents the "the backtest said this would work" failure mode.

---

## 12. Architecture sketch

New module `strategies/health/`:

```
strategies/health/
├── __init__.py
├── envelope.py        # StrategyEnvelope dataclass + JSON I/O (static, no recalibration)
├── benchmarks.py      # Per-strategy benchmark return computation
├── stats.py           # Bootstrap CI, t-test, EMA cross (pure functions)
├── thresholds.py      # Per-strategy inline health thresholds
├── assessor.py        # HealthAssessor: runs L1–L3 checks; returns HealthReport
├── edge.py            # EdgeAssessor: computes EdgeReport; combines signals into verdict
├── persistence.py     # Tracks NEGATIVE-verdict state across weekly checks (3-week rule)
├── reports.py         # Dataclasses: HealthReport, EdgeReport, CheckResult, Sufficiency
└── reviewer.py        # Weekly/monthly report rendering; Telegram summary
```

External pieces:

- `scripts/build_envelopes.py` — one-shot per strategy; runs single backtest at production config and writes envelope JSON.
- `scripts/strategy_health_review.py` — CLI for on-demand reviews.
- `dashboard.py` — new "Strategy Health & Edge" panel rendering both scorecards per strategy.
- `forward_test.py` — wires `HealthReviewScheduler` as the engine `post_cycle_hook`: weekly reviewer on Monday (completed week), monthly reviewer on the first of the month.
- `reporting/alerts.py` — new alert types (§11).

**No changes to trading decision behavior in `risk/allocator.py`, `risk/manager.py`, or `engine/trader.py`** — the advisory-only invariant (§1.2) is unchanged. v1 does add **observability-only wiring to `engine/trader.py`**: ~30 LOC to emit per-cycle gate counts (raw_signals, regime_blocked, edge_filter_blocked, sleeve_blocked, risk_blocked, submitted, filled_entries) into the new `strategy_lifecycle_counters` SQLite table (§12.4.1). These counter increments happen *after* each existing gate has already made its decision and have **zero influence on whether a signal is taken** — they are pure measurement of decisions the engine already made. Counter writes are also failure-tolerant: a write error is logged but never raises into the trading loop.

### 12.4 Data sources

v1 reads from existing infrastructure where possible and adds **one new SQLite table** + **one small JSON state file**:

**Existing (no changes):**
- `trades` table — already carries `strategy`, `realized_pnl`, `r_multiple`, `position_id`, slippage. Read by assessor for per-strategy P&L, expectancy (R and $), win rate, profit factor (existing `read_strategy_realized_pnl_summary` extends naturally).
- `engine_state.json` — sector exposure, positions, halt state, current state of automated risk controls (cooldown, drift switches). Read by L1 checks.
- `logs/bot.jsonl` — alert frequency, broker errors, reconciliation events. Read by L1 checks via the existing log-parsing pattern used by `scripts/donchian_chase_distribution.py`.

**New in v1:**
- `strategy_lifecycle_counters` SQLite table (§12.4.1) — historical, query-shaped, growing. Aggregate counts per period; *not* per-cycle.
- `data/health_state.json` (§12.4.2) — small, slow-changing verdict persistence state (3-week NEGATIVE tracking). Not historical; rewritten in place.

The two are deliberately split: JSON for small operator-readable persistence; SQLite for anything historical/queryable. Per-cycle lifecycle data with `reasons_json` taxonomy remains follow-up §F6.

### 12.4.1 `strategy_lifecycle_counters` SQLite table (v1)

Without per-strategy signal-flow counts, L3 drift claims like "edge-filter block rate vs envelope" are aspirational. v1 ships a compact aggregate table that closes the gap. **Aggregate (not per-cycle)** — full per-cycle granularity remains follow-up §F6.

**Schema** (added via `CREATE TABLE IF NOT EXISTS` in `TradeLogger.__init__`, matches existing 11.27 migration-safe pattern):

```sql
CREATE TABLE IF NOT EXISTS strategy_lifecycle_counters (
  id                  INTEGER PRIMARY KEY,
  schema_version      INTEGER NOT NULL DEFAULT 1,
  period_start        TEXT NOT NULL,           -- ISO date, inclusive
  period_end          TEXT NOT NULL,           -- ISO date, exclusive
  period_type         TEXT NOT NULL,           -- 'weekly' | 'monthly'
  strategy_name       TEXT NOT NULL,
  raw_signals         INTEGER NOT NULL,
  regime_blocked      INTEGER NOT NULL,
  edge_filter_blocked INTEGER NOT NULL,
  sleeve_blocked      INTEGER NOT NULL,
  risk_blocked        INTEGER NOT NULL,        -- RiskManager rejections
  submitted           INTEGER NOT NULL,
  filled_entries      INTEGER NOT NULL,        -- see counting unit below
  UNIQUE(period_type, period_start, strategy_name)
);
```

**Counter semantics — read carefully, this is where ambiguity bites:**

- **Counting unit:** **one symbol-level entry candidate per increment.** Per-cycle scaling is achieved by summing across cycles in the period. Not per-cycle, not per-leg, not per-share — per (symbol, strategy, candidate-evaluation).
- **Multi-leg / options:** one strategy entry candidate counts as **1**, regardless of leg count. A credit-spread entry attempt is `raw_signals += 1`, not `raw_signals += 2`. Same for fills: `filled_entries += 1` per completed multi-leg combo fill, not per leg.
- **`filled_entries`** is explicitly named to distinguish from "shares filled" or "legs filled." Partial-quantity fills that still open the intended position count as 1; partial fills that fail to open (cancelled, expired before completion) count as 0.
- **`raw_signals`** counts symbol-level candidates *after* strategy signal generation but *before* any gate evaluation. This is the "what the strategy proposed" baseline.
- **Gate counts are mutually exclusive in time order:** `regime_blocked` is incremented only if the regime gate rejects; if regime passes but edge filter rejects, only `edge_filter_blocked` increments. Total blocks ≤ `raw_signals - submitted`.

**Durability — bot can be offline at period boundary:**

- Weekly reports aggregate by `period_start / period_end` (Mon 00:00 UTC → next Mon 00:00 UTC). The reviewer/CLI computes the period bounds first, then queries; the bot does not need to be running at the exact period boundary for the report to be correct.
- If the bot is offline for part of a period, the row reflects only the time it was running — explicitly noted in the report when `(period_end - period_start) - actual_uptime > threshold`. Honest about partial coverage rather than pretending zero counts = no signals.
- Engine writes incremental updates via upsert (`INSERT … ON CONFLICT(period_type, period_start, strategy_name) DO UPDATE SET ...`). Crash-safe: at worst we lose the deltas accumulated since the last flush.

**Failure tolerance — counters are observability, never block trading:**

- All counter writes are wrapped in a try/except that logs at WARN and continues. A counter table I/O error must never raise into the trading loop.
- Counters are *strictly observability*. They are evaluated *after* each existing gate has already decided. **Counter logic never affects whether a signal is taken.** Stated as a hard rule in §12 and reinforced in code review.

**Read pattern (assessor side):**

```python
def lifecycle_for_period(strategy: str, start: date, end: date) -> dict[str, int]:
    """Sum counter rows in [start, end). Returns 6 ints + raw_signals."""
```

The assessor then derives ratios for L3 Drift comparison against envelope bands:
- `raw_signals` vs envelope `raw_signals_per_week_band`
- `edge_filter_blocked / raw_signals` vs `edge_filter_block_rate_band`
- `regime_blocked / raw_signals` vs `regime_block_rate_band`
- `risk_blocked / raw_signals` vs `risk_block_rate_band`
- `submitted / raw_signals` vs `submitted_per_raw_signal_band`
- `filled_entries / submitted` vs `fill_rate_band`

### 12.4.2 `data/health_state.json` — small verdict persistence

Small, slow-changing, operator-readable. **Only verdict persistence state** — no historical data, no counters.

```json
{
  "schema_version": 1,
  "donchian_breakout": {
    "negative_weeks": 2,
    "last_check": "2026-05-17",
    "last_verdict": "NEGATIVE"
  },
  ...
}
```

Reset to 0 on any non-NEGATIVE check. Git-ignored.

### 12.6 Report output format

**v1: markdown only.** `data/health_reports/weekly_YYYY-WW.md` — human-readable, with YAML front-matter for parseable metadata:

```yaml
---
schema_version: 1
period_start: 2026-05-18
period_end: 2026-05-25
period_type: weekly
generated_at: 2026-05-25T22:14:00Z
---
```

Top section is the per-strategy summary table:

| Strategy | Verdict | Confidence | Sample | Key metrics | Top failure reasons | Recommendation |
|---|---|---|---|---|---|---|

Below the table, one expandable section per strategy with the full EdgeReport + HealthReport breakdown. **In-text labels** distinguish what's measured vs inferred vs from the envelope (e.g. *"Expectancy $42 (measured from 23 trades); envelope band $30–$80 (from backtest)"*) — no machine-parseable `source` field needed in v1.

JSON twin output is a follow-up; build when a consumer needs it.

---

## 12.7 Implementation-ready v1 scope

Single-list reference of exactly what ships in v1, so the eventual implementation plan has a clear target. Each item maps to a section above.

**New code modules:**
- `strategies/health/envelope.py` — `StrategyEnvelope` dataclass + JSON I/O (static, no recalibration). §7
- `strategies/health/benchmarks.py` — `equal_weight_bh_return(symbols, start, end)` over existing `data/fetcher.py`. §5.3
- `strategies/health/stats.py` — iid bootstrap CI, one-sided t-test, EMA cross (pure functions). §5.2, §9
- `strategies/health/thresholds.py` — per-strategy inline Health thresholds with TODO calibration comments. §8.1
- `strategies/health/assessor.py` — `HealthAssessor`: runs L1–L3 checks; returns `HealthReport`. §6
- `strategies/health/edge.py` — `EdgeAssessor`: computes `EdgeReport`; combines three signals into verdict. §5
- `strategies/health/persistence.py` — reads/writes `data/health_state.json` for 3-week NEGATIVE persistence. §12.4.2
- `strategies/health/lifecycle.py` — reads/writes `strategy_lifecycle_counters` table; defines counter unit semantics. §12.4.1
- `strategies/health/reports.py` — dataclasses: `HealthReport`, `EdgeReport`, `CheckResult`, `Sufficiency`, `Recommendation`.
- `strategies/health/reviewer.py` — weekly/monthly report rendering; Telegram summary. §10, §12.6

**New scripts:**
- `scripts/build_envelopes.py` — one-shot per strategy; runs single backtest at production config and writes envelope JSON.
- `scripts/strategy_health_review.py` — CLI for on-demand reviews.
- `scripts/calibrate_health_thresholds.py` — reads N weeks of paper data and prints suggested Health threshold values.

**New SQL table:**
- `strategy_lifecycle_counters` — added via `CREATE TABLE IF NOT EXISTS` in `TradeLogger.__init__`. §12.4.1

**New JSON file:**
- `data/health_state.json` — small verdict persistence state. §12.4.2

**Existing files touched (observability wiring only, no decision changes):**
- `engine/trader.py` — ~30 LOC to emit per-cycle lifecycle counter updates (after each existing gate has already decided). §12.4.1
- `forward_test.py` — wires `HealthReviewScheduler` via `engine.start(post_cycle_hook=...)`; the scheduler fires the weekly reviewer every Monday (completed Mon→Mon week) and the monthly reviewer on the first of the month. §10
  *As-built note:* an earlier draft fired the weekly hook on Sunday EOD; PR #22 moved it to Monday so the window covers a completed week aligned with the lifecycle counter table's ISO Monday `period_start`.
- `reporting/alerts.py` — five new alert types (`STRATEGY_EDGE_LOSS`, `STRATEGY_EDGE_BELOW_BENCHMARK`, `STRATEGY_HEALTH_DEGRADED`, `STRATEGY_HEALTH_BROKEN`, `STRATEGY_DRIFT_WARNING`). §11
- `dashboard.py` — new "Strategy Health & Edge" panel: compact summary table + expandable detail. §10, Q4 resolution

**Configuration:**
- `config/settings.py` — `STRATEGY_MIN_TRADES_FOR_VERDICT` dict per Q1 resolution; `HEALTH_COUNTERS_ENABLED` feature flag gating the engine lifecycle-counter wiring (temporary scaffolding).

**Outputs:**
- `data/envelopes/{strategy_name}.json` — one per strategy. `schema_version: 1`. §7
- `data/health_reports/weekly_YYYY-WW.md` — weekly markdown (written Monday for the completed week). `schema_version: 1` in front-matter. §10, §12.6
- `data/health_reports/monthly_YYYY-MM.md` — first-of-month markdown. `schema_version: 1` in front-matter.
- Telegram alerts on defined transitions (silent killer + below-benchmark only; Health alerts INFO-only when Edge positive).

**Explicitly NOT in v1** (mapped to follow-ups in [strategy_health_future.md](strategy_health_future.md)):
- PSR / DSR / MinTRL rigorous statistics (§F1)
- CUSUM / Page-Hinkley change-point detection (§F2)
- Block bootstrap replacing iid bootstrap (§F3)
- Envelope as parameter-grid distribution (§F4)
- Hybrid paper recalibration of envelope (§F5)
- Per-cycle `signal_lifecycle` table with `reasons_json` taxonomy (§F6)
- `position_eod_marks` + bar-resolution MAE/MFE (§F7)
- `HealthThresholdProfile` archetype buckets (§F8)
- Pluggable IProtection-style check architecture (§F9)
- Continuous L1/L2 per-cycle in engine snapshot (§F10)
- Credit-spread short-vol benchmark (§F11)
- JSON twin of weekly report (§F12)
- `reduce size` auto-throttle mechanic (§F13)
- Intraday excursion MAE/MFE (§F14)

---

## 13. The silent-killer case + Carver's caveat

The silent killer is the design's reason to exist. Every report (weekly markdown, dashboard, on-demand CLI) **must surface the silent-killer status prominently** — not buried in a strategy's section.

**Carver's caveat surfaced alongside every CONCLUSIVE verdict.** Carver (*Systematic Trading* ch.3) argues that for a single strategy with typical Sharpe, you cannot statistically distinguish skill from luck in under ~10 years of daily returns. This is empirically correct for trend-following on small N. Implication: even when v1 declares `CONCLUSIVE` with `STRATEGY_EDGE_LOSS`, the operator should know the underlying epistemic confidence is "reasonably sure given assumptions" — not "proven."

Mitigation: every CONCLUSIVE verdict in the report includes a footer line like:

> *Note: per Carver (Systematic Trading, ch.3), a single strategy on typical Sharpe requires ~10 years of daily returns to fully separate skill from luck. This verdict reflects our heuristic statistical confidence — operator should treat it as advisory, not proof. Recommended action: manual review of trade log before any capital change.*

---

## 14. Where industry practice is overruled

1. **Carver's "monitor portfolio Sharpe only, not per-strategy."** Inapplicable here — we have 4–5 strategies with different theses and benchmarks. Portfolio-only health would say "the book is fine" while a specific strategy bleeds. Acknowledge Carver's epistemic caution (§13) but build per-strategy anyway.
2. **Industry-standard graduated weight reduction at INDICATIVE sufficiency** ("pure advisory lets capital bleed while collecting N"). Deferred — research informs the design but does not override the user's stated risk tolerance (advisory only in v1).

---

## 15. v1-blocking open questions

These need answers before implementation starts. Follow-up open questions live in [strategy_health_future.md](strategy_health_future.md).

1. **`min_trades_for_verdict` per strategy.** Suggested starting points in §8 (30/50/50/40/50) are conservative defaults — confirm or adjust.
2. **~~Benchmark recompute cost~~** — **RESOLVED.** `data/fetcher.py` already implements per-symbol Parquet caching with merge-on-fetch over arbitrary ranges; daily bars don't change retroactively so cache warms once and stays warm. Total benchmark universe ~40–50 unique symbols across SMA/RSI/Donchian watchlists. First-time fetch ~30s; subsequent runs near-zero. `strategies/health/benchmarks.py` is a thin `equal_weight_bh_return(symbols, start, end)` helper over `fetch_symbol()`. No fetcher changes needed.
3. **~~Health WATCH vs DEGRADED vs BROKEN per check~~** — **RESOLVED.** v1 ships with sensible engineering defaults per check in `strategies/health/thresholds.py` erring toward WATCH (noisy but harmless) rather than BROKEN (cries wolf). Each default carries a `# TODO: calibrate after 4 weeks of paper` comment. A companion `scripts/calibrate_health_thresholds.py` reads N weeks of paper data and prints suggested values per check; operator runs after 4+ weeks and adjusts inline. Safe because v1 invariant is advisory-only — mis-tuned thresholds cause dashboard noise, not capital action. Per-strategy overrides for archetype-specific cases (e.g., RSI's limit-order fill rate stricter than market-order strategies').
4. **~~Dashboard layout~~** — **RESOLVED.** Compact summary table (one row per strategy, columns: Verdict / Confidence / Sample / Key metrics / Top failure reasons / Recommendation) + expandable per-strategy detail. Matches the markdown report layout exactly so operators see the same shape in both formats.
5. **~~Quarantine mechanics in v1~~** — **RESOLVED.** Operator edits `STRATEGY_ALLOCATIONS[strategy] = 0.0` in `config/settings.py` and runs `recycle_bot.sh`. Zero new code, honest about being manual. 11.11 will ship the proper re-enable workflow with hot-reload and review state machine.
6. **~~Envelope build trigger~~** — **RESOLVED.** Manual: operator runs `scripts/build_envelopes.py` when strategy config changes. No detection logic in v1. Matches existing patterns for backtest re-runs (`phase5_verify.py` etc.). Operator discipline + the calibration-script TODO comments in `thresholds.py` are the v1 reminder pattern.
7. **~~Credit-spread v1 benchmark~~** — **RESOLVED.** Underlying-BH (SPY / QQQ daily-bar BH over the assessment window) as v1 placeholder. Honest about being imperfect for a short-vol strategy; SVXY / static short-strangle replicator is documented as follow-up §F11.

---

## 16. Canonical references

- Bailey, D. H., & López de Prado, M. M. (2014). **The Deflated Sharpe Ratio.** *Journal of Portfolio Management.* — PSR, DSR, MinTRL (referenced for follow-up).
- López de Prado, M. M. (2018). **Advances in Financial Machine Learning.** Wiley. Chapters 11–17.
- Carver, R. (2015). **Systematic Trading.** Harriman House. Chapters 3–4 (small-sample realism, skill-vs-luck horizons).
- Chan, E. (2013). **Algorithmic Trading.** Wiley. Chapter 8 (equity-curve trading).
- Grinold, R. C., & Kahn, R. N. (2000). **Active Portfolio Management.** McGraw-Hill. (Benchmark selection.)
- Politis, D. N., & Romano, J. P. (1994). **The Stationary Bootstrap.** *JASA.* (Referenced for follow-up.)
- Tharp, V. (1998). **Trade Your Way to Financial Freedom.** McGraw-Hill. (R-multiple framework.)
- freqtrade `IProtection` interface — concrete API precedent: https://www.freqtrade.io/en/stable/includes/protections/
