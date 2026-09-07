import sqlite3
import json
import pytest

from engine.lifecycle import (
    PositionLifecycleStore,
    _CREATE_POSITION_LIFECYCLE_LEGS_SQL,
    _CREATE_POSITION_LIFECYCLE_SQL,
)
from reporting.graduation import build_graduation_report, render_markdown
from reporting.graduation_marks import _CREATE_STRATEGY_DAILY_MARKS_SQL
from reporting.logger import _CREATE_TABLE_SQL


def _database(path):
    conn = sqlite3.connect(path)
    conn.execute(_CREATE_TABLE_SQL)
    conn.execute(_CREATE_POSITION_LIFECYCLE_SQL)
    conn.execute(_CREATE_POSITION_LIFECYCLE_LEGS_SQL)
    conn.execute(_CREATE_STRATEGY_DAILY_MARKS_SQL)
    conn.commit()
    return conn


class TestGraduationReport:
    def test_missing_database_is_not_created(self, tmp_path):
        db = tmp_path / "typo.db"

        with pytest.raises(FileNotFoundError, match="does not exist"):
            build_graduation_report(db)

        assert not db.exists()

    def test_old_trade_schema_gets_clear_error(self, tmp_path):
        db = tmp_path / "old.db"
        conn = _database(db)
        conn.execute("DROP TABLE trades")
        conn.execute(
            "CREATE TABLE trades (position_uid TEXT, realized_pnl REAL, "
            "strategy TEXT)"
        )
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="missing trades columns"):
            build_graduation_report(db)

    def test_absent_forward_mark_table_is_empty_evidence(self, tmp_path):
        db = tmp_path / "pre_deployment.db"
        conn = _database(db)
        store = PositionLifecycleStore(conn)
        store.create_pending(
            position_uid="pos_cccccccccccccccccccccccccccccccc",
            symbol="AAPL",
            owner_key="AAPL",
            strategy="sma_crossover",
            strategy_version="1.0",
            strategy_config_hash="abc123",
            bot_git_commit="deadbeef",
            position_type="single_leg",
            entry_qty=1,
        )
        conn.execute("DROP TABLE strategy_daily_marks")
        conn.commit()
        conn.close()

        report = build_graduation_report(db)

        assert len(report["cohorts"]) == 1
        cohort = report["cohorts"][0]
        assert cohort["coverage"]["daily_marks"] == 0
        assert cohort["performance"]["forward_daily_total_max_drawdown"] is None
        assert cohort["evidence_status"] == "DATA INCOMPLETE"

    def test_malformed_forward_mark_table_still_gets_clear_error(self, tmp_path):
        db = tmp_path / "bad_migration.db"
        conn = _database(db)
        conn.execute("DROP TABLE strategy_daily_marks")
        conn.execute("CREATE TABLE strategy_daily_marks (mark_date TEXT)")
        conn.commit()
        conn.close()

        with pytest.raises(RuntimeError, match="daily-mark migration"):
            build_graduation_report(db)

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
        assert cohort["coverage"]["trusted_completed"] == 1
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
        conn.execute(
            "INSERT INTO trades "
            "(timestamp,symbol,side,qty,strategy,reason,status,position_type,"
            "position_uid,realized_pnl) "
            "VALUES ('2026-01-03','MSFT','sell',1,'sma_crossover','exit',"
            "'filled','single_leg',?,25)",
            ("pos_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",),
        )
        conn.commit()
        conn.close()

        report = build_graduation_report(db)

        assert report["cohorts"] == []
        assert report["unknown_epoch_history"][0]["realized_pnl"] == 25
        assert "excluded from cohorts" in render_markdown(report)

    def test_footer_discloses_trade_events_without_lifecycle(self, tmp_path):
        db = tmp_path / "trades.db"
        conn = _database(db)
        conn.execute(
            "INSERT INTO trades "
            "(timestamp,symbol,side,qty,strategy,reason,status,position_type,"
            "position_uid,realized_pnl) VALUES "
            "('2026-01-02','SPY','sell',1,'legacy_strategy','exit','filled',"
            "'single_leg',NULL,-10),"
            "('2026-01-03','SPY','sell',1,'legacy_strategy','exit','filled',"
            "'single_leg','pos_missing_parent',4)"
        )
        conn.commit()
        conn.close()

        report = build_graduation_report(db)
        excluded = report["diagnostics"]["excluded_trade_only_realized_events"]

        assert excluded == [
            {
                "strategy": "legacy_strategy",
                "exclusion_kind": "missing_lifecycle_parent",
                "events": 1,
                "realized_pnl": 4.0,
            },
            {
                "strategy": "legacy_strategy",
                "exclusion_kind": "no_position_uid",
                "events": 1,
                "realized_pnl": -10.0,
            },
        ]
        markdown = render_markdown(report)
        assert "## Excluded trade-only history" in markdown
        assert "2 P&L events, $-6.00 net" in markdown
        assert "1 without lifecycle ID" in markdown
        assert "1 with no lifecycle parent" in markdown

    def test_filters_outcomes_marks_and_trade_only_footer(self, tmp_path):
        db = tmp_path / "trades.db"
        conn = _database(db)
        store = PositionLifecycleStore(conn)
        cases = (
            (
                "pos_11111111111111111111111111111111",
                "sma_crossover",
                "2026-01-15",
                10.0,
                "abc123",
            ),
            (
                "pos_22222222222222222222222222222222",
                "sma_crossover",
                "2026-02-15",
                20.0,
                "abc123",
            ),
            (
                "pos_33333333333333333333333333333333",
                "rsi_reversion",
                "2026-02-15",
                30.0,
                "abc123",
            ),
            (
                "pos_44444444444444444444444444444444",
                "sma_crossover",
                "2026-03-15",
                40.0,
                "later456",
            ),
        )
        for uid, strategy, day, pnl, config_hash in cases:
            store.create_pending(
                position_uid=uid,
                symbol="AAPL",
                owner_key=uid,
                strategy=strategy,
                strategy_version="1.0",
                strategy_config_hash=config_hash,
                bot_git_commit="deadbeef",
                position_type="single_leg",
                entry_qty=1,
            )
            store.mark_open(position_uid=uid, avg_entry_price=100, current_qty=1)
            conn.execute(
                "INSERT INTO trades "
                "(timestamp,symbol,side,qty,strategy,reason,status,position_type,"
                "position_uid,realized_pnl) VALUES "
                "(?,'AAPL','sell',1,?,'exit','filled','single_leg',?,?)",
                (f"{day}T20:00:00+00:00", strategy, uid, pnl),
            )
            conn.commit()
            store.mark_closed(position_uid=uid, net_realized_pnl=pnl)
            conn.execute(
                "UPDATE position_lifecycle SET created_at=?, closed_at=? "
                "WHERE position_uid=?",
                (
                    f"{day}T15:00:00+00:00",
                    f"{day}T20:00:00+00:00",
                    uid,
                ),
            )
        conn.executemany(
            "INSERT INTO strategy_daily_marks "
            "(mark_date,observed_at,strategy,strategy_version,"
            "strategy_config_hash,realized_pnl,unrealized_pnl,total_pnl,"
            "open_lifecycles,valued_lifecycles,missing_lifecycles,"
            "unresolved_economics,source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "2026-01-15", "2026-01-15T21:00:00+00:00",
                    "sma_crossover", "1.0", "abc123", 10, 0, 10,
                    0, 0, 0, 0, "broker_snapshot",
                ),
                (
                    "2026-02-15", "2026-02-15T21:00:00+00:00",
                    "sma_crossover", "1.0", "abc123", 30, 0, 30,
                    0, 0, 0, 0, "broker_snapshot",
                ),
                (
                    "2026-02-20", "2026-02-20T21:00:00+00:00",
                    "sma_crossover", "1.0", "later456", 0, 5, 5,
                    1, 1, 0, 0, "broker_snapshot",
                ),
                (
                    "2026-02-15", "2026-02-15T21:00:00+00:00",
                    "rsi_reversion", "1.0", "abc123", 30, 0, 30,
                    0, 0, 0, 0, "broker_snapshot",
                ),
            ],
        )
        conn.executemany(
            "INSERT INTO trades "
            "(timestamp,symbol,side,qty,strategy,reason,status,position_type,"
            "position_uid,realized_pnl) VALUES "
            "(?,'OLD','sell',1,?,'legacy','filled','single_leg',NULL,?)",
            [
                ("2026-01-20T20:00:00+00:00", "sma_crossover", -1),
                ("2026-02-20T20:00:00+00:00", "sma_crossover", -2),
                ("2026-02-20T20:00:00+00:00", "rsi_reversion", -3),
            ],
        )
        conn.commit()
        conn.close()

        report = build_graduation_report(
            db,
            start_date="2026-02-01",
            end_date="2026-02-28",
            strategies=["sma_crossover"],
        )

        assert report["filters"] == {
            "start_date": "2026-02-01",
            "end_date": "2026-02-28",
            "strategies": ["sma_crossover"],
            "lifetime": False,
            "timezone": "UTC",
        }
        assert len(report["cohorts"]) == 2
        cohort = next(
            item for item in report["cohorts"]
            if item["strategy_config_hash"] == "abc123"
        )
        assert cohort["coverage"]["trusted_completed"] == 1
        assert cohort["performance"]["gross_realized_pnl"] == 20
        assert [
            mark["mark_date"] for mark in cohort["mark_to_market"]["daily"]
        ] == ["2026-02-15"]
        mark_only = next(
            item for item in report["cohorts"]
            if item["strategy_config_hash"] == "later456"
        )
        assert mark_only["coverage"]["trusted_completed"] == 0
        assert [
            mark["mark_date"] for mark in mark_only["mark_to_market"]["daily"]
        ] == ["2026-02-20"]
        assert report["diagnostics"]["excluded_trade_only_realized_events"] == [
            {
                "strategy": "sma_crossover",
                "exclusion_kind": "no_position_uid",
                "events": 1,
                "realized_pnl": -2.0,
            }
        ]
        markdown = render_markdown(report)
        assert "UTC 2026-02-01 through 2026-02-28" in markdown
        assert "`sma_crossover`" in markdown

    def test_date_filter_validation_is_strict(self, tmp_path):
        db = tmp_path / "trades.db"
        _database(db).close()

        with pytest.raises(ValueError, match="valid YYYY-MM-DD"):
            build_graduation_report(db, start_date="February 1, 2026")
        with pytest.raises(ValueError, match="on or before"):
            build_graduation_report(
                db, start_date="2026-03-01", end_date="2026-02-01"
            )

    def test_terminal_without_economics_is_excluded_not_zero(self, tmp_path):
        db = tmp_path / "trades.db"
        conn = _database(db)
        store = PositionLifecycleStore(conn)
        store.create_pending(
            position_uid="pos_dddddddddddddddddddddddddddddddd",
            symbol="AAPL",
            owner_key="AAPL",
            strategy="sma_crossover",
            strategy_version="1.0",
            strategy_config_hash="abc123",
            bot_git_commit="deadbeef",
            position_type="single_leg",
            entry_qty=1,
        )
        store.mark_open(
            position_uid="pos_dddddddddddddddddddddddddddddddd",
            avg_entry_price=100,
            current_qty=1,
        )
        store.mark_closed(
            position_uid="pos_dddddddddddddddddddddddddddddddd",
            external=True,
        )
        conn.close()

        cohort = build_graduation_report(db)["cohorts"][0]

        assert cohort["coverage"]["terminal_lifecycles"] == 1
        assert cohort["coverage"]["trusted_completed"] == 0
        assert cohort["coverage"]["unresolved_economics"] == 1
        assert cohort["performance"]["gross_realized_pnl"] is None
        assert cohort["performance"]["average_outcome"] is None
        assert cohort["evidence_status"] == "DATA INCOMPLETE"

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
