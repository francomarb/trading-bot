#!/usr/bin/env python3
"""Resolve disposable PLAN 11.61 RSI shadow candidates offline.

The command previews by default. Pass ``--apply`` to update only the
``entry_candidate_shadow_outcomes`` table; trading decisions and lifecycle
history are never changed.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import settings  # noqa: E402
from data.fetcher import fetch_symbol  # noqa: E402
from engine.candidate_observation import CandidateObservationStore  # noqa: E402
from reporting.candidate_shadows import (  # noqa: E402
    replay_contract_for_candidate,
    resolve_rsi_shadow,
)


_NY = ZoneInfo("America/New_York")
_REFRESHABLE = ("pending", "pending_data", "awaiting_fill", "open")


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def _load_work(
    conn: sqlite3.Connection, candidate_uid: str | None
) -> list[dict[str, object]]:
    conn.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in _REFRESHABLE)
    sql = f"""
        SELECT d.*, s.status AS shadow_status,
               s.outcome_basis AS shadow_outcome_basis,
               (
                   SELECT plo.time_in_force
                   FROM entry_candidate_decisions AS peer
                   JOIN position_lifecycle_orders AS plo
                     ON plo.position_uid = peer.position_uid
                    AND plo.role = 'entry_primary'
                   WHERE peer.cycle_uid = d.cycle_uid
                     AND peer.strategy = d.strategy
                     AND peer.signal_at = d.signal_at
                     AND peer.selected = 1
                     AND peer.position_uid IS NOT NULL
                   ORDER BY peer.evaluation_ordinal
                   LIMIT 1
               ) AS peer_entry_time_in_force
        FROM entry_candidate_decisions AS d
        JOIN entry_candidate_shadow_outcomes AS s USING(candidate_uid)
        WHERE d.strategy = 'rsi_reversion'
          AND d.selected = 0
          AND d.capacity_contended = 1
          AND s.status IN ({placeholders})
    """
    params: list[object] = list(_REFRESHABLE)
    if candidate_uid:
        sql += " AND d.candidate_uid = ?"
        params.append(candidate_uid)
    sql += " ORDER BY d.observed_at, d.evaluation_ordinal"
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _entry_window(observed: datetime) -> tuple[datetime, datetime]:
    local_date = observed.astimezone(_NY).date()
    close = datetime.combine(local_date, time(16, 0), _NY).astimezone(
        timezone.utc
    )
    return observed.replace(second=0, microsecond=0), close + timedelta(minutes=1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=settings.TRADE_LOG_DB)
    parser.add_argument("--candidate", help="resolve one full candidate UID")
    parser.add_argument(
        "--as-of",
        help="UTC ISO timestamp; defaults to now",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="persist results to the disposable shadow table",
    )
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        parser.error(f"database does not exist: {db_path}")
    as_of = _parse_utc(args.as_of) if args.as_of else datetime.now(timezone.utc)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    store = CandidateObservationStore(conn)
    work = _load_work(conn, args.candidate)
    if not work:
        print("No refreshable RSI shadow candidates.")
        return 0

    failures = 0
    for candidate in work:
        uid = str(candidate["candidate_uid"])
        symbol = str(candidate["symbol"])
        try:
            contract, contract_source = replay_contract_for_candidate(
                candidate, repo_root=ROOT
            )
            observed = _parse_utc(str(candidate["observed_at"]))
            signal_at = _parse_utc(str(candidate["signal_at"]))
            minute_start, minute_end = _entry_window(observed)
            entry_minutes, _ = fetch_symbol(
                symbol,
                minute_start,
                minute_end,
                timeframe="1Min",
                adjustment="all",
                feed=str(candidate["data_feed"]),
            )
            daily_bars, _ = fetch_symbol(
                symbol,
                signal_at - timedelta(days=60),
                as_of + timedelta(days=1),
                timeframe="1Day",
                adjustment="all",
                feed=str(candidate["data_feed"]),
            )
            entry_complete = as_of >= minute_end
            resolution = resolve_rsi_shadow(
                candidate,
                contract,
                daily_bars=daily_bars,
                entry_minutes=entry_minutes,
                as_of=as_of,
                entry_window_complete=entry_complete,
                contract_source=contract_source,
            )
            summary = {
                "candidate": uid[:10],
                "symbol": symbol,
                "status": resolution.status,
                "entry_price": resolution.entry_price,
                "exit_price": resolution.exit_price,
                "return_pct": resolution.return_pct,
                "r_multiple": resolution.r_multiple,
                "contract_source": contract_source,
            }
            print(json.dumps(summary, sort_keys=True))
            if args.apply:
                store.record_shadow_outcome(uid, **resolution.store_values())
        except Exception as exc:  # explicit per-candidate audit failure
            failures += 1
            print(
                json.dumps(
                    {
                        "candidate": uid[:10],
                        "symbol": symbol,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
    if not args.apply:
        print("Preview only; pass --apply to update disposable shadow outcomes.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
