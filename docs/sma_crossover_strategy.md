# SMA Crossover — Strategy Research & Deployment Guide

**Status:** ✅ **PAPER ACTIVE** — wired in `engine/trader.py` and `forward_test.py`
since project inception. The original MVP trend-follower for this project.

**Last updated:** 2026-09-28

> **Entry-quality audit (PLAN `11.70`, closed 2026-09-11):** the
> production-mirror control, wider structure-aware stop, five-session pullback,
> and fixed extension cap completed a fixed SIP development/held-out
> comparison. No alternative passed the retention, stability, drawdown, and
> paired-CI decision rule, so current paper behavior is retained. See
> [`sma_entry_quality_audit.md`](sma_entry_quality_audit.md).

---

## Why this strategy

Slow trend-following on individual names. The thesis is simple and old:
medium-term moving-average crossovers identify regime shifts in a stock's
price trajectory. When the fast SMA crosses above the slow SMA, recent
price action is dominating older price action — a directional regime has
begun. When it crosses back below, the regime has ended.

The strategy has no take-profit, no trailing stop, and no exit other than
the death cross + a static disaster brake. It is intentionally patient.
On this watchlist, profit concentrates in a small number of explosive
runners (ASML, NVDA, MSTR, etc.); the strategy's job is to ride those
runners to their natural reversal without being shaken out by intermediate
pullbacks.

**Why 20/50 and not 50/200 (classic "golden cross"):**
The 50/200 golden cross is iconic but fires a few times per decade per
name — far too rare to be a workable strategy at the watchlist scale we
run (100 ranked names). The 20/50 cross fires multiple times per name per year,
which is what makes the sleeve generate enough trades to be statistically
meaningful. The 200 SMA *is* used elsewhere — as a gate in
`SMAEdgeFilter` (stock above its 200 SMA) and by `RegimeDetector` when
classifying the macro BEAR state from SPY. The 200 SMA is a *structural gate*;
20/50 is the *trigger*.

---

## Deployment configuration

| Parameter | Value | Source |
|---|---|---|
| `fast` | 20 | `engine/trader.py:5653`, `forward_test.py:209` |
| `slow` | 50 | same |
| Order type | MARKET | `SMACrossover.preferred_order_type` |
| Regime gate | `TRENDING`, `RANGING` only | `settings.STRATEGY_ALLOWED_REGIMES` |
| Edge filter | `SMAEdgeFilter` + `SectorMomentumFilter` | `forward_test.py:210-216` |
| Sleeve weight | 0.30 of deployable capital (target) | `settings.STRATEGY_ALLOCATIONS["sma_crossover"]["target_pct"]` |
| ATR stop | `entry − 2.0 × ATR(14)` (static) | `settings.ATR_STOP_MULTIPLIER` |
| Watchlist | 100 ranked v3 names plus lifecycle preservation | `config/settings.py` |
| Stop time-in-force | GTC (attached DAY child → confirmed cancel/rebuild as standalone GTC) | `engine/trader.py` |
| Fractional shares | Enabled when MARKET path active | `settings.FRACTIONAL_ENABLED` |

---

## Signal logic

Implemented in `strategies/sma_crossover.py`:

```python
fast_ma = close.rolling(20).mean()
slow_ma = close.rolling(50).mean()
diff    = fast_ma - slow_ma
prev    = diff.shift(1)

entries = (diff > 0) & (prev <= 0)   # 20 SMA crosses above 50 SMA
exits   = (diff < 0) & (prev >= 0)   # 20 SMA crosses below 50 SMA
```

**Look-ahead safety.** Both `rolling().mean()` and `shift(1)` use only past
data. The signal at bar `t` depends only on closes up to and including `t`.
The backtester shifts execution to bar `t+1`'s open; this strategy does
*not* itself shift.

**Order type.** Trend-followers prefer marketable orders — a crossover
means the move is already underway and missing the fill is worse than
paying a few bps of spread. The execution layer reads
`preferred_order_type = OrderType.MARKET` and routes accordingly.

---

## Exit logic

Two exits, both **static** (no trailing). The strategy does not implement
a take-profit.

### 1. Death-cross exit (primary)

Fires when `fast SMA < slow SMA` after being `≥` on the prior bar. Engine
reads `exits` from the SignalFrame each cycle and closes at market on the
next open. This is the "intended" exit.

### 2. ATR protective stop (disaster brake)

Set once at entry as a GTC stop order at the broker:

```python
stop_price = entry_price − 2.0 × ATR(14)
```

The stop does **not** ratchet up as price moves favorably. Its purpose is
asymmetric: cap the loss on a *bad entry* (head-fake crossover), and
serve as the denominator for the fixed-fractional position sizer
(`risk_dollars = equity × risk_per_trade_pct["sma_crossover"]` — 0.60% of
equity per 11.48; `qty = risk_dollars / |entry − stop|`. `MAX_POSITION_PCT`
= 2% remains only the global ceiling; pre-11.48 it was the formula input
but the notional caps overruled it on essentially every entry).
Once price moves meaningfully above entry, the static stop is well below
entry and is essentially dormant until the death cross fires.

---

## Filter stack

Applied in order. Any failure blocks the entry.

1. **Regime gate** (`settings.STRATEGY_ALLOWED_REGIMES["sma_crossover"]`) —
   sleeve enabled only when `RegimeDetector` reports `TRENDING` or
   `RANGING`. Disabled in `BEAR` and `VOLATILE`.
2. **`SMAEdgeFilter`** (`strategies/filters/sma_crossover.py`) —
   stock must be above its 200 SMA and show volume expansion.
3. **Earnings blackout** inside `SMAEdgeFilter` — no entry in the two calendar
   days before earnings; post-earnings entries are allowed immediately.
4. **`SectorMomentumFilter`** (`sector/gauge.py`) — warning-only for SMA.
   HOT/NEUTRAL/COLD/unknown context is recorded but never vetoes an entry.

The old standalone `SPYTrendFilter` is intentionally disabled because the
regime detector owns the macro SPY>200-SMA condition used to classify BEAR.

The composite is wired in `forward_test.py`:

```python
SMACrossover(
    fast=20, slow=50,
    edge_filter=CompositeEdgeFilter([
        SMAEdgeFilter(),
        SectorMomentumFilter(
            gauge=sector_gauge, resolver=sector_resolver,
            sector_entry_policy="warn",
        ),
    ]),
)
```

---

## Watchlist

Selection rules are spec-stable and documented separately in
[`sma-watchlist-selection.md`](sma-watchlist-selection.md).

Composition is dynamic and lives in `config/settings.py::SMA_WATCHLIST`.
Findings about *what* to put on the list and how often to refresh it are
tracked in [`sma_crossover_optimizations.md`](sma_crossover_optimizations.md).

**Current composition (2026-09-28):** 100 names ranked by 50-session SIP dollar
liquidity after durable price, size, solvency, and share-class gates, plus any
temporary lifecycle-preservation members. Technical state, historical
crossovers, FCF/revenue, ATR, and sector are diagnostics only. Promotion starts
a new exact-configuration paper cohort. See
[`sma-watchlist-selection.md`](sma-watchlist-selection.md) and
[`sma-watchlist-methodology-audit.md`](sma-watchlist-methodology-audit.md).

---

## Backtest validation

The strategy was originally validated via the Phase 5 vectorbt harness
(`backtest/runner.py`) on the early SMA watchlist. The current empirical
baseline is established by the **giveback audit**:

- Script: `scripts/sma_giveback_audit.py`
- Window: 2018-11-01 → 2026-06-05 (~7.5 years of Alpaca IEX daily bars)
- Universe: 40-name pinned `AUDIT_UNIVERSE` (frozen for reproducibility;
  not `settings.SMA_WATCHLIST` which drifts)
- Output: 571 entries, **34.9% headline win rate**, baseline net P&L
  **$8,277 per-share unit**, 61.5% capture ratio of peak open profit on
  the 174 death-cross winners.

Full results — including the failed exit-overlay experiments
(chandelier trail, profit-gated trail, fixed take-profit), the
per-symbol profit-concentration analysis, and the methodology gates
required before any operational change — are in
[`sma_crossover_optimizations.md`](sma_crossover_optimizations.md).

The Phase 5 vectorbt harness (`backtest/runner.py`) was used for
*initial* signal-logic validation and remains the right tool for
parameter sensitivity / walk-forward studies; the giveback audit is a
purpose-built simulator with stricter intrabar semantics for the
exit-policy comparison.

---

## Methodology and limitations

**Look-ahead — signal generation.** Signals fire on bar `t`'s close;
execution shifts to bar `t+1`'s open. The vectorbt harness uses the
`_shift_for_next_open` helper in `backtest/runner.py`; the giveback
audit uses an equivalent open-of-next-bar convention implemented
directly in the per-policy simulators.

**Look-ahead — intrabar exits.** The giveback audit avoids intrabar
look-ahead on the entry bar: the chandelier / gated-trail policies
enforce only the static disaster stop on entry day (since the trail
level would otherwise reference the entry bar's close, which prints
after the bar's high/low). The trail engages from the bar after entry
onward. Documented and unit-tested in
`tests/test_sma_giveback_audit.py::TestPolicyChandelierEntryBarNoLookahead`.

**Cost model.** The vectorbt harness applies 5 bps slippage and zero
commission (Alpaca default).

The giveback audit (`sma_giveback_audit.py`) runs **without slippage or
commissions** — this is a known limitation, not a costs-cancel
argument. An earlier exit under one policy can permit a later golden
cross to fire as a new entry that the baseline policy never reaches,
so the trade count and entry/exit prices differ across policies. Per-
fill slippage also differs by exit mechanism: a stop-out fills through
the trigger level, a market sell on a death-cross signal fills at the
next-open quote, and a take-profit fills at the target. Treat the
audit's policy-comparison numbers as a relative-mechanics study —
adding production-equivalent costs is a known follow-up before any
operational decision is taken on the results.

Live realized slippage is logged per fill via `reporting.logger` on
the unified taxonomy columns (`slippage_signed_bps`,
`slippage_adverse_bps`, plus `slippage_benchmark_kind` and
`slippage_measurement_quality` for provenance) regardless of which
research tool is run. The legacy `realized_slippage_bps` /
`modeled_slippage_bps` columns are NULL on new rows after the
Phase 2 + 4 unification (PR #67) — see
`docs/slippage_unification_design.md`.

SMA entries and signal-driven exits are both market orders, so both
carry an execution-quality measurement: an NBBO midpoint is captured
immediately before submission (`arrival_midpoint` / `primary`),
falling back to the prior bar close (`fallback_latest_close` /
`fallback`) when the quote is unavailable. Only the former counts as
execution quality — the fallback measures market drift between that
close and the fill, and PR #84 excludes it from the drift kill switch
and the L2 execution check. Stop-out fills record against the active
stop price, which is the stop-gap erosion family and likewise excluded
(PLAN `11.49`).

**Data feed.** Production runs on Alpaca's real-time IEX feed because of the
paper-account entitlement. Offline selection and current research use delayed
SIP through `BACKTEST_DATA_FEED`, while execution replay uses IEX to reproduce
what the bot saw. Historical reports retain their originally disclosed feed.

**Sample size.** ~10–18 trades per name per 7.5-year window. Individual
symbol-level conclusions are noisy; only aggregate watchlist-level
conclusions are statistically meaningful.

**Survivorship bias.** The current watchlist contains symbols that exist
*today*. Names delisted or merged before 2026 are not in the backtest.
This biases historical results upward.

**Regime coverage.** The 2018-11 → 2026-06 window contains one full bull
cycle (2019–2021), the 2020 COVID crash + melt-up, a 2022 bear, and the
2024–2026 AI rally. It does not contain a 2008-style deep, prolonged
bear — the strategy's behavior in that regime is untested in this window.

---

## Implementation files

- `strategies/sma_crossover.py` — strategy class.
- `strategies/filters/sma_crossover.py` — `SMAEdgeFilter`.
- `strategies/filters/common.py` — `SPYTrendFilter` (shared).
- `sector/gauge.py` — `SectorMomentumFilter`.
- `regime/detector.py` — regime classifier.
- `risk/manager.py` — sizing + ATR stop computation.
- `engine/trader.py` — engine wiring (live + paper).
- `forward_test.py` — forward-test wiring with full filter stack.
- `backtest/runner.py` — vectorbt harness used for initial validation.
- `scripts/sma_giveback_audit.py` — exit-rule and profit-concentration audit.
- `scripts/sma_watchlist_scan.py` — retired v2 implementation; CLI requires
  explicit historical opt-in and the module temporarily supplies shared helpers.
- `scripts/sma_durable_watchlist_scan.py` — active v3 durable-pool selector.

## Related docs

- [`sma-watchlist-selection.md`](sma-watchlist-selection.md) —
  watchlist selection rules (spec-stable).
- [`sma_crossover_optimizations.md`](sma_crossover_optimizations.md) —
  audit findings, experiments, and ranked optimization map (living).
- [`SMA-edge-filter.md`](SMA-edge-filter.md) — edge filter design.
- [`regime_flowchart.md`](regime_flowchart.md) — regime classification.
- [`capital_allocation_reference.md`](capital_allocation_reference.md) —
  sleeve weights and capital budgeting.
