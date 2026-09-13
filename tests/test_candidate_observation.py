"""PLAN 11.61 entry-candidate observation contracts."""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timezone

import pytest
import pandas as pd

from engine.candidate_observation import (
    CandidateObservationStore,
    CandidateStart,
    _CAPACITY_DISPOSITIONS,
)
from risk.allocator import SleeveRejectionCode
from risk.manager import RejectionCode
from strategies.donchian_breakout import DonchianBreakout
from strategies.leveraged_trend import LeveragedTrend
from strategies.rsi_reversion import RSIReversion
from strategies.sma_crossover import SMACrossover
from strategies.spy_options_reversion import SPYOptionsReversionStrategy


NOW = datetime(2026, 9, 8, 14, 30, tzinfo=timezone.utc)


def _start(
    *,
    cycle_uid: str = "cycle-1",
    strategy: str = "donchian_breakout",
    symbol: str = "AAPL",
    ordinal: int = 0,
) -> CandidateStart:
    return CandidateStart(
        cycle_uid=cycle_uid,
        signal_at=NOW,
        strategy=strategy,
        strategy_version="v1",
        strategy_config_hash="cfg123",
        bot_git_commit="abc123",
        symbol=symbol,
        signal_symbol=symbol,
        timeframe="1Day",
        data_feed="iex",
        regime="trending",
        slot_ordinal=2,
        watchlist_ordinal=ordinal,
        evaluation_ordinal=ordinal + 10,
        feature_schema_version=1,
        reference_price=100.0,
        atr=2.5,
        strategy_features={"breakout_pct": 0.012, "missing": math.nan},
        common_context={"sleeve": {"available": 1_000.0}},
    )


@pytest.fixture
def store() -> CandidateObservationStore:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    return CandidateObservationStore(conn)


class TestCandidateObservationStore:
    def test_capacity_dispositions_follow_source_enums(self) -> None:
        assert _CAPACITY_DISPOSITIONS == {
            SleeveRejectionCode.SLEEVE_FULL.value,
            SleeveRejectionCode.SLEEVE_MAX_POSITIONS.value,
            RejectionCode.GROSS_EXPOSURE_CAP.value,
            RejectionCode.INSUFFICIENT_CASH.value,
            RejectionCode.MAX_POSITIONS_REACHED.value,
            RejectionCode.MAX_STRATEGY_HEAT_REACHED.value,
        }

    def test_start_persists_identity_and_sanitizes_non_finite_features(
        self, store: CandidateObservationStore
    ) -> None:
        uid = store.start(_start(), observed_at=NOW)

        [row] = store.read_cycle("cycle-1")
        assert row["candidate_uid"] == uid
        assert row["strategy_version"] == "v1"
        assert row["strategy_config_hash"] == "cfg123"
        assert row["strategy_features"] == {
            "breakout_pct": 0.012,
            "missing": None,
        }
        assert row["disposition"] == "eligible"
        assert row["selected"] == 0

    def test_execution_enrichment_merges_instead_of_erasing_prior_facts(
        self, store: CandidateObservationStore
    ) -> None:
        uid = store.start(_start(), observed_at=NOW)
        store.update(
            uid,
            execution_features_json={"rank_score": 0.7},
            disposition="approved",
        )
        store.update(
            uid,
            execution_features_json={"projected_heat_dollars": 900.0},
            selected=True,
            disposition="accepted",
            position_uid="position-1",
        )

        [row] = store.read_cycle("cycle-1")
        assert row["execution_features"] == {
            "projected_heat_dollars": 900.0,
            "rank_score": 0.7,
        }
        assert row["selected"] == 1
        assert row["position_uid"] == "position-1"

    def test_noncontended_candidate_does_not_create_shadow_work(
        self, store: CandidateObservationStore
    ) -> None:
        uid = store.start(_start(), observed_at=NOW)
        store.update(uid, selected=True, disposition="filled")

        store.finalize_cycle("cycle-1")

        [row] = store.read_cycle("cycle-1")
        assert row["candidate_group_size"] == 1
        assert row["capacity_contended"] == 0
        count = store._conn.execute(
            "SELECT COUNT(*) FROM entry_candidate_shadow_outcomes"
        ).fetchone()[0]
        assert count == 0

    def test_capacity_contention_queues_actual_and_counterfactual_outcomes(
        self, store: CandidateObservationStore
    ) -> None:
        selected = store.start(_start(symbol="AAPL", ordinal=0), observed_at=NOW)
        blocked = store.start(_start(symbol="MSFT", ordinal=1), observed_at=NOW)
        store.update(
            selected,
            selected=True,
            disposition="filled",
            position_uid="position-1",
        )
        store.update(blocked, disposition="sleeve_full")

        store.finalize_cycle("cycle-1")

        rows = {row["symbol"]: row for row in store.read_cycle("cycle-1")}
        assert rows["AAPL"]["candidate_group_size"] == 2
        assert rows["MSFT"]["capacity_contended"] == 1
        shadows = store._conn.execute(
            "SELECT candidate_uid, status, outcome_basis, position_uid "
            "FROM entry_candidate_shadow_outcomes ORDER BY candidate_uid"
        ).fetchall()
        assert set(shadows) == {
            (selected, "actual_lifecycle", "actual_lifecycle", "position-1"),
            (blocked, "pending", "counterfactual_required", None),
        }

        store.record_shadow_outcome(
            blocked,
            status="resolved",
            outcome_basis="strategy_replay",
            exit_price=108.0,
            exit_at=NOW,
            return_pct=0.08,
            max_favorable_pct=0.10,
            max_adverse_pct=-0.02,
            metadata_json={"fill_status": "filled"},
        )
        resolved = store._conn.execute(
            "SELECT status, outcome_basis, return_pct, metadata_json "
            "FROM entry_candidate_shadow_outcomes WHERE candidate_uid = ?",
            (blocked,),
        ).fetchone()
        assert resolved == (
            "resolved",
            "strategy_replay",
            0.08,
            '{"fill_status":"filled"}',
        )

    def test_candidates_from_different_strategies_do_not_compete(
        self, store: CandidateObservationStore
    ) -> None:
        first = store.start(_start(strategy="sma_crossover"), observed_at=NOW)
        second = store.start(
            _start(strategy="rsi_reversion", symbol="MSFT"), observed_at=NOW
        )
        store.update(first, disposition="filled", selected=True)
        store.update(second, disposition="sleeve_full")

        store.finalize_cycle("cycle-1")

        assert all(
            row["candidate_group_size"] == 1
            and row["capacity_contended"] == 0
            for row in store.read_cycle("cycle-1")
        )

    def test_preexisting_full_sleeve_is_not_mislabeled_as_order_contention(
        self, store: CandidateObservationStore
    ) -> None:
        first = store.start(_start(symbol="AAPL"), observed_at=NOW)
        second = store.start(_start(symbol="MSFT", ordinal=1), observed_at=NOW)
        store.update(first, disposition="sleeve_full")
        store.update(second, disposition="sleeve_full")

        store.finalize_cycle("cycle-1")

        assert all(
            row["candidate_group_size"] == 2
            and row["capacity_contended"] == 0
            for row in store.read_cycle("cycle-1")
        )

    def test_unknown_update_field_is_rejected(self, store: CandidateObservationStore) -> None:
        uid = store.start(_start(), observed_at=NOW)
        with pytest.raises(ValueError, match="unknown candidate update fields"):
            store.update(uid, invented="value")


class TestStrategyCandidateFeatures:
    @staticmethod
    def _frame(length: int = 240) -> pd.DataFrame:
        index = pd.date_range("2025-01-01", periods=length, freq="D", tz="UTC")
        close = pd.Series(
            [100.0 + idx * 0.2 + (idx % 5) * 0.05 for idx in range(length)],
            index=index,
        )
        return pd.DataFrame(
            {
                "open": close - 0.1,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": [1_000_000 + idx * 1_000 for idx in range(length)],
            },
            index=index,
        )

    def test_sma_features_describe_crossover_shape(self) -> None:
        features = SMACrossover(20, 50).candidate_features(self._frame())
        assert features["fast_window"] == 20
        assert features["slow_window"] == 50
        assert features["crossover_gap_pct"] > 0
        assert "fast_slope_pct" in features

    def test_rsi_features_describe_oversold_depth_and_reversion_distance(self) -> None:
        frame = self._frame()
        features = RSIReversion(
            period=3,
            oversold=15,
            entry_mode="level_below",
            exit_sma_window=5,
            quick_exit_rsi=55,
        ).candidate_features(frame)
        assert features["period"] == 3
        assert features["oversold"] == 15.0
        assert "oversold_depth" in features
        assert "distance_to_exit_sma_pct" in features

    def test_rsi_replay_contract_freezes_execution_and_exit_policy(self) -> None:
        strategy = RSIReversion(
            period=3,
            oversold=15,
            entry_mode="level_below",
            exit_sma_window=5,
            quick_exit_rsi=55,
        )

        contract = strategy.candidate_replay_contract()

        assert strategy.candidate_feature_schema_version == 2
        assert contract["period"] == 3
        assert contract["entry_order_type"] == "limit"
        assert "entry_time_in_force" not in contract
        assert contract["stop_anchor"] == "reference"
        assert contract["atr_stop_multiplier"] > 0
        assert contract["exit_order_type"] == "market"

    def test_donchian_features_describe_breakout_and_channel(self) -> None:
        features = DonchianBreakout(
            entry_window=30, exit_window=15
        ).candidate_features(self._frame())
        assert features["entry_window"] == 30
        assert features["breakout_pct"] > 0
        assert features["channel_width_pct"] > 0

    def test_leveraged_features_use_unleveraged_signal_series(self) -> None:
        frame = self._frame()
        frame["signal_close"] = frame["close"] / 3.0
        features = LeveragedTrend(
            sma_length=200, entry_days=5, exit_days=2
        ).candidate_features(frame)
        assert features["confirmed_above_streak"] >= 5
        assert features["distance_above_sma_pct"] > 0
        assert features["stress_exposure_multiplier"] == 3.0

    def test_spy_option_features_capture_rsi_threshold_cross_shape(self) -> None:
        features = SPYOptionsReversionStrategy(
            rsi_length=14, rsi_threshold=45
        ).candidate_features(self._frame())
        assert features["rsi_length"] == 14
        assert features["rsi_threshold"] == 45
        assert "threshold_cross_size" in features
