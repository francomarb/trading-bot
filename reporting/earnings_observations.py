"""Dedicated append-only earnings ledger, with atomic registration and recovery."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterator

from config import settings
from data.earnings_observation import (
    Citation,
    Contamination,
    Facts,
    Label,
    Registry,
    Schedule,
    Text,
    WINDOWS,
    ReactionResult,
    encoded,
    identity,
    utc_stamp,
)
from pydantic import TypeAdapter

REVISION_TABLES = (
    "earnings_sources",
    "earnings_schedule_revisions",
    "earnings_event_fact_revisions",
    "earnings_label_revisions",
    "earnings_contamination_revisions",
    "earnings_reaction_plans",
    "earnings_reaction_observations",
    "earnings_reaction_attempts",
)


def utc_now() -> datetime:
    """Production knowledge clock; operator inputs cannot backdate it."""
    return datetime.now(timezone.utc)


class EarningsStore:
    """Local writer with short serialized transactions and immutable records."""

    def __init__(
        self, path: str | Path, *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self.path = Path(path).resolve()
        self.clock = clock
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as conn:
            existing = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
            }
            if existing and "earnings_events" not in existing:
                raise ValueError(
                    "refusing to initialize earnings tables in an unrelated database"
                )
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS earnings_protocol_versions (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL CHECK(json_valid(body)));
                CREATE TABLE IF NOT EXISTS earnings_registry_versions (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL CHECK(json_valid(body)));
                CREATE TABLE IF NOT EXISTS earnings_cycles (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL CHECK(json_valid(body)), known_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS earnings_events (
                    id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL REFERENCES earnings_cycles(id),
                    issuer_id TEXT NOT NULL, fiscal_period_label TEXT NOT NULL,
                    registry_version TEXT NOT NULL REFERENCES earnings_registry_versions(id),
                    role TEXT NOT NULL CHECK(role IN ('source','context','candidate_schedule_only')),
                    registration_mode TEXT NOT NULL CHECK(registration_mode IN ('prospective','retrospective_seed')),
                    known_at TEXT NOT NULL, body TEXT NOT NULL CHECK(json_valid(body)),
                    UNIQUE(cycle_id,issuer_id,fiscal_period_label));
                CREATE TABLE IF NOT EXISTS earnings_reaction_jobs (
                    id TEXT PRIMARY KEY, event_id TEXT NOT NULL REFERENCES earnings_events(id),
                    window TEXT NOT NULL CHECK(window IN ('close_to_open_30m','close_to_close_1','close_to_close_3')),
                    symbol TEXT NOT NULL, benchmark TEXT NOT NULL,
                    UNIQUE(event_id,window,symbol,benchmark));
            """
            )
            for table in REVISION_TABLES:
                status_check = ""
                if table in {
                    "earnings_reaction_observations",
                    "earnings_reaction_attempts",
                }:
                    status_check = ", CHECK(json_extract(body,'$.status') IS NOT NULL AND json_extract(body,'$.status') IN ('complete','partial','unavailable','provider_error','entitlement_error'))"
                elif table == "earnings_label_revisions":
                    status_check = ", CHECK(json_extract(body,'$.label') IN ('positive','neutral','negative','mixed','not_applicable','unavailable'))"
                prices = ""
                if table == "earnings_reaction_observations":
                    prices = "".join(
                        f", {name}_price REAL GENERATED ALWAYS AS (json_extract(body,'$.marks.{name}.price')) VIRTUAL CHECK ({name}_price IS NULL OR {name}_price > 0)"
                        for name in (
                            "baseline",
                            "target",
                            "benchmark_baseline",
                            "benchmark_target",
                        )
                    )
                conn.execute(
                    f"""CREATE TABLE IF NOT EXISTS {table} (
                    id TEXT PRIMARY KEY, logical_key TEXT NOT NULL,
                    event_id TEXT REFERENCES earnings_events(id), known_at TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK(revision > 0),
                    supersedes_id TEXT REFERENCES {table}(id), reason TEXT,
                    body TEXT NOT NULL CHECK(json_valid(body)) {prices},
                    UNIQUE(logical_key,revision),
                    CHECK((revision=1 AND supersedes_id IS NULL) OR
                          (revision>1 AND supersedes_id IS NOT NULL AND length(reason)>0))
                    {status_check})"""
                )
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {table}_event ON {table}(event_id,known_at)"
                )
            for table in (
                *REVISION_TABLES,
                "earnings_events",
                "earnings_reaction_jobs",
                "earnings_cycles",
                "earnings_registry_versions",
                "earnings_protocol_versions",
            ):
                for operation in ("UPDATE", "DELETE"):
                    conn.execute(
                        f"CREATE TRIGGER IF NOT EXISTS {table}_no_{operation.lower()} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'earnings records are append-only'); END"
                    )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialize short writes; release resources on success and failure."""
        conn = sqlite3.connect(self.path, timeout=settings.EARNINGS_DB_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def now(self) -> datetime:
        """Return a validated UTC knowledge timestamp."""
        return utc_stamp(self.clock())

    @staticmethod
    def _latest(conn: sqlite3.Connection, table: str, key: str) -> sqlite3.Row | None:
        """Fetch one internal revision key from a fixed table name."""
        return conn.execute(
            f"SELECT * FROM {table} WHERE logical_key=? ORDER BY revision DESC LIMIT 1",
            (key,),
        ).fetchone()

    def _append(
        self,
        conn: sqlite3.Connection,
        table: str,
        key: str,
        event: str | None,
        body: dict,
        now: datetime,
        reason: str | None = None,
    ) -> str:
        """Idempotently append a validated revision within the caller's transaction."""
        payload = encoded(body)
        previous = self._latest(conn, table, key)
        if previous and previous["body"] == payload:
            return previous["id"]
        if previous:
            if not reason:
                raise ValueError("correction requires a reason")
            TypeAdapter(Text).validate_python(reason)
            if now.isoformat() < previous["known_at"]:
                raise ValueError("knowledge clock cannot move backwards")
        revision = previous["revision"] + 1 if previous else 1
        record_id = identity(table, key, str(revision))
        conn.execute(
            f"INSERT INTO {table} VALUES (?,?,?,?,?,?,?,?)",
            (
                record_id,
                key,
                event,
                now.isoformat(),
                revision,
                previous["id"] if previous else None,
                reason,
                payload,
            ),
        )
        return record_id

    @staticmethod
    def _event(conn: sqlite3.Connection, event_id: str) -> dict:
        """Require a registered event and return its frozen definition."""
        row = conn.execute(
            "SELECT * FROM earnings_events WHERE id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown event")
        return {**dict(row), "definition": json.loads(row["body"])}

    @staticmethod
    def _sources(
        conn: sqlite3.Connection, ids: tuple[str, ...], issuer: str, now: datetime
    ) -> list[Citation]:
        """Verify citation ownership and knowledge before recording derived facts."""
        result = []
        for source_id in ids:
            row = conn.execute(
                "SELECT * FROM earnings_sources WHERE id=?", (source_id,)
            ).fetchone()
            if row is None or row["known_at"] > now.isoformat():
                raise ValueError("source is missing or not yet known")
            citation = Citation.model_validate_json(row["body"])
            if citation.issuer_id != issuer:
                raise ValueError("source belongs to another issuer")
            result.append(citation)
        return result

    def add_source(self, citation: Citation) -> str:
        """Retain source metadata with a non-backdated acquisition clock."""
        now = self.now()
        if citation.accessed_at > now:
            raise ValueError("source access cannot be in the future")
        with self.transaction() as conn:
            return self._append(
                conn,
                "earnings_sources",
                identity(encoded(citation)),
                None,
                citation.model_dump(mode="json"),
                now,
            )

    def add_cycle(
        self, cycle_id: str, *, prospective_cutoff: datetime, note: str
    ) -> str:
        """Freeze an operator-named reporting round without inferring fiscal periods."""
        from data.earnings_observation import Identifier

        TypeAdapter(Identifier).validate_python(cycle_id)
        TypeAdapter(Text).validate_python(note)
        body = encoded(
            {
                "prospective_cutoff": utc_stamp(prospective_cutoff).isoformat(),
                "note": note,
            }
        )
        with self.transaction() as conn:
            old = conn.execute(
                "SELECT body FROM earnings_cycles WHERE id=?", (cycle_id,)
            ).fetchone()
            if old and old["body"] != body:
                raise ValueError("cycle is immutable")
            conn.execute(
                "INSERT OR IGNORE INTO earnings_cycles VALUES (?,?,?)",
                (cycle_id, body, self.now().isoformat()),
            )
        return cycle_id

    def register(
        self,
        registry: Registry,
        cycle_id: str,
        issuer: str,
        fiscal_period_label: str,
        schedule: Schedule,
        *,
        retrospective: bool = False,
    ) -> str:
        """Atomically register one identity, its schedule, and all expected jobs."""
        from data.earnings_observation import Identifier

        TypeAdapter(Identifier).validate_python(fiscal_period_label)
        spec = registry.issuer(issuer)
        now = self.now()
        with self.transaction() as conn:
            cycle = conn.execute(
                "SELECT * FROM earnings_cycles WHERE id=?", (cycle_id,)
            ).fetchone()
            if cycle is None:
                raise ValueError("register the cycle first")
            citation = self._sources(conn, (schedule.source_id,), spec.issuer_id, now)[
                0
            ]
            bound = schedule.lower_bound()
            prospective = (
                not retrospective
                and bound is not None
                and now < bound
                and citation.source_type == "issuer_schedule"
                and citation.knowledge_mode == "observed"
            )
            mode = "prospective" if prospective else "retrospective_seed"
            key = identity(
                registry.contract_version,
                registry.version,
                cycle_id,
                spec.issuer_id,
                fiscal_period_label,
            )
            old = conn.execute(
                "SELECT * FROM earnings_events WHERE cycle_id=? AND issuer_id=? AND fiscal_period_label=?",
                (cycle_id, spec.issuer_id, fiscal_period_label),
            ).fetchone()
            if old:
                if old["id"] != key:
                    raise ValueError(
                        "event already belongs to another registry version"
                    )
                first = conn.execute(
                    "SELECT body FROM earnings_schedule_revisions WHERE logical_key=? AND revision=1",
                    (key,),
                ).fetchone()
                if first["body"] != encoded(schedule):
                    raise ValueError("event exists; append a schedule correction")
                return key
            for table, version, body in (
                (
                    "earnings_protocol_versions",
                    registry.contract_version,
                    {"contract_version": registry.contract_version},
                ),
                (
                    "earnings_registry_versions",
                    registry.version,
                    registry.model_dump(mode="json"),
                ),
            ):
                prior = conn.execute(
                    f"SELECT body FROM {table} WHERE id=?", (version,)
                ).fetchone()
                if prior and prior["body"] != encoded(body):
                    raise ValueError("version already has a different definition")
                conn.execute(
                    f"INSERT OR IGNORE INTO {table} VALUES (?,?)",
                    (version, encoded(body)),
                )
            definition = {
                "issuer": spec.model_dump(mode="json"),
                "registry": registry.model_dump(mode="json"),
            }
            conn.execute(
                "INSERT INTO earnings_events VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    key,
                    cycle_id,
                    spec.issuer_id,
                    fiscal_period_label,
                    registry.version,
                    spec.role,
                    mode,
                    now.isoformat(),
                    encoded(definition),
                ),
            )
            self._append(
                conn,
                "earnings_schedule_revisions",
                key,
                key,
                schedule.model_dump(mode="json"),
                now,
            )
            pairs = set()
            for cluster in registry.memberships(spec.symbol):
                for symbol in {spec.symbol, *cluster.candidates}:
                    for benchmark in {cluster.benchmark, registry.diagnostic_benchmark}:
                        pairs.add((symbol, benchmark))
            for symbol, benchmark in sorted(pairs):
                for window in WINDOWS:
                    job_id = identity(
                        key, registry.reaction_window_version, window, symbol, benchmark
                    )
                    conn.execute(
                        "INSERT INTO earnings_reaction_jobs VALUES (?,?,?,?,?)",
                        (job_id, key, window, symbol, benchmark),
                    )
        return key

    def revise(
        self,
        event_id: str,
        record: Schedule | Facts | Label | Contamination,
        *,
        reason: str | None = None,
    ) -> str:
        """Append an operator record, checking provenance and frozen membership."""
        now = self.now()
        with self.transaction() as conn:
            event = self._event(conn, event_id)
            ids = (
                (record.source_id,)
                if isinstance(record, Schedule)
                else record.source_ids
            )
            sources = self._sources(conn, ids, event["issuer_id"], now)
            body = record.model_dump(mode="json")
            key = event_id
            if isinstance(record, Schedule):
                table = "earnings_schedule_revisions"
            elif isinstance(record, Facts):
                table = "earnings_event_fact_revisions"
                if record.actual_release_at and record.actual_release_at > now:
                    raise ValueError("actual release cannot be in the future")
                if record.call_started_at and record.call_started_at > now:
                    raise ValueError("actual call cannot be in the future")
                if record.actual_release_time_kind == "issuer_exact" and not any(
                    s.source_type in {"issuer_release", "issuer_material"}
                    for s in sources
                ):
                    raise ValueError(
                        "issuer_exact requires an issuer release/material citation"
                    )
                if (
                    record.actual_release_time_kind == "sec_acceptance_proxy"
                    and not any(s.source_type == "sec_filing" for s in sources)
                ):
                    raise ValueError("SEC proxy requires SEC citation")
            elif isinstance(record, Label):
                table = "earnings_label_revisions"
                registry = Registry.model_validate(event["definition"]["registry"])
                clusters = registry.memberships(event["definition"]["issuer"]["symbol"])
                if record.cluster_id not in {c.cluster_id for c in clusters}:
                    raise ValueError("label outside frozen event membership")
                if record.label not in {"unavailable", "not_applicable"} and not any(
                    s.source_type in {"issuer_release", "issuer_material", "sec_filing"}
                    for s in sources
                ):
                    raise ValueError("fundamental label requires authoritative source")
                key = identity(event_id, record.cluster_id, record.dimension)
                plan = self._latest(conn, "earnings_reaction_plans", event_id)
                context = (
                    "retrospective"
                    if event["registration_mode"] == "retrospective_seed"
                    else "after_primary_reaction_available"
                )
                if (
                    context != "retrospective"
                    and plan
                    and all(s.knowledge_mode == "observed" for s in sources)
                ):
                    p = json.loads(plan["body"])
                    if p["state"] == "ready" and now < datetime.fromisoformat(
                        p["targets"][WINDOWS[0]]
                    ) + timedelta(minutes=settings.EARNINGS_SIP_DELAY_MINUTES):
                        context = "before_primary_reaction_available"
                body["label_context"] = context
                previous = self._latest(conn, table, key)
                if previous:
                    old = json.loads(previous["body"])
                    if {
                        k: v for k, v in old.items() if k != "label_context"
                    } == record.model_dump(mode="json"):
                        return previous["id"]
            else:
                table = "earnings_contamination_revisions"
                key = identity(
                    event_id,
                    record.flag,
                    *sorted(record.windows),
                    record.shared_driver_group_id or "",
                )
            return self._append(conn, table, key, event_id, body, now, reason)

    def plan(self, event_id: str, facts_id: str, times: dict) -> str:
        """Append calendar-derived timing for the latest facts; keep job IDs stable."""
        with self.transaction() as conn:
            latest = self._latest(conn, "earnings_event_fact_revisions", event_id)
            if not latest or latest["id"] != facts_id:
                raise ValueError("facts changed during planning; retry")
            if latest["known_at"] > self.now().isoformat():
                raise ValueError("plan cannot predate its release facts")
            body = {**times, "facts_id": facts_id}
            return self._append(
                conn,
                "earnings_reaction_plans",
                event_id,
                event_id,
                body,
                self.now(),
                "release facts or authoritative calendar corrected",
            )

    def pending_events(self, limit: int) -> list[dict]:
        """Find latest facts without a matching plan, including after restart."""
        with self.transaction() as conn:
            result = []
            rows = conn.execute(
                "SELECT id FROM earnings_events WHERE role!='candidate_schedule_only' ORDER BY known_at,id"
            )
            for row in rows:
                facts = self._latest(conn, "earnings_event_fact_revisions", row["id"])
                plan = self._latest(conn, "earnings_reaction_plans", row["id"])
                if facts and (
                    not plan or json.loads(plan["body"])["facts_id"] != facts["id"]
                ):
                    result.append(dict(facts))
                    if len(result) >= limit:
                        break
            return result

    def recollect(self, event_id: str, *, reason: str) -> str:
        """Explicitly request corrected marks without overwriting any old revision."""
        TypeAdapter(Text).validate_python(reason)
        with self.transaction() as conn:
            self._event(conn, event_id)
            plan = self._latest(conn, "earnings_reaction_plans", event_id)
            facts = self._latest(conn, "earnings_event_fact_revisions", event_id)
            if (
                not plan
                or not facts
                or json.loads(plan["body"])["facts_id"] != facts["id"]
            ):
                raise ValueError(
                    "collect a plan for the current facts before requesting recollection"
                )
            body = json.loads(plan["body"])
            body["recollection_of"] = plan["id"]
            body["correction_reason"] = reason
            return self._append(
                conn,
                "earnings_reaction_plans",
                event_id,
                event_id,
                body,
                self.now(),
                reason,
            )

    def due_jobs(self, limit: int) -> list[dict]:
        """Select mature jobs using durable plans, attempts, and final outcomes."""
        now = self.now()
        result = []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM earnings_reaction_jobs ORDER BY event_id,window,symbol,benchmark"
            )
            for row in rows:
                plan = self._latest(conn, "earnings_reaction_plans", row["event_id"])
                facts = self._latest(
                    conn, "earnings_event_fact_revisions", row["event_id"]
                )
                if not plan or not facts:
                    continue
                p = json.loads(plan["body"])
                if p["facts_id"] != facts["id"]:
                    continue
                if p["state"] == "ready" and now < datetime.fromisoformat(
                    p["targets"][row["window"]]
                ) + timedelta(minutes=settings.EARNINGS_SIP_DELAY_MINUTES):
                    continue
                observation = self._latest(
                    conn, "earnings_reaction_observations", row["id"]
                )
                if (
                    observation
                    and json.loads(observation["body"])["plan_id"] == plan["id"]
                ):
                    continue
                attempt_key = identity(row["id"], plan["id"])
                attempt = self._latest(conn, "earnings_reaction_attempts", attempt_key)
                if (
                    attempt
                    and json.loads(attempt["body"]).get("next_retry_at", "")
                    > now.isoformat()
                ):
                    continue
                result.append(
                    {
                        **dict(row),
                        "plan_id": plan["id"],
                        "plan": p,
                        "attempt_count": attempt["revision"] if attempt else 0,
                    }
                )
                if len(result) >= limit:
                    break
        return result

    def record_attempt(self, job: dict, outcome: dict, *, final: bool) -> None:
        """Atomically commit an attempt and optional final revision; tolerate races."""
        validated = ReactionResult.model_validate(outcome)
        outcome = validated.model_dump(mode="json")
        now = self.now()
        if validated.marks:
            baseline = datetime.fromisoformat(job["plan"]["baseline_at"])
            target = datetime.fromisoformat(job["plan"]["targets"][job["window"]])
            for name, mark in validated.marks.items():
                if mark and mark.target_at != (
                    baseline if "baseline" in name else target
                ):
                    raise ValueError(
                        "mark does not match registered reaction endpoints"
                    )
            if target + timedelta(minutes=settings.EARNINGS_SIP_DELAY_MINUTES) > now:
                raise ValueError("cannot finalize an immature window")
        with self.transaction() as conn:
            plan = self._latest(conn, "earnings_reaction_plans", job["event_id"])
            facts = self._latest(conn, "earnings_event_fact_revisions", job["event_id"])
            if (
                not plan
                or plan["id"] != job["plan_id"]
                or json.loads(plan["body"])["facts_id"] != facts["id"]
            ):
                return
            observation = self._latest(
                conn, "earnings_reaction_observations", job["id"]
            )
            if (
                observation
                and json.loads(observation["body"])["plan_id"] == job["plan_id"]
            ):
                return
            key = identity(job["id"], job["plan_id"])
            attempt = self._latest(conn, "earnings_reaction_attempts", key)
            if (attempt["revision"] if attempt else 0) != job["attempt_count"]:
                return
            body = {
                **outcome,
                "plan_id": job["plan_id"],
                "collected_at": now.isoformat(),
                "attempt": job["attempt_count"] + 1,
            }
            if job["plan"]["state"] == "ready":
                target = datetime.fromisoformat(job["plan"]["targets"][job["window"]])
                body["target_at"] = target.isoformat()
                body["available_at"] = (
                    target + timedelta(minutes=settings.EARNINGS_SIP_DELAY_MINUTES)
                ).isoformat()
            if not final:
                body["next_retry_at"] = (
                    now + timedelta(seconds=settings.EARNINGS_RETRY_SECONDS)
                ).isoformat()
            self._append(
                conn,
                "earnings_reaction_attempts",
                key,
                job["event_id"],
                body,
                now,
                "collection retry",
            )
            if final:
                self._append(
                    conn,
                    "earnings_reaction_observations",
                    job["id"],
                    job["event_id"],
                    body,
                    now,
                    job["plan"].get(
                        "correction_reason",
                        "recollected after release timing correction",
                    ),
                )
