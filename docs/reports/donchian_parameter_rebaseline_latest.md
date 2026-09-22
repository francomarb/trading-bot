# Donchian Parameter Rebaseline Result

**Generated:** 2026-09-21

> Fixed-current-cohort temporal study; not a survivorship-free universe backtest.

## Held-out annual folds

Values: return | Sharpe | max drawdown | trades | win rate | mean R | capacity skips.

| Year | 20/10 | 30/10 | 30/15 control | 55/20 | Expanding-history selection |
|---:|---|---|---|---|---|
| 2021 | +6.0%<br>+1.83<br>-1.9%<br>87<br>51.7%<br>+0.57R<br>2879 | +5.3%<br>+1.43<br>-2.2%<br>95<br>48.4%<br>+0.41R<br>2474 | +3.6%<br>+0.99<br>-2.9%<br>55<br>52.7%<br>+0.71R<br>2631 | +4.5%<br>+1.14<br>-2.7%<br>46<br>58.7%<br>+0.91R<br>2184 | 30/10 |
| 2022 | -2.4%<br>-1.66<br>-2.7%<br>34<br>14.7%<br>-0.41R<br>367 | -2.1%<br>-1.59<br>-2.5%<br>27<br>14.8%<br>-0.46R<br>306 | -2.4%<br>-1.92<br>-2.5%<br>30<br>10.0%<br>-0.45R<br>294 | -1.2%<br>-0.85<br>-2.5%<br>21<br>14.3%<br>+0.07R<br>243 | 30/10 |
| 2023 | +3.0%<br>+1.01<br>-3.0%<br>86<br>33.7%<br>+0.08R<br>2836 | +1.8%<br>+0.79<br>-2.6%<br>80<br>35.0%<br>+0.25R<br>2485 | +1.6%<br>+0.68<br>-2.6%<br>61<br>36.1%<br>+0.43R<br>2516 | +2.8%<br>+1.09<br>-2.6%<br>42<br>40.5%<br>+0.49R<br>2118 | 30/10 |
| 2024 | +3.8%<br>+1.31<br>-2.3%<br>82<br>48.8%<br>+0.45R<br>3189 | +4.4%<br>+1.18<br>-2.8%<br>95<br>47.4%<br>+0.66R<br>2710 | +1.1%<br>+0.43<br>-2.4%<br>79<br>44.3%<br>+0.51R<br>2774 | +2.5%<br>+0.86<br>-3.2%<br>61<br>37.7%<br>+0.56R<br>2525 | 30/10 |
| 2025 | +3.3%<br>+1.10<br>-2.8%<br>87<br>39.1%<br>+0.87R<br>2723 | +8.1%<br>+1.61<br>-3.3%<br>65<br>43.1%<br>+1.32R<br>2472 | +11.3%<br>+1.40<br>-7.0%<br>50<br>30.0%<br>+1.34R<br>2566 | +6.2%<br>+1.45<br>-2.6%<br>45<br>35.6%<br>+0.52R<br>2289 | 30/10 |

## Stitched held-out results

| Variant | Return | Sharpe | Max drawdown | Trades | Mean R |
|---|---:|---:|---:|---:|---:|
| 20/10 | +14.3% | +0.97 | -4.8% | 376 | +0.41R |
| 30/10 | +18.4% | +1.01 | -4.1% | 362 | +0.54R |
| 30/15 | +15.7% | +0.71 | -7.0% | 275 | +0.58R |
| 55/20 | +15.5% | +0.93 | -3.2% | 215 | +0.57R |
| Expanding selection | +18.4% | +1.01 | -4.1% | 362 | +0.54R |

## Held-out exit-reason mix

| Variant | Stop gap | Intrabar stop | Signal | Fold-end close |
|---|---:|---:|---:|---:|
| 20/10 | 6.6% | 25.8% | 60.9% | 6.6% |
| 30/10 | 6.1% | 22.4% | 65.2% | 6.4% |
| 30/15 | 7.6% | 32.7% | 48.4% | 11.3% |
| 55/20 | 11.6% | 29.8% | 44.7% | 14.0% |

## Pre-registered verdict

Every challenger is evaluated against the same conjunctive rule; the report does not choose one challenger after viewing the results.

| Challenger | Years won | Sharpe edge | DD difference | Mean R | Remove-best-year | Remove-best-symbol | Verdict |
|---|---:|---:|---:|---:|---|---|---|
| 20/10 | 3/5 | +0.26 | +2.2pp | +0.41R | 2024: +10.1% vs +14.5% (FAIL) | ex NVDA: +10.6% vs +14.3% (FAIL) | C1 F, C2 P, C3 P, C4 P, C5 F |
| 30/10 | 4/5 | +0.30 | +2.9pp | +0.54R | 2024: +13.4% vs +14.5% (FAIL) | ex SNDK: +14.2% vs +8.2% (PASS) | C1 P, C2 P, C3 P, C4 P, C5 F |
| 55/20 | 4/5 | +0.22 | +3.8pp | +0.57R | 2024: +12.7% vs +14.5% (FAIL) | ex STX: +10.6% vs +18.5% (FAIL) | C1 P, C2 P, C3 P, C4 P, C5 F |

**Decision: retain 30/15.** No challenger cleared every mandatory criterion; do not salvage a variant by changing the rule after seeing the result.

## Selection audit

- 2021: selected 30/10 from prior-history Sharpe (20/10=+1.04, 30/10=+1.24, 30/15=+0.97, 55/20=+0.65).
- 2022: selected 30/10 from prior-history Sharpe (20/10=+1.10, 30/10=+1.23, 30/15=+1.00, 55/20=+0.75).
- 2023: selected 30/10 from prior-history Sharpe (20/10=+0.76, 30/10=+0.98, 30/15=+0.71, 55/20=+0.52).
- 2024: selected 30/10 from prior-history Sharpe (20/10=+0.79, 30/10=+0.97, 30/15=+0.67, 55/20=+0.60).
- 2025: selected 30/10 from prior-history Sharpe (20/10=+0.72, 30/10=+0.93, 30/15=+0.65, 55/20=+0.56).

## Partial 2026 shadow (non-decision)

- 20/10: return +11.6%, Sharpe +2.06, max DD -3.1%, 71 trades, win rate 38.0%, mean +0.85R, 1723 capacity skips
- 30/10: return +11.6%, Sharpe +2.03, max DD -4.2%, 80 trades, win rate 26.2%, mean +0.55R, 1527 capacity skips
- 30/15: return +13.1%, Sharpe +1.85, max DD -5.5%, 53 trades, win rate 35.8%, mean +0.71R, 1624 capacity skips
- 55/20: return +7.6%, Sharpe +1.55, max DD -3.9%, 42 trades, win rate 47.6%, mean +1.10R, 1363 capacity skips

## Coverage and limitations

- Frozen ranked pool: 100 symbols; lifecycle-only SPCX excluded.
- Production parity includes the allocator's pre-sizing $100 minimum remaining-sleeve-capacity check and conservative STOP_LIMIT quantity from the worst permitted limit down to the pre-fill reference-anchored stop. Earlier drafts omitted the floor and then divided risk by only the post-fill 2 ATR protection distance; both corrections materially changed headline metrics, so these estimates support the no-change decision rather than precise expected returns.
- Coverage is listing/provider dependent; no pre-listing history is fabricated.
- Earnings blackout is unmodeled; current-cohort survivorship and selection bias remain.
- See `docs/donchian_parameter_rebaseline.md` for the frozen contract and decision rule.
