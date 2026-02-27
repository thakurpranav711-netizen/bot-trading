# app/market/data_feed.py

from typing import List, Dict
from app.exchange.binance_market import BinanceMarketClient
from app.utils.logger import get_logger
import time

logger = get_logger(__name__)


class MarketDataFeed:
    """
    Fetches raw market data from exchange.
    NO indicators
    NO trading logic
    """

    def __init__(self, symbol: str, timeframe: str = "5m", candle_limit: int = 50):
        self.symbol = symbol
        self.timeframe = timeframe
        self.candle_limit = candle_limit
        self.client = BinanceMarketClient()

    def get_current_price(self) -> float:
        """
        Fetch current market price
        """
        try:
            price = self.client.get_price(self.symbol)
            logger.info(f"📈 Current price fetched: {self.symbol} = {price}")
            return float(price)
        except Exception as e:
            logger.error(f"❌ Failed to fetch price for {self.symbol}: {e}")
            raise

    def get_candles(self) -> List[Dict]:
        """
        Fetch recent OHLCV candles
        """
        try:
            raw_candles = self.client.get_klines(
                symbol=self.symbol,
                interval=self.timeframe,
                limit=self.candle_limit,
            )

            candles = []
            for c in raw_candles:
                candles.append({
                    "timestamp": int(c[0]),
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                })

            logger.info(
                f"🕯️ Candles fetched: {self.symbol} | {self.timeframe} | {len(candles)} candles"
            )
            return candles

        except Exception as e:
            logger.error(f"❌ Failed to fetch candles for {self.symbol}: {e}")
            raise

    def fetch_market_data(self) -> Dict:
        """
        Unified market fetch
        """
        logger.info(f"📡 Fetching market data for {self.symbol}")

        price = self.get_current_price()
        candles = self.get_candles()

        market_data = {
            "symbol": self.symbol,
            "price": price,
            "timeframe": self.timeframe,
            "candles": candles,
            "fetched_at": int(time.time()),
        }

        logger.info(f"✅ Market data ready for {self.symbol}")
        return market_data