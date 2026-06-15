"""Disposable storage for temporary option-stop replacement diagnostics."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_SCHEMA = """
CREATE TABLE IF NOT EXISTS option_stop_replace_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    correlation_id  TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    record_type     TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    occ_symbol      TEXT NOT NULL,
    order_id        TEXT,
    payload_json    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_option_stop_audit_correlation
    ON option_stop_replace_audit(correlation_id, recorded_at, id);
CREATE INDEX IF NOT EXISTS idx_option_stop_audit_occ
    ON option_stop_replace_audit(occ_symbol, recorded_at, id);
"""


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


class OptionStopReplaceAuditStore:
    """Append-only standalone SQLite evidence store."""

    def __init__(self, db_path: str | Path) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        with self._conn:
            self._conn.executescript(_SCHEMA)

    def append(
        self,
        *,
        correlation_id: str,
        recorded_at: datetime,
        record_type: str,
        strategy: str,
        occ_symbol: str,
        order_id: str | None,
        payload: dict[str, Any],
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO option_stop_replace_audit (
                    correlation_id, recorded_at, record_type, strategy,
                    occ_symbol, order_id, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    correlation_id,
                    _iso(recorded_at),
                    record_type,
                    strategy,
                    occ_symbol,
                    order_id,
                    json.dumps(payload, sort_keys=True, default=str),
                ),
            )

    def prune_before(self, cutoff: datetime) -> int:
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM option_stop_replace_audit WHERE recorded_at < ?",
                (_iso(cutoff),),
            )
        return cursor.rowcount

    def read_records(
        self,
        *,
        occ_symbol: str | None = None,
        correlation_id: str | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        params: list[str] = []
        if occ_symbol:
            where.append("occ_symbol = ?")
            params.append(occ_symbol)
        if correlation_id:
            where.append("correlation_id = ?")
            params.append(correlation_id)
        sql = "SELECT * FROM option_stop_replace_audit"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY recorded_at, id"
        cursor = self._conn.execute(sql, params)
        columns = [description[0] for description in cursor.description]
        records: list[dict[str, Any]] = []
        for row in cursor.fetchall():
            record = dict(zip(columns, row))
            record["payload"] = json.loads(record.pop("payload_json"))
            records.append(record)
        return records

    def close(self) -> None:
        self._conn.close()
