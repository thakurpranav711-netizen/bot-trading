# app/orchestrator/scheduler.py

"""
Trade Scheduler — Production Grade v2.1 (Fixed)

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

NEW Features:
- Hourly market analysis notifications (combined single message)
- Daily loss limit monitoring (₹1500 limit)
- Automatic trading halt on daily loss limit
- Next day automatic resume
- UTC-safe date handling
- Semaphore-limited thread pool access
- Clean shutdown on STOPPING/STOPPED states

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
from datetime import datetime, timezone, timedelta
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
    DAILY_LOSS_LIMIT = "daily_loss_limit"  # Halted due to daily loss
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
    pnl: float = 0.0


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
    min_interval: int = 10                # Minimum allowed interval
    max_interval: int = 3600              # Maximum allowed interval
    
    # Hourly analysis settings
    hourly_analysis_enabled: bool = True  # Send hourly market updates
    hourly_analysis_interval: int = 3600  # Seconds (1 hour)
    
    # Daily loss limit settings
    daily_loss_limit_inr: float = 1500.0  # ₹1500 daily loss limit
    daily_loss_limit_enabled: bool = True # Enable daily loss protection
    
    # Thread pool concurrency limit
    max_thread_concurrency: int = 5       # Max concurrent to_thread calls


# ═════════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ═════════════════════════════════════════════════════════════════

def get_utc_today_str() -> str:
    """Get today's date string in UTC (YYYY-MM-DD)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_utc_date() -> datetime:
    """Get current UTC date (midnight)."""
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


# ═════════════════════════════════════════════════════════════════
#  TRADE SCHEDULER
# ═════════════════════════════════════════════════════════════════

class TradeScheduler:
    """
    Autonomous Trading Scheduler v2.1

    Runs the trading loop at configured intervals with:
    - Market awareness
    - Error budget with exponential backoff
    - Cycle metrics and sliding-window stats
    - Heartbeat monitoring
    - Dynamic interval adjustment
    - Graceful shutdown
    
    Features:
    - Hourly market analysis notifications (combined single message)
    - Daily loss limit (₹1500) monitoring
    - Automatic trading halt and next-day resume
    - UTC-safe date handling
    - Semaphore-limited thread pool access
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
        hourly_analysis_enabled: bool = True,
        daily_loss_limit_inr: float = 1500.0,
        max_thread_concurrency: int = 5,
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
            jitter_seconds: Random jitter (+/-) added to sleep interval
            adaptive_interval: Auto-adjust interval based on market conditions
            hourly_analysis_enabled: Send hourly market analysis
            daily_loss_limit_inr: Daily loss limit in INR
            max_thread_concurrency: Max concurrent asyncio.to_thread calls
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
            hourly_analysis_enabled=hourly_analysis_enabled,
            daily_loss_limit_inr=daily_loss_limit_inr,
            daily_loss_limit_enabled=daily_loss_limit_inr > 0,
            max_thread_concurrency=max(1, max_thread_concurrency),
        )

        # ── State ─────────────────────────────────────────────────
        self._state: SchedulerState = SchedulerState.IDLE
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
        self._consecutive_errors: int = 0
        self._current_backoff: float = 0.0

        # ── Semaphore for thread pool access ──────────────────────
        self._thread_semaphore: Optional[asyncio.Semaphore] = None

        # ── Statistics ────────────────────────────────────────────
        self._total_cycles: int = 0
        self._total_errors: int = 0
        self._total_skipped: int = 0
        self._total_latency: float = 0.0
        self._peak_latency: float = 0.0
        self._started_at: Optional[datetime] = None
        self._last_cycle_at: Optional[datetime] = None
        self._last_heartbeat: float = 0.0

        # ── Hourly Analysis Tracking ──────────────────────────────
        self._last_hourly_analysis: float = 0.0
        self._hourly_analysis_count: int = 0

        # ── Daily Loss Tracking ───────────────────────────────────
        self._daily_loss_halt_date: Optional[str] = None  # UTC date string
        self._daily_loss_notified: bool = False

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
        """Check if scheduler is actively running (not stopping/stopped)."""
        return self._running and self._state not in (
            SchedulerState.STOPPING,
            SchedulerState.STOPPED,
        )

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

    @property
    def is_daily_loss_halted(self) -> bool:
        """Check if trading is halted due to daily loss limit."""
        return self._state == SchedulerState.DAILY_LOSS_LIMIT

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

    def _should_continue(self) -> bool:
        """Check if the scheduler should continue running."""
        if not self._running:
            return False
        if self._state in (SchedulerState.STOPPING, SchedulerState.STOPPED):
            return False
        return True

    # ═════════════════════════════════════════════════════
    #  THREAD-SAFE EXECUTION
    # ═════════════════════════════════════════════════════

    async def _run_in_thread(self, func: Callable, *args, **kwargs) -> Any:
        """
        Run a sync function in thread pool with semaphore limiting.
        
        This prevents overwhelming the thread pool with too many
        concurrent blocking calls.
        """
        if self._thread_semaphore is None:
            self._thread_semaphore = asyncio.Semaphore(
                self.config.max_thread_concurrency
            )
        
        async with self._thread_semaphore:
            if kwargs:
                return await asyncio.to_thread(
                    lambda: func(*args, **kwargs)
                )
            return await asyncio.to_thread(func, *args)

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
        
        Also handles hourly analysis and daily loss limit checking.
        """
        self._running = True
        self._started_at = get_utc_now()
        self._last_heartbeat = time.monotonic()
        self._last_hourly_analysis = time.monotonic()
        
        # Initialize semaphore
        self._thread_semaphore = asyncio.Semaphore(
            self.config.max_thread_concurrency
        )
        
        self._set_state(SchedulerState.STARTING)

        logger.info(
            f"⏱️ Scheduler started | "
            f"interval={self.config.interval}s | "
            f"idle_poll={self.config.idle_poll}s | "
            f"market_aware={self.config.market_aware} | "
            f"exchange={self.config.exchange_type} | "
            f"jitter=±{self.config.jitter_seconds}s | "
            f"adaptive={self.config.adaptive_interval} | "
            f"hourly_analysis={self.config.hourly_analysis_enabled} | "
            f"daily_loss_limit=₹{self.config.daily_loss_limit_inr} | "
            f"max_threads={self.config.max_thread_concurrency}"
        )

        # ── Boot notification ─────────────────────────────────────
        await self._call_lifecycle("on_start")
        self._set_state(SchedulerState.RUNNING)

        try:
            while self._should_continue():
                # ── Check for new day (reset daily loss halt) ─────
                await self._check_daily_reset()

                # ── Early exit check ──────────────────────────────
                if not self._should_continue():
                    break

                # ── Heartbeat ─────────────────────────────────────
                await self._check_heartbeat()

                # ── Hourly Analysis ───────────────────────────────
                await self._check_hourly_analysis()

                # ── Check daily loss limit ────────────────────────
                if await self._check_daily_loss_limit():
                    await self._safe_sleep(self.config.idle_poll)
                    continue

                # ── Early exit check ──────────────────────────────
                if not self._should_continue():
                    break

                # ── Check if bot is active ────────────────────────
                bot_active = self._is_bot_active()

                if not bot_active:
                    self._set_state(SchedulerState.PAUSED)
                    await self._safe_sleep(self.config.idle_poll)
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
                        sleep_time = min(
                            60, mkt.next_change_seconds
                        ) if mkt.next_change_seconds > 0 else 60
                        await self._safe_sleep(sleep_time)
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
                    if not self._should_continue():
                        break

                # ── Run trading cycle ─────────────────────────────
                self._set_state(SchedulerState.RUNNING)
                await self._run_cycle_safe()

                if not self._should_continue():
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
        self._set_state(SchedulerState.STOPPING)

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
        
        # Clear daily loss halt if manually resumed
        if self._state == SchedulerState.DAILY_LOSS_LIMIT:
            self._daily_loss_halt_date = None
            self._daily_loss_notified = False
            logger.info("✅ Daily loss halt cleared manually")
        
        self._set_state(SchedulerState.RUNNING)
        logger.info("▶️ Scheduler resumed")

    # ═════════════════════════════════════════════════════
    #  DAILY LOSS LIMIT MANAGEMENT
    # ═════════════════════════════════════════════════════

    async def _check_daily_loss_limit(self) -> bool:
        """
        Check if daily loss limit (₹1500) has been reached.
        
        Returns:
            True if trading should be halted, False otherwise
        """
        if not self.config.daily_loss_limit_enabled:
            return False

        # Already halted today (using UTC date)
        today_utc = get_utc_today_str()
        if self._daily_loss_halt_date == today_utc:
            return True

        # Get daily loss from state manager
        daily_pnl_inr = self._get_daily_pnl_inr()
        
        if daily_pnl_inr <= -self.config.daily_loss_limit_inr:
            logger.critical(
                f"🚨 DAILY LOSS LIMIT REACHED: ₹{abs(daily_pnl_inr):,.2f} "
                f"(Limit: ₹{self.config.daily_loss_limit_inr:,.2f})"
            )
            
            # Close all positions
            await self._close_all_positions_on_limit()
            
            # Set halt state (using UTC date)
            self._daily_loss_halt_date = today_utc
            self._set_state(SchedulerState.DAILY_LOSS_LIMIT)
            
            # Send notification (only once)
            if not self._daily_loss_notified:
                await self._send_daily_loss_notification(daily_pnl_inr)
                self._daily_loss_notified = True
            
            return True
        
        return False

    def _get_daily_pnl_inr(self) -> float:
        """
        Get daily P/L in INR from state manager.
        
        Returns 0.0 if daily_pnl is missing or invalid.
        """
        try:
            if hasattr(self.controller, "state"):
                state = self.controller.state
                
                # Safely get daily PnL with default of 0.0
                daily_pnl = 0.0
                
                if hasattr(state, "get"):
                    raw_pnl = state.get("daily_pnl", None)
                    if raw_pnl is not None:
                        try:
                            daily_pnl = float(raw_pnl)
                        except (TypeError, ValueError):
                            logger.warning(
                                f"Invalid daily_pnl value: {raw_pnl}, using 0.0"
                            )
                            daily_pnl = 0.0
                elif isinstance(state, dict):
                    raw_pnl = state.get("daily_pnl", None)
                    if raw_pnl is not None:
                        try:
                            daily_pnl = float(raw_pnl)
                        except (TypeError, ValueError):
                            daily_pnl = 0.0
                
                # Check if we need to convert from USD to INR
                usd_to_inr = 83.0  # Default rate
                
                if hasattr(state, "get"):
                    usd_to_inr = state.get("usd_to_inr_rate", 83.0) or 83.0
                    pnl_currency = state.get("pnl_currency", "INR") or "INR"
                elif isinstance(state, dict):
                    usd_to_inr = state.get("usd_to_inr_rate", 83.0) or 83.0
                    pnl_currency = state.get("pnl_currency", "INR") or "INR"
                else:
                    pnl_currency = "INR"
                
                # If tracking in USD, convert
                if pnl_currency == "USD":
                    return daily_pnl * usd_to_inr
                
                return daily_pnl
            
            return 0.0
            
        except Exception as e:
            logger.error(f"Error getting daily PnL: {e}")
            return 0.0

    async def _close_all_positions_on_limit(self) -> None:
        """Close all open positions when daily loss limit is reached."""
        try:
            if hasattr(self.controller, "close_all_positions"):
                close_fn = self.controller.close_all_positions
                if asyncio.iscoroutinefunction(close_fn):
                    await close_fn(reason="Daily loss limit reached")
                else:
                    await self._run_in_thread(
                        close_fn, reason="Daily loss limit reached"
                    )
                logger.info("✅ All positions closed due to daily loss limit")
            else:
                logger.warning("Controller has no close_all_positions method")
        except Exception as e:
            logger.error(f"Error closing positions on limit: {e}")

    async def _send_daily_loss_notification(self, daily_pnl_inr: float) -> None:
        """Send notification about daily loss limit being reached."""
        now_utc = datetime.now(timezone.utc)
        
        message = (
            "🚨 **DAILY LOSS LIMIT REACHED**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📉 Daily Loss: `₹{abs(daily_pnl_inr):,.2f}`\n"
            f"🎯 Limit: `₹{self.config.daily_loss_limit_inr:,.2f}`\n"
            f"🕐 Time (UTC): `{now_utc.strftime('%Y-%m-%d %H:%M:%S')}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "⚠️ **Actions Taken:**\n"
            "• All open positions closed\n"
            "• Trading halted for today\n"
            "• Will resume automatically tomorrow (00:00 UTC)\n\n"
            "💡 Use `/resume` to manually override (not recommended)"
        )
        
        await self._send_notification(message)

    async def _check_daily_reset(self) -> None:
        """Check if it's a new day (UTC) and reset daily loss halt."""
        today_utc = get_utc_today_str()
        
        if self._daily_loss_halt_date and self._daily_loss_halt_date != today_utc:
            logger.info(
                f"🌅 New day detected (UTC) | Resetting daily loss halt | "
                f"Previous halt date: {self._daily_loss_halt_date} | "
                f"New date: {today_utc}"
            )
            self._daily_loss_halt_date = None
            self._daily_loss_notified = False
            
            # Reset state if we were halted
            if self._state == SchedulerState.DAILY_LOSS_LIMIT:
                self._set_state(SchedulerState.RUNNING)
                
                # Reset daily PnL in state manager
                if hasattr(self.controller, "state"):
                    state = self.controller.state
                    if hasattr(state, "set"):
                        state.set("daily_pnl", 0.0)
                    elif isinstance(state, dict):
                        state["daily_pnl"] = 0.0
                
                # Notify about resume
                await self._send_notification(
                    "🌅 **NEW TRADING DAY (UTC)**\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "✅ Daily loss limit reset\n"
                    "✅ Daily P/L reset to ₹0\n"
                    "✅ Trading resumed automatically\n"
                    f"📅 Date: `{today_utc}`\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n"
                    "💪 Good luck today!"
                )

    # ═════════════════════════════════════════════════════
    #  HOURLY ANALYSIS (Combined Single Message)
    # ═════════════════════════════════════════════════════

    async def _check_hourly_analysis(self) -> None:
        """
        Check if it's time to send hourly market analysis.
        Sends analysis even when not actively trading.
        """
        if not self.config.hourly_analysis_enabled:
            return
        
        if not self._should_continue():
            return

        now = time.monotonic()
        elapsed = now - self._last_hourly_analysis

        if elapsed >= self.config.hourly_analysis_interval:
            self._last_hourly_analysis = now
            await self._send_hourly_analysis()

    async def _send_hourly_analysis(self) -> None:
        """
        Generate and send hourly market analysis as a SINGLE combined message.
        Avoids Telegram spam by combining all coin analyses.
        """
        try:
            logger.info("📊 Generating hourly market analysis...")
            
            if not self._should_continue():
                return
            
            # Get analyzers and market data from controller
            if not hasattr(self.controller, "analyzers"):
                logger.warning("Controller has no analyzers - skipping hourly analysis")
                return

            analyzers = self.controller.analyzers
            if not analyzers:
                return

            # Collect all reports
            report_sections: List[str] = []
            symbols_analyzed: List[str] = []
            
            for symbol, analyzer in analyzers.items():
                if not self._should_continue():
                    return
                    
                try:
                    # Get market data
                    market_data = await self._get_market_data(symbol)
                    if not market_data:
                        continue

                    # Analyze market
                    market_state = analyzer.analyze(market_data)
                    
                    # Generate hourly report section
                    if hasattr(analyzer, "generate_hourly_report"):
                        report = analyzer.generate_hourly_report(market_state)
                        if hasattr(report, "format_section"):
                            section = report.format_section()
                        elif hasattr(report, "format_notification"):
                            section = report.format_notification()
                        else:
                            section = str(report)
                    else:
                        # Fallback: create basic section
                        section = self._create_basic_analysis_section(
                            symbol, market_state
                        )
                    
                    report_sections.append(section)
                    symbols_analyzed.append(symbol)
                    
                except Exception as e:
                    logger.error(f"Error analyzing {symbol} for hourly report: {e}")
                    continue

            # Build and send SINGLE combined message
            if report_sections:
                now_utc = datetime.now(timezone.utc)
                
                combined_message = (
                    "📊 **HOURLY MARKET ANALYSIS**\n"
                    f"🕐 `{now_utc.strftime('%Y-%m-%d %H:%M UTC')}`\n"
                    "━━━━━━━━━━━━━━━━━━━━━━\n\n"
                )
                
                combined_message += "\n\n".join(report_sections)
                
                combined_message += (
                    "\n\n━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📈 Symbols: {', '.join(symbols_analyzed)}\n"
                    f"⏰ Next update in ~1 hour"
                )
                
                await self._send_notification(combined_message)
                
                self._hourly_analysis_count += 1
                logger.info(
                    f"✅ Hourly analysis sent | "
                    f"Symbols: {len(report_sections)} | "
                    f"Total sent: {self._hourly_analysis_count}"
                )
            else:
                logger.warning("No hourly analysis reports generated")

        except Exception as e:
            logger.error(f"Error in hourly analysis: {e}")

    def _create_basic_analysis_section(
        self, symbol: str, market_state: Any
    ) -> str:
        """Create a basic analysis section when analyzer lacks format method."""
        try:
            # Extract common attributes
            trend = getattr(market_state, "trend", "Unknown")
            rsi = getattr(market_state, "rsi", None)
            price = getattr(market_state, "price", None)
            volatility = getattr(market_state, "volatility", None)
            
            section = f"**{symbol}**\n"
            
            if price:
                section += f"💰 Price: `${price:,.4f}`\n"
            if trend:
                section += f"📈 Trend: `{trend}`\n"
            if rsi:
                section += f"📊 RSI: `{rsi:.1f}`\n"
            if volatility:
                section += f"📉 Volatility: `{volatility:.2%}`\n"
            
            return section
            
        except Exception:
            return f"**{symbol}**: Analysis unavailable\n"

    async def _get_market_data(self, symbol: str) -> Optional[Dict]:
        """Get market data for a symbol from the exchange."""
        try:
            if not self._should_continue():
                return None
                
            if hasattr(self.controller, "exchange"):
                exchange = self.controller.exchange
                
                # Try to get candles
                if hasattr(exchange, "fetch_ohlcv"):
                    fetch_fn = exchange.fetch_ohlcv
                    
                    if asyncio.iscoroutinefunction(fetch_fn):
                        candles = await fetch_fn(
                            symbol, timeframe="5m", limit=100
                        )
                    else:
                        candles = await self._run_in_thread(
                            fetch_fn, symbol, "5m", 100
                        )
                    
                    if candles:
                        # Get current price
                        last_candle = candles[-1]
                        if isinstance(last_candle, dict):
                            price = last_candle.get("close", 0)
                        else:
                            price = last_candle[4] if len(last_candle) > 4 else 0
                        
                        # Format candles if needed
                        if candles and isinstance(candles[0], list):
                            formatted_candles = [
                                {
                                    "timestamp": c[0],
                                    "open": c[1],
                                    "high": c[2],
                                    "low": c[3],
                                    "close": c[4],
                                    "volume": c[5] if len(c) > 5 else 0,
                                }
                                for c in candles
                            ]
                        else:
                            formatted_candles = candles
                        
                        return {
                            "price": price,
                            "candles": formatted_candles,
                        }
            
            return None
            
        except Exception as e:
            logger.error(f"Error fetching market data for {symbol}: {e}")
            return None

    async def _send_notification(self, message: str) -> None:
        """Send notification via controller's notifier."""
        try:
            if not self._should_continue():
                return
                
            notifier = getattr(self.controller, "notifier", None)
            if not notifier:
                logger.debug("No notifier available")
                return

            # Try different notification methods
            send_fn = (
                getattr(notifier, "send_message", None) or
                getattr(notifier, "notify", None) or
                getattr(notifier, "send", None)
            )

            if send_fn:
                if asyncio.iscoroutinefunction(send_fn):
                    await send_fn(message)
                else:
                    await self._run_in_thread(send_fn, message)
            else:
                logger.warning("Notifier has no send method")
                
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    # ═════════════════════════════════════════════════════
    #  CYCLE EXECUTION
    # ═════════════════════════════════════════════════════

    async def _run_cycle_safe(self) -> Optional[CycleResult]:
        """
        Execute one trading cycle with full error isolation.
        """
        if not self._should_continue():
            return None
            
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
                cycle_result = await self._run_in_thread(run_cycle)

            # ── Extract trade count and PnL from result ───────────
            if isinstance(cycle_result, dict):
                result.trades_executed = cycle_result.get(
                    "trades_executed", 0
                )
                result.pnl = cycle_result.get("pnl", 0.0)

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
            pnl_str = (
                f" | PnL=₹{result.pnl:+,.2f}"
                if result.pnl != 0
                else ""
            )
            logger.debug(
                f"{status} Cycle #{cycle_num} done in "
                f"{elapsed:.2f}s{trades}{pnl_str}"
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
                    await self._run_in_thread(deactivate)

        await self._notify_error(
            "🚨 Emergency Stop",
            f"{self._consecutive_errors} consecutive cycle errors. "
            f"Bot deactivated. Manual intervention required.",
        )

        self._running = False
        self._set_state(SchedulerState.STOPPING)

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
                    await self._run_in_thread(
                        lambda: func(**kwargs)
                    )
                else:
                    await self._run_in_thread(func)

            logger.debug(f"✅ Lifecycle {method}() completed")

        except Exception as e:
            logger.exception(f"❌ Lifecycle {method}() failed: {e}")

    # ═════════════════════════════════════════════════════
    #  SLEEP & TIMING
    # ═════════════════════════════════════════════════════

    def _compute_sleep_interval(self) -> int:
        """
        Compute sleep duration with symmetric jitter (+/-).

        Returns:
            Sleep duration in seconds
        """
        base = self.config.interval

        if self.config.jitter_seconds > 0:
            # Symmetric jitter: can be positive OR negative
            jitter = random.uniform(
                -self.config.jitter_seconds,
                self.config.jitter_seconds
            )
            base = int(base + jitter)

        return max(self.config.min_interval, base)

    async def _safe_sleep(self, seconds: float) -> None:
        """Simple sleep that respects shutdown state."""
        if not self._should_continue():
            return
        await asyncio.sleep(min(seconds, 1.0))

    async def _interruptible_sleep(self, seconds: int) -> None:
        """
        Sleep in 1-second chunks for quick interrupt response.

        Checks:
        - stop() calls (self._running)
        - STOPPING/STOPPED states
        - bot_active state changes
        - Hourly analysis timing
        - Daily loss limit
        """
        for _ in range(max(1, seconds)):
            if not self._should_continue():
                break

            if not self._is_bot_active():
                break
            
            # Check for hourly analysis during sleep
            if self.config.hourly_analysis_enabled:
                now = time.monotonic()
                if now - self._last_hourly_analysis >= self.config.hourly_analysis_interval:
                    break  # Exit sleep to send hourly analysis

            await asyncio.sleep(1)

    def _is_bot_active(self) -> bool:
        """Check if bot is active via controller state."""
        try:
            # Check scheduler state first
            if self._state in (
                SchedulerState.DAILY_LOSS_LIMIT,
                SchedulerState.STOPPING,
                SchedulerState.STOPPED,
            ):
                return False
            
            if hasattr(self.controller, "state"):
                state = self.controller.state
                if hasattr(state, "get"):
                    return bool(state.get("bot_active", True))
                if isinstance(state, dict):
                    return bool(state.get("bot_active", True))
            return True
        except Exception:
            return True

    # ═════════════════════════════════════════════════════
    #  HEARTBEAT
    # ═════════════════════════════════════════════════════

    async def _check_heartbeat(self) -> None:
        """Log periodic heartbeat to prove liveness."""
        if not self._should_continue():
            return
            
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
                f"uptime={format_duration(stats['uptime_seconds'])} | "
                f"hourly_reports={self._hourly_analysis_count}"
            )

    # ═════════════════════════════════════════════════════
    #  NOTIFICATIONS
    # ═════════════════════════════════════════════════════

    async def _notify_error(
        self, context: str, error: str
    ) -> None:
        """Send error notification via controller's notifier."""
        if not self._should_continue():
            return
            
        notifier = getattr(self.controller, "notifier", None)
        if not notifier:
            return

        try:
            send_error = getattr(notifier, "send_error", None)
            if send_error:
                if asyncio.iscoroutinefunction(send_error):
                    await send_error(context=context, error=error)
                else:
                    await self._run_in_thread(
                        send_error, context, error
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
        """
        if not self._should_continue():
            logger.warning("⚠️ Cannot force cycle — scheduler not running")
            return None
        
        # Check if halted due to daily loss
        if self._state == SchedulerState.DAILY_LOSS_LIMIT:
            logger.warning("⚠️ Cannot force cycle — daily loss limit reached")
            return None

        logger.info("⚡ Manual cycle triggered")
        return await self._run_cycle_safe()

    async def force_hourly_analysis(self) -> None:
        """
        Immediately trigger hourly analysis outside normal schedule.
        """
        logger.info("⚡ Manual hourly analysis triggered")
        await self._send_hourly_analysis()

    def update_interval(self, new_interval: int) -> None:
        """
        Update cycle interval at runtime (bounded).
        """
        old = self.config.interval
        self.config.interval = max(
            self.config.min_interval,
            min(new_interval, self.config.max_interval),
        )
        logger.info(
            f"⏱️ Interval updated: {old}s → {self.config.interval}s"
        )

    def update_daily_loss_limit(self, new_limit: float) -> None:
        """
        Update daily loss limit at runtime.
        """
        old = self.config.daily_loss_limit_inr
        self.config.daily_loss_limit_inr = max(0.0, new_limit)
        self.config.daily_loss_limit_enabled = new_limit > 0
        logger.info(
            f"💰 Daily loss limit updated: ₹{old:,.2f} → "
            f"₹{self.config.daily_loss_limit_inr:,.2f}"
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

    def set_hourly_analysis(self, enabled: bool) -> None:
        """Toggle hourly analysis notifications."""
        self.config.hourly_analysis_enabled = enabled
        logger.info(f"📊 Hourly analysis: {'ON' if enabled else 'OFF'}")

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
        recent_pnl = sum(c.pnl for c in recent)

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
            "recent_pnl": round(recent_pnl, 2),
            
            # Hourly analysis stats
            "hourly_analysis_enabled": self.config.hourly_analysis_enabled,
            "hourly_analysis_count": self._hourly_analysis_count,
            
            # Daily loss limit stats
            "daily_loss_limit_enabled": self.config.daily_loss_limit_enabled,
            "daily_loss_limit_inr": self.config.daily_loss_limit_inr,
            "daily_loss_halted": self._state == SchedulerState.DAILY_LOSS_LIMIT,
            "daily_loss_halt_date": self._daily_loss_halt_date,
        }

    def get_recent_cycles(
        self, last_n: int = 10
    ) -> List[Dict[str, Any]]:
        """Get recent cycle results."""
        cycles = list(self._cycle_history)[-last_n:]
        return [
            {
                "cycle": c.cycle_number,
                "time": c.started_at.strftime("%H:%M:%S"),
                "duration": f"{c.duration_seconds:.2f}s",
                "success": c.success,
                "trades": c.trades_executed,
                "pnl": f"₹{c.pnl:+,.2f}" if c.pnl != 0 else "-",
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
            f"  Uptime:            {stats['uptime_human']}",
            f"  Total cycles:      {stats['total_cycles']}",
            f"  Total errors:      {stats['total_errors']}",
            f"  Skipped (mkt):     {stats['total_skipped']}",
            f"  Error rate:        {stats['error_rate_pct']}%",
            f"  Avg latency:       {stats['avg_latency_sec']}s",
            f"  Peak latency:      {stats['peak_latency_sec']}s",
            f"  Recent trades:     {stats['recent_trades_executed']}",
            f"  Recent PnL:        ₹{stats['recent_pnl']:+,.2f}",
            f"  Hourly reports:    {stats['hourly_analysis_count']}",
            f"  Daily loss halted: {stats['daily_loss_halted']}",
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
            SchedulerState.DAILY_LOSS_LIMIT: "🔴",
            SchedulerState.STOPPING: "🟠",
            SchedulerState.STOPPED: "🔴",
            SchedulerState.IDLE: "⚪",
            SchedulerState.STARTING: "🔵",
        }.get(self._state, "⚪")

        return (
            f"<TradeScheduler {icon} {self._state.value} | "
            f"cycles={self._total_cycles} | "
            f"errors={self._total_errors} | "
            f"interval={self.config.interval}s | "
            f"daily_limit=₹{self.config.daily_loss_limit_inr}>"
        )

    def __str__(self) -> str:
        return self.__repr__()