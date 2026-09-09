# RSI Watchlist Selection And Refresh

**Status:** Active procedure; v3 pool promoted 2026-09-09.

**Rule version:** `rsi_watchlist_v3_durable_company_pool`

**Target:** 50 ranked opportunity candidates, plus any temporarily protected
symbols with open RSI positions.

The current runtime list is the report's 50 candidates plus ABNB and CCK,
which were open at promotion time and remain only until flat and terminal.

## Purpose

The RSI watchlist is a stable opportunity pool for the active RSI3 reversion
strategy. It is refreshed periodically from durable tradability, liquidity,
data-quality, size, and financial-survival evidence.

The selector is forward-oriented: it asks which stocks look suitable to watch
for future RSI3 setups. It cannot know future returns. Historical event studies
and backtests are useful context, but they do not determine membership and are
not promotion gates.

## Responsibility Split

```text
Tradable universe -> offline selector -> 50-name static pool
                  -> RSI3 signal -> runtime edge filter -> risk -> execution
```

- The selector chooses a broad pool of plausible future candidates.
- `RSIReversion` decides whether RSI3 is below 15 and when a bounce exit occurs.
- `RSIEdgeFilter` rechecks stock-above-SMA200 and live liquidity at decision
  time. A name passing the periodic scan never bypasses those runtime gates.
- `RiskManager` and `SleeveAllocator` size and constrain every actual entry.

Selection should not reproduce the entire runtime gate stack or optimize the
pool for historical backtest winners. The pool is deliberately broader than
the set of names that could enter on any particular day.

## Active Strategy Context

The production configuration comes from `settings.RSI_REVERSION_PARAMS`:

- RSI period: 3
- entry: RSI3 below 15 while flat (`level_below`)
- exit: close above SMA5 or RSI3 above 55
- entry order: limit
- runtime edge gates: close above SMA200 and 20-day average dollar volume at
  least $10 million
- protective stop: 2 x ATR14
- risk target: 0.25% of account equity
- hard maximum positions: 8

The 50-name pool expands opportunity coverage; it does not increase the
position limit or weaken any entry, sizing, stop, ownership, or account guard.

## Scanner

The authoritative report-only selector is:

- `scripts/rsi_watchlist_scan.py`
- default target: 50
- delayed research feed: SIP
- lookback: 420 calendar days

### Eligibility and ordering

The scanner requires only:

- at least 260 clean daily bars;
- active, tradable, stock-like Alpaca security;
- close at least $10;
- 50-day average dollar volume at least $50 million;
- market capitalization at least $2 billion and affirmatively established
  solvency when `--include-fundamentals` is used.

Eligible companies are ordered by 50-day average dollar volume. This is an
execution-quality priority, not a return forecast. To keep Yahoo lookups
bounded after removing temporary technical gates, fundamentals are evaluated
in that order until twice the requested pool size (or the pool size plus 25,
whichever is larger) qualifies.

Share volume, SMA200 state, 52-week position, current ATR, Bollinger width,
one- and five-day returns, and historical RSI14 event outcomes remain visible
for review. None can include, exclude, or rank a company. The active strategy
itself remains RSI3, and current SMA200 and liquidity are enforced at runtime.

Sector concentration is accepted as an output of the method. The scanner does
not cap sectors or rerank candidates for diversification.

Alphabet has one explicit share-class rule: `GOOGL` is never eligible, and an
otherwise eligible `GOOG` is preserved in the selected 50 even if it falls
below the normal dollar-liquidity cutoff.

## Backtest Policy

Backtests answer, "How would these names have behaved under specified
historical assumptions?" They do not answer which names will work next.

Accordingly:

- backtest results are optional reference material;
- historical return, Sharpe, or profit factor does not automatically include
  or exclude a symbol;
- a weak historical result may prompt manual investigation, not an automatic
  veto;
- a strong historical result is not sufficient promotion evidence;
- the active paper cohort remains the authority for evaluating the strategy.

The old static-universe builder, post-analysis ranker, hybrid comparison, and
RSI14 portfolio reports are retained only for historical research. They are not
part of this refresh procedure.

## Refresh Procedure

### 1. Run focused tests

```bash
/Users/franco/trading-bot/venv/bin/pytest \
  tests/test_rsi_watchlist_scan.py \
  tests/test_watchlist_review.py \
  tests/test_strategies.py
```

### 2. Generate the candidate report

```bash
/Users/franco/trading-bot/venv/bin/python scripts/rsi_watchlist_scan.py \
  --top 50 \
  --feed sip \
  --end-delay-minutes 60 \
  --include-fundamentals \
  --output docs/reports/rsi_watchlist_scan_latest.md
```

The command is report-only. It never edits `config/settings.py`.

By default it reads the trade ledger and appends any open RSI position that is
outside the ranked 50 as a clearly marked protected symbol. Protected symbols
do not consume one of the 50 opportunity slots. `--ignore-open-positions`
exists for research diagnostics only and must not be used for promotion.

### 3. Review the proposed pool

Review:

1. all 50 names are ordinary stocks and operationally tradable;
2. fundamentals were actually enforced and failures are visible;
3. recent metrics are not distorted by a split, merger, stale bars, or other
   corporate action;
4. sector concentration is accepted as the method's output and has not been
   manually reranked;
5. `GOOGL` is absent and eligible `GOOG` is preserved;
6. current watchlist names that disappear have a documented reason;
7. every ledger-confirmed open RSI position remains protected in the runtime
   list, and unresolved RSI orders are checked separately before promotion;
8. no single historical backtest metric is being treated as a forecast.

Backtests may be run here as supporting context, but they cannot decide the
promotion outcome.

For an expansion, preserve established members unless there is a separate,
documented removal reason. Fill the new slots from the highest-priority acceptable
nonmembers after corporate-action and data-integrity review. Do not replace most of the pool
merely because one refresh reordered dollar-liquidity priority.

### 4. Recheck risk-target coverage

For the proposed runtime list, fetch current delayed-SIP daily bars, calculate
latest ATR14/close, and report the minimum, p10, median, and p90. Confirm how
many names would be clipped below the 0.25% risk target by sleeve or notional
caps. See `allocator_risk_target_reconciliation.md` section 9.

Ordinary cap clipping is safe because it reduces risk. Repeated clipping across
many names indicates that the watchlist's calm end moved and the target should
be reviewed separately; a watchlist refresh does not silently change the risk
target.

### 5. Promote with explicit approval

After operator approval:

- update `RSI_WATCHLIST` in `config/settings.py`;
- retain protected open-position symbols until they are flat and terminal;
- update this document, `rsi_reversion_strategy.md`, `strategies.md`, and
  `PLAN.md` if their operational statements change;
- run the focused tests and the full unit suite;
- commit code, tests, reports, and documentation together;
- recycle the paper bot only with `./recycle_bot.sh`;
- verify startup ownership, stop protection, and the 50-name RSI slot count.

## Evidence Cohorts

The strategy configuration hash includes the watchlist. A promoted refresh
therefore begins a new RSI paper-evidence cohort automatically. Pre-refresh and
post-refresh results remain available but must not be pooled as if membership
were unchanged.

## Cadence

Review quarterly, or sooner after a material market-structure change, sustained
candidate starvation, repeated symbol-specific failures, corporate actions, or
tradability changes. Stability matters: do not rotate the list merely because
another name briefly has higher dollar volume.

## Historical Documents

- `static-rsi-watchlist-selection.md`: obsolete RSI14/backtest-first procedure.
- `dynamic-rsi-watchlist.md`: obsolete future dynamic-runtime design.
- `reports/rsi_static_universe_latest.md`: historical May 2026 RSI14 research.
- `reports/rsi_static_backtest_report_latest.md`: historical May 2026 RSI14
  research.
- `reports/rsi_portfolio_backtest_latest.md`: historical May 2026 simplified
  RSI14 research.
- `reports/rsi_hybrid_comparison.md`: historical May 2026 hybrid research.
- `reports/rsi_watchlist_scan_v2_20260909.md`: superseded v2 scan retained for
  audit history; it must not be used for promotion.
