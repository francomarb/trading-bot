# Strategy Sharpe Comparison

**Generated:** 2026-05-01T12:43:02.101718+00:00

This is a snapshot reference comparing the three strategies (`SMACrossover`, `RSIReversion`, `BollingerSqueeze`) under identical backtest settings. Re-run via `python scripts/compare_strategy_sharpes.py`.

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
| RSI Reversion (14, 30/70) | RSI_WATCHLIST (static snapshot of dynamic scanner output — see caveat below) | 15/28 | +0.31 | +3.9% | -10.7% | 22 | 63.6% |
| BB Squeeze (bb=10, kc=10, min=6, roc=5) | Sector ETFs (GICS SPDRs — selected by universe research) | 11/11 | +0.22 | +3.5% | -7.7% | 98 | 46.9% |
| BB Squeeze (aggressive 10/4/3) | AI / Big-Tech / Semis (user thesis universe) | 32/32 | +0.17 | +13.6% | -26.8% | 394 | 40.6% |
| Donchian Breakout (30/15, mid-range) | AI / Big-Tech / Semis (DONCHIAN_WATCHLIST — universe research winner) | 32/32 | +0.87 | +171.1% | -36.3% | 435 | 50.6% |

## Universe details

### SMA Crossover (20/50)

- **Universe kind:** SMA_WATCHLIST (static, periodically rotated by scripts/sma_watchlist_scan.py)
- **Symbols (18):** `TERN, GOOG, WT, GOOGL, TD, IYZ, RY, MS, CM, JAZZ, BK, BMO, WDC, FIGS, VLUE, MU, NVDA, PG`
- **Symbols that produced any trade:** 18 of 18

### RSI Reversion (14, 30/70)

- **Universe kind:** RSI_WATCHLIST (static snapshot of dynamic scanner output — see caveat below)
- **Symbols (28):** `ALLY, CDNS, KBE, SN, BA, TFC, HON, TMUS, JNJ, CCK, ABNB, PG, SPG, MA, LMT, MCD, AAPL, ANET, CAT, CIEN, MCO, AMZN, EQIX, RTX, META, HD, SOFI, ARM`
- **Symbols that produced any trade:** 15 of 28

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

2. **RSI's low trade count is structural, not a bug.** `RSI_WATCHLIST` is a *static snapshot* of a weekly scanner (`scripts/rsi_watchlist_scan.py`) that selects names *currently close to triggering* the RSI-oversold setup. Backtesting that frozen list over 4 years means most names spent most of the period not setting up — only the ones that happened to oversold-revert during the window produced trades. **The 4-year Sharpe understates the production strategy** because production rotates the watchlist. A more honest RSI Sharpe would require a walk-forward backtest that re-runs the scanner each week — out of scope for this snapshot.

3. **Edge filters ON for all strategies.** Same configuration as production (`forward_test.py`).

4. **Equal-weight aggregation.** Each universe's Sharpe is the mean of per-symbol Sharpes (skipping NaN where a symbol produced no trades and Sharpe is undefined). This matches how each strategy is actually deployed — one position per symbol, no inter-symbol weighting.

5. **In-sample, single window.** No walk-forward. Step 2 (ATR stops) and walk-forward validation are deferred work.

## Reproducibility

```bash
python scripts/compare_strategy_sharpes.py
```

Settings live in `scripts/compare_strategy_sharpes.py` — change the factory closures or `runs` list to add a new strategy/universe.

Related research: [bollinger_squeeze_universe_research.md](./bollinger_squeeze_universe_research.md).
