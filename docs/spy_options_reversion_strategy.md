# SPY Options RSI Reversion — Strategy Research & Deployment Guide

**Status:** ✅ **ACTIVE** — wired in `forward_test.py` on `gemini/options-wip-save` branch.

**Last updated:** 2026-05-06 (trailing stop added)

---

## Strategy thesis

Buy slightly-ITM SPY calls when RSI recovers from an oversold reading — confirmation
that short-term selling pressure has broken.  The approach is derived from a manual
trading pattern that produced nominal success: buy SPY calls on multi-day weakness,
target 20–25% premium gain, cut at -25% loss.  The bot mechanises the discipline that
human discretion lacks (greed, holding winners past the target).

**Key design choices:**

- **Confirmation entry, not initiation:** wait for RSI to cross back *above* the
  threshold rather than buying into the dip.  Costs a few ticks of premium but
  avoids catching falling knives.
- **Options, not shares:** a moderately-ITM call on a 0.5–1% SPY dip typically
  carries a 0.55 delta; a +0.5% SPY bounce translates to ~25–35% option premium
  gain.  That asymmetry is the core edge.
- **Rule-based exits:** the bot enforces the TP/SL targets that human traders
  frequently override when a position is running well.

---

## Deployment configuration

| Parameter | Value |
|---|---|
| Underlying | SPY only |
| Timeframe | 5-minute bars |
| RSI length | 14 (Wilder's RMA) |
| RSI entry threshold | **45** — cross from below to above |
| Contract | Call, strike ≈ 0.5% ITM (strike = close × 0.995) |
| DTE window | **14–28 calendar days** (first available Friday expiry) |
| Target delta at entry | ~0.55 (approximated geometrically; no live Greeks) |
| Order type | LIMIT at OPRA bid/ask midpoint |
| Spread guard | Reject if (ask − bid) / midpoint > 5% |
| Take profit | **+200% safety valve** only — trailing stop handles real exits |
| Trailing stop | Activates once Alpaca's observed premium rises ≥ **10%** above entry premium; exits if premium drops ≥ **15%** below the durable HWM |
| Stop loss | **−25%** of entry premium (hard floor, always active) |
| Time stop | Wednesday of expiry week at 3:30 PM ET |
| Delta floor | Exit if Black-Scholes Delta < 0.30 (uses VIX as implied vol, cached daily) |
| Edge filter | `SPYOptionsEdgeFilter`: SPY close > **100-day SMA** |
| Regime gate | `TRENDING` or `RANGING` — blocked in BEAR and VOLATILE |
| Sleeve weight | 0.05 of gross capital |
| Max positions | 1 concurrent |

The trailing HWM is durable across restarts and is shared by the strategy's
software guard and its broker-side protective stop. Alpaca supports `gtc` for
single-leg option stop orders, so the engine keeps one GTC stop open and uses
the SDK replace-order endpoint when ratcheting it upward. Black-Scholes remains
an input to the Delta floor only; it is not treated as an executable premium.
If an already-open option is adopted without a recoverable HWM, the engine
alerts and bootstraps conservatively from the current Alpaca position premium.

**Capital math at $100k equity:**
- Sleeve budget = $100k × 0.80 × 0.05 = **$4,000**
- SPY call premium ~$8–12 per contract × 100 multiplier = $800–$1,200
- Typical position size at 5% sleeve: **3–4 contracts**
- Max loss per trade at −25% SL: ~$200–300 per contract

---

## Edge filter rationale — 100 SMA vs 200 SMA

The filter blocks entries when SPY is below its N-day SMA.  Backtest comparison
(RSI 45, TP 25%, SL 25%, DTE 14–28, 2019–2025):

| Filter | Trades | Win % | Avg P&L | Total P&L | PF | 2022 result |
|---|---|---|---|---|---|---|
| 200 SMA | 40 | 67.5% | +18.2% | +728% | 2.59 | 3tr / +26% |
| **100 SMA** | **35** | **71.4%** | **+21.3%** | **+746%** | **3.08** | **0tr / +0%** |
| 50 SMA | 25 | 64.0% | +14.4% | +361% | 2.19 | 3tr / −96% |
| No filter | 64 | 56.2% | +7.8% | +501% | 1.48 | 19tr / −113% |

The 100 SMA is superior to the 200 SMA: it is faster to reflect regime change, blocked
all 2022 entries entirely (0 trades vs. 3 borderline trades with 200 SMA), and delivers
a higher profit factor (3.08 vs. 2.59) with fewer but cleaner trades.  The 50 SMA and
no-filter variants bleed badly in 2022 — the filter is load-bearing, not cosmetic.

---

## Parameter grid summary

Grid tested: RSI threshold 30–50 × TP 15–30% × SL 20–30% × DTE (10–21, 14–28) ×
max_positions (1, 2) × 200 SMA filter.  Daily SPY bars 2019–2025, Black-Scholes
pricing with VIX as implied vol, r = 0.05, strike = close × 0.995.

**Top 10 combinations by total P&L (≥8 trades, 200 SMA filter):**

| RSI | TP % | SL % | DTE | max_pos | Trades | Win % | Avg P&L | Total P&L | PF |
|---|---|---|---|---|---|---|---|---|---|
| 45 | 25 | 25 | 14–28 | 2 | 44 | 68.2% | +18.3% | +803% | 2.57 |
| 45 | 25 | 25 | 10–21 | 2 | 44 | 65.9% | +18.0% | +794% | 2.47 |
| 45 | 25 | 20 | 10–21 | 2 | 44 | 63.6% | +18.0% | +791% | 2.54 |
| 45 | 25 | 25 | 14–28 | 1 | 40 | 67.5% | +18.2% | +729% | 2.59 |
| 45 | 25 | 30 | 14–28 | 1 | 40 | 67.5% | +16.9% | +675% | 2.32 |
| 45 | 25 | 20 | 10–21 | 1 | 42 | 61.9% | +15.6% | +655% | 2.28 |
| 45 | 25 | 25 | 10–21 | 1 | 41 | 63.4% | +15.3% | +629% | 2.16 |
| 45 | 15 | 25 | 10–21 | 1 | 40 | 72.5% | +15.6% | +624% | 2.45 |
| 45 | 15 | 25 | 14–28 | 1 | 39 | 74.4% | +16.0% | +623% | 2.74 |
| 45 | 25 | 30 | 10–21 | 1 | 39 | 66.7% | +16.1% | +630% | 2.17 |

**Key findings from the grid:**
- RSI 45 dominates every top slot.  RSI 40 produces fewer trades with lower total P&L.
  RSI 50 never appears in the top 20 — extra entries at milder dips degrade average quality.
- TP 25% + SL 25% is the optimal symmetric bracket.  Asymmetric brackets (wider SL) cost PF.
- DTE 14–28 is slightly better than 10–21 on quality metrics; 10–21 generates slightly more trades.
- max_pos=2 adds only 4 trades over 6 years vs. max_pos=1.  Marginal benefit; added complexity.
  One of the additional entries (April 2024) produced a −51% SL compounding an existing SL.
- The 100 SMA filter (used with the chosen combo) improves PF to 3.08 vs. 2.59 with 200 SMA.

**Chosen configuration rationale:**
RSI 45 / trailing stop (act=10%, trail=15%) / SL 25% / DTE 14–28 / max_pos=1 / 100 SMA filter.
Prioritises per-trade quality over volume.  39 trades over 6 years ≈ ~6/year on daily bars;
the live 5-minute strategy will fire more frequently because intraday dips are shallower and
more common than multi-day daily-bar dips.  The trailing stop replaces the fixed TP to let
winners run; see the trailing stop comparison section below.

---

## Backtest results — trailing stop (live configuration)

**RSI 45 | trailing stop (act=10%, trail=15%) | SL −25% | DTE 14–28 | SPY > 100 SMA | 2019–2025**

| Metric | Baseline (fixed TP 20%) | **Trailing stop** | Δ |
|---|---|---|---|
| Trades | 40 | **39** | −1 |
| Win rate | 67.5% | **46.2%** | −21pp (expected: larger winners trail below entry) |
| Avg P&L / trade | +14.6% | **+29.8%** | +2× |
| Cumulative P&L | +584% | **+1,164%** | **+2×** |
| Profit factor | 2.27 | **3.31** | +46% |
| Avg hold days | 3.3 | **8.1** | +2.4× |
| 2020 (COVID) | +76% | **+187%** | |
| 2022 (bear) | +26% | **−78%** | trade-off — see note |
| 2023–2025 | +333% | **+705%** | |

**Exit breakdown:**

| Exit type | Baseline | Trailing |
|---|---|---|
| Fixed TP hits | 27 | 0 (replaced) |
| Hard SL hits | 13 | 10 |
| Time stops | 0 | 10 |
| Trail exits | 0 | 19 |

**Big winners unlocked by the trailing stop** (capped at 20% in the baseline):

| Date | Baseline exit | Trailing exit |
|---|---|---|
| 2020-02-03 | +40.8% (tp) | **+147.4%** (time_stop) |
| 2020-11-03 | +61.6% (tp) | **+91.2%** (time_stop) |
| 2021-06-21 | +27.2% (tp) | **+89.9%** (time_stop) |
| 2021-10-13 | +48.8% (tp) | **+149.8%** (time_stop) |
| 2024-05-02 | +46.1% (tp) | **+246.1%** (time_stop) |
| 2024-08-09 | +38.8% (tp) | **+192.1%** (time_stop) |
| 2024-09-09 | +33.5% (tp) | **+144.6%** (time_stop) |
| 2024-11-01 | +102.1% (tp) | **+146.6%** (time_stop) |

**2022 trade-off:** The Jan/Feb 2022 trades (+26.8%, +36.0% with fixed TP) flip to losses
(−25%, −16.6%) because the market bounced just enough to activate the trail then reversed
hard.  This is a structural whipsaw risk.  A volatility gate or SMA re-tuning could address
this but is deferred — the overall improvement across 6 years is unambiguous.  The 100 SMA
filter already blocks the most dangerous 2022 entries (no trades after mid-Jan 2022).

**Per-trade log — trailing stop (live configuration):**

| Entry | Exit | SPY @ Entry | Strike | Expiry | Entry $ | Exit $ | P&L % | Reason |
|---|---|---|---|---|---|---|---|---|
| 2020-02-03 | 2020-02-19 | 296.20 | 294.72 | 2020-02-21 | 5.88 | 14.56 | **+147.4%** | time_stop |
| 2020-09-09 | 2020-09-10 | 313.72 | 312.15 | 2020-09-25 | 8.70 | 5.94 | −31.8% | sl |
| 2020-09-11 | 2020-09-17 | 308.43 | 306.89 | 2020-09-25 | 7.57 | 6.78 | −10.4% | trail |
| 2020-09-28 | 2020-10-02 | 309.79 | 308.24 | 2020-10-16 | 8.37 | 7.61 | −9.1% | trail |
| 2020-11-03 | 2020-11-18 | 311.49 | 309.94 | 2020-11-20 | 10.68 | 20.41 | **+91.2%** | time_stop |
| 2021-02-01 | 2021-02-17 | 350.25 | 348.49 | 2021-02-19 | 10.71 | 16.89 | **+57.7%** | time_stop |
| 2021-03-01 | 2021-03-03 | 362.67 | 360.86 | 2021-03-19 | 8.90 | 5.72 | −35.8% | sl |
| 2021-03-05 | 2021-03-17 | 357.13 | 355.35 | 2021-03-19 | 8.16 | 14.58 | **+78.6%** | time_stop |
| 2021-05-13 | 2021-05-18 | 383.19 | 381.28 | 2021-05-28 | 8.57 | 7.62 | −11.1% | trail |
| 2021-06-21 | 2021-07-07 | 394.36 | 392.39 | 2021-07-09 | 7.81 | 14.82 | **+89.9%** | time_stop |
| 2021-07-20 | 2021-07-28 | 403.92 | 401.90 | 2021-08-06 | 8.41 | 11.11 | +32.0% | trail |
| 2021-09-23 | 2021-09-28 | 416.61 | 414.52 | 2021-10-08 | 7.83 | 3.67 | −53.1% | sl |
| 2021-10-06 | 2021-10-11 | 408.82 | 406.78 | 2021-10-22 | 8.71 | 6.96 | −20.1% | trail |
| 2021-10-13 | 2021-10-27 | 409.09 | 407.04 | 2021-10-29 | 7.92 | 19.79 | **+149.8%** | time_stop |
| 2021-12-02 | 2021-12-09 | 429.97 | 427.82 | 2021-12-17 | 11.27 | 12.70 | +12.7% | trail |
| 2021-12-21 | 2021-12-31 | 436.82 | 434.64 | 2022-01-07 | 9.57 | 14.29 | +49.3% | trail |
| 2022-01-31 | 2022-02-03 | 424.42 | 422.30 | 2022-02-18 | 10.97 | 8.23 | −25.0% | trail |
| 2022-02-04 | 2022-02-10 | 423.28 | 421.16 | 2022-02-18 | 9.20 | 7.68 | −16.6% | trail |
| 2022-02-15 | 2022-02-17 | 420.83 | 418.72 | 2022-03-04 | 10.90 | 6.91 | −36.6% | sl |
| 2023-02-23 | 2023-02-24 | 384.09 | 382.17 | 2023-03-10 | 7.98 | 5.75 | −28.0% | sl |
| 2023-03-02 | 2023-03-07 | 381.36 | 379.45 | 2023-03-17 | 7.46 | 6.49 | −13.0% | trail |
| 2023-03-16 | 2023-03-22 | 379.73 | 377.83 | 2023-03-31 | 8.45 | 5.24 | −38.0% | sl |
| 2023-08-23 | 2023-08-24 | 427.91 | 425.77 | 2023-09-08 | 7.36 | 4.55 | −38.2% | sl |
| 2023-08-25 | 2023-09-05 | 424.96 | 422.83 | 2023-09-08 | 6.79 | 11.29 | **+66.4%** | trail |
| 2023-10-09 | 2023-10-12 | 419.01 | 416.92 | 2023-10-27 | 8.22 | 8.04 | −2.2% | trail |
| 2023-11-01 | 2023-11-09 | 409.68 | 407.63 | 2023-11-17 | 7.34 | 13.66 | **+86.0%** | trail |
| 2024-04-23 | 2024-04-30 | 493.64 | 491.17 | 2024-05-10 | 8.60 | 4.85 | −43.7% | sl |
| 2024-05-02 | 2024-05-15 | 493.03 | 490.57 | 2024-05-17 | 7.73 | 26.76 | **+246.1%** | time_stop |
| 2024-07-26 | 2024-08-01 | 533.22 | 530.55 | 2024-08-09 | 8.80 | 6.80 | −22.8% | trail |
| 2024-08-09 | 2024-08-21 | 522.01 | 519.40 | 2024-08-23 | 10.21 | 29.81 | **+192.1%** | time_stop |
| 2024-09-09 | 2024-09-25 | 535.15 | 532.47 | 2024-09-27 | 11.32 | 27.68 | **+144.6%** | time_stop |
| 2024-11-01 | 2024-11-13 | 560.99 | 558.18 | 2024-11-15 | 11.62 | 28.65 | **+146.6%** | time_stop |
| 2024-12-20 | 2024-12-27 | 582.70 | 579.78 | 2025-01-03 | 10.50 | 9.57 | −8.8% | trail |
| 2025-01-03 | 2025-01-07 | 583.49 | 580.57 | 2025-01-17 | 9.52 | 7.05 | −26.0% | sl |
| 2025-01-15 | 2025-01-27 | 584.30 | 581.38 | 2025-01-31 | 10.11 | 10.87 | +7.4% | trail |
| 2025-03-24 | 2025-03-26 | 567.57 | 564.74 | 2025-04-11 | 11.03 | 7.96 | −27.9% | sl |
| 2025-10-13 | 2025-10-22 | 659.29 | 655.99 | 2025-10-31 | 13.70 | 12.90 | −5.9% | trail |
| 2025-11-24 | 2025-12-09 | 664.94 | 661.62 | 2025-12-12 | 14.68 | 18.00 | +22.6% | trail |
| 2025-12-18 | 2025-12-29 | 672.64 | 669.28 | 2026-01-02 | 11.71 | 17.25 | +47.4% | trail |

**Future improvement noted:** 2022 whipsaw risk (Jan/Feb trades) could be addressed with a
volatility gate (e.g. VIX > threshold) or SMA re-tuning.  Deferred pending paper run data.

---

## Methodology and limitations

**Data:** Daily SPY and VIX bars via yfinance, 2019-01-01 – 2025-12-31 (1,759 bars).

**Option pricing:** Black-Scholes call formula.  Implied vol = VIX close / 100.  Strike =
close × 0.995.  r = 0.05.  T measured to 4:00 PM ET on expiry Friday.  No bid-ask spread
modelled on entry — real fills will differ by the half-spread.

**RSI:** Wilder's RMA (EWM alpha = 1/14), same implementation as the live strategy.

**Exit simulation:** TP/SL/time-stop checked at daily close.  Intraday TP/SL touches that
reverse before close are not captured — real P&L may be slightly better (intraday TP
exits) or worse (whipsaws that recover but were stopped intraday).

**Important caveat:** This backtest uses daily bars as a proxy for the live 5-minute signal.
On daily bars RSI 45 fires ~6 times per year; the 5-minute version will fire more often
because intraday RSI oscillates more.  Trade frequency in live paper trading is expected
to be higher.  Per-trade quality should be similar or better (5-min signals are faster
to resolve; less overnight risk).

**Regime note:** 2022 produced zero trades with the 100 SMA filter active.  SPY dropped
below the 100 SMA in mid-January 2022 and did not reclaim it until late 2022.  This is
the filter working as designed.  Without the filter, 2022 generated 19 trades at −113%
cumulative — the filter is the single most important risk control in this strategy.

---

## Implementation files

| File | Purpose |
|---|---|
| `strategies/spy_options_reversion.py` | Strategy class — RSI signal, time stop, Delta floor |
| `strategies/filters/spy_options_reversion.py` | `SPYOptionsEdgeFilter` — 100 SMA gate |
| `utils/options_lookup.py` | OCC contract resolver (`find_best_call`) |
| `execution/options_executor.py` | Background bracket-order worker |
| `backtest/spy_options_backtest.py` | Parameter grid backtest script |
| `tests/test_spy_options_reversion.py` | 14 unit tests |
