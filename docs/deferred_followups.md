# Deferred trade follow-ups — 2026-06-12

**Status:** Item 1 is active on a dedicated fix branch. Item 2 remains deferred
while its related order-lifecycle work is in flight.

**Context:** Two operational observations were found while reviewing the
June 12, 2026 paper-trading activity. Neither observation invalidates the
day's realized SPY option profit, but both deserve a focused review after the
active workstreams settle.

## 1. Option trailing-stop replacement and fill timing

**Status:** Fix in review. The replacement path now requires a fresh,
two-sided Alpaca option quote before increasing a stop price. The executable
bid must support the proposed stop with a safety buffer and the spread must
remain within a conservative quality ceiling. A failed quality check leaves
the existing broker stop active and retries next cycle; missing-stop creation
and required GTC/quantity maintenance remain fail-safe and are not blocked.

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

The fix does not classify either profitable fill as defective. It improves the
evidence required for future stop-price increases without changing the existing
broker-side protection or converting the strategy to software-only exits.

## 2. ALAB attached stop cannot be promoted to GTC

The `donchian_breakout` strategy opened 27 ALAB shares at an average price of
$378.49 with an attached protective stop near $308.53. The entry used the
capped DAY limit plus OTO path.

The attached stop became active, but attempts to promote it from DAY to GTC
were repeatedly rejected by Alpaca with:

`time_in_force cannot be changed for advanced orders`

The position had intraday protection at the time of review, but the repeated
promotion failure raises an overnight-protection concern because a DAY child
order expires at the end of the session.

Questions for later review:

1. Does Alpaca permit replacing only the time-in-force of an attached OTO child
   order, or must the child/order group be canceled and rebuilt?
2. What broker-native order shape should provide a capped equity entry plus a
   durable GTC protective stop?
3. If cancel/rebuild is required, how should the engine avoid an uncovered
   window or duplicate SELL exposure?
4. Why does the repair loop retry every cycle after receiving a deterministic
   unsupported-operation response?
5. Does startup reconciliation recreate valid protection after the DAY child
   expires?

Treat this as an execution-safety follow-up. Consult Alpaca's current official
SDK documentation and paper-test the chosen recovery path before changing the
live-facing order lifecycle.

## Revisit criteria

For any remaining work:

1. Start from the exact incident logs and trade rows.
2. Check the deployed order-lifecycle phase before assuming
   which component owns order identity or replacement state.
3. Keep each concern in a separate fix branch and PR.
4. Add focused unit tests and a targeted Alpaca paper verification for any
   broker-facing change.
