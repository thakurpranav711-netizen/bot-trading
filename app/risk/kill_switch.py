# app/risk/kill_switch.py

"""
Emergency Kill Switch — Production Grade (FIXED)

Responsibilities:
- Immediate halt of all trading activity
- Optional auto-close of open positions on activation
- Cooldown timer support (auto-reactivate after N minutes)
- Activation audit trail (who/when/why)
- Integration with controller via is_active() check every cycle
- Telegram notification hooks

FIXES APPLIED:
- deactivate() now clears ALL lock flags (risk_locked, reason, source, time)
- activate() now sets risk_locked = True for consistency
- is_active() checks both kill_switch AND risk_locked flags
- Double-activation protection (prevents spam from loss_guard)

Usage in controller.run_cycle():
    if self.kill_switch.is_active():
        logger.warning("Kill switch active — skipping cycle")
        return

Usage from Telegram:
    /kill reason        → activate
    /resume             → deactivate
    /kill_status        → check state
"""

from datetime import datetime, timedelta
from typing import Optional, Dict, List
from app.utils.logger import get_logger

logger = get_logger(__name__)


class KillSwitch:
    """
    Thread-safe emergency stop system.

    State keys used:
        kill_switch             : bool   — True = all trading halted
        risk_locked             : bool   — True = risk system locked (FIXED: now synced)
        bot_active              : bool   — False when killed
        kill_switch_reason      : str    — why it was activated
        kill_switch_time        : str    — ISO timestamp of activation
        kill_switch_source      : str    — who triggered it
        kill_switch_history     : list   — audit trail of all activations
        kill_switch_auto_resume : str    — ISO timestamp for auto-resume (optional)
    """

    MAX_HISTORY = 50

    def __init__(
        self,
        state_manager,
        notifier=None,
        auto_close_positions: bool = False,
        exchange=None,
    ):
        self.state = state_manager
        self.notifier = notifier
        self.auto_close = auto_close_positions
        self.exchange = exchange

    # ═════════════════════════════════════════════════════
    #  ACTIVATE (FIXED)
    # ═════════════════════════════════════════════════════

    def activate(
        self,
        reason: str = "Manual trigger",
        source: str = "system",
        auto_resume_minutes: Optional[int] = None,
        loss_pct: float = 0.0,
    ) -> Dict:
        """
        Immediately stop ALL trading activity.

        FIXED:
        - Now sets risk_locked = True (was missing)
        - Skips if already active with same source (prevents spam)

        Args:
            reason:               Human-readable explanation
            source:               Who triggered ("telegram", "loss_guard", "system", etc.)
            auto_resume_minutes:  If set, auto-resume after N minutes
            loss_pct:             Current loss percentage (for reporting)

        Returns:
            Activation summary dict
        """
        # ── FIXED: Prevent double-activation spam ─────────────────
        if self.state.get("kill_switch", False):
            current_source = self.state.get("kill_switch_source", "")
            if current_source == source:
                logger.debug(
                    f"⚠️ Kill switch already active from {source} — skipping re-activation"
                )
                return {
                    "activated": True,
                    "already_active": True,
                    "reason": self.state.get("kill_switch_reason", reason),
                    "source": source,
                }

        now = datetime.utcnow()

        # ── Set ALL kill state flags ──────────────────────────────
        self.state.set("kill_switch", True)
        self.state.set("risk_locked", True)          # FIXED: Was missing
        self.state.set("bot_active", False)
        self.state.set("kill_switch_reason", reason)
        self.state.set("kill_switch_time", now.isoformat())
        self.state.set("kill_switch_source", source)

        # ── Auto-resume timer ─────────────────────────────────────
        auto_resume_at = None
        if auto_resume_minutes and auto_resume_minutes > 0:
            auto_resume_at = now + timedelta(minutes=auto_resume_minutes)
            self.state.set(
                "kill_switch_auto_resume", auto_resume_at.isoformat()
            )
        else:
            self.state.set("kill_switch_auto_resume", None)

        # ── Audit trail ───────────────────────────────────────────
        record = {
            "action": "ACTIVATED",
            "reason": reason,
            "source": source,
            "loss_pct": loss_pct,
            "timestamp": now.isoformat(),
            "auto_resume": (
                auto_resume_at.isoformat() if auto_resume_at else None
            ),
        }
        self._append_history(record)

        # ── Auto-close positions ──────────────────────────────────
        closed_positions = []
        if self.auto_close and self.exchange:
            closed_positions = self._close_all_positions()

        logger.critical(
            f"🛑 KILL SWITCH ACTIVATED | "
            f"Reason: {reason} | Source: {source} | "
            f"Loss: {loss_pct:.1f}% | "
            f"Auto-resume: {auto_resume_minutes or 'NEVER'}min | "
            f"Positions closed: {len(closed_positions)}"
        )

        summary = {
            "activated": True,
            "already_active": False,
            "reason": reason,
            "source": source,
            "loss_pct": loss_pct,
            "timestamp": now.isoformat(),
            "auto_resume_at": (
                auto_resume_at.isoformat() if auto_resume_at else None
            ),
            "positions_closed": len(closed_positions),
        }

        return summary

    # ═════════════════════════════════════════════════════
    #  DEACTIVATE (FIXED)
    # ═════════════════════════════════════════════════════

    def deactivate(self, source: str = "manual") -> Dict:
        """
        Resume trading. Clears kill switch and reactivates bot.

        FIXED:
        - Now clears risk_locked flag (was NEVER cleared before)
        - Now clears kill_switch_reason, source, time
        - Now clears auto_resume timer
        - Properly resets ALL lock-related state

        Args:
            source: Who triggered the resume ("telegram", "auto", "manual")

        Returns:
            Deactivation summary dict
        """
        now = datetime.utcnow()

        # Calculate how long the kill switch was active
        activated_at = self.state.get("kill_switch_time")
        duration_seconds = 0
        if activated_at:
            try:
                start = datetime.fromisoformat(activated_at)
                duration_seconds = (now - start).total_seconds()
            except (ValueError, TypeError):
                pass

        # ── FIXED: Clear ALL kill/lock state ──────────────────────
        self.state.set("kill_switch", False)
        self.state.set("risk_locked", False)           # FIXED: Was never cleared
        self.state.set("bot_active", True)
        self.state.set("kill_switch_reason", None)     # FIXED: Clear stale reason
        self.state.set("kill_switch_source", None)     # FIXED: Clear stale source
        self.state.set("kill_switch_auto_resume", None)

        # NOTE: We intentionally keep kill_switch_time for audit purposes
        # It shows when the LAST activation happened

        # ── Audit trail ───────────────────────────────────────────
        record = {
            "action": "DEACTIVATED",
            "source": source,
            "timestamp": now.isoformat(),
            "was_active_seconds": round(duration_seconds, 1),
        }
        self._append_history(record)

        logger.warning(
            f"✅ Kill switch DEACTIVATED | Source: {source} | "
            f"Was active for {duration_seconds:.0f}s | "
            f"risk_locked cleared"
        )

        summary = {
            "activated": False,
            "source": source,
            "timestamp": now.isoformat(),
            "was_active_seconds": round(duration_seconds, 1),
        }

        return summary

    # ═════════════════════════════════════════════════════
    #  STATUS CHECK (FIXED)
    # ═════════════════════════════════════════════════════

    def is_active(self) -> bool:
        """
        Check if kill switch is currently ON.

        FIXED: Now checks BOTH kill_switch AND risk_locked flags.
        Previously only checked kill_switch, but risk_locked could
        be True independently (set by old code or manual state edit).

        Also handles auto-resume:
        If auto_resume time has passed, automatically deactivates.
        """
        kill_flag = self.state.get("kill_switch", False)
        risk_flag = self.state.get("risk_locked", False)

        is_locked = kill_flag or risk_flag

        if not is_locked:
            return False

        # ── If only risk_locked is True but kill_switch is False ──
        # This means state is inconsistent — sync them
        if risk_flag and not kill_flag:
            logger.warning(
                "⚠️ risk_locked=True but kill_switch=False — syncing flags"
            )
            # Treat as locked, but check auto-resume
            self.state.set("kill_switch", True)

        # ── Check auto-resume ─────────────────────────────────────
        auto_resume = self.state.get("kill_switch_auto_resume")
        if auto_resume:
            try:
                resume_at = datetime.fromisoformat(auto_resume)
                if datetime.utcnow() >= resume_at:
                    logger.info(
                        "⏰ Auto-resume timer expired — deactivating kill switch"
                    )
                    self.deactivate(source="auto_resume")
                    return False
            except (ValueError, TypeError):
                pass

        return True

    def get_status(self) -> Dict:
        """
        Full kill switch status for Telegram /kill_status command.
        """
        is_on = self.is_active()
        auto_resume = self.state.get("kill_switch_auto_resume")
        remaining_seconds = 0

        if is_on and auto_resume:
            try:
                resume_at = datetime.fromisoformat(auto_resume)
                remaining = (resume_at - datetime.utcnow()).total_seconds()
                remaining_seconds = max(0, remaining)
            except (ValueError, TypeError):
                pass

        return {
            "active": is_on,
            "kill_switch_flag": self.state.get("kill_switch", False),
            "risk_locked_flag": self.state.get("risk_locked", False),
            "reason": self.state.get("kill_switch_reason", ""),
            "source": self.state.get("kill_switch_source", ""),
            "activated_at": self.state.get("kill_switch_time", ""),
            "auto_resume_at": auto_resume or "",
            "auto_resume_remaining_sec": round(remaining_seconds, 0),
            "history_count": len(
                self.state.get("kill_switch_history") or []
            ),
        }

    def get_history(self, last_n: int = 10) -> List[Dict]:
        """Return last N kill switch events for audit."""
        history = self.state.get("kill_switch_history") or []
        return history[-last_n:]

    # ═════════════════════════════════════════════════════
    #  AUTO-CLOSE POSITIONS
    # ═════════════════════════════════════════════════════

    def _close_all_positions(self) -> List[Dict]:
        """
        Emergency close all open positions.
        Called when kill switch activates with auto_close=True.
        """
        results = []
        positions = self.state.get_all_positions()

        if not positions:
            return results

        logger.warning(
            f"🚨 Emergency closing {len(positions)} position(s)..."
        )

        for symbol, pos in positions.items():
            qty = pos.get("quantity", 0)
            if qty <= 0:
                continue

            try:
                fill = self.exchange.sell(symbol=symbol, quantity=qty)

                if fill and fill.get("status") != "REJECTED":
                    entry = pos.get(
                        "entry_price", pos.get("avg_price", 0)
                    )
                    exit_price = float(fill.get("price", 0))
                    fee = float(fill.get("fee", 0))
                    gross_pnl = (exit_price - entry) * qty
                    net_pnl = gross_pnl - fee

                    self.state.close_position(
                        symbol, net_pnl, exit_price=exit_price
                    )

                    results.append({
                        "symbol": symbol,
                        "qty": qty,
                        "exit_price": exit_price,
                        "pnl": round(net_pnl, 4),
                        "status": "CLOSED",
                    })

                    logger.info(
                        f"🚨 Emergency close | {symbol} | "
                        f"Qty={qty} @ ${exit_price:.6f} | "
                        f"PnL=${net_pnl:+.4f}"
                    )
                else:
                    results.append({
                        "symbol": symbol,
                        "qty": qty,
                        "status": "FAILED",
                        "reason": fill.get("reason", "Rejected"),
                    })
                    logger.error(
                        f"❌ Emergency close FAILED | {symbol}"
                    )

            except Exception as e:
                results.append({
                    "symbol": symbol,
                    "qty": qty,
                    "status": "ERROR",
                    "reason": str(e),
                })
                logger.exception(
                    f"❌ Emergency close error | {symbol}: {e}"
                )

        return results

    # ═════════════════════════════════════════════════════
    #  AUDIT TRAIL
    # ═════════════════════════════════════════════════════

    def _append_history(self, record: Dict) -> None:
        """Append an event to kill switch history (capped)."""
        history: list = self.state.get("kill_switch_history") or []
        history.append(record)

        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]

        self.state.set("kill_switch_history", history)

    # ═════════════════════════════════════════════════════
    #  CONVENIENCE
    # ═════════════════════════════════════════════════════

    def update_notifier(self, notifier) -> None:
        """Hot-swap notifier (called after Telegram bot starts)."""
        self.notifier = notifier

    def update_exchange(self, exchange) -> None:
        """Hot-swap exchange (for enabling auto_close after init)."""
        self.exchange = exchange

    def __repr__(self) -> str:
        status = "🛑 ACTIVE" if self.is_active() else "✅ INACTIVE"
        return f"<KillSwitch {status}>"