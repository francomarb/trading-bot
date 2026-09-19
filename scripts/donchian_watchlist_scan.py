#!/usr/bin/env python3
"""Report-only Donchian durable-universe scanner.

The selector answers which companies are durable enough to monitor. It does
not predict which symbol will win and does not modify the active watchlist.
Temporary trend state and historical breakout outcomes are report columns only.
"""

from __future__ import annotations

import argparse
import math
import sys
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
    assess_fitness,
    fetch_fundamentals,
)


RULE_VERSION = settings.DONCHIAN_WATCHLIST_RULE_VERSION
DEFAULT_POOL_SIZES = (50, 100, 200)
EXCLUDED_SHARE_CLASSES: dict[str, str] = {"GOOGL": "GOOG"}
DONCHIAN_PROFILE = CheckProfile(
    strategy_name="donchian_breakout",
    display_name="Donchian Breakout",
    fcf_required=False,
    revenue_required=False,
    min_cash_runway_months=12,
)


@dataclass(frozen=True)
class ScanConfig:
    """Durable company-eligibility thresholds for Donchian."""

    min_bars: int = 260
    min_market_cap: float = 2_000_000_000.0
    min_price: float = 10.0
    min_avg_dollar_volume_50: float = 50_000_000.0
    entry_window: int = 30
    atr_window: int = 14


@dataclass
class Candidate:
    """Eligible company plus non-decision research diagnostics."""

    symbol: str
    name: str
    exchange: str
    sector: str
    close: float
    avg_dollar_volume_50: float
    market_cap: float | None
    sma200: float
    atr_pct: float
    high_52w_ratio: float
    momentum_12m_skip_1m: float
    breakout_events_252: int
    breakout_dates_252: tuple[str, ...]
    latest_breakout: bool
    liquidity_percentile: float = 0.0
    momentum_percentile: float = 0.0
    high_52w_percentile: float = 0.0
    combined_percentile: float = 0.0
    notes: list[str] = field(default_factory=list)


REJECTION_LABELS: dict[str, str] = {
    "insufficient_or_bad_bars": "Not enough clean daily bars for durable review.",
    "price": "Latest close is below the minimum price threshold.",
    "dollar_volume": "50-day average dollar volume is below the threshold.",
    "market_cap": "Market capitalization is below the minimum size threshold.",
    "solvency": "Solvency was not affirmatively established.",
    "nonpreferred_share_class": "Use GOOG for Alphabet exposure, never GOOGL.",
}


def get_open_donchian_positions(db_path: str) -> set[str]:
    """Return symbols currently owned by Donchian in the trade ledger."""
    if not Path(db_path).exists():
        return set()
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
            if strategy == "donchian_breakout"
        }
    except Exception as exc:
        raise RuntimeError(
            f"could not safely read open Donchian positions from {db_path}"
        ) from exc


def _compute_metrics(
    df: pd.DataFrame,
    config: ScanConfig,
) -> dict[str, object] | None:
    """Compute eligibility inputs and reference-only trend diagnostics."""
    required = {"open", "high", "low", "close", "volume"}
    if df.empty or not required.issubset(df.columns) or len(df) < config.min_bars:
        return None
    if df[list(required)].isna().any().any():
        return None

    work = add_sma(df, 200)
    work = add_atr(work, config.atr_window)
    last = work.iloc[-1]
    if pd.isna(last["sma_200"]) or pd.isna(last[f"atr_{config.atr_window}"]):
        return None
    close = float(last["close"])
    if close <= 0:
        return None

    prior_high = work["close"].shift(1).rolling(config.entry_window).max()
    breakout = (work["close"] > prior_high) & (work["close"] > work["sma_200"])
    close_1m_ago = float(work["close"].iloc[-22])
    close_12m_ago = float(work["close"].iloc[-253])
    high_52w = float(work["high"].tail(252).max())
    recent_breakout = breakout.tail(252).fillna(False)
    breakout_dates = tuple(
        timestamp.date().isoformat()
        for timestamp in recent_breakout.index[recent_breakout]
    )
    return {
        "close": close,
        "avg_dollar_volume_50": float(
            (work["close"] * work["volume"]).tail(50).mean()
        ),
        "sma200": float(last["sma_200"]),
        "atr_pct": float(last[f"atr_{config.atr_window}"] / close),
        "high_52w_ratio": close / high_52w,
        "momentum_12m_skip_1m": close_1m_ago / close_12m_ago - 1.0,
        "breakout_events_252": int(recent_breakout.sum()),
        "breakout_dates_252": breakout_dates,
        "latest_breakout": bool(breakout.iloc[-1]),
    }


def _first_rejection(
    metric: dict[str, object],
    config: ScanConfig,
) -> str | None:
    if float(metric["close"]) < config.min_price:
        return "price"
    if float(metric["avg_dollar_volume_50"]) < config.min_avg_dollar_volume_50:
        return "dollar_volume"
    return None


def _percentiles(values: list[float]) -> list[float]:
    """Return stable 0-100 ascending percentile ranks."""
    if not values:
        return []
    series = pd.Series(values, dtype=float)
    return (series.rank(method="average", pct=True) * 100.0).tolist()


def _apply_percentiles(candidates: list[Candidate]) -> None:
    liquidity = _percentiles([c.avg_dollar_volume_50 for c in candidates])
    momentum = _percentiles([c.momentum_12m_skip_1m for c in candidates])
    high_52w = _percentiles([c.high_52w_ratio for c in candidates])
    for candidate, liq, mom, high in zip(
        candidates, liquidity, momentum, high_52w, strict=True
    ):
        candidate.liquidity_percentile = liq
        candidate.momentum_percentile = mom
        candidate.high_52w_percentile = high
        candidate.combined_percentile = (mom + high) / 2.0


def _rank_candidates(
    candidates: list[Candidate],
    ranking: str,
) -> list[Candidate]:
    """Rank eligible candidates without using historical Donchian outcomes."""
    key_by_ranking = {
        "liquidity": lambda c: (c.avg_dollar_volume_50, c.symbol),
        "momentum": lambda c: (c.momentum_12m_skip_1m, c.symbol),
        "high52": lambda c: (c.high_52w_ratio, c.symbol),
        "combined": lambda c: (c.combined_percentile, c.symbol),
    }
    return sorted(candidates, key=key_by_ranking[ranking], reverse=True)


def _candidate_from_metric(
    symbol: str,
    metric: dict[str, object],
    asset: AssetInfo,
    *,
    market_cap: float | None,
    notes: list[str] | None = None,
) -> Candidate:
    return Candidate(
        symbol=symbol,
        name=asset.name,
        exchange=asset.exchange,
        sector=asset.sector,
        close=float(metric["close"]),
        avg_dollar_volume_50=float(metric["avg_dollar_volume_50"]),
        market_cap=market_cap,
        sma200=float(metric["sma200"]),
        atr_pct=float(metric["atr_pct"]),
        high_52w_ratio=float(metric["high_52w_ratio"]),
        momentum_12m_skip_1m=float(metric["momentum_12m_skip_1m"]),
        breakout_events_252=int(metric["breakout_events_252"]),
        breakout_dates_252=tuple(metric["breakout_dates_252"]),
        latest_breakout=bool(metric["latest_breakout"]),
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


def scan_candidates(
    assets: list[AssetInfo],
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    config: ScanConfig,
    include_fundamentals: bool,
    top: int,
    ranking: str = "liquidity",
    explain_symbols: set[str] | None = None,
    protected_symbols: set[str] | None = None,
) -> tuple[list[Candidate], Counter[str], dict[str, list[str]], dict[str, str]]:
    """Apply durable eligibility and return ranked plus protected candidates."""
    if top <= 0:
        raise ValueError("top must be positive")
    if ranking not in {"liquidity", "momentum", "high52", "combined"}:
        raise ValueError(f"unsupported ranking: {ranking}")

    asset_by_symbol = {asset.symbol: asset for asset in assets}
    explain_symbols = {s.upper() for s in (explain_symbols or set())}
    protected_symbols = {s.upper() for s in (protected_symbols or set())}
    rejections: Counter[str] = Counter()
    examples: dict[str, list[str]] = defaultdict(list)
    explanations: dict[str, str] = {}
    prequalified: list[tuple[str, dict[str, object]]] = []

    for symbol, df in bars_by_symbol.items():
        if symbol in EXCLUDED_SHARE_CLASSES:
            _reject(symbol, "nonpreferred_share_class", rejections, examples)
            explanations[symbol] = "Rejected: use GOOG for Alphabet exposure."
            continue
        metric = _compute_metrics(df, config)
        if metric is None:
            _reject(symbol, "insufficient_or_bad_bars", rejections, examples)
            continue
        reason = _first_rejection(metric, config)
        if reason is not None:
            _reject(symbol, reason, rejections, examples)
            if symbol in explain_symbols:
                explanations[symbol] = f"Rejected: {reason}."
            continue
        prequalified.append((symbol, metric))

    # Fundamentals can be slow and rate-limited. Review likely leaders first,
    # but retain a buffer so rejections do not starve the requested pool.
    metric_key = {
        "liquidity": lambda item: float(item[1]["avg_dollar_volume_50"]),
        "momentum": lambda item: float(item[1]["momentum_12m_skip_1m"]),
        "high52": lambda item: float(item[1]["high_52w_ratio"]),
        "combined": lambda item: (
            float(item[1]["momentum_12m_skip_1m"])
            + float(item[1]["high_52w_ratio"])
        ),
    }[ranking]
    prequalified.sort(key=metric_key, reverse=True)
    review_goal = max(top + 25, math.ceil(top * 1.25))
    eligible: list[Candidate] = []
    for symbol, metric in prequalified:
        if include_fundamentals and len(eligible) >= review_goal:
            break
        market_cap: float | None = None
        if include_fundamentals:
            fundamentals = fetch_fundamentals(symbol)
            market_cap = fundamentals.market_cap
            if market_cap is None or market_cap < config.min_market_cap:
                _reject(symbol, "market_cap", rejections, examples)
                continue
            fitness = assess_fitness(fundamentals, DONCHIAN_PROFILE)
            if fitness.solvency_ok is not True or fitness.error:
                _reject(symbol, "solvency", rejections, examples)
                continue
        asset = asset_by_symbol.get(symbol, AssetInfo(symbol, symbol, "UNKNOWN"))
        eligible.append(
            _candidate_from_metric(
                symbol, metric, asset, market_cap=market_cap
            )
        )
        if symbol in explain_symbols:
            explanations[symbol] = "Passed all enabled durable eligibility gates."

    _apply_percentiles(eligible)
    ranked_all = _rank_candidates(eligible, ranking)
    selected = ranked_all[:top]

    # GOOGL is excluded above; GOOG remains eligible on the same durable rules
    # as every other company and is not manually forced into the selected pool.
    selected_symbols = {candidate.symbol for candidate in selected}

    # The watchlist is also the exit-evaluation universe. Never orphan a held
    # Donchian position merely because it fell outside a refreshed pool.
    for symbol in sorted(protected_symbols - selected_symbols):
        metric = _compute_metrics(bars_by_symbol.get(symbol, pd.DataFrame()), config)
        asset = asset_by_symbol.get(symbol, AssetInfo(symbol, symbol, "UNKNOWN"))
        if metric is None:
            selected.append(
                Candidate(
                    symbol=symbol,
                    name=asset.name,
                    exchange=asset.exchange,
                    sector=asset.sector,
                    close=float("nan"),
                    avg_dollar_volume_50=float("nan"),
                    market_cap=None,
                    sma200=float("nan"),
                    atr_pct=float("nan"),
                    high_52w_ratio=float("nan"),
                    momentum_12m_skip_1m=float("nan"),
                    breakout_events_252=0,
                    breakout_dates_252=(),
                    latest_breakout=False,
                    notes=["PROTECTED: open Donchian position; metrics unavailable"],
                )
            )
        else:
            selected.append(
                _candidate_from_metric(
                    symbol,
                    metric,
                    asset,
                    market_cap=None,
                    notes=[
                        "PROTECTED: open Donchian position; outside refreshed pool"
                    ],
                )
            )

    for symbol in explain_symbols - set(explanations):
        explanations[symbol] = "No qualifying bars returned or symbol was rejected."
    return selected, rejections, examples, explanations


def _is_protected(candidate: Candidate) -> bool:
    return any(note.startswith("PROTECTED:") for note in candidate.notes)


def _nearest_rank(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    return ordered[round((len(ordered) - 1) * percentile)]


def _contention_summary(pool: list[Candidate], capacity: int = 8) -> tuple[int, int, int]:
    """Return active days, peak same-day breakouts, and days above capacity."""
    counts: Counter[str] = Counter()
    for candidate in pool:
        counts.update(candidate.breakout_dates_252)
    return (
        len(counts),
        max(counts.values(), default=0),
        sum(count > capacity for count in counts.values()),
    )


def render_report(
    candidates: list[Candidate],
    rejections: Counter[str],
    examples: dict[str, list[str]],
    explanations: dict[str, str],
    *,
    ranking: str,
    pool_sizes: tuple[int, ...],
    feed: str,
    assets_seen: int,
    bars_seen: int,
    include_fundamentals: bool,
    start: datetime,
    end: datetime,
) -> str:
    """Render a reviewable markdown report."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    regular = [c for c in candidates if not _is_protected(c)]
    protected = [c for c in candidates if _is_protected(c)]
    lines = [
        f"# Donchian Watchlist Scan - {generated}",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Ranking: `{ranking}`",
        f"- Alpaca feed: `{feed}`",
        f"- Data window: {start.date()} to {end.date()}",
        f"- Tradable assets considered: {assets_seen}",
        f"- Assets with bars: {bars_seen}",
        f"- Fundamentals enforced: {include_fundamentals}",
        f"- Largest requested pool: {max(pool_sizes)}",
        "",
        "## Selection Contract",
        "",
        "- Membership uses durable price, dollar liquidity, size, and solvency only.",
        "- Default ordering is dollar liquidity; it is an execution priority, "
        "not a return forecast.",
        "- Momentum, 52-week-high proximity, ATR, SMA200 state, and historical "
        "breakout counts are diagnostic only under the default rule.",
        "- The 30-day breakout and runtime edge filters decide whether an eligible name can enter.",
        "- The script is report-only and never edits the active watchlist.",
        "",
        "## Nested Pool Comparison",
        "",
        "| Pool | Current overlap | Breakouts (252d) | Active days | "
        "Peak same-day | Days >8 | Zero-breakout names | Median ATR % | "
        "Cap-clipped |",
        "|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    current = set(settings.DONCHIAN_WATCHLIST)
    allocation = settings.STRATEGY_ALLOCATIONS["donchian_breakout"]
    baseline_cap = (
        settings.MAX_GROSS_EXPOSURE_PCT
        * float(allocation["target_pct"])
        * float(allocation["max_position_pct_of_sleeve"])
    )
    risk_target = float(allocation["risk_per_trade_pct"])
    atr_binding_threshold = risk_target / (
        settings.ATR_STOP_MULTIPLIER * baseline_cap
    )
    for size in pool_sizes:
        pool = regular[:size]
        atr_values = [c.atr_pct for c in pool if math.isfinite(c.atr_pct)]
        active_days, peak_same_day, days_over_capacity = _contention_summary(pool)
        lines.append(
            f"| {size} | {sum(c.symbol in current for c in pool)} | "
            f"{sum(c.breakout_events_252 for c in pool)} | "
            f"{active_days} | {peak_same_day} | {days_over_capacity} | "
            f"{sum(c.breakout_events_252 == 0 for c in pool)} | "
            f"{_nearest_rank(atr_values, 0.50):.2%} | "
            f"{sum(c.atr_pct < atr_binding_threshold for c in pool)} |"
        )

    lines.extend(
        [
            "",
            "The breakout counts characterize opportunity coverage among companies "
            "selected today. They are not a point-in-time backtest and do not "
            "decide membership.",
            "",
            "## Ranked Candidates",
            "",
            "| Rank | Symbol | Close | $Vol50 | Mom 12-1 | 52w % | ATR % | "
            ">SMA200 | Breakouts | Current |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for rank, candidate in enumerate(regular, start=1):
        lines.append(
            f"| {rank} | {candidate.symbol} | {candidate.close:.2f} | "
            f"{_fmt_dollars(candidate.avg_dollar_volume_50)} | "
            f"{candidate.momentum_12m_skip_1m:.1%} | "
            f"{candidate.high_52w_ratio:.1%} | {candidate.atr_pct:.1%} | "
            f"{'yes' if candidate.close > candidate.sma200 else 'no'} | "
            f"{candidate.breakout_events_252} | "
            f"{'yes' if candidate.symbol in current else 'no'} |"
        )

    if protected:
        lines.extend(
            [
                "",
                "## Protected Open Donchian Positions",
                "",
                "These names remain solely so normal signal exits continue to be evaluated.",
                "",
                "| Symbol | Note |",
                "|---|---|",
            ]
        )
        for candidate in protected:
            lines.append(f"| {candidate.symbol} | {'; '.join(candidate.notes)} |")

    clipped = [c.symbol for c in regular if c.atr_pct < atr_binding_threshold]
    lines.extend(
        [
            "",
            "## Risk-Target Coverage",
            "",
            f"- Baseline per-position cap: {baseline_cap:.2%} of account equity",
            f"- Donchian risk target: {risk_target:.2%} of account equity",
            f"- Risk sizing binds at ATR14/close >= {atr_binding_threshold:.2%}",
            f"- Conservatively cap-clipped candidates: {len(clipped)} of {len(regular)}",
            "- A universe refresh does not silently change the risk target.",
            "",
            "## Rejections",
            "",
        ]
    )
    if not rejections:
        lines.append("No rejected symbols.")
    else:
        lines.extend(["| Reason | Count | Meaning | Examples |", "|---|---:|---|---|"])
        for reason, count in rejections.most_common():
            lines.append(
                f"| `{reason}` | {count} | {REJECTION_LABELS.get(reason, '')} | "
                f"{', '.join(examples.get(reason, []))} |"
            )

    if explanations:
        lines.extend(
            [
                "",
                "## Requested Symbol Explanations",
                "",
                "| Symbol | Explanation |",
                "|---|---|",
            ]
        )
        for symbol in sorted(explanations):
            lines.append(f"| {symbol} | {explanations[symbol]} |")

    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- This is a current-universe snapshot, not a survivorship-free historical test.",
            "- Alternative ranking modes are research comparisons; only liquidity "
            "is the v1 durable default.",
            "- With fundamentals disabled, market cap and solvency are not enforced.",
            "- With IEX, volume is venue volume; use delayed SIP for promotion research.",
        ]
    )
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a report-only durable Donchian opportunity universe."
    )
    parser.add_argument(
        "--pool-sizes",
        nargs="+",
        type=int,
        default=list(DEFAULT_POOL_SIZES),
        help="Nested pool sizes to compare; the largest controls report length.",
    )
    parser.add_argument(
        "--ranking",
        choices=["liquidity", "momentum", "high52", "combined"],
        default="liquidity",
    )
    parser.add_argument("--lookback-days", type=int, default=420)
    parser.add_argument("--max-assets", type=int, default=None)
    parser.add_argument("--chunk-size", type=int, default=200)
    parser.add_argument("--feed", choices=["iex", "sip"], default="sip")
    parser.add_argument("--end-delay-minutes", type=int, default=60)
    parser.add_argument("--include-fundamentals", action="store_true")
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--explain-symbols", nargs="+", default=[])
    parser.add_argument("--ignore-open-positions", action="store_true")
    parser.add_argument("--trade-db", default=settings.TRADE_LOG_DB)
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)
    if args.end_delay_minutes < 0:
        raise ValueError("--end-delay-minutes must be >= 0")
    if not args.pool_sizes or any(size <= 0 for size in args.pool_sizes):
        raise ValueError("--pool-sizes must contain positive integers")
    pool_sizes = tuple(sorted(set(args.pool_sizes)))
    end = datetime.now(timezone.utc) - timedelta(minutes=args.end_delay_minutes)
    start = end - timedelta(days=args.lookback_days)

    from loguru import logger

    logger.info(
        f"Donchian scan started: rule={RULE_VERSION} ranking={args.ranking} "
        f"feed={args.feed} end_delay={args.end_delay_minutes}m"
    )
    assets = get_tradable_assets(args.max_assets)
    symbols = [asset.symbol for asset in assets]
    bars = fetch_daily_bars(
        symbols, start, end, chunk_size=args.chunk_size, feed=args.feed
    )
    protected = (
        set()
        if args.ignore_open_positions
        else get_open_donchian_positions(args.trade_db)
    )
    candidates, rejections, examples, explanations = scan_candidates(
        assets,
        bars,
        config=ScanConfig(),
        include_fundamentals=args.include_fundamentals,
        top=max(pool_sizes),
        ranking=args.ranking,
        explain_symbols={symbol.upper() for symbol in args.explain_symbols},
        protected_symbols=protected,
    )
    report = render_report(
        candidates,
        rejections,
        examples,
        explanations,
        ranking=args.ranking,
        pool_sizes=pool_sizes,
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
        logger.info(f"report saved: {args.output}")


if __name__ == "__main__":
    main()
