"""
Regime-gate wiring tests.

Two distinct things are pinned here:

1. **The wiring.** `config.settings.STRATEGY_ALLOWED_REGIMES` used to be read
   by nothing except `dashboard.py`, while the engine ran frozenset literals
   written out in `forward_test.py`. Editing the settings dict alone was a
   **no-op on live behaviour** while appearing to change the gate, and the
   dashboard could disagree with the engine indefinitely. `forward_test`
   now derives every slot's regimes from the dict; these tests keep it that
   way ([[feedback_single_source_of_truth_params]],
   [[feedback_fix_the_writer_production_uses]]).

2. **The `11.59` gate change.** Donchian moved from TRENDING-only to a
   BEAR-only exclusion on 2026-08-18. The set is asserted explicitly so a
   silent revert fails rather than passing quietly, and so the four sleeves
   that were *not* meant to change are pinned at their pre-change values.
"""

from __future__ import annotations

import pytest

from config import settings
from regime.detector import MarketRegime


# The literals exactly as they stood in forward_test.py at 94ca862, before the
# resolver replaced them. Four of these must never have changed; Donchian is
# the one deliberate change and is asserted separately below.
PRE_CHANGE_LITERALS = {
    "sma_crossover": frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
    "rsi_reversion": frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
    "spy_options_reversion": frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
    "credit_spread": frozenset({MarketRegime.TRENDING, MarketRegime.RANGING}),
}


class TestResolverIsTheOnlySource:
    def test_forward_test_has_no_hardcoded_regime_literals(self):
        """
        The defect this guards: a frozenset literal reintroduced here would
        once again let the engine and `STRATEGY_ALLOWED_REGIMES` disagree,
        and a settings-only edit would silently do nothing.
        """
        from pathlib import Path

        source = Path(settings.__file__).parent.parent / "forward_test.py"
        text = source.read_text()
        assert "allowed_regimes=frozenset(" not in text, (
            "forward_test.py contains a hardcoded allowed_regimes frozenset; "
            "slots must derive from settings.STRATEGY_ALLOWED_REGIMES via "
            "_allowed_regimes() or the settings dict becomes decorative again"
        )
        assert text.count("allowed_regimes=_allowed_regimes(") == 5

    def test_resolver_maps_names_to_enum_members(self):
        import forward_test

        assert forward_test._allowed_regimes("donchian_breakout") == frozenset(
            {MarketRegime.TRENDING, MarketRegime.RANGING, MarketRegime.VOLATILE}
        )

    def test_unknown_strategy_raises_with_the_known_keys(self):
        import forward_test

        with pytest.raises(KeyError, match="no entry for 'nope'"):
            forward_test._allowed_regimes("nope")

    def test_unknown_regime_name_raises_rather_than_silently_dropping(self, monkeypatch):
        """A typo must fail loudly, not resolve to a smaller (looser or tighter) set."""
        import forward_test

        monkeypatch.setitem(
            settings.STRATEGY_ALLOWED_REGIMES, "donchian_breakout", {"TRENDING", "TRNEDING"}
        )
        with pytest.raises(ValueError, match="unknown regime"):
            forward_test._allowed_regimes("donchian_breakout")


class TestDonchianBearOnlyGate:
    """PLAN 11.59 — changed 2026-08-18 after the pre-registered test passed."""

    def test_donchian_allows_trending_ranging_volatile(self):
        assert settings.STRATEGY_ALLOWED_REGIMES["donchian_breakout"] == {
            "TRENDING",
            "RANGING",
            "VOLATILE",
        }

    def test_donchian_still_blocks_bear(self):
        """
        BEAR is the gate's one measured value: 2022 was -47.8R ungated against
        -9.1R gated. Allowing BEAR is a different change and needs its own
        pre-registration per the investigation's no-substitution rule.
        """
        assert "BEAR" not in settings.STRATEGY_ALLOWED_REGIMES["donchian_breakout"]

    def test_engine_would_gate_on_the_changed_set(self):
        """
        Assert at the layer the engine reads — `trader.py` tests
        `current_regime not in slot.allowed_regimes` — rather than only at the
        settings dict, which is what made the old wiring look correct.
        """
        import forward_test

        allowed = forward_test._allowed_regimes("donchian_breakout")
        assert MarketRegime.RANGING in allowed
        assert MarketRegime.VOLATILE in allowed
        assert MarketRegime.BEAR not in allowed


class TestOtherSleevesUnchanged:
    @pytest.mark.parametrize("name", sorted(PRE_CHANGE_LITERALS))
    def test_sleeve_matches_its_pre_change_literal(self, name):
        """11.59 changed Donchian only; any other movement here is accidental."""
        import forward_test

        assert forward_test._allowed_regimes(name) == PRE_CHANGE_LITERALS[name]


class TestDashboardAgreesWithEngine:
    def test_dashboard_source_and_engine_source_are_the_same_object(self):
        """
        The dashboard reads `settings.STRATEGY_ALLOWED_REGIMES` directly; the
        engine now resolves from it. Same dict, so they cannot drift.
        """
        import forward_test

        for name, names in settings.STRATEGY_ALLOWED_REGIMES.items():
            if name == "bollinger_squeeze":
                continue  # implemented but not wired into forward_test
            resolved = forward_test._allowed_regimes(name)
            assert {m.name for m in resolved} == set(names)
