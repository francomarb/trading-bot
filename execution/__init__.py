"""Execution layer exports.

Keep package import side effects minimal so importing `execution.stream`
doesn't eagerly pull in Alpaca broker modules.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AlpacaBroker",
    "BrokerError",
    "BrokerSnapshot",
    "OpenOrder",
    "OrderResult",
    "OrderStatus",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from execution.broker import (
            AlpacaBroker,
            BrokerError,
            BrokerSnapshot,
            OpenOrder,
            OrderResult,
            OrderStatus,
        )

        exports = {
            "AlpacaBroker": AlpacaBroker,
            "BrokerError": BrokerError,
            "BrokerSnapshot": BrokerSnapshot,
            "OpenOrder": OpenOrder,
            "OrderResult": OrderResult,
            "OrderStatus": OrderStatus,
        }
        return exports[name]
    raise AttributeError(f"module 'execution' has no attribute {name}")
