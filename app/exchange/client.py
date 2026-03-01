# app/exchange/client.py

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class ExchangeClient(ABC):
    """
    Abstract Exchange Interface — Production Grade

    Every exchange (Paper, Binance, Alpaca) implements this contract.
    Controller and strategy layer swap exchanges without code changes.

    Design decisions:
    - Async-ready: all methods are sync but designed to be called
      via asyncio.to_thread() in the controller for non-blocking I/O.
      This keeps subclass implementations simple (no async boilerplate
      in paper exchange) while preventing event loop blocking.
    - get_price() is IDEMPOTENT within a cycle: subclasses must cache
      the price per symbol per call-window so repeated calls in one
      cycle return the SAME value (fixes bug #12).
    - get_recent_candles() must NOT append candles on every call.
      Candles only advance on time boundaries (fixes bug #13).
    - buy()/sell() return standardized receipt dicts with ALL fields
      the controller needs — no guessing.
    - close() method for cleanup (websocket, session teardown).

    Receipt Format (buy):
        {
            "status":    "FILLED" | "PARTIAL" | "REJECTED",
            "symbol":    str,
            "action":    "BUY",
            "price":     float,   # actual fill price
            "quantity":  float,   # actual filled quantity
            "cost":      float,   # price * quantity
            "fee":       float,   # exchange fee charged
            "order_id":  str,
            "timestamp": str,     # ISO format
            "mode":      str,     # "PAPER" | "LIVE"
        }

    Receipt Format (sell):
        Same as buy plus:
            "action":    "SELL",
            "proceeds":  float,   # price * quantity
            "gross_pnl": float,   # (exit_price - entry_price) * qty
    """

    # =====================================================
    # MARKET DATA (required)
    # =====================================================

    @abstractmethod
    def get_price(self, symbol: str) -> float:
        """
        Return the current market price for a symbol.

        MUST be idempotent within a trading cycle:
        - Cache the price on first call per symbol
        - Return the SAME cached price on subsequent calls
        - Cache resets at the start of each new cycle

        This prevents the bug where random walk generates
        different prices for the same symbol within one cycle.
        """

    @abstractmethod
    def get_recent_candles(self, symbol: str, limit: int = 150) -> List[Dict]:
        """
        Return the most recent OHLCV candles for a symbol.

        Each candle dict:
            {
                "open":      float,
                "high":      float,
                "low":       float,
                "close":     float,
                "volume":    float,
                "timestamp": str,   # ISO format
            }

        Ordered: oldest → newest.
        Candles must NOT be appended on every call.
        New candles only appear on time boundaries.
        """

    # =====================================================
    # ORDER EXECUTION (required)
    # =====================================================

    @abstractmethod
    def buy(self, symbol: str, quantity: float) -> Dict:
        """
        Place a market BUY order.

        Returns a standardized receipt dict (see class docstring).
        On failure: return {"status": "REJECTED", "symbol": ..., "action": "BUY"}

        Subclasses should NOT modify balance/positions.
        The controller handles all state mutations after receiving the receipt.
        """

    @abstractmethod
    def sell(self, symbol: str, quantity: float) -> Dict:
        """
        Place a market SELL order.

        Returns a standardized receipt dict (see class docstring).
        On failure: return {"status": "REJECTED", "symbol": ..., "action": "SELL"}

        Subclasses should NOT modify balance/positions.
        The controller handles all state mutations after receiving the receipt.
        """

    # =====================================================
    # ACCOUNT INFO (required)
    # =====================================================

    @abstractmethod
    def get_balance(self) -> float:
        """Return current available balance in quote currency (e.g. USDT)."""

    # =====================================================
    # OPTIONAL — Override in subclass if supported
    # =====================================================

    def get_open_positions(self) -> Dict:
        """Return all currently open positions keyed by symbol."""
        return {}

    def get_account_summary(self) -> Dict:
        """Return summary: balance, open_positions, exposure, mode."""
        return {
            "balance": self.get_balance(),
            "open_positions": 0,
            "exposure": 0.0,
            "mode": "UNKNOWN",
        }

    # =====================================================
    # CYCLE MANAGEMENT
    # =====================================================

    def begin_cycle(self) -> None:
        """
        Called by controller at the START of each trading cycle.
        Resets per-cycle caches (price cache, etc).

        Subclasses that cache prices MUST override this to clear
        their price cache, ensuring get_price() fetches fresh data.
        """
        pass

    def end_cycle(self) -> None:
        """
        Called by controller at the END of each trading cycle.
        Optional hook for cleanup, metrics flush, etc.
        """
        pass

    # =====================================================
    # PRICE SEEDING (testing / paper trading)
    # =====================================================

    def seed_price(self, symbol: str, price: float) -> None:
        """Manually set price for a symbol. Used in paper trading and tests."""
        pass

    def reset_candles(self, symbol: str) -> None:
        """Clear candle cache for a symbol. Forces regeneration."""
        pass

    # =====================================================
    # LIFECYCLE
    # =====================================================

    def ping(self) -> bool:
        """
        Health check. Returns True if exchange is reachable.
        Override in live exchange implementations with actual
        connectivity check (e.g. GET /api/v3/ping on Binance).
        """
        return True

    def close(self) -> None:
        """
        Teardown: close websockets, HTTP sessions, file handles.
        Called during graceful shutdown.
        Override in subclasses that hold persistent connections.
        """
        pass

    # =====================================================
    # HELPERS
    # =====================================================

    @staticmethod
    def _rejection(symbol: str, action: str, reason: str = "") -> Dict:
        """
        Standard rejection receipt builder.
        Use in subclasses:
            return self._rejection("BTC/USDT", "BUY", "Insufficient balance")
        """
        receipt = {
            "status": "REJECTED",
            "symbol": symbol,
            "action": action,
        }
        if reason:
            receipt["reason"] = reason
        return receipt

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}>"