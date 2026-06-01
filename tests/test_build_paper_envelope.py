"""
Unit tests for scripts/build_paper_envelope.py.

Covers the four refusal modes from design §8 (false envelopes are worse
than no envelope) and the happy path: synthetic trades + lifecycle rows
in, envelope JSON out with correct fields.

Pattern matches tests/test_calibrate_health_thresholds.py: a temp-path
TradeLogger gives us a real schema-correct DB to seed, then the script's
pure builder function is exercised directly without going through CLI.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from reporting.logger import TradeLogger
from scripts.build_paper_envelope import (
    EXIT_ALL_R_NULL,
    EXIT_INSUFFICIENT_LIFECYCLE,
    EXIT_INSUFFICIENT_TRADES,
    PaperEnvelopeError,
    _most_recent_completed_monday,
    build_envelope,
)
from strategies.health.envelope import StrategyEnvelope, envelope_path
from strategies.health.lifecycle import LifecycleCounters, upsert_counters


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def db_conn(tmp_path: Path):
    logger = TradeLogger(path=str(tmp_path / "trades.db"))
    conn = logger._ensure_db()
    yield conn
    logger.close()


def _seed_closed_trade(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    timestamp: str,
    realized_pnl: float,
    r_multiple: float | None,
    entry_timestamp: str | None = None,
    exit_timestamp: str | None = None,
) -> None:
    """Seed one closed-trade row matching the assessor's read filter
    (`side='sell'`, `status='filled'`, `realized_pnl IS NOT NULL`)."""
    conn.execute(
        "INSERT INTO trades ("
        "timestamp, symbol, side, qty, avg_fill_price, order_id, "
        "strategy, reason, stop_price, entry_reference_price, "
        "modeled_slippage_bps, realized_slippage_bps, order_type, "
        "status, requested_qty, filled_qty, realized_pnl, r_multiple, "
        "entry_timestamp, exit_timestamp"
        ") VALUES (?, 'SPY', 'sell', 1.0, 0.0, 'oid', ?, 'exit', "
        "0.0, 0.0, 0.0, 0.0, 'market', 'filled', 1.0, 1.0, ?, ?, ?, ?)",
        (
            timestamp, strategy, realized_pnl, r_multiple,
            entry_timestamp, exit_timestamp,
        ),
    )
    conn.commit()


def _seed_weekly_counter(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    period_start: date,
    **counts: int,
) -> None:
    period_end = period_start + timedelta(days=7)
    upsert_counters(
        conn,
        period_type="weekly",
        period_start=period_start,
        period_end=period_end,
        strategy_name=strategy,
        counters=LifecycleCounters(**counts),
    )


def _seed_n_closed_trades(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    n: int,
    base_date: date,
    r_value: float = 0.5,
    pnl_value: float = 100.0,
) -> None:
    """Seed N closed trades spaced one day apart starting at base_date."""
    for i in range(n):
        ts = (base_date + timedelta(days=i)).isoformat() + "T15:00:00+00:00"
        entry = (base_date + timedelta(days=i - 1)).isoformat() + "T15:00:00+00:00"
        _seed_closed_trade(
            conn,
            strategy=strategy,
            timestamp=ts,
            realized_pnl=pnl_value,
            r_multiple=r_value,
            entry_timestamp=entry,
            exit_timestamp=ts,
        )


def _seed_four_weeks_of_counters(
    conn: sqlite3.Connection,
    *,
    strategy: str,
    first_week_monday: date,
) -> None:
    """Seed four consecutive weekly counter rows with varying raw_signals."""
    weekly_counts = [
        {"raw_signals": 10, "edge_filter_blocked": 4, "submitted": 3,
         "filled_entries": 2},
        {"raw_signals": 14, "edge_filter_blocked": 7, "submitted": 4,
         "filled_entries": 3},
        {"raw_signals": 8,  "edge_filter_blocked": 3, "submitted": 2,
         "filled_entries": 1},
        {"raw_signals": 12, "edge_filter_blocked": 6, "submitted": 3,
         "filled_entries": 3},
    ]
    for i, counts in enumerate(weekly_counts):
        _seed_weekly_counter(
            conn,
            strategy=strategy,
            period_start=first_week_monday + timedelta(weeks=i),
            **counts,
        )


# ── Refusal modes ─────────────────────────────────────────────────────


class TestRefusals:
    def test_refuses_below_min_trades(self, db_conn):
        # Only 5 closed trades — below the default --min-trades=10.
        _seed_n_closed_trades(
            db_conn, strategy="credit_spread", n=5,
            base_date=date(2026, 5, 1),
        )
        with pytest.raises(PaperEnvelopeError) as exc:
            build_envelope(
                db_conn,
                strategy="credit_spread",
                period_start=date(2026, 4, 1),
                period_end=date(2026, 6, 1),
                min_trades=10,
            )
        assert exc.value.code == EXIT_INSUFFICIENT_TRADES
        assert "closed trades" in exc.value.message

    def test_refuses_when_all_r_multiples_null(self, db_conn):
        # 12 closed trades but every r_multiple is NULL — this is the
        # pre-Change-1 credit_spread state.
        for i in range(12):
            ts = (date(2026, 5, 1) + timedelta(days=i)).isoformat() + "T15:00:00+00:00"
            _seed_closed_trade(
                db_conn,
                strategy="credit_spread",
                timestamp=ts,
                realized_pnl=100.0,
                r_multiple=None,
                entry_timestamp=ts,
                exit_timestamp=ts,
            )
        with pytest.raises(PaperEnvelopeError) as exc:
            build_envelope(
                db_conn,
                strategy="credit_spread",
                period_start=date(2026, 4, 1),
                period_end=date(2026, 6, 1),
                min_trades=10,
            )
        assert exc.value.code == EXIT_ALL_R_NULL
        # Refusal hint must point at the upstream capture problem.
        assert "log_spread_fill" in exc.value.message
        assert "initial_risk_dollars" in exc.value.message

    def test_refuses_insufficient_lifecycle_weeks(self, db_conn):
        # 12 trades + only 2 lifecycle weeks — below MIN_WEEKLY_LIFECYCLE_ROWS.
        _seed_n_closed_trades(
            db_conn, strategy="credit_spread", n=12,
            base_date=date(2026, 5, 1), r_value=0.3,
        )
        _seed_weekly_counter(
            db_conn, strategy="credit_spread",
            period_start=date(2026, 5, 4),
            raw_signals=10, edge_filter_blocked=4,
            submitted=3, filled_entries=2,
        )
        _seed_weekly_counter(
            db_conn, strategy="credit_spread",
            period_start=date(2026, 5, 11),
            raw_signals=14, edge_filter_blocked=7,
            submitted=4, filled_entries=3,
        )
        with pytest.raises(PaperEnvelopeError) as exc:
            build_envelope(
                db_conn,
                strategy="credit_spread",
                period_start=date(2026, 4, 1),
                period_end=date(2026, 6, 1),
                min_trades=10,
            )
        assert exc.value.code == EXIT_INSUFFICIENT_LIFECYCLE
        assert "weekly lifecycle rows" in exc.value.message


# ── Happy path ────────────────────────────────────────────────────────


class TestEnvelopeBuilding:
    def test_writes_envelope_from_synthetic_trades(self, db_conn):
        # 20 closed trades, all r_multiple=0.5, all pnl=$100. Bootstrap CI
        # collapses near the point estimate when every sample is identical.
        _seed_n_closed_trades(
            db_conn, strategy="spy_options_reversion", n=20,
            base_date=date(2026, 5, 1), r_value=0.5, pnl_value=100.0,
        )
        _seed_four_weeks_of_counters(
            db_conn, strategy="spy_options_reversion",
            first_week_monday=date(2026, 5, 4),
        )
        envelope = build_envelope(
            db_conn,
            strategy="spy_options_reversion",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            min_trades=10,
        )
        assert isinstance(envelope, StrategyEnvelope)
        assert envelope.strategy == "spy_options_reversion"
        assert envelope.trade_count == 20
        assert envelope.r_expectancy == pytest.approx(0.5)
        # Bootstrap CI on a constant sequence collapses to the value itself.
        lo, hi = envelope.r_expectancy_ci_95
        assert lo == pytest.approx(0.5)
        assert hi == pytest.approx(0.5)
        assert envelope.expectancy_dollars == pytest.approx(100.0)
        # All wins → win_rate = 1.0, profit_factor is None (no losses for
        # the denominator).
        assert envelope.win_rate == pytest.approx(1.0)
        # The backtest_config payload records this is paper-derived.
        assert envelope.backtest_config["source"] == "paper"
        assert envelope.backtest_config["n_trades"] == 20
        # Note line includes provenance the operator can inspect.
        assert any("paper-derived envelope" in n for n in envelope.notes)

    def test_lifecycle_bands_pulled_from_counter_table(self, db_conn):
        _seed_n_closed_trades(
            db_conn, strategy="credit_spread", n=15,
            base_date=date(2026, 5, 1), r_value=0.3,
        )
        _seed_four_weeks_of_counters(
            db_conn, strategy="credit_spread",
            first_week_monday=date(2026, 5, 4),
        )
        envelope = build_envelope(
            db_conn,
            strategy="credit_spread",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            min_trades=10,
        )
        # raw_signals values: 10, 14, 8, 12 → p10≈8.6, p90≈13.4
        band = envelope.raw_signals_per_week_band
        assert band is not None
        lo, hi = band
        assert 8.0 <= lo <= 9.0
        assert 13.0 <= hi <= 14.0
        # edge_filter_block_rate: 0.40, 0.50, 0.375, 0.50 → ordered
        # [0.375, 0.40, 0.50, 0.50]; p10 and p90 land inside this range.
        edge_band = envelope.edge_filter_block_rate_band
        assert edge_band is not None
        elo, ehi = edge_band
        assert 0.37 <= elo <= 0.42
        assert 0.48 <= ehi <= 0.52
        # fill_rate: filled/submitted per week → 2/3, 3/4, 1/2, 3/3 =
        # [0.667, 0.75, 0.5, 1.0]; bands inside [0.5, 1.0].
        fill_band = envelope.fill_rate_band
        assert fill_band is not None
        flo, fhi = fill_band
        assert 0.5 <= flo
        assert fhi <= 1.0

    def test_skips_null_r_multiples_in_expectancy(self, db_conn):
        # Mixed: 6 with r_multiple, 6 with NULL. Edge metrics use only the
        # non-NULL ones; refusal does not trigger because some R exists.
        base = date(2026, 5, 1)
        for i in range(6):
            ts = (base + timedelta(days=i)).isoformat() + "T15:00:00+00:00"
            _seed_closed_trade(
                db_conn, strategy="credit_spread", timestamp=ts,
                realized_pnl=200.0, r_multiple=0.4,
                entry_timestamp=ts, exit_timestamp=ts,
            )
        for i in range(6, 12):
            ts = (base + timedelta(days=i)).isoformat() + "T15:00:00+00:00"
            _seed_closed_trade(
                db_conn, strategy="credit_spread", timestamp=ts,
                realized_pnl=150.0, r_multiple=None,
                entry_timestamp=ts, exit_timestamp=ts,
            )
        _seed_four_weeks_of_counters(
            db_conn, strategy="credit_spread",
            first_week_monday=date(2026, 5, 4),
        )
        envelope = build_envelope(
            db_conn,
            strategy="credit_spread",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            min_trades=10,
        )
        # trade_count uses all closed trades; r_expectancy uses only the
        # 6 with non-NULL r_multiple → 0.4.
        assert envelope.trade_count == 12
        assert envelope.r_expectancy == pytest.approx(0.4)
        # backtest_config records the discrepancy so the operator can see
        # the R-coverage rate.
        assert envelope.backtest_config["n_r_values"] == 6
        assert envelope.backtest_config["n_trades"] == 12

    def test_envelope_round_trips_through_json(self, db_conn, tmp_path):
        _seed_n_closed_trades(
            db_conn, strategy="spy_options_reversion", n=12,
            base_date=date(2026, 5, 1), r_value=0.5,
        )
        _seed_four_weeks_of_counters(
            db_conn, strategy="spy_options_reversion",
            first_week_monday=date(2026, 5, 4),
        )
        envelope = build_envelope(
            db_conn,
            strategy="spy_options_reversion",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            min_trades=10,
        )
        out_path = envelope_path(
            "spy_options_reversion", root=tmp_path,
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(envelope.to_json())
        # Read back via the dataclass loader.
        reloaded = StrategyEnvelope.from_json(out_path.read_text())
        assert reloaded.strategy == envelope.strategy
        assert reloaded.trade_count == envelope.trade_count
        assert reloaded.r_expectancy == pytest.approx(envelope.r_expectancy)


class TestMostRecentCompletedMonday:
    """Snap helper for the default CLI window. Reviewer catch on PR #34:
    a non-Monday `end_date` excludes the current partial week (correct)
    AND drops the leading partial week, so `--weeks 4` often saw only 3
    rows even after 4 full weeks of paper operation.
    """

    def test_monday_returns_itself(self):
        # 2026-06-01 is a Monday. The most recent completed week's
        # `period_end` is today's 00:00 UTC.
        assert _most_recent_completed_monday(date(2026, 6, 1)) == date(2026, 6, 1)

    def test_tuesday_snaps_back_one_day(self):
        # Tuesday → most recent completed week ended yesterday (Monday).
        assert _most_recent_completed_monday(date(2026, 6, 2)) == date(2026, 6, 1)

    def test_sunday_snaps_back_six_days(self):
        # Sunday → most recent completed week ended last Monday.
        assert _most_recent_completed_monday(date(2026, 6, 7)) == date(2026, 6, 1)

    def test_friday_snaps_back_four_days(self):
        assert _most_recent_completed_monday(date(2026, 6, 5)) == date(2026, 6, 1)

    def test_window_aligned_to_counter_grid_gives_full_week_count(self, db_conn):
        # Concrete reviewer scenario: today is Wednesday 2026-06-03 and the
        # bot has been writing 4 consecutive weekly counter rows (period_start
        # = 2026-05-04, 05-11, 05-18, 05-25). The unsnapped default would
        # compute end_date=2026-06-03 → window 2026-05-06..2026-06-03 →
        # period_start=2026-05-04 fails the `>= start` test, leaving 3 rows
        # → EXIT_INSUFFICIENT_LIFECYCLE. The snap helper makes end_date the
        # most recent completed Monday (2026-06-01), so start_date snaps to
        # 2026-05-04 and all four rows match.
        today = date(2026, 6, 3)  # Wednesday
        snapped_end = _most_recent_completed_monday(today)
        assert snapped_end == date(2026, 6, 1)
        snapped_start = snapped_end - timedelta(weeks=4)
        assert snapped_start == date(2026, 5, 4)
        # Anchor the trade seed to the snapped start so all 12 trades fall
        # inside the [snapped_start, snapped_end) window (otherwise the
        # window-boundary filter would skip the leading trades and the
        # build would fail EXIT_INSUFFICIENT_TRADES instead).
        _seed_n_closed_trades(
            db_conn, strategy="credit_spread", n=12,
            base_date=snapped_start, r_value=0.3,
        )
        _seed_four_weeks_of_counters(
            db_conn, strategy="credit_spread",
            first_week_monday=snapped_start,
        )
        envelope = build_envelope(
            db_conn,
            strategy="credit_spread",
            period_start=snapped_start,
            period_end=snapped_end,
            min_trades=10,
        )
        assert envelope.backtest_config["n_weekly_lifecycle_rows"] == 4
