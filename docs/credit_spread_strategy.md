# Credit Spread — Strategy Research & Deployment Guide

**Status:** ✅ **ACTIVE** — wired in `forward_test.py` since PLAN 11.29.
Running on **SPY + QQQ** in paper mode for several weeks as of this
update; allocated 10% of equity in the isolated-options pool.

**Last updated:** 2026-06-06

**Strategy ID:** `credit_spread` (instances run per underlying:
`credit_spread_spy`, `credit_spread_qqq`).

> **Document structure.** This doc was originally written as a *design
> proposal* before the strategy existed. The original proposal text is
> preserved below (sections 1–15) as the rationale and research record —
> it explains *why* the strategy looks the way it does. The
> **Deployment configuration** section immediately below this header
> captures the *live* parameters as of the last-updated date. When the
> two disagree, settings.py and `forward_test.py` are the source of
> truth; the proposal below is research, not spec.

---

## Deployment configuration (live)

### Sleeve mechanics

| Parameter | Value | Source |
|---|---|---|
| Pool type | Isolated options (defined-risk, never stretches) | `settings.STRATEGY_ALLOCATIONS["credit_spread"]` |
| Sleeve weight | 0.10 of equity (target) | same |
| Shared max concurrent positions | 8 across all underlyings | `settings.MAX_TOTAL_CONCURRENT_CREDIT_SPREADS` |
| Regime gate | `TRENDING`, `RANGING` only | `settings.STRATEGY_ALLOWED_REGIMES["credit_spread"]` |
| Sleeve budget pct | 0.10 | `settings.CREDIT_SPREAD_SLEEVE_BUDGET_PCT` |
| Min trades for health verdict | 25 | `settings.STRATEGY_MIN_TRADES_FOR_VERDICT` |
| Instruments | SPY, QQQ | `settings.CREDIT_SPREAD_INSTRUMENTS` |

The sleeve is **shared** across all instances: SPY + QQQ draw from the
same 10% budget rather than each getting their own. This is by design
— credit spreads on SPY and QQQ are highly correlated (QQQ tracks SPX
closely), and isolating each underlying would understate the cluster
risk.

The 0.10 sleeve was carved from the existing equity weights when credit
spread was added (PLAN 11.29): SMA 0.45 → 0.40 and RSI 0.25 → 0.20.
SPY Options Reversion (0.05) and Donchian (0.25) were not touched.

### Per-instrument config (current `CREDIT_SPREAD_INSTRUMENTS`)

Both SPY and QQQ share identical entry/exit logic, differing only in
`spread_width` (SPY uses $10-wide strikes; QQQ uses $15-wide to
compensate for the higher underlying price).

| Parameter | SPY | QQQ | Notes |
|---|---|---|---|
| `short_leg_delta` | 0.17 | 0.17 | Sell strike at ~17Δ — well OTM bull put |
| `spread_width` | 10 | 15 | Long strike = short strike − width |
| `dte_min` | 30 | 30 | Earliest entry expiry |
| `dte_max` | 45 | 45 | Latest entry expiry |
| `iv_proxy_source` | `vix` | `vix` | QQQ tracks SPX vol closely enough |
| `min_iv_proxy` | 14 | 14 | VIX must be ≥ 14 for entry (premium floor) |
| `min_credit_pct_of_width` | 0.13 | 0.13 | Credit ≥ 13% of spread width |
| `max_concurrent_positions` | 3 | 3 | Per-instance cap |
| `max_per_expiration` | 1 | 1 | One spread per expiry, per underlying |
| `min_dte_gap_between_opens` | 7 | 7 | Stagger entries across calendar |
| `profit_target_pct` | 0.50 | 0.50 | Close at 50% of max profit |
| `stop_loss_multiple` | 2.0 | 2.0 | Stop at 2× initial credit received |
| `time_stop_dte` | 21 | 21 | Force close inside 21 DTE |
| `exit_on_short_strike_breach` | True | True | Close immediately if short strike goes ITM |
| `limit_timeout_seconds` | 30 | 30 | Cancel-and-retry stale entry limits |
| `earnings_blackout_days` | 0 | 0 | ETFs — no earnings (single-name overrides exist) |

**Validation enforced at import time:** every block in
`CREDIT_SPREAD_INSTRUMENTS` must define all of `_REQUIRED_CREDIT_SPREAD_KEYS`;
extra keys raise on settings load. `STRATEGY_WATCHLISTS["credit_spread"]`
must match the instrument-block keys. This catches drift between the
two sources.

### Wiring (`forward_test.py:291-318`)

```python
_cs_quote_lookup = build_opra_quote_lookup()
for _cs_symbol in settings.STRATEGY_WATCHLISTS["credit_spread"]:
    _cs_config = CreditSpreadConfig.from_dict(
        _cs_symbol, settings.CREDIT_SPREAD_INSTRUMENTS[_cs_symbol]
    )
    slots.append(StrategySlot(
        strategy=CreditSpread(
            _cs_config,
            edge_filter=CreditSpreadEdgeFilter(
                iv_proxy_source=_cs_config.iv_proxy_source,
                min_iv_proxy=_cs_config.min_iv_proxy,
                earnings_blackout_days=_cs_config.earnings_blackout_days,
                iv_resolver=_iv_resolver,
            ),
            iv_resolver=_iv_resolver,
            quote_lookup=_cs_quote_lookup,
        ),
        watchlist_source=StaticWatchlistSource(
            [_cs_symbol], name=f"credit_spread_{_cs_symbol.lower()}"
        ),
        allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
    ))
```

The strategy is constructed **once per underlying** and they share the
same `_iv_resolver` (PLAN 11.46 — single VIX cache) and one OPRA quote
lookup. The allocator routes them through a single shared sleeve.

### Implementation files (live)

- `strategies/credit_spread.py` — `CreditSpread` strategy + `CreditSpreadConfig`.
- `strategies/filters/credit_spread.py` — `CreditSpreadEdgeFilter`.
- `utils/options_ranker.py` — two-leg composite-score ranker (extended from 11.25).
- `execution/options_executor.py` — async bracket / MLEG order worker.
- `engine/trader.py` — `dispatch_spread_order`, `SpreadExecutionWorker`,
  spread-aware position model, startup spread reconstruction.
- `config/settings.py` — `CREDIT_SPREAD_INSTRUMENTS`, sleeve config,
  validation.
- `forward_test.py` — slot wiring (lines 291–318).

### Operational status

- Paper-trading on SPY + QQQ since PLAN 11.29 landed.
- Allocated 10% of equity in the isolated-options pool (shared sleeve).
- Health monitor floor: 25 trades for `CONCLUSIVE` verdict; the sleeve
  takes a while to accumulate that many trades because each instance is
  capped at 3 concurrent and per-expiration entries are throttled.
- Watch items: (1) per-side fill quality on the two-leg combo, (2)
  realized credit-to-width ratio vs. the 0.13 floor, (3) frequency of
  short-strike-breach exits in volatile sessions, (4) walk-and-market
  close-fill distribution (see *Close-walk tuning review* below).

### Close path — walk-and-market

The 2026-06-08 PR replaced the single-shot limit-at-mid close with a
generic walk-and-market scheduler. The strategy now emits a typed
`MlegCloseDecision` from `evaluate_close()`; the engine resolves a
profile, applies the EOS bypass, builds an `MlegCloseScheduler`, and
hands it to `SpreadExecutionWorker`. The worker walks the limit through
several escalating prices (mid → walk steps → ask), and falls back to
market as the autonomous final step.

See [`mleg_close_design.md`](mleg_close_design.md) for the full design.

Live mapping of credit-spread triggers → typed reasons:

| Internal trigger | Typed reason | Behaviour |
|---|---|---|
| profit-target hit | `profit_target` | 3 walk steps, no market fallback (winners cancel and retry) |
| stop-loss hit | `stop_loss` | 5 walk steps + market fallback |
| time-stop (DTE ≤ `time_stop_dte`) | `time_stop` | 4 walk steps + market fallback |
| short-strike breach | `defensive_breach` | 2 walk steps + market fallback (defensive) |
| BEAR regime override | `defensive_breach` | Market-only (no walk) |

### Close-walk tuning review

After ~10–20 paper closes accumulate (4–8 weeks of live paper running),
review the walk-step fill distribution. The data lives in
`logs/bot.jsonl` as structured `mleg_walk_step` events; grep them out
with:

```bash
jq -c 'select(.record.extra.event == "mleg_walk_step")' \
    logs/bot.jsonl
```

Decision matrix:

| Pattern | Verdict |
|---|---|
| >60% fills at the market step | Walk isn't catching fills — tune more aggressive starting point |
| >60% fills at step 1 (mid) | Walk over-engineered for this universe — consider simplifying |
| Steps 2–4 capture most fills | Walking is doing what it should — leave alone |
| Avg fill price within $0.05 of starting ask | Walk isn't generating meaningful value |
| Avg fill price meaningfully below starting ask | Walk is paying for itself |

The defaults shipped in `settings.MLEG_CLOSE_PROFILES` are 30-second
steps with 4–5 patient escalations before market. Tuned for residential
network latency and the actual Alpaca paper-API roundtrip, not low-
latency colocated rails.

### Known doc gaps vs. live implementation

The proposal below was written before the engine acquired the MLEG path
and the spread-aware position model. Some §9 ("Infrastructure changes")
items are now done:

- ✅ `dispatch_spread_order` + `SpreadExecutionWorker` (engine MLEG path).
- ✅ Two-leg `Position` model + startup spread reconstruction.
- ✅ Trade-DB schema extended for spread legs (`position_lifecycle_legs`).
- ✅ Per-instrument config blocks + validation (§7).
- ✅ Shared sleeve across instances (§6).
- ⚠️ Two engine helpers still hardcode `strategy_name == "credit_spread"`
  (the global concurrent counter and the startup-reconstruction strategy
  lookup); see PLAN.md **11.31** for the cleanup needed before adding a
  second multi-leg strategy.

### Partial-close handling (PR #56 R6 + follow-up)

Credit-spread partial closes are rare in practice — Alpaca documents
MLEG combos as atomic per-leg — but quantity-wise partial fills are
structurally possible (a 2-contract close fill of 1 contract).

**Current behavior (live, post PR #56 R6):**

- The engine peeks at the open spread via `strategy.get_open_spread`
  BEFORE releasing it.
- If `close_qty < open_qty`: position state is **preserved** (no
  release, no pop), partial fill row is logged with
  `status='partial'`, allocator receives `is_full_close=False`,
  CRITICAL log + `broker_error` alert fires, and a new pending
  `position_lifecycle_orders` row at `role='partial_close'` is
  inserted to mark the residual qty. The substrate's
  `uniq_one_active_close_per_position` partial unique index then
  blocks a duplicate close dispatch on the next cycle — durable
  across restarts (§10.7 spread lifecycle PR).
- On restart, `read_open_spread_positions` honors the same semantics:
  spreads with only partial close rows are restored with residual
  qty; only `status='filled'` close rows mark the position closed.
  The substrate `partial_close` placeholder row is also reloaded,
  so the duplicate-block remains in effect immediately on boot.

**Residual handling (operator-resolved):**

Per the §10.7 worker-behavior decision (WAIT, evidence: zero
historical spread partial-fill events on paper), the partial_close
placeholder is **not** auto-progressed by the engine. The CRITICAL
alert is the operator-visible signal; the operator inspects, either
confirms the broker filled the residual or cancels the original
order, and clears the placeholder by hand. Auto-retry of the
residual close is explicitly out of scope until an actual partial
fires and the right cancel-vs-retry policy is informed by real
broker behavior.

**Durability of the substrate close-row attach (PR #72 R1+R2):**

`SpreadExecutionWorker` writes the broker `order_id` to the
`position_lifecycle_orders` row TWICE on every successful submit:
once durably via its own sqlite3 connection (so a crash within
milliseconds of submit still leaves a recoverable row on disk),
and once via the broker's `_pending_lifecycle_close_attaches`
queue (drained at the next cycle by
`_drain_lifecycle_close_attaches` → `attach_or_update_order_id_for_walk_step`).
For walk-and-market closes, every step's broker `order_id` overwrites
the previous one — only one broker order is alive at any moment, so
the substrate row tracks the current in-flight id. If the durable
write fails (DB locked beyond 5s busy_timeout, etc.) the worker
logs CRITICAL `[SpreadExecutor-...] durable substrate write FAILED`;
that is the operator-visible signal that the queue is the only
remaining attach path and a crash before the next cycle drain
re-opens the restart gap.

**Operator runbook — clearing a stuck `partial_close` placeholder:**

Until [PLAN P2 row "PR #72 follow-up: operator command to clear
stuck `partial_close` placeholder"](../PLAN.md) ships, the manual
recipe is:

1. Confirm the broker side: query Alpaca for any open order on the
   spread's short OCC (e.g. via the trading dashboard or Alpaca
   web UI) and EITHER let it fill / cancel naturally OR cancel it
   manually.
2. Find the placeholder substrate row:

   ```sql
   SELECT id, position_uid, client_order_id, intended_qty, created_at
   FROM position_lifecycle_orders
   WHERE role = 'partial_close'
     AND status IN ('pending', 'working', 'partially_filled', 'unknown');
   ```

3. Mark it terminal:

   ```sql
   UPDATE position_lifecycle_orders
   SET status = 'canceled',
       terminal_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now'),
       last_observed_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
   WHERE client_order_id = ?
     AND role = 'partial_close'
     AND status NOT IN ('filled', 'canceled', 'rejected');
   ```

The next cycle's `_spread_has_pending_close` will then return False
and the exit logic resumes normally.

---

## Related docs

- [`strategies.md`](strategies.md) — top-level strategy catalog.
- [`spy_options_reversion_strategy.md`](spy_options_reversion_strategy.md) —
  the long-premium options sleeve that preceded credit spreads.
- [`capital_allocation_reference.md`](capital_allocation_reference.md) —
  sleeve weights and the isolated-options pool design.
- [`regime_flowchart.md`](regime_flowchart.md) — regime classification.

---

# Original design proposal (preserved as research record)

The remainder of this document is the **original design proposal** that
preceded the implementation. It captures the rationale for the trade
structure, the per-instrument knob choices, the sleeve model, and the
infrastructure plan. Treat it as research / rationale; the
**Deployment configuration** section above is the live truth.

---

## 1. Objective

Build a defined-risk premium-selling options strategy that:

- Captures the **volatility risk premium** on index options (selling implied vol systematically higher than realized vol — one of the most-documented "risk premia" in finance)
- Is **underlying-agnostic by design** — one strategy class, one set of logic, parameterized per instrument. SPY, QQQ, IWM, mega-cap single names, and (with care) leveraged ETFs all use the same class with different config blocks
- Trades **multiple concurrent positions across multiple underlyings** so the absolute $ contribution to portfolio P&L is meaningful, not just the sleeve-return ratio
- Lays cleaner plumbing for future spread-based strategies (iron condors, calendar spreads, multi-leg directional)
- Sits **alongside or replaces** the current `spy_options_reversion` (long SPY calls) strategy depending on the 11.26 paper audit results

The current SPY options strategy is, by acknowledgement, **plumbing first, edge second**. It proved the engine can route OCC symbols, manage async fill workers, normalize underlying-vs-contract symbol ambiguity, and apply the 100× P&L multiplier correctly. That groundwork transfers directly to this strategy. What changes is the trade structure (one-leg long premium → two-leg short premium), the underlying engine plumbing for multi-leg orders, and the operating mode (one underlying → N underlyings).

### Why credit spreads (over long calls / cash-secured puts / covered calls)

Buying calls means paying time decay. Every day the underlying doesn't move enough in your direction, you lose. Empirical retail options data is consistently sobering on long-premium directional strategies.

Selling premium (with defined risk) reverses this. Theta works for you, the trade is aligned with index drift, and the edge has academic + practitioner support (volatility risk premium, ~3–5 vol points on SPX historically, similar or higher on QQQ/IWM).

Cash-secured puts capture the same edge but lock up the full strike × 100 in collateral per trade (~$72k for one SPY put at $720 strike) — impractical at portfolio scale. Covered calls require owning 100 shares of the underlying first (~$73k for SPY) — same problem.

The defined-risk credit spread captures roughly the same edge with **~$850 of collateral per position** (max loss only). That's the structural reason this strategy can run multiple concurrent positions on a $108k account in a way no other premium-selling structure can.

---

## 2. Trade structure

**Bull put credit spread** — sell one out-of-the-money put, buy another further out-of-the-money for protection. Both legs same underlying, same expiration.

| Component | Specification |
|---|---|
| Underlying | Any liquid optionable instrument (see §15 for suitability) |
| Leg 1 (short put) | **Sell to open**, target delta `short_leg_delta` from instrument config |
| Leg 2 (long put) | **Buy to open**, `spread_width` dollars below short strike |
| DTE at entry | Within `[dte_min, dte_max]` window from instrument config |
| Order class | Multi-leg combo (Alpaca `OrderClass.MLEG`) — fills both legs atomically or neither |

Both legs in one order means **no leg risk** — if the broker can't fill both at the combined credit, the order fails cleanly, no half-trade orphaned.

### Combo slippage attribution

The trade log records realized MLEG slippage against the submitted combo
limit, not against a later reconstructed leg price. Opening credits compare
the actual net credit received with the submitted credit limit; closing debits
compare the actual net debit paid with the submitted debit limit. Positive bps
means adverse execution versus the submitted combo limit, while negative bps
means price improvement. The economic value is stored on the short-leg row
alongside the combo net price so dashboard strategy stats can surface average
execution quality for completed spreads.

### Concrete example — SPY at $737

| Leg | Action | Strike | DTE | Premium |
|---|---|---|---|---|
| Short put | Sell to open | $720 (~17 delta) | 30 days | Receive $4.00 |
| Long put | Buy to open | $710 | 30 days | Pay $2.50 |

- **Net credit:** $1.50/share × 100 = $150 collected on day one
- **Collateral held:** $850 (= max loss = width × 100 − credit)
- **Outcome at expiration:**
  - SPY > $720 → both puts expire worthless, keep $150 (≈ 80% probability case)
  - SPY at $715 → short put ITM by $5, net P&L = $150 − $500 = −$350
  - SPY ≤ $710 → max loss = $850 (long put kicks in at $710, capping further damage)

The same trade structure on QQQ with $15 width and a $190 short strike behaves identically — just different absolute numbers.

### What this is NOT

- **Not a covered call.** Covered calls require owning 100 shares.
- **Not a cash-secured put.** Cash-secured puts require strike × 100 in cash.
- **Not a margin loan.** Alpaca holds the max-loss amount in *your* cash as collateral. You aren't borrowing. This is Level 3 options approval, supported on cash accounts.

---

## 3. Entry rules (apply per strategy instance, per underlying)

A new position opens when **all** of the following are true:

1. **Regime gate (allocator-level):** current regime in `{TRENDING, RANGING}` — never sell puts in BEAR or VOLATILE. Enforced via `StrategySlot.allowed_regimes`.
2. **Underlying trend gate:** underlying close > its own 50 SMA — don't sell puts into a downtrend on this specific instrument.
3. **Volatility gate:** instrument's IV proxy > `min_iv_proxy` from config. For SPY/QQQ this is VIX directly. For IWM it's RVX or `VIX × 1.2` as an approximation. For single names it's a per-symbol IV estimate. The proxy source is configurable per instrument.
4. **DTE availability:** at least one expiration in the chain falls within `[dte_min, dte_max]`.
5. **Spread availability:** the multi-leg ranker (§5) returns a valid pick that fits the sleeve budget.
6. **Concurrent position cap (per instance):** open positions on this underlying < `max_concurrent_positions` for this instrument.
7. **Same-expiration cap:** at most `max_per_expiration` (default 1) open position per (underlying, expiration_date) tuple.
8. **DTE staggering:** new entry's expiration ≥ `min_dte_gap_between_opens` days from the most recent existing position on this underlying.
9. **Earnings blackout** (single-name underlyings only): no entry within `earnings_blackout_days` of a known earnings date. Skipped for ETFs.

No FOMC/NFP filter in v1 — easy to add later if backtests show it matters.

---

## 4. Exit rules

A position closes when **any** of the following triggers fire:

| Trigger | Condition | Rationale |
|---|---|---|
| **Profit target** | Spread mid-price ≤ `profit_target_pct` × initial credit | Closing at 50% of max profit dominates "hold to expiration" on Sharpe. tastytrade-validated. |
| **Stop loss** | Spread mid-price ≥ `stop_loss_multiple` × initial credit | Soft stop so you don't ride losers to max loss. |
| **Time stop** | DTE ≤ `time_stop_dte` (default 21) | Gamma exposure becomes punishing in the last 3 weeks. |
| **Short strike breach** | Underlying close ≤ short strike | Trade thesis is broken; freeing capital beats riding it out. Configurable per instrument via `exit_on_short_strike_breach`. |
| **Regime exit** | Regime shifts to BEAR mid-trade | Defensive override. Exits are never blocked by regime gate. |

### Exit mechanics

Exits use **closing combo orders** with a positive net-debit limit derived from the
current quoted spread mid (`short mid − long mid`), rounded to cents. The worker
lets the combo rest for the configured timeout and, if still unfilled, cancels the
order and returns control to the engine. The engine keeps the spread open and
retries on a later cycle if the exit trigger still holds.

This is intentionally conservative for the first paper rollout: no forced
"market-style" close is sent after a timeout, and the bot never exits solely
because quote data is missing. The trade-off is that some MLEG closes can require
many retries before the market will trade at the modeled debit. That behavior is
tracked as a dedicated paper-watch tuning item in [PLAN.md](/Users/franco/trading-bot/PLAN.md) (`11.41`).

---

## 5. Two-leg contract picker (extends the 11.25 ranker)

The 11.25 ranker handles single-leg call picking. Credit spreads need a **paired-leg** picker.

### Design — extend `utils/options_ranker.py`

Add:
- `SpreadCandidate(short_leg: Candidate, long_leg: Candidate, width: float, net_credit: float)`
- `rank_put_spread_candidates(candidates, quotes, *, target_delta, max_loss_per_position, ...)`

Pure logic, no I/O. Same module so future spread strategies (iron condors = 2 spreads, calendars = different DTEs) reuse primitives.

### Hard filters (drop before scoring)

- **Affordability** — max loss ≤ `max_loss_per_position` (from sleeve allocator)
- **Min credit** — net credit ≥ `min_credit_pct_of_width` × spread width (default 25%); reject thin credits below this floor
- **Valid quotes** — both legs bid > 0, ask > 0, ask ≥ bid
- **Short leg delta window** — within `[short_leg_delta − 0.05, short_leg_delta + 0.05]`

### Composite score (0.0–1.0)

| Weight | Factor |
|---|---|
| 0.40 | Short-leg delta proximity to `short_leg_delta` target |
| 0.30 | Net credit relative to width (more credit = better) |
| 0.20 | Combined spread quality (mean of both legs' bid/ask spread%) |
| 0.10 | DTE proximity to mid-DTE target |

### Delta estimation (no reliable Greeks from Alpaca paper)

Use `blackscholes` (already in requirements) with:
- Underlying close
- Candidate strike
- Risk-free rate = 5.0% (config constant, refresh quarterly)
- Volatility = `iv_proxy × adjustment_factor` (per-instrument config)
- DTE = expiration − today

Approximate but fine for selection.

---

## 6. Position sizing & multi-position management

### Per-position size

```
max_loss_per_position = spread_width × 100 − net_credit_received
collateral_required   = max_loss_per_position   (Alpaca holds in cash)
```

For a $10-wide SPY spread with $150 credit: `max_loss = $850`.
For a $15-wide QQQ spread with $200 credit: `max_loss = $1,300`.
For a $5-wide IWM spread with $90 credit: `max_loss = $410`.

### Sleeve model — shared across underlyings (v1 recommendation)

All credit-spread strategy instances share a single allocator sleeve named `credit_spread`. The allocator enforces a global collateral budget across all instances:

```
sum(collateral_in_use across all credit_spread_* instances) ≤ CREDIT_SPREAD_SLEEVE_BUDGET
```

Each instance has its own `max_concurrent_positions` cap from its instrument config. The allocator's `max_per_underlying` is enforced implicitly through this.

**Why shared, not per-underlying:** simpler accounting, no risk of one instrument's sleeve sitting idle while another is starved, correlated underlyings should share fate naturally. Per-underlying sleeves can be introduced in v2 if data shows benefit.

### Diversification constraints (within the shared sleeve)

1. `max_per_expiration` (default 1) per instance — at most one position per (underlying, expiration_date) per instance
2. `min_dte_gap_between_opens` (default 7) per instance — stagger entries on each underlying
3. Concurrent caps per instance prevent any single underlying from dominating
4. Combined global cap: `MAX_TOTAL_CONCURRENT_CREDIT_SPREADS` (default 8) across all instances combined

### Sleeve sizing — recommendation

Honest math for SPY + QQQ + IWM running concurrently:

| Underlying | Avg collateral/position | Max concurrent | Avg deployed |
|---|---|---|---|
| SPY | $850 | 3 | $2,550 |
| QQQ | $1,300 | 3 | $3,900 |
| IWM | $410 | 2 | $820 |
| **Total** | — | **8** | **~$7,300** |

Plus headroom for new entries while existing ones are open. **Recommend `CREDIT_SPREAD_SLEEVE_BUDGET = $11k–13k`, i.e. 10–12% of $108k equity.**

Combined with the existing 5% `spy_options_reversion` sleeve, total options exposure = 15–17%. If the `spy_options_reversion` is being retired post-11.26 audit, the credit spread sleeve absorbs that allocation and lands at 15–17% solo.

### Capital flow under defaults

With profit target at 50% of credit and ~14-day average holding period, each position cycles ~24× per year. Across 8 max concurrent slots → 150–200 trade cycles per year. Even at modest per-trade expectancy, that's where the absolute $ contribution comes from.

---

## 7. Tuning knobs — per-instrument configuration

All per-instrument behavior lives in `config/settings.py` under `CREDIT_SPREAD_INSTRUMENTS`. Strategy logic is hardcoded; only thresholds, deltas, widths, and frequency caps are configurable.

```python
# config/settings.py
CREDIT_SPREAD_INSTRUMENTS = {
    "SPY": {
        # Entry
        "short_leg_delta": 0.17,
        "spread_width": 10,
        "dte_min": 30,
        "dte_max": 45,
        "iv_proxy_source": "vix",
        "min_iv_proxy": 14,
        "min_credit_pct_of_width": 0.25,

        # Position management
        "max_concurrent_positions": 3,
        "max_per_expiration": 1,
        "min_dte_gap_between_opens": 7,

        # Exits
        "profit_target_pct": 0.50,
        "stop_loss_multiple": 2.0,
        "time_stop_dte": 21,
        "exit_on_short_strike_breach": True,
        "limit_timeout_seconds": 30,

        # Earnings (only meaningful for single names)
        "earnings_blackout_days": 0,  # ETF, no earnings
    },
    "QQQ": {
        "short_leg_delta": 0.17,
        "spread_width": 15,           # higher price → wider strikes
        "dte_min": 30,
        "dte_max": 45,
        "iv_proxy_source": "vix",     # QQQ tracks SPX closely
        "min_iv_proxy": 14,
        "min_credit_pct_of_width": 0.25,
        "max_concurrent_positions": 3,
        "max_per_expiration": 1,
        "min_dte_gap_between_opens": 7,
        "profit_target_pct": 0.50,
        "stop_loss_multiple": 2.0,
        "time_stop_dte": 21,
        "exit_on_short_strike_breach": True,
        "limit_timeout_seconds": 30,
        "earnings_blackout_days": 0,
    },
    "IWM": {
        "short_leg_delta": 0.17,
        "spread_width": 5,            # lower price → tighter strikes
        "dte_min": 30,
        "dte_max": 45,
        "iv_proxy_source": "rvx",     # IWM has its own IV index
        "min_iv_proxy": 18,           # IWM IV runs higher than SPY/QQQ
        "min_credit_pct_of_width": 0.25,
        "max_concurrent_positions": 2,
        "max_per_expiration": 1,
        "min_dte_gap_between_opens": 7,
        "profit_target_pct": 0.50,
        "stop_loss_multiple": 2.0,
        "time_stop_dte": 21,
        "exit_on_short_strike_breach": True,
        "limit_timeout_seconds": 30,
        "earnings_blackout_days": 0,
    },
    # TQQQ deliberately not in v1 — see §15
    # AAPL/MSFT/NVDA possible in v2 with earnings_blackout_days=3
}

CREDIT_SPREAD_SLEEVE_BUDGET_PCT = 0.10
MAX_TOTAL_CONCURRENT_CREDIT_SPREADS = 8

# Ranker weights (shared across all instances)
CREDIT_SPREAD_RANKER_WEIGHTS = {
    "delta": 0.40,
    "credit": 0.30,
    "spread_quality": 0.20,
    "dte": 0.10,
}
```

### Direction of effect for each knob

| Knob | Higher value → effect |
|---|---|
| `short_leg_delta` | More credit per trade, lower win rate, larger losers |
| `spread_width` | Bigger absolute credit & max loss, same risk:reward ratio |
| `dte_min` / `dte_max` | More theta-positive but slower turnover; lower → less theta, more gamma |
| `min_iv_proxy` | Trade less, only when premium is rich |
| `min_credit_pct_of_width` | Reject more thin spreads; trade less but with better R:R |
| `max_concurrent_positions` | More capital deployed, more risk in a vol spike |
| `min_dte_gap_between_opens` | More calendar diversification, fewer opportunities |
| `profit_target_pct` | Higher → ride for more profit, more gamma risk; lower → close faster, smaller wins, more turnover |
| `stop_loss_multiple` | Higher → ride losers further; lower → cut losses earlier, more whipsaws |
| `time_stop_dte` | Higher → more time for thesis; lower → less gamma exposure |

---

## 8. Expected behavior (paper-projected)

Honest expectations based on documented research, **not promises**. Numbers below assume the proposed default knobs and SPY + QQQ + IWM running concurrently with 8 max concurrent positions.

| Metric | Expected range |
|---|---|
| Win rate (per trade) | 72–80% |
| Average winner | ~50% of credit received |
| Average loser | 1.5–2.5× credit received |
| Expectancy per trade | Positive but small (~+25% of max profit on average) |
| Trade cycles per year | 150–200 across all 3 underlyings |
| Max drawdown (sleeve) | 20–35% in a vol-expansion event (e.g., Aug 2024-style spike) |
| Annual sleeve return | 30–60% in normal conditions |
| Annual portfolio contribution | 3–6% on $108k account |
| Worst quarter | −10% to −25% of sleeve, plausibly more if positioned wrong into a crash |

### Critical caveat — negative skew

This strategy has **negative skew**: many small wins, occasional large losers. Sharpe is high but the path is bumpy. There will be weeks where the sleeve looks ugly even while expectancy holds long-term. **Acceptance of this characteristic is a prerequisite for running the strategy.** Anyone who can't sit through a 20% sleeve drawdown without intervening will sabotage the edge.

### Correlation risk across underlyings

SPY, QQQ, IWM are correlated — when one breaches its short strike, the others often follow. The shared sleeve model accepts this. The `MAX_TOTAL_CONCURRENT_CREDIT_SPREADS` global cap is the safety net: even if every instrument's individual cap is full, the combined cap prevents 9 simultaneous losers in a correlated drawdown.

---

## 9. Infrastructure changes

### Required new engine work

1. **Multi-leg order support in `execution/broker.py`** — wrap Alpaca's `MLEG` order class. The current broker handles only single-leg orders. ~100–150 LOC plus integration tests.
2. **`OptionsExecutionWorker` extension** — accept multi-leg legs spec, submit as combo, track combined fill confirmation.
3. **Trade DB schema** — add `spread_id` column (UUID grouping the two legs as one logical position) and `position_type` (`'single_leg'` or `'spread'`). Migration-safe column adds.
4. **`_position_owners` keying** — current keying by OCC works for single legs. For spreads, key by `spread_id` and store both legs as a paired attribute. Cleaner option: introduce a position abstraction that hides single-vs-spread.
5. **Per-instrument IV proxy lookup** — small utility that resolves an IV source (VIX, RVX, single-name IV) given an underlying ticker. Used at entry-gate evaluation time.

### Reusable from 11.25 / current infrastructure

- `utils/options_ranker.py` — extend with spread-pair scoring
- `utils/options_lookup.py` — chain query, pagination, K-nearest filter all reused (instrumented to accept underlying parameter, which it already does)
- OPRA snapshot quote lookup — reused, batched for both legs
- Sleeve allocator — reused with new shared sleeve entry
- Engine cycle loop, regime gating, watchlist plumbing — unchanged
- `OptionTradeRejected` / structured rejection — reused
- Stream manager + fill watcher — extends to handle combo events
- 100× P&L multiplier, `_OCC_PAT` gating — already in place
- Earnings calendar lookup (already exists for RSI / SMA edge filters) — reused for single-name underlying configs

### Estimated effort

| Component | LOC | Complexity |
|---|---|---|
| Strategy module (`strategies/credit_spread.py`) | ~450 | Medium |
| Edge filter (`strategies/filters/credit_spread.py`) | ~120 | Low-medium (handles per-instrument IV proxy) |
| Ranker extension (spread-pair scoring) | ~150 | Medium |
| Picker extension (multi-leg selection from chain) | ~130 | Medium |
| Broker multi-leg order method | ~100 | Medium |
| OptionsExecutionWorker spread variant | ~150 | Medium-high |
| Trade DB schema migration | ~30 | Low |
| Engine wiring (multi-instance slot support) | ~60 | Low |
| IV proxy lookup utility | ~40 | Low |
| Unit tests | ~750 (50–60 tests) | Medium |
| Integration verify script | ~250 | Medium |

**Total: ~2,250 LOC, 3–4 focused days of work.** Single PR — too interlinked to split cleanly.

---

## 10. Tests required

### Strategy unit tests (`tests/test_credit_spread.py`)

- Entry rules — each gate (regime, SMA, IV proxy, DTE, spread availability) independently
- Per-instance isolation — SPY instance signals don't affect QQQ instance state
- Position limits — opens up to `max_concurrent_positions`, rejects beyond, per-instance
- Global cap — `MAX_TOTAL_CONCURRENT_CREDIT_SPREADS` enforced across instances
- Expiration concentration — `max_per_expiration` enforced per instance
- DTE staggering — `min_dte_gap_between_opens` enforced per instance
- Profit target trigger — exits at configured percentage of credit
- Stop loss trigger — exits at configured multiple of credit
- Time stop — exits at `time_stop_dte` regardless of P&L
- Short strike breach — exits when underlying ≤ short strike
- Regime exit override — exits even when entries are blocked
- Earnings blackout — single-name instance blocks entry during blackout, ETF instances ignore

### Per-instrument config tests

- Loading `CREDIT_SPREAD_INSTRUMENTS["SPY"]` produces correctly-configured strategy
- Two instances with different underlyings run with different parameters
- Missing config key in an instrument block raises a clear error at load time

### Ranker extension tests (`tests/test_options_ranker.py` extended)

- Spread-pair scoring formula — verify weights
- Affordability filter — drops spreads where max loss > budget
- Min-credit filter — drops thin spreads
- Delta target window — short leg within ±0.05 of target

### IV proxy tests (`tests/test_iv_proxy.py`)

- VIX lookup returns current value
- RVX lookup returns current value
- Unknown source raises a clear error

### Broker multi-leg tests (`tests/test_broker.py` extended)

- Submits MLEG order with correct leg structure
- Tracks combined fill via single order ID
- Handles partial-fill on one leg (rejection, no orphan position)
- Cancel cleanly during pending state

### Trade DB tests (`tests/test_reporting.py` extended)

- `spread_id` grouping on writes
- Read-back groups legs correctly for ownership restore

---

## 11. Open questions for the implementer

Capture answers in the PLAN.md row before coding starts:

1. **Alpaca account options level** — is it Level 3 (spreads on cash)? Verify before any code goes in.
2. **Sleeve allocation** — accept the proposed 10–12% credit-spread sleeve, or different size? Replace the existing 5% `spy_options_reversion` sleeve entirely after 11.26 audit?
3. **v1 underlyings** — SPY + QQQ + IWM as proposed? Or start with SPY only and add the others in a v1.1? My recommendation: launch with SPY + QQQ at v1, add IWM after 30 days of paper.
4. **TQQQ in scope?** Strong recommendation: not v1, not v2 either. Possibly v3 only with explicit acknowledgement of the very different risk profile. See §15.
5. **Single-name underlyings (AAPL, NVDA, MSFT) in scope?** Possible from v1 if the earnings blackout works correctly, but adds idiosyncratic gap risk. My recommendation: ETFs only at launch; single names as v2 after 60 days of clean paper.
6. **Per-instance vs shared sleeve** — proposed: shared `credit_spread` sleeve. Accept, or want per-underlying isolation?
7. **Tuning bias** — defaults bias toward safety (17Δ, 50% profit target, 21 DTE time stop, $10 width on SPY). Confirm or override.

---

## 12. Acceptance criteria

This is **done** when:

- [ ] All entry/exit rules pass unit tests
- [ ] Multi-leg orders submit and fill cleanly on Alpaca paper
- [ ] Position ownership survives restart (durable ownership extended to spreads via `spread_id`)
- [ ] Sleeve allocator respects new strategy's budget and the global concurrent cap
- [ ] Two strategy instances (e.g. SPY + QQQ) run concurrently without interfering
- [ ] Dashboard shows open spreads with both legs visible
- [ ] First 5 paper trades complete (any P&L) without infrastructure errors
- [ ] PR description includes a paper-watch follow-up item (analogous to 11.26) for the first 30 fills with defined tuning triggers

---

## 13. Implementation sequence

When picked up:

1. Capture answers to §11 in the PLAN.md row for this strategy
2. Branch from main as `feat/credit-spread-strategy`
3. Build in order:
   1. IV proxy utility + tests
   2. Ranker extension (spread-pair scoring) + unit tests
   3. Multi-leg picker (chain query → ranker)
   4. Strategy module + edge filter + unit tests (start with one instance, then add multi-instance tests)
   5. Per-instrument config wiring + tests
   6. Broker multi-leg order method + unit tests
   7. OptionsExecutionWorker spread variant
   8. Trade DB schema migration
   9. Engine wiring (multi-instance slot support, shared-sleeve registration)
   10. Integration verify script (SPY + QQQ instances)
4. One PR for the whole thing
5. Recycle the bot once merged; first SPY entry is the validation moment
6. Define paper-watch follow-up task in PLAN.md (analogous to 11.26)
7. Add IWM instance after SPY+QQQ have ~15 fills under the belt

---

## 14. Why this is a good "next options strategy"

Summary for the decision review:

| Factor | Current `spy_options_reversion` | Proposed `credit_spread` |
|---|---|---|
| Trade direction | Long premium (pay theta) | Short premium (collect theta) |
| Win rate (theoretical) | ~40–50% — needs directional accuracy | ~75–80% — needs underlying to stay above strike |
| Per-trade capital | ~$1k–$2k (one contract) | $400–$1,300 (depends on underlying & width) |
| Risk profile | Large win/large loss, symmetric | Many small wins / occasional large loss (negative skew) |
| Edge source | Mean-reversion timing (fights drift) | Volatility risk premium (aligned with drift) |
| Sensitivity to time decay | Hurts | Helps |
| Scalability | Capped at 1 contract at a time | Scales across underlyings AND concurrent positions per underlying |
| Underlying portability | SPY only by design | Any liquid optionable instrument via config block |
| Empirical edge support | Mixed for retail long-premium | Strong for retail short-premium with defined risk |

The credit spread isn't a magic bullet. It will lose money some months. But the math is structurally aligned with what works in retail options trading, and the plumbing investment from 11.25 makes the next step cheaper to build than the first one was.

---

## 15. Underlying suitability

The strategy is **mechanically agnostic** — any liquid optionable underlying works. But the **profitability** varies dramatically by what you point it at. Pick instruments carefully.

### Suitability ranking

| Underlying | Suitability | Reasoning |
|---|---|---|
| **SPY** | ★★★★★ Ideal | Highest options liquidity in the world, tightest spreads, mean-reverting drift, low single-event tail risk |
| **QQQ** | ★★★★ Strong | Similar to SPY; slightly higher vol means richer premium for same delta. Tech concentration risk in tail events |
| **IWM** | ★★★ Workable | Good liquidity on monthlies, weeklies thinner. Small-caps can gap harder than large-cap |
| **DIA** | ★★ Marginal | Liquid but lower vol → smaller credits → barely worth the capital lock |
| **Mega-cap single names** (AAPL, MSFT, NVDA) | ★★ Conditional | Liquid options. **Must add earnings blackout.** Idiosyncratic gap risk higher than indexes |
| **Sector ETFs** (XLF, XLE, etc.) | ★ Avoid v1 | Liquidity varies wildly. Sector-specific event risk. Often wider spreads |
| **Mid-cap single names** | ★ Avoid | Spreads typically too wide, liquidity unreliable for spread orders |
| **TQQQ / SOXL / other 3× ETFs** | ★ **Avoid unless explicitly designed for** | See dedicated treatment below |

### Why TQQQ (and other leveraged ETFs) are not "free upgrades"

When you originally asked about TQQQ, the honest answer is they're a categorically different risk profile, not a juiced-up QQQ.

**The problems specific to leveraged ETFs:**

1. **Daily vol is 3× the underlying index.** A −5% day on QQQ is −15% on TQQQ. The "17-delta" short put on TQQQ has roughly the same theoretical probability of being breached, but when breached, the realized loss within the defined-risk envelope is much closer to max loss because the underlying gaps so far past the short strike.
2. **Volatility decay is structural.** TQQQ slowly decays over time even in flat markets due to daily-rebalancing drag. For selling puts this is a mixed bag — the structural drift drag means the underlying grinds toward your short strike more often than QQQ would predict.
3. **Tail events are catastrophic.** August 2024 vol spike: QQQ −5%, TQQQ −15%. A $5-wide TQQQ credit spread that looked perfectly reasonable Friday close was at max loss Monday open. The defined-risk envelope holds — you can't lose more than max loss — but you hit it more often.
4. **Bid/ask spreads are wider.** OPRA quotes on TQQQ options regularly show 5–10% bid/ask spreads even at-the-money, vs 1–2% on QQQ. The ranker's spread-quality filter helps but you'll skip more trades.
5. **Pin risk near expiration is more severe.** Strike-pinning behavior is more chaotic on leveraged ETFs.

**If you absolutely want TQQQ in scope someday:**

Treat it as a different strategy with different parameters:

- Tighter short-leg delta (0.10–0.12 instead of 0.17)
- Shorter DTE window (21–35 instead of 30–45)
- Smaller spread widths ($5 instead of $10)
- Higher IV-proxy threshold for entry
- Tighter stop loss (1.5× credit instead of 2×)
- Smaller per-trade sleeve allocation (max 1 concurrent at any time)
- Treat it as a v3+ addition after at least 90 days of paper on the index ETFs

The architecture supports this trivially — it's just another instrument config block. But don't drop it in with QQQ-like defaults and expect the same risk profile.

### When to add a new underlying

Two-step gate:

1. **Liquidity check** — manually inspect the chain. Are options 30–45 DTE quoted with bid > 0, ask > 0, spreads < 5% on at-the-money strikes? If not, skip.
2. **Paper validation** — once added to config, run for at least 30 days of paper before increasing its `max_concurrent_positions` past 1. Watch the same four metrics from 11.26: picked-spread distribution, fill rate, realized-vs-modeled slippage, and how often the chosen short strike falls outside the target delta band.

If both check out, ramp up concurrent positions to design defaults and add to live rotation.

---

## 16. Prerequisites & staging

The strategy itself is roughly 30% of the implementation. The remaining 70% is infrastructure the bot doesn't have yet. Most of that infrastructure is broadly useful — once it lands, future spread strategies (bear call, iron condor, calendar, diagonal) become small additions on top.

### What's already in place (reused as-is)

| Component | Used for |
|---|---|
| `utils/options_ranker.py` (11.25) | Extends with `rank_put_spread_candidates` |
| `utils/options_lookup.py` chain query + pagination | Reused for picking both legs |
| OPRA snapshot quote lookup | Reused, batched for both legs |
| `OptionsExecutionWorker` async pattern | Pattern reused, worker itself needs extension |
| `_OCC_PAT` engine gates | Reused as-is |
| 100× P&L multiplier on close paths | Reused as-is |
| `OptionTradeRejected` structured rejection | Reused as-is |
| Stream manager + fill watcher | Reused, needs combo-event extension |
| Earnings calendar (existing for SMA/RSI filters) | Reused for single-name underlyings |
| Sleeve allocator | Reused with new sleeve entry |
| Regime gating via `StrategySlot.allowed_regimes` | Reused as-is |
| `blackscholes` library (in requirements) | Reused for delta estimation |

### Hard prerequisites — must exist before strategy code can land

#### Prereq 1 — Position abstraction

Today the engine thinks `_position_owners: dict[symbol, strategy_name]`. For options it's keyed by underlying ticker (`"SPY"`) — the 11.23 known limitation. Credit spreads make this harder because **a single logical position is two OCC symbols simultaneously.**

Generalize to a position-ID concept:

```python
_positions: dict[str, Position]   # position_id → Position
# Position carries: strategy, legs[], entry_prices[], position_type
# Single-leg options: one entry in legs
# Spreads: two entries
# Equities: position_id = equity symbol (backward compat)
```

Touch points (~10 sites in `engine/trader.py`):
- `_restore_ownership_from_db`, `_reconcile_startup`
- `_detect_external_closes`, `_process_stream_stop_fills`
- `_drain_option_fills`, `_attribute_orders`
- State snapshot for dashboard
- `_record_realized_pnl`

**Estimated:** ~400 LOC + significant test rewrites. **Subsumes 11.23 as a byproduct.**

#### Prereq 2 — Trade DB schema migration

Two new columns:
- `position_id` (UUID, indexed) — groups legs of one logical position
- `position_type` (`'single_leg'` | `'spread'`)

Migration-safe column adds (the bot has done these before — see allocator state restore migrations). Backward compat: existing single-leg rows get `position_id = occ_symbol` and `position_type = 'single_leg'` in the migration.

**Estimated:** ~80 LOC including migration + `TradeLogger` updates + tests.

#### Prereq 3 — Multi-leg order support in `execution/broker.py`

Alpaca exposes multi-leg orders via `OrderClass.MLEG`. The current broker only handles single-leg paths. Adds:
- `place_spread_order()` — submits both legs as one combo order
- `close_spread_order()` for exits
- Atomic-fill / atomic-reject handling via Alpaca's combo semantics
- `OptionsExecutionWorker` extension to await combo fills via the stream

**Estimated:** ~350 LOC + tests.

### Soft prerequisites — can ride inside the strategy PR

| Item | Notes |
|---|---|
| Multi-strategy-name sleeve grouping | Use Option X from §6 (instances share a single strategy name `credit_spread`, underlying carried internally) — no allocator change needed |
| IV proxy lookup utility | ~40 LOC, lives in the strategy PR |
| Dashboard spread rendering | Follows naturally from the position abstraction; small dashboard update |

### Critical non-code gate

**Alpaca account must be at Level 3** (defined-risk spreads on cash accounts). If the account is Level 1 or 2, this entire strategy is off the table and the design needs rework.

**Verification status (2026-05-10):** Paper account confirmed Level 3 via `TradingClient.get_account()` (`options_approved_level=3`, `options_trading_level=3`, options buying power is sufficient for the planned sleeve). **Paper is unblocked.** Re-run the same check against live credentials (`LIVE_TRADING=true`) before the live flip — Alpaca paper and live approval levels can diverge, and live options buying power should be re-checked to size the real sleeve.

### Revised staging — 3 PRs

The original 2-PR plan combined the multi-leg broker with the strategy. Splitting at one more boundary makes the multi-leg broker testable on its own and keeps each PR review-sized.

#### PR 1 — Position abstraction + Trade DB schema

| Component | LOC |
|---|---|
| `Position` dataclass + engine refactor | ~400 |
| Trade DB schema migration + `TradeLogger` updates | ~80 |
| Backfill migration script (existing rows get `position_id`) | ~30 |
| Tests | ~400 |
| **Total** | **~910 LOC** |

**Functional outcome:** No new strategies. The bot has a clean position abstraction; single-leg options continue working unchanged. **11.23 closed as a byproduct.**

**Risk profile:** Lowest — pure refactor with backward-compat checks. The bot keeps running the existing strategies the whole time.

**Validation gate before merge:** Recycle the bot, confirm existing positions reconcile cleanly under the new abstraction, run a paper cycle without errors.

#### PR 2 — Multi-leg order support + spread ranker extension

| Component | LOC |
|---|---|
| `AlpacaBroker.place_spread_order` + `close_spread_order` | ~200 |
| `OptionsExecutionWorker` spread variant | ~150 |
| Stream manager combo-event handling | ~50 |
| `utils/options_ranker.rank_put_spread_candidates` | ~150 |
| Multi-leg picker (chain query → ranker → `SpreadCandidate`) | ~130 |
| Tests | ~450 |
| **Total** | **~1,130 LOC** |

**Functional outcome:** Bot can submit and track multi-leg orders. Ranker can score spread pairs. No strategy yet uses it.

**Risk profile:** Medium — new broker code path, but unit-testable with mocked Alpaca client. Has a one-shot integration verify script that places and cancels a test spread order on paper to prove the pipeline.

**Validation gate before merge:** Integration script places + cancels a real spread order on Alpaca paper without errors.

#### PR 3 — Credit spread strategy

| Component | LOC |
|---|---|
| `strategies/credit_spread.py` | ~450 |
| `strategies/filters/credit_spread.py` (edge filter + IV proxy) | ~160 |
| IV proxy utility (`utils/iv_proxy.py`) | ~40 |
| Per-instrument config in `config/settings.py` | ~80 |
| `forward_test.py` slot wiring for SPY + QQQ | ~30 |
| Dashboard spread rendering | ~100 |
| Strategy unit tests | ~500 |
| Integration verify script | ~250 |
| **Total** | **~1,610 LOC** |

**Functional outcome:** Live credit spreads on SPY + QQQ paper trading.

**Risk profile:** Strategy logic + parameter tuning. The hard plumbing is already in via PR 1 and PR 2, so review focuses on strategy correctness rather than infrastructure mechanics.

**Validation gate before merge:** Integration verify passes, first 5 paper trades complete without infrastructure errors, paper-watch follow-up item logged in PLAN.md (analogous to 11.26).

### Effort summary

| PR | LOC | Days (focused) | Cumulative |
|---|---|---|---|
| PR 1 — Position abstraction | ~910 | 1.5 | 1.5 |
| PR 2 — Multi-leg + ranker | ~1,130 | 1.5 | 3.0 |
| PR 3 — Credit spread strategy | ~1,610 | 2.0 | 5.0 |
| **Total** | **~3,650** | **5.0 days** | — |

Larger than the original 2,250 LOC estimate because the position-abstraction refactor surfaces more touch points than the SPY-only proposal anticipated. Worth it: the abstraction is the right end state regardless of credit spreads.

### What to decide before any code is written

1. ~~**Alpaca Level 3 verification**~~ — ✅ confirmed 2026-05-10 on paper (`options_trading_level: 3`). Re-verify on live before the live flip.
2. **Confirm 3-PR staging** — vs single PR or different split.
3. **Confirm 11.23 folds into PR 1** — close 11.23 as resolved by the position abstraction.
4. **Confirm v1 underlyings** — SPY + QQQ at v1 (default), or SPY only first then QQQ in v1.1?

---

## 17. Future spread variants this strategy enables

The infrastructure built for credit spreads makes these much cheaper to add later:

| Strategy | Reuses from credit spread | Net new work |
|---|---|---|
| **Bear call credit spread** | Everything; just sell calls instead of puts | ~50 LOC strategy variant |
| **Iron condor** (put spread + call spread) | Both-leg picker, sleeve accounting, exit logic | ~150 LOC, new multi-spread management |
| **Calendar spread** | Single underlying, multi-leg, sleeve | ~200 LOC, requires DTE-pair selection |
| **Diagonal spread** | Same | ~250 LOC, more parameter combinations |
| **Long call/put debit spreads** (for directional bets with defined risk) | Multi-leg picker, sleeve | ~150 LOC strategy variant |

This is the strategic case for getting the credit spread plumbing right the first time: it's the foundation for a whole family of multi-leg strategies, not a one-off.
