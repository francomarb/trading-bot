# Strategy Graduation Evidence Report

## Purpose

This report gives the operator a trustworthy, per-strategy evidence package.
It does not approve a strategy and does not change trading or preflight.

The unit of analysis is one completed `position_lifecycle`. Two partial exits
from one position are one outcome, and the legs of one MLEG spread are one
outcome. `position_lifecycle.net_realized_pnl` is the lifecycle total; the
report checks it against the sum of linked `trades.realized_pnl` events.

## Strategy identity

Every new lifecycle records three separate values at entry:

- `strategy_version`: a human-maintained semantic version. Increment it when
  the strategy's intended trading behavior changes.
- `strategy_config_hash`: an automatic fingerprint of the actual runtime
  strategy parameters, edge-filter configuration, allowed regimes, sleeve
  allocation, universe, effective data feed/timeframe, risk policy, and
  entry-cap policy. Active strategies and filters use an explicit field
  contract: runtime caches and observations are excluded by construction, and
  direct validation fails clearly for a component without a reviewed contract.
  Entry-time resolution logs the failure and stamps the lifecycle as unknown
  instead of allowing advisory reporting metadata to block a valid order.
- `bot_git_commit`: the exact running Git commit. A dirty checkout is recorded
  as `uncommitted:<commit>` rather than being presented as reproducible code.

Only version plus configuration hash defines a comparable performance cohort.
The bot commit is provenance: ordinary code fixes remain visible without
splitting otherwise identical strategy evidence.

Existing lifecycles are not relabeled. They appear under **unknown-epoch
history** as context and are excluded from versioned cohort metrics. A future
review may explicitly classify a historical interval with the optional
`--reviewed-epochs <json>` manifest. Each interval must carry its version,
configuration fingerprint, reviewer, review timestamp, and evidence reference.
The database remains unchanged; no report run may infer or backfill an epoch
automatically.

## Output and interpretation

Run:

```bash
./venv/bin/python scripts/strategy_graduation_report.py
```

The command writes matching schema-versioned JSON and Markdown files under
`logs/strategy_graduation/`. Status values are advisory:

- `DATA INCOMPLETE`: no completed lifecycle or an integrity mismatch exists.
- `EARLY EVIDENCE`: recorded, reconciled outcomes exist but the evidence is
  not a complete graduation case.
- `READY FOR OPERATOR REVIEW`: reserved for a later reviewed sufficiency
  contract; it still will not mean approved.

The default is the complete lifetime database view. Optional filters produce a
clearly labeled observation slice:

```bash
./venv/bin/python scripts/strategy_graduation_report.py \
  --start-date 2026-07-01 \
  --end-date 2026-09-30 \
  --strategy spy_options_reversion
```

Dates are inclusive UTC dates. Completed lifecycle outcomes are selected by
close date, daily marks by `mark_date`, and excluded trade-only P&L by event
date. Active lifecycles opened before the end date remain visible because they
intersect the observation window. Repeat `--strategy` to select more than one
strategy. The JSON `filters` object and Markdown `Report scope` line always
record the selection; omitted bounds mean earliest/latest. Filters never merge
strategy-version/configuration cohorts or replace the lifetime report.

The report includes lifecycle counts, realized P&L, expectancy, median, win
rate, profit factor, R coverage, realized drawdown and loss streak, monthly
consistency, entry regimes, outlier dependence, order outcomes, operator-order
count, external closes, calibration-grade execution slippage, modeled
regulatory costs, and forward daily total-P&L drawdown.

Only terminal lifecycles with at least one linked realized-P&L event and a
parent/ledger total that reconciles within one cent enter performance metrics.
External or recovered closes without durable economics remain visible as
`unresolved_economics`; they are not converted into zero-dollar outcomes. Any
such row keeps the cohort at `DATA INCOMPLETE` while valid outcomes and their
metrics remain visible.

The Markdown footer separately totals realized-P&L trade events that have no
matching lifecycle. It distinguishes rows with no lifecycle ID from rows whose
old identifier has no lifecycle parent. These values explain differences from
trade-row dashboards but remain context only: they are neither lifecycle
outcomes nor versioned cohort evidence.

### Forward daily marks

After each engine cycle, `strategy_daily_marks` keeps the latest broker
observation for each strategy-version/configuration cohort and UTC date. Its
total is:

`durable realized trade P&L to date + broker-reported unrealized P&L`

Single-leg positions use their exact broker symbol. MLEG positions sum the
broker-reported P&L of every recorded leg. A missing position, missing leg,
non-finite broker value, errored lifecycle, or unreconciled terminal lifecycle
makes that cohort/day incomplete and leaves `total_pnl` NULL. No price is
invented. Realized events later than the broker snapshot are deferred to the
next snapshot, preventing old position marks from being mixed with new ledger
events. Repeated cycles replace only that day's observation; older days are
immutable. The resulting `forward_daily_total_max_drawdown` begins when this
collector is deployed and does not pretend to reconstruct earlier marks. A
report run before the engine creates the new table treats forward marks as not
yet collected; an existing table with the wrong schema remains an error. If a
cohort has incomplete days, drawdown is calculated from its complete observed
days while mark coverage stays explicit and the cohort remains `DATA
INCOMPLETE`. Because a missing day could hide a deeper trough, that observed-day
drawdown may understate the true drawdown.

### Reviewed regulatory-cost model

Realized lifecycle P&L already uses actual broker fill prices, so execution
slippage is already present and is not deducted again. Report schema v3 applies
the versioned `alpaca-retail-us-2026-06-01-v1` pass-through schedule to actual
filled quantities:

| Cost | Rate |
|---|---:|
| Alpaca self-directed API commission | $0/order |
| Equity TAF, sells | $0.000195/share, $9.79/order cap |
| Option TAF, sells | $0.00329/contract |
| Option ORF, buys and sells | $0.02295/contract |
| OCC clearing, buys and sells | $0.025/contract |
| SEC transaction fee, sells | $20.60 per $1,000,000 principal |
| CAT fees, buys and sells | $0.000003/executed-equivalent share |

The model is effective for fills on or after 2026-06-01. Its version, rates,
effective date, and primary sources are embedded in every JSON report. Sources:
[Alpaca Brokerage Fee Schedule](https://files.alpaca.markets/disclosures/library/BrokFeeSched.pdf),
[FINRA 2026 TAF schedule](https://www.finra.org/rules-guidance/rule-filings/sr-finra-2024-019/fee-adjustment-schedule),
and [CAT fee alerts](https://www.catnmsplan.com/cat-fee-alerts).

If a round trip is missing a side, predates the model, or lacks sell principal,
`net_after_costs` remains unavailable. In particular, current MLEG rows store a
net combo price rather than both individual leg premiums, so their SEC
sell-principal component cannot be reconstructed and credit-spread cost
coverage remains explicitly incomplete.

The database is opened read-only by the report. A missing path or an older
lifecycle, trade, order, or mark schema produces an actionable error and never
creates or migrates a file as a side effect of reporting.

## Honest limitations

Pre-deployment daily marks remain unavailable and are never backfilled. Modeled
fees are not a broker fee ledger; changing rates requires a new reviewed model
version. Binding an operator-approved strategy/epoch/report digest into live
preflight remains deliberately separate and must happen only after generated
reports and evidence-sufficiency rules have been reviewed.
