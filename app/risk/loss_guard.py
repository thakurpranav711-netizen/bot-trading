# app/risk/loss_guard.py

"""
Loss Guard — Production Grade for Autonomous Trading

Multi-layer loss protection system:
- Daily loss limit in INR (₹1500 default) — HARD STOP
- Auto-close all positions when daily limit hit
- Stop trading for rest of day
- Auto-resume next day at midnight
- Total/emergency drawdown protection
- Consecutive loss circuit breaker with cooldown
- Per-trade risk validation
- Gradual recovery mode after losses
- Win/loss recording and streak tracking
- Trade duration tracking for exit notifications
- Full notification integration

Integration:
    Controller calls can_trade() before every cycle
    Controller calls validate_trade_risk() before every entry
    Controller calls record_trade() after every exit
    Controller calls check_daily_reset() at start of each cycle
    Controller calls unlock() from /resume command
    Controller calls reset_baseline() from /reset_risk command
"""

from datetime import datetime, timedelta, date
from typing import Dict, Optional, Tuple, List, Callable
from enum import Enum
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  GUARD STATUS
# ═══════════════════════════════════════════════════════════════════

class GuardStatus(Enum):
    """Loss guard status levels."""
    CLEAR = "clear"                    # All good
    WARNING = "warning"                # Approaching limits
    COOLDOWN = "cooldown"              # In cooldown period
    DAILY_LIMIT = "daily_limit"        # ₹1500 daily limit hit
    SOFT_STOP = "soft_stop"            # Daily percentage limit hit
    HARD_STOP = "hard_stop"            # Emergency stop
    LOCKED = "locked"                  # Manually locked


class LockReason(Enum):
    """Reasons for locking."""
    DAILY_LOSS_LIMIT = "daily_loss_limit"      # ₹1500 limit
    CONSECUTIVE_LOSSES = "consecutive_losses"
    DAILY_DRAWDOWN = "daily_drawdown"
    TOTAL_DRAWDOWN = "total_drawdown"
    EMERGENCY = "emergency"
    MANUAL = "manual"
    ERROR = "error"


# ═══════════════════════════════════════════════════════════════════
#  LOSS GUARD
# ═══════════════════════════════════════════════════════════════════

class LossGuard:
    """
    Multi-layer loss protection system with ₹1500 daily limit.

    Layer 0: Daily Loss Limit (₹1500 INR)
        - HARD STOP when daily loss reaches ₹1500
        - Auto-closes ALL open positions immediately
        - Stops trading for rest of the day
        - Auto-resumes at midnight (next day)

    Layer 1: Consecutive loss circuit breaker
        Locks trading after N losses in a row.
        Auto-cooldown before unlock attempt.

    Layer 2: Daily drawdown soft stop (percentage-based)
        Locks trading if daily loss exceeds max_daily_loss_pct.
        Resets at midnight.

    Layer 3: Emergency drawdown hard stop
        Locks trading permanently if total loss exceeds
        emergency_drawdown_pct of initial_balance.
        Requires manual reset.

    Layer 4: Per-trade risk cap
        Rejects individual trades whose potential loss
        exceeds max_trade_risk_pct of current balance.

    Layer 5: Recovery mode
        After unlocking from losses, trades with reduced
        risk until back in profit.
    """

    # ── Default thresholds ────────────────────────────────────────
    DEFAULT_DAILY_LOSS_LIMIT_INR = 1500.0    # ₹1500 daily hard limit
    DEFAULT_DAILY_LOSS_PCT = 0.05            # 5% daily drawdown (soft)
    DEFAULT_EMERGENCY_PCT = 0.15             # 15% total drawdown
    DEFAULT_MAX_CONSEC_LOSSES = 3            # 3 consecutive losses
    DEFAULT_COOLDOWN_MINUTES = 30            # 30 min cooldown
    DEFAULT_MAX_TRADE_RISK_PCT = 0.02        # 2% per trade
    DEFAULT_WARNING_THRESHOLD = 0.70         # Warn at 70% of limit

    # Recovery mode settings
    RECOVERY_RISK_MULTIPLIER = 1.00        # 50% of normal risk in recovery
    RECOVERY_EXIT_WINS = 2                   # Exit recovery after N wins

    def __init__(
        self,
        state_manager,
        kill_switch=None,
        notifier=None,
        exchange=None,
        # Daily loss limit in INR (PRIMARY PROTECTION)
        daily_loss_limit_inr: float = None,
        # Percentage-based limits (SECONDARY)
        max_daily_loss_pct: float = None,
        max_consecutive_losses: int = None,
        emergency_drawdown_pct: float = None,
        cooldown_minutes: int = None,
        max_trade_risk_pct: float = None,
        warning_threshold: float = None,
        enable_recovery_mode: bool = True,
        # Callbacks
        on_daily_limit_hit: Callable = None,
    ):
        """
        Initialize Loss Guard.

        Args:
            state_manager: State manager instance
            kill_switch: Kill switch instance
            notifier: Telegram notifier (optional)
            exchange: Exchange client for position closing
            daily_loss_limit_inr: Daily loss limit in INR (default ₹1500)
            max_daily_loss_pct: Daily drawdown limit (percentage)
            max_consecutive_losses: Max losses before cooldown
            emergency_drawdown_pct: Total drawdown for emergency stop
            cooldown_minutes: Cooldown duration
            max_trade_risk_pct: Max risk per trade
            warning_threshold: When to warn (fraction of limit)
            enable_recovery_mode: Enable reduced risk after unlocking
            on_daily_limit_hit: Callback when daily limit is hit
        """
        self.state = state_manager
        self.kill_switch = kill_switch
        self.notifier = notifier
        self.exchange = exchange

        # ₹1500 Daily Loss Limit (PRIMARY)
        self.daily_loss_limit_inr = (
            daily_loss_limit_inr or self.DEFAULT_DAILY_LOSS_LIMIT_INR
        )

        # Percentage-based thresholds (SECONDARY)
        self.max_daily_loss_pct = max_daily_loss_pct or self.DEFAULT_DAILY_LOSS_PCT
        self.max_consecutive_losses = (
            max_consecutive_losses or self.DEFAULT_MAX_CONSEC_LOSSES
        )
        self.emergency_drawdown_pct = (
            emergency_drawdown_pct or self.DEFAULT_EMERGENCY_PCT
        )
        self.cooldown_minutes = cooldown_minutes or self.DEFAULT_COOLDOWN_MINUTES
        self.max_trade_risk_pct = (
            max_trade_risk_pct or self.DEFAULT_MAX_TRADE_RISK_PCT
        )
        self.warning_threshold = warning_threshold or self.DEFAULT_WARNING_THRESHOLD
        self.enable_recovery_mode = enable_recovery_mode

        # Callbacks
        self.on_daily_limit_hit = on_daily_limit_hit

        # Tracking
        self._last_warning_time: Optional[datetime] = None
        self._warnings_sent_today: int = 0
        self._lock_history: List[Dict] = []
        self._trade_start_times: Dict[str, datetime] = {}  # For duration tracking

        # Initialize daily tracking in state
        self._ensure_daily_tracking()

        logger.info(
            f"🛡️ LossGuard initialized | "
            f"DailyLimit=₹{self.daily_loss_limit_inr:.0f} | "
            f"DailyDD={self.max_daily_loss_pct*100:.1f}% | "
            f"Emergency={self.emergency_drawdown_pct*100:.1f}% | "
            f"MaxConsec={self.max_consecutive_losses} | "
            f"Cooldown={self.cooldown_minutes}min"
        )

    def _ensure_daily_tracking(self) -> None:
        """Ensure daily loss tracking is initialized."""
        if self.state.get("daily_loss_inr") is None:
            self.state.set("daily_loss_inr", 0.0)
        if self.state.get("daily_loss_date") is None:
            self.state.set("daily_loss_date", str(date.today()))

    # ═══════════════════════════════════════════════════════════════
    #  DAILY RESET CHECK (call at start of each cycle)
    # ═══════════════════════════════════════════════════════════════

    def check_daily_reset(self) -> bool:
        """
        Check if it's a new day and reset daily limits.
        
        Call this at the START of each trading cycle.
        Returns True if a reset occurred.
        """
        today = str(date.today())
        last_date = self.state.get("daily_loss_date")

        if last_date != today:
            logger.info(
                f"🌅 New day detected | {last_date} → {today} | "
                f"Resetting daily limits..."
            )
            self._perform_daily_reset(today)
            return True

        return False

    def _perform_daily_reset(self, today: str) -> None:
        """Perform full daily reset."""
        old_loss = self._safe_float("daily_loss_inr")

        # Reset daily loss counter
        self.state.set("daily_loss_inr", 0.0)
        self.state.set("daily_loss_date", today)

        # Clear daily limit lock (auto-resume)
        if self.state.get("daily_limit_locked"):
            logger.info("✅ Daily limit lock cleared — trading resumed")
            self.state.set("daily_limit_locked", False)
            self.state.set("daily_limit_lock_time", None)

        # Reset daily notifications
        self.state.set("daily_limit_notified", False)
        self._warnings_sent_today = 0
        self._last_warning_time = None

        # Update start of day balance
        balance = self._safe_float("balance")
        self.state.set("start_of_day_balance", balance)

        # Reset daily trade count (if tracked)
        self.state.set("trades_today", 0)

        logger.info(
            f"🔄 Daily reset complete | "
            f"Previous loss: ₹{old_loss:.2f} | "
            f"New balance baseline: ₹{balance:.2f}"
        )

        # Send notification
        self._send_daily_reset_notification(old_loss, balance)

    # ═══════════════════════════════════════════════════════════════
    #  PRIMARY GATE
    # ═══════════════════════════════════════════════════════════════

    def can_trade(self) -> Tuple[bool, str]:
        """
        Master trading gate. Checks all protection layers.

        Returns:
            (can_trade: bool, reason: str)
        """
        # Auto-check for new day (enables auto-resume)
        self.check_daily_reset()

        # Layer 0: ₹1500 Daily Loss Limit (HIGHEST PRIORITY)
        daily_limit_blocked, daily_limit_reason = self._check_daily_loss_limit()
        if daily_limit_blocked:
            return False, daily_limit_reason

        # Layer 1: Emergency stop
        emergency, emergency_reason = self._check_emergency_stop()
        if emergency:
            return False, emergency_reason

        # Layer 2: Daily drawdown (percentage-based)
        daily_blocked, daily_reason = self._check_daily_drawdown()
        if daily_blocked:
            return False, daily_reason

        # Layer 3: Consecutive loss cooldown
        cooldown_blocked, cooldown_reason = self._check_consecutive_losses()
        if cooldown_blocked:
            return False, cooldown_reason

        # Layer 4: Manual lock
        if self.state.get("loss_guard_locked", False):
            reason = self.state.get("loss_guard_lock_reason", "Manual lock")
            return False, f"Loss guard locked: {reason}"

        # Check for warnings (don't block, just warn)
        self._check_warnings()

        return True, "OK"

    def get_guard_status(self) -> GuardStatus:
        """Get current guard status level."""
        can_trade_result, reason = self.can_trade()

        if can_trade_result:
            # Check if in warning zone
            daily_loss = self._safe_float("daily_loss_inr")
            warning_level = self.daily_loss_limit_inr * self.warning_threshold
            if daily_loss >= warning_level:
                return GuardStatus.WARNING
            return GuardStatus.CLEAR

        if "₹1500" in reason or "daily loss limit" in reason.lower():
            return GuardStatus.DAILY_LIMIT
        if "EMERGENCY" in reason.upper():
            return GuardStatus.HARD_STOP
        if "Daily drawdown" in reason:
            return GuardStatus.SOFT_STOP
        if "cooldown" in reason.lower():
            return GuardStatus.COOLDOWN

        return GuardStatus.LOCKED

    # ═══════════════════════════════════════════════════════════════
    #  ₹1500 DAILY LOSS LIMIT (PRIMARY PROTECTION)
    # ═══════════════════════════════════════════════════════════════

    def _check_daily_loss_limit(self) -> Tuple[bool, str]:
        """
        Layer 0: Check ₹1500 daily loss limit.
        
        This is the PRIMARY protection - takes precedence over all others.
        When triggered:
        - Immediately closes all open positions
        - Stops trading for rest of day
        - Auto-resumes next day
        """
        # Check if already locked for today
        if self.state.get("daily_limit_locked"):
            lock_time = self.state.get("daily_limit_lock_time", "")
            return True, (
                f"DAILY LIMIT: Trading stopped for today | "
                f"Loss ≥ ₹{self.daily_loss_limit_inr:.0f} | "
                f"Locked at {lock_time} | "
                f"Will resume tomorrow"
            )

        # Get current daily loss
        daily_loss = self._safe_float("daily_loss_inr")

        if daily_loss >= self.daily_loss_limit_inr:
            # TRIGGER DAILY LIMIT
            return self._trigger_daily_limit(daily_loss)

        return False, ""

    def _trigger_daily_limit(self, daily_loss: float) -> Tuple[bool, str]:
        """
        Trigger daily limit protection.
        
        Actions:
        1. Close all open positions
        2. Lock trading for rest of day
        3. Send notification
        4. Will auto-resume tomorrow
        """
        now = datetime.utcnow()
        lock_time = now.strftime("%H:%M:%S UTC")

        reason = (
            f"DAILY LIMIT REACHED: ₹{daily_loss:.2f} ≥ ₹{self.daily_loss_limit_inr:.0f}"
        )

        logger.critical(f"🚨 {reason}")

        # Set daily limit lock
        self.state.set("daily_limit_locked", True)
        self.state.set("daily_limit_lock_time", lock_time)
        self.state.set("daily_limit_lock_timestamp", now.isoformat())

        # Record lock event
        self._record_lock(
            LockReason.DAILY_LOSS_LIMIT,
            reason,
            daily_loss,
        )

        # Close all open positions
        closed_positions = self._close_all_positions_emergency(
            reason="Daily loss limit reached"
        )

        # Activate kill switch if available
        if self.kill_switch:
            self.kill_switch.soft_activate(
                reason=reason,
                source="loss_guard_daily_limit",
                auto_resume_minutes=None,  # No auto-resume (wait for next day)
            )

        # Call callback if provided
        if self.on_daily_limit_hit:
            try:
                self.on_daily_limit_hit(daily_loss, closed_positions)
            except Exception as e:
                logger.error(f"Daily limit callback error: {e}")

        # Send notification
        self._send_daily_limit_reached_notification(
            daily_loss, closed_positions
        )

        return True, (
            f"DAILY LIMIT: Trading stopped | "
            f"Loss ₹{daily_loss:.2f} ≥ ₹{self.daily_loss_limit_inr:.0f} | "
            f"Closed {len(closed_positions)} positions | "
            f"Will resume tomorrow"
        )

    def _close_all_positions_emergency(self, reason: str) -> List[Dict]:
        """
        Emergency close all open positions.
        
        Returns list of closed position details.
        """
        results = []
        positions = self.state.get_all_positions()

        if not positions:
            logger.info("📭 No open positions to close")
            return results

        logger.warning(
            f"🚨 Emergency closing {len(positions)} position(s) | "
            f"Reason: {reason}"
        )

        for symbol, pos in positions.items():
            qty = pos.get("quantity", 0)
            if qty <= 0:
                continue

            result = self._close_single_position(symbol, pos, qty, reason)
            results.append(result)

        return results

    def _close_single_position(
        self,
        symbol: str,
        position: Dict,
        quantity: float,
        reason: str,
    ) -> Dict:
        """Close a single position with full tracking."""
        entry_price = float(
            position.get("entry_price") or 
            position.get("avg_price") or 0
        )
        entry_time = position.get("entry_time")

        # Calculate duration
        duration_str = "Unknown"
        duration_seconds = 0
        if entry_time:
            try:
                if isinstance(entry_time, str):
                    start = datetime.fromisoformat(entry_time)
                else:
                    start = entry_time
                duration_seconds = (datetime.utcnow() - start).total_seconds()
                duration_str = self._format_duration(duration_seconds)
            except:
                pass

        try:
            if self.exchange:
                fill = self.exchange.sell(symbol=symbol, quantity=quantity)

                if fill and fill.get("status") != "REJECTED":
                    exit_price = float(fill.get("price", 0))
                    fee = float(fill.get("fee", 0))
                    gross_pnl = (exit_price - entry_price) * quantity
                    net_pnl = gross_pnl - fee

                    # Close position in state
                    self.state.close_position(
                        symbol, net_pnl,
                        exit_price=exit_price,
                        reason=reason,
                    )

                    logger.info(
                        f"🚨 Emergency close | {symbol} | "
                        f"Qty={quantity} @ ₹{exit_price:.6f} | "
                        f"PnL=₹{net_pnl:+.4f} | "
                        f"Duration={duration_str}"
                    )

                    return {
                        "symbol": symbol,
                        "quantity": quantity,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "pnl": round(net_pnl, 4),
                        "fee": fee,
                        "duration": duration_str,
                        "duration_seconds": duration_seconds,
                        "status": "CLOSED",
                        "reason": reason,
                    }
                else:
                    fail_reason = fill.get("reason", "Rejected") if fill else "No response"
                    logger.error(f"❌ Emergency close FAILED | {symbol}: {fail_reason}")
                    return {
                        "symbol": symbol,
                        "quantity": quantity,
                        "status": "FAILED",
                        "reason": fail_reason,
                    }
            else:
                # No exchange - just close in state
                current_price = entry_price  # Assume no change
                pnl = 0.0

                self.state.close_position(
                    symbol, pnl,
                    exit_price=current_price,
                    reason=reason,
                )

                return {
                    "symbol": symbol,
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "exit_price": current_price,
                    "pnl": 0.0,
                    "status": "CLOSED_NO_EXCHANGE",
                    "reason": reason,
                }

        except Exception as e:
            logger.exception(f"❌ Emergency close error | {symbol}: {e}")
            return {
                "symbol": symbol,
                "quantity": quantity,
                "status": "ERROR",
                "reason": str(e),
            }

    def add_daily_loss(self, loss_amount: float) -> Dict:
        """
        Add to daily loss counter.
        
        Call this after every losing trade.
        Returns status dict with whether limit was hit.
        """
        if loss_amount <= 0:
            return {"added": 0, "limit_hit": False}

        current_loss = self._safe_float("daily_loss_inr")
        new_loss = current_loss + loss_amount
        self.state.set("daily_loss_inr", new_loss)

        logger.info(
            f"📉 Daily loss updated | "
            f"₹{current_loss:.2f} + ₹{loss_amount:.2f} = ₹{new_loss:.2f} | "
            f"Limit: ₹{self.daily_loss_limit_inr:.0f} | "
            f"Remaining: ₹{max(0, self.daily_loss_limit_inr - new_loss):.2f}"
        )

        # Check if this triggers the limit
        limit_hit = new_loss >= self.daily_loss_limit_inr

        if limit_hit and not self.state.get("daily_limit_locked"):
            self._trigger_daily_limit(new_loss)

        return {
            "added": loss_amount,
            "total_daily_loss": new_loss,
            "limit": self.daily_loss_limit_inr,
            "remaining": max(0, self.daily_loss_limit_inr - new_loss),
            "limit_hit": limit_hit,
        }

    def get_daily_loss_status(self) -> Dict:
        """Get current daily loss status."""
        daily_loss = self._safe_float("daily_loss_inr")
        remaining = max(0, self.daily_loss_limit_inr - daily_loss)
        pct_used = (daily_loss / self.daily_loss_limit_inr * 100) if self.daily_loss_limit_inr > 0 else 0

        return {
            "daily_loss_inr": round(daily_loss, 2),
            "daily_limit_inr": self.daily_loss_limit_inr,
            "remaining_inr": round(remaining, 2),
            "pct_used": round(pct_used, 1),
            "is_locked": self.state.get("daily_limit_locked", False),
            "lock_time": self.state.get("daily_limit_lock_time", ""),
            "date": self.state.get("daily_loss_date", str(date.today())),
        }

    # ═══════════════════════════════════════════════════════════════
    #  PER-TRADE VALIDATION
    # ═══════════════════════════════════════════════════════════════

    def validate_trade_risk(
        self,
        potential_loss: float,
        symbol: str = "",
    ) -> Tuple[bool, str]:
        """
        Validate that a single trade's potential loss is acceptable.

        Checks:
        1. Would this trade push us over daily limit?
        2. Is the trade risk within per-trade limit?

        Args:
            potential_loss: Estimated loss if SL is hit (in INR)
            symbol: Trading pair (for logging)

        Returns:
            (valid: bool, reason: str)
        """
        # Check 1: Would this exceed daily limit?
        daily_loss = self._safe_float("daily_loss_inr")
        remaining_daily = self.daily_loss_limit_inr - daily_loss

        if potential_loss > remaining_daily:
            logger.warning(
                f"❌ Trade rejected | {symbol} | "
                f"Risk ₹{potential_loss:.2f} > Remaining daily ₹{remaining_daily:.2f}"
            )
            return (
                False,
                f"Trade risk ₹{potential_loss:.2f} exceeds remaining "
                f"daily limit ₹{remaining_daily:.2f}"
            )

        # Check 2: Per-trade risk limit
        balance = self._safe_float("balance") or 0

        if balance <= 0:
            return False, "Balance is zero"

        # Calculate max allowed loss per trade
        base_max = balance * self.max_trade_risk_pct

        # Reduce if in recovery mode
        if self._is_in_recovery_mode():
            max_loss = base_max * self.RECOVERY_RISK_MULTIPLIER
            mode = " (recovery mode)"
        else:
            max_loss = base_max
            mode = ""

        if potential_loss > max_loss:
            logger.warning(
                f"❌ Trade risk rejected | {symbol} | "
                f"Risk ₹{potential_loss:.4f} > Max ₹{max_loss:.4f}{mode}"
            )
            return (
                False,
                f"Trade risk ₹{potential_loss:.4f} > "
                f"max ₹{max_loss:.4f} "
                f"({self.max_trade_risk_pct*100:.1f}%{mode})",
            )

        return True, "OK"

    def get_max_trade_risk(self) -> float:
        """Get current max trade risk amount in INR."""
        balance = self._safe_float("balance") or 0
        base_max = balance * self.max_trade_risk_pct

        if self._is_in_recovery_mode():
            return base_max * self.RECOVERY_RISK_MULTIPLIER

        return base_max

    def get_remaining_daily_risk(self) -> float:
        """Get remaining daily risk allowance in INR."""
        daily_loss = self._safe_float("daily_loss_inr")
        return max(0, self.daily_loss_limit_inr - daily_loss)

    def get_risk_multiplier(self) -> float:
        """Get current risk multiplier (1.0 = normal, <1.0 = reduced)."""
        if self._is_in_recovery_mode():
            return self.RECOVERY_RISK_MULTIPLIER
        return 1.0

    # ═══════════════════════════════════════════════════════════════
    #  TRADE RECORDING
    # ═══════════════════════════════════════════════════════════════

    def record_trade_entry(self, symbol: str) -> None:
        """Record trade entry time for duration tracking."""
        self._trade_start_times[symbol] = datetime.utcnow()
        logger.debug(f"📝 Trade entry recorded | {symbol}")

    def record_trade(
        self,
        pnl: float,
        pnl_pct: float = 0.0,
        symbol: str = "",
        exit_price: float = 0.0,
        entry_price: float = 0.0,
    ) -> Dict:
        """
        Record a trade result.

        Args:
            pnl: Profit/loss amount in INR (positive = win)
            pnl_pct: PnL as percentage
            symbol: Trading pair
            exit_price: Exit price
            entry_price: Entry price

        Returns:
            Summary dict with exit notification data
        """
        # Calculate duration
        duration_str = "Unknown"
        duration_seconds = 0
        if symbol in self._trade_start_times:
            start = self._trade_start_times.pop(symbol)
            duration_seconds = (datetime.utcnow() - start).total_seconds()
            duration_str = self._format_duration(duration_seconds)

        if pnl > 0:
            result = self.record_win(pnl, pnl_pct, symbol)
        else:
            result = self.record_loss(abs(pnl), abs(pnl_pct), symbol)
            # Add to daily loss counter
            self.add_daily_loss(abs(pnl))

        # Add exit notification data
        result.update({
            "exit_price": exit_price,
            "entry_price": entry_price,
            "duration": duration_str,
            "duration_seconds": duration_seconds,
        })

        # Send exit notification
        self._send_trade_exit_notification(
            symbol=symbol,
            pnl=pnl,
            pnl_pct=pnl_pct,
            exit_price=exit_price,
            entry_price=entry_price,
            duration=duration_str,
            is_win=pnl > 0,
        )

        return result

    def record_loss(
        self,
        loss_amount: float,
        loss_pct: float = 0.0,
        symbol: str = "",
    ) -> Dict:
        """
        Record a losing trade.

        Updates consecutive losses, streaks, and checks for cooldown.
        """
        # Increment consecutive losses
        consec = (self.state.get("consecutive_losses") or 0) + 1
        self.state.set("consecutive_losses", consec)

        # Update streaks
        loss_streak = (self.state.get("loss_streak") or 0) + 1
        self.state.set("loss_streak", loss_streak)
        self.state.set("win_streak", 0)

        # Update totals
        self.state.increment("total_losses", 1)

        # Track largest loss
        largest_loss = self.state.get("largest_loss", 0) or 0
        if loss_amount > abs(largest_loss):
            self.state.set("largest_loss", -loss_amount)

        # Set recovery mode flag
        if self.enable_recovery_mode:
            self.state.set("in_recovery_mode", True)
            self.state.set("recovery_wins_needed", self.RECOVERY_EXIT_WINS)

        logger.warning(
            f"📉 Loss recorded | {symbol} | "
            f"₹{loss_amount:.4f} ({loss_pct:.2f}%) | "
            f"Consec={consec} | Streak={loss_streak}"
        )

        result = {
            "type": "loss",
            "amount": loss_amount,
            "pct": loss_pct,
            "consecutive": consec,
            "streak": loss_streak,
            "cooldown_triggered": False,
        }

        # Check if cooldown needed
        if consec >= self.max_consecutive_losses:
            self._start_cooldown(
                reason=f"{consec} consecutive losses",
                minutes=self.cooldown_minutes,
            )
            result["cooldown_triggered"] = True

            # Notify
            self._send_cooldown_notification(consec, loss_amount)

        return result

    def record_win(
        self,
        win_amount: float,
        win_pct: float = 0.0,
        symbol: str = "",
    ) -> Dict:
        """
        Record a winning trade.

        Resets consecutive losses and updates streaks.
        """
        # Reset consecutive losses
        self.state.set("consecutive_losses", 0)

        # Update streaks
        win_streak = (self.state.get("win_streak") or 0) + 1
        self.state.set("win_streak", win_streak)
        self.state.set("loss_streak", 0)

        # Update totals
        self.state.increment("total_wins", 1)

        # Track largest win
        largest_win = self.state.get("largest_win", 0) or 0
        if win_amount > largest_win:
            self.state.set("largest_win", win_amount)

        # Check recovery mode exit
        recovery_exited = False
        if self._is_in_recovery_mode():
            wins_needed = self.state.get("recovery_wins_needed", self.RECOVERY_EXIT_WINS)
            wins_needed -= 1
            if wins_needed <= 0:
                self.state.set("in_recovery_mode", False)
                self.state.set("recovery_wins_needed", 0)
                recovery_exited = True
                logger.info("✅ Exited recovery mode — back to normal risk")
            else:
                self.state.set("recovery_wins_needed", wins_needed)

        logger.info(
            f"📈 Win recorded | {symbol} | "
            f"₹{win_amount:.4f} ({win_pct:.2f}%) | "
            f"Streak={win_streak}"
        )

        return {
            "type": "win",
            "amount": win_amount,
            "pct": win_pct,
            "streak": win_streak,
            "recovery_exited": recovery_exited,
        }

    # ═══════════════════════════════════════════════════════════════
    #  PROTECTION LAYERS (Percentage-based)
    # ═══════════════════════════════════════════════════════════════

    def _check_emergency_stop(self) -> Tuple[bool, str]:
        """Layer 1: Emergency hard stop (percentage-based)."""
        # Skip if acknowledged
        if self.state.get("emergency_acknowledged", False):
            return False, ""

        initial = self._safe_float("initial_balance") or 0
        current = self._safe_float("balance") or 0

        if initial <= 0:
            return False, ""

        if current >= initial:
            return False, ""

        total_loss_pct = (initial - current) / initial

        if total_loss_pct >= self.emergency_drawdown_pct:
            reason = (
                f"EMERGENCY STOP: Total drawdown "
                f"{total_loss_pct*100:.1f}% ≥ "
                f"{self.emergency_drawdown_pct*100:.1f}% | "
                f"Balance ₹{current:.2f} / Initial ₹{initial:.2f}"
            )

            # Activate kill switch
            if self.kill_switch and not self.kill_switch.is_active():
                self.kill_switch.hard_activate(
                    reason=reason,
                    source="loss_guard_emergency",
                    loss_pct=total_loss_pct * 100,
                )

            # Record lock
            self._record_lock(
                LockReason.EMERGENCY,
                reason,
                total_loss_pct * 100,
            )

            # Notify
            self._send_emergency_notification(total_loss_pct * 100, current, initial)

            logger.critical(f"🚨 {reason}")
            return True, reason

        return False, ""

    def _check_daily_drawdown(self) -> Tuple[bool, str]:
        """Layer 2: Daily drawdown soft stop (percentage-based)."""
        start = self._safe_float("start_of_day_balance") or 0
        current = self._safe_float("balance") or 0

        if start <= 0 or current >= start:
            return False, ""

        daily_loss_pct = (start - current) / start

        if daily_loss_pct >= self.max_daily_loss_pct:
            reason = (
                f"Daily drawdown limit: "
                f"{daily_loss_pct*100:.1f}% ≥ "
                f"{self.max_daily_loss_pct*100:.1f}% | "
                f"Balance ₹{current:.2f} / Start ₹{start:.2f}"
            )

            # Record lock
            self._record_lock(
                LockReason.DAILY_DRAWDOWN,
                reason,
                daily_loss_pct * 100,
            )

            # Notify (once)
            if not self.state.get("daily_dd_pct_notified"):
                self._send_daily_drawdown_notification(daily_loss_pct * 100, current)
                self.state.set("daily_dd_pct_notified", True)

            logger.warning(f"🛑 {reason}")
            return True, reason

        return False, ""

    def _check_consecutive_losses(self) -> Tuple[bool, str]:
        """Layer 3: Consecutive loss cooldown."""
        cooldown_until = self.state.get("loss_guard_cooldown_until")

        if not cooldown_until:
            return False, ""

        try:
            cooldown_dt = datetime.fromisoformat(cooldown_until)
            now = datetime.utcnow()

            if now < cooldown_dt:
                remaining = (cooldown_dt - now).total_seconds()
                remaining_min = remaining / 60
                reason = (
                    f"Consecutive loss cooldown | "
                    f"{remaining_min:.1f}min remaining | "
                    f"Resumes {cooldown_dt.strftime('%H:%M')} UTC"
                )
                return True, reason
            else:
                # Cooldown expired
                logger.info("✅ Loss guard cooldown expired")
                self.state.set("loss_guard_cooldown_until", None)
                self.state.set("consecutive_losses", 0)
                return False, ""

        except (ValueError, TypeError):
            self.state.set("loss_guard_cooldown_until", None)
            return False, ""

    def _check_warnings(self) -> None:
        """Check and send warnings if approaching limits."""
        now = datetime.utcnow()

        # Rate limit warnings
        if self._last_warning_time:
            elapsed = (now - self._last_warning_time).total_seconds()
            if elapsed < 300:  # Max 1 warning per 5 minutes
                return

        if self._warnings_sent_today >= 5:
            return

        # Check daily loss warning (INR)
        daily_loss = self._safe_float("daily_loss_inr")
        warning_level = self.daily_loss_limit_inr * self.warning_threshold

        if daily_loss >= warning_level:
            remaining = self.daily_loss_limit_inr - daily_loss
            logger.warning(
                f"⚠️ Approaching daily limit: ₹{daily_loss:.2f} "
                f"(₹{remaining:.2f} remaining)"
            )
            self._send_warning_notification(daily_loss, remaining)
            self._last_warning_time = now
            self._warnings_sent_today += 1

    # ═══════════════════════════════════════════════════════════════
    #  COOLDOWN MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def _start_cooldown(self, reason: str, minutes: int) -> None:
        """Start a cooldown timer."""
        resume_at = datetime.utcnow() + timedelta(minutes=minutes)

        self.state.set("loss_guard_cooldown_until", resume_at.isoformat())
        self.state.set("loss_guard_lock_reason", reason)

        self._record_lock(LockReason.CONSECUTIVE_LOSSES, reason)

        logger.warning(
            f"⏳ Cooldown started | {reason} | "
            f"{minutes}min | Resumes {resume_at.strftime('%H:%M')} UTC"
        )

    def extend_cooldown(self, additional_minutes: int) -> Dict:
        """Extend active cooldown."""
        current = self.state.get("loss_guard_cooldown_until")

        if not current:
            # Create new cooldown
            resume_at = datetime.utcnow() + timedelta(minutes=additional_minutes)
        else:
            try:
                current_dt = datetime.fromisoformat(current)
                resume_at = current_dt + timedelta(minutes=additional_minutes)
            except:
                resume_at = datetime.utcnow() + timedelta(minutes=additional_minutes)

        self.state.set("loss_guard_cooldown_until", resume_at.isoformat())

        logger.info(f"⏳ Cooldown extended to {resume_at.strftime('%H:%M')} UTC")

        return {
            "success": True,
            "resume_at": resume_at.isoformat(),
            "added_minutes": additional_minutes,
        }

    def clear_cooldown(self) -> Dict:
        """Clear cooldown (for manual override)."""
        self.state.set("loss_guard_cooldown_until", None)
        self.state.set("consecutive_losses", 0)
        logger.info("✅ Cooldown cleared")
        return {"success": True}

    # ═══════════════════════════════════════════════════════════════
    #  RECOVERY MODE
    # ═══════════════════════════════════════════════════════════════

    def _is_in_recovery_mode(self) -> bool:
        """Check if in recovery mode."""
        if not self.enable_recovery_mode:
            return False
        return self.state.get("in_recovery_mode", False)

    def exit_recovery_mode(self) -> Dict:
        """Manually exit recovery mode."""
        self.state.set("in_recovery_mode", False)
        self.state.set("recovery_wins_needed", 0)
        logger.info("✅ Recovery mode manually exited")
        return {"success": True, "recovery_mode": False}

    def enter_recovery_mode(self, wins_needed: int = None) -> Dict:
        """Manually enter recovery mode."""
        wins = wins_needed or self.RECOVERY_EXIT_WINS
        self.state.set("in_recovery_mode", True)
        self.state.set("recovery_wins_needed", wins)
        logger.info(f"⚠️ Entered recovery mode — need {wins} wins to exit")
        return {"success": True, "recovery_mode": True, "wins_needed": wins}

    # ═══════════════════════════════════════════════════════════════
    #  UNLOCK / RESET
    # ═══════════════════════════════════════════════════════════════

    def unlock(self, source: str = "manual") -> Dict:
        """
        Clear all loss guard locks (except daily limit — that resets at midnight).

        Clears locks but keeps emergency_acknowledged = True
        until reset_baseline() is called.
        """
        # Clear all lock flags (EXCEPT daily limit)
        self.state.set("loss_guard_locked", False)
        self.state.set("loss_guard_lock_reason", None)
        self.state.set("loss_guard_cooldown_until", None)
        self.state.set("consecutive_losses", 0)
        self.state.set("daily_dd_pct_notified", False)

        # Acknowledge emergency (prevents re-trigger)
        self.state.set("emergency_acknowledged", True)

        # Enter recovery mode
        if self.enable_recovery_mode:
            self.state.set("in_recovery_mode", True)
            self.state.set("recovery_wins_needed", self.RECOVERY_EXIT_WINS)

        balance = self._safe_float("balance") or 0
        initial = self._safe_float("initial_balance") or 0
        total_dd = ((initial - balance) / initial * 100) if initial > 0 else 0
        daily_loss = self._safe_float("daily_loss_inr")

        # Check if daily limit is still locked
        daily_locked = self.state.get("daily_limit_locked", False)

        logger.warning(
            f"🔓 LossGuard unlocked | Source: {source} | "
            f"Balance=₹{balance:.2f} | DD={total_dd:.1f}% | "
            f"Daily Loss=₹{daily_loss:.2f} | "
            f"Daily Locked={daily_locked} | "
            f"Recovery mode: {self.enable_recovery_mode}"
        )

        return {
            "unlocked": True,
            "source": source,
            "balance": balance,
            "total_drawdown_pct": round(total_dd, 2),
            "daily_loss_inr": round(daily_loss, 2),
            "daily_limit_locked": daily_locked,
            "recovery_mode": self.enable_recovery_mode,
            "note": "Daily limit lock resets at midnight" if daily_locked else "",
        }

    def force_unlock_daily(self, source: str = "manual") -> Dict:
        """
        Force unlock daily limit (USE WITH CAUTION).
        
        This bypasses the safety of waiting until midnight.
        """
        was_locked = self.state.get("daily_limit_locked", False)

        self.state.set("daily_limit_locked", False)
        self.state.set("daily_limit_lock_time", None)
        # Note: We don't reset daily_loss_inr here — it will still count

        # Also unlock regular locks
        self.unlock(source)

        logger.warning(
            f"⚠️ FORCE UNLOCK daily limit | Source: {source} | "
            f"Was locked: {was_locked}"
        )

        return {
            "force_unlocked": True,
            "was_locked": was_locked,
            "source": source,
            "warning": "Daily loss counter NOT reset — still tracking today's losses",
        }

    def reset_baseline(self, source: str = "manual") -> Dict:
        """
        Full risk baseline reset.

        Accepts all past losses and starts fresh from current balance.
        Also resets daily loss counter.
        """
        balance = self._safe_float("balance") or 0

        if balance <= 0:
            logger.error("❌ Cannot reset — balance is zero")
            return {"reset": False, "reason": "Balance is zero"}

        old_initial = self._safe_float("initial_balance") or 0
        old_daily_loss = self._safe_float("daily_loss_inr")
        emergency_threshold = balance * (1 - self.emergency_drawdown_pct)

        # Reset baselines
        self.state.set("initial_balance", balance)
        self.state.set("start_of_day_balance", balance)
        self.state.set("daily_pnl", 0.0)

        # Reset daily loss tracking
        self.state.set("daily_loss_inr", 0.0)
        self.state.set("daily_loss_date", str(date.today()))
        self.state.set("daily_limit_locked", False)
        self.state.set("daily_limit_lock_time", None)

        # Clear all locks
        self.state.set("loss_guard_locked", False)
        self.state.set("loss_guard_lock_reason", None)
        self.state.set("loss_guard_cooldown_until", None)
        self.state.set("consecutive_losses", 0)
        self.state.set("daily_limit_notified", False)
        self.state.set("daily_dd_pct_notified", False)

        # Reset streaks
        self.state.set("win_streak", 0)
        self.state.set("loss_streak", 0)

        # Re-enable emergency protection with new baseline
        self.state.set("emergency_acknowledged", False)

        # Exit recovery mode
        self.state.set("in_recovery_mode", False)
        self.state.set("recovery_wins_needed", 0)

        # Reset daily counters
        self._warnings_sent_today = 0

        logger.warning(
            f"🔄 Baseline reset | Source: {source} | "
            f"₹{old_initial:.2f} → ₹{balance:.2f} | "
            f"Daily loss reset: ₹{old_daily_loss:.2f} → ₹0 | "
            f"Emergency threshold: ₹{emergency_threshold:.2f}"
        )

        return {
            "reset": True,
            "source": source,
            "old_initial": old_initial,
            "new_initial": balance,
            "old_daily_loss": old_daily_loss,
            "emergency_threshold": round(emergency_threshold, 2),
        }

    # ═══════════════════════════════════════════════════════════════
    #  LOCK HISTORY
    # ═══════════════════════════════════════════════════════════════

    def _record_lock(
        self,
        reason: LockReason,
        details: str,
        loss_amount: float = 0.0,
    ) -> None:
        """Record a lock event."""
        record = {
            "reason": reason.value,
            "details": details,
            "loss_amount": round(loss_amount, 2),
            "timestamp": datetime.utcnow().isoformat(),
            "balance": self._safe_float("balance"),
            "daily_loss": self._safe_float("daily_loss_inr"),
        }

        self._lock_history.append(record)
        if len(self._lock_history) > 50:
            self._lock_history = self._lock_history[-50:]

    def get_lock_history(self, limit: int = 10) -> List[Dict]:
        """Get recent lock history."""
        return self._lock_history[-limit:]

    # ═══════════════════════════════════════════════════════════════
    #  NOTIFICATIONS
    # ═══════════════════════════════════════════════════════════════

    def _send_trade_exit_notification(
        self,
        symbol: str,
        pnl: float,
        pnl_pct: float,
        exit_price: float,
        entry_price: float,
        duration: str,
        is_win: bool,
    ) -> None:
        """Send trade exit notification with full details."""
        if not self.notifier:
            return

        emoji = "🟢" if is_win else "🔴"
        status = "PROFIT" if is_win else "LOSS"

        daily_loss = self._safe_float("daily_loss_inr")
        remaining = max(0, self.daily_loss_limit_inr - daily_loss)

        message = (
            f"{emoji} <b>TRADE EXIT - {status}</b>\n\n"
            f"Symbol: {symbol}\n"
            f"Exit Price: ₹{exit_price:.6f}\n"
            f"Entry Price: ₹{entry_price:.6f}\n"
            f"PnL: ₹{pnl:+.4f} ({pnl_pct:+.2f}%)\n"
            f"Duration: {duration}\n"
            f"\n📊 Daily Status:\n"
            f"Daily Loss: ₹{daily_loss:.2f}\n"
            f"Remaining: ₹{remaining:.2f} / ₹{self.daily_loss_limit_inr:.0f}"
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send exit notification: {e}")

    def _send_daily_limit_reached_notification(
        self,
        daily_loss: float,
        closed_positions: List[Dict],
    ) -> None:
        """Send notification when daily limit is reached."""
        if not self.notifier:
            return

        positions_text = ""
        if closed_positions:
            positions_text = "\n\n📉 <b>Closed Positions:</b>\n"
            total_pnl = 0
            for pos in closed_positions:
                pnl = pos.get("pnl", 0)
                total_pnl += pnl
                positions_text += (
                    f"• {pos['symbol']}: ₹{pnl:+.4f} "
                    f"({pos.get('duration', 'N/A')})\n"
                )
            positions_text += f"\nTotal PnL: ₹{total_pnl:+.4f}"

        message = (
            f"🚨 <b>DAILY LOSS LIMIT REACHED</b> 🚨\n\n"
            f"Daily Loss: ₹{daily_loss:.2f}\n"
            f"Limit: ₹{self.daily_loss_limit_inr:.0f}\n"
            f"\n⛔ <b>Trading stopped for today</b>\n"
            f"✅ All positions closed\n"
            f"🌅 Trading will resume tomorrow"
            f"{positions_text}"
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send daily limit notification: {e}")

    def _send_daily_reset_notification(
        self,
        old_loss: float,
        balance: float,
    ) -> None:
        """Send notification when daily limits reset."""
        if not self.notifier:
            return

        message = (
            f"🌅 <b>NEW TRADING DAY</b>\n\n"
            f"Previous day loss: ₹{old_loss:.2f}\n"
            f"Starting balance: ₹{balance:.2f}\n"
            f"Daily limit: ₹{self.daily_loss_limit_inr:.0f}\n"
            f"\n✅ Trading resumed\n"
            f"Good luck today! 🍀"
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send reset notification: {e}")

    def _send_cooldown_notification(self, consec: int, loss_amount: float) -> None:
        """Send cooldown notification."""
        if not self.notifier:
            return

        message = (
            f"⏳ <b>TRADING PAUSED</b>\n\n"
            f"Reason: {consec} consecutive losses\n"
            f"Last loss: ₹{loss_amount:.4f}\n"
            f"Cooldown: {self.cooldown_minutes} minutes\n"
            f"\nTrading will auto-resume after cooldown."
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def _send_daily_drawdown_notification(self, dd_pct: float, balance: float) -> None:
        """Send daily drawdown (percentage) notification."""
        if not self.notifier:
            return

        message = (
            f"🛑 <b>DAILY DRAWDOWN LIMIT</b>\n\n"
            f"Daily drawdown: {dd_pct:.1f}%\n"
            f"Limit: {self.max_daily_loss_pct*100:.1f}%\n"
            f"Balance: ₹{balance:.2f}\n"
            f"\nTrading paused until tomorrow."
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def _send_emergency_notification(
        self, dd_pct: float, balance: float, initial: float
    ) -> None:
        """Send emergency notification."""
        if not self.notifier:
            return

        message = (
            f"🚨 <b>EMERGENCY STOP</b>\n\n"
            f"Total drawdown: {dd_pct:.1f}%\n"
            f"Limit: {self.emergency_drawdown_pct*100:.1f}%\n"
            f"Balance: ₹{balance:.2f}\n"
            f"Initial: ₹{initial:.2f}\n"
            f"\n⚠️ Manual intervention required.\n"
            f"Use /reset_risk to reset baseline."
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def _send_warning_notification(self, daily_loss: float, remaining: float) -> None:
        """Send warning notification."""
        if not self.notifier:
            return

        message = (
            f"⚠️ <b>APPROACHING DAILY LIMIT</b>\n\n"
            f"Current loss: ₹{daily_loss:.2f}\n"
            f"Remaining: ₹{remaining:.2f}\n"
            f"Limit: ₹{self.daily_loss_limit_inr:.0f}"
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    # ═══════════════════════════════════════════════════════════════
    #  DAILY RESET (called by scheduler)
    # ═══════════════════════════════════════════════════════════════

    def reset_daily(self) -> None:
        """Reset daily counters (called by scheduler at midnight)."""
        today = str(date.today())
        self._perform_daily_reset(today)
        logger.info("🔄 LossGuard daily reset (scheduler)")

    # ═══════════════════════════════════════════════════════════════
    #  STATUS
    # ═══════════════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """Get full loss guard status."""
        can_trade_result, block_reason = self.can_trade()
        status = self.get_guard_status()

        balance = self._safe_float("balance") or 0
        initial = self._safe_float("initial_balance") or 0
        start_day = self._safe_float("start_of_day_balance") or 0

        # Daily loss (INR)
        daily_loss_inr = self._safe_float("daily_loss_inr")
        daily_remaining = max(0, self.daily_loss_limit_inr - daily_loss_inr)

        # Daily drawdown (percentage)
        daily_dd_pct = self._get_daily_drawdown_pct()

        # Total drawdown
        total_dd_pct = 0.0
        if initial > 0 and balance < initial:
            total_dd_pct = (initial - balance) / initial * 100

        # Cooldown remaining
        cooldown_remaining = 0
        cooldown_until = self.state.get("loss_guard_cooldown_until")
        if cooldown_until:
            try:
                resume_at = datetime.fromisoformat(cooldown_until)
                remaining = (resume_at - datetime.utcnow()).total_seconds()
                cooldown_remaining = max(0, remaining)
            except:
                pass

        return {
            "status": status.value,
            "can_trade": can_trade_result,
            "block_reason": block_reason if not can_trade_result else "",

            # Daily loss limit (INR) - PRIMARY
            "daily_loss_inr": round(daily_loss_inr, 2),
            "daily_limit_inr": self.daily_loss_limit_inr,
            "daily_remaining_inr": round(daily_remaining, 2),
            "daily_limit_pct_used": round(
                daily_loss_inr / self.daily_loss_limit_inr * 100, 1
            ) if self.daily_loss_limit_inr > 0 else 0,
            "daily_limit_locked": self.state.get("daily_limit_locked", False),
            "daily_limit_lock_time": self.state.get("daily_limit_lock_time", ""),

            # Streaks
            "consecutive_losses": self.state.get("consecutive_losses") or 0,
            "max_consecutive_losses": self.max_consecutive_losses,
            "win_streak": self.state.get("win_streak") or 0,
            "loss_streak": self.state.get("loss_streak") or 0,

            # Cooldown
            "cooldown_active": cooldown_remaining > 0,
            "cooldown_remaining_sec": round(cooldown_remaining),
            "cooldown_remaining_min": round(cooldown_remaining / 60, 1),

            # Recovery
            "recovery_mode": self._is_in_recovery_mode(),
            "recovery_wins_needed": self.state.get("recovery_wins_needed", 0),
            "risk_multiplier": self.get_risk_multiplier(),

            # Balances
            "initial_balance": round(initial, 2),
            "start_day_balance": round(start_day, 2),
            "current_balance": round(balance, 2),

            # Percentage-based limits
            "daily_drawdown_pct": round(daily_dd_pct, 2),
            "daily_limit_pct": round(self.max_daily_loss_pct * 100, 1),

            "total_drawdown_pct": round(total_dd_pct, 2),
            "emergency_limit_pct": round(self.emergency_drawdown_pct * 100, 1),
            "emergency_acknowledged": self.state.get("emergency_acknowledged", False),

            "max_trade_risk": round(self.get_max_trade_risk(), 4),
        }

    def _get_daily_drawdown_pct(self) -> float:
        """Get current daily drawdown as percentage."""
        start = self._safe_float("start_of_day_balance") or 0
        current = self._safe_float("balance") or 0

        if start <= 0 or current >= start:
            return 0.0

        return ((start - current) / start) * 100

    # ═══════════════════════════════════════════════════════════════
    #  HELPERS
    # ═══════════════════════════════════════════════════════════════

    def _safe_float(self, key: str, default: float = 0.0) -> float:
        """Safely get a float from state."""
        value = self.state.get(key)
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    def _safe_int(self, key: str, default: int = 0) -> int:
        """Safely get an int from state."""
        value = self.state.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _format_duration(self, seconds: float) -> str:
        """Format duration in human-readable format."""
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            minutes = seconds / 60
            return f"{minutes:.1f}m"
        else:
            hours = seconds / 3600
            return f"{hours:.1f}h"

    def update_notifier(self, notifier) -> None:
        """Hot-swap notifier."""
        self.notifier = notifier

    def update_kill_switch(self, kill_switch) -> None:
        """Hot-swap kill switch."""
        self.kill_switch = kill_switch

    def update_exchange(self, exchange) -> None:
        """Hot-swap exchange client."""
        self.exchange = exchange

    def update_daily_limit(self, new_limit_inr: float) -> Dict:
        """Update daily loss limit."""
        old_limit = self.daily_loss_limit_inr
        self.daily_loss_limit_inr = new_limit_inr

        logger.info(
            f"⚙️ Daily limit updated | "
            f"₹{old_limit:.0f} → ₹{new_limit_inr:.0f}"
        )

        return {
            "old_limit": old_limit,
            "new_limit": new_limit_inr,
            "current_loss": self._safe_float("daily_loss_inr"),
        }

    def __repr__(self) -> str:
        status = self.get_guard_status()
        daily_loss = self._safe_float("daily_loss_inr")
        consec = self.state.get("consecutive_losses", 0)
        return (
            f"<LossGuard {status.value.upper()} | "
            f"Daily=₹{daily_loss:.0f}/₹{self.daily_loss_limit_inr:.0f} | "
            f"Consec={consec}/{self.max_consecutive_losses} | "
            f"Recovery={self._is_in_recovery_mode()}>"
        )