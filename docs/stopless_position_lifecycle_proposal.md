# Stopless Position Lifecycle and Notional Risk Proposal

**Status:** proposal for review; no runtime activation is authorized by this
document.

**Primary consumer:** leveraged-index trend strategies such as SPY→SPXL and
QQQ→TQQQ.

**Architectural scope:** bot-wide. This is a position/risk substrate capability,
not a leveraged-trend special case.

**Existing defect exposed:** review also found a reachable recovery failure on
the active single-leg options path. Normal dispatch pre-registers ownership and
therefore avoids reconstructing a `RiskDecision`, but recovery after a crash/
restart can encounter the fill before ownership is bound. Today the simple
LIMIT entry row omits the strategy's intended hard-stop price, recovery converts
that NULL to `0.0`, `RiskDecision` rejects it, and the outer handler emits
CRITICAL without binding ownership. The options sleeve must not be classified
as `SIGNAL_EXIT_ONLY`: it requires a broker GTC stop installed after fill and
later ratcheted. The durable contract must preserve that `BROKER_STOP` intent
through a simple entry and route recovery to the option-specific stop path.
This defect justifies the substrate work independently of whether leveraged
trend is ever activated.

## 1. Decision Summary

Extend the existing `RiskManager`, broker, and durable position lifecycle with
two independent typed policies:

| Dimension | Values | Meaning |
|---|---|---|
| Sizing model | `STOP_DISTANCE`, `NOTIONAL`, `DEFINED_MAX_LOSS` | How entry quantity and admission risk are calculated |
| Protection model | `BROKER_STOP`, `SIGNAL_EXIT_ONLY` | Whether absence of a broker-side protective stop is a fault |

The leveraged-index trend baseline uses `NOTIONAL + SIGNAL_EXIT_ONLY`.

This proposal does **not** add another risk manager. `RiskManager.evaluate()`
remains the only entry-admission authority and continues enforcing account and
sleeve cash, gross exposure, position limits, risk halts, and the allocator.
Only the sizing branch and protection requirement become explicit.

The protection model is persisted with the position lifecycle and its entry
intent before broker submission. Recovery must read the persisted value. It
must never infer protection from a strategy name, a ticker, a nullable stop
price, or the strategy's current configuration after restart.

## 2. Required Invariants

1. Every accepted entry has exactly one sizing model and one protection model.
2. `BROKER_STOP` requires a valid stop price. A missing live stop remains a
   repairable fault under today's strict behavior.
3. `SIGNAL_EXIT_ONLY` requires no stop price. Stop absence is healthy: it must
   not trigger repair, reconstruction, an alert, degraded health, or fractional
   cleanup.
4. Stopless does not mean lifecycle-less. Entry fills, ownership, current
   quantity, partial fills, P&L, external closes, and strategy exits continue
   through the normal substrate.
5. Policy is immutable for an open position. A deployment or configuration
   change cannot reinterpret an existing position after restart.
6. The broker executes the decision produced by `RiskManager`; strategies do
   not bypass either layer.
7. Existing stopped strategies retain their current behavior by default.
8. Unknown or missing policy on a legacy open equity position fails to the
   existing `BROKER_STOP` posture unless a reviewed migration explicitly proves
   otherwise. Never silently classify a legacy position as stopless.

## 3. Why Persistence Is Load-Bearing

The current engine can reconstruct ownership after the in-memory strategy
object and original `RiskDecision` are gone. Therefore a current configuration
lookup is not authoritative:

- strategy parameters may have changed between entry and restart;
- a strategy may eventually support more than one protection mode;
- recovered asynchronous fills are dispatched from per-order substrate rows;
- the position lifecycle, not slot configuration, is the durable identity
  authority.

Persist the policy on `position_lifecycle`, where it describes the position as
a whole. Also capture it on the immutable `entry_primary` order intent as
first-class validated columns. The order copy closes the pre-fill recovery
window; the position copy supports every post-fill consumer without reverse-
engineering an entry order.

Recommended first-class fields:

```text
position_lifecycle.sizing_model       NOT NULL
position_lifecycle.protection_model   NOT NULL

position_lifecycle_orders.sizing_model       NOT NULL for entry roles
position_lifecycle_orders.protection_model   NOT NULL for entry roles
```

`metadata_json` exists, but these values control order placement and repair.
First-class validated columns make invalid combinations queryable and prevent
silent metadata-key drift. Migration may need a staged nullable-add/backfill/
validate sequence before enforcing `NOT NULL`; the final schema must make an
absent or invalid policy impossible for new entry intents. Position and entry-
order copies must be created atomically and protected against disagreement.

## 4. Typed Risk Contract

Introduce enums in the risk domain rather than strings distributed across
strategies and engine code:

```python
class SizingModel(str, Enum):
    STOP_DISTANCE = "stop_distance"
    NOTIONAL = "notional"
    DEFINED_MAX_LOSS = "defined_max_loss"


class ProtectionModel(str, Enum):
    BROKER_STOP = "broker_stop"
    SIGNAL_EXIT_ONLY = "signal_exit_only"
```

`Signal` (or a strategy-owned immutable risk profile referenced by it) declares
the desired models. `RiskDecision` records the admitted models and makes
`stop_price` optional under a strict validation matrix:

| Sizing | Protection | Stop price | V1 status |
|---|---|---|---|
| `STOP_DISTANCE` | `BROKER_STOP` | required and side-valid | existing default |
| `NOTIONAL` | `SIGNAL_EXIT_ONLY` | must be `None` | leveraged trend |
| `DEFINED_MAX_LOSS` | strategy-specific | normally `None` | preserves spread direction; not expanded here |
| `STOP_DISTANCE` | `SIGNAL_EXIT_ONLY` | invalid | reject: risk cannot be sized to a nonexistent stop |
| `NOTIONAL` | `BROKER_STOP` | deferred | reject in V1 unless separately designed |

For `NOTIONAL`, quantity is based on an approved slice of sleeve/account equity
and still capped by all existing universal gates. The persisted admission basis
must include the approved notional dollars and effective exposure assumptions;
it must not overload `initial_risk_dollars`, which means loss to a defined stop.

Suggested decision/accounting fields:

- `approved_notional_dollars`
- `effective_exposure_dollars` or a documented leverage multiplier basis
- existing `risk_budget_dollars` only where it retains its current meaning
- nullable stop-risk fields for `SIGNAL_EXIT_ONLY`

## 5. Execution Contract

`AlpacaBroker.place_order()` remains the sole entry router.

For `BROKER_STOP`, preserve today's whole-share OTO and fractional-entry plus
standalone-stop behavior. Single-leg options are also `BROKER_STOP`, but their
simple LIMIT entry cannot attach the stop atomically: persist the intended hard-
stop price and install/reconcile it after fill through the existing option GTC-
stop path. Recovery must never route an OCC contract through the equity
`place_protective_stop()` helper.

For `SIGNAL_EXIT_ONLY` equities:

- submit a simple DAY entry order;
- do not create an OTO child;
- do not submit a standalone GTC stop after fill;
- persist `order_class='simple'` and `intended_stop_price=NULL` truthfully;
- return an execution result that distinguishes "protection not required" from
  "stop placement attempted and failed."

That final distinction should be typed. Reusing `placed_stop_price=None` alone
is ambiguous because it currently covers both intentional absence and a failed
or impossible stop placement.

## 6. Repair and Reconciliation Audit

The following code paths were inspected on 2026-08-26. Every item must consume
the durable protection model before stop-specific behavior.

### 6.1 Recurring stop repair — critical

`TradingEngine._repair_missing_protective_stops()` runs at startup, during
market-closed cycles, and during normal cycles. It currently assumes every
managed non-OCC equity requires a stop. If none exists, it reads a historical
stop, reconstructs an ATR stop from current data, alerts, submits a new stop,
or closes a fractional residual.

Required behavior:

- resolve the open lifecycle by `owner_key`;
- `SIGNAL_EXIT_ONLY`: return for that position before inspecting broker stop
  presence, reading a stop price, reconstructing context, or computing whole
  stop quantity;
- `BROKER_STOP`: preserve all existing repair behavior;
- missing/invalid persisted policy: fail safe as `BROKER_STOP` for legacy
  stopped positions and emit an explicit migration diagnostic.

Do not implement this as `if owner == 'leveraged_trend'`. Use a single helper
whose input is lifecycle-authoritative position intent.

### 6.2 Fractional residual cleanup — critical

`_close_fractional_residual_position()` liquidates a position because it cannot
carry a whole-share protective stop. It is valid only when the position's
protection model requires such a stop.

The helper should require proof of `BROKER_STOP`, preferably in its typed input
or precondition. A `SIGNAL_EXIT_ONLY` fractional position is a legitimate open
position and must remain available to normal strategy exits.

### 6.3 Recovered entry side effects — critical

`_apply_recovered_entry_side_effects()` unconditionally invokes
`_ensure_recovered_protective_stop()`. Both the synchronous-uncertain recovery
path and substrate-driven asynchronous fill path reach this helper.

Required behavior:

- always bind ownership, entry price, lifecycle quantity, and alerts;
- invoke stop assurance only for `BROKER_STOP`;
- treat `SIGNAL_EXIT_ONLY` stop absence as a successful recovered state;
- reconstruct `RiskDecision` with nullable stop and persisted policies.

Today substrate recovery converts `intended_stop_price=NULL` to `0.0`, and
`RiskDecision` rejects it. This is already reachable on the active options
sleeve when a LIMIT entry fills across a crash/restart boundary before normal
dispatch has pre-registered ownership. The current ownership gate prevents
false CRITICALs for the common already-bound case, but does not repair the
unbound recovery case.

The two cases must not be conflated:

- future `SIGNAL_EXIT_ONLY` equity: NULL is valid and recovery must not create a
  stop;
- current single-leg option: protection is `BROKER_STOP`; persist the intended
  hard-stop price even though `order_class='simple'`, then recover through the
  option-specific post-fill GTC-stop path.

Replacing the conversion with `_finite_or_none()` alone fixes neither contract:
`RiskDecision` still rejects NULL, and the shared recovered side effects would
route a missing OCC stop through the equity repair helper. Validation,
persisted intent, and asset-appropriate side effects must agree.

### 6.4 Missing entry-context reconstruction — critical

`_reconstruct_missing_entry_context()` presently manufactures an ATR stop from
current bars and writes recovered entry context so repair can continue. It must
never manufacture protection for `SIGNAL_EXIT_ONLY`.

If accounting or ownership context needs reconstruction, split that concern
from stop-price reconstruction. Stop reconstruction remains a `BROKER_STOP`
operation only.

### 6.5 Operator partial closes and reductions

Partial `close-position` and `reduce-position` results currently declare every
residual equity position degraded and `pending_repair_cycle`. For
`SIGNAL_EXIT_ONLY`, the residual is healthy and no repair is pending.

Required behavior:

- stopped position: preserve cancel/reduce/repair status;
- signal-exit position: report `protection_status='not_required'` (or omit the
  stop-specific status under a documented API contract), with no degraded flag;
- always retain the original position protection model on the residual
  lifecycle.

This proposal does not implement strategy-driven partial exits; that remains a
separate bot-wide feature. It only ensures existing operator reductions do not
misclassify a stopless residual.

### 6.6 Stop-leg stream synchronization

`_sync_managed_stop_legs()` registers stop orders that actually exist and does
not repair their absence. No functional change is required for a healthy
stopless position. Tests should nevertheless prove it does not synthesize a
required stop set from managed positions.

### 6.7 Vanished-position and external-close reconciliation

Stopless positions still require close reconciliation. The engine may search
broker history for real filled SELL orders and must continue updating P&L and
closing the lifecycle.

For `SIGNAL_EXIT_ONLY`, do not begin with a protective-stop-specific lookup.
Use generic broker SELL history and classify the recovered exit from the
persisted order role when available. A genuinely existing stop fill remains
recoverable as an anomalous/stale-order event, but stop history must not be the
assumed normal close mechanism.

V1 does not require reordering the established `BROKER_STOP` recovery path.
Making generic SELL history the common first step for every protection model is
a separate cleanup decision, gated on complete substrate order-role attribution
and regression evidence that current stop-fill classification is preserved.

### 6.8 Health, dashboard, and alerts

Missing-stop health checks must be policy-aware:

- `BROKER_STOP + no live stop` is a gap;
- `SIGNAL_EXIT_ONLY + no live stop` is not applicable, not zero and not healthy
  stop performance;
- an unexpected live stop on a `SIGNAL_EXIT_ONLY` position is a reconciliation
  anomaly.

Do not silently cancel an unexpected stop during initial rollout. Alert with
position identity, block new entries for the owning strategy only, and require
an operator decision. Do not turn a position-local reconciliation anomaly into
a global account halt, and do not auto-cancel in V1.

## 7. Exposure and Heat Accounting

The current correlated-entry heat ledger is denominated in initial dollars to a
protective stop. An open position with no `initial_risk_dollars` is placed in a
gap set. When that position's owning strategy has a configured heat cap and cap
enforcement is enabled, the gap may block further entries in that strategy's
sleeve. It cannot block another strategy's entries through this check. That
behavior remains correct for a position claiming `STOP_DISTANCE`; it is
incorrect as a data-quality diagnosis for intentional `NOTIONAL` sizing.

Do not fabricate an R value for stopless positions. Add a parallel measurement
inside the same risk-assessment system:

- stop-risk heat for `STOP_DISTANCE` positions;
- approved notional plus both stated and stress-adjusted effective exposure for
  `NOTIONAL` positions;
- defined maximum loss for `DEFINED_MAX_LOSS` positions.

Portfolio admission can then enforce the applicable metric plus universal gross
exposure and concentration ceilings. Consumers must distinguish "not applicable
under this sizing model" from "required admission data is missing."

Keep the two leverage views distinct. The fund's stated multiplier documents
its daily target; a separately configured stress multiplier expresses the
operator's conservative gap/path-risk assumption. Do not derive a supposedly
precise stress value from the stated 3x label, and do not ship a guessed stress
multiplier without calibration and an explicit operator decision.

## 8. Migration and Compatibility

Recommended rollout:

1. Add typed enums and schema fields with existing behavior as defaults.
2. Backfill historical lifecycle and entry-order rows to `STOP_DISTANCE +
   BROKER_STOP` only where existing entry intent/stop evidence supports it.
3. Refuse paper activation if any currently open managed position has unresolved
   policy. Do not guess from ticker.
4. Wire read-only policy resolution through repair, recovery, operator, health,
   and heat paths while all current strategies remain `BROKER_STOP`.
5. Add the `NOTIONAL + SIGNAL_EXIT_ONLY` entry path.
6. Treat architectural completion as necessary but insufficient for leveraged-
   trend activation. Paper activation requires its separate strategy-evidence
   gates in PLAN 11.64 as well as the complete seam suite.

Schema migration must be idempotent and compatible with databases created before
these columns existed. Open-position ambiguity is a preflight failure; closed
historical rows may use a documented legacy default where they cannot affect
broker behavior.

## 9. Acceptance Test Matrix

### Risk and validation

- Existing signals default to `STOP_DISTANCE + BROKER_STOP`.
- `BROKER_STOP` rejects null, non-positive, or wrong-side stops.
- `NOTIONAL + SIGNAL_EXIT_ONLY` accepts `stop_price=None` and sizes within the
  sleeve, account cash, global position, gross-exposure, and risk-halt gates.
- Invalid model combinations are rejected before broker dispatch.

### Broker entry

- Whole and fractional stopless entries submit simple orders with no stop child.
- Substrate rows persist both policies and a null intended stop.
- Result/reporting distinguishes not-required protection from failed placement.
- Existing stopped MARKET, LIMIT, STOP_LIMIT, and fractional paths are unchanged.

### Startup and cycle repair

- Stopless position: no stop-log lookup, ATR reconstruction, broker stop call,
  repair alert, error counter, or fractional cleanup at startup.
- Repeat the same proof for normal and market-closed cycles.
- Stopped positions retain every existing repair test and behavior.
- Unexpected stop on a stopless position produces the reviewed anomaly behavior.

### Recovery and restart

- Asynchronous stopless fill binds ownership with a null stop and no CRITICAL.
- Asynchronous/restart-recovered option fill retains `BROKER_STOP`, rebuilds a
  decision from its persisted hard-stop intent, and uses the option GTC-stop
  path rather than equity stop repair.
- Restart restores policies from lifecycle/order substrate, not current strategy
  configuration.
- Partial-fill then final-fill recovery remains idempotent.
- Missing policy on an open lifecycle fails preflight rather than silently
  becoming stopless.

### Close and reduction

- Signal exit closes normally and records P&L/lifecycle state.
- External/manual close reconstructs from generic SELL history.
- Operator partial reduction preserves `SIGNAL_EXIT_ONLY` and is not degraded.
- Fractional stopless residual remains open and later exits normally.

### Risk consumers and observability

- Stop-risk heat does not classify intentional notional exposure as missing data.
- Notional/effective exposure survives partial reductions and restart.
- Missing-stop health is N/A for stopless positions.
- Dashboard and alerts clearly distinguish required, not required, missing, and
  unexpected protection.

### Seam test

One end-to-end offline test must exercise:

```text
strategy intent
  → RiskManager approval
  → lifecycle/order persistence
  → simple broker entry
  → asynchronous fill recovery
  → restart
  → stop-repair cycle
  → signal exit
  → P&L and lifecycle closure
```

The test passes only if zero protective-stop submissions occur throughout and
the stopped-strategy regression suite remains green.

## 10. Out of Scope

- Tax and wash-sale optimization.
- Catastrophe stops or intraday circuit breakers for leveraged trend.
- Strategy-driven partial profit-taking.
- Replacing `RiskManager` or `SleeveAllocator`.
- Paper/live activation of leveraged trend.
- Reclassifying current production positions as stopless.

## 11. Review Resolutions and Remaining Question

Review resolved four design forks:

1. Use first-class validated lifecycle and entry-order columns, with atomic
   creation and migration-safe constraint enforcement.
2. An unexpected owned stop on `SIGNAL_EXIT_ONLY` alerts and blocks new entries
   for the owning strategy only. It does not globally halt the account and is
   not auto-cancelled in V1.
3. Persist stated leverage and a distinct configurable stress-adjusted exposure
   basis. Calibration and the selected stress multiplier remain operator risk-
   policy decisions before activation.
4. Generic SELL-history-first recovery is required for `SIGNAL_EXIT_ONLY`.
   Reordering `BROKER_STOP` recovery is deferred unless substrate attribution
   and regression evidence justify a common path.

One implementation question remains: whether the strategy-local entry block
for unexpected protection should reuse the existing durable pause-strategy
control state or be a distinct automatically managed reconciliation halt. The
choice must preserve operator visibility, restart durability, and an explicit
clear condition; it must not become an in-memory-only flag.
