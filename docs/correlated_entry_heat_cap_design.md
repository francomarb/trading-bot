# Correlated-Entry Heat Cap — Design (PLAN `11.60`)

**Status:** ✅ **Design review closed — revision 5.** Level pre-registered at **4R**, shipping observation-only first. Two independent
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

**Last updated:** 2026-08-19 (rev 5)

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

### 5.6 RESOLVED on verification — which store, and evaluated when

This is the one question the two reviews answered **differently**. It is
resolved by checking the code, not by counting votes.

**Review 2 proposed** computing heat from `AccountState.open_positions` inside
`RiskManager.evaluate()`, asserting *"No intra-cycle race conditions"* because
`running_account` is updated after each fill.

**That is insufficient, and verifiably so.** Donchian's
`preferred_order_type = OrderType.STOP_LIMIT`, and the engine's own comment is
explicit:

> `# STOP_LIMIT (Donchian today) emit a broker-resting stop-limit entry`
> — [`engine/trader.py:2269`](../engine/trader.py#L2269)

The intra-cycle merge is gated on an actual fill:

```python
if filled is not None:                      # engine/trader.py:1606
    updated_positions = {**running_account.open_positions, filled.symbol: filled}
```

A resting stop-limit produces no fill, so nothing merges — neither
`running_account` nor the next cycle's `AccountState.open_positions` ever sees
it. In the live lifecycle table, **14 of Donchian's 17 entry orders were
`stop_limit`**, 9 of which expired unfilled. So a filled-position cap is blind
to exactly the burst it exists to prevent: N orders resting at the broker,
zero heat counted, all triggerable by one market move.

**Review 1's model is the correct one.** Reserve worst-case risk at
submission; replace the reservation with actual risk on fill; release on
cancel or expiry.

**Risk is knowable before anything is written, because the stop is an input
to sizing rather than a product of order placement.** Inside
`RiskManager.evaluate()` the order is: validation → kill switches → cooldown →
duplicate guard → max-positions → **stop computed** (`_stop_price_for`, from
ATR) → **qty sized** (`_size_position`, which consumes that stop, since
`qty = risk_budget ÷ stop distance`) → live-size adjustment. Only then is the
`RiskDecision` returned.

So at the moment the cap must admit or refuse a candidate there is no stop
*order* and no stop *record* — but the stop *value* exists, and it is the very
number that determined the bet size. Looking for a written stop at that stage
is looking for something that does not exist yet, by design.

The same quantity simply changes custodian as the order becomes real:

| Stage | Authoritative source | Why |
|---|---|---|
| **Candidate**, inside `evaluate()` | Computed in memory from the decision being built | Nothing is written yet; there is nothing to reconcile against |
| Submitted / resting | Substrate `intended_qty`, `intended_limit_price`, `intended_stop_price` | Same numbers, now persisted. The broker has no protective stop yet — it is an OTO child |
| Filled | Broker: `(avg_entry_price − resting stop_price) × qty` | A written stop now exists and is the truth |
| Exited | — | Released |

> **Consistency requirement.** The candidate check must use the **worst-case
> entry price** — `RiskDecision.entry_max_price`, documented as *"PLAN 11.32
> entry price cap: worst-case fill ceiling (BUY)"* — **not** `reference_price`.
> A Donchian `STOP_LIMIT` can fill above reference, so admitting on reference
> and then reserving on limit means the cap approves one number and books a
> larger one. It would breach itself through its own accounting. **The number
> used to admit must equal the number reserved.**

| Event | Heat contribution |
|---|---|
| Entry submitted (resting) | **Reserved:** `intended_qty × (intended_limit_price − intended_stop_price)`. A buy stop-limit cannot fill above its limit, so this is the true worst case |
| Entry **fully** fills | Reservation **replaced** by actual `(entry_fill − initial_stop) × held_qty` |
| Entry **partially** fills | **Both components held at once:** actual risk on the filled shares **+** worst-case reservation on the shares still resting |
| Entry cancels / expires DAY | Release **only the unfilled portion's** reservation. Any filled shares keep their actual risk — they are a position now |
| Position **partially** exits | Reduce heat **pro rata to the exited quantity**, not to zero |
| Position fully exits | Contribution **released** |

**Held quantity has its own authority.** Use `position_lifecycle.current_qty`
(the parent row), **not** the entry order's `filled_qty`. The two agree only
until the first partial exit, fractional residual cleanup, or reduction — after
which `filled_qty` describes what was once bought and `current_qty` describes
what is still at risk. Only the latter belongs in a heat figure.

*Status of this path:* anticipatory, not a fix for an observed bug. All 142
rows in the live trade log are `status='filled'`; no equity entry has partially
filled yet. But `partial` is a supported state the reporting layer already
handles (`read_realized_pnl_events_for_day` counts `status IN ('filled',
'partial')`), the spread path already logs partial closes, and fractional
quantities are routine. A cap that mis-handles the first partial fill would
under- or over-count silently, so the accounting belongs in v1 even though it
is currently unexercised.

**Store.** [`position_lifecycle_orders`](../reporting/logger.py) is
authoritative for reservations; filled trade rows remain the audit trail for
actual initial risk. This is sound **by construction, not by convention**:
`_lifecycle_orders_insert_pending` runs *before* broker submit, and a failure
aborts the submit ("aborting submit + rolling back position lock"). A broker
order therefore cannot exist without a preceding lifecycle row. The rows
already carry `intended_qty`, `intended_limit_price`, `intended_stop_price`,
`status`, and the protective stop is known at submission because it is an OTO
bracket child.

#### 5.6.1 MARKET entries — same state machine, weaker guarantee

3 of 17 Donchian entries were `market` (the fractional path). **RESOLVED —
both reviews concur:** run them through the identical reservation state
machine rather than a special branch. Reserve at submit, transition to actual
risk on fill, release on rejection. Because a market entry fills in the same
cycle, reservation and replacement collapse into one step.

**But the two reservations are not equally strong, and the design must not
pretend otherwise:**

| Entry type | Reservation | Guarantee |
|---|---|---|
| `stop_limit` | `qty × (limit − stop)` | **True bound.** A buy stop-limit cannot fill above its limit |
| `market` | `qty × (reference_price − stop)` | **Estimate.** No price ceiling exists; the fill can be worse than reference |

The market reservation can therefore under-reserve by the slippage. The
exposure is sub-second (it is replaced by actual risk in the same cycle) and
slippage-sized, so this is acceptable — but it is an accepted approximation,
not a guarantee, and should be labelled as one.

#### 5.6.2 Read risk from the broker, not from stored fields

**The broker is authoritative for filled positions.** Heat per open position is

```
(position.avg_entry_price − resting_stop_order.stop_price) × position.qty
```

Every input comes from the `sync_with_broker()` snapshot the engine already
takes each cycle — `account.open_positions` for entry price and quantity,
`open_orders` for the live protective stop. **No database read is involved.**

This is the project's own durable position — *broker state is source of
truth* — applied to a number that had been sourced from a local mirror of it.

**Verified 2026-08-19.** Broker-derived heat reproduces the DB-derived figure
to the cent on all six open Donchian positions, totalling 1.99% of equity
either way. More importantly it is **immune to the failure class that made
§5.6.2 necessary in the first place**: SMCI reads 410.99 from the broker — the
correct figure — where the stored `initial_risk_dollars` had frozen at a 26%
understatement (`11.58`). A derived value cannot go stale because there is
nothing to keep in sync.

Three properties fall out for free:

| | |
|---|---|
| **Partial exits** | Handled automatically. `avg_entry_price` is the cost basis of the *remaining* shares and `qty` is the *remaining* quantity, so the product tracks the position down without special accounting |
| **Repaired stops** | Handled correctly. If a stop was rebuilt at a different level, the broker shows the level actually in force — which is the honest risk, and exactly what `_repair_missing_protective_stops` leaves stale in the stored field |
| **Write races** | Eliminated. Both `11.58` races were disagreements between two writers of a stored number. There is no stored number |

**The stored fields become a cross-check, not a source.** Compute from the
broker, compare against `initial_risk_dollars`, log on disagreement. That
converts a silent corruption into an observable one and gives `11.58` a
standing regression detector rather than a third incident.

**What still requires local state — and why it is not the same problem:**

1. **Attribution.** The broker does not know which strategy owns a position.
   That stays with the engine's ownership map, as it already is for every
   other per-strategy control.
2. **Reservations for resting entries.** A resting `STOP_LIMIT` entry exposes
   its `qty`, `limit_price` and trigger via `OpenOrder` — but its protective
   stop is an **OTO bracket child that does not exist at the broker until the
   parent fills**. So the intended stop for an unfilled entry must still come
   from `position_lifecycle_orders.intended_stop_price`. The broker cannot
   answer a question about an order it has not created yet.

So the authority splits cleanly by lifecycle stage: **substrate before fill,
broker after.**

**What "risk cannot be read" now means.** Not a data-quality cascade — a
filled position with **no resting stop order at the broker**, i.e. one that is
genuinely unprotected. That is a protection incident, and
`_repair_missing_protective_stops` already owns it and runs every cycle.

> **RESOLVED — operator decision, 2026-08-19: block new entries until the stop
> is restored.** During the window between a protective stop going missing and
> `_repair_missing_protective_stops` rebuilding it, the sleeve takes no new
> entries. Not "carry the last known risk", not "estimate" — an unprotected
> position is a reason to stop adding risk, not to keep sizing against a
> guess. The window is short (repair runs every cycle, ≤5 min) and the
> condition is rare, so the cost is small and the alternative is relaxing a
> risk control during a protection incident.
>
> This also fails in the right direction: the refusal is loud and logged,
> where an estimate would be silent.

#### 5.6.3 Restart — the reconciliation already exists

**RESOLVED, and smaller than it looks.** Review 2 called explicit startup
reconciliation against broker open orders "required", implying new machinery.
It is already built: `_reconcile_substrate_via_rest` runs at **both** startup
(P-3) and per cycle (P-2), takes substrate non-terminal rows, checks them
against `snapshot.open_orders`, and advances any that terminated while the bot
was down. It ran **4 times at the 2026-08-19 07:32 restart**, reconciling
exactly the expired DAY entry orders this design would otherwise have carried
as live reservations.

So the requirement is not "build reconciliation" but an **ordering
constraint**:

> Derive heat from substrate rows only **after** `_reconcile_substrate_via_rest`
> has run. Compute it before, and the cap counts reservations for orders that
> died overnight — blocking entries against risk that no longer exists.

Do not build a parallel reconciliation pass for the heat cap.

**Precondition still to verify before shipping:** a protective-stop substrate
row was once recorded stuck `pending` while its broker order was live. The
insert-before-submit ordering makes the *entry* path sound, and the
reconciliation pass covers the restart window, but a silently missing
reservation would still make the cap under-count. State this as verified, not
inherited.

---

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
2. ~~The last sub-decision~~ ✅ **Decided: block new entries while a managed
   position is unprotected** (§5.6.2). All design questions are now closed;
   what remains is implementation against §6.

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
