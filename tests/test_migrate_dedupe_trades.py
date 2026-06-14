"""Unit tests for scripts/migrate_dedupe_trades.py.

§12.2 of the discovery doc (PR #59) requires a detection-only default
behavior so the operator cannot accidentally mutate the DB by running
the script. PR #60 commit 7 review (R8-2 follow-up): the script must
also ship in this PR, not be referenced from runtime-only.

Tests cover:
  - --detect: clean DB exits 0, dirty DB exits 1, no writes
  - --review: emits a JSON decisions file with sensible defaults
  - --apply: runs deletes inside one transaction; rolls back on error
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

# Make sure the script's path is importable regardless of how pytest
# was invoked.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "scripts"))

import migrate_dedupe_trades  # noqa: E402


def _make_dirty_db(path: Path) -> None:
    """Build a legacy-shape DB with both owner_key and trades.order_id
    duplicates so all three script modes have something to chew on."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "timestamp TEXT, symbol TEXT, side TEXT, qty REAL, "
        "avg_fill_price REAL, order_id TEXT, strategy TEXT, "
        "reason TEXT, stop_price REAL, entry_reference_price REAL, "
        "modeled_slippage_bps REAL, realized_slippage_bps REAL, "
        "order_type TEXT, status TEXT, requested_qty REAL, "
        "filled_qty REAL, position_type TEXT, position_uid TEXT)"
    )
    conn.execute(
        "CREATE TABLE position_lifecycle ("
        "schema_version INTEGER, position_uid TEXT, "
        "created_at TEXT, closed_at TEXT, symbol TEXT, "
        "owner_key TEXT, strategy TEXT, position_type TEXT, "
        "status TEXT, entry_qty REAL, current_qty REAL, "
        "avg_entry_price REAL, net_realized_pnl REAL, "
        "entry_order_id TEXT, entry_client_order_id TEXT, "
        "first_fill_at TEXT, last_fill_at TEXT, "
        "metadata_json TEXT, opened_at TEXT)"
    )
    # Two trades rows sharing order_id (single_leg scope).
    conn.execute(
        "INSERT INTO trades "
        "(timestamp, symbol, side, qty, order_id, position_type) "
        "VALUES ('2026-06-01T10:00:00+00:00', 'AAPL', 'buy', 10, "
        "'ord-dup', 'single_leg')"
    )
    conn.execute(
        "INSERT INTO trades "
        "(timestamp, symbol, side, qty, order_id, position_type) "
        "VALUES ('2026-06-01T11:00:00+00:00', 'AAPL', 'buy', 10, "
        "'ord-dup', 'single_leg')"
    )
    # Two open position_lifecycle rows sharing owner_key.
    conn.execute(
        "INSERT INTO position_lifecycle "
        "(schema_version, position_uid, owner_key, status, strategy, "
        "opened_at) "
        "VALUES (1, 'pos-A', 'TSLA', 'open', 'sma', "
        "'2026-05-01T10:00:00+00:00')"
    )
    conn.execute(
        "INSERT INTO position_lifecycle "
        "(schema_version, position_uid, owner_key, status, strategy, "
        "opened_at) "
        "VALUES (1, 'pos-B', 'TSLA', 'open', 'sma', "
        "'2026-05-02T10:00:00+00:00')"
    )
    conn.commit()
    conn.close()


def _make_clean_db(path: Path) -> None:
    """Empty DB with the right schema — no duplicates."""
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE trades ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "order_id TEXT, position_type TEXT)"
    )
    conn.execute(
        "CREATE TABLE position_lifecycle ("
        "schema_version INTEGER, position_uid TEXT, "
        "owner_key TEXT, status TEXT, strategy TEXT, "
        "symbol TEXT, opened_at TEXT)"
    )
    conn.commit()
    conn.close()


class TestDetectMode:
    def test_clean_db_exits_zero(self, tmp_path, capsys):
        db = tmp_path / "trades.db"
        _make_clean_db(db)
        code = migrate_dedupe_trades.main(["--db", str(db), "--detect"])
        assert code == 0
        captured = capsys.readouterr()
        assert "CLEAN" in captured.out

    def test_dirty_db_exits_one(self, tmp_path, capsys):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        code = migrate_dedupe_trades.main(["--db", str(db), "--detect"])
        assert code == 1
        captured = capsys.readouterr()
        assert "DIRTY" in captured.out
        assert "ord-dup" in captured.out
        assert "TSLA" in captured.out

    def test_detect_does_not_mutate(self, tmp_path):
        """The detection-only default is the script's R8-2 safety
        property — read-only, no implicit deletes."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        trade_ids_before = sqlite3.connect(db).execute(
            "SELECT id FROM trades ORDER BY id"
        ).fetchall()
        position_uids_before = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle ORDER BY position_uid"
        ).fetchall()
        migrate_dedupe_trades.main(["--db", str(db), "--detect"])
        trade_ids_after = sqlite3.connect(db).execute(
            "SELECT id FROM trades ORDER BY id"
        ).fetchall()
        position_uids_after = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle ORDER BY position_uid"
        ).fetchall()
        assert trade_ids_before == trade_ids_after
        assert position_uids_before == position_uids_after

    def test_missing_db_path_returns_two(self, tmp_path, capsys):
        code = migrate_dedupe_trades.main(
            ["--db", str(tmp_path / "nonexistent.db"), "--detect"],
        )
        assert code == 2


class TestReviewMode:
    def test_review_writes_decisions_file(self, tmp_path):
        db = tmp_path / "trades.db"
        out = tmp_path / "decisions.json"
        _make_dirty_db(db)
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--review", str(out)],
        )
        assert code == 0
        assert out.exists()
        decisions = json.loads(out.read_text())
        assert decisions["version"] == 1
        assert len(decisions["owner_key_clusters"]) == 1
        assert len(decisions["trades_order_id_clusters"]) == 1

    def test_review_defaults_keep_earliest(self, tmp_path):
        """The earliest-timestamp row is the proposed keeper for each
        cluster. Operator can edit before --apply."""
        db = tmp_path / "trades.db"
        out = tmp_path / "decisions.json"
        _make_dirty_db(db)
        migrate_dedupe_trades.main(
            ["--db", str(db), "--review", str(out)],
        )
        decisions = json.loads(out.read_text())
        # _make_dirty_db inserts trades with timestamps 10:00 then 11:00;
        # earliest is id=1.
        cluster = decisions["trades_order_id_clusters"][0]
        assert cluster["keep_trade_id"] == 1
        assert cluster["delete_trade_ids"] == [2]
        # And the position_lifecycle cluster: pos-A opened 2026-05-01,
        # pos-B opened 2026-05-02 — keep pos-A.
        oc = decisions["owner_key_clusters"][0]
        assert oc["keep_position_uid"] == "pos-A"
        assert oc["delete_position_uids"] == ["pos-B"]

    def test_review_clean_db_emits_empty_file(self, tmp_path):
        db = tmp_path / "trades.db"
        out = tmp_path / "decisions.json"
        _make_clean_db(db)
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--review", str(out)],
        )
        assert code == 0
        decisions = json.loads(out.read_text())
        assert decisions["owner_key_clusters"] == []
        assert decisions["trades_order_id_clusters"] == []


class TestApplyMode:
    def test_apply_removes_listed_deletes(self, tmp_path):
        db = tmp_path / "trades.db"
        out = tmp_path / "decisions.json"
        _make_dirty_db(db)
        migrate_dedupe_trades.main(
            ["--db", str(db), "--review", str(out)],
        )
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 0
        # trades: id=1 kept, id=2 gone
        rows = sqlite3.connect(db).execute(
            "SELECT id FROM trades ORDER BY id"
        ).fetchall()
        assert rows == [(1,)]
        # position_lifecycle: pos-A kept, pos-B gone
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        assert rows == [("pos-A",)]

    def test_apply_then_detect_is_clean(self, tmp_path):
        """End-to-end: apply removes the duplicates so the next
        detect run exits 0."""
        db = tmp_path / "trades.db"
        out = tmp_path / "decisions.json"
        _make_dirty_db(db)
        migrate_dedupe_trades.main(
            ["--db", str(db), "--review", str(out)],
        )
        migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        code = migrate_dedupe_trades.main(["--db", str(db), "--detect"])
        assert code == 0

    def test_apply_unsupported_version_returns_two(self, tmp_path):
        db = tmp_path / "trades.db"
        out = tmp_path / "decisions.json"
        _make_dirty_db(db)
        out.write_text(json.dumps({"version": 999}))
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2


# ── PR #60 round 2 fixes ────────────────────────────────────────────────────


class TestReviewScopesToDetectedRowsOnly:
    """P0 from round 2: --review must fetch only rows the detector
    flagged, not every row matching the cluster key. Otherwise
    historical closed rows (owner_key) and legitimate spread legs
    (trades.order_id) get presented to the operator and the keep-
    earliest default proposes deleting valid positions."""

    def test_owner_key_review_excludes_historical_closed_rows(
        self, tmp_path,
    ):
        """A closed row plus two open duplicates: detector flags the
        two open rows. Review must propose deleting one of the open
        rows — NOT keeping the closed row and deleting both opens."""
        db = tmp_path / "trades.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "order_id TEXT, position_type TEXT)"
        )
        conn.execute(
            "CREATE TABLE position_lifecycle ("
            "schema_version INTEGER, position_uid TEXT, "
            "owner_key TEXT, status TEXT, strategy TEXT, "
            "symbol TEXT, opened_at TEXT)"
        )
        # Old closed row — outside the lock-holding status set.
        conn.execute(
            "INSERT INTO position_lifecycle "
            "(schema_version, position_uid, owner_key, status, strategy, "
            "opened_at) "
            "VALUES (1, 'pos-OLD', 'TSLA', 'closed', 'sma', "
            "'2025-01-01T10:00:00+00:00')"
        )
        # Two active duplicates.
        conn.execute(
            "INSERT INTO position_lifecycle "
            "(schema_version, position_uid, owner_key, status, strategy, "
            "opened_at) "
            "VALUES (1, 'pos-A', 'TSLA', 'open', 'sma', "
            "'2026-05-01T10:00:00+00:00')"
        )
        conn.execute(
            "INSERT INTO position_lifecycle "
            "(schema_version, position_uid, owner_key, status, strategy, "
            "opened_at) "
            "VALUES (1, 'pos-B', 'TSLA', 'open', 'sma', "
            "'2026-05-02T10:00:00+00:00')"
        )
        conn.commit()
        conn.close()

        out = tmp_path / "decisions.json"
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--review", str(out)],
        )
        assert code == 0
        decisions = json.loads(out.read_text())
        assert len(decisions["owner_key_clusters"]) == 1
        cluster = decisions["owner_key_clusters"][0]
        # pos-OLD must NOT appear anywhere in this cluster.
        all_uids = (
            [cluster["keep_position_uid"]]
            + cluster["delete_position_uids"]
            + [r["position_uid"] for r in cluster["rows"]]
        )
        assert "pos-OLD" not in all_uids
        # The two flagged opens are present.
        assert set(all_uids) == {"pos-A", "pos-B"}
        # Keeper is pos-A (earlier opened_at); delete pos-B.
        assert cluster["keep_position_uid"] == "pos-A"
        assert cluster["delete_position_uids"] == ["pos-B"]

    def test_trades_review_excludes_spread_legs_sharing_order_id(
        self, tmp_path,
    ):
        """A combo order with two spread legs sharing order_id PLUS
        two single_leg rows also sharing that order_id: detector
        flags the single_leg pair. Review must NOT include the spread
        legs in delete proposals."""
        db = tmp_path / "trades.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT, symbol TEXT, side TEXT, qty REAL, "
            "avg_fill_price REAL, order_id TEXT, position_type TEXT, "
            "position_uid TEXT, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE position_lifecycle ("
            "schema_version INTEGER, position_uid TEXT, "
            "owner_key TEXT, status TEXT, strategy TEXT, "
            "symbol TEXT, opened_at TEXT)"
        )
        # Two spread legs — same order_id, OUTSIDE the single_leg
        # scope. Detector must skip them.
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, order_id, position_type) "
            "VALUES ('2026-06-01T10:00:00+00:00', "
            "'SPY260618C00500000', 'combo-1', 'spread')"
        )
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, order_id, position_type) "
            "VALUES ('2026-06-01T10:00:00+00:00', "
            "'SPY260618C00510000', 'combo-1', 'spread')"
        )
        # Two single_leg duplicates sharing a DIFFERENT order_id.
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, order_id, position_type) "
            "VALUES ('2026-06-01T11:00:00+00:00', 'AAPL', "
            "'ord-dup', 'single_leg')"
        )
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, order_id, position_type) "
            "VALUES ('2026-06-01T12:00:00+00:00', 'AAPL', "
            "'ord-dup', 'single_leg')"
        )
        conn.commit()
        conn.close()

        out = tmp_path / "decisions.json"
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--review", str(out)],
        )
        assert code == 0
        decisions = json.loads(out.read_text())
        # Only the single_leg cluster appears.
        assert len(decisions["trades_order_id_clusters"]) == 1
        cluster = decisions["trades_order_id_clusters"][0]
        assert cluster["order_id"] == "ord-dup"
        # And the spread leg ids (1, 2) are NOWHERE in the cluster.
        # The flagged single_leg ids are 3 and 4.
        all_ids = (
            [cluster["keep_trade_id"]]
            + cluster["delete_trade_ids"]
            + [r["id"] for r in cluster["rows"]]
        )
        assert 1 not in all_ids and 2 not in all_ids
        assert set(all_ids) == {3, 4}


class TestApplyHardening:
    """P1.7 from round 2: --apply must defend against stale decisions
    files, missing FK enforcement, phantom deletes, and operator
    incomplete coverage."""

    def test_apply_aborts_on_stale_decisions_after_status_change(
        self, tmp_path,
    ):
        """Between --review and --apply, an active row's status flips
        (operator's monitoring tool touched it). The snapshot
        fingerprint check must abort and roll back."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])

        # Operator's monitor flips pos-A's status before apply runs.
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET status = 'partially_filled' "
            "WHERE position_uid = 'pos-A'"
        )
        conn.commit()
        conn.close()

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2  # aborted
        # Roll back proves it: pos-B still exists.
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        assert ("pos-B",) in rows

    def test_apply_aborts_on_phantom_delete(self, tmp_path):
        """If a row in the delete list no longer exists (already
        removed manually), the DELETE rowcount is 0 — must abort,
        not silently miscount."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])

        # Modify decisions file to point at a nonexistent id.
        decisions = json.loads(out.read_text())
        decisions["trades_order_id_clusters"][0]["delete_trade_ids"] = [9999]
        # Adjust fingerprint to NOT trip the snapshot check, so we
        # isolate the rowcount-check failure path.
        from scripts.migrate_dedupe_trades import (
            _cluster_fingerprint, _refetch_trades_cluster,
        )
        conn = sqlite3.connect(db)
        decisions["trades_order_id_clusters"][0]["review_fingerprint"] = (
            _cluster_fingerprint(_refetch_trades_cluster(
                conn,
                decisions["trades_order_id_clusters"][0],
            ))
        )
        conn.close()
        out.write_text(json.dumps(decisions))

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2

    def test_apply_aborts_on_incomplete_decisions_file(self, tmp_path):
        """If the operator removes a cluster from the decisions file,
        the post-apply rescan still finds duplicates → abort, roll
        back. The bot must never start with leftover duplicates."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])

        # Operator removes the trades cluster, keeping only owner_key.
        decisions = json.loads(out.read_text())
        decisions["trades_order_id_clusters"] = []
        out.write_text(json.dumps(decisions))

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        # Roll back proves it: trades cluster intact.
        cnt = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM trades WHERE order_id = 'ord-dup'"
        ).fetchone()[0]
        assert cnt == 2

    def test_apply_enables_foreign_keys(self, tmp_path):
        """PRAGMA foreign_keys must be ON for the apply transaction
        so dependent rows (position_lifecycle_orders FK-references
        position_lifecycle.position_uid) cause an explicit FK error
        rather than being silently orphaned.

        Plant a position_lifecycle_orders row referencing pos-B, then
        try to --apply (which proposes deleting pos-B). With FKs on,
        the DELETE raises FOREIGN KEY constraint failed → apply
        aborts and rolls back."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        # Add the FK-enforcing child table and a row referencing pos-B
        # (whose DELETE the apply will try).
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE position_lifecycle_orders ("
            "id INTEGER PRIMARY KEY, position_uid TEXT NOT NULL, "
            "FOREIGN KEY (position_uid) REFERENCES "
            "position_lifecycle(position_uid))"
        )
        conn.execute(
            "INSERT INTO position_lifecycle_orders "
            "(id, position_uid) VALUES (1, 'pos-B')"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        # With FKs ON, the DELETE FROM position_lifecycle WHERE
        # position_uid='pos-B' raises and apply rolls back.
        assert code == 2
        # pos-B is still present (rollback).
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle "
            "WHERE position_uid = 'pos-B'"
        ).fetchall()
        assert rows == [("pos-B",)]


class TestBackfillRespectsExplicitPositionType:
    """Finding 5 (round 2): _BACKFILL_SQL must NOT promote rows that
    already carry an explicit position_type. Predicate is now
    `position_id IS NULL AND position_type IS NULL`."""

    def test_backfill_skips_explicit_spread_row_with_null_position_id(
        self, tmp_path,
    ):
        """A pre-PR-60 spread row may have position_type='spread' but
        position_id=NULL. The previous BACKFILL predicate would have
        clobbered position_type to 'single_leg' and corrupted the
        row. The tightened predicate leaves it alone."""
        from reporting.logger import TradeLogger
        db_path = str(tmp_path / "trades.db")
        # Seed via raw sqlite3 to plant the legacy shape, then let
        # TradeLogger._ensure_db run the migration.
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT, symbol TEXT, side TEXT, qty REAL, "
            "avg_fill_price REAL, order_id TEXT, strategy TEXT, "
            "reason TEXT, stop_price REAL, "
            "entry_reference_price REAL, "
            "modeled_slippage_bps REAL, realized_slippage_bps REAL, "
            "order_type TEXT, status TEXT, requested_qty REAL, "
            "filled_qty REAL, position_id TEXT, "
            "position_type TEXT)"
        )
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, order_id, position_id, "
            "position_type) "
            "VALUES ('2026-06-01T10:00:00+00:00', "
            "'SPY260618C00500000', 'combo-X', NULL, 'spread')"
        )
        conn.commit()
        conn.close()

        # Let _ensure_db migrate (which runs preflight + BACKFILL).
        # The spread row's position_type='spread' must survive.
        tl = TradeLogger(path=db_path)
        conn = tl._ensure_db()
        row = conn.execute(
            "SELECT position_type, position_id FROM trades "
            "WHERE order_id = 'combo-X'"
        ).fetchone()
        assert row[0] == "spread"
        assert row[1] is None


# ── PR #60 round 3 fixes ────────────────────────────────────────────────────


class TestPartitionValidation:
    """Round 3 P0 finding: --apply must reject decisions files where
    keep + delete is not an exact partition of the reviewed snapshot
    ids. Without this, an operator could add an unrelated valid
    position_uid / trade id to delete_*; rowcount + rescan checks
    would both pass and the unrelated row would be silently
    destroyed."""

    def test_owner_apply_rejects_injected_unrelated_position_uid(
        self, tmp_path,
    ):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        # Plant an unrelated active position the operator might
        # maliciously / accidentally inject.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO position_lifecycle "
            "(schema_version, position_uid, owner_key, status, "
            "strategy, opened_at) VALUES "
            "(1, 'pos-UNRELATED', 'AAPL', 'open', 'sma', "
            "'2026-05-15T10:00:00+00:00')"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        # Operator injects the unrelated id into delete_position_uids.
        decisions = json.loads(out.read_text())
        decisions["owner_key_clusters"][0]["delete_position_uids"].append(
            "pos-UNRELATED"
        )
        out.write_text(json.dumps(decisions))

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        # pos-UNRELATED still exists (full rollback).
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle "
            "WHERE position_uid = 'pos-UNRELATED'"
        ).fetchall()
        assert rows == [("pos-UNRELATED",)]

    def test_trades_apply_rejects_injected_unrelated_trade_id(
        self, tmp_path,
    ):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        # Plant an unrelated trade row.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, side, qty, order_id, position_type) "
            "VALUES ('2026-06-02T10:00:00+00:00', 'AAPL', 'buy', 5, "
            "'ord-UNRELATED', 'single_leg')"
        )
        injected_id = conn.execute(
            "SELECT id FROM trades WHERE order_id = 'ord-UNRELATED'"
        ).fetchone()[0]
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        decisions = json.loads(out.read_text())
        decisions["trades_order_id_clusters"][0]["delete_trade_ids"].append(
            injected_id
        )
        out.write_text(json.dumps(decisions))

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        rows = sqlite3.connect(db).execute(
            "SELECT order_id FROM trades WHERE id = ?", (injected_id,),
        ).fetchall()
        assert rows == [("ord-UNRELATED",)]

    def test_owner_apply_rejects_keep_id_outside_snapshot(self, tmp_path):
        """Setting keep_position_uid to a value not in the snapshot
        also breaks the partition — reject."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        decisions = json.loads(out.read_text())
        decisions["owner_key_clusters"][0]["keep_position_uid"] = "pos-FAKE"
        out.write_text(json.dumps(decisions))
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2


class TestAccountingConflictRejection:
    """Round 3 P1 finding (dedupe): clusters where the kept row and a
    delete candidate disagree on a populated accounting column would
    require a merge the script does not implement. --apply must
    reject them instead of silently dropping data."""

    def test_owner_cluster_with_realized_pnl_mismatch_rejected(
        self, tmp_path,
    ):
        db = tmp_path / "trades.db"
        # Build a dirty DB and assign different realized P&L to each
        # row in the cluster.
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET net_realized_pnl = 12.34 "
            "WHERE position_uid = 'pos-A'"
        )
        conn.execute(
            "UPDATE position_lifecycle SET net_realized_pnl = 56.78 "
            "WHERE position_uid = 'pos-B'"
        )
        conn.commit()
        conn.close()

        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2  # conflict rejected
        # Roll back proves it: both rows still present.
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        assert ("pos-A",) in rows
        assert ("pos-B",) in rows

    def test_trades_cluster_with_realized_pnl_mismatch_rejected(
        self, tmp_path,
    ):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        # Add the column if it's not there.
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN realized_pnl REAL")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            "UPDATE trades SET realized_pnl = 10.0 WHERE id = 1"
        )
        conn.execute(
            "UPDATE trades SET realized_pnl = 20.0 WHERE id = 2"
        )
        conn.commit()
        conn.close()

        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        rows = sqlite3.connect(db).execute(
            "SELECT id FROM trades ORDER BY id"
        ).fetchall()
        # Both rows still present (rollback).
        assert (1,) in rows
        assert (2,) in rows

    def test_clusters_with_matching_accounting_proceed(self, tmp_path):
        """Positive control: when accounting columns AGREE, apply
        proceeds normally."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        # Both rows have the same realized P&L.
        conn.execute(
            "UPDATE position_lifecycle SET net_realized_pnl = 12.34 "
            "WHERE owner_key = 'TSLA'"
        )
        conn.commit()
        conn.close()

        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 0


class TestBackfillPromotesExplicitSingleLeg:
    """Round 3 P2 finding: BACKFILL must also populate position_id on
    rows that are explicit single_leg but missing position_id."""

    def test_explicit_single_leg_with_null_position_id_gets_position_id(
        self, tmp_path,
    ):
        from reporting.logger import TradeLogger
        db_path = str(tmp_path / "trades.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE trades ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "timestamp TEXT, symbol TEXT, side TEXT, qty REAL, "
            "avg_fill_price REAL, order_id TEXT, strategy TEXT, "
            "reason TEXT, stop_price REAL, "
            "entry_reference_price REAL, "
            "modeled_slippage_bps REAL, realized_slippage_bps REAL, "
            "order_type TEXT, status TEXT, requested_qty REAL, "
            "filled_qty REAL, position_id TEXT, position_type TEXT)"
        )
        # Partially migrated row: explicit single_leg, NULL position_id.
        conn.execute(
            "INSERT INTO trades "
            "(timestamp, symbol, order_id, position_id, position_type) "
            "VALUES ('2026-06-01T10:00:00+00:00', "
            "'AAPL', 'ord-partial', NULL, 'single_leg')"
        )
        conn.commit()
        conn.close()

        # _ensure_db runs BACKFILL with the new predicate.
        tl = TradeLogger(path=db_path)
        conn = tl._ensure_db()
        row = conn.execute(
            "SELECT position_id, position_type FROM trades "
            "WHERE order_id = 'ord-partial'"
        ).fetchone()
        assert row[0] == "AAPL"  # populated via OWNER_KEY
        assert row[1] == "single_leg"  # preserved


# ── PR #60 round 4 fixes ────────────────────────────────────────────────────


class TestKeeperRequiredPartition:
    """Round 4 P0: --apply must require exactly one keeper in the
    snapshot and deletes == snapshot - {keeper}. Round 3's symmetric
    check passed when keeper=None and delete=all_snapshot_ids."""

    def test_owner_apply_rejects_null_keeper_with_all_in_delete(
        self, tmp_path,
    ):
        """The delete-all-no-keeper bug ChatGPT caught: operator
        sets keep_position_uid=None and puts every snapshot id in
        delete_position_uids. Old check passed; new check rejects."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        decisions = json.loads(out.read_text())
        cluster = decisions["owner_key_clusters"][0]
        snapshot_uids = [r["position_uid"] for r in cluster["rows"]]
        cluster["keep_position_uid"] = None
        cluster["delete_position_uids"] = snapshot_uids
        out.write_text(json.dumps(decisions))

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        # Both rows survive (rollback).
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        assert ("pos-A",) in rows
        assert ("pos-B",) in rows

    def test_trades_apply_rejects_null_keeper_with_all_in_delete(
        self, tmp_path,
    ):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        decisions = json.loads(out.read_text())
        cluster = decisions["trades_order_id_clusters"][0]
        snapshot_ids = [r["id"] for r in cluster["rows"]]
        cluster["keep_trade_id"] = None
        cluster["delete_trade_ids"] = snapshot_ids
        out.write_text(json.dumps(decisions))

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        rows = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM trades WHERE order_id = 'ord-dup'"
        ).fetchone()
        assert rows[0] == 2

    def test_owner_apply_rejects_omitted_delete_in_3_row_cluster(
        self, tmp_path,
    ):
        """3-row cluster: keeper plus only one delete instead of two.
        Partition is a subset of the snapshot — must reject."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        # Add a third active duplicate so the cluster has 3 rows.
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO position_lifecycle "
            "(schema_version, position_uid, owner_key, status, "
            "strategy, opened_at) "
            "VALUES (1, 'pos-C', 'TSLA', 'open', 'sma', "
            "'2026-05-03T10:00:00+00:00')"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        decisions = json.loads(out.read_text())
        cluster = decisions["owner_key_clusters"][0]
        # Default would be: keep pos-A, delete [pos-B, pos-C].
        # Operator omits pos-C.
        cluster["delete_position_uids"] = ["pos-B"]
        out.write_text(json.dumps(decisions))

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        # All three rows survive (rollback).
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle "
            "WHERE owner_key = 'TSLA' ORDER BY position_uid"
        ).fetchall()
        assert len(rows) == 3

    def test_owner_apply_rejects_keeper_in_delete_list(self, tmp_path):
        """Keeper id also appears in delete list → reject."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        decisions = json.loads(out.read_text())
        cluster = decisions["owner_key_clusters"][0]
        cluster["delete_position_uids"].append(cluster["keep_position_uid"])
        out.write_text(json.dumps(decisions))
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2


class TestAsymmetricConflictRejection:
    """Round 4 P1 (asymmetric NULL): _delete_has_data_keeper_lacks
    must reject when the delete row has a non-null value the keeper
    lacks — silently dropping would lose data."""

    def test_keeper_null_delete_has_realized_pnl_rejected(
        self, tmp_path,
    ):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        # Keeper (earliest, pos-A) has NULL net_realized_pnl;
        # delete candidate (pos-B) has populated value. Round 3
        # would have silently dropped pos-B + the value with it.
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET net_realized_pnl = NULL "
            "WHERE position_uid = 'pos-A'"
        )
        conn.execute(
            "UPDATE position_lifecycle SET net_realized_pnl = 99.99 "
            "WHERE position_uid = 'pos-B'"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        # pos-B still exists with the populated value.
        row = sqlite3.connect(db).execute(
            "SELECT net_realized_pnl FROM position_lifecycle "
            "WHERE position_uid = 'pos-B'"
        ).fetchone()
        assert row[0] == 99.99

    def test_trades_keeper_null_delete_has_modeled_slippage_rejected(
        self, tmp_path,
    ):
        """Round 4 expanded column coverage: modeled_slippage_bps
        is now in the conflict check. A populated value on the
        delete row that the keeper lacks must reject."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE trades SET modeled_slippage_bps = NULL WHERE id = 1"
        )
        conn.execute(
            "UPDATE trades SET modeled_slippage_bps = 22.2 WHERE id = 2"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2

    def test_keeper_has_value_delete_null_proceeds(self, tmp_path):
        """Positive control: keeper has populated value, delete has
        NULL — no data lost; apply proceeds."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET net_realized_pnl = 12.34 "
            "WHERE position_uid = 'pos-A'"
        )
        conn.execute(
            "UPDATE position_lifecycle SET net_realized_pnl = NULL "
            "WHERE position_uid = 'pos-B'"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 0


class TestDetectOutputShowsAccountingEvidence:
    """Round 4 P1 sub-point: --detect output must surface the
    accounting columns _scan now fetches, so the operator can see
    differences before deciding the keeper."""

    def test_detect_prints_realized_pnl_when_present(
        self, tmp_path, capsys,
    ):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET net_realized_pnl = 77.7 "
            "WHERE position_uid = 'pos-A'"
        )
        conn.commit()
        conn.close()
        migrate_dedupe_trades.main(["--db", str(db), "--detect"])
        captured = capsys.readouterr()
        assert "net_realized_pnl" in captured.out
        assert "77.7" in captured.out


# ── PR #60 round 5 fixes ────────────────────────────────────────────────────


class TestSchemaDrivenLifecycleConflict:
    """Round 5 P0: the lifecycle conflict check now scans every
    column except the discardable noise allowlist. Reproduces the
    exact destructive sequence the reviewer caught: pending keeper
    with NULL fill state vs delete row carrying real fill data."""

    def test_pending_keeper_with_null_state_vs_filled_delete_rejected(
        self, tmp_path,
    ):
        """The reviewer's reproduction: earliest keeper is pending
        with current_qty=0 and no order identity; the delete row is
        open with current_qty=10, $101.25 avg basis, real broker
        order id. Apply MUST reject — earlier rounds would have
        silently retained the stale pending row."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        # Keeper (pos-A, earlier opened_at) is pending/empty.
        conn.execute(
            "UPDATE position_lifecycle SET "
            "status='pending', current_qty=0, "
            "avg_entry_price=NULL, entry_order_id=NULL, "
            "entry_client_order_id=NULL "
            "WHERE position_uid='pos-A'"
        )
        # Delete (pos-B) is the real position with fill state.
        conn.execute(
            "UPDATE position_lifecycle SET "
            "status='open', current_qty=10, entry_qty=10, "
            "avg_entry_price=101.25, "
            "entry_order_id='alpaca-real-1', "
            "entry_client_order_id='cli-real-1' "
            "WHERE position_uid='pos-B'"
        )
        conn.commit()
        conn.close()

        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2  # rejected
        # Both rows survive (rollback).
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid, status, current_qty FROM "
            "position_lifecycle ORDER BY position_uid"
        ).fetchall()
        statuses = {r[0]: (r[1], r[2]) for r in rows}
        assert statuses["pos-A"] == ("pending", 0)
        assert statuses["pos-B"] == ("open", 10)

    def test_current_qty_only_on_delete_rejected(self, tmp_path):
        """Single-column smoke for the previously-uncovered field."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET current_qty=NULL "
            "WHERE position_uid='pos-A'"
        )
        conn.execute(
            "UPDATE position_lifecycle SET current_qty=5 "
            "WHERE position_uid='pos-B'"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2

    def test_entry_order_id_only_on_delete_rejected(self, tmp_path):
        """The 'broker/client order IDs' from the reviewer's list."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET entry_order_id=NULL "
            "WHERE position_uid='pos-A'"
        )
        conn.execute(
            "UPDATE position_lifecycle SET entry_order_id='alpaca-9' "
            "WHERE position_uid='pos-B'"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2

    def test_first_fill_at_only_on_delete_rejected(self, tmp_path):
        """fill timestamps from the reviewer's list."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET first_fill_at=NULL "
            "WHERE position_uid='pos-A'"
        )
        conn.execute(
            "UPDATE position_lifecycle SET "
            "first_fill_at='2026-05-02T11:00:00+00:00' "
            "WHERE position_uid='pos-B'"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2


class TestSchemaDrivenTradesConflict:
    """Round 5 P1: trades conflict now scans every non-discardable
    column. Picks up columns the round-4 hand-curated list was
    missing — qty, stop_price, entry_reference_price, execution_id."""

    def test_qty_only_on_delete_rejected(self, tmp_path):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE trades SET qty=NULL WHERE id=1")
        conn.execute("UPDATE trades SET qty=10 WHERE id=2")
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2

    def test_stop_price_only_on_delete_rejected(self, tmp_path):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        conn.execute("UPDATE trades SET stop_price=NULL WHERE id=1")
        conn.execute("UPDATE trades SET stop_price=95.0 WHERE id=2")
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2

    def test_execution_id_only_on_delete_rejected(self, tmp_path):
        """execution_id wasn't in round 4's list. Round 5 catches it."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        conn = sqlite3.connect(db)
        # Add the column if it's missing.
        try:
            conn.execute("ALTER TABLE trades ADD COLUMN execution_id TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute("UPDATE trades SET execution_id=NULL WHERE id=1")
        conn.execute("UPDATE trades SET execution_id='exec-77' WHERE id=2")
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2


# ── PR #60 round 6 fixes ────────────────────────────────────────────────────


class TestFingerprintCoversTimestamps:
    """Round 6 P1 finding: a mutation to opened_at / created_at /
    trades.timestamp between --review and --apply was slipping
    through the staleness check because round 5 reused the conflict
    discardable set as the fingerprint exclusion. The two sets now
    diverge — timestamps are conflict-discardable but
    fingerprint-tracked."""

    def test_apply_aborts_on_keeper_opened_at_mutation(self, tmp_path):
        """The reviewer's exact reproduction: change keeper's
        opened_at after --review; --apply must abort on the
        snapshot fingerprint mismatch."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])

        # Operator's monitoring tool updates the keeper's opened_at
        # between review and apply (or a parallel writer touched it).
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET "
            "opened_at='2024-01-01T00:00:00+00:00' "
            "WHERE position_uid='pos-A'"
        )
        conn.commit()
        conn.close()

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2  # fingerprint mismatch → abort
        # Both rows survive (rollback).
        rows = sqlite3.connect(db).execute(
            "SELECT position_uid FROM position_lifecycle "
            "ORDER BY position_uid"
        ).fetchall()
        assert ("pos-A",) in rows
        assert ("pos-B",) in rows

    def test_apply_aborts_on_trades_timestamp_mutation(self, tmp_path):
        """The same property on the trades side: a row's timestamp
        column changing between --review and --apply invalidates
        the operator's decision context."""
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])

        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE trades SET "
            "timestamp='2024-01-01T00:00:00+00:00' WHERE id=1"
        )
        conn.commit()
        conn.close()

        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        assert code == 2
        # Trades cluster intact (rollback).
        cnt = sqlite3.connect(db).execute(
            "SELECT COUNT(*) FROM trades WHERE order_id='ord-dup'"
        ).fetchone()[0]
        assert cnt == 2


class TestSchemaVersionConflict:
    """Round 6 P2 finding: schema_version is no longer in the
    conflict discardable set, so a mixed-version cluster surfaces
    as a conflict instead of being silently glossed over."""

    def test_owner_cluster_with_different_schema_versions_rejected(
        self, tmp_path,
    ):
        db = tmp_path / "trades.db"
        _make_dirty_db(db)
        # Simulate a partial migration: keeper at version 1, delete
        # at version 2.
        conn = sqlite3.connect(db)
        conn.execute(
            "UPDATE position_lifecycle SET schema_version=1 "
            "WHERE position_uid='pos-A'"
        )
        conn.execute(
            "UPDATE position_lifecycle SET schema_version=2 "
            "WHERE position_uid='pos-B'"
        )
        conn.commit()
        conn.close()
        out = tmp_path / "decisions.json"
        migrate_dedupe_trades.main(["--db", str(db), "--review", str(out)])
        code = migrate_dedupe_trades.main(
            ["--db", str(db), "--apply", str(out)],
        )
        # Round 5 would have passed (schema_version was discardable).
        # Round 6 rejects.
        assert code == 2
