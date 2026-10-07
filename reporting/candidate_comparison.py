"""Read-only selected-versus-refused reporting for PLAN 11.61.

Selected candidates derive outcomes from durable broker lifecycles. Refused
candidates derive outcomes only from the disposable, strategy-specific shadow
table. The two bases stay visible and are never silently pooled.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from reporting.candidate_shadows import candidate_is_capacity_refusal


SUPPORTED_STRATEGIES = (
    "rsi_reversion",
    "sma_crossover",
    "donchian_breakout",
)
_TERMINAL_LIFECYCLES = {"closed", "external_closed"}


def _decoded(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(str(value))
    return decoded if isinstance(decoded, dict) else {}


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


def _duration(start: Any, end: Any) -> str | None:
    if not start or not end:
        return None
    try:
        delta = datetime.fromisoformat(str(end)) - datetime.fromisoformat(str(start))
    except (TypeError, ValueError):
        return None
    hours = max(0.0, delta.total_seconds() / 3600.0)
    return f"{hours / 24.0:.1f}d" if hours >= 48.0 else f"{hours:.1f}h"


def _selected_outcome(conn: sqlite3.Connection, position_uid: str | None) -> dict[str, Any]:
    if not position_uid:
        return {"status": "unresolved", "basis": "missing_lifecycle"}
    row = conn.execute(
        """
        SELECT p.status, p.avg_entry_price, p.first_fill_at, p.closed_at,
               p.net_realized_pnl,
               EXISTS(
                   SELECT 1 FROM position_lifecycle_orders o
                   WHERE o.position_uid = p.position_uid
                     AND o.origin_kind = 'operator'
               ) AS operator_modified
        FROM position_lifecycle p WHERE p.position_uid = ?
        """,
        (position_uid,),
    ).fetchone()
    if row is None:
        return {"status": "unresolved", "basis": "missing_lifecycle"}
    status = str(row["status"])
    initial_risk_row = conn.execute(
        "SELECT MAX(initial_risk_dollars) FROM trades "
        "WHERE position_uid = ? AND initial_risk_dollars IS NOT NULL",
        (position_uid,),
    ).fetchone()
    initial_risk = _optional_float(initial_risk_row[0] if initial_risk_row else None)
    pnl = _optional_float(row["net_realized_pnl"])
    r_multiple = (
        pnl / initial_risk
        if status in _TERMINAL_LIFECYCLES
        and pnl is not None
        and initial_risk is not None
        and initial_risk > 0
        else None
    )
    exits = conn.execute(
        """
        SELECT avg_fill_price, COALESCE(filled_qty, qty)
        FROM trades
        WHERE position_uid = ? AND realized_pnl IS NOT NULL
          AND avg_fill_price IS NOT NULL AND status IN ('filled', 'partial')
        """,
        (position_uid,),
    ).fetchall()
    exit_reason_row = conn.execute(
        """
        SELECT reason FROM trades
        WHERE position_uid = ? AND realized_pnl IS NOT NULL
        ORDER BY timestamp DESC LIMIT 1
        """,
        (position_uid,),
    ).fetchone()
    weighted_exit = None
    total_qty = sum(float(exit_row[1] or 0.0) for exit_row in exits)
    if total_qty > 0:
        weighted_exit = sum(
            float(exit_row[0]) * float(exit_row[1] or 0.0) for exit_row in exits
        ) / total_qty
    entry = _optional_float(row["avg_entry_price"])
    return_pct = (
        weighted_exit / entry - 1.0
        if status in _TERMINAL_LIFECYCLES
        and weighted_exit is not None
        and entry is not None
        and entry > 0
        else None
    )
    return {
        "status": "resolved" if status in _TERMINAL_LIFECYCLES else status,
        "basis": "actual_lifecycle",
        "return_pct": return_pct,
        "r_multiple": r_multiple,
        "mfe": None,
        "mae": None,
        "exit_at": row["closed_at"],
        "duration": _duration(row["first_fill_at"], row["closed_at"]),
        "exit_reason": exit_reason_row[0] if exit_reason_row else None,
        "operator_modified": bool(row["operator_modified"]),
    }


def _refused_outcome(
    row: sqlite3.Row, *, repo_root: str | Path
) -> dict[str, Any]:
    capacity_refusal = candidate_is_capacity_refusal(
        dict(row), repo_root=repo_root
    )
    if not capacity_refusal:
        return {
            "status": "not_eligible",
            "basis": "production_rejection",
            "return_pct": None,
            "r_multiple": None,
            "mfe": None,
            "mae": None,
            "exit_at": None,
            "duration": None,
            "exit_reason": None,
            "operator_modified": False,
            "capacity_refusal": False,
        }
    metadata = _decoded(row["shadow_metadata"])
    entry_at = metadata.get("entry_at")
    return {
        "status": str(row["shadow_status"] or "pending"),
        "basis": str(row["shadow_basis"] or "counterfactual_required"),
        "return_pct": _optional_float(row["shadow_return_pct"]),
        "r_multiple": _optional_float(row["shadow_r_multiple"]),
        "mfe": _optional_float(row["shadow_mfe"]),
        "mae": _optional_float(row["shadow_mae"]),
        "exit_at": row["shadow_exit_at"],
        "duration": _duration(entry_at, row["shadow_exit_at"]),
        "exit_reason": metadata.get("exit_reason"),
        "operator_modified": False,
        "capacity_refusal": True,
    }


def _feature_summary(row: sqlite3.Row) -> str:
    features = _decoded(row["strategy_features_json"])
    common = _decoded(row["common_context_json"])
    reference = _optional_float(row["reference_price"])
    atr = _optional_float(row["atr"])
    values: list[tuple[str, Any]]
    if row["strategy"] == "rsi_reversion":
        values = [
            ("depth", features.get("oversold_depth")),
            ("1d", features.get("one_bar_return_pct")),
            ("3d", features.get("three_bar_return_pct")),
            ("exitSMA", features.get("distance_to_exit_sma_pct")),
        ]
    elif row["strategy"] == "sma_crossover":
        slow = _optional_float(features.get("slow_sma"))
        extension = reference / slow - 1.0 if reference and slow and slow > 0 else None
        values = [
            ("gap", features.get("crossover_gap_pct")),
            ("fastSlope", features.get("fast_slope_pct")),
            ("slowSlope", features.get("slow_slope_pct")),
            ("extension", extension),
            ("1d", features.get("one_bar_return_pct")),
        ]
    else:
        values = [
            ("breakout", features.get("breakout_pct")),
            ("channel", features.get("channel_width_pct")),
            ("volumeRatio", features.get("volume_vs_20d_average")),
        ]
    values.extend([
        ("ATR", atr / reference if atr and reference and reference > 0 else None),
        ("sector", common.get("sector")),
    ])
    rendered = []
    for name, value in values:
        if value is None:
            continue
        if isinstance(value, (int, float)):
            rendered.append(f"{name}={float(value):+.3f}")
        else:
            rendered.append(f"{name}={value}")
    return ", ".join(rendered) or "—"


def _pct(value: Any) -> str:
    number = _optional_float(value)
    return "—" if number is None else f"{number * 100:+.2f}%"


def _r(value: Any) -> str:
    number = _optional_float(value)
    return "—" if number is None else f"{number:+.2f}R"


def build_candidate_comparison_report(
    conn: sqlite3.Connection,
    *,
    strategies: Iterable[str] = SUPPORTED_STRATEGIES,
    repo_root: str | Path | None = None,
) -> str:
    """Return a deterministic Markdown report of real contention groups."""
    selected_strategies = tuple(dict.fromkeys(strategies))
    invalid = sorted(set(selected_strategies) - set(SUPPORTED_STRATEGIES))
    if invalid:
        raise ValueError(f"unsupported strategies: {invalid}")
    conn.row_factory = sqlite3.Row
    resolved_repo_root = Path(repo_root or Path(__file__).resolve().parents[1])
    placeholders = ",".join("?" for _ in selected_strategies)
    rows = conn.execute(
        f"""
        SELECT d.*, s.status AS shadow_status,
               s.outcome_basis AS shadow_basis,
               s.return_pct AS shadow_return_pct,
               s.r_multiple AS shadow_r_multiple,
               s.max_favorable_pct AS shadow_mfe,
               s.max_adverse_pct AS shadow_mae,
               s.exit_at AS shadow_exit_at,
               s.metadata_json AS shadow_metadata
        FROM entry_candidate_decisions d
        LEFT JOIN entry_candidate_shadow_outcomes s USING(candidate_uid)
        WHERE d.capacity_contended = 1 AND d.strategy IN ({placeholders})
        ORDER BY d.strategy, d.signal_at, d.cycle_uid, d.evaluation_ordinal
        """,
        selected_strategies,
    ).fetchall()
    groups: dict[
        tuple[str, str, str, str],
        list[tuple[sqlite3.Row, dict[str, Any]]],
    ] = defaultdict(list)
    for row in rows:
        outcome = (
            _selected_outcome(conn, row["position_uid"])
            if bool(row["selected"])
            else _refused_outcome(row, repo_root=resolved_repo_root)
        )
        groups[
            (
                str(row["strategy"]),
                str(row["regime"] or "UNKNOWN"),
                str(row["cycle_uid"]),
                str(row["signal_at"]),
            )
        ].append((row, outcome))

    summary: dict[tuple[str, str], dict[str, int]] = defaultdict(
        lambda: {
            "groups": 0,
            "selected": 0,
            "refused": 0,
            "other_rejected": 0,
            "resolved_selected": 0,
            "resolved_refused": 0,
        }
    )
    for (strategy, regime, _cycle, _signal_at), items in groups.items():
        bucket = summary[(strategy, regime)]
        bucket["groups"] += 1
        for row, outcome in items:
            if bool(row["selected"]):
                kind = "selected"
            elif bool(outcome.get("capacity_refusal")):
                kind = "refused"
            else:
                bucket["other_rejected"] += 1
                continue
            bucket[kind] += 1
            if outcome["status"] == "resolved":
                bucket[f"resolved_{kind}"] += 1

    lines = [
        "# Entry-candidate comparison",
        "",
        "**Observation only. This report does not rank candidates or change trading.**",
        "",
        "Actual selected outcomes come from durable broker lifecycles. Refused "
        "outcomes are strategy-specific counterfactual replays; their dollar P&L "
        "is intentionally unavailable.",
        "",
        "## Coverage",
        "",
        "| Strategy | Regime | Groups | Selected resolved | Capacity-refused "
        "resolved | Other rejected |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for (strategy, regime), values in sorted(summary.items()):
        lines.append(
            f"| {strategy} | {regime} | {values['groups']} | "
            f"{values['resolved_selected']}/{values['selected']} | "
            f"{values['resolved_refused']}/{values['refused']} | "
            f"{values['other_rejected']} |"
        )
    if not groups:
        lines.append("| — | — | 0 | 0/0 | 0/0 | 0 |")

    lines.extend(["", "## Contention groups", ""])
    for (strategy, regime, cycle_uid, signal_at), items in groups.items():
        lines.extend([
            f"### {strategy} · {regime} · {signal_at[:10]} · {cycle_uid[:8]}",
            "",
            "| Candidate | Decision | Disposition | Outcome | Return | R | MFE | "
            "MAE | Hold | Exit | Basis | Decision-time characteristics |",
            "|---|---|---|---|---:|---:|---:|---:|---:|---|---|---|",
        ])
        for row, outcome in items:
            decision = "selected" if bool(row["selected"]) else "refused"
            status = str(outcome["status"])
            if outcome.get("operator_modified"):
                status += " (operator-modified)"
            lines.append(
                f"| {row['symbol']} | {decision} | {row['disposition']} | "
                f"{status} | {_pct(outcome.get('return_pct'))} | "
                f"{_r(outcome.get('r_multiple'))} | {_pct(outcome.get('mfe'))} | "
                f"{_pct(outcome.get('mae'))} | {outcome.get('duration') or '—'} | "
                f"{outcome.get('exit_reason') or '—'} | {outcome['basis']} | "
                f"{_feature_summary(row)} |"
            )
        lines.append("")

    lines.extend([
        "## Interpretation limits",
        "",
        "- Strategies and regimes are independent evidence pools; their outcomes are not pooled.",
        "- Open, unfilled, pending-data, and needs-review rows are not treated as resolved.",
        "- Actual lifecycle MFE/MAE remains unavailable unless it can be "
        "reconstructed from complete post-fill bars; the report prints `—` "
        "rather than inventing it.",
        "- Small samples describe observations only. Any ranking rule requires "
        "repeated groups and later-sample validation.",
        "",
    ])
    return "\n".join(lines)


__all__ = ["SUPPORTED_STRATEGIES", "build_candidate_comparison_report"]
