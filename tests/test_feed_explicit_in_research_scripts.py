"""
Repository-level safety net (PR #50 review P1): every fetch_symbol /
fetch_symbols call in research and execution-replay code MUST pass an
explicit ``feed=`` kwarg. If not, the call silently inherits the live engine
default (``ALPACA_DATA_FEED`` = ``"iex"`` on paper), which means:

  - Research scripts silently use IEX even though SIP is the documented
    default. Volume thresholds get evaluated against IEX's ~2-3% slice of
    consolidated volume (scaled 20× by ``apply_synthetic_sip_volume``),
    which is acceptable for live engine on paper but not for offline
    analysis aiming at consolidated-tape semantics.
  - Execution-replay scripts that should stay on IEX could accidentally
    drift to SIP if they're moved or refactored; the explicit ``feed=``
    keyword keeps the intent visible at the call site.

This test walks Python source files in ``scripts/`` and ``backtest/`` with
the ``ast`` module, finds every ``fetch_symbol(...)`` and
``fetch_symbols(...)`` call, and asserts it has a ``feed=`` keyword
argument. Files that don't fit either category (or are excluded for known
reasons) are listed in ``_EXEMPT_FILES`` with a justification comment.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


# Files explicitly exempt from the explicit-feed requirement. Add a comment
# next to each entry justifying the exemption — usually because the file
# doesn't actually call the fetcher (just imports it), or it's vendored /
# historical and not part of the active research surface.
_EXEMPT_FILES: dict[str, str] = {
    # Legacy verification scripts predate the feed-aware contract. They are
    # run manually against paper, not part of the offline-research workflow,
    # and any future replacement should adopt the explicit-feed pattern.
    "scripts/legacy_verify/phase7_verify.py": "legacy verification, manual paper run",
    "scripts/legacy_verify/phase8_verify.py": "legacy verification, manual paper run",
    # These verification scripts hit Alpaca directly (paper account) and use
    # the live engine's feed by design — they validate live behavior, not
    # research hypotheses. If they grow into research tools, flip them.
    "scripts/verify_credit_spread.py": "validates live credit-spread behavior",
    "scripts/verify_spread_order.py": "validates live spread-order behavior",
}


# Search roots for research / replay code that calls the fetcher.
_SEARCH_DIRS = ("scripts", "backtest")

# Function names whose calls require an explicit feed kwarg.
_REQUIRES_FEED = frozenset({"fetch_symbol", "fetch_symbols"})


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for sub in _SEARCH_DIRS:
        files.extend((REPO_ROOT / sub).rglob("*.py"))
    return files


def _is_exempt(path: Path) -> bool:
    rel = path.relative_to(REPO_ROOT).as_posix()
    return rel in _EXEMPT_FILES


def _find_calls_missing_feed(tree: ast.AST) -> list[tuple[str, int]]:
    """Return [(callee_name, line_no), ...] for fetcher calls missing ``feed=``."""
    missing: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        # Match both bare `fetch_symbol(...)` and attribute `fetcher.fetch_symbol(...)`.
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        else:
            continue
        if name not in _REQUIRES_FEED:
            continue
        kw_names = {kw.arg for kw in node.keywords if kw.arg}
        if "feed" not in kw_names:
            missing.append((name, node.lineno))
    return missing


def test_every_fetcher_call_in_research_or_replay_has_explicit_feed() -> None:
    """
    For every fetch_symbol / fetch_symbols call in scripts/ and backtest/,
    the call must include `feed=...`. Exemptions live in _EXEMPT_FILES with
    documented justification.
    """
    failures: list[str] = []

    for path in _iter_python_files():
        if _is_exempt(path):
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            failures.append(f"{path.relative_to(REPO_ROOT)}: failed to parse — {exc}")
            continue
        for name, lineno in _find_calls_missing_feed(tree):
            failures.append(
                f"{path.relative_to(REPO_ROOT)}:{lineno}: "
                f"{name}() call has no `feed=` kwarg. "
                f"Research scripts must pass `feed=settings.BACKTEST_DATA_FEED`; "
                f"execution-replay scripts must pass `feed=settings.ALPACA_DATA_FEED` "
                f"explicitly (see CLAUDE.md / AGENTS.md feed-strategy section). "
                f"If this call doesn't fit either category, add the file to "
                f"_EXEMPT_FILES in this test with a justification."
            )

    if failures:
        msg = "\n".join(failures)
        raise AssertionError(
            f"{len(failures)} fetcher call(s) missing explicit `feed=`:\n{msg}"
        )


def test_exempt_files_actually_exist() -> None:
    """Catch dead exemptions when a file is renamed or removed."""
    missing = [
        rel for rel in _EXEMPT_FILES
        if not (REPO_ROOT / rel).exists()
    ]
    assert not missing, (
        f"_EXEMPT_FILES references non-existent paths: {missing}. "
        f"Update the exemption list when files are renamed or removed."
    )
