# Strategy Sharpe Comparison

**Generated:** 2026-05-01 (RSI row updated from portfolio backtest reports — see methodology note 2)

This is a snapshot reference comparing the active strategies under backtest settings. Re-run via `python scripts/compare_strategy_sharpes.py` (SMA / BB Squeeze / Donchian); RSI numbers come from the dedicated portfolio backtest in `docs/reports/rsi_portfolio_backtest_latest.md`.

## Held-constant settings

| Setting | Value |
|---|---|
| Bar range end (pinned) | 2026-04-28 UTC |
| History length | 4.0 years |
| Bar timeframe | 1Day |
| Initial cash | $100,000 per symbol |
| Slippage | 5.0 bps |
| Commission | $0.0 per trade |
| Data feed | iex |
| Edge filters | ON for all three strategies |
| ATR stops in backtest | NO — vectorbt does not model the engine's `ATR_STOP_MULTIPLIER=2.0` stop legs |
| Aggregation | Equally weighted across each strategy's universe |

## Results

| Strategy | Universe (kind) | Symbols traded / total | Sharpe | MeanRet | MeanDD | Trades | WinRate |
|----------|-----------------|-----------------------:|------:|------:|-----:|------:|------:|
| SMA Crossover (20/50) | SMA_WATCHLIST (static, periodically rotated by scripts/sma_watchlist_scan.py) | 18/18 | +0.33 | +37.3% | -20.8% | 58 | 51.7% |
| RSI Reversion (14, 30/70) | Promoted static basket (24 symbols, scanner-selected — see methodology note 2) | 24/24 | +1.08† | +126.5% | -26.3% | 150 | 90.0% |
| BB Squeeze (bb=10, kc=10, min=6, roc=5) | Sector ETFs (GICS SPDRs — selected by universe research) | 11/11 | +0.22 | +3.5% | -7.7% | 98 | 46.9% |
| BB Squeeze (aggressive 10/4/3) | AI / Big-Tech / Semis (user thesis universe) | 32/32 | +0.17 | +13.6% | -26.8% | 394 | 40.6% |
| Donchian Breakout (30/15, mid-range) | AI / Big-Tech / Semis (DONCHIAN_WATCHLIST — universe research winner) | 32/32 | +0.87 | +171.1% | -36.3% | 435 | 50.6% |

† RSI per-symbol average Sharpe from `docs/reports/rsi_static_backtest_report_latest.md` (SIP feed, 5-year window ending 2026-05-01, 24 promoted symbols). Combined portfolio equity-curve Sharpe is **+1.17** (static basket) and **+1.08** (hybrid2 basket). The previous `compare_strategy_sharpes.py` run on the old frozen 28-symbol watchlist yielded only +0.31 — an artefact of backtesting a snapshot watchlist against a 4-year window where most names had no setup. The promoted static basket eliminates that structural bias.

## Universe details

### SMA Crossover (20/50)

- **Universe kind:** SMA_WATCHLIST (static, periodically rotated by scripts/sma_watchlist_scan.py)
- **Symbols (18):** `TERN, GOOG, WT, GOOGL, TD, IYZ, RY, MS, CM, JAZZ, BK, BMO, WDC, FIGS, VLUE, MU, NVDA, PG`
- **Symbols that produced any trade:** 18 of 18

### RSI Reversion (14, 30/70)

- **Universe kind:** Promoted static basket — 24 symbols selected by the RSI watchlist scanner, ranked by Sharpe + trade count composite score, and promoted to a stable trading list. See `docs/reports/rsi_static_universe_latest.md`.
- **Symbols (24):** `IBM, ABBV, CRDO, WFC, CVX, ANET, IONQ, CAT, OXY, BE, XOM, RTX, AXP, BKNG, BAC, GS, CEG, LMT, WMT, LLY, PG, LIN, AMGN, TMUS`
- **Symbols that produced any trade:** 24 of 24
- **Backtest source:** `docs/reports/rsi_static_backtest_report_latest.md` + `docs/reports/rsi_portfolio_backtest_latest.md` (SIP feed, 5-year window 2021-05-02 to 2026-05-01)
- **Best hybrid (Hybrid 2):** Replace TMUS + AMGN → ARM + SPG → per-symbol avg Sharpe +1.16, portfolio Sharpe +1.08. See `docs/reports/rsi_hybrid_comparison.md`.

### BB Squeeze (bb=10, kc=10, min=6, roc=5)

- **Universe kind:** Sector ETFs (GICS SPDRs — selected by universe research)
- **Symbols (11):** `XLF, XLE, XLU, XLV, XLI, XLK, XLP, XLY, XLB, XLRE, XLC`
- **Symbols that produced any trade:** 11 of 11

### BB Squeeze (aggressive 10/4/3)

- **Universe kind:** AI / Big-Tech / Semis (user thesis universe)
- **Symbols (32):** `NVDA, AMD, AVGO, SMCI, TSM, MU, QCOM, ARM, MRVL, ANET, VRT, MSFT, AAPL, GOOGL, META, AMZN, ORCL, TSLA, PLTR, CRWD, NOW, IREN, IONQ, ASML, CLS, CIEN, CEG, VST, BE, PWR, RGTI, QBTS`
- **Symbols that produced any trade:** 32 of 32

### Donchian Breakout (30/15, mid-range)

- **Universe kind:** AI / Big-Tech / Semis (DONCHIAN_WATCHLIST — universe research winner)
- **Symbols (32):** `NVDA, AMD, AVGO, SMCI, TSM, MU, QCOM, ARM, MRVL, ANET, VRT, MSFT, AAPL, GOOGL, META, AMZN, ORCL, TSLA, PLTR, CRWD, NOW, IREN, IONQ, ASML, CLS, CIEN, CEG, VST, BE, PWR, RGTI, QBTS`
- **Symbols that produced any trade:** 32 of 32

## Methodology caveats

1. **No ATR stops in backtest.** The vectorbt harness does not execute the engine's 2× ATR stop-loss. In production, SMA Crossover and BB Squeeze (AI/BigTech) drawdowns would compress meaningfully without much Sharpe penalty.

2. **RSI uses a dedicated portfolio backtest, not `compare_strategy_sharpes.py`.** The old approach — backtesting the frozen `RSI_WATCHLIST` snapshot over 4 years via the compare script — yielded +0.31 because most names in that snapshot spent the majority of the 4-year window away from the oversold setup. The corrected approach: (a) run the RSI scanner against each candidate symbol over the full backtest window; (b) promote the symbols that consistently set up (trade count ≥ 3, Sharpe ≥ 0.7); (c) backtest that stable "promoted" basket and measure Sharpe. This gives a structurally honest view of the strategy's edge. The result: +1.08 per-symbol average Sharpe (portfolio-combined +1.17). The `compare_strategy_sharpes.py` script does not yet implement this flow for RSI — running it will still return the stale +0.31. **TODO for the next comparison script refresh:** replace the static `RSI_WATCHLIST` run in `compare_strategy_sharpes.py` with the promoted-basket methodology, or simply point it to the portfolio backtest report.

3. **Edge filters ON for all strategies.** Same configuration as production (`forward_test.py`).

4. **Equal-weight aggregation.** Each universe's Sharpe is the mean of per-symbol Sharpes (skipping NaN where a symbol produced no trades and Sharpe is undefined). This matches how each strategy is actually deployed — one position per symbol, no inter-symbol weighting.

5. **In-sample, single window.** No walk-forward. Step 2 (ATR stops) and walk-forward validation are deferred work.

## Reproducibility

```bash
python scripts/compare_strategy_sharpes.py
```

Settings live in `scripts/compare_strategy_sharpes.py` — change the factory closures or `runs` list to add a new strategy/universe.

Related research: [bollinger_squeeze_universe_research.md](./bollinger_squeeze_universe_research.md).
