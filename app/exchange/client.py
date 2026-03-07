# app/exchange/client.py

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

5. TRADE DURATION TRACKING: Entry times are recorded for
   calculating trade duration on exit.

6. INR SUPPORT: Optional INR conversion for display in
   Indian Rupee values.

Receipt Format (BUY):
─────────────────────
    {
        "status":       "FILLED" | "PARTIAL" | "REJECTED",
        "symbol":       str,
        "action":       "BUY",
        "side":         "BUY",
        "order_type":   "MARKET" | "LIMIT",
        "price":        float,          # actual fill price (USD)
        "price_inr":    float,          # price in INR (optional)
        "quantity":     float,          # requested quantity
        "filled_qty":   float,          # actual filled quantity
        "cost":         float,          # price * filled_qty (USD)
        "cost_inr":     float,          # cost in INR (optional)
        "fee":          float,          # exchange fee charged (USD)
        "fee_inr":      float,          # fee in INR (optional)
        "fee_currency": str,            # fee currency (e.g., "USDT")
        "order_id":     str,
        "timestamp":    str,            # ISO format
        "mode":         str,            # "PAPER" | "LIVE" | "SIMULATION"
        "exchange":     str,            # "ALPACA" | "BINANCE" | "PAPER"
    }

Receipt Format (SELL):
──────────────────────
    Same as BUY plus:
        "entry_price":      float,      # original entry price
        "entry_price_inr":  float,      # entry price in INR (optional)
        "proceeds":         float,      # price * filled_qty
        "gross_pnl":        float,      # (exit - entry) * qty
        "gross_pnl_inr":    float,      # gross P/L in INR (optional)
        "net_pnl":          float,      # gross_pnl - fees
        "net_pnl_inr":      float,      # net P/L in INR (optional)
        "pnl_pct":          float,      # P/L as percentage
        "duration_seconds": float,      # trade duration in seconds
        "duration":         str,        # formatted duration ("5m", "2h")
        "close_reason":     str,        # why trade was closed (optional)

Position Format:
────────────────
    {
        "symbol":         str,
        "side":           "long" | "short",
        "quantity":       float,
        "entry_price":    float,
        "entry_time":     str,          # ISO format timestamp
        "current_price":  float,
        "unrealized_pnl": float,
        "market_value":   float,
        "cost_basis":     float,
    }
"""

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

    Features:
    - Standardized order receipts with P/L and duration
    - Trade entry time tracking for duration calculation
    - INR conversion support for Indian market display
    - Position management interface
    - Kill switch integration via close_position methods
    """

    # Exchange identifier - override in subclasses
    EXCHANGE_NAME: str = "UNKNOWN"
    MODE: str = "UNKNOWN"  # "PAPER", "LIVE", or "SIMULATION"

    # Default INR conversion rate
    DEFAULT_USD_TO_INR: float = 83.0

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
            float: Current price in USD, or 0.0 if unavailable

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
                "price_inr":    float,      # INR conversion
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
            "price_inr": self.to_inr(price),
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
            - Should call record_trade_entry() on success
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
            Standardized receipt dict with P/L and duration (see class docstring)

        Note:
            - Does NOT modify balance/positions
            - Controller handles all state mutations
            - Should include duration from record_trade_entry()
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
    # TRADE DURATION TRACKING
    # ═══════════════════════════════════════════════════════════════

    def record_trade_entry(self, symbol: str) -> None:
        """
        Record trade entry time for duration calculation.
        
        Called automatically by buy() on successful fill.
        Override in subclasses to implement tracking.
        
        Args:
            symbol: Trading pair
        """
        pass

    def get_trade_duration(self, symbol: str) -> Tuple[float, str]:
        """
        Get trade duration for a symbol.
        
        Args:
            symbol: Trading pair
            
        Returns:
            (duration_seconds, formatted_duration_string)
            e.g., (300.5, "5m") or (7200, "2h")
        """
        return 0.0, "Unknown"

    def clear_trade_entry(self, symbol: str) -> None:
        """
        Clear trade entry time after exit.
        
        Called automatically by sell() on successful fill.
        Override in subclasses to implement tracking.
        
        Args:
            symbol: Trading pair
        """
        pass

    @staticmethod
    def format_duration(seconds: float) -> str:
        """
        Format duration in human-readable format.
        
        Args:
            seconds: Duration in seconds
            
        Returns:
            Formatted string like "30s", "5.2m", "2.5h", "1.5d"
        """
        if seconds < 60:
            return f"{seconds:.0f}s"
        elif seconds < 3600:
            return f"{seconds / 60:.1f}m"
        elif seconds < 86400:
            return f"{seconds / 3600:.1f}h"
        else:
            return f"{seconds / 86400:.1f}d"

    # ═══════════════════════════════════════════════════════════════
    # INR CONVERSION
    # ═══════════════════════════════════════════════════════════════

    def to_inr(self, usd_amount: float) -> float:
        """
        Convert USD amount to INR.
        
        Override in subclasses with actual conversion rate.
        Default uses DEFAULT_USD_TO_INR (83.0).
        
        Args:
            usd_amount: Amount in USD
            
        Returns:
            Amount in INR
        """
        return usd_amount * self.DEFAULT_USD_TO_INR

    def get_inr_rate(self) -> float:
        """
        Get current USD to INR conversion rate.
        
        Returns:
            Conversion rate (1 USD = X INR)
        """
        return self.DEFAULT_USD_TO_INR

    def update_inr_rate(self, new_rate: float) -> None:
        """
        Update USD to INR conversion rate.
        
        Override in subclasses to implement rate updates.
        
        Args:
            new_rate: New conversion rate
        """
        pass

    # ═══════════════════════════════════════════════════════════════
    # ACCOUNT INFO (required)
    # ═══════════════════════════════════════════════════════════════

    @abstractmethod
    def get_balance(self) -> float:
        """
        Return current available balance in quote currency (e.g., USDT).

        This is the AVAILABLE balance, not including funds locked
        in open orders or positions.
        
        Returns:
            Balance in USD/USDT
        """

    def get_balance_inr(self) -> float:
        """
        Return current available balance in INR.
        
        Returns:
            Balance in INR
        """
        return self.to_inr(self.get_balance())

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
                    "entry_time": "2024-01-15T10:30:00",
                    "current_price": 51000.0,
                    "unrealized_pnl": 1.0,
                    "unrealized_pnl_inr": 83.0,
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
        """Get total unrealized PnL across all positions (USD)."""
        positions = self.get_open_positions()
        return sum(p.get("unrealized_pnl", 0.0) for p in positions.values())

    def get_unrealized_pnl_inr(self) -> float:
        """Get total unrealized PnL across all positions (INR)."""
        return self.to_inr(self.get_unrealized_pnl())

    def close_position(
        self,
        symbol: str,
        quantity: Optional[float] = None,
        reason: str = "Manual close",
    ) -> Dict:
        """
        Close a position (full or partial).

        Args:
            symbol: Position symbol
            quantity: Amount to close (None = close all)
            reason: Reason for closing (for audit trail)

        Returns:
            Sell order receipt with P/L and duration
        """
        pos = self.get_position(symbol)
        if not pos:
            return self._rejection(symbol, "SELL", "No position to close")

        close_qty = quantity or pos.get("quantity", 0)
        result = self.sell(symbol, close_qty)

        # Add close reason to receipt
        if result.get("status") == "FILLED":
            result["close_reason"] = reason

        return result

    def close_all_positions(
        self,
        reason: str = "Close all positions",
    ) -> List[Dict]:
        """
        Close all open positions.
        
        Used by kill switch for emergency closing.

        Args:
            reason: Reason for closing (for audit trail)

        Returns:
            List of sell order receipts with P/L and duration
        """
        results = []
        for symbol, pos in self.get_open_positions().items():
            qty = pos.get("quantity", 0)
            if qty > 0:
                result = self.close_position(symbol, qty, reason)
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
                "balance_usd": float,
                "balance_inr": float,
                "total_equity": float,
                "buying_power": float,
                "open_positions": int,
                "total_exposure": float,
                "exposure_pct": float,
                "unrealized_pnl_usd": float,
                "unrealized_pnl_inr": float,
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
            "balance_usd": balance,
            "balance_inr": self.to_inr(balance),
            "total_equity": total_equity,
            "total_equity_inr": self.to_inr(total_equity),
            "buying_power": self.get_buying_power(),
            "open_positions": len(positions),
            "total_exposure": exposure,
            "exposure_inr": self.to_inr(exposure),
            "exposure_pct": (exposure / total_equity * 100) if total_equity > 0 else 0,
            "unrealized_pnl_usd": unrealized,
            "unrealized_pnl_inr": self.to_inr(unrealized),
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
            "price": 0.0,
            "quantity": 0.0,
            "filled_qty": 0.0,
            "cost": 0.0,
            "fee": 0.0,
            "order_id": "",
            "reason": reason,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def _success_receipt(
        self,
        symbol: str,
        action: str,
        price: float,
        quantity: float,
        fee: float = 0.0,
        order_id: str = "",
        entry_price: float = 0.0,
        duration_seconds: float = 0.0,
        include_inr: bool = True,
        **extra
    ) -> Dict:
        """
        Standard success receipt builder with P/L and duration support.

        Usage:
            return self._success_receipt(
                symbol="BTC/USDT",
                action="BUY",
                price=50000.0,
                quantity=0.001,
                fee=0.05,
                order_id="12345",
            )
            
        For SELL with P/L:
            return self._success_receipt(
                symbol="BTC/USDT",
                action="SELL",
                price=51000.0,
                quantity=0.001,
                fee=0.05,
                order_id="12346",
                entry_price=50000.0,
                duration_seconds=3600,
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
            "mode": self.MODE,
            "exchange": self.EXCHANGE_NAME,
        }

        # Add INR values
        if include_inr:
            receipt["price_inr"] = self.to_inr(price)
            receipt["cost_inr"] = self.to_inr(cost)
            receipt["fee_inr"] = self.to_inr(fee)

        # Add P/L info for SELL orders
        if action.upper() == "SELL":
            receipt["proceeds"] = cost

            if entry_price > 0:
                gross_pnl = (price - entry_price) * quantity
                net_pnl = gross_pnl - fee
                pnl_pct = (gross_pnl / (entry_price * quantity) * 100) if entry_price > 0 else 0

                receipt["entry_price"] = entry_price
                receipt["gross_pnl"] = round(gross_pnl, 8)
                receipt["net_pnl"] = round(net_pnl, 8)
                receipt["pnl_pct"] = round(pnl_pct, 2)

                if include_inr:
                    receipt["entry_price_inr"] = self.to_inr(entry_price)
                    receipt["gross_pnl_inr"] = self.to_inr(gross_pnl)
                    receipt["net_pnl_inr"] = self.to_inr(net_pnl)

            # Add duration
            if duration_seconds > 0:
                receipt["duration_seconds"] = round(duration_seconds, 1)
                receipt["duration"] = self.format_duration(duration_seconds)

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