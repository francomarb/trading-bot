# Donchian trailing broker stop — investigation (2026-06-06 / updated 2026-06-07 R2)

> **Status:** Investigated and closed. Listed under `PLAN.md` → *Deferred Or Parked Ideas*.
> **TL;DR:** On the ai_bigtech universe with **production-realistic entry
> gating** (SPY TRENDING regime + DonchianEdgeFilter) over 2021-04-01 →
> 2024-12-31, replacing the static `entry − 2×ATR` broker stop with either a
> 15-day-low trail or a chandelier `HWM − 3×ATR` trail does **not** improve
> risk-adjusted returns. Differences vs static are within noise. The static
> stop's known weakness (gap-down past the rising 15-day-low) is real but
> rare, and the strategy's own daily-close signal exit already handles most
> trend failures one bar later.

## Revision history

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

**Hard constraint**: the Alpaca IEX paper feed serves data back to ~2021-01-04
only. Pre-2021 windows (2018 vol shock, 2020 COVID crash) are not reachable
without a SIP paid subscription, so the PLAN's original ask of "2018 / 2020 /
2021 / 2022 / 2023-24" was reduced to the reachable subset.

Symbol participation within the reachable range:

| Window | Symbols traded / 32 | Notes |
|---|---:|---|
| 2021 melt-up (Q2-Q4) | 28 | Window starts 2021-04-01 because the IEX feed begins 2021-01-04 and we need 50 trading days of warmup. IREN, CEG, ARM, RGTI excluded (later listings). |
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

**Recommendation: keep the static `entry − 2×ATR` broker stop.** With
production-realistic gates applied, the variants are within noise of each
other on the reachable 3.75 years. Neither trail variant clears the bar.

Re-open only with one of:

- **SIP-feed pre-2021 evidence** — re-run the simulator across 2018 vol
  shock and 2020 COVID crash regimes (script is ready; only the data is
  missing). Those are the regimes most likely to produce the catastrophic
  gap-through the static stop is bad at.
- **Live giveback event** — if real paper or live trading produces a
  documented case where the static stop visibly surrendered material P&L on
  a gap-down through a then-vestigial level, that single case study can
  override the aggregate evidence.

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
