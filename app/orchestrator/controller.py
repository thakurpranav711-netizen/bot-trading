# app/orchestrator/controller.py

"""
Autonomous AI Trading Controller — Production Grade

UPDATED: Realistic trader logic with controlled risk
FIXED: Trade execution now properly triggers when signals are valid
FIXED: Drawdown calculation corrected to prevent false emergency triggers
FIXED: LossGuard initialization parameters corrected
FIXED: TradeLimiter initialization parameters corrected
FIXED: State check now properly handles missing can_trade method
FIXED: Notification dispatch works in sync context (no event loop required)
"""

import asyncio
import json
import os
import queue
import threading
import requests
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any, List, Tuple

from app.utils.logger import get_logger
from app.market.analyzer import MarketAnalyzer, MarketState
from app.strategies.base import BaseStrategy
from app.risk.adaptive_risk import AdaptiveRiskManager
from app.risk.kill_switch import KillSwitch, KillSwitchMode
from app.risk.loss_guard import LossGuard
from app.risk.trade_limiter import TradeLimiter

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  DAILY LOSS LIMIT (₹1500)
# ═══════════════════════════════════════════════════════════════════

DAILY_LOSS_LIMIT_INR = 1500.0  # ₹1500 daily loss limit


# ═══════════════════════════════════════════════════════════════════
#  NOTIFICATION QUEUE (Thread-Safe)
# ═══════════════════════════════════════════════════════════════════

class NotificationQueue:
    """
    Thread-safe notification queue for dispatching messages
    from sync context to async context.
    """
    
    def __init__(self):
        self._queue: queue.Queue = queue.Queue()
        self._running: bool = False
        self._task: Optional[asyncio.Task] = None
    
    def put(self, message: str) -> None:
        """Add a message to the queue (thread-safe)."""
        self._queue.put(message)
    
    def get_all(self) -> List[str]:
        """Get all pending messages (non-blocking)."""
        messages = []
        while True:
            try:
                msg = self._queue.get_nowait()
                messages.append(msg)
            except queue.Empty:
                break
        return messages
    
    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return self._queue.empty()


class BotController:
    """
    Central Autonomous Trading Engine

    UPDATED for realistic trading with:
    - 60% probability auto-trade
    - 2-brain alignment requirement (enforced by strategy)
    - ₹1500 daily loss limit
    - Hourly market analysis notifications
    - Comprehensive trade notifications
    
    FIXED: 
    - Trade execution now properly triggers
    - Drawdown calculation corrected
    - LossGuard initialization parameters fixed
    - TradeLimiter initialization parameters fixed
    - State check properly handles missing can_trade method
    - Notifications work in sync context via queue
    """

    def __init__(
        self,
        state_manager,
        exchange,
        analyzers: Dict[str, MarketAnalyzer],
        strategy: BaseStrategy,
        notifier=None,
        mode: str = "PAPER",
        coins: List[str] = None,
        interval: int = 300,
        base_risk: float = 0.015,
        max_daily_drawdown: float = 0.05,
        emergency_drawdown: float = 0.15,
        max_exposure_pct: float = 0.30,
        max_consecutive_losses: int = 7,
        fee_pct: float = 0.001,
        slippage_pct: float = 0.0005,
        daily_loss_limit_inr: float = DAILY_LOSS_LIMIT_INR,
        usd_to_inr_rate: float = 83.0,
    ):
        # ── Core Components ───────────────────────────────────────
        self.state = state_manager
        self.exchange = exchange
        self.analyzers = analyzers
        self.strategy = strategy
        self.notifier = notifier

        # ── Configuration ─────────────────────────────────────────
        self.mode = mode.upper()
        self.coins = coins or list(analyzers.keys())
        self.interval = interval
        self.fee_pct = fee_pct
        self.slippage_pct = slippage_pct
        self.max_exposure_pct = max_exposure_pct
        self.emergency_drawdown = emergency_drawdown
        self.max_daily_drawdown = max_daily_drawdown

        # ── Daily Loss Limit (₹1500) ──────────────────────────────
        self.daily_loss_limit_inr = daily_loss_limit_inr
        self.usd_to_inr_rate = usd_to_inr_rate
        self.daily_loss_limit_usd = daily_loss_limit_inr / usd_to_inr_rate

        # ── Runtime State ─────────────────────────────────────────
        self.market_states: Dict[str, MarketState] = {}
        self.last_hourly_report: Optional[datetime] = None
        self.last_market_analysis: Optional[datetime] = None
        self.scheduler = None
        
        # ── Peak balance tracking for proper drawdown ─────────────
        self.session_peak_balance: float = 0.0

        # ── Notification Queue (Thread-Safe) ──────────────────────
        self._notification_queue = NotificationQueue()
        self._pending_notifications: List[Dict] = []

        # ── Brain 4 AI Cache ──────────────────────────────────────
        self._brain4_cache: Dict[str, Dict] = {}
        self._brain4_last_call: Optional[datetime] = None
        self._brain4_cache_ttl: int = interval
        self._groq_api_key = os.getenv("GROQ_API_KEY", "").strip()

        if self._groq_api_key:
            logger.info("🧠 Brain 4 AI enabled (GROQ API)")
        else:
            logger.warning("⚠️ Brain 4 AI disabled (no GROQ_API_KEY)")

        # ── Risk Management Components ────────────────────────────
        self.adaptive_risk = AdaptiveRiskManager(
            base_risk=base_risk,
            max_risk=0.03,
            min_risk=0.005,
        )

        # FIXED: Correct parameter names for LossGuard
        self.loss_guard = LossGuard(
            state_manager=state_manager,
            kill_switch=None,  # Will be set after kill_switch is created
            notifier=notifier,
            exchange=exchange,
            daily_loss_limit_inr=daily_loss_limit_inr,
            max_daily_loss_pct=max_daily_drawdown,
            emergency_drawdown_pct=emergency_drawdown,
            max_consecutive_losses=max_consecutive_losses,
        )

        self.kill_switch = KillSwitch(
            daily_loss_limit_inr=daily_loss_limit_inr,
            daily_drawdown_pct=max_daily_drawdown,
            emergency_drawdown_pct=emergency_drawdown,
            usd_to_inr=usd_to_inr_rate,
        )

        # Link kill_switch to loss_guard
        self.loss_guard.update_kill_switch(self.kill_switch)

        # FIXED: Correct parameter names for TradeLimiter
        self.trade_limiter = TradeLimiter(
            state_manager=state_manager,
            notifier=notifier,
            max_trades_per_day=15,
            max_trades_per_hour=5,
            min_trade_interval_sec=30,
            max_open_positions=3,
            daily_loss_limit_inr=daily_loss_limit_inr,
        )

        logger.info(
            f"🎮 Controller initialized | Mode={self.mode} | "
            f"Coins={len(self.coins)} | Interval={interval}s | "
            f"DailyLimit=₹{daily_loss_limit_inr:.0f} (${self.daily_loss_limit_usd:.2f})"
        )

    # ═════════════════════════════════════════════════════════════
    #  NOTIFICATION HELPERS (FIXED - Thread-Safe)
    # ═════════════════════════════════════════════════════════════

    def _queue_notification(self, message: str) -> None:
        """
        Queue a notification for async dispatch.
        Thread-safe — can be called from any context.
        """
        self._notification_queue.put(message)
        # Also store in pending list for immediate processing
        self._pending_notifications.append({
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })

    def _notify_sync(self, message: str) -> None:
        """
        Send a notification from sync context.
        Uses queue for thread-safe dispatch.
        """
        if not self.notifier:
            logger.debug("No notifier configured — skipping notification")
            return
        
        # Queue the notification
        self._queue_notification(message)
        
        # Try immediate dispatch if we have a running loop
        self._try_immediate_dispatch(message)

    def _try_immediate_dispatch(self, message: str) -> None:
        """
        Try to dispatch notification immediately if possible.
        Falls back to queue if no event loop available.
        """
        if not self.notifier:
            return
        
        # Method 1: Try to get running loop (works in async context)
        try:
            loop = asyncio.get_running_loop()
            # We're in async context — schedule directly
            if hasattr(self.notifier, 'send_message'):
                loop.create_task(self._safe_send(message))
            elif hasattr(self.notifier, 'send_custom'):
                loop.create_task(self._safe_send_custom(message))
            return
        except RuntimeError:
            pass  # No running loop — try other methods
        
        # Method 2: Try synchronous send if available
        if hasattr(self.notifier, 'send_message_sync'):
            try:
                self.notifier.send_message_sync(message)
                return
            except Exception as e:
                logger.debug(f"Sync send failed: {e}")
        
        # Method 3: Create new event loop in thread (last resort)
        try:
            self._send_in_new_loop(message)
        except Exception as e:
            logger.debug(f"New loop send failed: {e}")
            # Notification remains in queue for later processing

    def _send_in_new_loop(self, message: str) -> None:
        """Send notification using a new event loop in a thread."""
        def _send():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if hasattr(self.notifier, 'send_message'):
                    loop.run_until_complete(self.notifier.send_message(message))
                elif hasattr(self.notifier, 'send_custom'):
                    loop.run_until_complete(self.notifier.send_custom(message))
            finally:
                loop.close()
        
        # Run in a separate thread to avoid blocking
        thread = threading.Thread(target=_send, daemon=True)
        thread.start()
        # Don't wait — fire and forget

    async def _safe_send(self, message: str) -> None:
        """Safely send a message via notifier."""
        try:
            if hasattr(self.notifier, 'send_message'):
                await self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    async def _safe_send_custom(self, message: str) -> None:
        """Safely send a custom message via notifier."""
        try:
            if hasattr(self.notifier, 'send_custom'):
                await self.notifier.send_custom(message)
        except Exception as e:
            logger.error(f"Failed to send custom notification: {e}")

    async def process_pending_notifications(self) -> int:
        """
        Process all pending notifications from the queue.
        Call this from async context (e.g., scheduler).
        Returns number of notifications processed.
        """
        if not self.notifier:
            return 0
        
        messages = self._notification_queue.get_all()
        processed = 0
        
        for message in messages:
            try:
                if hasattr(self.notifier, 'send_message'):
                    await self.notifier.send_message(message)
                elif hasattr(self.notifier, 'send_custom'):
                    await self.notifier.send_custom(message)
                processed += 1
            except Exception as e:
                logger.error(f"Failed to process queued notification: {e}")
        
        # Clear pending list
        self._pending_notifications.clear()
        
        return processed

    def _notify_trade_entry(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        quantity: float,
        confidence: float,
        reason: str,
    ):
        """Send trade entry notification with all required fields."""
        probability_pct = round(confidence * 100, 1)

        message = (
            f"🚀 **TRADE ENTRY**\n\n"
            f"📊 **Symbol:** {symbol}\n"
            f"{'🟢' if direction == 'BUY' else '🔴'} **Direction:** {direction}\n"
            f"💰 **Entry Price:** ${entry_price:,.4f}\n"
            f"🎯 **Target (TP):** ${take_profit:,.4f}\n"
            f"🛑 **Stop Loss:** ${stop_loss:,.4f}\n"
            f"📦 **Quantity:** {quantity:.6f}\n"
            f"📈 **Probability:** {probability_pct}%\n"
            f"📝 **Reason:** {reason}\n"
            f"⏰ **Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        )
        self._notify_sync(message)

    def _notify_trade_exit(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        pnl_amount: float,
        pnl_pct: float,
        hold_duration_minutes: int,
        reason: str,
    ):
        """Send trade exit notification with all required fields."""
        pnl_emoji = "💰" if pnl_amount >= 0 else "💸"
        pnl_sign = "+" if pnl_amount >= 0 else ""

        # Convert to INR for display
        pnl_inr = pnl_amount * self.usd_to_inr_rate

        message = (
            f"🏁 **TRADE EXIT**\n\n"
            f"📊 **Symbol:** {symbol}\n"
            f"{'🟢' if direction == 'BUY' else '🔴'} **Direction:** {direction}\n"
            f"📥 **Entry Price:** ${entry_price:,.4f}\n"
            f"📤 **Exit Price:** ${exit_price:,.4f}\n"
            f"{pnl_emoji} **P&L:** {pnl_sign}${pnl_amount:,.4f} ({pnl_sign}{pnl_pct:.2f}%)\n"
            f"💵 **P&L (INR):** {pnl_sign}₹{pnl_inr:,.2f}\n"
            f"⏱️ **Duration:** {hold_duration_minutes} minutes\n"
            f"📝 **Reason:** {reason}\n"
            f"⏰ **Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        )
        self._notify_sync(message)

    def _notify_market_analysis(self):
        """
        Send hourly market analysis notification.

        Includes: Current trend, Important signals (RSI, trend, volatility),
        Potential trading opportunities.
        """
        if not self.market_states:
            return

        analyses = []

        for symbol, market in self.market_states.items():
            # Get AI prediction if available
            ai_signal = "N/A"
            ai_conf = 0
            if market.ai_prediction:
                ai_signal = market.ai_prediction.get("signal", "N/A")
                ai_conf = market.ai_prediction.get("confidence", 0)

            # Determine opportunity
            opportunity = "HOLD"
            rsi = market.rsi if market.rsi else 50
            if rsi < 35:
                opportunity = "🟢 Potential BUY (Oversold)"
            elif rsi > 65:
                opportunity = "🔴 Potential SELL (Overbought)"
            elif market.trend == "bullish":
                mom = getattr(market, 'momentum_strength', 0) or 0
                if mom > 0.001:
                    opportunity = "🟢 Bullish momentum"
            elif market.trend == "bearish":
                mom = getattr(market, 'momentum_strength', 0) or 0
                if mom > 0.001:
                    opportunity = "🔴 Bearish momentum"

            regime = getattr(market, 'regime', 'normal') or 'normal'
            vol_regime = getattr(market, 'volatility_regime', 'normal') or 'normal'

            analysis = (
                f"\n**{symbol}**\n"
                f"├ Price: ${market.price:,.4f}\n"
                f"├ Trend: {market.trend.upper()}\n"
                f"├ RSI: {rsi:.1f}\n"
                f"├ Regime: {regime}\n"
                f"├ Volatility: {vol_regime}\n"
                f"├ AI Signal: {ai_signal} ({ai_conf}%)\n"
                f"└ Opportunity: {opportunity}\n"
            )
            analyses.append(analysis)

        # Daily P&L summary
        daily_pnl = self.state.get("daily_pnl", 0)
        daily_pnl_inr = daily_pnl * self.usd_to_inr_rate
        remaining_loss_budget = self.daily_loss_limit_usd + daily_pnl if daily_pnl < 0 else self.daily_loss_limit_usd

        message = (
            f"📊 **HOURLY MARKET ANALYSIS**\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"{''.join(analyses)}\n"
            f"💼 **Daily Summary**\n"
            f"├ P&L: ${daily_pnl:+.4f} (₹{daily_pnl_inr:+.2f})\n"
            f"├ Loss Budget: ${remaining_loss_budget:.2f} remaining\n"
            f"└ Trades Today: {self.state.get('trades_today', 0)}\n"
        )
        self._notify_sync(message)

    def _notify_daily_limit_hit(self, total_loss_usd: float, total_loss_inr: float):
        """Send notification when daily loss limit is hit."""
        message = (
            f"🚨 **DAILY LOSS LIMIT REACHED**\n\n"
            f"❌ **Total Loss:** ${total_loss_usd:,.2f} (₹{total_loss_inr:,.2f})\n"
            f"🛑 **Limit:** ₹{self.daily_loss_limit_inr:,.0f}\n\n"
            f"**Actions Taken:**\n"
            f"✅ All open positions closed\n"
            f"✅ Trading stopped for today\n"
            f"✅ Will resume tomorrow automatically\n\n"
            f"⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        )
        self._notify_sync(message)

    # ═════════════════════════════════════════════════════════════
    #  LIFECYCLE HOOKS
    # ═════════════════════════════════════════════════════════════

    def on_start(self):
        """Called when bot starts."""
        balance = self.state.get("balance", 0.0)
        
        # Initialize peak balance tracking
        self.session_peak_balance = balance

        # Queue startup notification
        startup_msg = (
            f"🤖 **TRADING BOT STARTED**\n\n"
            f"📊 **Mode:** {self.mode}\n"
            f"💰 **Balance:** ${balance:,.2f}\n"
            f"🪙 **Coins:** {', '.join(self.coins)}\n"
            f"⏱️ **Interval:** {self.interval}s\n"
            f"🎯 **Daily Limit:** ₹{self.daily_loss_limit_inr:,.0f}\n"
            f"⏰ **Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        )
        self._notify_sync(startup_msg)

        # Initialize daily tracking
        self.state.set("start_of_day_balance", balance)
        self.state.set("daily_pnl", 0.0)
        self.state.set("trades_today", 0)
        self.state.set("daily_limit_hit", False)
        
        # CRITICAL FIX: Ensure bot is marked as active
        self.state.set("bot_active", True)
        
        # Initialize loss guard day
        self.loss_guard.check_daily_reset()

        logger.info(
            f"🤖 Bot started | Mode={self.mode} | "
            f"Balance=${balance:.2f} | Coins={self.coins} | "
            f"DailyLimit=₹{self.daily_loss_limit_inr:.0f}"
        )

    def on_stop(self, reason: str = "User request"):
        """Called when bot stops."""
        stop_msg = (
            f"🛑 **TRADING BOT STOPPED**\n\n"
            f"📝 **Reason:** {reason}\n"
            f"⏰ **Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        )
        self._notify_sync(stop_msg)
        logger.info(f"🛑 Bot stopped | Reason={reason}")

    # ═════════════════════════════════════════════════════════════
    #  STATE CHECK (FIXED)
    # ═════════════════════════════════════════════════════════════

    def _can_trade(self) -> Tuple[bool, str]:
        """
        Check if trading is allowed based on state.
        
        FIXED: Properly handles missing can_trade method.
        Returns (can_trade, reason).
        """
        # Check bot_active flag first
        if not self.state.get("bot_active", True):
            return False, "Bot is paused"
        
        # Check risk_locked flag
        if self.state.get("risk_locked", False):
            return False, "Risk locked"
        
        # Check daily limit
        if self.state.get("daily_limit_hit", False):
            return False, "Daily loss limit reached"
        
        # Check state manager's can_trade if available
        if hasattr(self.state, 'can_trade'):
            try:
                result = self.state.can_trade()
                if isinstance(result, tuple):
                    return result
                elif isinstance(result, bool):
                    if not result:
                        return False, "State manager blocked"
                    return True, "OK"
            except Exception as e:
                logger.warning(f"can_trade() check failed: {e}")
                # Don't block on error — allow trading
        
        return True, "OK"

    # ═════════════════════════════════════════════════════════════
    #  DRAWDOWN CALCULATION - FIXED
    # ═════════════════════════════════════════════════════════════

    def _calculate_drawdown(self) -> Dict[str, Any]:
        """
        Calculate drawdown correctly.
        
        FIXED: Uses realized P&L, not unrealized price movements.
        Only considers ACTUAL losses, not temporary position fluctuations.
        """
        daily_pnl = self.state.get("daily_pnl", 0.0)
        start_balance = self.state.get("start_of_day_balance", 0.0)
        current_balance = self.state.get("balance", 0.0)
        
        result = {
            "daily_pnl": daily_pnl,
            "daily_pnl_inr": daily_pnl * self.usd_to_inr_rate,
            "drawdown_pct": 0.0,
            "is_emergency": False,
            "is_daily_limit": False,
            "reason": None
        }
        
        # Only calculate drawdown if we have valid starting balance
        if start_balance <= 0:
            return result
        
        # Calculate drawdown based on REALIZED losses (daily_pnl)
        # NOT based on unrealized position value
        if daily_pnl < 0:
            realized_loss = abs(daily_pnl)
            realized_drawdown_pct = realized_loss / start_balance
            result["drawdown_pct"] = realized_drawdown_pct
            
            # Check daily loss limit (₹1500)
            loss_inr = realized_loss * self.usd_to_inr_rate
            if loss_inr >= self.daily_loss_limit_inr:
                result["is_daily_limit"] = True
                result["reason"] = f"Daily loss limit: ₹{loss_inr:.2f} >= ₹{self.daily_loss_limit_inr:.0f}"
            
            # Emergency drawdown only for LARGE realized losses
            # Must be at least $5 AND exceed percentage threshold
            if realized_loss >= 5.0 and realized_drawdown_pct >= self.emergency_drawdown:
                result["is_emergency"] = True
                result["reason"] = f"Emergency drawdown: {realized_drawdown_pct*100:.2f}% >= {self.emergency_drawdown*100:.0f}%"
        
        return result

    # ═════════════════════════════════════════════════════════════
    #  DAILY LOSS LIMIT CHECK
    # ═════════════════════════════════════════════════════════════

    def _check_daily_loss_limit(self) -> bool:
        """
        Check if daily loss limit (₹1500) has been reached.

        Returns:
            True if limit hit and trading should stop, False otherwise.
        """
        daily_pnl = self.state.get("daily_pnl", 0.0)

        # Only check if in loss
        if daily_pnl >= 0:
            return False

        loss_usd = abs(daily_pnl)
        loss_inr = loss_usd * self.usd_to_inr_rate

        if loss_inr >= self.daily_loss_limit_inr:
            logger.critical(
                f"🚨 DAILY LOSS LIMIT HIT | "
                f"Loss: ₹{loss_inr:.2f} >= Limit: ₹{self.daily_loss_limit_inr:.0f}"
            )

            # Mark limit as hit
            self.state.set("daily_limit_hit", True)

            # Close all positions
            self.force_exit_all("Daily loss limit reached (₹1500)")

            # Send notification
            self._notify_daily_limit_hit(loss_usd, loss_inr)

            # Activate kill switch for rest of day
            self.kill_switch.activate(
                mode=KillSwitchMode.SOFT,
                reason=f"Daily loss limit ₹{loss_inr:.0f}",
                source="daily_limit",
                loss_amount=loss_usd,
            )

            return True

        return False

    def _minutes_until_next_day(self) -> int:
        """Calculate minutes until next day (UTC midnight)."""
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return int((tomorrow - now).total_seconds() / 60)

    def _reset_daily_tracking(self):
        """Reset daily tracking (called at start of each day)."""
        balance = self.state.get("balance", 0.0)

        self.state.set("start_of_day_balance", balance)
        self.state.set("daily_pnl", 0.0)
        self.state.set("trades_today", 0)
        self.state.set("daily_limit_hit", False)
        
        # Reset peak balance
        self.session_peak_balance = balance

        # Deactivate kill switch if it was from daily limit
        if self.kill_switch.is_active():
            if self.kill_switch.should_resume_new_day():
                self.kill_switch.deactivate("New trading day")
        
        # Reset loss guard
        self.loss_guard.check_daily_reset()

        logger.info(f"🔄 Daily tracking reset | Balance: ${balance:.2f}")

    # ═════════════════════════════════════════════════════════════
    #  MAIN TRADING CYCLE
    # ═════════════════════════════════════════════════════════════

    def run_cycle(self) -> Dict[str, Any]:
        """
        Main trading cycle — called by scheduler every interval.

        Flow:
        1. Check daily reset (new day)
        2. Check daily loss limit (₹1500)
        3. Check all safety gates
        4. Analyze all coins
        5. Manage existing positions
        6. Evaluate new entries (60% probability + 2 brains = trade)
        7. Send hourly market analysis (every ~1 hour)
        8. Send reports
        
        Returns:
            Dict with cycle results (trades_executed, pnl, etc.)
        """
        logger.info("🔁 ══════════ Trading Cycle Start ══════════")
        cycle_start = datetime.now(timezone.utc)
        
        cycle_result = {
            "trades_executed": 0,
            "pnl": 0.0,
            "signals_generated": 0,
            "positions_managed": 0,
        }

        # ══════════════════════════════════════════════════════════
        #  CHECK: Daily Reset
        # ══════════════════════════════════════════════════════════

        last_reset = self.state.get("last_daily_reset", "")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        if last_reset != today:
            self._reset_daily_tracking()
            self.state.set("last_daily_reset", today)

        # ══════════════════════════════════════════════════════════
        #  CHECK: Daily Loss Limit (₹1500) - USING REALIZED P&L ONLY
        # ══════════════════════════════════════════════════════════

        if self.state.get("daily_limit_hit", False):
            logger.warning("🛑 Daily loss limit already hit — cycle skipped")
            return cycle_result

        if self._check_daily_loss_limit():
            return cycle_result

        # ══════════════════════════════════════════════════════════
        #  CHECK: Drawdown (FIXED - uses realized losses only)
        # ══════════════════════════════════════════════════════════
        
        drawdown_check = self._calculate_drawdown()
        
        if drawdown_check["is_emergency"]:
            logger.critical(f"🚨 {drawdown_check['reason']}")
            self.kill_switch.activate(
                mode=KillSwitchMode.HARD,
                reason=drawdown_check["reason"],
                source="drawdown_check",
            )
            self.force_exit_all("Emergency drawdown triggered")
            return cycle_result

        # ══════════════════════════════════════════════════════════
        #  GATE 1: Kill Switch
        # ══════════════════════════════════════════════════════════

        if self.kill_switch.is_active() and not self.kill_switch.can_trade():
            logger.warning("🛑 Kill switch active — cycle skipped")
            return cycle_result

        # ══════════════════════════════════════════════════════════
        #  GATE 2: Bot Active State (FIXED)
        # ══════════════════════════════════════════════════════════

        can_trade, reason = self._can_trade()
        if not can_trade:
            logger.debug(f"⏸️ Trading blocked: {reason}")
            # Still analyze markets and send reports
            self._analyze_all_coins()
            self._maybe_send_market_analysis()
            return cycle_result

        # ══════════════════════════════════════════════════════════
        #  GATE 3: Loss Guard
        # ══════════════════════════════════════════════════════════

        can_trade_lg, reason_lg = self.loss_guard.can_trade()
        if not can_trade_lg:
            logger.warning(f"🛑 Loss guard blocked: {reason_lg}")
            # Still send market analysis even if not trading
            self._maybe_send_market_analysis()
            return cycle_result

        # ══════════════════════════════════════════════════════════
        #  RESET EXCHANGE CACHE
        # ══════════════════════════════════════════════════════════

        if hasattr(self.exchange, 'begin_cycle'):
            self.exchange.begin_cycle()

        try:
            # ══════════════════════════════════════════════════════
            #  PHASE 1: ANALYZE ALL COINS
            # ══════════════════════════════════════════════════════

            logger.info("📊 Phase 1: Market Analysis")
            self._analyze_all_coins()

            # ══════════════════════════════════════════════════════
            #  PHASE 2: MANAGE EXISTING POSITIONS
            # ══════════════════════════════════════════════════════

            logger.info("📂 Phase 2: Position Management")

            positions = self.state.get_all_positions()

            if positions:
                logger.info(f"   Managing {len(positions)} open position(s)")

                for symbol, position in list(positions.items()):
                    market = self.market_states.get(symbol)
                    if market:
                        self._manage_position(market, position)
                        cycle_result["positions_managed"] += 1
                    else:
                        logger.warning(f"   ⚠️ No market data for {symbol}")

                # Re-check daily limit after position management
                if self._check_daily_loss_limit():
                    return cycle_result
            else:
                logger.info("   No open positions")

            # ══════════════════════════════════════════════════════
            #  PHASE 3: EVALUATE NEW ENTRIES
            # ══════════════════════════════════════════════════════

            logger.info("🎯 Phase 3: Entry Evaluation")

            # Refresh positions after phase 2
            positions = self.state.get_all_positions()

            for symbol in self.coins:
                if symbol in positions:
                    logger.debug(f"   {symbol}: Already have position — skip")
                    continue

                market = self.market_states.get(symbol)
                if market:
                    entry_result = self._evaluate_entry(market, symbol)
                    if entry_result:
                        cycle_result["trades_executed"] += 1
                        cycle_result["signals_generated"] += 1
                else:
                    logger.debug(f"   {symbol}: No market data — skip")

            # ══════════════════════════════════════════════════════
            #  PHASE 4: MARKET ANALYSIS & REPORTS
            # ══════════════════════════════════════════════════════

            logger.info("📱 Phase 4: Reports")

            self._send_cycle_summary()
            self._maybe_send_hourly_report()
            self._maybe_send_market_analysis()

            # ══════════════════════════════════════════════════════
            #  CYCLE COMPLETE
            # ══════════════════════════════════════════════════════

            elapsed = (datetime.now(timezone.utc) - cycle_start).total_seconds()
            logger.info(f"✅ Cycle complete in {elapsed:.2f}s")
            logger.info("🔁 ══════════ Trading Cycle End ══════════")

        except Exception as e:
            logger.exception(f"❌ Cycle error: {e}")
            error_msg = f"❌ **CYCLE ERROR**\n\n{str(e)}"
            self._notify_sync(error_msg)

        finally:
            if hasattr(self.exchange, 'end_cycle'):
                self.exchange.end_cycle()

        return cycle_result

    def _analyze_all_coins(self):
        """Analyze all configured coins."""
        for symbol in self.coins:
            self._analyze_coin(symbol)

    # ═════════════════════════════════════════════════════════════
    #  MARKET ANALYSIS
    # ═════════════════════════════════════════════════════════════

    def _analyze_coin(self, symbol: str):
        """Fetch market data and run full analysis for a coin."""
        try:
            analyzer = self.analyzers.get(symbol)
            if not analyzer:
                analyzer = MarketAnalyzer(symbol=symbol)
                self.analyzers[symbol] = analyzer

            price = self.exchange.get_price(symbol)
            candles = self.exchange.get_recent_candles(symbol, limit=150)

            if not price or price <= 0:
                logger.warning(f"   ⚠️ {symbol}: Invalid price ({price})")
                return

            if not candles or len(candles) < 30:
                logger.warning(
                    f"   ⚠️ {symbol}: Insufficient candles "
                    f"({len(candles) if candles else 0}/30 required)"
                )
                return

            market = analyzer.analyze({
                "price": price,
                "candles": candles,
            })

            # Inject Brain 4 AI prediction
            market.ai_prediction = self._get_brain4_prediction(market)

            self.market_states[symbol] = market

            ai_signal = "N/A"
            if market.ai_prediction:
                ai_signal = market.ai_prediction.get("signal", "N/A")

            rsi = market.rsi if market.rsi else 0
            regime = getattr(market, 'regime', 'normal') or 'normal'

            logger.info(
                f"   ✅ {symbol}: ${price:.2f} | "
                f"Trend={market.trend} | RSI={rsi:.1f} | "
                f"Regime={regime} | AI={ai_signal}"
            )

        except Exception as e:
            logger.error(f"   ❌ {symbol}: Analysis failed — {e}")

    # ═════════════════════════════════════════════════════════════
    #  BRAIN 4 — AI PREDICTION (GROQ API)
    # ═════════════════════════════════════════════════════════════

    def _get_brain4_prediction(self, market: MarketState) -> Optional[Dict]:
        """
        Brain 4: Real AI market analysis via GROQ API.
        """
        if not self._groq_api_key:
            return None

        now = datetime.now(timezone.utc)
        cache_key = market.symbol

        if self._brain4_last_call and cache_key in self._brain4_cache:
            elapsed = (now - self._brain4_last_call).total_seconds()
            if elapsed < self._brain4_cache_ttl:
                return self._brain4_cache[cache_key]

        prompt = self._build_brain4_prompt(market)

        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._groq_api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are an expert quantitative trading analyst. "
                                "Analyze the given market data and respond ONLY with valid JSON. "
                                "No explanation, no markdown, just JSON."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 256,
                    "temperature": 0.3,
                },
                timeout=15,
            )

            if response.status_code != 200:
                logger.warning(f"⚠️ Brain 4 API error: {response.status_code}")
                return None

            data = response.json()
            raw_text = data["choices"][0]["message"]["content"].strip()
            prediction = self._parse_brain4_response(raw_text)

            self._brain4_cache[cache_key] = prediction
            self._brain4_last_call = now

            logger.debug(
                f"🧠 Brain 4 | {market.symbol} | "
                f"{prediction['signal']} ({prediction['confidence']}%)"
            )

            return prediction

        except requests.exceptions.Timeout:
            logger.warning("⚠️ Brain 4: API timeout")
            return None
        except Exception as e:
            logger.warning(f"⚠️ Brain 4: {e}")
            return None

    def _build_brain4_prompt(self, market: MarketState) -> str:
        """Build structured prompt for AI analysis."""
        indicators = getattr(market, 'indicators', {}) or {}
        rsi = market.rsi if market.rsi else 50
        ema_20 = getattr(market, 'ema_20', 0) or 0
        ema_50 = getattr(market, 'ema_50', 0) or 0
        ema_200 = getattr(market, 'ema_200', 0) or 0
        macd_hist = getattr(market, 'macd_histogram', 0) or 0
        bb_pct = getattr(market, 'bb_percent_b', 0.5) or 0.5
        vol_spike = getattr(market, 'volume_spike', False)
        vol_pressure = getattr(market, 'volume_pressure', 0) or 0
        mom_strength = getattr(market, 'momentum_strength', 0) or 0
        trend_strength = getattr(market, 'trend_strength', 0) or 0
        structure_break = getattr(market, 'structure_break', False)
        sentiment_score = getattr(market, 'sentiment_score', 0) or 0
        sentiment = getattr(market, 'sentiment', 'neutral') or 'neutral'
        support = getattr(market, 'support_level', 0) or 0
        resistance = getattr(market, 'resistance_level', 0) or 0
        volatility_pct = getattr(market, 'volatility_pct', 0.02) or 0.02
        volatility_regime = getattr(market, 'volatility_regime', 'normal') or 'normal'
        regime = getattr(market, 'regime', 'normal') or 'normal'

        pattern_info = "None detected"
        chart_pattern = getattr(market, 'chart_pattern', None)
        if chart_pattern:
            p = chart_pattern
            pattern_info = (
                f"{p.get('pattern_name', 'Unknown')} "
                f"({p.get('signal', 'HOLD')} {p.get('confidence', 0)}%)"
            )

        return (
            f"Analyze this cryptocurrency market data and provide a trading signal.\n\n"
            f"MARKET DATA:\n"
            f"- Symbol: {market.symbol}\n"
            f"- Current Price: ${market.price:.4f}\n"
            f"- Trend: {market.trend}\n"
            f"- Market Regime: {regime}\n"
            f"- Volatility: {volatility_regime} ({volatility_pct * 100:.3f}%)\n\n"
            f"TECHNICAL INDICATORS:\n"
            f"- RSI (14): {rsi:.1f}\n"
            f"- EMA 20: ${ema_20:.4f}\n"
            f"- EMA 50: ${ema_50:.4f}\n"
            f"- EMA 200: ${ema_200:.4f}\n"
            f"- MACD Histogram: {macd_hist:.6f}\n"
            f"- MACD Cross: {indicators.get('macd_cross', 'None')}\n"
            f"- EMA Cross: {indicators.get('ema_cross', 'None')}\n"
            f"- Bollinger %B: {bb_pct:.3f}\n\n"
            f"MARKET DYNAMICS:\n"
            f"- Volume Spike: {vol_spike}\n"
            f"- Volume Pressure: {vol_pressure:.3f}\n"
            f"- Momentum Strength: {mom_strength:.6f}\n"
            f"- Trend Strength: {trend_strength:.6f}\n"
            f"- Structure Break: {structure_break}\n\n"
            f"SENTIMENT:\n"
            f"- Sentiment Score: {sentiment_score:.3f}\n"
            f"- Sentiment Category: {sentiment}\n\n"
            f"CHART PATTERN:\n"
            f"- {pattern_info}\n\n"
            f"SUPPORT/RESISTANCE:\n"
            f"- Support: ${support:.4f}\n"
            f"- Resistance: ${resistance:.4f}\n\n"
            f"Based on ALL of this data, provide your trading signal.\n"
            f"Respond with ONLY this JSON (no other text):\n"
            f'{{"signal": "BUY" | "SELL" | "HOLD", "confidence": <integer 0-100>, "reason": "<one sentence>"}}'
        )

    def _parse_brain4_response(self, raw_text: str) -> Dict:
        """Parse Brain 4 JSON response safely."""
        try:
            text = raw_text.strip()

            if text.startswith("```"):
                lines = text.split("\n")
                text = "\n".join(l for l in lines if not l.startswith("```")).strip()

            parsed = json.loads(text)

            signal = parsed.get("signal", "HOLD").upper()
            if signal not in ("BUY", "SELL", "HOLD"):
                signal = "HOLD"

            confidence = int(parsed.get("confidence", 0))
            confidence = max(0, min(100, confidence))

            return {
                "signal": signal,
                "confidence": confidence,
                "reason": parsed.get("reason", "AI analysis"),
            }

        except Exception as e:
            logger.debug(f"Brain 4 parse error: {e}")
            return {
                "signal": "HOLD",
                "confidence": 0,
                "reason": "Parse error — defaulting to HOLD",
            }

    # ═════════════════════════════════════════════════════════════
    #  ENTRY EVALUATION - FIXED
    # ═════════════════════════════════════════════════════════════

    def _evaluate_entry(self, market: MarketState, symbol: str) -> Optional[Dict]:
        """
        Evaluate potential trade entry for a symbol.
        
        Returns the executed trade info or None.
        """
        # ── Gate: Trade Limiter ───────────────────────────────────
        can_open, limit_reason = self.trade_limiter.can_open_trade(symbol)
        if not can_open:
            logger.debug(f"   {symbol}: Trade limited — {limit_reason}")
            return None

        # ── Update Strategy ───────────────────────────────────────
        self.strategy.symbol = symbol

        loss_streak = self.state.get("loss_streak", 0)
        if hasattr(self.strategy, 'set_loss_streak'):
            self.strategy.set_loss_streak(loss_streak)

        # ── Run 4-Brain Decision Engine (for logging) ─────────────
        decision = self._run_decision_engine(market, symbol)

        # Log decision for monitoring
        logger.info(
            f"   {symbol}: Decision={decision['final_signal']} "
            f"Conf={decision['confidence']} Trade={decision['trade']}"
        )

        # ── Get Strategy Signal ───────────────────────────────────
        entry_signal = self.strategy.should_enter(market)

        if not entry_signal:
            logger.debug(f"   {symbol}: Strategy declined entry")
            return None

        # ══════════════════════════════════════════════════════════
        #  STRATEGY RETURNED VALID SIGNAL - PROCEED TO TRADE
        # ══════════════════════════════════════════════════════════

        logger.info(f"   {symbol}: ✅ Strategy signal received — validating risk...")

        # ── Validate Risk ─────────────────────────────────────────
        price = market.price
        stop_loss = entry_signal.get("stop_loss", price * 0.98)
        take_profit = entry_signal.get("take_profit", price * 1.03)
        confidence = entry_signal.get("confidence", 0.6)

        est_qty = self._estimate_quantity(price, stop_loss)
        potential_loss = abs(price - stop_loss) * est_qty

        # Check against daily loss budget
        daily_pnl = self.state.get("daily_pnl", 0)
        remaining_budget_usd = self.daily_loss_limit_usd + daily_pnl if daily_pnl < 0 else self.daily_loss_limit_usd

        if potential_loss > remaining_budget_usd * 0.5:
            logger.warning(
                f"   {symbol}: Potential loss ${potential_loss:.2f} > "
                f"50% of remaining budget ${remaining_budget_usd:.2f} — skipped"
            )
            return None

        # ══════════════════════════════════════════════════════════
        #  ALL GATES PASSED — EXECUTE ENTRY
        # ══════════════════════════════════════════════════════════

        logger.info(f"   🚀 {symbol}: All gates passed — EXECUTING ENTRY!")

        # Ensure entry_signal has all required fields
        entry_signal["symbol"] = symbol
        entry_signal["entry_price"] = entry_signal.get("entry_price", price)
        entry_signal["stop_loss"] = stop_loss
        entry_signal["take_profit"] = take_profit
        entry_signal["confidence"] = confidence
        entry_signal["action"] = entry_signal.get("action", "BUY")

        # Execute the trade
        return self._execute_entry(entry_signal, market)

    # ═════════════════════════════════════════════════════════════
    #  4-BRAIN DECISION ENGINE
    # ═════════════════════════════════════════════════════════════

    def _run_decision_engine(
        self,
        market: MarketState,
        symbol: str,
    ) -> Dict:
        """
        Aggregate signals from all 4 analysis brains.
        """
        brains = self._collect_brain_signals(market)

        weighted_buy = 0.0
        weighted_sell = 0.0
        votes_buy = 0
        votes_sell = 0
        votes_hold = 0

        for brain in brains:
            signal = brain["signal"].upper()
            weight = brain["weight_pct"] / 100.0
            confidence = brain["confidence_pct"] / 100.0

            if signal == "BUY":
                weighted_buy += weight * confidence * 100
                votes_buy += 1
            elif signal == "SELL":
                weighted_sell += weight * confidence * 100
                votes_sell += 1
            else:
                votes_hold += 1

        if weighted_buy > weighted_sell and weighted_buy > 10:
            final_signal = "BUY"
        elif weighted_sell > weighted_buy and weighted_sell > 10:
            final_signal = "SELL"
        else:
            final_signal = "HOLD"

        score = max(weighted_buy, weighted_sell)
        if score >= 45:
            confidence = "HIGH"
        elif score >= 20:
            confidence = "MODERATE"
        else:
            confidence = "LOW"

        # MODERATE confidence now allows trades
        trade = (
            final_signal in ("BUY", "SELL") and
            confidence in ("HIGH", "MODERATE")
        )

        return {
            "final_signal": final_signal,
            "confidence": confidence,
            "trade": trade,
            "weighted_buy": weighted_buy,
            "weighted_sell": weighted_sell,
            "votes_buy": votes_buy,
            "votes_sell": votes_sell,
        }

    def _collect_brain_signals(self, market: MarketState) -> List[Dict]:
        """Collect signals from all 4 brains."""
        brains = []

        brain1 = self._brain_indicators(market)
        brain1["weight_pct"] = 35
        brains.append(brain1)

        brain2 = self._brain_sentiment(market)
        brain2["weight_pct"] = 15
        brains.append(brain2)

        brain3 = self._brain_chart(market)
        brain3["weight_pct"] = 25
        brains.append(brain3)

        brain4 = self._brain_ai(market)
        brain4["weight_pct"] = 25
        brains.append(brain4)

        return brains

    def _brain_indicators(self, market: MarketState) -> Dict:
        """Brain 1: Technical indicator signals."""
        indicators = getattr(market, 'indicators', {}) or {}
        score = 0
        factors = 0

        rsi = indicators.get("rsi") or market.rsi
        if rsi is not None:
            factors += 1
            if rsi < 40:
                score += 1
            elif rsi > 60:
                score -= 1

        macd = indicators.get("macd_cross")
        if macd:
            factors += 1
            if macd == "bullish":
                score += 1
            elif macd == "bearish":
                score -= 1

        ema = indicators.get("ema_cross")
        if ema:
            factors += 1
            if ema == "bullish":
                score += 1
            elif ema == "bearish":
                score -= 1

        bb = indicators.get("bb_position")
        if bb:
            factors += 1
            if bb == "oversold":
                score += 1
            elif bb == "overbought":
                score -= 1

        if factors == 0:
            confidence = 0
        else:
            confidence = int(abs(score) / factors * 100)

        if score > 0:
            signal = "BUY"
        elif score < 0:
            signal = "SELL"
        else:
            signal = "HOLD"

        return {
            "name": "Brain1 Indicators",
            "signal": signal,
            "confidence_pct": confidence,
        }

    def _brain_sentiment(self, market: MarketState) -> Dict:
        """Brain 2: Market sentiment signal."""
        sentiment = getattr(market, 'sentiment_score', 0) or 0

        if sentiment == 0:
            return {
                "name": "Brain2 Sentiment",
                "signal": "HOLD",
                "confidence_pct": 0,
            }

        confidence = int(min(abs(sentiment) * 100, 100))

        if sentiment > 0.05:
            signal = "BUY"
        elif sentiment < -0.05:
            signal = "SELL"
        else:
            signal = "HOLD"

        return {
            "name": "Brain2 Sentiment",
            "signal": signal,
            "confidence_pct": confidence,
        }

    def _brain_chart(self, market: MarketState) -> Dict:
        """Brain 3: Chart pattern signal."""
        pattern = getattr(market, 'chart_pattern', None)

        if not pattern:
            return {
                "name": "Brain3 Chart",
                "signal": "HOLD",
                "confidence_pct": 0,
            }

        return {
            "name": "Brain3 Chart",
            "signal": pattern.get("signal", "HOLD"),
            "confidence_pct": pattern.get("confidence", 0),
        }

    def _brain_ai(self, market: MarketState) -> Dict:
        """Brain 4: AI prediction signal."""
        ai = getattr(market, 'ai_prediction', None)

        if not ai:
            return {
                "name": "Brain4 AI",
                "signal": "HOLD",
                "confidence_pct": 0,
            }

        return {
            "name": "Brain4 AI",
            "signal": ai.get("signal", "HOLD"),
            "confidence_pct": ai.get("confidence", 0),
        }

    # ═════════════════════════════════════════════════════════════
    #  POSITION MANAGEMENT
    # ═════════════════════════════════════════════════════════════

    def _manage_position(self, market: MarketState, position: Dict):
        """
        Manage an existing open position.
        """
        symbol = position["symbol"]
        entry_price = position["entry_price"]
        stop_loss = position.get("stop_loss", entry_price * 0.98)
        take_profit = position.get("take_profit", entry_price * 1.03)
        quantity = position["quantity"]
        current_price = market.price

        action = position.get("action", "BUY").upper()
        is_long = action == "BUY"

        # Calculate current P&L
        if is_long:
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - current_price) / entry_price) * 100

        # ══════════════════════════════════════════════════════════
        #  CHECK STOP LOSS
        # ══════════════════════════════════════════════════════════

        if is_long:
            sl_hit = current_price <= stop_loss
        else:
            sl_hit = current_price >= stop_loss

        if sl_hit:
            logger.info(f"   🛑 {symbol}: Stop Loss hit @ ${current_price:.4f}")
            self._execute_exit(position, current_price, "STOP LOSS", is_long)
            return

        # ══════════════════════════════════════════════════════════
        #  CHECK TAKE PROFIT
        # ══════════════════════════════════════════════════════════

        if is_long:
            tp_hit = current_price >= take_profit
        else:
            tp_hit = current_price <= take_profit

        if tp_hit:
            logger.info(f"   🎯 {symbol}: Take Profit hit @ ${current_price:.4f}")
            self._execute_exit(position, current_price, "TAKE PROFIT", is_long)
            return

        # ══════════════════════════════════════════════════════════
        #  BREAK-EVEN & TRAILING STOP
        # ══════════════════════════════════════════════════════════

        if is_long:
            reward = take_profit - entry_price
            if reward > 0:
                progress = (current_price - entry_price) / reward
                
                # Break-even at 50% progress
                if progress >= 0.5 and stop_loss < entry_price:
                    new_sl = entry_price + (entry_price * 0.001)
                    self.state.update_position(symbol, {"stop_loss": new_sl})
                    logger.info(f"   ✅ {symbol}: Break-even activated | SL → ${new_sl:.2f}")
                    stop_loss = new_sl
                
                # Trailing stop at 80% progress
                if progress >= 0.8:
                    new_sl = current_price * 0.995
                    if new_sl > stop_loss:
                        self.state.update_position(symbol, {"stop_loss": new_sl})
                        logger.info(f"   📐 {symbol}: Trailing stop | SL → ${new_sl:.2f}")
        else:
            reward = entry_price - take_profit
            if reward > 0:
                progress = (entry_price - current_price) / reward
                
                if progress >= 0.5 and stop_loss > entry_price:
                    new_sl = entry_price - (entry_price * 0.001)
                    self.state.update_position(symbol, {"stop_loss": new_sl})
                    logger.info(f"   ✅ {symbol}: Break-even activated | SL → ${new_sl:.2f}")
                    stop_loss = new_sl
                
                if progress >= 0.8:
                    new_sl = current_price * 1.005
                    if new_sl < stop_loss:
                        self.state.update_position(symbol, {"stop_loss": new_sl})
                        logger.info(f"   📐 {symbol}: Trailing stop | SL → ${new_sl:.2f}")

        # ══════════════════════════════════════════════════════════
        #  STRATEGY EXIT SIGNAL
        # ══════════════════════════════════════════════════════════

        self.strategy.symbol = symbol

        if hasattr(self.strategy, 'should_exit'):
            exit_signal = self.strategy.should_exit(market, position)

            if exit_signal:
                reason = exit_signal.get("reason", "STRATEGY EXIT")
                logger.info(f"   🔴 {symbol}: Strategy exit signal — {reason}")
                self._execute_exit(position, current_price, reason, is_long)

    # ═════════════════════════════════════════════════════════════
    #  EXECUTE ENTRY
    # ═════════════════════════════════════════════════════════════

    def _execute_entry(self, signal: Dict, market: MarketState) -> Optional[Dict]:
        """Execute a trade entry with comprehensive notification."""
        symbol = signal["symbol"]
        direction = signal.get("action", "BUY").upper()
        entry_price = signal.get("entry_price", market.price)
        stop_loss = signal["stop_loss"]
        take_profit = signal["take_profit"]
        confidence = signal.get("confidence", 0.6)

        logger.info(
            f"   📝 {symbol}: Preparing {direction} order | "
            f"Entry=${entry_price:.2f} SL=${stop_loss:.2f} TP=${take_profit:.2f}"
        )

        qty = self._calculate_position_size(entry_price, stop_loss)

        if qty <= 0:
            logger.warning(f"   ⚠️ {symbol}: Position size is zero — entry skipped")
            return None

        balance = self.state.get("balance", 0)
        cost_estimate = qty * entry_price

        if cost_estimate > balance * 0.95:
            logger.warning(
                f"   ⚠️ {symbol}: Insufficient balance | "
                f"Need ${cost_estimate:.2f}, have ${balance:.2f}"
            )
            return None

        # Execute the trade
        if direction == "BUY":
            fill = self.exchange.buy(symbol=symbol, quantity=qty)
        else:
            fill = self.exchange.sell(symbol=symbol, quantity=qty)

        if not fill or fill.get("status") == "REJECTED":
            reason = fill.get("reason", "Unknown") if fill else "No response"
            logger.error(f"   ❌ {symbol}: Entry rejected — {reason}")
            return None

        fill_price = float(fill.get("price", entry_price))
        filled_qty = float(fill.get("quantity", qty))
        fee = float(fill.get("fee", fill_price * filled_qty * self.fee_pct))
        cost = fill_price * filled_qty

        self.state.adjust_balance(-(cost + fee))

        position_data = {
            "symbol": symbol,
            "action": direction,
            "entry_price": fill_price,
            "quantity": filled_qty,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "confidence": confidence,
            "strategy": signal.get("strategy", self.strategy.name if hasattr(self.strategy, 'name') else "scalping"),
            "fees_paid": fee,
            "entry_time": datetime.now(timezone.utc).isoformat(),
            "order_id": fill.get("order_id", ""),
            "mode": self.mode,
        }

        self.state.add_position(symbol, filled_qty, fill_price, position_data)

        self.trade_limiter.record_trade(symbol, direction)

        # Increment trades today
        trades_today = self.state.get("trades_today", 0)
        self.state.set("trades_today", trades_today + 1)

        # Record entry time in loss guard
        self.loss_guard.record_trade_entry(symbol)

        # ── Send comprehensive notification ───────────────────────
        self._notify_trade_entry(
            symbol=symbol,
            direction=direction,
            entry_price=fill_price,
            stop_loss=stop_loss,
            take_profit=take_profit,
            quantity=filled_qty,
            confidence=confidence,
            reason=signal.get("reason", direction),
        )

        logger.info(
            f"   ✅ ENTRY EXECUTED | {symbol} {direction} | "
            f"Qty={filled_qty:.6f} @ ${fill_price:.4f} | "
            f"Cost=${cost:.2f} | Fee=${fee:.4f} | "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f} | "
            f"Conf={confidence:.0%}"
        )

        return position_data

    # ═════════════════════════════════════════════════════════════
    #  EXECUTE EXIT
    # ═════════════════════════════════════════════════════════════

    def _execute_exit(
        self,
        position: Dict,
        exit_price: float,
        reason: str,
        is_long: bool,
    ):
        """Execute a position exit with comprehensive notification."""
        symbol = position["symbol"]
        quantity = position["quantity"]
        entry_price = position["entry_price"]
        direction = position.get("action", "BUY").upper()

        if is_long:
            fill = self.exchange.sell(symbol=symbol, quantity=quantity)
        else:
            fill = self.exchange.buy(symbol=symbol, quantity=quantity)

        if not fill or fill.get("status") == "REJECTED":
            reason_msg = fill.get("reason", "Unknown") if fill else "No response"
            logger.error(f"❌ {symbol}: Exit rejected — {reason_msg}")
            return

        actual_exit = float(fill.get("price", exit_price))
        filled_qty = float(fill.get("quantity", quantity))
        fee = float(fill.get("fee", actual_exit * filled_qty * self.fee_pct))

        if is_long:
            gross_pnl = (actual_exit - entry_price) * filled_qty
        else:
            gross_pnl = (entry_price - actual_exit) * filled_qty

        net_pnl = gross_pnl - fee
        pnl_pct = (net_pnl / (entry_price * filled_qty)) * 100 if entry_price > 0 else 0

        # Calculate hold duration
        entry_time = position.get("entry_time", "")
        hold_duration = 0
        if entry_time:
            try:
                open_time = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
                hold_duration = int(
                    (datetime.now(timezone.utc) - open_time).total_seconds() / 60
                )
            except Exception:
                pass

        # Close position in state
        if hasattr(self.state, 'close_position'):
            self.state.close_position(symbol, net_pnl, exit_price=actual_exit)
        else:
            self.state.remove_position(symbol)
            self.state.adjust_balance(actual_exit * filled_qty - fee)

        # Update daily P&L
        daily_pnl = self.state.get("daily_pnl", 0)
        self.state.set("daily_pnl", daily_pnl + net_pnl)

        # Record trade in loss guard
        self.loss_guard.record_trade(
            pnl=net_pnl,
            pnl_pct=pnl_pct,
            symbol=symbol,
            exit_price=actual_exit,
            entry_price=entry_price,
        )

        # Update loss/win streak
        if net_pnl < 0:
            loss_streak = self.state.get("loss_streak", 0)
            self.state.set("loss_streak", loss_streak + 1)
            self.state.set("win_streak", 0)
        else:
            win_streak = self.state.get("win_streak", 0)
            self.state.set("win_streak", win_streak + 1)
            self.state.set("loss_streak", 0)

        # Update strategy loss streak
        loss_streak = self.state.get("loss_streak", 0)
        if hasattr(self.strategy, 'set_loss_streak'):
            self.strategy.set_loss_streak(loss_streak)

        # ── Send comprehensive notification ───────────────────────
        self._notify_trade_exit(
            symbol=symbol,
            direction=direction,
            entry_price=entry_price,
            exit_price=actual_exit,
            quantity=filled_qty,
            pnl_amount=net_pnl,
            pnl_pct=pnl_pct,
            hold_duration_minutes=hold_duration,
            reason=reason,
        )

        pnl_emoji = "📈" if net_pnl >= 0 else "📉"
        side = "LONG" if is_long else "SHORT"

        logger.info(
            f"{pnl_emoji} EXIT EXECUTED | {symbol} {side} | "
            f"Qty={filled_qty:.6f} @ ${actual_exit:.4f} | "
            f"PnL=${net_pnl:+.4f} ({pnl_pct:+.2f}%) | "
            f"Duration={hold_duration}min | {reason}"
        )

    # ═════════════════════════════════════════════════════════════
    #  POSITION SIZING
    # ═════════════════════════════════════════════════════════════

    def _calculate_position_size(
        self,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """Calculate position size based on risk management."""
        balance = self.state.get("balance", 0)

        if balance <= 0:
            return 0.0

        market = None
        if self.market_states:
            market = list(self.market_states.values())[0]

        risk_pct = self.adaptive_risk.get_risk_percent(market)
        risk_amount = balance * risk_pct

        sl_distance = abs(entry_price - stop_loss)

        if sl_distance <= 0:
            logger.warning("⚠️ Stop loss distance is zero")
            return 0.0

        qty = risk_amount / sl_distance

        max_position_value = balance * self.max_exposure_pct
        if qty * entry_price > max_position_value:
            qty = max_position_value / entry_price
            logger.debug(f"Position capped by max exposure: {qty:.6f}")

        return round(qty, 8)

    def _estimate_quantity(
        self,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """Estimate quantity for pre-trade risk validation."""
        balance = self.state.get("balance", 0)
        risk_pct = self.adaptive_risk.base_risk
        risk_amount = balance * risk_pct
        sl_distance = abs(entry_price - stop_loss)

        if sl_distance <= 0:
            return 0.0

        return risk_amount / sl_distance

    # ═════════════════════════════════════════════════════════════
    #  REPORTS & SUMMARIES
    # ═════════════════════════════════════════════════════════════

    def _send_cycle_summary(self):
        """Send market summary after each analysis cycle."""
        if not self.market_states:
            return

        # Don't send cycle summaries via notification to avoid spam
        # Just log locally
        primary = self.coins[0] if self.coins else None
        market = self.market_states.get(primary)

        if market:
            rsi = market.rsi if market.rsi else 0
            regime = getattr(market, 'regime', 'normal') or 'normal'
            logger.debug(
                f"   Cycle summary: {market.symbol} ${market.price:.2f} "
                f"RSI={rsi:.1f} Regime={regime}"
            )

    def _maybe_send_hourly_report(self):
        """Send hourly performance report if due."""
        now = datetime.now(timezone.utc)

        if self.last_hourly_report:
            if now - self.last_hourly_report < timedelta(hours=1):
                return

        self.last_hourly_report = now

        positions = self.state.get_all_positions()
        balance = self.state.get("balance", 0)
        daily_pnl = self.state.get("daily_pnl", 0)
        daily_pnl_inr = daily_pnl * self.usd_to_inr_rate
        trades_today = self.state.get("trades_today", 0)

        message = (
            f"📊 **HOURLY REPORT**\n"
            f"⏰ {now.strftime('%Y-%m-%d %H:%M')} UTC\n\n"
            f"💰 **Balance:** ${balance:,.2f}\n"
            f"📈 **Daily P/L:** ${daily_pnl:+,.2f} (₹{daily_pnl_inr:+,.2f})\n"
            f"🔢 **Trades Today:** {trades_today}\n"
            f"📂 **Open Positions:** {len(positions)}\n"
            f"🏆 **Win Streak:** {self.state.get('win_streak', 0)}\n"
            f"📉 **Loss Streak:** {self.state.get('loss_streak', 0)}\n"
        )
        
        self._notify_sync(message)

    def _maybe_send_market_analysis(self):
        """Send market analysis notification approximately every hour."""
        now = datetime.now(timezone.utc)

        if self.last_market_analysis:
            if now - self.last_market_analysis < timedelta(hours=1):
                return

        self.last_market_analysis = now

        self._notify_market_analysis()

    # ═════════════════════════════════════════════════════════════
    #  STATUS & CONTROLS
    # ═════════════════════════════════════════════════════════════

        # ═════════════════════════════════════════════════════════════
    #  STATUS & CONTROLS
    # ═════════════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """Get comprehensive bot status."""
        positions = self.state.get_all_positions()
        balance = self.state.get("balance", 0)
        daily_pnl = self.state.get("daily_pnl", 0)
        daily_pnl_inr = daily_pnl * self.usd_to_inr_rate

        remaining_loss_budget = (
            self.daily_loss_limit_usd + daily_pnl
            if daily_pnl < 0
            else self.daily_loss_limit_usd
        )

        return {
            "running": self.state.get("bot_active", False),
            "mode": self.mode,
            "balance": balance,
            "daily_pnl_usd": daily_pnl,
            "daily_pnl_inr": daily_pnl_inr,
            "daily_loss_limit_inr": self.daily_loss_limit_inr,
            "remaining_loss_budget_usd": remaining_loss_budget,
            "daily_limit_hit": self.state.get("daily_limit_hit", False),
            "open_positions": len(positions),
            "positions": positions,
            "trades_today": self.state.get("trades_today", 0),
            "win_streak": self.state.get("win_streak", 0),
            "loss_streak": self.state.get("loss_streak", 0),
            "kill_switch_active": self.kill_switch.is_active(),
            "risk_locked": self.state.get("risk_locked", False),
            "loss_guard": self.loss_guard.get_status(),
            "trade_limiter": self.trade_limiter.get_status(),
        }

    def get_risk_report(self) -> Dict:
        """Get risk management report."""
        return self.adaptive_risk.get_report()

    # ═════════════════════════════════════════════════════════════
    #  MANUAL CONTROLS
    # ═════════════════════════════════════════════════════════════

    def pause(self):
        """Pause trading (soft stop)."""
        self.state.set("bot_active", False)
        self.state.set("pause_reason", "Manual pause")
        logger.info("⏸️ Bot paused")

    def resume(self):
        """Resume trading — unlocks ALL risk locks."""
        self.state.set("bot_active", True)
        self.state.set("risk_locked", False)
        self.state.set("pause_reason", None)
        self.state.set("daily_limit_hit", False)
        self.kill_switch.deactivate("Manual resume")
        logger.info("▶️ Bot resumed — all locks cleared")

    def unlock_risk(self, source: str = "manual") -> Dict:
        """Full risk system unlock."""
        self.state.set("bot_active", True)
        self.state.set("risk_locked", False)
        self.state.set("pause_reason", None)
        self.state.set("daily_limit_hit", False)

        self.kill_switch.deactivate(f"Unlock by {source}")

        logger.warning(f"🔓 FULL RISK UNLOCK by {source}")

        return {
            "unlocked": True,
            "source": source,
            "bot_active": True,
            "risk_locked": False,
        }

    def reset_risk_baseline(self, source: str = "manual") -> Dict:
        """Full risk baseline reset."""
        balance = self.state.get("balance", 0.0)

        if balance <= 0:
            logger.error("❌ Cannot reset — balance is zero")
            return {"reset": False, "reason": "Balance is zero"}

        old_initial = self.state.get("initial_balance", 0.0)

        # Reset baselines
        self.state.set("initial_balance", balance)
        self.state.set("start_of_day_balance", balance)
        self.state.set("daily_pnl", 0.0)
        self.state.set("daily_limit_hit", False)

        # Reset loss guard
        result = self.loss_guard.reset_baseline(source)

        self.kill_switch.deactivate(f"Baseline reset by {source}")

        logger.warning(
            f"🔄 RISK BASELINE RESET by {source} | "
            f"${old_initial:.2f} → ${balance:.2f}"
        )

        return result

    def force_exit_all(self, reason: str = "Manual exit"):
        """Force close all positions immediately."""
        positions = self.state.get_all_positions()

        if not positions:
            logger.info("No positions to close")
            return

        logger.warning(f"🚨 Force exiting {len(positions)} position(s)...")

        for symbol, position in list(positions.items()):
            try:
                price = self.exchange.get_price(symbol)
                is_long = position.get("action", "BUY").upper() == "BUY"
                self._execute_exit(position, price, reason, is_long)
            except Exception as e:
                logger.error(f"❌ Failed to exit {symbol}: {e}")

    def close_all_positions(self, reason: str = "Close all"):
        """
        Alias for force_exit_all — used by scheduler for daily loss limit.
        """
        self.force_exit_all(reason)

    def emergency_shutdown(self, reason: str):
        """Trigger emergency shutdown via kill switch."""
        start = self.state.get("start_of_day_balance", 0)
        current = self.state.get("balance", 0)
        loss_pct = ((start - current) / start * 100) if start > 0 else 0

        self.kill_switch.activate(
            mode=KillSwitchMode.HARD,
            reason=reason,
            source="controller",
            loss_amount=start - current,
        )

        # Send emergency notification
        emergency_msg = (
            f"🚨 **EMERGENCY SHUTDOWN**\n\n"
            f"❌ **Reason:** {reason}\n"
            f"📉 **Loss:** {loss_pct:.2f}%\n"
            f"⏰ **Time:** {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
        )
        self._notify_sync(emergency_msg)

        self.on_stop(reason=reason)

    # ═════════════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═════════════════════════════════════════════════════════════

    def __repr__(self) -> str:
        active = self.state.get("bot_active", False)
        locked = self.state.get("risk_locked", False)
        positions = len(self.state.get_all_positions())
        daily_limit_hit = self.state.get("daily_limit_hit", False)

        return (
            f"<BotController "
            f"mode={self.mode} | "
            f"active={active} | "
            f"locked={locked} | "
            f"daily_limit_hit={daily_limit_hit} | "
            f"positions={positions}>"
        )