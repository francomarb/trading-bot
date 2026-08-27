# Leveraged Trend Strategy

**Status:** research implementation complete; not wired to paper or live trading.

## Thesis

Use an unleveraged benchmark ETF to decide whether leverage is permitted, then
hold its benchmark-aligned 3x ETF only during a confirmed long-term uptrend.
The signal asset and execution asset are deliberately different instruments.

| Signal asset | Trading asset | Shared benchmark |
|---|---|---|
| SPY | SPXL | S&P 500 Index |
| QQQ | TQQQ | Nasdaq-100 Index |
| XLK | TECL | Technology Select Sector Index |
| SOXX | SOXL | NYSE Semiconductor Index |

SOXX replaces SMH for the semiconductor pair. SMH tracks the different MVIS
US Listed Semiconductor 25 Index and therefore introduces avoidable basis risk
when used to govern SOXL.

## Baseline Contract

- Data: Alpaca delayed SIP daily bars with `adjustment="all"`.
- Indicator: SMA of the signal asset's adjusted daily close; default 200 sessions.
- Initial state: OUT (cash). The strategy never seeds a position merely because
  the first evaluable bar is above its SMA.
- Entry: `entry_days` consecutive completed closes strictly above the SMA.
- Exit: `exit_days` consecutive completed closes strictly below the SMA.
- Equality: a close exactly on the SMA resets both streaks and changes no state.
- Execution: the next common session's open in the 3x trading asset.
- Inactive asset: cash.
- Stops, SMA-distance bands, volatility gates, and tax logic: excluded.

The strategy implementation requires an explicit `signal_close` column. It
fails rather than falling back to the leveraged ETF's own close. This is a
load-bearing boundary: the execution OHLC and signal close must never be
silently conflated.

## Research Implementation

- Signal state machine: `strategies/leveraged_trend.py`
- Pair alignment and metrics: `backtest/leveraged_trend.py`
- Reproducible CLI: `scripts/backtest_leveraged_trend.py`
- Full result grid: `docs/reports/leveraged_trend_grid.csv`
- Summary and stress windows: `docs/reports/leveraged_trend_backtest.md`

Reproduce from the project root:

```bash
./venv/bin/python scripts/backtest_leveraged_trend.py
```

The default grid fixes SMA at 200 and evaluates entry confirmation
`[1, 2, 3, 5, 7, 10]` against exit confirmation `[1, 2, 3, 5]`, with 5 bps
slippage and no commissions.

## SIP Coverage And Findings (2026-08-26)

All eight assets share Alpaca SIP coverage from 2016-01-04 through 2026-08-25:
2,676 common sessions. The first possible informed trade occurs only after the
200-session warmup and entry confirmation.

The 5-above / 2-below candidate is the strongest conservative common setting:

- It is the best Calmar configuration within SPY→SPXL and SOXX→SOXL.
- It ranks second across pairs by median Calmar, behind the much more reactive
  2-above / 1-below setting.
- Across the four standalone pairs it produced median CAGR 39.0%, median
  Sharpe 1.08, 39 total trades, and median time in market 74.0%.
- Pair maximum drawdowns remained severe: SPXL −42.1%, TQQQ −62.1%, TECL
  −65.3%, and SOXL −72.4%.

The result supports the return thesis but does not establish an acceptable
portfolio risk policy. A 200-day signal is slow by construction. When a crash
begins far above the SMA, it can retain the leveraged position through most of
the decline. In the continuous 5/2 equity paths, the 2020 crash/rebound window
reached −37.4% drawdown in SPXL, −62.1% in TQQQ, −65.3% in TECL, and −68.7%
in SOXL. These are observed baseline results, not hypothetical objections.

The four pairs are also highly correlated. Their standalone statistics cannot
be combined as if they were four independent sleeves.

## Activation Blockers

Research completion does not authorize paper activation. The engine currently
assumes one fetched symbol supplies both the strategy signal and execution
bars; this strategy needs a durable two-asset data contract. In addition:

1. `utils.asset_filters.is_stock_like` intentionally rejects leveraged funds.
2. `RiskManager` sizes equity entries from risk to a protective stop, while the
   baseline intentionally has no stop. Leveraged notional/effective-exposure
   sizing needs its own explicit policy rather than a fabricated stop price.
3. The allocator has no portfolio-level control for the overlapping SPY/QQQ/
   technology/semiconductor factor exposure.
4. Restart restoration must retain the signal/trading pair identity while the
   broker position remains keyed to the traded fund.
5. A temporal validation and combined-portfolio simulation must precede any
   sleeve allocation decision.

Until those contracts are designed and tested, this module remains a
reproducible research strategy only.

## Deferred Questions

- Whether 5/2 remains preferable under temporal validation rather than the
  full-window sensitivity table.
- Whether all four pairs should ever run together.
- Effective-exposure and aggregate heat limits.
- BIL/SGOV versus cash when inactive.
- Catastrophe overlay. The signal-only baseline is the required control; an
  overlay survives only if separately measured evidence justifies it.
- Tax-aware lot handling at the portfolio/account layer.
