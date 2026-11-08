"""Offline contract tests for earnings registration, provenance and recovery."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from data.earnings_observation import (
    Citation,
    Contamination,
    Facts,
    Label,
    Mark,
    Registry,
    Schedule,
    Session,
    ReactionResult,
    WINDOWS,
    DIMENSIONS,
    load_registry,
    reaction_times,
)
from data.earnings_provider import (
    EarningsCalendar,
    EarningsSIPProvider,
    SECMetadataProvider,
)
from data.preopen_provider import ProviderFailure
from reporting.earnings_collector import collect_once
from reporting.earnings_observation_reader import read_earnings
from reporting.earnings_observations import EarningsStore
from scripts.earnings_observations import main

ROOT = Path(__file__).resolve().parents[1]


def dt(value: str) -> datetime:
    """UTC test timestamp shorthand."""
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def sessions() -> tuple[Session, ...]:
    """Explicit authoritative fixture, including a weekend gap."""
    return tuple(
        Session(
            session_date=f"2026-11-{day}",
            open_at=dt(f"2026-11-{day}T14:30:00"),
            close_at=dt(f"2026-11-{day}T21:00:00"),
        )
        for day in ("03", "04", "05", "06", "09", "10", "11")
    )


class FakeMarks:
    """Deterministic SIP marks; no provider network traffic."""

    def mark(
        self, symbol: str, target: datetime, *, minute: bool, now: datetime
    ) -> dict:
        """Return matching interval provenance and a known benchmark-relative move."""
        from data.earnings_observation import NEW_YORK

        bar_at = (
            target - timedelta(minutes=1)
            if minute
            else target.astimezone(NEW_YORK)
            .replace(hour=0, minute=0, second=0)
            .astimezone(timezone.utc)
        )
        baseline = target.date().isoformat() == "2026-11-04"
        price = (
            100.0
            if baseline
            else (102.0 if symbol in {"SPY", "QQQ", "SOXX"} else 105.0)
        )
        return {
            "price": price,
            "bar_at": bar_at.isoformat(),
            "target_at": target.isoformat(),
            "feed": "sip",
            "adjustment": "split",
            "currency": "USD",
            "endpoint": "historical_bars",
            "timeframe": "1Min" if minute else "1Day",
        }


@pytest.fixture
def ledger(tmp_path):
    """Preregister an MSFT event while retaining a controllable knowledge clock."""
    clock = [dt("2026-11-03T12:00:00")]
    store = EarningsStore(tmp_path / "earnings.db", clock=lambda: clock[0])
    registry = load_registry(ROOT / "config/earnings_registry_v1.json")
    store.add_cycle(
        "2026-q3", prospective_cutoff=dt("2026-10-01T00:00:00"), note="mixed pilot"
    )
    source = Citation(
        issuer_id="microsoft",
        source_type="issuer_schedule",
        title="Schedule",
        url="https://example.com/schedule",
        accessed_at=clock[0],
        paraphrase="Issuer scheduled earnings",
    )
    source_id = store.add_source(source)
    schedule = Schedule(
        scheduled_date="2026-11-04",
        scheduled_release_at=dt("2026-11-04T21:05:00"),
        precision="exact",
        source_id=source_id,
        author="operator",
    )
    event = store.register(registry, "2026-q3", "MSFT", "FY27Q1", schedule)
    return SimpleNamespace(
        store=store,
        clock=clock,
        registry=registry,
        event=event,
        schedule=schedule,
        source=source,
        calendar=SimpleNamespace(sessions=lambda _: sessions()),
    )


def add_facts(l, *, release="2026-11-04T21:05:00", kind="issuer_exact", reason=None):
    """Record a cited actual release at the current controlled clock."""
    source = l.source.model_copy(
        update={
            "source_type": "issuer_release",
            "title": "Results",
            "accessed_at": l.clock[0],
        }
    )
    sid = l.store.add_source(source)
    return l.store.revise(
        l.event,
        Facts(
            actual_release_at=dt(release) if release else None,
            actual_release_time_kind=kind,
            source_ids=(sid,),
            author="operator",
            note="Observed release",
        ),
        reason=reason,
    )


class TestRegistry:
    def test_alias_and_adr_identity(self, ledger):
        """GOOG resolves once; foreign listings retain explicit ADR provenance."""
        assert ledger.registry.issuer("GOOG").issuer_id == "alphabet"
        assert ledger.registry.issuer("TSM").listing_kind == "us_adr"

    def test_reject_unknown_edge(self, ledger):
        """Registry typos cannot silently change coverage."""
        body = ledger.registry.model_dump()
        body["clusters"][0]["candidates"] = ("UNKNOWN",)
        with pytest.raises(ValueError):
            Registry.model_validate(body)

    def test_idempotency_and_midcycle_version(self, ledger):
        """Repeat registration is a no-op, version moves are rejected."""
        l = ledger
        assert (
            l.store.register(l.registry, "2026-q3", "MSFT", "FY27Q1", l.schedule)
            == l.event
        )
        newer = l.registry.model_copy(update={"version": "v2"})
        with pytest.raises(ValueError, match="another registry"):
            l.store.register(newer, "2026-q3", "MSFT", "FY27Q1", l.schedule)
        report = read_earnings(l.store.path, cutoff=l.clock[0])
        assert report["coverage"]["events"] == 1
        assert (
            report["coverage"]["jobs"] == 48
        )  # reporter + 7 candidates, 2 benchmarks, 3 windows
        assert report["coverage"]["windows"]["pending"] == 48

    def test_registry_content_cannot_change_under_version(self, ledger):
        """An existing version is immutable even for a different fiscal event."""
        l = ledger
        new = l.registry.model_copy(update={"diagnostic_benchmark": "DIA"})
        with pytest.raises(ValueError, match="different definition"):
            l.store.register(new, "2026-q3", "MSFT", "FY27Q2", l.schedule)

    @pytest.mark.parametrize(
        "at,expected",
        [
            ("2026-11-04T21:04:59", "prospective"),
            ("2026-11-04T21:05:00", "retrospective_seed"),
        ],
    )
    def test_registration_release_boundary(self, ledger, at, expected):
        """Exact release time is too late for prospective registration."""
        l = ledger
        l.clock[0] = dt(at)
        event = l.store.register(
            l.registry, "2026-q3", "MSFT", "another-period", l.schedule
        )
        report = read_earnings(l.store.path, cutoff=l.clock[0])
        assert (
            next(e for e in report["events"] if e["id"] == event)["registration_mode"]
            == expected
        )

    def test_candidate_only_creates_no_own_reaction_jobs(self, ledger):
        """Candidate calendar events do not become new evidence sources."""
        l = ledger
        sid = l.store.add_source(l.source.model_copy(update={"issuer_id": "arista"}))
        event = l.store.register(
            l.registry,
            "2026-q3",
            "ANET",
            "FY26Q3",
            l.schedule.model_copy(update={"source_id": sid}),
        )
        report = read_earnings(l.store.path, cutoff=l.clock[0])
        assert next(e for e in report["events"] if e["id"] == event)["jobs"] == []


class TestTiming:
    @pytest.mark.parametrize(
        "release,state",
        [
            ("2026-11-04T21:00:00", "unavailable"),
            ("2026-11-04T21:00:01", "ready"),
            ("2026-11-05T14:25:00", "ready"),
            ("2026-11-05T14:25:01", "unavailable"),
            ("2026-11-05T15:00:00", "unavailable"),
        ],
    )
    def test_close_open_and_buffer(self, release, state):
        """No assumption across the exact close or five-minute buffer."""
        assert reaction_times(dt(release), sessions())["state"] == state

    def test_weekend_and_third_session(self):
        """Third reaction close skips weekends."""
        plan = reaction_times(dt("2026-11-04T21:05:00"), sessions())
        assert plan["targets"]["close_to_close_3"].startswith("2026-11-09")
        assert reaction_times(None, ())["reason"] == "unknown_release_time"

    def test_missing_calendar_rejected(self):
        """Insufficient calendar is a recoverable planning error, not guessed days."""
        with pytest.raises(ValueError):
            reaction_times(dt("2026-11-04T21:05:00"), sessions()[:3])
        with pytest.raises(ValueError):
            reaction_times(datetime(2026, 11, 4), sessions())

    def test_calendar_dst_and_early_close(self):
        """SDK naive times are exchange-local, including half days and DST."""
        from alpaca.trading.models import Calendar

        rows = [
            Calendar(date="2026-10-30", open="09:30", close="16:00"),
            Calendar(date="2026-11-27", open="09:30", close="13:00"),
        ]
        client = Mock()
        client.get_calendar.return_value = rows
        got = EarningsCalendar(client).sessions(dt("2026-11-04T21:05:00"))
        assert got[0].open_at.hour == 13
        assert got[1].open_at.hour == 14
        assert got[1].close_at.hour == 18


class TestRevisions:
    def test_corrections_preserve_as_observed(self, ledger):
        """A correction cannot appear in an earlier historical projection."""
        l = ledger
        before = l.clock[0]
        l.clock[0] += timedelta(hours=1)
        revised = l.schedule.model_copy(
            update={"scheduled_release_at": dt("2026-11-04T21:10:00")}
        )
        with pytest.raises(ValueError, match="reason"):
            l.store.revise(l.event, revised)
        l.store.revise(l.event, revised, reason="Issuer corrected schedule")
        old = read_earnings(l.store.path, cutoff=before)["events"][0]
        new = read_earnings(l.store.path, cutoff=l.clock[0], corrected=True)["events"][
            0
        ]
        assert old["schedules"][0]["revision"] == 1
        assert new["schedules"][0]["revision"] == 2
        assert new["registration_mode"] == "prospective"

    def test_source_ownership_and_future_access(self, ledger):
        """Another issuer and future access cannot satisfy cited evidence."""
        l = ledger
        with pytest.raises(ValueError):
            l.store.add_source(
                l.source.model_copy(
                    update={"accessed_at": l.clock[0] + timedelta(days=1)}
                )
            )
        sid = l.store.add_source(l.source.model_copy(update={"issuer_id": "meta"}))
        with pytest.raises(ValueError, match="another issuer"):
            l.store.revise(l.event, l.schedule.model_copy(update={"source_id": sid}))

    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com",
            "https://user:password@example.com",
            "https://example.com?token=secret",
            "https://example.com/#secret",
        ],
    )
    def test_sensitive_urls_rejected(self, ledger, url):
        """Never persist credentials disguised as public citations."""
        with pytest.raises(ValueError):
            Citation.model_validate({**ledger.source.model_dump(), "url": url})

    def test_append_only_database_and_lock_timeout(self, ledger):
        """Direct updates/deletes fail; locks are bounded."""
        with sqlite3.connect(ledger.store.path) as conn:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("DELETE FROM earnings_events")
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute("UPDATE earnings_events SET issuer_id='other'")
            conn.rollback()
            conn.execute("BEGIN IMMEDIATE")
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                ledger.store.add_source(ledger.source)

    def test_labels_flags_and_completion(self, ledger):
        """Explicit unavailable labels count as complete knowledge, not success."""
        l = ledger
        l.clock[0] = dt("2026-11-04T21:06:00")
        add_facts(l)
        collect_once(l.store, l.calendar, FakeMarks())
        source_id = l.store.add_source(
            l.source.model_copy(
                update={"source_type": "issuer_release", "accessed_at": l.clock[0]}
            )
        )
        for dimension in DIMENSIONS:
            label = Label(
                cluster_id="hyperscaler_ai_capex",
                dimension=dimension,
                label="unavailable",
                source_ids=(source_id,),
                author="operator",
                evidence="Not disclosed in this source",
            )
            first = l.store.revise(l.event, label)
            l.clock[0] += timedelta(seconds=1)
            assert l.store.revise(l.event, label) == first
        flag = Contamination(
            flag="peer_same_window",
            windows=WINDOWS,
            severity="note",
            source_ids=(source_id,),
            author="operator",
            rationale="Shared reporting window",
            shared_driver_group_id="group-1",
        )
        l.store.revise(l.event, flag)
        early = read_earnings(l.store.path, cutoff=l.clock[0])["events"][0]
        assert all(
            r["data"]["label_context"] == "before_primary_reaction_available"
            for r in early["labels"]
        )
        assert not early["observation_complete"]
        l.clock[0] = dt("2026-11-10T12:00:00")
        for _ in range(3):
            collect_once(l.store, l.calendar, FakeMarks())
        event = read_earnings(l.store.path, cutoff=l.clock[0])["events"][0]
        assert event["observation_complete"]
        assert len(event["flags"]) == 1

    def test_registration_conflict_exact_equality(self, ledger):
        """Later facts cannot validate an event registered at the release instant."""
        l = ledger
        add_facts(l, release="2026-11-03T12:00:00")
        event = read_earnings(l.store.path, cutoff=l.clock[0])["events"][0]
        assert event["registration_timing_conflict"]
        assert not event["prospective_timing_valid"]


class TestCollector:
    def test_cache_shared_within_round_but_not_across_retries(self, ledger):
        """Missing marks are fetched again next pass using the same provider object."""
        l = ledger
        l.clock[0] = dt("2026-11-05T15:16:00")
        add_facts(l)
        provider = Mock(wraps=FakeMarks())
        provider.mark.return_value = None
        collect_once(l.store, l.calendar, provider)
        assert (
            provider.mark.call_count == 20
        )  # 8 observed symbols + 2 benchmarks, 2 marks each
        provider.mark.reset_mock(return_value=True)
        l.clock[0] += timedelta(minutes=5)
        collect_once(l.store, l.calendar, provider)
        assert provider.mark.call_count == 20
        assert (
            read_earnings(l.store.path, cutoff=l.clock[0])["coverage"]["windows"][
                "complete"
            ]
            == 16
        )

    def test_explicit_recollection_and_preserved_old_view(self, ledger):
        """Operator recollection appends revisions without changing job identities."""
        l = ledger
        l.clock[0] = dt("2026-11-05T15:16:00")
        add_facts(l)
        collect_once(l.store, l.calendar, FakeMarks())
        before = l.clock[0]
        l.clock[0] += timedelta(seconds=1)
        l.store.recollect(l.event, reason="Review provider correction")
        assert (
            read_earnings(l.store.path, cutoff=l.clock[0])["coverage"]["windows"][
                "complete"
            ]
            == 0
        )
        collect_once(l.store, l.calendar, FakeMarks())
        current = read_earnings(l.store.path, cutoff=l.clock[0])
        assert current["coverage"]["jobs"] == 48
        assert all(r["revision"] == 2 for r in current["events"][0]["observations"])
        old = read_earnings(l.store.path, cutoff=before)
        assert all(r["revision"] == 1 for r in old["events"][0]["observations"])

    def test_failed_atomic_write_leaves_no_attempt(self, ledger):
        """A rejected final record cannot consume an attempt or corrupt recovery."""
        l = ledger
        l.clock[0] = dt("2026-11-04T21:06:00")
        add_facts(l)
        # Plan only while no window has matured.
        collect_once(l.store, l.calendar, FakeMarks())
        l.clock[0] = dt("2026-11-05T15:16:00")
        job = l.store.due_jobs(1)[0]
        with l.store.transaction() as conn:
            conn.execute(
                "CREATE TRIGGER reject_final BEFORE INSERT ON earnings_reaction_observations BEGIN SELECT RAISE(ABORT,'test failure'); END"
            )
        with pytest.raises(sqlite3.IntegrityError):
            l.store.record_attempt(
                job, {"status": "provider_error", "reason": "test_failure"}, final=True
            )
        with l.store.transaction() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM earnings_reaction_attempts"
                ).fetchone()[0]
                == 0
            )
            conn.execute("DROP TRIGGER reject_final")
        assert (
            collect_once(l.store, l.calendar, FakeMarks(), max_jobs=1)["finalized"] == 1
        )

    def test_invalid_collection_bounds(self, ledger):
        """Public runner refuses unbounded job counts."""
        with pytest.raises(ValueError):
            collect_once(ledger.store, ledger.calendar, FakeMarks(), max_jobs=0)

    def test_maturity_restart_and_arithmetic(self, ledger):
        """Mature jobs resume exactly once after reopening the database."""
        l = ledger
        l.clock[0] = dt("2026-11-04T21:06:00")
        add_facts(l)
        assert collect_once(l.store, l.calendar, FakeMarks())["planned"] == 1
        l.clock[0] = dt("2026-11-05T15:15:59")
        assert collect_once(l.store, l.calendar, FakeMarks())["attempted"] == 0
        l.clock[0] += timedelta(seconds=1)
        reopened = EarningsStore(l.store.path, clock=lambda: l.clock[0])
        assert collect_once(reopened, l.calendar, FakeMarks())["finalized"] == 16
        assert collect_once(reopened, l.calendar, FakeMarks())["attempted"] == 0
        observations = read_earnings(l.store.path, cutoff=l.clock[0])["events"][0][
            "observations"
        ]
        assert all(
            r["data"]["abnormal_return"] == pytest.approx(0.03) for r in observations
        )

    def test_retry_survives_restart_and_final_failure(self, ledger):
        """Missing marks retry three times with no sleeps and then remain visible."""
        l = ledger
        l.clock[0] = dt("2026-11-05T15:16:00")
        add_facts(l)
        provider = SimpleNamespace(mark=lambda *a, **kw: None)
        for attempt in range(3):
            got = collect_once(l.store, l.calendar, provider, max_jobs=1)
            assert got["finalized"] == int(attempt == 2)
            # Limit assertions to the one job across attempts: remaining jobs can still run.
            l.clock[0] += timedelta(minutes=5)
        report = read_earnings(l.store.path, cutoff=l.clock[0])
        assert report["coverage"]["windows"]["partial"] == 1

    @pytest.mark.parametrize(
        "kind,retryable,status",
        [
            ("entitlement_error", False, "entitlement_error"),
            ("transient_failure", True, "provider_error"),
        ],
    )
    def test_provider_failure_classification(self, ledger, kind, retryable, status):
        """No provider exception body is persisted in the ledger."""
        l = ledger
        l.clock[0] = dt("2026-11-05T15:16:00")
        add_facts(l)
        provider = Mock()
        provider.mark.side_effect = ProviderFailure(
            kind, "http_403", "secret provider body", retryable
        )
        collect_once(l.store, l.calendar, provider, max_jobs=1)
        event = read_earnings(l.store.path, cutoff=l.clock[0])["events"][0]
        assert event["attempts"][0]["data"]["status"] == status
        assert "secret provider body" not in json.dumps(event)

    def test_corrected_facts_stale_old_observations(self, ledger):
        """Corrected release timing requires new marks while preserving old history."""
        l = ledger
        l.clock[0] = dt("2026-11-05T15:16:00")
        add_facts(l)
        collect_once(l.store, l.calendar, FakeMarks())
        before = l.clock[0]
        l.clock[0] += timedelta(seconds=1)
        add_facts(l, release="2026-11-05T15:00:00", reason="Corrected actual release")
        current = read_earnings(l.store.path, cutoff=l.clock[0])
        assert current["coverage"]["windows"]["complete"] == 0
        assert (
            read_earnings(l.store.path, cutoff=before)["coverage"]["windows"][
                "complete"
            ]
            == 16
        )
        collect_once(l.store, l.calendar, FakeMarks())
        current = read_earnings(l.store.path, cutoff=l.clock[0])
        assert current["coverage"]["windows"]["unavailable"] == 24

    def test_unknown_time_and_calendar_error(self, ledger):
        """Unknown timing finalizes unavailable; calendar outage stays recoverable."""
        l = ledger
        l.clock[0] = dt("2026-11-05T15:16:00")
        add_facts(l)
        calendar = Mock()
        calendar.sessions.side_effect = TimeoutError("private error")
        assert collect_once(l.store, calendar, FakeMarks())["planning_errors"] == 1
        add_facts(l, release=None, kind="unknown", reason="Time cannot be verified")
        assert collect_once(l.store, calendar, FakeMarks())["finalized"] == 24

    def test_return_payload_rejects_nonfinite_or_wrong_math(self):
        """Data integrity checks live at the store boundary, not only the provider."""
        with pytest.raises(ValueError):
            ReactionResult(status="partial", raw_return=0.2)
        with pytest.raises(ValueError):
            ReactionResult(status="complete")
        with pytest.raises(ValueError):
            Mark(
                price=float("nan"),
                bar_at=dt("2026-11-05T14:59:00"),
                target_at=dt("2026-11-05T15:00:00"),
                feed="sip",
                adjustment="split",
                currency="USD",
                endpoint="historical_bars",
                timeframe="1Min",
            )


class TestProvider:
    def test_exact_minute_shape(self):
        """Open+30 selects 09:59 ET, never the 10:00 bar."""
        client = Mock()
        target = dt("2026-11-05T15:00:00")
        client.get_stock_bars.return_value = SimpleNamespace(
            data={
                "MSFT": [
                    SimpleNamespace(timestamp=target - timedelta(minutes=1), close=100)
                ]
            }
        )
        provider = EarningsSIPProvider(client)
        now = target + timedelta(minutes=16)
        mark = provider.mark("MSFT", target, minute=True, now=now)
        assert mark["price"] == 100
        request = client.get_stock_bars.call_args[0][0]
        assert request.start == datetime(2026, 11, 5, 14, 59)
        assert request.end < datetime(2026, 11, 5, 15, 0)
        assert request.feed.value == "sip"
        assert request.adjustment.value == "split"
        provider.mark("MSFT", target, minute=True, now=now)
        assert client.get_stock_bars.call_count == 2
        with pytest.raises(ValueError, match="matured"):
            provider.mark("MSFT", target, minute=True, now=target)

    @pytest.mark.parametrize("offset", [-2, 0, 1])
    def test_no_bar_substitution(self, offset):
        """Earlier and later bars produce no mark."""
        target = dt("2026-11-05T15:00:00")
        client = Mock()
        client.get_stock_bars.return_value = SimpleNamespace(
            data={
                "MSFT": [
                    SimpleNamespace(
                        timestamp=target + timedelta(minutes=offset), close=100
                    )
                ]
            }
        )
        assert (
            EarningsSIPProvider(client).mark(
                "MSFT", target, minute=True, now=target + timedelta(minutes=16)
            )
            is None
        )

    def test_daily_shape(self):
        """Daily mark uses exact exchange-local day and a matured end."""
        client = Mock()
        client.get_stock_bars.return_value = SimpleNamespace(
            data={
                "MSFT": [
                    SimpleNamespace(timestamp=dt("2026-11-04T05:00:00"), close=100)
                ]
            }
        )
        assert (
            EarningsSIPProvider(client).mark(
                "MSFT",
                dt("2026-11-04T21:00:00"),
                minute=False,
                now=dt("2026-11-04T21:16:00"),
            )["price"]
            == 100
        )

    def test_sec_metadata_utc_and_bounds(self):
        """SEC acceptance remains a UTC source timestamp, with no report inference."""
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.status_code = 200
        payload = {
            "cik": "789019",
            "tickers": ["MSFT"],
            "filings": {
                "recent": {
                    "accessionNumber": ["0000789019-26-000001"],
                    "form": ["8-K"],
                    "primaryDocument": ["results.htm"],
                    "acceptanceDateTime": ["2026-11-04T21:04:44Z"],
                }
            },
        }
        response.iter_content.return_value = [json.dumps(payload).encode()]
        session = Mock()
        session.get.return_value = response
        provider = SECMetadataProvider("Research contact@example.com", session)
        citation = provider.citation(
            cik="789019",
            accession="0000789019-26-000001",
            issuer_id="microsoft",
            symbol="MSFT",
            now=dt("2026-11-05T12:00:00"),
            paraphrase="Results filing",
        )
        assert citation.published_at == dt("2026-11-04T21:04:44")
        assert citation.source_type == "sec_filing"
        assert session.get.call_args.kwargs["allow_redirects"] is False
        with pytest.raises(ValueError, match="interval"):
            provider.citation(
                cik="789019",
                accession="0000789019-26-000001",
                issuer_id="microsoft",
                symbol="MSFT",
                now=dt("2026-11-05T12:00:00"),
                paraphrase="Results filing",
            )


class TestReadOnlyAndCLI:
    def test_candidate_status_excludes_later_knowledge(self, ledger):
        """Later fact corrections cannot leak into a source-event snapshot."""
        l = ledger
        sid = l.store.add_source(l.source.model_copy(update={"issuer_id": "arista"}))
        schedule = l.schedule.model_copy(
            update={
                "source_id": sid,
                "scheduled_date": dt("2026-11-06T00:00:00").date(),
                "scheduled_release_at": dt("2026-11-06T21:05:00"),
            }
        )
        candidate = l.store.register(l.registry, "2026-q3", "ANET", "FY26Q3", schedule)
        l.clock[0] = dt("2026-11-04T21:06:00")
        add_facts(l)
        report = read_earnings(l.store.path, cutoff=l.clock[0])
        event = next(e for e in report["events"] if e["id"] == l.event)
        assert event["candidate_status"]["ANET"] == "scheduled_later"
        # News recorded after the source release cannot rewrite contemporaneous knowledge.
        sid = l.store.add_source(
            l.source.model_copy(
                update={
                    "issuer_id": "arista",
                    "source_type": "issuer_release",
                    "accessed_at": l.clock[0],
                }
            )
        )
        l.store.revise(
            candidate,
            Facts(
                actual_release_at=dt("2026-11-04T20:00:00"),
                actual_release_time_kind="issuer_exact",
                source_ids=(sid,),
                author="operator",
                note="Late-discovered release",
            ),
        )
        event = next(
            e
            for e in read_earnings(l.store.path, cutoff=l.clock[0])["events"]
            if e["id"] == l.event
        )
        assert event["candidate_status"]["ANET"] == "scheduled_later"

    def test_unrelated_database_rejected(self, tmp_path):
        """An accidental trade-database path cannot acquire earnings tables."""
        path = tmp_path / "unrelated.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE trades(id INTEGER)")
        with pytest.raises(ValueError, match="unrelated"):
            EarningsStore(path)
        with sqlite3.connect(path) as conn:
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall() == [("trades",)]

    def test_cli_workflow(self, tmp_path, monkeypatch, capsys):
        """Exercise the public operator commands and generate a sanitized fixture."""
        import reporting.earnings_observations as module

        clock = [dt("2026-11-03T12:00:00")]
        real_store = module.EarningsStore
        monkeypatch.setattr(
            "scripts.earnings_observations.EarningsStore",
            lambda path: real_store(path, clock=lambda: clock[0]),
        )
        path = tmp_path / "cli.db"
        prefix = [
            "--db",
            str(path),
            "--registry",
            str(ROOT / "config/earnings_registry_v1.json"),
        ]

        def run(arguments):
            """Capture structured CLI output without using shell interpolation."""
            assert main(prefix + arguments) == 0
            return json.loads(capsys.readouterr().out)

        def payload(name, body):
            """Write generated test inputs, never production data."""
            file = tmp_path / name
            file.write_text(json.dumps(body))
            return str(file)

        run(["init"])
        run(
            [
                "cycle",
                "pilot",
                "--prospective-cutoff",
                "2026-11-03T12:00:00+00:00",
                "--note",
                "Synthetic fixture",
            ]
        )
        source = {
            "issuer_id": "microsoft",
            "source_type": "issuer_schedule",
            "title": "Synthetic schedule",
            "url": "https://example.com/synthetic",
            "accessed_at": clock[0].isoformat(),
            "paraphrase": "Synthetic test data",
        }
        source_id = run(["source", "--json", payload("source.json", source)])[
            "source_id"
        ]
        schedule = {
            "scheduled_date": "2026-11-04",
            "scheduled_release_at": "2026-11-04T21:05:00+00:00",
            "precision": "exact",
            "source_id": source_id,
            "author": "fixture",
        }
        event = run(
            [
                "register",
                "--cycle",
                "pilot",
                "--issuer",
                "MSFT",
                "--period",
                "FY27Q1",
                "--json",
                payload("schedule.json", schedule),
            ]
        )["event_id"]
        clock[0] = dt("2026-11-04T21:06:00")
        source.update(source_type="issuer_release", accessed_at=clock[0].isoformat())
        sid = run(["source", "--json", payload("release.json", source)])["source_id"]
        facts = {
            "actual_release_at": "2026-11-04T21:05:00+00:00",
            "actual_release_time_kind": "issuer_exact",
            "source_ids": [sid],
            "author": "fixture",
            "note": "Synthetic release",
        }
        run(["facts", event, "--json", payload("facts.json", facts)])
        label = {
            "cluster_id": "hyperscaler_ai_capex",
            "dimension": "demand",
            "label": "neutral",
            "source_ids": [sid],
            "author": "fixture",
            "evidence": "Synthetic observation",
        }
        run(["label", event, "--json", payload("label.json", label)])
        result = run(["export", "--as-of", clock[0].isoformat()])
        assert result["coverage"]["events"] == 1
        assert result["coverage"]["jobs"] == 48
        assert result["events"][0]["labels"][0]["data"]["label"] == "neutral"

    def test_missing_export_creates_nothing(self, tmp_path, capsys):
        """A dashboard/CLI read never creates its database."""
        path = tmp_path / "missing.db"
        assert main(["--db", str(path), "export", "--corrected"]) == 0
        assert json.loads(capsys.readouterr().out)["state"] == "missing"
        assert not path.exists()

    def test_collection_disabled_by_default(self, tmp_path, monkeypatch):
        """Explicit collection switch is required before provider construction."""
        from config import settings

        monkeypatch.setattr(settings, "EARNINGS_OBSERVATIONS_ENABLED", False)
        path = tmp_path / "disabled.db"
        assert main(["--db", str(path), "collect"]) == 2
        assert not path.exists()

    def test_no_strategy_or_engine_consumer(self):
        """Observation modules cannot become a trading input accidentally."""
        import ast

        for directory in ("engine", "strategies", "risk", "execution"):
            for path in (ROOT / directory).rglob("*.py"):
                tree = ast.parse(path.read_text())
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        assert "earnings" not in (node.module or "")
                    elif isinstance(node, ast.Import):
                        assert all("earnings" not in name.name for name in node.names)
