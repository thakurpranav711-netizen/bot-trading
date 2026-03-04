# app/strategies/base.py

"""
Base Strategy — Production Grade for Autonomous Trading

Abstract base class for all trading strategies.

Provides:
- Structured entry/exit signal builders with validation gates
- Risk-aware validation (RR, confidence, volatility, drawdown)
- 4-Brain signal alignment checks
- Adaptive confidence floor during loss streaks
- ATR-based SL/TP helpers with trailing stop support
- Multi-factor scoring engine
- Regime and volatility gates
- Position sizing with Kelly criterion option
- Signal quality scoring
- Trade filtering (time, correlation, etc.)

All concrete strategies (ScalpingStrategy, SwingStrategy, etc.) inherit from this.
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, List, Any, Tuple
from datetime import datetime, time as dt_time
from app.market.analyzer import MarketState
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  SIGNAL QUALITY LEVELS
# ═══════════════════════════════════════════════════════════════════

class SignalQuality:
    """Signal quality classification."""
    EXCELLENT = "excellent"  # 80%+ confidence, strong alignment
    GOOD = "good"            # 65-80% confidence
    MODERATE = "moderate"    # 55-65% confidence
    WEAK = "weak"            # Below threshold, filtered out


# ═══════════════════════════════════════════════════════════════════
#  BASE STRATEGY
# ═══════════════════════════════════════════════════════════════════

class BaseStrategy(ABC):
    """
    Institutional Strategy Base Class — 4-Brain Edition

    Subclasses MUST implement:
        should_enter(market) -> Optional[Dict]
        should_exit(market, position) -> Optional[Dict]

    Subclasses SHOULD set:
        name: str = "strategy_name"

    Subclasses CAN override class-level defaults:
        MIN_CONFIDENCE = 0.55
        MIN_RISK_REWARD = 1.5
        MAX_VOLATILITY_ALLOWED = 0.08
        MIN_BRAIN_ALIGNMENT = 2
    """

    # ── Class-level defaults (override in subclass) ───────────────
    MIN_CONFIDENCE: float = 0.55
    MIN_RISK_REWARD: float = 1.5
    MAX_VOLATILITY_ALLOWED: float = 0.08
    MIN_VOLATILITY_REQUIRED: float = 0.002
    MIN_BRAIN_ALIGNMENT: int = 2
    MAX_LOSS_STREAK_ALLOWED: int = 5

    # Strategy identifier
    name: str = "base"
    version: str = "1.0.0"

    # Supported regimes (override in subclass)
    ALLOWED_REGIMES: List[str] = ["trending", "ranging", "explosive"]

    # Trading hours (None = 24/7)
    TRADING_START_HOUR: Optional[int] = None
    TRADING_END_HOUR: Optional[int] = None

    def __init__(
        self,
        symbol: str,
        min_confidence: float = None,
        min_risk_reward: float = None,
        max_volatility: float = None,
        min_volatility: float = None,
        min_brain_alignment: int = None,
        allowed_regimes: List[str] = None,
    ):
        """
        Initialize base strategy.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            min_confidence: Override class MIN_CONFIDENCE
            min_risk_reward: Override class MIN_RISK_REWARD
            max_volatility: Override class MAX_VOLATILITY_ALLOWED
            min_volatility: Override class MIN_VOLATILITY_REQUIRED
            min_brain_alignment: Override class MIN_BRAIN_ALIGNMENT
            allowed_regimes: Override class ALLOWED_REGIMES
        """
        self.symbol = symbol

        # Instance overrides with class fallbacks
        self._min_confidence = min_confidence if min_confidence is not None else self.MIN_CONFIDENCE
        self._min_risk_reward = min_risk_reward if min_risk_reward is not None else self.MIN_RISK_REWARD
        self._max_volatility = max_volatility if max_volatility is not None else self.MAX_VOLATILITY_ALLOWED
        self._min_volatility = min_volatility if min_volatility is not None else self.MIN_VOLATILITY_REQUIRED
        self._min_brain_alignment = min_brain_alignment if min_brain_alignment is not None else self.MIN_BRAIN_ALIGNMENT
        self._allowed_regimes = allowed_regimes if allowed_regimes is not None else self.ALLOWED_REGIMES

        # ── State tracking ────────────────────────────────────────
        self._loss_streak: int = 0
        self._win_streak: int = 0
        self._total_signals: int = 0
        self._filtered_signals: int = 0
        self._last_signal_time: Optional[datetime] = None
        self._last_signal_direction: Optional[str] = None

        # ── Performance tracking ──────────────────────────────────
        self._signal_history: List[Dict] = []
        self._max_history_size: int = 100

        logger.info(
            f"📊 Strategy initialized | {self.name} v{self.version} | "
            f"{self.symbol} | MinConf={self._min_confidence} | "
            f"MinRR={self._min_risk_reward}"
        )

    # ═══════════════════════════════════════════════════════════════
    #  ABSTRACT METHODS (MUST IMPLEMENT)
    # ═══════════════════════════════════════════════════════════════

    @abstractmethod
    def should_enter(self, market: MarketState) -> Optional[Dict]:
        """
        Evaluate entry conditions.

        Called every cycle when NO position is open.

        Args:
            market: Current MarketState from analyzer

        Returns:
            Entry signal dict or None

        Implementation should:
            1. Check pre-conditions (regime, volatility, time)
            2. Evaluate setups (long/short)
            3. Calculate confidence
            4. Call build_entry_signal() to validate and package
        """

    @abstractmethod
    def should_exit(self, market: MarketState, position: Dict) -> Optional[Dict]:
        """
        Evaluate exit conditions.

        Called every cycle when a position IS open.

        Args:
            market: Current MarketState from analyzer
            position: Current position dict

        Returns:
            Exit signal dict or None

        Note: SL/TP hits are handled by controller, not strategy.
        """

    # ═══════════════════════════════════════════════════════════════
    #  PRE-CONDITION CHECKS
    # ═══════════════════════════════════════════════════════════════

    def check_preconditions(self, market: MarketState) -> Tuple[bool, str]:
        """
        Check all pre-conditions before signal evaluation.

        Returns:
            (passes: bool, reason: str)
        """
        # Check trading hours
        if not self._is_trading_hours():
            return False, "Outside trading hours"

        # Check regime
        if market.regime not in self._allowed_regimes:
            return False, f"Regime '{market.regime}' not allowed"

        # Check volatility bounds
        if market.volatility > self._max_volatility:
            return False, f"Volatility too high ({market.volatility:.4f})"

        if market.volatility < self._min_volatility:
            return False, f"Volatility too low ({market.volatility:.4f})"

        # Check loss streak
        if self._loss_streak >= self.MAX_LOSS_STREAK_ALLOWED:
            return False, f"Loss streak limit ({self._loss_streak})"

        # Check for valid price
        if market.price <= 0:
            return False, "Invalid price"

        # Check for sufficient candles
        if market.candle_count < 50:
            return False, f"Insufficient candles ({market.candle_count})"

        return True, "OK"

    def _is_trading_hours(self) -> bool:
        """Check if current time is within trading hours."""
        if self.TRADING_START_HOUR is None or self.TRADING_END_HOUR is None:
            return True  # 24/7 trading

        current_hour = datetime.utcnow().hour

        if self.TRADING_START_HOUR <= self.TRADING_END_HOUR:
            return self.TRADING_START_HOUR <= current_hour < self.TRADING_END_HOUR
        else:
            # Handles overnight windows (e.g., 22:00 - 06:00)
            return current_hour >= self.TRADING_START_HOUR or current_hour < self.TRADING_END_HOUR

    # ═══════════════════════════════════════════════════════════════
    #  ENTRY SIGNAL BUILDER
    # ═══════════════════════════════════════════════════════════════

    def build_entry_signal(
        self,
        market: MarketState,
        direction: str,
        stop_loss: float,
        take_profit: float,
        confidence: float,
        reason: str,
        factors: Optional[List[Dict]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        require_brain_alignment: bool = True,
        allow_repeat_direction: bool = True,
    ) -> Optional[Dict]:
        """
        Validate and package a trade entry signal.

        Gates applied in order:
            1. Preconditions (regime, volatility, hours)
            2. Direction repeat filter
            3. Confidence floor (adaptive)
            4. Risk/Reward minimum
            5. Brain alignment

        Args:
            market: MarketState object
            direction: "BUY" or "SELL"
            stop_loss: Stop loss price
            take_profit: Take profit price
            confidence: Signal confidence (0-1)
            reason: Human-readable entry reason
            factors: List of factor dicts for audit
            metadata: Additional data to include
            require_brain_alignment: Check 4-brain consensus
            allow_repeat_direction: Allow same direction as last signal

        Returns:
            Validated entry signal dict, or None if blocked
        """
        direction = direction.upper()
        confidence = self._normalize_confidence(confidence)
        self._total_signals += 1

        # ── Gate 0: Preconditions ─────────────────────────────────
        passes, reason_blocked = self.check_preconditions(market)
        if not passes:
            self._filtered_signals += 1
            logger.debug(f"⛔ Entry blocked | Precondition: {reason_blocked}")
            return None

        # ── Gate 1: Direction repeat filter ───────────────────────
        if not allow_repeat_direction and self._last_signal_direction == direction:
            if self._last_signal_time:
                time_since = (datetime.utcnow() - self._last_signal_time).total_seconds()
                if time_since < 300:  # 5 minutes
                    self._filtered_signals += 1
                    logger.debug(f"⛔ Entry blocked | Repeat {direction} too soon")
                    return None

        # ── Gate 2: Adaptive confidence floor ─────────────────────
        min_conf = self.get_confidence_floor()
        if confidence < min_conf:
            self._filtered_signals += 1
            logger.debug(
                f"⛔ Entry blocked | Confidence {confidence:.2f} < "
                f"floor {min_conf:.2f} (streak={self._loss_streak})"
            )
            return None

        # ── Gate 3: Risk/Reward minimum ───────────────────────────
        rr = self._calculate_rr(market.price, stop_loss, take_profit, direction)
        if rr < self._min_risk_reward:
            self._filtered_signals += 1
            logger.debug(
                f"⛔ Entry blocked | RR {rr:.2f} < min {self._min_risk_reward}"
            )
            return None

        # ── Gate 4: Brain alignment ───────────────────────────────
        brain_votes = self._count_brain_votes(market, direction)
        if require_brain_alignment and brain_votes < self._min_brain_alignment:
            self._filtered_signals += 1
            logger.debug(
                f"⛔ Entry blocked | Brain alignment {brain_votes}/"
                f"{self._min_brain_alignment} for {direction}"
            )
            return None

        # ── Calculate signal quality ──────────────────────────────
        quality = self._assess_signal_quality(confidence, brain_votes, rr, market)

        # ── Calculate suggested position size ─────────────────────
        risk_distance = abs(market.price - stop_loss)
        risk_percent = risk_distance / market.price if market.price > 0 else 0

        # ── Build signal ──────────────────────────────────────────
        signal = {
            "symbol": self.symbol,
            "action": direction,
            "side": "long" if direction == "BUY" else "short",
            "entry_price": market.price,
            "stop_loss": round(stop_loss, 8),
            "take_profit": round(take_profit, 8),
            "risk_reward": round(rr, 3),
            "confidence": confidence,
            "quality": quality,
            "regime": market.regime,
            "trend": market.trend,
            "volatility": market.volatility,
            "volatility_regime": market.volatility_regime,
            "risk_percent": round(risk_percent * 100, 2),
            "reason": reason,
            "strategy": self.name,
            "strategy_version": self.version,
            "brain_votes": brain_votes,
            "factors": factors or [],
            "metadata": metadata or {},
            "timestamp": datetime.utcnow().isoformat(),
            "signal_number": self._total_signals,
        }

        # Update tracking
        self._last_signal_time = datetime.utcnow()
        self._last_signal_direction = direction
        self._record_signal(signal)

        logger.info(
            f"✅ Entry signal [{quality.upper()}] | {self.symbol} {direction} @ ${market.price:,.2f} | "
            f"SL=${stop_loss:,.2f} TP=${take_profit:,.2f} | "
            f"RR={rr:.2f} Conf={confidence:.0%} Brains={brain_votes}/4"
        )

        return signal

    def _assess_signal_quality(
        self,
        confidence: float,
        brain_votes: int,
        risk_reward: float,
        market: MarketState
    ) -> str:
        """Assess overall signal quality."""
        score = 0

        # Confidence contribution (40%)
        if confidence >= 0.80:
            score += 40
        elif confidence >= 0.70:
            score += 30
        elif confidence >= 0.60:
            score += 20
        else:
            score += 10

        # Brain alignment (30%)
        score += brain_votes * 7.5  # Max 30 for 4 votes

        # Risk/Reward (20%)
        if risk_reward >= 3.0:
            score += 20
        elif risk_reward >= 2.5:
            score += 15
        elif risk_reward >= 2.0:
            score += 10
        else:
            score += 5

        # Market conditions (10%)
        if market.regime == "trending" and market.trend != "sideways":
            score += 10
        elif market.regime == "ranging":
            score += 5

        # Classify
        if score >= 80:
            return SignalQuality.EXCELLENT
        elif score >= 65:
            return SignalQuality.GOOD
        elif score >= 50:
            return SignalQuality.MODERATE
        else:
            return SignalQuality.WEAK

    def _record_signal(self, signal: Dict) -> None:
        """Record signal for history tracking."""
        self._signal_history.append({
            "timestamp": signal["timestamp"],
            "direction": signal["action"],
            "confidence": signal["confidence"],
            "quality": signal["quality"],
            "rr": signal["risk_reward"],
        })

        # Trim history
        if len(self._signal_history) > self._max_history_size:
            self._signal_history = self._signal_history[-self._max_history_size:]

    # ═══════════════════════════════════════════════════════════════
    #  EXIT SIGNAL BUILDER
    # ═══════════════════════════════════════════════════════════════

    def build_exit_signal(
        self,
        market: MarketState,
        position: Dict,
        confidence: float,
        reason: str,
        exit_type: str = "signal",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        """
        Validate and package an exit signal.

        Args:
            market: MarketState object
            position: Current position dict
            confidence: Exit confidence (0-1)
            reason: Human-readable exit reason
            exit_type: "signal", "trailing", "breakeven", "reversal"
            metadata: Additional data

        Returns:
            Validated exit signal dict, or None if confidence too low
        """
        confidence = self._normalize_confidence(confidence)

        # Lower bar for exits (protecting capital > perfect entries)
        min_exit_confidence = 0.40
        if confidence < min_exit_confidence:
            logger.debug(f"⛔ Exit blocked | Confidence {confidence:.2f} < {min_exit_confidence}")
            return None

        entry_price = position.get("entry_price", position.get("avg_price", market.price))
        quantity = position.get("quantity", 0)
        side = position.get("side", "long")
        is_long = side == "long"

        # Calculate PnL
        if is_long:
            current_pnl = (market.price - entry_price) * quantity
        else:
            current_pnl = (entry_price - market.price) * quantity

        pnl_pct = (current_pnl / (entry_price * quantity) * 100) if entry_price * quantity > 0 else 0

        # Calculate hold duration
        opened_at = position.get("opened_at", "")
        hold_duration = 0
        if opened_at:
            try:
                open_time = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
                hold_duration = int((datetime.utcnow() - open_time.replace(tzinfo=None)).total_seconds() / 60)
            except:
                pass

        signal = {
            "symbol": self.symbol,
            "action": "EXIT",
            "exit_type": exit_type,
            "exit_price": market.price,
            "confidence": confidence,
            "reason": reason,
            "current_pnl": round(current_pnl, 8),
            "pnl_pct": round(pnl_pct, 2),
            "entry_price": entry_price,
            "quantity": quantity,
            "side": side,
            "is_long": is_long,
            "hold_duration_minutes": hold_duration,
            "strategy": self.name,
            "metadata": metadata or {},
            "timestamp": datetime.utcnow().isoformat(),
        }

        emoji = "🟢" if current_pnl >= 0 else "🔴"
        logger.info(
            f"{emoji} Exit signal | {self.symbol} @ ${market.price:,.2f} | "
            f"Type={exit_type} | Reason={reason} | "
            f"PnL=${current_pnl:+.4f} ({pnl_pct:+.2f}%) | Hold={hold_duration}min"
        )

        return signal

    # ═══════════════════════════════════════════════════════════════
    #  TRAILING STOP LOGIC
    # ═══════════════════════════════════════════════════════════════

    def calculate_trailing_stop(
        self,
        market: MarketState,
        position: Dict,
        trailing_percent: float = 0.02,
        use_atr: bool = True,
        atr_multiplier: float = 2.0,
    ) -> Optional[float]:
        """
        Calculate trailing stop level.

        Args:
            market: Current market state
            position: Current position
            trailing_percent: Percentage trailing distance
            use_atr: Use ATR-based trailing
            atr_multiplier: ATR multiplier

        Returns:
            New stop loss price, or None if no update needed
        """
        side = position.get("side", "long")
        current_stop = position.get("stop_loss", 0)
        entry_price = position.get("entry_price", market.price)
        highest = position.get("highest_price", market.price)
        lowest = position.get("lowest_price", market.price)

        # Calculate trailing distance
        if use_atr and market.atr > 0:
            trail_distance = market.atr * atr_multiplier
        else:
            trail_distance = market.price * trailing_percent

        if side == "long":
            # Trail up only
            new_stop = highest - trail_distance

            # Only update if higher than current stop and in profit
            if new_stop > current_stop and new_stop > entry_price:
                logger.debug(f"📈 Trailing stop: ${current_stop:.4f} → ${new_stop:.4f}")
                return round(new_stop, 8)

        else:  # short
            # Trail down only
            new_stop = lowest + trail_distance

            # Only update if lower than current stop and in profit
            if new_stop < current_stop and new_stop < entry_price:
                logger.debug(f"📉 Trailing stop: ${current_stop:.4f} → ${new_stop:.4f}")
                return round(new_stop, 8)

        return None

    def should_move_to_breakeven(
        self,
        market: MarketState,
        position: Dict,
        profit_threshold_pct: float = 1.0,
    ) -> bool:
        """
        Check if stop should be moved to breakeven.

        Args:
            market: Current market state
            position: Current position
            profit_threshold_pct: Minimum profit % to trigger

        Returns:
            True if should move to breakeven
        """
        entry_price = position.get("entry_price", market.price)
        current_stop = position.get("stop_loss", 0)
        side = position.get("side", "long")

        # Already at or past breakeven
        if side == "long" and current_stop >= entry_price:
            return False
        if side == "short" and current_stop <= entry_price:
            return False

        # Calculate current profit
        if side == "long":
            profit_pct = ((market.price - entry_price) / entry_price) * 100
        else:
            profit_pct = ((entry_price - market.price) / entry_price) * 100

        return profit_pct >= profit_threshold_pct

    # ═══════════════════════════════════════════════════════════════
    #  CONFIDENCE HANDLING
    # ═══════════════════════════════════════════════════════════════

    def get_confidence_floor(self) -> float:
        """
        Get current confidence floor (adaptive based on streaks).

        Increases floor by 3% per loss, capped at +15% above baseline.
        Decreases floor by 2% per win (minimum = base confidence).
        """
        loss_adjustment = min(self._loss_streak * 0.03, 0.15)
        win_adjustment = min(self._win_streak * 0.02, 0.10)

        floor = self._min_confidence + loss_adjustment - win_adjustment
        return max(self._min_confidence, floor)

    def _normalize_confidence(self, value: float) -> float:
        """Clamp confidence to [0, 1] range."""
        if value is None:
            return 0.0
        return round(max(0.0, min(1.0, value)), 3)

    def set_loss_streak(self, streak: int) -> None:
        """Update loss streak count."""
        self._loss_streak = max(0, streak)
        if streak > 0:
            self._win_streak = 0

    def set_win_streak(self, streak: int) -> None:
        """Update win streak count."""
        self._win_streak = max(0, streak)
        if streak > 0:
            self._loss_streak = 0

    def record_trade_result(self, is_win: bool) -> None:
        """Record trade result for streak tracking."""
        if is_win:
            self._win_streak += 1
            self._loss_streak = 0
        else:
            self._loss_streak += 1
            self._win_streak = 0

    # ═══════════════════════════════════════════════════════════════
    #  4-BRAIN ALIGNMENT
    # ═══════════════════════════════════════════════════════════════

    def _count_brain_votes(self, market: MarketState, direction: str) -> int:
        """
        Count how many brains agree with the proposed direction.

        Brain 1: Technical indicators
        Brain 2: Sentiment
        Brain 3: Chart patterns
        Brain 4: AI prediction

        Returns:
            Number of brains voting for direction (0-4)
        """
        direction = direction.upper()
        votes = 0

        # Brain 1: Indicators
        indicators = getattr(market, "indicators", {})
        b1_signal = self._score_indicators(indicators)
        if b1_signal == direction:
            votes += 1

        # Brain 2: Sentiment
        sentiment = getattr(market, "sentiment_score", 0.0)
        if direction == "BUY" and sentiment > 0.1:
            votes += 1
        elif direction == "SELL" and sentiment < -0.1:
            votes += 1

        # Brain 3: Chart Pattern
        chart = getattr(market, "chart_pattern", None)
        if chart and chart.get("signal", "HOLD").upper() == direction:
            votes += 1

        # Brain 4: AI Prediction
        ai = getattr(market, "ai_prediction", None)
        if ai and ai.get("signal", "HOLD").upper() == direction:
            votes += 1

        return votes

    def check_brain_alignment(
        self, market: MarketState, direction: str
    ) -> Tuple[bool, int, Dict]:
        """
        Check brain alignment with detailed breakdown.

        Returns:
            (passes: bool, vote_count: int, details: Dict)
        """
        direction = direction.upper()
        details = {
            "indicators": "neutral",
            "sentiment": "neutral",
            "pattern": "neutral",
            "ai": "neutral",
        }

        votes = 0

        # Brain 1
        indicators = getattr(market, "indicators", {})
        b1 = self._score_indicators(indicators)
        details["indicators"] = b1.lower()
        if b1 == direction:
            votes += 1

        # Brain 2
        sentiment = getattr(market, "sentiment_score", 0.0)
        if sentiment > 0.1:
            details["sentiment"] = "bullish"
            if direction == "BUY":
                votes += 1
        elif sentiment < -0.1:
            details["sentiment"] = "bearish"
            if direction == "SELL":
                votes += 1

        # Brain 3
        chart = getattr(market, "chart_pattern", None)
        if chart:
            chart_signal = chart.get("signal", "HOLD").upper()
            details["pattern"] = chart_signal.lower()
            if chart_signal == direction:
                votes += 1

        # Brain 4
        ai = getattr(market, "ai_prediction", None)
        if ai:
            ai_signal = ai.get("signal", "HOLD").upper()
            details["ai"] = ai_signal.lower()
            if ai_signal == direction:
                votes += 1

        passes = votes >= self._min_brain_alignment
        return passes, votes, details

    def _score_indicators(self, indicators: Dict) -> str:
        """Score technical indicators to get direction signal."""
        score = 0

        # RSI
        rsi = indicators.get("rsi")
        if rsi is not None:
            if rsi < 35:
                score += 1
            elif rsi > 65:
                score -= 1

        # Stochastic RSI
        stoch_k = indicators.get("stoch_rsi_k")
        if stoch_k is not None:
            if stoch_k < 20:
                score += 1
            elif stoch_k > 80:
                score -= 1

        # MACD cross
        macd_cross = indicators.get("macd_cross")
        if macd_cross == "bullish":
            score += 1
        elif macd_cross == "bearish":
            score -= 1

        # MACD histogram
        macd_hist = indicators.get("macd_histogram", 0)
        if macd_hist > 0:
            score += 0.5
        elif macd_hist < 0:
            score -= 0.5

        # EMA cross
        ema_cross = indicators.get("ema_cross")
        if ema_cross == "bullish":
            score += 1
        elif ema_cross == "bearish":
            score -= 1

        # Bollinger position
        bb_position = indicators.get("bb_position")
        if bb_position == "oversold":
            score += 1
        elif bb_position == "overbought":
            score -= 1

        # ADX direction
        plus_di = indicators.get("plus_di", 0)
        minus_di = indicators.get("minus_di", 0)
        if plus_di > minus_di + 5:
            score += 0.5
        elif minus_di > plus_di + 5:
            score -= 0.5

        if score >= 1.5:
            return "BUY"
        elif score <= -1.5:
            return "SELL"
        return "HOLD"

    # ═══════════════════════════════════════════════════════════════
    #  CONFIDENCE FROM BRAINS
    # ═══════════════════════════════════════════════════════════════

    def confidence_from_brains(
        self,
        market: MarketState,
        direction: str,
        weights: Optional[Dict[str, float]] = None,
    ) -> float:
        """
        Calculate confidence score from 4-brain weighted signals.

        Args:
            market: MarketState object
            direction: "BUY" or "SELL"
            weights: Optional custom weights

        Returns:
            Confidence score (0-1)
        """
        w = weights or {
            "indicators": 0.35,
            "sentiment": 0.15,
            "chart": 0.25,
            "ai": 0.25,
        }
        direction = direction.upper()
        score = 0.0

        # Brain 1: Indicators
        indicators = getattr(market, "indicators", {})
        b1 = self._score_indicators(indicators)
        if b1 == direction:
            score += w["indicators"]
        elif b1 == "HOLD":
            score += w["indicators"] * 0.3  # Partial credit for neutral

        # Brain 2: Sentiment
        sentiment = getattr(market, "sentiment_score", 0.0)
        if direction == "BUY" and sentiment > 0:
            score += w["sentiment"] * min(abs(sentiment), 1.0)
        elif direction == "SELL" and sentiment < 0:
            score += w["sentiment"] * min(abs(sentiment), 1.0)

        # Brain 3: Chart
        chart = getattr(market, "chart_pattern", None)
        if chart:
            chart_signal = chart.get("signal", "HOLD").upper()
            chart_conf = chart.get("confidence", 0) / 100
            if chart_signal == direction:
                score += w["chart"] * chart_conf
            elif chart_signal == "HOLD":
                score += w["chart"] * 0.2

        # Brain 4: AI
        ai = getattr(market, "ai_prediction", None)
        if ai:
            ai_signal = ai.get("signal", "HOLD").upper()
            ai_conf = ai.get("confidence", 0) / 100
            if ai_signal == direction:
                score += w["ai"] * ai_conf

        # Bonus for market alignment
        if market.trend == "bullish" and direction == "BUY":
            score += 0.05
        elif market.trend == "bearish" and direction == "SELL":
            score += 0.05

        return self._normalize_confidence(score)

    # ═══════════════════════════════════════════════════════════════
    #  RISK/REWARD CALCULATION
    # ═══════════════════════════════════════════════════════════════

    def _calculate_rr(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        direction: str = "BUY",
    ) -> float:
        """Calculate risk/reward ratio accounting for direction."""
        direction = direction.upper()

        if direction == "BUY":
            risk = entry_price - stop_loss
            reward = take_profit - entry_price
        else:  # SELL
            risk = stop_loss - entry_price
            reward = entry_price - take_profit

        if risk <= 0:
            return 0.0

        return round(reward / risk, 3)

    def validate_risk_reward(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        direction: str = "BUY",
    ) -> Tuple[bool, float]:
        """Check if RR meets minimum threshold."""
        rr = self._calculate_rr(entry_price, stop_loss, take_profit, direction)
        return rr >= self._min_risk_reward, rr

    # ═══════════════════════════════════════════════════════════════
    #  ATR-BASED SL/TP HELPERS
    # ═══════════════════════════════════════════════════════════════

    def atr_stop_loss(
        self,
        market: MarketState,
        direction: str,
        atr_multiplier: float = 1.5,
    ) -> float:
        """Calculate ATR-based stop loss."""
        direction = direction.upper()
        distance = market.atr * atr_multiplier

        if direction == "BUY":
            return round(max(0.00000001, market.price - distance), 8)
        else:  # SELL
            return round(market.price + distance, 8)

    def atr_take_profit(
        self,
        market: MarketState,
        direction: str,
        risk_reward: float = 2.0,
        atr_multiplier: float = 1.5,
    ) -> float:
        """Calculate ATR-based take profit."""
        stop = self.atr_stop_loss(market, direction, atr_multiplier)
        risk = abs(market.price - stop)
        reward = risk * risk_reward

        direction = direction.upper()
        if direction == "BUY":
            return round(market.price + reward, 8)
        else:  # SELL
            return round(max(0.00000001, market.price - reward), 8)

    def calculate_sl_tp(
        self,
        market: MarketState,
        direction: str,
        atr_multiplier: float = 1.5,
        risk_reward: float = None,
    ) -> Tuple[float, float]:
        """
        Calculate both SL and TP in one call.

        Returns:
            (stop_loss, take_profit)
        """
        rr = risk_reward or self.dynamic_risk_reward(market)

        sl = self.atr_stop_loss(market, direction, atr_multiplier)
        tp = self.atr_take_profit(market, direction, rr, atr_multiplier)

        return sl, tp

    def dynamic_risk_reward(self, market: MarketState) -> float:
        """
        Get dynamic R:R target based on market conditions.

        Tighter targets in volatile/ranging markets.
        Wider targets in trending/calm markets.
        """
        base_rr = self._min_risk_reward

        # Volatility adjustment
        if market.volatility_regime == "extreme":
            base_rr *= 0.7
        elif market.volatility_regime == "high":
            base_rr *= 0.85
        elif market.volatility_regime == "low":
            base_rr *= 1.2

        # Regime adjustment
        if market.regime == "trending":
            base_rr *= 1.1
        elif market.regime == "ranging":
            base_rr *= 0.9

        # ADX adjustment
        adx = getattr(market, "adx", 25)
        if adx > 40:
            base_rr *= 1.1  # Strong trend, let it run
        elif adx < 20:
            base_rr *= 0.9  # Weak trend, take profits quickly

        return round(max(1.2, min(4.0, base_rr)), 2)

    # ═══════════════════════════════════════════════════════════════
    #  REGIME / VOLATILITY GATES
    # ═══════════════════════════════════════════════════════════════

    def regime_allowed(self, market: MarketState) -> bool:
        """Check if current regime is in allowed list."""
        return market.regime in self._allowed_regimes

    def volatility_ok(self, market: MarketState) -> bool:
        """Check if volatility is within strategy limits."""
        return self._min_volatility <= market.volatility <= self._max_volatility

    def trend_aligned(self, market: MarketState, direction: str) -> bool:
        """Check if trend aligns with direction."""
        direction = direction.upper()
        if direction == "BUY":
            return market.trend in ("bullish", "sideways")
        elif direction == "SELL":
            return market.trend in ("bearish", "sideways")
        return False

    def strong_trend_aligned(self, market: MarketState, direction: str) -> bool:
        """Check for strong trend alignment (excludes sideways)."""
        direction = direction.upper()
        if direction == "BUY":
            return market.trend == "bullish"
        elif direction == "SELL":
            return market.trend == "bearish"
        return False

    # ═══════════════════════════════════════════════════════════════
    #  MULTI-FACTOR SCORING
    # ═══════════════════════════════════════════════════════════════

    def weighted_score(self, factors: List[Dict]) -> float:
        """Calculate weighted average of factor scores."""
        if not factors:
            return 0.0

        total_weight = sum(f.get("weight", 0) for f in factors)
        if total_weight == 0:
            return 0.0

        weighted_sum = sum(f.get("score", 0) * f.get("weight", 0) for f in factors)
        return round(weighted_sum / total_weight, 3)

    def build_factor_list(
        self, market: MarketState, direction: str
    ) -> List[Dict]:
        """Build comprehensive factor list from MarketState."""
        direction = direction.upper()
        factors = []

        # Trend alignment
        trend_score = 1.0 if self.strong_trend_aligned(market, direction) else (
            0.5 if self.trend_aligned(market, direction) else 0.0
        )
        factors.append({
            "name": "trend_alignment",
            "score": trend_score,
            "weight": 0.20,
            "value": market.trend,
        })

        # RSI position
        if direction == "BUY":
            rsi_score = max(0, min(1, (70 - market.rsi) / 40))
        else:
            rsi_score = max(0, min(1, (market.rsi - 30) / 40))
        factors.append({
            "name": "rsi_position",
            "score": round(rsi_score, 3),
            "weight": 0.15,
            "value": market.rsi,
        })

        # Volume pressure
        if direction == "BUY":
            vol_score = max(0, min(1, (market.volume_pressure + 1) / 2))
        else:
            vol_score = max(0, min(1, (1 - market.volume_pressure) / 2))
        factors.append({
            "name": "volume_pressure",
            "score": round(vol_score, 3),
            "weight": 0.15,
            "value": market.volume_pressure,
        })

        # Momentum
        mom_score = min(1.0, market.momentum_strength * 50)
        factors.append({
            "name": "momentum",
            "score": round(mom_score, 3),
            "weight": 0.15,
            "value": market.momentum_strength,
        })

        # Market structure
        structure_score = 0.0
        if market.structure_break:
            if (direction == "BUY" and market.break_direction == "up") or \
               (direction == "SELL" and market.break_direction == "down"):
                structure_score = 1.0
        factors.append({
            "name": "structure",
            "score": structure_score,
            "weight": 0.15,
            "value": market.break_direction,
        })

        # Market confidence
        factors.append({
            "name": "market_confidence",
            "score": market.confidence_score,
            "weight": 0.20,
            "value": market.confidence_score,
        })

        return factors

    # ═══════════════════════════════════════════════════════════════
    #  POSITION SIZING
    # ═══════════════════════════════════════════════════════════════

    def suggested_position_size(
        self,
        account_balance: float,
        risk_percent: float,
        entry_price: float,
        stop_loss: float,
        fee_percent: float = 0.001,
    ) -> float:
        """
        Calculate position size based on risk amount.

        Args:
            account_balance: Available balance
            risk_percent: Risk per trade (e.g., 0.01 = 1%)
            entry_price: Planned entry price
            stop_loss: Planned stop loss
            fee_percent: Expected trading fee

        Returns:
            Position size (quantity)
        """
        risk_amount = account_balance * risk_percent
        risk_per_unit = abs(entry_price - stop_loss)

        if risk_per_unit == 0:
            return 0.0

        # Account for fees
        fee_adjustment = 1 + (fee_percent * 2)  # Entry + exit
        adjusted_risk = risk_amount / fee_adjustment

        return round(adjusted_risk / risk_per_unit, 8)

    def kelly_position_size(
        self,
        account_balance: float,
        win_rate: float,
        avg_win: float,
        avg_loss: float,
        kelly_fraction: float = 0.25,
    ) -> float:
        """
        Calculate position size using Kelly Criterion.

        Args:
            account_balance: Available balance
            win_rate: Historical win rate (0-1)
            avg_win: Average winning trade amount
            avg_loss: Average losing trade amount
            kelly_fraction: Fraction of Kelly to use (0.25 = quarter Kelly)

        Returns:
            Suggested position size in quote currency
        """
        if avg_loss == 0 or win_rate == 0:
            return 0.0

        # Kelly formula: f = (bp - q) / b
        # where b = avg_win/avg_loss, p = win_rate, q = 1-p
        b = avg_win / avg_loss
        p = win_rate
        q = 1 - p

        kelly = (b * p - q) / b

        # Cap Kelly at 25% and apply fraction
        kelly = max(0, min(0.25, kelly)) * kelly_fraction

        return round(account_balance * kelly, 2)

    # ═══════════════════════════════════════════════════════════════
    #  STATISTICS & ANALYTICS
    # ═══════════════════════════════════════════════════════════════

    def get_stats(self) -> Dict:
        """Get strategy statistics."""
        filter_rate = (
            self._filtered_signals / self._total_signals * 100
            if self._total_signals > 0 else 0
        )

        return {
            "name": self.name,
            "version": self.version,
            "symbol": self.symbol,
            "total_signals": self._total_signals,
            "filtered_signals": self._filtered_signals,
            "filter_rate": round(filter_rate, 1),
            "loss_streak": self._loss_streak,
            "win_streak": self._win_streak,
            "confidence_floor": self.get_confidence_floor(),
            "last_signal_time": self._last_signal_time.isoformat() if self._last_signal_time else None,
            "last_signal_direction": self._last_signal_direction,
        }

    def get_signal_history(self, limit: int = 20) -> List[Dict]:
        """Get recent signal history."""
        return self._signal_history[-limit:]

    def reset_stats(self) -> None:
        """Reset all statistics."""
        self._total_signals = 0
        self._filtered_signals = 0
        self._signal_history.clear()
        logger.info(f"📊 Stats reset for {self.name}")

    # ═══════════════════════════════════════════════════════════════
    #  CONFIGURATION
    # ═══════════════════════════════════════════════════════════════

    def get_config(self) -> Dict:
        """Get strategy configuration."""
        return {
            "name": self.name,
            "version": self.version,
            "symbol": self.symbol,
            "min_confidence": self._min_confidence,
            "min_risk_reward": self._min_risk_reward,
            "max_volatility": self._max_volatility,
            "min_volatility": self._min_volatility,
            "min_brain_alignment": self._min_brain_alignment,
            "allowed_regimes": self._allowed_regimes,
            "loss_streak": self._loss_streak,
            "win_streak": self._win_streak,
            "confidence_floor": self.get_confidence_floor(),
        }

    def update_config(self, **kwargs) -> None:
        """Update strategy configuration at runtime."""
        if "min_confidence" in kwargs:
            self._min_confidence = kwargs["min_confidence"]
        if "min_risk_reward" in kwargs:
            self._min_risk_reward = kwargs["min_risk_reward"]
        if "max_volatility" in kwargs:
            self._max_volatility = kwargs["max_volatility"]
        if "min_brain_alignment" in kwargs:
            self._min_brain_alignment = kwargs["min_brain_alignment"]

        logger.info(f"⚙️ Strategy config updated: {kwargs}")

    # ═══════════════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═══════════════════════════════════════════════════════════════

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} '{self.name}' v{self.version} | "
            f"Symbol={self.symbol} | "
            f"Conf={self._min_confidence} | RR={self._min_risk_reward}>"
        )

    def __str__(self) -> str:
        return f"{self.name} ({self.symbol})"