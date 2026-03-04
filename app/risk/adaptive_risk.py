# app/risk/adaptive_risk.py

"""
Adaptive Risk Manager — Production Grade for Autonomous Trading

Institutional-grade position sizing with:
1. Base risk allocation
2. Non-linear streak scaling (win/loss)
3. Equity curve momentum awareness
4. Drawdown circuit breaker (tiered daily + total)
5. Volatility regime adjustment
6. Confidence weighting (from 4-brain signals)
7. Kelly criterion soft cap (properly calculated)
8. Signal quality adjustment
9. Time-of-day risk adjustment
10. Correlation-aware sizing
11. Smooth transitions (anti-shock damping)
12. Hard clamp to [min_risk, max_risk]

Integration:
    # Before position sizing:
    risk_pct = adaptive_risk.get_risk_percent(market_state, confidence, signal_quality)
    
    # After trade closes:
    adaptive_risk.update_after_trade(trade_result)
"""

from typing import Dict, Optional, List, Tuple
from math import exp, sqrt
from datetime import datetime
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  RISK LEVELS
# ═══════════════════════════════════════════════════════════════════

class RiskLevel:
    """Risk level classifications."""
    MINIMAL = "minimal"      # Near circuit breaker
    REDUCED = "reduced"      # Drawdown protection active
    NORMAL = "normal"        # Standard operation
    ELEVATED = "elevated"    # Win streak / high confidence
    MAXIMUM = "maximum"      # Optimal conditions


# ═══════════════════════════════════════════════════════════════════
#  ADAPTIVE RISK MANAGER
# ═══════════════════════════════════════════════════════════════════

class AdaptiveRiskManager:
    """
    Institutional Capital Allocation Engine

    Position sizing pipeline (applied in order):
    1. Start with base_risk
    2. Apply streak scaling (win boost / loss cut)
    3. Apply equity curve momentum
    4. Apply drawdown protection (tiered circuit breaker)
    5. Apply volatility regime adjustment
    6. Apply confidence weighting
    7. Apply signal quality adjustment
    8. Apply time-based adjustment (optional)
    9. Apply correlation adjustment (optional)
    10. Apply Kelly criterion cap
    11. Smooth transition from last risk
    12. Clamp to [min_risk, max_risk]

    All calculations are transparent and logged for audit.
    """

    # ── Volatility regime multipliers ─────────────────────────────
    VOLATILITY_REGIME_MULT: Dict[str, float] = {
        "low": 1.20,
        "normal": 1.00,
        "high": 0.70,
        "extreme": 0.40,
    }

    # ── Drawdown tiers (daily) ────────────────────────────────────
    DAILY_DRAWDOWN_TIERS: List[Tuple[float, float]] = [
        (0.05, 0.10),   # ≥5% DD → 10% of calculated risk
        (0.04, 0.35),   # ≥4% DD → 35% of calculated risk
        (0.03, 0.55),   # ≥3% DD → 55% of calculated risk
        (0.02, 0.75),   # ≥2% DD → 75% of calculated risk
        (0.01, 0.90),   # ≥1% DD → 90% of calculated risk
    ]

    # ── Total drawdown tiers (from initial balance) ───────────────
    TOTAL_DRAWDOWN_TIERS: List[Tuple[float, float]] = [
        (0.20, 0.05),   # ≥20% total DD → 5% of calculated
        (0.15, 0.20),   # ≥15% total DD → 20% of calculated
        (0.10, 0.50),   # ≥10% total DD → 50% of calculated
        (0.05, 0.80),   # ≥5% total DD → 80% of calculated
    ]

    # ── Signal quality multipliers ────────────────────────────────
    SIGNAL_QUALITY_MULT: Dict[str, float] = {
        "excellent": 1.25,
        "good": 1.10,
        "moderate": 1.00,
        "weak": 0.70,
    }

    # ── Trading session multipliers (UTC hours) ───────────────────
    SESSION_MULT: Dict[str, Tuple[int, int, float]] = {
        "asia": (0, 8, 0.90),       # Lower liquidity
        "europe": (8, 14, 1.05),    # Good liquidity
        "us": (14, 21, 1.10),       # Best liquidity
        "overnight": (21, 24, 0.85), # Lower liquidity
    }

    def __init__(
        self,
        state_manager,
        base_risk: float = 0.01,
        min_risk: float = 0.003,
        max_risk: float = 0.03,
        max_daily_dd: float = 0.05,
        max_total_dd: float = 0.20,
        kelly_fraction: float = 0.25,
        smooth_factor: float = 0.35,
        min_trades_for_kelly: int = 15,
        enable_session_adjustment: bool = False,
        enable_correlation_adjustment: bool = False,
    ):
        """
        Initialize Adaptive Risk Manager.

        Args:
            state_manager: State manager instance
            base_risk: Base risk per trade (default 1%)
            min_risk: Minimum risk floor (default 0.3%)
            max_risk: Maximum risk ceiling (default 3%)
            max_daily_dd: Max daily drawdown before shutdown (default 5%)
            max_total_dd: Max total drawdown from initial (default 20%)
            kelly_fraction: Fraction of Kelly to use (default 25%)
            smooth_factor: Max change per trade (default 35%)
            min_trades_for_kelly: Min trades before Kelly applies
            enable_session_adjustment: Adjust for trading sessions
            enable_correlation_adjustment: Adjust for correlated positions
        """
        self.state = state_manager

        # Core parameters
        self.base_risk = base_risk
        self.min_risk = min_risk
        self.max_risk = max_risk
        self.max_daily_dd = max_daily_dd
        self.max_total_dd = max_total_dd
        self.kelly_fraction = kelly_fraction
        self.smooth_factor = smooth_factor
        self.min_trades_for_kelly = min_trades_for_kelly

        # Optional adjustments
        self.enable_session_adjustment = enable_session_adjustment
        self.enable_correlation_adjustment = enable_correlation_adjustment

        # Tracking
        self._adjustment_history: List[Dict] = []
        self._risk_history: List[float] = []

        logger.info(
            f"🎯 AdaptiveRiskManager initialized | "
            f"Base={base_risk*100:.1f}% | "
            f"Range=[{min_risk*100:.1f}%, {max_risk*100:.1f}%]"
        )

    # ═══════════════════════════════════════════════════════════════
    #  PRIMARY API
    # ═══════════════════════════════════════════════════════════════

    def get_risk_percent(
        self,
        market_state=None,
        confidence: Optional[float] = None,
        signal_quality: Optional[str] = None,
        num_open_positions: int = 0,
    ) -> float:
        """
        Calculate adaptive risk percentage for current trade.

        Args:
            market_state: MarketState object (for volatility regime)
            confidence: 0-1 confidence from strategy
            signal_quality: "excellent", "good", "moderate", "weak"
            num_open_positions: Number of currently open positions

        Returns:
            Risk fraction (e.g., 0.012 = 1.2% of balance)
        """
        risk = self.base_risk
        adjustments = []
        multipliers = []

        # ── Pre-check: Circuit breaker ────────────────────────────
        if self._is_circuit_breaker_active():
            logger.warning("🛑 Circuit breaker active — minimal risk")
            return self.min_risk

        # ── Step 1: Streak scaling ────────────────────────────────
        risk, streak_mult = self._apply_streak_scaling(risk)
        if streak_mult != 1.0:
            adjustments.append(f"Streak×{streak_mult:.2f}")
            multipliers.append(streak_mult)

        # ── Step 2: Equity curve momentum ─────────────────────────
        risk, equity_mult = self._apply_equity_curve_scaling(risk)
        if equity_mult != 1.0:
            adjustments.append(f"Equity×{equity_mult:.2f}")
            multipliers.append(equity_mult)

        # ── Step 3: Daily drawdown protection ─────────────────────
        risk, dd_mult, dd_pct = self._apply_daily_drawdown_protection(risk)
        if dd_mult != 1.0:
            adjustments.append(f"DailyDD({dd_pct:.1f}%)×{dd_mult:.2f}")
            multipliers.append(dd_mult)

        # ── Step 4: Total drawdown protection ─────────────────────
        risk, total_dd_mult, total_dd_pct = self._apply_total_drawdown_protection(risk)
        if total_dd_mult != 1.0:
            adjustments.append(f"TotalDD({total_dd_pct:.1f}%)×{total_dd_mult:.2f}")
            multipliers.append(total_dd_mult)

        # ── Step 5: Volatility regime ─────────────────────────────
        if market_state is not None:
            risk, vol_mult, regime = self._apply_volatility_regime(risk, market_state)
            if vol_mult != 1.0:
                adjustments.append(f"Vol({regime})×{vol_mult:.2f}")
                multipliers.append(vol_mult)

        # ── Step 6: Confidence weighting ──────────────────────────
        if confidence is not None:
            risk, conf_mult = self._apply_confidence_weight(risk, confidence)
            adjustments.append(f"Conf({confidence:.0%})×{conf_mult:.2f}")
            multipliers.append(conf_mult)

        # ── Step 7: Signal quality ────────────────────────────────
        if signal_quality is not None:
            risk, quality_mult = self._apply_signal_quality(risk, signal_quality)
            if quality_mult != 1.0:
                adjustments.append(f"Quality({signal_quality})×{quality_mult:.2f}")
                multipliers.append(quality_mult)

        # ── Step 8: Session adjustment (optional) ─────────────────
        if self.enable_session_adjustment:
            risk, session_mult, session = self._apply_session_adjustment(risk)
            if session_mult != 1.0:
                adjustments.append(f"Session({session})×{session_mult:.2f}")
                multipliers.append(session_mult)

        # ── Step 9: Correlation adjustment (optional) ─────────────
        if self.enable_correlation_adjustment and num_open_positions > 0:
            risk, corr_mult = self._apply_correlation_adjustment(risk, num_open_positions)
            if corr_mult != 1.0:
                adjustments.append(f"Positions({num_open_positions})×{corr_mult:.2f}")
                multipliers.append(corr_mult)

        # ── Step 10: Kelly cap ────────────────────────────────────
        risk, kelly_applied = self._apply_kelly_cap(risk)
        if kelly_applied:
            adjustments.append("Kelly-capped")

        # ── Step 11: Smooth transition ────────────────────────────
        risk, smoothed = self._smooth_risk_transition(risk)
        if smoothed:
            adjustments.append("Smoothed")

        # ── Step 12: Hard clamp ───────────────────────────────────
        final_risk = max(self.min_risk, min(self.max_risk, risk))

        # ── Determine risk level ──────────────────────────────────
        risk_level = self._classify_risk_level(final_risk, multipliers)

        # ── Logging ───────────────────────────────────────────────
        daily_dd = self._current_daily_drawdown_pct()
        total_dd = self._current_total_drawdown_pct()
        adj_str = " | ".join(adjustments) if adjustments else "No adjustments"

        logger.info(
            f"🎯 Risk: {final_risk*100:.3f}% [{risk_level}] | "
            f"Base: {self.base_risk*100:.1f}% | "
            f"DD: {daily_dd:.1f}%/{total_dd:.1f}% | {adj_str}"
        )

        # ── Persist and track ─────────────────────────────────────
        self.state.set("last_risk", final_risk)
        self.state.set("risk_level", risk_level)
        self._record_risk(final_risk, adjustments)

        return round(final_risk, 6)

    def get_risk(
        self,
        market_state=None,
        confidence: Optional[float] = None,
        signal_quality: Optional[str] = None,
    ) -> float:
        """Alias for get_risk_percent() — backwards compatibility."""
        return self.get_risk_percent(market_state, confidence, signal_quality)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 1: STREAK SCALING
    # ═══════════════════════════════════════════════════════════════

    def _apply_streak_scaling(self, risk: float) -> Tuple[float, float]:
        """
        Non-linear streak scaling using exponential saturation.

        Win streak:  Increases risk with diminishing returns (cap +30%)
        Loss streak: Decreases risk aggressively (cap -60%)
        """
        win_streak = self._safe_int("win_streak")
        loss_streak = self._safe_int("loss_streak")

        multiplier = 1.0

        if win_streak > 0:
            # Exponential saturation: approaches max boost asymptotically
            boost = (1 - exp(-win_streak / 4)) * 0.30
            multiplier = 1 + boost

        elif loss_streak > 0:
            # Aggressive reduction on losses
            cut = (1 - exp(-loss_streak / 2)) * 0.60
            multiplier = 1 - cut

            # Extra penalty for consecutive losses
            if loss_streak >= 3:
                multiplier *= 0.90

        return risk * multiplier, round(multiplier, 3)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 2: EQUITY CURVE MOMENTUM
    # ═══════════════════════════════════════════════════════════════

    def _apply_equity_curve_scaling(self, risk: float) -> Tuple[float, float]:
        """
        Scale risk based on recent equity performance.

        Positive momentum → scale up (max +25%)
        Negative momentum → scale down (max -45%)
        """
        momentum = self._safe_float("equity_momentum")

        if momentum is None or momentum == 0:
            return risk, 1.0

        if momentum > 0:
            # Cap upside at 25%
            adj = min(momentum * 2.5, 0.25)
            multiplier = 1 + adj
        else:
            # More aggressive downside
            adj = max(momentum * 3, -0.45)
            multiplier = 1 + adj

        return risk * multiplier, round(multiplier, 3)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 3: DAILY DRAWDOWN PROTECTION
    # ═══════════════════════════════════════════════════════════════

    def _apply_daily_drawdown_protection(self, risk: float) -> Tuple[float, float, float]:
        """
        Tiered daily drawdown circuit breaker.

        Uses start-of-day balance for DAILY protection.
        """
        dd_pct = self._current_daily_drawdown()

        if dd_pct <= 0:
            return risk, 1.0, 0.0

        # Check tiers
        for threshold, multiplier in self.DAILY_DRAWDOWN_TIERS:
            if dd_pct >= threshold:
                if multiplier <= 0.20:
                    logger.warning(
                        f"🛑 Daily DD circuit breaker | "
                        f"DD={dd_pct*100:.1f}% ≥ {threshold*100}%"
                    )
                return risk * multiplier, multiplier, dd_pct * 100

        return risk, 1.0, dd_pct * 100

    # ═══════════════════════════════════════════════════════════════
    #  STEP 4: TOTAL DRAWDOWN PROTECTION
    # ═══════════════════════════════════════════════════════════════

    def _apply_total_drawdown_protection(self, risk: float) -> Tuple[float, float, float]:
        """
        Total drawdown protection from initial balance.

        More severe than daily — protects against account destruction.
        """
        dd_pct = self._current_total_drawdown()

        if dd_pct <= 0:
            return risk, 1.0, 0.0

        # Check tiers
        for threshold, multiplier in self.TOTAL_DRAWDOWN_TIERS:
            if dd_pct >= threshold:
                logger.warning(
                    f"⚠️ Total DD protection | "
                    f"DD={dd_pct*100:.1f}% ≥ {threshold*100}%"
                )
                return risk * multiplier, multiplier, dd_pct * 100

        return risk, 1.0, dd_pct * 100

    # ═══════════════════════════════════════════════════════════════
    #  STEP 5: VOLATILITY REGIME
    # ═══════════════════════════════════════════════════════════════

    def _apply_volatility_regime(
        self, risk: float, market_state
    ) -> Tuple[float, float, str]:
        """Adjust risk based on market volatility regime."""
        regime = getattr(market_state, "volatility_regime", None)

        if regime and regime in self.VOLATILITY_REGIME_MULT:
            mult = self.VOLATILITY_REGIME_MULT[regime]
            return risk * mult, mult, regime

        # Fallback to numeric volatility
        volatility = getattr(market_state, "volatility", None)

        if volatility is None:
            return risk, 1.0, "unknown"

        if volatility > 0.06:
            return risk * 0.40, 0.40, "extreme"
        elif volatility > 0.04:
            return risk * 0.70, 0.70, "high"
        elif volatility > 0.02:
            return risk, 1.0, "normal"
        else:
            return risk * 1.20, 1.20, "low"

    # ═══════════════════════════════════════════════════════════════
    #  STEP 6: CONFIDENCE WEIGHTING
    # ═══════════════════════════════════════════════════════════════

    def _apply_confidence_weight(
        self, risk: float, confidence: float
    ) -> Tuple[float, float]:
        """
        Scale risk based on signal confidence.

        Maps confidence [0, 1] → multiplier [0.50, 1.35]
        """
        confidence = max(0.0, min(1.0, confidence))

        # Non-linear mapping: penalize low confidence more
        if confidence < 0.5:
            multiplier = 0.50 + (confidence * 0.60)
        else:
            multiplier = 0.80 + ((confidence - 0.5) * 1.10)

        return risk * multiplier, round(multiplier, 3)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 7: SIGNAL QUALITY
    # ═══════════════════════════════════════════════════════════════

    def _apply_signal_quality(
        self, risk: float, quality: str
    ) -> Tuple[float, float]:
        """Adjust risk based on signal quality classification."""
        mult = self.SIGNAL_QUALITY_MULT.get(quality.lower(), 1.0)
        return risk * mult, mult

    # ═══════════════════════════════════════════════════════════════
    #  STEP 8: SESSION ADJUSTMENT
    # ═══════════════════════════════════════════════════════════════

    def _apply_session_adjustment(self, risk: float) -> Tuple[float, float, str]:
        """Adjust risk based on trading session (liquidity)."""
        current_hour = datetime.utcnow().hour

        for session, (start, end, mult) in self.SESSION_MULT.items():
            if start <= current_hour < end:
                return risk * mult, mult, session

        return risk, 1.0, "unknown"

    # ═══════════════════════════════════════════════════════════════
    #  STEP 9: CORRELATION ADJUSTMENT
    # ═══════════════════════════════════════════════════════════════

    def _apply_correlation_adjustment(
        self, risk: float, num_positions: int
    ) -> Tuple[float, float]:
        """
        Reduce risk when holding multiple positions.

        Assumes some correlation between crypto assets.
        """
        if num_positions == 0:
            return risk, 1.0

        # Reduce by 15% per additional position, min 40%
        multiplier = max(0.40, 1.0 - (num_positions * 0.15))
        return risk * multiplier, round(multiplier, 3)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 10: KELLY CRITERION CAP
    # ═══════════════════════════════════════════════════════════════

    def _apply_kelly_cap(self, risk: float) -> Tuple[float, bool]:
        """
        Apply fractional Kelly criterion as a soft cap.

        Kelly formula: f = W - (1-W)/R
        """
        total_trades = self._safe_int("total_wins") + self._safe_int("total_losses")

        if total_trades < self.min_trades_for_kelly:
            return risk, False

        win_rate = self._get_win_rate()
        avg_win, avg_loss = self._get_avg_win_loss()

        if avg_loss == 0 or win_rate <= 0:
            return risk, False

        r_ratio = avg_win / avg_loss
        kelly = win_rate - (1 - win_rate) / r_ratio

        if kelly <= 0:
            logger.warning(f"📐 Negative Kelly ({kelly:.3f}) — no edge")
            return self.min_risk, True

        kelly_cap = kelly * self.kelly_fraction

        if risk > kelly_cap:
            logger.debug(
                f"📐 Kelly cap: {risk*100:.3f}% → {kelly_cap*100:.3f}% | "
                f"WR={win_rate*100:.1f}% R={r_ratio:.2f}"
            )
            return kelly_cap, True

        return risk, False

    # ═══════════════════════════════════════════════════════════════
    #  STEP 11: SMOOTH TRANSITION
    # ═══════════════════════════════════════════════════════════════

    def _smooth_risk_transition(self, new_risk: float) -> Tuple[float, bool]:
        """Prevent jarring risk changes between trades."""
        last_risk = self._safe_float("last_risk")

        if last_risk is None or last_risk == 0:
            return new_risk, False

        max_change = last_risk * self.smooth_factor
        delta = new_risk - last_risk

        if abs(delta) <= max_change:
            return new_risk, False

        if delta > 0:
            smoothed = last_risk + max_change
        else:
            smoothed = last_risk - max_change

        return smoothed, True

    # ═══════════════════════════════════════════════════════════════
    #  CIRCUIT BREAKER
    # ═══════════════════════════════════════════════════════════════

    def _is_circuit_breaker_active(self) -> bool:
        """Check if circuit breaker should halt trading."""
        # Kill switch
        if self.state.get("kill_switch"):
            return True

        # Bot deactivated
        if not self.state.get("bot_active", True):
            return True

        # Max daily drawdown exceeded
        if self._current_daily_drawdown() >= self.max_daily_dd:
            return True

        # Max total drawdown exceeded
        if self._current_total_drawdown() >= self.max_total_dd:
            return True

        # Max consecutive losses
        max_losses = self.state.get("max_consecutive_losses", 5)
        if self._safe_int("consecutive_losses") >= max_losses:
            return True

        return False

    def _classify_risk_level(
        self, risk: float, multipliers: List[float]
    ) -> str:
        """Classify current risk level."""
        # Check if heavily reduced
        combined_mult = 1.0
        for m in multipliers:
            combined_mult *= m

        if risk <= self.min_risk * 1.5:
            return RiskLevel.MINIMAL
        elif combined_mult < 0.6:
            return RiskLevel.REDUCED
        elif combined_mult > 1.2:
            return RiskLevel.ELEVATED
        elif combined_mult > 1.4 and risk >= self.max_risk * 0.8:
            return RiskLevel.MAXIMUM
        else:
            return RiskLevel.NORMAL

    # ═══════════════════════════════════════════════════════════════
    #  TRADE RESULT UPDATE
    # ═══════════════════════════════════════════════════════════════

    def update_after_trade(self, trade: Dict) -> Dict:
        """
        Update statistics after a trade closes.

        Args:
            trade: Dict with pnl_amount or net_pnl

        Returns:
            Summary of updated statistics
        """
        pnl = trade.get("pnl_amount", trade.get("net_pnl", 0))
        is_win = pnl > 0

        win_streak = self._safe_int("win_streak")
        loss_streak = self._safe_int("loss_streak")

        if is_win:
            win_streak += 1
            loss_streak = 0
            self.state.increment("total_wins", 1)
            self._record_win_amount(abs(pnl))
        else:
            loss_streak += 1
            win_streak = 0
            self.state.increment("total_losses", 1)
            self._record_loss_amount(abs(pnl))

        self.state.set("win_streak", win_streak)
        self.state.set("loss_streak", loss_streak)
        self.state.set("consecutive_losses", loss_streak if not is_win else 0)

        # Update equity tracking
        self._update_equity_history()
        self._update_equity_momentum()

        # Calculate Sharpe ratio contribution
        self._update_returns_history(pnl)

        summary = {
            "pnl": pnl,
            "is_win": is_win,
            "win_streak": win_streak,
            "loss_streak": loss_streak,
            "win_rate": self._get_win_rate(),
            "total_trades": self._safe_int("total_wins") + self._safe_int("total_losses"),
            "risk_level": self.state.get("risk_level", RiskLevel.NORMAL),
        }

        logger.info(
            f"📊 Risk updated | PnL=${pnl:+.4f} | "
            f"W{win_streak}/L{loss_streak} | "
            f"WR={summary['win_rate']*100:.1f}%"
        )

        return summary

    # ═══════════════════════════════════════════════════════════════
    #  POSITION SIZING HELPERS
    # ═══════════════════════════════════════════════════════════════

    def calculate_position_size(
        self,
        balance: float,
        entry_price: float,
        stop_loss: float,
        risk_percent: Optional[float] = None,
        market_state=None,
        confidence: float = 0.6,
    ) -> Dict:
        """
        Calculate complete position sizing.

        Returns:
            Dict with quantity, risk_amount, risk_percent, etc.
        """
        # Get risk percent if not provided
        if risk_percent is None:
            risk_percent = self.get_risk_percent(market_state, confidence)

        risk_amount = balance * risk_percent
        risk_per_unit = abs(entry_price - stop_loss)

        if risk_per_unit == 0:
            return {
                "quantity": 0,
                "risk_amount": 0,
                "risk_percent": risk_percent,
                "position_value": 0,
                "error": "Stop loss equals entry price",
            }

        quantity = risk_amount / risk_per_unit
        position_value = quantity * entry_price

        # Check if position value exceeds balance
        max_position = balance * 0.95  # Leave 5% margin
        if position_value > max_position:
            quantity = max_position / entry_price
            position_value = max_position
            actual_risk = (quantity * risk_per_unit) / balance

            logger.warning(
                f"⚠️ Position capped: ${position_value:.2f} "
                f"(actual risk: {actual_risk*100:.2f}%)"
            )

        return {
            "quantity": round(quantity, 8),
            "risk_amount": round(risk_amount, 4),
            "risk_percent": round(risk_percent * 100, 3),
            "position_value": round(position_value, 2),
            "risk_per_unit": round(risk_per_unit, 8),
            "balance": balance,
            "leverage": round(position_value / balance, 2) if balance > 0 else 0,
        }

    def get_max_position_value(self, balance: float) -> float:
        """Get maximum allowed position value."""
        max_exposure = self.state.get("max_exposure_pct", 0.30)
        return balance * max_exposure

    # ═══════════════════════════════════════════════════════════════
    #  WIN/LOSS TRACKING
    # ═══════════════════════════════════════════════════════════════

    def _record_win_amount(self, amount: float) -> None:
        """Record a winning trade amount."""
        wins: List[float] = self.state.get("win_amounts") or []
        wins.append(amount)
        if len(wins) > 100:
            wins = wins[-100:]
        self.state.set("win_amounts", wins)

    def _record_loss_amount(self, amount: float) -> None:
        """Record a losing trade amount."""
        losses: List[float] = self.state.get("loss_amounts") or []
        losses.append(amount)
        if len(losses) > 100:
            losses = losses[-100:]
        self.state.set("loss_amounts", losses)

    def _get_avg_win_loss(self) -> Tuple[float, float]:
        """Get average win and average loss amounts."""
        wins: List[float] = self.state.get("win_amounts") or []
        losses: List[float] = self.state.get("loss_amounts") or []

        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        return avg_win, avg_loss

    # ═══════════════════════════════════════════════════════════════
    #  EQUITY TRACKING
    # ═══════════════════════════════════════════════════════════════

    def _update_equity_history(self) -> None:
        """Update equity history."""
        balance = self._safe_float("balance") or 0
        history: List[float] = self.state.get("equity_history") or []

        history.append(balance)
        if len(history) > 100:
            history = history[-100:]

        self.state.set("equity_history", history)

    def _update_equity_momentum(self) -> None:
        """Calculate equity curve momentum."""
        history: List[float] = self.state.get("equity_history") or []

        if len(history) < 5:
            return

        recent = history[-10:]
        if recent[0] == 0:
            return

        # Linear regression slope
        n = len(recent)
        x_mean = (n - 1) / 2
        y_mean = sum(recent) / n

        numerator = sum((i - x_mean) * (recent[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return

        slope = numerator / denominator
        normalized_slope = slope / y_mean if y_mean != 0 else 0

        self.state.set("equity_momentum", round(normalized_slope, 6))

    def _update_returns_history(self, pnl: float) -> None:
        """Track returns for Sharpe ratio calculation."""
        balance = self._safe_float("balance") or 1
        ret = pnl / balance

        returns: List[float] = self.state.get("returns_history") or []
        returns.append(ret)
        if len(returns) > 100:
            returns = returns[-100:]

        self.state.set("returns_history", returns)

    def _record_risk(self, risk: float, adjustments: List[str]) -> None:
        """Record risk for history tracking."""
        self._risk_history.append(risk)
        if len(self._risk_history) > 50:
            self._risk_history = self._risk_history[-50:]

        self._adjustment_history.append({
            "timestamp": datetime.utcnow().isoformat(),
            "risk": risk,
            "adjustments": adjustments,
        })
        if len(self._adjustment_history) > 20:
            self._adjustment_history = self._adjustment_history[-20:]

    # ═══════════════════════════════════════════════════════════════
    #  STAT HELPERS
    # ═══════════════════════════════════════════════════════════════

    def _get_win_rate(self) -> float:
        """Calculate win rate."""
        wins = self._safe_int("total_wins")
        losses = self._safe_int("total_losses")
        total = wins + losses
        return wins / total if total > 0 else 0.5

    def _current_daily_drawdown(self) -> float:
        """Current daily drawdown from start-of-day balance."""
        start = self._safe_float("start_of_day_balance")
        current = self._safe_float("balance")

        if not start or start <= 0:
            return 0.0

        dd = (start - (current or 0)) / start
        return max(0.0, dd)

    def _current_daily_drawdown_pct(self) -> float:
        """Current daily drawdown as percentage."""
        return round(self._current_daily_drawdown() * 100, 2)

    def _current_total_drawdown(self) -> float:
        """Current total drawdown from initial balance."""
        initial = self._safe_float("initial_balance")
        current = self._safe_float("balance")

        if not initial or initial <= 0:
            return 0.0

        dd = (initial - (current or 0)) / initial
        return max(0.0, dd)

    def _current_total_drawdown_pct(self) -> float:
        """Current total drawdown as percentage."""
        return round(self._current_total_drawdown() * 100, 2)

    def _safe_int(self, key: str, default: int = 0) -> int:
        """Safely get an integer from state."""
        value = self.state.get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _safe_float(self, key: str, default: float = None) -> Optional[float]:
        """Safely get a float from state."""
        value = self.state.get(key)
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    # ═══════════════════════════════════════════════════════════════
    #  ANALYTICS
    # ═══════════════════════════════════════════════════════════════

    def get_sharpe_ratio(self, risk_free_rate: float = 0.0) -> float:
        """Calculate Sharpe ratio from returns history."""
        returns: List[float] = self.state.get("returns_history") or []

        if len(returns) < 10:
            return 0.0

        avg_return = sum(returns) / len(returns)
        
        if len(returns) < 2:
            return 0.0

        variance = sum((r - avg_return) ** 2 for r in returns) / (len(returns) - 1)
        std_dev = sqrt(variance) if variance > 0 else 0

        if std_dev == 0:
            return 0.0

        # Annualize (assuming ~3 trades per day)
        annualized_return = avg_return * 365 * 3
        annualized_std = std_dev * sqrt(365 * 3)

        sharpe = (annualized_return - risk_free_rate) / annualized_std
        return round(sharpe, 2)

    def get_sortino_ratio(self, risk_free_rate: float = 0.0) -> float:
        """Calculate Sortino ratio (downside risk only)."""
        returns: List[float] = self.state.get("returns_history") or []

        if len(returns) < 10:
            return 0.0

        avg_return = sum(returns) / len(returns)
        negative_returns = [r for r in returns if r < 0]

        if not negative_returns:
            return float('inf') if avg_return > 0 else 0.0

        downside_variance = sum(r ** 2 for r in negative_returns) / len(negative_returns)
        downside_std = sqrt(downside_variance)

        if downside_std == 0:
            return 0.0

        sortino = (avg_return - risk_free_rate) / downside_std
        return round(sortino, 2)

    def get_risk_report(self) -> Dict:
        """Generate comprehensive risk report."""
        last_risk = self._safe_float("last_risk") or self.base_risk
        win_rate = self._get_win_rate()
        avg_win, avg_loss = self._get_avg_win_loss()

        kelly_raw = 0.0
        kelly_capped = 0.0
        profit_factor = 0.0

        if avg_loss > 0:
            r_ratio = avg_win / avg_loss
            kelly_raw = win_rate - (1 - win_rate) / r_ratio
            kelly_capped = max(0, kelly_raw) * self.kelly_fraction
            profit_factor = r_ratio

        return {
            # Current state
            "current_risk_pct": round(last_risk * 100, 3),
            "risk_level": self.state.get("risk_level", RiskLevel.NORMAL),
            "base_risk_pct": round(self.base_risk * 100, 3),
            "min_risk_pct": round(self.min_risk * 100, 3),
            "max_risk_pct": round(self.max_risk * 100, 3),

            # Streaks
            "win_streak": self._safe_int("win_streak"),
            "loss_streak": self._safe_int("loss_streak"),

            # Performance
            "win_rate_pct": round(win_rate * 100, 1),
            "total_wins": self._safe_int("total_wins"),
            "total_losses": self._safe_int("total_losses"),
            "total_trades": self._safe_int("total_wins") + self._safe_int("total_losses"),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 2),

            # Kelly
            "kelly_raw_pct": round(kelly_raw * 100, 2),
            "kelly_capped_pct": round(kelly_capped * 100, 3),
            "kelly_fraction": self.kelly_fraction,

            # Drawdown
            "daily_drawdown_pct": self._current_daily_drawdown_pct(),
            "total_drawdown_pct": self._current_total_drawdown_pct(),
            "max_daily_dd_pct": round(self.max_daily_dd * 100, 1),
            "max_total_dd_pct": round(self.max_total_dd * 100, 1),

            # Momentum & ratios
            "equity_momentum": self._safe_float("equity_momentum") or 0.0,
            "sharpe_ratio": self.get_sharpe_ratio(),
            "sortino_ratio": self.get_sortino_ratio(),

            # Risk history
            "recent_risks": self._risk_history[-5:] if self._risk_history else [],
        }

    # ═══════════════════════════════════════════════════════════════
    #  MANUAL CONTROLS
    # ═══════════════════════════════════════════════════════════════

    def reset_streaks(self) -> None:
        """Reset win/loss streaks."""
        self.state.set("win_streak", 0)
        self.state.set("loss_streak", 0)
        self.state.set("consecutive_losses", 0)
        logger.info("🔄 Risk streaks reset")

    def reset_statistics(self) -> None:
        """Full reset of all risk statistics."""
        self.state.set("win_streak", 0)
        self.state.set("loss_streak", 0)
        self.state.set("consecutive_losses", 0)
        self.state.set("total_wins", 0)
        self.state.set("total_losses", 0)
        self.state.set("win_amounts", [])
        self.state.set("loss_amounts", [])
        self.state.set("equity_history", [])
        self.state.set("returns_history", [])
        self.state.set("equity_momentum", 0.0)
        self.state.set("last_risk", None)
        self.state.set("risk_level", RiskLevel.NORMAL)
        self._risk_history.clear()
        self._adjustment_history.clear()
        logger.warning("⚠️ Risk statistics fully reset")

    def reset_daily(self) -> None:
        """Reset daily tracking."""
        balance = self._safe_float("balance") or 0
        self.state.set("equity_momentum", 0.0)

        history: List[float] = self.state.get("equity_history") or []
        history = history[-10:] if history else [balance]
        self.state.set("equity_history", history)

        logger.info(f"🔄 Risk daily reset | Balance: ${balance:.2f}")

    def set_base_risk(self, new_base: float) -> None:
        """Update base risk."""
        old = self.base_risk
        self.base_risk = max(self.min_risk, min(self.max_risk, new_base))
        logger.info(f"⚙️ Base risk: {old*100:.2f}% → {self.base_risk*100:.2f}%")

    def set_risk_limits(self, min_risk: float = None, max_risk: float = None) -> None:
        """Update risk limits."""
        if min_risk is not None:
            self.min_risk = min_risk
        if max_risk is not None:
            self.max_risk = max_risk
        logger.info(f"⚙️ Risk limits: [{self.min_risk*100:.2f}%, {self.max_risk*100:.2f}%]")

    # ═══════════════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═══════════════════════════════════════════════════════════════

    def __repr__(self) -> str:
        last_risk = self._safe_float("last_risk") or self.base_risk
        win_rate = self._get_win_rate()
        level = self.state.get("risk_level", RiskLevel.NORMAL)
        return (
            f"<AdaptiveRiskManager | "
            f"Risk={last_risk*100:.2f}% [{level}] | "
            f"WR={win_rate*100:.1f}% | "
            f"DD={self._current_daily_drawdown_pct():.1f}%>"
        )