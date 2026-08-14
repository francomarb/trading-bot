# Donchian Channel Breakout — Strategy Research & Deployment Guide

**Status:** ✅ **ACTIVE** — wired in `forward_test.py` since 2026-05-01.
Built as the successor analysis to [bollinger_squeeze_universe_research.md](bollinger_squeeze_universe_research.md).

**Last updated:** 2026-08-12

---

## Why this strategy

After parking BollingerSqueeze (Sharpe +0.22 on sector ETFs; 50% of practitioner
edge requires options/discretionary/multi-timeframe capabilities we lack), we needed
a strategy that *captures* trend continuation in stocks that just keep making new
highs — not one that fades or waits for compression.

**DonchianBreakout** (Turtle Trading System 1, Dennis & Eckhardt 1983): buy when price
makes a new N-day high; exit when price breaks an M-day low. Pure price-strength
signal, no preprocessing, no discretion. The logic matches the AI/BigTech universe
perfectly: stocks that keep making new highs fire entry signals constantly; the N-day
low exit only triggers when the trend genuinely fails.

**Why it works here where BB Squeeze did not:**
1. Signal logic ("buy new highs") matches universe behaviour directly
2. ATR stops *help* trend-followers — empirically confirmed vs. trailing-stop tests
3. No "missing practitioner edge" — the entire Donchian edge is in price + ATR, both
   available on daily IEX bars with no capability gap

---

## Deployment configuration

| Parameter | Value |
|---|---|
| `entry_window` | 30 days |
| `exit_window` | 15 days |
| Variant name | Mid-range (30/15) |
| Order type | MARKET |
| Regime gate | `TRENDING` only — blocked in BEAR, VOLATILE, RANGING |
| Edge filter | `DonchianEdgeFilter`: stock > 200 SMA, earnings blackout (1 day before / 0 after), IEX-scaled liquidity floor |
| Sleeve weight | 0.25 of gross capital |
| Max positions | 5 concurrent |
| ATR stop | 2× ATR (engine's `ATR_STOP_MULTIPLIER`) |
| HWM drawdown gate | Live (and opt-in mature paper): entries pause if cumulative realized P&L drops >15% of sleeve budget below peak; default paper reports the breach without pausing |
| Universe | `DONCHIAN_WATCHLIST` — 32 names (see below) |

**Capital math at $100k equity:**
- Sleeve budget = $100k × 0.80 × 0.25 = **$20,000**
- Per-position notional cap = $20,000 ÷ 5 = **$4,000**
- Max simultaneous loss (all 5 stops fire) = 5 × 2% × $100k = **$10,000** — capped by the 5% daily-loss kill switch before it compounds

---

## Watchlist — generation methodology

> **This section is critical for future refreshes.** The watchlist is not
> generated from a screen — it is curated from thesis-driven categories with
> specific liquidity and history requirements. Re-read this before making changes.

### Selection criteria

A symbol is included if it satisfies **all** of the following:

1. **Thesis alignment** — the company is a direct or adjacent beneficiary of the
   AI/semiconductor/BigTech secular uptrend (see categories below).
2. **≥ 2 full years of daily bar history** on the IEX feed before the backtest
   end-date. (4-year window preferred; ≥2y acceptable for recent names.)
3. **Liquidity** — average daily notional volume > $20M consolidated ($1M on IEX
   after the 0.05× IEX scaling factor). Every name in the current list clears this
   trivially.
4. **Donchian edge-filter compatible** — stock must have ≥200 trading days of
   history for the 200-day SMA gate to function. Names below 200 bars are admitted
   but the SMA filter fails-open (no blocking).

### Exclusion rule

A symbol is excluded if it fails **any** of the following:

- IPO less than 2 years before the target backtest end-date (insufficient history
  for meaningful backtest signal).
- Penny stock or micro-cap (below $1B market cap at time of inclusion).
- SPAC or recently de-SPACed with < 2y of continuous trading history.

*Example: ALAB (Astera Labs) was considered but excluded — March 2024 IPO yields
only ~2 years inside a 4-year backtest window, making the signal statistically
thin. Reconsider in 2026 Q1.*

---

### Current watchlist (32 names, as of 2026-05-01)

#### Category 1 — AI / Semiconductors (primary, 9 names)
The core thesis: AI training and inference hardware; highest-conviction names.

| Symbol | Company | Rationale |
|---|---|---|
| NVDA | NVIDIA | AI GPU monopoly for training and inference |
| AMD | Advanced Micro Devices | GPU/CPU alternatives; data-centre CPUs |
| AVGO | Broadcom | AI networking ASICs; custom silicon for hyperscalers |
| SMCI | Super Micro Computer | AI server racks and GPU chassis |
| TSM | Taiwan Semiconductor (ADR) | Only foundry capable of leading-edge AI chips |
| MU | Micron | HBM memory for AI GPUs (irreplaceable bandwidth) |
| QCOM | Qualcomm | AI inference at the edge; Snapdragon AI platform |
| ARM | Arm Holdings | CPU architecture licensing; AI chip instruction sets |
| MRVL | Marvell Technology | Custom AI networking silicon; DPUs |

#### Category 2 — AI Infrastructure / Data-Centre Buildout (2 names)
Companies enabling hyperscaler build-out: networking switches and power delivery.

| Symbol | Company | Rationale |
|---|---|---|
| ANET | Arista Networks | Ethernet switching for AI data centres |
| VRT | Vertiv Holdings | Thermal management and power for AI racks |

#### Category 3 — Big Tech / Hyperscalers (6 names)
AI model builders and cloud providers; all operate or commission their own AI infra.

| Symbol | Company | Rationale |
|---|---|---|
| MSFT | Microsoft | OpenAI partnership; Azure AI cloud |
| AAPL | Apple | On-device inference; Apple Silicon AI roadmap |
| GOOGL | Alphabet | TPU development; Gemini AI; Google Cloud |
| META | Meta Platforms | LLaMA models; AI-first product org |
| AMZN | Amazon | AWS AI/ML services; Trainium/Inferentia chips |
| ORCL | Oracle | AI cloud infrastructure; GPU cluster hosting |

#### Category 4 — AI-Adjacent Software / Platforms (4 names)
Software companies whose core value prop is directly enabled by AI adoption.

| Symbol | Company | Rationale |
|---|---|---|
| TSLA | Tesla | AI-driven autonomy (FSD, Dojo supercomputer); robotics |
| PLTR | Palantir | AI data platform for enterprise and government |
| CRWD | CrowdStrike | AI-driven cybersecurity; endpoint protection |
| NOW | ServiceNow | AI workflow automation at enterprise scale |

#### Category 5 — AI Compute / Quantum Computing (2 names)
Early-stage AI and quantum compute infrastructure plays.

| Symbol | Company | Rationale |
|---|---|---|
| IREN | Iris Energy | Bitcoin mining → AI GPU compute data centres |
| IONQ | IonQ | Trapped-ion quantum computing; AI quantum adjacency |

#### Category 6 — AI-Adjacent (semiconductor equipment, power, networking, quantum) (9 names)
Thematically adjacent to AI capex — high correlation with the core AI names.

| Symbol | Company | Rationale |
|---|---|---|
| ASML | ASML Holding | Only EUV lithography supplier; every AI chip needs ASML |
| CLS | Celestica | Contract manufacturing for hyperscaler AI networking hardware |
| CIEN | Ciena | Optical networking; AI data-centre traffic growth |
| CEG | Constellation Energy | Nuclear power for AI data-centre electricity demand |
| VST | Vistra Energy | Power generation; same AI-electricity demand thesis as CEG |
| BE | Bloom Energy | Fuel-cell backup power for AI data centres |
| PWR | Quanta Services | Electrical infrastructure build-out for AI campuses |
| RGTI | Rigetti Computing | Quantum hardware; early-stage AI compute adjacency |
| QBTS | D-Wave Quantum | Quantum annealing; same early-stage bet as RGTI |

> ⚠️ **Note on Category 6:** These names are highly correlated with the AI core
> (Category 1) and tend to move together during sector-wide sell-offs. The HWM
> drawdown gate and max-positions cap are the primary mitigations.
> RGTI and QBTS have <4y bar history (SPAC mergers in late 2021/2022) — the
> backtest runs on available bars.

---

### Parked diversifiers (tested but not deployed)

During DD-reduction research (2026-04-30), the following 18 names were tested
in a blended 50-name universe alongside the AI core. Results showed modest DD
improvement (-4.4pp) at a meaningful Sharpe cost (-0.20). The trade-off was
judged unfavourable. These are documented here for potential future inclusion
if DD becomes a blocking concern or thesis broadens:

| Symbol | Company | Sector |
|---|---|---|
| LLY | Eli Lilly | Healthcare — GLP-1 drugs; strong secular trend |
| NVO | Novo Nordisk | Healthcare — GLP-1 drugs; European counterpart to LLY |
| UNH | UnitedHealth | Healthcare — managed care; defensive compounder |
| GMED | Globus Medical | Healthcare — surgical robotics |
| ISRG | Intuitive Surgical | Healthcare — surgical robotics; da Vinci system |
| JPM | JPMorgan Chase | Financials — largest US bank; cyclical |
| SPGI | S&P Global | Financials — data and analytics; high-quality compounder |
| MCO | Moody's | Financials — credit ratings; same thesis as SPGI |
| SOFI | SoFi Technologies | Fintech — consumer banking; high beta |
| LMT | Lockheed Martin | Defense — F-35, missile systems |
| RTX | RTX Corp | Defense — missiles, aerospace engines |
| V | Visa | Payments — global network; low-volatility compounder |
| MA | Mastercard | Payments — global network; same thesis as Visa |
| COST | Costco | Consumer — membership-driven compounder |
| HD | Home Depot | Consumer — home improvement cyclical |
| CAT | Caterpillar | Industrials — infrastructure equipment |
| ROP | Roper Technologies | Industrials — niche software compounder |
| NEE | NextEra Energy | Utilities — clean energy; AI data-centre power adjacency |

**Why they underperformed in blend:** These names are genuine diversifiers in a
portfolio sense, but Donchian's edge is fundamentally tied to *how strongly a
name trends*. The healthcare/financial/consumer names trend more slowly and with
more mean-reversion, which reduces Donchian's per-symbol Sharpe and drags the
universe aggregate.

---

### Watchlist refresh process

> Refresh annually (January) or on any of the triggers below.

**Triggers for ad-hoc refresh:**
- New AI-sector IPO reaches ≥2y of trading history and meets liquidity criteria
- Any current name drops below liquidity floor or market cap threshold
- Strategy underperforms buy-and-hold on the universe by >10pp Sharpe over a
  rolling 12-month paper window
- Major sector regime change (e.g., AI investment cycle peaks)

**Refresh procedure:**
1. Draft candidate additions/removals against the selection criteria above
2. Add candidates to the `ai_bigtech` universe in `scripts/backtest_bollinger_squeeze.py`
3. Run sweep: `python scripts/backtest_donchian_breakout.py --sweep --universe ai_bigtech --years 4 --end-date <today> --atr-stop-mult 2.0`
4. Compare Sharpe and MeanDD vs. current production universe result
5. If Sharpe ≥ current −0.05 AND MeanDD ≤ current +2pp → candidate additions are safe
6. Get explicit user approval before deploying updated watchlist
7. Update `DONCHIAN_WATCHLIST` in `config/settings.py` and the table in this doc
8. Commit, push, recycle bot

---

## Methodology

### Test setup (held constant across every run)

| Parameter | Value |
|---|---|
| Bar range | 2022-03-30 → 2026-04-27 |
| History length | 4 years (1,021+ daily bars per symbol) |
| End-date | Pinned to 2026-04-28 UTC |
| Data feed | IEX |
| Initial cash | $100,000 per symbol (standalone per-symbol simulation) |
| Slippage | 5 bps |
| Commissions | $0 |
| Edge filter | ON |
| ATR stops | 2× ATR (`sl_stop` in vectorbt) |

### Parameter sweep (5 variants, held constant across universes)

| Variant | entry_window | exit_window |
|---|---|---|
| Aggressive (10/5) | 10 | 5 |
| System 1 (20/10) | 20 | 10 |
| **Mid-range (30/15) ⭐ deployed** | **30** | **15** |
| Hybrid (55/10) | 55 | 10 |
| System 2 (55/20) | 55 | 20 |

Run command:
```bash
python scripts/backtest_donchian_breakout.py --sweep \
    --universe ai_bigtech --years 4 --end-date 2026-04-28 \
    --atr-stop-mult 2.0 --output logs/backtests/donchian_sweep_ai_bigtech_32.md
```

---

## Backtest results

### Final deployed universe: ai_bigtech 32 names (2026-05-01)

| Variant | MeanRet | MeanCAGR | Sharpe | MeanDD | Trades | WinRate |
|---|---:|---:|---:|---:|---:|---:|
| Aggressive (10/5) | +53.1% | +9.0% | +0.44 | -43.3% | 1241 | 42.0% |
| System 1 (20/10) | +133.0% | +19.6% | +0.77 | -36.4% | 619 | 47.8% |
| **Mid-range (30/15) ⭐** | **+162.9%** | **+22.9%** | **+0.85** | **-35.1%** | **457** | **47.7%** |
| Hybrid (55/10) | +108.1% | +15.8% | +0.70 | -31.5% | 462 | 49.4% |
| System 2 (55/20) | +142.4% | +20.5% | +0.79 | -34.5% | 336 | 48.8% |

Detail: [logs/backtests/donchian_sweep_ai_bigtech_32.md](../logs/backtests/donchian_sweep_ai_bigtech_32.md)

### Previous universe: ai_bigtech 23 names (original)

| Variant | Sharpe | MeanDD | Trades |
|---|---:|---:|---:|
| Mid-range (30/15) | +0.80 | -33.6% | 336 |

Adding the 9 AI-adjacent names to 32 improved Sharpe (+0.80 → +0.85) by providing
more frequent breakout signals across the broader AI theme, without diluting
universe coherence.

### Cross-universe comparison (best variant per universe)

| Universe | Optimal variant | Sharpe | Return | MeanDD | Trades |
|---|---|---:|---:|---:|---:|
| **ai_bigtech 32 ⭐** | Mid-range (30/15) | **+0.85** | **+162.9%** | -35.1% | 457 |
| sector_etfs | System 1 (20/10) | +0.53 | +19.6% | -18.1% | 241 |
| defensive_megacaps | System 1 (20/10) | +0.25 | +9.7% | -19.2% | 330 |
| reits | System 2 (55/20) | +0.08 | +6.1% | -19.9% | 109 |

### Cross-strategy comparison

| Strategy | Sharpe | MeanRet | MeanDD | Trades |
|---|---:|---:|---:|---:|
| SMA Crossover (20/50) | +0.33 | +37.3% | -20.8% | 58 |
| RSI Reversion (14, 30/70) | +0.19 | +2.7% | -9.6% | 8 |
| BB Squeeze (sector ETFs) | +0.22 | +3.5% | -7.7% | 98 |
| **Donchian (30/15, 32-name) ⭐** | **+0.85** | **+162.9%** | -35.1% | 457 |

Donchian is the highest-Sharpe strategy in the codebase by a 2.6× margin over SMA.

---

## Activation gate

| Gate | Threshold | Result |
|---|---|---|
| Sharpe on ai_bigtech | ≥ +0.4 | ✅ +0.85 |
| Trade count over 4 years | ≥ 50 | ✅ 457 |
| Beats SMA on at least one metric | Sharpe OR MeanDD | ✅ Sharpe (2.6× SMA) |
| MeanDD with ATR stops | ≤ 25% | ❌ -35.1% (structural, see below) |

**Decision:** Activate at 0.25 sleeve weight. The DD gate misses by ~10pp, but
the per-symbol averaged MeanDD overstates portfolio-level drawdown:
- In production the engine runs one $100k pool, gross exposure capped at 80%
- With 0.25 sleeve, the maximum Donchian contribution to portfolio DD ≈ 0.25 × 35% = −8.7%
- In live mode, the HWM drawdown gate adds a further backstop: if cumulative realized
  P&L drops >15% of the $20k sleeve budget ($3k) below its peak, new entries pause
  automatically. Default paper mode reports this condition without pausing entries.
- The TRENDING-only regime gate prevents new entries entirely during market downturns

---

## Capital protection layers (production)

Eight independent layers are active when Donchian runs in production:

| Layer | Mechanism | Scope |
|---|---|---|
| Regime gate | `allowed_regimes={TRENDING}` — blocks entries in BEAR, VOLATILE, RANGING | No new entries |
| HWM drawdown gate | Live / opt-in mature paper: pause entries if cumulative realized P&L >15% below sleeve peak; default paper observes only | No new entries when armed |
| ATR stop | 2× ATR below the **signal-bar close** — see caveat below | Per-trade loss cap |
| Per-position risk target | `risk_per_trade_pct=0.40%` of equity at risk per trade (11.48), beneath the `MAX_POSITION_PCT=2%` global ceiling | Per-trade sizing |
| Sleeve max positions | 8 concurrent Donchian positions maximum (`hard_max_positions`) | Concentration cap |
| Gross exposure cap | `MAX_GROSS_EXPOSURE_PCT=0.80` | Portfolio-level |
| Daily session loss cap | `MAX_DAILY_LOSS_PCT=5%` — engine halts against Alpaca prior-close when available | Portfolio-level |
| Hard dollar loss cap | `HARD_DOLLAR_LOSS_CAP=$2,000` from Alpaca prior-close when available | Emergency halt |

### Caveat: the ATR stop is not 2× ATR from the entry (PLAN 11.54)

> **✅ FIXED for new entries, 2026-08-09.** The post-fill DAY→GTC stop
> rebuild now prices the replacement at `fill − k×ATR`, using the ATR the
> engine already computes every cycle (`_last_atr`) rather than the
> decision's offset — on the production path the decision is rebuilt with
> `entry_reference_price = fill`, which makes that offset collapse to
> `fill − stop` and re-anchoring a no-op. Fails safe: any missing input
> leaves the broker's existing stop untouched.
>
> **Existing open positions were deliberately not touched** — the rebuild
> branch fires only while the live stop is DAY, and an open position already
> holds a GTC stop. The four open on 2026-08-09 keep the room shown below.
>
> Closes cost **(a)**. Cost **(b)**, under-deployment, is unchanged and
> slightly widened. The trade row is rebased to the stop actually placed,
> so `r_multiple` and the stop-repair path both read the real level.
>
> **Removes a live-vs-backtest divergence.** `backtest/runner.py` models the
> stop as `entry_price − atr_stop_mult × ATR` — anchored to the actual fill,
> always. Live was the outlier.

The rest of this section describes the behaviour **before** that fix, and
still describes the seven entries measured below.

The row above says "2× ATR below entry" because that is the design intent.
It is not what the broker order does.

The protective stop is an OTO bracket child attached when the entry is
*submitted*, priced at `signal_close − 2×ATR`. But a STOP_LIMIT entry is a
resting order that fills anywhere between its trigger and the chase cap — and
the trigger sits *below* the signal close on every observed entry (ANET 175.39
vs 181.18, AAPL 333.75 vs 336.93, WYFI 32.25 vs 37.29). So the actual distance
from entry to stop is unknown at submit time and varies per trade:

#### Stop room — seven entries, all reconciled (2026-08-09)

**Room does not require the trade to close.** It is entry price, stop price
and ATR, all of which exist the moment the entry fills. An earlier version of
this section implied the open cohort could not contribute; that was wrong, and
four of the seven rows below cost nothing to gather.

| Entry | submitted | ref close | fill | fill vs ref | 2× ATR | room | **room %** | outcome |
|---|---|---|---|---|---|---|---|---|
| AMZN | 2026-08-04 | 284.12 | 277.90 | **−2.19%** | 19.68 | 13.45 | **68.4%** | open |
| WYFI | 2026-06-17 | 35.61 | 33.85 | **−4.94%** | 6.52 | 4.76 | **73.0%** | stopped out |
| ANET | 2026-07-09 | 181.18 | 182.82 | +0.91% | 19.96 | 21.60 | 108.2% | stopped out |
| MSFT | 2026-08-07 | 500.04 | 504.20 | +0.83% | 32.65 | 36.82 | 112.8% | open |
| AVGO | 2026-08-07 | 420.68 | 426.09 | +1.29% | 33.73 | 39.14 | 116.0% | open |
| AAPL | 2026-07-28 | 336.93 | 339.54 | +0.77% | 16.19 | 18.80 | 116.1% | stopped out (gap) |
| GOOG | 2026-08-04 | 372.50 | 376.55 | +1.09% | 25.21 | 29.26 | 116.1% | open |

**Shape: five long, two short.** Five entries cluster tightly at 108–116% —
*more* room than intended — and two fall short. Only the short ones carry
premature-stop risk; the long ones carry the under-deployment cost instead.

**There is no mystery in the distribution — it is an identity.** Because the
stop is `reference − 2×ATR` and room is `fill − stop`:

```
room % = 100% + (fill − reference) / (2 × ATR)
```

So room is fully determined at fill time by how far the fill landed from the
reference close, **measured in ATR units** — not in percent. That distinction
matters: WYFI's fill was 4.94% below its reference against AMZN's 2.19%, yet
WYFI kept *more* room, because WYFI's ATR was 9.2% of its price and AMZN's was
3.5%. Ranking entries by raw percentage gap gets the ordering wrong.

**Correction — WYFI is 73.0%, not the 58.1% previously recorded here.** The
old figure does not reconcile. The method used above does: for all seven
entries, `reference − 2×ATR` reproduces the stop price the broker actually
holds **to the cent** (verified against `position_lifecycle_orders`), which is
what licenses the recomputation over the recorded value.

⚠️ **Methodology — use the SUBMIT date, not the fill date.** The stop is an OTO
bracket child attached when the entry is *submitted*, so the reference bar is
the prior completed session as of submission. WYFI submitted 2026-06-17 and
filled 2026-06-18; anchoring on the fill date gives 70.3% and fails to
reconcile by $2.39. Every other entry here submitted and filled the same day,
which is exactly why the error stayed invisible until WYFI. **This is the
fourth reference/denominator error on this workstream** — after
`reference − stop` in the original 11.53 audit, `qty × (limit − stop)`, and the
reconstructed risk budget. Check reconciliation before trusting any new
number here.

**Deployed risk — the other cost, on its own line.** The three ratios formerly
in this table (94.1% / 64.4% / 99.8%) came from a *reconstructed* budget that
PLAN `11.54` documents as unreliable; `risk_budget_dollars` was only persisted
from 2026-08-03. With the real denominator:

| Entry | Deployed risk vs **persisted** budget |
|---|---|
| GOOG 2026-08-04 | 70.5% |
| AMZN 2026-08-04 | 64.8% |
| MSFT 2026-08-07 | 66.3% |
| AVGO 2026-08-07 | 71.4% |

Consistent ~2/3 deployment in a 65–71% band — systematic, not sporadic.

**What still needs the positions to close.** Room is the *condition*; the cost
is the *consequence*, and only outcomes supply it. AMZN sitting at 68.4% says
it is more exposed to being stopped by noise. It does not say it will be, nor
that a full-room stop would have changed anything — that needs the price path
after the stop. The fix choice depends on **how often short room converts into
a lost trade that would otherwise have worked**, and that conversion rate is
what the closes are for.

**Reproduce:** entry fill and stop from `trades` / `position_lifecycle_orders`
(`role='entry_primary'`, use `created_at` for the submit date); ATR via
`indicators.technicals.add_atr` at `settings.ATR_LENGTH` on IEX daily bars
strictly before the submit date. A standing version belongs in the `11.49`
health-assessor surface per `11.54`'s own instruction — not a new consumer.

**Capital safety is not affected.** `_size_position` divides STOP_LIMIT risk by
`limit_price − stop` — the worst permitted fill (PR #62 R1 P1-3) — so the dollar
loss is bounded at or below budget regardless of where the fill lands. No
observed entry exceeded its budget.

**Strategy performance is affected, in both directions.** A fill below the
reference close leaves the stop closer than 2× ATR, so ordinary noise can end
the trade before it works — WYFI kept 73% of its intended room and was stopped
out, and AMZN is currently open on 68%. A fill well below the chase cap leaves
the position smaller than the risk target calls for — the four measured entries
deployed 65–71% of budget. On the evidence so far the two costs are **not**
equally frequent: five of seven entries landed long (108–116%) and two short,
so under-deployment is the common case and premature stopping the occasional
one. That ratio is from seven entries and should not be treated as settled.

Do **not** apply the `11.53` re-anchor here: it works from `reference − stop`,
which on AAPL would have cut deployed risk from 64.4% to 55%. `stop_for_fill`
returns STOP_LIMIT decisions unchanged for exactly this reason. Gather evidence
before choosing a fix. See PLAN `11.54`.

**Date correction (2026-08-09):** this section previously read "STOP_LIMIT only
landed 2026-07-09". That is wrong — PR #62 merged **2026-06-14** (`16bcbfb`),
and 2026-07-09 is the date of the ANET *entry*, not the ship date. The old
claim contradicted the WYFI 2026-06-18 STOP_LIMIT row in the table above it.
The correct evidence window for STOP_LIMIT behaviour opens 2026-06-14.

### Reading realized Donchian P&L: mind the configuration eras

Realized P&L for this strategy is dominated by superseded configurations, and
reading it without that inverts the conclusion. **No Donchian trade has closed
under the current configuration.**

| Entry era | Entries | Order type | Risk range | Closed P&L |
|---|---|---|---|---|
| 05-01 → 06-12 | 16 | market | $36 – $1,600 (44×) | −$1,214 |
| 06-18 → 07-09 (STOP_LIMIT, pre-fix) | 2 | stop_limit | $778 | −$873 |
| 07-28 (post-11.48 sizing) | 1 | stop_limit | $211 | −$449 (overnight gap) |
| 08-04 → 08-07 (post fill-anchored stop) | 4 | stop_limit | $269 – $295 (1.1×) | none closed |

Per-strategy risk targets landed 2026-07-13 (11.48), the fill-anchored stop
2026-08-01 (`efe0d74`), budget persistence 2026-08-03 (`b5ca503`). The entry
risk range is the clearest evidence 11.48 worked: **44× dispersion before,
1.1× after**.

Split by exit reason across all 19 closed trades, the strategy's own exit is
profitable and the protective stop carries the loss:

| Exit | n | P&L | Avg R |
|---|---|---|---|
| `exit signal` (Donchian trend exit) | 5 | **+$1,882** | **+1.17** |
| `stop_triggered` | 13 | **−$3,872** | −0.96 |

That is directional support for `11.54`'s premise, but **10 of those 13
stop-outs are pre-2026-06-14 MARKET entries** and say nothing about STOP_LIMIT
anchoring. Only WYFI, ANET and AAPL are STOP_LIMIT-era.

### 30/10 versus 30/15 paper evidence collection (started 2026-08-12)

The deployed exit remains **30/15**. To make a future exit-window review
grounded in paper evidence rather than reconstructed cache data, the engine now
emits one log-only `DONCHIAN_EXIT_OBSERVATION` record for every evaluated open
Donchian position. Each record preserves the completed-bar timestamp, close,
prior 10-day low, prior 15-day low, and the corresponding `exit_10` / `exit_15`
booleans, together with the symbol and position ID.

This instrumentation does not alter entries, exits, sizing, or broker orders;
the trade database remains the source of truth for actual fills. When enough
positions have encountered a 10-day-only exit (`exit_10=True`,
`exit_15=False`), review those records alongside the eventual actual exit/stop
and label any hypothetical 30/10 execution price as a proxy. Do not turn the
logged signal comparison into claimed realized P&L without that qualification.

---

## DD reduction experiments (tested, rejected)

Two approaches were empirically tested before settling on the protection-layer approach:

**1. Trailing ATR stops (1.5×, 2×, 3×)** — tested via `--atr-trail` flag.
Result: trailing stops *hurt* Sharpe without meaningful DD improvement. Root cause:
Donchian's N-day-low exit IS a trailing exit; layering a second trailing mechanism
creates competing exits that clip winning trades early.

**2. Universe blending (50-name universe: 23 AI + 9 AI-adjacent + 18 diversifiers)**
Result: -4.4pp DD improvement at -0.20 Sharpe cost. Root cause: the AI-adjacent
names are highly correlated with the AI core and don't provide real diversification;
the genuine diversifiers (healthcare, financials, etc.) trend more slowly and drag
per-symbol Sharpe. Detail: `logs/backtests/donchian_sweep_ai_bigtech_blend*.md`.

**Decision:** The protection-layer approach (HWM gate + regime gate + sleeve cap)
is more targeted and preserves edge; universe dilution is not worth the Sharpe cost.

---

## IEX-related limitations (revisit on SIP transition)

| Limitation | Design decision | Revisit on SIP |
|---|---|---|
| Volume-confirmation gate skipped | IEX volume ≈ 5% of consolidated tape — unreliable for gate decisions | Add volume > 1.5× avg gate; likely improves win rate |
| Liquidity threshold scaled ×0.05 | Single point of feed-conditionality in `DonchianEdgeFilter` | Drop scaler; SIP path already passes unscaled |
| No volume-weighted variants tested | IEX volume unreliable for signal construction | Re-sweep with volume-confirmation variants post-SIP |

---

## Deferred work

1. **Pyramiding** — add-to-winners per original Turtle system; requires engine multi-position-per-symbol support
2. **Walk-forward validation** — current sweep is in-sample; validate with out-of-sample splits before live capital
3. **System 2 (55/20) as a second slot** — viable on ai_bigtech (+0.79 Sharpe) and sector_etfs; consider parallel slow-trend sleeve
4. **Portfolio-level DD simulation** — current harness averages per-symbol DD; a proper joint simulation would show true portfolio DD (expected to be significantly lower)
5. **Edge-filter ablation** — quantify filter contribution by running filter-OFF sweep
6. **Sector concentration cap** — `DONCHIAN_SECTOR_GROUPS` dict + 2-per-sector limit; deferred pending user decision on static-map approach
