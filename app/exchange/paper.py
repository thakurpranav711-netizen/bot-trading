# app/exchange/paper.py

import math
import random
import time
from datetime import datetime
from typing import Dict, List, Optional
from app.exchange.client import ExchangeClient
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ── Realistic seed prices ─────────────────────────────────────────
SEED_PRICES: Dict[str, float] = {
    "BTC/USDT":  65000.0,
    "ETH/USDT":  3200.0,
    "SOL/USDT":  145.0,
    "BNB/USDT":  580.0,
    "XRP/USDT":  0.52,
    "ADA/USDT":  0.45,
    "DOGE/USDT": 0.12,
    "AVAX/USDT": 35.0,
    "DOT/USDT":  7.5,
    "MATIC/USDT": 0.70,
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

    # Common quote currencies (longest first to avoid partial match)
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
    - Geometric Brownian Motion price walk (realistic drift + volatility)
    - Price caching: get_price() is IDEMPOTENT within a cycle
    - Time-based candle generation (not per-call)
    - Persistent OHLCV history per symbol (continuous series)
    - Bid-ask spread simulation
    - Slippage model (random within configurable band)
    - Proper order receipts matching ExchangeClient contract
    - NO balance/position mutation (controller handles state)

    Accounting:
        Exchange ONLY returns fill receipts.
        Controller calls state.adjust_balance() and state.add_position().
        This eliminates double-counting bugs.
    """

    # ── Configurable defaults ─────────────────────────────────────
    SPREAD_PCT = 0.0002          # 0.02% bid-ask spread
    SLIPPAGE_PCT = 0.0003        # 0.03% max slippage
    CANDLE_CACHE_MAX = 300       # Max candles stored per symbol
    CANDLE_INTERVAL_SEC = 300    # 5-minute candles (match trading interval)

    # ── GBM parameters ────────────────────────────────────────────
    GBM_MU = 0.00005             # Tiny positive drift
    GBM_SIGMA = 0.0012           # Per-tick volatility ~0.12%
    CANDLE_GBM_SIGMA = 0.0015   # Intra-candle volatility

    def __init__(
        self,
        state_manager,
        initial_balance: float = 100.0,
        spread_pct: float = None,
        slippage_pct: float = None,
        candle_interval: int = None,
    ):
        self.state = state_manager

        self.spread_pct = spread_pct if spread_pct is not None else self.SPREAD_PCT
        self.slippage_pct = slippage_pct if slippage_pct is not None else self.SLIPPAGE_PCT
        self.candle_interval = candle_interval or self.CANDLE_INTERVAL_SEC

        # ── Per-cycle price cache (reset by begin_cycle) ──────────
        self._price_cache: Dict[str, float] = {}

        # ── Candle history keyed by symbol ────────────────────────
        self._candle_cache: Dict[str, List[Dict]] = {}

        # ── Last candle generation timestamp per symbol ───────────
        self._last_candle_time: Dict[str, float] = {}

        # ── Order ID counter ──────────────────────────────────────
        self._order_counter: int = 0

        # ── Initialize balance if first run ───────────────────────
        if self.state.get("balance") is None:
            self.state.set("balance", initial_balance)
            logger.info(f"💰 Initial balance set: ${initial_balance:.2f}")

    # ═════════════════════════════════════════════════════
    #  CYCLE MANAGEMENT
    # ═════════════════════════════════════════════════════

    def begin_cycle(self) -> None:
        """
        Called by controller at start of each trading cycle.
        Clears price cache so get_price() generates fresh prices.
        Candle cache is NOT cleared — candles persist across cycles.
        """
        self._price_cache.clear()
        logger.debug("🔄 Paper exchange cycle reset — price cache cleared")

    def end_cycle(self) -> None:
        """No cleanup needed for paper exchange."""
        pass

    # ═════════════════════════════════════════════════════
    #  MARKET DATA
    # ═════════════════════════════════════════════════════

    def get_price(self, symbol: str) -> float:
        """
        GBM price walk — IDEMPOTENT within a cycle.

        First call per symbol per cycle:
            Generate new price via GBM from last known price.
            Cache it.
        Subsequent calls same cycle:
            Return cached price (same value).

        Cache is cleared by begin_cycle().
        """
        symbol = _normalize_symbol(symbol)

        # Return cached price if already computed this cycle
        if symbol in self._price_cache:
            return self._price_cache[symbol]

        # Get base price from state or seed
        base = (
            self.state.get(f"paper_price_{symbol}")
            or SEED_PRICES.get(symbol, DEFAULT_SEED)
        )

        # GBM step
        z = random.gauss(0, 1)
        price = base * math.exp(
            (self.GBM_MU - 0.5 * self.GBM_SIGMA ** 2) + self.GBM_SIGMA * z
        )
        price = round(max(price, 1e-8), 8)

        # Cache for this cycle AND persist for next cycle
        self._price_cache[symbol] = price
        self.state.set(f"paper_price_{symbol}", price)

        return price

    def get_recent_candles(self, symbol: str, limit: int = 150) -> List[Dict]:
        """
        Returns continuous OHLCV candle history.

        Candle generation is TIME-BASED:
        - New candle only appended if candle_interval seconds have passed
        - Repeated calls within the same interval return same data
        - This prevents indicator pollution from spurious candle injection

        History is seeded on first call to ensure enough data for
        EMA/RSI/MACD calculations (minimum 60 candles).
        """
        symbol = _normalize_symbol(symbol)
        cache = self._candle_cache.get(symbol, [])
        now = time.time()

        # ── Seed history if empty ─────────────────────────────────
        if not cache:
            seed_price = (
                self.state.get(f"paper_price_{symbol}")
                or SEED_PRICES.get(symbol, DEFAULT_SEED)
            )
            min_candles = max(limit, 60)  # Ensure enough for indicators
            cache = self._generate_candle_series(seed_price, min_candles)
            self._last_candle_time[symbol] = now
            logger.info(
                f"🕯️ Seeded {len(cache)} candles for {symbol} "
                f"(start=${seed_price:.2f})"
            )

        # ── Append new candle only if interval elapsed ────────────
        last_time = self._last_candle_time.get(symbol, 0)
        elapsed = now - last_time

        if elapsed >= self.candle_interval:
            # Calculate how many candles to generate (catch up if behind)
            num_new = min(int(elapsed // self.candle_interval), 10)  # Cap at 10
            last_close = cache[-1]["close"]

            for _ in range(num_new):
                new_candle = self._make_candle(last_close)
                cache.append(new_candle)
                last_close = new_candle["close"]

            self._last_candle_time[symbol] = now

            if num_new > 0:
                logger.debug(
                    f"🕯️ Generated {num_new} new candle(s) for {symbol}"
                )

        # ── Trim to max cache size ────────────────────────────────
        if len(cache) > self.CANDLE_CACHE_MAX:
            cache = cache[-self.CANDLE_CACHE_MAX:]

        self._candle_cache[symbol] = cache

        # ── Update state price to latest close ────────────────────
        if cache:
            self.state.set(f"paper_price_{symbol}", cache[-1]["close"])

        return cache[-limit:]

    # ═════════════════════════════════════════════════════
    #  CANDLE GENERATION (INTERNAL)
    # ═════════════════════════════════════════════════════

    def _generate_candle_series(self, seed_price: float, count: int) -> List[Dict]:
        """Generate a series of continuous candles from a seed price."""
        candles = []
        price = seed_price
        for i in range(count):
            candle = self._make_candle(price)
            candles.append(candle)
            price = candle["close"]
        return candles

    def _make_candle(self, prev_close: float) -> Dict:
        """
        Generate one realistic OHLCV candle.

        Properties:
        - Open gaps slightly from previous close (session gap)
        - Close derived via GBM
        - High ≥ max(open, close), Low ≤ min(open, close) (always valid)
        - Volume correlates with price range (volatile = high volume)
        """
        sigma = self.CANDLE_GBM_SIGMA
        mu = self.GBM_MU
        z = random.gauss(0, 1)

        # Open with tiny gap from previous close
        open_p = prev_close * (1 + random.gauss(0, 0.0003))

        # Close via GBM
        close_p = open_p * math.exp((mu - 0.5 * sigma ** 2) + sigma * z)

        # Ensure positive
        open_p = max(open_p, 1e-8)
        close_p = max(close_p, 1e-8)

        # High and Low must be valid (high ≥ both, low ≤ both)
        candle_range = abs(close_p - open_p)
        wick_extension = max(candle_range * 0.5, prev_close * 0.0002)

        high_p = max(open_p, close_p) + random.uniform(0, wick_extension)
        low_p = min(open_p, close_p) - random.uniform(0, wick_extension)
        low_p = max(low_p, 1e-8)  # Never negative

        # Volume: base volume inversely related to price, boosted by range
        base_vol = max(1.0, 50000.0 / max(prev_close, 0.01))
        range_factor = 1 + (candle_range / max(prev_close, 0.01) * 200)
        volume = base_vol * range_factor * random.uniform(0.6, 1.4)

        return {
            "open": round(open_p, 8),
            "high": round(high_p, 8),
            "low": round(low_p, 8),
            "close": round(close_p, 8),
            "volume": round(volume, 2),
            "timestamp": datetime.utcnow().isoformat(),
        }

    # ═════════════════════════════════════════════════════
    #  ORDER EXECUTION
    # ═════════════════════════════════════════════════════

    def buy(self, symbol: str, quantity: float) -> Dict:
        """
        Simulate a market BUY with spread + slippage.

        Returns standardized receipt. Does NOT modify balance.
        Controller handles all state changes.
        """
        symbol = _normalize_symbol(symbol)

        if quantity <= 0:
            return self._rejection(symbol, "BUY", "Invalid quantity")

        # Get price (idempotent within cycle)
        mid_price = self.get_price(symbol)

        # Apply spread (buy at ask)
        ask_price = mid_price * (1 + self.spread_pct)

        # Apply random slippage within band
        slip = ask_price * self.slippage_pct * random.uniform(0.2, 1.0)
        fill_price = round(ask_price + slip, 8)

        cost = round(fill_price * quantity, 8)
        fee = round(cost * 0.001, 8)  # 0.1% taker fee

        # Check if balance sufficient (informational — controller double-checks)
        balance = self.state.get("balance", 0)
        if cost + fee > balance:
            logger.warning(
                f"❌ PAPER BUY rejected | {symbol} | "
                f"Cost=${cost:.4f} + Fee=${fee:.4f} > Balance=${balance:.2f}"
            )
            return self._rejection(symbol, "BUY", "Insufficient balance")

        self._order_counter += 1
        order_id = f"PAPER-B-{self._order_counter}"

        logger.info(
            f"📝🟢 PAPER BUY | {symbol} | "
            f"Qty={quantity} | Price=${fill_price:.6f} | "
            f"Cost=${cost:.4f} | Fee=${fee:.4f}"
        )

        return {
            "status": "FILLED",
            "symbol": symbol,
            "action": "BUY",
            "price": fill_price,
            "quantity": quantity,
            "cost": cost,
            "fee": fee,
            "order_id": order_id,
            "timestamp": datetime.utcnow().isoformat(),
            "mode": "PAPER",
        }

    def sell(self, symbol: str, quantity: float) -> Dict:
        """
        Simulate a market SELL with spread + slippage.

        Returns standardized receipt. Does NOT modify balance/positions.
        Controller handles all state changes via close_position().
        """
        symbol = _normalize_symbol(symbol)

        if quantity <= 0:
            return self._rejection(symbol, "SELL", "Invalid quantity")

        # Validate position exists (informational check)
        position = self.state.get_position(symbol)
        if not position:
            logger.warning(f"❌ PAPER SELL rejected | {symbol} | No open position")
            return self._rejection(symbol, "SELL", "No open position")

        pos_qty = position.get("quantity", 0)
        if quantity > pos_qty * 1.001:  # 0.1% tolerance for float rounding
            logger.warning(
                f"❌ PAPER SELL rejected | {symbol} | "
                f"Qty={quantity} > Position={pos_qty}"
            )
            return self._rejection(symbol, "SELL", "Quantity exceeds position")

        # Clamp to actual position size
        actual_qty = min(quantity, pos_qty)

        # Get price (idempotent within cycle)
        mid_price = self.get_price(symbol)

        # Apply spread (sell at bid)
        bid_price = mid_price * (1 - self.spread_pct)

        # Apply random slippage within band
        slip = bid_price * self.slippage_pct * random.uniform(0.2, 1.0)
        fill_price = round(bid_price - slip, 8)
        fill_price = max(fill_price, 1e-8)

        proceeds = round(fill_price * actual_qty, 8)
        fee = round(proceeds * 0.001, 8)  # 0.1% taker fee

        # Calculate PnL for receipt
        entry_price = position.get(
            "entry_price", position.get("avg_price", fill_price)
        )
        gross_pnl = round((fill_price - entry_price) * actual_qty, 8)

        self._order_counter += 1
        order_id = f"PAPER-S-{self._order_counter}"

        logger.info(
            f"📝🔴 PAPER SELL | {symbol} | "
            f"Qty={actual_qty} | Price=${fill_price:.6f} | "
            f"PnL=${gross_pnl:+.4f} | Fee=${fee:.4f}"
        )

        return {
            "status": "FILLED",
            "symbol": symbol,
            "action": "SELL",
            "price": fill_price,
            "quantity": actual_qty,
            "proceeds": proceeds,
            "gross_pnl": gross_pnl,
            "fee": fee,
            "order_id": order_id,
            "timestamp": datetime.utcnow().isoformat(),
            "mode": "PAPER",
        }

    # ═════════════════════════════════════════════════════
    #  ACCOUNT INFO
    # ═════════════════════════════════════════════════════

    def get_balance(self) -> float:
        """Return current balance from state."""
        return self.state.get("balance", 0.0)

    def get_open_positions(self) -> Dict:
        """Return all open positions from state."""
        return self.state.get_all_positions()

    def get_account_summary(self) -> Dict:
        """Return account overview."""
        positions = self.get_open_positions()
        balance = self.get_balance()
        exposure = 0.0

        for pos in positions.values():
            qty = pos.get("quantity", 0)
            price = pos.get("entry_price", pos.get("avg_price", 0))
            exposure += qty * price

        return {
            "balance": round(balance, 2),
            "open_positions": len(positions),
            "exposure": round(exposure, 2),
            "mode": "PAPER",
        }

    # ═════════════════════════════════════════════════════
    #  PRICE SEEDING (testing / backtesting)
    # ═════════════════════════════════════════════════════

    def seed_price(self, symbol: str, price: float) -> None:
        """Manually set price for a symbol. Clears cycle cache too."""
        symbol = _normalize_symbol(symbol)
        self.state.set(f"paper_price_{symbol}", price)
        self._price_cache[symbol] = price
        logger.info(f"🌱 Price seeded | {symbol} = ${price}")

    def reset_candles(self, symbol: str) -> None:
        """Clear candle cache for a symbol. Forces regeneration next call."""
        symbol = _normalize_symbol(symbol)
        self._candle_cache.pop(symbol, None)
        self._last_candle_time.pop(symbol, None)
        logger.info(f"🗑️ Candle cache cleared | {symbol}")

    # ═════════════════════════════════════════════════════
    #  HEALTH
    # ═════════════════════════════════════════════════════

    def ping(self) -> bool:
        """Paper exchange is always available."""
        return True

    def close(self) -> None:
        """No resources to clean up for paper exchange."""
        logger.info("📝 Paper exchange closed")

    def __repr__(self) -> str:
        balance = self.state.get("balance", 0)
        positions = len(self.state.get_all_positions())
        return (
            f"<PaperExchange | "
            f"Balance=${balance:.2f} | "
            f"Positions={positions}>"
        )