# Leveraged trend confirmation study

This is the historical research result that preceded paper activation. The
strategy is now active in paper development; this report alone is not live
approval.

## Contract

- Signal: adjusted daily close of the unleveraged benchmark ETF versus its SMA.
- Entry: configurable consecutive closes strictly above the SMA.
- Exit: configurable consecutive closes strictly below the SMA.
- Execution: next common session open of the 3x ETF.
- Inactive state: cash.
- Stops and tax rules: excluded from the baseline.
- Data: Alpaca SIP, adjustment=all; slippage=5 bps.

## Coverage

| signal_asset | trading_asset | start | end | bars |
| --- | --- | --- | --- | --- |
| QQQ | TQQQ | 2016-01-04 05:00:00+00:00 | 2026-08-25 04:00:00+00:00 | 2676 |
| SOXX | SOXL | 2016-01-04 05:00:00+00:00 | 2026-08-25 04:00:00+00:00 | 2676 |
| SPY | SPXL | 2016-01-04 05:00:00+00:00 | 2026-08-25 04:00:00+00:00 | 2676 |
| XLK | TECL | 2016-01-04 05:00:00+00:00 | 2026-08-25 04:00:00+00:00 | 2676 |

The first executable signal occurs only after the SMA warmup and the full entry confirmation. Coverage dates above include warmup bars.

## Buy-and-hold context

Benchmarks enter at the first open after the 200-session warmup, so they do not receive an extra pre-strategy year.

| pair | asset_kind | symbol | cagr | sharpe | max_drawdown | final_equity |
| --- | --- | --- | --- | --- | --- | --- |
| SPY->SPXL | unleveraged | SPY | 15.6% | 1.09 | -33.8% | $415,528 |
| SPY->SPXL | leveraged | SPXL | 30.2% | 0.93 | -76.9% | $1,343,023 |
| QQQ->TQQQ | unleveraged | QQQ | 20.8% | 1.14 | -35.0% | $644,614 |
| QQQ->TQQQ | leveraged | TQQQ | 40.2% | 1.02 | -81.7% | $2,797,088 |
| XLK->TECL | unleveraged | XLK | 24.2% | 0.82 | -58.0% | $844,095 |
| XLK->TECL | leveraged | TECL | 46.6% | 1.07 | -78.0% | $4,331,625 |
| SOXX->SOXL | unleveraged | SOXX | 32.0% | 1.17 | -45.8% | $1,535,115 |
| SOXX->SOXL | leveraged | SOXL | 46.3% | 1.07 | -90.5% | $4,245,765 |

## Cross-pair parameter summary

Ranking uses median Calmar only as a navigation aid. The pairs are highly correlated and are not treated as independent evidence.

| entry_days | exit_days | pair_count | median_cagr | worst_cagr | median_sharpe | worst_max_drawdown | median_calmar | total_trades | median_time_in_market |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 2 | 1 | 4 | 44.0% | 22.7% | 1.11 | -83.4% | 0.69 | 67 | 75.0% |
| 5 | 2 | 4 | 39.0% | 26.8% | 1.08 | -72.4% | 0.63 | 39 | 74.0% |
| 3 | 1 | 4 | 40.6% | 24.6% | 1.06 | -81.5% | 0.62 | 58 | 74.2% |
| 3 | 2 | 4 | 41.3% | 27.0% | 1.09 | -77.0% | 0.61 | 47 | 75.2% |
| 2 | 2 | 4 | 42.7% | 25.3% | 1.09 | -81.8% | 0.60 | 55 | 75.9% |
| 5 | 1 | 4 | 38.0% | 25.1% | 1.05 | -80.9% | 0.59 | 49 | 72.9% |
| 7 | 2 | 4 | 37.0% | 25.6% | 1.05 | -75.5% | 0.58 | 37 | 73.0% |
| 7 | 1 | 4 | 36.7% | 24.0% | 1.05 | -79.8% | 0.57 | 45 | 72.1% |
| 10 | 1 | 4 | 37.0% | 20.7% | 1.05 | -79.3% | 0.57 | 40 | 70.9% |
| 10 | 2 | 4 | 36.5% | 22.3% | 1.02 | -75.0% | 0.55 | 36 | 71.7% |

## Best five configurations within each pair

| signal_asset | trading_asset | entry_days | exit_days | cagr | sharpe | max_drawdown | calmar | trade_count | time_in_market |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| QQQ | TQQQ | 3 | 1 | 42.8% | 1.20 | -48.1% | 0.89 | 10 | 76.1% |
| QQQ | TQQQ | 2 | 1 | 44.2% | 1.22 | -49.8% | 0.89 | 13 | 76.6% |
| QQQ | TQQQ | 5 | 1 | 39.1% | 1.13 | -48.2% | 0.81 | 10 | 75.4% |
| QQQ | TQQQ | 10 | 1 | 38.7% | 1.14 | -48.1% | 0.80 | 9 | 73.5% |
| QQQ | TQQQ | 7 | 1 | 37.3% | 1.10 | -49.3% | 0.76 | 10 | 74.6% |
| SOXX | SOXL | 5 | 2 | 47.1% | 1.07 | -72.4% | 0.65 | 13 | 72.6% |
| SOXX | SOXL | 5 | 3 | 44.5% | 1.04 | -75.2% | 0.59 | 11 | 73.5% |
| SOXX | SOXL | 3 | 2 | 44.9% | 1.05 | -77.0% | 0.58 | 16 | 73.8% |
| SOXX | SOXL | 7 | 2 | 43.5% | 1.04 | -75.5% | 0.58 | 11 | 71.2% |
| SOXX | SOXL | 10 | 1 | 44.7% | 1.05 | -79.3% | 0.56 | 11 | 68.7% |
| SPY | SPXL | 5 | 2 | 26.8% | 1.06 | -42.1% | 0.64 | 11 | 75.3% |
| SPY | SPXL | 7 | 2 | 25.6% | 1.03 | -44.4% | 0.58 | 11 | 74.5% |
| SPY | SPXL | 3 | 2 | 27.0% | 1.06 | -49.5% | 0.55 | 15 | 76.5% |
| SPY | SPXL | 5 | 1 | 25.1% | 1.01 | -46.4% | 0.54 | 11 | 74.6% |
| SPY | SPXL | 2 | 2 | 25.3% | 1.01 | -50.1% | 0.51 | 18 | 77.3% |
| XLK | TECL | 2 | 1 | 44.5% | 1.18 | -52.1% | 0.85 | 13 | 72.5% |
| XLK | TECL | 3 | 1 | 38.6% | 1.09 | -53.3% | 0.72 | 12 | 72.0% |
| XLK | TECL | 2 | 2 | 43.2% | 1.14 | -65.3% | 0.66 | 10 | 73.2% |
| XLK | TECL | 3 | 2 | 41.9% | 1.12 | -65.3% | 0.64 | 9 | 72.7% |
| XLK | TECL | 5 | 1 | 36.9% | 1.06 | -58.0% | 0.64 | 11 | 71.2% |

## Stress windows for the 5/2 candidate

These are slices of the continuous full-period equity path; strategy state is not reset at the start of each window.

| signal_asset | trading_asset | period | start | end | return | max_drawdown | time_in_market |
| --- | --- | --- | --- | --- | --- | --- | --- |
| SPY | SPXL | 2018 Q4 selloff | 2018-09-04 | 2019-01-31 | -16.4% | -19.9% | 35.0% |
| SPY | SPXL | 2020 COVID crash/rebound | 2020-02-03 | 2020-06-30 | -22.9% | -37.4% | 38.5% |
| SPY | SPXL | 2022 bear market | 2022-01-03 | 2022-12-30 | -31.9% | -31.9% | 14.7% |
| QQQ | TQQQ | 2018 Q4 selloff | 2018-09-04 | 2019-01-31 | -32.3% | -32.4% | 36.9% |
| QQQ | TQQQ | 2020 COVID crash/rebound | 2020-02-03 | 2020-06-30 | -18.5% | -62.1% | 77.9% |
| QQQ | TQQQ | 2022 bear market | 2022-01-03 | 2022-12-30 | -37.1% | -37.1% | 5.6% |
| XLK | TECL | 2018 Q4 selloff | 2018-09-04 | 2019-01-31 | -30.0% | -32.4% | 36.9% |
| XLK | TECL | 2020 COVID crash/rebound | 2020-02-03 | 2020-06-30 | -21.5% | -65.3% | 77.9% |
| XLK | TECL | 2022 bear market | 2022-01-03 | 2022-12-30 | -49.1% | -49.1% | 21.9% |
| SOXX | SOXL | 2018 Q4 selloff | 2018-09-04 | 2019-01-31 | -23.2% | -23.2% | 24.3% |
| SOXX | SOXL | 2020 COVID crash/rebound | 2020-02-03 | 2020-06-30 | -21.4% | -68.7% | 75.0% |
| SOXX | SOXL | 2022 bear market | 2022-01-03 | 2022-12-30 | -54.1% | -54.4% | 10.8% |

## Interpretation guardrails

- Prefer a stable neighborhood of confirmation values over the top row.
- This table is not a combined-portfolio simulation and does not resolve correlated sleeve sizing.
- A final parameter choice requires period-by-period and untouched holdout validation.
