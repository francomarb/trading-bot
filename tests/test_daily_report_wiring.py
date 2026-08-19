"""
Guards on the EOD daily-report call site in `forward_test.py`.

The defect these exist for: `generate_daily_summary` accepted
`unrealized_pnl` (and later `max_intraday_drawdown`) as keyword
arguments defaulting to 0.0, and the shutdown handler in
`forward_test.main()` — the only production caller — never passed
either. Nothing failed, nothing warned; all 71 daily reports written
between 2026-04 and 2026-08-19 simply said

    | Unrealized P&L        | $0.00 |
    | Max intraday drawdown | $0.00 |

on days that ended with a dozen open positions and a four-figure equity
swing.

Unit tests on `PnLTracker` cannot catch that class of bug: the module
was correct in isolation and stayed correct the whole time. Only the
caller was wrong, so the assertion has to be about the caller. These
are AST checks against `forward_test.py` for the same reason the
logger-sink guards in `test_allowed_regimes_parity.py` are — the
shutdown block lives inside `main()`'s `finally`, behind a live broker
connection, and cannot be invoked from a unit test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from config import settings


FORWARD_TEST = Path(settings.__file__).parent.parent / "forward_test.py"

# Both fields are unavailable from the trade log — unrealized P&L is a
# broker mark-to-market of open positions, and the drawdown is an equity
# path sampled across the session — so each must be supplied explicitly
# at the call site or it silently reverts to the 0.0 default.
REQUIRED_KWARGS = ("unrealized_pnl", "max_intraday_drawdown")


def _daily_summary_call() -> ast.Call:
    """The single `generate_daily_summary(...)` call in forward_test.py."""
    tree = ast.parse(FORWARD_TEST.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "generate_daily_summary"
    ]
    assert len(calls) == 1, (
        f"expected exactly one generate_daily_summary call in "
        f"{FORWARD_TEST.name}, found {len(calls)}"
    )
    return calls[0]


class TestDailySummaryCallSite:
    @pytest.mark.parametrize("kwarg", REQUIRED_KWARGS)
    def test_kwarg_is_passed(self, kwarg: str):
        passed = {kw.arg for kw in _daily_summary_call().keywords}
        assert kwarg in passed, (
            f"forward_test.py calls generate_daily_summary() without "
            f"{kwarg!r}, so it falls back to the 0.0 default and the daily "
            f"report prints $0.00 for it regardless of the real book. This "
            f"is the exact defect that made 71 consecutive reports wrong."
        )

    @pytest.mark.parametrize("kwarg", REQUIRED_KWARGS)
    def test_kwarg_is_not_a_hardcoded_constant(self, kwarg: str):
        """Passing a literal would satisfy the check above while still
        reporting a fiction — it has to come from live state."""
        value = next(
            kw.value for kw in _daily_summary_call().keywords if kw.arg == kwarg
        )
        assert not isinstance(value, ast.Constant), (
            f"forward_test.py passes a hardcoded {ast.dump(value)} for "
            f"{kwarg!r}; it must be derived from the broker snapshot or the "
            f"engine's equity path, not written into the source."
        )

    def test_unrealized_comes_from_the_broker_snapshot_positions(self):
        value = next(
            kw.value
            for kw in _daily_summary_call().keywords
            if kw.arg == "unrealized_pnl"
        )
        source = ast.unparse(value)
        assert "unrealized_pnl_from_positions" in source, source
        assert "open_positions" in source, source

    def test_drawdown_comes_from_the_engine(self):
        value = next(
            kw.value
            for kw in _daily_summary_call().keywords
            if kw.arg == "max_intraday_drawdown"
        )
        assert ast.unparse(value) == "engine.max_intraday_drawdown"

    def test_the_shutdown_snapshot_is_observed_before_it_is_reported(self):
        """`engine.max_intraday_drawdown` is only as current as the last
        equity observation. The shutdown handler takes a fresh snapshot
        for `session_end_equity`; if that snapshot is not also fed to
        the equity path, a decline between the final cycle and shutdown
        is missing from the very report summarising the session — peak
        $100k, last cycle $100k, shutdown $95k still prints $0.00.

        Asserted on order of statements, because "it is called" is not
        the property that matters — "it is called before the summary is
        built" is.
        """
        tree = ast.parse(FORWARD_TEST.read_text())
        main = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )

        observe_lines = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_observe_equity"
        ]
        assert observe_lines, (
            "forward_test.main() never feeds the shutdown snapshot to "
            "engine._observe_equity, so the reported drawdown stops at the "
            "last completed cycle."
        )

        summary_line = _daily_summary_call().lineno
        sync_lines = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "sync_with_broker"
        ]
        shutdown_sync = max(l for l in sync_lines if l < summary_line)

        assert any(shutdown_sync < l < summary_line for l in observe_lines), (
            f"_observe_equity (lines {observe_lines}) must be called between "
            f"the shutdown sync_with_broker (line {shutdown_sync}) and the "
            f"generate_daily_summary call (line {summary_line})."
        )

    def test_the_broker_equity_path_is_reconciled_before_reporting(self):
        """Per-cycle sampling cannot see equity that moved while no
        process was running, so the broker's own intraday series is
        folded in before the report is built. Ordering again: after the
        engine's own final observation, before the summary."""
        tree = ast.parse(FORWARD_TEST.read_text())
        main = next(
            n for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "main"
        )

        reconcile_lines = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "reconcile_intraday_drawdown_from_broker"
        ]
        assert reconcile_lines, (
            "forward_test.main() never calls "
            "engine.reconcile_intraday_drawdown_from_broker, so a drawdown "
            "reached entirely while the bot was down is missing from the "
            "day's report."
        )

        observe_lines = [
            node.lineno
            for node in ast.walk(main)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_observe_equity"
        ]
        summary_line = _daily_summary_call().lineno

        assert any(
            max(observe_lines) < l < summary_line for l in reconcile_lines
        ), (
            f"reconcile_intraday_drawdown_from_broker (lines "
            f"{reconcile_lines}) must run after the final _observe_equity "
            f"(lines {observe_lines}) and before generate_daily_summary "
            f"(line {summary_line})."
        )


class TestRemovedInMemoryAccumulator:
    """`record_trade_pnl` fed an in-memory list that production never
    populated. It was the origin of both the "P&L=$+0.00, trades=0" EOD
    bug and the dead drawdown field. It is gone; keep it gone."""

    def test_pnl_tracker_no_longer_exposes_record_trade_pnl(self):
        from reporting.pnl import PnLTracker

        assert not hasattr(PnLTracker, "record_trade_pnl"), (
            "record_trade_pnl is back. Realized P&L must come from the "
            "trade log (restart-safe) and drawdown from the engine's "
            "equity path — an in-memory accumulator reintroduces a "
            "number that a midday bot recycle silently zeroes."
        )

    def test_no_module_calls_or_defines_it(self):
        """Including the legacy verify script, which used to inject two
        synthetic trades and thereby 'verify' a path the bot never ran.

        Matched via AST, so the docstrings and commit-history comments
        that explain *why* it was removed do not count as references —
        only a real definition or attribute access does.
        """
        root = FORWARD_TEST.parent
        offenders: list[str] = []
        for path in sorted(root.rglob("*.py")):
            if "venv" in path.parts:
                continue
            try:
                tree = ast.parse(path.read_text())
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                is_def = (
                    isinstance(node, ast.FunctionDef)
                    and node.name == "record_trade_pnl"
                )
                is_access = (
                    isinstance(node, ast.Attribute)
                    and node.attr == "record_trade_pnl"
                )
                if is_def or is_access:
                    offenders.append(
                        f"{path.relative_to(root)}:{node.lineno}"
                    )
        assert not offenders, f"record_trade_pnl still referenced at: {offenders}"
