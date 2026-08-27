"""Typed position-sizing and broker-protection lifecycle policy."""

from __future__ import annotations

from enum import Enum
from dataclasses import dataclass


class SizingModel(str, Enum):
    """Authoritative basis used to calculate entry quantity."""

    STOP_DISTANCE = "stop_distance"
    NOTIONAL = "notional"
    DEFINED_MAX_LOSS = "defined_max_loss"


class ProtectionModel(str, Enum):
    """Whether a position requires broker-resting stop protection."""

    BROKER_STOP = "broker_stop"
    SIGNAL_EXIT_ONLY = "signal_exit_only"


class StrategyPauseCause(str, Enum):
    """Independent durable latches that can block strategy entries."""

    OPERATOR = "operator"
    UNEXPECTED_PROTECTION = "unexpected_protection"
    UNKNOWN_PERSISTED = "unknown_persisted"


@dataclass(frozen=True)
class PositionRiskProfile:
    """Immutable strategy request consumed and bounded by RiskManager."""

    sizing_model: SizingModel = SizingModel.STOP_DISTANCE
    protection_model: ProtectionModel = ProtectionModel.BROKER_STOP
    target_notional_pct: float | None = None
    stated_leverage_multiplier: float = 1.0
    stress_exposure_multiplier: float = 1.0
