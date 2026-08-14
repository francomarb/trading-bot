# Deferred trade follow-ups — 2026-06-12

**Status:** Item 1 is active on a dedicated fix branch. Item 2 remains deferred
while its related order-lifecycle work is in flight. Item 3 is being addressed
by a dedicated allocator-policy PR.

**Context:** Two operational observations were found while reviewing the
June 12, 2026 paper-trading activity. Neither observation invalidates the
day's realized SPY option profit, but both deserve a focused review after the
active workstreams settle.

## 1. Option trailing-stop replacement and fill timing

**Status:** Quote-quality hardening merged in PR #63. The replacement path now requires a fresh,
two-sided Alpaca option quote before increasing a stop price. The executable
bid must support the proposed stop with a safety buffer and the spread must
remain within a conservative quality ceiling. A failed quality check leaves
the existing broker stop active and retries next cycle; missing-stop creation
and required GTC/quantity maintenance remain fail-safe and are not blocked.

The fill-timing mechanism remains unresolved. A separate, temporary diagnostic
is being reviewed to capture broker and WebSocket evidence only for
`spy_options_reversion`. It is disabled by default, writes to a disposable
standalone DB, and does not close this follow-up by itself. See
`docs/option_stop_replace_diagnostics.md`.

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

Status: fix branch in review. Paper verification still required on the next
capped fill: DAY child cancel event, one standalone GTC protective stop, no
duplicate SELL exposure, substrate row TIF matches broker truth.

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

Policy update (2026-08-14): paper development defaults to observation-only at
every sample size, so the sleeve drawdown remains visible but never blocks new
entries while strategies are being tuned. A mature paper strategy can opt into
the normal post-floor gate with `PAPER_STRATEGY_DRAWDOWN_GATE_ENABLED=true`.
Live mode retains the catastrophic below-floor backstop and normal post-floor
gate. The bot still keeps the real hard-risk layers active in both modes:
daily/account loss controls, hard sizing, broker-side stops, max positions,
sleeve-budget checks, entry quality guards, and exits.

Acceptance:

1. In default paper mode, `SleeveAllocator.is_strategy_in_drawdown(...)`
   returns false at every trade count while retaining full observability.
2. `drawdown_snapshot(...)` still reports running P&L, HWM, drawdown dollars,
   trade count, floor, and the effective threshold for observability.
3. Explicitly enabling the paper gate resumes the normal HWM threshold at the
   configured floor.
4. In live mode, the catastrophic below-floor threshold and normal post-floor
   gate remain active.

## Revisit criteria

For any remaining work:

1. Start from the exact incident logs and trade rows.
2. Check the deployed order-lifecycle phase before assuming
   which component owns order identity or replacement state.
3. Keep each concern in a separate fix branch and PR.
4. Add focused unit tests and a targeted Alpaca paper verification for any
   broker-facing change.
