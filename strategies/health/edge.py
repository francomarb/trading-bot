"""
EdgeAssessor — the verdict layer.

Reads closed-trade R-multiples + dollar P&L for one strategy over a
time window, computes the three-signal Edge verdict (R-expectancy CI
vs envelope, one-sided t-test on R-expectancy, EMA50/100 crossover on
cumulative-R equity curve), applies the per-strategy sufficiency floor
and 3-week persistence requirement, then returns an EdgeReport and
the updated PersistenceState.

Per design §5 / §9 / §1.2 invariant: this module **computes verdicts,
never takes action**. The reviewer (11.10e) combines the EdgeReport
with the HealthReport to produce a Recommendation; the operator
decides whether to act on the recommendation. Persistence state
flows through this module because the 3-week NEGATIVE-must-persist
rule lives here — that's part of the verdict, not the recommendation.

Non-finite r_multiple values are filtered at the DB read boundary (per
the contract stated in health.stats: assessor filters at the data-fetch
layer so silent NaN propagation can't mask Edge degradation — the
silent-killer failure mode).

R-multiple is primary; dollar metrics are secondary context (sizing-
invariance — see design §5.1). Both are reported.

BELOW_BENCHMARK detection requires a benchmark_return input + a
nominal sleeve dollars proxy so strategy_return is a fraction
comparable with the benchmark's BH percentage. Both inputs are
optional — when absent, the verdict can be POSITIVE/NEGATIVE/
UNDETERMINED but never BELOW_BENCHMARK.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import date
from typing import Sequence

from strategies.health.envelope import StrategyEnvelope
from strategies.health.persistence import PersistenceState
from strategies.health.reports import (
    EdgeReport,
    EdgeVerdict,
    Sufficiency,
    sufficiency_for,
)
from strategies.health.stats import (
    bootstrap_mean_ci,
    ema_cross_negative,
    one_sided_t_test_mean_gt_zero,
    profit_factor as compute_profit_factor,
    win_rate as compute_win_rate,
)


# Three-week persistence requirement per design §9. Locked here as a
# module constant rather than a config knob — adjusting this is a
# design change, not an operational tuning.
NEGATIVE_PERSISTENCE_WEEKS_REQUIRED = 3


# ── Inputs / outputs ──────────────────────────────────────────────────


@dataclass(frozen=True)
class EdgeInputs:
    """All inputs the EdgeAssessor needs to produce one strategy's
    EdgeReport over one period window.

    `envelope` is optional — strategies without a built envelope (e.g.
    spy_options_reversion / credit_spread stubs from 11.10b) get an
    UNDETERMINED verdict because we can't compare R-expectancy to a
    reference band. The assessor handles None gracefully.

    `benchmark_return` and `nominal_sleeve_dollars` are optional too —
    BELOW_BENCHMARK detection is conditional on both being present.
    """

    strategy_name: str
    period_start: date
    period_end: date  # exclusive
    envelope: StrategyEnvelope | None
    conn: sqlite3.Connection
    persistence_state: PersistenceState
    min_trades_floor: int
    benchmark_return: float | None = None
    nominal_sleeve_dollars: float | None = None


@dataclass(frozen=True)
class _ClosedTrades:
    """Internal: closed-trade R-multiples + dollar P&Ls for one window."""

    r_multiples: tuple[float, ...]  # finite-only, in trade-close order
    pnls: tuple[float, ...]
    total_pnl: float


# ── DB read ───────────────────────────────────────────────────────────


def _read_closed_trades(
    conn: sqlite3.Connection,
    *,
    strategy_name: str,
    period_start: date,
    period_end: date,
) -> _ClosedTrades:
    """Return finite-only R-multiples + P&Ls for `strategy` in
    `[period_start, period_end)`, in trade-close order.

    Filters match the existing `read_strategy_realized_pnl_summary`
    convention: closes are `side='sell' OR position_type='spread'`
    rows with `realized_pnl IS NOT NULL`. Status must be `filled` or
    `partial`. Trade timestamps below day-resolution are normalized
    via `timestamp BETWEEN ? AND ?` against ISO date prefixes.

    Per the health.stats contract, non-finite r_multiple / realized_pnl
    values are filtered HERE at the data-fetch boundary so the stats
    module can't silently turn NaN into a false non-NEGATIVE signal.
    """
    # Use date prefix comparison so timestamps like '2026-05-18T16:30Z'
    # still get matched. period_end is exclusive (matches lifecycle.py).
    start_iso = period_start.isoformat()
    end_iso = period_end.isoformat()
    cursor = conn.execute(
        "SELECT r_multiple, realized_pnl "
        "FROM trades "
        "WHERE strategy = ? "
        "AND (side = 'sell' OR position_type = 'spread') "
        "AND status IN ('filled', 'partial') "
        "AND realized_pnl IS NOT NULL "
        "AND timestamp >= ? "
        "AND timestamp < ? "
        "ORDER BY id ASC",
        (strategy_name, start_iso, end_iso),
    )
    r_values: list[float] = []
    pnl_values: list[float] = []
    total = 0.0
    for r_mult, pnl in cursor.fetchall():
        pnl_f = float(pnl) if pnl is not None else None
        if pnl_f is None or not math.isfinite(pnl_f):
            continue
        pnl_values.append(pnl_f)
        total += pnl_f
        # r_multiple can legitimately be NULL on pre-11.27 rows or on
        # trades where initial_risk_dollars was zero. Skip those for
        # R-based metrics but keep their PnL for dollar-based metrics.
        if r_mult is None:
            continue
        r_f = float(r_mult)
        if math.isfinite(r_f):
            r_values.append(r_f)
    return _ClosedTrades(
        r_multiples=tuple(r_values),
        pnls=tuple(pnl_values),
        total_pnl=total,
    )


# ── Verdict computation ──────────────────────────────────────────────


@dataclass(frozen=True)
class _VerdictDecision:
    """Internal — verdict + which signals tripped + driving reasons."""

    verdict: EdgeVerdict
    failure_reasons: tuple[str, ...]
    # Tripped-signal flags for diagnostics (surfaced in failure_reasons
    # but kept structured for tests that want to assert specific
    # signals agreed/disagreed).
    ci_negative_signal: bool
    t_test_signal: bool
    ema_cross_signal: bool


def _compute_verdict(
    r_values: Sequence[float],
    r_expectancy_ci: tuple[float, float] | None,
    pnl_values: Sequence[float],
    envelope: StrategyEnvelope | None,
    sufficiency: Sufficiency,
    benchmark_return: float | None,
    strategy_return: float | None,
    persistence_negative_weeks_after_update: int,
) -> _VerdictDecision:
    """Apply the three-signal NEGATIVE rule + sufficiency + persistence.

    Per design §9:
      - NEGATIVE requires ALL three signals to agree AND
        sufficiency == CONCLUSIVE AND
        negative_weeks (after this assessment) >= 3.
      - POSITIVE requires R-expectancy CI > 0 AND realized R-expectancy
        within envelope's R-expectancy CI band AND sufficiency
        CONCLUSIVE.
      - BELOW_BENCHMARK requires POSITIVE conditions AND strategy_return
        < benchmark_return + CONCLUSIVE.
      - Otherwise UNDETERMINED.

    Insufficient sample → UNDETERMINED regardless of signals. The
    silent-killer alarm refuses to fire below the floor — that's the
    discipline that protects edge during normal drawdowns.
    """
    reasons: list[str] = []

    # ── Signal 1: R-expectancy CI vs envelope ─────────────────────
    ci_signal = False
    env_r_ci = envelope.r_expectancy_ci_95 if envelope is not None else None
    if r_expectancy_ci is None:
        reasons.append("R-expectancy CI unavailable (sample < 2)")
    else:
        ci_lo, ci_hi = r_expectancy_ci
        # NEGATIVE signal: CI excludes zero on the negative side AND
        # lies below the envelope band's lower bound (when known).
        excludes_zero_neg = ci_hi < 0.0
        below_envelope = (
            env_r_ci is not None and ci_hi < env_r_ci[0]
        )
        if env_r_ci is None:
            # No envelope to compare against — only the excludes-zero
            # half of the signal can fire. Conservative: don't count
            # this as a NEGATIVE signal without envelope context, but
            # do log it as a failure reason if the upper bound is
            # below zero.
            if excludes_zero_neg:
                reasons.append(
                    f"R-expectancy CI {r_expectancy_ci} excludes zero on the "
                    f"negative side (no envelope to compare against)."
                )
            # ci_signal stays False — design §9 requires envelope context
        else:
            ci_signal = excludes_zero_neg and below_envelope
            if ci_signal:
                reasons.append(
                    f"R-expectancy CI {r_expectancy_ci} is below zero AND "
                    f"below envelope band {env_r_ci}."
                )

    # ── Signal 2: one-sided t-test on R-expectancy ────────────────
    t_signal = False
    if len(r_values) < 2:
        reasons.append("t-test unavailable (sample < 2)")
    else:
        t_result = one_sided_t_test_mean_gt_zero(r_values, alpha=0.05)
        if t_result is not None and t_result.reject_h0:
            t_signal = True
            reasons.append(
                f"one-sided t-test rejected H0 (mean R >= 0) at "
                f"p={t_result.p_value:.4f}, mean R={t_result.sample_mean:.4f}."
            )

    # ── Signal 3: EMA50/100 cross on cumulative-R curve ──────────
    ema_signal = False
    if len(r_values) < 100:
        # Cross detector needs at least slow_length (100) bars to
        # claim a downward cross. Insufficient data → no signal.
        # Don't add a reason — sample-size is already surfaced.
        pass
    else:
        cumulative_r = _cumulative(r_values)
        ema_result = ema_cross_negative(
            cumulative_r, fast_length=50, slow_length=100,
        )
        if ema_result.latest_fast_below_slow:
            ema_signal = True
            reasons.append(
                f"cumulative-R EMA50/100 cross: fast "
                f"({ema_result.fast_value:.4f}) < slow "
                f"({ema_result.slow_value:.4f})."
            )

    # ── Verdict assembly ──────────────────────────────────────────
    if sufficiency != Sufficiency.CONCLUSIVE:
        return _VerdictDecision(
            verdict=EdgeVerdict.UNDETERMINED,
            failure_reasons=tuple(reasons),
            ci_negative_signal=ci_signal,
            t_test_signal=t_signal,
            ema_cross_signal=ema_signal,
        )

    all_signals_agree = ci_signal and t_signal and ema_signal
    persistence_met = (
        persistence_negative_weeks_after_update
        >= NEGATIVE_PERSISTENCE_WEEKS_REQUIRED
    )

    if all_signals_agree and persistence_met:
        return _VerdictDecision(
            verdict=EdgeVerdict.NEGATIVE,
            failure_reasons=tuple(reasons),
            ci_negative_signal=ci_signal,
            t_test_signal=t_signal,
            ema_cross_signal=ema_signal,
        )

    # POSITIVE conditions (R-expectancy CI > 0 AND realized within band)
    positive_candidate = False
    if r_expectancy_ci is not None and env_r_ci is not None:
        ci_lo, ci_hi = r_expectancy_ci
        env_lo, env_hi = env_r_ci
        ci_above_zero = ci_lo > 0.0
        within_band = env_lo <= sum(r_values) / len(r_values) <= env_hi
        positive_candidate = ci_above_zero and within_band

    if positive_candidate:
        # BELOW_BENCHMARK overrides POSITIVE when we have benchmark data
        # and the strategy underperformed.
        if (
            benchmark_return is not None
            and strategy_return is not None
            and strategy_return < benchmark_return
        ):
            reasons.append(
                f"strategy_return {strategy_return:.2%} < benchmark "
                f"{benchmark_return:.2%}"
            )
            return _VerdictDecision(
                verdict=EdgeVerdict.BELOW_BENCHMARK,
                failure_reasons=tuple(reasons),
                ci_negative_signal=ci_signal,
                t_test_signal=t_signal,
                ema_cross_signal=ema_signal,
            )
        return _VerdictDecision(
            verdict=EdgeVerdict.POSITIVE,
            failure_reasons=(),  # POSITIVE has no failures
            ci_negative_signal=ci_signal,
            t_test_signal=t_signal,
            ema_cross_signal=ema_signal,
        )

    # Conclusive sample but doesn't meet POSITIVE or NEGATIVE criteria —
    # somewhere between. UNDETERMINED is the right honest answer.
    return _VerdictDecision(
        verdict=EdgeVerdict.UNDETERMINED,
        failure_reasons=tuple(reasons),
        ci_negative_signal=ci_signal,
        t_test_signal=t_signal,
        ema_cross_signal=ema_signal,
    )


def _cumulative(values: Sequence[float]) -> list[float]:
    """Running sum — for the cumulative-R equity curve."""
    out: list[float] = []
    total = 0.0
    for v in values:
        total += v
        out.append(total)
    return out


# ── Sleeve utilization ───────────────────────────────────────────────


def _sleeve_utilization(
    pnls: Sequence[float],
    nominal_sleeve_dollars: float | None,
) -> float | None:
    """Approximate sleeve utilization from trade activity.

    True utilization requires per-bar deployed-capital tracking, which
    isn't available without the engine state snapshot — that's a v1
    approximation. For now, return the trade-volume ratio (sum of
    |PnL| / nominal sleeve), which approximates "how active was this
    sleeve over the window."

    Returns None when sleeve dollars unknown — the assessor degrades
    gracefully to "no utilization data."
    """
    if nominal_sleeve_dollars is None or nominal_sleeve_dollars <= 0:
        return None
    total_activity = sum(abs(p) for p in pnls)
    return total_activity / nominal_sleeve_dollars


# ── EdgeAssessor ──────────────────────────────────────────────────────


class EdgeAssessor:
    """The verdict layer.

    Stateless — all per-strategy state lives in PersistenceState which
    flows in and out of `assess()`. Multiple strategies can share one
    EdgeAssessor instance; the caller (reviewer in 11.10e) typically
    iterates strategies and threads the persistence file through.
    """

    def assess(
        self, inputs: EdgeInputs
    ) -> tuple[EdgeReport, PersistenceState]:
        """Compute one strategy's EdgeReport for the window and return
        the updated PersistenceState.

        Returns (report, new_persistence_state). Caller must save the
        new state back to disk; this method is pure (no I/O writes).
        """
        trades = _read_closed_trades(
            inputs.conn,
            strategy_name=inputs.strategy_name,
            period_start=inputs.period_start,
            period_end=inputs.period_end,
        )

        # Sufficiency uses R-multiple count (the primary verdict input).
        # Sample size for verdict-quality is the R-count; dollar metrics
        # are reported but don't drive the verdict.
        n_r = len(trades.r_multiples)
        sufficiency = sufficiency_for(n_r, inputs.min_trades_floor)

        # ── Metrics computation ──────────────────────────────────
        r_expectancy = (
            sum(trades.r_multiples) / n_r if n_r > 0 else None
        )
        r_expectancy_ci = (
            bootstrap_mean_ci(trades.r_multiples, seed=0)
            if n_r >= 2 else None
        )
        # Strategy return: realized PnL over period / nominal sleeve.
        # Required for BELOW_BENCHMARK detection (alpha = strategy - bench).
        strategy_return: float | None = None
        if (
            inputs.nominal_sleeve_dollars is not None
            and inputs.nominal_sleeve_dollars > 0
        ):
            strategy_return = trades.total_pnl / inputs.nominal_sleeve_dollars
        alpha: float | None = None
        if strategy_return is not None and inputs.benchmark_return is not None:
            alpha = strategy_return - inputs.benchmark_return

        # Dollar metrics (secondary context).
        n_pnl = len(trades.pnls)
        expectancy_dollars = (
            trades.total_pnl / n_pnl if n_pnl > 0 else None
        )
        expectancy_dollars_ci = (
            bootstrap_mean_ci(trades.pnls, seed=0) if n_pnl >= 2 else None
        )
        pf = compute_profit_factor(trades.pnls)
        wr = compute_win_rate(trades.pnls)

        envelope_r_ci = (
            inputs.envelope.r_expectancy_ci_95
            if inputs.envelope is not None else None
        )

        # ── Persistence update ─────────────────────────────────────
        # PersistenceState tracks **"weeks where signals tripped AND
        # sample was CONCLUSIVE"** — not "weeks where verdict was
        # NEGATIVE." This distinction matters: the 3-week gate is what
        # transforms tripped-signals into NEGATIVE verdicts. If the
        # state counted only issued-NEGATIVE weeks, the verdict would
        # always be UNDETERMINED below week 3 (which resets the
        # counter), and the alarm could never fire.
        #
        # Operator-facing semantics: PersistenceState.last_verdict says
        # "NEGATIVE" iff the signals tripped on that check; the
        # EdgeReport.verdict reflects whether the alarm actually fired
        # (which requires three such weeks). The two can disagree —
        # that's the design intent.
        signals_tripped = (
            sufficiency == Sufficiency.CONCLUSIVE
            and self._signals_agree_provisionally(
                trades.r_multiples, r_expectancy_ci, inputs.envelope,
            )
        )
        projected_neg_weeks = (
            inputs.persistence_state.negative_weeks + 1
            if signals_tripped else 0
        )

        decision = _compute_verdict(
            r_values=trades.r_multiples,
            r_expectancy_ci=r_expectancy_ci,
            pnl_values=trades.pnls,
            envelope=inputs.envelope,
            sufficiency=sufficiency,
            benchmark_return=inputs.benchmark_return,
            strategy_return=strategy_return,
            persistence_negative_weeks_after_update=projected_neg_weeks,
        )

        # Apply to PersistenceState: "NEGATIVE" if signals tripped this
        # week (the eligibility flag), else the final verdict (which
        # will reset the counter). This keeps the state machine's
        # `last_verdict` legible — "NEGATIVE" means "this week was
        # signal-eligible for the alarm," not necessarily "alarm fired."
        persistence_input = (
            "NEGATIVE" if signals_tripped else decision.verdict.value
        )
        new_state = inputs.persistence_state.apply_verdict(
            persistence_input, inputs.period_end,
        )

        # Sleeve utilization (approximate; design §5.4).
        sleeve_util = _sleeve_utilization(
            trades.pnls, inputs.nominal_sleeve_dollars,
        )

        report = EdgeReport(
            strategy=inputs.strategy_name,
            period_start=inputs.period_start,
            period_end=inputs.period_end,
            verdict=decision.verdict,
            sufficiency=sufficiency,
            trade_count=n_r,
            min_trades_for_verdict=inputs.min_trades_floor,
            r_expectancy=r_expectancy,
            r_expectancy_ci_95=r_expectancy_ci,
            envelope_r_expectancy_ci_95=envelope_r_ci,
            realized_pnl=trades.total_pnl,
            expectancy_dollars=expectancy_dollars,
            expectancy_dollars_ci_95=expectancy_dollars_ci,
            profit_factor=pf,
            win_rate=wr,
            sleeve_utilization=sleeve_util,
            benchmark_return=inputs.benchmark_return,
            strategy_return=strategy_return,
            alpha=alpha,
            negative_persistence_weeks=new_state.negative_weeks,
            failure_reasons=decision.failure_reasons,
        )
        return report, new_state

    @staticmethod
    def _signals_agree_provisionally(
        r_values: Sequence[float],
        r_expectancy_ci: tuple[float, float] | None,
        envelope: StrategyEnvelope | None,
    ) -> bool:
        """Quick all-three-signals check for the persistence projection.

        This is the same logic as _compute_verdict's signal flags but
        without the full decision wrapping — used to project what
        `negative_weeks` would become if we DID issue NEGATIVE this
        week. The actual verdict still requires sufficiency CONCLUSIVE
        AND projected_neg_weeks >= 3.
        """
        env_r_ci = envelope.r_expectancy_ci_95 if envelope is not None else None
        if r_expectancy_ci is None or env_r_ci is None:
            return False
        # Signal 1
        ci_signal = r_expectancy_ci[1] < 0.0 and r_expectancy_ci[1] < env_r_ci[0]
        if not ci_signal:
            return False
        # Signal 2
        if len(r_values) < 2:
            return False
        t_result = one_sided_t_test_mean_gt_zero(r_values, alpha=0.05)
        if t_result is None or not t_result.reject_h0:
            return False
        # Signal 3
        if len(r_values) < 100:
            return False
        cumulative = _cumulative(r_values)
        ema_result = ema_cross_negative(cumulative)
        return ema_result.latest_fast_below_slow
