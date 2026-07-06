# Temporary option-stop diagnostics

This diagnostic is narrowly scoped to unresolved `spy_options_reversion`
option-stop execution questions: immediate-fill timing while ratcheting
broker-side stops, and adverse slippage on ordinary protective-stop fills. It
is instrumentation, not a trading-behavior change and not a general order
audit.

## Isolation

- Disabled by default.
- Runs only for positions owned by `spy_options_reversion` by default.
- Uses `data/diagnostics/option_stop_replace_audit.db`, not `trades.db`.
- Performs no extra broker reads, stream registrations, or writes while
  disabled.
- Retains 14 days by default while enabled.
- Removing the standalone DB removes all captured evidence without touching
  accounting or lifecycle state.

## Enable temporarily

Set these in `config/.env`, then recycle the bot:

```dotenv
OPTION_STOP_REPLACE_AUDIT_ENABLED=true
OPTION_STOP_REPLACE_AUDIT_STRATEGY=spy_options_reversion
OPTION_STOP_REPLACE_AUDIT_WINDOW_SECONDS=300
OPTION_STOP_REPLACE_AUDIT_RETENTION_DAYS=14
```

Each initial stop submit records quote state, position mark/value, HWM,
requested stop, submit request timing, the broker REST response, and matching
WebSocket trade updates. Each ratchet decision records cached and freshly
fetched order state, quote state, position mark/value, HWM and requested stop,
replace request timing, the replacement REST response, and matching WebSocket
trade updates. When an audited option stop fills, the engine records a
separate fill-context row with stop price, fill price, adverse slippage bps,
execution id, broker timestamp, and the latest option bid/ask if available.

Stream association ends on fill, cancel, rejection, expiry, or after five
minutes. Retention pruning runs at startup and at most once per 24 hours while
enabled.

Inspect evidence with:

```bash
python scripts/dump_option_stop_audit.py --occ SPY260702C00724000
python scripts/dump_option_stop_audit.py --correlation-id optstop_... --raw
```

## Disable and remove

Set `OPTION_STOP_REPLACE_AUDIT_ENABLED=false` and recycle the bot. The
diagnostic then has zero runtime activity. After exporting any useful incident
report, delete `data/diagnostics/option_stop_replace_audit.db`.

This work does not close the deferred investigation. A behavioral change
should follow only if captured broker and stream evidence identifies a
specific failure mechanism.
