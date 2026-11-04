"""Best-effort delayed-SIP collector for pre-open checkpoint observations."""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

from loguru import logger

from data.preopen_observation import (
    Checkpoint,
    InstrumentSpec,
    ReferenceClose,
    UniverseSpec,
    calculate_mark,
    checkpoints_for_session,
    logical_key,
)
from data.preopen_provider import (
    AlpacaPreopenProvider,
    AlpacaSessionCalendar,
    ProviderFailure,
)
from reporting.market_observations import (
    AttemptState,
    AttemptWrite,
    MarketObservationStore,
    ObservationJob,
    ObservationWrite,
)


UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
RETRY_WINDOW = timedelta(days=7)
ON_SCHEDULE_GRACE = timedelta(minutes=10)
RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(hours=1),
)
MAX_RETRY_DELAY = timedelta(hours=6)


@dataclass(frozen=True)
class DueObservation:
    """One due symbol/checkpoint plus its durable retry state."""

    instrument: InstrumentSpec
    universe_version: str
    session_date: date
    previous_session_date: date
    checkpoint: Checkpoint
    logical_key: str
    state: AttemptState


class PreopenObservationScheduler:
    """Collect all currently due keys in one bounded post-cycle round."""

    def __init__(
        self,
        *,
        universe: UniverseSpec,
        store: MarketObservationStore,
        provider: AlpacaPreopenProvider | None = None,
        calendar: AlpacaSessionCalendar | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        max_keys_per_round: int = 4,
    ) -> None:
        if max_keys_per_round <= 0:
            raise ValueError("max_keys_per_round must be positive")
        self.universe = universe
        self.store = store
        self.provider = provider or AlpacaPreopenProvider()
        self.calendar = calendar or AlpacaSessionCalendar()
        self.clock = clock
        self.max_keys_per_round = max_keys_per_round
        self._universe_registered = False

    def __call__(self) -> None:
        """Post-cycle entry point; every failure is isolated from trading."""
        try:
            self.collect_due()
        except Exception as exc:  # noqa: BLE001 - hard isolation boundary
            logger.warning(
                "pre-open observation scheduler failed (trading unaffected): "
                f"{type(exc).__name__}: {exc}"
            )

    def collect_due(self) -> int:
        """Collect one round and return the number of per-key attempts made."""
        now = self.clock().astimezone(UTC)
        if not self._universe_registered:
            self.store.register_universe(self.universe, created_at=now)
            self._universe_registered = True

        self._register_available_jobs(now)
        due = self._due_observations(now)
        if not due:
            return 0

        round_id = str(uuid.uuid4())
        references: dict[tuple[date, str], ReferenceClose] = {}
        ready: list[DueObservation] = []
        previous_sessions = tuple(
            dict.fromkeys(item.previous_session_date for item in due)
        )
        for previous_session_date in previous_sessions:
            group = tuple(
                item for item in due
                if item.previous_session_date == previous_session_date
            )
            symbols = tuple(
                dict.fromkeys(item.instrument.symbol for item in group)
            )
            try:
                group_references = self.provider.reference_closes(
                    symbols,
                    previous_session_date=previous_session_date,
                )
            except ProviderFailure as failure:
                for item in group:
                    self._record_failure(item, round_id, now, failure)
                continue
            for symbol, reference in group_references.items():
                references[(previous_session_date, symbol)] = reference
            ready.extend(group)

        for item in ready:
            started_at = self.clock().astimezone(UTC)
            try:
                quote = self.provider.latest_quote(
                    item.instrument.symbol,
                    session_date=item.session_date,
                    checkpoint_at=item.checkpoint.checkpoint_at,
                )
            except ProviderFailure as failure:
                self._record_failure(item, round_id, started_at, failure)
                continue

            ended_at = self.clock().astimezone(UTC)
            reference = references.get(
                (item.previous_session_date, item.instrument.symbol)
            )
            calculated = calculate_mark(
                checkpoint_at=item.checkpoint.checkpoint_at,
                quote=quote,
                reference=reference,
            )
            attempt_count = item.state.attempt_count + 1
            first_attempt = item.state.first_attempt_at or started_at
            deadline = item.state.retry_deadline_at or first_attempt + RETRY_WINDOW
            collection_state = (
                "on_schedule"
                if ended_at <= item.checkpoint.available_at + ON_SCHEDULE_GRACE
                else "delayed_collection"
            )
            final = ObservationWrite(
                logical_key=item.logical_key,
                universe_version=item.universe_version,
                session_date=item.session_date,
                checkpoint=item.checkpoint,
                instrument=item.instrument,
                first_attempt_at=first_attempt,
                retry_deadline_at=deadline,
                collected_at=ended_at,
                collection_state=collection_state,
                attempt_count=attempt_count,
                status=calculated.status,
                reason_code=calculated.reason_code,
                diagnostic=None,
                quote=quote,
                reference=reference,
                calculated=calculated,
            )
            outcome = "unavailable" if calculated.status == "unavailable" else "success"
            try:
                self.store.record_attempt(
                    AttemptWrite(
                        logical_key=item.logical_key,
                        round_id=round_id,
                        started_at=started_at,
                        ended_at=ended_at,
                        outcome=outcome,
                        reason_code=calculated.reason_code,
                        next_retry_at=None,
                    ),
                    final=final,
                )
            except sqlite3.Error as exc:
                logger.warning(
                    "pre-open observation persistence failed; key remains "
                    "pending (trading unaffected): "
                    f"{type(exc).__name__}: {exc}"
                )
        return len(due)

    def _register_available_jobs(self, now: datetime) -> None:
        """Discover due sessions, including those missed while not running."""
        effective_start = min(
            item.effective_from_session for item in self.universe.instruments
        )
        latest_registered = self.store.latest_job_session()
        scan_start = latest_registered or effective_start
        today = now.astimezone(NEW_YORK).date()
        if scan_start > today:
            return
        sessions = self.calendar.session_contexts(scan_start, today)
        jobs: list[ObservationJob] = []
        for session in sessions:
            instruments = self.universe.active_instruments(session.session_date)
            for checkpoint in checkpoints_for_session(session):
                if now < checkpoint.available_at:
                    continue
                for instrument in instruments:
                    jobs.append(
                        ObservationJob(
                            logical_key=logical_key(
                                universe_version=self.universe.universe_version,
                                session_date=session.session_date,
                                checkpoint=checkpoint.name,
                                symbol=instrument.symbol,
                            ),
                            universe_version=self.universe.universe_version,
                            session_date=session.session_date,
                            previous_session_date=session.previous_session_date,
                            checkpoint=checkpoint,
                            instrument=instrument,
                        )
                    )
        self.store.register_jobs(tuple(jobs), created_at=now)

    def _due_observations(self, now: datetime) -> tuple[DueObservation, ...]:
        return tuple(
            DueObservation(
                instrument=job.instrument,
                universe_version=job.universe_version,
                session_date=job.session_date,
                previous_session_date=job.previous_session_date,
                checkpoint=job.checkpoint,
                logical_key=job.logical_key,
                state=state,
            )
            for job, state in self.store.pending_jobs(
                now=now,
                limit=self.max_keys_per_round,
            )
        )

    def _record_failure(
        self,
        item: DueObservation,
        round_id: str,
        started_at: datetime,
        failure: ProviderFailure,
    ) -> None:
        ended_at = self.clock().astimezone(UTC)
        attempt_count = item.state.attempt_count + 1
        first_attempt = item.state.first_attempt_at or started_at
        deadline = item.state.retry_deadline_at or first_attempt + RETRY_WINDOW
        final_recovery_round = (
            item.state.first_attempt_at is not None and started_at >= deadline
        )
        final: ObservationWrite | None = None
        next_retry_at: datetime | None = None
        outcome = failure.classification
        if failure.retryable and not final_recovery_round:
            next_retry_at = ended_at + _retry_delay(attempt_count)
        else:
            status = (
                "provider_error"
                if failure.retryable
                else failure.classification
            )
            if status not in {"provider_error", "entitlement_error"}:
                status = "provider_error"
            final = ObservationWrite(
                logical_key=item.logical_key,
                universe_version=item.universe_version,
                session_date=item.session_date,
                checkpoint=item.checkpoint,
                instrument=item.instrument,
                first_attempt_at=first_attempt,
                retry_deadline_at=deadline,
                collected_at=ended_at,
                collection_state=(
                    "on_schedule"
                    if ended_at <= item.checkpoint.available_at + ON_SCHEDULE_GRACE
                    else "delayed_collection"
                ),
                attempt_count=attempt_count,
                status=status,
                reason_code=failure.reason_code,
                diagnostic=failure.diagnostic,
            )
            outcome = status
        try:
            self.store.record_attempt(
                AttemptWrite(
                    logical_key=item.logical_key,
                    round_id=round_id,
                    started_at=started_at,
                    ended_at=ended_at,
                    outcome=outcome,
                    reason_code=failure.reason_code,
                    next_retry_at=next_retry_at,
                    diagnostic=failure.diagnostic,
                ),
                final=final,
            )
        except sqlite3.Error as exc:
            logger.warning(
                "pre-open observation persistence failed; key remains pending "
                f"(trading unaffected): {type(exc).__name__}: {exc}"
            )


def _retry_delay(attempt_count: int) -> timedelta:
    if attempt_count <= len(RETRY_DELAYS):
        return RETRY_DELAYS[attempt_count - 1]
    return MAX_RETRY_DELAY
