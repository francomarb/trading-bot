"""
Unit tests for the dashboard's "Strategy Health & Edge" panel
(PLAN 11.10g).

The panel reads from data/health_state.json + data/health_reports/.
Streamlit calls are mocked so tests don't need a running app —
we just verify the helper functions degrade gracefully and load
the right data.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from dashboard import (
    _find_latest_report,
    _format_generated_at,
    _load_health_state,
    _parse_report_metadata,
)


# ── _load_health_state ────────────────────────────────────────────────


class TestLoadHealthState:
    def test_returns_none_when_file_missing(self, monkeypatch, tmp_path):
        """First run after install — file doesn't exist yet."""
        # Patch Path(__file__).resolve().parent at the dashboard
        # module level to point at tmp_path so the absent file
        # resolves under tmp.
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        assert _load_health_state() is None

    def test_returns_none_on_malformed_json(self, monkeypatch, tmp_path):
        """Streamlit refresh must not crash on a partial-write or
        corrupt state file. Just degrade to empty panel state."""
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        (tmp_path / "data").mkdir()
        (tmp_path / "data" / "health_state.json").write_text(
            "{not valid json",
        )
        assert _load_health_state() is None

    def test_returns_strategies_dict_skipping_schema_version(
        self, monkeypatch, tmp_path,
    ):
        """Schema version is metadata, not a strategy. Filter it out
        so the panel iteration only sees real strategies."""
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        (tmp_path / "data").mkdir()
        payload = {
            "schema_version": 1,
            "donchian_breakout": {
                "negative_weeks": 0,
                "last_check": "2026-05-17",
                "last_verdict": "POSITIVE",
            },
            "rsi_reversion": {
                "negative_weeks": 3,
                "last_check": "2026-05-17",
                "last_verdict": "NEGATIVE",
            },
        }
        (tmp_path / "data" / "health_state.json").write_text(
            json.dumps(payload),
        )
        state = _load_health_state()
        assert state is not None
        assert "schema_version" not in state
        assert "donchian_breakout" in state
        assert "rsi_reversion" in state
        assert state["rsi_reversion"]["negative_weeks"] == 3


# ── _find_latest_report ───────────────────────────────────────────────


class TestFindLatestReport:
    def test_returns_none_when_dir_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        assert _find_latest_report("weekly_*.md") is None
        assert _find_latest_report("monthly_*.md") is None

    def test_returns_none_when_dir_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        (tmp_path / "data" / "health_reports").mkdir(parents=True)
        assert _find_latest_report("weekly_*.md") is None
        assert _find_latest_report("monthly_*.md") is None

    def test_returns_most_recent_by_mtime(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        reports = tmp_path / "data" / "health_reports"
        reports.mkdir(parents=True)
        import time as _time
        for name in ["weekly_2026-W19.md", "weekly_2026-W20.md", "weekly_2026-W21.md"]:
            p = reports / name
            p.write_text(f"# {name}")
            _time.sleep(0.01)  # ensure distinct mtimes
        latest = _find_latest_report("weekly_*.md")
        assert latest is not None
        assert latest.name == "weekly_2026-W21.md"

    def test_weekly_pattern_excludes_monthly_files(
        self, monkeypatch, tmp_path,
    ):
        """The two patterns are disjoint — `weekly_*.md` must not pick
        up `monthly_*.md` and vice versa."""
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        reports = tmp_path / "data" / "health_reports"
        reports.mkdir(parents=True)
        (reports / "monthly_2026-05.md").write_text("monthly")
        (reports / "weekly_2026-W20.md").write_text("weekly")
        weekly = _find_latest_report("weekly_*.md")
        monthly = _find_latest_report("monthly_*.md")
        assert weekly is not None and weekly.name == "weekly_2026-W20.md"
        assert monthly is not None and monthly.name == "monthly_2026-05.md"

    def test_backup_files_are_excluded(self, monkeypatch, tmp_path):
        """Operator backups (e.g. weekly_*.md.bak.<utc>) match `weekly_*.md`
        via fnmatch but must not surface as the canonical report."""
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        reports = tmp_path / "data" / "health_reports"
        reports.mkdir(parents=True)
        # The .bak file's mtime is newer, but the canonical file must win.
        canonical = reports / "weekly_2026-W22.md"
        canonical.write_text("canonical")
        import time as _time
        _time.sleep(0.01)
        backup = reports / "weekly_2026-W22.md.bak.20260602-120000"
        backup.write_text("backup")
        latest = _find_latest_report("weekly_*.md")
        assert latest is not None
        assert latest.name == "weekly_2026-W22.md"


# ── _parse_report_metadata ────────────────────────────────────────────


class TestParseReportMetadata:
    def test_extracts_front_matter_and_strips_body(self):
        text = (
            "---\n"
            "schema_version: 1\n"
            "period_start: 2026-05-25\n"
            "period_end: 2026-06-01\n"
            "period_type: weekly\n"
            "generated_at: 2026-06-02T12:30:00+00:00\n"
            "---\n"
            "# Strategy Health & Edge Report — Week 2026-W22\n"
        )
        metadata, body = _parse_report_metadata(text)
        assert metadata["period_start"] == "2026-05-25"
        assert metadata["period_end"] == "2026-06-01"
        assert metadata["period_type"] == "weekly"
        # Body must start at the title — front-matter removed entirely
        # so the operator-facing render doesn't show YAML cruft.
        assert body.startswith("# Strategy Health & Edge Report")
        assert "schema_version" not in body
        assert "---" not in body[:5]

    def test_handles_missing_front_matter(self):
        # Legacy or malformed report without YAML — return whole body
        # and an empty metadata dict so the panel still renders.
        text = "# Just a markdown title\n\nBody."
        metadata, body = _parse_report_metadata(text)
        assert metadata == {}
        assert body == text

    def test_skips_keyless_lines(self):
        # A YAML line without ":" should not crash the simple parser.
        text = (
            "---\n"
            "period_start: 2026-05-25\n"
            "this_is_a_garbage_line\n"
            "period_end: 2026-06-01\n"
            "---\n"
            "body"
        )
        metadata, body = _parse_report_metadata(text)
        assert metadata["period_start"] == "2026-05-25"
        assert metadata["period_end"] == "2026-06-01"
        assert body == "body"


# ── _format_generated_at ──────────────────────────────────────────────


class TestFormatGeneratedAt:
    def test_renders_iso_as_compact_local_time(self):
        # The reviewer writes ISO with +00:00 — display in the host's local zone.
        out = _format_generated_at("2026-06-02T12:30:00+00:00")
        assert out == "2026-06-02 07:30 CDT"

    def test_converts_offset_to_local_time(self):
        # Non-UTC offsets should still normalize to the host's local zone.
        out = _format_generated_at("2026-06-02T08:30:00-04:00")
        assert out == "2026-06-02 07:30 CDT"

    def test_renders_naive_as_utc_then_converts_local(self):
        # Some legacy reports may omit the offset; assume UTC, then convert local.
        out = _format_generated_at("2026-06-02T12:30:00")
        assert out == "2026-06-02 07:30 CDT"

    def test_falls_back_on_unparseable(self):
        # Malformed timestamp — surface the raw value rather than raise.
        out = _format_generated_at("not-a-date")
        assert out == "not-a-date"

    def test_empty_renders_as_dash(self):
        assert _format_generated_at("") == "—"
