# app/exchange/binance.py

"""
Binance Exchange Client — Production Grade

Two modes controlled by TRADING_MODE env var:

1. LIVE mode:
   - Real orders via ccxt (Binance Spot API)
   - Real market data (price, candles)
   - Real balance and position tracking
   - Requires BINANCE_API_KEY and BINANCE_SECRET in env

2. SIMULATION mode (fallback):
   - If ccxt is not installed or API keys are missing
   - Uses PaperExchange-style GBM simulation
   - Identical interface so controller code doesn't change

The controller never knows which mode is active —
it always gets standardized receipts back.
"""

import os
import math
import random
import time
from datetime import datetime
from typing import Dict, List, Optional
from app.exchange.client import ExchangeClient
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Try importing ccxt for real Binance API ───────────────────────
try:
    import ccxt
    CCXT_AVAILABLE = True
except ImportError:
    CCXT_AVAILABLE = False
    logger.warning(
        "⚠️ ccxt not installed — BinanceExchange will run in SIMULATION mode. "
        "Install with: pip install ccxt"
    )

# ── Seed prices for simulation fallback ───────────────────────────
SEED_PRICES: Dict[str, float] = {
    "BTC/USDT": 65000.0,
    "ETH/USDT": 3200.0,
    "SOL/USDT": 145.0,
    "BNB/USDT": 580.0,
    "XRP/USDT": 0.52,
    "ADA/USDT": 0.45,
    "DOGE/USDT": 0.12,
    "AVAX/USDT": 35.0,
}
DEFAULT_SEED = 100.0


def _normalize_symbol(symbol: str) -> str:
    """Normalize to BASE/QUOTE format."""
    s = symbol.upper().strip()
    if "/" in s:
        return s
    for quote in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH"):
        if s.endswith(quote):
            base = s[: -len(quote)]
            if base:
                return f"{base}/{quote}"
    return s


class BinanceExchange(ExchangeClient):
    """
    Binance Exchange — Auto-detecting Live / Simulation

    On init:
    - If ccxt available AND API keys present → LIVE mode
    - Otherwise → SIMULATION mode (GBM paper trading)

    Both modes return identical receipt formats so controller
    code works without any changes.
    """

    # ── Simulation parameters ─────────────────────────────────────
    SIM_SPREAD_PCT = 0.0005       # 0.05% spread
    SIM_SLIPPAGE_PCT = 0.0007     # 0.07% max slippage
    SIM_GBM_MU = 0.00005
    SIM_GBM_SIGMA = 0.0012
    SIM_CANDLE_SIGMA = 0.0015
    CANDLE_CACHE_MAX = 300
    CANDLE_INTERVAL_SEC = 300     # 5 min candles

    def __init__(
        self,
        state_manager,
        api_key: str = None,
        secret: str = None,
        sandbox: bool = False,
    ):
        self.state = state_manager
        self._live = False
        self._exchange = None

        # ── Per-cycle price cache ─────────────────────────────────
        self._price_cache: Dict[str, float] = {}

        # ── Simulation candle state ───────────────────────────────
        self._candle_cache: Dict[str, List[Dict]] = {}
        self._last_candle_time: Dict[str, float] = {}
        self._order_counter: int = 0

        # ── Attempt live connection ───────────────────────────────
        _key = api_key or os.getenv("BINANCE_API_KEY", "")
        _secret = secret or os.getenv("BINANCE_SECRET", "")

        if CCXT_AVAILABLE and _key and _secret:
            try:
                self._exchange = ccxt.binance({
                    "apiKey": _key,
                    "secret": _secret,
                    "sandbox": sandbox,
                    "enableRateLimit": True,
                    "options": {
                        "defaultType": "spot",
                        "adjustForTimeDifference": True,
                    },
                    "timeout": 30000,  # 30s timeout
                })

                # Test connectivity
                self._exchange.load_markets()
                balance_info = self._exchange.fetch_balance()
                usdt_free = float(
                    balance_info.get("USDT", {}).get("free", 0)
                )

                self._live = True
                logger.info(
                    f"✅ Binance LIVE connected | "
                    f"Sandbox={sandbox} | "
                    f"USDT Balance=${usdt_free:.2f}"
                )

                # Sync state balance with actual exchange balance
                self.state.set("balance", usdt_free)

            except ccxt.AuthenticationError as e:
                logger.error(f"❌ Binance auth failed: {e}")
                logger.info("↩️ Falling back to SIMULATION mode")
                self._live = False

            except ccxt.NetworkError as e:
                logger.error(f"❌ Binance network error: {e}")
                logger.info("↩️ Falling back to SIMULATION mode")
                self._live = False

            except Exception as e:
                logger.error(f"❌ Binance init failed: {e}")
                logger.info("↩️ Falling back to SIMULATION mode")
                self._live = False
        else:
            missing = []
            if not CCXT_AVAILABLE:
                missing.append("ccxt library")
            if not _key:
                missing.append("BINANCE_API_KEY")
            if not _secret:
                missing.append("BINANCE_SECRET")

            logger.info(
                f"📝 BinanceExchange in SIMULATION mode "
                f"(missing: {', '.join(missing)})"
            )

    @property
    def mode_label(self) -> str:
        return "LIVE" if self._live else "SIMULATION"

    # ═════════════════════════════════════════════════════
    #  CYCLE MANAGEMENT
    # ═════════════════════════════════════════════════════

    def begin_cycle(self) -> None:
        """Reset per-cycle price cache."""
        self._price_cache.clear()
        logger.debug(f"🔄 Binance cycle reset ({self.mode_label})")

    def end_cycle(self) -> None:
        pass

    # ═════════════════════════════════════════════════════
    #  MARKET DATA
    # ═════════════════════════════════════════════════════

    def get_price(self, symbol: str) -> float:
        """
        Get current price — idempotent within a cycle.
        LIVE: fetches from Binance API (cached per cycle).
        SIM: GBM random walk (cached per cycle).
        """
        symbol = _normalize_symbol(symbol)

        if symbol in self._price_cache:
            return self._price_cache[symbol]

        if self._live:
            price = self._live_get_price(symbol)
        else:
            price = self._sim_get_price(symbol)

        self._price_cache[symbol] = price
        return price

    def get_recent_candles(self, symbol: str, limit: int = 150) -> List[Dict]:
        """
        Get OHLCV candle history.
        LIVE: fetches from Binance API.
        SIM: generates time-based GBM candles.
        """
        symbol = _normalize_symbol(symbol)

        if self._live:
            return self._live_get_candles(symbol, limit)
        else:
            return self._sim_get_candles(symbol, limit)

    # ═════════════════════════════════════════════════════
    #  ORDER EXECUTION
    # ═════════════════════════════════════════════════════

    def buy(self, symbol: str, quantity: float) -> Dict:
        """
        Market BUY order.
        LIVE: real order via ccxt.
        SIM: simulated with spread/slippage.
        Does NOT modify balance — controller handles state.
        """
        symbol = _normalize_symbol(symbol)

        if quantity <= 0:
            return self._rejection(symbol, "BUY", "Invalid quantity")

        if self._live:
            return self._live_buy(symbol, quantity)
        else:
            return self._sim_buy(symbol, quantity)

    def sell(self, symbol: str, quantity: float) -> Dict:
        """
        Market SELL order.
        LIVE: real order via ccxt.
        SIM: simulated with spread/slippage.
        Does NOT modify balance — controller handles state.
        """
        symbol = _normalize_symbol(symbol)

        if quantity <= 0:
            return self._rejection(symbol, "SELL", "Invalid quantity")

        if self._live:
            return self._live_sell(symbol, quantity)
        else:
            return self._sim_sell(symbol, quantity)

    # ═════════════════════════════════════════════════════
    #  ACCOUNT
    # ═════════════════════════════════════════════════════

    def get_balance(self) -> float:
        """Get USDT balance. Live syncs from exchange."""
        if self._live:
            try:
                balance_info = self._exchange.fetch_balance()
                usdt = float(balance_info.get("USDT", {}).get("free", 0))
                self.state.set("balance", usdt)
                return usdt
            except Exception as e:
                logger.error(f"❌ Balance fetch failed: {e}")
                return self.state.get("balance", 0.0)
        return self.state.get("balance", 0.0)

    def get_open_positions(self) -> Dict:
        return self.state.get_all_positions()

    def get_account_summary(self) -> Dict:
        positions = self.get_open_positions()
        balance = self.get_balance()
        exposure = sum(
            p.get("quantity", 0) * p.get("entry_price", p.get("avg_price", 0))
            for p in positions.values()
        )
        return {
            "balance": round(balance, 2),
            "open_positions": len(positions),
            "exposure": round(exposure, 2),
            "mode": self.mode_label,
        }

    # ═════════════════════════════════════════════════════
    #  LIVE IMPLEMENTATIONS
    # ═════════════════════════════════════════════════════

    def _live_get_price(self, symbol: str) -> float:
        """Fetch real-time price from Binance."""
        try:
            ticker = self._exchange.fetch_ticker(symbol)
            price = float(ticker.get("last", 0))
            self.state.set(f"paper_price_{symbol}", price)
            return price
        except ccxt.NetworkError as e:
            logger.error(f"❌ Network error fetching price {symbol}: {e}")
            # Fallback to last known price
            return self.state.get(f"paper_price_{symbol}", 0.0)
        except Exception as e:
            logger.error(f"❌ Price fetch failed {symbol}: {e}")
            return self.state.get(f"paper_price_{symbol}", 0.0)

    def _live_get_candles(self, symbol: str, limit: int) -> List[Dict]:
        """Fetch real OHLCV from Binance."""
        try:
            ohlcv = self._exchange.fetch_ohlcv(
                symbol, timeframe="5m", limit=limit
            )
            candles = []
            for bar in ohlcv:
                candles.append({
                    "open": float(bar[1]),
                    "high": float(bar[2]),
                    "low": float(bar[3]),
                    "close": float(bar[4]),
                    "volume": float(bar[5]),
                    "timestamp": datetime.utcfromtimestamp(
                        bar[0] / 1000
                    ).isoformat(),
                })

            if candles:
                self.state.set(
                    f"paper_price_{symbol}", candles[-1]["close"]
                )

            logger.debug(
                f"🕯️ Fetched {len(candles)} live candles for {symbol}"
            )
            return candles

        except ccxt.NetworkError as e:
            logger.error(f"❌ Candle fetch network error {symbol}: {e}")
            return self._sim_get_candles(symbol, limit)

        except Exception as e:
            logger.error(f"❌ Candle fetch failed {symbol}: {e}")
            return self._sim_get_candles(symbol, limit)

    def _live_buy(self, symbol: str, quantity: float) -> Dict:
        """Execute real market BUY on Binance."""
        try:
            # Round quantity to exchange precision
            market_info = self._exchange.market(symbol)
            precision = market_info.get("precision", {}).get("amount", 8)
            qty = round(quantity, precision)

            if qty <= 0:
                return self._rejection(symbol, "BUY", "Qty rounds to 0")

            order = self._exchange.create_market_buy_order(symbol, qty)

            fill_price = float(order.get("average", order.get("price", 0)))
            filled_qty = float(order.get("filled", qty))
            cost = float(order.get("cost", fill_price * filled_qty))

            # Extract fee
            fee_info = order.get("fee", {})
            fee = float(fee_info.get("cost", 0)) if fee_info else 0.0

            status = order.get("status", "closed").upper()
            if status in ("CLOSED", "FILLED"):
                status = "FILLED"
            elif filled_qty > 0:
                status = "PARTIAL"
            else:
                status = "REJECTED"

            order_id = str(order.get("id", ""))

            logger.info(
                f"💰🟢 LIVE BUY | {symbol} | "
                f"Qty={filled_qty} | Price=${fill_price:.6f} | "
                f"Cost=${cost:.4f} | Fee=${fee:.4f} | "
                f"OrderID={order_id}"
            )

            return {
                "status": status,
                "symbol": symbol,
                "action": "BUY",
                "price": fill_price,
                "quantity": filled_qty,
                "cost": cost,
                "fee": fee,
                "order_id": order_id,
                "timestamp": datetime.utcnow().isoformat(),
                "mode": "LIVE",
            }

        except ccxt.InsufficientFunds as e:
            logger.error(f"❌ Insufficient funds for BUY {symbol}: {e}")
            return self._rejection(symbol, "BUY", f"Insufficient funds: {e}")

        except ccxt.InvalidOrder as e:
            logger.error(f"❌ Invalid order BUY {symbol}: {e}")
            return self._rejection(symbol, "BUY", f"Invalid order: {e}")

        except ccxt.NetworkError as e:
            logger.error(f"❌ Network error BUY {symbol}: {e}")
            return self._rejection(symbol, "BUY", f"Network error: {e}")

        except Exception as e:
            logger.exception(f"❌ BUY failed {symbol}: {e}")
            return self._rejection(symbol, "BUY", str(e))

    def _live_sell(self, symbol: str, quantity: float) -> Dict:
        """Execute real market SELL on Binance."""
        try:
            market_info = self._exchange.market(symbol)
            precision = market_info.get("precision", {}).get("amount", 8)
            qty = round(quantity, precision)

            if qty <= 0:
                return self._rejection(symbol, "SELL", "Qty rounds to 0")

            order = self._exchange.create_market_sell_order(symbol, qty)

            fill_price = float(order.get("average", order.get("price", 0)))
            filled_qty = float(order.get("filled", qty))
            proceeds = float(order.get("cost", fill_price * filled_qty))

            fee_info = order.get("fee", {})
            fee = float(fee_info.get("cost", 0)) if fee_info else 0.0

            status = order.get("status", "closed").upper()
            if status in ("CLOSED", "FILLED"):
                status = "FILLED"
            elif filled_qty > 0:
                status = "PARTIAL"
            else:
                status = "REJECTED"

            # Calculate PnL from state position
            position = self.state.get_position(symbol)
            entry_price = 0.0
            gross_pnl = 0.0
            if position:
                entry_price = position.get(
                    "entry_price", position.get("avg_price", fill_price)
                )
                gross_pnl = round(
                    (fill_price - entry_price) * filled_qty, 8
                )

            order_id = str(order.get("id", ""))

            logger.info(
                f"💰🔴 LIVE SELL | {symbol} | "
                f"Qty={filled_qty} | Price=${fill_price:.6f} | "
                f"PnL=${gross_pnl:+.4f} | Fee=${fee:.4f} | "
                f"OrderID={order_id}"
            )

            return {
                "status": status,
                "symbol": symbol,
                "action": "SELL",
                "price": fill_price,
                "quantity": filled_qty,
                "proceeds": proceeds,
                "gross_pnl": gross_pnl,
                "fee": fee,
                "order_id": order_id,
                "timestamp": datetime.utcnow().isoformat(),
                "mode": "LIVE",
            }

        except ccxt.InsufficientFunds as e:
            logger.error(f"❌ Insufficient balance for SELL {symbol}: {e}")
            return self._rejection(symbol, "SELL", f"Insufficient funds: {e}")

        except ccxt.InvalidOrder as e:
            logger.error(f"❌ Invalid order SELL {symbol}: {e}")
            return self._rejection(symbol, "SELL", f"Invalid order: {e}")

        except ccxt.NetworkError as e:
            logger.error(f"❌ Network error SELL {symbol}: {e}")
            return self._rejection(symbol, "SELL", f"Network error: {e}")

        except Exception as e:
            logger.exception(f"❌ SELL failed {symbol}: {e}")
            return self._rejection(symbol, "SELL", str(e))

    # ═════════════════════════════════════════════════════
    #  SIMULATION IMPLEMENTATIONS
    # ═════════════════════════════════════════════════════

    def _sim_get_price(self, symbol: str) -> float:
        """GBM price walk for simulation mode."""
        base = (
            self.state.get(f"paper_price_{symbol}")
            or SEED_PRICES.get(symbol, DEFAULT_SEED)
        )
        z = random.gauss(0, 1)
        price = base * math.exp(
            (self.SIM_GBM_MU - 0.5 * self.SIM_GBM_SIGMA ** 2)
            + self.SIM_GBM_SIGMA * z
        )
        price = round(max(price, 1e-8), 8)
        self.state.set(f"paper_price_{symbol}", price)
        return price

    def _sim_get_candles(self, symbol: str, limit: int) -> List[Dict]:
        """Time-based candle generation for simulation mode."""
        cache = self._candle_cache.get(symbol, [])
        now = time.time()

        if not cache:
            seed = (
                self.state.get(f"paper_price_{symbol}")
                or SEED_PRICES.get(symbol, DEFAULT_SEED)
            )
            min_candles = max(limit, 60)
            cache = self._sim_generate_series(seed, min_candles)
            self._last_candle_time[symbol] = now

        last_time = self._last_candle_time.get(symbol, 0)
        elapsed = now - last_time

        if elapsed >= self.CANDLE_INTERVAL_SEC:
            num_new = min(int(elapsed // self.CANDLE_INTERVAL_SEC), 10)
            last_close = cache[-1]["close"]
            for _ in range(num_new):
                candle = self._sim_make_candle(last_close)
                cache.append(candle)
                last_close = candle["close"]
            self._last_candle_time[symbol] = now

        if len(cache) > self.CANDLE_CACHE_MAX:
            cache = cache[-self.CANDLE_CACHE_MAX:]

        self._candle_cache[symbol] = cache

        if cache:
            self.state.set(f"paper_price_{symbol}", cache[-1]["close"])

        return cache[-limit:]

    def _sim_generate_series(
        self, seed: float, count: int
    ) -> List[Dict]:
        """Generate continuous candle series from seed."""
        candles = []
        price = seed
        for _ in range(count):
            candle = self._sim_make_candle(price)
            candles.append(candle)
            price = candle["close"]
        return candles

    def _sim_make_candle(self, prev_close: float) -> Dict:
        """Generate one realistic OHLCV candle via GBM."""
        sigma = self.SIM_CANDLE_SIGMA
        mu = self.SIM_GBM_MU
        z = random.gauss(0, 1)

        open_p = prev_close * (1 + random.gauss(0, 0.0003))
        close_p = open_p * math.exp(
            (mu - 0.5 * sigma ** 2) + sigma * z
        )
        open_p = max(open_p, 1e-8)
        close_p = max(close_p, 1e-8)

        candle_range = abs(close_p - open_p)
        wick = max(candle_range * 0.5, prev_close * 0.0002)
        high_p = max(open_p, close_p) + random.uniform(0, wick)
        low_p = min(open_p, close_p) - random.uniform(0, wick)
        low_p = max(low_p, 1e-8)

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

    def _sim_buy(self, symbol: str, quantity: float) -> Dict:
        """Simulated BUY with spread + slippage."""
        mid_price = self.get_price(symbol)
        ask = mid_price * (1 + self.SIM_SPREAD_PCT)
        slip = ask * self.SIM_SLIPPAGE_PCT * random.uniform(0.2, 1.0)
        fill_price = round(ask + slip, 8)
        cost = round(fill_price * quantity, 8)
        fee = round(cost * 0.001, 8)

        balance = self.state.get("balance", 0)
        if cost + fee > balance:
            logger.warning(
                f"❌ SIM BUY rejected | {symbol} | "
                f"Cost=${cost:.4f}+Fee=${fee:.4f} > Balance=${balance:.2f}"
            )
            return self._rejection(symbol, "BUY", "Insufficient balance")

        self._order_counter += 1
        order_id = f"SIM-B-{self._order_counter}"

        logger.info(
            f"📝🟢 SIM BUY | {symbol} | "
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
            "mode": "SIMULATION",
        }

    def _sim_sell(self, symbol: str, quantity: float) -> Dict:
        """Simulated SELL with spread + slippage."""
        position = self.state.get_position(symbol)
        if not position:
            return self._rejection(symbol, "SELL", "No open position")

        pos_qty = position.get("quantity", 0)
        if quantity > pos_qty * 1.001:
            return self._rejection(
                symbol, "SELL", f"Qty {quantity} > position {pos_qty}"
            )

        actual_qty = min(quantity, pos_qty)
        mid_price = self.get_price(symbol)
        bid = mid_price * (1 - self.SIM_SPREAD_PCT)
        slip = bid * self.SIM_SLIPPAGE_PCT * random.uniform(0.2, 1.0)
        fill_price = round(max(bid - slip, 1e-8), 8)
        proceeds = round(fill_price * actual_qty, 8)
        fee = round(proceeds * 0.001, 8)

        entry = position.get(
            "entry_price", position.get("avg_price", fill_price)
        )
        gross_pnl = round((fill_price - entry) * actual_qty, 8)

        self._order_counter += 1
        order_id = f"SIM-S-{self._order_counter}"

        logger.info(
            f"📝🔴 SIM SELL | {symbol} | "
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
            "mode": "SIMULATION",
        }

    # ═════════════════════════════════════════════════════
    #  TESTING / SEEDING
    # ═════════════════════════════════════════════════════

    def seed_price(self, symbol: str, price: float) -> None:
        symbol = _normalize_symbol(symbol)
        self.state.set(f"paper_price_{symbol}", price)
        self._price_cache[symbol] = price
        logger.info(f"🌱 Price seeded | {symbol} = ${price}")

    def reset_candles(self, symbol: str) -> None:
        symbol = _normalize_symbol(symbol)
        self._candle_cache.pop(symbol, None)
        self._last_candle_time.pop(symbol, None)
        logger.info(f"🗑️ Candle cache cleared | {symbol}")

    # ═════════════════════════════════════════════════════
    #  HEALTH / LIFECYCLE
    # ═════════════════════════════════════════════════════

    def ping(self) -> bool:
        if self._live:
            try:
                self._exchange.fetch_time()
                return True
            except Exception:
                return False
        return True

    def close(self) -> None:
        if self._exchange:
            try:
                self._exchange.close()
            except Exception:
                pass
        logger.info(f"🔌 BinanceExchange closed ({self.mode_label})")

    def __repr__(self) -> str:
        balance = self.state.get("balance", 0)
        positions = len(self.state.get_all_positions())
        return (
            f"<BinanceExchange({self.mode_label}) | "
            f"Balance=${balance:.2f} | "
            f"Positions={positions}>"
        )