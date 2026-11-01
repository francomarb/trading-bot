# Cluster Earnings Reaction Strategy — Research Design

**Status:** Parked research design. No production strategy, paper sleeve,
engine integration, or trading authorization exists. The first possible phase
is prospective, observation-only data collection.

**Last updated:** 2026-10-31

---

## Why this document exists

Large technology and AI-related companies report on different dates, but their
results are not independent. A hyperscaler's capital-spending outlook can
change expectations for accelerator, networking, memory, power, cooling, and
semiconductor-manufacturing companies. A supplier's results can reveal demand,
capacity, pricing, or execution conditions for other members of the same value
chain.

The proposed edge is not ordinary correlation and is not inherently bullish or
bearish. It is the combination of:

1. what each earnings event reveals about a connected cluster;
2. how the market rewards or punishes that information;
3. how that evidence accumulates in chronological order; and
4. which not-yet-reported or newly affected company offers the best
   risk-adjusted expression of the current cluster state, up or down.

The motivating downside pattern is a sequence in which stronger companies
publish good results but are still punished. That suggests an unusually high
market hurdle. A more fragile company reporting later may then offer an
asymmetric downside opportunity if its expectations remain high and its puts
do not already price the likely decline. The bullish mirror is a sequence in
which good or merely adequate results are rewarded across the cluster. A later
reporter or lagging beneficiary may offer the better upside expression.

This is a research hypothesis, not evidence that such trades are predictably
profitable. Historical analysis may justify a paper observation; only clean
forward outcomes can carry the strategy toward a deployment decision.

---

## Governing principle

> First understand the cluster's current earnings-reaction pattern, then find
> the best instrument for expressing it — up or down.

The chronological constraint is essential:

> At every decision time, accumulate only the cluster evidence already
> revealed, update the market-reaction regime, and rank only the companies and
> instruments that were genuinely available at that time.

A quarter-wide score calculated after every company has reported must never be
applied retroactively to an earlier reporter. That would be look-ahead bias.

---

## Starting research universe

The initial universe should be deliberately small enough to audit. Membership
is a registered research hypothesis, not a claim that every name has equal
economic relevance.

### Core information sources

The starting source set is the Magnificent Seven plus major AI and
semiconductor stalwarts:

| Role | Initial names | Primary information channel to test |
|---|---|---|
| Hyperscaler / AI-capex sources | MSFT, META, GOOGL/GOOG, AMZN | Cloud demand, accelerator and CPU demand, networking, datacenter construction, power and cooling |
| Accelerator and compute suppliers | NVDA, AMD, AVGO | Accelerator demand, custom silicon, networking, supply, pricing, product transitions |
| Semiconductor manufacturing | TSM, ASML | Leading-edge demand, capacity, equipment and process conditions |
| Memory | MU | HBM/server-memory demand, pricing and supply |
| Other Mag 7 context | AAPL, TSLA | Mega-cap risk appetite and any specifically documented AI, datacenter, silicon, or supply-chain read-through |

Mag 7 membership alone does not make an event relevant to every AI cluster.
AAPL or TSLA, for example, should receive little or no weight for a datacenter
memory hypothesis unless the report contains a documented connection. The
hyperscalers and core suppliers are expected to carry more direct information
for AI infrastructure, but that weighting must be registered and tested rather
than assumed from market capitalization.

### Initial candidate clusters

Clusters overlap and relationships are directed. The first mapping should be
manually registered from economic exposure, then evaluated rather than edited
to fit observed returns.

| Cluster | Example sources | Example candidates |
|---|---|---|
| Hyperscaler AI spending | MSFT, META, GOOGL/GOOG, AMZN | NVDA, AVGO, ANET, VRT, MRVL, AMD |
| Accelerators / custom silicon | NVDA, AMD, AVGO | AMD, AVGO, MRVL, MU, ANET, VRT |
| Networking | ANET, AVGO, MRVL | ANET, MRVL, CSCO and exposed suppliers |
| Memory | MU and relevant manufacturer reports | MU, NVDA, AMD and server-hardware beneficiaries |
| Datacenter power and cooling | Hyperscalers, VRT | VRT, ETN, CARR and directly exposed infrastructure names |
| Semiconductor manufacturing | TSM, ASML | NVDA, AMD, AVGO, AMAT, LRCX and other directly exposed suppliers |
| AI software / platform demand | MSFT, META, GOOGL/GOOG, AMZN | A separately registered set of software and platform candidates |

This table is a seed, not an approved trading universe. Before a formal study,
each directed source-to-candidate edge needs an economic rationale, an
effective date, and a rule for historical membership. Today's winners must not
be projected backward into periods when their exposure or public-company
status was different.

---

## The two-axis event model

Each earnings event has two distinct observations.

### 1. Fundamental result

The report should eventually be classified across dimensions such as:

- AI and cloud demand: accelerating, stable, or weakening;
- capital spending: raised, maintained, or reduced;
- supply: constrained, balanced, or excess;
- pricing: improving, stable, or deteriorating;
- guidance: above, broadly aligned with, or below the prior expectation set;
- margins: company-specific issue or evidence of a wider industry condition;
- execution: improving, neutral, or disrupted; and
- cluster-relevant management commentary.

A headline EPS beat/miss is insufficient. A company can miss EPS while raising
cluster-relevant capital spending, or beat the quarter while issuing guidance
that changes the forward cluster outlook.

### 2. Market reaction

The event reaction should be measured over registered windows and relative to
the appropriate market and sector benchmarks. Candidate windows include:

- release to the next observable market snapshot, when trustworthy data exist;
- next regular-session open to 30 minutes;
- prior close to next regular-session close;
- one full session after the initial reaction; and
- three full sessions after the initial reaction.

QQQ, SMH, SPY, and a registered equal-weight peer basket are possible
benchmarks. The benchmark choice must be fixed per cluster before outcome
review. A stock rising 3% when its relevant sector also rises 3% is not a 3%
abnormal response.

### Reaction-regime interpretation

| Fundamental result | Market reaction | Initial interpretation |
|---|---|---|
| Strong | Strongly positive | Bullish confirmation |
| Average | Positive | Strong appetite; relatively forgiving hurdle |
| Weak | Flat or positive | Bad news may already be discounted; strongly risk-seeking reaction |
| Strong | Negative | Demanding or hostile reaction regime; expectations may be too high |
| Average | Negative | Weak appetite and little tolerance for imperfection |
| Weak | Strongly negative | Bearish confirmation |

The contradictions are especially informative: good results being punished,
average results being rewarded, weak results being absorbed, or strong results
receiving little incremental reward.

---

## Chronological evidence state

For reporter event `i`, define:

- `F_i`: a point-in-time fundamental event score;
- `R_i`: its benchmark-adjusted market reaction; and
- `U_i`: the reaction unexplained by the quality of the reported result.

Conceptually:

```text
U_i = R_i - expected_reaction(F_i)
```

For a candidate evaluated at time `t`, the cluster state may be represented as:

```text
C_t = sum(w_i,t * U_i for events with usable_time_i < t)
      / sum(w_i,t for events with usable_time_i < t)
```

`usable_time_i` is not necessarily the press-release time. If the registered
signal requires the next regular-session close, that event cannot contribute
until that close exists. Two companies reporting after the same close cannot
use each other's unobserved market reaction. Exact timestamps and decision
cutoffs must decide eligibility, never a date alone.

`w_i,t` may eventually incorporate:

- economic relevance to the candidate;
- historical directed transmission strength;
- event recency and decay;
- reporter importance within the cluster;
- classification confidence;
- independence from earlier evidence; and
- contamination by macro, regulatory, or market-wide events.

The research state should keep three concepts separate:

1. **Fundamental cluster state:** what reports say about demand, spending,
   pricing, supply, margins, and execution.
2. **Market reaction state:** how investors currently reward or punish those
   results.
3. **Confidence state:** how much independent, relevant, timely evidence has
   accumulated.

Later reporters may receive a stronger signal because more evidence is
available, but event count alone must not create confidence. Three reporters
reacting to one macro shock are not three independent confirmations.

---

## Candidate selection

The strategy should rank candidates, not mechanically trade every remaining
reporter in the current cluster direction.

### Negative cluster-reaction regime

Potential downside candidates may have:

- high expectations or a large pre-earnings run-up;
- weaker execution or business quality than the companies already punished;
- stretched valuation relative to their own history and realistic growth;
- high historical earnings or cluster beta;
- crowded positioning or fragile momentum;
- material exposure to the cluster driver under pressure;
- company-specific guidance, margin, supply, or execution risk; and
- puts whose implied distribution does not already capture the expected tail.

### Positive cluster-reaction regime

Potential upside candidates may have:

- direct exposure to the favored demand or spending trend;
- credible fundamentals and an attainable expectation hurdle;
- high historical sensitivity to the confirming reporters;
- improving but not fully extended momentum;
- incomplete participation relative to relevant peers; and
- calls, call spreads, or equity prices that do not already embed the expected
  upside.

The weakest company is not automatically the best put, and the strongest is
not automatically the best long. A heavily discounted weak company can rally
on an adequate report; an excellent company can fall if perfection is already
priced. The desired candidate is the one with the largest point-in-time gap
between its predicted return distribution and the distribution implied by its
price and available instruments.

Conceptually:

```text
candidate_edge =
    cluster_state
    * source_to_candidate_relevance
    * candidate_reaction_sensitivity
    * candidate_quality_or_fragility_adjustment
    - move_already_implied
```

This is a research decomposition, not an approved formula or fitted model.

---

## Trade families to study separately

The concept contains distinct hypotheses that must not be pooled merely to
increase sample size.

### A. Pre-earnings candidate selection

Use prior, chronologically eligible cluster events to rank companies that have
not yet reported. The prospective instrument could be equity, a call or put,
or a defined-risk vertical spread. Directional correctness is not enough for
long options: the predicted move must exceed premium, spread, volatility-crush,
and breakeven requirements.

### B. Post-event read-through continuation

After a reporter reveals new cluster information, identify related companies
that have not incorporated the justified read-through.

### C. Post-event sympathy overreaction

Identify related companies pulled beyond their justified response by ETF,
index, options-hedging, momentum, or indiscriminate sector flows.

Each family needs its own entry clock, expected holding period, benchmark,
cost model, and outcome definition.

---

## Observation-first data design

Historical earnings and options data are unusually vulnerable to revised
timestamps, later consensus estimates, survivorship, and reconstructed implied
moves. Prospective collection is therefore valuable even before a backtest is
attempted.

### Proposed event ledger

For every source and candidate earnings event, retain:

| Group | Point-in-time fields |
|---|---|
| Identity | Symbol, company, fiscal quarter, source/candidate roles, registered cluster memberships |
| Timing | Scheduled report time as known then, actual release timestamp, before-open/after-close status, call timestamp, next decision cutoff |
| Expectations | Timestamped consensus revenue/EPS where licensed and available, company guidance, options-implied move and expiration |
| Fundamental result | Reported revenue/EPS, guidance changes, registered cluster-dimension labels, source citations, label author and freeze time |
| Market context | SPY/QQQ/SMH and peer-basket levels, macro announcements, market regime, material company-specific concurrent news |
| Reactions | Reporter and candidate returns over each registered window, benchmark-adjusted returns, volume and volatility context |
| Options | Point-in-time bid/ask, strike, expiration, IV/Greeks where available, modeled breakeven, next-session executable marks |
| Evidence state | Eligible earlier events, weights, fundamental/reaction/confidence state, conflicts and contamination flags |
| Candidate snapshot | Price run-up, valuation/expectation inputs, momentum, historical sensitivity, predicted direction and rank |
| Outcome | Equity and option/spread outcomes after realistic bid/ask and the registered exit rule |

Raw source records should be retained append-only where licensing permits.
Corrections should create a new version rather than silently rewriting what the
observer knew at the time.

### Data-source posture

- Company investor-relations releases and SEC filings are authoritative for
  reported results and guidance.
- Alpaca delayed SIP data can support regular-session stock and ETF reaction
  measurement for offline research.
- Point-in-time consensus estimates and historical option chains require a
  trustworthy licensed source; current estimates reconstructed later are not a
  substitute.
- Transcript or news NLP is a later phase. The first prospective ledger can use
  a small registered manual rubric with citations and immutable labels.
- If an input is unavailable, record it as unavailable. Do not silently replace
  it with current data or a guessed value.

---

## Proposed observation phases

### Phase 0 — Register the protocol

Before collecting outcomes:

1. freeze the initial source set, candidate clusters, and directed edges;
2. define event timestamps and when each reaction becomes usable;
3. define fundamental labels and benchmark selection;
4. define reaction windows and contamination rules;
5. define candidate snapshots and option-cost fields; and
6. define a versioned schema so later improvements create explicit evidence
   boundaries.

### Phase 1 — Prospective read-only ledger

Collect events and render a chronological scorecard. It may report what the
method would have ranked, but it must not connect to `TradingEngine`, allocate
capital, request an order, or change any active sleeve.

The initial goal is data integrity and operational feasibility, not a return
claim. At least one full earnings cycle is likely needed to expose timestamp,
coverage, and classification problems.

### Dashboard destination registered; observation contract remains open

The five-page dashboard now reserves an **Earnings Clusters** area within
Market Observatory. It is deliberately a non-calculating placeholder: no event
ledger, backfill, classification, candidate generation, or broker connectivity
was added with the navigation redesign. The next deliverable remains the
read-only observation contract below. Use the Q3 2026 earnings round in progress
as of 2026-10-31 as the first bounded observation cohort, starting with the
earliest recoverable core source event and continuing prospectively through the
end of the round.

The draft should specify:

- an exact chronological event timeline, including scheduled, release, call,
  reaction-usable, and candidate-decision timestamps;
- the point-in-time fundamental classification and market reaction for each
  eligible Mag 7 or registered AI-stalwart event;
- separate fundamental, market-reaction, and confidence states for each
  cluster, with every contributing event and weight visible;
- later reporters and sympathy candidates that remained available at each
  snapshot, including hypothetical direction, rank, and the evidence then
  available;
- stock and option-implied movement where trustworthy point-in-time data are
  available, with missing historical option data disclosed rather than
  reconstructed from current chains;
- benchmark-adjusted reaction windows and a clear indication of which windows
  were still incomplete at the observation cutoff;
- provenance, coverage, contamination, disagreement, and correction flags;
- an immutable **as observed then** view alongside any separately labeled
  corrected or enriched research view; and
- prominent labeling that the section is observational research, not an
  active strategy, recommendation, allocation, or order control.

The first deliverable is a dashboard/data-contract design tied back to the
event-ledger schema above. Dashboard code, backfill jobs, automated
classification, and broker connectivity require separate review. The current
round must not be presented as a successful example selected after its outcome;
include every registered source event and every candidate produced by the
frozen protocol, including neutral, losing, missing, and contaminated cases.

### Phase 2 — Retrospective event study

Only after the contract is stable, reconstruct prior events with explicit
coverage grades. Measure reporter-to-candidate abnormal returns and keep
development and time-ordered held-out periods separate. Historical options
results are valid only where point-in-time chains and executable bid/ask data
exist.

### Phase 3 — Shadow candidate observation

If the event study finds a plausible effect, pre-register a simple ranking rule
and record hypothetical candidates prospectively. Compare every qualified
candidate, including losing and rejected opportunities. Do not tune the rule
against the same cycle used to judge it.

### Phase 4 — Separate paper proposal

Only a later reviewed proposal may request paper orders. It would need explicit
entry and exit rules, option-selection and liquidity contracts, maximum-loss
sizing, event-gap controls, lifecycle/restart behavior, reporting, and focused
tests. Historical results alone cannot authorize this phase.

---

## First research questions

1. Does the reaction to a reporter contain incremental information about later
   reporters or sympathy candidates after controlling for QQQ, SMH, and the
   broader market?
2. Does accumulating multiple independent events improve directional accuracy
   or only restate a move already present in prices?
3. Are contradictory reactions — good results punished or average results
   rewarded — more informative than ordinary positive/negative reactions?
4. Which directed edges are stable enough to survive a later earnings season?
5. Does the signal predict movement beyond the option-implied breakeven, not
   merely the correct direction?
6. Do continuation, overreaction, and pre-earnings trades behave differently
   enough to require separate clusters or scoring rules?
7. Does reporting late in the cycle improve the signal after accounting for
   the possibility that the accumulated information is already priced?

The first falsifiable study should ask:

> Using only information available before each candidate's report, do earlier
> Mag 7 and AI-stalwart earnings events identify later companies whose
> benchmark-adjusted move exceeds the contemporaneous price or option-implied
> hurdle, and does the result persist in a later earnings cycle?

---

## Principal failure modes

- Small samples: even 12–20 quarterly releases per reporter remain limited.
- Look-ahead leakage through revised earnings calendars, consensus estimates,
  final transcripts, cluster membership, or full-quarter scores.
- Survivorship bias from applying today's AI leaders to earlier periods.
- Same-close reports whose reactions were not observable before one another.
- Macro, regulatory, product, financing, or geopolitical news contaminating an
  apparent earnings response.
- Double-counting several reporters that repeat the same underlying fact.
- Data-mined clusters, weights, windows, labels, and thresholds.
- Historical option marks that were not executable at the recorded bid/ask.
- Correct directional calls that still lose after implied-volatility collapse.
- A later reporter having a stronger evidence score but no remaining price
  edge because the market has already incorporated the sequence.
- Treating a favorable backtest as stronger than clean forward observation.

All reports must show event counts, independent confirmation counts, missing
coverage, contamination exclusions, and results with the best event removed.

---

## Current decision

Preserve this as a parked research track. The next defensible action, when
prioritized, is Phase 0 followed by a prospective observation-only ledger for
the Mag 7 and registered AI-stalwart source set. Do not add a seventh active
paper sleeve, alter the six current strategies, or infer trading authorization
from this document.
