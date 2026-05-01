# RSI Portfolio Backtest - 2026-05-01T14:15:39+00:00

- This report uses one shared-capital equity curve per basket.
- Entries split available cash equally across same-day new positions.
- Max simultaneous positions: `5`
- Alpaca feed: `sip`
- Data window: 2021-05-02 to 2026-05-01
- Data end timestamp: 2026-05-01T13:15:35+00:00

## Basket Comparison

| Basket | Symbols | Trades | Return | CAGR | Sharpe | Sortino | MaxDD | MaxDD Days | Win % | PF | Avg Util | Avg Open Pos | Final Equity |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| current | 28 | 18 | 193.4% | 24.1% | 0.86 | 1.18 | -39.6% | 77 | 88.9% | 25.10 | 90.8% | 1.25 | $293,389.32 |
| static | 24 | 21 | 520.0% | 44.1% | 1.17 | 1.50 | -53.9% | 37 | 90.5% | 14.00 | 83.9% | 1.28 | $619,951.66 |
| hybrid2 | 24 | 18 | 450.2% | 40.7% | 1.08 | 1.36 | -53.9% | 37 | 94.4% | 113.97 | 88.4% | 1.20 | $550,156.92 |

## Notes

- Unlike the per-symbol reports, Sharpe here is a true combined-portfolio Sharpe.
- This still does not model limit-order queueing or ATR stop legs; exits come from the exact RSI strategy.