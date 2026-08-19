# Correlated-Entry Heat Cap — Design (PLAN `11.60`)

**Status:** 🔄 **DRAFT — under active discussion. Nothing here is decided.**
This document exists to be argued with and iterated on, including by other
agents with access to this repo (Codex, Antigravity). Sections marked
**OPEN** are genuine forks awaiting evidence or a decision; sections marked
**CLOSED** were already measured and must not be re-proposed without meeting
the stated re-open bar.

**Last updated:** 2026-08-19

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

---

## 5. OPEN — the design forks

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

> **Proposed:** cap on **actual initial risk at entry, held fixed for the life
> of the position.** Stable, decays only on exit, and composes directly with
> `11.48` sizing.
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

> **Proposed:** count only the sleeve's own positions (per-sleeve
> independence), but denominate in **account equity** (stable, composes with
> existing sizing). Reads as: *"Donchian may carry at most X% of equity in
> open initial-R at once."*

### 5.3 Scope of count — per-sleeve vs portfolio

Distinct from §5.2, and easy to conflate with it.

Donchian and SMA crossover hold names from the same high-beta complex — on
2026-08-19, SMA carried NVDA and GSAT while Donchian held six AI-adjacent
names. Portfolio initial-R heat was **2.84% of equity** (Donchian 1.99% + SMA
0.85%) — already close to the **3.20%** that eight Donchian positions at target
size would consume on their own. The sleeves are correlated **in fact** even
though they are separate **in config**, so a per-sleeve cap alone leaves total
book heat free to stack.

> **Proposed:** per-sleeve cap now; a portfolio-level ceiling later as a
> second and higher backstop. **OPEN:** is the portfolio ceiling worth
> specifying in this document, or does it belong in its own item?

### 5.4 Control form

`PLAN.md` names three candidate forms. The evidence should pick, not intuition.

| Form | Assessment |
|---|---|
| **Aggregate open R** | Turtle-native; the only form that composes with `11.48` sizing. **Currently favoured.** |
| **New entries per window** | **Disfavoured** — a fixed window has a boundary you can straddle. Two clusters landing either side both pass while being one bet, and the failure mode depends on where the window edge happens to fall relative to the market move. |
| **Concurrent position count** | Already exists as `hard_max_positions = 8`. **Retain as last-resort insurance**, not as the primary control. |

> **Proposed shape:** aggregate open initial-R as the live control, with the
> existing count ceiling left in place beneath it as a hard backstop that
> should never normally bind.

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

**OPEN:** should a strategy without a declared cap be *unconstrained*, or
should there be a permissive global default? Unconstrained is simpler and
matches the existing pattern; a default is safer but invents a number.

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

**Step 2 — design choice.** Resolve §5.1–5.5 from the Step 1 evidence.

**Step 3 — pre-register a level** with a stated bar to clear before it is
applied, and run it as an arm rather than a silent config change.

**Step 4 — observability.** Whatever the cap, log every entry it blocks with
the heat at that moment and the candidate that was declined. A control whose
refusals leave no trace cannot be evaluated afterwards — this is the same flaw
that makes today's slot allocation (PLAN `11.61`) impossible to audit.

---

## 7. Questions for reviewers

1. **§5.1** — is there an argument for current-risk-to-stop that survives the
   static stop, other than re-opening the trailing-stop question?
2. **§5.2** — is the `can_stretch` objection decisive, or is there a
   formulation where a sleeve-denominated cap does not drift?
3. **§5.3** — should the portfolio-level ceiling be specified here or split?
4. **§5.4** — is there a fourth control form worth considering that is not
   count, aggregate R, or per-window?
5. **§6** — what is the minimum evidence that would justify a specific level,
   as opposed to merely justifying that a cap should exist?
6. Anything in §3 that you believe was closed on insufficient evidence — but
   argue against the stated re-open bar, not around it.

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
