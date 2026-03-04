# Trading Bot Package
# app/__init__.py

"""
Trading Bot Application — Production Grade v2

A sophisticated autonomous trading bot with:
• 4-Brain decision engine
• Multi-exchange support (Alpaca, Binance, Paper)
• Adaptive risk management
• Real-time Telegram integration
• Persistent state management

Modules:
────────
• exchange     - Exchange clients (Alpaca, Binance, Paper)
• market       - Market analysis and data feeds
• strategies   - Trading strategies (Scalping, etc.)
• orchestrator - Bot controller and scheduler
• risk         - Risk management (KillSwitch, LossGuard, etc.)
• state        - Persistent state management
• tg           - Telegram bot integration
• utils        - Logging and time utilities

Quick Start:
────────────
    from app.orchestrator import create_trading_engine
    from app.exchange import create_exchange
    from app.state import create_state_manager
    from app.strategies import get_strategy

    # Setup
    state = create_state_manager()
    exchange = create_exchange("paper")
    strategy = get_strategy("scalping")

    # Create engine
    controller, scheduler = create_trading_engine(
        state_manager=state,
        exchange=exchange,
        analyzers={},
        strategy=strategy,
    )

    # Start
    await scheduler.start()
"""

from typing import Any, Dict

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════════════════
#  VERSION
# ═════════════════════════════════════════════════════════════════

__version__ = "2.0.0"
__author__ = "Trading Bot Team"

# ═════════════════════════════════════════════════════════════════
#  MODULE IMPORTS (lazy-loaded on access)
# ═════════════════════════════════════════════════════════════════

# Core utilities (always available)
from app.utils import get_logger, get_utc_now, format_timestamp

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═════════════════════════════════════════════════════════════════

__all__ = [
    "__version__",
    "__author__",
    "get_logger",
    "get_utc_now",
    "format_timestamp",
    "get_app_info",
    "check_dependencies",
]


# ═════════════════════════════════════════════════════════════════
#  APP INFO
# ═════════════════════════════════════════════════════════════════

def get_app_info() -> Dict[str, Any]:
    """
    Get comprehensive application information.

    Returns:
        Dict with version, modules, and status
    """
    modules = {}

    # Check each module
    module_checks = [
        ("utils", "app.utils"),
        ("exchange", "app.exchange"),
        ("market", "app.market"),
        ("strategies", "app.strategies"),
        ("orchestrator", "app.orchestrator"),
        ("risk", "app.risk"),
        ("state", "app.state"),
        ("tg", "app.tg"),
    ]

    for name, import_path in module_checks:
        try:
            module = __import__(import_path, fromlist=[""])
            version = getattr(module, "__version__", "unknown")
            modules[name] = {"available": True, "version": version}
        except ImportError as e:
            modules[name] = {"available": False, "error": str(e)}

    return {
        "name": "Trading Bot",
        "version": __version__,
        "author": __author__,
        "modules": modules,
        "modules_ok": all(m["available"] for m in modules.values()),
    }


def check_dependencies() -> Dict[str, bool]:
    """
    Check if all required dependencies are installed.

    Returns:
        Dict mapping package name to availability
    """
    dependencies = {
        "pandas": False,
        "numpy": False,
        "telegram": False,
        "alpaca_trade_api": False,
        "requests": False,
        "aiohttp": False,
    }

    for pkg in dependencies:
        try:
            __import__(pkg)
            dependencies[pkg] = True
        except ImportError:
            dependencies[pkg] = False

    return dependencies


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  Trading Bot v2 — Diagnostics")
    print("=" * 60)

    info = get_app_info()

    print(f"\n  {info['name']} v{info['version']}")
    print(f"  Author: {info['author']}")

    print(f"\n  ── Modules ──")
    for name, status in info["modules"].items():
        if status["available"]:
            print(f"  ✅ {name}: v{status['version']}")
        else:
            print(f"  ❌ {name}: {status.get('error', 'Not available')}")

    print(f"\n  All modules OK: {'✅' if info['modules_ok'] else '❌'}")

    print(f"\n  ── Dependencies ──")
    deps = check_dependencies()
    for pkg, available in deps.items():
        print(f"  {'✅' if available else '❌'} {pkg}")

    print("\n" + "=" * 60)