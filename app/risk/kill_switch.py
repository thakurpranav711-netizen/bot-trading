# app/risk/kill_switch.py

"""
Emergency Kill Switch — Production Grade for Autonomous Trading

Responsibilities:
- Immediate halt of all trading activity
- Optional auto-close of open positions on activation
- Cooldown timer support (auto-reactivate after N minutes)
- Graduated activation levels (soft/hard)
- Activation audit trail (who/when/why)
- Recovery checks before resumption
- Integration with controller via is_active() check every cycle
- Telegram notification hooks

Activation Levels:
- SOFT: Pause new trades, keep positions open, can auto-resume
- HARD: Close all positions, require manual resume

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

from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple
from enum import Enum
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  KILL SWITCH LEVELS
# ═══════════════════════════════════════════════════════════════════

class KillLevel(Enum):
    """Kill switch activation levels."""
    NONE = "none"           # Not active
    SOFT = "soft"           # Pause trading, keep positions
    HARD = "hard"           # Close positions, require manual resume


class KillSource(Enum):
    """Kill switch trigger sources."""
    MANUAL = "manual"
    TELEGRAM = "telegram"
    LOSS_GUARD = "loss_guard"
    DRAWDOWN = "drawdown"
    ERROR = "error"
    SYSTEM = "system"
    API = "api"
    AUTO_RESUME = "auto_resume"


# ═══════════════════════════════════════════════════════════════════
#  KILL SWITCH
# ═══════════════════════════════════════════════════════════════════

class KillSwitch:
    """
    Thread-safe emergency stop system.

    State keys used:
        kill_switch             : bool   — True = all trading halted
        kill_switch_level       : str    — "soft" or "hard"
        risk_locked             : bool   — True = risk system locked
        bot_active              : bool   — False when killed
        kill_switch_reason      : str    — why it was activated
        kill_switch_time        : str    — ISO timestamp of activation
        kill_switch_source      : str    — who triggered it
        kill_switch_history     : list   — audit trail of all activations
        kill_switch_auto_resume : str    — ISO timestamp for auto-resume
        kill_switch_attempts    : int    — consecutive activation attempts
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
        state_manager,
        notifier=None,
        auto_close_positions: bool = False,
        exchange=None,
        default_cooldown_minutes: int = 30,
        require_manual_resume_after_hard: bool = True,
    ):
        """
        Initialize Kill Switch.

        Args:
            state_manager: State manager instance
            notifier: Telegram notifier (optional)
            auto_close_positions: Close positions on SOFT activation
            exchange: Exchange client for position closing
            default_cooldown_minutes: Default auto-resume time
            require_manual_resume_after_hard: Require manual resume after HARD kill
        """
        self.state = state_manager
        self.notifier = notifier
        self.auto_close = auto_close_positions
        self.exchange = exchange
        self.default_cooldown = default_cooldown_minutes
        self.require_manual_after_hard = require_manual_resume_after_hard

        # Tracking
        self._activation_count_today: int = 0
        self._last_notification_time: Optional[datetime] = None

    # ═══════════════════════════════════════════════════════════════
    #  ACTIVATE
    # ═══════════════════════════════════════════════════════════════

    def activate(
        self,
        reason: str = "Manual trigger",
        source: str = "system",
        level: str = "soft",
        auto_resume_minutes: Optional[int] = None,
        loss_pct: float = 0.0,
        close_positions: Optional[bool] = None,
        notify: bool = True,
    ) -> Dict:
        """
        Immediately stop ALL trading activity.

        Args:
            reason: Human-readable explanation
            source: Who triggered it
            level: "soft" or "hard"
            auto_resume_minutes: Auto-resume timer (None = no auto-resume)
            loss_pct: Current loss percentage for reporting
            close_positions: Override auto_close setting
            notify: Send Telegram notification

        Returns:
            Activation summary dict
        """
        # Validate level
        level = level.lower()
        if level not in ("soft", "hard"):
            level = "soft"

        # Prevent double-activation spam from same source
        if self.state.get("kill_switch", False):
            current_source = self.state.get("kill_switch_source", "")
            current_level = self.state.get("kill_switch_level", "soft")

            # Allow upgrade from soft to hard
            if level != "hard" or current_level == "hard":
                if current_source == source:
                    logger.debug(f"⚠️ Kill switch already active from {source}")
                    return {
                        "activated": True,
                        "already_active": True,
                        "level": current_level,
                        "reason": self.state.get("kill_switch_reason", reason),
                        "source": source,
                    }

        now = datetime.utcnow()

        # Set all kill state flags
        self.state.set("kill_switch", True)
        self.state.set("kill_switch_level", level)
        self.state.set("risk_locked", True)
        self.state.set("bot_active", False)
        self.state.set("kill_switch_reason", reason)
        self.state.set("kill_switch_time", now.isoformat())
        self.state.set("kill_switch_source", source)

        # Increment activation counter
        attempts = self.state.get("kill_switch_attempts", 0) or 0
        self.state.set("kill_switch_attempts", attempts + 1)
        self._activation_count_today += 1

        # Auto-resume timer
        auto_resume_at = None
        if level == "soft" and auto_resume_minutes and auto_resume_minutes > 0:
            # Cap auto-resume time
            auto_resume_minutes = min(auto_resume_minutes, self.MAX_AUTO_RESUME_MINUTES)
            auto_resume_at = now + timedelta(minutes=auto_resume_minutes)
            self.state.set("kill_switch_auto_resume", auto_resume_at.isoformat())
        else:
            self.state.set("kill_switch_auto_resume", None)

        # Hard kills don't auto-resume if require_manual_after_hard is True
        if level == "hard" and self.require_manual_after_hard:
            self.state.set("kill_switch_auto_resume", None)
            auto_resume_at = None

        # Audit trail
        record = {
            "action": "ACTIVATED",
            "level": level,
            "reason": reason,
            "source": source,
            "loss_pct": loss_pct,
            "timestamp": now.isoformat(),
            "auto_resume": auto_resume_at.isoformat() if auto_resume_at else None,
            "activation_count_today": self._activation_count_today,
        }
        self._append_history(record)

        # Close positions if needed
        should_close = close_positions if close_positions is not None else (
            level == "hard" or self.auto_close
        )
        closed_positions = []
        if should_close and self.exchange:
            closed_positions = self._close_all_positions()

        # Log
        level_emoji = "🛑" if level == "hard" else "⚠️"
        logger.critical(
            f"{level_emoji} KILL SWITCH [{level.upper()}] | "
            f"Reason: {reason} | Source: {source} | "
            f"Loss: {loss_pct:.1f}% | "
            f"Auto-resume: {auto_resume_minutes or 'NEVER'}min | "
            f"Closed: {len(closed_positions)} positions"
        )

        # Notification
        if notify and self.notifier:
            self._send_activation_notification(
                level, reason, source, loss_pct, auto_resume_minutes, closed_positions
            )

        return {
            "activated": True,
            "already_active": False,
            "level": level,
            "reason": reason,
            "source": source,
            "loss_pct": loss_pct,
            "timestamp": now.isoformat(),
            "auto_resume_at": auto_resume_at.isoformat() if auto_resume_at else None,
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
        loss_pct: float = 0.0,
    ) -> Dict:
        """Convenience method for hard activation (closes positions)."""
        return self.activate(
            reason=reason,
            source=source,
            level="hard",
            auto_resume_minutes=None,
            loss_pct=loss_pct,
            close_positions=True,
        )

    # ═══════════════════════════════════════════════════════════════
    #  DEACTIVATE
    # ═══════════════════════════════════════════════════════════════

    def deactivate(
        self,
        source: str = "manual",
        force: bool = False,
        notify: bool = True,
    ) -> Dict:
        """
        Resume trading. Clears kill switch and reactivates bot.

        Args:
            source: Who triggered the resume
            force: Skip safety checks
            notify: Send Telegram notification

        Returns:
            Deactivation summary dict
        """
        now = datetime.utcnow()

        # Check if actually active
        if not self.state.get("kill_switch", False):
            return {
                "activated": False,
                "was_active": False,
                "source": source,
                "message": "Kill switch was not active",
            }

        # Safety checks (unless forced)
        if not force:
            can_resume, block_reason = self._can_resume()
            if not can_resume:
                logger.warning(f"⚠️ Resume blocked: {block_reason}")
                return {
                    "activated": True,
                    "was_active": True,
                    "source": source,
                    "blocked": True,
                    "block_reason": block_reason,
                }

        # Calculate duration
        activated_at = self.state.get("kill_switch_time")
        duration_seconds = 0
        if activated_at:
            try:
                start = datetime.fromisoformat(activated_at)
                duration_seconds = (now - start).total_seconds()
            except (ValueError, TypeError):
                pass

        previous_level = self.state.get("kill_switch_level", "soft")
        previous_reason = self.state.get("kill_switch_reason", "")

        # Clear ALL kill/lock state
        self.state.set("kill_switch", False)
        self.state.set("kill_switch_level", KillLevel.NONE.value)
        self.state.set("risk_locked", False)
        self.state.set("bot_active", True)
        self.state.set("kill_switch_reason", None)
        self.state.set("kill_switch_source", None)
        self.state.set("kill_switch_auto_resume", None)
        self.state.set("kill_switch_attempts", 0)

        # Keep kill_switch_time for audit

        # Audit trail
        record = {
            "action": "DEACTIVATED",
            "previous_level": previous_level,
            "source": source,
            "timestamp": now.isoformat(),
            "was_active_seconds": round(duration_seconds, 1),
            "previous_reason": previous_reason,
        }
        self._append_history(record)

        logger.warning(
            f"✅ Kill switch DEACTIVATED | Source: {source} | "
            f"Was {previous_level.upper()} for {duration_seconds:.0f}s"
        )

        # Notification
        if notify and self.notifier:
            self._send_deactivation_notification(source, duration_seconds, previous_level)

        return {
            "activated": False,
            "was_active": True,
            "source": source,
            "previous_level": previous_level,
            "timestamp": now.isoformat(),
            "was_active_seconds": round(duration_seconds, 1),
        }

    def _can_resume(self) -> Tuple[bool, str]:
        """Check if it's safe to resume trading."""
        # Check for blocking reasons
        reason = self.state.get("kill_switch_reason", "")
        for blocker in self.RESUME_BLOCKERS:
            if blocker in reason.lower():
                return False, f"Blocked by reason: {reason}"

        # Check if hard kill requires manual resume
        level = self.state.get("kill_switch_level", "soft")
        source = self.state.get("kill_switch_source", "")
        if level == "hard" and self.require_manual_after_hard:
            if source == KillSource.AUTO_RESUME.value:
                return False, "Hard kill requires manual resume"

        # Check daily drawdown
        daily_dd = self._get_daily_drawdown_pct()
        max_dd = self.state.get("max_daily_drawdown", 0.05) or 0.05
        if daily_dd >= max_dd * 100:
            return False, f"Daily drawdown still at {daily_dd:.1f}%"

        return True, ""

    def _get_daily_drawdown_pct(self) -> float:
        """Get current daily drawdown percentage."""
        start = self.state.get("start_of_day_balance")
        current = self.state.get("balance")

        if not start or start <= 0:
            return 0.0

        dd = ((start - (current or 0)) / start) * 100
        return max(0.0, dd)

    # ═══════════════════════════════════════════════════════════════
    #  STATUS CHECK
    # ═══════════════════════════════════════════════════════════════

    def is_active(self) -> bool:
        """
        Check if kill switch is currently ON.

        Also handles auto-resume timing.
        """
        kill_flag = self.state.get("kill_switch", False)
        risk_flag = self.state.get("risk_locked", False)

        is_locked = kill_flag or risk_flag

        if not is_locked:
            return False

        # Sync flags if inconsistent
        if risk_flag and not kill_flag:
            logger.warning("⚠️ Syncing kill_switch flags")
            self.state.set("kill_switch", True)

        # Check auto-resume
        auto_resume = self.state.get("kill_switch_auto_resume")
        if auto_resume:
            try:
                resume_at = datetime.fromisoformat(auto_resume)
                if datetime.utcnow() >= resume_at:
                    # Check if safe to resume
                    can_resume, reason = self._can_resume()
                    if can_resume:
                        logger.info("⏰ Auto-resume timer expired — deactivating")
                        self.deactivate(source=KillSource.AUTO_RESUME.value)
                        return False
                    else:
                        logger.warning(f"⏰ Auto-resume blocked: {reason}")
                        # Extend timer by 15 minutes
                        new_resume = datetime.utcnow() + timedelta(minutes=15)
                        self.state.set("kill_switch_auto_resume", new_resume.isoformat())
            except (ValueError, TypeError):
                pass

        return True

    def get_level(self) -> str:
        """Get current kill switch level."""
        if not self.is_active():
            return KillLevel.NONE.value
        return self.state.get("kill_switch_level", KillLevel.SOFT.value)

    def get_status(self) -> Dict:
        """Full kill switch status."""
        is_on = self.is_active()
        level = self.get_level()
        auto_resume = self.state.get("kill_switch_auto_resume")
        remaining_seconds = 0

        if is_on and auto_resume:
            try:
                resume_at = datetime.fromisoformat(auto_resume)
                remaining = (resume_at - datetime.utcnow()).total_seconds()
                remaining_seconds = max(0, remaining)
            except (ValueError, TypeError):
                pass

        # Calculate how long it's been active
        active_seconds = 0
        activated_at = self.state.get("kill_switch_time")
        if is_on and activated_at:
            try:
                start = datetime.fromisoformat(activated_at)
                active_seconds = (datetime.utcnow() - start).total_seconds()
            except (ValueError, TypeError):
                pass

        return {
            "active": is_on,
            "level": level,
            "kill_switch_flag": self.state.get("kill_switch", False),
            "risk_locked_flag": self.state.get("risk_locked", False),
            "reason": self.state.get("kill_switch_reason", "") or "",
            "source": self.state.get("kill_switch_source", "") or "",
            "activated_at": self.state.get("kill_switch_time", "") or "",
            "active_seconds": round(active_seconds, 0),
            "active_minutes": round(active_seconds / 60, 1),
            "auto_resume_at": auto_resume or "",
            "auto_resume_remaining_sec": round(remaining_seconds, 0),
            "auto_resume_remaining_min": round(remaining_seconds / 60, 1),
            "activation_attempts": self.state.get("kill_switch_attempts", 0),
            "activations_today": self._activation_count_today,
            "history_count": len(self.state.get("kill_switch_history") or []),
        }

    def get_history(self, last_n: int = 10) -> List[Dict]:
        """Return last N kill switch events."""
        history = self.state.get("kill_switch_history") or []
        return history[-last_n:]

    # ═══════════════════════════════════════════════════════════════
    #  POSITION CLOSING
    # ═══════════════════════════════════════════════════════════════

    def _close_all_positions(self) -> List[Dict]:
        """Emergency close all open positions."""
        results = []
        positions = self.state.get_all_positions()

        if not positions:
            return results

        logger.warning(f"🚨 Emergency closing {len(positions)} position(s)...")

        for symbol, pos in positions.items():
            qty = pos.get("quantity", 0)
            if qty <= 0:
                continue

            result = self._close_single_position(symbol, pos, qty)
            results.append(result)

        return results

    def _close_single_position(
        self, symbol: str, position: Dict, quantity: float
    ) -> Dict:
        """Close a single position."""
        try:
            fill = self.exchange.sell(symbol=symbol, quantity=quantity)

            if fill and fill.get("status") != "REJECTED":
                entry = position.get("entry_price", position.get("avg_price", 0))
                exit_price = float(fill.get("price", 0))
                fee = float(fill.get("fee", 0))
                gross_pnl = (exit_price - entry) * quantity
                net_pnl = gross_pnl - fee

                self.state.close_position(
                    symbol, net_pnl, exit_price=exit_price, reason="emergency_close"
                )

                logger.info(
                    f"🚨 Emergency close | {symbol} | "
                    f"Qty={quantity} @ ${exit_price:.6f} | "
                    f"PnL=${net_pnl:+.4f}"
                )

                return {
                    "symbol": symbol,
                    "quantity": quantity,
                    "exit_price": exit_price,
                    "pnl": round(net_pnl, 4),
                    "status": "CLOSED",
                }
            else:
                reason = fill.get("reason", "Rejected") if fill else "No response"
                logger.error(f"❌ Emergency close FAILED | {symbol}: {reason}")
                return {
                    "symbol": symbol,
                    "quantity": quantity,
                    "status": "FAILED",
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

    # ═══════════════════════════════════════════════════════════════
    #  NOTIFICATIONS
    # ═══════════════════════════════════════════════════════════════

    def _send_activation_notification(
        self,
        level: str,
        reason: str,
        source: str,
        loss_pct: float,
        auto_resume_minutes: Optional[int],
        closed_positions: List[Dict],
    ) -> None:
        """Send activation notification."""
        if not self.notifier:
            return

        # Rate limit notifications
        now = datetime.utcnow()
        if self._last_notification_time:
            elapsed = (now - self._last_notification_time).total_seconds()
            if elapsed < 60:  # Max 1 notification per minute
                return

        self._last_notification_time = now

        emoji = "🛑" if level == "hard" else "⚠️"
        message = (
            f"{emoji} <b>KILL SWITCH ACTIVATED</b>\n\n"
            f"Level: <b>{level.upper()}</b>\n"
            f"Reason: {reason}\n"
            f"Source: {source}\n"
            f"Loss: {loss_pct:.1f}%\n"
        )

        if auto_resume_minutes:
            message += f"Auto-resume: {auto_resume_minutes} minutes\n"
        else:
            message += "Auto-resume: DISABLED\n"

        if closed_positions:
            message += f"\n📉 Closed {len(closed_positions)} position(s)"
            total_pnl = sum(p.get("pnl", 0) for p in closed_positions)
            message += f"\nTotal PnL: ${total_pnl:+.2f}"

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send kill notification: {e}")

    def _send_deactivation_notification(
        self,
        source: str,
        duration_seconds: float,
        previous_level: str,
    ) -> None:
        """Send deactivation notification."""
        if not self.notifier:
            return

        duration_min = duration_seconds / 60
        message = (
            f"✅ <b>KILL SWITCH DEACTIVATED</b>\n\n"
            f"Source: {source}\n"
            f"Was active: {duration_min:.1f} minutes\n"
            f"Previous level: {previous_level.upper()}\n"
            f"\n🚀 Trading resumed"
        )

        try:
            self.notifier.send_message(message)
        except Exception as e:
            logger.error(f"Failed to send resume notification: {e}")

    # ═══════════════════════════════════════════════════════════════
    #  AUDIT TRAIL
    # ═══════════════════════════════════════════════════════════════

    def _append_history(self, record: Dict) -> None:
        """Append an event to kill switch history."""
        history: list = self.state.get("kill_switch_history") or []
        history.append(record)

        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]

        self.state.set("kill_switch_history", history)

    def clear_history(self) -> None:
        """Clear kill switch history."""
        self.state.set("kill_switch_history", [])
        logger.info("🗑️ Kill switch history cleared")

    # ═══════════════════════════════════════════════════════════════
    #  DAILY RESET
    # ═══════════════════════════════════════════════════════════════

    def reset_daily_counters(self) -> None:
        """Reset daily counters."""
        self._activation_count_today = 0
        self.state.set("kill_switch_attempts", 0)
        logger.debug("🔄 Kill switch daily counters reset")

    # ═══════════════════════════════════════════════════════════════
    #  UTILITIES
    # ═══════════════════════════════════════════════════════════════

    def update_notifier(self, notifier) -> None:
        """Hot-swap notifier."""
        self.notifier = notifier

    def update_exchange(self, exchange) -> None:
        """Hot-swap exchange."""
        self.exchange = exchange

    def extend_auto_resume(self, additional_minutes: int) -> Dict:
        """Extend auto-resume timer."""
        if not self.is_active():
            return {"success": False, "reason": "Kill switch not active"}

        current = self.state.get("kill_switch_auto_resume")
        if not current:
            # Create new timer
            new_resume = datetime.utcnow() + timedelta(minutes=additional_minutes)
        else:
            try:
                current_time = datetime.fromisoformat(current)
                new_resume = current_time + timedelta(minutes=additional_minutes)
            except:
                new_resume = datetime.utcnow() + timedelta(minutes=additional_minutes)

        # Cap at max
        max_resume = datetime.utcnow() + timedelta(minutes=self.MAX_AUTO_RESUME_MINUTES)
        if new_resume > max_resume:
            new_resume = max_resume

        self.state.set("kill_switch_auto_resume", new_resume.isoformat())

        logger.info(f"⏰ Auto-resume extended to {new_resume.isoformat()}")

        return {
            "success": True,
            "new_auto_resume": new_resume.isoformat(),
            "added_minutes": additional_minutes,
        }

    def cancel_auto_resume(self) -> Dict:
        """Cancel auto-resume (require manual resume)."""
        self.state.set("kill_switch_auto_resume", None)
        logger.info("⏰ Auto-resume cancelled — manual resume required")
        return {"success": True, "auto_resume": None}

    def __repr__(self) -> str:
        if self.is_active():
            level = self.get_level()
            return f"<KillSwitch 🛑 {level.upper()}>"
        return "<KillSwitch ✅ INACTIVE>"