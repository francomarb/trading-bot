# RSI Mean Reversion — Strategy Research & Deployment Guide

**Status:** ✅ **ACTIVE** — wired in `forward_test.py` and `engine/trader.py`.
Paired with SMA Crossover for regime diversification: trend-followers
profit in directional markets, mean-reverters profit in ranging /
post-extreme markets.

**Last updated:** 2026-06-19

---

## Why this strategy

Short-term mean reversion on individual names. The thesis: a stock that
just crossed *below* an oversold RSI threshold has overshot its
short-term equilibrium and is statistically likely to bounce. We fade
the sell-off with a limit order, hold until RSI prints overbought, then
exit on the cross back up.

**Why this pairs with SMA Crossover.** SMA Crossover is a slow
trend-follower: it bleeds in choppy / ranging markets and prints monster
winners during clean trends. RSI Reversion behaves opposite: it prints
steady small wins during ranging / post-extreme conditions and gets
crushed in sustained one-way moves (a stock can keep falling through
oversold for weeks). Running them together smooths the equity curve —
when one sleeve is in drawdown, the other is typically working.

**Why limit orders.** Mean-reversion entries are timing-sensitive: we
are fading an extreme, so paying the spread + chasing with a market
order would systematically buy at the wrong end of the bounce. The
strategy submits limit orders at the entry reference price and waits
for the fill. If the bounce starts without us, we miss the trade — and
that's the right behavior. The execution layer reads
`preferred_order_type = OrderType.LIMIT` and routes accordingly.

---

## Deployment configuration

| Parameter | Value | Source |
|---|---|---|
| `period` | 14 | `forward_test.py:228` |
| `oversold` | 30 | same |
| `overbought` | 70 | same |
| Order type | LIMIT | `RSIReversion.preferred_order_type` |
| Regime gate | `TRENDING`, `RANGING` only | `settings.STRATEGY_ALLOWED_REGIMES` |
| Edge filter | `RSIEdgeFilter` + `SectorMomentumFilter` (policy=`block`) | `forward_test.py:229-236` |
| Sleeve weight | 0.20 of equity (target) — carved from 0.25 when credit_spread was added | `settings.STRATEGY_ALLOCATIONS["rsi_reversion"]["target_pct"]` |
| Hard max positions | 8 | same |
| Max position % of sleeve | 0.40 | same |
| ATR stop | `entry − 2.0 × ATR(14)` (static) | `settings.ATR_STOP_MULTIPLIER` |
| Watchlist | `RSI_WATCHLIST` (29 names) | `config/settings.py` |
| Min trades for health verdict | 8 | `settings.STRATEGY_MIN_TRADES_FOR_VERDICT` |
| Stop time-in-force | GTC (DAY at submit → promoted to GTC) | `engine/trader.py` |

The min-trades floor is intentionally low (vs. 25 for SMA): RSI's tight
edge filter produces multi-month zero-trade stretches, so a higher floor
would make the health verdict unreachable. See
[`strategy_health_design.md`](strategy_health_design.md) §8.

---

## Signal logic

Implemented in `strategies/rsi_reversion.py`:

```python
rsi      = RSI(close, period=14)
prev_rsi = rsi.shift(1)

entries = (rsi < 30) & (prev_rsi >= 30)   # crosses BELOW oversold
exits   = (rsi > 70) & (prev_rsi <= 70)   # crosses ABOVE overbought
```

**Look-ahead safety.** `rolling`, `shift`, and `diff` use only past data.
The signal at bar `t` depends only on closes up to and including `t`.
The backtester shifts execution to bar `t+1`'s open; this strategy does
*not* itself shift.

**Threshold-cross semantics.** Both signals require a confirmed
*crossing* — the previous bar's RSI must have been on the opposite side
of the threshold. This prevents re-firing on consecutive bars that stay
below 30 (or above 70). One position per cross.

---

## Exit logic

Two exits, both **static** (no trailing). No take-profit other than the
overbought signal itself.

### 1. Overbought-cross exit (primary)

Fires when `RSI > 70` after being `≤ 70` on the prior bar. Engine reads
`exits` from the SignalFrame and closes at market on the next open.
This is the natural "mean-reverted" exit.

### 2. ATR protective stop (disaster brake)

Set once at entry as a GTC stop order at the broker:

```python
stop_price = entry_price − 2.0 × ATR(14)
```

The stop does **not** trail. Its purpose is to cap loss when the
oversold cross was the *start* of a sustained breakdown rather than an
overshoot to fade (the "falling knife" failure mode). The ATR stop also
serves as the denominator for fixed-fractional position sizing
(`risk_dollars = equity × risk_per_trade_pct["rsi_reversion"]` — 0.25% of
equity per 11.48; `qty = risk_dollars / |entry − stop|`. `MAX_POSITION_PCT`
= 2% remains only the global ceiling; pre-11.48 it was the formula input
but the notional caps overruled it on essentially every entry).

**Note on the 90-day GTC ceiling.** Alpaca auto-cancels GTC orders
after 90 days. RSI's limit-order entries occasionally sit open for
weeks waiting for an oversold cross to actually fill; if a limit order
were submitted as GTC and sat open long enough, it would silently
expire. The engine's startup reconciliation (`sync_with_broker`) catches
this, but operators should be aware of the ceiling.

---

## Filter stack

Applied in order. Any failure blocks the entry.

1. **Regime gate** (`settings.STRATEGY_ALLOWED_REGIMES["rsi_reversion"]`) —
   sleeve enabled only in `TRENDING` and `RANGING`. Disabled in `BEAR`
   (stocks can keep falling past oversold for weeks) and `VOLATILE`
   (fear-driven overshoots are unpredictable; snap-back timing is unreliable).
2. **`RSIEdgeFilter`** (`strategies/filters/rsi_reversion.py`):
   - **SPY intermediate trend gate**: SPY within 1% of its 50 SMA. The structural
     SPY > 200 SMA / BEAR-market veto is owned by the engine-level regime
     detector, so the RSI filter keeps only the RSI-specific 50 SMA
     confirmation: we will not fade oversold in a market that is itself in
     material short-term decline, while allowing tiny SPY undercuts that
     backtested as high-value reversion windows.
   - **Earnings blackout**: 3 days before / 2 days after. Stricter than
     SMA's blackout because RSI fades volatility, and earnings is the
     event most likely to make today's "overshoot" tomorrow's "new
     normal."
   - **Liquidity floor**: 20-day average dollar volume ≥ $10M. Tighter
     than the broader watchlist liquidity requirement; mean-reversion
     is exit-quality-sensitive and we need fillable size.
   - **Active-breakdown gate**: blocks only when the stock is making a new
     20-day low and is below its 200-day SMA. RSI < 30 below long-term
     trend support can be a falling-knife signal; a short-term low above the
     200-day trend remains eligible as a normal pullback.
3. **`SectorMomentumFilter`** (`sector/gauge.py`) — sector policy is
   `block` (not `warn` as SMA uses). Mean-reversion in a COLD sector is
   cluster risk: multiple names in the same falling sector all flash
   oversold simultaneously, producing correlated losses. We block the
   entry rather than warn.

The composite is wired in `forward_test.py`:

```python
RSIReversion(
    period=14, oversold=30, overbought=70,
    edge_filter=CompositeEdgeFilter([
        RSIEdgeFilter(),
        SectorMomentumFilter(
            gauge=sector_gauge, resolver=sector_resolver,
            sector_entry_policy="block",
            score_threshold=-3,
        ),
    ]),
)
```

**Why the filter stack is tighter than SMA's.** Mean-reversion has a
worse failure mode than trend-following: the bad case is not "small
chop" but "the trend you tried to fade keeps going." The compounding
gates exist to filter out the falling-knife setups where the snap-back
never comes.

---

## Watchlist

Selection rules live in [`rsi-watchlist-selection.md`](rsi-watchlist-selection.md)
and [`static-rsi-watchlist-selection.md`](static-rsi-watchlist-selection.md);
dynamic-selection notes are in [`dynamic-rsi-watchlist.md`](dynamic-rsi-watchlist.md).

**Current composition (2026-06-06):** 29 names in
`config/settings.py::RSI_WATCHLIST`. The list is intentionally broader
than the original narrow scanner snapshot — the comment in `settings.py`
notes "this list intentionally favors breadth over the earlier narrow
scanner snapshot so the RSI sleeve can accumulate enough trades for
evaluation."

The tight filter stack significantly thins the effective watchlist on
any given day: most names are blocked by SPY trend, earnings blackout,
or active-breakdown at any moment. This is why the breadth was deliberately
increased.

---

## Backtest validation

The strategy was originally validated via the Phase 5 vectorbt harness
(`backtest/runner.py`) on a narrower RSI watchlist before the 2026-04-30
expansion. Key empirical properties from that pass:

- **Win rate higher than trend strategies** (mean-reversion's structural
  property): typical 55–65% win rate vs. SMA's 30–40%.
- **Smaller average trade size**: wins are bounded by the overbought
  exit (typically 5–15% from entry), unlike SMA where a single ASML
  can return 200%+. Many small wins, occasional moderate losses.
- **Negative correlation with SMA returns** at the sleeve level: when
  the regime is choppy and SMA is bleeding, RSI is firing entries; in a
  clean trend, RSI is filtered out by the macro gates while SMA prints
  runners.

A dedicated audit of RSI giveback / per-symbol P&L concentration in the
style of `scripts/sma_giveback_audit.py` has **not** been run. See the
optimization opportunities section below.

---

## Methodology and limitations

**Look-ahead.** Same as SMA — signals fire on bar `t`'s close; execution
shifts to bar `t+1`'s open via the backtester's `_shift_for_next_open`.

**Limit-order fill modeling.** Backtest assumes the limit order fills at
the bar's open if `open ≤ entry_reference_price`. Production sits a
genuine GTC limit at the price; partial fills and stale orders are
handled by `sync_with_broker`. RSI entries are LIMIT orders, so under
the unified slippage taxonomy (Phase 2 + 4, PR #67) they record
`slippage_benchmark_kind='limit_price'` and
`slippage_measurement_quality='unavailable'` with NULL on
`slippage_signed_bps` / `slippage_adverse_bps` — arrival-price
slippage isn't a meaningful execution-quality metric for a passive
fill. Limit-fill execution quality versus the limit price is a
separate (out-of-scope) metric. Stop-out fills do record adverse
slippage against the active stop price.

**Trade cadence.** The filter stack is intentionally tight and produces
multi-month zero-trade stretches. The min-trades-for-verdict floor (8)
reflects this; the L3 Drift detector handles the "RSI isn't firing at
all" case independently of whether the Edge verdict ever reaches
CONCLUSIVE.

**Data feed.** Same IEX caveat as the other equity strategies. Volume
gates (especially the $10M dollar-volume floor in `RSIEdgeFilter`) are
calibrated to IEX scale, not SIP.

**Survivorship bias.** Same as SMA — the current watchlist contains
symbols that exist today. Names delisted between 2018 and 2026 are not
in the backtest.

---

## Optimization opportunities

A dedicated `rsi_reversion_optimizations.md` has not been created yet. The
June 2026 filter audit relaxed three over-protective gates: structural SPY200
was delegated fully to regime, SPY50 became a 1% band, and the 20-day-low block
became an active-breakdown rule that only blocks below the stock's 200 SMA.
Remaining likely candidates, by analogy with the SMA audit:

- **Giveback / capture-ratio audit** — replicate `sma_giveback_audit.py`
  for the overbought-cross exit on RSI winners. Mean-reversion winners
  are bounded above by the overbought signal, so giveback should be
  structurally smaller than SMA — but worth measuring.
- **Per-symbol profit concentration** — does the same 80/20 pattern hold
  (a few names produce most of the alpha)? If yes, the same cull-the-
  chronic-losers approach applies.
- **Filter contribution analysis** — of the remaining `RSIEdgeFilter` gates,
  which actually catch falling-knife trades and which fire on already-good
  entries (false positives)? After the June 2026 relaxations, this should be
  paper-watched before further loosening.
- **Watchlist breadth review** — the 2026-04-30 expansion deliberately
  widened the list; six months of paper data is now available to assess
  whether the expansion produced viable trades or just more blocked
  signals.

When an audit produces findings worth recording, follow the SMA pattern
and create `docs/rsi_reversion_optimizations.md`.

---

## Implementation files

- `strategies/rsi_reversion.py` — strategy class.
- `strategies/filters/rsi_reversion.py` — `RSIEdgeFilter` (SPY50 band,
  earnings, liquidity, active-breakdown).
- `strategies/filters/common.py` — `SPYTrendFilter`, `EarningsBlackout`.
- `sector/gauge.py` — `SectorMomentumFilter`.
- `regime/detector.py` — regime classifier.
- `risk/manager.py` — sizing + ATR stop computation.
- `engine/trader.py` — engine wiring (live + paper).
- `forward_test.py` — forward-test wiring with full filter stack.
- `backtest/runner.py` — vectorbt harness used for initial validation.

## Related docs

- [`rsi-watchlist-selection.md`](rsi-watchlist-selection.md) — watchlist
  selection rules.
- [`static-rsi-watchlist-selection.md`](static-rsi-watchlist-selection.md) —
  static list rationale.
- [`dynamic-rsi-watchlist.md`](dynamic-rsi-watchlist.md) — dynamic-selection
  design notes.
- [`RSI-edge-filter.md`](RSI-edge-filter.md) — edge filter design.
- [`regime_flowchart.md`](regime_flowchart.md) — regime classification.
- [`capital_allocation_reference.md`](capital_allocation_reference.md) —
  sleeve weights and capital budgeting.
- [`strategy_health_design.md`](strategy_health_design.md) — health
  monitor; explains the low min-trades-for-verdict floor.
