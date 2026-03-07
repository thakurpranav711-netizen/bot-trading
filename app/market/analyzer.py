# app/market/analyzer.py

"""
Market Analyzer — Production Grade for Autonomous Trading

Computes all technical indicators and market state:
- Core indicators: RSI, EMA (9/20/50/200), MACD, Bollinger Bands, ATR
- Stochastic RSI, ADX, OBV, VWAP
- Trend detection and regime classification
- Volatility analysis (normalized and categorical)
- Volume analysis (pressure, spikes, divergence)
- Momentum metrics (strength, acceleration)
- Market structure (support/resistance, breakouts, pivots)
- Pattern recognition (candlestick + chart patterns)
- Multi-timeframe confluence (when data available)
- Signal generation with confidence scoring
- **NEW: Probability-based trade signals (60% threshold)**
- **NEW: Hourly market analysis notifications**
- **NEW: Exit probability monitoring**

Performance Optimizations:
- O(n) MACD calculation
- Cached intermediate calculations
- Efficient pure Python (no numpy dependency)
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from statistics import mean, stdev, median
from math import sqrt, log, exp
from datetime import datetime
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  MARKET STATE DATA CLASS
# ═══════════════════════════════════════════════════════════════════

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
    trend: str = "sideways"              # "bullish", "bearish", "sideways"
    trend_strength: float = 0.0          # 0-1 strength of trend
    regime: str = "ranging"              # "trending", "ranging", "explosive"
    sentiment: str = "neutral"           # "strong_bullish", "weak_bullish", etc.

    # ── EMAs ──────────────────────────────────────────────────────
    ema_9: float = 0.0
    ema_20: float = 0.0
    ema_50: float = 0.0
    ema_200: float = 0.0

    # ── RSI ───────────────────────────────────────────────────────
    rsi: float = 50.0
    rsi_signal: str = "neutral"          # "oversold", "overbought", "neutral"
    stoch_rsi_k: float = 50.0
    stoch_rsi_d: float = 50.0

    # ── MACD ──────────────────────────────────────────────────────
    macd_line: float = 0.0
    macd_signal: float = 0.0
    macd_histogram: float = 0.0
    macd_cross: str = "none"             # "bullish", "bearish", "none"
    macd_divergence: str = "none"        # "bullish", "bearish", "none"

    # ── Bollinger Bands ───────────────────────────────────────────
    bb_upper: float = 0.0
    bb_lower: float = 0.0
    bb_mid: float = 0.0
    bb_width: float = 0.0
    bb_percent_b: float = 0.5
    bb_squeeze: bool = False

    # ── Volatility / ATR ──────────────────────────────────────────
    atr: float = 0.0
    atr_percent: float = 0.0
    volatility: float = 0.0              # 0-1 normalized
    volatility_pct: float = 0.0          # ATR / price
    volatility_regime: str = "normal"    # "low", "normal", "high", "extreme"
    volatility_expanding: bool = False

    # ── ADX (Trend Strength) ──────────────────────────────────────
    adx: float = 0.0
    plus_di: float = 0.0
    minus_di: float = 0.0

    # ── Volume ────────────────────────────────────────────────────
    volume: float = 0.0
    volume_sma: float = 0.0
    volume_ratio: float = 1.0            # current / average
    volume_spike: bool = False
    volume_pressure: float = 0.0         # -1 to +1
    obv_trend: str = "neutral"           # "bullish", "bearish", "neutral"

    # ── Momentum ──────────────────────────────────────────────────
    momentum: float = 0.0
    momentum_strength: float = 0.0
    momentum_acceleration: float = 0.0
    roc: float = 0.0                     # Rate of change

    # ── Market Structure ──────────────────────────────────────────
    structure_break: bool = False
    break_direction: str = "none"        # "up", "down", "none"
    support_level: float = 0.0
    resistance_level: float = 0.0
    pivot_point: float = 0.0
    distance_to_support: float = 0.0     # percentage
    distance_to_resistance: float = 0.0

    # ── Price Action ──────────────────────────────────────────────
    price_vs_ema: str = "neutral"        # "above_all", "below_all", "mixed"
    higher_highs: bool = False
    lower_lows: bool = False
    consolidating: bool = False

    # ── Liquidity ─────────────────────────────────────────────────
    liquidity_score: float = 0.5

    # ── Signal & Confidence ───────────────────────────────────────
    signal: str = "HOLD"                 # "BUY", "SELL", "HOLD"
    signal_strength: float = 0.0         # 0-1
    confidence_score: float = 0.5        # 0-1

    # ── NEW: Probability-Based Trading ────────────────────────────
    profit_probability: float = 0.0      # 0-100% probability of profit
    loss_probability: float = 0.0        # 0-100% probability of loss
    should_enter: bool = False           # True if profit_probability >= 60%
    should_exit: bool = False            # True if loss_probability > profit_probability
    entry_recommendation: str = "WAIT"   # "BUY", "SELL", "WAIT"
    
    # ── Trade Targets ─────────────────────────────────────────────
    suggested_entry: float = 0.0
    suggested_target: float = 0.0
    suggested_stop_loss: float = 0.0
    risk_reward_ratio: float = 0.0

    # ── Brain Feed Fields ─────────────────────────────────────────
    indicators: Dict = field(default_factory=dict)
    sentiment_score: float = 0.0         # -1 to +1
    chart_pattern: Optional[Dict] = None
    ai_prediction: Optional[Dict] = None

    # ── Raw Data (for strategy use) ───────────────────────────────
    candle_count: int = 0


# ═══════════════════════════════════════════════════════════════════
#  TRADE SIGNAL DATA CLASS (NEW)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class TradeSignal:
    """
    Trade signal with probability-based entry/exit logic.
    Used for notifications and trade execution.
    """
    symbol: str
    signal_type: str  # "BUY", "SELL", "EXIT", "HOLD"
    probability: float  # 0-100
    entry_price: float
    target_price: float
    stop_loss: float
    risk_reward: float
    confidence: float  # 0-100
    reasons: List[str]
    timestamp: str
    
    # For exit signals
    exit_price: float = 0.0
    pnl_amount: float = 0.0
    pnl_percent: float = 0.0
    hold_duration_minutes: int = 0
    
    def should_execute(self) -> bool:
        """Check if signal meets 60% probability threshold."""
        return self.probability >= 60.0 and self.signal_type in ("BUY", "SELL")
    
    def format_entry_notification(self) -> str:
        """Format entry notification message."""
        direction_emoji = "🟢" if self.signal_type == "BUY" else "🔴"
        
        return (
            f"{direction_emoji} **TRADE SIGNAL: {self.signal_type}**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Symbol: `{self.symbol}`\n"
            f"💰 Entry Price: `${self.entry_price:,.4f}`\n"
            f"🎯 Target: `${self.target_price:,.4f}` ({self._calc_target_pct():+.2f}%)\n"
            f"🛑 Stop Loss: `${self.stop_loss:,.4f}` ({self._calc_sl_pct():+.2f}%)\n"
            f"📈 Risk/Reward: `1:{self.risk_reward:.2f}`\n"
            f"🎲 Probability: `{self.probability:.1f}%`\n"
            f"💪 Confidence: `{self.confidence:.1f}%`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 Reasons:\n" + "\n".join(f"  • {r}" for r in self.reasons[:5])
        )
    
    def format_exit_notification(self) -> str:
        """Format exit notification message."""
        pnl_emoji = "✅" if self.pnl_amount >= 0 else "❌"
        
        hours = self.hold_duration_minutes // 60
        mins = self.hold_duration_minutes % 60
        duration_str = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
        
        return (
            f"{pnl_emoji} **TRADE CLOSED**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Symbol: `{self.symbol}`\n"
            f"💰 Exit Price: `${self.exit_price:,.4f}`\n"
            f"📈 P/L: `₹{self.pnl_amount:+,.2f}` ({self.pnl_percent:+.2f}%)\n"
            f"⏱️ Duration: `{duration_str}`\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )
    
    def _calc_target_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return ((self.target_price - self.entry_price) / self.entry_price) * 100
    
    def _calc_sl_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        return ((self.stop_loss - self.entry_price) / self.entry_price) * 100


# ═══════════════════════════════════════════════════════════════════
#  HOURLY ANALYSIS REPORT (NEW)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class HourlyAnalysisReport:
    """
    Hourly market analysis report for notifications.
    Sent even when not trading.
    """
    symbol: str
    timestamp: str
    price: float
    
    # Trend
    trend: str
    trend_strength: float
    
    # Key Indicators
    rsi: float
    rsi_signal: str
    macd_cross: str
    adx: float
    
    # Volatility
    volatility_regime: str
    atr_percent: float
    
    # Volume
    volume_pressure: float
    volume_spike: bool
    
    # Opportunities
    opportunities: List[str]
    warnings: List[str]
    
    # Probability
    buy_probability: float
    sell_probability: float
    
    def format_notification(self) -> str:
        """Format hourly analysis notification."""
        trend_emoji = {
            "bullish": "🟢",
            "bearish": "🔴",
            "sideways": "🟡"
        }.get(self.trend, "⚪")
        
        rsi_emoji = {
            "oversold": "🟢",
            "overbought": "🔴",
            "neutral": "⚪",
            "bullish": "🟢",
            "bearish": "🔴"
        }.get(self.rsi_signal, "⚪")
        
        vol_emoji = {
            "low": "😴",
            "normal": "📊",
            "high": "⚡",
            "extreme": "🔥"
        }.get(self.volatility_regime, "📊")
        
        macd_emoji = "🟢" if self.macd_cross == "bullish" else "🔴" if self.macd_cross == "bearish" else "⚪"
        
        msg = (
            f"📊 **HOURLY MARKET ANALYSIS**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 {self.symbol} | `${self.price:,.2f}`\n"
            f"🕐 {self.timestamp}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n\n"
            
            f"**📈 TREND**\n"
            f"{trend_emoji} Direction: `{self.trend.upper()}`\n"
            f"💪 Strength: `{self.trend_strength*100:.0f}%`\n\n"
            
            f"**📉 INDICATORS**\n"
            f"{rsi_emoji} RSI: `{self.rsi:.1f}` ({self.rsi_signal})\n"
            f"{macd_emoji} MACD: `{self.macd_cross}`\n"
            f"📊 ADX: `{self.adx:.1f}` ({'Strong' if self.adx > 25 else 'Weak'} trend)\n\n"
            
            f"**⚡ VOLATILITY**\n"
            f"{vol_emoji} Regime: `{self.volatility_regime.upper()}`\n"
            f"📏 ATR: `{self.atr_percent:.2f}%`\n"
        )
        
        # Volume
        vol_dir = "Buying 🟢" if self.volume_pressure > 0.2 else "Selling 🔴" if self.volume_pressure < -0.2 else "Neutral ⚪"
        msg += f"📊 Volume: `{vol_dir}`"
        if self.volume_spike:
            msg += " ⚠️ SPIKE!"
        msg += "\n\n"
        
        # Probabilities
        msg += (
            f"**🎲 PROBABILITIES**\n"
            f"🟢 BUY Signal: `{self.buy_probability:.0f}%`\n"
            f"🔴 SELL Signal: `{self.sell_probability:.0f}%`\n\n"
        )
        
        # Opportunities
        if self.opportunities:
            msg += f"**💡 OPPORTUNITIES**\n"
            for opp in self.opportunities[:3]:
                msg += f"  • {opp}\n"
            msg += "\n"
        
        # Warnings
        if self.warnings:
            msg += f"**⚠️ WARNINGS**\n"
            for warn in self.warnings[:3]:
                msg += f"  • {warn}\n"
            msg += "\n"
        
        # Recommendation
        if self.buy_probability >= 60:
            msg += "🎯 **RECOMMENDATION: LOOK FOR BUY ENTRY**"
        elif self.sell_probability >= 60:
            msg += "🎯 **RECOMMENDATION: LOOK FOR SELL/EXIT**"
        else:
            msg += "⏳ **RECOMMENDATION: WAIT FOR BETTER SETUP**"
        
        return msg


# ═══════════════════════════════════════════════════════════════════
#  MARKET ANALYZER
# ═══════════════════════════════════════════════════════════════════

class MarketAnalyzer:
    """
    Stateless market analyzer for autonomous trading.

    Usage:
        analyzer = MarketAnalyzer("BTC/USDT")
        state = analyzer.analyze({"price": 67000, "candles": [...]})

    Returns fully populated MarketState with signals.
    
    NEW Features:
        - Probability-based trade signals (60% threshold)
        - Exit monitoring (loss probability detection)
        - Hourly analysis report generation
    """

    MIN_CANDLES = 50  # Minimum for reliable indicators
    IDEAL_CANDLES = 150  # Ideal for all indicators
    
    # Probability thresholds
    ENTRY_PROBABILITY_THRESHOLD = 60.0  # Minimum probability to enter trade
    EXIT_PROBABILITY_THRESHOLD = 55.0   # Exit if loss probability exceeds this

    def __init__(
        self,
        symbol: str,
        min_probability: float = None,  # ✅ NEW - accepts but uses class default if not provided
        entry_threshold: float = None,  # ✅ NEW - optional override
        exit_threshold: float = None,   # ✅ NEW - optional override
    ):
        """
        Initialize MarketAnalyzer.
        
        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            min_probability: Minimum probability for entry signals (default 60%)
            entry_threshold: Override for ENTRY_PROBABILITY_THRESHOLD
            exit_threshold: Override for EXIT_PROBABILITY_THRESHOLD
        """
        self.symbol = symbol
        self._cache: Dict = {}
        self._last_analysis_time: Optional[datetime] = None
        
        # Apply custom thresholds if provided
        if min_probability is not None:
            self.ENTRY_PROBABILITY_THRESHOLD = min_probability * 100 if min_probability <= 1 else min_probability
        if entry_threshold is not None:
            self.ENTRY_PROBABILITY_THRESHOLD = entry_threshold * 100 if entry_threshold <= 1 else entry_threshold
        if exit_threshold is not None:
            self.EXIT_PROBABILITY_THRESHOLD = exit_threshold * 100 if exit_threshold <= 1 else exit_threshold
        
        logger.info(
            f"📊 MarketAnalyzer initialized | {symbol} | "
            f"EntryThreshold={self.ENTRY_PROBABILITY_THRESHOLD:.0f}% | "
            f"ExitThreshold={self.EXIT_PROBABILITY_THRESHOLD:.0f}%"
        )

    # ═══════════════════════════════════════════════════════════════
    #  MAIN ANALYSIS
    # ═══════════════════════════════════════════════════════════════

    def analyze(self, market_data: Dict) -> MarketState:
        """
        Perform complete market analysis.

        Args:
            market_data: Dict with 'price' and 'candles' keys

        Returns:
            Fully populated MarketState with probability-based signals

        Raises:
            ValueError: If insufficient candle data
        """
        candles = market_data.get("candles", [])
        price = float(market_data.get("price", 0))

        if len(candles) < self.MIN_CANDLES:
            raise ValueError(
                f"Insufficient candles: {len(candles)} < {self.MIN_CANDLES} required"
            )

        # ── Extract OHLCV ─────────────────────────────────────────
        opens = [float(c["open"]) for c in candles]
        highs = [float(c["high"]) for c in candles]
        lows = [float(c["low"]) for c in candles]
        closes = [float(c["close"]) for c in candles]
        volumes = [float(c.get("volume", 0)) for c in candles]

        # ── EMAs ──────────────────────────────────────────────────
        ema_9 = self._ema(closes, 9)
        ema_20 = self._ema(closes, 20)
        ema_50 = self._ema(closes, 50)
        ema_200 = self._ema(closes, min(200, len(closes)))

        # ── RSI ───────────────────────────────────────────────────
        rsi = self._rsi_wilder(closes, 14)
        rsi_signal = self._classify_rsi(rsi)
        stoch_k, stoch_d = self._stochastic_rsi(closes, 14, 3, 3)

        # ── MACD ──────────────────────────────────────────────────
        macd_line, macd_signal_line, macd_hist = self._macd_optimized(closes)
        macd_cross = self._detect_macd_cross(closes)
        macd_divergence = self._detect_macd_divergence(closes, macd_hist)

        # ── ATR & Volatility ──────────────────────────────────────
        atr = self._atr(highs, lows, closes, 14)
        atr_percent = (atr / price * 100) if price > 0 else 0
        volatility_pct = atr / price if price > 0 else 0
        volatility_regime = self._classify_volatility(volatility_pct)
        volatility_normalized = self._normalize_volatility(closes, 20)
        volatility_expanding = self._is_volatility_expanding(highs, lows, closes)

        # ── Bollinger Bands ───────────────────────────────────────
        bb_upper, bb_mid, bb_lower, bb_width = self._bollinger(closes, 20, 2.0)
        bb_percent_b = self._bollinger_percent_b(price, bb_upper, bb_lower)
        bb_squeeze = self._detect_bb_squeeze(closes, highs, lows, atr)

        # ── ADX ───────────────────────────────────────────────────
        adx, plus_di, minus_di = self._adx(highs, lows, closes, 14)

        # ── Volume Analysis ───────────────────────────────────────
        current_volume = volumes[-1] if volumes else 0
        volume_sma = mean(volumes[-20:]) if len(volumes) >= 20 else mean(volumes) if volumes else 0
        volume_ratio = current_volume / volume_sma if volume_sma > 0 else 1.0
        volume_spike = volume_ratio > 1.5
        volume_pressure = self._volume_pressure(candles[-20:])
        obv_trend = self._obv_trend(closes, volumes)

        # ── Trend Analysis ────────────────────────────────────────
        trend = self._classify_trend(price, ema_9, ema_20, ema_50, ema_200)
        trend_strength = self._calculate_trend_strength(
            price, ema_20, ema_50, adx, macd_hist
        )
        regime = self._classify_regime(volatility_regime, trend, adx)
        sentiment = self._classify_sentiment(trend, rsi, macd_hist, volume_pressure)

        # ── Momentum ──────────────────────────────────────────────
        momentum = closes[-1] - closes[-10] if len(closes) >= 10 else 0
        momentum_strength = self._momentum_strength(closes)
        momentum_acceleration = self._momentum_acceleration(closes)
        roc = self._rate_of_change(closes, 10)

        # ── Market Structure ──────────────────────────────────────
        support, resistance = self._support_resistance(highs, lows, closes, 30)
        pivot = self._pivot_point(highs[-1], lows[-1], closes[-1])
        structure_break, break_dir = self._detect_structure_break(closes, highs, lows)
        dist_support = ((price - support) / price * 100) if price > 0 else 0
        dist_resistance = ((resistance - price) / price * 100) if price > 0 else 0

        # ── Price Action ──────────────────────────────────────────
        price_vs_ema = self._price_vs_emas(price, ema_9, ema_20, ema_50, ema_200)
        higher_highs = self._check_higher_highs(highs)
        lower_lows = self._check_lower_lows(lows)
        consolidating = self._is_consolidating(closes, atr)

        # ── Liquidity ─────────────────────────────────────────────
        liquidity_score = self._liquidity_score(volumes)

        # ── Confidence ────────────────────────────────────────────
        confidence_score = self._compute_confidence(
            trend_strength=trend_strength,
            momentum_strength=momentum_strength,
            volume_pressure=volume_pressure,
            liquidity_score=liquidity_score,
            adx=adx,
            rsi=rsi,
        )

        # ── Signal Generation ─────────────────────────────────────
        signal, signal_strength = self._generate_signal(
            trend=trend,
            rsi=rsi,
            macd_cross=macd_cross,
            macd_hist=macd_hist,
            bb_percent_b=bb_percent_b,
            volume_pressure=volume_pressure,
            adx=adx,
            price_vs_ema=price_vs_ema,
            structure_break=structure_break,
            break_direction=break_dir,
        )

        # ══════════════════════════════════════════════════════════
        # NEW: PROBABILITY-BASED TRADING SIGNALS
        # ══════════════════════════════════════════════════════════
        
        profit_probability, loss_probability = self._calculate_probabilities(
            trend=trend,
            trend_strength=trend_strength,
            rsi=rsi,
            rsi_signal=rsi_signal,
            macd_cross=macd_cross,
            macd_hist=macd_hist,
            bb_percent_b=bb_percent_b,
            volume_pressure=volume_pressure,
            adx=adx,
            momentum_acceleration=momentum_acceleration,
            structure_break=structure_break,
            break_direction=break_dir,
            volatility_regime=volatility_regime,
            stoch_k=stoch_k,
            stoch_d=stoch_d,
        )
        
        # Determine if we should enter or exit
        should_enter = profit_probability >= self.ENTRY_PROBABILITY_THRESHOLD
        should_exit = loss_probability > profit_probability and loss_probability >= self.EXIT_PROBABILITY_THRESHOLD
        
        # Entry recommendation
        if should_enter and signal == "BUY":
            entry_recommendation = "BUY"
        elif should_enter and signal == "SELL":
            entry_recommendation = "SELL"
        else:
            entry_recommendation = "WAIT"
        
        # Calculate trade targets
        suggested_entry = price
        suggested_target, suggested_stop_loss, risk_reward = self._calculate_trade_targets(
            price=price,
            atr=atr,
            trend=trend,
            support=support,
            resistance=resistance,
            signal=signal,
        )

        # ── Brain Feeds ───────────────────────────────────────────
        indicators = self._build_indicators_dict(
            rsi=rsi,
            stoch_k=stoch_k,
            stoch_d=stoch_d,
            macd_line=macd_line,
            macd_signal=macd_signal_line,
            macd_hist=macd_hist,
            macd_cross=macd_cross,
            ema_9=ema_9,
            ema_20=ema_20,
            ema_50=ema_50,
            ema_200=ema_200,
            price=price,
            bb_upper=bb_upper,
            bb_lower=bb_lower,
            bb_mid=bb_mid,
            bb_percent_b=bb_percent_b,
            adx=adx,
            plus_di=plus_di,
            minus_di=minus_di,
            atr=atr,
        )

        sentiment_score = self._build_sentiment_score(
            trend=trend,
            rsi=rsi,
            macd_hist=macd_hist,
            volume_pressure=volume_pressure,
            momentum_accel=momentum_acceleration,
            adx=adx,
            stoch_k=stoch_k,
        )

        chart_pattern = self._detect_chart_pattern(
            opens=opens,
            highs=highs,
            lows=lows,
            closes=closes,
            volumes=volumes,
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
            trend_strength=round(trend_strength, 4),
            regime=regime,
            sentiment=sentiment,

            # EMAs
            ema_9=round(ema_9, 6),
            ema_20=round(ema_20, 6),
            ema_50=round(ema_50, 6),
            ema_200=round(ema_200, 6),

            # RSI
            rsi=round(rsi, 2),
            rsi_signal=rsi_signal,
            stoch_rsi_k=round(stoch_k, 2),
            stoch_rsi_d=round(stoch_d, 2),

            # MACD
            macd_line=round(macd_line, 8),
            macd_signal=round(macd_signal_line, 8),
            macd_histogram=round(macd_hist, 8),
            macd_cross=macd_cross,
            macd_divergence=macd_divergence,

            # Bollinger
            bb_upper=round(bb_upper, 6),
            bb_lower=round(bb_lower, 6),
            bb_mid=round(bb_mid, 6),
            bb_width=round(bb_width, 6),
            bb_percent_b=round(bb_percent_b, 4),
            bb_squeeze=bb_squeeze,

            # Volatility
            atr=round(atr, 8),
            atr_percent=round(atr_percent, 4),
            volatility=round(volatility_normalized, 4),
            volatility_pct=round(volatility_pct, 6),
            volatility_regime=volatility_regime,
            volatility_expanding=volatility_expanding,

            # ADX
            adx=round(adx, 2),
            plus_di=round(plus_di, 2),
            minus_di=round(minus_di, 2),

            # Volume
            volume=current_volume,
            volume_sma=round(volume_sma, 2),
            volume_ratio=round(volume_ratio, 2),
            volume_spike=volume_spike,
            volume_pressure=round(volume_pressure, 4),
            obv_trend=obv_trend,

            # Momentum
            momentum=round(momentum, 6),
            momentum_strength=round(momentum_strength, 6),
            momentum_acceleration=round(momentum_acceleration, 8),
            roc=round(roc, 4),

            # Structure
            structure_break=structure_break,
            break_direction=break_dir,
            support_level=round(support, 6),
            resistance_level=round(resistance, 6),
            pivot_point=round(pivot, 6),
            distance_to_support=round(dist_support, 2),
            distance_to_resistance=round(dist_resistance, 2),

            # Price Action
            price_vs_ema=price_vs_ema,
            higher_highs=higher_highs,
            lower_lows=lower_lows,
            consolidating=consolidating,

            # Liquidity
            liquidity_score=round(liquidity_score, 4),

            # Signal
            signal=signal,
            signal_strength=round(signal_strength, 4),
            confidence_score=round(confidence_score, 4),

            # NEW: Probability-based fields
            profit_probability=round(profit_probability, 2),
            loss_probability=round(loss_probability, 2),
            should_enter=should_enter,
            should_exit=should_exit,
            entry_recommendation=entry_recommendation,
            
            # Trade targets
            suggested_entry=round(suggested_entry, 6),
            suggested_target=round(suggested_target, 6),
            suggested_stop_loss=round(suggested_stop_loss, 6),
            risk_reward_ratio=round(risk_reward, 2),

            # Brain feeds
            indicators=indicators,
            sentiment_score=round(sentiment_score, 4),
            chart_pattern=chart_pattern,
            ai_prediction=None,

            # Meta
            candle_count=len(candles),
        )

        # ── Log ───────────────────────────────────────────────────
        logger.info(
            f"📊 {state.symbol} | ${price:,.2f} | "
            f"{trend.upper()} | RSI={rsi:.1f} | "
            f"MACD={'▲' if macd_hist > 0 else '▼'} | "
            f"ADX={adx:.1f} | Vol={volatility_regime} | "
            f"Signal={signal} ({signal_strength*100:.0f}%) | "
            f"Prob={profit_probability:.0f}%/{loss_probability:.0f}% | "
            f"{'✅ ENTER' if should_enter else '⏳ WAIT'}"
        )

        self._last_analysis_time = datetime.utcnow()
        return state

    # ═══════════════════════════════════════════════════════════════
    #  PROBABILITY CALCULATION (NEW)
    # ═══════════════════════════════════════════════════════════════

    def _calculate_probabilities(
        self,
        trend: str,
        trend_strength: float,
        rsi: float,
        rsi_signal: str,
        macd_cross: str,
        macd_hist: float,
        bb_percent_b: float,
        volume_pressure: float,
        adx: float,
        momentum_acceleration: float,
        structure_break: bool,
        break_direction: str,
        volatility_regime: str,
        stoch_k: float,
        stoch_d: float,
    ) -> Tuple[float, float]:
        """
        Calculate profit and loss probabilities based on multiple factors.
        
        This uses a weighted scoring system across multiple indicators
        to estimate the probability of a profitable trade.
        
        Returns:
            Tuple of (profit_probability, loss_probability) as percentages 0-100
        """
        bullish_score = 0.0
        bearish_score = 0.0
        total_weight = 0.0
        
        # ── 1. Trend Alignment (Weight: 20%) ──────────────────────
        weight = 20.0
        total_weight += weight
        if trend == "bullish":
            bullish_score += weight * (0.5 + trend_strength * 0.5)
        elif trend == "bearish":
            bearish_score += weight * (0.5 + trend_strength * 0.5)
        else:
            # Sideways - slight negative for both
            bullish_score += weight * 0.3
            bearish_score += weight * 0.3
        
        # ── 2. RSI Conditions (Weight: 15%) ───────────────────────
        weight = 15.0
        total_weight += weight
        if rsi < 30:  # Oversold - bullish opportunity
            bullish_score += weight * (1.0 - rsi / 30)
        elif rsi > 70:  # Overbought - bearish signal
            bearish_score += weight * ((rsi - 70) / 30)
        elif 40 <= rsi <= 60:  # Neutral zone
            bullish_score += weight * 0.4
            bearish_score += weight * 0.4
        elif rsi < 40:  # Slightly bearish
            bearish_score += weight * 0.6
            bullish_score += weight * 0.3
        else:  # 60 < rsi < 70 - Slightly bullish
            bullish_score += weight * 0.6
            bearish_score += weight * 0.3
        
        # ── 3. MACD Cross (Weight: 15%) ───────────────────────────
        weight = 15.0
        total_weight += weight
        if macd_cross == "bullish":
            bullish_score += weight * 0.9
        elif macd_cross == "bearish":
            bearish_score += weight * 0.9
        elif macd_hist > 0:
            bullish_score += weight * 0.6
        elif macd_hist < 0:
            bearish_score += weight * 0.6
        else:
            bullish_score += weight * 0.3
            bearish_score += weight * 0.3
        
        # ── 4. Bollinger Band Position (Weight: 10%) ──────────────
        weight = 10.0
        total_weight += weight
        if bb_percent_b < 0.2:  # Near lower band - potential bounce
            bullish_score += weight * 0.8
        elif bb_percent_b > 0.8:  # Near upper band - potential reversal
            bearish_score += weight * 0.8
        elif 0.4 <= bb_percent_b <= 0.6:  # Middle - neutral
            bullish_score += weight * 0.4
            bearish_score += weight * 0.4
        elif bb_percent_b < 0.4:
            bullish_score += weight * 0.6
            bearish_score += weight * 0.3
        else:
            bearish_score += weight * 0.6
            bullish_score += weight * 0.3
        
        # ── 5. Volume Pressure (Weight: 12%) ──────────────────────
        weight = 12.0
        total_weight += weight
        if volume_pressure > 0.3:  # Strong buying
            bullish_score += weight * 0.9
        elif volume_pressure < -0.3:  # Strong selling
            bearish_score += weight * 0.9
        elif volume_pressure > 0:
            bullish_score += weight * (0.5 + volume_pressure)
        else:
            bearish_score += weight * (0.5 + abs(volume_pressure))
        
        # ── 6. ADX Trend Strength (Weight: 10%) ───────────────────
        weight = 10.0
        total_weight += weight
        if adx > 25:  # Strong trend
            if trend == "bullish":
                bullish_score += weight * min(1.0, adx / 50)
            elif trend == "bearish":
                bearish_score += weight * min(1.0, adx / 50)
            else:
                # Strong ADX but sideways trend - breakout possible
                bullish_score += weight * 0.4
                bearish_score += weight * 0.4
        else:  # Weak trend
            bullish_score += weight * 0.3
            bearish_score += weight * 0.3
        
        # ── 7. Momentum Acceleration (Weight: 8%) ─────────────────
        weight = 8.0
        total_weight += weight
        if momentum_acceleration > 0:
            bullish_score += weight * min(1.0, 0.5 + momentum_acceleration * 10)
        else:
            bearish_score += weight * min(1.0, 0.5 + abs(momentum_acceleration) * 10)
        
        # ── 8. Structure Break (Weight: 5%) ───────────────────────
        weight = 5.0
        total_weight += weight
        if structure_break:
            if break_direction == "up":
                bullish_score += weight
            elif break_direction == "down":
                bearish_score += weight
        else:
            bullish_score += weight * 0.3
            bearish_score += weight * 0.3
        
        # ── 9. Stochastic RSI (Weight: 5%) ────────────────────────
        weight = 5.0
        total_weight += weight
        if stoch_k < 20 and stoch_k > stoch_d:  # Oversold with bullish cross
            bullish_score += weight * 0.9
        elif stoch_k > 80 and stoch_k < stoch_d:  # Overbought with bearish cross
            bearish_score += weight * 0.9
        elif stoch_k > stoch_d:
            bullish_score += weight * 0.6
        else:
            bearish_score += weight * 0.6
        
        # ── Calculate Final Probabilities ─────────────────────────
        # Normalize to 0-100%
        profit_prob = (bullish_score / total_weight) * 100
        loss_prob = (bearish_score / total_weight) * 100
        
        # Adjust based on volatility regime
        if volatility_regime == "extreme":
            # High volatility increases uncertainty
            profit_prob *= 0.85
            loss_prob *= 0.85
        elif volatility_regime == "high":
            profit_prob *= 0.92
            loss_prob *= 0.92
        elif volatility_regime == "low":
            # Low volatility - less opportunity but more predictable
            profit_prob *= 0.95
            loss_prob *= 0.95
        
        # Ensure within bounds
        profit_prob = max(5.0, min(95.0, profit_prob))
        loss_prob = max(5.0, min(95.0, loss_prob))
        
        return profit_prob, loss_prob

    def _calculate_trade_targets(
        self,
        price: float,
        atr: float,
        trend: str,
        support: float,
        resistance: float,
        signal: str,
    ) -> Tuple[float, float, float]:
        """
        Calculate target price and stop loss based on ATR and support/resistance.
        
        Returns:
            Tuple of (target_price, stop_loss, risk_reward_ratio)
        """
        if price == 0 or atr == 0:
            return price, price, 0.0
        
        # Default ATR multipliers
        stop_multiplier = 1.5
        target_multiplier = 3.0  # Default 2:1 R/R
        
        if signal == "BUY" or trend == "bullish":
            # Long position
            stop_loss = max(price - (atr * stop_multiplier), support * 0.995)
            target = min(price + (atr * target_multiplier), resistance * 0.995)
            
            # Ensure stop is below entry
            if stop_loss >= price:
                stop_loss = price * 0.98
            
            # Ensure target is above entry
            if target <= price:
                target = price * 1.04
                
        elif signal == "SELL" or trend == "bearish":
            # Short position / Exit signal
            stop_loss = min(price + (atr * stop_multiplier), resistance * 1.005)
            target = max(price - (atr * target_multiplier), support * 1.005)
            
            # Ensure stop is above entry
            if stop_loss <= price:
                stop_loss = price * 1.02
            
            # Ensure target is below entry
            if target >= price:
                target = price * 0.96
        else:
            # Neutral - use simple ATR-based levels
            stop_loss = price - (atr * stop_multiplier)
            target = price + (atr * target_multiplier)
        
        # Calculate risk/reward ratio
        risk = abs(price - stop_loss)
        reward = abs(target - price)
        risk_reward = reward / risk if risk > 0 else 0.0
        
        return target, stop_loss, risk_reward

    # ═══════════════════════════════════════════════════════════════
    #  GENERATE TRADE SIGNAL (NEW)
    # ═══════════════════════════════════════════════════════════════

    def generate_trade_signal(self, market_state: MarketState) -> TradeSignal:
        """
        Generate a complete trade signal from market state.
        
        This is used by the controller to decide whether to execute a trade
        and to format notifications.
        
        Args:
            market_state: Analyzed market state
            
        Returns:
            TradeSignal with all details for execution and notification
        """
        # Collect reasons for the signal
        reasons = []
        
        if market_state.trend == "bullish":
            reasons.append(f"Bullish trend (strength: {market_state.trend_strength*100:.0f}%)")
        elif market_state.trend == "bearish":
            reasons.append(f"Bearish trend (strength: {market_state.trend_strength*100:.0f}%)")
        
        if market_state.rsi < 30:
            reasons.append(f"RSI oversold ({market_state.rsi:.1f})")
        elif market_state.rsi > 70:
            reasons.append(f"RSI overbought ({market_state.rsi:.1f})")
        
        if market_state.macd_cross == "bullish":
            reasons.append("MACD bullish crossover")
        elif market_state.macd_cross == "bearish":
            reasons.append("MACD bearish crossover")
        
        if market_state.volume_spike:
            reasons.append("Volume spike detected")
        
        if market_state.volume_pressure > 0.3:
            reasons.append(f"Strong buying pressure ({market_state.volume_pressure*100:.0f}%)")
        elif market_state.volume_pressure < -0.3:
            reasons.append(f"Strong selling pressure ({abs(market_state.volume_pressure)*100:.0f}%)")
        
        if market_state.structure_break:
            reasons.append(f"Structure break: {market_state.break_direction}")
        
        if market_state.bb_percent_b < 0.2:
            reasons.append("Price near lower Bollinger Band")
        elif market_state.bb_percent_b > 0.8:
            reasons.append("Price near upper Bollinger Band")
        
        if market_state.adx > 25:
            reasons.append(f"Strong trend (ADX: {market_state.adx:.1f})")
        
        if not reasons:
            reasons.append("Multiple indicators aligned")
        
        signal = TradeSignal(
            symbol=market_state.symbol,
            signal_type=market_state.entry_recommendation,
            probability=market_state.profit_probability,
            entry_price=market_state.suggested_entry,
            target_price=market_state.suggested_target,
            stop_loss=market_state.suggested_stop_loss,
            risk_reward=market_state.risk_reward_ratio,
            confidence=market_state.confidence_score * 100,
            reasons=reasons,
            timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
        )
        
        return signal

    def generate_exit_signal(
        self,
        market_state: MarketState,
        entry_price: float,
        current_pnl: float,
        hold_duration_minutes: int,
    ) -> TradeSignal:
        """
        Generate exit signal for an open position.
        
        Args:
            market_state: Current market analysis
            entry_price: Original entry price
            current_pnl: Current profit/loss in currency
            hold_duration_minutes: How long position has been held
            
        Returns:
            TradeSignal configured for exit
        """
        pnl_percent = ((market_state.price - entry_price) / entry_price * 100) if entry_price > 0 else 0
        
        reasons = []
        
        if market_state.should_exit:
            reasons.append(f"Loss probability ({market_state.loss_probability:.0f}%) exceeds profit probability")
        
        if market_state.macd_cross == "bearish" and current_pnl > 0:
            reasons.append("MACD bearish cross - secure profits")
        
        if market_state.rsi > 70:
            reasons.append("RSI overbought - potential reversal")
        
        if market_state.trend == "bearish" and current_pnl > 0:
            reasons.append("Trend turned bearish")
        
        if not reasons:
            reasons.append("Exit conditions met")
        
        signal = TradeSignal(
            symbol=market_state.symbol,
            signal_type="EXIT",
            probability=market_state.loss_probability,
            entry_price=entry_price,
            target_price=market_state.suggested_target,
            stop_loss=market_state.suggested_stop_loss,
            risk_reward=market_state.risk_reward_ratio,
            confidence=market_state.confidence_score * 100,
            reasons=reasons,
            timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
            exit_price=market_state.price,
            pnl_amount=current_pnl,
            pnl_percent=pnl_percent,
            hold_duration_minutes=hold_duration_minutes,
        )
        
        return signal

    # ═══════════════════════════════════════════════════════════════
    #  HOURLY ANALYSIS REPORT (NEW)
    # ═══════════════════════════════════════════════════════════════

    def generate_hourly_report(self, market_state: MarketState) -> HourlyAnalysisReport:
        """
        Generate hourly market analysis report.
        
        This is called by the scheduler every hour to send market updates
        even when not actively trading.
        
        Args:
            market_state: Current market analysis
            
        Returns:
            HourlyAnalysisReport ready for notification
        """
        # Generate opportunities based on conditions
        opportunities = []
        warnings = []
        
        # Bullish opportunities
        if market_state.rsi < 35 and market_state.trend != "bearish":
            opportunities.append("RSI approaching oversold - watch for bounce")
        
        if market_state.bb_percent_b < 0.15:
            opportunities.append("Price near lower BB - potential reversal zone")
        
        if market_state.macd_cross == "bullish":
            opportunities.append("Fresh MACD bullish crossover")
        
        if market_state.structure_break and market_state.break_direction == "up":
            opportunities.append("Bullish structure break detected")
        
        if market_state.volume_pressure > 0.4:
            opportunities.append("Strong buying volume pressure")
        
        if market_state.profit_probability >= 60:
            opportunities.append(f"BUY signal active ({market_state.profit_probability:.0f}% probability)")
        
        # Bearish warnings
        if market_state.rsi > 65:
            warnings.append("RSI approaching overbought territory")
        
        if market_state.bb_percent_b > 0.85:
            warnings.append("Price extended - near upper Bollinger Band")
        
        if market_state.macd_cross == "bearish":
            warnings.append("MACD bearish crossover - caution")
        
        if market_state.volume_pressure < -0.3:
            warnings.append("Selling pressure increasing")
        
        if market_state.volatility_regime == "extreme":
            warnings.append("⚠️ Extreme volatility - reduce position sizes")
        elif market_state.volatility_regime == "high":
            warnings.append("High volatility conditions")
        
        if market_state.loss_probability > market_state.profit_probability:
            warnings.append("Higher probability of downside - wait for better entry")
        
        # Calculate buy/sell probabilities
        buy_prob = market_state.profit_probability if market_state.signal == "BUY" else market_state.profit_probability * 0.7
        sell_prob = market_state.loss_probability if market_state.signal == "SELL" else market_state.loss_probability * 0.7
        
        report = HourlyAnalysisReport(
            symbol=market_state.symbol,
            timestamp=datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
            price=market_state.price,
            trend=market_state.trend,
            trend_strength=market_state.trend_strength,
            rsi=market_state.rsi,
            rsi_signal=market_state.rsi_signal,
            macd_cross=market_state.macd_cross,
            adx=market_state.adx,
            volatility_regime=market_state.volatility_regime,
            atr_percent=market_state.atr_percent,
            volume_pressure=market_state.volume_pressure,
            volume_spike=market_state.volume_spike,
            opportunities=opportunities,
            warnings=warnings,
            buy_probability=buy_prob,
            sell_probability=sell_prob,
        )
        
        return report

    # ═══════════════════════════════════════════════════════════════
    #  CORE INDICATORS
    # ═══════════════════════════════════════════════════════════════

    def _ema(self, prices: List[float], period: int) -> float:
        """Exponential Moving Average - O(n)."""
        if not prices:
            return 0.0

        period = min(period, len(prices))
        k = 2 / (period + 1)
        ema = prices[0]

        for price in prices[1:]:
            ema = price * k + ema * (1 - k)

        return ema

    def _ema_series(self, prices: List[float], period: int) -> List[float]:
        """Calculate EMA series - O(n)."""
        if not prices:
            return []

        k = 2 / (period + 1)
        ema_values = [prices[0]]

        for i in range(1, len(prices)):
            ema = prices[i] * k + ema_values[-1] * (1 - k)
            ema_values.append(ema)

        return ema_values

    def _sma(self, prices: List[float], period: int) -> float:
        """Simple Moving Average."""
        if len(prices) < period:
            return mean(prices) if prices else 0.0
        return mean(prices[-period:])

    def _rsi_wilder(self, prices: List[float], period: int = 14) -> float:
        """RSI using Wilder's smoothing."""
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

        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _stochastic_rsi(
        self,
        prices: List[float],
        rsi_period: int = 14,
        k_period: int = 3,
        d_period: int = 3
    ) -> Tuple[float, float]:
        """Stochastic RSI."""
        if len(prices) < rsi_period + k_period:
            return 50.0, 50.0

        # Calculate RSI series
        rsi_values = []
        for i in range(rsi_period, len(prices) + 1):
            rsi = self._rsi_wilder(prices[:i], rsi_period)
            rsi_values.append(rsi)

        if len(rsi_values) < k_period:
            return 50.0, 50.0

        # Calculate Stochastic of RSI
        stoch_values = []
        for i in range(k_period - 1, len(rsi_values)):
            window = rsi_values[i - k_period + 1:i + 1]
            high = max(window)
            low = min(window)
            if high - low > 0:
                stoch = ((rsi_values[i] - low) / (high - low)) * 100
            else:
                stoch = 50.0
            stoch_values.append(stoch)

        if not stoch_values:
            return 50.0, 50.0

        k = stoch_values[-1]
        d = mean(stoch_values[-d_period:]) if len(stoch_values) >= d_period else k

        return k, d

    def _classify_rsi(self, rsi: float) -> str:
        """Classify RSI into signal zones."""
        if rsi >= 70:
            return "overbought"
        elif rsi <= 30:
            return "oversold"
        elif rsi >= 60:
            return "bullish"
        elif rsi <= 40:
            return "bearish"
        return "neutral"

    def _atr(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 14,
    ) -> float:
        """Average True Range."""
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

    # ═══════════════════════════════════════════════════════════════
    #  MACD
    # ═══════════════════════════════════════════════════════════════

    def _macd_optimized(
        self,
        prices: List[float],
        fast: int = 12,
        slow: int = 26,
        signal: int = 9,
    ) -> Tuple[float, float, float]:
        """MACD - O(n) optimized."""
        if len(prices) < slow:
            return 0.0, 0.0, 0.0

        ema_fast_series = self._ema_series(prices, fast)
        ema_slow_series = self._ema_series(prices, slow)

        macd_series = [
            ema_fast_series[i] - ema_slow_series[i]
            for i in range(len(prices))
        ]

        signal_series = self._ema_series(macd_series[slow - 1:], signal)

        macd_line = macd_series[-1]
        signal_line = signal_series[-1] if signal_series else macd_line
        histogram = macd_line - signal_line

        return macd_line, signal_line, histogram

    def _detect_macd_cross(self, prices: List[float]) -> str:
        """Detect MACD crossover in recent candles."""
        if len(prices) < 30:
            return "none"

        # Calculate MACD for last 5 periods
        crosses = []
        for i in range(-5, 0):
            subset = prices[:len(prices) + i + 1]
            if len(subset) >= 26:
                line, sig, _ = self._macd_optimized(subset)
                crosses.append(line - sig)

        if len(crosses) < 2:
            return "none"

        # Check for crossover
        if crosses[-2] < 0 and crosses[-1] > 0:
            return "bullish"
        elif crosses[-2] > 0 and crosses[-1] < 0:
            return "bearish"

        return "none"

    def _detect_macd_divergence(
        self,
        prices: List[float],
        macd_hist: float
    ) -> str:
        """Detect MACD divergence."""
        if len(prices) < 20:
            return "none"

        # Price making higher highs but MACD making lower highs = bearish divergence
        # Price making lower lows but MACD making higher lows = bullish divergence

        recent_prices = prices[-20:]
        mid = len(recent_prices) // 2

        price_trend = recent_prices[-1] - recent_prices[0]
        
        # Simplified divergence detection
        if price_trend > 0 and macd_hist < 0:
            return "bearish"
        elif price_trend < 0 and macd_hist > 0:
            return "bullish"

        return "none"

    # ═══════════════════════════════════════════════════════════════
    #  BOLLINGER BANDS
    # ═══════════════════════════════════════════════════════════════

    def _bollinger(
        self,
        prices: List[float],
        period: int = 20,
        num_std: float = 2.0,
    ) -> Tuple[float, float, float, float]:
        """Bollinger Bands."""
        if len(prices) < period:
            period = len(prices)

        window = prices[-period:]
        mid = mean(window)
        std = stdev(window) if len(window) > 1 else 0

        upper = mid + num_std * std
        lower = mid - num_std * std
        width = (upper - lower) / mid if mid != 0 else 0

        return upper, mid, lower, width

    def _bollinger_percent_b(self, price: float, upper: float, lower: float) -> float:
        """Position within Bollinger Bands (0-1)."""
        band_range = upper - lower
        if band_range == 0:
            return 0.5
        return (price - lower) / band_range

    def _detect_bb_squeeze(
        self,
        closes: List[float],
        highs: List[float],
        lows: List[float],
        atr: float
    ) -> bool:
        """Detect Bollinger Band squeeze (low volatility before breakout)."""
        if len(closes) < 20:
            return False

        _, _, _, current_width = self._bollinger(closes, 20, 2.0)
        _, _, _, prev_width = self._bollinger(closes[:-5], 20, 2.0)

        # Squeeze when width is contracting significantly
        return current_width < prev_width * 0.8

    # ═══════════════════════════════════════════════════════════════
    #  ADX (Average Directional Index)
    # ═══════════════════════════════════════════════════════════════

    def _adx(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float],
        period: int = 14
    ) -> Tuple[float, float, float]:
        """Calculate ADX, +DI, -DI."""
        if len(closes) < period + 1:
            return 0.0, 0.0, 0.0

        plus_dm = []
        minus_dm = []
        tr_list = []

        for i in range(1, len(closes)):
            high_diff = highs[i] - highs[i - 1]
            low_diff = lows[i - 1] - lows[i]

            plus = high_diff if high_diff > low_diff and high_diff > 0 else 0
            minus = low_diff if low_diff > high_diff and low_diff > 0 else 0

            plus_dm.append(plus)
            minus_dm.append(minus)

            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_list.append(tr)

        if len(tr_list) < period:
            return 0.0, 0.0, 0.0

        # Smoothed values
        smoothed_plus_dm = sum(plus_dm[:period])
        smoothed_minus_dm = sum(minus_dm[:period])
        smoothed_tr = sum(tr_list[:period])

        for i in range(period, len(tr_list)):
            smoothed_plus_dm = smoothed_plus_dm - (smoothed_plus_dm / period) + plus_dm[i]
            smoothed_minus_dm = smoothed_minus_dm - (smoothed_minus_dm / period) + minus_dm[i]
            smoothed_tr = smoothed_tr - (smoothed_tr / period) + tr_list[i]

        if smoothed_tr == 0:
            return 0.0, 0.0, 0.0

        plus_di = (smoothed_plus_dm / smoothed_tr) * 100
        minus_di = (smoothed_minus_dm / smoothed_tr) * 100

        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0.0, plus_di, minus_di

        dx = abs(plus_di - minus_di) / di_sum * 100

        # ADX is smoothed DX
        adx = dx  # Simplified; full implementation would smooth over period

        return adx, plus_di, minus_di

    # ═══════════════════════════════════════════════════════════════
    #  VOLATILITY
    # ═══════════════════════════════════════════════════════════════

    def _normalize_volatility(self, closes: List[float], period: int = 20) -> float:
        """Normalized volatility (0-1)."""
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

        return min(1.0, annualized)

    def _classify_volatility(self, volatility_pct: float) -> str:
        """Classify volatility regime."""
        if volatility_pct > 0.025:
            return "extreme"
        elif volatility_pct > 0.015:
            return "high"
        elif volatility_pct < 0.005:
            return "low"
        return "normal"

    def _is_volatility_expanding(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float]
    ) -> bool:
        """Check if volatility is expanding."""
        if len(closes) < 20:
            return False

        recent_atr = self._atr(highs[-10:], lows[-10:], closes[-10:], 10)
        prior_atr = self._atr(highs[-20:-10], lows[-20:-10], closes[-20:-10], 10)

        return recent_atr > prior_atr * 1.2

    # ═══════════════════════════════════════════════════════════════
    #  VOLUME ANALYSIS
    # ═══════════════════════════════════════════════════════════════

    def _volume_pressure(self, candles: List[Dict]) -> float:
        """Volume pressure: -1 (bearish) to +1 (bullish)."""
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

    def _obv_trend(self, closes: List[float], volumes: List[float]) -> str:
        """On-Balance Volume trend."""
        if len(closes) < 20 or len(volumes) < 20:
            return "neutral"

        obv = 0
        obv_values = []

        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv += volumes[i]
            elif closes[i] < closes[i - 1]:
                obv -= volumes[i]
            obv_values.append(obv)

        if len(obv_values) < 10:
            return "neutral"

        recent_obv = mean(obv_values[-5:])
        prior_obv = mean(obv_values[-15:-5])

        if recent_obv > prior_obv * 1.05:
            return "bullish"
        elif recent_obv < prior_obv * 0.95:
            return "bearish"
        return "neutral"

    def _liquidity_score(self, volumes: List[float]) -> float:
        """Liquidity score based on volume consistency."""
        if len(volumes) < 10:
            return 0.5

        recent = volumes[-30:] if len(volumes) >= 30 else volumes
        avg = mean(recent)
        std = stdev(recent) if len(recent) > 1 else 0

        if std == 0:
            return 1.0

        cv = std / avg if avg > 0 else 1
        return min(1.0, 1 / (1 + cv))

    # ═══════════════════════════════════════════════════════════════
    #  TREND ANALYSIS
    # ═══════════════════════════════════════════════════════════════

    def _classify_trend(
        self,
        price: float,
        ema_9: float,
        ema_20: float,
        ema_50: float,
        ema_200: float
    ) -> str:
        """Classify trend based on EMA alignment and price position."""
        bullish_signals = 0
        bearish_signals = 0

        # EMA alignment
        if ema_9 > ema_20 > ema_50:
            bullish_signals += 2
        elif ema_9 < ema_20 < ema_50:
            bearish_signals += 2

        # Price vs EMAs
        if price > ema_20 and price > ema_50:
            bullish_signals += 1
        elif price < ema_20 and price < ema_50:
            bearish_signals += 1

        # EMA 200 (long-term)
        if price > ema_200:
            bullish_signals += 1
        elif price < ema_200:
            bearish_signals += 1

        if bullish_signals >= 3:
            return "bullish"
        elif bearish_signals >= 3:
            return "bearish"
        return "sideways"

    def _calculate_trend_strength(
        self,
        price: float,
        ema_20: float,
        ema_50: float,
        adx: float,
        macd_hist: float
    ) -> float:
        """Calculate trend strength (0-1)."""
        strength = 0.0

        # EMA spread
        if price > 0:
            ema_spread = abs(ema_20 - ema_50) / price
            strength += min(0.3, ema_spread * 10)

        # ADX contribution
        strength += min(0.4, adx / 100)

        # MACD histogram strength
        if price > 0:
            macd_strength = abs(macd_hist) / price * 1000
            strength += min(0.3, macd_strength)

        return min(1.0, strength)

    def _classify_regime(
        self,
        volatility_regime: str,
        trend: str,
        adx: float
    ) -> str:
        """Classify market regime."""
        if volatility_regime in ("high", "extreme"):
            return "explosive"
        if adx < 20 or trend == "sideways":
            return "ranging"
        return "trending"

    def _classify_sentiment(
        self,
        trend: str,
        rsi: float,
        macd_hist: float,
        volume_pressure: float
    ) -> str:
        """Classify market sentiment."""
        bullish_count = 0
        bearish_count = 0

        if trend == "bullish":
            bullish_count += 1
        elif trend == "bearish":
            bearish_count += 1

        if rsi > 60:
            bullish_count += 1
        elif rsi < 40:
            bearish_count += 1

        if macd_hist > 0:
            bullish_count += 1
        elif macd_hist < 0:
            bearish_count += 1

        if volume_pressure > 0.2:
            bullish_count += 1
        elif volume_pressure < -0.2:
            bearish_count += 1

        if bullish_count >= 3:
            return "strong_bullish"
        elif bullish_count >= 2:
            return "weak_bullish"
        elif bearish_count >= 3:
            return "strong_bearish"
        elif bearish_count >= 2:
            return "weak_bearish"
        return "neutral"

    # ═══════════════════════════════════════════════════════════════
    #  MOMENTUM
    # ═══════════════════════════════════════════════════════════════

    def _momentum_strength(self, closes: List[float]) -> float:
        """Average absolute return over last 10 bars."""
        if len(closes) < 11:
            return 0.0

        returns = [
            abs(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(len(closes) - 10, len(closes))
            if closes[i - 1] > 0
        ]

        return mean(returns) if returns else 0.0

    def _momentum_acceleration(self, closes: List[float]) -> float:
        """Momentum acceleration."""
        if len(closes) < 15:
            return 0.0

        recent = [closes[i] - closes[i - 1] for i in range(-5, 0)]
        prior = [closes[i] - closes[i - 1] for i in range(-15, -5)]

        return mean(recent) - mean(prior)

    def _rate_of_change(self, closes: List[float], period: int = 10) -> float:
        """Rate of Change indicator."""
        if len(closes) < period + 1:
            return 0.0

        if closes[-period - 1] == 0:
            return 0.0

        return ((closes[-1] - closes[-period - 1]) / closes[-period - 1]) * 100

    # ═══════════════════════════════════════════════════════════════
    #  MARKET STRUCTURE
    # ═══════════════════════════════════════════════════════════════

    def _support_resistance(
        self,
        highs: List[float],
        lows: List[float],
        closes: List[float],
        lookback: int = 30
    ) -> Tuple[float, float]:
        """Calculate support and resistance levels."""
        lookback = min(lookback, len(highs))

        # Simple approach: recent high/low
        resistance = max(highs[-lookback:])
        support = min(lows[-lookback:])

        return support, resistance

    def _pivot_point(self, high: float, low: float, close: float) -> float:
        """Classic pivot point."""
        return (high + low + close) / 3

    def _detect_structure_break(
        self,
        closes: List[float],
        highs: List[float],
        lows: List[float]
    ) -> Tuple[bool, str]:
        """Detect structure break."""
        if len(closes) < 21:
            return False, "none"

        recent_high = max(highs[-20:-1])
        recent_low = min(lows[-20:-1])
        current = closes[-1]

        if current > recent_high:
            return True, "up"
        elif current < recent_low:
            return True, "down"

        return False, "none"

    def _price_vs_emas(
        self,
        price: float,
        ema_9: float,
        ema_20: float,
        ema_50: float,
        ema_200: float
    ) -> str:
        """Analyze price position relative to EMAs."""
        above_count = sum([
            price > ema_9,
            price > ema_20,
            price > ema_50,
            price > ema_200
        ])

        if above_count == 4:
            return "above_all"
        elif above_count == 0:
            return "below_all"
        elif above_count >= 2:
            return "mostly_above"
        else:
            return "mostly_below"

    def _check_higher_highs(self, highs: List[float]) -> bool:
        """Check for higher highs pattern."""
        if len(highs) < 10:
            return False

        recent = highs[-5:]
        prior = highs[-10:-5]

        return max(recent) > max(prior)

    def _check_lower_lows(self, lows: List[float]) -> bool:
        """Check for lower lows pattern."""
        if len(lows) < 10:
            return False

        recent = lows[-5:]
        prior = lows[-10:-5]

        return min(recent) < min(prior)

    def _is_consolidating(self, closes: List[float], atr: float) -> bool:
        """Check if price is consolidating."""
        if len(closes) < 10 or atr == 0:
            return False

        recent_range = max(closes[-10:]) - min(closes[-10:])
        expected_range = atr * 3

        return recent_range < expected_range

    # ═══════════════════════════════════════════════════════════════
    #  SIGNAL GENERATION
    # ═══════════════════════════════════════════════════════════════

    def _generate_signal(
        self,
        trend: str,
        rsi: float,
        macd_cross: str,
        macd_hist: float,
        bb_percent_b: float,
        volume_pressure: float,
        adx: float,
        price_vs_ema: str,
        structure_break: bool,
        break_direction: str,
    ) -> Tuple[str, float]:
        """
        Generate trading signal based on multiple indicators.
        
        Returns:
            (signal, strength) where signal is BUY/SELL/HOLD
            and strength is 0-1
        """
        buy_score = 0.0
        sell_score = 0.0

        # Trend alignment (weight: 25%)
        if trend == "bullish":
            buy_score += 0.25
        elif trend == "bearish":
            sell_score += 0.25

        # RSI (weight: 20%)
        if rsi < 30:
            buy_score += 0.20
        elif rsi < 40:
            buy_score += 0.10
        elif rsi > 70:
            sell_score += 0.20
        elif rsi > 60:
            sell_score += 0.10

        # MACD cross (weight: 20%)
        if macd_cross == "bullish":
            buy_score += 0.20
        elif macd_cross == "bearish":
            sell_score += 0.20
        elif macd_hist > 0:
            buy_score += 0.10
        elif macd_hist < 0:
            sell_score += 0.10

        # Bollinger position (weight: 15%)
        if bb_percent_b < 0.2:
            buy_score += 0.15
        elif bb_percent_b > 0.8:
            sell_score += 0.15

        # Volume pressure (weight: 10%)
        if volume_pressure > 0.3:
            buy_score += 0.10
        elif volume_pressure < -0.3:
            sell_score += 0.10

        # ADX (trend strength bonus)
        if adx > 25:
            if trend == "bullish":
                buy_score += 0.05
            elif trend == "bearish":
                sell_score += 0.05

        # Price vs EMA alignment (weight: 5%)
        if price_vs_ema == "above_all":
            buy_score += 0.05
        elif price_vs_ema == "below_all":
            sell_score += 0.05

        # Structure break (weight: 5%)
        if structure_break:
            if break_direction == "up":
                buy_score += 0.05
            elif break_direction == "down":
                sell_score += 0.05

        # Determine signal
        threshold = 0.35

        if buy_score >= threshold and buy_score > sell_score:
            return "BUY", min(1.0, buy_score)
        elif sell_score >= threshold and sell_score > buy_score:
            return "SELL", min(1.0, sell_score)
        else:
            return "HOLD", max(buy_score, sell_score)

    # ═══════════════════════════════════════════════════════════════
    #  CONFIDENCE SCORING
    # ═══════════════════════════════════════════════════════════════

    def _compute_confidence(
        self,
        trend_strength: float,
        momentum_strength: float,
        volume_pressure: float,
        liquidity_score: float,
        adx: float,
        rsi: float,
    ) -> float:
        """Compute overall confidence score."""
        score = 0.0

        # Trend strength (25%)
        score += trend_strength * 0.25

        # Momentum (20%)
        score += min(1.0, momentum_strength * 100) * 0.20

        # Volume clarity (20%)
        score += abs(volume_pressure) * 0.20

        # Liquidity (15%)
        score += liquidity_score * 0.15

        # ADX (trend confirmation) (10%)
        score += min(1.0, adx / 50) * 0.10

        # RSI clarity (extremes = higher confidence) (10%)
        rsi_clarity = abs(rsi - 50) / 50
        score += rsi_clarity * 0.10

        return min(1.0, score)

    # ═══════════════════════════════════════════════════════════════
    #  BRAIN FEEDS
    # ═══════════════════════════════════════════════════════════════

    def _build_indicators_dict(
        self,
        rsi: float,
        stoch_k: float,
        stoch_d: float,
        macd_line: float,
        macd_signal: float,
        macd_hist: float,
        macd_cross: str,
        ema_9: float,
        ema_20: float,
        ema_50: float,
        ema_200: float,
        price: float,
        bb_upper: float,
        bb_lower: float,
        bb_mid: float,
        bb_percent_b: float,
        adx: float,
        plus_di: float,
        minus_di: float,
        atr: float,
    ) -> Dict:
        """Build indicators dict for Brain1."""
        # EMA cross detection
        if ema_9 > ema_20 > ema_50:
            ema_cross = "bullish"
        elif ema_9 < ema_20 < ema_50:
            ema_cross = "bearish"
        else:
            ema_cross = "mixed"

        # Bollinger position
        if bb_percent_b <= 0.2:
            bb_position = "oversold"
        elif bb_percent_b >= 0.8:
            bb_position = "overbought"
        else:
            bb_position = "mid"

        # RSI condition
        if rsi <= 30:
            rsi_condition = "oversold"
        elif rsi >= 70:
            rsi_condition = "overbought"
        elif rsi > 50:
            rsi_condition = "bullish"
        else:
            rsi_condition = "bearish"

        return {
            # RSI
            "rsi": rsi,
            "rsi_condition": rsi_condition,
            "stoch_rsi_k": stoch_k,
            "stoch_rsi_d": stoch_d,
            
            # MACD
            "macd_line": macd_line,
            "macd_signal": macd_signal,
            "macd_histogram": macd_hist,
            "macd_cross": macd_cross,
            
            # EMAs
            "ema_9": ema_9,
            "ema_20": ema_20,
            "ema_50": ema_50,
            "ema_200": ema_200,
            "ema_cross": ema_cross,
            
            # Bollinger
            "bb_upper": bb_upper,
            "bb_mid": bb_mid,
            "bb_lower": bb_lower,
            "bb_percent_b": bb_percent_b,
            "bb_position": bb_position,
            
            # ADX
            "adx": adx,
            "plus_di": plus_di,
            "minus_di": minus_di,
            
            # ATR
            "atr": atr,
        }

    def _build_sentiment_score(
        self,
        trend: str,
        rsi: float,
        macd_hist: float,
        volume_pressure: float,
        momentum_accel: float,
        adx: float,
        stoch_k: float,
    ) -> float:
        """Build sentiment score (-1 to +1) for Brain2."""
        score = 0.0

        # Trend (30%)
        if trend == "bullish":
            score += 0.30
        elif trend == "bearish":
            score -= 0.30

        # RSI (20%)
        rsi_norm = (rsi - 50) / 50
        score += rsi_norm * 0.20

        # MACD histogram (20%)
        if macd_hist > 0:
            score += 0.20
        elif macd_hist < 0:
            score -= 0.20

        # Volume pressure (15%)
        score += volume_pressure * 0.15

        # Stochastic RSI (10%)
        stoch_norm = (stoch_k - 50) / 50
        score += stoch_norm * 0.10

        # ADX direction (5%)
        if adx > 25:
            if trend == "bullish":
                score += 0.05
            elif trend == "bearish":
                score -= 0.05

        return max(-1.0, min(1.0, score))

    # ═══════════════════════════════════════════════════════════════
    #  PATTERN RECOGNITION
    # ═══════════════════════════════════════════════════════════════

    def _detect_chart_pattern(
        self,
        opens: List[float],
        highs: List[float],
        lows: List[float],
        closes: List[float],
        volumes: List[float],
        volume_pressure: float,
        structure_break: bool,
    ) -> Dict:
        """Detect candlestick and chart patterns."""
        patterns_found = []

        if len(closes) < 3:
            return {"signal": "HOLD", "confidence": 0, "pattern_name": "None", "patterns": []}

        # ── Bullish Patterns ──────────────────────────────────────
        if self._is_bullish_engulfing(opens, highs, lows, closes):
            patterns_found.append(("BUY", 70, "Bullish Engulfing"))

        if self._is_hammer(opens[-1], highs[-1], lows[-1], closes[-1]):
            patterns_found.append(("BUY", 60, "Hammer"))

        if self._is_morning_star(opens, closes):
            patterns_found.append(("BUY", 75, "Morning Star"))

        if self._is_bullish_harami(opens, closes):
            patterns_found.append(("BUY", 55, "Bullish Harami"))

        if self._is_three_white_soldiers(opens, closes):
            patterns_found.append(("BUY", 80, "Three White Soldiers"))

        if len(lows) >= 20 and self._is_double_bottom(lows[-20:]):
            patterns_found.append(("BUY", 65, "Double Bottom"))

        if len(lows) >= 30 and self._is_inverse_head_shoulders(lows[-30:]):
            patterns_found.append(("BUY", 75, "Inverse H&S"))

        # ── Bearish Patterns ──────────────────────────────────────
        if self._is_bearish_engulfing(opens, highs, lows, closes):
            patterns_found.append(("SELL", 70, "Bearish Engulfing"))

        if self._is_shooting_star(opens[-1], highs[-1], lows[-1], closes[-1]):
            patterns_found.append(("SELL", 60, "Shooting Star"))

        if self._is_evening_star(opens, closes):
            patterns_found.append(("SELL", 75, "Evening Star"))

        if self._is_bearish_harami(opens, closes):
            patterns_found.append(("SELL", 55, "Bearish Harami"))

        if self._is_three_black_crows(opens, closes):
            patterns_found.append(("SELL", 80, "Three Black Crows"))

        if len(highs) >= 20 and self._is_double_top(highs[-20:]):
            patterns_found.append(("SELL", 65, "Double Top"))

        if len(highs) >= 30 and self._is_head_shoulders(highs[-30:]):
            patterns_found.append(("SELL", 75, "Head & Shoulders"))

        # ── Neutral / Continuation ────────────────────────────────
        if self._is_doji(opens[-1], closes[-1], highs[-1], lows[-1]):
            patterns_found.append(("HOLD", 40, "Doji"))

        # ── Structure Break Overlay ───────────────────────────────
        if structure_break:
            if closes[-1] > closes[-2]:
                patterns_found.append(("BUY", 55, "Breakout Up"))
            else:
                patterns_found.append(("SELL", 55, "Breakout Down"))

        if not patterns_found:
            return {"signal": "HOLD", "confidence": 0, "pattern_name": "None", "patterns": []}

        # Pick highest confidence pattern
        best = max(patterns_found, key=lambda x: x[1])
        signal, conf, name = best

        # Volume confirmation boost
        if signal == "BUY" and volume_pressure > 0.2:
            conf = min(100, conf + 10)
        elif signal == "SELL" and volume_pressure < -0.2:
            conf = min(100, conf + 10)

        return {
            "signal": signal,
            "confidence": conf,
            "pattern_name": name,
            "patterns": [p[2] for p in patterns_found],
        }

    # ── Candlestick Pattern Helpers ───────────────────────────────

    def _is_bullish_engulfing(self, opens, highs, lows, closes) -> bool:
        if len(closes) < 2:
            return False
        prev_bearish = closes[-2] < opens[-2]
        curr_bullish = closes[-1] > opens[-1]
        engulfs = opens[-1] < closes[-2] and closes[-1] > opens[-2]
        return prev_bearish and curr_bullish and engulfs

    def _is_bearish_engulfing(self, opens, highs, lows, closes) -> bool:
        if len(closes) < 2:
            return False
        prev_bullish = closes[-2] > opens[-2]
        curr_bearish = closes[-1] < opens[-1]
        engulfs = opens[-1] > closes[-2] and closes[-1] < opens[-2]
        return prev_bullish and curr_bearish and engulfs

    def _is_hammer(self, open_p, high_p, low_p, close_p) -> bool:
        body = abs(close_p - open_p)
        total_range = high_p - low_p
        if total_range == 0:
            return False
        lower_wick = min(open_p, close_p) - low_p
        upper_wick = high_p - max(open_p, close_p)
        return (
            lower_wick / total_range > 0.6 and
            body / total_range < 0.3 and
            upper_wick / total_range < 0.1
        )

    def _is_shooting_star(self, open_p, high_p, low_p, close_p) -> bool:
        body = abs(close_p - open_p)
        total_range = high_p - low_p
        if total_range == 0:
            return False
        upper_wick = high_p - max(open_p, close_p)
        lower_wick = min(open_p, close_p) - low_p
        return (
            upper_wick / total_range > 0.6 and
            body / total_range < 0.3 and
            lower_wick / total_range < 0.1
        )

    def _is_morning_star(self, opens, closes) -> bool:
        if len(closes) < 3:
            return False
        first_bearish = closes[-3] < opens[-3]
        second_small = abs(closes[-2] - opens[-2]) < abs(closes[-3] - opens[-3]) * 0.3
        third_bullish = closes[-1] > opens[-1]
        third_strong = closes[-1] > (opens[-3] + closes[-3]) / 2
        return first_bearish and second_small and third_bullish and third_strong

    def _is_evening_star(self, opens, closes) -> bool:
        if len(closes) < 3:
            return False
        first_bullish = closes[-3] > opens[-3]
        second_small = abs(closes[-2] - opens[-2]) < abs(closes[-3] - opens[-3]) * 0.3
        third_bearish = closes[-1] < opens[-1]
        third_strong = closes[-1] < (opens[-3] + closes[-3]) / 2
        return first_bullish and second_small and third_bearish and third_strong

    def _is_bullish_harami(self, opens, closes) -> bool:
        if len(closes) < 2:
            return False
        prev_bearish = closes[-2] < opens[-2]
        curr_bullish = closes[-1] > opens[-1]
        inside = opens[-1] > closes[-2] and closes[-1] < opens[-2]
        return prev_bearish and curr_bullish and inside

    def _is_bearish_harami(self, opens, closes) -> bool:
        if len(closes) < 2:
            return False
        prev_bullish = closes[-2] > opens[-2]
        curr_bearish = closes[-1] < opens[-1]
        inside = opens[-1] < closes[-2] and closes[-1] > opens[-2]
        return prev_bullish and curr_bearish and inside

    def _is_three_white_soldiers(self, opens, closes) -> bool:
        if len(closes) < 3:
            return False
        for i in range(-3, 0):
            if closes[i] <= opens[i]:  # Must be bullish
                return False
            if i > -3 and closes[i] <= closes[i - 1]:  # Must be higher close
                return False
        return True

    def _is_three_black_crows(self, opens, closes) -> bool:
        if len(closes) < 3:
            return False
        for i in range(-3, 0):
            if closes[i] >= opens[i]:  # Must be bearish
                return False
            if i > -3 and closes[i] >= closes[i - 1]:  # Must be lower close
                return False
        return True

    def _is_doji(self, open_p, close_p, high_p, low_p) -> bool:
        body = abs(close_p - open_p)
        total_range = high_p - low_p
        if total_range == 0:
            return False
        return body / total_range < 0.1

    def _is_double_bottom(self, lows: List[float]) -> bool:
        if len(lows) < 10:
            return False
        mid = len(lows) // 2
        left_low = min(lows[:mid])
        right_low = min(lows[mid:])
        middle_high = max(lows[mid - 2:mid + 2]) if mid >= 2 else max(lows)
        lows_similar = abs(left_low - right_low) / max(left_low, right_low) < 0.02
        middle_higher = middle_high > left_low * 1.01 and middle_high > right_low * 1.01
        return lows_similar and middle_higher

    def _is_double_top(self, highs: List[float]) -> bool:
        if len(highs) < 10:
            return False
        mid = len(highs) // 2
        left_high = max(highs[:mid])
        right_high = max(highs[mid:])
        middle_low = min(highs[mid - 2:mid + 2]) if mid >= 2 else min(highs)
        highs_similar = abs(left_high - right_high) / max(left_high, right_high) < 0.02
        middle_lower = middle_low < left_high * 0.99 and middle_low < right_high * 0.99
        return highs_similar and middle_lower

    def _is_head_shoulders(self, highs: List[float]) -> bool:
        """Simplified head and shoulders detection."""
        if len(highs) < 15:
            return False
        third = len(highs) // 3
        left = max(highs[:third])
        head = max(highs[third:2*third])
        right = max(highs[2*third:])
        return head > left and head > right and abs(left - right) / max(left, right) < 0.05

    def _is_inverse_head_shoulders(self, lows: List[float]) -> bool:
        """Simplified inverse head and shoulders detection."""
        if len(lows) < 15:
            return False
        third = len(lows) // 3
        left = min(lows[:third])
        head = min(lows[third:2*third])
        right = min(lows[2*third:])
        return head < left and head < right and abs(left - right) / max(left, right) < 0.05

    # ═══════════════════════════════════════════════════════════════
    #  EXIT MONITORING (NEW)
    # ═══════════════════════════════════════════════════════════════

    def should_exit_position(
        self,
        market_state: MarketState,
        entry_price: float,
        current_pnl_pct: float,
        hold_duration_minutes: int,
    ) -> Tuple[bool, str, float]:
        """
        Monitor if an open position should be exited.
        
        Checks multiple exit conditions:
        1. Loss probability exceeds profit probability
        2. Stop loss hit
        3. Target reached
        4. Trend reversal
        5. Maximum hold time exceeded
        
        Args:
            market_state: Current market analysis
            entry_price: Position entry price
            current_pnl_pct: Current P/L percentage
            hold_duration_minutes: Minutes since entry
            
        Returns:
            Tuple of (should_exit, reason, urgency_score 0-1)
        """
        exit_reasons = []
        urgency = 0.0
        
        # 1. Probability-based exit
        if market_state.loss_probability > market_state.profit_probability:
            diff = market_state.loss_probability - market_state.profit_probability
            if diff > 20:
                exit_reasons.append(f"High loss probability ({market_state.loss_probability:.0f}%)")
                urgency = max(urgency, 0.9)
            elif diff > 10:
                exit_reasons.append(f"Rising loss probability ({market_state.loss_probability:.0f}%)")
                urgency = max(urgency, 0.7)
        
        # 2. Stop loss check
        if market_state.price <= market_state.suggested_stop_loss:
            exit_reasons.append("Stop loss triggered")
            urgency = max(urgency, 1.0)
        
        # 3. Target reached
        if market_state.price >= market_state.suggested_target and current_pnl_pct > 0:
            exit_reasons.append("Target price reached")
            urgency = max(urgency, 0.8)
        
        # 4. Trend reversal with profit
        if current_pnl_pct > 1.0:  # In profit
            if market_state.trend == "bearish" and market_state.trend_strength > 0.5:
                exit_reasons.append("Trend reversal - secure profits")
                urgency = max(urgency, 0.75)
            
            if market_state.macd_cross == "bearish":
                exit_reasons.append("MACD bearish cross - secure profits")
                urgency = max(urgency, 0.7)
        
        # 5. RSI overbought with profit
        if market_state.rsi > 75 and current_pnl_pct > 0.5:
            exit_reasons.append("RSI overbought - consider taking profits")
            urgency = max(urgency, 0.6)
        
        # 6. Maximum hold time (optional: 4 hours for scalping)
        if hold_duration_minutes > 240:
            exit_reasons.append("Maximum hold time exceeded")
            urgency = max(urgency, 0.5)
        
        # 7. Significant loss
        if current_pnl_pct < -2.0:
            exit_reasons.append("Significant loss - cut losses")
            urgency = max(urgency, 0.85)
        
        # 8. Volatility spike (protect capital)
        if market_state.volatility_regime == "extreme" and current_pnl_pct > 0:
            exit_reasons.append("Extreme volatility - protect profits")
            urgency = max(urgency, 0.7)
        
        should_exit = len(exit_reasons) > 0 and urgency >= 0.5
        reason = exit_reasons[0] if exit_reasons else "No exit signal"
        
        return should_exit, reason, urgency

    # ═══════════════════════════════════════════════════════════════
    #  MULTI-SYMBOL ANALYSIS (NEW)
    # ═══════════════════════════════════════════════════════════════

    def generate_multi_symbol_report(
        self,
        market_states: Dict[str, MarketState]
    ) -> str:
        """
        Generate a combined report for multiple symbols.
        
        Args:
            market_states: Dict of symbol -> MarketState
            
        Returns:
            Formatted string for notification
        """
        if not market_states:
            return "📊 No market data available"
        
        lines = [
            "📊 **MULTI-SYMBOL ANALYSIS**",
            "━" * 30,
            "",
        ]
        
        # Sort by profit probability (best opportunities first)
        sorted_states = sorted(
            market_states.items(),
            key=lambda x: x[1].profit_probability,
            reverse=True
        )
        
        for symbol, state in sorted_states:
            trend_emoji = "🟢" if state.trend == "bullish" else "🔴" if state.trend == "bearish" else "🟡"
            prob_emoji = "✅" if state.profit_probability >= 60 else "⏳"
            
            lines.append(
                f"{trend_emoji} **{symbol}** | `${state.price:,.2f}`\n"
                f"   {prob_emoji} Prob: `{state.profit_probability:.0f}%` | "
                f"RSI: `{state.rsi:.0f}` | "
                f"ADX: `{state.adx:.0f}`"
            )
            
            if state.should_enter:
                lines.append(f"   🎯 **{state.entry_recommendation} SIGNAL ACTIVE**")
            
            lines.append("")
        
        # Best opportunity
        best = sorted_states[0]
        if best[1].profit_probability >= 60:
            lines.append(f"💡 **Best Opportunity:** {best[0]} ({best[1].profit_probability:.0f}%)")
        else:
            lines.append("⏳ **No high-probability setups currently**")
        
        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════════════
    #  UTILITY
    # ═══════════════════════════════════════════════════════════════

    def __repr__(self) -> str:
        return (
            f"<MarketAnalyzer symbol={self.symbol} | "
            f"EntryThreshold={self.ENTRY_PROBABILITY_THRESHOLD:.0f}% | "
            f"ExitThreshold={self.EXIT_PROBABILITY_THRESHOLD:.0f}%>"
        )