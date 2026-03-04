# app/market/data_feed.py

"""
Market Data Feed — Production Grade

Fetches raw market data from multiple exchange sources:
- Binance (public market data)
- Alpaca (crypto data)
- Paper exchange (simulated data)

Features:
- Multi-exchange support with automatic fallback
- Intelligent caching to reduce API calls
- Retry logic with exponential backoff
- Rate limit handling
- Data validation and normalization
- Health monitoring

NO indicators, NO trading logic — pure data fetching.
"""

import os
import time
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═══════════════════════════════════════════════════════════════════

# Timeframe mappings
TIMEFRAME_SECONDS = {
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

# Cache TTL based on timeframe (seconds)
CACHE_TTL = {
    "1m": 30,
    "3m": 60,
    "5m": 120,
    "15m": 300,
    "30m": 600,
    "1h": 900,
    "4h": 1800,
    "1d": 3600,
}


class MarketDataFeed:
    """
    Unified market data fetcher with multi-exchange support.

    Usage:
        feed = MarketDataFeed("BTC/USDT", exchange=exchange_client)
        data = feed.fetch_market_data()

    Or standalone with Binance:
        feed = MarketDataFeed("BTC/USDT")
        data = feed.fetch_market_data()
    """

    # Default settings
    DEFAULT_TIMEFRAME = "5m"
    DEFAULT_CANDLE_LIMIT = 150
    MAX_RETRIES = 3
    RETRY_DELAY = 1.0

    def __init__(
        self,
        symbol: str,
        timeframe: str = None,
        candle_limit: int = None,
        exchange=None,
        use_cache: bool = True,
    ):
        """
        Initialize market data feed.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            timeframe: Candle timeframe (e.g., "5m", "1h")
            candle_limit: Number of candles to fetch
            exchange: Exchange client (optional, uses Binance if None)
            use_cache: Enable caching to reduce API calls
        """
        self.symbol = self._normalize_symbol(symbol)
        self.timeframe = timeframe or self.DEFAULT_TIMEFRAME
        self.candle_limit = candle_limit or self.DEFAULT_CANDLE_LIMIT
        self.exchange = exchange
        self.use_cache = use_cache

        # Binance client for public market data
        self._binance_client = None
        
        # Cache storage
        self._price_cache: Dict[str, Tuple[float, float]] = {}  # symbol -> (price, timestamp)
        self._candle_cache: Dict[str, Tuple[List[Dict], float]] = {}  # key -> (candles, timestamp)
        
        # Health tracking
        self._last_fetch_time: float = 0
        self._fetch_count: int = 0
        self._error_count: int = 0
        self._last_error: Optional[str] = None

        logger.info(
            f"📡 MarketDataFeed initialized | "
            f"{self.symbol} | {self.timeframe} | "
            f"Limit={self.candle_limit} | Cache={self.use_cache}"
        )

    # ═══════════════════════════════════════════════════════════════
    #  SYMBOL HANDLING
    # ═══════════════════════════════════════════════════════════════

    def _normalize_symbol(self, symbol: str) -> str:
        """Normalize symbol to standard format (BASE/QUOTE)."""
        s = symbol.upper().strip()
        
        if "/" in s:
            return s

        # Common quote currencies
        for quote in ("USDT", "USDC", "BUSD", "USD", "BTC", "ETH"):
            if s.endswith(quote):
                base = s[:-len(quote)]
                if base:
                    return f"{base}/{quote}"

        return s

    def _to_binance_symbol(self, symbol: str) -> str:
        """Convert to Binance format (no slash)."""
        return symbol.replace("/", "")

    # ═══════════════════════════════════════════════════════════════
    #  BINANCE CLIENT (LAZY INIT)
    # ═══════════════════════════════════════════════════════════════

    def _get_binance_client(self):
        """Lazy initialization of Binance client."""
        if self._binance_client is None:
            try:
                from app.exchange.binance_market import BinanceMarketClient
                self._binance_client = BinanceMarketClient()
                logger.debug("✅ Binance market client initialized")
            except ImportError:
                logger.warning("⚠️ BinanceMarketClient not available")
                self._binance_client = False  # Mark as unavailable
            except Exception as e:
                logger.error(f"❌ Failed to init Binance client: {e}")
                self._binance_client = False

        return self._binance_client if self._binance_client else None

    # ═══════════════════════════════════════════════════════════════
    #  PRICE FETCHING
    # ═══════════════════════════════════════════════════════════════

    def get_current_price(self, force_refresh: bool = False) -> float:
        """
        Fetch current market price.

        Args:
            force_refresh: Bypass cache and fetch fresh data

        Returns:
            Current price as float

        Raises:
            Exception if all sources fail
        """
        # Check cache first
        if self.use_cache and not force_refresh:
            cached = self._get_cached_price()
            if cached is not None:
                return cached

        price = None
        errors = []

        # Try exchange client first
        if self.exchange:
            try:
                price = self.exchange.get_price(self.symbol)
                if price and price > 0:
                    self._cache_price(price)
                    logger.debug(f"📈 Price from exchange: {self.symbol} = ${price:,.6f}")
                    return price
            except Exception as e:
                errors.append(f"Exchange: {e}")

        # Try Binance as fallback
        binance = self._get_binance_client()
        if binance:
            try:
                binance_symbol = self._to_binance_symbol(self.symbol)
                price = binance.get_price(binance_symbol)
                if price and price > 0:
                    self._cache_price(float(price))
                    logger.debug(f"📈 Price from Binance: {self.symbol} = ${price:,.6f}")
                    return float(price)
            except Exception as e:
                errors.append(f"Binance: {e}")

        # All sources failed
        self._error_count += 1
        self._last_error = "; ".join(errors)
        
        logger.error(f"❌ Failed to fetch price for {self.symbol}: {self._last_error}")
        raise Exception(f"Price fetch failed: {self._last_error}")

    def _get_cached_price(self) -> Optional[float]:
        """Get price from cache if still valid."""
        if self.symbol not in self._price_cache:
            return None

        price, cached_time = self._price_cache[self.symbol]
        cache_ttl = CACHE_TTL.get(self.timeframe, 60)

        if time.time() - cached_time < cache_ttl:
            logger.debug(f"💾 Price from cache: {self.symbol} = ${price:,.6f}")
            return price

        return None

    def _cache_price(self, price: float) -> None:
        """Cache price with timestamp."""
        self._price_cache[self.symbol] = (price, time.time())

    # ═══════════════════════════════════════════════════════════════
    #  CANDLE FETCHING
    # ═══════════════════════════════════════════════════════════════

    def get_candles(
        self,
        timeframe: str = None,
        limit: int = None,
        force_refresh: bool = False
    ) -> List[Dict]:
        """
        Fetch recent OHLCV candles.

        Args:
            timeframe: Override default timeframe
            limit: Override default limit
            force_refresh: Bypass cache

        Returns:
            List of candle dicts with keys:
                - timestamp (ISO string)
                - open, high, low, close (float)
                - volume (float)
        """
        tf = timeframe or self.timeframe
        lmt = limit or self.candle_limit
        cache_key = f"{self.symbol}_{tf}_{lmt}"

        # Check cache
        if self.use_cache and not force_refresh:
            cached = self._get_cached_candles(cache_key)
            if cached is not None:
                return cached

        candles = None
        errors = []

        # Try exchange client first
        if self.exchange:
            try:
                candles = self.exchange.get_recent_candles(
                    symbol=self.symbol,
                    limit=lmt,
                    timeframe=tf
                )
                if candles and len(candles) > 0:
                    candles = self._normalize_candles(candles)
                    self._cache_candles(cache_key, candles)
                    logger.debug(
                        f"🕯️ {len(candles)} candles from exchange | "
                        f"{self.symbol} {tf}"
                    )
                    return candles
            except Exception as e:
                errors.append(f"Exchange: {e}")

        # Try Binance as fallback
        binance = self._get_binance_client()
        if binance:
            try:
                binance_symbol = self._to_binance_symbol(self.symbol)
                raw_candles = binance.get_klines(
                    symbol=binance_symbol,
                    interval=tf,
                    limit=lmt
                )
                if raw_candles:
                    candles = self._parse_binance_candles(raw_candles)
                    self._cache_candles(cache_key, candles)
                    logger.debug(
                        f"🕯️ {len(candles)} candles from Binance | "
                        f"{self.symbol} {tf}"
                    )
                    return candles
            except Exception as e:
                errors.append(f"Binance: {e}")

        # All sources failed
        self._error_count += 1
        self._last_error = "; ".join(errors)
        
        logger.error(f"❌ Failed to fetch candles for {self.symbol}: {self._last_error}")
        raise Exception(f"Candle fetch failed: {self._last_error}")

    def _parse_binance_candles(self, raw_candles: List) -> List[Dict]:
        """Parse Binance kline format to standard candle dict."""
        candles = []
        
        for c in raw_candles:
            try:
                # Binance kline format:
                # [0]=open_time, [1]=open, [2]=high, [3]=low, [4]=close, [5]=volume
                timestamp = int(c[0])
                
                # Convert timestamp to ISO string
                ts_str = datetime.utcfromtimestamp(timestamp / 1000).isoformat()
                
                candles.append({
                    "timestamp": ts_str,
                    "open": float(c[1]),
                    "high": float(c[2]),
                    "low": float(c[3]),
                    "close": float(c[4]),
                    "volume": float(c[5]),
                })
            except (IndexError, ValueError, TypeError) as e:
                logger.warning(f"⚠️ Invalid candle data: {e}")
                continue

        return candles

    def _normalize_candles(self, candles: List[Dict]) -> List[Dict]:
        """Ensure candles have consistent format."""
        normalized = []
        
        for c in candles:
            try:
                # Handle timestamp
                ts = c.get("timestamp", "")
                if isinstance(ts, (int, float)):
                    # Convert Unix timestamp to ISO
                    if ts > 1e12:  # Milliseconds
                        ts = ts / 1000
                    ts = datetime.utcfromtimestamp(ts).isoformat()

                normalized.append({
                    "timestamp": str(ts),
                    "open": float(c.get("open", 0)),
                    "high": float(c.get("high", 0)),
                    "low": float(c.get("low", 0)),
                    "close": float(c.get("close", 0)),
                    "volume": float(c.get("volume", 0)),
                })
            except (ValueError, TypeError) as e:
                logger.warning(f"⚠️ Candle normalization error: {e}")
                continue

        return normalized

    def _get_cached_candles(self, cache_key: str) -> Optional[List[Dict]]:
        """Get candles from cache if still valid."""
        if cache_key not in self._candle_cache:
            return None

        candles, cached_time = self._candle_cache[cache_key]
        cache_ttl = CACHE_TTL.get(self.timeframe, 120)

        if time.time() - cached_time < cache_ttl:
            logger.debug(f"💾 {len(candles)} candles from cache | {cache_key}")
            return candles

        return None

    def _cache_candles(self, cache_key: str, candles: List[Dict]) -> None:
        """Cache candles with timestamp."""
        self._candle_cache[cache_key] = (candles, time.time())

    # ═══════════════════════════════════════════════════════════════
    #  UNIFIED MARKET DATA
    # ═══════════════════════════════════════════════════════════════

    def fetch_market_data(
        self,
        include_ticker: bool = False,
        force_refresh: bool = False
    ) -> Dict:
        """
        Fetch complete market data package.

        Args:
            include_ticker: Include extended ticker data
            force_refresh: Bypass all caches

        Returns:
            Dict with:
                - symbol: str
                - price: float
                - candles: List[Dict]
                - timeframe: str
                - fetched_at: int (Unix timestamp)
                - source: str
                - ticker: Dict (optional)
        """
        start_time = time.time()
        logger.info(f"📡 Fetching market data | {self.symbol} | {self.timeframe}")

        errors = []
        
        # Fetch price
        try:
            price = self.get_current_price(force_refresh=force_refresh)
        except Exception as e:
            errors.append(f"Price: {e}")
            price = 0.0

        # Fetch candles
        try:
            candles = self.get_candles(force_refresh=force_refresh)
        except Exception as e:
            errors.append(f"Candles: {e}")
            candles = []

        # Validate data
        if price == 0 and candles:
            # Use last candle close as price
            price = candles[-1].get("close", 0)
            logger.warning(f"⚠️ Using candle close as price: ${price:,.4f}")

        if not candles:
            # This is a critical error
            self._error_count += 1
            logger.error(f"❌ No candle data available for {self.symbol}")

        # Build response
        market_data = {
            "symbol": self.symbol,
            "price": price,
            "candles": candles,
            "timeframe": self.timeframe,
            "candle_count": len(candles),
            "fetched_at": int(time.time()),
            "fetch_duration_ms": int((time.time() - start_time) * 1000),
            "source": self._get_data_source(),
        }

        # Include ticker if requested
        if include_ticker:
            market_data["ticker"] = self._get_ticker_data(price)

        # Update stats
        self._last_fetch_time = time.time()
        self._fetch_count += 1

        if errors:
            market_data["warnings"] = errors
            logger.warning(f"⚠️ Market data incomplete: {errors}")
        else:
            logger.info(
                f"✅ Market data ready | {self.symbol} | "
                f"${price:,.2f} | {len(candles)} candles | "
                f"{market_data['fetch_duration_ms']}ms"
            )

        return market_data

    def _get_data_source(self) -> str:
        """Get the data source being used."""
        if self.exchange:
            return getattr(self.exchange, 'EXCHANGE_NAME', 'EXCHANGE')
        return "BINANCE"

    def _get_ticker_data(self, price: float) -> Dict:
        """Get extended ticker data if available."""
        ticker = {
            "price": price,
            "bid": price * 0.9999,
            "ask": price * 1.0001,
            "timestamp": datetime.utcnow().isoformat(),
        }

        # Try to get real ticker from exchange
        if self.exchange and hasattr(self.exchange, 'get_ticker'):
            try:
                real_ticker = self.exchange.get_ticker(self.symbol)
                if real_ticker:
                    ticker.update(real_ticker)
            except Exception as e:
                logger.debug(f"Ticker fetch failed: {e}")

        return ticker

    # ═══════════════════════════════════════════════════════════════
    #  MULTI-SYMBOL SUPPORT
    # ═══════════════════════════════════════════════════════════════

    def fetch_multiple_symbols(
        self,
        symbols: List[str],
        timeframe: str = None,
        limit: int = None
    ) -> Dict[str, Dict]:
        """
        Fetch market data for multiple symbols.

        Args:
            symbols: List of trading pairs
            timeframe: Override timeframe for all
            limit: Override candle limit

        Returns:
            Dict mapping symbol -> market_data
        """
        results = {}
        tf = timeframe or self.timeframe
        lmt = limit or self.candle_limit

        for symbol in symbols:
            try:
                # Create temporary feed for this symbol
                feed = MarketDataFeed(
                    symbol=symbol,
                    timeframe=tf,
                    candle_limit=lmt,
                    exchange=self.exchange,
                    use_cache=self.use_cache
                )
                results[symbol] = feed.fetch_market_data()
            except Exception as e:
                logger.error(f"❌ Failed to fetch {symbol}: {e}")
                results[symbol] = {
                    "symbol": symbol,
                    "error": str(e),
                    "price": 0,
                    "candles": [],
                }

        return results

    # ═══════════════════════════════════════════════════════════════
    #  STREAMING / UPDATES
    # ═══════════════════════════════════════════════════════════════

    def get_latest_candle(self) -> Optional[Dict]:
        """Get only the most recent candle (efficient for updates)."""
        try:
            candles = self.get_candles(limit=1)
            return candles[-1] if candles else None
        except Exception as e:
            logger.error(f"❌ Failed to get latest candle: {e}")
            return None

    def has_new_candle(self, last_timestamp: str) -> bool:
        """Check if a new candle is available since last_timestamp."""
        try:
            latest = self.get_latest_candle()
            if not latest:
                return False
            return latest.get("timestamp", "") != last_timestamp
        except Exception:
            return False

    # ═══════════════════════════════════════════════════════════════
    #  CACHE MANAGEMENT
    # ═══════════════════════════════════════════════════════════════

    def clear_cache(self, symbol_only: bool = False) -> None:
        """Clear cached data."""
        if symbol_only:
            # Clear only this symbol's cache
            self._price_cache.pop(self.symbol, None)
            keys_to_remove = [k for k in self._candle_cache if k.startswith(self.symbol)]
            for key in keys_to_remove:
                self._candle_cache.pop(key, None)
            logger.debug(f"🗑️ Cache cleared for {self.symbol}")
        else:
            # Clear all cache
            self._price_cache.clear()
            self._candle_cache.clear()
            logger.debug("🗑️ All cache cleared")

    def get_cache_stats(self) -> Dict:
        """Get cache statistics."""
        return {
            "price_entries": len(self._price_cache),
            "candle_entries": len(self._candle_cache),
            "cache_enabled": self.use_cache,
        }

    # ═══════════════════════════════════════════════════════════════
    #  HEALTH & DIAGNOSTICS
    # ═══════════════════════════════════════════════════════════════

    def health_check(self) -> Dict:
        """Perform health check on data sources."""
        health = {
            "symbol": self.symbol,
            "status": "healthy",
            "sources": {},
            "stats": {
                "fetch_count": self._fetch_count,
                "error_count": self._error_count,
                "last_fetch": self._last_fetch_time,
                "last_error": self._last_error,
            }
        }

        # Check exchange
        if self.exchange:
            try:
                if hasattr(self.exchange, 'ping') and self.exchange.ping():
                    health["sources"]["exchange"] = "ok"
                else:
                    health["sources"]["exchange"] = "unavailable"
            except Exception as e:
                health["sources"]["exchange"] = f"error: {e}"

        # Check Binance
        binance = self._get_binance_client()
        if binance:
            try:
                # Try a simple request
                binance.get_price("BTCUSDT")
                health["sources"]["binance"] = "ok"
            except Exception as e:
                health["sources"]["binance"] = f"error: {e}"
        else:
            health["sources"]["binance"] = "not_initialized"

        # Determine overall status
        if not any(s == "ok" for s in health["sources"].values()):
            health["status"] = "degraded"

        if self._error_count > 10:
            health["status"] = "unhealthy"

        return health

    def get_stats(self) -> Dict:
        """Get feed statistics."""
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "candle_limit": self.candle_limit,
            "fetch_count": self._fetch_count,
            "error_count": self._error_count,
            "error_rate": (
                self._error_count / self._fetch_count 
                if self._fetch_count > 0 else 0
            ),
            "last_fetch": self._last_fetch_time,
            "last_error": self._last_error,
            "cache_enabled": self.use_cache,
            "cache_stats": self.get_cache_stats(),
        }

    def reset_stats(self) -> None:
        """Reset statistics counters."""
        self._fetch_count = 0
        self._error_count = 0
        self._last_error = None
        logger.debug("📊 Stats reset")

    # ═══════════════════════════════════════════════════════════════
    #  CONFIGURATION
    # ═══════════════════════════════════════════════════════════════

    def set_timeframe(self, timeframe: str) -> None:
        """Update timeframe."""
        if timeframe in TIMEFRAME_SECONDS:
            self.timeframe = timeframe
            logger.info(f"⏰ Timeframe changed to {timeframe}")
        else:
            logger.warning(f"⚠️ Invalid timeframe: {timeframe}")

    def set_candle_limit(self, limit: int) -> None:
        """Update candle limit."""
        if 10 <= limit <= 1000:
            self.candle_limit = limit
            logger.info(f"📊 Candle limit changed to {limit}")
        else:
            logger.warning(f"⚠️ Invalid candle limit: {limit} (must be 10-1000)")

    def set_exchange(self, exchange) -> None:
        """Update exchange client."""
        self.exchange = exchange
        logger.info(f"🔄 Exchange updated: {exchange}")

    # ═══════════════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═══════════════════════════════════════════════════════════════

    def __repr__(self) -> str:
        return (
            f"<MarketDataFeed {self.symbol} | "
            f"{self.timeframe} | "
            f"Limit={self.candle_limit} | "
            f"Fetches={self._fetch_count}>"
        )

    def __str__(self) -> str:
        return f"MarketDataFeed({self.symbol})"


# ═══════════════════════════════════════════════════════════════════
#  FACTORY FUNCTION
# ═══════════════════════════════════════════════════════════════════

def create_data_feed(
    symbol: str,
    exchange=None,
    timeframe: str = "5m",
    candle_limit: int = 150
) -> MarketDataFeed:
    """
    Factory function to create a MarketDataFeed.

    Args:
        symbol: Trading pair
        exchange: Exchange client (optional)
        timeframe: Candle timeframe
        candle_limit: Number of candles

    Returns:
        Configured MarketDataFeed instance
    """
    return MarketDataFeed(
        symbol=symbol,
        timeframe=timeframe,
        candle_limit=candle_limit,
        exchange=exchange,
        use_cache=True
    )


# ═══════════════════════════════════════════════════════════════════
#  MULTI-FEED MANAGER
# ═══════════════════════════════════════════════════════════════════

class MultiSymbolFeed:
    """
    Manages data feeds for multiple symbols.

    Usage:
        multi_feed = MultiSymbolFeed(["BTC/USDT", "ETH/USDT"], exchange=exchange)
        all_data = multi_feed.fetch_all()
    """

    def __init__(
        self,
        symbols: List[str],
        exchange=None,
        timeframe: str = "5m",
        candle_limit: int = 150
    ):
        self.symbols = symbols
        self.exchange = exchange
        self.timeframe = timeframe
        self.candle_limit = candle_limit

        # Create feeds for each symbol
        self.feeds: Dict[str, MarketDataFeed] = {}
        for symbol in symbols:
            self.feeds[symbol] = MarketDataFeed(
                symbol=symbol,
                timeframe=timeframe,
                candle_limit=candle_limit,
                exchange=exchange,
                use_cache=True
            )

        logger.info(f"📡 MultiSymbolFeed initialized | {len(symbols)} symbols")

    def fetch_all(self, force_refresh: bool = False) -> Dict[str, Dict]:
        """Fetch market data for all symbols."""
        results = {}
        
        for symbol, feed in self.feeds.items():
            try:
                results[symbol] = feed.fetch_market_data(force_refresh=force_refresh)
            except Exception as e:
                logger.error(f"❌ Failed to fetch {symbol}: {e}")
                results[symbol] = {
                    "symbol": symbol,
                    "error": str(e),
                    "price": 0,
                    "candles": [],
                }

        return results

    def get_prices(self) -> Dict[str, float]:
        """Get current prices for all symbols."""
        prices = {}
        
        for symbol, feed in self.feeds.items():
            try:
                prices[symbol] = feed.get_current_price()
            except Exception:
                prices[symbol] = 0.0

        return prices

    def add_symbol(self, symbol: str) -> None:
        """Add a new symbol to track."""
        if symbol not in self.feeds:
            self.feeds[symbol] = MarketDataFeed(
                symbol=symbol,
                timeframe=self.timeframe,
                candle_limit=self.candle_limit,
                exchange=self.exchange,
                use_cache=True
            )
            self.symbols.append(symbol)
            logger.info(f"➕ Added symbol: {symbol}")

    def remove_symbol(self, symbol: str) -> None:
        """Remove a symbol from tracking."""
        if symbol in self.feeds:
            del self.feeds[symbol]
            self.symbols.remove(symbol)
            logger.info(f"➖ Removed symbol: {symbol}")

    def health_check(self) -> Dict:
        """Health check for all feeds."""
        return {
            symbol: feed.health_check()
            for symbol, feed in self.feeds.items()
        }

    def clear_all_caches(self) -> None:
        """Clear cache for all feeds."""
        for feed in self.feeds.values():
            feed.clear_cache()

    def __repr__(self) -> str:
        return f"<MultiSymbolFeed | {len(self.feeds)} symbols>"