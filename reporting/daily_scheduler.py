"""Final daily P&L reports for completed US-equity sessions.

This scheduler is deliberately separate from process shutdown. A recycle is
not end-of-day, and a laptop that sleeps through the close must be able to
backfill the completed session on its next cycle.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, TYPE_CHECKING
from zoneinfo import ZoneInfo

from loguru import logger

from data import market_calendar
from reporting.pnl import max_drawdown_from_equity_path, unrealized_pnl_from_positions

if TYPE_CHECKING:
    from engine.trader import TradingEngine
    from execution.broker import AlpacaBroker
    from reporting.alerts import AlertDispatcher
    from reporting.pnl import PnLTracker


_NEW_YORK = ZoneInfo("America/New_York")
_FINAL_REPORT_TIME = time(16, 5)


@dataclass
class DailyPnLReportScheduler:
    """Write at most one missing final session report per engine cycle."""

    tracker: "PnLTracker"
    broker: "AlpacaBroker"
    engine: "TradingEngine"
    dispatcher: "AlertDispatcher"
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    lookback_days: int = 14

    def __call__(self) -> None:
        try:
            self._maybe_finalize_one()
        except Exception as exc:  # noqa: BLE001 - reporting cannot stop trading
            logger.warning(
                f"daily P&L scheduler failed (trading unaffected): {exc}"
            )

    def _maybe_finalize_one(self) -> None:
        now = self.clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        now_et = now.astimezone(_NEW_YORK)
        last_eligible = now_et.date()
        if now_et.time() < _FINAL_REPORT_TIME:
            last_eligible -= timedelta(days=1)

        first_eligible = last_eligible - timedelta(days=self.lookback_days)
        sessions = market_calendar.trading_sessions(
            first_eligible, last_eligible,
        )
        if sessions is None:
            logger.warning(
                "daily P&L scheduler: Alpaca session calendar unavailable; "
                "finalization deferred"
            )
            return

        pending = [
            session for session in sorted(sessions, reverse=True)
            if not self._is_finalized(session)
        ]
        if not pending:
            return

        # Newest first: the most recent operator-facing report should not wait
        # behind a historical backlog. Older sessions catch up one per cycle.
        session = pending[0]
        is_latest = session == max(sessions)
        self._finalize(
            session,
            now=now,
            include_current_marks=session == now_et.date(),
            send_alert=is_latest,
        )

    def _is_finalized(self, session: date) -> bool:
        return self.tracker.is_daily_report_final(session.isoformat())

    def _finalize(
        self,
        session: date,
        *,
        now: datetime,
        include_current_marks: bool,
        send_alert: bool,
    ) -> None:
        start_et = datetime.combine(session, time.min, tzinfo=_NEW_YORK)
        # Keep same-evening and catch-up reports comparable. Alpaca clamps a
        # future end to request time, so asking through the next midnight made
        # a report's equity path depend on when the scheduler happened to run.
        end_et = datetime.combine(session, _FINAL_REPORT_TIME, tzinfo=_NEW_YORK)
        bounded_end = min(now, end_et.astimezone(timezone.utc))
        equity_path = self.broker.get_intraday_equity_path(
            start_et.astimezone(timezone.utc),
            bounded_end,
        )
        if not equity_path:
            raise RuntimeError(
                f"broker equity history unavailable for {session.isoformat()}"
            )
        start_equity = equity_path[0]
        end_equity = equity_path[-1]
        drawdown = max_drawdown_from_equity_path(equity_path)

        unrealized = None
        snapshot = getattr(self.engine, "_last_snapshot", None)
        if include_current_marks and snapshot is not None:
            unrealized = unrealized_pnl_from_positions(
                snapshot.account.open_positions.values()
            )
        if include_current_marks:
            locally_observed = getattr(
                self.engine, "max_intraday_drawdown", None,
            )
            if locally_observed is not None:
                drawdown = max(float(locally_observed), drawdown or 0.0)

        summary = self.tracker.generate_daily_summary(
            day=session.isoformat(),
            session_start_equity=start_equity,
            session_end_equity=end_equity,
            unrealized_pnl=unrealized,
            max_intraday_drawdown=drawdown,
            finalized_at=now.astimezone(timezone.utc).isoformat(),
        )
        path = self.tracker.write_daily_report(summary)
        logger.info(
            f"daily P&L scheduler: finalized {session.isoformat()} at {path}"
        )

        if send_alert:
            total_wins = sum(s.wins for s in summary.strategies.values())
            win_rate = (
                total_wins / summary.total_trades
                if summary.total_trades else 0.0
            )
            self.dispatcher.eod_summary(
                daily_pnl=summary.realized_pnl,
                trade_count=summary.total_trades,
                win_rate=win_rate,
            )
