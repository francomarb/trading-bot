# Strategies

This document catalogues every strategy in the bot, its signal logic, default parameters, and current lifecycle status.

---

## Strategy Lifecycle

| Stage | Meaning |
|---|---|
| **Development** | Code written, not yet backtested |
| **Backtesting** | Backtested with vectorbt, tuning parameters |
| **Paper Trading** | Running live on Alpaca paper account |
| **Live** | Deployed with real capital |
| **Retired** | Disabled after failing go/no-go or underperforming |

---

## Active Strategies

### SMA Crossover

| Field | Value |
|---|---|
| File | `strategies/sma_crossover.py` |
| Class | `SMACrossover` |
| Type | Trend-following |
| Order type | Market |
| Status | **Paper Trading** |

**Signal logic:**
- **Entry:** Fast SMA crosses *above* slow SMA (bullish crossover)
- **Exit:** Fast SMA crosses *below* slow SMA (bearish crossunder)

Both signals require a confirmed crossover — the previous bar must have been on the opposite side. Bars where either SMA is NaN (early warmup period) produce no signal.

**Default parameters:**

| Parameter | Default | Description |
|---|---|---|
| `fast` | 20 | Fast SMA window (bars) |
| `slow` | 50 | Slow SMA window (bars) |

**Required bars:** `slow` (e.g. 50 with defaults)

**Why this strategy:**
SMA crossover is the simplest trend-following signal. It captures sustained directional moves and naturally avoids counter-trend entries. It underperforms in sideways/choppy markets, which is why it's paired with RSI Reversion for regime diversification.

---

### RSI Mean Reversion

| Field | Value |
|---|---|
| File | `strategies/rsi_reversion.py` |
| Class | `RSIReversion` |
| Type | Mean-reversion |
| Order type | Limit |
| Status | **Implemented — not yet active** (pending SMA crossover go/no-go) |

**Signal logic:**
- **Entry:** RSI crosses *below* the oversold threshold (fading a sell-off)
- **Exit:** RSI crosses *above* the overbought threshold (taking profit on reversion)

Both signals require a confirmed threshold crossing — the previous bar's RSI must have been on the opposite side. Bars where RSI is NaN (early warmup period) produce no signal.

**Default parameters:**

| Parameter | Default | Description |
|---|---|---|
| `period` | 14 | RSI lookback period (bars) |
| `oversold` | 30 | Entry threshold (RSI below this) |
| `overbought` | 70 | Exit threshold (RSI above this) |

**Required bars:** `period + 1` (e.g. 15 with defaults)

**Why this strategy:**
RSI mean reversion profits when prices snap back from extremes. It performs well in ranging/sideways markets where SMA crossover suffers, providing natural regime diversification when both strategies run simultaneously.

---

## Strategy Diversification

Running SMA Crossover and RSI Reversion together provides complementary coverage:

| Market Regime | SMA Crossover | RSI Reversion |
|---|---|---|
| Strong trend | Profitable | Few signals (RSI rarely hits extremes) |
| Sideways/range | Whipsawed (false crossovers) | Profitable (buys dips, sells rips) |
| Volatile crash | Late entry, but captures recovery | Early entry on oversold bounce |

When RSI Reversion is activated, the engine will run both strategies in separate `StrategySlot` instances with independent symbol universes, sharing a single risk manager and equity pool. Currently only SMA Crossover is active.

---

## Planned Strategies

No additional strategies are currently planned. The go/no-go framework (`scripts/gonogo.py`) must confirm the existing two-strategy setup meets all thresholds before adding complexity.

---

## Adding a New Strategy

See the checklist in [`architecture.md`](architecture.md#adding-a-new-strategy--checklist).
