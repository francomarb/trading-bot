"""
Background worker to handle options midpoint cancel/replace bracket orders.
"""

import threading
import time
import uuid
from loguru import logger
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import LimitOrderRequest, TakeProfitRequest, StopLossRequest
from alpaca.trading.enums import OrderSide, OrderType as AlpacaOrderType, OrderClass, TimeInForce

from risk.manager import RiskDecision, Side
from execution.stream import StreamManager

class OptionsExecutionWorker(threading.Thread):
    def __init__(self, decision: RiskDecision, api: TradingClient, stream_manager: StreamManager | None = None):
        super().__init__(daemon=True, name=f"OptionsExecutor-{decision.symbol}")
        self.decision = decision
        self.api = api
        self.stream_manager = stream_manager
        
    def run(self):
        logger.info(f"[{self.name}] Started background execution for {self.decision.symbol}")
        
        limit_price = self.decision.limit_price
        if limit_price is None:
            logger.error(f"[{self.name}] Options execution requires a limit price.")
            return

        client_order_id = f"opt-{self.decision.strategy_name}-{uuid.uuid4().hex[:8]}"
        
        stop_loss = StopLossRequest(stop_price=round(self.decision.stop_price, 2))
        take_profit = None
        if self.decision.take_profit_price:
            take_profit = TakeProfitRequest(limit_price=round(self.decision.take_profit_price, 2))
            
        order_class = OrderClass.BRACKET if take_profit else OrderClass.OTO
            
        req = LimitOrderRequest(
            symbol=self.decision.symbol,
            qty=self.decision.qty,
            side=OrderSide.BUY if self.decision.side is Side.BUY else OrderSide.SELL,
            type=AlpacaOrderType.LIMIT,
            time_in_force=TimeInForce.DAY,
            order_class=order_class,
            stop_loss=stop_loss,
            take_profit=take_profit,
            client_order_id=client_order_id,
            limit_price=round(limit_price, 2)
        )
        
        stream_event = None
        if self.stream_manager:
            stream_event = self.stream_manager.watch(client_order_id)
            
        try:
            order = self.api.submit_order(req)
            logger.info(f"[{self.name}] Submitted option bracket order {order.id}")
        except Exception as e:
            logger.error(f"[{self.name}] Failed to submit option bracket order: {e}")
            if self.stream_manager:
                self.stream_manager.unwatch(client_order_id)
            return
            
        # The 60-second cancel/replace loop
        # Since we approximate Delta/Prices via paper feed without OPRA,
        # we will wait 60s and cancel if unfilled. A full OPRA implementation
        # would fetch live quotes in a loop and use replace_order_by_id.
        if stream_event:
            filled = stream_event.wait(timeout=60.0)
            if filled:
                logger.info(f"[{self.name}] Option order filled.")
            else:
                logger.warning(f"[{self.name}] Option limit order unfilled after 60s. Cancelling.")
                try:
                    self.api.cancel_order_by_id(order.id)
                except Exception as e:
                    logger.error(f"[{self.name}] Cancel failed: {e}")
            self.stream_manager.unwatch(client_order_id)
        else:
            # Fallback REST polling
            for _ in range(12):
                time.sleep(5)
                try:
                    latest = self.api.get_order_by_client_id(client_order_id)
                    if latest.status in ("filled", "canceled", "rejected"):
                        logger.info(f"[{self.name}] Option order reached terminal state: {latest.status}")
                        return
                except Exception:
                    pass
            
            logger.warning(f"[{self.name}] Option order unfilled after 60s via REST. Cancelling.")
            try:
                self.api.cancel_order_by_id(order.id)
            except Exception as e:
                pass
