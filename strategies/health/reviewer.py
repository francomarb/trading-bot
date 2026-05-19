"""
Reviewer — the orchestration + reporting layer.

Loads envelopes (11.10b), wires inputs into EdgeAssessor + HealthAssessor
(11.10d), threads PersistenceState (11.10c) across runs, computes the
operator-facing Recommendation per design §11.5 decision matrix,
renders the weekly/monthly markdown report, and dispatches the
appropriate alerts.

Per design §1.2 invariant (bot informs, operator decides): this module
ONLY writes a report file and dispatches alerts. It NEVER modifies
sleeve allocations, halts strategies, or touches any runtime trading
behavior. The persistence state file IS written here (that's the
3-week NEGATIVE tracking state) but that's a read-only input to the
next assessor run; nothing in the trading loop reads it.

Single Telegram digest per run (design §11) — one summary message
listing all per-strategy verdicts + recommendations, not N per-strategy
messages. Avoids rate-limits and operator alarm fatigue.

The report markdown style mirrors `reporting/pnl.py:write_daily_report`
so a future operator skimming `data/health_reports/` sees the same
voice as the existing P&L reports.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Sequence

from loguru import logger

from config import settings
from reporting.alerts import AlertDispatcher
from strategies.health.assessor import HealthAssessor, HealthInputs, load_engine_state
from strategies.health.benchmarks import (
    benchmark_symbols_for,
    equal_weight_bh_return,
)
from strategies.health.edge import EdgeAssessor, EdgeInputs
from strategies.health.envelope import StrategyEnvelope, envelope_path
from strategies.health.persistence import (
    HealthStateFile,
    PersistenceState,
    default_state_path,
    load_state,
    save_state,
)
from strategies.health.reports import (
    AssessmentBundle,
    EdgeReport,
    EdgeVerdict,
    HealthReport,
    HealthStatus,
    Recommendation,
    Sufficiency,
)


# Reports schema version mirrors the envelope's and the state file's —
# bumped if the markdown structure (front-matter, table layout) changes
# in a non-additive way. Used in YAML front-matter so a future parser
# can detect older reports.
REPORT_SCHEMA_VERSION = 1


# Recommendation threshold for "Health DEGRADED + low sleeve utilization
# → reduce size" per design §11.5. v1 picks 20% — tuneable in 11.10h.
LOW_SLEEVE_UTILIZATION_THRESHOLD = 0.20


# ── Recommendation mapping (design §11.5 decision matrix) ─────────────


def recommend(edge: EdgeReport, health: HealthReport) -> Recommendation:
    """Map an (EdgeReport, HealthReport) pair to the operator-facing
    recommendation per design §11.5.

    Priority order — first match wins:
      1. Edge NEGATIVE+CONCLUSIVE → pause and investigate (silent killer)
      2. Edge BELOW_BENCHMARK+CONCLUSIVE → reduce size
      3. Edge trending NEGATIVE (persistence ≥ 2 weeks, not yet alarm)
         → reduce size (early warning)
      4. Health DEGRADED + low sleeve utilization → reduce size
         (starved + struggling — candidate for reallocation by operator)
      5. Edge POSITIVE + Health HEALTHY → continue
      6. Default → continue and monitor (Health is forensic; do not
         throttle a profitable strategy with messy execution)

    Per the v1 invariant (design §1.2): this is text in a report;
    nothing in the bot's runtime reads it. The operator decides.
    `disable pending review` is owned by 11.11, never returned here.
    """
    # 1. Silent killer takes top priority.
    if (
        edge.verdict == EdgeVerdict.NEGATIVE
        and edge.sufficiency == Sufficiency.CONCLUSIVE
    ):
        return Recommendation.PAUSE_AND_INVESTIGATE

    # 2. Below-benchmark with conclusive sample.
    if (
        edge.verdict == EdgeVerdict.BELOW_BENCHMARK
        and edge.sufficiency == Sufficiency.CONCLUSIVE
    ):
        return Recommendation.REDUCE_SIZE

    # 3. Early warning: two consecutive weeks of negative signals (but
    # not yet 3 — alarm hasn't fired). Reduce size before the silent-
    # killer trips so capital exposure shrinks ahead of confirmation.
    if (
        edge.negative_persistence_weeks >= 2
        and edge.sufficiency != Sufficiency.INSUFFICIENT
    ):
        return Recommendation.REDUCE_SIZE

    # 4. Health DEGRADED + low sleeve utilization — the strategy is
    # struggling AND not deploying its allocated capital. Operator
    # candidate for reallocation.
    if (
        health.overall_status == HealthStatus.DEGRADED
        and edge.sleeve_utilization is not None
        and edge.sleeve_utilization < LOW_SLEEVE_UTILIZATION_THRESHOLD
    ):
        return Recommendation.REDUCE_SIZE

    # 5. Best case: confirmed profitable + clean operation.
    if (
        edge.verdict == EdgeVerdict.POSITIVE
        and health.overall_status == HealthStatus.HEALTHY
    ):
        return Recommendation.CONTINUE

    # 6. Everything else — including: INSUFFICIENT/INDICATIVE sample,
    # POSITIVE+Health WATCH/DEGRADED/BROKEN (profitable despite messy
    # execution), UNDETERMINED. Continue with active observation.
    return Recommendation.CONTINUE_AND_MONITOR


# ── Assess one strategy end-to-end ────────────────────────────────────


@dataclass(frozen=True)
class ReviewWindow:
    """Period bounds + classification (weekly/monthly/yearly)."""

    period_start: date
    period_end: date
    period_type: str  # "weekly" | "monthly" | "yearly"


def _nominal_sleeve_dollars(
    strategy_name: str, initial_cash: float = 100_000.0,
) -> float | None:
    """Look up the strategy's target sleeve allocation × initial_cash.

    Matches the `risk_unit_dollars` calculation pattern in
    scripts/build_envelopes.py so envelope-R and live-R use comparable
    sizing scales. Returns None when STRATEGY_ALLOCATIONS lacks the
    strategy entry — strategy_return + alpha + BELOW_BENCHMARK
    detection all degrade gracefully to None in that case.
    """
    alloc = getattr(settings, "STRATEGY_ALLOCATIONS", {}).get(strategy_name)
    if not alloc:
        return None
    target_pct = alloc.get("target_pct")
    if target_pct is None or target_pct <= 0:
        return None
    return initial_cash * target_pct


def _load_envelope_for(strategy_name: str) -> StrategyEnvelope | None:
    """Read the strategy's envelope JSON, returning None on missing /
    malformed file. EdgeAssessor degrades to UNDETERMINED without an
    envelope (no R-band to compare against)."""
    path = envelope_path(strategy_name)
    if not path.exists():
        logger.info(
            f"{strategy_name}: no envelope at {path} — verdict will be "
            f"UNDETERMINED. Run scripts/build_envelopes.py to create."
        )
        return None
    try:
        return StrategyEnvelope.read(path)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"{strategy_name}: envelope at {path} could not be parsed — "
            f"{exc}. Treating as missing."
        )
        return None


def assess_strategy(
    strategy_name: str,
    window: ReviewWindow,
    *,
    conn: sqlite3.Connection,
    persistence_state: PersistenceState,
    engine_state: dict,
    benchmark_return: float | None = None,
) -> tuple[AssessmentBundle, PersistenceState]:
    """Run both assessors for one strategy and bundle with recommendation.

    Returns (bundle, new_persistence_state) — caller saves the updated
    persistence state back to disk after iterating all strategies.

    `benchmark_return` is computed by the orchestrator (assess_all)
    once per window per benchmark universe rather than per-strategy —
    avoids redundant fetches when multiple strategies share a benchmark.
    """
    envelope = _load_envelope_for(strategy_name)

    min_trades_floor = settings.STRATEGY_MIN_TRADES_FOR_VERDICT.get(
        strategy_name, 50,  # safe default — favors INSUFFICIENT
    )
    sleeve_dollars = _nominal_sleeve_dollars(strategy_name)

    edge_report, new_state = EdgeAssessor().assess(EdgeInputs(
        strategy_name=strategy_name,
        period_start=window.period_start,
        period_end=window.period_end,
        envelope=envelope,
        conn=conn,
        persistence_state=persistence_state,
        min_trades_floor=min_trades_floor,
        benchmark_return=benchmark_return,
        nominal_sleeve_dollars=sleeve_dollars,
    ))

    health_report = HealthAssessor().assess(HealthInputs(
        strategy_name=strategy_name,
        period_start=window.period_start,
        period_end=window.period_end,
        envelope=envelope,
        conn=conn,
        engine_state=engine_state,
    ))

    bundle = AssessmentBundle(
        strategy=strategy_name,
        edge=edge_report,
        health=health_report,
        recommendation=recommend(edge_report, health_report),
    )
    return bundle, new_state


# ── Alert dispatch ────────────────────────────────────────────────────


def dispatch_alerts(
    bundles: Sequence[AssessmentBundle],
    dispatcher: AlertDispatcher,
) -> int:
    """Fire per-bundle alerts per the v1 alert taxonomy (design §11).

    Returns the count of alerts sent (post-dedup-cooldown). Counts
    suppressed-by-cooldown as 0; the dispatcher's existing 5-minute
    cooldown applies normally — multiple runs in the same window
    don't re-blast the operator.

    Alert hierarchy per design:
      - STRATEGY_EDGE_LOSS (CRITICAL) on Edge NEGATIVE+CONCLUSIVE
        with persistence reached
      - STRATEGY_EDGE_BELOW_BENCHMARK (WARNING) on BELOW_BENCHMARK
        verdict
      - STRATEGY_HEALTH_BROKEN (WARNING when Edge non-positive, INFO
        when positive) on L1/L2 BROKEN
      - STRATEGY_HEALTH_DEGRADED (same severity ladder) on L1/L2
        DEGRADED
      - STRATEGY_DRIFT_WARNING (INFO) on L3 DEGRADED (drift)
    """
    n_sent = 0
    for bundle in bundles:
        edge, health = bundle.edge, bundle.health

        # 1. Silent-killer alarm (only fires when the persistence
        # requirement is met — EdgeAssessor handles that gating).
        if edge.verdict == EdgeVerdict.NEGATIVE:
            n_sent += int(dispatcher.strategy_edge_loss(
                bundle.strategy,
                r_expectancy=edge.r_expectancy,
                trade_count=edge.trade_count,
                negative_persistence_weeks=edge.negative_persistence_weeks,
            ))

        # 2. Below-benchmark
        if edge.verdict == EdgeVerdict.BELOW_BENCHMARK:
            n_sent += int(dispatcher.strategy_edge_below_benchmark(
                bundle.strategy,
                strategy_return=edge.strategy_return,
                benchmark_return=edge.benchmark_return,
                alpha=edge.alpha,
            ))

        # 3 & 4. Health alerts — fire on the layer-level status, not
        # overall_status, so the operator knows which layer to look at.
        # Skip L3 here; that's the drift alert below.
        edge_verdict_str = edge.verdict.value
        for layer, status in [
            ("L1", health.l1_status),
            ("L2", health.l2_status),
        ]:
            findings = _findings_for_layer(health, layer)
            if status == HealthStatus.BROKEN:
                n_sent += int(dispatcher.strategy_health_broken(
                    bundle.strategy,
                    layer=layer,
                    edge_verdict=edge_verdict_str,
                    findings=findings,
                ))
            elif status == HealthStatus.DEGRADED:
                n_sent += int(dispatcher.strategy_health_degraded(
                    bundle.strategy,
                    layer=layer,
                    edge_verdict=edge_verdict_str,
                    findings=findings,
                ))

        # 5. L3 drift warning — different alert type so the operator
        # can route differently (e.g., L3 is informational; L1/L2 may
        # need fixes).
        if health.l3_status in (HealthStatus.WATCH, HealthStatus.DEGRADED):
            # Find the L3 check with the largest drift (most informative).
            from strategies.health.reports import Layer
            l3_drifters = [
                c for c in health.checks
                if c.layer == Layer.L3 and c.numeric_value is not None
                and c.status != HealthStatus.HEALTHY
            ]
            for check in l3_drifters[:1]:  # one alert per bundle is enough
                # Try to extract the envelope band from the finding text
                # — best-effort, falls back to (0, 0) if not present.
                band = _parse_band_from_finding(check.findings)
                n_sent += int(dispatcher.strategy_drift_warning(
                    bundle.strategy,
                    check_name=check.name,
                    observed=check.numeric_value or 0.0,
                    envelope_band=band,
                ))

    return n_sent


def _findings_for_layer(health: HealthReport, layer: str) -> list[str]:
    """Collect findings strings from checks at the given layer."""
    from strategies.health.reports import Layer
    layer_enum = {"L1": Layer.L1, "L2": Layer.L2, "L3": Layer.L3}[layer]
    findings: list[str] = []
    for c in health.checks:
        if c.layer == layer_enum and c.status != HealthStatus.HEALTHY:
            findings.extend(c.findings)
    return findings


def _parse_band_from_finding(findings: tuple[str, ...]) -> tuple[float, float]:
    """Best-effort extraction of envelope band from drift check
    findings text. Returns (0, 0) if not parseable — caller uses this
    purely for operator-facing alert formatting, so failure is
    cosmetic, not functional."""
    import re
    for f in findings:
        # Look for "band X-Y" or "band [X, Y]" patterns
        m = re.search(r"band\s*\[?\s*([\d.]+)%?\s*[-,]\s*([\d.]+)%?", f)
        if m:
            try:
                lo = float(m.group(1))
                hi = float(m.group(2))
                # If percentages (X%-Y%) the parsed numbers are 1-100;
                # divide by 100 to get fractions for consistent display.
                if "%" in f:
                    return (lo / 100.0, hi / 100.0)
                return (lo, hi)
            except ValueError:
                continue
    return (0.0, 0.0)


# ── Markdown rendering ───────────────────────────────────────────────


def render_markdown(
    bundles: Sequence[AssessmentBundle],
    window: ReviewWindow,
) -> str:
    """Render the weekly/monthly health report as markdown.

    Layout per design §12.6:
      - YAML front-matter (schema_version, period bounds, generated_at)
      - Top: silent-killer summary banner (if any) — design §13 says
        this MUST surface prominently, not be buried in a strategy
        section
      - Per-strategy summary table — one row per strategy with the
        7 columns specified in design (Verdict, Confidence, Sample,
        Key metrics, Top failure reasons, Recommendation)
      - Per-strategy detail sections — full EdgeReport + HealthReport
      - Carver caveat footer on any CONCLUSIVE verdict (design §13)
    """
    lines: list[str] = []

    # ── YAML front-matter ────────────────────────────────────────
    lines += [
        "---",
        f"schema_version: {REPORT_SCHEMA_VERSION}",
        f"period_start: {window.period_start.isoformat()}",
        f"period_end: {window.period_end.isoformat()}",
        f"period_type: {window.period_type}",
        f"generated_at: {datetime.now(timezone.utc).isoformat()}",
        "---",
        "",
    ]

    # ── Title ────────────────────────────────────────────────────
    period_label = _format_period_label(window)
    lines += [
        f"# Strategy Health & Edge Report — {period_label}",
        "",
    ]

    # ── Silent-killer banner ────────────────────────────────────
    killers = [b for b in bundles if b.edge.verdict == EdgeVerdict.NEGATIVE]
    if killers:
        lines += [
            "## ⚠️ Silent-Killer Alarm",
            "",
            "The following strategies have **clean execution but are losing money**. "
            "Per design §13 this is the case the monitor exists to catch loudly. "
            "**Operator action: pause and investigate.**",
            "",
        ]
        for b in killers:
            r_str = (
                f"R-expectancy {b.edge.r_expectancy:+.3f}"
                if b.edge.r_expectancy is not None
                else "R-expectancy n/a"
            )
            lines.append(
                f"- **{b.strategy}** — {r_str} over "
                f"{b.edge.trade_count} trades, "
                f"{b.edge.negative_persistence_weeks} weeks of negative signals"
            )
        lines.append("")

    # ── Summary table ────────────────────────────────────────────
    lines += [
        "## Summary",
        "",
        "| Strategy | Verdict | Confidence | Sample | Key Metrics | "
        "Top Failure Reasons | Recommendation |",
        "|---|---|---|---|---|---|---|",
    ]
    for b in sorted(bundles, key=lambda x: x.strategy):
        lines.append(_summary_row(b))
    lines.append("")

    # ── Per-strategy detail sections ─────────────────────────────
    has_conclusive = any(
        b.edge.sufficiency == Sufficiency.CONCLUSIVE for b in bundles
    )
    for b in sorted(bundles, key=lambda x: x.strategy):
        lines += _strategy_detail_section(b)

    # ── Carver caveat footer (design §13) ───────────────────────
    if has_conclusive:
        lines += [
            "---",
            "",
            "## Epistemic Caveat",
            "",
            "Per Carver (*Systematic Trading*, ch.3), a single strategy on "
            "typical Sharpe requires ~10 years of daily returns to fully "
            "separate skill from luck. CONCLUSIVE verdicts above reflect "
            "heuristic statistical confidence (sample ≥ "
            "min_trades_for_verdict, three independent signals, three-week "
            "persistence) — the operator should treat them as advisory, "
            "not proof. Recommended action: manual review of the trade "
            "log before any capital change.",
            "",
        ]

    return "\n".join(lines)


def _format_period_label(window: ReviewWindow) -> str:
    """Operator-friendly period label for the report title."""
    if window.period_type == "weekly":
        iso = window.period_start.isocalendar()
        return f"Week {iso[0]}-W{iso[1]:02d}"
    if window.period_type == "monthly":
        return window.period_start.strftime("%B %Y")
    return f"{window.period_start.isoformat()} → {window.period_end.isoformat()}"


def _summary_row(bundle: AssessmentBundle) -> str:
    """One markdown table row per strategy. Per design §12.6 the
    in-text metric labels distinguish measured/inferred/envelope
    sources (e.g. 'R-exp 0.42 (measured from 58 trades); envelope
    band [0.20, 0.70] (from backtest)')."""
    edge, health = bundle.edge, bundle.health
    # Verdict + status badge
    verdict_str = edge.verdict.value
    confidence = edge.sufficiency.value
    sample = f"{edge.trade_count} of {edge.min_trades_for_verdict} trades"
    # Key metrics — R-expectancy primary, dollar secondary (design §5.1)
    if edge.r_expectancy is not None:
        key = f"R={edge.r_expectancy:+.3f} (measured)"
    elif edge.expectancy_dollars is not None:
        key = f"${edge.expectancy_dollars:+,.0f} (measured)"
    else:
        key = "—"
    # Top failure reasons — empty for POSITIVE verdicts
    reasons = (
        "; ".join(edge.failure_reasons[:2])
        if edge.failure_reasons else "—"
    )
    # Truncate reasons cell for table readability
    if len(reasons) > 80:
        reasons = reasons[:77] + "…"
    rec = bundle.recommendation.value
    return (
        f"| {bundle.strategy} | **{verdict_str}** | {confidence} | "
        f"{sample} | {key} | {reasons} | **{rec}** |"
    )


def _strategy_detail_section(bundle: AssessmentBundle) -> list[str]:
    """Per-strategy full breakdown — EdgeReport + HealthReport detail."""
    edge, health = bundle.edge, bundle.health
    lines: list[str] = [
        f"## {bundle.strategy}",
        "",
        f"**Verdict:** `{edge.verdict.value}` "
        f"({edge.sufficiency.value}) — "
        f"**Recommendation:** `{bundle.recommendation.value}`",
        "",
    ]

    # ── Edge Report ──────────────────────────────────────────────
    lines += [
        "### Edge Report",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Realized P&L | ${edge.realized_pnl:+,.2f} (measured) |",
        f"| Trade count | {edge.trade_count} |",
        f"| Sufficiency floor | {edge.min_trades_for_verdict} trades |",
    ]
    if edge.r_expectancy is not None:
        lines.append(
            f"| R-expectancy | {edge.r_expectancy:+.3f} "
            f"(measured from {edge.trade_count} trades) |"
        )
    if edge.r_expectancy_ci_95 is not None:
        lo, hi = edge.r_expectancy_ci_95
        lines.append(
            f"| R-expectancy 95% CI | [{lo:+.3f}, {hi:+.3f}] "
            f"(measured, iid bootstrap) |"
        )
    if edge.envelope_r_expectancy_ci_95 is not None:
        lo, hi = edge.envelope_r_expectancy_ci_95
        lines.append(
            f"| Envelope R-band | [{lo:+.3f}, {hi:+.3f}] (from backtest) |"
        )
    if edge.expectancy_dollars is not None:
        lines.append(
            f"| Dollar expectancy | ${edge.expectancy_dollars:+,.2f} "
            f"(measured) |"
        )
    if edge.profit_factor is not None:
        lines.append(
            f"| Profit factor | {edge.profit_factor:.2f} (measured) |"
        )
    if edge.win_rate is not None:
        lines.append(
            f"| Win rate | {edge.win_rate:.1%} (measured) |"
        )
    if edge.sleeve_utilization is not None:
        lines.append(
            f"| Sleeve utilization | {edge.sleeve_utilization:.1%} "
            f"(inferred from PnL activity) |"
        )
    if edge.benchmark_return is not None:
        lines.append(
            f"| Benchmark return | {edge.benchmark_return:+.1%} (measured) |"
        )
    if edge.strategy_return is not None:
        lines.append(
            f"| Strategy return | {edge.strategy_return:+.1%} "
            f"(inferred from nominal sleeve) |"
        )
    if edge.alpha is not None:
        lines.append(f"| Alpha (vs benchmark) | {edge.alpha:+.1%} |")
    if edge.negative_persistence_weeks > 0:
        lines.append(
            f"| Negative-signal weeks | {edge.negative_persistence_weeks} "
            f"(alarm fires at 3) |"
        )
    lines.append("")

    if edge.failure_reasons:
        lines += [
            "**Failure reasons:**",
            "",
        ]
        for reason in edge.failure_reasons:
            lines.append(f"- {reason}")
        lines.append("")

    # ── Health Report ────────────────────────────────────────────
    lines += [
        "### Health Report",
        "",
        f"**Overall:** `{health.overall_status.value}` "
        f"(L1: {health.l1_status.value}, "
        f"L2: {health.l2_status.value}, "
        f"L3: {health.l3_status.value})",
        "",
    ]

    # Only surface checks that aren't HEALTHY — keeps the section
    # short. A HealthReport with all HEALTHY checks shows only the
    # overall line, no table.
    non_healthy_checks = [
        c for c in health.checks if c.status != HealthStatus.HEALTHY
    ]
    if non_healthy_checks:
        lines += [
            "| Check | Layer | Status | Findings |",
            "|---|---|---|---|",
        ]
        for c in non_healthy_checks:
            findings_str = "; ".join(c.findings) if c.findings else "—"
            if len(findings_str) > 120:
                findings_str = findings_str[:117] + "…"
            lines.append(
                f"| {c.name} | {c.layer.value} | "
                f"`{c.status.value}` | {findings_str} |"
            )
        lines.append("")
    else:
        lines += ["All L1/L2/L3 checks passing.", ""]

    return lines


# ── Top-level orchestration ──────────────────────────────────────────


def assess_all_strategies(
    window: ReviewWindow,
    *,
    conn: sqlite3.Connection,
    state_path: str | Path | None = None,
    engine_state_path: str | Path | None = None,
    strategies: Sequence[str] | None = None,
    persist_state: bool = True,
) -> list[AssessmentBundle]:
    """Run the full assessment pipeline for all (or a filtered set of)
    strategies for the given window.

    Loads persistence state once, threads it through each strategy's
    assess() call, saves the updated state back to disk at the end
    (atomic via persistence.save_state). Loads engine_state once too —
    L1 checks read from the same snapshot for consistency.

    Benchmark returns are computed per benchmark-symbol-set so two
    strategies sharing a watchlist share a benchmark fetch.

    **`persist_state=False` makes the call side-effect free** — the
    state is loaded and threaded through assessment (so verdicts
    reflect the correct projected counts), but the updated state is
    NOT written back to disk. Used by dry-run preview to ensure
    repeated operator-driven previews don't advance the silent-killer
    persistence counter outside the real scheduled cadence.
    PR #20 reviewer caught the original always-persist behavior as a
    safety issue: a "safe preview" advancing `negative_weeks` could
    trip the 3-week alarm prematurely.
    """
    state_file = load_state(state_path)
    engine_state = load_engine_state(engine_state_path)

    strategy_list = (
        list(strategies)
        if strategies is not None
        else list(getattr(settings, "STRATEGY_MIN_TRADES_FOR_VERDICT", {}).keys())
    )

    # Benchmark cache: benchmark_key (tuple of symbols) → return.
    benchmark_cache: dict[tuple, float | None] = {}

    bundles: list[AssessmentBundle] = []
    for strategy_name in strategy_list:
        # Resolve benchmark and cache by symbol-tuple — strategies
        # sharing a watchlist share the cache hit.
        benchmark_return: float | None = None
        try:
            symbols = benchmark_symbols_for(strategy_name)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"{strategy_name}: benchmark resolution failed — {exc}"
            )
            symbols = []
        if symbols:
            cache_key = tuple(sorted(symbols))
            if cache_key not in benchmark_cache:
                try:
                    period_start_dt = datetime.combine(
                        window.period_start, datetime.min.time(),
                        tzinfo=timezone.utc,
                    )
                    period_end_dt = datetime.combine(
                        window.period_end, datetime.min.time(),
                        tzinfo=timezone.utc,
                    )
                    benchmark_cache[cache_key] = equal_weight_bh_return(
                        symbols, period_start_dt, period_end_dt,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"{strategy_name}: benchmark fetch failed — {exc}"
                    )
                    benchmark_cache[cache_key] = None
            benchmark_return = benchmark_cache[cache_key]

        prev_state = state_file.get_or_default(strategy_name)
        bundle, new_state = assess_strategy(
            strategy_name,
            window,
            conn=conn,
            persistence_state=prev_state,
            engine_state=engine_state,
            benchmark_return=benchmark_return,
        )
        bundles.append(bundle)
        state_file = state_file.with_updated(strategy_name, new_state)

    # Save the threaded state ONCE at the end (atomic) — unless
    # persist_state=False (dry-run preview). See docstring.
    if persist_state:
        save_state(state_file, state_path)

    return bundles


def run_review(
    window: ReviewWindow,
    *,
    conn: sqlite3.Connection,
    dispatcher: AlertDispatcher | None = None,
    output_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    engine_state_path: str | Path | None = None,
    strategies: Sequence[str] | None = None,
    dry_run: bool = False,
) -> tuple[Path | None, list[AssessmentBundle]]:
    """End-to-end: assess all strategies, render markdown, dispatch
    alerts. Returns (report_path, bundles).

    Dry-run mode is **fully side-effect free**: skips alert dispatch,
    skips report file write, AND skips persistence-state save. The
    assessment still loads the existing persistence state and threads
    it through so verdict projections are accurate, but the updated
    state is not written back. This means an operator can run
    dry-run previews repeatedly without inadvertently advancing the
    silent-killer counter outside the scheduled weekly cadence.
    """
    bundles = assess_all_strategies(
        window,
        conn=conn,
        state_path=state_path,
        engine_state_path=engine_state_path,
        strategies=strategies,
        persist_state=not dry_run,
    )

    markdown = render_markdown(bundles, window)

    report_path: Path | None = None
    if not dry_run:
        report_path = _write_report(markdown, window, output_dir)
        if dispatcher is not None:
            n_sent = dispatch_alerts(bundles, dispatcher)
            logger.info(
                f"strategy_health_review: {n_sent} alerts dispatched "
                f"({len(bundles)} strategies assessed)"
            )

    return report_path, bundles


def _write_report(
    markdown: str,
    window: ReviewWindow,
    output_dir: str | Path | None,
) -> Path:
    """Atomic-write the markdown report to disk."""
    base = (
        Path(output_dir) if output_dir
        else Path(__file__).resolve().parents[2] / "data" / "health_reports"
    )
    base.mkdir(parents=True, exist_ok=True)
    if window.period_type == "weekly":
        iso = window.period_start.isocalendar()
        fname = f"weekly_{iso[0]}-W{iso[1]:02d}.md"
    elif window.period_type == "monthly":
        fname = window.period_start.strftime("monthly_%Y-%m.md")
    else:
        fname = (
            f"{window.period_type}_{window.period_start.isoformat()}_"
            f"{window.period_end.isoformat()}.md"
        )
    path = base / fname
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(markdown)
    tmp.replace(path)
    logger.info(f"strategy health report written: {path}")
    return path


def window_from_args(
    period_type: str, *, end_date: date | None = None,
) -> ReviewWindow:
    """Compute period bounds from `--window` flag and an optional end
    date (defaults to today).

    weekly: 7 days back from end_date (Mon→Mon convention when
            end_date is a Monday; otherwise just trailing 7 days).
    monthly: previous calendar month.
    yearly: previous 365 days.
    """
    end = end_date or date.today()
    if period_type == "weekly":
        from datetime import timedelta
        return ReviewWindow(
            period_start=end - timedelta(days=7),
            period_end=end,
            period_type="weekly",
        )
    if period_type == "monthly":
        # Previous calendar month
        first_of_this = end.replace(day=1)
        if first_of_this.month == 1:
            first_of_prev = first_of_this.replace(
                year=first_of_this.year - 1, month=12,
            )
        else:
            first_of_prev = first_of_this.replace(
                month=first_of_this.month - 1,
            )
        return ReviewWindow(
            period_start=first_of_prev,
            period_end=first_of_this,
            period_type="monthly",
        )
    if period_type == "yearly":
        from datetime import timedelta
        return ReviewWindow(
            period_start=end - timedelta(days=365),
            period_end=end,
            period_type="yearly",
        )
    raise ValueError(
        f"unknown period_type {period_type!r} (use weekly|monthly|yearly)"
    )
