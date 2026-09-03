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
"""

from __future__ import annotations

import math

import pytest

from reporting.metrics import TRADING_DAYS_PER_YEAR, MetricsSnapshot, compute_metrics


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
