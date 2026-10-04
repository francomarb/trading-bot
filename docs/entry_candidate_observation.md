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
the entry order, the broker's actual TIF, the engine's maximum stale-entry age,
the stop anchor and ATR multiplier, the exit rule, and modeled market-exit
slippage. A configuration hash
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
money. There is deliberately no generic resolver: even equity strategies have
different entry, protection, and exit semantics, and applying one price horizon
would create misleading evidence. RSI remains the only supported resolver
because it was the first observed contention case. SMA and Donchian contention
groups now exist and require their own reviewed replay contracts before their
refused candidates can be resolved.

The RSI resolver is an explicit offline command:

```bash
# Preview only; does not update the database.
./venv/bin/python scripts/resolve_candidate_shadows.py

# Persist the previewed states to the disposable shadow table.
./venv/bin/python scripts/resolve_candidate_shadows.py --apply
```

It tests the observation session against complete one-minute bars from the
candidate's recorded feed after the observation time. RSI equity limits are
GTC at the broker, but the engine normally cancels an unfilled LIMIT after the
frozen `STALE_LIMIT_MAX_AGE_SECONDS` threshold. Replay therefore uses the
earlier of that local policy and Alpaca's 90-day ceiling. Because cleanup runs
only in a market-hours cycle, the first trading session ending after the cutoff
is the cancellation-boundary session, including after a weekend or holiday. A
daily bar that touches the limit during that session is marked `needs_review`
unless its open proves a pre-cleanup fill; an in-progress boundary session stays
`awaiting_fill`, because daily resolution cannot invent the ordering. A legacy candidate's exact TIF is recovered from
the selected peer's durable entry-order row, while its historical configured
age default is parsed from its immutable commit. The replay contract explicitly
records that ordinary RSI GTC
OTO stops currently remain anchored to the entry reference; this matches the
selected trades rather than assuming the fill-anchoring used by other equity
entry variants. After a fill, the resolver applies that recorded ATR stop and
the production RSI exit rule on completed daily bars; signal exits use the next
session open and the recorded market-slippage model. Same-bar entry/stop
ordering is marked `needs_review`, not guessed. A live GTC order remains
`awaiting_fill`; a filled candidate with no exit stays `open`. Both can be
refreshed later. The indicator warm-up is derived from the frozen RSI/SMA
windows rather than a fixed date span. Old schema-v1 candidates recover their
missing configuration and stale-age default by parsing settings from their
immutable stored bot commit; historical Python is never executed and current
settings are never substituted.

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

### Independent strategy evidence pools

RSI, SMA, and Donchian must be evaluated separately. Their signals describe
different events, so a characteristic that identifies a strong RSI pullback
cannot be assumed to identify a strong crossover or breakout. Each pool should
compare only facts available when the decision was made with later percentage
and R outcomes. Outcome quality is broader than the final winner label: include
whether the order would fill, terminal R, favorable/adverse excursion, time to
resolution, and stop-versus-signal exit when the strategy resolver can establish
them truthfully.

| Pool | Candidate characteristics to evaluate |
|---|---|
| RSI | Oversold depth, one- and three-bar decline, distance to the exit SMA, ATR%, liquidity, and same-cycle sector overlap |
| SMA | Crossover gap, fast/slow slopes, price extension, recent return, ATR%, volume state, and sector context |
| Donchian | Trigger excess, channel width, volume ratio, ATR%, SMA200 extension, earnings state, and sector heat |

These are starting fields, not an approved model or an exhaustive list. Review
may reveal a useful characteristic that is not currently recorded. When that
happens, first define why it is available at decision time, add it prospectively
under a new feature-schema version, and collect later groups. Do not invent a
historical value or silently reconstruct it with information the engine did not
have at the time.

Every strategy pool must also be segmented by the regime recorded on the
candidate decision. A characteristic associated with stronger outcomes in an
allowed RANGING entry cannot be assumed to behave the same way in TRENDING or
VOLATILE conditions. Reports should show contention-group count, resolved
candidate count, and outcomes by regime before pooling them. Sparse regime
evidence remains explicitly inconclusive; it is not combined merely to reach a
larger sample.

The first resolved RSI contention group is an indication, not a rule. Its two
capacity-refused candidates finished at about +0.11R and -0.18R, while the two
selected candidates finished at about -0.50R and -1.04R. The best outcome had
a shallow oversold reading, low relative volatility, high liquidity, and a
controlled pullback; the worst selected candidates showed either extreme
oversold depth or a sharper, more volatile drop. Those observations create
RSI-specific hypotheses to test against later contention groups. They do not
authorize a filter or ranking formula, and they say nothing about SMA or
Donchian candidate quality.

For every pool, first accumulate repeated resolved groups, then describe which
ex-ante characteristics consistently separate better and worse outcomes. Any
resulting rule must be pre-registered and tested on later groups rather than
fit and judged on the same observations.

The final `11.61` deliverable is an evidence-backed profile of what the
consistently better-ranked candidates look like for each strategy in each
regime that strategy is allowed to trade, including the supporting sample size
and remaining uncertainty. That profile is the basis for an actionable ranking
proposal; observation alone does not change production ordering.
