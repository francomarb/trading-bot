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
    _find_latest_weekly_report,
    _load_health_state,
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


# ── _find_latest_weekly_report ────────────────────────────────────────


class TestFindLatestWeeklyReport:
    def test_returns_none_when_dir_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        assert _find_latest_weekly_report() is None

    def test_returns_none_when_dir_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        (tmp_path / "data" / "health_reports").mkdir(parents=True)
        assert _find_latest_weekly_report() is None

    def test_returns_most_recent_by_mtime(self, monkeypatch, tmp_path):
        """Three weekly reports — should return the one with the
        latest mtime."""
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
        latest = _find_latest_weekly_report()
        assert latest is not None
        assert latest.name == "weekly_2026-W21.md"

    def test_only_returns_weekly_files(self, monkeypatch, tmp_path):
        """Monthly files should not be considered by the weekly-
        report finder."""
        monkeypatch.setattr(
            "dashboard.__file__", str(tmp_path / "dashboard.py"),
        )
        reports = tmp_path / "data" / "health_reports"
        reports.mkdir(parents=True)
        (reports / "monthly_2026-05.md").write_text("monthly")
        (reports / "weekly_2026-W20.md").write_text("weekly")
        latest = _find_latest_weekly_report()
        assert latest is not None
        assert latest.name == "weekly_2026-W20.md"
