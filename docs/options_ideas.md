# Options Strategy Ideas — Analysis & Forward Ledger

> Deep think on what options strategies could complement the bot's existing
> portfolio, grounded in current architecture and capital posture. Promote any
> entry to PLAN.md when picked up; this doc is for shaping the decision, not
> tracking work.

---

## 1. What the current portfolio actually covers

### Strategies in production
| Strategy | Direction | P&L source | Regime gate |
|---|---|---|---|
| SMA Crossover | Long equity | Trend | TRENDING + RANGING |
| RSI Reversion | Long equity | Mean reversion | TRENDING + RANGING |
| Donchian Breakout | Long equity | Trend continuation | TRENDING only |
| SPY Options Reversion | Long calls | Directional + delta | TRENDING + RANGING |
| Credit Spread (bull put) | Bullish/neutral | Short premium (theta + IVR) | TRENDING + RANGING |

### What this mix is actually betting on
- **Bullish or neutral underlying drift.** Every strategy needs the market to
  stop falling. None makes money on the way down.
- **Normal to elevated IV** for the short-premium leg; **normal** IV for
  long-direction equity.
- **TRENDING and RANGING regimes** — VOLATILE and BEAR are deliberately
  unstaffed. Capital sits in cash when the regime detector says either.

### Gaps worth naming explicitly
1. **No income on the long equity book.** SMA + Donchian hold shares
   continuously for weeks. None of that capital generates premium while it
   waits for the trend to mature or fail.
2. **No equity acquisition discount mechanism.** The bot already wants to own
   the ai_bigtech names; buying them at market every time leaves the
   limit-order discount on the table.
3. **No long-vol exposure.** Long calls (`spy_options_reversion`) are long
   delta, not long vol. A vol expansion the bot didn't enter long for is pure
   drawdown.
4. **No low-IV options income.** Credit spread idles when the IV proxy is
   below floor; the options sleeve does nothing in quiet regimes.
5. **No downside premium collection.** Bull put spread profits on the put
   side only. A bear call spread layer (Iron Condor) is the obvious symmetric
   complement.

### What's actually an empty roster vs. an out-of-scope zone

The five active strategies are all gated to TRENDING + RANGING (Donchian is
TRENDING-only). **BEAR and VOLATILE currently have zero active strategies** —
when the regime detector flips, the bot sits in cash. That's not a
deliberate "never trade BEAR" posture; it's an empty roster. The right
framing for adding options strategies is *which regimes is each candidate
active in*, not just *which gap does it fill*.

Strategy Health (PLAN 11.10) is the operator-facing mechanism that makes
this roster thinking workable: it surfaces underperforming strategies as
WATCH / DEGRADED / BROKEN with weekly/monthly reports and Telegram alerts,
so a strategy that stops earning its sleeve can be shelved (set
`enabled=False`) without leaving the codebase. The five-strategy ceiling
isn't on *coded* strategies — it's on *concurrently active* ones in a given
regime. A BEAR-active strategy and a TRENDING-active strategy are not
competing for the same paper-watch attention because they never run at the
same time.

### Truly out of scope (not just empty roster)
- **0DTE / weekly intraday.** Architectural mismatch — cycle-based engine,
  daily-bar indicators, no intraday quote streaming for non-options paths.
- **Naked premium** (uncovered strangles, naked puts on non-ownership
  names, short calls without cover). Project stance is defined-risk only on
  the options side.

---

## 2. Strategy universe — what could fit

A pass over the practitioner options-strategy menu, organized by what gap each
would fill. Items already considered in earlier versions of this doc are
included for completeness with deeper takes.

### Theta / premium-selling

| Strategy | Fills which gap | Architecture fit |
|---|---|---|
| **Covered calls** on existing equity | Income on long book (#1) | Single-leg path exists; needs assignment handling |
| **Cash-secured puts** on ai_bigtech | Acquisition discount (#2) + income | Single-leg path exists; needs cash-reserve + assignment-to-equity |
| **Wheel** (CSP → assigned → CC → assigned away → CSP …) | (#1) + (#2) combined | Composition of CSP + CC; needs state machine across the cycle |
| **Iron Condor** (add bear call leg to credit spread) | Downside premium (#5) | Engine MLEG path is leg-count-agnostic; trivial extension |
| **Jade Lizard** (short put + bear call spread; no upside risk) | (#5) + higher credit than IC | Sits on top of IC infrastructure; net premium ≥ call-spread width = no upside risk |
| **Iron Butterfly** | Narrower IC, higher max profit, lower P(win) | Same as IC mechanically; just strike selection |
| **Short strangle** (uncovered) | High premium | Skipped — undefined risk violates project stance |

### Long premium / directional

| Strategy | Fills which gap | Architecture fit |
|---|---|---|
| **Long calls** (current `spy_options_reversion`) | — | Already in place |
| **Bull call debit spread** (cap the long call) | Cheaper directional, defined max loss | Trivial refactor of `spy_options_reversion` when IV rich |
| **Long straddle / strangle** around earnings | Long vol (#3) | Needs earnings calendar (RSI filter has one); but systematic earnings-vol edge is weak |

### Calendar / term structure

| Strategy | Fills which gap | Architecture fit |
|---|---|---|
| **Long calendar spread** (sell front, buy back, same strike) | Low-IV income (#4) | Bigger lift — unequal expiry, front-leg roll/expiry handler, new exit rules |
| **Diagonal spread** (calendar with different strikes) | (#3) + (#4) | Same complexity as calendar; little distinct benefit until calendar exists |
| **Poor Man's Covered Call (PMCC)** (long LEAPS + short OTM call) | Capital-efficient CC | Same unequal-expiry complexity as calendar; LEAPS roll mechanics |

### Volatility / event

| Strategy | Fills which gap | Architecture fit |
|---|---|---|
| **Pre-earnings long straddle** | (#3) on the calendar | Earnings data exists; edge is questionable systematically — IV crush eats most of the move |
| **Post-earnings IV crush short premium** | Theta after vol collapse | Defined-risk version is a same-day IC — operationally tight for a cycle-based engine |
| **VIX-spike short premium** | (#5) + take advantage of fear | Already captured by the credit spread's `min_iv_proxy` gate |

---

## 3. The capital-feasibility constraint (read before §4)

Every short-premium single-leg strategy (covered calls, cash-secured puts,
wheel) requires **100-share notional capacity per contract**. Defined-risk
multi-leg spreads do not — they cost `width × 100 - credit` regardless of
underlying price. At the current paper capitalization and sleeve sizing this
is the binding constraint on what's actually addable next, more than code
complexity.

### Current sleeve budgets vs. 100-share thresholds

| Sleeve | Per-position budget | Max underlying price for 100 shares |
|---|---|---|
| SMA Crossover | ~$7,200 | ≤ $72 |
| Donchian Breakout | ~$8,000 | ≤ $80 |
| Credit Spread (sleeve total) | $8,000 | n/a — defined-risk, $800/spread typical |
| Options single-leg | $4,000 | CSP cash-secure strike ≤ $40 |

### ai_bigtech reality
Approximate share prices in the bot's primary equity universe: AAPL ~$200,
MSFT ~$400, NVDA $400+, META $500+, GOOGL ~$170, AMD ~$140, AMZN ~$180. **No
ai_bigtech name supports a single CC contract under current per-position
budgets**, and CSP on these strikes would consume multiple sleeves' worth of
cash secured per contract. Donchian/SMA positions today are 15–40 shares per
name, not 100.

### What this implies
- **Covered calls and the wheel are not addable at current capital.** They're
  architecturally clean but structurally infeasible until either (a) the
  account scales meaningfully (live capital, growth), (b) sleeves are
  reallocated to concentrate per-position budgets above the 100-share
  threshold for a curated subset, or (c) a separate cheap-underlying
  watchlist is introduced (changes the project's universe, not just its
  strategy mix).
- **Defined-risk multi-leg additions are the capital-efficient path.** Iron
  Condor, debit-spread refinements, calendar (if its arch lift is taken on)
  all scale at the spread level, not the share level, and fit existing
  sleeves.
- **The cheap-underlying alternative is real but distinct.** Names like SLV,
  GLD, XLF, KRE, GDX, EFA, HYG, and small-cap ETFs trade in the $20–$60 band
  and would support 100-share notional within a sleeve like the current
  options budget. A "small ETF wheel" sleeve is feasible *today* but is a
  separate strategy with a separate watchlist — not the bot doing CC on its
  existing equity book.

---

## 4. Recommended additions

Ordered by conviction × architectural fit × marginal portfolio value
× capital feasibility at current sizing. Capital implications matter: the
options sleeves are 5% single-leg + 10% credit spread = 15% of equity.
Adding new options strategies either reallocates from these buckets or cuts
equity sleeves further. There's no free room.

### Tier 1 — High conviction, capital-feasible today

#### A. IV Rank as a reusable utility (consumed by every options strategy)
IV Rank — "where does the current IV proxy sit in its own 52-week range" —
is not a credit-spread refinement; it's a primitive that every options
strategy in the portfolio cares about, just with the direction of usage
flipping based on whether the strategy is short or long premium.

**Scope of applicability:**

| Consumer | Direction | How IVR gets used |
|---|---|---|
| Credit spread (current) | Short premium | Gate — enter only when IVR ≥ floor (premium is rich) |
| `spy_options_reversion` (current) | Long premium | Gate — skip or switch to debit spread (Tier 2 #C) when IVR is high |
| Bear call spread (G, future) | Short premium | Mirror credit spread |
| SPY long puts (H, future) | Long premium | Mirror long calls |
| Bear put debit spread (I, future) | Long premium, capped | Same as H |
| Calendar spread (D, future) | Vega-positive | Term-structure variant — front IVR vs. back IVR |
| Covered calls / CSP / Wheel (capital-gated) | Short premium | Time entries when IVR is rich |

**Architecture — utility first, applications after.**

1. **Ship the utility independently.** `utils/iv_rank.py` takes an IV proxy
   series (VIX, RVX) and a current value, returns a 0–1 rank against
   52-week range. Pure math on data the bot already fetches via
   `utils/iv_proxy.py`. Zero strategy dependencies. Daily-cached, same
   pattern as the existing sector resolver. **No paper-watch wait** — this
   can land anytime.

2. **First application — credit spread filter.** New filter in
   `strategies/filters/credit_spread.py` consuming the utility. Three
   wiring options:
   - Replace the absolute `min_iv_proxy = 14` floor with an IVR floor.
   - AND with the absolute floor (both must pass) — more conservative.
   - Per-instrument tuned thresholds (SPY vs. QQQ may have different
     distributions).

   *This choice* benefits from 11.30 paper-watch data — "did the absolute
   floor over-trigger in cheap regimes? did it under-trigger?" — but a
   v1 wiring (AND with the absolute floor) can ship without waiting and
   be re-tuned later.

3. **Subsequent applications** plug in as each consuming strategy lands.
   Each strategy owns its own filter; the utility is shared.

- **Why this matters:** the most consistent finding in TastyTrade-style
  research is that premium pricing matters more than direction for
  short-premium strategies, and that IV rank captures it better than
  absolute level. Same finding inverted for long-premium: avoid paying
  for vol you don't need. The utility makes both sides addressable with
  one piece of code.
- **Capital implication:** none. Strictly a gate/refinement.
- **Effort:** low for the utility, low for each wiring. The hardest part
  is data-quality — IV proxy series can have gaps around holidays and the
  52-week-range computation needs to handle them cleanly.

**Note on `spy_options_reversion`:** this strategy is *not* gated by
11.30 (which is credit-spread-specific). Three possible IVR applications
to it, in increasing order of behavior change:

1. **Observation-only logging** — compute and record IVR at every signal
   entry/exit. Zero gate, zero behavior change. Ships safely with the
   utility and builds an evidence base for future threshold decisions.
   This is the recommended first step.
2. **Simple "skip when IVR too high" gate** — capital-neutral but the
   threshold is a guess without evidence. Defer until application #1
   has produced data.
3. **Structure switch to bull call debit spread when IVR is rich** —
   this is Tier 2 #C. Bigger code lift; needs its own paper-watch on
   long-call behavior across IV regimes (not 11.30).

Recommended sequencing: ship the utility + observation-only logging in
parallel with the 11.30 wait. Both are free during the observation
period and build evidence for later filter choices on both sides
(credit spread short-premium gate + `spy_options_reversion` long-premium
gate or structure switch).

---

### Tier 2 — Capital-feasible, awaiting paper-watch data

#### B. Iron Condor — add bear call spread to existing credit spread
Layer a bear call spread on top of the bull put at the same expiration. Same
DTE, same exit rules, same execution path. PLAN 11.31 shipped 2026-05-27, so
the engine is already strategy-name-agnostic and leg-count-agnostic.

- **Why:** symmetric premium collection on a strategy designed for symmetric
  outcomes. Improves credit-to-width without raising max loss (both spreads
  defined-risk). Genuinely direction-neutral, where the current bull put is
  bullish-neutral.
- **Architecture decision:** new strategy class vs. config mode on
  `CreditSpread` (`structure: "bull_put" | "iron_condor"`). Strong preference
  for config mode — reuses watchlist, sizing, exit rules; one paper-watch
  covers both structures via the structure column.
- **Capital implication:** no new sleeve. Same max loss per position; just
  more credit collected.
- **Gating:** 11.30 paper-watch on the bull put. If puts ride to 50% profit
  without testing strikes, the call side adds free premium. If puts are
  getting whipsawed, IC doubles the whipsaw — IVR filter (Tier 1 #A) probably
  needs to land first.

#### C. Convert SPY Options Reversion to bull call debit spread when IV is rich
Today `spy_options_reversion` buys naked SPY calls. In rich-IV regimes, the
premium paid for that long call eats a meaningful chunk of the directional
edge. A bull call debit spread caps upside but cuts cost and IV exposure.

- **Why:** the strategy's edge is directional (RSI recovery), not vol. Paying
  vol-rich premium for delta exposure is a leak. A debit spread keeps the
  delta exposure while shedding the vega.
- **Architecture:** uses the existing MLEG path. Strategy gains a structure
  toggle (`structure: "long_call" | "bull_call_spread"`) gated on IV rank.
- **Capital implication:** lower per-trade premium → potentially more
  concurrent trades within the same sleeve, or smaller sleeve usage.
- **Gating:** 11.30-equivalent paper-watch on existing long-call performance
  vs. modeled spread performance. If long-call is fine, this is overengineering.

---

### Tier 3 — Capital-feasible, bigger architectural lifts

#### D. Calendar spread for low-IV regimes (fills gap #4)
Sell front-month, buy back-month, same strike. Profits when near-term decays
faster than long-term. Thrives in the exact regime where credit spread
idles.

- **Why this is Tier 3:** the existing MLEG path handles same-expiry atomic
  spreads. Calendars have:
  - Unequal-expiry legs — the back leg lives on after the front expires.
  - A front-leg roll/expiry handler distinct from the credit spread close path.
  - Different P&L and Greek behavior (vega-positive vs. credit spread's
    vega-negative).
  - New exit rules: front-leg expiry, back-leg-only management post-roll,
    spread-value drawdown threshold.
- **Realistic effort:** medium-to-high, not "medium" as earlier versions of
  this doc suggested. Probably 2x the credit-spread integration work.
- **Worth it only if:** the operator wants the options sleeve to be active in
  *every* regime, including quiet ones. If "sit on cash when IV is low" is
  acceptable, calendar is optional complexity.

#### E. Jade Lizard
Short put + bear call spread. Net premium ≥ call-spread width = no upside
risk by construction. Downside risk is the short put (cash-secured if
configured that way).

- **Why interesting:** higher credit than IC for the same defined-risk
  envelope, with a structurally favorable risk graph (no loss on a rip up).
- **Why Tier 3:** redundant with IC initially. Add only if IC paper data
  shows the bot is consistently losing on call-side strikes (rallies tagging
  the short call) but rarely on put-side — Jade Lizard captures the same
  upside-rip profile without the call-spread loss. If IC is symmetric in
  practice, Jade Lizard adds complexity without distinct edge.

#### F. Revive BollingerSqueeze paired with calendar
Squeeze in low IV is a classic calendar entry. Unlocks the parked
BollingerSqueeze strategy without abandoning its equity version.

- **Depends on:** calendar infrastructure (D). Without D this collapses to
  "unpark BollingerSqueeze as equity," which is a separate decision tracked
  in strategies.md.

---

### Regime-rotational — fill the empty BEAR/VOLATILE roster

These don't compete with the existing five strategies for paper-watch
attention or capital because they activate only in regimes where the
current roster is dormant. Capital-feasible (defined-risk multi-leg or
single-leg within existing sleeves) and rely on Strategy Health for
informed shelve/revive decisions over time.

#### G. Bear call credit spread (mirror of bull put — fills BEAR roster)
Sell OTM call spread above market in BEAR. Profits when underlying falls
or stays put. The structural mirror of the existing bull put credit spread.

- **Why high conviction:** identical infrastructure to the bull put credit
  spread already in production — MLEG path, exit triggers (50% profit, 2×
  credit stop, 21 DTE, short-strike breach), `find_best_put_spread`'s call
  sibling. The only meaningfully new code is direction-aware strike
  selection and an opposite regime gate.
- **Architecture decision:** new strategy class `BearCallSpread` reusing the
  `CreditSpread` execution machinery, or a `direction` config mode on
  `CreditSpread` itself. New class is cleaner for paper-watch and health
  (separate counters, separate verdicts, regime-gated independently).
- **Capital implication:** shares the credit-spread sleeve; competes for
  capital with the bull put only in regime transitions (rare). Effectively
  free capacity in BEAR.
- **Gating:** 11.30 paper-watch on the bull put first. The credit spread's
  edge thesis (defined-risk theta with IV gating) needs to be validated on
  the bullish side before mirroring it.
- **Risk to name:** BEAR regimes typically come with high VIX, which means
  wider spreads and more credit but also more whipsaw. The `min_iv_proxy`
  gate already screens for premium richness; a *maximum* IV ceiling on the
  bear side might be worth considering (vol-of-vol blow-ups are exactly
  when bear call spreads hit max loss fast).

#### H. SPY long-put RSI strategy (mirror of `spy_options_reversion`)
Buy SPY puts on RSI overbought during BEAR — mirror of the existing call
strategy that buys on oversold during TRENDING/RANGING. Edge thesis: bear
rallies fail.

- **Why this is medium conviction:** the call-side strategy has a clear
  mean-reversion thesis (oversold during bull/range regimes tends to bounce).
  The mirror thesis (overbought during BEAR tends to fail) is *plausible*
  but less proven systematically — bear-market rallies can run further than
  expected before failing. Wants backtest evidence specifically on the BEAR
  regime tag before paper.
- **Architecture:** uses existing single-leg path. Needs `find_best_put`
  helper (sibling to `find_best_call`). Otherwise no new infrastructure.
- **Capital implication:** could share the existing $4k single-leg options
  sleeve (puts and calls won't both fire — they're regime-disjoint).
- **Gating:** backtest on at least one historical BEAR window (2022, 2020
  March, 2018 Q4) before committing to paper.

#### I. Bear put debit spread for directional BEAR exposure
Defined-risk version of buying naked puts: buy ATM/ITM put, sell further
OTM put. Active in confirmed BEAR with downside continuation thesis.

- **Why:** capital-efficient alternative to candidate H. The debit spread
  caps both cost and max profit but removes the IV-decay risk of holding
  long puts.
- **Architecture:** MLEG path; new strategy class sharing execution with
  H (decision in §6 Q5 — new class, not config mode).
- **Capital implication:** lower premium per trade than long puts; same
  sleeve.
- **Gating:** decide after H — if naked puts work, the debit-spread
  variant is a vega-shedding refinement on the same signal.

#### Paired posture — idle-capital parking in prolonged BEAR
> **Tracked as PLAN 11.44.** Independent of credit-spread paper-watch
> (11.30); ships standalone.

Not an options strategy, but a structural pairing with the bear roster
worth capturing here. In prolonged BEAR / VOLATILE, the equity sleeves
(SMA, RSI, Donchian) are dormant and their capital sits in cash. Cash
loses purchasing power to inflation. Sweep idle equity-sleeve capital into
**SGOV** (or equivalent short-duration treasury ETF — BIL, SHV) for the
duration of the regime; reverse on regime exit.

- **Why:** ~5% annualized risk-free return on what would otherwise be
  inflation-drag cash. The bear options sleeve (G, H, I) operates on a
  small defined-risk budget; the bulk of the dormant capital should be
  doing *something*, even if that something is just keeping pace with
  inflation.
- **Activation is for prolonged BEAR only, not corrections or drawdowns.**
  This is a multi-month posture shift, not a tactical regime-flip response.
  Required confirmation before SGOV ever activates:
  - Regime = BEAR for **≥ 20 consecutive trading days** (~1 calendar month).
  - AND SPY closing price **< 200-day SMA for ≥ 20 consecutive trading
    days** (independent confirmation that doesn't share the regime
    detector's logic).
  - AND drawdown from 52-week high **≥ 20%** (textbook bear market
    definition).
  - All three together — a normal correction (5–10% drawdown, 200 SMA
    intact, regime flickering) should never trip this.
- **Deactivation is similarly conservative.** Once parked, don't unwind on
  the first BEAR exit. Wait for **regime ∉ {BEAR, VOLATILE} for ≥ 10
  consecutive trading days** AND SPY > 200 SMA for the same window
  before unwinding. The asymmetry is intentional — re-entering equity
  too early in a false-bottom rally is more costly than holding SGOV a
  bit too long.
- **Architecture sketch:**
  - New pseudo-strategy `DefensiveCashSweep` or extension of the sleeve
    allocator with a "park target" per sleeve.
  - Persistent state: a `defensive_posture_state` JSON tracking
    "prolonged BEAR confirmed since YYYY-MM-DD" and the confirmation
    counters. Must survive bot recycles (same pattern as `health_state.json`).
  - Execution: single-leg long ETF, market or limit at open of activation
    day. No options machinery.
  - Sleeve accounting: SGOV holdings stay attributed to the dormant
    equity sleeve for P&L purposes (not a new sleeve).
- **Tax implication on live (not paper).** With activation/deactivation
  thresholds this conservative, flips should happen at most once every
  1–2 years (real prolonged bear markets are rare). Tax-lot tracking is
  still needed for the live-readiness checklist, but the volume is low
  enough that it's a record-keeping concern, not a profitability drag.
- **Effort:** low–medium. ETF execution is trivial; the work is the
  confirmation state machine, persistence, and sleeve-allocator
  integration.

---

### Capital-gated — strategically attractive, not addable at current sizing

These two are the most-discussed retail options strategies and would fill
real gaps (#1 and #2 in §1), but **§3 shows they don't fit current sleeve
sizing**. Captured here so the design is ready when capital permits — do
not start work on them under current capitalization.

#### J. Covered calls on existing equity (fills gap #1)
Sell OTM calls (~0.20–0.30Δ, 30–45 DTE) against shares held by SMA Crossover
and Donchian Breakout. Reuses the single-leg `OptionsExecutionWorker` path.

- **Why it would be valuable:** writes income on capital that's currently
  producing nothing while the trend matures or fails. Closest thing to free
  premium the portfolio could access — *if* the share count were there.
- **What's blocking it today:** the ai_bigtech universe trades at $140–$700
  per share. SMA's $7.2k per-position budget and Donchian's $8k cap support
  15–40 shares per name, not 100. No name in the active universe currently
  supports a single contract.
- **What unblocks it:** any of (a) account growth, (b) reallocation that
  concentrates per-position budget on a curated subset of cheaper names,
  (c) a separate cheap-underlying CC sleeve (sub-$80 names, distinct from
  the trend equity book). Option (c) is a different strategy character; the
  bot stops being purely large-cap.
- **Design preserved for the future:** overlay vs. standalone-strategy
  decision; assignment handoff to the equity strategy's exit logic; trend-cap
  vs. SMA-only-restriction tradeoff. None of this is urgent.

#### K. Cash-secured puts / Wheel (fills gap #2 + idle-cash income)
Sell ~0.20–0.30Δ puts on names the bot would happily own at the strike. On
assignment, the strike-paid shares enter the equity book. Wheel = CSP →
assigned → CC → called away → CSP …

- **Why it would be valuable:** Alpaca Level 3 is in place, single-leg
  execution exists, equity tracking exists, and the wheel is the single
  most-discussed systematic retail options strategy — for good reasons.
- **What's blocking it today:** CSP requires cash secured = strike × 100. At
  the $4k single-leg sleeve, that caps the underlying at ~$40 — no
  ai_bigtech name qualifies. Even with the credit-spread sleeve's $8k, the
  cap is ~$80. The capital-feasibility blocker is sharper on CSP than on CC.
- **The cheap-underlying path is real but distinct.** Sub-$60 ETFs (SLV,
  GLD, XLF, KRE, GDX, HYG, etc.) would fit a CSP sleeve at current sizing.
  That's a separate strategy from "wheel the ai_bigtech book" — it's a
  small-ETF-income strategy with its own watchlist and edge thesis. Worth
  considering, but it doesn't piggyback on existing equity infrastructure.
- **Design preserved for the future:** sleeve allocator change for cash
  reservation; assignment-to-equity transfer; CSP→CC state machine; symbol
  conflict with mean-reversion entries on the same name.

---

## 5. Declined / out of scope (with reasons)

| Strategy | Reason |
|---|---|
| Naked short premium (uncovered strangles, naked puts on non-ownership names) | Undefined risk; against project stance |
| 0DTE / weekly scalping | Cycle-based engine, daily-bar indicators — architectural mismatch |
| Pre-earnings long straddles | IV crush eats the move; systematic edge thin and inconsistent in published research |
| PMCC (long LEAPS + short OTM call) | Same unequal-expiry complexity as calendar; doesn't add a distinct gap beyond CC |
| Iron Butterfly | Mechanically same as IC at different strikes; revisit only after IC is settled |
| Ratio / broken-wing spreads | Asymmetric risk; needs much more paper history before the bot should consider them |
| Box spreads | Interest-rate arbitrage, not strategy edge |

---

## 6. Operator decisions — recorded and open

### Decisions recorded

1. **No hard cap on coded strategies; ~5–6 enabled is the realistic
   ceiling given capital.** Decision (2026-05-28): don't enforce a
   numerical cap. Capital constraints make 5–6 simultaneously enabled
   strategies the practical ceiling; beyond that, per-position sizing
   becomes too thin to be meaningful. Strategy Health (PLAN 11.10) is the
   shelve/revive mechanism — WATCH / DEGRADED / BROKEN verdicts give the
   operator informed disable decisions over time. Regime-disjoint
   strategies (bull put credit spread in TRENDING/RANGING + bear call
   credit spread in BEAR) effectively share a single roster slot since
   they never run simultaneously.

   *Operator note:* the 5–6 ceiling is on **enabled** strategies (those
   with `enabled=True` that the engine will consider per cycle), not on
   strategies whose regime gate happens to be open right now. At any
   given moment the bot is in one regime, so the count of strategies
   actually emitting signals is typically 4–5 (current TRENDING/RANGING
   roster) or 1–2 (BEAR roster, once filled). Keep an eye on the total
   enabled list rather than the per-regime activity — that's what
   competes for capital, paper-watch attention, and health-monitor
   bandwidth even when a given strategy is dormant for the current
   regime.

2. **Fill the BEAR/VOLATILE roster with 1–2 strategies, paired with
   SGOV cash parking.** Decision (2026-05-28): implement at least one,
   ideally two, bear-active strategies from §4 G/H/I. These don't need to
   be useful immediately — they exist to be ready for prolonged
   BEAR/VOLATILE windows where the equity book is dormant. **Pair with
   the SGOV-parking posture** described at the end of §4: in prolonged
   BEAR, dormant equity-sleeve capital sweeps into short-duration
   treasury ETFs so the bulk of capital at least keeps pace with
   inflation while the small bear-options sleeve does the active work.

   *Cold-start risk to manage:* rarely-active strategies have a real
   shakedown problem — bear strategies will get very few chances to
   prove themselves under current market conditions, and the first time
   the bot trades a real BEAR window the bear call spread (G) or its
   siblings (H, I) will be doing so with zero accumulated paper data
   and untuned Strategy Health thresholds. PLAN-11.30-style calibration
   needs a regime that lasts long enough to accumulate ≥20 cycles; if
   BEAR windows are short, the strategy gets repeatedly cold-started
   without ever earning a settled verdict. Mitigations to apply *before*
   the first live BEAR window:
   - **Aggressive backtest** on historical BEAR windows (2008, 2018 Q4,
     2020 March, 2022) using `backtest/runner.py`. The bear roster needs
     more backtest evidence than the bull roster did at the same stage
     because paper won't provide it for years.
   - **Smaller first-window sizing** — when the bear roster first
     activates live, treat it as a stress test. Half-size sleeve, tighter
     concurrent caps, more conservative delta target. Loosen only after
     real BEAR cycles accumulate.
   - **Synthetic-regime override for paper-watch (optional)** —
     temporarily force the regime detector to BEAR against historical
     bars to let the strategy emit signals and accumulate Health priors
     before live BEAR arrives. Risky — synthetic conditions are not the
     same as live ones — but better than starting cold.

   The honest accounting: SGOV parking will earn its keep on the first
   day BEAR arrives; the bear options strategies are on a much slower
   path to validated edge and should be sized accordingly.

3. **New class per regime-active strategy (no config-mode multiplexing).**
   Decision (2026-05-28): Iron Condor (B), Bear call spread (G), Bear put
   strategies (H/I) all ship as new strategy classes sharing execution
   machinery via the post-11.31 MLEG path. Config modes on existing
   strategies were considered and rejected — they blur Strategy Health
   attribution and create paper-watch noise. The engine already supports
   independent classes for free.

### Open questions

4. **The cheap-underlying watchlist question.** Section §3 makes clear
   that CC and CSP/Wheel on ai_bigtech are blocked by share-count
   economics. The bypass is a separate sub-$80 universe (small ETFs,
   cheaper single names). Real fork: either (a) accept that single-leg
   short premium waits for live-account scale, or (b) introduce a
   cheap-underlying sleeve as a distinct strategy with its own watchlist
   and edge thesis. Path (b) is interesting but is not "covered calls on
   the existing book" — it's a new business line.

5. **Capital reallocation appetite.** Is the bot willing to trim SMA from
   40% → 35% (or similar) to concentrate per-position budgets on a few
   names that *would* support 100-share contracts? Or is per-position
   diversification (15–40 share slices across many names) the right
   posture for the equity strategies and options stay capped at 15%?

---

## 7. When to revisit this doc

After the credit spread sleeve completes 11.30 paper-watch (≥ 20–30 cycles)
and any threshold tuning lands. Re-rank against observed credit-spread
behavior plus the regime-roster decision (§6 Q2):

Bear-roster decision is recorded (§6 Q2 — fill with 1–2 strategies +
SGOV parking). Two items have no paper-watch dependency and can ship
anytime regardless of credit-spread outcome:

- **IVR utility (A, step 1)** — pure math helper, zero strategy
  dependencies, immediately useful for every existing and future options
  strategy. Ship whenever there's appetite.
- **SGOV parking (PLAN 11.44)** — independent of options entirely; ships
  on its own schedule.

Re-rank for the credit-spread-dependent items, applying the recorded
bear-roster decision:

- If credit spread is solidly profitable → bear call credit spread (G) is
  the highest-leverage next move; same infrastructure, fills the
  most-empty regime slot, aligns with the recorded bear-roster decision.
  IC (B) follows as the bull-side symmetric extension; IVR wiring (A,
  step 2) tunes the credit-spread filter using 11.30 evidence.
- If credit spread is marginal → debit-spread refinement to
  `spy_options_reversion` (C) becomes the more interesting next move; it's
  independent edge and doesn't depend on short-premium working. Holding
  off on bear mirrors is correct here — they share the credit-spread
  thesis.
- If credit spread is structurally losing → re-open the project posture
  question. The capital-gated wheel (K) is a different bet on the same
  theta thesis with a built-in acquisition hedge, and would justify the
  reallocation needed to make it feasible. But this is a project-direction
  decision, not a strategy add.

Strategy Health verdicts on the credit spread (and any future additions)
are the canonical input to this re-rank — not raw P&L. A strategy in
WATCH or DEGRADED is a signal to hold off on its mirrors and extensions
until the verdict recovers or the operator shelves it.
