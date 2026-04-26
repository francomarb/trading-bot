# SMA Edge Filter — Implementation Reference

> Documents the **as-built** `SMAEdgeFilter` in `strategies/filters/sma_crossover.py`.
> This is not a design proposal — it reflects what is running in paper trading today.

---

## Purpose

The SMA crossover strategy fires on 20/50 SMA crossovers. Without entry gates, it fires on crossovers in structurally broken names (the stock is below its own 200-day SMA) and on crossovers driven by low-participation moves (contracting volume, no institutional demand behind the signal). The `SMAEdgeFilter` blocks those two conditions while leaving all valid trend-following setups intact.

**Core principle:** confirm that the crossover aligns with the long-term trend and that institutional volume supports the move.

---

## Architecture

```
raw SMA crossover entry signal
        │
        ▼
  SMAEdgeFilter.__call__(df)
        │  AND-gates 2 boolean Series
        ▼
filtered entry signal → Risk Manager → Execution
```

- `SMACrossover._raw_signals(df)` detects the unfiltered 20/50 SMA crossover
- `BaseStrategy.generate_signals(df)` AND-gates entries with the edge filter output
- **Exits are never blocked** — enforced unconditionally by `BaseStrategy`
- `BaseStrategy.generate_signals` calls `filter.set_symbol(symbol)` before `filter(df)` so the filter knows which symbol it is evaluating

The filter lives at `strategies/filters/sma_crossover.py`. Shared building blocks (`SPYTrendFilter`, `EarningsBlackout`) live in `strategies/filters/common.py`.

---

## Gates

Both gates must be `True` on a given bar for an entry to be allowed on that bar. Exits are never evaluated against the filter.

### Gate 1 — Stock Structural Strength

**Rule:** `stock close > stock 200-day SMA`

**Why:**
A 20/50 SMA crossover while the stock is below its 200-day SMA is typically a short-term recovery within a structurally broken name, not a genuine trend change. These false crossovers occur frequently when a declining stock bounces temporarily — the fast SMA crosses the slow SMA on the bounce, but the underlying trend has not reversed. Requiring the stock to be above its own 200 SMA ensures the crossover aligns with the long-term trend direction.

This is computed from the same historical bars already fetched for the symbol — no additional API call. Window = 200 bars.

**Fail-open:** if there are fewer than 200 bars of history (NaN SMA), the gate returns `True`. Insufficient history is not treated as a rejection — it occurs naturally during early warmup periods and on new watchlist additions.

---

### Gate 2 — Volume Expansion

**Rule:** 10-day average volume > 30-day average volume

**Why:**
A crossover on contracting volume is a weak signal. If institutions are not participating — volume is shrinking relative to the recent baseline — the move is likely noise rather than the start of a sustained trend. Expanding volume (10-day average exceeding 30-day average) confirms that demand is growing, not fading, at the point of the crossover.

**Fail-open:** if the `volume` column is absent or there are fewer than 30 bars of history (NaN rolling averages), the gate returns `True`. Volume data absence does not silently block trades.

---

## Default Parameters

| Parameter | Default | Description |
|---|---|---|
| `stock_sma_window` | 200 | SMA period for the stock's own trend check (bars) |
| `vol_short_window` | 10 | Short rolling window for volume expansion (bars) |
| `vol_long_window` | 30 | Long rolling window for volume expansion (bars) |

All parameters can be overridden at construction time:

```python
edge = SMAEdgeFilter(
    stock_sma_window=150,
    vol_short_window=5,
    vol_long_window=20,
)
```

---

## Observability

Every filter decision on the most recent bar is logged. No silent passes or blocks.

**Entry allowed:**
```
DEBUG | SMAEdgeFilter: ALLOWED MU — stock>200SMA vol_expanding
```

**Entry blocked** (all failing gates listed):
```
INFO | SMAEdgeFilter: BLOCKED NVDA — stock 118.32 ≤ SMA200 124.57
INFO | SMAEdgeFilter: BLOCKED WDC — volume contracting (avg10 ≤ avg30)
INFO | SMAEdgeFilter: BLOCKED CIEN — stock 42.10 ≤ SMA200 45.88, volume contracting (avg10 ≤ avg30)
```

Allowed decisions log at DEBUG (high-frequency, not noise-worthy). Blocked decisions log at INFO (operator should know why a setup was suppressed).

---

## Design Decisions and Exclusions

### SPY > 200 SMA gate — intentionally disabled

**Why it was considered:** trading trend-following in a broad bear market (SPY below 200 SMA) produces systematically worse outcomes as the macro tailwind is absent.

**Why it is disabled here:** this gate is owned by the `RegimeDetector` in `regime/detector.py` as the universal BEAR regime classification. The engine enforces it at the slot level — when the regime is BEAR, no new SMA entries are processed regardless of what the filter returns. Duplicating the SPY > 200 SMA check inside the filter would be redundant dead code.

**Re-enable if:**
- The `RegimeDetector` is disabled, removed, or bypassed
- The BEAR regime no longer uses `SPY > 200 SMA` as its primary condition

The re-enable path is prepared in the code: the `SPYTrendFilter` import is retained, and the commented-out `self._spy_filter` instantiation and `spy_gate` lines in `__call__` can be uncommented in one step.

### Earnings blackout — intentionally excluded

Earnings blackout belongs on RSI Reversion, not SMA Crossover. Trend-following strategies benefit from earnings announcements that act as catalysts — a strong earnings beat can accelerate an established trend and is exactly the kind of move the strategy is designed to capture. Blocking entries around earnings would cause missed setups without meaningfully reducing risk for this strategy type.

By contrast, RSI mean-reversion buys oversold dips. A dip into an earnings announcement is binary event risk, not a reversion opportunity — the correct home for the blackout rule.

### Stock 50-day SMA gate — excluded

A crossover on a stock below its 50 SMA is a valid entry for trend-following if the stock is above its 200 SMA. The 50 SMA gate would be too aggressive: it would block many valid early-stage trend resumptions where the 20/50 crossover fires while the stock is still recovering from a pullback. The 200 SMA gate provides sufficient structural confirmation without over-filtering.

---

## Relationship to the Regime Detector

`SMAEdgeFilter` and `RegimeDetector` operate at different scopes:

| Layer | Scope | Blocks when |
|---|---|---|
| `RegimeDetector` (engine-level) | Entire strategy slot | BEAR (SPY < 200 SMA) or VOLATILE (high ATR%) |
| `SMAEdgeFilter` (symbol-level) | Individual symbol | Stock below 200 SMA or volume contracting |

The regime detector fires first — if the regime is BEAR or VOLATILE, the engine skips entries entirely for the SMA slot and the filter is never called. The filter only evaluates individual symbols when the macro regime permits new entries.

---

## Integration

```python
# forward_test.py
from strategies.filters.sma_crossover import SMAEdgeFilter
from strategies.sma_crossover import SMACrossover

slot = StrategySlot(
    strategy=SMACrossover(
        fast=20,
        slow=50,
        edge_filter=SMAEdgeFilter(),
    ),
    watchlist_source=StaticWatchlistSource(settings.SMA_WATCHLIST, name="sma"),
    allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
)
```

The `allowed_regimes` on the `StrategySlot` provides the engine-level BEAR and VOLATILE block. The `SMAEdgeFilter` provides the symbol-level stock trend and volume gates within allowed regimes.

---

## Phase 11 Deferred Items

| Item | Description |
|---|---|
| 11.21 | RSI-at-entry overbought gate — block SMA entries where RSI on the crossover bar is already ≥ 70; crossovers into overbought territory have lower continuation probability; requires forward-test data to calibrate threshold |
| 11.22 | Same-day concentration cap — when ≥ N SMA symbols cross over on the same day (broad market rip), limit new entries to the top N ranked by volume expansion or crossover angle; prevents concentrated simultaneous deployment into correlated positions |
| 11.25 | VIX integration — high VIX generally unfavourable for SMA trend-following (whipsaw risk in late-stage volatile trends); could tighten the VOLATILE threshold selectively |
