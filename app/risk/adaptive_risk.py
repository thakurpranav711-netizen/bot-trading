# app/risk/adaptive_risk.py

"""
Adaptive Risk Manager — Production Grade for Autonomous Trading

UPDATED: Realistic risk management with controlled exposure
FIXED: Works with or without state_manager (standalone mode supported)
FIXED: Robust type handling for equity_history to prevent TypeError
"""

from typing import Dict, Optional, List, Tuple, Any, Union
from math import exp, sqrt
from datetime import datetime, timezone
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

    UPDATED for realistic trading with controlled risk.
    FIXED: Works with or without state_manager.
    FIXED: Robust handling of equity_history data types.
    """

    # ── Volatility regime multipliers — LESS AGGRESSIVE ───────────
    VOLATILITY_REGIME_MULT: Dict[str, float] = {
        "low": 1.15,
        "normal": 1.00,
        "high": 0.80,
        "extreme": 0.55,
    }

    # ── Drawdown tiers (daily) — RELAXED ──────────────────────────
    DAILY_DRAWDOWN_TIERS: List[Tuple[float, float]] = [
        (0.06, 0.10),
        (0.05, 0.30),
        (0.04, 0.50),
        (0.03, 0.70),
        (0.02, 0.85),
    ]

    # ── Total drawdown tiers — RELAXED ────────────────────────────
    TOTAL_DRAWDOWN_TIERS: List[Tuple[float, float]] = [
        (0.25, 0.05),
        (0.20, 0.20),
        (0.15, 0.45),
        (0.10, 0.70),
    ]

    # ── Signal quality multipliers — BOOSTED ──────────────────────
    SIGNAL_QUALITY_MULT: Dict[str, float] = {
        "excellent": 1.30,
        "good": 1.15,
        "moderate": 1.00,
        "weak": 0.75,
    }

    # ── Trading session multipliers (UTC hours) ───────────────────
    SESSION_MULT: Dict[str, Tuple[int, int, float]] = {
        "asia": (0, 8, 0.95),
        "europe": (8, 14, 1.05),
        "us": (14, 21, 1.10),
        "overnight": (21, 24, 0.90),
    }

    def __init__(
        self,
        # Can be None for standalone mode
        state_manager=None,
        # Core risk parameters
        base_risk: float = 0.02,
        min_risk: float = 0.005,
        max_risk: float = 0.03,
        max_daily_dd: float = 0.05,
        max_total_dd: float = 0.20,
        kelly_fraction: float = 0.35,
        smooth_factor: float = 0.40,
        min_trades_for_kelly: int = 12,
        enable_session_adjustment: bool = False,
        enable_correlation_adjustment: bool = False,
        # Additional parameters for compatibility
        daily_loss_limit_inr: float = 1500.0,
        usd_to_inr: float = 83.0,
        **kwargs  # Accept any extra parameters
    ):
        """
        Initialize Adaptive Risk Manager.
        
        Supports both standalone mode (no state_manager) and
        full mode (with state_manager for persistent state).
        """
        # Optional state manager
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
        self.daily_loss_limit_inr = daily_loss_limit_inr
        self.usd_to_inr = usd_to_inr
        self.daily_loss_limit_usd = daily_loss_limit_inr / usd_to_inr

        # Optional adjustments
        self.enable_session_adjustment = enable_session_adjustment
        self.enable_correlation_adjustment = enable_correlation_adjustment

        # In-memory state (used when no state_manager)
        self._memory_state: Dict[str, Any] = {
            "win_streak": 0,
            "loss_streak": 0,
            "consecutive_losses": 0,
            "total_wins": 0,
            "total_losses": 0,
            "win_amounts": [],
            "loss_amounts": [],
            "equity_history": [],
            "returns_history": [],
            "equity_momentum": 0.0,
            "last_risk": None,
            "risk_level": RiskLevel.NORMAL,
            "balance": 0.0,
            "start_of_day_balance": 0.0,
            "initial_balance": 0.0,
            "peak_balance": 0.0,
            "daily_pnl": 0.0,
            "trades_today": 0,
            "daily_wins": 0,
            "daily_losses": 0,
            "kill_switch": False,
            "bot_active": True,
            "max_consecutive_losses": 7,
        }

        # Tracking
        self._adjustment_history: List[Dict] = []
        self._risk_history: List[float] = []
        self._current_risk: float = base_risk

        # Clean up any corrupted data on init
        if self.state:
            self._sanitize_state_data()

        logger.info(
            f"🎯 AdaptiveRiskManager initialized | "
            f"Base={base_risk*100:.1f}% | "
            f"Range=[{min_risk*100:.1f}%, {max_risk*100:.1f}%] | "
            f"Kelly={kelly_fraction*100:.0f}%"
        )

    # ═══════════════════════════════════════════════════════════════
    #  STATE ACCESS - HYBRID (state_manager or in-memory)
    # ═══════════════════════════════════════════════════════════════

    def _get(self, key: str, default=None):
        """Get value from state_manager or in-memory state."""
        if self.state:
            return self.state.get(key, default)
        return self._memory_state.get(key, default)

    def _set(self, key: str, value) -> None:
        """Set value in state_manager or in-memory state."""
        if self.state:
            self.state.set(key, value)
        else:
            self._memory_state[key] = value

    def _increment(self, key: str, amount: int = 1) -> None:
        """Increment a value."""
        current = self._safe_int(key)
        self._set(key, current + amount)

    # ═══════════════════════════════════════════════════════════════
    #  DATA SANITIZATION
    # ═══════════════════════════════════════════════════════════════

    def _sanitize_state_data(self) -> None:
        """Clean up any corrupted or mistyped data in state."""
        float_list_fields = [
            "equity_history",
            "returns_history",
            "win_amounts",
            "loss_amounts",
        ]

        for field in float_list_fields:
            data = self._get(field)
            if data is not None:
                cleaned = self._extract_float_list(data)
                if cleaned != data:
                    logger.warning(f"🔧 Sanitized {field}")
                    self._set(field, cleaned)

    def _extract_float_list(self, data: Any) -> List[float]:
        """Extract a list of floats from potentially corrupted data."""
        if data is None:
            return []

        if not isinstance(data, list):
            extracted = self._extract_float_value(data)
            return [extracted] if extracted is not None else []

        result = []
        for item in data:
            extracted = self._extract_float_value(item)
            if extracted is not None:
                result.append(extracted)

        return result

    def _extract_float_value(self, item: Any) -> Optional[float]:
        """Extract a float value from various data types."""
        if item is None:
            return None

        if isinstance(item, (int, float)):
            return float(item)

        if isinstance(item, dict):
            for key in ['equity', 'value', 'balance', 'amount', 'pnl', 'price']:
                if key in item:
                    try:
                        return float(item[key])
                    except (ValueError, TypeError):
                        continue
            return None

        if isinstance(item, str):
            try:
                return float(item)
            except ValueError:
                return None

        return None

    def _get_float_list(self, key: str, default: List[float] = None) -> List[float]:
        """Safely get a list of floats from state."""
        if default is None:
            default = []

        data = self._get(key)
        if data is None:
            return default

        return self._extract_float_list(data)

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
        """
        risk = self.base_risk
        adjustments = []
        multipliers = []

        # Pre-check: Circuit breaker
        if self._is_circuit_breaker_active():
            logger.warning("🛑 Circuit breaker active — minimal risk")
            return self.min_risk

        # Step 1: Streak scaling
        risk, streak_mult = self._apply_streak_scaling(risk)
        if streak_mult != 1.0:
            adjustments.append(f"Streak×{streak_mult:.2f}")
            multipliers.append(streak_mult)

        # Step 2: Equity curve momentum
        risk, equity_mult = self._apply_equity_curve_scaling(risk)
        if equity_mult != 1.0:
            adjustments.append(f"Equity×{equity_mult:.2f}")
            multipliers.append(equity_mult)

        # Step 3: Daily drawdown protection
        risk, dd_mult, dd_pct = self._apply_daily_drawdown_protection(risk)
        if dd_mult != 1.0:
            adjustments.append(f"DailyDD({dd_pct:.1f}%)×{dd_mult:.2f}")
            multipliers.append(dd_mult)

        # Step 4: Total drawdown protection
        risk, total_dd_mult, total_dd_pct = self._apply_total_drawdown_protection(risk)
        if total_dd_mult != 1.0:
            adjustments.append(f"TotalDD({total_dd_pct:.1f}%)×{total_dd_mult:.2f}")
            multipliers.append(total_dd_mult)

        # Step 5: Volatility regime
        if market_state is not None:
            risk, vol_mult, regime = self._apply_volatility_regime(risk, market_state)
            if vol_mult != 1.0:
                adjustments.append(f"Vol({regime})×{vol_mult:.2f}")
                multipliers.append(vol_mult)

        # Step 6: Confidence weighting
        if confidence is not None:
            risk, conf_mult = self._apply_confidence_weight(risk, confidence)
            adjustments.append(f"Conf({confidence:.0%})×{conf_mult:.2f}")
            multipliers.append(conf_mult)

        # Step 7: Signal quality
        if signal_quality is not None:
            risk, quality_mult = self._apply_signal_quality(risk, signal_quality)
            if quality_mult != 1.0:
                adjustments.append(f"Quality({signal_quality})×{quality_mult:.2f}")
                multipliers.append(quality_mult)

        # Step 8: Session adjustment (optional)
        if self.enable_session_adjustment:
            risk, session_mult, session = self._apply_session_adjustment(risk)
            if session_mult != 1.0:
                adjustments.append(f"Session({session})×{session_mult:.2f}")
                multipliers.append(session_mult)

        # Step 9: Correlation adjustment (optional)
        if self.enable_correlation_adjustment and num_open_positions > 0:
            risk, corr_mult = self._apply_correlation_adjustment(risk, num_open_positions)
            if corr_mult != 1.0:
                adjustments.append(f"Positions({num_open_positions})×{corr_mult:.2f}")
                multipliers.append(corr_mult)

        # Step 10: Kelly cap
        risk, kelly_applied = self._apply_kelly_cap(risk)
        if kelly_applied:
            adjustments.append("Kelly-capped")

        # Step 11: Smooth transition
        risk, smoothed = self._smooth_risk_transition(risk)
        if smoothed:
            adjustments.append("Smoothed")

        # Step 12: Hard clamp
        final_risk = max(self.min_risk, min(self.max_risk, risk))

        # Determine risk level
        risk_level = self._classify_risk_level(final_risk, multipliers)

        # Logging
        daily_dd = self._current_daily_drawdown_pct()
        total_dd = self._current_total_drawdown_pct()
        adj_str = " | ".join(adjustments) if adjustments else "No adjustments"

        logger.info(
            f"🎯 Risk: {final_risk*100:.3f}% [{risk_level}] | "
            f"Base: {self.base_risk*100:.1f}% | "
            f"DD: {daily_dd:.1f}%/{total_dd:.1f}% | {adj_str}"
        )

        # Persist and track
        self._set("last_risk", final_risk)
        self._set("risk_level", risk_level)
        self._current_risk = final_risk
        self._record_risk(final_risk, adjustments)

        return round(final_risk, 6)

    def get_risk(
        self,
        market_state=None,
        confidence: Optional[float] = None,
        signal_quality: Optional[str] = None,
    ) -> float:
        """Alias for get_risk_percent()."""
        return self.get_risk_percent(market_state, confidence, signal_quality)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 1: STREAK SCALING
    # ═══════════════════════════════════════════════════════════════

    def _apply_streak_scaling(self, risk: float) -> Tuple[float, float]:
        """Non-linear streak scaling using exponential saturation."""
        win_streak = self._safe_int("win_streak")
        loss_streak = self._safe_int("loss_streak")

        multiplier = 1.0

        if win_streak > 0:
            boost = (1 - exp(-win_streak / 4)) * 0.35
            multiplier = 1 + boost
        elif loss_streak > 0:
            cut = (1 - exp(-loss_streak / 3)) * 0.40
            multiplier = 1 - cut

        return risk * multiplier, round(multiplier, 3)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 2: EQUITY CURVE MOMENTUM
    # ═══════════════════════════════════════════════════════════════

    def _apply_equity_curve_scaling(self, risk: float) -> Tuple[float, float]:
        """Scale risk based on recent equity performance."""
        momentum = self._safe_float("equity_momentum")

        if momentum is None or momentum == 0:
            return risk, 1.0

        if momentum > 0:
            adj = min(momentum * 2.5, 0.30)
            multiplier = 1 + adj
        else:
            adj = max(momentum * 2.5, -0.35)
            multiplier = 1 + adj

        return risk * multiplier, round(multiplier, 3)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 3: DAILY DRAWDOWN PROTECTION
    # ═══════════════════════════════════════════════════════════════

    def _apply_daily_drawdown_protection(self, risk: float) -> Tuple[float, float, float]:
        """Tiered daily drawdown circuit breaker."""
        dd_pct = self._current_daily_drawdown()

        if dd_pct <= 0:
            return risk, 1.0, 0.0

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
        """Total drawdown protection from initial balance."""
        dd_pct = self._current_total_drawdown()

        if dd_pct <= 0:
            return risk, 1.0, 0.0

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

        volatility = getattr(market_state, "volatility", None)

        if volatility is None:
            return risk, 1.0, "unknown"

        if volatility > 0.06:
            return risk * 0.55, 0.55, "extreme"
        elif volatility > 0.04:
            return risk * 0.80, 0.80, "high"
        elif volatility > 0.02:
            return risk, 1.0, "normal"
        else:
            return risk * 1.15, 1.15, "low"

    # ═══════════════════════════════════════════════════════════════
    #  STEP 6: CONFIDENCE WEIGHTING
    # ═══════════════════════════════════════════════════════════════

    def _apply_confidence_weight(
        self, risk: float, confidence: float
    ) -> Tuple[float, float]:
        """Scale risk based on signal confidence."""
        confidence = max(0.0, min(1.0, confidence))

        if confidence < 0.4:
            multiplier = 0.60 + (confidence * 0.50)
        elif confidence < 0.6:
            multiplier = 0.80 + ((confidence - 0.4) * 0.75)
        else:
            multiplier = 0.95 + ((confidence - 0.6) * 1.125)

        multiplier = min(1.40, multiplier)

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
        """Adjust risk based on trading session."""
        current_hour = datetime.now(timezone.utc).hour

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
        """Reduce risk when holding multiple positions."""
        if num_positions == 0:
            return risk, 1.0

        multiplier = max(0.50, 1.0 - (num_positions * 0.10))
        return risk * multiplier, round(multiplier, 3)

    # ═══════════════════════════════════════════════════════════════
    #  STEP 10: KELLY CRITERION CAP
    # ═══════════════════════════════════════════════════════════════

    def _apply_kelly_cap(self, risk: float) -> Tuple[float, bool]:
        """Apply fractional Kelly criterion as a soft cap."""
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
            logger.warning(f"📐 Negative Kelly ({kelly:.3f}) — no edge detected")
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
        if self._get("kill_switch"):
            return True

        if not self._get("bot_active", True):
            return True

        if self._current_daily_drawdown() >= self.max_daily_dd:
            return True

        if self._current_total_drawdown() >= self.max_total_dd:
            return True

        max_losses = self._get("max_consecutive_losses", 7)
        if self._safe_int("consecutive_losses") >= max_losses:
            return True

        return False

    def _classify_risk_level(
        self, risk: float, multipliers: List[float]
    ) -> str:
        """Classify current risk level."""
        combined_mult = 1.0
        for m in multipliers:
            combined_mult *= m

        if risk <= self.min_risk * 1.5:
            return RiskLevel.MINIMAL
        elif combined_mult < 0.5:
            return RiskLevel.REDUCED
        elif combined_mult > 1.15:
            return RiskLevel.ELEVATED
        elif combined_mult > 1.3 and risk >= self.max_risk * 0.8:
            return RiskLevel.MAXIMUM
        else:
            return RiskLevel.NORMAL

    # ═══════════════════════════════════════════════════════════════
    #  TRADE RESULT UPDATE
    # ═══════════════════════════════════════════════════════════════

    def update_after_trade(self, trade: Dict) -> Dict:
        """Update statistics after a trade closes."""
        pnl = self._extract_float_value(trade.get("pnl_amount")) or \
              self._extract_float_value(trade.get("net_pnl")) or \
              self._extract_float_value(trade.get("pnl")) or 0.0

        is_win = pnl > 0

        win_streak = self._safe_int("win_streak")
        loss_streak = self._safe_int("loss_streak")

        if is_win:
            win_streak += 1
            loss_streak = 0
            self._increment("total_wins", 1)
            self._record_win_amount(abs(pnl))
        else:
            loss_streak += 1
            win_streak = 0
            self._increment("total_losses", 1)
            self._record_loss_amount(abs(pnl))

        self._set("win_streak", win_streak)
        self._set("loss_streak", loss_streak)
        self._set("consecutive_losses", loss_streak if not is_win else 0)

        # Update equity tracking
        self._update_equity_history()
        self._update_equity_momentum()
        self._update_returns_history(pnl)

        summary = {
            "pnl": pnl,
            "is_win": is_win,
            "win_streak": win_streak,
            "loss_streak": loss_streak,
            "win_rate": self._get_win_rate(),
            "total_trades": self._safe_int("total_wins") + self._safe_int("total_losses"),
            "risk_level": self._get("risk_level", RiskLevel.NORMAL),
        }

        logger.info(
            f"📊 Risk updated | PnL=${pnl:+.4f} | "
            f"W{win_streak}/L{loss_streak} | "
            f"WR={summary['win_rate']*100:.1f}%"
        )

        return summary

    # ═══════════════════════════════════════════════════════════════
    #  POSITION SIZING
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
        """Calculate complete position sizing."""
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

        max_position = balance * 0.95
        if position_value > max_position:
            quantity = max_position / entry_price
            position_value = max_position

        return {
            "quantity": round(quantity, 8),
            "risk_amount": round(risk_amount, 4),
            "risk_percent": round(risk_percent * 100, 3),
            "position_value": round(position_value, 2),
            "risk_per_unit": round(risk_per_unit, 8),
            "balance": balance,
            "leverage": round(position_value / balance, 2) if balance > 0 else 0,
        }

    # ═══════════════════════════════════════════════════════════════
    #  WIN/LOSS TRACKING
    # ═══════════════════════════════════════════════════════════════

    def _record_win_amount(self, amount: float) -> None:
        """Record a winning trade amount."""
        wins = self._get_float_list("win_amounts")
        wins.append(float(amount))
        if len(wins) > 100:
            wins = wins[-100:]
        self._set("win_amounts", wins)

    def _record_loss_amount(self, amount: float) -> None:
        """Record a losing trade amount."""
        losses = self._get_float_list("loss_amounts")
        losses.append(float(amount))
        if len(losses) > 100:
            losses = losses[-100:]
        self._set("loss_amounts", losses)

    def _get_avg_win_loss(self) -> Tuple[float, float]:
        """Get average win and average loss amounts."""
        wins = self._get_float_list("win_amounts")
        losses = self._get_float_list("loss_amounts")

        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        return avg_win, avg_loss

    # ═══════════════════════════════════════════════════════════════
    #  EQUITY TRACKING
    # ═══════════════════════════════════════════════════════════════

    def _update_equity_history(self) -> None:
        """Update equity history with current balance."""
        balance = self._safe_float("balance") or 0
        history = self._get_float_list("equity_history")
        history.append(float(balance))

        if len(history) > 100:
            history = history[-100:]

        self._set("equity_history", history)

    def _update_equity_momentum(self) -> None:
        """Calculate equity curve momentum using linear regression slope."""
        history = self._get_float_list("equity_history")

        if len(history) < 5:
            return

        recent = history[-10:]
        n = len(recent)

        if n == 0 or recent[0] == 0:
            return

        x_mean = (n - 1) / 2
        y_mean = sum(recent) / n

        numerator = sum((i - x_mean) * (recent[i] - y_mean) for i in range(n))
        denominator = sum((i - x_mean) ** 2 for i in range(n))

        if denominator == 0:
            return

        slope = numerator / denominator
        normalized_slope = slope / y_mean if y_mean != 0 else 0

        self._set("equity_momentum", round(normalized_slope, 6))

    def _update_returns_history(self, pnl: float) -> None:
        """Track returns for Sharpe ratio calculation."""
        balance = self._safe_float("balance") or 1
        ret = float(pnl) / balance

        returns = self._get_float_list("returns_history")
        returns.append(ret)

        if len(returns) > 100:
            returns = returns[-100:]

        self._set("returns_history", returns)

    def _record_risk(self, risk: float, adjustments: List[str]) -> None:
        """Record risk for history tracking."""
        self._risk_history.append(risk)
        if len(self._risk_history) > 50:
            self._risk_history = self._risk_history[-50:]

        self._adjustment_history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
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
        value = self._get(key)
        if value is None:
            return default
        try:
            return int(value)
        except (ValueError, TypeError):
            return default

    def _safe_float(self, key: str, default: float = None) -> Optional[float]:
        """Safely get a float from state."""
        value = self._get(key)
        if value is None:
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    # ═══════════════════════════════════════════════════════════════
    #  REPORTS & STATUS
    # ═══════════════════════════════════════════════════════════════

    def get_report(self) -> Dict:
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
            "current_risk_pct": round(last_risk * 100, 3),
            "risk_level": self._get("risk_level", RiskLevel.NORMAL),
            "base_risk_pct": round(self.base_risk * 100, 3),
            "min_risk_pct": round(self.min_risk * 100, 3),
            "max_risk_pct": round(self.max_risk * 100, 3),
            "win_streak": self._safe_int("win_streak"),
            "loss_streak": self._safe_int("loss_streak"),
            "win_rate_pct": round(win_rate * 100, 1),
            "total_wins": self._safe_int("total_wins"),
            "total_losses": self._safe_int("total_losses"),
            "total_trades": self._safe_int("total_wins") + self._safe_int("total_losses"),
            "avg_win": round(avg_win, 4),
            "avg_loss": round(avg_loss, 4),
            "profit_factor": round(profit_factor, 2),
            "kelly_raw_pct": round(kelly_raw * 100, 2),
            "kelly_capped_pct": round(kelly_capped * 100, 3),
            "kelly_fraction": self.kelly_fraction,
            "daily_drawdown_pct": self._current_daily_drawdown_pct(),
            "total_drawdown_pct": self._current_total_drawdown_pct(),
            "max_daily_dd_pct": round(self.max_daily_dd * 100, 1),
            "max_total_dd_pct": round(self.max_total_dd * 100, 1),
            "equity_momentum": self._safe_float("equity_momentum") or 0.0,
        }

    def get_risk_report(self) -> Dict:
        """Alias for get_report()."""
        return self.get_report()

    def get_status(self) -> Dict:
        """Get current status."""
        return {
            "current_risk": self._current_risk,
            "base_risk": self.base_risk,
            "daily_drawdown": self._current_daily_drawdown(),
            "win_streak": self._safe_int("win_streak"),
            "loss_streak": self._safe_int("loss_streak"),
            "daily_pnl": self._safe_float("daily_pnl") or 0,
        }

    # ═══════════════════════════════════════════════════════════════
    #  MANUAL CONTROLS
    # ═══════════════════════════════════════════════════════════════

    def reset_streaks(self) -> None:
        """Reset win/loss streaks."""
        self._set("win_streak", 0)
        self._set("loss_streak", 0)
        self._set("consecutive_losses", 0)
        logger.info("🔄 Risk streaks reset")

    def reset_daily(self) -> None:
        """Reset daily tracking."""
        balance = self._safe_float("balance") or 0
        self._set("start_of_day_balance", balance)
        self._set("daily_pnl", 0)
        self._set("trades_today", 0)
        self._set("daily_wins", 0)
        self._set("daily_losses", 0)
        self._set("equity_momentum", 0.0)
        logger.info(f"🔄 Risk daily reset | Balance: ${balance:.2f}")

    def initialize_day(self, balance: float) -> None:
        """Initialize for new trading day."""
        self._set("start_of_day_balance", balance)
        self._set("balance", balance)
        self._set("initial_balance", self._get("initial_balance") or balance)
        self.reset_daily()

    def update_balance(self, balance: float) -> None:
        """Update current balance."""
        self._set("balance", balance)
        peak = self._safe_float("peak_balance") or 0
        if balance > peak:
            self._set("peak_balance", balance)

    def __repr__(self) -> str:
        last_risk = self._safe_float("last_risk") or self.base_risk
        win_rate = self._get_win_rate()
        level = self._get("risk_level", RiskLevel.NORMAL)
        return (
            f"<AdaptiveRiskManager | "
            f"Risk={last_risk*100:.2f}% [{level}] | "
            f"Base={self.base_risk*100:.1f}% | "
            f"WR={win_rate*100:.1f}% | "
            f"DD={self._current_daily_drawdown_pct():.1f}%>"
        )