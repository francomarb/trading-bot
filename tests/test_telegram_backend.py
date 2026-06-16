"""Unit tests for TelegramAlertBackend and TelegramCommandListener."""

from __future__ import annotations

import json
import threading
import time
from unittest.mock import MagicMock, patch, call

import pytest

from reporting.alerts import (
    Alert,
    AlertDispatcher,
    AlertSeverity,
    AlertType,
    TelegramAlertBackend,
    TelegramCommandListener,
    _SEVERITY_EMOJI,
)


# ── TelegramAlertBackend ─────────────────────────────────────────────────────


class TestTelegramAlertBackend:
    def _make_backend(self, token="tok123", chat_id="42") -> TelegramAlertBackend:
        return TelegramAlertBackend(token=token, chat_id=chat_id)

    def test_send_posts_to_telegram_api(self):
        backend = self._make_backend()
        alert = Alert(
            alert_type=AlertType.CIRCUIT_BREAKER,
            severity=AlertSeverity.CRITICAL,
            message="daily loss exceeded",
        )
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("reporting.alerts.requests.post", return_value=mock_resp) as mock_post:
            backend.send(alert)

        mock_post.assert_called_once()
        url, kwargs = mock_post.call_args[0][0], mock_post.call_args[1]
        assert "sendMessage" in url
        assert "tok123" in url
        payload = kwargs["json"]
        assert payload["chat_id"] == "42"
        assert "🚨" in payload["text"]
        assert "circuit_breaker" in payload["text"]

    def test_critical_severity_uses_alarm_emoji(self):
        backend = self._make_backend()
        alert = Alert(
            alert_type=AlertType.ENGINE_HALT,
            severity=AlertSeverity.CRITICAL,
            message="halted",
        )
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("reporting.alerts.requests.post", return_value=mock_resp) as mock_post:
            backend.send(alert)

        text = mock_post.call_args[1]["json"]["text"]
        assert text.startswith("🚨")

    def test_warning_severity_uses_warning_emoji(self):
        backend = self._make_backend()
        alert = Alert(
            alert_type=AlertType.STALE_DATA,
            severity=AlertSeverity.WARNING,
            message="stale",
        )
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("reporting.alerts.requests.post", return_value=mock_resp) as mock_resp_ctx:
            backend.send(alert)

        text = mock_resp_ctx.call_args[1]["json"]["text"]
        assert text.startswith("⚠️")

    def test_info_severity_uses_info_emoji(self):
        backend = self._make_backend()
        alert = Alert(
            alert_type=AlertType.TRADE_EXECUTED,
            severity=AlertSeverity.INFO,
            message="BUY 10 AAPL @ $150.00",
        )
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("reporting.alerts.requests.post", return_value=mock_resp) as mock_post:
            backend.send(alert)

        text = mock_post.call_args[1]["json"]["text"]
        assert text.startswith("ℹ️")

    def test_send_does_not_raise_on_request_exception(self):
        backend = self._make_backend()
        alert = Alert(
            alert_type=AlertType.BROKER_ERROR,
            severity=AlertSeverity.WARNING,
            message="timeout",
        )
        with patch(
            "reporting.alerts.requests.post", side_effect=ConnectionError("timeout")
        ):
            backend.send(alert)  # must not raise

    def test_send_does_not_raise_on_non_ok_response(self):
        backend = self._make_backend()
        alert = Alert(
            alert_type=AlertType.BROKER_ERROR,
            severity=AlertSeverity.WARNING,
            message="error",
        )
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 400
        mock_resp.text = "Bad Request"

        with patch("reporting.alerts.requests.post", return_value=mock_resp):
            backend.send(alert)  # must not raise

    def test_send_text_posts_raw_message(self):
        backend = self._make_backend(chat_id="99")
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("reporting.alerts.requests.post", return_value=mock_resp) as mock_post:
            backend.send_text("hello world")

        payload = mock_post.call_args[1]["json"]
        assert payload["chat_id"] == "99"
        assert payload["text"] == "hello world"

    def test_dispatcher_dedup_suppression_with_telegram_backend(self):
        backend = self._make_backend()
        mock_resp = MagicMock()
        mock_resp.ok = True

        with patch("reporting.alerts.requests.post", return_value=mock_resp) as mock_post:
            dispatcher = AlertDispatcher(backends=[backend], cooldown_seconds=300)
            first = dispatcher.broker_error("test error")
            second = dispatcher.broker_error("test error")

        assert first is True
        assert second is False
        assert mock_post.call_count == 1


# ── TelegramCommandListener ─────────────────────────────────────────────────


class TestTelegramCommandListener:
    def _make_listener(self, chat_id="42") -> tuple[TelegramCommandListener, TelegramAlertBackend]:
        backend = TelegramAlertBackend(token="tok", chat_id=chat_id)
        listener = TelegramCommandListener(backend)
        return listener, backend

    def _build_update(self, text: str, chat_id: str, update_id: int = 1) -> dict:
        return {
            "update_id": update_id,
            "message": {
                "text": text,
                "chat": {"id": int(chat_id)},
            },
        }

    def test_status_command_replies_with_state(self, tmp_path):
        state = {
            "running": True,
            "live_trading": False,
            "regime": "TRENDING",
            "equity": 10250.0,
            "daily_pnl": 250.0,
            "cycle_count": 10,
            "timestamp": "2026-04-28T14:00:00+00:00",
            "open_positions": {"MU": "sma_crossover"},
            "sleeve_usage": {},
        }
        state_file = tmp_path / "engine_state.json"
        state_file.write_text(json.dumps(state))

        listener, backend = self._make_listener(chat_id="42")

        with (
            patch.object(
                backend, "send_text"
            ) as mock_send,
            patch("reporting.alerts.settings") as mock_settings,
        ):
            mock_settings.STATE_SNAPSHOT_PATH = str(state_file)
            listener._handle_status()

        mock_send.assert_called_once()
        reply = mock_send.call_args[0][0]
        assert "TRENDING" in reply
        assert "10,250.00" in reply
        assert "MU" in reply

    def test_status_command_replies_offline_if_state_missing(self, tmp_path):
        listener, backend = self._make_listener()

        with (
            patch.object(backend, "send_text") as mock_send,
            patch("reporting.alerts.settings") as mock_settings,
        ):
            mock_settings.STATE_SNAPSHOT_PATH = str(tmp_path / "nonexistent.json")
            listener._handle_status()

        mock_send.assert_called_once()
        assert "offline" in mock_send.call_args[0][0].lower() or "not found" in mock_send.call_args[0][0].lower()

    def test_halt_command_queues_via_operator_store(self):
        """Phase B migration: Telegram /halt writes to the operator
        command queue (single channel). The engine heartbeat picks it
        up on the next tick; we no longer call engine.stop() directly,
        which left no audit trail."""
        listener, backend = self._make_listener()
        engine = MagicMock()
        # Engine has the operator_command_store wired (Phase A PR-2).
        listener._engine = engine

        with patch.object(backend, "send_text"):
            listener._handle_halt()

        # Queue write happened with the expected action.
        engine.operator_command_store.insert.assert_called_once()
        kwargs = engine.operator_command_store.insert.call_args.kwargs
        assert kwargs["action"] == "halt"
        assert kwargs["reason"] == "telegram /halt"
        assert kwargs["requested_by"].startswith("telegram:")
        # engine.stop() is NOT called — sticky halt via kill switch
        # is the new mechanism.
        engine.stop.assert_not_called()

    def test_halt_command_falls_back_to_stop_when_queue_unavailable(self):
        """Pre-Phase-A builds (no operator_command_store) or queue
        write failures fall back to the legacy engine.stop() path so
        the operator still has an emergency stop."""
        listener, backend = self._make_listener()
        engine = MagicMock(spec=["stop"])  # no operator_command_store attribute
        listener._engine = engine

        with patch.object(backend, "send_text"):
            listener._handle_halt()

        engine.stop.assert_called_once()

    def test_halt_command_replies_with_confirmation(self):
        listener, backend = self._make_listener()
        # MagicMock auto-creates operator_command_store; the reply
        # in the Phase B path mentions "queued" / "kill switch".
        listener._engine = MagicMock()

        with patch.object(backend, "send_text") as mock_send:
            listener._handle_halt()

        assert mock_send.called
        msg = mock_send.call_args[0][0].lower()
        assert "halt" in msg or "kill switch" in msg

    def test_poll_ignores_unauthorized_chat_id(self):
        listener, backend = self._make_listener(chat_id="42")
        update = self._build_update("/halt", chat_id="999")  # wrong chat
        get_resp = MagicMock()
        get_resp.ok = True
        get_resp.json.return_value = {"result": [update]}

        with (
            patch("reporting.alerts.requests.get", return_value=get_resp),
            patch.object(listener, "_handle_halt") as mock_halt,
        ):
            listener._poll_once()

        mock_halt.assert_not_called()

    def test_poll_dispatches_halt_for_authorized_chat(self):
        listener, backend = self._make_listener(chat_id="42")
        update = self._build_update("/halt", chat_id="42")
        get_resp = MagicMock()
        get_resp.ok = True
        get_resp.json.return_value = {"result": [update]}

        with (
            patch("reporting.alerts.requests.get", return_value=get_resp),
            patch.object(listener, "_handle_halt") as mock_halt,
        ):
            listener._poll_once()

        mock_halt.assert_called_once()

    def test_poll_dispatches_status_for_authorized_chat(self):
        listener, backend = self._make_listener(chat_id="42")
        update = self._build_update("/status", chat_id="42")
        get_resp = MagicMock()
        get_resp.ok = True
        get_resp.json.return_value = {"result": [update]}

        with (
            patch("reporting.alerts.requests.get", return_value=get_resp),
            patch.object(listener, "_handle_status") as mock_status,
        ):
            listener._poll_once()

        mock_status.assert_called_once()

    def test_poll_does_not_raise_on_request_failure(self):
        listener, _ = self._make_listener()
        with patch(
            "reporting.alerts.requests.get", side_effect=ConnectionError("fail")
        ):
            listener._poll_once()  # must not raise

    def test_last_update_id_advances(self):
        listener, backend = self._make_listener(chat_id="42")
        updates = [
            self._build_update("/status", "42", update_id=10),
            self._build_update("/status", "42", update_id=20),
        ]
        get_resp = MagicMock()
        get_resp.ok = True
        get_resp.json.return_value = {"result": updates}

        with (
            patch("reporting.alerts.requests.get", return_value=get_resp),
            patch.object(listener, "_handle_status"),
        ):
            listener._poll_once()

        assert listener._last_update_id == 20


# ── New AlertType / convenience methods ─────────────────────────────────────


class TestNewAlertTypes:
    def _dispatcher(self) -> AlertDispatcher:
        return AlertDispatcher(backends=[], cooldown_seconds=0)

    def test_trade_executed_fires_info_alert(self):
        d = self._dispatcher()
        with patch.object(d, "fire", return_value=True) as mock_fire:
            d.trade_executed("AAPL", "sma_crossover", "buy", 10, 150.0, "entry signal")
        alert = mock_fire.call_args[0][0]
        assert alert.alert_type == AlertType.TRADE_EXECUTED
        assert alert.severity == AlertSeverity.INFO
        assert "AAPL" in alert.message

    def test_regime_shift_fires_info_alert(self):
        d = self._dispatcher()
        with patch.object(d, "fire", return_value=True) as mock_fire:
            d.regime_shift("BEAR", "TRENDING")
        alert = mock_fire.call_args[0][0]
        assert alert.alert_type == AlertType.REGIME_SHIFT
        assert "BEAR" in alert.message
        assert "TRENDING" in alert.message

    def test_eod_summary_fires_info_alert(self):
        d = self._dispatcher()
        with patch.object(d, "fire", return_value=True) as mock_fire:
            d.eod_summary(daily_pnl=500.0, trade_count=5, win_rate=0.6)
        alert = mock_fire.call_args[0][0]
        assert alert.alert_type == AlertType.EOD_SUMMARY
        assert "500" in alert.message
        assert "5" in alert.message

    def test_broker_info_fires_info_alert(self):
        d = self._dispatcher()
        with patch.object(d, "fire", return_value=True) as mock_fire:
            d.broker_info("stream healthy again")
        alert = mock_fire.call_args[0][0]
        assert alert.alert_type == AlertType.BROKER_INFO
        assert alert.severity == AlertSeverity.INFO
        assert "stream healthy again" in alert.message
