# Watchlist Fundamentals Parsing Audit And Proposal

**Roadmap item:** `11.72`

**Audit date:** 2026-09-20

**Status:** Implementation complete on the PR branch; universe decision pending

## Decision Summary

Implement one focused correctness PR for the shared fundamentals boundary, but
do not silently promote the resulting RSI or Donchian membership changes in
that PR.

The implementation should:

1. parse a small, explicit, ordered set of semantically equivalent Yahoo annual
   net-income rows;
2. retain which row supplied the value;
3. preserve the existing fail-closed scanner policy while distinguishing
   provider/data uncertainty from an affirmative solvency failure;
4. prevent the standalone fitness report from calling an unknown required
   check `GOOD FIT`;
5. apply consistent unknown handling to every active consumer of the shared
   parser; and
6. regenerate review-only RSI and Donchian reports, then ask the operator to
   approve any configured-universe changes separately.

Do not recycle the Donchian cohort from PR #160 until the implementation and
the resulting membership review are complete.

## What Was Audited

- `scripts/watchlist_review.py`: shared Yahoo fetcher, tri-state assessment,
  standalone report, and process exit status.
- `scripts/rsi_watchlist_scan.py`: active RSI durable-pool selector.
- `scripts/donchian_watchlist_scan.py`: active Donchian durable-pool selector.
- `scripts/sma_watchlist_scan.py`: active SMA research selector and its use of
  the shared display verdict as an eligibility decision.
- `scripts/rsi_static_universe.py`: historical builder retained for research.
- Current tests, committed RSI/Donchian reports, configuration, and the
  `yfinance 1.3.0` statement schema shipped in the project environment.

The audit used current Yahoo annual statements for the symbols named by the
PR #160 review and delayed-SIP bars from the original committed report windows.
No active watchlist, runtime setting, or bot process was changed.

## Findings

### 1. The ISRG rejection is a parser miss

The fetcher asks only for the exact row `Net Income`. Yahoo's current ISRG
annual statement does not expose that row, but does expose these positive
values:

- `Net Income Common Stockholders`: about $2.856 billion;
- `Net Income From Continuing And Discontinued Operation`: about $2.856
  billion; and
- `Net Income From Continuing Operation Net Minority Interest`: about $2.856
  billion.

The actual shared fetcher therefore returns a valid market cap, no fetch error,
and `is_profitable=None`. Both durable-pool scanners correctly fail closed, but
their reports cannot say whether the company failed the rule or the fact was
unavailable.

The project's installed `yfinance` schema explicitly requests `NetIncome`,
`NetIncomeCommonStockholders`, and the other variants as separate fields. This
is provider schema variation, not evidence that ISRG lacks profits.

### 2. The other current Donchian solvency rejections remain genuine

Using the existing exact `Net Income` row and the scanner's current cash
definition produced these approximate cash runways:

| Symbol | Net-income result | Cash runway | Current 12-month rule |
|---|---:|---:|---|
| LITE | loss | 4.7 months | fail |
| MSTR | loss | 7.2 months | fail |
| KHC | loss | 7.5 months | fail |
| CIFR | loss | 9.2 months | fail |
| MARA | loss | 5.0 months | fail |

Adding the narrow ISRG fallback does not turn these failures into passes.

### 3. Unknown and failed are conflated differently by each consumer

| Consumer | Current unknown-data behavior | Risk |
|---|---|---|
| Donchian scanner | Rejects safely, reports all non-passes as `solvency` | False exclusion is indistinguishable from failed runway |
| RSI scanner | Rejects safely, combines missing/fetch failure with `solvency`; also combines unknown market cap with below-threshold market cap | Audit explanation can assert the wrong fact |
| SMA scanner | Uses `fitness.verdict != GOOD FIT`, but the verdict currently treats missing required fields as `GOOD FIT` | A company with unavailable FCF, revenue, or solvency can pass the fundamental gate |
| Standalone watchlist review | Treats `None` required checks as `GOOD FIT` and can exit zero | Operator-facing result contradicts its legend that all required checks passed |
| Historical RSI static builder | Rejects only `solvency_ok is False`; `None` passes | Legacy research can treat unknown solvency as acceptable |

The existing tests explicitly encode both sides of this inconsistency: scanner
tests require unknown solvency to fail closed, while fitness tests assert that
all-`None` required checks are `GOOD FIT`.

### 4. Correcting the parser changes a real universe boundary

On the frozen 2026-09-19 Donchian report window, ISRG's 50-session average SIP
dollar volume was about $1.182 billion. It slots between current ranks 83 and
84, so the isolated parser correction would place ISRG at approximately rank
84 and move current rank-100 LIN to rank 101.

ISRG remains below the RSI report's top-50 liquidity boundary, so this parser
fix alone does not change the frozen RSI top 50.

### 5. The committed RSI report also predates the dot-class fix

PR #160 fixed the shared provider boundary from `BRK.B` to Yahoo's `BRK-B`, but
the committed RSI report was generated on 2026-09-09. It still falsely lists
BRK.B as below the market-cap threshold.

On that report's frozen bar window, BRK.B had about $2.113 billion in 50-session
average SIP dollar volume. With its fundamentals now resolved it would rank at
approximately 42, moving BAC from rank 50 to rank 51. This is not caused by the
ISRG alias change, but it will appear when `11.72` regenerates the RSI report
and must be identified separately in the review.

### 6. Partial provider data is normal and must remain first-class

The shared fetcher can return some fields successfully while another field is
unavailable without raising an exception. In the audit run:

- ISRG had a valid market cap but unknown profitability under the current
  parser;
- AZO had positive net income but an unavailable market cap; and
- BRK.B resolved both after provider-symbol normalization.

A single whole-request `error` string is therefore insufficient provenance.
Missing fields must not be converted into negative financial facts or a passing
assessment.

## Proposed Contract

### Raw fact layer

Keep `fetch_fundamentals` strategy-agnostic and preserve raw provenance:

- add `net_income_annual: float | None`;
- add `net_income_source: str | None`;
- retain `is_profitable: bool | None` as a derived convenience field;
- retain `None` when no approved row has a finite value; and
- never infer zero, loss, or insolvency from a missing row.

Use this ordered annual-income policy:

1. `Net Income` — preserves current behavior wherever Yahoo supplies it;
2. `Net Income Common Stockholders` — narrow fallback for earnings attributable
   to common shareholders and the verified ISRG case.

Do not use fuzzy substring matching. Do not automatically fall through to
`Net Income Including Noncontrolling Interests` or a continuing-operations-only
measure: those have different ownership/scope semantics and can change loss
magnitude and runway. A future alias can be added only with a fixture proving
its intended accounting equivalence for this policy.

### Assessment layer

Retain `solvency_ok: bool | None`, but add an explicit reason with this contract:

- `profitable` -> `True`;
- `runway_sufficient` -> `True`;
- `runway_insufficient` -> `False`;
- `profitability_unknown` -> `None`;
- `cash_unknown_for_unprofitable_company` -> `None`; and
- fetch exception -> existing error path.

For the standalone `StrategyFitness.verdict`, use this precedence:

1. fetch error -> `ERROR`;
2. any affirmative required-check failure -> `POOR FIT`;
3. any unknown required check -> `UNKNOWN`;
4. optional-check failure -> `MARGINAL`;
5. otherwise -> `GOOD FIT`.

`UNKNOWN` must be non-zero at the CLI boundary and must appear in the flagged
section. Optional unavailable fields may remain neutral, but required unavailable
fields can never support `GOOD FIT`.

### Scanner layer

All active scanners should use structured facts, not the display verdict, for
eligibility and rejection reasons:

- `market_cap_unknown`: market-cap field unavailable;
- `market_cap`: known value below the threshold;
- `fundamentals_error`: provider request raised an exception;
- `solvency_unknown`: profitability or required cash fact unavailable;
- `solvency`: known loss with known runway below the strategy threshold; and
- pass only when `solvency_ok is True`.

Requested-symbol explanations should include the precise reason and, where
available, the source row and calculated runway. Reports should state that
unknowns are conservatively excluded, not financially failed.

The same fail-closed rule should be applied to the historical static builder so
that a future manual run cannot treat `None` as a pass, although its historical
reports do not need regeneration.

## Implementation Plan

### Phase A — shared facts and assessment

Files:

- `scripts/watchlist_review.py`
- `tests/test_watchlist_review.py`

Work:

- add the explicit two-row net-income policy and provenance fields;
- add the solvency reason contract;
- introduce the `UNKNOWN` standalone verdict;
- update report counts, legend, flagged details, and exit behavior; and
- keep exact `Net Income` first so existing companies do not change source or
  runway merely because more Yahoo fields are present.

### Phase B — consumer consistency

Files:

- `scripts/rsi_watchlist_scan.py`
- `scripts/donchian_watchlist_scan.py`
- `scripts/sma_watchlist_scan.py`
- `scripts/rsi_static_universe.py`
- their focused tests and watchlist-selection documentation.

Work:

- split unknown, failed, and request-error rejection reasons;
- make SMA eligibility independent of a human-readable verdict string;
- make the historical static gate require affirmative solvency; and
- preserve each strategy's existing market-cap and runway thresholds.

### Phase C — report-only impact review

1. Run focused and full tests.
2. Generate sanitized RSI and Donchian reports with fundamentals enabled.
3. Run private promotion copies that include open-position protection.
4. Compare separately:
   - parser-driven changes such as ISRG;
   - the earlier BRK.B provider-boundary correction; and
   - ordinary market-data/provider drift since the prior report.
5. Verify configured holdings and unresolved orders cannot be orphaned.
6. Present the proposed membership diffs for explicit operator approval.
7. Only after approval, update configuration and start a new evidence cohort.

The implementation PR may include regenerated candidate reports, but it should
not change `RSI_WATCHLIST` or `DONCHIAN_WATCHLIST` without that explicit
approval.

## Required Tests

- Exact `Net Income` remains first when both approved rows exist and differ.
- `Net Income Common Stockholders` supplies positive and negative fallback
  values when exact `Net Income` is absent.
- Missing/NaN/non-finite approved rows remain unknown.
- Profitable, sufficient-runway, insufficient-runway, missing-profitability,
  missing-cash, and fetch-error cases have distinct assessment reasons.
- Missing required SMA facts produce `UNKNOWN`, never `GOOD FIT`.
- RSI and Donchian map each tri-state outcome to the correct rejection reason
  and requested-symbol explanation.
- SMA rejects unknown required fundamentals without consulting display text.
- Historical RSI static selection requires `solvency_ok is True`.
- Frozen boundary fixtures demonstrate the expected ISRG and BRK.B effects
  without relying on live Yahoo or Alpaca calls in unit tests.
- Existing open-position protection tests remain green.

## Non-Goals

- No change to the $2 billion market-cap floor.
- No change to the 12- or 18-month runway thresholds.
- No change to RSI/Donchian pool sizes or liquidity ordering.
- No fuzzy accounting-row discovery.
- No new fundamentals cache or provider migration; broader cache and retry
  design remains under `11.71`.
- No automatic watchlist promotion or bot recycle.

## Acceptance Recommendation

Approve the contract above, then implement Phases A and B in one focused PR.
Use Phase C to produce an operator decision rather than treating parser-correct
membership changes as automatic. Close `11.72` only after the shared behavior,
all consumers, regenerated reports, and any explicitly approved universe
changes are synchronized.

## Implementation And Candidate-Report Result

The approved contract was implemented on 2026-09-20 without changing either
configured universe:

- the shared parser now uses exact `Net Income` first, then the narrow
  `Net Income Common Stockholders` fallback, and records both value and source;
- assessment exposes explicit solvency provenance and the standalone review
  uses a non-passing `UNKNOWN` verdict for unavailable required facts;
- RSI, Donchian, and SMA now separate provider errors, unavailable facts, and
  affirmative threshold failures;
- SMA eligibility no longer depends on a display verdict string; and
- the historical RSI builder now requires affirmative solvency.

Review-only candidate artifacts:

- [`reports/rsi_watchlist_scan_11_72_candidate.md`](reports/rsi_watchlist_scan_11_72_candidate.md)
- [`reports/donchian_watchlist_scan_11_72_candidate.md`](reports/donchian_watchlist_scan_11_72_candidate.md)

Current candidate differences, not yet promoted:

| Strategy | Proposed additions | Proposed removals | Attribution |
|---|---|---|---|
| Donchian | ISRG (rank 84) | LIN (moves to 101) | Isolated parser correction on an otherwise unchanged weekend market-data window |
| RSI | BRK.B, COHR, JNJ | COST, GLW, TXN | Combined effect of the earlier BRK.B provider fix and liquidity drift since the 2026-09-09 promoted report |

The sanitized reports intentionally omit account state. A private ledger check
confirmed that the current configurations cover every open RSI and Donchian
position. If the candidate pools are promoted, two RSI holdings and one
Donchian holding would need to remain as lifecycle-protection additions outside
the ranked pools. Their identities must remain in the private promotion check,
not the committed report.

Validation:

- focused fundamentals/scanner tests: 175 passed, one existing warning;
- full suite: 3,855 passed, five existing numerical warnings; and
- generated reports contain no `solvency_unknown` or `fundamentals_error`
  rejection in this successful provider run. AZO remains honestly reported as
  `market_cap_unknown`.

No bot recycle, strategy setting, or active watchlist change was performed.
