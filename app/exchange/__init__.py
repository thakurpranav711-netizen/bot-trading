# Exchange Module
# app/exchange/__init__.py

"""
Exchange Module — Unified Trading Interface

Provides a consistent interface for trading across multiple exchanges:

Supported Exchanges:
────────────────────
• Alpaca    - US stocks & crypto (paper + live trading)
• Binance   - Global crypto (spot trading)
• Paper     - Simulation mode (no API keys needed)

Usage:
──────
    # Recommended: Use the factory (auto-detects exchange)
    from app.exchange import create_exchange
    
    exchange = create_exchange(
        mode="PAPER",          # or "LIVE"
        exchange_type="AUTO",  # or "ALPACA", "BINANCE", "PAPER"
        state_manager=state,
    )
    
    # All exchanges implement the same interface:
    price = exchange.get_price("BTC/USDT")
    candles = exchange.get_recent_candles("BTC/USDT", limit=150)
    fill = exchange.buy("BTC/USDT", quantity=0.001)
    fill = exchange.sell("BTC/USDT", quantity=0.001)
    balance = exchange.get_balance()
    positions = exchange.get_open_positions()

Direct Import (if you know which exchange you want):
────────────────────────────────────────────────────
    from app.exchange.alpaca import AlpacaExchange
    from app.exchange.binance import BinanceExchange
    from app.exchange.paper import PaperExchange

Exchange Interface:
───────────────────
All exchanges implement ExchangeClient (app/exchange/client.py):

    Market Data:
        get_price(symbol) -> float
        get_recent_candles(symbol, limit) -> List[Dict]
    
    Order Execution:
        buy(symbol, quantity) -> Dict (fill receipt)
        sell(symbol, quantity) -> Dict (fill receipt)
    
    Account:
        get_balance() -> float
        get_open_positions() -> Dict
        get_account_summary() -> Dict
    
    Lifecycle:
        begin_cycle() -> None  (reset price cache)
        end_cycle() -> None
        ping() -> bool
        close() -> None

Fill Receipt Format:
────────────────────
    {
        "status": "FILLED" | "REJECTED",
        "symbol": "BTC/USDT",
        "action": "BUY" | "SELL",
        "price": 65000.0,
        "quantity": 0.001,
        "cost": 65.0,      # price * quantity
        "fee": 0.065,      # exchange fee
        "order_id": "...",
        "timestamp": "2024-01-01T12:00:00",
        "mode": "PAPER" | "LIVE" | "SIMULATION",
    }
"""

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═════════════════════════════════════════════════════════════════

# Factory function (recommended way to create exchanges)
from app.exchange.factory import (
    create_exchange,
    get_available_exchanges,
    validate_exchange_config,
)

# Base interface (for type hints and isinstance checks)
from app.exchange.client import ExchangeClient

# Direct exchange classes (if you need specific exchange)
from app.exchange.paper import PaperExchange

# Conditional imports (may not be available if dependencies missing)
try:
    from app.exchange.alpaca import AlpacaExchange
    _ALPACA_AVAILABLE = True
except ImportError:
    AlpacaExchange = None
    _ALPACA_AVAILABLE = False

try:
    from app.exchange.binance import BinanceExchange
    _BINANCE_AVAILABLE = True
except ImportError:
    BinanceExchange = None
    _BINANCE_AVAILABLE = False


# ═════════════════════════════════════════════════════════════════
#  MODULE INFO
# ═════════════════════════════════════════════════════════════════

__all__ = [
    # Factory (recommended)
    "create_exchange",
    "get_available_exchanges",
    "validate_exchange_config",
    
    # Base class
    "ExchangeClient",
    
    # Exchange implementations
    "PaperExchange",
    "AlpacaExchange",
    "BinanceExchange",
]

__version__ = "2.0.0"


# ═════════════════════════════════════════════════════════════════
#  CONVENIENCE FUNCTIONS
# ═════════════════════════════════════════════════════════════════

def get_exchange_info() -> dict:
    """
    Get information about available exchanges.
    
    Returns:
        Dict with exchange availability and version info
        
    Example:
        >>> from app.exchange import get_exchange_info
        >>> info = get_exchange_info()
        >>> print(info)
        {
            'version': '2.0.0',
            'alpaca_available': True,
            'binance_available': True,
            'paper_available': True,
            'recommended': 'ALPACA'
        }
    """
    available = get_available_exchanges()
    
    # Determine recommended based on what's configured
    if available.get("ALPACA"):
        recommended = "ALPACA"
    elif available.get("BINANCE"):
        recommended = "BINANCE"
    else:
        recommended = "PAPER"
    
    return {
        "version": __version__,
        "alpaca_available": _ALPACA_AVAILABLE and available.get("ALPACA", False),
        "binance_available": _BINANCE_AVAILABLE and available.get("BINANCE", False),
        "paper_available": True,  # Always available
        "alpaca_keys_configured": available.get("ALPACA", False),
        "binance_keys_configured": available.get("BINANCE", False),
        "recommended": recommended,
    }


def quick_test(exchange_type: str = "PAPER") -> bool:
    """
    Quick connectivity test for an exchange.
    
    Args:
        exchange_type: "PAPER", "ALPACA", or "BINANCE"
        
    Returns:
        True if exchange responds, False otherwise
        
    Example:
        >>> from app.exchange import quick_test
        >>> quick_test("ALPACA")
        True
    """
    try:
        # Create a minimal state manager for testing
        class _MinimalState:
            _data = {"balance": 100.0}
            def get(self, key, default=None):
                return self._data.get(key, default)
            def set(self, key, value):
                self._data[key] = value
            def get_position(self, symbol):
                return None
            def get_all_positions(self):
                return {}
        
        exchange = create_exchange(
            mode="PAPER",
            exchange_type=exchange_type,
            state_manager=_MinimalState(),
        )
        
        # Try to ping
        result = exchange.ping()
        
        # Try to get a price
        if result:
            try:
                price = exchange.get_price("BTC/USDT")
                result = price > 0
            except Exception:
                pass
        
        # Cleanup
        try:
            exchange.close()
        except Exception:
            pass
        
        return result
        
    except Exception as e:
        print(f"Quick test failed: {e}")
        return False


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Run module diagnostics.
    
    Usage:
        python -m app.exchange
    """
    print("=" * 60)
    print("Exchange Module Diagnostics")
    print("=" * 60)
    
    info = get_exchange_info()
    print(f"\nModule Version: {info['version']}")
    
    print(f"\nExchange Availability:")
    print(f"  • Alpaca:  {'✅ Available' if info['alpaca_available'] else '❌ Not available'}")
    print(f"  • Binance: {'✅ Available' if info['binance_available'] else '❌ Not available'}")
    print(f"  • Paper:   {'✅ Available' if info['paper_available'] else '❌ Not available'}")
    
    print(f"\nAPI Keys Configured:")
    print(f"  • Alpaca:  {'✅ Yes' if info['alpaca_keys_configured'] else '❌ No'}")
    print(f"  • Binance: {'✅ Yes' if info['binance_keys_configured'] else '❌ No'}")
    
    print(f"\nRecommended Exchange: {info['recommended']}")
    
    print(f"\nQuick Connectivity Test (Paper):")
    if quick_test("PAPER"):
        print("  ✅ Paper exchange working")
    else:
        print("  ❌ Paper exchange failed")
    
    print("\n" + "=" * 60)