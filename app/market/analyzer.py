# app/market/analyzer.py

from dataclasses import dataclass
from typing import List, Dict
from statistics import stdev
from app.utils.logger import get_logger

logger = get_logger(__name__)


# =============================
# MARKET SNAPSHOT (WHAT BOT SEES)
# =============================
@dataclass
class MarketState:
    symbol: str
    price: float

    trend: str                # bullish / bearish / sideways
    rsi: float                # momentum
    volatility: str           # low / medium / high

    ema_20: float
    ema_50: float

    sentiment: str            # strong_bullish / weak_bullish / neutral / weak_bearish / strong_bearish


# =============================
# ANALYZER
# =============================
class MarketAnalyzer:
    """
    Converts raw market data into a structured MarketState
    Used by:
    - Strategy
    - Controller (market reports)
    """

    def __init__(self, symbol: str):
        self.symbol = symbol

    # -----------------------------
    # MAIN ENTRY POINT
    # -----------------------------
    def analyze(self, market_data: Dict) -> MarketState:
        candles = market_data["candles"]
        closes = [c["close"] for c in candles]

        if len(closes) < 50:
            raise ValueError("❌ Not enough candle data for analysis")

        ema_20 = self._ema(closes, 20)
        ema_50 = self._ema(closes, 50)
        rsi = self._rsi(closes)
        trend = self._trend(ema_20, ema_50)
        volatility = self._volatility(closes)
        sentiment = self._sentiment(trend, rsi)

        state = MarketState(
            symbol=self.symbol,
            price=market_data["price"],
            trend=trend,
            rsi=round(rsi, 2),
            volatility=volatility,
            ema_20=round(ema_20, 2),
            ema_50=round(ema_50, 2),
            sentiment=sentiment,
        )

        logger.info(
            f"📊 MARKET | {state.symbol} | "
            f"Price: {state.price} | "
            f"Trend: {state.trend} | "
            f"RSI: {state.rsi} | "
            f"Volatility: {state.volatility} | "
            f"Sentiment: {state.sentiment}"
        )

        return state

    # -----------------------------
    # INDICATORS
    # -----------------------------
    def _ema(self, prices: List[float], period: int) -> float:
        k = 2 / (period + 1)
        ema = prices[0]
        for price in prices[1:]:
            ema = price * k + ema * (1 - k)
        return ema

    def _rsi(self, prices: List[float], period: int = 14) -> float:
        gains, losses = [], []

        for i in range(1, period + 1):
            delta = prices[i] - prices[i - 1]
            if delta > 0:
                gains.append(delta)
            else:
                losses.append(abs(delta))

        avg_gain = sum(gains) / period if gains else 0.0
        avg_loss = sum(losses) / period if losses else 1e-9

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _trend(self, ema_20: float, ema_50: float) -> str:
        if ema_20 > ema_50:
            return "bullish"
        if ema_20 < ema_50:
            return "bearish"
        return "sideways"

    def _volatility(self, prices: List[float]) -> str:
        vol = stdev(prices[-20:])

        if vol < 0.2:
            return "low"
        if vol < 0.5:
            return "medium"
        return "high"

    def _sentiment(self, trend: str, rsi: float) -> str:
        if trend == "bullish" and rsi > 60:
            return "strong_bullish"
        if trend == "bullish":
            return "weak_bullish"
        if trend == "bearish" and rsi < 40:
            return "strong_bearish"
        if trend == "bearish":
            return "weak_bearish"
        return "neutral"