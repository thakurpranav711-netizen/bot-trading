from ..utils.logger import get_logger

logger = get_logger(__name__)


class LossGuard:
    def __init__(self, state_manager, max_daily_loss: float):
        self.state = state_manager
        self.max_daily_loss = max_daily_loss

    def can_trade(self) -> bool:
        """
        Check if daily loss limit is breached
        """
        daily_pnl = self.state.get("daily_pnl")

        if daily_pnl <= -abs(self.max_daily_loss):
            logger.critical(
                f"🛑 DAILY LOSS LIMIT HIT: {daily_pnl} <= -{self.max_daily_loss}"
            )
            return False

        return True