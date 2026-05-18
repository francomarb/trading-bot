"""
Unit tests for strategies/health/thresholds.py.

Coverage:
  - CheckThresholds validates direction
  - get_thresholds falls back to defaults
  - get_thresholds raises on unknown check name (typo guard)
  - list_known_checks returns every default + per-strategy key
  - Every L3 default has broken=None (design §3.6: L3 cannot be BROKEN)
"""

from __future__ import annotations

import pytest

from strategies.health import thresholds as t


class TestCheckThresholds:
    def test_above_direction_default(self):
        ct = t.CheckThresholds(watch=10, degraded=20, broken=30)
        assert ct.direction == "above"

    def test_below_direction(self):
        ct = t.CheckThresholds(watch=0.7, degraded=0.5, broken=0.3, direction="below")
        assert ct.direction == "below"

    def test_invalid_direction_raises(self):
        with pytest.raises(ValueError):
            t.CheckThresholds(watch=1, degraded=2, broken=3, direction="sideways")

    def test_broken_can_be_none(self):
        """L3 drift checks have broken=None — drift cannot be BROKEN."""
        ct = t.CheckThresholds(watch=0.2, degraded=0.5, broken=None)
        assert ct.broken is None


class TestGetThresholds:
    def test_falls_back_to_default(self):
        ct = t.get_thresholds("sma_crossover", "slippage_realized_vs_modeled_bps_p95")
        assert ct.watch == 20
        assert ct.degraded == 50
        assert ct.broken == 100

    def test_unknown_check_raises_keyerror(self):
        with pytest.raises(KeyError, match="unknown health check"):
            t.get_thresholds("sma_crossover", "nonexistent_check_name")

    def test_per_strategy_override_takes_precedence(self, monkeypatch):
        # Inject an override for one strategy and confirm it wins.
        override = t.CheckThresholds(watch=99, degraded=999, broken=9999)
        monkeypatch.setitem(
            t.THRESHOLDS,
            "rsi_reversion",
            {"slippage_realized_vs_modeled_bps_p95": override},
        )
        ct = t.get_thresholds("rsi_reversion", "slippage_realized_vs_modeled_bps_p95")
        assert ct.watch == 99
        # Other strategies still see the default.
        default = t.get_thresholds("sma_crossover", "slippage_realized_vs_modeled_bps_p95")
        assert default.watch == 20


class TestListKnownChecks:
    def test_includes_all_defaults(self):
        names = t.list_known_checks()
        assert "slippage_realized_vs_modeled_bps_p95" in names
        assert "stream_disconnects_per_day" in names
        assert "trade_frequency_drift_pct" in names

    def test_includes_per_strategy_overrides(self, monkeypatch):
        monkeypatch.setitem(
            t.THRESHOLDS,
            "credit_spread",
            {"my_special_check": t.CheckThresholds(watch=1, degraded=2, broken=None)},
        )
        names = t.list_known_checks()
        assert "my_special_check" in names

    def test_returns_sorted(self):
        names = t.list_known_checks()
        assert names == sorted(names)


class TestL3InvariantNoBroken:
    """Design §3.6: L3 drift checks cannot be BROKEN.

    All default L3 checks (suffixed `_drift_pct` or `_drift_ks_pvalue`)
    should have broken=None.
    """

    def test_all_drift_checks_have_no_broken(self):
        l3_checks = [
            name
            for name in t._DEFAULTS
            if "drift" in name
        ]
        assert len(l3_checks) > 0, "no L3 drift checks found in defaults"
        for check_name in l3_checks:
            ct = t._DEFAULTS[check_name]
            assert ct.broken is None, (
                f"L3 check {check_name!r} has broken={ct.broken} but L3 cannot be BROKEN"
            )
