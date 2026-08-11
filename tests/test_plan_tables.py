"""
Structural guard for PLAN.md's markdown tables.

PLAN.md is this project's persistent operational memory (CLAUDE.md names
it as required reading before any code change). A row whose cell count
disagrees with its header does not fail loudly — it renders content in
the wrong column, or drops it off the visible table entirely, which is
worse than a missing file because it looks fine.

Three ways it broke on 2026-08-10/11, all while editing unrelated rows:

* An insert appended a trailing ``|`` to text that already followed a
  delimiter, splitting a 3-column row into 4 — silently, four times.
* ``|r|`` (absolute value) and ``` `|fill − new_stop|` ``` were written
  unescaped inside cells, each adding two phantom delimiters.
* A data row was inserted between a table header and its delimiter, causing
  GitHub to render the whole block as prose despite matching cell counts.

And two rows had been malformed for longer: ``11.49`` was split across
lines by a blank line inside a cell, and the MLEG row was missing a
delimiter entirely.

The file has tables of 2, 3 and 5 columns, so a fixed pipe count is not
the test — each row is checked against its own header.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PLAN = Path(__file__).resolve().parent.parent / "PLAN.md"

# A pipe escaped as \| is cell content, not a delimiter.
_PIPE = re.compile(r"(?<!\\)\|")
_DELIMITER_CELL = re.compile(r":?-{3,}:?$")


def cell_count(row: str) -> int:
    """Cells in a markdown row: delimiters minus the leading/trailing pair."""
    return len(_PIPE.split(row)) - 2


def tables() -> list[list[tuple[int, str]]]:
    """Contiguous blocks of table rows, as (1-indexed line number, text)."""
    out: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for lineno, line in enumerate(PLAN.read_text().splitlines(), start=1):
        if line.strip().startswith("|"):
            current.append((lineno, line))
        elif current:
            out.append(current)
            current = []
    if current:
        out.append(current)
    return out


class TestPlanTables:
    def test_plan_exists_and_has_tables(self):
        assert PLAN.exists(), "PLAN.md is required reading; it must exist"
        assert len(tables()) >= 5

    def test_every_row_matches_its_own_header(self):
        """The actual guard. Reported all at once — fixing them one failure
        at a time is how the last round took several passes."""
        problems = []
        for block in tables():
            header_line, header = block[0]
            width = cell_count(header)
            for lineno, row in block:
                got = cell_count(row)
                if got != width:
                    problems.append(
                        f"  PLAN.md:{lineno} has {got} cells, but the table "
                        f"starting at line {header_line} has {width}\n"
                        f"     {row[:90]}"
                    )
        assert not problems, (
            "malformed PLAN.md table row(s) — content will render in the "
            "wrong column:\n" + "\n".join(problems)
        )

    def test_header_is_immediately_followed_by_delimiter(self):
        """Markdown only recognizes a table when its delimiter follows the header.

        A valid-looking data row placed between them turns the entire block into
        prose on GitHub, despite every row having the correct cell count.
        """
        problems = []
        for block in tables():
            if len(block) < 2:
                problems.append(f"PLAN.md:{block[0][0]} table has no delimiter row")
                continue
            lineno, delimiter = block[1]
            cells = [cell.strip() for cell in _PIPE.split(delimiter)[1:-1]]
            if not cells or not all(_DELIMITER_CELL.fullmatch(cell) for cell in cells):
                problems.append(
                    f"PLAN.md:{lineno} must be the delimiter immediately after "
                    f"the header at line {block[0][0]}"
                )
        assert not problems, "malformed PLAN.md table header(s):\n" + "\n".join(problems)

    def test_no_blank_line_splits_a_table(self):
        """A blank line inside a cell ends the table and orphans the rest.
        That is what happened to 11.49: its acceptance text became a loose
        paragraph between two table fragments. Use <br> inside a cell."""
        lines = PLAN.read_text().splitlines()
        orphans = [
            i + 1
            for i, l in enumerate(lines)
            if l.strip()
            and not l.strip().startswith("|")
            and i > 0
            and lines[i - 1].strip().startswith("|")
            and not l.startswith("#")
            and not l.startswith("---")
            and not l.startswith(">")
        ]
        assert not orphans, (
            f"line(s) {orphans} follow a table row without being one — a "
            "blank line inside a cell splits the table"
        )

    @pytest.mark.parametrize("literal", ["|r|", "|fill", "new_stop|"])
    def test_known_in_cell_pipes_stay_escaped(self, literal):
        """These specific literals broke the table once each. If they come
        back UNESCAPED inside a row, catch them by name.

        The match must be lookbehind-anchored: a plain substring test also
        matches the correctly-escaped `\\|fill`, since that contains
        `|fill`. The first version of this test failed on the very fix it
        was written to protect.
        """
        pattern = re.compile(r"(?<!\\)" + re.escape(literal))
        for block in tables():
            for lineno, row in block:
                assert not pattern.search(row), (
                    f"PLAN.md:{lineno} contains an unescaped {literal!r} "
                    "inside a table cell — escape each pipe as \\| "
                )
