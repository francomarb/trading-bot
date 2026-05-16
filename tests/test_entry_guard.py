"""Unit tests for execution.entry_guard (PLAN 11.32)."""

from __future__ import annotations

import pytest

from execution.entry_guard import (
    CapAction,
    EntryPriceCap,
    compute_cap_price,
    gate_entry,
)


class TestEntryPriceCap:
    def test_requires_at_least_one_knob(self):
        with pytest.raises(ValueError, match="at least one of"):
            EntryPriceCap()

    def test_negative_bps_rejected(self):
        with pytest.raises(ValueError, match="max_chase_bps"):
            EntryPriceCap(max_chase_bps=-1)

    def test_zero_bps_rejected(self):
        with pytest.raises(ValueError, match="max_chase_bps"):
            EntryPriceCap(max_chase_bps=0)

    def test_negative_atr_fraction_rejected(self):
        with pytest.raises(ValueError, match="max_chase_atr_fraction"):
            EntryPriceCap(max_chase_atr_fraction=-0.5)

    def test_unknown_on_breach_rejected(self):
        with pytest.raises(ValueError, match="on_breach"):
            EntryPriceCap(max_chase_bps=50, on_breach="explode")

    def test_bps_only_ok(self):
        EntryPriceCap(max_chase_bps=500)

    def test_atr_only_ok(self):
        EntryPriceCap(max_chase_atr_fraction=2.0)

    def test_both_ok(self):
        EntryPriceCap(max_chase_bps=500, max_chase_atr_fraction=2.0)


class TestComputeCapPrice:
    def test_bps_only_buy(self):
        policy = EntryPriceCap(max_chase_bps=500)  # 5%
        cap = compute_cap_price(
            reference_price=100.0, atr=2.0, side="buy", policy=policy
        )
        assert cap == pytest.approx(105.0)

    def test_bps_only_sell_is_floor(self):
        policy = EntryPriceCap(max_chase_bps=500)
        cap = compute_cap_price(
            reference_price=100.0, atr=2.0, side="sell", policy=policy
        )
        assert cap == pytest.approx(95.0)

    def test_atr_only_buy(self):
        policy = EntryPriceCap(max_chase_atr_fraction=2.0)
        cap = compute_cap_price(
            reference_price=100.0, atr=3.0, side="buy", policy=policy
        )
        assert cap == pytest.approx(106.0)

    def test_tighter_wins_when_both_set_bps_tighter(self):
        # bps gives +5; atr gives +6 → bps wins
        policy = EntryPriceCap(max_chase_bps=500, max_chase_atr_fraction=2.0)
        cap = compute_cap_price(
            reference_price=100.0, atr=3.0, side="buy", policy=policy
        )
        assert cap == pytest.approx(105.0)

    def test_tighter_wins_when_both_set_atr_tighter(self):
        # bps gives +5; atr gives +2 → atr wins
        policy = EntryPriceCap(max_chase_bps=500, max_chase_atr_fraction=2.0)
        cap = compute_cap_price(
            reference_price=100.0, atr=1.0, side="buy", policy=policy
        )
        assert cap == pytest.approx(102.0)

    def test_zero_atr_with_atr_only_collapses_cap_to_reference(self):
        # Defensive: ATR=0 means "no chase allowed under the ATR rule"
        # — caller should pair with a bps floor in production, but math is sane.
        policy = EntryPriceCap(max_chase_atr_fraction=2.0)
        cap = compute_cap_price(
            reference_price=100.0, atr=0.0, side="buy", policy=policy
        )
        assert cap == pytest.approx(100.0)

    def test_rejects_nonpositive_reference(self):
        policy = EntryPriceCap(max_chase_bps=500)
        with pytest.raises(ValueError, match="reference_price"):
            compute_cap_price(reference_price=0.0, atr=2.0, side="buy", policy=policy)

    def test_rejects_negative_atr(self):
        policy = EntryPriceCap(max_chase_bps=500)
        with pytest.raises(ValueError, match="atr"):
            compute_cap_price(reference_price=100.0, atr=-1.0, side="buy", policy=policy)

    def test_rejects_bad_side(self):
        policy = EntryPriceCap(max_chase_bps=500)
        with pytest.raises(ValueError, match="side"):
            compute_cap_price(
                reference_price=100.0, atr=2.0, side="short", policy=policy  # type: ignore[arg-type]
            )


class TestGateEntry:
    def test_no_policy_passes_through(self):
        result = gate_entry(
            reference_price=100.0, atr=2.0, side="buy",
            order_type="market", policy=None,
        )
        assert result.action is CapAction.SUBMIT_AS_IS
        assert result.cap_price is None

    def test_limit_order_passes_through_even_with_policy(self):
        policy = EntryPriceCap(max_chase_bps=500)
        result = gate_entry(
            reference_price=100.0, atr=2.0, side="buy",
            order_type="limit", policy=policy,
        )
        assert result.action is CapAction.SUBMIT_AS_IS
        assert result.cap_price is None

    def test_market_with_policy_converts_to_limit(self):
        policy = EntryPriceCap(max_chase_bps=500, max_chase_atr_fraction=2.0)
        result = gate_entry(
            reference_price=100.0, atr=3.0, side="buy",
            order_type="market", policy=policy,
        )
        assert result.action is CapAction.CONVERT_TO_LIMIT
        # atr (2*3=6) vs bps (5) → bps wins
        assert result.cap_price == pytest.approx(105.0)
        # diagnostics surface the chosen cap and inputs for logging
        assert result.diagnostics["cap_price"] == pytest.approx(105.0)
        assert result.diagnostics["chase_bps"] == pytest.approx(500.0)
        assert result.diagnostics["policy_max_chase_bps"] == 500
        assert result.diagnostics["policy_max_chase_atr_fraction"] == 2.0

    def test_qcom_class_outlier_caps_well_below_actual_fill(self):
        # Regression for PLAN 11.32. QCOM signal close ~$219.19,
        # actual market fill ~$245.62 (+1205 bps). With 500 bps / 2.0 ATR
        # policy and ATR ~$6, cap = min(219.19 + 5%, 219.19 + 12) = 230.15.
        policy = EntryPriceCap(max_chase_bps=500, max_chase_atr_fraction=2.0)
        result = gate_entry(
            reference_price=219.19, atr=6.0, side="buy",
            order_type="market", policy=policy,
        )
        assert result.action is CapAction.CONVERT_TO_LIMIT
        assert result.cap_price is not None
        assert result.cap_price < 245.62, "cap must be tighter than the incident fill"
        # The bps gate wins here (219.19 * 1.05 = 230.15 vs 219.19 + 12 = 231.19).
        assert result.cap_price == pytest.approx(219.19 * 1.05)
