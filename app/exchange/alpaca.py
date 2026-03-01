# app/exchange/alpaca.py

import os
import time
import math
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from app.exchange.client import ExchangeClient
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Optional imports (installed at runtime) ───────────────────────
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.error("❌ requests not installed — run: pip install requests")


# ── Alpaca endpoints ──────────────────────────────────────────────
PAPER_BASE_URL  = "https://paper-api.alpaca.markets"
DATA_BASE_URL   = "https://data.alpaca.markets"

# ── Symbol normalisation (BTC/USDT → BTC/USD for Alpaca) ─────────
def _normalise_symbol(symbol: str) -> str:
    """
    Alpaca uses BTC/USD not BTC/USDT.
    Strips the T from USDT pairs.
    """
    return symbol.replace("/USDT", "/USD").replace("USDT", "USD")


class AlpacaExchange(ExchangeClient):
    """
    Alpaca Paper Trading Exchange Client

    Connects to Alpaca's real paper trading API so orders show up
    in your Alpaca dashboard while risk stays simulated.

    Requirements:
        pip install requests

    .env keys needed:
        ALPACA_API_KEY=...
        ALPACA_SECRET_KEY=...
        ALPACA_BASE_URL=https://paper-api.alpaca.markets  (optional override)

    Alpaca crypto supports:
        BTC/USD, ETH/USD, SOL/USD, BNB/USD, XRP/USD etc.
    """

    CANDLE_CACHE    = 300
    REQUEST_TIMEOUT = 10    # seconds

    def __init__(self, state_manager):
        self.state      = state_manager
        self.api_key    = os.getenv("ALPACA_API_KEY", "")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        self.base_url   = os.getenv("ALPACA_BASE_URL", PAPER_BASE_URL).rstrip("/")

        self._candle_cache: Dict[str, List[Dict]] = {}

        if not self.api_key or not self.secret_key:
            raise RuntimeError(
                "❌ ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in config/.env"
            )

        if not REQUESTS_AVAILABLE:
            raise RuntimeError("❌ requests library not installed. Run: pip install requests")

        logger.info(f"✅ AlpacaExchange initialised | URL={self.base_url}")

    # =====================================================
    # HTTP HELPERS
    # =====================================================
    @property
    def _headers(self) -> Dict:
        return {
            "APCA-API-KEY-ID":     self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type":        "application/json",
        }

    def _get(self, url: str, params: Dict = None) -> Optional[Dict]:
        try:
            resp = requests.get(
                url,
                headers=self._headers,
                params=params or {},
                timeout=self.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Alpaca HTTP error: {e} | {resp.text}")
        except Exception as e:
            logger.error(f"❌ Alpaca GET failed: {e}")
        return None

    def _post(self, url: str, payload: Dict) -> Optional[Dict]:
        try:
            resp = requests.post(
                url,
                headers=self._headers,
                json=payload,
                timeout=self.REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            logger.error(f"❌ Alpaca HTTP error: {e} | {resp.text}")
        except Exception as e:
            logger.error(f"❌ Alpaca POST failed: {e}")
        return None

    def _delete(self, url: str) -> bool:
        try:
            resp = requests.delete(
                url,
                headers=self._headers,
                timeout=self.REQUEST_TIMEOUT,
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"❌ Alpaca DELETE failed: {e}")
        return False

    # =====================================================
    # MARKET DATA
    # =====================================================
    def get_price(self, symbol: str) -> float:
        """
        Fetches latest trade price from Alpaca crypto data API.
        Falls back to last known price if API fails.
        """
        alpaca_sym = _normalise_symbol(symbol)
        url = f"{DATA_BASE_URL}/v1beta3/crypto/us/latest/trades"

        data = self._get(url, params={"symbols": alpaca_sym})

        if data and "trades" in data:
            trade = data["trades"].get(alpaca_sym)
            if trade and "p" in trade:
                price = float(trade["p"])
                self.state.set(f"paper_price_{symbol}", price)
                return price

        # Fallback to cached price
        cached = self.state.get(f"paper_price_{symbol}")
        if cached:
            logger.warning(f"⚠️ Using cached price for {symbol}: ${cached}")
            return float(cached)

        logger.error(f"❌ Could not get price for {symbol}")
        return 0.0

    def get_recent_candles(self, symbol: str, limit: int = 150) -> List[Dict]:
        """
        Fetches real OHLCV bars from Alpaca crypto data API.
        Falls back to simulated candles if API fails.
        Timeframe: 5Min bars to match 300s interval.
        """
        alpaca_sym = _normalise_symbol(symbol)
        url        = f"{DATA_BASE_URL}/v1beta3/crypto/us/bars"

        # Fetch enough bars for the limit
        start = (datetime.utcnow() - timedelta(hours=max(2, limit // 12))).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        data = self._get(url, params={
            "symbols":   alpaca_sym,
            "timeframe": "5Min",
            "start":     start,
            "limit":     limit,
        })

        candles = []
        if data and "bars" in data:
            bars = data["bars"].get(alpaca_sym, [])
            for bar in bars:
                candles.append({
                    "open":      float(bar.get("o", 0)),
                    "high":      float(bar.get("h", 0)),
                    "low":       float(bar.get("l", 0)),
                    "close":     float(bar.get("c", 0)),
                    "volume":    float(bar.get("v", 0)),
                    "timestamp": bar.get("t", ""),
                })

        if len(candles) >= 60:
            self._candle_cache[symbol] = candles
            logger.debug(f"📊 {symbol}: {len(candles)} real candles fetched from Alpaca")
            return candles[-limit:]

        # ── Fallback: build simulated candles from last known price ──
        logger.warning(
            f"⚠️ Alpaca returned only {len(candles)} bars for {symbol} — "
            f"padding with simulated candles."
        )
        return self._fallback_candles(symbol, limit, seed_candles=candles)

    def _fallback_candles(
        self, symbol: str, limit: int, seed_candles: List[Dict]
    ) -> List[Dict]:
        """Generates simulated candles when Alpaca data is insufficient."""
        cache = self._candle_cache.get(symbol, []) + seed_candles
        seed  = cache[-1]["close"] if cache else (
            self.state.get(f"paper_price_{symbol}") or 100.0
        )

        needed = max(0, limit - len(cache))
        for _ in range(needed):
            mu    = 0.00005
            sigma = 0.0015
            z     = random.gauss(0, 1)
            open_p  = seed * (1 + random.gauss(0, 0.0003))
            close_p = open_p * math.exp((mu - 0.5 * sigma ** 2) + sigma * z)
            rng     = abs(close_p - open_p)
            cache.append({
                "open":   round(open_p,  6),
                "high":   round(max(open_p, close_p) + random.uniform(0, rng * 0.5), 6),
                "low":    round(min(open_p, close_p) - random.uniform(0, rng * 0.5), 6),
                "close":  round(close_p, 6),
                "volume": round(random.uniform(100, 2000), 2),
            })
            seed = close_p

        if len(cache) > self.CANDLE_CACHE:
            cache = cache[-self.CANDLE_CACHE:]
        self._candle_cache[symbol] = cache
        return cache[-limit:]

    # =====================================================
    # ORDER EXECUTION
    # =====================================================
    def _place_order(self, symbol: str, side: str, quantity: float) -> Optional[Dict]:
        """
        Places a market order and polls until filled or failed.
        Alpaca market orders return 'pending_new' immediately —
        we must poll until status = 'filled'.
        """
        alpaca_sym = _normalise_symbol(symbol)
        url        = f"{self.base_url}/v2/orders"

        payload = {
            "symbol":        alpaca_sym,
            "qty":           str(round(quantity, 6)),
            "side":          side,
            "type":          "market",
            "time_in_force": "gtc",
        }

        logger.info(f"📤 Alpaca {side.upper()} order | {alpaca_sym} qty={quantity}")
        result = self._post(url, payload)

        if not result:
            logger.error(f"❌ Alpaca {side.upper()} — no response from API")
            return None

        order_id = result.get("id")
        status   = result.get("status", "")

        # ── Immediate rejection ───────────────────────────────────
        if status in ("rejected", "canceled", "expired"):
            logger.error(
                f"❌ Alpaca order {status} | {alpaca_sym} | "
                f"Reason: {result.get('failed_at') or result.get('reason', 'unknown')}"
            )
            return None

        # ── Poll until filled (max 10 seconds) ───────────────────
        if status != "filled":
            logger.info(f"⏳ Order {order_id} status={status} — polling for fill...")
            result = self._poll_order_fill(order_id, timeout=10)
            if not result:
                logger.error(f"❌ Order {order_id} did not fill within timeout")
                # Cancel the dangling order
                self._delete(f"{self.base_url}/v2/orders/{order_id}")
                return None

        fill_price = result.get("filled_avg_price")
        filled_qty = result.get("filled_qty")

        if not fill_price or not filled_qty:
            # Alpaca sometimes needs one more fetch after fill confirmation
            final = self._get(f"{self.base_url}/v2/orders/{order_id}")
            if final:
                fill_price = final.get("filled_avg_price") or fill_price
                filled_qty = final.get("filled_qty") or filled_qty

        return {
            "order_id":   order_id,
            "price":      float(fill_price or 0),
            "quantity":   float(filled_qty or quantity),
            "status":     "FILLED",
            "timestamp":  datetime.utcnow().isoformat(),
            "mode":       "ALPACA_PAPER",
        }

    def _poll_order_fill(
        self, order_id: str, timeout: int = 10, interval: float = 0.5
    ) -> Optional[Dict]:
        """
        Polls Alpaca order status every `interval` seconds
        until filled or `timeout` seconds elapsed.
        """
        url      = f"{self.base_url}/v2/orders/{order_id}"
        deadline = time.time() + timeout

        while time.time() < deadline:
            time.sleep(interval)
            data = self._get(url)
            if not data:
                continue
            status = data.get("status", "")
            logger.debug(f"⏳ Order {order_id} status={status}")
            if status == "filled":
                return data
            if status in ("canceled", "rejected", "expired"):
                logger.error(f"❌ Order {order_id} terminal status: {status}")
                return None

        logger.warning(f"⚠️ Order {order_id} still not filled after {timeout}s")
        return None

    def buy(self, symbol: str, quantity: float) -> Dict:
        """Places a real market BUY on Alpaca paper trading and waits for fill."""
        result = self._place_order(symbol, "buy", quantity)

        if not result:
            return {"status": "REJECTED", "symbol": symbol, "action": "BUY"}

        cost = round(result["price"] * result["quantity"], 8)
        logger.info(
            f"📝🟢 ALPACA BUY FILLED | {symbol} | "
            f"Qty={result['quantity']} | Price=${result['price']:.4f} | "
            f"Cost=${cost:.2f} | OrderID={result['order_id']}"
        )
        result["action"] = "BUY"
        result["cost"]   = cost
        result["symbol"] = symbol
        return result

    def sell(self, symbol: str, quantity: float) -> Dict:
        """Places a real market SELL on Alpaca paper trading and waits for fill."""
        position = self.state.get_position(symbol)
        if not position:
            logger.warning(f"❌ SELL rejected — no open position for {symbol}")
            return {"status": "REJECTED", "symbol": symbol, "action": "SELL"}

        if quantity > position.get("quantity", 0):
            logger.warning(
                f"❌ SELL rejected — qty {quantity} > "
                f"position {position['quantity']}"
            )
            return {"status": "REJECTED", "symbol": symbol, "action": "SELL"}

        result = self._place_order(symbol, "sell", quantity)

        if not result:
            return {"status": "REJECTED", "symbol": symbol, "action": "SELL"}

        entry     = position.get("entry_price", position.get("avg_price", result["price"]))
        gross_pnl = round((result["price"] - entry) * result["quantity"], 8)
        proceeds  = round(result["price"] * result["quantity"], 8)

        logger.info(
            f"📝🔴 ALPACA SELL FILLED | {symbol} | "
            f"Qty={result['quantity']} | Price=${result['price']:.4f} | "
            f"PnL=${gross_pnl:+.4f} | OrderID={result['order_id']}"
        )
        result["action"]    = "SELL"
        result["proceeds"]  = proceeds
        result["gross_pnl"] = gross_pnl
        result["symbol"]    = symbol
        return result

    # =====================================================
    # ACCOUNT
    # =====================================================
    def get_balance(self) -> float:
        """Fetches real buying power from Alpaca paper account."""
        data = self._get(f"{self.base_url}/v2/account")
        if data and "buying_power" in data:
            balance = round(float(data["buying_power"]), 2)
            self.state.set("balance", balance)
            return balance
        return self.state.get("balance", 0.0)

    def get_open_positions(self) -> Dict:
        """Fetches real open positions from Alpaca."""
        data = self._get(f"{self.base_url}/v2/positions")
        if not data:
            return self.state.get_all_positions()

        positions = {}
        for pos in data:
            sym = pos.get("symbol", "")
            positions[sym] = {
                "symbol":      sym,
                "quantity":    float(pos.get("qty", 0)),
                "avg_price":   float(pos.get("avg_entry_price", 0)),
                "entry_price": float(pos.get("avg_entry_price", 0)),
                "market_val":  float(pos.get("market_value", 0)),
                "unrealized_pnl": float(pos.get("unrealized_pl", 0)),
            }
        return positions

    def get_account_summary(self) -> Dict:
        data = self._get(f"{self.base_url}/v2/account")
        if data:
            return {
                "balance":        round(float(data.get("buying_power", 0)), 2),
                "portfolio_value": round(float(data.get("portfolio_value", 0)), 2),
                "open_positions": len(self._get(f"{self.base_url}/v2/positions") or []),
                "mode":           "ALPACA_PAPER",
            }
        return {"balance": self.get_balance(), "mode": "ALPACA_PAPER"}

    def ping(self) -> bool:
        """Checks if Alpaca API is reachable."""
        data = self._get(f"{self.base_url}/v2/account")
        return data is not None

    # =====================================================
    # UTILITIES
    # =====================================================
    def cancel_all_orders(self) -> bool:
        """Cancel all open Alpaca orders. Used by panic_stop."""
        ok = self._delete(f"{self.base_url}/v2/orders")
        if ok:
            logger.info("✅ All Alpaca orders cancelled.")
        return ok

    def get_order_status(self, order_id: str) -> Optional[Dict]:
        return self._get(f"{self.base_url}/v2/orders/{order_id}")

    def seed_price(self, symbol: str, price: float):
        self.state.set(f"paper_price_{symbol}", price)

    def reset_candles(self, symbol: str):
        self._candle_cache.pop(symbol, None)