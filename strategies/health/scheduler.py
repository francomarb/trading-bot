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
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

from loguru import logger

from reporting.alerts import AlertDispatcher
from strategies.health.reviewer import run_review, window_from_args


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
    last_weekly_fired_date: date | None = None
    last_monthly_fired_date: date | None = None

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
        self._run(window)
        self.last_weekly_fired_date = today

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

    def _run(self, window) -> None:
        """Invoke the reviewer with the given window. Persists state
        (this is NOT a dry-run — scheduled runs are the canonical
        cadence the persistence file is designed for)."""
        conn = self.conn_factory()
        run_review(
            window,
            conn=conn,
            dispatcher=self.dispatcher,
            dry_run=False,
        )
