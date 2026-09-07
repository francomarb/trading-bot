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

The report includes lifecycle counts, realized P&L, expectancy, median, win
rate, profit factor, R coverage, realized drawdown and loss streak, monthly
consistency, entry regimes, outlier dependence, order outcomes, operator-order
count, external closes, and calibration-grade execution slippage.

Only terminal lifecycles with at least one linked realized-P&L event and a
parent/ledger total that reconciles within one cent enter performance metrics.
External or recovered closes without durable economics remain visible as
`unresolved_economics`; they are not converted into zero-dollar outcomes. Any
such row keeps the cohort at `DATA INCOMPLETE` while valid outcomes and their
metrics remain visible.

The database is opened read-only. A missing path or an older lifecycle, trade,
or order schema produces an actionable error and never creates or migrates a
file as a side effect of reporting.

## Honest limitations in the first deployment

The database has no historical daily mark-to-market series and no complete,
reviewed fee ledger. Therefore the report labels total-P&L drawdown and net
after costs as unavailable; it never substitutes realized-only drawdown or
zero fees under those names. Realized-only drawdown is shown explicitly.

Forward collection of daily strategy marks and a reviewed cost model are the
next report increments. Binding an operator-approved strategy/epoch/report
digest into live preflight is also deliberately separate and must happen only
after the evidence contract and generated reports have been reviewed.
