"""
Unit tests for scripts/rsi_candidate_post_analysis.py.

The post-analysis layer is where validation winners become promotion
candidates. These tests lock down the guardrail behavior around drawdown and
weak exact-strategy performance.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from scripts.rsi_candidate_post_analysis import (
    PostAnalysisConfig,
    rank_results,
)
from scripts.rsi_candidate_validate import EventRecord, SymbolValidation


def _event(stop_failed: bool = False) -> EventRecord:
    return EventRecord(
        symbol="TST",
        date=datetime(2025, 1, 1, tzinfo=timezone.utc),
        close=100.0,
        rsi=28.0,
        hit_rsi50_10d=True,
        return_10d=0.04,
        max_drawdown_10d=-0.03,
        stop_failed=stop_failed,
        days_to_rsi50=5,
    )


def _validation(
    symbol: str,
    *,
    total_return: float,
    max_drawdown: float,
    profit_factor: float,
    events: int = 8,
    stop_failures: int = 1,
) -> SymbolValidation:
    event_records = [_event(i < stop_failures) for i in range(events)]
    return SymbolValidation(
        symbol=symbol,
        group="candidate",
        bars=1000,
        start=datetime(2021, 1, 1, tzinfo=timezone.utc),
        end=datetime(2025, 1, 1, tzinfo=timezone.utc),
        events=event_records,
        backtest_stats={
            "total_return": total_return,
            "max_drawdown": max_drawdown,
            "profit_factor": profit_factor,
            "trade_count": 5.0,
        },
        buy_hold_return=0.10,
    )


class TestRankResults:
    def test_promotes_clean_candidate(self):
        ranked = rank_results(
            [
                _validation(
                    "GOOD",
                    total_return=0.60,
                    max_drawdown=-0.30,
                    profit_factor=3.0,
                )
            ],
            config=PostAnalysisConfig(),
        )

        assert ranked[0].symbol == "GOOD"
        assert ranked[0].verdict == "PROMOTE"
        assert ranked[0].reasons == []

    def test_rejects_deep_drawdown_even_with_positive_return(self):
        ranked = rank_results(
            [
                _validation(
                    "DEEP",
                    total_return=0.80,
                    max_drawdown=-0.70,
                    profit_factor=2.0,
                )
            ],
            config=PostAnalysisConfig(),
        )

        assert ranked[0].verdict == "REJECT"
        assert "strategy drawdown too deep" in ranked[0].reasons

    def test_rejects_weak_exact_strategy_return(self):
        ranked = rank_results(
            [
                _validation(
                    "WEAK",
                    total_return=0.05,
                    max_drawdown=-0.20,
                    profit_factor=2.0,
                )
            ],
            config=PostAnalysisConfig(),
        )

        assert ranked[0].verdict == "REJECT"
        assert "negative or weak exact strategy return" in ranked[0].reasons

    def test_ranks_higher_score_first(self):
        ranked = rank_results(
            [
                _validation("OK", total_return=0.30, max_drawdown=-0.40, profit_factor=2.0),
                _validation("BEST", total_return=1.00, max_drawdown=-0.25, profit_factor=5.0),
            ],
            config=PostAnalysisConfig(),
        )

        assert [item.symbol for item in ranked] == ["BEST", "OK"]
        assert ranked[0].score > ranked[1].score
