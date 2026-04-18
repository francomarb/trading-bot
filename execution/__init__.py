"""Execution layer (Phase 7).

The broker is the only thing in the system that talks to Alpaca for order
placement. Its `place_order` accepts only a `RiskDecision`, so unsafe orders
are impossible by construction.
"""

from execution.broker import (
    AlpacaBroker,
    BrokerError,
    BrokerSnapshot,
    OpenOrder,
    OrderResult,
    OrderStatus,
)

__all__ = [
    "AlpacaBroker",
    "BrokerError",
    "BrokerSnapshot",
    "OpenOrder",
    "OrderResult",
    "OrderStatus",
]
