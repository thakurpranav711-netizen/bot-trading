# app/risk/trade_limiter.py

"""
Trade Rate Limiter — Production Grade for Autonomous Trading

Multi-layer trade frequency control:
1. Daily trade limit (max trades per day)
2. Hourly trade limit (prevent over-trading in volatile periods)
3. Per-symbol limit (avoid concentration)
4. Minimum interval between trades (anti-churn)
5. Rapid trading cooldown (burst protection)
6. Losing streak slowdown (reduce frequency after losses)
7. Time-of-day restrictions (optional)
8. Position-based limits (limit if too many open)

Integration:
    Controller calls can_open_trade() BEFORE every entry
    Controller calls record_trade() AFTER every successful entry
    Controller calls record_trade_close() AFTER every exit
"""

from datetime import datetime, timedelta, date, time as dt_time
from typing import Tuple, Dict, List, Optional
from enum import Enum
from app.utils.logger import get_logger

logger = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
#  LIMIT STATUS
# ═══════════════════════════════════════════════════════════════════

class LimitStatus(Enum):
    """Trade limit status."""
    AVAILABLE = "available"
    WARNING = "warning"      # Approaching limit
    LIMITED = "limited"      # At or near limit
    BLOCKED = "blocked"      # Cannot trade


class BlockReason(Enum):
    """Reasons for blocking trades."""
    DAILY_LIMIT = "daily_limit"
    HOURLY_LIMIT = "hourly_limit"
    SYMBOL_LIMIT = "symbol_limit"
    INTERVAL = "interval"
    RAPID_COOLDOWN = "rapid_cooldown"
    LOSS_SLOWDOWN = "loss_slowdown"
    MAX_POSITIONS = "max_positions"
    TIME_RESTRICTION = "time_restriction"


# ═══════════════════════════════════════════════════════════════════
#  TRADE LIMITER
# ═══════════════════════════════════════════════════════════════════

class TradeLimiter:
    """
    Institutional-Grade Trade Frequency Control

    Features:
    - Daily / hourly / per-symbol limits
    - Minimum time between trades
    - Rapid-fire burst detection + cooldown
    - Loss streak slowdown
    - Position count limits
    - Time-of-day restrictions
    - Auto-reset on new day/hour
    - Hot-updatable limits
    - Status reporting
    - Full audit trail
    """

    MAX_HISTORY = 200
    WARNING_THRESHOLD = 0.80  # Warn at 80% of limit

    def __init__(
        self,
        state_manager,
        max_trades_per_day: int = 10,
        max_trades_per_hour: int = 3,
        max_trades_per_symbol: int = 3,
        min_trade_interval_sec: int = 60,
        rapid_trade_threshold: int = 5,
        rapid_trade_cooldown_min: int = 30,
        max_open_positions: int = 3,
        loss_slowdown_enabled: bool = True,
        loss_slowdown_multiplier: float = 2.0,
        trading_hours: Optional[Tuple[int, int]] = None,
    ):
        """
        Initialize Trade Limiter.

        Args:
            state_manager: State manager instance
            max_trades_per_day: Maximum trades allowed per day
            max_trades_per_hour: Maximum trades per hour
            max_trades_per_symbol: Maximum trades per symbol per day
            min_trade_interval_sec: Minimum seconds between trades
            rapid_trade_threshold: Trades in 30min to trigger cooldown
            rapid_trade_cooldown_min: Cooldown duration in minutes
            max_open_positions: Maximum concurrent open positions
            loss_slowdown_enabled: Increase interval after losses
            loss_slowdown_multiplier: Multiply interval by this per loss
            trading_hours: Tuple of (start_hour, end_hour) UTC, None = 24/7
        """
        self.state = state_manager

        # Limits
        self.max_trades_per_day = max_trades_per_day
        self.max_trades_per_hour = max_trades_per_hour
        self.max_trades_per_symbol = max_trades_per_symbol
        self.min_trade_interval_sec = min_trade_interval_sec
        self.rapid_trade_threshold = rapid_trade_threshold
        self.rapid_trade_cooldown_min = rapid_trade_cooldown_min
        self.max_open_positions = max_open_positions

        # Loss slowdown
        self.loss_slowdown_enabled = loss_slowdown_enabled
        self.loss_slowdown_multiplier = loss_slowdown_multiplier

        # Trading hours (None = 24/7)
        self.trading_hours = trading_hours

        # Tracking
        self._consecutive_blocks: int = 0
        self._last_block_reason: Optional[str] = None

        # Sync to state
        self.state.set("max_trades_per_day", max_trades_per_day)

        logger.info(
            f"⏱️ TradeLimiter initialized | "
            f"Daily={max_trades_per_day} | "
            f"Hourly={max_trades_per_hour} | "
            f"Symbol={max_trades_per_symbol} | "
            f"Interval={min_trade_interval_sec}s | "
            f"MaxPositions={max_open_positions}"
        )

    # ═══════════════════════════════════════════════════════════════
    #  MAIN GATE
    # ═══════════════════════════════════════════════════════════════

    def can_open_trade(
        self,
        symbol: str = None,
        check_positions: bool = True,
    ) -> Tuple[bool, str]:
        """
        Master trade permission check.

        Checks in order:
        1. Trading hours restriction
        2. Rapid trading cooldown
        3. Maximum open positions
        4. Daily limit
        5. Hourly limit
        6. Per-symbol limit
        7. Minimum interval (with loss slowdown)

        Args:
            symbol: Trading pair (for per-symbol limit)
            check_positions: Whether to check position count

        Returns:
            (allowed: bool, reason: str)
        """
        # Ensure counters are fresh
        self._reset_if_new_day()
        self._reset_if_new_hour()

        # Check 1: Trading hours
        if not self._is_trading_hours():
            reason = "Outside trading hours"
            self._record_block(BlockReason.TIME_RESTRICTION, reason)
            return False, reason

        # Check 2: Rapid trading cooldown
        cooldown_active, remaining = self._check_rapid_cooldown()
        if cooldown_active:
            reason = f"Rapid trading cooldown: {remaining/60:.1f}min remaining"
            self._record_block(BlockReason.RAPID_COOLDOWN, reason)
            return False, reason

        # Check 3: Maximum open positions
        if check_positions:
            position_ok, position_reason = self._check_position_limit()
            if not position_ok:
                self._record_block(BlockReason.MAX_POSITIONS, position_reason)
                return False, position_reason

        # Check 4: Daily limit
        daily_count = self._get_daily_count()
        if daily_count >= self.max_trades_per_day:
            reason = f"Daily limit: {daily_count}/{self.max_trades_per_day}"
            self._record_block(BlockReason.DAILY_LIMIT, reason)
            return False, reason

        # Check 5: Hourly limit
        hourly_count = self._get_hourly_count()
        if hourly_count >= self.max_trades_per_hour:
            reason = f"Hourly limit: {hourly_count}/{self.max_trades_per_hour}"
            self._record_block(BlockReason.HOURLY_LIMIT, reason)
            return False, reason

        # Check 6: Per-symbol limit
        if symbol:
            symbol_count = self._get_symbol_count(symbol)
            if symbol_count >= self.max_trades_per_symbol:
                reason = f"Symbol limit: {symbol} has {symbol_count}/{self.max_trades_per_symbol}"
                self._record_block(BlockReason.SYMBOL_LIMIT, reason)
                return False, reason

        # Check 7: Minimum interval (with loss slowdown)
        interval_ok, wait_time = self._check_min_interval()
        if not interval_ok:
            reason = f"Wait {wait_time:.0f}s (min interval)"
            self._record_block(BlockReason.INTERVAL, reason)
            return False, reason

        # All checks passed
        self._consecutive_blocks = 0
        return True, "OK"

    def get_limit_status(self) -> LimitStatus:
        """Get current overall limit status."""
        can_trade, _ = self.can_open_trade(check_positions=False)

        if not can_trade:
            return LimitStatus.BLOCKED

        daily_count = self._get_daily_count()
        hourly_count = self._get_hourly_count()

        daily_pct = daily_count / self.max_trades_per_day if self.max_trades_per_day > 0 else 0
        hourly_pct = hourly_count / self.max_trades_per_hour if self.max_trades_per_hour > 0 else 0

        if daily_pct >= self.WARNING_THRESHOLD or hourly_pct >= self.WARNING_THRESHOLD:
            return LimitStatus.WARNING

        if daily_pct >= 0.5 or hourly_pct >= 0.5:
            return LimitStatus.LIMITED

        return LimitStatus.AVAILABLE

    def _record_block(self, reason: BlockReason, details: str) -> None:
        """Record a block event."""
        self._consecutive_blocks += 1
        self._last_block_reason = reason.value
        logger.debug(f"🚫 Trade blocked ({self._consecutive_blocks}x): {details}")

    # ═══════════════════════════════════════════════════════════════
    #  RECORD TRADE
    # ═══════════════════════════════════════════════════════════════

    def record_trade(
        self,
        symbol: str,
        action: str,
        metadata: Optional[Dict] = None,
    ) -> Dict:
        """
        Record a trade execution (entry).

        Updates all counters and checks for rapid trading.

        Args:
            symbol: Trading pair
            action: "BUY" or "SELL"
            metadata: Optional extra info

        Returns:
            Updated trade counts summary
        """
        now = datetime.utcnow()

        # Increment daily count
        daily_count = self._get_daily_count() + 1
        self.state.set("trades_today", daily_count)
        self.state.set("trades_done_today", daily_count)

        # Increment hourly count
        hourly_count = self._get_hourly_count() + 1
        self.state.set("trades_this_hour", hourly_count)

        # Increment per-symbol count
        symbol_counts: Dict = self.state.get("trades_per_symbol") or {}
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        self.state.set("trades_per_symbol", symbol_counts)

        # Update last trade time
        self.state.set("last_trade_time", now.isoformat())

        # Track recent trades for rapid detection
        self._track_recent_trade(now)

        # Check for rapid trading
        rapid_triggered = False
        recent_count = len(self.state.get("recent_trade_times") or [])
        if recent_count >= self.rapid_trade_threshold:
            self._activate_rapid_cooldown()
            rapid_triggered = True

        # Audit trail
        self._append_history({
            "type": "TRADE_OPENED",
            "symbol": symbol,
            "action": action,
            "timestamp": now.isoformat(),
            "daily_count": daily_count,
            "hourly_count": hourly_count,
            "metadata": metadata or {},
        })

        logger.debug(
            f"📝 Trade recorded | {symbol} {action} | "
            f"Daily={daily_count}/{self.max_trades_per_day} | "
            f"Hourly={hourly_count}/{self.max_trades_per_hour}"
        )

        return {
            "daily_count": daily_count,
            "hourly_count": hourly_count,
            "symbol_count": symbol_counts.get(symbol, 0),
            "remaining_today": max(0, self.max_trades_per_day - daily_count),
            "remaining_hour": max(0, self.max_trades_per_hour - hourly_count),
            "rapid_triggered": rapid_triggered,
        }

    def record_trade_close(
        self,
        symbol: str,
        pnl: float,
        is_win: bool,
    ) -> None:
        """
        Record a trade close for loss slowdown tracking.

        Args:
            symbol: Trading pair
            pnl: Profit/loss amount
            is_win: Whether trade was profitable
        """
        if not self.loss_slowdown_enabled:
            return

        if is_win:
            # Reset loss slowdown on win
            self.state.set("trade_limiter_loss_count", 0)
        else:
            # Increment loss count for slowdown
            loss_count = (self.state.get("trade_limiter_loss_count") or 0) + 1
            self.state.set("trade_limiter_loss_count", loss_count)

            if loss_count >= 2:
                logger.info(
                    f"⏳ Loss slowdown active: {loss_count} recent losses, "
                    f"interval multiplied by {self.loss_slowdown_multiplier}"
                )

    def _track_recent_trade(self, timestamp: datetime) -> None:
        """Track recent trade for rapid detection."""
        recent: List[str] = self.state.get("recent_trade_times") or []
        recent.append(timestamp.isoformat())

        # Keep only last 30 minutes
        cutoff = timestamp - timedelta(minutes=30)
        recent = [
            t for t in recent
            if datetime.fromisoformat(t) > cutoff
        ]

        self.state.set("recent_trade_times", recent)

    # ═══════════════════════════════════════════════════════════════
    #  COUNT HELPERS
    # ═══════════════════════════════════════════════════════════════

    def _get_daily_count(self) -> int:
        """Get trades today."""
        count = self.state.get("trades_today")
        try:
            return int(count) if count is not None else 0
        except (ValueError, TypeError):
            return 0

    def _get_hourly_count(self) -> int:
        """Get trades this hour."""
        count = self.state.get("trades_this_hour")
        try:
            return int(count) if count is not None else 0
        except (ValueError, TypeError):
            return 0

    def _get_symbol_count(self, symbol: str) -> int:
        """Get trades for a symbol today."""
        symbol_counts: Dict = self.state.get("trades_per_symbol") or {}
        count = symbol_counts.get(symbol, 0)
        try:
            return int(count)
        except (ValueError, TypeError):
            return 0

    # ═══════════════════════════════════════════════════════════════
    #  POSITION LIMIT
    # ═══════════════════════════════════════════════════════════════

    def _check_position_limit(self) -> Tuple[bool, str]:
        """Check if position count is within limit."""
        positions = self.state.get_all_positions()
        count = len(positions) if positions else 0

        if count >= self.max_open_positions:
            return False, f"Max positions: {count}/{self.max_open_positions}"

        return True, ""

    def get_open_position_count(self) -> int:
        """Get current open position count."""
        positions = self.state.get_all_positions()
        return len(positions) if positions else 0

    # ═══════════════════════════════════════════════════════════════
    #  MINIMUM INTERVAL (with loss slowdown)
    # ═══════════════════════════════════════════════════════════════

    def _check_min_interval(self) -> Tuple[bool, float]:
        """
        Check minimum time since last trade.

        Applies loss slowdown multiplier if enabled.
        """
        if self.min_trade_interval_sec <= 0:
            return True, 0.0

        last_trade = self.state.get("last_trade_time")
        if not last_trade:
            return True, 0.0

        try:
            last_time = datetime.fromisoformat(last_trade)
            elapsed = (datetime.utcnow() - last_time).total_seconds()

            # Calculate effective interval (with loss slowdown)
            effective_interval = self._get_effective_interval()

            if elapsed < effective_interval:
                wait = effective_interval - elapsed
                return False, wait

            return True, 0.0

        except (ValueError, TypeError):
            self.state.set("last_trade_time", None)
            return True, 0.0

    def _get_effective_interval(self) -> float:
        """Get effective interval with loss slowdown applied."""
        base_interval = self.min_trade_interval_sec

        if not self.loss_slowdown_enabled:
            return base_interval

        loss_count = self.state.get("trade_limiter_loss_count") or 0

        if loss_count >= 2:
            # Apply multiplier for each loss above 1
            multiplier = self.loss_slowdown_multiplier ** (loss_count - 1)
            # Cap at 10x
            multiplier = min(multiplier, 10.0)
            return base_interval * multiplier

        return base_interval

    # ═══════════════════════════════════════════════════════════════
    #  RAPID TRADING COOLDOWN
    # ═══════════════════════════════════════════════════════════════

    def _activate_rapid_cooldown(self) -> None:
        """Activate rapid trading cooldown."""
        cooldown_until = datetime.utcnow() + timedelta(
            minutes=self.rapid_trade_cooldown_min
        )
        self.state.set("trade_limiter_cooldown", cooldown_until.isoformat())

        # Clear recent trades
        self.state.set("recent_trade_times", [])

        self._append_history({
            "type": "RAPID_COOLDOWN",
            "until": cooldown_until.isoformat(),
            "minutes": self.rapid_trade_cooldown_min,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.warning(
            f"⚠️ Rapid trading cooldown | "
            f"{self.rapid_trade_cooldown_min}min until "
            f"{cooldown_until.strftime('%H:%M')} UTC"
        )

    def _check_rapid_cooldown(self) -> Tuple[bool, float]:
        """Check if rapid cooldown is active."""
        cooldown_until = self.state.get("trade_limiter_cooldown")
        if not cooldown_until:
            return False, 0.0

        try:
            end_time = datetime.fromisoformat(cooldown_until)
            now = datetime.utcnow()

            if now >= end_time:
                self.state.set("trade_limiter_cooldown", None)
                logger.info("✅ Rapid cooldown expired")
                return False, 0.0

            remaining = (end_time - now).total_seconds()
            return True, remaining

        except (ValueError, TypeError):
            self.state.set("trade_limiter_cooldown", None)
            return False, 0.0

    def clear_cooldown(self, source: str = "manual") -> Dict:
        """Clear rapid trading cooldown."""
        self.state.set("trade_limiter_cooldown", None)
        self.state.set("recent_trade_times", [])

        self._append_history({
            "type": "COOLDOWN_CLEARED",
            "source": source,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.warning(f"⚠️ Cooldown cleared | Source: {source}")
        return {"success": True}

    def extend_cooldown(self, additional_minutes: int) -> Dict:
        """Extend rapid trading cooldown."""
        current = self.state.get("trade_limiter_cooldown")

        if not current:
            new_time = datetime.utcnow() + timedelta(minutes=additional_minutes)
        else:
            try:
                current_time = datetime.fromisoformat(current)
                new_time = current_time + timedelta(minutes=additional_minutes)
            except:
                new_time = datetime.utcnow() + timedelta(minutes=additional_minutes)

        self.state.set("trade_limiter_cooldown", new_time.isoformat())

        return {
            "success": True,
            "cooldown_until": new_time.isoformat(),
            "added_minutes": additional_minutes,
        }

    # ═══════════════════════════════════════════════════════════════
    #  TRADING HOURS
    # ═══════════════════════════════════════════════════════════════

    def _is_trading_hours(self) -> bool:
        """Check if within trading hours."""
        if self.trading_hours is None:
            return True  # 24/7

        start_hour, end_hour = self.trading_hours
        current_hour = datetime.utcnow().hour

        if start_hour <= end_hour:
            return start_hour <= current_hour < end_hour
        else:
            # Overnight window (e.g., 22:00 - 06:00)
            return current_hour >= start_hour or current_hour < end_hour

    def set_trading_hours(
        self,
        start_hour: Optional[int],
        end_hour: Optional[int]
    ) -> None:
        """Set trading hours (None for 24/7)."""
        if start_hour is None or end_hour is None:
            self.trading_hours = None
            logger.info("⏰ Trading hours: 24/7")
        else:
            self.trading_hours = (start_hour, end_hour)
            logger.info(f"⏰ Trading hours: {start_hour}:00 - {end_hour}:00 UTC")

    # ═══════════════════════════════════════════════════════════════
    #  AUTO-RESET
    # ═══════════════════════════════════════════════════════════════

    def _reset_if_new_day(self) -> None:
        """Reset daily counters when date rolls."""
        today = str(date.today())
        last_reset = self.state.get("trade_limiter_last_daily_reset")

        if last_reset == today:
            return

        self.state.set("trades_today", 0)
        self.state.set("trades_done_today", 0)
        self.state.set("trades_per_symbol", {})
        self.state.set("trade_limiter_loss_count", 0)
        self.state.set("trade_limiter_last_daily_reset", today)

        self._append_history({
            "type": "DAILY_RESET",
            "date": today,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(f"🔄 TradeLimiter daily reset | {today}")

    def _reset_if_new_hour(self) -> None:
        """Reset hourly counters when hour rolls."""
        current_hour = datetime.utcnow().strftime("%Y-%m-%d-%H")
        last_reset = self.state.get("trade_limiter_last_hour_reset")

        if last_reset == current_hour:
            return

        self.state.set("trades_this_hour", 0)
        self.state.set("trade_limiter_last_hour_reset", current_hour)

    def reset_daily(self) -> None:
        """Manual full daily reset."""
        today = str(date.today())
        self.state.set("trades_today", 0)
        self.state.set("trades_done_today", 0)
        self.state.set("trades_this_hour", 0)
        self.state.set("trades_per_symbol", {})
        self.state.set("recent_trade_times", [])
        self.state.set("last_trade_time", None)
        self.state.set("trade_limiter_loss_count", 0)
        self.state.set("trade_limiter_last_daily_reset", today)
        self.state.set("trade_limiter_last_hour_reset",
                       datetime.utcnow().strftime("%Y-%m-%d-%H"))

        self._consecutive_blocks = 0

        logger.info("🔄 TradeLimiter full reset")

    def reset_interval_timer(self) -> None:
        """Clear minimum interval timer."""
        self.state.set("last_trade_time", None)
        logger.info("⏱️ Interval timer cleared")

    def reset_loss_slowdown(self) -> None:
        """Reset loss slowdown counter."""
        self.state.set("trade_limiter_loss_count", 0)
        logger.info("🔄 Loss slowdown reset")

    # ═══════════════════════════════════════════════════════════════
    #  AUDIT TRAIL
    # ═══════════════════════════════════════════════════════════════

    def _append_history(self, record: Dict) -> None:
        """Append to history."""
        history: list = self.state.get("trade_limiter_history") or []
        history.append(record)

        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]

        self.state.set("trade_limiter_history", history)

    def get_history(self, limit: int = 20) -> List[Dict]:
        """Get recent history."""
        history = self.state.get("trade_limiter_history") or []
        return history[-limit:]

    # ═══════════════════════════════════════════════════════════════
    #  STATUS
    # ═══════════════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """Get full status."""
        daily_count = self._get_daily_count()
        hourly_count = self._get_hourly_count()
        cooldown_active, cooldown_remaining = self._check_rapid_cooldown()
        interval_ok, interval_wait = self._check_min_interval()
        position_count = self.get_open_position_count()
        symbol_counts: Dict = self.state.get("trades_per_symbol") or {}
        loss_count = self.state.get("trade_limiter_loss_count") or 0

        can_trade_result, block_reason = self.can_open_trade()
        status = self.get_limit_status()

        return {
            "status": status.value,
            "can_trade": can_trade_result,
            "block_reason": block_reason if not can_trade_result else "",
            "consecutive_blocks": self._consecutive_blocks,

            # Daily
            "trades_today": daily_count,
            "max_trades_per_day": self.max_trades_per_day,
            "remaining_today": max(0, self.max_trades_per_day - daily_count),
            "daily_utilization_pct": round(daily_count / self.max_trades_per_day * 100, 1) if self.max_trades_per_day > 0 else 0,

            # Hourly
            "trades_this_hour": hourly_count,
            "max_trades_per_hour": self.max_trades_per_hour,
            "remaining_hour": max(0, self.max_trades_per_hour - hourly_count),

            # Per-symbol
            "trades_per_symbol": symbol_counts,
            "max_trades_per_symbol": self.max_trades_per_symbol,

            # Positions
            "open_positions": position_count,
            "max_open_positions": self.max_open_positions,
            "position_slots_available": max(0, self.max_open_positions - position_count),

            # Interval
            "min_trade_interval_sec": self.min_trade_interval_sec,
            "effective_interval_sec": round(self._get_effective_interval()),
            "interval_wait_sec": round(interval_wait) if not interval_ok else 0,

            # Loss slowdown
            "loss_slowdown_active": loss_count >= 2,
            "loss_count": loss_count,

            # Rapid cooldown
            "rapid_cooldown_active": cooldown_active,
            "rapid_cooldown_remaining_sec": round(cooldown_remaining),
            "rapid_cooldown_remaining_min": round(cooldown_remaining / 60, 1) if cooldown_active else 0,

            # Trading hours
            "trading_hours": f"{self.trading_hours[0]}:00-{self.trading_hours[1]}:00 UTC" if self.trading_hours else "24/7",
            "in_trading_hours": self._is_trading_hours(),

            # Last trade
            "last_trade_time": self.state.get("last_trade_time") or "",
        }

    def get_remaining(self) -> Dict:
        """Quick summary of remaining capacity."""
        daily_count = self._get_daily_count()
        hourly_count = self._get_hourly_count()
        position_count = self.get_open_position_count()

        return {
            "daily": max(0, self.max_trades_per_day - daily_count),
            "hourly": max(0, self.max_trades_per_hour - hourly_count),
            "positions": max(0, self.max_open_positions - position_count),
            "can_trade": self.can_open_trade()[0],
        }

    # ═══════════════════════════════════════════════════════════════
    #  HOT-UPDATE LIMITS
    # ═══════════════════════════════════════════════════════════════

    def update_limits(
        self,
        max_daily: int = None,
        max_hourly: int = None,
        max_per_symbol: int = None,
        max_positions: int = None,
        min_interval: int = None,
    ) -> Dict:
        """Hot-update limits."""
        if max_daily is not None:
            self.max_trades_per_day = max_daily
            self.state.set("max_trades_per_day", max_daily)

        if max_hourly is not None:
            self.max_trades_per_hour = max_hourly

        if max_per_symbol is not None:
            self.max_trades_per_symbol = max_per_symbol

        if max_positions is not None:
            self.max_open_positions = max_positions

        if min_interval is not None:
            self.min_trade_interval_sec = min_interval

        logger.info(
            f"⚙️ Limits updated | "
            f"Daily={self.max_trades_per_day} | "
            f"Hourly={self.max_trades_per_hour} | "
            f"Symbol={self.max_trades_per_symbol} | "
            f"Positions={self.max_open_positions}"
        )

        return self.get_config()

    def get_config(self) -> Dict:
        """Get current configuration."""
        return {
            "max_trades_per_day": self.max_trades_per_day,
            "max_trades_per_hour": self.max_trades_per_hour,
            "max_trades_per_symbol": self.max_trades_per_symbol,
            "max_open_positions": self.max_open_positions,
            "min_trade_interval_sec": self.min_trade_interval_sec,
            "rapid_trade_threshold": self.rapid_trade_threshold,
            "rapid_trade_cooldown_min": self.rapid_trade_cooldown_min,
            "loss_slowdown_enabled": self.loss_slowdown_enabled,
            "trading_hours": self.trading_hours,
        }

    # ═══════════════════════════════════════════════════════════════
    #  LEGACY
    # ═══════════════════════════════════════════════════════════════

    def can_trade(self) -> bool:
        """Legacy method for backwards compatibility."""
        allowed, _ = self.can_open_trade()
        return allowed

    # ═══════════════════════════════════════════════════════════════
    #  REPRESENTATION
    # ═══════════════════════════════════════════════════════════════

    def __repr__(self) -> str:
        daily = self._get_daily_count()
        hourly = self._get_hourly_count()
        positions = self.get_open_position_count()
        status = self.get_limit_status()

        return (
            f"<TradeLimiter [{status.value}] | "
            f"D={daily}/{self.max_trades_per_day} | "
            f"H={hourly}/{self.max_trades_per_hour} | "
            f"P={positions}/{self.max_open_positions}>"
        )