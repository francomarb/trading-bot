# Donchian Parameter Rebaseline

**Roadmap item:** `11.73`

**Status:** PRE-REGISTERED — design frozen before the comparison is run

**Pre-registration date:** 2026-09-21

## Question

The original 30/15 Donchian setting was chosen in sample on a manually themed
32-name AI / big-tech basket. The active strategy now trades a mechanically
selected 100-name durable-liquidity pool. This study asks whether 30/15 remains
a robust channel pair on that materially different cohort.

It compares four standard, interpretable variants only:

- 20-day entry / 10-day exit;
- 30-day entry / 10-day exit;
- 30-day entry / 15-day exit (current control); and
- 55-day entry / 20-day exit.

No additional window may be added after results are viewed. This work is an
audit, not authorization to change the paper configuration.

## Claim Boundary

The promoted pool is a 2026 current-universe snapshot. Historical membership,
delisted companies, and point-in-time fundamentals are unavailable. Therefore
this is a **fixed-cohort temporal robustness study**, not a survivorship-free
universe walk-forward test. It may compare channel behavior on the same names;
it may not establish the historical profitability of the selector or provide
an unbiased absolute return estimate.

The ranked first 100 members of `DONCHIAN_WATCHLIST` are frozen for the study.
Temporary lifecycle-preservation names appended after the ranked pool are
excluded. Symbols participate only after enough real history exists; missing
pre-listing bars are never backfilled.

## Frozen Simulation Contract

- delayed consolidated SIP daily bars, adjusted consistently through the
  repository fetcher;
- full-history indicators computed before any evaluation slice;
- signals compare the close with a shifted prior-channel value;
- the production stock-SMA200 and $20M 20-day SIP dollar-volume gates;
- the production SPY regime classifier, allowing TRENDING, RANGING, and
  VOLATILE while blocking BEAR;
- earnings blackout omitted because no point-in-time calendar is available;
- DAY STOP_LIMIT entry on the following session, with the production 500 bps
  / 2 ATR tighter-of chase ceiling and intraday trigger/retrace semantics;
- static protective stop at fill minus 2 ATR, including gap-through fills;
- channel exits filled at the following session open;
- one position per symbol, no pyramiding;
- shared account equity, 0.40% risk budget per entry, 12% baseline Donchian
  sleeve notional, 4.8% per-position notional cap, and eight-position ceiling;
- no allocator stretch, so results do not assume other sleeves donate capital;
- exits process before same-session entries; simultaneous candidates follow
  the frozen liquidity-ranked watchlist order;
- 5 bps adverse cost on market-like exits and protective stops. Passive
  STOP_LIMIT entries are not charged invented arrival-price slippage.

The observation-only 4R heat cap is not enforced, matching paper behavior.
Sector heat remains warning-only and has no entry effect.

## Temporal Protocol

The common evaluation range begins 2017-01-01 after 2016 indicator warmup.
Calendar years 2017-2020 form the initial development history. Calendar years
2021-2025 are five untouched held-out folds. For each held-out year, the
variant with the highest annualized Sharpe on all preceding completed years is
selected; ties within 0.02 Sharpe go to the lower maximum drawdown, then the
slower entry window. The selected variant is then scored only on the next year.

Every fixed variant is also reported in every held-out fold. Partial 2026 is a
labelled shadow period and cannot affect the verdict.

## Metrics And Frozen Decision Rule

Report shared-portfolio return, annualized Sharpe, maximum drawdown, trade
count, win rate, mean R, exit-reason mix, skipped capacity candidates, and
symbols with usable history. Report per-year results and the stitched
expanding-selection path. Absolute dollar results are omitted.

A challenger may be recommended for a separate paper-configuration PR only if
all of the following hold on the five fixed held-out folds:

1. it beats 30/15 on return in at least four of five years;
2. its stitched held-out Sharpe exceeds 30/15 by at least 0.15;
3. its held-out maximum drawdown is no more than 3 percentage points worse;
4. pooled held-out mean R is positive; and
5. the result is not dependent on one year or one symbol (remove-best-year and
   remove-best-symbol sensitivity remains directionally favorable).

If no challenger clears every bar, retain 30/15. A good static-cohort result
does not validate the watchlist selector and does not erase the need for paper
evidence under the new configuration hash.

## Known Limitations

- current-cohort survivorship and selection bias;
- no delisted-name history or historical fundamentals membership;
- earnings blackout omitted;
- daily OHLC cannot reveal intrabar path after both stop and limit levels trade;
- allocator stretch, cross-sleeve contention, and observation-only heat are
  excluded;
- SIP research volume differs from the live paper engine's IEX feed, although
  SIP is required for an interpretable consolidated-liquidity gate.

These limitations must accompany the result; they are not footnotes that can
be dropped from a favorable summary.
