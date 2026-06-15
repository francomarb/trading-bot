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
    OPTION_TRAILING_STATE_UNVERIFIED = "option_trailing_state_unverified"
    # MLEG walk-and-market close events. Both are FYI only — the bot
    # never blocks on these; the operator sees them post-fact.
    MLEG_CLOSE_WALK_STARTED = "mleg_close_walk_started"
    MLEG_CLOSE_MARKET_FALLBACK = "mleg_close_market_fallback"
    ENGINE_HALT = "engine_halt"
    # PR-65 review F4: soft operator actions (pause-entries /
    # resume-entries / pause-strategy / resume-strategy) get their own
    # alert type at INFO severity. Previously routed through engine_halt
    # which formatted them as "engine halted: ..." at CRITICAL — a
    # routine resume-entries looked like an emergency to alerting
    # channels. ENGINE_HALT stays for actual halt + resume-after-halt.
    OPERATOR_ACTION = "operator_action"
    TRADE_EXECUTED = "trade_executed"
    REGIME_SHIFT = "regime_shift"
    EOD_SUMMARY = "eod_summary"
    # PLAN 11.10e — Strategy Health & Edge Monitor (advisory only).
    # STRATEGY_EDGE_LOSS is the silent-killer alarm (CRITICAL); the
    # others are forensic/informational and never drive auto-action
    # per the v1 invariant (design §1.2).
    STRATEGY_EDGE_LOSS = "strategy_edge_loss"
    STRATEGY_EDGE_BELOW_BENCHMARK = "strategy_edge_below_benchmark"
    STRATEGY_HEALTH_DEGRADED = "strategy_health_degraded"
    STRATEGY_HEALTH_BROKEN = "strategy_health_broken"
    STRATEGY_DRIFT_WARNING = "strategy_drift_warning"


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
        """Telegram /halt — Phase B migration.

        Was: called `engine.stop()` directly, which shut the bot down
        rather than engaging the kill switch. That left no audit trail
        and bypassed the standard halt/resume mechanism. Phase B writes
        a `halt` row to the operator queue so the engine heartbeat
        engages the kill switch (sticky), captures the audit trail, and
        the operator can resume via the queue without restarting.

        Falls back to the legacy `engine.stop()` path if the operator
        queue store is not wired (older builds, unit tests with bare
        backends).
        """
        store = getattr(self._engine, "operator_command_store", None) if self._engine else None
        if store is not None:
            try:
                from engine.operator_queue import new_command_uid
                uid = new_command_uid()
                store.insert(
                    command_uid=uid,
                    action="halt",
                    reason="telegram /halt",
                    requested_by=f"telegram:{self._backend._chat_id}",
                )
                self._backend.send_text(
                    f"🛑 halt queued ({uid[:18]}…) — engine will engage "
                    "kill switch on its next heartbeat tick."
                )
                logger.warning(
                    f"TelegramCommandListener: queued halt {uid[:18]}…"
                )
                return
            except Exception as exc:
                logger.warning(
                    f"TelegramCommandListener: queue write failed, "
                    f"falling back to engine.stop(): {exc}"
                )
        # Legacy fallback — pre-Phase-A builds or queue write failure.
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

    def mleg_close_walk_started(
        self,
        *,
        strategy_name: str,
        underlying: str,
        position_id: str,
        reason: str,
        mode: str,
        initial_mid: float,
    ) -> bool:
        """FYI alert — walk-and-market close kicking off.

        Mode is "walk-and-market" or "market-only" (latter is EOS bypass
        or BEAR override). initial_mid is the net spread mid at decision
        time, in $/share. Fires once per close attempt; never blocks.
        """
        mid_str = (
            f"{initial_mid:.2f}"
            if initial_mid == initial_mid  # NaN check
            else "n/a"
        )
        return self.fire(Alert(
            alert_type=AlertType.MLEG_CLOSE_WALK_STARTED,
            severity=AlertSeverity.INFO,
            message=(
                f"[{strategy_name}] {underlying} {position_id[:8]}: "
                f"close started — reason={reason}, mode={mode}, mid=${mid_str}"
            ),
        ))

    def mleg_close_market_fallback(
        self,
        *,
        strategy_name: str,
        underlying: str,
        position_id: str,
        reason: str,
        terminal_status: str,
    ) -> bool:
        """FYI alert — walk exhausted, market fallback fired.

        terminal_status is the final outcome of the market step:
        "filled" or "rejected". Fires at most once per close attempt.
        """
        return self.fire(Alert(
            alert_type=AlertType.MLEG_CLOSE_MARKET_FALLBACK,
            severity=AlertSeverity.WARNING,
            message=(
                f"[{strategy_name}] {underlying} {position_id[:8]}: "
                f"walk exhausted → market close {terminal_status} "
                f"(reason={reason})"
            ),
        ))

    def option_trailing_state_unverified(
        self, symbol: str, strategy: str, hwm_premium: float
    ) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.OPTION_TRAILING_STATE_UNVERIFIED,
            severity=AlertSeverity.WARNING,
            message=(
                "open option had no durable high-water mark; "
                "initialized conservatively from available broker/entry evidence"
            ),
            symbol=symbol,
            strategy=strategy,
            details={"hwm_premium": hwm_premium},
        ))

    def engine_halt(self, reason: str) -> bool:
        return self.fire(Alert(
            alert_type=AlertType.ENGINE_HALT,
            severity=AlertSeverity.CRITICAL,
            message=f"engine halted: {reason}",
        ))

    def operator_action(self, message: str) -> bool:
        """PR-65 review F4: routine operator soft-control transitions
        (pause-entries / resume-entries / pause-strategy /
        resume-strategy). Routed as INFO not CRITICAL so a routine
        resume doesn't trip emergency-style alerting channels. Use
        `engine_halt` only for halt / resume-after-halt where CRITICAL
        is correct."""
        return self.fire(Alert(
            alert_type=AlertType.OPERATOR_ACTION,
            severity=AlertSeverity.INFO,
            message=message,
        ))

    def trade_executed(
        self,
        symbol: str,
        strategy: str,
        side: str,
        qty: float,
        price: float,
        reason: str,
        *,
        position_uid: str | None = None,
    ) -> bool:
        # Phase B §17.2 — include position_uid so the operator can
        # paste it into `operator.py show-position`. Optional kwarg
        # keeps existing callers source-compatible; the uid is appended
        # to the message body only when present.
        message = f"{side.upper()} {qty} {symbol} @ ${price:.2f} — {reason}"
        details: dict = {"side": side, "qty": qty, "price": price, "reason": reason}
        if position_uid:
            message = f"{message} [{position_uid[:18]}…]"
            details["position_uid"] = position_uid
        return self.fire(Alert(
            alert_type=AlertType.TRADE_EXECUTED,
            severity=AlertSeverity.INFO,
            message=message,
            symbol=symbol,
            strategy=strategy,
            details=details,
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

    # ── Strategy Health & Edge Monitor (PLAN 11.10e) ────────────────────
    # All five are advisory-only per the v1 invariant — they inform the
    # operator but the bot never takes action. Only STRATEGY_EDGE_LOSS
    # is CRITICAL (the silent-killer alarm); the others are forensic
    # context to surface alongside it. Health alerts are explicitly
    # INFO-level when Edge is positive (no alarm fatigue on a profitable
    # strategy with messy execution) and only escalate to WARNING when
    # actually BROKEN.

    def strategy_edge_loss(
        self,
        strategy: str,
        *,
        r_expectancy: float | None,
        trade_count: int,
        negative_persistence_weeks: int,
    ) -> bool:
        """The silent-killer alarm — Edge verdict NEGATIVE + CONCLUSIVE
        + persistence reached. Per design §13 this MUST surface
        prominently; operator decides on quarantine."""
        r_str = (
            f"{r_expectancy:+.3f}R" if r_expectancy is not None else "n/a"
        )
        return self.fire(Alert(
            alert_type=AlertType.STRATEGY_EDGE_LOSS,
            severity=AlertSeverity.CRITICAL,
            message=(
                f"SILENT KILLER: strategy is losing money on clean "
                f"execution — recommend pause and investigate "
                f"(expectancy {r_str}, {trade_count} trades, "
                f"{negative_persistence_weeks} weeks of negative signals)"
            ),
            strategy=strategy,
            details={
                "r_expectancy": r_expectancy,
                "trade_count": trade_count,
                "negative_persistence_weeks": negative_persistence_weeks,
            },
        ))

    def strategy_edge_below_benchmark(
        self,
        strategy: str,
        *,
        strategy_return: float | None,
        benchmark_return: float | None,
        alpha: float | None,
    ) -> bool:
        """Strategy is profitable but underperforms its passive benchmark.
        Recommend reduce_size — strategy is destroying value vs the
        watchlist it's expressing a view on."""
        return self.fire(Alert(
            alert_type=AlertType.STRATEGY_EDGE_BELOW_BENCHMARK,
            severity=AlertSeverity.WARNING,
            message=(
                f"below benchmark: strategy {strategy_return:+.1%} vs "
                f"benchmark {benchmark_return:+.1%} "
                f"(alpha {alpha:+.1%}) — recommend reduce size"
            ),
            strategy=strategy,
            details={
                "strategy_return": strategy_return,
                "benchmark_return": benchmark_return,
                "alpha": alpha,
            },
        ))

    def strategy_health_degraded(
        self,
        strategy: str,
        *,
        layer: str,
        edge_verdict: str,
        findings: list[str],
    ) -> bool:
        """L1/L2/L3 DEGRADED. Severity is INFO when Edge is positive
        (no alarm fatigue on profitable strategies) and WARNING
        otherwise. Forensic — never auto-action."""
        severity = (
            AlertSeverity.INFO
            if edge_verdict == "POSITIVE"
            else AlertSeverity.WARNING
        )
        finding_summary = "; ".join(findings[:3]) if findings else "(no detail)"
        return self.fire(Alert(
            alert_type=AlertType.STRATEGY_HEALTH_DEGRADED,
            severity=severity,
            message=f"health {layer} DEGRADED — {finding_summary}",
            strategy=strategy,
            details={"layer": layer, "edge_verdict": edge_verdict},
        ))

    def strategy_health_broken(
        self,
        strategy: str,
        *,
        layer: str,
        edge_verdict: str,
        findings: list[str],
    ) -> bool:
        """L1/L2 BROKEN (L3 cannot be BROKEN per design §3.6).

        **Always WARNING regardless of Edge verdict** — BROKEN is the
        non-cosmetic operational tier; routing it through INFO when
        the strategy happens to be profitable would hide a real
        operational failure (stream disconnect, reconciliation
        mismatch, etc.) behind a quiet log line.

        PR #20 reviewer caught the original INFO-when-Edge-positive
        ladder as a footgun. DEGRADED keeps the
        INFO-when-positive/WARNING-otherwise ladder via
        `strategy_health_degraded` (those are softer signals where
        alarm fatigue on profitable strategies is the bigger risk);
        BROKEN does not.

        Operator should investigate; the bot still does not
        auto-disable on Health alone (v1 invariant — design §1.2).
        """
        finding_summary = "; ".join(findings[:3]) if findings else "(no detail)"
        return self.fire(Alert(
            alert_type=AlertType.STRATEGY_HEALTH_BROKEN,
            severity=AlertSeverity.WARNING,
            message=f"health {layer} BROKEN — {finding_summary}",
            strategy=strategy,
            details={"layer": layer, "edge_verdict": edge_verdict},
        ))

    def strategy_drift_warning(
        self,
        strategy: str,
        *,
        check_name: str,
        observed: float,
        envelope_band: tuple[float, float],
    ) -> bool:
        """L3 drift detected (live behavior diverged from envelope band).
        Leading indicator of future edge erosion — never auto-action."""
        return self.fire(Alert(
            alert_type=AlertType.STRATEGY_DRIFT_WARNING,
            severity=AlertSeverity.INFO,
            message=(
                f"drift in {check_name}: observed {observed:.1%} "
                f"vs envelope band "
                f"[{envelope_band[0]:.1%}, {envelope_band[1]:.1%}]"
            ),
            strategy=strategy,
            details={
                "check": check_name,
                "observed": observed,
                "envelope_lo": envelope_band[0],
                "envelope_hi": envelope_band[1],
            },
        ))
