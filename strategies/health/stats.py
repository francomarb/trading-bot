"""
Pure-statistics helpers for the Edge verdict and Health drift checks.

v1 ships the simplest defensible statistics. The rigorous replacements
(PSR / DSR / MinTRL, CUSUM, block bootstrap) are deferred — see
docs/strategy_health_future.md §F1–F3. The functions here are gated by
those follow-ups: if `min_trades` + 3-week persistence + these three
signals prove too crude after 4 weeks of paper, we upgrade.

All RNG state is local (`np.random.default_rng`) — never the global
state — so tests are reproducible by seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np
import pandas as pd

from indicators.technicals import add_ema


# ── Input validation ───────────────────────────────────────────────────


def _to_finite_array(values: Sequence[float], *, name: str) -> np.ndarray:
    """Convert input to float array; reject NaN / Inf.

    Per design §1.2 the monitor exists to catch the silent-killer case
    (clean execution + steady losses). A single NaN in a sample silently
    propagates through bootstrap CI (→ nan bounds) and t-test
    (→ sample_mean=nan, reject_h0=False because `nan < 0` is False) —
    which would turn missing data into a false non-NEGATIVE Edge signal.
    That defeats the whole monitor.

    We raise instead of filtering: the assessor (11.10d) must make the
    NaN-filtering decision explicitly at the data-fetch layer (e.g.
    "drop rows where r_multiple IS NULL"), as a code-visible policy
    rather than a silent stats-module behavior.
    """
    arr = np.asarray(list(values), dtype=float)
    if arr.size == 0:
        return arr
    if not np.all(np.isfinite(arr)):
        n_bad = int(np.sum(~np.isfinite(arr)))
        raise ValueError(
            f"{name}: input contains {n_bad} non-finite value(s) (NaN/Inf). "
            f"Filter at the data-fetch layer before passing to the stats "
            f"module — silent NaN propagation would mask Edge degradation "
            f"(the silent-killer failure mode)."
        )
    return arr


# ── Bootstrap CI ───────────────────────────────────────────────────────


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    confidence: float = 0.95,
    n_resamples: int = 2000,
    seed: int | None = None,
) -> tuple[float, float] | None:
    """Iid bootstrap confidence interval on the sample mean.

    Returns (lo, hi) at the given confidence level, or None when the
    sample is too small to bootstrap meaningfully (N < 2).

    Notes:
      - iid (not stationary block) bootstrap. Per design §F3, block
        bootstrap is a follow-up — we accept the autocorrelation
        underestimate in v1 to ship a simpler module. CI widths that
        look obviously wrong on real paper data is the trigger to
        upgrade.
      - `confidence` is two-sided (e.g. 0.95 → 2.5%/97.5% percentiles).
    """
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0,1), got {confidence}")
    if n_resamples < 100:
        raise ValueError(f"n_resamples must be >= 100, got {n_resamples}")
    arr = _to_finite_array(values, name="bootstrap_mean_ci")
    n = arr.size
    if n < 2:
        return None
    rng = np.random.default_rng(seed)
    # Vectorized resample: (n_resamples, n) integer indices in [0, n)
    idx = rng.integers(0, n, size=(n_resamples, n))
    means = arr[idx].mean(axis=1)
    alpha = (1.0 - confidence) / 2.0
    lo = float(np.quantile(means, alpha))
    hi = float(np.quantile(means, 1.0 - alpha))
    return (lo, hi)


# ── One-sided t-test ───────────────────────────────────────────────────


@dataclass(frozen=True)
class TTestResult:
    """Result of `one_sided_t_test_mean_gt_zero`.

    `reject_h0` is True when `p_value <= alpha` and the sample mean is
    actually negative — i.e. we reject "mean ≥ 0" at the given alpha.
    A True `reject_h0` corresponds to a NEGATIVE signal in §9 verdict
    logic.
    """

    n: int
    sample_mean: float
    t_stat: float
    p_value: float
    reject_h0: bool


def one_sided_t_test_mean_gt_zero(
    values: Sequence[float],
    *,
    alpha: float = 0.05,
) -> TTestResult | None:
    """One-sided Student's t-test of `H0: mean(values) >= 0`.

    Rejects (returns `reject_h0=True`) when there is evidence the true
    mean is below zero. Used by the EdgeAssessor as one of the three
    NEGATIVE-verdict signals (design §9).

    Returns None when N < 2 (variance undefined).

    Implementation uses scipy.stats.t.sf for the survival function so
    no external dependency beyond what's already in the project. If
    scipy is unavailable we fall back to a normal approximation, which
    is acceptable for our typical sample sizes (≥ 25-50).
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0,1), got {alpha}")
    arr = _to_finite_array(values, name="one_sided_t_test_mean_gt_zero")
    n = int(arr.size)
    if n < 2:
        return None
    mean = float(arr.mean())
    # Sample std (ddof=1) — unbiased; matches scipy.stats.ttest defaults.
    std = float(arr.std(ddof=1))
    if std == 0.0:
        # Degenerate: no variance. If mean < 0, H0 is rejected
        # mathematically (any negative sample with zero variance
        # definitively rules out mean ≥ 0).
        return TTestResult(
            n=n,
            sample_mean=mean,
            t_stat=float("-inf") if mean < 0 else 0.0,
            p_value=0.0 if mean < 0 else 1.0,
            reject_h0=mean < 0,
        )
    se = std / (n**0.5)
    t_stat = mean / se  # under H0: mean=0
    # One-sided p-value: P(T < t_stat | H0) with df = n-1
    try:
        from scipy.stats import t as _t

        p_value = float(_t.cdf(t_stat, df=n - 1))
    except ImportError:  # pragma: no cover - scipy is a hard project dep
        # Normal approximation fallback (df → ∞)
        from math import erf, sqrt

        p_value = 0.5 * (1.0 + erf(t_stat / sqrt(2.0)))
    return TTestResult(
        n=n,
        sample_mean=mean,
        t_stat=float(t_stat),
        p_value=float(p_value),
        reject_h0=bool(p_value <= alpha and mean < 0),
    )


# ── EMA crossover on cumulative-R equity curve ────────────────────────


@dataclass(frozen=True)
class EmaCrossResult:
    """Result of `ema_cross_negative`.

    `latest_fast_below_slow` is True iff the most recent observation has
    EMA(fast) < EMA(slow). This is the value the EdgeAssessor reads as
    one of the three NEGATIVE-verdict signals (design §9).

    `cross_index` is the last index at which the fast crossed below the
    slow (or None if no such cross exists in the window). Useful for
    debugging / report context.
    """

    latest_fast_below_slow: bool
    cross_index: int | None
    fast_value: float | None
    slow_value: float | None


def ema_cross_negative(
    cumulative_r: Sequence[float],
    *,
    fast_length: int = 50,
    slow_length: int = 100,
) -> EmaCrossResult:
    """EMA(fast)/EMA(slow) crossover detector on a cumulative-R equity curve.

    Returns whether the latest observation has fast below slow (the
    design §9 signal) plus the last downward-cross index for context.

    Per design §9: cumulative-R (not cumulative dollars) so this is
    sizing-invariant. v1 defaults are 50/100 — slower than the classic
    20/50 to filter routine drawdowns on our low-trade-rate strategies.

    Returns degraded result (`latest_fast_below_slow=False`,
    `cross_index=None`) when the series is too short to compute both
    EMAs — there is not enough data yet to claim a downward cross.
    """
    if fast_length < 1 or slow_length < 1:
        raise ValueError("EMA lengths must be ≥ 1")
    if fast_length >= slow_length:
        raise ValueError(f"fast_length ({fast_length}) must be < slow_length ({slow_length})")
    arr = _to_finite_array(cumulative_r, name="ema_cross_negative")
    n = arr.size
    if n < slow_length:
        return EmaCrossResult(False, None, None, None)
    # Reuse the project's `add_ema` over a synthetic OHLCV-shaped DataFrame
    # (it only reads the `close` column by default). Keeps a single EMA
    # implementation across the codebase.
    df = pd.DataFrame({"close": arr})
    df = add_ema(df, length=fast_length).pipe(add_ema, length=slow_length)
    fast = df[f"ema_{fast_length}"]
    slow = df[f"ema_{slow_length}"]
    # Last value cross check
    fast_last = float(fast.iloc[-1])
    slow_last = float(slow.iloc[-1])
    latest_below = bool(fast_last < slow_last)
    # Find the most recent index where fast crossed from above to below
    # (transition: prev fast >= prev slow AND curr fast < curr slow)
    diff = fast - slow
    # Skip NaN warm-up by aligning on dropna
    valid = diff.dropna()
    cross_index: int | None = None
    if len(valid) >= 2:
        prev = valid.shift(1)
        crossed_down = (prev >= 0) & (valid < 0)
        crossed = crossed_down[crossed_down]
        if not crossed.empty:
            cross_index = int(crossed.index[-1])
    return EmaCrossResult(latest_below, cross_index, fast_last, slow_last)


# ── Profit factor / win rate ──────────────────────────────────────────


def profit_factor(values: Sequence[float]) -> float | None:
    """Profit factor = sum(positive) / |sum(negative)|.

    Returns None on empty input, +inf when there are wins but no losses,
    0.0 when all values are non-positive. Matches the convention used
    in `reporting/metrics.py` and `reporting/pnl.py:StrategyStats`.

    Raises ValueError on non-finite input (see _to_finite_array docstring).
    """
    arr = _to_finite_array(values, name="profit_factor")
    if arr.size == 0:
        return None
    gross_win = float(arr[arr > 0].sum())
    gross_loss = float(-arr[arr < 0].sum())
    if gross_loss == 0.0:
        return float("inf") if gross_win > 0 else 0.0
    return gross_win / gross_loss


def win_rate(values: Sequence[float]) -> float | None:
    """Fraction of strictly positive values. Returns None on empty input.

    Zeroes (break-even trades) are not counted as wins — matches the
    convention used in `reporting/metrics.py`.

    Raises ValueError on non-finite input (see _to_finite_array docstring).
    """
    arr = _to_finite_array(values, name="win_rate")
    if arr.size == 0:
        return None
    return float((arr > 0).sum() / arr.size)
