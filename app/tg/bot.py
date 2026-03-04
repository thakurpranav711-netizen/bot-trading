# app/tg/bot.py

"""
Telegram Bot Integration — Production Grade v2.1

Provides:
- Async Telegram bot using python-telegram-bot v20+
- Command handling via TelegramCommands
- Authentication via TelegramAuth (role-based)
- TelegramNotifier class with message queuing and retry
- Message rate limiting (Telegram API: 30 msg/sec global)
- Message chunking for long texts (4096 char limit)
- Delivery tracking and stats
- Graceful startup with drop_pending_updates
- **IMPROVED: Conflict detection and graceful degradation**
- **IMPROVED: Webhook cleanup before polling**
- Keepalive loop with health monitoring
- Graceful shutdown via stop_telegram_bot()
- Proactive alert dispatch from logger critical buffer
- Error handling with exponential backoff

Usage:
    # In main.py:
    app = await start_telegram_bot(controller, drop_pending_updates=True)

    # Later, on shutdown:
    await stop_telegram_bot(app)

    # Or via controller reference:
    telegram_app = getattr(controller, '_telegram_app', None)
    if telegram_app:
        await stop_telegram_bot(telegram_app)
"""

import asyncio
import html
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional

from app.utils.logger import get_logger, get_pending_alerts
from app.utils.time import (
    Cooldown,
    format_duration,
    get_utc_now,
    get_uptime_str,
)

logger = get_logger(__name__)

# ═════════════════════════════════════════════════════════════════
#  TELEGRAM IMPORT (graceful fallback)
# ═════════════════════════════════════════════════════════════════

try:
    from telegram import Update, Bot
    from telegram.ext import (
        Application,
        CommandHandler,
        ContextTypes,
        MessageHandler,
        filters,
    )
    from telegram.constants import ParseMode
    from telegram.error import (
        RetryAfter,
        TimedOut,
        NetworkError,
        TelegramError,
        Conflict,
    )
    TELEGRAM_AVAILABLE = True
    _HTML = ParseMode.HTML
    _MARKDOWN = ParseMode.MARKDOWN_V2
except ImportError:
    TELEGRAM_AVAILABLE = False
    _HTML = "HTML"
    _MARKDOWN = "MarkdownV2"
    RetryAfter = None       # type: ignore
    TimedOut = None          # type: ignore
    NetworkError = None      # type: ignore
    TelegramError = None     # type: ignore
    Conflict = None          # type: ignore
    logger.warning(
        "python-telegram-bot not installed. "
        "Install with: pip install python-telegram-bot>=20.0"
    )


# ═════════════════════════════════════════════════════════════════
#  CONSTANTS
# ═════════════════════════════════════════════════════════════════

# Telegram API limits
MAX_MESSAGE_LENGTH = 4096
MAX_MESSAGES_PER_SECOND = 30
MAX_MESSAGES_PER_MINUTE_PER_CHAT = 20

# Retry config
MAX_SEND_RETRIES = 3
RETRY_BASE_DELAY = 1.0     # seconds
RETRY_MAX_DELAY = 30.0     # seconds

# Keepalive
KEEPALIVE_INTERVAL = 60    # seconds
ALERT_CHECK_INTERVAL = 30  # seconds

# Message queue
MAX_QUEUE_SIZE = 200

# Conflict handling
CONFLICT_WAIT_TIME = 10    # seconds to wait for other instance to release
MAX_CONFLICT_RETRIES = 3


# ═════════════════════════════════════════════════════════════════
#  DATA CLASSES
# ═════════════════════════════════════════════════════════════════

@dataclass
class MessageRecord:
    """Record of a sent message."""
    timestamp: datetime
    chat_id: str
    success: bool
    length: int
    retry_count: int = 0
    error: Optional[str] = None
    category: str = "general"


@dataclass
class NotifierStats:
    """Notifier delivery statistics."""
    messages_sent: int = 0
    messages_failed: int = 0
    messages_queued: int = 0
    messages_dropped: int = 0
    total_retries: int = 0
    total_chars_sent: int = 0
    errors_by_type: Dict[str, int] = field(default_factory=dict)
    last_send_time: Optional[datetime] = None
    last_error_time: Optional[datetime] = None
    last_error: str = ""
    started_at: Optional[datetime] = None


# ═════════════════════════════════════════════════════════════════
#  MESSAGE UTILITIES
# ═════════════════════════════════════════════════════════════════

def _escape_html(text: str) -> str:
    """Escape HTML special characters for Telegram."""
    return html.escape(str(text))


def _chunk_message(
    text: str,
    max_length: int = MAX_MESSAGE_LENGTH,
) -> List[str]:
    """
    Split a long message into chunks that fit Telegram's limit.

    Tries to split at newlines for readability.

    Args:
        text: Message text
        max_length: Maximum chunk size

    Returns:
        List of message chunks
    """
    if len(text) <= max_length:
        return [text]

    chunks: List[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break

        # Try to split at last newline within limit
        split_pos = remaining[:max_length].rfind("\n")
        if split_pos < max_length // 2:
            # No good newline found — split at limit
            split_pos = max_length

        chunk = remaining[:split_pos]
        remaining = remaining[split_pos:].lstrip("\n")
        chunks.append(chunk)

    return chunks


def _build_separator() -> str:
    """Standard message separator line."""
    return "━━━━━━━━━━━━━━━━━━━━━"


def _utc_stamp() -> str:
    """Current UTC timestamp formatted for messages."""
    return get_utc_now().strftime("%Y-%m-%d %H:%M:%S UTC")


def _utc_time_short() -> str:
    """Short UTC time for messages."""
    return get_utc_now().strftime("%H:%M:%S UTC")


# ═════════════════════════════════════════════════════════════════
#  TELEGRAM NOTIFIER
# ═════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    Async notification sender for trading events.

    Features:
    - Automatic message chunking (>4096 chars)
    - Retry with exponential backoff
    - Rate limiting (respects Telegram API limits)
    - Delivery tracking and statistics
    - Message queue for burst handling
    - Category-based cooldowns (prevent spam)

    Used by controller to send:
    - Bot started/stopped messages
    - Trade executed/closed notifications
    - Decision engine reports
    - Market updates
    - Hourly reports
    - Error alerts
    - Kill switch activations
    """

    def __init__(self, bot: Any, chat_id: str, enabled: bool = True):
        self.bot = bot
        self.chat_id = chat_id
        self._enabled = enabled  # Can be disabled if Telegram fails

        # Statistics
        self._stats = NotifierStats(started_at=get_utc_now())

        # Rate limiting
        self._send_times: Deque[float] = deque(maxlen=MAX_MESSAGES_PER_SECOND)
        self._chat_send_times: Deque[float] = deque(
            maxlen=MAX_MESSAGES_PER_MINUTE_PER_CHAT
        )

        # Category cooldowns (prevent spam per message type)
        self._category_cooldowns: Dict[str, Cooldown] = {
            "market_update": Cooldown(seconds=60),
            "hourly_report": Cooldown(seconds=3500),
            "error": Cooldown(seconds=30),
            "heartbeat": Cooldown(seconds=300),
        }

        # Message history
        self._history: Deque[MessageRecord] = deque(maxlen=100)

        # Send lock (serialize sends)
        self._send_lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        """Check if notifier is enabled."""
        return self._enabled

    def disable(self, reason: str = "Unknown") -> None:
        """Disable the notifier (e.g., due to persistent conflicts)."""
        if self._enabled:
            self._enabled = False
            logger.warning(f"TelegramNotifier disabled: {reason}")

    def enable(self) -> None:
        """Re-enable the notifier."""
        if not self._enabled:
            self._enabled = True
            logger.info("TelegramNotifier re-enabled")

    # ═══════════════════════════════════════════════════════
    #  CORE SEND
    # ═══════════════════════════════════════════════════════

    async def _send(
        self,
        text: str,
        parse_mode: Optional[str] = None,
        category: str = "general",
        force: bool = False,
        disable_notification: bool = False,
    ) -> bool:
        """
        Send a Telegram message with retry, chunking, and rate limiting.

        Args:
            text: Message text
            parse_mode: Parse mode (default: HTML)
            category: Message category for cooldown
            force: Skip cooldown check
            disable_notification: Send silently

        Returns:
            True if sent successfully
        """
        # Check if notifier is enabled
        if not self._enabled:
            logger.debug(f"Notification skipped (notifier disabled) | category={category}")
            return False

        if parse_mode is None:
            parse_mode = _HTML

        # Category cooldown check
        if not force and category in self._category_cooldowns:
            cd = self._category_cooldowns[category]
            if not cd.try_acquire():
                logger.debug(
                    f"Notification skipped (cooldown) | "
                    f"category={category} | "
                    f"remaining={cd.remaining_str}"
                )
                return False

        # Chunk long messages
        chunks = _chunk_message(text)

        all_ok = True
        for i, chunk in enumerate(chunks):
            ok = await self._send_single(
                chunk,
                parse_mode=parse_mode,
                category=category,
                disable_notification=disable_notification,
            )
            if not ok:
                all_ok = False
                break

            # Small delay between chunks
            if i < len(chunks) - 1:
                await asyncio.sleep(0.5)

        return all_ok

    async def _send_single(
        self,
        text: str,
        parse_mode: str = "HTML",
        category: str = "general",
        disable_notification: bool = False,
    ) -> bool:
        """Send a single message with retry and rate limiting."""
        if not self._enabled:
            return False

        async with self._send_lock:
            # Rate limit check
            await self._enforce_rate_limit()

            for attempt in range(MAX_SEND_RETRIES):
                try:
                    await self.bot.send_message(
                        chat_id=self.chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        disable_notification=disable_notification,
                    )

                    # Record success
                    now = get_utc_now()
                    self._stats.messages_sent += 1
                    self._stats.total_chars_sent += len(text)
                    self._stats.total_retries += attempt
                    self._stats.last_send_time = now

                    self._send_times.append(time.monotonic())
                    self._chat_send_times.append(time.monotonic())

                    self._history.append(MessageRecord(
                        timestamp=now,
                        chat_id=self.chat_id,
                        success=True,
                        length=len(text),
                        retry_count=attempt,
                        category=category,
                    ))

                    return True

                except Exception as e:
                    error_type = type(e).__name__
                    delay = min(
                        RETRY_BASE_DELAY * (2 ** attempt),
                        RETRY_MAX_DELAY,
                    )

                    # Handle Telegram-specific errors
                    if RetryAfter and isinstance(e, RetryAfter):
                        delay = e.retry_after + 1
                        logger.warning(
                            f"Telegram rate limit — "
                            f"waiting {delay}s"
                        )
                    elif TimedOut and isinstance(e, TimedOut):
                        logger.warning(
                            f"Telegram timeout — "
                            f"retry {attempt + 1}/{MAX_SEND_RETRIES}"
                        )
                    elif NetworkError and isinstance(e, NetworkError):
                        logger.warning(
                            f"Telegram network error — "
                            f"retry {attempt + 1}/{MAX_SEND_RETRIES}"
                        )

                    # Track error type
                    self._stats.errors_by_type[error_type] = (
                        self._stats.errors_by_type.get(error_type, 0) + 1
                    )

                    if attempt < MAX_SEND_RETRIES - 1:
                        await asyncio.sleep(delay)
                    else:
                        # Final failure
                        self._stats.messages_failed += 1
                        self._stats.last_error = str(e)[:200]
                        self._stats.last_error_time = get_utc_now()

                        self._history.append(MessageRecord(
                            timestamp=get_utc_now(),
                            chat_id=self.chat_id,
                            success=False,
                            length=len(text),
                            retry_count=attempt + 1,
                            error=str(e)[:100],
                            category=category,
                        ))

                        logger.error(
                            f"Telegram send failed after "
                            f"{MAX_SEND_RETRIES} retries: {e}"
                        )
                        return False

        return False

    async def _enforce_rate_limit(self) -> None:
        """Wait if we're sending too fast."""
        now = time.monotonic()

        # Global rate limit (30/sec)
        if len(self._send_times) >= MAX_MESSAGES_PER_SECOND:
            oldest = self._send_times[0]
            elapsed = now - oldest
            if elapsed < 1.0:
                wait = 1.0 - elapsed + 0.1
                logger.debug(f"Rate limit: waiting {wait:.2f}s")
                await asyncio.sleep(wait)

        # Per-chat rate limit (20/min)
        if (
            len(self._chat_send_times)
            >= MAX_MESSAGES_PER_MINUTE_PER_CHAT
        ):
            oldest = self._chat_send_times[0]
            elapsed = now - oldest
            if elapsed < 60.0:
                wait = 60.0 - elapsed + 0.5
                logger.debug(
                    f"Chat rate limit: waiting {wait:.2f}s"
                )
                await asyncio.sleep(wait)

    # ═══════════════════════════════════════════════════════
    #  LIFECYCLE NOTIFICATIONS
    # ═══════════════════════════════════════════════════════

    async def send_bot_started(
        self,
        mode: str,
        balance: float,
        coins: List[str],
        interval: int,
        timestamp: Optional[datetime] = None,
    ) -> None:
        """Send bot started notification."""
        ts = timestamp or get_utc_now()
        coins_str = ", ".join(coins) if coins else "None"
        sep = _build_separator()
        text = (
            f"🤖 <b>TRADING BOT STARTED</b>\n"
            f"{sep}\n"
            f"📊 Mode: <code>{_escape_html(mode)}</code>\n"
            f"💰 Balance: <code>${balance:,.2f}</code>\n"
            f"🪙 Coins: <code>{_escape_html(coins_str)}</code>\n"
            f"⏱️ Interval: <code>{interval}s ({format_duration(interval)})</code>\n"
            f"🕐 Time: <code>{ts.strftime('%Y-%m-%d %H:%M:%S')} UTC</code>\n"
            f"{sep}\n"
            f"✅ Bot is now actively trading!"
        )
        await self._send(text, category="lifecycle", force=True)

    async def send_bot_stopped(
        self,
        reason: str = "User request",
        uptime: Optional[str] = None,
        session_pnl: Optional[float] = None,
    ) -> None:
        """Send bot stopped notification."""
        sep = _build_separator()
        uptime_str = uptime or get_uptime_str()

        pnl_line = ""
        if session_pnl is not None:
            pnl_emoji = "📈" if session_pnl >= 0 else "📉"
            pnl_line = (
                f"{pnl_emoji} Session PnL: "
                f"<code>${session_pnl:+,.2f}</code>\n"
            )

        text = (
            f"🛑 <b>TRADING BOT STOPPED</b>\n"
            f"{sep}\n"
            f"📝 Reason: <code>{_escape_html(reason)}</code>\n"
            f"⏱️ Uptime: <code>{uptime_str}</code>\n"
            f"{pnl_line}"
            f"🕐 Time: <code>{_utc_stamp()}</code>\n"
            f"{sep}\n"
            f"⏸️ Trading has been paused."
        )
        await self._send(text, category="lifecycle", force=True)

    # ═══════════════════════════════════════════════════════
    #  TRADE NOTIFICATIONS
    # ═══════════════════════════════════════════════════════

    async def send_trade_executed(
        self,
        mode: str,
        side: str,
        coin: str,
        amount: float,
        price: float,
        cost: float,
        fee: float = 0.0,
        remaining: float = 0.0,
        strategy: str = "",
        reason: str = "",
        confidence: float = 0.0,
        order_id: str = "",
    ) -> None:
        """Send trade executed notification."""
        emoji = "🟢" if side.upper() == "BUY" else "🔴"
        conf_pct = confidence * 100 if confidence <= 1 else confidence
        sep = _build_separator()

        order_line = (
            f"🆔 Order: <code>{_escape_html(order_id)}</code>\n"
            if order_id else ""
        )

        text = (
            f"{emoji} <b>TRADE EXECUTED</b>\n"
            f"{sep}\n"
            f"📊 Mode: <code>{_escape_html(mode)}</code>\n"
            f"🪙 Coin: <code>{_escape_html(coin)}</code>\n"
            f"📈 Side: <code>{side.upper()}</code>\n"
            f"📦 Amount: <code>{amount:.6f}</code>\n"
            f"💵 Price: <code>${price:,.4f}</code>\n"
            f"💰 Cost: <code>${cost:,.2f}</code>\n"
            f"🏷️ Fee: <code>${fee:.4f}</code>\n"
            f"💼 Remaining: <code>${remaining:,.2f}</code>\n"
            f"🎯 Strategy: <code>{_escape_html(strategy)}</code>\n"
            f"📝 Reason: <code>{_escape_html(reason)}</code>\n"
            f"🎲 Confidence: <code>{conf_pct:.1f}%</code>\n"
            f"{order_line}"
            f"{sep}"
        )
        await self._send(text, category="trade", force=True)

    async def send_trade_closed(
        self,
        mode: str,
        coin: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        strategy: str = "",
        reason: str = "",
        hold_duration: Optional[str] = None,
    ) -> None:
        """Send trade closed notification."""
        emoji = "✅" if pnl >= 0 else "❌"
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        sep = _build_separator()

        duration_line = (
            f"⏱️ Duration: <code>{hold_duration}</code>\n"
            if hold_duration else ""
        )

        text = (
            f"{emoji} <b>TRADE CLOSED</b>\n"
            f"{sep}\n"
            f"📊 Mode: <code>{_escape_html(mode)}</code>\n"
            f"🪙 Coin: <code>{_escape_html(coin)}</code>\n"
            f"📥 Entry: <code>${entry_price:,.4f}</code>\n"
            f"📤 Exit: <code>${exit_price:,.4f}</code>\n"
            f"{pnl_emoji} PnL: <code>${pnl:+,.4f} ({pnl_pct:+.2f}%)</code>\n"
            f"🎯 Strategy: <code>{_escape_html(strategy)}</code>\n"
            f"📝 Reason: <code>{_escape_html(reason)}</code>\n"
            f"{duration_line}"
            f"{sep}"
        )
        await self._send(text, category="trade", force=True)

    # ═══════════════════════════════════════════════════════
    #  DECISION ENGINE REPORT
    # ═══════════════════════════════════════════════════════

    async def send_decision_report(
        self,
        coin: str,
        price: float,
        brains: List[Dict],
        votes_buy: int,
        votes_sell: int,
        votes_hold: int,
        weighted_buy: float,
        weighted_sell: float,
        final_signal: str,
        confidence: str,
        trade: bool,
    ) -> None:
        """Send 4-brain decision engine report."""
        brain_lines = []
        for b in brains:
            sig = b.get("signal", "HOLD")
            sig_emoji = {
                "BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"
            }.get(sig, "⚪")
            brain_lines.append(
                f"  {b.get('name', '?')}: {sig_emoji} {sig} "
                f"({b.get('confidence_pct', 0)}% "
                f"× {b.get('weight_pct', 0)}%)"
            )
        brains_str = "\n".join(brain_lines)

        sig_emoji = {
            "BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"
        }.get(final_signal, "⚪")
        trade_str = "✅ YES" if trade else "❌ NO"
        sep = _build_separator()

        text = (
            f"🧠 <b>4-BRAIN DECISION ENGINE</b>\n"
            f"{sep}\n"
            f"🪙 Coin: <code>{_escape_html(coin)}</code>\n"
            f"💵 Price: <code>${price:,.4f}</code>\n"
            f"\n"
            f"<b>Brain Signals:</b>\n"
            f"<code>{brains_str}</code>\n"
            f"\n"
            f"<b>Voting Summary:</b>\n"
            f"  🟢 Buy: {votes_buy} | 🔴 Sell: {votes_sell} "
            f"| ⚪ Hold: {votes_hold}\n"
            f"  📊 Weighted: Buy={weighted_buy:.1f} "
            f"| Sell={weighted_sell:.1f}\n"
            f"\n"
            f"<b>Final:</b> {sig_emoji} "
            f"<code>{final_signal}</code> | "
            f"Confidence: <code>{confidence}</code>\n"
            f"<b>Execute:</b> {trade_str}\n"
            f"{sep}"
        )
        await self._send(text, category="decision")

    # ═══════════════════════════════════════════════════════
    #  MARKET & REPORT NOTIFICATIONS
    # ═══════════════════════════════════════════════════════

    async def send_market_update(
        self,
        symbol: str,
        price: float,
        trend: str,
        rsi: float,
        regime: str = "",
        volatility: str = "",
        volume_24h: Optional[float] = None,
        change_24h: Optional[float] = None,
    ) -> None:
        """Send periodic market snapshot."""
        trend_emoji = {
            "bullish": "📈", "bearish": "📉", "sideways": "➡️"
        }.get(trend.lower(), "📊")
        sep = _build_separator()

        extra_lines = ""
        if volume_24h is not None:
            extra_lines += (
                f"📊 Volume 24h: <code>${volume_24h:,.0f}</code>\n"
            )
        if change_24h is not None:
            chg_emoji = "📈" if change_24h >= 0 else "📉"
            extra_lines += (
                f"{chg_emoji} Change 24h: "
                f"<code>{change_24h:+.2f}%</code>\n"
            )

        text = (
            f"📊 <b>MARKET UPDATE</b>\n"
            f"{sep}\n"
            f"🪙 <code>{_escape_html(symbol)}</code> | "
            f"💵 <code>${price:,.4f}</code>\n"
            f"{trend_emoji} Trend: "
            f"<code>{trend.capitalize()}</code>\n"
            f"📐 RSI: <code>{rsi:.1f}</code>\n"
            f"🔄 Regime: "
            f"<code>{regime.capitalize() if regime else 'N/A'}</code>\n"
            f"🌪️ Volatility: "
            f"<code>{volatility.capitalize() if volatility else 'N/A'}</code>\n"
            f"{extra_lines}"
            f"🕐 <code>{_utc_time_short()}</code>\n"
            f"{sep}"
        )
        await self._send(text, category="market_update")

    async def send_hourly_report(
        self,
        mode: str,
        balance: float,
        daily_pnl: float,
        trades_today: int,
        max_trades: int,
        open_positions: int,
        win_streak: int = 0,
        loss_streak: int = 0,
        coins: Optional[List[str]] = None,
        win_rate: Optional[float] = None,
        total_volume: Optional[float] = None,
    ) -> None:
        """Send hourly performance summary."""
        pnl_emoji = "📈" if daily_pnl >= 0 else "📉"
        coins_str = ", ".join(coins) if coins else "None"
        sep = _build_separator()

        extra_lines = ""
        if win_rate is not None:
            extra_lines += (
                f"🎯 Win Rate: <code>{win_rate:.1f}%</code>\n"
            )
        if total_volume is not None:
            extra_lines += (
                f"📊 Volume: <code>${total_volume:,.2f}</code>\n"
            )

        text = (
            f"⏰ <b>HOURLY REPORT</b>\n"
            f"{sep}\n"
            f"📊 Mode: <code>{_escape_html(mode)}</code>\n"
            f"💰 Balance: <code>${balance:,.2f}</code>\n"
            f"{pnl_emoji} Daily PnL: "
            f"<code>${daily_pnl:+,.2f}</code>\n"
            f"📈 Trades: <code>{trades_today}/{max_trades}</code>\n"
            f"📂 Open: <code>{open_positions}</code>\n"
            f"🏆 Win Streak: <code>{win_streak}</code>\n"
            f"💔 Loss Streak: <code>{loss_streak}</code>\n"
            f"{extra_lines}"
            f"🪙 Coins: <code>{_escape_html(coins_str)}</code>\n"
            f"🕐 <code>{_utc_time_short()}</code>\n"
            f"{sep}"
        )
        await self._send(text, category="hourly_report")

    # ═══════════════════════════════════════════════════════
    #  STATUS & ALERTS
    # ═══════════════════════════════════════════════════════

    async def send_status(
        self,
        running: bool,
        mode: str,
        balance: float,
        open_trades: int,
        total_trades_today: int,
        max_trades: int,
        pnl_today: float,
        coins: List[str],
        uptime: Optional[str] = None,
        scheduler_state: Optional[str] = None,
    ) -> None:
        """Send status update."""
        status_emoji = "🟢" if running else "🔴"
        status_text = "RUNNING" if running else "STOPPED"
        pnl_emoji = "📈" if pnl_today >= 0 else "📉"
        coins_str = ", ".join(coins) if coins else "None"
        sep = _build_separator()

        extra_lines = ""
        if uptime:
            extra_lines += f"⏱️ Uptime: <code>{uptime}</code>\n"
        if scheduler_state:
            extra_lines += (
                f"📋 Scheduler: <code>{scheduler_state}</code>\n"
            )

        text = (
            f"{status_emoji} <b>BOT STATUS: {status_text}</b>\n"
            f"{sep}\n"
            f"📊 Mode: <code>{_escape_html(mode)}</code>\n"
            f"💰 Balance: <code>${balance:,.2f}</code>\n"
            f"📂 Open Trades: <code>{open_trades}</code>\n"
            f"📈 Trades Today: "
            f"<code>{total_trades_today}/{max_trades}</code>\n"
            f"{pnl_emoji} PnL Today: "
            f"<code>${pnl_today:+,.2f}</code>\n"
            f"🪙 Coins: <code>{_escape_html(coins_str)}</code>\n"
            f"{extra_lines}"
            f"🕐 <code>{_utc_time_short()}</code>\n"
            f"{sep}"
        )
        await self._send(text, category="status", force=True)

    async def send_error(
        self,
        context: str = "",
        error: str = "",
    ) -> None:
        """Send error alert."""
        sep = _build_separator()
        text = (
            f"⚠️ <b>ERROR ALERT</b>\n"
            f"{sep}\n"
            f"📍 Context: "
            f"<code>{_escape_html(context)}</code>\n"
            f"❌ Error: "
            f"<code>{_escape_html(str(error)[:500])}</code>\n"
            f"🕐 <code>{_utc_time_short()}</code>\n"
            f"{sep}"
        )
        await self._send(text, category="error", force=True)

    async def send_kill_switch(
        self,
        reason: str,
        loss_pct: float,
        current_balance: Optional[float] = None,
    ) -> None:
        """Send kill switch activation alert."""
        sep = _build_separator()
        bal_line = ""
        if current_balance is not None:
            bal_line = (
                f"💰 Balance: <code>${current_balance:,.2f}</code>\n"
            )

        text = (
            f"🚨 <b>KILL SWITCH ACTIVATED</b>\n"
            f"{sep}\n"
            f"📝 Reason: <code>{_escape_html(reason)}</code>\n"
            f"📉 Loss: <code>{loss_pct:.2f}%</code>\n"
            f"{bal_line}"
            f"🕐 <code>{_utc_time_short()}</code>\n"
            f"{sep}\n"
            f"⛔ All trading has been halted!\n"
            f"Use /resume to restart trading."
        )
        await self._send(text, category="kill_switch", force=True)

    async def send_position_update(
        self,
        coin: str,
        side: str,
        entry_price: float,
        current_price: float,
        unrealized_pnl: float,
        unrealized_pnl_pct: float,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> None:
        """Send position update notification."""
        pnl_emoji = "📈" if unrealized_pnl >= 0 else "📉"
        side_emoji = "🟢" if side.upper() == "BUY" else "🔴"
        sep = _build_separator()

        sl_line = ""
        tp_line = ""
        if stop_loss is not None:
            sl_line = (
                f"🛑 Stop Loss: <code>${stop_loss:,.4f}</code>\n"
            )
        if take_profit is not None:
            tp_line = (
                f"🎯 Take Profit: <code>${take_profit:,.4f}</code>\n"
            )

        text = (
            f"📊 <b>POSITION UPDATE</b>\n"
            f"{sep}\n"
            f"🪙 {_escape_html(coin)} | "
            f"{side_emoji} <code>{side.upper()}</code>\n"
            f"📥 Entry: <code>${entry_price:,.4f}</code>\n"
            f"💵 Current: <code>${current_price:,.4f}</code>\n"
            f"{pnl_emoji} PnL: <code>${unrealized_pnl:+,.4f} "
            f"({unrealized_pnl_pct:+.2f}%)</code>\n"
            f"{sl_line}{tp_line}"
            f"🕐 <code>{_utc_time_short()}</code>\n"
            f"{sep}"
        )
        await self._send(text, category="position")

    async def send_custom(
        self,
        message: str,
        parse_mode: Optional[str] = None,
    ) -> None:
        """Send a custom message."""
        await self._send(
            message,
            parse_mode=parse_mode,
            category="custom",
            force=True,
        )

    # ═══════════════════════════════════════════════════════
    #  STATISTICS
    # ═══════════════════════════════════════════════════════

    def get_stats(self) -> Dict[str, Any]:
        """Get notifier delivery statistics."""
        uptime = 0.0
        if self._stats.started_at:
            uptime = (
                get_utc_now() - self._stats.started_at
            ).total_seconds()

        return {
            "enabled": self._enabled,
            "messages_sent": self._stats.messages_sent,
            "messages_failed": self._stats.messages_failed,
            "total_retries": self._stats.total_retries,
            "total_chars_sent": self._stats.total_chars_sent,
            "errors_by_type": dict(self._stats.errors_by_type),
            "last_send": (
                self._stats.last_send_time.strftime("%H:%M:%S")
                if self._stats.last_send_time
                else None
            ),
            "last_error": self._stats.last_error or None,
            "uptime": format_duration(uptime),
            "chat_id": (
                self.chat_id[:4] + "..."
                if self.chat_id
                else ""
            ),
            "delivery_rate_pct": (
                round(
                    self._stats.messages_sent
                    / max(
                        1,
                        self._stats.messages_sent
                        + self._stats.messages_failed,
                    )
                    * 100,
                    1,
                )
            ),
        }

    def get_recent_messages(
        self, last_n: int = 10
    ) -> List[Dict[str, Any]]:
        """Get recent message history."""
        records = list(self._history)[-last_n:]
        return [
            {
                "time": r.timestamp.strftime("%H:%M:%S"),
                "success": r.success,
                "length": r.length,
                "retries": r.retry_count,
                "category": r.category,
                "error": r.error,
            }
            for r in records
        ]


# ═════════════════════════════════════════════════════════════════
#  HELPER: CLEAR WEBHOOK & PENDING UPDATES
# ═════════════════════════════════════════════════════════════════

async def _clear_telegram_state(bot: Bot) -> bool:
    """
    Clear any existing webhook and pending updates.
    
    This helps resolve conflicts by ensuring a clean state.
    
    Returns:
        True if successful, False otherwise
    """
    try:
        # Delete any existing webhook
        await bot.delete_webhook(drop_pending_updates=True)
        logger.debug("Cleared Telegram webhook and pending updates")
        
        # Small delay to let Telegram process
        await asyncio.sleep(1)
        
        return True
    except Exception as e:
        logger.warning(f"Failed to clear Telegram state: {e}")
        return False


# ═════════════════════════════════════════════════════════════════
#  TELEGRAM BOT STARTUP
# ═════════════════════════════════════════════════════════════════

async def start_telegram_bot(
    controller,
    drop_pending_updates: bool = True,
) -> Optional[Any]:
    """
    Initialize and start the Telegram bot.

    Includes:
    - Webhook cleanup before polling
    - Conflict detection with graceful degradation
    - Keepalive loop and proactive alert dispatch
    - Runs until cancelled (SIGINT / scheduler shutdown)

    Args:
        controller: BotController instance
        drop_pending_updates: Ignore messages received while offline

    Returns:
        Application instance (stored on controller._telegram_app)
    """
    if not TELEGRAM_AVAILABLE:
        logger.error("Telegram library not available")
        return None

    # Get credentials
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        logger.error("TELEGRAM_BOT_TOKEN not set in .env")
        return None
    if not chat_id:
        logger.error("TELEGRAM_CHAT_ID not set in .env")
        return None

    app = None
    polling_enabled = True

    try:
        # ── Build application ─────────────────────────────────
        app = Application.builder().token(token).build()

        # ── Clear any existing webhook/state ──────────────────
        logger.info("🔄 Clearing Telegram webhook and pending updates...")
        await _clear_telegram_state(app.bot)

        # ── Create notifier ───────────────────────────────────
        notifier = TelegramNotifier(bot=app.bot, chat_id=chat_id, enabled=True)
        controller.notifier = notifier
        controller._telegram_app = app

        # ── Register commands ─────────────────────────────────
        from app.tg.commands import setup_commands
        setup_commands(app, controller, chat_id)

        # ── Error handler ─────────────────────────────────────
        async def _error_handler(update, context):
            """Global error handler for telegram handlers."""
            err_str = str(context.error)
            
            # Suppress Conflict error spam
            if "Conflict" in err_str and "getUpdates" in err_str:
                logger.debug("Telegram Conflict in handler (suppressed)")
                return
            
            logger.error(
                f"Telegram handler error: {context.error}",
                exc_info=context.error,
            )
            if update and update.effective_chat:
                try:
                    await context.bot.send_message(
                        chat_id=update.effective_chat.id,
                        text=(
                            "⚠️ An error occurred processing "
                            "your command. Check logs."
                        ),
                    )
                except Exception:
                    pass

        app.add_error_handler(_error_handler)

        # ── Initialize and start ──────────────────────────────
        await app.initialize()
        await app.start()
        
        # ── Start polling with conflict retry logic ───────────
        for attempt in range(MAX_CONFLICT_RETRIES):
            try:
                await app.updater.start_polling(
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )
                logger.debug("Telegram polling started successfully")
                break
                
            except Exception as e:
                err_str = str(e)
                is_conflict = "Conflict" in err_str and "getUpdates" in err_str
                
                if is_conflict:
                    if attempt < MAX_CONFLICT_RETRIES - 1:
                        wait_time = CONFLICT_WAIT_TIME * (attempt + 1)
                        logger.warning(
                            f"⚠️ Telegram Conflict detected (attempt {attempt + 1}/{MAX_CONFLICT_RETRIES}). "
                            f"Another instance may be running. "
                            f"Waiting {wait_time}s for it to release..."
                        )
                        await asyncio.sleep(wait_time)
                        
                        # Try to clear state again
                        await _clear_telegram_state(app.bot)
                    else:
                        # Max retries - disable polling but continue
                        logger.warning(
                            "⚠️ TELEGRAM CONFLICT: Could not start polling after "
                            f"{MAX_CONFLICT_RETRIES} attempts.\n"
                            "   Another bot instance is using this token.\n"
                            "   Telegram notifications will be DISABLED.\n"
                            "   Trading bot will continue without Telegram.\n"
                            "\n"
                            "   To fix: Stop all other bot instances, wait 30s, restart."
                        )
                        polling_enabled = False
                        notifier.disable("Conflict - another instance is polling")
                        break
                else:
                    # Non-conflict error - raise it
                    raise

        # Mask chat ID in logs
        masked_id = (
            chat_id[:4] + "..." + chat_id[-2:]
            if len(chat_id) > 6
            else chat_id[:3] + "..."
        )
        
        if polling_enabled:
            logger.info(f"✅ Telegram bot started | chat={masked_id}")
        else:
            logger.warning(f"⚠️ Telegram bot started WITHOUT polling | chat={masked_id}")

        # ── Keepalive + alert dispatch loop ───────────────────
        try:
            alert_check_counter = 0
            
            while True:
                await asyncio.sleep(KEEPALIVE_INTERVAL)

                # Dispatch pending critical alerts (only if notifier enabled)
                alert_check_counter += KEEPALIVE_INTERVAL
                if alert_check_counter >= ALERT_CHECK_INTERVAL:
                    alert_check_counter = 0
                    if notifier.enabled:
                        await _dispatch_pending_alerts(notifier)

        except asyncio.CancelledError:
            logger.info("Telegram keepalive cancelled — stopping bot")

        return app

    except Exception as e:
        err_str = str(e)
        is_conflict = "Conflict" in err_str and "getUpdates" in err_str
        
        if is_conflict:
            logger.warning(
                "⚠️ TELEGRAM CONFLICT ERROR during startup!\n"
                "   Another bot instance is using the same Telegram token.\n"
                "   Trading bot will continue WITHOUT Telegram notifications.\n"
                "\n"
                "   To fix:\n"
                "   1. Stop all other bot instances (check cloud servers, other terminals)\n"
                "   2. Run: pkill -f 'python.*app.main'\n"
                "   3. Wait 30 seconds\n"
                "   4. Restart the bot\n"
            )
            # Return app anyway so trading continues
            if app and hasattr(controller, 'notifier') and controller.notifier:
                controller.notifier.disable("Startup conflict")
            return app
        else:
            logger.exception(f"Telegram bot failed to start: {e}")
            raise


async def _dispatch_pending_alerts(
    notifier: TelegramNotifier,
) -> None:
    """
    Dispatch pending critical alerts from the logger buffer.

    Drains get_pending_alerts() and sends each as a Telegram error.
    """
    if not notifier.enabled:
        return
        
    try:
        alerts = get_pending_alerts()
        if not alerts:
            return

        for alert in alerts[:5]:  # Max 5 per cycle
            msg = (
                f"🚨 <b>SYSTEM ALERT</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏰ {alert.get('ts', 'N/A')}\n"
                f"📍 Level: <code>{alert.get('level', 'ERROR')}</code>\n"
                f"📝 Logger: <code>"
                f"{_escape_html(alert.get('logger', 'unknown'))}</code>\n"
                f"❌ <code>"
                f"{_escape_html(alert.get('message', 'No message')[:500])}"
                f"</code>\n"
                f"━━━━━━━━━━━━━━━━━━━━━"
            )
            await notifier.send_custom(msg)
            await asyncio.sleep(0.5)  # Rate limit

        if len(alerts) > 5:
            await notifier.send_custom(
                f"⚠️ +{len(alerts) - 5} more alerts suppressed. "
                f"Check logs."
            )

    except Exception as e:
        logger.error(f"Failed to dispatch alerts: {e}")


# ═════════════════════════════════════════════════════════════════
#  TELEGRAM BOT SHUTDOWN
# ═════════════════════════════════════════════════════════════════

async def stop_telegram_bot(app: Any) -> None:
    """
    Gracefully stop the Telegram bot.

    Called from main.py finally block:
        telegram_app = getattr(controller, '_telegram_app', None)
        if telegram_app:
            await stop_telegram_bot(telegram_app)
    """
    if not app:
        return

    try:
        # Stop polling first
        if hasattr(app, "updater") and app.updater:
            if app.updater.running:
                await app.updater.stop()
                logger.debug("Telegram updater stopped")

        # Stop application
        if hasattr(app, 'running') and app.running:
            await app.stop()
            logger.debug("Telegram application stopped")

        # Shutdown
        await app.shutdown()
        logger.info("✅ Telegram bot stopped gracefully")

    except Exception as e:
        # Don't raise - just log
        logger.warning(f"Error stopping Telegram bot (non-critical): {e}")