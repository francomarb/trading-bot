"""
Options Contract Resolver

Finds the closest OCC symbol matching the strategy's DTE and Delta criteria.
"""

from datetime import datetime, timedelta, timezone
from loguru import logger

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOptionContractsRequest
from alpaca.trading.enums import AssetStatus, ContractType

from config.settings import ALPACA_API_KEY, ALPACA_SECRET_KEY, ALPACA_PAPER

_client: TradingClient | None = None


def _get_client() -> TradingClient:
    global _client
    if _client is None:
        _client = TradingClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_SECRET_KEY,
            paper=ALPACA_PAPER,
        )
    return _client

def find_best_call(
    symbol: str, 
    underlying_price: float, 
    min_dte: int = 10, 
    max_dte: int = 21, 
    target_delta: float = 0.55
) -> str | None:
    """
    Finds the optimal Call option contract for the given underlying symbol.
    
    Since Alpaca free tier does not stream live Greeks, we approximate 
    a ~0.55 Delta by finding the closest Slightly In-The-Money (ITM) strike.
    
    Args:
        symbol: The underlying ticker (e.g., 'SPY').
        underlying_price: The current price of the underlying.
        min_dte: Minimum days to expiration.
        max_dte: Maximum days to expiration.
        target_delta: Approximate delta target (0.55 means slightly ITM for calls).
        
    Returns:
        The OCC symbol string (e.g., 'SPY260522C00520000') or None if not found.
    """
    client = _get_client()
    
    now = datetime.now(timezone.utc).date()
    min_date = now + timedelta(days=min_dte)
    max_date = now + timedelta(days=max_dte)
    
    # We want a slightly ITM strike for a Call to get ~0.55 Delta.
    # An ATM call is ~0.50 Delta. A strike 0.5% - 1% below current price
    # is roughly 0.55 Delta on SPY.
    target_strike = underlying_price * 0.995 
    
    req = GetOptionContractsRequest(
        underlying_symbols=[symbol],
        status=AssetStatus.ACTIVE,
        expiration_date_gte=min_date.isoformat(),
        expiration_date_lte=max_date.isoformat(),
        type=ContractType.CALL,
        limit=100  # fetch enough to find the closest strike
    )
    
    try:
        response = client.get_option_contracts(req)
        contracts = response.option_contracts
    except Exception as e:
        logger.error(f"Failed to fetch option contracts for {symbol}: {e}")
        return None
        
    if not contracts:
        logger.warning(f"No active call contracts found for {symbol} between {min_date} and {max_date}.")
        return None
        
    # Group by expiration date
    from collections import defaultdict
    by_expiry = defaultdict(list)
    for c in contracts:
        by_expiry[c.expiration_date].append(c)
        
    if not by_expiry:
        return None
        
    # Pick the expiration closest to min_dte
    expirations = sorted(list(by_expiry.keys()))
    best_expiry = expirations[0]
    
    # Sort contracts for this expiration by how close their strike is to target_strike
    available_contracts = by_expiry[best_expiry]
    best_contract = min(
        available_contracts, 
        key=lambda c: abs(float(c.strike_price) - target_strike)
    )
    
    logger.info(
        f"Resolved Option: {best_contract.symbol} "
        f"(Strike: ${float(best_contract.strike_price):.2f}, "
        f"Expiry: {best_contract.expiration_date}, "
        f"Underlying: ${underlying_price:.2f})"
    )
    
    return best_contract.symbol
