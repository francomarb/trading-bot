"""
SPY Options RSI Reversion Strategy.
"""

from typing import Tuple
import pandas as pd
import numpy as np

from strategies.base import BaseStrategy, SignalFrame, OrderType
from utils.options_lookup import find_best_call, _get_client

class SPYOptionsReversionStrategy(BaseStrategy):
    name = "spy_options_reversion"
    preferred_order_type = OrderType.LIMIT
    
    def __init__(self, rsi_length: int = 14, rsi_threshold: float = 30):
        super().__init__()
        self.rsi_length = rsi_length
        self.rsi_threshold = rsi_threshold
        
    @property
    def required_bars(self) -> int:
        return self.rsi_length + 5
        
    def _raw_signals(self, df: pd.DataFrame) -> SignalFrame:
        if len(df) < self.required_bars:
            return SignalFrame(
                entries=pd.Series(False, index=df.index),
                exits=pd.Series(False, index=df.index)
            )
            
        close = df["close"]
        
        # Calculate RSI
        delta = close.diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=self.rsi_length).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=self.rsi_length).mean()
        
        # Prevent division by zero
        loss = loss.replace(0, np.nan)
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        # Fill NaN rsi (where loss was 0) with 100
        rsi = rsi.fillna(100)
        
        # Entry: RSI drops below threshold, then crosses above.
        prev_rsi = rsi.shift(1)
        entries = (prev_rsi < self.rsi_threshold) & (rsi > self.rsi_threshold)
        
        # Time-based exit: Wednesday 3:30 PM EST
        try:
            est_time = df.index.tz_convert("US/Eastern")
        except TypeError:
            est_time = df.index.tz_localize("UTC").tz_convert("US/Eastern")
            
        is_wednesday = est_time.weekday == 2
        is_after_330 = (est_time.hour > 15) | ((est_time.hour == 15) & (est_time.minute >= 30))
        
        exits_array = is_wednesday & is_after_330
        exits = pd.Series(exits_array, index=df.index)
        
        return SignalFrame(entries=entries, exits=exits)

    def inspect_open_positions(self, position, latest_close: float) -> bool:
        """
        Calculates a real-time approximate Delta using blackscholes.
        Returns True (triggering exit) if Delta < 0.30.
        """
        import re
        from datetime import datetime, timezone
        from loguru import logger
        
        # position.symbol should be the OCC string (e.g., SPY260515C00510000)
        match = re.match(r"^([A-Z]+)(\d{6})([CP])(\d{8})$", position.symbol)
        if not match:
            return False
            
        contract_type = match.group(3)
        if contract_type != "C":
            return False # We only trade calls for now
            
        # Parse Strike (last 8 digits, e.g. 00510000 -> 510.00)
        strike_str = match.group(4)
        strike = float(strike_str) / 1000.0
        
        # Parse Expiry (YYMMDD)
        expiry_str = match.group(2)
        expiry_date = datetime.strptime(expiry_str, "%y%m%d").replace(tzinfo=timezone.utc)
        
        # Calculate Time (T) in years
        now = datetime.now(timezone.utc)
        time_to_expiry_days = (expiry_date - now).total_seconds() / 86400.0
        T = max(time_to_expiry_days / 365.0, 0.001) # Avoid <=0 DTE errors
        
        # Volatility: Fetch VIX as global vol proxy
        sigma = 0.15 # Fallback
        try:
            import yfinance as yf
            vix_ticker = yf.Ticker("^VIX")
            vix_history = vix_ticker.history(period="1d")
            if not vix_history.empty:
                vix = float(vix_history["Close"].iloc[-1])
                sigma = vix / 100.0
        except Exception as e:
            logger.debug(f"[{self.name}] Failed to fetch VIX, using fallback sigma 0.15: {e}")
            
        r = 0.05 # 5% Risk-free rate
        
        try:
            from blackscholes import BlackScholesCall
            call = BlackScholesCall(S=latest_close, K=strike, T=T, r=r, sigma=sigma)
            delta = call.delta()
            
            logger.debug(f"[{self.name}] {position.symbol} Delta: {delta:.2f} (S={latest_close}, K={strike}, T={T:.3f}, VIX={sigma:.2f})")
            
            if delta < 0.30:
                logger.warning(f"[{self.name}] Delta Floor Breached! Delta={delta:.2f} < 0.30 for {position.symbol}")
                return True
        except Exception as e:
            logger.error(f"[{self.name}] Failed to calculate Delta: {e}")
            
        return False

    def build_option_execution(self, symbol: str, underlying_price: float) -> Tuple[str, float, float, float]:
        """
        Dynamically fetch the OCC symbol and approximate its current premium.
        Returns: (occ_symbol, limit_price, take_profit, stop_loss)
        """
        occ_symbol = find_best_call(symbol, underlying_price, min_dte=10, max_dte=21, target_delta=0.55)
        if not occ_symbol:
            raise ValueError(f"Could not find a valid option contract for {symbol}")
            
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionSnapshotRequest
            from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY
            
            data_client = OptionHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
            req = OptionSnapshotRequest(symbol_or_symbols=occ_symbol)
            snapshot = data_client.get_option_snapshots(req)
            
            if occ_symbol in snapshot and snapshot[occ_symbol].latest_quote:
                quote = snapshot[occ_symbol].latest_quote
                bid = float(quote.bid_price)
                ask = float(quote.ask_price)
                if bid > 0:
                    spread_pct = (ask - bid) / bid
                    if spread_pct > 0.05:
                        raise ValueError(f"Spread is too wide: {spread_pct:.2%} (>5%). Rejecting trade.")
                premium = (bid + ask) / 2.0
                if premium <= 0 and snapshot[occ_symbol].latest_trade:
                    premium = float(snapshot[occ_symbol].latest_trade.price)
            elif occ_symbol in snapshot and snapshot[occ_symbol].latest_trade:
                premium = float(snapshot[occ_symbol].latest_trade.price)
            else:
                premium = underlying_price * 0.01
        except ValueError as ve:
            raise ve
        except Exception:
            # Fallback for paper testing without OPRA access
            premium = underlying_price * 0.01

        if premium <= 0:
            premium = 1.0
            
        take_profit = premium * 1.20
        stop_loss = premium * 0.70
        
        return occ_symbol, premium, take_profit, stop_loss
