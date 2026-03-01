# app/tg/commands.py

"""
Telegram Command Handlers — Production Grade

Available commands:
    /start       - Welcome message
    /help        - Show available commands
    /status      - Get bot status
    /balance     - Check current balance
    /positions   - List open positions
    /trades      - Recent trade history
    /risk        - Risk management report
    /limits      - Trade limiter status
    /pause       - Pause trading
    /resume      - Resume trading (unlocks all locks)
    /kill        - Activate kill switch
    /unlock      - Full risk unlock
    /reset_risk  - Reset risk baseline
    /start_bot   - Alias for /resume
    /force_exit  - Force close all positions
    /force_cycle - Manually trigger a trading cycle
"""

from datetime import datetime
from typing import TYPE_CHECKING

from app.utils.logger import get_logger
from app.tg.auth import require_auth

logger = get_logger(__name__)

if TYPE_CHECKING:
    from telegram.ext import Application

# ── Try importing telegram ────────────────────────────────────────
try:
    from telegram import Update
    from telegram.ext import CommandHandler, ContextTypes
    from telegram.constants import ParseMode
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False


def setup_commands(app: "Application", controller, chat_id: str):
    """
    Register all command handlers.

    Args:
        app: Telegram Application instance
        controller: BotController instance
        chat_id: Authorized chat ID
    """
    if not TELEGRAM_AVAILABLE:
        logger.warning("⚠️ Telegram not available — commands not registered")
        return

    # Store references in app context
    app.bot_data["controller"] = controller
    app.bot_data["chat_id"] = chat_id

    # Register handlers
    handlers = [
        ("start", cmd_start),
        ("help", cmd_help),
        ("status", cmd_status),
        ("balance", cmd_balance),
        ("positions", cmd_positions),
        ("trades", cmd_trades),
        ("risk", cmd_risk),
        ("limits", cmd_limits),
        ("pause", cmd_pause),
        ("resume", cmd_resume),
        ("start_bot", cmd_resume),       # NEW: alias for /resume
        ("kill", cmd_kill),
        ("unlock", cmd_unlock),          # NEW: full risk unlock
        ("reset_risk", cmd_reset_risk),  # NEW: reset baseline
        ("force_exit", cmd_force_exit),
        ("force_cycle", cmd_force_cycle),
    ]

    for name, handler in handlers:
        app.add_handler(CommandHandler(name, handler))

    logger.info(f"📋 Registered {len(handlers)} Telegram commands")


# ═════════════════════════════════════════════════════════════════
#  HELPER
# ═════════════════════════════════════════════════════════════════

def _get_controller(context: ContextTypes.DEFAULT_TYPE):
    """Get controller from context."""
    return context.bot_data.get("controller")


def _get_chat_id(context: ContextTypes.DEFAULT_TYPE) -> str:
    """Get authorized chat ID from context."""
    return context.bot_data.get("chat_id", "")


async def _reply(update: Update, text: str):
    """Send reply with HTML parsing."""
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ═════════════════════════════════════════════════════════════════
#  BASIC COMMANDS
# ═════════════════════════════════════════════════════════════════

@require_auth
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Welcome message."""
    text = (
        "🤖 <b>4-Brain Trading Bot</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "Welcome! I'm your autonomous trading assistant.\n\n"
        "Use /help to see available commands.\n"
        "Use /status to check bot status."
    )
    await _reply(update, text)


@require_auth
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show available commands."""
    text = (
        "📋 <b>Available Commands</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "\n"
        "<b>📊 Information:</b>\n"
        "/status - Bot status overview\n"
        "/balance - Current balance\n"
        "/positions - Open positions\n"
        "/trades - Recent trade history\n"
        "/risk - Risk management report\n"
        "/limits - Trade limiter status\n"
        "\n"
        "<b>⚙️ Controls:</b>\n"
        "/pause - Pause trading\n"
        "/resume - Resume trading\n"
        "/start_bot - Same as /resume\n"
        "/kill - Emergency stop\n"
        "\n"
        "<b>🔓 Risk Management:</b>\n"
        "/unlock - Unlock risk locks\n"
        "/reset_risk - Accept losses & restart fresh\n"
        "\n"
        "<b>⚡ Actions:</b>\n"
        "/force_exit - Close all positions\n"
        "/force_cycle - Trigger trading cycle\n"
        "\n"
        "<b>ℹ️ Other:</b>\n"
        "/start - Welcome message\n"
        "/help - This help message\n"
        "━━━━━━━━━━━━━━━━━━━━━"
    )
    await _reply(update, text)


# ═════════════════════════════════════════════════════════════════
#  INFORMATION COMMANDS (FIXED)
# ═════════════════════════════════════════════════════════════════

@require_auth
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Get comprehensive bot status.
    """
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        status = controller.get_status()

        status_emoji = "🟢" if status["running"] else "🔴"
        status_text = "RUNNING" if status["running"] else "STOPPED"
        kill_status = "🚨 ACTIVE" if status["kill_switch_active"] else "✅ OFF"
        pnl = status.get("daily_pnl", 0)
        pnl_emoji = "📈" if pnl >= 0 else "📉"

        emergency_ack = status.get("emergency_acknowledged", False)
        emergency_text = "✅ Acknowledged" if emergency_ack else "—"

        text = (
            f"{status_emoji} <b>BOT STATUS: {status_text}</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Mode: <code>{status['mode']}</code>\n"
            f"💰 Balance: <code>${status['balance']:.2f}</code>\n"
            f"{pnl_emoji} Daily PnL: <code>${pnl:+.2f}</code>\n"
            f"📂 Open Positions: <code>{status['open_positions']}</code>\n"
            f"📈 Trades Today: <code>{status['trades_today']}</code>\n"
            f"🏆 Win Streak: <code>{status['win_streak']}</code>\n"
            f"💔 Loss Streak: <code>{status['loss_streak']}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🛑 Kill Switch: {kill_status}\n"
            f"🚨 Emergency: {emergency_text}\n"
            f"🕐 Time: <code>{datetime.utcnow().strftime('%H:%M:%S')} UTC</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )

        if status["kill_switch_active"] or not status["running"]:
            text += (
                "\n\n💡 <b>Bot is blocked!</b>\n"
                "Use /unlock to clear all locks\n"
                "Use /reset_risk to accept losses & restart"
            )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Status command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check current balance."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        balance = controller.state.get("balance", 0)
        initial = controller.state.get("initial_balance", 0)
        daily_pnl = controller.state.get("daily_pnl", 0)

        total_pnl = balance - initial if initial else 0
        total_pct = (total_pnl / initial * 100) if initial else 0

        text = (
            "💰 <b>BALANCE REPORT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"💵 Current: <code>${balance:.2f}</code>\n"
            f"🏦 Initial: <code>${initial:.2f}</code>\n"
            f"📊 Total PnL: <code>${total_pnl:+.2f} ({total_pct:+.2f}%)</code>\n"
            f"📈 Today: <code>${daily_pnl:+.2f}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Balance command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """List open positions."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        positions = controller.state.get_all_positions()

        if not positions:
            await _reply(update, "📂 No open positions")
            return

        lines = ["📂 <b>OPEN POSITIONS</b>\n━━━━━━━━━━━━━━━━━━━━━"]

        for symbol, pos in positions.items():
            entry = pos.get("entry_price", 0)
            qty = pos.get("quantity", 0)
            sl = pos.get("stop_loss", 0)
            tp = pos.get("take_profit", 0)
            action = pos.get("action", "BUY")

            lines.append(
                f"\n🪙 <b>{symbol}</b>\n"
                f"  📈 Side: <code>{action}</code>\n"
                f"  📦 Qty: <code>{qty:.6f}</code>\n"
                f"  💵 Entry: <code>${entry:.2f}</code>\n"
                f"  🛑 SL: <code>${sl:.2f}</code>\n"
                f"  🎯 TP: <code>${tp:.2f}</code>"
            )

        lines.append("\n━━━━━━━━━━━━━━━━━━━━━")
        await _reply(update, "\n".join(lines))

    except Exception as e:
        logger.exception(f"Positions command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_trades(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show recent trade history."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        history = controller.state.get("trade_history") or []
        recent = history[-5:]

        if not recent:
            await _reply(update, "📜 No trade history")
            return

        lines = ["📜 <b>RECENT TRADES</b>\n━━━━━━━━━━━━━━━━━━━━━"]

        for trade in reversed(recent):
            symbol = trade.get("symbol", "?")
            pnl = trade.get("pnl_amount", trade.get("pnl", 0))
            pnl_emoji = "✅" if pnl >= 0 else "❌"
            closed_at = trade.get("closed_at", trade.get("timestamp", ""))[:10]

            lines.append(
                f"\n{pnl_emoji} <b>{symbol}</b>\n"
                f"  💰 PnL: <code>${pnl:+.4f}</code>\n"
                f"  📅 Date: <code>{closed_at}</code>"
            )

        lines.append("\n━━━━━━━━━━━━━━━━━━━━━")
        await _reply(update, "\n".join(lines))

    except Exception as e:
        logger.exception(f"Trades command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show risk management report."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        report = controller.adaptive_risk.get_risk_report()

        # FIXED: Also show loss guard status
        lg_status = controller.loss_guard.get_status()
        can_trade = "✅ YES" if lg_status["can_trade"] else "❌ NO"
        block = lg_status.get("block_reason", "")
        emergency_ack = "✅" if lg_status.get("emergency_acknowledged") else "❌"

        text = (
            "🎯 <b>RISK MANAGEMENT REPORT</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔓 Can Trade: {can_trade}\n"
        )

        if block:
            text += f"⛔ Blocked: <code>{block}</code>\n"

        text += (
            f"🚨 Emergency Ack: {emergency_ack}\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 Current Risk: <code>{report['current_risk_pct']:.3f}%</code>\n"
            f"📏 Base Risk: <code>{report['base_risk_pct']:.2f}%</code>\n"
            f"📈 Win Rate: <code>{report['win_rate_pct']:.1f}%</code>\n"
            f"📊 Total Trades: <code>{report['total_trades']}</code>\n"
            f"🏆 Win Streak: <code>{report['win_streak']}</code>\n"
            f"💔 Loss Streak: <code>{report['loss_streak']}</code>\n"
            f"📉 Drawdown: <code>{report['drawdown_pct']:.2f}%</code>\n"
            f"📐 Kelly (capped): <code>{report['kelly_capped_pct']:.3f}%</code>\n"
            f"📈 Avg Win: <code>${report['avg_win']:.4f}</code>\n"
            f"📉 Avg Loss: <code>${report['avg_loss']:.4f}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Baseline: <code>${lg_status.get('initial_balance', 0):.2f}</code>\n"
            f"📊 Daily DD: <code>{lg_status.get('daily_drawdown_pct', 0):.2f}%</code> "
            f"(max {lg_status.get('daily_limit_pct', 5):.0f}%)\n"
            f"📊 Total DD: <code>{lg_status.get('total_drawdown_pct', 0):.2f}%</code> "
            f"(emergency {lg_status.get('emergency_limit_pct', 15):.0f}%)\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )

        # Add hints if blocked
        if not lg_status["can_trade"]:
            text += (
                "\n\n💡 <b>Trading is blocked!</b>\n"
                "/unlock — Clear all locks\n"
                "/reset_risk — Accept losses & restart"
            )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Risk command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_limits(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show trade limiter status."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        status = controller.trade_limiter.get_status()

        can_trade = "✅ YES" if status["can_trade"] else "❌ NO"
        cooldown = "🔴 ACTIVE" if status["rapid_cooldown_active"] else "🟢 OFF"

        text = (
            "📊 <b>TRADE LIMITER STATUS</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔓 Can Trade: {can_trade}\n"
            f"📈 Today: <code>{status['trades_today']}/{status['max_trades_per_day']}</code>\n"
            f"⏰ This Hour: <code>{status['trades_this_hour']}/{status['max_trades_per_hour']}</code>\n"
            f"⏳ Cooldown: {cooldown}\n"
            f"🕐 Min Interval: <code>{status['min_trade_interval_sec']}s</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━"
        )
        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Limits command error: {e}")
        await _reply(update, f"❌ Error: {e}")


# ═════════════════════════════════════════════════════════════════
#  CONTROL COMMANDS (FIXED)
# ═════════════════════════════════════════════════════════════════

@require_auth
async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pause trading."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        controller.pause()
        await _reply(
            update,
            "⏸️ Trading <b>PAUSED</b>\n\n"
            "Use /resume to continue."
        )
    except Exception as e:
        logger.exception(f"Pause command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Resume trading.

    FIXED: Now shows detailed unlock status.
    Also works as /start_bot alias.
    """
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        # Get state BEFORE resume for comparison
        was_kill_active = controller.kill_switch.is_active()
        was_bot_active = controller.state.get("bot_active", False)

        # Perform resume (now unlocks everything)
        controller.resume()

        # Build detailed response
        changes = []
        if was_kill_active:
            changes.append("🛑 Kill switch deactivated")
        if not was_bot_active:
            changes.append("▶️ Bot reactivated")

        if not changes:
            changes.append("ℹ️ Bot was already running")

        changes_text = "\n".join(changes)

        balance = controller.state.get("balance", 0)
        initial = controller.state.get("initial_balance", 0)

        text = (
            "▶️ <b>TRADING RESUMED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"{changes_text}\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance: <code>${balance:.2f}</code>\n"
            f"🏦 Baseline: <code>${initial:.2f}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Bot is now monitoring!\n\n"
            "💡 If bot re-locks, use /reset_risk\n"
            "to accept past losses and start fresh."
        )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Resume command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_kill(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Activate kill switch."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        args = context.args
        reason = " ".join(args) if args else "Manual Telegram trigger"

        controller.kill_switch.activate(
            reason=reason,
            source="telegram",
            auto_resume_minutes=None,
        )

        await _reply(
            update,
            "🚨 <b>KILL SWITCH ACTIVATED</b>\n\n"
            f"Reason: {reason}\n\n"
            "All trading has been halted.\n"
            "Use /unlock to clear all locks.\n"
            "Use /resume to restart trading."
        )
    except Exception as e:
        logger.exception(f"Kill command error: {e}")
        await _reply(update, f"❌ Error: {e}")


# ═════════════════════════════════════════════════════════════════
#  NEW RISK COMMANDS
# ═════════════════════════════════════════════════════════════════

@require_auth
async def cmd_unlock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Full risk system unlock.

    Clears ALL locks:
    - kill_switch → OFF
    - loss_guard cooldown → cleared
    - consecutive_losses → reset to 0
    - emergency_acknowledged → True (prevents re-lock)
    - bot_active → True

    Does NOT reset initial_balance.
    Use /reset_risk for that.
    """
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        result = controller.unlock_risk(source="telegram")

        balance = controller.state.get("balance", 0)
        initial = controller.state.get("initial_balance", 0)

        # Calculate when emergency would re-trigger
        if initial > 0:
            emergency_pct = controller.loss_guard.emergency_drawdown_pct
            emergency_threshold = initial * (1 - emergency_pct)
            emergency_text = f"${emergency_threshold:.2f}"
        else:
            emergency_text = "N/A"

        text = (
            "🔓 <b>RISK UNLOCKED</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Kill switch: OFF\n"
            "✅ Cooldown: CLEARED\n"
            "✅ Loss streak: RESET\n"
            "✅ Emergency: ACKNOWLEDGED\n"
            "✅ Bot: ACTIVE\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance: <code>${balance:.2f}</code>\n"
            f"🏦 Baseline: <code>${initial:.2f}</code>\n"
            f"🚨 Emergency at: <code>{emergency_text}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ Emergency won't re-trigger until\n"
            "baseline is reset via /reset_risk\n\n"
            "💡 If baseline is wrong (old $100),\n"
            "use /reset_risk to fix it."
        )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Unlock command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_reset_risk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Reset risk baseline — NEW COMMAND.

    This ACCEPTS all past losses and starts fresh:
    - initial_balance = current balance
    - start_of_day_balance = current balance
    - peak_balance = current balance
    - All locks cleared
    - All streaks reset
    - Emergency protection re-enabled with NEW baseline

    After this:
    - Drawdown is measured from current balance
    - Emergency triggers at 15% of CURRENT balance (not old $100)

    Example:
    - Old: initial=$100, balance=$39.58, DD=60.4% → LOCKED
    - After reset: initial=$39.58, DD=0% → UNLOCKED
    - Emergency now triggers at $39.58 * 0.85 = $33.64
    """
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        result = controller.reset_risk_baseline(source="telegram")

        if not result.get("reset"):
            await _reply(
                update,
                "❌ Cannot reset — balance is zero"
            )
            return

        lg_result = result.get("loss_guard", {})
        new_baseline = result.get("new_baseline", 0)
        old_initial = lg_result.get("old_initial", 0)
        emergency_threshold = lg_result.get("emergency_threshold", 0)

        text = (
            "🔄 <b>RISK BASELINE RESET</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"📉 Old baseline: <code>${old_initial:.2f}</code>\n"
            f"📈 New baseline: <code>${new_baseline:.2f}</code>\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ All locks: CLEARED\n"
            "✅ Streaks: RESET\n"
            "✅ Daily PnL: RESET\n"
            "✅ Drawdown: 0%\n"
            "✅ Bot: ACTIVE\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            f"🚨 Emergency now triggers at:\n"
            f"   <code>${emergency_threshold:.2f}</code> "
            f"(15% below ${new_baseline:.2f})\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✅ Past losses accepted.\n"
            "🤖 Bot is monitoring with fresh metrics!"
        )

        await _reply(update, text)

    except Exception as e:
        logger.exception(f"Reset risk command error: {e}")
        await _reply(update, f"❌ Error: {e}")


# ═════════════════════════════════════════════════════════════════
#  ACTION COMMANDS
# ═════════════════════════════════════════════════════════════════

@require_auth
async def cmd_force_exit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Force close all positions."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        positions = controller.state.get_all_positions()
        count = len(positions)

        if count == 0:
            await _reply(update, "📂 No positions to close")
            return

        await _reply(update, f"⏳ Closing {count} position(s)...")

        controller.force_exit_all(reason="Telegram /force_exit")

        await _reply(
            update,
            f"✅ <b>FORCE EXIT COMPLETE</b>\n\n"
            f"Closed {count} position(s)"
        )
    except Exception as e:
        logger.exception(f"Force exit command error: {e}")
        await _reply(update, f"❌ Error: {e}")


@require_auth
async def cmd_force_cycle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Manually trigger a trading cycle."""
    controller = _get_controller(context)
    if not controller:
        await _reply(update, "❌ Controller not available")
        return

    try:
        kill_active = controller.kill_switch.is_active()

        if kill_active:
            await _reply(
                update,
                "🔒 <b>Cannot force cycle — kill switch is active</b>\n\n"
                "Use /unlock first, then try again."
            )
            return

        scheduler = controller.scheduler
        if not scheduler:
            await _reply(update, "❌ Scheduler not available")
            return

        await _reply(update, "⚡ Triggering manual cycle...")

        await scheduler.force_cycle()

        await _reply(update, "✅ Manual cycle completed")
    except Exception as e:
        logger.exception(f"Force cycle command error: {e}")
        await _reply(update, f"❌ Error: {e}")