import sqlite3
import json

from engine.lifecycle import (
    PositionLifecycleStore,
    _CREATE_POSITION_LIFECYCLE_LEGS_SQL,
    _CREATE_POSITION_LIFECYCLE_SQL,
)
from reporting.graduation import build_graduation_report, render_markdown
from reporting.logger import _CREATE_TABLE_SQL


def _database(path):
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_POSITION_LIFECYCLE_SQL)
    conn.execute(_CREATE_POSITION_LIFECYCLE_LEGS_SQL)
    conn.commit()
    return conn


class TestGraduationReport:
    def test_partial_exit_rows_are_one_lifecycle_outcome(self, tmp_path):
        db = tmp_path / "trades.db"
        conn = _database(db)
        store = PositionLifecycleStore(conn)
        store.create_pending(
            position_uid="pos_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            symbol="AAPL",
            owner_key="AAPL",
            strategy="sma_crossover",
            strategy_version="1.0",
            strategy_config_hash="abc123",
            bot_git_commit="deadbeef",
            entry_regime="TRENDING",
            position_type="single_leg",
            entry_qty=3,
        )
        store.mark_open(
            position_uid="pos_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            avg_entry_price=100,
            current_qty=3,
        )
        conn.execute(
            "INSERT INTO trades "
            "(timestamp,symbol,side,qty,strategy,reason,status,position_type,"
            "position_uid,realized_pnl,initial_risk_dollars) "
            "VALUES ('2026-01-02','AAPL','sell',1,'sma_crossover','reduce',"
            "'filled','single_leg',?,40,100)",
            ("pos_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
        )
        conn.execute(
            "INSERT INTO trades "
            "(timestamp,symbol,side,qty,strategy,reason,status,position_type,"
            "position_uid,realized_pnl,initial_risk_dollars) "
            "VALUES ('2026-01-03','AAPL','sell',2,'sma_crossover','exit',"
            "'filled','single_leg',?,60,100)",
            ("pos_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
        )
        conn.commit()
        store.mark_closed(
            position_uid="pos_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            net_realized_pnl=100,
        )
        conn.close()

        report = build_graduation_report(db)

        assert len(report["cohorts"]) == 1
        cohort = report["cohorts"][0]
        assert cohort["coverage"]["completed"] == 1
        assert cohort["performance"]["gross_realized_pnl"] == 100
        assert cohort["performance"]["average_r"] == 1

    def test_unknown_epoch_is_context_not_cohort(self, tmp_path):
        db = tmp_path / "trades.db"
        conn = _database(db)
        store = PositionLifecycleStore(conn)
        store.create_pending(
            position_uid="pos_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            symbol="MSFT",
            owner_key="MSFT",
            strategy="sma_crossover",
            position_type="single_leg",
            entry_qty=1,
        )
        store.mark_open(
            position_uid="pos_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            avg_entry_price=100,
            current_qty=1,
        )
        store.mark_closed(
            position_uid="pos_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            net_realized_pnl=25,
        )
        conn.close()

        report = build_graduation_report(db)

        assert report["cohorts"] == []
        assert report["unknown_epoch_history"][0]["realized_pnl"] == 25
        assert "excluded from cohorts" in render_markdown(report)

    def test_explicit_reviewed_epoch_can_classify_legacy_lifecycle(self, tmp_path):
        db = tmp_path / "trades.db"
        conn = _database(db)
        store = PositionLifecycleStore(conn)
        store.create_pending(
            position_uid="pos_cccccccccccccccccccccccccccccccc",
            symbol="MSFT",
            owner_key="MSFT",
            strategy="sma_crossover",
            position_type="single_leg",
            entry_qty=1,
        )
        store.mark_open(
            position_uid="pos_cccccccccccccccccccccccccccccccc",
            avg_entry_price=100,
            current_qty=1,
        )
        store.mark_closed(
            position_uid="pos_cccccccccccccccccccccccccccccccc",
            net_realized_pnl=0,
        )
        conn.close()
        epochs = tmp_path / "epochs.json"
        epochs.write_text(json.dumps({
            "schema_version": 1,
            "epochs": [{
                "strategy": "sma_crossover",
                "start_at": "2000-01-01T00:00:00+00:00",
                "end_at": "2100-01-01T00:00:00+00:00",
                "strategy_version": "0.9",
                "strategy_config_hash": "reviewed123",
                "reviewed_by": "operator",
                "reviewed_at": "2026-09-06T00:00:00+00:00",
                "evidence": "PR review",
            }],
        }))

        report = build_graduation_report(db, reviewed_epochs_path=epochs)

        assert report["unknown_epoch_history"] == []
        assert report["cohorts"][0]["strategy_version"] == "0.9"
        assert report["cohorts"][0]["identity_sources"] == ["reviewed_inference"]
