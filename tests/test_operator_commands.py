"""Unit tests for `engine.operator_queue` — `OperatorCommandStore`.

Operator Controls Phase A PR-2. Verifies:

- Schema creation via `TradeLogger._ensure_db()` is idempotent.
- `new_command_uid()` produces unique `cmd_<hex>` IDs.
- `insert` validates action, command_uid format, reason.
- `claim_next_pending` atomically transitions oldest pending to accepted,
  expires stale rows in the same pass, and never returns the same row twice.
- Terminal-state writes (`mark_succeeded`, `mark_failed`, `mark_rejected`).
- Read helpers — `get_by_command_uid`, `recent`, `count_pending`.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from engine.operator_queue import (
    OPERATOR_QUEUE_SCHEMA_VERSION,
    OperatorCommandStore,
    VALID_ACTIONS,
    VALID_STATUSES,
    new_command_uid,
)
from reporting.logger import TradeLogger


@pytest.fixture
def store(tmp_path) -> OperatorCommandStore:
    db_path = tmp_path / "trades.db"
    tl = TradeLogger(path=str(db_path))
    conn = tl._ensure_db()
    return OperatorCommandStore(conn)


@pytest.fixture
def conn(tmp_path) -> sqlite3.Connection:
    db_path = tmp_path / "trades.db"
    return TradeLogger(path=str(db_path))._ensure_db()


class TestIDGenerator:
    def test_format(self):
        uid = new_command_uid()
        assert uid.startswith("cmd_")
        assert len(uid) == 36

    def test_unique(self):
        uids = {new_command_uid() for _ in range(100)}
        assert len(uids) == 100


class TestSchemaMigration:
    def test_ensure_db_creates_operator_commands(self, tmp_path):
        db_path = tmp_path / "trades.db"
        conn = TradeLogger(path=str(db_path))._ensure_db()
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "operator_commands" in tables

    def test_indexes_created(self, conn):
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_operator_commands%'"
            ).fetchall()
        }
        assert "idx_operator_commands_status_created" in names
        assert "idx_operator_commands_target_position_uid" in names
        assert "idx_operator_commands_target_strategy" in names

    def test_idempotent(self, tmp_path):
        db_path = tmp_path / "trades.db"
        TradeLogger(path=str(db_path))._ensure_db()
        # Second open must not raise or duplicate work.
        conn2 = TradeLogger(path=str(db_path))._ensure_db()
        assert conn2.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type='table' AND name='operator_commands'"
        ).fetchone()[0] == 1


class TestInsert:
    def test_creates_pending_row(self, store):
        uid = new_command_uid()
        store.insert(
            command_uid=uid,
            action="halt",
            reason="market event",
            requested_by="franco",
        )
        row = store.get_by_command_uid(uid)
        assert row is not None
        assert row.status == "pending"
        assert row.action == "halt"
        assert row.reason == "market event"
        assert row.requested_by == "franco"
        assert row.accepted_at is None
        assert row.completed_at is None
        assert row.params == {}

    def test_persists_params_as_json(self, store):
        uid = new_command_uid()
        store.insert(
            command_uid=uid,
            action="halt",
            reason="test",
            params={"severity": "high", "scope": "all"},
        )
        row = store.get_by_command_uid(uid)
        assert row.params == {"severity": "high", "scope": "all"}

    def test_rejects_unknown_action(self, store):
        with pytest.raises(ValueError, match="action must be one of"):
            store.insert(
                command_uid=new_command_uid(),
                # Use a deliberately-fake action so this test stays
                # accurate across Phase A/B/C extensions of the enum.
                action="not-a-real-action",
                reason="test",
            )

    def test_rejects_bad_command_uid(self, store):
        with pytest.raises(ValueError, match="command_uid"):
            store.insert(
                command_uid="not_a_cmd_uid",
                action="halt",
                reason="test",
            )

    def test_rejects_empty_reason(self, store):
        with pytest.raises(ValueError, match="reason"):
            store.insert(
                command_uid=new_command_uid(),
                action="halt",
                reason="",
            )

    def test_rejects_whitespace_only_reason(self, store):
        with pytest.raises(ValueError, match="reason"):
            store.insert(
                command_uid=new_command_uid(),
                action="halt",
                reason="   ",
            )

    def test_duplicate_command_uid_raises_integrity(self, store):
        uid = new_command_uid()
        store.insert(command_uid=uid, action="halt", reason="t")
        with pytest.raises(sqlite3.IntegrityError):
            store.insert(command_uid=uid, action="halt", reason="t")


class TestClaimNextPending:
    def test_returns_none_when_empty(self, store):
        assert store.claim_next_pending(expiry_seconds=180) is None

    def test_claims_oldest_first(self, store):
        # Insert two; both fresh; claim should pick the older one.
        uid_a, uid_b = new_command_uid(), new_command_uid()
        store.insert(command_uid=uid_a, action="halt", reason="first")
        store.insert(command_uid=uid_b, action="halt", reason="second")
        claimed = store.claim_next_pending(expiry_seconds=180)
        assert claimed.command_uid == uid_a
        assert claimed.status == "accepted"
        assert claimed.accepted_at is not None

    def test_does_not_reclaim_same_row(self, store):
        uid = new_command_uid()
        store.insert(command_uid=uid, action="halt", reason="t")
        first = store.claim_next_pending(expiry_seconds=180)
        assert first.command_uid == uid
        second = store.claim_next_pending(expiry_seconds=180)
        assert second is None

    def test_expires_stale_pending(self, store):
        # Insert with a backdated created_at to simulate staleness.
        # Bypass insert validation by writing directly to confirm the
        # claim path itself does the expiration.
        old_uid = new_command_uid()
        old_iso = (
            datetime.now(timezone.utc) - timedelta(hours=2)
        ).isoformat()
        store._conn.execute(
            "INSERT INTO operator_commands ("
            "schema_version, command_uid, created_at, action, reason, status, params_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                OPERATOR_QUEUE_SCHEMA_VERSION,
                old_uid,
                old_iso,
                "halt",
                "stale",
                "pending",
                "{}",
            ),
        )
        store._conn.commit()

        # And a fresh pending row.
        fresh_uid = new_command_uid()
        store.insert(command_uid=fresh_uid, action="halt", reason="fresh")

        # Claim should expire the stale one and return the fresh one.
        claimed = store.claim_next_pending(expiry_seconds=180)
        assert claimed.command_uid == fresh_uid
        assert claimed.status == "accepted"

        # The stale row was transitioned to rejected_expired.
        stale = store.get_by_command_uid(old_uid)
        assert stale.status == "rejected_expired"
        assert stale.completed_at is not None

    def test_expiry_uses_injected_now(self, store):
        uid = new_command_uid()
        store.insert(command_uid=uid, action="halt", reason="t")
        # Pretend a year has passed.
        far_future = (
            datetime.now(timezone.utc) + timedelta(days=365)
        ).isoformat()
        claimed = store.claim_next_pending(
            now_iso=far_future, expiry_seconds=180
        )
        assert claimed is None
        row = store.get_by_command_uid(uid)
        assert row.status == "rejected_expired"


class TestTerminalTransitions:
    def _claimed_uid(self, store) -> str:
        uid = new_command_uid()
        store.insert(command_uid=uid, action="halt", reason="t")
        store.claim_next_pending(expiry_seconds=180)
        return uid

    def test_mark_succeeded(self, store):
        uid = self._claimed_uid(store)
        store.mark_succeeded(
            command_uid=uid,
            result={"halt_engaged": True},
        )
        row = store.get_by_command_uid(uid)
        assert row.status == "succeeded"
        assert row.completed_at is not None
        assert row.result == {"halt_engaged": True}

    def test_mark_failed(self, store):
        uid = self._claimed_uid(store)
        store.mark_failed(
            command_uid=uid,
            result={"error": "kill switch already engaged"},
        )
        row = store.get_by_command_uid(uid)
        assert row.status == "failed"
        assert row.result == {"error": "kill switch already engaged"}

    def test_mark_rejected_unsupported(self, store):
        uid = self._claimed_uid(store)
        store.mark_rejected(
            command_uid=uid,
            status="rejected_unsupported_phase_a",
            result={"note": "wait for phase b"},
        )
        row = store.get_by_command_uid(uid)
        assert row.status == "rejected_unsupported_phase_a"

    def test_mark_rejected_validation(self, store):
        uid = self._claimed_uid(store)
        store.mark_rejected(
            command_uid=uid,
            status="rejected_validation",
            result={"reason": "no target position"},
        )
        assert store.get_by_command_uid(uid).status == "rejected_validation"

    def test_mark_rejected_rejects_unknown_status(self, store):
        uid = self._claimed_uid(store)
        with pytest.raises(ValueError):
            store.mark_rejected(
                command_uid=uid,
                status="invalid_status",
            )


class TestReads:
    def test_recent_newest_first(self, store):
        uids = []
        for i in range(3):
            uid = new_command_uid()
            store.insert(command_uid=uid, action="halt", reason=f"#{i}")
            uids.append(uid)
        rows = store.recent(limit=10)
        # Newest first.
        assert [r.command_uid for r in rows] == list(reversed(uids))

    def test_recent_respects_limit(self, store):
        for i in range(5):
            store.insert(
                command_uid=new_command_uid(),
                action="halt",
                reason=f"#{i}",
            )
        rows = store.recent(limit=2)
        assert len(rows) == 2

    def test_recent_limit_zero_returns_empty(self, store):
        store.insert(
            command_uid=new_command_uid(), action="halt", reason="t"
        )
        assert store.recent(limit=0) == []

    def test_count_pending(self, store):
        assert store.count_pending() == 0
        for _ in range(3):
            store.insert(
                command_uid=new_command_uid(),
                action="halt",
                reason="t",
            )
        assert store.count_pending() == 3
        # After claim, the pending count drops.
        store.claim_next_pending(expiry_seconds=180)
        assert store.count_pending() == 2

    def test_get_by_unknown_uid_returns_none(self, store):
        assert store.get_by_command_uid("cmd_doesnotexist00000000000000000000") is None


class TestStatusEnum:
    def test_valid_actions_covers_all_shipped_phases(self):
        """Phase A: halt / resume-after-halt. Phase B: 4 soft pauses.
        Phase C: 3 destructive position controls."""
        assert VALID_ACTIONS == {
            "halt",
            "resume-after-halt",
            "pause-entries",
            "resume-entries",
            "pause-strategy",
            "resume-strategy",
            "close-position",
            "reduce-position",
            "cancel-position-orders",
        }

    def test_valid_statuses_documented(self):
        expected = {
            "pending",
            "accepted",
            "succeeded",
            "failed",
            "rejected_expired",
            "rejected_unsupported_phase_a",
            "rejected_validation",
        }
        assert VALID_STATUSES == expected
