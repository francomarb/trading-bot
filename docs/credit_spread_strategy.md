# Credit Spread Strategy — Design Proposal (Agnostic)

**Status:** Proposal — no code yet, awaiting implementation
**Suggested strategy ID:** `credit_spread` (instances named `credit_spread_spy`, `credit_spread_qqq`, etc.)
**Proposed branch on implementation:** `feat/credit-spread-strategy`
**Related PR:** follows [#6](https://github.com/francomarb/trading-bot/pull/6) (11.25 — composite-score options picker)
**Author note:** This is the proposal that came out of the post-PR-#6 conversation about "if you had to pick one options strategy with profitability as top priority, what would it be?" — written so future sessions can pick it up cold. Originally drafted SPY-only; rewritten on the same day to be underlying-agnostic with per-instrument config blocks after the user correctly observed that strategy logic should be portable.

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

Exits use **closing combo orders** at the spread bid. If the spread doesn't fill within `limit_timeout_seconds`, escalate to market.

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

## 16. Future spread variants this strategy enables

The infrastructure built for credit spreads makes these much cheaper to add later:

| Strategy | Reuses from credit spread | Net new work |
|---|---|---|
| **Bear call credit spread** | Everything; just sell calls instead of puts | ~50 LOC strategy variant |
| **Iron condor** (put spread + call spread) | Both-leg picker, sleeve accounting, exit logic | ~150 LOC, new multi-spread management |
| **Calendar spread** | Single underlying, multi-leg, sleeve | ~200 LOC, requires DTE-pair selection |
| **Diagonal spread** | Same | ~250 LOC, more parameter combinations |
| **Long call/put debit spreads** (for directional bets with defined risk) | Multi-leg picker, sleeve | ~150 LOC strategy variant |

This is the strategic case for getting the credit spread plumbing right the first time: it's the foundation for a whole family of multi-leg strategies, not a one-off.
