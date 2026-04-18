"""Trading engine (Phase 8).

`TradingEngine` orchestrates the layers built in Phases 2–7:
  fetch → freshness → indicators → signals → risk → execute → log.

The broker is the source of truth on every cycle, the loop survives any
single-cycle failure, and any open orders are tidied on graceful shutdown.
"""

from engine.trader import EngineConfig, TradingEngine

__all__ = ["EngineConfig", "TradingEngine"]
