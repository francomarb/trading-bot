# SMA Durable Watchlist Scan - 2026-09-28T16:34:44+00:00

- Rule version: `sma_watchlist_v3_durable_liquid_pool`
- Ranking: `liquidity`
- Alpaca feed: `sip`
- Data window: 2025-08-03 to 2026-09-27
- Tradable assets considered: 5758
- Assets with bars: 5756
- Fundamentals enforced: True
- Promoted pool size under review: 100
- Promotion status: approved and promoted to paper configuration on 2026-09-28

## Selection Contract

- Membership uses durable completed-session price, dollar liquidity, size, solvency, and share-class rules only.
- Liquidity ordering is an execution priority, not a return forecast.
- Trend, crossover history, ATR, FCF, revenue, and sector are diagnostics only.
- The strategy and runtime filters decide whether a member may enter.
- This script is report-only and never edits the active watchlist.

## Nested Pool Comparison

| Pool | Crossovers (252d) | Active days | Peak same-day | Days above hard 8-position count | Zero-cross names | Median ATR% | Cap-clipped |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 25 | 62 | 50 | 3 | 0 | 2 | 3.92% | 8 |
| 50 | 119 | 83 | 4 | 0 | 3 | 3.63% | 22 |
| 100 | 243 | 142 | 5 | 0 | 3 | 3.63% | 47 |
| 200 | 491 | 193 | 11 | 3 | 5 | 3.23% | 97 |

Counts characterize opportunity coverage among companies selected today; they are not a point-in-time backtest. The hard-position column is an upper-bound count diagnostic, not sleeve-dollar capacity; `SLEEVE_FULL` can bind first and is the forward starvation metric.

## Ranked Candidates

| Rank | Symbol | Close | $Vol50 | Mom 12-1 | 52w % | ATR % | SMA stack | Crosses | FCF | Revenue |
|---:|---|---:|---:|---:|---:|---:|---|---:|---|---|
| 1 | MU | 1082.28 | $30.5B | 481.2% | 86.3% | 4.4% | yes | 2 | pass | pass |
| 2 | NVDA | 225.07 | $26.5B | 18.6% | 95.4% | 2.6% | yes | 4 | pass | pass |
| 3 | SNDK | 1777.80 | $20.3B | 1401.9% | 75.5% | 6.3% | yes | 1 | pass | pass |
| 4 | AAPL | 341.07 | $15.0B | 24.7% | 98.8% | 2.0% | yes | 4 | pass | pass |
| 5 | TSLA | 372.11 | $13.5B | -21.9% | 74.6% | 3.4% | no | 3 | pass | fail |
| 6 | MSFT | 516.17 | $13.5B | -1.9% | 94.0% | 2.3% | no | 2 | pass | pass |
| 7 | AMD | 630.63 | $12.2B | 198.9% | 98.7% | 3.9% | yes | 3 | pass | pass |
| 8 | META | 751.66 | $12.0B | -24.1% | 96.4% | 3.6% | no | 4 | pass | pass |
| 9 | INTC | 123.00 | $10.9B | 182.6% | 86.4% | 5.1% | yes | 3 | fail | fail |
| 10 | AMZN | 249.67 | $10.3B | 18.2% | 86.9% | 2.3% | no | 4 | pass | pass |
| 11 | AVGO | 352.81 | $8.4B | 5.4% | 71.5% | 3.1% | no | 2 | pass | pass |
| 12 | GOOG | 341.08 | $6.3B | 37.1% | 84.4% | 2.5% | no | 2 | pass | pass |
| 13 | PLTR | 189.67 | $5.8B | -1.1% | 91.4% | 3.6% | no | 3 | pass | pass |
| 14 | MRVL | 261.94 | $4.9B | 206.7% | 79.4% | 5.2% | yes | 3 | pass | pass |
| 15 | TSM | 450.61 | $4.7B | 49.7% | 94.3% | 2.3% | yes | 3 | pass | pass |
| 16 | NBIS | 237.33 | $4.6B | 88.9% | 79.1% | 7.0% | yes | 5 | fail | pass |
| 17 | ORCL | 137.10 | $4.3B | -51.2% | 42.9% | 5.2% | no | 2 | fail | pass |
| 18 | DELL | 562.89 | $3.8B | 255.6% | 94.5% | 5.6% | yes | 1 | pass | pass |
| 19 | STX | 916.83 | $3.8B | 281.5% | 80.2% | 5.4% | yes | 2 | pass | pass |
| 20 | BE | 288.70 | $3.7B | 215.4% | 82.2% | 6.6% | yes | 3 | pass | pass |
| 21 | AMAT | 485.00 | $3.5B | 139.5% | 65.6% | 4.1% | no | 0 | pass | pass |
| 22 | WDC | 456.81 | $3.5B | 328.1% | 57.1% | 6.0% | no | 0 | pass | pass |
| 23 | CRM | 234.02 | $3.1B | -15.8% | 87.4% | 3.8% | no | 4 | pass | pass |
| 24 | LLY | 1183.46 | $3.0B | 61.3% | 91.6% | 2.7% | yes | 1 | pass | pass |
| 25 | LRCX | 315.21 | $2.9B | 144.7% | 72.0% | 4.7% | yes | 1 | pass | pass |
| 26 | MRNA | 198.88 | $2.7B | 478.3% | 98.1% | 6.5% | yes | 3 | fail | fail |
| 27 | NFLX | 71.14 | $2.7B | -32.3% | 57.0% | 3.0% | no | 2 | pass | pass |
| 28 | WMT | 107.98 | $2.7B | 2.4% | 80.1% | 2.0% | no | 2 | pass | pass |
| 29 | CRWV | 87.59 | $2.5B | -34.0% | 57.2% | 6.2% | no | 3 | fail | pass |
| 30 | JPM | 343.06 | $2.5B | 15.9% | 93.6% | 1.9% | no | 3 | fail | pass |
| 31 | V | 367.38 | $2.4B | 14.3% | 95.3% | 1.7% | no | 3 | pass | pass |
| 32 | ASML | 1743.94 | $2.4B | 85.4% | 87.3% | 3.0% | yes | 2 | pass | pass |
| 33 | NOW | 135.62 | $2.3B | -32.6% | 70.3% | 4.3% | no | 2 | pass | pass |
| 34 | GEV | 957.63 | $2.3B | 51.9% | 80.1% | 3.9% | no | 2 | pass | pass |
| 35 | XOM | 160.59 | $2.3B | 42.1% | 92.2% | 2.3% | yes | 2 | pass | fail |
| 36 | CAT | 821.58 | $2.3B | 76.5% | 76.7% | 2.8% | no | 1 | pass | pass |
| 37 | PANW | 374.74 | $2.2B | 69.1% | 93.9% | 4.7% | yes | 1 | pass | pass |
| 38 | BRK.B | 505.48 | $2.2B | 1.6% | 94.0% | 1.2% | no | 4 | pass | fail |
| 39 | CSCO | 106.70 | $2.2B | 70.4% | 82.2% | 2.7% | no | 3 | pass | pass |
| 40 | HOOD | 119.40 | $2.2B | -14.4% | 77.6% | 5.2% | no | 3 | pass | pass |
| 41 | CRWD | 252.13 | $2.2B | 58.9% | 95.6% | 4.8% | yes | 3 | pass | pass |
| 42 | QCOM | 201.97 | $2.2B | -4.2% | 78.4% | 4.3% | no | 4 | pass | pass |
| 43 | APP | 310.75 | $2.1B | -52.0% | 41.7% | 5.2% | no | 2 | pass | pass |
| 44 | GS | 935.45 | $2.0B | 33.2% | 81.5% | 2.8% | no | 2 | fail | pass |
| 45 | BAC | 56.70 | $2.0B | 22.3% | 87.4% | 2.1% | no | 2 | pass | pass |
| 46 | COHR | 295.83 | $2.0B | 176.4% | 67.2% | 6.8% | no | 0 | fail | pass |
| 47 | KLAC | 187.92 | $2.0B | 73.0% | 61.2% | 4.2% | no | 1 | pass | pass |
| 48 | UNH | 376.59 | $1.9B | 16.3% | 82.1% | 2.4% | no | 2 | pass | pass |
| 49 | COST | 922.76 | $1.9B | 1.7% | 84.3% | 1.7% | no | 3 | pass | pass |
| 50 | TXN | 278.07 | $1.8B | 45.6% | 83.7% | 3.0% | yes | 2 | pass | pass |
| 51 | JNJ | 271.22 | $1.8B | 56.2% | 96.5% | 1.9% | yes | 1 | pass | pass |
| 52 | IREN | 44.12 | $1.8B | -16.0% | 57.4% | 6.7% | no | 3 | fail | pass |
| 53 | CVX | 204.45 | $1.8B | 30.7% | 93.9% | 2.1% | yes | 3 | pass | fail |
| 54 | SMCI | 43.26 | $1.7B | -19.1% | 73.6% | 5.4% | yes | 3 | fail | pass |
| 55 | COIN | 195.11 | $1.6B | -43.5% | 48.5% | 5.9% | no | 3 | pass | pass |
| 56 | GLW | 156.74 | $1.6B | 92.5% | 57.8% | 5.2% | no | 2 | pass | pass |
| 57 | MA | 567.65 | $1.6B | 5.9% | 94.4% | 1.6% | no | 3 | pass | pass |
| 58 | SNOW | 335.94 | $1.6B | 41.7% | 87.4% | 4.4% | yes | 1 | pass | pass |
| 59 | IBM | 225.51 | $1.5B | -11.8% | 68.3% | 3.1% | no | 2 | pass | pass |
| 60 | VRT | 253.28 | $1.4B | 86.4% | 66.7% | 5.0% | no | 1 | pass | pass |
| 61 | MRK | 148.78 | $1.4B | 96.6% | 95.4% | 2.4% | yes | 1 | pass | pass |
| 62 | KO | 87.81 | $1.4B | 38.3% | 95.5% | 1.5% | yes | 3 | pass | pass |
| 63 | SHOP | 142.25 | $1.4B | 1.0% | 78.1% | 4.9% | no | 3 | pass | pass |
| 64 | ADI | 393.60 | $1.4B | 51.0% | 88.5% | 2.9% | no | 2 | pass | pass |
| 65 | UBER | 69.62 | $1.4B | -19.7% | 68.7% | 2.8% | no | 3 | pass | pass |
| 66 | CRDO | 210.97 | $1.4B | 51.6% | 68.3% | 6.4% | yes | 2 | pass | pass |
| 67 | GE | 327.09 | $1.3B | 18.6% | 84.1% | 2.6% | no | 3 | pass | pass |
| 68 | HD | 293.20 | $1.3B | -16.6% | 73.3% | 2.4% | no | 2 | pass | pass |
| 69 | ADBE | 235.47 | $1.3B | -22.6% | 64.7% | 4.0% | no | 3 | pass | pass |
| 70 | RKLB | 73.95 | $1.3B | 35.9% | 49.0% | 5.4% | no | 2 | fail | pass |
| 71 | BA | 198.07 | $1.3B | -1.4% | 77.9% | 2.8% | no | 3 | fail | pass |
| 72 | MCD | 236.50 | $1.3B | -10.7% | 70.6% | 2.1% | no | 2 | pass | pass |
| 73 | C | 134.28 | $1.3B | 34.2% | 91.2% | 2.5% | yes | 3 | fail | pass |
| 74 | ALAB | 364.62 | $1.3B | 41.4% | 73.0% | 6.3% | yes | 2 | pass | pass |
| 75 | TMO | 675.00 | $1.3B | 35.8% | 98.8% | 2.3% | no | 2 | pass | pass |
| 76 | BKNG | 163.95 | $1.3B | -4.9% | 73.5% | 4.0% | no | 3 | pass | pass |
| 77 | INTU | 275.79 | $1.3B | -49.7% | 39.6% | 4.9% | no | 3 | pass | pass |
| 78 | PG | 146.23 | $1.3B | -2.1% | 88.7% | 1.5% | no | 4 | pass | pass |
| 79 | ABBV | 264.34 | $1.2B | 23.2% | 98.1% | 2.0% | yes | 5 | pass | pass |
| 80 | ANET | 206.55 | $1.2B | 41.8% | 96.1% | 3.7% | yes | 4 | pass | pass |
| 81 | T | 25.38 | $1.2B | -4.3% | 88.3% | 2.3% | no | 2 | pass | pass |
| 82 | WFC | 82.97 | $1.2B | 3.5% | 86.3% | 2.4% | no | 2 | fail | pass |
| 83 | CRCL | 89.00 | $1.2B | -31.7% | 55.8% | 7.4% | no | 3 | pass | pass |
| 84 | AMGN | 414.61 | $1.1B | 62.1% | 92.7% | 2.6% | yes | 3 | pass | pass |
| 85 | ISRG | 405.18 | $1.1B | -16.6% | 67.1% | 2.8% | no | 3 | pass | pass |
| 86 | HPE | 62.94 | $1.1B | 129.3% | 95.9% | 5.6% | yes | 2 | pass | pass |
| 87 | AAOI | 101.40 | $1.1B | 326.2% | 43.4% | 8.1% | no | 2 | fail | pass |
| 88 | VLO | 387.18 | $1.1B | 108.1% | 92.4% | 4.0% | yes | 1 | pass | fail |
| 89 | TER | 398.38 | $1.1B | 173.0% | 81.7% | 5.1% | yes | 1 | pass | pass |
| 90 | PEP | 128.63 | $1.1B | 3.0% | 77.3% | 1.8% | no | 4 | pass | pass |
| 91 | NU | 13.59 | $1.1B | -5.2% | 71.6% | 3.7% | no | 2 | pass | pass |
| 92 | NKE | 35.75 | $1.1B | -44.6% | 48.0% | 2.7% | no | 5 | pass | pass |
| 93 | NET | 349.02 | $1.1B | 30.9% | 95.0% | 5.1% | yes | 1 | pass | pass |
| 94 | DDOG | 268.13 | $1.1B | 66.7% | 91.6% | 4.7% | yes | 2 | pass | pass |
| 95 | VZ | 47.08 | $1.0B | 24.2% | 91.1% | 2.2% | no | 4 | pass | pass |
| 96 | TJX | 130.06 | $1.0B | -1.4% | 76.7% | 2.2% | no | 2 | pass | pass |
| 97 | MS | 196.31 | $1.0B | 38.7% | 85.0% | 2.6% | no | 2 | fail | pass |
| 98 | RDDT | 149.84 | $1.0B | -34.1% | 56.9% | 5.1% | no | 2 | pass | pass |
| 99 | AAL | 13.87 | $1.0B | 15.9% | 73.8% | 3.4% | no | 2 | fail | pass |
| 100 | ACN | 176.11 | $1.0B | -22.3% | 61.4% | 4.1% | no | 2 | pass | pass |
| 101 | APH | 84.09 | $1.0B | 31.7% | 94.4% | 3.3% | yes | 4 | pass | pass |
| 102 | MPWR | 1367.43 | $1.0B | 44.5% | 79.9% | 4.2% | no | 2 | pass | pass |
| 103 | LIN | 469.89 | $997M | 4.5% | 86.0% | 1.6% | no | 1 | pass | pass |
| 104 | DHR | 224.49 | $988M | 16.7% | 92.9% | 2.6% | no | 2 | pass | pass |
| 105 | DIS | 106.15 | $978M | -1.9% | 92.0% | 2.1% | no | 3 | pass | pass |
| 106 | PFE | 28.67 | $977M | 25.7% | 98.2% | 1.8% | yes | 3 | pass | fail |
| 107 | SOFI | 16.58 | $970M | -33.8% | 50.7% | 4.2% | no | 3 | fail | pass |
| 108 | PATH | 12.46 | $970M | 36.0% | 62.8% | 6.5% | no | 3 | pass | pass |
| 109 | BSX | 43.92 | $968M | -50.9% | 41.6% | 3.4% | no | 2 | pass | pass |
| 110 | MPC | 393.52 | $966M | 90.7% | 91.3% | 3.7% | yes | 1 | pass | fail |
| 111 | ABT | 101.29 | $960M | -12.8% | 76.5% | 2.3% | no | 1 | pass | pass |
| 112 | RTX | 189.40 | $951M | 33.3% | 83.7% | 2.2% | no | 1 | pass | pass |
| 113 | MELI | 1752.61 | $943M | -22.3% | 68.8% | 3.3% | no | 3 | pass | pass |
| 114 | FCX | 72.31 | $936M | 112.0% | 90.1% | 3.7% | yes | 4 | pass | pass |
| 115 | AXP | 308.89 | $936M | -0.2% | 80.4% | 2.0% | no | 2 | pass | pass |
| 116 | NEE | 76.08 | $919M | 16.5% | 78.2% | 1.7% | no | 2 | pass | pass |
| 117 | PM | 190.48 | $906M | 23.4% | 91.7% | 2.3% | yes | 2 | pass | pass |
| 118 | SPOT | 510.01 | $902M | -22.4% | 68.9% | 3.7% | no | 5 | pass | pass |
| 119 | NXPI | 238.08 | $897M | -0.9% | 70.5% | 3.3% | no | 2 | pass | fail |
| 120 | NEM | 121.43 | $889M | 58.8% | 89.9% | 3.5% | no | 3 | pass | pass |
| 121 | BMNR | 27.56 | $886M | -52.0% | 42.0% | 6.1% | no | 2 | fail | pass |
| 122 | CLS | 365.44 | $879M | 27.8% | 77.1% | 5.3% | no | 2 | pass | pass |
| 123 | CIEN | 356.91 | $877M | 191.9% | 56.0% | 6.4% | no | 0 | pass | pass |
| 124 | GILD | 150.93 | $869M | 32.8% | 97.7% | 2.4% | yes | 3 | pass | pass |
| 125 | SCHW | 99.03 | $861M | 19.9% | 86.5% | 2.5% | no | 3 | pass | pass |
| 126 | NOK | 10.39 | $858M | 122.3% | 59.7% | 4.7% | no | 3 | pass | pass |
| 127 | TMUS | 165.43 | $858M | -23.5% | 69.7% | 3.0% | no | 3 | pass | pass |
| 128 | IONQ | 45.48 | $856M | -45.8% | 53.7% | 6.1% | no | 2 | fail | pass |
| 129 | COP | 127.30 | $855M | 40.9% | 89.9% | 2.7% | yes | 2 | pass | pass |
| 130 | ABNB | 157.47 | $848M | 52.8% | 81.4% | 3.6% | no | 4 | pass | pass |
| 131 | WBD | 30.86 | $844M | 45.1% | 99.8% | 1.5% | no | 2 | pass | fail |
| 132 | DASH | 193.36 | $843M | -9.1% | 67.7% | 3.8% | no | 3 | pass | pass |
| 133 | MCK | 866.64 | $837M | 18.2% | 86.9% | 2.5% | no | 2 | pass | pass |
| 134 | SYK | 272.36 | $815M | -10.3% | 69.9% | 3.1% | no | 2 | pass | pass |
| 135 | AZO | 2872.16 | $808M | -28.2% | 66.3% | 2.9% | no | 2 | pass | pass |
| 136 | CDNS | 326.13 | $807M | -6.2% | 78.3% | 3.3% | no | 2 | pass | pass |
| 137 | DE | 690.46 | $806M | 37.0% | 95.7% | 2.5% | yes | 3 | pass | fail |
| 138 | SPGI | 403.30 | $801M | -5.1% | 77.7% | 2.5% | no | 4 | pass | pass |
| 139 | UNP | 273.79 | $797M | 37.0% | 87.0% | 2.0% | no | 3 | pass | pass |
| 140 | FTNT | 173.46 | $782M | 88.4% | 95.6% | 3.9% | yes | 3 | pass | pass |
| 141 | SNPS | 425.76 | $772M | -12.4% | 78.9% | 3.6% | no | 3 | pass | pass |
| 142 | WDAY | 189.45 | $760M | -21.0% | 75.8% | 4.2% | no | 2 | pass | pass |
| 143 | COF | 200.20 | $758M | -1.7% | 78.0% | 2.5% | no | 4 | pass | pass |
| 144 | ON | 77.20 | $755M | 43.7% | 57.2% | 4.6% | no | 2 | pass | fail |
| 145 | HWM | 232.48 | $747M | 41.9% | 75.0% | 3.3% | no | 2 | pass | pass |
| 146 | CEG | 263.27 | $746M | -17.1% | 64.1% | 3.5% | no | 3 | pass | pass |
| 147 | CVS | 89.13 | $739M | 27.4% | 81.0% | 2.6% | no | 2 | pass | pass |
| 148 | PYPL | 55.04 | $734M | -8.1% | 70.3% | 3.1% | no | 2 | pass | pass |
| 149 | MCHP | 78.69 | $729M | 15.4% | 75.1% | 3.6% | no | 2 | pass | pass |
| 150 | SBUX | 94.86 | $725M | 32.0% | 86.3% | 2.4% | no | 3 | pass | pass |
| 151 | MDT | 88.64 | $724M | -1.3% | 85.6% | 2.3% | no | 3 | pass | pass |
| 152 | HON | 212.55 | $717M | 9.2% | 82.4% | 2.2% | no | 4 | pass | pass |
| 153 | CMCSA | 21.91 | $714M | -3.7% | 68.4% | 3.4% | no | 2 | pass | fail |
| 154 | ASTS | 61.81 | $711M | 9.9% | 46.2% | 6.6% | no | 2 | fail | pass |
| 155 | FIX | 1658.91 | $707M | 104.6% | 80.1% | 4.4% | no | 0 | pass | pass |
| 156 | PSX | 255.75 | $704M | 82.6% | 92.3% | 3.4% | yes | 4 | pass | fail |
| 157 | HL | 18.19 | $699M | 89.2% | 53.3% | 5.4% | no | 1 | pass | pass |
| 158 | BMY | 62.86 | $697M | 60.4% | 91.6% | 2.1% | no | 2 | pass | fail |
| 159 | AXTI | 78.94 | $691M | 1249.5% | 55.1% | 8.8% | no | 1 | fail | fail |
| 160 | CDE | 19.22 | $686M | 23.2% | 69.3% | 5.2% | no | 2 | pass | pass |
| 161 | WELL | 231.37 | $682M | 43.4% | 91.0% | 2.2% | no | 3 | pass | pass |
| 162 | F | 12.71 | $681M | 25.1% | 72.3% | 3.2% | no | 3 | pass | pass |
| 163 | SLB | 51.54 | $673M | 57.8% | 85.7% | 3.4% | no | 3 | pass | fail |
| 164 | BLK | 1086.31 | $670M | 5.7% | 90.9% | 2.2% | no | 3 | pass | pass |
| 165 | PWR | 649.13 | $668M | 53.2% | 82.3% | 3.4% | no | 2 | pass | pass |
| 166 | TEAM | 187.74 | $662M | 2.8% | 93.9% | 4.7% | no | 4 | pass | pass |
| 167 | LMT | 519.56 | $661M | 18.6% | 76.0% | 2.5% | no | 3 | pass | pass |
| 168 | TRV | 363.04 | $660M | 35.7% | 91.4% | 1.9% | no | 4 | pass | pass |
| 169 | MDB | 410.44 | $646M | 28.8% | 86.8% | 5.2% | no | 2 | pass | pass |
| 170 | LOW | 189.28 | $645M | -16.6% | 65.3% | 2.4% | no | 2 | pass | pass |
| 171 | RCL | 242.70 | $645M | -9.8% | 69.2% | 4.0% | no | 2 | pass | pass |
| 172 | VST | 138.46 | $644M | -30.4% | 64.1% | 3.2% | no | 3 | pass | pass |
| 173 | PH | 978.33 | $623M | 39.7% | 89.1% | 2.2% | no | 3 | pass | pass |
| 174 | ADP | 263.67 | $622M | -1.3% | 92.1% | 2.1% | no | 2 | pass | pass |
| 175 | FERG | 222.74 | $621M | 6.1% | 82.9% | 2.5% | no | 4 | pass | pass |
| 176 | SHW | 329.10 | $619M | 2.6% | 87.7% | 2.3% | no | 3 | pass | pass |
| 177 | CMI | 525.06 | $610M | 40.3% | 71.4% | 2.9% | no | 1 | pass | fail |
| 178 | TGT | 157.45 | $609M | 93.3% | 92.2% | 2.5% | yes | 3 | pass | fail |
| 179 | ILMN | 270.00 | $606M | 138.2% | 97.1% | 4.3% | yes | 2 | pass | fail |
| 180 | CB | 333.37 | $605M | 24.7% | 91.4% | 1.5% | no | 2 | pass | pass |
| 181 | MO | 68.82 | $603M | 11.5% | 90.7% | 1.9% | yes | 3 | pass | fail |
| 182 | PCG | 12.34 | $601M | 25.6% | 64.8% | 4.9% | no | 3 | fail | pass |
| 183 | HLT | 313.96 | $598M | 26.6% | 87.7% | 2.0% | no | 2 | pass | pass |
| 184 | VRTX | 526.19 | $597M | 45.3% | 93.9% | 2.4% | yes | 1 | pass | pass |
| 185 | CVNA | 65.06 | $597M | -1.3% | 66.8% | 4.7% | no | 3 | pass | pass |
| 186 | HCA | 435.61 | $592M | 3.0% | 78.7% | 3.1% | no | 2 | pass | pass |
| 187 | EQIX | 1008.08 | $588M | 39.6% | 90.1% | 2.8% | no | 4 | fail | pass |
| 188 | OKTA | 195.19 | $588M | 49.6% | 91.9% | 5.1% | yes | 2 | pass | pass |
| 189 | NVO | 38.80 | $578M | -16.9% | 62.5% | 3.2% | no | 2 | pass | pass |
| 190 | REGN | 788.04 | $577M | 41.9% | 91.7% | 2.5% | no | 3 | pass | pass |
| 191 | GM | 82.63 | $575M | 45.0% | 90.1% | 3.2% | no | 2 | pass | fail |
| 192 | CSX | 46.78 | $573M | 54.6% | 87.5% | 2.0% | no | 2 | pass | fail |
| 193 | MAR | 352.03 | $572M | 38.0% | 85.8% | 2.0% | no | 3 | pass | pass |
| 194 | PLD | 133.05 | $571M | 29.3% | 87.5% | 1.6% | no | 2 | pass | pass |
| 195 | AZN | 166.58 | $565M | 124.4% | 79.7% | 2.1% | no | 1 | pass | pass |
| 196 | BX | 118.42 | $564M | -16.3% | 69.1% | 3.3% | no | 3 | pass | pass |
| 197 | TTWO | 201.44 | $559M | -4.4% | 75.7% | 3.6% | no | 2 | pass | pass |
| 198 | ROST | 236.12 | $559M | 59.1% | 92.1% | 2.1% | no | 2 | pass | pass |
| 199 | CMG | 31.33 | $555M | -5.6% | 73.2% | 3.7% | no | 4 | pass | pass |
| 200 | MMM | 169.55 | $553M | 18.6% | 92.1% | 2.0% | no | 3 | pass | pass |

## Risk-Target Coverage

- Baseline per-position cap: 9.60% of account equity
- SMA risk target: 0.60% of account equity
- Risk sizing binds at ATR14/close >= 3.12%
- Conservatively cap-clipped candidates: 97 of 200
- A universe refresh does not silently change the risk target.

## Rejections

| Reason | Count | Meaning | Examples |
|---|---:|---|---|
| `price` | 2020 | Latest completed-session close is below the minimum. | AAME, AARD, ABAT, ABEO, ABEV, ABLV, ABOS, ABSI, ABTC, ABTS |
| `dollar_volume` | 1755 | 50-session average dollar volume is below the minimum. | AACI, AAMI, AAPG, AAUC, AB, ABCB, ABM, ABXL, ACAD, ACEL |
| `insufficient_or_bad_bars` | 640 | Not enough clean daily bars for durable review. | AAC, AACO, AACOW, AACP, AACPR, AADX, ACAA, ACAAW, ACCL, ACGC |
| `solvency` | 4 | Known cash runway is below the durable-company minimum. | LITE, MSTR, CIFR, MARA |
| `nonpreferred_share_class` | 1 | Use GOOG for Alphabet exposure, never GOOGL. | GOOGL |

## Limitations

- This is a current-universe snapshot, not a survivorship-free historical test.
- Alternative ranking modes are diagnostics; liquidity is the proposed v3 default.
- With fundamentals disabled, market cap and solvency are not enforced.
- Unresolved entry orders must be reconciled separately before promotion.
