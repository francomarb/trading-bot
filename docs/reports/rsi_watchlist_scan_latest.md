# RSI Watchlist Scan - 2026-09-09T17:06:25+00:00

- Rule version: `rsi_watchlist_v3_durable_company_pool`
- Alpaca feed: `sip`
- Data window: 2025-07-16 to 2026-09-09
- Data end timestamp: 2026-09-09T16:02:19+00:00
- Tradable assets considered: 5747
- Assets with bars: 5746
- Fundamentals enforced: True
- Target opportunity-pool size: 50
- Ranked candidates selected: 50
- Protected open-position additions: 2

## Rule Rationale

- Price, dollar liquidity, market cap, and affirmative solvency are the only company eligibility gates.
- Dollar liquidity orders eligible companies because it is durable and directly relevant to execution quality.
- Sector concentration is accepted; no sector cap or diversification reranking is applied.
- Alphabet share-class policy is explicit: preserve eligible GOOG and never select GOOGL.
- SMA200, 52-week location, volatility, recent returns, and historical RSI outcomes are reference-only columns.
- The active RSI3 strategy and its runtime gates decide whether an eligible company can actually enter.

## Top Candidates

| Rank | Symbol | Mkt Cap | Close | RSI14 | Hit % | Events | Avg10d | ATR % | Median ATR % | BB Width | $Vol50 | Stops |
|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | MU | $1153.1B | 1023.20 | 60.2 | 0.0% | 0 | 0.0% | 5.3% | 5.8% | 12.6% | $34.6B | 0 |
| 2 | NVDA | $5411.6B | 224.18 | 54.8 | 0.0% | 0 | 0.0% | 3.2% | 3.3% | 10.9% | $27.3B | 0 |
| 3 | SNDK | $260.0B | 1776.42 | 62.7 | 0.0% | 0 | 0.0% | 6.9% | 7.7% | 24.8% | $21.9B | 0 |
| 4 | AAPL | $4569.6B | 314.93 | 49.0 | 0.0% | 2 | -2.6% | 2.3% | 2.2% | 8.8% | $15.2B | 2 |
| 5 | TSLA | $1456.0B | 368.83 | 55.9 | 0.0% | 1 | -0.1% | 4.0% | 3.9% | 13.1% | $14.2B | 0 |
| 6 | MSFT | $3661.4B | 493.43 | 55.9 | 16.7% | 6 | 1.8% | 2.3% | 2.4% | 7.8% | $13.9B | 0 |
| 7 | AMD | $854.0B | 523.04 | 60.8 | 0.0% | 0 | 0.0% | 4.4% | 5.1% | 14.4% | $12.2B | 0 |
| 8 | META | $1659.4B | 651.21 | 70.0 | 33.3% | 3 | 3.5% | 3.3% | 3.1% | 16.4% | $10.9B | 2 |
| 9 | INTC | $557.5B | 105.43 | 62.6 | 0.0% | 0 | 0.0% | 5.1% | 5.6% | 23.9% | $10.6B | 0 |
| 10 | AMZN | $2717.6B | 251.89 | 43.6 | 0.0% | 1 | -2.4% | 2.6% | 2.7% | 6.3% | $10.5B | 0 |
| 11 | AVGO | $1725.5B | 362.25 | 42.2 | 0.0% | 0 | 0.0% | 3.6% | 4.0% | 19.7% | $8.3B | 0 |
| 12 | GOOG | $4012.6B | 328.10 | 38.8 | 100.0% | 1 | 12.5% | 2.4% | 2.6% | 5.3% | $6.2B | 0 |
| 13 | PLTR | $410.5B | 171.03 | 51.8 | 33.3% | 3 | -0.0% | 4.7% | 4.7% | 12.5% | $6.0B | 1 |
| 14 | MRVL | $213.5B | 237.90 | 56.6 | 0.0% | 0 | 0.0% | 6.5% | 5.1% | 21.7% | $5.5B | 0 |
| 15 | TSM | $2253.1B | 434.61 | 58.6 | 0.0% | 0 | 0.0% | 2.6% | 3.4% | 7.5% | $5.2B | 0 |
| 16 | NBIS | $65.8B | 242.77 | 58.1 | 0.0% | 0 | 0.0% | 7.6% | 8.5% | 36.8% | $4.6B | 0 |
| 17 | ORCL | $467.1B | 162.55 | 63.4 | 9.1% | 11 | -0.9% | 4.3% | 5.0% | 15.7% | $4.3B | 5 |
| 18 | AMAT | $371.3B | 467.64 | 44.2 | 0.0% | 0 | 0.0% | 4.8% | 4.1% | 26.7% | $4.3B | 0 |
| 19 | STX | $206.6B | 912.94 | 58.0 | 0.0% | 0 | 0.0% | 6.0% | 6.0% | 23.8% | $3.9B | 0 |
| 20 | WDC | $177.4B | 493.12 | 53.8 | 0.0% | 0 | 0.0% | 6.5% | 6.2% | 19.5% | $3.7B | 0 |
| 21 | BE | $80.6B | 273.13 | 67.3 | 0.0% | 0 | 0.0% | 7.1% | 9.4% | 31.5% | $3.3B | 0 |
| 22 | DELL | $349.8B | 543.60 | 66.5 | 0.0% | 2 | 8.2% | 5.8% | 4.7% | 25.0% | $3.3B | 0 |
| 23 | LRCX | $395.4B | 316.16 | 52.1 | 0.0% | 0 | 0.0% | 5.0% | 4.6% | 18.5% | $3.2B | 0 |
| 24 | LLY | $1003.1B | 1124.92 | 37.3 | 100.0% | 1 | 5.8% | 3.0% | 3.0% | 15.4% | $3.1B | 0 |
| 25 | ASML | $667.7B | 1737.87 | 50.7 | 0.0% | 0 | 0.0% | 3.2% | 3.5% | 13.9% | $2.8B | 0 |
| 26 | CRM | $203.7B | 247.66 | 66.3 | 50.0% | 4 | 0.1% | 4.2% | 4.1% | 44.3% | $2.8B | 2 |
| 27 | NFLX | $317.1B | 76.16 | 44.0 | 16.7% | 12 | -2.8% | 3.0% | 3.0% | 12.2% | $2.8B | 7 |
| 28 | WMT | $844.1B | 106.06 | 43.5 | 0.0% | 3 | 5.0% | 2.2% | 2.1% | 17.4% | $2.7B | 0 |
| 29 | V | $688.1B | 368.42 | 48.1 | 0.0% | 1 | 2.6% | 1.7% | 2.0% | 8.7% | $2.6B | 0 |
| 30 | JPM | $943.3B | 354.85 | 49.9 | 0.0% | 0 | 0.0% | 1.7% | 2.1% | 4.4% | $2.6B | 0 |
| 31 | GEV | $254.2B | 954.81 | 47.4 | 100.0% | 1 | 25.2% | 4.3% | 4.5% | 21.8% | $2.5B | 0 |
| 32 | CAT | $375.7B | 817.07 | 46.5 | 0.0% | 1 | 9.3% | 3.2% | 3.2% | 12.0% | $2.4B | 0 |
| 33 | NOW | $135.6B | 131.26 | 51.6 | 16.7% | 6 | -5.3% | 5.1% | 4.9% | 26.3% | $2.4B | 3 |
| 34 | CRWV | $53.0B | 96.12 | 57.1 | 50.0% | 2 | 28.9% | 6.9% | 8.4% | 35.0% | $2.4B | 1 |
| 35 | KLAC | $238.0B | 182.03 | 45.1 | 0.0% | 0 | 0.0% | 5.0% | 4.2% | 25.3% | $2.4B | 0 |
| 36 | APP | $103.8B | 309.33 | 37.7 | 0.0% | 7 | -9.1% | 5.3% | 6.3% | 6.5% | $2.3B | 3 |
| 37 | CSCO | $432.9B | 109.88 | 43.3 | 0.0% | 0 | 0.0% | 2.4% | 2.5% | 11.5% | $2.3B | 0 |
| 38 | MRNA | $54.9B | 137.53 | 59.1 | 0.0% | 0 | 0.0% | 9.3% | 6.2% | 107.5% | $2.3B | 0 |
| 39 | PANW | $273.5B | 336.04 | 44.6 | 33.3% | 3 | 1.1% | 5.3% | 3.8% | 25.0% | $2.2B | 1 |
| 40 | HOOD | $104.7B | 116.75 | 60.1 | 0.0% | 2 | -10.9% | 5.7% | 5.9% | 31.9% | $2.2B | 1 |
| 41 | XOM | $673.5B | 163.79 | 58.8 | 0.0% | 0 | 0.0% | 2.2% | 2.2% | 7.4% | $2.2B | 0 |
| 42 | IBM | $222.1B | 236.27 | 52.0 | 0.0% | 3 | 11.8% | 2.7% | 3.2% | 4.3% | $2.0B | 0 |
| 43 | QCOM | $188.1B | 176.12 | 62.0 | 12.5% | 8 | 1.4% | 3.8% | 3.4% | 11.1% | $2.0B | 3 |
| 44 | UNH | $353.7B | 393.74 | 44.5 | 0.0% | 3 | -0.5% | 2.6% | 2.7% | 5.1% | $2.0B | 0 |
| 45 | GS | $300.4B | 1032.17 | 50.2 | 0.0% | 1 | 4.5% | 2.5% | 2.7% | 5.4% | $2.0B | 0 |
| 46 | GLW | $145.0B | 168.97 | 59.5 | 0.0% | 0 | 0.0% | 5.3% | 5.3% | 20.6% | $1.9B | 0 |
| 47 | COST | $399.0B | 899.27 | 33.4 | 100.0% | 1 | 3.8% | 1.8% | 1.9% | 8.4% | $1.9B | 0 |
| 48 | TXN | $239.5B | 262.23 | 45.4 | 0.0% | 1 | 2.7% | 3.2% | 3.1% | 12.7% | $1.9B | 0 |
| 49 | CRWD | $213.4B | 208.49 | 50.9 | 33.3% | 3 | 9.0% | 5.6% | 4.1% | 25.6% | $1.9B | 0 |
| 50 | BAC | $438.7B | 62.71 | 55.2 | 0.0% | 1 | 0.5% | 1.7% | 2.0% | 6.2% | $1.9B | 0 |

## Protected Open RSI Positions

These symbols are retained outside the 50-name opportunity pool until their RSI positions are flat, so normal strategy exits remain active.

| Symbol | Note |
|---|---|
| ABNB | PROTECTED: open RSI position; outside refreshed top pool |
| CCK | PROTECTED: open RSI position; outside refreshed top pool |

## Expansion-Only Review

- Current active pool: 31 symbols
- Current symbols also in the refreshed top 50: 6 (AAPL, MSFT, META, AMZN, NFLX, CAT)
- Additions needed to reach 50 without one-scan removals: 19
- Highest-ranked nonmembers for review: MU, NVDA, SNDK, TSLA, AMD, INTC, AVGO, GOOG, PLTR, MRVL, TSM, NBIS, ORCL, AMAT, STX, WDC, BE, DELL, LRCX
- This is a stability-first review list, not an automatic promotion. Operator approval remains required.

## Risk-Target Coverage

- Ranked symbols measured: 50
- ATR14/close: min=1.70%, p10=2.22%, median=4.01%, p90=6.50%
- Baseline per-position cap: 4.80% of equity
- RSI risk target: 0.25% of equity
- Risk sizing binds at ATR14/close >= 2.60%; 12 symbol(s) are conservatively cap-clipped: AAPL, MSFT, AMZN, GOOG, WMT, V, JPM, CSCO, XOM, GS, COST, BAC
- Protected open-position coverage is reported separately: 2 measured; 1 conservatively cap-clipped (CCK).
- This is a coverage check, not a reason to change the risk target automatically.

## Rejections

| Reason | Count | Meaning | Examples |
|---|---:|---|---|
| `price` | 1958 | Latest close is below the minimum price threshold. | AAME, AARD, ABAT, ABEO, ABEV, ABLV, ABOS, ABSI, ABTC, ABTS |
| `dollar_volume` | 1812 | 50-day average dollar volume is below the liquidity threshold. | AACI, AAMI, AAPG, AAUC, AB, ABCB, ABM, ABXL, ACAD, ACEL |
| `insufficient_or_bad_bars` | 649 | Not enough clean daily bars for durable review and reference metrics. | AAC, AACO, AACOW, AACP, AACPR, AADX, ACAA, ACAAW, ACCL, ACGC |
| `solvency` | 3 | Solvency was not affirmatively established or the fundamentals request failed. | LITE, MSTR, ISRG |
| `nonpreferred_share_class` | 1 | Excluded by share-class policy; use GOOG for Alphabet exposure, never GOOGL. | GOOGL |
| `market_cap` | 1 | Market capitalization is below the RSI minimum size threshold. | BRK.B |

## Notes

- This script is report-only and does not change the active bot watchlist.
- Earnings-calendar blocking is not implemented yet; treat as `not_checked`.
- Sector concentration is accepted by design; the scanner does not cap or rerank sectors.
- GOOGL is always excluded; eligible GOOG is preserved in the selected pool.
- Fundamentals are checked in dollar-liquidity order until a 2x candidate review buffer qualifies; this avoids rate-limited full-universe lookups.
- Fundamental rejection counts cover that reviewed buffer, not every operationally eligible symbol.
- If fundamentals are disabled, market cap and solvency are not enforced.
- With `feed=sip`, Basic Alpaca accounts require the request end time to be outside the latest 15-minute restricted window.
- With `feed=iex`, volume is IEX venue volume, not consolidated market volume.