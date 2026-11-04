"""Persistence, recovery, and composite scheduling tests for pre-open data."""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

from data.preopen_observation import (
    InstrumentSpec,
    QuoteSample,
    ReferenceClose,
    SessionContext,
    UniverseSpec,
    calculate_mark,
    checkpoints_for_session,
    logical_key,
)
from data.preopen_provider import ProviderFailure
from forward_test import _build_preopen_observation_scheduler
from config import settings
from reporting.market_observations import MarketObservationStore, ObservationWrite
from reporting.preopen_scheduler import PreopenObservationScheduler
from utils.hooks import CompositePostCycleHook


UTC = timezone.utc


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class _Calendar:
    def __init__(self, sessions):
        self.sessions = tuple(sessions)

    def session_contexts(self, start, end):
        return tuple(
            session for session in self.sessions
            if start <= session.session_date <= end
        )


class _Provider:
    def __init__(self, *, failure=None, missing_symbols=()):
        self.failure = failure
        self.missing_symbols = set(missing_symbols)
        self.quote_calls = []

    def reference_closes(self, symbols, *, previous_session_date):
        return {
            symbol: ReferenceClose(
                session_date=previous_session_date,
                timestamp=datetime(2026, 11, 3, 21, 0, tzinfo=UTC),
                price=100.0,
            )
            for symbol in symbols
        }

    def latest_quote(self, symbol, *, session_date, checkpoint_at):
        self.quote_calls.append((symbol, session_date, checkpoint_at))
        if self.failure is not None:
            raise self.failure
        if symbol in self.missing_symbols:
            return None
        return QuoteSample(
            timestamp=checkpoint_at - timedelta(seconds=10),
            bid_price=100.0,
            bid_size=10,
            bid_exchange="P",
            ask_price=100.2,
            ask_size=12,
            ask_exchange="Q",
        )


def _universe(effective=date(2026, 11, 4)):
    return UniverseSpec.build(
        InstrumentSpec(symbol, "etf", role, effective)
        for symbol, role in (
            ("SPY", "broad_market"),
            ("QQQ", "growth_market"),
            ("XLK", "technology"),
            ("SOXX", "semiconductors"),
        )
    )


def _session(day=date(2026, 11, 4)):
    return SessionContext(
        session_date=day,
        open_at=datetime(2026, 11, 4, 14, 30, tzinfo=UTC),
        previous_session_date=date(2026, 11, 3),
    )


class TestSchedulerCollection:
    def test_first_checkpoint_finalizes_four_independent_symbols(self, tmp_path):
        clock = _Clock(datetime(2026, 11, 4, 13, 17, tzinfo=UTC))
        provider = _Provider()
        store = MarketObservationStore(tmp_path / "observations.db")
        scheduler = PreopenObservationScheduler(
            universe=_universe(),
            store=store,
            provider=provider,
            calendar=_Calendar((_session(),)),
            clock=clock,
        )
        assert scheduler.collect_due() == 4
        assert store.count_observations() == 4
        assert [call[0] for call in provider.quote_calls] == [
            "QQQ", "SOXX", "SPY", "XLK"
        ]
        with sqlite3.connect(store.path) as conn:
            statuses = conn.execute(
                "SELECT observation_status FROM preopen_observations"
            ).fetchall()
        assert statuses == [("complete",)] * 4

    def test_same_cycle_is_idempotent(self, tmp_path):
        clock = _Clock(datetime(2026, 11, 4, 13, 17, tzinfo=UTC))
        store = MarketObservationStore(tmp_path / "observations.db")
        scheduler = PreopenObservationScheduler(
            universe=_universe(), store=store, provider=_Provider(),
            calendar=_Calendar((_session(),)), clock=clock,
        )
        assert scheduler.collect_due() == 4
        assert scheduler.collect_due() == 0
        assert store.count_observations() == 4

    def test_one_missing_symbol_does_not_make_other_symbols_unavailable(self, tmp_path):
        clock = _Clock(datetime(2026, 11, 4, 13, 17, tzinfo=UTC))
        store = MarketObservationStore(tmp_path / "observations.db")
        scheduler = PreopenObservationScheduler(
            universe=_universe(), store=store,
            provider=_Provider(missing_symbols=("XLK",)),
            calendar=_Calendar((_session(),)), clock=clock,
        )
        assert scheduler.collect_due() == 4
        with sqlite3.connect(store.path) as conn:
            statuses = dict(
                conn.execute(
                    "SELECT symbol, observation_status FROM preopen_observations"
                ).fetchall()
            )
        assert statuses["XLK"] == "unavailable"
        assert statuses["SPY"] == "complete"
        assert statuses["QQQ"] == "complete"
        assert statuses["SOXX"] == "complete"

    def test_restart_discovers_prior_session_jobs(self, tmp_path):
        prior = SessionContext(
            session_date=date(2026, 11, 4),
            open_at=datetime(2026, 11, 4, 14, 30, tzinfo=UTC),
            previous_session_date=date(2026, 11, 3),
        )
        current = SessionContext(
            session_date=date(2026, 11, 5),
            open_at=datetime(2026, 11, 5, 14, 30, tzinfo=UTC),
            previous_session_date=date(2026, 11, 4),
        )
        clock = _Clock(datetime(2026, 11, 5, 13, 17, tzinfo=UTC))
        store = MarketObservationStore(tmp_path / "observations.db")
        scheduler = PreopenObservationScheduler(
            universe=_universe(), store=store, provider=_Provider(),
            calendar=_Calendar((prior, current)), clock=clock,
        )
        # Recovery is bounded to four keys per engine hook. Four calls drain
        # three prior checkpoints (12) plus today's first checkpoint (4).
        assert [scheduler.collect_due() for _ in range(4)] == [4, 4, 4, 4]
        assert store.count_observations() == 16

    def test_correction_appends_revision_without_mutating_original(self, tmp_path):
        clock = _Clock(datetime(2026, 11, 4, 13, 17, tzinfo=UTC))
        universe = _universe()
        store = MarketObservationStore(tmp_path / "observations.db")
        scheduler = PreopenObservationScheduler(
            universe=universe, store=store, provider=_Provider(),
            calendar=_Calendar((_session(),)), clock=clock,
        )
        scheduler.collect_due()
        instrument = next(item for item in universe.instruments if item.symbol == "SPY")
        checkpoint = checkpoints_for_session(_session())[0]
        key = logical_key(
            universe_version=universe.universe_version,
            session_date=_session().session_date,
            checkpoint=checkpoint.name,
            symbol="SPY",
        )
        with sqlite3.connect(store.path) as conn:
            parent_id = conn.execute(
                "SELECT observation_id FROM preopen_observations WHERE logical_key = ?",
                (key,),
            ).fetchone()[0]
        quote = QuoteSample(
            timestamp=checkpoint.checkpoint_at - timedelta(seconds=5),
            bid_price=100.0, bid_size=1, bid_exchange="P",
            ask_price=100.4, ask_size=1, ask_exchange="Q",
        )
        reference = ReferenceClose(
            session_date=date(2026, 11, 3),
            timestamp=datetime(2026, 11, 3, 21, 0, tzinfo=UTC),
            price=100.0,
        )
        calculated = calculate_mark(
            checkpoint_at=checkpoint.checkpoint_at,
            quote=quote,
            reference=reference,
        )
        store.append_correction(
            ObservationWrite(
                logical_key=key,
                universe_version=universe.universe_version,
                session_date=date(2026, 11, 4),
                checkpoint=checkpoint,
                instrument=instrument,
                first_attempt_at=clock.value,
                retry_deadline_at=clock.value + timedelta(days=7),
                collected_at=clock.value + timedelta(minutes=1),
                collection_state="on_schedule",
                attempt_count=1,
                status=calculated.status,
                reason_code=None,
                diagnostic=None,
                quote=quote,
                reference=reference,
                calculated=calculated,
                revision=2,
                supersedes_id=parent_id,
                correction_reason="provider correction fixture",
            )
        )
        with sqlite3.connect(store.path) as conn:
            revisions = conn.execute(
                "SELECT revision FROM preopen_observations WHERE logical_key = ? ORDER BY revision",
                (key,),
            ).fetchall()
        assert revisions == [(1,), (2,)]


class TestSchedulerRetries:
    def test_transient_failure_stays_pending_then_retries(self, tmp_path):
        clock = _Clock(datetime(2026, 11, 4, 13, 17, tzinfo=UTC))
        failure = ProviderFailure(
            "transient_failure", "http_500", "server error", True
        )
        provider = _Provider(failure=failure)
        store = MarketObservationStore(tmp_path / "observations.db")
        scheduler = PreopenObservationScheduler(
            universe=_universe(), store=store, provider=provider,
            calendar=_Calendar((_session(),)), clock=clock,
        )
        assert scheduler.collect_due() == 4
        assert store.count_observations() == 0
        clock.value += timedelta(minutes=4)
        assert scheduler.collect_due() == 0
        clock.value += timedelta(minutes=2)
        assert scheduler.collect_due() == 4
        assert store.count_observations() == 0

    def test_final_recovery_round_can_succeed_after_seven_days(self, tmp_path):
        clock = _Clock(datetime(2026, 11, 4, 13, 17, tzinfo=UTC))
        provider = _Provider(
            failure=ProviderFailure(
                "transient_failure", "transport_error", "offline", True
            )
        )
        store = MarketObservationStore(tmp_path / "observations.db")
        scheduler = PreopenObservationScheduler(
            universe=_universe(), store=store, provider=provider,
            calendar=_Calendar((_session(),)), clock=clock,
        )
        assert scheduler.collect_due() == 4
        clock.value += timedelta(days=8)
        provider.failure = None
        # The first bounded round recovers the four pending early-checkpoint
        # keys; later never-attempted checkpoints remain queued for later hooks.
        assert scheduler.collect_due() == 4
        assert store.count_observations() == 4

    def test_non_retryable_entitlement_failure_finalizes_once(self, tmp_path):
        clock = _Clock(datetime(2026, 11, 4, 13, 17, tzinfo=UTC))
        provider = _Provider(
            failure=ProviderFailure(
                "entitlement_error", "http_403", "forbidden", False
            )
        )
        store = MarketObservationStore(tmp_path / "observations.db")
        scheduler = PreopenObservationScheduler(
            universe=_universe(), store=store, provider=provider,
            calendar=_Calendar((_session(),)), clock=clock,
        )
        assert scheduler.collect_due() == 4
        assert store.count_observations() == 4
        assert scheduler.collect_due() == 0
        with sqlite3.connect(store.path) as conn:
            statuses = conn.execute(
                "SELECT DISTINCT observation_status FROM preopen_observations"
            ).fetchall()
        assert statuses == [("entitlement_error",)]


class TestCompositeHook:
    def test_failure_in_one_hook_does_not_suppress_next(self):
        calls = []

        def failing():
            calls.append("health")
            raise RuntimeError("boom")

        def succeeding():
            calls.append("preopen")

        CompositePostCycleHook(failing, succeeding)()
        assert calls == ["health", "preopen"]


class TestForwardTestWiring:
    def test_disabled_collector_builds_no_hook(self, monkeypatch):
        monkeypatch.setattr(settings, "PREOPEN_OBSERVATIONS_ENABLED", False)
        assert _build_preopen_observation_scheduler() is None

    def test_setup_failure_disables_collector_without_raising(self, monkeypatch):
        monkeypatch.setattr(settings, "PREOPEN_OBSERVATIONS_ENABLED", True)
        monkeypatch.setattr(
            settings,
            "PREOPEN_OBSERVATION_EFFECTIVE_FROM_SESSION",
            "not-a-date",
        )
        assert _build_preopen_observation_scheduler() is None
