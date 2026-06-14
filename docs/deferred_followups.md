# Deferred trade follow-ups — 2026-06-12

**Status:** Recorded for later review. Do not investigate or implement while
the current parallel bot work is still in flight.

**Context:** Two operational observations were found while reviewing the
June 12, 2026 paper-trading activity. Neither observation invalidates the
day's realized SPY option profit, but both deserve a focused review after the
active workstreams settle.

## 1. Option trailing-stop replacement and fill timing

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

Questions for later review:

1. Did the fill belong to the old stop while its replacement was in flight, the
   replacement order, or another broker-side event?
2. Is the persisted stop order identity correctly associated with the fill
   during Alpaca replace events?
3. Is the reported stop slippage meaningful when a replace and fill race in
   this way?
4. Do tests cover replacement/fill ordering from both REST snapshots and
   WebSocket trade updates?

Do not infer a defect solely from the favorable fill. Reconstruct the broker
event sequence and Alpaca replacement semantics before proposing a change.

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

Return to these items only when the operator explicitly resumes them and the
current order-lifecycle and other parallel bot work has settled. At that point:

1. Start from the exact June 12 logs and trade rows.
2. Check the current branch and deployed order-lifecycle phase before assuming
   which component owns order identity or replacement state.
3. Investigate each concern separately; do not bundle them into one fix.
4. Add focused unit tests and a targeted Alpaca paper verification for any
   broker-facing change.
