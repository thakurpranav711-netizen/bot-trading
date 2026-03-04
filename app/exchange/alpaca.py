# app/exchange/alpaca.py

import os
import time
import math
import random
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from app.exchange.client import ExchangeClient, OrderStatus
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Optional imports ──────────────────────────────────────────────
try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.error("❌ requests not installed — run: pip install requests")


# ── Alpaca endpoints ──────────────────────────────────────────────
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
LIVE_BASE_URL = "https://api.alpaca.markets"
DATA_BASE_URL = "https://data.alpaca.markets"


class AlpacaExchange(ExchangeClient):
    """
    Alpaca Paper/Live Trading Exchange Client

    Connects to Alpaca's trading API for crypto trading.
    Orders appear in your Alpaca dashboard.

    Features:
    - Real market data from Alpaca crypto API
    - Market order execution with fill polling
    - Position and account management
    - Automatic retry with exponential backoff
    - Rate limit handling
    - Price caching per cycle

    Requirements:
        pip install requests

    .env keys needed:
        ALPACA_API_KEY=...
        ALPACA_SECRET_KEY=...
        ALPACA_BASE_URL=https://paper-api.alpaca.markets

    Supported crypto pairs:
        BTC/USD, ETH/USD, SOL/USD, DOGE/USD, SHIB/USD, etc.
    """

    EXCHANGE_NAME = "ALPACA"
    MODE = "PAPER"  # Will be set based on URL

    # Cache settings
    CANDLE_CACHE_SIZE = 500
    PRICE_CACHE_TTL = 5  # seconds

    # API settings
    REQUEST_TIMEOUT = 15
    MAX_RETRIES = 3
    RETRY_BACKOFF = 0.5

    # Rate limiting
    RATE_LIMIT_REQUESTS = 200
    RATE_LIMIT_WINDOW = 60  # seconds

    def __init__(self, state_manager=None):
        self.state = state_manager
        
        # Load credentials
        self.api_key = os.getenv("ALPACA_API_KEY", "")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY", "")
        self.base_url = os.getenv("ALPACA_BASE_URL", PAPER_BASE_URL).rstrip("/")

        # Determine mode
        if "paper" in self.base_url.lower():
            self.MODE = "PAPER"
        else:
            self.MODE = "LIVE"

        # Validate credentials
        if not self.api_key or not self.secret_key:
            raise RuntimeError(
                "❌ ALPACA_API_KEY and ALPACA_SECRET_KEY must be set in config/.env"
            )

        if not REQUESTS_AVAILABLE:
            raise RuntimeError("❌ requests library not installed")

        # Initialize caches
        self._price_cache: Dict[str, Tuple[float, float]] = {}  # symbol -> (price, timestamp)
        self._candle_cache: Dict[str, List[Dict]] = {}
        self._candle_cache_time: Dict[str, float] = {}

        # Rate limiting tracking
        self._request_times: List[float] = []

        # Setup session with retry
        self._session = self._create_session()

        # Verify connection
        if not self.ping():
            logger.warning("⚠️ Alpaca API not reachable on init")

        logger.info(
            f"✅ AlpacaExchange initialized | "
            f"Mode={self.MODE} | URL={self.base_url}"
        )

    def _create_session(self) -> 'requests.Session':
        """Create requests session with retry strategy."""
        session = requests.Session()
        
        retry_strategy = Retry(
            total=self.MAX_RETRIES,
            backoff_factor=self.RETRY_BACKOFF,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "POST", "DELETE"],
        )
        
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        
        return session

    # ═══════════════════════════════════════════════════════════════
    # SYMBOL NORMALIZATION
    # ═══════════════════════════════════════════════════════════════

    def normalize_symbol(self, symbol: str) -> str:
        """
        Convert standard symbol to Alpaca format.
        BTC/USDT -> BTC/USD
        ETH/USDT -> ETH/USD
        """
        # Remove slash for API calls
        normalized = symbol.replace("/USDT", "/USD").replace("USDT", "USD")
        # Alpaca uses no slash for crypto
        return normalized.replace("/", "")

    def denormalize_symbol(self, symbol: str) -> str:
        """
        Convert Alpaca symbol to standard format.
        BTCUSD -> BTC/USDT
        """
        # Common crypto bases
        bases = ["BTC", "ETH", "SOL", "DOGE", "SHIB", "AVAX", "LINK", "UNI", "AAVE", "XRP", "ADA", "DOT", "MATIC"]
        
        for base in bases:
            if symbol.startswith(base):
                quote = symbol[len(base):]
                if quote == "USD":
                    return f"{base}/USDT"
                return f"{base}/{quote}"
        
        return symbol

    def _api_symbol(self, symbol: str) -> str:
        """Get symbol format for API calls."""
        return self.normalize_symbol(symbol)

    # ═══════════════════════════════════════════════════════════════
    # HTTP HELPERS
    # ═══════════════════════════════════════════════════════════════

    @property
    def _headers(self) -> Dict:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def _check_rate_limit(self) -> bool:
        """Check if we're within rate limits."""
        now = time.time()
        # Remove old requests outside window
        self._request_times = [
            t for t in self._request_times 
            if now - t < self.RATE_LIMIT_WINDOW
        ]
        
        if len(self._request_times) >= self.RATE_LIMIT_REQUESTS:
            logger.warning("⚠️ Rate limit approaching, throttling...")
            time.sleep(1)
            return False
        
        self._request_times.append(now)
        return True

    def _get(self, url: str, params: Dict = None) -> Optional[Dict]:
        """HTTP GET with error handling."""
        self._check_rate_limit()
        
        try:
            resp = self._session.get(
                url,
                headers=self._headers,
                params=params or {},
                timeout=self.REQUEST_TIMEOUT,
            )
            
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"⚠️ Rate limited, waiting {retry_after}s")
                time.sleep(retry_after)
                return self._get(url, params)
            
            resp.raise_for_status()
            return resp.json()
            
        except requests.exceptions.HTTPError as e:
            error_body = ""
            try:
                error_body = resp.text
            except:
                pass
            logger.error(f"❌ Alpaca HTTP error: {e} | {error_body}")
        except requests.exceptions.Timeout:
            logger.error(f"❌ Alpaca request timeout: {url}")
        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Alpaca connection error: {e}")
        except Exception as e:
            logger.error(f"❌ Alpaca GET failed: {e}")
        
        return None

    def _post(self, url: str, payload: Dict) -> Optional[Dict]:
        """HTTP POST with error handling."""
        self._check_rate_limit()
        
        try:
            resp = self._session.post(
                url,
                headers=self._headers,
                json=payload,
                timeout=self.REQUEST_TIMEOUT,
            )
            
            if resp.status_code == 429:
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(f"⚠️ Rate limited, waiting {retry_after}s")
                time.sleep(retry_after)
                return self._post(url, payload)
            
            resp.raise_for_status()
            return resp.json()
            
        except requests.exceptions.HTTPError as e:
            error_body = ""
            try:
                error_body = resp.text
            except:
                pass
            logger.error(f"❌ Alpaca HTTP error: {e} | {error_body}")
        except requests.exceptions.Timeout:
            logger.error(f"❌ Alpaca request timeout: {url}")
        except Exception as e:
            logger.error(f"❌ Alpaca POST failed: {e}")
        
        return None

    def _delete(self, url: str) -> bool:
        """HTTP DELETE with error handling."""
        self._check_rate_limit()
        
        try:
            resp = self._session.delete(
                url,
                headers=self._headers,
                timeout=self.REQUEST_TIMEOUT,
            )
            return resp.status_code in (200, 204)
        except Exception as e:
            logger.error(f"❌ Alpaca DELETE failed: {e}")
        return False

    # ═══════════════════════════════════════════════════════════════
    # MARKET DATA
    # ═══════════════════════════════════════════════════════════════

    def get_price(self, symbol: str) -> float:
        """
        Get current price for symbol.
        Uses cache within cycle to ensure consistency.
        """
        # Check cache first
        now = time.time()
        if symbol in self._price_cache:
            cached_price, cached_time = self._price_cache[symbol]
            if now - cached_time < self.PRICE_CACHE_TTL:
                return cached_price

        # Fetch from API
        alpaca_sym = self._api_symbol(symbol)
        url = f"{DATA_BASE_URL}/v1beta3/crypto/us/latest/trades"

        data = self._get(url, params={"symbols": alpaca_sym})

        if data and "trades" in data:
            trade = data["trades"].get(alpaca_sym)
            if trade and "p" in trade:
                price = float(trade["p"])
                self._price_cache[symbol] = (price, now)
                
                # Also store in state for fallback
                if self.state:
                    self.state.set(f"last_price_{symbol}", price)
                
                return price

        # Try quotes endpoint as fallback
        quote_url = f"{DATA_BASE_URL}/v1beta3/crypto/us/latest/quotes"
        quote_data = self._get(quote_url, params={"symbols": alpaca_sym})
        
        if quote_data and "quotes" in quote_data:
            quote = quote_data["quotes"].get(alpaca_sym)
            if quote:
                # Use midpoint of bid/ask
                bid = float(quote.get("bp", 0))
                ask = float(quote.get("ap", 0))
                if bid > 0 and ask > 0:
                    price = (bid + ask) / 2
                    self._price_cache[symbol] = (price, now)
                    return price

        # Fallback to stored price
        if self.state:
            cached = self.state.get(f"last_price_{symbol}")
            if cached:
                logger.warning(f"⚠️ Using stored price for {symbol}: ${cached}")
                return float(cached)

        logger.error(f"❌ Could not get price for {symbol}")
        return 0.0

    def get_ticker(self, symbol: str) -> Dict:
        """Get extended ticker information."""
        alpaca_sym = self._api_symbol(symbol)
        
        # Get latest quote
        quote_url = f"{DATA_BASE_URL}/v1beta3/crypto/us/latest/quotes"
        quote_data = self._get(quote_url, params={"symbols": alpaca_sym})
        
        # Get latest trade
        trade_url = f"{DATA_BASE_URL}/v1beta3/crypto/us/latest/trades"
        trade_data = self._get(trade_url, params={"symbols": alpaca_sym})
        
        price = 0.0
        bid = 0.0
        ask = 0.0
        
        if trade_data and "trades" in trade_data:
            trade = trade_data["trades"].get(alpaca_sym, {})
            price = float(trade.get("p", 0))
        
        if quote_data and "quotes" in quote_data:
            quote = quote_data["quotes"].get(alpaca_sym, {})
            bid = float(quote.get("bp", 0))
            ask = float(quote.get("ap", 0))
        
        if price == 0 and bid > 0 and ask > 0:
            price = (bid + ask) / 2
        
        spread = ask - bid if bid > 0 and ask > 0 else 0
        spread_pct = (spread / price * 100) if price > 0 else 0
        
        return {
            "symbol": symbol,
            "price": price,
            "bid": bid,
            "ask": ask,
            "spread": spread,
            "spread_pct": round(spread_pct, 4),
            "volume_24h": 0.0,  # Would need separate call
            "change_24h": 0.0,
            "change_pct": 0.0,
            "high_24h": price,
            "low_24h": price,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def get_recent_candles(
        self,
        symbol: str,
        limit: int = 150,
        timeframe: str = "5m"
    ) -> List[Dict]:
        """
        Fetch OHLCV candles from Alpaca.
        Falls back to simulated candles if insufficient data.
        """
        # Map timeframe to Alpaca format
        tf_map = {
            "1m": "1Min",
            "5m": "5Min",
            "15m": "15Min",
            "30m": "30Min",
            "1h": "1Hour",
            "4h": "4Hour",
            "1d": "1Day",
        }
        alpaca_tf = tf_map.get(timeframe, "5Min")
        
        # Check cache
        cache_key = f"{symbol}_{timeframe}"
        now = time.time()
        
        if cache_key in self._candle_cache:
            cache_time = self._candle_cache_time.get(cache_key, 0)
            # Cache for 60 seconds
            if now - cache_time < 60:
                cached = self._candle_cache[cache_key]
                if len(cached) >= limit:
                    return cached[-limit:]

        alpaca_sym = self._api_symbol(symbol)
        url = f"{DATA_BASE_URL}/v1beta3/crypto/us/bars"

        # Calculate start time based on timeframe
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
        minutes = tf_minutes.get(timeframe, 5)
        hours_needed = max(2, (limit * minutes) // 60 + 1)
        
        start = (datetime.utcnow() - timedelta(hours=hours_needed)).strftime("%Y-%m-%dT%H:%M:%SZ")

        data = self._get(url, params={
            "symbols": alpaca_sym,
            "timeframe": alpaca_tf,
            "start": start,
            "limit": limit,
        })

        candles = []
        if data and "bars" in data:
            bars = data["bars"].get(alpaca_sym, [])
            for bar in bars:
                candles.append({
                    "open": float(bar.get("o", 0)),
                    "high": float(bar.get("h", 0)),
                    "low": float(bar.get("l", 0)),
                    "close": float(bar.get("c", 0)),
                    "volume": float(bar.get("v", 0)),
                    "timestamp": bar.get("t", ""),
                    "timeframe": timeframe,
                })

        if len(candles) >= 50:
            self._candle_cache[cache_key] = candles
            self._candle_cache_time[cache_key] = now
            logger.debug(f"📊 {symbol}: {len(candles)} candles from Alpaca")
            return candles[-limit:]

        # Fallback to simulated candles
        logger.warning(
            f"⚠️ Alpaca returned {len(candles)} candles for {symbol} — "
            f"padding with simulated data"
        )
        return self._generate_fallback_candles(symbol, limit, candles, timeframe)

    def _generate_fallback_candles(
        self,
        symbol: str,
        limit: int,
        seed_candles: List[Dict],
        timeframe: str = "5m"
    ) -> List[Dict]:
        """Generate simulated candles when API data insufficient."""
        cache_key = f"{symbol}_{timeframe}"
        existing = self._candle_cache.get(cache_key, [])
        candles = existing + seed_candles
        
        # Get seed price
        if candles:
            seed_price = candles[-1]["close"]
        elif self.state:
            seed_price = self.state.get(f"last_price_{symbol}") or 100.0
        else:
            seed_price = 100.0

        needed = max(0, limit - len(candles))
        
        for i in range(needed):
            # GBM parameters
            mu = 0.00005
            sigma = 0.0015
            z = random.gauss(0, 1)
            
            open_p = seed_price * (1 + random.gauss(0, 0.0003))
            close_p = open_p * math.exp((mu - 0.5 * sigma ** 2) + sigma * z)
            rng = abs(close_p - open_p)
            
            candles.append({
                "open": round(open_p, 6),
                "high": round(max(open_p, close_p) + random.uniform(0, rng * 0.5), 6),
                "low": round(min(open_p, close_p) - random.uniform(0, rng * 0.5), 6),
                "close": round(close_p, 6),
                "volume": round(random.uniform(100, 2000), 2),
                "timestamp": (datetime.utcnow() - timedelta(minutes=(needed - i) * 5)).isoformat(),
                "timeframe": timeframe,
                "simulated": True,
            })
            seed_price = close_p

        # Trim cache
        if len(candles) > self.CANDLE_CACHE_SIZE:
            candles = candles[-self.CANDLE_CACHE_SIZE:]
        
        self._candle_cache[cache_key] = candles
        self._candle_cache_time[cache_key] = time.time()
        
        return candles[-limit:]

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
        """Place a BUY order on Alpaca."""
        # Validate order
        is_valid, error = self.validate_order(symbol, quantity, "BUY", price)
        if not is_valid:
            logger.warning(f"❌ BUY validation failed: {error}")
            return self._rejection(symbol, "BUY", error)

        result = self._place_order(symbol, "buy", quantity, order_type, price)

        if not result:
            return self._rejection(symbol, "BUY", "Order failed")

        cost = round(result["price"] * result["filled_qty"], 8)
        fee = self._calculate_fee(cost)

        receipt = self._success_receipt(
            symbol=symbol,
            action="BUY",
            price=result["price"],
            quantity=result["filled_qty"],
            fee=fee,
            order_id=result["order_id"],
            mode=self.MODE,
            exchange=self.EXCHANGE_NAME,
        )

        logger.info(
            f"📝🟢 ALPACA BUY FILLED | {symbol} | "
            f"Qty={result['filled_qty']} @ ${result['price']:.4f} | "
            f"Cost=${cost:.2f} | Fee=${fee:.4f}"
        )

        return receipt

    def sell(
        self,
        symbol: str,
        quantity: float,
        order_type: str = "MARKET",
        price: Optional[float] = None,
    ) -> Dict:
        """Place a SELL order on Alpaca."""
        # Check position
        if self.state:
            position = self.state.get_position(symbol)
            if not position:
                logger.warning(f"❌ SELL rejected — no position for {symbol}")
                return self._rejection(symbol, "SELL", "No position")

            pos_qty = position.get("quantity", 0)
            if quantity > pos_qty:
                logger.warning(
                    f"❌ SELL rejected — qty {quantity} > position {pos_qty}"
                )
                return self._rejection(symbol, "SELL", f"Quantity exceeds position ({pos_qty})")

        result = self._place_order(symbol, "sell", quantity, order_type, price)

        if not result:
            return self._rejection(symbol, "SELL", "Order failed")

        proceeds = round(result["price"] * result["filled_qty"], 8)
        fee = self._calculate_fee(proceeds)

        # Calculate PnL if we have position info
        gross_pnl = 0.0
        if self.state:
            position = self.state.get_position(symbol)
            if position:
                entry = position.get("entry_price", position.get("avg_price", result["price"]))
                gross_pnl = round((result["price"] - entry) * result["filled_qty"], 8)

        net_pnl = gross_pnl - fee

        receipt = self._success_receipt(
            symbol=symbol,
            action="SELL",
            price=result["price"],
            quantity=result["filled_qty"],
            fee=fee,
            order_id=result["order_id"],
            mode=self.MODE,
            exchange=self.EXCHANGE_NAME,
            proceeds=proceeds,
            gross_pnl=gross_pnl,
            net_pnl=net_pnl,
        )

        logger.info(
            f"📝🔴 ALPACA SELL FILLED | {symbol} | "
            f"Qty={result['filled_qty']} @ ${result['price']:.4f} | "
            f"PnL=${net_pnl:+.4f}"
        )

        return receipt

    def _place_order(
        self,
        symbol: str,
        side: str,
        quantity: float,
        order_type: str = "MARKET",
        limit_price: Optional[float] = None,
    ) -> Optional[Dict]:
        """
        Place order and poll until filled.
        Returns order details or None on failure.
        """
        alpaca_sym = self._api_symbol(symbol)
        url = f"{self.base_url}/v2/orders"

        # Round quantity
        quantity = self.round_quantity(symbol, quantity)

        payload = {
            "symbol": alpaca_sym,
            "qty": str(quantity),
            "side": side,
            "type": order_type.lower(),
            "time_in_force": "gtc",
        }

        if order_type.upper() == "LIMIT" and limit_price:
            payload["limit_price"] = str(round(limit_price, 2))

        logger.info(f"📤 Alpaca {side.upper()} | {alpaca_sym} qty={quantity}")
        result = self._post(url, payload)

        if not result:
            logger.error(f"❌ Alpaca {side.upper()} — no API response")
            return None

        order_id = result.get("id")
        status = result.get("status", "")

        # Check for immediate rejection
        if status in ("rejected", "canceled", "expired"):
            reason = result.get("reject_reason") or result.get("failed_at") or "unknown"
            logger.error(f"❌ Order {status}: {reason}")
            return None

        # Poll for fill if not immediately filled
        if status != "filled":
            logger.info(f"⏳ Order {order_id} status={status} — polling...")
            result = self._poll_order_fill(order_id)
            
            if not result:
                logger.error(f"❌ Order {order_id} did not fill")
                # Cancel dangling order
                self._delete(f"{self.base_url}/v2/orders/{order_id}")
                return None

        # Extract fill details
        fill_price = float(result.get("filled_avg_price") or 0)
        filled_qty = float(result.get("filled_qty") or quantity)

        # Sometimes need extra fetch for fill price
        if fill_price == 0:
            final = self._get(f"{self.base_url}/v2/orders/{order_id}")
            if final:
                fill_price = float(final.get("filled_avg_price") or 0)
                filled_qty = float(final.get("filled_qty") or filled_qty)

        return {
            "order_id": order_id,
            "price": fill_price,
            "filled_qty": filled_qty,
            "status": "FILLED",
            "timestamp": datetime.utcnow().isoformat(),
        }

    def _poll_order_fill(
        self,
        order_id: str,
        timeout: int = 15,
        interval: float = 0.5
    ) -> Optional[Dict]:
        """Poll order status until filled or timeout."""
        url = f"{self.base_url}/v2/orders/{order_id}"
        deadline = time.time() + timeout

        while time.time() < deadline:
            time.sleep(interval)
            data = self._get(url)
            
            if not data:
                continue
            
            status = data.get("status", "")
            logger.debug(f"⏳ Order {order_id}: {status}")
            
            if status == "filled":
                return data
            
            if status in ("canceled", "rejected", "expired"):
                logger.error(f"❌ Order {order_id} terminal: {status}")
                return None
            
            # Increase interval gradually
            interval = min(interval * 1.2, 2.0)

        logger.warning(f"⚠️ Order {order_id} not filled after {timeout}s")
        return None

    def _calculate_fee(self, amount: float) -> float:
        """
        Calculate trading fee.
        Alpaca has no commission, but we account for spread costs.
        """
        # Alpaca is commission-free, but estimate spread cost
        spread_cost = amount * 0.001  # ~0.1% spread estimate
        return round(spread_cost, 8)

    # ═══════════════════════════════════════════════════════════════
    # ACCOUNT & POSITIONS
    # ═══════════════════════════════════════════════════════════════

    def get_balance(self) -> float:
        """Get available buying power from Alpaca account."""
        data = self._get(f"{self.base_url}/v2/account")
        
        if data:
            # Use cash for crypto, buying_power includes margin
            cash = float(data.get("cash", 0))
            buying_power = float(data.get("buying_power", 0))
            
            # For crypto, use the smaller of cash and buying_power
            balance = min(cash, buying_power) if cash > 0 else buying_power
            
            if self.state:
                self.state.set("alpaca_balance", balance)
            
            return round(balance, 2)

        # Fallback
        if self.state:
            return self.state.get("alpaca_balance", 0.0)
        return 0.0

    def get_total_balance(self) -> float:
        """Get total portfolio value."""
        data = self._get(f"{self.base_url}/v2/account")
        if data:
            return round(float(data.get("portfolio_value", 0)), 2)
        return self.get_balance()

    def get_buying_power(self) -> float:
        """Get buying power."""
        data = self._get(f"{self.base_url}/v2/account")
        if data:
            return round(float(data.get("buying_power", 0)), 2)
        return self.get_balance()

    def get_position(self, symbol: str) -> Optional[Dict]:
        """Get position for a specific symbol."""
        alpaca_sym = self._api_symbol(symbol)
        data = self._get(f"{self.base_url}/v2/positions/{alpaca_sym}")
        
        if not data:
            return None

        return {
            "symbol": symbol,
            "side": "long" if float(data.get("qty", 0)) > 0 else "short",
            "quantity": abs(float(data.get("qty", 0))),
            "entry_price": float(data.get("avg_entry_price", 0)),
            "current_price": float(data.get("current_price", 0)),
            "market_value": float(data.get("market_value", 0)),
            "cost_basis": float(data.get("cost_basis", 0)),
            "unrealized_pnl": float(data.get("unrealized_pl", 0)),
            "unrealized_pnl_pct": float(data.get("unrealized_plpc", 0)) * 100,
        }

    def get_open_positions(self) -> Dict[str, Dict]:
        """Get all open positions."""
        data = self._get(f"{self.base_url}/v2/positions")
        
        if not data:
            # Fallback to state
            if self.state:
                return self.state.get_all_positions()
            return {}

        positions = {}
        for pos in data:
            sym = pos.get("symbol", "")
            standard_sym = self.denormalize_symbol(sym)
            
            positions[standard_sym] = {
                "symbol": standard_sym,
                "alpaca_symbol": sym,
                "side": "long" if float(pos.get("qty", 0)) > 0 else "short",
                "quantity": abs(float(pos.get("qty", 0))),
                "entry_price": float(pos.get("avg_entry_price", 0)),
                "current_price": float(pos.get("current_price", 0)),
                "market_value": float(pos.get("market_value", 0)),
                "cost_basis": float(pos.get("cost_basis", 0)),
                "unrealized_pnl": float(pos.get("unrealized_pl", 0)),
            }

        return positions

    def get_account_summary(self) -> Dict:
        """Get comprehensive account summary."""
        data = self._get(f"{self.base_url}/v2/account")
        
        if not data:
            return {
                "balance": self.get_balance(),
                "mode": self.MODE,
                "exchange": self.EXCHANGE_NAME,
                "error": "Could not fetch account data",
            }

        positions = self.get_open_positions()
        
        return {
            "balance": round(float(data.get("cash", 0)), 2),
            "total_equity": round(float(data.get("equity", 0)), 2),
            "buying_power": round(float(data.get("buying_power", 0)), 2),
            "portfolio_value": round(float(data.get("portfolio_value", 0)), 2),
            "open_positions": len(positions),
            "total_exposure": sum(p.get("market_value", 0) for p in positions.values()),
            "unrealized_pnl": sum(p.get("unrealized_pnl", 0) for p in positions.values()),
            "account_status": data.get("status", "unknown"),
            "trading_blocked": data.get("trading_blocked", False),
            "mode": self.MODE,
            "exchange": self.EXCHANGE_NAME,
            "timestamp": datetime.utcnow().isoformat(),
        }

    def close_position(self, symbol: str, quantity: Optional[float] = None) -> Dict:
        """Close a position (full or partial)."""
        position = self.get_position(symbol)
        
        if not position:
            return self._rejection(symbol, "SELL", "No position to close")

        close_qty = quantity or position.get("quantity", 0)
        return self.sell(symbol, close_qty)

    def close_all_positions(self) -> List[Dict]:
        """Close all open positions."""
        results = []
        
        # Use Alpaca's bulk close endpoint
        resp = self._delete(f"{self.base_url}/v2/positions")
        
        if resp:
            logger.info("✅ All positions close requested")
            # Get individual results
            for symbol in list(self.get_open_positions().keys()):
                results.append({
                    "symbol": symbol,
                    "status": "CLOSING",
                })
        else:
            # Fallback to individual closes
            for symbol, pos in self.get_open_positions().items():
                result = self.sell(symbol, pos.get("quantity", 0))
                results.append(result)

        return results

    # ═══════════════════════════════════════════════════════════════
    # SYMBOL INFO
    # ═══════════════════════════════════════════════════════════════

    def get_symbol_info(self, symbol: str) -> Dict:
        """Get trading rules for a symbol."""
        alpaca_sym = self._api_symbol(symbol)
        
        # Alpaca crypto assets
        data = self._get(f"{self.base_url}/v2/assets/{alpaca_sym}")
        
        if data:
            return {
                "symbol": symbol,
                "alpaca_symbol": alpaca_sym,
                "base_asset": data.get("symbol", "")[:3],
                "quote_asset": "USD",
                "min_quantity": float(data.get("min_order_size", 0.0001)),
                "max_quantity": float(data.get("max_order_size", 100000)),
                "quantity_step": float(data.get("min_trade_increment", 0.0001)),
                "min_notional": 1.0,
                "price_precision": 8,
                "quantity_precision": 6,
                "is_tradable": data.get("tradable", True),
                "marginable": data.get("marginable", False),
            }

        # Defaults
        return {
            "symbol": symbol,
            "base_asset": symbol.split("/")[0] if "/" in symbol else symbol[:3],
            "quote_asset": "USD",
            "min_quantity": 0.0001,
            "max_quantity": 100000.0,
            "quantity_step": 0.0001,
            "min_notional": 1.0,
            "price_precision": 8,
            "quantity_precision": 6,
            "is_tradable": True,
        }

    def get_tradable_symbols(self) -> List[str]:
        """Get list of tradable crypto symbols."""
        data = self._get(f"{self.base_url}/v2/assets?asset_class=crypto&status=active")
        
        if data:
            return [
                self.denormalize_symbol(asset["symbol"])
                for asset in data
                if asset.get("tradable", False)
            ]
        
        # Default crypto pairs
        return ["BTC/USDT", "ETH/USDT", "SOL/USDT", "DOGE/USDT"]

    # ═══════════════════════════════════════════════════════════════
    # ORDER MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def cancel_order(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """Cancel an open order."""
        success = self._delete(f"{self.base_url}/v2/orders/{order_id}")
        
        if success:
            return {
                "status": "CANCELLED",
                "order_id": order_id,
                "symbol": symbol or "",
            }
        
        return {
            "status": "REJECTED",
            "order_id": order_id,
            "symbol": symbol or "",
            "reason": "Cancel failed",
        }

    def cancel_all_orders(self) -> bool:
        """Cancel all open orders."""
        success = self._delete(f"{self.base_url}/v2/orders")
        
        if success:
            logger.info("✅ All orders cancelled")
        
        return success

    def get_order_status(self, order_id: str, symbol: Optional[str] = None) -> Dict:
        """Get status of an order."""
        data = self._get(f"{self.base_url}/v2/orders/{order_id}")
        
        if data:
            return {
                "order_id": order_id,
                "symbol": data.get("symbol", ""),
                "status": data.get("status", "unknown").upper(),
                "side": data.get("side", "").upper(),
                "quantity": float(data.get("qty", 0)),
                "filled_qty": float(data.get("filled_qty", 0)),
                "remaining_qty": float(data.get("qty", 0)) - float(data.get("filled_qty", 0)),
                "avg_price": float(data.get("filled_avg_price", 0)),
                "created_at": data.get("created_at", ""),
                "filled_at": data.get("filled_at", ""),
            }
        
        return {
            "order_id": order_id,
            "status": "UNKNOWN",
            "error": "Could not fetch order",
        }

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict]:
        """Get all open orders."""
        params = {"status": "open"}
        if symbol:
            params["symbols"] = self._api_symbol(symbol)
        
        data = self._get(f"{self.base_url}/v2/orders", params)
        
        if not data:
            return []
        
        return [
            {
                "order_id": order.get("id"),
                "symbol": self.denormalize_symbol(order.get("symbol", "")),
                "side": order.get("side", "").upper(),
                "quantity": float(order.get("qty", 0)),
                "filled_qty": float(order.get("filled_qty", 0)),
                "type": order.get("type", ""),
                "status": order.get("status", ""),
                "created_at": order.get("created_at", ""),
            }
            for order in data
        ]

    # ═══════════════════════════════════════════════════════════════
    # CYCLE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def begin_cycle(self) -> None:
        """Called at start of each trading cycle."""
        # Clear price cache for fresh data
        self._price_cache.clear()
        logger.debug("🔄 Alpaca cycle started — price cache cleared")

    def end_cycle(self) -> None:
        """Called at end of each trading cycle."""
        pass

    # ═══════════════════════════════════════════════════════════════
    # HEALTH & UTILITIES
    # ═══════════════════════════════════════════════════════════════

    def ping(self) -> bool:
        """Check if Alpaca API is reachable."""
        data = self._get(f"{self.base_url}/v2/account")
        return data is not None

    def get_server_time(self) -> Optional[datetime]:
        """Get Alpaca server time."""
        data = self._get(f"{self.base_url}/v2/clock")
        if data and "timestamp" in data:
            try:
                return datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
            except:
                pass
        return datetime.utcnow()

    def is_market_open(self, symbol: Optional[str] = None) -> bool:
        """
        Check if market is open.
        Crypto is 24/7 but Alpaca has maintenance windows.
        """
        data = self._get(f"{self.base_url}/v2/clock")
        if data:
            return data.get("is_open", True)
        return True  # Assume open

    def get_rate_limit_status(self) -> Dict:
        """Get current rate limit status."""
        used = len(self._request_times)
        return {
            "requests_used": used,
            "requests_limit": self.RATE_LIMIT_REQUESTS,
            "requests_remaining": max(0, self.RATE_LIMIT_REQUESTS - used),
            "window_seconds": self.RATE_LIMIT_WINDOW,
        }

    def seed_price(self, symbol: str, price: float) -> None:
        """Manually set price for testing."""
        self._price_cache[symbol] = (price, time.time())
        if self.state:
            self.state.set(f"last_price_{symbol}", price)

    def seed_candles(self, symbol: str, candles: List[Dict]) -> None:
        """Manually set candles for testing."""
        self._candle_cache[f"{symbol}_5m"] = candles
        self._candle_cache_time[f"{symbol}_5m"] = time.time()

    def reset_candles(self, symbol: str) -> None:
        """Clear candle cache for symbol."""
        keys_to_remove = [k for k in self._candle_cache if k.startswith(symbol)]
        for key in keys_to_remove:
            self._candle_cache.pop(key, None)
            self._candle_cache_time.pop(key, None)

    def close(self) -> None:
        """Cleanup resources."""
        if hasattr(self, '_session'):
            self._session.close()
        logger.info("🔌 Alpaca connection closed")

    def __repr__(self) -> str:
        return f"<AlpacaExchange | {self.MODE} | {self.base_url}>"