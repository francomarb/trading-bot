# SMA Crossover — Optimization Notes

**Status:** Living document. Audit findings, completed experiments, and
ranked optimization opportunities for the SMA crossover sleeve.

**Last updated:** 2026-06-06

For the strategy specification (signal logic, deployment configuration,
filter stack, methodology), see
[`sma_crossover_strategy.md`](sma_crossover_strategy.md). This document
is *findings-tracking*: it records what we've measured, what we've ruled
out, and what's worth investigating next.

---

## Empirical baseline (2018-11 → 2026-06)

Source: `scripts/sma_giveback_audit.py`. Walks every 20/50 round trip on
daily Alpaca IEX bars across the full historical SMA_WATCHLIST. Unit
shares; no filter overlays applied to the simulation (pure strategy
mechanics with the production ATR stop at 2.0 × ATR(14)).

### Trade outcomes (546 round trips, 40-name universe)

| | Count | Notes |
|---|---|---|
| Total round trips | 546 | |
| Exited at ATR stop | 336 (62%) | False-start entries |
| Exited at death cross | 210 (38%) | Trend ran to its natural end |
| ↳ death-cross winners | 174 | 83% win rate *given* death-cross exit |
| ↳ death-cross losers | 36 | Signal flipped before stop got hit |

**Headline win rate across all entries: 31.9%.** Low-win-rate, high-skew
strategy by design — most entries fail; a few runners pay for everything.

### Profit concentration

| Tier | Symbols | Net P&L (per-share units) |
|---|---|---|
| Top 5 (ASML, CAT, STRL, STX, MU) | 5 | **+$1,871 (70% of total)** |
| Middle 15 | 15 | +$956 |
| Bottom 20 | 20 | **−$149 (net loss)** |
| ↳ chronic losers (negative net) | 6 (ADBE, ALB, VSAT, INTC, CIEN, VIAV) | combined drag |

Total net across 40 symbols: **+$2,678** per-share-unit.

This is the textbook signature of a trend-following strategy: a handful
of monster runners subsidize all the choppy losing trades. The watchlist
composition is the dominant lever on overall P&L.

### Giveback (winners only)

On the 174 winning death-cross trades, profit surrendered from the
high-water mark to the death-cross exit:

| | % of peak open profit |
|---|---|
| Median | 44.2% |
| Mean | 43.8% |
| P75 | 64.4% |
| P90 | 81.6% |
| Max | 99.9% |

**Capture ratio: 61.5% of peak open profit on average.** The strategy
collects ~$0.61 of every $1.00 of unrealized gain it creates. The
remainder is the price paid for not exiting early.

---

## Completed experiments (all ruled out)

Three alternative exit rules were tested against the death-cross baseline
on the 174 winning trades. Each one **lost** money vs. baseline; the
death-cross exit is the empirical optimum on this universe.

### 1. Chandelier ATR trail (HWM − K · ATR)

| K | Captured | Δ vs baseline | Bit early on |
|---|---|---|---|
| Baseline (death cross) | $4,917 | — | — |
| 2.5 | $2,181 | −$2,736 (−55.6%) | 157/174 winners (90%) |
| 3.0 | $2,427 | −$2,489 (−50.6%) | 154/174 |
| 3.5 | $2,777 | −$2,140 (−43.5%) | 147/174 |

**Why it fails:** SMA winners typically push +5 ATR, pull back 3–5 ATR
during consolidation, then resume up. The trail catches the consolidation
and treats it as reversal. The 20/50 cross is slow enough to ride through.

### 2. Profit-gated trail (arm only after profit ≥ N · ATR)

| Activation | Trail K | Captured | Δ vs baseline |
|---|---|---|---|
| 3 ATR | 5 ATR | $3,967 | −$950 (−19%) |
| 4 ATR | 5 ATR | $4,024 | −$892 (−18%) |
| 5 ATR | 5 ATR | $4,041 | −$876 (−18%) |

**Why it fails:** even with a 5-ATR activation, the trail bites early on
71% of armed winners. The pullback-then-resume pattern is too common in
this universe for any trail-based rule to survive.

### 3. Fixed-% take-profit

| Target | Captured | Δ vs baseline |
|---|---|---|
| +10% | $1,593 | −$3,214 (−67%) |
| +50% | $3,693 | −$1,114 (−23%) |
| +100% | $4,396 | −$411 (−8.5%) |
| +150% | $4,775 | −$32 (−0.7%) |

**Why it fails:** the strategy's profit comes from a small number of
+200%–+400% runners. Even a +150% target caps the tail and bleeds value;
tighter targets bleed disastrously.

### What NOT to revisit

- ❌ **Adding a trailing stop** of any flavor (chandelier, gated, EMA).
- ❌ **Adding a fixed-% take-profit** (defensible only as a behavioral
  "sleep well" rule at +150%, where the cost is near-zero but so is the
  benefit).
- ❌ **Faster MA exits** (e.g., close below 20 EMA) — same failure mode
  as the trails.
- ❌ **Adding more entry signals on top of crossover** — dilutes the
  signal. The strategy works *because* it is patient.

The full reproducible test is `scripts/sma_giveback_audit.py`; rerun if
the universe materially changes or someone proposes a new exit variant.

---

## Optimization opportunities (ranked by leverage)

### 1. Watchlist composition — HIGHEST LEVERAGE

Profit concentration analysis: **70% of lifetime profit comes from 5 of
40 names**, and the bottom 20 collectively *lose* money. Every dollar
spent improving watchlist composition is worth more than every dollar
spent on entry/exit logic.

| | Status | Action |
|---|---|---|
| Cull chronic underperformers | ✅ DONE 2026-06-06 | Removed VIAV, VSAT, CIEN, ALB, INTC. ADBE retained on operator AI-rebound thesis. |
| Quarterly automatic regeneration | TODO | Schedule `scripts/sma_watchlist_scan.py` via cron / scheduled task. Diff before promoting to settings. |
| Scanner ranking criterion audit | TODO | Audit `sma_watchlist_v2` rule in `sma_watchlist_scan.py`. Does it select for *trend-friendliness* (clean directional moves, high ATR-adjusted returns) or just historical price action? |
| Chronic-loser eject rule | TODO | Add a scanner rule: any name with negative net P&L on the strategy over a rolling 3y window is auto-excluded from the next regeneration. |

### 2. Filter calibration — MEDIUM-HIGH LEVERAGE

**62% of all entries die at the ATR stop**, not at the death cross. These
are false-start entries — the crossover fired but the trend never
materialized. The filter stack exists to cut these; the question is
whether it's tight enough.

| | Status | Action |
|---|---|---|
| Replay false starts with stricter filters | TODO | Replay the 546 historical entries with stricter filter configs (higher volume-expansion threshold, hotter sector floor, longer SPY trend window). For each config, report *winners blocked* vs *losers avoided*. |
| Symmetric false-negative check | TODO | Are any current gates blocking eventual winners? E.g., winners that entered on a NEUTRAL-sector bar that rotated HOT shortly after — `SectorMomentumFilter` would have blocked them. |

### 3. Regime allocation — MEDIUM LEVERAGE

The strategy currently runs in `TRENDING` + `RANGING`. The audit did not
bucket P&L by regime. If `RANGING` is mostly false starts, dropping it
would tighten the sleeve and free regime-aware capital for other
strategies.

| | Status | Action |
|---|---|---|
| Bucket the 546 audit trades by regime-at-entry | TODO | Report win rate, expectancy, and giveback per regime. |
| Drop dead regimes if signal is clear | TODO | If a regime is clearly negative-expectancy, gate the sleeve more tightly and rebalance `SLEEVE_TARGETS`. |

### 4. Position sizing — MEDIUM LEVERAGE

Current sizing is fixed-fractional: every position risks the same dollar
amount at the ATR stop. Since edge concentrates in a small set of
high-ATR names (ASML, MSTR, MU), equal-risk sizing *systematically
under-sizes the names that pay*. Sizing-up in advance requires knowing
the winners — which is the watchlist problem again.

| | Status | Action |
|---|---|---|
| Vol-scaling experiment | TODO (lower priority) | Cap exposure on the wildest names so a single MSTR/MU trade can't dominate sleeve drawdown. Equal-risk in *cash* terms, not in ATR terms. |
| Kelly-weighted sizing | NOT NOW | Deferred to Phase 11 per project memory (`feedback_kelly_rsi.md`); becomes useful for capital *allocation* across sleeves, not within SMA. |

### 5. Parameter robustness — LOW PRIORITY (overfit risk)

Is (20, 50) the right pair? Run the backtest harness's
`parameter_sensitivity()` as a **sanity check**, not an optimization. If
(20, 50) sits on a plateau of good values, we're fine. If it's a sharp
spike with bad neighbors, we're overfit and need to back off.

| | Status | Action |
|---|---|---|
| Walk-forward sweep | TODO | `fast ∈ {10, 15, 20, 25, 30} × slow ∈ {40, 50, 70, 100, 150, 200}` with `walk_forward()` in `backtest/runner.py`. Report **out-of-sample stability**, not in-sample best. |

---

## Change log

| Date | Change | Source / Rationale |
|---|---|---|
| 2026-06-06 | Created this doc | Capture findings from giveback audit; separated from strategy spec doc per pattern of other sleeves. |
| 2026-06-06 | Removed VIAV, VSAT, CIEN, ALB, INTC from `SMA_WATCHLIST` | Chronic losers over 7.5y backtest (negative cumulative P&L, no producing runner). ADBE retained on operator thesis: AI-driven sell-off similar to NOW, expected to mean-revert as Adobe's GenAI products land. |

---

## Reproducing the audit

```bash
# Full audit (giveback distribution + chandelier + gated trail + take-profit overlays)
venv/bin/python -m scripts.sma_giveback_audit

# Per-symbol P&L breakdown (used for chronic-loser identification)
# — embedded in the script's symbol loop; see `simulate_symbol()` returns.
```

Both run offline once the IEX daily cache is warm (~1 minute end-to-end).
No live API calls beyond cache top-up.

## Related docs

- [`sma_crossover_strategy.md`](sma_crossover_strategy.md) — strategy
  spec and deployment guide.
- [`sma-watchlist-selection.md`](sma-watchlist-selection.md) — watchlist
  selection rules (spec-stable).
- [`SMA-edge-filter.md`](SMA-edge-filter.md) — edge filter design.
