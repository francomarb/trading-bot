"""Read-only chronological earnings projection; no providers or writer imports."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from config import settings
from data.earnings_observation import DIMENSIONS, Registry, Schedule, utc_stamp


def read_earnings(
    path: str | Path, *, cutoff: datetime, corrected: bool = False
) -> dict:
    """Project only knowledge acquired by cutoff, or explicitly corrected history."""
    path = Path(path).resolve()
    cutoff = utc_stamp(cutoff)
    if not path.is_file():
        return {"state": "missing", "events": [], "coverage": {}}
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=1)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        # A corrected projection still records its actual request-time cutoff.
        at = cutoff.isoformat()
        events = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM earnings_events WHERE known_at<=? ORDER BY known_at,id",
                (at,),
            )
        ]
        sources = {
            row["id"]: {**dict(row), "data": json.loads(row["body"])}
            for row in conn.execute(
                "SELECT * FROM earnings_sources WHERE known_at<=?", (at,)
            )
        }
        result = []
        for event in events:
            definition = json.loads(event.pop("body"))
            registry = Registry.model_validate(definition["registry"])
            event["issuer"] = definition["issuer"]
            event["registry"] = definition["registry"]
            for name, table in (
                ("schedules", "earnings_schedule_revisions"),
                ("facts", "earnings_event_fact_revisions"),
                ("labels", "earnings_label_revisions"),
                ("flags", "earnings_contamination_revisions"),
                ("plans", "earnings_reaction_plans"),
                ("observations", "earnings_reaction_observations"),
                ("attempts", "earnings_reaction_attempts"),
            ):
                rows = conn.execute(
                    f"SELECT * FROM {table} WHERE event_id=? AND known_at<=? ORDER BY revision",
                    (event["id"], at),
                )
                latest = {}
                for row in rows:
                    latest[row["logical_key"]] = {
                        **dict(row),
                        "data": json.loads(row["body"]),
                    }
                event[name] = list(latest.values())
            jobs = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM earnings_reaction_jobs WHERE event_id=? ORDER BY id",
                    (event["id"],),
                )
            ]
            facts = event["facts"][0] if event["facts"] else None
            plan = event["plans"][0] if event["plans"] else None
            active_plan = (
                plan
                if plan and facts and plan["data"]["facts_id"] == facts["id"]
                else None
            )
            observations = {o["logical_key"]: o for o in event["observations"]}
            for job in jobs:
                observation = observations.get(job["id"])
                current = (
                    observation
                    if observation
                    and active_plan
                    and observation["data"]["plan_id"] == active_plan["id"]
                    else None
                )
                job["status"] = current["data"]["status"] if current else "pending"
                job["observation"] = current
                job["prior_observation"] = (
                    observation if observation and not current else None
                )
                if active_plan and active_plan["data"]["state"] == "ready":
                    target = datetime.fromisoformat(
                        active_plan["data"]["targets"][job["window"]]
                    )
                    job["target_at"] = target.isoformat()
                    job["available_at"] = (
                        target + timedelta(minutes=settings.EARNINGS_SIP_DELAY_MINUTES)
                    ).isoformat()
            event["jobs"] = jobs
            raw_release = facts["data"].get("actual_release_at") if facts else None
            release = (
                datetime.fromisoformat(raw_release).isoformat() if raw_release else None
            )
            event["prospective_timing_valid"] = bool(
                event["registration_mode"] == "prospective"
                and release
                and event["known_at"] < release
            )
            event["registration_timing_conflict"] = bool(
                event["registration_mode"] == "prospective"
                and release
                and event["known_at"] >= release
            )
            clusters = registry.memberships(event["issuer"]["symbol"])
            required = {(c.cluster_id, d) for c in clusters for d in DIMENSIONS}
            present = {
                (row["data"]["cluster_id"], row["data"]["dimension"])
                for row in event["labels"]
            }
            cited_ids = {
                sid
                for group in ("facts", "labels", "flags")
                for row in event[group]
                for sid in row["data"]["source_ids"]
            }
            cited_ids.update(row["data"]["source_id"] for row in event["schedules"])
            event_sources = [s for sid, s in sources.items() if sid in cited_ids]
            event["sources"] = event_sources
            event["fundamental_source_available"] = any(
                s["data"]["source_type"]
                in {"issuer_release", "issuer_material", "sec_filing"}
                for s in event_sources
            )
            event["observation_complete"] = bool(
                event["role"] != "candidate_schedule_only"
                and facts
                and required <= present
                and all(j["status"] != "pending" for j in jobs)
                and any(
                    s["data"]["source_type"]
                    in {
                        "issuer_release",
                        "issuer_material",
                        "sec_filing",
                        "unavailable",
                    }
                    for s in event_sources
                )
            )
            result.append(event)
        # Candidate status uses only already-known actual facts, never a schedule as proof of release.
        for event in result:
            registry = Registry.model_validate(event["registry"])
            candidates = sorted(
                {
                    s
                    for c in registry.memberships(event["issuer"]["symbol"])
                    for s in c.candidates
                }
            )
            raw_release = (
                event["facts"][0]["data"].get("actual_release_at")
                if event["facts"]
                else None
            )
            release = (
                datetime.fromisoformat(raw_release).isoformat() if raw_release else None
            )
            status_cutoff = min(at, release) if release else at
            event["candidate_status_cutoff"] = status_cutoff
            statuses = {}
            for symbol in candidates:
                if symbol == event["issuer"]["symbol"]:
                    statuses[symbol] = "reporter"
                    continue
                issuer = registry.issuer(symbol).issuer_id
                peers = [
                    e
                    for e in result
                    if e["cycle_id"] == event["cycle_id"]
                    and e["issuer_id"] == issuer
                    and e["known_at"] <= status_cutoff
                ]
                statuses[symbol] = "unknown"
                if len(peers) == 1:
                    # Query the actual historical revisions even in a current/corrected export.
                    peer = peers[-1]
                    fact = conn.execute(
                        "SELECT body FROM earnings_event_fact_revisions WHERE event_id=? AND known_at<=? ORDER BY revision DESC LIMIT 1",
                        (peer["id"], status_cutoff),
                    ).fetchone()
                    schedule = conn.execute(
                        "SELECT body FROM earnings_schedule_revisions WHERE event_id=? AND known_at<=? ORDER BY revision DESC LIMIT 1",
                        (peer["id"], status_cutoff),
                    ).fetchone()
                    raw_actual = (
                        json.loads(fact["body"]).get("actual_release_at")
                        if fact
                        else None
                    )
                    actual = (
                        datetime.fromisoformat(raw_actual).isoformat()
                        if raw_actual
                        else None
                    )
                    if actual and actual <= status_cutoff:
                        statuses[symbol] = "reported"
                    elif schedule:
                        bound = Schedule.model_validate_json(
                            schedule["body"]
                        ).lower_bound()
                        if bound and bound.isoformat() > status_cutoff:
                            statuses[symbol] = "scheduled_later"
            event["candidate_status"] = statuses
        coverage = {
            "events": len(result),
            "prospective": sum(e["registration_mode"] == "prospective" for e in result),
            "retrospective_seed": sum(
                e["registration_mode"] == "retrospective_seed" for e in result
            ),
            "observation_complete": sum(e["observation_complete"] for e in result),
            "jobs": sum(len(e["jobs"]) for e in result),
        }
        coverage["windows"] = {
            status: sum(j["status"] == status for e in result for j in e["jobs"])
            for status in (
                "pending",
                "complete",
                "partial",
                "unavailable",
                "provider_error",
                "entitlement_error",
            )
        }
        coverage.update(
            candidate_schedule_only=sum(
                e["role"] == "candidate_schedule_only" for e in result
            ),
            source_context_events=sum(
                e["role"] != "candidate_schedule_only" for e in result
            ),
            fundamental_source_available=sum(
                e["fundamental_source_available"] for e in result
            ),
            registration_timing_conflicts=sum(
                e["registration_timing_conflict"] for e in result
            ),
            with_recorded_flags=sum(bool(e["flags"]) for e in result),
            no_recorded_flags=sum(not e["flags"] for e in result),
            schedule_precision={
                p: sum(
                    bool(e["schedules"]) and e["schedules"][0]["data"]["precision"] == p
                    for e in result
                )
                for p in ("exact", "before_open", "after_close", "date_only", "unknown")
            },
            label_context={
                c: sum(
                    row["data"]["label_context"] == c
                    for e in result
                    for row in e["labels"]
                )
                for c in (
                    "before_primary_reaction_available",
                    "after_primary_reaction_available",
                    "retrospective",
                )
            },
        )
        return {
            "state": "ready",
            "view": "corrected" if corrected else "as_observed",
            "cutoff": at,
            "events": result,
            "coverage": coverage,
        }
    finally:
        conn.close()
