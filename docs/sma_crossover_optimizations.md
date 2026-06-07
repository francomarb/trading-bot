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

Source: `scripts/sma_giveback_audit.py` on the **pinned 40-symbol
`AUDIT_UNIVERSE`** (frozen at the time of the original audit; not
`settings.SMA_WATCHLIST` which drifts). Daily Alpaca IEX bars.

### Methodology disclosures

This audit measures *strategy mechanics in isolation*. It does **not**:

- apply production entry filters (`SMAEdgeFilter`, `SectorMomentumFilter`,
  `SPYTrendFilter`, regime gate, earnings blackout) — every signal bar
  trades;
- model position sizing — unit shares throughout, so $-magnitudes are
  comparable across policies but not to live equity-curve impact;
- perform walk-forward / OOS validation — the same window generates
  signals and evaluates policies;
- model gap-through-stop fills accurately — a violent gap-down fills at
  the stop level, not at the gap-down open (`_fill_through_stop` in the
  script pins this assumption with a unit test).

Any operational decision (e.g., culling a name from production)
**requires an audit that addresses these limits.** See the
[Methodology gates](#methodology-gates-before-acting-on-this-audit)
section below.

### Trade outcomes (571 entries, 40-name pinned universe)

Run `venv/bin/python -m scripts.sma_giveback_audit --universe audit`
to reproduce.

| | Count | Notes |
|---|---|---|
| Total entries | 571 | Every 20/50 golden cross, non-overlapping |
| Exited at ATR stop | 336 (59%) | False-start entries |
| Exited at death cross | 209 (37%) | Trend ran to its natural end |
| Still open at end-of-data | 26 (5%) | `reason="eod"` — not silently dropped |
| ↳ death-cross winners | 174 | 83% win rate *given* death-cross exit |

**Baseline net P&L across all 571 entries: $8,277 (per-share unit).**
**Headline win rate: 34.9%** (199 of 571 trades positive). Low-win-rate,
high-skew strategy by design — most entries fail; a few runners pay for
everything.

> **Note on earlier headline numbers.** An earlier version of this doc
> reported "546 round trips" and "$4,917 captured." Both were artifacts
> of the previous script structure: 546 excluded the 26 EOD-open
> positions (silently dropped), and $4,917 was the sum across **death-
> cross winners only** rather than all entries — making the
> alternative-exit comparison apples-to-oranges. The unified-policy
> script fixes both. The qualitative conclusions are unchanged; the
> exact numbers in the comparison tables below shifted.

### Profit concentration

Reproducible from the per-symbol baseline section of
`scripts/sma_giveback_audit.py` output (`--universe audit`).

| Tier | Symbols | Net P&L (per-share units) |
|---|---|---|
| Top 5 | SNDK, ASML, STRL, MU, STX | **+$5,004 (60% of total)** |
| Middle 15 | Range +$148 to +$585 | +$3,051 |
| Bottom half (20 symbols) | Range −$132 to +$92 | +$222 |
| ↳ net-negative names | 5: VIAV, CIEN, VSAT, ALB, ADBE | combined −$263 |

Total net across 40 symbols: **+$8,277** per-share unit (baseline policy
across all 571 entries, no production filters, no sizing).

This is the textbook signature of a trend-following strategy: a handful
of monster runners subsidize all the choppy losing trades. The watchlist
composition is the dominant lever on overall P&L.

> **A note on SNDK in the top 5.** SNDK shows the highest single-name
> contribution ($1,715 on 2 trades) but is a *tiny sample*. Both of its
> trades won; the apparent dominance is more luck than signal. A
> filter-aware audit (see *Methodology gates* below) would likely
> reweight which names actually contribute reliably vs. which got
> lucky on a small N.

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

Three families of alternative exit rules were tested under the **unified
policy comparison** — every entry replayed under each *complete* exit
policy (death cross + 2.0×ATR disaster stop + overlay), with aggregate
net P&L on all 571 entries as the comparison metric. Each policy lost
to the baseline; the death cross is the empirical optimum on this
universe.

### 1. Chandelier ATR trail (HWM − K · ATR)

| K | Net P&L | Δ vs baseline | Win rate | Avg trade |
|---|---|---|---|---|
| Baseline (death cross + 2.0 ATR) | $8,277 | — | 34.9% | $14.50 |
| 2.5 | $2,011 | −$6,266 (−76%) | 44.0% | $3.52 |
| 3.0 | $2,444 | −$5,832 (−70%) | 41.3% | $4.28 |
| 3.5 | $2,813 | −$5,464 (−66%) | 41.0% | $4.93 |

**Why it fails:** SMA winners typically push +5 ATR, pull back 3–5 ATR
during consolidation, then resume up. The trail catches the consolidation
and treats it as reversal. The 20/50 cross is slow enough to ride through.
Win rate goes *up* (a chandelier exits some atr-stop losers at a smaller
loss) but the cumulative loss-of-runner-upside dwarfs the savings.

### 2. Profit-gated trail (arm only after profit ≥ N · ATR)

| Activation | Trail K | Net P&L | Δ vs baseline | Win rate |
|---|---|---|---|---|
| 3 ATR | 5 ATR | $4,004 | −$4,273 (−52%) | 36.8% |
| 4 ATR | 5 ATR | $4,032 | −$4,245 (−51%) | 37.0% |
| 5 ATR | 5 ATR | $4,026 | −$4,251 (−51%) | 37.7% |
| 4 ATR | 3 ATR | $2,595 | −$5,682 (−69%) | 40.6% |
| 5 ATR | 4 ATR | $3,175 | −$5,102 (−62%) | 37.7% |

**Why it fails:** even with a 5-ATR activation, the trail bites early on
the pullback-then-resume pattern. Wider trails (K=5) help but still
underperform — once the trail arms, *any* consolidation deeper than the
trail distance exits before the runner finishes.

### 3. Fixed-% take-profit

| Target | Net P&L | Δ vs baseline | Win rate | Avg trade |
|---|---|---|---|---|
| +10% | $837 | −$7,440 (−90%) | 49.7% | $1.47 |
| +20% | $1,797 | −$6,480 (−78%) | 38.7% | $3.15 |
| +30% | $2,295 | −$5,981 (−72%) | 36.1% | $4.02 |
| +50% | $3,464 | −$4,813 (−58%) | 35.2% | $6.07 |
| +75% | $4,538 | −$3,739 (−45%) | 34.9% | $7.95 |
| +100% | $5,183 | −$3,094 (−37%) | 34.9% | $9.08 |
| +150% | $5,946 | −$2,331 (−28%) | 34.9% | $10.41 |

**Why it fails:** the strategy's profit comes from a small number of
+200%–+400% runners. Every TP threshold tested caps that tail.
Interestingly, *even with the selection-bias fix*, no take-profit
threshold approaches the baseline — earlier doc said +150% was within
$32 of baseline, but that was comparing winner-only sums. On the full
571-entry sample, +150% still loses $2,331 because some "losing" trades
that briefly popped above +150% never had a chance to roll into the
catastrophic ones.

### Behavioral observation worth flagging

Look at the **win rate** column. Tight take-profits push win rate from
35% → 50%. That *feels* great psychologically — you're "right" more
often. The math is the opposite: avg trade collapses from $14.50 to
$1.47. This is the textbook trend-follower psychology trap: small wins
feel like skill; the rare monster runner that pays for all of it gets
sliced.

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
| Cull chronic underperformers | ⛔ DEFERRED 2026-06-06 | Briefly removed VIAV, VSAT, CIEN, ALB, INTC; reverted after reviewer correctly noted the audit was unit-share, unfiltered, and in-sample — see *Methodology gates* below. Re-promote only after the gated audit signs off. |
| Quarterly automatic regeneration | TODO | Schedule `scripts/sma_watchlist_scan.py` via cron / scheduled task. Diff before promoting to settings. |
| Scanner ranking criterion audit | TODO | Audit `sma_watchlist_v2` rule in `sma_watchlist_scan.py`. Does it select for *trend-friendliness* (clean directional moves, high ATR-adjusted returns) or just historical price action? |
| Chronic-loser eject rule | TODO | Add a scanner rule: any name with negative net P&L on the strategy over a rolling 3y window is auto-excluded from the next regeneration. Must use the filtered/sized audit, not the raw one. |

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

## Methodology gates before acting on this audit

The audit as it stands is sufficient for **directional research questions**
(does adding a trail / take-profit beat the death cross? Probably not.).
It is **not** sufficient for **operational decisions** (e.g., culling a
name from the live watchlist).

Before any operational change is promoted based on this audit, **all
four** of the following must be true:

1. **Filter-aware replay** — the audit must apply the production filter
   stack (`SMAEdgeFilter`, `SectorMomentumFilter`, `SPYTrendFilter`,
   regime gate, earnings blackout) at the entry bar. A name that looks
   terrible in unfiltered sim may be fine once production gates remove
   the bad entries.
2. **Production-equivalent sizing** — fixed-fractional ATR-risk
   position sizing, not unit shares. Per-share P&L can be very different
   from equity-curve impact.
3. **Walk-forward / OOS validation** — the rule deriving the change
   (e.g., "this name's net P&L is negative") must hold *out-of-sample*
   on at least one held-out fold of comparable length to the proposed
   change's expected life.
4. **Operator review** — quantitative signal alone is not enough. The
   operator must sign off after seeing both the quantitative result and
   their independent thesis on the name. (See: ADBE was nominally
   negative in the audit but retained on operator AI-rebound thesis.)

These gates exist because of the 2026-06-06 false-start: a watchlist
cull was promoted based on raw audit P&L, reviewed by ChatGPT, found to
be unsupportable on methodology, and reverted before merge.

## Change log

| Date | Change | Source / Rationale |
|---|---|---|
| 2026-06-06 | Created this doc | Capture findings from giveback audit; separated from strategy spec doc per pattern of other sleeves. |
| 2026-06-06 | **Reverted** removal of VIAV, VSAT, CIEN, ALB, INTC from `SMA_WATCHLIST` | Reviewer correctly identified the audit was unit-share / unfiltered / in-sample; not a sufficient basis for an operational change. Cull deferred until the *Methodology gates* are satisfied. |
| 2026-06-06 | Restructured audit script to a **unified policy comparison** | Earlier version simulated alternative exits only on death-cross winners — selection-biased. New version replays every entry under each *complete* policy; aggregate net P&L is the comparison metric. Headline numbers in this doc updated accordingly. |
| 2026-06-06 | Pinned audit universe (`AUDIT_UNIVERSE` constant in script) | Earlier version read `settings.SMA_WATCHLIST` and would silently drift. Now the documented numbers reproduce exactly via `--universe audit` (default). |
| 2026-06-06 | Entry-bar stop/take-profit honored | Reviewer identified that simulation skipped the entry bar entirely. Production's OTO stop attaches as soon as the parent fills at the open, so the stop can trigger same-day. Loops now check stops + take-profits starting at `entry_idx`; the death-cross check still starts at `entry_idx + 1` (a same-bar cross would only be observed by production on the next engine cycle). Per-symbol numbers shifted: **INTC turned out to be net-positive (+$46) under the corrected policy**, validating the reviewer's broader point that the original cull was not supported by the data. Negative-net names reduced from 6 to 5. |
| 2026-06-06 | Per-symbol output added to `scripts/sma_giveback_audit.py` | Profit-concentration section in this doc is now reproducible by running the audit — addresses reviewer's "script emits no per-symbol P&L table" finding. |

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
