import random
from app.exchange.client import ExchangeClient
from app.utils.logger import get_logger

logger = get_logger(__name__)


class BinancePaperClient(ExchangeClient):
    """
    Clean Paper Trading Exchange
    Exchange ONLY simulates fills.
    Balance & positions are handled by StateManager.
    """

    def __init__(self, state_manager):
        self.state = state_manager

    # =====================================================
    # PRICE SIMULATION (Random Walk)
    # =====================================================
    def get_price(self, symbol: str) -> float:
        base_price = self.state.get(f"price_{symbol}") or 30000.0
        change = random.uniform(-5, 5)
        new_price = round(base_price + change, 2)

        self.state.set(f"price_{symbol}", new_price)
        return new_price

    # =====================================================
    # EXECUTION SIMULATION
    # =====================================================
    def buy(self, symbol: str, quantity: float) -> dict:
        price = self.get_price(symbol)

        logger.info(f"🧪 PAPER BUY | {symbol} | Qty: {quantity} | Price: {price}")

        return {
            "symbol": symbol,
            "side": "BUY",
            "price": price,
            "quantity": quantity
        }

    def sell(self, symbol: str, quantity: float) -> dict:
        price = self.get_price(symbol)

        logger.info(f"🧪 PAPER SELL | {symbol} | Qty: {quantity} | Price: {price}")

        return {
            "symbol": symbol,
            "side": "SELL",
            "price": price,
            "quantity": quantity
        }

    # =====================================================
    # BALANCE (Read Only)
    # =====================================================
    def get_balance(self) -> float:
        return self.state.get("balance")