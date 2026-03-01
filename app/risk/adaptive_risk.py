# app/risk/adaptive_risk.py

"""
Adaptive Risk Manager — Production Grade (FIXED)

Institutional-grade position sizing with:
1. Base risk allocation
2. Non-linear streak scaling (win/loss)
3. Equity curve momentum awareness
4. Drawdown circuit breaker (tiered)
5. Volatility regime adjustment
6. Confidence weighting (from 4-brain signals)
7. Kelly criterion soft cap (properly calculated)
8. Smooth transitions (anti-shock damping)
9. Hard clamp to [min_risk, max_risk]

FIXES APPLIED:
- Drawdown tier 5% now uses 0.10 multiplier instead of 0.00
- _current_drawdown() properly floors at 0.0
- Added reset_daily() for daily sync
- Better logging with DD% in output
- Safer negative drawdown handling throughout

Integration with controller:
    Called for EVERY trade to determine position size via get_risk_percent()
    Called AFTER every trade to update statistics via update_after_trade()

Usage:
    # In controller.__init__():
    self.adaptive_risk = AdaptiveRiskManager(state_manager, base_risk=0.01)

    # Before position sizing:
    risk_pct = self.adaptive_risk.get_risk_percent(market_state, confidence)
    position_value = balance * risk_pct / (entry_price - stop_loss)

    # After trade closes:
    self.adaptive_risk.update_after_trade(trade_result)
"""

from typing import Dict, Optional, List, Tuple
from math import exp
from app.utils.logger import get_logger

logger = get_logger(__name__)


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
    7. Apply Kelly criterion cap
    8. Smooth transition from last risk
    9. Clamp to [min_risk, max_risk]

    All calculations are transparent and logged for audit.
    """

    # ── Volatility regime multipliers ─────────────────────────────
    VOLATILITY_REGIME_MULT: Dict[str, float] = {
        "low": 1.15,
        "normal": 1.00,
        "high": 0.75,
        "extreme": 0.50,
    }

    # ── Drawdown tiers (FIXED) ────────────────────────────────────
    # (threshold_pct, risk_multiplier)
    # NOTE: These use DAILY drawdown (start_of_day_balance)
    #       NOT total drawdown (initial_balance) — that's in LossGuard
    DRAWDOWN_TIERS: List[Tuple[float, float]] = [
        (0.05, 0.10),   # FIXED: ≥5% DD → 10% of calculated (was 0.00 = zero)
        (0.04, 0.40),   # ≥4% DD → 40% of calculated risk
        (0.03, 0.60),   # ≥3% DD → 60% of calculated risk
        (0.02, 0.80),   # ≥2% DD → 80% of calculated risk
    ]

    def __init__(
        self,
        state_manager,
        base_risk: float = 0.01,
        min_risk: float = 0.003,
        max_risk: float = 0.03,
        max_daily_dd: float = 0.05,
        kelly_fraction: float = 0.25,
        smooth_factor: float = 0.40,
        min_trades_for_kelly: int = 10,
    ):
        self.state = state_manager

        self.base_risk = base_risk
        self.min_risk = min_risk
        self.max_risk = max_risk
        self.max_daily_dd = max_daily_dd
        self.kelly_fraction = kelly_fraction
        self.smooth_factor = smooth_factor
        self.min_trades_for_kelly = min_trades_for_kelly

    # ═════════════════════════════════════════════════════
    #  PRIMARY API — Called before every trade
    # ═════════════════════════════════════════════════════

    def get_risk_percent(
        self,
        market_state=None,
        confidence: Optional[float] = None,
    ) -> float:
        """
        Calculate adaptive risk percentage for current trade.

        Args:
            market_state: MarketState object (for volatility regime)
            confidence: 0-1 confidence from 4-brain signals

        Returns:
            Risk fraction (e.g., 0.012 = 1.2% of balance)

        This is the ONLY method controller needs to call for sizing.
        """
        risk = self.base_risk
        adjustments = []

        # ── Step 1: Streak scaling ────────────────────────────────
        risk, streak_adj = self._apply_streak_scaling(risk)
        if streak_adj != 1.0:
            adjustments.append(f"Streak ×{streak_adj:.2f}")

        # ── Step 2: Equity curve momentum ─────────────────────────
        risk, equity_adj = self._apply_equity_curve_scaling(risk)
        if equity_adj != 1.0:
            adjustments.append(f"Equity ×{equity_adj:.2f}")

        # ── Step 3: Drawdown protection ───────────────────────────
        risk, dd_adj, dd_pct = self._apply_drawdown_protection(risk)
        if dd_adj != 1.0:
            adjustments.append(f"DD({dd_pct:.1f}%) ×{dd_adj:.2f}")

        # ── Step 4: Volatility regime ─────────────────────────────
        if market_state is not None:
            risk, vol_adj, regime = self._apply_volatility_regime(risk, market_state)
            if vol_adj != 1.0:
                adjustments.append(f"Vol({regime}) ×{vol_adj:.2f}")

        # ── Step 5: Confidence weighting ──────────────────────────
        if confidence is not None:
            risk, conf_adj = self._apply_confidence_weight(risk, confidence)
            adjustments.append(f"Conf({confidence:.2f}) ×{conf_adj:.2f}")

        # ── Step 6: Kelly cap ─────────────────────────────────────
        risk, kelly_applied = self._apply_kelly_cap(risk)
        if kelly_applied:
            adjustments.append("Kelly capped")

        # ── Step 7: Smooth transition ─────────────────────────────
        risk, smoothed = self._smooth_risk_transition(risk)
        if smoothed:
            adjustments.append("Smoothed")

        # ── Step 8: Hard clamp ────────────────────────────────────
        final_risk = max(self.min_risk, min(self.max_risk, risk))

        # ── FIXED: Enhanced logging with DD% ──────────────────────
        adj_str = " | ".join(adjustments) if adjustments else "No adjustments"
        dd_pct_current = self._current_drawdown_pct()
        logger.info(
            f"🎯 Adaptive Risk: {final_risk * 100:.3f}% | "
            f"Base: {self.base_risk * 100:.1f}% | "
            f"DD: {dd_pct_current:.1f}% | {adj_str}"
        )

        # ── Persist for next cycle ────────────────────────────────
        self.state.set("last_risk", final_risk)

        return round(final_risk, 6)

    # Backwards-compat alias
    def get_risk(
        self,
        market_state=None,
        confidence: Optional[float] = None
    ) -> float:
        """Alias for get_risk_percent() — backwards compatibility."""
        return self.get_risk_percent(market_state, confidence)

    # ═════════════════════════════════════════════════════
    #  STEP 1: STREAK SCALING
    # ═════════════════════════════════════════════════════

    def _apply_streak_scaling(self, risk: float) -> Tuple[float, float]:
        """
        Non-linear streak scaling using exponential saturation.

        Win streak:  Increases risk with diminishing returns (cap +25%)
        Loss streak: Decreases risk aggressively (cap -50%)

        Returns:
            (adjusted_risk, multiplier)
        """
        win_streak = self._safe_int("win_streak")
        loss_streak = self._safe_int("loss_streak")

        multiplier = 1.0

        if win_streak > 0:
            boost = (1 - exp(-win_streak / 3)) * 0.25
            multiplier = 1 + boost

        elif loss_streak > 0:
            cut = (1 - exp(-loss_streak / 2)) * 0.50
            multiplier = 1 - cut

        return risk * multiplier, round(multiplier, 3)

    # ═════════════════════════════════════════════════════
    #  STEP 2: EQUITY CURVE MOMENTUM
    # ═════════════════════════════════════════════════════

    def _apply_equity_curve_scaling(self, risk: float) -> Tuple[float, float]:
        """
        Scale risk based on recent equity performance.

        Positive momentum → scale up (max +20%)
        Negative momentum → scale down (max -40%)

        Returns:
            (adjusted_risk, multiplier)
        """
        momentum = self._safe_float("equity_momentum")

        if momentum is None or momentum == 0:
            return risk, 1.0

        if momentum > 0:
            adj = min(momentum * 2, 0.20)
            multiplier = 1 + adj
        else:
            adj = max(momentum * 2, -0.40)
            multiplier = 1 + adj

        return risk * multiplier, round(multiplier, 3)

    # ═════════════════════════════════════════════════════
    #  STEP 3: DRAWDOWN PROTECTION (FIXED)
    # ═════════════════════════════════════════════════════

    def _apply_drawdown_protection(
        self, risk: float
    ) -> Tuple[float, float, float]:
        """
        Tiered drawdown circuit breaker.

        Uses start-of-day balance (not initial_balance) for DAILY protection.
        Total/emergency drawdown is handled by LossGuard separately.

        FIXED:
        - 5% tier now uses 0.10 multiplier (was 0.00 which killed risk entirely)
        - Negative drawdown (profit) is properly handled (returns 1.0 multiplier)

        Returns:
            (adjusted_risk, multiplier, current_dd_pct)
        """
        dd_pct = self._current_drawdown()

        # FIXED: If in profit (negative drawdown), no adjustment
        if dd_pct <= 0:
            return risk, 1.0, 0.0

        # Check tiers from most severe to least
        for threshold, multiplier in self.DRAWDOWN_TIERS:
            if dd_pct >= threshold:
                if multiplier <= 0.10:
                    # FIXED: Near-circuit-breaker, but don't zero out
                    # Let the hard clamp at Step 8 enforce min_risk
                    logger.warning(
                        f"🛑 Drawdown circuit breaker | "
                        f"DD={dd_pct * 100:.1f}% ≥ {threshold * 100}% → "
                        f"risk × {multiplier}"
                    )
                return risk * multiplier, multiplier, dd_pct * 100

        return risk, 1.0, dd_pct * 100

    # ═════════════════════════════════════════════════════
    #  STEP 4: VOLATILITY REGIME
    # ═════════════════════════════════════════════════════

    def _apply_volatility_regime(
        self, risk: float, market_state
    ) -> Tuple[float, float, str]:
        """
        Adjust risk based on market volatility regime.

        Reads volatility_regime string from MarketState.
        Falls back to numeric volatility if not available.

        Returns:
            (adjusted_risk, multiplier, regime_label)
        """
        regime = getattr(market_state, "volatility_regime", None)

        if regime and regime in self.VOLATILITY_REGIME_MULT:
            mult = self.VOLATILITY_REGIME_MULT[regime]
            return risk * mult, mult, regime

        volatility = getattr(market_state, "volatility", None)

        if volatility is None:
            return risk, 1.0, "unknown"

        if volatility > 0.06:
            return risk * 0.50, 0.50, "extreme"
        elif volatility > 0.04:
            return risk * 0.75, 0.75, "high"
        elif volatility > 0.02:
            return risk, 1.0, "normal"
        else:
            return risk * 1.15, 1.15, "low"

    # ═════════════════════════════════════════════════════
    #  STEP 5: CONFIDENCE WEIGHTING
    # ═════════════════════════════════════════════════════

    def _apply_confidence_weight(
        self, risk: float, confidence: float
    ) -> Tuple[float, float]:
        """
        Scale risk based on signal confidence.

        Maps confidence [0, 1] → multiplier [0.60, 1.30]
        Low confidence = smaller position
        High confidence = larger position

        Returns:
            (adjusted_risk, multiplier)
        """
        confidence = max(0.0, min(1.0, confidence))
        multiplier = 0.60 + (confidence * 0.70)
        return risk * multiplier, round(multiplier, 3)

    # ═════════════════════════════════════════════════════
    #  STEP 6: KELLY CRITERION CAP
    # ═════════════════════════════════════════════════════

    def _apply_kelly_cap(self, risk: float) -> Tuple[float, bool]:
        """
        Apply fractional Kelly criterion as a soft cap.

        Kelly formula: f = W - (1-W)/R
        Where:
            W = win rate
            R = average win / average loss

        We use fractional Kelly (default 25%) to stay conservative.

        Returns:
            (adjusted_risk, was_capped: bool)
        """
        total_trades = self._safe_int("total_wins") + self._safe_int("total_losses")

        if total_trades < self.min_trades_for_kelly:
            return risk, False

        win_rate = self._get_win_rate()
        avg_win, avg_loss = self._get_avg_win_loss()

        if avg_loss == 0 or win_rate <= 0:
            return risk, False

        r_ratio = avg_win / avg_loss if avg_loss > 0 else 1.0
        kelly = win_rate - (1 - win_rate) / r_ratio

        if kelly <= 0:
            logger.warning(
                f"📐 Negative Kelly ({kelly:.3f}) — no statistical edge"
            )
            return self.min_risk, True

        kelly_cap = kelly * self.kelly_fraction

        if risk > kelly_cap:
            logger.debug(
                f"📐 Kelly cap: {risk * 100:.3f}% → {kelly_cap * 100:.3f}% | "
                f"WinRate={win_rate * 100:.1f}% R={r_ratio:.2f}"
            )
            return kelly_cap, True

        return risk, False

    # ═════════════════════════════════════════════════════
    #  STEP 7: SMOOTH TRANSITION
    # ═════════════════════════════════════════════════════

    def _smooth_risk_transition(self, new_risk: float) -> Tuple[float, bool]:
        """
        Prevent jarring risk changes between trades.

        Limits single-step change to smooth_factor of last risk.

        Returns:
            (smoothed_risk, was_smoothed: bool)
        """
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

        logger.debug(
            f"📉 Risk smoothed: {new_risk * 100:.3f}% → {smoothed * 100:.3f}% | "
            f"Max change: ±{max_change * 100:.3f}%"
        )

        return smoothed, True

    # ═════════════════════════════════════════════════════
    #  TRADE RESULT UPDATE
    # ═════════════════════════════════════════════════════

    def update_after_trade(self, trade: Dict) -> Dict:
        """
        Update statistics after a trade closes.

        Called by controller from _execute_exit().
        Updates: streaks, win/loss counts, equity history,
                 win/loss amounts for Kelly calculation.

        Args:
            trade: Dict with keys:
                - pnl_amount or net_pnl: float (profit/loss)
                - entry_price: float
                - exit_price: float
                - quantity: float

        Returns:
            Summary of updated statistics
        """
        pnl = trade.get("pnl_amount", trade.get("net_pnl", 0))

        win_streak = self._safe_int("win_streak")
        loss_streak = self._safe_int("loss_streak")

        if pnl > 0:
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
        self.state.set("consecutive_losses", loss_streak)

        self._update_equity_history()
        self._update_equity_momentum()

        logger.info(
            f"📊 Risk stats updated | "
            f"PnL=${pnl:+.4f} | "
            f"Streaks W{win_streak}/L{loss_streak} | "
            f"WinRate={self._get_win_rate() * 100:.1f}%"
        )

        return {
            "pnl": pnl,
            "win_streak": win_streak,
            "loss_streak": loss_streak,
            "win_rate": self._get_win_rate(),
            "total_trades": self._safe_int("total_wins") + self._safe_int("total_losses"),
        }

    # ═════════════════════════════════════════════════════
    #  WIN/LOSS TRACKING (for Kelly)
    # ═════════════════════════════════════════════════════

    def _record_win_amount(self, amount: float) -> None:
        """Record a winning trade amount for Kelly calculation."""
        wins: List[float] = self.state.get("win_amounts") or []
        wins.append(amount)

        if len(wins) > 50:
            wins = wins[-50:]

        self.state.set("win_amounts", wins)

    def _record_loss_amount(self, amount: float) -> None:
        """Record a losing trade amount for Kelly calculation."""
        losses: List[float] = self.state.get("loss_amounts") or []
        losses.append(amount)

        if len(losses) > 50:
            losses = losses[-50:]

        self.state.set("loss_amounts", losses)

    def _get_avg_win_loss(self) -> Tuple[float, float]:
        """
        Get average win and average loss amounts.

        Returns:
            (avg_win, avg_loss) — both as positive numbers
        """
        wins: List[float] = self.state.get("win_amounts") or []
        losses: List[float] = self.state.get("loss_amounts") or []

        avg_win = sum(wins) / len(wins) if wins else 0.0
        avg_loss = sum(losses) / len(losses) if losses else 0.0

        return avg_win, avg_loss

    # ═════════════════════════════════════════════════════
    #  EQUITY TRACKING
    # ═════════════════════════════════════════════════════

    def _update_equity_history(self) -> None:
        """Append current balance to equity history (max 50 points)."""
        balance = self._safe_float("balance") or 0
        history: List[float] = self.state.get("equity_history") or []

        history.append(balance)

        if len(history) > 50:
            history = history[-50:]

        self.state.set("equity_history", history)

    def _update_equity_momentum(self) -> None:
        """
        Calculate equity curve momentum from recent history.

        Uses linear slope of last 5 equity points normalized by starting value.
        """
        history: List[float] = self.state.get("equity_history") or []

        if len(history) < 5:
            return

        recent = history[-5:]

        if recent[0] == 0:
            return

        slope = (recent[-1] - recent[0]) / recent[0]
        self.state.set("equity_momentum", round(slope, 6))

    # ═════════════════════════════════════════════════════
    #  STAT HELPERS (FIXED)
    # ═════════════════════════════════════════════════════

    def _get_win_rate(self) -> float:
        """Calculate win rate from total wins/losses."""
        wins = self._safe_int("total_wins")
        losses = self._safe_int("total_losses")
        total = wins + losses

        if total == 0:
            return 0.5

        return wins / total

    def _current_drawdown(self) -> float:
        """
        Calculate current DAILY drawdown from start-of-day balance.

        NOTE: This is DAILY drawdown only.
        Total/emergency drawdown is handled by LossGuard._check_emergency_stop()
        which uses initial_balance.

        FIXED: Properly floors at 0.0 (profit = 0% drawdown, not negative)

        Returns:
            Drawdown as decimal (0.05 = 5%), minimum 0.0
        """
        start = self._safe_float("start_of_day_balance")
        current = self._safe_float("balance")

        if not start or start <= 0:
            return 0.0

        if not current:
            current = 0.0

        dd = (start - current) / start

        # FIXED: Ensure non-negative (profit should not give negative drawdown)
        return max(0.0, dd)

    def _current_drawdown_pct(self) -> float:
        """Current daily drawdown as percentage."""
        return round(self._current_drawdown() * 100, 2)

    # ═════════════════════════════════════════════════════
    #  NULL-SAFE STATE ACCESS
    # ═════════════════════════════════════════════════════

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

    # ═════════════════════════════════════════════════════
    #  STATUS REPORTING
    # ═════════════════════════════════════════════════════

    def get_risk_report(self) -> Dict:
        """
        Generate risk report for Telegram /status or /risk command.
        """
        last_risk = self._safe_float("last_risk") or self.base_risk
        win_rate = self._get_win_rate()
        avg_win, avg_loss = self._get_avg_win_loss()

        kelly_raw = 0.0
        kelly_capped = 0.0
        if avg_loss > 0:
            r_ratio = avg_win / avg_loss
            kelly_raw = win_rate - (1 - win_rate) / r_ratio
            kelly_capped = max(0, kelly_raw) * self.kelly_fraction

        return {
            "current_risk_pct": round(last_risk * 100, 3),
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
            "profit_factor": round(avg_win / avg_loss, 2) if avg_loss > 0 else 0.0,

            "kelly_raw_pct": round(kelly_raw * 100, 2),
            "kelly_capped_pct": round(kelly_capped * 100, 3),
            "kelly_fraction": self.kelly_fraction,

            "drawdown_pct": self._current_drawdown_pct(),
            "max_daily_dd_pct": round(self.max_daily_dd * 100, 1),

            "equity_momentum": self._safe_float("equity_momentum") or 0.0,
        }

    # ═════════════════════════════════════════════════════
    #  MANUAL OVERRIDES
    # ═════════════════════════════════════════════════════

    def reset_streaks(self) -> None:
        """Manually reset win/loss streaks (e.g., after strategy change)."""
        self.state.set("win_streak", 0)
        self.state.set("loss_streak", 0)
        self.state.set("consecutive_losses", 0)
        logger.info("🔄 Risk manager streaks reset")

    def reset_statistics(self) -> None:
        """
        Full reset of all risk statistics.
        Use with caution — loses all historical data.
        """
        self.state.set("win_streak", 0)
        self.state.set("loss_streak", 0)
        self.state.set("consecutive_losses", 0)
        self.state.set("total_wins", 0)
        self.state.set("total_losses", 0)
        self.state.set("win_amounts", [])
        self.state.set("loss_amounts", [])
        self.state.set("equity_history", [])
        self.state.set("equity_momentum", 0.0)
        self.state.set("last_risk", None)
        logger.warning("⚠️ Risk manager statistics fully reset")

    def reset_daily(self) -> None:
        """
        Reset daily tracking — NEW METHOD.

        Called by loss_guard or controller on daily reset.
        Resets equity momentum and refreshes equity history
        with current balance.
        """
        balance = self._safe_float("balance") or 0
        self.state.set("equity_momentum", 0.0)

        # Keep last 5 equity points for momentum continuity
        history: List[float] = self.state.get("equity_history") or []
        if history:
            history = history[-5:]
        else:
            history = [balance]

        self.state.set("equity_history", history)
        logger.info(f"🔄 Adaptive risk daily reset | Balance: ${balance:.2f}")

    def set_base_risk(self, new_base: float) -> None:
        """Hot-update base risk without restart."""
        old = self.base_risk
        self.base_risk = max(self.min_risk, min(self.max_risk, new_base))
        logger.info(f"⚙️ Base risk updated: {old * 100:.2f}% → {self.base_risk * 100:.2f}%")

    # ═════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═════════════════════════════════════════════════════

    def __repr__(self) -> str:
        last_risk = self._safe_float("last_risk") or self.base_risk
        win_rate = self._get_win_rate()
        return (
            f"<AdaptiveRiskManager | "
            f"Risk={last_risk * 100:.2f}% | "
            f"WinRate={win_rate * 100:.1f}% | "
            f"DD={self._current_drawdown_pct():.1f}%>"
        )