"""Forward-only daily mark-to-market evidence for strategy cohorts."""

from __future__ import annotations

import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Mapping


_CREATE_STRATEGY_DAILY_MARKS_SQL = """
CREATE TABLE IF NOT EXISTS strategy_daily_marks (
    mark_date               TEXT NOT NULL,
    observed_at             TEXT NOT NULL,
    strategy                TEXT NOT NULL,
    strategy_version        TEXT NOT NULL,
    strategy_config_hash    TEXT NOT NULL,
    realized_pnl            REAL NOT NULL,
    unrealized_pnl          REAL,
    total_pnl               REAL,
    open_lifecycles         INTEGER NOT NULL,
    valued_lifecycles       INTEGER NOT NULL,
    missing_lifecycles      INTEGER NOT NULL,
    unresolved_economics    INTEGER NOT NULL,
    source                  TEXT NOT NULL,
    PRIMARY KEY (
        mark_date, strategy, strategy_version, strategy_config_hash
    )
);
"""

_CREATE_STRATEGY_DAILY_MARKS_INDEXES_SQL = (
    "CREATE INDEX IF NOT EXISTS idx_strategy_daily_marks_cohort_date "
    "ON strategy_daily_marks("
    "strategy, strategy_version, strategy_config_hash, mark_date)",
)

_ACTIVE_STATUSES = frozenset({"open", "partially_filled"})
_ECONOMIC_TERMINAL_STATUSES = frozenset({"closed", "external_closed"})


def _position_unrealized_pnl(position: Any) -> float | None:
    """Return broker P&L, with a cost-basis fallback when it is absent."""
    value = getattr(position, "unrealized_pl", None)
    if value is None:
        value = getattr(position, "unrealized_pnl", None)
    if value is not None:
        value = float(value)
        return value if math.isfinite(value) else None
    market_value = getattr(position, "market_value", None)
    cost_basis = getattr(position, "cost_basis", None)
    if market_value is None or cost_basis is None:
        return None
    value = float(market_value) - float(cost_basis)
    return value if math.isfinite(value) else None


class StrategyDailyMarkStore:
    """Upsert the latest trustworthy observation for each cohort/day."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record_snapshot(
        self,
        broker_positions: Mapping[str, Any],
        *,
        observed_at: datetime | None = None,
    ) -> int:
        """Record cohort P&L from the durable ledger and broker positions.

        Existing history is never reconstructed. Repeated engine cycles replace
        the same UTC day's row, leaving the latest observation for that day.
        """
        observed = observed_at or datetime.now(timezone.utc)
        if observed.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        observed_utc = observed.astimezone(timezone.utc)
        mark_date = observed_utc.date().isoformat()
        observed_iso = observed_utc.isoformat()

        lifecycle_rows = self._conn.execute(
            "SELECT position_uid, strategy, strategy_version, "
            "strategy_config_hash, symbol, position_type, status, "
            "net_realized_pnl FROM position_lifecycle "
            "WHERE strategy_version IS NOT NULL "
            "AND strategy_version != 'unknown' "
            "AND strategy_config_hash IS NOT NULL "
            "AND strategy_config_hash != 'unknown'"
        ).fetchall()
        if not lifecycle_rows:
            return 0

        pnl_by_uid: dict[str, tuple[int, float]] = {}
        invalid_economics_uids: set[str] = set()
        pnl_rows = self._conn.execute(
            "SELECT position_uid, timestamp, realized_pnl FROM trades "
            "WHERE position_uid IS NOT NULL AND realized_pnl IS NOT NULL"
        ).fetchall()
        for uid, timestamp, pnl in pnl_rows:
            try:
                event_at = datetime.fromisoformat(
                    str(timestamp).replace("Z", "+00:00")
                )
                if event_at.tzinfo is None:
                    event_at = event_at.replace(tzinfo=timezone.utc)
                event_at = event_at.astimezone(timezone.utc)
            except ValueError:
                # Never pull an event with an unknown time across the snapshot
                # boundary; mark the cohort incomplete below.
                invalid_economics_uids.add(str(uid))
                continue
            if event_at > observed_utc:
                continue
            old_count, old_total = pnl_by_uid.get(str(uid), (0, 0.0))
            pnl_by_uid[str(uid)] = (old_count + 1, old_total + float(pnl))
        leg_rows = self._conn.execute(
            "SELECT position_uid, symbol FROM position_lifecycle_legs "
            "ORDER BY id"
        ).fetchall()
        legs_by_uid: dict[str, list[str]] = defaultdict(list)
        for uid, symbol in leg_rows:
            legs_by_uid[str(uid)].append(str(symbol))

        latest_by_cohort: dict[tuple[str, str, str], tuple[float, int]] = {}
        for strategy, version, config_hash, realized, unresolved in self._conn.execute(
            "SELECT strategy, strategy_version, strategy_config_hash, "
            "realized_pnl, unresolved_economics FROM strategy_daily_marks "
            "ORDER BY mark_date, observed_at"
        ).fetchall():
            latest_by_cohort[(str(strategy), str(version), str(config_hash))] = (
                float(realized), int(unresolved)
            )

        cohorts: dict[tuple[str, str, str], dict[str, float | int]] = defaultdict(
            lambda: {
                "realized": 0.0,
                "unrealized": 0.0,
                "open": 0,
                "valued": 0,
                "missing": 0,
                "unresolved": 0,
            }
        )
        for row in lifecycle_rows:
            (
                uid,
                strategy,
                version,
                config_hash,
                symbol,
                position_type,
                status,
                parent_pnl,
            ) = row
            cohort = cohorts[(str(strategy), str(version), str(config_hash))]
            pnl_count, ledger_pnl = pnl_by_uid.get(str(uid), (0, 0.0))
            cohort["realized"] += ledger_pnl

            economics_unresolved = str(uid) in invalid_economics_uids
            if status in _ECONOMIC_TERMINAL_STATUSES:
                economics_unresolved = economics_unresolved or (
                    pnl_count == 0
                    or not math.isclose(
                        float(parent_pnl or 0.0), ledger_pnl, abs_tol=0.01
                    )
                )
            if economics_unresolved:
                cohort["unresolved"] += 1

            if status == "error":
                cohort["missing"] += 1

            if status not in _ACTIVE_STATUSES:
                continue
            cohort["open"] += 1
            symbols = (
                legs_by_uid.get(str(uid), [])
                if position_type == "spread"
                else [str(symbol)]
            )
            if not symbols:
                cohort["missing"] += 1
                continue
            values: list[float] = []
            for leg_symbol in symbols:
                position = broker_positions.get(leg_symbol)
                value = (
                    _position_unrealized_pnl(position)
                    if position is not None
                    else None
                )
                if value is None:
                    values = []
                    break
                values.append(value)
            if not values:
                cohort["missing"] += 1
                continue
            cohort["valued"] += 1
            cohort["unrealized"] += sum(values)

        written = 0
        for (strategy, version, config_hash), values in cohorts.items():
            key = (strategy, version, config_hash)
            previous = latest_by_cohort.get(key)
            # Forward collection starts only for a cohort that is open while
            # the collector is deployed. Once its final realized value is
            # captured, do not append flat rows forever. A changed ledger or
            # integrity count still produces a correcting/final observation.
            if not values["open"] and not values["missing"]:
                if previous is None:
                    continue
                realized_unchanged = math.isclose(
                    previous[0], float(values["realized"]), abs_tol=0.01
                )
                integrity_unchanged = previous[1] == int(values["unresolved"])
                if realized_unchanged and integrity_unchanged:
                    continue
            complete = not values["missing"] and not values["unresolved"]
            unrealized = float(values["unrealized"]) if complete else None
            total = float(values["realized"]) + unrealized if complete else None
            self._conn.execute(
                "INSERT INTO strategy_daily_marks ("
                "mark_date, observed_at, strategy, strategy_version, "
                "strategy_config_hash, realized_pnl, unrealized_pnl, total_pnl, "
                "open_lifecycles, valued_lifecycles, missing_lifecycles, "
                "unresolved_economics, source) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(mark_date, strategy, strategy_version, "
                "strategy_config_hash) DO UPDATE SET "
                "observed_at=excluded.observed_at, "
                "realized_pnl=excluded.realized_pnl, "
                "unrealized_pnl=excluded.unrealized_pnl, "
                "total_pnl=excluded.total_pnl, "
                "open_lifecycles=excluded.open_lifecycles, "
                "valued_lifecycles=excluded.valued_lifecycles, "
                "missing_lifecycles=excluded.missing_lifecycles, "
                "unresolved_economics=excluded.unresolved_economics, "
                "source=excluded.source",
                (
                    mark_date,
                    observed_iso,
                    strategy,
                    version,
                    config_hash,
                    float(values["realized"]),
                    unrealized,
                    total,
                    int(values["open"]),
                    int(values["valued"]),
                    int(values["missing"]),
                    int(values["unresolved"]),
                    "broker_snapshot_plus_trade_ledger",
                ),
            )
            written += 1
        self._conn.commit()
        return written
