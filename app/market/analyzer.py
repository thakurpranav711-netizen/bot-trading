# app/market/analyzer.py

"""
Market Analyzer — Production Grade

Computes all technical indicators and market state for the 4-Brain system:
- Core indicators: RSI, EMA (20/50/200), MACD, Bollinger Bands, ATR
- Trend detection and regime classification
- Volatility analysis (normalized and categorical)
- Volume analysis (pressure, spikes)
- Momentum metrics (strength, acceleration)
- Market structure (support/resistance, breakouts)
- Confidence scoring

Brain Feed Fields:
- indicators: Dict for Brain1 (technical signals)
- sentiment_score: Float for Brain2 (-1 to +1)
- chart_pattern: Dict for Brain3 (pattern recognition)
- ai_prediction: Dict for Brain4 (populated externally)

Performance Optimizations:
- O(n) MACD calculation (fixed from O(n²))
- Cached intermediate calculations
- Efficient numpy-free pure Python (no dependencies)
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from statistics import mean, stdev
from math import sqrt, log
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═════════════════════════════════════════════════════════════════
#  MARKET STATE DATA CLASS
# ═════════════════════════════════════════════════════════════════

@dataclass
class MarketState:
    """
    Complete market snapshot consumed by strategy and controller.

    All fields are pre-computed by MarketAnalyzer.analyze().
    Strategy and controller should treat this as read-only.
    """

    # ── Core ──────────────────────────────────────────────────────
    symbol: str
    price: float
    timestamp: str = ""

    # ── Trend / Regime ────────────────────────────────────────────
    trend: str = "sideways"          # "bullish", "bearish", "sideways"
    regime: str = "ranging"          # "trending", "ranging", "explosive"
    sentiment: str = "neutral"       # "strong_bullish", "weak_bullish", etc.

    # ── Classic Indicators ────────────────────────────────────────
    rsi: float = 50.0
    ema_20: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0

    # ── MACD ──────────────────────────────────────────────────────
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0

    # ── Bollinger Bands ───────────────────────────────────────────
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_mid: float = 0.0
    bb_width: float = 0.0
    bb_percent_b: float = 0.5        # Position within bands (0-1)

    # ── Volatility / ATR ──────────────────────────────────────────
    atr: float = 0.0
    volatility: float = 0.0          # 0-1 normalized for volatility_guard
    volatility_pct: float = 0.0      # ATR / price
    volatility_regime: str = "normal"  # "low", "normal", "high", "extreme"

    # ── Volume ────────────────────────────────────────────────────
    volume_spike: bool = False
    volume_pressure: float = 0.0     # -1 (bearish) to +1 (bullish)
    volume_sma: float = 0.0

    # ── Momentum ──────────────────────────────────────────────────
    momentum_strength: float = 0.0
    momentum_acceleration: float = 0.0
    trend_strength: float = 0.0

    # ── Market Structure ──────────────────────────────────────────
    structure_break: bool = False
    liquidity_score: float = 0.5
    support_level: float = 0.0
    resistance_level: float = 0.0

    # ── Confidence ────────────────────────────────────────────────
    confidence_score: float = 0.5

    # ── Brain Feed Fields ─────────────────────────────────────────
    # Brain 1: Technical indicators dict
    indicators: Dict = field(default_factory=dict)

    # Brain 2: Sentiment score (-1.0 to +1.0)
    sentiment_score: float = 0.0

    # Brain 3: Chart pattern recognition
    chart_pattern: Optional[Dict] = None

    # Brain 4: AI prediction (populated externally by ML module)
    ai_prediction: Optional[Dict] = None


# ═════════════════════════════════════════════════════════════════
#  MARKET ANALYZER
# ═════════════════════════════════════════════════════════════════

class MarketAnalyzer:
    """
    Stateless market analyzer.

    Call analyze() with market_data dict containing:
        - price: float
        - candles: List[Dict] with open, high, low, close, volume

    Returns fully populated MarketState.
    """

    # Minimum candles required for analysis
    MIN_CANDLES = 60

    def __init__(self, symbol: str):
        self.symbol = symbol

    # ═════════════════════════════════════════════════════
    #  MAIN ANALYSIS
    # ═════════════════════════════════════════════════════

    def analyze(self, market_data: Dict) -> MarketState:
        """
        Perform complete market analysis.

        Args:
            market_data: Dict with 'price' and 'candles' keys

        Returns:
            Fully populated MarketState

        Raises:
            ValueError: If insufficient candle data
        """
        candles = market_data.get("candles", [])
        price = float(market_data.get("price", 0))

        if len(candles) < self.MIN_CANDLES:
            raise ValueError(
                f"❌ Insufficient candles: {len(candles)} < {self.MIN_CANDLES} required"
            )

        # ── Extract OHLCV arrays ──────────────────────────────────
        opens = [float(c["open"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        closes = [float(c["close"]) for c in candles]
        volumes = [float(c["volume"]) for c in candles]

        # ── Core Indicators ───────────────────────────────────────
        ema_20 = self._ema(closes, 20)
        ema_50 = self._ema(closes, 50)
        ema_200 = self._ema(closes, min(200, len(closes)))
        rsi = self._rsi_wilder(closes, 14)
        atr = self._atr(highs, lows, closes, 14)

        # ── MACD (O(n) optimized) ─────────────────────────────────
        macd_line, macd_signal, macd_hist = self._macd_optimized(closes)

        # ── Bollinger Bands ───────────────────────────────────────
        bb_upper, bb_mid, bb_lower, bb_width = self._bollinger(closes, 20, 2.0)
        bb_percent_b = self._bollinger_percent_b(price, bb_upper, bb_lower)

        # ── Volatility ────────────────────────────────────────────
        volatility_pct = atr / price if price > 0 else 0
        volatility_regime = self._classify_volatility(volatility_pct)
        volatility_normalized = self._normalize_volatility(closes, 20)

        # ── Trend / Regime ────────────────────────────────────────
        trend = self._classify_trend(ema_20, ema_50, ema_200)
        trend_strength = abs(ema_20 - ema_50) / price if price > 0 else 0
        regime = self._classify_regime(volatility_regime, trend)
        sentiment = self._classify_sentiment(trend, rsi)

        # ── Momentum ──────────────────────────────────────────────
        momentum_strength = self._momentum_strength(closes)
        momentum_acceleration = self._momentum_acceleration(closes)

        # ── Volume ────────────────────────────────────────────────
        volume_sma = mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes)
        volume_spike = volumes[-1] > volume_sma * 1.5 if volumes else False
        volume_pressure = self._volume_pressure(candles[-20:])

        # ── Market Structure ──────────────────────────────────────
        structure_break = self._detect_structure_break(closes)
        liquidity_score = self._liquidity_score(volumes)
        support, resistance = self._support_resistance(highs, lows, 30)

        # ── Confidence Score ──────────────────────────────────────
        confidence_score = self._compute_confidence(
            trend_strength, momentum_strength, volume_pressure, liquidity_score
        )

        # ── Brain Feeds ───────────────────────────────────────────
        indicators = self._build_indicators_dict(
            rsi=rsi,
            macd_line=macd_line,
            macd_signal=macd_signal,
            macd_hist=macd_hist,
            ema_20=ema_20,
            ema_50=ema_50,
            price=price,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            bb_mid=bb_mid,
        )

        sentiment_score = self._build_sentiment_score(
            trend=trend,
            rsi=rsi,
            macd_hist=macd_hist,
            volume_pressure=volume_pressure,
            momentum_accel=momentum_acceleration,
        )

        chart_pattern = self._detect_chart_pattern(
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volume_pressure=volume_pressure,
            structure_break=structure_break,
        )

        # ── Build MarketState ─────────────────────────────────────
        state = MarketState(
            symbol=self.symbol,
            price=price,
            timestamp=candles[-1].get("timestamp", "") if candles else "",

            # Trend
            trend=trend,
            regime=regime,
            sentiment=sentiment,

            # Indicators
            rsi=round(rsi, 2),
            ema_20=round(ema_20, 6),
            ema_50=round(ema_50, 6),
            ema_200=round(ema_200, 6),

            # MACD
            macd_line=round(macd_line, 8),
            macd_signal=round(macd_signal, 8),
            macd_histogram=round(macd_hist, 8),

            # Bollinger
            bb_upper=round(bb_upper, 6),
            bb_lower=round(bb_lower, 6),
            bb_mid=round(bb_mid, 6),
            bb_width=round(bb_width, 6),
            bb_percent_b=round(bb_percent_b, 4),

            # Volatility
            atr=round(atr, 8),
            volatility=round(volatility_normalized, 4),
            volatility_pct=round(volatility_pct, 6),
            volatility_regime=volatility_regime,

            # Volume
            volume_spike=volume_spike,
            volume_pressure=round(volume_pressure, 4),
            volume_sma=round(volume_sma, 2),

            # Momentum
            momentum_strength=round(momentum_strength, 6),
            momentum_acceleration=round(momentum_acceleration, 8),
            trend_strength=round(trend_strength, 6),

            # Structure
            structure_break=structure_break,
            liquidity_score=round(liquidity_score, 4),
            support_level=round(support, 6),
            resistance_level=round(resistance, 6),

            # Confidence
            confidence_score=round(confidence_score, 4),

            # Brain feeds
            indicators=indicators,
            sentiment_score=round(sentiment_score, 4),
            chart_pattern=chart_pattern,
            ai_prediction=None,
        )

        # ── Log summary ───────────────────────────────────────────
        logger.info(
            f"📊 {state.symbol} | ${price:.2f} | "
            f"Trend={trend} | RSI={rsi:.1f} | "
            f"MACD={'▲' if macd_hist > 0 else '▼'} | "
            f"Regime={regime} | Vol={volatility_regime} | "
            f"Conf={confidence_score * 100:.1f}%"
        )

        return state

    # ═════════════════════════════════════════════════════
    #  CORE INDICATORS
    # ═════════════════════════════════════════════════════

    def _ema(self, prices: List[float], period: int) -> float:
        """
        Exponential Moving Average.

        O(n) single-pass calculation.
        """
        if not prices:
            return 0.0

        period = min(period, len(prices))
        k = 2 / (period + 1)
        ema = prices[0]

        for price in prices[1:]:
            ema = price * k + ema * (1 - k)

        return ema

    def _ema_series(self, prices: List[float], period: int) -> List[float]:
        """
        Calculate EMA for entire series.

        Returns list of EMA values same length as input.
        O(n) single-pass.
        """
        if not prices:
            return []

        k = 2 / (period + 1)
        ema_values = [prices[0]]

        for i in range(1, len(prices)):
            ema = prices[i] * k + ema_values[-1] * (1 - k)
            ema_values.append(ema)

        return ema_values

    def _rsi_wilder(self, prices: List[float], period: int = 14) -> float:
        """
        RSI using Wilder's smoothing method.

        More accurate than simple average method.
        """
        if len(prices) < period + 1:
            return 50.0

        gains = []
        losses = []

        for i in range(1, len(prices)):
            delta = prices[i] - prices[i - 1]
            gains.append(max(delta, 0))
            losses.append(abs(min(delta, 0)))

        if len(gains) < period:
            return 50.0

        # Initial averages
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        # Wilder's smoothing
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _atr(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 14,
    ) -> float:
        """
        Average True Range.

        Measures volatility accounting for gaps.
        """
        if len(closes) < 2:
            return 0.0

        true_ranges = []

        for i in range(1, len(closes)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            true_ranges.append(tr)

        if not true_ranges:
            return 0.0

        return mean(true_ranges[-period:])

    # ═════════════════════════════════════════════════════
    #  MACD (OPTIMIZED O(n))
    # ═════════════════════════════════════════════════════

    def _macd_optimized(
        self,
        prices: List[float],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Tuple[float, float, float]:
        """
        MACD calculation — O(n) optimized.

        FIX: Previous implementation was O(n²) because it recalculated
        EMA from scratch for each point in the series.

        Now uses single-pass EMA series calculation.

        Returns:
            (macd_line, signal_line, histogram)
        """
        if len(prices) < slow:
            return 0.0, 0.0, 0.0

        # Calculate EMA series in O(n)
        ema_fast_series = self._ema_series(prices, fast)
        ema_slow_series = self._ema_series(prices, slow)

        # MACD line series
        macd_series = [
            ema_fast_series[i] - ema_slow_series[i]
            for i in range(len(prices))
        ]

        # Signal line (EMA of MACD line)
        # Only calculate from point where we have enough data
        signal_series = self._ema_series(macd_series[slow - 1:], signal)

        # Current values
        macd_line = macd_series[-1]
        signal_line = signal_series[-1] if signal_series else macd_line
        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    # ═════════════════════════════════════════════════════
    #  BOLLINGER BANDS
    # ═════════════════════════════════════════════════════

    def _bollinger(
        self,
        prices: List[float],
        period: int = 20,
        num_std: float = 2.0,
    ) -> Tuple[float, float, float, float]:
        """
        Bollinger Bands.

        Returns:
            (upper, middle, lower, width)
        """
        if len(prices) < period:
            period = len(prices)

        window = prices[-period:]
        mid = mean(window)
        std = stdev(window) if len(window) > 1 else 0

        upper = mid + num_std * std
        lower = mid - num_std * std
        width = (upper - lower) / mid if mid != 0 else 0

        return upper, mid, lower, width

    def _bollinger_percent_b(
        self, price: float, upper: float, lower: float
    ) -> float:
        """
        %B indicator: position within Bollinger Bands.

        0 = at lower band
        0.5 = at middle
        1 = at upper band
        Can be <0 or >1 if outside bands.
        """
        band_range = upper - lower
        if band_range == 0:
            return 0.5
        return (price - lower) / band_range

    # ═════════════════════════════════════════════════════
    #  VOLATILITY
    # ═════════════════════════════════════════════════════

    def _normalize_volatility(
        self, closes: List[float], period: int = 20
    ) -> float:
        """
        Normalized volatility (0-1) for volatility_guard.

        Uses annualized log-return standard deviation.
        """
        if len(closes) < period + 1:
            return 0.0

        log_returns = []
        for i in range(len(closes) - period, len(closes)):
            if closes[i - 1] > 0:
                log_returns.append(log(closes[i] / closes[i - 1]))

        if len(log_returns) < 2:
            return 0.0

        daily_std = stdev(log_returns)
        annualized = daily_std * sqrt(365)

        # Normalize to 0-1 range (cap at 1.0)
        return min(1.0, annualized)

    def _classify_volatility(self, volatility_pct: float) -> str:
        """Classify volatility into regime categories."""
        if volatility_pct > 0.02:
            return "extreme"
        elif volatility_pct > 0.01:
            return "high"
        elif volatility_pct < 0.005:
            return "low"
        return "normal"

    # ═════════════════════════════════════════════════════
    #  TREND / REGIME CLASSIFICATION
    # ═════════════════════════════════════════════════════

    def _classify_trend(
        self, ema_20: float, ema_50: float, ema_200: float
    ) -> str:
        """Classify trend based on EMA alignment."""
        if ema_20 > ema_50 > ema_200:
            return "bullish"
        elif ema_20 < ema_50 < ema_200:
            return "bearish"
        return "sideways"

    def _classify_regime(self, volatility_regime: str, trend: str) -> str:
        """Classify market regime for strategy selection."""
        if volatility_regime in ("high", "extreme"):
            return "explosive"
        if trend == "sideways":
            return "ranging"
        return "trending"

    def _classify_sentiment(self, trend: str, rsi: float) -> str:
        """Classify market sentiment combining trend and RSI."""
        if trend == "bullish" and rsi > 65:
            return "strong_bullish"
        elif trend == "bearish" and rsi < 35:
            return "strong_bearish"
        elif trend == "bullish":
            return "weak_bullish"
        elif trend == "bearish":
            return "weak_bearish"
        return "neutral"

    # ═════════════════════════════════════════════════════
    #  MOMENTUM
    # ═════════════════════════════════════════════════════

    def _momentum_strength(self, closes: List[float]) -> float:
        """
        Momentum strength: average absolute return over last 10 bars.
        """
        if len(closes) < 11:
            return 0.0

        returns = [
            abs(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(len(closes) - 10, len(closes))
            if closes[i - 1] > 0
        ]

        return mean(returns) if returns else 0.0

    def _momentum_acceleration(self, closes: List[float]) -> float:
        """
        Momentum acceleration: recent momentum vs prior momentum.

        Positive = accelerating (momentum increasing)
        Negative = decelerating (momentum decreasing)
        """
        if len(closes) < 15:
            return 0.0

        recent = [closes[i] - closes[i - 1] for i in range(-5, 0)]
        prior = [closes[i] - closes[i - 1] for i in range(-15, -5)]

        return mean(recent) - mean(prior)

    # ═════════════════════════════════════════════════════
    #  VOLUME
    # ═════════════════════════════════════════════════════

    def _volume_pressure(self, candles: List[Dict]) -> float:
        """
        Volume pressure: net buying vs selling pressure.

        Returns:
            -1 (all bearish) to +1 (all bullish)
        """
        if not candles:
            return 0.0

        bull_vol = 0.0
        bear_vol = 0.0

        for c in candles:
            vol = float(c.get("volume", 0))
            if c["close"] > c["open"]:
                bull_vol += vol
            else:
                bear_vol += vol

        total = bull_vol + bear_vol
        if total == 0:
            return 0.0

        return (bull_vol - bear_vol) / total

    def _liquidity_score(self, volumes: List[float]) -> float:
        """
        Liquidity score based on volume consistency.

        Higher = more consistent volume (easier fills)
        Lower = erratic volume (slippage risk)
        """
        if len(volumes) < 10:
            return 0.5

        recent = volumes[-30:] if len(volumes) >= 30 else volumes
        avg = mean(recent)
        std = stdev(recent) if len(recent) > 1 else 0

        if std == 0:
            return 1.0

        # CV (coefficient of variation) inverted and normalized
        cv = std / avg if avg > 0 else 1
        return min(1.0, 1 / (1 + cv))

    # ═════════════════════════════════════════════════════
    #  MARKET STRUCTURE
    # ═════════════════════════════════════════════════════

    def _detect_structure_break(self, closes: List[float]) -> bool:
        """
        Detect if price broke recent structure (highs/lows).
        """
        if len(closes) < 21:
            return False

        recent_high = max(closes[-20:-1])
        recent_low = min(closes[-20:-1])
        current = closes[-1]

        return current > recent_high or current < recent_low

    def _support_resistance(
        self,
        highs: List[float],
        lows: List[float],
        lookback: int = 30,
    ) -> Tuple[float, float]:
        """
        Simple support/resistance from recent highs/lows.
        """
        lookback = min(lookback, len(highs))

        resistance = max(highs[-lookback:])
        support = min(lows[-lookback:])

        return support, resistance

    # ═════════════════════════════════════════════════════
    #  CONFIDENCE SCORING
    # ═════════════════════════════════════════════════════

    def _compute_confidence(
        self,
        trend_strength: float,
        momentum_strength: float,
        volume_pressure: float,
        liquidity_score: float,
    ) -> float:
        """
        Aggregate confidence score (0-1).

        Weighted combination of factors.
        """
        score = (
            trend_strength * 100 * 0.30 +      # Normalize trend strength
            momentum_strength * 100 * 0.25 +   # Normalize momentum
            abs(volume_pressure) * 0.25 +      # Volume direction clarity
            liquidity_score * 0.20             # Execution quality
        )

        return min(1.0, score)

    # ═════════════════════════════════════════════════════
    #  BRAIN 1: INDICATORS DICT
    # ═════════════════════════════════════════════════════

    def _build_indicators_dict(
        self,
        rsi: float,
        macd_line: float,
        macd_signal: float,
        macd_hist: float,
        ema_20: float,
        ema_50: float,
        price: float,
        bb_upper: float,
        bb_lower: float,
        bb_mid: float,
    ) -> Dict:
        """
        Build structured dict for Brain1 (technical indicators).

        Returns categorical signals that controller._brain_indicators() consumes.
        """
        # MACD cross
        if macd_hist > 0 and macd_line > macd_signal:
            macd_cross = "bullish"
        elif macd_hist < 0 and macd_line < macd_signal:
            macd_cross = "bearish"
        else:
            macd_cross = None

        # EMA cross
        if ema_20 > ema_50:
            ema_cross = "bullish"
        elif ema_20 < ema_50:
            ema_cross = "bearish"
        else:
            ema_cross = None

        # Bollinger position
        bb_range = bb_upper - bb_lower
        if bb_range > 0:
            pct_b = (price - bb_lower) / bb_range
        else:
            pct_b = 0.5

        if pct_b <= 0.2:
            bb_position = "oversold"
        elif pct_b >= 0.8:
            bb_position = "overbought"
        else:
            bb_position = "mid"

        return {
            "rsi": rsi,
            "macd_cross": macd_cross,
            "macd_histogram": macd_hist,
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "ema_cross": ema_cross,
            "ema_20": ema_20,
            "ema_50": ema_50,
            "bb_position": bb_position,
            "bb_pct_b": round(pct_b, 4),
        }

    # ═════════════════════════════════════════════════════
    #  BRAIN 2: SENTIMENT SCORE
    # ═════════════════════════════════════════════════════

    def _build_sentiment_score(
        self,
        trend: str,
        rsi: float,
        macd_hist: float,
        volume_pressure: float,
        momentum_accel: float,
    ) -> float:
        """
        Multi-factor sentiment aggregation.

        Returns:
            Float in [-1.0, +1.0] for Brain2.
        """
        score = 0.0

        # Trend component (weight 0.30)
        if trend == "bullish":
            score += 0.30
        elif trend == "bearish":
            score -= 0.30

        # RSI component (weight 0.25)
        # Normalize: 50 = neutral, 30 = -1, 70 = +1
        rsi_norm = (rsi - 50) / 50
        score += rsi_norm * 0.25

        # MACD histogram direction (weight 0.20)
        if macd_hist > 0:
            score += 0.20
        elif macd_hist < 0:
            score -= 0.20

        # Volume pressure (weight 0.15)
        score += volume_pressure * 0.15

        # Momentum acceleration (weight 0.10)
        accel_norm = max(-1.0, min(1.0, momentum_accel * 1000))
        score += accel_norm * 0.10

        return max(-1.0, min(1.0, score))

    # ═════════════════════════════════════════════════════
    #  BRAIN 3: CHART PATTERN RECOGNITION
    # ═════════════════════════════════════════════════════

    def _detect_chart_pattern(
        self,
        opens: List[float],
        highs: List[float],
        lows: List[float],
        closes: List[float],
        volume_pressure: float,
        structure_break: bool,
    ) -> Dict:
        """
        Detect candlestick and price action patterns.

        FIX: Now passes actual open prices (not previous close).

        Returns:
            {signal, confidence, pattern_name}
        """
        patterns_found = []

        if len(closes) < 3:
            return {"signal": "HOLD", "confidence": 0, "pattern_name": "None"}

        # ── Bullish patterns ──────────────────────────────────────
        if self._is_bullish_engulfing(opens, highs, lows, closes):
            patterns_found.append(("BUY", 70, "Bullish Engulfing"))

        if self._is_hammer(opens[-1], highs[-1], lows[-1], closes[-1]):
            patterns_found.append(("BUY", 60, "Hammer"))

        if self._is_morning_star(opens, closes):
            patterns_found.append(("BUY", 75, "Morning Star"))

        if self._is_double_bottom(lows[-20:]):
            patterns_found.append(("BUY", 65, "Double Bottom"))

        # ── Bearish patterns ──────────────────────────────────────
        if self._is_bearish_engulfing(opens, highs, lows, closes):
            patterns_found.append(("SELL", 70, "Bearish Engulfing"))

        if self._is_shooting_star(opens[-1], highs[-1], lows[-1], closes[-1]):
            patterns_found.append(("SELL", 60, "Shooting Star"))

        if self._is_evening_star(opens, closes):
            patterns_found.append(("SELL", 75, "Evening Star"))

        if self._is_double_top(highs[-20:]):
            patterns_found.append(("SELL", 65, "Double Top"))

        # ── Breakout overlay ──────────────────────────────────────
        if structure_break:
            if closes[-1] > closes[-2]:
                patterns_found.append(("BUY", 55, "Breakout Up"))
            else:
                patterns_found.append(("SELL", 55, "Breakout Down"))

        if not patterns_found:
            return {"signal": "HOLD", "confidence": 0, "pattern_name": "None"}

        # Pick highest-confidence pattern
        best = max(patterns_found, key=lambda x: x[1])
        signal, conf, name = best

        # Boost confidence if volume confirms
        if signal == "BUY" and volume_pressure > 0.2:
            conf = min(100, conf + 10)
        elif signal == "SELL" and volume_pressure < -0.2:
            conf = min(100, conf + 10)

        return {
            "signal": signal,
            "confidence": conf,
            "pattern_name": name,
        }

    # ═════════════════════════════════════════════════════
    #  CANDLESTICK PATTERN HELPERS (FIXED)
    # ═════════════════════════════════════════════════════

    def _is_bullish_engulfing(
        self,
        opens: List[float],
        highs: List[float],
        lows: List[float],
        closes: List[float],
    ) -> bool:
        """Bullish engulfing: bearish candle followed by larger bullish candle."""
        if len(closes) < 2:
            return False

        # Previous candle bearish
        prev_bearish = closes[-2] < opens[-2]
        # Current candle bullish
        curr_bullish = closes[-1] > opens[-1]
        # Current body engulfs previous body
        engulfs = (
            opens[-1] < closes[-2] and
            closes[-1] > opens[-2]
        )

        return prev_bearish and curr_bullish and engulfs

    def _is_bearish_engulfing(
        self,
        opens: List[float],
        highs: List[float],
        lows: List[float],
        closes: List[float],
    ) -> bool:
        """Bearish engulfing: bullish candle followed by larger bearish candle."""
        if len(closes) < 2:
            return False

        prev_bullish = closes[-2] > opens[-2]
        curr_bearish = closes[-1] < opens[-1]
        engulfs = (
            opens[-1] > closes[-2] and
            closes[-1] < opens[-2]
        )

        return prev_bullish and curr_bearish and engulfs

    def _is_hammer(
        self,
        open_p: float,
        high_p: float,
        low_p: float,
        close_p: float,
    ) -> bool:
        """
        Hammer: small body at top, long lower wick.

        FIX: Now uses actual open price, not previous close.
        """
        body = abs(close_p - open_p)
        total_range = high_p - low_p

        if total_range == 0:
            return False

        lower_wick = min(open_p, close_p) - low_p
        upper_wick = high_p - max(open_p, close_p)

        # Lower wick at least 60% of range, body less than 30%
        return (
            lower_wick / total_range > 0.6 and
            body / total_range < 0.3 and
            upper_wick / total_range < 0.1
        )

    def _is_shooting_star(
        self,
        open_p: float,
        high_p: float,
        low_p: float,
        close_p: float,
    ) -> bool:
        """
        Shooting star: small body at bottom, long upper wick.

        FIX: Now uses actual open price, not previous close.
        """
        body = abs(close_p - open_p)
        total_range = high_p - low_p

        if total_range == 0:
            return False

        upper_wick = high_p - max(open_p, close_p)
        lower_wick = min(open_p, close_p) - low_p

        # Upper wick at least 60% of range, body less than 30%
        return (
            upper_wick / total_range > 0.6 and
            body / total_range < 0.3 and
            lower_wick / total_range < 0.1
        )

    def _is_morning_star(
        self, opens: List[float], closes: List[float]
    ) -> bool:
        """Morning star: bearish, small body (doji), bullish."""
        if len(closes) < 3:
            return False

        # First candle: bearish
        first_bearish = closes[-3] < opens[-3]
        # Second candle: small body (doji-like)
        second_small = abs(closes[-2] - opens[-2]) < abs(closes[-3] - opens[-3]) * 0.3
        # Third candle: bullish, closes above midpoint of first
        third_bullish = closes[-1] > opens[-1]
        third_strong = closes[-1] > (opens[-3] + closes[-3]) / 2

        return first_bearish and second_small and third_bullish and third_strong

    def _is_evening_star(
        self, opens: List[float], closes: List[float]
    ) -> bool:
        """Evening star: bullish, small body (doji), bearish."""
        if len(closes) < 3:
            return False

        first_bullish = closes[-3] > opens[-3]
        second_small = abs(closes[-2] - opens[-2]) < abs(closes[-3] - opens[-3]) * 0.3
        third_bearish = closes[-1] < opens[-1]
        third_strong = closes[-1] < (opens[-3] + closes[-3]) / 2

        return first_bullish and second_small and third_bearish and third_strong

    def _is_double_bottom(self, lows: List[float]) -> bool:
        """Double bottom: two similar lows with higher middle."""
        if len(lows) < 10:
            return False

        mid = len(lows) // 2
        left_low = min(lows[:mid])
        right_low = min(lows[mid:])
        middle_high = max(lows[mid - 2:mid + 2])

        # Lows within 1.5% of each other
        lows_similar = abs(left_low - right_low) / max(left_low, right_low) < 0.015
        # Middle higher than both lows
        middle_higher = middle_high > left_low and middle_high > right_low

        return lows_similar and middle_higher

    def _is_double_top(self, highs: List[float]) -> bool:
        """Double top: two similar highs with lower middle."""
        if len(highs) < 10:
            return False

        mid = len(highs) // 2
        left_high = max(highs[:mid])
        right_high = max(highs[mid:])
        middle_low = min(highs[mid - 2:mid + 2])

        highs_similar = abs(left_high - right_high) / max(left_high, right_high) < 0.015
        middle_lower = middle_low < left_high and middle_low < right_high

        return highs_similar and middle_lower

    # ═════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═════════════════════════════════════════════════════

    def __repr__(self) -> str:
        return f"<MarketAnalyzer symbol={self.symbol}>"