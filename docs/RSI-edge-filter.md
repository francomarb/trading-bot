# RSI Edge Filter

> Documents the **as-built** `RSIEdgeFilter` in `strategies/filters/rsi_reversion.py`.

**Last updated:** 2026-08-21

## Current Role

`RSIEdgeFilter` is intentionally small for the active RSI3 quick-exit paper experiment. Its job is not to make RSI "perfect"; its job is to keep the strategy from buying structurally weak or illiquid names while letting the raw RSI signal prove whether it has live edge.

The production strategy wiring is:

```python
RSIReversion(
    period=3,
    oversold=15,
    overbought=70,
    entry_mode="level_below",
    exit_sma_window=5,
    quick_exit_rsi=55,
    edge_filter=RSIEdgeFilter(),
)
```

The `StrategySlot` uses `allowed_regimes=None`, meaning the engine does not apply a regime gate to RSI entries.

## Active Gates

All gates must pass for an entry to be allowed. Exits are never blocked by an edge filter.

| Gate | Rule | Failure Behavior | Rationale |
|---|---|---|---|
| Stock trend | `close > SMA200` | Fails closed until SMA200 exists | Avoids buying dips in companies already below long-term trend |
| Liquidity | 20-day average dollar volume >= `$10,000,000` | Fails open if volume is unavailable or the rolling window is not ready | Passive limit entries and stop exits need fillable names |

The filter returns `EdgeFilterDecision`, so callers receive both an aligned boolean `allowed` series and structured latest-bar block reasons through the existing `BaseStrategy.inspect_signals(...)` seam.

## Removed Gates

The prior RSI filter stack included SPY50, earnings blackout, active-breakdown, sector COLD, and `TRENDING`/`RANGING` regime gates. The 2026-08 starvation audit showed the strategy had become over-protected: scarce raw RSI14 crosses were then filtered heavily, producing months with no fills.

Those gates are deliberately **not** part of the active RSI3 experiment:

| Removed Gate | Current Status |
|---|---|
| SPY50 / 1% band | Removed from production RSI3; still present only in historical research harnesses |
| Earnings blackout | Removed from production RSI3 |
| Active-breakdown 20-day low | Replaced by the simpler stock `close > SMA200` gate |
| Sector momentum block | Removed from production RSI3 |
| Regime gate | Disabled for RSI via `allowed_regimes=None` |

This is a design reset, not a claim that those risks are imaginary. The live paper question is narrower: can a simple RSI3 dip signal produce enough clean evidence when protected only by stock trend, liquidity, ATR stops, sleeve limits, max-position caps, and global risk halts?

## Observability

The engine continues to write aggregate lifecycle counters to `strategy_lifecycle_counters`:

- `raw_signals`
- `regime_blocked`
- `edge_filter_blocked`
- `sleeve_blocked`
- `risk_blocked`
- `submitted`
- `filled_entries`

Additionally, when RSI has a raw entry candidate, the engine logs one compact `RSI_CANDIDATE` line with:

- symbol and signal bar
- current regime for context
- RSI value and oversold threshold
- whether the edge filter allowed the candidate
- stock SMA200/liquidity diagnostics
- structured block reasons

This uses the existing `inspect_signals(...)` and lifecycle-counter architecture. No new trading decision path or database schema is introduced.

## Historical Context

The prior starvation audit remains in [`rsi_reversion_starvation_audit.md`](rsi_reversion_starvation_audit.md). Treat it as diagnosis of the old RSI14 plus stacked-filter design, not as the active production spec.

The old SPY50 variant harness (`scripts/rsi_filter_variant_backtest.py`) remains useful for historical comparison, but it no longer mirrors active RSI3 production behavior.
