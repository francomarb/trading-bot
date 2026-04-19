"""
Unit tests for reporting/metrics.py.

Every expected value is hand-computed so these tests function as a
specification, not just a regression suite.

Coverage:
  - compute_metrics on empty input
  - Sharpe ratio: positive, negative, zero-std, annualized vs raw
  - Max drawdown: peak-to-trough as fraction of peak
  - Profit factor: normal, no losses (inf), no wins (0)
  - Win rate: 100%, 0%, mixed
  - Avg win/loss ratio: normal, no losses (inf), no wins (0)
  - MetricsSnapshot.meets_go_thresholds: all-pass, individual failures
  - format_report: produces readable output
"""

from __future__ import annotations

import math

import pytest

from reporting.metrics import (
    AVG_WIN_LOSS_THRESHOLD,
    MAX_DRAWDOWN_THRESHOLD,
    PROFIT_FACTOR_THRESHOLD,
    SHARPE_THRESHOLD,
    TRADING_DAYS_PER_YEAR,
    WIN_RATE_THRESHOLD,
    MetricsSnapshot,
    compute_metrics,
)


# ── Empty input ─────────────────────────────────────────────────────────────


class TestEmptyInput:
    def test_no_trades_returns_zeroes(self):
        m = compute_metrics([])
        assert m.trade_count == 0
        assert m.sharpe_ratio == 0.0
        assert m.max_drawdown_pct == 0.0
        assert m.profit_factor == 0.0
        assert m.win_rate == 0.0
        assert m.avg_win_loss_ratio == 0.0
        assert m.total_pnl == 0.0

    def test_no_trades_fails_go_threshold(self):
        m = compute_metrics([])
        go, reasons = m.meets_go_thresholds()
        assert go is False
        assert any("insufficient" in r for r in reasons)


# ── Sharpe Ratio ────────────────────────────────────────────────────────────


class TestSharpeRatio:
    def test_positive_consistent_returns(self):
        # 10 trades, each +100. Mean=100, std=0 → Sharpe=0 (no variance)
        m = compute_metrics([100.0] * 10)
        assert m.sharpe_ratio == 0.0

    def test_mixed_returns_annualized(self):
        pnls = [100, -50, 80, -20, 60]
        m = compute_metrics(pnls, annualize=True)
        # mean = 34, std = ~61.48 (sample), raw = 34/61.48 ≈ 0.553
        # annualized = 0.553 * sqrt(252) ≈ 8.78
        assert m.sharpe_ratio > 5.0  # sanity check — positive and annualized

    def test_raw_sharpe_not_annualized(self):
        pnls = [100, -50, 80, -20, 60]
        m = compute_metrics(pnls, annualize=False)
        # raw sharpe = mean_excess / std ≈ 34 / 61.48 ≈ 0.553
        assert 0.4 < m.sharpe_ratio < 0.7

    def test_single_trade_zero_sharpe(self):
        m = compute_metrics([100.0])
        assert m.sharpe_ratio == 0.0  # n<2 → std=0 → sharpe=0

    def test_all_losses_negative_sharpe(self):
        m = compute_metrics([-100, -50, -80], annualize=False)
        assert m.sharpe_ratio < 0


# ── Max Drawdown ────────────────────────────────────────────────────────────


class TestMaxDrawdown:
    def test_no_drawdown_on_all_wins(self):
        m = compute_metrics([100, 100, 100])
        assert m.max_drawdown_pct == 0.0

    def test_single_loss_from_peak(self):
        # cumulative: 100, 200, 100. Peak=200, trough=100, dd=100/200=50%
        m = compute_metrics([100, 100, -100])
        assert abs(m.max_drawdown_pct - 0.50) < 0.01

    def test_recovery_does_not_erase_drawdown(self):
        # cumulative: 100, 200, 100, 200. Peak=200, trough=100, dd=50%
        m = compute_metrics([100, 100, -100, 100])
        assert abs(m.max_drawdown_pct - 0.50) < 0.01

    def test_all_losses(self):
        # cumulative: -100. Peak=0 (never positive), dd=0 (denominator is 0)
        m = compute_metrics([-100, -50, -80])
        # peak never goes above 0, so drawdown is 0 (no peak to draw from)
        assert m.max_drawdown_pct == 0.0

    def test_win_then_full_loss(self):
        # cumulative: 100, 0. Peak=100, trough=0, dd=100/100=100%
        m = compute_metrics([100, -100])
        assert abs(m.max_drawdown_pct - 1.0) < 0.01


# ── Profit Factor ───────────────────────────────────────────────────────────


class TestProfitFactor:
    def test_normal_case(self):
        # gross win = 300, gross loss = 100
        m = compute_metrics([100, 200, -100])
        assert abs(m.profit_factor - 3.0) < 0.01

    def test_no_losses_returns_inf(self):
        m = compute_metrics([100, 200])
        assert m.profit_factor == float("inf")

    def test_no_wins_returns_zero(self):
        m = compute_metrics([-100, -200])
        assert m.profit_factor == 0.0

    def test_breakeven(self):
        m = compute_metrics([100, -100])
        assert abs(m.profit_factor - 1.0) < 0.01


# ── Win Rate ────────────────────────────────────────────────────────────────


class TestWinRate:
    def test_all_wins(self):
        m = compute_metrics([100, 200, 50])
        assert m.win_rate == 1.0

    def test_all_losses(self):
        m = compute_metrics([-100, -200])
        assert m.win_rate == 0.0

    def test_mixed(self):
        m = compute_metrics([100, -50, 80, -20])
        assert abs(m.win_rate - 0.5) < 0.01

    def test_flat_trade_not_a_win(self):
        m = compute_metrics([0.0, 100.0])
        assert abs(m.win_rate - 0.5) < 0.01


# ── Avg Win / Avg Loss ─────────────────────────────────────────────────────


class TestAvgWinLoss:
    def test_normal_case(self):
        # wins: 200, 100 → avg=150. losses: -50, -50 → avg=50. ratio=3.0
        m = compute_metrics([200, 100, -50, -50])
        assert abs(m.avg_win_loss_ratio - 3.0) < 0.01

    def test_no_losses_returns_inf(self):
        m = compute_metrics([100, 200])
        assert m.avg_win_loss_ratio == float("inf")

    def test_no_wins_returns_zero(self):
        m = compute_metrics([-100, -200])
        assert m.avg_win_loss_ratio == 0.0


# ── Aggregate fields ────────────────────────────────────────────────────────


class TestAggregates:
    def test_total_and_mean_pnl(self):
        m = compute_metrics([100, -50, 80])
        assert m.total_pnl == 130.0
        assert abs(m.mean_pnl - 43.33) < 0.01

    def test_largest_win_and_loss(self):
        m = compute_metrics([100, -200, 50, -10])
        assert m.largest_win == 100.0
        assert m.largest_loss == -200.0


# ── Go/No-Go thresholds ────────────────────────────────────────────────────


class TestGoThresholds:
    def _passing_pnls(self) -> list[float]:
        """Build a P&L list that passes all go thresholds."""
        # 60 trades: interleaved 3 wins of +200, then 1 loss of -50
        # win rate = 45/60 = 75% > 45% ✓
        # avg win = 200, avg loss = 50, ratio = 4.0 > 1.5 ✓
        # profit factor = 9000/750 = 12.0 > 1.3 ✓
        # Max early dd: after 3 wins (600), one loss (550) = 8.3% < 15% ✓
        pattern = [200.0, 200.0, 200.0, -50.0]  # repeats 15 times = 60 trades
        return pattern * 15

    def test_all_pass(self):
        m = compute_metrics(self._passing_pnls())
        go, reasons = m.meets_go_thresholds()
        assert go is True, f"expected GO, got failures: {reasons}"
        assert reasons == []

    def test_insufficient_trades(self):
        m = compute_metrics([200.0] * 10 + [-80.0] * 5)
        go, reasons = m.meets_go_thresholds(min_trades=50)
        assert go is False
        assert any("insufficient" in r for r in reasons)

    def test_custom_min_trades(self):
        m = compute_metrics([200.0] * 10 + [-80.0] * 5)
        go, reasons = m.meets_go_thresholds(min_trades=10)
        # With 15 trades ≥ 10, the trade count gate passes
        assert not any("insufficient" in r for r in reasons)

    def test_low_sharpe_fails(self):
        # All flat trades → sharpe = 0
        m = compute_metrics([0.0] * 60)
        go, reasons = m.meets_go_thresholds()
        assert go is False
        assert any("Sharpe" in r for r in reasons)

    def test_high_drawdown_fails(self):
        # Big win then big loss → high drawdown
        pnls = [1000] + [-500] * 5 + [100] * 55
        m = compute_metrics(pnls)
        if m.max_drawdown_pct >= MAX_DRAWDOWN_THRESHOLD:
            go, reasons = m.meets_go_thresholds()
            assert any("drawdown" in r for r in reasons)

    def test_low_win_rate_fails(self):
        # 20 wins, 40 losses → 33% win rate < 45%
        pnls = [300.0] * 20 + [-100.0] * 40
        m = compute_metrics(pnls)
        assert m.win_rate < WIN_RATE_THRESHOLD
        go, reasons = m.meets_go_thresholds()
        assert any("win rate" in r for r in reasons)


# ── format_report ───────────────────────────────────────────────────────────


class TestFormatReport:
    def test_report_contains_key_sections(self):
        m = compute_metrics([100, -50, 80, -20, 60])
        report = m.format_report()
        assert "Performance Metrics" in report
        assert "Sharpe Ratio" in report
        assert "Max Drawdown" in report
        assert "Profit Factor" in report
        assert "Win Rate" in report
        assert "Avg Win/Loss" in report
        assert "Verdict" in report

    def test_passing_report_shows_go(self):
        pnls = [200.0, 200.0, 200.0, -50.0] * 15
        m = compute_metrics(pnls)
        report = m.format_report()
        assert "GO" in report

    def test_failing_report_shows_failures(self):
        m = compute_metrics([])
        report = m.format_report()
        assert "NO-GO" in report
