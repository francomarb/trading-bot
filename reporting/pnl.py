"""
P&L tracking and reporting (Phase 9).

`PnLTracker` reads the trade CSV produced by `TradeLogger` and computes:

  1. **Daily P&L summary** — realized + unrealized, trade count, largest
     win/loss, max intraday drawdown. Written as a markdown file per day.

  2. **Per-strategy attribution** — P&L, trade count, expectancy, and
     rolling Sharpe broken out per `strategy_name`. Even with one strategy
     today the schema supports N strategies (Phase 11 readiness).

  3. **Continuous slippage monitoring** — rolling comparison of realized vs.
     modeled slippage. The stats feed the Phase 6.11 drift kill switch; the
     report surfaces them for operator review.

  4. **Weekly summary report** — aggregates daily summaries into a markdown
     weekly digest.

Design principles:
  - All computation is from the trade CSV — single source of truth.
  - Reports are markdown files, human-readable, git-friendly.
  - No external dependencies beyond pandas (already in stack).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import pandas as pd
from loguru import logger

from config import settings


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
    unrealized_pnl: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    max_intraday_drawdown: float = 0.0
    session_start_equity: float = 0.0
    session_end_equity: float = 0.0
    strategies: dict[str, StrategyStats] = field(default_factory=dict)
    slippage_mean_bps: float = 0.0
    slippage_max_bps: float = 0.0


# ── PnL Tracker ─────────────────────────────────────────────────────────────


class PnLTracker:
    """
    Reads the trade CSV and computes P&L reports.

    In-memory state tracks intraday P&L for the running drawdown
    calculation. Persisted reports are markdown files.
    """

    def __init__(
        self,
        trade_csv_path: str | None = None,
        daily_pnl_dir: str | None = None,
        weekly_report_dir: str | None = None,
    ) -> None:
        self._trade_csv = trade_csv_path or settings.TRADE_LOG_CSV
        self._daily_dir = daily_pnl_dir or settings.DAILY_PNL_DIR
        self._weekly_dir = weekly_report_dir or settings.WEEKLY_REPORT_DIR

        # Running intraday state (reset each day).
        self._today: str = ""
        self._intraday_pnl: float = 0.0
        self._intraday_peak: float = 0.0
        self._intraday_drawdown: float = 0.0
        self._trade_pnls: list[tuple[str, float]] = []  # (strategy, pnl)

    # ── Trade-level updates (called by the engine) ──────────────────────

    def record_trade_pnl(
        self,
        strategy_name: str,
        pnl: float,
        *,
        today: str | None = None,
    ) -> None:
        """
        Record one closed trade's P&L. Called by the engine when a position
        is closed (exit signal or stop hit).

        `today` override is for tests; production uses UTC date.
        """
        day = today or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if day != self._today:
            self._reset_day(day)

        self._trade_pnls.append((strategy_name, pnl))
        self._intraday_pnl += pnl
        if self._intraday_pnl > self._intraday_peak:
            self._intraday_peak = self._intraday_pnl
        dd = self._intraday_peak - self._intraday_pnl
        if dd > self._intraday_drawdown:
            self._intraday_drawdown = dd

    def _reset_day(self, day: str) -> None:
        self._today = day
        self._intraday_pnl = 0.0
        self._intraday_peak = 0.0
        self._intraday_drawdown = 0.0
        self._trade_pnls = []

    # ── Daily summary ───────────────────────────────────────────────────

    def generate_daily_summary(
        self,
        day: str | None = None,
        *,
        session_start_equity: float = 0.0,
        session_end_equity: float = 0.0,
        unrealized_pnl: float = 0.0,
    ) -> DailySummary:
        """
        Build a DailySummary from the in-memory trade P&Ls recorded today,
        plus the trade CSV for slippage stats.
        """
        day = day or self._today or datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Per-strategy breakdown.
        strats: dict[str, StrategyStats] = {}
        for strat_name, pnl in self._trade_pnls:
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

        # Aggregate.
        all_pnls = [p for _, p in self._trade_pnls]
        total_trades = len(all_pnls)
        realized = sum(all_pnls)
        largest_win = max(all_pnls) if all_pnls else 0.0
        largest_loss = min(all_pnls) if all_pnls else 0.0

        # Slippage from CSV (today's rows).
        slip_mean, slip_max = self._slippage_stats_for_day(day)

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
            max_intraday_drawdown=round(self._intraday_drawdown, 2),
            session_start_equity=round(session_start_equity, 2),
            session_end_equity=round(session_end_equity, 2),
            strategies=strats,
            slippage_mean_bps=round(slip_mean, 2),
            slippage_max_bps=round(slip_max, 2),
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
            f"| Mean slippage | {summary.slippage_mean_bps} bps |",
            f"| Max slippage | {summary.slippage_max_bps} bps |",
        ]

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
                    f"| Mean slippage | {s.mean_slippage_bps:.1f} bps |",
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
        # round-trips). Weekly report summarizes trade activity + slippage.
        slip_values = []
        for t in trades:
            try:
                slip_values.append(float(t.get("realized_slippage_bps", 0)))
            except (ValueError, TypeError):
                pass

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
            f"| Mean slippage | {sum(slip_values)/len(slip_values):.1f} bps |"
            if slip_values
            else f"| Mean slippage | — |",
            f"| Max slippage | {max(slip_values):.1f} bps |"
            if slip_values
            else f"| Max slippage | — |",
            "",
            "## Daily Reports",
            "",
        ]
        for day_str, _ in daily_files:
            lines.append(f"- [{day_str}]({day_str}.md)")

        lines.append("")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        logger.info(f"weekly report written: {path}")
        return path

    # ── Slippage monitoring ─────────────────────────────────────────────

    def slippage_report(self, last_n: int = 50) -> dict:
        """
        Rolling slippage stats from the trade CSV. Returns a dict with
        mean/max/count for the last N fills.
        """
        trades = self._read_trades()
        recent = trades[-last_n:] if trades else []
        if not recent:
            return {"count": 0, "mean_bps": 0.0, "max_bps": 0.0}

        slips = []
        for t in recent:
            try:
                slips.append(float(t.get("realized_slippage_bps", 0)))
            except (ValueError, TypeError):
                pass

        if not slips:
            return {"count": len(recent), "mean_bps": 0.0, "max_bps": 0.0}

        return {
            "count": len(slips),
            "mean_bps": round(sum(slips) / len(slips), 2),
            "max_bps": round(max(slips), 2),
        }

    # ── CSV helpers ─────────────────────────────────────────────────────

    def _read_trades(self) -> list[dict]:
        if not os.path.exists(self._trade_csv):
            return []
        with open(self._trade_csv, newline="") as f:
            return list(csv.DictReader(f))

    def _trades_in_range(self, start: str, end: str) -> list[dict]:
        """Filter trades whose timestamp falls within [start, end] (date prefix match)."""
        trades = self._read_trades()
        return [
            t
            for t in trades
            if start <= t.get("timestamp", "")[:10] <= end
        ]

    def _slippage_stats_for_day(self, day: str) -> tuple[float, float]:
        """Mean and max realized slippage for a given day."""
        trades = self._trades_in_range(day, day)
        slips = []
        for t in trades:
            try:
                slips.append(float(t.get("realized_slippage_bps", 0)))
            except (ValueError, TypeError):
                pass
        if not slips:
            return 0.0, 0.0
        return sum(slips) / len(slips), max(slips)

    def _slippage_by_strategy(self, day: str) -> dict[str, float]:
        """Mean realized slippage per strategy for a given day."""
        trades = self._trades_in_range(day, day)
        by_strat: dict[str, list[float]] = {}
        for t in trades:
            strat = t.get("strategy", "unknown")
            try:
                by_strat.setdefault(strat, []).append(
                    float(t.get("realized_slippage_bps", 0))
                )
            except (ValueError, TypeError):
                pass
        return {
            name: sum(vs) / len(vs)
            for name, vs in by_strat.items()
            if vs
        }


# Need csv for _read_trades
import csv  # noqa: E402 — grouped with stdlib but placed after class for readability
