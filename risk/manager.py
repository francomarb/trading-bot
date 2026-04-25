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
   *dollar* loss on a stop-out is bounded to `MAX_POSITION_PCT * equity`
   regardless of the symbol's volatility.

   Sizing is also capped by per-position notional exposure. A tight stop must
   not let one trade consume the whole gross-exposure sleeve.

3. **Multiple independent kill switches.** Daily-loss %, hard-dollar cap,
   broker-error streak, and slippage drift each halt trading independently.
   Once tripped, the manager stays halted until `reset_kill_switches()` is
   explicitly called by an operator.

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
    qty: int
    avg_entry_price: float
    market_value: float  # current market value (qty * last price)


@dataclass(frozen=True)
class AccountState:
    """
    Snapshot of broker state passed into `RiskManager.evaluate`.

    `session_start_equity` is the equity at the start of the current trading
    session — used by the daily-loss and hard-dollar kill switches. The engine
    is responsible for capturing this once at session open.
    """

    equity: float
    cash: float
    session_start_equity: float
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
    # Required only when order_type is LIMIT.
    limit_price: float | None = None


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
    qty: int
    entry_reference_price: float
    stop_price: float
    strategy_name: str
    reason: str
    # Strategy-chosen entry type. Phase 7 broker reads this and routes
    # accordingly. Defaults to MARKET so legacy construction still works.
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None

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
        # rolling slippage samples: (modeled_bps, realized_bps)
        self._slippage_samples: Deque[tuple[float, float]] = deque(maxlen=200)
        self._halted: bool = False
        self._halt_reason: str | None = None

    # ── Kill-switch state ────────────────────────────────────────────────

    def is_halted(self) -> bool:
        return self._halted

    def halt_reason(self) -> str | None:
        return self._halt_reason

    def _engage_kill_switch(self, reason: str) -> None:
        if not self._halted:
            self._halted = True
            self._halt_reason = reason
            logger.critical(f"RiskManager kill switch engaged: {reason}")

    def reset_kill_switches(self) -> None:
        """Operator-only: clear all halts. Use after manual review."""
        if self._halted:
            logger.warning(
                f"RiskManager kill switch reset (was: {self._halt_reason})"
            )
        self._halted = False
        self._halt_reason = None

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
        self, modeled_bps: float, realized_bps: float
    ) -> None:
        """
        Append a fill-slippage sample. Once we have at least
        `slippage_min_samples`, logs drift metrics and — if
        `slippage_drift_enabled` — engages the kill switch when mean realized
        exceeds `slippage_drift_multiplier × mean modeled`.

        Both inputs must be non-negative bps (absolute slippage cost).

        The kill switch is disabled by default (SLIPPAGE_DRIFT_ENABLED=False)
        during paper trading. Enable only after calibrating the modeled baseline
        against real fills. Must be enabled before going live (Phase 10).
        """
        if modeled_bps < 0 or realized_bps < 0:
            raise ValueError("slippage bps must be non-negative")
        self._slippage_samples.append((modeled_bps, realized_bps))
        n = len(self._slippage_samples)
        if n < self.slippage_min_samples:
            return
        modeled_mean = sum(m for m, _ in self._slippage_samples) / n
        realized_mean = sum(r for _, r in self._slippage_samples) / n

        logger.debug(
            f"slippage monitor: realized={realized_mean:.2f}bps "
            f"modeled={modeled_mean:.2f}bps n={n} "
            f"threshold={self.slippage_drift_multiplier}× "
            f"enabled={self.slippage_drift_enabled}"
        )

        # Guard: if modeled baseline is zero we have nothing to compare against.
        # Skip the ratio check rather than using an epsilon that would fire on
        # any positive realized slippage.
        if modeled_mean == 0.0:
            logger.warning(
                f"slippage monitor: modeled mean is 0 — skipping drift check "
                f"(realized={realized_mean:.2f}bps, n={n}). "
                "Set SLIPPAGE_MODEL_MARKET_BPS to a non-zero value to enable."
            )
            return

        if not self.slippage_drift_enabled:
            return

        if realized_mean > self.slippage_drift_multiplier * modeled_mean:
            self._engage_kill_switch(
                f"slippage drift: realized mean {realized_mean:.2f}bps > "
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
        offset = self.atr_stop_multiplier * signal.atr
        if signal.side is Side.BUY:
            return signal.reference_price - offset
        return signal.reference_price + offset

    def _size_position(
        self, signal: Signal, stop_price: float, account: AccountState
    ) -> int:
        """
        Fixed-fractional sizing on stop distance:
            risk_dollars = equity * max_position_pct
            qty          = floor(risk_dollars / |entry - stop|)
        Then capped by per-position notional exposure, remaining
        gross-exposure budget, and available cash.
        """
        risk_dollars = account.equity * self.max_position_pct
        stop_distance = abs(signal.reference_price - stop_price)
        if stop_distance <= 0:
            return 0
        raw_qty = math.floor(risk_dollars / stop_distance)
        if raw_qty <= 0:
            return 0

        # Cap by per-position notional budget so tight stops do not consume
        # the whole SMA sleeve.
        if signal.reference_price > 0:
            max_position_notional = account.equity * self.max_position_notional_pct
            notional_qty_cap = math.floor(
                max_position_notional / signal.reference_price
            )
            raw_qty = min(raw_qty, notional_qty_cap)

        # Cap by remaining gross-exposure budget.
        max_gross = account.equity * self.max_gross_exposure_pct
        remaining_gross = max(0.0, max_gross - account.gross_exposure())
        if signal.reference_price > 0:
            gross_qty_cap = math.floor(remaining_gross / signal.reference_price)
            raw_qty = min(raw_qty, gross_qty_cap)

        # Cap by cash on hand (a buy must be payable).
        if signal.side is Side.BUY and signal.reference_price > 0:
            cash_qty_cap = math.floor(max(0.0, account.cash) / signal.reference_price)
            raw_qty = min(raw_qty, cash_qty_cap)

        return max(raw_qty, 0)

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
        now: datetime | None = None,
    ) -> RiskDecision | RiskRejection:
        """
        Run every gate in order. Returns either a `RiskDecision` (the only
        legitimate input to broker.place_order) or a typed `RiskRejection`.
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

        # 2. Kill switches (cheapest, most decisive).
        if self._halted:
            return self._reject(
                RejectionCode.HALTED,
                f"trading halted: {self._halt_reason}",
                signal,
            )

        # Daily-loss circuit breaker (% drawdown from session start).
        sess_start = account.session_start_equity
        if sess_start > 0:
            drawdown_pct = (sess_start - account.equity) / sess_start
            if drawdown_pct >= self.max_daily_loss_pct:
                self._engage_kill_switch(
                    f"daily loss limit hit: down "
                    f"{drawdown_pct * 100:.2f}% from session start"
                )
                return self._reject(
                    RejectionCode.DAILY_LOSS_LIMIT,
                    f"daily loss {drawdown_pct * 100:.2f}% >= "
                    f"{self.max_daily_loss_pct * 100:.2f}%",
                    signal,
                )

        # Hard dollar cap (absolute $ loss from session start).
        dollar_loss = sess_start - account.equity
        if dollar_loss >= self.hard_dollar_loss_cap:
            self._engage_kill_switch(
                f"hard dollar loss cap hit: down ${dollar_loss:.2f} "
                f"(cap ${self.hard_dollar_loss_cap:.2f})"
            )
            return self._reject(
                RejectionCode.HARD_DOLLAR_CAP,
                f"dollar loss ${dollar_loss:.2f} >= cap ${self.hard_dollar_loss_cap:.2f}",
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

        qty = self._size_position(signal, stop_price, account)

        # Live-trading size multiplier (10.G1): scale down on first live exposure.
        if settings.LIVE_TRADING and settings.LIVE_SIZE_MULTIPLIER != 1.0:
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
        )
        logger.info(
            f"risk approved {decision.symbol}: {decision.qty} shares @ "
            f"${decision.entry_reference_price:.2f}, stop ${decision.stop_price:.2f} "
            f"({decision.strategy_name})"
        )
        return decision
