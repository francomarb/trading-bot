# Allocator Risk-Target Reconciliation

**Status:** ACCEPTED 2026-07-13 (operator sign-off on the §6 targets as
proposed: donchian 0.40% / sma 0.60% / rsi 0.25%). Implemented per §7 on
`feat/allocator-risk-targets`: `STRATEGY_ALLOCATIONS[*].risk_per_trade_pct`
+ derived `STRATEGY_RISK_PER_TRADE_PCT` with import-time validation,
per-strategy sizing + binding-cap clip logging in
`RiskManager._size_position`, tests in
`tests/test_risk.py::TestPerStrategyRiskTargets`. Paper acceptance per §8
pending.
**Date:** 2026-07-13
**Origin:** Donchian trade-profile audit (2026-07-12) and the RiskManager
sizing audit that followed it (see PR #82 for the P&L-integrity fixes that
made the audit data trustworthy).

---

## 1. Problem statement

Per-trade initial risk across the equity sleeves currently ranges **$55 to
$1,900 (29×)** with no strategy logic behind the dispersion. Root cause: the
risk-per-trade control (`MAX_POSITION_PCT = 0.02`) is arithmetically
unreachable under the per-position concentration caps, so the concentration
cap sizes essentially every entry. Consequences, all measured on paper data:

1. **Dollar risk scales with volatility** — at an equal notional cap, risk at
   stop = notional × 2×ATR%. On 2026-05-01 the five Donchian entries each got
   ~$4,03x notional; AAPL (2.2% ATR) risked $188 while CIEN (6.2% ATR) risked
   $474. This is the exact outcome `capital_allocation_reference.md` §3.4
   names as the thing to avoid ("avoid oversizing volatile symbols").
2. **Freed-capital lottery** — capital released by an exit goes entirely to
   whichever signal is scanned first. ARM (2026-06-04 13:36) captured the full
   $7.4k freed by the AAPL/AMZN exits; MRVL one minute later got $570. SMCI
   got $470 at 13:42 on 06-01; QCOM got $3.7k at 19:56 the same day after the
   PWR exit freed capital.
3. **The dispersion contaminates everything downstream** — R-multiples are
   comparable but dollar P&L is sizing noise; the future Quarter-Kelly
   allocator (reference doc §5, PLAN 11.9) needs comparable per-trade risk as
   input.

Affected: `sma_crossover` (all entries bound by the global 10% notional cap;
effective risk 0.7–0.94% vs the configured 2%), `donchian_breakout` (worst:
sleeve slices + high-ATR universe), `rsi_reversion` (not yet traded; tightest
sleeve → would run at ~0.2–0.5% effective). Not affected: `credit_spread`
(max-loss sized natively), `spy_options_reversion` (single instrument →
consistent risk; deliberately small isolated vault).

## 2. How we got here (all decisions were rational; one parameter went stale)

| Stage | Commit / doc | What it did | Why |
|---|---|---|---|
| Phase 6 | `5f74d2b` | Risk-first sizing: 2% loss-to-stop, ATR stops | Equal dollar risk "regardless of the symbol's volatility" (risk/manager.py docstring) |
| 2026-04-22 | `dc65435` | 10% per-position notional cap | Gap-risk + diversification: "so 5 can fit in 50% gross". Correct — the pure 2% formula wants 25–50% of equity in one calm name, and gaps ignore stops (CIEN filled 1.15R below its stop; QCOM 0.8R below) |
| Early May | allocator v1 | Sleeves with `budget ÷ max_positions` even slices | Strategy fairness (the `per_pos=$4,043` in the 05-01 logs) |
| Paper evidence | reference doc §3.4 | **Rejected** `budget ÷ max_positions` | Position-count rejections while deployable capital sat idle (36–84 sleeve-blocked signals/week mid-May) |
| 2026-05-08 | `d9e1c72` | Capital-driven model (current) | "Allocator computes capital available; **risk manager still sizes from stop-risk first**; a separate per-position concentration cap limits how much one trade can consume; count ceiling is only a safety rail" |

The adopted architecture is four **distinct** controls. The defect is that the
risk control has been dead since 2026-04-22: at 2×ATR stops, risking 2% of
equity needs notional = `1% ÷ ATR%` of equity (45% for a 2.2%-ATR name) —
forbidden by every cap. So the binding order silently inverted: the
concentration cap, designed as an exceptional brake, became the sizer.

**This proposal does not change the architecture.** It re-derives the one
stale parameter so the documented binding order (risk sizes the trade; caps
fire exceptionally) becomes true again. The v1 slice model stays rejected;
no slice mechanism is reintroduced.

## 3. Design rule

Choose per-strategy risk targets `T` such that a risk-sized position fits
inside the per-position concentration cap for ~90% of the strategy's
watchlist (by current ATR%):

```
risk-based notional% = T / (2 × ATR%)   must be ≤ per-position cap%
⇒ T ≤ cap% × 2 × ATR%₁₀   (10th-percentile ATR of the watchlist)
```

Names calmer than the coverage point still get clipped by the concentration
cap — **visibly and exceptionally**, which is that cap's designed role
(gap-risk containment on big calm positions).

## 4. Measured watchlist volatility (SIP, ATR14/close, 2026-07-13)

| Watchlist | n | min | p10 | median | p90 |
|---|---|---|---|---|---|
| SMA | 50 | 1.3% (GSAT) | 4.0% | 7.1% | 10.5% |
| RSI | 29 | 1.6% (KBE) | 2.2% | 3.0% | 7.8% |
| Donchian | 46 | 2.6% (AAPL) | 3.6% | 8.4% | 11.8% |

Reproduce: fetch each watchlist symbol's daily SIP bars, `add_atr(d, 14)`,
take `atr_14/close` of the latest bar (see §9).

## 5. Per-position caps in force today

| Strategy | Sleeve budget (of equity) | Concentration cap | Effective per-pos cap |
|---|---|---|---|
| sma_crossover | 32% | 12.8% (14.7% stretched) | **10%** (global notional cap binds first) |
| donchian_breakout | 20% | 8.0% (9.2% stretched) | **8.0%** |
| rsi_reversion | 16% | 6.4% (7.4% stretched) | **6.4%** |

## 6. Proposed risk targets (the sign-off decision)

| Strategy | Proposed T | Coverage (risk rule binds when ATR% ≥) | Clipped names today | Worst-case sleeve heat (T × 8 slots) | Budget coherence |
|---|---|---|---|---|---|
| donchian_breakout | **0.40%** | 2.5% — full watchlist | none (AAPL 2.6% is the floor) | 3.2% | median-ATR position ≈ 2.4% notional × 8 ≈ 19% ≈ the 20% budget |
| sma_crossover | **0.60%** | 3.0% — all but GSAT | GSAT | 4.8% | median-ATR position ≈ 4.2% × 8 ≈ 34% ≈ the 32% budget |
| rsi_reversion | **0.25%** | 2.0% — all but KBE | KBE | 2.0% | median-ATR position ≈ 4.2% notional; budget supports ~4 concurrent, then waterfall priority applies as designed |

Notes:

- These are **increases in consistency, not appetite**: current realized risk
  averages ~0.3% (Donchian) and ~0.8% (SMA) — the targets formalize roughly
  today's average while killing the 29× spread around it.
- Combined worst-case same-day equity-sleeve heat ≈ 10% of equity if every
  slot is full and every stop is hit at once; the 5% daily-loss halt fires
  well before that completes. Operator may prefer lower T's for that reason —
  the table's coverage math scales linearly.
- `MAX_POSITION_PCT = 0.02` remains as the global hard ceiling / validation
  bound; the new per-strategy targets sit beneath it.
- `LIVE_SIZE_MULTIPLIER` continues to apply after sizing, unchanged.

## 7. Implementation sketch (single PR once §6 is signed off)

1. `config/settings.py`: add `risk_per_trade_pct` to `STRATEGY_ALLOCATIONS`
   entries (equity strategies only), with a validation check that each is
   `< MAX_POSITION_PCT` and that `T ≤ cap% × 2 × 0.02` documented per §3.
2. `risk/manager.py::_size_position`: use the per-strategy target when the
   signal's strategy has one; fall back to `max_position_pct` otherwise.
   No changes to the cap stack.
3. **Visibility promise:** when any notional cap clips qty below the
   risk-sized qty, log at INFO with both quantities and the binding cap name
   (the sleeve cap already logs; add the notional/gross/cash cases) — the
   concentration brake must be observable when it fires.
4. Unit tests: binding-order matrix (calm/median/wild ATR × each strategy)
   asserting risk rule binds above the coverage point and the cap clips below
   it; regression test that two same-cycle signals receive equal risk when
   capital is ample.
5. No changes to: entry signals, stops, exits, sleeve budgets, priorities,
   stretch, count rails, options/credit-spread sizing, drawdown gates.

## 8. Acceptance / verification (paper, evidence-gated per house rule)

1. After the first 6 equity entries post-deploy: `initial_risk_dollars`
   within ±10% of `T × equity` for every entry whose ATR% is above the
   coverage point; clipped entries logged with the binding cap.
2. No recurrence of the freed-capital lottery: two entries in the same cycle
   differ in risk only via the documented caps.
3. `strategy_lifecycle_counters.sleeve_blocked` does not materially exceed
   its pre-change baseline (guards against re-creating the v1 starvation
   problem this must not reintroduce).

## 9. Rejection / re-open conditions

- If paper shows repeated `SLEEVE_FULL` rejections while deployable capital
  is idle (the v1 failure mode), the targets are too high for the budgets —
  revisit §6 with the same coverage math, or revisit sleeve budgets.
- Reproduce the ATR distributions: `fetch_symbol(sym, ..., feed="sip")` per
  watchlist + `add_atr(df, 14)`, last bar `atr_14/close`. Re-run before
  changing any T — the coverage points move with market volatility.
- **The targets are coupled to the watchlists' calm end.** Re-run the ATR
  measurement whenever a watchlist changes. Drift fails soft in both
  directions: wilder watchlist → T merely becomes conservative; calmer
  additions → only those names get cap-clipped below target (risk moves
  DOWN, never disperses up), and the §7 cap-clip logging surfaces it
  organically. Routine clip logs on multiple names = the floor moved →
  re-derive. If dynamic watchlists (PLAN 11.1) ever ship, fold this
  measurement into the watchlist refresh itself.
- Related but explicitly out of scope: same-day correlated-cohort entry
  staggering (needs the 2016–2024 SIP backtest with production gates and an
  explicit signal-ranking rule; see the Donchian trade-profile findings).

## Sources

- `docs/capital_allocation_reference.md` §1, §2, §3.4 (architecture + intent)
- `risk/manager.py` module docstring (Phase 6 design principles)
- `docs/architecture.md` ("RiskManager still sizes from stop-risk first")
- Commits `5f74d2b`, `dc65435`, `d9e1c72` (parameter history)
- 2026-07-12/13 audits: Donchian trade profile, sizing dispersion, PR #82
