# Risk Management Module
# app/risk/__init__.py

"""
Risk Management Module — Multi-Layer Protection System

This module provides comprehensive risk management for the trading bot:

Components:
───────────
• AdaptiveRiskManager - Dynamic position sizing based on performance
• KillSwitch          - Emergency stop system
• LossGuard           - Drawdown and loss protection
• TradeLimiter        - Trade frequency control

Architecture:
─────────────
    ┌─────────────────────────────────────────────────────────┐
    │                    BotController                         │
    │                         │                                │
    │         ┌───────────────┼───────────────┐               │
    │         ▼               ▼               ▼               │
    │  ┌────────────┐  ┌────────────┐  ┌────────────┐        │
    │  │ KillSwitch │  │ LossGuard  │  │TradeLimiter│        │
    │  │            │  │            │  │            │        │
    │  │ Emergency  │  │ Drawdown   │  │ Frequency  │        │
    │  │ Stop       │  │ Protection │  │ Control    │        │
    │  └────────────┘  └────────────┘  └────────────┘        │
    │         │               │               │               │
    │         └───────────────┼───────────────┘               │
    │                         ▼                                │
    │              ┌─────────────────────┐                    │
    │              │ AdaptiveRiskManager │                    │
    │              │                     │                    │
    │              │ Position Sizing     │                    │
    │              │ Kelly Criterion     │                    │
    │              │ Streak Adjustment   │                    │
    │              └─────────────────────┘                    │
    └─────────────────────────────────────────────────────────┘

Protection Layers:
──────────────────
    Layer 1: KillSwitch
        - Immediate halt of all trading
        - Auto-close positions (optional)
        - Auto-resume timer (optional)
        - Telegram notifications
        
    Layer 2: LossGuard
        - Daily drawdown limit (default 5%)
        - Emergency drawdown limit (default 15%)
        - Consecutive loss cooldown
        - Per-trade risk validation
        
    Layer 3: TradeLimiter
        - Daily trade limit
        - Hourly trade limit
        - Per-symbol limit
        - Minimum interval between trades
        - Rapid trading detection + cooldown
        
    Layer 4: AdaptiveRiskManager
        - Base risk allocation (default 1%)
        - Win/loss streak adjustment
        - Equity curve momentum
        - Volatility regime scaling
        - Confidence weighting
        - Kelly criterion cap

Usage:
──────
    from app.risk import (
        AdaptiveRiskManager,
        KillSwitch,
        LossGuard,
        TradeLimiter,
    )
    
    # Create risk components
    adaptive_risk = AdaptiveRiskManager(state_manager, base_risk=0.01)
    kill_switch = KillSwitch(state_manager, exchange)
    loss_guard = LossGuard(state_manager, kill_switch)
    trade_limiter = TradeLimiter(state_manager)
    
    # Check if trading is allowed
    if kill_switch.is_active():
        print("Kill switch active!")
        
    can_trade, reason = loss_guard.can_trade()
    if not can_trade:
        print(f"Loss guard blocked: {reason}")
        
    can_open, reason = trade_limiter.can_open_trade("BTC/USDT")
    if not can_open:
        print(f"Trade limited: {reason}")
    
    # Get position size
    risk_pct = adaptive_risk.get_risk_percent(market_state)
    
    # After trade
    adaptive_risk.update_after_trade({"pnl_amount": pnl})
    loss_guard.record_win(pnl, pnl_pct)  # or record_loss()
    trade_limiter.record_trade("BTC/USDT", "BUY")
"""

# ═════════════════════════════════════════════════════════════════
#  PUBLIC API
# ═════════════════════════════════════════════════════════════════

from app.risk.adaptive_risk import AdaptiveRiskManager
from app.risk.kill_switch import KillSwitch
from app.risk.loss_guard import LossGuard
from app.risk.trade_limiter import TradeLimiter

__all__ = [
    "AdaptiveRiskManager",
    "KillSwitch",
    "LossGuard",
    "TradeLimiter",
]

__version__ = "2.0.0"


# ═════════════════════════════════════════════════════════════════
#  MODULE INFO
# ═════════════════════════════════════════════════════════════════

def get_risk_info() -> dict:
    """
    Get information about the risk module.
    
    Returns:
        Dict with version and component info
    """
    return {
        "version": __version__,
        "components": {
            "AdaptiveRiskManager": "Dynamic position sizing",
            "KillSwitch": "Emergency stop system",
            "LossGuard": "Drawdown protection",
            "TradeLimiter": "Trade frequency control",
        },
        "protection_layers": [
            "Emergency stop (KillSwitch)",
            "Daily drawdown limit (LossGuard)",
            "Emergency drawdown limit (LossGuard)",
            "Consecutive loss cooldown (LossGuard)",
            "Daily trade limit (TradeLimiter)",
            "Hourly trade limit (TradeLimiter)",
            "Per-symbol limit (TradeLimiter)",
            "Minimum trade interval (TradeLimiter)",
            "Rapid trading cooldown (TradeLimiter)",
            "Adaptive position sizing (AdaptiveRiskManager)",
            "Kelly criterion cap (AdaptiveRiskManager)",
        ],
    }


# ═════════════════════════════════════════════════════════════════
#  QUICK SETUP HELPER
# ═════════════════════════════════════════════════════════════════

def create_risk_system(
    state_manager,
    exchange=None,
    base_risk: float = 0.01,
    max_daily_drawdown: float = 0.05,
    max_consecutive_losses: int = 3,
    max_trades_per_day: int = 10,
    max_trades_per_hour: int = 3,
) -> dict:
    """
    Quick setup helper to create all risk components at once.
    
    Args:
        state_manager: StateManager instance
        exchange: Exchange client (optional, for KillSwitch auto-close)
        base_risk: Base risk per trade (0.01 = 1%)
        max_daily_drawdown: Max daily loss before stopping (0.05 = 5%)
        max_consecutive_losses: Losses before cooldown
        max_trades_per_day: Daily trade limit
        max_trades_per_hour: Hourly trade limit
        
    Returns:
        Dict with all risk components
        
    Example:
        risk = create_risk_system(state, exchange, base_risk=0.01)
        
        # Access components
        adaptive_risk = risk["adaptive_risk"]
        kill_switch = risk["kill_switch"]
        loss_guard = risk["loss_guard"]
        trade_limiter = risk["trade_limiter"]
    """
    # Create kill switch first (loss_guard depends on it)
    kill_switch = KillSwitch(
        state_manager=state_manager,
        exchange=exchange,
        auto_close_positions=exchange is not None,
    )
    
    # Create loss guard
    loss_guard = LossGuard(
        state_manager=state_manager,
        kill_switch=kill_switch,
        max_daily_loss_pct=max_daily_drawdown,
        max_consecutive_losses=max_consecutive_losses,
    )
    
    # Create trade limiter
    trade_limiter = TradeLimiter(
        state_manager=state_manager,
        max_trades_per_day=max_trades_per_day,
        max_trades_per_hour=max_trades_per_hour,
    )
    
    # Create adaptive risk manager
    adaptive_risk = AdaptiveRiskManager(
        state_manager=state_manager,
        base_risk=base_risk,
        max_daily_dd=max_daily_drawdown,
    )
    
    return {
        "adaptive_risk": adaptive_risk,
        "kill_switch": kill_switch,
        "loss_guard": loss_guard,
        "trade_limiter": trade_limiter,
    }


# ═════════════════════════════════════════════════════════════════
#  COMBINED STATUS
# ═════════════════════════════════════════════════════════════════

def get_combined_risk_status(
    kill_switch: KillSwitch,
    loss_guard: LossGuard,
    trade_limiter: TradeLimiter,
    adaptive_risk: AdaptiveRiskManager,
) -> dict:
    """
    Get combined status from all risk components.
    
    Useful for Telegram /risk command or dashboards.
    
    Args:
        All four risk components
        
    Returns:
        Combined status dict
    """
    # Check if trading is allowed
    ks_active = kill_switch.is_active()
    lg_can_trade, lg_reason = loss_guard.can_trade()
    tl_can_trade, tl_reason = trade_limiter.can_open_trade()
    
    # Overall status
    can_trade = not ks_active and lg_can_trade and tl_can_trade
    
    block_reasons = []
    if ks_active:
        block_reasons.append("Kill switch active")
    if not lg_can_trade:
        block_reasons.append(lg_reason)
    if not tl_can_trade:
        block_reasons.append(tl_reason)
    
    return {
        "can_trade": can_trade,
        "block_reasons": block_reasons,
        "kill_switch": kill_switch.get_status(),
        "loss_guard": loss_guard.get_status(),
        "trade_limiter": trade_limiter.get_status(),
        "adaptive_risk": adaptive_risk.get_risk_report(),
    }


# ═════════════════════════════════════════════════════════════════
#  UNLOCK ALL
# ═════════════════════════════════════════════════════════════════

def unlock_all_risk(
    state_manager,
    kill_switch: KillSwitch,
    loss_guard: LossGuard,
    trade_limiter: TradeLimiter,
    adaptive_risk: AdaptiveRiskManager,
    source: str = "manual",
) -> dict:
    """
    Unlock all risk locks at once.
    
    Use with caution — this clears ALL protections.
    
    Args:
        All risk components
        source: Who triggered the unlock
        
    Returns:
        Summary of actions taken
    """
    results = {}
    
    # Clear state flags
    state_manager.set("bot_active", True)
    state_manager.set("risk_locked", False)
    
    # Kill switch
    results["kill_switch"] = kill_switch.deactivate(source=source)
    
    # Loss guard
    results["loss_guard"] = loss_guard.unlock(source=source)
    
    # Trade limiter cooldown
    trade_limiter.clear_cooldown(source=source)
    results["trade_limiter"] = {"cooldown_cleared": True}
    
    # Reset adaptive risk streaks
    adaptive_risk.reset_streaks()
    results["adaptive_risk"] = {"streaks_reset": True}
    
    return {
        "unlocked": True,
        "source": source,
        "results": results,
    }


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Run risk module diagnostics.
    
    Usage:
        python -m app.risk
    """
    print("=" * 60)
    print("Risk Management Module Diagnostics")
    print("=" * 60)
    
    info = get_risk_info()
    
    print(f"\nVersion: {info['version']}")
    
    print(f"\nComponents:")
    for name, desc in info["components"].items():
        print(f"  • {name}: {desc}")
    
    print(f"\nProtection Layers ({len(info['protection_layers'])}):")
    for layer in info["protection_layers"]:
        print(f"  ✅ {layer}")
    
    print(f"\nImport Test:")
    components = [
        ("AdaptiveRiskManager", AdaptiveRiskManager),
        ("KillSwitch", KillSwitch),
        ("LossGuard", LossGuard),
        ("TradeLimiter", TradeLimiter),
    ]
    
    for name, cls in components:
        try:
            assert cls is not None
            print(f"  ✅ {name} imported successfully")
        except Exception as e:
            print(f"  ❌ {name} import failed: {e}")
    
    print("\n" + "=" * 60)