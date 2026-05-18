"""
Unit tests for strategies/health/persistence.py.

Coverage:
  - PersistenceState.apply_verdict state machine: NEGATIVE
    increments, anything else resets, same-day same-verdict is
    idempotent.
  - HealthStateFile JSON round-trip including default + forward-compat
    unknown-fields.
  - load_state: returns default empty file when missing.
  - save_state: atomic write — tmp cleaned up, parent dir created,
    existing file overwritten cleanly.
  - Operator scenarios: 3-week negative trip + reset, mid-week
    re-assessment, partial file recovery.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from strategies.health.persistence import (
    PERSISTENCE_SCHEMA_VERSION,
    HealthStateFile,
    PersistenceState,
    default_state_path,
    load_state,
    save_state,
)


# ── PersistenceState.apply_verdict state machine ──────────────────────


class TestApplyVerdict:
    def test_negative_increments_from_zero(self):
        s = PersistenceState()
        s = s.apply_verdict("NEGATIVE", "2026-05-17")
        assert s.negative_weeks == 1
        assert s.last_verdict == "NEGATIVE"
        assert s.last_check == "2026-05-17"

    def test_three_consecutive_negative_weeks(self):
        s = PersistenceState()
        s = s.apply_verdict("NEGATIVE", "2026-05-17")
        s = s.apply_verdict("NEGATIVE", "2026-05-24")
        s = s.apply_verdict("NEGATIVE", "2026-05-31")
        assert s.negative_weeks == 3
        # At 3, the EdgeAssessor fires STRATEGY_EDGE_LOSS — verified
        # in 11.10d, but the counter math is the contract.

    def test_positive_resets_counter(self):
        s = PersistenceState(
            negative_weeks=2, last_check="2026-05-24", last_verdict="NEGATIVE"
        )
        s = s.apply_verdict("POSITIVE", "2026-05-31")
        assert s.negative_weeks == 0
        assert s.last_verdict == "POSITIVE"

    @pytest.mark.parametrize(
        "reset_verdict",
        ["POSITIVE", "BELOW_BENCHMARK", "UNDETERMINED"],
    )
    def test_any_non_negative_resets(self, reset_verdict):
        """Per design §9 only NEGATIVE persists; everything else resets."""
        s = PersistenceState(
            negative_weeks=5, last_check="2026-05-24", last_verdict="NEGATIVE"
        )
        s = s.apply_verdict(reset_verdict, "2026-05-31")
        assert s.negative_weeks == 0
        assert s.last_verdict == reset_verdict

    def test_same_day_same_verdict_is_idempotent(self):
        """Re-running the assessor on the same date with the same
        verdict must not double-increment — protects against operator
        running on-demand reports without polluting state."""
        s = PersistenceState()
        s1 = s.apply_verdict("NEGATIVE", "2026-05-17")
        s2 = s1.apply_verdict("NEGATIVE", "2026-05-17")
        assert s1 == s2
        assert s2.negative_weeks == 1

    def test_same_day_different_verdict_updates(self):
        """If somehow a re-run produces a different verdict on the same
        day (envelope changed under us, etc.) the new verdict wins."""
        s = PersistenceState()
        s = s.apply_verdict("NEGATIVE", "2026-05-17")
        s = s.apply_verdict("POSITIVE", "2026-05-17")
        assert s.negative_weeks == 0
        assert s.last_verdict == "POSITIVE"

    def test_date_object_accepted(self):
        s = PersistenceState()
        s = s.apply_verdict("NEGATIVE", date(2026, 5, 17))
        assert s.last_check == "2026-05-17"

    def test_invalid_date_string_rejected(self):
        s = PersistenceState()
        with pytest.raises(ValueError):
            s.apply_verdict("NEGATIVE", "not-a-date")

    def test_negative_after_reset_starts_fresh(self):
        """End-to-end: NEG x2 → POS reset → NEG x3 → alarm count is 3."""
        s = PersistenceState()
        s = s.apply_verdict("NEGATIVE", "2026-04-19")
        s = s.apply_verdict("NEGATIVE", "2026-04-26")
        s = s.apply_verdict("POSITIVE", "2026-05-03")  # reset
        s = s.apply_verdict("NEGATIVE", "2026-05-10")
        s = s.apply_verdict("NEGATIVE", "2026-05-17")
        s = s.apply_verdict("NEGATIVE", "2026-05-24")
        # Fresh NEG run from zero — current count 3.
        assert s.negative_weeks == 3

    def test_frozen_no_mutation(self):
        s = PersistenceState()
        with pytest.raises(Exception):  # FrozenInstanceError
            s.negative_weeks = 99  # type: ignore[misc]


# ── HealthStateFile container ─────────────────────────────────────────


class TestHealthStateFile:
    def test_default_is_empty(self):
        f = HealthStateFile()
        assert f.schema_version == PERSISTENCE_SCHEMA_VERSION
        assert f.states == {}

    def test_get_or_default_for_missing_strategy(self):
        f = HealthStateFile()
        s = f.get_or_default("donchian_breakout")
        assert s == PersistenceState()

    def test_with_updated_returns_new_file(self):
        f = HealthStateFile()
        new_state = PersistenceState(
            negative_weeks=1, last_check="2026-05-17", last_verdict="NEGATIVE"
        )
        f2 = f.with_updated("donchian_breakout", new_state)
        # Original unchanged
        assert f.states == {}
        # New file has the update
        assert f2.states["donchian_breakout"] == new_state

    def test_json_round_trip_empty(self):
        f = HealthStateFile()
        restored = HealthStateFile.from_json(f.to_json())
        assert restored == f

    def test_json_round_trip_with_strategies(self):
        f = HealthStateFile().with_updated(
            "donchian_breakout",
            PersistenceState(2, "2026-05-24", "NEGATIVE"),
        ).with_updated(
            "rsi_reversion",
            PersistenceState(0, "2026-05-24", "POSITIVE"),
        )
        restored = HealthStateFile.from_json(f.to_json())
        assert restored == f

    def test_json_is_sorted_by_strategy(self):
        """Sorted output makes git diffs stable across runs that
        process strategies in different orders."""
        f = HealthStateFile()
        for name in ["zzz", "aaa", "mmm"]:
            f = f.with_updated(
                name, PersistenceState(0, "2026-05-17", "POSITIVE")
            )
        text = f.to_json()
        # Extract the strategy keys in order they appear in the JSON.
        ordered_keys = [
            k for k in json.loads(text).keys() if k != "schema_version"
        ]
        assert ordered_keys == ["aaa", "mmm", "zzz"]

    def test_forward_compat_unknown_fields_ignored(self):
        """Older bot reads newer state file — unknown per-strategy
        fields silently ignored, no crash."""
        text = json.dumps({
            "schema_version": 99,
            "donchian_breakout": {
                "negative_weeks": 1,
                "last_check": "2026-05-17",
                "last_verdict": "NEGATIVE",
                "future_field_we_dont_know": "ignored",
                "another_future_metric": 42,
            },
        })
        f = HealthStateFile.from_json(text)
        # Schema version preserved (so a newer bot can decide what to do)
        assert f.schema_version == 99
        # Per-strategy known fields preserved; unknown silently dropped
        assert f.states["donchian_breakout"].negative_weeks == 1

    def test_non_dict_top_level_keys_ignored(self):
        """A future version might add top-level scalar fields. They
        shouldn't be misinterpreted as strategy blobs."""
        text = json.dumps({
            "schema_version": PERSISTENCE_SCHEMA_VERSION,
            "donchian_breakout": {"negative_weeks": 1, "last_check": "2026-05-17", "last_verdict": "NEGATIVE"},
            "future_global_metadata": "some_value",  # scalar, not a dict
        })
        f = HealthStateFile.from_json(text)
        assert "donchian_breakout" in f.states
        assert "future_global_metadata" not in f.states


# ── File I/O ──────────────────────────────────────────────────────────


class TestFileIO:
    def test_load_missing_returns_default(self, tmp_path: Path):
        """First run after install — file doesn't exist yet."""
        f = load_state(tmp_path / "health_state.json")
        assert f == HealthStateFile()

    def test_save_then_load(self, tmp_path: Path):
        f = HealthStateFile().with_updated(
            "donchian_breakout",
            PersistenceState(2, "2026-05-24", "NEGATIVE"),
        )
        path = tmp_path / "health_state.json"
        save_state(f, path)
        loaded = load_state(path)
        assert loaded == f

    def test_save_creates_parent_directory(self, tmp_path: Path):
        """Operator-friendly: first run can target a path under a
        directory that doesn't exist yet."""
        nested = tmp_path / "deep" / "subdir"
        path = nested / "health_state.json"
        save_state(HealthStateFile(), path)
        assert path.exists()

    def test_atomic_write_no_tmp_leftover(self, tmp_path: Path):
        """After a successful write, the .tmp scratch file is gone."""
        path = tmp_path / "health_state.json"
        save_state(HealthStateFile(), path)
        assert path.exists()
        assert not path.with_suffix(path.suffix + ".tmp").exists()

    def test_save_overwrites_existing(self, tmp_path: Path):
        path = tmp_path / "health_state.json"
        f1 = HealthStateFile().with_updated(
            "x", PersistenceState(1, "2026-05-17", "NEGATIVE")
        )
        f2 = HealthStateFile().with_updated(
            "y", PersistenceState(0, "2026-05-24", "POSITIVE")
        )
        save_state(f1, path)
        save_state(f2, path)
        loaded = load_state(path)
        assert loaded == f2
        assert "x" not in loaded.states
        assert "y" in loaded.states

    def test_default_path_is_under_data_dir(self):
        p = default_state_path()
        assert p.name == "health_state.json"
        assert p.parent.name == "data"


# ── Operator scenario walkthroughs ────────────────────────────────────


class TestOperatorScenarios:
    """End-to-end state evolution mirroring how the EdgeAssessor + reviewer
    use this module across weeks of paper operation."""

    def test_three_week_negative_to_alarm_trip(self, tmp_path: Path):
        """Week-by-week: assessor reads state, applies verdict, saves.
        After three NEGATIVE weeks the alarm count hits 3."""
        path = tmp_path / "state.json"
        strategy = "donchian_breakout"

        # Week 1 (initial state)
        state = load_state(path)
        s = state.get_or_default(strategy)
        s = s.apply_verdict("NEGATIVE", "2026-05-17")
        save_state(state.with_updated(strategy, s), path)

        # Week 2 — new process / new assessor instance / new memory
        state = load_state(path)
        s = state.get_or_default(strategy)
        assert s.negative_weeks == 1
        s = s.apply_verdict("NEGATIVE", "2026-05-24")
        save_state(state.with_updated(strategy, s), path)

        # Week 3 — alarm should fire after this update
        state = load_state(path)
        s = state.get_or_default(strategy)
        assert s.negative_weeks == 2
        s = s.apply_verdict("NEGATIVE", "2026-05-31")
        save_state(state.with_updated(strategy, s), path)

        # Verify persisted
        state = load_state(path)
        assert state.get_or_default(strategy).negative_weeks == 3

    def test_recovery_resets_counter(self, tmp_path: Path):
        """Two NEG weeks then a POSITIVE — counter must reset so a
        future bad period starts from zero."""
        path = tmp_path / "state.json"
        strategy = "rsi_reversion"

        for verdict, check_date in [
            ("NEGATIVE", "2026-04-26"),
            ("NEGATIVE", "2026-05-03"),
            ("POSITIVE", "2026-05-10"),  # reset
        ]:
            state = load_state(path)
            s = state.get_or_default(strategy)
            s = s.apply_verdict(verdict, check_date)
            save_state(state.with_updated(strategy, s), path)

        loaded = load_state(path)
        assert loaded.get_or_default(strategy).negative_weeks == 0
        assert loaded.get_or_default(strategy).last_verdict == "POSITIVE"

    def test_multiple_strategies_independent_state(self, tmp_path: Path):
        """Each strategy's persistence state must be independent —
        SMA going NEGATIVE doesn't affect RSI's count."""
        path = tmp_path / "state.json"

        state = load_state(path)
        state = state.with_updated(
            "sma_crossover",
            PersistenceState().apply_verdict("NEGATIVE", "2026-05-17"),
        )
        state = state.with_updated(
            "rsi_reversion",
            PersistenceState().apply_verdict("POSITIVE", "2026-05-17"),
        )
        save_state(state, path)

        loaded = load_state(path)
        assert loaded.get_or_default("sma_crossover").negative_weeks == 1
        assert loaded.get_or_default("rsi_reversion").negative_weeks == 0
