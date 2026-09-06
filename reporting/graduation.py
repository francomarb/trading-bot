"""Lifecycle-first, advisory strategy graduation evidence report."""

from __future__ import annotations

import json
import math
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from reporting.logger import is_execution_quality_measurement


REPORT_SCHEMA_VERSION = 1
TERMINAL_STATUSES = frozenset({"closed", "external_closed"})
_REQUIRED_LIFECYCLE_COLUMNS = frozenset({
    "position_uid", "strategy", "strategy_version", "strategy_config_hash",
    "bot_git_commit", "entry_regime", "created_at", "closed_at", "status",
    "net_realized_pnl",
})
_REQUIRED_TRADE_COLUMNS = frozenset({
    "position_uid", "initial_risk_dollars", "realized_pnl", "strategy",
    "slippage_benchmark_kind", "slippage_measurement_quality",
    "slippage_measurement_version", "slippage_adverse_bps",
})
_REQUIRED_ORDER_COLUMNS = frozenset({"position_uid", "status", "origin_kind"})


@dataclass(frozen=True)
class _Outcome:
    position_uid: str
    strategy: str
    version: str | None
    config_hash: str | None
    bot_commit: str | None
    entry_regime: str | None
    opened_at: str
    closed_at: str | None
    status: str
    pnl: float
    risk_dollars: float | None
    execution_slippage_bps: tuple[float, ...]
    order_events: tuple[tuple[str, str], ...]
    pnl_reconciled: bool
    has_realized_pnl_event: bool
    identity_source: str = "recorded"


def _safe_div(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def _max_drawdown(values: list[float]) -> float:
    running = 0.0
    peak = 0.0
    worst = 0.0
    for value in values:
        running += value
        peak = max(peak, running)
        worst = min(worst, running - peak)
    return worst


def _longest_loss_streak(values: list[float]) -> int:
    longest = current = 0
    for value in values:
        current = current + 1 if value < 0 else 0
        longest = max(longest, current)
    return longest


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _read_outcomes(conn: sqlite3.Connection) -> tuple[list[_Outcome], dict[str, Any]]:
    lifecycle_columns = _table_columns(conn, "position_lifecycle")
    if not _REQUIRED_LIFECYCLE_COLUMNS.issubset(lifecycle_columns):
        missing = sorted(_REQUIRED_LIFECYCLE_COLUMNS - lifecycle_columns)
        raise RuntimeError(
            "database has not received the graduation identity migration; "
            f"missing position_lifecycle columns: {', '.join(missing)}"
        )

    trade_columns = _table_columns(conn, "trades")
    if not _REQUIRED_TRADE_COLUMNS.issubset(trade_columns):
        missing = sorted(_REQUIRED_TRADE_COLUMNS - trade_columns)
        raise RuntimeError(
            "trade database schema is too old for graduation reporting; "
            f"missing trades columns: {', '.join(missing)}"
        )

    trade_rows = conn.execute(
        "SELECT position_uid, initial_risk_dollars, realized_pnl, "
        "slippage_benchmark_kind, slippage_measurement_quality, "
        "slippage_measurement_version, slippage_adverse_bps "
        "FROM trades WHERE position_uid IS NOT NULL"
    ).fetchall()
    risk_by_uid: dict[str, float] = {}
    trade_pnl_by_uid: dict[str, float] = defaultdict(float)
    pnl_event_uids: set[str] = set()
    slippage_by_uid: dict[str, list[float]] = defaultdict(list)
    for row in trade_rows:
        uid = str(row[0])
        risk = row[1]
        if risk is not None and float(risk) > 0:
            risk_by_uid[uid] = max(risk_by_uid.get(uid, 0.0), float(risk))
        if row[2] is not None:
            pnl_event_uids.add(uid)
            trade_pnl_by_uid[uid] += float(row[2])
        if (
            row[6] is not None
            and is_execution_quality_measurement(row[3], row[4], row[5])
        ):
            slippage_by_uid[uid].append(float(row[6]))

    order_events_by_uid: dict[str, list[tuple[str, str]]] = defaultdict(list)
    order_columns = _table_columns(conn, "position_lifecycle_orders")
    if order_columns and not _REQUIRED_ORDER_COLUMNS.issubset(order_columns):
        missing = sorted(_REQUIRED_ORDER_COLUMNS - order_columns)
        raise RuntimeError(
            "order lifecycle schema is too old for graduation reporting; "
            f"missing position_lifecycle_orders columns: {', '.join(missing)}"
        )
    if order_columns:
        for uid, status, origin_kind in conn.execute(
            "SELECT position_uid, status, origin_kind "
            "FROM position_lifecycle_orders"
        ).fetchall():
            order_events_by_uid[str(uid)].append((str(status), str(origin_kind)))

    rows = conn.execute(
        "SELECT position_uid, strategy, strategy_version, "
        "strategy_config_hash, bot_git_commit, entry_regime, created_at, "
        "closed_at, status, net_realized_pnl FROM position_lifecycle "
        "ORDER BY COALESCE(closed_at, created_at), position_uid"
    ).fetchall()
    outcomes = [
        _Outcome(
            position_uid=str(row[0]),
            strategy=str(row[1]),
            version=row[2],
            config_hash=row[3],
            bot_commit=row[4],
            entry_regime=row[5],
            opened_at=str(row[6]),
            closed_at=row[7],
            status=str(row[8]),
            pnl=float(row[9] or 0.0),
            risk_dollars=risk_by_uid.get(str(row[0])),
            execution_slippage_bps=tuple(slippage_by_uid.get(str(row[0]), ())),
            order_events=tuple(order_events_by_uid.get(str(row[0]), ())),
            pnl_reconciled=math.isclose(
                float(row[9] or 0.0),
                trade_pnl_by_uid.get(str(row[0]), 0.0),
                abs_tol=0.01,
            ),
            has_realized_pnl_event=str(row[0]) in pnl_event_uids,
        )
        for row in rows
    ]
    legacy = conn.execute(
        "SELECT strategy, COUNT(*), COALESCE(SUM(realized_pnl), 0.0) "
        "FROM trades WHERE realized_pnl IS NOT NULL AND position_uid IS NULL "
        "GROUP BY strategy ORDER BY strategy"
    ).fetchall()
    diagnostics = {
        "legacy_unlinked_realized_events": [
            {"strategy": row[0], "events": int(row[1]), "realized_pnl": float(row[2])}
            for row in legacy
        ],
        "execution_quality_measurements": sum(len(v) for v in slippage_by_uid.values()),
        "historical_mtm": "unavailable_not_recorded",
        "cost_adjustment": "unavailable_not_recorded",
    }
    return outcomes, diagnostics


def _summarize(
    strategy: str,
    version: str,
    config_hash: str,
    rows: list[_Outcome],
) -> dict[str, Any]:
    terminal = [row for row in rows if row.status in TERMINAL_STATUSES]
    trusted = [
        row for row in terminal
        if row.has_realized_pnl_event and row.pnl_reconciled
    ]
    trusted.sort(key=lambda row: (row.closed_at or row.opened_at, row.position_uid))
    pnls = [row.pnl for row in trusted]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    r_values = [row.pnl / row.risk_dollars for row in trusted if row.risk_dollars]
    months: dict[str, float] = defaultdict(float)
    for row in trusted:
        months[(row.closed_at or row.opened_at)[:7]] += row.pnl
    commits = sorted({row.bot_commit for row in rows if row.bot_commit})
    if len(commits) == 1:
        raw_commit = commits[0]
        commit_label = raw_commit.removeprefix("uncommitted:")[:9]
        if raw_commit.startswith("uncommitted:"):
            commit_label += " (dirty)"
    elif commits:
        commit_label = f"{len(commits)} commits"
    else:
        commit_label = "unknown"
    regimes: dict[str, int] = defaultdict(int)
    for row in trusted:
        regimes[row.entry_regime or "unknown"] += 1
    slippage = sorted(
        value for row in rows for value in row.execution_slippage_bps
    )
    order_statuses: dict[str, int] = defaultdict(int)
    operator_orders = 0
    for row in rows:
        for order_status, origin_kind in row.order_events:
            order_statuses[order_status] += 1
            operator_orders += int(origin_kind == "operator")

    def percentile(values: list[float], fraction: float) -> float | None:
        if not values:
            return None
        index = min(len(values) - 1, math.ceil(fraction * len(values)) - 1)
        return values[index]

    total = sum(pnls)
    best = max(pnls) if pnls else None
    pnl_without_best = total - best if best is not None else None
    integrity_missing = sum(1 for row in rows if not row.version or not row.config_hash)
    pnl_mismatches = sum(not row.pnl_reconciled for row in terminal)
    missing_pnl_events = sum(not row.has_realized_pnl_event for row in terminal)
    status = "EARLY EVIDENCE" if trusted else "DATA INCOMPLETE"
    if integrity_missing or pnl_mismatches or missing_pnl_events or not trusted:
        status = "DATA INCOMPLETE"

    return {
        "strategy": strategy,
        "strategy_version": version,
        "strategy_config_hash": config_hash,
        "display_identity": (
            f"{strategy} v{version} · cfg {config_hash} · bot {commit_label}"
        ),
        "bot_git_commits": commits,
        "identity_sources": sorted({row.identity_source for row in rows}),
        "evidence_status": status,
        "operator_decision": "not_recorded",
        "coverage": {
            "lifecycles": len(rows),
            "terminal_lifecycles": len(terminal),
            "trusted_completed": len(trusted),
            "unresolved_economics": len(terminal) - len(trusted),
            "open_or_nonterminal": len(rows) - len(terminal),
            "identity_missing": integrity_missing,
            "pnl_reconciliation_mismatches": pnl_mismatches,
            "missing_realized_pnl_events": missing_pnl_events,
            "first_opened_at": min((row.opened_at for row in rows), default=None),
            "last_closed_at": max(
                (row.closed_at for row in trusted if row.closed_at),
                default=None,
            ),
            "historical_mtm": "unavailable_not_recorded",
            "cost_adjustment": "unavailable_not_recorded",
        },
        "performance": {
            "gross_realized_pnl": total if pnls else None,
            "net_after_costs": None,
            "average_outcome": statistics.fmean(pnls) if pnls else None,
            "median_outcome": statistics.median(pnls) if pnls else None,
            "win_rate": _safe_div(len(wins), len(pnls)),
            "profit_factor": _safe_div(sum(wins), abs(sum(losses))),
            "worst_outcome": min(pnls) if pnls else None,
            "realized_max_drawdown": _max_drawdown(pnls),
            "longest_loss_streak": _longest_loss_streak(pnls),
            "pnl_without_best_outcome": pnl_without_best,
            "best_outcome_share_of_positive_pnl": (
                _safe_div(best or 0.0, sum(wins))
                if best and best > 0
                else None
            ),
            "r_multiple_count": len(r_values),
            "average_r": statistics.fmean(r_values) if r_values else None,
        },
        "consistency": {
            "monthly_realized_pnl": dict(sorted(months.items())),
            "positive_months": sum(value > 0 for value in months.values()),
            "months_observed": len(months),
            "entry_regimes": dict(sorted(regimes.items())),
        },
        "operations": {
            "order_status_counts": dict(sorted(order_statuses.items())),
            "operator_orders": operator_orders,
            "external_closes": sum(row.status == "external_closed" for row in rows),
            "execution_slippage_samples": len(slippage),
            "median_execution_slippage_bps": statistics.median(slippage) if slippage else None,
            "p95_execution_slippage_bps": percentile(slippage, 0.95),
        },
        "limitations": [
            "Net-after-costs is unavailable until a reviewed cost model or recorded fees exists.",
            "Drawdown is realized-only; historical daily mark-to-market was not recorded.",
            "This status reports evidence readiness and never approves live trading.",
        ],
    }


def _apply_reviewed_epochs(
    outcomes: list[_Outcome], reviewed_epochs_path: str | Path | None
) -> list[_Outcome]:
    """Apply explicit operator-reviewed historical classifications."""
    if reviewed_epochs_path is None:
        return outcomes
    document = json.loads(Path(reviewed_epochs_path).read_text())
    if document.get("schema_version") != 1 or not isinstance(document.get("epochs"), list):
        raise ValueError("reviewed epoch file must use schema_version 1 and an epochs list")
    epochs = document["epochs"]
    result: list[_Outcome] = []
    for row in outcomes:
        if row.version and row.config_hash:
            result.append(row)
            continue
        matches = [
            epoch for epoch in epochs
            if epoch.get("strategy") == row.strategy
            and str(epoch.get("start_at", "")) <= row.opened_at
            and row.opened_at < str(epoch.get("end_at", ""))
        ]
        if len(matches) > 1:
            raise ValueError(
                f"overlapping reviewed epochs for {row.strategy} at {row.opened_at}"
            )
        if not matches:
            result.append(row)
            continue
        epoch = matches[0]
        required = {
            "strategy_version",
            "strategy_config_hash",
            "reviewed_by",
            "reviewed_at",
            "evidence",
        }
        missing = required - epoch.keys()
        if missing:
            raise ValueError(
                f"reviewed epoch for {row.strategy} missing {sorted(missing)}"
            )
        result.append(replace(
            row,
            version=str(epoch["strategy_version"]),
            config_hash=str(epoch["strategy_config_hash"]),
            identity_source="reviewed_inference",
        ))
    return result


def build_graduation_report(
    db_path: str | Path,
    *,
    reviewed_epochs_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build a schema-versioned report without modifying the trade database."""
    path = Path(db_path)
    if not path.is_file():
        raise FileNotFoundError(f"trade database does not exist: {path}")
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        outcomes, diagnostics = _read_outcomes(conn)
    finally:
        conn.close()

    outcomes = _apply_reviewed_epochs(outcomes, reviewed_epochs_path)
    cohorts: dict[tuple[str, str, str], list[_Outcome]] = defaultdict(list)
    unknown: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {
            "lifecycles": 0,
            "terminal_lifecycles": 0,
            "trusted_completed": 0,
            "unresolved_economics": 0,
            "realized_pnl": 0.0,
        }
    )
    for row in outcomes:
        if row.version and row.version != "unknown" and row.config_hash:
            cohorts[(row.strategy, row.version, row.config_hash)].append(row)
        else:
            unknown[row.strategy]["lifecycles"] += 1
            if row.status in TERMINAL_STATUSES:
                unknown[row.strategy]["terminal_lifecycles"] += 1
                if row.has_realized_pnl_event and row.pnl_reconciled:
                    unknown[row.strategy]["trusted_completed"] += 1
                    unknown[row.strategy]["realized_pnl"] += row.pnl
                else:
                    unknown[row.strategy]["unresolved_economics"] += 1

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(path),
        "reviewed_epoch_file": (
            str(reviewed_epochs_path) if reviewed_epochs_path is not None else None
        ),
        "report_contract": {
            "unit": (
                "one economically reconciled completed position_lifecycle, "
                "regardless of partial exits or leg count"
            ),
            "authority": "position_lifecycle.net_realized_pnl backed by trades.realized_pnl",
            "approval": "operator_only",
        },
        "cohorts": [
            _summarize(strategy, version, config_hash, rows)
            for (strategy, version, config_hash), rows in sorted(cohorts.items())
        ],
        "unknown_epoch_history": [
            {
                "strategy": strategy,
                **values,
                "realized_pnl": (
                    values["realized_pnl"]
                    if values["trusted_completed"]
                    else None
                ),
            }
            for strategy, values in sorted(unknown.items())
        ],
        "diagnostics": diagnostics,
    }


def render_markdown(report: dict[str, Any]) -> str:
    """Render the JSON contract as a compact operator-facing document."""
    lines = [
        "# Strategy Graduation Evidence",
        "",
        f"Generated: {report['generated_at']}",
        "",
        "> Advisory evidence only. The report never authorizes live trading.",
        "",
    ]
    if not report["cohorts"]:
        lines.extend([
            "No recorded strategy-version cohorts exist yet.",
            "",
        ])
    for cohort in report["cohorts"]:
        perf = cohort["performance"]
        coverage = cohort["coverage"]
        lines.extend([
            f"## {cohort['display_identity']}",
            "",
            f"Status: **{cohort['evidence_status']}** — "
            f"operator decision: `{cohort['operator_decision']}`",
            "",
            f"Trusted completed lifecycles: {coverage['trusted_completed']} "
            f"(unresolved economics: {coverage['unresolved_economics']}; "
            f"open/nonterminal: {coverage['open_or_nonterminal']})",
            (
                f"Gross realized P&L: ${perf['gross_realized_pnl']:,.2f}"
                if perf["gross_realized_pnl"] is not None
                else "Gross realized P&L: unavailable"
            ),
            f"Net after costs: unavailable",
            (
                f"Win rate: {perf['win_rate']:.1%}"
                if perf["win_rate"] is not None
                else "Win rate: unavailable"
            ),
            (
                f"Profit factor: {perf['profit_factor']:.2f}"
                if perf["profit_factor"] is not None
                else "Profit factor: unavailable"
            ),
            f"Realized max drawdown: ${perf['realized_max_drawdown']:,.2f}",
            (
                f"P&L excluding best outcome: "
                f"${perf['pnl_without_best_outcome']:,.2f}"
                if perf["pnl_without_best_outcome"] is not None
                else "P&L excluding best outcome: unavailable"
            ),
            "",
            "Limitations: costs and historical daily mark-to-market are not "
            "recorded; drawdown is realized-only.",
            "",
        ])
    if report["unknown_epoch_history"]:
        lines.extend(["## Unknown-epoch history", ""])
        for item in report["unknown_epoch_history"]:
            pnl_text = (
                f"${item['realized_pnl']:,.2f} realized"
                if item["realized_pnl"] is not None
                else "realized P&L unavailable"
            )
            lines.append(
                f"- `{item['strategy']}`: {item['trusted_completed']} trusted "
                f"completed lifecycle(s), {item['unresolved_economics']} unresolved, "
                f"{pnl_text}; context only, "
                "excluded from cohorts."
            )
        lines.append("")
    return "\n".join(lines)


def write_graduation_report(
    db_path: str | Path,
    output_dir: str | Path,
    *,
    reviewed_epochs_path: str | Path | None = None,
) -> tuple[Path, Path]:
    """Write matching JSON and Markdown evidence artifacts."""
    report = build_graduation_report(
        db_path, reviewed_epochs_path=reviewed_epochs_path
    )
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = destination / f"strategy_graduation_{stamp}.json"
    md_path = destination / f"strategy_graduation_{stamp}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    md_path.write_text(render_markdown(report))
    return json_path, md_path
