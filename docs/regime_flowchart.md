# Regime Detection — Architecture & Audit

## 1. Regime Classification (`regime/detector.py`)

The `RegimeDetector._classify()` method processes SPY daily bars through a
priority-ordered decision tree. The first matching condition wins.

```mermaid
flowchart TD
    START([SPY OHLCV bars]) --> BEAR{SPY close < SMA 200?}

    BEAR -- Yes --> BEAR_OUT[/BEAR\nAll new longs blocked/]
    BEAR -- No --> VOL{ATR% >= 80th percentile\nof trailing 126 bars?}

    VOL -- Yes --> VOL_OUT[/VOLATILE\nAll new entries blocked/]
    VOL -- No --> ADX_HIGH{ADX 14 >= 25?}

    ADX_HIGH -- Yes --> TREND_OUT[/TRENDING\nTrend-following favoured/]
    ADX_HIGH -- No --> ADX_LOW{ADX 14 <= 20?}

    ADX_LOW -- Yes --> RANGE_OUT[/RANGING\nMean-reversion favoured/]
    ADX_LOW -- No --> SLOPE{SMA 50 slope > 0?\n5-bar lookback}

    SLOPE -- Yes --> TREND_OUT2[/TRENDING\nAmbiguous zone — positive slope/]
    SLOPE -- No/NaN --> RANGE_OUT2[/RANGING\nAmbiguous zone — flat or negative/]

    style BEAR_OUT fill:#f44,color:#fff
    style VOL_OUT fill:#f80,color:#fff
    style TREND_OUT fill:#4a4,color:#fff
    style TREND_OUT2 fill:#4a4,color:#fff
    style RANGE_OUT fill:#48f,color:#fff
    style RANGE_OUT2 fill:#48f,color:#fff
```

**Defaults:** ADX trend = 25, ADX range = 20, volatility percentile = 80th,
volatility window = 126 bars, SMA slope lookback = 5 bars.

---

## 2. Engine Integration (`engine/trader.py`)

The engine calls `RegimeDetector.detect()` once per cycle, then gates each
strategy slot's new entries on the result.

```mermaid
flowchart TD
    CYCLE([Engine cycle start]) --> DETECT[RegimeDetector.detect]
    DETECT --> CACHE{TTL cache fresh?}
    CACHE -- Yes --> USE_CACHED[Return cached regime]
    CACHE -- No --> FETCH[Fetch 300d SPY bars]
    FETCH --> CLASSIFY[_classify — decision tree above]
    CLASSIFY --> REGIME_RESULT[MarketRegime enum]
    USE_CACHED --> REGIME_RESULT

    REGIME_RESULT --> SHIFT{Regime changed\nsince last cycle?}
    SHIFT -- Yes --> ALERT[AlertDispatcher.regime_shift]
    SHIFT -- No --> SLOTS
    ALERT --> SLOTS

    SLOTS([For each StrategySlot]) --> GATE{current_regime\nin slot.allowed_regimes?}
    GATE -- Yes --> ENTRY_OK[entry_allowed = True]
    GATE -- No --> ENTRY_BLOCKED[entry_allowed = False\nLog: entries blocked]

    ENTRY_OK --> SYMBOLS
    ENTRY_BLOCKED --> SYMBOLS

    SYMBOLS([For each symbol]) --> SIGNALS[strategy.generate_signals]
    SIGNALS --> EXIT_CHECK{Exit signal?}
    EXIT_CHECK -- Yes --> EXIT[Execute exit\nNEVER blocked by regime]
    EXIT_CHECK -- No --> ENTRY_CHECK{Entry signal\nAND entry_allowed?}
    ENTRY_CHECK -- Yes --> SLEEVE[SleeveAllocator.check]
    ENTRY_CHECK -- No --> SKIP[Skip — blocked or no signal]
    SLEEVE --> RISK[RiskManager.evaluate]
    RISK --> ORDER[Place order]

    style ENTRY_BLOCKED fill:#f44,color:#fff
    style EXIT fill:#4a4,color:#fff
    style SKIP fill:#888,color:#fff
```

**Key rule:** Exits are never blocked by regime. Only new entries are gated.

---

## 3. Detection Failure Path

When `detect()` raises an exception, the engine uses a graduated fallback:

```mermaid
flowchart TD
    DETECT_CALL[detect raises Exception] --> INC[regime_fail_count += 1]
    INC --> CHECK{fail_count >=\nMAX_CONSECUTIVE_FAILURES?}

    CHECK -- Yes --> BEAR[Fall back to BEAR\nLog ERROR\nAll entries blocked]
    CHECK -- No --> PRIOR{Last known regime\nexists?}

    PRIOR -- Yes --> REUSE[Use last known regime\nLog WARNING]
    PRIOR -- No --> RANGING[Default to RANGING\nLog WARNING]

    DETECT_OK[detect succeeds] --> RESET[regime_fail_count = 0]

    style BEAR fill:#f44,color:#fff
    style REUSE fill:#fa0,color:#fff
    style RANGING fill:#48f,color:#fff
    style RESET fill:#4a4,color:#fff
```

**Default threshold:** 3 consecutive failures before BEAR lockdown
(`settings.REGIME_MAX_CONSECUTIVE_FAILURES`).

---

## 4. Regime Consumers

| Module | Uses Regime? | How |
|--------|:---:|-----|
| `regime/detector.py` | — | Computes regime from SPY data |
| `engine/trader.py` | Yes | Fetches once per cycle; gates entries per slot |
| `strategies/base.py` | Config only | `StrategySlot.allowed_regimes` declares permitted regimes |
| `risk/manager.py` | No | Independent risk gates applied downstream |
| `risk/allocator.py` | No | HWM drawdown gate independent of regime |

---

## 5. Current Slot Configuration

| Strategy | Allowed Regimes | Blocked By |
|----------|----------------|------------|
| SMA Crossover | TRENDING, RANGING | BEAR, VOLATILE |
| RSI Reversion | TRENDING, RANGING | BEAR, VOLATILE |
| Donchian Breakout | TRENDING | RANGING, BEAR, VOLATILE |

---

## 6. Audit Findings (2026-05-01)

| # | Issue | Severity | Status |
|---|-------|----------|--------|
| 1 | Detection failure bypassed all gating (`current_regime = None` skipped the gate) | High | **Fixed** — graduated fallback: last known → BEAR after N failures |
| 2 | ADX ambiguous zone (20–25) uses 5-bar SMA(50) slope — fragile to noise | Low | Documented. Acceptable for current risk profile. |
| 3 | Config strings uppercase (`"TRENDING"`) vs enum lowercase (`"trending"`) | Low | Documented. Not compared in code today. |
| 4 | Duplicate SPY fetching (RegimeDetector + SPYTrendFilter) | Low | Documented. Future optimization candidate. |
