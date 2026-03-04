# app/exchange/factory.py

"""
Exchange Factory — Production Grade

Auto-detects and creates the appropriate exchange based on:
1. Available API keys in environment
2. User preference in EXCHANGE setting
3. Trading mode (PAPER vs LIVE)

Supported Exchanges:
- ALPACA: Alpaca Markets (Paper & Live trading)
- BINANCE: Binance Spot (Live & Simulation)
- PAPER: Pure simulation (no API needed)

Priority when EXCHANGE=AUTO:
1. Alpaca (if ALPACA_API_KEY + ALPACA_SECRET_KEY present)
2. Binance (if BINANCE_API_KEY + BINANCE_SECRET present)
3. Paper (fallback — always works)

Usage:
    from app.exchange.factory import create_exchange
    
    exchange = create_exchange(
        mode="LIVE",           # or "PAPER"
        exchange_type="AUTO",  # or "ALPACA", "BINANCE", "PAPER"
        state_manager=state,
    )
    
    # Now use exchange normally
    price = exchange.get_price("BTC/USDT")
    fill = exchange.buy("BTC/USDT", quantity=0.001)
"""

import os
from typing import Optional, Dict, Any
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════
#  API KEY DETECTION
# ═════════════════════════════════════════════════════════════════

def _get_alpaca_keys() -> Dict[str, str]:
    """
    Get Alpaca API keys from environment.
    
    Returns:
        Dict with 'api_key', 'secret_key', 'base_url'
        Empty strings if not configured.
    """
    return {
        "api_key": os.getenv("ALPACA_API_KEY", "").strip(),
        "secret_key": os.getenv("ALPACA_SECRET_KEY", "").strip(),
        "base_url": os.getenv(
            "ALPACA_BASE_URL", 
            "https://paper-api.alpaca.markets"
        ).strip(),
    }


def _get_binance_keys() -> Dict[str, str]:
    """
    Get Binance API keys from environment.
    
    Returns:
        Dict with 'api_key', 'secret'
        Empty strings if not configured.
    """
    return {
        "api_key": os.getenv("BINANCE_API_KEY", "").strip(),
        "secret": os.getenv("BINANCE_SECRET", "").strip(),
    }


def _has_alpaca_keys() -> bool:
    """Check if Alpaca API keys are configured."""
    keys = _get_alpaca_keys()
    return bool(keys["api_key"] and keys["secret_key"])


def _has_binance_keys() -> bool:
    """Check if Binance API keys are configured."""
    keys = _get_binance_keys()
    return bool(keys["api_key"] and keys["secret"])


# ═════════════════════════════════════════════════════════════════
#  EXCHANGE CREATION
# ═════════════════════════════════════════════════════════════════

def create_exchange(
    mode: str,
    exchange_type: str,
    state_manager,
) -> Any:
    """
    Factory function to create the appropriate exchange.

    Args:
        mode: Trading mode
            - "PAPER": Paper trading / simulation
            - "LIVE": Real money trading
            
        exchange_type: Which exchange to use
            - "AUTO": Auto-detect based on available API keys
            - "ALPACA": Force Alpaca (requires keys)
            - "BINANCE": Force Binance (works without keys in sim mode)
            - "PAPER": Force paper trading simulation
            
        state_manager: StateManager instance for position/balance tracking

    Returns:
        Exchange instance implementing ExchangeClient interface
        
    Raises:
        None — always returns a working exchange (falls back to Paper)

    Examples:
        # Auto-detect (recommended)
        exchange = create_exchange("LIVE", "AUTO", state)
        
        # Force Alpaca
        exchange = create_exchange("PAPER", "ALPACA", state)
        
        # Force simulation
        exchange = create_exchange("PAPER", "PAPER", state)
    """
    mode = mode.upper().strip()
    exchange_type = exchange_type.upper().strip()

    logger.info(
        f"🏭 Exchange Factory | Mode={mode} | Type={exchange_type}"
    )

    # ══════════════════════════════════════════════════════════════
    #  FORCE PAPER MODE
    # ══════════════════════════════════════════════════════════════
    
    if exchange_type == "PAPER":
        return _create_paper_exchange(state_manager)

    # ══════════════════════════════════════════════════════════════
    #  PAPER MODE WITH AUTO — Use simulation
    # ══════════════════════════════════════════════════════════════
    
    if mode == "PAPER" and exchange_type == "AUTO":
        # In paper mode with auto, prefer paper exchange for safety
        # unless user explicitly requested a specific exchange
        logger.info("📝 PAPER mode with AUTO — using Paper exchange for safety")
        return _create_paper_exchange(state_manager)

    # ══════════════════════════════════════════════════════════════
    #  AUTO DETECTION
    # ══════════════════════════════════════════════════════════════
    
    if exchange_type == "AUTO":
        detected = _auto_detect_exchange()
        
        if detected:
            exchange_type = detected
            logger.info(f"🔍 Auto-detected exchange: {exchange_type}")
        else:
            logger.warning(
                "⚠️ No API keys found — falling back to Paper exchange.\n"
                "   To use real trading, set one of:\n"
                "   - ALPACA_API_KEY + ALPACA_SECRET_KEY\n"
                "   - BINANCE_API_KEY + BINANCE_SECRET"
            )
            return _create_paper_exchange(state_manager)

    # ══════════════════════════════════════════════════════════════
    #  CREATE ALPACA EXCHANGE
    # ══════════════════════════════════════════════════════════════
    
    if exchange_type == "ALPACA":
        return _create_alpaca_exchange(state_manager, mode)

    # ══════════════════════════════════════════════════════════════
    #  CREATE BINANCE EXCHANGE
    # ══════════════════════════════════════════════════════════════
    
    if exchange_type == "BINANCE":
        return _create_binance_exchange(state_manager, mode)

    # ══════════════════════════════════════════════════════════════
    #  FALLBACK — Unknown type
    # ══════════════════════════════════════════════════════════════
    
    logger.warning(
        f"⚠️ Unknown exchange type '{exchange_type}' — using Paper"
    )
    return _create_paper_exchange(state_manager)


# ═════════════════════════════════════════════════════════════════
#  AUTO DETECTION LOGIC
# ═════════════════════════════════════════════════════════════════

def _auto_detect_exchange() -> Optional[str]:
    """
    Auto-detect which exchange to use based on available API keys.
    
    Priority:
    1. Alpaca (most common for US users, good paper trading)
    2. Binance (global, high liquidity)
    
    Returns:
        "ALPACA", "BINANCE", or None if no keys found
    """
    if _has_alpaca_keys():
        logger.debug("✅ Alpaca API keys detected")
        return "ALPACA"
    
    if _has_binance_keys():
        logger.debug("✅ Binance API keys detected")
        return "BINANCE"
    
    logger.debug("❌ No exchange API keys detected")
    return None


# ═════════════════════════════════════════════════════════════════
#  EXCHANGE CREATORS
# ═════════════════════════════════════════════════════════════════

def _create_paper_exchange(state_manager) -> Any:
    """Create Paper (simulation) exchange."""
    from app.exchange.paper import PaperExchange
    
    exchange = PaperExchange(state_manager=state_manager)
    
    logger.info(
        f"📝 Created PaperExchange | "
        f"Balance=${state_manager.get('balance', 0):.2f}"
    )
    
    return exchange


def _create_alpaca_exchange(state_manager, mode: str) -> Any:
    """
    Create Alpaca exchange.
    
    Falls back to Paper if keys are missing.
    """
    if not _has_alpaca_keys():
        logger.error(
            "❌ ALPACA selected but no API keys found!\n"
            "   Set these in your .env file:\n"
            "   - ALPACA_API_KEY=your_key_here\n"
            "   - ALPACA_SECRET_KEY=your_secret_here\n"
            "   - ALPACA_BASE_URL=https://paper-api.alpaca.markets"
        )
        logger.warning("↩️ Falling back to Paper exchange")
        return _create_paper_exchange(state_manager)

    try:
        from app.exchange.alpaca import AlpacaExchange
        
        exchange = AlpacaExchange(state_manager=state_manager)
        
        keys = _get_alpaca_keys()
        is_paper = "paper" in keys["base_url"].lower()
        mode_label = "PAPER" if is_paper else "LIVE"
        
        logger.info(
            f"💰 Created AlpacaExchange | "
            f"Mode={mode_label} | "
            f"URL={keys['base_url'][:30]}..."
        )
        
        return exchange

    except Exception as e:
        logger.exception(f"❌ Failed to create AlpacaExchange: {e}")
        logger.warning("↩️ Falling back to Paper exchange")
        return _create_paper_exchange(state_manager)


def _create_binance_exchange(state_manager, mode: str) -> Any:
    """
    Create Binance exchange.
    
    Works without keys (simulation mode) or with keys (live mode).
    """
    try:
        from app.exchange.binance import BinanceExchange
        
        has_keys = _has_binance_keys()
        
        exchange = BinanceExchange(state_manager=state_manager)
        
        # Check if it connected in live mode
        actual_mode = getattr(exchange, 'mode_label', 'UNKNOWN')
        
        logger.info(
            f"💰 Created BinanceExchange | "
            f"Mode={actual_mode} | "
            f"Keys={'Present' if has_keys else 'Missing (simulation)'}"
        )
        
        return exchange

    except Exception as e:
        logger.exception(f"❌ Failed to create BinanceExchange: {e}")
        logger.warning("↩️ Falling back to Paper exchange")
        return _create_paper_exchange(state_manager)


# ═════════════════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ═════════════════════════════════════════════════════════════════

def get_available_exchanges() -> Dict[str, bool]:
    """
    Check which exchanges are available based on API keys.
    
    Returns:
        Dict mapping exchange name to availability
        
    Example:
        {
            "ALPACA": True,   # Keys present
            "BINANCE": False, # No keys
            "PAPER": True,    # Always available
        }
    """
    return {
        "ALPACA": _has_alpaca_keys(),
        "BINANCE": _has_binance_keys(),
        "PAPER": True,  # Always available
    }


def validate_exchange_config() -> Dict[str, Any]:
    """
    Validate exchange configuration and return diagnostic info.
    
    Useful for debugging and Telegram /status command.
    
    Returns:
        Dict with validation results
    """
    alpaca_keys = _get_alpaca_keys()
    binance_keys = _get_binance_keys()
    
    return {
        "alpaca": {
            "configured": _has_alpaca_keys(),
            "api_key_set": bool(alpaca_keys["api_key"]),
            "secret_key_set": bool(alpaca_keys["secret_key"]),
            "base_url": alpaca_keys["base_url"],
            "is_paper": "paper" in alpaca_keys["base_url"].lower(),
        },
        "binance": {
            "configured": _has_binance_keys(),
            "api_key_set": bool(binance_keys["api_key"]),
            "secret_set": bool(binance_keys["secret"]),
        },
        "recommended": _auto_detect_exchange() or "PAPER",
    }


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Quick test of exchange factory.
    Run with: python -m app.exchange.factory
    """
    print("=" * 50)
    print("Exchange Factory Diagnostic")
    print("=" * 50)
    
    available = get_available_exchanges()
    print(f"\nAvailable Exchanges:")
    for name, is_available in available.items():
        status = "✅" if is_available else "❌"
        print(f"  {status} {name}")
    
    config = validate_exchange_config()
    print(f"\nRecommended: {config['recommended']}")
    
    print(f"\nAlpaca Config:")
    for key, value in config["alpaca"].items():
        print(f"  {key}: {value}")
    
    print(f"\nBinance Config:")
    for key, value in config["binance"].items():
        print(f"  {key}: {value}")