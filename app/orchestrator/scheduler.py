# app/orchestrator/scheduler.py

"""
Trade Scheduler — Production Grade v2

Autonomous market heartbeat that:
- Fires controller.on_start() on boot (TRADING BOT STARTED message)
- Fires controller.on_stop() on shutdown (TRADING BOT STOPPED message)
- Calls controller.run_cycle() on configurable interval
- Respects bot_active state (idles without busy-loop)
- Market-hours awareness (optional: skip cycles when market closed)
- Error budget with exponential backoff on consecutive failures
- Health monitor: alerts if cycle latency spikes
- Graceful shutdown with position awareness
- Cycle statistics tracking with sliding window metrics
- Heartbeat pings to prove liveness
- Dynamic interval adjustment (adaptive mode)
- Async-native: no blocking calls on event loop
- Jitter support to avoid thundering-herd on restarts

Integration:
    Created in main.py, passed controller reference.
    scheduler.start() is the main entry point (awaited).

Usage:
    scheduler = TradeScheduler(controller, interval=300)
    await scheduler.start()

    # Or non-blocking:
    scheduler.run()
    # ...later...
    scheduler.stop("maintenance")

    # Manual trigger:
    await scheduler.force_cycle()

    # Adjust at runtime:
    scheduler.update_interval(60)
"""

import asyncio
import random
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Deque, Dict, List, Optional

from app.utils.logger import get_logger
from app.utils.time import (
    Cooldown,
    Stopwatch,
    format_duration,
    get_uptime_str,
    get_utc_now,
    is_market_hours,
    market_status,
)

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════
#  ENUMS & DATA CLASSES
# ═════════════════════════════════════════════════════════════════

class SchedulerState(str, Enum):
    """Scheduler lifecycle states."""
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"           # Bot inactive, scheduler alive
    MARKET_CLOSED = "market_closed"
    ERROR_BACKOFF = "error_backoff"
    STOPPING = "stopping"
    STOPPED = "stopped"


@dataclass
class CycleResult:
    """Result of a single trading cycle."""
    cycle_number: int
    started_at: datetime
    duration_seconds: float
    success: bool
    error: Optional[str] = None
    trades_executed: int = 0


@dataclass
class SchedulerConfig:
    """Scheduler configuration (mutable at runtime)."""
    interval: int = 300                   # Seconds between cycles
    idle_poll: int = 2                    # Seconds between idle checks
    max_consecutive_errors: int = 5       # Emergency stop threshold
    latency_warn_seconds: float = 30.0    # Slow cycle alert threshold
    latency_critical_seconds: float = 120.0  # Very slow cycle alert
    jitter_seconds: float = 0.0           # Random jitter added to interval
    market_aware: bool = False            # Skip cycles when market closed
    exchange_type: str = "crypto"         # For market-hours check
    heartbeat_interval: int = 600         # Heartbeat log every N seconds
    backoff_base: float = 5.0             # Exponential backoff base (seconds)
    backoff_max: float = 300.0            # Max backoff (5 minutes)
    max_cycle_history: int = 100          # Sliding window for metrics
    adaptive_interval: bool = False       # Auto-adjust interval based on vol
    min_interval: int = 10               # Minimum allowed interval
    max_interval: int = 3600              # Maximum allowed interval


# ═════════════════════════════════════════════════════════════════
#  TRADE SCHEDULER
# ═════════════════════════════════════════════════════════════════

class TradeScheduler:
    """
    Autonomous Trading Scheduler v2

    Runs the trading loop at configured intervals with:
    - Market awareness
    - Error budget with exponential backoff
    - Cycle metrics and sliding-window stats
    - Heartbeat monitoring
    - Dynamic interval adjustment
    - Graceful shutdown
    """

    def __init__(
        self,
        controller,
        interval: int = 300,
        idle_poll: int = 2,
        max_consecutive_errors: int = 5,
        market_aware: bool = False,
        exchange_type: str = "crypto",
        jitter_seconds: float = 0.0,
        adaptive_interval: bool = False,
    ):
        """
        Initialize scheduler.

        Args:
            controller: BotController instance
            interval: Seconds between trading cycles (default 300 = 5min)
            idle_poll: Seconds between checks when bot is inactive
            max_consecutive_errors: Stop bot after N consecutive errors
            market_aware: If True, skip cycles when market is closed
            exchange_type: Exchange for market-hours check
            jitter_seconds: Random jitter added to sleep interval
            adaptive_interval: Auto-adjust interval based on market conditions
        """
        self.controller = controller

        # ── Configuration ─────────────────────────────────────────
        self.config = SchedulerConfig(
            interval=max(10, interval),
            idle_poll=max(1, idle_poll),
            max_consecutive_errors=max(1, max_consecutive_errors),
            jitter_seconds=max(0.0, jitter_seconds),
            market_aware=market_aware,
            exchange_type=exchange_type,
            adaptive_interval=adaptive_interval,
        )

        # ── State ─────────────────────────────────────────────────
        self._state: SchedulerState = SchedulerState.IDLE
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._consecutive_errors: int = 0
        self._current_backoff: float = 0.0

        # ── Statistics ────────────────────────────────────────────
        self._total_cycles: int = 0
        self._total_errors: int = 0
        self._total_skipped: int = 0
        self._total_latency: float = 0.0
        self._peak_latency: float = 0.0
        self._started_at: Optional[datetime] = None
        self._last_cycle_at: Optional[datetime] = None
        self._last_heartbeat: float = 0.0

        # ── Cycle History (sliding window) ────────────────────────
        self._cycle_history: Deque[CycleResult] = deque(
            maxlen=self.config.max_cycle_history
        )

        # ── Callbacks ─────────────────────────────────────────────
        self._on_cycle_complete: Optional[Callable] = None
        self._on_error: Optional[Callable] = None
        self._on_state_change: Optional[Callable] = None

    # ═════════════════════════════════════════════════════
    #  PROPERTIES
    # ═════════════════════════════════════════════════════

    @property
    def state(self) -> SchedulerState:
        """Current scheduler state."""
        return self._state

    @property
    def is_running(self) -> bool:
        """Check if scheduler is running."""
        return self._running

    @property
    def interval(self) -> int:
        """Current cycle interval in seconds."""
        return self.config.interval

    @interval.setter
    def interval(self, value: int) -> None:
        """Set cycle interval with bounds checking."""
        self.config.interval = max(
            self.config.min_interval,
            min(value, self.config.max_interval),
        )

    @property
    def cycles_completed(self) -> int:
        """Total cycles completed."""
        return self._total_cycles

    @property
    def error_rate(self) -> float:
        """Error rate as percentage."""
        if self._total_cycles == 0:
            return 0.0
        return round(self._total_errors / self._total_cycles * 100, 2)

    @property
    def uptime_seconds(self) -> float:
        """Scheduler uptime in seconds."""
        if not self._started_at:
            return 0.0
        return (get_utc_now() - self._started_at).total_seconds()

    # ═════════════════════════════════════════════════════
    #  STATE MANAGEMENT
    # ═════════════════════════════════════════════════════

    def _set_state(self, new_state: SchedulerState) -> None:
        """Update scheduler state and notify."""
        old_state = self._state
        if old_state == new_state:
            return

        self._state = new_state
        logger.info(
            f"📋 Scheduler state: {old_state.value} → {new_state.value}"
        )

        if self._on_state_change:
            try:
                self._on_state_change(old_state, new_state)
            except Exception as e:
                logger.error(f"State change callback error: {e}")

    # ═════════════════════════════════════════════════════
    #  PUBLIC API
    # ═════════════════════════════════════════════════════

    async def start(self) -> None:
        """
        Main scheduler entry point.

        Flow:
        1. Fire on_start() (sends TRADING BOT STARTED)
        2. Loop: check state, run cycle if active
        3. On shutdown: fire on_stop() (sends TRADING BOT STOPPED)
        """
        self._running = True
        self._started_at = get_utc_now()
        self._last_heartbeat = time.monotonic()
        self._set_state(SchedulerState.STARTING)

        logger.info(
            f"⏱️ Scheduler started | "
            f"interval={self.config.interval}s | "
            f"idle_poll={self.config.idle_poll}s | "
            f"market_aware={self.config.market_aware} | "
            f"exchange={self.config.exchange_type} | "
            f"jitter={self.config.jitter_seconds}s | "
            f"adaptive={self.config.adaptive_interval}"
        )

        # ── Boot notification ─────────────────────────────────────
        await self._call_lifecycle("on_start")
        self._set_state(SchedulerState.RUNNING)

        try:
            while self._running:
                # ── Heartbeat ─────────────────────────────────────
                await self._check_heartbeat()

                # ── Check if bot is active ────────────────────────
                bot_active = self._is_bot_active()

                if not bot_active:
                    self._set_state(SchedulerState.PAUSED)
                    await asyncio.sleep(self.config.idle_poll)
                    continue

                # ── Check market hours ────────────────────────────
                if self.config.market_aware:
                    mkt = market_status(self.config.exchange_type)
                    if not mkt.is_open:
                        if self._state != SchedulerState.MARKET_CLOSED:
                            self._set_state(SchedulerState.MARKET_CLOSED)
                            logger.info(
                                f"💤 Market closed ({mkt.session.value}) | "
                                f"{mkt.next_change}"
                            )
                        # Sleep longer when market is closed
                        sleep_time = min(
                            60, mkt.next_change_seconds
                        ) if mkt.next_change_seconds > 0 else 60
                        await asyncio.sleep(sleep_time)
                        self._total_skipped += 1
                        continue

                # ── Error backoff ─────────────────────────────────
                if self._current_backoff > 0:
                    self._set_state(SchedulerState.ERROR_BACKOFF)
                    logger.warning(
                        f"⏳ Error backoff: {self._current_backoff:.1f}s"
                    )
                    await self._interruptible_sleep(
                        int(self._current_backoff)
                    )
                    self._current_backoff = 0.0
                    if not self._running:
                        break

                # ── Run trading cycle ─────────────────────────────
                self._set_state(SchedulerState.RUNNING)
                await self._run_cycle_safe()

                if not self._running:
                    break

                # ── Wait for next interval ────────────────────────
                sleep_secs = self._compute_sleep_interval()
                await self._interruptible_sleep(sleep_secs)

        except asyncio.CancelledError:
            logger.info("🛑 Scheduler cancelled")

        except Exception as e:
            logger.exception(f"💥 Scheduler fatal error: {e}")
            await self._notify_error("Scheduler crash", str(e))

        finally:
            self._set_state(SchedulerState.STOPPING)
            self._running = False
            await self._call_lifecycle("on_stop", reason="Scheduler shutdown")
            self._set_state(SchedulerState.STOPPED)
            logger.info(self._build_summary())

    def stop(self, reason: str = "User request") -> None:
        """Request graceful shutdown."""
        logger.info(f"🛑 Scheduler stop requested | reason={reason}")
        self._running = False

        if self._task and not self._task.done():
            self._task.cancel()

    def run(self) -> asyncio.Task:
        """
        Create scheduler as a background task.

        Returns:
            The created asyncio.Task

        Usage:
            task = scheduler.run()
            # ...later...
            scheduler.stop()
        """
        if self._task and not self._task.done():
            logger.warning("⚠️ Scheduler already running")
            return self._task

        self._task = asyncio.create_task(
            self.start(),
            name="trade_scheduler",
        )
        logger.info("📋 Scheduler task created")
        return self._task

    def pause(self) -> None:
        """Pause the scheduler (cycles stop, scheduler stays alive)."""
        if hasattr(self.controller, "state"):
            self.controller.state.set("bot_active", False)
        self._set_state(SchedulerState.PAUSED)
        logger.info("⏸️ Scheduler paused")

    def resume(self) -> None:
        """Resume the scheduler from pause."""
        if hasattr(self.controller, "state"):
            self.controller.state.set("bot_active", True)
        self._set_state(SchedulerState.RUNNING)
        logger.info("▶️ Scheduler resumed")

    # ═════════════════════════════════════════════════════
    #  CYCLE EXECUTION
    # ═════════════════════════════════════════════════════

    async def _run_cycle_safe(self) -> Optional[CycleResult]:
        """
        Execute one trading cycle with full error isolation.

        Features:
        - Precise timing via Stopwatch
        - Error catching (cycle crash ≠ bot crash)
        - Consecutive error tracking with exponential backoff
        - Latency warnings (warn + critical thresholds)
        - Cycle result recording
        """
        sw = Stopwatch()
        self._total_cycles += 1
        self._last_cycle_at = get_utc_now()
        cycle_num = self._total_cycles

        logger.debug(
            f"🔁 Cycle #{cycle_num} starting | "
            f"{self._last_cycle_at.strftime('%H:%M:%S UTC')}"
        )

        result = CycleResult(
            cycle_number=cycle_num,
            started_at=self._last_cycle_at,
            duration_seconds=0.0,
            success=False,
        )

        try:
            # ── Run controller cycle ──────────────────────────────
            run_cycle = getattr(self.controller, "run_cycle", None)
            if run_cycle is None:
                logger.error("Controller has no run_cycle() method")
                result.error = "Missing run_cycle method"
                return result

            if asyncio.iscoroutinefunction(run_cycle):
                cycle_result = await run_cycle()
            else:
                cycle_result = await asyncio.to_thread(run_cycle)

            # ── Extract trade count from result ───────────────────
            if isinstance(cycle_result, dict):
                result.trades_executed = cycle_result.get(
                    "trades_executed", 0
                )

            # ── Success: reset error state ────────────────────────
            result.success = True
            self._consecutive_errors = 0
            self._current_backoff = 0.0

        except Exception as e:
            self._total_errors += 1
            self._consecutive_errors += 1
            result.error = str(e)

            logger.exception(
                f"❌ Cycle #{cycle_num} error "
                f"(consecutive: {self._consecutive_errors}/"
                f"{self.config.max_consecutive_errors}): {e}"
            )
            await self._notify_error(
                f"Cycle #{cycle_num} failed", str(e)
            )

            # ── Exponential backoff ───────────────────────────────
            self._current_backoff = min(
                self.config.backoff_base * (
                    2 ** (self._consecutive_errors - 1)
                ),
                self.config.backoff_max,
            )
            logger.info(
                f"⏳ Next backoff: {self._current_backoff:.1f}s"
            )

            # ── Emergency stop ────────────────────────────────────
            if (
                self._consecutive_errors
                >= self.config.max_consecutive_errors
            ):
                await self._emergency_stop()
                return result

        finally:
            sw.stop()
            elapsed = sw.elapsed
            result.duration_seconds = round(elapsed, 3)

            self._total_latency += elapsed
            self._peak_latency = max(self._peak_latency, elapsed)

            # ── Record cycle ──────────────────────────────────────
            self._cycle_history.append(result)

            # ── Callback ──────────────────────────────────────────
            if self._on_cycle_complete:
                try:
                    self._on_cycle_complete(result)
                except Exception:
                    pass

            # ── Latency warnings ──────────────────────────────────
            if elapsed > self.config.latency_critical_seconds:
                logger.error(
                    f"🚨 CRITICAL cycle latency: {elapsed:.1f}s "
                    f"(limit: {self.config.latency_critical_seconds}s)"
                )
                await self._notify_error(
                    "Critical cycle latency",
                    f"Cycle #{cycle_num} took {elapsed:.1f}s",
                )
            elif elapsed > self.config.latency_warn_seconds:
                logger.warning(
                    f"⚠️ Slow cycle: {elapsed:.1f}s "
                    f"(warn: {self.config.latency_warn_seconds}s)"
                )

            # ── Success log ───────────────────────────────────────
            status = "✅" if result.success else "❌"
            trades = (
                f" | trades={result.trades_executed}"
                if result.trades_executed > 0
                else ""
            )
            logger.debug(
                f"{status} Cycle #{cycle_num} done in "
                f"{elapsed:.2f}s{trades}"
            )

        return result

    # ═════════════════════════════════════════════════════
    #  EMERGENCY STOP
    # ═════════════════════════════════════════════════════

    async def _emergency_stop(self) -> None:
        """Trigger emergency stop after too many consecutive errors."""
        logger.critical(
            f"🚨 EMERGENCY STOP — {self._consecutive_errors} "
            f"consecutive errors exceeded limit of "
            f"{self.config.max_consecutive_errors}"
        )

        # Deactivate via controller state
        if hasattr(self.controller, "state"):
            deactivate = getattr(
                self.controller.state, "deactivate_bot", None
            )
            if deactivate:
                if asyncio.iscoroutinefunction(deactivate):
                    await deactivate()
                else:
                    deactivate()

        await self._notify_error(
            "🚨 Emergency Stop",
            f"{self._consecutive_errors} consecutive cycle errors. "
            f"Bot deactivated. Manual intervention required.",
        )

        self._running = False

    # ═════════════════════════════════════════════════════
    #  LIFECYCLE HOOKS
    # ═════════════════════════════════════════════════════

    async def _call_lifecycle(
        self, method: str, **kwargs
    ) -> None:
        """
        Call a controller lifecycle method safely.
        Handles both sync and async methods.
        """
        try:
            func = getattr(self.controller, method, None)
            if func is None:
                logger.debug(
                    f"Controller has no {method}() — skipping"
                )
                return

            if asyncio.iscoroutinefunction(func):
                await func(**kwargs)
            else:
                if kwargs:
                    await asyncio.to_thread(func, **kwargs)
                else:
                    await asyncio.to_thread(func)

            logger.debug(f"✅ Lifecycle {method}() completed")

        except Exception as e:
            logger.exception(f"❌ Lifecycle {method}() failed: {e}")

    # ═════════════════════════════════════════════════════
    #  SLEEP & TIMING
    # ═════════════════════════════════════════════════════

    def _compute_sleep_interval(self) -> int:
        """
        Compute sleep duration with optional jitter.

        Returns:
            Sleep duration in seconds
        """
        base = self.config.interval

        if self.config.jitter_seconds > 0:
            jitter = random.uniform(0, self.config.jitter_seconds)
            base = int(base + jitter)

        return max(self.config.min_interval, base)

    async def _interruptible_sleep(self, seconds: int) -> None:
        """
        Sleep in 1-second chunks for quick interrupt response.

        Checks:
        - stop() calls (self._running)
        - bot_active state changes
        """
        for _ in range(max(1, seconds)):
            if not self._running:
                break

            if not self._is_bot_active():
                break

            await asyncio.sleep(1)

    def _is_bot_active(self) -> bool:
        """Check if bot is active via controller state."""
        try:
            if hasattr(self.controller, "state"):
                state = self.controller.state
                if hasattr(state, "get"):
                    return state.get("bot_active", True)
                if isinstance(state, dict):
                    return state.get("bot_active", True)
            return True
        except Exception:
            return True

    # ═════════════════════════════════════════════════════
    #  HEARTBEAT
    # ═════════════════════════════════════════════════════

    async def _check_heartbeat(self) -> None:
        """Log periodic heartbeat to prove liveness."""
        now = time.monotonic()
        if (
            now - self._last_heartbeat
            >= self.config.heartbeat_interval
        ):
            self._last_heartbeat = now
            stats = self.get_stats()

            logger.info(
                f"💓 Heartbeat | "
                f"state={self._state.value} | "
                f"cycles={stats['total_cycles']} | "
                f"errors={stats['total_errors']} | "
                f"err_rate={stats['error_rate_pct']}% | "
                f"avg_latency={stats['avg_latency_sec']}s | "
                f"uptime={format_duration(stats['uptime_seconds'])}"
            )

    # ═════════════════════════════════════════════════════
    #  NOTIFICATIONS
    # ═════════════════════════════════════════════════════

    async def _notify_error(
        self, context: str, error: str
    ) -> None:
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
                    await asyncio.to_thread(
                        send_error, context=context, error=error
                    )
        except Exception as e:
            logger.error(f"Failed to send error notification: {e}")

    # ═════════════════════════════════════════════════════
    #  MANUAL CONTROLS
    # ═════════════════════════════════════════════════════

    async def force_cycle(self) -> Optional[CycleResult]:
        """
        Immediately trigger one cycle outside normal interval.

        Returns:
            CycleResult or None if scheduler not running

        Usage:
            result = await scheduler.force_cycle()
            if result and result.success:
                print(f"Cycle completed in {result.duration_seconds}s")
        """
        if not self._running:
            logger.warning("⚠️ Cannot force cycle — scheduler not running")
            return None

        logger.info("⚡ Manual cycle triggered")
        return await self._run_cycle_safe()

    def update_interval(self, new_interval: int) -> None:
        """
        Update cycle interval at runtime (bounded).

        Args:
            new_interval: New interval in seconds

        Takes effect on next sleep cycle.
        """
        old = self.config.interval
        self.config.interval = max(
            self.config.min_interval,
            min(new_interval, self.config.max_interval),
        )
        logger.info(
            f"⏱️ Interval updated: {old}s → {self.config.interval}s"
        )

    def reset_error_count(self) -> None:
        """Reset consecutive error counter and backoff."""
        self._consecutive_errors = 0
        self._current_backoff = 0.0
        logger.info("🔄 Error counter and backoff reset")

    def set_market_aware(
        self,
        enabled: bool,
        exchange: Optional[str] = None,
    ) -> None:
        """Toggle market-hours awareness at runtime."""
        self.config.market_aware = enabled
        if exchange:
            self.config.exchange_type = exchange
        logger.info(
            f"🏪 Market awareness: {'ON' if enabled else 'OFF'}"
            + (f" ({exchange})" if exchange else "")
        )

    # ═════════════════════════════════════════════════════
    #  CALLBACKS
    # ═════════════════════════════════════════════════════

    def on_cycle_complete(self, callback: Callable) -> None:
        """Register callback fired after each cycle."""
        self._on_cycle_complete = callback

    def on_error(self, callback: Callable) -> None:
        """Register callback fired on cycle errors."""
        self._on_error = callback

    def on_state_change(self, callback: Callable) -> None:
        """Register callback fired on state transitions."""
        self._on_state_change = callback

    # ═════════════════════════════════════════════════════
    #  STATISTICS
    # ═════════════════════════════════════════════════════

    def get_stats(self) -> Dict[str, Any]:
        """Get comprehensive scheduler statistics."""
        uptime = self.uptime_seconds

        avg_latency = 0.0
        if self._total_cycles > 0:
            avg_latency = self._total_latency / self._total_cycles

        # Sliding window metrics (last N cycles)
        recent = list(self._cycle_history)
        recent_success = sum(1 for c in recent if c.success)
        recent_total = len(recent)
        recent_avg = 0.0
        if recent_total > 0:
            recent_avg = sum(
                c.duration_seconds for c in recent
            ) / recent_total

        recent_trades = sum(c.trades_executed for c in recent)

        return {
            "state": self._state.value,
            "running": self._running,
            "total_cycles": self._total_cycles,
            "total_errors": self._total_errors,
            "total_skipped": self._total_skipped,
            "consecutive_errors": self._consecutive_errors,
            "error_rate_pct": self.error_rate,
            "avg_latency_sec": round(avg_latency, 3),
            "peak_latency_sec": round(self._peak_latency, 3),
            "current_backoff_sec": round(self._current_backoff, 1),
            "uptime_seconds": round(uptime, 1),
            "uptime_human": format_duration(uptime),
            "last_cycle_at": (
                self._last_cycle_at.isoformat()
                if self._last_cycle_at
                else None
            ),
            "started_at": (
                self._started_at.isoformat()
                if self._started_at
                else None
            ),
            "interval_seconds": self.config.interval,
            "jitter_seconds": self.config.jitter_seconds,
            "market_aware": self.config.market_aware,
            "exchange_type": self.config.exchange_type,
            "max_consecutive_errors": self.config.max_consecutive_errors,
            # Sliding window
            "recent_window_size": recent_total,
            "recent_success_rate_pct": (
                round(recent_success / recent_total * 100, 1)
                if recent_total > 0
                else 100.0
            ),
            "recent_avg_latency_sec": round(recent_avg, 3),
            "recent_trades_executed": recent_trades,
        }

    def get_recent_cycles(
        self, last_n: int = 10
    ) -> List[Dict[str, Any]]:
        """
        Get recent cycle results.

        Args:
            last_n: Number of recent cycles

        Returns:
            List of cycle result dicts
        """
        cycles = list(self._cycle_history)[-last_n:]
        return [
            {
                "cycle": c.cycle_number,
                "time": c.started_at.strftime("%H:%M:%S"),
                "duration": f"{c.duration_seconds:.2f}s",
                "success": c.success,
                "trades": c.trades_executed,
                "error": c.error,
            }
            for c in cycles
        ]

    def _build_summary(self) -> str:
        """Build session summary string for shutdown log."""
        stats = self.get_stats()

        lines = [
            "═" * 50,
            "📊 SCHEDULER SESSION SUMMARY",
            "═" * 50,
            f"  Uptime:           {stats['uptime_human']}",
            f"  Total cycles:     {stats['total_cycles']}",
            f"  Total errors:     {stats['total_errors']}",
            f"  Skipped (mkt):    {stats['total_skipped']}",
            f"  Error rate:       {stats['error_rate_pct']}%",
            f"  Avg latency:      {stats['avg_latency_sec']}s",
            f"  Peak latency:     {stats['peak_latency_sec']}s",
            f"  Recent trades:    {stats['recent_trades_executed']}",
            "═" * 50,
        ]
        return "\n".join(lines)

    # ═════════════════════════════════════════════════════
    #  DUNDER METHODS
    # ═════════════════════════════════════════════════════

    def __repr__(self) -> str:
        icon = {
            SchedulerState.RUNNING: "🟢",
            SchedulerState.PAUSED: "🟡",
            SchedulerState.MARKET_CLOSED: "🌙",
            SchedulerState.ERROR_BACKOFF: "🟠",
            SchedulerState.STOPPED: "🔴",
            SchedulerState.IDLE: "⚪",
        }.get(self._state, "⚪")

        return (
            f"<TradeScheduler {icon} {self._state.value} | "
            f"cycles={self._total_cycles} | "
            f"errors={self._total_errors} | "
            f"interval={self.config.interval}s>"
        )

    def __str__(self) -> str:
        return self.__repr__()