# Temporary option-stop replacement diagnostics

This diagnostic is narrowly scoped to the unresolved immediate-fill timing
seen while `spy_options_reversion` ratcheted broker-side option stops. It is
instrumentation, not a trading-behavior change and not a general order audit.

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

Each ratchet decision records cached and freshly fetched order state, quote
state, position mark/value, HWM and requested stop, replace request timing,
the replacement REST response, and matching WebSocket trade updates. Stream
association ends on fill, cancel, rejection, expiry, or after five minutes.
Retention pruning runs at startup and at most once per 24 hours while enabled.

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
