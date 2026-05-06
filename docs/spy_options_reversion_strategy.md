# SPY Options RSI Reversion — Strategy Research & Deployment Guide

**Status:** ✅ **ACTIVE** — wired in `forward_test.py` on `gemini/options-wip-save` branch.

**Last updated:** 2026-05-06

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
| Take profit | **+25%** of entry premium |
| Stop loss | **−25%** of entry premium |
| Time stop | Wednesday of expiry week at 3:30 PM ET |
| Delta floor | Exit if Black-Scholes Delta < 0.30 (uses VIX as implied vol, cached daily) |
| Edge filter | `SPYOptionsEdgeFilter`: SPY close > **100-day SMA** |
| Regime gate | `TRENDING` or `RANGING` — blocked in BEAR and VOLATILE |
| Sleeve weight | 0.05 of gross capital |
| Max positions | 1 concurrent |

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
RSI 45 / TP 25% / SL 25% / DTE 14–28 / max_pos=1 / 100 SMA filter.
Prioritises per-trade quality over volume.  35 trades over 6 years ≈ ~6/year on daily bars;
the live 5-minute strategy will fire more frequently because intraday dips are shallower and
more common than multi-day daily-bar dips.

---

## Backtest results — chosen configuration

**RSI 45 | TP +25% | SL −25% | DTE 14–28 | SPY > 100 SMA | 2019–2025**

| Metric | Value |
|---|---|
| Total trades | 35 |
| Win rate | 71.4% |
| Avg P&L per trade | +21.3% |
| Cumulative P&L (sum of trade %) | +746% |
| Profit factor | 3.08 |
| Avg hold days | ~3.5 |
| 2020 (COVID) | 5 trades / 4 wins / **+145%** |
| 2021 | 11 trades / 8 wins / **+180%** |
| 2022 (bear market) | **0 trades** — 100 SMA filter blocked all entries |
| 2023 | 6 trades / 3 wins / +71% |
| 2024 | 8 trades / 6 wins / **+216%** |
| 2025 | 5 trades / 4 wins / +134% |

**Per-trade log (Black-Scholes modelled, daily close prices):**

| Entry | Exit | SPY @ Entry | Strike | Expiry | Entry $ | Exit $ | P&L % | Reason |
|---|---|---|---|---|---|---|---|---|
| 2020-02-03 | 2020-02-04 | 296.20 | 294.72 | 2020-02-21 | 5.88 | 8.28 | +40.8% | tp |
| 2020-09-09 | 2020-09-10 | 313.72 | 312.15 | 2020-09-25 | 8.70 | 5.94 | −31.8% | sl |
| 2020-09-11 | 2020-09-15 | 308.43 | 306.89 | 2020-09-25 | 7.57 | 9.90 | +30.7% | tp |
| 2020-09-28 | 2020-10-08 | 309.79 | 308.24 | 2020-10-16 | 8.37 | 12.05 | +43.9% | tp |
| 2020-11-03 | 2020-11-05 | 311.49 | 309.94 | 2020-11-20 | 10.68 | 17.25 | +61.6% | tp |
| 2021-02-01 | 2021-02-04 | 350.25 | 348.49 | 2021-02-19 | 10.71 | 13.78 | +28.7% | tp |
| 2021-03-01 | 2021-03-03 | 362.67 | 360.86 | 2021-03-19 | 8.90 | 5.72 | −35.8% | sl |
| 2021-03-05 | 2021-03-11 | 357.13 | 355.35 | 2021-03-19 | 8.16 | 12.42 | +52.1% | tp |
| 2021-05-13 | 2021-05-14 | 383.19 | 381.28 | 2021-05-28 | 8.57 | 10.91 | +27.3% | tp |
| 2021-06-21 | 2021-06-25 | 394.36 | 392.39 | 2021-07-09 | 7.81 | 9.93 | +27.2% | tp |
| 2021-07-20 | 2021-07-23 | 403.92 | 401.90 | 2021-08-06 | 8.41 | 12.73 | +51.2% | tp |
| 2021-09-23 | 2021-09-28 | 416.61 | 414.52 | 2021-10-08 | 7.83 | 3.67 | −53.1% | sl |
| 2021-10-06 | 2021-10-12 | 408.82 | 406.78 | 2021-10-22 | 8.71 | 6.06 | −30.4% | sl |
| 2021-10-13 | 2021-10-14 | 409.09 | 407.04 | 2021-10-29 | 7.92 | 11.79 | +48.8% | tp |
| 2021-12-02 | 2021-12-07 | 429.97 | 427.82 | 2021-12-17 | 11.27 | 14.77 | +31.0% | tp |
| 2021-12-21 | 2021-12-23 | 436.82 | 434.64 | 2022-01-07 | 9.57 | 12.72 | +32.9% | tp |
| 2023-01-06 | 2023-01-11 | 372.03 | 370.17 | 2023-01-20 | 7.49 | 11.04 | +47.4% | tp |
| 2023-02-23 | 2023-02-24 | 384.09 | 382.17 | 2023-03-10 | 7.98 | 5.75 | −28.0% | sl |
| 2023-03-02 | 2023-03-03 | 381.36 | 379.45 | 2023-03-17 | 7.46 | 10.97 | +47.2% | tp |
| 2023-03-16 | 2023-03-22 | 379.73 | 377.83 | 2023-03-31 | 8.45 | 5.24 | −38.0% | sl |
| 2023-08-23 | 2023-08-24 | 427.91 | 425.77 | 2023-09-08 | 7.36 | 4.55 | −38.2% | sl |
| 2023-08-25 | 2023-08-29 | 424.96 | 422.83 | 2023-09-08 | 6.79 | 12.24 | +80.3% | tp |
| 2024-04-23 | 2024-04-30 | 493.64 | 491.17 | 2024-05-10 | 8.60 | 4.85 | −43.7% | sl |
| 2024-05-02 | 2024-05-03 | 493.03 | 490.57 | 2024-05-17 | 7.73 | 11.29 | +46.1% | tp |
| 2024-07-26 | 2024-07-31 | 533.22 | 530.55 | 2024-08-09 | 8.80 | 11.53 | +31.0% | tp |
| 2024-07-31 | 2024-08-01 | 539.46 | 536.76 | 2024-08-16 | 9.44 | 6.25 | −33.8% | sl |
| 2024-08-09 | 2024-08-13 | 522.01 | 519.40 | 2024-08-23 | 10.21 | 14.16 | +38.8% | tp |
| 2024-09-09 | 2024-09-11 | 535.15 | 532.47 | 2024-09-27 | 11.32 | 15.11 | +33.5% | tp |
| 2024-11-01 | 2024-11-06 | 560.99 | 558.18 | 2024-11-15 | 11.62 | 23.48 | +102.1% | tp |
| 2024-12-20 | 2024-12-24 | 582.70 | 579.78 | 2025-01-03 | 10.50 | 14.88 | +41.8% | tp |
| 2025-01-03 | 2025-01-07 | 583.49 | 580.57 | 2025-01-17 | 9.52 | 7.05 | −26.0% | sl |
| 2025-01-15 | 2025-01-21 | 584.30 | 581.38 | 2025-01-31 | 10.11 | 15.19 | +50.2% | tp |
| 2025-10-13 | 2025-10-24 | 659.29 | 655.99 | 2025-10-31 | 13.70 | 18.90 | +38.0% | tp |
| 2025-11-24 | 2025-11-26 | 664.94 | 661.62 | 2025-12-12 | 14.68 | 19.39 | +32.1% | tp |
| 2025-12-18 | 2025-12-22 | 672.64 | 669.28 | 2026-01-02 | 11.71 | 16.38 | +40.0% | tp |

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
