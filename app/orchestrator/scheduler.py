# app/orchestrator/scheduler.py

"""
Trade Scheduler — Production Grade

Autonomous market heartbeat that:
- Fires controller.on_start() on boot (TRADING BOT STARTED message)
- Fires controller.on_stop() on shutdown (TRADING BOT STOPPED message)
- Calls controller.run_cycle() on configurable interval
- Respects bot_active state (idles without busy-loop)
- Error budget: isolates per-cycle crashes (bot keeps running)
- Health monitor: alerts if cycle latency spikes
- Graceful shutdown with position awareness
- Cycle statistics tracking

Integration:
    Created in main.py, passed controller reference.
    scheduler.start() is the main entry point (awaited).
"""

import asyncio
import time
from datetime import datetime
from typing import Optional, Dict
from app.utils.logger import get_logger

logger = get_logger(__name__)


class TradeScheduler:
    """
    Autonomous Trading Scheduler

    Runs the trading loop at configured intervals.
    Handles errors gracefully without crashing the bot.
    """

    # Alert if cycle takes longer than this
    LATENCY_WARN_SECONDS = 30

    def __init__(
        self,
        controller,
        interval: int = 300,
        idle_poll: int = 2,
        max_consecutive_errors: int = 5,
    ):
        """
        Initialize scheduler.

        Args:
            controller: BotController instance
            interval: Seconds between trading cycles (default 300 = 5min)
            idle_poll: Seconds between checks when bot is inactive
            max_consecutive_errors: Stop bot after N consecutive errors
        """
        self.controller = controller
        self.interval = interval
        self.idle_poll = idle_poll
        self.max_consecutive_errors = max_consecutive_errors

        # ── State ─────────────────────────────────────────────────
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._consecutive_errors: int = 0

        # ── Statistics ────────────────────────────────────────────
        self._total_cycles: int = 0
        self._total_errors: int = 0
        self._total_latency: float = 0.0
        self._started_at: Optional[datetime] = None
        self._last_cycle_at: Optional[datetime] = None

    # ═════════════════════════════════════════════════════
    #  PUBLIC API
    # ═════════════════════════════════════════════════════

    async def start(self):
        """
        Main scheduler entry point.

        Flow:
        1. Fire on_start() (sends TRADING BOT STARTED)
        2. Loop: check bot_active, run cycle if active
        3. On shutdown: fire on_stop() (sends TRADING BOT STOPPED)
        """
        self._running = True
        self._started_at = datetime.utcnow()

        logger.info(
            f"⏱️ Scheduler started | Interval={self.interval}s | "
            f"IdlePoll={self.idle_poll}s"
        )

        # ── Boot notification ─────────────────────────────────────
        await self._call_lifecycle("on_start")

        try:
            while self._running:
                # ── Check if bot is active ────────────────────────
                bot_active = self.controller.state.get("bot_active", True)

                if not bot_active:
                    # Idle mode: short sleep, check again
                    await asyncio.sleep(self.idle_poll)
                    continue

                # ── Run trading cycle ─────────────────────────────
                await self._run_cycle_safe()

                # ── Wait for next interval ────────────────────────
                await self._interruptible_sleep(self.interval)

        except asyncio.CancelledError:
            logger.info("🛑 Scheduler cancelled")

        except Exception as e:
            logger.exception(f"💥 Scheduler fatal error: {e}")
            await self._notify_error("Scheduler crash", str(e))

        finally:
            self._running = False
            await self._call_lifecycle("on_stop", reason="Scheduler shutdown")
            logger.info(self._build_summary())

    def stop(self, reason: str = "User request"):
        """Request graceful shutdown."""
        logger.info(f"🛑 Scheduler stop requested | Reason: {reason}")
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()

    def run(self):
        """
        Create scheduler task (alternative to awaiting start()).

        Usage:
            scheduler.run()  # Creates task
            # Later...
            scheduler.stop()
        """
        if self._task and not self._task.done():
            logger.warning("⚠️ Scheduler already running")
            return

        self._task = asyncio.create_task(
            self.start(),
            name="trade_scheduler"
        )
        logger.info("📋 Scheduler task created")

    # ═════════════════════════════════════════════════════
    #  CYCLE EXECUTION
    # ═════════════════════════════════════════════════════

    async def _run_cycle_safe(self):
        """
        Execute one trading cycle with error isolation.

        Features:
        - Timing measurement
        - Error catching (cycle crash ≠ bot crash)
        - Consecutive error tracking
        - Latency warnings
        """
        cycle_start = time.monotonic()
        self._total_cycles += 1
        self._last_cycle_at = datetime.utcnow()

        cycle_num = self._total_cycles
        logger.debug(
            f"🔁 Cycle #{cycle_num} | "
            f"{self._last_cycle_at.strftime('%H:%M:%S')}"
        )

        try:
            # ── Run controller cycle ──────────────────────────────
            # Controller.run_cycle() is sync, run in thread to not block
            await asyncio.to_thread(self.controller.run_cycle)

            # ── Success: reset error counter ──────────────────────
            self._consecutive_errors = 0

        except Exception as e:
            self._total_errors += 1
            self._consecutive_errors += 1

            logger.exception(f"❌ Cycle #{cycle_num} error: {e}")
            await self._notify_error(f"Cycle #{cycle_num} failed", str(e))

            # ── Too many errors: emergency stop ───────────────────
            if self._consecutive_errors >= self.max_consecutive_errors:
                logger.critical(
                    f"🚨 {self._consecutive_errors} consecutive errors — "
                    f"triggering emergency stop"
                )
                self.controller.state.deactivate_bot()
                await self._notify_error(
                    "Emergency stop",
                    f"{self._consecutive_errors} consecutive cycle errors"
                )
                self._running = False
                return

        finally:
            elapsed = time.monotonic() - cycle_start
            self._total_latency += elapsed

            logger.debug(f"⏱️ Cycle #{cycle_num} completed in {elapsed:.2f}s")

            # ── Latency warning ───────────────────────────────────
            if elapsed > self.LATENCY_WARN_SECONDS:
                logger.warning(
                    f"⚠️ Slow cycle: {elapsed:.1f}s > "
                    f"{self.LATENCY_WARN_SECONDS}s limit"
                )
                await self._notify_error(
                    "Slow cycle detected",
                    f"Cycle took {elapsed:.1f}s"
                )

    # ═════════════════════════════════════════════════════
    #  LIFECYCLE HOOKS
    # ═════════════════════════════════════════════════════

    async def _call_lifecycle(self, method: str, **kwargs):
        """
        Call a controller lifecycle method safely.

        Handles both sync and async methods.
        """
        try:
            func = getattr(self.controller, method, None)
            if not func:
                return

            if asyncio.iscoroutinefunction(func):
                await func(**kwargs)
            else:
                await asyncio.to_thread(func, **kwargs) if kwargs else await asyncio.to_thread(func)

            logger.debug(f"✅ {method}() called")

        except Exception as e:
            logger.exception(f"❌ {method}() failed: {e}")

    # ═════════════════════════════════════════════════════
    #  INTERRUPTIBLE SLEEP
    # ═════════════════════════════════════════════════════

    async def _interruptible_sleep(self, seconds: int):
        """
        Sleep in 1-second chunks.

        Allows quick response to:
        - stop() calls
        - bot_active changes
        """
        for _ in range(seconds):
            if not self._running:
                break

            bot_active = self.controller.state.get("bot_active", True)
            if not bot_active:
                break

            await asyncio.sleep(1)

    # ═════════════════════════════════════════════════════
    #  NOTIFICATIONS
    # ═════════════════════════════════════════════════════

    async def _notify_error(self, context: str, error: str):
        """Send error notification via controller's notifier."""
        notifier = getattr(self.controller, "notifier", None)
        if not notifier:
            return

        try:
            send_error = getattr(notifier, "send_error", None)
            if send_error:
                if asyncio.iscoroutinefunction(send_error):
                    await send_error(context=context, error=error)
                else:
                    await asyncio.to_thread(send_error, context=context, error=error)
        except Exception as e:
            logger.error(f"❌ Failed to send error notification: {e}")

    # ═════════════════════════════════════════════════════
    #  STATISTICS
    # ═════════════════════════════════════════════════════

    def get_stats(self) -> Dict:
        """Get scheduler runtime statistics."""
        uptime = 0.0
        if self._started_at:
            uptime = (datetime.utcnow() - self._started_at).total_seconds()

        avg_latency = 0.0
        if self._total_cycles > 0:
            avg_latency = self._total_latency / self._total_cycles

        return {
            "running": self._running,
            "total_cycles": self._total_cycles,
            "total_errors": self._total_errors,
            "consecutive_errors": self._consecutive_errors,
            "avg_latency_sec": round(avg_latency, 3),
            "uptime_seconds": round(uptime, 1),
            "last_cycle_at": (
                self._last_cycle_at.isoformat() if self._last_cycle_at else None
            ),
            "started_at": (
                self._started_at.isoformat() if self._started_at else None
            ),
            "interval_seconds": self.interval,
            "max_consecutive_errors": self.max_consecutive_errors,
        }

    def _build_summary(self) -> str:
        """Build session summary string for logging."""
        stats = self.get_stats()
        return (
            f"📊 Session Summary | "
            f"Cycles={stats['total_cycles']} | "
            f"Errors={stats['total_errors']} | "
            f"AvgLatency={stats['avg_latency_sec']}s | "
            f"Uptime={stats['uptime_seconds']}s"
        )

    # ═════════════════════════════════════════════════════
    #  MANUAL CONTROLS
    # ═════════════════════════════════════════════════════

    async def force_cycle(self):
        """
        Immediately trigger one cycle outside normal interval.

        Useful for /force_cycle Telegram command.
        """
        if not self._running:
            logger.warning("⚠️ Cannot force cycle — scheduler not running")
            return

        logger.info("⚡ Manual cycle triggered")
        await self._run_cycle_safe()

    def update_interval(self, new_interval: int):
        """
        Update cycle interval without restart.

        Takes effect on next sleep cycle.
        """
        old = self.interval
        self.interval = max(10, new_interval)  # Minimum 10 seconds
        logger.info(f"⏱️ Interval updated: {old}s → {self.interval}s")

    def reset_error_count(self):
        """Reset consecutive error counter."""
        self._consecutive_errors = 0
        logger.info("🔄 Error counter reset")

    # ═════════════════════════════════════════════════════
    #  PROPERTIES
    # ═════════════════════════════════════════════════════

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running

    @property
    def cycles_completed(self) -> int:
        """Get total cycles completed."""
        return self._total_cycles

    @property
    def error_rate(self) -> float:
        """Get error rate as percentage."""
        if self._total_cycles == 0:
            return 0.0
        return round(self._total_errors / self._total_cycles * 100, 2)

    def __repr__(self) -> str:
        status = "🟢 Running" if self._running else "🔴 Stopped"
        return (
            f"<TradeScheduler {status} | "
            f"Cycles={self._total_cycles} | "
            f"Errors={self._total_errors} | "
            f"Interval={self.interval}s>"
        )