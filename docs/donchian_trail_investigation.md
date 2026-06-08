# Donchian trailing broker stop — investigation (2026-06-06 / updated 2026-06-08 R5)

> **Status: CLOSED — static stop retained.** SIP re-test on 2016-2024
> confirms the IEX-only conclusion. Neither trailing variant clears the
> bar in any reachable regime, including the catastrophic-gap regimes
> (March 2020 COVID crash) that originally motivated the PLAN concern.
> Q4 2018 vol shock is technically under-sampled (6 trades; production
> regime gate kept the strategy out of BEAR Q4 — which is what production
> would also do).
>
> **TL;DR:** Across 2016-04 → 2024-12 on the ai_bigtech universe with
> production-realistic gating (SPY TRENDING + DonchianEdgeFilter), the
> static `entry − 2×ATR` broker stop, a 15-day-low trail, and a
> chandelier `HWM − 3×ATR` trail produce essentially identical
> risk-adjusted returns. On the combined 536-trade run: static +18.4% /
> Sharpe +0.28 / MaxDD −14.3%; low_trail +17.3% / +0.27 / −13.8%;
> chandelier +15.7% / +0.27 / −12.9%. On the COVID-crash-exposed subset
> specifically (16 trades), chandelier UNDER-performs static by 1.19 R
> per trade — the opposite of the hypothesis. Donchian-low trail washes
> vs static everywhere. **The recommendation is to keep the static stop.**

## Revision history

- **2026-06-08 (R5) — SIP re-test landed; closure confirmed.** The active
  P2 retest (post-PR #50 SIP infrastructure) ran on SIP data 2016-04 →
  2024-12. Findings:
  1. **2018 Q4 vol shock**: 6 total trades, 4 crash-exposed. Under the
     PLAN-spec minimum-sample floors (25 total, 10 crash-exposed) →
     closure abstains for this sub-window. The SPY TRENDING-only regime
     gate kept the strategy out of most BEAR Q4 entries (Wall Street's
     worst Q4 since 1931 was largely BEAR regime). The "stop policy is
     moot for trades that don't exist" pattern from R2's 2022 bear window
     repeats here.
  2. **2020 COVID crash**: 53 total trades, 16 crash-exposed — floors met.
     Aggregate: static +1.7%, low_trail +1.5%, chandelier +1.9% (max-min
     0.4 pp > 0.3 pp threshold; Sharpes 0.02 / −0.04 / 0.02). The
     crash-exposed subset is the decisive read: static mean R +1.03,
     low_trail +1.14, **chandelier −0.16**. Chandelier's tighter trail
     stopped out gap-down trades at worse prices than static, the
     opposite of what the PLAN concern hypothesized. Donchian-low trail
     within noise.
  3. **2021-2024 sub-windows**: same pattern as R2 — chandelier gives back
     3.9 pp in the 2023-24 rally (clips trending winners); Donchian-low
     trail washes vs static.
  4. **Combined 2016-04 → 2024-12, 21 syms, 536 trades**: static +18.4% /
     Shp +0.28 / MaxDD −14.3%; low_trail +17.3% / +0.27 / −13.8%;
     chandelier +15.7% / +0.27 / −12.9%. Variants within 0.04 Sharpe and
     1.4 pp MaxDD; chandelier −2.7 pp on returns vs static.

  **Verdict**: The retest confirms the R2 conclusion on a much wider
  dataset (4× the trades, 4.5y deeper history, both catastrophic-gap
  regimes the PLAN concern named). The static stop is retained. PLAN row
  moves from "Active P2 retest" to fully-closed "Parked Ideas" with the
  retest evidence cited.



- **2026-06-07 (R3) — addressed PR #49 third-round audit.** Three more fixes:
  1. **SIP-will-not-help premise was wrong**: empirically AAPL/MSFT/NVDA SIP
     daily bars go back to 2016-01-04 on Alpaca's basic delayed tier.
     The 2018 Q4 vol shock and 2020 COVID crash that originally motivated the
     PLAN concern are reachable on SIP — not on IEX, but on SIP. PR #50
     establishes the SIP backtest infrastructure; the actual SIP retest is
     now tracked as an active P2 roadmap item, not a Parked Ideas footnote.
  2. **SMA200 wasn't populated at 2021-04-01**: stocks' first IEX bar is
     2020-07-27, so April 1, 2021 has only 172 prior bars. SMA200 needs 200
     bars and isn't populated until ~May 11, 2021. The `DonchianEdgeFilter`
     rule 1 fails open for ~5 weeks of the 2021 window. Same biased entries
     fed all three stop variants so the A/B is preserved, but absolute 2021
     numbers (51 trades) over-state production behavior. Documented in §4
     and reflected in the participation table.
  3. **Audit-script docstring sync**: said "fetches from 2021-01-01" while
     the implementation has been at 2018-11-01 for a while. Trivial.

- **2026-06-07 (R2) — addressed PR #49 follow-up audit.** Three additional
  fixes landed on top of R1:
  1. **SPY regime defaults diverged from production (P1)**: my
     `classify_spy_regime` used 252-bar vol window / 90th percentile /
     20-bar SMA slope. Production
     [`RegimeDetector`](../regime/detector.py) uses 126 / 0.80 / 5. Fixed
     defaults and locked parity in
     [`TestRegimeParity`](../tests/test_donchian_trail_sim.py), which
     constructs a `RegimeDetector` with production defaults and asserts
     last-bar parity against the per-bar series classifier across all four
     regime branches.
  2. **DonchianEdgeFilter mask computed on sliced window (P1)**: SMA200
     needs 200 bars of warmup, but each window only carried 50 warmup bars.
     The filter silently failed open on bars where SMA200 was NaN, allowing
     entries production would block. Fix: compute the filter on the full
     cached history per symbol, then `reindex` onto the sliced window.
     Locked in [`TestFilterMaskOnFullHistory`](../tests/test_donchian_trail_sim.py).
  3. **Audit script didn't backfill SPY (P2)**: the documented clean-cache
     reproduction path failed because
     [scripts/audit_donchian_history.py](../scripts/audit_donchian_history.py)
     only fetched `UNIVERSES["ai_bigtech"]`. Added an explicit SPY backfill
     so a reviewer with an empty cache can reproduce in one command.
- **2026-06-07 (R1) — addressed PR #49 first review.** Three fixes landed:
  1. **Warmup leak (P1)**: the simulator was counting entries that fired on
     the pre-window warmup bars in regime metrics. Added `trade_start` so
     indicators warm up on pre-window bars but entries can only fill in-window;
     equity-curve stats now compute on the in-window slice only.
  2. **Missing production gates (P1)**: the simulator ignored SPY TRENDING
     regime and DonchianEdgeFilter entry rules, trading every Donchian high
     as if those gates didn't exist. Added a per-bar SPY regime classifier
     and a per-symbol Donchian filter mask, both passed to the simulator as
     `entry_mask`. Earnings blackout (filter rule 2) remains deferred — see
     §4 *Limitations*.
  3. **Audit-script window date (P2)**: `2021_melt_up` start in
     [scripts/audit_donchian_history.py](../scripts/audit_donchian_history.py)
     was 2021-01-01, but the comparison harness uses 2021-04-01. Fixed.
- **R2 numbers nudged again.** Combined static dropped 213 → 207 trades and
  mean return 5.6% → 4.5% after the regime defaults and full-history filter
  fixes. The qualitative conclusion still holds: no variant clears a
  meaningful margin over static.

## 1. The question

Today's broker-side ATR stop is placed at `entry − 2.0 × ATR_at_entry`
([risk/manager.py:528](../risk/manager.py)) and **never moved**. The strategy
also runs a separate signal-exit rule (`close < rolling 15-day low`) that
liquidates at the next-bar open.

That leaves a hole: in a strong trend, by the time the 15-day low has marched
up to (say) $165 on a name that entered at $100, the broker stop is still
sitting at ~$94. If the name **gaps down overnight** through both the trail's
implicit level and the strategy's exit level, the only fill we get is whatever
the open prints, plus a hard stop way down at $94. The strategy's signal exit
never gets a chance to fire because the close that would trigger it never
happens.

The PLAN P2 entry framed this as a real hole on the AI/big-tech universe and
gated any change behind backtest evidence — paper accumulates too slowly to
A/B-test a stop-design change.

## 2. What we changed in the backtest only

Three protective-stop policies, identical initial-stop distance (so per-trade
sizing is identical across variants):

| Variant | Initial stop | Trail mechanism |
|---|---|---|
| **static_atr** (current production) | `entry − 2×ATR_at_entry` | None — stop never moves |
| **donchian_low_trail** | `entry − 2×ATR_at_entry` | Ratchets up with `rolling_15_low − 0.5×ATR_today` (0.5×ATR wick buffer) |
| **chandelier** | `entry − 2×ATR_at_entry` | Ratchets up with `HWM_close − 3×ATR_today` (textbook LeBeau) |

All other knobs are held constant at the production config: Donchian 30/15
windows, ATR length 14, 5 bps slippage per fill, 2% risk per trade, **SPY
TRENDING-only regime gate**, and **DonchianEdgeFilter rules 1 + 3** (stock >
200 SMA, 20-day avg dollar volume ≥ $20M).

The strategy's **signal exit** (close < 15-day low → next-bar open) is
**unchanged** in all three variants. Only the broker-side protective stop
differs.

## 3. Simulator design

The standard backtest harness ([backtest/runner.py](../backtest/runner.py))
uses `vbt.Portfolio.from_signals`, which only supports a fixed-fraction
`sl_stop` set at entry (with an optional fixed-percent HWM trail). That can't
faithfully model either the 15-day-low trail or a true chandelier where the
ATR distance is recomputed each bar. We therefore built a small custom
simulator in [backtest/donchian_trail_sim.py](../backtest/donchian_trail_sim.py).

Key properties:

- **Production cadence**: stop level for bar t is derived from data through
  bar t-1's close (the live engine replaces the stop after computing the new
  level on yesterday's close).
- **Gap-through semantics**: if `open[t] ≤ stop[t]`, fill at the open (the
  broker can't honor a stop level price has already traded through overnight).
- **Intrabar trigger**: else if `low[t] ≤ stop[t] ≤ high[t]`, fill at the
  stop price.
- **Signal exit takes precedence on its own bar**: if `exits[t-1]` was True,
  the broker stop is cancelled overnight and the position liquidates at
  `open[t]`. This is what the live engine does today.
- **No look-ahead**: stop computation reads ATR and donchian-low aligned to
  yesterday's close. The `add_donchian_low` helper already shifts by 1 so
  reading `donchian_low[t-1]` is identical to the live engine's view at
  t-1 close.
- **Identical sizing across variants**: initial stop distance is the same
  across all three policies, so position size is the same for any given
  entry. The A/B is purely the exit mechanism.
- **Warmup excluded from metrics** (added 2026-06-07): `trade_start` blocks
  entries until the window start while letting indicators warm up using
  pre-window bars; equity-curve stats are computed on the in-window slice
  only.
- **Production gates applied** (added 2026-06-07): `entry_mask` is a boolean
  Series aligned to bars; when False the entry signal is suppressed before
  fill. The comparison harness builds this mask from per-bar SPY regime
  (TRENDING-only) AND per-symbol DonchianEdgeFilter rules.

Coverage: 22 unit tests in
[tests/test_donchian_trail_sim.py](../tests/test_donchian_trail_sim.py) verify
each policy's ratchet behavior, all four fill paths (gap, intrabar, signal,
EOD), no-look-ahead invariants, identical-sizing assertion, and the new
`trade_start` / `entry_mask` gates.

## 4. Universe, coverage, and limitations

Audit script: [scripts/audit_donchian_history.py](../scripts/audit_donchian_history.py).

**Coverage constraint — IEX, partially corrected post-merge** (and now
acknowledged to be partially **wrong** about SIP):

The Alpaca IEX paper-feed coverage for individual `ai_bigtech` mega-caps
begins **2020-07-27** (SPY itself goes back to 2018-11-01). Later-listed
names start at their listing dates. So on IEX, 2018 / 2019 / early 2020
windows are not reachable for a stock-level stop comparison.

**Reviewer correction (PR #49 round-3 audit, 2026-06-07)**: the original
PR claim that "pre-2020 evidence would need Polygon, yfinance, or paid
Alpaca extended history" was **wrong**. Alpaca's basic-tier **delayed
SIP feed** (free, 15-min delay — fine for backtests) returns AAPL /
MSFT / NVDA daily bars from **2016-01-04**, verified empirically
post-review. The 2018 Q4 vol shock and 2020 COVID crash — the exact
regimes the PLAN concern originally cited — **are reachable** on SIP
via the basic tier. The IEX-only investigation simply tested the
wrong window because it inherited an IEX-by-default cache.

**This investigation as it stands** therefore tests 2021-04-01 →
2024-12-31, which covers chop / bear / rally but **not** the
catastrophic-gap regimes that motivated the question. The "park the
change" recommendation applies to the IEX-reachable window; it cannot
honestly close the question for pre-2021 regimes without running on
SIP. PR #50 (feed-aware cache + SIP-default backtest infrastructure)
is the blocking dependency; once it lands, this investigation should be
re-run on the same universe with `feed="sip"` and a 2016-01-01 →
2024-12-31 window to test the trail variants against the gap-down
regimes the question is actually about.

**Additionally — SMA200 gap on the 2021 window** (reviewer P2,
2026-06-07): April 1, 2021 is only 172 trading days after most
stocks' first IEX bar at 2020-07-27. `DonchianEdgeFilter` rule 1
(stock > 200 SMA) needs 200 bars for SMA200 to be computable; before
that the filter fails open. Mature mega-caps' first valid SMA200 is
~May 11, 2021. So for the first ~5 weeks of the 2021 window the filter
allowed entries that production would have evaluated against a real
SMA200. The same biased entries fed all three stop variants, so the
A/B comparison between them is preserved — but the absolute 2021
numbers (51 trades) over-state what production would actually have
traded in that sub-window. This is another reason the SIP re-test
matters: SIP gives 4.5+ years of pre-2021 history per symbol, so
SMA200 is populated comfortably before any 2021 boundary.

Symbol participation within the reachable range:

| Window | Symbols traded / 32 | Notes |
|---|---:|---|
| 2021 melt-up (Q2-Q4) | 28 | Window starts 2021-04-01. ⚠ SMA200 is **not** populated at the window boundary — stocks have only 172 prior bars (first IEX bar 2020-07-27), SMA200 needs 200 → first valid SMA200 ~May 11, 2021. `DonchianEdgeFilter` rule 1 fails open for ~5 weeks of the window. Absolute 2021 numbers (51 trades) over-state production. IREN, CEG, ARM excluded (later listings); RGTI is borderline. SIP retest removes this gap (4.5y+ pre-window history per symbol). |
| 2022 bear | 29 | IREN, CEG, ARM excluded |
| 2023 rally | 31 | ARM only excluded (Sep 2023 IPO) |
| 2024 rally | 32 | All names trade |
| Combined 2021-04 → 2024-12 | 28 | Limited by 2021 entrants |

The de-SPAC names (QBTS, RGTI, IONQ) have pre-merger SPAC bars in the feed.
Those bars participate as ordinary OHLC; we did not strip them. The aggregate
results are not visibly skewed by this.

**Limitations of the production-gate emulation:**

- **Earnings blackout (DonchianEdgeFilter rule 2) is not modeled.** Production
  blocks new entries 1 calendar day before earnings using a yfinance/cache
  lookup that has no offline backtest equivalent. Skipping it means the
  simulator allows a small fraction of trades that production would block.
  The direction of the bias is the same across all three stop variants, so
  it doesn't favor one over the other in the A/B.
- **Sector momentum "warn" mode is not modeled.** In production the Donchian
  sleeve uses `sector_entry_policy="warn"`, which only logs — it doesn't
  block. So no behavioral difference.
- **SPY regime is classified on the same SPY history a backtest reviewer can
  fetch.** Per-bar regime is computed via a faithful reimplementation of
  [`RegimeDetector._classify`](../regime/detector.py) priority logic
  (BEAR > VOLATILE > TRENDING > RANGING, with the same ADX/SMA/ATR%
  thresholds). Logged regime distribution over the 2021-04 → 2024-12 SPY
  bars: TRENDING 607, RANGING 533, BEAR 274, VOLATILE 60 (out of 1474).

## 5. Results — production gates ON (canonical)

| Window | static_atr | donchian_low_trail | chandelier |
|---|---|---|---|
| 2021 melt-up (28 syms, 51 trades) | +1.9% / Shp +0.07 / MaxDD −4.1% | +2.1% / +0.10 / −3.9% | **+2.4% / +0.15 / −3.3%** |
| 2022 bear (29 syms, 7 trades) | −0.4% / −0.16 / −0.7% | −0.3% / −0.15 / −0.7% | **−0.3% / −0.14 / −0.6%** |
| 2023-24 rally (31 syms, 153 trades) | **+2.8% / +0.15 / −6.0%** | +2.8% / +0.15 / −5.9% | +1.9% / +0.12 / −5.6% |
| Combined 2021-2024 (28 syms, 207 trades) | +4.5% / +0.18 / −8.0% | **+4.6% / +0.18 / −8.1%** | +4.0% / +0.18 / −7.5% |

Exit-reason mix (combined 2021-2024):

| Variant | %Gap | %Intra | %Sig | %EOD |
|---|---:|---:|---:|---:|
| static_atr | 9.2 | 31.4 | 53.6 | 5.8 |
| donchian_low_trail | 14.9 | 51.0 | 28.8 | 5.3 |
| chandelier | 18.6 | 61.8 | 16.8 | 2.7 |

### Effect of fixing the audit findings (PR #49 R1 → R2)

For reference, the R1 numbers (before SPY regime defaults and full-history
filter mask were corrected) overstated the trade count and the chandelier
gap somewhat:

| Variant | R1 gated (broken regime defaults + sliced-window filter) | R2 gated (production-faithful) | Delta |
|---|---|---|---|
| static_atr | +5.6% / Shp +0.22 / 213 trades | +4.5% / +0.18 / 207 trades | −6 trades, returns nudged down by ungating fewer SMA200-NaN entries |
| donchian_low_trail | +5.7% / +0.22 / 214 trades | +4.6% / +0.18 / 208 trades | same direction |
| chandelier | +4.8% / +0.20 / 228 trades | +4.0% / +0.18 / 220 trades | same direction |

And for completeness, the original ungated baseline (no gates, warmup trades
counted) — kept here to show the gate's effect, not as a recommendation:

| Variant | Ungated combined | R2 gated combined | Delta |
|---|---|---|---|
| static_atr | +24.2% / Shp +0.57 / 468 trades | +4.5% / +0.18 / 207 trades | ~56% fewer trades |
| donchian_low_trail | +23.7% / +0.56 / 475 trades | +4.6% / +0.18 / 208 trades | same |
| chandelier | +19.1% / +0.53 / 512 trades | +4.0% / +0.18 / 220 trades | same |

The gates' main effect: 2022 trade count collapses from 106 → 7 because most
of 2022 was BEAR regime on SPY, which blocks all new long entries via the
regime gate. This is faithful to production behavior — and means the stop
policy on bear-regime trades is largely a non-question (very few trades
happen there at all).

## 6. What the numbers actually say (gated)

### Donchian-low trail is a wash vs static

Mean returns inside 0.1 pp across every window. Sharpe identical to two
decimals on the combined run. MaxDD inside 0.1 pp. The exit-mix shifts
dramatically — signal exits fall from 54% → 29%, intrabar stops rise from
31% → 51% — but the realized PnL doesn't move.

**Why**: the trail level (`rolling_15_low − 0.5×ATR`) sits roughly where the
strategy's own signal-exit trigger sits. The trail fires one bar *earlier*
than the signal exit would have fired the next morning. The trades are the
same, the prices are similar, the aggregate is unchanged. The trail makes
the signal exit redundant, not better.

### Chandelier shifts the strategy's character — wrong direction for this universe

Chandelier helps marginally in chop (2021: +0.5 pp, smaller MaxDD by 0.8 pp).
But it **gives back 0.9 pp in the AI rally** (+2.8% → +1.9%) and 0.5 pp on
the combined run. Trade count rises 6% (207 → 220) — more re-entries after
premature stop-outs.

**Why**: `HWM_close − 3×ATR` is a tighter trail than the strategy's own
signal exit on names that are running. Trending mega-caps pull back through
the chandelier level on normal vol expansion, get stopped out, then trigger
re-entry on the next new high. The strategy's whole reason for existing is
to ride those names — clipping them early defeats the design.

### The gap-through fear is real but small in magnitude

Static stop's `%Gap` is 7-14% across regimes. Trailing variants raise it to
12-29% because the trail level sits closer to price. So the trail does catch
more gaps closer to the recent high — but those catches don't translate into
aggregate edge because:

1. The strategy's daily-close signal exit handles most trend failures one
   bar later anyway.
2. The catastrophic single-day gap-down that motivated the PLAN entry
   (QCOM-2026-05-11-style) is a tail event, not a typical exit.
3. **The production regime gate already keeps the strategy out of the
   regimes where catastrophic gaps cluster** (2022 bear → 7 in-window trades
   total). The stop policy is moot for trades that don't exist.

## 7. Recommendation and re-open conditions

**Conditional recommendation: keep the static `entry − 2×ATR` broker stop
on the IEX-reachable window.** With production-realistic gates applied,
the variants are within noise of each other across 2021-2024. Neither
trail variant clears the bar on chop, bear, or rally regimes.

**This is not a full closure.** The 2018 Q4 vol shock and 2020 COVID
crash were the original motivation for the PLAN concern and **were not
tested** in this round. SIP makes them reachable (verified post-review,
2026-06-07); PR #50 makes SIP the backtest default. Once that lands, the
SIP re-test is the blocking step before this question can move to fully
closed.

Re-open / next-step paths:

- **SIP re-test (planned, post-PR #50)** — re-run the simulator on the
  same `ai_bigtech` universe with `feed="sip"`. **Critical window
  design**: `run_window()` passes `window.start` as `trade_start` to
  `simulate_symbol`, which blocks entries before that boundary. The
  catastrophic-gap test is specifically about positions opened during
  the prior trend and then carried into the crash, so the window's
  entry boundary must begin BEFORE the crash. Use:
    - `2018_q4_vol_shock`: **2018-07-01 → 2018-12-31** (entries from
      July; crash days Oct 3 - Dec 24). A 2018-10-01 boundary would
      have excluded every pre-crash entry — which IS the case the
      PLAN concern is about.
    - `2020_covid_crash`: **2019-09-01 → 2020-05-31** (entries from
      Sep 2019; crash days Feb 20 - Mar 23, 2020).
  Plus the existing four windows + a combined 2016-2024 run.

  **Minimum sample requirements** (mandatory before any closure
  verdict): each of the two new sub-windows must have ≥ 25 total
  trades AND ≥ 10 trades open during the named crash days. If either
  floor isn't met, document the gap and **abstain** from closure —
  "no difference observed on 5 trades" is sample noise, not evidence.

  **Closure thresholds** (apply only after minimum samples met): if
  all three variants stay within 0.3 pp mean return / 0.05 Sharpe /
  1.0 pp MaxDD on each of the two new sub-windows AND there's no
  meaningful divergence in the per-variant R-distribution on the
  crash-exposed trade subset, the closure is complete. If chandelier
  or Donchian-low trail materially outperforms static on either
  catastrophic-gap regime — particularly on the crash-exposed trade
  subset — that's the evidence the PLAN concern was asking for, and
  the change gets a 4-6 week paper A/B before live promotion.
- **Live giveback event** — if real paper or live trading produces a
  documented case where the static stop visibly surrendered material P&L
  on a gap-down through a then-vestigial level, that single case study
  can override the aggregate evidence regardless of regime testing.

## 8. Reproducing the work

```bash
# 1. Backfill / verify cache, including SPY for regime classification
venv/bin/python scripts/audit_donchian_history.py

# 2. Run the comparison with production gates ON (default)
venv/bin/python scripts/donchian_trail_compare.py

# 3. (optional) Compare against an ungated baseline to see the gates' effect
venv/bin/python scripts/donchian_trail_compare.py --no-production-gates \
    --output logs/backtests/donchian_trail_compare_ungated.md

# 4. Unit tests on the simulator
venv/bin/pytest tests/test_donchian_trail_sim.py -v
```

Output reports land under `logs/backtests/` (gitignored).
