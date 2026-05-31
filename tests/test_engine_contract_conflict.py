"""
Unit tests for the contract-level conflict rule (PLAN.md 11.44).

The rule:
  * Equity-vs-equity on the same underlying ticker still blocks (unchanged).
  * Two options strategies on the same underlying but DISTINCT OCC contracts
    are now allowed — they are separate broker positions.
  * Two strategies on the EXACT same OCC contract are blocked at dispatch,
    regardless of direction, single-leg vs MLEG, or which strategy got there
    first. The broker aggregates positions by exact symbol; shared ownership
    cannot be represented by the engine's per-strategy ownership map.

These tests target the helpers directly (`_contract_owner`,
`_reject_if_contract_conflict`) plus the underlying-level skip in
`_process_symbol`, so they stay decoupled from the live option-picker / MLEG
worker plumbing exercised in `test_engine_credit_spread.py` and
`test_engine.py`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from engine.positions import PositionLeg, make_single_leg, make_spread
from engine.trader import EngineConfig, TradingEngine
from reporting.logger import TradeLogger
from risk.manager import RiskManager


# Two distinct SPY OCCs to play with — same underlying / expiry, different strike.
_SPY_CALL_A = "SPY260620C00520000"
_SPY_CALL_B = "SPY260620C00530000"
_SPY_PUT_A = "SPY260620P00500000"


# ── Bare-bones engine helper ────────────────────────────────────────────────


def _engine(tmp_path) -> TradingEngine:
    """Spin up an engine just to access the helpers; broker is a mock and no
    strategy is wired (the helpers don't need slots)."""
    broker = MagicMock()
    broker.sync_with_broker.return_value = SimpleNamespace(
        account=SimpleNamespace(open_positions={}, equity=100_000.0),
        open_orders=[],
    )
    risk = RiskManager(
        max_position_pct=0.02, max_open_positions=5, max_gross_exposure_pct=0.50,
        atr_stop_multiplier=2.0, max_daily_loss_pct=0.05,
        hard_dollar_loss_cap=1_000_000.0, loss_streak_threshold=10,
        broker_error_threshold=10,
    )
    tl = TradeLogger(path=str(tmp_path / "trades.db"))
    engine = TradingEngine(
        strategy=MagicMock(name="placeholder_strategy"),
        symbols=["SPY"],
        risk=risk,
        broker=broker,
        trade_logger=tl,
        config=EngineConfig(
            history_lookback_days=120, cycle_interval_seconds=0.01,
            max_bar_age_multiplier=10.0, market_hours_only=False,
        ),
    )
    return engine


# ── _contract_owner ─────────────────────────────────────────────────────────


class TestContractOwner:
    """Direct test of the leg-level ownership scan."""

    def test_returns_none_when_no_positions(self, tmp_path):
        engine = _engine(tmp_path)
        assert engine._contract_owner(_SPY_CALL_A) is None

    def test_finds_single_leg_owner_by_exact_occ(self, tmp_path):
        engine = _engine(tmp_path)
        engine._positions["SPY"] = make_single_leg(
            strategy_name="spy_options_reversion", symbol=_SPY_CALL_A
        )
        owner = engine._contract_owner(_SPY_CALL_A)
        assert owner == ("spy_options_reversion", "SPY")

    def test_distinct_occ_on_same_underlying_is_not_owned(self, tmp_path):
        """The whole point of 11.44 — different strike/expiry/right ≠ collision."""
        engine = _engine(tmp_path)
        engine._positions["SPY"] = make_single_leg(
            strategy_name="spy_options_reversion", symbol=_SPY_CALL_A
        )
        assert engine._contract_owner(_SPY_CALL_B) is None
        assert engine._contract_owner(_SPY_PUT_A) is None

    def test_finds_mleg_leg_owner(self, tmp_path):
        engine = _engine(tmp_path)
        engine._positions["uuid-spread"] = make_spread(
            strategy_name="credit_spread",
            position_id="uuid-spread",
            legs=[
                PositionLeg(symbol=_SPY_CALL_A, qty=-1.0, side="SELL"),
                PositionLeg(symbol=_SPY_CALL_B, qty=1.0, side="BUY"),
            ],
        )
        # Both legs are owned by the same strategy via the spread.
        assert engine._contract_owner(_SPY_CALL_A) == ("credit_spread", "uuid-spread")
        assert engine._contract_owner(_SPY_CALL_B) == ("credit_spread", "uuid-spread")
        # An unrelated SPY OCC is still free.
        assert engine._contract_owner(_SPY_PUT_A) is None


# ── _reject_if_contract_conflict ────────────────────────────────────────────


class TestRejectIfContractConflict:
    """The dispatch-time guard fires alerts + counter on a conflict and is
    silent on a clean check."""

    def test_no_owner_passes(self, tmp_path):
        engine = _engine(tmp_path)
        engine.alerts = MagicMock()
        result = engine._reject_if_contract_conflict(
            strategy_name="credit_spread", symbol="SPY", occs=[_SPY_CALL_A],
        )
        assert result is None
        engine.alerts.order_rejection.assert_not_called()
        assert engine._contract_conflicts == []

    def test_same_strategy_re_check_passes(self, tmp_path):
        """An already-owned contract from the same strategy is not a conflict —
        only cross-strategy collisions block. (Re-entry de-dup is handled
        elsewhere via _entry_blocked_by_existing_position.)"""
        engine = _engine(tmp_path)
        engine.alerts = MagicMock()
        engine._positions["SPY"] = make_single_leg(
            strategy_name="spy_options_reversion", symbol=_SPY_CALL_A
        )
        result = engine._reject_if_contract_conflict(
            strategy_name="spy_options_reversion", symbol="SPY", occs=[_SPY_CALL_A],
        )
        assert result is None
        engine.alerts.order_rejection.assert_not_called()

    def test_cross_strategy_exact_occ_blocks(self, tmp_path):
        engine = _engine(tmp_path)
        engine.alerts = MagicMock()
        engine._positions["SPY"] = make_single_leg(
            strategy_name="spy_options_reversion", symbol=_SPY_CALL_A
        )
        result = engine._reject_if_contract_conflict(
            strategy_name="credit_spread", symbol="SPY", occs=[_SPY_CALL_A],
        )
        assert result is not None
        other_strategy, conflicting_occ = result
        assert other_strategy == "spy_options_reversion"
        assert conflicting_occ == _SPY_CALL_A
        engine.alerts.order_rejection.assert_called_once()
        code = engine.alerts.order_rejection.call_args.args[3]
        assert code == "CONTRACT_CONFLICT"
        # Counter was incremented.
        assert len(engine._contract_conflicts) == 1

    def test_mleg_leg_overlap_blocks(self, tmp_path):
        """A new spread sharing even one leg with an existing position is blocked."""
        engine = _engine(tmp_path)
        engine.alerts = MagicMock()
        engine._positions["uuid-existing"] = make_spread(
            strategy_name="iron_condor",  # hypothetical future MLEG strategy
            position_id="uuid-existing",
            legs=[
                PositionLeg(symbol=_SPY_CALL_A, qty=-1.0, side="SELL"),
                PositionLeg(symbol=_SPY_CALL_B, qty=1.0, side="BUY"),
            ],
        )
        # Incoming credit spread happens to use the same long leg.
        result = engine._reject_if_contract_conflict(
            strategy_name="credit_spread", symbol="SPY",
            occs=[_SPY_PUT_A, _SPY_CALL_B],  # second OCC collides
        )
        assert result is not None
        assert result[0] == "iron_condor"
        assert result[1] == _SPY_CALL_B

    def test_distinct_occ_same_underlying_passes(self, tmp_path):
        """The headline regression: SPY long call + SPY bull put spread must
        coexist as long as no OCC overlaps."""
        engine = _engine(tmp_path)
        engine.alerts = MagicMock()
        engine._positions["SPY"] = make_single_leg(
            strategy_name="spy_options_reversion", symbol=_SPY_CALL_A
        )
        result = engine._reject_if_contract_conflict(
            strategy_name="credit_spread", symbol="SPY",
            occs=[_SPY_PUT_A, "SPY260620P00510000"],
        )
        assert result is None
        engine.alerts.order_rejection.assert_not_called()


# ── _prune_window ──────────────────────────────────────────────────────────


class TestEnterMultiLegContractGuard:
    """Verify the MLEG entry path actually invokes the guard before dispatch.
    Mirrors the credit-spread happy-path fixture style."""

    def _make_engine_and_strategy(self, tmp_path):
        from datetime import date
        from unittest.mock import patch
        from strategies.credit_spread import CreditSpread, CreditSpreadConfig
        from utils.iv_proxy import IVProxyResolver
        from utils.options_lookup import SpreadPick

        raw = {
            "short_leg_delta": 0.17, "spread_width": 10, "dte_min": 30,
            "dte_max": 45, "iv_proxy_source": "vix", "min_iv_proxy": 14,
            "min_credit_pct_of_width": 0.13, "max_concurrent_positions": 3,
            "max_per_expiration": 1, "min_dte_gap_between_opens": 7,
            "profit_target_pct": 0.50, "stop_loss_multiple": 2.0,
            "time_stop_dte": 21, "exit_on_short_strike_breach": True,
            "limit_timeout_seconds": 30, "earnings_blackout_days": 0,
        }
        strategy = CreditSpread(
            CreditSpreadConfig.from_dict("SPY", raw),
            iv_resolver=IVProxyResolver(fetch_fn=lambda t: 18.0),
            quote_lookup=lambda occs: {o: None for o in occs},
        )
        engine = _engine(tmp_path)
        # Replace the placeholder strategy.
        engine.slots = []
        pick = SpreadPick(
            short_occ=_SPY_CALL_A, long_occ=_SPY_CALL_B,
            short_strike=520.0, long_strike=530.0,
            expiration_date=date.today() + timedelta(days=37),
            width=10.0, net_credit=1.45,
            max_loss=(10.0 - 1.45) * 100, short_leg_delta=0.17,
            score=0.7, components={
                "short_delta": 1.0, "net_credit": 0.15,
                "spread_quality": 0.8, "dte": 0.9,
            },
            runners_up=[],
        )
        return engine, strategy, pick, patch

    def test_dispatch_blocked_when_leg_owned_by_other_strategy(self, tmp_path):
        engine, strategy, pick, patch = self._make_engine_and_strategy(tmp_path)
        # Pre-own one of the spread legs under a different strategy.
        engine._positions["SPY"] = make_single_leg(
            strategy_name="spy_options_reversion", symbol=_SPY_CALL_A
        )
        engine.alerts = MagicMock()
        engine.broker.dispatch_spread_order = MagicMock()

        with patch("strategies.credit_spread.find_best_put_spread", return_value=pick):
            engine._enter_multi_leg(
                strategy=strategy, symbol="SPY", underlying_close=525.0,
                notional_cap=2_000.0,
                signal_key=("credit_spread", "SPY", "1Day"),
                signal_bar="2026-05-14",
                strategy_statuses={}, strategy_reasons={},
            )

        engine.broker.dispatch_spread_order.assert_not_called()
        # Pre-existing position untouched; no new spread registered.
        assert len(engine._positions) == 1
        assert "SPY" in engine._positions
        # CONTRACT_CONFLICT alert fired.
        engine.alerts.order_rejection.assert_called_once()
        assert engine.alerts.order_rejection.call_args.args[3] == "CONTRACT_CONFLICT"

    def test_dispatch_proceeds_when_legs_are_distinct(self, tmp_path):
        from execution.broker import OrderResult, OrderStatus

        engine, strategy, pick, patch = self._make_engine_and_strategy(tmp_path)
        # Pre-own a DIFFERENT SPY OCC — no leg overlap.
        engine._positions["SPY"] = make_single_leg(
            strategy_name="spy_options_reversion", symbol=_SPY_PUT_A
        )
        engine.alerts = MagicMock()
        engine.broker.dispatch_spread_order = MagicMock(
            return_value=OrderResult(
                status=OrderStatus.ACCEPTED, order_id="w-1", symbol="SPY",
                requested_qty=1, filled_qty=0.0, avg_fill_price=0.0,
                raw_status="accepted", message="",
            )
        )
        with patch("strategies.credit_spread.find_best_put_spread", return_value=pick):
            engine._enter_multi_leg(
                strategy=strategy, symbol="SPY", underlying_close=525.0,
                notional_cap=2_000.0,
                signal_key=("credit_spread", "SPY", "1Day"),
                signal_bar="2026-05-14",
                strategy_statuses={}, strategy_reasons={},
            )

        engine.broker.dispatch_spread_order.assert_called_once()
        # Original single-leg + new spread coexist.
        assert len(engine._positions) == 2
        # No CONTRACT_CONFLICT alert.
        engine.alerts.order_rejection.assert_not_called()


# ── _prune_window ──────────────────────────────────────────────────────────


class TestPruneWindow:
    def test_keeps_recent_drops_old(self):
        now = datetime.now(timezone.utc)
        ts = [
            now - timedelta(hours=48),
            now - timedelta(hours=25),  # outside 24h
            now - timedelta(hours=12),  # inside
            now - timedelta(minutes=5),  # inside
        ]
        remaining = TradingEngine._prune_window(ts, window=timedelta(hours=24))
        assert remaining == 2
        # Mutated in place — only the inside-window timestamps survived.
        assert all((now - t) <= timedelta(hours=24) for t in ts)

    def test_empty_is_noop(self):
        ts: list[datetime] = []
        assert TradingEngine._prune_window(ts, window=timedelta(hours=24)) == 0
