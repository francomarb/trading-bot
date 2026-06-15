import os
from dotenv import load_dotenv

load_dotenv("config/.env")

# ── Paper vs live environment selection (Phase 10.B1) ───────────────────────
# Set LIVE_TRADING=true only after scripts/preflight.py exits 0.
# All credential and DB routing derives from this single flag.
LIVE_TRADING: bool = os.getenv("LIVE_TRADING", "false").lower() in ("true", "1", "yes")

_ALPACA_API_KEY_PAPER: str | None = os.getenv("ALPACA_API_KEY")
_ALPACA_SECRET_KEY_PAPER: str | None = os.getenv("ALPACA_SECRET_KEY")
_ALPACA_API_KEY_LIVE: str | None = os.getenv("ALPACA_API_KEY_LIVE")
_ALPACA_SECRET_KEY_LIVE: str | None = os.getenv("ALPACA_SECRET_KEY_LIVE")

ALPACA_API_KEY: str | None = _ALPACA_API_KEY_LIVE if LIVE_TRADING else _ALPACA_API_KEY_PAPER
ALPACA_SECRET_KEY: str | None = _ALPACA_SECRET_KEY_LIVE if LIVE_TRADING else _ALPACA_SECRET_KEY_PAPER
ALPACA_PAPER: bool = not LIVE_TRADING

# Data feed selection (Phase 10)
#
# Live engine on a paper account uses 'iex' (free, real-time). The paid
# Algo Trader Plus subscription ($99/mo) is required for real-time SIP, used
# only when the engine goes live with real money.
ALPACA_DATA_FEED: str = os.getenv("ALPACA_DATA_FEED", "iex").lower()

# Offline / backtest data feed. Basic Alpaca accounts can query SIP
# historical data with a 15-minute delay at no extra cost — perfect for
# backtests, calibration jobs, walkforward audits, and any analysis that
# doesn't need real-time bars. The fetcher enforces the 15-min cutoff
# automatically (see data/fetcher.py SIP end-clamp). Use this constant in
# backtest/audit scripts; the live engine continues to read
# ALPACA_DATA_FEED (= 'iex' on paper).
#
# Why this matters: SIP gives true consolidated-tape volume, so filter
# liquidity floors are interpretable in plain English. With 'iex' the
# fetcher applies a static 20x synthetic-SIP volume multiplier
# (utils.market.apply_synthetic_sip_volume), which is approximately right
# for liquid mega-caps and noticeably off for less-liquid names. Switching
# offline analysis to SIP removes the approximation.
BACKTEST_DATA_FEED: str = os.getenv("BACKTEST_DATA_FEED", "sip").lower()

# Derived base URL — used only by legacy verify scripts; alpaca-py uses the
# `paper=` flag on TradingClient directly.
ALPACA_BASE_URL = (
    "https://paper-api.alpaca.markets" if ALPACA_PAPER
    else "https://api.alpaca.markets"
)

# WebSocket connection management (Phase 11.21)
# Conservative defaults: detect dead sockets quickly, but do not churn on
# minor blips. The stream manager owns ping/pong heartbeats directly.
STREAM_HEARTBEAT_INTERVAL_SECONDS: float = float(
    os.getenv("STREAM_HEARTBEAT_INTERVAL_SECONDS", "15")
)
STREAM_HEARTBEAT_TIMEOUT_SECONDS: float = float(
    os.getenv("STREAM_HEARTBEAT_TIMEOUT_SECONDS", "10")
)
STREAM_RECONNECT_BASE_DELAY_SECONDS: float = float(
    os.getenv("STREAM_RECONNECT_BASE_DELAY_SECONDS", "1")
)
STREAM_RECONNECT_MAX_DELAY_SECONDS: float = float(
    os.getenv("STREAM_RECONNECT_MAX_DELAY_SECONDS", "30")
)

# Broker order confirmation window
# Give Alpaca enough time to stream or surface slower fills before we classify
# an order as timed out. This especially matters for fractional entries, which
# can fill in multiple chunks over more than a few seconds.
ORDER_CONFIRM_TIMEOUT_SECONDS: float = float(
    os.getenv("ORDER_CONFIRM_TIMEOUT_SECONDS", "240")
)

# Broker order confirmation window — broker-resting STOP_LIMIT entries (PLAN 11.47).
# Resting orders are EXPECTED to be unfilled at submit time (the stop arms only
# when price trades through the trigger). Polling for 240s would stall the
# serial symbol loop for every Donchian submission; the substrate's
# WebSocket / cycle reconcile / startup reconcile drains capture the eventual
# fill regardless. Keep the timeout long enough to capture the rare
# trigger-already-in-band intraday fill on submit, and short enough to release
# the cycle loop quickly.
RESTING_ENTRY_CONFIRM_TIMEOUT_SECONDS: float = float(
    os.getenv("RESTING_ENTRY_CONFIRM_TIMEOUT_SECONDS", "5")
)

# Multi-leg options entry watch window
# Credit spreads often need longer than single-leg options to fill at a fair
# net price. Give MLEG combo orders more time to work before we cancel them.
MLEG_ENTRY_WATCH_TIMEOUT_SECONDS: float = float(
    os.getenv("MLEG_ENTRY_WATCH_TIMEOUT_SECONDS", "180")
)

# ── MLEG close-walk profiles ────────────────────────────────────────────────
#
# Generic walk-and-market close logic for ANY multi-leg options strategy.
# A profile is a list of (price_expression, duration_seconds) tuples. The
# executor submits a limit at the computed price, waits ``duration``
# seconds for a fill, then cancels and advances to the next step. The
# `"market"` price expression is a sentinel — it submits a market order
# and does not wait (Alpaca fills it).
#
# Price expressions are parsed by ``utils.safe_expr`` against the
# whitelist {"mid", "bid", "ask"}. Compile-time validation runs at
# settings-load (see the validation loop below) so a typo blows up the
# bot at startup, not during a stop-loss close.
#
# Each MLEG strategy emits an exit-reason code from a fixed taxonomy when
# it decides to close a position. The engine looks up the matching
# profile and dispatches to the executor.
#
# Resolution order for picking a profile at runtime:
#   1. Per-instrument override (e.g. CREDIT_SPREAD_INSTRUMENTS["SPY"]["close_profiles"])
#   2. Per-strategy override (MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY)
#   3. Global default (MLEG_CLOSE_PROFILES — this dict)
#
# Missing exit-reason keys in any override fall through to the next
# layer, so partial overrides are supported.

MLEG_CLOSE_REASONS: frozenset[str] = frozenset({
    "profit_target",       # winner — patient close, never escalate to market
    "stop_loss",           # loss — urgent close
    "time_stop",           # DTE-driven — moderately urgent
    "defensive_breach",    # defensive emergency (e.g. short strike ITM) — fast close
})

MLEG_CLOSE_PROFILES: dict[str, list[tuple[str, int]]] = {
    "stop_loss": [
        ("mid",                   30),
        ("mid + 0.25*(ask-mid)",  30),
        ("mid + 0.50*(ask-mid)",  30),
        ("mid + 0.75*(ask-mid)",  30),
        ("ask",                   30),
        ("market",                 0),
    ],
    "time_stop": [
        ("mid",                   30),
        ("mid + 0.33*(ask-mid)",  30),
        ("mid + 0.67*(ask-mid)",  30),
        ("ask",                   30),
        ("market",                 0),
    ],
    "defensive_breach": [
        ("mid + 0.50*(ask-mid)",  30),
        ("ask",                   30),
        ("market",                 0),
    ],
    "profit_target": [
        ("mid",                   30),
        ("mid + 0.25*(ask-mid)",  30),
        ("mid + 0.50*(ask-mid)",  30),
        # No market step — winners cancel cleanly and let the strategy
        # re-evaluate next cycle. A profit target that can't capture
        # after 90s of walking probably isn't actually at target anymore.
    ],
}

# Per-strategy overrides (partial; missing reasons inherit from MLEG_CLOSE_PROFILES).
# Empty by default; add entries here when a strategy needs different pacing.
# Example:
#   MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY = {
#       "iron_condor": {
#           "stop_loss": [("mid", 45), ("ask", 30), ("market", 0)],
#       },
#   }
MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY: dict[str, dict[str, list[tuple[str, int]]]] = {}

# End-of-session bypass: if remaining seconds in the regular session is below
# this threshold when a close decision fires, skip the walk and submit market
# directly. The default (210s ≈ 3.5 min) covers the longest walk
# (stop_loss: 5 × 30s = 150s) plus a 60s safety buffer for cancel/replace
# latency and fill-confirmation roundtrip.
#
# Rationale: Alpaca's mleg orders are day-TIF only, so a partial walk active
# at 15:59 EDT may not get its remaining steps in before session close.
# Better to take the market fill at 15:56 than to leave the position open
# overnight without a working stop.
MLEG_END_OF_SESSION_BYPASS_SECONDS: int = int(
    os.getenv("MLEG_END_OF_SESSION_BYPASS_SECONDS", "210")
)


def _validate_mleg_close_profile(
    reason: str,
    steps: list[tuple[str, int]],
    *,
    context: str,
) -> None:
    """Validate a single profile at config-load time. Raises on bad config."""
    from utils.safe_expr import UnsafeExpressionError, compile_price_expression

    if not isinstance(steps, list) or not steps:
        raise ValueError(
            f"{context}: profile for reason '{reason}' must be a non-empty list"
        )
    saw_market = False
    for i, step in enumerate(steps):
        if not isinstance(step, tuple) or len(step) != 2:
            raise ValueError(
                f"{context}: profile['{reason}'][{i}] must be a (expr, duration) tuple"
            )
        expr, duration = step
        if not isinstance(expr, str) or not isinstance(duration, int):
            raise ValueError(
                f"{context}: profile['{reason}'][{i}] expected (str, int), got "
                f"({type(expr).__name__}, {type(duration).__name__})"
            )
        if duration < 0:
            raise ValueError(
                f"{context}: profile['{reason}'][{i}] duration must be non-negative"
            )
        if expr == "market":
            # Sentinel — must be the last step, and the only "market" step.
            if saw_market:
                raise ValueError(
                    f"{context}: profile['{reason}'] has multiple 'market' steps"
                )
            if i != len(steps) - 1:
                raise ValueError(
                    f"{context}: profile['{reason}'] 'market' step must be last"
                )
            saw_market = True
        else:
            # All non-sentinel steps must be parseable price expressions.
            try:
                compile_price_expression(expr, allowed={"mid", "bid", "ask"})
            except UnsafeExpressionError as exc:
                raise ValueError(
                    f"{context}: profile['{reason}'][{i}] bad expression {expr!r}: {exc}"
                ) from exc


# Validate the global profiles at module import.
for _reason, _steps in MLEG_CLOSE_PROFILES.items():
    if _reason not in MLEG_CLOSE_REASONS:
        raise ValueError(
            f"MLEG_CLOSE_PROFILES: unknown reason '{_reason}'; "
            f"must be one of {sorted(MLEG_CLOSE_REASONS)}"
        )
    _validate_mleg_close_profile(
        _reason, _steps, context="MLEG_CLOSE_PROFILES"
    )

# Validate per-strategy overrides.
for _strat, _overrides in MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY.items():
    for _reason, _steps in _overrides.items():
        if _reason not in MLEG_CLOSE_REASONS:
            raise ValueError(
                f"MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY['{_strat}']: "
                f"unknown reason '{_reason}'"
            )
        _validate_mleg_close_profile(
            _reason, _steps,
            context=f"MLEG_CLOSE_PROFILE_OVERRIDES_BY_STRATEGY['{_strat}']",
        )

# Strategy-specific watchlists
# SMA Crossover — trend-following; static list promoted from:
#   /Users/franco/trading-bot/scripts/sma_watchlist_scan.py --top 30 --feed sip
#   rule=sma_watchlist_v2, feed=sip, end_delay=60m
#   generated 2026-05-11; report: logs/sma_scan_top30.md
#
# Scanner-derived top 30 by composite score (the first 30 entries below).
# Manual additions can also come from a quick external 20/50 SMA crossover
# screen, then verified against local Alpaca daily bars and existing bot
# watchlists before inclusion. Example: DUOL was added 2026-06-04 from
# stock-screener.org/20-50-day-moving-average-crossover after this filter.
# NVDA is the lone non-scanner exception, retained as a protected open SMA
# position (RS%=40.5 at scan time — clearly weak; let the strategy exit it
# on its own signal, then remove from this list on next refresh).
SMA_WATCHLIST = [
    "SNDK", "WDC", "STX", "GSAT", "POWL", "VIAV", "VSAT", "CIEN", "ASML", "MSTR",
    "MU", "FORM", "ALB", "CSTM", "DOCN", "TTMI", "FRO", "MTZ",
    "DK", "ASX", "CAT", "HUT", "GLW", "AMD", "STRL", "INTC",
    "BE", "ECG", "MRVL", "NVT", "SQM", "TSEM", "PL", "UBER",
    "NVDA", "ADBE", "ANET", "META", "PLTR", "DUOL",
    # Scanned additions passing fundamentals check (2026-06-08)
    # MANUAL OVERRIDE: Added mid-paper-run to capitalize on the active AI/Semiconductor uptrend,
    # temporarily bypassing the 3-per-sector cap and mid-run freeze per operator direction.
    "TSM", "DELL", "LSCC", "LRCX", "NOK", "FLEX", "SANM", "ATI", "COHU", "AA",
]
# Cull deferred 2026-06-06 — an earlier audit (scripts/sma_giveback_audit.py)
# flagged VIAV, VSAT, CIEN, ALB, INTC as chronic underperformers and removed
# them. Reviewer correctly pointed out the audit used unit-share, unfiltered,
# in-sample P&L; production uses ATR-risk sizing plus regime + SPY + SMA edge
# + sector + earnings filters. The cull was reverted pending a filter-aware,
# walk-forward, OOS-validated re-audit. See sma_crossover_optimizations.md
# for the gating conditions before any cull is re-promoted.
# RSI Reversion — mean-reversion; promoted from the 2026-04-30 expanded
# backtest pass to increase signal density for the static paper-trading pool.
# This list intentionally favors breadth over the earlier narrow scanner
# snapshot so the RSI sleeve can accumulate enough trades for evaluation.
RSI_WATCHLIST = [
    "ALLY", "CDNS", "KBE", "SN", "BA", "TFC", "HON", "TMUS", "MSFT",
    "CCK", "ABNB", "PG", "SPG", "MA", "LMT", "MCD", "AAPL", "ANET",
    "CAT", "CIEN", "MCO", "AMZN", "EQIX", "RTX", "META", "HD",
    "SOFI", "ARM", "MSTR",
]
# Bollinger Squeeze (TTM-style volatility breakout) — IMPLEMENTED BUT NOT
# ACTIVE. Cross-universe research (docs/bollinger_squeeze_universe_research.md)
# concluded sector ETFs are the optimal universe (Sharpe +0.22, MeanDD -7.7%
# over 4y) — far better than the original AI/BigTech thesis which produced a
# negative Sharpe. The strategy is parked in a "ready to activate" state:
# this watchlist + the strategy code are correct for eventual deployment.
#
# To activate later: add a third StrategySlot in forward_test.py wired with
#   BollingerSqueeze(bb_length=10, kc_length=10, min_squeeze_bars=6, roc_lookback=5,
#                    edge_filter=BollingerSqueezeEdgeFilter())
# and decide a sleeve weight (the strategy is a low-DD diversifier, not a
# return generator — keep weight small).
BOLLINGER_WATCHLIST = [
    "XLF",   # Financials
    "XLE",   # Energy
    "XLU",   # Utilities
    "XLV",   # Healthcare
    "XLI",   # Industrials
    "XLK",   # Technology
    "XLP",   # Consumer Staples
    "XLY",   # Consumer Discretionary
    "XLB",   # Materials
    "XLRE",  # Real Estate
    "XLC",   # Communications
]
# Donchian Breakout (Turtle System 1) — IMPLEMENTED
# Trend-continuation strategy designed to capture relentless
# uptrends in AI / Big-Tech / Semis / NRG / Space (the user's directional thesis universe).
# Activation gate: Sharpe ≥ +0.4, ≥ 50 trades, MeanDD ≤ 25% on AI/BigTech
# backtest with 2× ATR stops. Until that gate passes, strategy is parked
# (same pattern as BollingerSqueeze).
#
# This list is the PROPOSED initial universe; user will review/edit before
# any backtest is run. See docs/donchian_breakout_strategy.md once written.
DONCHIAN_WATCHLIST = [
    # AI / Semis (primary)
    "NVDA", "AMD", "AVGO", "SMCI", "TSM", "MU", "QCOM", "ARM", "MRVL",
    # AI infrastructure / data-centre buildout
    "ANET", "VRT",
    # Big Tech
    "MSFT", "AAPL", "GOOG", "META", "AMZN", "ORCL", "TSLA",
    # AI software (secondary)
    "PLTR", "CRWD", "NOW", "ALAB", "CRWV", "NBIS",
    # AI compute / quantum (post-IPO names with full 4y history)
    "IREN", "IONQ",
    # AI-adjacent: semiconductor equipment, networking, data-centre power,
    # quantum computing — highly correlated with AI core but add breadth
    "ASML",   # Semiconductor lithography — only supplier of EUV, AI capex pick-and-shovel
    "CLS",    # Celestica — contract mfg for hyperscaler AI networking hardware
    "CIEN",   # Ciena — optical networking; direct beneficiary of AI data-centre traffic
    "CEG",    # Constellation Energy — nuclear power for AI data-centre load growth
    "VST",    # Vistra — power generation; same AI-electricity demand thesis as CEG
    "BE",     # Bloom Energy — fuel-cell backup power; AI data-centre resilience play
    "PWR",    # Quanta Services — electrical infrastructure buildout for AI campuses
    "OKLO",   # Oklo — advanced nuclear power; AI data-centre electricity demand thesis
    "SMR",    # NuScale Power — small modular reactors; AI power infrastructure thesis
    "ONDS",   # Ondas — autonomous systems and industrial wireless; AI/defense adjacency
    "PL",     # Planet Labs — satellite imagery/data platform; space and AI data adjacency
    "RGTI",   # Rigetti Computing — quantum hardware; early-stage AI compute adjacency
    "QBTS",   # D-Wave Quantum — quantum annealing; same early-stage bet as RGTI
    "RKLB",   # Development of rocket launch and control systems for the space and defense industries
    "RDW",    # Redwire — space infrastructure and defense-adjacent systems
    "ASTS",   # Space-based broadband cellular network
    # Leopold Aschenbrenner picks
    "APLD", "RIOT", "WYFI", "CORZ",
]
# Full engine universe — union of all lists; preserves paper-run continuity.
#
# IMPORTANT: When adding new symbols to any of these watchlists, remember to also
# map them to their corresponding Sector ETF in `scripts/post_mortem.py`'s
# SECTOR_MAP dictionary to ensure proper Relative Strength diagnostic reporting.
WATCHLIST = list(dict.fromkeys(
    SMA_WATCHLIST + RSI_WATCHLIST + BOLLINGER_WATCHLIST + DONCHIAN_WATCHLIST
))

# ── Per-strategy dashboard metadata ─────────────────────────────────────────
# Maps strategy_name → watchlist and allowed market regimes.
# Add a new strategy here (one entry) and the dashboard picks it up automatically.
STRATEGY_WATCHLISTS: dict[str, list[str]] = {
    "sma_crossover":      SMA_WATCHLIST,
    "rsi_reversion":      RSI_WATCHLIST,
    "bollinger_squeeze":  BOLLINGER_WATCHLIST,
    "donchian_breakout":  DONCHIAN_WATCHLIST,
    "spy_options_reversion": ["SPY"],
    # Kept in sync with CREDIT_SPREAD_INSTRUMENTS (defined later in this file);
    # a validation check below asserts the two never drift apart.
    "credit_spread": ["SPY", "QQQ"],
}
REGIME_MAX_CONSECUTIVE_FAILURES: int = 3

# ── Sector Momentum Gauge ──────────────────────────────────────────────────
# Normalized sector label → sector ETF ticker.  Used by SectorMomentumGauge
# to compute per-sector heat scores (HOT / NEUTRAL / COLD).
# Manual sector overrides — take precedence over yfinance/cache.
# Use for GICS reclassifications that don't match trading behaviour
# (e.g. Alphabet and Meta moved to Communication Services in 2018 but
# trade as technology names).
SYMBOL_SECTOR_OVERRIDES: dict[str, str] = {
    "GOOG":  "technology",
    "GOOGL": "technology",
    "META":  "technology",
    "IREN":  "technology",   # AI cloud services (Microsoft $9.7B contract); yfinance maps to financials via legacy Bitcoin mining SIC
    "AMZN":  "technology",   # AWS + AI cloud dominate revenue and valuation; GICS maps to Consumer Discretionary
    "TSLA":  "technology",   # AI, autonomous driving, software-defined vehicle; GICS maps to Consumer Discretionary
}

SECTOR_ETFS: dict[str, str] = {
    "technology":     "XLK",
    "semiconductors": "SMH",
    "financials":     "XLF",
    "energy":         "XLE",
    "utilities":      "XLU",
    "healthcare":     "XLV",
    "industrials":    "XLI",
    "staples":        "XLP",
    "discretionary":  "XLY",
    "materials":      "XLB",
    "real_estate":    "XLRE",
    "communications": "XLC",
}

# Smoothing window (in days) for sector momentum scores to prevent flip-flopping.
# 5 days provides a good balance of responsiveness and stability (reduces noise by >50%).
SECTOR_MOMENTUM_SMOOTH_WINDOW: int = 5


STRATEGY_ALLOWED_REGIMES: dict[str, set[str]] = {
    "sma_crossover":     {"TRENDING", "RANGING"},
    "rsi_reversion":     {"TRENDING", "RANGING"},
    # Squeeze fires best after compression breaks (TRENDING) or during it (RANGING).
    "bollinger_squeeze": {"TRENDING", "RANGING"},
    # Donchian whipsaws hard in RANGING regimes (every 20-day high gets faded).
    # Restrict to TRENDING only — academic literature is unanimous on this.
    "donchian_breakout": {"TRENDING"},
    "spy_options_reversion": {"TRENDING", "RANGING"},
    # Credit spreads sell premium — never in BEAR or VOLATILE (a vol spike
    # is exactly when defined-risk shorts hit max loss). See design doc §3.
    "credit_spread": {"TRENDING", "RANGING"},
}

# ── Capital allocation (Immediate allocator enhancements) ───────────────────
# The allocator works on deployable capital:
#   deployable_capital = equity × MAX_GROSS_EXPOSURE_PCT
#
# Strategy sizing remains risk-first in RiskManager. The allocator supplies
# only strategy-level ceilings:
#   - target_pct: baseline share of deployable capital
#   - pool type: shared equity pool or isolated options vault
#   - priority: lower number gets first access when capital is scarce
#   - hard_max_positions: count-based safety ceiling only
#   - max_position_pct_of_sleeve: concentration cap for one trade
#
# Example at $100k equity and 80% deployable gross:
#   deployable capital = $80,000
#   SMA target budget  = $80,000 × 0.45 = $36,000
#   40% concentration  = $14,400 max notional in one SMA position
#
# Equity strategies may stretch up to 115% of target while total deployable
# utilization remains below 80%, borrowing only from idle equity-pool capital.
CAPITAL_POOLS: dict[str, float] = {
    "equity": 0.85,
    "isolated_options": 0.15,
}

STRATEGY_ALLOCATIONS: dict[str, dict] = {
    "sma_crossover": {
        "target_pct": 0.40,
        "type": "equity",
        "priority": 3,
        "can_stretch": True,
        "hard_max_positions": 8,
        "max_position_pct_of_sleeve": 0.40,
    },
    "rsi_reversion": {
        "target_pct": 0.20,
        "type": "equity",
        "priority": 1,
        "can_stretch": True,
        "hard_max_positions": 8,
        "max_position_pct_of_sleeve": 0.40,
    },
    "donchian_breakout": {
        "target_pct": 0.25,
        "type": "equity",
        "priority": 2,
        "can_stretch": True,
        "hard_max_positions": 8,
        "max_position_pct_of_sleeve": 0.40,
    },
    "spy_options_reversion": {
        "target_pct": 0.05,
        "type": "isolated",
        "priority": 0,
        "can_stretch": False,
        "hard_max_positions": 1,
        "max_position_pct_of_sleeve": 1.00,
    },
    # Credit spread (11.29): shared sleeve across all CreditSpread instances
    # (SPY + QQQ at v1). isolated pool — defined-risk, never stretches.
    # hard_max_positions mirrors MAX_TOTAL_CONCURRENT_CREDIT_SPREADS; the
    # per-instance caps live in CREDIT_SPREAD_INSTRUMENTS. 0.10 carved from
    # SMA (0.45→0.40) and RSI (0.25→0.20); spy_options_reversion unchanged.
    "credit_spread": {
        "target_pct": 0.10,
        "type": "isolated",
        # SleeveAllocator enforces unique priorities; spy_options_reversion
        # already holds 0. Priority rarely binds inside the isolated_options
        # pool (it is 15% with the two sleeves summing to it cleanly), so
        # credit_spread takes the next free slot.
        "priority": 4,
        "can_stretch": False,
        "hard_max_positions": 8,
        "max_position_pct_of_sleeve": 0.40,
    },
}

ALLOCATOR_STRETCH_UTILIZATION_THRESHOLD = 0.80
ALLOCATOR_DEFAULT_STRETCH_PCT = 0.15
MIN_TRADE_NOTIONAL = 100.0      # Reject entries if sleeve available < this

# Strategy Health monitor (PLAN 11.10) — per-strategy minimum trade
# count before the EdgeAssessor can emit a CONCLUSIVE verdict. Below
# half this value the verdict is INSUFFICIENT; between half and the
# floor it is INDICATIVE; at or above the floor the verdict can be
# CONCLUSIVE (and the silent-killer alarm becomes possible).
#
# Calibration philosophy (re-anchored 2026-06): floors target
# "CONCLUSIVE reachable within ~6 months of live operation" against
# observed paper cadence, not "ideal sample size for statistical
# confidence." The latter framing produced floors of 30–50 that were
# unreachable inside multi-year windows for slow strategies (SMA
# closed ~17 trades/year in paper, RSI ~0–6/year because of heavy
# gating). The trade-off is explicit: lower floors weaken per-verdict
# statistical power, but they make INDICATIVE/CONCLUSIVE bands a
# usable operator signal instead of dead code. MinTRL-based rigorous
# replacement remains follow-up §F1.
#
# rsi_reversion stays the lowest (8) because its tight filters
# (SPY trend, earnings blackout, no-new-low) produce observed
# multi-month zero-trade stretches. The "RSI isn't firing" case is
# still handled by L3 Drift independently of whether Edge ever
# reaches CONCLUSIVE.
#
# See docs/strategy_health_design.md §8.
STRATEGY_MIN_TRADES_FOR_VERDICT: dict[str, int] = {
    "sma_crossover": 25,
    "rsi_reversion": 8,
    "donchian_breakout": 25,
    "spy_options_reversion": 15,
    "credit_spread": 25,
}

# Sleeve-drawdown gate — minimum closed trades a strategy must have before
# its allocator-level drawdown check (HWM−running vs target_budget × threshold)
# is allowed to halt new entries. With too few trades the check is noise,
# not signal: any single bad trade — buggy code, atypical market, ordinary
# variance — produces an indefinite lockout that doesn't reflect the
# strategy's true behavior.
#
# Motivating case (2026-06-10): the `spy_options_reversion` sleeve had
# exactly ONE round trip on record (entry 2026-05-28, exit 2026-06-05 with
# −$1,269 realized P&L). That trade was executed against the legacy fixed
# stop and the still-in-flight trailing-stop migration (PR #46 hardening
# landed 2026-06-05 but the exit fired before it). The sleeve-drawdown
# gate locked the strategy out of new entries indefinitely based on that
# single, code-bug-tainted data point. The lockout would persist until a
# real win offset the historical loss, but the gate itself prevented
# accumulating any new trades to either confirm a real drawdown or to
# work the strategy back to its HWM. Classic chicken-and-egg.
#
# Floors mirror STRATEGY_MIN_TRADES_FOR_VERDICT (above) because the
# conceptual question is identical: "how many trades do we need before we
# trust this strategy's realized P&L as a verdict?" Below the floor, the
# gate fails open (does not halt) and the daily-loss / hard-dollar cap
# kill switches remain the active line of defense.
#
# The default for strategies not in the map is high (10) to ensure new
# strategies aren't gated out on their first bad day. Adjust per-strategy
# overrides when calibration data exists.
STRATEGY_MIN_TRADES_FOR_DRAWDOWN_GATE: dict[str, int] = {
    "sma_crossover": 25,
    "rsi_reversion": 8,
    "donchian_breakout": 25,
    "spy_options_reversion": 15,
    "credit_spread": 25,
}
STRATEGY_DEFAULT_MIN_TRADES_FOR_DRAWDOWN_GATE: int = 10

# Catastrophic drawdown threshold (PR #56 R1) — even below the min-trades
# floor, sample size MUST NOT disable protection entirely. A second-tier
# threshold gates against catastrophic loss while the strategy is still
# in its "we don't have enough sample to evaluate normally" window.
#
# Two-tier semantics in SleeveAllocator.is_strategy_in_drawdown:
#   - trade_count <  floor: gate fires at  STRATEGY_CATASTROPHIC_DRAWDOWN_THRESHOLD
#                           × target_budget (default: 35%)
#   - trade_count >= floor: gate fires at  dd_threshold × target_budget
#                           (the configured normal threshold, e.g. 15%)
#
# The catastrophic level is intentionally generous — it should NOT fire on
# ordinary single-trade variance, but it MUST fire on a 35%+ sleeve loss
# (which on the spy_options_reversion case would have been ~-$3,500+ on a
# ~$10k target budget — clearly beyond "noise from one bad trade").
STRATEGY_CATASTROPHIC_DRAWDOWN_THRESHOLD: float = 0.35

# Strategy Health monitor (PLAN 11.10f) — feature flag for the
# engine's lifecycle-counter emissions. Defaults True (ship with
# observability on) but the operator can flip to False as an instant
# revert if any unexpected behavior is observed. The flag gates the
# ENTIRE counter emission/flush path — when disabled, no DB writes
# happen and the trading loop behavior is bit-identical to pre-11.10f.
#
# Per the v1 invariant (design §1.2), counter writes are observability
# only and wrapped in try/except: a write failure logs a warning and
# continues; it never raises into the trading loop. The flag is
# additional belt-and-suspenders defense for instant revert without
# code changes.
#
# Removal target: after 4-8 weeks of clean paper operation, the flag
# is removed in a follow-up PR. Tracked in the design doc as
# intentionally temporary scaffolding.
HEALTH_COUNTERS_ENABLED = True

# Strategy-level high-water-mark drawdown gate (SleeveAllocator).
# If a strategy's cumulative realized P&L drops more than this fraction
# below its peak (HWM), new entries for that strategy are paused until
# P&L recovers. Set to 0.0 to disable. Exits are never blocked.
#   Example at $100k equity, Donchian weight 0.25:
#   sleeve budget = $100k × 0.80 × 0.25 = $20k
#   gate fires when realized PnL < HWM − 0.15 × $20k = HWM − $3k
STRATEGY_SLEEVE_DD_THRESHOLD = 0.15

# ── Credit spread strategy (Phase 11.29) ─────────────────────────────────────
# Per-instrument config for the underlying-agnostic bull put credit spread
# strategy. v1 ships SPY + QQQ (both ETFs, both use VIX as the IV proxy).
# IWM, single names, and leveraged ETFs are deferred — see
# docs/credit_spread_strategy.md §15.
#
# Strategy LOGIC is hardcoded; only the thresholds below are configurable.
# Every instrument block must define all _REQUIRED_CREDIT_SPREAD_KEYS — a
# missing key fails loudly at import (see the validation loop below) rather
# than silently at first trade.
#
# min_credit_pct_of_width is 0.13, not the design doc's 0.25 default: the
# 11.28 merge gate showed real ~17Δ $10-wide SPY put spreads collect only
# ~13–15% of width. 0.25 would reject nearly every spread. Revisit during
# the paper-watch follow-up.
CREDIT_SPREAD_INSTRUMENTS: dict[str, dict] = {
    "SPY": {
        # Entry
        "short_leg_delta": 0.17,
        "spread_width": 10,
        "dte_min": 30,
        "dte_max": 45,
        "iv_proxy_source": "vix",
        "min_iv_proxy": 14,                 # VIX index points
        "min_credit_pct_of_width": 0.13,
        # Position management
        "max_concurrent_positions": 3,
        "max_per_expiration": 1,
        "min_dte_gap_between_opens": 7,
        # Exits
        "profit_target_pct": 0.50,
        "stop_loss_multiple": 2.0,
        "time_stop_dte": 21,
        "exit_on_short_strike_breach": True,
        "limit_timeout_seconds": 30,
        # Earnings (ETF — no earnings; meaningful only for single names)
        "earnings_blackout_days": 0,
    },
    "QQQ": {
        "short_leg_delta": 0.17,
        "spread_width": 15,                 # higher price → wider strikes
        "dte_min": 30,
        "dte_max": 45,
        "iv_proxy_source": "vix",           # QQQ tracks SPX closely
        "min_iv_proxy": 14,
        "min_credit_pct_of_width": 0.13,
        "max_concurrent_positions": 3,
        "max_per_expiration": 1,
        "min_dte_gap_between_opens": 7,
        "profit_target_pct": 0.50,
        "stop_loss_multiple": 2.0,
        "time_stop_dte": 21,
        "exit_on_short_strike_breach": True,
        "limit_timeout_seconds": 30,
        "earnings_blackout_days": 0,
    },
}

# Every CREDIT_SPREAD_INSTRUMENTS block must define exactly these keys.
_REQUIRED_CREDIT_SPREAD_KEYS: frozenset[str] = frozenset({
    "short_leg_delta", "spread_width", "dte_min", "dte_max",
    "iv_proxy_source", "min_iv_proxy", "min_credit_pct_of_width",
    "max_concurrent_positions", "max_per_expiration", "min_dte_gap_between_opens",
    "profit_target_pct", "stop_loss_multiple", "time_stop_dte",
    "exit_on_short_strike_breach", "limit_timeout_seconds", "earnings_blackout_days",
})

for _cs_symbol, _cs_cfg in CREDIT_SPREAD_INSTRUMENTS.items():
    _cs_missing = _REQUIRED_CREDIT_SPREAD_KEYS - _cs_cfg.keys()
    _cs_extra = _cs_cfg.keys() - _REQUIRED_CREDIT_SPREAD_KEYS
    if _cs_missing:
        raise ValueError(
            f"CREDIT_SPREAD_INSTRUMENTS['{_cs_symbol}'] is missing required "
            f"key(s): {sorted(_cs_missing)}"
        )
    if _cs_extra:
        raise ValueError(
            f"CREDIT_SPREAD_INSTRUMENTS['{_cs_symbol}'] has unknown key(s): "
            f"{sorted(_cs_extra)}"
        )

# STRATEGY_WATCHLISTS["credit_spread"] is hardcoded above (it precedes this
# block in the file); assert it never drifts from CREDIT_SPREAD_INSTRUMENTS.
if set(STRATEGY_WATCHLISTS["credit_spread"]) != set(CREDIT_SPREAD_INSTRUMENTS):
    raise ValueError(
        "STRATEGY_WATCHLISTS['credit_spread'] "
        f"{sorted(STRATEGY_WATCHLISTS['credit_spread'])} does not match "
        f"CREDIT_SPREAD_INSTRUMENTS keys {sorted(CREDIT_SPREAD_INSTRUMENTS)}"
    )

# Shared sleeve: all credit-spread instances draw from one budget. The
# allocator wiring (STRATEGY_ALLOCATIONS entry, pool rebalance) lands with
# the engine integration in PR 3b — these constants are config-only here.
CREDIT_SPREAD_SLEEVE_BUDGET_PCT = 0.10
# Global cap across ALL credit-spread instances combined — the safety net
# for a correlated drawdown where every instrument's own cap is full.
MAX_TOTAL_CONCURRENT_CREDIT_SPREADS = 8

# Composite weights for utils.options_ranker.rank_put_spread_candidates.
# Mirrors the ranker module's defaults; here so the values are reviewable
# alongside the rest of the credit-spread config.
CREDIT_SPREAD_RANKER_WEIGHTS: dict[str, float] = {
    "delta": 0.40,
    "credit": 0.30,
    "spread_quality": 0.20,
    "dte": 0.10,
}

# ── Risk settings (Phase 6) ──────────────────────────────────────────────────
# Position sizing
MAX_POSITION_PCT = 0.02         # Risk no more than 2% of equity per trade (loss-to-stop)
MAX_POSITION_NOTIONAL_PCT = 0.10 # Global per-position cap; allocator adds strategy-level caps
MAX_OPEN_POSITIONS = 30         # Global cap; per-strategy limit enforced by sleeve
MAX_GROSS_EXPOSURE_PCT = 0.80   # 80% of equity tradeable across all strategies

# Stop-loss
ATR_STOP_MULTIPLIER = 2.0       # Stop = entry - k * ATR (long); always defined pre-entry

# Daily / hard kill switches
MAX_DAILY_LOSS_PCT = 0.05       # Halt for the session if equity down 5% from session start
HARD_DOLLAR_LOSS_CAP = 2_000.0  # Absolute $ loss cap from session start; CRITICAL halt

# Loss-streak cooldown (per strategy)
LOSS_STREAK_THRESHOLD = 3       # Disable strategy after N consecutive losing trades
LOSS_STREAK_COOLDOWN_HOURS = 24 # ... for this many hours

# Broker-error-streak kill switch
BROKER_ERROR_STREAK_THRESHOLD = 5    # Halt all trading if N broker errors ...
BROKER_ERROR_WINDOW_SECONDS = 300    # ... within this rolling window (5 min)

# Slippage-drift kill switch
SLIPPAGE_DRIFT_MIN_SAMPLES = 10      # Need at least this many fills before judging
SLIPPAGE_DRIFT_MULTIPLIER = 3.0      # Halt if mean realized slippage > k * mean modeled
# Enable the slippage-drift kill switch. Disabled by default during paper trading
# because modeled slippage has not yet been calibrated against real fills.
# Enable once you have enough paper fills to validate the threshold (≥ min_samples).
# Must be True before going live (Phase 10).
SLIPPAGE_DRIFT_ENABLED = False
# Expected execution cost for MARKET orders in bps. Matches the backtest default
# (runner.py slippage_bps=5). LIMIT orders model 0 bps (price is controlled).
SLIPPAGE_MODEL_MARKET_BPS = 5.0

# ── Live-trading safety overrides (Phase 10.G) ──────────────────────────────
# Scale calculated position sizes to this fraction when LIVE_TRADING=True.
# Default 0.25 = start live at 25% of the paper-tested size.
# Raise to 1.0 once you are confident in live execution quality.
LIVE_SIZE_MULTIPLIER: float = float(os.getenv("LIVE_SIZE_MULTIPLIER", "0.25"))

# Dry-run mode: log order decisions but do not submit to the broker.
# Useful for verifying the live environment before committing real capital.
# Set DRY_RUN=true in the environment; never rely on code-level default alone.
DRY_RUN: bool = os.getenv("DRY_RUN", "false").lower() in ("true", "1", "yes")

# Fractional share sizing (Phase 10.G6).
# When True: market orders use round(notional/price, 2) instead of floor(),
# and the broker submits a DAY entry + standalone GTC stop (floor(qty) whole
# shares). When False: exact current behaviour — floor() everywhere, OTO GTC
# entry+stop submitted atomically. Disable once account exceeds ~$10k and
# whole-share rounding error becomes negligible.
# Applies to MARKET orders only — LIMIT/GTC orders (RSI) always use floor().
FRACTIONAL_ENABLED: bool = True

# ── Engine settings (Phase 8 / 10) ──────────────────────────────────────────
ATR_LENGTH = 14                     # ATR window the engine uses for stops
ENGINE_TIMEFRAME = "1Day"           # Bar timeframe for the live loop
ENGINE_HISTORY_LOOKBACK_DAYS = 300  # Calendar days of stock history per cycle.
                                    # Keep this at >= 300 for 1Day live trading:
                                    # 300 cd ≈ 206 trading days, which safely
                                    # warms up stock/SPY 200-day SMA filters.
                                    # Do not reduce casually — this is a
                                    # central engine invariant, not something
                                    # individual filters should have to enforce.
ENGINE_CYCLE_INTERVAL_SECONDS = 300 # 5 min between cycles for daily strategy
ENGINE_MAX_BAR_AGE_MULTIPLIER = 4.0 # Stale guard: refuse to trade if last bar
                                    # is older than (bar_interval × multiplier)
ENGINE_MARKET_HOURS_ONLY = True     # Only trade during regular session
# Preserve OTO stop-loss legs across bot restarts. Manual liquidation paths
# When True, the engine will read broker.get_open_orders() on stop() and
# explicitly cancel sibling orders before closing a position.
ENGINE_CANCEL_ORDERS_ON_SHUTDOWN = False

# Maximum age in seconds for an unfilled entry LIMIT order before it is
# considered stale and canceled by the engine. Default is 24 hours.
STALE_LIMIT_MAX_AGE_SECONDS: int = int(os.getenv("STALE_LIMIT_MAX_AGE_SECONDS", 86400))
# Consecutive cycles a managed position must be absent from the broker before
# it is declared externally closed (stop-out / manual liquidation). Protects
# against transient API blips that return incomplete position data.
# With WebSocket streaming (Phase 10), this becomes a fallback for gap periods.
ENGINE_EXTERNAL_CLOSE_CONFIRM_CYCLES = 3

if ENGINE_TIMEFRAME == "1Day" and ENGINE_HISTORY_LOOKBACK_DAYS < 300:
    raise ValueError(
        "ENGINE_HISTORY_LOOKBACK_DAYS must be >= 300 when ENGINE_TIMEFRAME='1Day' "
        "to keep 200-day SMA-based filters warmed up safely"
    )

# ── Entry price caps (PLAN 11.32) ────────────────────────────────────────────
# Per-strategy worst-case fill ceiling for MARKET entries. When a policy is
# set, the engine converts the market entry to a marketable DAY LIMIT + OTO
# at min(reference + bps_cap, reference + atr_fraction * ATR). The exchange
# enforces the limit — fills above the cap are impossible.
#
# Why this exists: the 2026-05-11 QCOM Donchian incident filled a MARKET BUY
# at +1205 bps from the signal close. Sizing and ATR stop had been derived
# from the signal close. See PLAN 11.32 and scripts/donchian_chase_distribution.py
# for the calibration analysis (1326 ai_bigtech signals, 12 months).
#
# Donchian-only in v1. SMA and other MARKET strategies stay uncapped until
# paper observation validates the gate.
#
# Cap interpretation: tighter of the two knobs wins. Setting only one is fine.
from execution.entry_guard import EntryPriceCap  # noqa: E402

ENTRY_PRICE_CAPS: dict[str, EntryPriceCap] = {
    "donchian_breakout": EntryPriceCap(
        # 500 bps blocks ~2.3% of historical signals; 2.0 ATR catches the
        # high-vol low-price names (QBTS, IREN) that slip past the bps gate.
        # Both together kill 100% of the top-5 historical outliers including
        # the QCOM-class incident. Tighten on observed paper data.
        max_chase_bps=500,
        max_chase_atr_fraction=2.0,
    ),
}

# ── Reporting settings (Phase 9) ────────────────────────────────────────────
TRADE_LOG_CSV = "logs/trades.csv"           # Legacy CSV trade log (deprecated)
TRADE_LOG_DB_PAPER = "data/trades.db"       # Paper-trading SQLite log
TRADE_LOG_DB_LIVE = "data/trades_live.db"   # Live-trading SQLite log (separate to prevent cross-contamination)
TRADE_LOG_DB = TRADE_LOG_DB_LIVE if LIVE_TRADING else TRADE_LOG_DB_PAPER
DAILY_PNL_DIR = "logs/daily_pnl"            # Daily P&L markdown summaries
WEEKLY_REPORT_DIR = "logs/weekly_reports"    # Weekly summary markdowns
JSON_LOG_FILE = "logs/bot.jsonl"            # Structured JSON log sink
ALERT_LOG_FILE = "logs/alerts.log"          # Dedicated alert log file

# ── Forward-test settings (Phase 10) ───────────────────────────────────────
FORWARD_TEST_DIR = "logs/forward_tests"          # Go/no-go decision docs
# Divergence gate: if |paper_return - backtest_return| exceeds this, no-go.
FORWARD_TEST_RETURN_DIVERGENCE_PCT = 0.10        # 10 percentage points
# Slippage gate: if mean adverse slippage exceeds this, no-go. Signed
# price-improvement rows count as 0 adverse bps for this reconciliation gate.
FORWARD_TEST_MAX_SLIPPAGE_BPS = 20.0             # 20 bps mean adverse

# ── Telegram / messaging (Phase 11.13) ──────────────────────────────────────
# Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config/.env to enable.
# Leave blank (default) to disable Telegram entirely — bot runs fine without it.
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
# Set TELEGRAM_COMMANDS_ENABLED=true to enable /status and /halt commands.
# Requires TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID to be set.
TELEGRAM_COMMANDS_ENABLED: bool = (
    os.getenv("TELEGRAM_COMMANDS_ENABLED", "false").lower() in ("true", "1", "yes")
)

# ── Dashboard (Phase 11.14) ──────────────────────────────────────────────────
# Path where the engine writes a JSON state snapshot each cycle.
# The Streamlit dashboard reads this file to show live bot status.
STATE_SNAPSHOT_PATH: str = "data/engine_state.json"
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8501"))

# ── Operator controls (Phase A — PR-2) ──────────────────────────────────────
# Operator command queue + sticky halt. See docs/operator_controls_proposal.md
# §13 Phase A and the operator-controls implementation plan.
#
# OPERATOR_COMMAND_EXPIRY_SECONDS: how old a `pending` operator command may
#   be before the engine rejects it on pickup with status='rejected_expired'.
#   Defensive: an old command is operator intent that may no longer match
#   current state, so a stale halt or future destructive command should not
#   auto-fire.
#
#   PR-2 reviewer fix F1: in Phase A the engine polls once per main cycle
#   (every CYCLE_INTERVAL_SECONDS = 300s). The expiry must be safely
#   greater than one cycle interval plus jitter, otherwise a halt queued
#   right after a poll will get rejected_expired before the next poll
#   sees it. 1800s gives the bot up to 5 missed cycles (or a brief
#   restart) to drain the queue before any operator command auto-expires.
#   Phase B will add a fast heartbeat (~5s) and this value can drop
#   back to 180-300s at that point.
#
# OPERATOR_COMMAND_HEARTBEAT_SECONDS: target poll cadence for processing the
#   queue. Defined here for Phase B's fast heartbeat thread; Phase A still
#   polls once per main engine cycle (5 min) — see the F1 note above.
#
# OPERATOR_CONTROL_STATE_PATH: durable record of sticky halt state. Read at
#   engine startup so a halt issued before a restart re-engages immediately.
#   Lives outside the SQLite DB so corruption in one file does not lock out
#   the other.
#
# LIFECYCLE_PENDING_GRACE_SECONDS: PR-2 gap 2 fix. The reverse-reconcile
#   pass of `_reconcile_position_lifecycle` skips `pending` lifecycle rows
#   younger than this — gives an in-flight entry submitted just before a
#   bot restart time to either confirm at the broker or transition via
#   `_lifecycle_mark_filled`. Older pending rows are still closed (almost
#   certainly orphaned from a crashed submit). 5 min is longer than the
#   longest fill-confirm window (BROKER_ORDER_CONFIRM_WINDOW_SECONDS).
OPERATOR_COMMAND_EXPIRY_SECONDS: int = 1800  # 30 minutes (PR-2 F1 fix)
OPERATOR_COMMAND_HEARTBEAT_SECONDS: int = 5
OPERATOR_CONTROL_STATE_PATH: str = "data/operator_control_state.json"
LIFECYCLE_PENDING_GRACE_SECONDS: int = 300
