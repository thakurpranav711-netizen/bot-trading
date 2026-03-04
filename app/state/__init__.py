# State Management Module
# app/state/__init__.py

"""
State Management Module — Production Grade v2

This module provides persistent state management for the trading bot:

Components:
───────────
• StateManager - Atomic JSON-based state persistence with auto-save

Usage:
──────
    from app.state import StateManager

    # Create manager
    state = StateManager(state_file="app/state/state.json")

    # Get/set values
    balance = state.get("balance", 0)
    state.set("balance", 1000.0)

    # Positions
    state.set_position("BTC/USD", {"entry_price": 67000, "quantity": 0.5})
    positions = state.get_all_positions()

    # Save explicitly (also auto-saves on changes)
    state.save()

Features:
─────────
• Atomic file writes (no corruption on crash)
• Auto-save on state changes
• Default values from defaults.json
• Position management helpers
• Trade history tracking
• Thread-safe operations
• State reset capabilities
"""

from typing import Any, Dict, Optional

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════════════════
#  IMPORTS
# ═════════════════════════════════════════════════════════════════

from app.state.manager import StateManager

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═════════════════════════════════════════════════════════════════

__all__ = [
    "StateManager",
    "create_state_manager",
    "get_state_info",
]

__version__ = "2.0.0"

# Default paths
DEFAULT_STATE_FILE = "app/state/state.json"
DEFAULT_DEFAULTS_FILE = "app/state/defaults.json"


# ═════════════════════════════════════════════════════════════════
#  FACTORY FUNCTION
# ═════════════════════════════════════════════════════════════════

def create_state_manager(
    state_file: str = DEFAULT_STATE_FILE,
    defaults_file: str = DEFAULT_DEFAULTS_FILE,
    auto_save: bool = True,
) -> StateManager:
    """
    Create a StateManager instance with default configuration.

    Args:
        state_file: Path to state JSON file
        defaults_file: Path to defaults JSON file
        auto_save: Enable auto-save on changes

    Returns:
        Configured StateManager instance

    Example:
        state = create_state_manager()
        state.set("balance", 1000.0)
    """
    return StateManager(
        state_file=state_file,
        defaults_file=defaults_file,
        auto_save=auto_save,
    )


# ═════════════════════════════════════════════════════════════════
#  MODULE INFO
# ═════════════════════════════════════════════════════════════════

def get_state_info() -> Dict[str, Any]:
    """Get information about the state module."""
    return {
        "version": __version__,
        "default_state_file": DEFAULT_STATE_FILE,
        "default_defaults_file": DEFAULT_DEFAULTS_FILE,
        "components": {
            "StateManager": {
                "description": "JSON-based state persistence",
                "features": [
                    "atomic_writes",
                    "auto_save",
                    "default_values",
                    "position_management",
                    "trade_history",
                    "thread_safe",
                ],
            },
        },
    }


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    print("=" * 60)
    print("  State Module v2 — Diagnostics")
    print("=" * 60)

    info = get_state_info()

    print(f"\n  Version: {info['version']}")
    print(f"  State file: {info['default_state_file']}")
    print(f"  Defaults file: {info['default_defaults_file']}")

    print(f"\n  ── Components ──")
    for name, comp in info["components"].items():
        print(f"  📦 {name}: {comp['description']}")
        features = ", ".join(comp["features"][:4])
        print(f"     Features: {features}")

    print(f"\n  ── Test StateManager ──")
    try:
        state = create_state_manager()
        print(f"  ✅ StateManager created")
        print(f"  ✅ Balance: {state.get('balance', 0)}")
    except Exception as e:
        print(f"  ❌ Error: {e}")

    print("\n" + "=" * 60)