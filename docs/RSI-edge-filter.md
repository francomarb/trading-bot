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

### Gate 1 — SPY Intermediate Trend (mandatory)

**Rule:** `SPY close >= SPY 50-day SMA * 0.99`

**Implemented by:** `SPYTrendFilter(sma_windows=[50], sma_tolerance_pct=settings.RSI_SPY50_TOLERANCE_PCT)` from `strategies/filters/common.py`

**Why this SMA:**
- 50 SMA with a 1% band: intermediate downtrend gate — even outside a structural bear market, a SPY materially below its 50 SMA signals deteriorating momentum where individual oversold setups are more likely to extend than revert
- The 1% tolerance avoids starving RSI when SPY is only fractionally below its 50 SMA, where SIP backtests showed the hard cutoff rejected high-value mean-reversion entries
- 200 SMA: intentionally delegated to the engine-level `RegimeDetector`, which blocks RSI entries in `BEAR` regimes

**SPY data:** fetched once per engine cycle with a 600-second TTL cache. All symbols in one cycle share the same SPY fetch — no repeated API calls. The default 320-calendar-day lookback is retained as a conservative buffer.

**Failure behaviour:**

| Situation | Behaviour | Rationale |
|---|---|---|
| Cold-start: no prior cache exists | **Fail closed** — block all entries, log ERROR | No SPY data at all during a potential crash is the highest-risk scenario. Deploying the entire RSI sleeve into a collapsing market because the data API is down is unacceptable. |
| Warm: prior cache exists, fetch failed | **Use stale cache**, log WARNING | Last known SPY state (at most one TTL interval old) is a reasonable proxy during brief outages. Daily SMA values do not change materially in 10 minutes. |
| Fewer bars than any configured SMA requires | **Fail closed** — block all entries and report available/required bars | A mandatory macro gate must not silently disappear because a calendar lookback contains fewer trading sessions than expected. |

The TTL is 600 seconds. After a failed fetch `cache_time` is still advanced, so the API is retried no more than once per TTL interval rather than every cycle.

**Note:** This gate does not duplicate the structural `SPY > 200 SMA` check. That BEAR-market veto is owned by `RegimeDetector` at the strategy-slot level. The RSI filter only keeps the RSI-specific 50 SMA confirmation band, which the regime detector does not apply to other equity strategies.

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

**Rule:** 20-day rolling average dollar volume ≥ $10,000,000

**Why:**
RSI reversion enters via LIMIT orders. Thinly traded stocks fill partially at the limit price (or not at all), leaving the bot with a partial position at an unfavorable price while the stop is sized for a full position. The edge on paper disappears in practice because fill quality degrades below this liquidity floor.

This is a hard structural floor, not a directional signal. It is evaluated against the historical bars already fetched for the symbol — no additional API call.

**Fail-open:** if the `volume` column is absent or there are fewer than 20 bars of history, the gate returns `True`. Insufficient data is not treated as a rejection signal.

---

### Gate 4 — No Active Breakdown

**Rule:** Block only when both are true:
- Current `close <= min(close)` of the prior 20 bars, not including the current bar
- Current `close < 200-day SMA`

**Why:**
A stock making new 20-day lows below its 200-day trend is in active breakdown. Each lower low looks like an RSI oversold setup — it generates a raw entry signal — but each one is a knife-catch. The price action is dominated by forced selling, structural deterioration, or bad news that the SPY macro gates cannot see.

This gate blocks those entries without penalising normal pullbacks above the stock's long-term trend, which is what the strategy is designed to capture. The SPY 50 SMA band guards the macro environment; this gate guards individual stock breakdown.

**Why not the 50-day SMA:** RSI oversold stocks are typically below their 50-day SMA — that is exactly the population the strategy targets. Filtering on `close < 50 SMA` would remove most valid setups. The active-breakdown gate addresses the same concern more precisely: it distinguishes "temporarily below 50 SMA" (normal pullback, reversion candidate) from "making new 20-day lows below the 200-day trend" (active breakdown, knife-catch).

**Fail-open with warning:** if there is not enough history to compute either the
prior-low window or the 200-day SMA on the latest bar, the gate returns `True`
and logs `RSI_FILTER_WARN ... active-breakdown gate failed open`. This preserves
eligibility for recent IPOs or temporary data gaps while making the weakened
protection visible to the operator.

---

## Default Parameters

| Parameter | Default | Description |
|---|---|---|
| `spy_lookback_days` | 320 | Calendar days of SPY history to fetch; retained as a conservative cache window |
| `spy_cache_ttl` | 600.0 s | SPY cache TTL — reused across all symbols in one cycle |
| `settings.RSI_SPY50_TOLERANCE_PCT` | 0.01 | SPY may be up to 1% below its 50 SMA |
| `days_before` | 3 | Earnings blackout: calendar days before announcement |
| `days_after` | 2 | Earnings blackout: calendar days after announcement |
| `vol_min_window` | 20 | Rolling window for average volume calculation (bars) |
| `notional_min_avg` | 10,000,000 | Minimum 20-day average daily dollar volume |
| `new_low_window` | 20 | Short-term low window for breakdown detection (bars) |

All parameters can be overridden at construction time:

```python
edge = RSIEdgeFilter(
    days_before=5,
    notional_min_avg=20_000_000,
)
```

---

## Observability

Every filter decision on the most recent bar is logged. No silent passes or blocks.

**Entry allowed:**
```
INFO | RSI_FILTER_ALLOWED ALLY — SPY=True earnings=True liquid=True no_active_breakdown=True
```

**Entry blocked** (one or more gates failed, all failing gates listed):
```
INFO | RSI_FILTER_BLOCKED CDNS — SPY trend gate failed: SPY 640.00 ≤ SMA50 tolerance floor 648.45 (SMA 655.00, tolerance 1.0%)
INFO | RSI_FILTER_BLOCKED CCK — earnings blackout
INFO | RSI_FILTER_BLOCKED SN — liquidity too low (avg_dollar_vol20=$8,500,000 < $10,000,000)
INFO | RSI_FILTER_BLOCKED TFC — new 20-day low below 200 SMA (active breakdown)
```

Multiple reasons are comma-separated in a single log line if more than one gate fails simultaneously.

---

## Design Decisions and Exclusions

### Stock 50-day SMA gate — intentionally excluded

**Why it was considered:** a stock below its 50 SMA has weakening momentum.

**Why it was removed:** RSI oversold stocks are almost always below their 50 SMA. This was the population the strategy is designed to trade. Including this gate was observed to filter out the majority of valid setups. The active-breakdown gate (Gate 4) addresses the legitimate concern — active breakdown — without penalising normal mean-reversion candidates.

### Earnings blackout on SMA Crossover — intentionally absent

Earnings blackout belongs on RSI, not SMA. Trend-following strategies benefit from earnings catalysts that accelerate established trends. Mean-reversion strategies are hurt by binary events that can gap a stock far beyond any sensible stop before the trade has a chance to work.

### SPY volatility filter (ATR%) — deferred to Phase 11

High volatility environments are handled at the engine level by the `RegimeDetector` VOLATILE regime gate, which blocks new entries for all strategies. A redundant ATR% check in this filter would be duplication. If the regime detector is ever removed, revisit.

### SPY 50 SMA band — paper-watch after loosening

The prior hard gate blocked all RSI entries as soon as SPY crossed below the 50 SMA. SIP backtests showed that a 1% tolerance band materially improved RSI participation and return, at the cost of higher drawdown. This looser gate should be paper-watched before considering any further SPY50 smoothing or removal.

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
| 11.23 | SPY 50 SMA band paper-watch — evaluate the looser 1% threshold before considering any further smoothing or removal |
| 11.25 | VIX integration — high VIX is actually favourable for RSI reversion (sharp oversold snaps); could relax VOLATILE block selectively for RSI when VIX is elevated |
