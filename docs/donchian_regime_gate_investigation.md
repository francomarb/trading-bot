# Donchian entry-gate investigation — does the gate stack earn its cost?

> **Status: APPLIED 2026-08-18 — the gate is now a BEAR-only exclusion.**
> `STRATEGY_ALLOWED_REGIMES["donchian_breakout"]` changed from `{TRENDING}`
> to `{TRENDING, RANGING, VOLATILE}` in paper after the pre-registered test
> in §10 passed all three criteria. **Switch date 2026-08-18** — quote it
> with any Donchian statistic; pre- and post-change cohorts are not
> comparable. Reverting is one line. A wiring defect found while applying
> it (the settings dict was read by nothing but the dashboard) is recorded
> in `11.59`.
>
> **Prior status, kept for the record:**
> A pass authorises *proposing* the change via PR for paper observation; it is
> not a live-behaviour change and not self-approving. Arm E (allow
> TRENDING/RANGING/VOLATILE, block BEAR) returns **+41.0% / Sharpe 0.52 /
> maxDD −15.9%** against production's **+27.4% / 0.39 / −15.0%**, and beats
> production in **9 of 11 years**. The accepted cost: 2022 worsens from −9.1R
> to −16.1R, and entry count rises 714 → 997, which interacts with the
> unmodelled `11.48` correlated-entry clustering.
>
> **Original finding, unchanged:**
> The SPY-TRENDING regime gate's stated premise does not survive contact with
> ten years of SIP data on the `ai_bigtech` universe: RANGING entries have
> **identical** expectancy to TRENDING entries (both `mean R = 0.71`). The gate
> removes 41% of trades and slightly *lowers* per-trade quality (`mean R`
> 0.70 → 0.62). But per `11.56` re-open condition (4), **a good backtest result
> is not a reason to ship a change** — this document records evidence, not a
> recommendation. See §7 for what would have to happen before anything moves.

**Opened:** 2026-08-18 · **Harness:** [`scripts/donchian_gate_ab.py`](../scripts/donchian_gate_ab.py)
· **Tests:** [`tests/test_donchian_gate_ab.py`](../tests/test_donchian_gate_ab.py)
· **Reproduce:** `PYTHONPATH=. venv/bin/python -m scripts.donchian_gate_ab --validate`

---

## 1. The question

Donchian entries pass through two gates before the engine will act:

1. **SPY regime gate** — `STRATEGY_ALLOWED_REGIMES["donchian_breakout"] = {"TRENDING"}`
   ([`config/settings.py`](../config/settings.py)). Its stated justification,
   verbatim in the source:

   > *"Donchian whipsaws hard in RANGING regimes (every 20-day high gets
   > faded). Restrict to TRENDING only — academic literature is unanimous on
   > this."*

2. **`DonchianEdgeFilter`** — stock > 200 SMA, 20-day average dollar volume
   ≥ $20M (rule 2, earnings blackout, has no offline equivalent).

Neither has ever been measured on this universe. The premise in that comment
is an inherited belief, not a finding from this repo's data. This
investigation asks whether it holds.

**Why now.** The live Donchian sleeve is −$3,431 across 27 closed trades at a
15% win rate. Diagnosing that (2026-08-18) established the losers never get
going — median MFE +0.42R against winners' +2.55R — and that the separator is
SPY's move *during* the hold, which is not knowable at entry. That closed off
entry-feature filtering (already settled independently by `11.56`) and pointed
at the gates instead.

---

## 2. Method

The harness varies **only** `entry_mask` across four arms. Every other knob is
held identical and that invariant is asserted in
[`tests/test_donchian_gate_ab.py`](../tests/test_donchian_gate_ab.py) rather
than claimed here — per [[feedback_assert_constants_in_comparative_tests]],
drift in any other knob would turn the headline number into noise.

| Arm | `entry_mask` |
|---|---|
| **A** raw signal | `None` — every Donchian 30-day high |
| **B** regime only | SPY regime == TRENDING |
| **C** filter only | `DonchianEdgeFilter` rules 1 + 3 |
| **D** production | B ∧ C |

Held constant: `entry_window=30`, `exit_window=15`, `atr_length=14`,
`initial_cash=$100k`, `risk_per_trade_pct=2%`, `slippage_bps=5`, and
`StaticATRStop(k=2.0)` — the production stop.

**Window:** 2016-11-01 → 2026-08-18, SIP feed, `ai_bigtech` (32/32 symbols).
This extends the prior study, which stopped at 2024-12 and therefore never
covered the regime the live losses occurred in.

### 2.1 Traps inherited from the trail investigation

[`docs/donchian_trail_investigation.md`](donchian_trail_investigation.md) took
five review rounds to get its method right. All of its documented traps are
applied here:

- **SIP feed**, not IEX — consolidated-tape volume is what makes the $20M
  liquidity floor interpretable ([[feedback_backtest_use_sip_feed]]).
- **Filter mask computed on FULL cached history, then reindexed** (PR #49 R2
  P1). Computed on a sliced window, SMA200 is NaN through the warmup and the
  filter *fails open*, silently admitting entries production would block. The
  test asserts the sliced shortcut is genuinely more permissive, so it cannot
  pass vacuously.
- **Production regime defaults** (126 / 0.80 / 5) via the parity-tested
  `classify_spy_regime`, not the 252 / 90 / 20 that diverged in R1.
- **`trade_start` excludes warmup entries** from metrics (PR #49 R1 P1).

> **Do not compare against the ungated `+24.2%` baseline** quoted in the trail
> investigation's §5. That figure counts warmup trades and its own document
> flags it as "kept here to show the gate's effect, not as a recommendation."
> Arm A supersedes it.

### 2.2 Cohort caveat — the arms are not matched trades

Gated entries are **not** a subset of ungated ones. Suppressing an entry frees
the simulator to take a *later* entry the raw arm was still holding through, so
each arm walks a different path. This is the same caveat the trail
investigation records for its stop variants, and it is asserted as an
executable fact in `test_arms_are_not_matched_trade_sets`. **Read every
comparison below as a policy-path comparison, never as a per-trade A/B.**

---

## 3. Results — arm comparison

| arm | trades | win% | **mean R** | mean ret% | Sharpe | maxDD% |
|---|---:|---:|---:|---:|---:|---:|
| **A** raw signal (no gates) | 1209 | 41.9 | **0.70** | +51.7 | **0.58** | −17.5 |
| **B** regime gate only | 821 | 39.6 | 0.67 | +32.5 | 0.41 | −17.4 |
| **C** edge filter only | 1036 | 42.3 | 0.67 | +43.1 | 0.55 | −15.5 |
| **D** both (production) | 714 | 41.7 | **0.62** | +27.4 | 0.39 | −15.0 |

The sharpest reading is the `mean R` column: **the gates do not improve
per-trade quality — they slightly degrade it (0.70 → 0.62) while removing 41%
of the trades.** A filter that earns its keep should raise the average quality
of what survives it. Neither gate does.

Comparing B against C isolates the culprit: the regime gate costs 19 points of
mean return, the edge filter 9.

Exit-reason mix is nearly flat across arms (signal 58–61%, intrabar stop
31–34%, gap 6–7%, EOD 1–2%), so the gates are not changing *how* trades end —
only how many there are.

---

## 4. The premise test (the decisive result)

Bucketing **raw-signal** trades by the SPY regime in force on their entry bar
tests the gate's justification directly:

| regime at entry | trades | win% | mean R | sum R | production |
|---|---:|---:|---:|---:|---|
| TRENDING | 520 | 39.2% | **0.71** | +368.3 | kept |
| **RANGING** | 443 | 42.4% | **0.71** | +313.4 | **BLOCKED** |
| VOLATILE | 86 | 40.7% | **0.72** | +61.8 | **BLOCKED** |
| BEAR | 160 | **50.0%** | 0.63 | +100.9 | **BLOCKED** |

**RANGING entries perform identically to TRENDING entries** — same mean R to
two decimals, and a *higher* win rate. The premise that "every 20-day high gets
faded" in RANGING is not true on this universe over this window. VOLATILE is
marginally the best bucket of the four. The gate blocks 689 trades carrying
+476 R.

The BEAR row deserves scepticism rather than celebration: 160 long breakouts
entered while SPY was in a BEAR regime, returning +0.63R at a 50% win rate, is
counterintuitive. The plausible explanation is universe-specific — AI/big-tech
names that trended through broad-market weakness — which is exactly the kind of
result that would not generalise. It is reported because it is what the data
says, not because it is believed.

---

## 5. Per-year — does the gate protect the years it exists for?

Sum R by entry year, arm A vs arm D:

| year | A raw (n / sum R) | D production (n / sum R) | gate effect |
|---:|---:|---:|---|
| 2016 | 23 / +22.8 | 20 / +21.8 | — |
| 2017 | 113 / +45.2 | 76 / +39.5 | −5.7 |
| **2018** | 96 / **−13.7** | 21 / **−19.5** | **−5.8 (worse)** |
| 2019 | 115 / +103.7 | 77 / +32.4 | −71.3 |
| 2020 | 117 / +92.4 | 78 / +36.9 | −55.5 |
| 2021 | 141 / +36.3 | 84 / +8.3 | −28.0 |
| **2022** | 103 / **−47.8** | 14 / **−9.1** | **+38.7 (saved)** |
| 2023 | 150 / +168.9 | 90 / +62.4 | −106.5 |
| 2024 | 136 / +246.7 | 126 / +110.5 | −136.2 |
| 2025 | 125 / +141.7 | 77 / +95.8 | −45.9 |
| 2026 | 90 / +48.3 | 51 / +60.3 | **+12.0 (saved)** |

**The gate earns its keep in exactly one year: 2022**, where it cut a −47.8R
bear market to −9.1R by blocking almost all entries (103 → 14). That is real
and it is the case *for* the gate — a long-only breakout strategy in a
sustained bear market is precisely what it protects against.

Against that: it made **2018 worse**, and in every trending year it removes
large amounts of profit. Drawdown protection is modest — maxDD −17.5% ungated
vs −15.0% gated, about 2.5 points for 41% of the trades.

So the honest framing is not "the gate is wrong" but **"the gate is a very
expensive form of bear insurance whose stated rationale (RANGING whipsaw) is
false; its actual value is bear-market protection, which is a different claim
and priced differently."**

---

## 6. Model validation against the live trade log

Per [[feedback_trade_log_outranks_the_model]], a model that disagrees with
closed trades is presumed broken. Over the window the bot has actually traded:

| entries on/after 2026-05-01 | n | win% | mean R |
|---|---:|---:|---:|
| model (arm D, production config) | 26 | 23.1% | **−0.51** |
| **live trade log** | 24 | 16.7% | **−0.47** |

The model reproduces the live losing streak closely. That is a validation of
the harness, and it also explains the live results independently of the gate
question:

| 2026 cohort | n | win% | mean R |
|---|---:|---:|---:|
| Jan–Apr (bot not yet live) | 25 | **80%** | **+2.94** |
| May–Aug (bot live) | 26 | 23% | −0.51 |

**The bot went live 2026-05-01, the bar the regime turned.** March–April
produced MRVL +16.98R, MU +8.32R, ARM +7.54R, CRWD +7.15R, GOOGL +3.86R and the
bot caught none of them. The live sleeve's −$3,431 is consistent with normal
variance for this strategy entering at an unlucky moment — **not** evidence
that the strategy is broken, and **not**, on its own, evidence about the gate.

---

## 7. What this does NOT license

- **No production change is authorised by this document.** `11.56` re-open
  condition (4) is explicit: a good backtest result is not a reason to ship;
  a bad one is a reason not to. This is a good backtest result. It is therefore
  a reason to open a properly-constructed question, nothing more.
- **These are not portfolio returns.** Each symbol runs independently on its
  own $100k. There is no shared capital, no allocator sleeve budget, no sector
  cap, and no correlated-entry limit. Do not read +27.4% as "the bot would have
  made 27.4%". Removing the regime gate in production would raise concurrent
  position count sharply, which interacts with the allocator and with the
  correlated-entry clustering already noted against `11.48` — that interaction
  is entirely unmodelled here.
- **Buy-and-hold on the same 32 names returned +1,933%** over the same window
  against arm A's +51.7%. Not risk-equivalent — the strategy is in the market a
  fraction of the time at 2% risk — but it is the relevant context for any
  conversation about whether this sleeve deserves its capital at all.
- **One universe, one strategy, one window.** The regime gate also governs
  other sleeves; nothing here says anything about those.

## 8. Limitations

- **Earnings blackout (`DonchianEdgeFilter` rule 2) is unmodelled** in all four
  arms — no offline equivalent exists. Bias direction is identical across arms.
- **Fills are next-bar-open.** Production uses STOP_LIMIT with a chase cap, so
  real entry prices differ; `11.54` covers that path.
- **Sector momentum is `warn`-mode in production** (logs only), so its absence
  here is faithful.
- **Regime is classified on SPY only**, matching production.

## 9. Next step — the question that would have to be asked properly

Before any change is contemplated, this needs the `11.56` treatment: a
**pre-declared** test with a bar to clear, written down before further looking,
so the answer cannot be tuned into existence. The candidate formulation:

> Replace the binary TRENDING-only gate with a **BEAR-only exclusion** (allow
> TRENDING, RANGING and VOLATILE; block BEAR), pre-declared, and require it to
> beat the current gate on the 2016–2026 SIP run **and** to not worsen maxDD by
> more than 3 points **and** to survive the 2022 sub-window specifically.

That formulation is chosen because it is the one the §4 and §5 evidence
actually points at — the gate's measured value is bear protection, and its
measured cost is everything it blocks outside bear markets. It is written here
as a *candidate to be pre-registered*, not as a proposal to implement.

---

## 10. PRE-REGISTRATION — BEAR-only exclusion

> **Registered 2026-08-18, before the arm was run.** Criteria, thresholds and
> the decision rule below were committed in the same commit that added the
> code, and before any arm-E number existed. If a later revision of this
> document changes a threshold, that revision is the finding — not the number
> it produced.

### 10.1 The change under test

**Arm E** — replace the binary TRENDING-only regime gate with a **BEAR-only
exclusion**:

```
entry_mask = (SPY regime != "BEAR") AND DonchianEdgeFilter(rules 1+3)
```

i.e. allow TRENDING, RANGING and VOLATILE; block only BEAR. The edge filter is
unchanged, so this isolates the regime gate. Every other knob stays at the
values in §2 and remains asserted by the test suite.

**Run parameters, fixed now:** `ai_bigtech` (32 symbols), SIP feed,
`trade_start = 2016-11-01`, `end = 2026-08-18`, `StaticATRStop(k=2.0)`,
`SIM_CONSTANTS` as in §2. No other universe, window or stop policy may be
substituted after seeing results.

### 10.2 Criteria — all three must pass

**Primary — does BEAR-only retain the protection that is the gate's only
demonstrated value?**

> **C1. 2022 sum R under arm E ≥ −20.0R.**

Reference points: raw/ungated **−47.8R**, current production **−9.1R**. This is
the decisive criterion because §5 showed 2022 is the *single year the current
gate earns its keep*. A BEAR-only rule is worth having only if it keeps most of
that. The outcome is genuinely unpredictable from anything published above:
2022's damage may well have arrived on RANGING or VOLATILE bars inside the bear
year, which this rule would let straight through. **If C1 fails, the
formulation is rejected regardless of how good the aggregate looks.**

**Secondary — aggregate performance.**

> **C2. Full-run mean total return (E) ≥ mean total return (D) + 5.0 pp**
> (i.e. ≥ +32.4%, against production's +27.4%).

**Declared weakness, stated before running:** §4 already shows BEAR is 160 of
1209 raw trades at a below-average 0.63R, so removing only BEAR is very likely
to land between arm C (+43.1%) and arm D (+27.4%). C2 is therefore close to
*implied* by results already published, and is recorded for completeness — **it
is not independent evidence and must not be reported as the headline.**

**Guardrail — the cost side.**

> **C3. Mean maxDD (E) no worse than mean maxDD (D) by more than 3.0 pp**
> (D = −15.0%, so E must be ≥ −18.0%).

Loosening a regime gate should be expected to cost drawdown; this bounds how
much is acceptable. Reference: raw/ungated is −17.5%.

### 10.3 Decision rule

| outcome | consequence |
|---|---|
| **C1 ∧ C2 ∧ C3 all pass** | Authorises **proposing** the config change via PR for paper observation. Not a live-behaviour change and not self-approving. |
| **any criterion fails** | **This formulation is rejected and not implemented.** |

**No substitutions.** If arm E fails, I will not re-cut the window, swap the
metric, tune the BEAR definition, or test "BEAR-and-VOLATILE excluded" as a
salvage and report it as this test. Any different formulation is a **new**
pre-registration, written before it is run, and must say that it followed a
failure. This clause exists because the `11.56` sweep returned 10 of 10 rules
"improving" against a negative baseline — which is what a sweep does, not what
an edge looks like.

**Precedence unchanged.** Passing does not override `11.56` re-open condition
(4) or [[feedback_trade_log_outranks_the_model]]. A pass authorises a proposal
under paper observation; the live trade log remains the arbiter.

### 10.4 Result — run 2026-08-18, criteria unchanged from commit `d799ac0`

**Verdict: PASS on all three.** Nothing in §10.1–10.3 was edited after running.

| criterion | threshold | arm E | production | |
|---|---|---:|---:|---|
| **C1 primary** — 2022 sum R | ≥ −20.0R | **−16.1R** | −9.1R | **PASS** |
| **C2 secondary** — full-run return | ≥ D + 5.0pp | **+41.0%** | +27.4% | **PASS** |
| **C3 guardrail** — mean maxDD | ≥ −18.0% | **−15.9%** | −15.0% | **PASS** |

Arm E in full: **997 trades, 41.5% win, mean R 0.66, mean return +41.0%,
Sharpe 0.52, maxDD −15.9%.**

| arm | trades | mean R | mean ret% | Sharpe | maxDD% |
|---|---:|---:|---:|---:|---:|
| A raw (no gates) | 1209 | 0.70 | +51.7 | 0.58 | −17.5 |
| D production | 714 | 0.62 | +27.4 | 0.39 | −15.0 |
| **E BEAR-only** | **997** | **0.66** | **+41.0** | **0.52** | **−15.9** |

#### Read C1 carefully — it passed, but it is the weakest of the three

2022 lands at **−16.1R against production's −9.1R**. The BEAR exclusion is
**worse than the current gate in the year the current gate exists for**, by 7R.
It cleared the pre-registered bar of −20.0R with 3.9R of margin, which is not
comfortable. Stated plainly because the bar was set in advance and cleared, not
because the result is unambiguous.

What makes it defensible: against the **ungated** −47.8R, arm E removes 66% of
the 2022 damage while retaining 25 of 103 entries. So the BEAR exclusion does
provide real bear protection — it is simply less absolute than blocking
everything that is not TRENDING.

#### The genuinely new evidence is the per-year shape

Sum R by entry year (A raw / D production / E BEAR-only):

| year | A raw | D production | **E BEAR-only** |
|---:|---:|---:|---:|
| 2016 | +22.8 | +21.8 | +21.8 |
| 2017 | +45.2 | +39.5 | **+44.4** |
| **2018** | −13.7 | −19.5 | **−9.8** ← best arm |
| 2019 | +103.7 | +32.4 | **+54.9** |
| 2020 | +92.4 | +36.9 | **+77.9** |
| 2021 | +36.3 | +8.3 | **+27.0** |
| **2022** | −47.8 | **−9.1** | −16.1 |
| 2023 | +168.9 | +62.4 | **+126.4** |
| 2024 | +246.7 | +110.5 | **+154.4** |
| 2025 | +141.7 | +95.8 | **+123.8** |
| 2026 | +48.3 | **+60.3** | +51.3 |

**Arm E beats production in 9 of 11 years**, losing only 2022 and 2026. More
tellingly, **it beats the ungated arm in both bad years** (2018: −9.8 vs −13.7;
2022: −16.1 vs −47.8) — which is the evidence that the BEAR exclusion is doing
real protective work rather than just trading more. In 2018 it is the best of
all three arms, where the current TRENDING-only gate is the worst.

C2 behaved exactly as the registration predicted it would: +41.0% sits between
arm C (+43.1%) and arm D (+27.4%). **Per §10.2 it is not reported as evidence.**

#### What this authorises, and what it does not

Per §10.3, a pass **authorises proposing the change via PR for paper
observation**. It is not a live-behaviour change, it is not self-approving, and
it does not override `11.56` re-open condition (4) or
[[feedback_trade_log_outranks_the_model]].

Carried forward into any such proposal, unchanged from §7:

- **These are not portfolio returns.** Arm E opens **997 trades against
  production's 714** — a 40% increase in entries and therefore in concurrent
  positions. Nothing here models the allocator sleeve budget, sector caps, or
  the correlated-entry clustering recorded against `11.48`, and that clustering
  is the mechanism that turned the live June cohort into a single correlated
  loss. **This is the largest unmodelled risk in the change** and it argues for
  pairing any gate loosening with the correlated-entry heat cap rather than
  shipping it alone.
- 2022 gets worse (−9.1R → −16.1R). That is a real, accepted cost, not noise.
- Earnings blackout unmodelled; fills are next-bar-open against production's
  STOP_LIMIT with chase cap.

---

**Related:** [`docs/donchian_trail_investigation.md`](donchian_trail_investigation.md)
(closed; stop policy), `11.48` (risk-target reconciliation — the 28.8×
sizing dispersion that amplifies the clustered losses), `11.56` (entry-quality
audit; closed with pre-registered re-open conditions).
