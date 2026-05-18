"""
Unit tests for strategies/health/stats.py.

Coverage:
  - bootstrap_mean_ci: brackets known mu on synthetic samples in ≥95%
    of trials; handles N=0/N=1/N=2 edge cases; respects confidence
    parameter; deterministic with fixed seed.
  - one_sided_t_test_mean_gt_zero: rejects H0 on negative-mean sample;
    does not reject on positive-mean sample; handles zero variance;
    handles N < 2.
  - ema_cross_negative: flags negative cross at known index;
    latest-fast-below-slow correctly identified; degraded result on
    short series.
  - profit_factor / win_rate: convention-matching edge cases.
"""

from __future__ import annotations

import numpy as np
import pytest

from strategies.health.stats import (
    EmaCrossResult,
    TTestResult,
    bootstrap_mean_ci,
    ema_cross_negative,
    one_sided_t_test_mean_gt_zero,
    profit_factor,
    win_rate,
)


# ── Bootstrap CI ───────────────────────────────────────────────────────


class TestBootstrapMeanCI:
    def test_returns_none_on_empty(self):
        assert bootstrap_mean_ci([]) is None

    def test_returns_none_on_single_sample(self):
        assert bootstrap_mean_ci([1.0]) is None

    def test_two_samples_returns_ci(self):
        ci = bootstrap_mean_ci([1.0, 2.0], seed=42)
        assert ci is not None
        lo, hi = ci
        assert lo <= 1.5 <= hi

    def test_brackets_known_mu_at_95_pct(self):
        """Coverage test: 95% CI brackets true mu in ≥95% of trials.

        Sample is N(mu=0.1, sigma=1.0), n=200, 200 trials. The bootstrap
        CI is asymptotically valid; the empirical coverage on this
        sample size should be very close to 95%.
        """
        mu = 0.1
        rng = np.random.default_rng(123)
        successes = 0
        trials = 200
        for trial in range(trials):
            sample = rng.normal(loc=mu, scale=1.0, size=200)
            ci = bootstrap_mean_ci(sample, confidence=0.95, n_resamples=1000, seed=trial)
            assert ci is not None
            lo, hi = ci
            if lo <= mu <= hi:
                successes += 1
        # Expect ~95%; allow ±5pp slack to keep the test non-flaky.
        coverage = successes / trials
        assert 0.90 <= coverage <= 1.0, f"coverage {coverage:.3f} outside [0.90, 1.0]"

    def test_seed_makes_result_deterministic(self):
        sample = [1.0, 2.0, 3.0, 4.0, 5.0]
        a = bootstrap_mean_ci(sample, seed=42, n_resamples=500)
        b = bootstrap_mean_ci(sample, seed=42, n_resamples=500)
        assert a == b

    def test_different_seeds_differ_on_larger_sample(self):
        # On a 5-value sample with 500 resamples, percentile values can
        # collide across seeds — that's a property of small discrete
        # supports, not the RNG. Use a larger continuous sample.
        rng = np.random.default_rng(0)
        sample = rng.normal(size=100).tolist()
        a = bootstrap_mean_ci(sample, seed=1, n_resamples=500)
        b = bootstrap_mean_ci(sample, seed=2, n_resamples=500)
        assert a != b

    def test_invalid_confidence_raises(self):
        with pytest.raises(ValueError):
            bootstrap_mean_ci([1.0, 2.0], confidence=0.0)
        with pytest.raises(ValueError):
            bootstrap_mean_ci([1.0, 2.0], confidence=1.0)

    def test_low_n_resamples_raises(self):
        with pytest.raises(ValueError):
            bootstrap_mean_ci([1.0, 2.0], n_resamples=50)


# ── One-sided t-test ───────────────────────────────────────────────────


class TestOneSidedTTest:
    def test_returns_none_on_empty(self):
        assert one_sided_t_test_mean_gt_zero([]) is None

    def test_returns_none_on_single_sample(self):
        assert one_sided_t_test_mean_gt_zero([1.0]) is None

    def test_rejects_h0_on_clearly_negative_sample(self):
        """Strong negative mean → reject H0: mean ≥ 0."""
        # n=50 from N(mu=-1.0, sigma=1.0) — overwhelming evidence mu < 0.
        rng = np.random.default_rng(42)
        sample = rng.normal(loc=-1.0, scale=1.0, size=50)
        result = one_sided_t_test_mean_gt_zero(sample, alpha=0.05)
        assert result is not None
        assert result.reject_h0 is True
        assert result.sample_mean < 0
        assert result.p_value < 0.05

    def test_does_not_reject_on_positive_sample(self):
        rng = np.random.default_rng(42)
        sample = rng.normal(loc=1.0, scale=1.0, size=50)
        result = one_sided_t_test_mean_gt_zero(sample, alpha=0.05)
        assert result is not None
        assert result.reject_h0 is False
        assert result.sample_mean > 0

    def test_does_not_reject_on_zero_mean(self):
        """Mean=0 case should not reject (H0 is mean ≥ 0)."""
        rng = np.random.default_rng(42)
        sample = rng.normal(loc=0.0, scale=1.0, size=200)
        result = one_sided_t_test_mean_gt_zero(sample, alpha=0.05)
        assert result is not None
        # Very rarely (~5%) we'd reject by chance; with seed=42 we don't.
        assert result.reject_h0 is False

    def test_zero_variance_negative_mean_rejects(self):
        result = one_sided_t_test_mean_gt_zero([-1.0, -1.0, -1.0, -1.0])
        assert result is not None
        assert result.reject_h0 is True
        assert result.p_value == 0.0

    def test_zero_variance_positive_mean_does_not_reject(self):
        result = one_sided_t_test_mean_gt_zero([2.0, 2.0, 2.0, 2.0])
        assert result is not None
        assert result.reject_h0 is False
        assert result.p_value == 1.0

    def test_alpha_validated(self):
        with pytest.raises(ValueError):
            one_sided_t_test_mean_gt_zero([1.0, 2.0], alpha=0.0)
        with pytest.raises(ValueError):
            one_sided_t_test_mean_gt_zero([1.0, 2.0], alpha=1.0)


# ── EMA cross detector ────────────────────────────────────────────────


class TestEmaCrossNegative:
    def test_short_series_degrades_gracefully(self):
        """Series shorter than slow_length → no cross claimable."""
        result = ema_cross_negative([1.0] * 50, fast_length=20, slow_length=100)
        assert result.latest_fast_below_slow is False
        assert result.cross_index is None

    def test_rising_series_no_negative_cross(self):
        """Monotonically rising equity curve → fast > slow → no negative cross."""
        series = list(np.linspace(0.0, 100.0, 200))
        result = ema_cross_negative(series, fast_length=20, slow_length=50)
        assert result.latest_fast_below_slow is False

    def test_falling_series_latest_below(self):
        """Monotonically falling series → fast < slow → latest_fast_below_slow.

        Note: there is no *cross* event here — fast EMA leads slow EMA down
        from the first valid index, so the diff is negative from the start.
        `cross_index` is None when there's no above-to-below transition
        within the visible window. The genuine cross detection is covered
        by `test_rising_then_falling_finds_late_cross`.
        """
        series = list(np.linspace(100.0, 0.0, 200))
        result = ema_cross_negative(series, fast_length=20, slow_length=50)
        assert result.latest_fast_below_slow is True
        assert result.cross_index is None  # never crossed — started below

    def test_rising_then_falling_finds_late_cross(self):
        """Rising for first half then falling: cross happens after the peak."""
        rising = list(np.linspace(0.0, 100.0, 100))
        falling = list(np.linspace(100.0, 0.0, 100))
        series = rising + falling
        result = ema_cross_negative(series, fast_length=20, slow_length=50)
        assert result.latest_fast_below_slow is True
        assert result.cross_index is not None
        # Cross happens after the peak (index 100) — but the fast EMA
        # leads, so it crosses below the slow EMA shortly after the
        # turning point.
        assert result.cross_index > 100

    def test_invalid_lengths_raise(self):
        with pytest.raises(ValueError):
            ema_cross_negative([1.0, 2.0], fast_length=0, slow_length=10)
        with pytest.raises(ValueError):
            ema_cross_negative([1.0, 2.0], fast_length=50, slow_length=50)
        with pytest.raises(ValueError):
            ema_cross_negative([1.0, 2.0], fast_length=100, slow_length=50)


# ── Profit factor + win rate ──────────────────────────────────────────


class TestProfitFactor:
    def test_empty_returns_none(self):
        assert profit_factor([]) is None

    def test_normal_case(self):
        # gross win = 30, gross loss = 10 → PF = 3.0
        assert profit_factor([10, 20, -5, -5]) == 3.0

    def test_no_losses_with_wins_returns_inf(self):
        assert profit_factor([1, 2, 3]) == float("inf")

    def test_no_wins_with_losses_returns_zero(self):
        assert profit_factor([-1, -2, -3]) == 0.0

    def test_all_zeros_returns_zero(self):
        assert profit_factor([0, 0, 0]) == 0.0


class TestWinRate:
    def test_empty_returns_none(self):
        assert win_rate([]) is None

    def test_all_wins(self):
        assert win_rate([1, 2, 3]) == 1.0

    def test_all_losses(self):
        assert win_rate([-1, -2, -3]) == 0.0

    def test_mixed(self):
        # 3 strict positives out of 5 → 0.6
        assert win_rate([1, 2, 3, -1, -2]) == 0.6

    def test_zero_not_counted_as_win(self):
        # Convention: zero (break-even) is not a win.
        assert win_rate([0, 0, 1]) == 1.0 / 3.0


# ── Non-finite input rejection (PR #16 second pass) ───────────────────


class TestNonFiniteInputRejection:
    """Design §1.2: silent NaN propagation through the stats module
    would mask Edge degradation — `t_test(nan) → reject_h0=False`
    because `nan < 0` is False, turning missing data into a false
    non-NEGATIVE signal. Stats functions raise on non-finite input;
    the assessor (11.10d) filters at the data-fetch layer as an
    explicit policy decision."""

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_bootstrap_rejects_non_finite(self, bad_value):
        with pytest.raises(ValueError, match="non-finite"):
            bootstrap_mean_ci([1.0, 2.0, bad_value, 3.0])

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_t_test_rejects_non_finite(self, bad_value):
        with pytest.raises(ValueError, match="non-finite"):
            one_sided_t_test_mean_gt_zero([1.0, 2.0, bad_value, 3.0])

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_ema_cross_rejects_non_finite(self, bad_value):
        with pytest.raises(ValueError, match="non-finite"):
            ema_cross_negative([1.0] * 50 + [bad_value] + [2.0] * 60)

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_profit_factor_rejects_non_finite(self, bad_value):
        with pytest.raises(ValueError, match="non-finite"):
            profit_factor([1.0, -2.0, bad_value])

    @pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), float("-inf")])
    def test_win_rate_rejects_non_finite(self, bad_value):
        with pytest.raises(ValueError, match="non-finite"):
            win_rate([1.0, -1.0, bad_value])

    def test_error_message_names_function_and_count(self):
        """Error message tells the operator which stats call rejected
        and how many bad values were found — for fast triage in logs."""
        try:
            bootstrap_mean_ci([1.0, float("nan"), float("nan"), 2.0])
        except ValueError as exc:
            msg = str(exc)
            assert "bootstrap_mean_ci" in msg
            assert "2" in msg  # n_bad count
            assert "silent-killer" in msg.lower()  # rationale visible to operator
        else:
            pytest.fail("expected ValueError")

    def test_empty_input_still_returns_none_not_error(self):
        """Empty input is not 'non-finite' — it's just empty. Existing
        contract: bootstrap/t-test return None on insufficient sample."""
        assert bootstrap_mean_ci([]) is None
        assert one_sided_t_test_mean_gt_zero([]) is None
        assert profit_factor([]) is None
        assert win_rate([]) is None
