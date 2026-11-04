"""Dedicated append-only SQLite store for market observations.

The store is intentionally separate from ``trades.db``.  A collection attempt
and any resulting revision-1 observation are committed atomically so an early
error cannot occupy the final row or block a later successful retry.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from data.preopen_observation import (
    CONTRACT_VERSION,
    CalculatedMark,
    Checkpoint,
    InstrumentSpec,
    QuoteSample,
    ReferenceClose,
    UniverseSpec,
)


@dataclass(frozen=True)
class AttemptState:
    """Durable retry state for one logical observation key."""

    attempt_count: int
    first_attempt_at: datetime | None
    retry_deadline_at: datetime | None
    next_retry_at: datetime | None


@dataclass(frozen=True)
class ObservationJob:
    """Durable expected key, including enough context for later recovery."""

    logical_key: str
    universe_version: str
    session_date: date
    previous_session_date: date
    checkpoint: Checkpoint
    instrument: InstrumentSpec


@dataclass(frozen=True)
class AttemptWrite:
    """One per-key outcome from a shared collection round."""

    logical_key: str
    round_id: str
    started_at: datetime
    ended_at: datetime
    outcome: str
    reason_code: str | None
    next_retry_at: datetime | None
    diagnostic: str | None = None


@dataclass(frozen=True)
class ObservationWrite:
    """Finalized revision-1 pre-open observation."""

    logical_key: str
    universe_version: str
    session_date: date
    checkpoint: Checkpoint
    instrument: InstrumentSpec
    first_attempt_at: datetime
    retry_deadline_at: datetime
    collected_at: datetime
    collection_state: str
    attempt_count: int
    status: str
    reason_code: str | None
    diagnostic: str | None
    quote: QuoteSample | None = None
    reference: ReferenceClose | None = None
    calculated: CalculatedMark | None = None
    revision: int = 1
    supersedes_id: str | None = None
    correction_reason: str | None = None


class MarketObservationStore:
    """Small transactional store with idempotent append-only writes."""

    def __init__(self, path: str | Path, *, timeout_seconds: float = 1.0) -> None:
        self.path = Path(path)
        self.timeout_seconds = timeout_seconds
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=self.timeout_seconds)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS observation_universes (
                    universe_version TEXT PRIMARY KEY,
                    contract_version TEXT NOT NULL,
                    definition_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS observation_attempts (
                    attempt_id TEXT PRIMARY KEY,
                    logical_key TEXT NOT NULL,
                    attempt_number INTEGER NOT NULL CHECK (attempt_number >= 1),
                    round_id TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    ended_at TEXT NOT NULL,
                    outcome TEXT NOT NULL CHECK (outcome IN (
                        'success', 'unavailable', 'transient_failure',
                        'not_yet_available', 'provider_error',
                        'entitlement_error'
                    )),
                    reason_code TEXT,
                    next_retry_at TEXT,
                    diagnostic TEXT,
                    UNIQUE(logical_key, attempt_number)
                );

                CREATE INDEX IF NOT EXISTS idx_observation_attempts_key
                    ON observation_attempts(logical_key, attempt_number DESC);

                CREATE TABLE IF NOT EXISTS observation_jobs (
                    logical_key TEXT PRIMARY KEY,
                    universe_version TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    previous_session_date TEXT NOT NULL,
                    checkpoint TEXT NOT NULL,
                    checkpoint_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    asset_type TEXT NOT NULL CHECK (asset_type IN ('stock', 'etf')),
                    observation_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(universe_version) REFERENCES observation_universes(universe_version)
                );

                CREATE INDEX IF NOT EXISTS idx_observation_jobs_session
                    ON observation_jobs(session_date, checkpoint, symbol);

                CREATE TABLE IF NOT EXISTS preopen_observations (
                    observation_id TEXT PRIMARY KEY,
                    logical_key TEXT NOT NULL,
                    contract_version TEXT NOT NULL,
                    universe_version TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 1),
                    supersedes_id TEXT,
                    correction_reason TEXT,
                    layer TEXT NOT NULL CHECK (layer IN ('sip_checkpoint', 'iex_diagnostic')),
                    session_date TEXT NOT NULL,
                    checkpoint TEXT NOT NULL,
                    checkpoint_at TEXT NOT NULL,
                    available_at TEXT NOT NULL,
                    first_attempt_at TEXT NOT NULL,
                    retry_deadline_at TEXT NOT NULL,
                    collected_at TEXT NOT NULL,
                    collection_state TEXT NOT NULL CHECK (collection_state IN (
                        'on_schedule', 'delayed_collection'
                    )),
                    symbol TEXT NOT NULL,
                    asset_type TEXT NOT NULL CHECK (asset_type IN ('stock', 'etf')),
                    observation_role TEXT NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'USD',
                    provider TEXT NOT NULL DEFAULT 'alpaca',
                    feed TEXT NOT NULL DEFAULT 'sip',
                    endpoint_kind TEXT NOT NULL DEFAULT 'historical_quotes',
                    is_consolidated INTEGER NOT NULL CHECK (is_consolidated IN (0, 1)),
                    attempt_count INTEGER NOT NULL CHECK (attempt_count >= 1),
                    quote_timestamp TEXT,
                    bid_price REAL,
                    bid_size REAL CHECK (bid_size IS NULL OR bid_size >= 0),
                    bid_exchange TEXT,
                    ask_price REAL,
                    ask_size REAL CHECK (ask_size IS NULL OR ask_size >= 0),
                    ask_exchange TEXT,
                    quote_conditions_json TEXT,
                    quote_tape TEXT,
                    reference_session_date TEXT,
                    reference_timestamp TEXT,
                    reference_price REAL CHECK (reference_price IS NULL OR reference_price > 0),
                    reference_feed TEXT,
                    reference_adjustment TEXT,
                    mark_kind TEXT,
                    mark_price REAL,
                    spread_bps REAL,
                    gap_dollars REAL,
                    gap_pct REAL,
                    quote_age_seconds REAL,
                    freshness_state TEXT CHECK (freshness_state IS NULL OR freshness_state IN (
                        'fresh', 'aging', 'stale', 'invalid'
                    )),
                    observation_status TEXT NOT NULL CHECK (observation_status IN (
                        'complete', 'partial', 'unavailable',
                        'provider_error', 'entitlement_error'
                    )),
                    reason_code TEXT,
                    diagnostic TEXT,
                    created_at TEXT NOT NULL,
                    UNIQUE(logical_key, revision),
                    FOREIGN KEY(universe_version) REFERENCES observation_universes(universe_version),
                    FOREIGN KEY(supersedes_id) REFERENCES preopen_observations(observation_id)
                );

                CREATE INDEX IF NOT EXISTS idx_preopen_session
                    ON preopen_observations(session_date, checkpoint, symbol);
                """
            )

    def register_universe(self, universe: UniverseSpec, *, created_at: datetime) -> None:
        """Persist and collision-check the immutable universe definition."""
        definition = json.dumps(
            [
                {
                    "symbol": item.symbol,
                    "asset_type": item.asset_type,
                    "observation_role": item.observation_role,
                    "effective_from_session": item.effective_from_session.isoformat(),
                    "effective_through_session": (
                        item.effective_through_session.isoformat()
                        if item.effective_through_session
                        else None
                    ),
                    "enabled": item.enabled,
                }
                for item in universe.instruments
            ],
            sort_keys=True,
            separators=(",", ":"),
        )
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT definition_json FROM observation_universes WHERE universe_version = ?",
                (universe.universe_version,),
            ).fetchone()
            if existing is not None and existing["definition_json"] != definition:
                raise ValueError("universe_version collision with a different definition")
            conn.execute(
                """
                INSERT OR IGNORE INTO observation_universes (
                    universe_version, contract_version, definition_json, created_at
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    universe.universe_version,
                    CONTRACT_VERSION,
                    definition,
                    _iso(created_at),
                ),
            )

    def is_finalized(self, logical_key: str) -> bool:
        """Whether revision 1 exists for this logical observation."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM preopen_observations WHERE logical_key = ? AND revision = 1",
                (logical_key,),
            ).fetchone()
        return row is not None

    def register_jobs(self, jobs: tuple[ObservationJob, ...], *, created_at: datetime) -> None:
        """Idempotently register expected keys before provider work begins."""
        if not jobs:
            return
        with self._connect() as conn:
            for job in jobs:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO observation_jobs (
                        logical_key, universe_version, session_date,
                        previous_session_date, checkpoint, checkpoint_at,
                        available_at, symbol, asset_type, observation_role,
                        created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job.logical_key,
                        job.universe_version,
                        job.session_date.isoformat(),
                        job.previous_session_date.isoformat(),
                        job.checkpoint.name,
                        _iso(job.checkpoint.checkpoint_at),
                        _iso(job.checkpoint.available_at),
                        job.instrument.symbol,
                        job.instrument.asset_type,
                        job.instrument.observation_role,
                        _iso(created_at),
                    ),
                )

    def latest_job_session(self) -> date | None:
        """Latest session with at least one durable expected key."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(session_date) AS session_date FROM observation_jobs"
            ).fetchone()
        return date.fromisoformat(row["session_date"]) if row["session_date"] else None

    def pending_jobs(
        self,
        *,
        now: datetime,
        limit: int,
    ) -> tuple[tuple[ObservationJob, AttemptState], ...]:
        """Return a bounded set of due jobs with retry state in one query."""
        if limit <= 0:
            raise ValueError("pending job limit must be positive")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    j.*,
                    (
                        SELECT COUNT(*)
                        FROM observation_attempts AS ac
                        WHERE ac.logical_key = j.logical_key
                    ) AS attempt_count,
                    (
                        SELECT af.started_at
                        FROM observation_attempts AS af
                        WHERE af.logical_key = j.logical_key
                        ORDER BY af.attempt_number ASC
                        LIMIT 1
                    ) AS first_attempt_at,
                    (
                        SELECT al.next_retry_at
                        FROM observation_attempts AS al
                        WHERE al.logical_key = j.logical_key
                        ORDER BY al.attempt_number DESC
                        LIMIT 1
                    ) AS next_retry_at
                FROM observation_jobs AS j
                LEFT JOIN preopen_observations AS o
                  ON o.logical_key = j.logical_key AND o.revision = 1
                WHERE o.observation_id IS NULL
                  AND (
                    (
                        SELECT al.next_retry_at
                        FROM observation_attempts AS al
                        WHERE al.logical_key = j.logical_key
                        ORDER BY al.attempt_number DESC
                        LIMIT 1
                    ) IS NULL
                    OR (
                        SELECT al.next_retry_at
                        FROM observation_attempts AS al
                        WHERE al.logical_key = j.logical_key
                        ORDER BY al.attempt_number DESC
                        LIMIT 1
                    ) <= ?
                  )
                ORDER BY j.session_date, j.checkpoint_at, j.symbol
                LIMIT ?
                """
                , (_iso(now), limit)
            ).fetchall()
        result: list[tuple[ObservationJob, AttemptState]] = []
        for row in rows:
            first_attempt = (
                _parse(row["first_attempt_at"])
                if row["first_attempt_at"]
                else None
            )
            state = AttemptState(
                attempt_count=int(row["attempt_count"]),
                first_attempt_at=first_attempt,
                retry_deadline_at=(
                    first_attempt + _RETRY_WINDOW if first_attempt else None
                ),
                next_retry_at=(
                    _parse(row["next_retry_at"])
                    if row["next_retry_at"]
                    else None
                ),
            )
            result.append(
                (
                    ObservationJob(
                        logical_key=row["logical_key"],
                        universe_version=row["universe_version"],
                        session_date=date.fromisoformat(row["session_date"]),
                        previous_session_date=date.fromisoformat(
                            row["previous_session_date"]
                        ),
                        checkpoint=Checkpoint(
                            name=row["checkpoint"],
                            checkpoint_at=_parse(row["checkpoint_at"]),
                            available_at=_parse(row["available_at"]),
                        ),
                        instrument=InstrumentSpec(
                            symbol=row["symbol"],
                            asset_type=row["asset_type"],
                            observation_role=row["observation_role"],
                            effective_from_session=date.fromisoformat(
                                row["session_date"]
                            ),
                        ),
                    ),
                    state,
                )
            )
        return tuple(result)

    def attempt_state(self, logical_key: str) -> AttemptState:
        """Load retry state without manufacturing absent attempts."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT attempt_number, started_at, next_retry_at
                FROM observation_attempts
                WHERE logical_key = ?
                ORDER BY attempt_number
                """,
                (logical_key,),
            ).fetchall()
        if not rows:
            return AttemptState(0, None, None, None)
        first = _parse(rows[0]["started_at"])
        return AttemptState(
            attempt_count=int(rows[-1]["attempt_number"]),
            first_attempt_at=first,
            retry_deadline_at=first + _RETRY_WINDOW,
            next_retry_at=(
                _parse(rows[-1]["next_retry_at"])
                if rows[-1]["next_retry_at"]
                else None
            ),
        )

    def record_attempt(
        self,
        attempt: AttemptWrite,
        *,
        final: ObservationWrite | None = None,
    ) -> None:
        """Atomically append one attempt and optional final observation."""
        with self._connect() as conn:
            current = conn.execute(
                "SELECT COALESCE(MAX(attempt_number), 0) AS n FROM observation_attempts WHERE logical_key = ?",
                (attempt.logical_key,),
            ).fetchone()
            attempt_number = int(current["n"]) + 1
            conn.execute(
                """
                INSERT INTO observation_attempts (
                    attempt_id, logical_key, attempt_number, round_id,
                    started_at, ended_at, outcome, reason_code,
                    next_retry_at, diagnostic
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    attempt.logical_key,
                    attempt_number,
                    attempt.round_id,
                    _iso(attempt.started_at),
                    _iso(attempt.ended_at),
                    attempt.outcome,
                    attempt.reason_code,
                    _iso(attempt.next_retry_at) if attempt.next_retry_at else None,
                    _sanitize(attempt.diagnostic),
                ),
            )
            if final is not None:
                if final.attempt_count != attempt_number:
                    raise ValueError("final attempt_count does not match durable attempt number")
                self._insert_observation(conn, final)

    def count_observations(self) -> int:
        """Testing/operator helper returning finalized row count."""
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM preopen_observations").fetchone()[0])

    def append_correction(self, correction: ObservationWrite) -> None:
        """Append a reviewed higher revision without mutating history."""
        if correction.revision <= 1:
            raise ValueError("a correction revision must be greater than 1")
        if not correction.supersedes_id or not correction.correction_reason:
            raise ValueError("corrections require supersedes_id and correction_reason")
        with self._connect() as conn:
            parent = conn.execute(
                """
                SELECT observation_id, logical_key, revision
                FROM preopen_observations
                WHERE observation_id = ?
                """,
                (correction.supersedes_id,),
            ).fetchone()
            if parent is None:
                raise ValueError("superseded observation does not exist")
            if parent["logical_key"] != correction.logical_key:
                raise ValueError("correction logical key differs from its parent")
            if correction.revision != int(parent["revision"]) + 1:
                raise ValueError("correction revision must immediately follow its parent")
            self._insert_observation(conn, correction)

    @staticmethod
    def _insert_observation(conn: sqlite3.Connection, final: ObservationWrite) -> None:
        quote = final.quote
        reference = final.reference
        calculated = final.calculated
        conn.execute(
            """
            INSERT INTO preopen_observations (
                observation_id, logical_key, contract_version, universe_version,
                revision, supersedes_id, correction_reason, layer, session_date, checkpoint,
                checkpoint_at, available_at, first_attempt_at, retry_deadline_at,
                collected_at, collection_state, symbol, asset_type,
                observation_role, currency, provider, feed, endpoint_kind,
                is_consolidated, attempt_count, quote_timestamp, bid_price,
                bid_size, bid_exchange, ask_price, ask_size, ask_exchange,
                quote_conditions_json, quote_tape, reference_session_date,
                reference_timestamp, reference_price, reference_feed,
                reference_adjustment, mark_kind, mark_price, spread_bps,
                gap_dollars, gap_pct, quote_age_seconds, freshness_state,
                observation_status, reason_code, diagnostic, created_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, 'sip_checkpoint', ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, 'USD', 'alpaca', 'sip', 'historical_quotes', 1, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'sip', 'split', ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                str(uuid.uuid4()),
                final.logical_key,
                CONTRACT_VERSION,
                final.universe_version,
                final.revision,
                final.supersedes_id,
                final.correction_reason,
                final.session_date.isoformat(),
                final.checkpoint.name,
                _iso(final.checkpoint.checkpoint_at),
                _iso(final.checkpoint.available_at),
                _iso(final.first_attempt_at),
                _iso(final.retry_deadline_at),
                _iso(final.collected_at),
                final.collection_state,
                final.instrument.symbol,
                final.instrument.asset_type,
                final.instrument.observation_role,
                final.attempt_count,
                _iso(quote.timestamp) if quote else None,
                quote.bid_price if quote else None,
                quote.bid_size if quote else None,
                quote.bid_exchange if quote else None,
                quote.ask_price if quote else None,
                quote.ask_size if quote else None,
                quote.ask_exchange if quote else None,
                json.dumps(list(quote.conditions), separators=(",", ":")) if quote else None,
                quote.tape if quote else None,
                reference.session_date.isoformat() if reference else None,
                _iso(reference.timestamp) if reference else None,
                reference.price if reference else None,
                calculated.mark_kind if calculated else None,
                calculated.mark_price if calculated else None,
                calculated.spread_bps if calculated else None,
                calculated.gap_dollars if calculated else None,
                calculated.gap_pct if calculated else None,
                calculated.quote_age_seconds if calculated else None,
                calculated.freshness_state if calculated else None,
                final.status,
                final.reason_code,
                _sanitize(final.diagnostic),
                _iso(final.collected_at),
            ),
        )


_RETRY_WINDOW = timedelta(days=7)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("stored timestamps must be timezone-aware")
    return value.isoformat()


def _parse(value: str) -> datetime:
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("stored timestamp is not timezone-aware")
    return result


def _sanitize(value: str | None) -> str | None:
    if value is None:
        return None
    return value[:500]
