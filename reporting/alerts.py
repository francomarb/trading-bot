"""
Operator alert dispatcher (Phase 9).

Fires alerts on operator-critical events:

  - Order rejection (risk gate blocked a trade)
  - Circuit-breaker trip (daily loss, hard $ cap, broker-error streak)
  - Loss-streak cooldown triggered
  - Stale data feed (StaleDataError on a symbol)
  - Unusual slippage (realized >> modeled)
  - Position mismatch (local vs. broker state)

MVP backend is a dedicated log file (`logs/alerts.log`). The dispatcher
is pluggable — Slack / email / PagerDuty backends can be added later by
subclassing `AlertBackend`.

Design principles:
  - Alerts are fire-and-forget from the engine's perspective — an alert
    failure must never block or crash the trading loop.
  - Every alert carries a severity (INFO / WARNING / CRITICAL) and a
    structured payload.
  - Duplicate suppression: the same alert (same type + symbol) is not
    re-fired within a configurable cooldown window (default 5 min).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum

from loguru import logger

from config import settings


# ── Types ───────────────────────────────────────────────────────────────────


class AlertSeverity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class AlertType(Enum):
    ORDER_REJECTION = "order_rejection"
    CIRCUIT_BREAKER = "circuit_breaker"
    LOSS_STREAK_COOLDOWN = "loss_streak_cooldown"
    STALE_DATA = "stale_data"
    SLIPPAGE_DRIFT = "slippage_drift"
    POSITION_MISMATCH = "position_mismatch"
    BROKER_ERROR = "broker_error"
    ENGINE_HALT = "engine_halt"


@dataclass(frozen=True)
class Alert:
    """One operator alert."""

    alert_type: AlertType
    severity: AlertSeverity
    message: str
    symbol: str = ""
    strategy: str = ""
    details: dict = field(default_factory=dict)
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def format(self) -> str:
        """Human-readable single-line format."""
        parts = [
            f"[{self.severity.value}]",
            f"[{self.alert_type.value}]",
        ]
        if self.symbol:
            parts.append(f"[{self.symbol}]")
        if self.strategy:
            parts.append(f"[{self.strategy}]")
        parts.append(self.message)
        if self.details:
            detail_str = ", ".join(f"{k}={v}" for k, v in self.details.items())
            parts.append(f"({detail_str})")
        return " ".join(parts)


# ── Backends ────────────────────────────────────────────────────────────────


class AlertBackend(ABC):
    """Abstract alert backend. Subclass for Slack, email, etc."""

    @abstractmethod
    def send(self, alert: Alert) -> None:
        """Deliver the alert. Must not raise."""


class LogFileBackend(AlertBackend):
    """Write alerts to a dedicated log file via loguru."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or settings.ALERT_LOG_FILE
        self._sink_id: int | None = None

    def _ensure_sink(self) -> None:
        if self._sink_id is not None:
            return
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        self._sink_id = logger.add(
            self._path,
            format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
            rotation="5 MB",
            retention="90 days",
            level="INFO",
            filter=lambda record: record["extra"].get("alert") is True,
        )

    def send(self, alert: Alert) -> None:
        self._ensure_sink()
        log_fn = {
            AlertSeverity.INFO: logger.bind(alert=True).info,
            AlertSeverity.WARNING: logger.bind(alert=True).warning,
            AlertSeverity.CRITICAL: logger.bind(alert=True).critical,
        }.get(alert.severity, logger.bind(alert=True).warning)
        log_fn(alert.format())


# ── Dispatcher ──────────────────────────────────────────────────────────────


class AlertDispatcher:
    """
    Central alert dispatcher. Sends alerts through all registered backends
    with duplicate suppression.
    """

    def __init__(
        self,
        *,
        backends: list[AlertBackend] | None = None,
        cooldown_seconds: float = 300,
    ) -> None:
        self._backends = backends or [LogFileBackend()]
        self._cooldown = timedelta(seconds=cooldown_seconds)
        # Dedup key → last fire time
        self._last_fired: dict[str, datetime] = {}

    def _dedup_key(self, alert: Alert) -> str:
        return f"{alert.alert_type.value}:{alert.symbol}:{alert.strategy}"

    def _is_suppressed(self, alert: Alert) -> bool:
        key = self._dedup_key(alert)
        last = self._last_fired.get(key)
        if last is None:
            return False
        now = datetime.now(timezone.utc)
        return (now - last) < self._cooldown

    def fire(self, alert: Alert) -> bool:
        """
        Dispatch an alert to all backends. Returns True if sent, False if
        suppressed by cooldown. Never raises — backend errors are logged.
        """
        if self._is_suppressed(alert):
            return False

        self._last_fired[self._dedup_key(alert)] = datetime.now(timezone.utc)

        for backend in self._backends:
            try:
                backend.send(alert)
            except Exception as e:
                # Alert failure must never crash the bot.
                logger.error(f"alert backend {type(backend).__name__} failed: {e}")

        # Also log to the main log stream.
        logger.warning(f"ALERT: {alert.format()}")
        return True

    # ── Convenience factory methods ─────────────────────────────────────

    def order_rejection(
        self, symbol: str, strategy: str, reason: str, code: str
    ) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.ORDER_REJECTION,
            severity=AlertSeverity.WARNING,
            message=f"order rejected: {reason}",
            symbol=symbol,
            strategy=strategy,
            details={"code": code},
        ))

    def circuit_breaker(self, reason: str) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.CIRCUIT_BREAKER,
            severity=AlertSeverity.CRITICAL,
            message=f"circuit breaker tripped: {reason}",
        ))

    def loss_streak_cooldown(
        self, strategy: str, streak: int, cooldown_hours: float
    ) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.LOSS_STREAK_COOLDOWN,
            severity=AlertSeverity.WARNING,
            message=f"strategy disabled: {streak} consecutive losses",
            strategy=strategy,
            details={"streak": streak, "cooldown_hours": cooldown_hours},
        ))

    def stale_data(self, symbol: str, age_desc: str) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.STALE_DATA,
            severity=AlertSeverity.WARNING,
            message=f"stale data: {age_desc}",
            symbol=symbol,
        ))

    def slippage_drift(
        self, mean_realized: float, mean_modeled: float
    ) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.SLIPPAGE_DRIFT,
            severity=AlertSeverity.CRITICAL,
            message=(
                f"slippage drift: realized {mean_realized:.1f}bps "
                f"vs modeled {mean_modeled:.1f}bps"
            ),
            details={
                "realized_bps": mean_realized,
                "modeled_bps": mean_modeled,
            },
        ))

    def broker_error(self, error: str) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.BROKER_ERROR,
            severity=AlertSeverity.WARNING,
            message=f"broker error: {error}",
        ))

    def engine_halt(self, reason: str) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.ENGINE_HALT,
            severity=AlertSeverity.CRITICAL,
            message=f"engine halted: {reason}",
        ))
