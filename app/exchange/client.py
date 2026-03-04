# app/exchange/client.py

from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Tuple
from enum import Enum
from datetime import datetime


class OrderStatus(Enum):
    """Standardized order status codes."""
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    REJECTED = "REJECTED"
    PENDING = "PENDING"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


class OrderSide(Enum):
    """Order side."""
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    """Order types supported."""
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LIMIT = "STOP_LIMIT"


class ExchangeClient(ABC):
    """
    Abstract Exchange Interface — Production Grade

    Every exchange (Paper, Binance, Alpaca) implements this contract.
    Controller and strategy layer swap exchanges without code changes.

    Design Principles:
    ─────────────────
    1. SYNC-FIRST: Methods are synchronous but designed to be called
       via asyncio.to_thread() for non-blocking I/O.

    2. IDEMPOTENT PRICING: get_price() caches per symbol per cycle.
       Repeated calls return the SAME value within one cycle.

    3. STATELESS ORDERS: buy()/sell() return receipts only.
       Controller handles all state mutations.

    4. STANDARDIZED RECEIPTS: All order methods return dicts with
       consistent fields for controller processing.

    Receipt Format (BUY):
    ─────────────────────
        {
            "status":       "FILLED" | "PARTIAL" | "REJECTED",
            "symbol":       str,
            "action":       "BUY",
            "side":         "BUY",
            "order_type":   "MARKET" | "LIMIT",
            "price":        float,      # actual fill price
            "quantity":     float,      # requested quantity
            "filled_qty":   float,      # actual filled quantity
            "cost":         float,      # price * filled_qty
            "fee":          float,      # exchange fee charged
            "fee_currency": str,        # fee currency (e.g., "USDT")
            "order_id":     str,
            "timestamp":    str,        # ISO format
            "mode":         str,        # "PAPER" | "LIVE"
            "exchange":     str,        # "ALPACA" | "BINANCE" | "PAPER"
        }

    Receipt Format (SELL):
    ──────────────────────
        Same as BUY plus:
            "proceeds":     float,      # price * filled_qty
            "gross_pnl":    float,      # (exit - entry) * qty (if entry known)
            "net_pnl":      float,      # gross_pnl - fees

    Position Format:
    ────────────────
        {
            "symbol":       str,
            "side":         "long" | "short",
            "quantity":     float,
            "entry_price":  float,
            "current_price": float,
            "unrealized_pnl": float,
            "market_value": float,
            "cost_basis":   float,
        }
    """

    # Exchange identifier - override in subclasses
    EXCHANGE_NAME: str = "UNKNOWN"
    MODE: str = "UNKNOWN"  # "PAPER" or "LIVE"

    # ═══════════════════════════════════════════════════════════════
    # MARKET DATA (required)
    # ═══════════════════════════════════════════════════════════════

    @abstractmethod
    def get_price(self, symbol: str) -> float:
        """
        Return the current market price for a symbol.

        MUST be idempotent within a trading cycle:
        - Cache the price on first call per symbol
        - Return the SAME cached price on subsequent calls
        - Cache resets at the start of each new cycle (via begin_cycle)

        Returns:
            float: Current price, or 0.0 if unavailable

        Raises:
            Does NOT raise - returns 0.0 on failure
        """

    @abstractmethod
    def get_recent_candles(
        self,
        symbol: str,
        limit: int = 150,
        timeframe: str = "5m"
    ) -> List[Dict]:
        """
        Return the most recent OHLCV candles for a symbol.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            limit: Number of candles to fetch (default 150)
            timeframe: Candle interval ("1m", "5m", "15m", "1h", "4h", "1d")

        Returns:
            List of candle dicts, oldest → newest:
            [
                {
                    "open":      float,
                    "high":      float,
                    "low":       float,
                    "close":     float,
                    "volume":    float,
                    "timestamp": str,       # ISO format
                    "timeframe": str,       # e.g., "5m"
                },
                ...
            ]

        Note:
            - Candles must NOT be appended on every call
            - New candles only appear on time boundaries
            - Returns empty list on failure
        """

    def get_ticker(self, symbol: str) -> Dict:
        """
        Get extended ticker information.

        Returns:
            {
                "symbol":       str,
                "price":        float,
                "bid":          float,
                "ask":          float,
                "spread":       float,      # ask - bid
                "spread_pct":   float,      # spread / price * 100
                "volume_24h":   float,
                "change_24h":   float,      # price change in 24h
                "change_pct":   float,      # percentage change
                "high_24h":     float,
                "low_24h":      float,
                "timestamp":    str,
            }
        """
        price = self.get_price(symbol)
        return {
            "symbol": symbol,
            "price": price,
            "bid": price,
            "ask": price,
            "spread": 0.0,
            "spread_pct": 0.0,
            "volume_24h": 0.0,
            "change_24h": 0.0,
            "change_pct": 0.0,
            "high_24h": price,
            "low_24h": price,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def get_orderbook(self, symbol: str, depth: int = 10) -> Dict:
        """
        Get order book data.

        Returns:
            {
                "symbol": str,
                "bids": [(price, quantity), ...],  # highest first
                "asks": [(price, quantity), ...],  # lowest first
                "timestamp": str,
            }
        """
        return {
            "symbol": symbol,
            "bids": [],
            "asks": [],
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ═══════════════════════════════════════════════════════════════
    # ORDER EXECUTION (required)
    # ═══════════════════════════════════════════════════════════════

    @abstractmethod
    def buy(
        self,
        symbol: str,
        quantity: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Dict:
        """
        Place a BUY order.

        Args:
            symbol: Trading pair
            quantity: Amount to buy
            order_type: "MARKET" or "LIMIT"
            price: Limit price (required for LIMIT orders)

        Returns:
            Standardized receipt dict (see class docstring)

        Note:
            - Does NOT modify balance/positions
            - Controller handles all state mutations
        """

    @abstractmethod
    def sell(
        self,
        symbol: str,
        quantity: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Dict:
        """
        Place a SELL order.

        Args:
            symbol: Trading pair
            quantity: Amount to sell
            order_type: "MARKET" or "LIMIT"
            price: Limit price (required for LIMIT orders)

        Returns:
            Standardized receipt dict (see class docstring)

        Note:
            - Does NOT modify balance/positions
            - Controller handles all state mutations
        """

    def place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> Dict:
        """
        Unified order placement with optional SL/TP.

        This is a convenience method that calls buy() or sell()
        and optionally sets up bracket orders if supported.

        Args:
            symbol: Trading pair
            side: "BUY" or "SELL"
            quantity: Amount
            order_type: "MARKET" or "LIMIT"
            price: Limit price (for LIMIT orders)
            stop_loss: Stop loss price (optional)
            take_profit: Take profit price (optional)

        Returns:
            Order receipt dict
        """
        if side.upper() == "BUY":
            return self.buy(symbol, quantity, order_type, price)
        elif side.upper() == "SELL":
            return self.sell(symbol, quantity, order_type, price)
        else:
            return self._rejection(symbol, side, f"Invalid side: {side}")

    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """
        Cancel an open order.

        Returns:
            {
                "status": "CANCELLED" | "REJECTED",
                "order_id": str,
                "symbol": str,
                "reason": str,  # if rejected
            }
        """
        return {
            "status": "REJECTED",
            "order_id": order_id,
            "symbol": symbol or "",
            "reason": "Cancel not supported",
        }

    def get_order_status(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """
        Get status of an order.

        Returns:
            {
                "order_id": str,
                "symbol": str,
                "status": str,
                "filled_qty": float,
                "remaining_qty": float,
                "avg_price": float,
            }
        """
        return {
            "order_id": order_id,
            "symbol": symbol or "",
            "status": "UNKNOWN",
            "filled_qty": 0.0,
            "remaining_qty": 0.0,
            "avg_price": 0.0,
        }

    # ═══════════════════════════════════════════════════════════════
    # ACCOUNT INFO (required)
    # ═══════════════════════════════════════════════════════════════

    @abstractmethod
    def get_balance(self) -> float:
        """
        Return current available balance in quote currency (e.g., USDT).

        This is the AVAILABLE balance, not including funds locked
        in open orders or positions.
        """

    def get_total_balance(self) -> float:
        """
        Return total account value including positions.
        Override in subclasses for accurate calculation.
        """
        return self.get_balance()

    def get_buying_power(self) -> float:
        """
        Return available buying power.
        May differ from balance due to margin/leverage.
        """
        return self.get_balance()

    # ═══════════════════════════════════════════════════════════════
    # POSITION MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def get_position(self, symbol: str) -> Optional[Dict]:
        """
        Get position for a specific symbol.

        Returns:
            Position dict or None if no position
        """
        positions = self.get_open_positions()
        return positions.get(symbol)

    def get_open_positions(self) -> Dict[str, Dict]:
        """
        Return all currently open positions keyed by symbol.

        Returns:
            {
                "BTC/USDT": {
                    "symbol": "BTC/USDT",
                    "side": "long",
                    "quantity": 0.001,
                    "entry_price": 50000.0,
                    "current_price": 51000.0,
                    "unrealized_pnl": 1.0,
                    "market_value": 51.0,
                    "cost_basis": 50.0,
                },
                ...
            }
        """
        return {}

    def get_position_value(self, symbol: str) -> float:
        """Get current market value of a position."""
        pos = self.get_position(symbol)
        if not pos:
            return 0.0
        return pos.get("market_value", 0.0)

    def get_total_exposure(self) -> float:
        """Get total market value of all open positions."""
        positions = self.get_open_positions()
        return sum(p.get("market_value", 0.0) for p in positions.values())

    def get_unrealized_pnl(self) -> float:
        """Get total unrealized PnL across all positions."""
        positions = self.get_open_positions()
        return sum(p.get("unrealized_pnl", 0.0) for p in positions.values())

    def close_position(
        self,
        symbol: str,
        quantity: Optional[float] = None
    ) -> Dict:
        """
        Close a position (full or partial).

        Args:
            symbol: Position symbol
            quantity: Amount to close (None = close all)

        Returns:
            Sell order receipt
        """
        pos = self.get_position(symbol)
        if not pos:
            return self._rejection(symbol, "SELL", "No position to close")

        close_qty = quantity or pos.get("quantity", 0)
        return self.sell(symbol, close_qty)

    def close_all_positions(self) -> List[Dict]:
        """
        Close all open positions.

        Returns:
            List of sell order receipts
        """
        results = []
        for symbol, pos in self.get_open_positions().items():
            qty = pos.get("quantity", 0)
            if qty > 0:
                result = self.sell(symbol, qty)
                results.append(result)
        return results

    # ═══════════════════════════════════════════════════════════════
    # ACCOUNT SUMMARY
    # ═══════════════════════════════════════════════════════════════

    def get_account_summary(self) -> Dict:
        """
        Return comprehensive account summary.

        Returns:
            {
                "balance": float,
                "total_equity": float,
                "buying_power": float,
                "open_positions": int,
                "total_exposure": float,
                "exposure_pct": float,
                "unrealized_pnl": float,
                "mode": str,
                "exchange": str,
                "timestamp": str,
            }
        """
        balance = self.get_balance()
        positions = self.get_open_positions()
        exposure = sum(p.get("market_value", 0.0) for p in positions.values())
        unrealized = sum(p.get("unrealized_pnl", 0.0) for p in positions.values())
        total_equity = balance + exposure

        return {
            "balance": balance,
            "total_equity": total_equity,
            "buying_power": self.get_buying_power(),
            "open_positions": len(positions),
            "total_exposure": exposure,
            "exposure_pct": (exposure / total_equity * 100) if total_equity > 0 else 0,
            "unrealized_pnl": unrealized,
            "mode": self.MODE,
            "exchange": self.EXCHANGE_NAME,
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ═══════════════════════════════════════════════════════════════
    # SYMBOL INFORMATION
    # ═══════════════════════════════════════════════════════════════

    def get_symbol_info(self, symbol: str) -> Dict:
        """
        Get trading rules and limits for a symbol.

        Returns:
            {
                "symbol": str,
                "base_asset": str,          # e.g., "BTC"
                "quote_asset": str,         # e.g., "USDT"
                "min_quantity": float,      # minimum order size
                "max_quantity": float,      # maximum order size
                "quantity_step": float,     # quantity increment
                "min_notional": float,      # minimum order value
                "price_precision": int,     # decimal places for price
                "quantity_precision": int,  # decimal places for quantity
                "is_tradable": bool,
            }
        """
        parts = symbol.replace("/", "").split("USDT")
        base = parts[0] if parts else symbol[:3]

        return {
            "symbol": symbol,
            "base_asset": base,
            "quote_asset": "USDT",
            "min_quantity": 0.00001,
            "max_quantity": 1000000.0,
            "quantity_step": 0.00001,
            "min_notional": 1.0,
            "price_precision": 8,
            "quantity_precision": 8,
            "is_tradable": True,
        }

    def get_tradable_symbols(self) -> List[str]:
        """Get list of tradable symbols."""
        return []

    def normalize_symbol(self, symbol: str) -> str:
        """
        Normalize symbol format for this exchange.
        Override in subclasses with exchange-specific formats.

        Examples:
            "BTC/USDT" -> "BTCUSDT" (Binance)
            "BTC/USDT" -> "BTC/USD" (Alpaca)
        """
        return symbol

    def denormalize_symbol(self, symbol: str) -> str:
        """
        Convert exchange symbol format to standard format.
        Reverse of normalize_symbol.
        """
        return symbol

    # ═══════════════════════════════════════════════════════════════
    # CYCLE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def begin_cycle(self) -> None:
        """
        Called at the START of each trading cycle.

        Subclasses MUST override to:
        - Clear price cache
        - Reset any per-cycle state
        - Fetch fresh market data if needed
        """
        pass

    def end_cycle(self) -> None:
        """
        Called at the END of each trading cycle.

        Optional hook for:
        - Metrics flush
        - Cleanup
        - Logging
        """
        pass

    # ═══════════════════════════════════════════════════════════════
    # PRICE SEEDING (testing / paper trading)
    # ═══════════════════════════════════════════════════════════════

    def seed_price(self, symbol: str, price: float) -> None:
        """
        Manually set price for a symbol.
        Used in paper trading and tests.
        """
        pass

    def seed_candles(self, symbol: str, candles: List[Dict]) -> None:
        """
        Manually set candle history for a symbol.
        Used in backtesting and tests.
        """
        pass

    def reset_candles(self, symbol: str) -> None:
        """
        Clear candle cache for a symbol.
        Forces regeneration on next request.
        """
        pass

    # ═══════════════════════════════════════════════════════════════
    # LIFECYCLE & HEALTH
    # ═══════════════════════════════════════════════════════════════

    def ping(self) -> bool:
        """
        Health check. Returns True if exchange is reachable.
        Override with actual connectivity check for live exchanges.
        """
        return True

    def get_server_time(self) -> Optional[datetime]:
        """
        Get exchange server time.
        Useful for sync checks.
        """
        return datetime.utcnow()

    def is_market_open(self, symbol: Optional[str] = None) -> bool:
        """
        Check if market is open for trading.
        Crypto markets are 24/7, but stock markets have hours.
        """
        return True

    def get_rate_limit_status(self) -> Dict:
        """
        Get current rate limit status.

        Returns:
            {
                "requests_remaining": int,
                "requests_limit": int,
                "reset_time": str,
            }
        """
        return {
            "requests_remaining": 1000,
            "requests_limit": 1200,
            "reset_time": "",
        }

    def close(self) -> None:
        """
        Teardown: close websockets, HTTP sessions, file handles.
        Called during graceful shutdown.
        """
        pass

    # ═══════════════════════════════════════════════════════════════
    # HELPERS
    # ═══════════════════════════════════════════════════════════════

    @staticmethod
    def _rejection(symbol: str, action: str, reason: str = "") -> Dict:
        """
        Standard rejection receipt builder.

        Usage:
            return self._rejection("BTC/USDT", "BUY", "Insufficient balance")
        """
        return {
            "status": OrderStatus.REJECTED.value,
            "symbol": symbol,
            "action": action,
            "side": action,
            "reason": reason,
            "timestamp": datetime.utcnow().isoformat(),
        }

    @staticmethod
    def _success_receipt(
        symbol: str,
        action: str,
        price: float,
        quantity: float,
        fee: float = 0.0,
        order_id: str = "",
        mode: str = "PAPER",
        exchange: str = "UNKNOWN",
        **extra
    ) -> Dict:
        """
        Standard success receipt builder.

        Usage:
            return self._success_receipt(
                symbol="BTC/USDT",
                action="BUY",
                price=50000.0,
                quantity=0.001,
                fee=0.05,
                order_id="12345",
                mode="PAPER",
                exchange="PAPER"
            )
        """
        cost = price * quantity

        receipt = {
            "status": OrderStatus.FILLED.value,
            "symbol": symbol,
            "action": action,
            "side": action,
            "order_type": "MARKET",
            "price": price,
            "quantity": quantity,
            "filled_qty": quantity,
            "cost": cost,
            "fee": fee,
            "fee_currency": "USDT",
            "order_id": order_id,
            "timestamp": datetime.utcnow().isoformat(),
            "mode": mode,
            "exchange": exchange,
        }

        # Add proceeds for SELL orders
        if action.upper() == "SELL":
            receipt["proceeds"] = cost

        # Merge extra fields
        receipt.update(extra)

        return receipt

    def validate_order(
        self,
        symbol: str,
        quantity: float,
        side: str,
        price: Optional[float] = None
    ) -> Tuple[bool, str]:
        """
        Validate order parameters before submission.

        Returns:
            (is_valid, error_message)
        """
        if quantity <= 0:
            return False, "Quantity must be positive"

        info = self.get_symbol_info(symbol)

        if quantity < info.get("min_quantity", 0):
            return False, f"Quantity below minimum: {info['min_quantity']}"

        if quantity > info.get("max_quantity", float('inf')):
            return False, f"Quantity above maximum: {info['max_quantity']}"

        check_price = price or self.get_price(symbol)
        notional = check_price * quantity

        if notional < info.get("min_notional", 0):
            return False, f"Order value below minimum: {info['min_notional']}"

        if side.upper() == "BUY":
            if notional > self.get_buying_power():
                return False, "Insufficient buying power"

        return True, ""

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """
        Round quantity to valid step size for symbol.
        """
        info = self.get_symbol_info(symbol)
        precision = info.get("quantity_precision", 8)
        step = info.get("quantity_step", 0.00001)

        # Round to step
        rounded = round(quantity / step) * step
        # Apply precision
        return round(rounded, precision)

    def round_price(self, symbol: str, price: float) -> float:
        """
        Round price to valid precision for symbol.
        """
        info = self.get_symbol_info(symbol)
        precision = info.get("price_precision", 8)
        return round(price, precision)

    # ═══════════════════════════════════════════════════════════════
    # REPRESENTATION
    # ═══════════════════════════════════════════════════════════════

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} | {self.EXCHANGE_NAME} | {self.MODE}>"

    def __str__(self) -> str:
        return f"{self.EXCHANGE_NAME} ({self.MODE})"