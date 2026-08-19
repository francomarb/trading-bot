"""
P&L tracking and reporting (Phase 9).

`PnLTracker` reads the trade database produced by `TradeLogger` and computes:

  1. **Daily P&L summary** — realized + unrealized, trade count, largest
     win/loss, max intraday drawdown. Written as a markdown file per day.

  2. **Per-strategy attribution** — P&L, trade count, expectancy, and
     rolling Sharpe broken out per `strategy_name`. Even with one strategy
     today the schema supports N strategies (Phase 11 readiness).

  3. **Continuous slippage monitoring** — rolling adverse-only slippage
     stats from `slippage_adverse_bps` (Phase 2 slippage unification).
     The Phase 6.11 drift kill switch consumes adverse slippage live; the
     reports here surface the rolling distribution for operator review.

  4. **Weekly summary report** — aggregates daily summaries into a markdown
     weekly digest.

Design principles:
  - All computation is from the trade database — single source of truth.
  - Reports are markdown files, human-readable, git-friendly.
  - No external dependencies beyond the standard library.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from loguru import logger

from config import settings
from reporting.logger import is_execution_quality_measurement


_OCC_SYMBOL_RE = re.compile(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$")

# PLAN 11.50. Basis points are a *percentage*, so they are only
# comparable between instruments of similar price. They are not
# comparable across equities and options, and pooling them produces a
# number that describes neither:
#
#   an 8¢ execution miss on a $400 stock   ->     2 bps
#   the same 8¢ miss on a $0.66 contract   -> 1,212 bps
#
# On the live trade log that made equity execution of 1.6 bps read as
# 75.6 bps, because 17 cheap option rows outweighed 3 expensive equity
# ones. An operator would reasonably conclude execution had degraded 15x.
#
# Every aggregate in this module is therefore reported per instrument
# class. The health assessor and dashboard were never affected because
# both group by strategy, and strategies are single-instrument — which
# is also why grouping is the right fix rather than a special case.
INSTRUMENT_CLASSES: tuple[str, ...] = ("equity", "option")


def _instrument_class(symbol: str | None) -> str:
    """"equity" or "option" for a trade-log symbol (OCC = option)."""
    return "option" if _OCC_SYMBOL_RE.match(str(symbol or "")) else "equity"


def unrealized_pnl_from_positions(positions) -> float:
    """
    Sum broker-reported unrealized P&L across open positions.

    `positions` is any iterable of objects carrying `unrealized_pl` — in
    production `BrokerSnapshot.account.open_positions.values()`, whose
    `unrealized_pl` is Alpaca's own mark-to-market for the position.

    A position whose `unrealized_pl` is None contributes 0.0 *and is
    logged*. That is deliberate: this function exists because the daily
    report shipped a hardcoded $0.00 unrealized for 71 sessions, and a
    silent None→0.0 coercion would reintroduce exactly that failure one
    position at a time. Alpaca populates the field for every equity and
    option position, so a warning here means the snapshot is degraded
    and the total is an understatement, not that the position is flat.
    """
    total = 0.0
    missing: list[str] = []
    for pos in positions:
        value = getattr(pos, "unrealized_pl", None)
        if value is None:
            missing.append(str(getattr(pos, "symbol", "?")))
            continue
        total += float(value)
    if missing:
        logger.warning(
            f"unrealized P&L: broker reported no unrealized_pl for "
            f"{len(missing)} position(s) ({', '.join(sorted(missing))}) — "
            f"daily-report unrealized total ${total:,.2f} understates the book"
        )
    return total


def _slippage_by_instrument(trades: list[dict]) -> dict[str, dict]:
    """Adverse-slippage stats split by instrument class.

    Returns only classes that actually have measured rows — a class with
    nothing to report is omitted rather than shown as 0.0, for the same
    reason the dashboard renders a blank instead of a zero: absence of a
    measurement must not read as perfect execution.
    """
    buckets: dict[str, list[float]] = {}
    for t in trades:
        value = _adverse_bps(t)
        if value is None:
            continue
        buckets.setdefault(_instrument_class(t.get("symbol")), []).append(value)
    return {
        cls: {
            "count": len(vals),
            "mean_bps": round(sum(vals) / len(vals), 2),
            "max_bps": round(max(vals), 2),
        }
        for cls, vals in buckets.items()
    }


_SLIPPAGE_COLUMN = "slippage_adverse_bps"
_QUALITY_COLUMN = "slippage_measurement_quality"
_BENCHMARK_KIND_COLUMN = "slippage_benchmark_kind"


def _adverse_bps(trade: dict) -> float | None:
    """Return adverse slippage for a trade row, or None when the row
    doesn't carry an execution-quality measurement.

    Delegates the decision to `is_execution_quality_measurement`, the
    single definition shared with health / calibration / reconcile /
    dashboard / the drift kill switch.

    Returns None for:
      - NULL / missing / non-numeric slippage values, OR
      - rows whose benchmark isn't in the execution-quality family
        (`fallback_latest_close` measures market drift between the
        last bar close and the fill, not fill quality), OR
      - rows whose quality is reconstructed rather than live
        (`recovered`, `unavailable`, future enums, or a typo).

    All three mean the row has no honest execution measurement and
    must be skipped — defaulting to 0 silently dilutes operator-facing
    means, and mixing in the other metric families reports market
    movement as though it were execution cost.
    """
    if not is_execution_quality_measurement(
        trade.get(_BENCHMARK_KIND_COLUMN), trade.get(_QUALITY_COLUMN)
    ):
        return None
    raw = trade.get(_SLIPPAGE_COLUMN)
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass
class StrategyStats:
    """Per-strategy attribution for a given period."""

    strategy_name: str
    trade_count: int = 0
    total_pnl: float = 0.0
    wins: int = 0
    losses: int = 0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    # Mean adverse slippage (bps). Sourced from
    # `slippage_adverse_bps` on `trades` (Phase 2 slippage
    # unification); rows with NULL slippage are skipped — not
    # treated as zero — so the average isn't diluted by paths
    # without a benchmark (LIMIT entries, external closes, MLEG
    # long-leg structural NULLs).
    mean_slippage_bps: float = 0.0
    trade_pnls: list[float] = field(default_factory=list)

    @property
    def win_rate(self) -> float:
        return self.wins / self.trade_count if self.trade_count > 0 else 0.0

    @property
    def expectancy(self) -> float:
        """Average P&L per trade."""
        return self.total_pnl / self.trade_count if self.trade_count > 0 else 0.0

    @property
    def profit_factor(self) -> float:
        """Gross profit / gross loss. inf if no losses."""
        gross_win = sum(p for p in self.trade_pnls if p > 0)
        gross_loss = abs(sum(p for p in self.trade_pnls if p < 0))
        if gross_loss == 0:
            return float("inf") if gross_win > 0 else 0.0
        return gross_win / gross_loss


@dataclass
class DailySummary:
    """Daily P&L snapshot."""

    date: str
    total_trades: int = 0
    realized_pnl: float = 0.0
    # Broker mark-to-market across open positions at session end. Not
    # derivable from the trade log (which only knows closed rows) — the
    # caller supplies it from the broker snapshot.
    unrealized_pnl: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    # Peak-to-trough of *account equity* over the day, in dollars —
    # mark-to-market, so open positions count. The pre-2026-08-19
    # definition was peak-to-trough of cumulative realized P&L, which
    # ignored the open book entirely and, because nothing in production
    # fed it, was always 0.00.
    max_intraday_drawdown: float = 0.0
    session_start_equity: float = 0.0
    session_end_equity: float = 0.0
    strategies: dict[str, StrategyStats] = field(default_factory=dict)
    # PLAN 11.50 — per instrument class, e.g.
    # {"equity": {"count": 3, "mean_bps": 1.6, "max_bps": 4.9}}.
    # Replaces the pooled `slippage_mean_bps` / `slippage_max_bps` pair:
    # basis points are a percentage of price, so equities and options
    # cannot share a mean. Classes with no measured rows are absent
    # rather than present-and-zero.
    slippage_by_instrument: dict[str, dict] = field(default_factory=dict)


# ── PnL Tracker ─────────────────────────────────────────────────────────────


class PnLTracker:
    """
    Reads the trade database and computes P&L reports.

    Stateless with respect to the trading session: every number in a
    report is derived from the trade database or passed in by the
    caller, so a bot recycle mid-day cannot wipe the day's progress out
    of the summary. Persisted reports are markdown files.
    """

    def __init__(
        self,
        trade_csv_path: str | None = None,
        daily_pnl_dir: str | None = None,
        weekly_report_dir: str | None = None,
        *,
        trade_logger: "TradeLogger | None" = None,
    ) -> None:
        from reporting.logger import TradeLogger

        self._trade_logger = trade_logger or TradeLogger(path=trade_csv_path)
        self._daily_dir = daily_pnl_dir or settings.DAILY_PNL_DIR
        self._weekly_dir = weekly_report_dir or settings.WEEKLY_REPORT_DIR

    # ── Daily summary ───────────────────────────────────────────────────

    def generate_daily_summary(
        self,
        day: str | None = None,
        *,
        session_start_equity: float = 0.0,
        session_end_equity: float = 0.0,
        unrealized_pnl: float = 0.0,
        max_intraday_drawdown: float = 0.0,
    ) -> DailySummary:
        """
        Build a DailySummary from the day's realized-P&L events on disk
        (the trade log is the source of truth) plus DB slippage stats.

        Realized P&L comes from ``read_realized_pnl_events_for_day``,
        which is restart-safe: a bot recycle mid-day does not wipe the
        day's progress from the summary. This replaced an in-memory
        accumulator that production never populated — the well-known
        "P&L=$+0.00, trades=0" EOD bug.

        ``unrealized_pnl`` and ``max_intraday_drawdown`` are supplied by
        the caller, because neither is derivable from the trade log:
        unrealized P&L is a broker mark-to-market of *open* positions,
        and the drawdown is an equity path sampled across the session.
        The engine owns both (see ``TradingEngine.max_intraday_drawdown``
        and ``unrealized_pnl_from_positions``). They previously defaulted
        to a silent 0.0 that no caller ever overrode, so all 71 daily
        reports written before this change show $0.00 in both fields
        regardless of the actual book.
        """
        day = day or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Sole source: every realized-P&L close row whose exit_timestamp
        # falls on ``day``. Includes single-leg + spread closes and
        # partial rows (their dollar contribution is honest).
        events: list[tuple[str, float]] = list(
            self._trade_logger.read_realized_pnl_events_for_day(day)
        )

        # Per-strategy breakdown.
        strats: dict[str, StrategyStats] = {}
        for strat_name, pnl in events:
            if strat_name not in strats:
                strats[strat_name] = StrategyStats(strategy_name=strat_name)
            s = strats[strat_name]
            s.trade_count += 1
            s.total_pnl += pnl
            s.trade_pnls.append(pnl)
            if pnl > 0:
                s.wins += 1
                if pnl > s.largest_win:
                    s.largest_win = pnl
            elif pnl < 0:
                s.losses += 1
                if pnl < s.largest_loss:
                    s.largest_loss = pnl

        # Aggregate (over the same merged events used above).
        all_pnls = [p for _, p in events]
        total_trades = len(all_pnls)
        realized = sum(all_pnls)
        largest_win = max(all_pnls) if all_pnls else 0.0
        largest_loss = min(all_pnls) if all_pnls else 0.0

        # Slippage from database (today's rows), split by instrument
        # class — PLAN 11.50.
        slip_by_instrument = self._slippage_stats_for_day(day)

        # Enrich strategy stats with slippage.
        csv_strat_slip = self._slippage_by_strategy(day)
        for name, mean_slip in csv_strat_slip.items():
            if name in strats:
                strats[name].mean_slippage_bps = mean_slip

        return DailySummary(
            date=day,
            total_trades=total_trades,
            realized_pnl=round(realized, 2),
            unrealized_pnl=round(unrealized_pnl, 2),
            largest_win=round(largest_win, 2),
            largest_loss=round(largest_loss, 2),
            max_intraday_drawdown=round(max_intraday_drawdown, 2),
            session_start_equity=round(session_start_equity, 2),
            session_end_equity=round(session_end_equity, 2),
            strategies=strats,
            slippage_by_instrument=slip_by_instrument,
        )

    def write_daily_report(self, summary: DailySummary) -> str:
        """Write the daily summary as a markdown file. Returns the path."""
        os.makedirs(self._daily_dir, exist_ok=True)
        path = os.path.join(self._daily_dir, f"{summary.date}.md")

        lines = [
            f"# Daily P&L — {summary.date}",
            "",
            "## Account",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Session start equity | ${summary.session_start_equity:,.2f} |",
            f"| Session end equity | ${summary.session_end_equity:,.2f} |",
            f"| Realized P&L | ${summary.realized_pnl:,.2f} |",
            f"| Unrealized P&L | ${summary.unrealized_pnl:,.2f} |",
            f"| Max intraday drawdown | ${summary.max_intraday_drawdown:,.2f} |",
            "",
            "## Trades",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Total trades | {summary.total_trades} |",
            f"| Largest win | ${summary.largest_win:,.2f} |",
            f"| Largest loss | ${summary.largest_loss:,.2f} |",
        ]
        for cls in INSTRUMENT_CLASSES:
            stats = summary.slippage_by_instrument.get(cls)
            if stats is None:
                continue
            lines.append(
                f"| Mean adverse slippage ({cls}) | "
                f"{stats['mean_bps']:.1f} bps (n={stats['count']}) |"
            )
            lines.append(
                f"| Max adverse slippage ({cls}) | {stats['max_bps']:.1f} bps |"
            )
        if not summary.slippage_by_instrument:
            lines.append("| Adverse slippage | — (no measured fills) |")

        if summary.strategies:
            lines += [
                "",
                "## Per-Strategy Attribution",
                "",
            ]
            for name, s in sorted(summary.strategies.items()):
                lines += [
                    f"### {name}",
                    "",
                    f"| Metric | Value |",
                    f"|---|---|",
                    f"| Trades | {s.trade_count} |",
                    f"| P&L | ${s.total_pnl:,.2f} |",
                    f"| Win rate | {s.win_rate:.1%} |",
                    f"| Expectancy | ${s.expectancy:,.2f} |",
                    f"| Profit factor | {s.profit_factor:.2f} |",
                    f"| Largest win | ${s.largest_win:,.2f} |",
                    f"| Largest loss | ${s.largest_loss:,.2f} |",
                    f"| Mean adverse slippage | {s.mean_slippage_bps:.1f} bps |",
                    "",
                ]

        lines.append("")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        logger.info(f"daily P&L report written: {path}")
        return path

    # ── Weekly summary ──────────────────────────────────────────────────

    def generate_weekly_report(
        self,
        week_end: str | None = None,
    ) -> str | None:
        """
        Aggregate the last 7 daily summary files into a weekly markdown
        report. Returns the file path, or None if no daily reports exist.
        """
        end = (
            date.fromisoformat(week_end)
            if week_end
            else date.today()
        )
        start = end - timedelta(days=6)

        daily_files = []
        for i in range(7):
            d = start + timedelta(days=i)
            path = os.path.join(self._daily_dir, f"{d.isoformat()}.md")
            if os.path.exists(path):
                daily_files.append((d.isoformat(), path))

        if not daily_files:
            logger.info("no daily reports found for weekly summary")
            return None

        # Parse key metrics from trade CSV for the week.
        trades = self._trades_in_range(start.isoformat(), end.isoformat())
        total_trades = len(trades)
        total_pnl = 0.0
        strat_pnls: dict[str, list[float]] = {}

        # We don't have per-trade P&L in the CSV (we have fills, not
        # round-trips). Weekly report summarizes trade activity +
        # adverse-only slippage from measured rows. Rows with NULL
        # slippage are skipped — not treated as zero — so paths that
        # legitimately have no benchmark (LIMIT entries, external
        # closes) don't drag the mean toward zero.
        # PLAN 11.50: split by instrument class. A single pooled mean
        # across equities and options is not a number — see the module
        # header for why.
        slip_by_instrument = _slippage_by_instrument(trades)

        os.makedirs(self._weekly_dir, exist_ok=True)
        path = os.path.join(
            self._weekly_dir, f"week_{start.isoformat()}_to_{end.isoformat()}.md"
        )

        lines = [
            f"# Weekly Report — {start.isoformat()} to {end.isoformat()}",
            "",
            "## Summary",
            "",
            f"| Metric | Value |",
            f"|---|---|",
            f"| Trading days with reports | {len(daily_files)} |",
            f"| Total fills | {total_trades} |",
        ]
        # One row per instrument class that actually has measurements.
        # Basis points are not comparable across price scales, so there
        # is deliberately no combined figure to fall back on.
        for cls in INSTRUMENT_CLASSES:
            stats = slip_by_instrument.get(cls)
            if stats is None:
                continue
            lines.append(
                f"| Mean adverse slippage ({cls}) | "
                f"{stats['mean_bps']:.1f} bps (n={stats['count']}) |"
            )
            lines.append(
                f"| Max adverse slippage ({cls}) | {stats['max_bps']:.1f} bps |"
            )
        if not slip_by_instrument:
            lines.append("| Adverse slippage | — (no measured fills) |")
        lines += [
            "",
            "> Slippage is reported per instrument class. Basis points are a "
            "percentage of price, so equity and option figures are not "
            "comparable and are never combined — an 8¢ miss is 2 bps on a "
            "$400 stock and 1,212 bps on a $0.66 contract (PLAN 11.50).",
            "",
            "## Daily Reports",
            "",
        ]
        # Links are resolved by the reader relative to the WEEKLY report's
        # own directory, not the daily one. `{day}.md` therefore pointed at
        # `logs/weekly_reports/{day}.md`, which never exists — the dailies
        # live in `logs/daily_pnl/`. Harmless while nothing generated this
        # report; every link in the first live one was broken.
        #
        # `os.path.relpath` rather than a hardcoded `../daily_pnl/` so the
        # link stays correct when either directory is overridden via
        # settings or the constructor (tests pass tmp dirs for both).
        for day_str, day_path in daily_files:
            rel = os.path.relpath(day_path, start=self._weekly_dir)
            lines.append(f"- [{day_str}]({rel})")

        lines.append("")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        logger.info(f"weekly report written: {path}")
        return path

    # ── Slippage monitoring ─────────────────────────────────────────────

    def slippage_report(self, last_n: int = 50) -> dict:
        """
        Rolling adverse-only slippage stats from the trade database.
        Returns a dict with mean / max / count for the last N fills
        that have a measured `slippage_adverse_bps` (rows with NULL
        slippage are skipped — see `_adverse_bps`).

        `count` reflects the number of measured rows in the window,
        not the raw count of recent rows. A window of 50 recent
        fills with only 20 measured will report `count=20`. The
        denominator matches the numerator so the mean isn't
        diluted by paths that legitimately have no benchmark.
        """
        recent = self._trade_logger.read_recent(last_n)
        by_instrument = _slippage_by_instrument(recent) if recent else {}
        total = sum(s["count"] for s in by_instrument.values())
        return {
            "count": total,
            # PLAN 11.50: per instrument class. There is deliberately no
            # combined `mean_bps` — pooling equity and option basis
            # points produces a number that describes neither, and
            # keeping one "for compatibility" would preserve exactly the
            # misleading figure this change removes. Callers must name
            # the class they mean.
            "by_instrument": by_instrument,
        }

    # ── Database helpers ────────────────────────────────────────────────

    def _trades_in_range(self, start: str, end: str) -> list[dict]:
        """Read trades whose timestamp falls within [start, end]."""
        return self._trade_logger.read_trades_in_range(start, end)

    def _slippage_stats_for_day(self, day: str) -> dict[str, dict]:
        """Adverse slippage for a day, split by instrument class.

        PLAN 11.50: previously returned a single pooled `(mean, max)`
        pair across every fill. Basis points are a percentage of price,
        so equities and options cannot share a mean — on the live log
        that reported equity execution of 1.6 bps as 75.6 bps. Classes
        with no measured rows are omitted rather than reported as 0.0.
        """
        return _slippage_by_instrument(self._trades_in_range(day, day))

    def _slippage_by_strategy(self, day: str) -> dict[str, float]:
        """Mean adverse slippage per strategy for a given day. Rows
        whose `slippage_adverse_bps` is NULL/missing are skipped;
        strategies with no measured rows are omitted from the
        result entirely (rather than reporting a misleading 0)."""
        trades = self._trades_in_range(day, day)
        by_strat: dict[str, list[float]] = {}
        for t in trades:
            v = _adverse_bps(t)
            if v is None:
                continue
            strat = t.get("strategy", "unknown")
            by_strat.setdefault(strat, []).append(v)
        return {
            name: sum(vs) / len(vs)
            for name, vs in by_strat.items()
            if vs
        }
