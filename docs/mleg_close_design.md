# MLEG Close — Walk-and-Market Design

**Status:** ✅ Active. First consumer: `credit_spread`. Reusable for any
multi-leg options strategy.

**Last updated:** 2026-06-08

---

## Why this exists

Closing a multi-leg options position by submitting a single limit at the
net mid and waiting is unreliable under stressed conditions. The
canonical failure mode is the 2026-06-05 (Friday) QQQ credit spread:

- Stop-loss correctly triggered at 13:39 (mid debit $4.60 ≥ 2× $2.26 credit)
- Strategy submitted a limit at the mid; broker timeout 180s; cancelled
- Repeated five times across the afternoon; **zero fills**
- Position carried the weekend at mark-to-market loss

The mid wasn't a fillable price on a stressed day. Each retry started
from the same starting price with no escalation. There was no
autonomous fallback that guaranteed the close before session close.

The walk-and-market design replaces that with:

1. Walk the limit from a patient starting price (the mid) through several
   escalating steps toward the ask.
2. Each step gives the order book ~30 seconds to interact.
3. If the walk exhausts, submit a market order as the autonomous
   fallback.
4. Operator never has to intervene; the bot resolves every close on its
   own.

## Generic-first design

This subsystem is strategy-agnostic. Any multi-leg options strategy plugs
in by:

1. Emitting an `MlegCloseDecision` from `evaluate_close()` with a typed
   reason code (one of `settings.MLEG_CLOSE_REASONS`).
2. Exposing `build_close_quote_provider(position)` that returns a
   callable producing fresh net `MlegQuote(mid, bid, ask)` per step.

Credit spread is the first consumer. Iron condor, calendar, and other
future MLEG strategies will pattern-match without touching the executor
or the scheduler.

## Architecture

### `utils/safe_expr.py`

AST-whitelisted parser for price-formula strings like
`"mid + 0.25*(ask-mid)"`. No `eval()`; only specific node types
(constants, named bindings, `+ - * /`, unary minus, parentheses) are
allowed. Parse errors fire at config-load time, not at close time.

### `config/settings.py::MLEG_CLOSE_PROFILES`

Global default profiles keyed by exit reason. Each profile is a list of
`(price_expression, duration_seconds)` tuples. The `"market"` expression
is a sentinel — it submits a `MarketOrderRequest` and does not wait for
a timeout.

Three resolution layers, first match wins at runtime:

1. Per-instrument override (e.g. `CREDIT_SPREAD_INSTRUMENTS["SPY"]["close_profiles"]`)
2. Per-strategy override (`MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY`)
3. Global `MLEG_CLOSE_PROFILES`

Missing reasons in any override fall through to the next layer, so
partial overrides are supported.

Settings validation runs at module import: unknown reasons, bad
expressions, malformed step tuples, or "market" appearing anywhere
other than as the final step all raise on startup.

### `execution/mleg_close.py`

Pure data types + scheduler:

- `MlegQuote(mid, bid, ask)` — defensive sanity check (`bid <= mid <= ask`).
- `MlegCloseDecision(should_close, reason, detail, position_id, initial_mid/bid/ask)`
  — what strategies return.
- `MlegCloseStep(step_number, total_steps, price_expr, is_market, limit_price, duration_seconds)`
  — what the executor receives per step.
- `MlegCloseScheduler(profile, *, reason, position_id)` — stateful
  iterator. Pre-compiles all expressions; `next_step(quote)` resolves
  the current step's price; `advance()` moves to the next step;
  `exhausted` flags the end.

No I/O, no broker calls — pure logic, fully unit-testable.

### `execution/options_executor.py::SpreadExecutionWorker`

Accepts optional `close_scheduler`, `quote_provider`, `on_walk_step`.
When `close_scheduler` is set, the worker enters walk-and-market mode:

1. For each step the scheduler yields:
   - Get fresh quote via `quote_provider()`
   - Resolve step price via `scheduler.next_step(quote)`
   - Submit (limit or market) with a new `client_order_id`
   - Wait `step.duration_seconds` for fill via stream/REST
   - On fill → done
   - On timeout → cancel → `scheduler.advance()` → next iteration
2. Telemetry: each step calls `on_walk_step(...)` with status.
3. Outer `on_fill` is deferred until the walk terminates so intermediate
   cancels aren't reported as terminal outcomes.

When `close_scheduler` is `None`, the worker behaves exactly as before
(single-shot submit, wait, cancel-or-fill). Opens always use this path.

### `engine/trader.py` — close dispatch

```
strategy.evaluate_close()              → MlegCloseDecision
                                         (typed reason, detail, initial quote)
resolve_mleg_close_profile(reason, …)  → list[(expr, duration), …]
_mleg_should_bypass_walk(now)          → True near session close
                                         → substitute [("market", 0)] profile
MlegCloseScheduler(profile, …)         → walk iterator
strategy.build_close_quote_provider()  → fresh-quote callable
broker.dispatch_spread_order(
    closing=True,
    close_scheduler=…,
    quote_provider=…,
    on_walk_step=…,
)                                       → SpreadExecutionWorker walks
```

The BEAR regime override and EOS bypass both force a market-only profile
so the position closes autonomously regardless of quote conditions.

## Exit-reason taxonomy

Defined in `config/settings.py::MLEG_CLOSE_REASONS`. Used by every MLEG
strategy:

| Reason | Urgency | Reaches market? |
|---|---|---|
| `profit_target` | Patient — we're winning | No — cancel + reassess next cycle |
| `stop_loss` | Urgent — strategy said exit | Yes |
| `time_stop` | Moderately urgent — DTE driven | Yes |
| `defensive_breach` | Critical — at/near max loss | Yes, fast |

Strategies map their internal triggers to these codes:

| Credit spread trigger | Maps to |
|---|---|
| `mid ≤ profit_target_pct × net_credit` | `profit_target` |
| `mid ≥ stop_loss_multiple × net_credit` | `stop_loss` |
| `DTE ≤ time_stop_dte` | `time_stop` |
| Short strike breached (config-gated) | `defensive_breach` |
| Engine-level BEAR regime override | `defensive_breach` |

## Live profiles (defaults)

```python
MLEG_CLOSE_PROFILES = {
    "stop_loss": [          # 150s walk + market = ~150s autonomous exit
        ("mid",                   30),
        ("mid + 0.25*(ask-mid)",  30),
        ("mid + 0.50*(ask-mid)",  30),
        ("mid + 0.75*(ask-mid)",  30),
        ("ask",                   30),
        ("market",                 0),
    ],
    "time_stop": [          # 120s walk + market
        ("mid",                   30),
        ("mid + 0.33*(ask-mid)",  30),
        ("mid + 0.67*(ask-mid)",  30),
        ("ask",                   30),
        ("market",                 0),
    ],
    "defensive_breach": [   # 60s walk + market — defensive, no patient steps
        ("mid + 0.50*(ask-mid)",  30),
        ("ask",                   30),
        ("market",                 0),
    ],
    "profit_target": [      # 90s walk, never market — winners can wait
        ("mid",                   30),
        ("mid + 0.25*(ask-mid)",  30),
        ("mid + 0.50*(ask-mid)",  30),
    ],
}
```

End-of-session bypass threshold: `MLEG_END_OF_SESSION_BYPASS_SECONDS = 210`.
When less than 210s remain in the regular session (i.e. after ~15:56:30 ET),
the close-dispatch substitutes a market-only profile to guarantee the
exit before the bell. Alpaca mleg orders are day-TIF only.

## Telemetry

Each step the walk visits logs an `mleg_walk_step` structured event
to `logs/bot.jsonl` with:

- `strategy`, `underlying`, `position_id`
- `reason` (one of `MLEG_CLOSE_REASONS`)
- `step_number`, `total_steps`
- `price_expr`, `is_market`, `limit_price` (None when market)
- `duration_seconds`
- `terminal_status` ("filled", "canceled", "rejected", "skipped")

Plus two FYI-only `AlertDispatcher` events:

- `MLEG_CLOSE_WALK_STARTED` (INFO) — fires once when a walk begins
- `MLEG_CLOSE_MARKET_FALLBACK` (WARNING) — fires when the market step
  resolves (filled or rejected)

Neither alert blocks. The operator sees them post-fact via Telegram or
the alerts log.

### Review trigger (`docs/credit_spread_strategy.md`)

After ~10–20 paper credit-spread closes accumulate (4–8 weeks of paper
running), review the distribution of `close_walk_step_filled` from the
`mleg_walk_step` log records:

```
sqlite3 data/trades.db   # plus a small jq pass over bot.jsonl
```

| Pattern | Verdict |
|---|---|
| >60% fills at the market step | Walk isn't catching fills — tune more aggressive starting point |
| >60% fills at step 1 (mid) | Walk over-engineered for this universe; consider simplifying |
| Steps 2–4 capture most fills | Walking is doing what it should; leave alone |
| Avg fill price within $0.05 of starting ask | Walk isn't generating meaningful value |
| Avg fill price meaningfully below starting ask | Walk is paying for itself |

## What this design deliberately doesn't do

- **No operator-required alerts.** Every alert is FYI; the bot resolves
  everything autonomously. This was an explicit operator constraint.
- **No cross-cycle escalation state.** Each cycle starts fresh — if the
  walk doesn't fill, the strategy re-evaluates the close decision on the
  next cycle. Walk-and-market completes within one cycle by design.
- **No single-leg fallback for failed mleg closes.** Alpaca historically
  had mleg close rejection issues. Out of scope; can be added if
  rejections appear in paper data.
- **No structured DB columns for walk telemetry (yet).** The data lands
  as structured `mleg_walk_step` events in `bot.jsonl`. Once the review
  trigger data accumulates and shows what queries we actually need, a
  follow-up adds dedicated columns to `position_lifecycle`.

## Implementation files

- `utils/safe_expr.py` — price-formula parser (33 unit tests)
- `execution/mleg_close.py` — scheduler, decision dataclass, profile resolver (29 unit tests)
- `execution/options_executor.py` — `SpreadExecutionWorker` walk-and-market path
- `execution/broker.py::dispatch_spread_order` — passes scheduler + quote provider through
- `engine/trader.py::_mleg_should_bypass_walk` — EOS bypass detector
- `engine/trader.py::_process_credit_spread_exits` — close-dispatch wiring
- `strategies/credit_spread.py::evaluate_close` — typed decision emitter
- `strategies/credit_spread.py::build_close_quote_provider` — quote callable
- `reporting/alerts.py::mleg_close_walk_started / mleg_close_market_fallback`

## Related docs

- [`credit_spread_strategy.md`](credit_spread_strategy.md) — credit spread spec including the close-walk reference.
- [`strategies.md`](strategies.md) — top-level strategy catalog.
