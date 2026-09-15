"""Lifecycle-first paper-versus-backtest investigation.

The paper side is grouped by the durable ``position_uid``. A paper lifecycle
is matched to at most one backtest round trip through its recorded entry signal
bar; price proximity is never used as identity. Rows that pre-date the signal
anchor, use an unsupported instrument model, or were produced by a different
strategy configuration remain explicitly unresolved.

This report is advisory. It explains replay differences; it does not issue a
strategy-graduation or live-readiness verdict.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from loguru import logger

from backtest.runner import BacktestConfig, BacktestResult, run_backtest
from config import settings
from data.fetcher import fetch_symbol
from strategies.base import BaseStrategy
from strategies.identity import resolve_strategy_identity


_SUPPORTED_EQUITY_STRATEGIES = frozenset({
    "sma_crossover",
    "donchian_breakout",
})
_UNSUPPORTED_STRATEGY_REASONS = {
    "rsi_reversion": "unsupported_resting_gtc_limit_replay",
    "spy_options_reversion": "unsupported_option_replay",
    "credit_spread": "unsupported_mleg_replay",
    "leveraged_trend": "unsupported_cross_symbol_replay",
}
_MATCHED = "matched"


@dataclass(frozen=True)
class PaperLifecycle:
    """One paper position and its durable signal anchor."""

    position_uid: str
    strategy: str
    strategy_version: str | None
    strategy_config_hash: str | None
    bot_git_commit: str | None
    symbol: str
    position_type: str
    status: str
    signal_at: str | None
    signal_symbol: str | None
    timeframe: str | None
    data_feed: str | None
    first_fill_at: str | None
    closed_at: str | None
    entry_price: float | None
    exit_price: float | None
    realized_pnl: float
    operator_modified: bool
    signal_anchor_count: int


@dataclass(frozen=True)
class BacktestLifecycle:
    """One deterministic round trip extracted from vectorbt."""

    lifecycle_id: str
    symbol: str
    entry_at: pd.Timestamp
    exit_at: pd.Timestamp | None
    entry_price: float
    exit_price: float | None
    status: str


@dataclass(frozen=True)
class LifecycleComparison:
    """Result of trying to match one paper lifecycle once."""

    position_uid: str
    symbol: str
    status: str
    reason: str
    signal_at: str | None = None
    paper_entry_at: str | None = None
    backtest_lifecycle_id: str | None = None
    backtest_entry_at: str | None = None
    paper_entry_price: float | None = None
    backtest_entry_price: float | None = None
    entry_diff_bps: float | None = None
    paper_exit_at: str | None = None
    backtest_exit_at: str | None = None
    paper_exit_price: float | None = None
    backtest_exit_price: float | None = None
    exit_diff_bps: float | None = None
    realized_pnl: float = 0.0
    operator_modified: bool = False


@dataclass
class ReconciliationResult:
    """Advisory lifecycle matching result."""

    strategy_name: str
    symbols: list[str]
    start_date: str
    end_date: str
    paper_lifecycle_count: int
    backtest_lifecycle_count: int
    matched_count: int
    unresolved_count: int
    comparisons: list[LifecycleComparison] = field(default_factory=list)


class Reconciler:
    """Match lifecycle-backed paper positions to exact backtest round trips."""

    def __init__(
        self,
        strategy: BaseStrategy,
        symbols: list[str],
        start_date: str,
        end_date: str,
        *,
        trade_csv_path: str | None = None,
        forward_test_dir: str | None = None,
        backtest_config: BacktestConfig | None = None,
        timeframe: str = "1Day",
        history_lookback_days: int = 420,
        position_uids: list[str] | None = None,
    ) -> None:
        from reporting.logger import TradeLogger

        self.strategy = strategy
        self.symbols = list(symbols)
        self.start_date = start_date
        self.end_date = end_date
        self._trade_logger = TradeLogger(path=trade_csv_path)
        self._forward_test_dir = forward_test_dir or settings.FORWARD_TEST_DIR
        self._bt_config = backtest_config or BacktestConfig()
        self._timeframe = timeframe
        self._lookback = history_lookback_days
        self._position_uids = frozenset(position_uids or ())

    def run(self) -> ReconciliationResult:
        """Run the advisory reconciliation without fuzzy fallbacks."""
        papers = self._read_paper_lifecycles()
        identity = resolve_strategy_identity(
            self.strategy,
            data_feed=settings.ALPACA_DATA_FEED,
            timeframe=self._timeframe,
        )

        bt_results: dict[str, BacktestResult] = {}
        if self.strategy.name in _SUPPORTED_EQUITY_STRATEGIES:
            replay_end = max(
                [_utc_timestamp(self.end_date)]
                + [_utc_timestamp(p.closed_at) for p in papers if p.closed_at]
            ) + pd.Timedelta(days=7)
            for symbol in sorted({p.signal_symbol for p in papers if p.signal_symbol}):
                bt = self._run_backtest_for_symbol(
                    symbol, replay_end_date=replay_end.strftime("%Y-%m-%d")
                )
                if bt is not None:
                    bt_results[symbol] = bt

        backtests = {
            symbol: self._extract_backtest_lifecycles(
                result,
                strategy_config_hash=identity.strategy_config_hash,
            )
            for symbol, result in bt_results.items()
        }
        comparisons = self._match_lifecycles(
            papers,
            bt_results,
            backtests,
            expected_version=identity.strategy_version,
            expected_config_hash=identity.strategy_config_hash,
        )
        bt_count = sum(len(rows) for rows in backtests.values())
        matched = sum(row.status == _MATCHED for row in comparisons)
        return ReconciliationResult(
            strategy_name=self.strategy.name,
            symbols=self.symbols,
            start_date=self.start_date,
            end_date=self.end_date,
            paper_lifecycle_count=len(papers),
            backtest_lifecycle_count=bt_count,
            matched_count=matched,
            unresolved_count=len(comparisons) - matched,
            comparisons=comparisons,
        )

    def write_report(self, result: ReconciliationResult) -> str:
        """Write a markdown investigation report."""
        os.makedirs(self._forward_test_dir, exist_ok=True)
        path = os.path.join(
            self._forward_test_dir,
            f"{result.strategy_name}_{result.end_date}.md",
        )
        lines = [
            f"# Lifecycle Reconciliation — {result.strategy_name}",
            "",
            "**Advisory only — this report does not approve a strategy for live trading.**",
            "",
            "## Scope",
            "",
            f"- Entry-signal dates: {result.start_date} through {result.end_date}, inclusive",
            f"- Symbols: {', '.join(result.symbols)}",
            f"- Paper lifecycles: {result.paper_lifecycle_count}",
            f"- Backtest lifecycles: {result.backtest_lifecycle_count}",
            f"- Exact matches: {result.matched_count}",
            f"- Unresolved or unsupported: {result.unresolved_count}",
            "",
            "## Lifecycle comparisons",
            "",
            "| Paper lifecycle | Symbol | Match | Signal | Paper entry | BT entry | Entry diff | Paper exit | BT exit | Exit diff | Notes |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---:|---|",
        ]
        for row in result.comparisons:
            lines.append(
                "| "
                + " | ".join([
                    row.position_uid,
                    row.symbol,
                    row.status,
                    _date_text(row.signal_at),
                    _price_text(row.paper_entry_price),
                    _price_text(row.backtest_entry_price),
                    _bps_text(row.entry_diff_bps),
                    _price_text(row.paper_exit_price),
                    _price_text(row.backtest_exit_price),
                    _bps_text(row.exit_diff_bps),
                    row.reason + ("; operator-modified" if row.operator_modified else ""),
                ])
                + " |"
            )
        lines.extend([
            "",
            "Rows without a durable signal anchor are deliberately left unresolved. "
            "The tool never substitutes a similar price or a nearby trade.",
            "",
        ])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
        logger.info(f"lifecycle reconciliation report written: {path}")
        return path

    def _read_paper_lifecycles(self) -> list[PaperLifecycle]:
        """Read filled paper lifecycles and their permanent signal records."""
        db_path = Path(self._trade_logger.path)
        if not db_path.exists():
            return []
        conn = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        try:
            return self._read_paper_lifecycles_from_connection(conn)
        finally:
            conn.close()

    def _read_paper_lifecycles_from_connection(
        self,
        conn: sqlite3.Connection,
    ) -> list[PaperLifecycle]:
        """Query lifecycle evidence from an already-open connection."""
        conn.row_factory = sqlite3.Row
        if not self.symbols and not self._position_uids:
            return []
        lifecycle_rows = conn.execute(
            """
            SELECT p.position_uid, p.strategy, p.strategy_version,
                   p.strategy_config_hash, p.bot_git_commit, p.symbol,
                   p.position_type, p.status, p.first_fill_at, p.closed_at,
                   p.avg_entry_price, p.net_realized_pnl,
                   EXISTS(
                       SELECT 1 FROM position_lifecycle_orders o
                       WHERE o.position_uid = p.position_uid
                         AND o.origin_kind = 'operator'
                   ) AS operator_modified
            FROM position_lifecycle p
            WHERE p.strategy = ?
              AND p.first_fill_at IS NOT NULL
            ORDER BY p.first_fill_at, p.position_uid
            """,
            (self.strategy.name,),
        ).fetchall()

        candidates_by_uid: dict[str, list[sqlite3.Row]] = {}
        candidate_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'entry_candidate_decisions'"
        ).fetchone()
        if candidate_table is not None:
            candidate_rows = conn.execute(
                "SELECT position_uid, signal_at, signal_symbol, timeframe, data_feed "
                "FROM entry_candidate_decisions "
                "WHERE selected = 1 AND position_uid IS NOT NULL"
            ).fetchall()
            for candidate in candidate_rows:
                candidates_by_uid.setdefault(
                    str(candidate["position_uid"]), []
                ).append(candidate)

        output: list[PaperLifecycle] = []
        for row in lifecycle_rows:
            candidates = candidates_by_uid.get(str(row["position_uid"]), [])
            candidate = candidates[0] if len(candidates) == 1 else None
            signal_at = candidate["signal_at"] if candidate is not None else None
            signal_symbol = (
                candidate["signal_symbol"] if candidate is not None else None
            )
            position_uid = str(row["position_uid"])
            if self._position_uids and position_uid not in self._position_uids:
                continue
            scope_symbol = signal_symbol or _scope_symbol(str(row["symbol"]))
            scope_date = (signal_at or row["first_fill_at"] or "")[:10]
            if self.symbols and scope_symbol not in self.symbols:
                continue
            if not self.start_date <= scope_date <= self.end_date:
                continue
            exit_rows = conn.execute(
                "SELECT avg_fill_price, COALESCE(filled_qty, qty) "
                "FROM trades WHERE position_uid = ? "
                "AND realized_pnl IS NOT NULL AND avg_fill_price IS NOT NULL "
                "AND status IN ('filled', 'partial')",
                (row["position_uid"],),
            ).fetchall()
            output.append(PaperLifecycle(
                position_uid=position_uid,
                strategy=str(row["strategy"]),
                strategy_version=row["strategy_version"],
                strategy_config_hash=row["strategy_config_hash"],
                bot_git_commit=row["bot_git_commit"],
                symbol=str(row["symbol"]),
                position_type=str(row["position_type"]),
                status=str(row["status"]),
                signal_at=signal_at,
                signal_symbol=signal_symbol,
                timeframe=(candidate["timeframe"] if candidate is not None else None),
                data_feed=(candidate["data_feed"] if candidate is not None else None),
                first_fill_at=row["first_fill_at"],
                closed_at=row["closed_at"],
                entry_price=_optional_float(row["avg_entry_price"]),
                exit_price=_weighted_price(exit_rows),
                realized_pnl=float(row["net_realized_pnl"] or 0.0),
                operator_modified=bool(row["operator_modified"]),
                signal_anchor_count=len(candidates),
            ))
        return output

    def _run_backtest_for_symbol(
        self,
        symbol: str,
        *,
        replay_end_date: str | None = None,
    ) -> BacktestResult | None:
        """Replay one supported equity symbol using the live data feed."""
        try:
            end_dt = datetime.fromisoformat(
                (replay_end_date or self.end_date) + "T23:59:59+00:00"
            )
            start_dt = datetime.fromisoformat(self.start_date + "T00:00:00+00:00")
            df, _ = fetch_symbol(
                symbol,
                start_dt - timedelta(days=self._lookback),
                end_dt,
                timeframe=self._timeframe,
                feed=settings.ALPACA_DATA_FEED,
            )
            if df.empty:
                logger.warning(f"reconcile: no bars for {symbol}")
                return None
            return run_backtest(
                self.strategy,
                df,
                self._bt_config,
                symbol=symbol,
                atr_stop_mult=settings.ATR_STOP_MULTIPLIER,
            )
        except Exception as exc:
            logger.error(f"reconcile: backtest for {symbol} failed: {exc}")
            return None

    @staticmethod
    def _extract_backtest_lifecycles(
        result: BacktestResult,
        *,
        strategy_config_hash: str,
    ) -> list[BacktestLifecycle]:
        """Extract deterministic, complete vectorbt round trips."""
        records = result.portfolio.trades.records_readable
        output: list[BacktestLifecycle] = []
        for ordinal, (_, row) in enumerate(records.iterrows()):
            entry_at = pd.Timestamp(row["Entry Timestamp"])
            status = str(row.get("Status", "unknown")).lower()
            exit_raw = row.get("Exit Timestamp")
            exit_at = (
                None
                if status != "closed" or pd.isna(exit_raw)
                else pd.Timestamp(exit_raw)
            )
            raw_id = (
                f"{result.strategy_name}|{strategy_config_hash}|{result.symbol}|"
                f"{entry_at.isoformat()}|{ordinal}"
            )
            output.append(BacktestLifecycle(
                lifecycle_id="bt_" + hashlib.sha256(raw_id.encode()).hexdigest()[:16],
                symbol=result.symbol,
                entry_at=entry_at,
                exit_at=exit_at,
                entry_price=float(row["Avg Entry Price"]),
                exit_price=(
                    None if status != "closed" or pd.isna(row.get("Avg Exit Price"))
                    else float(row["Avg Exit Price"])
                ),
                status=status,
            ))
        return output

    def _match_lifecycles(
        self,
        papers: list[PaperLifecycle],
        results: dict[str, BacktestResult],
        backtests: dict[str, list[BacktestLifecycle]],
        *,
        expected_version: str,
        expected_config_hash: str,
    ) -> list[LifecycleComparison]:
        used: set[str] = set()
        output: list[LifecycleComparison] = []
        for paper in papers:
            failure = self._pre_match_failure(
                paper,
                expected_version=expected_version,
                expected_config_hash=expected_config_hash,
            )
            if failure is not None:
                output.append(_unresolved(paper, failure))
                continue

            assert paper.signal_symbol is not None and paper.signal_at is not None
            result = results.get(paper.signal_symbol)
            if result is None:
                output.append(_unresolved(paper, "backtest_unavailable"))
                continue
            expected_at = _next_execution_bar(result.entries_executed.index, paper.signal_at)
            if expected_at is None:
                output.append(_unresolved(paper, "signal_bar_not_in_replay"))
                continue
            try:
                if not bool(result.entries_executed.loc[expected_at]):
                    output.append(_unresolved(paper, "backtest_no_entry_on_expected_bar"))
                    continue
            except (KeyError, TypeError):
                output.append(_unresolved(paper, "signal_bar_not_in_replay"))
                continue

            all_matches = [
                row for row in backtests.get(paper.signal_symbol, [])
                if _same_timestamp(row.entry_at, expected_at)
            ]
            matches = [row for row in all_matches if row.lifecycle_id not in used]
            if len(matches) != 1:
                reason = (
                    "backtest_lifecycle_already_used"
                    if all_matches and not matches
                    else "backtest_lifecycle_missing"
                    if not all_matches
                    else "ambiguous_backtest_lifecycle"
                )
                output.append(_unresolved(paper, reason))
                continue
            bt = matches[0]
            used.add(bt.lifecycle_id)
            output.append(LifecycleComparison(
                position_uid=paper.position_uid,
                symbol=paper.symbol,
                status=_MATCHED,
                reason="exact_signal_bar",
                signal_at=paper.signal_at,
                paper_entry_at=paper.first_fill_at,
                backtest_lifecycle_id=bt.lifecycle_id,
                backtest_entry_at=bt.entry_at.isoformat(),
                paper_entry_price=paper.entry_price,
                backtest_entry_price=bt.entry_price,
                entry_diff_bps=_diff_bps(paper.entry_price, bt.entry_price),
                paper_exit_at=paper.closed_at,
                backtest_exit_at=bt.exit_at.isoformat() if bt.exit_at is not None else None,
                paper_exit_price=paper.exit_price,
                backtest_exit_price=bt.exit_price,
                exit_diff_bps=_diff_bps(paper.exit_price, bt.exit_price),
                realized_pnl=paper.realized_pnl,
                operator_modified=paper.operator_modified,
            ))
        return output

    def _pre_match_failure(
        self,
        paper: PaperLifecycle,
        *,
        expected_version: str,
        expected_config_hash: str,
    ) -> str | None:
        if self.strategy.name not in _SUPPORTED_EQUITY_STRATEGIES:
            return _UNSUPPORTED_STRATEGY_REASONS.get(
                self.strategy.name, "unsupported_strategy_replay"
            )
        if paper.position_type != "single_leg" or _looks_like_occ(paper.symbol):
            return "unsupported_instrument_model"
        if paper.signal_anchor_count == 0:
            return "missing_signal_anchor"
        if paper.signal_anchor_count != 1:
            return "ambiguous_signal_anchor"
        if paper.strategy_version != expected_version:
            return "strategy_version_mismatch"
        if paper.strategy_config_hash != expected_config_hash:
            return "strategy_config_mismatch"
        if paper.timeframe != self._timeframe:
            return "timeframe_mismatch"
        if paper.data_feed != settings.ALPACA_DATA_FEED:
            return "data_feed_mismatch"
        if paper.signal_symbol != paper.symbol:
            return "unsupported_cross_symbol_replay"
        return None


def _weighted_price(rows: list[Any]) -> float | None:
    total_qty = 0.0
    total_value = 0.0
    for row in rows:
        price = float(row[0])
        qty = float(row[1] or 0.0)
        if price > 0 and qty > 0:
            total_value += price * qty
            total_qty += qty
    return total_value / total_qty if total_qty else None


def _optional_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _next_execution_bar(index: pd.Index, signal_at: str) -> pd.Timestamp | None:
    signal = pd.Timestamp(signal_at)
    timestamps = [pd.Timestamp(value) for value in index]
    exact = [i for i, value in enumerate(timestamps) if _same_timestamp(value, signal)]
    if not exact:
        exact = [i for i, value in enumerate(timestamps) if value.date() == signal.date()]
    if len(exact) != 1 or exact[0] + 1 >= len(timestamps):
        return None
    return timestamps[exact[0] + 1]


def _utc_timestamp(value: str) -> pd.Timestamp:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _same_timestamp(left: pd.Timestamp, right: pd.Timestamp) -> bool:
    left = pd.Timestamp(left)
    right = pd.Timestamp(right)
    if left.tzinfo is None:
        left = left.tz_localize("UTC")
    else:
        left = left.tz_convert("UTC")
    if right.tzinfo is None:
        right = right.tz_localize("UTC")
    else:
        right = right.tz_convert("UTC")
    return left == right


def _diff_bps(paper: float | None, backtest: float | None) -> float | None:
    if paper is None or backtest is None or paper <= 0:
        return None
    return round((paper - backtest) / paper * 10_000, 1)


def _unresolved(paper: PaperLifecycle, reason: str) -> LifecycleComparison:
    return LifecycleComparison(
        position_uid=paper.position_uid,
        symbol=paper.symbol,
        status="unresolved",
        reason=reason,
        signal_at=paper.signal_at,
        paper_entry_at=paper.first_fill_at,
        paper_entry_price=paper.entry_price,
        paper_exit_at=paper.closed_at,
        paper_exit_price=paper.exit_price,
        realized_pnl=paper.realized_pnl,
        operator_modified=paper.operator_modified,
    )


def _looks_like_occ(symbol: str) -> bool:
    return re.fullmatch(r"[A-Z]{1,6}\d{6}[CP]\d{8}", symbol) is not None


def _scope_symbol(symbol: str) -> str:
    match = re.fullmatch(r"([A-Z]{1,6})\d{6}[CP]\d{8}", symbol)
    return match.group(1) if match is not None else symbol


def _price_text(value: float | None) -> str:
    return "—" if value is None else f"${value:.2f}"


def _bps_text(value: float | None) -> str:
    return "—" if value is None else f"{value:+.1f} bps"


def _date_text(value: str | None) -> str:
    return "—" if not value else value[:10]
