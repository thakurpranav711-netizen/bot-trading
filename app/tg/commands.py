from telegram import Update
from telegram.ext import ContextTypes

from ..utils.logger import get_logger

logger = get_logger(__name__)


# -------------------------
# Helper: safe reply
# -------------------------
async def safe_reply(update: Update, text: str, **kwargs):
    logger.info(f"💬 Attempting to reply: {text[:50]}...")
    if update.message:
        logger.info(f"✅ Found update.message, sending reply")
        await update.message.reply_text(text, **kwargs)
    elif update.effective_message:
        logger.info(f"✅ Found update.effective_message, sending reply")
        await update.effective_message.reply_text(text, **kwargs)
    else:
        logger.error("❌ No message object found! Available: message={}, effective_message={}".format(
            update.message, update.effective_message
        ))


# -------------------------
# /start_bot
# -------------------------
async def start_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, controller):
    try:
        if not update.effective_user:
            logger.error("❌ No effective_user in update")
            return
            
        logger.info(f"👤 /start_bot command received from user {update.effective_user.id}")
        
        # 🔥 SAVE CHAT ID (MOST IMPORTANT LINE)
        if not update.effective_chat:
            logger.error("❌ No effective_chat in update")
            await safe_reply(update, "❌ No chat available")
            return
            
        chat_id = update.effective_chat.id
        logger.info(f"📌 Chat ID detected: {chat_id}")
        controller.state.set("telegram_chat_id", chat_id)
        logger.info(f"📌 Telegram chat_id saved: {chat_id}")

        started = controller.start_bot()
        if started:
            msg = "✅ Bot started.\n🤖 Paper trading is LIVE.\n📡 Notifications ENABLED."
            logger.info(f"Sending success message: {msg}")
            await safe_reply(update, msg)
        else:
            await safe_reply(update, "⚠️ Bot is already running.")
            
    except Exception as e:
        logger.exception(f"❌ Error in start_bot_cmd: {e}")
        try:
            await safe_reply(update, f"❌ Failed to start bot: {str(e)[:100]}")
        except Exception as reply_err:
            logger.error(f"❌ Failed to send error reply: {reply_err}")


# -------------------------
# /stop_bot
# -------------------------
async def stop_bot_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, controller):
    try:
        stopped = controller.stop_bot()
        if stopped:
            await safe_reply(update, "🛑 Bot stopped safely.")
        else:
            await safe_reply(update, "⚠️ Bot is already stopped.")
    except Exception as e:
        logger.exception("Error stopping bot")
        await safe_reply(update, f"❌ Failed to stop bot: {e}")


# -------------------------
# /panic_stop
# -------------------------
async def panic_stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, controller):
    try:
        controller.panic_stop()
        await safe_reply(update, "🚨 PANIC STOP ACTIVATED!\nBot halted immediately.")
    except Exception as e:
        logger.exception("Error during panic stop")
        await safe_reply(update, f"❌ Panic stop failed: {e}")


# -------------------------
# /set_trades <number>
# -------------------------
async def set_trades_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, controller):
    if not context.args:
        await safe_reply(update, "❌ Usage: /set_trades <1-100>")
        return

    try:
        trades = int(context.args[0])
        controller.set_max_trades(trades)
        await safe_reply(update, f"🔧 Max trades per day set to {trades}")
    except ValueError:
        await safe_reply(update, "❌ Invalid number")
    except Exception as e:
        logger.exception("Error setting trades")
        await safe_reply(update, f"❌ Failed to set trades: {e}")


# -------------------------
# /status
# -------------------------
async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE, controller):
    try:
        status = controller.get_status()

        msg = (
            "📊 *BOT STATUS (Paper Trading)*\n\n"
            f"🟢 Active: {status['bot_active']}\n"
            f"💰 Balance: ₹{round(status['balance'], 2)}\n"
            f"📈 Trades Today: {status['trades_done_today']}/{status['max_trades_per_day']}\n"
            f"📦 Positions: {len(status['open_positions'])}\n"
        )

        if status["open_positions"]:
            msg += "\n*Open Positions:*\n"
            for sym, pos in status["open_positions"].items():
                msg += f"- {sym}: {pos['quantity']} @ {pos['avg_price']}\n"

        await safe_reply(update, msg, parse_mode="Markdown")

    except Exception as e:
        logger.exception("Error fetching status")
        await safe_reply(update, f"❌ Failed to fetch status: {e}")