"""Read-only dashboard projection for persisted market observations."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PreOpenObservationRow:
    """Latest finalized revision of one pre-open observation key."""

    logical_key: str
    revision: int
    universe_version: str
    session_date: str
    checkpoint: str
    checkpoint_at: str
    available_at: str
    collected_at: str
    collection_state: str
    symbol: str
    asset_type: str
    observation_role: str
    feed: str
    is_consolidated: bool
    quote_timestamp: str | None
    reference_price: float | None
    mark_price: float | None
    spread_bps: float | None
    gap_pct: float | None
    quote_age_seconds: float | None
    freshness_state: str | None
    observation_status: str
    reason_code: str | None


@dataclass(frozen=True)
class PreOpenDashboardSnapshot:
    """Latest-session observation rows plus their durable coverage denominator."""

    session_date: str | None
    universe_version: str | None
    expected_count: int
    expected_by_checkpoint: tuple[tuple[str, int], ...]
    rows: tuple[PreOpenObservationRow, ...]
    load_error: str | None = None

    @property
    def finalized_count(self) -> int:
        return len(self.rows)

    @property
    def complete_count(self) -> int:
        return sum(row.observation_status == "complete" for row in self.rows)

    @property
    def fresh_count(self) -> int:
        return sum(row.freshness_state == "fresh" for row in self.rows)


def load_preopen_dashboard_snapshot(
    db_path: str | Path,
) -> PreOpenDashboardSnapshot:
    """Load the latest finalized SIP cohort without creating or changing a DB.

    The connection uses SQLite ``mode=ro``.  Coverage denominators come from
    the durable expected jobs, while displayed market values come only from
    finalized observations.  Higher correction revisions supersede prior
    revisions in the projection without rewriting history.
    """
    path = Path(db_path)
    if not path.exists():
        return _empty_snapshot()

    try:
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
            conn.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            required = {"observation_jobs", "preopen_observations"}
            if not required.issubset(tables):
                return _empty_snapshot(
                    "Market observation database is missing the expected schema."
                )

            latest = conn.execute(
                """
                SELECT MAX(session_date) AS session_date
                FROM (
                    SELECT session_date FROM observation_jobs
                    UNION ALL
                    SELECT session_date
                    FROM preopen_observations
                    WHERE layer = 'sip_checkpoint'
                )
                """
            ).fetchone()
            session_date = latest["session_date"] if latest else None
            if not session_date:
                return _empty_snapshot()

            expected_rows = conn.execute(
                """
                SELECT checkpoint, COUNT(*) AS expected_count
                FROM observation_jobs
                WHERE session_date = ?
                GROUP BY checkpoint
                ORDER BY MIN(checkpoint_at), checkpoint
                """,
                (session_date,),
            ).fetchall()
            expected_by_checkpoint = tuple(
                (str(row["checkpoint"]), int(row["expected_count"]))
                for row in expected_rows
            )

            rows = conn.execute(
                """
                SELECT o.*
                FROM preopen_observations AS o
                JOIN (
                    SELECT logical_key, MAX(revision) AS revision
                    FROM preopen_observations
                    WHERE session_date = ?
                      AND layer = 'sip_checkpoint'
                    GROUP BY logical_key
                ) AS latest_revision
                  ON latest_revision.logical_key = o.logical_key
                 AND latest_revision.revision = o.revision
                WHERE o.session_date = ?
                  AND o.layer = 'sip_checkpoint'
                ORDER BY o.checkpoint_at, o.symbol
                """,
                (session_date, session_date),
            ).fetchall()
    except (OSError, sqlite3.Error) as exc:
        return _empty_snapshot(
            f"Failed to read market observations: {type(exc).__name__}: {exc}"
        )

    observations = tuple(_row_from_sql(row) for row in rows)
    expected_count = sum(count for _, count in expected_by_checkpoint)
    if expected_count == 0:
        expected_count = len(observations)
    universe_versions = {row.universe_version for row in observations}
    universe_version = (
        next(iter(universe_versions)) if len(universe_versions) == 1 else None
    )
    return PreOpenDashboardSnapshot(
        session_date=str(session_date),
        universe_version=universe_version,
        expected_count=expected_count,
        expected_by_checkpoint=expected_by_checkpoint,
        rows=observations,
    )


def _row_from_sql(row: sqlite3.Row) -> PreOpenObservationRow:
    values: dict[str, Any] = dict(row)
    return PreOpenObservationRow(
        logical_key=str(values["logical_key"]),
        revision=int(values["revision"]),
        universe_version=str(values["universe_version"]),
        session_date=str(values["session_date"]),
        checkpoint=str(values["checkpoint"]),
        checkpoint_at=str(values["checkpoint_at"]),
        available_at=str(values["available_at"]),
        collected_at=str(values["collected_at"]),
        collection_state=str(values["collection_state"]),
        symbol=str(values["symbol"]),
        asset_type=str(values["asset_type"]),
        observation_role=str(values["observation_role"]),
        feed=str(values["feed"]),
        is_consolidated=bool(values["is_consolidated"]),
        quote_timestamp=values["quote_timestamp"],
        reference_price=values["reference_price"],
        mark_price=values["mark_price"],
        spread_bps=values["spread_bps"],
        gap_pct=values["gap_pct"],
        quote_age_seconds=values["quote_age_seconds"],
        freshness_state=values["freshness_state"],
        observation_status=str(values["observation_status"]),
        reason_code=values["reason_code"],
    )


def _empty_snapshot(error: str | None = None) -> PreOpenDashboardSnapshot:
    return PreOpenDashboardSnapshot(
        session_date=None,
        universe_version=None,
        expected_count=0,
        expected_by_checkpoint=(),
        rows=(),
        load_error=error,
    )
