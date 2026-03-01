# app/tg/bot.py

"""
Telegram Bot Integration — Production Grade

Provides:
- Async Telegram bot using python-telegram-bot v20+
- Command handling via TelegramCommands
- Authentication via TelegramAuth
- Notification system (Notifier class)
- Graceful startup with drop_pending_updates
- Error handling and logging

Usage:
    # In main.py:
    await start_telegram_bot(controller, drop_pending_updates=True)
"""

import asyncio
from datetime import datetime
from typing import Optional, List, Dict, Any

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Try importing telegram library ────────────────────────────────
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
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    logger.warning(
        "⚠️ python-telegram-bot not installed. "
        "Install with: pip install python-telegram-bot>=20.0"
    )

import os


# ═════════════════════════════════════════════════════════════════
#  NOTIFIER CLASS
# ═════════════════════════════════════════════════════════════════

class TelegramNotifier:
    """
    Async notification sender for trading events.

    Used by controller to send:
    - Bot started/stopped messages
    - Trade executed/closed notifications
    - Decision engine reports
    - Status updates
    - Error alerts
    - Kill switch activations
    """

    def __init__(self, bot: "Bot", chat_id: str):
        self.bot = bot
        self.chat_id = chat_id

    async def _send(self, text: str, parse_mode: str = ParseMode.HTML) -> bool:
        """Send a message with error handling."""
        try:
            await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=parse_mode,
            )
            return True
        except Exception as e:
            logger.error(f"❌ Telegram send failed: {e}")
            return False

    # ═══════════════════════════════════════════════════════
    #  LIFECYCLE NOTIFICATIONS
    # ═══════════════════════════════════════════════════════

    async def send_bot_started(
        self,
        mode: str,
        balance: float,
        coins: List[str],
        interval: int,
        timestamp: datetime,
    ):
        """Send bot started notification."""
        coins_str = ", ".join(coins) if coins else "None"
        text = (
            "🤖 <b>TRADING BOT STARTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Mode: <code>{mode}</code>\n"
            f"💰 Balance: <code>${balance:.2f}</code>\n"
            f"🪙 Coins: <code>{coins_str}</code>\n"
            f"⏱️ Interval: <code>{interval}s</code>\n"
            f"🕐 Time: <code>{timestamp.strftime('%Y-%m-%d %H:%M:%S')} UTC</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Bot is now actively trading!"
        )
        await self._send(text)

    async def send_bot_stopped(self, reason: str = "User request"):
        """Send bot stopped notification."""
        text = (
            "🛑 <b>TRADING BOT STOPPED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 Reason: <code>{reason}</code>\n"
            f"🕐 Time: <code>{datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⏸️ Trading has been paused."
        )
        await self._send(text)

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
        fee: float,
        remaining: float,
        strategy: str,
        reason: str,
        confidence: float,
    ):
        """Send trade executed notification."""
        emoji = "🟢" if side == "BUY" else "🔴"
        conf_pct = confidence * 100 if confidence <= 1 else confidence

        text = (
            f"{emoji} <b>TRADE EXECUTED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Mode: <code>{mode}</code>\n"
            f"🪙 Coin: <code>{coin}</code>\n"
            f"📈 Side: <code>{side}</code>\n"
            f"📦 Amount: <code>{amount:.6f}</code>\n"
            f"💵 Price: <code>${price:.2f}</code>\n"
            f"💰 Cost: <code>${cost:.2f}</code>\n"
            f"🏷️ Fee: <code>${fee:.4f}</code>\n"
            f"💼 Remaining: <code>${remaining:.2f}</code>\n"
            f"🎯 Strategy: <code>{strategy}</code>\n"
            f"📝 Reason: <code>{reason}</code>\n"
            f"🎲 Confidence: <code>{conf_pct:.1f}%</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await self._send(text)

    async def send_trade_closed(
        self,
        mode: str,
        coin: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        strategy: str,
        reason: str,
    ):
        """Send trade closed notification."""
        emoji = "✅" if pnl >= 0 else "❌"
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        text = (
            f"{emoji} <b>TRADE CLOSED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Mode: <code>{mode}</code>\n"
            f"🪙 Coin: <code>{coin}</code>\n"
            f"📥 Entry: <code>${entry_price:.2f}</code>\n"
            f"📤 Exit: <code>${exit_price:.2f}</code>\n"
            f"{pnl_emoji} PnL: <code>${pnl:+.4f} ({pnl_pct:+.2f}%)</code>\n"
            f"🎯 Strategy: <code>{strategy}</code>\n"
            f"📝 Reason: <code>{reason}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await self._send(text)

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
    ):
        """Send 4-brain decision engine report."""
        # Build brain details
        brain_lines = []
        for b in brains:
            signal_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(
                b["signal"], "⚪"
            )
            brain_lines.append(
                f"  {b['name']}: {signal_emoji} {b['signal']} "
                f"({b['confidence_pct']}% × {b['weight_pct']}%)"
            )
        brains_str = "\n".join(brain_lines)

        signal_emoji = {"BUY": "🟢", "SELL": "🔴", "HOLD": "⚪"}.get(
            final_signal, "⚪"
        )
        trade_str = "✅ YES" if trade else "❌ NO"

        text = (
            "🧠 <b>4-BRAIN DECISION ENGINE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🪙 Coin: <code>{coin}</code>\n"
            f"💵 Price: <code>${price:.2f}</code>\n"
            "\n"
            "<b>Brain Signals:</b>\n"
            f"<code>{brains_str}</code>\n"
            "\n"
            "<b>Voting Summary:</b>\n"
            f"  🟢 Buy: {votes_buy} | 🔴 Sell: {votes_sell} | ⚪ Hold: {votes_hold}\n"
            f"  📊 Weighted: Buy={weighted_buy:.1f} | Sell={weighted_sell:.1f}\n"
            "\n"
            f"<b>Final Decision:</b> {signal_emoji} <code>{final_signal}</code>\n"
            f"<b>Confidence:</b> <code>{confidence}</code>\n"
            f"<b>Execute Trade:</b> {trade_str}\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await self._send(text)

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
    ):
        """Send status update."""
        status_emoji = "🟢" if running else "🔴"
        status_text = "RUNNING" if running else "STOPPED"
        pnl_emoji = "📈" if pnl_today >= 0 else "📉"
        coins_str = ", ".join(coins) if coins else "None"

        text = (
            f"{status_emoji} <b>BOT STATUS: {status_text}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Mode: <code>{mode}</code>\n"
            f"💰 Balance: <code>${balance:.2f}</code>\n"
            f"📂 Open Trades: <code>{open_trades}</code>\n"
            f"📈 Trades Today: <code>{total_trades_today}/{max_trades}</code>\n"
            f"{pnl_emoji} PnL Today: <code>${pnl_today:+.2f}</code>\n"
            f"🪙 Coins: <code>{coins_str}</code>\n"
            f"🕐 Time: <code>{datetime.utcnow().strftime('%H:%M:%S')} UTC</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await self._send(text)

    async def send_error(self, context: str, error: str):
        """Send error alert."""
        text = (
            "⚠️ <b>ERROR ALERT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📍 Context: <code>{context}</code>\n"
            f"❌ Error: <code>{error[:500]}</code>\n"
            f"🕐 Time: <code>{datetime.utcnow().strftime('%H:%M:%S')} UTC</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await self._send(text)

    async def send_kill_switch(self, reason: str, loss_pct: float):
        """Send kill switch activation alert."""
        text = (
            "🚨 <b>KILL SWITCH ACTIVATED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📝 Reason: <code>{reason}</code>\n"
            f"📉 Loss: <code>{loss_pct:.2f}%</code>\n"
            f"🕐 Time: <code>{datetime.utcnow().strftime('%H:%M:%S')} UTC</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⛔ All trading has been halted!\n"
            "Use /resume to restart trading."
        )
        await self._send(text)

    async def send_custom(self, message: str):
        """Send a custom message."""
        await self._send(message)


# ═════════════════════════════════════════════════════════════════
#  TELEGRAM BOT SETUP
# ═════════════════════════════════════════════════════════════════

async def start_telegram_bot(
    controller,
    drop_pending_updates: bool = True,
) -> Optional[Application]:
    """
    Initialize and start the Telegram bot.

    Args:
        controller: BotController instance
        drop_pending_updates: If True, ignore messages received while bot was offline

    Returns:
        Application instance if successful, None otherwise
    """
    if not TELEGRAM_AVAILABLE:
        logger.error("❌ Telegram library not available")
        return None

    # Get credentials
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not token:
        logger.error("❌ TELEGRAM_BOT_TOKEN not set")
        return None

    if not chat_id:
        logger.error("❌ TELEGRAM_CHAT_ID not set")
        return None

    try:
        # Create application
        app = (
            Application.builder()
            .token(token)
            .build()
        )

        # Create notifier and attach to controller
        notifier = TelegramNotifier(bot=app.bot, chat_id=chat_id)
        controller.notifier = notifier

        # Import and setup commands
        from app.tg.commands import setup_commands
        setup_commands(app, controller, chat_id)

        # Initialize application
        await app.initialize()

        # Start polling (non-blocking)
        await app.start()
        await app.updater.start_polling(
            drop_pending_updates=drop_pending_updates,
            allowed_updates=Update.ALL_TYPES,
        )

        logger.info(
            f"✅ Telegram bot started | "
            f"Chat ID: {chat_id[:4]}...{chat_id[-4:]}"
        )

        return app

    except Exception as e:
        error_str = str(e)
        
        # Handle "Conflict" error (multiple instances)
        if "Conflict" in error_str and "getUpdates" in error_str:
            logger.error(
                "❌ TELEGRAM CONFLICT ERROR!\n"
                "   Another bot instance is using the same token.\n"
                "   This usually happens when running multiple instances.\n\n"
                "   SOLUTION:\n"
                "   1. Kill ALL bot processes: pkill -9 -f 'python.*app.main'\n"
                "   2. Wait 5 seconds\n"
                "   3. Delete lock file: rm ~/.bot_lock\n"
                "   4. Restart bot\n\n"
                f"   Error: {e}"
            )
        else:
            logger.exception(f"❌ Telegram bot failed to start: {e}")
        
        raise


async def stop_telegram_bot(app: Application):
    """Gracefully stop the Telegram bot."""
    if not app:
        return

    try:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        logger.info("✅ Telegram bot stopped")
    except Exception as e:
        logger.error(f"❌ Error stopping Telegram bot: {e}")