# RSI Hybrid Comparison - 2026-05-01

- Purpose: compare the new static RSI basket against hybrid variants that swap in the strongest names from the current promoted `RSI_WATCHLIST`
- Source static report: `logs/rsi_static_universe_latest.md`
- Current promoted watchlist source: `config/settings.py`
- Comparison window: `2021-05-02` to `2026-05-01`
- Data feed: `sip`
- Method: exact RSI strategy backtest per symbol, summarized as a basket-level per-symbol aggregate

## Source Watchlists

### Static Builder Final Basket

`IBM, ABBV, CRDO, WFC, CVX, ANET, IONQ, CAT, OXY, BE, XOM, RTX, AXP, BKNG, BAC, GS, CEG, LMT, WMT, LLY, PG, LIN, AMGN, TMUS`

### Current Promoted RSI Watchlist

`ALLY, CDNS, KBE, SN, BA, TFC, HON, TMUS, JNJ, CCK, ABNB, PG, SPG, MA, LMT, MCD, AAPL, ANET, CAT, CIEN, MCO, AMZN, EQIX, RTX, META, HD, SOFI, ARM`

## Ranked Swap Inputs

### Weakest `WATCH` names in the static basket

| Symbol | Score | Trades | Return | Sharpe | MaxDD | PF |
|---|---:|---:|---:|---:|---:|---:|
| `TMUS` | 34.7 | 6 | 44.8% | 0.70 | -24.6% | 4.15 |
| `AMGN` | 40.8 | 6 | 49.3% | 0.72 | -14.9% | 42.96 |
| `WMT` | 51.7 | 4 | 72.4% | 1.11 | -8.8% | inf |
| `CEG` | 52.8 | 4 | 112.9% | 0.97 | -25.2% | inf |
| `GS` | 57.5 | 6 | 88.5% | 0.98 | -24.4% | inf |
| `BAC` | 60.0 | 8 | 111.1% | 1.09 | -31.4% | 17.26 |

### Strongest current-watchlist names not already in the static basket

| Symbol | Score | Verdict | Trades | Return | Sharpe | MaxDD | PF |
|---|---:|---|---:|---:|---:|---:|---:|
| `ARM` | 102.5 | `PROMOTE` | 4 | 317.9% | 2.20 | -26.4% | inf |
| `SPG` | 67.5 | `PROMOTE` | 6 | 106.3% | 1.28 | -17.6% | inf |
| `ALLY` | 57.6 | `PROMOTE` | 6 | 131.4% | 0.86 | -46.4% | 7.61 |
| `CCK` | 48.0 | `PROMOTE` | 8 | 89.6% | 0.77 | -41.3% | 6.92 |
| `MA` | 47.6 | `PROMOTE` | 5 | 61.7% | 0.79 | -25.0% | 8.86 |
| `CDNS` | 45.6 | `PROMOTE` | 5 | 88.4% | 0.72 | -28.5% | inf |

## Basket Comparison

| Basket | Symbols | Trades | Trades/Month | Avg Return | Median Return | Avg Sharpe | Median Sharpe | Avg MaxDD | Avg PF | Avg Win % |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Static base | 24 | 149 | 3.04 | 122.9% | 113.8% | 1.07 | 1.06 | -26.3% | 29.90 | 89.9% |
| Hybrid 2 | 24 | 147 | 3.00 | 136.6% | 116.3% | 1.16 | 1.13 | -26.5% | 28.61 | 92.0% |
| Hybrid 4 | 24 | 153 | 3.12 | 138.1% | 118.5% | 1.14 | 1.12 | -28.8% | 28.54 | 90.3% |
| Hybrid 6 | 24 | 149 | 3.04 | 136.0% | 118.5% | 1.12 | 1.09 | -28.7% | 28.20 | 90.0% |
| Hybrid 8 | 24 | 148 | 3.02 | 123.4% | 111.4% | 1.09 | 1.03 | -26.3% | 32.76 | 89.9% |
| Current RSI watchlist | 28 | 157 | 3.20 | 64.0% | 44.4% | 0.70 | 0.64 | -33.9% | 22.33 | 81.3% |

## Preferred Hybrid

### Hybrid 2

- Remove: `TMUS`, `AMGN`
- Add: `ARM`, `SPG`

Final basket:

`IBM, ABBV, CRDO, WFC, CVX, ANET, IONQ, CAT, OXY, BE, XOM, RTX, AXP, BKNG, BAC, GS, CEG, LMT, WMT, LLY, PG, LIN, ARM, SPG`

Why this version:

- Best average Sharpe of the tested hybrids: `1.16`
- Higher average return than the static base: `136.6%` vs `122.9%`
- Very similar drawdown to the static base: `-26.5%` vs `-26.3%`
- Highest win rate of the tested hybrids: `92.0%`

## Notes

- These are per-symbol aggregate summaries, not a synchronized portfolio simulation.
- The static base still has some high-return `WATCH` names that remain interesting despite weaker event-hit diagnostics.
- `Hybrid 4` produced the highest average return, but `Hybrid 2` was the cleaner risk-adjusted choice.
