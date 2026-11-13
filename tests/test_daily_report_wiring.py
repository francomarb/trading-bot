"""Guards on production wiring for market-session daily P&L reports."""

from __future__ import annotations

import ast
from pathlib import Path

from config import settings


FORWARD_TEST = Path(settings.__file__).parent.parent / "forward_test.py"

def _main_function() -> ast.FunctionDef:
    tree = ast.parse(FORWARD_TEST.read_text())
    return next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    )


class TestDailyReportSchedulerWiring:
    """The report must follow the market session, not process lifetime."""

    def test_forward_test_constructs_exactly_one_scheduler(self):
        calls = [
            node
            for node in ast.walk(_main_function())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "DailyPnLReportScheduler"
        ]
        assert len(calls) == 1

    def test_scheduler_is_installed_as_a_post_cycle_hook(self):
        assignments = [
            node
            for node in ast.walk(_main_function())
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name)
                and target.id == "post_cycle_hooks"
                for target in node.targets
            )
        ]
        assert len(assignments) == 1
        value = assignments[0].value
        assert isinstance(value, ast.List)
        names = {
            element.id
            for element in value.elts
            if isinstance(element, ast.Name)
        }
        assert "daily_pnl_scheduler" in names

    def test_forward_test_does_not_build_reports_directly(self):
        forbidden = {
            "generate_daily_summary",
            "write_daily_report",
            "eod_summary",
        }
        calls = {
            node.func.attr
            for node in ast.walk(_main_function())
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
        }
        assert calls.isdisjoint(forbidden), (
            "forward_test.main() must delegate report timing and creation "
            "to DailyPnLReportScheduler"
        )

    def test_shutdown_only_invokes_the_idempotent_scheduler(self):
        main = _main_function()
        try_node = next(
            node
            for node in ast.walk(main)
            if isinstance(node, ast.Try) and node.finalbody
        )
        scheduler_calls = [
            node
            for statement in try_node.finalbody
            for node in ast.walk(statement)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "daily_pnl_scheduler"
        ]
        assert len(scheduler_calls) == 1


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
