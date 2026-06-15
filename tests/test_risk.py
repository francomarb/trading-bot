"""
Unit tests for risk/manager.py.

The RiskManager is the gatekeeper between strategy signals and order
placement. Every rule in the manager has a positive test (it lets the right
trade through) and a negative test (it blocks the wrong one). Tests are
offline and deterministic — no network, no clock dependency (we pass `now=`
explicitly).

Coverage map (one class per concern):
  - TestRiskManagerConstruction: bad knobs reject at __init__
  - TestSignalValidation: invalid signals → INVALID_SIGNAL / UNSUPPORTED_SIDE
  - TestStopAndSizing: ATR stop math, fixed-fractional sizing, dollar-loss
    bound, gross exposure cap, cash cap
  - TestDuplicateAndMaxPositions: 6.4, 6.8
  - TestDailyLoss: 6.5 — % drawdown circuit breaker engages and persists
  - TestHardDollarCap: 6.6
  - TestStrategyCooldown: 6.9 — loss-streak triggers per-strategy disable;
    elapses correctly; wins reset; one strategy doesn't block another
  - TestBrokerErrorStreak: 6.10 — N errors in window → halt; old errors fall
    out of the window
  - TestSlippageDrift: 6.11 — too few samples = no halt; mean exceeds
    multiplier = halt
  - TestRiskDecisionInvariant: malformed RiskDecision construction is rejected
  - TestKillSwitchHalt: while halted, every signal rejects with HALTED
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from risk.manager import (
    AccountState,
    OrderType,
    Position,
    RejectionCode,
    RiskDecision,
    RiskManager,
    RiskRejection,
    Side,
    Signal,
)


# ── Fixtures / helpers ───────────────────────────────────────────────────────


T0 = datetime(2026, 4, 15, 14, 30, tzinfo=timezone.utc)


def _account(
    *,
    equity: float = 100_000.0,
    cash: float | None = None,
    session_start: float | None = None,
    previous_close: float | None = None,
    positions: dict[str, Position] | None = None,
) -> AccountState:
    return AccountState(
        equity=equity,
        cash=equity if cash is None else cash,
        session_start_equity=equity if session_start is None else session_start,
        previous_close_equity=previous_close,
        open_positions=positions or {},
    )


def _signal(
    *,
    symbol: str = "AAPL",
    side: Side = Side.BUY,
    strategy: str = "sma_crossover",
    price: float = 100.0,
    atr: float = 2.0,
    order_type: OrderType = OrderType.MARKET,
    limit_price: float | None = None,
    entry_trigger_price: float | None = None,
) -> Signal:
    return Signal(
        symbol=symbol,
        side=side,
        strategy_name=strategy,
        reference_price=price,
        atr=atr,
        reason="test",
        order_type=order_type,
        limit_price=limit_price,
        entry_trigger_price=entry_trigger_price,
    )


def _mgr(**overrides) -> RiskManager:
    """RiskManager with deterministic, easy-to-reason-about defaults.

    slippage_drift_enabled defaults to True in tests so existing drift tests
    work without change. Production default is False (paper calibration phase).
    """
    defaults = dict(
        max_position_pct=0.02,
        max_open_positions=3,
        max_gross_exposure_pct=0.50,
        atr_stop_multiplier=2.0,
        max_daily_loss_pct=0.05,
        hard_dollar_loss_cap=2_000.0,
        loss_streak_threshold=3,
        loss_streak_cooldown_hours=24,
        broker_error_threshold=3,
        broker_error_window_seconds=60,
        slippage_min_samples=5,
        slippage_drift_multiplier=3.0,
        slippage_drift_enabled=True,   # tests exercise the kill switch directly
    )
    defaults.update(overrides)
    return RiskManager(**defaults)


# ── Construction ─────────────────────────────────────────────────────────────


class TestRiskManagerConstruction:
    @pytest.mark.parametrize(
        "field,bad_value",
        [
            ("max_position_pct", 0),
            ("max_position_pct", 1.0),
            ("max_position_notional_pct", 0),
            ("max_position_notional_pct", 1.1),
            ("max_open_positions", 0),
            ("max_gross_exposure_pct", 0),
            ("max_gross_exposure_pct", 6),
            ("atr_stop_multiplier", 0),
            ("max_daily_loss_pct", 0),
            ("max_daily_loss_pct", 1.0),
            ("hard_dollar_loss_cap", 0),
            ("loss_streak_threshold", 0),
            ("loss_streak_cooldown_hours", 0),
            ("broker_error_threshold", 0),
            ("broker_error_window_seconds", 0),
            ("slippage_min_samples", 0),
            ("slippage_drift_multiplier", 1.0),
        ],
    )
    def test_invalid_config_rejected(self, field, bad_value):
        with pytest.raises(ValueError):
            _mgr(**{field: bad_value})

    def test_default_construction_uses_settings(self):
        # Smoke test: defaults from settings.py construct a working manager.
        mgr = RiskManager()
        assert mgr.max_position_pct > 0
        assert mgr.is_halted() is False


# ── Signal validation ───────────────────────────────────────────────────────


class TestSignalValidation:
    def test_zero_price_rejected(self):
        rej = _mgr().evaluate(_signal(price=0.0), _account(), now=T0)
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_SIGNAL

    def test_negative_atr_rejected(self):
        rej = _mgr().evaluate(_signal(atr=-1.0), _account(), now=T0)
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_SIGNAL

    def test_zero_atr_rejected(self):
        rej = _mgr().evaluate(_signal(atr=0.0), _account(), now=T0)
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_SIGNAL

    def test_zero_equity_rejected(self):
        rej = _mgr().evaluate(_signal(), _account(equity=0.0), now=T0)
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_SIGNAL

    def test_short_side_rejected_in_mvp(self):
        rej = _mgr().evaluate(
            _signal(side=Side.SELL), _account(), now=T0
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.UNSUPPORTED_SIDE


# ── STOP_LIMIT manager-level validation (PLAN 11.47) ────────────────────────


class TestSignalValidationStopLimit:
    def _stop_limit_signal(self, **overrides) -> Signal:
        kwargs = dict(
            order_type=OrderType.STOP_LIMIT,
            entry_trigger_price=100.5,
            limit_price=101.0,
            price=100.0,
        )
        kwargs.update(overrides)
        return _signal(**kwargs)

    def test_stop_limit_missing_trigger_rejected(self):
        rej = _mgr().evaluate(
            self._stop_limit_signal(entry_trigger_price=None),
            _account(), now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_SIGNAL
        assert "entry_trigger_price" in rej.message

    def test_stop_limit_missing_limit_rejected(self):
        rej = _mgr().evaluate(
            self._stop_limit_signal(limit_price=None),
            _account(), now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_SIGNAL
        assert "limit_price" in rej.message

    def test_stop_limit_limit_below_trigger_rejected(self):
        rej = _mgr().evaluate(
            self._stop_limit_signal(entry_trigger_price=100.5, limit_price=100.0),
            _account(), now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_SIGNAL

    def test_stop_limit_stop_above_trigger_returns_invalid_stop(self):
        # Force the ATR stop above the trigger: atr*2.0 stop offset from
        # reference_price=100 puts stop at 90. Trigger at 89 < stop → INVALID_STOP.
        rej = _mgr().evaluate(
            self._stop_limit_signal(
                price=100.0, atr=5.0, entry_trigger_price=89.0, limit_price=90.0,
            ),
            _account(), now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_STOP

    def test_stop_limit_passthrough_to_decision(self):
        decision = _mgr().evaluate(
            self._stop_limit_signal(),
            _account(), now=T0,
        )
        assert isinstance(decision, RiskDecision)
        assert decision.order_type is OrderType.STOP_LIMIT
        assert decision.entry_trigger_price == 100.5
        assert decision.limit_price == 101.0

    def test_stop_limit_decision_qty_is_whole_share(self):
        # Even with FRACTIONAL_ENABLED=True (the LIVE_TRADING/paper default),
        # STOP_LIMIT must floor to int because Alpaca rejects fractional
        # stop-limit. _size_position's fractional branch is gated on MARKET.
        decision = _mgr().evaluate(
            self._stop_limit_signal(),
            _account(equity=100_000.0),
            now=T0,
        )
        assert isinstance(decision, RiskDecision)
        assert float(decision.qty).is_integer(), (
            f"STOP_LIMIT qty must be whole-share, got {decision.qty}"
        )

    def test_stop_limit_sizing_uses_limit_price_not_close(self):
        # PLAN 11.47 R1 P1-3: sizing must anchor to the worst-permitted
        # fill (limit_price), not the signal-bar close. A STOP_LIMIT with
        # close=$100, trigger=$101, limit=$105 has worst-case fill at $105.
        # Stop at $95 → worst-case stop_distance = $10, NOT $5. Sizing on
        # close would double the qty if the fill landed at the cap.
        #
        # max_position_pct=0.02 on equity=$100k → $2k risk dollars. At a
        # worst-case stop_distance of $10, that's 200 shares. Sizing on
        # close ($5 distance) would have given 400 — a 2× over-allocation.
        # max_position_notional_pct lifted out of the way so the assertion
        # tests stop-risk sizing in isolation.
        mgr = _mgr(max_position_pct=0.02, max_position_notional_pct=1.0)
        decision = mgr.evaluate(
            self._stop_limit_signal(
                price=100.0,
                atr=2.5,            # ATR stop → 100 - 2*2.5 = 95
                entry_trigger_price=101.0,
                limit_price=105.0,
            ),
            _account(equity=100_000.0, cash=100_000.0),
            now=T0,
        )
        assert isinstance(decision, RiskDecision)
        assert decision.qty == 200, (
            f"STOP_LIMIT qty must size off limit_price (worst-case fill), "
            f"not close. Expected 200 shares (risk=$2k / stop_distance=$10); "
            f"got {decision.qty} — likely still sizing off close ($5 distance)."
        )

    def test_stop_limit_notional_cap_uses_limit_price(self):
        # Per-position notional cap: the cap is on worst-case exposure.
        # equity=$100k, max_position_notional_pct=0.10 → $10k cap. At
        # limit_price=$105, the cap allows floor($10k/$105) = 95 shares.
        # If sizing used close ($100), it would allow 100 shares and
        # exceed the cap at the worst-case fill.
        mgr = _mgr(max_position_pct=0.99, max_position_notional_pct=0.10)
        decision = mgr.evaluate(
            self._stop_limit_signal(
                price=100.0,
                atr=2.5,
                entry_trigger_price=101.0,
                limit_price=105.0,
            ),
            _account(equity=100_000.0, cash=100_000.0),
            now=T0,
        )
        assert isinstance(decision, RiskDecision)
        assert decision.qty == 95
        # And the worst-case notional respects the cap.
        assert decision.qty * 105.0 <= 100_000.0 * 0.10

    def test_market_sizing_unchanged_by_stop_limit_anchor(self):
        # Negative control: MARKET sizing still uses reference_price (close).
        # Same risk budget, same close, same ATR → identical qty to the
        # pre-PLAN-11.47 behavior.
        mgr = _mgr(max_position_pct=0.02, max_position_notional_pct=1.0)
        decision = mgr.evaluate(
            _signal(
                order_type=OrderType.MARKET,
                price=100.0,
                atr=2.5,        # stop at 95, distance $5
            ),
            _account(equity=100_000.0, cash=100_000.0),
            now=T0,
        )
        assert isinstance(decision, RiskDecision)
        assert decision.qty == 400  # $2k / $5 = 400 (close-anchored, unchanged)

    def test_live_multiplier_does_not_revive_zero_share_stop_limit(self, monkeypatch):
        # PLAN 11.47 R2 P1: a STOP_LIMIT signal whose risk budget can't
        # afford even one share must stay rejected in live mode. The
        # previous code applied max(1, math.floor(qty * mult)) AFTER
        # sizing returned 0, coercing the reject to a one-share order
        # and violating the never-round-up-beyond-budget invariant.
        from risk import manager as risk_manager_module
        monkeypatch.setattr(risk_manager_module.settings, "LIVE_TRADING", True)
        monkeypatch.setattr(risk_manager_module.settings, "LIVE_SIZE_MULTIPLIER", 0.25)

        # equity=$100, max_position_pct=0.02 → $2 risk dollars. STOP_LIMIT
        # worst-case fill at $105 with stop=$95 → $10 stop_distance. The
        # zero-floor rounds qty to 0. Live multiplier 0.25 used to push
        # max(1, math.floor(0 * 0.25)) = 1.
        rej = _mgr(max_position_pct=0.02).evaluate(
            self._stop_limit_signal(price=100.0, atr=2.5),
            _account(equity=100.0, cash=100.0),
            now=T0,
        )
        assert isinstance(rej, RiskRejection), (
            f"live multiplier revived a zero-share STOP_LIMIT: got {rej!r}"
        )
        assert rej.code is RejectionCode.POSITION_TOO_SMALL

    def test_live_multiplier_invariant_covers_fractional_path(self, monkeypatch):
        # Same invariant for the fractional MARKET branch. Bypass the
        # rounding accident (fractional shares are too granular to easily
        # round to 0 from realistic inputs) by patching _size_position
        # directly — the test asserts the multiplier doesn't fabricate a
        # 0.01-share order when sizing rejected the trade.
        from risk import manager as risk_manager_module
        monkeypatch.setattr(risk_manager_module.settings, "LIVE_TRADING", True)
        monkeypatch.setattr(risk_manager_module.settings, "LIVE_SIZE_MULTIPLIER", 0.25)
        monkeypatch.setattr(risk_manager_module.settings, "FRACTIONAL_ENABLED", True)

        mgr = _mgr(max_position_pct=0.02)
        monkeypatch.setattr(mgr, "_size_position", lambda *a, **k: 0)
        rej = mgr.evaluate(
            _signal(order_type=OrderType.MARKET, price=100.0, atr=2.5),
            _account(equity=100_000.0, cash=100_000.0),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.POSITION_TOO_SMALL

    def test_stop_limit_sub_share_rejected(self):
        # 1 share at the entry trigger needs ~$100.5 of risk budget.
        # max_position_pct=0.02 → risk_dollars = equity * 0.02. With
        # equity=$100, risk_dollars=$2 — far below a single-share stop
        # loss of (ref - stop) = $10. Sizing rounds to 0 → POSITION_TOO_SMALL.
        rej = _mgr().evaluate(
            self._stop_limit_signal(price=100.0, atr=2.5),  # stop=95
            _account(equity=100.0, cash=100.0),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.POSITION_TOO_SMALL


# ── Stop placement & position sizing ────────────────────────────────────────


class TestStopAndSizing:
    def test_happy_path_returns_decision(self):
        mgr = _mgr()
        result = mgr.evaluate(_signal(price=100.0, atr=2.0), _account(), now=T0)
        assert isinstance(result, RiskDecision)
        assert result.symbol == "AAPL"
        assert result.side is Side.BUY
        # Stop = 100 - 2*2 = 96
        assert result.stop_price == pytest.approx(96.0)
        # risk = 100k * 0.02 = $2000; stop dist = $4 → risk qty 500.
        # Per-position notional cap = 10% of 100k = $10k → 100 shares.
        assert result.qty == 100

    def test_dollar_loss_to_stop_bounded_by_max_position_pct(self):
        """The whole point of the sizing rule: stopping out costs ≤ X% of equity."""
        mgr = _mgr(
            max_position_pct=0.01,
            max_position_notional_pct=1.0,
        )  # 1% per trade
        equity = 50_000.0
        result = mgr.evaluate(
            _signal(price=200.0, atr=5.0),
            _account(equity=equity),
            now=T0,
        )
        assert isinstance(result, RiskDecision)
        # Stop = 200 - 10 = 190; risk budget = $500; qty = floor(500/10) = 50
        assert result.qty == 50
        loss_at_stop = result.qty * (result.entry_reference_price - result.stop_price)
        assert loss_at_stop <= equity * 0.01 + 1e-6

    def test_per_position_notional_cap_scales_qty_down(self):
        mgr = _mgr(max_position_notional_pct=0.10)
        result = mgr.evaluate(
            _signal(symbol="NVDA", price=200.0, atr=4.7),
            _account(equity=100_000.0),
            now=T0,
        )
        assert isinstance(result, RiskDecision)
        # Stop-risk sizing would allow floor(2000 / 9.4) = 212 shares.
        # 10% notional cap allows floor(10000 / 200) = 50 shares.
        assert result.qty == 50
        assert result.qty * result.entry_reference_price <= 10_000.0

    def test_per_position_notional_cap_still_allows_five_position_sleeve(self):
        mgr = _mgr(
            max_position_notional_pct=0.10,
            max_open_positions=5,
            max_gross_exposure_pct=0.50,
        )
        equity = 100_000.0
        positions = {
            f"SYM{i}": Position(
                symbol=f"SYM{i}",
                qty=100,
                avg_entry_price=100.0,
                market_value=10_000.0,
            )
            for i in range(4)
        }
        result = mgr.evaluate(
            _signal(symbol="AAPL", price=100.0, atr=1.0),
            _account(equity=equity, positions=positions),
            now=T0,
        )
        assert isinstance(result, RiskDecision)
        assert result.qty == 100

    def test_gross_exposure_cap_scales_qty_down(self):
        mgr = _mgr(max_gross_exposure_pct=0.10)  # very tight cap
        equity = 100_000.0
        # Already holding $9k of MSFT; only $1k of headroom left.
        positions = {
            "MSFT": Position(
                symbol="MSFT",
                qty=30,
                avg_entry_price=300.0,
                market_value=9_000.0,
            )
        }
        result = mgr.evaluate(
            _signal(symbol="AAPL", price=100.0, atr=2.0),
            _account(equity=equity, positions=positions),
            now=T0,
        )
        assert isinstance(result, RiskDecision)
        # Cap headroom $1000 / $100 = 10 shares max — even though risk-based
        # sizing alone would have allowed 500.
        assert result.qty == 10

    def test_gross_exposure_fully_exhausted_rejects(self):
        mgr = _mgr(max_gross_exposure_pct=0.10)
        equity = 100_000.0
        positions = {
            "MSFT": Position(
                symbol="MSFT",
                qty=50,
                avg_entry_price=300.0,
                market_value=15_000.0,
            )
        }
        rej = mgr.evaluate(
            _signal(symbol="AAPL", price=100.0, atr=2.0),
            _account(equity=equity, positions=positions),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.GROSS_EXPOSURE_CAP

    def test_insufficient_cash_rejects_when_cash_below_one_share(self):
        mgr = _mgr()
        rej = mgr.evaluate(
            _signal(price=100.0, atr=2.0),
            _account(equity=100_000.0, cash=0.5),  # can't afford even min fractional
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INSUFFICIENT_CASH

    def test_position_too_small_when_stop_distance_huge(self):
        # ATR so wide that even risking 2% of $1k equity rounds to 0 shares.
        mgr = _mgr(max_position_pct=0.02)
        rej = mgr.evaluate(
            _signal(price=100.0, atr=50.0),  # stop 100-100=0 → INVALID_STOP
            _account(equity=1_000.0),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_STOP

    def test_invalid_stop_when_atr_too_wide_for_long(self):
        # 2 * 60 = 120 → stop = -20 (negative)
        mgr = _mgr(atr_stop_multiplier=2.0)
        rej = mgr.evaluate(
            _signal(price=100.0, atr=60.0),
            _account(equity=1_000_000.0),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.INVALID_STOP

    def test_position_too_small_for_tiny_equity(self):
        # risk budget = 0.02 * $50 = $1; stop distance = 2*2 = $4 → qty = 0
        # cash $50 / price $10 = 5 → cash isn't the binding cap, sizing is.
        # Use LIMIT order type: LIMIT orders always use whole-share floor()
        # regardless of FRACTIONAL_ENABLED, so this test is unambiguous.
        mgr = _mgr(max_position_pct=0.02)
        rej = mgr.evaluate(
            _signal(
                price=10.0, atr=2.0,
                order_type=OrderType.LIMIT, limit_price=9.5,
            ),
            _account(equity=50.0, cash=50.0, session_start=50.0),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.POSITION_TOO_SMALL


# ── Duplicate position + max positions ──────────────────────────────────────


class TestDuplicateAndMaxPositions:
    def test_duplicate_position_blocked(self):
        existing = {
            "AAPL": Position(
                symbol="AAPL",
                qty=10,
                avg_entry_price=100.0,
                market_value=1_000.0,
            )
        }
        rej = _mgr().evaluate(
            _signal(symbol="AAPL"),
            _account(positions=existing),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.DUPLICATE_POSITION

    def test_max_open_positions_blocks_new_entry(self):
        mgr = _mgr(max_open_positions=2)
        existing = {
            "MSFT": Position("MSFT", 1, 300.0, 300.0),
            "GOOG": Position("GOOG", 1, 140.0, 140.0),
        }
        rej = mgr.evaluate(
            _signal(symbol="AAPL"),
            _account(positions=existing),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.MAX_POSITIONS_REACHED

    def test_under_cap_lets_new_entry_through(self):
        mgr = _mgr(max_open_positions=3)
        existing = {"MSFT": Position("MSFT", 1, 300.0, 300.0)}
        result = mgr.evaluate(
            _signal(symbol="AAPL"),
            _account(positions=existing),
            now=T0,
        )
        assert isinstance(result, RiskDecision)


# ── Daily loss circuit breaker ──────────────────────────────────────────────


class TestDailyLoss:
    def test_drawdown_below_threshold_allows_trade(self):
        # hard_dollar_loss_cap intentionally large here so the % gate is the
        # only daily-loss gate in play.
        mgr = _mgr(max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0)
        # Down 4% — still under the 5% gate.
        result = mgr.evaluate(
            _signal(),
            _account(equity=96_000.0, session_start=100_000.0),
            now=T0,
        )
        assert isinstance(result, RiskDecision)
        assert mgr.is_halted() is False

    def test_drawdown_at_or_above_threshold_halts(self):
        mgr = _mgr(max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0)
        rej = mgr.evaluate(
            _signal(),
            _account(equity=95_000.0, session_start=100_000.0),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.DAILY_LOSS_LIMIT
        assert mgr.is_halted() is True

    def test_halt_persists_for_subsequent_signals(self):
        mgr = _mgr(max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0)
        mgr.evaluate(
            _signal(),
            _account(equity=95_000.0, session_start=100_000.0),
            now=T0,
        )
        # Even with a healthy account, manager stays halted.
        rej2 = mgr.evaluate(_signal(symbol="MSFT"), _account(), now=T0)
        assert isinstance(rej2, RiskRejection)
        assert rej2.code is RejectionCode.HALTED

    def test_reset_kill_switches_clears_halt(self):
        mgr = _mgr(max_daily_loss_pct=0.05, hard_dollar_loss_cap=1_000_000.0)
        mgr.evaluate(
            _signal(),
            _account(equity=95_000.0, session_start=100_000.0),
            now=T0,
        )
        assert mgr.is_halted()
        mgr.reset_kill_switches()
        assert not mgr.is_halted()
        result = mgr.evaluate(_signal(), _account(), now=T0)
        assert isinstance(result, RiskDecision)


# ── Hard dollar cap ─────────────────────────────────────────────────────────


class TestHardDollarCap:
    def test_dollar_loss_below_cap_allows(self):
        mgr = _mgr(hard_dollar_loss_cap=2_000.0, max_daily_loss_pct=0.99)
        result = mgr.evaluate(
            _signal(),
            _account(equity=98_500.0, session_start=100_000.0),
            now=T0,
        )
        assert isinstance(result, RiskDecision)

    def test_dollar_loss_at_cap_halts(self):
        # max_daily_loss_pct set high so daily-loss doesn't trip first.
        mgr = _mgr(hard_dollar_loss_cap=2_000.0, max_daily_loss_pct=0.99)
        rej = mgr.evaluate(
            _signal(),
            _account(equity=98_000.0, session_start=100_000.0),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.HARD_DOLLAR_CAP
        assert mgr.is_halted()

    def test_account_check_prefers_alpaca_previous_close(self):
        mgr = _mgr(hard_dollar_loss_cap=2_000.0, max_daily_loss_pct=0.99)
        code = mgr.evaluate_account(
            _account(
                equity=98_000.0,
                session_start=98_000.0,
                previous_close=100_000.0,
            )
        )
        assert code is RejectionCode.HARD_DOLLAR_CAP
        assert mgr.is_halted()
        assert "previous close" in (mgr.halt_reason() or "")

    def test_account_check_falls_back_to_process_session_start(self):
        mgr = _mgr(hard_dollar_loss_cap=2_000.0, max_daily_loss_pct=0.99)
        code = mgr.evaluate_account(
            _account(equity=98_000.0, session_start=100_000.0)
        )
        assert code is RejectionCode.HARD_DOLLAR_CAP
        assert "session start" in (mgr.halt_reason() or "")

    def test_signal_evaluation_uses_previous_close_after_restart(self):
        mgr = _mgr(hard_dollar_loss_cap=2_000.0, max_daily_loss_pct=0.99)
        rej = mgr.evaluate(
            _signal(),
            _account(
                equity=98_000.0,
                session_start=98_000.0,
                previous_close=100_000.0,
            ),
            now=T0,
        )
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.HARD_DOLLAR_CAP


# ── Strategy cooldown ───────────────────────────────────────────────────────


class TestStrategyCooldown:
    def test_consecutive_losses_disable_strategy(self):
        mgr = _mgr(loss_streak_threshold=3, loss_streak_cooldown_hours=24)
        for _ in range(3):
            mgr.record_trade_result("sma_crossover", -100.0, now=T0)
        rej = mgr.evaluate(_signal(), _account(), now=T0)
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.STRATEGY_COOLDOWN

    def test_win_resets_loss_streak(self):
        mgr = _mgr(loss_streak_threshold=3)
        mgr.record_trade_result("sma_crossover", -100.0, now=T0)
        mgr.record_trade_result("sma_crossover", -100.0, now=T0)
        mgr.record_trade_result("sma_crossover", +50.0, now=T0)  # reset
        mgr.record_trade_result("sma_crossover", -100.0, now=T0)
        # Streak is now 1, not 3 — should still be tradable.
        result = mgr.evaluate(_signal(), _account(), now=T0)
        assert isinstance(result, RiskDecision)

    def test_cooldown_elapses(self):
        mgr = _mgr(loss_streak_threshold=2, loss_streak_cooldown_hours=1)
        for _ in range(2):
            mgr.record_trade_result("sma_crossover", -100.0, now=T0)
        # Just past 1 hour later
        later = T0 + timedelta(hours=1, seconds=1)
        result = mgr.evaluate(_signal(), _account(), now=later)
        assert isinstance(result, RiskDecision)

    def test_cooldown_is_per_strategy(self):
        mgr = _mgr(loss_streak_threshold=2)
        for _ in range(2):
            mgr.record_trade_result("losing_strat", -100.0, now=T0)
        # Different strategy still trades.
        result = mgr.evaluate(
            _signal(strategy="other_strat"), _account(), now=T0
        )
        assert isinstance(result, RiskDecision)


# ── Broker error streak ─────────────────────────────────────────────────────


class TestBrokerErrorStreak:
    def test_threshold_engages_kill_switch(self):
        mgr = _mgr(broker_error_threshold=3, broker_error_window_seconds=60)
        for i in range(3):
            mgr.record_broker_error(now=T0 + timedelta(seconds=i))
        assert mgr.is_halted()
        assert "broker errors" in mgr.halt_reason()

    def test_old_errors_age_out_of_window(self):
        mgr = _mgr(broker_error_threshold=3, broker_error_window_seconds=60)
        # 2 errors a long time ago — should not count toward today's streak.
        mgr.record_broker_error(now=T0 - timedelta(minutes=10))
        mgr.record_broker_error(now=T0 - timedelta(minutes=9))
        # Now 2 fresh errors — total in-window = 2, below threshold.
        mgr.record_broker_error(now=T0)
        mgr.record_broker_error(now=T0 + timedelta(seconds=1))
        assert not mgr.is_halted()
        # One more fresh error → 3 in window → halt.
        mgr.record_broker_error(now=T0 + timedelta(seconds=2))
        assert mgr.is_halted()


# ── Slippage drift ──────────────────────────────────────────────────────────


class TestSlippageDrift:
    def test_below_min_samples_no_halt(self):
        mgr = _mgr(slippage_min_samples=5, slippage_drift_multiplier=3.0)
        # 4 samples way over threshold — but min not met.
        for _ in range(4):
            mgr.record_fill_slippage(modeled_bps=5.0, realized_bps=100.0)
        assert not mgr.is_halted()

    def test_drift_engages_kill_switch(self):
        mgr = _mgr(slippage_min_samples=5, slippage_drift_multiplier=3.0)
        for _ in range(5):
            mgr.record_fill_slippage(modeled_bps=5.0, realized_bps=20.0)
        # mean realized 20 > 3 * mean modeled 5 (=15) → halt
        assert mgr.is_halted()
        assert "slippage drift" in mgr.halt_reason()

    def test_within_drift_no_halt(self):
        mgr = _mgr(slippage_min_samples=5, slippage_drift_multiplier=3.0)
        for _ in range(5):
            mgr.record_fill_slippage(modeled_bps=5.0, realized_bps=10.0)
        # mean realized 10 ≤ 15 → no halt
        assert not mgr.is_halted()

    def test_negative_slippage_rejected(self):
        mgr = _mgr()
        with pytest.raises(ValueError):
            mgr.record_fill_slippage(modeled_bps=-1.0, realized_bps=5.0)

    def test_zero_modeled_skips_ratio_check_no_halt(self):
        """modeled_bps=0 must not trigger the kill switch via epsilon trick.
        Previously the code used max(modeled_mean, 1e-9) which caused any
        positive realized slippage to exceed the threshold."""
        mgr = _mgr(slippage_min_samples=5, slippage_drift_multiplier=3.0)
        for _ in range(5):
            mgr.record_fill_slippage(modeled_bps=0.0, realized_bps=50.0)
        assert not mgr.is_halted()

    def test_flag_disabled_prevents_halt(self):
        """With slippage_drift_enabled=False the kill switch never fires,
        even when the ratio clearly exceeds the threshold."""
        mgr = _mgr(
            slippage_min_samples=5,
            slippage_drift_multiplier=3.0,
            slippage_drift_enabled=False,
        )
        for _ in range(5):
            mgr.record_fill_slippage(modeled_bps=5.0, realized_bps=100.0)
        assert not mgr.is_halted()

    def test_flag_enabled_still_halts_on_drift(self):
        """Sanity-check: when the flag is explicitly enabled, the kill switch
        fires normally on genuine drift."""
        mgr = _mgr(
            slippage_min_samples=5,
            slippage_drift_multiplier=3.0,
            slippage_drift_enabled=True,
        )
        for _ in range(5):
            mgr.record_fill_slippage(modeled_bps=5.0, realized_bps=20.0)
        assert mgr.is_halted()
        assert "slippage drift" in mgr.halt_reason()


# ── RiskDecision invariants ─────────────────────────────────────────────────


class TestRiskDecisionInvariant:
    """Direct construction must still satisfy the contract."""

    def test_zero_qty_rejected(self):
        with pytest.raises(ValueError):
            RiskDecision(
                symbol="AAPL",
                side=Side.BUY,
                qty=0,
                entry_reference_price=100.0,
                stop_price=95.0,
                strategy_name="x",
                reason="r",
            )

    def test_long_stop_above_entry_rejected(self):
        with pytest.raises(ValueError):
            RiskDecision(
                symbol="AAPL",
                side=Side.BUY,
                qty=10,
                entry_reference_price=100.0,
                stop_price=110.0,
                strategy_name="x",
                reason="r",
            )

    def test_short_stop_below_entry_rejected(self):
        with pytest.raises(ValueError):
            RiskDecision(
                symbol="AAPL",
                side=Side.SELL,
                qty=10,
                entry_reference_price=100.0,
                stop_price=95.0,
                strategy_name="x",
                reason="r",
            )

    def test_negative_prices_rejected(self):
        with pytest.raises(ValueError):
            RiskDecision(
                symbol="AAPL",
                side=Side.BUY,
                qty=10,
                entry_reference_price=-1.0,
                stop_price=0.5,
                strategy_name="x",
                reason="r",
            )


# ── STOP_LIMIT shape (PLAN 11.47) ───────────────────────────────────────────


class TestRiskDecisionStopLimitShape:
    """STOP_LIMIT entries require trigger + limit and the BUY ordering
    stop_price < entry_trigger_price <= limit_price."""

    def _kwargs(self, **overrides):
        base = dict(
            symbol="NVDA",
            side=Side.BUY,
            qty=10,
            entry_reference_price=100.0,
            stop_price=95.0,
            strategy_name="donchian_breakout",
            reason="r",
            order_type=OrderType.STOP_LIMIT,
            entry_trigger_price=100.5,
            limit_price=101.0,
        )
        base.update(overrides)
        return base

    def test_valid_buy_stop_limit_accepted(self):
        decision = RiskDecision(**self._kwargs())
        assert decision.order_type is OrderType.STOP_LIMIT
        assert decision.entry_trigger_price == 100.5
        assert decision.limit_price == 101.0

    def test_missing_trigger_rejected(self):
        with pytest.raises(ValueError, match="entry_trigger_price"):
            RiskDecision(**self._kwargs(entry_trigger_price=None))

    def test_zero_trigger_rejected(self):
        with pytest.raises(ValueError, match="entry_trigger_price"):
            RiskDecision(**self._kwargs(entry_trigger_price=0))

    def test_missing_limit_rejected(self):
        with pytest.raises(ValueError, match="limit_price"):
            RiskDecision(**self._kwargs(limit_price=None))

    def test_buy_trigger_at_or_below_stop_rejected(self):
        # trigger == stop
        with pytest.raises(ValueError, match="strictly above"):
            RiskDecision(**self._kwargs(entry_trigger_price=95.0))
        # trigger < stop
        with pytest.raises(ValueError, match="strictly above"):
            RiskDecision(**self._kwargs(entry_trigger_price=94.0))

    def test_buy_limit_below_trigger_rejected(self):
        with pytest.raises(ValueError, match="limit_price"):
            RiskDecision(**self._kwargs(entry_trigger_price=100.5, limit_price=100.0))

    def test_buy_limit_equal_to_trigger_accepted(self):
        # No-chase: limit == trigger is the tightest valid form.
        decision = RiskDecision(
            **self._kwargs(entry_trigger_price=100.5, limit_price=100.5)
        )
        assert decision.limit_price == decision.entry_trigger_price

    def test_entry_trigger_price_rejected_on_market(self):
        with pytest.raises(ValueError, match="only valid on STOP_LIMIT"):
            RiskDecision(
                **self._kwargs(
                    order_type=OrderType.MARKET,
                    limit_price=None,
                    entry_trigger_price=100.5,
                )
            )

    def test_entry_trigger_price_rejected_on_limit(self):
        with pytest.raises(ValueError, match="only valid on STOP_LIMIT"):
            RiskDecision(
                **self._kwargs(
                    order_type=OrderType.LIMIT,
                    limit_price=99.0,
                    entry_trigger_price=100.5,
                )
            )

    def test_entry_max_price_rejected_on_stop_limit(self):
        with pytest.raises(ValueError, match="STOP_LIMIT"):
            RiskDecision(**self._kwargs(entry_max_price=102.0))

    def test_fractional_qty_rejected_on_stop_limit(self):
        # Alpaca rejects fractional stop-limit; the RiskDecision invariant
        # catches manually-constructed decisions before they reach the broker.
        with pytest.raises(ValueError, match="whole-share"):
            RiskDecision(**self._kwargs(qty=10.5))

    def test_integer_valued_float_qty_accepted_on_stop_limit(self):
        # qty=10.0 (float but integer-valued) is fine — it's the round-trip
        # form _size_position produces after math.floor.
        decision = RiskDecision(**self._kwargs(qty=10.0))
        assert decision.qty == 10.0


# ── Halt behaviour ──────────────────────────────────────────────────────────


class TestKillSwitchHalt:
    def test_halted_blocks_every_signal_with_HALTED_code(self):
        mgr = _mgr()
        # Manually engage via the broker-error path.
        for i in range(3):
            mgr.record_broker_error(now=T0 + timedelta(seconds=i))
        assert mgr.is_halted()
        rej = mgr.evaluate(_signal(), _account(), now=T0)
        assert isinstance(rej, RiskRejection)
        assert rej.code is RejectionCode.HALTED
