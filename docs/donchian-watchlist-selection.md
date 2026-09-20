# Donchian Watchlist Selection And Refresh

**Status:** Active procedure; v1 pool promoted 2026-09-19.

**Rule version:** `donchian_watchlist_v1_durable_liquid_pool`

**Target:** 100 ranked opportunity candidates, plus any temporarily protected
symbols with open Donchian positions.

**Pending review:**
`docs/reports/donchian_watchlist_scan_11_72_candidate.md` reflects the shared
fundamentals-parser correction but is not active configuration. ISRG now
qualifies at rank 84 and moves LIN to rank 101. Promotion requires separate
operator approval and private lifecycle-protection reconciliation.

## Purpose

The Donchian watchlist is a stable opportunity pool for the active 30/15
breakout strategy. It is refreshed from durable tradability, liquidity,
data-quality, company-size, and financial-survival evidence.

The old 52-name universe was operator-curated around AI, semiconductors, power,
space, and related themes. The original 32-name research established how 30/15
behaved inside that supplied universe; it did not establish that those were the
best companies for Donchian to monitor.

The selector is forward-oriented. It asks which companies are reliable enough
to watch for future breakouts. It does not select historical Donchian winners.

## Responsibility Split

```text
Tradable universe -> offline selector -> 100-name static pool
                  -> 30-day breakout -> runtime edge filters -> risk -> execution
```

- The selector chooses a broad pool of operationally suitable companies.
- `DonchianBreakout` decides whether the close exceeded the prior 30-day high.
- `DonchianEdgeFilter` rechecks stock-above-SMA200, the earnings blackout, and
  live liquidity at decision time.
- The engine's regime gate blocks BEAR entries.
- `RiskManager` and `SleeveAllocator` size and constrain every actual entry.

Membership must not reproduce the runtime signal or optimize for historical
breakout outcomes. Current trend state belongs to the strategy, not the pool.

## Active Selection Contract

The authoritative report-only selector is
`scripts/donchian_watchlist_scan.py`. The promotion command uses delayed SIP
bars and fundamentals:

```bash
/Users/franco/trading-bot/venv/bin/python scripts/donchian_watchlist_scan.py \
  --pool-sizes 50 100 200 \
  --promotion-size 100 \
  --feed sip \
  --end-delay-minutes 60 \
  --include-fundamentals \
  --ignore-open-positions \
  --output docs/reports/donchian_watchlist_scan_latest.md
```

The scanner requires:

- at least 260 clean adjusted daily bars;
- active, tradable, stock-like Alpaca security;
- close of at least $10;
- 50-day average SIP dollar volume of at least $50 million;
- market capitalization of at least $2 billion; and
- affirmatively established solvency: profitable, or at least 12 months of
  cash runway when unprofitable.

Fundamentals fail closed per field. The shared parser uses Yahoo's exact
`Net Income` row first and `Net Income Common Stockholders` as the only approved
fallback. Provider errors, unavailable market cap, unavailable solvency facts,
and a known sub-12-month runway are reported separately. A missing fact excludes
the company from a promotion scan but is not labeled as a negative financial
fact.

Eligible companies are ordered by 50-day average dollar volume. Liquidity is a
durable execution priority, not a prediction of return. `GOOGL` is excluded;
`GOOG` is the eligible Alphabet share class but must rank into the pool on the
same durable rule as every other company. Broker dot-class symbols are
translated to Yahoo's hyphen form only at the fundamentals/metadata provider
boundary, so an unavailable lookup is not mislabeled as a sub-$2 billion
company.

The following remain visible but do not include, exclude, or rank a company
under the v1 rule:

- stock-above-SMA200 state;
- ATR percentage;
- 12-to-1-month momentum;
- proximity to the 52-week high;
- recent 30-day breakout count; and
- historical Donchian return, Sharpe, win rate, or profit factor.

## Why 100 Names

The 2026-09-19 delayed-SIP snapshot compared nested liquidity pools. The raw
breakout counts use companies selected today, so they characterize opportunity
coverage and contention; they are not a survivorship-free backtest.

| Pool | Breakouts over trailing 252 sessions | Active breakout days | Peak same-day signals | Days above 8-position capacity | Median ATR% | Conservatively cap-clipped |
|---:|---:|---:|---:|---:|---:|---:|
| 50 | 479 | 87 | 18 | 17 | 3.98% | 26 |
| **100** | **896** | **89** | **27** | **42** | **3.79%** | **56** |
| 200 | 1,727 | 90 | 41 | 79 | 3.23% | 136 |

Fifty names offered little breadth beyond the old 52-name pool. One hundred
nearly doubled raw opportunity coverage while remaining operationally modest:
the live engine currently evaluates 167 slot-symbol combinations per cycle and
normally completes a warmed market-hours cycle in roughly 13-23 seconds against
a five-minute cadence. The 100-name pool raises that count to approximately
215. Two hundred would raise it to approximately 315, make cold-cache cycles
materially heavier, and almost double the days on which raw signals exceeded
the sleeve's eight-position capacity.

The 100-name choice is a first paper cohort, not a permanent optimum. Raw
contention is deliberately reported because the engine does not yet use an
outcome-predictive candidate ranker. Dollar-liquidity order is deterministic,
but no claim is made that earlier names will outperform later names.

## Ranking Experiments

The scanner exposes `momentum` and `high52` modes for research.
They are not the active membership rule.

In the same current-universe snapshot:

- momentum ranking increased the top-100 trailing breakout count to 1,173 but
  retained only eight names from the former manual universe and selected much
  higher-volatility names;
- 52-week-high ranking increased it to 1,664 but retained only two former
  names and cap-clipped 83 of 100 under the current sizing controls.

Those results mostly show that selecting companies already in strong trends
produces more historical breakouts. They do not establish forward superiority
and would create large quarterly churn. Liquidity therefore remains the v1
default until a point-in-time walk-forward comparison justifies another rule.

## Risk And Capacity Findings

At the current allocation, the baseline Donchian per-position notional cap is
4.8% of account equity. A 0.40% risk target with a 2x ATR stop requires
ATR14/close of at least 4.17% for risk sizing to bind before the notional cap.
The promoted 100 contains 56 calmer names that will be conservatively
cap-clipped. This lowers risk; it is not a correctness failure.

The result does supersede the old comment that the 0.40% target covered the
full Donchian list. That claim was calibrated before the sleeve allocation fell
from 25% to 15%. Do not change the risk target inside a universe refresh;
recalibration belongs to the separate long-term risk-target maintenance item.

## Protected Holdings

A static watchlist is also the strategy's signal-exit evaluation universe.
Every ledger-confirmed open Donchian position outside the refreshed top 100 is
listed in the report's protected section and must be appended to the promoted
configuration until flat and terminal—even when it still ranks inside a larger
50/100/200 comparison pool. Protected symbols do not consume one of the 100
opportunity slots and cannot be removed merely because they fail the new
eligibility rule.

The committed research report uses `--ignore-open-positions` so account state is
not written to source control. Before promotion, run a private copy without
that flag and reconcile its protected names against the proposed configuration.
Remove each lifecycle-preservation member only after it is flat and terminal.

## Refresh Procedure

1. Run the focused scanner and watchlist tests.
2. Generate a delayed-SIP report with fundamentals enabled.
3. Review data integrity, exclusions, rejection reasons, open-position
   protection, risk-target coverage, and raw contention.
4. Preserve the rule and target size unless a separately documented review
   authorizes a change; do not hand-rerank sectors or themes.
5. Check unresolved Donchian entry orders separately from open positions.
   A missing trade database is an error; only the sanitized committed report
   may use `--ignore-open-positions`.
6. Obtain explicit operator approval before promotion.
7. Update `DONCHIAN_WATCHLIST`, this document, the strategy documentation, and
   `PLAN.md`; run the full suite; recycle only with `./recycle_bot.sh`.

Refresh quarterly, or sooner after a material tradability, data-quality, or
market-structure change. Stability matters: do not rotate the pool merely
because one refresh slightly reorders dollar liquidity.

## Evidence And Limitations

- Promotion starts a new strategy-configuration-hash paper cohort.
- Pre- and post-refresh outcomes must not be pooled as one configuration.
- The snapshot uses today's tradable universe and is not a
  survivorship-free historical test.
- A future selector comparison must reconstruct membership point in time,
  include delisted names where possible, and model shared capital, the
  eight-position cap, execution, and candidate contention.
- Paper trading remains the authority for the promoted configuration.
