# app/risk/trade_limiter.py

"""
Trade Rate Limiter — Production Grade

Multi-layer trade frequency control:
1. Daily trade limit (max trades per day)
2. Hourly trade limit (prevent over-trading in volatile periods)
3. Per-symbol limit (avoid concentration)
4. Minimum interval between trades (anti-churn)
5. Cooldown after rapid trading

Integration with controller:
    Called BEFORE every trade entry via can_open_trade()
    Called AFTER every trade via record_trade()

Usage:
    # In controller.__init__():
    self.trade_limiter = TradeLimiter(state_manager)

    # Before entry in run_cycle():
    allowed, reason = self.trade_limiter.can_open_trade(symbol)
    if not allowed:
        logger.info(f"Trade limited: {reason}")
        return

    # After entry:
    self.trade_limiter.record_trade(symbol, "BUY")
"""

from datetime import datetime, timedelta, date
from typing import Tuple, Dict, List, Optional
from app.utils.logger import get_logger

logger = get_logger(__name__)


class TradeLimiter:
    """
    Institutional-Grade Trade Frequency Control

    Features:
    - Daily / hourly / per-symbol limits
    - Minimum time between trades (prevents churn)
    - Auto-reset on new day/hour
    - Status reporting for Telegram
    - Null-safe (handles missing state gracefully)
    """

    # Max audit entries kept
    MAX_HISTORY = 200

    def __init__(
        self,
        state_manager,
        max_trades_per_day: int = 10,
        max_trades_per_hour: int = 3,
        max_trades_per_symbol: int = 2,
        min_trade_interval_sec: int = 60,
        rapid_trade_threshold: int = 5,      # Trades in 30min to trigger cooldown
        rapid_trade_cooldown_min: int = 30,  # Cooldown duration
    ):
        self.state = state_manager

        self.max_trades_per_day = max_trades_per_day
        self.max_trades_per_hour = max_trades_per_hour
        self.max_trades_per_symbol = max_trades_per_symbol
        self.min_trade_interval_sec = min_trade_interval_sec
        self.rapid_trade_threshold = rapid_trade_threshold
        self.rapid_trade_cooldown_min = rapid_trade_cooldown_min

    # ═════════════════════════════════════════════════════
    #  MAIN GATE — Called before every trade
    # ═════════════════════════════════════════════════════

    def can_open_trade(self, symbol: str = None) -> Tuple[bool, str]:
        """
        Master trade permission check.

        Args:
            symbol: Trading pair (optional, for per-symbol limit)

        Returns:
            (allowed: bool, reason: str)

        Called BEFORE every trade entry decision.
        """
        # ── Ensure counters are fresh ─────────────────────────────
        self._reset_if_new_day()
        self._reset_if_new_hour()

        # ── Check 1: Rapid trading cooldown ───────────────────────
        cooldown_active, remaining = self._check_rapid_cooldown()
        if cooldown_active:
            reason = f"Rapid trading cooldown: {remaining:.0f}s remaining"
            return False, reason

        # ── Check 2: Daily limit ──────────────────────────────────
        daily_count = self._get_daily_count()
        if daily_count >= self.max_trades_per_day:
            reason = f"Daily limit reached: {daily_count}/{self.max_trades_per_day}"
            logger.warning(f"🚫 {reason}")
            return False, reason

        # ── Check 3: Hourly limit ─────────────────────────────────
        hourly_count = self._get_hourly_count()
        if hourly_count >= self.max_trades_per_hour:
            reason = f"Hourly limit reached: {hourly_count}/{self.max_trades_per_hour}"
            logger.info(f"⏳ {reason}")
            return False, reason

        # ── Check 4: Per-symbol limit ─────────────────────────────
        if symbol:
            symbol_count = self._get_symbol_count(symbol)
            if symbol_count >= self.max_trades_per_symbol:
                reason = f"Symbol limit reached: {symbol} has {symbol_count}/{self.max_trades_per_symbol} trades today"
                logger.info(f"📊 {reason}")
                return False, reason

        # ── Check 5: Minimum interval ─────────────────────────────
        interval_ok, wait_time = self._check_min_interval()
        if not interval_ok:
            reason = f"Too soon: wait {wait_time:.0f}s (min interval {self.min_trade_interval_sec}s)"
            return False, reason

        return True, "OK"

    # ═════════════════════════════════════════════════════
    #  RECORD TRADE
    # ═════════════════════════════════════════════════════

    def record_trade(
        self,
        symbol: str,
        action: str,
        metadata: Optional[Dict] = None,
    ) -> Dict:
        """
        Record a trade execution.

        Called AFTER every successful trade entry.

        Args:
            symbol: Trading pair
            action: "BUY" or "SELL"
            metadata: Optional extra info (price, qty, etc.)

        Returns:
            Updated trade counts summary
        """
        now = datetime.utcnow()

        # ── Increment daily count ─────────────────────────────────
        daily_count = self._get_daily_count() + 1
        self.state.set("trades_today", daily_count)
        self.state.set("trades_done_today", daily_count)  # Alias

        # ── Increment hourly count ────────────────────────────────
        hourly_count = self._get_hourly_count() + 1
        self.state.set("trades_this_hour", hourly_count)

        # ── Increment per-symbol count ────────────────────────────
        symbol_counts: Dict = self.state.get("trades_per_symbol") or {}
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        self.state.set("trades_per_symbol", symbol_counts)

        # ── Update last trade time ────────────────────────────────
        self.state.set("last_trade_time", now.isoformat())

        # ── Track recent trades for rapid detection ───────────────
        recent: List[str] = self.state.get("recent_trade_times") or []
        recent.append(now.isoformat())
        # Keep only last 30 minutes of trades
        cutoff = now - timedelta(minutes=30)
        recent = [
            t for t in recent
            if datetime.fromisoformat(t) > cutoff
        ]
        self.state.set("recent_trade_times", recent)

        # ── Check if rapid trading threshold hit ──────────────────
        if len(recent) >= self.rapid_trade_threshold:
            self._activate_rapid_cooldown()

        # ── Audit trail ───────────────────────────────────────────
        record = {
            "symbol": symbol,
            "action": action,
            "timestamp": now.isoformat(),
            "daily_count": daily_count,
            "hourly_count": hourly_count,
            "metadata": metadata or {},
        }
        self._append_history(record)

        logger.debug(
            f"📝 Trade recorded | {symbol} {action} | "
            f"Daily: {daily_count}/{self.max_trades_per_day} | "
            f"Hourly: {hourly_count}/{self.max_trades_per_hour}"
        )

        return {
            "daily_count": daily_count,
            "hourly_count": hourly_count,
            "symbol_count": symbol_counts.get(symbol, 0),
            "remaining_today": max(0, self.max_trades_per_day - daily_count),
            "remaining_hour": max(0, self.max_trades_per_hour - hourly_count),
        }

    # ═════════════════════════════════════════════════════
    #  COUNT HELPERS
    # ═════════════════════════════════════════════════════

    def _get_daily_count(self) -> int:
        """Get number of trades today (null-safe)."""
        count = self.state.get("trades_today")
        if count is None:
            return 0
        try:
            return int(count)
        except (ValueError, TypeError):
            return 0

    def _get_hourly_count(self) -> int:
        """Get number of trades this hour (null-safe)."""
        count = self.state.get("trades_this_hour")
        if count is None:
            return 0
        try:
            return int(count)
        except (ValueError, TypeError):
            return 0

    def _get_symbol_count(self, symbol: str) -> int:
        """Get number of trades for a specific symbol today."""
        symbol_counts: Dict = self.state.get("trades_per_symbol") or {}
        count = symbol_counts.get(symbol, 0)
        try:
            return int(count)
        except (ValueError, TypeError):
            return 0

    # ═════════════════════════════════════════════════════
    #  MINIMUM INTERVAL CHECK
    # ═════════════════════════════════════════════════════

    def _check_min_interval(self) -> Tuple[bool, float]:
        """
        Check if minimum time has passed since last trade.

        Returns:
            (ok: bool, wait_seconds: float)
        """
        if self.min_trade_interval_sec <= 0:
            return True, 0.0

        last_trade = self.state.get("last_trade_time")
        if not last_trade:
            return True, 0.0

        try:
            last_time = datetime.fromisoformat(last_trade)
            elapsed = (datetime.utcnow() - last_time).total_seconds()

            if elapsed < self.min_trade_interval_sec:
                wait = self.min_trade_interval_sec - elapsed
                return False, wait

            return True, 0.0

        except (ValueError, TypeError):
            return True, 0.0

    # ═════════════════════════════════════════════════════
    #  RAPID TRADING COOLDOWN
    # ═════════════════════════════════════════════════════

    def _activate_rapid_cooldown(self) -> None:
        """Activate cooldown due to rapid trading."""
        cooldown_until = datetime.utcnow() + timedelta(
            minutes=self.rapid_trade_cooldown_min
        )
        self.state.set("trade_limiter_cooldown", cooldown_until.isoformat())

        self._append_history({
            "type": "RAPID_COOLDOWN",
            "until": cooldown_until.isoformat(),
            "minutes": self.rapid_trade_cooldown_min,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.warning(
            f"⚠️ Rapid trading detected | "
            f"Cooldown {self.rapid_trade_cooldown_min}min until "
            f"{cooldown_until.strftime('%H:%M:%S')}"
        )

    def _check_rapid_cooldown(self) -> Tuple[bool, float]:
        """
        Check if rapid trading cooldown is active.

        Returns:
            (active: bool, remaining_seconds: float)
        """
        cooldown_until = self.state.get("trade_limiter_cooldown")
        if not cooldown_until:
            return False, 0.0

        try:
            end_time = datetime.fromisoformat(cooldown_until)
            now = datetime.utcnow()

            if now >= end_time:
                # Cooldown expired
                self.state.set("trade_limiter_cooldown", None)
                logger.info("✅ Rapid trading cooldown expired")
                return False, 0.0

            remaining = (end_time - now).total_seconds()
            return True, remaining

        except (ValueError, TypeError):
            self.state.set("trade_limiter_cooldown", None)
            return False, 0.0

    def clear_cooldown(self, source: str = "manual") -> None:
        """Manually clear rapid trading cooldown."""
        self.state.set("trade_limiter_cooldown", None)
        self.state.set("recent_trade_times", [])

        self._append_history({
            "type": "COOLDOWN_CLEARED",
            "source": source,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.warning(f"⚠️ Trade limiter cooldown cleared by {source}")

    # ═════════════════════════════════════════════════════
    #  AUTO-RESET
    # ═════════════════════════════════════════════════════

    def _reset_if_new_day(self) -> None:
        """Reset daily counters when date changes."""
        today = str(date.today())
        last_reset = self.state.get("trade_limiter_last_daily_reset")

        if last_reset == today:
            return

        self.state.set("trades_today", 0)
        self.state.set("trades_done_today", 0)
        self.state.set("trades_per_symbol", {})
        self.state.set("trade_limiter_last_daily_reset", today)

        self._append_history({
            "type": "DAILY_RESET",
            "date": today,
            "timestamp": datetime.utcnow().isoformat(),
        })

        logger.info(f"🔄 Trade limiter daily reset | {today}")

    def _reset_if_new_hour(self) -> None:
        """Reset hourly counters when hour changes."""
        current_hour = datetime.utcnow().strftime("%Y-%m-%d-%H")
        last_hour_reset = self.state.get("trade_limiter_last_hour_reset")

        if last_hour_reset == current_hour:
            return

        self.state.set("trades_this_hour", 0)
        self.state.set("trade_limiter_last_hour_reset", current_hour)

        logger.debug(f"🔄 Trade limiter hourly reset | {current_hour}")

    # ═════════════════════════════════════════════════════
    #  AUDIT TRAIL
    # ═════════════════════════════════════════════════════

    def _append_history(self, record: Dict) -> None:
        """Append event to trade limiter history (capped)."""
        history: list = self.state.get("trade_limiter_history") or []
        history.append(record)

        if len(history) > self.MAX_HISTORY:
            history = history[-self.MAX_HISTORY:]

        self.state.set("trade_limiter_history", history)

    def get_history(self, last_n: int = 20) -> List[Dict]:
        """Return last N trade limiter events."""
        history = self.state.get("trade_limiter_history") or []
        return history[-last_n:]

    # ═════════════════════════════════════════════════════
    #  STATUS REPORTING
    # ═════════════════════════════════════════════════════

    def get_status(self) -> Dict:
        """
        Full trade limiter status for Telegram /status or /limits.
        """
        daily_count = self._get_daily_count()
        hourly_count = self._get_hourly_count()
        cooldown_active, cooldown_remaining = self._check_rapid_cooldown()
        interval_ok, interval_wait = self._check_min_interval()

        symbol_counts: Dict = self.state.get("trades_per_symbol") or {}

        return {
            # Can trade?
            "can_trade": self.can_open_trade()[0],
            "block_reason": self.can_open_trade()[1] if not self.can_open_trade()[0] else None,

            # Daily
            "trades_today": daily_count,
            "max_trades_per_day": self.max_trades_per_day,
            "remaining_today": max(0, self.max_trades_per_day - daily_count),

            # Hourly
            "trades_this_hour": hourly_count,
            "max_trades_per_hour": self.max_trades_per_hour,
            "remaining_hour": max(0, self.max_trades_per_hour - hourly_count),

            # Per-symbol
            "trades_per_symbol": symbol_counts,
            "max_trades_per_symbol": self.max_trades_per_symbol,

            # Interval
            "min_trade_interval_sec": self.min_trade_interval_sec,
            "interval_wait_sec": round(interval_wait, 0) if not interval_ok else 0,

            # Cooldown
            "rapid_cooldown_active": cooldown_active,
            "rapid_cooldown_remaining_sec": round(cooldown_remaining, 0),

            # Last trade
            "last_trade_time": self.state.get("last_trade_time", ""),
        }

    def get_remaining(self) -> Dict:
        """Quick summary of remaining trade capacity."""
        daily_count = self._get_daily_count()
        hourly_count = self._get_hourly_count()

        return {
            "daily": max(0, self.max_trades_per_day - daily_count),
            "hourly": max(0, self.max_trades_per_hour - hourly_count),
            "can_trade": self.can_open_trade()[0],
        }

    # ═════════════════════════════════════════════════════
    #  LEGACY COMPATIBILITY
    # ═════════════════════════════════════════════════════

    def can_trade(self) -> bool:
        """
        Legacy method for backwards compatibility.

        Use can_open_trade() for new code (returns reason too).
        """
        allowed, _ = self.can_open_trade()
        return allowed

    # ═════════════════════════════════════════════════════
    #  CONFIGURATION UPDATE
    # ═════════════════════════════════════════════════════

    def update_limits(
        self,
        max_daily: int = None,
        max_hourly: int = None,
        max_per_symbol: int = None,
        min_interval: int = None,
    ) -> Dict:
        """
        Hot-update limits without restart.

        Returns new configuration.
        """
        if max_daily is not None:
            self.max_trades_per_day = max_daily
        if max_hourly is not None:
            self.max_trades_per_hour = max_hourly
        if max_per_symbol is not None:
            self.max_trades_per_symbol = max_per_symbol
        if min_interval is not None:
            self.min_trade_interval_sec = min_interval

        logger.info(
            f"⚙️ Trade limits updated | "
            f"Daily={self.max_trades_per_day} | "
            f"Hourly={self.max_trades_per_hour} | "
            f"PerSymbol={self.max_trades_per_symbol} | "
            f"Interval={self.min_trade_interval_sec}s"
        )

        return {
            "max_trades_per_day": self.max_trades_per_day,
            "max_trades_per_hour": self.max_trades_per_hour,
            "max_trades_per_symbol": self.max_trades_per_symbol,
            "min_trade_interval_sec": self.min_trade_interval_sec,
        }

    def __repr__(self) -> str:
        daily = self._get_daily_count()
        hourly = self._get_hourly_count()
        return (
            f"<TradeLimiter "
            f"Daily={daily}/{self.max_trades_per_day} | "
            f"Hourly={hourly}/{self.max_trades_per_hour}>"
        )