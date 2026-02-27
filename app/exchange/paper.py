# app/exchange/paper.py

import random
from app.exchange.client import ExchangeClient
from app.utils.logger import get_logger

logger = get_logger(__name__)


class PaperExchange(ExchangeClient):
    """
    Paper trading implementation using StateManager
    """

    def __init__(self, state_manager):
        self.state = state_manager

        # Ensure defaults
        if self.state.get("balance") is None:
            self.state.set("balance", 100000.0)

        if self.state.get("positions") is None:
            self.state.set("positions", {})

    # -------------------------
    # MARKET DATA
    # -------------------------
    def get_price(self, symbol: str) -> float:
        base_price = self.state.get(f"price_{symbol}") or 30000.0
        change = random.uniform(-0.5, 0.5)
        price = round(base_price + change, 2)

        self.state.set(f"price_{symbol}", price)
        return price

    def get_recent_candles(self, symbol: str, limit: int = 10) -> list:
        candles = []
        price = self.get_price(symbol)

        for _ in range(limit):
            open_p = price
            close_p = open_p + random.uniform(-1, 1)
            high_p = max(open_p, close_p) + random.uniform(0, 0.5)
            low_p = min(open_p, close_p) - random.uniform(0, 0.5)

            candles.append({
                "open": round(open_p, 2),
                "high": round(high_p, 2),
                "low": round(low_p, 2),
                "close": round(close_p, 2),
                "volume": random.randint(1000, 10000)
            })

            price = close_p

        return candles

    # -------------------------
    # TRADING
    # -------------------------
    def buy(self, symbol: str, quantity: float) -> dict:
        price = self.get_price(symbol)
        cost = price * quantity

        balance = self.state.get("balance")
        if cost > balance:
            logger.warning("❌ PAPER BUY rejected: insufficient balance")
            return {}

        self.state.update_balance(-cost)
        self.state.add_position(symbol, quantity, price)
        self.state.increment_trade_count()

        logger.info(f"🧪 PAPER BUY | {symbol} | Qty: {quantity} | Price: {price}")

        return {
            "symbol": symbol,
            "action": "BUY",
            "price": price,
            "quantity": quantity
        }

    def sell(self, symbol: str, quantity: float) -> dict:
        position = self.state.get_position(symbol)
        if not position:
            logger.warning("❌ PAPER SELL rejected: no open position")
            return {}

        if quantity > position["quantity"]:
            logger.warning("❌ PAPER SELL rejected: qty exceeds position")
            return {}

        price = self.get_price(symbol)
        proceeds = price * quantity

        # Realized PnL
        pnl = (price - position["avg_price"]) * quantity

        self.state.update_balance(proceeds)
        self.state.reduce_position(symbol, quantity)
        self.state.increment_trade_count()

        # Update daily PnL
        self.state.set(
            "daily_pnl",
            round(self.state.get("daily_pnl", 0.0) + pnl, 2)
        )

        logger.info(
            f"🧪 PAPER SELL | {symbol} | Qty: {quantity} | Price: {price} | PnL: {round(pnl,2)}"
        )

        return {
            "symbol": symbol,
            "action": "SELL",
            "price": price,
            "quantity": quantity,
            "pnl": round(pnl, 2)
        }

    # -------------------------
    # ACCOUNT
    # -------------------------
    def get_balance(self) -> float:
        return self.state.get("balance")