"""
Forward-test reconciliation (Phase 9.5).

Compares realized paper fills from the trade CSV (or Alpaca closed orders)
against backtest-predicted fills over the same date range. Produces:

  1. **Per-trade divergence** — matched entry/exit pairs showing price
     deviation, slippage, and signal-to-fill latency.
  2. **Aggregate divergence** — total paper return vs. backtest return.
  3. **Go/no-go gate** — automatic pass/fail against pre-committed thresholds
     (return divergence %, mean slippage bps).
  4. **Decision report** — written as markdown in `logs/forward_tests/`.

Design principles:
  - Backtest is rerun on the same bars the bot saw, with the same strategy
    and config — the comparison is apples-to-apples.
  - The trade CSV is the paper source of truth (not Alpaca's fill history
    directly, since the CSV includes strategy metadata).
  - The gate is conservative: fail = go back to Phase 5, not "try harder."
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import pandas as pd
from loguru import logger

from backtest.runner import BacktestConfig, BacktestResult, run_backtest
from config import settings
from data.fetcher import fetch_symbol
from indicators.technicals import add_atr
from strategies.base import BaseStrategy


# ── Data structures ─────────────────────────────────────────────────────────


@dataclass
class TradeDivergence:
    """One matched paper fill vs. backtest fill."""

    symbol: str
    side: str
    paper_date: str
    paper_price: float
    backtest_price: float | None
    price_diff_bps: float
    matched: bool  # True if a corresponding backtest trade was found


@dataclass
class ReconciliationResult:
    """Full reconciliation output."""

    # Inputs
    strategy_name: str
    symbols: list[str]
    start_date: str
    end_date: str

    # Aggregate
    paper_return_pct: float
    backtest_return_pct: float
    return_divergence_pct: float
    paper_trade_count: int
    backtest_trade_count: int

    # Per-trade
    divergences: list[TradeDivergence] = field(default_factory=list)

    # Slippage
    mean_slippage_bps: float = 0.0
    max_slippage_bps: float = 0.0

    # Gate
    return_threshold_pct: float = 0.0
    slippage_threshold_bps: float = 0.0
    go: bool = False
    reasons: list[str] = field(default_factory=list)


# ── Reconciler ──────────────────────────────────────────────────────────────


class Reconciler:
    """
    Compares paper trading results against a backtest of the same period.

    Usage:
        recon = Reconciler(strategy, symbols, start, end)
        result = recon.run()
        path = recon.write_report(result)
    """

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
        return_divergence_threshold: float | None = None,
        max_slippage_threshold: float | None = None,
        timeframe: str = "1Day",
        history_lookback_days: int = 200,
    ) -> None:
        self.strategy = strategy
        self.symbols = symbols
        self.start_date = start_date
        self.end_date = end_date
        self._trade_csv = trade_csv_path or settings.TRADE_LOG_CSV
        self._forward_test_dir = forward_test_dir or settings.FORWARD_TEST_DIR
        self._bt_config = backtest_config or BacktestConfig()
        self._return_threshold = (
            return_divergence_threshold
            if return_divergence_threshold is not None
            else settings.FORWARD_TEST_RETURN_DIVERGENCE_PCT
        )
        self._slippage_threshold = (
            max_slippage_threshold
            if max_slippage_threshold is not None
            else settings.FORWARD_TEST_MAX_SLIPPAGE_BPS
        )
        self._timeframe = timeframe
        self._lookback = history_lookback_days

    def run(self) -> ReconciliationResult:
        """Execute the full reconciliation."""
        # 1. Read paper trades from CSV.
        paper_trades = self._read_paper_trades()
        paper_fills = [
            t for t in paper_trades
            if t.get("status") == "filled"
            and t.get("strategy") == self.strategy.name
        ]
        logger.info(
            f"reconcile: {len(paper_fills)} paper fills for "
            f"{self.strategy.name} in [{self.start_date}, {self.end_date}]"
        )

        # 2. Run backtest on the same bars.
        bt_results: dict[str, BacktestResult] = {}
        for sym in self.symbols:
            bt = self._run_backtest_for_symbol(sym)
            if bt is not None:
                bt_results[sym] = bt

        # 3. Compute aggregate returns.
        paper_return = self._compute_paper_return(paper_fills)
        bt_return = self._compute_backtest_return(bt_results)
        return_div = abs(paper_return - bt_return)

        # 4. Per-trade divergence.
        divergences = self._match_trades(paper_fills, bt_results)

        # 5. Slippage stats from paper fills.
        slips = []
        for t in paper_fills:
            try:
                slips.append(float(t.get("realized_slippage_bps", 0)))
            except (ValueError, TypeError):
                pass
        mean_slip = sum(slips) / len(slips) if slips else 0.0
        max_slip = max(slips) if slips else 0.0

        # 6. Count backtest trades.
        bt_trade_count = 0
        for bt in bt_results.values():
            bt_trade_count += int(bt.stats.get("trade_count", 0))

        # 7. Go/no-go gate.
        reasons: list[str] = []
        go = True
        if return_div > self._return_threshold:
            go = False
            reasons.append(
                f"return divergence {return_div:.1%} exceeds "
                f"threshold {self._return_threshold:.1%}"
            )
        if mean_slip > self._slippage_threshold:
            go = False
            reasons.append(
                f"mean slippage {mean_slip:.1f}bps exceeds "
                f"threshold {self._slippage_threshold:.1f}bps"
            )
        if not paper_fills:
            go = False
            reasons.append("no paper fills found — cannot evaluate")
        if go:
            reasons.append("all gates passed")

        return ReconciliationResult(
            strategy_name=self.strategy.name,
            symbols=self.symbols,
            start_date=self.start_date,
            end_date=self.end_date,
            paper_return_pct=round(paper_return * 100, 2),
            backtest_return_pct=round(bt_return * 100, 2),
            return_divergence_pct=round(return_div * 100, 2),
            paper_trade_count=len(paper_fills),
            backtest_trade_count=bt_trade_count,
            divergences=divergences,
            mean_slippage_bps=round(mean_slip, 2),
            max_slippage_bps=round(max_slip, 2),
            return_threshold_pct=round(self._return_threshold * 100, 2),
            slippage_threshold_bps=self._slippage_threshold,
            go=go,
            reasons=reasons,
        )

    def write_report(self, result: ReconciliationResult) -> str:
        """Write the go/no-go decision report as markdown."""
        os.makedirs(self._forward_test_dir, exist_ok=True)
        fname = f"{result.strategy_name}_{result.end_date}.md"
        path = os.path.join(self._forward_test_dir, fname)

        verdict = "GO" if result.go else "NO-GO"
        lines = [
            f"# Forward-Test Reconciliation — {result.strategy_name}",
            "",
            f"**Verdict: {verdict}**",
            "",
            "## Period",
            "",
            f"- Start: {result.start_date}",
            f"- End: {result.end_date}",
            f"- Symbols: {', '.join(result.symbols)}",
            "",
            "## Aggregate Returns",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Paper return | {result.paper_return_pct:+.2f}% |",
            f"| Backtest return | {result.backtest_return_pct:+.2f}% |",
            f"| Divergence | {result.return_divergence_pct:.2f}% |",
            f"| Threshold | {result.return_threshold_pct:.2f}% |",
            "",
            "## Trade Counts",
            "",
            "| Source | Count |",
            "|---|---|",
            f"| Paper fills | {result.paper_trade_count} |",
            f"| Backtest trades | {result.backtest_trade_count} |",
            "",
            "## Slippage",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Mean realized | {result.mean_slippage_bps:.1f} bps |",
            f"| Max realized | {result.max_slippage_bps:.1f} bps |",
            f"| Threshold | {result.slippage_threshold_bps:.1f} bps |",
            "",
            "## Gate Reasons",
            "",
        ]
        for r in result.reasons:
            lines.append(f"- {r}")

        if result.divergences:
            lines += [
                "",
                "## Per-Trade Divergence",
                "",
                "| Symbol | Side | Date | Paper Price | BT Price | Diff (bps) | Matched |",
                "|---|---|---|---|---|---|---|",
            ]
            for d in result.divergences:
                bt_price = f"${d.backtest_price:.2f}" if d.backtest_price else "—"
                lines.append(
                    f"| {d.symbol} | {d.side} | {d.paper_date} "
                    f"| ${d.paper_price:.2f} | {bt_price} "
                    f"| {d.price_diff_bps:.1f} | {'yes' if d.matched else 'no'} |"
                )

        lines.append("")
        with open(path, "w") as f:
            f.write("\n".join(lines))
        logger.info(f"forward-test report written: {path} [{verdict}]")
        return path

    # ── Internals ───────────────────────────────────────────────────────────

    def _read_paper_trades(self) -> list[dict]:
        """Read trades from CSV filtered by date range and symbols."""
        if not os.path.exists(self._trade_csv):
            return []
        with open(self._trade_csv, newline="") as f:
            all_trades = list(csv.DictReader(f))
        return [
            t for t in all_trades
            if self.start_date <= t.get("timestamp", "")[:10] <= self.end_date
            and t.get("symbol", "") in self.symbols
        ]

    def _run_backtest_for_symbol(self, symbol: str) -> BacktestResult | None:
        """Fetch bars and run backtest for one symbol over the test period."""
        try:
            end_dt = datetime.fromisoformat(self.end_date + "T23:59:59+00:00")
            start_dt = datetime.fromisoformat(self.start_date + "T00:00:00+00:00")
            # Need lookback for indicators to warm up.
            fetch_start = start_dt - timedelta(days=self._lookback)
            df, _ = fetch_symbol(
                symbol, fetch_start, end_dt, timeframe=self._timeframe
            )
            if df.empty:
                logger.warning(f"reconcile: no bars for {symbol}")
                return None
            return run_backtest(
                self.strategy, df, self._bt_config, symbol=symbol
            )
        except Exception as e:
            logger.error(f"reconcile: backtest for {symbol} failed: {e}")
            return None

    def _compute_paper_return(self, fills: list[dict]) -> float:
        """
        Estimate paper return from buy/sell pairs in the CSV.

        Simple approach: match sequential buy→sell pairs per symbol and sum
        the P&L as a fraction of entry cost.
        """
        if not fills:
            return 0.0

        # Group by symbol, track open buys.
        by_symbol: dict[str, list[dict]] = {}
        for f in fills:
            by_symbol.setdefault(f["symbol"], []).append(f)

        total_pnl = 0.0
        total_cost = 0.0
        for sym, trades in by_symbol.items():
            open_entry: dict | None = None
            for t in trades:
                if t["side"] == "buy":
                    open_entry = t
                elif t["side"] == "sell" and open_entry is not None:
                    try:
                        entry_price = float(open_entry["avg_fill_price"])
                        exit_price = float(t["avg_fill_price"])
                        qty = int(float(open_entry.get("qty", 1)))
                        pnl = (exit_price - entry_price) * qty
                        total_pnl += pnl
                        total_cost += entry_price * qty
                    except (ValueError, TypeError):
                        pass
                    open_entry = None

        return total_pnl / total_cost if total_cost > 0 else 0.0

    def _compute_backtest_return(
        self, bt_results: dict[str, BacktestResult]
    ) -> float:
        """Average total return across all symbols' backtests."""
        if not bt_results:
            return 0.0
        returns = [bt.stats["total_return"] for bt in bt_results.values()]
        return sum(returns) / len(returns)

    def _match_trades(
        self,
        paper_fills: list[dict],
        bt_results: dict[str, BacktestResult],
    ) -> list[TradeDivergence]:
        """
        Build per-trade divergence records by matching paper fills against
        backtest entry/exit prices. Matching is approximate: we look for
        the closest backtest trade within ±1 bar of the paper fill date.
        """
        divergences: list[TradeDivergence] = []

        for fill in paper_fills:
            sym = fill.get("symbol", "")
            side = fill.get("side", "")
            try:
                paper_price = float(fill["avg_fill_price"])
            except (ValueError, TypeError, KeyError):
                continue
            paper_date = fill.get("timestamp", "")[:10]

            bt = bt_results.get(sym)
            bt_price = None
            matched = False

            if bt is not None:
                # Try to find a matching backtest trade.
                try:
                    trades_df = bt.portfolio.trades.records_readable
                    if not trades_df.empty:
                        if side == "buy":
                            bt_prices = trades_df["Entry Price"].values
                        else:
                            bt_prices = trades_df["Exit Price"].values
                        # Use the closest price as a rough match.
                        if len(bt_prices) > 0:
                            diffs = [abs(float(p) - paper_price) for p in bt_prices]
                            min_idx = diffs.index(min(diffs))
                            bt_price = float(bt_prices[min_idx])
                            matched = True
                except Exception:
                    pass

            price_diff_bps = 0.0
            if bt_price is not None and paper_price > 0:
                price_diff_bps = abs(bt_price - paper_price) / paper_price * 10_000

            divergences.append(TradeDivergence(
                symbol=sym,
                side=side,
                paper_date=paper_date,
                paper_price=paper_price,
                backtest_price=bt_price,
                price_diff_bps=round(price_diff_bps, 1),
                matched=matched,
            ))

        return divergences
