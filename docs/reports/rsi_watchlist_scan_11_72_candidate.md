# RSI Watchlist Scan - 2026-09-20T13:26:17+00:00

- Rule version: `rsi_watchlist_v3_durable_company_pool`
- Alpaca feed: `sip`
- Data window: 2025-07-27 to 2026-09-20
- Data end timestamp: 2026-09-20T12:23:19+00:00
- Tradable assets considered: 5762
- Assets with bars: 5761
- Fundamentals enforced: True
- Target opportunity-pool size: 50
- Ranked candidates selected: 50
- Protected open-position additions: 0

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
| 1 | MU | $1147.2B | 1015.80 | 58.3 | 0.0% | 0 | 0.0% | 4.8% | 5.8% | 13.2% | $31.5B | 0 |
| 2 | NVDA | $5367.2B | 222.27 | 54.4 | 0.0% | 0 | 0.0% | 2.9% | 3.3% | 11.3% | $27.0B | 0 |
| 3 | SNDK | $262.4B | 1791.82 | 61.4 | 0.0% | 0 | 0.0% | 6.5% | 7.7% | 22.1% | $21.0B | 0 |
| 4 | AAPL | $4905.5B | 336.13 | 64.3 | 0.0% | 2 | -2.6% | 2.2% | 2.2% | 10.7% | $15.5B | 2 |
| 5 | MSFT | $3666.6B | 493.78 | 53.3 | 16.7% | 6 | 1.8% | 2.1% | 2.4% | 6.1% | $13.4B | 0 |
| 6 | TSLA | $1438.7B | 364.27 | 53.8 | 0.0% | 1 | -0.1% | 3.6% | 3.9% | 8.5% | $13.4B | 0 |
| 7 | AMD | $913.9B | 559.82 | 65.4 | 0.0% | 0 | 0.0% | 4.3% | 5.1% | 20.7% | $11.8B | 0 |
| 8 | META | $1696.0B | 665.75 | 65.8 | 33.3% | 3 | 4.4% | 3.2% | 3.1% | 26.0% | $11.0B | 1 |
| 9 | INTC | $574.1B | 108.60 | 61.7 | 0.0% | 0 | 0.0% | 5.4% | 5.6% | 26.6% | $10.4B | 0 |
| 10 | AMZN | $2736.6B | 253.71 | 48.0 | 0.0% | 1 | -2.4% | 2.4% | 2.7% | 7.5% | $10.2B | 0 |
| 11 | AVGO | $1707.1B | 357.61 | 45.5 | 0.0% | 0 | 0.0% | 3.4% | 4.0% | 11.0% | $8.4B | 0 |
| 12 | GOOG | $4212.1B | 344.41 | 53.6 | 100.0% | 1 | 12.5% | 2.3% | 2.6% | 5.7% | $6.2B | 0 |
| 13 | PLTR | $426.9B | 177.64 | 57.0 | 33.3% | 3 | -0.0% | 4.0% | 4.7% | 13.5% | $5.8B | 1 |
| 14 | MRVL | $219.5B | 244.25 | 58.0 | 0.0% | 0 | 0.0% | 5.9% | 5.1% | 19.8% | $5.0B | 0 |
| 15 | TSM | $2254.4B | 434.67 | 57.3 | 0.0% | 0 | 0.0% | 2.5% | 3.4% | 7.8% | $4.9B | 0 |
| 16 | NBIS | $60.8B | 223.54 | 52.0 | 0.0% | 0 | 0.0% | 7.7% | 8.5% | 20.1% | $4.6B | 0 |
| 17 | ORCL | $446.3B | 147.61 | 49.6 | 9.1% | 11 | -0.9% | 5.2% | 5.1% | 16.7% | $4.5B | 5 |
| 18 | STX | $195.3B | 858.79 | 52.9 | 0.0% | 0 | 0.0% | 6.1% | 6.0% | 15.3% | $3.9B | 0 |
| 19 | DELL | $361.2B | 568.06 | 61.8 | 0.0% | 2 | 8.2% | 5.9% | 4.7% | 34.8% | $3.7B | 0 |
| 20 | AMAT | $352.8B | 444.57 | 43.8 | 0.0% | 0 | 0.0% | 4.9% | 4.2% | 20.9% | $3.6B | 0 |
| 21 | BE | $78.2B | 265.63 | 57.5 | 0.0% | 0 | 0.0% | 7.1% | 9.4% | 41.8% | $3.6B | 0 |
| 22 | WDC | $159.1B | 441.36 | 45.8 | 0.0% | 0 | 0.0% | 6.5% | 6.4% | 16.9% | $3.5B | 0 |
| 23 | CRM | $195.8B | 237.92 | 53.5 | 50.0% | 4 | 0.1% | 4.1% | 4.1% | 32.0% | $3.0B | 2 |
| 24 | LLY | $1028.1B | 1152.93 | 47.3 | 100.0% | 1 | 5.8% | 2.6% | 3.1% | 13.6% | $3.0B | 0 |
| 25 | LRCX | $360.5B | 288.11 | 45.5 | 0.0% | 0 | 0.0% | 5.5% | 4.8% | 23.1% | $2.9B | 0 |
| 26 | NFLX | $298.9B | 71.79 | 36.3 | 16.7% | 12 | -2.8% | 3.4% | 3.0% | 15.8% | $2.8B | 7 |
| 27 | ASML | $645.3B | 1679.92 | 48.6 | 0.0% | 0 | 0.0% | 3.3% | 3.6% | 13.1% | $2.7B | 0 |
| 28 | WMT | $849.4B | 106.73 | 46.0 | 0.0% | 3 | 5.0% | 1.9% | 2.1% | 6.2% | $2.6B | 0 |
| 29 | JPM | $929.5B | 349.67 | 44.5 | 0.0% | 0 | 0.0% | 1.9% | 2.1% | 3.8% | $2.5B | 0 |
| 30 | V | $691.4B | 368.29 | 47.3 | 0.0% | 1 | 2.6% | 1.6% | 2.0% | 6.0% | $2.5B | 0 |
| 31 | CRWV | $44.9B | 81.36 | 44.2 | 50.0% | 2 | 28.9% | 7.1% | 8.4% | 23.7% | $2.4B | 1 |
| 32 | MRNA | $61.5B | 154.04 | 63.3 | 0.0% | 0 | 0.0% | 7.6% | 6.2% | 17.3% | $2.4B | 0 |
| 33 | GEV | $250.4B | 940.33 | 48.7 | 100.0% | 1 | 25.2% | 4.5% | 4.6% | 10.8% | $2.4B | 0 |
| 34 | NOW | $140.1B | 135.47 | 52.9 | 16.7% | 6 | -5.3% | 4.7% | 4.9% | 19.2% | $2.4B | 3 |
| 35 | CAT | $371.9B | 808.99 | 47.9 | 0.0% | 1 | 9.3% | 3.1% | 3.2% | 7.1% | $2.3B | 0 |
| 36 | XOM | $672.5B | 163.54 | 53.9 | 0.0% | 0 | 0.0% | 2.3% | 2.2% | 7.8% | $2.3B | 0 |
| 37 | HOOD | $107.7B | 119.82 | 60.0 | 0.0% | 2 | -10.9% | 5.8% | 5.9% | 20.3% | $2.2B | 1 |
| 38 | PANW | $297.4B | 363.58 | 53.1 | 33.3% | 3 | 1.1% | 5.2% | 3.9% | 21.0% | $2.2B | 1 |
| 39 | CSCO | $431.8B | 109.51 | 46.1 | 0.0% | 0 | 0.0% | 2.6% | 2.5% | 4.7% | $2.2B | 0 |
| 40 | BRK.B | $1091.3B | 509.77 | 52.3 | 100.0% | 3 | 4.7% | 1.3% | 1.4% | 4.1% | $2.2B | 0 |
| 41 | APP | $103.5B | 308.06 | 39.8 | 0.0% | 7 | -9.1% | 5.3% | 6.3% | 11.5% | $2.2B | 3 |
| 42 | QCOM | $189.8B | 177.72 | 54.4 | 12.5% | 8 | 1.4% | 4.6% | 3.5% | 21.0% | $2.1B | 3 |
| 43 | CRWD | $243.3B | 237.65 | 60.4 | 33.3% | 3 | 9.0% | 5.4% | 4.3% | 30.9% | $2.1B | 0 |
| 44 | GS | $274.3B | 942.00 | 33.4 | 0.0% | 1 | 4.5% | 3.0% | 2.7% | 14.3% | $2.1B | 0 |
| 45 | KLAC | $231.2B | 176.99 | 46.0 | 0.0% | 0 | 0.0% | 4.8% | 4.3% | 14.7% | $2.0B | 0 |
| 46 | UNH | $338.3B | 376.90 | 38.7 | 0.0% | 3 | -0.5% | 2.6% | 2.7% | 8.8% | $2.0B | 0 |
| 47 | BAC | $403.7B | 57.73 | 29.6 | 0.0% | 2 | 0.1% | 2.2% | 2.0% | 11.4% | $2.0B | 0 |
| 48 | IBM | $216.3B | 229.55 | 44.1 | 0.0% | 3 | 11.8% | 3.3% | 3.2% | 9.4% | $2.0B | 0 |
| 49 | COHR | $62.1B | 317.36 | 56.1 | 100.0% | 1 | 60.2% | 7.2% | 7.3% | 17.9% | $1.9B | 0 |
| 50 | JNJ | $650.6B | 269.99 | 55.1 | 0.0% | 1 | -0.3% | 1.9% | 1.8% | 5.2% | $1.9B | 0 |

## Expansion-Only Review

- Current active pool: 52 symbols
- Current symbols also in the refreshed top 50: 47 (MU, NVDA, SNDK, AAPL, MSFT, TSLA, AMD, META, INTC, AMZN, AVGO, GOOG, PLTR, MRVL, TSM, NBIS, ORCL, STX, DELL, AMAT, BE, WDC, CRM, LLY, LRCX, NFLX, ASML, WMT, JPM, V, CRWV, MRNA, GEV, NOW, CAT, XOM, HOOD, PANW, CSCO, APP, QCOM, CRWD, GS, KLAC, UNH, BAC, IBM)
- Additions needed to reach 50 without one-scan removals: 0
- Highest-ranked nonmembers for review: none
- This is a stability-first review list, not an automatic promotion. Operator approval remains required.

## Risk-Target Coverage

- Ranked symbols measured: 50
- ATR14/close: min=1.27%, p10=2.15%, median=3.98%, p90=6.51%
- Baseline per-position cap: 4.80% of equity
- RSI risk target: 0.25% of equity
- Risk sizing binds at ATR14/close >= 2.60%; 15 symbol(s) are conservatively cap-clipped: AAPL, MSFT, AMZN, GOOG, TSM, LLY, WMT, JPM, V, XOM, CSCO, BRK.B, UNH, BAC, JNJ
- This is a coverage check, not a reason to change the risk target automatically.

## Rejections

| Reason | Count | Meaning | Examples |
|---|---:|---|---|
| `price` | 1990 | Latest close is below the minimum price threshold. | AAME, AARD, ABAT, ABEO, ABEV, ABLV, ABOS, ABSI, ABTS, ABUS |
| `dollar_volume` | 1766 | 50-day average dollar volume is below the liquidity threshold. | AACI, AAMI, AAPG, AAUC, AB, ABCB, ABM, ABTC, ABXL, ACAD |
| `insufficient_or_bad_bars` | 657 | Not enough clean daily bars for durable review and reference metrics. | AAC, AACO, AACOW, AACP, AACPR, AADX, ACAA, ACAAW, ACCL, ACGC |
| `solvency` | 2 | Known cash runway is below the strategy minimum. | LITE, MSTR |
| `nonpreferred_share_class` | 1 | Excluded by share-class policy; use GOOG for Alphabet exposure, never GOOGL. | GOOGL |

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