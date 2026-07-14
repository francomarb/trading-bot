"""
Risk Management layer (Phase 6).

The risk layer is the *gatekeeper* between strategy signals and order placement.
The Phase 7 broker's `place_order(decision: RiskDecision)` will accept *only*
a `RiskDecision` — and the only legitimate way to obtain one is through
`RiskManager.evaluate(signal, account_state)`. This makes unsafe order
placement structurally impossible.

Design principles
-----------------
1. **Mandatory gate.** `RiskDecision` is the API token. Strategies emit
   `Signal`s; only `RiskManager` produces `RiskDecision`s. Anything else gets
   a typed `RiskRejection`.

2. **Stop is defined before entry.** Position size is derived from the
   distance between entry reference price and the ATR-based stop, so the
   *dollar* loss on a stop-out equals the strategy's risk target
   (`STRATEGY_RISK_PER_TRADE_PCT`, 11.48) regardless of the symbol's
   volatility. `MAX_POSITION_PCT` is the global hard ceiling above the
   per-strategy targets, and the sizing fallback for strategies without
   a target (options / credit-spread paths own their sizing).

   Sizing is also capped by per-position notional exposure. A tight stop must
   not let one trade consume the whole gross-exposure sleeve. The 11.48
   targets are derived (docs/allocator_risk_target_reconciliation.md §3)
   so these caps bind exceptionally, not routinely — a cap that overrules
   the risk-sized qty logs the clip and the binding cap by name.

3. **Multiple independent kill switches.** Daily-loss %, hard-dollar cap,
   broker-error streak, and slippage drift each halt trading independently.
   Account-loss halts stay sticky for the broker baseline they tripped on;
   non-account halts stay halted until `reset_kill_switches()` is explicitly
   called by an operator.

4. **Per-strategy loss-streak cooldown.** A strategy that posts N consecutive
   losses gets disabled for K hours; other strategies keep running.

5. **In-memory state.** This MVP keeps state in process memory. Phases 8/9
   will persist it via the trade log so the engine survives restarts; the
   `RiskManager` API stays the same.

The `evaluate` flow (in order of cheapness → expense):
    halted? → strategy in cooldown? → duplicate position? → max positions?
        → daily-loss tripped? → compute stop & qty → gross exposure ok?
        → buying-power ok? → return RiskDecision
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Deque

from loguru import logger

from config import settings
from strategies.base import OrderType


# ── Public types ─────────────────────────────────────────────────────────────


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class Position:
    """Snapshot of a single open broker position."""

    symbol: str
    qty: float
    avg_entry_price: float
    market_value: float  # current market value (qty * last price)
    current_price: float | None = None
    cost_basis: float | None = None
    unrealized_pl: float | None = None
    unrealized_plpc: float | None = None


@dataclass(frozen=True)
class AccountState:
    """
    Snapshot of broker state passed into `RiskManager.evaluate`.

    `previous_close_equity` is Alpaca's prior trading-day close and is the
    preferred baseline for daily-loss and hard-dollar kill switches. The
    process-local `session_start_equity` is retained as a conservative fallback
    when the broker does not provide prior-close equity.
    """

    equity: float
    cash: float
    session_start_equity: float
    previous_close_equity: float | None = None
    open_positions: dict[str, Position] = field(default_factory=dict)

    def gross_exposure(self) -> float:
        """Sum of |market_value| across open positions."""
        return sum(abs(p.market_value) for p in self.open_positions.values())


@dataclass(frozen=True)
class Signal:
    """
    Strategy → Risk hand-off.

    `reference_price` is the price at which we'd execute (typically the next
    bar's open estimate, or the latest close — engine's choice). `atr` is the
    current ATR value used to derive the stop. `strategy_name` is required so
    the loss-streak cooldown can be applied per-strategy.
    """

    symbol: str
    side: Side
    strategy_name: str
    reference_price: float
    atr: float
    reason: str = ""  # human-readable trade thesis (logged with the decision)
    # Strategy declares its preferred entry order type (PLAN 4.8). Hard-risk
    # exits (stop-outs, circuit breakers) are always market regardless.
    order_type: OrderType = OrderType.MARKET
    # Required when order_type is LIMIT or STOP_LIMIT.
    limit_price: float | None = None
    # For bracket options orders
    take_profit_price: float | None = None
    stop_price_override: float | None = None
    # PLAN 11.32 entry price cap: worst-case fill ceiling (BUY) / floor (SELL).
    # When set on a MARKET signal, the broker submits a marketable DAY LIMIT
    # + OTO at exactly this price instead of an unbounded market order.
    # LIMIT signals must not carry this — they already control their fill price.
    entry_max_price: float | None = None
    # PLAN 11.47 STOP_LIMIT entries: broker-resting stop trigger above the
    # prior-N-day high. Required only when order_type is STOP_LIMIT.
    entry_trigger_price: float | None = None


class RejectionCode(str, Enum):
    HALTED = "halted"
    STRATEGY_COOLDOWN = "strategy_cooldown"
    DUPLICATE_POSITION = "duplicate_position"
    MAX_POSITIONS_REACHED = "max_positions_reached"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    HARD_DOLLAR_CAP = "hard_dollar_cap"
    INVALID_SIGNAL = "invalid_signal"
    INVALID_STOP = "invalid_stop"
    POSITION_TOO_SMALL = "position_too_small"
    GROSS_EXPOSURE_CAP = "gross_exposure_cap"
    INSUFFICIENT_CASH = "insufficient_cash"
    UNSUPPORTED_SIDE = "unsupported_side"


@dataclass(frozen=True)
class RiskRejection:
    """Returned from `evaluate` when a signal is blocked."""

    code: RejectionCode
    message: str
    symbol: str
    strategy_name: str


@dataclass(frozen=True)
class RiskDecision:
    """
    The *only* legitimate input to `AlpacaBroker.place_order` (Phase 7).

    Produced exclusively by `RiskManager.evaluate`. Carries everything the
    execution layer needs to place the entry order and a separately-attached
    stop order. `entry_reference_price` is recorded for slippage attribution
    in Phase 9 (realized vs. modeled).
    """

    symbol: str
    side: Side
    qty: float  # int when FRACTIONAL_ENABLED=False, float (2 dp) when True
    entry_reference_price: float
    stop_price: float
    strategy_name: str
    reason: str
    # Strategy-chosen entry type. Phase 7 broker reads this and routes
    # accordingly. Defaults to MARKET so legacy construction still works.
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    take_profit_price: float | None = None
    # PLAN 11.32 — see Signal.entry_max_price. Set by the engine after
    # gate_entry() decides to convert a MARKET entry to a capped marketable
    # DAY LIMIT. Broker honors this regardless of order_type.
    entry_max_price: float | None = None
    # PLAN 11.47 STOP_LIMIT entries — see Signal.entry_trigger_price.
    entry_trigger_price: float | None = None

    def __post_init__(self) -> None:
        # Defensive: any caller that constructs this manually still has to pass
        # a sane shape. The contract enforcement (caller is RiskManager) is
        # cultural; these checks make a malformed decision impossible.
        if self.qty <= 0:
            raise ValueError(f"qty must be positive, got {self.qty}")
        if self.entry_reference_price <= 0:
            raise ValueError(
                f"entry_reference_price must be positive, got {self.entry_reference_price}"
            )
        if self.stop_price <= 0:
            raise ValueError(f"stop_price must be positive, got {self.stop_price}")
        if self.side is Side.BUY and self.stop_price >= self.entry_reference_price:
            raise ValueError(
                "long stop must be strictly below entry "
                f"(entry={self.entry_reference_price}, stop={self.stop_price})"
            )
        if self.side is Side.SELL and self.stop_price <= self.entry_reference_price:
            raise ValueError(
                "short stop must be strictly above entry "
                f"(entry={self.entry_reference_price}, stop={self.stop_price})"
            )
        if self.order_type is OrderType.LIMIT and (
            self.limit_price is None or self.limit_price <= 0
        ):
            raise ValueError(
                f"LIMIT order requires a positive limit_price, got {self.limit_price!r}"
            )
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET order must not carry a limit_price")
        # PLAN 11.47 STOP_LIMIT shape: trigger and limit are both required and
        # ordered. For a BUY breakout: stop_price < entry_trigger_price <=
        # limit_price. The stop is the protective stop (below entry); the
        # trigger arms the stop-limit at the prior-N-day high; the limit caps
        # the chase after the trigger fires.
        if self.order_type is OrderType.STOP_LIMIT:
            # Alpaca rejects fractional stop-limit. Sizing (`_size_position`)
            # already floors to int for STOP_LIMIT, but enforce here as a
            # last-line invariant for manually-constructed decisions.
            if float(self.qty).is_integer() is False:
                raise ValueError(
                    f"STOP_LIMIT order requires whole-share qty (Alpaca "
                    f"rejects fractional stop-limit), got qty={self.qty}"
                )
            if self.entry_trigger_price is None or self.entry_trigger_price <= 0:
                raise ValueError(
                    "STOP_LIMIT order requires a positive entry_trigger_price, "
                    f"got {self.entry_trigger_price!r}"
                )
            if self.limit_price is None or self.limit_price <= 0:
                raise ValueError(
                    "STOP_LIMIT order requires a positive limit_price, "
                    f"got {self.limit_price!r}"
                )
            if self.side is Side.BUY:
                if self.entry_trigger_price <= self.stop_price:
                    raise ValueError(
                        f"BUY STOP_LIMIT entry_trigger_price "
                        f"{self.entry_trigger_price} must be strictly above "
                        f"stop_price {self.stop_price}"
                    )
                if self.limit_price < self.entry_trigger_price:
                    raise ValueError(
                        f"BUY STOP_LIMIT limit_price {self.limit_price} must be "
                        f">= entry_trigger_price {self.entry_trigger_price}"
                    )
            else:  # SELL
                if self.entry_trigger_price >= self.stop_price:
                    raise ValueError(
                        f"SELL STOP_LIMIT entry_trigger_price "
                        f"{self.entry_trigger_price} must be strictly below "
                        f"stop_price {self.stop_price}"
                    )
                if self.limit_price > self.entry_trigger_price:
                    raise ValueError(
                        f"SELL STOP_LIMIT limit_price {self.limit_price} must be "
                        f"<= entry_trigger_price {self.entry_trigger_price}"
                    )
        elif self.entry_trigger_price is not None:
            raise ValueError(
                "entry_trigger_price is only valid on STOP_LIMIT orders, "
                f"got order_type={self.order_type.value}"
            )
        if self.entry_max_price is not None:
            if self.entry_max_price <= 0:
                raise ValueError(
                    f"entry_max_price must be positive, got {self.entry_max_price}"
                )
            if self.order_type is OrderType.LIMIT:
                raise ValueError(
                    "entry_max_price is for capping MARKET entries; "
                    "LIMIT orders set their fill price via limit_price"
                )
            if self.order_type is OrderType.STOP_LIMIT:
                raise ValueError(
                    "entry_max_price is for capping MARKET entries; "
                    "STOP_LIMIT orders cap their fill via limit_price"
                )
            if self.side is Side.BUY and self.entry_max_price < self.stop_price:
                raise ValueError(
                    f"BUY entry_max_price {self.entry_max_price} must be "
                    f">= stop_price {self.stop_price}"
                )


# ── Manager ──────────────────────────────────────────────────────────────────


class RiskManager:
    """
    Stateful risk gatekeeper. One instance lives for the duration of a session.

    State that persists across `evaluate` calls:
      - per-strategy loss streak + disabled-until timestamp
      - rolling broker-error timestamps
      - rolling (modeled, realized) slippage samples
      - kill-switch engaged flag + reason
    """

    def __init__(
        self,
        *,
        max_position_pct: float = settings.MAX_POSITION_PCT,
        risk_per_trade_pct_by_strategy: dict[str, float] | None = None,
        max_position_notional_pct: float = settings.MAX_POSITION_NOTIONAL_PCT,
        max_open_positions: int = settings.MAX_OPEN_POSITIONS,
        max_gross_exposure_pct: float = settings.MAX_GROSS_EXPOSURE_PCT,
        atr_stop_multiplier: float = settings.ATR_STOP_MULTIPLIER,
        max_daily_loss_pct: float = settings.MAX_DAILY_LOSS_PCT,
        hard_dollar_loss_cap: float = settings.HARD_DOLLAR_LOSS_CAP,
        loss_streak_threshold: int = settings.LOSS_STREAK_THRESHOLD,
        loss_streak_cooldown_hours: float = settings.LOSS_STREAK_COOLDOWN_HOURS,
        broker_error_threshold: int = settings.BROKER_ERROR_STREAK_THRESHOLD,
        broker_error_window_seconds: float = settings.BROKER_ERROR_WINDOW_SECONDS,
        slippage_min_samples: int = settings.SLIPPAGE_DRIFT_MIN_SAMPLES,
        slippage_drift_multiplier: float = settings.SLIPPAGE_DRIFT_MULTIPLIER,
        slippage_drift_enabled: bool = settings.SLIPPAGE_DRIFT_ENABLED,
    ) -> None:
        # Validate config — bad knobs should fail loudly at construction, not
        # silently disable a rule mid-session.
        if not (0 < max_position_pct < 1):
            raise ValueError("max_position_pct must be in (0, 1)")
        # 11.48: per-strategy risk-to-stop targets beneath the global
        # ceiling. None → production defaults from settings. A target at
        # or above max_position_pct would defeat the ceiling, and the
        # derivation contract (docs/allocator_risk_target_reconciliation.md
        # §3) requires targets small enough that the notional caps do not
        # do the sizing.
        if risk_per_trade_pct_by_strategy is None:
            risk_per_trade_pct_by_strategy = dict(
                settings.STRATEGY_RISK_PER_TRADE_PCT
            )
        for _strat, _pct in risk_per_trade_pct_by_strategy.items():
            if not (0 < _pct < max_position_pct):
                raise ValueError(
                    f"risk_per_trade_pct_by_strategy['{_strat}'] = {_pct} "
                    f"must be in (0, max_position_pct={max_position_pct})"
                )
        if not (0 < max_position_notional_pct <= 1):
            raise ValueError("max_position_notional_pct must be in (0, 1]")
        if max_open_positions < 1:
            raise ValueError("max_open_positions must be >= 1")
        if not (0 < max_gross_exposure_pct <= 5):
            raise ValueError("max_gross_exposure_pct must be in (0, 5]")
        if atr_stop_multiplier <= 0:
            raise ValueError("atr_stop_multiplier must be > 0")
        if not (0 < max_daily_loss_pct < 1):
            raise ValueError("max_daily_loss_pct must be in (0, 1)")
        if hard_dollar_loss_cap <= 0:
            raise ValueError("hard_dollar_loss_cap must be > 0")
        if loss_streak_threshold < 1:
            raise ValueError("loss_streak_threshold must be >= 1")
        if loss_streak_cooldown_hours <= 0:
            raise ValueError("loss_streak_cooldown_hours must be > 0")
        if broker_error_threshold < 1:
            raise ValueError("broker_error_threshold must be >= 1")
        if broker_error_window_seconds <= 0:
            raise ValueError("broker_error_window_seconds must be > 0")
        if slippage_min_samples < 1:
            raise ValueError("slippage_min_samples must be >= 1")
        if slippage_drift_multiplier <= 1:
            raise ValueError("slippage_drift_multiplier must be > 1")

        self.slippage_drift_enabled = slippage_drift_enabled
        self.max_position_pct = max_position_pct
        self.risk_per_trade_pct_by_strategy = risk_per_trade_pct_by_strategy
        self.max_position_notional_pct = max_position_notional_pct
        self.max_open_positions = max_open_positions
        self.max_gross_exposure_pct = max_gross_exposure_pct
        self.atr_stop_multiplier = atr_stop_multiplier
        self.max_daily_loss_pct = max_daily_loss_pct
        self.hard_dollar_loss_cap = hard_dollar_loss_cap
        self.loss_streak_threshold = loss_streak_threshold
        self.loss_streak_cooldown = timedelta(hours=loss_streak_cooldown_hours)
        self.broker_error_threshold = broker_error_threshold
        self.broker_error_window = timedelta(seconds=broker_error_window_seconds)
        self.slippage_min_samples = slippage_min_samples
        self.slippage_drift_multiplier = slippage_drift_multiplier

        # Mutable session state
        self._loss_streak: dict[str, int] = defaultdict(int)
        self._disabled_until: dict[str, datetime] = {}
        self._broker_errors: Deque[datetime] = deque()
        # rolling slippage samples: (modeled_bps, adverse_bps).
        # `adverse_bps` matches the `slippage_adverse_bps` taxonomy on
        # `trades`: signed slippage clamped to `max(0, signed)` so
        # price improvement contributes 0 to the drift mean rather than
        # offsetting later adverse fills (Phase 2 slippage unification).
        self._slippage_samples: Deque[tuple[float, float]] = deque(maxlen=200)
        self._halted: bool = False
        self._halt_reason: str | None = None
        self._halt_code: RejectionCode | None = None
        self._account_halt_baseline: float | None = None
        # Operator Controls Phase B — soft entry pauses. Independent of
        # the kill switch: pauses block NEW entries only; exits, stops,
        # reconciliation, allocator updates, and lifecycle reconciles
        # continue to run. Halt and pauses can coexist; halt is the
        # stricter gate. Resume-after-halt only clears the kill switch
        # (after the F2 operator-prefix reconcile check); resume-entries
        # / resume-strategy clear their own pause flags without any
        # reconciliation. All three pause/halt states persist across
        # restart via `OPERATOR_CONTROL_STATE_PATH` JSON owned by the
        # engine; this class is the in-memory authority while the engine
        # is running.
        self._entries_paused: bool = False
        self._entries_paused_reason: str | None = None
        self._entries_paused_command_uid: str | None = None
        self._paused_strategies: dict[str, dict] = {}

    # ── Kill-switch state ────────────────────────────────────────────────

    def is_halted(self) -> bool:
        return self._halted

    def halt_reason(self) -> str | None:
        return self._halt_reason

    def _engage_kill_switch(
        self,
        reason: str,
        *,
        code: RejectionCode | None = None,
        account_baseline: float | None = None,
    ) -> None:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            self._halt_code = code
            self._account_halt_baseline = account_baseline
            logger.critical(f"RiskManager kill switch engaged: {reason}")

    def reset_kill_switches(self) -> None:
        """Operator-only: clear all halts. Use after manual review."""
        if self._halted:
            logger.warning(
                f"RiskManager kill switch reset (was: {self._halt_reason})"
            )
        self._halted = False
        self._halt_reason = None
        self._halt_code = None
        self._account_halt_baseline = None

    # ── Soft entry pauses (Operator Controls Phase B) ──────────────────
    #
    # All four accessors are pure flag checks — no I/O, no side effects.
    # The engine layer is responsible for persisting state to disk
    # (via `_persist_control_state`) so a restart can rehydrate the
    # in-memory flags here. That separation lets us unit-test pause
    # behavior without touching the filesystem.

    def is_entries_paused(self) -> bool:
        """True when the operator has issued `pause-entries` and not
        yet issued `resume-entries`. Independent of `is_halted()`."""
        return self._entries_paused

    def entries_paused_reason(self) -> str | None:
        return self._entries_paused_reason

    def is_strategy_paused(self, strategy_name: str) -> bool:
        """True when the operator has issued `pause-strategy <name>`
        and not yet issued `resume-strategy <name>`. Independent of
        `is_entries_paused()`: a strategy can be paused individually
        while global pause-entries is off."""
        if not strategy_name:
            return False
        return strategy_name in self._paused_strategies

    def paused_strategies_snapshot(self) -> dict[str, dict]:
        """Read-only snapshot of paused-strategy metadata. Returns a
        deep copy so callers (CLI / dashboard / engine_state.json)
        can iterate without holding a reference into RiskManager state."""
        return {name: dict(meta) for name, meta in self._paused_strategies.items()}

    def pause_entries(
        self,
        *,
        reason: str,
        command_uid: str | None = None,
    ) -> bool:
        """Set the global entries-paused flag.

        Returns True if state changed, False if entries were already
        paused (so the engine handler can write an idempotent succeeded
        result without alerting twice).
        """
        if self._entries_paused:
            return False
        self._entries_paused = True
        self._entries_paused_reason = reason
        self._entries_paused_command_uid = command_uid
        logger.warning(
            f"RiskManager entries paused: {reason} "
            f"(cmd={(command_uid or '-')[:18]}…)"
        )
        return True

    def resume_entries(self) -> bool:
        """Clear the global entries-paused flag.

        Returns True if state changed, False if entries were not paused.
        Resume does NOT require any reconciliation — soft pause is
        a flag flip, distinct from sticky halt's `resume-after-halt`.
        """
        if not self._entries_paused:
            return False
        prior = self._entries_paused_reason
        self._entries_paused = False
        self._entries_paused_reason = None
        self._entries_paused_command_uid = None
        logger.info(f"RiskManager entries resumed (prior reason: {prior!r})")
        return True

    def pause_strategy(
        self,
        *,
        strategy_name: str,
        reason: str,
        command_uid: str | None = None,
    ) -> bool:
        """Pause entries for one strategy only.

        Returns True if state changed, False if the strategy was
        already paused.
        """
        if not strategy_name:
            raise ValueError("strategy_name must not be empty")
        if strategy_name in self._paused_strategies:
            return False
        from datetime import datetime, timezone

        self._paused_strategies[strategy_name] = {
            "reason": reason,
            "command_uid": command_uid,
            "paused_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.warning(
            f"RiskManager strategy '{strategy_name}' paused: {reason} "
            f"(cmd={(command_uid or '-')[:18]}…)"
        )
        return True

    def resume_strategy(self, *, strategy_name: str) -> bool:
        """Clear the per-strategy pause flag for one strategy.

        Returns True if state changed, False if the strategy was not
        paused.
        """
        if not strategy_name:
            raise ValueError("strategy_name must not be empty")
        meta = self._paused_strategies.pop(strategy_name, None)
        if meta is None:
            return False
        logger.info(
            f"RiskManager strategy '{strategy_name}' resumed "
            f"(prior reason: {meta.get('reason')!r})"
        )
        return True

    def _account_limit_breach(
        self, account: AccountState
    ) -> tuple[RejectionCode, str, float] | None:
        """Return the first account-level loss-limit breach, if any."""
        baseline, baseline_name = self._account_loss_baseline(account)
        if baseline <= 0:
            return None

        drawdown_pct = (baseline - account.equity) / baseline
        if drawdown_pct >= self.max_daily_loss_pct:
            return (
                RejectionCode.DAILY_LOSS_LIMIT,
                f"daily loss {drawdown_pct * 100:.2f}% >= "
                f"{self.max_daily_loss_pct * 100:.2f}% from {baseline_name}",
                baseline,
            )

        dollar_loss = baseline - account.equity
        if dollar_loss >= self.hard_dollar_loss_cap:
            return (
                RejectionCode.HARD_DOLLAR_CAP,
                f"dollar loss ${dollar_loss:.2f} >= "
                f"cap ${self.hard_dollar_loss_cap:.2f} from {baseline_name}",
                baseline,
            )
        return None

    def _account_loss_baseline(self, account: AccountState) -> tuple[float, str]:
        baseline = getattr(account, "previous_close_equity", None)
        baseline_name = "previous close"
        if baseline is None or baseline <= 0:
            baseline = getattr(account, "session_start_equity", 0.0)
            baseline_name = "session start"
        return float(baseline or 0.0), baseline_name

    def _halt_is_account_loss(self) -> bool:
        return self._halt_code in {
            RejectionCode.DAILY_LOSS_LIMIT,
            RejectionCode.HARD_DOLLAR_CAP,
        }

    def evaluate_account(self, account: AccountState) -> RejectionCode | None:
        """
        Evaluate account-wide kill switches without requiring an entry signal.

        This is run from each fresh broker snapshot so defined-risk strategies
        that do not use signal sizing cannot bypass global loss limits. Using
        Alpaca's prior-close equity also makes the halt re-engage after a bot
        recycle during the same trading day. Account-loss halts are recomputed
        when the broker baseline rolls over; non-account halts remain sticky
        until an operator reset.
        """
        if self._halted:
            if not self._halt_is_account_loss():
                return RejectionCode.HALTED

            baseline, baseline_name = self._account_loss_baseline(account)
            baseline_changed = (
                self._account_halt_baseline is None
                or not math.isclose(
                    baseline,
                    self._account_halt_baseline,
                    rel_tol=0.0,
                    abs_tol=0.01,
                )
            )
            if not baseline_changed:
                return RejectionCode.HALTED

            breach = self._account_limit_breach(account)
            if breach is None:
                logger.warning(
                    "RiskManager account-loss halt cleared after broker "
                    f"baseline rollover to {baseline_name} "
                    f"${baseline:,.2f} (was: {self._halt_reason})"
                )
                self._halted = False
                self._halt_reason = None
                self._halt_code = None
                self._account_halt_baseline = None
                return None

            code, message, breach_baseline = breach
            if message != self._halt_reason:
                logger.critical(
                    "RiskManager account-loss halt refreshed after broker "
                    f"baseline rollover: {message} "
                    f"(was: {self._halt_reason})"
                )
                self._halt_reason = message
            self._halt_code = code
            self._account_halt_baseline = breach_baseline
            return code
        breach = self._account_limit_breach(account)
        if breach is None:
            return None
        code, message, baseline = breach
        self._engage_kill_switch(
            message,
            code=code,
            account_baseline=baseline,
        )
        return code

    def cooldown_snapshot(
        self, *, now: datetime | None = None,
    ) -> dict[str, dict]:
        """Read-only per-strategy cooldown state.

        Surfaces the loss-streak cooldown state for the engine state
        snapshot. Consumed by HealthAssessor L1 checks (PLAN 11.10d/f)
        to surface "strategy currently in cooldown" as a WATCH finding.
        Pure read; no mutation.

        Returns: `{strategy_name: {"active": bool, "until": ISO str
        or None, "loss_streak": int}}` for every strategy that has a
        loss_streak entry. Strategies with no recorded losses are
        omitted.
        """
        now = now or datetime.now(timezone.utc)
        out: dict[str, dict] = {}
        for strategy_name, streak in self._loss_streak.items():
            until = self._disabled_until.get(strategy_name)
            active = until is not None and now < until
            out[strategy_name] = {
                "active": active,
                "until": until.isoformat() if until else None,
                "loss_streak": streak,
            }
        return out

    # ── External event recorders ─────────────────────────────────────────

    def record_trade_result(
        self,
        strategy_name: str,
        pnl: float,
        *,
        now: datetime | None = None,
    ) -> None:
        """
        Update the per-strategy loss-streak counter. Engages the strategy
        cooldown when the threshold is hit. Wins reset the streak.
        """
        now = now or datetime.now(timezone.utc)
        if pnl < 0:
            self._loss_streak[strategy_name] += 1
            if self._loss_streak[strategy_name] >= self.loss_streak_threshold:
                until = now + self.loss_streak_cooldown
                self._disabled_until[strategy_name] = until
                logger.warning(
                    f"strategy '{strategy_name}' disabled until {until.isoformat()} "
                    f"(loss streak = {self._loss_streak[strategy_name]})"
                )
        else:
            if self._loss_streak[strategy_name] > 0:
                logger.info(
                    f"strategy '{strategy_name}' loss streak reset after winning trade"
                )
            self._loss_streak[strategy_name] = 0

    def record_broker_error(self, *, now: datetime | None = None) -> None:
        """
        Append a broker-error timestamp. Engages the kill switch when ≥
        `broker_error_threshold` errors land within `broker_error_window`.
        """
        now = now or datetime.now(timezone.utc)
        self._broker_errors.append(now)
        cutoff = now - self.broker_error_window
        while self._broker_errors and self._broker_errors[0] < cutoff:
            self._broker_errors.popleft()
        if len(self._broker_errors) >= self.broker_error_threshold:
            self._engage_kill_switch(
                f"broker errors: {len(self._broker_errors)} within "
                f"{self.broker_error_window.total_seconds():.0f}s window"
            )

    def record_fill_slippage(
        self, modeled_bps: float, adverse_bps: float
    ) -> None:
        """
        Append a fill-slippage sample. Once we have at least
        `slippage_min_samples`, logs drift metrics and — if
        `slippage_drift_enabled` — engages the kill switch when
        mean adverse exceeds `slippage_drift_multiplier × mean modeled`.

        Both inputs must be non-negative bps. `adverse_bps` is the
        magnitude of execution drift in the unfavorable direction —
        the same quantity persisted as `slippage_adverse_bps` on
        `trades` (Phase 2 slippage unification): the engine computes
        signed slippage in `_record_fill` and clamps `max(0, signed)`
        before calling. Price improvement contributes 0, never a
        negative value, so a run of good fills can't shift the mean
        downward and mask later drift.

        The kill switch is disabled by default (SLIPPAGE_DRIFT_ENABLED=False)
        during paper trading. Enable only after calibrating the modeled baseline
        against real fills. Must be enabled before going live (Phase 10).
        """
        if modeled_bps < 0 or adverse_bps < 0:
            raise ValueError("slippage bps must be non-negative")
        self._slippage_samples.append((modeled_bps, adverse_bps))
        n = len(self._slippage_samples)
        if n < self.slippage_min_samples:
            return
        modeled_mean = sum(m for m, _ in self._slippage_samples) / n
        adverse_mean = sum(a for _, a in self._slippage_samples) / n

        logger.debug(
            f"slippage monitor: adverse={adverse_mean:.2f}bps "
            f"modeled={modeled_mean:.2f}bps n={n} "
            f"threshold={self.slippage_drift_multiplier}× "
            f"enabled={self.slippage_drift_enabled}"
        )

        # Guard: if modeled baseline is zero we have nothing to compare against.
        # Skip the ratio check rather than using an epsilon that would fire on
        # any positive adverse slippage.
        if modeled_mean == 0.0:
            logger.warning(
                f"slippage monitor: modeled mean is 0 — skipping drift check "
                f"(adverse={adverse_mean:.2f}bps, n={n}). "
                "Set SLIPPAGE_MODEL_MARKET_BPS to a non-zero value to enable."
            )
            return

        if not self.slippage_drift_enabled:
            return

        if adverse_mean > self.slippage_drift_multiplier * modeled_mean:
            self._engage_kill_switch(
                f"slippage drift: adverse mean {adverse_mean:.2f}bps > "
                f"{self.slippage_drift_multiplier}× modeled mean {modeled_mean:.2f}bps "
                f"(n={n})"
            )

    # ── Internal helpers ─────────────────────────────────────────────────

    def _strategy_in_cooldown(
        self, strategy_name: str, now: datetime
    ) -> datetime | None:
        until = self._disabled_until.get(strategy_name)
        if until is None:
            return None
        if now >= until:
            # Cooldown elapsed — clear it and reset the streak.
            self._disabled_until.pop(strategy_name, None)
            self._loss_streak[strategy_name] = 0
            return None
        return until

    def _stop_price_for(self, signal: Signal) -> float:
        """Compute ATR-based stop. Long: entry - k*ATR. Short: entry + k*ATR."""
        if signal.stop_price_override is not None:
            return signal.stop_price_override
        offset = self.atr_stop_multiplier * signal.atr
        if signal.side is Side.BUY:
            return signal.reference_price - offset
        return signal.reference_price + offset

    def _size_position(
        self,
        signal: Signal,
        stop_price: float,
        account: AccountState,
        notional_cap: float | None = None,
    ) -> float:
        """
        Fixed-fractional sizing on stop distance:
            risk_dollars = equity * risk_per_trade_pct[strategy]
                           (fallback: equity * max_position_pct)
            qty          = _floor(risk_dollars / |entry - stop|)
        Then capped by per-position notional exposure, remaining
        gross-exposure budget, available cash, and — when provided —
        the sleeve notional_cap from the SleeveAllocator. Per the 11.48
        derivation the per-strategy targets are small enough that these
        caps clip exceptionally (calmest watchlist names only); any clip
        below the risk-sized qty is logged with the binding cap.

        When settings.FRACTIONAL_ENABLED=True and signal.order_type is MARKET:
          _floor rounds down to 2 decimal places (fractional shares).
          Returns float; broker routes to DAY entry + standalone GTC stop.

        When FRACTIONAL_ENABLED=False (or LIMIT order):
          _floor is math.floor (returns int); exact current behaviour.
          Broker uses OTO GTC exactly as before — this path is byte-for-byte
          identical to the pre-fractional implementation.
        """
        import re
        is_option = bool(re.match(r"^[A-Z]{1,6}[0-9]{6}[CP][0-9]{8}$", signal.symbol))

        # Choose floor function based on fractional mode.
        # LIMIT orders (RSI reversion) always use whole shares — Alpaca GTC
        # limit orders do not support fractional quantities. Options cannot be fractional.
        # STOP_LIMIT (PLAN 11.47, Donchian) is also whole-share — Alpaca rejects
        # fractional stop-limit orders, so we floor to int here and let the
        # POSITION_TOO_SMALL gate below reject sub-share entries explicitly
        # rather than rounding up to 1 share (which would silently amplify
        # risk past the configured budget).
        fractional = (
            settings.FRACTIONAL_ENABLED
            and signal.order_type is OrderType.MARKET
            and not is_option
        )
        _floor = (lambda x: math.floor(x * 100) / 100) if fractional else math.floor

        # PLAN 11.47 R1 P1-3: STOP_LIMIT sizing must use the worst-permitted
        # fill price, not the signal-bar close. The order can fill anywhere
        # from the trigger up to the chase cap (limit_price); sizing on close
        # would understate stop_distance and over-allocate qty, exceeding
        # max_position_pct, the per-position notional cap, sleeve, and cash
        # budgets if the fill lands at the cap. We anchor to limit_price
        # (worst case) for stop_distance and every notional cap below.
        # reference_price stays = close for slippage attribution downstream.
        sizing_price = (
            signal.limit_price
            if signal.order_type is OrderType.STOP_LIMIT and signal.limit_price is not None
            else signal.reference_price
        )

        # 11.48: per-strategy risk-to-stop target; strategies without one
        # (options, credit spread — own sizing semantics) fall back to the
        # global ceiling. See docs/allocator_risk_target_reconciliation.md.
        risk_pct = self.risk_per_trade_pct_by_strategy.get(
            signal.strategy_name, self.max_position_pct
        )
        risk_dollars = account.equity * risk_pct
        stop_distance = abs(sizing_price - stop_price)
        if stop_distance <= 0:
            return 0

        multiplier = 100.0 if is_option else 1.0

        raw_qty = _floor(risk_dollars / (stop_distance * multiplier))
        if raw_qty <= 0:
            return 0

        # The caps below are brakes, not the sizer. Track which one (if
        # any) overrules the risk-sized qty so the clip is visible in the
        # log — the 11.48 derivation keeps clips exceptional, and routine
        # clip logs are the operator's signal that watchlist volatility
        # drifted below the risk target's coverage point.
        risk_qty = raw_qty
        binding_cap: str | None = None

        # Cap by per-position notional budget so tight stops do not consume
        # the whole sleeve in a single position.
        if sizing_price > 0:
            max_position_notional = account.equity * self.max_position_notional_pct
            notional_qty_cap = _floor(max_position_notional / (sizing_price * multiplier))
            if notional_qty_cap < raw_qty:
                raw_qty = notional_qty_cap
                binding_cap = f"global notional cap ${max_position_notional:,.0f}"

        # Cap by remaining gross-exposure budget.
        max_gross = account.equity * self.max_gross_exposure_pct
        remaining_gross = max(0.0, max_gross - account.gross_exposure())
        if sizing_price > 0:
            gross_qty_cap = _floor(remaining_gross / (sizing_price * multiplier))
            if gross_qty_cap < raw_qty:
                raw_qty = gross_qty_cap
                binding_cap = f"remaining gross exposure ${remaining_gross:,.0f}"

        # Cap by cash on hand (a buy must be payable).
        if signal.side is Side.BUY and sizing_price > 0:
            cash_qty_cap = _floor(max(0.0, account.cash) / (sizing_price * multiplier))
            if cash_qty_cap < raw_qty:
                raw_qty = cash_qty_cap
                binding_cap = f"cash on hand ${max(0.0, account.cash):,.0f}"

        # Cap by sleeve budget (supplied by SleeveAllocator when active).
        # This prevents one strategy from consuming another's reserved capital.
        if notional_cap is not None and sizing_price > 0:
            sleeve_qty_cap = _floor(notional_cap / (sizing_price * multiplier))
            if sleeve_qty_cap < raw_qty:
                raw_qty = sleeve_qty_cap
                binding_cap = f"sleeve notional_cap=${notional_cap:,.0f}"

        raw_qty = max(raw_qty, 0)
        if binding_cap is not None and raw_qty < risk_qty:
            implied_risk = raw_qty * stop_distance * multiplier
            logger.info(
                f"[{signal.strategy_name}] {signal.symbol}: risk-sized qty "
                f"{risk_qty}→{raw_qty} clipped by {binding_cap} — implied "
                f"risk ${implied_risk:,.0f} vs target ${risk_dollars:,.0f} "
                f"({risk_pct:.2%} of equity)"
            )
        return raw_qty

    @staticmethod
    def _reject(
        code: RejectionCode, message: str, signal: Signal
    ) -> RiskRejection:
        rej = RiskRejection(
            code=code,
            message=message,
            symbol=signal.symbol,
            strategy_name=signal.strategy_name,
        )
        logger.info(
            f"risk rejected {signal.symbol} ({signal.strategy_name}): "
            f"{code.value} — {message}"
        )
        return rej

    # ── Main entry point ─────────────────────────────────────────────────

    def evaluate(
        self,
        signal: Signal,
        account: AccountState,
        *,
        notional_cap: float | None = None,
        now: datetime | None = None,
    ) -> RiskDecision | RiskRejection:
        """
        Run every gate in order. Returns either a `RiskDecision` (the only
        legitimate input to broker.place_order) or a typed `RiskRejection`.

        Args:
            notional_cap: Optional upper bound on position notional (market
                          value = qty × price) supplied by the sleeve allocator.
                          When provided, sizing is additionally capped so the
                          new position cannot exceed the strategy's remaining
                          sleeve budget. Global risk caps remain authoritative.
        """
        now = now or datetime.now(timezone.utc)

        # 1. Signal validation — reject malformed inputs up front.
        if signal.reference_price <= 0:
            return self._reject(
                RejectionCode.INVALID_SIGNAL,
                f"reference_price must be > 0, got {signal.reference_price}",
                signal,
            )
        if signal.atr <= 0 or math.isnan(signal.atr):
            return self._reject(
                RejectionCode.INVALID_SIGNAL,
                f"atr must be > 0, got {signal.atr}",
                signal,
            )
        if account.equity <= 0:
            return self._reject(
                RejectionCode.INVALID_SIGNAL,
                f"account equity must be > 0, got {account.equity}",
                signal,
            )
        # MVP: long-only. Shorts will be enabled when a strategy needs them.
        if signal.side is not Side.BUY:
            return self._reject(
                RejectionCode.UNSUPPORTED_SIDE,
                f"only long entries are supported in MVP, got {signal.side.value}",
                signal,
            )
        if signal.order_type is OrderType.LIMIT and (
            signal.limit_price is None or signal.limit_price <= 0
        ):
            return self._reject(
                RejectionCode.INVALID_SIGNAL,
                f"LIMIT signal requires positive limit_price, got {signal.limit_price!r}",
                signal,
            )
        if signal.order_type is OrderType.STOP_LIMIT:
            if signal.entry_trigger_price is None or signal.entry_trigger_price <= 0:
                return self._reject(
                    RejectionCode.INVALID_SIGNAL,
                    f"STOP_LIMIT signal requires positive entry_trigger_price, "
                    f"got {signal.entry_trigger_price!r}",
                    signal,
                )
            if signal.limit_price is None or signal.limit_price <= 0:
                return self._reject(
                    RejectionCode.INVALID_SIGNAL,
                    f"STOP_LIMIT signal requires positive limit_price, "
                    f"got {signal.limit_price!r}",
                    signal,
                )
            if signal.side is Side.BUY and signal.limit_price < signal.entry_trigger_price:
                return self._reject(
                    RejectionCode.INVALID_SIGNAL,
                    f"BUY STOP_LIMIT limit_price {signal.limit_price} must be "
                    f">= entry_trigger_price {signal.entry_trigger_price}",
                    signal,
                )

        # 2. Kill switches (cheapest, most decisive).
        # The engine runs evaluate_account() before per-signal evaluation each
        # cycle so account-loss halts can clear or refresh on broker-baseline
        # rollover before this sticky halted branch blocks new entries.
        if self._halted:
            return self._reject(
                RejectionCode.HALTED,
                f"trading halted: {self._halt_reason}",
                signal,
            )

        breach = self._account_limit_breach(account)
        if breach is not None:
            code, message, baseline = breach
            self._engage_kill_switch(
                message,
                code=code,
                account_baseline=baseline,
            )
            return self._reject(
                code,
                message,
                signal,
            )

        # 3. Per-strategy cooldown.
        until = self._strategy_in_cooldown(signal.strategy_name, now)
        if until is not None:
            return self._reject(
                RejectionCode.STRATEGY_COOLDOWN,
                f"strategy '{signal.strategy_name}' in cooldown until "
                f"{until.isoformat()}",
                signal,
            )

        # 4. Duplicate-position guard. MVP has no pyramiding.
        if signal.symbol in account.open_positions:
            return self._reject(
                RejectionCode.DUPLICATE_POSITION,
                f"already hold {account.open_positions[signal.symbol].qty} "
                f"shares of {signal.symbol}",
                signal,
            )

        # 5. Max-open-positions cap.
        if len(account.open_positions) >= self.max_open_positions:
            return self._reject(
                RejectionCode.MAX_POSITIONS_REACHED,
                f"{len(account.open_positions)} positions open "
                f"(cap {self.max_open_positions})",
                signal,
            )

        # 6. Stop & sizing.
        stop_price = self._stop_price_for(signal)
        if stop_price <= 0:
            return self._reject(
                RejectionCode.INVALID_STOP,
                f"computed stop price {stop_price} is non-positive",
                signal,
            )
        if signal.side is Side.BUY and stop_price >= signal.reference_price:
            return self._reject(
                RejectionCode.INVALID_STOP,
                f"long stop {stop_price} not below entry {signal.reference_price}",
                signal,
            )
        # STOP_LIMIT-specific: the protective stop must sit strictly below
        # the arming trigger so the OTO leg never enters a crossed state at
        # submit time. Reject as INVALID_STOP rather than letting RiskDecision
        # raise — keeps the engine's rejection-handling path uniform.
        if (
            signal.order_type is OrderType.STOP_LIMIT
            and signal.side is Side.BUY
            and signal.entry_trigger_price is not None
            and stop_price >= signal.entry_trigger_price
        ):
            return self._reject(
                RejectionCode.INVALID_STOP,
                f"long stop {stop_price} not below STOP_LIMIT trigger "
                f"{signal.entry_trigger_price}",
                signal,
            )

        qty = self._size_position(signal, stop_price, account, notional_cap=notional_cap)

        # Live-trading size multiplier (10.G1): scale down on first live exposure.
        # PLAN 11.47 R2 P1: only apply when sizing produced a positive qty.
        # max(1, ...) and max(0.01, ...) below were reviving a zero-share
        # sizing rejection back into a 1-share / 0.01-share order, which on
        # STOP_LIMIT violates the never-round-up-beyond-budget invariant.
        # Letting qty stay 0 falls into the POSITION_TOO_SMALL branch below.
        if (
            qty > 0
            and settings.LIVE_TRADING
            and settings.LIVE_SIZE_MULTIPLIER != 1.0
        ):
            _is_fractional = (
                settings.FRACTIONAL_ENABLED
                and signal.order_type is OrderType.MARKET
            )
            if _is_fractional:
                qty = max(
                    0.01,
                    math.floor(qty * settings.LIVE_SIZE_MULTIPLIER * 100) / 100,
                )
            else:
                qty = max(1, math.floor(qty * settings.LIVE_SIZE_MULTIPLIER))

        if qty <= 0:
            # Distinguish gross-exposure exhaustion from cash exhaustion for
            # better operator diagnostics.
            max_gross = account.equity * self.max_gross_exposure_pct
            if account.gross_exposure() >= max_gross:
                return self._reject(
                    RejectionCode.GROSS_EXPOSURE_CAP,
                    f"gross exposure ${account.gross_exposure():.2f} >= "
                    f"cap ${max_gross:.2f}",
                    signal,
                )
            if (
                signal.side is Side.BUY
                and account.cash < signal.reference_price
            ):
                return self._reject(
                    RejectionCode.INSUFFICIENT_CASH,
                    f"cash ${account.cash:.2f} insufficient for 1 share at "
                    f"${signal.reference_price:.2f}",
                    signal,
                )
            return self._reject(
                RejectionCode.POSITION_TOO_SMALL,
                f"sized position rounds to 0 shares "
                f"(equity=${account.equity:.2f}, stop_distance="
                f"${abs(signal.reference_price - stop_price):.4f})",
                signal,
            )

        decision = RiskDecision(
            symbol=signal.symbol,
            side=signal.side,
            qty=qty,
            entry_reference_price=signal.reference_price,
            stop_price=stop_price,
            strategy_name=signal.strategy_name,
            reason=signal.reason or f"{signal.strategy_name} entry",
            order_type=signal.order_type,
            limit_price=signal.limit_price,
            entry_max_price=signal.entry_max_price,
            entry_trigger_price=signal.entry_trigger_price,
        )
        logger.info(
            f"risk approved {decision.symbol}: {decision.qty} shares @ "
            f"${decision.entry_reference_price:.2f}, stop ${decision.stop_price:.2f} "
            f"({decision.strategy_name})"
        )
        return decision
