# RSI Reversion Strategy

**Status:** Active in paper trading.

**Last updated:** 2026-08-21

## Thesis

RSI Reversion is a short-term mean-reversion sleeve. The active paper experiment is deliberately simple:

> Buy meaningful short-term oversold conditions in liquid stocks that are still above long-term trend, then exit on the first practical bounce.

This is a reset from the earlier conservative RSI14 design, which produced months of no fills. Backtests are used as a reference only; live paper evidence and broker/trade logs remain the authority for whether the strategy is viable.

## Production Configuration

Configured in `forward_test.py`:

| Parameter | Value |
|---|---|
| Strategy class | `RSIReversion` |
| RSI period | `3` |
| Entry threshold | `RSI3 < 15` |
| Entry mode | `level_below` |
| Quick RSI exit | `RSI3 > 55` |
| SMA exit | `close > SMA5` |
| Entry order type | LIMIT |
| Regime gate | None (`allowed_regimes=None`) |
| Edge filter | `RSIEdgeFilter()` |
| Watchlist | `settings.RSI_WATCHLIST` |
| Sleeve target | 20% of deployable equity pool |
| Hard max positions | 8 |
| Risk per trade | 0.25% of equity |
| Protective stop | `entry - 2 * ATR(14)` |

`RSIReversion` keeps backward-compatible defaults (`period=14`, cross-below-30 entry, cross-above-70 exit) for generic tests and research scripts. Production opts into the RSI3 quick-exit behavior through explicit parameters.

## Signal Logic

Entry:

```python
rsi = RSI(close, period=3)
entries = rsi < 15
```

Exit:

```python
exits = (close > SMA(close, 5)) | (rsi > 55)
```

Plain English:

- The old design waited for a single RSI14 cross below 30, then waited for RSI14 to recover above 70.
- The active design treats each daily RSI3 value below 15 as a candidate while flat, then exits much sooner on a close above the 5-day average or an RSI3 recovery above 55.

The engine still enforces normal one-position ownership and pending-order checks, so consecutive oversold bars do not stack duplicate positions in the same symbol.

## Filter Stack

The active RSI edge filter has only two gates:

| Gate | Rule | Rationale |
|---|---|---|
| Stock trend | `close > SMA200` | Avoid buying dips in companies already below long-term trend |
| Liquidity | 20-day average dollar volume >= `$10M` | Passive limit entries and stop exits need fillable names |

Removed from active production RSI3:

- SPY50 gate
- earnings blackout
- active-breakdown 20-day-low gate
- sector COLD block
- regime gate

Those removed gates may still appear in historical research docs/scripts. They are no longer active production RSI behavior unless explicitly reintroduced in `forward_test.py`.

## Exits And Risk

Signal exits are deliberately quicker than the old RSI14 design. The strategy should not wait for a full overbought condition after a mean-reversion bounce.

The ATR stop remains the disaster brake:

```python
stop_price = entry_price - 2.0 * ATR(14)
```

Position sizing is risk-first through the existing `RiskManager` and sleeve allocator. The simplification is in signal/filter design only; it does not bypass max positions, sleeve caps, global halts, startup reconciliation, broker ownership, or stop placement.

## Observability

The existing `strategy_lifecycle_counters` table remains the aggregate source for raw candidates and gate attribution.

For RSI raw entry candidates, the engine also logs one compact `RSI_CANDIDATE` line with RSI value, edge pass/fail, SMA200/liquidity diagnostics, and block reasons. This is intentionally candidate-only, not every symbol every cycle.

## Evidence And Current Read

The 2026-08 starvation audit found the old design was starved by two things:

- raw RSI14 cross-below-30 signals were rare
- stacked filters then blocked most of the few raw candidates

Reference SIP studies for the RSI3 quick-exit candidate showed materially higher signal density, but those numbers are not treated as final truth. The paper run is the decision source. The acceptance question is:

> Does the simplified RSI3 strategy produce enough live paper candidates and fills to evaluate expectancy without immediately showing unacceptable drawdown or clustered failures?

Historical audit: [`rsi_reversion_starvation_audit.md`](rsi_reversion_starvation_audit.md).

Edge filter details: [`RSI-edge-filter.md`](RSI-edge-filter.md).
