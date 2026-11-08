"""One bounded standalone earnings collection pass, restartable from SQLite."""

from __future__ import annotations

import fcntl
import time
from datetime import datetime
from typing import Any

from loguru import logger

from config import settings
from data.earnings_observation import Facts, ReactionResult, reaction_times
from data.preopen_provider import ProviderFailure
from reporting.earnings_observations import EarningsStore


def collect_once(
    store: EarningsStore,
    calendar: Any,
    provider: Any,
    *,
    max_jobs: int = settings.EARNINGS_MAX_JOBS,
    max_seconds: float = settings.EARNINGS_MAX_ROUND_SECONDS,
) -> dict:
    """Collect mature jobs with durable retries and no in-process retry sleeps."""
    if (
        not 1 <= max_jobs <= settings.EARNINGS_MAX_JOBS
        or not 0 < max_seconds <= settings.EARNINGS_MAX_ROUND_SECONDS
    ):
        raise ValueError("collection bounds exceed configuration")
    result = {"planned": 0, "attempted": 0, "finalized": 0, "planning_errors": 0}
    deadline = time.monotonic() + max_seconds
    mark_cache: dict[tuple, dict | None] = {}
    # The lock covers only this separate research collector, never the bot.
    with store.path.with_suffix(".collector.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {**result, "busy": True, "remaining_due": None}
        for row in store.pending_events(max_jobs):
            if time.monotonic() >= deadline:
                break
            try:
                facts = Facts.model_validate_json(row["body"])
                release = facts.actual_release_at
                sessions = calendar.sessions(release) if release else ()
                times = reaction_times(release, sessions)
                if facts.actual_release_time_kind == "operator_bounded":
                    times = {"state": "unavailable", "reason": "bounded_release_time"}
                # Bounded/proxy timestamps are not proof of first publication.
                times["release_time_kind"] = facts.actual_release_time_kind
                store.plan(row["event_id"], row["id"], times)
                result["planned"] += 1
            except Exception as exc:
                logger.warning("earnings planning failed ({})", type(exc).__name__)
                result["planning_errors"] += 1
        for job in store.due_jobs(max_jobs):
            if time.monotonic() >= deadline:
                break
            final = False
            try:
                outcome = collect_job(job, provider, store.now(), mark_cache)
                outcome = ReactionResult.model_validate(outcome).model_dump(mode="json")
                final = (
                    outcome["status"] in {"complete", "unavailable"}
                    or job["attempt_count"] + 1 >= settings.EARNINGS_MAX_ATTEMPTS
                )
            except ProviderFailure as exc:
                status = (
                    "entitlement_error"
                    if exc.classification == "entitlement_error"
                    else "provider_error"
                )
                outcome = {"status": status, "reason": exc.reason_code}
                final = (
                    not exc.retryable
                    or job["attempt_count"] + 1 >= settings.EARNINGS_MAX_ATTEMPTS
                )
                logger.warning("earnings provider failure: {}", status)
            except Exception as exc:
                # Deliberately do not persist exception messages or response bodies.
                logger.warning("earnings collection failed ({})", type(exc).__name__)
                outcome = {
                    "status": "provider_error",
                    "reason": "invalid_provider_response",
                }
                final = job["attempt_count"] + 1 >= settings.EARNINGS_MAX_ATTEMPTS
            store.record_attempt(job, outcome, final=final)
            result["attempted"] += 1
            result["finalized"] += int(final)
        result["remaining_due"] = store.due_job_count()
    return result


def collect_job(job: dict, provider: Any, now: datetime, mark_cache: dict) -> dict:
    """Calculate a single reporter/candidate comparison over identical endpoints."""
    plan = job["plan"]
    if plan["state"] == "unavailable":
        return {"status": "unavailable", "reason": plan["reason"]}
    baseline = datetime.fromisoformat(plan["baseline_at"])
    target = datetime.fromisoformat(plan["targets"][job["window"]])
    marks = {}
    for name, symbol, at, minute in (
        ("baseline", job["symbol"], baseline, False),
        ("target", job["symbol"], target, job["window"] == "close_to_open_30m"),
        ("benchmark_baseline", job["benchmark"], baseline, False),
        (
            "benchmark_target",
            job["benchmark"],
            target,
            job["window"] == "close_to_open_30m",
        ),
    ):
        key = (symbol, at, minute)
        if key not in mark_cache:
            mark_cache[key] = provider.mark(symbol, at, minute=minute, now=now)
        marks[name] = mark_cache[key]
    result = {"status": "partial", "reason": "missing_exact_bar", "marks": marks}
    if all(marks.values()):
        raw = marks["target"]["price"] / marks["baseline"]["price"] - 1
        benchmark = (
            marks["benchmark_target"]["price"] / marks["benchmark_baseline"]["price"]
            - 1
        )
        result.update(
            status="complete",
            reason=None,
            raw_return=raw,
            benchmark_return=benchmark,
            abnormal_return=raw - benchmark,
        )
    return result
