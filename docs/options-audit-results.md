# Options Strategies Audit — Addendum (Phase 11)

**Date:** 2026-05-27
**Companion to:** `options_strategies_audit_results.md`
**Purpose:** Verified, actionable items only, ordered for Phase 11 follow-up. Each item has its claim checked against the actual code at the cited line numbers; verification notes are kept inline so future readers can re-check without redoing the work.

This addendum supersedes the priority matrix in the original document. Of the original 15 numbered issues + 2 missing-feature notes, **7 are actionable** (6 from the original audit + PLAN.md 11.31 promoted from "tracked"), **1 is documented design** (kept as a future-work note), and the rest are dropped — see `## Rejected / De-prioritized` for reasoning per item.

**Context for ordering:** the user has signalled that more MLEG (multi-leg) options strategies are coming. That promotes 11.31 from "deferred" to "do before the next MLEG strategy lands," and it gives a natural home for A5.

---

## Actionable issues (priority order)

### A1 — Wire the BEAR mid-trade regime exit for credit spreads

**Severity:** High — designed, documented, currently absent.

**Verification:**
- Design says it must exist: [`docs/credit_spread_strategy.md:112`](docs/credit_spread_strategy.md:112) — *"Regime exit | Regime shifts to BEAR mid-trade | Defensive override. Exits are never blocked by regime gate."*
- Design lists it as a tested behavior: [`docs/credit_spread_strategy.md:407`](docs/credit_spread_strategy.md:407) — *"Regime exit override — exits even when entries are blocked."*
- Engine does not implement it: [`engine/trader.py:614`](engine/trader.py:614) only says *"Exits are never blocked by regime"* (a passive guarantee, not the required override). [`engine/trader.py:2923-2986`](engine/trader.py:2923) (`_process_credit_spread_exits`) delegates entirely to `strategy.evaluate_spread_exit`, which evaluates only profit target / stop loss / time stop / short breach — no regime check.

**Fix:** thread `current_regime` (already computed in `_run_one_cycle`) into `_process_credit_spread_exits` and short-circuit `should_exit = True, reason = "regime shift to BEAR — defensive override"` when `current_regime == MarketRegime.BEAR` and the position has no close in flight. Skip the quote lookup on the BEAR-override path so a quote outage cannot suppress the defensive exit.

**Tests:**
- BEAR regime + open spread → exit dispatched once, reason logged.
- BEAR regime + spread already in `_spreads_pending_close` → no duplicate dispatch.
- Non-BEAR regime → falls through to normal `evaluate_spread_exit` path (unchanged).
- BEAR regime + quote outage → still exits.

---

### A2 — Generalize the hardcoded `"credit_spread"` literals (PLAN.md 11.31)

**Status:** ✅ Shipped 2026-05-27 in PR [#27](https://github.com/francomarb/trading-bot/pull/27).

**Severity (original):** High — promoted from "deferred" because more MLEG strategies are planned, and each one would trip over this.

**What landed:**
- `_count_open_credit_spreads` → `_count_open_spreads`. Counts every `p.is_spread` position. All spread strategies now contribute to the same global MLEG concurrent total, which is the resource they actually share (execution slots, buying power).
- `_credit_spread_strategy_for` → `_spread_strategy_for(underlying, *, strategy_name=None)`. Matches any slot whose strategy exposes `build_spread_execution`. The `strategy_name` argument is used by the restart path so the DB row's recorded strategy name disambiguates when two spread strategies cover the same underlying — preventing a second strategy from accidentally claiming the first's positions.
- The kwarg `total_open_credit_spreads` on `CreditSpread.build_spread_execution` is **unchanged** — that's the strategy's contract. Generalize when strategy #2 lands. The `STRATEGY_WATCHLISTS` config literal is also unchanged — it's a config-shape concern, not engine plumbing.

**Tests added (`TestSpreadStrategyFor`):** duck-type resolution against a non-`credit_spread` name; name disambiguation between two spread strategies on the same underlying; rejection of single-leg strategies (no `build_spread_execution`). Plus the renamed `TestCountOpenSpreads` proving every spread strategy contributes to the count.

---

### A3 — Anchor SPY-options trailing-stop activation base to the fill price

**Severity:** Medium-High — material at live capital scale.

**Verification:** [`strategies/spy_options_reversion.py:139-146`](strategies/spy_options_reversion.py:139):
```python
if occ not in self._position_base:
    self._position_base[occ] = opt_val           # first B-S valuation, NOT fill premium
self._position_hwm[occ] = max(self._position_hwm.get(occ, opt_val), opt_val)
base = self._position_base[occ]
hwm = self._position_hwm[occ]
if hwm >= base * (1.0 + self.trail_activation_pct):
    trail_floor = hwm * (1.0 - self.trail_pct)
```

`base` controls **when the trailing stop activates** (the trail floor itself uses `hwm`, not `base`, so the floor is fine). But the activation threshold drifts away from the position's actual cost basis whenever the first B-S valuation differs from what we paid — which it usually does because (a) `_fetch_vix` uses yesterday's VIX close, and (b) the underlying has often moved between fill and first `inspect_open_positions` call.

Practical effect: if the first B-S value is below the real fill cost, the trailing stop activates too easily (and may exit a position that is still underwater relative to entry). If it is above, the trailing stop activates too late.

**Fix:** add `register_fill(occ, fill_premium)` on the strategy and call it from the engine's option-fill confirmation path (`_drain_option_fills` or wherever the buy fill is observed). On the first `inspect_open_positions` call for that OCC, prefer the registered fill premium over the live B-S valuation. Keep current behavior as a fallback for positions restored from the broker on startup (no fill premium known).

**Tests:**
- `register_fill` sets `_position_base` and `_position_hwm` to the fill premium.
- A subsequent `inspect_open_positions` call does not overwrite the registered base.
- Without `register_fill`, behavior matches today (back-compat for restored positions).

---

### A4 — `find_best_call` should pick the expiration closest to the DTE-window midpoint

**Severity:** Medium — improves DTE consistency and matches the put-spread picker.

**Verification:**
- Call picker (broken): [`utils/options_lookup.py:185-186`](utils/options_lookup.py:185) — `expirations = sorted(...); best_expiry = expirations[0]` always selects the nearest expiration.
- Put-spread picker (already correct): [`utils/options_lookup.py:455-459`](utils/options_lookup.py:455):
  ```python
  target_dte = (min_dte + max_dte) / 2.0
  chosen_expiry = min(by_expiry.keys(), key=lambda exp: abs((exp - now).days - target_dte))
  ```
- For SPY (multiple weekly expirations in the configured 14–28 DTE window), the picker takes the 14-DTE expiration even when a 21-DTE expiration is tighter and gives better theta-per-dollar.

**Fix:** copy the put-spread picker's midpoint selection into `find_best_call`. Three-line change.

**Tests:** given two candidate expirations bracketing the midpoint, the picker returns the one with smaller `|dte − midpoint|`.

---

### A5 — Inject `quote_lookup` into `SPYOptionsReversionStrategy`

**Severity:** Medium — symmetry with `CreditSpread`, better testability, removes per-bar client churn.

**Verification:** [`strategies/spy_options_reversion.py:223`](strategies/spy_options_reversion.py:223) — `quote_lookup = _build_quote_lookup()` is called inside `build_option_execution`, and `_build_quote_lookup` itself ([`:259-297`](strategies/spy_options_reversion.py:259)) instantiates a fresh `OptionHistoricalDataClient` every call. At 5-min cycles this is wasteful but not broken; the real value is symmetry: `CreditSpread` accepts an injected `quote_lookup` and tests rely on injecting a stub.

**Fix:** accept an optional `quote_lookup` in `__init__`, defaulting to `_build_quote_lookup()` produced once. Mirror the `CreditSpread` constructor signature. Wire the engine to construct a single shared lookup at startup and inject it into both options strategies.

**Tests:** existing unit tests get a stub `quote_lookup` directly via constructor instead of monkey-patching; production path unchanged.

---

### A6 — Move `OptionTradeRejected` out of `spy_options_reversion.py`

**Status:** ✅ Shipped 2026-05-27 in PR [#27](https://github.com/francomarb/trading-bot/pull/27).

**Severity (original):** Low standalone — bundled with A2 since both target multi-strategy MLEG support.

**What landed:** `OptionTradeRejected` now lives in `strategies/base.py`. The engine and `tests/test_engine.py` import it from the canonical location. `strategies/spy_options_reversion.py` re-exports it via `__all__` so `from strategies.spy_options_reversion import OptionTradeRejected` continues to work — existing callers and `tests/test_spy_options_reversion.py` are unchanged.

---

### A7 — Tighten `FATAL_SPREAD_PCT` after paper-data review

**Severity:** Low — already on the watch list per memory `project_options_picker_spread_watch`.

**Verification (and correction of the original audit's claim):**
- [`utils/options_ranker.py:43`](utils/options_ranker.py:43) — `FATAL_SPREAD_PCT = 0.10`
- [`utils/options_ranker.py:44`](utils/options_ranker.py:44) — `SOFT_SPREAD_PCT = 0.05` (scoring only, not a hard filter)
- **The original audit (and my first addendum) incorrectly claimed the SPY-options strategy enforces its own 5% spread gate downstream in `build_option_execution`. It does not.** I re-read [`strategies/spy_options_reversion.py:213-256`](strategies/spy_options_reversion.py:213): the strategy only checks `notional_cap > 0` and `premium > 0`. `pick.spread_pct` is logged but never gated. So `FATAL_SPREAD_PCT = 0.10` is the **only** hard spread cutoff for SPY calls.

That makes the 10% threshold more load-bearing than the audit assumed: there is no second-line defense. It was deliberately relaxed from 5% during 11.25 (`project_options_picker_spread_watch`).

**Fix (only after paper-data confirmation):** consider lowering `FATAL_SPREAD_PCT` to ~0.06 (still above SOFT_SPREAD_PCT for graceful scoring) **or** make it a per-call parameter so each strategy can pass its own ceiling.

**Pre-requisite:** review actual SPY-options fills from paper trading to confirm what spread% the filled contracts have been transacting at. Do not tighten without that data.

---

## Future-work note (not actionable in 11.x)

### N1 — VIX `sigma` cached daily — backtest-faithful but lags intraday vol events

[`strategies/spy_options_reversion.py:168-183`](strategies/spy_options_reversion.py:168) caches the VIX close once per calendar day. The original audit flagged this as "stale during intraday vol spikes." After verification, this is documented design, not an oversight:
- [`docs/architecture.md:656`](docs/architecture.md:656) — *"yfinance | VIX daily fetch for Black-Scholes sigma input (options only)."*
- [`docs/spy_options_reversion_strategy.md:47`](docs/spy_options_reversion_strategy.md:47) — *"Black-Scholes Delta < 0.30 (uses VIX as implied vol, cached daily)."*
- [`docs/spy_options_reversion_strategy.md:214-216`](docs/spy_options_reversion_strategy.md:214) — backtest used daily SPY+VIX bars.

Switching to intraday-fresh VIX would create a backtest/live data-cadence mismatch in the exit guards. **Not actionable as a defect.** If we later want intraday freshness, pair it with a re-backtest using the same data cadence the live engine will see.

File against future work; do not fix in Phase 11.

---

## Rejected / De-prioritized (from the original audit)

| Orig # | Reason dropped |
|---|---|
| **#11** (`opening=True` on close legs is "high severity, may cause Alpaca rejection") | **Wrong.** [`execution/broker.py:1102-1111`](execution/broker.py:1102) already flips legs when `closing=True`. The engine even comments this contract at [`engine/trader.py:2960`](engine/trader.py:2960). No rejection risk. |
| **#9** (`should_exit_spread` triggers false profit-target at `spread_mid == 0`) | **Wrong economics.** A real `spread_mid` of 0 means the spread can be closed for $0 — that *is* maximum profit, exiting is correct. Missing-quote case is already guarded in `evaluate_spread_exit`. A defensive `spread_mid < 0` guard is fine to add but the framing is mistaken. |
| **#10** (IV fails open on fetch failure) | **Design choice, not a bug.** Fallback (15.0) is intentional, docstring states it explicitly. Fail-open vs fail-closed is a separate policy discussion, not a Phase 11 fix. |
| **#12** (DTE stagger off-by-one) | **Intentional** per the audit's own admission. `< gap` is correct per the config wording "at least 7 days." |
| **#8** (string matching in `_caps_reject_reason`) | **Premature.** Strings are stable; the enum/tuple refactor adds complexity without solving a current problem. Revisit when the messages need to change. |
| **#14** (two independent VIX caches) | **Cosmetic.** Becomes a near-free cleanup once A5 lands and the shared `IVProxyResolver` can be injected at the same time. Not standalone work. |
| **#15** (no guard against per-share `max_premium_per_contract`) | **Defensive-only warning.** No real bug; the math is correct. Skip. |
| **#3** (calendar-day Wednesday computation) | **Real but low-value.** Holiday edge case 3–4 times/year, worst case is exiting one trading day early. Not worth the complexity. |
| **Missing — IV rank filter for long calls** | **Enhancement, not a defect.** Real idea worth backtesting separately; not in Phase 11 scope. |

---

## Suggested PR slicing

| PR | Contains | Rationale |
|---|---|---|
| 1 | **A1** alone | Defensive exit behavior. Isolate so it can be paper-verified in its own window before any other spread changes ship. |
| 2 | **A2 + A6** | Both target multi-MLEG readiness and touch overlapping plumbing (`engine/trader.py` spread dispatch + `OptionTradeRejected` location). One pass = no rework. |
| 3 | **A3** alone | Trailing-stop semantics for SPY options. Needs its own paper-validation window before live to confirm exits behave as expected. |
| 4 | **A4 + A5** | Both touch `find_best_call` / SPY-options-strategy construction. Land together. |
| 5 | **A7** | Single-constant change. Merge only after paper-fill data review. |

**N1** stays in the future-work backlog.

---

## Verification ledger (for future re-audits)

| Item | Claim | Verified at | Verdict |
|---|---|---|---|
| A1 | BEAR exit override required by design | `docs/credit_spread_strategy.md:112, 407` | ✓ |
| A1 | BEAR exit override absent in engine | `engine/trader.py:614, 2923-2986` | ✓ |
| A2 | Two hardcoded `"credit_spread"` literals remain | `engine/trader.py:2537, 3103` | ✓ |
| A2 | Engine already dispatches via `hasattr` elsewhere | `engine/trader.py:831, 930, 1119, 2521` | ✓ |
| A3 | `_position_base` set from first B-S, not fill | `strategies/spy_options_reversion.py:139-146` | ✓ |
| A4 | Call picker selects earliest expiration | `utils/options_lookup.py:185-186` | ✓ |
| A4 | Put-spread picker uses midpoint | `utils/options_lookup.py:455-459` | ✓ |
| A5 | `_build_quote_lookup` instantiates client per call | `strategies/spy_options_reversion.py:223, 259-297` | ✓ |
| A6 | `OptionTradeRejected` imported cross-module | `engine/trader.py:95`, `tests/test_engine.py:55`, `tests/test_spy_options_reversion.py:163` | ✓ |
| A7 | `FATAL_SPREAD_PCT = 0.10` is the only hard spread cutoff (no 5% gate in strategy) | `utils/options_ranker.py:43`, `strategies/spy_options_reversion.py:213-256` | ✓ (and corrects the original audit) |
| N1 | Daily VIX caching is documented design | `docs/architecture.md:656`, `docs/spy_options_reversion_strategy.md:47, 214-216` | ✓ |
