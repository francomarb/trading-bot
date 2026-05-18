"""
Signal-lifecycle counters: read/write API for the
`strategy_lifecycle_counters` SQLite table.

Per design §12.4.1 the table stores aggregate per-period counters per
strategy — how many signals each strategy generated, how many were
blocked at each gate (regime → edge filter → sleeve → risk), how many
were submitted, how many filled. Aggregated by `(period_type,
period_start)` with a UNIQUE constraint so per-cycle increments inside
the engine accumulate locally and flush once via upsert.

This module is **observability only**. Counter writes must NEVER affect
trading decisions — they are recorded AFTER each gate has already
decided whether to take a signal. Per design §1.2 and §12 the engine
wraps counter writes in try/except → logger.warning + continue so a
counter table I/O failure can never raise into the trading loop. That
discipline is the caller's responsibility; this module provides the
primitives.

Counting unit (design §12.4.1):
  - **One symbol-level entry candidate per increment** — not per cycle,
    not per leg, not per share. A multi-leg/options entry attempt is
    `raw_signals += 1` regardless of leg count. Same for `filled_entries`.
  - Gate counts are **mutually exclusive in time order**: if regime
    rejects, only `regime_blocked` increments; if regime passes but
    edge filter rejects, only `edge_filter_blocked`. Total blocks ≤
    `raw_signals - submitted`.
  - `filled_entries` excludes partial fills that failed to open the
    intended position (cancelled, expired, etc.); partial-quantity
    fills that did open the position count as 1.

The table itself is created by `TradeLogger._ensure_db()`. This module
talks to it via a passed-in `sqlite3.Connection` so the connection
lifecycle stays with TradeLogger.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime


LIFECYCLE_TABLE_SCHEMA_VERSION = 1


_CREATE_LIFECYCLE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS strategy_lifecycle_counters (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    period_start        TEXT    NOT NULL,
    period_end          TEXT    NOT NULL,
    period_type         TEXT    NOT NULL,
    strategy_name       TEXT    NOT NULL,
    raw_signals         INTEGER NOT NULL DEFAULT 0,
    regime_blocked      INTEGER NOT NULL DEFAULT 0,
    edge_filter_blocked INTEGER NOT NULL DEFAULT 0,
    sleeve_blocked      INTEGER NOT NULL DEFAULT 0,
    risk_blocked        INTEGER NOT NULL DEFAULT 0,
    submitted           INTEGER NOT NULL DEFAULT 0,
    filled_entries      INTEGER NOT NULL DEFAULT 0,
    UNIQUE(period_type, period_start, strategy_name)
);
"""


# Valid `period_type` values. Enforced at the API boundary — invalid
# inputs raise rather than silently storing garbage that would confuse
# the reviewer aggregating by period_type later.
VALID_PERIOD_TYPES = frozenset({"weekly", "monthly"})


# ── Counter data class ────────────────────────────────────────────────


@dataclass
class LifecycleCounters:
    """One period's counters for one strategy.

    Mutable (unlike most health dataclasses) because the engine's
    per-cycle accumulator increments fields in place before flushing
    once at end of cycle — that's the §12.4.1 batching requirement to
    avoid 7 separate DB writes per symbol.
    """

    raw_signals: int = 0
    regime_blocked: int = 0
    edge_filter_blocked: int = 0
    sleeve_blocked: int = 0
    risk_blocked: int = 0
    submitted: int = 0
    filled_entries: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "raw_signals": self.raw_signals,
            "regime_blocked": self.regime_blocked,
            "edge_filter_blocked": self.edge_filter_blocked,
            "sleeve_blocked": self.sleeve_blocked,
            "risk_blocked": self.risk_blocked,
            "submitted": self.submitted,
            "filled_entries": self.filled_entries,
        }

    def __add__(self, other: "LifecycleCounters") -> "LifecycleCounters":
        """Field-wise sum — used by `read_counters_for_period` when
        aggregating across multiple stored rows."""
        if not isinstance(other, LifecycleCounters):
            return NotImplemented
        return LifecycleCounters(
            raw_signals=self.raw_signals + other.raw_signals,
            regime_blocked=self.regime_blocked + other.regime_blocked,
            edge_filter_blocked=self.edge_filter_blocked + other.edge_filter_blocked,
            sleeve_blocked=self.sleeve_blocked + other.sleeve_blocked,
            risk_blocked=self.risk_blocked + other.risk_blocked,
            submitted=self.submitted + other.submitted,
            filled_entries=self.filled_entries + other.filled_entries,
        )


# ── Public I/O API ────────────────────────────────────────────────────


def upsert_counters(
    conn: sqlite3.Connection,
    *,
    period_type: str,
    period_start: date | str,
    period_end: date | str,
    strategy_name: str,
    counters: LifecycleCounters,
) -> None:
    """Add `counters` to the existing row for (period_type, period_start,
    strategy_name), or insert a new row if none exists.

    Uses INSERT ON CONFLICT DO UPDATE so the engine's per-cycle flush
    is naturally accumulative — multiple flushes within the same period
    add to the same row rather than creating duplicates. UNIQUE
    constraint on (period_type, period_start, strategy_name) backs this.

    Raises ValueError on:
      - unknown period_type (only "weekly" or "monthly" allowed)
      - empty strategy_name
      - period_end <= period_start

    The caller (engine) is responsible for wrapping the call in
    try/except → log.warning to satisfy the failure-tolerance invariant
    (design §12.4.1: counter write failure must never raise into the
    trading loop).
    """
    if period_type not in VALID_PERIOD_TYPES:
        raise ValueError(
            f"period_type must be one of {sorted(VALID_PERIOD_TYPES)}; "
            f"got {period_type!r}"
        )
    if not strategy_name:
        raise ValueError("strategy_name must not be empty")
    start_iso = _to_iso_date(period_start)
    end_iso = _to_iso_date(period_end)
    if end_iso <= start_iso:
        raise ValueError(
            f"period_end ({end_iso}) must be strictly after period_start ({start_iso})"
        )

    conn.execute(
        """
        INSERT INTO strategy_lifecycle_counters (
            schema_version, period_start, period_end, period_type,
            strategy_name, raw_signals, regime_blocked,
            edge_filter_blocked, sleeve_blocked, risk_blocked,
            submitted, filled_entries
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(period_type, period_start, strategy_name) DO UPDATE SET
            period_end          = excluded.period_end,
            raw_signals         = raw_signals         + excluded.raw_signals,
            regime_blocked      = regime_blocked      + excluded.regime_blocked,
            edge_filter_blocked = edge_filter_blocked + excluded.edge_filter_blocked,
            sleeve_blocked      = sleeve_blocked      + excluded.sleeve_blocked,
            risk_blocked        = risk_blocked        + excluded.risk_blocked,
            submitted           = submitted           + excluded.submitted,
            filled_entries      = filled_entries      + excluded.filled_entries
        """,
        (
            LIFECYCLE_TABLE_SCHEMA_VERSION,
            start_iso, end_iso, period_type, strategy_name,
            counters.raw_signals,
            counters.regime_blocked,
            counters.edge_filter_blocked,
            counters.sleeve_blocked,
            counters.risk_blocked,
            counters.submitted,
            counters.filled_entries,
        ),
    )
    conn.commit()


def read_counters_for_period(
    conn: sqlite3.Connection,
    *,
    strategy_name: str,
    start: date | str,
    end: date | str,
    period_type: str | None = None,
) -> LifecycleCounters:
    """Sum counter rows whose `period_start` falls in `[start, end)`.

    `period_type` filter is optional — when omitted, sums across both
    weekly and monthly rows. (That would double-count if both are
    populated for the same span, so callers usually pass an explicit
    type. The flexibility is for ad-hoc queries.)

    Returns a zero-filled LifecycleCounters when no rows match. Per
    the docstring of `lifecycle.py`, callers should interpret zero
    counters as "no data yet" rather than "no signals" — the
    EdgeAssessor (11.10d) is responsible for distinguishing those
    cases via the persistence + envelope context.
    """
    if not strategy_name:
        raise ValueError("strategy_name must not be empty")
    start_iso = _to_iso_date(start)
    end_iso = _to_iso_date(end)
    if end_iso <= start_iso:
        raise ValueError(
            f"end ({end_iso}) must be strictly after start ({start_iso})"
        )

    sql = (
        "SELECT raw_signals, regime_blocked, edge_filter_blocked, "
        "sleeve_blocked, risk_blocked, submitted, filled_entries "
        "FROM strategy_lifecycle_counters "
        "WHERE strategy_name = ? AND period_start >= ? AND period_start < ?"
    )
    params: list = [strategy_name, start_iso, end_iso]
    if period_type is not None:
        if period_type not in VALID_PERIOD_TYPES:
            raise ValueError(
                f"period_type must be one of {sorted(VALID_PERIOD_TYPES)}; "
                f"got {period_type!r}"
            )
        sql += " AND period_type = ?"
        params.append(period_type)

    total = LifecycleCounters()
    for row in conn.execute(sql, params):
        total += LifecycleCounters(
            raw_signals=row[0],
            regime_blocked=row[1],
            edge_filter_blocked=row[2],
            sleeve_blocked=row[3],
            risk_blocked=row[4],
            submitted=row[5],
            filled_entries=row[6],
        )
    return total


def list_strategies(conn: sqlite3.Connection) -> list[str]:
    """All distinct strategy names recorded in the table, sorted.

    Used by the reviewer (11.10e) to iterate which strategies to
    report on without needing settings.STRATEGY_WATCHLISTS — handles
    decommissioned strategies that still have historical rows.
    """
    rows = conn.execute(
        "SELECT DISTINCT strategy_name FROM strategy_lifecycle_counters "
        "ORDER BY strategy_name"
    ).fetchall()
    return [r[0] for r in rows]


# ── Helpers ───────────────────────────────────────────────────────────


def _to_iso_date(value: date | str) -> str:
    """Coerce date or ISO-string to a canonical YYYY-MM-DD.

    Rejects:
      - `datetime` objects: Python's `datetime` is a subclass of `date`,
        so an unguarded `isinstance(value, date)` check would accept
        `datetime.now()` and serialize it with a time component. A flush
        at `2026-05-18T00:00:00` vs `2026-05-18T16:30:00` would create
        distinct UNIQUE keys instead of accumulating into the same
        weekly row — fragmenting L3 lifecycle counts. The engine
        wiring (11.10f) is likely to have datetime period values
        available; rejecting them at this boundary forces an explicit
        `.date()` coercion at the call site.
      - Strings with a time component: `date.fromisoformat` only accepts
        `YYYY-MM-DD` and raises `ValueError` on anything broader.

    Raises `TypeError` for datetimes (category mistake) and `ValueError`
    for malformed strings.
    """
    # Order matters: check datetime BEFORE date (datetime IS a date in Python).
    if isinstance(value, datetime):
        raise TypeError(
            f"datetime input rejected (got {value!r}); pass .date() "
            f"explicitly. Period boundaries are date-level — a time "
            f"component would fragment the UNIQUE(period_type, "
            f"period_start, strategy_name) idempotency."
        )
    if isinstance(value, date):
        return value.isoformat()
    # Strict string path: rejects timestamps and malformed dates.
    return date.fromisoformat(str(value)).isoformat()
