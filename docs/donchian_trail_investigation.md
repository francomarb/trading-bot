# Donchian trailing broker stop — investigation (2026-06-06)

> **Status:** Investigated and closed. Listed under `PLAN.md` → *Deferred Or Parked Ideas*.
> **TL;DR:** On the ai_bigtech universe across 2021-04-01 → 2024-12-31, replacing
> the static `entry − 2×ATR` broker stop with either a 15-day-low trail or a
> chandelier `HWM − 3×ATR` trail does **not** improve risk-adjusted returns.
> The static stop's known weakness (gap-down past the rising 15-day-low) is
> real but rare, and the strategy's own daily-close signal exit already
> handles most trend failures one bar later.

---

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
windows, ATR length 14, 5 bps slippage per fill, 2% risk per trade.

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

Coverage: 17 unit tests in
[tests/test_donchian_trail_sim.py](../tests/test_donchian_trail_sim.py) verify
each policy's ratchet behavior, all four fill paths (gap, intrabar, signal,
EOD), no-look-ahead invariants, and identical-sizing assertion.

## 4. Universe and coverage caveat

Audit script: [scripts/audit_donchian_history.py](../scripts/audit_donchian_history.py).

**Hard constraint**: the Alpaca IEX paper feed serves data back to ~2021-01-04
only. Pre-2021 windows (2018 vol shock, 2020 COVID crash) are not reachable
without a SIP paid subscription, so the PLAN's original ask of "2018 / 2020 /
2021 / 2022 / 2023-24" was reduced to the reachable subset.

Symbol participation within the reachable range:

| Window | Symbols traded / 32 | Notes |
|---|---:|---|
| 2021 melt-up (Q2-Q4) | 28 | Window starts 2021-04-01 because nothing in the universe has bars before 2021-01-04; 50-trading-day minimum warmup needed for Donchian 30 + ATR 14. ARM, CEG, IREN, partial RGTI excluded. |
| 2022 bear | 29 | IREN (Nov 2021), CEG (Feb 2022 spinoff), ARM (Sep 2023 IPO) excluded |
| 2023 rally | 31 | ARM only excluded |
| 2024 rally | 32 | All names trade |
| Combined 2021-04 → 2024-12 | 28 | Limited by 2021 entrants |

The de-SPAC names (QBTS, RGTI, IONQ) have pre-merger SPAC bars in the feed.
Those bars participate as ordinary OHLC; we did not strip them. The aggregate
results are not visibly skewed by this.

## 5. Results

| Window | static_atr | donchian_low_trail | chandelier |
|---|---|---|---|
| 2021 melt-up (28 syms, 114 trades) | +4.3% / Shp +0.43 / MaxDD -6.5% | +3.9% / +0.40 / -6.4% | **+5.1% / +0.54 / -5.5%** |
| 2022 bear (29 syms, 106 trades) | -3.8% / -0.56 / -7.5% | -3.7% / -0.54 / -7.4% | **-3.2% / -0.48 / -7.0%** |
| 2023-24 rally (31 syms, 294 trades) | **+32.7% / +0.89 / -9.3%** | +32.4% / +0.88 / -9.3% | +19.6% / +0.78 / -8.9% |
| Combined 2021-2024 (28 syms, 468 trades) | **+24.2% / +0.57 / -13.0%** | +23.7% / +0.56 / -12.8% | +19.1% / +0.53 / -12.3% |

Exit-reason mix (combined 2021-2024):

| Variant | %Gap (stop fills at open after gap-through) | %Intra (intrabar stop) | %Sig (15-day-low signal exit) | %EOD |
|---|---:|---:|---:|---:|
| static_atr | 7.3 | 26.5 | 62.8 | 3.4 |
| donchian_low_trail | 13.1 | 50.3 | 33.5 | 3.2 |
| chandelier | 17.2 | 58.0 | 22.9 | 2.0 |

## 6. What the numbers actually say

### Donchian-low trail is a wash vs static

Mean returns inside 0.5 pp across every window. Sharpe inside 0.03. MaxDD
identical to one decimal. The exit-mix shifts dramatically — signal exits
fall from 63% → 34%, intrabar stops rise from 26% → 50% — but the realized
PnL doesn't move.

**Why**: the trail level (`rolling_15_low − 0.5×ATR`) sits roughly where the
strategy's own signal-exit trigger sits. The trail fires one bar *earlier*
than the signal exit would have fired the next morning. The trades are the
same, the prices are similar, the aggregate is unchanged. The trail makes
the signal exit redundant, not better.

### Chandelier shifts the strategy's character — wrong direction for this universe

Chandelier helps in chop (2021 melt-up: +0.8 pp, smaller MaxDD) and bear
(2022: +0.6 pp). But it **gives back 13 pp in the AI rally** (+32.7% →
+19.6%) and 5 pp on the combined run. Trade count rises 9% (468 → 512) —
more re-entries after premature stop-outs.

**Why**: `HWM_close − 3×ATR` is a tighter trail than the strategy's own
signal exit on names that are running. Trending mega-caps pull back through
the chandelier level on normal vol expansion, get stopped out, then trigger
re-entry on the next new high. The strategy's whole reason for existing is
to ride those names — clipping them early defeats the design.

### The gap-through fear is real but small in magnitude

Static stop's `%Gap` is 7-14% across regimes. Trailing variants raise it to
12-22% because the trail level sits closer to price. So the trail does catch
more gaps closer to the recent high — but those catches don't translate into
aggregate edge because:

1. The strategy's daily-close signal exit handles most trend failures one
   bar later anyway.
2. The catastrophic single-day gap-down that motivated the PLAN entry
   (QCOM-2026-05-11-style) is a tail event, not a typical exit.

## 7. Recommendation and re-open conditions

**Recommendation: keep the static `entry − 2×ATR` broker stop.** The PLAN
worry was legitimate in kind but small in magnitude across 28-31 ai_bigtech
names over 3.75 years. Neither trail variant clears the bar.

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
# 1. Backfill / verify cache and per-symbol coverage
venv/bin/python scripts/audit_donchian_history.py

# 2. Run the comparison
venv/bin/python scripts/donchian_trail_compare.py

# 3. Unit tests on the simulator
venv/bin/pytest tests/test_donchian_trail_sim.py -v
```

Output reports land under `logs/backtests/` (gitignored).
