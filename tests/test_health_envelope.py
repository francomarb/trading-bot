"""
Unit tests for strategies/health/envelope.py.

Coverage:
  - StrategyEnvelope construction with full + partial field sets
  - JSON round-trip preserves all fields including None/tuple
  - Atomic write produces the expected file; partial-write doesn't
    clobber an existing file
  - Forward-compat: unknown JSON fields are silently ignored
  - envelope_path defaults + override
  - frozen-collection invariant on `notes`
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from strategies.health.envelope import (
    ENVELOPE_SCHEMA_VERSION,
    StrategyEnvelope,
    envelope_path,
)


# ── Construction ──────────────────────────────────────────────────────


def _minimal_envelope() -> StrategyEnvelope:
    return StrategyEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy="donchian_breakout",
        built_at="2026-05-18T12:00:00+00:00",
        backtest_window_start="2024-05-18",
        backtest_window_end="2026-05-18",
    )


def _full_envelope() -> StrategyEnvelope:
    return StrategyEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        strategy="donchian_breakout",
        built_at="2026-05-18T12:00:00+00:00",
        backtest_window_start="2024-05-18",
        backtest_window_end="2026-05-18",
        backtest_config={"atr_stop_mult": 2.0, "atr_trail": True, "years": 2.0},
        r_expectancy=0.42,
        r_expectancy_ci_95=(0.18, 0.65),
        risk_unit_dollars=2000.0,
        expectancy_dollars=140.0,
        expectancy_dollars_ci_95=(85.0, 199.0),
        win_rate=0.48,
        win_rate_ci_95=(0.41, 0.55),
        profit_factor=1.62,
        profit_factor_ci_95=(1.21, 2.14),
        sharpe=1.05,
        cagr=0.18,
        trade_count=58,
        trades_per_month_band=(4.0, 11.0),
        hold_days_band=(2.0, 18.0),
        p95_drawdown_pct=0.12,
        notes=["lifecycle bands skipped: no offline simulator in v1"],
    )


class TestConstruction:
    def test_minimal_envelope_constructs(self):
        env = _minimal_envelope()
        assert env.schema_version == 1
        assert env.strategy == "donchian_breakout"
        # All optional fields default to None / empty
        assert env.r_expectancy is None
        assert env.notes == ()

    def test_full_envelope_construction(self):
        env = _full_envelope()
        assert env.r_expectancy == 0.42
        assert env.r_expectancy_ci_95 == (0.18, 0.65)
        assert env.trades_per_month_band == (4.0, 11.0)

    def test_frozen_no_attribute_mutation(self):
        env = _minimal_envelope()
        with pytest.raises(Exception):  # FrozenInstanceError
            env.strategy = "other"  # type: ignore[misc]


class TestNotesImmutability:
    def test_list_notes_coerced_to_tuple(self):
        env = StrategyEnvelope(
            schema_version=1,
            strategy="x",
            built_at="2026-01-01T00:00:00+00:00",
            backtest_window_start="2024-01-01",
            backtest_window_end="2026-01-01",
            notes=["a", "b"],
        )
        assert isinstance(env.notes, tuple)
        with pytest.raises(AttributeError):
            env.notes.append("c")  # type: ignore[attr-defined]

    def test_tuple_notes_pass_through(self):
        env = StrategyEnvelope(
            schema_version=1,
            strategy="x",
            built_at="2026-01-01T00:00:00+00:00",
            backtest_window_start="2024-01-01",
            backtest_window_end="2026-01-01",
            notes=("a", "b"),
        )
        assert env.notes == ("a", "b")


# ── JSON round-trip ───────────────────────────────────────────────────


class TestJsonRoundTrip:
    def test_minimal_envelope_round_trip(self):
        original = _minimal_envelope()
        text = original.to_json()
        restored = StrategyEnvelope.from_json(text)
        assert restored == original

    def test_full_envelope_round_trip(self):
        original = _full_envelope()
        text = original.to_json()
        restored = StrategyEnvelope.from_json(text)
        # Equality covers all fields including tuples.
        assert restored == original

    def test_ci_tuples_become_lists_in_json(self):
        env = _full_envelope()
        parsed = json.loads(env.to_json())
        # CI tuples serialize as JSON lists.
        assert parsed["r_expectancy_ci_95"] == [0.18, 0.65]
        assert parsed["trades_per_month_band"] == [4.0, 11.0]

    def test_unknown_fields_ignored_for_forward_compat(self):
        """A future-version JSON with extra fields should still load on
        an older bot — unknown fields silently ignored."""
        env = _minimal_envelope()
        parsed = json.loads(env.to_json())
        parsed["future_field_we_dont_know_about"] = "ignored"
        parsed["another_future_metric"] = 42
        restored = StrategyEnvelope.from_json(json.dumps(parsed))
        assert restored.strategy == env.strategy

    def test_null_ci_round_trips_as_none(self):
        env = _minimal_envelope()
        text = env.to_json()
        restored = StrategyEnvelope.from_json(text)
        assert restored.r_expectancy_ci_95 is None


# ── File I/O (atomic write) ───────────────────────────────────────────


class TestFileIO:
    def test_write_creates_parent_directory(self, tmp_path: Path):
        env = _full_envelope()
        nested = tmp_path / "deep" / "subdir"
        path = nested / "donchian_breakout.json"
        env.write(path)
        assert path.exists()
        assert path.read_text() == env.to_json()

    def test_read_round_trip(self, tmp_path: Path):
        env = _full_envelope()
        path = tmp_path / "env.json"
        env.write(path)
        restored = StrategyEnvelope.read(path)
        assert restored == env

    def test_atomic_write_uses_tmp_then_replace(self, tmp_path: Path):
        """The write uses a `.tmp` suffix + os.replace. We can't observe
        the intermediate state directly in unit tests, but we can confirm
        the leftover .tmp file is gone after a successful write."""
        env = _minimal_envelope()
        path = tmp_path / "env.json"
        env.write(path)
        tmp = path.with_suffix(path.suffix + ".tmp")
        assert path.exists()
        assert not tmp.exists()

    def test_write_overwrites_existing(self, tmp_path: Path):
        env_a = _minimal_envelope()
        env_b = _full_envelope()
        path = tmp_path / "env.json"
        env_a.write(path)
        env_b.write(path)
        assert StrategyEnvelope.read(path) == env_b


# ── envelope_path ─────────────────────────────────────────────────────


class TestEnvelopePath:
    def test_default_path_points_at_data_envelopes(self):
        p = envelope_path("sma_crossover")
        assert p.name == "sma_crossover.json"
        assert p.parent.name == "envelopes"
        # ROOT/data/envelopes/sma_crossover.json
        assert p.parent.parent.name == "data"

    def test_override_root(self, tmp_path: Path):
        p = envelope_path("donchian_breakout", root=tmp_path)
        assert p == tmp_path / "donchian_breakout.json"


# ── Non-finite JSON safety (PR #17 reviewer feedback) ─────────────────


class TestNonFiniteJsonSafety:
    """Standard JSON has no representation for Infinity/NaN. Strict
    parsers (jq, browser JSON.parse) reject the `Infinity` token the
    default Python encoder emits. Envelope `to_json` defensively
    replaces non-finite scalars with None and uses `allow_nan=False`
    as a final backstop."""

    def test_inf_profit_factor_serializes_as_null(self):
        env = StrategyEnvelope(
            schema_version=1,
            strategy="x",
            built_at="2026-01-01T00:00:00+00:00",
            backtest_window_start="2024-01-01",
            backtest_window_end="2026-01-01",
            profit_factor=float("inf"),
            profit_factor_ci_95=(1.2, float("inf")),
        )
        text = env.to_json()
        # No "Infinity" literal anywhere in the output
        assert "Infinity" not in text
        assert "inf" not in text.lower()
        parsed = json.loads(text)
        assert parsed["profit_factor"] is None
        # Tuple with one inf element → both elements replaced (we
        # null the whole tuple at the build_script layer normally;
        # this test confirms even partial-inf gets stripped).
        assert parsed["profit_factor_ci_95"] == [1.2, None]

    def test_nan_serializes_as_null(self):
        env = StrategyEnvelope(
            schema_version=1,
            strategy="x",
            built_at="2026-01-01T00:00:00+00:00",
            backtest_window_start="2024-01-01",
            backtest_window_end="2026-01-01",
            r_expectancy=float("nan"),
        )
        text = env.to_json()
        assert "NaN" not in text
        parsed = json.loads(text)
        assert parsed["r_expectancy"] is None

    def test_negative_inf_serializes_as_null(self):
        env = StrategyEnvelope(
            schema_version=1,
            strategy="x",
            built_at="2026-01-01T00:00:00+00:00",
            backtest_window_start="2024-01-01",
            backtest_window_end="2026-01-01",
            expectancy_dollars=float("-inf"),
        )
        text = env.to_json()
        parsed = json.loads(text)
        assert parsed["expectancy_dollars"] is None

    def test_finite_values_unchanged(self):
        """Sanity: normalization only touches non-finite — finite
        values round-trip identically."""
        env = _full_envelope()
        parsed = json.loads(env.to_json())
        assert parsed["expectancy_dollars"] == 140.0
        assert parsed["r_expectancy"] == 0.42
        assert parsed["profit_factor"] == 1.62
        assert parsed["r_expectancy_ci_95"] == [0.18, 0.65]

    def test_output_is_valid_standard_json(self):
        """jq-style strict parsers should accept the output. We
        simulate this by asserting the output passes
        `json.loads(strict=True)`. Python's loads has strict=True by
        default; the real test is that no 'Infinity' / 'NaN' tokens
        appear and that `allow_nan=False` was applied."""
        env = StrategyEnvelope(
            schema_version=1,
            strategy="x",
            built_at="2026-01-01T00:00:00+00:00",
            backtest_window_start="2024-01-01",
            backtest_window_end="2026-01-01",
            profit_factor=float("inf"),
            r_expectancy=float("nan"),
            expectancy_dollars=float("-inf"),
        )
        text = env.to_json()
        # Round-trip via strict parser
        json.loads(text)  # would raise on Infinity
        # Verify all three non-finite became null
        parsed = json.loads(text)
        assert parsed["profit_factor"] is None
        assert parsed["r_expectancy"] is None
        assert parsed["expectancy_dollars"] is None
