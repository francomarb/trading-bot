"""Read-only candidate comparison reporting for PLAN 11.61."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from engine.candidate_observation import CandidateObservationStore, CandidateStart
from reporting.candidate_comparison import build_candidate_comparison_report


NOW = datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc)


def _candidate(symbol: str, ordinal: int) -> CandidateStart:
    return CandidateStart(
        cycle_uid="cycle-sma",
        signal_at=NOW,
        strategy="sma_crossover",
        strategy_version="1.0",
        strategy_config_hash="cfg",
        bot_git_commit="a" * 40,
        symbol=symbol,
        signal_symbol=symbol,
        timeframe="1Day",
        data_feed="iex",
        regime="TRENDING",
        slot_ordinal=0,
        watchlist_ordinal=ordinal,
        evaluation_ordinal=ordinal,
        feature_schema_version=1,
        reference_price=100.0,
        atr=2.0,
        strategy_features={
            "fast_window": 20,
            "slow_window": 50,
            "slow_sma": 95.0,
            "crossover_gap_pct": 0.001,
        },
        common_context={"sector": "Technology"},
    )


def _connection() -> tuple[sqlite3.Connection, CandidateObservationStore]:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    store = CandidateObservationStore(conn)
    conn.executescript(
        """
        CREATE TABLE position_lifecycle (
            position_uid TEXT PRIMARY KEY,
            status TEXT,
            avg_entry_price REAL,
            first_fill_at TEXT,
            closed_at TEXT,
            net_realized_pnl REAL
        );
        CREATE TABLE position_lifecycle_orders (
            position_uid TEXT,
            origin_kind TEXT
        );
        CREATE TABLE trades (
            position_uid TEXT,
            initial_risk_dollars REAL,
            avg_fill_price REAL,
            filled_qty REAL,
            qty REAL,
            realized_pnl REAL,
            status TEXT,
            timestamp TEXT,
            reason TEXT
        );
        """
    )
    return conn, store


class TestCandidateComparisonReport:
    def test_combines_actual_lifecycle_with_refused_strategy_replay(self) -> None:
        conn, store = _connection()
        selected = store.start(_candidate("AAA", 0), observed_at=NOW)
        refused = store.start(_candidate("BBB", 1), observed_at=NOW)
        store.update(
            selected,
            disposition="accepted",
            selected=True,
            position_uid="pos_selected",
        )
        store.update(refused, disposition="sleeve_full")
        store.finalize_cycle("cycle-sma")
        store.record_shadow_outcome(
            refused,
            status="resolved",
            outcome_basis="sma_market_stop_daily_v1",
            entry_price=101.0,
            exit_price=105.0,
            return_pct=0.0396,
            r_multiple=0.5,
            max_favorable_pct=0.06,
            max_adverse_pct=-0.01,
            metadata_json={"exit_reason": "strategy_signal"},
        )
        conn.execute(
            "INSERT INTO position_lifecycle VALUES (?, ?, ?, ?, ?, ?)",
            (
                "pos_selected",
                "closed",
                100.0,
                NOW.isoformat(),
                "2026-09-15T14:30:00+00:00",
                -50.0,
            ),
        )
        conn.executemany(
            "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "pos_selected", 100.0, 100.0, 10.0, 10.0, None,
                    "filled", NOW.isoformat(), "entry",
                ),
                (
                    "pos_selected", 100.0, 95.0, 10.0, 10.0, -50.0,
                    "filled", "2026-09-15T14:30:00+00:00", "protective stop",
                ),
            ],
        )
        conn.commit()

        report = build_candidate_comparison_report(conn)

        assert "sma_crossover | TRENDING | 1 | 1/1 | 1/1 | 0" in report
        assert "AAA | selected" in report
        assert "-0.50R" in report
        assert "actual_lifecycle" in report
        assert "protective stop" in report
        assert "7.0d" in report
        assert "BBB | refused" in report
        assert "+0.50R" in report
        assert "sma_market_stop_daily_v1" in report
        assert "gap=+0.001" in report

    def test_open_selected_and_pending_refused_are_not_counted_resolved(self) -> None:
        conn, store = _connection()
        selected = store.start(_candidate("AAA", 0), observed_at=NOW)
        refused = store.start(_candidate("BBB", 1), observed_at=NOW)
        store.update(
            selected,
            disposition="accepted",
            selected=True,
            position_uid="pos_open",
        )
        store.update(refused, disposition="sleeve_full")
        store.finalize_cycle("cycle-sma")
        conn.execute(
            "INSERT INTO position_lifecycle VALUES (?, ?, ?, ?, ?, ?)",
            ("pos_open", "open", 100.0, NOW.isoformat(), None, 0.0),
        )
        conn.commit()

        report = build_candidate_comparison_report(conn)

        assert "sma_crossover | TRENDING | 1 | 0/1 | 0/1 | 0" in report
        assert "AAA | selected | accepted | open" in report
        assert "BBB | refused | sleeve_full | pending" in report

    def test_noncapacity_rejection_is_visible_but_not_replayed(self) -> None:
        conn, store = _connection()
        selected = store.start(_candidate("AAA", 0), observed_at=NOW)
        capacity = store.start(_candidate("BBB", 1), observed_at=NOW)
        invalid = store.start(_candidate("CCC", 2), observed_at=NOW)
        store.update(
            selected,
            disposition="accepted",
            selected=True,
            position_uid="pos_open",
        )
        store.update(capacity, disposition="sleeve_full")
        store.update(invalid, disposition="invalid_stop")
        store.finalize_cycle("cycle-sma")
        conn.execute(
            "INSERT INTO position_lifecycle VALUES (?, ?, ?, ?, ?, ?)",
            ("pos_open", "open", 100.0, NOW.isoformat(), None, 0.0),
        )
        conn.commit()

        report = build_candidate_comparison_report(conn)

        assert "CCC | refused | invalid_stop | not_eligible" in report
        assert "production_rejection" in report

    def test_legacy_subshare_sleeve_rejection_remains_in_refused_pool(
        self, tmp_path
    ) -> None:
        conn, store = _connection()
        selected = store.start(_candidate("AAA", 0), observed_at=NOW)
        capacity = store.start(_candidate("BBB", 1), observed_at=NOW)
        subshare = store.start(_candidate("CCC", 2), observed_at=NOW)
        store.update(selected, selected=True, disposition="filled")
        store.update(capacity, disposition="sleeve_full")
        store.update(subshare, disposition="position_too_small")
        conn.execute(
            "UPDATE entry_candidate_decisions SET common_context_json = ? "
            "WHERE candidate_uid = ?",
            (
                json.dumps({"sleeve": {"max_position_notional": 99.0}}),
                subshare,
            ),
        )
        conn.commit()
        store.finalize_cycle("cycle-sma")

        report = build_candidate_comparison_report(conn, repo_root=tmp_path)

        assert "sma_crossover | TRENDING | 1 | 0/1 | 0/2 | 0" in report
        assert "CCC | refused | position_too_small | pending" in report
