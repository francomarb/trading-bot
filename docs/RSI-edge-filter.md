# RSI Edge Filter Design (Phase 9.5 → Phase 10 Bridge)

## Purpose

Define a **minimal, robust edge filter** for the RSI mean-reversion strategy that:

- Prevents trading in conditions where RSI is known to fail
- Aligns RSI with broader market trends
- Enables safe multi-strategy (SMA + RSI) paper testing
- Provides strong observability for debugging
- Bridges current system → future meta-signal architecture

This filter is intentionally:

- simple
- deterministic
- auditable

---

## Core Principle

RSI should only operate when:

> **Mean reversion has a statistical edge**

Avoid:

- trending down markets
- structurally weak stocks
- chaotic / high-volatility regimes

---

## Tier 1 — Minimum Viable Filter (REQUIRED)

### Rule 1 — Market Trend Filter (Mandatory)

Only allow RSI trades when the overall market is healthy.

Conditions:

- SPY price > 200-day SMA
- SPY price > 50-day SMA

Purpose:

- Avoid bear markets
- Avoid macro downtrends

---

### Rule 2 — Symbol Trend Filter

Only trade RSI on stocks that are not structurally weak.

Condition:

- Stock price > 50-day SMA

Purpose:

- Avoid buying collapsing stocks
- Align with upward bias

---

### Rule 3 — Long-Only Mode

Disable RSI shorting entirely.

Purpose:

- Avoid fighting strong trends
- Reduce complexity during validation phase

---

## Resulting Entry Logic

RSI BUY is allowed ONLY if:

- SPY > 200 SMA
- SPY > 50 SMA
- Stock > 50 SMA
- RSI < oversold threshold (e.g., 30)

---

## Tier 2 — Optional Additions (After Validation)

### Rule 4 — Volatility Filter

Avoid unstable environments.

Options:

- ATR / price < threshold (e.g., < 3–4%)
- OR VIX below threshold

Purpose:

- Reduce noise-driven trades
- Avoid panic conditions

---

## Tier 3 — Optional Refinements (Later)

### Rule 5 — Momentum Confirmation

Improve entry quality.

Options:

- RSI crossing upward (not just below threshold)
- Price above short-term SMA (e.g., 10-day)

Purpose:

- Avoid premature entries

---

## Example Edge Filter Function

```python
def rsi_edge_filter(symbol, market_data):
    spy = market_data["SPY"]
    stock = market_data[symbol]

    # Market filter
    if spy.price < spy.sma200:
        return False
    if spy.price < spy.sma50:
        return False

    # Symbol filter
    if stock.price < stock.sma50:
        return False

    return True
```

---

## Integration with Architecture

Flow:

```text
RSI signal → Edge Filter → Risk Engine → Execution
```

Important:

- Strategy logic remains untouched
- Filter acts as a gate
- Future `MetaSignal` layer can replace this

---

## Capital Allocation Interaction

RSI must respect:

- Strategy-level capital bucket
- Max open positions per strategy
- Portfolio-level exposure caps

The filter reduces:

- Unnecessary capital usage
- Low-quality trades

---

## Success Criteria

### Behavioral Success (Primary)

- RSI trades only occur in strong/uptrending markets
- No trades in clear downtrends
- Trades align with SMA direction, with no conflict
- Reduced frequency of bad entries

### System-Level Success

- No increase in errors or warnings
- No duplicate orders
- No ownership confusion
- Clean integration with existing logs

### Comparative Success

Compared to no filter:

- Fewer trades
- Higher trade quality
- Lower drawdowns
- More consistent behavior

---

## Observability (Critical)

The filter must be fully observable.

### Required Logs

For every RSI signal:

- `RSI_SIGNAL_DETECTED`
- `RSI_FILTER_ALLOWED` or `RSI_FILTER_BLOCKED`
- Reason for block:
  - Market trend failed
  - Symbol trend failed
  - Volatility filter, if enabled

### Example Log

```text
RSI signal detected: AAPL RSI=28
RSI filter blocked: AAPL — price below 50 SMA
```

### Metrics to Track

- Total RSI signals
- Allowed vs blocked ratio
- Reasons for filtering
- Average holding time
- Win/loss ratio, later stage

---

## Testing Strategy

### Phase 1 — Log-Only Mode

- Generate RSI signals
- Apply filter
- Do not execute trades
- Validate behavior vs charts

### Phase 2 — Limited Trading

- Enable RSI with small allocation, e.g. 20–30%
- Monitor trade quality
- Monitor system stability
- Monitor interaction with SMA

### Phase 3 — Full Paper Parallel

- SMA + RSI both active
- Validate ownership correctness
- Validate capital allocation
- Validate no conflicts

---

## Failure Modes

### Known Risks

- Over-filtering → no trades
- Under-filtering → poor trades
- Missing SPY data → false negatives
- Data lag → incorrect filtering

### Critical Stop Conditions

- RSI trading below 50 SMA
- RSI firing excessively in chop
- Capital overuse
- Strategy conflicts with SMA

---

## Rollout Plan

1. Implement filter logic
2. Run in log-only mode
3. Validate logs vs charts
4. Enable small capital allocation
5. Monitor behavior
6. Expand gradually

---

## Design Philosophy

This filter is:

- Simple
- Explainable
- Testable
- Aligned with architecture

It is not:

- Predictive
- Adaptive
- Sentiment-driven

---

## Future Evolution

This filter will evolve into:

- Phase 10+ future work
- `MetaSignalState`
- Sentiment gating
- Volatility-aware regimes
- Dynamic capital allocation

### Long-Term Vision

```text
Strategy → MetaSignal → Position Expression → Execution
```

Where:

- RSI/SMA generate opportunities
- Meta layer decides validity
- Execution chooses instrument, equity or options

---

## Key Insight

The goal is not to make RSI smarter.

It is to remove situations where RSI fails.

---

## Sample Core Watchlist for RSI

- AAPL
- MSFT
- GOOGL
- AMZN
- META
- NVDA
- AVGO
- AMD
- MU
- TSLA
- QQQ
- SMH
