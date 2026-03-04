# app/exchange/binance_market.py

"""
Binance Market Data Client — Production Grade

Responsibilities:
- Fetch real-time price from Binance public API
- Fetch OHLCV candlestick data from Binance public API
- NO trading, NO balance, NO API keys required
- Automatic symbol normalization (BTC/USDT → BTCUSDT)
- Response caching to prevent rate limiting
- Fallback to mock data if Binance unreachable

Binance Public API endpoints used:
    GET /api/v3/ticker/price   — latest price
    GET /api/v3/klines         — OHLCV candles

No authentication required for market data.
Rate limit: 1200 requests/minute (weight-based)

Usage:
    client = BinanceMarketClient()
    price = client.get_price("BTC/USDT")
    candles = client.get_klines("BTC/USDT", "5m", 150)
"""

import math
import random
import time
from typing import List, Dict, Optional
from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Optional requests import ──────────────────────────────────────
try:
    import requests
    REQUESTS_AVAILABLE = True
except ImportError:
    REQUESTS_AVAILABLE = False
    logger.warning("⚠️ requests not installed — BinanceMarketClient will use mock data")


# ── Binance API base ──────────────────────────────────────────────
BINANCE_BASE_URL = "https://api.binance.com"
BINANCE_TESTNET_URL = "https://testnet.binance.vision"

# ── Realistic fallback seed prices ───────────────────────────────
SEED_PRICES: Dict[str, float] = {
    "BTCUSDT":  65000.0,
    "ETHUSDT":  3200.0,
    "SOLUSDT":  145.0,
    "BNBUSDT":  580.0,
    "XRPUSDT":  0.52,
    "ADAUSDT":  0.45,
    "DOGEUSDT": 0.12,
    "AVAXUSDT": 35.0,
    "DOTUSDT":  7.5,
    "MATICUSDT": 0.70,
}

DEFAULT_SEED_PRICE = 100.0


def _normalize_symbol(symbol: str) -> str:
    """
    Normalize symbol to Binance format.

    Examples:
        BTC/USDT  → BTCUSDT
        btc/usdt  → BTCUSDT
        BTCUSDT   → BTCUSDT
    """
    return symbol.upper().replace("/", "").strip()


class BinanceMarketClient:
    """
    Binance Public Market Data Client.

    Fetches real price and candle data from Binance REST API.
    No API keys required — public endpoints only.

    Features:
    - Real Binance market data
    - Price response caching (TTL-based, default 5s)
    - Candle response caching (TTL-based, default 30s)
    - Automatic retry on transient failures
    - Graceful fallback to mock data if API unreachable
    - Symbol normalization (handles BTC/USDT and BTCUSDT formats)
    """

    # ── Cache TTL settings ────────────────────────────────────────
    PRICE_CACHE_TTL_SEC = 5       # Price cache: 5 seconds
    CANDLE_CACHE_TTL_SEC = 30     # Candle cache: 30 seconds
    REQUEST_TIMEOUT_SEC = 10      # HTTP timeout
    MAX_RETRIES = 3               # Retry attempts on failure

    def __init__(
        self,
        base_url: str = None,
        use_testnet: bool = False,
        price_cache_ttl: int = None,
        candle_cache_ttl: int = None,
    ):
        """
        Initialize Binance market client.

        Args:
            base_url:         Override API base URL
            use_testnet:      Use Binance testnet instead of mainnet
            price_cache_ttl:  Seconds to cache price responses
            candle_cache_ttl: Seconds to cache candle responses
        """
        if base_url:
            self.base_url = base_url.rstrip("/")
        elif use_testnet:
            self.base_url = BINANCE_TESTNET_URL
        else:
            self.base_url = BINANCE_BASE_URL

        self.price_cache_ttl = price_cache_ttl or self.PRICE_CACHE_TTL_SEC
        self.candle_cache_ttl = candle_cache_ttl or self.CANDLE_CACHE_TTL_SEC

        # ── Internal caches ───────────────────────────────────────
        self._price_cache: Dict[str, Dict] = {}   # {symbol: {price, ts}}
        self._candle_cache: Dict[str, Dict] = {}  # {symbol: {candles, ts}}

        # ── Track API availability ────────────────────────────────
        self._api_available: bool = True
        self._last_failure: Optional[float] = None
        self._failure_backoff_sec: int = 60  # Wait 60s after failure

        logger.info(
            f"🌐 BinanceMarketClient initialized | "
            f"URL={self.base_url} | "
            f"PriceTTL={self.price_cache_ttl}s | "
            f"CandleTTL={self.candle_cache_ttl}s"
        )

    # ═════════════════════════════════════════════════════
    #  PUBLIC API
    # ═════════════════════════════════════════════════════

    def get_price(self, symbol: str) -> float:
        """
        Fetch current market price from Binance.

        Uses cached value if within TTL to avoid rate limiting.
        Falls back to mock price if API unavailable.

        Args:
            symbol: Trading pair (e.g. "BTC/USDT" or "BTCUSDT")

        Returns:
            Current price as float
        """
        norm = _normalize_symbol(symbol)

        # ── Check cache ───────────────────────────────────────────
        cached = self._price_cache.get(norm)
        if cached and (time.time() - cached["ts"]) < self.price_cache_ttl:
            logger.debug(
                f"💾 Price cache hit | {norm} = ${cached['price']:.4f}"
            )
            return cached["price"]

        # ── Try real API ──────────────────────────────────────────
        if self._should_try_api():
            price = self._fetch_price_api(norm)
            if price and price > 0:
                self._price_cache[norm] = {
                    "price": price,
                    "ts": time.time(),
                }
                self._api_available = True
                logger.info(
                    f"📈 Price fetched | {norm} = ${price:.4f}"
                )
                return price

        # ── Fallback to mock ──────────────────────────────────────
        logger.warning(
            f"⚠️ Binance API unavailable for {norm} — using mock price"
        )
        return self._mock_price(norm)

    def get_klines(
        self,
        symbol: str,
        interval: str = "5m",
        limit: int = 150,
    ) -> List[List]:
        """
        Fetch OHLCV candlestick data from Binance.

        Returns Binance-format klines:
            [open_time, open, high, low, close, volume, ...]

        Uses cached value if within TTL.
        Falls back to mock candles if API unavailable.

        Args:
            symbol:   Trading pair (e.g. "BTC/USDT" or "BTCUSDT")
            interval: Candle timeframe ("1m", "5m", "15m", "1h", etc.)
            limit:    Number of candles (max 1000)

        Returns:
            List of kline arrays in Binance format
        """
        norm = _normalize_symbol(symbol)
        cache_key = f"{norm}_{interval}_{limit}"

        # ── Check cache ───────────────────────────────────────────
        cached = self._candle_cache.get(cache_key)
        if cached and (time.time() - cached["ts"]) < self.candle_cache_ttl:
            logger.debug(
                f"💾 Candle cache hit | {norm} {interval} "
                f"({len(cached['candles'])} candles)"
            )
            return cached["candles"]

        # ── Try real API ──────────────────────────────────────────
        if self._should_try_api():
            candles = self._fetch_klines_api(norm, interval, limit)
            if candles and len(candles) >= 60:
                self._candle_cache[cache_key] = {
                    "candles": candles,
                    "ts": time.time(),
                }
                self._api_available = True
                logger.info(
                    f"🕯️ Candles fetched | {norm} {interval} | "
                    f"{len(candles)} candles"
                )
                return candles

        # ── Fallback to mock ──────────────────────────────────────
        logger.warning(
            f"⚠️ Binance API unavailable for {norm} candles — using mock"
        )
        return self._mock_klines(norm, limit)

    def ping(self) -> bool:
        """
        Check if Binance API is reachable.

        Returns:
            True if API responds, False otherwise
        """
        if not REQUESTS_AVAILABLE:
            return False

        try:
            resp = requests.get(
                f"{self.base_url}/api/v3/ping",
                timeout=5,
            )
            return resp.status_code == 200
        except Exception:
            return False

    def get_exchange_info(self, symbol: str) -> Optional[Dict]:
        """
        Fetch symbol trading rules from Binance.

        Returns lot size, min notional, tick size etc.
        Useful for validating order quantities.

        Args:
            symbol: Trading pair

        Returns:
            Symbol info dict or None
        """
        if not REQUESTS_AVAILABLE or not self._should_try_api():
            return None

        norm = _normalize_symbol(symbol)

        try:
            resp = requests.get(
                f"{self.base_url}/api/v3/exchangeInfo",
                params={"symbol": norm},
                timeout=self.REQUEST_TIMEOUT_SEC,
            )
            resp.raise_for_status()
            data = resp.json()

            symbols = data.get("symbols", [])
            for s in symbols:
                if s.get("symbol") == norm:
                    return s

        except Exception as e:
            logger.warning(f"⚠️ Exchange info fetch failed: {e}")

        return None

    # ═════════════════════════════════════════════════════
    #  API FETCH METHODS (INTERNAL)
    # ═════════════════════════════════════════════════════

    def _fetch_price_api(self, symbol: str) -> Optional[float]:
        """
        Fetch price from Binance ticker endpoint.

        GET /api/v3/ticker/price?symbol=BTCUSDT

        Returns:
            Price as float, or None on failure
        """
        if not REQUESTS_AVAILABLE:
            return None

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.get(
                    f"{self.base_url}/api/v3/ticker/price",
                    params={"symbol": symbol},
                    timeout=self.REQUEST_TIMEOUT_SEC,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    price = float(data.get("price", 0))
                    if price > 0:
                        return price

                elif resp.status_code == 400:
                    # Invalid symbol — don't retry
                    logger.error(
                        f"❌ Invalid symbol: {symbol} | "
                        f"Response: {resp.text[:100]}"
                    )
                    return None

                elif resp.status_code == 429:
                    # Rate limited — back off
                    logger.warning(
                        f"⚠️ Binance rate limited (429) | "
                        f"Attempt {attempt + 1}/{self.MAX_RETRIES}"
                    )
                    time.sleep(2 ** attempt)

                else:
                    logger.warning(
                        f"⚠️ Price fetch HTTP {resp.status_code} | "
                        f"{symbol} | Attempt {attempt + 1}"
                    )

            except requests.exceptions.Timeout:
                logger.warning(
                    f"⚠️ Price fetch timeout | {symbol} | "
                    f"Attempt {attempt + 1}/{self.MAX_RETRIES}"
                )
                time.sleep(1)

            except requests.exceptions.ConnectionError:
                logger.warning(
                    f"⚠️ Connection error fetching price | {symbol}"
                )
                self._mark_api_unavailable()
                return None

            except Exception as e:
                logger.error(f"❌ Price fetch error: {e}")
                return None

        self._mark_api_unavailable()
        return None

    def _fetch_klines_api(
        self,
        symbol: str,
        interval: str,
        limit: int,
    ) -> Optional[List[List]]:
        """
        Fetch klines from Binance candle endpoint.

        GET /api/v3/klines?symbol=BTCUSDT&interval=5m&limit=150

        Binance kline format:
            [
                open_time,       # 0: int ms
                open,            # 1: str
                high,            # 2: str
                low,             # 3: str
                close,           # 4: str
                volume,          # 5: str
                close_time,      # 6: int ms
                quote_volume,    # 7: str
                trade_count,     # 8: int
                taker_buy_base,  # 9: str
                taker_buy_quote, # 10: str
                ignore           # 11: str
            ]

        Returns:
            List of kline arrays in Binance format, or None on failure
        """
        if not REQUESTS_AVAILABLE:
            return None

        # Cap limit at Binance max
        limit = min(limit, 1000)

        for attempt in range(self.MAX_RETRIES):
            try:
                resp = requests.get(
                    f"{self.base_url}/api/v3/klines",
                    params={
                        "symbol": symbol,
                        "interval": interval,
                        "limit": limit,
                    },
                    timeout=self.REQUEST_TIMEOUT_SEC,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and len(data) > 0:
                        return data

                elif resp.status_code == 400:
                    logger.error(
                        f"❌ Invalid kline request: {symbol} {interval} | "
                        f"{resp.text[:100]}"
                    )
                    return None

                elif resp.status_code == 429:
                    logger.warning(
                        f"⚠️ Rate limited on klines | "
                        f"Attempt {attempt + 1}/{self.MAX_RETRIES}"
                    )
                    time.sleep(2 ** attempt)

                else:
                    logger.warning(
                        f"⚠️ Klines HTTP {resp.status_code} | "
                        f"{symbol} {interval}"
                    )

            except requests.exceptions.Timeout:
                logger.warning(
                    f"⚠️ Klines fetch timeout | {symbol} | "
                    f"Attempt {attempt + 1}/{self.MAX_RETRIES}"
                )
                time.sleep(1)

            except requests.exceptions.ConnectionError:
                logger.warning(
                    f"⚠️ Connection error fetching klines | {symbol}"
                )
                self._mark_api_unavailable()
                return None

            except Exception as e:
                logger.error(f"❌ Klines fetch error: {e}")
                return None

        self._mark_api_unavailable()
        return None

    # ═════════════════════════════════════════════════════
    #  API AVAILABILITY TRACKING
    # ═════════════════════════════════════════════════════

    def _should_try_api(self) -> bool:
        """
        Check if we should attempt an API call.

        After a failure, backs off for _failure_backoff_sec
        before trying again. Prevents hammering a down API.
        """
        if not REQUESTS_AVAILABLE:
            return False

        if self._api_available:
            return True

        # Check if backoff period has passed
        if self._last_failure:
            elapsed = time.time() - self._last_failure
            if elapsed >= self._failure_backoff_sec:
                logger.info(
                    f"🔄 Retrying Binance API after "
                    f"{elapsed:.0f}s backoff..."
                )
                self._api_available = True
                return True

        return False

    def _mark_api_unavailable(self) -> None:
        """Mark API as temporarily unavailable and record failure time."""
        self._api_available = False
        self._last_failure = time.time()
        logger.warning(
            f"⚠️ Binance API marked unavailable | "
            f"Will retry in {self._failure_backoff_sec}s"
        )

    # ═════════════════════════════════════════════════════
    #  MOCK DATA FALLBACK (Geometric Brownian Motion)
    # ═════════════════════════════════════════════════════

    def _mock_price(self, symbol: str) -> float:
        """
        Generate realistic mock price via GBM.

        Used only when Binance API is unreachable.
        Starts from SEED_PRICES and drifts realistically.
        """
        base = SEED_PRICES.get(symbol, DEFAULT_SEED_PRICE)

        # GBM step
        mu = 0.00005
        sigma = 0.0012
        z = random.gauss(0, 1)
        price = base * math.exp(
            (mu - 0.5 * sigma ** 2) + sigma * z
        )
        price = round(max(price, 1e-8), 8)

        logger.debug(f"🎲 Mock price | {symbol} = ${price:.4f}")
        return price

    def _mock_klines(self, symbol: str, limit: int) -> List[List]:
        """
        Generate realistic mock OHLCV candles via GBM.

        Returns Binance-compatible kline format.
        Used only when Binance API is unreachable.
        """
        seed = SEED_PRICES.get(symbol, DEFAULT_SEED_PRICE)
        candles = []

        mu = 0.00005
        sigma = 0.0015
        now_ms = int(time.time() * 1000)
        interval_ms = 5 * 60 * 1000  # 5 minutes in ms

        price = seed

        for i in range(limit):
            # GBM price step
            z = random.gauss(0, 1)
            open_p = price * (1 + random.gauss(0, 0.0003))
            close_p = open_p * math.exp(
                (mu - 0.5 * sigma ** 2) + sigma * z
            )

            open_p = max(open_p, 1e-8)
            close_p = max(close_p, 1e-8)

            candle_range = abs(close_p - open_p)
            wick = max(candle_range * 0.5, price * 0.0002)

            high_p = max(open_p, close_p) + random.uniform(0, wick)
            low_p = max(
                min(open_p, close_p) - random.uniform(0, wick),
                1e-8
            )

            base_vol = max(1.0, 50000.0 / max(price, 0.01))
            volume = base_vol * random.uniform(0.6, 1.4)

            open_time = now_ms - (limit - i) * interval_ms
            close_time = open_time + interval_ms - 1

            # Binance kline format
            candles.append([
                open_time,                   # 0: open time (ms)
                str(round(open_p, 8)),       # 1: open
                str(round(high_p, 8)),       # 2: high
                str(round(low_p, 8)),        # 3: low
                str(round(close_p, 8)),      # 4: close
                str(round(volume, 2)),       # 5: volume
                close_time,                  # 6: close time (ms)
                str(round(volume * close_p, 2)),  # 7: quote volume
                random.randint(100, 1000),   # 8: trade count
                str(round(volume * 0.5, 2)),  # 9: taker buy base
                str(round(volume * 0.5 * close_p, 2)),  # 10: taker buy quote
                "0",                         # 11: ignore
            ])

            price = close_p

        logger.debug(
            f"🎲 Mock candles generated | {symbol} | {len(candles)} candles"
        )
        return candles

    # ═════════════════════════════════════════════════════
    #  CACHE MANAGEMENT
    # ═════════════════════════════════════════════════════

    def clear_cache(self, symbol: str = None) -> None:
        """
        Clear price and candle caches.

        Args:
            symbol: If provided, clear only this symbol's cache.
                    If None, clear all caches.
        """
        if symbol:
            norm = _normalize_symbol(symbol)
            self._price_cache.pop(norm, None)

            # Clear all candle cache keys for this symbol
            keys_to_remove = [
                k for k in self._candle_cache
                if k.startswith(norm)
            ]
            for k in keys_to_remove:
                self._candle_cache.pop(k, None)

            logger.debug(f"🗑️ Cache cleared for {norm}")
        else:
            self._price_cache.clear()
            self._candle_cache.clear()
            logger.debug("🗑️ All caches cleared")

    # ═════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═════════════════════════════════════════════════════

    def __repr__(self) -> str:
        status = "🟢 Online" if self._api_available else "🔴 Offline"
        return (
            f"<BinanceMarketClient "
            f"{status} | "
            f"URL={self.base_url} | "
            f"PriceCached={len(self._price_cache)} | "
            f"CandleCached={len(self._candle_cache)}>"
        )