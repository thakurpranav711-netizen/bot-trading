# app/strategies/scalping.py

"""
4-Brain Adaptive Scalping Strategy — Production Grade

A sophisticated scalping strategy designed for autonomous trading.

Entry Logic:
- Bidirectional: BUY on bullish setups, SELL on bearish setups
- Multiple setup types: Momentum, Mean Reversion, Breakout
- Requires brain alignment gate (min 2 of 4 brains agree)
- Confidence from weighted 4-brain signals + factor scoring
- Dynamic SL/TP via ATR + volatility regime
- Regime filter with setup-specific allowances
- Volatility band filter (min/max)
- Cooldown between signals

Exit Logic:
- Trend reversal detection
- RSI extreme (overbought/oversold)
- MACD histogram flip
- Momentum deceleration
- Sentiment reversal
- Chart pattern counter-signal
- Time-based exit for stale positions
- Trailing stop suggestions

Risk Management:
- Adaptive ATR multiplier based on volatility
- Dynamic R:R targets based on market regime
- Loss streak awareness via adaptive confidence floor
- Position scaling suggestions
"""

from typing import Dict, Optional, List, Tuple
from datetime import datetime, timedelta
from app.strategies.base import BaseStrategy, SignalQuality
from app.market.analyzer import MarketState
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  SETUP TYPES
# ═══════════════════════════════════════════════════════════════════

class SetupType:
    """Trading setup classifications."""
    MOMENTUM = "momentum"
    MEAN_REVERSION = "mean_reversion"
    BREAKOUT = "breakout"
    TREND_CONTINUATION = "trend_continuation"
    REVERSAL = "reversal"


# ═══════════════════════════════════════════════════════════════════
#  SCALPING STRATEGY
# ═══════════════════════════════════════════════════════════════════

class ScalpingStrategy(BaseStrategy):
    """
    4-Brain Adaptive Scalping Strategy

    Designed for quick entries and exits with tight risk management.
    Supports multiple setup types and adapts to market conditions.
    """

    # ── Strategy identifier ───────────────────────────────────────
    name = "4brain_scalping"
    version = "2.1.0"

    # ── Class defaults ────────────────────────────────────────────
    MIN_CONFIDENCE = 0.55
    MIN_RISK_REWARD = 1.5
    MIN_BRAIN_ALIGNMENT = 2
    MAX_VOLATILITY_ALLOWED = 0.08
    MIN_VOLATILITY_REQUIRED = 0.002
    MAX_LOSS_STREAK_ALLOWED = 5

    # ── Allowed market regimes per setup type ─────────────────────
    REGIME_SETUPS = {
        SetupType.MOMENTUM: ["trending", "explosive"],
        SetupType.MEAN_REVERSION: ["ranging"],
        SetupType.BREAKOUT: ["ranging", "explosive"],
        SetupType.TREND_CONTINUATION: ["trending"],
        SetupType.REVERSAL: ["trending", "explosive"],
    }

    # ── Default allowed regimes ───────────────────────────────────
    ALLOWED_REGIMES = ["trending", "explosive", "ranging"]

    def __init__(
        self,
        symbol: str,
        risk_reward_ratio: float = 2.0,
        atr_multiplier: float = 1.2,
        min_volatility_pct: float = 0.002,
        max_volatility_pct: float = 0.025,
        min_confidence: float = None,
        min_risk_reward: float = None,
        allowed_regimes: List[str] = None,
        enable_mean_reversion: bool = True,
        enable_breakout: bool = True,
        signal_cooldown_seconds: int = 60,
        max_position_hold_minutes: int = 120,
    ):
        """
        Initialize scalping strategy.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            risk_reward_ratio: Target R:R (default 2.0)
            atr_multiplier: ATR multiplier for SL (default 1.2)
            min_volatility_pct: Minimum ATR/price for entry
            max_volatility_pct: Maximum ATR/price for entry
            min_confidence: Override base MIN_CONFIDENCE
            min_risk_reward: Override base MIN_RISK_REWARD
            allowed_regimes: Override ALLOWED_REGIMES
            enable_mean_reversion: Allow mean reversion setups
            enable_breakout: Allow breakout setups
            signal_cooldown_seconds: Minimum time between signals
            max_position_hold_minutes: Max hold time before time exit
        """
        super().__init__(
            symbol=symbol,
            min_confidence=min_confidence if min_confidence is not None else self.MIN_CONFIDENCE,
            min_risk_reward=min_risk_reward if min_risk_reward is not None else self.MIN_RISK_REWARD,
            min_volatility=min_volatility_pct,
            max_volatility=max_volatility_pct,
            allowed_regimes=allowed_regimes or self.ALLOWED_REGIMES,
        )

        # Strategy-specific parameters
        self.rr_ratio = risk_reward_ratio
        self.atr_multiplier = atr_multiplier
        self.min_volatility_pct = min_volatility_pct
        self.max_volatility_pct = max_volatility_pct
        self.enable_mean_reversion = enable_mean_reversion
        self.enable_breakout = enable_breakout
        self.signal_cooldown_seconds = signal_cooldown_seconds
        self.max_position_hold_minutes = max_position_hold_minutes

        # Tracking
        self._last_entry_time: Optional[datetime] = None
        self._setup_stats: Dict[str, Dict] = {
            setup: {"signals": 0, "wins": 0, "losses": 0}
            for setup in [SetupType.MOMENTUM, SetupType.MEAN_REVERSION, 
                         SetupType.BREAKOUT, SetupType.TREND_CONTINUATION]
        }

        logger.info(
            f"📊 ScalpingStrategy v{self.version} initialized | "
            f"{symbol} | RR={risk_reward_ratio} | ATR×{atr_multiplier}"
        )

    # ═══════════════════════════════════════════════════════════════
    #  ENTRY LOGIC
    # ═══════════════════════════════════════════════════════════════

    def should_enter(self, market: MarketState) -> Optional[Dict]:
        """
        Evaluate entry conditions for multiple setup types.

        Returns the highest-confidence qualifying signal.
        """
        # ── Validate market data ──────────────────────────────────
        if not self._validate_market_data(market):
            return None

        # ── Check preconditions ───────────────────────────────────
        passes, reason = self.check_preconditions(market)
        if not passes:
            logger.debug(f"⛔ Precondition failed: {reason}")
            return None

        # ── Check signal cooldown ─────────────────────────────────
        if not self._check_signal_cooldown():
            logger.debug("⛔ Signal cooldown active")
            return None

        # ── Volatility band filter ────────────────────────────────
        vol_check, vol_reason = self._check_volatility_band(market)
        if not vol_check:
            logger.debug(f"⛔ {vol_reason}")
            return None

        # ── Evaluate all setup types ──────────────────────────────
        signals = []

        # Momentum setups (trend following)
        momentum_signal = self._evaluate_momentum_setup(market)
        if momentum_signal:
            signals.append(momentum_signal)

        # Mean reversion setups (ranging markets)
        if self.enable_mean_reversion:
            mr_signal = self._evaluate_mean_reversion_setup(market)
            if mr_signal:
                signals.append(mr_signal)

        # Breakout setups
        if self.enable_breakout:
            breakout_signal = self._evaluate_breakout_setup(market)
            if breakout_signal:
                signals.append(breakout_signal)

        # ── Select best signal ────────────────────────────────────
        if not signals:
            return None

        # Sort by confidence and quality
        def signal_score(s):
            quality_scores = {
                SignalQuality.EXCELLENT: 100,
                SignalQuality.GOOD: 75,
                SignalQuality.MODERATE: 50,
                SignalQuality.WEAK: 25,
            }
            return (
                quality_scores.get(s.get("quality", SignalQuality.WEAK), 0) +
                s.get("confidence", 0) * 50
            )

        signals.sort(key=signal_score, reverse=True)
        best_signal = signals[0]

        # Update tracking
        self._last_entry_time = datetime.utcnow()
        setup_type = best_signal.get("metadata", {}).get("setup_type", "unknown")
        if setup_type in self._setup_stats:
            self._setup_stats[setup_type]["signals"] += 1

        return best_signal

    # ═══════════════════════════════════════════════════════════════
    #  MOMENTUM SETUP
    # ═══════════════════════════════════════════════════════════════

    def _evaluate_momentum_setup(self, market: MarketState) -> Optional[Dict]:
        """
        Evaluate momentum/trend-following setup.

        Entry when:
        - Strong trend alignment
        - Momentum confirmation
        - Volume confirmation
        """
        # Check regime
        if market.regime not in self.REGIME_SETUPS[SetupType.MOMENTUM]:
            return None

        # Evaluate long momentum
        if market.trend == "bullish":
            signal = self._evaluate_long_momentum(market)
            if signal:
                return signal

        # Evaluate short momentum
        if market.trend == "bearish":
            signal = self._evaluate_short_momentum(market)
            if signal:
                return signal

        return None

    def _evaluate_long_momentum(self, market: MarketState) -> Optional[Dict]:
        """Evaluate long momentum setup."""
        direction = "BUY"

        # ── Pre-filters ───────────────────────────────────────────
        # Skip if RSI overbought
        if market.rsi is not None and market.rsi > 72:
            logger.debug("⛔ RSI overbought — skip long momentum")
            return None

        # Skip if price too far from EMA (overextended)
        if market.price > 0 and market.ema_20 > 0:
            extension = (market.price - market.ema_20) / market.ema_20
            if extension > 0.03:  # More than 3% above EMA20
                logger.debug("⛔ Price overextended — skip long momentum")
                return None

        # ── Momentum confirmation ─────────────────────────────────
        momentum_ok = (
            market.momentum_strength > 0.001 and
            market.momentum_acceleration >= 0
        )
        if not momentum_ok:
            return None

        # ── Calculate confidence ──────────────────────────────────
        brain_conf = self.confidence_from_brains(market, direction)
        factors = self._build_momentum_factors(market, direction)
        factor_conf = self.weighted_score(factors)

        # Blend: 55% brain signals, 45% technical factors
        confidence = round(brain_conf * 0.55 + factor_conf * 0.45, 3)

        # ── Calculate SL/TP ───────────────────────────────────────
        adaptive_mult = self._adaptive_atr_multiplier(market)
        sl, tp = self.calculate_sl_tp(market, direction, adaptive_mult)
        rr = self._calculate_rr(market.price, sl, tp, direction)

        # ── Build entry signal ────────────────────────────────────
        reason = f"Long momentum: Trend={market.trend}, RSI={market.rsi:.1f}, MACD={'▲' if market.macd_histogram > 0 else '▼'}"

        logger.info(
            f"🟢 LONG MOMENTUM | {self.symbol} @ ${market.price:,.2f} | "
            f"Conf={confidence:.0%} | RR={rr:.1f}"
        )

        return self.build_entry_signal(
            market=market,
            direction=direction,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=reason,
            factors=factors,
            require_brain_alignment=True,
            metadata=self._build_entry_metadata(market, adaptive_mult, SetupType.MOMENTUM, "bullish"),
        )

    def _evaluate_short_momentum(self, market: MarketState) -> Optional[Dict]:
        """Evaluate short momentum setup."""
        direction = "SELL"

        # ── Pre-filters ───────────────────────────────────────────
        if market.rsi is not None and market.rsi < 28:
            logger.debug("⛔ RSI oversold — skip short momentum")
            return None

        if market.price > 0 and market.ema_20 > 0:
            extension = (market.ema_20 - market.price) / market.ema_20
            if extension > 0.03:
                logger.debug("⛔ Price overextended — skip short momentum")
                return None

        # ── Momentum confirmation ─────────────────────────────────
        momentum_ok = (
            market.momentum_strength > 0.001 and
            market.momentum_acceleration <= 0
        )
        if not momentum_ok:
            return None

        # ── Calculate confidence ──────────────────────────────────
        brain_conf = self.confidence_from_brains(market, direction)
        factors = self._build_momentum_factors(market, direction)
        factor_conf = self.weighted_score(factors)

        confidence = round(brain_conf * 0.55 + factor_conf * 0.45, 3)

        # ── Calculate SL/TP ───────────────────────────────────────
        adaptive_mult = self._adaptive_atr_multiplier(market)
        sl, tp = self.calculate_sl_tp(market, direction, adaptive_mult)
        rr = self._calculate_rr(market.price, sl, tp, direction)

        reason = f"Short momentum: Trend={market.trend}, RSI={market.rsi:.1f}, MACD={'▲' if market.macd_histogram > 0 else '▼'}"

        logger.info(
            f"🔴 SHORT MOMENTUM | {self.symbol} @ ${market.price:,.2f} | "
            f"Conf={confidence:.0%} | RR={rr:.1f}"
        )

        return self.build_entry_signal(
            market=market,
            direction=direction,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=reason,
            factors=factors,
            require_brain_alignment=True,
            metadata=self._build_entry_metadata(market, adaptive_mult, SetupType.MOMENTUM, "bearish"),
        )

    def _build_momentum_factors(self, market: MarketState, direction: str) -> List[Dict]:
        """Build factor list for momentum scoring."""
        factors = []
        is_long = direction.upper() == "BUY"

        # ── Trend strength via EMA alignment ──────────────────────
        if market.price > 0:
            if is_long:
                ema_spread = (market.ema_20 - market.ema_50) / market.price
            else:
                ema_spread = (market.ema_50 - market.ema_20) / market.price
            trend_score = min(max(ema_spread / 0.005, 0.0), 1.0)
        else:
            trend_score = 0.0
        factors.append({
            "name": "trend_strength",
            "score": round(trend_score, 3),
            "weight": 0.25,
        })

        # ── RSI quality ───────────────────────────────────────────
        rsi = market.rsi or 50
        if is_long:
            if 45 <= rsi <= 65:
                rsi_score = 1.0
            elif 65 < rsi <= 72:
                rsi_score = 0.6
            elif 35 <= rsi < 45:
                rsi_score = 0.7
            else:
                rsi_score = 0.3
        else:
            if 35 <= rsi <= 55:
                rsi_score = 1.0
            elif 28 <= rsi < 35:
                rsi_score = 0.6
            elif 55 < rsi <= 65:
                rsi_score = 0.7
            else:
                rsi_score = 0.3
        factors.append({
            "name": "rsi_quality",
            "score": rsi_score,
            "weight": 0.20,
        })

        # ── Volume confirmation ───────────────────────────────────
        vol_score = 0.8 if market.volume_spike else 0.4
        if is_long and market.volume_pressure > 0.2:
            vol_score = min(1.0, vol_score + 0.2)
        elif not is_long and market.volume_pressure < -0.2:
            vol_score = min(1.0, vol_score + 0.2)
        factors.append({
            "name": "volume_confirm",
            "score": round(vol_score, 3),
            "weight": 0.20,
        })

        # ── MACD alignment ────────────────────────────────────────
        macd_hist = market.macd_histogram
        if is_long:
            macd_score = 1.0 if macd_hist > 0 else 0.2
        else:
            macd_score = 1.0 if macd_hist < 0 else 0.2
        factors.append({
            "name": "macd_direction",
            "score": macd_score,
            "weight": 0.20,
        })

        # ── ADX strength ──────────────────────────────────────────
        adx = getattr(market, "adx", 25)
        adx_score = min(1.0, adx / 40)  # Strong trend above 40
        factors.append({
            "name": "adx_strength",
            "score": round(adx_score, 3),
            "weight": 0.15,
        })

        return factors

    # ═══════════════════════════════════════════════════════════════
    #  MEAN REVERSION SETUP
    # ═══════════════════════════════════════════════════════════════

    def _evaluate_mean_reversion_setup(self, market: MarketState) -> Optional[Dict]:
        """
        Evaluate mean reversion setup.

        Entry when:
        - Ranging market
        - RSI at extremes
        - Price at Bollinger Band extremes
        - Volume spike for confirmation
        """
        # Check regime
        if market.regime not in self.REGIME_SETUPS[SetupType.MEAN_REVERSION]:
            return None

        # Long mean reversion (oversold bounce)
        if self._is_oversold_bounce(market):
            return self._build_mean_reversion_signal(market, "BUY")

        # Short mean reversion (overbought rejection)
        if self._is_overbought_rejection(market):
            return self._build_mean_reversion_signal(market, "SELL")

        return None

    def _is_oversold_bounce(self, market: MarketState) -> bool:
        """Check for oversold bounce conditions."""
        conditions = [
            market.rsi is not None and market.rsi < 32,
            market.bb_percent_b < 0.15,
            market.volume_pressure > 0,  # Buyers stepping in
        ]
        return sum(conditions) >= 2

    def _is_overbought_rejection(self, market: MarketState) -> bool:
        """Check for overbought rejection conditions."""
        conditions = [
            market.rsi is not None and market.rsi > 68,
            market.bb_percent_b > 0.85,
            market.volume_pressure < 0,  # Sellers stepping in
        ]
        return sum(conditions) >= 2

    def _build_mean_reversion_signal(
        self, market: MarketState, direction: str
    ) -> Optional[Dict]:
        """Build mean reversion entry signal."""
        is_long = direction == "BUY"

        # Calculate confidence
        brain_conf = self.confidence_from_brains(market, direction)
        factors = self._build_mean_reversion_factors(market, direction)
        factor_conf = self.weighted_score(factors)

        # Mean reversion relies more on technical factors
        confidence = round(brain_conf * 0.40 + factor_conf * 0.60, 3)

        # Tighter stops for mean reversion
        adaptive_mult = self._adaptive_atr_multiplier(market) * 0.8
        sl, tp = self.calculate_sl_tp(market, direction, adaptive_mult, risk_reward=1.8)
        rr = self._calculate_rr(market.price, sl, tp, direction)

        setup_name = "Oversold bounce" if is_long else "Overbought rejection"
        reason = f"{setup_name}: RSI={market.rsi:.1f}, BB%B={market.bb_percent_b:.2f}"

        logger.info(
            f"{'🟢' if is_long else '🔴'} MEAN REVERSION | {self.symbol} @ ${market.price:,.2f} | "
            f"Conf={confidence:.0%} | RR={rr:.1f}"
        )

        return self.build_entry_signal(
            market=market,
            direction=direction,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=reason,
            factors=factors,
            require_brain_alignment=False,  # Mean reversion can go against trend
            metadata=self._build_entry_metadata(market, adaptive_mult, SetupType.MEAN_REVERSION, 
                                                "bullish" if is_long else "bearish"),
        )

    def _build_mean_reversion_factors(self, market: MarketState, direction: str) -> List[Dict]:
        """Build factors for mean reversion scoring."""
        factors = []
        is_long = direction == "BUY"

        # RSI extreme
        rsi = market.rsi or 50
        if is_long:
            rsi_score = min(1.0, (35 - rsi) / 15) if rsi < 35 else 0.3
        else:
            rsi_score = min(1.0, (rsi - 65) / 15) if rsi > 65 else 0.3
        factors.append({
            "name": "rsi_extreme",
            "score": max(0, round(rsi_score, 3)),
            "weight": 0.30,
        })

        # Bollinger Band position
        bb_pct = market.bb_percent_b
        if is_long:
            bb_score = min(1.0, (0.2 - bb_pct) / 0.2) if bb_pct < 0.2 else 0.2
        else:
            bb_score = min(1.0, (bb_pct - 0.8) / 0.2) if bb_pct > 0.8 else 0.2
        factors.append({
            "name": "bb_extreme",
            "score": max(0, round(bb_score, 3)),
            "weight": 0.30,
        })

        # Volume spike (reversal confirmation)
        vol_score = 0.8 if market.volume_spike else 0.3
        factors.append({
            "name": "volume_spike",
            "score": vol_score,
            "weight": 0.20,
        })

        # Distance from mean (EMA20)
        if market.price > 0 and market.ema_20 > 0:
            deviation = abs(market.price - market.ema_20) / market.ema_20
            reversion_potential = min(1.0, deviation / 0.02)
        else:
            reversion_potential = 0.5
        factors.append({
            "name": "reversion_potential",
            "score": round(reversion_potential, 3),
            "weight": 0.20,
        })

        return factors

    # ═══════════════════════════════════════════════════════════════
    #  BREAKOUT SETUP
    # ═══════════════════════════════════════════════════════════════

    def _evaluate_breakout_setup(self, market: MarketState) -> Optional[Dict]:
        """
        Evaluate breakout setup.

        Entry when:
        - Structure break detected
        - Volume confirmation
        - Bollinger squeeze preceding breakout (optional)
        """
        # Check regime
        if market.regime not in self.REGIME_SETUPS[SetupType.BREAKOUT]:
            return None

        # Need structure break
        if not market.structure_break:
            return None

        direction = "BUY" if market.break_direction == "up" else "SELL"
        
        # Confirm with volume
        if direction == "BUY" and market.volume_pressure < 0:
            return None
        if direction == "SELL" and market.volume_pressure > 0:
            return None

        return self._build_breakout_signal(market, direction)

    def _build_breakout_signal(self, market: MarketState, direction: str) -> Optional[Dict]:
        """Build breakout entry signal."""
        is_long = direction == "BUY"

        # Calculate confidence
        brain_conf = self.confidence_from_brains(market, direction)
        factors = self._build_breakout_factors(market, direction)
        factor_conf = self.weighted_score(factors)

        confidence = round(brain_conf * 0.50 + factor_conf * 0.50, 3)

        # Wider stops for breakouts (more volatility expected)
        adaptive_mult = self._adaptive_atr_multiplier(market) * 1.2
        # Higher R:R for breakouts
        sl, tp = self.calculate_sl_tp(market, direction, adaptive_mult, risk_reward=2.5)
        rr = self._calculate_rr(market.price, sl, tp, direction)

        reason = f"Breakout {market.break_direction}: Vol spike={market.volume_spike}, Squeeze={market.bb_squeeze}"

        logger.info(
            f"{'🟢' if is_long else '🔴'} BREAKOUT | {self.symbol} @ ${market.price:,.2f} | "
            f"Dir={market.break_direction} | Conf={confidence:.0%} | RR={rr:.1f}"
        )

        return self.build_entry_signal(
            market=market,
            direction=direction,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason=reason,
            factors=factors,
            require_brain_alignment=True,
            metadata=self._build_entry_metadata(market, adaptive_mult, SetupType.BREAKOUT,
                                                "bullish" if is_long else "bearish"),
        )

    def _build_breakout_factors(self, market: MarketState, direction: str) -> List[Dict]:
        """Build factors for breakout scoring."""
        factors = []

        # Structure break strength
        factors.append({
            "name": "structure_break",
            "score": 1.0,
            "weight": 0.25,
        })

        # Volume confirmation
        vol_score = 1.0 if market.volume_spike else 0.5
        if abs(market.volume_pressure) > 0.3:
            vol_score = min(1.0, vol_score + 0.2)
        factors.append({
            "name": "volume_confirm",
            "score": vol_score,
            "weight": 0.25,
        })

        # Bollinger squeeze (setup quality)
        squeeze_score = 0.9 if getattr(market, "bb_squeeze", False) else 0.4
        factors.append({
            "name": "bb_squeeze",
            "score": squeeze_score,
            "weight": 0.20,
        })

        # Momentum alignment
        is_long = direction == "BUY"
        if is_long:
            mom_score = 1.0 if market.momentum_strength > 0 and market.momentum_acceleration > 0 else 0.4
        else:
            mom_score = 1.0 if market.momentum_strength > 0 and market.momentum_acceleration < 0 else 0.4
        factors.append({
            "name": "momentum_align",
            "score": mom_score,
            "weight": 0.15,
        })

        # ADX (low ADX before breakout is good)
        adx = getattr(market, "adx", 25)
        adx_score = 0.8 if adx < 25 else 0.5  # Low ADX = ranging, ready for breakout
        factors.append({
            "name": "adx_setup",
            "score": adx_score,
            "weight": 0.15,
        })

        return factors

    # ═══════════════════════════════════════════════════════════════
    #  EXIT LOGIC
    # ═══════════════════════════════════════════════════════════════

    def should_exit(self, market: MarketState, position: Dict) -> Optional[Dict]:
        """
        Evaluate exit conditions.

        Multiple exit types:
        - Signal-based (trend reversal, indicator flip)
        - Time-based (stale position)
        - Trailing stop suggestion
        """
        entry_price = position.get("entry_price", position.get("avg_price", market.price))
        quantity = position.get("quantity", 0)
        side = position.get("side", "long")
        is_long = side == "long"

        # Calculate current PnL
        if is_long:
            pnl_pct = ((market.price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - market.price) / entry_price) * 100

        # ── Check time-based exit ─────────────────────────────────
        time_exit = self._check_time_exit(position, pnl_pct)
        if time_exit:
            return time_exit

        # ── Collect signal-based exit factors ─────────────────────
        factors = self._collect_exit_factors(market, position, is_long, pnl_pct)

        if not factors:
            return None

        # Calculate exit confidence
        confidence = self.weighted_score(factors)

        # Dynamic exit threshold based on PnL
        exit_threshold = 0.45
        if pnl_pct > 1.5:
            exit_threshold = 0.35  # Easier exit when in profit
        elif pnl_pct < -0.5:
            exit_threshold = 0.55  # Harder exit when in loss (let SL handle it)

        if confidence < exit_threshold:
            return None

        # Build reason
        reason_parts = [f["name"] for f in factors if f["score"] > 0.5]
        reason = " + ".join(reason_parts[:3]) if reason_parts else "Multiple signals"

        return self.build_exit_signal(
            market=market,
            position=position,
            confidence=confidence,
            reason=reason,
            exit_type="signal",
            metadata={
                "pnl_pct": round(pnl_pct, 3),
                "factors": [f["name"] for f in factors],
                "setup_type": position.get("metadata", {}).get("setup_type", "unknown"),
            },
        )

    def _check_time_exit(self, position: Dict, pnl_pct: float) -> Optional[Dict]:
        """Check for time-based exit."""
        opened_at = position.get("opened_at", "")
        if not opened_at:
            return None

        try:
            open_time = datetime.fromisoformat(opened_at.replace('Z', '+00:00'))
            hold_minutes = (datetime.utcnow() - open_time.replace(tzinfo=None)).total_seconds() / 60

            # Exit if held too long
            if hold_minutes > self.max_position_hold_minutes:
                # Only if not in significant loss (let SL handle those)
                if pnl_pct > -1.0:
                    logger.info(
                        f"⏰ Time exit | {self.symbol} | "
                        f"Hold={hold_minutes:.0f}min > Max={self.max_position_hold_minutes}min"
                    )
                    return {
                        "symbol": self.symbol,
                        "action": "EXIT",
                        "exit_type": "time",
                        "confidence": 0.60,
                        "reason": f"Max hold time ({hold_minutes:.0f}min)",
                        "exit_price": 0,  # Will be filled by caller
                    }
        except:
            pass

        return None

    def _collect_exit_factors(
        self,
        market: MarketState,
        position: Dict,
        is_long: bool,
        pnl_pct: float,
    ) -> List[Dict]:
        """Collect all exit signal factors."""
        factors = []
        setup_type = position.get("metadata", {}).get("setup_type", SetupType.MOMENTUM)

        # ── Trend reversal ────────────────────────────────────────
        if is_long and market.trend == "bearish":
            factors.append({
                "name": "trend_reversal",
                "score": 0.90,
                "weight": 0.30,
            })
        elif not is_long and market.trend == "bullish":
            factors.append({
                "name": "trend_reversal",
                "score": 0.90,
                "weight": 0.30,
            })

        # ── RSI extreme ───────────────────────────────────────────
        rsi = market.rsi
        if rsi is not None:
            if is_long and rsi > 75:
                rsi_exit = min(1.0, (rsi - 75) / 20)
                factors.append({
                    "name": "rsi_overbought",
                    "score": round(rsi_exit, 3),
                    "weight": 0.20,
                })
            elif not is_long and rsi < 25:
                rsi_exit = min(1.0, (25 - rsi) / 20)
                factors.append({
                    "name": "rsi_oversold",
                    "score": round(rsi_exit, 3),
                    "weight": 0.20,
                })

        # ── MACD histogram flip ───────────────────────────────────
        macd_hist = market.macd_histogram
        if macd_hist is not None:
            if is_long and macd_hist < 0:
                factors.append({
                    "name": "macd_bearish",
                    "score": 0.75,
                    "weight": 0.18,
                })
            elif not is_long and macd_hist > 0:
                factors.append({
                    "name": "macd_bullish",
                    "score": 0.75,
                    "weight": 0.18,
                })

        # ── MACD cross ────────────────────────────────────────────
        macd_cross = getattr(market, "macd_cross", "none")
        if is_long and macd_cross == "bearish":
            factors.append({
                "name": "macd_cross_bearish",
                "score": 0.85,
                "weight": 0.15,
            })
        elif not is_long and macd_cross == "bullish":
            factors.append({
                "name": "macd_cross_bullish",
                "score": 0.85,
                "weight": 0.15,
            })

        # ── Momentum deceleration ─────────────────────────────────
        mom_accel = market.momentum_acceleration
        if is_long and mom_accel < -0.0001:
            factors.append({
                "name": "momentum_decelerating",
                "score": 0.65,
                "weight": 0.12,
            })
        elif not is_long and mom_accel > 0.0001:
            factors.append({
                "name": "momentum_recovering",
                "score": 0.65,
                "weight": 0.12,
            })

        # ── Sentiment flip ────────────────────────────────────────
        sentiment = getattr(market, "sentiment_score", 0.0)
        if is_long and sentiment < -0.3:
            factors.append({
                "name": "sentiment_bearish",
                "score": min(1.0, abs(sentiment)),
                "weight": 0.10,
            })
        elif not is_long and sentiment > 0.3:
            factors.append({
                "name": "sentiment_bullish",
                "score": min(1.0, abs(sentiment)),
                "weight": 0.10,
            })

        # ── Chart pattern counter-signal ──────────────────────────
        chart = getattr(market, "chart_pattern", None)
        if chart:
            pattern_signal = chart.get("signal", "HOLD").upper()
            pattern_conf = chart.get("confidence", 0) / 100

            if is_long and pattern_signal == "SELL" and pattern_conf > 0.6:
                factors.append({
                    "name": "chart_bearish",
                    "score": pattern_conf,
                    "weight": 0.08,
                })
            elif not is_long and pattern_signal == "BUY" and pattern_conf > 0.6:
                factors.append({
                    "name": "chart_bullish",
                    "score": pattern_conf,
                    "weight": 0.08,
                })

        # ── Profit protection ─────────────────────────────────────
        if pnl_pct > 2.0:
            # In solid profit, be more willing to exit on weakness
            if market.momentum_strength < 0.001:
                factors.append({
                    "name": "profit_protection",
                    "score": 0.70,
                    "weight": 0.12,
                })

        # ── Mean reversion target hit ─────────────────────────────
        if setup_type == SetupType.MEAN_REVERSION:
            # Exit when price returns to mean
            if is_long and market.price >= market.ema_20:
                factors.append({
                    "name": "mean_reached",
                    "score": 0.80,
                    "weight": 0.15,
                })
            elif not is_long and market.price <= market.ema_20:
                factors.append({
                    "name": "mean_reached",
                    "score": 0.80,
                    "weight": 0.15,
                })

        return factors

    # ═══════════════════════════════════════════════════════════════
    #  TRAILING STOP
    # ═══════════════════════════════════════════════════════════════

    def get_trailing_stop_update(
        self, market: MarketState, position: Dict
    ) -> Optional[Dict]:
        """
        Calculate trailing stop update if applicable.

        Returns dict with new stop_loss if update needed.
        """
        # Check for breakeven first
        if self.should_move_to_breakeven(market, position, profit_threshold_pct=1.0):
            entry_price = position.get("entry_price", market.price)
            side = position.get("side", "long")
            
            # Add small buffer for breakeven
            buffer = market.atr * 0.1
            if side == "long":
                new_stop = entry_price + buffer
            else:
                new_stop = entry_price - buffer

            return {
                "type": "breakeven",
                "stop_loss": round(new_stop, 8),
                "reason": "Moved to breakeven",
            }

        # Then check trailing
        new_stop = self.calculate_trailing_stop(
            market=market,
            position=position,
            trailing_percent=0.015,  # 1.5% trailing
            use_atr=True,
            atr_multiplier=1.5,
        )

        if new_stop:
            return {
                "type": "trailing",
                "stop_loss": new_stop,
                "reason": "Trailing stop update",
            }

        return None

    # ═══════════════════════════════════════════════════════════════
    #  HELPER METHODS
    # ═══════════════════════════════════════════════════════════════

    def _validate_market_data(self, market: MarketState) -> bool:
        """Validate required market data is present."""
        required = [
            market.ema_20,
            market.ema_50,
            market.rsi,
            market.atr,
        ]
        
        if any(x is None or x == 0 for x in required):
            logger.debug("⚠️ Missing required market data")
            return False

        if market.price <= 0:
            logger.debug("⚠️ Invalid price")
            return False

        return True

    def _check_volatility_band(self, market: MarketState) -> Tuple[bool, str]:
        """Check if volatility is within acceptable band."""
        if market.price == 0:
            return False, "Price is zero"

        vol_pct = market.atr / market.price

        if vol_pct < self.min_volatility_pct:
            return False, f"Volatility {vol_pct:.4f} < min {self.min_volatility_pct}"

        if vol_pct > self.max_volatility_pct:
            return False, f"Volatility {vol_pct:.4f} > max {self.max_volatility_pct}"

        return True, "OK"

    def _check_signal_cooldown(self) -> bool:
        """Check if enough time has passed since last signal."""
        if self._last_entry_time is None:
            return True

        elapsed = (datetime.utcnow() - self._last_entry_time).total_seconds()
        return elapsed >= self.signal_cooldown_seconds

    def _adaptive_atr_multiplier(self, market: MarketState) -> float:
        """
        Adjust ATR multiplier based on volatility regime.

        Tighter stops in high volatility (avoid noise).
        Wider stops in low volatility (avoid premature exit).
        """
        base = self.atr_multiplier
        regime = market.volatility_regime

        adjustments = {
            "extreme": 0.75,
            "high": 0.85,
            "normal": 1.00,
            "low": 1.15,
        }

        return round(base * adjustments.get(regime, 1.0), 3)

    def _build_entry_metadata(
        self,
        market: MarketState,
        adaptive_mult: float,
        setup_type: str,
        setup_direction: str,
    ) -> Dict:
        """Build metadata dict for entry signal."""
        return {
            "setup_type": setup_type,
            "setup_direction": setup_direction,
            "trend": market.trend,
            "regime": market.regime,
            "rsi": round(market.rsi, 2) if market.rsi else None,
            "atr": market.atr,
            "atr_pct": round(market.atr / market.price * 100, 3) if market.price > 0 else 0,
            "ema_20": market.ema_20,
            "ema_50": market.ema_50,
            "volatility_regime": market.volatility_regime,
            "sentiment_score": getattr(market, "sentiment_score", 0),
            "adx": getattr(market, "adx", 0),
            "volume_spike": market.volume_spike,
            "bb_percent_b": market.bb_percent_b,
            "chart_pattern": (
                getattr(market, "chart_pattern", {}).get("pattern_name", "None")
                if market.chart_pattern else "None"
            ),
            "adaptive_atr_mult": adaptive_mult,
            "base_rr_ratio": self.rr_ratio,
            "strategy_version": self.version,
        }

    # ═══════════════════════════════════════════════════════════════
    #  STATISTICS & CONFIGURATION
    # ═══════════════════════════════════════════════════════════════

    def record_trade_result(self, is_win: bool, setup_type: str = None) -> None:
        """Record trade result for statistics."""
        super().record_trade_result(is_win)

        if setup_type and setup_type in self._setup_stats:
            if is_win:
                self._setup_stats[setup_type]["wins"] += 1
            else:
                self._setup_stats[setup_type]["losses"] += 1

    def get_setup_stats(self) -> Dict:
        """Get performance statistics by setup type."""
        stats = {}
        for setup, data in self._setup_stats.items():
            total = data["wins"] + data["losses"]
            win_rate = (data["wins"] / total * 100) if total > 0 else 0
            stats[setup] = {
                "signals": data["signals"],
                "wins": data["wins"],
                "losses": data["losses"],
                "win_rate": round(win_rate, 1),
            }
        return stats

    def get_config(self) -> Dict:
        """Get full strategy configuration."""
        base_config = super().get_config()
        base_config.update({
            "rr_ratio": self.rr_ratio,
            "atr_multiplier": self.atr_multiplier,
            "min_volatility_pct": self.min_volatility_pct,
            "max_volatility_pct": self.max_volatility_pct,
            "enable_mean_reversion": self.enable_mean_reversion,
            "enable_breakout": self.enable_breakout,
            "signal_cooldown_seconds": self.signal_cooldown_seconds,
            "max_position_hold_minutes": self.max_position_hold_minutes,
            "setup_stats": self.get_setup_stats(),
        })
        return base_config

    def __repr__(self) -> str:
        return (
            f"<ScalpingStrategy '{self.name}' v{self.version} | "
            f"Symbol={self.symbol} | "
            f"RR={self.rr_ratio} | ATR×{self.atr_multiplier}>"
        )