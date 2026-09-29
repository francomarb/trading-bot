#!/usr/bin/env python3
"""Report-only durable-pool selector for SMA Crossover (PLAN 11.75).

Membership uses durable company and execution properties. Current trend state,
historical 20/50 crossovers, FCF, revenue growth, sector, and volatility are
diagnostics only. The script never edits the active watchlist.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings
from indicators.technicals import add_atr, add_sma
from scripts.sma_watchlist_scan import (
    AssetInfo,
    _fmt_dollars,
    configure_logging,
    fetch_daily_bars,
    get_tradable_assets,
)
from scripts.watchlist_review import (
    CheckProfile,
    SymbolFundamentals,
    assess_fitness,
    fetch_fundamentals,
)


RULE_VERSION = settings.SMA_WATCHLIST_RULE_VERSION
DEFAULT_POOL_SIZES = (25, 50, 100, 200)
DEFAULT_PROMOTION_SIZE = settings.SMA_TARGET_POOL_SIZE
EXCLUDED_SHARE_CLASSES: dict[str, str] = {"GOOGL": "GOOG"}
DURABLE_PROFILE = CheckProfile(
    strategy_name="sma_crossover",
    display_name="SMA Crossover durable pool",
    fcf_required=False,
    revenue_required=False,
    min_cash_runway_months=12,
)


@dataclass(frozen=True)
class ScanConfig:
    """Durable membership thresholds."""

    min_bars: int = settings.DURABLE_WATCHLIST_MIN_BARS
    min_market_cap: float = settings.DURABLE_WATCHLIST_MIN_MARKET_CAP
    min_price: float = settings.DURABLE_WATCHLIST_MIN_PRICE
    min_avg_dollar_volume_50: float = (
        settings.DURABLE_WATCHLIST_MIN_AVG_DOLLAR_VOLUME_50
    )
    atr_window: int = settings.ATR_LENGTH


@dataclass
class Candidate:
    """Eligible company plus non-decision diagnostics."""

    symbol: str
    name: str
    exchange: str
    close: float
    avg_dollar_volume_50: float
    market_cap: float | None
    sma50: float
    sma150: float
    sma200: float
    atr_pct: float
    high_52w_ratio: float
    momentum_12m_skip_1m: float
    crossover_dates_252: tuple[str, ...]
    fcf_ok: bool | None
    revenue_ok: bool | None
    in_ranked_pool: bool = True
    protected_for_promotion: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def crossover_events_252(self) -> int:
        return len(self.crossover_dates_252)


REJECTION_LABELS: dict[str, str] = {
    "insufficient_or_bad_bars": "Not enough clean daily bars for durable review.",
    "price": "Latest completed-session close is below the minimum.",
    "dollar_volume": "50-session average dollar volume is below the minimum.",
    "market_cap": "Market capitalization is below the minimum.",
    "market_cap_unknown": "Market capitalization could not be established.",
    "fundamentals_error": "The fundamentals provider request failed.",
    "solvency_unknown": "Profitability or required cash data was unavailable.",
    "solvency": "Known cash runway is below the durable-company minimum.",
    "nonpreferred_share_class": "Use GOOG for Alphabet exposure, never GOOGL.",
}


def get_open_sma_positions(db_path: str) -> set[str]:
    """Return SMA-owned symbols, failing closed when ownership is unavailable."""
    if not Path(db_path).exists():
        raise FileNotFoundError(
            f"trade database does not exist: {db_path}; pass "
            "--ignore-open-positions only for a sanitized research report"
        )
    try:
        from reporting.logger import TradeLogger

        trade_logger = TradeLogger(path=db_path)
        try:
            owners = trade_logger.read_all_open_owners()
        finally:
            trade_logger.close()
        return {
            symbol.upper()
            for symbol, strategy in owners.items()
            if strategy == "sma_crossover"
        }
    except Exception as exc:
        raise RuntimeError(
            f"could not safely read open SMA positions from {db_path}"
        ) from exc


def _compute_metrics(df: pd.DataFrame, config: ScanConfig) -> dict[str, object] | None:
    """Compute durable inputs and reference-only SMA diagnostics."""
    required = {"open", "high", "low", "close", "volume"}
    if df.empty or not required.issubset(df.columns) or len(df) < config.min_bars:
        return None
    if df[list(required)].isna().any().any():
        return None
    work = add_sma(df, 20)
    work = add_sma(work, 50)
    work = add_sma(work, 150)
    work = add_sma(work, 200)
    work = add_atr(work, config.atr_window)
    last = work.iloc[-1]
    needed = ("sma_50", "sma_150", "sma_200", f"atr_{config.atr_window}")
    if any(pd.isna(last[name]) for name in needed):
        return None
    close = float(last["close"])
    if close <= 0:
        return None
    diff = work["sma_20"] - work["sma_50"]
    crossover = ((diff > 0) & (diff.shift(1) <= 0)).tail(252).fillna(False)
    dates = tuple(index.date().isoformat() for index in crossover.index[crossover])
    return {
        "close": close,
        "avg_dollar_volume_50": float(
            (work["close"] * work["volume"]).tail(50).mean()
        ),
        "sma50": float(last["sma_50"]),
        "sma150": float(last["sma_150"]),
        "sma200": float(last["sma_200"]),
        "atr_pct": float(last[f"atr_{config.atr_window}"] / close),
        "high_52w_ratio": close / float(work["high"].tail(252).max()),
        "momentum_12m_skip_1m": (
            float(work["close"].iloc[-22]) / float(work["close"].iloc[-253]) - 1.0
        ),
        "crossover_dates_252": dates,
    }


def _first_rejection(metric: dict[str, object], config: ScanConfig) -> str | None:
    if float(metric["close"]) < config.min_price:
        return "price"
    if float(metric["avg_dollar_volume_50"]) < config.min_avg_dollar_volume_50:
        return "dollar_volume"
    return None


def _rank(candidates: list[Candidate], ranking: str) -> list[Candidate]:
    keys = {
        "liquidity": lambda c: (c.avg_dollar_volume_50, c.symbol),
        "momentum": lambda c: (c.momentum_12m_skip_1m, c.symbol),
        "high52": lambda c: (c.high_52w_ratio, c.symbol),
    }
    return sorted(candidates, key=keys[ranking], reverse=True)


def _candidate(
    symbol: str,
    metric: dict[str, object],
    asset: AssetInfo,
    *,
    market_cap: float | None,
    fcf_ok: bool | None,
    revenue_ok: bool | None,
    notes: list[str] | None = None,
) -> Candidate:
    return Candidate(
        symbol=symbol,
        name=asset.name,
        exchange=asset.exchange,
        close=float(metric["close"]),
        avg_dollar_volume_50=float(metric["avg_dollar_volume_50"]),
        market_cap=market_cap,
        sma50=float(metric["sma50"]),
        sma150=float(metric["sma150"]),
        sma200=float(metric["sma200"]),
        atr_pct=float(metric["atr_pct"]),
        high_52w_ratio=float(metric["high_52w_ratio"]),
        momentum_12m_skip_1m=float(metric["momentum_12m_skip_1m"]),
        crossover_dates_252=tuple(metric["crossover_dates_252"]),
        fcf_ok=fcf_ok,
        revenue_ok=revenue_ok,
        notes=list(notes or []),
    )


def _reject(
    symbol: str,
    reason: str,
    rejections: Counter[str],
    examples: dict[str, list[str]],
) -> None:
    rejections[reason] += 1
    if len(examples[reason]) < 10:
        examples[reason].append(symbol)


def _fetch_fundamentals_with_retry(
    symbol: str, attempts: int = 2
) -> SymbolFundamentals:
    """Retry one transient whole-request failure, preserving fail-closed facts."""
    if attempts <= 0:
        raise ValueError("attempts must be positive")
    result = fetch_fundamentals(symbol)
    for attempt in range(1, attempts):
        if result.error is None:
            break
        from loguru import logger

        logger.warning(
            f"{symbol}: fundamentals attempt {attempt}/{attempts} failed; "
            "retrying once after 1s"
        )
        time.sleep(1.0)
        result = fetch_fundamentals(symbol)
    return result


def scan_candidates(
    assets: list[AssetInfo],
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    config: ScanConfig,
    include_fundamentals: bool,
    top: int,
    promotion_size: int,
    ranking: str = "liquidity",
    protected_symbols: set[str] | None = None,
) -> tuple[list[Candidate], Counter[str], dict[str, list[str]]]:
    """Return ranked durable candidates and required protection members."""
    if top <= 0 or promotion_size <= 0 or promotion_size > top:
        raise ValueError("top and promotion_size must be positive; promotion_size <= top")
    if ranking not in {"liquidity", "momentum", "high52"}:
        raise ValueError(f"unsupported ranking: {ranking}")
    protected_symbols = {symbol.upper() for symbol in (protected_symbols or set())}
    asset_by_symbol = {asset.symbol: asset for asset in assets}
    rejections: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    prequalified: list[tuple[str, dict[str, object]]] = []
    for symbol, frame in bars_by_symbol.items():
        if symbol in EXCLUDED_SHARE_CLASSES:
            _reject(symbol, "nonpreferred_share_class", rejections, examples)
            continue
        metric = _compute_metrics(frame, config)
        if metric is None:
            _reject(symbol, "insufficient_or_bad_bars", rejections, examples)
            continue
        reason = _first_rejection(metric, config)
        if reason is not None:
            _reject(symbol, reason, rejections, examples)
            continue
        prequalified.append((symbol, metric))

    metric_key = {
        "liquidity": lambda item: float(item[1]["avg_dollar_volume_50"]),
        "momentum": lambda item: float(item[1]["momentum_12m_skip_1m"]),
        "high52": lambda item: float(item[1]["high_52w_ratio"]),
    }[ranking]
    prequalified.sort(key=metric_key, reverse=True)
    review_goal = max(top + 25, math.ceil(top * 1.25))
    eligible: list[Candidate] = []
    for symbol, metric in prequalified:
        if include_fundamentals and len(eligible) >= review_goal:
            continue
        market_cap: float | None = None
        fcf_ok: bool | None = None
        revenue_ok: bool | None = None
        if include_fundamentals:
            facts = _fetch_fundamentals_with_retry(symbol)
            if facts.error:
                _reject(symbol, "fundamentals_error", rejections, examples)
                continue
            market_cap = facts.market_cap
            if market_cap is None:
                _reject(symbol, "market_cap_unknown", rejections, examples)
                continue
            if market_cap < config.min_market_cap:
                _reject(symbol, "market_cap", rejections, examples)
                continue
            fitness = assess_fitness(facts, DURABLE_PROFILE)
            fcf_ok = fitness.fcf_ok
            revenue_ok = fitness.revenue_ok
            if fitness.solvency_ok is None:
                _reject(symbol, "solvency_unknown", rejections, examples)
                continue
            if fitness.solvency_ok is False:
                _reject(symbol, "solvency", rejections, examples)
                continue
        asset = asset_by_symbol.get(symbol, AssetInfo(symbol, symbol, "UNKNOWN"))
        eligible.append(
            _candidate(
                symbol,
                metric,
                asset,
                market_cap=market_cap,
                fcf_ok=fcf_ok,
                revenue_ok=revenue_ok,
            )
        )

    ranked_all = _rank(eligible, ranking)
    selected = ranked_all[:top]
    promoted = {candidate.symbol for candidate in ranked_all[:promotion_size]}
    selected_by_symbol = {candidate.symbol: candidate for candidate in selected}
    for symbol in sorted(protected_symbols - promoted):
        existing = selected_by_symbol.get(symbol)
        if existing is not None:
            existing.protected_for_promotion = True
            existing.notes.append("PROTECTED: open SMA position; outside promoted pool")
            continue
        metric = _compute_metrics(bars_by_symbol.get(symbol, pd.DataFrame()), config)
        asset = asset_by_symbol.get(symbol, AssetInfo(symbol, symbol, "UNKNOWN"))
        if metric is None:
            protected = Candidate(
                symbol=symbol,
                name=asset.name,
                exchange=asset.exchange,
                close=float("nan"),
                avg_dollar_volume_50=float("nan"),
                market_cap=None,
                sma50=float("nan"),
                sma150=float("nan"),
                sma200=float("nan"),
                atr_pct=float("nan"),
                high_52w_ratio=float("nan"),
                momentum_12m_skip_1m=float("nan"),
                crossover_dates_252=(),
                fcf_ok=None,
                revenue_ok=None,
                in_ranked_pool=False,
                protected_for_promotion=True,
                notes=["PROTECTED: open SMA position; metrics unavailable"],
            )
        else:
            protected = _candidate(
                symbol,
                metric,
                asset,
                market_cap=None,
                fcf_ok=None,
                revenue_ok=None,
                notes=["PROTECTED: open SMA position; outside promoted pool"],
            )
            protected.in_ranked_pool = False
            protected.protected_for_promotion = True
        selected.append(protected)
    return selected, rejections, examples


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    return ordered[round((len(ordered) - 1) * percentile)]


def _contention(pool: list[Candidate], capacity: int) -> tuple[int, int, int]:
    counts: Counter[str] = Counter()
    for candidate in pool:
        counts.update(candidate.crossover_dates_252)
    return len(counts), max(counts.values(), default=0), sum(
        count > capacity for count in counts.values()
    )


def render_report(
    candidates: list[Candidate],
    rejections: Counter[str],
    examples: dict[str, list[str]],
    *,
    ranking: str,
    pool_sizes: tuple[int, ...],
    promotion_size: int,
    feed: str,
    assets_seen: int,
    bars_seen: int,
    include_fundamentals: bool,
    start: datetime,
    end: datetime,
) -> str:
    """Render the review artifact."""
    regular = [candidate for candidate in candidates if candidate.in_ranked_pool]
    protected = [candidate for candidate in candidates if candidate.protected_for_promotion]
    allocation = settings.STRATEGY_ALLOCATIONS["sma_crossover"]
    capacity = int(allocation["hard_max_positions"])
    baseline_cap = (
        settings.MAX_GROSS_EXPOSURE_PCT
        * float(allocation["target_pct"])
        * float(allocation["max_position_pct_of_sleeve"])
    )
    risk_target = float(allocation["risk_per_trade_pct"])
    binding = risk_target / (settings.ATR_STOP_MULTIPLIER * baseline_cap)
    lines = [
        f"# SMA Durable Watchlist Scan - {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Ranking: `{ranking}`",
        f"- Alpaca feed: `{feed}`",
        f"- Data window: {start.date()} to {end.date()}",
        f"- Tradable assets considered: {assets_seen}",
        f"- Assets with bars: {bars_seen}",
        f"- Fundamentals enforced: {include_fundamentals}",
        f"- Promoted pool size under review: {promotion_size}",
        "- Decision status: candidate only; explicit operator approval is required before promotion",
        "",
        "## Selection Contract",
        "",
        "- Membership uses durable completed-session price, dollar liquidity, size, solvency, and share-class rules only.",
        "- Liquidity ordering is an execution priority, not a return forecast.",
        "- Trend, crossover history, ATR, FCF, revenue, and sector are diagnostics only.",
        "- The strategy and runtime filters decide whether a member may enter.",
        "- This script is report-only and never edits the active watchlist.",
        "",
        "## Nested Pool Comparison",
        "",
        f"| Pool | Crossovers (252d) | Active days | Peak same-day | Days above hard {capacity}-position count | Zero-cross names | Median ATR% | Cap-clipped |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for size in pool_sizes:
        pool = regular[:size]
        active, peak, over = _contention(pool, capacity)
        atrs = [candidate.atr_pct for candidate in pool if math.isfinite(candidate.atr_pct)]
        lines.append(
            f"| {size} | {sum(c.crossover_events_252 for c in pool)} | {active} | "
            f"{peak} | {over} | {sum(c.crossover_events_252 == 0 for c in pool)} | "
            f"{_nearest_rank(atrs, 0.5):.2%} | {sum(c.atr_pct < binding for c in pool)} |"
        )
    lines.extend([
        "",
        "Counts characterize opportunity coverage among companies selected today; they are not a point-in-time backtest. The hard-position column is an upper-bound count diagnostic, not sleeve-dollar capacity; `SLEEVE_FULL` can bind first and is the forward starvation metric.",
        "",
        "## Ranked Candidates",
        "",
        "| Rank | Symbol | Close | $Vol50 | Mom 12-1 | 52w % | ATR % | SMA stack | Crosses | FCF | Revenue |",
        "|---:|---|---:|---:|---:|---:|---:|---|---:|---|---|",
    ])
    for rank, candidate in enumerate(regular, start=1):
        stack = candidate.close > candidate.sma50 > candidate.sma150 > candidate.sma200
        def diag(value: bool | None) -> str:
            return "pass" if value is True else "fail" if value is False else "unknown"
        lines.append(
            f"| {rank} | {candidate.symbol} | {candidate.close:.2f} | "
            f"{_fmt_dollars(candidate.avg_dollar_volume_50)} | "
            f"{candidate.momentum_12m_skip_1m:.1%} | {candidate.high_52w_ratio:.1%} | "
            f"{candidate.atr_pct:.1%} | {'yes' if stack else 'no'} | "
            f"{candidate.crossover_events_252} | {diag(candidate.fcf_ok)} | "
            f"{diag(candidate.revenue_ok)} |"
        )
    if protected:
        lines.extend([
            "",
            "## Protected Open SMA Positions",
            "",
            "These names remain solely so normal signal exits continue to be evaluated.",
            "",
            "| Symbol | Note |",
            "|---|---|",
        ])
        for candidate in protected:
            lines.append(f"| {candidate.symbol} | {'; '.join(candidate.notes)} |")
    clipped = [candidate.symbol for candidate in regular if candidate.atr_pct < binding]
    lines.extend([
        "",
        "## Risk-Target Coverage",
        "",
        f"- Baseline per-position cap: {baseline_cap:.2%} of account equity",
        f"- SMA risk target: {risk_target:.2%} of account equity",
        f"- Risk sizing binds at ATR14/close >= {binding:.2%}",
        f"- Conservatively cap-clipped candidates: {len(clipped)} of {len(regular)}",
        "- A universe refresh does not silently change the risk target.",
        "",
        "## Rejections",
        "",
        "| Reason | Count | Meaning | Examples |",
        "|---|---:|---|---|",
    ])
    for reason, count in rejections.most_common():
        lines.append(
            f"| `{reason}` | {count} | {REJECTION_LABELS.get(reason, '')} | "
            f"{', '.join(examples.get(reason, []))} |"
        )
    lines.extend([
        "",
        "## Limitations",
        "",
        "- This is a current-universe snapshot, not a survivorship-free historical test.",
        "- Alternative ranking modes are diagnostics; liquidity is the proposed v3 default.",
        "- With fundamentals disabled, market cap and solvency are not enforced.",
        "- Unresolved entry orders must be reconciled separately before promotion.",
    ])
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool-sizes", nargs="+", type=int, default=list(DEFAULT_POOL_SIZES))
    parser.add_argument("--promotion-size", type=int, default=DEFAULT_PROMOTION_SIZE)
    parser.add_argument("--ranking", choices=("liquidity", "momentum", "high52"), default="liquidity")
    parser.add_argument("--lookback-days", type=int, default=420)
    parser.add_argument("--max-assets", type=int, default=None)
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--feed", choices=("iex", "sip"), default="sip")
    parser.add_argument(
        "--end-delay-minutes",
        type=int,
        default=1440,
        help="At least 1440 minutes; prevents an incomplete current daily bar.",
    )
    parser.add_argument("--include-fundamentals", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--ignore-open-positions", action="store_true")
    parser.add_argument("--trade-db", default=settings.TRADE_LOG_DB)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)
    if args.end_delay_minutes < 1440:
        raise ValueError("--end-delay-minutes must be >= 1440 for completed-session research")
    if not args.pool_sizes or any(size <= 0 for size in args.pool_sizes):
        raise ValueError("--pool-sizes must contain positive integers")
    pool_sizes = tuple(sorted(set(args.pool_sizes)))
    if args.promotion_size <= 0 or args.promotion_size > max(pool_sizes):
        raise ValueError("--promotion-size must not exceed the largest pool")
    end = datetime.now(timezone.utc) - timedelta(minutes=args.end_delay_minutes)
    start = end - timedelta(days=args.lookback_days)
    assets = get_tradable_assets(args.max_assets)
    bars = fetch_daily_bars(
        [asset.symbol for asset in assets],
        start,
        end,
        chunk_size=args.chunk_size,
        feed=args.feed,
    )
    protected = (
        set()
        if args.ignore_open_positions
        else get_open_sma_positions(args.trade_db)
    )
    candidates, rejections, examples = scan_candidates(
        assets,
        bars,
        config=ScanConfig(),
        include_fundamentals=args.include_fundamentals,
        top=max(pool_sizes),
        promotion_size=args.promotion_size,
        ranking=args.ranking,
        protected_symbols=protected,
    )
    report = render_report(
        candidates,
        rejections,
        examples,
        ranking=args.ranking,
        pool_sizes=pool_sizes,
        promotion_size=args.promotion_size,
        feed=args.feed,
        assets_seen=len(assets),
        bars_seen=len(bars),
        include_fundamentals=args.include_fundamentals,
        start=start,
        end=end,
    )
    print(report)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")


if __name__ == "__main__":
    main()
