# app/telegram/bot.py

import os
import asyncio
from functools import wraps
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .commands import (
    start_bot_cmd,
    stop_bot_cmd,
    set_trades_cmd,
    status_cmd,
    panic_stop_cmd,
)
from .auth import is_authorized
from app.utils.logger import get_logger

load_dotenv()
logger = get_logger(__name__)


# =========================
# TELEGRAM NOTIFIER
# =========================
class TelegramNotifier:
    def __init__(self, app, controller):
        self.app = app
        self.controller = controller

    async def send(self, message: str):
        chat_id = self.controller.state.get("telegram_chat_id")
        if not chat_id:
            return

        try:
            await self.app.bot.send_message(
                chat_id=chat_id,
                text=message,
            )
        except Exception as e:
            logger.error(f"❌ Failed to send message: {e}")


# =========================
# AUTH DECORATOR
# =========================
def auth_required(handler):
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_user:
            return

        user_id = update.effective_user.id

        if not is_authorized(update):
            if update.message:
                await update.message.reply_text(
                    f"⛔ Unauthorized\n\n"
                    f"Your User ID: {user_id}\n\n"
                    f"Add this ID to TELEGRAM_ALLOWED_USERS in .env"
                )
            return

        # Save chat ID in state
        if update.effective_chat:
            context.bot_data["chat_id"] = update.effective_chat.id

        return await handler(update, context)

    return wrapped


# =========================
# HANDLER WRAPPER
# =========================
def make_handler(func, controller):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
        # Save chat id to state
        if update.effective_chat:
            controller.state.set(
                "telegram_chat_id",
                update.effective_chat.id,
            )

        try:
            return await func(update, context, controller)
        except Exception as e:
            logger.exception(f"❌ Error in {func.__name__}: {e}")
            if update.message:
                await update.message.reply_text("❌ Something went wrong.")

    return auth_required(handler)


# =========================
# ERROR HANDLER
# =========================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"❌ Telegram error: {context.error}")


# =========================
# POST INIT
# =========================
async def post_init(app):
    logger.info("📡 Telegram bot initialized")


# =========================
# BOT STARTER
# =========================
async def start_telegram_bot(controller):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("❌ TELEGRAM_BOT_TOKEN missing")

    logger.info("🔧 Building Telegram application...")

    app = (
        ApplicationBuilder()
        .token(token)
        .post_init(post_init)
        .build()
    )

    # 🔥 Inject Notifier Into Controller
    notifier = TelegramNotifier(app, controller)
    controller.notifier = notifier

    # Register command handlers
    commands = [
        ("start_bot", start_bot_cmd),
        ("stop_bot", stop_bot_cmd),
        ("set_trades", set_trades_cmd),
        ("status", status_cmd),
        ("panic_stop", panic_stop_cmd),
    ]

    for cmd_name, cmd_func in commands:
        app.add_handler(
            CommandHandler(cmd_name, make_handler(cmd_func, controller))
        )
        logger.info(f"📝 Registered /{cmd_name}")

    app.add_error_handler(error_handler)

    # Proper async startup
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("🚀 Telegram bot is LIVE and polling...")

    # Keep running forever
    await asyncio.Event().wait()