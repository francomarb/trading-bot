"""
Repository-level safety net (PR #50 review P1 — both rounds): every
fetch_symbol / fetch_symbols call **anywhere in the repo** outside an
explicit allowlist MUST pass an explicit ``feed=`` kwarg. If not, the call
silently inherits ``ALPACA_DATA_FEED`` (= ``"iex"`` on paper), which means:

  - Research scripts silently use IEX even though SIP is the documented
    default. Volume thresholds get evaluated against IEX's ~2-3% slice of
    consolidated volume (scaled 20× by ``apply_synthetic_sip_volume``),
    which is acceptable for live engine on paper but not for offline
    analysis aiming at consolidated-tape semantics.
  - Live-engine call sites that should track ``ALPACA_DATA_FEED`` lose
    their declared intent — if someone flips the env var, you can't tell
    which path was supposed to follow.
  - Execution-replay scripts that should stay on IEX could accidentally
    drift to SIP if they're moved or refactored.

The original PR #50 review caught that scanning only ``scripts/`` and
``backtest/`` missed research code elsewhere (specifically
``strategies/health/benchmarks.py``). This expanded version scans the
**entire repository** with an explicit-exempt allowlist.

Every fetcher call site must therefore:

  1. Pass ``feed=settings.BACKTEST_DATA_FEED`` if it's research /
     calibration / audit work.
  2. Pass ``feed=settings.ALPACA_DATA_FEED`` if it's live-engine code or
     execution-replay work.
  3. Or live in ``_EXEMPT_FILES`` with documented justification.
"""

from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


# Files explicitly exempt from the explicit-feed requirement. Add a comment
# next to each entry justifying the exemption.
_EXEMPT_FILES: dict[str, str] = {
    # The fetcher itself owns the feed parameter; calls inside its
    # implementation are recursive / helper paths, not call sites that
    # should declare intent.
    "data/fetcher.py": "fetcher implementation; owns the feed parameter",
    # Legacy verification scripts predate the feed-aware contract. They are
    # run manually against paper, not part of the active offline-research
    # workflow. Any future replacement should adopt the explicit-feed pattern.
    "scripts/legacy_verify/phase7_verify.py": "legacy verification, manual paper run",
    "scripts/legacy_verify/phase8_verify.py": "legacy verification, manual paper run",
    # Verification scripts hit Alpaca paper directly to confirm SDK behaviour.
    # They use the live engine's feed by design; if they grow into research
    # tools, flip them and remove from this list.
    "scripts/verify_credit_spread.py": "validates live credit-spread behavior",
    "scripts/verify_spread_order.py": "validates live spread-order behavior",
}

# Directories whose .py files are skipped entirely. Tests monkeypatch the
# fetcher so explicit feed= is not meaningful there.
_SKIP_DIRS: tuple[str, ...] = ("tests", "venv", ".venv", "__pycache__")

# Function names whose calls require an explicit feed kwarg.
_REQUIRES_FEED = frozenset({"fetch_symbol", "fetch_symbols"})


def _iter_python_files() -> list[Path]:
    files: list[Path] = []
    for path in REPO_ROOT.rglob("*.py"):
        # Skip files anywhere under one of the SKIP dirs (relative).
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(part in _SKIP_DIRS for part in rel_parts):
            continue
        files.append(path)
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


def test_every_fetcher_call_in_repo_has_explicit_feed() -> None:
    """
    For every fetch_symbol / fetch_symbols call anywhere in the repo
    (outside _SKIP_DIRS and _EXEMPT_FILES), the call must include
    ``feed=``. The choice between ``settings.BACKTEST_DATA_FEED`` (research)
    and ``settings.ALPACA_DATA_FEED`` (live engine / execution-replay) is
    documented in CLAUDE.md / AGENTS.md "feed strategy" section.
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
                f"Research code must pass `feed=settings.BACKTEST_DATA_FEED`; "
                f"live engine or execution-replay code must pass "
                f"`feed=settings.ALPACA_DATA_FEED` explicitly "
                f"(see CLAUDE.md / AGENTS.md feed-strategy section). "
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
