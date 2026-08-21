"""
Health-review scheduler — Monday-completed-week + first-of-month hooks.

Consumed by forward_test.py as `engine.start(post_cycle_hook=...)`.
The hook checks the current date and fires the appropriate reviewer
window when the trigger conditions are met. Idempotent — calling
multiple times on the same trigger day produces one report.

**Why Monday and not Sunday EOD:** an earlier iteration fired on the
first Sunday cycle, but that was the wrong shape for two reasons:
  1. It fires on an in-progress week. Whenever the bot's first
     Sunday cycle runs (often Sunday morning UTC = Saturday evening
     US), the trailing-7-day window covers a still-open week, and
     the in-memory idempotency suppresses any later Sunday-EOD
     cycle. The canonical weekly report ends up based on an
     incomplete week.
  2. The lifecycle-counter table is keyed by ISO Monday (the engine
     flush in PLAN 11.10f computes period_start = Monday of current
     ISO week). A Sunday-to-Sunday rolling window misaligns with
     that storage shape.

Firing on Monday with `period_end = this Monday` gives a clean
previous-Mon → this-Mon completed week that lines up with the
lifecycle counter rows. Monday 00:00 UTC is also close to "right
after the trading week ended" (Sunday evening US time), which is
the operationally intended cadence.

Per design §10 cadence + §1.2 invariant:
  - Monday (weekday=0, UTC) → weekly report for the *completed*
    Mon→Mon week ending at the current Monday
  - First of month (UTC) → monthly report
  - The hook NEVER modifies trading state; it only triggers the
    reviewer which writes a markdown report + dispatches alerts.
  - Engine-loop hook failures are absorbed by the engine's
    try/except wrap (engine/trader.py:start post_cycle_hook).

Idempotency is double-protected:
  1. In-memory: the scheduler tracks `last_weekly_fired_date` and
     `last_monthly_fired_date` and short-circuits when called
     repeatedly on the same trigger day.
  2. On-disk: even without (1), the lifecycle_counters table's
     UNIQUE(period_type, period_start, strategy_name) constraint
     would dedupe upserts, and atomic-write of the markdown report
     would just overwrite the previous file.

PR #22 reviewer caught the Sunday-firing bug; this is the fix.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, TYPE_CHECKING

from loguru import logger

from reporting.alerts import AlertDispatcher
from strategies.health.reviewer import run_review, window_from_args

if TYPE_CHECKING:  # pragma: no cover - import cycle guard
    from reporting.pnl import PnLTracker


# Monday in Python's weekday() is 0 (Monday=0, ..., Sunday=6). Firing
# here means the weekly report covers the previous Mon → this Mon
# completed week — aligned with the lifecycle counter table's ISO
# Monday period_start.
_MONDAY = 0


@dataclass
class HealthReviewScheduler:
    """Stateful scheduler — tracks last-fired dates to enforce
    "fire once per trigger day" idempotency. Constructed once in
    forward_test.py and passed to engine.start(post_cycle_hook=...).

    Mutable (unlike most health dataclasses) because last_*_fired
    dates advance over the lifetime of a forward-test run.

    Dependencies are injected so tests can mock the conn / dispatcher /
    clock cleanly:
      - `conn_factory`: callable returning an open SQLite connection
        (forward_test passes a lambda returning the existing trade
        logger's connection)
      - `dispatcher`: an AlertDispatcher to fire transition alerts
      - `clock`: callable returning the current UTC datetime; defaults
        to wall-clock
    """

    conn_factory: Callable[[], sqlite3.Connection]
    dispatcher: AlertDispatcher
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    # Optional PnLTracker. When supplied, the Monday trigger also writes
    # the weekly P&L digest, reusing this scheduler's proven once-per-day
    # idempotency rather than standing up a second timer.
    #
    # `generate_weekly_report` existed since Phase 9 and was called from
    # nothing but tests and phase9_verify — so in ~4 months of operation
    # no weekly P&L report was ever written, and `logs/weekly_reports/`
    # did not exist. Discovered 2026-08-03 when the operator went looking
    # for it. Left optional so existing constructions (and tests) keep
    # working without a tracker.
    pnl_tracker: "PnLTracker | None" = None
    last_weekly_fired_date: date | None = None
    last_monthly_fired_date: date | None = None
    last_long_window_fired_date: date | None = None

    def __call__(self) -> None:
        """Engine's post_cycle_hook entry point.

        Checks the current date and fires the appropriate reviewer
        if the trigger conditions are met. Logs and continues on
        reviewer failure — never raises into the engine loop.
        """
        try:
            today = self.clock().date()
            self._maybe_fire_weekly(today)
            self._maybe_fire_monthly(today)
            self._maybe_fire_long_window(today)
        except Exception as exc:  # noqa: BLE001
            # Belt-and-suspenders — the engine also wraps the hook
            # call in try/except, but log the actual error here too
            # so the operator sees it in the bot log.
            logger.warning(
                f"health-review scheduler failed (trading not "
                f"affected): {exc}"
            )

    def _maybe_fire_weekly(self, today: date) -> None:
        # Monday only — fires the report for the previous Mon → this
        # Mon completed week. window_from_args("weekly",
        # end_date=Monday) gives period_start = previous Monday,
        # period_end = this Monday (a clean completed-week window).
        if today.weekday() != _MONDAY:
            return
        # Once per Monday.
        if self.last_weekly_fired_date == today:
            return
        logger.info(
            f"health-review scheduler: firing WEEKLY review for "
            f"completed week ending {today.isoformat()}"
        )
        window = window_from_args("weekly", end_date=today)
        # Both Monday artefacts are attempted independently. `_run` does
        # not catch its own failures, so without this the health review
        # raising (a bad conn, a reviewer bug) would propagate to
        # `__call__` and silently skip the P&L digest entirely — the
        # isolation was one-directional and the weaker direction was the
        # one that mattered, since the P&L write is the newer path.
        health_ok = True
        try:
            self._run(window)
        except Exception as exc:  # noqa: BLE001
            health_ok = False
            logger.warning(
                f"weekly health review failed for week ending "
                f"{today.isoformat()} (trading unaffected; weekly P&L "
                f"digest still attempted): {exc}"
            )
        self._maybe_write_weekly_pnl(today)
        # Mark fired regardless: retrying on the next cycle would re-run
        # whichever half succeeded, and both writers are already
        # idempotent-by-overwrite. A failure is logged, not retried in a
        # loop that fires every cycle for the rest of the day.
        self.last_weekly_fired_date = today
        if not health_ok:
            return

    def _maybe_write_weekly_pnl(self, today: date) -> None:
        """Write the weekly P&L digest alongside the health review.

        `week_end` is **Sunday**, not today. The health review's window is
        previous-Monday → this-Monday, but this hook fires *during*
        Monday's session, so today's daily file is either absent or a
        stub with zero trades. Ending on Sunday covers the fully
        completed Mon→Sun week and never averages in a partial day.

        Isolated in its own try/except: a P&L failure must not prevent
        the health review from being recorded, and the reverse is
        already guaranteed by `__call__`'s wrapper. Neither can reach
        the trading loop.
        """
        if self.pnl_tracker is None:
            return
        week_end = today - timedelta(days=1)
        try:
            path = self.pnl_tracker.generate_weekly_report(
                week_end=week_end.isoformat(),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"weekly P&L report failed for week ending "
                f"{week_end.isoformat()} (trading and health review "
                f"unaffected): {exc}"
            )
            return
        if path is None:
            logger.info(
                f"weekly P&L report: no daily summaries in the week "
                f"ending {week_end.isoformat()} — nothing to aggregate"
            )
        else:
            logger.info(f"weekly P&L report written: {path}")

    def _maybe_fire_monthly(self, today: date) -> None:
        # First of month only.
        if today.day != 1:
            return
        # Once per month (track by first-of-month date).
        if self.last_monthly_fired_date == today:
            return
        logger.info(
            f"health-review scheduler: firing MONTHLY review for "
            f"period ending {today.isoformat()}"
        )
        window = window_from_args("monthly", end_date=today)
        self._run(window)
        self.last_monthly_fired_date = today

    def _maybe_fire_long_window(self, today: date) -> None:
        """Fire the trailing-365-day review on the first of the month.

        Why this exists: the weekly and monthly windows filter trades by
        date (`period_start <= timestamp < period_end`), so their sample
        is only that week's or month's closes — it does not accumulate.
        Measured against `STRATEGY_MIN_TRADES_FOR_VERDICT` (8-25), no
        strategy at this bot's trade rate can reach its floor inside
        either window, so both report INSUFFICIENT indefinitely. The
        design's own "time to CONCLUSIVE" table (4-12 months for
        Donchian) only holds over a window that spans months.

        Concretely, on the same day: the weekly window saw 2 of 25
        Donchian closes; a 365-day window sees 27 of 25 and reports
        measured R-expectancy with a confidence interval.

        Observational by design: `persist_state=False` AND
        `use_persistence=False`. `negative_weeks` is documented as
        *consecutive weekly checks*, so this run must neither advance it
        nor be gated on it.

        `persist_state=False` alone is NOT sufficient (PR #120 review, P1):
        the persisted weekly count is still loaded and threaded into the
        assessment, so a long-window run sitting on two persisted weekly
        negatives would project 3, return NEGATIVE and dispatch
        `STRATEGY_EDGE_LOSS` — on a non-weekly observation, contributing no
        weekly check of its own, and re-alerting every month because it
        never persists the increment. `use_persistence=False` feeds the
        assessment a zeroed state so the `>= 3` gate cannot be reached from
        here. Health alerts (L1/L2/L3) are not persistence-gated and still
        fire normally.
        """
        if today.day != 1:
            return
        if self.last_long_window_fired_date == today:
            return
        logger.info(
            f"health-review scheduler: firing LONG-WINDOW (365d) review "
            f"ending {today.isoformat()}"
        )
        try:
            self._run(
                window_from_args("yearly", end_date=today),
                persist_state=False,
                use_persistence=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"long-window health review failed for period ending "
                f"{today.isoformat()} (trading unaffected): {exc}"
            )
        # Marked regardless — the writer is idempotent-by-overwrite and a
        # failure must not re-fire every cycle for the rest of the day.
        self.last_long_window_fired_date = today

    def _run(
        self, window, *, persist_state: bool = True, use_persistence: bool = True,
    ) -> None:
        """Invoke the reviewer with the given window. Persists state by
        default (scheduled weekly/monthly runs are the canonical cadence
        the persistence file is designed for); the long-window run passes
        `persist_state=False` — see `_maybe_fire_long_window`."""
        conn = self.conn_factory()
        run_review(
            window,
            conn=conn,
            dispatcher=self.dispatcher,
            dry_run=False,
            persist_state=persist_state,
            use_persistence=use_persistence,
        )
