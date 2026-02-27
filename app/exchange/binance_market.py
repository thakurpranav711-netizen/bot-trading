from app.utils.logger import get_logger
import random

logger = get_logger(__name__)


class BinanceMarketClient:
    """
    Market data only (price + candles)
    No trading, no balance
    """

    def get_price(self, symbol: str) -> float:
        # TEMP mock price (safe for Step-1)
        price = round(42000 + random.uniform(-50, 50), 2)
        logger.info(f"📈 Market price fetched: {symbol} = {price}")
        return price

    def get_klines(self, symbol: str, interval: str, limit: int):
        candles = []
        base = self.get_price(symbol)

        for i in range(limit):
            open_p = base + random.uniform(-10, 10)
            close = open_p + random.uniform(-10, 10)
            high = max(open_p, close) + random.uniform(0, 5)
            low = min(open_p, close) - random.uniform(0, 5)

            candles.append([
                i,
                round(open_p, 2),
                round(high, 2),
                round(low, 2),
                round(close, 2),
                random.uniform(50, 200),
            ])

        return candles