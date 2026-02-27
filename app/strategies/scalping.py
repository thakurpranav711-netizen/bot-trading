# app/strategies/scalping.py

from typing import Dict, Optional
from app.strategies.base import BaseStrategy
from app.market.analyzer import MarketState
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ScalpingStrategy(BaseStrategy):
    """
    Professional Scalping Strategy
    - EMA momentum confirmation
    - RSI mid-zone momentum bias
    - Fixed TP/SL
    - Trend reversal protection
    """

    def __init__(
        self,
        symbol: str,
        quantity: float,
        take_profit_pct: float = 0.003,   # 0.3%
        stop_loss_pct: float = 0.002,     # 0.2%
    ):
        super().__init__(symbol, quantity)
        self.take_profit_pct = take_profit_pct
        self.stop_loss_pct = stop_loss_pct

    # =====================================================
    # ENTRY LOGIC
    # =====================================================
    def should_enter(self, market: MarketState) -> Optional[Dict]:

        # Must be bullish trend
        if market.trend != "bullish":
            return None

        # EMA crossover confirmation
        if market.ema_20 <= market.ema_50:
            return None

        # RSI must show bullish momentum
        if not (50 <= market.rsi <= 65):
            return None

        logger.info(f"🟢 ENTRY SIGNAL | {self.symbol}")

        return self.build_signal(
            action="BUY",
            reason="Bullish trend + EMA momentum + RSI strength",
            confidence=0.80,
            metadata={
                "trend": market.trend,
                "rsi": market.rsi,
                "ema_20": market.ema_20,
                "ema_50": market.ema_50,
            },
        )

    # =====================================================
    # EXIT LOGIC
    # =====================================================
    def should_exit(self, market: MarketState, position: Dict) -> Optional[Dict]:

        entry_price = position["avg_price"]
        current_price = market.price

        pnl_pct = (current_price - entry_price) / entry_price

        # ---- TAKE PROFIT ----
        if pnl_pct >= self.take_profit_pct:
            logger.info(f"🟡 TAKE PROFIT | {self.symbol}")
            return self.build_signal(
                action="SELL",
                reason="Take profit target reached",
                confidence=0.95,
                metadata={"pnl_pct": round(pnl_pct * 100, 3)},
            )

        # ---- STOP LOSS ----
        if pnl_pct <= -self.stop_loss_pct:
            logger.warning(f"🔴 STOP LOSS | {self.symbol}")
            return self.build_signal(
                action="SELL",
                reason="Stop loss triggered",
                confidence=1.0,
                metadata={"pnl_pct": round(pnl_pct * 100, 3)},
            )

        # ---- TREND REVERSAL ----
        if market.trend == "bearish" and pnl_pct > 0:
            logger.info(f"🔵 EXIT | Trend reversal protection | {self.symbol}")
            return self.build_signal(
                action="SELL",
                reason="Trend turned bearish (profit protection)",
                confidence=0.85,
                metadata={"pnl_pct": round(pnl_pct * 100, 3)},
            )

        return None