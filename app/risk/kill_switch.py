from ..utils.logger import get_logger

logger = get_logger(__name__)


class KillSwitch:
    def __init__(self, state_manager):
        self.state = state_manager

    def activate(self, reason: str = "Manual trigger"):
        """
        Immediately stop all trading activity
        """
        self.state.set("bot_active", False)
        self.state.set("kill_switch", True)

        logger.critical(f"🛑 KILL SWITCH ACTIVATED | Reason: {reason}")

    def deactivate(self):
        """
        Resume trading (only if explicitly allowed)
        """
        self.state.set("kill_switch", False)
        self.state.set("bot_active", True)

        logger.warning("⚠️ Kill switch deactivated. Trading resumed.")

    def is_active(self) -> bool:
        """
        Check if kill switch is ON
        """
        return self.state.get("kill_switch") is True