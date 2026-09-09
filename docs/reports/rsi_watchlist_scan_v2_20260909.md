# RSI Watchlist Scan - 2026-09-09T14:28:43+00:00

> **Superseded:** Historical v2 scan. Its temporary technical-state and RSI14
> outcome gates were retired by `rsi_watchlist_v3_durable_company_pool`. Do not
> use this list for promotion.

- Rule version: `rsi_watchlist_v2_forward_pool`
- Alpaca feed: `sip`
- Data window: 2025-07-16 to 2026-09-09
- Data end timestamp: 2026-09-09T13:13:20+00:00
- Tradable assets considered: 5747
- Assets with bars: 5746
- Fundamentals enforced: True
- Target opportunity-pool size: 50
- Ranked candidates selected: 50
- Protected open-position additions: 2

## Rule Rationale

- Liquidity and market-cap filters keep RSI in names that can absorb limit orders.
- Solvency keeps RSI from buying companies where a sell-off may be terminal.
- Price above SMA200 keeps entries in structurally intact names.
- 52-week position avoids deep breakdowns while allowing deep pullbacks (down to 40% off highs) required for mean-reversion.
- Recent oversold-event behavior ranks plausible future candidates; it is evidence, not a promise or a backtest-based promotion gate.
- ATR and Bollinger width require enough movement for opportunity without chaos.
- One-day and five-day crash filters avoid news shocks and falling knives.

## Top Candidates

| Rank | Symbol | Score | Close | RSI | Hit % | Events | Avg10d | ATR % | BB Width | $Vol50 | Stops |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | ENTG | 96.4 | 140.91 | 50.4 | 100.0% | 2 | 24.1% | 5.3% | 29.9% | $365M | 0 |
| 2 | AEM | 94.3 | 205.06 | 59.0 | 100.0% | 3 | 9.5% | 3.9% | 23.6% | $518M | 0 |
| 3 | DINO | 93.1 | 106.80 | 70.6 | 100.0% | 3 | 5.5% | 3.4% | 21.6% | $237M | 0 |
| 4 | ABNB | 89.8 | 170.65 | 43.7 | 100.0% | 2 | 11.0% | 3.2% | 11.2% | $742M | 0 |
| 5 | GFI | 89.5 | 47.33 | 63.2 | 100.0% | 2 | 8.7% | 3.7% | 25.0% | $145M | 0 |
| 6 | AROC | 87.5 | 33.53 | 52.9 | 100.0% | 2 | 9.5% | 3.4% | 13.7% | $60M | 0 |
| 7 | RGLD | 87.2 | 263.12 | 64.9 | 100.0% | 2 | 10.4% | 2.9% | 21.0% | $152M | 0 |
| 8 | GKOS | 87.2 | 182.19 | 56.9 | 66.7% | 3 | 11.2% | 3.5% | 7.7% | $127M | 0 |
| 9 | CNC | 85.9 | 62.60 | 41.8 | 66.7% | 3 | 8.3% | 3.6% | 8.4% | $287M | 0 |
| 10 | ADM | 82.3 | 84.90 | 61.4 | 100.0% | 2 | 5.0% | 3.1% | 10.6% | $288M | 0 |
| 11 | NTAP | 80.8 | 189.14 | 53.1 | 50.0% | 2 | 7.5% | 4.3% | 16.7% | $463M | 0 |
| 12 | EGO | 80.8 | 44.89 | 61.2 | 50.0% | 2 | 15.4% | 4.3% | 25.7% | $93M | 0 |
| 13 | OKTA | 80.4 | 170.72 | 62.5 | 66.7% | 3 | 14.2% | 5.0% | 36.2% | $480M | 1 |
| 14 | GFL | 79.5 | 42.35 | 54.4 | 83.3% | 6 | 6.2% | 2.8% | 8.4% | $102M | 0 |
| 15 | RELY | 79.2 | 24.95 | 47.6 | 50.0% | 2 | 7.7% | 4.4% | 12.4% | $77M | 0 |
| 16 | TDW | 79.0 | 93.36 | 55.2 | 50.0% | 2 | 7.4% | 4.1% | 9.9% | $53M | 0 |
| 17 | CMG | 78.3 | 36.42 | 52.2 | 66.7% | 6 | 7.1% | 3.2% | 21.2% | $580M | 0 |
| 18 | KDP | 77.9 | 32.56 | 58.8 | 100.0% | 2 | 7.2% | 2.4% | 10.3% | $364M | 0 |
| 19 | NOG | 77.7 | 25.92 | 59.8 | 66.7% | 3 | 9.8% | 3.2% | 12.4% | $58M | 0 |
| 20 | ZETA | 77.5 | 30.48 | 58.6 | 50.0% | 2 | 7.5% | 4.8% | 17.7% | $213M | 0 |
| 21 | PAAS | 77.0 | 52.61 | 56.2 | 50.0% | 2 | 10.9% | 4.0% | 17.8% | $212M | 0 |
| 22 | HPE | 76.5 | 56.87 | 59.4 | 50.0% | 2 | 9.0% | 5.6% | 18.0% | $966M | 0 |
| 23 | DLTR | 76.4 | 121.00 | 38.3 | 50.0% | 4 | 8.7% | 4.0% | 11.2% | $318M | 0 |
| 24 | DIOD | 76.2 | 91.27 | 48.3 | 50.0% | 2 | 12.6% | 5.5% | 25.5% | $54M | 0 |
| 25 | LH | 76.1 | 322.13 | 49.6 | 100.0% | 2 | 6.6% | 2.3% | 7.6% | $204M | 0 |
| 26 | SNOW | 76.0 | 334.26 | 56.1 | 50.0% | 2 | 6.2% | 5.0% | 12.2% | $1.5B | 0 |
| 27 | OSCR | 75.6 | 30.91 | 50.5 | 50.0% | 2 | 22.4% | 5.8% | 12.7% | $154M | 0 |
| 28 | EW | 74.2 | 87.01 | 38.1 | 100.0% | 3 | 4.6% | 2.3% | 6.9% | $344M | 0 |
| 29 | ALKS | 73.8 | 45.20 | 32.5 | 50.0% | 2 | 15.6% | 3.3% | 13.1% | $78M | 0 |
| 30 | GPN | 73.6 | 88.46 | 47.5 | 75.0% | 4 | 8.8% | 3.3% | 8.0% | $257M | 1 |
| 31 | LPLA | 71.4 | 346.30 | 42.8 | 66.7% | 3 | 7.6% | 2.9% | 9.0% | $230M | 0 |
| 32 | KGC | 71.2 | 30.54 | 55.9 | 50.0% | 2 | 8.3% | 4.0% | 26.9% | $214M | 0 |
| 33 | CART | 70.9 | 46.32 | 39.0 | 40.0% | 5 | 6.5% | 3.9% | 12.0% | $190M | 0 |
| 34 | MA | 70.6 | 566.99 | 45.1 | 100.0% | 2 | 4.9% | 1.7% | 8.7% | $1.6B | 0 |
| 35 | WFRD | 70.0 | 95.18 | 56.0 | 66.7% | 3 | 6.5% | 3.8% | 11.7% | $101M | 1 |
| 36 | BG | 70.0 | 122.62 | 61.5 | 50.0% | 2 | 6.3% | 3.6% | 13.6% | $161M | 0 |
| 37 | PARR | 69.9 | 81.01 | 56.2 | 50.0% | 2 | 4.6% | 5.4% | 14.2% | $73M | 0 |
| 38 | VEEV | 69.8 | 264.06 | 58.1 | 50.0% | 6 | 2.5% | 4.0% | 24.8% | $410M | 1 |
| 39 | ALSN | 68.6 | 128.88 | 55.6 | 50.0% | 2 | 6.5% | 3.1% | 9.4% | $131M | 0 |
| 40 | DDOG | 68.2 | 218.52 | 42.6 | 60.0% | 5 | 3.0% | 5.9% | 24.9% | $1.1B | 1 |
| 41 | FLS | 67.6 | 76.21 | 44.1 | 50.0% | 2 | 7.7% | 3.0% | 10.1% | $129M | 0 |
| 42 | STNG | 67.5 | 82.31 | 61.5 | 50.0% | 2 | 7.8% | 2.7% | 9.6% | $57M | 0 |
| 43 | BALL | 67.5 | 60.52 | 36.7 | 60.0% | 5 | 7.0% | 2.0% | 6.8% | $113M | 0 |
| 44 | NDAQ | 66.6 | 94.39 | 43.8 | 80.0% | 5 | 7.4% | 2.2% | 6.2% | $310M | 1 |
| 45 | TFC | 66.6 | 50.49 | 46.7 | 75.0% | 4 | 5.1% | 1.8% | 8.0% | $342M | 0 |
| 46 | CNH | 65.2 | 13.62 | 67.6 | 50.0% | 6 | 1.8% | 4.1% | 38.9% | $198M | 1 |
| 47 | SN | 64.9 | 173.18 | 45.6 | 50.0% | 4 | 1.2% | 3.6% | 14.4% | $276M | 1 |
| 48 | MIAX | 64.6 | 43.31 | 51.4 | 50.0% | 2 | 5.3% | 4.1% | 9.7% | $58M | 0 |
| 49 | OXY | 62.9 | 61.74 | 63.5 | 50.0% | 2 | 5.9% | 2.4% | 7.4% | $458M | 0 |
| 50 | RBRK | 62.0 | 90.92 | 49.2 | 50.0% | 2 | 6.4% | 6.0% | 23.2% | $285M | 1 |

## Protected Open RSI Positions

These symbols are retained outside the 50-name opportunity pool until their RSI positions are flat, so normal strategy exits remain active.

| Symbol | Note |
|---|---|
| CCK | PROTECTED: open RSI position; outside refreshed top pool |
| SPG | PROTECTED: open RSI position; outside refreshed top pool |

## Expansion-Only Review

- Current active pool: 31 symbols
- Current symbols also in the refreshed top 50: 4 (ABNB, MA, TFC, SN)
- Additions needed to reach 50 without one-scan removals: 19
- Highest-ranked nonmembers for review: ENTG, AEM, DINO, GFI, AROC, RGLD, GKOS, CNC, ADM, NTAP, EGO, OKTA, GFL, RELY, TDW, CMG, KDP, NOG, ZETA
- This is a stability-first review list, not an automatic promotion. Sector/correlation review and operator approval remain required.

## Risk-Target Coverage

- Ranked symbols measured: 50
- ATR14/close: min=1.70%, p10=2.30%, median=3.60%, p90=5.40%
- Baseline per-position cap: 4.80% of equity
- RSI risk target: 0.25% of equity
- Risk sizing binds at ATR14/close >= 2.60%; 8 ranked symbols are conservatively cap-clipped: KDP, LH, EW, MA, BALL, NDAQ, TFC, OXY
- CCK and SPG are temporarily retained for open-position management and are also below the baseline binding threshold in the latest available delayed-SIP cache. This reduces their approved risk; it does not increase risk.
- This is a coverage check, not a reason to change the risk target automatically.

## Rejections

| Reason | Count | Meaning | Examples |
|---|---:|---|---|
| `price` | 1951 | Latest close is below the minimum price threshold. | AAME, AARD, ABAT, ABEO, ABEV, ABLV, ABOS, ABSI, ABTC, ABTS |
| `share_volume` | 1499 | 20-day average share volume is below the liquidity threshold. | AACI, AAMI, AAPG, AB, ABCB, ABG, ABM, ABXL, ACEL, ACFN |
| `insufficient_or_bad_bars` | 649 | Not enough clean daily bars for 200-day structure and 1-year RSI event checks. | AAC, AACO, AACOW, AACP, AACPR, AADX, ACAA, ACAAW, ACCL, ACGC |
| `dollar_volume` | 489 | 50-day average dollar volume is below the liquidity threshold. | AAUC, ACAD, ADEA, ADNT, AESI, AEVA, AGIO, AGRO, AI, AIP |
| `below_sma200` | 442 | Price is below SMA200, so the stock may be structurally broken. | AA, AAL, AAON, AAP, ACGL, ACI, ACM, ACN, ADBE, ADC |
| `oversold_events` | 314 | Too few RSI oversold events in the last year. | ABBV, ADI, ADPT, AEP, AG, AGCO, AHR, AKAM, ALL, ALM |
| `reversion_hit_rate` | 207 | Historical oversold events did not revert often enough. | A, AAPL, ABCL, ABT, ACHC, ACIW, ADP, AFRM, AJG, ALLE |
| `too_close_to_low` | 42 | Price is not far enough above the 52-week low. | AEE, AFL, AMH, AMT, ARCC, AWK, BLK, BRK.B, CHD, CUBE |
| `too_far_from_high` | 23 | Price is too far below the 52-week high. | AAOI, ACMR, ASST, AXTI, BAND, BMNR, BTDR, CLSK, CRCL, DUOL |
| `atr_too_low` | 21 | ATR14 / close is too low; the name may be too quiet for RSI reversion. | ACA, AES, ARR, ATKR, BRX, CBZ, CZR, DBRG, DX, EPD |
| `atr_too_high` | 15 | ATR14 / close is too high; the name may be too chaotic for RSI reversion. | AEHR, ALAB, BRZE, COHR, DYN, FSLY, HUT, IREN, MRNA, NBIS |
| `bb_width` | 10 | Bollinger Band width is too narrow; not enough volatility for meaningful reversion. | AGNC, BKH, CB, CTRE, ETR, EXPD, HSIC, MPLX, OHI, TRV |
| `market_cap` | 2 | Market capitalization is below the RSI minimum size threshold. | CMC, USFD |

## Notes

- This script is report-only and does not change the active bot watchlist.
- Earnings-calendar blocking is not implemented yet; treat as `not_checked`.
- Sector caps are best-effort because Alpaca asset metadata does not include sector.
- Fundamentals are checked only after technical filters pass; requested-symbol explanations can show `MarketCap=N/A` when a symbol failed earlier.
- If fundamentals are disabled, market cap and solvency are not enforced.
- With `feed=sip`, Basic Alpaca accounts require the request end time to be outside the latest 15-minute restricted window.
- With `feed=iex`, volume is IEX venue volume, not consolidated market volume.
