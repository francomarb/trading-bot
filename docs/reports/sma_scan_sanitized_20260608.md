# SMA Watchlist Scan - 2026-06-08T01:37:11+00:00

- Rule version: `sma_watchlist_v2`
- Alpaca feed: `sip`
- Data window: 2025-04-14 to 2026-06-08
- Data end timestamp: 2026-06-08T00:34:53+00:00
- Tradable assets considered: 5712
- Assets with bars: 5710
- Fundamentals enforced: True
- Candidates selected: 31

## Rule Rationale

- Liquidity filters reduce slippage and avoid thin, noisy names.
- Price above SMA50/SMA150/SMA200 confirms the stock is already in an uptrend.
- SMA50 > SMA150 > SMA200 requires trend alignment across short, medium, and long horizons.
- The long-term direction filter is owned by the BEAR regime gate (SPY < 200 SMA); a per-symbol "SMA200 rising 20 days" rule is redundant on top of the alignment + close-above-SMA200 stack and was retired in v2.
- 52-week strength keeps the watchlist near leadership instead of damaged recovery names.
- Relative strength requires the stock to be a market leader before SMA is allowed to watch it.
- Consolidation scoring penalizes parabolic exhaustion by checking price vs the 50-day moving average.
- Freshness scoring rewards stocks whose 20-day and 50-day moving averages are tightly coiled.
- ADX and +DI/-DI reduce sideways whipsaw risk and require bullish directional pressure.
- ATR sanity rejects names that are too quiet to move or too chaotic for stable trend following.
- Fundamental sanity keeps SMA out of deteriorating or solvency-stressed companies.
- Market-cap minimum keeps SMA away from very small companies whose trends can be fragile.

## Top Candidates

| Rank | Symbol | Score | Close | RS % | ADX | +DI/-DI | ATR % | $Vol50 | Mom 12-1 | Crosses |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | GSAT | 88.2 | 81.49 | 94.3 | 39.5 | 20.1/18.4 | 1.7% | $133M | 317.8% | 2 |
| 2 | WDC | 85.4 | 511.72 | 99.4 | 35.9 | 29.9/22.1 | 6.3% | $3.2B | 789.9% | 0 |
| 3 | AA | 82.9 | 71.89 | 79.1 | 25.3 | 35.1/28.3 | 5.1% | $331M | 132.9% | 2 |
| 4 | DOCN | 81.6 | 169.87 | 96.8 | 38.8 | 23.4/11.6 | 7.4% | $516M | 439.0% | 1 |
| 5 | MU | 79.7 | 864.01 | 98.7 | 40.3 | 35.1/26.6 | 7.6% | $31.8B | 547.2% | 4 |
| 6 | STX | 79.1 | 847.47 | 98.1 | 45.9 | 30.6/19.0 | 5.7% | $2.7B | 524.0% | 2 |
| 7 | STRL | 78.3 | 882.43 | 96.2 | 35.0 | 36.9/17.3 | 6.8% | $423M | 357.3% | 2 |
| 8 | TSM | 76.1 | 415.17 | 73.4 | 27.5 | 23.9/17.3 | 4.0% | $5.3B | 109.3% | 4 |
| 9 | POWL | 76.0 | 284.87 | 97.5 | 27.4 | 20.3/14.5 | 6.4% | $170M | 442.9% | 2 |
| 10 | CAT | 75.5 | 904.28 | 84.2 | 25.9 | 29.9/14.9 | 3.3% | $2.1B | 168.2% | 2 |
| 11 | DELL | 75.2 | 394.39 | 76.6 | 56.7 | 46.3/15.0 | 6.7% | $2.5B | 115.1% | 4 |
| 12 | LSCC | 75.2 | 135.57 | 83.5 | 25.8 | 25.9/25.1 | 5.8% | $264M | 162.8% | 2 |
| 13 | ADEA | 75.0 | 28.98 | 77.2 | 23.9 | 30.8/17.4 | 7.5% | $56M | 124.8% | 4 |
| 14 | LRCX | 73.2 | 303.28 | 91.8 | 22.9 | 31.4/27.2 | 5.3% | $2.5B | 253.1% | 2 |
| 15 | NOK | 71.9 | 14.38 | 82.3 | 43.3 | 32.4/26.1 | 7.4% | $1.4B | 147.6% | 4 |
| 16 | FLEX | 71.6 | 151.92 | 89.9 | 45.9 | 38.6/18.5 | 5.3% | $619M | 215.0% | 8 |
| 17 | VSXY | 71.6 | 74.60 | 82.9 | 28.9 | 44.0/14.6 | 6.9% | $136M | 151.7% | 4 |
| 18 | GLW | 70.8 | 177.58 | 93.0 | 20.4 | 27.9/26.1 | 7.7% | $2.3B | 261.1% | 2 |
| 19 | AMD | 70.6 | 466.38 | 92.4 | 43.3 | 29.9/24.5 | 6.3% | $14.0B | 255.4% | 6 |
| 20 | SANM | 70.2 | 252.08 | 86.1 | 43.9 | 28.4/18.8 | 5.9% | $181M | 171.2% | 4 |
| 21 | CGNX | 69.7 | 60.82 | 75.3 | 32.6 | 28.0/24.6 | 4.5% | $131M | 109.9% | 4 |
| 22 | ATI | 69.3 | 177.47 | 70.3 | 20.9 | 28.2/14.7 | 3.9% | $285M | 98.9% | 2 |
| 23 | COHU | 68.7 | 49.81 | 85.4 | 30.4 | 28.3/25.6 | 6.5% | $64M | 170.1% | 4 |
| 24 | GTX | 68.6 | 31.96 | 84.8 | 46.4 | 35.0/15.8 | 4.0% | $72M | 169.6% | 5 |
| 25 | TXG | 68.1 | 31.04 | 81.6 | 32.4 | 40.7/13.7 | 6.5% | $66M | 143.5% | 6 |
| 26 | DIOD | 67.4 | 101.06 | 80.4 | 28.2 | 31.0/24.2 | 7.0% | $67M | 142.2% | 6 |
| 27 | MKSI | 65.5 | 301.65 | 91.1 | 21.0 | 26.8/25.4 | 5.4% | $329M | 235.8% | 4 |
| 28 | UMC | 65.3 | 19.70 | 74.1 | 56.7 | 36.0/17.5 | 5.7% | $222M | 109.5% | 6 |
| 29 | MTSI | 65.1 | 345.40 | 81.0 | 29.6 | 29.5/28.1 | 6.6% | $440M | 143.2% | 6 |
| 30 | VISN | 62.9 | 11.75 | 94.9 | 24.2 | 25.5/22.3 | 3.9% | $79M | 319.2% | 6 |

## Protected (Open Positions)

These symbols have open SMA positions and are force-included to prevent the engine from orphaning held positions on watchlist refresh. Pass `--ignore-open-positions` to disable.

| Symbol | RS % | Close | Note |
|---|---:|---:|---|
| NVDA | N/A | 205.10 | PROTECTED: open position; would fail: di_direction |

## Rejections

| Reason | Count | Meaning | Examples |
|---|---:|---|---|
| `price` | 1968 | Latest close is below the minimum price threshold. | AACBR, AAME, AARD, ABAT, ABCL, ABEO, ABEV, ABLV, ABOS, ABSI |
| `share_volume` | 1312 | 20-day average share volume is below the liquidity threshold. | AACB, AAMI, AAPG, AB, ABCB, ABG, ABXL, ACA, ACEL, ACIC |
| `price_above_smas` | 697 | Price is not above SMA50, SMA150, and SMA200. | ABNB, ABT, ACGL, ACHC, ACI, ACM, ACN, ADBE, ADC, ADP |
| `insufficient_or_bad_bars` | 678 | Not enough clean daily bars for 200-day trend and 12-month momentum checks. | AACI, AACO, AACOW, AACP, AACPR, AADX, AAUC, ACAA, ACAAW, ACCL |
| `dollar_volume` | 525 | 50-day average dollar volume is below the liquidity threshold. | ABM, ACAD, ACIW, ADNT, ADPT, ADTN, AEVA, AGIO, AGRO, AGYS |
| `sma_alignment` | 166 | Moving averages are not stacked as SMA50 > SMA150 > SMA200. | A, AAL, AAP, ABBV, ALAB, ALV, AMGN, AMH, AMT, ANET |
| `relative_strength` | 110 | 12-month momentum excluding the latest month is below the top-30% cutoff. | AAON, AAPL, ADI, ADM, AKAM, ALGM, ALKS, ARMK, ARW, AVT |
| `adx` | 99 | ADX14 is below the minimum trend-strength threshold. | AMAT, APGE, ARWR, ASML, BC, BEAM, BG, BIIB, BNL, BRX |
| `above_52w_low` | 37 | Price is not at least 30% above the 52-week low. | AFL, ALL, ALLY, ASB, BCE, CB, CCEP, CFR, CHD, CINF |
| `di_direction` | 26 | +DI is not above -DI, so directional pressure is not bullish. | AESI, APLD, ASX, BAP, BE, BTSG, BUD, CM, CX, DTM |
| `atr_too_high` | 21 | ATR14 / close is too high; the name may be too unstable for SMA trend following. | AAOI, ACMR, AEHR, AMPX, CIFR, CRDO, ENPH, HUT, MOD, MRVL |
| `near_52w_high` | 18 | Price is not at least 75% of the 52-week high. | ASTC, ASTS, DXYZ, FCEL, INTC, IREN, LUNR, MRAM, NVTS, ONDS |
| `fundamental_sanity` | 17 | SMA fundamental sanity check failed. | ACLS, AXSM, CECO, CYTK, DINO, GNRC, INDV, KLIC, LQDA, ON |
| `atr_too_low` | 5 | ATR14 / close is too low; the name may be too quiet for SMA trend following. | AES, CPRX, CWAN, JHG, MASI |
| `biotech_industry` | 1 | Industry is biotech / specialty pharma / diagnostics; binary-catalyst risk is a poor fit for SMA trend-following. | JAZZ |

## Notes

- This script is report-only and does not change the active bot watchlist.
- Sector caps are best-effort because Alpaca asset metadata does not include sector.
- If fundamentals are disabled, the report is a technical/liquidity scan only.
- With `feed=sip`, Basic Alpaca accounts require the request end time to be outside the latest 15-minute restricted window.
- With `feed=iex`, daily volume is multiplied by the synthetic-SIP factor (`utils.market.apply_synthetic_sip_volume`) so the scanner sees the same consolidated-tape-equivalent volume the running bot does.
- Symbols with open SMA positions are force-included as protected entries and shown in a separate table. Use `--ignore-open-positions` to disable.