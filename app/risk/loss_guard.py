# app/risk/loss_guard.py

"""
Advanced Capital Protection System — Production Grade (FIXED)

Multi-layer protection:
1. Daily drawdown limit (% of START-OF-DAY balance)
2. Consecutive loss limit
3. Per-trade loss limit
4. Cooldown timer (auto-resume after N minutes)
5. Emergency equity stop (total account protection)
6. Escalation to KillSwitch for severe breaches

FIXES APPLIED:
- Emergency stop no longer re-triggers after manual unlock
- Added reset_baseline() to acknowledge losses and restart
- Added unlock() method for full risk reset
- Daily reset now clears consecutive losses
- get_status() no longer calls can_trade() 3 times
"""

from datetime import datetime, timedelta, date
from typing import Tuple, Optional, Dict, List
from app.utils.logger import get_logger

logger = get_logger(__name__)


class LossGuard:
    """
    Institutional-Grade Capital Protection

    Features:
    - Daily drawdown uses START_OF_DAY balance (not current)
    - Tiered response: cooldown → kill switch escalation
    - Consecutive loss tracking with streak reset on win
    - Per-trade max loss validation
    - Cooldown timer with auto-resume
    - Emergency stop with acknowledgment (prevents re-lock loop)
    - Full audit trail
    - Status reporting for Telegram
    """

    MAX_HISTORY = 100

    def __init__(
        self,
        state_manager,
        kill_switch=None,
        max_daily_loss_pct: float = 0.05,
        max_consecutive_losses: int = 3,
        max_single_loss_pct: float = 0.02,
        cooldown_minutes: int = 60,
        emergency_drawdown_pct: float = 0.15,
        escalate_to_kill_switch: bool = True,
    ):
        self.state = state_manager
        self.kill_switch = kill_switch

        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_consecutive_losses = max_consecutive_losses
        self.max_single_loss_pct = max_single_loss_pct
        self.cooldown_minutes = cooldown_minutes
        self.emergency_drawdown_pct = emergency_drawdown_pct
        self.escalate_to_kill_switch = escalate_to_kill_switch

    # ═════════════════════════════════════════════════════
    #  MAIN GATE — Called every cycle
    # ═════════════════════════════════════════════════════

    def can_trade(self) -> Tuple[bool, str]:
        """
        Master trade permission check.

        Returns:
            (allowed: bool, reason: str)

        Called at the START of every trading cycle.
        If returns (False, reason), controller should skip the cycle.
        """
        # ── Daily reset check ─────────────────────────────────────
        self._reset_if_new_day()

        # ── Check 1: Emergency equity stop (most severe) ──────────
        # FIXED: Skip if emergency was already acknowledged
        if not self.state.get("emergency_acknowledged", False):
            emergency, emergency_dd = self._check_emergency_stop()
            if emergency:
                reason = f"Emergency stop: {emergency_dd:.1f}% total drawdown"
                self._trigger_emergency(reason, emergency_dd)
                return False, reason

        # ── Check 2: Cooldown active (check BEFORE daily/streak) ──
        cooldown_active, remaining = self._check_cooldown()
        if cooldown_active:
            reason = f"Cooldown active: {remaining:.0f}s remaining"
            return False, reason

        # ── Check 3: Daily drawdown limit ─────────────────────────
        daily_hit, daily_dd = self._check_daily_drawdown()
        if daily_hit:
            reason = f"Daily limit: {daily_dd:.1f}% drawdown (max {self.max_daily_loss_pct * 100:.1f}%)"
            self._activate_cooldown(reason, "daily_drawdown")
            return False, reason

        # ── Check 4: Consecutive loss limit ───────────────────────
        streak_hit, streak = self._check_consecutive_losses()
        if streak_hit:
            reason = f"Loss streak: {streak} consecutive losses (max {self.max_consecutive_losses})"
            self._activate_cooldown(reason, "consecutive_losses")
            return False, reason

        return True, "OK"

    # ═════════════════════════════════════════════════════
    #  RECORD TRADE RESULTS
    # ═════════════════════════════════════════════════════

    def record_loss(self, loss_amount: float, loss_pct: float = 0.0) -> Dict:
        """
        Called after a losing trade closes.

        Updates:
        - Daily PnL
        - Consecutive loss streak
        - Loss history
        """
        loss_amount = abs(loss_amount)

        daily_pnl = self.state.get("daily_pnl", 0.0) or 0.0
        new_daily_pnl = daily_pnl - loss_amount
        self.state.set("daily_pnl", new_daily_pnl)

        streak = (self.state.get("consecutive_losses", 0) or 0) + 1
        self.state.set("consecutive_losses", streak)
        self.state.set("loss_streak", streak)
        self.state.set("win_streak", 0)

        self._append_history({
            "type": "LOSS",
            "amount": round(loss_amount, 4),
            "loss_pct": round(loss_pct, 2),
            "streak": streak,
            "daily_pnl": round(new_daily_pnl, 4),
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.warning(
            f"📉 Loss recorded | -${loss_amount:.4f} ({loss_pct:.1f}%) | "
            f"Streak: {streak} | Daily PnL: ${new_daily_pnl:.2f}"
        )

        return {
            "loss_amount": loss_amount,
            "streak": streak,
            "daily_pnl": new_daily_pnl,
        }

    def record_win(self, profit_amount: float, profit_pct: float = 0.0) -> Dict:
        """
        Called after a winning trade closes.
        Resets consecutive loss streak.
        """
        daily_pnl = self.state.get("daily_pnl", 0.0) or 0.0
        new_daily_pnl = daily_pnl + profit_amount
        self.state.set("daily_pnl", new_daily_pnl)

        win_streak = (self.state.get("win_streak", 0) or 0) + 1
        self.state.set("consecutive_losses", 0)
        self.state.set("loss_streak", 0)
        self.state.set("win_streak", win_streak)

        self._append_history({
            "type": "WIN",
            "amount": round(profit_amount, 4),
            "profit_pct": round(profit_pct, 2),
            "streak": win_streak,
            "daily_pnl": round(new_daily_pnl, 4),
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(
            f"📈 Win recorded | +${profit_amount:.4f} ({profit_pct:.1f}%) | "
            f"Streak: {win_streak} | Daily PnL: ${new_daily_pnl:.2f}"
        )

        return {
            "profit_amount": profit_amount,
            "streak": win_streak,
            "daily_pnl": new_daily_pnl,
        }

    # ═════════════════════════════════════════════════════
    #  PRE-TRADE VALIDATION
    # ═════════════════════════════════════════════════════

    def validate_trade_risk(
        self,
        potential_loss: float,
        balance: float = None,
    ) -> Tuple[bool, str]:
        """
        Validate a trade BEFORE entry.
        Checks if potential loss exceeds max_single_loss_pct.
        """
        if balance is None:
            balance = self.state.get("balance", 0)

        if balance <= 0:
            return False, "Zero balance"

        potential_loss = abs(potential_loss)
        loss_pct = potential_loss / balance

        if loss_pct > self.max_single_loss_pct:
            reason = (
                f"Trade risk {loss_pct * 100:.1f}% exceeds max "
                f"{self.max_single_loss_pct * 100:.1f}%"
            )
            logger.warning(f"⚠️ Trade blocked: {reason}")
            return False, reason

        return True, "OK"

    # ═════════════════════════════════════════════════════
    #  DAILY DRAWDOWN CHECK
    # ═════════════════════════════════════════════════════

    def _check_daily_drawdown(self) -> Tuple[bool, float]:
        """
        Check if daily drawdown limit is breached.
        Uses START_OF_DAY balance, not current balance.
        """
        start_balance = self.state.get("start_of_day_balance")
        current_balance = self.state.get("balance", 0)

        if not start_balance or start_balance <= 0:
            return False, 0.0

        drawdown = start_balance - current_balance
        drawdown_pct = drawdown / start_balance

        # Only trigger if drawdown is positive (actually losing money)
        if drawdown <= 0:
            return False, 0.0

        max_loss_amount = start_balance * self.max_daily_loss_pct

        if drawdown >= max_loss_amount:
            logger.critical(
                f"🛑 Daily drawdown limit | "
                f"Lost ${drawdown:.2f} ({drawdown_pct * 100:.1f}%) | "
                f"Limit: ${max_loss_amount:.2f} ({self.max_daily_loss_pct * 100:.1f}%)"
            )
            return True, drawdown_pct * 100

        return False, drawdown_pct * 100

    # ═════════════════════════════════════════════════════
    #  CONSECUTIVE LOSS CHECK
    # ═════════════════════════════════════════════════════

    def _check_consecutive_losses(self) -> Tuple[bool, int]:
        """Check if consecutive loss limit is breached."""
        streak = self.state.get("consecutive_losses", 0) or 0

        if streak >= self.max_consecutive_losses:
            logger.warning(
                f"⚠️ Consecutive loss limit | "
                f"{streak} losses (max {self.max_consecutive_losses})"
            )
            return True, streak

        return False, streak

    # ═════════════════════════════════════════════════════
    #  EMERGENCY STOP CHECK (FIXED)
    # ═════════════════════════════════════════════════════

    def _check_emergency_stop(self) -> Tuple[bool, float]:
        """
        Check total account drawdown from initial balance.

        FIXED: Now respects 'emergency_acknowledged' flag.
        Once user acknowledges (via unlock/reset_baseline),
        this check is skipped until the flag is cleared.

        The flag is cleared when:
        - reset_baseline() is called (sets new initial_balance)
        - The bot is restarted and state is fresh

        This prevents the infinite re-lock loop where:
        old: resume → next cycle → emergency re-triggers → locked again
        new: resume+acknowledge → next cycle → skip emergency → trades normally
        """
        initial_balance = self.state.get("initial_balance")
        current_balance = self.state.get("balance", 0)

        if not initial_balance or initial_balance <= 0:
            return False, 0.0

        drawdown = initial_balance - current_balance

        # Only trigger if actually in drawdown (not profit)
        if drawdown <= 0:
            return False, 0.0

        drawdown_pct = drawdown / initial_balance

        if drawdown_pct >= self.emergency_drawdown_pct:
            logger.critical(
                f"🚨 EMERGENCY DRAWDOWN | "
                f"Lost ${drawdown:.2f} ({drawdown_pct * 100:.1f}%) of initial | "
                f"Limit: {self.emergency_drawdown_pct * 100:.1f}%"
            )
            return True, drawdown_pct * 100

        return False, drawdown_pct * 100

    # ═════════════════════════════════════════════════════
    #  COOLDOWN MANAGEMENT
    # ═════════════════════════════════════════════════════

    def _activate_cooldown(self, reason: str, trigger: str) -> None:
        """Activate cooldown timer."""
        cooldown_until = datetime.utcnow() + timedelta(
            minutes=self.cooldown_minutes
        )
        self.state.set("cooldown_until", cooldown_until.isoformat())
        self.state.set("cooldown_reason", reason)
        self.state.set("cooldown_trigger", trigger)

        self._append_history({
            "type": "COOLDOWN_START",
            "reason": reason,
            "trigger": trigger,
            "until": cooldown_until.isoformat(),
            "minutes": self.cooldown_minutes,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.warning(
            f"⏳ Cooldown activated | {self.cooldown_minutes}min | "
            f"Reason: {reason}"
        )

    def _check_cooldown(self) -> Tuple[bool, float]:
        """Check if cooldown is still active."""
        cooldown_until = self.state.get("cooldown_until")

        if not cooldown_until:
            return False, 0.0

        try:
            end_time = datetime.fromisoformat(cooldown_until)
            now = datetime.utcnow()

            if now >= end_time:
                self._clear_cooldown()
                return False, 0.0

            remaining = (end_time - now).total_seconds()
            return True, remaining

        except (ValueError, TypeError):
            self._clear_cooldown()
            return False, 0.0

    def _clear_cooldown(self) -> None:
        """Clear cooldown state."""
        self.state.set("cooldown_until", None)
        self.state.set("cooldown_reason", None)
        self.state.set("cooldown_trigger", None)

        self._append_history({
            "type": "COOLDOWN_END",
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info("✅ Cooldown expired — trading can resume")

    def clear_cooldown_manual(self, source: str = "manual") -> None:
        """Manually clear cooldown (e.g., from Telegram command)."""
        self._clear_cooldown()

        self._append_history({
            "type": "COOLDOWN_CLEARED",
            "source": source,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.warning(f"⚠️ Cooldown manually cleared by {source}")

    # ═════════════════════════════════════════════════════
    #  EMERGENCY ESCALATION (FIXED)
    # ═════════════════════════════════════════════════════

    def _trigger_emergency(self, reason: str, drawdown_pct: float) -> None:
        """
        Escalate to kill switch for severe breaches.

        FIXED: Only triggers if not already locked.
        Previously this fired EVERY cycle creating log spam
        and preventing any recovery.
        """
        # ── Don't re-trigger if already locked ────────────────────
        if self.kill_switch and self.kill_switch.is_active():
            logger.debug(
                "🚨 Emergency already active — skipping re-trigger"
            )
            return

        self._append_history({
            "type": "EMERGENCY",
            "reason": reason,
            "drawdown_pct": drawdown_pct,
            "timestamp": datetime.utcnow().isoformat(),
        })

        if self.escalate_to_kill_switch and self.kill_switch:
            self.kill_switch.activate(
                reason=reason,
                source="loss_guard",
                loss_pct=drawdown_pct,
                auto_resume_minutes=None,
            )
        else:
            self.state.set("bot_active", False)

        logger.critical(f"🚨 EMERGENCY TRIGGERED | {reason}")

    # ═════════════════════════════════════════════════════
    #  UNLOCK & RESET (NEW — FIXES THE LOCK LOOP)
    # ═════════════════════════════════════════════════════

    def unlock(self, source: str = "manual") -> Dict:
        """
        Full risk unlock — breaks out of ANY lock state.

        This is the ESCAPE HATCH that was missing.
        Called from /unlock or /resume Telegram commands.

        Actions:
        1. Clear cooldown
        2. Reset consecutive losses
        3. Acknowledge emergency (prevent re-trigger)
        4. Clear risk_locked flag

        Does NOT reset initial_balance — use reset_baseline() for that.
        """
        # Clear cooldown
        self._clear_cooldown()

        # Reset streaks
        self.state.set("consecutive_losses", 0)
        self.state.set("loss_streak", 0)

        # Acknowledge emergency — prevents re-trigger loop
        self.state.set("emergency_acknowledged", True)

        # Clear risk_locked flag
        self.state.set("risk_locked", False)

        self._append_history({
            "type": "UNLOCKED",
            "source": source,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.warning(
            f"🔓 Loss guard UNLOCKED by {source} | "
            f"Emergency acknowledged — will not re-trigger until baseline reset"
        )

        return {
            "unlocked": True,
            "source": source,
            "emergency_acknowledged": True,
            "note": "Use reset_baseline() to set new initial_balance",
        }

    def reset_baseline(self, source: str = "manual") -> Dict:
        """
        Reset initial_balance to current balance.

        This ACCEPTS the losses and starts fresh measurements.
        After this, emergency drawdown is measured from the NEW baseline.

        Called from /reset_risk Telegram command.

        Example:
            Initial was $100, now $39.58 (60% loss)
            After reset: initial=$39.58, emergency triggers at $33.64 (15% of $39.58)
        """
        current_balance = self.state.get("balance", 0)

        if current_balance <= 0:
            return {
                "reset": False,
                "reason": "Balance is zero — cannot reset baseline",
            }

        old_initial = self.state.get("initial_balance", 0)

        # ── Set new baselines ─────────────────────────────────────
        self.state.set("initial_balance", current_balance)
        self.state.set("start_of_day_balance", current_balance)
        self.state.set("peak_balance", current_balance)

        # ── Reset daily counters ──────────────────────────────────
        self.state.set("daily_pnl", 0.0)
        self.state.set("max_drawdown", 0.0)

        # ── Clear emergency acknowledgment (re-enable protection) ─
        self.state.set("emergency_acknowledged", False)

        # ── Clear all locks ───────────────────────────────────────
        self.state.set("risk_locked", False)
        self.state.set("cooldown_until", None)
        self.state.set("cooldown_reason", None)
        self.state.set("cooldown_trigger", None)

        # ── Reset streaks ─────────────────────────────────────────
        self.state.set("consecutive_losses", 0)
        self.state.set("loss_streak", 0)

        self._append_history({
            "type": "BASELINE_RESET",
            "source": source,
            "old_initial": round(old_initial, 2),
            "new_initial": round(current_balance, 2),
            "timestamp": datetime.utcnow().isoformat(),
        })

        new_emergency_threshold = current_balance * (1 - self.emergency_drawdown_pct)

        logger.warning(
            f"🔄 Baseline RESET by {source} | "
            f"Old: ${old_initial:.2f} → New: ${current_balance:.2f} | "
            f"Emergency now triggers at ${new_emergency_threshold:.2f} "
            f"({self.emergency_drawdown_pct * 100:.0f}% of ${current_balance:.2f})"
        )

        return {
            "reset": True,
            "source": source,
            "old_initial": round(old_initial, 2),
            "new_initial": round(current_balance, 2),
            "emergency_threshold": round(new_emergency_threshold, 2),
        }

    # ═════════════════════════════════════════════════════
    #  DAILY RESET (FIXED)
    # ═════════════════════════════════════════════════════

    def _reset_if_new_day(self) -> None:
        """
        Reset daily counters when date rolls over.

        FIXED: Now also resets consecutive_losses on new day.
        Yesterday's losses shouldn't block today's trading.
        """
        today = str(date.today())
        last_reset = self.state.get("last_loss_guard_reset")

        if last_reset == today:
            return

        balance = self.state.get("balance", 0)

        self.state.set("daily_pnl", 0.0)
        self.state.set("start_of_day_balance", balance)
        self.state.set("last_loss_guard_reset", today)

        # ── Clear cooldown on new day ─────────────────────────────
        self._clear_cooldown()

        # ── FIXED: Reset consecutive losses on new day ────────────
        self.state.set("consecutive_losses", 0)
        self.state.set("loss_streak", 0)

        # ── Reset trades today counter ────────────────────────────
        self.state.set("trades_today", 0)
        self.state.set("trades_done_today", 0)

        self._append_history({
            "type": "DAILY_RESET",
            "date": today,
            "start_balance": balance,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(
            f"🔄 Loss guard daily reset | {today} | "
            f"Start balance: ${balance:.2f}"
        )

    # ═════════════════════════════════════════════════════
    #  AUDIT TRAIL
    # ═════════════════════════════════════════════════════

    def _append_history(self, record: Dict) -> None:
        """Append event to loss guard history (capped)."""
        history: list = self.state.get("loss_guard_history") or []
        history.append(record)

        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]

        self.state.set("loss_guard_history", history)

    def get_history(self, last_n: int = 20) -> List[Dict]:
        """Return last N loss guard events."""
        history = self.state.get("loss_guard_history") or []
        return history[-last_n:]

    # ═════════════════════════════════════════════════════
    #  STATUS REPORTING (FIXED)
    # ═════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """
        Full loss guard status for Telegram /status or /risk_status.

        FIXED: No longer calls can_trade() three times.
        Previous version had triple evaluation causing side effects.
        """
        start_balance = self.state.get("start_of_day_balance", 0)
        current_balance = self.state.get("balance", 0)
        initial_balance = self.state.get("initial_balance", 0)

        # Daily drawdown
        daily_dd = 0.0
        daily_dd_pct = 0.0
        if start_balance and start_balance > 0:
            daily_dd = max(0, start_balance - current_balance)
            daily_dd_pct = (daily_dd / start_balance) * 100

        # Total drawdown
        total_dd = 0.0
        total_dd_pct = 0.0
        if initial_balance and initial_balance > 0:
            total_dd = max(0, initial_balance - current_balance)
            total_dd_pct = (total_dd / initial_balance) * 100

        # Cooldown
        cooldown_active, cooldown_remaining = self._check_cooldown()

        # FIXED: Call can_trade() ONCE and cache result
        trade_allowed, block_reason = self.can_trade()

        # Emergency acknowledged?
        emergency_ack = self.state.get("emergency_acknowledged", False)

        return {
            # Current state
            "can_trade": trade_allowed,
            "block_reason": block_reason if not trade_allowed else None,

            # Emergency
            "emergency_acknowledged": emergency_ack,

            # Drawdown
            "daily_drawdown": round(daily_dd, 2),
            "daily_drawdown_pct": round(daily_dd_pct, 2),
            "daily_limit_pct": self.max_daily_loss_pct * 100,
            "total_drawdown": round(total_dd, 2),
            "total_drawdown_pct": round(total_dd_pct, 2),
            "emergency_limit_pct": self.emergency_drawdown_pct * 100,

            # Streaks
            "consecutive_losses": self.state.get("consecutive_losses", 0),
            "max_consecutive_losses": self.max_consecutive_losses,
            "win_streak": self.state.get("win_streak", 0),

            # Cooldown
            "cooldown_active": cooldown_active,
            "cooldown_remaining_sec": round(cooldown_remaining, 0),
            "cooldown_reason": self.state.get("cooldown_reason", ""),

            # Balances
            "start_of_day_balance": round(start_balance, 2),
            "current_balance": round(current_balance, 2),
            "initial_balance": round(initial_balance, 2),
            "daily_pnl": round(self.state.get("daily_pnl", 0), 2),

            # Config
            "max_single_loss_pct": self.max_single_loss_pct * 100,
            "cooldown_minutes": self.cooldown_minutes,
        }

    # ═════════════════════════════════════════════════════
    #  CONVENIENCE
    # ═════════════════════════════════════════════════════

    def update_kill_switch(self, kill_switch) -> None:
        """Hot-swap kill switch reference."""
        self.kill_switch = kill_switch

    def __repr__(self) -> str:
        allowed, reason = self.can_trade()
        status = "✅ OK" if allowed else f"🛑 {reason}"
        return f"<LossGuard {status}>"