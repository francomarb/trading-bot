"""Durable entry-candidate observations for PLAN 11.61.

The decision table is permanent audit evidence.  The shadow table is a small,
disposable calibration queue created only for candidate groups that actually
encounter a capacity constraint.  Neither table is read by the trading path.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping


_CREATE_CANDIDATE_DECISIONS_SQL = """
CREATE TABLE IF NOT EXISTS entry_candidate_decisions (
    candidate_uid              TEXT PRIMARY KEY,
    cycle_uid                  TEXT NOT NULL,
    observed_at                TEXT NOT NULL,
    updated_at                 TEXT NOT NULL,
    signal_at                  TEXT NOT NULL,
    strategy                   TEXT NOT NULL,
    strategy_version           TEXT,
    strategy_config_hash       TEXT,
    bot_git_commit             TEXT,
    symbol                     TEXT NOT NULL,
    signal_symbol              TEXT NOT NULL,
    timeframe                  TEXT NOT NULL,
    data_feed                  TEXT NOT NULL,
    regime                     TEXT,
    slot_ordinal               INTEGER,
    watchlist_ordinal          INTEGER,
    evaluation_ordinal         INTEGER,
    feature_schema_version     INTEGER NOT NULL,
    reference_price            REAL NOT NULL,
    atr                        REAL,
    strategy_features_json     TEXT NOT NULL,
    common_context_json        TEXT NOT NULL,
    execution_features_json    TEXT NOT NULL DEFAULT '{}',
    disposition                TEXT NOT NULL,
    disposition_reason         TEXT,
    target_symbol              TEXT,
    order_type                 TEXT,
    requested_qty              REAL,
    approved_notional_dollars  REAL,
    risk_budget_dollars        REAL,
    approved_risk_dollars      REAL,
    risk_clip_kind             TEXT,
    applied_size_multiplier    REAL,
    selected                   INTEGER NOT NULL DEFAULT 0 CHECK(selected IN (0, 1)),
    order_id                   TEXT,
    position_uid               TEXT,
    candidate_group_size       INTEGER NOT NULL DEFAULT 1,
    capacity_contended         INTEGER NOT NULL DEFAULT 0
        CHECK(capacity_contended IN (0, 1))
);
"""

_CREATE_CANDIDATE_SHADOW_SQL = """
CREATE TABLE IF NOT EXISTS entry_candidate_shadow_outcomes (
    candidate_uid          TEXT PRIMARY KEY,
    created_at             TEXT NOT NULL,
    status                 TEXT NOT NULL,
    outcome_basis          TEXT NOT NULL,
    position_uid           TEXT,
    entry_price            REAL,
    exit_price             REAL,
    exit_at                TEXT,
    pnl_dollars            REAL,
    return_pct             REAL,
    r_multiple             REAL,
    max_favorable_pct      REAL,
    max_adverse_pct        REAL,
    metadata_json          TEXT NOT NULL DEFAULT '{}',
    FOREIGN KEY(candidate_uid)
        REFERENCES entry_candidate_decisions(candidate_uid) ON DELETE CASCADE
);
"""

_CREATE_CANDIDATE_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_entry_candidates_cycle_strategy "
    "ON entry_candidate_decisions(cycle_uid, strategy, signal_at)",
    "CREATE INDEX IF NOT EXISTS idx_entry_candidates_strategy_signal "
    "ON entry_candidate_decisions(strategy, signal_at, symbol)",
    "CREATE INDEX IF NOT EXISTS idx_entry_candidates_contention "
    "ON entry_candidate_decisions(capacity_contended, strategy, signal_at)",
)

_CAPACITY_DISPOSITIONS = frozenset(
    {
        "sleeve_full",
        "sleeve_max_positions",
        "gross_exposure_cap",
        "insufficient_cash",
        "max_positions_reached",
        "max_strategy_heat_reached",
    }
)

_UPDATE_COLUMNS = frozenset(
    {
        "disposition",
        "disposition_reason",
        "target_symbol",
        "order_type",
        "requested_qty",
        "approved_notional_dollars",
        "risk_budget_dollars",
        "approved_risk_dollars",
        "risk_clip_kind",
        "applied_size_multiplier",
        "selected",
        "order_id",
        "position_uid",
        "execution_features_json",
    }
)

_SHADOW_UPDATE_COLUMNS = frozenset(
    {
        "status",
        "outcome_basis",
        "position_uid",
        "entry_price",
        "exit_price",
        "exit_at",
        "pnl_dollars",
        "return_pct",
        "r_multiple",
        "max_favorable_pct",
        "max_adverse_pct",
        "metadata_json",
    }
)


def _utc_iso(value: datetime | None = None) -> str:
    observed = value or datetime.now(timezone.utc)
    if observed.tzinfo is None:
        raise ValueError("candidate observation timestamps must be timezone-aware")
    return observed.astimezone(timezone.utc).isoformat()


def _json_safe(value: Any) -> Any:
    """Return deterministic JSON-compatible data without NaN/Infinity."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if hasattr(value, "value"):
        return _json_safe(value.value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(
        _json_safe(dict(value or {})),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


@dataclass(frozen=True)
class CandidateStart:
    """Facts available when an actionable signal reaches allocation."""

    cycle_uid: str
    signal_at: datetime
    strategy: str
    strategy_version: str | None
    strategy_config_hash: str | None
    bot_git_commit: str | None
    symbol: str
    signal_symbol: str
    timeframe: str
    data_feed: str
    regime: str | None
    slot_ordinal: int | None
    watchlist_ordinal: int | None
    evaluation_ordinal: int | None
    feature_schema_version: int
    reference_price: float
    atr: float | None
    strategy_features: Mapping[str, Any]
    common_context: Mapping[str, Any]


class CandidateObservationStore:
    """Write-only-from-engine evidence store; never consulted for decisions."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.execute(_CREATE_CANDIDATE_DECISIONS_SQL)
        self._conn.execute(_CREATE_CANDIDATE_SHADOW_SQL)
        for statement in _CREATE_CANDIDATE_INDEXES_SQL:
            self._conn.execute(statement)
        self._conn.commit()

    def start(
        self,
        candidate: CandidateStart,
        *,
        observed_at: datetime | None = None,
    ) -> str:
        candidate_uid = uuid.uuid4().hex
        now = _utc_iso(observed_at)
        self._conn.execute(
            """
            INSERT INTO entry_candidate_decisions (
                candidate_uid, cycle_uid, observed_at, updated_at, signal_at,
                strategy, strategy_version, strategy_config_hash,
                bot_git_commit, symbol, signal_symbol, timeframe, data_feed,
                regime, slot_ordinal, watchlist_ordinal, evaluation_ordinal,
                feature_schema_version,
                reference_price, atr, strategy_features_json,
                common_context_json, execution_features_json, disposition
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                candidate_uid,
                candidate.cycle_uid,
                now,
                now,
                _utc_iso(candidate.signal_at),
                candidate.strategy,
                candidate.strategy_version,
                candidate.strategy_config_hash,
                candidate.bot_git_commit,
                candidate.symbol,
                candidate.signal_symbol,
                candidate.timeframe,
                candidate.data_feed,
                candidate.regime,
                candidate.slot_ordinal,
                candidate.watchlist_ordinal,
                candidate.evaluation_ordinal,
                candidate.feature_schema_version,
                candidate.reference_price,
                candidate.atr,
                _json(candidate.strategy_features),
                _json(candidate.common_context),
                "eligible",
            ),
        )
        self._conn.commit()
        return candidate_uid

    def update(self, candidate_uid: str, **values: Any) -> None:
        """Enrich one candidate while enforcing a fixed column allow-list."""
        unknown = set(values) - _UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"unknown candidate update fields: {sorted(unknown)}")
        if not values:
            return
        normalized = dict(values)
        if "selected" in normalized:
            normalized["selected"] = int(bool(normalized["selected"]))
        if "execution_features_json" in normalized:
            existing = self._conn.execute(
                "SELECT execution_features_json FROM entry_candidate_decisions "
                "WHERE candidate_uid = ?",
                (candidate_uid,),
            ).fetchone()
            if existing is None:
                raise KeyError(f"unknown candidate_uid {candidate_uid}")
            merged = json.loads(existing[0])
            merged.update(dict(normalized["execution_features_json"] or {}))
            normalized["execution_features_json"] = _json(merged)
        normalized["updated_at"] = _utc_iso()
        assignments = ", ".join(f"{column} = ?" for column in normalized)
        cursor = self._conn.execute(
            f"UPDATE entry_candidate_decisions SET {assignments} "
            "WHERE candidate_uid = ?",
            (*normalized.values(), candidate_uid),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"unknown candidate_uid {candidate_uid}")
        self._conn.commit()

    def finalize_cycle(self, cycle_uid: str) -> None:
        """Label same-strategy groups and enqueue only real contention shadows."""
        rows = self._conn.execute(
            "SELECT candidate_uid, strategy, signal_at, disposition, selected, "
            "position_uid, reference_price FROM entry_candidate_decisions "
            "WHERE cycle_uid = ? ORDER BY observed_at, candidate_uid",
            (cycle_uid,),
        ).fetchall()
        groups: dict[tuple[str, str], list[tuple[Any, ...]]] = {}
        for row in rows:
            groups.setdefault((row[1], row[2]), []).append(row)

        now = _utc_iso()
        for group in groups.values():
            size = len(group)
            contended = (
                size > 1
                and any(row[4] for row in group)
                and any(row[3] in _CAPACITY_DISPOSITIONS for row in group)
            )
            self._conn.executemany(
                "UPDATE entry_candidate_decisions SET candidate_group_size = ?, "
                "capacity_contended = ?, updated_at = ? WHERE candidate_uid = ?",
                [(size, int(contended), now, row[0]) for row in group],
            )
            if not contended:
                continue
            for row in group:
                (
                    candidate_uid,
                    _strategy,
                    _signal_at,
                    disposition,
                    selected,
                    position_uid,
                    reference_price,
                ) = row
                has_lifecycle = bool(selected and position_uid)
                status = "actual_lifecycle" if has_lifecycle else "pending"
                basis = (
                    "actual_lifecycle" if has_lifecycle else "counterfactual_required"
                )
                self._conn.execute(
                    """
                    INSERT OR IGNORE INTO entry_candidate_shadow_outcomes (
                        candidate_uid, created_at, status, outcome_basis,
                        position_uid, entry_price, metadata_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_uid,
                        now,
                        status,
                        basis,
                        position_uid,
                        reference_price,
                        _json({"decision_disposition": disposition}),
                    ),
                )
        self._conn.commit()

    def read_cycle(self, cycle_uid: str) -> list[dict[str, Any]]:
        """Return decoded rows for tests and offline reporting."""
        cursor = self._conn.execute(
            "SELECT * FROM entry_candidate_decisions WHERE cycle_uid = ? "
            "ORDER BY observed_at, candidate_uid",
            (cycle_uid,),
        )
        columns = [item[0] for item in cursor.description]
        decoded: list[dict[str, Any]] = []
        for raw in cursor.fetchall():
            row = dict(zip(columns, raw))
            for key in (
                "strategy_features_json",
                "common_context_json",
                "execution_features_json",
            ):
                row[key.removesuffix("_json")] = json.loads(row.pop(key))
            decoded.append(row)
        return decoded

    def record_shadow_outcome(self, candidate_uid: str, **values: Any) -> None:
        """Resolve or enrich one disposable counterfactual outcome row."""
        unknown = set(values) - _SHADOW_UPDATE_COLUMNS
        if unknown:
            raise ValueError(f"unknown shadow outcome fields: {sorted(unknown)}")
        if not values:
            return
        normalized = dict(values)
        if "exit_at" in normalized and isinstance(normalized["exit_at"], datetime):
            normalized["exit_at"] = _utc_iso(normalized["exit_at"])
        if "metadata_json" in normalized:
            normalized["metadata_json"] = _json(normalized["metadata_json"])
        assignments = ", ".join(f"{column} = ?" for column in normalized)
        cursor = self._conn.execute(
            f"UPDATE entry_candidate_shadow_outcomes SET {assignments} "
            "WHERE candidate_uid = ?",
            (*normalized.values(), candidate_uid),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"candidate {candidate_uid} has no shadow outcome row")
        self._conn.commit()


__all__ = [
    "CandidateObservationStore",
    "CandidateStart",
    "_CREATE_CANDIDATE_DECISIONS_SQL",
    "_CREATE_CANDIDATE_INDEXES_SQL",
    "_CREATE_CANDIDATE_SHADOW_SQL",
]
