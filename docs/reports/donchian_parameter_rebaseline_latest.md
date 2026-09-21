# Donchian Parameter Rebaseline Result

**Generated:** 2026-09-21

> Fixed-current-cohort temporal study; not a survivorship-free universe backtest.

## Held-out annual folds

Values: return | Sharpe | max drawdown | trades | win rate | mean R | capacity skips.

| Year | 20/10 | 30/10 | 30/15 control | 55/20 | Expanding-history selection |
|---:|---|---|---|---|---|
| 2021 | +7.0%<br>+1.98<br>-1.4%<br>94<br>48.9%<br>+0.44R<br>2694 | +5.0%<br>+1.44<br>-1.9%<br>102<br>42.2%<br>+0.21R<br>2332 | +6.4%<br>+1.93<br>-1.8%<br>47<br>59.6%<br>+1.04R<br>2465 | +3.9%<br>+1.25<br>-1.8%<br>40<br>60.0%<br>+0.98R<br>2061 | 30/15 |
| 2022 | -2.6%<br>-1.60<br>-3.3%<br>34<br>14.7%<br>-0.35R<br>335 | -2.4%<br>-1.64<br>-2.7%<br>33<br>18.2%<br>-0.45R<br>263 | -2.6%<br>-1.94<br>-2.7%<br>32<br>9.4%<br>-0.55R<br>274 | -1.5%<br>-1.04<br>-2.7%<br>26<br>15.4%<br>-0.10R<br>205 | 30/15 |
| 2023 | -0.3%<br>-0.10<br>-2.9%<br>90<br>36.7%<br>+0.16R<br>2553 | +0.7%<br>+0.30<br>-2.6%<br>90<br>32.2%<br>+0.16R<br>2232 | +1.1%<br>+0.43<br>-2.7%<br>64<br>35.9%<br>+0.22R<br>2318 | +1.9%<br>+0.75<br>-2.3%<br>48<br>35.4%<br>+0.13R<br>1975 | 30/15 |
| 2024 | +0.9%<br>+0.34<br>-2.7%<br>94<br>46.8%<br>+0.50R<br>2843 | +4.7%<br>+1.36<br>-2.5%<br>91<br>45.1%<br>+0.75R<br>2455 | +1.8%<br>+0.62<br>-3.1%<br>76<br>43.4%<br>+0.62R<br>2517 | +6.7%<br>+1.69<br>-3.2%<br>58<br>37.9%<br>+0.76R<br>2352 | 20/10 |
| 2025 | +2.9%<br>+0.85<br>-2.8%<br>82<br>41.5%<br>+0.93R<br>2440 | +5.1%<br>+1.28<br>-2.7%<br>62<br>37.1%<br>+1.24R<br>2289 | +5.4%<br>+1.22<br>-3.4%<br>55<br>36.4%<br>+1.21R<br>2313 | +5.5%<br>+1.31<br>-2.8%<br>41<br>36.6%<br>+0.49R<br>2109 | 20/10 |

## Stitched held-out results

| Variant | Return | Sharpe | Max drawdown | Trades | Mean R |
|---|---:|---:|---:|---:|---:|
| 20/10 | +7.8% | +0.54 | -4.9% | 394 | +0.42R |
| 30/10 | +13.5% | +0.84 | -5.0% | 378 | +0.44R |
| 30/15 | +12.5% | +0.78 | -4.1% | 274 | +0.58R |
| 55/20 | +17.4% | +1.02 | -3.3% | 213 | +0.50R |
| Expanding selection | +8.8% | +0.62 | -4.7% | 319 | +0.53R |

## Held-out exit-reason mix

| Variant | Stop gap | Intrabar stop | Signal | Fold-end close |
|---|---:|---:|---:|---:|
| 20/10 | 6.6% | 22.6% | 64.7% | 6.1% |
| 30/10 | 5.8% | 26.2% | 61.6% | 6.3% |
| 30/15 | 7.7% | 31.8% | 50.0% | 10.6% |
| 55/20 | 8.9% | 36.6% | 43.7% | 10.8% |

## Pre-registered verdict

The strongest challenger, 55/20, beat 30/15 on return in **4 of 5** held-out years; the required bar was 4 of 5.

Its stitched Sharpe advantage was **+0.24** (required at least +0.15), maximum-drawdown difference was **+0.8 percentage points** (must not be worse by more than 3), and mean R was **+0.50R**.

### Concentration sensitivities

- Remove-best-year: excluded 2024. The remaining stitched return was +10.0% for 55/20 versus +10.5% for 30/15 — **FAIL**.
- Remove-best-symbol: excluded PLTR, the largest realized 55/20 P&L contributor. The rerun return was +12.1% for 55/20 versus +14.1% for 30/15 — **FAIL**.

Criteria: C1 PASS, C2 PASS, C3 PASS, C4 PASS, C5 FAIL.

**Decision: retain 30/15.** At least one mandatory pre-registered criterion failed; do not salvage 55/20 by changing the rule after seeing the result.

## Selection audit

- 2021: selected 30/15 from prior-history Sharpe (20/10=+0.77, 30/10=+0.90, 30/15=+1.04, 55/20=+0.49).
- 2022: selected 30/15 from prior-history Sharpe (20/10=+0.92, 30/10=+0.90, 30/15=+1.01, 55/20=+0.66).
- 2023: selected 30/15 from prior-history Sharpe (20/10=+0.66, 30/10=+0.59, 30/15=+0.68, 55/20=+0.35).
- 2024: selected 20/10 from prior-history Sharpe (20/10=+0.71, 30/10=+0.59, 30/15=+0.65, 55/20=+0.43).
- 2025: selected 20/10 from prior-history Sharpe (20/10=+0.68, 30/10=+0.59, 30/15=+0.67, 55/20=+0.47).

## Partial 2026 shadow (non-decision)

- 20/10: return +12.0%, Sharpe +1.94, max DD -3.8%, 69 trades, win rate 26.1%, mean +0.48R, 1534 capacity skips
- 30/10: return +11.7%, Sharpe +1.87, max DD -4.4%, 70 trades, win rate 27.1%, mean +0.55R, 1356 capacity skips
- 30/15: return +13.3%, Sharpe +1.60, max DD -6.2%, 64 trades, win rate 29.7%, mean +0.26R, 1376 capacity skips
- 55/20: return +9.4%, Sharpe +1.58, max DD -5.1%, 42 trades, win rate 45.2%, mean +1.30R, 1173 capacity skips

## Coverage and limitations

- Frozen ranked pool: 100 symbols; lifecycle-only SPCX excluded.
- Production parity includes the allocator's pre-sizing $100 minimum remaining-sleeve-capacity check. The first published draft omitted it; review showed that residual-capacity handling materially changes headline metrics, so these estimates support the no-change decision rather than precise expected returns.
- Coverage is listing/provider dependent; no pre-listing history is fabricated.
- Earnings blackout is unmodeled; current-cohort survivorship and selection bias remain.
- See `docs/donchian_parameter_rebaseline.md` for the frozen contract and decision rule.
