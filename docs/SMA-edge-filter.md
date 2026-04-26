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

All three gates must be `True` on a given bar for an entry to be allowed on that bar. Exits are never evaluated against the filter.

### Gate 1 — Stock Structural Strength

**Rule:** `stock close > stock 200-day SMA`

**Why:**
A 20/50 SMA crossover while the stock is below its 200-day SMA is typically a short-term recovery within a structurally broken name, not a genuine trend change. These false crossovers occur frequently when a declining stock bounces temporarily — the fast SMA crosses the slow SMA on the bounce, but the underlying trend has not reversed. Requiring the stock to be above its own 200 SMA ensures the crossover aligns with the long-term trend direction.

This is computed from the same historical bars already fetched for the symbol — no additional API call. Window = 200 bars.

**Fail-open:** if there are fewer than 200 bars of history (NaN SMA), the gate returns `True`. Insufficient history is not treated as a rejection — it occurs naturally during early warmup periods and on new watchlist additions.

---

### Gate 2 — Volume Expansion

**Rule:** 10-day median volume > 30-day median volume

**Why:**
A crossover on contracting volume is a weak signal. If institutions are not participating — volume is shrinking relative to the recent baseline — the move is likely noise rather than the start of a sustained trend. Expanding volume (10-day median exceeding 30-day median) confirms that demand is growing, not fading, at the point of the crossover. We use the median rather than the mean to prevent a single massive volume spike (e.g. an old earnings event 25 days ago) from artificially inflating the 30-day baseline and causing false lockouts.

**Fail-open:** if the `volume` column is absent or there are fewer than 30 bars of history (NaN rolling averages), the gate returns `True`. Volume data absence does not silently block trades.

---

### Gate 3 — Pre-Earnings Blackout

**Rule:** No new entry within 2 calendar days **before** an earnings announcement. Entries are allowed immediately **after** earnings (`days_after=0`).

**Why:**
An SMA crossover the day before earnings creates an asymmetric gap risk that the OTO stop-loss cannot protect against. A GTC stop order becomes a market order that executes at the next available price after the open — if a stock gaps down 20% on an earnings miss, the stop fills at the gap-down open price, not at the stop price. The 2% `MAX_POSITION_PCT` risk limit is completely bypassed.

**Why `days_after=0`** (different from RSI's `days_after=2`):
Trend-following specifically captures earnings acceleration. A stock that beats earnings and gaps up is exactly the kind of momentum an SMA crossover is designed to ride. Blocking post-earnings entries would miss the best setups. RSI reversion holds a longer post-earnings window (2 days) because options-unwinding and analyst follow-through noise makes oversold readings unreliable after the event — that concern does not apply to trend-following.

**Data source:** `yfinance` ticker calendar and `earnings_dates`, cached daily per symbol.

**Fail-open:** if `yfinance` is unavailable, the gate returns `True`. Missing earnings data affects one symbol at a time; silent blocking is worse than occasionally entering near earnings.

---

## Default Parameters

| Parameter | Default | Description |
|---|---|---|
| `stock_sma_window` | 200 | SMA period for the stock's own trend check (bars) |
| `vol_short_window` | 10 | Short rolling window for volume expansion (bars) |
| `vol_long_window` | 30 | Long rolling window for volume expansion (bars) |
| `days_before` | 2 | Earnings blackout: calendar days before announcement |
| `days_after` | 0 | Earnings blackout: days after (0 = allow immediately) |

All parameters can be overridden at construction time:

```python
edge = SMAEdgeFilter(
    stock_sma_window=150,
    vol_short_window=5,
    vol_long_window=20,
    days_before=3,   # more conservative window
)
```

---

## Observability

Every filter decision on the most recent bar is logged. No silent passes or blocks.

**Entry allowed:**
```
DEBUG | SMAEdgeFilter: ALLOWED MU — stock>200SMA vol_expanding no_earnings_blackout
```

**Entry blocked** (all failing gates listed):
```
INFO | SMAEdgeFilter: BLOCKED NVDA — stock 118.32 ≤ SMA200 124.57
INFO | SMAEdgeFilter: BLOCKED WDC — volume contracting (med10 ≤ med30)
INFO | SMAEdgeFilter: BLOCKED DELL — earnings blackout (gap-risk protection)
INFO | SMAEdgeFilter: BLOCKED CIEN — stock 42.10 ≤ SMA200 45.88, volume contracting (med10 ≤ med30)
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

### Earnings blackout — pre-earnings only, post-earnings open

The SMA filter blocks new entries up to 2 days **before** earnings but allows entries immediately **after** (`days_after=0`). This is a narrower window than RSI's 3/2 split and it is asymmetric by design.

**The pre-earnings block** guards against gap risk on a new position. An OTO GTC stop-loss becomes a market order at the open after an overnight gap — a 20% earnings miss would bypass `MAX_POSITION_PCT` entirely. This is not a theoretical concern; it is a guaranteed bad trade on any significant miss when the position is entered the session before.

**The post-earnings open** preserves the core SMA edge. Earnings beats that accelerate trends are exactly the entries this strategy is designed to capture. A blackout after earnings would systematically miss the highest-momentum setups.

**Why different from RSI (days_after=2):** RSI reversion enters on oversold dips. Post-earnings, options unwinding and analyst price-target revisions create follow-through that makes oversold readings unreliable for 2 days — buying an "oversold" stock that is down 15% because of a bad earnings report is a knife-catch, not a reversion. This noise does not affect trend-following.

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
