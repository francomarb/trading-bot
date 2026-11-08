# Earnings observation ledger operations

The ledger and standalone collectors implement the v0.1 observation contract.
They are invoked through `scripts/earnings_observations.py`; there is no engine
hook or automatic startup job. Market Observatory still shows its placeholder.
The next dashboard PR can use `reporting/earnings_observation_reader.py` and the
sanitized CLI workflow exercised in `tests/test_earnings_observations.py`.

## Configuration and ownership

- `config/earnings_registry_v1.json` contains the frozen issuer, alias, cluster,
  benchmark, window-version and rubric-version definitions.
- `EARNINGS_OBSERVATION_DB_PATH` defaults to `data/earnings_observations.db`.
  This must be a dedicated database. Initialization rejects an existing
  unrelated database. Updates and deletes of earnings records are rejected by
  SQLite triggers.
- `EARNINGS_OBSERVATIONS_ENABLED` defaults to `false`. Set it to `true` for
  the standalone `collect` or `sec-source` commands. Local registration,
  classifications, and read-only exports work without enabling collection.
- `EARNINGS_SEC_USER_AGENT` must identify the application and a contact email
  before optional SEC requests. It is sent as an HTTP header and is never
  persisted in the ledger. Issuer citations can always be registered manually.
- No live account, positions, order or execution endpoints are used. The
  calendar uses the configured Alpaca client's calendar endpoint; prices use
  historical consolidated SIP with split adjustment and a 16-minute delay.

Run from the repository root using `./venv/bin/python`. All JSON input schemas
are strict about unknown fields and defined in `data/earnings_observation.py`.
Timestamps require an offset, normally `+00:00` or `Z`. Knowledge timestamps
are assigned by the writer's current clock and cannot be supplied through the
CLI. Do not use the test clock injection in production.

## Operator workflow

1. Initialize the dedicated database with
   `./venv/bin/python scripts/earnings_observations.py init`.
2. Create a reporting round with `cycle CYCLE_ID --prospective-cutoff TIMESTAMP
   --note TEXT`. The cutoff describes the planned complete-cycle boundary;
   it does not certify that a cycle was registered prospectively. Each event
   has its own actual registration clock and eligibility.
3. Record an issuer schedule citation with `source --json SOURCE_FILE`.
   The command returns its `source_id`. Citation input has `issuer_id`,
   `source_type`, `title`, `url`, `published_at` (optional), `accessed_at`,
   and an operator-authored `paraphrase`. Types are `issuer_schedule`,
   `issuer_release`, `issuer_material`, `sec_filing`, `operator_note`, or
   `unavailable`. SEC citations also require `accession`. Public URLs must
   use HTTPS without credentials, query strings or fragments.
4. Register an event with `register --cycle CYCLE_ID --issuer MSFT
   --period FISCAL_PERIOD --json SCHEDULE_FILE`. The command returns its
   `event_id`. Schedule input contains `scheduled_date`,
   `scheduled_release_at` (only for exact precision), `precision`, `source_id`,
   and `author`. Preserve the issuer's fiscal-period label. Use
   `--retrospective` for deliberate seed work; the writer also automatically
   assigns retrospective mode when prospective requirements are not met.
5. After release, record the source citation, then use `facts EVENT_ID --json
   FACTS_FILE`. Facts contain `actual_release_at`,
   `actual_release_time_kind`, optional `call_scheduled_at`/`call_started_at`,
   `source_ids`, `author`, and `note`. The time kind is `issuer_exact`,
   `sec_acceptance_proxy`, `operator_bounded`, or `unknown`. Unknown requires a
   null release timestamp. Bounded operator times remain unavailable for
   standard reaction windows; they do not manufacture an exact first release.
6. Run `collect` once to persist the authoritative calendar plan. Collection
   may be invoked again as windows mature. Each invocation is bounded and
   exits; this PR does not install a recurring job. Missing release facts
   leave expected jobs pending. Planning failures are reported and remain
   recoverable by the next invocation.
7. Append a dimension label with `label EVENT_ID --json LABEL_FILE`.
   Required fields are `cluster_id`, `dimension`, `label`, `source_ids`,
   `author`, and `evidence`; `disagreement` is optional. Use all seven
   registered dimensions, including explicit `unavailable` and
   `not_applicable` labels. The writer derives label context. Without a known
   reaction plan it conservatively uses the late context, so collect a plan
   before freezing early labels.
8. Append contamination with `flag EVENT_ID --json FLAG_FILE`: `flag`,
   `windows`, `severity`, `source_ids`, `author`, `rationale`, and optional
   `shared_driver_group_id`. Flags do not delete or down-weight events.
9. Export history with `export --as-of TIMESTAMP`, or current reviewed
   revisions with `export --corrected`. JSON goes to stdout. Reads open
   SQLite with `mode=ro`, do not initialize a missing database, and never call
   a provider. No dashboard renderer is included yet.

`schedule`, `facts`, `label`, and `flag` corrections require `--reason TEXT`
when an existing logical record changes. Identical records are idempotent.
They append a revision and retain the old record and its original knowledge
time. Registration identity, mode, memberships, and job counts never change.
If later facts reveal that an initially prospective registration was actually
late, the export sets `registration_timing_conflict=true` and
`prospective_timing_valid=false`; it does not silently rewrite registration.

The same issuer/cycle/fiscal period cannot be registered again under a new
registry version. For BMO/AMC schedules without an exact time, prospective
registration conservatively requires registration before midnight New York
time on the scheduled date. Date-only and unknown schedules do not qualify.

## Corrections, retries and restart recovery

All reporter/candidate/benchmark job identities are created atomically with
registration. Candidate-schedule-only issuers create no own-report jobs.
Timing plans and attempts are separate append-only records. A revised release
fact invalidates the previous timing plan for current completeness. Collection
creates a new plan and observations under the same job identities; historical
exports retain the original plans and marks.

First revisions always have a null correction reason and no predecessor.
Later revisions retain their reason and predecessor. Recollection following a
facts revision is labeled `release facts revised`, including note-only edits;
it does not imply that the release timestamp changed. Explicit `recollect`
requests retain the operator's supplied reason.

To request revised provider marks without changing release facts, run
`recollect EVENT_ID --reason TEXT`, then `collect`. This creates a new plan
revision explicitly tied to the prior plan. Until collection completes, the
current view shows pending jobs and separately exposes prior observations.
It never presents old marks as satisfying the newly requested correction.

Transient provider errors and missing exact bars receive up to three attempts,
spaced at least five minutes apart by durable retry timestamps. There are no
retry sleeps in the earnings runner. Entitlement and non-retryable provider
errors finalize immediately. Missing marks finalize `partial` after the third
attempt; unknown/irregular release timing finalizes `unavailable`. Pending
work survives process exit. Attempt plus final observation writes are atomic.
Recovery of terminal failures is an explicit `recollect` operation.

Each pass considers at most 24 planning events and collects at most 24 jobs.
Its `remaining_due` output counts jobs still eligible at the end of the pass,
so a positive count means another pass can make progress immediately. Future
windows, retries waiting for their next attempt, and events awaiting a current
timing plan are excluded. A busy collector returns null for this count because
it did not acquire the lock. Planning failures remain visible separately.
It checks a 45-second admission budget before starting each event/job. A job
already in flight can finish beyond the budget, bounded by the existing Alpaca
transport timeouts and SDK retry policy. Per-run mark caching shares requests
between benchmark views; there is no durable mark cache that could mix
adjustment bases across different collection runs. A nonblocking file lock
prevents overlapping collectors for the same database.
The local `data/*.collector.lock` files are ignored by Git.

The one-minute target uses the bar starting one minute before open + 30:
09:59–10:00 ET for an ordinary 09:30 open. Earlier/later bars cannot substitute.
Daily marks require the exact exchange-local date. All marks have positive,
finite prices and identical reporter/benchmark endpoints. SQLite exposes the
four prices as generated `REAL` columns with positivity checks; the complete
provenance and returns remain in validated JSON. Daily and minute requests use
the official [Alpaca historical bars API](https://docs.alpaca.markets/us/reference/stockbars).

## SEC and issuer metadata

`sec-source --issuer MSFT --cik CIK --accession ACCESSION --paraphrase TEXT`
makes one request to SEC recent submissions, verifies the CIK's ticker against
the frozen registry symbol, and selects the exact requested accession.
It accepts only 8-K, 10-Q, 10-K, 6-K, and 20-F. It does not infer which filing
is an earnings release or automatically turn acceptance time into release
time. The operator cites that timestamp as `sec_acceptance_proxy` in a
separate facts command when appropriate.

The request has connection/read timeouts, a 5 MB response limit, no redirects,
and a minimum one-second interval per provider instance. Run SEC commands
serially; this command does not coordinate unrelated SEC clients on the host.
No archive pagination, full filing corpus, or companyfacts parsing is included.
An accession outside recent submissions can still be entered as a manually
reviewed public citation. Issuer pages are manually cited, since there is no
uniform authoritative free earnings-calendar API. See the
[SEC API documentation](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).

## Evidence and validation

Tests cover exact registration/close boundaries, aliases and ADR identity,
frozen versions/denominators, weekend/DST/early-close handling, cited revisions,
atomic writes, bounded locks, errors, restarts, timing corrections, explicit
recollection, read-only exports and absence of trading consumers. Synthetic
input records in the CLI test produce a sanitized fixture for dashboard work.

A read-only Alpaca adapter check returned the exact SPY SIP minute mark for
2026-11-05 open + 30 and the 2026-11-04 daily baseline; the calendar resolved
the third reaction session to 2026-11-09. No production ledger was seeded and
no service was restarted. Live SEC retrieval awaits an operator-configured
identifying user agent. A real preregistered event and a complete prospective
reporting cycle remain operational evidence to collect after review and setup.

Candidate statuses are `reported`, `scheduled_later`, `unknown`, or `reporter`.
They use schedule/fact knowledge available at the source-event cutoff; a
schedule alone never proves that release occurred. `scheduled_later` describes
the known schedule, not guaranteed non-release. `no_recorded_flags` means no
contamination flag was entered, not verified absence of all external drivers.
