"""
Smoke tests for scripts/build_envelopes.py.

Goal: verify the build pipeline produces a valid envelope JSON without
hitting Alpaca. We mock `fetch_symbol` to return synthetic OHLCV and
exercise the full code path (backtest → aggregate → bootstrap CI →
envelope write). Heavy validation of the underlying bootstrap/stats is
covered by tests/test_health_stats.py — this file confirms the script
wires everything together correctly.

Covers:
  - Build for a real equity strategy (SMACrossover) on synthetic bars
    produces a populated envelope file
  - Options strategies (spy_options_reversion, credit_spread) write
    stub envelopes with explanatory `notes`
  - Zero-bars / zero-trades cases produce stub envelopes (not crash)
  - Idempotency: re-running with the same inputs produces a deterministic
    envelope (same Edge metrics; built_at timestamp differs)
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scripts import build_envelopes
from strategies.health.envelope import StrategyEnvelope


# ── Test fixtures ─────────────────────────────────────────────────────


def _synthetic_bars(n: int = 400, *, seed: int = 0) -> pd.DataFrame:
    """Generate a synthetic OHLCV frame long enough for SMA(20,50)
    crossovers to fire. ~400 daily bars covers ~20 months."""
    rng = np.random.default_rng(seed)
    # Geometric random walk with drift — generates enough crossover
    # signals for the backtest to produce a few trades.
    log_rets = rng.normal(loc=0.0005, scale=0.015, size=n)
    closes = 100.0 * np.exp(np.cumsum(log_rets))
    idx = pd.DatetimeIndex(
        [datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(days=i) for i in range(n)]
    )
    return pd.DataFrame(
        {
            "open": closes * (1 + rng.uniform(-0.005, 0.005, n)),
            "high": closes * (1 + rng.uniform(0.0, 0.01, n)),
            "low": closes * (1 - rng.uniform(0.0, 0.01, n)),
            "close": closes,
            "volume": rng.integers(1_000_000, 5_000_000, n),
        },
        index=idx,
    )


# ── Equity strategy build ─────────────────────────────────────────────


class TestEquityEnvelopeBuild:
    def test_sma_envelope_built_from_synthetic_bars(self, tmp_path: Path, monkeypatch):
        """End-to-end: SMACrossover backtest on 2 synthetic symbols
        produces a populated envelope JSON."""
        bars = {
            "AAA": _synthetic_bars(seed=1),
            "BBB": _synthetic_bars(seed=2),
        }

        def fake_fetch(sym, start, end, timeframe):
            if sym not in bars:
                raise RuntimeError(f"no bars for {sym}")
            return bars[sym], None

        monkeypatch.setattr(build_envelopes, "fetch_symbol", fake_fetch)
        # Override the SMA watchlist via settings monkeypatch.
        monkeypatch.setitem(
            build_envelopes.settings.STRATEGY_WATCHLISTS,
            "sma_crossover",
            ["AAA", "BBB"],
        )

        env = build_envelopes.build_envelope(
            "sma_crossover",
            years=1.0,
            end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            out_dir=tmp_path,
        )

        # File should exist
        path = tmp_path / "sma_crossover.json"
        assert path.exists()

        # Envelope should have populated Edge metrics (synthetic walk
        # produces at least a few SMA crossovers over 400 bars).
        loaded = StrategyEnvelope.read(path)
        assert loaded.strategy == "sma_crossover"
        assert loaded.schema_version == 1
        # Trade count > 0 demonstrates the pipeline ran.
        # (If random seeds change and zero trades occur, the stub path
        # is also valid — assert one or the other branch).
        if loaded.trade_count > 0:
            assert loaded.expectancy_dollars is not None
            assert loaded.expectancy_dollars_ci_95 is not None
            assert loaded.win_rate is not None
            # R-expectancy populated when approx_stop_pct is configured.
            assert loaded.r_expectancy is not None
            assert loaded.risk_unit_dollars is not None
            assert loaded.risk_unit_dollars > 0
        else:
            # Stub branch — zero trades on this synthetic seed.
            assert "zero closed trades" in " ".join(loaded.notes)


# ── Options strategy stubs ────────────────────────────────────────────


class TestOptionsStubs:
    @pytest.mark.parametrize(
        "strategy",
        ["spy_options_reversion", "credit_spread"],
    )
    def test_options_writes_stub_with_skip_reason(
        self, tmp_path: Path, strategy: str
    ):
        env = build_envelopes.build_envelope(
            strategy,
            years=1.0,
            end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            out_dir=tmp_path,
        )
        path = tmp_path / f"{strategy}.json"
        assert path.exists()
        loaded = StrategyEnvelope.read(path)
        # Edge metrics all null
        assert loaded.r_expectancy is None
        assert loaded.expectancy_dollars is None
        assert loaded.win_rate is None
        # Notes must explain why it was skipped
        notes_joined = " ".join(loaded.notes)
        assert "requires" in notes_joined.lower()
        assert "11.10g" in notes_joined or "calibrate" in notes_joined.lower()


# ── No-bars / no-trades edge cases ────────────────────────────────────


class TestEmptyCases:
    def test_no_bars_writes_stub(self, tmp_path: Path, monkeypatch):
        """All fetch attempts fail → stub envelope with explanatory notes."""

        def fake_fetch(sym, start, end, timeframe):
            raise RuntimeError("simulated outage")

        monkeypatch.setattr(build_envelopes, "fetch_symbol", fake_fetch)
        # Use SMA which has a real watchlist
        env = build_envelopes.build_envelope(
            "sma_crossover",
            years=1.0,
            end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            out_dir=tmp_path,
        )
        loaded = StrategyEnvelope.read(tmp_path / "sma_crossover.json")
        assert loaded.trade_count == 0
        assert any("no usable bars" in n for n in loaded.notes)

    def test_unknown_strategy_raises(self, tmp_path: Path):
        with pytest.raises(ValueError, match="unknown strategy"):
            build_envelopes.build_envelope(
                "nonexistent_strategy",
                years=1.0,
                end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
                out_dir=tmp_path,
            )


# ── Idempotency ───────────────────────────────────────────────────────


class TestIdempotency:
    def test_two_consecutive_builds_match_on_metrics(
        self, tmp_path: Path, monkeypatch
    ):
        """Re-running the build with identical inputs should produce
        identical Edge metrics (built_at timestamp will differ)."""
        bars = {"AAA": _synthetic_bars(seed=42), "BBB": _synthetic_bars(seed=43)}

        def fake_fetch(sym, start, end, timeframe):
            return bars[sym], None

        monkeypatch.setattr(build_envelopes, "fetch_symbol", fake_fetch)
        monkeypatch.setitem(
            build_envelopes.settings.STRATEGY_WATCHLISTS,
            "sma_crossover",
            ["AAA", "BBB"],
        )

        env1 = build_envelopes.build_envelope(
            "sma_crossover",
            years=1.0,
            end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            out_dir=tmp_path,
        )
        env2 = build_envelopes.build_envelope(
            "sma_crossover",
            years=1.0,
            end_date=datetime(2025, 1, 1, tzinfo=timezone.utc),
            out_dir=tmp_path,
        )
        # built_at will differ (timestamps); compare structural fields.
        assert env1.trade_count == env2.trade_count
        assert env1.expectancy_dollars == env2.expectancy_dollars
        assert env1.r_expectancy == env2.r_expectancy
        assert env1.win_rate == env2.win_rate
        # Bootstrap CIs deterministic via seed=0
        assert env1.expectancy_dollars_ci_95 == env2.expectancy_dollars_ci_95


# ── Spec coverage ─────────────────────────────────────────────────────


class TestStrategySpecsCoverage:
    """Every strategy that has a MIN_TRADES_FOR_VERDICT entry should
    also have a STRATEGY_SPECS entry. Keeps the two in sync."""

    def test_all_min_trades_strategies_have_specs(self):
        from config import settings

        for strategy in settings.STRATEGY_MIN_TRADES_FOR_VERDICT:
            assert strategy in build_envelopes.STRATEGY_SPECS, (
                f"{strategy!r} has a MIN_TRADES_FOR_VERDICT entry but no "
                f"STRATEGY_SPECS entry in scripts/build_envelopes.py"
            )

    def test_every_spec_has_required_keys(self):
        required = {"builder", "watchlist_key", "timeframe"}
        for name, spec in build_envelopes.STRATEGY_SPECS.items():
            missing = required - spec.keys()
            assert not missing, f"{name}: spec missing keys {missing}"
