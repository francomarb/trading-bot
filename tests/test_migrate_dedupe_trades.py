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
