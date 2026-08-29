# Deferred trade follow-ups — incident forensics

> **What this file is.** A forensics and debugging record: what was observed
> on a given day, what was measured, what was ruled out, and why. It exists so
> a future reader can reconstruct an incident without re-deriving it from raw
> logs and trade rows.
>
> **What this file is NOT.** It is not a work queue, and it does not track
> whether anything is done. **`PLAN.md` is the single source of truth for
> status** — what is shipped, what is pending, what is blocked, and what the
> acceptance is. Nothing here should be read as "current state of the work".
>
> This split exists because it already failed the other way: on 2026-08-14 an
> audit found item 2 below still reading "fix branch in review" while the
> cancel-child + standalone-GTC path had long since shipped
> (`execution/broker.py:1839`, `:2045`). Two documents carrying the same status
> will diverge; only one of them can be right, and it is PLAN.
>
> **Rules for editing.** Add narrative, evidence, measurements and ruled-out
> hypotheses freely. Do not add status lines, branch states, or "still
> required" checklists — those belong in the PLAN row this section links to.
> Statements of historical fact ("merged in PR #63") are fine; statements of
> present state ("in review") are not.

| section | tracked in PLAN as |
|---|---|
| 1. Option trailing-stop replacement and fill timing | Live Readiness Gate — *SPY option trailing durability* |
| 2. ALAB / ANET attached DAY stop durability | Live Readiness Gate — *Capped equity entry stop durability* |
| 3. SPY options reversion blocked before enough paper trades | resolved 2026-08-14 by PR #104 (paper drawdown observability) |

**Context:** Two operational observations were found while reviewing the
June 12, 2026 paper-trading activity. Neither observation invalidates the
day's realized SPY option profit, but both deserve a focused review after the
active workstreams settle.

## 1. Option trailing-stop replacement and fill timing

*Status: see the PLAN gate "SPY option trailing durability".*

Quote-quality hardening merged in PR #63. The replacement path now requires a fresh,
two-sided Alpaca option quote before increasing a stop price. The executable
bid must support the proposed stop with a safety buffer and the spread must
remain within a conservative quality ceiling. A failed quality check leaves
the existing broker stop active and retries next cycle; missing-stop creation
and required GTC/quantity maintenance remain fail-safe and are not blocked.

The exact June incident mechanism could not be reconstructed from historical
quotes. A later temporary diagnostic captured broker and WebSocket evidence for
`spy_options_reversion`; the observed position completed 14 replacements without
reproducing the immediate fill. The diagnostic is disabled by default and writes
to a disposable standalone DB. See `docs/option_stop_replace_diagnostics.md` and
PLAN for the operator's final disposition.

The `spy_options_reversion` position in `SPY260702C00724000` closed profitably:

- Entry: 2 contracts at $15.40
- Exit: 2 contracts at $23.00
- Realized P&L: +$1,520 (+49.35%, +1.97R)
- Durable HWM reached $23.00
- Computed 15% trailing floor reached $19.55

The logs show the engine replacing the trailing stop for the new $23.00 HWM
and receiving a fill update at $23.00 almost simultaneously. The trade logger
therefore records a stop trigger benchmark of $19.55 and an unusually favorable
$23.00 fill.

Investigation results:

1. Broker history confirmed the replacement order itself filled, not the old
   stop.
2. The persisted order identity followed Alpaca's replacement id correctly.
3. Alpaca paper fills against current quotes, but the ratchet decision used the
   Positions API current price without validating the executable option book.
4. Exact historical quote reconstruction was unavailable, so a transient quote
   and a paper-simulation quirk cannot be distinguished conclusively.

PR #63 does not classify either profitable fill as defective. It improves the
evidence required for future stop-price increases without changing the existing
broker-side protection or converting the strategy to software-only exits.

## 2. ALAB / ANET attached DAY stop durability

The `donchian_breakout` strategy opened capped DAY + OTO equity entries whose
attached stop children were DAY orders. ALAB first exposed the issue; ANET
reproduced it on 2026-07-09 after a capped STOP_LIMIT + OTO entry.

The attached stop became active, but attempts to promote it from DAY to GTC
were repeatedly rejected by Alpaca with:

`time_in_force cannot be changed for advanced orders`

Resolution note, 2026-07-09:

- Checked Alpaca's current Orders docs and local `alpaca-py` SDK request model.
- Alpaca supports `day` / `gtc` for advanced orders at submission time, but OTO
  child replacement is not a supported path for changing TIF. The SDK can build
  a `ReplaceOrderRequest(time_in_force=...)`, but Alpaca rejects that mutation
  for advanced-order children.
- The correct local behavior is: record the attached OTO child with its actual
  DAY TIF, then rebuild durable protection as cancel-child + standalone simple
  GTC stop after confirmed fill / broker-snapshot reconciliation.
- The PR for this fix must not treat this as a generic `PATCH` promotion. It
  should keep the existing DAY child if cancel fails, and it should alert if the
  cancel/rebuild path cannot establish standalone GTC protection.

*Status: see the PLAN gate "Capped equity entry stop durability". As of
2026-08-14 the cancel-child + standalone-GTC path is shipped
(`execution/broker.py:1839`, `:2045`); the outstanding paper confirmation is
tracked in that PLAN row, not here.*

What a paper confirmation of this fix looks like: a DAY child cancel event, one
standalone GTC protective stop, no duplicate SELL exposure, and a substrate row
whose TIF matches broker truth.

## 3. SPY options reversion blocked before enough paper trades

On June 22, 2026, `spy_options_reversion` produced valid entry signals but was
blocked by the allocator sleeve-drawdown gate:

- Closed round trips on record: 5 of the configured 15-trade floor
- Cumulative realized P&L: +$160
- Prior realized-P&L high-water mark: +$2,252
- Drawdown from HWM: $2,092
- Active pre-fix threshold: "catastrophic" below-floor tier, about $1.6k that day

The DB rows feeding the allocator looked internally consistent; this was not an
accounting-hole finding. The concern is policy: with only five closed trades,
the bot is making a hard entry-blocking decision from a very small sample. The
pre-fix implementation applied a generous catastrophic threshold below the
min-trades floor instead of fully failing open. That protected against a true
early sleeve disaster, but for sparse paper-watch strategies it also created
the same chicken-and-egg lockout the min-trades floor was meant to avoid.

Policy update, shipped 2026-08-14 in PR #104 (verified in code:
`settings.PAPER_STRATEGY_DRAWDOWN_GATE_ENABLED`,
`SleeveAllocator.drawdown_snapshot`'s `observed_in_drawdown`). Paper development defaults to observation-only at
every sample size, so the sleeve drawdown remains visible but never blocks new
entries while strategies are being tuned. A mature paper strategy can opt into
the normal post-floor gate with `PAPER_STRATEGY_DRAWDOWN_GATE_ENABLED=true`.
Live mode retains the catastrophic below-floor backstop and normal post-floor
gate. The bot still keeps the real hard-risk layers active in both modes:
daily/account loss controls, hard sizing, broker-side stops, max positions,
sleeve-budget checks, entry quality guards, and exits.

Acceptance that was applied when this shipped (recorded for reconstruction,
not as outstanding work):

1. In default paper mode, `SleeveAllocator.is_strategy_in_drawdown(...)`
   returns false at every trade count while retaining full observability.
2. `drawdown_snapshot(...)` still reports running P&L, HWM, drawdown dollars,
   trade count, floor, and both states: `in_drawdown` for entry blocking and
   `observed_in_drawdown` against the normal threshold for paper-watch evidence.
   Observed breaches are surfaced in the dashboard and `scripts/operator.py
   status`, not only in the raw engine snapshot.
3. Explicitly enabling the paper gate resumes the normal HWM threshold at the
   configured floor.
4. In live mode, the catastrophic below-floor threshold and normal post-floor
   gate remain active.

## Revisit criteria

Guidance for whoever picks up one of these threads. What is actually
outstanding is defined by the linked PLAN row, not by this list:

1. Start from the exact incident logs and trade rows.
2. Check the deployed order-lifecycle phase before assuming
   which component owns order identity or replacement state.
3. Keep each concern in a separate fix branch and PR.
4. Add focused unit tests and a targeted Alpaca paper verification for any
   broker-facing change.
