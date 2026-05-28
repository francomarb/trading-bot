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

**Severity:** High — promoted from "deferred" because more MLEG strategies are planned, and each one will trip over this.

**Verification:**
- Hardcoded literal #1: [`engine/trader.py:2537`](engine/trader.py:2537), inside `_count_open_credit_spreads`, with NOTE comment at [`:2531`](engine/trader.py:2531).
- Hardcoded literal #2: [`engine/trader.py:3103`](engine/trader.py:3103), inside `_credit_spread_strategy_for`, with NOTE comment at [`:3096`](engine/trader.py:3096).
- The rest of the engine already dispatches MLEG behavior via duck-typing: `hasattr(strategy, "build_spread_execution")` (lines 1119, 2521) and `hasattr(strategy, "evaluate_spread_exit")` (lines 831, 930). Only the two paths above still gate on the literal.

**Fix:**
1. Add an `is_spread_strategy: bool = False` class attribute (or property) on `BaseStrategy`, set `True` on `CreditSpread` and every future MLEG strategy. Alternative: duck-type via `hasattr(strategy, "build_spread_execution")`. Pick one and apply consistently — the existing engine already uses the `hasattr` form, so going that route is the smallest diff.
2. `_count_open_credit_spreads` → rename to `_count_open_spreads` and count any `p.is_spread` position whose owner strategy is a spread strategy. Update its single caller (the global cap injected into `build_spread_execution`).
3. `_credit_spread_strategy_for(underlying)` → rename to `_spread_strategy_for(underlying)` and match by `hasattr(strategy, "build_spread_execution")` (drop the name check). Audit its callers — restart-restore in particular ([`engine/trader.py:3163`](engine/trader.py:3163)) must still find the right owner for each spread.
4. Audit `_spread_owner_strategy` and `_restore_spread_positions` paths for any other implicit single-strategy assumptions (e.g. iteration that assumes one spread type, log strings that hardcode "credit spread").

**Tests:**
- A second mock spread strategy in the slot list — `_count_open_spreads` includes it, `_spread_strategy_for` resolves to the right instance per underlying.
- Existing credit-spread tests still pass unchanged.
- Restart-from-DB path: a synthetic open spread owned by a mock second strategy is correctly re-attached.

**Coordinate with A5** — once `OptionTradeRejected` moves to a shared module, the same cleanup pass can update the spread literals.

---

### A3 — Anchor SPY-options trailing-stop activation base to the fill price

**Status:** ✅ Shipped 2026-05-27 in PR [#28](https://github.com/francomarb/trading-bot/pull/28).

**Severity (original):** Medium-High — material at live capital scale.

**Why it mattered:** `_position_base` controlled *when* the trailing stop activated (the trail floor itself reads off `_position_hwm`, so the floor was fine). But the activation threshold drifted away from actual cost basis whenever the first Black-Scholes valuation differed from the real fill — which it usually did, because (a) `_fetch_vix` uses yesterday's VIX close and (b) the underlying often moved between fill and first `inspect_open_positions`. If the first B-S value was below the real fill cost, the trailing stop activated too easily. If above, too late.

**What landed:** `SPYOptionsReversionStrategy.register_fill(occ, fill_premium)` sets `_position_base[occ] = fill_premium` and `_position_hwm[occ] = max(existing, fill_premium)` — the `max` preserves any higher HWM that `inspect_open_positions` already observed if it raced ahead of the fill confirmation. The engine's `_drain_option_fills` resolves the strategy from `decision.strategy_name` (via a new `_strategy_by_name` helper) and calls `register_fill` on FILLED / PARTIAL outcomes. The hook is opt-in via `getattr`, so strategies without trailing logic are unaffected, and any exception is caught so a `register_fill` bug cannot break the drain loop. Restored positions on engine restart receive no fill confirmation and continue using the lazy first-B-S seeding path — unchanged behavior on that path.

**Tests added:**
- `TestRegisterFill` (strategy): anchors base + HWM to premium, preserves higher prior HWM in the race case, rejects invalid premiums, end-to-end overrides the lazy first B-S seeding.
- `test_drain_option_filled_calls_register_fill_on_strategy` (engine): drain confirms a FILLED outcome calls `register_fill(occ, avg_fill_price)` on the strategy resolved from the decision's `strategy_name`.

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

**Severity:** Low standalone — but bundle with A2 since both target multi-strategy MLEG support.

**Verification:**
- Defined: [`strategies/spy_options_reversion.py:22`](strategies/spy_options_reversion.py:22).
- Imported cross-module: [`engine/trader.py:95`](engine/trader.py:95), [`tests/test_engine.py:55`](tests/test_engine.py:55), [`tests/test_spy_options_reversion.py:163`](tests/test_spy_options_reversion.py:163).

The engine importing an exception from a specific strategy module is a structural smell that becomes blocking when a second options-buying strategy ships and needs to raise the same expected-veto exception.

**Fix:** move to `strategies/base.py` (or new `strategies/exceptions.py`); update the four import sites; re-export from `strategies.spy_options_reversion` for one cycle if any external code imports it (none in this repo). ~5 line change, no behavior change.

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

Entries describe the *pre-fix* state observed during the audit. The Status column reflects what's true *now*; the Claim/Verified-at columns preserve the original finding for future re-audits.

| Item | Claim at audit time | Verified at | Status |
|---|---|---|---|
| A1 | BEAR exit override required by design | `docs/credit_spread_strategy.md:112, 407` | Design unchanged — exit override now wired (PR [#26](https://github.com/francomarb/trading-bot/pull/26)) |
| A1 | BEAR exit override absent in engine | `engine/trader.py:614, 2923-2986` (pre-PR-26) | ✅ Addressed by PR [#26](https://github.com/francomarb/trading-bot/pull/26) |
| A2 | Two hardcoded `"credit_spread"` literals remained | `engine/trader.py:2537, 3103` (pre-PR-27) | ✅ Addressed by PR [#27](https://github.com/francomarb/trading-bot/pull/27) |
| A2 | Engine already dispatched via `hasattr` elsewhere | `engine/trader.py:831, 930, 1119, 2521` | Still true — extended to the renamed helpers in PR [#27](https://github.com/francomarb/trading-bot/pull/27) |
| A3 | `_position_base` was set from first B-S, not fill | `strategies/spy_options_reversion.py:139-146` (pre-PR-28) | ✅ Addressed by PR [#28](https://github.com/francomarb/trading-bot/pull/28) |
| A4 | Call picker selected earliest expiration | `utils/options_lookup.py:185-186` (pre-PR-29) | ✅ Addressed by PR [#29](https://github.com/francomarb/trading-bot/pull/29) |
| A4 | Put-spread picker uses midpoint | `utils/options_lookup.py:455-459` | Reference design — call picker now mirrors it (PR [#29](https://github.com/francomarb/trading-bot/pull/29)) |
| A5 | `_build_quote_lookup` instantiated client per call | `strategies/spy_options_reversion.py:223, 259-297` (pre-PR-29) | ✅ Addressed by PR [#29](https://github.com/francomarb/trading-bot/pull/29) |
| A6 | `OptionTradeRejected` imported cross-module from a strategy module | `engine/trader.py:95`, `tests/test_engine.py:55`, `tests/test_spy_options_reversion.py:163` (pre-PR-27) | ✅ Addressed by PR [#27](https://github.com/francomarb/trading-bot/pull/27) — canonical location now `strategies/base.py`, re-exported for back-compat |
| A7 | `FATAL_SPREAD_PCT = 0.10` is the only hard spread cutoff (no 5% gate in strategy) | `utils/options_ranker.py:43`, `strategies/spy_options_reversion.py:213-256` | ⏸ Deferred — paper-data review per `project_options_picker_spread_watch` memory |
| N1 | Daily VIX caching is documented design | `docs/architecture.md:656`, `docs/spy_options_reversion_strategy.md:47, 214-216` | ✓ — future-work note only, no fix planned |
