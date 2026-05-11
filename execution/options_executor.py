"""
Background worker to handle async single-leg options limit entries.
"""

import threading
import time
import uuid
import warnings
from typing import Callable
from loguru import logger
with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=r"websockets\.legacy is deprecated.*",
        category=DeprecationWarning,
    )
    from alpaca.trading.client import TradingClient
    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, OrderType as AlpacaOrderType, TimeInForce

from risk.manager import RiskDecision, Side
from execution.stream import StreamManager

# Callback signature: (status_str, filled_qty, avg_fill_price, order_id)
FillCallback = Callable[[str, float, "float | None", str], None]


class OptionsExecutionWorker(threading.Thread):
    def __init__(
        self,
        decision: RiskDecision,
        api: TradingClient,
        stream_manager: StreamManager | None = None,
        on_fill: FillCallback | None = None,
    ):
        super().__init__(daemon=True, name=f"OptionsExecutor-{decision.symbol}")
        self.decision = decision
        self.api = api
        self.stream_manager = stream_manager
        self._on_fill = on_fill
        
    def _report_fill(self, status: str, order_id: str, order=None) -> None:
        if self._on_fill is None:
            return
        filled_qty = 0.0
        avg_price = None
        if order is not None:
            filled_qty = float(order.filled_qty or 0)
            avg = order.filled_avg_price
            avg_price = float(avg) if avg is not None else None
        try:
            self._on_fill(status, filled_qty, avg_price, order_id)
        except Exception as e:
            logger.error(f"[{self.name}] on_fill callback raised: {e}")

    def run(self):
        logger.info(f"[{self.name}] Started background execution for {self.decision.symbol}")
        
        limit_price = self.decision.limit_price
        if limit_price is None:
            logger.error(f"[{self.name}] Options execution requires a limit price.")
            return

        client_order_id = f"opt-{self.decision.strategy_name}-{uuid.uuid4().hex[:8]}"
        
        req = LimitOrderRequest(
            symbol=self.decision.symbol,
            qty=self.decision.qty,
            side=OrderSide.BUY if self.decision.side is Side.BUY else OrderSide.SELL,
            type=AlpacaOrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
            limit_price=round(limit_price, 2)
        )
        
        stream_event = None
        if self.stream_manager:
            stream_event = self.stream_manager.watch(client_order_id)
            
        try:
            order = self.api.submit_order(req)
            logger.info(f"[{self.name}] Submitted option limit order {order.id}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to submit option limit order: {e}")
            if self.stream_manager:
                self.stream_manager.unwatch(client_order_id)
            self._report_fill("rejected", client_order_id)
            return
            
        if self.stream_manager:
            self.stream_manager.bind_submitted_order(
                client_order_id=client_order_id,
                order_id=str(order.id),
                stop_leg_ids=[],
            )

        # The 60-second entry watch loop.
        # Since we approximate Delta/Prices via paper feed without OPRA,
        # we wait 60s and cancel if the entry remains unresolved. A fuller
        # implementation could retry/reprice with fresh quote data.
        if stream_event:
            filled = stream_event.wait(timeout=60.0)
            self.stream_manager.unwatch(str(order.id))
            if filled:
                logger.info(f"[{self.name}] Option order filled.")
                try:
                    final = self.api.get_order_by_id(order.id)
                    self._report_fill("filled", str(order.id), final)
                except Exception:
                    self._report_fill("filled", str(order.id))
            else:
                try:
                    latest = self.api.get_order_by_id(order.id)
                    status = latest.status.value if hasattr(latest.status, "value") else str(latest.status)
                    if status in ("filled", "partially_filled", "canceled", "rejected"):
                        logger.info(
                            f"[{self.name}] Option order resolved during stream gap: {status}"
                        )
                        self._report_fill(status, str(latest.id), latest)
                        return
                except Exception:
                    latest = None

                logger.warning(f"[{self.name}] Option limit order unfilled after 60s. Cancelling.")
                try:
                    self.api.cancel_order_by_id(order.id)
                except Exception as e:
                    logger.error(f"[{self.name}] Cancel failed: {e}")
                self._report_fill("canceled", str(order.id), latest)
        else:
            # Fallback REST polling
            terminal_status = "canceled"
            for _ in range(12):
                time.sleep(5)
                try:
                    latest = self.api.get_order_by_client_id(client_order_id)
                    if latest.status in ("filled", "canceled", "rejected"):
                        logger.info(f"[{self.name}] Option order reached terminal state: {latest.status}")
                        terminal_status = latest.status
                        self._report_fill(terminal_status, str(latest.id), latest)
                        return
                except Exception:
                    pass

            logger.warning(f"[{self.name}] Option order unfilled after 60s via REST. Cancelling.")
            try:
                self.api.cancel_order_by_id(order.id)
            except Exception as e:
                pass
            self._report_fill("canceled", str(order.id))
