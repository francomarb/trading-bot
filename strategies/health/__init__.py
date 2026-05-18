"""
Strategy Health & Edge Monitor (PLAN 11.10).

Per-strategy assessment system that catches the silent killer (clean
execution + steady losses) loudly without over-reacting to normal drawdown.

Edge is the verdict; Health is forensics. Health never overrides Edge.

v1 invariant: the bot informs, the operator decides. No auto-throttle,
no auto-disable. Existing automated controls (cooldown, drawdown halt,
slippage drift halt) remain fully active and are reported as Health inputs.

See docs/strategy_health_design.md for the v1 design and
docs/strategy_health_future.md for the deferred follow-up roadmap.
"""
