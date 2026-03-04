# app/strategies/__init__.py

"""
Trading Strategies Module — Production Grade v2

This module provides trading strategy implementations:

Components:
───────────
• BaseStrategy    - Abstract base class for all strategies
• ScalpingStrategy - High-frequency scalping with tight SL/TP

Usage:
──────
    from app.strategies import ScalpingStrategy, BaseStrategy

    # Create strategy
    strategy = ScalpingStrategy(
        take_profit_pct=0.02,
        stop_loss_pct=0.01,
    )

    # Evaluate signals
    signal = strategy.evaluate(market_snapshot)
    # Returns: {"action": "BUY", "confidence": 0.85, ...}

Creating Custom Strategies:
───────────────────────────
    from app.strategies import BaseStrategy

    class MyStrategy(BaseStrategy):
        name = "my_strategy"

        def evaluate(self, snapshot: dict) -> dict:
            # Your logic here
            return {
                "action": "BUY",  # BUY, SELL, HOLD
                "confidence": 0.8,
                "reason": "Custom signal",
            }
"""

from typing import Dict, Any, List, Type

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════════════════
#  IMPORTS FROM SUBMODULES
# ═════════════════════════════════════════════════════════════════

from app.strategies.base import BaseStrategy
from app.strategies.scalping import ScalpingStrategy

# ═════════════════════════════════════════════════════════════════
#  STRATEGY REGISTRY
# ═════════════════════════════════════════════════════════════════

STRATEGY_REGISTRY: Dict[str, Type[BaseStrategy]] = {
    "scalping": ScalpingStrategy,
    "scalp": ScalpingStrategy,
}


def get_strategy(name: str, **kwargs) -> BaseStrategy:
    """
    Get a strategy instance by name.

    Args:
        name: Strategy name (e.g., "scalping")
        **kwargs: Strategy configuration parameters

    Returns:
        Strategy instance

    Raises:
        ValueError: If strategy not found

    Example:
        strategy = get_strategy("scalping", take_profit_pct=0.02)
    """
    name_lower = name.lower().strip()

    if name_lower not in STRATEGY_REGISTRY:
        available = ", ".join(STRATEGY_REGISTRY.keys())
        raise ValueError(
            f"Unknown strategy: {name}. Available: {available}"
        )

    strategy_class = STRATEGY_REGISTRY[name_lower]
    return strategy_class(**kwargs)


def register_strategy(name: str, strategy_class: Type[BaseStrategy]) -> None:
    """
    Register a custom strategy.

    Args:
        name: Strategy name for lookup
        strategy_class: Strategy class (must inherit BaseStrategy)

    Example:
        register_strategy("my_strategy", MyCustomStrategy)
    """
    if not issubclass(strategy_class, BaseStrategy):
        raise TypeError(
            f"{strategy_class} must inherit from BaseStrategy"
        )

    STRATEGY_REGISTRY[name.lower()] = strategy_class
    logger.info(f"Registered strategy: {name}")


def list_strategies() -> List[str]:
    """Get list of available strategy names."""
    return list(STRATEGY_REGISTRY.keys())


# ═════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═════════════════════════════════════════════════════════════════

__all__ = [
    "BaseStrategy",
    "ScalpingStrategy",
    "get_strategy",
    "register_strategy",
    "list_strategies",
    "STRATEGY_REGISTRY",
    "get_strategies_info",
]

__version__ = "2.0.0"


# ═════════════════════════════════════════════════════════════════
#  MODULE INFO
# ═════════════════════════════════════════════════════════════════

def get_strategies_info() -> Dict[str, Any]:
    """Get information about available strategies."""
    strategies = {}

    for name, cls in STRATEGY_REGISTRY.items():
        strategies[name] = {
            "class": cls.__name__,
            "description": getattr(cls, "description", "No description"),
        }

    return {
        "version": __version__,
        "count": len(set(STRATEGY_REGISTRY.values())),
        "strategies": strategies,
    }


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Strategies Module v2 — Diagnostics")
    print("=" * 60)

    info = get_strategies_info()

    print(f"\n  Version: {info['version']}")
    print(f"  Strategies: {info['count']}")

    print(f"\n  ── Available Strategies ──")
    for name, details in info["strategies"].items():
        print(f"  • {name}: {details['class']}")

    print(f"\n  ── Test Strategy Creation ──")
    try:
        strategy = get_strategy("scalping")
        print(f"  ✅ Created: {strategy}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    print("\n" + "=" * 60)