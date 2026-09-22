# Donchian Deployment Validation Result

**Generated:** 2026-09-21

> Historical fixed-cohort sensitivity only. Forward paper evidence has higher authority.

The 2021–2025 years were inspected by `11.73`; they are fixed comparison folds here, not a newly untouched holdout. Production remains close-based 30/15.

## Common-period portfolio results (2017–2025)

Every cell enforces the pre-registered 4R heat cap, including pending DAY-entry reservations. Production currently runs this cap in observation-only mode (`STRATEGY_HEAT_CAP_ENFORCED=False`), so the classic results are conditional on a separately approved enforcement change.

| Universe | Variant | Return | Sharpe | Max DD | Trades | Entry clusters | Mean R | Capacity skips | Heat skips |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| ai_bigtech_32 | current close 30/15 | +18.6% | +0.66 | -4.1% | 343 | 113 | +0.56R | 4310 | 0 |
| ai_bigtech_32 | classic high/low 20/10 | +23.4% | +0.91 | -2.7% | 315 | 121 | +0.58R | 20583 | 8224 |
| ai_bigtech_32 | classic high/low 55/20 | +22.4% | +0.93 | -3.8% | 180 | 92 | +1.01R | 21083 | 7580 |
| durable_100 | current close 30/15 | +26.3% | +0.73 | -7.3% | 483 | 101 | +0.51R | 19142 | 0 |
| durable_100 | classic high/low 20/10 | +23.8% | +1.22 | -2.3% | 357 | 126 | +0.61R | 98353 | 15436 |
| durable_100 | classic high/low 55/20 | +31.5% | +1.03 | -4.8% | 197 | 99 | +1.30R | 97890 | 15256 |

## Fixed annual comparison

| Universe | Year | Current close 30/15 | Classic high/low 20/10 | Classic high/low 55/20 |
|---|---:|---:|---:|---:|
| ai_bigtech_32 | 2017 | +2.9% / +1.14 / 42 trades | +0.1% / +0.05 / 40 trades | +2.6% / +1.22 / 21 trades |
| ai_bigtech_32 | 2018 | +1.0% / +0.39 / 35 trades | +1.0% / +0.53 / 36 trades | +1.4% / +0.59 / 20 trades |
| ai_bigtech_32 | 2019 | +0.7% / +0.40 / 36 trades | +2.4% / +1.46 / 38 trades | +2.1% / +1.29 / 25 trades |
| ai_bigtech_32 | 2020 | +1.6% / +0.64 / 46 trades | +1.1% / +0.45 / 37 trades | +3.3% / +1.13 / 19 trades |
| ai_bigtech_32 | 2021 | +4.8% / +1.57 / 39 trades | +3.1% / +1.32 / 42 trades | +3.3% / +1.23 / 29 trades |
| ai_bigtech_32 | 2022 | -3.4% / -2.43 / 21 trades | -1.7% / -1.69 / 13 trades | -1.9% / -3.20 / 10 trades |
| ai_bigtech_32 | 2023 | +6.8% / +1.94 / 41 trades | +5.8% / +1.78 / 39 trades | +6.1% / +1.81 / 21 trades |
| ai_bigtech_32 | 2024 | +9.6% / +2.44 / 27 trades | +9.0% / +1.88 / 45 trades | +6.7% / +1.29 / 21 trades |
| ai_bigtech_32 | 2025 | -1.2% / -0.24 / 52 trades | +1.7% / +0.65 / 36 trades | +0.3% / +0.12 / 29 trades |
| durable_100 | 2017 | +3.1% / +1.76 / 60 trades | +3.3% / +1.68 / 34 trades | +2.5% / +1.33 / 19 trades |
| durable_100 | 2018 | +4.9% / +1.44 / 50 trades | -0.8% / -0.54 / 41 trades | -0.9% / -0.65 / 27 trades |
| durable_100 | 2019 | +2.7% / +1.34 / 45 trades | +1.9% / +1.33 / 42 trades | +1.9% / +1.24 / 26 trades |
| durable_100 | 2020 | +4.2% / +1.01 / 46 trades | +3.9% / +1.40 / 43 trades | +5.0% / +1.46 / 23 trades |
| durable_100 | 2021 | +3.6% / +0.99 / 55 trades | +4.6% / +2.35 / 41 trades | +3.5% / +1.55 / 28 trades |
| durable_100 | 2022 | -2.4% / -1.92 / 30 trades | -1.4% / -1.37 / 23 trades | -2.2% / -3.15 / 15 trades |
| durable_100 | 2023 | +1.6% / +0.68 / 61 trades | +1.5% / +0.75 / 49 trades | +3.2% / +1.41 / 29 trades |
| durable_100 | 2024 | +1.1% / +0.43 / 79 trades | +5.3% / +2.28 / 51 trades | +3.9% / +1.54 / 31 trades |
| durable_100 | 2025 | +11.3% / +1.40 / 50 trades | +10.3% / +1.74 / 42 trades | +11.7% / +1.71 / 24 trades |

## Contributor concentration

The no-top-symbol column is a full rerun after removing that variant's largest realized contributor; it is not simple subtraction.

| Universe | Variant | Top contributor | Share of positive P&L | Return without top contributor |
|---|---|---|---:|---:|
| ai_bigtech_32 | current close 30/15 | NVDA | 17.9% | +22.3% |
| ai_bigtech_32 | classic high/low 20/10 | NVDA | 25.4% | +24.6% |
| ai_bigtech_32 | classic high/low 55/20 | NVDA | 35.2% | +18.1% |
| durable_100 | current close 30/15 | SNDK | 32.0% | +16.3% |
| durable_100 | classic high/low 20/10 | NVDA | 26.3% | +25.2% |
| durable_100 | classic high/low 55/20 | SNDK | 34.0% | +20.4% |

## Exit-reason mix

| Universe | Variant | Protective gap | Protective intraday | Close signal | Channel gap | Channel intraday | Fold end |
|---|---|---:|---:|---:|---:|---:|---:|
| ai_bigtech_32 | current close 30/15 | 7.3% | 28.3% | 63.6% | 0.0% | 0.0% | 0.9% |
| ai_bigtech_32 | classic high/low 20/10 | 7.3% | 16.8% | 0.0% | 13.0% | 62.2% | 0.6% |
| ai_bigtech_32 | classic high/low 55/20 | 6.1% | 36.1% | 0.0% | 13.9% | 43.9% | 0.0% |
| durable_100 | current close 30/15 | 8.5% | 32.9% | 56.9% | 0.0% | 0.0% | 1.7% |
| durable_100 | classic high/low 20/10 | 4.5% | 19.9% | 0.0% | 13.7% | 60.8% | 1.1% |
| durable_100 | classic high/low 55/20 | 5.6% | 32.0% | 0.0% | 14.2% | 46.7% | 1.5% |

## Interpretation boundary

- `ai_bigtech_32` preserves the original operator-supplied membership and list order; `durable_100` preserves the promoted liquidity ranking. Capacity and heat therefore reflect each deployment's actual deterministic ordering.
- The universes are present-day frozen cohorts, not point-in-time membership histories. Survivorship and selection bias prevent an unbiased absolute-return claim.
- Earnings blackout remains omitted because trustworthy point-in-time history is unavailable.
- Daily OHLC cannot resolve every intraday path when several levels trade in one session; the simulator applies the documented deterministic ordering.
- STOP_LIMIT quantity uses the production worst-limit-to-reference-stop distance; post-fill protection then re-anchors to fill minus 2 ATR.
- The original pre-registration (`997e247`) named deterministic liquidity order and fill-anchored stops. The result commit (`becc25b`) amended those terms for parity: preserve each frozen universe's actual order and size from the worst limit to the reference-anchored stop. The amendments were applied uniformly, but were not part of the original frozen wording.
- Production does not currently enforce the 4R heat cap. Because the cap rejected thousands of classic candidates while never binding current close-based 30/15 here, any classic paper experiment must separately authorize enforcement or these modeled classic results do not describe its behavior.
- Allocator stretch and cross-sleeve competition are omitted. The 12% Donchian baseline is a conservative isolated deployment boundary.
- These results can motivate a separately reviewed paper cohort. They cannot change the current 30/15 configuration, satisfy the 25-exit requirement, or authorize live graduation.
