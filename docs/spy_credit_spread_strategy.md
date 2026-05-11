# SPY Index Credit Spread Strategy — Design Proposal

**Status:** Proposal — no code yet, awaiting implementation
**Suggested strategy ID:** `spy_credit_spread`
**Proposed branch on implementation:** `feat/spy-credit-spread-strategy`
**Related PR:** follows [#6](https://github.com/francomarb/trading-bot/pull/6) (11.25 — composite-score options picker)
**Author note:** This is the proposal that came out of the post-PR-#6 conversation about "if you had to pick one options strategy with profitability as top priority, what would it be?" — written so future sessions can pick it up cold.

---

## 1. Objective

Build a defined-risk premium-selling options strategy that:

- Captures the **volatility risk premium** on index options (selling implied vol systematically higher than realized vol — one of the most-documented "risk premia" in finance)
- Trades **multiple concurrent positions** so the absolute $ contribution to portfolio P&L is meaningful, not just the sleeve-return ratio
- Lays cleaner plumbing for future spread-based strategies (iron condors, calendar spreads, multi-underlying)
- Sits **alongside or replaces** the current `spy_options_reversion` (long SPY calls) strategy depending on the 11.26 paper audit results

The current SPY options strategy is, by acknowledgement, **plumbing first, edge second**. It proved the engine can route OCC symbols, manage async fill workers, normalize underlying-vs-contract symbol ambiguity, and apply the 100× P&L multiplier correctly. That groundwork transfers directly to this strategy. What changes is the trade structure (one-leg long premium → two-leg short premium) and the underlying engine plumbing for multi-leg orders.

### Why credit spreads and not the current long-call setup

Buying calls means paying time decay. Every day the underlying doesn't move enough in your direction, you lose. RSI mean-reversion on SPY also fights the asset's structural uptrend. The current strategy has three structural headwinds at once: long premium → theta against you, mean-reversion signal → fights drift, 14–28 DTE → squarely in the theta-acceleration window. Empirical retail options data is consistently sobering on long-premium directional strategies.

Selling premium (with defined risk) reverses all three. Theta works for you, the trade is aligned with SPY's structural up-and-flat tendency, and the edge has academic + practitioner support (volatility risk premium, ~3–5 vol points on SPX historically).

---

## 2. Trade structure

**Bull put credit spread** — sell one out-of-the-money put, buy another further out-of-the-money for protection. Both legs same underlying, same expiration.

| Component | Specification |
|---|---|
| Underlying | SPY at launch → expand to QQQ, IWM in v2 |
| Leg 1 (short put) | **Sell to open**, target delta `SHORT_LEG_DELTA` |
| Leg 2 (long put) | **Buy to open**, `SPREAD_WIDTH` dollars below short strike |
| DTE at entry | Within `[DTE_MIN, DTE_MAX]` window |
| Order class | Multi-leg combo (Alpaca `OrderClass.MLEG`) — fills both legs atomically or neither |

Both legs in one order means **no leg risk** — if the broker can't fill both at the combined credit, the order fails cleanly, no half-trade orphaned. This is materially safer than legging in separately.

### Concrete example

SPY at $737, view: "SPY will stay above $720 for the next 30 days."

| Leg | Action | Strike | DTE | Premium |
|---|---|---|---|---|
| Short put | Sell to open | $720 (~17 delta) | 30 days | Receive $4.00 |
| Long put | Buy to open | $710 | 30 days | Pay $2.50 |

- **Net credit: $1.50/share × 100 = $150 collected on day one**
- **Collateral held: $850** (= max loss = width × 100 − credit)
- **Outcome at expiration:**
  - SPY > $720 → both puts expire worthless, keep $150 (≈ 80% probability case)
  - SPY at $715 → short put ITM by $5, net P&L = $150 − $500 = −$350
  - SPY ≤ $710 → max loss = $850 (long put kicks in at $710, capping further damage)

In practice the position would close at +50% credit (~$75 profit) much earlier than expiration, freeing the collateral to redeploy. See §4.

### What this is NOT

- **Not a covered call.** Covered calls require owning 100 shares of the underlying ($73,700 at SPY $737).
- **Not a cash-secured put.** Cash-secured puts require the full strike × 100 in cash collateral ($72,000 at strike $720).
- **Not a margin loan.** Alpaca holds the $850 max-loss amount in *your* cash as collateral. You aren't borrowing — your own cash is locked up against the defined max loss until the position closes. This is Level 3 options approval, supported on cash accounts.

---

## 3. Entry rules

A new position opens when **all** of the following are true:

1. **Regime gate (allocator-level):** current regime in `{TRENDING, RANGING}` — never sell puts in BEAR or VOLATILE. Enforced via `StrategySlot.allowed_regimes` exactly like existing strategies.
2. **SPY trend gate:** SPY close > 50 SMA — don't sell puts into a downtrend.
3. **Volatility gate:** VIX > `MIN_VIX_FOR_ENTRY` (default 14) — only sell premium when it's worth selling. VIX as an IV-rank proxy because Alpaca paper Greeks are unreliable.
4. **DTE availability:** at least one expiration in the chain falls within `[DTE_MIN, DTE_MAX]`.
5. **Spread availability:** the multi-leg ranker (§5) returns a valid pick that fits the sleeve budget.
6. **Concurrent position cap:** open positions in this strategy < `MAX_CONCURRENT_POSITIONS`.
7. **Same-cycle expiration cap:** at most `MAX_PER_EXPIRATION` (default 1) open position per (underlying, expiration_date) tuple — prevents stacking three spreads on the same Friday and getting hammered by a single event.
8. **DTE staggering:** new entry's expiration ≥ `MIN_DTE_GAP_BETWEEN_OPENS` days from the most recent existing position's expiration — forces calendar diversification.

No earnings filter (SPY is an index, not a single name). No FOMC/NFP filter in v1 — easy to add later if backtests show it matters.

---

## 4. Exit rules

A position closes when **any** of the following triggers fire:

| Trigger | Condition | Rationale |
|---|---|---|
| **Profit target** | Spread mid-price ≤ `PROFIT_TARGET_PCT` × initial credit received | Empirical: closing at 50% of max profit dominates "hold to expiration" on Sharpe. tastytrade-validated across 10+ years of data. |
| **Stop loss** | Spread mid-price ≥ `STOP_LOSS_MULTIPLE` × initial credit received | Defines a soft stop at 2× credit so you don't ride losers to max loss. |
| **Time stop** | DTE ≤ `TIME_STOP_DTE` (default 21) | Gamma exposure becomes punishing in the last 3 weeks — exit regardless of P&L. |
| **Short strike breach** | SPY close ≤ short strike | Trade thesis is broken; defined risk already capped loss but freeing capital beats riding it out. Configurable via `EXIT_ON_SHORT_STRIKE_BREACH`. |
| **Regime exit** | Regime shifts to BEAR mid-trade | Defensive override. Exits are never blocked by regime gate; new entries are. |

### Exit mechanics

Exits use **closing combo orders** at the spread bid (you're buying back what you sold, so you pay the ask of the spread — or equivalently sell the spread at the bid). No mid-market negotiation in v1; if the spread doesn't fill within `LIMIT_TIMEOUT_SECONDS`, escalate to market.

---

## 5. Two-leg contract picker

The 11.25 ranker handles single-leg call picking. Credit spreads need a **paired-leg** picker.

### Design — extend the existing ranker (preferred)

`utils/options_ranker.py` already has `Candidate`, `Quote`, `ScoredPick`, `rank_call_candidates`. Add:

- `SpreadCandidate(short_leg: Candidate, long_leg: Candidate, width: float, net_credit: float)`
- `rank_put_spread_candidates(candidates, quotes, *, target_delta, max_loss_per_position, ...)`

Pure logic, no I/O. Same module so future spread strategies (iron condors = 2 spreads, calendars = different DTEs) reuse primitives.

### Hard filters (drop before scoring)

- **Affordability** — max loss ≤ `max_loss_per_position` (from sleeve allocator)
- **Min credit** — net credit ≥ `MIN_CREDIT_PCT_OF_WIDTH` × spread width (default 25%); reject thin credits below this floor
- **Valid quotes** — both legs bid > 0, ask > 0, ask ≥ bid
- **Short leg delta window** — within `[SHORT_LEG_DELTA − 0.05, SHORT_LEG_DELTA + 0.05]`

### Composite score (0.0–1.0)

| Weight | Factor |
|---|---|
| 0.40 | Short-leg delta proximity to `SHORT_LEG_DELTA` target |
| 0.30 | Net credit relative to width (more credit = better) |
| 0.20 | Combined spread quality (mean of both legs' bid/ask spread%) |
| 0.10 | DTE proximity to mid-DTE target |

### Delta estimation (no reliable Greeks from Alpaca paper)

Use `blackscholes` (already in requirements) with:
- Underlying = SPY close
- Strike = candidate strike
- Risk-free rate = 5.0% (config constant, refresh quarterly)
- Volatility = `VIX × adjustment_factor` (VIX is 30-day forward IV on SPX; for 30–45 DTE SPY puts the relationship is close enough for delta targeting)
- DTE = expiration − today

Approximate but fine for selection — the goal is "find ~17 delta," not "compute delta to 4 decimals."

---

## 6. Position sizing & multi-position management

This is where the strategy differs most from the current `spy_options_reversion`.

### Per-position size

```
max_loss_per_position = SPREAD_WIDTH × 100 − net_credit_received
collateral_required   = max_loss_per_position   (Alpaca holds in cash)
```

For a $10-wide spread with $150 credit: `max_loss = $850`, `collateral = $850`.

### Concurrent positions

Up to `MAX_CONCURRENT_POSITIONS` open at once. Sleeve allocator enforces:

```
total_collateral_in_use ≤ SPY_CREDIT_SPREAD_SLEEVE_BUDGET
```

If a new signal fires but the sleeve is full, the signal is rejected with `SLEEVE_FULL` — same code path as existing allocator rejection, fires `order_rejection` alert.

### Diversification within the sleeve

Three constraints prevent the sleeve from concentrating risk on one event:

1. `MAX_PER_EXPIRATION` (default 1) — at most one open position per expiration date per underlying
2. `MIN_DTE_GAP_BETWEEN_OPENS` (default 7) — don't open two spreads on consecutive Fridays; stagger entries across the calendar
3. When multi-underlying comes online (QQQ, IWM): `MAX_PER_UNDERLYING` (default 3)

### Sleeve sizing — recommendation

Honest math: at 5–10 concurrent spreads on SPY with $850/position, the sleeve needs to be $5k–$10k.

**Recommendation: split the existing 5% options sleeve into two strategy sleeves:**

| Sleeve | Allocation | Strategy |
|---|---|---|
| `spy_options_reversion` (current long-call) | 2% | Continues running as plumbing-validation + occasional directional bet |
| `spy_credit_spread` (new) | 8% | New primary options edge |

At 8% of a $108k equity → $8,600 budget. Comfortably runs 5–8 concurrent spreads with max-loss headroom. **Allocator config change only; no code change for the split itself.**

Decision on whether to fully replace `spy_options_reversion` waits for the 11.26 audit results.

---

## 7. Tuning knobs

Every parameter lives in `config/settings.py` under a `SPY_CREDIT_SPREAD_*` namespace. No hardcoded values inside the strategy.

### Entry-side knobs

| Knob | Default | Direction → effect |
|---|---|---|
| `SHORT_LEG_DELTA` | 0.17 | Higher (0.20–0.25) → more credit per trade, lower win rate, larger losers |
| `SPREAD_WIDTH` | 10 ($) | Wider (15–25) → bigger absolute credit & max loss, same risk:reward ratio |
| `DTE_MIN` | 30 | Lower → less theta benefit, more gamma |
| `DTE_MAX` | 45 | Upper bound on chain candidates |
| `MIN_VIX_FOR_ENTRY` | 14 | Higher → trade less, only when premium is rich; lower → trade more, in cheap-premium environments |
| `MIN_CREDIT_PCT_OF_WIDTH` | 0.25 | Floor — reject spreads where credit < 25% of width (poor risk:reward) |

### Position-management knobs

| Knob | Default | Direction → effect |
|---|---|---|
| `MAX_CONCURRENT_POSITIONS` | 5 | Higher → more capital deployed, more risk in a vol spike |
| `MAX_PER_EXPIRATION` | 1 | Concentration cap per Friday |
| `MIN_DTE_GAP_BETWEEN_OPENS` | 7 | Forces staggered entries across the calendar |
| `MAX_PER_UNDERLYING` | 3 | Relevant once multi-underlying lands |

### Exit-side knobs

| Knob | Default | Direction → effect |
|---|---|---|
| `PROFIT_TARGET_PCT` | 0.50 | Lower (0.30–0.40) → close faster, smaller wins, higher turnover; higher → ride for more profit, gamma risk |
| `STOP_LOSS_MULTIPLE` | 2.0 | Lower → exit losers earlier, smaller drawdowns, more whipsaw exits |
| `TIME_STOP_DTE` | 21 | Lower (14–18) → less gamma; higher → more time for thesis to play out |
| `EXIT_ON_SHORT_STRIKE_BREACH` | True | False → ride defined-loss to expiration if breach happens |
| `LIMIT_TIMEOUT_SECONDS` | 30 | Wait time before escalating to market order |

### Ranker weights

| Knob | Default |
|---|---|
| `RANKER_WEIGHT_DELTA` | 0.40 |
| `RANKER_WEIGHT_CREDIT` | 0.30 |
| `RANKER_WEIGHT_SPREAD_QUALITY` | 0.20 |
| `RANKER_WEIGHT_DTE` | 0.10 |

---

## 8. Expected behavior (paper-projected)

Honest expectations based on documented research, **not promises**:

| Metric | Expected range |
|---|---|
| Win rate (per trade) | 72–80% |
| Average winner | ~50% of credit received |
| Average loser | 1.5–2.5× credit received |
| Expectancy per trade | Positive but small (~+25% of max profit on average) |
| Max drawdown (sleeve) | 20–35% in a vol-expansion event (e.g., Aug 2024-style spike) |
| Annual sleeve return | 25–45% in normal conditions |
| Worst quarter | −10% to −25% of sleeve, plausibly more if positioned wrong into a crash |

### Critical caveat — negative skew

This strategy has **negative skew**: many small wins, occasional large losers. Sharpe is high but the path is bumpy. There will be weeks where the sleeve looks ugly even while expectancy holds long-term. That's the deal with selling premium. **Acceptance of this characteristic is a prerequisite for running the strategy.** Anyone who can't sit through a 20% sleeve drawdown without intervening will sabotage the edge.

### What "moves the needle" actually looks like

At default settings, 5 concurrent spreads at $150 credit each, 50% profit target, 12–15 trade cycles/year:

- Sleeve P&L: $4,500–$6,500/year
- Sleeve capital deployed: ~$4,500 average (with 5 concurrent at $850 each, but rotation frees capital)
- **Return on sleeve: ~70–100%**
- **Contribution to overall $108k portfolio: ~4–6%/year**

Levers to scale up further (each with explicit risk tradeoff) documented in §7.

---

## 9. Infrastructure changes

### Required new engine work

1. **Multi-leg order support in `execution/broker.py`** — wrap Alpaca's `MLEG` order class. The current broker handles only single-leg orders. ~100–150 LOC plus integration tests.
2. **`OptionsExecutionWorker` extension** — accept multi-leg legs spec, submit as combo, track combined fill confirmation. Existing async worker pattern carries over.
3. **Trade DB schema** — add `spread_id` column (UUID grouping the two legs as one logical position) and `position_type` (`'single_leg'` or `'spread'`). Migration-safe column adds.
4. **`_position_owners` keying** — current keying by OCC works for single legs. For spreads, key by `spread_id` and store both legs as a paired attribute. Cleaner option: introduce a position abstraction that hides single-vs-spread from the engine.

### Reusable from 11.25 / current infrastructure

- `utils/options_ranker.py` — extend with spread-pair scoring (Option A in §5)
- `utils/options_lookup.py` — chain query, pagination, K-nearest filter all reused
- OPRA snapshot quote lookup — reused, batched for both legs
- Sleeve allocator — reused with new strategy entry
- Engine cycle loop, regime gating, watchlist plumbing — unchanged
- `OptionTradeRejected` / structured rejection — reused
- Stream manager + fill watcher — extends to handle combo events
- 100× P&L multiplier, `_OCC_PAT` gating — already in place

### Estimated effort

| Component | LOC | Complexity |
|---|---|---|
| Strategy module (`strategies/spy_credit_spread.py`) | ~400 | Medium |
| Edge filter (`strategies/filters/spy_credit_spread.py`) | ~80 | Low |
| Ranker extension (spread-pair scoring) | ~150 | Medium |
| Picker extension (multi-leg selection from chain) | ~120 | Medium |
| Broker multi-leg order method | ~100 | Medium |
| OptionsExecutionWorker spread variant | ~150 | Medium-high |
| Trade DB schema migration | ~30 | Low |
| Engine wiring | ~50 | Low |
| Unit tests | ~600 (40–50 tests) | Medium |
| Integration verify script | ~200 | Medium |

**Total: ~1,900 LOC, 2–3 focused days of work.** Single PR — too interlinked to split cleanly.

---

## 10. Tests required

### Strategy unit tests (`tests/test_spy_credit_spread.py`)

- Entry rules — each gate (regime, SMA, VIX, DTE, spread availability) independently
- Position limits — opens up to `MAX_CONCURRENT`, rejects beyond
- Expiration concentration — `MAX_PER_EXPIRATION` enforced
- DTE staggering — `MIN_DTE_GAP_BETWEEN_OPENS` enforced
- Profit target trigger — exits at 50% credit
- Stop loss trigger — exits at 2× credit
- Time stop — exits at `TIME_STOP_DTE` regardless of P&L
- Short strike breach — exits when SPY ≤ short strike
- Regime exit override — exits even when entries are blocked

### Ranker extension tests (`tests/test_options_ranker.py` extended)

- Spread-pair scoring formula — verify weights
- Affordability filter — drops spreads where max loss > budget
- Min-credit filter — drops thin spreads
- Delta target window — short leg within ±0.05 of target

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
2. **Sleeve split** — accept the proposed 2% (long-call) + 8% (credit spread) split, or different ratio? Replace `spy_options_reversion` entirely?
3. **Initial max concurrent positions** — start with 3 and ramp up after first 20 trades, or go to 5 from day one?
4. **Multi-underlying timing** — SPY only at launch, or wire SPY + QQQ together at v1? QQQ doubles trade frequency but adds complexity.
5. **Run alongside or replace** — keep `spy_options_reversion` running for the 11.26 audit, then decide based on data? Or pull the long-call strategy now since it's known plumbing rather than edge?
6. **Tuning bias** — defaults bias toward (a) safety (17Δ short, $10 wide, manage at 21 DTE) or (b) profitability (22Δ short, $15 wide, manage at 14 DTE)? Defaults above are (a) — confirm or override.

---

## 12. Acceptance criteria

This is **done** when:

- [ ] All entry/exit rules pass unit tests
- [ ] Multi-leg orders submit and fill cleanly on Alpaca paper
- [ ] Position ownership survives restart (durable ownership extended to spreads)
- [ ] Sleeve allocator respects new strategy's budget and concurrent caps
- [ ] Dashboard shows open spreads with both legs visible
- [ ] First 5 paper trades complete (any P&L) without infrastructure errors
- [ ] PR description includes a paper-watch follow-up item (analogous to 11.26) for the first 30 fills with defined tuning triggers

---

## 13. Implementation sequence

When picked up:

1. Capture answers to §11 in the PLAN.md row for this strategy
2. Branch from main as `feat/spy-credit-spread-strategy`
3. Build in order:
   1. Ranker extension (spread-pair scoring) + unit tests
   2. Multi-leg picker (chain query → ranker)
   3. Strategy module + edge filter + unit tests
   4. Broker multi-leg order method + unit tests
   5. OptionsExecutionWorker spread variant
   6. Trade DB schema migration
   7. Engine wiring (sleeve allocator entry, `build_option_execution` adapter)
   8. Integration verify script
4. One PR for the whole thing
5. Recycle the bot once merged; first SPY entry is the validation moment
6. Define paper-watch follow-up task in PLAN.md (analogous to 11.26)

---

## 14. Why this is a good "next options strategy"

A summary for the decision review:

| Factor | Current `spy_options_reversion` | Proposed `spy_credit_spread` |
|---|---|---|
| Trade direction | Long premium (pay theta) | Short premium (collect theta) |
| Win rate (theoretical) | ~40–50% — needs directional accuracy | ~75–80% — needs SPY to stay above strike |
| Per-trade capital | ~$1k–$2k (one contract) | ~$850 (per spread, defined max loss) |
| Risk profile | Large win/large loss, symmetric | Many small wins / occasional large loss (negative skew) |
| Edge source | Mean-reversion timing (fights drift) | Volatility risk premium (aligned with drift) |
| Sensitivity to time decay | Hurts | Helps |
| Scalability | Capped (1 contract at a time at 5% sleeve) | Scales with concurrent positions and sleeve size |
| Empirical edge support | Mixed for retail long-premium | Strong for retail short-premium with defined risk |

The credit spread isn't a magic bullet. It will lose money some months. But the math is structurally aligned with what works in retail options trading, and the plumbing investment from 11.25 makes the next step (this strategy) cheaper to build than the first one was.
