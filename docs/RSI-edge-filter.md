# RSI Edge Filter — Implementation Reference

> Documents the **as-built** `RSIEdgeFilter` in `strategies/filters/rsi_reversion.py`.
> This is not a design proposal — it reflects what is running in paper trading today.

---

## Purpose

The RSI mean-reversion strategy buys oversold dips. Without entry gates, it fires in conditions where reversion has no statistical edge: broken markets, broken individual stocks, pre-earnings binary events, and illiquid names where limit orders fill poorly. The `RSIEdgeFilter` removes those conditions without touching the strategy's core signal logic.

**Core principle:** don't make RSI smarter — remove situations where RSI reliably fails.

---

## Architecture

```
raw RSI entry signal
        │
        ▼
  RSIEdgeFilter.__call__(df)
        │  AND-gates 4 boolean Series
        ▼
filtered entry signal → Risk Manager → Execution
```

- `RSIReversion._raw_signals(df)` detects the unfiltered RSI threshold crossing
- `BaseStrategy.generate_signals(df)` AND-gates entries with the edge filter output
- **Exits are never blocked** — enforced unconditionally by `BaseStrategy`
- `BaseStrategy.generate_signals` calls `filter.set_symbol(symbol)` before `filter(df)` so the filter knows which symbol it is evaluating

The filter lives at `strategies/filters/rsi_reversion.py`. Shared building blocks (`SPYTrendFilter`, `EarningsBlackout`) live in `strategies/filters/common.py`.

---

## Gates

All four gates must be `True` on a given bar for an entry to be allowed on that bar. Exits are never evaluated against the filter.

### Gate 1 — SPY Macro Trend (mandatory)

**Rule:** `SPY close > SPY 200-day SMA` AND `SPY close > SPY 50-day SMA`

**Implemented by:** `SPYTrendFilter(sma_windows=[200, 50])` from `strategies/filters/common.py`

**Why both SMAs:**
- 200 SMA: structural bear market gate — RSI reversion into a broad bear is catching a falling knife, not a mean-reversion setup
- 50 SMA: intermediate downtrend gate — even in a long-term bull, a SPY below its 50 SMA signals deteriorating momentum where individual oversold setups are more likely to extend than revert

**SPY data:** fetched once per engine cycle with a 600-second TTL cache. All symbols in one cycle share the same SPY fetch — no repeated API calls.

**Fail-open:** if SPY data is unavailable (network error, API outage), the gate returns `True` and logs a WARNING. The operator sees the failure; the bot does not silently block all trades.

**Note:** This gate provides a second BEAR block on top of the `RegimeDetector`'s BEAR classification. The redundancy is intentional — the regime detector has a TTL-cached SPY fetch with a longer staleness window. The filter's SPY check is more current and is also the correct home for the RSI-specific 50 SMA gate (which the regime detector does not apply to SMA entries).

---

### Gate 2 — Earnings Blackout

**Rule:** No entry within 3 calendar days **before** or 2 calendar days **after** a known earnings date.

**Implemented by:** `EarningsBlackout(days_before=3, days_after=2)` from `strategies/filters/common.py`

**Why:**
RSI reversion buys oversold dips. A dip into an upcoming earnings announcement is binary event risk — the stock may gap down 15% or gap up 15% on the report. The expected reversion does not materialize; the fill is at an entry price that may be wildly wrong post-announcement. 

Post-earnings (2 days after): options unwinding, analyst price-target updates, and institutional rebalancing create follow-through that makes the "oversold" signal unreliable. After 2 days, these effects have largely settled.

**Data source:** `yfinance` ticker calendar and `earnings_dates`. Cached daily per symbol (one lookup per symbol per trading day).

**Fail-open:** if `yfinance` is unavailable or returns no dates for a symbol, the gate returns `True` and logs a WARNING. Missing earnings data does not silently block trades.

---

### Gate 3 — Minimum Liquidity

**Rule:** 20-day rolling average volume ≥ 500,000 shares

**Why:**
RSI reversion enters via LIMIT orders. Thinly traded stocks fill partially at the limit price (or not at all), leaving the bot with a partial position at an unfavorable price while the stop is sized for a full position. The edge on paper disappears in practice because fill quality degrades below this liquidity floor.

This is a hard structural floor, not a directional signal. It is evaluated against the historical bars already fetched for the symbol — no additional API call.

**Fail-open:** if the `volume` column is absent or there are fewer than 20 bars of history, the gate returns `True`. Insufficient data is not treated as a rejection signal.

---

### Gate 4 — No Active Breakdown

**Rule:** Current `close > min(close)` of the prior 20 bars (not including the current bar)

**Why:**
A stock making new 20-day lows is in active breakdown. Each lower low looks like an RSI oversold setup — it generates a raw entry signal — but each one is a knife-catch. The price action is dominated by forced selling, structural deterioration, or bad news that the SPY macro gates cannot see (the broad market may be fine while this individual stock collapses).

This gate blocks those entries without penalising normal pullbacks, which is what the strategy is designed to capture. The SPY > 50 SMA gate guards the macro environment; this gate guards individual stock breakdown.

**Why not the 50-day SMA:** RSI oversold stocks are typically below their 50-day SMA — that is exactly the population the strategy targets. Filtering on `close < 50 SMA` would remove most valid setups. The new-low gate addresses the same concern more precisely: it distinguishes "temporarily below 50 SMA" (normal pullback, reversion candidate) from "making new 20-day lows" (active breakdown, knife-catch).

**Fail-open:** if there are fewer than 21 bars of history (20 for rolling minimum + 1 for shift), the gate returns `True`.

---

## Default Parameters

| Parameter | Default | Description |
|---|---|---|
| `spy_lookback_days` | 280 | Calendar days of SPY history to fetch (covers 200 trading days) |
| `spy_cache_ttl` | 600.0 s | SPY cache TTL — reused across all symbols in one cycle |
| `days_before` | 3 | Earnings blackout: calendar days before announcement |
| `days_after` | 2 | Earnings blackout: calendar days after announcement |
| `vol_min_window` | 20 | Rolling window for average volume calculation (bars) |
| `vol_min_avg` | 500,000 | Minimum average daily share volume |
| `new_low_window` | 20 | Look-back window for breakdown detection (bars) |

All parameters can be overridden at construction time:

```python
edge = RSIEdgeFilter(
    days_before=5,
    vol_min_avg=1_000_000,
)
```

---

## Observability

Every filter decision on the most recent bar is logged. No silent passes or blocks.

**Entry allowed:**
```
INFO | RSI_FILTER_ALLOWED ALLY — SPY=True earnings=True liquid=True no_new_low=True
```

**Entry blocked** (one or more gates failed, all failing gates listed):
```
INFO | RSI_FILTER_BLOCKED CDNS — SPY trend gate failed (below 200 or 50 SMA)
INFO | RSI_FILTER_BLOCKED CCK — earnings blackout
INFO | RSI_FILTER_BLOCKED SN — volume illiquid (avg20=312,450 < 500,000)
INFO | RSI_FILTER_BLOCKED TFC — new 20-day low (active breakdown)
```

Multiple reasons are comma-separated in a single log line if more than one gate fails simultaneously.

---

## Design Decisions and Exclusions

### Stock 50-day SMA gate — intentionally excluded

**Why it was considered:** a stock below its 50 SMA has weakening momentum.

**Why it was removed:** RSI oversold stocks are almost always below their 50 SMA. This was the population the strategy is designed to trade. Including this gate was observed to filter out the majority of valid setups. The new-low gate (Gate 4) addresses the legitimate concern — active breakdown — without penalising normal mean-reversion candidates.

### Earnings blackout on SMA Crossover — intentionally absent

Earnings blackout belongs on RSI, not SMA. Trend-following strategies benefit from earnings catalysts that accelerate established trends. Mean-reversion strategies are hurt by binary events that can gap a stock far beyond any sensible stop before the trade has a chance to work.

### SPY volatility filter (ATR%) — deferred to Phase 11

High volatility environments are handled at the engine level by the `RegimeDetector` VOLATILE regime gate, which blocks new entries for all strategies. A redundant ATR% check in this filter would be duplication. If the regime detector is ever removed, revisit.

### Cliff-edge SPY 50 SMA gate — noted for Phase 11

The current gate is a hard binary: SPY crosses the 50 SMA on one bar → all RSI entries blocked on the next cycle. A brief SPY dip below the 50 SMA on a single day can trigger a lockout during a valid paper run. A smoother N-bar confirmation window (require SPY below 50 SMA for ≥ 3 consecutive bars) would reduce false lockouts. Deferred: hard gates are operationally auditable; the smooth version requires forward-test data to validate the threshold.

---

## Integration

```python
# forward_test.py
from strategies.filters.rsi_reversion import RSIEdgeFilter
from strategies.rsi_reversion import RSIReversion

slot = StrategySlot(
    strategy=RSIReversion(
        period=14,
        oversold=30,
        overbought=70,
        edge_filter=RSIEdgeFilter(),
    ),
    watchlist_source=StaticWatchlistSource(settings.RSI_WATCHLIST, name="rsi"),
    allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
)
```

The `allowed_regimes` on the `StrategySlot` provides the engine-level BEAR and VOLATILE block. The `RSIEdgeFilter` provides the symbol-level and SPY-level gates within allowed regimes.

---

## Phase 11 Deferred Items

| Item | Description |
|---|---|
| 11.23 | SPY 50 SMA cliff-edge smoothing — N-bar confirmation before engaging the block |
| 11.25 | VIX integration — high VIX is actually favourable for RSI reversion (sharp oversold snaps); could relax VOLATILE block selectively for RSI when VIX is elevated |
