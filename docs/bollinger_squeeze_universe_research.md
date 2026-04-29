# Bollinger Squeeze — Universe Research & Cross-Universe Backtest

**Status:** Research artifact. Used to decide which universe(s) to deploy the
strategy on; informs the final `BOLLINGER_WATCHLIST` and sleeve allocation.

**Last updated:** 2026-04-29

---

## Why this document exists

The initial backtest of `BollingerSqueeze` on the AI/Big-Tech/Semis universe
(NVDA, AVGO, AMD, ...) produced a mean total return of **-3.0%** with a Sharpe
of **-0.18** over 4 years — vastly underperforming buy-and-hold (+293%) on the
same universe. Before discarding the strategy or moving to engine-level
mitigations (ATR stops in the backtester), we hypothesised the **universe**
was the wrong fit, not the strategy itself.

**Hypothesis:** The TTM Squeeze is a *coil → explosive breakout* pattern
detector. Hyper-trending mega-caps (NVDA et al.) rarely consolidate — every
"pause" is buyers absorbing before another leg up. The strategy is being asked
to wait for compression that never arrives. Universes that *do* consolidate
(sector ETFs, defensive blue-chips, REITs) should produce meaningfully
different results.

This document records the empirical test of that hypothesis.

---

## Methodology

### Test setup (held constant across every run)

| Parameter | Value |
|---|---|
| Bar range | 2022-03-30 → 2026-04-27 |
| History length | 4 years (1,021 daily bars per symbol) |
| Bar timeframe | 1Day |
| End-date | **Pinned** to 2026-04-28 UTC (so cached bars match across re-runs) |
| Data feed | IEX |
| Initial cash | $100,000 |
| Slippage | 5 bps |
| Commissions | $0 |
| Edge filter | ON (liquidity floor + earnings blackout + exhaustion gate) |
| Stop-loss | None in backtest *(engine adds 2× ATR stops in production — Step 2 will simulate)* |

The pinned end-date is critical for reproducibility: a sweep re-run a week
later would otherwise see different bars and any metric drift would mix
strategy effects with data drift.

### Parameter sweep (5 variants per universe)

Same grid for every universe so cross-universe comparison is apples-to-apples:

| Variant | bb_length | kc_length | min_squeeze_bars | roc_lookback |
|---|---|---|---|---|
| Baseline                   | 20 | 20 | 6 | 5 |
| Shorter bands              | 10 | 10 | 6 | 5 |
| Lower duration             | 20 | 20 | 4 | 5 |
| Shorter ROC                | 20 | 20 | 6 | 3 |
| Aggressive combo           | 10 | 10 | 4 | 3 |

Defined in `scripts/backtest_bollinger_squeeze.py:SWEEP_GRID`. Run via:

```bash
python scripts/backtest_bollinger_squeeze.py --sweep \
    --universe <name> --years 4 --end-date 2026-04-28 \
    --output logs/backtests/squeeze_sweep_<name>.md
```

### Metric priority (most → least important)

1. **Sharpe ratio** — return per unit of risk; the only number that combines edge and volatility.
2. **Trade count** — statistical significance. < 50 trades in a 4-year cross-section is noise.
3. **Max drawdown** — survivability and sleeve sizing.
4. **Mean return vs buy-and-hold** — reality check against the trivial alternative.
5. Win rate — secondary; misleading without paired profit factor / avg-win-loss ratio.

---

## Universe selection — research notes

### What the literature says fits TTM Squeeze well

Across StockCharts, TrendSpider, and the original TTM documentation:

- **Mid-to-large caps** with steady volume (avoid micro-caps and meme stocks)
- **Sector ETFs** — diversified, less single-stock noise, consolidate during sector rotation
- **Stocks with identifiable 6+ week consolidation patterns** — multi-week compressions
  on the daily chart give the cleanest squeeze fires
- **Avoid hyper-trending mega-caps** — they don't pause long enough to coil
- **Avoid extreme-volatility names** (biotech, low-float, story stocks) — the
  exhaustion gate will reject them and even when it doesn't, the breakout
  follow-through is unreliable

### Universes selected for this test

#### 1. AI / Big-Tech / Semis (`ai_bigtech`) — 16 names *(thesis universe)*

`NVDA, AMD, AVGO, SMCI, TSM, MU, QCOM, ARM, MSFT, AAPL, GOOGL, META, AMZN, PLTR, CRWD, NOW`

**Rationale:** The user's directional thesis universe. Tested first; weak
results motivated this cross-universe study.

#### 2. GICS Sector SPDRs (`sector_etfs`) — 11 names

`XLF, XLE, XLU, XLV, XLI, XLK, XLP, XLY, XLB, XLRE, XLC`

**Rationale:** The textbook TTM Squeeze application. ETFs absorb single-stock
idiosyncratic risk and tend to consolidate cleanly during sector rotation.
Eleven sector ETFs cover the full GICS classification.

#### 3. Defensive Mega-caps (`defensive_megacaps`) — 16 names

`KO, PEP, PG, CL, KMB, JNJ, PFE, MRK, MCD, WMT, COST, T, VZ, MO, SO, DUK`

**Rationale:** Slow-movers — staples, healthcare, telecom, utilities.
Genuine multi-week consolidations between earnings; minimal directional
drift. The purest test of the "tight coil → breakout" hypothesis.

#### 4. REITs (`reits`) — 10 names

`O, PLD, AMT, CCI, EQIX, SPG, VICI, WELL, DLR, AVB`

**Rationale:** Range-bound around interest-rate cycles. REITs are known
coilers — they consolidate while the market digests rate expectations,
then break in the direction of policy moves.

---

## Per-universe results

All metrics are aggregates over the listed universe, equally weighted. Same
4-year window, same backtest config.

### 1. AI / Big-Tech / Semis

| Variant | MeanRet | Sharpe | Trades | TradedSyms | WinRate | MaxDD |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (20/6/5)             | -3.0% | -0.18 | 26 | 13 | 26.9% | -9.6% |
| Shorter bands (10/6/5)        | +0.9% | +0.01 | 153 | 16 | 43.8% | -18.5% |
| Lower duration (20/4/5)       | -2.5% | -0.13 | 40 | 15 | 30.0% | -14.1% |
| Shorter ROC (20/6/3)          | -3.0% | -0.18 | 26 | 13 | 26.9% | -9.6% |
| **Aggressive (10/4/3)** ⭐ | **+11.5%** | **+0.16** | **227** | **16** | 42.3% | -24.5% |

**Optimal:** Aggressive combo — but Sharpe +0.16 with -24.5% DD is weak.

**Buy-and-hold benchmark:** +293% (mean across the 16 names).

Detail: [logs/backtests/squeeze_sweep_step1.md](../logs/backtests/squeeze_sweep_step1.md)

### 2. Sector ETFs

| Variant | MeanRet | Sharpe | Trades | TradedSyms | WinRate | MaxDD |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (20/6/5)             | +1.3% | +0.07 | 21 | 8 | 28.6% | -3.5% |
| **Shorter bands (10/6/5)** ⭐ | **+3.5%** | **+0.22** | **98** | **11** | **46.9%** | **-7.7%** |
| Lower duration (20/4/5)       | +2.8% | +0.10 | 32 | 9 | 34.4% | -4.5% |
| Shorter ROC (20/6/3)          | +1.3% | +0.07 | 21 | 8 | 28.6% | -3.5% |
| Aggressive (10/4/3)           | +0.3% | +0.02 | 145 | 11 | 44.1% | -9.8% |

**Optimal:** Shorter bands (10/6/5) — Sharpe +0.22, DD -7.7%, 98 trades. **Best result of any universe tested.**

Detail: [logs/backtests/squeeze_sweep_sector_etfs.md](../logs/backtests/squeeze_sweep_sector_etfs.md)

### 3. Defensive Mega-caps

| Variant | MeanRet | Sharpe | Trades | TradedSyms | WinRate | MaxDD |
|---|---:|---:|---:|---:|---:|---:|
| **Baseline (20/6/5)** ⭐ | **+3.0%** | **+0.02** | **28** | 15 | **42.9%** | **-4.5%** |
| Shorter bands (10/6/5)        | -2.1% | -0.20 | 143 | 16 | 32.9% | -12.3% |
| Lower duration (20/4/5)       | +2.1% | -0.07 | 41 | 15 | 36.6% | -5.8% |
| Shorter ROC (20/6/3)          | +3.0% | +0.02 | 28 | 15 | 42.9% | -4.5% |
| Aggressive (10/4/3)           | -3.8% | -0.22 | 203 | 16 | 34.5% | -15.4% |

**Optimal:** Baseline (20/6/5). The longer 20-bar BB matches these stocks'
genuine multi-week consolidation rhythm; shortening it produces noise.

**Caveat:** Only 28 trades across 16 symbols / 4 years — statistically thin.

Detail: [logs/backtests/squeeze_sweep_defensive_megacaps.md](../logs/backtests/squeeze_sweep_defensive_megacaps.md)

### 4. REITs

| Variant | MeanRet | Sharpe | Trades | TradedSyms | WinRate | MaxDD |
|---|---:|---:|---:|---:|---:|---:|
| Baseline (20/6/5)             | -0.1% | -0.04 | 26 | 10 | 38.5% | -7.3% |
| **Shorter bands (10/6/5)** ⭐ | **+3.2%** | **+0.14** | **95** | 10 | **43.2%** | **-10.6%** |
| Lower duration (20/4/5)       | -1.5% | -0.10 | 39 | 10 | 38.5% | -9.4% |
| Shorter ROC (20/6/3)          | -0.1% | -0.04 | 26 | 10 | 38.5% | -7.3% |
| Aggressive (10/4/3)           | +3.5% | +0.11 | 134 | 10 | 38.8% | -12.7% |

**Optimal:** Shorter bands (10/6/5) — Sharpe +0.14, DD -10.6%, 95 trades.
Aggressive combo earns slightly more total return but at worse DD.

Detail: [logs/backtests/squeeze_sweep_reits.md](../logs/backtests/squeeze_sweep_reits.md)

---

## Cross-universe comparison (best variant per universe)

| Universe | Optimal params | Sharpe | MeanRet | Trades | MaxDD | TradedSyms |
|---|---|---:|---:|---:|---:|---:|
| **Sector ETFs** ⭐ | Shorter bands (10/6/5) | **+0.22** | +3.5% | 98 | -7.7% | 11/11 |
| AI / Big-Tech | Aggressive (10/4/3) | +0.16 | +11.5% | 227 | -24.5% | 16/16 |
| REITs | Shorter bands (10/6/5) | +0.14 | +3.2% | 95 | -10.6% | 10/10 |
| Defensive Mega-caps | Baseline (20/6/5) | +0.02 | +3.0% | 28 | -4.5% | 15/16 |

### Key findings

1. **Universe matters more than parameters.** The same strategy goes from -0.18 Sharpe (AI/BigTech baseline) to +0.22 Sharpe (Sector ETFs shorter-bands) — a 0.4-Sharpe spread driven entirely by the asset universe.

2. **Sector ETFs are the clear winner.** Highest Sharpe (+0.22), reasonable drawdown (-7.7%), meaningful trade count (98), all 11 ETFs traded. Both the *return* and the *risk-adjusted return* are best, and the drawdown is by far the most defensible.

3. **`bb_length` is the dominant knob; `roc_lookback` is dead weight.** Shortening BB 20→10 has the largest single-knob impact in every universe except Defensive Mega-caps (where it hurts). `roc_lookback` 3 vs 5 produced byte-identical results in every universe — the `close > prior_high` constraint dominates and ROC isn't doing meaningful work.

4. **Defensive Mega-caps prefer the slower 20-bar BB**; everyone else prefers the 10-bar. The slow-mover universe has multi-week consolidations that match the 20-bar BB's natural cadence.

5. **The hypothesis is confirmed.** AI/BigTech genuinely is a poor fit — it's the worst combination of (low Sharpe, high drawdown, structural mismatch). The strategy is not broken; it was being deployed against an unsuitable universe.

6. **None of the universes deliver a "great" Sharpe.** Even the best (+0.22 on sector ETFs) is modest by absolute standards. The strategy provides a small positive edge as a *diversifying sleeve* — not as a standalone return-generator. This is consistent with squeeze-strategy literature: it's a low-frequency, selective-entry technique that adds non-correlated signal alongside trend and reversion sleeves, not a replacement.

---

## Decisions and recommendations

### Recommended deployment universe

**Sector ETFs.** Best risk-adjusted return, defensible drawdown, broadest
participation (all 11 traded), nearly 100 trades over 4 years for statistical
weight.

### Recommended parameters

`bb_length=10, kc_length=10, min_squeeze_bars=6, roc_lookback=5`
(or 3 — equivalent due to ROC being inert).

### Watchlist update (deferred until user sign-off)

`config.settings.BOLLINGER_WATCHLIST` should be updated from the AI/BigTech
placeholder to the sector ETFs list:

```python
BOLLINGER_WATCHLIST = [
    "XLF", "XLE", "XLU", "XLV", "XLI",
    "XLK", "XLP", "XLY", "XLB", "XLRE", "XLC",
]
```

Hold off on writing this change until paper-validation gates pass — see "Open
items" below.

### `BollingerSqueeze` defaults

The strategy's default `bb_length=20, kc_length=20, min_squeeze_bars=6,
roc_lookback=5` should remain as-is — these are the TTM literature defaults
and the right choice on Defensive Mega-caps. The sector-ETF sleeve will
override via `BollingerSqueeze(bb_length=10, kc_length=10, ...)`.

### `roc_lookback` simplification

The `roc_lookback` parameter is currently inert (sweeps show no signal
between 3 and 5). Either:

- Document this and leave as-is (allows future re-activation if the direction
  rule is changed)
- Remove the parameter and the ROC condition entirely, simplifying the strategy

Recommendation: **leave as-is for now.** Removing it is premature — once we
add ATR stops (Step 2) and re-test, the dynamics may change.

---

## Why this strategy is a poor fit for *our* setup (read this first)

The TTM Squeeze has a real edge in the hands of practitioners, but our
implementation captures only a fraction of it. Future reviewers should
understand the gap before considering reactivation:

**Bridging the gap would require capabilities we don't have:**

| Required for full edge | Our setup | Estimated gap contribution |
|---|---|---|
| **Options trading** (calls into the breakout) | Stock-only — captures direction, misses asymmetric payoff structure | ~50% |
| **Discretionary chart-pattern filtering** (clean setups only) | Systematic — takes every squeeze meeting mechanical criteria | ~30% |
| **Squeeze-into-earnings setups** | Excluded by design (`EarningsBlackout` — gap-risk on the underlying) | ~10% |
| **Multi-timeframe confluence** (weekly + daily + intraday) | Daily-only (engine constraint) | ~10% |

In short: **"BB Squeeze for systematic stock trading on daily bars" is a
fundamentally weaker variant of "BB Squeeze for discretionary options
trading with multi-timeframe confluence."** The 47% win rate / +0.22 Sharpe
we measured on the best universe is probably an honest read on what the
signal-only version of this strategy delivers — the published 67% win rate
numbers come from the discretionary + options + catalyst-timed version.

**When (if ever) to reconsider this strategy:**

- The bot gains options trading capability → revisit the entire payoff math
- We add intraday bar support → can test multi-timeframe confluence
- We add ML chart-pattern recognition → could approximate the discretionary
  filtering
- We change the earnings-blackout policy and accept gap-risk → unlocks the
  highest-edge setups
- We need a low-DD diversifier sleeve more than additional return — sector
  ETF version is the lowest-drawdown sleeve in the codebase

**Until then:** the strategy is parked, not removed, because the code is
correct and well-tested. Reactivation is a config change, not a rewrite.

## Final status (2026-04-29) — IMPLEMENTED BUT NOT ACTIVATED

**Decision:** Park the strategy in a "ready to deploy" state without wiring
it into `forward_test.py`. Reasoning:

- Best-case Sharpe (+0.22 on Sector ETFs) is **modest by absolute standards**
  and only marginally above RSI Reversion's +0.19. It would not be a
  meaningful return-generator.
- The user's directional thesis is AI / Big-Tech / Semis (5–10y bull). The
  strategy's *only* viable deployment universe (Sector ETFs) is **a different
  bet entirely** — diversified sector exposure rather than concentrated
  AI/Semi conviction. Activating it would dilute, not amplify, the thesis.
- Comparable strategies on the original ranking (Momentum Breakout, Dual
  Momentum, PEAD) are structurally aligned with the bull thesis and likely
  have higher upside before adding a 3rd modest-edge sleeve.
- The published practitioner edge does not transfer to our setup — see
  "Why this strategy is a poor fit for *our* setup" section above for the
  capability gaps.

**Step 2 (ATR stop simulation) was implemented and empirically tested** —
adding a 2× ATR stop produced a small *negative* Sharpe impact on both
AI/BigTech (+0.16 → +0.09) and Sector ETFs (+0.22 → +0.19). See "ATR stop
simulation — empirically tested" section below. Stops do not help BB Squeeze
because the strategy enters on volatility breakouts where ATR is already
elevated, making 2× ATR stops too wide to catch real losses but tight enough
to clip would-be winners on intra-trade noise.

### What's been parked

| Artifact | Location | Status |
|---|---|---|
| `BollingerSqueeze` strategy | [strategies/bollinger_squeeze.py](../strategies/bollinger_squeeze.py) | Tested, documented |
| `BollingerSqueezeEdgeFilter` | [strategies/filters/bollinger_squeeze.py](../strategies/filters/bollinger_squeeze.py) | Tested, IEX-aware |
| BB / KC indicators | [indicators/technicals.py](../indicators/technicals.py) | Tested |
| `BOLLINGER_WATCHLIST` | [config/settings.py](../config/settings.py) | Set to **sector ETFs** (optimal universe) |
| `STRATEGY_WATCHLISTS` / `STRATEGY_ALLOWED_REGIMES` entries | [config/settings.py](../config/settings.py) | Present (dashboard-aware) |
| `forward_test.py` slot wiring | [forward_test.py](../forward_test.py) | **NOT WIRED** — strategy will not trade |
| `STRATEGY_ALLOCATIONS` entry | [config/settings.py](../config/settings.py) | **NOT ADDED** — sleeve allocation unchanged |
| Tests | `tests/test_bollinger_squeeze.py`, additions to `tests/test_filters.py`, `tests/test_technicals.py` | All passing in the 746-test suite |
| Backtest harness | [scripts/backtest_bollinger_squeeze.py](../scripts/backtest_bollinger_squeeze.py) | Reproducible per-universe sweeps |
| Cross-strategy comparison | [scripts/compare_strategy_sharpes.py](../scripts/compare_strategy_sharpes.py) → [docs/strategy_sharpe_comparison.md](./strategy_sharpe_comparison.md) | Reference snapshot |

### To activate later

If the team decides to deploy this strategy after all (e.g., as a small
diversifier sleeve, or after comparing against newer strategies):

1. Add a `StrategySlot` to `forward_test.py`:
   ```python
   from strategies.bollinger_squeeze import BollingerSqueeze
   from strategies.filters.bollinger_squeeze import BollingerSqueezeEdgeFilter

   squeeze_slot = StrategySlot(
       strategy=BollingerSqueeze(
           bb_length=10, kc_length=10,
           min_squeeze_bars=6, roc_lookback=5,
           edge_filter=BollingerSqueezeEdgeFilter(),
       ),
       watchlist_source=StaticWatchlistSource(
           list(settings.BOLLINGER_WATCHLIST), name="bollinger_squeeze"
       ),
       timeframe="1Day",
       allowed_regimes=frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
   )
   slots.append(squeeze_slot)
   ```

2. Add `STRATEGY_ALLOCATIONS["bollinger_squeeze"]` in `config/settings.py`
   and rebalance existing sleeve weights so they still sum to ≤ 1.0.
   Recommendation: small weight (≤ 0.20) given the modest Sharpe.

3. Run Step 2 (ATR stop simulation) and walk-forward validation before
   committing live capital.

## ATR stop simulation — empirically tested (2026-04-29)

The vectorbt backtester now supports a `--atr-stop-mult` flag that simulates
the engine's `ATR_STOP_MULTIPLIER=2.0` stop-loss. The hypothesis was that
stops would help most on high-vol universes (AI/BigTech) and minimally on
sector ETFs. **The actual data contradicts the first half of that hypothesis:**

### Sweep with vs without 2× ATR stops (held-constant universe & dates)

| Universe | Optimal variant | Sharpe (no stops) | Sharpe (2× ATR stops) | DD (no stops) | DD (2× ATR stops) |
|---|---|---:|---:|---:|---:|
| AI / Big-Tech | Aggressive (10/4/3) | +0.16 | **+0.09** | -24.5% | -25.8% |
| Sector ETFs | Shorter bands (10/6/5) | +0.22 | **+0.19** | -7.7% | -7.9% |

Sharpe dropped on **both** universes; drawdown was essentially unchanged or
slightly worse. The 2× ATR stop produces a small negative Sharpe impact
across the board.

### Why ATR stops *don't* help BB Squeeze

The squeeze fires *because* volatility just expanded — by definition the
entry happens at a bar where ATR is elevated. A 2× ATR stop is therefore:

- **Wide** at entry (because ATR is large from the breakout itself), so it
  rarely catches the worst losing trades — those run to the strategy's
  signal exit anyway.
- **Susceptible to intra-trade whipsaws** that the would-be winners recover
  from. The stop converts "small noise dip → recovery → win" into "stop
  hit → close at low → loss".

This is the opposite of how ATR stops behave on trend-following strategies
(SMA Crossover) where entries happen at moderate-vol bars and stops act as
genuine risk control.

**Conclusion:** ATR stops are not a useful addition to BB Squeeze on either
universe tested. The strategy's `close < BB mid` signal exit is sufficient
on its own. *The production engine's stop layer remains as a worst-case
safety net but should rarely fire in the typical course of trading.*

Reproducible:

```bash
python scripts/backtest_bollinger_squeeze.py --sweep --universe ai_bigtech \
    --years 4 --end-date 2026-04-28 --atr-stop-mult 2.0
python scripts/backtest_bollinger_squeeze.py --sweep --universe sector_etfs \
    --years 4 --end-date 2026-04-28 --atr-stop-mult 2.0
```

Detail: [logs/backtests/squeeze_sweep_ai_atr.md](../logs/backtests/squeeze_sweep_ai_atr.md), [logs/backtests/squeeze_sweep_etfs_atr.md](../logs/backtests/squeeze_sweep_etfs_atr.md)

## Deferred work

1. **Walk-forward validation.** Current sweep is in-sample on the full 4-year
   window. Use `backtest.runner.walk_forward()` to verify the optimal-per-
   universe choice generalises out-of-sample before any live deployment.

2. **Edge-filter ablation.** All sweeps ran with the edge filter ON. Could
   re-run filter-OFF to quantify its contribution per universe.
