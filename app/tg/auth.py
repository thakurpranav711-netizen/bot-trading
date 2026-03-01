# app/tg/auth.py

"""
Telegram Authentication — Production Grade

Provides decorator for command authentication.
Only allows commands from authorized chat ID.
"""

import os
import functools
from typing import Callable

from app.utils.logger import get_logger

logger = get_logger(__name__)

# ── Try importing telegram ────────────────────────────────────────
try:
    from telegram import Update
    from telegram.ext import ContextTypes
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False


def require_auth(func: Callable) -> Callable:
    """
    Decorator that requires command sender to be authorized.

    Checks if the message chat ID matches TELEGRAM_CHAT_ID.
    """
    @functools.wraps(func)
    async def wrapper(
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        *args,
        **kwargs
    ):
        if not update.effective_chat:
            logger.warning("⚠️ No chat in update")
            return

        chat_id = str(update.effective_chat.id)

        # Get authorized chat ID from context or env
        authorized_id = context.bot_data.get("chat_id", "")
        if not authorized_id:
            authorized_id = os.getenv("TELEGRAM_CHAT_ID", "")

        if not authorized_id:
            logger.error("❌ TELEGRAM_CHAT_ID not configured")
            await update.message.reply_text(
                "❌ Bot not configured. Missing TELEGRAM_CHAT_ID."
            )
            return

        if chat_id != authorized_id:
            logger.warning(
                f"⚠️ Unauthorized access attempt | "
                f"Chat: {chat_id} | Expected: {authorized_id}"
            )
            await update.message.reply_text(
                "⛔ Unauthorized. This bot is private."
            )
            return

        # Authorized — proceed
        return await func(update, context, *args, **kwargs)

    return wrapper


def get_authorized_chat_id() -> str:
    """Get authorized chat ID from environment."""
    return os.getenv("TELEGRAM_CHAT_ID", "").strip()


def is_authorized(chat_id: str) -> bool:
    """Check if a chat ID is authorized."""
    authorized = get_authorized_chat_id()
    return chat_id == authorized and bool(authorized)