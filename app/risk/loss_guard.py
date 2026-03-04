# app/risk/loss_guard.py

"""
Loss Guard — Production Grade for Autonomous Trading

Multi-layer loss protection system:
- Daily drawdown protection (soft stop)
- Total/emergency drawdown protection (hard stop)
- Consecutive loss circuit breaker with cooldown
- Per-trade risk validation
- Gradual recovery mode after losses
- Win/loss recording and streak tracking
- Cooldown timer management
- Full unlock / baseline reset support
- Notification integration

Integration:
    Controller calls can_trade() before every cycle
    Controller calls validate_trade_risk() before every entry
    Controller calls record_trade() after every exit
    Controller calls unlock() from /resume command
    Controller calls reset_baseline() from /reset_risk command
"""

from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple, List
from enum import Enum
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  GUARD STATUS
# ═══════════════════════════════════════════════════════════════════

class GuardStatus(Enum):
    """Loss guard status levels."""
    CLEAR = "clear"              # All good
    WARNING = "warning"          # Approaching limits
    COOLDOWN = "cooldown"        # In cooldown period
    SOFT_STOP = "soft_stop"      # Daily limit hit
    HARD_STOP = "hard_stop"      # Emergency stop
    LOCKED = "locked"            # Manually locked


class LockReason(Enum):
    """Reasons for locking."""
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
    Multi-layer loss protection system.

    Layer 1: Consecutive loss circuit breaker
        Locks trading after N losses in a row.
        Auto-cooldown before unlock attempt.

    Layer 2: Daily drawdown soft stop
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
    DEFAULT_DAILY_LOSS_PCT = 0.05        # 5% daily drawdown
    DEFAULT_EMERGENCY_PCT = 0.15         # 15% total drawdown
    DEFAULT_MAX_CONSEC_LOSSES = 3        # 3 consecutive losses
    DEFAULT_COOLDOWN_MINUTES = 30        # 30 min cooldown
    DEFAULT_MAX_TRADE_RISK_PCT = 0.02    # 2% per trade
    DEFAULT_WARNING_THRESHOLD = 0.70     # Warn at 70% of limit

    # Recovery mode settings
    RECOVERY_RISK_MULTIPLIER = 0.50      # 50% of normal risk in recovery
    RECOVERY_EXIT_WINS = 2               # Exit recovery after N wins

    def __init__(
        self,
        state_manager,
        kill_switch=None,
        notifier=None,
        max_daily_loss_pct: float = None,
        max_consecutive_losses: int = None,
        emergency_drawdown_pct: float = None,
        cooldown_minutes: int = None,
        max_trade_risk_pct: float = None,
        warning_threshold: float = None,
        enable_recovery_mode: bool = True,
    ):
        """
        Initialize Loss Guard.

        Args:
            state_manager: State manager instance
            kill_switch: Kill switch instance
            notifier: Telegram notifier (optional)
            max_daily_loss_pct: Daily drawdown limit
            max_consecutive_losses: Max losses before cooldown
            emergency_drawdown_pct: Total drawdown for emergency stop
            cooldown_minutes: Cooldown duration
            max_trade_risk_pct: Max risk per trade
            warning_threshold: When to warn (fraction of limit)
            enable_recovery_mode: Enable reduced risk after unlocking
        """
        self.state = state_manager
        self.kill_switch = kill_switch
        self.notifier = notifier

        # Thresholds
        self.max_daily_loss_pct = max_daily_loss_pct or self.DEFAULT_DAILY_LOSS_PCT
        self.max_consecutive_losses = max_consecutive_losses or self.DEFAULT_MAX_CONSEC_LOSSES
        self.emergency_drawdown_pct = emergency_drawdown_pct or self.DEFAULT_EMERGENCY_PCT
        self.cooldown_minutes = cooldown_minutes or self.DEFAULT_COOLDOWN_MINUTES
        self.max_trade_risk_pct = max_trade_risk_pct or self.DEFAULT_MAX_TRADE_RISK_PCT
        self.warning_threshold = warning_threshold or self.DEFAULT_WARNING_THRESHOLD
        self.enable_recovery_mode = enable_recovery_mode

        # Tracking
        self._last_warning_time: Optional[datetime] = None
        self._warnings_sent_today: int = 0
        self._lock_history: List[Dict] = []

        logger.info(
            f"🛡️ LossGuard initialized | "
            f"DailyDD={self.max_daily_loss_pct*100:.1f}% | "
            f"Emergency={self.emergency_drawdown_pct*100:.1f}% | "
            f"MaxConsec={self.max_consecutive_losses} | "
            f"Cooldown={self.cooldown_minutes}min"
        )

    # ═══════════════════════════════════════════════════════════════
    #  PRIMARY GATE
    # ═══════════════════════════════════════════════════════════════

    def can_trade(self) -> Tuple[bool, str]:
        """
        Master trading gate. Checks all protection layers.

        Returns:
            (can_trade: bool, reason: str)
        """
        # Layer 1: Emergency stop
        emergency, emergency_reason = self._check_emergency_stop()
        if emergency:
            return False, emergency_reason

        # Layer 2: Daily drawdown
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
        can_trade, reason = self.can_trade()

        if can_trade:
            # Check if in warning zone
            daily_dd = self._get_daily_drawdown_pct()
            if daily_dd >= self.max_daily_loss_pct * self.warning_threshold * 100:
                return GuardStatus.WARNING
            return GuardStatus.CLEAR

        if "EMERGENCY" in reason.upper():
            return GuardStatus.HARD_STOP
        if "Daily drawdown" in reason:
            return GuardStatus.SOFT_STOP
        if "cooldown" in reason.lower():
            return GuardStatus.COOLDOWN

        return GuardStatus.LOCKED

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

        Args:
            potential_loss: Estimated loss if SL is hit
            symbol: Trading pair (for logging)

        Returns:
            (valid: bool, reason: str)
        """
        balance = self._safe_float("balance") or 0

        if balance <= 0:
            return False, "Balance is zero"

        # Calculate max allowed loss
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
                f"Risk ${potential_loss:.4f} > Max ${max_loss:.4f}{mode}"
            )
            return (
                False,
                f"Trade risk ${potential_loss:.4f} > "
                f"max ${max_loss:.4f} "
                f"({self.max_trade_risk_pct*100:.1f}%{mode})",
            )

        return True, "OK"

    def get_max_trade_risk(self) -> float:
        """Get current max trade risk amount."""
        balance = self._safe_float("balance") or 0
        base_max = balance * self.max_trade_risk_pct

        if self._is_in_recovery_mode():
            return base_max * self.RECOVERY_RISK_MULTIPLIER

        return base_max

    def get_risk_multiplier(self) -> float:
        """Get current risk multiplier (1.0 = normal, <1.0 = reduced)."""
        if self._is_in_recovery_mode():
            return self.RECOVERY_RISK_MULTIPLIER
        return 1.0

    # ═══════════════════════════════════════════════════════════════
    #  TRADE RECORDING
    # ═══════════════════════════════════════════════════════════════

    def record_trade(
        self,
        pnl: float,
        pnl_pct: float = 0.0,
        symbol: str = "",
    ) -> Dict:
        """
        Record a trade result.

        Args:
            pnl: Profit/loss amount (positive = win)
            pnl_pct: PnL as percentage
            symbol: Trading pair

        Returns:
            Summary dict
        """
        if pnl > 0:
            return self.record_win(pnl, pnl_pct, symbol)
        else:
            return self.record_loss(abs(pnl), abs(pnl_pct), symbol)

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
            f"${loss_amount:.4f} ({loss_pct:.2f}%) | "
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
            f"${win_amount:.4f} ({win_pct:.2f}%) | "
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
    #  PROTECTION LAYERS
    # ═══════════════════════════════════════════════════════════════

    def _check_emergency_stop(self) -> Tuple[bool, str]:
        """Layer 1: Emergency hard stop."""
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
                f"Balance ${current:.2f} / Initial ${initial:.2f}"
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
        """Layer 2: Daily drawdown soft stop."""
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
                f"Balance ${current:.2f} / Start ${start:.2f}"
            )

            # Record lock
            self._record_lock(
                LockReason.DAILY_DRAWDOWN,
                reason,
                daily_loss_pct * 100,
            )

            # Notify (once)
            if not self.state.get("daily_limit_notified"):
                self._send_daily_limit_notification(daily_loss_pct * 100, current)
                self.state.set("daily_limit_notified", True)

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

        # Check daily drawdown warning
        daily_dd = self._get_daily_drawdown_pct()
        warning_level = self.max_daily_loss_pct * self.warning_threshold * 100

        if daily_dd >= warning_level:
            remaining_pct = (self.max_daily_loss_pct * 100) - daily_dd
            logger.warning(
                f"⚠️ Approaching daily limit: {daily_dd:.1f}% "
                f"({remaining_pct:.1f}% remaining)"
            )
            self._send_warning_notification(daily_dd, remaining_pct)
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
        Clear all loss guard locks.

        Clears locks but keeps emergency_acknowledged = True
        until reset_baseline() is called.
        """
        # Clear all lock flags
        self.state.set("loss_guard_locked", False)
        self.state.set("loss_guard_lock_reason", None)
        self.state.set("loss_guard_cooldown_until", None)
        self.state.set("consecutive_losses", 0)
        self.state.set("daily_limit_notified", False)

        # Acknowledge emergency (prevents re-trigger)
        self.state.set("emergency_acknowledged", True)

        # Enter recovery mode
        if self.enable_recovery_mode:
            self.state.set("in_recovery_mode", True)
            self.state.set("recovery_wins_needed", self.RECOVERY_EXIT_WINS)

        balance = self._safe_float("balance") or 0
        initial = self._safe_float("initial_balance") or 0
        total_dd = ((initial - balance) / initial * 100) if initial > 0 else 0

        logger.warning(
            f"🔓 LossGuard unlocked | Source: {source} | "
            f"Balance=${balance:.2f} | DD={total_dd:.1f}% | "
            f"Recovery mode: {self.enable_recovery_mode}"
        )

        return {
            "unlocked": True,
            "source": source,
            "balance": balance,
            "total_drawdown_pct": round(total_dd, 2),
            "recovery_mode": self.enable_recovery_mode,
        }

    def reset_baseline(self, source: str = "manual") -> Dict:
        """
        Full risk baseline reset.

        Accepts all past losses and starts fresh from current balance.
        """
        balance = self._safe_float("balance") or 0

        if balance <= 0:
            logger.error("❌ Cannot reset — balance is zero")
            return {"reset": False, "reason": "Balance is zero"}

        old_initial = self._safe_float("initial_balance") or 0
        emergency_threshold = balance * (1 - self.emergency_drawdown_pct)

        # Reset baselines
        self.state.set("initial_balance", balance)
        self.state.set("start_of_day_balance", balance)
        self.state.set("daily_pnl", 0.0)

        # Clear all locks
        self.state.set("loss_guard_locked", False)
        self.state.set("loss_guard_lock_reason", None)
        self.state.set("loss_guard_cooldown_until", None)
        self.state.set("consecutive_losses", 0)
        self.state.set("daily_limit_notified", False)

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
            f"${old_initial:.2f} → ${balance:.2f} | "
            f"Emergency threshold: ${emergency_threshold:.2f}"
        )

        return {
            "reset": True,
            "source": source,
            "old_initial": old_initial,
            "new_initial": balance,
            "emergency_threshold": round(emergency_threshold, 2),
        }

    # ═══════════════════════════════════════════════════════════════
    #  LOCK HISTORY
    # ═══════════════════════════════════════════════════════════════

    def _record_lock(
        self,
        reason: LockReason,
        details: str,
        loss_pct: float = 0.0,
    ) -> None:
        """Record a lock event."""
        record = {
            "reason": reason.value,
            "details": details,
            "loss_pct": round(loss_pct, 2),
            "timestamp": datetime.utcnow().isoformat(),
            "balance": self._safe_float("balance"),
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

    def _send_cooldown_notification(self, consec: int, loss_amount: float) -> None:
        """Send cooldown notification."""
        if not self.notifier:
            return

        message = (
            f"⏳ <b>TRADING PAUSED</b>\n\n"
            f"Reason: {consec} consecutive losses\n"
            f"Last loss: ${loss_amount:.4f}\n"
            f"Cooldown: {self.cooldown_minutes} minutes\n"
            f"\nTrading will auto-resume after cooldown."
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def _send_daily_limit_notification(self, dd_pct: float, balance: float) -> None:
        """Send daily limit notification."""
        if not self.notifier:
            return

        message = (
            f"🛑 <b>DAILY LIMIT REACHED</b>\n\n"
            f"Daily drawdown: {dd_pct:.1f}%\n"
            f"Limit: {self.max_daily_loss_pct*100:.1f}%\n"
            f"Balance: ${balance:.2f}\n"
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
            f"Balance: ${balance:.2f}\n"
            f"Initial: ${initial:.2f}\n"
            f"\n⚠️ Manual intervention required.\n"
            f"Use /reset_risk to reset baseline."
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    def _send_warning_notification(self, dd_pct: float, remaining_pct: float) -> None:
        """Send warning notification."""
        if not self.notifier:
            return

        message = (
            f"⚠️ <b>APPROACHING DAILY LIMIT</b>\n\n"
            f"Current drawdown: {dd_pct:.1f}%\n"
            f"Remaining: {remaining_pct:.1f}%\n"
            f"Limit: {self.max_daily_loss_pct*100:.1f}%"
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send notification: {e}")

    # ═══════════════════════════════════════════════════════════════
    #  DAILY RESET
    # ═══════════════════════════════════════════════════════════════

    def reset_daily(self) -> None:
        """Reset daily counters."""
        self.state.set("daily_limit_notified", False)
        self._warnings_sent_today = 0
        self._last_warning_time = None
        logger.debug("🔄 LossGuard daily counters reset")

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

        # Daily drawdown
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

            "consecutive_losses": self.state.get("consecutive_losses") or 0,
            "max_consecutive_losses": self.max_consecutive_losses,
            "win_streak": self.state.get("win_streak") or 0,
            "loss_streak": self.state.get("loss_streak") or 0,

            "cooldown_active": cooldown_remaining > 0,
            "cooldown_remaining_sec": round(cooldown_remaining),
            "cooldown_remaining_min": round(cooldown_remaining / 60, 1),

            "recovery_mode": self._is_in_recovery_mode(),
            "recovery_wins_needed": self.state.get("recovery_wins_needed", 0),
            "risk_multiplier": self.get_risk_multiplier(),

            "initial_balance": round(initial, 2),
            "start_day_balance": round(start_day, 2),
            "current_balance": round(balance, 2),

            "daily_drawdown_pct": round(daily_dd_pct, 2),
            "daily_limit_pct": round(self.max_daily_loss_pct * 100, 1),
            "daily_remaining_pct": round(max(0, self.max_daily_loss_pct * 100 - daily_dd_pct), 2),

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

    def update_notifier(self, notifier) -> None:
        """Hot-swap notifier."""
        self.notifier = notifier

    def update_kill_switch(self, kill_switch) -> None:
        """Hot-swap kill switch."""
        self.kill_switch = kill_switch

    def __repr__(self) -> str:
        status = self.get_guard_status()
        consec = self.state.get("consecutive_losses", 0)
        return (
            f"<LossGuard {status.value.upper()} | "
            f"Consec={consec}/{self.max_consecutive_losses} | "
            f"Recovery={self._is_in_recovery_mode()}>"
        )