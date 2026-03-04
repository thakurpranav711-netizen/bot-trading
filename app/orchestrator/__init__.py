# app/orchestrator/__init__.py

"""
Orchestrator Module — Trading Engine Core v2

This module contains the core trading engine components:

Components:
───────────
• BotController  - Central trading engine that coordinates all operations
• TradeScheduler - Autonomous loop with market awareness, backoff, metrics

Architecture:
─────────────
    ┌──────────────────────────────────────────────────────────────┐
    │                      TradeScheduler                          │
    │  • Configurable interval with jitter                         │
    │  • Market-hours awareness (skip when closed)                 │
    │  • Exponential backoff on errors                             │
    │  • Heartbeat monitoring & cycle metrics                      │
    │  • State machine (RUNNING, PAUSED, MARKET_CLOSED, etc.)      │
    └──────────────────────────┬───────────────────────────────────┘
                               │ run_cycle()
                               ▼
    ┌──────────────────────────────────────────────────────────────┐
    │                      BotController                           │
    │                                                              │
    │  ┌───────────┐ ┌───────────┐ ┌───────────┐ ┌──────────────┐ │
    │  │ Analyzer  │ │ Strategy  │ │ Exchange  │ │    Risk      │ │
    │  │ (Market)  │ │ (Signals) │ │ (Orders)  │ │  (Guards)    │ │
    │  │           │ │           │ │           │ │              │ │
    │  │ • OHLCV   │ │ • Scalp   │ │ • Alpaca  │ │ • KillSwitch│ │
    │  │ • Trends  │ │ • Custom  │ │ • Binance │ │ • LossGuard │ │
    │  │ • Signals │ │ • Backtest│ │ • Paper   │ │ • Adaptive  │ │
    │  └───────────┘ └───────────┘ └───────────┘ │ • Limiter   │ │
    │                                             └──────────────┘ │
    │  ┌───────────┐ ┌───────────┐                                 │
    │  │   State   │ │ Notifier  │                                 │
    │  │ (Persist) │ │(Telegram) │                                 │
    │  │           │ │           │                                 │
    │  │ • JSON    │ │ • Alerts  │                                 │
    │  │ • Atomic  │ │ • Reports │                                 │
    │  └───────────┘ └───────────┘                                 │
    └──────────────────────────────────────────────────────────────┘

Usage:
──────
    from app.orchestrator import BotController, TradeScheduler

    # Create controller with all dependencies
    controller = BotController(
        state_manager=state,
        exchange=exchange,
        analyzers={"BTC/USDT": analyzer},
        strategy=strategy,
        mode="PAPER",
        coins=["BTC/USDT"],
    )

    # Create scheduler
    scheduler = TradeScheduler(
        controller=controller,
        interval=300,
        market_aware=True,
        exchange_type="crypto",
    )

    # Link them
    controller.scheduler = scheduler

    # Start (async)
    await scheduler.start()

Quick Setup:
────────────
    from app.orchestrator import create_trading_engine

    controller, scheduler = create_trading_engine(
        state_manager=state,
        exchange=exchange,
        analyzers={"BTC/USDT": analyzer},
        strategy=strategy,
        interval=300,
    )
    await scheduler.start()

Trading Cycle Flow:
───────────────────
    1. Scheduler fires controller.run_cycle()
    2. Controller checks safety gates (kill switch, loss guard, limiter)
    3. Controller analyzes all coins (market snapshots + indicators)
    4. Controller manages positions (SL/TP/trailing stop updates)
    5. Controller evaluates new entry signals
    6. Controller executes orders via exchange
    7. Controller sends reports via Telegram
    8. Scheduler records CycleResult (latency, trades, success)
    9. Scheduler sleeps (with jitter) until next interval
    10. Repeat...

Lifecycle Events:
─────────────────
    • on_start()  → Called at boot (sends TRADING BOT STARTED)
    • on_stop()   → Called at shutdown (sends TRADING BOT STOPPED)
    • run_cycle() → Called every interval (main trading logic)

Scheduler States:
─────────────────
    IDLE → STARTING → RUNNING ⇄ PAUSED
                        ↓          ↓
                  MARKET_CLOSED  ERROR_BACKOFF
                        ↓          ↓
                     STOPPING → STOPPED
"""

from typing import Any, Dict, List, Optional, Tuple

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════════════════
#  PUBLIC IMPORTS
# ═════════════════════════════════════════════════════════════════

from app.orchestrator.controller import BotController
from app.orchestrator.scheduler import (
    TradeScheduler,
    SchedulerConfig,
    SchedulerState,
    CycleResult,
)

__all__ = [
    # Core
    "BotController",
    "TradeScheduler",
    # Scheduler types
    "SchedulerConfig",
    "SchedulerState",
    "CycleResult",
    # Helpers
    "create_trading_engine",
    "get_orchestrator_info",
]

__version__ = "2.0.0"


# ═════════════════════════════════════════════════════════════════
#  MODULE INFO
# ═════════════════════════════════════════════════════════════════

def get_orchestrator_info() -> Dict[str, Any]:
    """
    Get comprehensive information about the orchestrator module.

    Returns:
        Dict with version, components, features, and scheduler states
    """
    return {
        "version": __version__,
        "components": {
            "BotController": {
                "description": "Central trading engine",
                "responsibilities": [
                    "Market analysis coordination",
                    "Signal evaluation",
                    "Order execution",
                    "Position lifecycle management",
                    "Risk gate checking",
                    "Telegram notifications",
                ],
            },
            "TradeScheduler": {
                "description": "Autonomous trading loop",
                "responsibilities": [
                    "Cycle timing with jitter",
                    "Market-hours awareness",
                    "Error budget with exponential backoff",
                    "Heartbeat monitoring",
                    "Cycle metrics and sliding-window stats",
                    "State machine transitions",
                    "Emergency stop on consecutive failures",
                    "Lifecycle hook management",
                ],
            },
        },
        "scheduler_states": [s.value for s in SchedulerState],
        "features": [
            "Multi-coin monitoring",
            "4-Brain decision engine",
            "Position lifecycle management",
            "Risk management integration",
            "Telegram notifications",
            "Market-hours awareness",
            "Exponential backoff on errors",
            "Heartbeat liveness monitoring",
            "Sliding-window cycle metrics",
            "Dynamic interval adjustment",
            "Graceful shutdown with summary",
            "Pause / resume support",
            "Manual force-cycle trigger",
        ],
    }


# ═════════════════════════════════════════════════════════════════
#  QUICK SETUP HELPER
# ═════════════════════════════════════════════════════════════════

def create_trading_engine(
    state_manager,
    exchange,
    analyzers: Dict[str, Any],
    strategy,
    mode: str = "PAPER",
    coins: Optional[List[str]] = None,
    interval: int = 300,
    market_aware: bool = False,
    exchange_type: str = "crypto",
    jitter_seconds: float = 0.0,
    max_scheduler_errors: int = 5,
    notifier=None,
    **controller_kwargs,
) -> Tuple["BotController", "TradeScheduler"]:
    """
    Quick setup helper to create controller and scheduler together.

    Creates both components, links them, and returns a ready-to-start
    trading engine.

    Args:
        state_manager: StateManager instance
        exchange: Exchange client (Alpaca, Binance, Paper, etc.)
        analyzers: Dict mapping symbol → MarketAnalyzer
        strategy: Trading strategy instance
        mode: "PAPER" or "LIVE"
        coins: List of symbols to trade (defaults to analyzer keys)
        interval: Seconds between trading cycles
        market_aware: Skip cycles when market is closed
        exchange_type: Exchange type for market-hours ("crypto", "us_stock")
        jitter_seconds: Random jitter added to interval
        max_scheduler_errors: Emergency stop threshold
        notifier: Telegram notifier instance (optional)
        **controller_kwargs: Additional BotController arguments

    Returns:
        Tuple of (BotController, TradeScheduler)

    Example:
        controller, scheduler = create_trading_engine(
            state_manager=state,
            exchange=exchange,
            analyzers={"BTC/USDT": analyzer},
            strategy=strategy,
            mode="PAPER",
            coins=["BTC/USDT"],
            interval=300,
            market_aware=True,
            exchange_type="crypto",
        )

        # Start trading
        await scheduler.start()

        # Or as background task
        task = scheduler.run()
    """
    resolved_coins = coins or list(analyzers.keys())

    logger.info(
        f"🏗️ Creating trading engine | "
        f"mode={mode} | coins={resolved_coins} | "
        f"interval={interval}s | "
        f"market_aware={market_aware} | "
        f"exchange={exchange_type}"
    )

    # ── Create controller ─────────────────────────────────────
    controller = BotController(
        state_manager=state_manager,
        exchange=exchange,
        analyzers=analyzers,
        strategy=strategy,
        mode=mode,
        coins=resolved_coins,
        interval=interval,
        **controller_kwargs,
    )

    # Attach notifier if provided
    if notifier is not None:
        controller.notifier = notifier

    # ── Create scheduler ──────────────────────────────────────
    scheduler = TradeScheduler(
        controller=controller,
        interval=interval,
        idle_poll=2,
        max_consecutive_errors=max_scheduler_errors,
        market_aware=market_aware,
        exchange_type=exchange_type,
        jitter_seconds=jitter_seconds,
    )

    # ── Cross-link ────────────────────────────────────────────
    controller.scheduler = scheduler

    logger.info(
        f"✅ Trading engine created | "
        f"controller={controller.__class__.__name__} | "
        f"scheduler={scheduler}"
    )

    return controller, scheduler


# ═════════════════════════════════════════════════════════════════
#  VALIDATION HELPER
# ═════════════════════════════════════════════════════════════════

def validate_engine_config(
    state_manager=None,
    exchange=None,
    analyzers: Optional[Dict] = None,
    strategy=None,
    coins: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Validate that all engine components are properly configured.

    Args:
        state_manager: StateManager to validate
        exchange: Exchange client to validate
        analyzers: Analyzers dict to validate
        strategy: Strategy to validate
        coins: Coin list to validate

    Returns:
        Dict with validation results:
        {
            "valid": bool,
            "errors": ["..."],
            "warnings": ["..."],
            "components": {"state": True, "exchange": True, ...}
        }
    """
    errors: List[str] = []
    warnings: List[str] = []
    components: Dict[str, bool] = {}

    # ── State Manager ─────────────────────────────────────────
    if state_manager is None:
        errors.append("state_manager is required")
        components["state_manager"] = False
    else:
        has_get = hasattr(state_manager, "get")
        has_set = hasattr(state_manager, "set")
        components["state_manager"] = has_get and has_set
        if not has_get:
            errors.append("state_manager missing get() method")
        if not has_set:
            errors.append("state_manager missing set() method")

    # ── Exchange ──────────────────────────────────────────────
    if exchange is None:
        errors.append("exchange is required")
        components["exchange"] = False
    else:
        components["exchange"] = True
        # Check for essential methods
        for method in ("get_account", "create_order"):
            if not hasattr(exchange, method):
                warnings.append(
                    f"exchange missing {method}() — "
                    f"some features may not work"
                )

    # ── Analyzers ─────────────────────────────────────────────
    if not analyzers:
        errors.append("analyzers dict is required (at least 1 symbol)")
        components["analyzers"] = False
    else:
        components["analyzers"] = True
        if len(analyzers) == 0:
            errors.append("analyzers dict is empty")

    # ── Strategy ──────────────────────────────────────────────
    if strategy is None:
        errors.append("strategy is required")
        components["strategy"] = False
    else:
        components["strategy"] = True
        if not hasattr(strategy, "evaluate"):
            warnings.append(
                "strategy missing evaluate() method"
            )

    # ── Coins ─────────────────────────────────────────────────
    if coins is not None:
        components["coins"] = len(coins) > 0
        if len(coins) == 0:
            errors.append("coins list is empty")

        # Check coins have analyzers
        if analyzers:
            for coin in coins:
                if coin not in analyzers:
                    warnings.append(
                        f"coin '{coin}' has no analyzer configured"
                    )
    else:
        components["coins"] = True  # Will default to analyzer keys

    valid = len(errors) == 0

    return {
        "valid": valid,
        "errors": errors,
        "warnings": warnings,
        "components": components,
    }


# ═════════════════════════════════════════════════════════════════
#  MODULE SELF-TEST
# ═════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    """
    Run module diagnostics.

    Usage:
        python -m app.orchestrator
    """
    print("=" * 64)
    print("  Orchestrator Module v2 — Diagnostics")
    print("=" * 64)

    info = get_orchestrator_info()

    print(f"\n  Version: {info['version']}")

    # ── Components ──
    print(f"\n  ── Components ──")
    for name, comp in info["components"].items():
        print(f"\n  📦 {name}: {comp['description']}")
        for resp in comp["responsibilities"][:4]:
            print(f"     • {resp}")
        remaining = len(comp["responsibilities"]) - 4
        if remaining > 0:
            print(f"     ... +{remaining} more")

    # ── Scheduler States ──
    print(f"\n  ── Scheduler States ──")
    states = info["scheduler_states"]
    print(f"  {' → '.join(states[:4])}")
    if len(states) > 4:
        print(f"  {' → '.join(states[4:])}")

    # ── Features ──
    print(f"\n  ── Features ({len(info['features'])}) ──")
    for feat in info["features"]:
        print(f"  ✅ {feat}")

    # ── Import Test ──
    print(f"\n  ── Import Test ──")
    imports = {
        "BotController": BotController,
        "TradeScheduler": TradeScheduler,
        "SchedulerConfig": SchedulerConfig,
        "SchedulerState": SchedulerState,
        "CycleResult": CycleResult,
    }
    for name, cls in imports.items():
        print(f"  ✅ {name}: {cls}")

    # ── Validation Test ──
    print(f"\n  ── Validation Test (no args) ──")
    result = validate_engine_config()
    print(f"  Valid: {'✅' if result['valid'] else '❌'}")
    for err in result["errors"]:
        print(f"    ❌ {err}")
    for warn in result["warnings"]:
        print(f"    ⚠️ {warn}")
    print(f"  Components: {result['components']}")

    print("\n" + "=" * 64)
    print("  ✅ Orchestrator Module v2 — Ready")
    print("=" * 64)