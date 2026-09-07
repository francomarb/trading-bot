from datetime import datetime, timezone

import pytest

from engine.lifecycle import PositionLifecycleLeg, PositionLifecycleStore
from reporting.costs import DEFAULT_COST_MODEL
from reporting.graduation import build_graduation_report
from reporting.graduation_marks import StrategyDailyMarkStore
from reporting.logger import TradeLogger
from risk.manager import Position


UID = "pos_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _stores(path):
    logger = TradeLogger(path=str(path))
    conn = logger._ensure_db()
    return conn, PositionLifecycleStore(conn), StrategyDailyMarkStore(conn)


def _create_open(store, *, position_type="single_leg", symbol="AAPL", legs=()):
    store.create_pending(
        position_uid=UID,
        symbol=symbol,
        owner_key="AAPL" if position_type == "single_leg" else "spread_1",
        strategy="sma_crossover" if position_type == "single_leg" else "credit_spread",
        strategy_version="1.0",
        strategy_config_hash="abc123",
        bot_git_commit="deadbeef",
        position_type=position_type,
        entry_qty=10,
        legs=legs,
    )
    store.mark_open(position_uid=UID, avg_entry_price=100, current_qty=10)


def _trade(conn, *, timestamp, side, price, pnl=None, symbol="AAPL", qty=10):
    conn.execute(
        "INSERT INTO trades (timestamp, symbol, side, qty, avg_fill_price, "
        "strategy, reason, status, filled_qty, position_type, position_uid, "
        "realized_pnl) VALUES (?, ?, ?, ?, ?, 'sma_crossover', 'test', "
        "'filled', ?, 'single_leg', ?, ?)",
        (timestamp, symbol, side, qty, price, qty, UID, pnl),
    )
    conn.commit()


class TestStrategyDailyMarkStore:
    def test_same_day_upsert_keeps_latest_broker_mark(self, tmp_path):
        conn, lifecycle, marks = _stores(tmp_path / "trades.db")
        _create_open(lifecycle)

        first = Position("AAPL", 10, 100, 1050, cost_basis=1000, unrealized_pl=50)
        second = Position("AAPL", 10, 100, 1060, cost_basis=1000, unrealized_pl=60)
        marks.record_snapshot(
            {"AAPL": first},
            observed_at=datetime(2026, 9, 7, 18, tzinfo=timezone.utc),
        )
        marks.record_snapshot(
            {"AAPL": second},
            observed_at=datetime(2026, 9, 7, 21, tzinfo=timezone.utc),
        )

        rows = conn.execute(
            "SELECT observed_at, unrealized_pnl, total_pnl "
            "FROM strategy_daily_marks"
        ).fetchall()
        assert rows == [("2026-09-07T21:00:00+00:00", 60.0, 60.0)]

    def test_spread_mark_sums_broker_reported_leg_pnl(self, tmp_path):
        conn, lifecycle, marks = _stores(tmp_path / "trades.db")
        legs = (
            PositionLifecycleLeg(UID, "SPY260918P00600000", "sell", 1),
            PositionLifecycleLeg(UID, "SPY260918P00595000", "buy", 1),
        )
        _create_open(
            lifecycle,
            position_type="spread",
            symbol="SPY260918P00600000",
            legs=legs,
        )
        positions = {
            legs[0].symbol: Position(legs[0].symbol, -1, 2, -150, unrealized_pl=50),
            legs[1].symbol: Position(legs[1].symbol, 1, 1, 80, unrealized_pl=-20),
        }

        marks.record_snapshot(
            positions,
            observed_at=datetime(2026, 9, 7, 21, tzinfo=timezone.utc),
        )

        row = conn.execute(
            "SELECT unrealized_pnl, total_pnl, missing_lifecycles "
            "FROM strategy_daily_marks"
        ).fetchone()
        assert row == (30.0, 30.0, 0)

    def test_missing_broker_position_is_recorded_not_invented(self, tmp_path):
        conn, lifecycle, marks = _stores(tmp_path / "trades.db")
        _create_open(lifecycle)

        marks.record_snapshot(
            {}, observed_at=datetime(2026, 9, 7, 21, tzinfo=timezone.utc)
        )

        row = conn.execute(
            "SELECT unrealized_pnl, total_pnl, missing_lifecycles "
            "FROM strategy_daily_marks"
        ).fetchone()
        assert row == (None, None, 1)

    def test_post_snapshot_realized_event_is_not_mixed_into_old_mark(self, tmp_path):
        conn, lifecycle, marks = _stores(tmp_path / "trades.db")
        _create_open(lifecycle)
        _trade(
            conn,
            timestamp="2026-09-07T22:00:00+00:00",
            side="sell",
            price=105,
            pnl=50,
        )
        lifecycle.refresh_realized_pnl(position_uid=UID)

        marks.record_snapshot(
            {"AAPL": Position("AAPL", 10, 100, 1020, unrealized_pl=20)},
            observed_at=datetime(2026, 9, 7, 21, tzinfo=timezone.utc),
        )

        row = conn.execute(
            "SELECT realized_pnl, unrealized_pnl, total_pnl "
            "FROM strategy_daily_marks"
        ).fetchone()
        assert row == (0.0, 20.0, 20.0)

    def test_closed_cohort_is_not_given_invented_predeployment_history(self, tmp_path):
        conn, lifecycle, marks = _stores(tmp_path / "trades.db")
        _create_open(lifecycle)
        lifecycle.mark_closed(position_uid=UID, net_realized_pnl=0)

        written = marks.record_snapshot(
            {}, observed_at=datetime(2026, 9, 7, 21, tzinfo=timezone.utc)
        )

        assert written == 0
        assert conn.execute("SELECT COUNT(*) FROM strategy_daily_marks").fetchone()[0] == 0

    def test_closed_cohort_stops_appending_after_final_mark(self, tmp_path):
        conn, lifecycle, marks = _stores(tmp_path / "trades.db")
        _create_open(lifecycle)
        marks.record_snapshot(
            {"AAPL": Position("AAPL", 10, 100, 1050, unrealized_pl=50)},
            observed_at=datetime(2026, 9, 7, 21, tzinfo=timezone.utc),
        )
        _trade(
            conn,
            timestamp="2026-09-08T15:00:00+00:00",
            side="sell",
            price=105,
            pnl=50,
        )
        lifecycle.mark_closed(position_uid=UID, net_realized_pnl=50)
        assert marks.record_snapshot(
            {}, observed_at=datetime(2026, 9, 8, 21, tzinfo=timezone.utc)
        ) == 1

        assert marks.record_snapshot(
            {}, observed_at=datetime(2026, 9, 9, 21, tzinfo=timezone.utc)
        ) == 0
        assert conn.execute("SELECT COUNT(*) FROM strategy_daily_marks").fetchone()[0] == 2


class TestRegulatoryCostModel:
    def test_equity_round_trip_uses_actual_fill_principal(self):
        estimate = DEFAULT_COST_MODEL.estimate([
            {"timestamp": "2026-09-07T15:00:00+00:00", "symbol": "AAPL", "side": "buy", "qty": 10, "filled_qty": 10, "avg_fill_price": 100, "status": "filled"},
            {"timestamp": "2026-09-08T15:00:00+00:00", "symbol": "AAPL", "side": "sell", "qty": 10, "filled_qty": 10, "avg_fill_price": 110, "status": "filled"},
        ])

        assert estimate.complete
        assert estimate.amount == pytest.approx(0.04006)

    def test_option_round_trip_includes_per_contract_fees(self):
        symbol = "SPY260918C00600000"
        estimate = DEFAULT_COST_MODEL.estimate([
            {"timestamp": "2026-09-07", "symbol": symbol, "side": "buy", "qty": 1, "filled_qty": 1, "avg_fill_price": 2, "status": "filled"},
            {"timestamp": "2026-09-08", "symbol": symbol, "side": "sell", "qty": 1, "filled_qty": 1, "avg_fill_price": 3, "status": "filled"},
        ])

        assert estimate.complete
        assert estimate.amount == pytest.approx(0.1165)

    def test_missing_sell_principal_is_unavailable(self):
        estimate = DEFAULT_COST_MODEL.estimate([
            {"timestamp": "2026-09-07", "symbol": "SPY260918P00600000", "side": "buy", "qty": 1, "filled_qty": 1, "avg_fill_price": 1, "status": "filled"},
            {"timestamp": "2026-09-08", "symbol": "SPY260918P00595000", "side": "sell", "qty": 1, "filled_qty": 1, "avg_fill_price": None, "status": "filled"},
        ])

        assert not estimate.complete
        assert estimate.amount is None
        assert estimate.reasons == ("missing_sell_principal",)


class TestGraduationDailyMarksAndCosts:
    def test_report_uses_forward_marks_and_complete_cost_model(self, tmp_path):
        db = tmp_path / "trades.db"
        conn, lifecycle, marks = _stores(db)
        _create_open(lifecycle)
        _trade(
            conn,
            timestamp="2026-09-07T15:00:00+00:00",
            side="buy",
            price=100,
        )
        marks.record_snapshot(
            {"AAPL": Position("AAPL", 10, 100, 980, unrealized_pl=-20)},
            observed_at=datetime(2026, 9, 7, 21, tzinfo=timezone.utc),
        )
        _trade(
            conn,
            timestamp="2026-09-08T15:00:00+00:00",
            side="sell",
            price=110,
            pnl=100,
        )
        lifecycle.mark_closed(position_uid=UID, net_realized_pnl=100)
        marks.record_snapshot(
            {}, observed_at=datetime(2026, 9, 8, 21, tzinfo=timezone.utc)
        )
        conn.close()

        cohort = build_graduation_report(db)["cohorts"][0]

        assert cohort["coverage"]["complete_daily_marks"] == 2
        assert [
            mark["total_pnl"] for mark in cohort["mark_to_market"]["daily"]
        ] == [-20.0, 100.0]
        assert cohort["performance"]["forward_daily_total_max_drawdown"] == -20
        assert cohort["performance"]["estimated_regulatory_costs"] == pytest.approx(0.04006)
        assert cohort["performance"]["net_after_costs"] == pytest.approx(99.95994)
        assert cohort["evidence_status"] == "EARLY EVIDENCE"
