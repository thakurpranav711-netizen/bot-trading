# app/strategies/base.py

"""
Base Strategy — Production Grade

Abstract base class for all trading strategies.

Provides:
- Structured entry/exit signal builders with validation gates
- Risk-aware validation (RR, confidence, volatility)
- 4-Brain signal alignment checks
- Adaptive confidence floor during loss streaks
- ATR-based SL/TP helpers
- Multi-factor scoring engine
- Regime and volatility gates
- Position sizing suggestions

All concrete strategies (ScalpingStrategy, etc.) inherit from this.
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional, List, Any, Tuple
from app.market.analyzer import MarketState
from app.utils.logger import get_logger

logger = get_logger(__name__)


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
        MIN_RISK_REWARD = 1.2
        MAX_VOLATILITY_ALLOWED = 0.08
        MIN_BRAIN_ALIGNMENT = 2
    """

    # ── Class-level defaults (override in subclass) ───────────────
    MIN_CONFIDENCE: float = 0.55
    MIN_RISK_REWARD: float = 1.2
    MAX_VOLATILITY_ALLOWED: float = 0.08
    MIN_BRAIN_ALIGNMENT: int = 2

    # Strategy identifier
    name: str = "base"

    def __init__(
        self,
        symbol: str,
        min_confidence: float = None,
        min_risk_reward: float = None,
        max_volatility: float = None,
        min_brain_alignment: int = None,
    ):
        """
        Initialize base strategy.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            min_confidence: Override class MIN_CONFIDENCE
            min_risk_reward: Override class MIN_RISK_REWARD
            max_volatility: Override class MAX_VOLATILITY_ALLOWED
            min_brain_alignment: Override class MIN_BRAIN_ALIGNMENT

        Instance variables take precedence over class defaults.
        This allows per-instance customization while maintaining
        sensible class-level defaults.
        """
        self.symbol = symbol

        # ── FIX: Unified confidence handling ──────────────────────
        # Use instance var if provided, otherwise fall back to class default
        # This fixes bug #15 where class and instance vars conflicted
        self._min_confidence = (
            min_confidence if min_confidence is not None
            else self.MIN_CONFIDENCE
        )
        self._min_risk_reward = (
            min_risk_reward if min_risk_reward is not None
            else self.MIN_RISK_REWARD
        )
        self._max_volatility = (
            max_volatility if max_volatility is not None
            else self.MAX_VOLATILITY_ALLOWED
        )
        self._min_brain_alignment = (
            min_brain_alignment if min_brain_alignment is not None
            else self.MIN_BRAIN_ALIGNMENT
        )

        # ── Loss streak tracking (set by controller) ──────────────
        self._loss_streak: int = 0

    # ═════════════════════════════════════════════════════
    #  ABSTRACT METHODS (MUST IMPLEMENT)
    # ═════════════════════════════════════════════════════

    @abstractmethod
    def should_enter(self, market: MarketState) -> Optional[Dict]:
        """
        Evaluate entry conditions.

        Called every cycle when NO position is open.

        Args:
            market: Current MarketState from analyzer

        Returns:
            Entry signal dict (from build_entry_signal) or None

        Implementation should:
            1. Check regime/volatility filters
            2. Evaluate long and/or short setups
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
            position: Current position dict with entry_price, quantity, etc.

        Returns:
            Exit signal dict (from build_exit_signal) or None

        Implementation should:
            1. Check for reversal signals
            2. Check momentum/trend changes
            3. Calculate exit confidence
            4. Call build_exit_signal() to validate and package

        Note: SL/TP hits are handled by controller, not strategy.
        """

    # ═════════════════════════════════════════════════════
    #  ENTRY SIGNAL BUILDER
    # ═════════════════════════════════════════════════════

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
    ) -> Optional[Dict]:
        """
        Validate and package a trade entry signal.

        Applies gates in order:
            1. Confidence floor (adaptive based on loss streak)
            2. Volatility cap
            3. Risk/Reward minimum
            4. Brain alignment (optional)

        Returns None if ANY gate fails.

        Args:
            market: MarketState object
            direction: "BUY" or "SELL"
            stop_loss: Stop loss price
            take_profit: Take profit price
            confidence: Signal confidence (0-1)
            reason: Human-readable entry reason
            factors: List of factor dicts for audit
            metadata: Additional data to include
            require_brain_alignment: If True, check 4-brain consensus

        Returns:
            Validated entry signal dict, or None if blocked
        """
        direction = direction.upper()
        confidence = self._normalize_confidence(confidence)

        # ── Gate 1: Adaptive confidence floor ─────────────────────
        min_conf = self.get_confidence_floor()
        if confidence < min_conf:
            logger.debug(
                f"⛔ Entry blocked | Confidence {confidence:.2f} < "
                f"floor {min_conf:.2f} (streak={self._loss_streak})"
            )
            return None

        # ── Gate 2: Volatility cap ────────────────────────────────
        if market.volatility > self._max_volatility:
            logger.debug(
                f"⛔ Entry blocked | Volatility {market.volatility:.4f} > "
                f"max {self._max_volatility}"
            )
            return None

        # ── Gate 3: Risk/Reward minimum ───────────────────────────
        rr = self._calculate_rr(market.price, stop_loss, take_profit)
        if rr < self._min_risk_reward:
            logger.debug(
                f"⛔ Entry blocked | RR {rr:.2f} < min {self._min_risk_reward}"
            )
            return None

        # ── Gate 4: Brain alignment ───────────────────────────────
        brain_votes = self._count_brain_votes(market, direction)
        if require_brain_alignment and brain_votes < self._min_brain_alignment:
            logger.debug(
                f"⛔ Entry blocked | Brain alignment {brain_votes}/"
                f"{self._min_brain_alignment} for {direction}"
            )
            return None

        # ── Build signal ──────────────────────────────────────────
        signal = {
            "symbol": self.symbol,
            "action": direction,
            "entry_price": market.price,
            "stop_loss": round(stop_loss, 8),
            "take_profit": round(take_profit, 8),
            "risk_reward": round(rr, 3),
            "confidence": confidence,
            "regime": market.regime,
            "volatility": market.volatility,
            "volatility_regime": market.volatility_regime,
            "reason": reason,
            "strategy": self.name,
            "brain_votes": brain_votes,
            "factors": factors or [],
            "metadata": metadata or {},
        }

        logger.info(
            f"✅ Entry signal | {self.symbol} {direction} @ ${market.price:.2f} | "
            f"SL=${stop_loss:.2f} TP=${take_profit:.2f} | "
            f"RR={rr:.2f} Conf={confidence:.2f} Brains={brain_votes}"
        )

        return signal

    # ═════════════════════════════════════════════════════
    #  EXIT SIGNAL BUILDER
    # ═════════════════════════════════════════════════════

    def build_exit_signal(
        self,
        market: MarketState,
        position: Dict,
        confidence: float,
        reason: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict]:
        """
        Validate and package an exit signal.

        Exit signals require lower confidence than entries (0.5 vs 0.55)
        because protecting capital is more important than perfect entries.

        Args:
            market: MarketState object
            position: Current position dict
            confidence: Exit confidence (0-1)
            reason: Human-readable exit reason
            metadata: Additional data to include

        Returns:
            Validated exit signal dict, or None if confidence too low
        """
        confidence = self._normalize_confidence(confidence)

        # Lower bar for exits than entries
        if confidence < 0.45:
            return None

        entry_price = position.get(
            "entry_price", position.get("avg_price", market.price)
        )
        quantity = position.get("quantity", 0)
        current_pnl = (market.price - entry_price) * quantity

        # Determine if long or short
        action = position.get("action", "BUY").upper()
        is_long = action == "BUY"
        if not is_long:
            current_pnl = -current_pnl  # Invert for shorts

        pnl_pct = (current_pnl / (entry_price * quantity) * 100) if entry_price * quantity > 0 else 0

        signal = {
            "symbol": self.symbol,
            "action": "EXIT",
            "exit_price": market.price,
            "confidence": confidence,
            "reason": reason,
            "current_pnl": round(current_pnl, 4),
            "pnl_pct": round(pnl_pct, 2),
            "entry_price": entry_price,
            "quantity": quantity,
            "is_long": is_long,
            "strategy": self.name,
            "metadata": metadata or {},
        }

        logger.info(
            f"🔴 Exit signal | {self.symbol} @ ${market.price:.2f} | "
            f"Reason={reason} | PnL=${current_pnl:+.4f} ({pnl_pct:+.2f}%)"
        )

        return signal

    # ═════════════════════════════════════════════════════
    #  CONFIDENCE HANDLING
    # ═════════════════════════════════════════════════════

    def get_confidence_floor(self) -> float:
        """
        Get current confidence floor (adaptive based on loss streak).

        FIX: Uses unified _min_confidence instead of class/instance mismatch.

        Increases floor by 3% per loss, capped at +15% above baseline.
        """
        adjustment = min(self._loss_streak * 0.03, 0.15)
        return self._min_confidence + adjustment

    def _normalize_confidence(self, value: float) -> float:
        """Clamp confidence to [0, 1] range."""
        if value is None:
            return 0.0
        return round(max(0.0, min(1.0, value)), 3)

    def set_loss_streak(self, streak: int) -> None:
        """
        Update loss streak count.

        Called by controller after each trade to keep
        adaptive confidence floor up to date.
        """
        self._loss_streak = max(0, streak)

    def reset_loss_streak(self) -> None:
        """Reset loss streak to zero (e.g., after a win)."""
        self._loss_streak = 0

    # ═════════════════════════════════════════════════════
    #  4-BRAIN ALIGNMENT
    # ═════════════════════════════════════════════════════

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

        # ── Brain 1: Indicators ───────────────────────────────────
        indicators = getattr(market, "indicators", {})
        b1_signal = self._score_indicators(indicators)
        if b1_signal == direction:
            votes += 1

        # ── Brain 2: Sentiment ────────────────────────────────────
        sentiment = getattr(market, "sentiment_score", 0.0)
        if direction == "BUY" and sentiment > 0.1:
            votes += 1
        elif direction == "SELL" and sentiment < -0.1:
            votes += 1

        # ── Brain 3: Chart Pattern ────────────────────────────────
        chart = getattr(market, "chart_pattern", None)
        if chart and chart.get("signal", "HOLD").upper() == direction:
            votes += 1

        # ── Brain 4: AI Prediction ────────────────────────────────
        ai = getattr(market, "ai_prediction", None)
        if ai and ai.get("signal", "HOLD").upper() == direction:
            votes += 1

        return votes

    def check_brain_alignment(
        self, market: MarketState, direction: str
    ) -> Tuple[bool, int]:
        """
        Check if brain alignment meets minimum threshold.

        Returns:
            (passes: bool, vote_count: int)
        """
        votes = self._count_brain_votes(market, direction)
        return votes >= self._min_brain_alignment, votes

    def _score_indicators(self, indicators: Dict) -> str:
        """
        Score Brain1 indicators to get direction signal.

        Mirrors controller._brain_indicators() logic.
        """
        score = 0

        rsi = indicators.get("rsi")
        if rsi is not None:
            if rsi < 35:
                score += 1
            elif rsi > 65:
                score -= 1

        macd_cross = indicators.get("macd_cross")
        if macd_cross == "bullish":
            score += 1
        elif macd_cross == "bearish":
            score -= 1

        ema_cross = indicators.get("ema_cross")
        if ema_cross == "bullish":
            score += 1
        elif ema_cross == "bearish":
            score -= 1

        bb_position = indicators.get("bb_position")
        if bb_position == "oversold":
            score += 1
        elif bb_position == "overbought":
            score -= 1

        if score > 0:
            return "BUY"
        if score < 0:
            return "SELL"
        return "HOLD"

    # ═════════════════════════════════════════════════════
    #  CONFIDENCE FROM BRAINS
    # ═════════════════════════════════════════════════════

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
            weights: Optional custom weights (default balanced)

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

        # Brain 2: Sentiment
        sentiment = getattr(market, "sentiment_score", 0.0)
        if direction == "BUY" and sentiment > 0:
            score += w["sentiment"] * min(abs(sentiment), 1.0)
        elif direction == "SELL" and sentiment < 0:
            score += w["sentiment"] * min(abs(sentiment), 1.0)

        # Brain 3: Chart
        chart = getattr(market, "chart_pattern", None)
        if chart and chart.get("signal", "HOLD").upper() == direction:
            chart_conf = chart.get("confidence", 0) / 100
            score += w["chart"] * chart_conf

        # Brain 4: AI
        ai = getattr(market, "ai_prediction", None)
        if ai and ai.get("signal", "HOLD").upper() == direction:
            ai_conf = ai.get("confidence", 0) / 100
            score += w["ai"] * ai_conf

        return self._normalize_confidence(score)

    # ═════════════════════════════════════════════════════
    #  RISK/REWARD CALCULATION
    # ═════════════════════════════════════════════════════

    def _calculate_rr(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> float:
        """Calculate risk/reward ratio."""
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)

        if risk == 0:
            return 0.0

        return round(reward / risk, 3)

    def validate_risk_reward(
        self,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
    ) -> bool:
        """Check if RR meets minimum threshold."""
        rr = self._calculate_rr(entry_price, stop_loss, take_profit)
        return rr >= self._min_risk_reward

    # ═════════════════════════════════════════════════════
    #  ATR-BASED SL/TP HELPERS
    # ═════════════════════════════════════════════════════

    def atr_stop_loss(
        self,
        market: MarketState,
        direction: str,
        atr_multiplier: float = 1.5,
    ) -> float:
        """
        Calculate ATR-based stop loss.

        Args:
            market: MarketState with price and atr
            direction: "BUY" or "SELL"
            atr_multiplier: ATR multiplier (default 1.5)

        Returns:
            Stop loss price
        """
        direction = direction.upper()
        distance = market.atr * atr_multiplier

        if direction == "BUY":
            return round(market.price - distance, 8)
        elif direction == "SELL":
            return round(market.price + distance, 8)
        else:
            raise ValueError(f"Invalid direction: {direction}")

    def atr_take_profit(
        self,
        market: MarketState,
        direction: str,
        risk_reward: float = 2.0,
        atr_multiplier: float = 1.5,
    ) -> float:
        """
        Calculate ATR-based take profit.

        Args:
            market: MarketState with price and atr
            direction: "BUY" or "SELL"
            risk_reward: Target R:R ratio
            atr_multiplier: ATR multiplier for stop distance

        Returns:
            Take profit price
        """
        stop = self.atr_stop_loss(market, direction, atr_multiplier)
        risk = abs(market.price - stop)
        reward = risk * risk_reward

        direction = direction.upper()
        if direction == "BUY":
            return round(market.price + reward, 8)
        elif direction == "SELL":
            return round(market.price - reward, 8)
        else:
            raise ValueError(f"Invalid direction: {direction}")

    def dynamic_risk_reward(self, market: MarketState) -> float:
        """
        Get dynamic R:R target based on volatility regime.

        Tighter targets in volatile markets (take profits quickly).
        Wider targets in calm markets (let winners run).
        """
        regime_rr = {
            "low": 2.5,
            "normal": 2.0,
            "high": 1.8,
            "extreme": 1.5,
        }
        return regime_rr.get(market.volatility_regime, 2.0)

    # ═════════════════════════════════════════════════════
    #  REGIME / VOLATILITY GATES
    # ═════════════════════════════════════════════════════

    def regime_allowed(
        self, market: MarketState, allowed_regimes: List[str]
    ) -> bool:
        """Check if current regime is in allowed list."""
        return market.regime in allowed_regimes

    def volatility_ok(self, market: MarketState) -> bool:
        """Check if volatility is within strategy limits."""
        return market.volatility <= self._max_volatility

    def trend_aligned(self, market: MarketState, direction: str) -> bool:
        """Check if trend aligns with direction."""
        direction = direction.upper()
        if direction == "BUY":
            return market.trend == "bullish"
        elif direction == "SELL":
            return market.trend == "bearish"
        return False

    # ═════════════════════════════════════════════════════
    #  MULTI-FACTOR SCORING
    # ═════════════════════════════════════════════════════

    def weighted_score(self, factors: List[Dict]) -> float:
        """
        Calculate weighted average of factor scores.

        Args:
            factors: List of dicts with 'score' and 'weight' keys

        Returns:
            Weighted average (0-1)
        """
        if not factors:
            return 0.0

        total_weight = sum(f.get("weight", 0) for f in factors)
        if total_weight == 0:
            return 0.0

        weighted_sum = sum(
            f.get("score", 0) * f.get("weight", 0)
            for f in factors
        )

        return round(weighted_sum / total_weight, 3)

    def build_factor_list(
        self, market: MarketState, direction: str
    ) -> List[Dict]:
        """
        Build standard factor list from MarketState.

        Returns list of factor dicts for scoring.
        """
        direction = direction.upper()
        factors = []

        # Trend alignment
        trend_score = 1.0 if self.trend_aligned(market, direction) else 0.0
        factors.append({
            "name": "trend_alignment",
            "score": trend_score,
            "weight": 0.25,
        })

        # RSI position
        if direction == "BUY":
            rsi_score = max(0, (70 - market.rsi) / 40)  # Best at low RSI
        else:
            rsi_score = max(0, (market.rsi - 30) / 40)  # Best at high RSI
        factors.append({
            "name": "rsi_position",
            "score": round(min(1.0, rsi_score), 3),
            "weight": 0.15,
        })

        # Volume pressure
        if direction == "BUY":
            vol_score = max(0, market.volume_pressure)
        else:
            vol_score = max(0, -market.volume_pressure)
        factors.append({
            "name": "volume_pressure",
            "score": round(min(1.0, vol_score), 3),
            "weight": 0.20,
        })

        # Momentum
        mom_score = min(1.0, market.momentum_strength * 100)
        factors.append({
            "name": "momentum",
            "score": round(mom_score, 3),
            "weight": 0.20,
        })

        # Market confidence
        factors.append({
            "name": "market_confidence",
            "score": market.confidence_score,
            "weight": 0.20,
        })

        return factors

    # ═════════════════════════════════════════════════════
    #  POSITION SIZING SUGGESTION
    # ═════════════════════════════════════════════════════

    def suggested_position_size(
        self,
        account_balance: float,
        risk_percent: float,
        entry_price: float,
        stop_loss: float,
    ) -> float:
        """
        Calculate position size based on risk amount.

        Args:
            account_balance: Available balance
            risk_percent: Risk per trade (e.g., 0.01 = 1%)
            entry_price: Planned entry price
            stop_loss: Planned stop loss

        Returns:
            Position size (quantity)
        """
        risk_amount = account_balance * risk_percent
        risk_per_unit = abs(entry_price - stop_loss)

        if risk_per_unit == 0:
            return 0.0

        return round(risk_amount / risk_per_unit, 8)

    # ═════════════════════════════════════════════════════
    #  UTILITY METHODS
    # ═════════════════════════════════════════════════════

    def get_symbol(self) -> str:
        """Get the strategy's trading symbol."""
        return self.symbol

    def get_config(self) -> Dict:
        """Get strategy configuration for display/logging."""
        return {
            "name": self.name,
            "symbol": self.symbol,
            "min_confidence": self._min_confidence,
            "min_risk_reward": self._min_risk_reward,
            "max_volatility": self._max_volatility,
            "min_brain_alignment": self._min_brain_alignment,
            "loss_streak": self._loss_streak,
            "confidence_floor": self.get_confidence_floor(),
        }

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__name__} '{self.name}' | "
            f"Symbol={self.symbol} | "
            f"MinConf={self._min_confidence}>"
        )