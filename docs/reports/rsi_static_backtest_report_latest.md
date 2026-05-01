# RSI Backtest Report - 2026-05-01T13:28:47+00:00

- Rule version: `rsi_backtest_report_v1`
- Strategy: `RSIReversion(period=14, oversold=30, overbought=70)`
- Promoted symbols: IBM, ABBV, CRDO, WFC, CVX, ANET, IONQ, CAT, OXY, BE, XOM, RTX, AXP, BKNG, BAC, GS, CEG, LMT, WMT, LLY, PG, LIN, AMGN, TMUS
- Comparison symbols: ARM, SPG
- Alpaca feed: `sip`
- Data window: 2021-05-02 to 2026-05-01
- Data end timestamp: 2026-05-01T12:28:44+00:00
- Initial cash per symbol: $100,000
- Costs: slippage=5 bps, commission=$0.00

## Summary

| Group | Symbol | Trades | Return | CAGR | Sharpe | Sortino | MaxDD | MaxDD Days | Win % | PF | Expectancy | Final Equity | Buy/Hold | Events | Hit % | Avg10d | Stops |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| promoted | ABBV | 9 | 130.2% | 18.2% | 1.43 | 2.16 | -12.6% | 231 | 100.0% | inf | $14,461.54 | $230,153.89 | 122.3% | 16 | 37.5% | 2.2% | 3 |
| promoted | AMGN | 6 | 49.3% | 8.4% | 0.72 | 1.13 | -14.9% | 320 | 83.3% | 42.96 | $8,221.93 | $149,331.60 | 65.1% | 18 | 16.7% | -0.0% | 6 |
| promoted | ANET | 4 | 175.0% | 22.5% | 1.27 | 2.05 | -26.4% | 196 | 100.0% | inf | $43,755.09 | $275,020.38 | 778.0% | 6 | 0.0% | 5.0% | 1 |
| promoted | AXP | 8 | 117.8% | 16.9% | 1.02 | 1.61 | -28.2% | 311 | 100.0% | inf | $14,725.97 | $217,807.77 | 121.7% | 13 | 30.8% | 3.7% | 3 |
| promoted | BAC | 8 | 111.1% | 16.1% | 1.09 | 1.63 | -31.4% | 591 | 87.5% | 17.25 | $13,881.97 | $211,055.78 | 48.8% | 16 | 12.5% | 1.3% | 5 |
| promoted | BE | 6 | 295.4% | 31.7% | 0.92 | 1.52 | -61.8% | 648 | 83.3% | 21.35 | $49,229.66 | $395,377.95 | 1041.2% | 15 | 13.3% | 4.8% | 4 |
| promoted | BKNG | 7 | 119.1% | 17.0% | 0.95 | 1.44 | -29.5% | 589 | 85.7% | 3.68 | $17,015.02 | $219,105.17 | 75.7% | 15 | 53.3% | 5.9% | 2 |
| promoted | CAT | 7 | 126.6% | 17.8% | 1.18 | 1.90 | -21.8% | 148 | 100.0% | inf | $18,085.74 | $226,600.21 | 326.6% | 16 | 31.2% | 4.9% | 3 |
| promoted | CEG | 4 | 118.1% | 20.2% | 0.99 | 1.64 | -25.2% | 536 | 100.0% | inf | $29,530.82 | $218,123.27 | 511.4% | 7 | 14.3% | 5.0% | 2 |
| promoted | CRDO | 4 | 254.2% | 34.6% | 1.26 | 2.30 | -34.8% | 1077 | 75.0% | 124.47 | $63,562.36 | $354,249.44 | 1393.6% | 7 | 28.6% | 2.4% | 2 |
| promoted | CVX | 6 | 114.8% | 16.5% | 1.37 | 2.18 | -14.4% | 79 | 100.0% | inf | $19,125.02 | $214,750.14 | 125.6% | 10 | 30.0% | 2.4% | 1 |
| promoted | GS | 6 | 88.5% | 13.5% | 0.98 | 1.54 | -24.4% | 468 | 100.0% | inf | $14,754.07 | $188,524.44 | 197.5% | 12 | 25.0% | 5.0% | 0 |
| promoted | IBM | 9 | 139.1% | 19.1% | 1.68 | 2.71 | -12.3% | 336 | 100.0% | inf | $15,451.67 | $239,065.04 | 94.8% | 16 | 18.8% | 0.9% | 6 |
| promoted | IONQ | 6 | 335.7% | 34.3% | 0.93 | 1.63 | -74.2% | 1195 | 66.7% | 6.46 | $55,954.86 | $435,729.16 | 317.4% | 12 | 50.0% | 14.1% | 2 |
| promoted | LIN | 5 | 51.0% | 8.6% | 0.72 | 1.10 | -21.0% | 426 | 100.0% | inf | $10,203.01 | $151,015.03 | 86.5% | 14 | 42.9% | 1.9% | 3 |
| promoted | LLY | 6 | 84.6% | 13.1% | 0.84 | 1.26 | -30.3% | 230 | 100.0% | inf | $14,099.52 | $184,597.12 | 428.4% | 10 | 40.0% | 5.0% | 3 |
| promoted | LMT | 8 | 75.0% | 11.9% | 0.98 | 1.38 | -26.5% | 301 | 75.0% | 3.98 | $9,372.80 | $174,982.42 | 53.9% | 21 | 38.1% | 2.2% | 5 |
| promoted | OXY | 5 | 128.5% | 18.0% | 1.20 | 2.08 | -22.4% | 800 | 80.0% | 13.33 | $25,694.58 | $228,472.91 | 147.1% | 10 | 50.0% | 6.4% | 2 |
| promoted | PG | 7 | 45.5% | 7.8% | 0.77 | 1.10 | -20.1% | 216 | 85.7% | 222.96 | $6,505.93 | $145,541.54 | 23.7% | 16 | 43.8% | 2.5% | 3 |
| promoted | RTX | 7 | 89.2% | 13.6% | 1.28 | 2.11 | -20.6% | 359 | 71.4% | 7.87 | $12,739.27 | $189,174.92 | 134.0% | 9 | 44.4% | 1.7% | 1 |
| promoted | TMUS | 6 | 43.6% | 7.5% | 0.69 | 1.14 | -24.6% | 661 | 66.7% | 4.04 | $7,262.23 | $143,573.40 | 54.7% | 17 | 23.5% | 1.5% | 5 |
| promoted | WFC | 7 | 164.4% | 21.5% | 1.24 | 1.88 | -29.5% | 483 | 100.0% | inf | $23,479.36 | $264,355.52 | 103.2% | 14 | 28.6% | 3.7% | 2 |
| promoted | WMT | 4 | 72.2% | 11.5% | 1.11 | 1.74 | -8.9% | 89 | 100.0% | inf | $18,059.48 | $172,237.92 | 197.4% | 7 | 14.3% | 1.6% | 2 |
| promoted | XOM | 5 | 108.1% | 15.8% | 1.30 | 2.01 | -16.1% | 343 | 100.0% | inf | $21,624.21 | $208,121.04 | 217.7% | 7 | 42.9% | 5.3% | 1 |
| comparison | ARM | 4 | 312.1% | 71.5% | 2.18 | 4.72 | -26.3% | 307 | 100.0% | inf | $78,022.38 | $412,089.51 | 230.7% | 6 | 33.3% | 4.9% | 2 |
| comparison | SPG | 6 | 107.3% | 15.7% | 1.29 | 1.96 | -17.6% | 500 | 100.0% | inf | $17,888.22 | $207,329.34 | 115.8% | 11 | 27.3% | 2.9% | 2 |

## Promoted Pool Aggregate

- Symbols tested: 24
- Total trades across the basket: 150
- Trades per month across the basket: 2.50
- Average per-symbol strategy return: 126.5%
- Median per-symbol strategy return: 116.3%
- Average per-symbol Sharpe: 1.08
- Median per-symbol Sharpe: 1.06
- Average per-symbol max drawdown: -26.3%
- Median per-symbol max drawdown: -24.5%
- Average capped profit factor: 23.85
- Average per-symbol win rate: 90.0%

## Interpretation

- This is a per-symbol backtest, not a combined portfolio simulation.
- The strategy is the current bot RSI logic: entry below RSI 30, exit above RSI 70, next-open fills.
- ATR stop counts are contextual event diagnostics; the vectorbt run does not execute broker OTO stop legs.
- Buy/Hold is shown only as context. RSI is a tactical strategy, so it can lag buy-and-hold in strong trends.
- Large max drawdown means the current RSI exit/stop behavior still needs paper validation before activation.
- Equity charts are optional local artifacts and are not required to verify the published markdown metrics.