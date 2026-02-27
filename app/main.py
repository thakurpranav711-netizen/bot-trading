# app/main.py

import asyncio
import signal
from pathlib import Path

from dotenv import load_dotenv

from app.tg.bot import start_telegram_bot
from app.orchestrator.scheduler import TradeScheduler
from app.orchestrator.controller import BotController
from app.state.manager import StateManager
from app.market.analyzer import MarketAnalyzer
from app.strategies.scalping import ScalpingStrategy
from app.exchange.paper import PaperExchange
from app.utils.logger import get_logger


# ===============================
# ENV
# ===============================
BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / "config" / ".env")

logger = get_logger(__name__)


# ===============================
# MAIN ENTRY
# ===============================
async def main():
    logger.info("🚀 Starting Trading Bot...")

    # ---------------------------
    # STATE
    # ---------------------------
    state = StateManager()

    # ---------------------------
    # CORE COMPONENTS
    # ---------------------------
    symbol = state.get("symbol", "BTCUSDT")
    quantity = state.get("trade_quantity", 0.001)

    exchange = PaperExchange(state)
    analyzer = MarketAnalyzer(symbol)
    strategy = ScalpingStrategy(symbol, quantity)

    # ---------------------------
    # CONTROLLER (BRAIN)
    # ---------------------------
    controller = BotController(
        state_manager=state,
        exchange=exchange,
        analyzer=analyzer,
        strategy=strategy,
        notifier=None,  # will be injected by telegram bot
    )

    # ---------------------------
    # SCHEDULER (HEARTBEAT)
    # ---------------------------
    scheduler = TradeScheduler(
        controller=controller,
        interval=state.get("analysis_interval", 3),
    )

    # ---------------------------
    # TELEGRAM BOT
    # ---------------------------
    telegram_task = asyncio.create_task(
        start_telegram_bot(controller),
        name="telegram-bot",
    )

    # ---------------------------
    # SCHEDULER TASK
    # ---------------------------
    scheduler_task = asyncio.create_task(
        scheduler.start(),
        name="trade-scheduler",
    )

    # ---------------------------
    # SHUTDOWN HANDLING
    # ---------------------------
    loop = asyncio.get_running_loop()

    def shutdown(sig):
        logger.warning(f"⚠️ Shutdown signal received: {sig}")
        controller.stop_bot()
        scheduler.stop()

        telegram_task.cancel()
        scheduler_task.cancel()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, shutdown, sig)
    except NotImplementedError:
        logger.warning("Signal handlers not supported on this platform")

    logger.info("✅ Bot is live and running")

    # ---------------------------
    # KEEP ALIVE
    # ---------------------------
    try:
        await asyncio.gather(
            telegram_task,
            scheduler_task,
            return_exceptions=True,
        )
    except asyncio.CancelledError:
        logger.info("🛑 Tasks cancelled")
    except Exception as e:
        logger.exception(f"❌ Runtime error: {e}")


# ===============================
# BOOTSTRAP
# ===============================
if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("👋 Bot shutdown requested")
    except Exception as e:
        logger.exception(f"❌ Fatal error: {e}")