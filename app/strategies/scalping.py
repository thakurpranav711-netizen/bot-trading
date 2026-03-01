# app/strategies/scalping.py

"""
4-Brain Adaptive Scalping Strategy — Production Grade

Entry Logic:
- Bidirectional: BUY on bullish setups, SELL on bearish setups
- Requires brain alignment gate (min 2 of 4 brains agree)
- Confidence from weighted 4-brain signals + factor scoring
- Dynamic SL/TP via ATR + volatility regime
- Regime filter: trending & explosive only (skip ranging)
- Volatility band filter (min/max)

Exit Logic:
- Trend reversal detection
- RSI extreme (overbought/oversold)
- MACD histogram flip
- Momentum deceleration
- Sentiment reversal
- Chart pattern counter-signal

Risk Management:
- Adaptive ATR multiplier based on volatility
- Dynamic R:R targets based on market regime
- Loss streak awareness via adaptive confidence floor
"""

from typing import Dict, Optional, List
from app.strategies.base import BaseStrategy
from app.market.analyzer import MarketState
from app.utils.logger import get_logger

logger = get_logger(__name__)


class ScalpingStrategy(BaseStrategy):
    """
    4-Brain Adaptive Scalping Strategy

    Designed for quick entries and exits with tight risk management.
    Works best in trending and explosive market regimes.
    """

    # ── Strategy identifier ───────────────────────────────────────
    name = "4brain_scalping"

    # ── Class defaults (can be overridden per instance) ───────────
    MIN_CONFIDENCE = 0.55
    MIN_RISK_REWARD = 1.5
    MIN_BRAIN_ALIGNMENT = 2
    MAX_VOLATILITY_ALLOWED = 0.08

    # ── Allowed market regimes ────────────────────────────────────
    ALLOWED_REGIMES = ["trending", "explosive"]

    def __init__(
        self,
        symbol: str,
        risk_reward_ratio: float = 2.0,
        atr_multiplier: float = 1.2,
        min_volatility_pct: float = 0.002,
        max_volatility_pct: float = 0.02,
        min_confidence: float = None,
        allowed_regimes: List[str] = None,
    ):
        """
        Initialize scalping strategy.

        Args:
            symbol: Trading pair (e.g., "BTC/USDT")
            risk_reward_ratio: Target R:R (default 2.0)
            atr_multiplier: ATR multiplier for SL (default 1.2)
            min_volatility_pct: Minimum ATR/price for entry (default 0.2%)
            max_volatility_pct: Maximum ATR/price for entry (default 2%)
            min_confidence: Override base MIN_CONFIDENCE
            allowed_regimes: Override ALLOWED_REGIMES
        """
        # FIX: Pass min_confidence to parent properly
        super().__init__(
            symbol=symbol,
            min_confidence=min_confidence if min_confidence is not None else self.MIN_CONFIDENCE,
        )

        self.rr_ratio = risk_reward_ratio
        self.atr_multiplier = atr_multiplier
        self.min_volatility_pct = min_volatility_pct
        self.max_volatility_pct = max_volatility_pct
        self.allowed_regimes = allowed_regimes or self.ALLOWED_REGIMES

    # ═════════════════════════════════════════════════════
    #  ENTRY LOGIC
    # ═════════════════════════════════════════════════════

    def should_enter(self, market: MarketState) -> Optional[Dict]:
        """
        Evaluate entry conditions for both long and short setups.

        Returns the higher-confidence signal, or None if neither qualifies.
        """
        # ── Null-safety guard ─────────────────────────────────────
        if not self._validate_market_data(market):
            return None

        # ── Regime filter ─────────────────────────────────────────
        if not self.regime_allowed(market, self.allowed_regimes):
            logger.debug(
                f"⛔ Regime '{market.regime}' not in {self.allowed_regimes}"
            )
            return None

        # ── Volatility band filter ────────────────────────────────
        vol_check, vol_reason = self._check_volatility_band(market)
        if not vol_check:
            logger.debug(f"⛔ {vol_reason}")
            return None

        # ── Evaluate both directions ──────────────────────────────
        long_signal = self._evaluate_long(market)
        short_signal = self._evaluate_short(market)

        # ── Return higher confidence signal ───────────────────────
        if long_signal and short_signal:
            if long_signal["confidence"] >= short_signal["confidence"]:
                return long_signal
            return short_signal

        return long_signal or short_signal

    # ═════════════════════════════════════════════════════
    #  LONG SETUP EVALUATION
    # ═════════════════════════════════════════════════════

    def _evaluate_long(self, market: MarketState) -> Optional[Dict]:
        """Evaluate conditions for a BUY entry."""
        direction = "BUY"

        # ── Pre-filters ───────────────────────────────────────────
        if market.trend != "bullish":
            return None

        if market.rsi is not None and market.rsi > 75:
            logger.debug("⛔ RSI overbought — skip long")
            return None

        # ── Calculate confidence ──────────────────────────────────
        brain_conf = self.confidence_from_brains(market, direction)
        factors = self._build_long_factors(market)
        factor_conf = self.weighted_score(factors)

        # Blend: 60% brain signals, 40% technical factors
        confidence = round(brain_conf * 0.60 + factor_conf * 0.40, 3)

        # ── Check confidence floor ────────────────────────────────
        min_conf = self.get_confidence_floor()
        if confidence < min_conf:
            logger.debug(
                f"⛔ Long confidence {confidence:.2f} < floor {min_conf:.2f}"
            )
            return None

        # ── Calculate SL/TP ───────────────────────────────────────
        adaptive_mult = self._adaptive_atr_multiplier(market)
        sl = self.atr_stop_loss(market, direction, adaptive_mult)
        rr = self.dynamic_risk_reward(market)
        tp = self.atr_take_profit(market, direction, rr, adaptive_mult)

        # ── Build entry signal ────────────────────────────────────
        logger.info(
            f"🟢 LONG setup | {self.symbol} @ ${market.price:.2f} | "
            f"BrainConf={brain_conf:.2f} FactorConf={factor_conf:.2f} "
            f"Final={confidence:.2f}"
        )

        return self.build_entry_signal(
            market=market,
            direction=direction,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason="Bullish setup",
            factors=factors,
            require_brain_alignment=True,
            metadata=self._build_entry_metadata(market, adaptive_mult, "bullish"),
        )

    # ═════════════════════════════════════════════════════
    #  SHORT SETUP EVALUATION
    # ═════════════════════════════════════════════════════

    def _evaluate_short(self, market: MarketState) -> Optional[Dict]:
        """Evaluate conditions for a SELL entry."""
        direction = "SELL"

        # ── Pre-filters ───────────────────────────────────────────
        if market.trend != "bearish":
            return None

        if market.rsi is not None and market.rsi < 25:
            logger.debug("⛔ RSI oversold — skip short")
            return None

        # ── Calculate confidence ──────────────────────────────────
        brain_conf = self.confidence_from_brains(market, direction)
        factors = self._build_short_factors(market)
        factor_conf = self.weighted_score(factors)

        # Blend: 60% brain signals, 40% technical factors
        confidence = round(brain_conf * 0.60 + factor_conf * 0.40, 3)

        # ── Check confidence floor ────────────────────────────────
        min_conf = self.get_confidence_floor()
        if confidence < min_conf:
            logger.debug(
                f"⛔ Short confidence {confidence:.2f} < floor {min_conf:.2f}"
            )
            return None

        # ── Calculate SL/TP ───────────────────────────────────────
        adaptive_mult = self._adaptive_atr_multiplier(market)
        sl = self.atr_stop_loss(market, direction, adaptive_mult)
        rr = self.dynamic_risk_reward(market)
        tp = self.atr_take_profit(market, direction, rr, adaptive_mult)

        # ── Build entry signal ────────────────────────────────────
        logger.info(
            f"🔴 SHORT setup | {self.symbol} @ ${market.price:.2f} | "
            f"BrainConf={brain_conf:.2f} FactorConf={factor_conf:.2f} "
            f"Final={confidence:.2f}"
        )

        return self.build_entry_signal(
            market=market,
            direction=direction,
            stop_loss=sl,
            take_profit=tp,
            confidence=confidence,
            reason="Bearish setup",
            factors=factors,
            require_brain_alignment=True,
            metadata=self._build_entry_metadata(market, adaptive_mult, "bearish"),
        )

    # ═════════════════════════════════════════════════════
    #  LONG FACTORS
    # ═════════════════════════════════════════════════════

    def _build_long_factors(self, market: MarketState) -> List[Dict]:
        """Build factor list for long entry scoring."""
        factors = []

        # ── Trend strength via EMA spread ─────────────────────────
        if market.price > 0:
            ema_spread = (market.ema_20 - market.ema_50) / market.price
            trend_score = min(max(ema_spread / 0.005, 0.0), 1.0)
        else:
            trend_score = 0.0
        factors.append({
            "name": "trend_strength",
            "score": round(trend_score, 3),
            "weight": 0.25,
        })

        # ── RSI quality (50-65 ideal for longs) ───────────────────
        rsi = market.rsi or 50
        if 50 <= rsi <= 65:
            rsi_score = 1.0
        elif 65 < rsi <= 72:
            rsi_score = 0.7
        elif 45 <= rsi < 50:
            rsi_score = 0.6
        else:
            rsi_score = 0.3
        factors.append({
            "name": "rsi_quality",
            "score": rsi_score,
            "weight": 0.20,
        })

        # ── Volume confirmation ───────────────────────────────────
        vol_score = 0.9 if market.volume_spike else 0.4
        if market.volume_pressure > 0.2:
            vol_score = min(1.0, vol_score + 0.1)
        factors.append({
            "name": "volume_confirm",
            "score": round(vol_score, 3),
            "weight": 0.20,
        })

        # ── MACD direction ────────────────────────────────────────
        macd_hist = market.macd_histogram
        if macd_hist is not None and macd_hist > 0:
            macd_score = 1.0
        elif macd_hist is not None and macd_hist < 0:
            macd_score = 0.1
        else:
            macd_score = 0.5
        factors.append({
            "name": "macd_direction",
            "score": macd_score,
            "weight": 0.20,
        })

        # ── Momentum continuation ─────────────────────────────────
        mom_score = min(1.0, market.momentum_strength * 120)
        factors.append({
            "name": "momentum",
            "score": round(mom_score, 3),
            "weight": 0.15,
        })

        return factors

    # ═════════════════════════════════════════════════════
    #  SHORT FACTORS
    # ═════════════════════════════════════════════════════

    def _build_short_factors(self, market: MarketState) -> List[Dict]:
        """Build factor list for short entry scoring."""
        factors = []

        # ── Trend strength (bearish = inverted spread) ────────────
        if market.price > 0:
            ema_spread = (market.ema_50 - market.ema_20) / market.price
            trend_score = min(max(ema_spread / 0.005, 0.0), 1.0)
        else:
            trend_score = 0.0
        factors.append({
            "name": "trend_strength",
            "score": round(trend_score, 3),
            "weight": 0.25,
        })

        # ── RSI quality (35-50 ideal for shorts) ──────────────────
        rsi = market.rsi or 50
        if 35 <= rsi <= 50:
            rsi_score = 1.0
        elif 28 <= rsi < 35:
            rsi_score = 0.7
        elif 50 < rsi <= 55:
            rsi_score = 0.6
        else:
            rsi_score = 0.3
        factors.append({
            "name": "rsi_quality",
            "score": rsi_score,
            "weight": 0.20,
        })

        # ── Volume confirmation (bearish pressure) ────────────────
        vol_score = 0.9 if market.volume_spike else 0.4
        if market.volume_pressure < -0.2:
            vol_score = min(1.0, vol_score + 0.1)
        factors.append({
            "name": "volume_confirm",
            "score": round(vol_score, 3),
            "weight": 0.20,
        })

        # ── MACD direction ────────────────────────────────────────
        macd_hist = market.macd_histogram
        if macd_hist is not None and macd_hist < 0:
            macd_score = 1.0
        elif macd_hist is not None and macd_hist > 0:
            macd_score = 0.1
        else:
            macd_score = 0.5
        factors.append({
            "name": "macd_direction",
            "score": macd_score,
            "weight": 0.20,
        })

        # ── Momentum (bearish) ────────────────────────────────────
        mom_score = min(1.0, market.momentum_strength * 120)
        factors.append({
            "name": "momentum",
            "score": round(mom_score, 3),
            "weight": 0.15,
        })

        return factors

    # ═════════════════════════════════════════════════════
    #  EXIT LOGIC
    # ═════════════════════════════════════════════════════

    def should_exit(
        self, market: MarketState, position: Dict
    ) -> Optional[Dict]:
        """
        Evaluate exit conditions based on multiple signals.

        Note: SL/TP hits are handled by controller, not here.
        This handles strategy-based exits (reversals, etc.)
        """
        entry_price = position.get(
            "entry_price", position.get("avg_price", market.price)
        )
        quantity = position.get("quantity", 0)
        action = position.get("action", "BUY").upper()
        is_long = action == "BUY"

        # Calculate current PnL
        if is_long:
            pnl_pct = ((market.price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - market.price) / entry_price) * 100

        # ── Collect exit factors ──────────────────────────────────
        factors = self._collect_exit_factors(market, is_long, pnl_pct)

        if not factors:
            return None

        # ── Calculate exit confidence ─────────────────────────────
        confidence = self.weighted_score(factors)

        if confidence < 0.45:
            return None

        # ── Build reason string ───────────────────────────────────
        reason_parts = [f["name"] for f in factors if f["score"] > 0.5]
        reason = " + ".join(reason_parts) if reason_parts else "Multiple signals"

        logger.info(
            f"🔴 EXIT signal | {self.symbol} @ ${market.price:.2f} | "
            f"Conf={confidence:.2f} | Reason={reason} | PnL={pnl_pct:+.2f}%"
        )

        return self.build_exit_signal(
            market=market,
            position=position,
            confidence=confidence,
            reason=reason,
            metadata={
                "pnl_pct": round(pnl_pct, 3),
                "rsi": market.rsi,
                "trend": market.trend,
                "regime": market.regime,
                "sentiment": getattr(market, "sentiment_score", 0),
                "is_long": is_long,
                "factors": [f["name"] for f in factors],
            },
        )

    def _collect_exit_factors(
        self,
        market: MarketState,
        is_long: bool,
        pnl_pct: float,
    ) -> List[Dict]:
        """Collect all exit signal factors."""
        factors = []

        # ── Signal 1: Trend reversal ──────────────────────────────
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

        # ── Signal 2: RSI extreme ─────────────────────────────────
        rsi = market.rsi
        if rsi is not None:
            if is_long and rsi > 75:
                rsi_exit = min(1.0, (rsi - 75) / 25)
                factors.append({
                    "name": "rsi_overbought",
                    "score": round(rsi_exit, 3),
                    "weight": 0.20,
                })
            elif not is_long and rsi < 25:
                rsi_exit = min(1.0, (25 - rsi) / 25)
                factors.append({
                    "name": "rsi_oversold",
                    "score": round(rsi_exit, 3),
                    "weight": 0.20,
                })

        # ── Signal 3: MACD histogram flip ─────────────────────────
        macd_hist = market.macd_histogram
        if macd_hist is not None:
            if is_long and macd_hist < 0:
                factors.append({
                    "name": "macd_bearish",
                    "score": 0.75,
                    "weight": 0.20,
                })
            elif not is_long and macd_hist > 0:
                factors.append({
                    "name": "macd_bullish",
                    "score": 0.75,
                    "weight": 0.20,
                })

        # ── Signal 4: Momentum deceleration ───────────────────────
        mom_accel = market.momentum_acceleration
        if is_long and mom_accel < -0.0001:
            factors.append({
                "name": "momentum_decelerating",
                "score": 0.65,
                "weight": 0.15,
            })
        elif not is_long and mom_accel > 0.0001:
            factors.append({
                "name": "momentum_recovering",
                "score": 0.65,
                "weight": 0.15,
            })

        # ── Signal 5: Sentiment flip ──────────────────────────────
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

        # ── Signal 6: Chart pattern counter-signal ────────────────
        chart = getattr(market, "chart_pattern", None)
        if chart:
            pattern_signal = chart.get("signal", "HOLD").upper()
            pattern_conf = chart.get("confidence", 0) / 100

            if is_long and pattern_signal == "SELL":
                factors.append({
                    "name": "chart_bearish",
                    "score": pattern_conf,
                    "weight": 0.05,
                })
            elif not is_long and pattern_signal == "BUY":
                factors.append({
                    "name": "chart_bullish",
                    "score": pattern_conf,
                    "weight": 0.05,
                })

        # ── Signal 7: Profit protection (optional) ────────────────
        # If in significant profit and momentum weakening, consider exit
        if pnl_pct > 2.0 and market.momentum_strength < 0.001:
            factors.append({
                "name": "profit_protection",
                "score": 0.60,
                "weight": 0.10,
            })

        return factors

    # ═════════════════════════════════════════════════════
    #  HELPER METHODS
    # ═════════════════════════════════════════════════════

    def _validate_market_data(self, market: MarketState) -> bool:
        """Validate that required market data is present."""
        required = [market.ema_20, market.ema_50, market.rsi, market.atr]
        if any(x is None for x in required):
            logger.debug("⚠️ Missing required market data — entry skipped")
            return False
        if market.atr == 0:
            logger.debug("⚠️ ATR is zero — entry skipped")
            return False
        return True

    def _check_volatility_band(
        self, market: MarketState
    ) -> tuple[bool, str]:
        """
        Check if volatility is within acceptable band.

        Returns:
            (ok: bool, reason: str)
        """
        if market.price == 0:
            return False, "Price is zero"

        vol_pct = market.atr / market.price

        if vol_pct < self.min_volatility_pct:
            return False, f"Volatility {vol_pct:.4f} < min {self.min_volatility_pct}"

        if vol_pct > self.max_volatility_pct:
            return False, f"Volatility {vol_pct:.4f} > max {self.max_volatility_pct}"

        return True, "OK"

    def _adaptive_atr_multiplier(self, market: MarketState) -> float:
        """
        Adjust ATR multiplier based on volatility regime.

        Tighter stops in high volatility (avoid noise).
        Wider stops in low volatility (avoid premature exit).
        """
        base = self.atr_multiplier
        regime = market.volatility_regime

        adjustments = {
            "extreme": 0.80,
            "high": 0.90,
            "normal": 1.00,
            "low": 1.20,
        }

        mult = adjustments.get(regime, 1.0)
        return round(base * mult, 3)

    def _build_entry_metadata(
        self,
        market: MarketState,
        adaptive_mult: float,
        setup: str,
    ) -> Dict:
        """Build metadata dict for entry signal."""
        return {
            "setup": setup,
            "trend": market.trend,
            "regime": market.regime,
            "rsi": market.rsi,
            "atr": market.atr,
            "ema_20": market.ema_20,
            "ema_50": market.ema_50,
            "volatility_regime": market.volatility_regime,
            "sentiment_score": getattr(market, "sentiment_score", 0),
            "chart_pattern": (
                getattr(market, "chart_pattern", {}).get("pattern_name", "None")
                if market.chart_pattern else "None"
            ),
            "adaptive_atr_mult": adaptive_mult,
            "base_rr_ratio": self.rr_ratio,
            "strategy_version": "2.0",
        }

    # ═════════════════════════════════════════════════════
    #  CONFIGURATION
    # ═════════════════════════════════════════════════════

    def get_config(self) -> Dict:
        """Get full strategy configuration."""
        base_config = super().get_config()
        base_config.update({
            "rr_ratio": self.rr_ratio,
            "atr_multiplier": self.atr_multiplier,
            "min_volatility_pct": self.min_volatility_pct,
            "max_volatility_pct": self.max_volatility_pct,
            "allowed_regimes": self.allowed_regimes,
        })
        return base_config

    def __repr__(self) -> str:
        return (
            f"<ScalpingStrategy '{self.name}' | "
            f"Symbol={self.symbol} | "
            f"RR={self.rr_ratio} | "
            f"ATR×{self.atr_multiplier}>"
        )