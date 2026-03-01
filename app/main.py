# app/main.py

"""
Trading Bot Entry Point — Production Grade

Orchestrates all components:
- Environment configuration
- State management
- Exchange connection (auto-detect live/paper)
- Market analyzer
- Trading strategy
- Risk management (KillSwitch, LossGuard, TradeLimiter, AdaptiveRisk)
- Bot controller
- Trade scheduler
- Telegram bot integration
- Graceful shutdown handling

Usage:
    python -m app.main
    # or
    python app/main.py
"""

import asyncio
import os
import signal
import sys
from pathlib import Path
from datetime import datetime
from typing import List

from dotenv import load_dotenv

# ── Load environment BEFORE importing app modules ─────────────────
BASE_DIR = Path(__file__).resolve().parents[1]
ENV_FILE = BASE_DIR / "config" / ".env"

if ENV_FILE.exists():
    load_dotenv(ENV_FILE)
else:
    # Try alternative locations
    for alt in [BASE_DIR / ".env", Path.cwd() / ".env"]:
        if alt.exists():
            load_dotenv(alt)
            break

# ── Now import app modules ────────────────────────────────────────
from app.utils.logger import get_logger
from app.state.manager import StateManager
from app.exchange.paper import PaperExchange
from app.exchange.binance import BinanceExchange
from app.market.analyzer import MarketAnalyzer
from app.strategies.scalping import ScalpingStrategy
from app.orchestrator.controller import BotController
from app.orchestrator.scheduler import TradeScheduler

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════
#  CONFIGURATION HELPERS
# ═════════════════════════════════════════════════════════════════

def _env_str(key: str, default: str = "") -> str:
    """Get string from environment."""
    return os.getenv(key, default).strip()


def _env_int(key: str, default: int) -> int:
    """Get integer from environment."""
    try:
        return int(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    """Get float from environment."""
    try:
        return float(os.getenv(key, default))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    """Get boolean from environment."""
    val = os.getenv(key, "").lower().strip()
    if val in ("true", "1", "yes", "on"):
        return True
    if val in ("false", "0", "no", "off"):
        return False
    return default


def _env_list(key: str, default: str = "") -> List[str]:
    """Get comma-separated list from environment."""
    raw = os.getenv(key, default)
    return [s.strip().upper() for s in raw.split(",") if s.strip()]


def _validate_config(config: dict) -> bool:
    """Validate critical configuration."""
    errors = []

    if not config["coins"]:
        errors.append("TRADING_COINS is empty")

    if config["base_risk"] <= 0 or config["base_risk"] > 0.1:
        errors.append(f"BASE_RISK={config['base_risk']} invalid (should be 0.001-0.1)")

    if config["max_daily_drawdown"] <= 0 or config["max_daily_drawdown"] > 0.5:
        errors.append(f"MAX_DAILY_DRAWDOWN={config['max_daily_drawdown']} invalid")

    if config["interval"] < 10:
        errors.append(f"ANALYSIS_INTERVAL={config['interval']} too short (min 10s)")

    if errors:
        for err in errors:
            logger.error(f"❌ Config error: {err}")
        return False

    return True


# ═════════════════════════════════════════════════════════════════
#  COMPONENT FACTORY
# ═════════════════════════════════════════════════════════════════

def create_exchange(mode: str, state: StateManager):
    """
    Create appropriate exchange based on mode.

    LIVE mode: Uses BinanceExchange (real API if keys present, else simulation)
    PAPER mode: Uses PaperExchange (always simulation)
    """
    if mode == "LIVE":
        exchange = BinanceExchange(state_manager=state)
        logger.info(f"💰 Exchange: Binance ({exchange.mode_label})")
    else:
        exchange = PaperExchange(state_manager=state)
        logger.info("📝 Exchange: Paper Trading")

    return exchange


def create_strategy(symbol: str, config: dict) -> ScalpingStrategy:
    """Create trading strategy with configuration."""
    strategy = ScalpingStrategy(
        symbol=symbol,
        risk_reward_ratio=config.get("risk_reward_ratio", 2.0),
        atr_multiplier=config.get("atr_multiplier", 1.2),
        min_confidence=config.get("min_confidence", 0.55),
        min_volatility_pct=config.get("min_volatility_pct", 0.002),
        max_volatility_pct=config.get("max_volatility_pct", 0.02),
    )
    logger.info(f"📈 Strategy: {strategy.name} | Symbol: {symbol}")
    return strategy


# ═════════════════════════════════════════════════════════════════
#  TELEGRAM INTEGRATION
# ═════════════════════════════════════════════════════════════════

async def start_telegram_safe(controller, max_retries: int = 3) -> bool:
    """
    Start Telegram bot with retry logic.

    Returns True if started successfully, False otherwise.
    Trading continues even if Telegram fails.
    """
    # Check if Telegram is configured
    token = _env_str("TELEGRAM_BOT_TOKEN")
    chat_id = _env_str("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning(
            "⚠️ Telegram not configured (missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID). "
            "Bot will run without notifications."
        )
        return False

    for attempt in range(max_retries):
        try:
            from app.tg.bot import start_telegram_bot

            await start_telegram_bot(
                controller=controller,
                drop_pending_updates=True,
            )
            logger.info("✅ Telegram bot started")
            return True

        except Exception as e:
            logger.warning(
                f"⚠️ Telegram start failed (attempt {attempt + 1}/{max_retries}): {e}"
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(10)

    logger.error(
        "❌ Telegram bot failed to start after all retries. "
        "Trading will continue without Telegram notifications. "
        "To fix: restart the bot or check TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID."
    )
    return False


# ═════════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═════════════════════════════════════════════════════════════════

async def main():
    """Main async entry point."""
    logger.info("🚀 Starting 4-Brain Trading Bot...")
    logger.info(f"📁 Base directory: {BASE_DIR}")

    # ══════════════════════════════════════════════════════════════
    #  LOAD CONFIGURATION
    # ══════════════════════════════════════════════════════════════

    config = {
        # Trading mode
        "mode": _env_str("TRADING_MODE", "PAPER").upper(),

        # Coins to trade
        "coins": _env_list("TRADING_COINS", "BTC/USDT,ETH/USDT"),

        # Timing
        "interval": _env_int("ANALYSIS_INTERVAL", 300),

        # Risk management
        "base_risk": _env_float("BASE_RISK", 0.01),
        "min_risk": _env_float("MIN_RISK", 0.003),
        "max_risk": _env_float("MAX_RISK", 0.03),
        "max_daily_drawdown": _env_float("MAX_DAILY_DRAWDOWN", 0.05),
        "max_exposure_pct": _env_float("MAX_EXPOSURE_PCT", 0.30),
        "max_consecutive_losses": _env_int("MAX_CONSECUTIVE_LOSSES", 3),

        # Fees
        "fee_pct": _env_float("FEE_PCT", 0.001),
        "slippage_pct": _env_float("SLIPPAGE_PCT", 0.0005),

        # Strategy
        "risk_reward_ratio": _env_float("RISK_REWARD_RATIO", 2.0),
        "atr_multiplier": _env_float("ATR_MULTIPLIER", 1.2),
        "min_confidence": _env_float("MIN_CONFIDENCE", 0.55),
        "min_volatility_pct": _env_float("MIN_VOLATILITY_PCT", 0.002),
        "max_volatility_pct": _env_float("MAX_VOLATILITY_PCT", 0.02),

        # Scheduler
        "max_scheduler_errors": _env_int("MAX_SCHED_ERRORS", 5),

        # Initial balance (for paper trading)
        "initial_balance": _env_float("INITIAL_BALANCE", 100.0),
    }

    # Validate configuration
    if not _validate_config(config):
        logger.critical("❌ Invalid configuration — exiting")
        sys.exit(1)

    # Primary trading symbol
    primary_symbol = config["coins"][0]

    logger.info(
        f"⚙️ Config loaded | Mode={config['mode']} | "
        f"Coins={config['coins']} | Interval={config['interval']}s | "
        f"BaseRisk={config['base_risk'] * 100:.1f}%"
    )

    # ══════════════════════════════════════════════════════════════
    #  INITIALIZE COMPONENTS
    # ══════════════════════════════════════════════════════════════

    # State Manager
    state = StateManager(initial_balance=config["initial_balance"])
    logger.info(f"📊 State: Balance=${state.get('balance', 0):.2f}")

    # Exchange
    exchange = create_exchange(config["mode"], state)

    # Market Analyzer
    analyzer = MarketAnalyzer(symbol=primary_symbol)
    logger.info(f"📈 Analyzer: {primary_symbol}")

    # Strategy
    strategy = create_strategy(primary_symbol, config)

    # Controller (creates all risk management components internally)
    controller = BotController(
        state_manager=state,
        exchange=exchange,
        analyzer=analyzer,
        strategy=strategy,
        notifier=None,  # Set by Telegram bot
        mode=config["mode"],
        coins=config["coins"],
        interval=config["interval"],
        base_risk=config["base_risk"],
        max_daily_drawdown=config["max_daily_drawdown"],
        max_exposure_pct=config["max_exposure_pct"],
        max_consecutive_losses=config["max_consecutive_losses"],
        fee_pct=config["fee_pct"],
        slippage_pct=config["slippage_pct"],
    )

    # Scheduler
    scheduler = TradeScheduler(
        controller=controller,
        interval=config["interval"],
        idle_poll=2,
        max_consecutive_errors=config["max_scheduler_errors"],
    )

    # Back-link scheduler to controller (for status commands)
    controller.scheduler = scheduler

    logger.info("✅ All components initialized")

    # ══════════════════════════════════════════════════════════════
    #  SETUP TASKS
    # ══════════════════════════════════════════════════════════════

    # Telegram task
    telegram_task = asyncio.create_task(
        start_telegram_safe(controller),
        name="telegram-bot",
    )

    # Scheduler task
    scheduler_task = asyncio.create_task(
        scheduler.start(),
        name="trade-scheduler",
    )

    # ══════════════════════════════════════════════════════════════
    #  GRACEFUL SHUTDOWN
    # ══════════════════════════════════════════════════════════════

    shutdown_event = asyncio.Event()

    def handle_shutdown(sig):
        """Handle shutdown signals."""
        logger.warning(f"⚠️ Received signal: {sig.name}")
        scheduler.stop(reason=f"Signal {sig.name}")
        shutdown_event.set()

    # Register signal handlers (Unix only)
    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, handle_shutdown, sig)
        logger.debug("📡 Signal handlers registered")
    except NotImplementedError:
        logger.warning("⚠️ Signal handlers not supported (Windows)")

    # ══════════════════════════════════════════════════════════════
    #  RUN UNTIL SHUTDOWN
    # ══════════════════════════════════════════════════════════════

    logger.info(
        f"✅ Bot is LIVE | Mode={config['mode']} | "
        f"Coins={','.join(config['coins'])} | "
        f"Interval={config['interval']}s"
    )

    try:
        # Wait for tasks
        results = await asyncio.gather(
            telegram_task,
            scheduler_task,
            return_exceptions=True,
        )

        # Log any exceptions
        for i, result in enumerate(results):
            if isinstance(result, Exception) and not isinstance(result, asyncio.CancelledError):
                task_name = ["Telegram", "Scheduler"][i]
                logger.error(f"❌ {task_name} task failed: {result}")

    except asyncio.CancelledError:
        logger.info("🛑 Main tasks cancelled")

    except Exception as e:
        logger.exception(f"❌ Runtime error: {e}")

    finally:
        # ══════════════════════════════════════════════════════════
        #  CLEANUP
        # ══════════════════════════════════════════════════════════

        logger.info("🧹 Cleaning up...")

        # Stop scheduler if still running
        if scheduler.is_running:
            scheduler.stop(reason="Shutdown")

        # Close exchange connections
        try:
            exchange.close()
        except Exception as e:
            logger.warning(f"⚠️ Exchange close error: {e}")

        # Cancel remaining tasks
        for task in asyncio.all_tasks(loop):
            if task is not asyncio.current_task() and not task.done():
                task.cancel()

        # Final stats
        stats = scheduler.get_stats()
        logger.info(
            f"📊 Final Stats | "
            f"Cycles={stats['total_cycles']} | "
            f"Errors={stats['total_errors']} | "
            f"Uptime={stats['uptime_seconds']:.0f}s"
        )

        logger.info("👋 Bot shutdown complete")


# ═════════════════════════════════════════════════════════════════
#  BOOTSTRAP
# ═════════════════════════════════════════════════════════════════

def run():
    """Synchronous entry point for console script."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Interrupted by user")
    except Exception as e:
        logger.exception(f"❌ Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run()