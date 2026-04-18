"""Risk management layer (Phase 6).

Re-exports the public surface so the rest of the codebase can do
`from risk import RiskManager, RiskDecision, ...` without reaching into
submodules.
"""

from risk.manager import (
    AccountState,
    Position,
    RejectionCode,
    RiskDecision,
    RiskManager,
    RiskRejection,
    Signal,
)

__all__ = [
    "AccountState",
    "Position",
    "RejectionCode",
    "RiskDecision",
    "RiskManager",
    "RiskRejection",
    "Signal",
]
