# app/exchange/paper.py

import math
import random
import time
import os
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from app.exchange.client import ExchangeClient, OrderStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ── Realistic seed prices ─────────────────────────────────────────
SEED_PRICES: Dict[str, float] = {
    "BTC/USDT": 67000.0,
    "ETH/USDT": 3400.0,
    "SOL/USDT": 145.0,
    "BNB/USDT": 590.0,
    "XRP/USDT": 0.52,
    "ADA/USDT": 0.45,
    "DOGE/USDT": 0.12,
    "AVAX/USDT": 36.0,
    "DOT/USDT": 7.2,
    "MATIC/USDT": 0.68,
    "LINK/USDT": 14.5,
    "UNI/USDT": 9.8,
    "ATOM/USDT": 8.5,
    "LTC/USDT": 85.0,
    "ETC/USDT": 26.0,
}

DEFAULT_SEED = 100.0


def _normalize_symbol(symbol: str) -> str:
    """
    Normalize symbol format to 'BASE/QUOTE'.
    Handles: BTCUSDT → BTC/USDT, btc/usdt → BTC/USDT
    """
    s = symbol.upper().strip()
    if "/" in s:
        return s

    # Common quote currencies (longest first)
    for quote in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH"):
        if s.endswith(quote):
            base = s[: -len(quote)]
            if base:
                return f"{base}/{quote}"

    return s


class PaperExchange(ExchangeClient):
    """
    Production-Grade Paper Trading Exchange

    Features:
    ─────────
    - Geometric Brownian Motion price simulation
    - Price caching: IDEMPOTENT within a cycle
    - Time-based candle generation (not per-call)
    - Continuous OHLCV history per symbol
    - Realistic bid-ask spread simulation
    - Slippage model with configurable bands
    - Multiple timeframe support
    - Order validation and rejection handling
    - Full ExchangeClient interface implementation

    Accounting:
    ───────────
    Exchange ONLY returns fill receipts.
    Controller handles all state mutations.
    This prevents double-counting bugs.

    Configuration via .env:
    ───────────────────────
    FEE_PCT=0.001           # 0.1% fee
    SLIPPAGE_PCT=0.0005     # 0.05% slippage
    """

    EXCHANGE_NAME = "PAPER"
    MODE = "PAPER"

    # ── Configurable defaults ─────────────────────────────────────
    DEFAULT_SPREAD_PCT = 0.0002      # 0.02% bid-ask spread
    DEFAULT_SLIPPAGE_PCT = 0.0003    # 0.03% max slippage
    DEFAULT_FEE_PCT = 0.001          # 0.1% taker fee
    CANDLE_CACHE_MAX = 500           # Max candles stored per symbol
    DEFAULT_CANDLE_INTERVAL = 300    # 5-minute candles

    # ── GBM parameters ────────────────────────────────────────────
    GBM_MU = 0.00005                 # Tiny positive drift
    GBM_SIGMA = 0.0012               # Per-tick volatility ~0.12%
    CANDLE_GBM_SIGMA = 0.0015        # Intra-candle volatility

    # ── Volatility regimes (for realistic simulation) ─────────────
    VOLATILITY_REGIMES = {
        "low": 0.0008,
        "normal": 0.0015,
        "high": 0.0030,
        "extreme": 0.0050,
    }

    def __init__(
        self,
        state_manager=None,
        initial_balance: float = 100.0,
        spread_pct: float = None,
        slippage_pct: float = None,
        fee_pct: float = None,
        candle_interval: int = None,
    ):
        self.state = state_manager

        # Load from env or use defaults
        self.spread_pct = spread_pct if spread_pct is not None else float(
            os.getenv("SPREAD_PCT", self.DEFAULT_SPREAD_PCT)
        )
        self.slippage_pct = slippage_pct if slippage_pct is not None else float(
            os.getenv("SLIPPAGE_PCT", self.DEFAULT_SLIPPAGE_PCT)
        )
        self.fee_pct = fee_pct if fee_pct is not None else float(
            os.getenv("FEE_PCT", self.DEFAULT_FEE_PCT)
        )
        self.candle_interval = candle_interval or int(
            os.getenv("ANALYSIS_INTERVAL", self.DEFAULT_CANDLE_INTERVAL)
        )

        # ── Per-cycle price cache (reset by begin_cycle) ──────────
        self._price_cache: Dict[str, float] = {}

        # ── Candle caches per symbol and timeframe ────────────────
        self._candle_cache: Dict[str, List[Dict]] = {}  # "symbol_timeframe" -> candles
        self._last_candle_time: Dict[str, float] = {}

        # ── Ticker data cache ─────────────────────────────────────
        self._ticker_cache: Dict[str, Dict] = {}

        # ── Order tracking ────────────────────────────────────────
        self._order_counter: int = 0
        self._order_history: List[Dict] = []
        self._pending_orders: Dict[str, Dict] = {}

        # ── Volatility regime ─────────────────────────────────────
        self._current_volatility = "normal"
        self._volatility_change_time = time.time()

        # ── Statistics ────────────────────────────────────────────
        self._stats = {
            "total_orders": 0,
            "filled_orders": 0,
            "rejected_orders": 0,
            "total_volume": 0.0,
            "total_fees": 0.0,
        }

        # ── Initialize balance if first run ───────────────────────
        if self.state:
            if self.state.get("balance") is None:
                self.state.set("balance", initial_balance)
                logger.info(f"💰 Paper exchange: Initial balance ${initial_balance:.2f}")

        logger.info(
            f"✅ PaperExchange initialized | "
            f"Spread={self.spread_pct*100:.3f}% | "
            f"Slippage={self.slippage_pct*100:.3f}% | "
            f"Fee={self.fee_pct*100:.2f}%"
        )

    # ═══════════════════════════════════════════════════════════════
    # SYMBOL HANDLING
    # ═══════════════════════════════════════════════════════════════

    def normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol to standard format."""
        return _normalize_symbol(symbol)

    def denormalize_symbol(self, symbol: str) -> str:
        """Paper exchange uses standard format."""
        return _normalize_symbol(symbol)

    # ═══════════════════════════════════════════════════════════════
    # CYCLE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def begin_cycle(self) -> None:
        """
        Called at start of each trading cycle.
        Clears price cache for fresh prices.
        Candle cache persists across cycles.
        """
        self._price_cache.clear()
        self._maybe_change_volatility()
        logger.debug("🔄 Paper exchange cycle started — price cache cleared")

    def end_cycle(self) -> None:
        """Called at end of trading cycle."""
        # Update any pending limit orders
        self._process_pending_orders()

    def _maybe_change_volatility(self) -> None:
        """Randomly shift volatility regime for realism."""
        now = time.time()
        # Change regime roughly every 30 minutes
        if now - self._volatility_change_time > 1800:
            regimes = list(self.VOLATILITY_REGIMES.keys())
            weights = [0.1, 0.6, 0.25, 0.05]  # Mostly normal
            self._current_volatility = random.choices(regimes, weights=weights)[0]
            self._volatility_change_time = now
            logger.debug(f"📊 Volatility regime: {self._current_volatility}")

    # ═══════════════════════════════════════════════════════════════
    # MARKET DATA
    # ═══════════════════════════════════════════════════════════════

    def get_price(self, symbol: str) -> float:
        """
        GBM price walk — IDEMPOTENT within a cycle.

        First call per symbol per cycle generates new price.
        Subsequent calls return cached price.
        Cache cleared by begin_cycle().
        """
        symbol = _normalize_symbol(symbol)

        # Return cached price if already computed this cycle
        if symbol in self._price_cache:
            return self._price_cache[symbol]

        # Get base price from state or seed
        if self.state:
            base = self.state.get(f"paper_price_{symbol}")
        else:
            base = None
        
        if base is None:
            base = SEED_PRICES.get(symbol, DEFAULT_SEED)

        # Get current volatility
        sigma = self.VOLATILITY_REGIMES.get(self._current_volatility, self.GBM_SIGMA)

        # GBM step
        z = random.gauss(0, 1)
        price = base * math.exp(
            (self.GBM_MU - 0.5 * sigma ** 2) + sigma * z
        )
        price = round(max(price, 1e-8), 8)

        # Cache for this cycle AND persist for next cycle
        self._price_cache[symbol] = price
        if self.state:
            self.state.set(f"paper_price_{symbol}", price)

        return price

    def get_ticker(self, symbol: str) -> Dict:
        """Get extended ticker information with bid/ask."""
        symbol = _normalize_symbol(symbol)
        price = self.get_price(symbol)

        # Calculate bid/ask with spread
        half_spread = price * self.spread_pct / 2
        bid = round(price - half_spread, 8)
        ask = round(price + half_spread, 8)
        spread = ask - bid

        # Simulate 24h stats
        change_pct = random.gauss(0, 2)  # Random daily change
        change_24h = price * (change_pct / 100)
        high_24h = price * (1 + abs(random.gauss(0, 0.02)))
        low_24h = price * (1 - abs(random.gauss(0, 0.02)))

        # Volume based on price level
        base_volume = 50000 / max(price, 0.01)
        volume_24h = base_volume * random.uniform(0.5, 2.0)

        ticker = {
            "symbol": symbol,
            "price": price,
            "bid": bid,
            "ask": ask,
            "spread": round(spread, 8),
            "spread_pct": round((spread / price) * 100, 4) if price > 0 else 0,
            "volume_24h": round(volume_24h, 2),
            "change_24h": round(change_24h, 4),
            "change_pct": round(change_pct, 2),
            "high_24h": round(high_24h, 8),
            "low_24h": round(low_24h, 8),
            "timestamp": datetime.utcnow().isoformat(),
        }

        self._ticker_cache[symbol] = ticker
        return ticker

    def get_orderbook(self, symbol: str, depth: int = 10) -> Dict:
        """Generate simulated order book."""
        symbol = _normalize_symbol(symbol)
        price = self.get_price(symbol)

        bids = []
        asks = []

        # Generate order book levels
        for i in range(depth):
            # Bids below current price
            bid_price = price * (1 - (i + 1) * 0.0005)
            bid_qty = random.uniform(0.1, 10.0) * (depth - i) / depth
            bids.append((round(bid_price, 8), round(bid_qty, 6)))

            # Asks above current price
            ask_price = price * (1 + (i + 1) * 0.0005)
            ask_qty = random.uniform(0.1, 10.0) * (depth - i) / depth
            asks.append((round(ask_price, 8), round(ask_qty, 6)))

        return {
            "symbol": symbol,
            "bids": bids,
            "asks": asks,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def get_recent_candles(
        self,
        symbol: str,
        limit: int = 150,
        timeframe: str = "5m"
    ) -> List[Dict]:
        """
        Returns continuous OHLCV candle history.

        Time-based generation:
        - New candle only added when interval elapses
        - Prevents indicator pollution from spurious injection
        - Supports multiple timeframes
        """
        symbol = _normalize_symbol(symbol)
        cache_key = f"{symbol}_{timeframe}"

        # Get timeframe in seconds
        tf_seconds = self._timeframe_to_seconds(timeframe)
        
        cache = self._candle_cache.get(cache_key, [])
        now = time.time()

        # ── Seed history if empty ─────────────────────────────────
        if not cache:
            if self.state:
                seed_price = self.state.get(f"paper_price_{symbol}")
            else:
                seed_price = None
            
            if seed_price is None:
                seed_price = SEED_PRICES.get(symbol, DEFAULT_SEED)
            
            min_candles = max(limit, 100)  # Enough for indicators
            cache = self._generate_candle_series(seed_price, min_candles, timeframe)
            self._last_candle_time[cache_key] = now
            logger.info(
                f"🕯️ Seeded {len(cache)} {timeframe} candles for {symbol} "
                f"(start=${seed_price:.2f})"
            )

        # ── Append new candles if interval elapsed ────────────────
        last_time = self._last_candle_time.get(cache_key, 0)
        elapsed = now - last_time

        if elapsed >= tf_seconds:
            num_new = min(int(elapsed // tf_seconds), 20)  # Cap catch-up
            last_close = cache[-1]["close"]

            for _ in range(num_new):
                new_candle = self._make_candle(last_close, timeframe)
                cache.append(new_candle)
                last_close = new_candle["close"]

            self._last_candle_time[cache_key] = now

            if num_new > 0:
                logger.debug(f"🕯️ Generated {num_new} {timeframe} candle(s) for {symbol}")

        # ── Trim cache ────────────────────────────────────────────
        if len(cache) > self.CANDLE_CACHE_MAX:
            cache = cache[-self.CANDLE_CACHE_MAX:]

        self._candle_cache[cache_key] = cache

        # ── Update state price to latest close ────────────────────
        if cache and self.state:
            self.state.set(f"paper_price_{symbol}", cache[-1]["close"])

        return cache[-limit:]

    def _timeframe_to_seconds(self, timeframe: str) -> int:
        """Convert timeframe string to seconds."""
        tf_map = {
            "1m": 60,
            "3m": 180,
            "5m": 300,
            "15m": 900,
            "30m": 1800,
            "1h": 3600,
            "2h": 7200,
            "4h": 14400,
            "6h": 21600,
            "12h": 43200,
            "1d": 86400,
        }
        return tf_map.get(timeframe, 300)

    # ═══════════════════════════════════════════════════════════════
    # CANDLE GENERATION
    # ═══════════════════════════════════════════════════════════════

    def _generate_candle_series(
        self,
        seed_price: float,
        count: int,
        timeframe: str = "5m"
    ) -> List[Dict]:
        """Generate a series of continuous candles."""
        candles = []
        price = seed_price
        tf_seconds = self._timeframe_to_seconds(timeframe)

        for i in range(count):
            # Generate timestamp going backwards
            timestamp = datetime.utcnow() - timedelta(seconds=(count - i) * tf_seconds)
            candle = self._make_candle(price, timeframe, timestamp)
            candles.append(candle)
            price = candle["close"]

        return candles

    def _make_candle(
        self,
        prev_close: float,
        timeframe: str = "5m",
        timestamp: datetime = None
    ) -> Dict:
        """
        Generate one realistic OHLCV candle.

        Properties:
        - Open gaps slightly from previous close
        - Close via GBM with regime-based volatility
        - Valid high/low (high ≥ max, low ≤ min)
        - Volume correlates with range
        """
        # Adjust volatility based on timeframe
        tf_seconds = self._timeframe_to_seconds(timeframe)
        time_factor = math.sqrt(tf_seconds / 300)  # Normalize to 5m

        sigma = self.VOLATILITY_REGIMES.get(
            self._current_volatility, self.CANDLE_GBM_SIGMA
        ) * time_factor
        mu = self.GBM_MU * (tf_seconds / 300)

        z = random.gauss(0, 1)

        # Open with tiny gap from previous close
        open_p = prev_close * (1 + random.gauss(0, 0.0003))

        # Close via GBM
        close_p = open_p * math.exp((mu - 0.5 * sigma ** 2) + sigma * z)

        # Ensure positive
        open_p = max(open_p, 1e-8)
        close_p = max(close_p, 1e-8)

        # High and Low
        candle_range = abs(close_p - open_p)
        wick_extension = max(candle_range * 0.5, prev_close * 0.0002)

        high_p = max(open_p, close_p) + random.uniform(0, wick_extension)
        low_p = min(open_p, close_p) - random.uniform(0, wick_extension)
        low_p = max(low_p, 1e-8)

        # Volume: inversely related to price, boosted by range
        base_vol = max(1.0, 50000.0 / max(prev_close, 0.01))
        range_factor = 1 + (candle_range / max(prev_close, 0.01) * 200)
        volume = base_vol * range_factor * random.uniform(0.6, 1.4) * time_factor

        return {
            "open": round(open_p, 8),
            "high": round(high_p, 8),
            "low": round(low_p, 8),
            "close": round(close_p, 8),
            "volume": round(volume, 2),
            "timestamp": (timestamp or datetime.utcnow()).isoformat(),
            "timeframe": timeframe,
        }

    # ═══════════════════════════════════════════════════════════════
    # ORDER EXECUTION
    # ═══════════════════════════════════════════════════════════════

    def buy(
        self,
        symbol: str,
        quantity: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Dict:
        """
        Simulate a market/limit BUY order.
        Returns receipt. Does NOT modify state.
        """
        symbol = _normalize_symbol(symbol)
        self._stats["total_orders"] += 1

        # Validate
        is_valid, error = self.validate_order(symbol, quantity, "BUY", price)
        if not is_valid:
            self._stats["rejected_orders"] += 1
            logger.warning(f"❌ PAPER BUY rejected: {error}")
            return self._rejection(symbol, "BUY", error)

        # Handle limit orders
        if order_type.upper() == "LIMIT":
            return self._create_limit_order(symbol, "BUY", quantity, price)

        # Market order execution
        mid_price = self.get_price(symbol)

        # Apply spread (buy at ask)
        ask_price = mid_price * (1 + self.spread_pct)

        # Apply random slippage
        slip = ask_price * self.slippage_pct * random.uniform(0.2, 1.0)
        fill_price = round(ask_price + slip, 8)

        cost = round(fill_price * quantity, 8)
        fee = round(cost * self.fee_pct, 8)

        # Check balance
        if self.state:
            balance = self.state.get("balance", 0)
            if cost + fee > balance:
                self._stats["rejected_orders"] += 1
                logger.warning(
                    f"❌ PAPER BUY rejected | {symbol} | "
                    f"Cost=${cost:.4f} + Fee=${fee:.4f} > Balance=${balance:.2f}"
                )
                return self._rejection(symbol, "BUY", "Insufficient balance")

        # Generate order
        self._order_counter += 1
        order_id = f"PAPER-B-{self._order_counter:06d}"

        self._stats["filled_orders"] += 1
        self._stats["total_volume"] += cost
        self._stats["total_fees"] += fee

        receipt = self._success_receipt(
            symbol=symbol,
            action="BUY",
            price=fill_price,
            quantity=quantity,
            fee=fee,
            order_id=order_id,
            mode="PAPER",
            exchange="PAPER",
            cost=cost,
        )

        self._order_history.append(receipt)

        logger.info(
            f"📝🟢 PAPER BUY | {symbol} | "
            f"Qty={quantity:.6f} @ ${fill_price:.6f} | "
            f"Cost=${cost:.4f} | Fee=${fee:.4f}"
        )

        return receipt

    def sell(
        self,
        symbol: str,
        quantity: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Dict:
        """
        Simulate a market/limit SELL order.

        Handles TWO distinct cases:
        ─────────────────────────────
        Case 1 — CLOSE LONG: An existing BUY position is open.
          Validates quantity ≤ position size.
           Calculates PnL from entry price.

        Case 2 — OPEN SHORT: No position exists (or action == SELL).
        Treated as a synthetic short entry.
        Requires sufficient balance as margin collateral.
        PnL is calculated on exit (in buy() when closing short).

        Returns receipt. Does NOT modify state.
        """
        symbol = _normalize_symbol(symbol)
        self._stats["total_orders"] += 1

        if quantity <= 0:
            self._stats["rejected_orders"] += 1
            return self._rejection(symbol, "SELL", "Invalid quantity")

        # ── Detect position context ───────────────────────────────
        position = None
        if self.state:
            position = self.state.get_position(symbol)

        is_close_long = (
            position is not None
            and position.get("action", "BUY").upper() == "BUY"
        )
        is_open_short = not is_close_long  # No position, or existing position is already SHORT

        # ── Handle limit orders ───────────────────────────────────
        if order_type.upper() == "LIMIT":
            return self._create_limit_order(symbol, "SELL", quantity, price)

        # ── Market price ──────────────────────────────────────────
        mid_price = self.get_price(symbol)
        bid_price = mid_price * (1 - self.spread_pct)
        slip = bid_price * self.slippage_pct * random.uniform(0.2, 1.0)
        fill_price = round(max(bid_price - slip, 1e-8), 8)

        # ══════════════════════════════════════════════════════════
        #  CASE 1: CLOSE LONG
        # ══════════════════════════════════════════════════════════
        if is_close_long:
            pos_qty = position.get("quantity", 0)

            if quantity > pos_qty * 1.001:  # float tolerance
                self._stats["rejected_orders"] += 1
                logger.warning(
                    f"❌ PAPER SELL rejected | {symbol} | "
                    f"Qty={quantity} > Position={pos_qty}"
                )
                return self._rejection(symbol, "SELL", f"Quantity exceeds position ({pos_qty:.6f})")

            quantity = min(quantity, pos_qty)  # clamp

            proceeds = round(fill_price * quantity, 8)
            fee = round(proceeds * self.fee_pct, 8)

            entry_price = position.get("entry_price", position.get("avg_price", fill_price))
            gross_pnl = round((fill_price - entry_price) * quantity, 8)
            net_pnl = round(gross_pnl - fee, 8)

            self._order_counter += 1
            order_id = f"PAPER-S-{self._order_counter:06d}"

            self._stats["filled_orders"] += 1
            self._stats["total_volume"] += proceeds
            self._stats["total_fees"] += fee

            receipt = self._success_receipt(
                symbol=symbol,
                action="SELL",
                price=fill_price,
                quantity=quantity,
                fee=fee,
                order_id=order_id,
                mode="PAPER",
                exchange="PAPER",
                proceeds=proceeds,
                gross_pnl=gross_pnl,
                net_pnl=net_pnl,
            )
            self._order_history.append(receipt)

            logger.info(
                f"📝🔴 PAPER SELL (Close Long) | {symbol} | "
                f"Qty={quantity:.6f} @ ${fill_price:.6f} | "
                f"PnL=${net_pnl:+.4f} | Fee=${fee:.4f}"
            )
            return receipt

        # ══════════════════════════════════════════════════════════
        #  CASE 2: OPEN SHORT
        # ══════════════════════════════════════════════════════════
        # For paper shorts we require a margin reserve equal to
        # the notional value of the position (100% collateral).
        notional = fill_price * quantity
        fee = round(notional * self.fee_pct, 8)
        margin_required = notional + fee  # full collateral

        if self.state:
            balance = self.state.get("balance", 0)
            if margin_required > balance:
                self._stats["rejected_orders"] += 1
                logger.warning(
                    f"❌ PAPER SHORT rejected | {symbol} | "
                    f"Margin=${margin_required:.4f} > Balance=${balance:.2f}"
                )
                return self._rejection(symbol, "SELL", f"Insufficient margin (need ${margin_required:.2f}, have ${balance:.2f})")

        self._order_counter += 1
        order_id = f"PAPER-SS-{self._order_counter:06d}"  # SS = Short Sell

        self._stats["filled_orders"] += 1
        self._stats["total_volume"] += notional
        self._stats["total_fees"] += fee

        receipt = self._success_receipt(
            symbol=symbol,
            action="SELL",          # keeps controller logic intact
            price=fill_price,
            quantity=quantity,
            fee=fee,
            order_id=order_id,
            mode="PAPER",
            exchange="PAPER",
            proceeds=notional,      # credited to balance on open
            gross_pnl=0.0,          # PnL realised on close (buy-to-cover)
            net_pnl=0.0,
            is_short_open=True,     # metadata flag — informational only
        )
        self._order_history.append(receipt)

        logger.info(
            f"📝🔴 PAPER SELL (Open Short) | {symbol} | "
            f"Qty={quantity:.6f} @ ${fill_price:.6f} | "
            f"Margin=${margin_required:.4f} | Fee=${fee:.4f}"
        )
        return receipt

    def _create_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        limit_price: float
    ) -> Dict:
        """Create a pending limit order."""
        self._order_counter += 1
        order_id = f"PAPER-L-{self._order_counter:06d}"

        order = {
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "limit_price": limit_price,
            "status": "PENDING",
            "order_type": "LIMIT",
            "created_at": datetime.utcnow().isoformat(),
        }

        self._pending_orders[order_id] = order
        
        logger.info(
            f"📝 PAPER LIMIT {side} | {symbol} | "
            f"Qty={quantity:.6f} @ ${limit_price:.6f} | ID={order_id}"
        )

        return {
            "status": OrderStatus.PENDING.value,
            "symbol": symbol,
            "action": side,
            "side": side,
            "order_type": "LIMIT",
            "quantity": quantity,
            "limit_price": limit_price,
            "order_id": order_id,
            "timestamp": datetime.utcnow().isoformat(),
            "mode": "PAPER",
        }

    def _process_pending_orders(self) -> None:
        """Check and fill pending limit orders."""
        filled = []

        for order_id, order in self._pending_orders.items():
            symbol = order["symbol"]
            current_price = self.get_price(symbol)
            limit_price = order["limit_price"]
            side = order["side"]

            should_fill = False

            if side == "BUY" and current_price <= limit_price:
                should_fill = True
            elif side == "SELL" and current_price >= limit_price:
                should_fill = True

            if should_fill:
                logger.info(f"✅ Limit order filled: {order_id}")
                filled.append(order_id)

        for order_id in filled:
            del self._pending_orders[order_id]

    # ═══════════════════════════════════════════════════════════════
    # ORDER MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """Cancel a pending order."""
        if order_id in self._pending_orders:
            order = self._pending_orders.pop(order_id)
            logger.info(f"🚫 Order cancelled: {order_id}")
            return {
                "status": "CANCELLED",
                "order_id": order_id,
                "symbol": order.get("symbol", ""),
            }

        return {
            "status": "REJECTED",
            "order_id": order_id,
            "reason": "Order not found",
        }

    def cancel_all_orders(self) -> bool:
        """Cancel all pending orders."""
        count = len(self._pending_orders)
        self._pending_orders.clear()
        logger.info(f"🚫 Cancelled {count} pending orders")
        return True

    def get_order_status(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """Get status of an order."""
        # Check pending
        if order_id in self._pending_orders:
            order = self._pending_orders[order_id]
            return {
                "order_id": order_id,
                "symbol": order.get("symbol", ""),
                "status": "PENDING",
                "side": order.get("side", ""),
                "quantity": order.get("quantity", 0),
                "filled_qty": 0,
                "remaining_qty": order.get("quantity", 0),
            }

        # Check history
        for order in reversed(self._order_history):
            if order.get("order_id") == order_id:
                return {
                    "order_id": order_id,
                    "symbol": order.get("symbol", ""),
                    "status": order.get("status", "FILLED"),
                    "side": order.get("action", ""),
                    "quantity": order.get("quantity", 0),
                    "filled_qty": order.get("quantity", 0),
                    "remaining_qty": 0,
                    "avg_price": order.get("price", 0),
                }

        return {"order_id": order_id, "status": "UNKNOWN"}

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get all pending orders."""
        orders = list(self._pending_orders.values())
        if symbol:
            symbol = _normalize_symbol(symbol)
            orders = [o for o in orders if o.get("symbol") == symbol]
        return orders

    # ═══════════════════════════════════════════════════════════════
    # VALIDATION
    # ═══════════════════════════════════════════════════════════════

    def validate_order(
        self,
        symbol: str,
        quantity: float,
        side: str,
        price: Optional[float] = None
    ) -> Tuple[bool, str]:
        """Validate order parameters."""
        if quantity <= 0:
            return False, "Quantity must be positive"

        symbol = _normalize_symbol(symbol)
        info = self.get_symbol_info(symbol)

        if quantity < info.get("min_quantity", 0):
            return False, f"Quantity below minimum: {info['min_quantity']}"

        check_price = price or self.get_price(symbol)
        notional = check_price * quantity

        if notional < info.get("min_notional", 1.0):
            return False, f"Order value below minimum: ${info['min_notional']}"

        if side.upper() == "BUY" and self.state:
            balance = self.state.get("balance", 0)
            fee = notional * self.fee_pct
            if notional + fee > balance:
                return False, f"Insufficient balance (need ${notional + fee:.2f}, have ${balance:.2f})"

        return True, ""

    # ═══════════════════════════════════════════════════════════════
    # ACCOUNT & POSITIONS
    # ═══════════════════════════════════════════════════════════════

    def get_balance(self) -> float:
        """Return current balance from state."""
        if self.state:
            return self.state.get("balance", 0.0)
        return 0.0

    def get_total_balance(self) -> float:
        """Get total equity including positions."""
        balance = self.get_balance()
        positions = self.get_open_positions()
        
        for pos in positions.values():
            qty = pos.get("quantity", 0)
            price = self.get_price(pos.get("symbol", ""))
            balance += qty * price
        
        return balance

    def get_buying_power(self) -> float:
        """Get available buying power."""
        return self.get_balance()

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get position for a symbol."""
        symbol = _normalize_symbol(symbol)
        if self.state:
            pos = self.state.get_position(symbol)
            if pos:
                # Add current price info
                current_price = self.get_price(symbol)
                entry_price = pos.get("entry_price", pos.get("avg_price", current_price))
                qty = pos.get("quantity", 0)
                
                pos["current_price"] = current_price
                pos["market_value"] = qty * current_price
                pos["unrealized_pnl"] = (current_price - entry_price) * qty
                
            return pos
        return None

    def get_open_positions(self) -> Dict[str, Dict]:
        """Return all open positions with current prices."""
        if not self.state:
            return {}
        
        positions = self.state.get_all_positions()
        
        # Add current price info to each position
        for symbol, pos in positions.items():
            current_price = self.get_price(symbol)
            entry_price = pos.get("entry_price", pos.get("avg_price", current_price))
            qty = pos.get("quantity", 0)
            
            pos["current_price"] = current_price
            pos["market_value"] = qty * current_price
            pos["unrealized_pnl"] = round((current_price - entry_price) * qty, 8)
        
        return positions

    def close_position(self, symbol: str, quantity: Optional[float] = None) -> Dict:
        """Close a position."""
        symbol = _normalize_symbol(symbol)
        position = self.get_position(symbol)
        
        if not position:
            return self._rejection(symbol, "SELL", "No position to close")
        
        close_qty = quantity or position.get("quantity", 0)
        return self.sell(symbol, close_qty)

    def close_all_positions(self) -> List[Dict]:
        """Close all open positions."""
        results = []
        positions = self.get_open_positions()
        
        for symbol, pos in positions.items():
            qty = pos.get("quantity", 0)
            if qty > 0:
                result = self.sell(symbol, qty)
                results.append(result)
        
        return results

    def get_account_summary(self) -> Dict:
        """Return comprehensive account summary."""
        positions = self.get_open_positions()
        balance = self.get_balance()
        
        exposure = sum(p.get("market_value", 0) for p in positions.values())
        unrealized = sum(p.get("unrealized_pnl", 0) for p in positions.values())
        total_equity = balance + exposure

        return {
            "balance": round(balance, 2),
            "total_equity": round(total_equity, 2),
            "buying_power": round(balance, 2),
            "open_positions": len(positions),
            "total_exposure": round(exposure, 2),
            "exposure_pct": round((exposure / total_equity * 100) if total_equity > 0 else 0, 2),
            "unrealized_pnl": round(unrealized, 4),
            "mode": "PAPER",
            "exchange": "PAPER",
            "volatility_regime": self._current_volatility,
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ═══════════════════════════════════════════════════════════════
    # SYMBOL INFO
    # ═══════════════════════════════════════════════════════════════

    def get_symbol_info(self, symbol: str) -> Dict:
        """Get trading rules for a symbol."""
        symbol = _normalize_symbol(symbol)
        parts = symbol.split("/")
        base = parts[0] if parts else symbol[:3]
        quote = parts[1] if len(parts) > 1 else "USDT"

        # Price-based minimums
        price = self.get_price(symbol)
        min_notional = 1.0
        min_qty = min_notional / max(price, 0.01)

        return {
            "symbol": symbol,
            "base_asset": base,
            "quote_asset": quote,
            "min_quantity": round(min_qty, 8),
            "max_quantity": 1000000.0,
            "quantity_step": 0.00001,
            "min_notional": min_notional,
            "price_precision": 8,
            "quantity_precision": 8,
            "is_tradable": True,
        }

    def get_tradable_symbols(self) -> List[str]:
        """Get list of tradable symbols."""
        return list(SEED_PRICES.keys())

    # ═══════════════════════════════════════════════════════════════
    # SEEDING & TESTING
    # ═══════════════════════════════════════════════════════════════

    def seed_price(self, symbol: str, price: float) -> None:
        """Manually set price for testing."""
        symbol = _normalize_symbol(symbol)
        self._price_cache[symbol] = price
        if self.state:
            self.state.set(f"paper_price_{symbol}", price)
        logger.info(f"🌱 Price seeded | {symbol} = ${price}")

    def seed_candles(self, symbol: str, candles: List[Dict]) -> None:
        """Manually set candle history for backtesting."""
        symbol = _normalize_symbol(symbol)
        # Add to default timeframe cache
        self._candle_cache[f"{symbol}_5m"] = candles
        self._last_candle_time[f"{symbol}_5m"] = time.time()
        logger.info(f"🌱 Seeded {len(candles)} candles for {symbol}")

    def reset_candles(self, symbol: str) -> None:
        """Clear candle cache for a symbol."""
        symbol = _normalize_symbol(symbol)
        keys_to_remove = [k for k in self._candle_cache if k.startswith(symbol)]
        for key in keys_to_remove:
            self._candle_cache.pop(key, None)
            self._last_candle_time.pop(key, None)
        logger.info(f"🗑️ Candle cache cleared | {symbol}")

    def set_volatility(self, regime: str) -> None:
        """Manually set volatility regime for testing."""
        if regime in self.VOLATILITY_REGIMES:
            self._current_volatility = regime
            logger.info(f"📊 Volatility set to: {regime}")

    def reset_stats(self) -> None:
        """Reset trading statistics."""
        self._stats = {
            "total_orders": 0,
            "filled_orders": 0,
            "rejected_orders": 0,
            "total_volume": 0.0,
            "total_fees": 0.0,
        }

    def get_stats(self) -> Dict:
        """Get trading statistics."""
        return self._stats.copy()

    # ═══════════════════════════════════════════════════════════════
    # HEALTH & UTILITIES
    # ═══════════════════════════════════════════════════════════════

    def ping(self) -> bool:
        """Paper exchange is always available."""
        return True

    def get_server_time(self) -> Optional[datetime]:
        """Return current time."""
        return datetime.utcnow()

    def is_market_open(self, symbol: Optional[str] = None) -> bool:
        """Paper market is always open."""
        return True

    def get_rate_limit_status(self) -> Dict:
        """No rate limits for paper trading."""
        return {
            "requests_remaining": 999999,
            "requests_limit": 999999,
            "reset_time": "",
        }

    def close(self) -> None:
        """Cleanup resources."""
        logger.info("📝 Paper exchange closed")

    def __repr__(self) -> str:
        balance = self.get_balance()
        positions = len(self.get_open_positions())
        return (
            f"<PaperExchange | "
            f"Balance=${balance:.2f} | "
            f"Positions={positions} | "
            f"Vol={self._current_volatility}>"
        )