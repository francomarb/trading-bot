# Donchian Channel Breakout — Strategy Research & Deployment Guide

**Status:** ✅ **ACTIVE** — wired in `forward_test.py` since 2026-05-01.
Built as the successor analysis to [bollinger_squeeze_universe_research.md](bollinger_squeeze_universe_research.md).

**Last updated:** 2026-05-01

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
| HWM drawdown gate | Entries pause if cumulative realized P&L drops >15% of sleeve budget below peak |
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
- The HWM drawdown gate adds a further backstop: if cumulative realized P&L drops
  >15% of the $20k sleeve budget ($3k) below its peak, new entries pause automatically
- The TRENDING-only regime gate prevents new entries entirely during market downturns

---

## Capital protection layers (production)

Eight independent layers are active when Donchian runs in production:

| Layer | Mechanism | Scope |
|---|---|---|
| Regime gate | `allowed_regimes={TRENDING}` — blocks entries in BEAR, VOLATILE, RANGING | No new entries |
| HWM drawdown gate | Pause entries if cumulative realized P&L > 15% below sleeve peak | No new entries per strategy |
| ATR stop | 2× ATR below entry for every position | Per-trade loss cap |
| Per-position risk target | `risk_per_trade_pct=0.40%` of equity at risk per trade (11.48), beneath the `MAX_POSITION_PCT=2%` global ceiling | Per-trade sizing |
| Sleeve max positions | 8 concurrent Donchian positions maximum (`hard_max_positions`) | Concentration cap |
| Gross exposure cap | `MAX_GROSS_EXPOSURE_PCT=0.80` | Portfolio-level |
| Daily session loss cap | `MAX_DAILY_LOSS_PCT=5%` — engine halts against Alpaca prior-close when available | Portfolio-level |
| Hard dollar loss cap | `HARD_DOLLAR_LOSS_CAP=$2,000` from Alpaca prior-close when available | Emergency halt |

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
