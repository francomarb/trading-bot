# Correlated-Entry Heat Cap — Design (PLAN `11.60`)

**Status:** ✅ **Design review closed — revision 6.** Level pre-registered at **4R**, shipping observation-only first. Two independent
reviewers (Codex, Gemini/Antigravity), two rounds each. Every structural
question is resolved: §5.1–§5.5 by convergence, §5.6 and its sub-questions by
verification against the code. **The cap level is deliberately unchosen** —
§6 explains why that is an operator decision rather than a finding, and §7
lists the one design sub-decision still open.
The five design forks in §5 are now **RESOLVED**: two reviews (Codex,
Gemini/Antigravity) reached the same answer on each, independently. The
enforcement model in §5.6 is resolved on evidence where the two reviews
*disagreed*. The **cap level remains undecided** — that is §6 and it is
deliberate.

Sections marked **CLOSED** were measured and rejected; do not re-propose
without meeting the stated re-open bar. Sections marked **RESOLVED** carry
the reasoning both reviews converged on. §7 lists what is still open.

**Last updated:** 2026-08-20 (rev 6)

**Scope:** the `donchian_breakout` sleeve first, built as reusable per-strategy
machinery. Slot allocation / candidate ranking is deliberately **out of scope**
— see PLAN `11.61`.

---

## Conventions for this document

**No account balances.** This repository is public. Every quantity here is
expressed as a **percentage of equity**, a **percentage of sleeve budget**, an
**R-multiple**, or a **ratio**. Do not "add the dollar figure for context" —
per-trade risk dollars combined with a stated percentage reconstruct the
account size, which is the exact leak this rule prevents. If you need an
absolute magnitude, give a ratio instead.

**Evidence over intuition.** Every claim about live behaviour in this document
was verified in code or in `data/trades.db` on the stated date. If you add a
claim, say how you checked it. If you cannot check it, mark it as a
hypothesis.

**Cite the reader's own sources.** The code, `PLAN.md`, `docs/`, `data/trades.db`
and `logs/` are all readable locally. Point at `file:line` rather than
paraphrasing mechanism.

---

## 1. The problem

Breakout entries are not independent bets. Donchian System 1 fires when price
makes a new N-day high; that condition is satisfied across many names
simultaneously because they are all responding to the same underlying market
move. So entries arrive in correlated bursts, and one adverse market move
resolves the entire burst at once.

**Live evidence (verified against `data/trades.db`):**

| Window | Entries | What happened |
|---|---|---|
| 2026-06-01 → 06-05 | 6 (SMCI, QCOM, IONQ, ARM, MRVL, ASML) | SPY fell 2.5–2.9%; **five dead within 1–8 days**; ARM and MRVL lasted a single day each |
| 2026-08-04 → 08-07 | 3 (GOOG, AMZN, AVGO) | SPY flat; **all three dead** |

The consequence is not only risk concentration — it is **sample poverty**. The
27 closed live trades at the time of the 11.60 write-up represented roughly
**six independent market-timing bets**. Every Donchian statistic therefore
rests on a far smaller effective sample than the trade count implies. This
matters for the operator's stated goal of accumulating more trades: clustered
trades grow the count much faster than they grow the evidence.

**Why now.** The `11.59` gate change (2026-08-18, BEAR-only exclusion) raises
modelled entries **714 → 997 (+40%)**. Loosening the gate makes clustering
strictly worse. Neither the gate A/B harness nor any backtest in this repo
models concurrency, shared capital, or the allocator budget.

**Amplifier.** `11.48` documented risk-dollar dispersion across entries (28.8×
historically; 3.4× among entries carrying a recorded `risk_budget_dollars`
from 2026-08-04 onward). Correlated entries sized inconsistently turn one bad
market call into a lumpy loss rather than a uniform one.

---

## 2. What "heat" means here

**Heat is the total open risk carried at one time** — the sum, across open
positions, of what would be lost if every position resolved at its stop. It is
the Turtle-system term. Donchian System 1 *is* the Turtle system, so the
concept is native to the strategy rather than imported.

> ⚠️ **Not to be confused with [`docs/sector_heat.md`](sector_heat.md)**, which
> is a generated sector-ETF momentum report (HOT/NEUTRAL/COLD) with no
> relationship to this. Pure name collision.

---

## 3. CLOSED — do not re-propose without meeting the bar

Reviewers with repo access but no project history tend to re-propose these.
Each was measured and rejected.

| Question | Outcome | Re-open bar |
|---|---|---|
| **Rank/filter entries by predicted quality** (overextension veto, RSI level, run-up) | **CLOSED by `11.56`** (2026-08-10, "hypothesis NOT supported"). The separator between winners and losers is SPY's move *during the hold* — not knowable at entry. Median MFE: losers +0.42R vs winners +2.55R. | ≥30 closed trades under current config, ONE feature named before looking, \|r\| ≥ 0.4, not a sweep |
| **Trailing / breakeven-at-+1R stop** | **CLOSED.** Tested: saves CIEN/TSLA/AVGO but clips ALAB on its 6-26 dip to entry. Net ≈ a wash. Static stop retained. | New evidence; note §5.1 depends on this staying closed |
| **Blanket sector caps** | **REJECTED** as too blunt (`11.7`); calibrated targeted caps deferred to `11.8` pending evidence of a real concentration problem | Paper exposure showing a genuine sector concentration problem |
| **Slice-based allocator** (`budget ÷ max_positions`) | **REJECTED** — v1 was tried and caused position-count starvation while capital sat idle. See `docs/capital_allocation_reference.md` §3.4 | Do not re-propose; fix is parameter reconciliation, not structural |
| **Enforce the cap from filled positions** (`AccountState.open_positions` / the trade log) | **CLOSED — verified insufficient.** Proposed in review 2 with the claim "no intra-cycle race conditions". It is blind to the burst it exists to prevent; see §5.6 for the verification | Show that Donchian entries do not rest at the broker — they do (`trader.py:2269`) |

**A note on sector caps specifically.** `PLAN.md` currently justifies
preferring a heat cap over a sector cap with the observation that on the
`ai_bigtech` universe nearly every name is tech or semiconductors, so the two
controls nearly coincide. **That is a snapshot argument and it expires.** The
durable reason is: watchlist composition rotates unpredictably — today AI and
big tech, plausibly healthcare and cyclicals or financials later — whereas the
risk carried on a sleeve is knowable at every instant regardless of what is in
the list. A heat cap is invariant to universe rotation; a sector cap is not.

Today's blocked list illustrates why the sector label under-discriminates
even now: nuclear (OKLO, SMR), quantum (RGTI, QBTS, IONQ), bitcoin-miners-
turned-datacentres (APLD, CORZ, IREN), space (ASTS, PL) — several distinct
GICS sectors, one trade.

---

## 4. Current state (verified 2026-08-19)

**There is no risk-weighted control anywhere in `risk/`, `engine/`, or
`config/` — no heat cap and no correlated-entry limit.** A count-based
ceiling does exist (`hard_max_positions`, below); it bounds how many
positions a sleeve may hold, not how much risk they carry.

What exists today:

| Control | Value | Character |
|---|---|---|
| `hard_max_positions` (donchian) | 8 | Count ceiling. Crude, uncalibrated, not risk-weighted |
| `risk_per_trade_pct` (donchian) | 0.40% **of account equity** | Per-entry sizing target (`11.48`) |
| `max_position_pct_of_sleeve` | 0.4 | Notional cap, not risk |
| `target_pct` | 0.25 of the equity pool (pool = 85% of equity) | Sleeve capital budget; `can_stretch = True` |
| Allocator drawdown gate | `PAPER_STRATEGY_DRAWDOWN_GATE_ENABLED = False` | Observation-only in paper; does not pause the sleeve |

**A snapshot of the book on 2026-08-19** (6 donchian positions open, mechanism
illustration only — this is one day and sets no level):

| Measure | Donchian sleeve |
|---|---|
| Open heat, **initial R** | **1.99% of equity** = 9.37% of sleeve budget |
| Open heat, **current risk-to-stop** | **1.50% of equity** = 7.06% of sleeve budget |
| 8 positions at target size | **3.20% of equity** = 15.06% of sleeve budget |

Two things fall out immediately. First, the existing config already implies a
de-facto ceiling near **3.2% of equity**, enforced crudely by the count — so
any heat cap below that binds before `hard_max_positions` does, which is the
intent. Second, the same book reads 1.99% or 1.50% depending only on which
definition of R you pick. That is §5.1.

**What is and is not already throttled — the precise version.** It would be
wrong to say nothing constrains a burst today. `_pool_used_notional` sums
positions **plus** pending order notional
([`risk/allocator.py`](../risk/allocator.py)), so resting entry orders already
consume sleeve and pool budget. A burst is bounded — **in notional**.

> **The argument for this cap is therefore narrower and stronger than "nothing
> throttles bursts": budget bounds notional, and nothing bounds risk.**

Notional is a poor proxy for risk in this system by construction. Per `11.48`,
dollar risk = capped notional × 2×ATR%, so a volatile name carries **2–4× the
risk of a calm one at identical notional**. A budget denominated in notional
cannot see that, which is precisely the gap a heat cap fills.

### 4.1 How a heat cap interacts with the existing notional limits

The heat cap does not replace the sleeve budget or the per-position notional
cap, and it does not override them: an entry must pass **all** of them, so the
tightest one binds. They bind in different volatility regimes, because
Donchian's stop is 2 × ATR — a calm name has a *tight* stop and therefore needs
a *large* position to carry a full 1R.

Sleeve = 21.25% of equity; per-position cap = 40% of sleeve; 1R = 0.40% of
equity. A position is notional-clipped whenever its stop sits closer than
**4.7%** (i.e. ATR below ~2.4%).

| Name's ATR | Stop distance | Position size | Risk it can carry | First limit to bind |
|---|---|---|---|---|
| 1.0% | 2% | 40% of sleeve (clipped) | **0.43R** | sleeve, at 2.5 positions |
| 1.5% | 3% | 40% of sleeve (clipped) | **0.64R** | sleeve, at 2.5 positions |
| 3.0% | 6% | 31% of sleeve | 1.00R | sleeve, at 3.2 positions |
| 5.0% | 10% | 19% of sleeve | 1.00R | **heat cap, at 4 positions** |
| 7.5% | 15% | 13% of sleeve | 1.00R | **heat cap, at 4 positions** |

Two consequences worth stating plainly:

1. **On calm names the heat cap never participates.** They are clipped so far
   below target size that they cannot fill their own risk budget — a 1% ATR
   name carries 0.43R, so the sleeve is exhausted at 2.5 positions while heat
   is barely at 1R. The heat cap is not generous there; it is simply not the
   operative constraint.
2. **On volatile names the sleeve never participates.** A 7.5% ATR name uses
   13% of the sleeve per position, so eight would fit on capital — but four
   exhaust a 4R cap. This is the regime the cap exists for, and it is where
   Donchian's universe currently sits (open positions on 2026-08-19 carried
   stop distances of 7.4%–15.3%).

**Neither substitutes for the other, and the notional cap is not redundant.**
A heat figure assumes the stop holds. A tightly-stopped position that gaps
through its stop overnight loses several times its booked R — a 2%-stop
position gapping 12% loses ~6R. Notional cannot be gapped through; risk can.
That is why the notional cap exists (`dc65435`) and why it stays.

Whatever cap ships should therefore **log which limit refused an entry**.
Otherwise the handover between the two — which follows the universe's
volatility, not any config change — is invisible.

---

## 5. RESOLVED — the design forks

Each fork below was reviewed independently by Codex and by
Gemini/Antigravity. **Both reached the same answer on §5.1–§5.5**, which is
why they are marked RESOLVED rather than Proposed. §5.6 is where the two
reviews disagreed; it is resolved on verification, not on consensus.

### 5.1 Which R? — *initial risk at entry* vs *current risk-to-stop*

This is the fork that most changes behaviour, and the static stop decides it.

With a **static** stop (which is the retained design — see §3), risk-to-stop
moves in a perverse direction as a trade develops:

| Position | Trade is | Current risk-to-stop, vs its own initial risk |
|---|---|---|
| NOW | winning | **+22%** |
| DASH | winning | **+21%** |
| MSFT | losing | −54% |
| SMCI | losing | −51% |
| ONDS | losing | −64% |

*(2026-08-19 book. Dollar amounts omitted per §Conventions; the ratio is the
point and it is what a cap would act on.)*

A **winner** consumes *more* heat, because the stop has not moved and there is
more open profit to give back before reaching it. A **loser** consumes *less*,
because it is closer to its stop and has less left to lose.

As a risk budget that is backwards: it would free capacity precisely as the
book deteriorates and squeeze it as the book works.

In classic Turtle the same measure is fine — but only because the stop
trails, so real risk genuinely declines as a trade matures. This system does
not have that, by an explicit prior decision.

> **RESOLVED — both reviews concur:** cap on **actual initial risk at entry,
> held fixed for the life of the position.** Stable, decays only on exit, and
> composes directly with `11.48` sizing. Review 2 re-derived the inverted
> incentive independently, in its own terms: a loser drifting toward its stop
> "frees up heat to buy more falling knives into a deteriorating market".
>
> **Dependency to record:** if the trailing-stop question is ever re-opened,
> current-risk-to-stop becomes viable and arguably better. These two decisions
> are linked and should move together.

**Implementation note.** Use **actual** initial risk
`(entry_fill − initial_stop) × filled_qty`, **not** `risk_budget_dollars`.
The latter is *intended* risk (`equity × risk_per_trade_pct` at sizing time)
and diverges materially once notional caps and whole-share flooring bite — the
recorded ANET case carried **≈2.2× its intended budget**. Capping on intent
would understate real heat by more than a factor of two. Both inputs are
persisted on the entry trade row.

**OPEN — which store does the cap read, and at what moment does it evaluate?**
This document has not established that. Measuring heat after the fact and
enforcing a limit before an entry is accepted are not necessarily the same
read; do not assume they are.

### 5.2 Denominator — sleeve budget vs account equity

The same heat reads **9.37% of sleeve** or **1.99% of equity**. A cap number
that looks reasonable in one currency is wildly wrong in the other, so the
denominator must be stated explicitly wherever a level is written down.

**Case for sleeve-denominated:** it is the unit the allocator already thinks
in, and it keeps sleeves independent and self-governing.

**Two arguments against, both structural:**

1. **The increments are already denominated in equity.**
   `risk_budget_dollars = equity × risk_per_trade_pct`
   ([`risk/manager.py`](../risk/manager.py) — see the `11.54` comment on the
   field). A cap in sleeve currency counting increments in equity currency is
   two numbers that must agree with nothing forcing them to — the same shape
   as the `STRATEGY_ALLOWED_REGIMES` defect, where the settings dict and the
   engine's literals silently disagreed.

2. **`can_stretch = True` makes the sleeve budget a moving target.** Donchian
   borrows beyond its 25% when other sleeves are idle. A sleeve-denominated
   cap therefore *loosens precisely when the other strategies are quiet* —
   permitting more correlated market risk because RSI happened to have no
   signals that week. That is correct for **capital** (it is the point of
   stretch) and wrong for **risk**: a 3% SPY drawdown does not care how much
   of the budget is idle. **Capital can stretch; risk appetite should not.**

> **RESOLVED — both reviews concur:** count only the sleeve's own positions
> (per-sleeve independence), but denominate in **account equity** (stable,
> composes with existing sizing). Reads as: *"Donchian may carry at most X% of
> equity in open initial-R at once."*

### 5.3 Scope of count — per-sleeve vs portfolio

Distinct from §5.2, and easy to conflate with it.

Donchian and SMA crossover hold names from the same high-beta complex — on
2026-08-19, SMA carried NVDA and GSAT while Donchian held six AI-adjacent
names. Portfolio initial-R heat was **2.84% of equity** (Donchian 1.99% + SMA
0.85%) — already close to the **3.20%** that eight Donchian positions at target
size would consume on their own. The sleeves are correlated **in fact** even
though they are separate **in config**, so a per-sleeve cap alone leaves total
book heat free to stack.

> **RESOLVED — both reviews concur:** per-sleeve cap now; **no portfolio-level
> cap in v1.** Both reviews independently called it out of scope as a separate
> cross-strategy risk-policy decision. It should get its own PLAN item rather
> than a section here.

### 5.4 Control form

`PLAN.md` names three candidate forms. The evidence should pick, not intuition.

| Form | Assessment |
|---|---|
| **Aggregate open R** | Turtle-native; the only form that composes with `11.48` sizing. **Currently favoured.** |
| **New entries per window** | **Disfavoured** — a fixed window has a boundary you can straddle. Two clusters landing either side both pass while being one bet, and the failure mode depends on where the window edge happens to fall relative to the market move. |
| **Concurrent position count** | Already exists as `hard_max_positions = 8`. **Retain as last-resort insurance**, not as the primary control. |

> **RESOLVED — both reviews concur:** aggregate open initial-R as the live
> control, with the existing count ceiling left in place beneath it as a hard
> backstop that should never normally bind. Review 2 added the reason windows
> fail that this document had not stated: calendar edge effects, where a
> Friday/Monday split lets one economic burst present as two.

### 5.5 Reusability

A heat cap is a good fit for Donchian, whose entries genuinely cluster. It is
a poor fit for SMA crossover, whose signals are per-symbol moving-average
crossovers that are individually timed and unlikely to fire in bursts.

The mechanism should therefore be **opt-in per strategy**, not global. The
seam already exists: `RiskManager` takes
`risk_per_trade_pct_by_strategy: dict[str, float]`
([`risk/manager.py:401`](../risk/manager.py#L401)). A
`max_open_heat_pct_by_strategy` dict follows the identical pattern —
strategies that declare a cap get one, strategies that do not are
unconstrained, and there is no special-casing anywhere.

> **RESOLVED — both reviews concur:** opt-in, and a strategy without an entry
> is **unconstrained by heat**. No permissive global default — a default would
> invent a number for four sleeves nobody has measured. Review 2's framing:
> unconstrained strategies remain bounded by sleeve budget and
> `hard_max_positions`, which §4 confirms is true for *notional* if not for
> risk.

Config shape (from review 2, matching the existing per-strategy dict pattern):

```python
STRATEGY_MAX_OPEN_HEAT_PCT: dict[str, float] = {
    "donchian_breakout": 0.0XX,   # level pre-registered per §6 — NOT yet chosen
}
```

Rejection surfaces as a new `RejectionCode.MAX_STRATEGY_HEAT_REACHED`,
alongside the existing codes.

---

### 5.6 RESOLVED on verification — heat is a ledger, not a query

This is the one question the two reviews answered **differently**, and the
answer landed somewhere neither proposed.

**Review 2 proposed** computing heat each cycle from
`AccountState.open_positions`, asserting *"no intra-cycle race conditions"*.
That is insufficient: Donchian's `preferred_order_type = OrderType.STOP_LIMIT`
and the engine's own comment says these *"emit a broker-resting stop-limit
entry"* ([`trader.py:2269`](../engine/trader.py#L2269)). The intra-cycle merge
is gated on `if filled is not None`, so a resting order merges nothing —
**14 of Donchian's 17 entry orders were `stop_limit`**, 9 expiring unfilled. A
filled-position cap is blind to the burst it exists to prevent.

**Review 1 proposed** reserving worst-case risk at submission from
`position_lifecycle_orders`. Correct, and adopted below.

**But both framed heat as something to *recompute*.** It is not. Risk is
determined once — when the bet is sized — and under initial-R semantics
(§5.1) nothing afterwards changes it. So the cap maintains a **ledger**:
admit, carry, adjust on lifecycle events, release.

#### Why the stop is never looked up after admission

The stop is an **input to sizing, not a product of order placement.** Verified
order inside `RiskManager.evaluate()`: validation → kill switches → cooldown →
duplicate guard → max-positions → **stop computed** (`_stop_price_for`, a pure
ATR expression that always yields a value, rejected if non-positive or not
below entry) → **qty sized** (`_size_position`, which consumes that stop,
since `qty = risk_budget ÷ stop distance`) → live-size adjustment →
`RiskDecision`.

So at the moment the cap admits or refuses, there is no stop *order* and no
stop *record* — but the stop *value* exists and is the number that determined
the bet size. Looking for a written stop there is looking for something that
does not exist yet, by design. And once admitted, the position's contribution
**is** the number it was admitted with. There is nothing to re-read.

> **This is not only a simplification, it is a correctness property.**
> Re-deriving heat from whatever stop is currently in force equals initial-R
> only while the two coincide. They normally do — the post-fill re-anchor
> (`11.53`/`11.54`) moves the stop and `rebase_entry_stop` updates the trade
> row to match, so `_repair_missing_protective_stops` later restores the
> re-anchored level rather than a stale one. But `_reconstruct_missing_entry_context`,
> the fallback for a position with **no trade-log context at all**,
> reconstructs an "original-style" stop from the latest completed bar — a
> level the position never actually carried. A recomputing cap would adopt it
> and silently report current-risk-to-stop, the semantics §5.1 rejected. A
> ledger cannot drift into the wrong metric, because it never asks.

#### The ledger

| Event | Effect on the sleeve's heat |
|---|---|
| **Candidate admitted** (in `evaluate()`, post-sizing) | `+ (worst-case entry − stop_price) × qty`, computed from the decision being built |
| Entry submitted (resting) | Reservation persisted as `intended_qty × (intended_limit_price − intended_stop_price)` |
| Entry **fully** fills | Reservation becomes actual fill-to-stop risk |
| Entry **partially** fills | Actual risk on filled shares **+** reservation still standing on the unfilled remainder |
| Entry cancels / expires DAY | Release **only the unfilled portion**; filled shares keep their risk |
| Position **partially** exits | Reduce **pro rata** to the exited quantity |
| Position fully exits, or is closed externally | Release |

> **Consistency requirement.** Admission must use the **worst-case entry
> price** — `RiskDecision.entry_max_price`, documented as *"PLAN 11.32 entry
> price cap: worst-case fill ceiling (BUY)"* — not `reference_price`. A
> `STOP_LIMIT` can fill above reference, so admitting on reference while
> reserving on limit means the cap approves one number and books a larger one.
> **The number used to admit must equal the number reserved.**

#### Rebuilding the ledger after a restart — the only read

An in-memory ledger is lost on recycle, and nothing in the engine restores
per-position risk today (verified: `_restore_ownership_from_db`,
`_restore_entry_prices_from_db`, `_restore_allocator_pnl_from_db` — no risk
equivalent). This is the one place the cap reads rather than carries, and the
sources already exist:

| Stage at restart | Source |
|---|---|
| Filled positions | `trades.initial_risk_dollars` on the entry row |
| Held quantity | `position_lifecycle.current_qty` ÷ `entry_qty` (pro-rates partial exits) |
| Resting entries | `position_lifecycle_orders.intended_*` — the trade log is NULL here **by design**, since nothing filled |

`initial_risk_dollars` is the right source and is **already maintained as a
ledger value**: computed as `|actual fill − initial_stop| × cumulative filled
qty`, written LATEST-NON-NULL so each successive fill rewrites it, with the
completing fill or the cancellation standing last. That is the `11.58` fix
(PR #105 review), and it is why the field now tracks reality rather than
freezing at the first partial.

**Verified 2026-08-19 — three independent constructions agree at 1.99% of
equity** across the six open Donchian positions: DB-derived (trade-log fill
price × broker stop), broker-derived (`avg_entry_price` × resting stop), and
the ledger (`initial_risk_dollars` pro-rated by `current_qty ÷ entry_qty`).
The ledger needs no stop lookup at all.

Closure is tracked, so the ledger does not silently drift: `position_lifecycle`
carries `closed` (36), `canceled` (29) and **`external_closed` (6)** — an
operator closing a position by hand is observed, not missed.

#### What the ledger model removes

Framing heat as a per-cycle query dragged in a chain of problems that the
ledger simply does not have:

| Problem | Status under the ledger |
|---|---|
| Stale `initial_risk_dollars` (`11.58` SMCI, 26% understatement) | Fixed at source by the LATEST-NON-NULL rule; read once at restart, not every cycle |
| NULL fallback cascades | Reduced to one meaningful case — see below |
| ~~`_repair_missing_protective_stops` not rebasing risk~~ | **Not a defect — investigated 2026-08-20 and withdrawn.** Repair *should not* rebase: it restores from `read_latest_open_stop_price`, and `rebase_entry_stop` keeps that row describing the re-anchored stop. Rebase is a precondition for repair being correct, not a call repair is missing. The `11.58` quantity freeze was fixed at source by the LATEST-NON-NULL rule. Do not re-raise from `11.58`'s prose, which describes the pre-fix state |
| A fractional residual that cannot carry a stop (QCOM 0.1, TSLA 0.39 — real, 2026-05-19) | Irrelevant. It carries the risk it was admitted with, pro-rated down. The cap never asks whether it has a stop |
| "Block new entries while a position is unprotected" | **Withdrawn** — it had nothing to attach to, and as written would have halted the sleeve over a tenth of a share of dust |

**The one NULL that still means something.** `initial_risk_dollars` is NULL
when no stop existed at entry — the logger's own reasoning: *"you cannot
express a loss in units of a risk that was never bounded."* That is a position
whose risk was never **defined**, which is different from one whose stop is
momentarily missing. Both live instances are historical and both causes are
fixed (PWR 2026-05-01, pre-substrate; WYFI 2026-06-18, the `11.58(b)`
zero-stop bug). If one recurs, the position cannot be given a heat number
honestly, and the cap should refuse new entries while it is open rather than
invent one.

#### MARKET entries — same ledger, weaker admission guarantee

3 of 17 Donchian entries were `market` (the fractional path). Same ledger, no
special branch: admitted, reserved, and settled in the same cycle because the
fill is immediate. But the two admissions differ in strength:

| Entry type | Admission figure | Guarantee |
|---|---|---|
| `stop_limit` | `qty × (limit − stop)` | **True bound.** Cannot fill above its limit |
| `market` | `qty × (reference − stop)` | **Estimate.** No price ceiling; the fill can be worse |

The market figure can under-admit by the slippage. Exposure is sub-second and
slippage-sized, so it is accepted — as an approximation, explicitly, not
labelled a bound alongside one that is.

#### Restart reconciliation already exists — mind the ordering

`_reconcile_substrate_via_rest` runs at **both** startup (P-3) and per cycle
(P-2), checking substrate non-terminal rows against `snapshot.open_orders` and
advancing any that terminated while the bot was down. It ran **4 times at the
2026-08-19 07:32 restart**, reconciling exactly the expired DAY entry orders
this design would otherwise rebuild as live reservations.

> Rebuild the ledger **after** that pass has run. Before it, the cap would
> carry reservations for orders that died overnight and refuse entries against
> risk that no longer exists. Do not build a parallel reconciliation.


## 6. Acceptance — measurement before a level

**Do not ship a guessed cap number.** A sweep over cap values against a
negative baseline will find ten that "improve" things; `11.56` is the
cautionary case. The discipline is the one `11.59` used: measure, pre-register
a level with a bar to clear, then run it.

**Step 1 — evidence.** Quantify clustering from the live trade log *and* the
2016–2026 SIP run:

- distribution of entries per rolling 5 days
- distribution of concurrent open positions
- aggregate open R at the moment of each entry
- realised outcome conditional on cohort size

Extend [`scripts/donchian_gate_ab.py`](../scripts/donchian_gate_ab.py), which
already walks every entry, rather than starting a new harness. Any run answering a production
question must apply the same regime gate, edge filters, sizing rules and
warmup semantics as the live engine, and must report **exit reasons** per
variant — not only aggregate returns.

**Step 2 — design choice.** ✅ Done — §5.1–5.6 are resolved.

**Step 3 — pre-register a level.** Test **discrete multiples of the existing
0.40% per-trade risk unit**, not a continuous sweep. The candidates derive
from a parameter that already exists, which keeps them interpretable.

> ### ⚠️ Discretising narrows the search; it does not make the choice empirical
>
> Three candidates instead of thirty is still a selection if historical
> outcome picks the winner. **The evidence describes the consequence of each
> level. It cannot discover the right one.**
>
> The reason is in §1 of this document: the 27 closed live trades represent
> roughly **six independent market-timing bets**. A sample that small, and that
> clustered, cannot distinguish 3R from 5R — whichever looks best will
> mostly be telling you which few market episodes the window happened to
> contain. Choosing on that basis is `11.56` by instalment.
>
> **So the level is an operator risk-policy decision, not a measurement.** The
> honest sequence is: use the evidence to state what each level *would have
> done* (cohort sizes permitted, entries forgone, worst cohort loss bounded),
> have the operator choose an appetite from that, pre-register it, and only
> then evaluate forward on paper. A level chosen because one candidate made
> the backtest look best has been fitted, however few candidates there were.

Trade-offs as characterised by review 2, which is the useful thing a reviewer
can contribute here — describing each posture's cost, not choosing between
them. "Positions" is at *actual* average sizing (0.33% of equity), not at the
0.40% target; see the callout below for why those differ.

| Candidate | % of equity | ≈ positions | Policy posture | What it costs / buys |
|---|---|---|---|---|
| **3R** | 1.20% | 3.6 | Strict defensive | Bounds worst correlated loss at 1.20%. Routinely refuses the 4th and 5th candidate in a sustained rally |
| **4R** | 1.60% | 4.8 | Balanced budget | Clamps a June-2026-scale cluster while still allowing multi-name diversification. **Would have bound on positions #5 and #6 of the 2026-08-19 book** |
| **5R** | 2.00% | 6.0 | Tail damper only | Prevents 7–8 position runaway and blocks outsized allocations when share flooring inflates risk (ANET at 2.2×). **Effectively at the current book, not above it** — 2.00% against 1.99% is 0.01pp of headroom, so one further position breaches it |

> ### ⚠️ The suggested 4R baseline is already breached by the live book
>
> Review 2 recommended 4R (1.60%) as the baseline. **On 2026-08-19 the sleeve
> was carrying 1.99% of equity across 6 open positions** — over that cap
> before it is written. Flagged here so re-review starts from the fact rather
> than rediscovering it.
>
> **Why it is breached, precisely.** The "4R = 4 standard positions" framing
> assumes entries are sized *at* the 0.40% target. They are not: those 6
> positions averaged **0.33%** each, because notional caps and whole-share
> flooring clip most entries below target (the `11.48` finding). So the real
> constraint a given R-multiple imposes is a **position count**, and it is a
> lower count than the framing suggests — 1.60% ÷ 0.33% ≈ **4.8 positions**,
> against a `hard_max_positions` of 8.
>
> **The consequence to pre-register, not discover later:** an R-multiple cap
> silently redefines the sleeve's effective concurrency ceiling. At current
> average sizing, 3R ≈ 3.6 positions, 4R ≈ 4.8, 5R ≈ 6.0. That is a material
> tightening from 8, and 5R is approximately the status quo — it would not
> have bound today at all. Whether that tightening is desirable is exactly the
> question Step 1's evidence must answer; it should not arrive as a surprise
> after the level is chosen.

**PRE-REGISTERED LEVEL: 4R — 1.60% of equity.** Operator decision, recorded
2026-08-19, before any measurement run. It is written here so that a later
"the evidence supported 4R" cannot be reconstructed after the fact: the
evidence had not been gathered when this was chosen. Changing it later is
allowed and should be recorded the same way, with the reason.

**Step 3a — ship in observation-only mode first.** The cap computes and logs;
it refuses nothing until a flag is flipped. This follows the established
pattern of `PAPER_STRATEGY_DRAWDOWN_GATE_ENABLED`, whose comment states the
same rationale — *"Paper development defaults to observation-only at every
sample size so strategies can accumulate tuning evidence"*.

Two reasons it matters here specifically:

1. **Day one would otherwise freeze the sleeve.** Donchian carries 1.99%
   against a 1.60% cap, so enforcement from the first cycle blocks every new
   entry until roughly two positions exit. That may be the intended
   tightening, but it should be a decision taken with data, not the
   deployment's opening move.
2. **It produces the consequence data §6 actually needs** — how often 4R
   binds, which candidates it declines, how long the sleeve sits at the
   ceiling — without any of it costing a trade.

> **What observation-only does *not* do.** It does not discover the right
> level. The sample problem in the callout above is unchanged by watching it
> for longer: at roughly six independent market bets, more observation
> sharpens *what a level costs* without making the choice between 3R, 4R and
> 5R empirical. Tune the number if the observed cost is unacceptable — that is
> a policy revision, and it should be recorded as one rather than presented as
> a finding.

**Step 4 — observability.** Whatever the cap, every refusal must emit a
structured record (review 1's specification, adopted):

| Field | Why |
|---|---|
| filled heat | the part already committed |
| pending reserved heat | the part §5.6 exists to count |
| candidate reservation | what was declined |
| configured cap | so the record is self-describing across config changes |
| symbol | attribution |

A control whose refusals leave no trace cannot be evaluated afterwards — the
same flaw that makes today's slot allocation (PLAN `11.61`) impossible to
audit. Splitting filled from reserved matters for a second reason: it is the
only way to tell a cap that is binding on real exposure from one binding on
reservations that later expire unfilled.

---

## 7. What remains

**The design review is closed.** Two independent reviewers converged on every
structural question across two rounds. §5.1–§5.5 resolved by convergence,
§5.6 and its three sub-questions resolved by verification. Do not re-litigate
without new evidence; argue with a verification rather than around it.

Two things remain, and only one of them is a design question:

1. ~~The level~~ ✅ **Decided: 4R (1.60% of equity)**, pre-registered
   2026-08-19 before any measurement run, and shipping **observation-only**
   first (§6 Step 3a). Revisable, but as a recorded policy change rather than
   a finding.
2. ~~The last sub-decision~~ ✅ **Withdrawn, not decided.** Reframing heat as
   a ledger (§5.6) removed the question: the cap never asks whether an open
   position currently has a stop, so "block while unprotected" had nothing to
   attach to. The one residual case — a position whose risk was never
   *bounded*, i.e. NULL `initial_risk_dollars` — is handled in §5.6. All
   design questions are closed; what remains is implementation against §6.

Then implementation, whose acceptance is §6 Steps 1, 3 and 4.

**Split out rather than resolved here:** the portfolio-level ceiling, now
tracked as **PLAN `11.62`**. Both reviews put it out of v1 for the same
reason — it introduces cross-strategy priority arbitration (who yields when
the *book* is at its limit) that a single-sleeve control does not, and that
arbitration is the actual work. `11.62` depends on this item shipping first,
since it should reuse the primitive established here rather than
re-implement it.

---

## Related

- PLAN `11.60` — this item
- PLAN `11.61` — entry-candidate ranking (separate; composes but does not block)
- PLAN `11.8` — calibrated sector caps (see §3 on why this is not the same control)
- PLAN `11.48` — per-strategy risk targets; the sizing this cap must compose with
- PLAN `11.56` — closed entry-feature tuning; constrains §3
- PLAN `11.59` — the gate change that makes clustering more urgent
- [`docs/donchian_regime_gate_investigation.md`](donchian_regime_gate_investigation.md) — the pre-registration discipline this design should follow
- [`docs/capital_allocation_reference.md`](capital_allocation_reference.md) §3.4 — allocator architecture constraints
