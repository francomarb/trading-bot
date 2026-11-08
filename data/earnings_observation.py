"""Validated, provider-independent earnings observation vocabulary and clocks."""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from config import settings

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
WINDOWS = ("close_to_open_30m", "close_to_close_1", "close_to_close_3")
DIMENSIONS = (
    "demand",
    "capex",
    "supply",
    "pricing",
    "guidance",
    "margins",
    "execution",
)
Window = Literal["close_to_open_30m", "close_to_close_1", "close_to_close_3"]
Dimension = Literal[
    "demand", "capex", "supply", "pricing", "guidance", "margins", "execution"
]
FlagKind = Literal[
    "macro_release",
    "fed_or_rates",
    "regulatory_or_legal",
    "geopolitical",
    "market_dislocation",
    "issuer_non_earnings_news",
    "peer_same_window",
    "call_overlap",
    "irregular_timing",
    "data_quality",
    "other_cited",
    "unknown",
]
Stamp = Annotated[AwareDatetime, AfterValidator(lambda value: value.astimezone(UTC))]
Identifier = Annotated[
    str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
]
Symbol = Annotated[
    str, Field(min_length=1, max_length=12, pattern=r"^[A-Z][A-Z0-9.\-]*$")
]


def clean_text(value: str) -> str:
    """Reject controls and common credential forms rather than persisting them."""
    if any(ord(char) < 32 for char in value) or re.search(
        r"(?i)(bearer\s|api[_-]?key\s*[:=]|secret\s*[:=]|token\s*[:=]|password\s*[:=])",
        value,
    ):
        raise ValueError("text contains controls or credential-like content")
    return value


Text = Annotated[str, Field(min_length=1, max_length=1000), AfterValidator(clean_text)]


class Model(BaseModel):
    """Immutable input records; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class Issuer(Model):
    """Stable issuer identity distinct from ticker aliases."""

    issuer_id: Identifier
    symbol: Symbol
    aliases: tuple[Symbol, ...] = ()
    role: Literal["source", "context", "candidate_schedule_only"]
    listing_kind: Literal["us_equity", "us_adr"] = "us_equity"
    currency: Literal["USD"] = "USD"


class Cluster(Model):
    """Registered economic relationships without scores or weights."""

    cluster_id: Identifier
    sources: tuple[Symbol, ...]
    candidates: tuple[Symbol, ...]
    benchmark: Symbol


class Registry(Model):
    """Content-addressed registry definition; versions cannot be overwritten."""

    version: Identifier
    contract_version: Identifier
    reaction_window_version: Identifier
    classification_rubric_version: Identifier
    issuers: tuple[Issuer, ...]
    clusters: tuple[Cluster, ...]
    diagnostic_benchmark: Symbol

    @model_validator(mode="after")
    def validate_edges(self) -> Self:
        """Reject ambiguous aliases, missing members and non-source edges."""
        if self.contract_version != settings.EARNINGS_PROTOCOL_VERSION:
            raise ValueError("unsupported earnings contract")
        ids = [item.issuer_id for item in self.issuers]
        symbols = [s for item in self.issuers for s in (item.symbol, *item.aliases)]
        clusters = [c.cluster_id for c in self.clusters]
        if (
            not ids
            or len(ids) != len(set(ids))
            or len(symbols) != len(set(symbols))
            or len(clusters) != len(set(clusters))
        ):
            raise ValueError("empty or duplicate registry identity")
        by_symbol = {i.symbol: i for i in self.issuers}
        for cluster in self.clusters:
            if (
                not cluster.sources
                or len(cluster.sources) != len(set(cluster.sources))
                or len(cluster.candidates) != len(set(cluster.candidates))
            ):
                raise ValueError("empty or duplicate cluster edge")
            if any(s not in by_symbol for s in (*cluster.sources, *cluster.candidates)):
                raise ValueError("cluster references an unknown canonical symbol")
            if any(
                by_symbol[s].role == "candidate_schedule_only" for s in cluster.sources
            ):
                raise ValueError("candidate-only issuer cannot supply cluster evidence")
            if (
                any(by_symbol[s].role == "context" for s in cluster.sources)
                and cluster.candidates
            ):
                raise ValueError("context cluster cannot contribute candidate evidence")
        for issuer in self.issuers:
            if issuer.role != "candidate_schedule_only" and not any(
                issuer.symbol in c.sources for c in self.clusters
            ):
                raise ValueError("source/context issuer must have a cluster")
        return self

    def issuer(self, identity: str) -> Issuer:
        """Resolve canonical ID or ticker, including GOOG -> Alphabet."""
        for issuer in self.issuers:
            if identity in (issuer.issuer_id, issuer.symbol, *issuer.aliases):
                return issuer
        raise ValueError("issuer is not registered")

    def memberships(self, symbol: str) -> tuple[Cluster, ...]:
        """Return frozen clusters that receive this reporter's evidence."""
        return tuple(c for c in self.clusters if symbol in c.sources)


def load_registry(path: str | Path) -> Registry:
    """Load and validate the versioned JSON registry."""
    return Registry.model_validate_json(Path(path).read_text())


def encoded(value: dict | Model) -> str:
    """Canonical JSON, disallowing non-finite numbers."""
    body = value.model_dump(mode="json") if isinstance(value, Model) else value
    return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False)


def identity(*parts: str) -> str:
    """Stable bounded key without delimiter ambiguity."""
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


class Citation(Model):
    """Public metadata and operator paraphrase; never a copied document."""

    issuer_id: Identifier
    source_type: Literal[
        "issuer_schedule",
        "issuer_release",
        "issuer_material",
        "sec_filing",
        "operator_note",
        "unavailable",
    ]
    title: Text
    url: str | None = Field(default=None, max_length=2000)
    published_at: Stamp | None = None
    accessed_at: Stamp
    paraphrase: Text
    accession: str | None = Field(default=None, pattern=r"^\d{10}-\d{2}-\d{6}$")
    content_hash: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    knowledge_mode: Literal["observed", "correction_only"] = "observed"

    @model_validator(mode="after")
    def validate_source(self) -> Self:
        """Require public citation URLs and chronological publication/access."""
        if self.published_at and self.published_at > self.accessed_at:
            raise ValueError("publication cannot follow access")
        if self.url:
            p = urlsplit(self.url)
            if (
                p.scheme != "https"
                or not p.hostname
                or p.username
                or p.password
                or p.query
                or p.fragment
            ):
                raise ValueError(
                    "citation requires public HTTPS URL without credentials/query/fragment"
                )
            clean_text(self.url)
            if self.source_type == "sec_filing" and p.hostname not in {
                "www.sec.gov",
                "sec.gov",
            }:
                raise ValueError("SEC citation requires sec.gov")
        elif self.source_type not in {"operator_note", "unavailable"}:
            raise ValueError("authoritative source requires URL")
        if self.source_type == "sec_filing" and not self.accession:
            raise ValueError("SEC citation requires accession")
        return self


class Schedule(Model):
    """Schedule knowledge captured separately from actual release facts."""

    scheduled_date: date | None = None
    scheduled_release_at: Stamp | None = None
    precision: Literal["exact", "before_open", "after_close", "date_only", "unknown"]
    source_id: Identifier
    author: Text

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        """Do not let a date-only schedule impersonate an exact timestamp."""
        if (self.precision == "exact") != (self.scheduled_release_at is not None):
            raise ValueError(
                "exact precision requires an exact timestamp and vice versa"
            )
        if self.precision != "unknown" and self.scheduled_date is None:
            raise ValueError("schedule date is required")
        if (
            self.scheduled_release_at
            and self.scheduled_release_at.astimezone(NEW_YORK).date()
            != self.scheduled_date
        ):
            raise ValueError("schedule date disagrees with New York timestamp")
        return self

    def lower_bound(self) -> datetime | None:
        """Conservative eligibility bound; date-level registration precedes midnight."""
        if self.scheduled_release_at:
            return self.scheduled_release_at
        if self.scheduled_date and self.precision in {"before_open", "after_close"}:
            return datetime.combine(self.scheduled_date, time.min, NEW_YORK).astimezone(
                UTC
            )
        return None


class Facts(Model):
    """Actual release facts with explicitly labeled uncertainty."""

    actual_release_at: Stamp | None = None
    actual_release_time_kind: Literal[
        "issuer_exact", "sec_acceptance_proxy", "operator_bounded", "unknown"
    ]
    call_scheduled_at: Stamp | None = None
    call_started_at: Stamp | None = None
    source_ids: tuple[Identifier, ...] = Field(min_length=1)
    author: Text
    note: Text

    @model_validator(mode="after")
    def validate_time(self) -> Self:
        """Unknown time must remain unknown, never an invented instant."""
        if (self.actual_release_time_kind == "unknown") != (
            self.actual_release_at is None
        ):
            raise ValueError("release time and time kind disagree")
        return self


class Label(Model):
    """One cited dimension; context is derived by the store, never user-supplied."""

    cluster_id: Identifier
    dimension: Dimension
    label: Literal[
        "positive", "neutral", "negative", "mixed", "not_applicable", "unavailable"
    ]
    source_ids: tuple[Identifier, ...] = Field(min_length=1)
    author: Text
    evidence: Text
    disagreement: Text | None = None


class Contamination(Model):
    """Append-only cited flags; no automatic exclusion or weighting."""

    flag: FlagKind
    windows: tuple[Window, ...] = Field(min_length=1)
    severity: Literal["note", "material", "exclusion_candidate"]
    source_ids: tuple[Identifier, ...] = Field(min_length=1)
    author: Text
    rationale: Text
    shared_driver_group_id: Identifier | None = None


class Session(Model):
    """Authoritative exchange session with UTC open and close."""

    session_date: date
    open_at: Stamp
    close_at: Stamp

    @model_validator(mode="after")
    def validate_session(self) -> Self:
        """Reject malformed provider sessions before planning any jobs."""
        if self.open_at >= self.close_at or any(
            t.astimezone(NEW_YORK).date() != self.session_date
            for t in (self.open_at, self.close_at)
        ):
            raise ValueError("invalid exchange session")
        return self


def reaction_times(release: datetime | None, sessions: tuple[Session, ...]) -> dict:
    """Resolve standard windows without inferring holidays or release times."""
    if release is None:
        return {"state": "unavailable", "reason": "unknown_release_time"}
    release = utc_stamp(release)
    ordered = sorted(sessions, key=lambda s: s.open_at)
    for i, session in enumerate(ordered):
        if (
            session.open_at - timedelta(minutes=settings.EARNINGS_OPEN_BUFFER_MINUTES)
            < release
            <= session.close_at
        ):
            return {"state": "unavailable", "reason": "irregular_timing"}
        if i and ordered[i - 1].close_at < release <= session.open_at - timedelta(
            minutes=settings.EARNINGS_OPEN_BUFFER_MINUTES
        ):
            if len(ordered) <= i + 2:
                raise ValueError("calendar lacks third reaction session")
            targets = (
                session.open_at
                + timedelta(minutes=settings.EARNINGS_OPEN_MARK_MINUTES),
                session.close_at,
                ordered[i + 2].close_at,
            )
            return {
                "state": "ready",
                "baseline_date": ordered[i - 1].session_date.isoformat(),
                "baseline_at": ordered[i - 1].close_at.isoformat(),
                "targets": {w: t.isoformat() for w, t in zip(WINDOWS, targets)},
            }
    raise ValueError("calendar does not bracket release")


def utc_stamp(value: datetime) -> datetime:
    """Normalize an internal clock while rejecting naive datetimes."""
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("aware timestamp required")
    return value.astimezone(UTC)


class Mark(Model):
    """One exact, positive mark with immutable provider provenance."""

    price: float = Field(gt=0)
    bar_at: Stamp
    target_at: Stamp
    feed: Literal["sip"]
    adjustment: Literal["split"]
    currency: Literal["USD"]
    endpoint: Literal["historical_bars"]
    timeframe: Literal["1Min", "1Day"]

    @model_validator(mode="after")
    def exact_interval(self) -> Self:
        """Reject substituted intervals and unexpected daily label semantics."""
        expected = (
            self.target_at - timedelta(minutes=1)
            if self.timeframe == "1Min"
            else datetime.combine(
                self.target_at.astimezone(NEW_YORK).date(), time.min, NEW_YORK
            ).astimezone(UTC)
        )
        if self.bar_at != expected:
            raise ValueError("mark is not the exact required interval")
        return self


class ReactionResult(Model):
    """Validated final/attempt payload; returns must agree with the stored marks."""

    status: Literal[
        "complete", "partial", "unavailable", "provider_error", "entitlement_error"
    ]
    reason: Identifier | None = None
    marks: dict[str, Mark | None] = Field(default_factory=dict)
    raw_return: float | None = None
    benchmark_return: float | None = None
    abnormal_return: float | None = None

    @model_validator(mode="after")
    def validate_returns(self) -> Self:
        """Complete means all four exact marks and consistent arithmetic."""
        expected = {"baseline", "target", "benchmark_baseline", "benchmark_target"}
        if self.marks and set(self.marks) != expected:
            raise ValueError("unexpected mark keys")
        if self.status == "complete":
            if not self.marks or not all(self.marks.values()):
                raise ValueError("complete reaction requires all marks")
            a, z = self.marks["baseline"], self.marks["target"]
            b, y = self.marks["benchmark_baseline"], self.marks["benchmark_target"]
            if (
                a.target_at != b.target_at
                or z.target_at != y.target_at
                or a.target_at >= z.target_at
            ):
                raise ValueError("benchmark endpoints disagree")
            raw, bench = z.price / a.price - 1, y.price / b.price - 1
            if any(
                v is None or not math.isclose(v, e, rel_tol=1e-12, abs_tol=1e-12)
                for v, e in (
                    (self.raw_return, raw),
                    (self.benchmark_return, bench),
                    (self.abnormal_return, raw - bench),
                )
            ):
                raise ValueError("returns disagree with marks")
        elif any(
            v is not None
            for v in (self.raw_return, self.benchmark_return, self.abnormal_return)
        ):
            raise ValueError("incomplete reaction cannot contain returns")
        return self
