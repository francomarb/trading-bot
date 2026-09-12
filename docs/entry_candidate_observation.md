# Entry-candidate observation (`11.61`)

## Status

Phase `11.61a` records which actionable candidates reached allocation, what
distinguished them, which one the existing sequential engine selected, and why
another was refused. The first strategy-specific shadow resolver now supports
RSI equity candidates offline. Nothing ranks, reorders, resizes, or submits an
additional order.

## The decision boundary

A recorded candidate has already passed its strategy signal, edge filter,
regime gate, pre-instrument ownership checks, operator pauses, and pending-order
checks. Contract-specific conflict checks still occur after an option picker
resolves the instrument and are retained as the candidate's final disposition.
Recording every no-signal symbol would create noise and would not help answer
the allocation question.

Candidates compete only with other candidates from the same strategy and
signal bar. SMA, RSI, Donchian, leveraged trend, SPY options, and credit spread
express different trades; a numerical signal from one is not comparable with a
signal from another. Cross-strategy admission remains an allocator and
portfolio-risk policy.

## Permanent evidence

`entry_candidate_decisions` lives in the normal trade database. Each row has:

- candidate, engine-cycle, signal-time, strategy-version, configuration, and
  bot-commit identity;
- symbol, signal asset, timeframe, feed, regime, slot/watchlist/evaluation
  order;
- strategy-specific signal and filter facts;
- the pre-decision sleeve, pool, portfolio, sector, volume, and order context;
- option/spread picker economics when the real path reaches a picker;
- approved quantity, notional, risk, binding cap, launch multiplier, and heat
  facts when sizing succeeds; and
- final disposition, exact refusal reason, broker order, and lifecycle link.

Feature payloads are versioned JSON so one strategy can evolve without forcing
unrelated strategies into a false universal schema. Non-finite values become
`NULL` in JSON rather than invalid `NaN` tokens.

Current strategy-owned feature groups are:

| Strategy | Candidate facts |
|---|---|
| SMA Crossover | crossover gap, fast/slow slopes, one-bar return, SMA200 and volume gate state, sector state |
| RSI Reversion | current/previous RSI, oversold depth, recent returns, exit-SMA distance, SMA200 and liquidity state |
| Donchian Breakout | trigger excess, channel width, volume ratio, SMA200/liquidity/earnings and sector state |
| Leveraged Trend | unleveraged signal/SMA distance and slope, confirmation streak, stated and stress leverage |
| SPY Options Reversion | RSI recovery shape, SPY/VIX gate state, selected contract premium/spread and rank components |
| Credit Spread | underlying trend/IV state, configured delta/DTE/credit constraints, selected spread economics and rank components |

RSI feature schema v2 also stores the RSI period. Its candidate context freezes
the entry order, the broker's actual TIF, the stop anchor and ATR multiplier,
the exit rule, and modeled market-exit slippage. A configuration hash
distinguishes epochs but cannot be reversed into these values, so future replay
never borrows whatever configuration happens to be active when the resolver is
run.

The engine captures only values already computed by the real path. Observation
must not add quote calls, chain requests, or timing changes. Consequently, a
candidate rejected by the sleeve before sizing or option selection explicitly
records `execution_envelope_available=false`; it does not fabricate economics.
Credit-spread ranking cannot be enabled until both SPY and QQQ envelopes can be
gathered safely before admission. SPY options currently has one underlying, so
there is no same-strategy cross-symbol contest to rank.

## Disposable calibration evidence

`entry_candidate_shadow_outcomes` is deliberately separate from the permanent
decision record. It is populated only when a same-strategy group contains both
an actually selected candidate and a capacity refusal. The selected row points
to its real lifecycle. The refused row is marked
`counterfactual_required`; a strategy-aware replay can attach fill, exit,
return/R, and favorable/adverse excursion through
`CandidateObservationStore.record_shadow_outcome`. Dollar P&L remains NULL for
a refused candidate because allocation never approved a quantity.

The table is a work queue, not a claim that an untraded position earned or lost
money. There is deliberately no generic resolver: equity, single-leg option,
and MLEG fills/exits have different semantics, and applying one price horizon
would create misleading evidence. RSI is the only supported resolver because
it is the only strategy with a real contention group so far.

The RSI resolver is an explicit offline command:

```bash
# Preview only; does not update the database.
./venv/bin/python scripts/resolve_candidate_shadows.py

# Persist the previewed states to the disposable shadow table.
./venv/bin/python scripts/resolve_candidate_shadows.py --apply
```

It tests the observation session against complete one-minute bars from the
candidate's recorded feed after the observation time. RSI equity limits are
GTC in the running bot, so an untouched order remains eligible on later
completed daily sessions until Alpaca's 90-day GTC expiry. A legacy candidate's
exact TIF is recovered from the selected peer's durable entry-order row; it is
never guessed. The replay contract explicitly records that ordinary RSI GTC
OTO stops currently remain anchored to the entry reference; this matches the
selected trades rather than assuming the fill-anchoring used by other equity
entry variants. After a fill, the resolver applies that recorded ATR stop and
the production RSI exit rule on completed daily bars; signal exits use the next
session open and the recorded market-slippage model. Same-bar entry/stop
ordering is marked `needs_review`, not guessed. A live GTC order remains
`awaiting_fill`; a filled candidate with no exit stays `open`. Both can be
refreshed later. The indicator warm-up is derived from the frozen RSI/SMA
windows rather than a fixed date span. Old schema-v1 candidates recover their
missing configuration by parsing literal settings from their immutable stored
bot commit; historical Python is never executed and current settings are never
substituted.

The command only updates `entry_candidate_shadow_outcomes`. It never changes a
decision, lifecycle, allocator state, or bot behavior. Once ranking is accepted,
this temporary table can be dropped without affecting trading or the permanent
audit trail.

## When ranking may begin

`11.61b` remains blocked until the evidence shows repeated real contention and
resolved counterfactual outcomes. A proposed strategy-specific rule
must be pre-registered, tested out of sample, and remain explainable from the
permanent fields. Signal characteristics may be evaluated, but they must not be
assumed predictive merely because they sound stronger. Existing order behavior
continues unchanged until a separately reviewed ranking PR is approved.
