# Donchian Parameter Rebaseline Result

**Generated:** 2026-09-21

> Fixed-current-cohort temporal study; not a survivorship-free universe backtest.

## Held-out annual folds

Values: return | Sharpe | max drawdown | trades | win rate | mean R | capacity skips.

| Year | 20/10 | 30/10 | 30/15 control | 55/20 | Expanding-history selection |
|---:|---|---|---|---|---|
| 2021 | +4.3%<br>+1.38<br>-2.6%<br>100<br>48.0%<br>+0.46R<br>2640 | +4.9%<br>+1.44<br>-1.5%<br>109<br>44.0%<br>+0.26R<br>2263 | +5.1%<br>+1.54<br>-2.4%<br>58<br>53.4%<br>+0.73R<br>2418 | +3.9%<br>+1.24<br>-1.9%<br>54<br>51.9%<br>+0.60R<br>2001 | 30/15 |
| 2022 | -2.6%<br>-1.60<br>-3.3%<br>38<br>15.8%<br>-0.35R<br>317 | -2.4%<br>-1.67<br>-2.7%<br>38<br>15.8%<br>-0.53R<br>250 | -2.6%<br>-1.96<br>-2.7%<br>37<br>8.1%<br>-0.60R<br>257 | -1.4%<br>-0.99<br>-2.7%<br>27<br>14.8%<br>-0.15R<br>203 | 30/15 |
| 2023 | +0.4%<br>+0.18<br>-2.8%<br>110<br>41.8%<br>+0.08R<br>2443 | +1.2%<br>+0.50<br>-2.5%<br>100<br>39.0%<br>+0.24R<br>2146 | +1.5%<br>+0.58<br>-2.7%<br>67<br>43.3%<br>+0.53R<br>2245 | +2.1%<br>+0.82<br>-2.2%<br>52<br>38.5%<br>+0.25R<br>1917 | 30/15 |
| 2024 | +2.4%<br>+0.82<br>-2.8%<br>98<br>43.9%<br>+0.48R<br>2838 | +3.5%<br>+1.24<br>-2.4%<br>93<br>49.5%<br>+0.85R<br>2457 | +1.5%<br>+0.58<br>-2.7%<br>81<br>38.3%<br>+0.36R<br>2536 | +6.0%<br>+1.77<br>-2.6%<br>65<br>38.5%<br>+0.78R<br>2295 | 30/15 |
| 2025 | +4.1%<br>+1.16<br>-2.7%<br>90<br>42.2%<br>+0.87R<br>2405 | +5.1%<br>+1.29<br>-2.6%<br>65<br>38.5%<br>+1.11R<br>2284 | +6.9%<br>+1.60<br>-3.0%<br>57<br>38.6%<br>+1.32R<br>2272 | +5.5%<br>+1.30<br>-2.8%<br>46<br>37.0%<br>+0.42R<br>2075 | 55/20 |

## Stitched held-out results

| Variant | Return | Sharpe | Max drawdown | Trades | Mean R |
|---|---:|---:|---:|---:|---:|
| 20/10 | +8.7% | +0.61 | -6.0% | 436 | +0.38R |
| 30/10 | +12.7% | +0.83 | -4.3% | 405 | +0.45R |
| 30/15 | +12.7% | +0.82 | -5.0% | 300 | +0.53R |
| 55/20 | +16.9% | +1.03 | -3.3% | 244 | +0.46R |
| Expanding selection | +11.3% | +0.74 | -5.0% | 289 | +0.36R |

## Held-out exit-reason mix

| Variant | Stop gap | Intrabar stop | Signal | Fold-end close |
|---|---:|---:|---:|---:|
| 20/10 | 6.2% | 26.1% | 61.2% | 6.4% |
| 30/10 | 5.4% | 25.2% | 62.7% | 6.7% |
| 30/15 | 9.0% | 32.7% | 48.3% | 10.0% |
| 55/20 | 8.2% | 36.5% | 44.3% | 11.1% |

## Pre-registered verdict

The strongest challenger, 55/20, beat 30/15 on return in **3 of 5** held-out years; the required bar was 4 of 5.

Its stitched Sharpe advantage was **+0.21** (required at least +0.15), maximum-drawdown difference was **+1.7 percentage points** (must not be worse by more than 3), and mean R was **+0.46R**.

**Decision: retain 30/15.** Criterion 1 failed, so no challenger can clear the conjunctive rule. Remove-best-year and remove-best-symbol promotion sensitivities cannot reverse that failed mandatory condition and were not used to search for a salvage variant.

## Selection audit

- 2021: selected 30/15 from prior-history Sharpe (20/10=+0.87, 30/10=+0.90, 30/15=+1.06, 55/20=+0.83).
- 2022: selected 30/15 from prior-history Sharpe (20/10=+0.95, 30/10=+0.95, 30/15=+1.16, 55/20=+0.93).
- 2023: selected 30/15 from prior-history Sharpe (20/10=+0.59, 30/10=+0.63, 30/15=+0.81, 55/20=+0.63).
- 2024: selected 30/15 from prior-history Sharpe (20/10=+0.69, 30/10=+0.60, 30/15=+0.82, 55/20=+0.68).
- 2025: selected 55/20 from prior-history Sharpe (20/10=+0.68, 30/10=+0.60, 30/15=+0.80, 55/20=+0.82).

## Partial 2026 shadow (non-decision)

- 20/10: return +12.4%, Sharpe +2.00, max DD -3.6%, 70 trades, win rate 32.9%, mean +0.55R, 1519 capacity skips
- 30/10: return +11.2%, Sharpe +1.79, max DD -5.0%, 75 trades, win rate 26.7%, mean +0.53R, 1348 capacity skips
- 30/15: return +14.0%, Sharpe +1.85, max DD -5.7%, 55 trades, win rate 36.4%, mean +0.71R, 1398 capacity skips
- 55/20: return +9.3%, Sharpe +1.57, max DD -5.2%, 45 trades, win rate 42.2%, mean +1.14R, 1172 capacity skips

## Coverage and limitations

- Frozen ranked pool: 100 symbols; lifecycle-only SPCX excluded.
- Coverage is listing/provider dependent; no pre-listing history is fabricated.
- Earnings blackout is unmodeled; current-cohort survivorship and selection bias remain.
- See `docs/donchian_parameter_rebaseline.md` for the frozen contract and decision rule.
