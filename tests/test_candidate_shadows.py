"""Offline, strategy-correct shadow replay for PLAN 11.61."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd
import pytest

import reporting.candidate_shadows as candidate_shadows
from reporting.candidate_shadows import (
    RSIReplayContract,
    historical_contract_from_settings_source,
    replay_contract_for_candidate,
    resolve_rsi_shadow,
)


OBSERVED = datetime(2026, 9, 9, 13, 43, 21, tzinfo=timezone.utc)


def _contract(
    *, time_in_force: str = "day", stop_anchor: str = "reference"
) -> RSIReplayContract:
    return RSIReplayContract.from_mapping(
        {
            "contract_version": 1,
            "strategy": "rsi_reversion",
            "timeframe": "1Day",
            "period": 3,
            "oversold": 15.0,
            "overbought": 70.0,
            "entry_mode": "level_below",
            "exit_sma_window": None,
            "quick_exit_rsi": 55.0,
            "entry_order_type": "limit",
            "entry_time_in_force": time_in_force,
            "stop_anchor": stop_anchor,
            "atr_stop_multiplier": 2.0,
            "exit_order_type": "market",
            "modeled_exit_slippage_bps": 5.0,
        }
    )


def _candidate(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "strategy": "rsi_reversion",
        "disposition": "sleeve_full",
        "observed_at": OBSERVED.isoformat(),
        "signal_at": "2026-09-08T04:00:00+00:00",
        "reference_price": 100.0,
        "atr": 5.0,
        "bot_git_commit": "a" * 40,
        "peer_entry_time_in_force": "gtc",
        "strategy_features_json": json.dumps(
            {
                "oversold": 15.0,
                "quick_exit_rsi": 55.0,
                "exit_sma_window": None,
                "entry_mode": "level_below",
            }
        ),
        "common_context_json": "{}",
    }
    value.update(overrides)
    return value


def _minutes(*rows: tuple[str, float, float, float, float]) -> pd.DataFrame:
    return pd.DataFrame(
        [row[1:] for row in rows],
        columns=["open", "high", "low", "close"],
        index=pd.DatetimeIndex([row[0] for row in rows], tz="UTC"),
    )


def _daily() -> pd.DataFrame:
    closes = [110, 109, 108, 107, 106, 105, 100, 95, 100, 105, 110]
    index = pd.date_range(
        "2026-09-01", periods=len(closes), freq="D", tz="America/New_York"
    ).tz_convert("UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": [value + 1 for value in closes],
            "low": [value - 1 for value in closes],
            "close": closes,
        },
        index=index,
    )


class TestReplayContract:
    def test_stored_contract_is_preferred(self, tmp_path) -> None:
        contract = _contract()
        candidate = _candidate(
            common_context_json=json.dumps(
                {"shadow_replay_contract": contract.__dict__}
            )
        )

        resolved, source = replay_contract_for_candidate(
            candidate, repo_root=tmp_path
        )

        assert resolved == contract
        assert source == "stored_candidate_contract"

    def test_historical_settings_are_parsed_without_execution(self) -> None:
        source = """
SLIPPAGE_MODEL_MARKET_BPS = 7.0
RSI_REVERSION_PARAMS: dict = {
    "period": 4, "oversold": 12.0, "overbought": 75.0,
    "entry_mode": "level_below", "exit_sma_window": 6,
    "quick_exit_rsi": 58.0,
}
ATR_STOP_MULTIPLIER = 2.5
"""

        contract = historical_contract_from_settings_source(
            source, entry_time_in_force="gtc"
        )

        assert contract.period == 4
        assert contract.entry_time_in_force == "gtc"
        assert contract.stop_anchor == "reference"
        assert contract.atr_stop_multiplier == 2.5
        assert contract.modeled_exit_slippage_bps == 7.0

    def test_warmup_is_derived_from_frozen_indicator_settings(self) -> None:
        short = _contract()
        long = RSIReplayContract.from_mapping(
            {**short.__dict__, "exit_sma_window": 100}
        )

        assert short.warmup_calendar_days == 30
        assert long.warmup_calendar_days == 214

    def test_legacy_candidate_uses_immutable_commit_settings(
        self, tmp_path, monkeypatch
    ) -> None:
        source = """
SLIPPAGE_MODEL_MARKET_BPS = 5.0
RSI_REVERSION_PARAMS = {
    "period": 3, "oversold": 15.0, "overbought": 70.0,
    "entry_mode": "level_below", "exit_sma_window": None,
    "quick_exit_rsi": 55.0,
}
ATR_STOP_MULTIPLIER = 2.0
"""
        calls: list[list[str]] = []

        def fake_run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(stdout=source)

        monkeypatch.setattr(candidate_shadows.subprocess, "run", fake_run)

        contract, source_label = replay_contract_for_candidate(
            _candidate(), repo_root=tmp_path
        )

        assert contract.period == 3
        assert source_label == "historical_commit:aaaaaaaaaa"
        assert calls == [
            ["git", "show", f"{'a' * 40}:config/settings.py"]
        ]


class TestRSIShadowReplay:
    def test_day_limit_fill_then_rsi_exit_at_next_session_open(self) -> None:
        minutes = _minutes(
            ("2026-09-09T13:44:00", 99.0, 100.0, 98.0, 99.5),
            ("2026-09-09T13:45:00", 99.5, 101.0, 99.0, 100.5),
        )

        result = resolve_rsi_shadow(
            _candidate(),
            _contract(),
            daily_bars=_daily(),
            entry_minutes=minutes,
            as_of=datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
            entry_window_complete=True,
            contract_source="test",
        )

        assert result.status == "resolved"
        assert result.entry_price == 99.0
        assert result.exit_price == pytest.approx(110.0 * 0.9995)
        assert result.metadata["exit_reason"] == "rsi_signal"
        assert result.return_pct == pytest.approx(result.exit_price / 99.0 - 1.0)
        assert result.r_multiple == pytest.approx(
            (result.exit_price - 99.0) / 9.0
        )
        assert result.store_values()["pnl_dollars"] is None

    def test_complete_day_without_limit_touch_is_not_filled(self) -> None:
        result = resolve_rsi_shadow(
            _candidate(),
            _contract(),
            daily_bars=_daily(),
            entry_minutes=_minutes(
                ("2026-09-09T13:44:00", 101.0, 102.0, 100.5, 101.5)
            ),
            as_of=datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
            entry_window_complete=True,
            contract_source="test",
        )

        assert result.status == "not_filled"
        assert result.entry_price is None
        assert result.return_pct is None

    def test_entry_session_stop_uses_only_bars_through_stop(self) -> None:
        result = resolve_rsi_shadow(
            _candidate(),
            _contract(),
            daily_bars=_daily(),
            entry_minutes=_minutes(
                ("2026-09-09T13:44:00", 99.0, 100.0, 98.0, 99.5),
                ("2026-09-09T13:45:00", 95.0, 96.0, 89.0, 90.0),
                ("2026-09-09T13:46:00", 110.0, 120.0, 109.0, 119.0),
            ),
            as_of=datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
            entry_window_complete=True,
            contract_source="test",
        )

        assert result.status == "resolved"
        assert result.metadata["exit_reason"] == "protective_stop_entry_session"
        assert result.exit_price == pytest.approx(90.0 * 0.9995)
        assert result.max_favorable_pct == pytest.approx(100.0 / 99.0 - 1.0)

    def test_fill_anchored_contract_keeps_exact_atr_risk_offset(self) -> None:
        result = resolve_rsi_shadow(
            _candidate(),
            _contract(stop_anchor="fill"),
            daily_bars=_daily(),
            entry_minutes=_minutes(
                ("2026-09-09T13:44:00", 99.0, 100.0, 98.0, 99.5),
                ("2026-09-09T13:45:00", 89.0, 90.0, 88.0, 89.0),
            ),
            as_of=datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
            entry_window_complete=True,
            contract_source="test",
        )

        assert result.status == "resolved"
        assert result.entry_price == 99.0
        assert result.metadata["stop_price"] == 89.0
        assert result.entry_price - result.metadata["stop_price"] == 10.0

    def test_same_minute_entry_and_stop_is_not_guessed(self) -> None:
        result = resolve_rsi_shadow(
            _candidate(),
            _contract(),
            daily_bars=_daily(),
            entry_minutes=_minutes(
                ("2026-09-09T13:44:00", 99.0, 101.0, 89.0, 90.0)
            ),
            as_of=datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
            entry_window_complete=True,
            contract_source="test",
        )

        assert result.status == "needs_review"
        assert result.exit_price is None
        assert "same minute" in result.metadata["unresolved_reason"]

    def test_incomplete_entry_window_remains_pending(self) -> None:
        result = resolve_rsi_shadow(
            _candidate(),
            _contract(),
            daily_bars=_daily(),
            entry_minutes=_minutes(
                ("2026-09-09T13:44:00", 101.0, 102.0, 100.5, 101.5)
            ),
            as_of=datetime(2026, 9, 9, 14, tzinfo=timezone.utc),
            entry_window_complete=False,
            contract_source="test",
        )

        assert result.status == "pending_data"

    def test_gtc_limit_can_fill_on_a_later_session(self) -> None:
        daily = _daily()
        fill_session = daily.index.tz_convert("America/New_York").date == datetime(
            2026, 9, 10
        ).date()
        daily.loc[fill_session, "low"] = 99.0

        result = resolve_rsi_shadow(
            _candidate(),
            _contract(time_in_force="gtc"),
            daily_bars=daily,
            entry_minutes=_minutes(
                ("2026-09-09T13:44:00", 102.0, 103.0, 101.0, 102.0)
            ),
            as_of=datetime(2026, 9, 10, 21, tzinfo=timezone.utc),
            entry_window_complete=True,
            contract_source="test",
        )

        assert result.status == "open"
        assert result.entry_price == 100.0
        assert result.metadata["fill_resolution"] == "daily"
        assert result.metadata["excursion_precision"] == "fill_session_excluded"

    def test_unfilled_gtc_limit_waits_until_broker_expiry(self) -> None:
        arguments = {
            "candidate": _candidate(),
            "contract": _contract(time_in_force="gtc"),
            "daily_bars": _daily(),
            "entry_minutes": _minutes(
                ("2026-09-09T13:44:00", 102.0, 103.0, 101.0, 102.0)
            ),
            "entry_window_complete": True,
            "contract_source": "test",
        }

        waiting = resolve_rsi_shadow(
            **arguments,
            as_of=datetime(2026, 9, 12, 12, tzinfo=timezone.utc),
        )
        expired = resolve_rsi_shadow(
            **arguments,
            as_of=datetime(2026, 12, 9, 14, tzinfo=timezone.utc),
        )

        assert waiting.status == "awaiting_fill"
        assert expired.status == "not_filled"
