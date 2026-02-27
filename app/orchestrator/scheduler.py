# app/orchestrator/scheduler.py

import asyncio
import inspect
from app.utils.logger import get_logger

logger = get_logger(__name__)


class TradeScheduler:
    """
    Autonomous Market Heartbeat

    Responsibilities:
    - Runs continuously
    - Calls controller cycle
    - Respects bot state
    - Handles timing & resilience
    """

    def __init__(self, controller, interval: int = 3):
        self.controller = controller
        self.interval = interval
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self):
        """
        Starts the scheduler loop
        """
        if self._running:
            logger.warning("⚠️ Scheduler already running")
            return

        self._running = True
        logger.info("⏱️ Trade Scheduler started")

        try:
            while self._running:
                # Respect bot state
                if not self.controller.state.get("bot_active"):
                    await asyncio.sleep(1)
                    continue

                try:
                    # Support async OR sync controller
                    result = self.controller.run_cycle()

                    if inspect.iscoroutine(result):
                        await result

                except Exception as e:
                    logger.exception(f"❌ Scheduler cycle error: {e}")

                await asyncio.sleep(self.interval)

        except asyncio.CancelledError:
            logger.info("🛑 Scheduler task cancelled")

        finally:
            self._running = False
            logger.info("🛑 Trade Scheduler stopped")

    def run(self):
        """
        Create asyncio task
        """
        if self._task and not self._task.done():
            logger.warning("⚠️ Scheduler task already running")
            return

        self._task = asyncio.create_task(self.start())

    def stop(self):
        """
        Graceful shutdown
        """
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()

        logger.info("🛑 Trade Scheduler stop requested")