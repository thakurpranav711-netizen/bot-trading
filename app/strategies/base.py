# app/strategies/base.py

from abc import ABC, abstractmethod
from typing import Dict, Optional
from app.market.analyzer import MarketState


class BaseStrategy(ABC):
    """
    Base Strategy Interface
    Strategy ONLY decides signals, never executes trades
    """

    def __init__(self, symbol: str, quantity: float):
        self.symbol = symbol
        self.quantity = quantity

    # -----------------------------
    # Required Strategy Methods
    # -----------------------------
    @abstractmethod
    def should_enter(self, market: MarketState) -> Optional[Dict]:
        """
        Decide whether to ENTER a trade

        Return:
        {
            "action": "BUY",
            "reason": str,
            "confidence": float
        }
        OR None
        """
        pass

    @abstractmethod
    def should_exit(self, market: MarketState, position: Dict) -> Optional[Dict]:
        """
        Decide whether to EXIT a trade

        position example:
        {
            "quantity": float,
            "avg_price": float
        }

        Return:
        {
            "action": "SELL",
            "reason": str,
            "confidence": float
        }
        OR None
        """
        pass

    # -----------------------------
    # Helpers
    # -----------------------------
    def get_symbol(self) -> str:
        return self.symbol

    def get_quantity(self) -> float:
        return self.quantity

    def build_signal(self, action: str, reason: str, confidence: float) -> Dict:
        return {
            "symbol": self.symbol,
            "action": action,
            "quantity": self.quantity,
            "reason": reason,
            "confidence": round(confidence, 2),
        }