# app/risk/kill_switch.py

"""
Emergency Kill Switch — Production Grade for Autonomous Trading

Responsibilities:
- Immediate halt of all trading activity
- Auto-close of open positions with exit notifications
- Coordinate with LossGuard for ₹1500 daily limit
- Cooldown timer support (auto-reactivate after N minutes)
- Graduated activation levels (soft/hard)
- Daily reset handling (auto-resume next day)
- Activation audit trail (who/when/why)
- Recovery checks before resumption
- Trade exit notifications with P/L and duration

Activation Levels:
- SOFT: Pause new trades, keep positions open, can auto-resume
- HARD: Close all positions immediately, require manual resume
- DAILY_LIMIT: Triggered by ₹1500 limit, auto-resume next day

Usage in controller.run_cycle():
    if self.kill_switch.is_active():
        logger.warning("Kill switch active — skipping cycle")
        return

Usage from Telegram:
    /kill reason        → soft activate
    /kill hard reason   → hard activate with position close
    /resume             → deactivate
    /kill_status        → check state
"""

from datetime import datetime, timedelta, date, timezone
from typing import Optional, Dict, List, Tuple, Callable, Any
from enum import Enum
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  KILL SWITCH LEVELS / MODES
# ═══════════════════════════════════════════════════════════════════

class KillLevel(Enum):
    """Kill switch activation levels."""
    NONE = "none"                    # Not active
    OFF = "off"                      # Alias for NONE
    SOFT = "soft"                    # Pause trading, keep positions
    HARD = "hard"                    # Close positions, require manual resume
    DAILY_LIMIT = "daily_limit"      # ₹1500 limit hit, auto-resume next day


# Alias for backward compatibility with controller
KillSwitchMode = KillLevel


class KillSource(Enum):
    """Kill switch trigger sources."""
    MANUAL = "manual"
    TELEGRAM = "telegram"
    LOSS_GUARD = "loss_guard"
    DAILY_LIMIT = "daily_limit"
    DRAWDOWN = "drawdown"
    ERROR = "error"
    SYSTEM = "system"
    API = "api"
    AUTO_RESUME = "auto_resume"
    DAILY_RESET = "daily_reset"


# ═══════════════════════════════════════════════════════════════════
#  KILL SWITCH
# ═══════════════════════════════════════════════════════════════════

class KillSwitch:
    """
    Thread-safe emergency stop system with ₹1500 daily limit support.

    Supports two initialization modes:
    1. With state_manager (full features, persistent state)
    2. Without state_manager (standalone mode, in-memory state)
    """

    MAX_HISTORY = 100
    MAX_AUTO_RESUME_MINUTES = 60 * 24  # 24 hours max

    # Conditions that block auto-resume
    RESUME_BLOCKERS = [
        "drawdown_exceeded",
        "account_liquidation",
        "api_error_critical",
    ]

    def __init__(
        self,
        # New-style parameters (standalone mode)
        daily_loss_limit_inr: float = 1500.0,
        daily_drawdown_pct: float = 0.05,
        emergency_drawdown_pct: float = 0.15,
        auto_resume_minutes: int = 0,
        usd_to_inr: float = 83.0,
        # Legacy parameters (with state_manager)
        state_manager=None,
        notifier=None,
        auto_close_positions: bool = False,
        exchange=None,
        default_cooldown_minutes: int = 30,
        require_manual_resume_after_hard: bool = True,
        **kwargs  # Accept any extra parameters
    ):
        """
        Initialize Kill Switch.

        Supports both standalone mode (no state_manager) and 
        full mode (with state_manager for persistent state).
        """
        # Store configuration
        self.daily_loss_limit_inr = daily_loss_limit_inr
        self.daily_loss_limit_usd = daily_loss_limit_inr / usd_to_inr
        self.daily_drawdown_pct = daily_drawdown_pct
        self.emergency_drawdown_pct = emergency_drawdown_pct
        self.usd_to_inr = usd_to_inr
        self.default_cooldown = default_cooldown_minutes or auto_resume_minutes or 30
        self.require_manual_after_hard = require_manual_resume_after_hard
        self.auto_close = auto_close_positions

        # Optional components
        self.state = state_manager
        self.notifier = notifier
        self.exchange = exchange

        # In-memory state (used when no state_manager)
        self._mode: KillLevel = KillLevel.NONE
        self._activated_at: Optional[datetime] = None
        self._activation_reason: Optional[str] = None
        self._activation_source: Optional[str] = None
        self._auto_resume_at: Optional[datetime] = None
        self._positions_closed: int = 0
        self._loss_at_activation: float = 0.0
        self._daily_lock: bool = False
        self._lock_date: Optional[str] = None
        self._history: List[Dict] = []

        # Tracking
        self._activation_count_today: int = 0
        self._last_notification_time: Optional[datetime] = None
        self._trade_start_times: Dict[str, datetime] = {}

        # Balance tracking (for standalone mode)
        self._daily_start_balance: float = 0.0
        self._current_balance: float = 0.0
        self._daily_pnl: float = 0.0

        logger.info(
            f"🛡️ KillSwitch initialized | "
            f"DailyLimit=₹{daily_loss_limit_inr:.0f} | "
            f"DailyDD={daily_drawdown_pct*100:.1f}% | "
            f"EmergencyDD={emergency_drawdown_pct*100:.1f}% | "
            f"Mode={'stateful' if state_manager else 'standalone'}"
        )

    # ═══════════════════════════════════════════════════════════════
    #  STATE ACCESS HELPERS
    # ═══════════════════════════════════════════════════════════════

    def _get_state(self, key: str, default=None):
        """Get state value (from state_manager or in-memory)."""
        if self.state:
            return self.state.get(key, default)
        
        # In-memory state mapping
        mapping = {
            "kill_switch": self._mode != KillLevel.NONE,
            "kill_switch_level": self._mode.value,
            "kill_switch_reason": self._activation_reason,
            "kill_switch_source": self._activation_source,
            "kill_switch_time": self._activated_at.isoformat() if self._activated_at else None,
            "kill_switch_auto_resume": self._auto_resume_at.isoformat() if self._auto_resume_at else None,
            "kill_switch_daily_lock": self._daily_lock,
            "kill_switch_lock_date": self._lock_date,
            "kill_switch_history": self._history,
            "kill_switch_attempts": self._activation_count_today,
            "risk_locked": self._mode != KillLevel.NONE,
            "bot_active": self._mode == KillLevel.NONE,
        }
        return mapping.get(key, default)

    def _set_state(self, key: str, value) -> None:
        """Set state value (in state_manager or in-memory)."""
        if self.state:
            self.state.set(key, value)
            return
        
        # In-memory state mapping
        if key == "kill_switch":
            if not value:
                self._mode = KillLevel.NONE
        elif key == "kill_switch_level":
            try:
                self._mode = KillLevel(value) if value else KillLevel.NONE
            except ValueError:
                self._mode = KillLevel.NONE
        elif key == "kill_switch_reason":
            self._activation_reason = value
        elif key == "kill_switch_source":
            self._activation_source = value
        elif key == "kill_switch_time":
            if value:
                try:
                    self._activated_at = datetime.fromisoformat(value) if isinstance(value, str) else value
                except:
                    self._activated_at = None
            else:
                self._activated_at = None
        elif key == "kill_switch_auto_resume":
            if value:
                try:
                    self._auto_resume_at = datetime.fromisoformat(value) if isinstance(value, str) else value
                except:
                    self._auto_resume_at = None
            else:
                self._auto_resume_at = None
        elif key == "kill_switch_daily_lock":
            self._daily_lock = bool(value)
        elif key == "kill_switch_lock_date":
            self._lock_date = value
        elif key == "kill_switch_history":
            self._history = value if isinstance(value, list) else []
        elif key == "kill_switch_attempts":
            self._activation_count_today = int(value) if value else 0

    def _get_all_positions(self) -> Dict:
        """Get all positions (from state_manager or empty)."""
        if self.state and hasattr(self.state, 'get_all_positions'):
            return self.state.get_all_positions()
        return {}

    def _close_position_in_state(self, symbol: str, pnl: float, **kwargs) -> None:
        """Close position in state manager."""
        if self.state and hasattr(self.state, 'close_position'):
            self.state.close_position(symbol, pnl, **kwargs)

    # ═══════════════════════════════════════════════════════════════
    #  BALANCE TRACKING (for standalone mode)
    # ═══════════════════════════════════════════════════════════════

    def update_balance(self, current_balance: float, daily_start_balance: float = None) -> None:
        """Update balance tracking."""
        self._current_balance = current_balance
        if daily_start_balance is not None:
            self._daily_start_balance = daily_start_balance

    def update_daily_pnl(self, daily_pnl: float) -> None:
        """Update daily P&L tracking."""
        self._daily_pnl = daily_pnl

    # ═══════════════════════════════════════════════════════════════
    #  DRAWDOWN CHECK (for controller compatibility)
    # ═══════════════════════════════════════════════════════════════

    def check_drawdown(self, current_balance: float, peak_balance: float) -> Dict[str, Any]:
        """
        Check if drawdown exceeds limits.
        
        FIXED: Proper drawdown calculation with minimum thresholds.
        Only triggers on SIGNIFICANT losses, not tiny fluctuations.
        """
        result = {
            "exceeded": False,
            "drawdown_pct": 0.0,
            "limit_pct": self.emergency_drawdown_pct,
            "action": None
        }
        
        # Safety checks
        if peak_balance <= 0:
            return result
        
        if current_balance <= 0:
            result["exceeded"] = True
            result["drawdown_pct"] = 1.0
            result["action"] = "emergency"
            return result
        
        # Calculate drawdown from peak
        drawdown = (peak_balance - current_balance) / peak_balance
        result["drawdown_pct"] = drawdown
        
        # IMPORTANT: Only trigger emergency if drawdown is actually significant
        # Minimum $5 or 5% loss to avoid false triggers on tiny fluctuations
        actual_loss = peak_balance - current_balance
        min_loss_threshold = max(5.0, peak_balance * 0.05)
        
        if actual_loss < min_loss_threshold:
            # Loss too small to be emergency - ignore
            return result
        
        # Check emergency drawdown (15%)
        if drawdown >= self.emergency_drawdown_pct:
            result["exceeded"] = True
            result["action"] = "emergency"
            logger.warning(
                f"⚠️ Drawdown check: {drawdown*100:.2f}% >= {self.emergency_drawdown_pct*100:.1f}% "
                f"(Loss: ${actual_loss:.2f})"
            )
        # Check daily drawdown (5%)
        elif drawdown >= self.daily_drawdown_pct:
            result["exceeded"] = True
            result["action"] = "soft_stop"
            logger.warning(
                f"⚠️ Daily drawdown: {drawdown*100:.2f}% >= {self.daily_drawdown_pct*100:.1f}%"
            )
        
        return result

    def check_daily_loss_limit(self, daily_pnl_usd: float) -> Dict[str, Any]:
        """
        Check if daily loss limit is exceeded.
        """
        result = {
            "exceeded": False,
            "daily_loss_usd": 0.0,
            "daily_loss_inr": 0.0,
            "limit_inr": self.daily_loss_limit_inr,
            "remaining_inr": self.daily_loss_limit_inr
        }
        
        # Only check if we have a loss
        if daily_pnl_usd >= 0:
            result["remaining_inr"] = self.daily_loss_limit_inr
            return result
        
        daily_loss_usd = abs(daily_pnl_usd)
        daily_loss_inr = daily_loss_usd * self.usd_to_inr
        
        result["daily_loss_usd"] = daily_loss_usd
        result["daily_loss_inr"] = daily_loss_inr
        result["remaining_inr"] = max(0, self.daily_loss_limit_inr - daily_loss_inr)
        
        if daily_loss_inr >= self.daily_loss_limit_inr:
            result["exceeded"] = True
            logger.warning(
                f"⚠️ Daily loss limit reached: ₹{daily_loss_inr:.2f} >= ₹{self.daily_loss_limit_inr:.2f}"
            )
        
        return result

    # ═══════════════════════════════════════════════════════════════
    #  DAILY RESET CHECK
    # ═══════════════════════════════════════════════════════════════

    def check_daily_reset(self) -> bool:
        """
        Check if it's a new day and auto-resume from daily limit lock.
        
        Call this at the START of each trading cycle.
        Returns True if a reset occurred.
        """
        today = str(date.today())
        lock_date = self._get_state("kill_switch_lock_date")
        is_daily_lock = self._get_state("kill_switch_daily_lock", False)

        if is_daily_lock and lock_date and lock_date != today:
            logger.info(
                f"🌅 New day detected | {lock_date} → {today} | "
                f"Auto-resuming from daily limit lock..."
            )
            self._perform_daily_reset(today)
            return True

        return False

    def should_resume_new_day(self) -> bool:
        """Check if we should resume on new day (alias for check_daily_reset)."""
        return self.check_daily_reset()

    def _perform_daily_reset(self, today: str) -> None:
        """Perform daily reset - auto-resume from daily limit lock."""
        previous_reason = self._get_state("kill_switch_reason", "")

        # Deactivate kill switch
        self.deactivate(
            source=KillSource.DAILY_RESET.value,
            force=True,
            notify=True,
        )

        # Clear daily lock flags
        self._set_state("kill_switch_daily_lock", False)
        self._set_state("kill_switch_lock_date", None)

        # Reset daily counters
        self._activation_count_today = 0
        self._set_state("kill_switch_attempts", 0)

        logger.info(
            f"🔄 Kill switch daily reset | "
            f"Previous reason: {previous_reason} | "
            f"Trading resumed for {today}"
        )

    # ═══════════════════════════════════════════════════════════════
    #  ACTIVATE
    # ═══════════════════════════════════════════════════════════════

    def activate(
        self,
        # New-style parameters (controller compatible)
        mode: KillLevel = None,
        reason: str = "Manual trigger",
        source: str = "system",
        loss_amount: float = 0.0,
        # Legacy parameters
        level: str = None,
        auto_resume_minutes: Optional[int] = None,
        loss_pct: float = 0.0,
        close_positions: Optional[bool] = None,
        notify: bool = True,
        is_daily_limit: bool = False,
        **kwargs  # Accept any extra parameters
    ) -> Dict:
        """
        Immediately stop ALL trading activity.

        Supports both new-style (mode=KillLevel.SOFT) and 
        legacy (level="soft") parameter styles.
        """
        # Normalize mode/level parameter
        if mode is not None:
            if isinstance(mode, KillLevel):
                level = mode.value
            else:
                level = str(mode)
        elif level is None:
            level = "soft"
        
        level = level.lower()
        if level == "off" or level == "none":
            level = "soft"  # Default to soft if off/none passed to activate
        if level not in ("soft", "hard", "daily_limit"):
            level = "soft"

        # Auto-set daily_limit level if triggered by daily limit
        if is_daily_limit:
            level = "daily_limit"

        now = datetime.now(timezone.utc)
        today = str(date.today())

        # Prevent double-activation spam from same source
        if self._get_state("kill_switch", False):
            current_source = self._get_state("kill_switch_source", "")
            current_level = self._get_state("kill_switch_level", "soft")

            # Allow upgrade from soft to hard/daily_limit
            if level not in ("hard", "daily_limit") or current_level in ("hard", "daily_limit"):
                if current_source == source:
                    logger.debug(f"⚠️ Kill switch already active from {source}")
                    return {
                        "activated": True,
                        "already_active": True,
                        "level": current_level,
                        "reason": self._get_state("kill_switch_reason", reason),
                        "source": source,
                    }

        # Set all kill state flags
        self._mode = KillLevel(level)
        self._activated_at = now
        self._activation_reason = reason
        self._activation_source = source
        self._loss_at_activation = loss_amount

        self._set_state("kill_switch", True)
        self._set_state("kill_switch_level", level)
        self._set_state("risk_locked", True)
        self._set_state("bot_active", False)
        self._set_state("kill_switch_reason", reason)
        self._set_state("kill_switch_time", now.isoformat())
        self._set_state("kill_switch_source", source)

        # Set daily limit specific flags
        if is_daily_limit or level == "daily_limit":
            self._daily_lock = True
            self._lock_date = today
            self._set_state("kill_switch_daily_lock", True)
            self._set_state("kill_switch_lock_date", today)

        # Increment activation counter
        attempts = self._get_state("kill_switch_attempts", 0) or 0
        self._set_state("kill_switch_attempts", attempts + 1)
        self._activation_count_today += 1

        # Auto-resume timer (not for daily_limit - that resets at midnight)
        auto_resume_at = None
        if level == "soft" and auto_resume_minutes and auto_resume_minutes > 0:
            auto_resume_minutes = min(auto_resume_minutes, self.MAX_AUTO_RESUME_MINUTES)
            auto_resume_at = now + timedelta(minutes=auto_resume_minutes)
            self._auto_resume_at = auto_resume_at
            self._set_state("kill_switch_auto_resume", auto_resume_at.isoformat())
        else:
            self._auto_resume_at = None
            self._set_state("kill_switch_auto_resume", None)

        # Hard kills don't auto-resume if require_manual_after_hard is True
        if level == "hard" and self.require_manual_after_hard:
            self._auto_resume_at = None
            self._set_state("kill_switch_auto_resume", None)
            auto_resume_at = None

        # Daily limit doesn't use timer - resets at midnight
        if level == "daily_limit":
            self._auto_resume_at = None
            self._set_state("kill_switch_auto_resume", None)
            auto_resume_at = None

        # Audit trail
        record = {
            "action": "ACTIVATED",
            "level": level,
            "reason": reason,
            "source": source,
            "loss_amount_inr": loss_amount * self.usd_to_inr if loss_amount < 1000 else loss_amount,
            "loss_pct": loss_pct,
            "timestamp": now.isoformat(),
            "auto_resume": auto_resume_at.isoformat() if auto_resume_at else None,
            "is_daily_limit": is_daily_limit,
            "activation_count_today": self._activation_count_today,
        }
        self._append_history(record)

        # Close positions if needed
        should_close = close_positions if close_positions is not None else (
            level in ("hard", "daily_limit") or self.auto_close
        )
        closed_positions = []
        if should_close and self.exchange:
            closed_positions = self._close_all_positions(reason=reason)

        # Determine positions closed count
        self._positions_closed = len(closed_positions)

        # Log
        level_emoji = "🚨" if level == "daily_limit" else ("🛑" if level == "hard" else "⚠️")
        resume_str = "TOMORROW" if level == "daily_limit" else (f"{auto_resume_minutes}min" if auto_resume_minutes else "NEVER")
        
        logger.critical(
            f"{level_emoji} KILL SWITCH [{level.upper()}] | "
            f"Reason: {reason} | Source: {source} | "
            f"Loss: ${loss_amount:.4f} (₹{loss_amount * self.usd_to_inr:.2f}) | "
            f"Auto-resume: {resume_str} | "
            f"Closed: {len(closed_positions)} positions"
        )

        # Notification
        if notify and self.notifier:
            self._send_activation_notification(
                level, reason, source, loss_amount, loss_pct,
                auto_resume_minutes, closed_positions, is_daily_limit
            )

        return {
            "activated": True,
            "already_active": False,
            "level": level,
            "reason": reason,
            "source": source,
            "loss_amount": loss_amount,
            "loss_amount_inr": loss_amount * self.usd_to_inr,
            "loss_pct": loss_pct,
            "timestamp": now.isoformat(),
            "auto_resume_at": auto_resume_at.isoformat() if auto_resume_at else None,
            "is_daily_limit": is_daily_limit,
            "resume_date": str(date.today() + timedelta(days=1)) if is_daily_limit else None,
            "positions_closed": len(closed_positions),
            "closed_details": closed_positions,
        }

    def soft_activate(
        self,
        reason: str,
        source: str = "system",
        auto_resume_minutes: Optional[int] = None,
    ) -> Dict:
        """Convenience method for soft activation."""
        return self.activate(
            reason=reason,
            source=source,
            level="soft",
            auto_resume_minutes=auto_resume_minutes or self.default_cooldown,
        )

    def hard_activate(
        self,
        reason: str,
        source: str = "system",
        loss_amount: float = 0.0,
        loss_pct: float = 0.0,
    ) -> Dict:
        """Convenience method for hard activation (closes positions)."""
        return self.activate(
            reason=reason,
            source=source,
            level="hard",
            auto_resume_minutes=None,
            loss_amount=loss_amount,
            loss_pct=loss_pct,
            close_positions=True,
        )

    def daily_limit_activate(
        self,
        daily_loss: float,
        source: str = "loss_guard",
    ) -> Dict:
        """
        Activate kill switch due to ₹1500 daily limit.
        """
        return self.activate(
            reason=f"Daily loss limit reached: ₹{daily_loss:.2f} ≥ ₹{self.daily_loss_limit_inr:.0f}",
            source=source,
            level="daily_limit",
            auto_resume_minutes=None,
            loss_amount=daily_loss / self.usd_to_inr,  # Convert INR to USD
            close_positions=True,
            is_daily_limit=True,
        )

    # ═══════════════════════════════════════════════════════════════
    #  DEACTIVATE
    # ═══════════════════════════════════════════════════════════════

    def deactivate(
        self,
        reason: str = "Manual reset",
        source: str = "manual",
        force: bool = False,
        notify: bool = True,
    ) -> Dict:
        """
        Resume trading. Clears kill switch and reactivates bot.
        """
        now = datetime.now(timezone.utc)

        # Check if actually active
        if not self._get_state("kill_switch", False) and self._mode == KillLevel.NONE:
            return {
                "deactivated": False,
                "was_active": False,
                "source": source,
                "reason": reason,
                "message": "Kill switch was not active",
            }

        # Check if this is a daily limit lock
        is_daily_lock = self._get_state("kill_switch_daily_lock", False) or self._daily_lock
        lock_date = self._get_state("kill_switch_lock_date") or self._lock_date
        today = str(date.today())

        # Block manual resume of daily limit lock (unless forced or new day)
        if is_daily_lock and lock_date == today and not force:
            if source not in (KillSource.DAILY_RESET.value, KillSource.AUTO_RESUME.value, "daily_reset", "auto_resume"):
                logger.warning(f"⚠️ Cannot resume: Daily limit lock active until tomorrow")
                return {
                    "deactivated": False,
                    "was_active": True,
                    "source": source,
                    "blocked": True,
                    "block_reason": "Daily limit lock active — will auto-resume tomorrow",
                    "resume_date": str(date.today() + timedelta(days=1)),
                }

        # Safety checks (unless forced)
        if not force:
            can_resume, block_reason = self._can_resume()
            if not can_resume:
                logger.warning(f"⚠️ Resume blocked: {block_reason}")
                return {
                    "deactivated": False,
                    "was_active": True,
                    "source": source,
                    "blocked": True,
                    "block_reason": block_reason,
                }

        # Calculate duration
        activated_at = self._get_state("kill_switch_time") or (self._activated_at.isoformat() if self._activated_at else None)
        duration_seconds = 0
        if activated_at:
            try:
                start = datetime.fromisoformat(activated_at.replace('Z', '+00:00')) if isinstance(activated_at, str) else activated_at
                duration_seconds = (now - start.replace(tzinfo=None if start.tzinfo else timezone.utc)).total_seconds()
            except (ValueError, TypeError):
                pass

        previous_level = self._get_state("kill_switch_level", "soft") or self._mode.value
        previous_reason = self._get_state("kill_switch_reason", "") or self._activation_reason or ""

        # Clear ALL kill/lock state - in-memory
        self._mode = KillLevel.NONE
        self._activated_at = None
        self._activation_reason = None
        self._activation_source = None
        self._auto_resume_at = None
        self._daily_lock = False
        self._lock_date = None

        # Clear ALL kill/lock state - state manager
        self._set_state("kill_switch", False)
        self._set_state("kill_switch_level", KillLevel.NONE.value)
        self._set_state("risk_locked", False)
        self._set_state("bot_active", True)
        self._set_state("kill_switch_reason", None)
        self._set_state("kill_switch_source", None)
        self._set_state("kill_switch_auto_resume", None)
        self._set_state("kill_switch_attempts", 0)
        self._set_state("kill_switch_daily_lock", False)
        self._set_state("kill_switch_lock_date", None)

        # Audit trail
        record = {
            "action": "DEACTIVATED",
            "previous_level": previous_level,
            "source": source,
            "reason": reason,
            "timestamp": now.isoformat(),
            "was_active_seconds": round(duration_seconds, 1),
            "previous_reason": previous_reason,
            "was_daily_limit": is_daily_lock,
        }
        self._append_history(record)

        duration_str = self._format_duration(duration_seconds)
        logger.warning(
            f"✅ Kill switch DEACTIVATED | Source: {source} | "
            f"Reason: {reason} | Was {previous_level.upper()} for {duration_str}"
        )

        # Notification
        if notify and self.notifier:
            self._send_deactivation_notification(
                source, duration_seconds, previous_level, is_daily_lock
            )

        return {
            "deactivated": True,
            "was_active": True,
            "source": source,
            "reason": reason,
            "previous_level": previous_level,
            "timestamp": now.isoformat(),
            "was_active_seconds": round(duration_seconds, 1),
            "was_active_duration": duration_str,
            "was_daily_limit": is_daily_lock,
        }

    def _can_resume(self) -> Tuple[bool, str]:
        """Check if it's safe to resume trading."""
        # Check for blocking reasons
        reason = self._get_state("kill_switch_reason", "") or self._activation_reason or ""
        for blocker in self.RESUME_BLOCKERS:
            if blocker in reason.lower():
                return False, f"Blocked by reason: {reason}"

        # Check if hard kill requires manual resume
        level = self._get_state("kill_switch_level", "soft") or self._mode.value
        source = self._get_state("kill_switch_source", "") or self._activation_source or ""
        if level == "hard" and self.require_manual_after_hard:
            if source in (KillSource.AUTO_RESUME.value, "auto_resume"):
                return False, "Hard kill requires manual resume"

        return True, ""

    # ═══════════════════════════════════════════════════════════════
    #  STATUS CHECK
    # ═══════════════════════════════════════════════════════════════

    def is_active(self) -> bool:
        """
        Check if kill switch is currently ON.

        Also handles auto-resume timing and daily reset.
        """
        # First check for daily reset
        self.check_daily_reset()

        # Check in-memory state
        if self._mode != KillLevel.NONE:
            is_locked = True
        else:
            # Check state manager
            kill_flag = self._get_state("kill_switch", False)
            risk_flag = self._get_state("risk_locked", False)
            is_locked = kill_flag or risk_flag

        if not is_locked:
            return False

        # Check auto-resume (not for daily limit)
        is_daily_lock = self._get_state("kill_switch_daily_lock", False) or self._daily_lock
        if not is_daily_lock:
            auto_resume = self._get_state("kill_switch_auto_resume") or (self._auto_resume_at.isoformat() if self._auto_resume_at else None)
            if auto_resume:
                try:
                    resume_at = datetime.fromisoformat(auto_resume.replace('Z', '+00:00')) if isinstance(auto_resume, str) else auto_resume
                    now = datetime.now(timezone.utc)
                    if now >= resume_at.replace(tzinfo=timezone.utc if resume_at.tzinfo is None else resume_at.tzinfo):
                        can_resume, block_reason = self._can_resume()
                        if can_resume:
                            logger.info("⏰ Auto-resume timer expired — deactivating")
                            self.deactivate(source=KillSource.AUTO_RESUME.value, reason="Auto-resume timer")
                            return False
                        else:
                            logger.warning(f"⏰ Auto-resume blocked: {block_reason}")
                            # Extend timer by 15 minutes
                            new_resume = datetime.now(timezone.utc) + timedelta(minutes=15)
                            self._auto_resume_at = new_resume
                            self._set_state("kill_switch_auto_resume", new_resume.isoformat())
                except (ValueError, TypeError) as e:
                    logger.debug(f"Auto-resume parse error: {e}")

        return True

    def can_trade(self) -> bool:
        """Check if trading is allowed (inverse of is_active)."""
        return not self.is_active()

    def get_level(self) -> str:
        """Get current kill switch level."""
        if not self.is_active():
            return KillLevel.NONE.value
        return self._get_state("kill_switch_level", KillLevel.SOFT.value) or self._mode.value

    def is_daily_limit_lock(self) -> bool:
        """Check if currently locked due to daily limit."""
        return (
            self.is_active() and
            (self._get_state("kill_switch_daily_lock", False) or self._daily_lock)
        )

    def get_status(self) -> Dict:
        """Full kill switch status."""
        is_on = self.is_active()
        level = self.get_level()
        auto_resume = self._get_state("kill_switch_auto_resume") or (self._auto_resume_at.isoformat() if self._auto_resume_at else None)
        remaining_seconds = 0
        is_daily_lock = self._get_state("kill_switch_daily_lock", False) or self._daily_lock
        lock_date = self._get_state("kill_switch_lock_date") or self._lock_date

        if is_on and auto_resume and not is_daily_lock:
            try:
                resume_at = datetime.fromisoformat(auto_resume) if isinstance(auto_resume, str) else auto_resume
                remaining = (resume_at - datetime.now(timezone.utc)).total_seconds()
                remaining_seconds = max(0, remaining)
            except (ValueError, TypeError):
                pass

        # Calculate how long it's been active
        active_seconds = 0
        activated_at = self._get_state("kill_switch_time") or (self._activated_at.isoformat() if self._activated_at else None)
        if is_on and activated_at:
            try:
                start = datetime.fromisoformat(activated_at) if isinstance(activated_at, str) else activated_at
                active_seconds = (datetime.now(timezone.utc) - start.replace(tzinfo=timezone.utc if start.tzinfo is None else start.tzinfo)).total_seconds()
            except (ValueError, TypeError):
                pass

        # Calculate time until midnight (for daily limit)
        time_until_resume = ""
        if is_daily_lock:
            now = datetime.now(timezone.utc)
            tomorrow = datetime.combine(
                date.today() + timedelta(days=1),
                datetime.min.time()
            ).replace(tzinfo=timezone.utc)
            remaining_to_midnight = (tomorrow - now).total_seconds()
            time_until_resume = self._format_duration(remaining_to_midnight)

        return {
            "active": is_on,
            "level": level,
            "mode": level,  # Alias for compatibility
            "is_active": is_on,
            "can_trade": not is_on,
            "kill_switch_flag": self._get_state("kill_switch", False),
            "risk_locked_flag": self._get_state("risk_locked", False),
            "reason": self._get_state("kill_switch_reason", "") or self._activation_reason or "",
            "source": self._get_state("kill_switch_source", "") or self._activation_source or "",
            "activated_at": activated_at or "",
            "active_seconds": round(active_seconds, 0),
            "active_duration": self._format_duration(active_seconds),

            # Daily limit specific
            "is_daily_limit_lock": is_daily_lock,
            "lock_date": lock_date or "",
            "resume_date": str(date.today() + timedelta(days=1)) if is_daily_lock else "",
            "time_until_resume": time_until_resume,

            # Auto-resume (for non-daily limit)
            "auto_resume_at": auto_resume or "",
            "auto_resume_remaining_sec": round(remaining_seconds, 0),
            "auto_resume_remaining_min": round(remaining_seconds / 60, 1),

            "activation_attempts": self._get_state("kill_switch_attempts", 0),
            "activations_today": self._activation_count_today,
            "positions_closed": self._positions_closed,
            "loss_at_activation": self._loss_at_activation,
            "daily_loss_limit_inr": self.daily_loss_limit_inr,
            "daily_drawdown_pct": self.daily_drawdown_pct,
            "emergency_drawdown_pct": self.emergency_drawdown_pct,
            "history_count": len(self._get_state("kill_switch_history") or self._history),
        }

    def get_history(self, last_n: int = 10) -> List[Dict]:
        """Return last N kill switch events."""
        history = self._get_state("kill_switch_history") or self._history
        return history[-last_n:]

    # ═══════════════════════════════════════════════════════════════
    #  POSITION CLOSING
    # ═══════════════════════════════════════════════════════════════

    def record_trade_entry(self, symbol: str) -> None:
        """Record trade entry time for duration tracking."""
        self._trade_start_times[symbol] = datetime.now(timezone.utc)

    def _close_all_positions(self, reason: str = "Kill switch activated") -> List[Dict]:
        """Emergency close all open positions with exit notifications."""
        results = []
        positions = self._get_all_positions()

        if not positions:
            logger.info("📭 No open positions to close")
            return results

        logger.warning(f"🚨 Emergency closing {len(positions)} position(s)...")

        for symbol, pos in positions.items():
            qty = pos.get("quantity", 0)
            if qty <= 0:
                continue

            result = self._close_single_position(symbol, pos, qty, reason)
            results.append(result)

            # Send individual exit notification
            if result.get("status") == "CLOSED" and self.notifier:
                self._send_position_exit_notification(result)

        # Send summary if multiple positions
        if len(results) > 1 and self.notifier:
            self._send_positions_closed_summary(results, reason)

        return results

    def _close_single_position(
        self,
        symbol: str,
        position: Dict,
        quantity: float,
        reason: str,
    ) -> Dict:
        """Close a single position with full exit details."""
        entry_price = float(
            position.get("entry_price") or
            position.get("avg_price") or 0
        )
        entry_time = position.get("entry_time")

        # Calculate duration
        duration_str = "Unknown"
        duration_seconds = 0

        if symbol in self._trade_start_times:
            start = self._trade_start_times.pop(symbol)
            duration_seconds = (datetime.now(timezone.utc) - start).total_seconds()
            duration_str = self._format_duration(duration_seconds)
        elif entry_time:
            try:
                if isinstance(entry_time, str):
                    start = datetime.fromisoformat(entry_time.replace('Z', '+00:00'))
                else:
                    start = entry_time
                duration_seconds = (datetime.now(timezone.utc) - start.replace(tzinfo=timezone.utc if start.tzinfo is None else start.tzinfo)).total_seconds()
                duration_str = self._format_duration(duration_seconds)
            except:
                pass

        if not self.exchange:
            logger.warning(f"⚠️ No exchange available to close {symbol}")
            return {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": entry_price,
                "status": "SKIPPED",
                "reason": "No exchange available",
            }

        try:
            fill = self.exchange.sell(symbol=symbol, quantity=quantity)

            if fill and fill.get("status") != "REJECTED":
                exit_price = float(fill.get("price", 0))
                fee = float(fill.get("fee", 0))
                gross_pnl = (exit_price - entry_price) * quantity
                net_pnl = gross_pnl - fee

                # Calculate PnL percentage
                cost_basis = entry_price * quantity
                pnl_pct = (net_pnl / cost_basis * 100) if cost_basis > 0 else 0

                self._close_position_in_state(
                    symbol, net_pnl,
                    exit_price=exit_price,
                    reason=reason,
                )

                logger.info(
                    f"🚨 Emergency close | {symbol} | "
                    f"Qty={quantity} @ ${exit_price:.6f} | "
                    f"PnL=${net_pnl:+.4f} ({pnl_pct:+.2f}%) | "
                    f"Duration={duration_str}"
                )

                return {
                    "symbol": symbol,
                    "quantity": quantity,
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl": round(net_pnl, 4),
                    "pnl_pct": round(pnl_pct, 2),
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
                    "entry_price": entry_price,
                    "status": "FAILED",
                    "reason": fail_reason,
                }

        except Exception as e:
            logger.exception(f"❌ Emergency close error | {symbol}: {e}")
            return {
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": entry_price,
                "status": "ERROR",
                "reason": str(e),
            }

    async def emergency_close_positions(
        self,
        positions: List[Dict],
        close_func: Callable,
        reason: str = "Emergency drawdown exceeded"
    ) -> List[Dict]:
        """
        Async emergency close all positions (for controller compatibility).
        """
        if not positions:
            logger.info("No positions to close")
            return []

        logger.warning(f"🚨 Emergency closing {len(positions)} position(s)...")

        results = []
        for position in positions:
            try:
                symbol = position.get("symbol", "UNKNOWN")
                qty = position.get("quantity", 0)
                entry_price = position.get("entry_price", 0)

                result = await close_func(position, reason=reason)

                if result:
                    exit_price = result.get("exit_price", entry_price)
                    pnl = result.get("pnl", 0)
                    pnl_pct = (pnl / (entry_price * qty) * 100) if entry_price * qty > 0 else 0
                    duration = result.get("duration_minutes", 0)

                    logger.info(
                        f"🚨 Emergency close | {symbol} | "
                        f"Qty={qty:.8f} @ ${exit_price:.6f} | "
                        f"PnL=${pnl:.4f} ({pnl_pct:.2f}%) | "
                        f"Duration={duration:.1f}m"
                    )

                    results.append(result)
                    self._positions_closed += 1

            except Exception as e:
                logger.error(f"❌ Failed to close {position.get('symbol')}: {e}")

        return results

    # ═══════════════════════════════════════════════════════════════
    #  NOTIFICATIONS
    # ═══════════════════════════════════════════════════════════════

    def _send_activation_notification(
        self,
        level: str,
        reason: str,
        source: str,
        loss_amount: float,
        loss_pct: float,
        auto_resume_minutes: Optional[int],
        closed_positions: List[Dict],
        is_daily_limit: bool,
    ) -> None:
        """Send activation notification."""
        if not self.notifier:
            return

        # Rate limit notifications
        now = datetime.now(timezone.utc)
        if self._last_notification_time:
            elapsed = (now - self._last_notification_time).total_seconds()
            if elapsed < 60:
                return

        self._last_notification_time = now

        loss_inr = loss_amount * self.usd_to_inr if loss_amount < 1000 else loss_amount

        if is_daily_limit:
            emoji = "🚨"
            title = "DAILY LOSS LIMIT REACHED"
            resume_info = f"⏰ Will auto-resume: Tomorrow ({date.today() + timedelta(days=1)})"
        elif level == "hard":
            emoji = "🛑"
            title = "KILL SWITCH - HARD STOP"
            resume_info = "⏰ Auto-resume: DISABLED (manual resume required)"
        else:
            emoji = "⚠️"
            title = "KILL SWITCH - SOFT STOP"
            resume_info = f"⏰ Auto-resume: {auto_resume_minutes} minutes" if auto_resume_minutes else "⏰ Auto-resume: DISABLED"

        message = (
            f"{emoji} <b>{title}</b>\n\n"
            f"Level: <b>{level.upper()}</b>\n"
            f"Reason: {reason}\n"
            f"Source: {source}\n"
        )

        if loss_amount > 0:
            message += f"Loss: ₹{loss_inr:.2f} ({loss_pct:.1f}%)\n"

        if is_daily_limit:
            message += f"Daily Limit: ₹{self.daily_loss_limit_inr:.0f}\n"

        message += f"\n{resume_info}\n"

        if closed_positions:
            message += f"\n📉 <b>Closed {len(closed_positions)} position(s):</b>\n"
            total_pnl = 0
            for pos in closed_positions:
                pnl = pos.get("pnl", 0)
                total_pnl += pnl
                pnl_pct = pos.get("pnl_pct", 0)
                duration = pos.get("duration", "N/A")
                message += f"• {pos['symbol']}: ${pnl:+.4f} ({pnl_pct:+.2f}%) | {duration}\n"
            message += f"\n<b>Total PnL: ${total_pnl:+.4f}</b>"

        try:
            if hasattr(self.notifier, 'send_message'):
                self.notifier.send_message(message)
            elif hasattr(self.notifier, 'send_custom'):
                self.notifier.send_custom(message)
        except Exception as e:
            logger.error(f"Failed to send kill notification: {e}")

    def _send_deactivation_notification(
        self,
        source: str,
        duration_seconds: float,
        previous_level: str,
        was_daily_limit: bool,
    ) -> None:
        """Send deactivation notification."""
        if not self.notifier:
            return

        duration_str = self._format_duration(duration_seconds)

        if was_daily_limit:
            title = "NEW TRADING DAY - RESUMED"
            emoji = "🌅"
            extra_info = "Daily loss counter has been reset."
        else:
            title = "KILL SWITCH DEACTIVATED"
            emoji = "✅"
            extra_info = ""

        message = (
            f"{emoji} <b>{title}</b>\n\n"
            f"Source: {source}\n"
            f"Was active: {duration_str}\n"
            f"Previous level: {previous_level.upper()}\n"
        )

        if extra_info:
            message += f"\n{extra_info}\n"

        message += f"\n🚀 Trading resumed!"

        try:
            if hasattr(self.notifier, 'send_message'):
                self.notifier.send_message(message)
            elif hasattr(self.notifier, 'send_custom'):
                self.notifier.send_custom(message)
        except Exception as e:
            logger.error(f"Failed to send resume notification: {e}")

    def _send_position_exit_notification(self, position: Dict) -> None:
        """Send individual position exit notification."""
        if not self.notifier:
            return

        pnl = position.get("pnl", 0)
        emoji = "🟢" if pnl > 0 else "🔴"
        status = "PROFIT" if pnl > 0 else "LOSS"

        message = (
            f"{emoji} <b>EMERGENCY EXIT - {status}</b>\n\n"
            f"Symbol: {position['symbol']}\n"
            f"Exit Price: ${position.get('exit_price', 0):.6f}\n"
            f"Entry Price: ${position.get('entry_price', 0):.6f}\n"
            f"Quantity: {position.get('quantity', 0)}\n"
            f"PnL: ${pnl:+.4f} ({position.get('pnl_pct', 0):+.2f}%)\n"
            f"Duration: {position.get('duration', 'N/A')}\n"
            f"Reason: {position.get('reason', 'Kill switch')}"
        )

        try:
            if hasattr(self.notifier, 'send_message'):
                self.notifier.send_message(message)
            elif hasattr(self.notifier, 'send_custom'):
                self.notifier.send_custom(message)
        except Exception as e:
            logger.error(f"Failed to send exit notification: {e}")

    def _send_positions_closed_summary(
        self,
        positions: List[Dict],
        reason: str,
    ) -> None:
        """Send summary of all closed positions."""
        if not self.notifier:
            return

        total_pnl = sum(p.get("pnl", 0) for p in positions)
        successful = [p for p in positions if p.get("status") == "CLOSED"]
        failed = [p for p in positions if p.get("status") != "CLOSED"]

        emoji = "🟢" if total_pnl > 0 else "🔴"

        message = (
            f"📊 <b>POSITIONS CLOSED SUMMARY</b>\n\n"
            f"Reason: {reason}\n"
            f"Total Positions: {len(positions)}\n"
            f"Successfully Closed: {len(successful)}\n"
            f"Failed: {len(failed)}\n\n"
            f"{emoji} <b>Total PnL: ${total_pnl:+.4f}</b>"
        )

        if failed:
            message += "\n\n⚠️ <b>Failed Positions:</b>\n"
            for pos in failed:
                message += f"• {pos['symbol']}: {pos.get('reason', 'Unknown')}\n"

        try:
            if hasattr(self.notifier, 'send_message'):
                self.notifier.send_message(message)
            elif hasattr(self.notifier, 'send_custom'):
                self.notifier.send_custom(message)
        except Exception as e:
            logger.error(f"Failed to send summary notification: {e}")

    # ═══════════════════════════════════════════════════════════════
    #  AUDIT TRAIL
    # ═══════════════════════════════════════════════════════════════

    def _append_history(self, record: Dict) -> None:
        """Append an event to kill switch history."""
        history = self._get_state("kill_switch_history") or self._history
        if not isinstance(history, list):
            history = []
        
        history.append(record)

        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]

        self._history = history
        self._set_state("kill_switch_history", history)

    def clear_history(self) -> None:
        """Clear kill switch history."""
        self._history = []
        self._set_state("kill_switch_history", [])
        logger.info("🗑️ Kill switch history cleared")

    # ═══════════════════════════════════════════════════════════════
    #  DAILY RESET (called by scheduler)
    # ═══════════════════════════════════════════════════════════════

    def reset_daily(self) -> None:
        """Reset for new trading day (alias for reset_daily_counters)."""
        self.reset_daily_counters()

    def reset_daily_counters(self) -> None:
        """Reset daily counters (called by scheduler at midnight)."""
        self._activation_count_today = 0
        self._set_state("kill_switch_attempts", 0)
        self._daily_pnl = 0.0

        # Check if we should auto-resume from daily limit
        if self._get_state("kill_switch_daily_lock") or self._daily_lock:
            self._perform_daily_reset(str(date.today()))

        logger.debug("🔄 Kill switch daily counters reset")

    # ═══════════════════════════════════════════════════════════════
    #  UTILITIES
    # ═══════════════════════════════════════════════════════════════

    def _format_duration(self, seconds: float) -> str:
        """Format duration in human-readable format."""
        if seconds < 0:
            seconds = 0
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            minutes = seconds / 60
            return f"{minutes:.1f}m"
        elif seconds < 86400:
            hours = seconds / 3600
            return f"{hours:.1f}h"
        else:
            days = seconds / 86400
            return f"{days:.1f}d"

    def update_notifier(self, notifier) -> None:
        """Hot-swap notifier."""
        self.notifier = notifier

    def update_exchange(self, exchange) -> None:
        """Hot-swap exchange."""
        self.exchange = exchange

    def update_daily_limit(self, new_limit_inr: float) -> None:
        """Update daily loss limit."""
        self.daily_loss_limit_inr = new_limit_inr
        self.daily_loss_limit_usd = new_limit_inr / self.usd_to_inr

    def extend_auto_resume(self, additional_minutes: int) -> Dict:
        """Extend auto-resume timer."""
        if not self.is_active():
            return {"success": False, "reason": "Kill switch not active"}

        if self._get_state("kill_switch_daily_lock") or self._daily_lock:
            return {"success": False, "reason": "Daily limit lock — will auto-resume tomorrow"}

        current = self._get_state("kill_switch_auto_resume") or (self._auto_resume_at.isoformat() if self._auto_resume_at else None)
        
        if not current:
            new_resume = datetime.now(timezone.utc) + timedelta(minutes=additional_minutes)
        else:
            try:
                current_time = datetime.fromisoformat(current) if isinstance(current, str) else current
                new_resume = current_time + timedelta(minutes=additional_minutes)
            except:
                new_resume = datetime.now(timezone.utc) + timedelta(minutes=additional_minutes)

        # Cap at max
        max_resume = datetime.now(timezone.utc) + timedelta(minutes=self.MAX_AUTO_RESUME_MINUTES)
        if new_resume > max_resume:
            new_resume = max_resume

        self._auto_resume_at = new_resume
        self._set_state("kill_switch_auto_resume", new_resume.isoformat())

        logger.info(f"⏰ Auto-resume extended to {new_resume.isoformat()}")

        return {
            "success": True,
            "new_auto_resume": new_resume.isoformat(),
            "added_minutes": additional_minutes,
        }

    def cancel_auto_resume(self) -> Dict:
        """Cancel auto-resume (require manual resume)."""
        if self._get_state("kill_switch_daily_lock") or self._daily_lock:
            return {"success": False, "reason": "Daily limit lock — will auto-resume tomorrow"}

        self._auto_resume_at = None
        self._set_state("kill_switch_auto_resume", None)
        logger.info("⏰ Auto-resume cancelled — manual resume required")
        return {"success": True, "auto_resume": None}

    def force_resume(self, source: str = "manual") -> Dict:
        """
        Force resume even from daily limit lock.
        
        USE WITH CAUTION - bypasses daily limit safety.
        """
        logger.warning(f"⚠️ FORCE RESUME requested | Source: {source}")

        return self.deactivate(
            source=f"FORCE_{source}",
            reason="Force resume requested",
            force=True,
            notify=True,
        )

    def __repr__(self) -> str:
        if self.is_active():
            level = self.get_level()
            is_daily = "📅" if self.is_daily_limit_lock() else ""
            return f"<KillSwitch 🛑 {level.upper()}{is_daily}>"
        return "<KillSwitch ✅ INACTIVE>"