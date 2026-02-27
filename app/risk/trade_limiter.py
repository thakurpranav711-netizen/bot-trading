from ..utils.logger import get_logger

logger = get_logger(__name__)


class TradeLimiter:
    def __init__(self, state_manager):
        self.state = state_manager

    def can_trade(self) -> bool:
        """
        Check if bot can place a new trade today
        """
        trades_done = self.state.get("trades_done_today")
        max_trades = self.state.get("max_trades_per_day")

        if trades_done >= max_trades:
            logger.warning(
                f"🚫 Trade limit reached: {trades_done}/{max_trades}"
            )
            return False

        return True