#!/usr/bin/env python3
"""
Build a static RSI universe optimized for long-run profitability and trade density.

This script is intentionally separate from `scripts/rsi_watchlist_scan.py`.
The dynamic scanner answers: "Which names look attractive *right now*?"
This static builder answers: "Which names make the most sense as a persistent
RSI mean-reversion basket over a multi-year validation window?"

Workflow:
  1. Fetch a recent daily-bar window for the tradable universe.
  2. Apply a *coarse* viability filter only (price, liquidity, blacklist).
  3. Fetch a long validation window for the survivors.
  4. Reuse the exact RSI validation/backtest logic.
  5. Rank symbols primarily by long-run profitability and trade density.
  6. Apply the hard market-cap / solvency gate only to the strongest ranked
     names, using a local cache to avoid repeated fundamentals fetches.

This is deliberately different from the dynamic RSI scanner: we do *not* want
recent oversold activity or short-horizon setup quality to dominate the
selection, because the goal is a persistent basket rather than a "best names
this week" list.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from indicators.technicals import add_rsi
from config.settings import RSI_WATCHLIST
from scripts.rsi_candidate_validate import ValidationConfig, validate_symbols
from scripts.sma_watchlist_scan import AssetInfo, configure_logging, fetch_daily_bars, get_tradable_assets
from scripts.watchlist_review import RSI_PROFILE, assess_fitness, fetch_fundamentals


RULE_VERSION = "rsi_static_universe_v2"
DEFAULT_FUNDAMENTALS_CACHE = ROOT / "data" / "rsi_static_fundamentals_cache.json"


@dataclass(frozen=True)
class StaticUniverseConfig:
    """Configuration for the static RSI universe builder."""

    recent_lookback_days: int = 420
    validation_lookback_days: int = 1825
    min_market_cap: float = 2_000_000_000.0
    min_price: float = 10.0
    min_avg_volume_20: float = 500_000.0
    min_avg_dollar_volume_50: float = 50_000_000.0
    # Static baskets should not require recent oversold activity. We only care
    # that the name is liquid and viable enough to deserve a long-window test.
    min_recent_oversold_events: int = 0
    shortlist_size: int = 120
    top: int = 30
    fundamentals_pool_size: int = 60
    min_trade_count: int = 4
    min_total_return: float = 0.10
    min_profit_factor: float = 1.10
    max_drawdown: float = -0.60
    min_event_hit_rate: float = 0.30
    max_stop_rate: float = 0.40
    target_count: int = 24
    max_per_sector: int = 4
    blacklist: tuple[str, ...] = ("DINO",)


@dataclass(frozen=True)
class PrefilterCandidate:
    """One symbol that passed the broad recent-window viability filter."""

    symbol: str
    price: float
    avg_volume_20: float
    avg_dollar_volume_50: float
    recent_oversold_events: int
    recent_rsi: float
    market_cap: float | None
    sector: str


@dataclass(frozen=True)
class RankedStaticCandidate:
    """Final ranked static-universe candidate."""

    symbol: str
    score: float
    verdict: str
    reasons: list[str]
    recent: PrefilterCandidate
    result: object  # SymbolValidation


@dataclass(frozen=True)
class BasketSummary:
    """Aggregate per-symbol basket summary."""

    symbol_count: int
    total_trades: int
    trades_per_month: float
    avg_return: float
    median_return: float
    avg_sharpe: float
    median_sharpe: float
    avg_max_drawdown: float
    median_max_drawdown: float
    avg_profit_factor_capped: float
    avg_win_rate: float


@dataclass(frozen=True)
class FundamentalSnapshot:
    """Cached metadata used for the hard quality gate."""

    market_cap: float | None
    sector: str
    solvency_ok: bool | None


def prefilter_assets(
    assets: list[AssetInfo],
    bars_by_symbol: dict[str, pd.DataFrame],
    *,
    config: StaticUniverseConfig,
) -> tuple[list[PrefilterCandidate], Counter[str]]:
    """Apply broad viability filters using only recent bars."""
    candidates: list[PrefilterCandidate] = []
    rejections: Counter[str] = Counter()

    for asset in assets:
        symbol = asset.symbol
        if symbol in config.blacklist:
            rejections["blacklist"] += 1
            continue
        if not _is_stock_like(asset):
            rejections["non_stock_product"] += 1
            continue

        df = bars_by_symbol.get(symbol)
        metric = _recent_metric(df)
        if metric is None:
            rejections["insufficient_or_bad_bars"] += 1
            continue

        reason = _recent_rejection(metric, config)
        if reason is not None:
            rejections[reason] += 1
            continue

        candidates.append(
            PrefilterCandidate(
                symbol=symbol,
                price=float(metric["price"]),
                avg_volume_20=float(metric["avg_volume_20"]),
                avg_dollar_volume_50=float(metric["avg_dollar_volume_50"]),
                recent_oversold_events=int(metric["recent_oversold_events"]),
                recent_rsi=float(metric["recent_rsi"]),
                market_cap=None,
                sector="UNKNOWN",
            )
        )

    # Prefilter order is intentionally coarse: prioritize liquid names and use
    # recent oversold count only as a tertiary tiebreaker.
    candidates.sort(
        key=lambda c: (
            c.avg_dollar_volume_50,
            c.avg_volume_20,
            c.recent_oversold_events,
        ),
        reverse=True,
    )
    return candidates[: config.shortlist_size], rejections


def _is_stock_like(asset: AssetInfo) -> bool:
    """Best-effort exclusion of ETFs, funds, trusts, and other wrappers."""
    from utils.asset_filters import is_stock_like
    return is_stock_like(asset.symbol, asset.name, asset.exchange)


def _fetch_sector(symbol: str) -> str:
    """Best-effort sector fetch for basket concentration control."""
    try:
        info = getattr(yf.Ticker(symbol), "info", {}) or {}
    except Exception:
        return "UNKNOWN"
    sector = info.get("sector")
    if not sector or pd.isna(sector):
        return "UNKNOWN"
    return str(sector).upper()


def _load_fundamental_cache(cache_path: Path) -> dict[str, FundamentalSnapshot]:
    """Load cached fundamentals metadata for repeat static-universe rebuilds."""
    if not cache_path.exists():
        return {}
    try:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    out: dict[str, FundamentalSnapshot] = {}
    for symbol, payload in raw.items():
        if not isinstance(payload, dict):
            continue
        out[symbol] = FundamentalSnapshot(
            market_cap=_safe_float(payload.get("market_cap")),
            sector=str(payload.get("sector") or "UNKNOWN").upper(),
            solvency_ok=_safe_bool(payload.get("solvency_ok")),
        )
    return out


def _save_fundamental_cache(cache_path: Path, cache: dict[str, FundamentalSnapshot]) -> None:
    """Persist fundamentals metadata for future quarterly rebuilds."""
    payload = {
        symbol: {
            "market_cap": snapshot.market_cap,
            "sector": snapshot.sector,
            "solvency_ok": snapshot.solvency_ok,
        }
        for symbol, snapshot in sorted(cache.items())
    }
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _get_fundamental_snapshot(
    symbol: str,
    cache: dict[str, FundamentalSnapshot],
) -> FundamentalSnapshot | None:
    """Return cached-or-fresh fundamentals metadata for one symbol."""
    cached = cache.get(symbol)
    if cached is not None:
        return cached

    try:
        fundamentals = fetch_fundamentals(symbol)
    except Exception:
        return None

    if fundamentals.error:
        return None

    fitness = assess_fitness(fundamentals, RSI_PROFILE)
    if fitness.error:
        return None

    snapshot = FundamentalSnapshot(
        market_cap=fundamentals.market_cap,
        sector=_fetch_sector(symbol),
        solvency_ok=fitness.solvency_ok,
    )
    cache[symbol] = snapshot
    return snapshot


def _recent_metric(df: pd.DataFrame | None) -> dict[str, float | int] | None:
    """Compute a small recent-window metric bundle for viability filtering."""
    required = {"close", "volume"}
    if df is None or df.empty or not required.issubset(df.columns):
        return None

    work = df.copy()
    work = work[["open", "high", "low", "close", "volume"]].dropna().sort_index()
    if len(work) < 60:
        return None

    work = add_rsi(work, 14)
    last = work.iloc[-1]
    if math.isnan(float(last["rsi_14"])):
        return None

    avg_volume_20 = float(work["volume"].tail(20).mean())
    avg_dollar_volume_50 = float((work["close"] * work["volume"]).tail(50).mean())
    rsi = work["rsi_14"]
    recent_events = (rsi < 30.0) & (rsi.shift(1) >= 30.0)

    return {
        "price": float(last["close"]),
        "avg_volume_20": avg_volume_20,
        "avg_dollar_volume_50": avg_dollar_volume_50,
        "recent_oversold_events": int(recent_events.tail(252).fillna(False).sum()),
        "recent_rsi": float(last["rsi_14"]),
    }


def _recent_rejection(
    metric: dict[str, float | int],
    config: StaticUniverseConfig,
) -> str | None:
    """Return the first recent-window rejection reason, if any."""
    if float(metric["price"]) < config.min_price:
        return "price"
    if float(metric["avg_volume_20"]) < config.min_avg_volume_20:
        return "share_volume"
    if float(metric["avg_dollar_volume_50"]) < config.min_avg_dollar_volume_50:
        return "dollar_volume"
    if int(metric["recent_oversold_events"]) < config.min_recent_oversold_events:
        return "recent_oversold_events"
    return None


def rank_static_universe(
    recent_candidates: list[PrefilterCandidate],
    validation_results: list[object],
    *,
    config: StaticUniverseConfig,
) -> list[RankedStaticCandidate]:
    """Rank long-run validation results for the static universe use case."""
    recent_by_symbol = {c.symbol: c for c in recent_candidates}
    ranked: list[RankedStaticCandidate] = []

    for result in validation_results:
        recent = recent_by_symbol.get(result.symbol)
        if recent is None:
            continue
        score = _score_result(result)
        reasons = _reasons(result, config)
        verdict = _verdict(reasons)
        ranked.append(
            RankedStaticCandidate(
                symbol=result.symbol,
                score=score,
                verdict=verdict,
                reasons=reasons,
                recent=recent,
                result=result,
            )
        )

    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def apply_fundamental_gate(
    ranked: list[RankedStaticCandidate],
    *,
    config: StaticUniverseConfig,
    cache_path: Path,
    pool_size: int | None = None,
) -> tuple[list[RankedStaticCandidate], Counter[str]]:
    """Apply the strict market-cap / solvency gate to the strongest contenders."""
    limit = pool_size or config.fundamentals_pool_size
    cache = _load_fundamental_cache(cache_path)
    filtered: list[RankedStaticCandidate] = []
    rejections: Counter[str] = Counter()

    for item in ranked[:limit]:
        snapshot = _get_fundamental_snapshot(item.symbol, cache)
        if snapshot is None:
            rejections["fundamental_fetch_error"] += 1
            continue
        if snapshot.market_cap is None or snapshot.market_cap < config.min_market_cap:
            rejections["market_cap"] += 1
            continue
        if snapshot.solvency_ok is False:
            rejections["solvency"] += 1
            continue

        filtered.append(
            replace(
                item,
                recent=replace(
                    item.recent,
                    market_cap=snapshot.market_cap,
                    sector=snapshot.sector,
                ),
            )
        )

    _save_fundamental_cache(cache_path, cache)
    return filtered, rejections


def assemble_final_basket(
    ranked: list[RankedStaticCandidate],
    *,
    config: StaticUniverseConfig,
) -> tuple[list[RankedStaticCandidate], list[RankedStaticCandidate]]:
    """Pick the final basket from ranked names, respecting sector caps."""
    selected: list[RankedStaticCandidate] = []
    near_misses: list[RankedStaticCandidate] = []
    sector_counts: Counter[str] = Counter()

    for item in ranked:
        if item.verdict == "REJECT":
            near_misses.append(item)
            continue

        sector = item.recent.sector or "UNKNOWN"
        if sector != "UNKNOWN" and sector_counts[sector] >= config.max_per_sector:
            near_misses.append(item)
            continue

        selected.append(item)
        if sector != "UNKNOWN":
            sector_counts[sector] += 1
        if len(selected) >= config.target_count:
            break

    for item in ranked:
        if item not in selected and item not in near_misses:
            near_misses.append(item)
    return selected, near_misses


def summarize_basket(results: list[object]) -> BasketSummary:
    """Aggregate the per-symbol summary metrics for a basket."""
    if not results:
        return BasketSummary(0, 0, 0.0, float("nan"), float("nan"), float("nan"), float("nan"), float("nan"), float("nan"), float("nan"), float("nan"))

    returns = [float(result.backtest_stats["total_return"]) for result in results]
    sharpes = [float(result.backtest_stats.get("sharpe", float("nan"))) for result in results]
    max_dds = [float(result.backtest_stats["max_drawdown"]) for result in results]
    pfs = []
    win_rates = []
    trade_count = 0
    for result in results:
        pf = float(result.backtest_stats["profit_factor"])
        pfs.append(8.0 if math.isinf(pf) else pf)
        win_rates.append(float(result.backtest_stats["win_rate"]))
        trade_count += int(result.backtest_stats["trade_count"])

    return BasketSummary(
        symbol_count=len(results),
        total_trades=trade_count,
        trades_per_month=trade_count / 49.0,
        avg_return=float(pd.Series(returns).mean()),
        median_return=float(pd.Series(returns).median()),
        avg_sharpe=float(pd.Series(sharpes).mean()),
        median_sharpe=float(pd.Series(sharpes).median()),
        avg_max_drawdown=float(pd.Series(max_dds).mean()),
        median_max_drawdown=float(pd.Series(max_dds).median()),
        avg_profit_factor_capped=float(pd.Series(pfs).mean()),
        avg_win_rate=float(pd.Series(win_rates).mean()),
    )


def _score_result(result: object) -> float:
    """Score a SymbolValidation for long-run static-universe selection."""
    stats = result.backtest_stats
    total_return = float(stats["total_return"])
    sharpe = float(stats.get("sharpe", float("nan")))
    if math.isnan(sharpe):
        sharpe = -1.0
    max_drawdown = abs(float(stats["max_drawdown"]))
    profit_factor = float(stats["profit_factor"])
    if math.isinf(profit_factor):
        profit_factor = 8.0
    trade_count = float(stats["trade_count"])
    hit_rate = float(result.event_hit_rate)
    stop_rate = result.stop_failures / max(len(result.events), 1)

    # Backtest-first ranking: Sharpe, total return, trade count, and PF do the
    # heavy lifting. Event/stops are secondary quality modifiers.
    return (
        _clamp(sharpe / 1.5, -1.0, 1.5) * 32.0
        + _clamp(total_return / 1.50, -1.0, 1.5) * 30.0
        + _clamp(trade_count / 10.0, 0.0, 1.5) * 18.0
        + _clamp((profit_factor - 1.0) / 4.0, -1.0, 1.0) * 12.0
        + _clamp(hit_rate / 0.60, 0.0, 1.0) * 6.0
        - _clamp(max_drawdown / 0.60, 0.0, 1.5) * 16.0
        - _clamp(stop_rate / 0.40, 0.0, 1.5) * 6.0
    )


def _reasons(result: object, config: StaticUniverseConfig) -> list[str]:
    """Explain why a candidate is promote/watch/reject."""
    stats = result.backtest_stats
    trade_count = int(stats["trade_count"])
    stop_rate = result.stop_failures / max(len(result.events), 1)
    profit_factor = float(stats["profit_factor"])
    reasons: list[str] = []

    if trade_count < config.min_trade_count:
        reasons.append("too few exact strategy trades")
    if float(stats["total_return"]) < config.min_total_return:
        reasons.append("exact strategy return below threshold")
    if not math.isinf(profit_factor) and profit_factor < config.min_profit_factor:
        reasons.append("profit factor below threshold")
    if float(stats["max_drawdown"]) < config.max_drawdown:
        reasons.append("strategy drawdown too deep")
    if result.event_hit_rate < config.min_event_hit_rate:
        reasons.append("event hit rate below threshold")
    if stop_rate > config.max_stop_rate:
        reasons.append("too many ATR stop failures")
    return reasons


def _verdict(reasons: list[str]) -> str:
    """Map reason list to a promote/watch/reject verdict."""
    if not reasons:
        return "PROMOTE"
    hard_fails = {
        "exact strategy return below threshold",
        "profit factor below threshold",
    }
    if any(reason in hard_fails for reason in reasons):
        return "REJECT"
    return "WATCH"


def render_report(
    ranked: list[RankedStaticCandidate],
    selected: list[RankedStaticCandidate],
    near_misses: list[RankedStaticCandidate],
    rejections: Counter[str],
    selected_summary: BasketSummary,
    current_summary: BasketSummary,
    *,
    feed: str,
    recent_start: datetime,
    validation_start: datetime,
    end: datetime,
    config: StaticUniverseConfig,
) -> str:
    """Render a markdown report for the static-universe build."""
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"# RSI Static Universe Builder - {generated}",
        "",
        f"- Rule version: `{RULE_VERSION}`",
        f"- Alpaca feed: `{feed}`",
        f"- Recent prefilter window: {recent_start.date()} to {end.date()}",
        f"- Validation window: {validation_start.date()} to {end.date()}",
        f"- Data end timestamp: {end.isoformat(timespec='seconds')}",
        f"- Blacklist: {', '.join(config.blacklist) if config.blacklist else 'none'}",
        "",
        "## Final Basket",
        "",
        f"- Target basket size: {config.target_count}",
        f"- Selected symbols ({len(selected)}): {', '.join(item.symbol for item in selected) if selected else 'none'}",
        "",
        "## Basket Comparison",
        "",
        "| Basket | Symbols | Trades | Trades/Month | Avg Return | Median Return | Avg Sharpe | Median Sharpe | Avg MaxDD | Avg PF | Avg Win % |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        f"| Static Builder Final | {selected_summary.symbol_count} | {selected_summary.total_trades} | {selected_summary.trades_per_month:.2f} | {_fmt_pct(selected_summary.avg_return)} | {_fmt_pct(selected_summary.median_return)} | {_fmt_float(selected_summary.avg_sharpe)} | {_fmt_float(selected_summary.median_sharpe)} | {_fmt_pct(selected_summary.avg_max_drawdown)} | {_fmt_float(selected_summary.avg_profit_factor_capped)} | {_fmt_pct(selected_summary.avg_win_rate)} |",
        f"| Current RSI Watchlist | {current_summary.symbol_count} | {current_summary.total_trades} | {current_summary.trades_per_month:.2f} | {_fmt_pct(current_summary.avg_return)} | {_fmt_pct(current_summary.median_return)} | {_fmt_float(current_summary.avg_sharpe)} | {_fmt_float(current_summary.median_sharpe)} | {_fmt_pct(current_summary.avg_max_drawdown)} | {_fmt_float(current_summary.avg_profit_factor_capped)} | {_fmt_pct(current_summary.avg_win_rate)} |",
        "",
        "## Ranked Universe",
        "",
        "| Rank | Verdict | Symbol | Score | Trades | Return | Sharpe | MaxDD | PF | Win % | Events | Hit % | Stops | Recent $Vol50 | Reasons |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, item in enumerate(ranked, start=1):
        stats = item.result.backtest_stats
        reasons = "; ".join(item.reasons) if item.reasons else "-"
        lines.append(
            "| "
            f"{i} | {item.verdict} | {item.symbol} | {item.score:.1f} | "
            f"{int(stats['trade_count'])} | {_fmt_pct(float(stats['total_return']))} | "
            f"{_fmt_float(float(stats.get('sharpe', float('nan'))))} | "
            f"{_fmt_pct(float(stats['max_drawdown']))} | "
            f"{_fmt_float(float(stats['profit_factor']))} | "
            f"{_fmt_pct(float(stats['win_rate']))} | {len(item.result.events)} | "
            f"{_fmt_pct(item.result.event_hit_rate)} | {item.result.stop_failures} | "
            f"${item.recent.avg_dollar_volume_50 / 1e6:,.1f}M | {reasons} |"
        )

    lines.extend(["", "## Near Misses", "", "| Symbol | Verdict | Score | Reasons |", "|---|---|---:|---|"])
    for item in near_misses[:20]:
        reasons = "; ".join(item.reasons) if item.reasons else "sector cap overflow / lower rank"
        lines.append(f"| {item.symbol} | {item.verdict} | {item.score:.1f} | {reasons} |")

    lines.extend(["", "## Recent-Prefilter Rejections", "", "| Reason | Count |", "|---|---:|"])
    for reason, count in rejections.most_common():
        lines.append(f"| `{reason}` | {count} |")
    return "\n".join(lines)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _fmt_pct(value: float) -> str:
    if math.isnan(value):
        return "n/a"
    return f"{value * 100:.1f}%"


def _fmt_float(value: float) -> str:
    if math.isinf(value):
        return "inf"
    if math.isnan(value):
        return "n/a"
    return f"{value:.2f}"


def _safe_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_bool(value: object) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build a static RSI universe optimized for long-run profitability.",
    )
    parser.add_argument("--feed", choices=["iex", "sip"], default="sip")
    parser.add_argument("--end-delay-minutes", type=int, default=60)
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--shortlist-size", type=int, default=120)
    parser.add_argument("--target-count", type=int, default=24)
    parser.add_argument("--fundamentals-pool-size", type=int, default=60)
    parser.add_argument(
        "--skip-fundamentals",
        action="store_true",
        help="Skip late-stage market-cap, solvency, and sector enrichment checks.",
    )
    parser.add_argument(
        "--fundamentals-cache",
        type=Path,
        default=DEFAULT_FUNDAMENTALS_CACHE,
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("logs/rsi_static_universe_latest.md"),
    )
    parser.add_argument("--verbose", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(args.verbose)

    end = datetime.now(timezone.utc) - timedelta(minutes=args.end_delay_minutes)
    recent_start = end - timedelta(days=420)
    validation_start = end - timedelta(days=1825)
    config = StaticUniverseConfig(
        top=args.top,
        shortlist_size=args.shortlist_size,
        target_count=args.target_count,
        fundamentals_pool_size=args.fundamentals_pool_size,
    )

    from loguru import logger

    logger.info(
        f"RSI static universe build started: rule={RULE_VERSION} "
        f"feed={args.feed} shortlist={config.shortlist_size} top={config.top}"
    )
    assets = get_tradable_assets()
    symbols = [asset.symbol for asset in assets]
    recent_bars = fetch_daily_bars(
        symbols,
        recent_start,
        end,
        chunk_size=200,
        feed=args.feed,
    )
    recent_candidates, rejections = prefilter_assets(
        assets,
        recent_bars,
        config=config,
    )
    logger.info(f"recent prefilter survivors: {len(recent_candidates)}")

    validation_symbols = list(dict.fromkeys([candidate.symbol for candidate in recent_candidates] + list(RSI_WATCHLIST)))
    validation_bars = fetch_daily_bars(
        validation_symbols,
        validation_start,
        end,
        chunk_size=200,
        feed=args.feed,
    )
    results = validate_symbols(
        validation_bars,
        candidates=validation_symbols,
        controls=[],
        config=ValidationConfig(),
    )
    ranked = rank_static_universe(recent_candidates, results, config=config)
    if args.skip_fundamentals:
        gated_ranked = ranked
        fundamental_rejections: Counter[str] = Counter()
    else:
        gated_ranked, fundamental_rejections = apply_fundamental_gate(
            ranked,
            config=config,
            cache_path=args.fundamentals_cache,
            pool_size=args.fundamentals_pool_size,
        )
        for reason, count in fundamental_rejections.items():
            rejections[reason] += count
        logger.info(
            "late-stage fundamentals gate survivors: "
            f"{len(gated_ranked)} / {min(len(ranked), args.fundamentals_pool_size)}"
        )

    selected, near_misses = assemble_final_basket(gated_ranked, config=config)
    results_by_symbol = {result.symbol: result for result in results}
    selected_results = [results_by_symbol[item.symbol] for item in selected if item.symbol in results_by_symbol]
    current_results = [results_by_symbol[symbol] for symbol in RSI_WATCHLIST if symbol in results_by_symbol]
    selected_summary = summarize_basket(selected_results)
    current_summary = summarize_basket(current_results)

    report = render_report(
        gated_ranked[: config.top],
        selected,
        near_misses,
        rejections,
        selected_summary,
        current_summary,
        feed=args.feed,
        recent_start=recent_start,
        validation_start=validation_start,
        end=end,
        config=config,
    )
    print(report)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
        logger.info(f"report saved: {args.output}")


if __name__ == "__main__":
    main()
