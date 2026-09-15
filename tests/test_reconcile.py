"""Tests for lifecycle-first paper-versus-backtest reconciliation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from backtest.reconcile import (
    BacktestLifecycle,
    PaperLifecycle,
    Reconciler,
    ReconciliationResult,
    _next_execution_bar,
)
from backtest.runner import BacktestConfig, BacktestResult
from config import settings
from engine.candidate_observation import CandidateObservationStore, CandidateStart
from engine.lifecycle import PositionLifecycleStore
from execution.broker import AlpacaBroker, OrderStatus
from reporting.logger import TradeLogger
from regime.detector import MarketRegime
from risk.models import ProtectionModel, SizingModel
from sector.gauge import SectorMomentumGauge
from sector.resolver import SectorResolver
from strategies.base import BaseStrategy, OrderType, SignalFrame
from strategies.filters.common import CompositeEdgeFilter
from strategies.filters.sector_momentum import SectorMomentumFilter
from strategies.filters.sma_crossover import SMAEdgeFilter
from strategies.identity import resolve_strategy_identity
from strategies.sma_crossover import SMACrossover


class _DummyStrategy(BaseStrategy):
    name = "sma_crossover"
    preferred_order_type = OrderType.MARKET

    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        return SignalFrame(
            entries=pd.Series(False, index=df.index, dtype=bool),
            exits=pd.Series(False, index=df.index, dtype=bool),
        )


@pytest.fixture
def recon(tmp_path: Path) -> Reconciler:
    return Reconciler(
        _DummyStrategy(),
        ["AAPL"],
        "2026-09-01",
        "2026-09-30",
        allowed_regimes=frozenset(),
        trade_csv_path=str(tmp_path / "trades.db"),
        forward_test_dir=str(tmp_path / "reports"),
    )


def _paper(**changes) -> PaperLifecycle:
    values = {
        "position_uid": "pos_" + "a" * 32,
        "strategy": "sma_crossover",
        "strategy_version": "v1",
        "strategy_config_hash": "cfg123",
        "bot_git_commit": "abc123",
        "symbol": "AAPL",
        "position_type": "single_leg",
        "status": "closed",
        "signal_at": "2026-09-01T00:00:00+00:00",
        "signal_symbol": "AAPL",
        "timeframe": "1Day",
        "data_feed": "iex",
        "first_fill_at": "2026-09-02T14:30:00+00:00",
        "closed_at": "2026-09-04T14:30:00+00:00",
        "entry_price": 101.0,
        "exit_price": 109.0,
        "realized_pnl": 80.0,
        "operator_modified": False,
        "signal_anchor_count": 1,
    }
    values.update(changes)
    return PaperLifecycle(**values)


def _bt_result(*, entries: list[bool] | None = None) -> BacktestResult:
    index = pd.date_range("2026-09-01", periods=5, freq="D", tz="UTC")
    records = pd.DataFrame({
        "Entry Timestamp": [index[1]],
        "Avg Entry Price": [100.0],
        "Exit Timestamp": [index[3]],
        "Avg Exit Price": [110.0],
        "Status": ["Closed"],
    })
    portfolio = MagicMock()
    portfolio.trades.records_readable = records
    return BacktestResult(
        portfolio=portfolio,
        stats={"trade_count": 1},
        entries_executed=pd.Series(
            entries or [False, True, False, False, False], index=index
        ),
        exits_executed=pd.Series(False, index=index),
        config=BacktestConfig(),
        strategy_name="sma_crossover",
        symbol="AAPL",
    )


def _production_sma() -> SMACrossover:
    """Mirror the behavior-affecting SMA construction in forward_test.py."""
    return SMACrossover(
        fast=20,
        slow=50,
        edge_filter=CompositeEdgeFilter([
            SMAEdgeFilter(),
            SectorMomentumFilter(
                gauge=SectorMomentumGauge(sector_etfs=settings.SECTOR_ETFS),
                resolver=SectorResolver(valid_sectors=set(settings.SECTOR_ETFS)),
                sector_entry_policy="warn",
            ),
        ]),
    )


class TestLifecycleMatching:
    def test_run_uses_engine_allowed_regimes_for_identity(self, tmp_path):
        strategy = _production_sma()
        allowed = frozenset({MarketRegime.TRENDING, MarketRegime.RANGING})
        identity = resolve_strategy_identity(
            strategy,
            allowed_regimes=allowed,
            data_feed=settings.ALPACA_DATA_FEED,
            timeframe="1Day",
        )
        fallback_identity = resolve_strategy_identity(
            strategy,
            data_feed=settings.ALPACA_DATA_FEED,
            timeframe="1Day",
        )
        assert identity.strategy_config_hash != fallback_identity.strategy_config_hash

        reconciler = Reconciler(
            strategy,
            ["AAPL"],
            "2026-09-01",
            "2026-09-30",
            allowed_regimes=allowed,
            trade_csv_path=str(tmp_path / "trades.db"),
        )
        conn = reconciler._trade_logger._ensure_db()
        uid = "pos_" + "d" * 32
        lifecycle = PositionLifecycleStore(conn)
        lifecycle.create_pending(
            position_uid=uid,
            symbol="AAPL",
            owner_key="AAPL",
            strategy="sma_crossover",
            strategy_version=identity.strategy_version,
            strategy_config_hash=identity.strategy_config_hash,
            bot_git_commit=identity.bot_git_commit,
            position_type="single_leg",
            entry_qty=10,
            sizing_model=SizingModel.STOP_DISTANCE,
            protection_model=ProtectionModel.BROKER_STOP,
        )
        lifecycle.mark_open(
            position_uid=uid,
            avg_entry_price=101.0,
            current_qty=10,
            first_fill_at="2026-09-02T14:30:00+00:00",
        )
        candidates = CandidateObservationStore(conn)
        candidate_uid = candidates.start(CandidateStart(
            cycle_uid="identity-cycle",
            signal_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            strategy="sma_crossover",
            strategy_version=identity.strategy_version,
            strategy_config_hash=identity.strategy_config_hash,
            bot_git_commit=identity.bot_git_commit,
            symbol="AAPL",
            signal_symbol="AAPL",
            timeframe="1Day",
            data_feed=settings.ALPACA_DATA_FEED,
            regime="trending",
            slot_ordinal=0,
            watchlist_ordinal=0,
            evaluation_ordinal=0,
            feature_schema_version=1,
            reference_price=100.0,
            atr=2.0,
            strategy_features={},
            common_context={},
        ))
        candidates.update(
            candidate_uid,
            selected=True,
            position_uid=uid,
        )
        backtest = _bt_result()
        with patch.object(
            reconciler, "_run_backtest_for_symbol", return_value=backtest
        ):
            result = reconciler.run()

        assert result.matched_count == 1
        assert result.unresolved_count == 0
        assert result.comparisons[0].reason == "exact_signal_bar"

    def test_signal_bar_maps_to_next_execution_bar(self):
        index = pd.date_range("2026-09-01", periods=3, freq="D", tz="UTC")
        assert _next_execution_bar(index, "2026-09-01T00:00:00+00:00") == index[1]

    def test_exact_signal_bar_matches_complete_round_trip(self, recon):
        result = _bt_result()
        rows = recon._extract_backtest_lifecycles(
            result, strategy_config_hash="cfg123"
        )
        [comparison] = recon._match_lifecycles(
            [_paper()],
            {"AAPL": result},
            {"AAPL": rows},
            expected_version="v1",
            expected_config_hash="cfg123",
        )
        assert comparison.status == "matched"
        assert comparison.reason == "exact_signal_bar"
        assert comparison.entry_diff_bps == pytest.approx(99.0)
        assert comparison.exit_diff_bps == pytest.approx(-91.7)
        assert comparison.backtest_lifecycle_id.startswith("bt_")

    def test_backtest_lifecycle_cannot_be_reused(self, recon):
        result = _bt_result()
        rows = recon._extract_backtest_lifecycles(
            result, strategy_config_hash="cfg123"
        )
        comparisons = recon._match_lifecycles(
            [_paper(), _paper(position_uid="pos_" + "b" * 32)],
            {"AAPL": result},
            {"AAPL": rows},
            expected_version="v1",
            expected_config_hash="cfg123",
        )
        assert [row.status for row in comparisons] == ["matched", "unresolved"]
        assert comparisons[1].reason == "backtest_lifecycle_already_used"

    @pytest.mark.parametrize(
        ("changes", "reason"),
        [
            ({"signal_at": None, "signal_anchor_count": 0}, "missing_signal_anchor"),
            ({"strategy_version": "v0"}, "strategy_version_mismatch"),
            ({"strategy_config_hash": "old"}, "strategy_config_mismatch"),
            ({"data_feed": "sip"}, "data_feed_mismatch"),
            ({"symbol": "SPY260918C00500000"}, "unsupported_instrument_model"),
        ],
    )
    def test_untrusted_identity_is_never_fuzzily_matched(self, recon, changes, reason):
        result = _bt_result()
        [comparison] = recon._match_lifecycles(
            [_paper(**changes)],
            {"AAPL": result},
            {"AAPL": recon._extract_backtest_lifecycles(
                result, strategy_config_hash="cfg123"
            )},
            expected_version="v1",
            expected_config_hash="cfg123",
        )
        assert comparison.status == "unresolved"
        assert comparison.reason == reason
        assert comparison.backtest_lifecycle_id is None

    def test_price_similarity_does_not_override_wrong_signal_bar(self, recon):
        result = _bt_result()
        [comparison] = recon._match_lifecycles(
            [_paper(signal_at="2026-09-02T00:00:00+00:00", entry_price=100.0)],
            {"AAPL": result},
            {"AAPL": recon._extract_backtest_lifecycles(
                result, strategy_config_hash="cfg123"
            )},
            expected_version="v1",
            expected_config_hash="cfg123",
        )
        assert comparison.reason == "backtest_no_entry_on_expected_bar"


class TestLifecycleReadAndReport:
    def test_partial_exits_are_weighted_inside_one_lifecycle(self, recon):
        conn = recon._trade_logger._ensure_db()
        lifecycle = PositionLifecycleStore(conn)
        uid = "pos_" + "c" * 32
        lifecycle.create_pending(
            position_uid=uid,
            symbol="AAPL",
            owner_key="AAPL",
            strategy="sma_crossover",
            strategy_version="v1",
            strategy_config_hash="cfg123",
            bot_git_commit="abc123",
            position_type="single_leg",
            entry_qty=10,
            sizing_model=SizingModel.STOP_DISTANCE,
            protection_model=ProtectionModel.BROKER_STOP,
        )
        lifecycle.mark_open(
            position_uid=uid,
            avg_entry_price=100.0,
            current_qty=10,
            first_fill_at="2026-09-02T14:30:00+00:00",
        )
        conn.execute(
            "UPDATE position_lifecycle SET status='closed', current_qty=0, "
            "closed_at='2026-09-10T14:30:00+00:00', net_realized_pnl=160 "
            "WHERE position_uid=?",
            (uid,),
        )
        candidates = CandidateObservationStore(conn)
        candidate_uid = candidates.start(CandidateStart(
            cycle_uid="cycle-1",
            signal_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
            strategy="sma_crossover",
            strategy_version="v1",
            strategy_config_hash="cfg123",
            bot_git_commit="abc123",
            symbol="AAPL",
            signal_symbol="AAPL",
            timeframe="1Day",
            data_feed="iex",
            regime="TRENDING",
            slot_ordinal=0,
            watchlist_ordinal=0,
            evaluation_ordinal=0,
            feature_schema_version=1,
            reference_price=100.0,
            atr=2.0,
            strategy_features={},
            common_context={},
        ))
        candidates.update(candidate_uid, selected=True, position_uid=uid)
        for order_id, price, qty, pnl in (
            ("exit-1", 110.0, 4, 40.0),
            ("exit-2", 120.0, 6, 120.0),
        ):
            conn.execute(
                "INSERT INTO trades (timestamp,symbol,side,qty,avg_fill_price,"
                "order_id,strategy,reason,status,filled_qty,position_type,"
                "position_uid,realized_pnl) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                ("2026-09-10T14:30:00+00:00", "AAPL", "sell", qty, price,
                 order_id, "sma_crossover", "exit", "filled", qty,
                 "single_leg", uid, pnl),
            )
        conn.commit()

        [paper] = recon._read_paper_lifecycles()
        assert paper.position_uid == uid
        assert paper.exit_price == pytest.approx(116.0)
        assert paper.signal_at.startswith("2026-09-01")

        uid_only = Reconciler(
            _DummyStrategy(),
            [],
            "2026-09-01",
            "2026-09-30",
            allowed_regimes=frozenset(),
            trade_csv_path=recon._trade_logger.path,
            position_uids=[uid],
        )
        assert [row.position_uid for row in uid_only._read_paper_lifecycles()] == [uid]

    def test_report_is_advisory_and_discloses_unresolved_rows(self, recon):
        result = ReconciliationResult(
            strategy_name="sma_crossover",
            symbols=["AAPL"],
            start_date="2026-09-01",
            end_date="2026-09-30",
            paper_lifecycle_count=1,
            backtest_lifecycle_count=0,
            matched_count=0,
            unresolved_count=1,
            comparisons=[],
        )
        path = recon.write_report(result)
        content = Path(path).read_text()
        assert "Advisory only" in content
        assert "does not approve" in content
        assert "GO" not in content
        assert "nearest" not in content


class TestGetClosedOrders:
    def test_returns_order_results(self):
        client = MagicMock()
        order = MagicMock()
        order.id = "order-1"
        order.symbol = "AAPL"
        order.qty = "10"
        order.filled_qty = "10"
        order.filled_avg_price = "150.05"
        order.status = "filled"
        order.side = "buy"
        client.get_orders.return_value = [order]

        results = AlpacaBroker(client=client).get_closed_orders()
        assert len(results) == 1
        assert results[0].status == OrderStatus.FILLED
        assert results[0].avg_fill_price == 150.05
