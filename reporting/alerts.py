"""
Operator alert dispatcher (Phase 9).

Fires alerts on operator-critical events:

  - Order rejection (risk gate blocked a trade)
  - Circuit-breaker trip (daily loss, hard $ cap, broker-error streak)
  - Loss-streak cooldown triggered
  - Stale data feed (StaleDataError on a symbol)
  - Unusual slippage (realized >> modeled)
  - Position mismatch (local vs. broker state)
  - Trade executed (entry or exit fill)
  - Regime shift (market regime changed)
  - End-of-day summary

MVP backend is a dedicated log file (`logs/alerts.log`). The dispatcher
is pluggable — Slack / email / PagerDuty / Telegram backends can be added
by subclassing `AlertBackend`.

Design principles:
  - Alerts are fire-and-forget from the engine's perspective — an alert
    failure must never block or crash the trading loop.
  - Every alert carries a severity (INFO / WARNING / CRITICAL) and a
    structured payload.
  - Duplicate suppression: the same alert (same type + symbol) is not
    re-fired within a configurable cooldown window (default 5 min).
"""

from __future__ import annotations

import json
import os
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import TYPE_CHECKING

import requests
from loguru import logger

from config import settings

if TYPE_CHECKING:
    pass


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
    BROKER_INFO = "broker_info"
    ENGINE_HALT = "engine_halt"
    TRADE_EXECUTED = "trade_executed"
    REGIME_SHIFT = "regime_shift"
    EOD_SUMMARY = "eod_summary"


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
    """Abstract alert backend. Subclass for Slack, email, Telegram, etc."""

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


_SEVERITY_EMOJI = {
    AlertSeverity.INFO: "ℹ️",
    AlertSeverity.WARNING: "⚠️",
    AlertSeverity.CRITICAL: "🚨",
}


class TelegramAlertBackend(AlertBackend):
    """
    Send alerts to a Telegram chat via the Bot API.

    Uses synchronous requests.post — no external bot library required.
    Requires TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in config/.env.
    """

    _API_BASE = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, token: str, chat_id: str, timeout: float = 5.0) -> None:
        self._token = token
        self._chat_id = chat_id
        self._timeout = timeout

    def _url(self, method: str) -> str:
        return self._API_BASE.format(token=self._token, method=method)

    def send(self, alert: Alert) -> None:
        emoji = _SEVERITY_EMOJI.get(alert.severity, "")
        text = f"{emoji} {alert.format()}"
        try:
            resp = requests.post(
                self._url("sendMessage"),
                json={"chat_id": self._chat_id, "text": text},
                timeout=self._timeout,
            )
            if not resp.ok:
                logger.warning(
                    f"TelegramAlertBackend: sendMessage failed "
                    f"(status={resp.status_code}): {resp.text[:200]}"
                )
        except Exception as exc:
            logger.warning(f"TelegramAlertBackend: send error: {exc}")

    def send_text(self, text: str) -> None:
        """Send a raw text message (used by TelegramCommandListener for replies)."""
        try:
            resp = requests.post(
                self._url("sendMessage"),
                json={"chat_id": self._chat_id, "text": text},
                timeout=self._timeout,
            )
            if not resp.ok:
                logger.warning(
                    f"TelegramAlertBackend: sendMessage failed "
                    f"(status={resp.status_code}): {resp.text[:200]}"
                )
        except Exception as exc:
            logger.warning(f"TelegramAlertBackend: send_text error: {exc}")


# ── Interactive command listener ─────────────────────────────────────────────


class TelegramCommandListener:
    """
    Polls Telegram for incoming commands and dispatches them to the engine.

    Runs as a daemon thread. Only accepts messages from the authorized chat_id
    (all other senders are silently ignored).

    Supported commands:
      /status  — replies with current equity, regime, positions, cycle count
      /halt    — calls engine.stop() and replies with confirmation

    Reads engine state from data/engine_state.json written by the engine
    each cycle, avoiding direct coupling to the engine object (except for halt).
    """

    _POLL_INTERVAL = 3.0  # seconds between getUpdates calls

    def __init__(self, backend: TelegramAlertBackend) -> None:
        self._backend = backend
        self._engine: object | None = None
        self._last_update_id: int = 0
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self, engine: object) -> None:
        """Start the polling thread. Call after engine is constructed."""
        self._engine = engine
        self._thread = threading.Thread(
            target=self._poll_loop, name="TelegramCmdListener", daemon=True
        )
        self._thread.start()
        logger.info("TelegramCommandListener started")

    def stop(self) -> None:
        self._stop_event.set()

    def _poll_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._poll_once()
            except Exception as exc:
                logger.warning(f"TelegramCommandListener: poll error: {exc}")
            time.sleep(self._POLL_INTERVAL)

    def _poll_once(self) -> None:
        url = self._backend._url("getUpdates")
        params: dict = {"timeout": 0, "allowed_updates": ["message"]}
        if self._last_update_id:
            params["offset"] = self._last_update_id + 1

        try:
            resp = requests.get(url, params=params, timeout=10.0)
        except Exception as exc:
            logger.warning(f"TelegramCommandListener: getUpdates failed: {exc}")
            return
        if not resp.ok:
            return

        data = resp.json()
        for update in data.get("result", []):
            self._last_update_id = max(self._last_update_id, update["update_id"])
            msg = update.get("message", {})
            chat_id = str(msg.get("chat", {}).get("id", ""))
            text = (msg.get("text") or "").strip()

            # Security gate — only authorized chat
            if chat_id != self._backend._chat_id:
                continue

            if text.startswith("/status"):
                self._handle_status()
            elif text.startswith("/halt"):
                self._handle_halt()

    def _handle_status(self) -> None:
        state_path = getattr(settings, "STATE_SNAPSHOT_PATH", "data/engine_state.json")
        try:
            with open(state_path) as f:
                state = json.load(f)
        except Exception:
            self._backend.send_text("⚠️ Engine state file not found — bot may be offline.")
            return

        positions = state.get("open_positions") or {}
        pos_lines = "\n".join(f"  {sym}: {strat}" for sym, strat in positions.items())
        sleeve = state.get("sleeve_usage") or {}
        sleeve_lines = "\n".join(f"  {k}: ${v:,.0f}" for k, v in sleeve.items())

        lines = [
            f"🤖 *Bot Status*",
            f"Running: {state.get('running', '?')}",
            f"Live trading: {state.get('live_trading', False)}",
            f"Regime: {state.get('regime', 'unknown')}",
            f"Equity: ${state.get('equity', 0):,.2f}",
            f"Daily P&L: ${state.get('daily_pnl', 0):+,.2f}",
            f"Cycle count: {state.get('cycle_count', 0)}",
            f"Last update: {state.get('timestamp', '?')}",
            f"Open positions:\n{pos_lines or '  (none)'}",
            f"Sleeve usage:\n{sleeve_lines or '  (none)'}",
        ]
        self._backend.send_text("\n".join(lines))

    def _handle_halt(self) -> None:
        if self._engine is not None and hasattr(self._engine, "stop"):
            self._engine.stop()  # type: ignore[union-attr]
            self._backend.send_text("🛑 Engine halt requested — bot will stop after the current cycle.")
        else:
            self._backend.send_text("⚠️ Engine reference not available.")


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

    def broker_info(self, message: str) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.BROKER_INFO,
            severity=AlertSeverity.INFO,
            message=f"broker info: {message}",
        ))

    def engine_halt(self, reason: str) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.ENGINE_HALT,
            severity=AlertSeverity.CRITICAL,
            message=f"engine halted: {reason}",
        ))

    def trade_executed(
        self,
        symbol: str,
        strategy: str,
        side: str,
        qty: float,
        price: float,
        reason: str,
    ) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.TRADE_EXECUTED,
            severity=AlertSeverity.INFO,
            message=f"{side.upper()} {qty} {symbol} @ ${price:.2f} — {reason}",
            symbol=symbol,
            strategy=strategy,
            details={"side": side, "qty": qty, "price": price, "reason": reason},
        ))

    def regime_shift(self, old_regime: str, new_regime: str) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.REGIME_SHIFT,
            severity=AlertSeverity.INFO,
            message=f"regime changed: {old_regime} → {new_regime}",
            details={"old": old_regime, "new": new_regime},
        ))

    def eod_summary(
        self, daily_pnl: float, trade_count: int, win_rate: float
    ) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.EOD_SUMMARY,
            severity=AlertSeverity.INFO,
            message=(
                f"EOD summary: P&L=${daily_pnl:+,.2f}, "
                f"trades={trade_count}, win_rate={win_rate:.0%}"
            ),
            details={
                "daily_pnl": daily_pnl,
                "trade_count": trade_count,
                "win_rate": win_rate,
            },
        ))
