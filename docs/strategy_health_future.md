# Strategy Health & Edge Monitor — Future Work / Follow-Up Roadmap

**Status:** Companion to [strategy_health_design.md](strategy_health_design.md) (the v1 design). This doc collects everything *deliberately deferred* from v1 with full design context so a future session can pick up any item cold and start building.

**Read this doc only when:** v1 is shipped, has accumulated operating experience, and a specific deferral trigger has fired. Do not pre-emptively build from here. The whole point of the v1/follow-up split is to earn each refinement with observed evidence rather than build everything on speculation.

**Invariant inherited from v1:** the **"bot informs, operator decides"** rule in `strategy_health_design.md` §1.2 applies to every item in this doc unless an item explicitly and individually gets that rule relaxed by the operator. None of the items below should be packaged together with relaxing that rule — each follow-up is its own decision.

---

## Roadmap overview

| # | Follow-up | Trigger to build | Section |
|---|---|---|---|
| F1 | PSR + DSR + MinTRL replacing simple `min_trades` floor | v1's `min_trades` proves too crude after 3+ months of paper data | §F1 |
| F2 | CUSUM / Page-Hinkley change-point detection | Silent-killer alarm fires too late or misses regime breaks visible to the operator | §F2 |
| F3 | Block bootstrap (Politis-Romano stationary) replacing iid bootstrap | CI widths visibly wrong on inspection of real data | §F3 |
| F4 | Envelope as parameter-grid distribution (PBO defense) | Point envelope shown too brittle — live keeps falling outside it for non-real reasons | §F4 |
| F5 | Hybrid paper recalibration of envelope | 6+ months of paper data AND static envelope drifts from reality | §F5 |
| F6 | `signal_lifecycle` SQL table | L3 drift detection needs longitudinal per-gate block-rate timeseries to attribute a degradation | §F6 |
| F7 | `position_eod_marks` SQL table + bar-resolution MAE/MFE | Operator wants "are stops too tight / targets too greedy" diagnostics | §F7 |
| F8 | `HealthThresholdProfile` archetype buckets | Strategy count grows past ~8 or threshold duplication becomes painful | §F8 |
| F9 | Pluggable `IProtection`-style check architecture | Check count exceeds ~15 or third-party strategy contributions need custom checks | §F9 |
| F10 | Continuous L1/L2 per-cycle in engine state snapshot | Operator wants real-time dashboard health (not just weekly) | §F10 |
| F11 | Credit-spread short-vol benchmark (SVXY / static short-strangle replicator) | Underlying-BH benchmark shown to misclassify spread P&L vs underlying delta | §F11 |
| F12 | Machine-readable JSON twin of weekly report | A consumer exists (11.9 dynamic allocation, external dashboard) | §F12 |
| F13 | `reduce size` auto-throttle mechanic | Silent-killer alarm fires too late under pure-advisory AND capital bleeds noticeably between alarm and operator response | §F13 |
| F14 | Intraday excursion capture for true MAE/MFE | Bar-resolution MAE/MFE shown diagnostically insufficient | §F14 |

---

## §F1 — PSR + DSR + MinTRL replacing `min_trades` floor

**v1 baseline:** hand-picked `min_trades_for_verdict` per strategy (e.g. 30/50/50/40/50). Honest about being a heuristic.

**Rigorous replacement:** **Minimum Track Record Length (MinTRL)** from Bailey & López de Prado (2014):

> Given target Sharpe ≥ S*, observed skew, and observed kurtosis, MinTRL gives the smallest N such that PSR(S*) ≥ confidence level.

Replace the hand-picked floor with `MinTRL(target_sharpe, observed_skew, observed_kurtosis, confidence=0.95)`. Per-strategy MinTRL is computed dynamically as observations accumulate.

**Verdict layer extension:** add **Probabilistic Sharpe Ratio (PSR)** as a primary Edge signal — returns `P(true Sharpe > benchmark | N, skew, kurtosis)`. Works on small N. Output is a probability that slots into the sufficiency framework.

**Multiple-testing correction:** **Deflated Sharpe Ratio (DSR)** — additionally penalizes for the multiple-testing bias of how many strategy variants were researched. The strategy's research history (parameter sweeps, watchlist iterations) feeds the deflation count.

**Open question (blocks this sub-item):** MinTRL target Sharpe per strategy. Bailey-LdP MinTRL requires a target Sharpe to test against. Options: (a) each strategy's own backtest Sharpe; (b) fixed `S* = 0.5` as minimum-acceptable floor; (c) per-strategy config. Per-strategy config is most flexible but requires a decision per strategy.

**Practical implication after MinTRL adoption** (rough order of magnitude, target Sharpe 1.0):

| Strategy | Trades/year | Time to CONCLUSIVE |
|---|---|---|
| SMA crossover | 10–30 | 3–10 years |
| RSI reversion | 30–100 | 1–3 years |
| Donchian breakout | 50–150 | 1–2 years |
| SPY options reversion | 50–150 | 1–2 years |
| Credit spreads | 60–200 | 1–2 years |

These are *more conservative* than v1's hand-picked floors — MinTRL is correctly stricter. This is the rigorous answer to Carver's "~10 years" caveat.

---

## §F2 — CUSUM / Page-Hinkley change-point detection

**v1 baseline:** EMA50/EMA100 crossover as assumption-free decay detector.

**Rigorous addition:** **CUSUM (Cumulative Sum) or Page-Hinkley change-point statistic** on the per-strategy equity curve. Reference: López de Prado AFML ch.17 ("Structural Breaks"). Detects structural breaks faster than rolling Sharpe and faster than EMA cross on sparse-trade strategies.

**Why complementary to PSR/DSR:** PSR/DSR have distributional assumptions (Sharpe distribution conditional on N, skew, kurtosis). CUSUM has none — it just detects when the mean of the increment process changes. Industry standard at quant funds for live decay detection.

**Integration:** add to §9.2 of the v1 doc's combine logic — `STRATEGY_EDGE_LOSS` requires all of `{PSR < 0.5, DSR < 0.5, CUSUM downward break detected, EMA50/100 cross}` to agree. Same conservative-AND philosophy as v1.

**Open question:** CUSUM parameters (threshold `h`, drift `k`) need calibration per strategy. Probably tied to envelope expectancy and standard deviation. Calibration script needed.

---

## §F3 — Block bootstrap replacing iid bootstrap

**v1 baseline:** iid bootstrap on trade returns for expectancy/profit-factor CIs.

**Rigorous replacement:** **Politis-Romano stationary bootstrap** (1994). Trade returns are not independent — autocorrelated by regime. iid bootstrap underestimates CI width during regime persistence (most of the time). Block bootstrap respects the autocorrelation structure.

**Implementation cost:** ~50 LOC in `strategies/health/stats.py`; well-documented method. Block length parameter `L` defaults to a geometric distribution centered at ~5 trades.

**Trigger to build:** CI widths visibly wrong on inspection of real data. Easy to test — compare iid vs block bootstrap CIs on the same window of paper trades; if they materially disagree in operator-relevant ways, swap.

---

## §F4 — Envelope as parameter-grid distribution (PBO defense)

**v1 baseline:** point envelope from single backtest at production config.

**Rigorous replacement:** envelope as a **distribution over backtest hyperparameters**, not a point. This is the **Probability of Backtest Overfitting (PBO) defense** from López de Prado AFML ch.14 — comparing live behavior to the *distribution* of plausible backtest outcomes across the parameter grid, not the single chosen-hyperparameter point.

`backtest/runner.py` already supports parameter-grid sweeps. The envelope build process runs the grid, collects the per-cell distribution of:

- Expectancy (mean, median, p10, p90)
- Win rate (mean, CI)
- Hold time (median, p10, p90)
- Trades per month (mean, p10, p90)
- p95 drawdown (mean, p90 — worst-case envelope)
- Sharpe (mean, p10, p90)

Stored as versioned JSON in `data/envelopes/{strategy_name}_v{N}.json`.

**Open question (blocks this sub-item):** envelope grid scope per strategy. What hyperparameter ranges go into the sweep? E.g., for SMA crossover: SMA periods (20-30, 50-200), watchlist subsetting, regime mask on/off? Decision affects how much realized-paper variance the envelope tolerates.

**Trigger to build:** point envelope shown too brittle — live keeps falling outside it for non-real reasons. If this happens, the upgrade path is direct because `backtest/runner.py` already supports grid sweeps.

---

## §F5 — Hybrid paper recalibration of envelope

**v1 baseline:** static envelope, regenerated only by operator-run `scripts/build_envelopes.py`.

**Future addition:** blend paper distribution into the envelope after sufficient sample. Recalibration is the **most dangerous piece of the design** — "envelope laundering" is the failure mode where bad live behavior gets quietly accepted into the expected band, defeating the silent-killer alarm.

**Guardrails required:**

- Versioned files (`data/envelopes/{strategy_name}_v{N}.json`) — every recalibration writes a new file; old preserved for audit.
- If recalibration would move the envelope materially (e.g. expectancy CI shifts > X%), alert operator and **require operator approval before adoption**. Do not auto-apply.
- Audit log of every recalibration: old envelope → new envelope → triggering paper sample.

**Open questions (block this sub-item):**

- Recalibration trigger N per strategy (likely tied to MinTRL).
- Blending weight (50/50? Bayesian update with backtest as prior?).
- Material-shift threshold for the approval gate.

**Trigger to build:** 6+ months of paper data AND static envelope is shown to drift from reality in operator-relevant ways. Until then, recalibration is solving a hypothetical problem.

---

## §F6 — `signal_lifecycle` SQL table

**v1 baseline:** no signal lifecycle counters; L3 drift detection uses log-parsing for ad-hoc audits when needed.

**Future addition:** per-cycle counters of how many signals progressed through each gate. Without this, L3 drift detection cannot attribute a change in fill rate to a specific cause (regime gate? edge filter? sleeve full?).

**Schema:**

```sql
signal_lifecycle (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                  -- ISO timestamp
  cycle_id TEXT,                     -- engine cycle UUID
  strategy_name TEXT NOT NULL,
  raw_signals INTEGER NOT NULL,      -- strategy.generate_signals() count
  edge_filtered INTEGER NOT NULL,    -- after edge filter
  regime_passed INTEGER NOT NULL,    -- after regime gate
  sleeve_passed INTEGER NOT NULL,    -- after sleeve allocator
  risk_passed INTEGER NOT NULL,      -- after RiskManager.evaluate
  submitted INTEGER NOT NULL,        -- orders actually submitted
  filled INTEGER NOT NULL,           -- orders filled this cycle
  reasons_json TEXT                  -- aggregated block-reason counts
)
```

Indexed on `(strategy_name, ts)`. Rolled up by the assessor into block-rate distributions per gate per window.

**Migration approach:** `CREATE TABLE IF NOT EXISTS` in `TradeLogger.__init__` (matches existing 11.27 migration-safe ALTER pattern). No backfill; table populates as the engine runs.

**Open questions (block this sub-item):**

- **Granularity.** Per-cycle row gives high fidelity but high row volume (every 5-minute cycle × N strategies). Acceptable, or roll up to per-symbol-per-day?
- **`reasons_json` enum stability.** Block reasons today are free-form strings in `EdgeFilterDecision.reasons`. Stable enumeration needed for longitudinal counting — otherwise reason taxonomy drift breaks drift detection. Worth a small reason-code enumeration pass across all active edge filters before the schema lands.

**Trigger to build:** L3 drift detection actually needs longitudinal per-gate block-rate timeseries to attribute a degradation. If v1's ad-hoc log-parsing answers operator questions adequately, this stays deferred.

---

## §F7 — `position_eod_marks` table + bar-resolution MAE/MFE

**v1 baseline:** no MAE/MFE; the L3 drift check explicitly notes this gap.

**Future addition:** once-per-session end-of-day mark-to-market per open position. Enables next-best MAE/MFE approximation since we don't capture intraday excursion.

**Schema:**

```sql
position_eod_marks (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                  -- session close timestamp
  position_id TEXT NOT NULL,         -- FK to trades.position_id (11.27)
  strategy_name TEXT NOT NULL,
  symbol TEXT NOT NULL,
  mark_price REAL NOT NULL,
  unrealized_pnl REAL NOT NULL
)
```

Indexed on `(position_id, ts)`. Written by `engine/trader.py` at session close. Bar-resolution MAE = `min(unrealized_pnl)` over position lifetime; bar-resolution MFE = `max(unrealized_pnl)`.

**Why it matters:** MAE/MFE are diagnostic gold for whether stops are too tight, targets too greedy, or holding times misaligned with the realized trade arc. Direct operator value once it exists.

**Open question (blocks this sub-item):** EOD mark price source. For options, mark = OPRA snapshot at session close. Equity mark = last bar close. Single fetch at 16:00 ET in a fresh engine path, or piggyback on existing close-of-session logic? Latency/reliability tradeoff for a once-daily write.

**Trigger to build:** operator wants "are stops too tight / targets too greedy" diagnostics — not on day 1.

---

## §F8 — `HealthThresholdProfile` archetype buckets

**v1 baseline:** inline thresholds per strategy in `strategies/health/thresholds.py` as flat dicts. With 5 strategies, cleaner than abstracting.

**Future refactor:** group strategies into archetype buckets with shared default threshold profiles; per-strategy override is a thin dict that merges over the bucket default.

| Archetype | Strategies in bucket | Distinctive expectations |
|---|---|---|
| **Trend-following equities** | SMA crossover, Donchian breakout | Wide expected hold time; sparse signals → small-N tolerance; can chase entries up to a cap (11.32); higher slippage tolerance acceptable on momentum names |
| **Mean-reversion equities** | RSI reversion | Limit orders → fill rate is critical health metric; entry slippage near zero (passive fills); earnings blackout discipline → blackout violations are L1 BROKEN |
| **Breakout trend** | Donchian breakout (sub-bucket of trend) | Watch entry chase tightly; signal clustering on universe-wide breakout days is expected, not anomalous |
| **Options long premium** | SPY options reversion | Spread quality is dominant L2 metric; theta decay → expected daily MTM drift; premium-paid efficiency in EdgeReport |
| **Options short premium / multi-leg** | Credit spreads | Atomic combo fill rate; net credit captured vs spread width; assignment risk near expiry as L1 check |

Per-bucket profile stored as a `HealthThresholdProfile` dataclass in `strategies/health/profiles.py`.

**Open question (blocks this sub-item):** initial archetype threshold values need calibration against existing paper data — not just guessed. Calibration script needed per archetype.

**Trigger to refactor:** strategy count grows past ~8 or threshold duplication becomes painful.

---

## §F9 — Pluggable `IProtection`-style check architecture

**v1 baseline:** Health checks inlined in `HealthAssessor`. With ~10–15 checks total, no abstraction needed.

**Future refactor:** modeled on **freqtrade's `IProtection` interface** — each check is a pluggable class with `name`, `check(state) -> CheckResult`. Reference: https://www.freqtrade.io/en/stable/includes/protections/

**API sketch:**

```python
class HealthCheck(Protocol):
    name: str
    layer: Layer  # L1 / L2 / L3
    def check(self, state: AssessmentInput) -> CheckResult: ...
```

`HealthAssessor` iterates registered checks instead of calling each one explicitly. Third-party strategy contributions can register their own checks without modifying the assessor.

**Trigger to refactor:** check count exceeds ~15 or third-party strategy contributions need custom checks.

---

## §F10 — Continuous L1/L2 per-cycle in engine state snapshot

**v1 baseline:** health assessment runs weekly only. Engine state snapshot does not carry health status.

**Future addition:** L1/L2 cheap checks run every cycle, cached in `TradingEngine` state snapshot, surfaced on dashboard in near-real-time, Telegram on state transition only.

**Engine surface area:** new `HealthAssessor.assess_l1_l2_cheap(state)` call after each cycle; result cached in `engine_state.json["strategy_health"]`. Dashboard reads the same key.

**Trigger to build:** operator wants real-time dashboard health (not just weekly). Probably comes up after first real silent-killer event when the weekly cadence feels too slow.

---

## §F11 — Credit-spread short-vol benchmark

**v1 baseline:** underlying buy-and-hold (SPY / QQQ) over the assessment window as credit-spread benchmark.

**Rigorous replacement:** **short-vol replicator** — either SVXY (passive short-VIX-futures ETF) or a static short-strangle replicator constructed on the same underlying. Reference: Grinold-Kahn (canonical benchmark selection).

**Why it matters:** credit spreads capture the volatility risk premium. The economically right benchmark is "a passive way to capture short-vol P&L" — SVXY/replicator — not "the underlying's directional return." A spread strategy that returns +12% in a year where SVXY returned +18% is destroying value even though the raw P&L is positive against SPY.

**Trigger to build:** underlying-BH benchmark shown to misclassify spread P&L vs underlying delta — i.e., spreads look fine vs SPY but bad vs SVXY, or vice versa, in a way that materially changes the recommendation.

---

## §F12 — Machine-readable JSON twin of weekly report

**v1 baseline:** markdown only.

**Future addition:** `data/health_reports/weekly_YYYY-WW.json` with structured data + `source: measured | inferred | envelope` field per numeric metric + `schema_version` field so future consumers can detect breaks.

**Why it matters:** 11.9 dynamic allocation will need structured input. External dashboard might too. JSON twin makes both straightforward.

**Trigger to build:** a consumer exists. Pure markdown is enough for the human operator; building JSON before a consumer is YAGNI.

---

## §F13 — `reduce size` auto-throttle mechanic

**v1 baseline:** `reduce size` is emitted as advisory text. Operator manually edits `STRATEGY_ALLOCATIONS` if they agree.

**Future addition:** auto-throttle that applies a specific weight multiplier to the allocator automatically. Pattern matches existing automated risk controls (loss-streak cooldown, slippage drift kill switch) — small, well-understood, paper-validated, individually opt-in.

**Required for build:**

- Specific weight-multiplier schedule (e.g., DEGRADED → 0.75×, BROKEN-but-Edge-positive → 0.5×, BELOW-BENCHMARK → 0.5×). Schedule itself needs calibration.
- **Auto-restore-to-baseline trigger** after N healthy weekly checks. Without this, auto-throttle becomes a one-way ratchet.
- **Hard floor** (never below 25% weight without operator confirm). Prevents the "10% of 10% of 10%" cascade.
- Per-feature opt-in config flag — operator turns it on per strategy.
- Audit log of every auto-throttle action.

**This is the item that relaxes the v1 "bot informs, operator decides" invariant.** Should be packaged with explicit operator approval as a discrete sub-item, not bundled with anything else.

**Open question (blocks this sub-item):** how do we measure "silent-killer alarm fired too late"? Probably "operator manually reduced size N weeks after the alarm fired and the data supported reducing earlier." That measurement needs to be recorded during v1 operation so this sub-item is data-driven, not a vibes call.

**Trigger to build:** silent-killer alarm fires too late under pure-advisory AND capital bleeds noticeably between alarm and operator response. Until we have lived experience showing this is a real problem, do not build.

---

## §F14 — Intraday excursion capture for true MAE/MFE

**Prerequisite:** §F7 (`position_eod_marks` bar-resolution MAE/MFE).

**Future addition:** capture true intraday MAE/MFE per position (not bar-resolution). Requires either a stream tap on existing positions or a fast polling loop on quotes.

**Why deferred behind §F7:** significant engine surface area. The bar-resolution version is honest about the resolution gap and may be operationally sufficient. Only build intraday if the resolution gap is shown to miss something diagnostically important.

**Trigger to build:** bar-resolution MAE/MFE shown diagnostically insufficient — operator regularly wants to know "did this trade actually hit the stop intraday but recover?" and bar marks can't answer.

---

## Open questions (follow-up only)

These block their respective sub-items, not v1:

- **MinTRL target Sharpe per strategy** — blocks §F1
- **Combine logic for 4-signal NEGATIVE verdict (PSR + DSR + CUSUM + EMA)** — blocks §F1 + §F2 integration
- **CUSUM parameters per strategy** — blocks §F2
- **Block length `L` for stationary bootstrap** — blocks §F3
- **Envelope grid scope** (which hyperparameters sweep) — blocks §F4
- **Recalibration trigger N + blending weight + material-shift threshold** — blocks §F5
- **`signal_lifecycle` granularity** (per-cycle vs per-symbol-per-day) — blocks §F6
- **`reasons_json` enum stability** (block reason code taxonomy) — blocks §F6
- **EOD mark price source** — blocks §F7
- **`HealthThresholdProfile` archetype initial values** — blocks §F8
- **Auto-throttle weight-multiplier schedule + restore trigger + floor** — blocks §F13
- **Operationally measuring "silent-killer alarm fired too late"** — blocks §F13

---

## Canonical references (for follow-up work)

- Bailey, D. H., & López de Prado, M. M. (2014). **The Deflated Sharpe Ratio.** *Journal of Portfolio Management.* — PSR, DSR, MinTRL.
- López de Prado, M. M. (2018). **Advances in Financial Machine Learning.** Wiley. Chapters 11–17 (backtesting, overfitting, DSR/PSR, structural breaks/CUSUM, PBO).
- Carver, R. (2015). **Systematic Trading.** Harriman House. Chapters 3–4 (small-sample realism, graduated sizing, skill-vs-luck horizons).
- Chan, E. (2013). **Algorithmic Trading.** Wiley. Chapter 8 (equity-curve trading, stop-loss-on-strategy).
- Pardo, R. (2008). **The Evaluation and Optimization of Trading Strategies.** Wiley. (Walk-forward efficiency.)
- Politis, D. N., & Romano, J. P. (1994). **The Stationary Bootstrap.** *Journal of the American Statistical Association.*
- Grinold, R. C., & Kahn, R. N. (2000). **Active Portfolio Management.** McGraw-Hill. (Benchmark selection.)
- Tharp, V. (1998). **Trade Your Way to Financial Freedom.** McGraw-Hill. (Equity-curve trading folklore.)
- freqtrade `IProtection` interface — concrete API precedent: https://www.freqtrade.io/en/stable/includes/protections/
