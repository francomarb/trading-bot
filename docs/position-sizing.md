# Position Sizing Design

## Purpose

Define a consistent, scalable approach to position sizing for the trading bot that:

- Works across stocks with very different prices
- Supports small and large accounts
- Avoids bias toward low-priced stocks
- Enables clean capital allocation across strategies
- Aligns with fractional trading capabilities

---

## 🧠 The Root Problem

Stock prices vary significantly:

- Some stocks trade at $20–$50
- Others trade at $150–$300
- Others exceed $500–$1000+

With small capital, e.g. $2K, this creates issues if using **whole-share sizing**.

---

## ❌ Problem with Share-Based Sizing

Example:

```text
shares = 1
```

Results:

| Symbol Type | Price | Exposure |
|---|---:|---:|
| Low-priced stock | $30 | $30 |
| High-priced stock | $900 | $900 |

Consequences:

- Inconsistent risk per trade
- Expensive stocks dominate capital
- Cheap stocks are under-allocated
- Portfolio becomes distorted
- Strategies behave unpredictably

---

## ⚠️ Naive Solution (Not Recommended)

Naive approach:

> Use fractional shares only when price is too high.

Problems:

- Introduces branching logic
- Creates inconsistent behavior
- Is harder to debug
- Still leads to uneven exposure

---

## ✅ Correct Solution: Notional-Based Sizing

Always size positions in dollars, not shares.

Core rule:

```text
position_size = fixed dollar amount, or % of capital
shares = position_size / price
```

---

## 🎯 Example

Capital per trade: $200

| Symbol | Price | Shares |
|---|---:|---:|
| NVDA | $900 | 0.22 |
| AAPL | $180 | 1.11 |
| BAC | $35 | 5.70 |

Same capital means consistent risk.

---

## ⚙️ Implementation

### Step 1 — Define Position Size

Examples:

- SMA: $300 per trade
- RSI: $100–$150 per trade

### Step 2 — Convert to Shares

```text
shares = position_size / price
```

### Step 3 — Handle Fractional Support

If broker supports fractional trading:

```text
shares = position_size / price
```

Broker caveat: Alpaca supports fractional equity orders only with `DAY` time in
force. Any future fractional execution path cannot assume `GTC` entry or
protective stop orders.

If not:

```text
shares = int(position_size / price)
```

### Step 4 — Minimum Trade Guard

```python
if shares * price < MIN_NOTIONAL:
    skip_trade
```

Recommended:

```text
MIN_NOTIONAL = $50-$100
```

---

## 🔄 Optional Optimization

If fractional shares are supported:

```python
if shares >= 1:
    shares = round(shares, 2)  # optional
```

This keeps:

- Precision
- Cleaner logs

---

## 📊 Interaction with Capital Allocation

Position sizing must respect:

- Strategy-level capital buckets
- Max open positions per strategy
- Portfolio-level exposure caps

Example:

```text
Total capital = $2000

SMA allocation = 60% → $1200
RSI allocation = 40% → $800
```

Each strategy sizes positions within its own budget.

---

## ⚠️ Edge Cases

### 1. Extremely High-Priced Stocks

- Fractional shares required
- Otherwise skipped

### 2. Very Low-Priced Stocks

- May create large share counts
- Ensure liquidity filter exists

### 3. Rounding Issues

- Ensure precision handling
- Avoid float errors in logs

---

## 📈 Benefits

Using notional-based sizing:

- Provides consistent risk per trade
- Gives fair exposure across symbols
- Works with any capital size
- Scales as the account grows
- Supports multi-strategy systems

---

## 🧠 Key Insight

Position sizing is more important than entry logic in early systems.

- A good strategy with bad sizing → poor results
- A simple strategy with good sizing → scalable system

---

## 🚀 Future Enhancements

Later versions may include:

- Volatility-adjusted sizing, e.g. ATR-based
- Dynamic sizing based on strategy performance
- Risk-per-trade sizing, e.g. % of equity
- Kelly criterion, advanced and optional

---

## 🏁 Summary

- Always size positions in dollars
- Convert to shares at execution
- Use fractional shares when available
- Enforce minimum trade size
- Integrate with capital allocation system

---

## 🔥 Final Thought

A capital-consistent system behaves the same regardless of stock price.

That is a prerequisite for a reliable trading bot.
